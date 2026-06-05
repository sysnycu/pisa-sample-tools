from __future__ import annotations

import csv
import html
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from pisa_sample_tools.trajectory import (
    AGENT_COLORS,
    AGENT_STATE_FILENAMES,
    AgentState,
    _format_number,
    _natural_key,
    _svg_header,
    _svg_rect,
    _svg_text,
    discover_agent_state_files,
    load_agent_states,
    load_run_info_for_agent_state_file,
)


class TrajectoryCompareError(ValueError):
    """Raised for user-facing trajectory comparison failures."""


@dataclass(frozen=True)
class AgentComparison:
    agent_id: str
    compared_steps: int
    ade: float
    fde: float
    rmse: float
    max_error: float
    mean_speed_delta: float | None


@dataclass(frozen=True)
class TrajectoryComparison:
    name: str
    left_source: Path
    right_source: Path
    svg_path: Path
    agents: list[AgentComparison]
    params: dict[str, Any]
    left_result: dict[str, Any]
    right_result: dict[str, Any]

    @property
    def agent_count(self) -> int:
        return len(self.agents)

    @property
    def compared_steps(self) -> int:
        return sum(agent.compared_steps for agent in self.agents)

    @property
    def ade(self) -> float | None:
        return _weighted_mean((agent.ade, agent.compared_steps) for agent in self.agents)

    @property
    def fde(self) -> float | None:
        return _mean(agent.fde for agent in self.agents)

    @property
    def rmse(self) -> float | None:
        return _weighted_mean((agent.rmse, agent.compared_steps) for agent in self.agents)

    @property
    def max_error(self) -> float | None:
        if not self.agents:
            return None
        return max(agent.max_error for agent in self.agents)


@dataclass(frozen=True)
class TrajectoryCompareBatchResult:
    output_dir: Path
    manifest_path: Path
    summary_csv_path: Path
    comparisons: list[TrajectoryComparison]


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
    _prepare_compare_output_dir(output_dir, overwrite=overwrite)
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
    _write_summary_csv(summary_csv_path, comparisons)
    manifest_path = output_dir / "manifest.yaml"
    _write_manifest(
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


def pair_agent_state_files(left_path: Path, right_path: Path) -> list[tuple[str, Path, Path]]:
    if left_path.is_file() and right_path.is_file():
        return [("comparison", _validate_agent_state_file(left_path), _validate_agent_state_file(right_path))]
    if _is_single_iteration_path(left_path) and _is_single_iteration_path(right_path):
        return [
            (
                _pair_name(left_path, right_path),
                _single_agent_state_file(left_path),
                _single_agent_state_file(right_path),
            )
        ]

    left_files = _agent_state_file_map(left_path)
    right_files = _agent_state_file_map(right_path)
    names = sorted(set(left_files) & set(right_files), key=_natural_key)
    return [(name, left_files[name], right_files[name]) for name in names]


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
    svg_path = output_dir / f"{_slug(name)}_comparison.svg"
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


def compare_states(
    left_states: list[AgentState],
    right_states: list[AgentState],
    *,
    ignore_agent_ids: set[str],
) -> list[AgentComparison]:
    left_by_agent = _states_by_agent(left_states)
    right_by_agent = _states_by_agent(right_states)
    agent_ids = sorted((set(left_by_agent) & set(right_by_agent)) - ignore_agent_ids, key=_natural_key)
    comparisons: list[AgentComparison] = []
    for agent_id in agent_ids:
        left = left_by_agent[agent_id]
        right = right_by_agent[agent_id]
        count = min(len(left), len(right))
        if count == 0:
            continue
        distances = [_distance(left[index], right[index]) for index in range(count)]
        speed_deltas = [abs(left[index].speed - right[index].speed) for index in range(count)]
        comparisons.append(
            AgentComparison(
                agent_id=agent_id,
                compared_steps=count,
                ade=sum(distances) / count,
                fde=distances[-1],
                rmse=math.sqrt(sum(distance * distance for distance in distances) / count),
                max_error=max(distances),
                mean_speed_delta=sum(speed_deltas) / count,
            )
        )
    return comparisons


def comparison_to_svg(
    *,
    name: str,
    left_states: list[AgentState],
    right_states: list[AgentState],
    agents: list[AgentComparison],
    left_label: str,
    right_label: str,
    params: dict[str, Any],
    left_result: dict[str, Any],
    right_result: dict[str, Any],
    ignore_agent_ids: set[str],
    width: int = 1200,
    height: int = 820,
) -> str:
    included_agent_ids = {agent.agent_id for agent in agents}
    left_plot = [state for state in left_states if state.agent_id in included_agent_ids]
    right_plot = [state for state in right_states if state.agent_id in included_agent_ids]
    if not left_plot or not right_plot:
        return _empty_comparison_svg(name, width, height)

    xs = [state.x for state in [*left_plot, *right_plot]]
    ys = [state.y for state in [*left_plot, *right_plot]]
    min_x, max_x = _expanded_range(min(xs), max(xs))
    min_y, max_y = _expanded_range(min(ys), max(ys))

    side_width = 330
    margin_left = 78
    margin_top = 78
    margin_right = side_width + 36
    margin_bottom = 70
    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom

    def sx(x: float) -> float:
        return margin_left + (x - min_x) / (max_x - min_x) * plot_width

    def sy(y: float) -> float:
        return margin_top + (1.0 - (y - min_y) / (max_y - min_y)) * plot_height

    left_by_agent = _states_by_agent(left_plot)
    right_by_agent = _states_by_agent(right_plot)

    parts = [_svg_header(width, height)]
    parts.append(_svg_rect(0, 0, width, height, "#ffffff"))
    parts.append(_svg_text(width / 2, 30, f"Trajectory Comparison: {name}", size=19, weight="700", anchor="middle"))
    parts.extend(_axes(margin_left, margin_top, plot_width, plot_height, min_x, max_x, min_y, max_y))

    for index, agent in enumerate(agents):
        color = AGENT_COLORS[index % len(AGENT_COLORS)]
        _append_agent_path(parts, left_by_agent[agent.agent_id], sx, sy, color=color, dashed=False, label=f"{left_label} agent {agent.agent_id}")
        _append_agent_path(parts, right_by_agent[agent.agent_id], sx, sy, color=color, dashed=True, label=f"{right_label} agent {agent.agent_id}")
        _append_error_connectors(parts, left_by_agent[agent.agent_id], right_by_agent[agent.agent_id], sx, sy)

    parts.extend(
        _side_panel(
            x=width - side_width + 8,
            y=margin_top,
            agents=agents,
            left_label=left_label,
            right_label=right_label,
            params=params,
            left_result=left_result,
            right_result=right_result,
            ignored=ignore_agent_ids,
        )
    )
    parts.append("</svg>")
    return "\n".join(parts)


def _append_agent_path(
    parts: list[str],
    states: list[AgentState],
    sx,
    sy,
    *,
    color: str,
    dashed: bool,
    label: str,
) -> None:
    if not states:
        return
    dash = ' stroke-dasharray="7 5"' if dashed else ""
    if len(states) == 1:
        parts.append(f'<circle cx="{sx(states[0].x):.2f}" cy="{sy(states[0].y):.2f}" r="4" fill="{color}"><title>{_escape(label)}</title></circle>')
        return
    points = " ".join(f"{sx(state.x):.2f},{sy(state.y):.2f}" for state in states)
    parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"{dash}><title>{_escape(label)}</title></polyline>')
    start = states[0]
    end = states[-1]
    parts.append(f'<circle cx="{sx(start.x):.2f}" cy="{sy(start.y):.2f}" r="3" fill="#ffffff" stroke="{color}" stroke-width="2"/>')
    parts.append(f'<circle cx="{sx(end.x):.2f}" cy="{sy(end.y):.2f}" r="4" fill="{color}"/>')


def _append_error_connectors(parts: list[str], left: list[AgentState], right: list[AgentState], sx, sy) -> None:
    count = min(len(left), len(right))
    if count <= 1:
        return
    stride = max(1, count // 20)
    for index in range(0, count, stride):
        parts.append(
            f'<line x1="{sx(left[index].x):.2f}" y1="{sy(left[index].y):.2f}" '
            f'x2="{sx(right[index].x):.2f}" y2="{sy(right[index].y):.2f}" '
            'stroke="#111827" stroke-width="1" stroke-opacity="0.22"/>'
        )


def _side_panel(
    *,
    x: float,
    y: float,
    agents: list[AgentComparison],
    left_label: str,
    right_label: str,
    params: dict[str, Any],
    left_result: dict[str, Any],
    right_result: dict[str, Any],
    ignored: set[str],
) -> list[str]:
    parts = [f'<g transform="translate({x:.1f},{y:.1f})">']
    parts.append(_svg_text(0, 0, "Legend", size=15, weight="700"))
    parts.append('<line x1="0" y1="24" x2="34" y2="24" stroke="#111827" stroke-width="3"/>')
    parts.append(_svg_text(44, 28, f"{left_label}: solid", size=11))
    parts.append('<line x1="0" y1="46" x2="34" y2="46" stroke="#111827" stroke-width="3" stroke-dasharray="7 5"/>')
    parts.append(_svg_text(44, 50, f"{right_label}: dashed", size=11))
    cursor = 82
    parts.append(_svg_text(0, cursor, "Metrics", size=15, weight="700"))
    cursor += 20
    if ignored:
        parts.append(_svg_text(0, cursor, f"ignored agents: {', '.join(sorted(ignored, key=_natural_key))}", size=11))
        cursor += 18
    for index, agent in enumerate(agents):
        color = AGENT_COLORS[index % len(AGENT_COLORS)]
        parts.append(f'<rect x="0" y="{cursor - 11}" width="10" height="10" fill="{color}"/>')
        parts.append(_svg_text(16, cursor, f"agent {agent.agent_id}: ADE {_format_number(agent.ade)} FDE {_format_number(agent.fde)}", size=11))
        cursor += 16
        parts.append(_svg_text(16, cursor, f"RMSE {_format_number(agent.rmse)} max {_format_number(agent.max_error)} n={agent.compared_steps}", size=11))
        cursor += 20
    cursor += 10
    cursor = _append_kv_section(parts, "Params", params, cursor, max_items=8)
    cursor = _append_kv_section(parts, f"{left_label} Result", _compact_result(left_result), cursor, max_items=5)
    _append_kv_section(parts, f"{right_label} Result", _compact_result(right_result), cursor, max_items=5)
    parts.append("</g>")
    return parts


def _append_kv_section(
    parts: list[str],
    title: str,
    values: dict[str, Any],
    cursor_y: int,
    *,
    max_items: int,
) -> int:
    if not values:
        return cursor_y
    parts.append(_svg_text(0, cursor_y, title, size=15, weight="700"))
    cursor_y += 18
    for index, (key, value) in enumerate(values.items()):
        if index >= max_items:
            parts.append(_svg_text(0, cursor_y, f"... {len(values) - max_items} more", size=10))
            cursor_y += 14
            break
        text = f"{key}: {_format_panel_value(value)}"
        for line in _wrap_text(text, max_chars=42):
            parts.append(_svg_text(0, cursor_y, line, size=10))
            cursor_y += 13
    return cursor_y + 12


def _compact_result(result: dict[str, Any]) -> dict[str, Any]:
    keys = ["status", "test_outcome", "stop_reason", "ego_to_agent_1.min_ttc_s", "ego.max_speed_mps"]
    compact = {key: result[key] for key in keys if key in result}
    if compact:
        return compact
    return result


def _write_summary_csv(path: Path, comparisons: list[TrajectoryComparison]) -> None:
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


def _write_manifest(
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
        "ignore_agent_ids": sorted(ignore_agent_ids, key=_natural_key),
        "comparison_count": len(comparisons),
        "summary_csv_path": str(summary_csv_path),
        "overall": {
            "ade": _weighted_mean((comparison.ade or 0.0, comparison.compared_steps) for comparison in comparisons),
            "fde": _mean(value for comparison in comparisons if (value := comparison.fde) is not None),
            "rmse": _weighted_mean((comparison.rmse or 0.0, comparison.compared_steps) for comparison in comparisons),
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
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")


def _prepare_compare_output_dir(output_dir: Path, *, overwrite: bool) -> None:
    if output_dir.exists():
        if not output_dir.is_dir():
            raise TrajectoryCompareError(f"output path exists and is not a directory: {output_dir}")
        if not overwrite:
            raise TrajectoryCompareError(f"output directory already exists: {output_dir}")
        if not any(output_dir.iterdir()):
            return
        _clear_previous_compare_output(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)


def _clear_previous_compare_output(output_dir: Path) -> None:
    manifest_path = output_dir / "manifest.yaml"
    if not manifest_path.exists():
        raise TrajectoryCompareError(
            "output directory already exists and is not empty, but no manifest.yaml was found; "
            "refusing to overwrite non-tool output"
        )
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise TrajectoryCompareError(f"could not read existing manifest.yaml: {exc}") from exc
    comparisons = manifest.get("comparisons")
    if not isinstance(comparisons, list) or "summary_csv_path" not in manifest:
        raise TrajectoryCompareError(
            "existing manifest.yaml does not look like trajectory compare tool output"
        )

    for comparison in comparisons:
        if not isinstance(comparison, dict):
            continue
        svg_path = Path(str(comparison.get("svg_path", "")))
        if svg_path.exists() and svg_path.is_file() and _is_relative_to(svg_path, output_dir):
            svg_path.unlink()

    summary_csv_path = Path(str(manifest.get("summary_csv_path", "")))
    if (
        summary_csv_path.exists()
        and summary_csv_path.is_file()
        and _is_relative_to(summary_csv_path, output_dir)
    ):
        summary_csv_path.unlink()
    manifest_path.unlink()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _states_by_agent(states: list[AgentState]) -> dict[str, list[AgentState]]:
    grouped: dict[str, list[AgentState]] = defaultdict(list)
    for state in states:
        grouped[state.agent_id].append(state)
    return {agent_id: sorted(values, key=_state_time_key) for agent_id, values in grouped.items()}


def _state_time_key(state: AgentState) -> tuple[float, int, float, float]:
    time_value = state.sim_time_ms if state.sim_time_ms is not None else math.inf
    step_value = state.step_index if state.step_index is not None else 10**12
    return (time_value, step_value, state.x, state.y)


def _distance(left: AgentState, right: AgentState) -> float:
    return math.hypot(left.x - right.x, left.y - right.y)


def _agent_state_file_map(path: Path) -> dict[str, Path]:
    if path.is_file():
        return {"comparison": _validate_agent_state_file(path)}
    if _is_single_iteration_path(path):
        return {_iteration_name(path): _single_agent_state_file(path)}
    files: dict[str, Path] = {}
    for source_file in discover_agent_state_files(path):
        name = _iteration_name_from_file(source_file)
        files[name] = source_file
    return files


def _validate_agent_state_file(path: Path) -> Path:
    if not path.is_file() or path.name not in AGENT_STATE_FILENAMES:
        raise TrajectoryCompareError(f"expected agent state csv file: {path}")
    return path


def _single_agent_state_file(path: Path) -> Path:
    files = discover_agent_state_files(path)
    if len(files) != 1:
        raise TrajectoryCompareError(f"expected one agent state file below {path}, found {len(files)}")
    return files[0]


def _is_single_iteration_path(path: Path) -> bool:
    return path.is_dir() and path.name.startswith("iteration_")


def _iteration_name(path: Path) -> str:
    return path.name if path.name.startswith("iteration_") else path.stem


def _iteration_name_from_file(path: Path) -> str:
    for parent in path.parents:
        if parent.name.startswith("iteration_"):
            return parent.name
    return path.stem


def _pair_name(left_path: Path, right_path: Path) -> str:
    left = _iteration_name(left_path)
    right = _iteration_name(right_path)
    return left if left == right else f"{left}_vs_{right}"


def _axes(
    margin_left: float,
    margin_top: float,
    plot_width: float,
    plot_height: float,
    min_x: float,
    max_x: float,
    min_y: float,
    max_y: float,
) -> list[str]:
    x0 = margin_left
    y0 = margin_top + plot_height
    parts = [
        f'<rect x="{margin_left:.2f}" y="{margin_top:.2f}" width="{plot_width:.2f}" height="{plot_height:.2f}" fill="#f8fafc" stroke="#d1d5db"/>',
    ]
    for tick in range(6):
        fraction = tick / 5
        x = margin_left + fraction * plot_width
        y = margin_top + plot_height - fraction * plot_height
        x_value = min_x + fraction * (max_x - min_x)
        y_value = min_y + fraction * (max_y - min_y)
        parts.append(f'<line x1="{x:.2f}" y1="{margin_top:.2f}" x2="{x:.2f}" y2="{y0:.2f}" stroke="#e5e7eb"/>')
        parts.append(f'<line x1="{x0:.2f}" y1="{y:.2f}" x2="{x0 + plot_width:.2f}" y2="{y:.2f}" stroke="#e5e7eb"/>')
        parts.append(_svg_text(x, y0 + 24, _format_number(x_value), size=11, anchor="middle"))
        parts.append(_svg_text(x0 - 14, y + 4, _format_number(y_value), size=11, anchor="end"))
    parts.append(_svg_text(margin_left + plot_width / 2, y0 + 50, "x", size=13, weight="700", anchor="middle"))
    parts.append(_svg_text(18, margin_top + plot_height / 2, "y", size=13, weight="700", anchor="middle", rotate=-90))
    return parts


def _expanded_range(min_value: float, max_value: float) -> tuple[float, float]:
    if math.isclose(min_value, max_value):
        delta = max(1.0, abs(min_value) * 0.1)
        return min_value - delta, max_value + delta
    padding = (max_value - min_value) * 0.06
    return min_value - padding, max_value + padding


def _empty_comparison_svg(name: str, width: int, height: int) -> str:
    return "\n".join(
        [
            _svg_header(width, height),
            _svg_text(width / 2, height / 2, f"No comparable non-ego agents for {name}", anchor="middle", size=18),
            "</svg>",
        ]
    )


def _format_panel_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _wrap_text(text: str, *, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _default_label(path: Path) -> str:
    path = path.expanduser()
    if path.is_file():
        for parent in path.parents:
            if parent.name.startswith("iteration_"):
                return parent.parent.parent.name if parent.parent.name == "monitor" else parent.name
        return path.stem
    return path.name


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return slug.strip("._") or "comparison"


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _mean(values) -> float | None:
    values = list(values)
    if not values:
        return None
    return sum(values) / len(values)


def _weighted_mean(values_and_weights) -> float | None:
    total = 0.0
    weight_sum = 0
    for value, weight in values_and_weights:
        if value is None or weight <= 0:
            continue
        total += value * weight
        weight_sum += weight
    if weight_sum == 0:
        return None
    return total / weight_sum
