from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from simcore.sampler import create_sampler, load_parameter_space
from simcore.sampler.loader import load_sampler_spec, resolve_sampler_source

from .models import EXPLICIT_SAMPLE_FILE_NAME, ExportError, ExportResult, SourcePathMode
from .output import (
    build_summary,
    default_output_dir,
    default_zip_path,
    prepare_output_dir,
    prepare_zip_path,
    write_export_yaml,
    zip_output_dir,
)
from .scenario import (
    load_export_mapping_file,
    resolve_scenario_assets,
    runner_scenario_path,
    scenario_base_from_path,
)
from .sharding import collect_samples, sample_to_dict, split_samples, validate_split_args


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
    validate_split_args(shard_size=shard_size, num_shards=num_shards)

    runner_spec: dict[str, Any] | None = None
    if runner_spec_path is not None:
        runner_spec = load_export_mapping_file(runner_spec_path, label="runner spec")
        sampler_runtime_spec = runner_spec.get("sampler")
        if sampler_runtime_spec is None:
            raise ExportError("runner spec must contain sampler")
        if not isinstance(sampler_runtime_spec, dict):
            raise ExportError("runner spec sampler must be a mapping/object")
        scenario_path = runner_scenario_path(runner_spec, runner_spec_path)
    elif sampler_spec_path is not None:
        sampler_runtime_spec = load_export_mapping_file(sampler_spec_path, label="sampler spec")
        if scenario_path is None:
            raise ExportError("--scenario-path is required when --sampler-spec is used")
    else:
        raise ExportError("either runner_spec_path or sampler_spec_path is required")

    scenario_base = scenario_base_from_path(scenario_path) if scenario_path is not None else None
    scenario_assets = resolve_scenario_assets(
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

    samples = collect_samples(sampler)
    shards = split_samples(samples, shard_size=shard_size, num_shards=num_shards)
    output_dir = output_dir or default_output_dir(
        scenario_name=scenario_assets.name,
        sampler_name=str(sampler_runtime_spec.get("name")),
        total_samples=len(samples),
    )
    if create_zip and zip_path is None:
        zip_path = default_zip_path(output_dir)

    output_dir = output_dir.expanduser()
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
                "bundle_path": _manifest_path(bundle_dir, output_dir, source_path_mode),
                "scenario_file_path": _manifest_path(xosc_path, output_dir, source_path_mode),
                "sample_file_path": _manifest_path(explicit_path, output_dir, source_path_mode),
                "spec_file_path": _manifest_path(spec_path, output_dir, source_path_mode),
                "stop_conditions_file_path": _manifest_path(
                    stop_conditions_path, output_dir, source_path_mode
                ),
                "first_sample_id": shard_samples[0].id if shard_samples else None,
                "last_sample_id": shard_samples[-1].id if shard_samples else None,
            }
        )

    if not dry_run:
        prepare_output_dir(output_dir, overwrite=overwrite)
        if create_zip:
            assert zip_path is not None
            prepare_zip_path(zip_path, overwrite=overwrite)
        for shard_entry, shard_samples in zip(shard_entries, shards, strict=True):
            bundle_dir = output_dir / (
                f"{scenario_assets.name}-{sampler_runtime_spec.get('name')}"
                f"{shard_entry['bundle_id']}"
            )
            bundle_dir.mkdir()
            shutil.copy2(scenario_assets.xosc_path, bundle_dir / f"{scenario_assets.name}.xosc")
            shutil.copy2(scenario_assets.spec_path, bundle_dir / "spec.yaml")
            shutil.copy2(
                scenario_assets.stop_conditions_path,
                bundle_dir / "stop_conditions.yaml",
            )
            write_export_yaml(
                bundle_dir / EXPLICIT_SAMPLE_FILE_NAME,
                {"samples": [sample_to_dict(sample) for sample in shard_samples]},
            )

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_path_mode": source_path_mode.value,
        "runner_spec_path": _optional_manifest_path(
            runner_spec_path, output_dir, source_path_mode
        ),
        "sampler_spec_path": _optional_manifest_path(
            sampler_spec_path, output_dir, source_path_mode
        ),
        "scenario_name": scenario_assets.name,
        "scenario_path": _optional_manifest_path(scenario_path, output_dir, source_path_mode),
        "scenario_base": _optional_manifest_path(scenario_base, output_dir, source_path_mode),
        "scenario_xosc_path": _manifest_path(
            scenario_assets.xosc_path, output_dir, source_path_mode
        ),
        "scenario_spec_path": _manifest_path(
            scenario_assets.spec_path, output_dir, source_path_mode
        ),
        "stop_conditions_path": _manifest_path(
            scenario_assets.stop_conditions_path, output_dir, source_path_mode
        ),
        "sampler_name": sampler_runtime_spec.get("name"),
        "sampler_config_path": sampler_runtime_spec.get("config_path"),
        "source_path": _manifest_path(source_path, output_dir, source_path_mode),
        "source_type": source_type,
        "total_samples": len(samples),
        "shard_count": len(shards),
        "shard_size": shard_size,
        "num_shards": num_shards,
        "zip_path": str(zip_path) if create_zip and zip_path is not None else None,
        "shards": shard_entries,
    }
    manifest_path = output_dir / "manifest.yaml"
    summary = build_summary(
        manifest,
        output_dir=output_dir,
        manifest_path=manifest_path,
        zip_path=zip_path if create_zip else None,
        dry_run=dry_run,
    )
    if not dry_run:
        write_export_yaml(manifest_path, manifest)
    if create_zip and not dry_run:
        assert zip_path is not None
        zip_output_dir(output_dir, zip_path=zip_path)

    return ExportResult(
        output_dir=output_dir,
        manifest_path=manifest_path,
        total_samples=len(samples),
        shard_count=len(shards),
        zip_path=zip_path if create_zip else None,
        dry_run=dry_run,
        summary=summary,
    )


def _optional_manifest_path(
    path: Path | None,
    output_dir: Path,
    mode: SourcePathMode,
) -> str | None:
    return None if path is None else _manifest_path(path, output_dir, mode)


def _manifest_path(path: Path, output_dir: Path, mode: SourcePathMode) -> str:
    """Serialize paths consistently for either reproducibility or portable bundles.

    Relative paths are relative to the export root (including ``..`` for source
    inputs outside that root), so relocating the root and its nearby source tree
    preserves references. Absolute mode resolves symlinks and user expansions.
    """

    resolved = path.expanduser().resolve()
    if mode is SourcePathMode.ABSOLUTE:
        return str(resolved)
    return os.path.relpath(resolved, start=output_dir.expanduser().resolve())
