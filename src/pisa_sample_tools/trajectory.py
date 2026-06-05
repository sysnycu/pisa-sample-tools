from __future__ import annotations

import csv
import html
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class TrajectoryError(ValueError):
    """Raised for user-facing trajectory visualization failures."""


AGENT_STATE_FILENAMES = {"agent_state.csv", "agent_states.csv"}
AGENT_COLORS = [
    "#2563eb",
    "#16a34a",
    "#dc2626",
    "#f59e0b",
    "#7c3aed",
    "#0891b2",
    "#be123c",
    "#4b5563",
    "#84cc16",
    "#c026d3",
    "#0f766e",
    "#ea580c",
]


@dataclass(frozen=True)
class AgentState:
    step_index: int | None
    sim_time_ms: float | None
    agent_id: str
    x: float
    y: float
    speed: float


@dataclass(frozen=True)
class RunInfo:
    params: dict[str, Any]
    result: dict[str, Any]
    result_path: Path | None = None


@dataclass(frozen=True)
class TrajectorySvgResult:
    source_path: Path
    svg_path: Path
    agent_count: int
    state_count: int
    min_speed: float
    max_speed: float
    params: dict[str, Any]
    result: dict[str, Any]


@dataclass(frozen=True)
class TrajectoryBatchResult:
    output_dir: Path
    manifest_path: Path
    results: list[TrajectorySvgResult]


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

    _prepare_output_dir(output_dir, overwrite=overwrite)
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
    _write_manifest(
        manifest_path,
        input_path=input_path,
        results=results,
        x_range=x_range,
        y_range=y_range,
        equal_scale=equal_scale,
    )
    return TrajectoryBatchResult(output_dir=output_dir, manifest_path=manifest_path, results=results)


def discover_agent_state_files(input_path: Path) -> list[Path]:
    input_path = input_path.expanduser()
    if input_path.is_file():
        if input_path.name not in AGENT_STATE_FILENAMES:
            raise TrajectoryError(f"input file must be agent_state.csv or agent_states.csv: {input_path}")
        return [input_path]
    if not input_path.is_dir():
        raise TrajectoryError(f"input path does not exist: {input_path}")

    direct_monitor_files = [
        input_path / "monitor" / filename for filename in ("agent_states.csv", "agent_state.csv")
    ]
    for path in direct_monitor_files:
        if path.exists():
            return [path]

    direct_files = [input_path / filename for filename in ("agent_states.csv", "agent_state.csv")]
    for path in direct_files:
        if path.exists():
            return [path]

    iteration_files: list[Path] = []
    for iteration_dir in sorted(input_path.glob("iteration_*"), key=_natural_path_key):
        if not iteration_dir.is_dir():
            continue
        for filename in ("agent_states.csv", "agent_state.csv"):
            path = iteration_dir / "monitor" / filename
            if path.exists():
                iteration_files.append(path)
                break
    if iteration_files:
        return iteration_files

    return sorted(
        (
            path
            for path in input_path.rglob("*.csv")
            if path.name in AGENT_STATE_FILENAMES
        ),
        key=_natural_path_key,
    )


def load_agent_states(source_path: Path) -> list[AgentState]:
    source_path = source_path.expanduser()
    if not source_path.exists():
        raise TrajectoryError(f"agent state file does not exist: {source_path}")

    rows: list[AgentState] = []
    with source_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, skipinitialspace=True)
        if reader.fieldnames is None:
            raise TrajectoryError(f"agent state file has no header: {source_path}")
        for line_number, raw_row in enumerate(reader, start=2):
            row = {
                (key or "").strip(): (value.strip() if isinstance(value, str) else value)
                for key, value in raw_row.items()
            }
            if not any(row.values()):
                continue
            try:
                rows.append(
                    AgentState(
                        step_index=_optional_int(_field(row, "step_index", "step", default=None)),
                        sim_time_ms=_optional_float(
                            _field(row, "sim_time_ms", "time_ms", "timestamp_ms", default=None)
                        ),
                        agent_id=str(_field(row, "agent_id", "id", "actor_id")),
                        x=_required_float(row, line_number, "x"),
                        y=_required_float(row, line_number, "y"),
                        speed=_optional_float(_field(row, "speed", "speed_mps", default=0.0)) or 0.0,
                    )
                )
            except ValueError as exc:
                raise TrajectoryError(f"{source_path}:{line_number}: {exc}") from exc
    return sorted(rows, key=_state_sort_key)


def load_run_info_for_agent_state_file(source_path: Path) -> RunInfo:
    result_path = source_path.parent / "result.csv"
    if not result_path.exists():
        return RunInfo(params={}, result={}, result_path=None)
    return load_run_info(result_path)


def load_run_info(result_path: Path) -> RunInfo:
    rows = _read_csv_dicts(result_path)
    if not rows:
        return RunInfo(params={}, result={}, result_path=result_path)
    row = rows[-1]
    params = _parse_params(row.get("run.params"))
    result = {}
    for key, value in row.items():
        if value in (None, "") or key == "run.params":
            continue
        result[key.removeprefix("run.") if key.startswith("run.") else key] = value
    return RunInfo(params=params, result=result, result_path=result_path)


def _read_csv_dicts(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, skipinitialspace=True)
        rows: list[dict[str, str]] = []
        for raw_row in reader:
            row = {
                (key or "").strip(): (value.strip() if isinstance(value, str) else "")
                for key, value in raw_row.items()
            }
            if any(row.values()):
                rows.append(row)
        return rows


def _parse_params(raw_params: str | None) -> dict[str, Any]:
    if raw_params in (None, ""):
        return {}
    try:
        params = json.loads(raw_params)
    except json.JSONDecodeError:
        return {"run.params": raw_params}
    if isinstance(params, dict):
        return params
    return {"run.params": params}


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
) -> str:
    if width < 500 or height < 360:
        raise TrajectoryError("SVG width/height are too small")
    if not states:
        raise TrajectoryError("no agent states to render")

    by_agent: dict[str, list[AgentState]] = defaultdict(list)
    for state in states:
        by_agent[state.agent_id].append(state)
    agent_ids = sorted(by_agent, key=_natural_key)
    speeds = [abs(state.speed) for state in states]
    min_speed = min(speeds)
    max_speed = max(speeds)

    xs = [state.x for state in states]
    ys = [state.y for state in states]
    min_x, max_x = x_range if x_range is not None else _expanded_range(min(xs), max(xs))
    min_y, max_y = y_range if y_range is not None else _expanded_range(min(ys), max(ys))

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
        margin_left, margin_top, plot_width, plot_height = _equal_scale_plot_area(
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

    parts = [_svg_header(width, height)]
    parts.append(_svg_rect(0, 0, width, height, "#ffffff"))
    parts.append(_svg_text(width / 2, 30, title, size=19, weight="700", anchor="middle"))
    parts.extend(
        _axes(margin_left, margin_top, plot_width, plot_height, min_x, max_x, min_y, max_y)
    )

    for index, agent_id in enumerate(agent_ids):
        color = AGENT_COLORS[index % len(AGENT_COLORS)]
        agent_states = by_agent[agent_id]
        if len(agent_states) == 1:
            state = agent_states[0]
            parts.append(
                f'<circle cx="{sx(state.x):.2f}" cy="{sy(state.y):.2f}" r="4.5" '
                f'fill="{color}" fill-opacity="{_speed_opacity(state.speed, min_speed, max_speed):.3f}">'
                f"<title>{_escape(f'agent {agent_id}, speed {state.speed:g}')}</title></circle>"
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
                f"<title>{_escape(f'agent {agent_id}, speed {segment_speed:g}')}</title></line>"
            )
        start = agent_states[0]
        end = agent_states[-1]
        parts.append(
            f'<circle cx="{sx(start.x):.2f}" cy="{sy(start.y):.2f}" r="3.4" '
            f'fill="#ffffff" stroke="{color}" stroke-width="2"><title>{_escape(f"agent {agent_id} start")}</title></circle>'
        )
        parts.append(
            f'<circle cx="{sx(end.x):.2f}" cy="{sy(end.y):.2f}" r="4.2" '
            f'fill="{color}"><title>{_escape(f"agent {agent_id} end")}</title></circle>'
        )

    parts.extend(
        _side_panel(
            agent_ids,
            x=width - legend_width + 4,
            y=margin_top,
            min_speed=min_speed,
            max_speed=max_speed,
            run_info=run_info,
        )
    )
    parts.append("</svg>")
    return "\n".join(parts)


def _field(row: dict[str, Any], *names: str, default: Any = ...):
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    if default is not ...:
        return default
    raise ValueError(f"missing required column: {'/'.join(names)}")


def _required_float(row: dict[str, Any], line_number: int, name: str) -> float:
    value = _field(row, name)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"column {name!r} must be numeric, got {value!r} on row {line_number}") from exc


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(float(value))


def _state_sort_key(state: AgentState) -> tuple[str, float, int, float, float]:
    time_value = state.sim_time_ms if state.sim_time_ms is not None else math.inf
    step_value = state.step_index if state.step_index is not None else 10**12
    return (state.agent_id, time_value, step_value, state.x, state.y)


def _speed_opacity(speed: float, min_speed: float, max_speed: float) -> float:
    if max_speed <= min_speed:
        return 0.78
    normalized = (abs(speed) - min_speed) / (max_speed - min_speed)
    return 0.18 + 0.82 * max(0.0, min(1.0, normalized))


def _expanded_range(min_value: float, max_value: float) -> tuple[float, float]:
    if math.isclose(min_value, max_value):
        delta = max(1.0, abs(min_value) * 0.1)
        return min_value - delta, max_value + delta
    padding = (max_value - min_value) * 0.06
    return min_value - padding, max_value + padding


def _validate_range(value: tuple[float, float] | None, *, label: str) -> None:
    if value is None:
        return
    if not math.isfinite(value[0]) or not math.isfinite(value[1]):
        raise TrajectoryError(f"{label} values must be finite")
    if value[0] >= value[1]:
        raise TrajectoryError(f"{label} min must be smaller than max")


def _equal_scale_plot_area(
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
        f'<rect id="trajectory-plot-area" x="{margin_left:.2f}" y="{margin_top:.2f}" width="{plot_width:.2f}" height="{plot_height:.2f}" fill="#f8fafc" stroke="#d1d5db"/>',
    ]
    for tick in range(6):
        fraction = tick / 5
        x = margin_left + fraction * plot_width
        y = margin_top + plot_height - fraction * plot_height
        x_value = min_x + fraction * (max_x - min_x)
        y_value = min_y + fraction * (max_y - min_y)
        parts.append(f'<line x1="{x:.2f}" y1="{margin_top}" x2="{x:.2f}" y2="{y0}" stroke="#e5e7eb"/>')
        parts.append(f'<line x1="{x0}" y1="{y:.2f}" x2="{x0 + plot_width}" y2="{y:.2f}" stroke="#e5e7eb"/>')
        parts.append(_svg_text(x, y0 + 24, _format_number(x_value), size=11, anchor="middle"))
        parts.append(_svg_text(x0 - 14, y + 4, _format_number(y_value), size=11, anchor="end"))
    parts.append(_svg_text(margin_left + plot_width / 2, y0 + 50, "x", size=13, weight="700", anchor="middle"))
    parts.append(_svg_text(18, margin_top + plot_height / 2, "y", size=13, weight="700", anchor="middle", rotate=-90))
    return parts


def _side_panel(
    agent_ids: list[str],
    *,
    x: float,
    y: float,
    min_speed: float,
    max_speed: float,
    run_info: RunInfo | None,
) -> list[str]:
    parts = [
        f'<g transform="translate({x:.2f},{y:.2f})">',
        _svg_text(0, 0, "Agents", size=15, weight="700"),
    ]
    cursor_y = 24
    for index, agent_id in enumerate(agent_ids):
        color = AGENT_COLORS[index % len(AGENT_COLORS)]
        parts.append(f'<line x1="0" y1="{cursor_y}" x2="32" y2="{cursor_y}" stroke="{color}" stroke-width="4" stroke-linecap="round"/>')
        parts.append(_svg_text(42, cursor_y + 4, f"agent {agent_id}", size=12))
        cursor_y += 22
    cursor_y += 12
    parts.append(_svg_text(0, cursor_y, "Speed opacity", size=15, weight="700"))
    cursor_y += 20
    for offset, opacity in enumerate((0.18, 0.48, 1.0)):
        y0 = cursor_y + offset * 18
        parts.append(f'<line x1="0" y1="{y0}" x2="32" y2="{y0}" stroke="#111827" stroke-width="4" stroke-opacity="{opacity:.2f}" stroke-linecap="round"/>')
    parts.append(_svg_text(42, cursor_y + 4, f"slow {_format_number(min_speed)}", size=12))
    parts.append(_svg_text(42, cursor_y + 22, "medium", size=12))
    parts.append(_svg_text(42, cursor_y + 40, f"fast {_format_number(max_speed)}", size=12))
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
    parts.append(_svg_text(0, cursor_y, title, size=15, weight="700"))
    cursor_y += 20
    for index, (key, value) in enumerate(values.items()):
        if index >= max_items:
            parts.append(_svg_text(0, cursor_y, f"... {len(values) - max_items} more", size=11))
            cursor_y += 18
            break
        for line in _wrap_text(f"{key}: {_format_panel_value(value)}", max_chars=34):
            parts.append(_svg_text(0, cursor_y, line, size=11))
            cursor_y += 15
        cursor_y += 2
    return cursor_y + 12


def _format_panel_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True)
    return str(value)


def _wrap_text(text: str, *, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    words = text.split()
    if not words:
        return [text[:max_chars]]
    lines: list[str] = []
    current = ""
    for word in words:
        if len(word) > max_chars:
            if current:
                lines.append(current)
                current = ""
            lines.extend(word[index : index + max_chars] for index in range(0, len(word), max_chars))
            continue
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= max_chars:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _svg_header(width: int, height: int) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'


def _svg_rect(x: float, y: float, width: float, height: float, fill: str) -> str:
    return f'<rect x="{x:g}" y="{y:g}" width="{width:g}" height="{height:g}" fill="{fill}"/>'


def _svg_text(
    x: float,
    y: float,
    text: Any,
    *,
    size: int = 12,
    anchor: str = "start",
    weight: str = "400",
    rotate: int | None = None,
) -> str:
    transform = f' transform="rotate({rotate} {x:.2f} {y:.2f})"' if rotate is not None else ""
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" font-family="Inter, Arial, sans-serif" '
        f'font-size="{size}" font-weight="{weight}" text-anchor="{anchor}" '
        f'fill="#111827"{transform}>{_escape(text)}</text>'
    )


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _format_number(value: float) -> str:
    return f"{value:.3g}"


def _natural_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", str(value))]


def _natural_path_key(path: Path) -> list[Any]:
    return _natural_key(str(path))


def _prepare_output_dir(output_dir: Path, *, overwrite: bool) -> None:
    output_dir = output_dir.expanduser()
    if output_dir.exists():
        if not output_dir.is_dir():
            raise TrajectoryError(f"output path exists and is not a directory: {output_dir}")
        if not overwrite:
            raise TrajectoryError(f"output directory already exists: {output_dir}")
        if not any(output_dir.iterdir()):
            return
        _clear_previous_output(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)


def _clear_previous_output(output_dir: Path) -> None:
    manifest_path = output_dir / "manifest.yaml"
    if not manifest_path.exists():
        raise TrajectoryError(
            "output directory already exists and is not empty, but no manifest.yaml was found; "
            "refusing to overwrite non-tool output"
        )
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise TrajectoryError(f"could not read existing manifest.yaml: {exc}") from exc
    outputs = manifest.get("outputs")
    if not isinstance(outputs, list):
        raise TrajectoryError("existing manifest.yaml does not look like trajectory tool output")
    for output in outputs:
        if not isinstance(output, dict):
            continue
        svg_path = Path(str(output.get("svg_path", "")))
        if svg_path.exists() and svg_path.is_file() and _is_relative_to(svg_path, output_dir):
            svg_path.unlink()
    manifest_path.unlink()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _write_manifest(
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
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")


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
            return f"{_slug(parts[-3] if len(parts) >= 3 else source_file.stem)}_trajectory"
        return f"{_slug('_'.join(parts[:-1]) or source_file.stem)}_trajectory"
    return f"{_slug(str(relative))}_trajectory"


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return slug.strip("._") or "trajectory"
