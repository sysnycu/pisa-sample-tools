from __future__ import annotations

import time
from pathlib import Path

import pytest

from pisa_sample_tools.evidence.comparison import build_paired_comparisons
from pisa_sample_tools.evidence.concrete_compare import build_concrete_comparison_groups
from pisa_sample_tools.evidence.models import AnalysisSpec, MetricBinding, RunRecord
from pisa_sample_tools.evidence.statistics import select_representative_cases


@pytest.mark.performance
def test_boundary_and_pairing_handle_twenty_thousand_runs() -> None:
    spec = AnalysisSpec(
        version=2,
        validation_mode="strict",
        metrics={
            "min_ttc": MetricBinding(summary="ttc"),
            "min_distance": MetricBinding(summary="distance"),
        },
        bootstrap_samples=0,
    )
    runs = [
        _run(dataset, index)
        for dataset in ("left", "right")
        for index in range(10_000)
    ]

    started = time.perf_counter()
    cases = select_representative_cases(runs, spec, "x", "y")
    comparison = build_paired_comparisons(runs, spec)
    concrete_groups, warnings = build_concrete_comparison_groups(runs, spec)
    elapsed = time.perf_counter() - started

    assert len(cases) >= 3
    assert len(comparison.matched_runs) == 10_000
    assert len(concrete_groups) == 10_000
    assert warnings == []
    assert elapsed < 5.0


def _run(dataset: str, index: int) -> RunRecord:
    failure = index % 4 == 0
    return RunRecord(
        experiment_id=dataset,
        scenario_id=str(index),
        sample_id=str(index),
        logical_scenario_name="cutin",
        params={"x": float(index % 100), "y": float(index // 100)},
        metadata={"av_name": dataset},
        status="finished",
        outcome="fail" if failure else "success",
        termination_reason="collision" if failure else "goal",
        stop_reason=None,
        metrics={
            "ttc": 0.0 if failure else 3.0,
            "distance": 2.0 if failure else 8.0,
            "run.final_sim_time_ms": 1000.0,
        },
        result_path=Path("/tmp"),
    )
