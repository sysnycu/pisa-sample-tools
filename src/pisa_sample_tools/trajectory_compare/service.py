from __future__ import annotations

from pathlib import Path

from pisa_sample_tools.common.formatting import slug
from pisa_sample_tools.trajectory import load_agent_states, load_run_info_for_agent_state_file

from .metrics import compare_states
from .models import (
    TrajectoryCompareBatchResult,
    TrajectoryCompareError,
    TrajectoryComparison,
)
from .output import prepare_compare_output_dir, write_manifest, write_summary_csv
from .pairing import pair_agent_state_files
from .render import comparison_to_svg


def compare_trajectory_sets(
    *,
    left_path: Path,
    right_path: Path,
    output_dir: Path,
    left_label: str | None = None,
    right_label: str | None = None,
    ignore_agent_ids: set[str] | None = None,
    overwrite: bool = False,
    width: int = 1200,
    height: int = 820,
) -> TrajectoryCompareBatchResult:
    ignore_agent_ids = ignore_agent_ids or {"1"}
    pairs = pair_agent_state_files(left_path.expanduser(), right_path.expanduser())
    if not pairs:
        raise TrajectoryCompareError("no comparable agent state files found")

    output_dir = output_dir.expanduser()
    prepare_compare_output_dir(output_dir, overwrite=overwrite)
    comparisons: list[TrajectoryComparison] = []
    for name, left_file, right_file in pairs:
        comparison = compare_agent_state_files(
            left_file=left_file,
            right_file=right_file,
            output_dir=output_dir,
            name=name,
            left_label=left_label or _default_label(left_path),
            right_label=right_label or _default_label(right_path),
            ignore_agent_ids=ignore_agent_ids,
            width=width,
            height=height,
        )
        if comparison.agents:
            comparisons.append(comparison)

    if not comparisons:
        raise TrajectoryCompareError("agent state files were found, but no non-ignored agents overlapped")

    summary_csv_path = output_dir / "summary.csv"
    write_summary_csv(summary_csv_path, comparisons)
    manifest_path = output_dir / "manifest.yaml"
    write_manifest(
        manifest_path,
        left_path=left_path,
        right_path=right_path,
        left_label=left_label or _default_label(left_path),
        right_label=right_label or _default_label(right_path),
        ignore_agent_ids=ignore_agent_ids,
        comparisons=comparisons,
        summary_csv_path=summary_csv_path,
    )
    return TrajectoryCompareBatchResult(
        output_dir=output_dir,
        manifest_path=manifest_path,
        summary_csv_path=summary_csv_path,
        comparisons=comparisons,
    )


def compare_agent_state_files(
    *,
    left_file: Path,
    right_file: Path,
    output_dir: Path,
    name: str,
    left_label: str,
    right_label: str,
    ignore_agent_ids: set[str],
    width: int = 1200,
    height: int = 820,
) -> TrajectoryComparison:
    left_states = load_agent_states(left_file)
    right_states = load_agent_states(right_file)
    agents = compare_states(left_states, right_states, ignore_agent_ids=ignore_agent_ids)
    left_info = load_run_info_for_agent_state_file(left_file)
    right_info = load_run_info_for_agent_state_file(right_file)
    svg_path = output_dir / f"{slug(name, fallback='comparison')}_comparison.svg"
    if not agents:
        return TrajectoryComparison(
            name=name,
            left_source=left_file,
            right_source=right_file,
            svg_path=svg_path,
            agents=agents,
            params=left_info.params or right_info.params,
            left_result=left_info.result,
            right_result=right_info.result,
        )
    svg = comparison_to_svg(
        name=name,
        left_states=left_states,
        right_states=right_states,
        agents=agents,
        left_label=left_label,
        right_label=right_label,
        params=left_info.params or right_info.params,
        left_result=left_info.result,
        right_result=right_info.result,
        ignore_agent_ids=ignore_agent_ids,
        width=width,
        height=height,
    )
    svg_path.write_text(svg, encoding="utf-8")
    return TrajectoryComparison(
        name=name,
        left_source=left_file,
        right_source=right_file,
        svg_path=svg_path,
        agents=agents,
        params=left_info.params or right_info.params,
        left_result=left_info.result,
        right_result=right_info.result,
    )


def _default_label(path: Path) -> str:
    path = path.expanduser()
    if path.is_file():
        for parent in path.parents:
            if parent.name.startswith("iteration_"):
                return parent.parent.parent.name if parent.parent.name == "monitor" else parent.name
        return path.stem
    return path.name

