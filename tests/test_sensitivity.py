from __future__ import annotations

from pathlib import Path

import numpy as np

from pisa_sample_tools.evidence.models import (
    AnalysisSpec,
    MetricBinding,
    RunRecord,
    SensitivitySpec,
)
from pisa_sample_tools.evidence.sensitivity import (
    analyze_sensitivity,
    render_sensitivity_figures,
)


def _run(
    index: int,
    *,
    params: dict[str, float],
    outcome: str = "success",
    metrics: dict[str, float] | None = None,
) -> RunRecord:
    return RunRecord(
        experiment_id="experiment",
        scenario_id=str(index),
        sample_id=str(index),
        logical_scenario_name="synthetic",
        params=params,
        metadata={},
        status="finished",
        outcome=outcome,
        termination_reason="goal" if outcome == "success" else "collision",
        stop_reason=None,
        metrics=metrics or {},
        result_path=Path(f"iteration_{index}"),
    )


def _settings(**overrides) -> SensitivitySpec:
    values = {
        "enabled": True,
        "outcome_targets": ("failure",),
        "metric_targets": (),
        "minimum_samples": 30,
        "minimum_minority": 10,
        "cv_folds": 3,
        "permutation_repeats": 3,
        "bootstrap_samples": 0,
        "top_parameters": 4,
        "random_seed": 11,
    }
    values.update(overrides)
    return SensitivitySpec(**values)


def test_sensitivity_ranks_strong_failure_driver(tmp_path: Path) -> None:
    rng = np.random.default_rng(4)
    runs = []
    for index in range(160):
        driver = float(rng.uniform(-1, 1))
        noise = float(rng.uniform(-1, 1))
        outcome = "failure" if driver + rng.normal(0, 0.12) > 0 else "success"
        runs.append(_run(index, params={"driver": driver, "noise": noise}, outcome=outcome))

    result = analyze_sensitivity(runs, AnalysisSpec(sensitivity=_settings()))

    ranking = sorted(result.importance, key=lambda row: row["rank"])
    assert ranking[0]["parameter"] == "driver"
    assert ranking[0]["importance_mean"] > ranking[1]["importance_mean"]
    effect = next(row for row in result.effects if row["parameter"] == "driver")
    assert effect["effect"] > 0.8
    assert result.model_quality[0]["roc_auc"] > 0.9
    figure_paths = render_sensitivity_figures(result, tmp_path / "figures")
    assert any(path.name == "importance_ranking.svg" for path in figure_paths)


def test_sensitivity_describes_u_shaped_metric_response() -> None:
    rng = np.random.default_rng(8)
    runs = []
    for index in range(180):
        x = float(rng.uniform(-2, 2))
        noise = float(rng.uniform(-2, 2))
        value = x * x + float(rng.normal(0, 0.05))
        runs.append(_run(index, params={"x": x, "noise": noise}, metrics={"risk": value}))
    spec = AnalysisSpec(
        metrics={"risk": MetricBinding(summary="risk")},
        sensitivity=_settings(outcome_targets=(), metric_targets=("risk",)),
    )

    result = analyze_sensitivity(runs, spec)

    ranking = sorted(result.importance, key=lambda row: row["rank"])
    assert ranking[0]["parameter"] == "x"
    effect = next(row for row in result.effects if row["parameter"] == "x")
    assert effect["characteristic"] == "u_shaped_or_inverted"
    assert any(row["method"] == "ale" and row["parameter"] == "x" for row in result.profiles)


def test_sensitivity_detects_pairwise_interaction() -> None:
    rng = np.random.default_rng(12)
    runs = []
    for index in range(220):
        left = float(rng.uniform(-1, 1))
        right = float(rng.uniform(-1, 1))
        failure = (left > 0) != (right > 0)
        runs.append(
            _run(
                index,
                params={"left": left, "right": right},
                outcome="failure" if failure else "success",
            )
        )

    result = analyze_sensitivity(runs, AnalysisSpec(sensitivity=_settings()))

    assert result.interactions
    interaction = result.interactions[0]
    assert {interaction["left_parameter"], interaction["right_parameter"]} == {
        "left",
        "right",
    }
    assert interaction["h_statistic"] > 0.5


def test_sensitivity_reports_unavailable_model_for_small_sample() -> None:
    runs = [
        _run(index, params={"x": float(index)}, outcome="failure" if index > 2 else "success")
        for index in range(5)
    ]

    result = analyze_sensitivity(runs, AnalysisSpec(sensitivity=_settings()))

    assert result.model_quality[0]["reliability"] == "unavailable"
    assert "requires at least" in result.model_quality[0]["reason"]
    assert result.sampling_plan


def test_sensitivity_reports_target_progress() -> None:
    messages: list[str] = []
    runs = [
        _run(index, params={"x": float(index)}, outcome="failure" if index > 2 else "success")
        for index in range(5)
    ]

    analyze_sensitivity(
        runs,
        AnalysisSpec(sensitivity=_settings()),
        progress=messages.append,
    )

    assert any("prepared 1 target(s)" in message for message in messages)
    assert any("target 1/1 (100%)" in message for message in messages)
    assert any("computing empirical effects" in message for message in messages)
    assert any("model unavailable" in message for message in messages)
    assert messages[-1] == "sensitivity: analysis complete"


def test_sensitivity_labels_constant_binary_response() -> None:
    runs = [_run(index, params={"x": float(index)}) for index in range(40)]

    result = analyze_sensitivity(
        runs,
        AnalysisSpec(
            sensitivity=_settings(outcome_targets=("invalidity",)),
        ),
    )

    assert result.effects[0]["effect"] is None
    assert result.effects[0]["characteristic"] == "constant_response"
    assert result.model_quality[0]["reliability"] == "unavailable"


def test_sensitivity_separates_invalidity_and_reports_correlated_group() -> None:
    rng = np.random.default_rng(21)
    runs = []
    for index in range(220):
        failure_driver = float(rng.uniform(-1, 1))
        invalidity_driver = float(rng.uniform(-1, 1))
        correlated_copy = failure_driver + float(rng.normal(0, 0.01))
        if invalidity_driver > 0.65:
            outcome = "invalid"
        elif failure_driver > 0:
            outcome = "failure"
        else:
            outcome = "success"
        runs.append(
            _run(
                index,
                params={
                    "failure_driver": failure_driver,
                    "correlated_copy": correlated_copy,
                    "invalidity_driver": invalidity_driver,
                },
                outcome=outcome,
            )
        )

    result = analyze_sensitivity(
        runs,
        AnalysisSpec(
            sensitivity=_settings(outcome_targets=("failure", "invalidity"))
        ),
    )

    failure_effects = {
        row["parameter"]: row for row in result.effects if row["target"] == "failure"
    }
    invalidity_effects = {
        row["parameter"]: row
        for row in result.effects
        if row["target"] == "invalidity"
    }
    assert failure_effects["failure_driver"]["effect_abs"] > 0.8
    assert invalidity_effects["invalidity_driver"]["effect_abs"] > 0.8
    clusters = [
        row
        for row in result.importance
        if row.get("importance_type") == "correlated_cluster"
    ]
    assert any(
        {"failure_driver", "correlated_copy"} <= set(row["parameter"].split(" + "))
        for row in clusters
    )
    assert any(row["high_correlation"] for row in result.correlations)
