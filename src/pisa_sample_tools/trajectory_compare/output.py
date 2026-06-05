from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from pisa_sample_tools.common.output import (
    is_relative_to,
    prepare_manifest_output_dir,
    unlink_manifest_paths,
)
from pisa_sample_tools.common.sorting import natural_key
from pisa_sample_tools.common.yaml import write_yaml

from .models import TrajectoryCompareError, TrajectoryComparison
from .stats import mean, weighted_mean


def write_summary_csv(path: Path, comparisons: list[TrajectoryComparison]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "name",
            "agent_id",
            "compared_steps",
            "ade",
            "fde",
            "rmse",
            "max_error",
            "mean_speed_delta",
            "left_source",
            "right_source",
            "svg_path",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for comparison in comparisons:
            for agent in comparison.agents:
                writer.writerow(
                    {
                        "name": comparison.name,
                        "agent_id": agent.agent_id,
                        "compared_steps": agent.compared_steps,
                        "ade": agent.ade,
                        "fde": agent.fde,
                        "rmse": agent.rmse,
                        "max_error": agent.max_error,
                        "mean_speed_delta": agent.mean_speed_delta,
                        "left_source": comparison.left_source,
                        "right_source": comparison.right_source,
                        "svg_path": comparison.svg_path,
                    }
                )


def write_manifest(
    path: Path,
    *,
    left_path: Path,
    right_path: Path,
    left_label: str,
    right_label: str,
    ignore_agent_ids: set[str],
    comparisons: list[TrajectoryComparison],
    summary_csv_path: Path,
) -> None:
    manifest = {
        "left_path": str(left_path),
        "right_path": str(right_path),
        "left_label": left_label,
        "right_label": right_label,
        "ignore_agent_ids": sorted(ignore_agent_ids, key=natural_key),
        "comparison_count": len(comparisons),
        "summary_csv_path": str(summary_csv_path),
        "overall": {
            "ade": weighted_mean((comparison.ade or 0.0, comparison.compared_steps) for comparison in comparisons),
            "fde": mean(value for comparison in comparisons if (value := comparison.fde) is not None),
            "rmse": weighted_mean((comparison.rmse or 0.0, comparison.compared_steps) for comparison in comparisons),
            "max_error": max((comparison.max_error or 0.0) for comparison in comparisons),
        },
        "comparisons": [
            {
                "name": comparison.name,
                "left_source": str(comparison.left_source),
                "right_source": str(comparison.right_source),
                "svg_path": str(comparison.svg_path),
                "agent_count": comparison.agent_count,
                "compared_steps": comparison.compared_steps,
                "ade": comparison.ade,
                "fde": comparison.fde,
                "rmse": comparison.rmse,
                "max_error": comparison.max_error,
                "agents": [
                    {
                        "agent_id": agent.agent_id,
                        "compared_steps": agent.compared_steps,
                        "ade": agent.ade,
                        "fde": agent.fde,
                        "rmse": agent.rmse,
                        "max_error": agent.max_error,
                        "mean_speed_delta": agent.mean_speed_delta,
                    }
                    for agent in comparison.agents
                ],
            }
            for comparison in comparisons
        ],
    }
    write_yaml(path, manifest)


def prepare_compare_output_dir(output_dir: Path, *, overwrite: bool) -> None:
    prepare_manifest_output_dir(
        output_dir,
        overwrite=overwrite,
        error_type=TrajectoryCompareError,
        tool_label="trajectory compare",
        validate_manifest=lambda manifest: isinstance(manifest.get("comparisons"), list)
        and "summary_csv_path" in manifest,
        clear_previous=clear_previous_compare_output,
    )


def clear_previous_compare_output(output_dir: Path, manifest: dict[str, Any]) -> None:
    unlink_manifest_paths(output_dir, manifest, collection_key="comparisons", path_key="svg_path")
    summary_csv_path = Path(str(manifest.get("summary_csv_path", "")))
    if (
        summary_csv_path.exists()
        and summary_csv_path.is_file()
        and is_relative_to(summary_csv_path, output_dir)
    ):
        summary_csv_path.unlink()

