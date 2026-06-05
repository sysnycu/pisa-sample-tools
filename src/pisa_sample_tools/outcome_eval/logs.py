from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pisa_sample_tools.common.csv import read_csv_dicts_required
from pisa_sample_tools.common.sorting import natural_path_key

from .models import OutcomeEvalError


@dataclass
class ScenarioLog:
    scenario_path: Path
    monitor_path: Path
    _agent_state_rows: list[dict[str, str]] | None = None
    _frame_metric_rows: list[dict[str, str]] | None = None
    _result_rows: list[dict[str, str]] | None = None

    @property
    def agent_states_path(self) -> Path:
        return self.monitor_path / "agent_states.csv"

    @property
    def frame_metrics_path(self) -> Path:
        return self.monitor_path / "frame_metrics.csv"

    @property
    def result_path(self) -> Path:
        return self.monitor_path / "result.csv"

    def agent_state_rows(self) -> list[dict[str, str]]:
        if self._agent_state_rows is None:
            self._agent_state_rows = read_csv_dicts_required(
                self.agent_states_path,
                error_type=OutcomeEvalError,
            )
        return self._agent_state_rows

    def frame_metric_rows(self) -> list[dict[str, str]]:
        if self._frame_metric_rows is None:
            self._frame_metric_rows = read_csv_dicts_required(
                self.frame_metrics_path,
                error_type=OutcomeEvalError,
            )
        return self._frame_metric_rows

    def result_rows(self) -> list[dict[str, str]]:
        if self._result_rows is None:
            self._result_rows = read_csv_dicts_required(self.result_path, error_type=OutcomeEvalError)
        return self._result_rows


def discover_scenario_logs(input_path: Path) -> list[ScenarioLog]:
    input_path = input_path.expanduser()
    if not input_path.exists():
        raise OutcomeEvalError(f"input path does not exist: {input_path}")
    if input_path.is_file():
        raise OutcomeEvalError("input must be an iteration directory or runner result directory")
    if monitor_path(input_path).exists():
        return [ScenarioLog(scenario_path=input_path, monitor_path=monitor_path(input_path))]
    logs: list[ScenarioLog] = []
    for iteration_dir in sorted(input_path.glob("iteration_*"), key=natural_path_key):
        monitor = monitor_path(iteration_dir)
        if iteration_dir.is_dir() and monitor.exists():
            logs.append(ScenarioLog(scenario_path=iteration_dir, monitor_path=monitor))
    return logs


def monitor_path(path: Path) -> Path:
    return path / "monitor"
