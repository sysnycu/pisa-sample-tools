from __future__ import annotations

from pathlib import Path

from simcore.sampler import create_sampler, load_parameter_space
from simcore.sampler.loader import load_sampler_spec, resolve_sampler_source

from pisa_sample_tools.sample_export import (
    EXPLICIT_SAMPLE_FILE_NAME,
    load_export_mapping_file,
    runner_scenario_path,
    scenario_base_from_path,
)

from .models import AnalyzeError, SampleRecord
from .utils import (
    coerce_scalar,
    iteration_sort_key,
    none_if_empty,
    parse_json_mapping,
    read_csv_dicts,
)


def load_records_from_runner_spec(runner_spec_path: Path) -> list[SampleRecord]:
    runner_spec = load_export_mapping_file(runner_spec_path, label="runner spec")
    sampler_runtime_spec = runner_spec.get("sampler")
    if not isinstance(sampler_runtime_spec, dict):
        raise AnalyzeError("runner spec must contain sampler mapping/object")
    scenario_path = runner_scenario_path(runner_spec, runner_spec_path)
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


def load_records_from_results(results_path: Path) -> list[SampleRecord]:
    results_path = Path(results_path).expanduser()
    if not results_path.is_dir():
        raise AnalyzeError(f"results path does not exist or is not a directory: {results_path}")

    records: list[SampleRecord] = []
    for iteration_dir in sorted(results_path.glob("iteration_*"), key=iteration_sort_key):
        if not iteration_dir.is_dir():
            continue
        sample_id = iteration_dir.name.removeprefix("iteration_")
        result_csv = iteration_dir / "monitor" / "result.csv"
        if not result_csv.exists():
            records.append(SampleRecord(sample_id=sample_id, params={}, result_path=iteration_dir))
            continue
        rows = read_csv_dicts(result_csv)
        if not rows:
            records.append(SampleRecord(sample_id=sample_id, params={}, result_path=iteration_dir))
            continue
        row = rows[-1]
        params = parse_json_mapping(row.get("run.params"))
        metrics = {
            key: coerce_scalar(value)
            for key, value in row.items()
            if key and not key.startswith("run.") and value not in {"", None}
        }
        records.append(
            SampleRecord(
                sample_id=sample_id,
                params=params,
                status=none_if_empty(row.get("run.status")),
                outcome=none_if_empty(row.get("run.test_outcome")),
                stop_condition=none_if_empty(row.get("run.stop_condition")),
                stop_reason=none_if_empty(row.get("run.stop_reason")),
                metrics=metrics,
                result_path=iteration_dir,
            )
        )
    return records


def _find_explicit_sample_file(samples_path: Path) -> Path:
    explicit_file = samples_path / EXPLICIT_SAMPLE_FILE_NAME
    if explicit_file.exists():
        return explicit_file
    return samples_path / "explicit.yaml"


def _load_records_from_manifest(samples_root: Path, manifest_path: Path) -> list[SampleRecord]:
    manifest = load_export_mapping_file(manifest_path, label="sample manifest")
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
    for index, row in enumerate(read_csv_dicts(path), start=1):
        sample_id = row.get("sample_id") or row.get("id") or str(index)
        params: dict[str, object] = {}
        metrics: dict[str, object] = {}
        for key, value in row.items():
            if key is None or value in {None, ""}:
                continue
            if key.startswith("param."):
                params[key.removeprefix("param.")] = coerce_scalar(value)
            elif key.startswith("metric."):
                metrics[key.removeprefix("metric.")] = coerce_scalar(value)
            elif key not in reserved:
                params[key] = coerce_scalar(value)
        records.append(
            SampleRecord(
                sample_id=str(sample_id),
                params=params,
                status=none_if_empty(row.get("status")),
                outcome=none_if_empty(row.get("outcome")),
                stop_condition=none_if_empty(row.get("stop_condition")),
                stop_reason=none_if_empty(row.get("stop_reason")),
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
    data = load_export_mapping_file(path, label="explicit samples")
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
