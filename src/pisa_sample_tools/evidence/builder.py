from __future__ import annotations

import hashlib
import json
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from pisa_sample_tools.common.yaml import write_yaml

from .campaign import load_campaign
from .ingest import execution_component_metadata, load_experiment, read_execution_manifest
from .models import DatasetSpec, EvidenceError
from .report_version import REPORT_BUILD_VERSION
from .spec import load_analysis_spec, spec_to_dict
from .statistics import apply_derived_parameters, normalized_outcome
from .validation import validate_runs


def browse_path(path: Path, *, kind: str = "directory") -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise EvidenceError(f"directory does not exist: {resolved}")
    entries = []
    for child in sorted(resolved.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if child.name.startswith("."):
            continue
        if child.is_dir() or kind == "any" or child.suffix.lower() in {".yaml", ".yml", ".xodr"}:
            entries.append(
                {
                    "name": child.name,
                    "path": str(child),
                    "is_directory": child.is_dir(),
                    "is_experiment": is_experiment_root(child) if child.is_dir() else False,
                    "is_report": is_report_bundle(child) if child.is_dir() else False,
                }
            )
    return {
        "path": str(resolved),
        "parent": str(resolved.parent),
        "is_experiment": is_experiment_root(resolved),
        "is_report": is_report_bundle(resolved),
        "entries": entries,
    }


def preview_experiment(path: Path, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    manifest_path, manifest = read_execution_manifest(resolved)
    merged = dict(manifest.get("metadata") or {})
    for key in ("map_name", "scenario_name", "runner_version", "seed"):
        if manifest.get(key) not in {None, ""}:
            merged[key] = manifest[key]
    merged.update(execution_component_metadata(manifest))
    merged.update(metadata or {})
    dataset = DatasetSpec(resolved.name, resolved, merged)
    runs, warnings = load_experiment(dataset, load_analysis_spec(None))
    if not runs:
        raise EvidenceError(f"no completed runs found in {resolved}")
    parameters = sorted({key for run in runs for key in run.params})
    metrics = sorted({key for run in runs for key in run.metrics})
    samples = [
        {
            "logical_scenario_name": run.logical_scenario_name,
            "sample_id": run.sample_id,
            "scenario_id": run.scenario_id,
            "params": run.params,
        }
        for run in runs
    ]
    outcomes = Counter(normalized_outcome(run, load_analysis_spec(None)) for run in runs)
    first = runs[0]
    simulator = _component_preview(manifest, "simulator", merged.get("simulator_name"))
    av = _component_preview(manifest, "av", merged.get("av_name"))
    xodr_path = resolve_xodr_path(merged.get("xodr_path"), merged.get("map_name"))
    return {
        "dataset_id": dataset.dataset_id,
        "results": str(resolved),
        "manifest": str(manifest_path) if manifest_path else None,
        "scenario_name": first.logical_scenario_name,
        "map_name": first.metadata.get("map_name") or merged.get("map_name"),
        "simulator": simulator["component_name"],
        "av": av["component_name"],
        "simulator_component": simulator,
        "av_component": av,
        "sampler": first.metadata.get("sampler_name"),
        "xodr_path": str(xodr_path) if xodr_path else None,
        "runner_version": first.metadata.get("runner_version"),
        "run_count": len(runs),
        "parameters": parameters,
        "metrics": metrics,
        "outcomes": dict(sorted(outcomes.items())),
        "samples": samples,
        "warnings": warnings,
        "trace_coverage": {
            "frame_metrics": sum(run.frame_metrics_path is not None for run in runs),
            "agent_states": sum(run.agent_states_path is not None for run in runs),
            "controls": sum(run.control_commands_path is not None for run in runs),
        },
    }


def is_experiment_root(path: Path) -> bool:
    return bool(
        path.is_dir()
        and (path / "execution_manifest.yaml").is_file()
        and (path / "summary.csv").is_file()
        and any(child.is_dir() and child.name.startswith("iteration_") for child in path.iterdir())
    )


def is_report_bundle(path: Path) -> bool:
    manifest_path = path / "manifest.yaml"
    if not (
        path.is_dir()
        and manifest_path.is_file()
        and (path / "report" / "analysis_report.html").is_file()
    ):
        return False
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    return manifest.get("tool") == "pisa-analysis-tools"


def scan_reports(root: Path) -> dict[str, Any]:
    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        raise EvidenceError(f"report directory does not exist: {resolved}")
    reports, warnings = [], []
    candidates = [resolved] if is_report_bundle(resolved) else []
    try:
        candidates.extend(
            manifest.parent
            for manifest in resolved.rglob("manifest.yaml")
            if not any(part.startswith(".") for part in manifest.relative_to(resolved).parts)
        )
    except OSError as exc:
        warnings.append(str(exc))
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen or not is_report_bundle(candidate):
            continue
        seen.add(candidate)
        try:
            preview = preview_report(candidate)
        except (EvidenceError, OSError, json.JSONDecodeError) as exc:
            warnings.append(f"{candidate}: {exc}")
            continue
        if preview:
            reports.append(preview)
    reports.sort(key=lambda item: str(item.get("generated_at") or ""), reverse=True)
    return {"root": str(resolved), "reports": reports, "warnings": warnings}


def preview_report(path: Path) -> dict[str, Any] | None:
    manifest_path = path / "manifest.yaml"
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise EvidenceError(f"invalid report manifest: {exc}") from exc
    if manifest.get("tool") != "pisa-analysis-tools":
        return None
    data_path = path / "report" / "analysis_data.json"
    data = json.loads(data_path.read_text(encoding="utf-8")) if data_path.is_file() else {}
    experiments = data.get("experiments") or []
    summary = data.get("summary") or {}
    outcomes = (
        data.get("experiment_summaries", {}).get("outcomes")
        if data.get("report_mode") == "compare"
        else summary.get("outcomes")
    ) or []
    return {
        "report_id": hashlib.sha256(str(path).encode()).hexdigest()[:20],
        "name": path.name,
        "path": str(path),
        "generated_at": manifest.get("generated_at"),
        "run_count": manifest.get("run_count", summary.get("run_count", 0)),
        "warning_count": manifest.get("warning_count", summary.get("warning_count", 0)),
        "experiment_count": summary.get("experiment_count", len(experiments)),
        "parameter_count": summary.get("parameter_count", 0),
        "report_mode": data.get("report_mode"),
        "report_build_version": int(manifest.get("report_build_version") or 0),
        "latest_report_build_version": REPORT_BUILD_VERSION,
        "update_available": int(manifest.get("report_build_version") or 0)
        < REPORT_BUILD_VERSION,
        "experiments": experiments,
        "outcomes": outcomes,
        "sensitivity_generated": bool(data.get("sensitivity", {}).get("generated")),
        "report_path": str(path / "report" / "analysis_report.html"),
    }


def inspect_output(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return {"path": str(resolved), "state": "available", "can_overwrite": False}
    if not resolved.is_dir():
        return {"path": str(resolved), "state": "not_directory", "can_overwrite": False}
    if not any(resolved.iterdir()):
        return {"path": str(resolved), "state": "empty", "can_overwrite": False}
    if is_report_bundle(resolved):
        preview = preview_report(resolved)
        return {"path": str(resolved), "state": "pisa_report", "can_overwrite": True, "report": preview}
    return {"path": str(resolved), "state": "non_pisa_nonempty", "can_overwrite": False}


def resolve_xodr_path(value: Any, map_name: Any = None) -> Path | None:
    if value in {None, ""}:
        return None
    path = Path(str(value)).expanduser().resolve()
    if path.is_file() and path.suffix.lower() == ".xodr":
        return path
    if not path.is_dir():
        return None
    files = sorted(path.glob("*.xodr"))
    named = [item for item in files if item.stem == str(map_name)]
    if len(named) == 1:
        return named[0]
    return files[0] if len(files) == 1 else None


def _component_preview(manifest: dict[str, Any], kind: str, fallback: Any) -> dict[str, Any]:
    descriptor = manifest.get("components", {}).get(kind, {}) or {}
    wrapper = descriptor.get("wrapper", {}) or {}
    component = descriptor.get("component", {}) or {}
    return {
        "component_name": component.get("name") or fallback,
        "wrapper_name": wrapper.get("name"),
        "wrapper_version": wrapper.get("version"),
        "metadata": component.get("metadata") or {},
    }


def preview_campaign(path: Path) -> dict[str, Any]:
    datasets = load_campaign(path)
    experiments = []
    for item in datasets:
        preview = preview_experiment(item.results_path, item.metadata)
        preview["dataset_id"] = item.dataset_id
        experiments.append(preview)
    return {
        "path": str(path.expanduser().resolve()),
        "experiments": experiments,
        "compatibility": compare_experiments(experiments),
    }


def compare_experiments(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    if len(experiments) < 2:
        return {"compatible": True, "errors": [], "component_differences": {}}
    baseline = experiments[0]
    baseline_samples = _sample_map(baseline)
    errors: list[dict[str, Any]] = []
    dataset_ids = [str(item.get("dataset_id") or "") for item in experiments]
    duplicates = sorted(key for key, count in Counter(dataset_ids).items() if key and count > 1)
    if duplicates:
        errors.append({"duplicate_dataset_ids": duplicates})
    for field in ("scenario_name", "map_name", "sampler", "xodr_path"):
        values = {json.dumps(item.get(field), sort_keys=True) for item in experiments}
        if len(values) > 1:
            errors.append(
                {
                    "shared_field_mismatch": field,
                    "values": {
                        str(item.get("dataset_id")): item.get(field) for item in experiments
                    },
                }
            )
    missing_baseline_ids = sorted(
        key for key, value in baseline_samples.items() if value == "missing-sample-id"
    )
    if missing_baseline_ids:
        errors.append(
            {"dataset_id": baseline.get("dataset_id"), "missing_sample_ids": missing_baseline_ids}
        )
    for candidate in experiments[1:]:
        current = _sample_map(candidate)
        missing_ids = sorted(set(baseline_samples) - set(current))
        extra_ids = sorted(set(current) - set(baseline_samples))
        mismatched = sorted(
            sample_id
            for sample_id in set(baseline_samples) & set(current)
            if baseline_samples[sample_id] != current[sample_id]
        )
        if missing_ids or extra_ids or mismatched:
            errors.append(
                {
                    "dataset_id": candidate.get("dataset_id"),
                    "missing_samples": missing_ids,
                    "extra_samples": extra_ids,
                    "parameter_mismatches": mismatched,
                }
            )
        missing_current_ids = sorted(
            key for key, value in current.items() if value == "missing-sample-id"
        )
        if missing_current_ids:
            errors.append(
                {
                    "dataset_id": candidate.get("dataset_id"),
                    "missing_sample_ids": missing_current_ids,
                }
            )
    fields = (
        "scenario_name",
        "map_name",
        "simulator",
        "av",
        "sampler",
        "xodr_path",
        "runner_version",
    )
    differences = {
        field: {str(item.get("dataset_id")): item.get(field) for item in experiments}
        for field in fields
        if len({json.dumps(item.get(field), sort_keys=True) for item in experiments}) > 1
    }
    return {"compatible": not errors, "errors": errors, "component_differences": differences}


def default_spec() -> dict[str, Any]:
    return spec_to_dict(load_analysis_spec(None))


def preview_spec(path: Path) -> dict[str, Any]:
    return spec_to_dict(load_analysis_spec(path))


def validate_builder_request(
    experiments: list[dict[str, Any]], spec_data: dict[str, Any], *, deep: bool = False
) -> dict[str, Any]:
    compatibility = compare_experiments(experiments)
    with tempfile.TemporaryDirectory(prefix="pisa-builder-") as temporary:
        spec_path = Path(temporary) / "analysis_spec.yaml"
        write_yaml(spec_path, spec_data)
        spec = load_analysis_spec(spec_path)
    runs = []
    warnings = []
    for experiment in experiments:
        metadata = _campaign_metadata(experiment)
        loaded, item_warnings = load_experiment(
            DatasetSpec(str(experiment["dataset_id"]), Path(experiment["results"]), metadata), spec
        )
        runs.extend(loaded)
        warnings.extend(item_warnings)
    runs = apply_derived_parameters(runs, spec)
    findings = validate_runs(runs, spec, deep=deep)
    return {
        "valid": compatibility["compatible"]
        and not any(item.severity == "error" for item in findings),
        "compatibility": compatibility,
        "findings": [item.as_row() for item in findings],
        "warnings": warnings,
        "run_count": len(runs),
    }


def campaign_document(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 1,
        "datasets": [
            {
                "id": item["dataset_id"],
                "results": item["results"],
                "logical_scenario_name": item.get("logical_scenario_name")
                or item.get("scenario_name"),
                "labels": {
                    "simulator": item.get("simulator"),
                    "av": item.get("av"),
                    "sampler": item.get("sampler"),
                },
                "grouping": {"repeat_id": item.get("repeat_id"), "seed": item.get("seed")},
                **({"xodr_path": item["xodr_path"]} if item.get("xodr_path") else {}),
            }
            for item in experiments
        ],
    }


def export_yaml(path: Path, data: dict[str, Any]) -> str:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(resolved, _drop_empty(data))
    return str(resolved)


def _sample_map(experiment: dict[str, Any]) -> dict[str, str]:
    result = {}
    for sample in experiment.get("samples") or []:
        sample_id = sample.get("sample_id")
        if sample_id in {None, ""}:
            result[f"<missing:{sample.get('scenario_id')}>"] = "missing-sample-id"
        else:
            result[f"{sample.get('logical_scenario_name')}:{sample_id}"] = json.dumps(
                sample.get("params") or {}, sort_keys=True, separators=(",", ":")
            )
    return result


def _campaign_metadata(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "logical_scenario_name": item.get("logical_scenario_name")
            or item.get("scenario_name"),
            "simulator_name": item.get("simulator"),
            "av_name": item.get("av"),
            "sampler_name": item.get("sampler"),
            "repeat_id": item.get("repeat_id"),
            "seed": item.get("seed"),
            "xodr_path": item.get("xodr_path"),
            "map_name": item.get("map_name"),
        }.items()
        if value not in {None, ""}
    }


def _drop_empty(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _drop_empty(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_drop_empty(item) for item in value]
    return value
