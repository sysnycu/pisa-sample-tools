from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import replace
from typing import Any

from pisa_sample_tools.common.formatting import format_number, panel_value, wrap_text
from pisa_sample_tools.common.sorting import natural_key
from pisa_sample_tools.common.svg import escape, svg_header, svg_rect, svg_text

from .models import AGENT_COLORS, AgentGeometry, AgentState, RunInfo, TrajectoryError


def filter_states_by_range(
    states: list[AgentState],
    *,
    x_range: tuple[float, float] | None = None,
    y_range: tuple[float, float] | None = None,
) -> list[AgentState]:
    _validate_range(x_range, label="x_range")
    _validate_range(y_range, label="y_range")
    filtered = []
    for state in states:
        if x_range is not None and not (x_range[0] <= state.x <= x_range[1]):
            continue
        if y_range is not None and not (y_range[0] <= state.y <= y_range[1]):
            continue
        filtered.append(state)
    return filtered


def filter_states_by_agent(
    states: list[AgentState],
    *,
    ignore_agent_ids: set[str] | None = None,
) -> list[AgentState]:
    if not ignore_agent_ids:
        return states
    return [state for state in states if state.agent_id not in ignore_agent_ids]


def origin_for_agent(states: list[AgentState], agent_id: str) -> tuple[float, float] | None:
    for state in states:
        if state.agent_id == agent_id:
            return state.x, state.y
    return None


def translate_states(states: list[AgentState], *, origin: tuple[float, float]) -> list[AgentState]:
    origin_x, origin_y = origin
    return [replace(state, x=state.x - origin_x, y=state.y - origin_y) for state in states]


def states_to_svg(
    states: list[AgentState],
    *,
    title: str,
    width: int = 1100,
    height: int = 760,
    x_range: tuple[float, float] | None = None,
    y_range: tuple[float, float] | None = None,
    equal_scale: bool = True,
    run_info: RunInfo | None = None,
    geometries: list[AgentGeometry] | None = None,
) -> str:
    if width < 500 or height < 360:
        raise TrajectoryError("SVG width/height are too small")
    if not states:
        raise TrajectoryError("no agent states to render")

    by_agent: dict[str, list[AgentState]] = defaultdict(list)
    for state in states:
        by_agent[state.agent_id].append(state)
    agent_ids = sorted(by_agent, key=natural_key)
    speeds = [abs(state.speed) for state in states]
    min_speed = min(speeds)
    max_speed = max(speeds)

    geometry_by_agent = _geometry_by_agent(geometries or [])
    footprint_points = [
        point
        for state in states
        for point in footprint_world(
            state, _geometry_at(geometry_by_agent.get(state.agent_id, []), state)
        )
    ]
    xs = [state.x for state in states] + [point[0] for point in footprint_points]
    ys = [state.y for state in states] + [point[1] for point in footprint_points]
    min_x, max_x = x_range if x_range is not None else expanded_range(min(xs), max(xs))
    min_y, max_y = y_range if y_range is not None else expanded_range(min(ys), max(ys))

    legend_width = 250
    base_left = 78
    base_top = 72
    margin_right = legend_width + 34
    margin_bottom = 70
    available_width = width - base_left - margin_right
    available_height = height - base_top - margin_bottom
    if available_width <= 0 or available_height <= 0:
        raise TrajectoryError("SVG dimensions leave no plot area")
    if equal_scale:
        margin_left, margin_top, plot_width, plot_height = equal_scale_plot_area(
            base_left=base_left,
            base_top=base_top,
            available_width=available_width,
            available_height=available_height,
            x_span=max_x - min_x,
            y_span=max_y - min_y,
        )
    else:
        margin_left = base_left
        margin_top = base_top
        plot_width = available_width
        plot_height = available_height

    def sx(x: float) -> float:
        return margin_left + ((x - min_x) / (max_x - min_x)) * plot_width

    def sy(y: float) -> float:
        return margin_top + (1.0 - ((y - min_y) / (max_y - min_y))) * plot_height

    parts = [svg_header(width, height)]
    parts.append(svg_rect(0, 0, width, height, "#ffffff"))
    parts.append(svg_text(width / 2, 30, title, size=19, weight="700", anchor="middle"))
    parts.extend(axes(margin_left, margin_top, plot_width, plot_height, min_x, max_x, min_y, max_y))

    for index, agent_id in enumerate(agent_ids):
        color = AGENT_COLORS[index % len(AGENT_COLORS)]
        agent_states = by_agent[agent_id]
        if len(agent_states) == 1:
            state = agent_states[0]
            parts.append(
                f'<circle cx="{sx(state.x):.2f}" cy="{sy(state.y):.2f}" r="4.5" '
                f'fill="{color}" fill-opacity="{_speed_opacity(state.speed, min_speed, max_speed):.3f}">'
                f"<title>{escape(f'agent {agent_id}, speed {state.speed:g}')}</title></circle>"
            )
            continue
        for first, second in zip(agent_states, agent_states[1:], strict=False):
            segment_speed = (abs(first.speed) + abs(second.speed)) / 2.0
            opacity = _speed_opacity(segment_speed, min_speed, max_speed)
            parts.append(
                f'<line x1="{sx(first.x):.2f}" y1="{sy(first.y):.2f}" '
                f'x2="{sx(second.x):.2f}" y2="{sy(second.y):.2f}" '
                f'stroke="{color}" stroke-width="3.2" stroke-linecap="round" '
                f'stroke-opacity="{opacity:.3f}">'
                f"<title>{escape(f'agent {agent_id}, speed {segment_speed:g}')}</title></line>"
            )
        start = agent_states[0]
        end = agent_states[-1]
        geometry_start = _geometry_at(geometry_by_agent.get(agent_id, []), start)
        geometry_end = _geometry_at(geometry_by_agent.get(agent_id, []), end)
        for state, geometry, label in (
            (start, geometry_start, "start"),
            (end, geometry_end, "end"),
        ):
            polygon = footprint_world(state, geometry)
            if polygon:
                points = " ".join(f"{sx(px):.2f},{sy(py):.2f}" for px, py in polygon)
                parts.append(
                    f'<polygon points="{points}" fill="{color}" fill-opacity="0.14" stroke="{color}" stroke-width="1.6"><title>{escape(f"{_agent_label(state, geometry)} {label} bounding box")}</title></polygon>'
                )
        parts.append(
            f'<circle cx="{sx(start.x):.2f}" cy="{sy(start.y):.2f}" r="3.4" '
            f'fill="#ffffff" stroke="{color}" stroke-width="2"><title>{escape(f"agent {agent_id} start")}</title></circle>'
        )
        parts.append(
            f'<circle cx="{sx(end.x):.2f}" cy="{sy(end.y):.2f}" r="4.2" '
            f'fill="{color}"><title>{escape(f"agent {agent_id} end")}</title></circle>'
        )

    parts.extend(
        _side_panel(
            agent_ids,
            x=width - legend_width + 4,
            y=margin_top,
            min_speed=min_speed,
            max_speed=max_speed,
            run_info=run_info,
            states_by_agent=by_agent,
            geometry_by_agent=geometry_by_agent,
        )
    )
    parts.append("</svg>")
    return "\n".join(parts)


def expanded_range(min_value: float, max_value: float) -> tuple[float, float]:
    if math.isclose(min_value, max_value):
        delta = max(1.0, abs(min_value) * 0.1)
        return min_value - delta, max_value + delta
    padding = (max_value - min_value) * 0.06
    return min_value - padding, max_value + padding


def axes(
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
        f'<rect id="trajectory-plot-area" x="{margin_left:.2f}" y="{margin_top:.2f}" width="{plot_width:.2f}" height="{plot_height:.2f}" fill="#f8fafc" stroke="#d1d5db"/>',
    ]
    for tick in range(6):
        fraction = tick / 5
        x = margin_left + fraction * plot_width
        y = margin_top + plot_height - fraction * plot_height
        x_value = min_x + fraction * (max_x - min_x)
        y_value = min_y + fraction * (max_y - min_y)
        parts.append(
            f'<line x1="{x:.2f}" y1="{margin_top}" x2="{x:.2f}" y2="{y0}" stroke="#e5e7eb"/>'
        )
        parts.append(
            f'<line x1="{x0}" y1="{y:.2f}" x2="{x0 + plot_width}" y2="{y:.2f}" stroke="#e5e7eb"/>'
        )
        parts.append(svg_text(x, y0 + 24, format_number(x_value), size=11, anchor="middle"))
        parts.append(svg_text(x0 - 14, y + 4, format_number(y_value), size=11, anchor="end"))
    parts.append(
        svg_text(margin_left + plot_width / 2, y0 + 50, "x", size=13, weight="700", anchor="middle")
    )
    parts.append(
        svg_text(
            18,
            margin_top + plot_height / 2,
            "y",
            size=13,
            weight="700",
            anchor="middle",
            rotate=-90,
        )
    )
    return parts


def _validate_range(value: tuple[float, float] | None, *, label: str) -> None:
    if value is None:
        return
    if not math.isfinite(value[0]) or not math.isfinite(value[1]):
        raise TrajectoryError(f"{label} values must be finite")
    if value[0] >= value[1]:
        raise TrajectoryError(f"{label} min must be smaller than max")


def equal_scale_plot_area(
    *,
    base_left: float,
    base_top: float,
    available_width: float,
    available_height: float,
    x_span: float,
    y_span: float,
) -> tuple[float, float, float, float]:
    if x_span <= 0 or y_span <= 0:
        raise TrajectoryError("x/y range spans must be greater than 0")
    scale = min(available_width / x_span, available_height / y_span)
    plot_width = x_span * scale
    plot_height = y_span * scale
    margin_left = base_left + (available_width - plot_width) / 2.0
    margin_top = base_top + (available_height - plot_height) / 2.0
    return margin_left, margin_top, plot_width, plot_height


_equal_scale_plot_area = equal_scale_plot_area


def _speed_opacity(speed: float, min_speed: float, max_speed: float) -> float:
    if max_speed <= min_speed:
        return 0.78
    normalized = (abs(speed) - min_speed) / (max_speed - min_speed)
    return 0.18 + 0.82 * max(0.0, min(1.0, normalized))


def _side_panel(
    agent_ids: list[str],
    *,
    x: float,
    y: float,
    min_speed: float,
    max_speed: float,
    run_info: RunInfo | None,
    states_by_agent: dict[str, list[AgentState]],
    geometry_by_agent: dict[str, list[AgentGeometry]],
) -> list[str]:
    parts = [
        f'<g transform="translate({x:.2f},{y:.2f})">',
        svg_text(0, 0, "Agents", size=15, weight="700"),
    ]
    cursor_y = 24
    for index, agent_id in enumerate(agent_ids):
        color = AGENT_COLORS[index % len(AGENT_COLORS)]
        parts.append(
            f'<line x1="0" y1="{cursor_y}" x2="32" y2="{cursor_y}" stroke="{color}" stroke-width="4" stroke-linecap="round"/>'
        )
        state = states_by_agent[agent_id][0]
        geometry = _geometry_at(geometry_by_agent.get(agent_id, []), state)
        parts.append(svg_text(42, cursor_y + 4, _agent_label(state, geometry), size=12))
        cursor_y += 22
    cursor_y += 12
    parts.append(svg_text(0, cursor_y, "Speed opacity", size=15, weight="700"))
    cursor_y += 20
    for offset, opacity in enumerate((0.18, 0.48, 1.0)):
        y0 = cursor_y + offset * 18
        parts.append(
            f'<line x1="0" y1="{y0}" x2="32" y2="{y0}" stroke="#111827" stroke-width="4" stroke-opacity="{opacity:.2f}" stroke-linecap="round"/>'
        )
    parts.append(svg_text(42, cursor_y + 4, f"slow {format_number(min_speed)}", size=12))
    parts.append(svg_text(42, cursor_y + 22, "medium", size=12))
    parts.append(svg_text(42, cursor_y + 40, f"fast {format_number(max_speed)}", size=12))
    cursor_y += 68
    if run_info is not None and run_info.params:
        cursor_y = _append_kv_section(parts, "Params", run_info.params, cursor_y)
    if run_info is not None and run_info.result:
        _append_kv_section(parts, "Result", run_info.result, cursor_y)
    parts.append("</g>")
    return parts


def _append_kv_section(
    parts: list[str],
    title: str,
    values: dict[str, Any],
    cursor_y: int,
    *,
    max_items: int = 12,
) -> int:
    parts.append(svg_text(0, cursor_y, title, size=15, weight="700"))
    cursor_y += 20
    for index, (key, value) in enumerate(values.items()):
        if index >= max_items:
            parts.append(svg_text(0, cursor_y, f"... {len(values) - max_items} more", size=11))
            cursor_y += 18
            break
        for line in wrap_text(f"{key}: {_format_panel_value(value)}", max_chars=34):
            parts.append(svg_text(0, cursor_y, line, size=11))
            cursor_y += 15
        cursor_y += 2
    return cursor_y + 12


def _format_panel_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True)
    return panel_value(value)


def footprint_world(state: AgentState, geometry: AgentGeometry | None) -> list[tuple[float, float]]:
    if geometry is None:
        return []
    local = list(geometry.footprint)
    if (
        not local
        and geometry.length_m
        and geometry.width_m
        and geometry.length_m > 0
        and geometry.width_m > 0
    ):
        half_length, half_width = geometry.length_m / 2, geometry.width_m / 2
        local = [
            (-half_length, -half_width),
            (half_length, -half_width),
            (half_length, half_width),
            (-half_length, half_width),
        ]
    if not local:
        return []
    pose_cos, pose_sin = math.cos(state.yaw), math.sin(state.yaw)
    center_x = state.x + pose_cos * geometry.center_offset_x - pose_sin * geometry.center_offset_y
    center_y = state.y + pose_sin * geometry.center_offset_x + pose_cos * geometry.center_offset_y
    angle = state.yaw + geometry.yaw_offset
    cos_yaw, sin_yaw = math.cos(angle), math.sin(angle)
    return [
        (center_x + cos_yaw * x - sin_yaw * y, center_y + sin_yaw * x + cos_yaw * y)
        for x, y in local
    ]


def _geometry_by_agent(geometries: list[AgentGeometry]) -> dict[str, list[AgentGeometry]]:
    output: dict[str, list[AgentGeometry]] = defaultdict(list)
    for geometry in geometries:
        output[geometry.agent_id].append(geometry)
    return output


def _geometry_at(geometries: list[AgentGeometry], state: AgentState) -> AgentGeometry | None:
    eligible = [
        item
        for item in geometries
        if (
            item.sim_time_ms is None
            or state.sim_time_ms is None
            or item.sim_time_ms <= state.sim_time_ms
        )
        and (
            item.step_index is None
            or state.step_index is None
            or item.step_index <= state.step_index
        )
    ]
    return eligible[-1] if eligible else (geometries[0] if geometries else None)


def _agent_label(state: AgentState, geometry: AgentGeometry | None) -> str:
    name = state.entity_name or (geometry.entity_name if geometry else None)
    is_ego = state.is_ego if state.is_ego is not None else (geometry.is_ego if geometry else False)
    return f"{name or 'agent'} [{state.agent_id}]" + (" (ego)" if is_ego else "")
