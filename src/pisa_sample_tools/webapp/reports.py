from __future__ import annotations

import csv
import hashlib
import json
import math
import mimetypes
import os
import shutil
import sqlite3
import statistics
import tempfile
import threading
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any

import yaml

from .errors import APIError
from .paths import PathPolicy

ARTIFACT_SUFFIXES = {
    ".svg",
    ".pdf",
    ".png",
    ".csv",
    ".json",
    ".html",
    ".md",
    ".tex",
    ".mp4",
    ".webm",
    ".gif",
    ".jpg",
    ".jpeg",
    ".webp",
    ".yaml",
    ".yml",
}
CHART_SUFFIXES = {".svg", ".pdf", ".png"}
MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".mp4", ".webm", ".gif"}


def report_id(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:20]


def is_report_bundle(path: Path) -> bool:
    if not path.is_dir():
        return False
    manifest_path = path / "manifest.yaml"
    report_dir = path / "report"
    if not manifest_path.is_file() or not report_dir.is_dir():
        return False
    if not ((report_dir / "analysis_report.html").is_file() or (report_dir / "index.sqlite").is_file()):
        return False
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    return manifest.get("tool") == "pisa-analysis-tools"


def ensure_report_index(root: Path) -> Path:
    """Create the compact v1 run index for an existing evidence bundle."""

    report_dir = root / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    target = report_dir / "index.sqlite"
    data_path = report_dir / "runs.json"
    if data_path.is_file():
        rows = json.loads(data_path.read_text(encoding="utf-8"))
    else:
        payload = _load_legacy_data(root) or {}
        rows = payload.get("runs", [])
    if not isinstance(rows, list):
        raise ValueError("report runs must be a list")
    with tempfile.NamedTemporaryFile(
        dir=report_dir, delete=False, prefix=".index.sqlite."
    ) as handle:
        temporary = Path(handle.name)
    connection = sqlite3.connect(temporary)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode = DELETE;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                experiment_id TEXT,
                dataset_id TEXT,
                scenario_id TEXT,
                sample_id TEXT,
                outcome TEXT,
                status TEXT,
                stop_reason TEXT,
                params_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX runs_outcome_idx ON runs(outcome);
            CREATE INDEX runs_experiment_idx ON runs(experiment_id);
            CREATE INDEX runs_sample_idx ON runs(sample_id);
            """
        )
        connection.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            [
                ("schema_version", "1"),
                ("run_count", str(len(rows))),
                ("source", json.dumps("legacy_report_payload")),
            ],
        )
        inserts = []
        identifiers: set[str] = set()
        for index, value in enumerate(rows):
            if not isinstance(value, dict):
                continue
            experiment = value.get("experiment_id") or value.get("dataset_id")
            scenario = value.get("scenario_id") or value.get("run_id") or str(index + 1)
            identifier = value.get("run_id") or f"{experiment or 'run'}:{scenario}"
            if str(identifier) in identifiers:
                identifier = f"{identifier}:{index + 1}"
            identifiers.add(str(identifier))
            inserts.append(
                (
                    str(identifier),
                    _optional_text(value.get("experiment_id")),
                    _optional_text(value.get("dataset_id")),
                    _optional_text(value.get("scenario_id")),
                    _optional_text(value.get("sample_id")),
                    _optional_text(value.get("normalized_outcome") or value.get("outcome")),
                    _optional_text(value.get("status")),
                    _optional_text(value.get("stop_reason") or value.get("termination_reason")),
                    json.dumps(value.get("params") or {}, sort_keys=True),
                    json.dumps(value.get("metrics") or {}, sort_keys=True),
                    json.dumps(value, sort_keys=True),
                )
            )
        connection.executemany(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", inserts
        )
        connection.commit()
    finally:
        connection.close()
    temporary.replace(target)
    return target


class ReportLibrary:
    def __init__(self, roots: list[Path], policy: PathPolicy) -> None:
        self.roots = tuple(Path(root).expanduser().resolve() for root in roots)
        self.policy = policy
        self._reports: dict[str, Path] = {}
        self._preview_cache: dict[Path, tuple[tuple[tuple[int, int], ...], dict[str, Any]]] = {}
        self._cross_comparison_cache: dict[
            Path, tuple[tuple[int, int], dict[str, Any]]
        ] = {}
        self._lock = threading.RLock()

    def scan(
        self, root: str | Path | None = None, *, recursive: bool = True
    ) -> dict[str, Any]:
        roots = [
            self.policy.resolve(root, field="root", kind="directory")
        ] if root is not None else list(self.roots)
        reports: list[dict[str, Any]] = []
        warnings: list[str] = []
        seen: set[Path] = set()
        for scan_root in roots:
            if not scan_root.is_dir():
                warnings.append(f"report root does not exist: {scan_root}")
                continue
            if is_report_bundle(scan_root):
                candidates = [scan_root]
            elif not recursive:
                candidates = [
                    item
                    for item in sorted(scan_root.iterdir(), key=lambda value: value.name.casefold())
                    if item.is_dir() and not item.name.startswith(".") and is_report_bundle(item)
                ]
            else:
                candidates = []
                for current, directories, _files in os.walk(scan_root, followlinks=False):
                    current_path = Path(current)
                    directories[:] = [name for name in directories if not name.startswith(".")]
                    if is_report_bundle(current_path):
                        candidates.append(current_path)
                        directories[:] = []
            for candidate in candidates:
                candidate = candidate.resolve()
                if candidate in seen:
                    continue
                seen.add(candidate)
                try:
                    preview = self.preview(candidate)
                except (OSError, ValueError, yaml.YAMLError, json.JSONDecodeError) as exc:
                    warnings.append(f"{candidate}: {exc}")
                    continue
                reports.append(preview)
                with self._lock:
                    self._reports[preview["id"]] = candidate
        reports.sort(key=lambda item: str(item.get("generated_at") or ""), reverse=True)
        return {
            "roots": [str(item) for item in roots],
            "reports": reports,
            "warnings": warnings,
        }

    def browse(self, value: str | Path | None = None) -> dict[str, Any]:
        directory = self.policy.resolve(
            value or self.roots[0], field="path", kind="directory"
        )
        entries: list[dict[str, Any]] = []
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise APIError(400, "directory_unreadable", str(exc), field="path") from exc
        for child in children[:1000]:
            if child.name.startswith(".") or child.is_symlink():
                continue
            if child.is_dir():
                child_is_report = is_report_bundle(child)
                entries.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "kind": "report" if child_is_report else "directory",
                        "is_report": child_is_report,
                        "looks_like_output": _looks_like_results(child),
                    }
                )
            elif child.suffix.casefold() in {".yaml", ".yml", ".json"}:
                entries.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "kind": _configuration_kind(child),
                        "is_report": False,
                        "looks_like_output": False,
                    }
                )
        current = self.preview(directory) if is_report_bundle(directory) else None
        parent = directory.parent
        parent_value = (
            str(parent)
            if parent != directory
            and any(parent == root or parent.is_relative_to(root) for root in self.policy.roots)
            else None
        )
        return {
            "path": str(directory),
            "parent": parent_value,
            "roots": [str(root) for root in self.policy.roots],
            "current_report": current,
            "looks_like_output": _looks_like_results(directory),
            "entries": entries,
            "truncated": len(children) > 1000,
        }

    def create_directory(self, parent: str | Path, name: str) -> dict[str, Any]:
        directory = self.policy.resolve(parent, field="parent", kind="directory")
        clean_name = name.strip()
        if (
            not clean_name
            or clean_name in {".", ".."}
            or clean_name.startswith(".")
            or Path(clean_name).name != clean_name
            or "/" in clean_name
            or "\\" in clean_name
        ):
            raise APIError(
                400,
                "invalid_directory_name",
                "name must be one visible directory name without path separators",
                field="name",
            )
        target = self.policy.resolve(
            directory / clean_name,
            field="name",
            must_exist=False,
            kind="directory",
        )
        try:
            target.mkdir()
        except FileExistsError as exc:
            raise APIError(
                409,
                "directory_exists",
                "a file or directory with this name already exists",
                field="name",
            ) from exc
        except OSError as exc:
            raise APIError(
                400,
                "directory_create_failed",
                str(exc),
                field="name",
            ) from exc
        return self.browse(target)

    def inspect_source(self, value: str | Path) -> dict[str, Any]:
        source = self.policy.resolve(value, field="path", kind="directory")
        try:
            from pisa_sample_tools.reporting import discover_experiments

            discovered = discover_experiments(source)
        except (ImportError, OSError, ValueError) as exc:
            raise APIError(400, "report_source_invalid", str(exc), field="path") from exc
        datasets: list[dict[str, Any]] = []
        for item in discovered:
            manifest = (
                _read_optional_mapping(item.manifest_path)
                if item.manifest_path is not None
                else {}
            )
            components = manifest.get("components") if isinstance(manifest.get("components"), dict) else {}
            execution = manifest.get("execution") if isinstance(manifest.get("execution"), dict) else {}
            resolved = manifest.get("resolved_inputs") if isinstance(manifest.get("resolved_inputs"), dict) else {}
            metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
            datasets.append(
                {
                    "id": item.dataset_id,
                    "path": str(item.root),
                    "manifest_path": str(item.manifest_path) if item.manifest_path else None,
                    "scenario": manifest.get("scenario_name"),
                    "map": metadata.get("map_name"),
                    "simulator": _nested_component_name(components.get("simulator")),
                    "av": _nested_component_name(components.get("av")),
                    "sampler": execution.get("sampler_name"),
                    "completed_runs": len(item.result_paths),
                    "missing_runs": len(item.missing_result_dirs),
                    "expected_runs": (
                        manifest.get("summary", {}).get("finished")
                        if isinstance(manifest.get("summary"), dict)
                        else None
                    ),
                    "resolved_inputs": resolved,
                    "execution": execution,
                    "components": components,
                }
            )
        suggestion = Path.cwd().resolve() / "analysis" / f"{source.name}-report"
        suffix = 2
        while suggestion.exists():
            suggestion = Path.cwd().resolve() / "analysis" / f"{source.name}-report-{suffix}"
            suffix += 1
        return {
            "path": str(source),
            "valid": bool(discovered and any(item.result_paths for item in discovered)),
            "dataset_count": len(datasets),
            "run_count": sum(item["completed_runs"] for item in datasets),
            "missing_run_count": sum(item["missing_runs"] for item in datasets),
            "datasets": datasets,
            "suggested_output_dir": str(suggestion),
            "warnings": [
                f"{item['id']}: execution manifest missing"
                for item in datasets
                if not item["manifest_path"]
            ],
        }

    def preview(self, path: Path) -> dict[str, Any]:
        path = path.expanduser().resolve()
        watched = (path / "manifest.yaml", path / "summary" / "summary.json", path / "report" / "index.sqlite")
        signature = tuple(
            (item.stat().st_mtime_ns, item.stat().st_size) if item.is_file() else (0, 0)
            for item in watched
        )
        with self._lock:
            cached = self._preview_cache.get(path)
            if cached is not None and cached[0] == signature:
                preview = dict(cached[1])
                self._reports[preview["id"]] = path
                return preview
        manifest = _load_mapping(path / "manifest.yaml", label="report manifest")
        data = _load_legacy_data(path, maximum_bytes=16 * 1024 * 1024) or {}
        normalized = _load_normalized_summary(path) or {}
        summary = (
            normalized.get("summary")
            if isinstance(normalized.get("summary"), dict)
            else data.get("summary") if isinstance(data.get("summary"), dict) else {}
        )
        experiments = data.get("experiments") if isinstance(data.get("experiments"), list) else []
        findings = normalized.get("findings") if isinstance(normalized.get("findings"), list) else []
        build_version = int(manifest.get("report_build_version") or 0)
        try:
            from pisa_sample_tools.evidence.report_version import REPORT_BUILD_VERSION
        except ImportError:
            REPORT_BUILD_VERSION = build_version
        health = [_frontend_finding(item, index) for index, item in enumerate(findings)]
        datasets = normalized.get("datasets") if isinstance(normalized.get("datasets"), list) else []
        tags = sorted(
            {
                str(value)
                for item in datasets
                if isinstance(item, dict)
                for value in (item.get("simulator"), item.get("av"), item.get("sampler"))
                if value
            }
        )
        scenario_names = sorted({str(item.get("scenario_name") or item.get("scenario")) for item in datasets if isinstance(item, dict) and (item.get("scenario_name") or item.get("scenario"))})
        sampler_names = sorted({str(item.get("sampler")) for item in datasets if isinstance(item, dict) and item.get("sampler")})
        simulator_names = sorted({str(item.get("simulator")) for item in datasets if isinstance(item, dict) and item.get("simulator")})
        av_names = sorted({str(item.get("av")) for item in datasets if isinstance(item, dict) and item.get("av")})
        normalized_bundle = bool(normalized)
        preview = {
            "id": report_id(path),
            "name": path.name,
            "path": str(path),
            "generated_at": manifest.get("generated_at"),
            "run_count": int(
                summary.get("total")
                or normalized.get("all_browsable_runs")
                or manifest.get("run_count")
                or summary.get("run_count")
                or 0
            ),
            "warning_count": int(
                manifest.get("warning_count")
                or summary.get("warning_count")
                or sum(item.get("severity") == "warning" for item in findings if isinstance(item, dict))
            ),
            "experiment_count": int(
                normalized.get("aggregate_dataset_count")
                or normalized.get("dataset_count")
                or summary.get("experiment_count")
                or len(experiments)
            ),
            "parameter_count": int(summary.get("parameter_count") or 0),
            "report_mode": data.get("report_mode") or manifest.get("report_mode"),
            "report_build_version": build_version,
            "latest_report_build_version": REPORT_BUILD_VERSION,
            "update_available": build_version < REPORT_BUILD_VERSION,
            "has_index": (path / "report" / "index.sqlite").is_file(),
            "has_snapshot": (path / "report" / "analysis_report.html").is_file(),
            "status": "ready" if normalized_bundle and not build_version < REPORT_BUILD_VERSION else "legacy",
            "health": health,
            "tags": tags,
            "scenario_names": scenario_names,
            "sampler_names": sampler_names,
            "simulator_names": simulator_names,
            "av_names": av_names,
            **(
                {
                    "browsable_run_count": int(normalized.get("all_browsable_runs") or 0),
                    "total_dataset_count": int(normalized.get("dataset_count") or 0),
                }
                if normalized_bundle
                else {}
            ),
        }
        with self._lock:
            self._preview_cache[path] = (signature, dict(preview))
            self._reports[preview["id"]] = path
        return preview

    def preview_path(self, value: str | Path) -> dict[str, Any]:
        path = self.policy.resolve(value, field="path", kind="directory")
        if not is_report_bundle(path):
            raise APIError(400, "not_a_report", "the selected directory is not a PISA report", field="path")
        return self.preview(path)

    def get(self, identifier: str) -> Path:
        if not identifier or any(character not in "0123456789abcdef" for character in identifier):
            raise APIError(404, "report_not_found", "report was not found")
        with self._lock:
            path = self._reports.get(identifier)
        if path is None:
            self.scan()
            with self._lock:
                path = self._reports.get(identifier)
        if path is None or not is_report_bundle(path):
            raise APIError(404, "report_not_found", "report was not found")
        return path

    def overview(self, identifier: str) -> dict[str, Any]:
        root = self.get(identifier)
        manifest = _load_mapping(root / "manifest.yaml", label="report manifest")
        data = _load_legacy_data(root)
        normalized = False
        if data is None:
            summary_path = root / "summary" / "summary.json"
            if summary_path.is_file():
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                data = summary if isinstance(summary, dict) else None
                normalized = data is not None
            else:
                data = _sqlite_overview(root / "report" / "index.sqlite")
        if data is None:
            raise APIError(
                501,
                "report_overview_unavailable",
                "this report has no supported overview data",
            )
        excluded = {
            "runs",
            "concrete_cases",
            "comparison_data",
            "comparison_groups",
            "animation_data",
        }
        frontend = _frontend_overview(
            identifier,
            self.preview(root),
            data,
            normalized=normalized,
        )
        index_path = root / "report" / "index.sqlite"
        if _is_normalized_index(index_path):
            frontend["experiment_summaries"] = _normalized_experiment_summaries(index_path)
        return {
            **frontend,
            "report": self.preview(root),
            "manifest": manifest,
            "data": {key: value for key, value in data.items() if key not in excluded},
        }

    def comparisons(self, identifier: str) -> dict[str, Any]:
        root = self.get(identifier)
        data = _load_legacy_data(root)
        if data is None:
            index_path = root / "report" / "index.sqlite"
            if _is_normalized_index(index_path):
                items = _normalized_comparisons(index_path)
                fingerprint = (index_path.stat().st_mtime_ns, index_path.stat().st_size)
                with self._lock:
                    cached = self._cross_comparison_cache.get(index_path)
                if cached is not None and cached[0] == fingerprint:
                    cross_experiment = cached[1]
                else:
                    cross_experiment = _normalized_cross_experiment_summary(
                        index_path, self.policy
                    )
                    with self._lock:
                        self._cross_comparison_cache[index_path] = (
                            fingerprint,
                            cross_experiment,
                        )
                similarity_candidates = [
                    item
                    for item in items
                    if item.get("role") != "duplicate_alias"
                    and item.get("information_comparable_count", 0) > 0
                    and item.get("information_agreement_ratio") is not None
                ]
                most_similar = max(
                    similarity_candidates,
                    key=lambda item: (
                        float(item["information_agreement_ratio"]),
                        int(item["information_consistent_count"]),
                        int(item["information_comparable_count"]),
                        str(item["left"]),
                        str(item["right"]),
                    ),
                    default=None,
                )
                cross_experiment = {
                    **cross_experiment,
                    "most_similar_pair": (
                        {
                            key: most_similar[key]
                            for key in (
                                "left",
                                "right",
                                "information_consistent_count",
                                "information_comparable_count",
                                "information_agreement_ratio",
                                "information_scope",
                                "information_exclusions",
                            )
                        }
                        if most_similar is not None
                        else None
                    ),
                }
                return {
                    "items": items,
                    "available": bool(items),
                    "reason": None if items else "no_defensible_comparison",
                    "cross_experiment": cross_experiment,
                }
            return {"items": [], "available": False, "reason": "comparison_data_unavailable"}
        comparison = data.get("comparison")
        if not isinstance(comparison, dict):
            return {"items": [], "available": False, "reason": "not_a_comparison_report"}
        scenarios = comparison.get("concrete_scenarios")
        return {
            "available": True,
            "items": scenarios if isinstance(scenarios, list) else [],
            "cross_experiment": {
                "available": False,
                "reason": "normalized_run_index_required",
            },
            "summary": {
                key: value
                for key, value in comparison.items()
                if key not in {"concrete_scenarios", "parameter_points"}
            },
        }

    def case_detail(
        self, identifier: str, run_id: str, *, maximum_points: int = 5_000,
        include_map: bool = True,
    ) -> dict[str, Any]:
        root = self.get(identifier)
        aggregate_path = root / "report" / "case_data.json"
        if aggregate_path.is_file():
            aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
            for case in aggregate.get("cases", []):
                run = case.get("run") if isinstance(case, dict) else None
                if isinstance(run, dict) and str(run.get("run_id")) == run_id:
                    return {"source": "representative_case", **case}
        data_path = root / "report" / "runs.json"
        rows = json.loads(data_path.read_text(encoding="utf-8")) if data_path.is_file() else []
        run = next(
            (
                item
                for item in rows
                if isinstance(item, dict) and str(item.get("run_id")) == run_id
            ),
            None,
        )
        if run is None:
            indexed = _normalized_index_run(
                root / "report" / "index.sqlite",
                run_id,
                maximum_points=maximum_points,
                policy=self.policy,
                include_map=include_map,
            )
            if indexed is not None:
                return indexed
            raise APIError(404, "run_not_found", "report run was not found")
        traces: dict[str, list[dict[str, Any]]] = {}
        for name, value in (run.get("artifacts") or {}).items():
            if not value or not str(value).lower().endswith(".csv"):
                continue
            try:
                path = self.policy.resolve(value, field=f"artifacts.{name}", kind="file")
                traces[name] = _read_trace(path, maximum_points=maximum_points)
            except APIError:
                traces[name] = []
        return {"source": "lazy_trace", "run": run, "traces": traces}

    def runs(
        self,
        identifier: str,
        *,
        cursor: str | None,
        limit: int | None,
        outcome: str | None,
        experiment: str | None,
        query: str | None,
        sort: str | None,
        descending: bool,
    ) -> dict[str, Any]:
        root = self.get(identifier)
        index_path = root / "report" / "index.sqlite"
        if index_path.is_file():
            normalized = _normalized_index_runs(
                index_path,
                cursor=cursor,
                limit=limit,
                outcome=outcome,
                experiment=experiment,
                query=query,
                sort=sort,
                descending=descending,
            )
            if normalized is not None:
                return normalized
            offset = _decode_cursor(cursor)
            indexed = _sqlite_runs(
                index_path,
                offset=offset,
                limit=limit,
                outcome=outcome,
                experiment=experiment,
                query=query,
                sort=sort,
                descending=descending,
            )
            if indexed is not None:
                return indexed
        offset = _decode_cursor(cursor)
        data_path = root / "report" / "runs.json"
        if not data_path.is_file():
            data = _load_legacy_data(root) or {}
            rows = data.get("runs", [])
        else:
            rows = json.loads(data_path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise APIError(500, "invalid_report", "report run data must be a list")
        selected = [row for row in rows if isinstance(row, dict)]
        if outcome:
            selected = [row for row in selected if str(row.get("outcome") or "") == outcome]
        if experiment:
            selected = [
                row
                for row in selected
                if str(row.get("experiment_id") or row.get("dataset_id") or "") == experiment
            ]
        if query:
            needle = query.casefold()
            selected = [
                row
                for row in selected
                if needle
                in " ".join(
                    str(row.get(key) or "")
                    for key in ("run_id", "scenario_id")
                ).casefold()
            ]
        if sort:
            selected.sort(key=lambda row: _sort_value(row.get(sort)), reverse=descending)
        total = len(selected)
        page = [_normalize_run(row) for row in selected[offset : offset + limit]]
        next_cursor = str(offset + limit) if offset + limit < total else None
        return {
            "items": page,
            "total": total,
            "cursor": cursor,
            "next_cursor": next_cursor,
            "limit": limit,
            "source": "legacy_json",
        }

    def artifacts(self, identifier: str, *, media: bool = False) -> list[dict[str, Any]]:
        root = self.get(identifier)
        suffixes = MEDIA_SUFFIXES if media else CHART_SUFFIXES
        candidates: list[dict[str, Any]] = []
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            if media:
                if not (
                    path.suffix.lower() in {".mp4", ".webm", ".gif", ".jpg", ".jpeg", ".webp"}
                    or "media" in path.parts
                    or "representative_cases" in path.parts
                ):
                    continue
            elif not {"figures", "comparison", "comparisons", "exports"}.intersection(
                path.parts
            ):
                continue
            candidates.append(_artifact_row(path, root, identifier))
        candidates.sort(key=lambda item: item["path"])
        return candidates

    def artifact(self, identifier: str, relative_path: str) -> Path:
        root = self.get(identifier)
        return self.policy.relative_asset(root, relative_path, suffixes=ARTIFACT_SUFFIXES)

    def index_info(self, identifier: str) -> dict[str, Any]:
        root = self.get(identifier)
        index_path = root / "report" / "index.sqlite"
        if not index_path.is_file():
            return {"available": False, "reason": "index_not_available"}
        try:
            from pisa_sample_tools.reporting import ReportIndex

            with ReportIndex(index_path) as index:
                return {
                    "available": True,
                    "metadata": index.metadata(),
                    "datasets": [item.as_dict() for item in index.datasets()],
                    "findings": [item.as_dict() for item in index.findings()],
                    "outcomes": index.outcome_summary().as_dict(),
                }
        except (ImportError, OSError, ValueError, sqlite3.DatabaseError):
            overview = _sqlite_overview(index_path)
            return {"available": True, **(overview or {})}

    def details(self, identifier: str) -> dict[str, Any]:
        root = self.get(identifier)
        manifest = _load_mapping(root / "manifest.yaml", label="report manifest")
        provenance: dict[str, Any] = {}
        for name in (
            "input_manifest.yaml",
            "input_manifest.json",
            "resolved_campaign.yaml",
            "resolved_analysis_spec.yaml",
            "rebuild_lineage.json",
        ):
            path = root / "provenance" / name
            if path.is_file() and path.stat().st_size <= 8 * 1024 * 1024:
                try:
                    if path.suffix == ".json":
                        value = json.loads(path.read_text(encoding="utf-8"))
                    else:
                        value = yaml.safe_load(path.read_text(encoding="utf-8"))
                    provenance[name] = value
                except (OSError, json.JSONDecodeError, yaml.YAMLError):
                    provenance[name] = {"error": "could not parse recorded provenance"}
        experiment_manifests: list[dict[str, Any]] = []
        index_path = root / "report" / "index.sqlite"
        if _is_normalized_index(index_path):
            try:
                from pisa_sample_tools.reporting import ReportIndex

                with ReportIndex(index_path) as index:
                    descriptors = index.datasets()
                for descriptor in descriptors:
                    recorded: dict[str, Any] = {}
                    path_value: str | None = None
                    if descriptor.manifest_path is not None:
                        try:
                            manifest_path = self.policy.resolve(
                                descriptor.manifest_path,
                                field=f"datasets.{descriptor.dataset_id}.manifest_path",
                                kind="file",
                                suffixes={".yaml", ".yml", ".json"},
                            )
                            path_value = str(manifest_path)
                            recorded = _read_optional_mapping(manifest_path)
                        except APIError:
                            recorded = {"unavailable": "manifest is outside configured roots"}
                    experiment_manifests.append(
                        {
                            "dataset_id": descriptor.dataset_id,
                            "manifest_path": path_value,
                            "scenario": descriptor.scenario_name,
                            "simulator": descriptor.simulator,
                            "av": descriptor.av,
                            "sampler": descriptor.sampler,
                            "run_count": descriptor.run_count,
                            "attempt_count": descriptor.attempt_count,
                            "expected_runs": descriptor.expected_runs,
                            "completed_at": descriptor.completed_at,
                            "health_counts": descriptor.health_counts,
                            "manifest": recorded,
                        }
                    )
            except (ImportError, OSError, ValueError, sqlite3.DatabaseError):
                experiment_manifests = []
        return {
            "report": self.preview(root),
            "manifest": manifest,
            "index": self.index_info(identifier),
            "experiments": experiment_manifests,
            "provenance": provenance,
        }

    def rename(self, identifier: str, new_name: str) -> dict[str, Any]:
        source = self.get(identifier)
        clean_name = new_name.strip()
        if clean_name in {"", ".", ".."} or "/" in clean_name or "\\" in clean_name:
            raise APIError(422, "invalid_report_name", "new_name must be a directory name")
        target = source.with_name(clean_name)
        self.policy.resolve(target, field="new_name", must_exist=False, kind="directory")
        if target.exists():
            raise APIError(409, "report_name_exists", "a file or directory already uses that name")
        try:
            source.rename(target)
        except OSError as exc:
            raise APIError(409, "report_rename_failed", str(exc)) from exc
        preview = self.preview(target)
        with self._lock:
            self._reports.pop(identifier, None)
            self._reports[preview["id"]] = target
        return preview

    def delete(self, identifier: str, confirm_name: str) -> None:
        source = self.get(identifier)
        if confirm_name != source.name:
            raise APIError(
                409,
                "report_delete_confirmation_mismatch",
                "confirm_name must exactly match the report directory name",
                field="confirm_name",
            )
        if source.is_symlink() or not is_report_bundle(source):
            raise APIError(409, "report_delete_refused", "only a real PISA report bundle can be deleted")
        try:
            shutil.rmtree(source)
        except OSError as exc:
            raise APIError(409, "report_delete_failed", str(exc)) from exc
        with self._lock:
            self._reports.pop(identifier, None)

    def scatter(
        self,
        identifier: str,
        *,
        x: str | None,
        y: str | None,
        color: str | None,
        dataset: str | None,
        stop_reason: str | None,
        limit: int | None,
    ) -> dict[str, Any]:
        root = self.get(identifier)
        path = root / "report" / "index.sqlite"
        if not _is_normalized_index(path):
            raise APIError(
                409,
                "scatter_requires_current_report",
                "rebuild this legacy report to use the indexed scatter explorer",
            )
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            fields = _scatter_fields(connection)
            field_keys = {item["key"] for item in fields}
            parameters = [item["key"] for item in fields if item["source"] == "parameter"]
            metrics = [item["key"] for item in fields if item["source"] == "metric"]
            x_key = x or (parameters[0] if parameters else "sample_order")
            y_key = y or (
                parameters[1]
                if len(parameters) > 1
                else metrics[0] if metrics else "scenario_order"
            )
            color_key = color or "outcome"
            for label, key in (("x", x_key), ("y", y_key), ("color", color_key)):
                if key not in field_keys and not (label == "color" and key == "outcome"):
                    raise APIError(422, "invalid_scatter_field", f"unknown {label} field: {key}")
            dataset_ids = [
                str(row[0])
                for row in connection.execute("SELECT dataset_id FROM datasets ORDER BY dataset_id")
            ]
            if dataset and dataset not in dataset_ids:
                raise APIError(422, "invalid_scatter_dataset", "dataset is not in this report")
            stop_reasons = [str(row[0]) for row in connection.execute(
                "SELECT DISTINCT stop_reason FROM runs WHERE stop_reason IS NOT NULL AND stop_reason != '' ORDER BY stop_reason"
            )]
            stop_conditions = [str(row[0]) for row in connection.execute(
                "SELECT DISTINCT stop_condition FROM runs WHERE stop_condition IS NOT NULL AND stop_condition != '' ORDER BY stop_condition"
            )]
            if stop_reason and stop_reason not in stop_reasons:
                raise APIError(422, "invalid_scatter_stop_reason", "stop reason is not in this report")
            query = """
                SELECT run_id, dataset_id, scenario_id, sample_id, parameter_hash, outcome_class,
                       params_json, has_collision, stop_condition, stop_reason
                FROM runs
                WHERE (? IS NULL OR dataset_id = ?)
                  AND (? IS NULL OR stop_reason = ?)
                ORDER BY dataset_id,
                         CASE WHEN scenario_id GLOB '[0-9]*' THEN CAST(scenario_id AS INTEGER) END,
                         scenario_id, run_id
                """
            parameters_sql: tuple[Any, ...] = (dataset, dataset, stop_reason, stop_reason)
            if limit is not None:
                query += " LIMIT ?"
                parameters_sql += (limit,)
            rows = connection.execute(query, parameters_sql).fetchall()
            requested = {key for key in (x_key, y_key, color_key) if ":" in key}
            values = _scatter_values(connection, [str(row["run_id"]) for row in rows], requested)
            points = []
            for ordinal, row in enumerate(rows, start=1):
                run_values = values.get(str(row["run_id"]), {})
                context = {
                    **run_values,
                    "sample_order": ordinal,
                    "scenario_order": _numeric_scenario(row["scenario_id"], ordinal),
                    "outcome": str(row["outcome_class"]),
                    "collision": int(bool(row["has_collision"])),
                    "stop_condition": _optional_text(row["stop_condition"]),
                    "stop_reason": _optional_text(row["stop_reason"]),
                }
                x_value = _first_number(context.get(x_key))
                y_value = _first_number(context.get(y_key))
                if x_value is None or y_value is None:
                    continue
                points.append(
                    {
                        "run_id": str(row["run_id"]),
                        "dataset_id": str(row["dataset_id"]),
                        "scenario_id": str(row["scenario_id"]),
                        "sample_id": _optional_text(row["sample_id"]),
                        "parameter_hash": _optional_text(row["parameter_hash"]),
                        "ordinal": ordinal,
                        "outcome": str(row["outcome_class"]),
                        "collision": bool(row["has_collision"]),
                        "stop_condition": _optional_text(row["stop_condition"]),
                        "stop_reason": _optional_text(row["stop_reason"]),
                        "x": x_value,
                        "y": y_value,
                        "color": context.get(color_key),
                    }
                )
            return {
                "fields": fields,
                "datasets": dataset_ids,
                "stop_reasons": stop_reasons,
                "stop_conditions": stop_conditions,
                "selection": {"x": x_key, "y": y_key, "color": color_key, "dataset": dataset},
                "points": points,
                "returned": len(points),
                "scanned": len(rows),
                "limit": limit,
                "truncated": limit is not None and len(rows) == limit,
            }
        finally:
            connection.close()


def _looks_like_results(path: Path) -> bool:
    if any((path / name).is_file() for name in ("execution_manifest.yaml", "execution_manifest.json")):
        return True
    if (path / "monitor" / "result.csv").is_file():
        return True
    try:
        return any(
            child.is_dir()
            and child.name.startswith("iteration_")
            and (child / "monitor" / "result.csv").is_file()
            for child in path.iterdir()
        )
    except OSError:
        return False


def _configuration_kind(path: Path) -> str:
    name = path.name.casefold()
    if "campaign" in name:
        return "campaign"
    if "spec" in name or "analysis" in name:
        return "analysis_spec"
    return "configuration"


def _read_optional_mapping(path: Path) -> dict[str, Any]:
    try:
        if path.suffix.casefold() == ".json":
            value = json.loads(path.read_text(encoding="utf-8"))
        else:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, yaml.YAMLError):
        return {}
    return value if isinstance(value, dict) else {}


def _nested_component_name(value: Any) -> str | None:
    if not isinstance(value, dict):
        return _optional_text(value)
    component = value.get("component")
    wrapper = value.get("wrapper")
    for candidate in (component, wrapper, value):
        if isinstance(candidate, dict) and candidate.get("name") not in {None, ""}:
            return str(candidate["name"])
    return None


def _scatter_fields(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = [
        {"key": "sample_order", "label": "Sample order", "source": "order", "numeric_count": None},
        {"key": "scenario_order", "label": "Scenario / iteration order", "source": "order", "numeric_count": None},
        {"key": "collision", "label": "Collision (0/1)", "source": "outcome", "numeric_count": None},
        {"key": "outcome", "label": "Outcome", "source": "outcome", "numeric_count": None},
        {"key": "stop_condition", "label": "Stop condition", "source": "run", "numeric_count": None},
        {"key": "stop_reason", "label": "Stop reason", "source": "run", "numeric_count": None},
    ]
    for table, prefix, source in (
        ("parameters", "param", "parameter"),
        ("metrics", "metric", "metric"),
    ):
        for row in connection.execute(
            f"SELECT name, COUNT(value_real) AS numeric_count, COUNT(*) AS total_count "
            f"FROM {table} GROUP BY name ORDER BY name"
        ):
            if int(row[1] or 0) <= 0:
                continue
            fields.append(
                {
                    "key": f"{prefix}:{row[0]}",
                    "label": str(row[0]),
                    "source": (
                        "control"
                        if table == "metrics" and _is_control_metric(str(row[0]))
                        else source
                    ),
                    "numeric_count": int(row[1]),
                    "total_count": int(row[2]),
                }
            )
    return fields


def _is_control_metric(name: str) -> bool:
    lowered = name.casefold()
    return any(
        token in lowered
        for token in ("throttle", "accelerator", "brake", "steer", "steering")
    )


def _scatter_values(
    connection: sqlite3.Connection, run_ids: list[str], requested: set[str]
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {run_id: {} for run_id in run_ids}
    if not run_ids or not requested:
        return output
    placeholders = ",".join("?" for _ in run_ids)
    for table, prefix in (("parameters", "param"), ("metrics", "metric")):
        names = sorted(key.removeprefix(f"{prefix}:") for key in requested if key.startswith(f"{prefix}:"))
        if not names:
            continue
        name_placeholders = ",".join("?" for _ in names)
        query = (
            f"SELECT run_id, name, value_real, value_text FROM {table} "
            f"WHERE run_id IN ({placeholders}) AND name IN ({name_placeholders})"
        )
        for row in connection.execute(query, (*run_ids, *names)):
            output[str(row[0])][f"{prefix}:{row[1]}"] = row[2] if row[2] is not None else row[3]
    return output


def _numeric_scenario(value: Any, fallback: int) -> int:
    text = str(value or "")
    try:
        return int(text)
    except ValueError:
        digits = "".join(character for character in text if character.isdigit())
        return int(digits) if digits else fallback


def _load_normalized_summary(root: Path) -> dict[str, Any] | None:
    path = root / "summary" / "summary.json"
    if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def _task_specific_comparison_field(name: str) -> bool:
    normalized = name.casefold().replace("-", "_").replace(" ", "_")
    return any(
        token in normalized
        for token in (
            "job_id",
            "job.id",
            "wall_time",
            "wall_clock",
            "speedup",
            "queue_time",
            "worker_id",
            "cpu_time",
            "memory_usage",
        )
    ) or normalized in {"created_at", "completed_at", "host", "hostname"}


def _comparison_information_profiles(
    connection: sqlite3.Connection,
) -> dict[str, dict[str, dict[str, Any]]]:
    run_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(runs)")}
    run_fields = [
        field
        for field in (
            "status",
            "outcome",
            "outcome_class",
            "stop_condition",
            "stop_reason",
            "has_collision",
        )
        if field in run_columns
    ]
    rows = connection.execute(
        f"SELECT run_id, dataset_id, parameter_hash{''.join(f', {field}' for field in run_fields)} "
        "FROM runs WHERE parameter_hash IS NOT NULL AND parameter_hash != '' "
        "ORDER BY dataset_id, parameter_hash, run_id"
    ).fetchall()
    run_ids = [str(row["run_id"]) for row in rows]
    parameters: dict[str, dict[str, Any]] = {run_id: {} for run_id in run_ids}
    metrics: dict[str, dict[str, Any]] = {run_id: {} for run_id in run_ids}
    for table, destination in (("parameters", parameters), ("metrics", metrics)):
        if not run_ids:
            continue
        columns = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        }
        value_text = "value_text" if "value_text" in columns else "NULL AS value_text"
        value_real = "value_real" if "value_real" in columns else "NULL AS value_real"
        value_type = "value_type" if "value_type" in columns else "NULL AS value_type"
        for row in connection.execute(
            f"SELECT run_id, name, {value_text}, {value_real}, {value_type} FROM {table} "
            "ORDER BY run_id, name"
        ):
            run_id, name = str(row["run_id"]), str(row["name"])
            if run_id not in destination or _task_specific_comparison_field(name):
                continue
            destination[run_id][name] = (
                float(row["value_real"])
                if row["value_type"] == "number" and row["value_real"] is not None
                else _try_json(row["value_text"])
            )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        run_id = str(row["run_id"])
        profile = {
            "run_id": run_id,
            "information": {
                "run": {field: row[field] for field in run_fields},
                "parameters": parameters[run_id],
                "metrics": metrics[run_id],
            },
        }
        grouped.setdefault((str(row["dataset_id"]), str(row["parameter_hash"])), []).append(profile)
    profiles: dict[str, dict[str, dict[str, Any]]] = {}
    for (dataset, parameter_hash), values in grouped.items():
        if len(values) == 1:
            profiles.setdefault(dataset, {})[parameter_hash] = values[0]
    return profiles


def _pair_information_agreement(
    profiles: dict[str, dict[str, dict[str, Any]]], left: str, right: str
) -> dict[str, Any]:
    left_profiles, right_profiles = profiles.get(left, {}), profiles.get(right, {})
    common = sorted(set(left_profiles) & set(right_profiles))
    consistent = sum(
        left_profiles[key]["information"] == right_profiles[key]["information"]
        for key in common
    )
    return {
        "information_consistent_count": consistent,
        "information_comparable_count": len(common),
        "information_agreement_ratio": consistent / len(common) if common else None,
        "information_scope": "run result, parameters, and non-task-specific indexed metrics",
        "information_exclusions": "job IDs, wall-clock/runtime bookkeeping, speedup, worker/host resource fields",
    }


def _normalized_comparisons(path: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "dataset_relations" not in tables:
            return []
        counts = {
            str(row["dataset_id"]): int(row["count"])
            for row in connection.execute(
                "SELECT dataset_id, COUNT(*) AS count FROM runs GROUP BY dataset_id"
            )
        }
        information_profiles = _comparison_information_profiles(connection)
        result: list[dict[str, Any]] = []
        for row in connection.execute(
            "SELECT * FROM dataset_relations ORDER BY left_dataset_id, right_dataset_id"
        ):
            left = str(row["left_dataset_id"])
            right = str(row["right_dataset_id"])
            details = _try_json(row["details_json"])
            details = details if isinstance(details, dict) else {}
            left_count = counts.get(left, 0)
            right_count = counts.get(right, 0)
            matched = int(
                details.get("matched_count")
                or details.get("run_count")
                or (min(left_count, right_count) if row["role"] == "duplicate_alias" else 0)
            )
            result.append(
                {
                    "id": hashlib.sha256(f"{left}\0{right}".encode()).hexdigest()[:20],
                    "left": left,
                    "right": right,
                    "role": str(row["role"]),
                    "matched": matched,
                    "left_only": max(0, left_count - matched),
                    "right_only": max(0, right_count - matched),
                    **_pair_information_agreement(information_profiles, left, right),
                    **(
                        {"agreement": float(details["agreement"])}
                        if isinstance(details.get("agreement"), (int, float))
                        else {}
                    ),
                    "note": str(
                        details.get("reason")
                        or (
                            "Canonical run sets are identical; the alias is excluded from aggregates."
                            if row["role"] == "duplicate_alias"
                            else "Comparison role derived from recorded provenance and canonical inputs."
                        )
                    ),
                }
            )
        return result
    finally:
        connection.close()


def _ego_trajectory_from_indexed_run(
    row: sqlite3.Row, policy: PathPolicy | None
) -> dict[float, tuple[float, float]]:
    if "trace_paths_json" not in set(row.keys()) or not row["trace_paths_json"]:
        return {}
    try:
        trace_paths = json.loads(str(row["trace_paths_json"]))
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(trace_paths, dict):
        return {}
    raw_path = trace_paths.get("agent_states")
    if not raw_path:
        return {}
    supplied = Path(str(raw_path)).expanduser()
    try:
        path = (
            policy.resolve(
                supplied,
                field="trajectory_comparison.agent_states",
                kind="file",
                suffixes={".csv"},
            )
            if policy is not None
            else supplied.resolve(strict=True)
        )
    except (APIError, OSError):
        return {}
    if path.name not in {"agent_states.csv", "agent_state.csv"}:
        return {}

    positions: dict[float, tuple[float, float]] = {}
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            for raw in csv.DictReader(handle):
                row_values = {
                    (key or "").strip(): _coerce_csv_value((value or "").strip())
                    for key, value in raw.items()
                }
                if "is_ego" in row_values:
                    is_ego = _truthy(row_values.get("is_ego"))
                else:
                    identity = str(
                        row_values.get("entity_name")
                        or row_values.get("role")
                        or row_values.get("actor_name")
                        or ""
                    ).casefold()
                    is_ego = "ego" in identity or str(row_values.get("agent_id")) == "0"
                if not is_ego:
                    continue
                timestamp = _first_number(
                    row_values.get("time"),
                    row_values.get("time_s"),
                    row_values.get("sim_time_s"),
                    row_values.get("timestamp"),
                )
                if timestamp is None:
                    milliseconds = _first_number(
                        row_values.get("sim_time_ms"), row_values.get("timestamp_ms")
                    )
                    timestamp = milliseconds / 1_000.0 if milliseconds is not None else None
                x = _first_number(
                    row_values.get("x"),
                    row_values.get("position_x"),
                    row_values.get("location_x"),
                )
                y = _first_number(
                    row_values.get("y"),
                    row_values.get("position_y"),
                    row_values.get("location_y"),
                )
                if timestamp is None or x is None or y is None:
                    continue
                positions[round(timestamp, 9)] = (x, y)
    except (OSError, csv.Error):
        return {}
    return positions


def _trajectory_statistic(
    samples: list[dict[str, Any]], key: str
) -> dict[str, Any]:
    values = [float(sample["variation"]) for sample in samples]
    if not values:
        return {
            "max": None,
            "min": None,
            "mean": None,
            "std": None,
            "median": None,
            "representatives": {},
        }
    statistics_by_name = {
        "max": max(values),
        "min": min(values),
        "mean": statistics.fmean(values),
        "std": statistics.pstdev(values),
        "median": statistics.median(values),
    }
    representatives: dict[str, dict[str, Any]] = {}
    for name, target in statistics_by_name.items():
        nearest = min(
            samples,
            key=lambda sample: (
                abs(float(sample["variation"]) - target),
                str(sample["parameter_hash"]),
                str(sample["left_experiment"]),
                str(sample["right_experiment"]),
            ),
        )
        representatives[name] = {
            **nearest,
            "distance_to_statistic": abs(float(nearest["variation"]) - target),
        }
    return {**statistics_by_name, "key": key, "representatives": representatives}


def _cross_trajectory_summary(
    sample_runs: dict[str, dict[str, sqlite3.Row]],
    datasets: list[str],
    policy: PathPolicy | None,
) -> dict[str, Any]:
    if not sample_runs or not datasets:
        return {"available": False, "reason": "no_common_samples"}
    first_row = next(iter(next(iter(sample_runs.values())).values()))
    if "trace_paths_json" not in set(first_row.keys()):
        return {"available": False, "reason": "trajectory_paths_not_indexed"}

    trajectory_cache: dict[str, dict[float, tuple[float, float]]] = {}
    ade_samples: list[dict[str, Any]] = []
    fde_samples: list[dict[str, Any]] = []
    partial_samples = 0
    unavailable_samples = 0
    for parameter_hash, rows_by_dataset in sample_runs.items():
        trajectories: dict[str, dict[float, tuple[float, float]]] = {}
        for dataset in datasets:
            row = rows_by_dataset[dataset]
            run_id = str(row["run_id"])
            if run_id not in trajectory_cache:
                trajectory_cache[run_id] = _ego_trajectory_from_indexed_run(row, policy)
            if len(trajectory_cache[run_id]) >= 2:
                trajectories[dataset] = trajectory_cache[run_id]
        if len(trajectories) != len(datasets):
            if len(trajectories) >= 2:
                partial_samples += 1
            else:
                unavailable_samples += 1
            continue

        pair_results: list[dict[str, Any]] = []
        for left, right in combinations(datasets, 2):
            common_times = sorted(set(trajectories[left]) & set(trajectories[right]))
            if len(common_times) < 2:
                continue
            distances = [
                math.hypot(
                    trajectories[left][time][0] - trajectories[right][time][0],
                    trajectories[left][time][1] - trajectories[right][time][1],
                )
                for time in common_times
            ]
            pair_results.append(
                {
                    "parameter_hash": parameter_hash,
                    "left_experiment": left,
                    "right_experiment": right,
                    "left_run_id": str(rows_by_dataset[left]["run_id"]),
                    "right_run_id": str(rows_by_dataset[right]["run_id"]),
                    "common_steps": len(common_times),
                    "ade": statistics.fmean(distances),
                    "fde": distances[-1],
                }
            )
        if not pair_results:
            unavailable_samples += 1
            continue
        for metric, target in (("ade", ade_samples), ("fde", fde_samples)):
            maximum = max(
                pair_results,
                key=lambda item: (
                    float(item[metric]),
                    str(item["left_experiment"]),
                    str(item["right_experiment"]),
                ),
            )
            target.append(
                {
                    key: value
                    for key, value in maximum.items()
                    if key not in {"ade", "fde"}
                }
                | {"variation": float(maximum[metric])}
            )

    eligible = min(len(ade_samples), len(fde_samples))
    return {
        "available": bool(eligible),
        "reason": None if eligible else "no_common_recorded_ego_trajectory_steps",
        "eligible_sample_count": eligible,
        "partial_sample_count": partial_samples,
        "unavailable_sample_count": unavailable_samples,
        "experiment_pair_count": len(datasets) * (len(datasets) - 1) // 2,
        "alignment_rule": (
            "Exact common recorded timestamps only; no interpolation. Each sample uses "
            "the experiment pair with the largest ego trajectory difference."
        ),
        "ade": _trajectory_statistic(ade_samples, "ade"),
        "fde": _trajectory_statistic(fde_samples, "fde"),
    }


def _normalized_cross_experiment_summary(
    path: Path, policy: PathPolicy | None = None
) -> dict[str, Any]:
    """Summarize variation across every canonical dataset on common samples.

    Samples are paired only by a parameter hash that occurs exactly once in every
    included dataset. Metrics are summarized only when all datasets provide a
    finite valid number for that sample. Missing and invalid metric cells remain
    explicit coverage counts and never receive a numeric sentinel.
    """

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"runs", "metrics"}.issubset(tables):
            return {"available": False, "reason": "normalized_metric_index_required"}

        run_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(runs)")
        }
        if not {"run_id", "dataset_id", "parameter_hash"}.issubset(run_columns):
            return {"available": False, "reason": "parameter_hash_unavailable"}

        excluded_aliases: list[str] = []
        if "dataset_relations" in tables:
            excluded_aliases = [
                str(row[0])
                for row in connection.execute(
                    "SELECT right_dataset_id FROM dataset_relations "
                    "WHERE role = 'duplicate_alias' ORDER BY right_dataset_id"
                )
            ]
        dataset_rows = connection.execute(
            "SELECT dataset_id, COUNT(*) AS run_count FROM runs "
            "GROUP BY dataset_id HAVING COUNT(*) > 0 ORDER BY dataset_id"
        ).fetchall()
        datasets = [
            str(row["dataset_id"])
            for row in dataset_rows
            if str(row["dataset_id"]) not in excluded_aliases
        ]
        if len(datasets) < 2:
            return {
                "available": False,
                "reason": "at_least_two_canonical_experiments_required",
                "experiments": datasets,
                "excluded_duplicate_aliases": excluded_aliases,
            }

        placeholders = ",".join("?" for _ in datasets)
        hash_quality = {
            str(row["dataset_id"]): {
                "run_count": int(row["run_count"]),
                "missing_hash_runs": int(row["missing_hash_runs"]),
                "ambiguous_hashes": 0,
            }
            for row in connection.execute(
                f"""
                SELECT dataset_id, COUNT(*) AS run_count,
                       SUM(CASE WHEN parameter_hash IS NULL OR parameter_hash = ''
                                THEN 1 ELSE 0 END) AS missing_hash_runs
                FROM runs WHERE dataset_id IN ({placeholders})
                GROUP BY dataset_id ORDER BY dataset_id
                """,
                datasets,
            )
        }
        for row in connection.execute(
            f"""
            SELECT dataset_id, COUNT(*) AS ambiguous_hashes
            FROM (
                SELECT dataset_id, parameter_hash
                FROM runs
                WHERE dataset_id IN ({placeholders})
                  AND parameter_hash IS NOT NULL AND parameter_hash != ''
                GROUP BY dataset_id, parameter_hash HAVING COUNT(*) > 1
            )
            GROUP BY dataset_id
            """,
            datasets,
        ):
            hash_quality[str(row["dataset_id"])]["ambiguous_hashes"] = int(
                row["ambiguous_hashes"]
            )

        common_cte = f"""
            WITH hash_counts AS (
                SELECT dataset_id, parameter_hash, COUNT(*) AS occurrence_count,
                       MIN(run_id) AS run_id
                FROM runs
                WHERE dataset_id IN ({placeholders})
                  AND parameter_hash IS NOT NULL AND parameter_hash != ''
                GROUP BY dataset_id, parameter_hash
            ), unique_runs AS (
                SELECT dataset_id, parameter_hash, run_id
                FROM hash_counts WHERE occurrence_count = 1
            ), common_hashes AS (
                SELECT parameter_hash
                FROM unique_runs
                GROUP BY parameter_hash
                HAVING COUNT(DISTINCT dataset_id) = {len(datasets)}
            )
        """
        union_sample_count = int(
            connection.execute(
                f"""
                WITH hash_counts AS (
                    SELECT dataset_id, parameter_hash, COUNT(*) AS occurrence_count
                    FROM runs
                    WHERE dataset_id IN ({placeholders})
                      AND parameter_hash IS NOT NULL AND parameter_hash != ''
                    GROUP BY dataset_id, parameter_hash
                )
                SELECT COUNT(DISTINCT parameter_hash)
                FROM hash_counts WHERE occurrence_count = 1
                """,
                datasets,
            ).fetchone()[0]
            or 0
        )

        optional_run_fields = [
            name
            for name in (
                "outcome_class",
                "has_collision",
                "status",
                "stop_condition",
                "stop_reason",
                "trace_paths_json",
            )
            if name in run_columns
        ]
        run_select = ", ".join(f"r.{name}" for name in optional_run_fields)
        if run_select:
            run_select = ", " + run_select
        run_rows = connection.execute(
            common_cte
            + f"""
            SELECT u.parameter_hash, u.dataset_id, u.run_id{run_select}
            FROM unique_runs AS u
            JOIN common_hashes AS c ON c.parameter_hash = u.parameter_hash
            JOIN runs AS r ON r.run_id = u.run_id
            ORDER BY u.parameter_hash, u.dataset_id
            """,
            datasets,
        ).fetchall()
        sample_runs: dict[str, dict[str, sqlite3.Row]] = {}
        for row in run_rows:
            parameter_hash = str(row["parameter_hash"])
            dataset_id = str(row["dataset_id"])
            sample_runs.setdefault(parameter_hash, {})[dataset_id] = row
        common_sample_count = len(sample_runs)

        discrete_definitions = [
            ("outcome", "Outcome", "outcome_class"),
            ("collision", "Collision", "has_collision"),
            ("status", "Run status", "status"),
            ("stop_condition", "Stop condition", "stop_condition"),
            ("stop_reason", "Stop reason", "stop_reason"),
        ]
        discrete: list[dict[str, Any]] = []
        for key, label, column in discrete_definitions:
            if column not in optional_run_fields:
                continue
            consistent = 0
            comparable = 0
            unavailable = 0
            for rows_by_dataset in sample_runs.values():
                values = [rows_by_dataset[dataset][column] for dataset in datasets]
                if any(value is None or str(value).strip() == "" for value in values):
                    unavailable += 1
                    continue
                comparable += 1
                if len({str(value) for value in values}) == 1:
                    consistent += 1
            discrete.append(
                {
                    "key": key,
                    "label": label,
                    "consistent_count": consistent,
                    "comparable_count": comparable,
                    "agreement_ratio": consistent / comparable if comparable else None,
                    "unavailable_sample_count": unavailable,
                }
            )

        metric_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(metrics)")
        }
        value_text_expression = (
            "m.value_text" if "value_text" in metric_columns else "NULL AS value_text"
        )
        metric_rows = connection.execute(
            common_cte
            + f"""
            SELECT u.parameter_hash, u.dataset_id, m.name, m.value_real,
                   m.value_type, {value_text_expression}
            FROM unique_runs AS u
            JOIN common_hashes AS c ON c.parameter_hash = u.parameter_hash
            JOIN metrics AS m ON m.run_id = u.run_id
            ORDER BY m.name, u.parameter_hash, u.dataset_id
            """,
            datasets,
        ).fetchall()
        metric_cells: dict[str, dict[str, dict[str, tuple[str, float | None]]]] = {}
        metrics_with_valid_numbers: set[str] = set()
        for row in metric_rows:
            name = str(row["name"])
            parameter_hash = str(row["parameter_hash"])
            dataset_id = str(row["dataset_id"])
            state = "invalid"
            numeric: float | None = None
            try:
                candidate = float(row["value_real"])
            except (TypeError, ValueError):
                candidate = math.nan
            if (
                str(row["value_type"]) == "number"
                and math.isfinite(candidate)
                and ("ttc" not in name.casefold() or candidate >= 0)
            ):
                state = "valid"
                numeric = candidate
                metrics_with_valid_numbers.add(name)
            metric_cells.setdefault(name, {}).setdefault(parameter_hash, {})[
                dataset_id
            ] = (state, numeric)

        continuous: list[dict[str, Any]] = []
        total_execution_cells = common_sample_count * len(datasets)
        for name in sorted(metrics_with_valid_numbers, key=_cross_metric_sort_key):
            variation_samples: list[tuple[str, float]] = []
            partial_samples = 0
            unavailable_samples = 0
            missing_executions = 0
            invalid_executions = 0
            cells_by_sample = metric_cells[name]
            for parameter_hash in sample_runs:
                cells = cells_by_sample.get(parameter_hash, {})
                valid_values: list[float] = []
                for dataset_id in datasets:
                    cell = cells.get(dataset_id)
                    if cell is None:
                        missing_executions += 1
                    elif cell[0] != "valid" or cell[1] is None:
                        invalid_executions += 1
                    else:
                        valid_values.append(cell[1])
                if len(valid_values) == len(datasets):
                    variation_samples.append(
                        (parameter_hash, max(valid_values) - min(valid_values))
                    )
                elif valid_values:
                    partial_samples += 1
                else:
                    unavailable_samples += 1
            variations = [value for _parameter_hash, value in variation_samples]
            variation_max = max(variations) if variations else None
            variation_min = min(variations) if variations else None
            variation_p95 = _linear_percentile(variations, 0.95)
            variation_std = statistics.pstdev(variations) if variations else None
            variation_median = statistics.median(variations) if variations else None

            def representative(
                target: float | None,
                samples: list[tuple[str, float]] = variation_samples,
                runs: dict[str, dict[str, sqlite3.Row]] = sample_runs,
                first_dataset: str = datasets[0],
            ) -> dict[str, Any] | None:
                if target is None or not samples:
                    return None
                parameter_hash, value = min(
                    samples,
                    key=lambda sample: (abs(sample[1] - target), sample[0]),
                )
                return {
                    "parameter_hash": parameter_hash,
                    "run_id": str(runs[parameter_hash][first_dataset]["run_id"]),
                    "variation": value,
                }

            continuous.append(
                {
                    "key": name,
                    "label": _cross_metric_label(name),
                    "unit": _cross_metric_unit(name),
                    "eligible_sample_count": len(variations),
                    "partial_sample_count": partial_samples,
                    "unavailable_sample_count": unavailable_samples,
                    "valid_execution_count": (
                        total_execution_cells
                        - missing_executions
                        - invalid_executions
                    ),
                    "total_execution_count": total_execution_cells,
                    "missing_execution_count": missing_executions,
                    "invalid_execution_count": invalid_executions,
                    "variation_max": variation_max,
                    "variation_min": variation_min,
                    "variation_p95": variation_p95,
                    "variation_std": variation_std,
                    "variation_median": variation_median,
                    "representatives": {
                        key: item
                        for key, item in (
                            ("max", representative(variation_max)),
                            ("min", representative(variation_min)),
                            ("p95", representative(variation_p95)),
                            ("std", representative(variation_std)),
                            ("median", representative(variation_median)),
                        )
                        if item is not None
                    },
                    "validity_rule": (
                        "finite non-negative values in every experiment"
                        if "ttc" in name.casefold()
                        else "finite numeric values in every experiment"
                    ),
                }
            )

        return {
            "available": True,
            "experiments": datasets,
            "experiment_count": len(datasets),
            "excluded_duplicate_aliases": excluded_aliases,
            "pairing_key": "parameter_hash unique within every included experiment",
            "common_sample_count": common_sample_count,
            "union_sample_count": union_sample_count,
            "excluded_noncommon_sample_count": max(
                0, union_sample_count - common_sample_count
            ),
            "hash_quality": hash_quality,
            "discrete": discrete,
            "continuous": continuous,
            "trajectory": _cross_trajectory_summary(sample_runs, datasets, policy),
            "variation_definition": "per-sample maximum minus minimum",
            "std_definition": "population standard deviation across eligible sample variations",
            "missing_value_rule": (
                "Only samples with a valid value in every experiment contribute to variation; "
                "partial, unavailable, missing, and invalid values are counted separately."
            ),
        }
    finally:
        connection.close()


def _cross_metric_sort_key(name: str) -> tuple[int, str]:
    lowered = name.casefold()
    priorities = (
        ("total_steps", 0),
        ("final_sim_time", 1),
        ("wall_time", 2),
        ("ttc", 3),
        ("relative_distance", 4),
        ("distance", 5),
    )
    return next(
        ((priority, lowered) for token, priority in priorities if token in lowered),
        (10, lowered),
    )


def _cross_metric_label(name: str) -> str:
    lowered = name.casefold()
    if "total_steps" in lowered or "terminal_step" in lowered:
        return "Terminal step / total steps"
    if "final_sim_time" in lowered or "simulated_duration" in lowered:
        return "Simulated duration"
    if "wall_time" in lowered or "wall_duration" in lowered:
        return "Wall duration"
    if "ttc" in lowered and ("min" in lowered or "minimum" in lowered):
        return "Minimum TTC"
    if "distance" in lowered and ("min" in lowered or "minimum" in lowered):
        return "Minimum relative distance"
    return name.replace(".", " · ").replace("_", " ").strip().title()


def _cross_metric_unit(name: str) -> str | None:
    lowered = name.casefold()
    if lowered.endswith("_ms"):
        return "ms"
    if "total_steps" in lowered or "terminal_step" in lowered:
        return "steps"
    if "ttc" in lowered or lowered.endswith("_s"):
        return "s"
    if "distance" in lowered and (lowered.endswith("_m") or ".min" in lowered):
        return "m"
    if "speedup" in lowered:
        return "×"
    return None


def _linear_percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _frontend_finding(value: Any, index: int) -> dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    code = str(item.get("code") or "data_health")
    detail = str(item.get("message") or item.get("detail") or "Data-health finding")
    dataset = item.get("dataset_id")
    run = item.get("run_id")
    identity = hashlib.sha256(
        f"{code}\0{dataset or ''}\0{run or ''}\0{index}".encode()
    ).hexdigest()[:20]
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    affected = details.get("affected_runs")
    return {
        "id": identity,
        "severity": item.get("severity") if item.get("severity") in {"info", "warning", "error"} else "info",
        "code": code,
        "title": code.replace("_", " ").strip().title(),
        "detail": detail,
        **({"affected_runs": int(affected)} if isinstance(affected, (int, float)) else {}),
        "dataset_id": dataset,
        "run_id": run,
    }


def _frontend_overview(
    identifier: str,
    preview: dict[str, Any],
    data: dict[str, Any],
    *,
    normalized: bool,
) -> dict[str, Any]:
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    if normalized:
        outcomes = {
            key: int(summary.get(key) or 0) for key in ("success", "fail", "invalid", "unknown")
        }
        health_values = data.get("findings") if isinstance(data.get("findings"), list) else []
        health = [_frontend_finding(item, index) for index, item in enumerate(health_values)]
        return {
            "dataset_id": identifier,
            "generated_at": data.get("generated_at") or preview.get("generated_at"),
            "experiment_count": int(
                data.get("aggregate_dataset_count") or data.get("dataset_count") or 0
            ),
            "run_count": int(summary.get("total") or 0),
            "browsable_run_count": int(data.get("all_browsable_runs") or summary.get("total") or 0),
            "total_dataset_count": int(data.get("dataset_count") or 0),
            "outcomes": outcomes,
            "collision_count": int(summary.get("collision") or 0),
            "health": health,
        }

    rows = summary.get("outcomes") if isinstance(summary.get("outcomes"), list) else []
    counts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("outcome") or "unknown").casefold()
        canonical = {"failure": "fail", "failed": "fail", "passed": "success"}.get(name, name)
        counts[canonical] = counts.get(canonical, 0) + int(row.get("count") or 0)
    run_count = int(summary.get("run_count") or preview.get("run_count") or 0)
    known = sum(counts.get(key, 0) for key in ("success", "fail", "invalid"))
    outcomes = {
        "success": counts.get("success", 0),
        "fail": counts.get("fail", 0),
        "invalid": counts.get("invalid", 0),
        "unknown": counts.get("unknown", max(0, run_count - known)),
    }
    performance = summary.get("performance") if isinstance(summary.get("performance"), list) else []
    simulated = _performance_total(performance, ("simulated_seconds", "simulation_seconds"))
    wall = _performance_total(performance, ("wall_seconds", "wall_time_seconds"))
    return {
        "dataset_id": identifier,
        "generated_at": preview.get("generated_at"),
        "experiment_count": int(summary.get("experiment_count") or preview.get("experiment_count") or 0),
        "run_count": run_count,
        "outcomes": outcomes,
        "collision_count": counts.get("collision", 0),
        **({"simulated_seconds": simulated} if simulated is not None else {}),
        **({"wall_seconds": wall} if wall is not None else {}),
        "health": preview.get("health") or [],
    }


def _performance_total(rows: list[Any], names: tuple[str, ...]) -> float | None:
    values = [
        float(row.get("value"))
        for row in rows
        if isinstance(row, dict)
        and row.get("metric") in names
        and isinstance(row.get("value"), (int, float))
    ]
    return sum(values) if values else None


def _load_mapping(path: Path, *, label: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"failed to read {label}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{label} must contain a mapping")
    return data


def _load_legacy_data(root: Path, maximum_bytes: int = 256 * 1024 * 1024) -> dict[str, Any] | None:
    path = root / "report" / "analysis_data.json"
    if not path.is_file():
        return None
    if path.stat().st_size > maximum_bytes:
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def _sqlite_overview(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        overview: dict[str, Any] = {"store": {"schema": 1, "tables": sorted(tables)}}
        if "runs" in tables:
            overview["summary"] = {
                "run_count": connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            }
        if "metadata" in tables:
            metadata = connection.execute("SELECT key, value FROM metadata").fetchall()
            overview["metadata"] = {row["key"]: _try_json(row["value"]) for row in metadata}
        return overview
    finally:
        connection.close()


def _normalized_index_runs(
    path: Path,
    *,
    cursor: str | None,
    limit: int,
    outcome: str | None,
    experiment: str | None,
    query: str | None,
    sort: str | None,
    descending: bool,
) -> dict[str, Any] | None:
    if not _is_normalized_index(path):
        return None
    try:
        from pisa_sample_tools.reporting import ReportIndex, RunFilter

        outcome_class = {
            "failure": "fail",
            "failed": "fail",
            "test_fail": "fail",
        }.get(outcome or "", outcome)
        canonical = {"success", "fail", "invalid", "unknown"}
        filters = RunFilter(
            dataset_ids=(experiment,) if experiment else (),
            outcome_classes=(outcome_class,) if outcome_class in canonical else (),
            outcomes=(outcome,) if outcome and outcome_class not in canonical else (),
            search=query,
        )
        sort_by = {
            None: "scenario_id",
            "id": "run_id",
            "experiment": "dataset_id",
            "experiment_id": "dataset_id",
            "normalized_outcome": "outcome_class",
        }.get(sort, sort)
        with ReportIndex(path) as index:
            page = index.page_runs(
                filters=filters,
                limit=limit,
                cursor=cursor,
                sort_by=sort_by,
                sort_direction="desc" if descending else "asc",
            )
        return {
            "items": [_normalize_run(item.as_dict()) for item in page.items],
            "total": page.total,
            "cursor": cursor,
            "next_cursor": page.next_cursor,
            "limit": page.limit,
            "source": "normalized_index",
        }
    except ImportError:
        return None
    except ValueError as exc:
        raise APIError(400, "invalid_run_query", str(exc)) from exc


def _normalized_experiment_summaries(path: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        datasets = connection.execute(
            "SELECT dataset_id, simulator, av, sampler FROM datasets ORDER BY dataset_id"
        ).fetchall()
        runs = connection.execute(
            "SELECT run_id, dataset_id, outcome_class FROM runs ORDER BY dataset_id, run_id"
        ).fetchall()
        metric_rows = connection.execute(
            "SELECT run_id, name, value_real FROM metrics WHERE value_real IS NOT NULL"
        ).fetchall()
    finally:
        connection.close()
    metrics: dict[str, dict[str, float]] = {}
    for row in metric_rows:
        metrics.setdefault(str(row["run_id"]), {})[str(row["name"])] = float(row["value_real"])
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in runs:
        grouped.setdefault(str(row["dataset_id"]), []).append(row)
    summaries = []
    for dataset in datasets:
        dataset_id = str(dataset["dataset_id"])
        items = grouped.get(dataset_id, [])
        outcomes = Counter(str(row["outcome_class"]) for row in items)
        durations: list[float] = []
        speedups: list[float] = []
        for row in items:
            values = metrics.get(str(row["run_id"]), {})
            duration = next((values[key] for key in ("duration_seconds", "sim_duration_s", "simulation_time_seconds", "simulation_time_s") if key in values), None)
            if duration is None:
                duration_ms = next((values[key] for key in ("run.final_sim_time_ms", "final_sim_time_ms", "run.wall_time_ms") if key in values), None)
                duration = duration_ms / 1_000.0 if duration_ms is not None else None
            if duration is not None:
                durations.append(duration)
            recorded_speedup = values.get("run.speedup", values.get("speedup"))
            if recorded_speedup is not None:
                speedups.append(recorded_speedup)
            else:
                simulated_ms = values.get("run.final_sim_time_ms")
                wall_ms = values.get("run.wall_time_ms")
                if simulated_ms is not None and wall_ms is not None and wall_ms > 0:
                    speedups.append(simulated_ms / wall_ms)
        summaries.append(
            {
                "experiment": dataset_id,
                "simulator": dataset["simulator"],
                "av": dataset["av"],
                "sampler": dataset["sampler"],
                "total_samples": len(items),
                "success": outcomes["success"],
                "fail": outcomes["fail"],
                "invalid": outcomes["invalid"],
                "unknown": outcomes["unknown"],
                "avg_time_seconds": sum(durations) / len(durations) if durations else None,
                "avg_speedup": sum(speedups) / len(speedups) if speedups else None,
            }
        )
    return summaries


def _normalized_index_run(
    path: Path, run_id: str, *, maximum_points: int, policy: PathPolicy,
    include_map: bool = True,
) -> dict[str, Any] | None:
    if not _is_normalized_index(path):
        return None
    try:
        from pisa_sample_tools.reporting import ReportIndex

        with ReportIndex(path) as index:
            item = index.run(run_id)
            attempts = index.attempts(run_id) if item is not None else ()
            dataset = index.dataset(item.dataset_id) if item is not None else None
            fingerprints = (
                index.source_fingerprints(dataset_id=item.dataset_id)
                if item is not None
                else ()
            )
        if item is None:
            return None
        run = _normalize_run(item.as_dict())
        traces: dict[str, list[dict[str, Any]]] = {}
        events: list[dict[str, Any]] = []
        geometry: list[dict[str, Any]] = []
        validated_paths = _validated_normalized_trace_paths(item, fingerprints, policy)
        for name, trace_path in validated_paths.items():
            try:
                rows = _read_trace(trace_path, maximum_points=maximum_points)
            except OSError:
                rows = []
            normalized_name = name.casefold()
            if normalized_name == "agent_states":
                grouped: dict[str, list[dict[str, Any]]] = {}
                for row in rows:
                    agent_id = str(row.get("agent_id") or "unknown")
                    entity_name = _optional_text(row.get("entity_name"))
                    label = "ego" if _truthy(row.get("is_ego")) else entity_name or f"agent_{agent_id}"
                    grouped.setdefault(label, []).append(_trace_point(row))
                traces.update(grouped)
            elif normalized_name in {"agent_controls", "control_commands"}:
                grouped = {}
                for row in rows:
                    agent_id = str(row.get("agent_id") or "unknown")
                    grouped.setdefault(f"controls_{agent_id}", []).append(_trace_point(row))
                traces.update(grouped)
            elif normalized_name in {
                "events",
                "scenario_events",
                "collisions",
                "collision_events",
            }:
                events.extend(_event_point(row, fallback_type=normalized_name) for row in rows)
            elif normalized_name == "frame_metrics":
                traces["metrics"] = [_trace_point(row) for row in rows]
            elif normalized_name == "agent_geometry":
                geometry = [_geometry_point(row) for row in rows]
            else:
                traces[name] = [_trace_point(row) for row in rows]
        map_payload = _normalized_case_map(item, dataset, fingerprints, policy) if include_map else {"status": "omitted"}
        try:
            from pisa_sample_tools.common.goal import load_ego_goal

            goal, goal_warning = load_ego_goal(Path(item.result_path).parent)
            ego_goal = goal.as_dict() if goal is not None else None
        except (ImportError, OSError, ValueError) as exc:
            ego_goal, goal_warning = None, str(exc)
        return {
            "source": "normalized_index",
            "run": run,
            "attempts": [attempt.as_dict() for attempt in attempts],
            "traces": traces,
            "events": sorted(events, key=lambda item: float(item.get("time") or 0)),
            "geometry": geometry,
            "map": map_payload,
            "ego_goal": ego_goal,
            "ego_goal_warning": goal_warning,
            "navigation": _case_navigation(path, item),
            "trace_channels": {
                name: {
                    "point_count": len(points),
                    "fields": sorted(
                        {
                            key
                            for point in points
                            for key in (point.get("values") or {})
                        }
                    ),
                }
                for name, points in traces.items()
            },
        }
    except (ImportError, ValueError, sqlite3.DatabaseError):
        return None


def _validated_normalized_trace_paths(
    item: Any, fingerprints: tuple[Any, ...], policy: PathPolicy
) -> dict[str, Path]:
    allowed_names = {
        "frame_metrics": {"frame_metrics.csv"},
        "agent_states": {"agent_states.csv", "agent_state.csv"},
        "agent_geometry": {"agent_geometry.csv"},
        "collision_events": {"collision_events.csv"},
        "scenario_events": {"scenario_events.csv"},
        "control_commands": {"control_commands.csv"},
    }
    supplied_result = Path(item.result_path).expanduser()
    if supplied_result.is_symlink():
        return {}
    try:
        result_path = policy.resolve(
            supplied_result,
            field="indexed_result",
            kind="file",
            suffixes={".csv"},
        )
    except APIError:
        return {}
    recorded_results = {
        fingerprint.path.expanduser().resolve()
        for fingerprint in fingerprints
        if fingerprint.kind == "result_csv"
    }
    if result_path not in recorded_results:
        return {}

    validated: dict[str, Path] = {}
    for name, raw_value in item.trace_paths.items():
        names = allowed_names.get(name)
        supplied = Path(raw_value).expanduser()
        if names is None or supplied.is_symlink():
            continue
        try:
            trace_path = policy.resolve(
                supplied,
                field=f"trace_paths.{name}",
                kind="file",
                suffixes={".csv"},
            )
        except APIError:
            continue
        if trace_path.parent == result_path.parent and trace_path.name in names:
            validated[name] = trace_path
    return validated


def _case_navigation(path: Path, item: Any) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT run_id FROM runs WHERE dataset_id = ?
            ORDER BY CASE WHEN scenario_id GLOB '[0-9]*' THEN CAST(scenario_id AS INTEGER) END,
                     scenario_id, run_id
            """,
            (item.dataset_id,),
        ).fetchall()
        current = connection.execute(
            "SELECT parameter_hash, sample_id, scenario_id FROM runs WHERE run_id = ?",
            (item.run_id,),
        ).fetchone()
        paired: list[sqlite3.Row] = []
        pairing = "scenario_id"
        pairing_value = str(item.scenario_id)
        if current is not None:
            if current["parameter_hash"]:
                pairing, pairing_value = "parameter_hash", str(current["parameter_hash"])
            elif current["sample_id"]:
                pairing, pairing_value = "sample_id", str(current["sample_id"])
            paired = connection.execute(
                f"SELECT run_id, dataset_id, scenario_id, outcome_class FROM runs WHERE {pairing} = ? ORDER BY dataset_id, run_id",
                (pairing_value,),
            ).fetchall()
    finally:
        connection.close()
    identifiers = [str(row[0]) for row in rows]
    try:
        index = identifiers.index(str(item.run_id))
    except ValueError:
        return {"previous_run_id": None, "next_run_id": None, "ordinal": None, "total": len(identifiers)}
    return {
        "previous_run_id": identifiers[index - 1] if index > 0 else None,
        "next_run_id": identifiers[index + 1] if index + 1 < len(identifiers) else None,
        "ordinal": index + 1,
        "total": len(identifiers),
        "sample_key": {"field": pairing, "value": pairing_value},
        "comparison_runs": [dict(row) for row in paired],
    }


def _normalized_case_map(
    item: Any, dataset: Any, fingerprints: tuple[Any, ...], policy: PathPolicy
) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "unavailable"}
    if dataset is None or dataset.manifest_path is None:
        return payload
    recorded_manifest = next(
        (
            fingerprint.path.expanduser().resolve()
            for fingerprint in fingerprints
            if fingerprint.kind == "execution_manifest"
        ),
        None,
    )
    try:
        manifest_path = policy.resolve(
            dataset.manifest_path,
            field="dataset.manifest_path",
            kind="file",
            suffixes={".yaml", ".yml", ".json"},
        )
    except APIError:
        return {**payload, "warning": "execution manifest is outside configured roots"}
    if recorded_manifest is not None and manifest_path != recorded_manifest:
        return {**payload, "warning": "execution manifest does not match indexed provenance"}
    manifest = _read_optional_mapping(manifest_path)
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    resolved = manifest.get("resolved_inputs") if isinstance(manifest.get("resolved_inputs"), dict) else {}
    map_name = _optional_text(metadata.get("map_name"))
    raw_xodr = resolved.get("map_xodr") or metadata.get("xodr_path")
    try:
        from pisa_sample_tools.evidence.opendrive import discover_xodr, load_map_geometry

        xodr: Path | None = None
        if raw_xodr not in {None, ""}:
            xodr = _recorded_xodr_candidate(raw_xodr, map_name, policy)
        if xodr is None:
            xodr = discover_xodr(
                Path(item.result_path),
                {"map_name": map_name},
            )
            if xodr is not None:
                xodr = policy.resolve(xodr, field="map_xodr", kind="file", suffixes={".xodr"})
        if xodr is None:
            return {**payload, "name": map_name, "warning": "OpenDRIVE source was not resolved"}
        geometry, warning = load_map_geometry(xodr)
        return {
            "status": "available" if geometry is not None else "error",
            "name": map_name or xodr.stem,
            "source": xodr.name,
            "geometry": geometry,
            "warning": warning,
        }
    except (ImportError, OSError, ValueError, APIError) as exc:
        return {**payload, "name": map_name, "warning": str(exc)}


def _recorded_xodr_candidate(
    raw_value: Any, map_name: str | None, policy: PathPolicy
) -> Path | None:
    """Resolve the manifest-recorded OpenDRIVE input without granting general file access.

    Map inputs commonly live beside, rather than below, the configured output root.
    Outside-root access is therefore limited to a real, non-symlink ``.xodr`` file
    explicitly named by the already indexed execution manifest.  Nothing else in
    that directory becomes readable through the workbench.
    """

    raw = Path(str(raw_value)).expanduser()
    if raw.is_symlink():
        raise APIError(403, "path_not_allowed", "recorded OpenDRIVE path cannot be a symlink")
    candidate = raw.resolve() if raw.is_absolute() else raw
    try:
        candidate = policy.resolve(candidate, field="resolved_inputs.map_xodr")
    except APIError:
        if not candidate.is_absolute() or candidate.is_symlink() or not candidate.exists():
            raise
    choices: list[Path]
    if candidate.is_file():
        choices = [candidate]
    elif candidate.is_dir() and not candidate.is_symlink():
        choices = sorted(candidate.glob("*.xodr"))
    else:
        return None
    choices = [
        path.resolve()
        for path in choices
        if path.is_file()
        and not path.is_symlink()
        and path.suffix.casefold() == ".xodr"
        and path.stat().st_size <= 100 * 1024 * 1024
    ]
    named = [path for path in choices if map_name and path.stem == map_name]
    return named[0] if len(named) == 1 else choices[0] if len(choices) == 1 else None


def _is_normalized_index(path: Path) -> bool:
    if not path.is_file():
        return False
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
        return {"outcome_class", "trace_paths_json", "canonical_attempt_id"} <= columns
    finally:
        connection.close()


def _sqlite_runs(
    path: Path,
    *,
    offset: int,
    limit: int,
    outcome: str | None,
    experiment: str | None,
    query: str | None,
    sort: str | None,
    descending: bool,
) -> dict[str, Any] | None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "runs" not in tables:
            return None
        columns = [row[1] for row in connection.execute("PRAGMA table_info(runs)")]
        clauses: list[str] = []
        parameters: list[Any] = []
        if outcome and "outcome" in columns:
            clauses.append("outcome = ?")
            parameters.append(outcome)
        experiment_column = "experiment_id" if "experiment_id" in columns else "dataset_id"
        if experiment and experiment_column in columns:
            clauses.append(f'"{experiment_column}" = ?')
            parameters.append(experiment)
        if query:
            search_columns = [
                column
                for column in ("run_id", "scenario_id")
                if column in columns
            ]
            if search_columns:
                clauses.append(
                    "(" + " OR ".join(f'CAST("{column}" AS TEXT) LIKE ?' for column in search_columns) + ")"
                )
                parameters.extend([f"%{query}%"] * len(search_columns))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = connection.execute(f"SELECT COUNT(*) FROM runs{where}", parameters).fetchone()[0]
        sort_column = sort if sort in columns else ("run_id" if "run_id" in columns else columns[0])
        direction = "DESC" if descending else "ASC"
        rows = connection.execute(
            f'SELECT * FROM runs{where} ORDER BY "{sort_column}" {direction} LIMIT ? OFFSET ?',
            [*parameters, limit, offset],
        ).fetchall()
        items = [
            _normalize_run({key: _try_json(row[key]) for key in columns}) for row in rows
        ]
        return {
            "items": items,
            "total": total,
            "cursor": str(offset) if offset else None,
            "next_cursor": str(offset + limit) if offset + limit < total else None,
            "limit": limit,
            "source": "sqlite",
        }
    finally:
        connection.close()


def _try_json(value: Any) -> Any:
    if not isinstance(value, str) or not value or value[0] not in "[{\"":
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _optional_text(value: Any) -> str | None:
    return None if value in {None, ""} else str(value)


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        value = int(cursor)
    except ValueError as exc:
        raise APIError(400, "invalid_cursor", "cursor is invalid", field="cursor") from exc
    if value < 0:
        raise APIError(400, "invalid_cursor", "cursor is invalid", field="cursor")
    return value


def _sort_value(value: Any) -> tuple[bool, str]:
    return value is None, str(value or "").casefold()


def _normalize_run(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    payload = result.get("payload_json")
    if isinstance(payload, dict):
        result = {**payload, **{key: value for key, value in result.items() if key != "payload_json"}}
    result["id"] = str(result.get("id") or result.get("run_id") or result.get("scenario_id") or "")
    result["experiment"] = result.get("experiment_id") or result.get("dataset_id")
    outcome = (
        result.get("normalized_outcome")
        or result.get("outcome_class")
        or result.get("outcome")
        or "unknown"
    )
    result["outcome"] = {"failure": "fail", "failed": "fail"}.get(str(outcome), outcome)
    if "params" not in result and "params_json" in result:
        result["params"] = result.get("params_json") or {}
    if "metrics" not in result and "metrics_json" in result:
        result["metrics"] = result.get("metrics_json") or {}
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    duration_seconds = _first_number(
        result.get("duration_seconds"),
        metrics.get("duration_seconds"),
        metrics.get("sim_duration_s"),
        metrics.get("simulation_time_s"),
    )
    if duration_seconds is None:
        duration_ms = _first_number(
            result.get("run.final_sim_time_ms"),
            metrics.get("run.final_sim_time_ms"),
            metrics.get("final_sim_time_ms"),
            result.get("run.wall_time_ms"),
            metrics.get("run.wall_time_ms"),
        )
        duration_seconds = duration_ms / 1_000.0 if duration_ms is not None else None
    result["duration_seconds"] = duration_seconds
    result["min_ttc"] = _first_number(
        result.get("min_ttc"),
        metrics.get("min_ttc"),
        metrics.get("min_ttc_s"),
        metrics.get("min_ttc.min_ttc_s"),
    )
    result["collision"] = bool(result.get("collision") or result.get("has_collision"))
    result["parameters"] = result.get("params") or {}
    scenario = str(result.get("scenario_id") or "")
    digits = "".join(character for character in scenario if character.isdigit())
    if digits:
        result["iteration"] = int(digits)
    return result


def _read_trace(path: Path, *, maximum_points: int) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = [
            {
                (key or "").strip(): _coerce_csv_value((value or "").strip())
                for key, value in row.items()
            }
            for row in csv.DictReader(handle, skipinitialspace=True)
        ]
    if len(rows) <= maximum_points:
        return rows
    selected = {0, len(rows) - 1}
    numeric_columns = {
        key
        for row in rows
        for key, value in row.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    for key in sorted(numeric_columns):
        candidates = [
            (index, float(row[key]))
            for index, row in enumerate(rows)
            if isinstance(row.get(key), (int, float)) and not isinstance(row.get(key), bool)
        ]
        if candidates:
            selected.add(min(candidates, key=lambda item: item[1])[0])
            selected.add(max(candidates, key=lambda item: item[1])[0])
        if len(selected) >= maximum_points:
            break
    remaining = maximum_points - len(selected)
    if remaining > 0:
        step = (len(rows) - 1) / max(1, remaining + 1)
        selected.update(round(step * index) for index in range(1, remaining + 1))
    indices = sorted(selected)[:maximum_points]
    if len(rows) - 1 not in indices:
        indices[-1] = len(rows) - 1
    return [rows[index] for index in sorted(set(indices))]


def _coerce_csv_value(value: str) -> Any:
    if value == "":
        return None
    lowered = value.casefold()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        number = float(value)
    except ValueError:
        return value
    return int(number) if number.is_integer() else number


def _trace_point(row: dict[str, Any]) -> dict[str, Any]:
    sim_time = _first_number(row.get("sim_time_ms"))
    time_value = sim_time / 1000.0 if sim_time is not None else _first_number(row.get("time"), 0)
    ttc_values = [
        float(value)
        for key, value in row.items()
        if key.casefold().endswith("ttc_s")
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and float(value) >= 0
    ]
    values = {
        key: value
        for key, value in row.items()
        if value is not None
        and not key.casefold().endswith("_json")
        and isinstance(value, (str, int, float, bool))
        and (not isinstance(value, str) or len(value) <= 256)
    }
    payload = row.get("payload_json")
    if isinstance(payload, str):
        try:
            parsed_payload = json.loads(payload)
        except json.JSONDecodeError:
            parsed_payload = None
        if isinstance(parsed_payload, dict):
            values.update(
                {
                    str(key): value
                    for key, value in parsed_payload.items()
                    if value is None or isinstance(value, (str, int, float, bool))
                }
            )
    return {
        "time": float(time_value or 0),
        "x": _first_number(row.get("x"), row.get("ego.x")),
        "y": _first_number(row.get("y"), row.get("ego.y")),
        "yaw": _first_number(row.get("yaw"), row.get("ego.yaw")),
        "speed": _first_number(row.get("speed"), row.get("ego.speed")),
        "ttc": min(ttc_values) if ttc_values else None,
        "throttle": _first_number(row.get("throttle"), row.get("accelerator")),
        "brake": _first_number(row.get("brake")),
        "steer": _first_number(row.get("steer"), row.get("steering")),
        "acceleration": _first_number(row.get("acceleration"), row.get("ego.acceleration")),
        "yaw_rate": _first_number(row.get("yaw_rate"), row.get("ego.yaw_rate")),
        "values": values,
    }


def _geometry_point(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "agent_id": str(row.get("agent_id") or row.get("actor_id") or "unknown"),
        "entity_name": str(row.get("entity_name") or row.get("agent_name") or row.get("agent_id") or "unknown"),
        "is_ego": _truthy(row.get("is_ego")),
        "shape_type": _optional_text(row.get("shape_type")),
        "length_m": _first_number(row.get("length_m")),
        "width_m": _first_number(row.get("width_m")),
        "height_m": _first_number(row.get("height_m")),
        "reference_point": str(row.get("reference_point") or "unknown"),
        "center_offset_x": _first_number(row.get("center_offset_x")),
        "center_offset_y": _first_number(row.get("center_offset_y")),
        "center_offset_z": _first_number(row.get("center_offset_z")),
        "roll_offset": _first_number(row.get("roll_offset")),
        "pitch_offset": _first_number(row.get("pitch_offset")),
        "yaw_offset": _first_number(row.get("yaw_offset")),
        "footprint_json": _try_json(row.get("footprint_json")),
        "source": str(row.get("source") or "recorded"),
    }


def _event_point(row: dict[str, Any], *, fallback_type: str) -> dict[str, Any]:
    point = _trace_point(row)
    event_type = str(row.get("event_type") or row.get("type") or fallback_type)
    return {
        "time": point["time"],
        "type": event_type,
        "label": str(row.get("label") or row.get("description") or event_type),
        "severity": str(row.get("severity") or ("error" if "collision" in event_type else "info")),
        "x": point.get("x"),
        "y": point.get("y"),
        "details": {
            key: value
            for key, value in row.items()
            if value is not None and not key.casefold().endswith("_json")
        },
    }


def _first_number(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def _truthy(value: Any) -> bool:
    return value is True or str(value).casefold() in {"true", "1", "yes"}


def _artifact_row(path: Path, root: Path, identifier: str) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    suffix = path.suffix.lower()
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    url = f"/api/v1/reports/{identifier}/artifacts/{relative}"
    if suffix in {".mp4", ".webm"}:
        kind = "video"
    elif suffix == ".gif":
        kind = "animation"
    else:
        kind = "image"
    parts = {part.casefold() for part in path.parts}
    source = (
        "derived"
        if parts & {"figures", "exports", "representative_cases"}
        or any(token in path.stem.casefold() for token in ("schematic", "animation"))
        else "recorded"
    )
    return {
        "id": hashlib.sha256(relative.encode()).hexdigest()[:20],
        "path": relative,
        "name": path.name,
        "format": suffix.lstrip("."),
        "media_type": media_type,
        "mime_type": media_type,
        "kind": kind,
        "source": source,
        "size": path.stat().st_size,
        "download_url": url,
        "url": url,
        **({"thumbnail_url": url} if kind == "image" else {}),
    }
