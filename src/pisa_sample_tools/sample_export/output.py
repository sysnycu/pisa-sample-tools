from __future__ import annotations

import shutil
import zipfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from pisa_sample_tools.common.yaml import write_yaml

from .models import ExportError
from .scenario import load_export_mapping_file


def default_output_dir(*, scenario_name: str, sampler_name: str, total_samples: int) -> Path:
    return Path("output") / f"{scenario_name}-{sampler_name}-{total_samples}"


def default_zip_path(output_dir: Path) -> Path:
    return output_dir.with_suffix(".zip")


def prepare_output_dir(output_dir: Path, *, overwrite: bool) -> None:
    if output_dir.exists():
        if not output_dir.is_dir():
            raise ExportError(f"output-dir exists and is not a directory: {output_dir}")
        if not overwrite:
            raise ExportError(f"output-dir already exists: {output_dir}")
        if not any(output_dir.iterdir()):
            return
        clear_previous_output(output_dir)
    else:
        output_dir.mkdir(parents=True)


def clear_previous_output(output_dir: Path) -> None:
    manifest_path = output_dir / "manifest.yaml"
    if not manifest_path.exists():
        raise ExportError(
            "output-dir already exists and is not empty, but no manifest.yaml was found; "
            "refusing to overwrite a directory not created by this tool"
        )

    manifest = load_export_mapping_file(manifest_path, label="existing output manifest")
    for shard in manifest.get("shards", []):
        if not isinstance(shard, dict):
            continue
        bundle_path = shard.get("bundle_path")
        if bundle_path is not None:
            remove_path_if_under_output(Path(bundle_path), output_dir)
        for key in (
            "sample_file_path",
            "sampler_config_path",
            "scenario_file_path",
            "spec_file_path",
            "stop_conditions_file_path",
        ):
            raw_path = shard.get(key)
            if raw_path is not None:
                remove_path_if_under_output(Path(raw_path), output_dir)
    manifest_path.unlink()
    remove_empty_dirs(output_dir)
    if any(output_dir.iterdir()):
        raise ExportError(
            "output-dir still contains files after clearing manifest-listed outputs; "
            "refusing to overwrite unrelated files"
        )


def remove_path_if_under_output(path: Path, output_dir: Path) -> None:
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


def remove_empty_dirs(output_dir: Path) -> None:
    for path in sorted((path for path in output_dir.rglob("*") if path.is_dir()), reverse=True):
        with suppress(OSError):
            path.rmdir()


def prepare_zip_path(zip_path: Path, *, overwrite: bool) -> None:
    if zip_path.exists():
        if not overwrite:
            raise ExportError(f"zip path already exists: {zip_path}")
        if zip_path.is_dir():
            raise ExportError(f"zip path exists and is a directory: {zip_path}")
        zip_path.unlink()
    zip_path.parent.mkdir(parents=True, exist_ok=True)


def build_summary(
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


def zip_output_dir(output_dir: Path, *, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output_dir.rglob("*")):
            if not path.is_file() or path.name == "manifest.yaml":
                continue
            archive.write(path, arcname=path.relative_to(output_dir))


def write_export_yaml(path: Path, data: dict[str, Any]) -> None:
    write_yaml(path, data)
