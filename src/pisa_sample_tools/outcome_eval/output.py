from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from simcore.conditions import ConditionCode

from pisa_sample_tools.common.csv import write_csv_rows
from pisa_sample_tools.common.output import is_relative_to, prepare_manifest_output_dir
from pisa_sample_tools.common.yaml import write_yaml

from .models import OutcomeEvalError, OutcomeEvalMode, ScenarioOutcome


def write_monitor_outcome(outcome: ScenarioOutcome) -> ScenarioOutcome:
    output_path = outcome.monitor_path / "offline_outcome.csv"
    write_outcome_csv_rows(
        output_path,
        [
            {
                "run.analysis_test_outcome": outcome.test_outcome,
                "run.analysis_stop_condition": outcome.stop_condition,
                "run.analysis_stop_reason": outcome.stop_reason,
                "run.analysis_condition_code": condition_code_label(outcome.code),
                "run.analysis_condition_name": outcome.condition_name,
                "run.analysis_triggered": str(outcome.triggered).lower(),
            }
        ],
    )
    return ScenarioOutcome(
        scenario_path=outcome.scenario_path,
        monitor_path=outcome.monitor_path,
        condition_name=outcome.condition_name,
        code=outcome.code,
        test_outcome=outcome.test_outcome,
        stop_condition=outcome.stop_condition,
        stop_reason=outcome.stop_reason,
        detail=outcome.detail,
        triggered=outcome.triggered,
        output_path=output_path,
    )


def write_summary_csv(path: Path, outcomes: list[ScenarioOutcome]) -> None:
    write_outcome_csv_rows(
        path,
        [
            {
                "scenario_path": outcome.scenario_path,
                "monitor_path": outcome.monitor_path,
                "test_outcome": outcome.test_outcome,
                "stop_condition": outcome.stop_condition,
                "condition_code": condition_code_label(outcome.code),
                "condition_name": outcome.condition_name,
                "triggered": str(outcome.triggered).lower(),
                "detail": outcome.detail,
                "offline_outcome_path": outcome.output_path or "",
            }
            for outcome in outcomes
        ],
    )


def write_outcome_csv_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    write_csv_rows(path, rows, error_type=OutcomeEvalError)


def write_manifest(
    path: Path,
    *,
    input_path: Path,
    config_path: Path,
    mode: OutcomeEvalMode,
    default_outcome: str,
    write_monitor_outcome: bool,
    outcomes: list[ScenarioOutcome],
    summary_csv_path: Path,
) -> None:
    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "input_path": str(input_path),
        "config_path": str(config_path),
        "mode": mode.value,
        "default_outcome": default_outcome,
        "write_monitor_outcome": write_monitor_outcome,
        "scenario_count": len(outcomes),
        "triggered_count": sum(outcome.triggered for outcome in outcomes),
        "summary_csv_path": str(summary_csv_path),
        "outcomes": [
            {
                "scenario_path": str(outcome.scenario_path),
                "test_outcome": outcome.test_outcome,
                "stop_condition": outcome.stop_condition,
                "condition_code": condition_code_label(outcome.code),
                "condition_name": outcome.condition_name,
                "triggered": outcome.triggered,
                "detail": outcome.detail,
                "offline_outcome_path": str(outcome.output_path) if outcome.output_path else None,
            }
            for outcome in outcomes
        ],
    }
    write_yaml(path, manifest)


def prepare_output_dir(output_dir: Path, *, overwrite: bool) -> None:
    prepare_manifest_output_dir(
        output_dir,
        overwrite=overwrite,
        error_type=OutcomeEvalError,
        tool_label="outcome eval",
        validate_manifest=lambda manifest: "outcomes" in manifest and "summary_csv_path" in manifest,
        clear_previous=clear_previous_output,
    )


def clear_previous_output(output_dir: Path, manifest: dict[str, Any]) -> None:
    summary_csv_path = Path(str(manifest.get("summary_csv_path", "")))
    if summary_csv_path.exists() and summary_csv_path.is_file() and is_relative_to(summary_csv_path, output_dir):
        summary_csv_path.unlink()


def condition_code_label(code: ConditionCode) -> str:
    if code == ConditionCode.TRIGGERED:
        return "triggered"
    if code == ConditionCode.NOT_TRIGGERED:
        return "not_triggered"
    return "not_evaluated"
