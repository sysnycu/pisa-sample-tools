from __future__ import annotations

import math
from typing import Any

from pisa_sample_tools.common.formatting import format_number, panel_value, wrap_text
from pisa_sample_tools.common.sorting import natural_key
from pisa_sample_tools.common.svg import escape, svg_header, svg_rect, svg_text
from pisa_sample_tools.trajectory import AGENT_COLORS, AgentState

from .metrics import states_by_agent
from .models import AgentComparison


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

    left_by_agent = states_by_agent(left_plot)
    right_by_agent = states_by_agent(right_plot)

    parts = [svg_header(width, height)]
    parts.append(svg_rect(0, 0, width, height, "#ffffff"))
    parts.append(svg_text(width / 2, 30, f"Trajectory Comparison: {name}", size=19, weight="700", anchor="middle"))
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
        parts.append(f'<circle cx="{sx(states[0].x):.2f}" cy="{sy(states[0].y):.2f}" r="4" fill="{color}"><title>{escape(label)}</title></circle>')
        return
    points = " ".join(f"{sx(state.x):.2f},{sy(state.y):.2f}" for state in states)
    parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"{dash}><title>{escape(label)}</title></polyline>')
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
    parts.append(svg_text(0, 0, "Legend", size=15, weight="700"))
    parts.append('<line x1="0" y1="24" x2="34" y2="24" stroke="#111827" stroke-width="3"/>')
    parts.append(svg_text(44, 28, f"{left_label}: solid", size=11))
    parts.append('<line x1="0" y1="46" x2="34" y2="46" stroke="#111827" stroke-width="3" stroke-dasharray="7 5"/>')
    parts.append(svg_text(44, 50, f"{right_label}: dashed", size=11))
    cursor = 82
    parts.append(svg_text(0, cursor, "Metrics", size=15, weight="700"))
    cursor += 20
    if ignored:
        parts.append(svg_text(0, cursor, f"ignored agents: {', '.join(sorted(ignored, key=natural_key))}", size=11))
        cursor += 18
    for index, agent in enumerate(agents):
        color = AGENT_COLORS[index % len(AGENT_COLORS)]
        parts.append(f'<rect x="0" y="{cursor - 11}" width="10" height="10" fill="{color}"/>')
        parts.append(svg_text(16, cursor, f"agent {agent.agent_id}: ADE {format_number(agent.ade)} FDE {format_number(agent.fde)}", size=11))
        cursor += 16
        parts.append(svg_text(16, cursor, f"RMSE {format_number(agent.rmse)} max {format_number(agent.max_error)} n={agent.compared_steps}", size=11))
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
    parts.append(svg_text(0, cursor_y, title, size=15, weight="700"))
    cursor_y += 18
    for index, (key, value) in enumerate(values.items()):
        if index >= max_items:
            parts.append(svg_text(0, cursor_y, f"... {len(values) - max_items} more", size=10))
            cursor_y += 14
            break
        text = f"{key}: {_format_panel_value(value)}"
        for line in wrap_text(text, max_chars=42, split_long_words=False):
            parts.append(svg_text(0, cursor_y, line, size=10))
            cursor_y += 13
    return cursor_y + 12


def _compact_result(result: dict[str, Any]) -> dict[str, Any]:
    keys = ["status", "test_outcome", "stop_reason", "ego_to_agent_1.min_ttc_s", "ego.max_speed_mps"]
    compact = {key: result[key] for key in keys if key in result}
    if compact:
        return compact
    return result


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
        parts.append(svg_text(x, y0 + 24, format_number(x_value), size=11, anchor="middle"))
        parts.append(svg_text(x0 - 14, y + 4, format_number(y_value), size=11, anchor="end"))
    parts.append(svg_text(margin_left + plot_width / 2, y0 + 50, "x", size=13, weight="700", anchor="middle"))
    parts.append(svg_text(18, margin_top + plot_height / 2, "y", size=13, weight="700", anchor="middle", rotate=-90))
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
            svg_header(width, height),
            svg_text(width / 2, height / 2, f"No comparable non-ego agents for {name}", anchor="middle", size=18),
            "</svg>",
        ]
    )


def _format_panel_value(value: Any) -> str:
    return panel_value(value)

