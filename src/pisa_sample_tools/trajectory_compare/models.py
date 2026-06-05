from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .stats import mean, weighted_mean


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
        return weighted_mean((agent.ade, agent.compared_steps) for agent in self.agents)

    @property
    def fde(self) -> float | None:
        return mean(agent.fde for agent in self.agents)

    @property
    def rmse(self) -> float | None:
        return weighted_mean((agent.rmse, agent.compared_steps) for agent in self.agents)

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

