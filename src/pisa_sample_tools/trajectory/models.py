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

