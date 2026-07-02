from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

from pisa_sample_tools.common.csv import read_csv_dicts
from pisa_sample_tools.common.sorting import natural_path_key

from .models import AGENT_STATE_FILENAMES, AgentGeometry, AgentState, RunInfo, TrajectoryError


def discover_agent_state_files(input_path: Path) -> list[Path]:
    input_path = input_path.expanduser()
    if input_path.is_file():
        if input_path.name not in AGENT_STATE_FILENAMES:
            raise TrajectoryError(
                f"input file must be agent_state.csv or agent_states.csv: {input_path}"
            )
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
    for iteration_dir in sorted(input_path.glob("iteration_*"), key=natural_path_key):
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
        (path for path in input_path.rglob("*.csv") if path.name in AGENT_STATE_FILENAMES),
        key=natural_path_key,
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
                        speed=_optional_float(_field(row, "speed", "speed_mps", default=0.0))
                        or 0.0,
                        yaw=_optional_float(_field(row, "yaw", default=0.0)) or 0.0,
                        entity_name=_optional_text(
                            _field(row, "entity_name", "agent_name", default=None)
                        ),
                        sim_tracking_id=_optional_text(
                            _field(row, "sim_tracking_id", default=None)
                        ),
                        is_ego=_optional_bool(_field(row, "is_ego", default=None)),
                    )
                )
            except ValueError as exc:
                raise TrajectoryError(f"{source_path}:{line_number}: {exc}") from exc
    return sorted(rows, key=state_sort_key)


def load_agent_geometry_for_state_file(source_path: Path) -> list[AgentGeometry]:
    path = source_path.parent / "agent_geometry.csv"
    if not path.exists():
        return []
    output: list[AgentGeometry] = []
    for line_number, row in enumerate(read_csv_dicts(path), start=2):
        try:
            output.append(
                AgentGeometry(
                    agent_id=str(_field(row, "agent_id", "actor_id")),
                    step_index=_optional_int(row.get("step_index")),
                    sim_time_ms=_optional_float(row.get("sim_time_ms")),
                    entity_name=_optional_text(row.get("entity_name") or row.get("agent_name")),
                    sim_tracking_id=_optional_text(row.get("sim_tracking_id")),
                    is_ego=_optional_bool(row.get("is_ego")),
                    shape_type=_optional_text(row.get("shape_type")),
                    length_m=_optional_float(row.get("length_m")),
                    width_m=_optional_float(row.get("width_m")),
                    height_m=_optional_float(row.get("height_m")),
                    reference_point=_optional_text(row.get("reference_point")),
                    center_offset_x=_optional_float(row.get("center_offset_x")) or 0.0,
                    center_offset_y=_optional_float(row.get("center_offset_y")) or 0.0,
                    center_offset_z=_optional_float(row.get("center_offset_z")) or 0.0,
                    roll_offset=_optional_float(row.get("roll_offset")) or 0.0,
                    pitch_offset=_optional_float(row.get("pitch_offset")) or 0.0,
                    yaw_offset=_optional_float(row.get("yaw_offset")) or 0.0,
                    footprint=_parse_footprint(row.get("footprint_json")),
                    source=_optional_text(row.get("source")),
                )
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TrajectoryError(f"{path}:{line_number}: invalid geometry: {exc}") from exc
    return sorted(
        output, key=lambda item: (item.agent_id, item.sim_time_ms or 0, item.step_index or 0)
    )


def load_run_info_for_agent_state_file(source_path: Path) -> RunInfo:
    result_path = source_path.parent / "result.csv"
    if not result_path.exists():
        return RunInfo(params={}, result={}, result_path=None)
    return load_run_info(result_path)


def load_run_info(result_path: Path) -> RunInfo:
    rows = read_csv_dicts(result_path)
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


def state_sort_key(state: AgentState) -> tuple[str, float, int, float, float]:
    time_value = state.sim_time_ms if state.sim_time_ms is not None else math.inf
    step_value = state.step_index if state.step_index is not None else 10**12
    return (state.agent_id, time_value, step_value, state.x, state.y)


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
        raise ValueError(
            f"column {name!r} must be numeric, got {value!r} on row {line_number}"
        ) from exc


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(float(value))


def _optional_text(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _optional_bool(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ValueError(f"invalid boolean {value!r}")


def _parse_footprint(value: Any) -> tuple[tuple[float, float], ...]:
    if value in (None, ""):
        return ()
    raw = json.loads(str(value))
    if isinstance(raw, dict):
        raw = raw.get("points") or raw.get("vertices")
    if not isinstance(raw, list):
        raise ValueError("footprint_json must be a list of points")
    points = []
    for point in raw:
        if isinstance(point, dict):
            points.append((float(point["x"]), float(point["y"])))
        elif isinstance(point, (list, tuple)) and len(point) >= 2:
            points.append((float(point[0]), float(point[1])))
        else:
            raise ValueError("footprint_json contains an invalid point")
    return tuple(points)
