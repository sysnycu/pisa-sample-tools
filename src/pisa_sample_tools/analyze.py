from __future__ import annotations

import csv
import html
import json
import math
import shutil
import statistics
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from simcore.sampler import create_sampler, load_parameter_space
from simcore.sampler.loader import load_sampler_spec, resolve_sampler_source

from pisa_sample_tools.exporter import (
    EXPLICIT_SAMPLE_FILE_NAME,
    _load_mapping_file,
    _runner_scenario_path,
    scenario_base_from_path,
)
from pisa_sample_tools.outcome_eval import OutcomeEvalError, OutcomeEvalMode, evaluate_outcomes


class AnalyzeError(ValueError):
    """Raised for user-facing analysis failures."""


OUTCOME_COLORS = {
    "success": "#16a34a",
    "invalid": "#2563eb",
    "fail": "#dc2626",
    "test_fail": "#dc2626",
    "failure": "#dc2626",
    "failed": "#dc2626",
}
DEFAULT_PALETTE = [
    "#7c3aed",
    "#f59e0b",
    "#0891b2",
    "#be123c",
    "#4b5563",
    "#84cc16",
    "#c026d3",
    "#0f766e",
]


@dataclass(frozen=True)
class SampleRecord:
    sample_id: str
    params: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str | None = None
    outcome: str | None = None
    stop_condition: str | None = None
    stop_reason: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    result_path: Path | None = None
    post_outcome: dict[str, Any] | None = None


@dataclass(frozen=True)
class AnalysisResult:
    output_dir: Path
    report_path: Path
    summary_path: Path
    csv_path: Path
    figure_paths: list[Path]
    record_count: int
    selected_params: tuple[str, ...]


@dataclass(frozen=True)
class ColorSpec:
    color_by: str
    mode: str
    values: list[str]
    palette: dict[str, str]
    numeric_values: list[float | None]
    numeric_min: float | None = None
    numeric_max: float | None = None


def analyze_samples(
    *,
    output_dir: Path,
    runner_spec_path: Path | None = None,
    samples_path: Path | None = None,
    results_path: Path | None = None,
    params: list[str] | None = None,
    color_by: str = "outcome",
    bins: int = 28,
    post_outcome_config_path: Path | None = None,
    post_outcome_mode: str | OutcomeEvalMode = OutcomeEvalMode.OVERLAY,
    overwrite: bool = False,
) -> AnalysisResult:
    source_count = sum(path is not None for path in (runner_spec_path, samples_path, results_path))
    if source_count != 1:
        raise AnalyzeError("exactly one of runner_spec_path, samples_path, or results_path is required")
    if bins <= 0:
        raise AnalyzeError("bins must be greater than 0")

    if runner_spec_path is not None:
        records = load_records_from_runner_spec(runner_spec_path)
        source_label = str(runner_spec_path)
        source_type = "runner_spec"
    elif samples_path is not None:
        records = load_records_from_samples(samples_path)
        source_label = str(samples_path)
        source_type = "samples"
    else:
        assert results_path is not None
        records = load_records_from_results(results_path)
        source_label = str(results_path)
        source_type = "results"

    if not records:
        raise AnalyzeError("no sample records were loaded")

    selected_params = _select_params(records, params)
    _prepare_analysis_dir(output_dir, overwrite=overwrite)

    post_outcome_summary: dict[str, Any] | None = None
    if post_outcome_config_path is not None:
        if results_path is None:
            raise AnalyzeError("--post-outcome-config is only supported with --results")
        records, post_outcome_summary = _apply_post_outcome_eval(
            records,
            results_path=results_path,
            config_path=post_outcome_config_path,
            mode=OutcomeEvalMode(post_outcome_mode),
            output_dir=output_dir / "post_outcome_eval",
        )

    rows, columns = _records_to_rows(records)
    csv_path = output_dir / "samples.csv"
    _write_csv(csv_path, rows, columns)

    summary = _build_analysis_summary(
        records,
        source_type=source_type,
        source_label=source_label,
        selected_params=selected_params,
        color_by=color_by,
        bins=bins,
        post_outcome=post_outcome_summary,
    )
    summary_path = output_dir / "summary.yaml"
    _write_yaml(summary_path, summary)

    figures_dir = output_dir / "figures"
    figures_dir.mkdir()
    figure_paths = _write_figures(
        records,
        selected_params=selected_params,
        color_by=color_by,
        bins=bins,
        figures_dir=figures_dir,
    )
    report_path = output_dir / "report.html"
    _write_report(
        report_path,
        summary=summary,
        records=records,
        figure_paths=figure_paths,
        output_dir=output_dir,
    )

    return AnalysisResult(
        output_dir=output_dir,
        report_path=report_path,
        summary_path=summary_path,
        csv_path=csv_path,
        figure_paths=figure_paths,
        record_count=len(records),
        selected_params=selected_params,
    )


def load_records_from_runner_spec(runner_spec_path: Path) -> list[SampleRecord]:
    runner_spec = _load_mapping_file(runner_spec_path, label="runner spec")
    sampler_runtime_spec = runner_spec.get("sampler")
    if not isinstance(sampler_runtime_spec, dict):
        raise AnalyzeError("runner spec must contain sampler mapping/object")
    scenario_path = _runner_scenario_path(runner_spec, runner_spec_path)
    scenario_base = scenario_base_from_path(scenario_path)
    try:
        sampler_spec = load_sampler_spec(sampler_runtime_spec, source_base_path=scenario_base)
        source_path, source_type = resolve_sampler_source(sampler_spec)
        parameter_space = load_parameter_space(source_path, source_type)
        sampler = create_sampler(sampler_spec, parameter_space)
    except Exception as exc:
        raise AnalyzeError(str(exc)) from exc

    records: list[SampleRecord] = []
    index = 1
    while True:
        sample = sampler.next()
        if sample is None:
            return records
        sample_id = str(sample.id) if sample.id is not None else str(index)
        records.append(
            SampleRecord(
                sample_id=sample_id,
                params=dict(sample.params),
                metadata=dict(sample.metadata),
            )
        )
        index += 1


def load_records_from_samples(samples_path: Path) -> list[SampleRecord]:
    samples_path = Path(samples_path).expanduser()
    if samples_path.is_file():
        if samples_path.suffix.lower() == ".csv":
            return _load_records_from_csv_file(samples_path)
        return _load_records_from_explicit_file(samples_path)
    if not samples_path.is_dir():
        raise AnalyzeError(f"samples path does not exist: {samples_path}")

    explicit_file = _find_explicit_sample_file(samples_path)
    if explicit_file.exists():
        return _load_records_from_explicit_file(explicit_file)

    manifest_path = samples_path / "manifest.yaml"
    if manifest_path.exists():
        records = _load_records_from_manifest(samples_path, manifest_path)
        if records:
            return records

    explicit_files = sorted(samples_path.glob(f"*/{EXPLICIT_SAMPLE_FILE_NAME}"))
    if not explicit_files:
        explicit_files = sorted(samples_path.glob("*/explicit.yaml"))
    if explicit_files:
        records: list[SampleRecord] = []
        for path in explicit_files:
            records.extend(_load_records_from_explicit_file(path, result_path=path.parent))
        return records

    raise AnalyzeError(
        f"could not find {EXPLICIT_SAMPLE_FILE_NAME}, explicit.yaml, or manifest.yaml in samples path: {samples_path}"
    )


def _find_explicit_sample_file(samples_path: Path) -> Path:
    explicit_file = samples_path / EXPLICIT_SAMPLE_FILE_NAME
    if explicit_file.exists():
        return explicit_file
    return samples_path / "explicit.yaml"


def load_records_from_results(results_path: Path) -> list[SampleRecord]:
    results_path = Path(results_path).expanduser()
    if not results_path.is_dir():
        raise AnalyzeError(f"results path does not exist or is not a directory: {results_path}")

    records: list[SampleRecord] = []
    for iteration_dir in sorted(results_path.glob("iteration_*"), key=_iteration_sort_key):
        if not iteration_dir.is_dir():
            continue
        sample_id = iteration_dir.name.removeprefix("iteration_")
        result_csv = iteration_dir / "monitor" / "result.csv"
        if not result_csv.exists():
            records.append(SampleRecord(sample_id=sample_id, params={}, result_path=iteration_dir))
            continue
        rows = _read_csv_dicts(result_csv)
        if not rows:
            records.append(SampleRecord(sample_id=sample_id, params={}, result_path=iteration_dir))
            continue
        row = rows[-1]
        params = _parse_json_mapping(row.get("run.params"))
        metrics = {
            key: _coerce_scalar(value)
            for key, value in row.items()
            if key and not key.startswith("run.") and value not in {"", None}
        }
        records.append(
            SampleRecord(
                sample_id=sample_id,
                params=params,
                status=_none_if_empty(row.get("run.status")),
                outcome=_none_if_empty(row.get("run.test_outcome")),
                stop_condition=_none_if_empty(row.get("run.stop_condition")),
                stop_reason=_none_if_empty(row.get("run.stop_reason")),
                metrics=metrics,
                result_path=iteration_dir,
            )
        )
    return records


def _load_records_from_manifest(samples_root: Path, manifest_path: Path) -> list[SampleRecord]:
    manifest = _load_mapping_file(manifest_path, label="sample manifest")
    records: list[SampleRecord] = []
    for shard in manifest.get("shards", []):
        if not isinstance(shard, dict):
            continue
        raw_path = shard.get("sample_file_path")
        if raw_path is None:
            continue
        sample_path = _resolve_manifest_path(samples_root, Path(raw_path))
        if sample_path.exists():
            records.extend(_load_records_from_explicit_file(sample_path, result_path=sample_path.parent))
    return records


def _load_records_from_csv_file(path: Path) -> list[SampleRecord]:
    reserved = {
        "sample_id",
        "id",
        "status",
        "outcome",
        "stop_condition",
        "stop_reason",
        "result_path",
    }
    records: list[SampleRecord] = []
    for index, row in enumerate(_read_csv_dicts(path), start=1):
        sample_id = row.get("sample_id") or row.get("id") or str(index)
        params: dict[str, Any] = {}
        metrics: dict[str, Any] = {}
        for key, value in row.items():
            if key is None or value in {None, ""}:
                continue
            if key.startswith("param."):
                params[key.removeprefix("param.")] = _coerce_scalar(value)
            elif key.startswith("metric."):
                metrics[key.removeprefix("metric.")] = _coerce_scalar(value)
            elif key not in reserved:
                params[key] = _coerce_scalar(value)
        records.append(
            SampleRecord(
                sample_id=str(sample_id),
                params=params,
                status=_none_if_empty(row.get("status")),
                outcome=_none_if_empty(row.get("outcome")),
                stop_condition=_none_if_empty(row.get("stop_condition")),
                stop_reason=_none_if_empty(row.get("stop_reason")),
                metrics=metrics,
                result_path=Path(row["result_path"]) if row.get("result_path") else path,
            )
        )
    return records


def _resolve_manifest_path(samples_root: Path, raw_path: Path) -> Path:
    if raw_path.is_absolute() or raw_path.exists():
        return raw_path
    parts = raw_path.parts
    if samples_root.name in parts:
        index = parts.index(samples_root.name)
        return samples_root.parent.joinpath(*parts[index:])
    return samples_root / raw_path


def _load_records_from_explicit_file(
    path: Path,
    *,
    result_path: Path | None = None,
) -> list[SampleRecord]:
    data = _load_mapping_file(path, label="explicit samples")
    raw_samples = data.get("samples")
    if not isinstance(raw_samples, list):
        raise AnalyzeError(f"explicit sample file must contain samples list: {path}")

    records: list[SampleRecord] = []
    for index, raw_sample in enumerate(raw_samples, start=1):
        if not isinstance(raw_sample, dict):
            raise AnalyzeError(f"sample entry #{index} in {path} must be a mapping")
        raw_params = raw_sample.get("params")
        if not isinstance(raw_params, dict):
            raise AnalyzeError(f"sample entry #{index} in {path} must contain params mapping")
        sample_id = raw_sample.get("id")
        records.append(
            SampleRecord(
                sample_id=str(sample_id) if sample_id is not None else str(index),
                params=dict(raw_params),
                metadata=dict(raw_sample.get("metadata") or {}),
                result_path=result_path or path,
            )
        )
    return records


def _select_params(records: list[SampleRecord], params: list[str] | None) -> tuple[str, ...]:
    if params:
        requested = tuple(param.strip() for param in params if param.strip())
        selected = requested[:3]
        known_params = {name for record in records for name in record.params}
        missing = [name for name in requested if name not in known_params]
        if missing:
            raise AnalyzeError(f"selected param(s) not found: {', '.join(missing)}")
        return selected

    numeric = _numeric_param_names(records)
    if numeric:
        return tuple(numeric[:3])
    all_params = sorted({name for record in records for name in record.params})
    return tuple(all_params[:3])


def _numeric_param_names(records: list[SampleRecord]) -> list[str]:
    names = sorted({name for record in records for name in record.params})
    numeric: list[str] = []
    for name in names:
        values = [_as_float(record.params.get(name)) for record in records]
        values = [value for value in values if value is not None]
        if values:
            numeric.append(name)
    return numeric


def _prepare_analysis_dir(output_dir: Path, *, overwrite: bool) -> None:
    if output_dir.exists():
        if not output_dir.is_dir():
            raise AnalyzeError(f"output path exists and is not a directory: {output_dir}")
        if not overwrite:
            raise AnalyzeError(f"analysis output already exists: {output_dir}")
        marker = output_dir / "summary.yaml"
        if not marker.exists():
            raise AnalyzeError(
                "analysis output exists but summary.yaml was not found; refusing to overwrite"
            )
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)


def _build_analysis_summary(
    records: list[SampleRecord],
    *,
    source_type: str,
    source_label: str,
    selected_params: tuple[str, ...],
    color_by: str,
    bins: int,
    post_outcome: dict[str, Any] | None,
) -> dict[str, Any]:
    outcomes = Counter(record.outcome or "unknown" for record in records)
    statuses = Counter(record.status or "unknown" for record in records)
    stop_conditions = Counter(record.stop_condition or "unknown" for record in records)
    param_names = sorted({name for record in records for name in record.params})
    metric_names = sorted({name for record in records for name in record.metrics})

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_type": source_type,
        "source": source_label,
        "record_count": len(records),
        "selected_params": list(selected_params),
        "color_by": color_by,
        "bins": bins,
        "param_count": len(param_names),
        "metric_count": len(metric_names),
        "params": _parameter_summary(records, param_names),
        "metrics": _metric_summary(records, metric_names),
        "outcomes": dict(outcomes),
        "statuses": dict(statuses),
        "stop_conditions": dict(stop_conditions),
        "missing_result_count": sum(record.status is None and record.outcome is None for record in records),
        "post_outcome": post_outcome,
    }


def _apply_post_outcome_eval(
    records: list[SampleRecord],
    *,
    results_path: Path,
    config_path: Path,
    mode: OutcomeEvalMode,
    output_dir: Path,
) -> tuple[list[SampleRecord], dict[str, Any]]:
    try:
        result = evaluate_outcomes(
            input_path=results_path,
            config_path=config_path,
            output_dir=output_dir,
            mode=mode,
            overwrite=True,
        )
    except OutcomeEvalError as exc:
        raise AnalyzeError(f"post outcome eval failed: {exc}") from exc

    outcomes_by_path = {outcome.scenario_path.resolve(): outcome for outcome in result.outcomes}
    updated_records: list[SampleRecord] = []
    matched = 0
    for record in records:
        outcome = outcomes_by_path.get(record.result_path.resolve()) if record.result_path else None
        if outcome is None:
            updated_records.append(record)
            continue
        matched += 1
        updated_records.append(
            replace(
                record,
                post_outcome={
                    "test_outcome": outcome.test_outcome,
                    "stop_condition": outcome.stop_condition,
                    "stop_reason": outcome.stop_reason,
                    "condition_code": outcome.code.name.lower(),
                    "condition_name": outcome.condition_name,
                    "triggered": outcome.triggered,
                    "detail": outcome.detail,
                },
            )
        )

    counts = Counter(
        record.post_outcome.get("test_outcome", "unknown")
        for record in updated_records
        if record.post_outcome is not None
    )
    return updated_records, {
        "config_path": str(config_path),
        "mode": mode.value,
        "output_dir": str(result.output_dir),
        "summary_csv_path": str(result.summary_csv_path),
        "manifest_path": str(result.manifest_path),
        "matched_records": matched,
        "evaluated_scenarios": len(result.outcomes),
        "triggered_count": sum(outcome.triggered for outcome in result.outcomes),
        "outcomes": dict(counts),
    }


def _parameter_summary(records: list[SampleRecord], names: list[str]) -> dict[str, dict[str, Any]]:
    return {name: _value_summary([record.params.get(name) for record in records]) for name in names}


def _metric_summary(records: list[SampleRecord], names: list[str]) -> dict[str, dict[str, Any]]:
    return {name: _value_summary([record.metrics.get(name) for record in records]) for name in names}


def _value_summary(values: list[Any]) -> dict[str, Any]:
    present = [value for value in values if value not in {None, ""}]
    floats = [_as_float(value) for value in present]
    numeric = [value for value in floats if value is not None]
    summary: dict[str, Any] = {
        "count": len(present),
        "missing": len(values) - len(present),
        "unique": len({str(value) for value in present}),
        "type": "numeric" if numeric else "categorical",
    }
    if numeric:
        summary.update(
            {
                "min": min(numeric),
                "max": max(numeric),
                "mean": statistics.fmean(numeric),
                "std": statistics.pstdev(numeric) if len(numeric) > 1 else 0.0,
            }
        )
    else:
        summary["top_values"] = dict(Counter(str(value) for value in present).most_common(10))
    return summary


def _records_to_rows(records: list[SampleRecord]) -> tuple[list[dict[str, Any]], list[str]]:
    param_names = sorted({name for record in records for name in record.params})
    metric_names = sorted({name for record in records for name in record.metrics})
    columns = [
        "sample_id",
        "status",
        "outcome",
        "stop_condition",
        "stop_reason",
        "post_outcome",
        "post_stop_condition",
        "post_condition_code",
        "post_triggered",
        "result_path",
        *[f"param.{name}" for name in param_names],
        *[f"metric.{name}" for name in metric_names],
    ]
    rows: list[dict[str, Any]] = []
    for record in records:
        row: dict[str, Any] = {
            "sample_id": record.sample_id,
            "status": record.status or "",
            "outcome": record.outcome or "",
            "stop_condition": record.stop_condition or "",
            "stop_reason": record.stop_reason or "",
            "post_outcome": (record.post_outcome or {}).get("test_outcome", ""),
            "post_stop_condition": (record.post_outcome or {}).get("stop_condition", ""),
            "post_condition_code": (record.post_outcome or {}).get("condition_code", ""),
            "post_triggered": (record.post_outcome or {}).get("triggered", ""),
            "result_path": str(record.result_path) if record.result_path is not None else "",
        }
        row.update({f"param.{name}": record.params.get(name, "") for name in param_names})
        row.update({f"metric.{name}": record.metrics.get(name, "") for name in metric_names})
        rows.append(row)
    return rows, columns


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=False), encoding="utf-8")


def _write_figures(
    records: list[SampleRecord],
    *,
    selected_params: tuple[str, ...],
    color_by: str,
    bins: int,
    figures_dir: Path,
) -> list[Path]:
    figure_paths: list[Path] = []
    color_spec = _build_color_spec(records, color_by)

    overview = figures_dir / "class_counts.svg"
    overview.write_text(_color_overview_svg(color_spec), encoding="utf-8")
    figure_paths.append(overview)

    for param in selected_params:
        values = [record.params.get(param) for record in records]
        path = figures_dir / f"hist_{_slug(param)}.svg"
        path.write_text(_histogram_svg(param, values, color_spec, bins=bins), encoding="utf-8")
        figure_paths.append(path)

    numeric_params = [param for param in selected_params if _param_is_numeric(records, param)]
    if len(numeric_params) >= 2:
        scatter = figures_dir / "scatter_2d.svg"
        scatter.write_text(
            _scatter_2d_svg(records, numeric_params[0], numeric_params[1], color_spec),
            encoding="utf-8",
        )
        figure_paths.append(scatter)

        heatmap = figures_dir / "coverage_heatmap.svg"
        heatmap.write_text(
            _coverage_heatmap_svg(records, numeric_params[0], numeric_params[1]),
            encoding="utf-8",
        )
        figure_paths.append(heatmap)

    if len(numeric_params) >= 3:
        scatter3d = figures_dir / "scatter_3d.html"
        scatter3d.write_text(
            _scatter_3d_html(records, numeric_params[:3], color_spec),
            encoding="utf-8",
        )
        figure_paths.append(scatter3d)

    if len(numeric_params) >= 2:
        matrix = figures_dir / "pair_matrix.svg"
        matrix.write_text(_pair_matrix_svg(records, numeric_params, color_spec), encoding="utf-8")
        figure_paths.append(matrix)

    return figure_paths


def _write_report(
    path: Path,
    *,
    summary: dict[str, Any],
    records: list[SampleRecord],
    figure_paths: list[Path],
    output_dir: Path,
) -> None:
    figure_blocks = []
    for figure_path in figure_paths:
        relative = figure_path.relative_to(output_dir)
        title = figure_path.stem.replace("_", " ").title()
        if figure_path.suffix == ".html":
            body = f'<iframe src="{html.escape(str(relative))}" loading="lazy"></iframe>'
        else:
            body = f'<img src="{html.escape(str(relative))}" alt="{html.escape(title)}">'
        figure_blocks.append(f"<section><h2>{html.escape(title)}</h2>{body}</section>")

    param_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(name)}</td>"
        f"<td>{html.escape(info['type'])}</td>"
        f"<td>{info['count']}</td>"
        f"<td>{info['missing']}</td>"
        f"<td>{_fmt(info.get('min'))}</td>"
        f"<td>{_fmt(info.get('max'))}</td>"
        f"<td>{_fmt(info.get('mean'))}</td>"
        "</tr>"
        for name, info in summary["params"].items()
    )
    dynamic_explorer = _dynamic_explorer_html(summary, records)
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>PISA Sample Analysis</title>
  <style>
    body {{ margin: 0; font-family: Inter, system-ui, sans-serif; color: #17202a; background: #f5f7fa; }}
    header {{ background: #102033; color: white; padding: 28px 36px; }}
    main {{ padding: 24px 36px 48px; max-width: 1280px; margin: 0 auto; }}
    .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 18px 0; }}
    .stat {{ background: white; border: 1px solid #d9e1ea; border-radius: 8px; padding: 14px; }}
    .stat b {{ display: block; font-size: 24px; margin-top: 4px; }}
    section {{ background: white; border: 1px solid #d9e1ea; border-radius: 8px; padding: 18px; margin: 18px 0; }}
    img {{ width: 100%; height: auto; border: 1px solid #e1e7ef; }}
    iframe {{ width: 100%; min-height: 680px; border: 1px solid #e1e7ef; background: white; }}
    canvas {{ display: block; width: 100%; min-height: 620px; background: #101820; border: 1px solid #d9e1ea; border-radius: 6px; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
    th, td {{ border-bottom: 1px solid #e6ecf2; text-align: left; padding: 8px 10px; }}
    code {{ background: #eef3f7; padding: 2px 5px; border-radius: 4px; }}
    label {{ display: grid; gap: 4px; font-size: 13px; font-weight: 600; color: #314155; }}
    select, input[type="number"], button {{ min-height: 34px; border: 1px solid #b8c4d0; border-radius: 6px; background: white; padding: 6px 8px; }}
    button {{ cursor: pointer; background: #102033; color: white; border-color: #102033; }}
    .controls {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; align-items: end; margin-bottom: 14px; }}
    .filter-box {{ display: flex; flex-wrap: wrap; gap: 8px 14px; margin: 12px 0; }}
    .filter-box label {{ display: inline-flex; grid-template-columns: none; align-items: center; gap: 6px; font-weight: 500; }}
    .detail {{ white-space: pre-wrap; overflow: auto; max-height: 260px; background: #f7fafc; border: 1px solid #d9e1ea; border-radius: 6px; padding: 12px; }}
    .subtle-panel {{ background: #f8fafc; border: 1px solid #d9e1ea; border-radius: 8px; padding: 14px; margin: 12px 0; }}
    .inline-controls {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; align-items: end; }}
  </style>
</head>
<body>
  <header>
    <h1>PISA Sample Analysis</h1>
    <div>Source: <code>{html.escape(str(summary["source"]))}</code></div>
  </header>
  <main>
    <div class="stats">
      <div class="stat">Records<b>{summary["record_count"]}</b></div>
      <div class="stat">Parameters<b>{summary["param_count"]}</b></div>
      <div class="stat">Metrics<b>{summary["metric_count"]}</b></div>
      <div class="stat">Color By<b>{html.escape(str(summary["color_by"]))}</b></div>
    </div>
    <section>
      <h2>Outcome Counts</h2>
      <pre>{html.escape(json.dumps(summary["outcomes"], indent=2))}</pre>
    </section>
    <section>
      <h2>Parameter Summary</h2>
      <table>
        <thead><tr><th>Name</th><th>Type</th><th>Count</th><th>Missing</th><th>Min</th><th>Max</th><th>Mean</th></tr></thead>
        <tbody>{param_rows}</tbody>
      </table>
    </section>
    {dynamic_explorer}
    {''.join(figure_blocks)}
  </main>
</body>
</html>
"""
    path.write_text(html_text, encoding="utf-8")


def _dynamic_explorer_html(summary: dict[str, Any], records: list[SampleRecord]) -> str:
    payload = {
        "records": [
            {
                "sample_id": record.sample_id,
                "params": record.params,
                "status": record.status,
                "outcome": record.outcome,
                "stop_condition": record.stop_condition,
                "stop_reason": record.stop_reason,
                "metrics": record.metrics,
                "result_path": str(record.result_path) if record.result_path is not None else "",
                "post_outcome": record.post_outcome,
            }
            for record in records
        ],
        "paramNames": list(summary["params"]),
        "metricNames": list(summary["metrics"]),
        "selectedParams": list(summary["selected_params"]),
        "defaultColorBy": summary["color_by"],
        "defaultBins": summary["bins"],
        "postOutcomeSummary": summary.get("post_outcome"),
    }
    payload_json = json.dumps(payload, ensure_ascii=True).replace("</", "<\\/")
    return f"""
    <section id="dynamic-explorer">
      <h2>Dynamic Explorer</h2>
      <p class="muted">All discovered parameters and metrics are available in the selectors below. CLI --params only chooses the initial axes.</p>
      <div class="controls">
        <label>X parameter<select id="dyn-x"></select></label>
        <label>Y parameter<select id="dyn-y"></select></label>
        <label>Z parameter<select id="dyn-z"></select></label>
        <label>Color by<select id="dyn-color"></select></label>
        <label>Outcome source<select id="dyn-outcome-source"><option value="original">Original</option><option value="post">Post Eval</option><option value="lab">Lab Draft</option></select></label>
        <label>View<select id="dyn-view"><option value="auto">auto</option><option value="1d">1D</option><option value="2d">2D</option><option value="3d">3D</option></select></label>
        <label>1D bins<input id="dyn-bins" type="number" min="1" max="500" step="1"></label>
        <button id="dyn-download" type="button">Download Filtered CSV</button>
      </div>
      <div id="post-summary" class="subtle-panel"></div>
      <div class="subtle-panel">
        <h3>Post Outcome Lab</h3>
        <div class="inline-controls">
          <label>Value source<select id="lab-source"><option value="metric">Metric</option><option value="param">Param</option></select></label>
          <label>Field<select id="lab-field"></select></label>
          <label>Operator<select id="lab-op"><option value="lt">&lt;</option><option value="le">&lt;=</option><option value="gt">&gt;</option><option value="ge">&gt;=</option><option value="eq">==</option><option value="between">between</option><option value="outside">outside</option></select></label>
          <label>Value<input id="lab-value" type="text" placeholder="1.0 or min,max"></label>
          <label>Outcome<select id="lab-outcome"><option value="fail">fail</option><option value="invalid">invalid</option><option value="success">success</option></select></label>
          <label>Name<input id="lab-name" type="text" value="draft_condition"></label>
          <button id="lab-apply" type="button">Apply Draft</button>
        </div>
        <pre id="lab-summary" class="detail">No lab rule applied.</pre>
      </div>
      <div><strong>Outcome filters</strong><div id="dyn-outcomes" class="filter-box"></div></div>
      <div><strong>Status filters</strong><div id="dyn-statuses" class="filter-box"></div></div>
      <canvas id="dyn-canvas"></canvas>
      <div class="stats">
        <div class="stat">Visible<b id="dyn-visible">0</b></div>
        <div class="stat">Mode<b id="dyn-mode">2D</b></div>
        <div class="stat">Color<b id="dyn-classes">0</b></div>
        <div class="stat">Params<b>{len(summary["params"])}</b></div>
        <div class="stat">Metrics<b>{len(summary["metrics"])}</b></div>
      </div>
      <h3>Selected Sample</h3>
      <pre id="dyn-detail" class="detail">Click a point to inspect a sample.</pre>
      <script id="pisa-analysis-data" type="application/json">{payload_json}</script>
      <script>
(() => {{
  const payload = JSON.parse(document.getElementById('pisa-analysis-data').textContent);
  const records = payload.records;
  const paramNames = payload.paramNames;
  const metricNames = payload.metricNames;
  const selected = payload.selectedParams;
  const semanticColors = {{"success":"#16a34a","invalid":"#2563eb","fail":"#dc2626","test_fail":"#dc2626","failure":"#dc2626","failed":"#dc2626"}};
  const paletteBase = ['#7c3aed','#f59e0b','#0891b2','#be123c','#4b5563','#84cc16','#c026d3','#0f766e'];
  const els = {{
    x: document.getElementById('dyn-x'),
    y: document.getElementById('dyn-y'),
    z: document.getElementById('dyn-z'),
    color: document.getElementById('dyn-color'),
    outcomeSource: document.getElementById('dyn-outcome-source'),
    view: document.getElementById('dyn-view'),
    bins: document.getElementById('dyn-bins'),
    outcomes: document.getElementById('dyn-outcomes'),
    statuses: document.getElementById('dyn-statuses'),
    canvas: document.getElementById('dyn-canvas'),
    visible: document.getElementById('dyn-visible'),
    mode: document.getElementById('dyn-mode'),
    classes: document.getElementById('dyn-classes'),
    detail: document.getElementById('dyn-detail'),
    download: document.getElementById('dyn-download'),
    postSummary: document.getElementById('post-summary'),
    labSource: document.getElementById('lab-source'),
    labField: document.getElementById('lab-field'),
    labOp: document.getElementById('lab-op'),
    labValue: document.getElementById('lab-value'),
    labOutcome: document.getElementById('lab-outcome'),
    labName: document.getElementById('lab-name'),
    labApply: document.getElementById('lab-apply'),
    labSummary: document.getElementById('lab-summary')
  }};
  const ctx = els.canvas.getContext('2d');
  let projected = [];
  let updatingFilters = false;
  let yaw = -0.65, pitch = 0.55, zoom = 1.0, dragging = false, lastX = 0, lastY = 0;

  function addOptions(select, values, includeNone=false) {{
    select.textContent = '';
    if (includeNone) {{
      const opt = document.createElement('option');
      opt.value = ''; opt.textContent = '(none)';
      select.appendChild(opt);
    }}
    for (const value of values) {{
      const opt = document.createElement('option');
      opt.value = value; opt.textContent = value;
      select.appendChild(opt);
    }}
  }}
  function countValues(values) {{
    const counts = new Map();
    for (const value of values) counts.set(value, (counts.get(value) || 0) + 1);
    return [...counts.entries()].sort((a,b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0])));
  }}
  function checkboxGroup(container, values) {{
    container.textContent = '';
    for (const [value, count] of countValues(values)) {{
      const id = 'filter-' + container.id + '-' + String(value).replace(/[^A-Za-z0-9_-]/g, '_');
      const label = document.createElement('label');
      label.innerHTML = `<input id="${{id}}" type="checkbox" value="${{String(value).replace(/"/g, '&quot;')}}" checked> ${{value}} (${{count}})`;
      container.appendChild(label);
      label.querySelector('input').addEventListener('change', draw);
    }}
  }}
  function checked(container) {{
    return new Set([...container.querySelectorAll('input:checked')].map(input => input.value));
  }}
  function numericValue(record, param) {{
    if (!param) return null;
    const value = record.params[param];
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }}
  function activeOutcomeRecord(record) {{
    const source = els.outcomeSource.value;
    if (source === 'post' && record.post_outcome) return record.post_outcome;
    if (source === 'lab' && record.lab_outcome) return record.lab_outcome;
    return null;
  }}
  function effectiveOutcome(record) {{
    const active = activeOutcomeRecord(record);
    return active ? (active.test_outcome || 'unknown') : (record.outcome || 'unknown');
  }}
  function effectiveStopCondition(record) {{
    const active = activeOutcomeRecord(record);
    return active ? (active.stop_condition || '') : (record.stop_condition || '');
  }}
  function colorValue(record, colorBy) {{
    if (!colorBy || colorBy === 'none') return 'sample';
    if (colorBy === 'outcome') return effectiveOutcome(record);
    if (colorBy === 'status') return record.status || 'unknown';
    if (colorBy === 'stop_condition') return effectiveStopCondition(record) || 'unknown';
    if (colorBy.startsWith('param:')) return String(record.params[colorBy.slice(6)] ?? 'missing');
    if (colorBy.startsWith('metric:')) return String(record.metrics[colorBy.slice(7)] ?? 'missing');
    return 'sample';
  }}
  function numericColorValue(record, colorBy) {{
    let value = null;
    if (colorBy.startsWith('param:')) value = record.params[colorBy.slice(6)];
    else if (colorBy.startsWith('metric:')) value = record.metrics[colorBy.slice(7)];
    else return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }}
  function continuousBlue(value, min, max) {{
    const t = max <= min ? 0.5 : Math.max(0, Math.min(1, (value - min) / (max - min)));
    const r = Math.round(239 - t * 198);
    const g = Math.round(246 - t * 111);
    const b = Math.round(255 - t * 35);
    return `rgb(${{r}},${{g}},${{b}})`;
  }}
  function colorOptions() {{
    return ['none','outcome','status','stop_condition', ...paramNames.map(p => 'param:' + p), ...metricNames.map(m => 'metric:' + m)];
  }}
  function filteredRecords() {{
    const outcomes = checked(els.outcomes);
    const statuses = checked(els.statuses);
    return records.filter(record => outcomes.has(effectiveOutcome(record)) && statuses.has(record.status || 'unknown'));
  }}
  function rebuildOutcomeFilters() {{
    updatingFilters = true;
    checkboxGroup(els.outcomes, records.map(record => effectiveOutcome(record)));
    updatingFilters = false;
  }}
  function renderPostSummary() {{
    const post = payload.postOutcomeSummary;
    const original = countValues(records.map(r => r.outcome || 'unknown')).map(([k,v]) => `${{k}}: ${{v}}`).join(', ');
    const postCounts = post && post.outcomes ? Object.entries(post.outcomes).map(([k,v]) => `${{k}}: ${{v}}`).join(', ') : 'no post eval config loaded';
    const changed = records.filter(r => r.post_outcome && (r.post_outcome.test_outcome || 'unknown') !== (r.outcome || 'unknown')).length;
    els.postSummary.textContent = `Original outcomes: ${{original || 'none'}}\\nPost outcomes: ${{postCounts}}\\nChanged by post eval: ${{changed}}\\nPost mode: ${{post ? post.mode : 'none'}}\\nPost config: ${{post ? post.config_path : 'none'}}`;
  }}
  function resize() {{
    const rect = els.canvas.getBoundingClientRect();
    els.canvas.width = Math.max(820, Math.floor(rect.width)) * devicePixelRatio;
    els.canvas.height = 620 * devicePixelRatio;
    draw();
  }}
  function range(values) {{
    let min = Math.min(...values), max = Math.max(...values);
    if (min === max) {{ min -= 0.5; max += 0.5; }}
    const pad = (max - min) * 0.04;
    return [min - pad, max + pad];
  }}
  function palette(values) {{
    const keys = countValues(values).map(([value]) => value);
    const map = new Map();
    let index = 0;
    keys.forEach(key => {{
      const semantic = semanticColors[String(key).toLowerCase()];
      if (semantic) map.set(key, semantic);
      else map.set(key, paletteBase[index++ % paletteBase.length]);
    }});
    return map;
  }}
  function colorState(records, colorBy) {{
    const values = records.map(record => colorValue(record, colorBy));
    const numeric = records.map(record => numericColorValue(record, colorBy));
    const present = numeric.filter(value => value !== null);
    if ((colorBy.startsWith('param:') || colorBy.startsWith('metric:')) && present.length) {{
      let min = Math.min(...present), max = Math.max(...present);
      if (min === max) {{ min -= 0.5; max += 0.5; }}
      return {{mode:'continuous', values, numeric, min, max, map:null, colorBy}};
    }}
    return {{mode:'categorical', values, numeric, min:null, max:null, map:palette(values), colorBy}};
  }}
  function pointColor(state, index) {{
    if (state.mode === 'continuous') {{
      const value = state.numeric[index];
      return value === null ? '#9ca3af' : continuousBlue(value, state.min, state.max);
    }}
    return state.map.get(state.values[index]);
  }}
  function drawAxes(xLabel, yLabel, width, height, margin) {{
    ctx.strokeStyle = '#d7e0ea'; ctx.lineWidth = 1 * devicePixelRatio;
    ctx.beginPath();
    ctx.moveTo(margin, height - margin); ctx.lineTo(width - margin, height - margin);
    ctx.moveTo(margin, margin); ctx.lineTo(margin, height - margin);
    ctx.stroke();
    ctx.fillStyle = '#edf3f8'; ctx.font = `${{13 * devicePixelRatio}}px system-ui`;
    ctx.textAlign = 'center'; ctx.fillText(xLabel, width / 2, height - 18 * devicePixelRatio);
    ctx.save(); ctx.translate(18 * devicePixelRatio, height / 2); ctx.rotate(-Math.PI / 2); ctx.fillText(yLabel, 0, 0); ctx.restore();
  }}
  function drawLegend(map, width) {{
    let y = 22 * devicePixelRatio;
    ctx.font = `${{12 * devicePixelRatio}}px system-ui`;
    ctx.textAlign = 'left';
    for (const [label, color] of map.entries()) {{
      ctx.fillStyle = color; ctx.fillRect(width - 210 * devicePixelRatio, y, 12 * devicePixelRatio, 12 * devicePixelRatio);
      ctx.fillStyle = '#edf3f8'; ctx.fillText(label, width - 192 * devicePixelRatio, y + 11 * devicePixelRatio);
      y += 22 * devicePixelRatio;
      if (y > 230 * devicePixelRatio) break;
    }}
  }}
  function drawContinuousLegend(state, width) {{
    const x = width - 210 * devicePixelRatio;
    const y = 22 * devicePixelRatio;
    const h = 118 * devicePixelRatio;
    const w = 14 * devicePixelRatio;
    ctx.font = `${{12 * devicePixelRatio}}px system-ui`;
    ctx.textAlign = 'left';
    ctx.fillStyle = '#edf3f8';
    ctx.fillText(state.colorBy, x, y - 8 * devicePixelRatio);
    for (let i = 0; i < 80; i++) {{
      const t = i / 79;
      const value = state.max - t * (state.max - state.min);
      ctx.fillStyle = continuousBlue(value, state.min, state.max);
      ctx.fillRect(x, y + t * h, w, h / 80 + 1);
    }}
    ctx.fillStyle = '#edf3f8';
    ctx.fillText(String(Number(state.max.toPrecision(4))), x + 22 * devicePixelRatio, y + 8 * devicePixelRatio);
    ctx.fillText(String(Number(state.min.toPrecision(4))), x + 22 * devicePixelRatio, y + h);
  }}
  function mode() {{
    if (els.view.value !== 'auto') return els.view.value;
    return els.z.value ? '3d' : (els.y.value ? '2d' : '1d');
  }}
  function draw() {{
    const visible = filteredRecords();
    const xParam = els.x.value, yParam = els.y.value, zParam = els.z.value;
    const colorBy = els.color.value;
    const activeMode = mode();
    const colors = colorState(visible, colorBy);
    els.visible.textContent = String(visible.length);
    els.mode.textContent = activeMode.toUpperCase();
    els.classes.textContent = colors.mode === 'continuous' ? 'gradient' : String(colors.map.size);
    const w = els.canvas.width, h = els.canvas.height, margin = 70 * devicePixelRatio;
    ctx.clearRect(0,0,w,h); ctx.fillStyle = '#101820'; ctx.fillRect(0,0,w,h);
    projected = [];
    if (!visible.length) return;
    if (activeMode === '1d') draw1d(visible, xParam, colors, w, h, margin);
    else if (activeMode === '3d' && zParam) draw3d(visible, [xParam, yParam, zParam], colors, w, h);
    else draw2d(visible, xParam, yParam, colors, w, h, margin);
    if (colors.mode === 'continuous') drawContinuousLegend(colors, w);
    else drawLegend(colors.map, w);
  }}
  function draw1d(records, xParam, colors, w, h, margin) {{
    const values = records.map(r => numericValue(r, xParam));
    const points = records.map((r,i) => [r, values[i], i]).filter(p => p[1] !== null);
    if (!points.length) return;
    const [min, max] = range(points.map(p => p[1]));
    const requestedBins = Number(els.bins.value);
    const bins = Math.max(1, Math.min(500, Number.isFinite(requestedBins) ? Math.round(requestedBins) : 28));
    if (String(bins) !== els.bins.value) els.bins.value = String(bins);
    const counts = Array.from({{length: bins}}, () => []);
    for (const [, value, index] of points) {{
      const idx = Math.min(Math.floor((value - min) / (max - min) * bins), bins - 1);
      counts[idx].push(index);
    }}
    const maxCount = Math.max(...counts.map(items => items.length), 1);
    const barW = (w - 2 * margin) / bins;
    counts.forEach((items, idx) => {{
      const bh = (h - 2 * margin) * items.length / maxCount;
      const representative = items.length ? items[Math.floor(items.length / 2)] : 0;
      ctx.fillStyle = items.length ? pointColor(colors, representative) : '#2563eb';
      ctx.fillRect(margin + idx * barW, h - margin - bh, barW - 1, bh);
    }});
    drawAxes(xParam, 'count', w, h, margin);
  }}
  function draw2d(records, xParam, yParam, colors, w, h, margin) {{
    const points = records.map((r,i) => [r, numericValue(r, xParam), numericValue(r, yParam), i]).filter(p => p[1] !== null && p[2] !== null);
    if (!points.length) return;
    const [xMin, xMax] = range(points.map(p => p[1]));
    const [yMin, yMax] = range(points.map(p => p[2]));
    drawAxes(xParam, yParam, w, h, margin);
    for (const [record, x, y, index] of points) {{
      const sx = margin + (x - xMin) / (xMax - xMin) * (w - 2 * margin);
      const sy = h - margin - (y - yMin) / (yMax - yMin) * (h - 2 * margin);
      ctx.beginPath(); ctx.arc(sx, sy, 4.2 * devicePixelRatio, 0, Math.PI * 2);
      ctx.fillStyle = pointColor(colors, index); ctx.globalAlpha = 0.78; ctx.fill(); ctx.globalAlpha = 1;
      projected.push({{x:sx, y:sy, record}});
    }}
  }}
  function draw3d(records, params, colors, w, h) {{
    const points = records.map((r,i) => [r, ...params.map(p => numericValue(r,p)), i]).filter(p => p[1] !== null && p[2] !== null && p[3] !== null);
    if (!points.length) return;
    const ranges = [1,2,3].map(i => range(points.map(p => p[i])));
    function norm(v, i) {{ return (v - ranges[i][0]) / (ranges[i][1] - ranges[i][0]) * 2 - 1; }}
    function project(x,y,z) {{
      const cy=Math.cos(yaw), sy=Math.sin(yaw), cp=Math.cos(pitch), sp=Math.sin(pitch);
      let x1 = cy*x + sy*z, z1 = -sy*x + cy*z;
      let y1 = cp*y - sp*z1, z2 = sp*y + cp*z1;
      const scale = Math.min(w,h) * 0.34 * zoom / (1.7 + z2);
      return {{x: w/2 + x1*scale, y: h/2 - y1*scale, z: z2}};
    }}
    const drawn = points.map(([record,x,y,z,index]) => [record, project(norm(x,0), norm(y,1), norm(z,2)), index]).sort((a,b) => a[1].z - b[1].z);
    ctx.fillStyle = '#edf3f8'; ctx.font = `${{13 * devicePixelRatio}}px system-ui`; ctx.fillText(params.join(' / '), 20 * devicePixelRatio, 28 * devicePixelRatio);
    for (const [record, p, index] of drawn) {{
      ctx.beginPath(); ctx.arc(p.x, p.y, 4.2 * devicePixelRatio, 0, Math.PI * 2);
      ctx.fillStyle = pointColor(colors, index); ctx.globalAlpha = 0.8; ctx.fill(); ctx.globalAlpha = 1;
      projected.push({{x:p.x, y:p.y, record}});
    }}
  }}
  function selectNearest(clientX, clientY) {{
    const rect = els.canvas.getBoundingClientRect();
    const x = (clientX - rect.left) * devicePixelRatio, y = (clientY - rect.top) * devicePixelRatio;
    let best = null, bestD = Infinity;
    for (const item of projected) {{
      const d = (item.x-x)**2 + (item.y-y)**2;
      if (d < bestD) {{ bestD = d; best = item; }}
    }}
    if (best && bestD < (18 * devicePixelRatio) ** 2) els.detail.textContent = JSON.stringify(best.record, null, 2);
  }}
  function downloadCsv() {{
    const rows = filteredRecords();
    const paramSet = new Set(), metricSet = new Set();
    rows.forEach(r => {{ Object.keys(r.params).forEach(k => paramSet.add(k)); Object.keys(r.metrics).forEach(k => metricSet.add(k)); }});
    const cols = ['sample_id','status','outcome','effective_outcome','stop_condition','effective_stop_condition','stop_reason', ...[...paramSet].map(k => 'param.'+k), ...[...metricSet].map(k => 'metric.'+k)];
    const esc = v => '"' + String(v ?? '').replace(/"/g, '""') + '"';
    const lines = [cols.join(',')];
    for (const r of rows) lines.push(cols.map(c => c === 'effective_outcome' ? esc(effectiveOutcome(r)) : c === 'effective_stop_condition' ? esc(effectiveStopCondition(r)) : c.startsWith('param.') ? esc(r.params[c.slice(6)]) : c.startsWith('metric.') ? esc(r.metrics[c.slice(7)]) : esc(r[c])).join(','));
    const blob = new Blob([lines.join('\\n')], {{type:'text/csv'}});
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'filtered_samples.csv'; a.click(); URL.revokeObjectURL(a.href);
  }}
  function updateLabFieldOptions() {{
    addOptions(els.labField, els.labSource.value === 'metric' ? metricNames : paramNames);
  }}
  function compareValue(value, op, raw) {{
    const number = Number(value);
    if (!Number.isFinite(number)) return false;
    if (op === 'between' || op === 'outside') {{
      const parts = String(raw).split(',').map(v => Number(v.trim()));
      if (parts.length !== 2 || !parts.every(Number.isFinite)) return false;
      const inside = number >= parts[0] && number <= parts[1];
      return op === 'between' ? inside : !inside;
    }}
    const threshold = Number(raw);
    if (!Number.isFinite(threshold)) return false;
    if (op === 'lt') return number < threshold;
    if (op === 'le') return number <= threshold;
    if (op === 'gt') return number > threshold;
    if (op === 'ge') return number >= threshold;
    if (op === 'eq') return Math.abs(number - threshold) <= 1e-6;
    return false;
  }}
  function applyLabOutcome() {{
    const source = els.labSource.value;
    const field = els.labField.value;
    const op = els.labOp.value;
    const raw = els.labValue.value;
    const outcome = els.labOutcome.value;
    const name = els.labName.value || 'draft_condition';
    let triggered = 0;
    for (const record of records) {{
      const value = source === 'metric' ? record.metrics[field] : record.params[field];
      const matched = compareValue(value, op, raw);
      if (matched) triggered += 1;
      record.lab_outcome = matched ? {{
        test_outcome: outcome,
        stop_condition: name,
        stop_reason: `Lab condition '${{name}}' matched: ${{source}}.${{field}} ${{op}} ${{raw}}`,
        condition_code: 'triggered',
        condition_name: name,
        triggered: true,
        detail: `${{source}}.${{field}}=${{value}}`
      }} : {{
        test_outcome: record.post_outcome?.test_outcome || record.outcome || 'unknown',
        stop_condition: record.post_outcome?.stop_condition || record.stop_condition || '',
        stop_reason: record.post_outcome?.stop_reason || record.stop_reason || '',
        condition_code: 'not_triggered',
        condition_name: name,
        triggered: false,
        detail: `${{source}}.${{field}}=${{value ?? 'missing'}}`
      }};
    }}
    els.outcomeSource.value = 'lab';
    els.labSummary.textContent = `Applied ${{name}}\\nRule: ${{source}}.${{field}} ${{op}} ${{raw}} -> ${{outcome}}\\nTriggered records: ${{triggered}} / ${{records.length}}`;
    rebuildOutcomeFilters();
    draw();
  }}
  addOptions(els.x, paramNames); addOptions(els.y, paramNames, true); addOptions(els.z, paramNames, true); addOptions(els.color, colorOptions());
  updateLabFieldOptions();
  els.x.value = selected[0] || paramNames[0] || '';
  els.y.value = selected[1] || paramNames[1] || '';
  els.z.value = selected[2] || '';
  els.color.value = payload.defaultColorBy || 'outcome';
  els.bins.value = String(payload.defaultBins || 28);
  renderPostSummary();
  rebuildOutcomeFilters();
  checkboxGroup(els.statuses, records.map(r => r.status || 'unknown'));
  [els.x,els.y,els.z,els.color,els.view,els.bins].forEach(el => el.addEventListener('change', draw));
  els.outcomeSource.addEventListener('change', () => {{ rebuildOutcomeFilters(); draw(); }});
  els.labSource.addEventListener('change', updateLabFieldOptions);
  els.labApply.addEventListener('click', applyLabOutcome);
  els.bins.addEventListener('input', () => {{ if (mode() === '1d') draw(); }});
  els.download.addEventListener('click', downloadCsv);
  els.canvas.addEventListener('mousedown', e => {{ dragging = true; lastX = e.clientX; lastY = e.clientY; selectNearest(e.clientX, e.clientY); }});
  window.addEventListener('mouseup', () => dragging = false);
  window.addEventListener('mousemove', e => {{ if (!dragging || mode() !== '3d') return; yaw += (e.clientX-lastX)*0.01; pitch += (e.clientY-lastY)*0.01; lastX=e.clientX; lastY=e.clientY; draw(); }});
  els.canvas.addEventListener('wheel', e => {{ if (mode() !== '3d') return; e.preventDefault(); zoom *= Math.exp(-e.deltaY*0.001); draw(); }});
  els.canvas.addEventListener('click', e => selectNearest(e.clientX, e.clientY));
  window.addEventListener('resize', resize);
  resize();
}})();
      </script>
    </section>
"""


def _color_value(record: SampleRecord, color_by: str) -> str:
    if color_by in {"", "none"}:
        return "sample"
    if color_by == "outcome":
        return record.outcome or "unknown"
    if color_by == "status":
        return record.status or "unknown"
    if color_by == "stop_condition":
        return record.stop_condition or "unknown"
    if color_by.startswith("param:"):
        return str(record.params.get(color_by.removeprefix("param:"), "missing"))
    if color_by.startswith("metric:"):
        return str(record.metrics.get(color_by.removeprefix("metric:"), "missing"))
    raise AnalyzeError(
        "color-by must be one of none, outcome, status, stop_condition, param:<name>, metric:<name>"
    )


def _build_color_spec(records: list[SampleRecord], color_by: str) -> ColorSpec:
    values = [_color_value(record, color_by) for record in records]
    numeric_values = [_numeric_color_value(record, color_by) for record in records]
    present = [value for value in numeric_values if value is not None]
    if present and _continuous_color_key(color_by):
        lower, upper = min(present), max(present)
        if math.isclose(lower, upper):
            lower -= 0.5
            upper += 0.5
        return ColorSpec(
            color_by=color_by,
            mode="continuous",
            values=values,
            palette={},
            numeric_values=numeric_values,
            numeric_min=lower,
            numeric_max=upper,
        )
    return ColorSpec(
        color_by=color_by,
        mode="categorical",
        values=values,
        palette=_build_palette(values),
        numeric_values=numeric_values,
    )


def _continuous_color_key(color_by: str) -> bool:
    return color_by.startswith("metric:") or color_by.startswith("param:")


def _numeric_color_value(record: SampleRecord, color_by: str) -> float | None:
    if color_by.startswith("param:"):
        return _as_float(record.params.get(color_by.removeprefix("param:")))
    if color_by.startswith("metric:"):
        return _as_float(record.metrics.get(color_by.removeprefix("metric:")))
    return None


def _color_for_index(color_spec: ColorSpec, index: int) -> str:
    if color_spec.mode == "continuous":
        value = color_spec.numeric_values[index]
        if value is None:
            return "#9ca3af"
        assert color_spec.numeric_min is not None
        assert color_spec.numeric_max is not None
        return _continuous_blue(value, color_spec.numeric_min, color_spec.numeric_max)
    return color_spec.palette[color_spec.values[index]]


def _build_palette(values: list[str]) -> dict[str, str]:
    ordered = [value for value, _ in Counter(values).most_common()]
    palette: dict[str, str] = {}
    fallback_index = 0
    for value in ordered:
        semantic = OUTCOME_COLORS.get(value.lower())
        if semantic is not None:
            palette[value] = semantic
        else:
            palette[value] = DEFAULT_PALETTE[fallback_index % len(DEFAULT_PALETTE)]
            fallback_index += 1
    return palette


def _color_overview_svg(color_spec: ColorSpec) -> str:
    if color_spec.mode == "continuous":
        return _continuous_color_overview_svg(color_spec)
    return _class_counts_svg(color_spec.values, color_spec.palette)


def _continuous_color_overview_svg(color_spec: ColorSpec) -> str:
    width, height = 980, 220
    if color_spec.numeric_min is None or color_spec.numeric_max is None:
        return _empty_svg("No numeric color values", width, height)
    parts = [_svg_header(width, height), _svg_title(f"Color scale: {color_spec.color_by}", width)]
    x0, y0, w, h = 180, 92, 620, 34
    steps = 80
    for index in range(steps):
        fraction = index / (steps - 1)
        value = color_spec.numeric_min + fraction * (color_spec.numeric_max - color_spec.numeric_min)
        x = x0 + index * w / steps
        parts.append(
            f'<rect x="{x:.1f}" y="{y0}" width="{w / steps + 1:.1f}" height="{h}" fill="{_continuous_blue(value, color_spec.numeric_min, color_spec.numeric_max)}"/>'
        )
    parts.append(_svg_text(x0, y0 + h + 24, f"{color_spec.numeric_min:.3g}", size=12))
    parts.append(_svg_text(x0 + w, y0 + h + 24, f"{color_spec.numeric_max:.3g}", size=12, anchor="end"))
    parts.append(_svg_text(width / 2, y0 + h + 50, "low to high continuous values", size=12, anchor="middle"))
    parts.append("</svg>")
    return "\n".join(parts)


def _class_counts_svg(values: list[str], palette: dict[str, str]) -> str:
    counts = Counter(values)
    labels = [value for value, _ in counts.most_common()]
    width, height = 980, 360
    margin = 70
    max_count = max(counts.values()) if counts else 1
    bar_w = max(20, (width - 2 * margin) / max(len(labels), 1) * 0.68)
    parts = [_svg_header(width, height), _svg_title("Class counts", width)]
    for index, label in enumerate(labels):
        count = counts[label]
        x = margin + index * ((width - 2 * margin) / max(len(labels), 1))
        bar_h = (height - 140) * count / max_count
        y = height - margin - bar_h
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" fill="{palette[label]}"/>'
        )
        parts.append(_svg_text(x + bar_w / 2, height - 42, label, size=12, anchor="middle", rotate=-25))
        parts.append(_svg_text(x + bar_w / 2, y - 8, str(count), size=12, anchor="middle"))
    parts.append("</svg>")
    return "\n".join(parts)


def _histogram_svg(
    param: str,
    values: list[Any],
    color_spec: ColorSpec,
    *,
    bins: int,
) -> str:
    numeric_values = [_as_float(value) for value in values]
    if any(value is not None for value in numeric_values):
        if color_spec.mode == "continuous":
            return _numeric_histogram_svg_plain(param, numeric_values, bins=bins)
        return _numeric_histogram_svg(param, numeric_values, color_spec.values, color_spec.palette, bins=bins)
    return _categorical_histogram_svg(param, values)


def _numeric_histogram_svg_plain(param: str, values: list[float | None], *, bins: int) -> str:
    present = [value for value in values if value is not None]
    width, height = 980, 420
    margin = 70
    if not present:
        return _empty_svg(f"No numeric values for {param}", width, height)
    lower, upper = min(present), max(present)
    if lower == upper:
        lower -= 0.5
        upper += 0.5
    bin_count = bins
    bins = [0 for _ in range(bin_count)]
    for value in present:
        index = min(int((value - lower) / (upper - lower) * bin_count), bin_count - 1)
        bins[index] += 1
    max_count = max(bins) or 1
    parts = [_svg_header(width, height), _svg_title(f"Distribution: {param}", width)]
    plot_h = height - 2 * margin
    plot_w = width - 2 * margin
    bar_w = plot_w / bin_count
    for index, count in enumerate(bins):
        x = margin + index * bar_w
        h = plot_h * count / max_count
        y = height - margin - h
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w - 1:.1f}" height="{h:.1f}" fill="#2563eb"/>'
        )
    parts.extend(_axis(width, height, margin, f"{lower:.3g}", f"{upper:.3g}"))
    parts.append("</svg>")
    return "\n".join(parts)


def _numeric_histogram_svg(
    param: str,
    values: list[float | None],
    color_values: list[str],
    palette: dict[str, str],
    *,
    bins: int,
) -> str:
    present = [value for value in values if value is not None]
    width, height = 980, 420
    margin = 70
    if not present:
        return _empty_svg(f"No numeric values for {param}", width, height)
    lower, upper = min(present), max(present)
    if lower == upper:
        lower -= 0.5
        upper += 0.5
    bin_count = bins
    bins = [0 for _ in range(bin_count)]
    class_bins: dict[str, list[int]] = {key: [0 for _ in range(bin_count)] for key in palette}
    for value, color_value in zip(values, color_values, strict=True):
        if value is None:
            continue
        index = min(int((value - lower) / (upper - lower) * bin_count), bin_count - 1)
        bins[index] += 1
        class_bins[color_value][index] += 1
    max_count = max(bins) or 1
    parts = [_svg_header(width, height), _svg_title(f"Distribution: {param}", width)]
    plot_h = height - 2 * margin
    plot_w = width - 2 * margin
    bar_w = plot_w / bin_count
    for index in range(bin_count):
        x = margin + index * bar_w
        y_base = height - margin
        stacked = 0
        for label, counts in class_bins.items():
            count = counts[index]
            if count == 0:
                continue
            h = plot_h * count / max_count
            y = y_base - stacked - h
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w - 1:.1f}" height="{h:.1f}" fill="{palette[label]}"/>'
            )
            stacked += h
    parts.extend(_axis(width, height, margin, f"{lower:.3g}", f"{upper:.3g}"))
    parts.append("</svg>")
    return "\n".join(parts)


def _categorical_histogram_svg(param: str, values: list[Any]) -> str:
    counts = Counter(str(value) for value in values if value not in {None, ""})
    palette = _build_palette(list(counts))
    return _class_counts_svg([str(value) for value in values], palette).replace("Class counts", f"Distribution: {param}", 1)


def _scatter_2d_svg(
    records: list[SampleRecord],
    x_param: str,
    y_param: str,
    color_spec: ColorSpec,
) -> str:
    xs = [_as_float(record.params.get(x_param)) for record in records]
    ys = [_as_float(record.params.get(y_param)) for record in records]
    points = [
        (index, x, y)
        for index, (x, y) in enumerate(zip(xs, ys, strict=True))
        if x is not None and y is not None
    ]
    width, height, margin = 980, 700, 80
    if not points:
        return _empty_svg(f"No numeric 2D points for {x_param} / {y_param}", width, height)
    x_min, x_max = _range([point[1] for point in points])
    y_min, y_max = _range([point[2] for point in points])
    parts = [_svg_header(width, height), _svg_title(f"2D scatter: {x_param} vs {y_param}", width)]
    parts.extend(_axis(width, height, margin, f"{x_min:.3g}", f"{x_max:.3g}", y_label=y_param, x_label=x_param))
    for index, x, y in points:
        sx = margin + (x - x_min) / (x_max - x_min) * (width - 2 * margin)
        sy = height - margin - (y - y_min) / (y_max - y_min) * (height - 2 * margin)
        parts.append(f'<circle cx="{sx:.2f}" cy="{sy:.2f}" r="4" fill="{_color_for_index(color_spec, index)}" fill-opacity="0.78"/>')
    parts.append(_color_legend(color_spec, width - 220, 70))
    parts.append("</svg>")
    return "\n".join(parts)


def _coverage_heatmap_svg(records: list[SampleRecord], x_param: str, y_param: str) -> str:
    xs = [_as_float(record.params.get(x_param)) for record in records]
    ys = [_as_float(record.params.get(y_param)) for record in records]
    points = [(x, y) for x, y in zip(xs, ys, strict=True) if x is not None and y is not None]
    width, height, margin = 760, 760, 80
    if not points:
        return _empty_svg(f"No coverage points for {x_param} / {y_param}", width, height)
    x_min, x_max = _range([point[0] for point in points])
    y_min, y_max = _range([point[1] for point in points])
    bins = 12
    grid = [[0 for _ in range(bins)] for _ in range(bins)]
    for x, y in points:
        xi = min(int((x - x_min) / (x_max - x_min) * bins), bins - 1)
        yi = min(int((y - y_min) / (y_max - y_min) * bins), bins - 1)
        grid[yi][xi] += 1
    max_count = max(max(row) for row in grid) or 1
    cell_w = (width - 2 * margin) / bins
    cell_h = (height - 2 * margin) / bins
    parts = [_svg_header(width, height), _svg_title(f"Coverage heatmap: {x_param} / {y_param}", width)]
    for yi, row in enumerate(grid):
        for xi, count in enumerate(row):
            intensity = count / max_count
            color = _blue_scale(intensity)
            x = margin + xi * cell_w
            y = height - margin - (yi + 1) * cell_h
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell_w:.1f}" height="{cell_h:.1f}" fill="{color}" stroke="#ffffff" stroke-width="1"/>'
            )
    parts.extend(_axis(width, height, margin, f"{x_min:.3g}", f"{x_max:.3g}", y_label=y_param, x_label=x_param))
    parts.append("</svg>")
    return "\n".join(parts)


def _pair_matrix_svg(
    records: list[SampleRecord],
    params: list[str],
    color_spec: ColorSpec,
) -> str:
    size = 260
    margin = 55
    n = len(params)
    width = margin + n * size
    height = margin + n * size
    parts = [_svg_header(width, height), _svg_title("Pair matrix", width, y=26)]
    values = {param: [_as_float(record.params.get(param)) for record in records] for param in params}
    ranges = {param: _range([value for value in vals if value is not None]) for param, vals in values.items()}
    for row, y_param in enumerate(params):
        for col, x_param in enumerate(params):
            x0 = margin + col * size
            y0 = margin + row * size
            parts.append(f'<rect x="{x0}" y="{y0}" width="{size}" height="{size}" fill="#ffffff" stroke="#d8e0e8"/>')
            if row == col:
                parts.append(_svg_text(x0 + size / 2, y0 + size / 2, x_param, size=13, anchor="middle"))
                continue
            x_min, x_max = ranges[x_param]
            y_min, y_max = ranges[y_param]
            for index, record in enumerate(records):
                x = _as_float(record.params.get(x_param))
                y = _as_float(record.params.get(y_param))
                if x is None or y is None:
                    continue
                sx = x0 + 18 + (x - x_min) / (x_max - x_min) * (size - 36)
                sy = y0 + size - 18 - (y - y_min) / (y_max - y_min) * (size - 36)
                parts.append(f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="2.4" fill="{_color_for_index(color_spec, index)}" fill-opacity="0.7"/>')
    parts.append("</svg>")
    return "\n".join(parts)


def _scatter_3d_html(
    records: list[SampleRecord],
    params: list[str],
    color_spec: ColorSpec,
) -> str:
    points = []
    for index, record in enumerate(records):
        coords = [_as_float(record.params.get(param)) for param in params]
        if any(coord is None for coord in coords):
            continue
        points.append(
            {
                "id": record.sample_id,
                "x": coords[0],
                "y": coords[1],
                "z": coords[2],
                "color": _color_for_index(color_spec, index),
                "label": color_spec.values[index],
            }
        )
    data = json.dumps({"params": params, "points": points})
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>3D Sample Scatter</title>
  <style>
    body {{ margin: 0; font-family: system-ui, sans-serif; background: #101820; color: #edf3f8; }}
    #toolbar {{ padding: 10px 14px; background: #162437; }}
    canvas {{ display: block; width: 100vw; height: calc(100vh - 52px); }}
  </style>
</head>
<body>
  <div id="toolbar">Drag to rotate. Wheel to zoom. Points: {len(points)}. Axes: {html.escape(', '.join(params))}</div>
  <canvas id="plot"></canvas>
  <script>
const DATA = {data};
const canvas = document.getElementById('plot');
const ctx = canvas.getContext('2d');
let yaw = -0.65, pitch = 0.55, zoom = 1.0, dragging = false, lastX = 0, lastY = 0;
function resize() {{ canvas.width = canvas.clientWidth * devicePixelRatio; canvas.height = canvas.clientHeight * devicePixelRatio; draw(); }}
window.addEventListener('resize', resize);
canvas.addEventListener('mousedown', e => {{ dragging = true; lastX = e.clientX; lastY = e.clientY; }});
window.addEventListener('mouseup', () => dragging = false);
window.addEventListener('mousemove', e => {{ if (!dragging) return; yaw += (e.clientX-lastX)*0.01; pitch += (e.clientY-lastY)*0.01; lastX=e.clientX; lastY=e.clientY; draw(); }});
canvas.addEventListener('wheel', e => {{ e.preventDefault(); zoom *= Math.exp(-e.deltaY * 0.001); draw(); }});
const pts = DATA.points;
const ranges = ['x','y','z'].map(k => [Math.min(...pts.map(p => p[k])), Math.max(...pts.map(p => p[k]))]);
function norm(p,k,i) {{ const r = ranges[i]; return (p[k] - r[0]) / (r[1] - r[0] || 1) * 2 - 1; }}
function project(p) {{
  let x = norm(p,'x',0), y = norm(p,'y',1), z = norm(p,'z',2);
  const cy=Math.cos(yaw), sy=Math.sin(yaw), cp=Math.cos(pitch), sp=Math.sin(pitch);
  let x1 = cy*x + sy*z, z1 = -sy*x + cy*z;
  let y1 = cp*y - sp*z1, z2 = sp*y + cp*z1;
  const scale = Math.min(canvas.width, canvas.height) * 0.34 * zoom / (1.7 + z2);
  return {{ x: canvas.width/2 + x1*scale, y: canvas.height/2 - y1*scale, z: z2 }};
}}
function draw() {{
  ctx.clearRect(0,0,canvas.width,canvas.height);
  ctx.fillStyle = '#101820'; ctx.fillRect(0,0,canvas.width,canvas.height);
  const drawn = pts.map(p => [p, project(p)]).sort((a,b) => a[1].z - b[1].z);
  for (const [p, q] of drawn) {{
    ctx.beginPath(); ctx.arc(q.x, q.y, 4.2 * devicePixelRatio, 0, Math.PI*2);
    ctx.fillStyle = p.color; ctx.globalAlpha = 0.8; ctx.fill();
  }}
  ctx.globalAlpha = 1;
}}
resize();
  </script>
</body>
</html>
"""


def _svg_header(width: int, height: int) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'


def _svg_title(title: str, width: int, y: int = 32) -> str:
    return _svg_text(width / 2, y, title, size=18, anchor="middle", weight="700")


def _svg_text(
    x: float,
    y: float,
    text: str,
    *,
    size: int = 12,
    anchor: str = "start",
    rotate: float | None = None,
    weight: str = "400",
) -> str:
    transform = f' transform="rotate({rotate:.1f} {x:.1f} {y:.1f})"' if rotate is not None else ""
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="Inter, system-ui, sans-serif" '
        f'font-size="{size}" font-weight="{weight}" text-anchor="{anchor}" fill="#17202a"{transform}>'
        f"{html.escape(str(text))}</text>"
    )


def _axis(
    width: int,
    height: int,
    margin: int,
    x_min: str,
    x_max: str,
    *,
    x_label: str = "",
    y_label: str = "",
) -> list[str]:
    return [
        f'<line x1="{margin}" y1="{height - margin}" x2="{width - margin}" y2="{height - margin}" stroke="#263442"/>',
        f'<line x1="{margin}" y1="{margin}" x2="{margin}" y2="{height - margin}" stroke="#263442"/>',
        _svg_text(margin, height - margin + 24, x_min, size=11),
        _svg_text(width - margin, height - margin + 24, x_max, size=11, anchor="end"),
        _svg_text(width / 2, height - 16, x_label, size=13, anchor="middle", weight="600") if x_label else "",
        _svg_text(18, height / 2, y_label, size=13, anchor="middle", rotate=-90, weight="600") if y_label else "",
    ]


def _legend(palette: dict[str, str], x: float, y: float) -> str:
    parts = [f'<g transform="translate({x:.1f},{y:.1f})">']
    for index, (label, color) in enumerate(palette.items()):
        yy = index * 22
        parts.append(f'<rect x="0" y="{yy}" width="12" height="12" fill="{color}"/>')
        parts.append(_svg_text(18, yy + 11, label, size=11))
    parts.append("</g>")
    return "\n".join(parts)


def _color_legend(color_spec: ColorSpec, x: float, y: float) -> str:
    if color_spec.mode == "categorical":
        return _legend(color_spec.palette, x, y)
    if color_spec.numeric_min is None or color_spec.numeric_max is None:
        return ""
    parts = [f'<g transform="translate({x:.1f},{y:.1f})">']
    parts.append(_svg_text(0, -10, color_spec.color_by, size=11, weight="600"))
    steps = 48
    for index in range(steps):
        fraction = index / (steps - 1)
        value = color_spec.numeric_min + fraction * (color_spec.numeric_max - color_spec.numeric_min)
        yy = index * 2.2
        parts.append(
            f'<rect x="0" y="{yy:.1f}" width="14" height="2.5" fill="{_continuous_blue(value, color_spec.numeric_min, color_spec.numeric_max)}"/>'
        )
    parts.append(_svg_text(20, 4, f"{color_spec.numeric_max:.3g}", size=11))
    parts.append(_svg_text(20, 104, f"{color_spec.numeric_min:.3g}", size=11))
    parts.append("</g>")
    return "\n".join(parts)


def _empty_svg(message: str, width: int, height: int) -> str:
    return "\n".join(
        [
            _svg_header(width, height),
            _svg_text(width / 2, height / 2, message, anchor="middle", size=18),
            "</svg>",
        ]
    )


def _range(values: list[float]) -> tuple[float, float]:
    lower, upper = min(values), max(values)
    if lower == upper:
        return lower - 0.5, upper + 0.5
    padding = (upper - lower) * 0.04
    return lower - padding, upper + padding


def _blue_scale(value: float) -> str:
    value = max(0.0, min(1.0, value))
    r = int(239 - value * 198)
    g = int(246 - value * 111)
    b = int(255 - value * 35)
    return f"#{r:02x}{g:02x}{b:02x}"


def _continuous_blue(value: float, lower: float, upper: float) -> str:
    fraction = 0.5 if upper <= lower else (value - lower) / (upper - lower)
    return _blue_scale(fraction)


def _param_is_numeric(records: list[SampleRecord], param: str) -> bool:
    return any(_as_float(record.params.get(param)) is not None for record in records)


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value in {None, ""}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _coerce_scalar(value: str) -> Any:
    if value == "":
        return ""
    number = _as_float(value)
    return number if number is not None else value


def _parse_json_mapping(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _none_if_empty(value: str | None) -> str | None:
    return value if value not in {None, ""} else None


def _read_csv_dicts(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows: list[dict[str, str]] = []
        for row in csv.DictReader(handle, skipinitialspace=True):
            clean = {
                key.strip(): value.strip() if isinstance(value, str) else value
                for key, value in row.items()
                if key is not None
            }
            if any(value not in {"", None} for value in clean.values()):
                rows.append(clean)
        return rows


def _iteration_sort_key(path: Path) -> tuple[int, str]:
    suffix = path.name.removeprefix("iteration_")
    return (int(suffix), suffix) if suffix.isdigit() else (10**12, suffix)


def _slug(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value).strip("_") or "value"


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.5g}"
    return html.escape(str(value))
