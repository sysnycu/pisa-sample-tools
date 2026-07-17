from __future__ import annotations

import csv
import fcntl
import html
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Sequence
from contextlib import contextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .index import REPORT_INDEX_SCHEMA_VERSION, ReportIndex, build_report_index
from .models import DatasetDescriptor, OutcomeSummary, ReportBundleResult

REPORT_MANIFEST_SCHEMA_VERSION = 3
REPORT_BUILD_VERSION = 9
REPORT_TOOL = "pisa-analysis-tools"
REPORT_ARTIFACT_TYPE = "normalized-report-bundle"


class ReportBundleError(ValueError):
    """Raised for safe, user-facing bundle construction failures."""


def build_report_bundle(
    source_roots: Path | Sequence[Path],
    output_dir: Path,
    *,
    title: str = "PISA Analysis Report",
    overwrite: bool = False,
    lineage: dict[str, Any] | None = None,
    progress: Callable[[str, float, float, str], None] | None = None,
) -> ReportBundleResult:
    """Atomically build a compact report bundle around the normalized index.

    The portable HTML contains aggregates and provenance only.  Per-run traces
    remain lazy paths in SQLite and are never copied or eagerly rendered.
    """

    roots = _normalize_roots(source_roots)
    output_dir = output_dir.expanduser().resolve()
    _validate_destination(output_dir, overwrite=overwrite)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.building-", dir=output_dir.parent))
    try:
        report_dir = stage / "report"
        summary_dir = stage / "summary"
        provenance_dir = stage / "provenance"
        for directory in (report_dir, summary_dir, provenance_dir):
            directory.mkdir(parents=True, exist_ok=True)

        notify = progress or (lambda _stage, _current, _total, _message: None)
        index_build = build_report_index(
            roots, report_dir / "index.sqlite", force=True, progress=notify
        )
        notify("summarize", 9, 11, "Computing report summaries and comparisons")
        with ReportIndex(report_dir / "index.sqlite") as index:
            datasets = index.datasets()
            findings = index.findings()
            relations = index.dataset_relations()
            aliases = _duplicate_aliases(findings)
            included = tuple(dataset for dataset in datasets if dataset.dataset_id not in aliases)
            aggregate = _aggregate_summary(index, included)
            all_runs = index.outcome_summary()
            payload = {
                "title": title,
                "generated_at": datetime.now(UTC).isoformat(),
                "source_fingerprint": index_build.source_fingerprint,
                "summary": aggregate.as_dict(),
                "all_browsable_runs": all_runs.total,
                "aggregate_dataset_count": len(included),
                "dataset_count": len(datasets),
                "datasets": [
                    {
                        **dataset.as_dict(),
                        "source_path": None,
                        "manifest_path": None,
                        "aggregate_included": dataset.dataset_id not in aliases,
                        "alias_of": aliases.get(dataset.dataset_id),
                    }
                    for dataset in datasets
                ],
                "findings": [_portable_value(finding.as_dict()) for finding in findings],
                "comparisons": [_portable_value(relation.as_dict()) for relation in relations],
            }
            _write_json(summary_dir / "summary.json", payload)
            _write_outcome_csv(summary_dir / "outcomes.csv", aggregate)
            _write_dataset_csv(summary_dir / "datasets.csv", datasets, aliases)
            _write_finding_csv(summary_dir / "data_health.csv", payload["findings"])
            _write_json(summary_dir / "data_health.json", payload["findings"])
            _write_comparison_csv(summary_dir / "comparisons.csv", payload["comparisons"])
            (report_dir / "analysis_report.html").write_text(
                _portable_html(payload), encoding="utf-8"
            )

        notify("provenance", 10, 11, "Writing provenance and portable artifacts")
        provenance = {
            "source_fingerprint": index_build.source_fingerprint,
            "sources": [{"label": path.name} for path in roots],
            "trace_policy": "lazy-reference-only",
            "duplicate_aliases_excluded_from_aggregate": aliases,
        }
        _write_json(provenance_dir / "input_manifest.json", provenance)
        outputs = sorted(
            path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file()
        )
        manifest = {
            "tool": REPORT_TOOL,
            "artifact_type": REPORT_ARTIFACT_TYPE,
            "schema_version": REPORT_MANIFEST_SCHEMA_VERSION,
            "report_build_version": REPORT_BUILD_VERSION,
            "report_store_schema": REPORT_INDEX_SCHEMA_VERSION,
            "store_schema_version": REPORT_INDEX_SCHEMA_VERSION,
            "generated_at": payload["generated_at"],
            "source_fingerprint": index_build.source_fingerprint,
            "dataset_count": len(datasets),
            "aggregate_dataset_count": len(included),
            "run_count": all_runs.total,
            "aggregate_run_count": aggregate.total,
            "finding_count": len(findings),
            "entrypoint": "report/analysis_report.html",
            "outputs": outputs,
        }
        if lineage:
            manifest["lineage"] = lineage
        (stage / "manifest.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
        )
        notify("publish", 11, 11, "Atomically publishing the completed report")
        _publish_stage(stage, output_dir, overwrite=overwrite)
        index_build = replace(index_build, database_path=output_dir / "report" / "index.sqlite")
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    return ReportBundleResult(
        output_dir=output_dir,
        report_path=output_dir / "report" / "analysis_report.html",
        index_path=output_dir / "report" / "index.sqlite",
        manifest_path=output_dir / "manifest.yaml",
        summary_json_path=output_dir / "summary" / "summary.json",
        dataset_count=len(datasets),
        aggregate_dataset_count=len(included),
        run_count=all_runs.total,
        finding_count=len(findings),
        source_fingerprint=index_build.source_fingerprint,
        index_build=index_build,
    )


def rebuild_legacy_report(
    legacy_report_dir: Path,
    *,
    source_roots: Path | Sequence[Path] | None = None,
    destination_parent: Path | None = None,
    title: str = "PISA Analysis Report",
) -> ReportBundleResult:
    """Rebuild beside a legacy report without modifying the original directory."""

    legacy = legacy_report_dir.expanduser().resolve()
    if not legacy.is_dir():
        raise ReportBundleError(f"legacy report directory does not exist: {legacy}")
    roots = _legacy_source_roots(legacy) if source_roots is None else _normalize_roots(source_roots)
    parent = (destination_parent or legacy.parent).expanduser().resolve()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = parent / f"{legacy.name}--rebuilt-{stamp}"
    if destination.exists():
        counter = 2
        while (parent / f"{destination.name}-{counter}").exists():
            counter += 1
        destination = parent / f"{destination.name}-{counter}"
    return build_report_bundle(
        roots,
        destination,
        title=title,
        lineage={
            "operation": "non_destructive_legacy_rebuild",
            "source_report_name": legacy.name,
        },
    )


def _normalize_roots(source_roots: Path | Sequence[Path]) -> tuple[Path, ...]:
    roots = (source_roots,) if isinstance(source_roots, Path) else tuple(source_roots)
    if not roots:
        raise ReportBundleError("at least one source root is required")
    normalized = tuple(sorted({Path(path).expanduser().resolve() for path in roots}, key=str))
    missing = [path for path in normalized if not path.is_dir()]
    if missing:
        raise ReportBundleError(f"source root does not exist: {missing[0]}")
    return normalized


def _validate_destination(output_dir: Path, *, overwrite: bool) -> None:
    if output_dir.is_symlink():
        raise ReportBundleError("report output must not be a symbolic link")
    if not output_dir.exists():
        return
    if not output_dir.is_dir():
        raise ReportBundleError(f"report output exists and is not a directory: {output_dir}")
    if not overwrite:
        raise ReportBundleError(f"report output already exists: {output_dir}")
    entries = list(output_dir.iterdir())
    if not entries:
        return
    manifest_path = output_dir / "manifest.yaml"
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ReportBundleError(
            "refusing to overwrite output not owned by pisa-analysis-tools "
            "(valid manifest.yaml required)"
        ) from exc
    if not isinstance(manifest, dict) or manifest.get("tool") != REPORT_TOOL:
        raise ReportBundleError("refusing to overwrite output not owned by pisa-analysis-tools")
    try:
        manifest_schema = int(manifest.get("schema_version") or 0)
        store_schema = int(
            manifest.get("report_store_schema") or manifest.get("store_schema_version") or 0
        )
        build_version = int(manifest.get("report_build_version") or 0)
    except (TypeError, ValueError) as exc:
        raise ReportBundleError("refusing to overwrite a report with invalid version metadata") from exc
    if (
        manifest_schema > REPORT_MANIFEST_SCHEMA_VERSION
        or store_schema > REPORT_INDEX_SCHEMA_VERSION
        or build_version > REPORT_BUILD_VERSION
    ):
        raise ReportBundleError(
            "refusing to overwrite a report produced by a newer schema/build; open it read-only"
        )


def _publish_stage(stage: Path, output_dir: Path, *, overwrite: bool) -> None:
    # Validation before the potentially long build is only an early failure.
    # Serialize and revalidate at publication time so concurrent builders cannot
    # move or restore one another's destination.
    with _destination_publish_lock(output_dir):
        _validate_destination(output_dir, overwrite=overwrite)
        if not output_dir.exists():
            os.replace(stage, output_dir)
            return
        if not overwrite:  # pragma: no cover - guarded by validation above
            raise ReportBundleError(f"report output already exists: {output_dir}")

        # mkdtemp reserves a process- and thread-unique sibling.  os.replace can
        # atomically replace that empty directory, avoiding the old PID-only name
        # collision when two publishers run in one process.
        backup = Path(
            tempfile.mkdtemp(prefix=f".{output_dir.name}.replaced-", dir=output_dir.parent)
        )
        os.replace(output_dir, backup)
        try:
            os.replace(stage, output_dir)
        except BaseException:
            os.replace(backup, output_dir)
            raise
        shutil.rmtree(backup)


@contextmanager
def _destination_publish_lock(output_dir: Path) -> Any:
    lock_path = output_dir.with_name(f".{output_dir.name}.publish.lock")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ReportBundleError(f"cannot acquire report publication lock: {lock_path}") from exc
    try:
        with os.fdopen(descriptor, "r+b", closefd=True) as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except BaseException:
        # fdopen owns the descriptor once constructed; close only if that step
        # itself failed and ownership was never transferred.
        with suppress(OSError):
            os.close(descriptor)
        raise


def _duplicate_aliases(findings: Sequence[Any]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for finding in findings:
        if finding.code != "duplicate_alias":
            continue
        canonical = finding.details.get("canonical_dataset")
        if canonical and finding.dataset_id and finding.dataset_id != canonical:
            aliases[finding.dataset_id] = str(canonical)
    return aliases


def _aggregate_summary(index: ReportIndex, datasets: Sequence[DatasetDescriptor]) -> OutcomeSummary:
    values = [index.outcome_summary(dataset_id=dataset.dataset_id) for dataset in datasets]
    return OutcomeSummary(
        total=sum(item.total for item in values),
        success=sum(item.success for item in values),
        fail=sum(item.fail for item in values),
        invalid=sum(item.invalid for item in values),
        unknown=sum(item.unknown for item in values),
        collision=sum(item.collision for item in values),
    )


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _portable_value(value: Any) -> Any:
    """Remove machine-specific absolute paths from snapshot-visible metadata."""

    if isinstance(value, dict):
        return {str(key): _portable_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_portable_value(item) for item in value]
    if isinstance(value, str) and Path(value).is_absolute():
        return Path(value).name
    return value


def _write_outcome_csv(path: Path, summary: OutcomeSummary) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["outcome", "count"])
        writer.writeheader()
        for name in ("success", "fail", "invalid", "unknown", "collision"):
            writer.writerow({"outcome": name, "count": getattr(summary, name)})


def _write_dataset_csv(
    path: Path, datasets: Sequence[DatasetDescriptor], aliases: dict[str, str]
) -> None:
    columns = [
        "dataset_id",
        "scenario_name",
        "simulator",
        "av",
        "sampler",
        "run_count",
        "attempt_count",
        "aggregate_included",
        "alias_of",
        "health_errors",
        "health_warnings",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for dataset in datasets:
            writer.writerow(
                {
                    "dataset_id": dataset.dataset_id,
                    "scenario_name": dataset.scenario_name,
                    "simulator": dataset.simulator,
                    "av": dataset.av,
                    "sampler": dataset.sampler,
                    "run_count": dataset.run_count,
                    "attempt_count": dataset.attempt_count,
                    "aggregate_included": dataset.dataset_id not in aliases,
                    "alias_of": aliases.get(dataset.dataset_id),
                    "health_errors": dataset.health_counts.get("error", 0),
                    "health_warnings": dataset.health_counts.get("warning", 0),
                }
            )


def _write_finding_csv(path: Path, findings: Sequence[dict[str, Any]]) -> None:
    columns = ["severity", "code", "dataset_id", "run_id", "message", "details_json"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for finding in findings:
            writer.writerow(
                {
                    "severity": finding["severity"],
                    "code": finding["code"],
                    "dataset_id": finding.get("dataset_id"),
                    "run_id": finding.get("run_id"),
                    "message": finding["message"],
                    "details_json": json.dumps(
                        finding.get("details") or {}, ensure_ascii=False, sort_keys=True
                    ),
                }
            )


def _write_comparison_csv(path: Path, relations: Sequence[dict[str, Any]]) -> None:
    columns = [
        "left_dataset_id",
        "right_dataset_id",
        "role",
        "matched_count",
        "left_only_count",
        "right_only_count",
        "reason",
        "details_json",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for relation in relations:
            details = relation.get("details") if isinstance(relation.get("details"), dict) else {}
            writer.writerow(
                {
                    "left_dataset_id": relation.get("left_dataset_id"),
                    "right_dataset_id": relation.get("right_dataset_id"),
                    "role": relation.get("role"),
                    "matched_count": details.get("matched_count"),
                    "left_only_count": details.get("left_only_count"),
                    "right_only_count": details.get("right_only_count"),
                    "reason": details.get("reason"),
                    "details_json": json.dumps(details, ensure_ascii=False, sort_keys=True),
                }
            )


def _portable_html(payload: dict[str, Any]) -> str:
    safe_title = html.escape(str(payload["title"]))
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    data = data.replace("<", "\\u003c").replace("&", "\\u0026")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title}</title><style>
:root{{--ink:#172033;--muted:#667085;--line:#e4e7ec;--panel:#fff;--bg:#f5f7fb;--success:#16794b;--fail:#c4322b;--invalid:#a05a00}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 Inter,ui-sans-serif,system-ui,sans-serif}}
main{{max-width:1180px;margin:auto;padding:40px 24px 80px}}header{{margin-bottom:30px}}h1{{font-size:30px;margin:0 0 6px}}h2{{font-size:18px;margin:28px 0 12px}}
.muted{{color:var(--muted)}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;box-shadow:0 1px 2px #1018280d}}.value{{font-size:26px;font-weight:700}}.bar{{height:12px;display:flex;overflow:hidden;border-radius:999px;background:#eaeef4;margin-top:14px}}.bar span{{min-width:1px}}
.table-wrap{{overflow:auto;background:#fff;border:1px solid var(--line);border-radius:12px}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}}th{{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}}tr:last-child td{{border:0}}.badge{{display:inline-block;padding:2px 8px;border-radius:999px;background:#eef2f8}}.warn{{color:#9a6700}}.error{{color:#b42318}}@media print{{body{{background:white}}main{{padding:0}}.card,.table-wrap{{box-shadow:none}}}}
</style></head><body><main><header><h1>{safe_title}</h1><div class="muted" id="meta"></div></header>
<section><div class="cards" id="cards"></div><div class="bar" id="bar"></div></section>
<section><h2>Datasets</h2><div class="table-wrap"><table><thead><tr><th>Dataset</th><th>System</th><th>Sampler</th><th>Runs</th><th>Aggregate</th><th>Health</th></tr></thead><tbody id="datasets"></tbody></table></div></section>
<section><h2>Data health</h2><div class="table-wrap"><table><thead><tr><th>Severity</th><th>Code</th><th>Dataset</th><th>Finding</th></tr></thead><tbody id="findings"></tbody></table></div></section>
<section><h2>Comparison classification</h2><p class="muted">Only recorded semantics and canonical input matches justify paired claims.</p><div class="table-wrap"><table><thead><tr><th>Left</th><th>Right</th><th>Role</th><th>Matched</th><th>Interpretation</th></tr></thead><tbody id="comparisons"></tbody></table></div></section>
<p class="muted">Portable aggregate snapshot. Open the server-backed report to browse lazy per-run traces.</p>
<script>const D={data};const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
document.getElementById('meta').textContent=`${{D.summary.total.toLocaleString()}} aggregate runs · ${{D.dataset_count}} datasets · generated ${{new Date(D.generated_at).toLocaleString()}}`;
const colors={{success:'#2d9d69',fail:'#d64b45',invalid:'#d98b22',unknown:'#98a2b3'}};for(const k of ['success','fail','invalid','unknown']){{document.getElementById('cards').insertAdjacentHTML('beforeend',`<div class="card"><div class="muted">${{k}}</div><div class="value">${{D.summary[k].toLocaleString()}}</div></div>`);const s=document.createElement('span');s.style.cssText=`width:${{D.summary.total?100*D.summary[k]/D.summary.total:0}}%;background:${{colors[k]}}`;document.getElementById('bar').appendChild(s)}}
document.getElementById('datasets').innerHTML=D.datasets.map(x=>`<tr><td>${{esc(x.dataset_id)}}${{x.alias_of?` <span class="badge">alias of ${{esc(x.alias_of)}}</span>`:''}}</td><td>${{esc([x.simulator,x.av].filter(Boolean).join(' / '))}}</td><td>${{esc(x.sampler)}}</td><td>${{x.run_count.toLocaleString()}}</td><td>${{x.aggregate_included?'Included':'Collapsed'}}</td><td class="${{x.health_counts.error?'error':x.health_counts.warning?'warn':''}}">${{x.health_counts.error}} errors · ${{x.health_counts.warning}} warnings</td></tr>`).join('');
document.getElementById('findings').innerHTML=D.findings.map(x=>`<tr><td class="${{esc(x.severity)}}">${{esc(x.severity)}}</td><td>${{esc(x.code)}}</td><td>${{esc(x.dataset_id)}}</td><td style="white-space:normal">${{esc(x.message)}}</td></tr>`).join('');
document.getElementById('comparisons').innerHTML=D.comparisons.map(x=>{{const d=x.details||{{}};return `<tr><td>${{esc(x.left_dataset_id)}}</td><td>${{esc(x.right_dataset_id)}}</td><td><span class="badge">${{esc(x.role)}}</span></td><td>${{Number(d.matched_count||0).toLocaleString()}}</td><td style="white-space:normal">${{esc(d.reason||'Recorded classification')}}</td></tr>`}}).join('');</script></main></body></html>"""


def _legacy_source_roots(legacy: Path) -> tuple[Path, ...]:
    candidates = (
        legacy / "provenance" / "input_manifest.yaml",
        legacy / "provenance" / "input_manifest.json",
    )
    document: dict[str, Any] = {}
    for path in candidates:
        if not path.is_file():
            continue
        try:
            parsed = (
                json.loads(path.read_text(encoding="utf-8"))
                if path.suffix == ".json"
                else yaml.safe_load(path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
            raise ReportBundleError(f"failed to read legacy input manifest: {path}") from exc
        if isinstance(parsed, dict):
            document = parsed
            break
    values: list[str] = []
    values.extend(str(item) for item in document.get("inputs") or [])
    for dataset in document.get("datasets") or []:
        if isinstance(dataset, dict):
            value = dataset.get("results") or dataset.get("results_path")
            if value:
                values.append(str(value))
    roots = tuple(Path(value).expanduser().resolve() for value in values)
    if not roots:
        raise ReportBundleError(
            "legacy report does not record source inputs; pass source_roots explicitly"
        )
    missing = [path for path in roots if not path.is_dir()]
    if missing:
        raise ReportBundleError(f"recorded legacy source no longer exists: {missing[0]}")
    return tuple(sorted(set(roots), key=str))
