from __future__ import annotations

from pathlib import Path

from pisa_sample_tools.common.formatting import slug

from .io import discover_agent_state_files, load_agent_states, load_run_info_for_agent_state_file
from .models import TrajectoryBatchResult, TrajectoryError, TrajectorySvgResult
from .output import prepare_output_dir, write_manifest
from .render import filter_states_by_range, states_to_svg


def render_agent_trajectory_svg(
    source_path: Path,
    *,
    title: str | None = None,
    width: int = 1100,
    height: int = 760,
    x_range: tuple[float, float] | None = None,
    y_range: tuple[float, float] | None = None,
    equal_scale: bool = True,
) -> str:
    states = load_agent_states(source_path)
    states = filter_states_by_range(states, x_range=x_range, y_range=y_range)
    if not states:
        raise TrajectoryError(f"no agent states found in requested range for {source_path}")
    return states_to_svg(
        states,
        title=title or _default_title(source_path),
        width=width,
        height=height,
        x_range=x_range,
        y_range=y_range,
        equal_scale=equal_scale,
        run_info=load_run_info_for_agent_state_file(source_path),
    )


def visualize_trajectories(
    *,
    input_path: Path,
    output_dir: Path,
    overwrite: bool = False,
    width: int = 1100,
    height: int = 760,
    x_range: tuple[float, float] | None = None,
    y_range: tuple[float, float] | None = None,
    equal_scale: bool = True,
) -> TrajectoryBatchResult:
    input_path = input_path.expanduser()
    source_files = discover_agent_state_files(input_path)
    if not source_files:
        raise TrajectoryError(f"no agent_state.csv or agent_states.csv files found in {input_path}")

    prepare_output_dir(output_dir, overwrite=overwrite)
    results: list[TrajectorySvgResult] = []
    for source_file in source_files:
        states = load_agent_states(source_file)
        states = filter_states_by_range(states, x_range=x_range, y_range=y_range)
        if not states:
            continue
        run_info = load_run_info_for_agent_state_file(source_file)
        title = _title_for_batch_source(source_file, input_path)
        svg = states_to_svg(
            states,
            title=title,
            width=width,
            height=height,
            x_range=x_range,
            y_range=y_range,
            equal_scale=equal_scale,
            run_info=run_info,
        )
        svg_path = output_dir / f"{_output_stem_for_source(source_file, input_path)}.svg"
        svg_path.write_text(svg, encoding="utf-8")
        speeds = [abs(state.speed) for state in states]
        results.append(
            TrajectorySvgResult(
                source_path=source_file,
                svg_path=svg_path,
                agent_count=len({state.agent_id for state in states}),
                state_count=len(states),
                min_speed=min(speeds),
                max_speed=max(speeds),
                params=run_info.params,
                result=run_info.result,
            )
        )

    if not results:
        raise TrajectoryError("agent state files were found, but none contained points in range")

    manifest_path = output_dir / "manifest.yaml"
    write_manifest(
        manifest_path,
        input_path=input_path,
        results=results,
        x_range=x_range,
        y_range=y_range,
        equal_scale=equal_scale,
    )
    return TrajectoryBatchResult(output_dir=output_dir, manifest_path=manifest_path, results=results)


def _default_title(source_path: Path) -> str:
    for parent in source_path.parents:
        if parent.name.startswith("iteration_"):
            return f"Trajectory: {parent.name}"
    return f"Trajectory: {source_path.stem}"


def _title_for_batch_source(source_file: Path, input_path: Path) -> str:
    if input_path.is_file():
        return _default_title(source_file)
    if input_path.name.startswith("iteration_"):
        return f"Trajectory: {input_path.name}"
    try:
        relative = source_file.relative_to(input_path)
    except ValueError:
        return _default_title(source_file)
    parts = relative.parts
    if len(parts) >= 3 and parts[0].startswith("iteration_"):
        return f"Trajectory: {parts[0]}"
    return f"Trajectory: {relative.parent}"


def _output_stem_for_source(source_file: Path, input_path: Path) -> str:
    if input_path.is_dir() and input_path.name.startswith("iteration_"):
        return f"{input_path.name}_trajectory"
    try:
        relative = source_file.relative_to(input_path)
    except ValueError:
        relative = source_file.name
    if isinstance(relative, Path):
        parts = relative.parts
        if len(parts) >= 3 and parts[0].startswith("iteration_"):
            return f"{parts[0]}_trajectory"
        if len(parts) >= 2 and parts[-2] == "monitor":
            return f"{slug(parts[-3] if len(parts) >= 3 else source_file.stem, fallback='trajectory')}_trajectory"
        return f"{slug('_'.join(parts[:-1]) or source_file.stem, fallback='trajectory')}_trajectory"
    return f"{slug(str(relative), fallback='trajectory')}_trajectory"

