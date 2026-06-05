from __future__ import annotations

import json
import shutil
import zipfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from simcore.sampler import Sample, create_sampler, load_parameter_space
from simcore.sampler.loader import load_sampler_spec, resolve_sampler_source


class ExportError(ValueError):
    """Raised for user-facing export failures."""


class SourcePathMode(StrEnum):
    ABSOLUTE = "absolute"
    RELATIVE_TO_OUTPUT = "relative-to-output"


EXPLICIT_SAMPLE_FILE_NAME = "explicit_samples.yaml"


@dataclass(frozen=True)
class ExportResult:
    output_dir: Path
    manifest_path: Path
    total_samples: int
    shard_count: int
    zip_path: Path | None = None
    dry_run: bool = False
    summary: dict[str, Any] | None = None


def export_samples(
    *,
    output_dir: Path | None = None,
    runner_spec_path: Path | None = None,
    sampler_spec_path: Path | None = None,
    scenario_path: Path | None = None,
    shard_size: int | None = None,
    num_shards: int | None = None,
    source_path_mode: SourcePathMode = SourcePathMode.ABSOLUTE,
    create_zip: bool = False,
    zip_path: Path | None = None,
    dry_run: bool = False,
    overwrite: bool = False,
) -> ExportResult:
    _validate_split_args(shard_size=shard_size, num_shards=num_shards)

    runner_spec: dict[str, Any] | None = None
    if runner_spec_path is not None:
        runner_spec = _load_mapping_file(runner_spec_path, label="runner spec")
        sampler_runtime_spec = runner_spec.get("sampler")
        if sampler_runtime_spec is None:
            raise ExportError("runner spec must contain sampler")
        if not isinstance(sampler_runtime_spec, dict):
            raise ExportError("runner spec sampler must be a mapping/object")
        scenario_path = _runner_scenario_path(runner_spec, runner_spec_path)
    elif sampler_spec_path is not None:
        sampler_runtime_spec = _load_mapping_file(sampler_spec_path, label="sampler spec")
        if scenario_path is None:
            raise ExportError("--scenario-path is required when --sampler-spec is used")
    else:
        raise ExportError("either runner_spec_path or sampler_spec_path is required")

    scenario_base = scenario_base_from_path(scenario_path) if scenario_path is not None else None
    scenario_assets = _resolve_scenario_assets(
        scenario_base=scenario_base,
        runner_spec=runner_spec,
    )
    try:
        sampler_spec = load_sampler_spec(
            sampler_runtime_spec,
            source_base_path=scenario_base,
        )
        source_path, source_type = resolve_sampler_source(sampler_spec)
        parameter_space = load_parameter_space(source_path, source_type)
        sampler = create_sampler(sampler_spec, parameter_space)
    except Exception as exc:
        raise ExportError(str(exc)) from exc

    samples = _collect_samples(sampler)
    shards = _split_samples(samples, shard_size=shard_size, num_shards=num_shards)
    output_dir = output_dir or _default_output_dir(
        scenario_name=scenario_assets.name,
        sampler_name=str(sampler_runtime_spec.get("name")),
        total_samples=len(samples),
    )
    if create_zip and zip_path is None:
        zip_path = _default_zip_path(output_dir)

    shard_entries: list[dict[str, Any]] = []
    for index, shard_samples in enumerate(shards):
        bundle_id = index + 1
        bundle_dir = output_dir / f"{scenario_assets.name}-{sampler_runtime_spec.get('name')}{bundle_id}"
        xosc_path = bundle_dir / f"{scenario_assets.name}.xosc"
        explicit_path = bundle_dir / EXPLICIT_SAMPLE_FILE_NAME
        spec_path = bundle_dir / "spec.yaml"
        stop_conditions_path = bundle_dir / "stop_conditions.yaml"

        shard_entries.append(
            {
                "index": index,
                "bundle_id": bundle_id,
                "sample_count": len(shard_samples),
                "bundle_path": str(bundle_dir),
                "scenario_file_path": str(xosc_path),
                "sample_file_path": str(explicit_path),
                "spec_file_path": str(spec_path),
                "stop_conditions_file_path": str(stop_conditions_path),
                "first_sample_id": shard_samples[0].id if shard_samples else None,
                "last_sample_id": shard_samples[-1].id if shard_samples else None,
            }
        )

    if not dry_run:
        _prepare_output_dir(output_dir, overwrite=overwrite)
        if create_zip:
            assert zip_path is not None
            _prepare_zip_path(zip_path, overwrite=overwrite)
        for shard_entry, shard_samples in zip(shard_entries, shards, strict=True):
            bundle_dir = Path(shard_entry["bundle_path"])
            bundle_dir.mkdir()
            shutil.copy2(scenario_assets.xosc_path, shard_entry["scenario_file_path"])
            shutil.copy2(scenario_assets.spec_path, shard_entry["spec_file_path"])
            shutil.copy2(
                scenario_assets.stop_conditions_path,
                shard_entry["stop_conditions_file_path"],
            )
            _write_yaml(
                Path(shard_entry["sample_file_path"]),
                {"samples": [_sample_to_dict(sample) for sample in shard_samples]},
            )

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "runner_spec_path": str(runner_spec_path) if runner_spec_path is not None else None,
        "sampler_spec_path": str(sampler_spec_path) if sampler_spec_path is not None else None,
        "scenario_name": scenario_assets.name,
        "scenario_path": str(scenario_path) if scenario_path is not None else None,
        "scenario_base": str(scenario_base) if scenario_base is not None else None,
        "scenario_xosc_path": str(scenario_assets.xosc_path),
        "scenario_spec_path": str(scenario_assets.spec_path),
        "stop_conditions_path": str(scenario_assets.stop_conditions_path),
        "sampler_name": sampler_runtime_spec.get("name"),
        "sampler_config_path": sampler_runtime_spec.get("config_path"),
        "source_path": str(source_path),
        "source_type": source_type,
        "total_samples": len(samples),
        "shard_count": len(shards),
        "shard_size": shard_size,
        "num_shards": num_shards,
        "zip_path": str(zip_path) if create_zip and zip_path is not None else None,
        "shards": shard_entries,
    }
    manifest_path = output_dir / "manifest.yaml"
    summary = _build_summary(
        manifest,
        output_dir=output_dir,
        manifest_path=manifest_path,
        zip_path=zip_path if create_zip else None,
        dry_run=dry_run,
    )
    if not dry_run:
        _write_yaml(manifest_path, manifest)
    if create_zip and not dry_run:
        assert zip_path is not None
        _zip_output_dir(output_dir, zip_path=zip_path)

    return ExportResult(
        output_dir=output_dir,
        manifest_path=manifest_path,
        total_samples=len(samples),
        shard_count=len(shards),
        zip_path=zip_path if create_zip else None,
        dry_run=dry_run,
        summary=summary,
    )


@dataclass(frozen=True)
class ScenarioAssets:
    name: str
    xosc_path: Path
    spec_path: Path
    stop_conditions_path: Path


def scenario_base_from_path(scenario_path: Path) -> Path:
    scenario_path = Path(scenario_path).expanduser()
    if scenario_path.exists():
        if scenario_path.is_dir():
            return scenario_path
        return scenario_path.parent
    if scenario_path.suffix:
        return scenario_path.parent
    return scenario_path


def _default_output_dir(*, scenario_name: str, sampler_name: str, total_samples: int) -> Path:
    return Path("output") / f"{scenario_name}-{sampler_name}-{total_samples}"


def _default_zip_path(output_dir: Path) -> Path:
    return output_dir.with_suffix(".zip")


def _validate_split_args(*, shard_size: int | None, num_shards: int | None) -> None:
    if shard_size is not None and num_shards is not None:
        raise ExportError("shard-size and num-shards are mutually exclusive")
    if shard_size is None and num_shards is None:
        raise ExportError("one of shard-size or num-shards is required")
    if shard_size is not None and shard_size <= 0:
        raise ExportError("shard-size must be greater than 0")
    if num_shards is not None and num_shards <= 0:
        raise ExportError("num-shards must be greater than 0")


def _prepare_output_dir(output_dir: Path, *, overwrite: bool) -> None:
    if output_dir.exists():
        if not output_dir.is_dir():
            raise ExportError(f"output-dir exists and is not a directory: {output_dir}")
        if not overwrite:
            raise ExportError(f"output-dir already exists: {output_dir}")
        if not any(output_dir.iterdir()):
            return
        _clear_previous_output(output_dir)
    else:
        output_dir.mkdir(parents=True)


def _clear_previous_output(output_dir: Path) -> None:
    manifest_path = output_dir / "manifest.yaml"
    if not manifest_path.exists():
        raise ExportError(
            "output-dir already exists and is not empty, but no manifest.yaml was found; "
            "refusing to overwrite a directory not created by this tool"
        )

    manifest = _load_mapping_file(manifest_path, label="existing output manifest")
    for shard in manifest.get("shards", []):
        if not isinstance(shard, dict):
            continue
        bundle_path = shard.get("bundle_path")
        if bundle_path is not None:
            _remove_path_if_under_output(Path(bundle_path), output_dir)
        for key in (
            "sample_file_path",
            "sampler_config_path",
            "scenario_file_path",
            "spec_file_path",
            "stop_conditions_file_path",
        ):
            raw_path = shard.get(key)
            if raw_path is not None:
                _remove_path_if_under_output(Path(raw_path), output_dir)
    manifest_path.unlink()
    _remove_empty_dirs(output_dir)
    if any(output_dir.iterdir()):
        raise ExportError(
            "output-dir still contains files after clearing manifest-listed outputs; "
            "refusing to overwrite unrelated files"
        )


def _remove_path_if_under_output(path: Path, output_dir: Path) -> None:
    output_root = output_dir.resolve()
    target = path.resolve()
    try:
        target.relative_to(output_root)
    except ValueError:
        return
    if not target.exists():
        return
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def _remove_empty_dirs(output_dir: Path) -> None:
    for path in sorted((path for path in output_dir.rglob("*") if path.is_dir()), reverse=True):
        with suppress(OSError):
            path.rmdir()


def _prepare_zip_path(zip_path: Path, *, overwrite: bool) -> None:
    if zip_path.exists():
        if not overwrite:
            raise ExportError(f"zip path already exists: {zip_path}")
        if zip_path.is_dir():
            raise ExportError(f"zip path exists and is a directory: {zip_path}")
        zip_path.unlink()
    zip_path.parent.mkdir(parents=True, exist_ok=True)


def _load_mapping_file(path: Path, *, label: str) -> dict[str, Any]:
    path = Path(path).expanduser()
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ExportError(f"failed to read {label} {path}: {exc}") from exc
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ExportError(f"failed to parse {label} {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ExportError(f"{label} must contain a mapping/object")
    return data


def _runner_scenario_path(runner_spec: dict[str, Any], runner_spec_path: Path) -> Path:
    scenario = runner_spec.get("scenario")
    if not isinstance(scenario, dict):
        raise ExportError("runner spec must contain scenario.scenario_path")
    raw_path = scenario.get("scenario_path")
    if not raw_path:
        raise ExportError("runner spec must contain scenario.scenario_path")
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return Path(runner_spec_path).expanduser().parent / path


def _resolve_scenario_assets(
    *,
    scenario_base: Path | None,
    runner_spec: dict[str, Any] | None,
) -> ScenarioAssets:
    if scenario_base is None:
        raise ExportError("scenario path is required to build output bundles")

    candidate_dirs = [scenario_base]
    stop_conditions_config_path = _runner_stop_conditions_path(runner_spec)
    if stop_conditions_config_path is not None:
        candidate_dirs.append(stop_conditions_config_path.parent)

    scenario_name = _resolve_scenario_name(runner_spec, candidate_dirs)
    xosc_path = _find_required_file(
        candidate_dirs,
        file_names=[f"{scenario_name}.xosc"],
        description=f"{scenario_name}.xosc",
    )
    spec_path = _find_required_file(candidate_dirs, file_names=["spec.yaml"], description="spec.yaml")

    if stop_conditions_config_path is not None and stop_conditions_config_path.exists():
        stop_conditions_path = stop_conditions_config_path
    else:
        stop_conditions_path = _find_required_file(
            candidate_dirs,
            file_names=["stop_conditions.yaml"],
            description="stop_conditions.yaml",
        )

    return ScenarioAssets(
        name=scenario_name,
        xosc_path=xosc_path,
        spec_path=spec_path,
        stop_conditions_path=stop_conditions_path,
    )


def _runner_stop_conditions_path(runner_spec: dict[str, Any] | None) -> Path | None:
    if runner_spec is None:
        return None
    scenario = runner_spec.get("scenario")
    if not isinstance(scenario, dict):
        return None
    raw_path = scenario.get("stop_condition_config_path")
    if not raw_path:
        return None
    return Path(raw_path).expanduser()


def _resolve_scenario_name(
    runner_spec: dict[str, Any] | None,
    candidate_dirs: list[Path],
) -> str:
    runner_name = _runner_scenario_name(runner_spec)
    if runner_name:
        return runner_name

    for directory in candidate_dirs:
        spec_path = directory / "spec.yaml"
        if spec_path.exists():
            spec = _load_mapping_file(spec_path, label="scenario spec")
            raw_name = spec.get("scenario_name")
            if raw_name:
                return str(raw_name)

    xosc_paths = sorted({path for directory in candidate_dirs for path in directory.glob("*.xosc")})
    if len(xosc_paths) == 1:
        return xosc_paths[0].stem
    if not xosc_paths:
        raise ExportError("could not infer scenario name because no .xosc file was found")
    names = ", ".join(path.name for path in xosc_paths)
    raise ExportError(f"could not infer scenario name because multiple .xosc files were found: {names}")


def _runner_scenario_name(runner_spec: dict[str, Any] | None) -> str | None:
    if runner_spec is None:
        return None
    scenario = runner_spec.get("scenario")
    if isinstance(scenario, dict):
        raw_name = scenario.get("title") or scenario.get("name")
        if raw_name:
            return str(raw_name)
    simulator = runner_spec.get("simulator")
    if isinstance(simulator, dict):
        simulator_scenario = simulator.get("scenario")
        if isinstance(simulator_scenario, dict) and simulator_scenario.get("name"):
            return str(simulator_scenario["name"])
    return None


def _find_required_file(
    candidate_dirs: list[Path],
    *,
    file_names: list[str],
    description: str,
) -> Path:
    searched: list[str] = []
    for directory in candidate_dirs:
        for file_name in file_names:
            path = directory / file_name
            searched.append(str(path))
            if path.exists() and path.is_file():
                return path
    raise ExportError(f"required scenario file not found: {description}; searched: {searched}")


def _collect_samples(sampler: Any) -> list[Sample]:
    samples: list[Sample] = []
    index = 1
    while True:
        sample = sampler.next()
        if sample is None:
            return samples
        sample_id = str(sample.id) if sample.id is not None else str(index)
        samples.append(Sample(id=sample_id, params=sample.params, metadata=sample.metadata))
        index += 1


def _split_samples(
    samples: list[Sample],
    *,
    shard_size: int | None,
    num_shards: int | None,
) -> list[list[Sample]]:
    if shard_size is not None:
        return [samples[index : index + shard_size] for index in range(0, len(samples), shard_size)]

    assert num_shards is not None
    if not samples:
        return []
    base_size, remainder = divmod(len(samples), num_shards)
    shards: list[list[Sample]] = []
    start = 0
    for index in range(num_shards):
        current_size = base_size + (1 if index < remainder else 0)
        if current_size == 0:
            break
        end = start + current_size
        shards.append(samples[start:end])
        start = end
    return shards


def _sample_to_dict(sample: Sample) -> dict[str, Any]:
    return {
        "id": sample.id,
        "params": sample.sim_params,
    }


def _config_source_path(
    sample_path: Path,
    *,
    config_path: Path,
    mode: SourcePathMode,
) -> str:
    if mode == SourcePathMode.ABSOLUTE:
        return str(sample_path.resolve())
    if mode == SourcePathMode.RELATIVE_TO_OUTPUT:
        return str(Path("..") / sample_path.name)
    raise ExportError(f"unknown source-path-mode: {mode}")


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )


def _build_summary(
    manifest: dict[str, Any],
    *,
    output_dir: Path,
    manifest_path: Path,
    zip_path: Path | None,
    dry_run: bool,
) -> dict[str, Any]:
    return {
        "dry_run": dry_run,
        "scenario_name": manifest["scenario_name"],
        "sampler_name": manifest["sampler_name"],
        "source_path": manifest["source_path"],
        "source_type": manifest["source_type"],
        "total_samples": manifest["total_samples"],
        "shard_count": manifest["shard_count"],
        "shard_size": manifest["shard_size"],
        "num_shards": manifest["num_shards"],
        "output_dir": str(output_dir),
        "manifest_path": str(manifest_path),
        "zip_path": str(zip_path) if zip_path is not None else None,
        "scenario_xosc_path": manifest["scenario_xosc_path"],
        "scenario_spec_path": manifest["scenario_spec_path"],
        "stop_conditions_path": manifest["stop_conditions_path"],
        "shards": [
            {
                "bundle_id": shard["bundle_id"],
                "sample_count": shard["sample_count"],
                "bundle_path": shard["bundle_path"],
                "first_sample_id": shard["first_sample_id"],
                "last_sample_id": shard["last_sample_id"],
            }
            for shard in manifest["shards"]
        ],
    }


def _zip_output_dir(output_dir: Path, *, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output_dir.rglob("*")):
            if not path.is_file() or path.name == "manifest.yaml":
                continue
            archive.write(path, arcname=path.relative_to(output_dir))
