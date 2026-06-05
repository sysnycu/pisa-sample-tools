from __future__ import annotations

from pathlib import Path
from typing import Any

from pisa_sample_tools.common.output import prepare_manifest_output_dir, unlink_manifest_paths
from pisa_sample_tools.common.yaml import write_yaml

from .models import TrajectoryError, TrajectorySvgResult


def prepare_output_dir(output_dir: Path, *, overwrite: bool) -> None:
    prepare_manifest_output_dir(
        output_dir,
        overwrite=overwrite,
        error_type=TrajectoryError,
        tool_label="trajectory",
        validate_manifest=lambda manifest: isinstance(manifest.get("outputs"), list),
        clear_previous=clear_previous_output,
    )


def clear_previous_output(output_dir: Path, manifest: dict[str, Any]) -> None:
    unlink_manifest_paths(output_dir, manifest, collection_key="outputs", path_key="svg_path")


def write_manifest(
    manifest_path: Path,
    *,
    input_path: Path,
    results: list[TrajectorySvgResult],
    x_range: tuple[float, float] | None,
    y_range: tuple[float, float] | None,
    equal_scale: bool,
) -> None:
    manifest = {
        "input_path": str(input_path),
        "svg_count": len(results),
        "x_range": list(x_range) if x_range is not None else None,
        "y_range": list(y_range) if y_range is not None else None,
        "scale_mode": "equal" if equal_scale else "stretch",
        "outputs": [
            {
                "source_path": str(result.source_path),
                "svg_path": str(result.svg_path),
                "agent_count": result.agent_count,
                "state_count": result.state_count,
                "min_speed": result.min_speed,
                "max_speed": result.max_speed,
                "params": result.params,
                "result": result.result,
            }
            for result in results
        ],
    }
    write_yaml(manifest_path, manifest)

