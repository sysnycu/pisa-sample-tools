from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    yaw: float = 0.0
    entity_name: str | None = None
    sim_tracking_id: str | None = None
    is_ego: bool | None = None


@dataclass(frozen=True)
class AgentGeometry:
    agent_id: str
    step_index: int | None = None
    sim_time_ms: float | None = None
    entity_name: str | None = None
    sim_tracking_id: str | None = None
    is_ego: bool | None = None
    shape_type: str | None = None
    length_m: float | None = None
    width_m: float | None = None
    height_m: float | None = None
    reference_point: str | None = None
    center_offset_x: float = 0.0
    center_offset_y: float = 0.0
    center_offset_z: float = 0.0
    roll_offset: float = 0.0
    pitch_offset: float = 0.0
    yaw_offset: float = 0.0
    footprint: tuple[tuple[float, float], ...] = ()
    source: str | None = None


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
    origin_agent_id: str | None = None
    origin_x: float | None = None
    origin_y: float | None = None


@dataclass(frozen=True)
class TrajectoryBatchResult:
    output_dir: Path
    manifest_path: Path
    results: list[TrajectorySvgResult]
