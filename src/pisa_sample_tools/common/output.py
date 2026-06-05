from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def prepare_manifest_output_dir(
    output_dir: Path,
    *,
    overwrite: bool,
    error_type: type[ValueError],
    tool_label: str,
    validate_manifest,
    clear_previous,
) -> None:
    output_dir = output_dir.expanduser()
    if output_dir.exists():
        if not output_dir.is_dir():
            raise error_type(f"output path exists and is not a directory: {output_dir}")
        if not overwrite:
            raise error_type(f"output directory already exists: {output_dir}")
        if not any(output_dir.iterdir()):
            return
        clear_manifest_output_dir(
            output_dir,
            error_type=error_type,
            tool_label=tool_label,
            validate_manifest=validate_manifest,
            clear_previous=clear_previous,
        )
    output_dir.mkdir(parents=True, exist_ok=True)


def clear_manifest_output_dir(
    output_dir: Path,
    *,
    error_type: type[ValueError],
    tool_label: str,
    validate_manifest,
    clear_previous,
) -> None:
    manifest_path = output_dir / "manifest.yaml"
    if not manifest_path.exists():
        raise error_type(
            "output directory already exists and is not empty, but no manifest.yaml was found; "
            "refusing to overwrite non-tool output"
        )
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise error_type(f"could not read existing manifest.yaml: {exc}") from exc
    if not isinstance(manifest, dict) or not validate_manifest(manifest):
        raise error_type(f"existing manifest.yaml does not look like {tool_label} tool output")
    clear_previous(output_dir, manifest)
    manifest_path.unlink()


def unlink_manifest_paths(
    output_dir: Path,
    manifest: dict[str, Any],
    *,
    collection_key: str,
    path_key: str,
) -> None:
    entries = manifest.get(collection_key)
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = Path(str(entry.get(path_key, "")))
        if path.exists() and path.is_file() and is_relative_to(path, output_dir):
            path.unlink()

