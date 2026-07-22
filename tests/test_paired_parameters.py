from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pisa_sample_tools.reporting.paired_parameters import (
    PairedMetricAgreementError,
    PairedParameterError,
    analyze_paired_metric_agreement,
    analyze_paired_parameters,
    build_portable_paired_parameter_summary,
    comparison_identifier,
)


def _database(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "index.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE datasets (dataset_id TEXT PRIMARY KEY);
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, parameter_hash TEXT,
            outcome_class TEXT NOT NULL, stop_condition TEXT, stop_reason TEXT
        );
        CREATE TABLE parameters (
            run_id TEXT NOT NULL, name TEXT NOT NULL, value_real REAL,
            value_type TEXT NOT NULL, PRIMARY KEY(run_id, name)
        );
        CREATE TABLE metrics (
            run_id TEXT NOT NULL, name TEXT NOT NULL, value_real REAL,
            value_type TEXT NOT NULL, PRIMARY KEY(run_id, name)
        );
        CREATE TABLE dataset_relations (
            left_dataset_id TEXT NOT NULL, right_dataset_id TEXT NOT NULL,
            role TEXT NOT NULL, details_json TEXT NOT NULL,
            PRIMARY KEY(left_dataset_id, right_dataset_id)
        );
        """
    )
    connection.executemany("INSERT INTO datasets VALUES (?)", [("left",), ("right",)])
    left_outcomes = ["success", "success", "fail", "fail", "invalid", "success"]
    right_outcomes = ["success", "fail", "success", "fail", "fail", "success"]
    for index, (left_outcome, right_outcome) in enumerate(
        zip(left_outcomes, right_outcomes, strict=True)
    ):
        parameter_hash = f"hash-{index}"
        for dataset, outcome in (("left", left_outcome), ("right", right_outcome)):
            run_id = f"{dataset}:{index}"
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    dataset,
                    parameter_hash,
                    outcome,
                    "complete" if index != 3 else "collision",
                    f"reason-{dataset}" if index == 3 else "done",
                ),
            )
            connection.executemany(
                "INSERT INTO parameters VALUES (?, ?, ?, 'number')",
                [
                    (run_id, "Ego_Speed", 10.0 + index),
                    (run_id, "Agent_Speed", 5.0 + 2 * index),
                    (run_id, "Relative_Distance", 20.0 + index),
                ],
            )
            metrics = [
                (run_id, "ego_to_agent_1_distance.min", 10.0 - index + (1 if dataset == "right" else 0)),
                (run_id, "control.max_brake", 0.1 * index),
            ]
            if not (dataset == "right" and index == 4):
                metrics.append((run_id, "min_ttc.min_ttc_s", 5.0 - 0.5 * index))
            connection.executemany(
                "INSERT INTO metrics VALUES (?, ?, ?, 'number')", metrics
            )
    connection.execute(
        "INSERT INTO dataset_relations VALUES ('left', 'right', 'paired_policy_intervention', ?)",
        (json.dumps({"matched_count": 6}),),
    )
    connection.commit()
    connection.close()
    return path, comparison_identifier("left", "right")


def test_paired_parameter_analysis_maps_outcomes_to_original_parameters(
    tmp_path: Path,
) -> None:
    path, relation_id = _database(tmp_path)

    result = analyze_paired_parameters(
        path,
        relation_id,
        {
            "x": "Ego_Speed",
            "y": "Agent_Speed",
            "facet": "Relative_Distance",
            "bin_count": 2,
            "minimum_cell_count": 1,
        },
    )

    assert result["overview"]["paired_count"] == 6
    assert result["overview"]["disagreement_count"] == 3
    assert result["overview"]["direct_reversal_count"] == 2
    assert result["overview"]["invalid_related_count"] == 1
    assert result["selection"]["delta_definition"] == "right minus left"
    assert result["disclosure"]["derived_parameters_used"] is False
    assert sum(cell["total"] for cell in result["marginals"][0]["bins"]) == 6
    assert result["marginals"][0]["bins"][-1]["upper_inclusive"] is True
    assert result["observations"]


def test_metric_delta_keeps_missing_coverage_and_excludes_raw_controls(
    tmp_path: Path,
) -> None:
    path, relation_id = _database(tmp_path)

    result = analyze_paired_parameters(
        path,
        relation_id,
        {
            "view": "metric_delta",
            "metric": "min_ttc.min_ttc_s",
            "minimum_cell_count": 1,
        },
    )

    assert "control.max_brake" not in result["metrics"]
    assert result["overview"]["metric_eligible_count"] == 5
    assert result["overview"]["metric_missing_count"] == 1
    assert all(point["delta"] == pytest.approx(0) for point in result["points"] if point["delta"] is not None)


def test_metric_agreement_uses_same_outcomes_and_exact_inclusive_thresholds(
    tmp_path: Path,
) -> None:
    path, relation_id = _database(tmp_path)
    connection = sqlite3.connect(path)
    connection.executemany(
        "UPDATE metrics SET value_real = ? WHERE run_id = ? AND name = ?",
        [
            (15.0, "right:0", "ego_to_agent_1_distance.min"),
            (17.0, "right:3", "ego_to_agent_1_distance.min"),
            (16.0, "right:5", "ego_to_agent_1_distance.min"),
        ],
    )
    connection.commit()
    connection.close()

    result = analyze_paired_metric_agreement(
        path,
        relation_id,
        {
            "metric": "ego_to_agent_1_distance.min",
            "x_side": "right",
            "primary_threshold": 5,
            "secondary_threshold": 10,
        },
    )

    assert result["selection"]["x_dataset"] == "right"
    assert result["selection"]["y_dataset"] == "left"
    assert result["selection"]["unit"] == "m"
    assert result["summary"]["same_outcome_metric_eligible_count"] == 3
    assert result["summary"]["outcome_disagreement_metric_eligible_count"] == 3
    assert result["summary"]["included"]["thresholds"] == [
        {
            "threshold": 5.0,
            "count": 3,
            "rate": 1.0,
            "y_greater_count": 0,
            "x_greater_count": 3,
        },
        {
            "threshold": 10.0,
            "count": 2,
            "rate": pytest.approx(2 / 3),
            "y_greater_count": 0,
            "x_greater_count": 2,
        },
    ]
    assert result["summary"]["categories"]["success"]["count"] == 2
    assert result["points"][0]["y_minus_x"] == pytest.approx(-5)


def test_metric_agreement_filters_scope_swaps_axes_and_validates_thresholds(
    tmp_path: Path,
) -> None:
    path, relation_id = _database(tmp_path)

    result = analyze_paired_metric_agreement(
        path,
        relation_id,
        {
            "metric": "ego_to_agent_1_distance.min",
            "x_side": "left",
            "outcome_scope": "success",
        },
    )

    assert result["summary"]["included"]["count"] == 2
    assert {point["category"] for point in result["points"]} == {"success_success"}
    assert all(point["y_minus_x"] == pytest.approx(1) for point in result["points"])

    with pytest.raises(PairedMetricAgreementError, match="greater than"):
        analyze_paired_metric_agreement(
            path,
            relation_id,
            {"primary_threshold": 10, "secondary_threshold": 5},
        )


def test_custom_boundaries_disclose_exclusions_and_reject_non_parameters(
    tmp_path: Path,
) -> None:
    path, relation_id = _database(tmp_path)

    result = analyze_paired_parameters(
        path,
        relation_id,
        {"boundaries": {"Ego_Speed": [11, 13, 15]}, "minimum_cell_count": 1},
    )
    assert result["coverage"]["excluded_by_boundaries"] == 1
    assert result["coverage"]["included_count"] == 5

    with pytest.raises(PairedParameterError, match="original numeric parameter"):
        analyze_paired_parameters(path, relation_id, {"x": "initial_closing_speed"})


def test_portable_summary_omits_raw_points(tmp_path: Path) -> None:
    path, _relation_id = _database(tmp_path)

    summary = build_portable_paired_parameter_summary(path)

    assert len(summary["items"]) == 1
    assert "points" not in summary["items"][0]
    assert summary["items"][0]["marginals"]


def test_parameter_hash_pair_with_mismatched_values_is_excluded(tmp_path: Path) -> None:
    path, relation_id = _database(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE parameters SET value_real = 999 WHERE run_id = 'right:0' AND name = 'Ego_Speed'"
    )
    connection.commit()
    connection.close()

    result = analyze_paired_parameters(path, relation_id, {"minimum_cell_count": 1})

    assert result["coverage"]["excluded_parameter_mismatch"] == 1
    assert result["coverage"]["included_count"] == 5
