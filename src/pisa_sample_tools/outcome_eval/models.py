from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from simcore.conditions import ConditionCode


class OutcomeEvalError(ValueError):
    """Raised for user-facing offline outcome evaluation failures."""


class OutcomeEvalMode(StrEnum):
    OVERLAY = "overlay"
    REPLACE = "replace"


OUTCOME_ALIASES = {
    "success": "success",
    "succeed": "success",
    "pass": "success",
    "passed": "success",
    "ok": "success",
    "fail": "fail",
    "failure": "fail",
    "failed": "fail",
    "invalid": "invalid",
}


@dataclass(frozen=True)
class ScenarioOutcome:
    scenario_path: Path
    monitor_path: Path
    condition_name: str
    code: ConditionCode
    test_outcome: str
    stop_condition: str
    stop_reason: str
    detail: str
    triggered: bool
    output_path: Path | None = None


@dataclass(frozen=True)
class OutcomeEvalResult:
    input_path: Path
    output_dir: Path
    manifest_path: Path
    summary_csv_path: Path
    outcomes: list[ScenarioOutcome]

