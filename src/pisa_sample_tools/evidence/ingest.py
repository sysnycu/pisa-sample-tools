from __future__ import annotations

import json
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from pisa_sample_tools.sample_analyze.utils import coerce_scalar, none_if_empty, read_csv_dicts

from .models import AnalysisSpec, DatasetSpec, EvidenceError, RunRecord

MANIFEST_NAMES = (
    "execution_manifest.yaml",
    "execution_manifest.yml",
    "execution_manifest.json",
    "experiment_manifest.yaml",
    "experiment_manifest.yml",
    "experiment_manifest.json",
)


def load_experiments(
    datasets: list[DatasetSpec],
    spec: AnalysisSpec,
    *,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[RunRecord], list[str]]:
    records: list[RunRecord] = []
    warnings: list[str] = []
    for index, dataset in enumerate(datasets, start=1):
        if progress is not None:
            progress(
                f"loading dataset {index}/{len(datasets)}: "
                f"{dataset.dataset_id} ({dataset.results_path})"
            )
        loaded, path_warnings = load_experiment(dataset, spec, progress=progress)
        records.extend(loaded)
        warnings.extend(path_warnings)
    if not records:
        raise EvidenceError("no completed result.csv files were found")
    if progress is not None:
        progress(f"loaded {len(records)} run(s) from {len(datasets)} dataset(s)")
    return records, warnings


def load_experiment(
    dataset: DatasetSpec,
    spec: AnalysisSpec,
    *,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[RunRecord], list[str]]:
    root = dataset.results_path.expanduser().resolve()
    if not root.is_dir():
        raise EvidenceError(f"results path does not exist or is not a directory: {root}")
    manifest_path, manifest = read_execution_manifest(root)
    experiment_id = dataset.dataset_id
    base_metadata = dict(spec.metadata)
    base_metadata.update(_execution_metadata(manifest))
    base_metadata.update(dataset.metadata)
    for key in ("dt", "seed", "runner_version"):
        if manifest.get(key) not in {None, ""}:
            base_metadata[key] = manifest[key]
    warnings: list[str] = []
    if manifest_path is None:
        warnings.append(
            f"{experiment_id}: no execution_manifest.yaml; execution provenance is incomplete"
        )
    iteration_dirs = sorted(root.glob("iteration_*"), key=_iteration_key)
    if not iteration_dirs and (root / "monitor" / "result.csv").exists():
        iteration_dirs = [root]
    records: list[RunRecord] = []
    for index, iteration_dir in enumerate(iteration_dirs, start=1):
        if progress is not None and index == 1:
            progress(
                f"reading {len(iteration_dirs)} iteration(s) from {dataset.dataset_id}"
            )
        monitor = iteration_dir / "monitor"
        result_path = monitor / "result.csv"
        if not result_path.exists():
            warnings.append(f"{iteration_dir.name}: missing monitor/result.csv")
            continue
        rows = read_csv_dicts(result_path)
        if not rows:
            warnings.append(f"{iteration_dir.name}: empty monitor/result.csv")
            continue
        row = rows[-1]
        params = _json_mapping(row.get("run.params"))
        core_run_fields = {
            "run.status",
            "run.test_outcome",
            "run.stop_condition",
            "run.stop_reason",
            "run.params",
            "run.sample_id",
            "run.concrete_scenario_id",
        }
        metrics = {
            key: coerce_scalar(value)
            for key, value in row.items()
            if key and key not in core_run_fields and value not in {"", None}
        }
        scenario_id = (
            iteration_dir.name.removeprefix("iteration_")
            if iteration_dir.name.startswith("iteration_")
            else str(row.get("run.job_id") or "concrete")
        )
        metadata = dict(base_metadata)
        metadata.update(_mapping(_manifest_run_metadata(manifest, scenario_id)))
        metadata.setdefault("experiment_id", experiment_id)
        records.append(
            RunRecord(
                experiment_id=experiment_id,
                scenario_id=scenario_id,
                sample_id=none_if_empty(
                    row.get("run.sample_id") or row.get("run.concrete_scenario_id")
                ),
                logical_scenario_name=str(
                    metadata.get("logical_scenario_name")
                    or manifest.get("scenario_name")
                    or root.name
                ),
                params=params,
                metadata=metadata,
                status=none_if_empty(row.get("run.status")),
                outcome=none_if_empty(row.get("run.test_outcome")),
                termination_reason=none_if_empty(row.get("run.stop_condition")),
                stop_reason=none_if_empty(row.get("run.stop_reason")),
                metrics=metrics,
                result_path=iteration_dir,
                frame_metrics_path=_existing(monitor / "frame_metrics.csv"),
                agent_states_path=_existing(monitor / "agent_states.csv")
                or _existing(monitor / "agent_state.csv"),
                agent_geometry_path=_existing(monitor / "agent_geometry.csv"),
                collision_events_path=_existing(monitor / "collision_events.csv"),
                scenario_events_path=_existing(monitor / "scenario_events.csv"),
                control_commands_path=_existing(monitor / "control_commands.csv"),
            )
        )
        if progress is not None and (index % 1000 == 0 or index == len(iteration_dirs)):
            progress(
                f"{dataset.dataset_id}: loaded {index}/{len(iteration_dirs)} iteration(s)"
            )
    return records, warnings


def read_trace_rows(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.exists():
        return []
    stat = path.stat()
    return list(_read_trace_rows_cached(str(path), stat.st_mtime_ns, stat.st_size))


@lru_cache(maxsize=256)
def _read_trace_rows_cached(
    path: str, _mtime_ns: int, _size: int
) -> tuple[dict[str, str], ...]:
    return tuple(read_csv_dicts(Path(path)))


def clear_trace_cache() -> None:
    _read_trace_rows_cached.cache_clear()


def read_execution_manifest(root: Path) -> tuple[Path | None, dict[str, Any]]:
    path = next((root / name for name in MANIFEST_NAMES if (root / name).exists()), None)
    return path, _load_manifest(path) if path is not None else {}


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise EvidenceError(f"failed to read experiment manifest {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise EvidenceError(f"experiment manifest must contain a mapping: {path}")
    return data


def _manifest_run_metadata(manifest: dict[str, Any], scenario_id: str) -> dict[str, Any]:
    runs = manifest.get("runs")
    if not isinstance(runs, list):
        return {}
    for run in runs:
        if isinstance(run, dict) and str(run.get("scenario_id")) == scenario_id:
            return _mapping(run.get("metadata"))
    return {}


def _execution_metadata(manifest: dict[str, Any]) -> dict[str, Any]:
    metadata = _mapping(manifest.get("metadata"))
    for key in (
        "execution_id",
        "dt",
        "seed",
        "runner_version",
        "pisa_api_version",
        "created_at",
        "completed_at",
        "runner_spec_sha256",
        "runner_git_sha",
        "scenario_name",
    ):
        if manifest.get(key) not in {None, ""}:
            metadata[key] = manifest[key]
    return metadata


def _json_mapping(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _existing(path: Path) -> Path | None:
    return path if path.exists() else None


def _iteration_key(path: Path) -> tuple[int, str]:
    suffix = path.name.removeprefix("iteration_")
    return (int(suffix), suffix) if suffix.isdigit() else (2**31 - 1, suffix)
