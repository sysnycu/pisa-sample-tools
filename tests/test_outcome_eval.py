from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pytest
import yaml

from pisa_sample_tools.outcome_eval import OutcomeEvalError, evaluate_outcomes
from pisa_sample_tools.outcome_eval_cli import main


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _write_yaml(path: Path, data: Any) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _write_iteration(root: Path, name: str = "iteration_1") -> Path:
    iteration = root / name
    monitor = iteration / "monitor"
    _write_csv(
        monitor / "agent_states.csv",
        [
            {"step_index": 0, "sim_time_ms": 0, "agent_id": 0, "x": 0.0, "y": 0.0, "speed": 10},
            {"step_index": 1, "sim_time_ms": 50, "agent_id": 0, "x": 101.0, "y": 0.0, "speed": 10},
            {"step_index": 0, "sim_time_ms": 0, "agent_id": 1, "x": 2.0, "y": 0.0, "speed": 8},
            {"step_index": 1, "sim_time_ms": 50, "agent_id": 1, "x": 3.0, "y": 0.0, "speed": 8},
        ],
    )
    _write_csv(
        monitor / "frame_metrics.csv",
        [
            {"step_index": 0, "ego_to_agent_1.ttc_s": 2.0, "ego.speed": 10.0},
            {"step_index": 1, "ego_to_agent_1.ttc_s": 0.8, "ego.speed": 12.0},
        ],
    )
    _write_csv(
        monitor / "result.csv",
        [
            {
                "run.status": "finished",
                "run.test_outcome": "success",
                "ego_to_agent_1.min_ttc_s": 0.8,
                "ego.max_speed_mps": 12.0,
            }
        ],
    )
    return iteration


def test_agent_state_threshold_can_mark_out_of_range_failure(tmp_path: Path) -> None:
    iteration = _write_iteration(tmp_path / "results")
    config = tmp_path / "conditions.yaml"
    _write_yaml(
        config,
        {
            "condition": {
                "type": "agent_state_threshold",
                "name": "agent_0_x_out_of_range",
                "outcome": "Fail",
                "agent_id": 0,
                "metric": "x",
                "op": "outside",
                "min": -10,
                "max": 100,
            }
        },
    )

    result = evaluate_outcomes(
        input_path=iteration,
        config_path=config,
        output_dir=tmp_path / "analysis",
    )

    assert result.outcomes[0].triggered is True
    assert result.outcomes[0].test_outcome == "fail"
    assert result.outcomes[0].stop_condition == "agent_0_x_out_of_range"
    summary = (tmp_path / "analysis" / "offline_outcomes.csv").read_text(encoding="utf-8")
    assert "agent_0_x_out_of_range" in summary


def test_frame_metric_threshold_can_mark_ttc_failure(tmp_path: Path) -> None:
    iteration = _write_iteration(tmp_path / "results")
    config = tmp_path / "conditions.yaml"
    _write_yaml(
        config,
        [
            {
                "type": "frame_metric_threshold",
                "name": "low_frame_ttc",
                "outcome": "Fail",
                "metric": "ego_to_agent_1.ttc_s",
                "op": "<",
                "value": 1.0,
            }
        ],
    )

    result = evaluate_outcomes(
        input_path=iteration,
        config_path=config,
        output_dir=tmp_path / "analysis",
    )

    assert result.outcomes[0].test_outcome == "fail"
    assert "low_frame_ttc triggered" in result.outcomes[0].stop_reason


def test_threshold_supports_runner_rule_value_format(tmp_path: Path) -> None:
    iteration = _write_iteration(tmp_path / "results")
    config = tmp_path / "conditions.yaml"
    _write_yaml(
        config,
        [
            {
                "type": "frame_metric_threshold",
                "name": "low_frame_ttc",
                "outcome": "Fail",
                "metric": "ego_to_agent_1.ttc_s",
                "rule": "lt",
                "value": 1.0,
            }
        ],
    )

    result = evaluate_outcomes(
        input_path=iteration,
        config_path=config,
        output_dir=tmp_path / "analysis",
    )

    assert result.outcomes[0].test_outcome == "fail"


def test_frame_metric_expression_uses_runner_expression_evaluator(tmp_path: Path) -> None:
    iteration = _write_iteration(tmp_path / "results")
    config = tmp_path / "conditions.yaml"
    _write_yaml(
        config,
        {
            "condition": {
                "type": "frame_metric_expression",
                "name": "speed_delta_expr",
                "outcome": "Fail",
                "expression": "abs(speed - baseline)",
                "rule": "gt",
                "value": 0.5,
                "variables": {
                    "speed": "ego.speed",
                    "baseline": "ego_to_agent_1.ttc_s",
                },
            }
        },
    )

    result = evaluate_outcomes(
        input_path=iteration,
        config_path=config,
        output_dir=tmp_path / "analysis",
    )

    assert result.outcomes[0].triggered is True
    assert result.outcomes[0].stop_condition == "speed_delta_expr"


def test_agent_pair_expression_compares_two_agents(tmp_path: Path) -> None:
    iteration = _write_iteration(tmp_path / "results")
    config = tmp_path / "conditions.yaml"
    _write_yaml(
        config,
        {
            "condition": {
                "type": "agent_pair_expression",
                "name": "agent_0_far_ahead_of_agent_1",
                "outcome": "Invalid",
                "source_agent_id": 0,
                "target_agent_id": 1,
                "expression": "source_x - target_x",
                "rule": "gt",
                "value": 90.0,
            }
        },
    )

    result = evaluate_outcomes(
        input_path=iteration,
        config_path=config,
        output_dir=tmp_path / "analysis",
    )

    assert result.outcomes[0].test_outcome == "invalid"
    assert result.outcomes[0].stop_condition == "agent_0_far_ahead_of_agent_1"


def test_result_metric_threshold_can_mark_ttc_failure(tmp_path: Path) -> None:
    iteration = _write_iteration(tmp_path / "results")
    config = tmp_path / "conditions.yaml"
    _write_yaml(
        config,
        {
            "condition": {
                "type": "result_metric_threshold",
                "name": "low_summary_ttc",
                "outcome": "Fail",
                "metric": "ego_to_agent_1.min_ttc_s",
                "op": "<",
                "value": 1.0,
            }
        },
    )

    result = evaluate_outcomes(
        input_path=iteration,
        config_path=config,
        output_dir=tmp_path / "analysis",
    )

    assert result.outcomes[0].test_outcome == "fail"
    assert result.outcomes[0].stop_condition == "low_summary_ttc"


def test_replace_mode_uses_unknown_when_no_condition_triggers(tmp_path: Path) -> None:
    iteration = _write_iteration(tmp_path / "results")
    config = tmp_path / "conditions.yaml"
    _write_yaml(
        config,
        {
            "condition": {
                "type": "result_metric_threshold",
                "name": "very_low_summary_ttc",
                "outcome": "Fail",
                "metric": "ego_to_agent_1.min_ttc_s",
                "rule": "lt",
                "value": 0.1,
            }
        },
    )

    result = evaluate_outcomes(
        input_path=iteration,
        config_path=config,
        output_dir=tmp_path / "analysis",
    )

    assert result.outcomes[0].triggered is False
    assert result.outcomes[0].test_outcome == "unknown"


def test_overlay_mode_keeps_original_outcome_when_no_condition_triggers(tmp_path: Path) -> None:
    iteration = _write_iteration(tmp_path / "results")
    config = tmp_path / "conditions.yaml"
    _write_yaml(
        config,
        {
            "condition": {
                "type": "result_metric_threshold",
                "name": "very_low_summary_ttc",
                "outcome": "Fail",
                "metric": "ego_to_agent_1.min_ttc_s",
                "rule": "lt",
                "value": 0.1,
            }
        },
    )

    result = evaluate_outcomes(
        input_path=iteration,
        config_path=config,
        output_dir=tmp_path / "analysis",
        mode="overlay",
    )

    assert result.outcomes[0].triggered is False
    assert result.outcomes[0].test_outcome == "success"


def test_batch_evaluation_writes_one_row_per_iteration(tmp_path: Path) -> None:
    results = tmp_path / "results"
    _write_iteration(results, "iteration_1")
    _write_iteration(results, "iteration_2")
    config = tmp_path / "conditions.yaml"
    _write_yaml(
        config,
        {
            "condition": {
                "type": "result_metric_threshold",
                "name": "low_summary_ttc",
                "outcome": "Fail",
                "metric": "ego_to_agent_1.min_ttc_s",
                "op": "<",
                "value": 1.0,
            }
        },
    )

    result = evaluate_outcomes(
        input_path=results,
        config_path=config,
        output_dir=tmp_path / "analysis",
    )

    assert len(result.outcomes) == 2
    manifest = yaml.safe_load((tmp_path / "analysis" / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["scenario_count"] == 2
    assert manifest["triggered_count"] == 2


def test_missing_metric_column_errors(tmp_path: Path) -> None:
    iteration = _write_iteration(tmp_path / "results")
    config = tmp_path / "conditions.yaml"
    _write_yaml(
        config,
        {
            "condition": {
                "type": "frame_metric_threshold",
                "name": "missing_metric",
                "outcome": "Fail",
                "metric": "missing.column",
                "op": "<",
                "value": 1.0,
            }
        },
    )

    with pytest.raises(OutcomeEvalError, match="required column 'missing.column' not found"):
        evaluate_outcomes(
            input_path=iteration,
            config_path=config,
            output_dir=tmp_path / "analysis",
        )


def test_cli_can_write_monitor_outcome_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    iteration = _write_iteration(tmp_path / "results")
    config = tmp_path / "conditions.yaml"
    _write_yaml(
        config,
        {
            "condition": {
                "type": "result_metric_threshold",
                "name": "low_summary_ttc",
                "outcome": "Fail",
                "metric": "ego_to_agent_1.min_ttc_s",
                "op": "<",
                "value": 1.0,
            }
        },
    )

    assert (
        main(
            [
                "--input",
                str(iteration),
                "--config",
                str(config),
                "--output-dir",
                str(tmp_path / "analysis"),
                "--write-monitor-outcome",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert "scenario_count: 1" in captured.out
    outcome_csv = iteration / "monitor" / "offline_outcome.csv"
    assert outcome_csv.exists()
    assert "run.analysis_test_outcome" in outcome_csv.read_text(encoding="utf-8")
