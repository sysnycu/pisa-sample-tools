from __future__ import annotations

import csv
from pathlib import Path

import pytest
import yaml

from pisa_sample_tools.evidence.concrete_compare import (
    align_numeric_series,
    build_comparison_chunk,
    build_concrete_comparison_groups,
)
from pisa_sample_tools.evidence.models import (
    AnalysisSpec,
    EvidenceError,
    MetricBinding,
    RunRecord,
)
from pisa_sample_tools.evidence.spec import load_analysis_spec


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _run(
    root: Path,
    experiment: str,
    *,
    scenario: str = "cutin",
    sample_id: str = "sample-1",
    params: dict[str, float] | None = None,
    offset: float = 0.0,
    outcome: str = "success",
    time_ms: int = 100,
) -> RunRecord:
    monitor = root / experiment / "iteration_1" / "monitor"
    _write_csv(
        monitor / "agent_states.csv",
        [
            {"step_index": 0, "sim_time_ms": 0, "agent_id": 0, "x": 0 + offset, "y": 0, "z": 0, "speed": 10},
            {"step_index": 1, "sim_time_ms": time_ms, "agent_id": 0, "x": 1 + offset, "y": 0, "z": 0, "speed": 10},
            {"step_index": 0, "sim_time_ms": 0, "agent_id": 1, "x": 5, "y": 1, "z": 0, "speed": 8},
            {"step_index": 1, "sim_time_ms": time_ms, "agent_id": 1, "x": 6, "y": 1, "z": 0, "speed": 8},
        ],
    )
    _write_csv(
        monitor / "frame_metrics.csv",
        [
            {"step_index": 0, "sim_time_ms": 0, "pair.ttc": 3.0, "ego.speed": 10, "ego.acceleration": 0},
            {"step_index": 1, "sim_time_ms": time_ms, "pair.ttc": 2.0 - offset, "ego.speed": 9, "ego.acceleration": -1},
        ],
    )
    _write_csv(
        monitor / "control_commands.csv",
        [
            {"step_index": 0, "sim_time_ms": 0, "control_type": "vehicle", "throttle": 0.5, "brake": 0, "steer": 0},
            {"step_index": 1, "sim_time_ms": time_ms, "control_type": "vehicle", "throttle": 0, "brake": 0.4 + offset, "steer": 0.1},
        ],
    )
    return RunRecord(
        experiment_id=experiment,
        scenario_id="1",
        sample_id=sample_id,
        logical_scenario_name=scenario,
        params=params or {"speed": 10.0, "distance": 20.0},
        metadata={"av_name": experiment, "simulator_name": "sim", "ego_agent_id": 0},
        status="finished",
        outcome=outcome,
        termination_reason="goal" if outcome == "success" else "collision",
        stop_reason=None,
        metrics={"min_ttc": 2.0 - offset, "run.final_sim_time_ms": 100},
        result_path=monitor.parent,
        frame_metrics_path=monitor / "frame_metrics.csv",
        agent_states_path=monitor / "agent_states.csv",
        control_commands_path=monitor / "control_commands.csv",
    )


def _spec(*, strict: bool = True) -> AnalysisSpec:
    return AnalysisSpec(
        version=2,
        validation_mode="strict" if strict else "permissive",
        metrics={"min_ttc": MetricBinding(summary="min_ttc", series="pair.ttc")},
    )


def test_groups_same_scenario_and_parameters_across_all_datasets(tmp_path: Path) -> None:
    runs = [_run(tmp_path, "a"), _run(tmp_path, "b", offset=0.1), _run(tmp_path, "c", offset=0.2)]

    groups, warnings = build_concrete_comparison_groups(runs, _spec())

    assert warnings == []
    assert len(groups) == 1
    assert [run.experiment_id for run in groups[0].runs] == ["a", "b", "c"]
    assert groups[0].pairing_method == "sample_id"


def test_grouping_does_not_mix_logical_scenarios(tmp_path: Path) -> None:
    runs = [_run(tmp_path, "a", scenario="cutin"), _run(tmp_path, "b", scenario="crossing")]

    groups, _ = build_concrete_comparison_groups(runs, _spec())

    assert groups == []


def test_duplicate_dataset_parameter_group_is_rejected_in_strict_mode(tmp_path: Path) -> None:
    first = _run(tmp_path, "a", sample_id="one")
    second = _run(tmp_path, "a", sample_id="two")
    second = RunRecord(**{**second.__dict__, "scenario_id": "2"})
    other = _run(tmp_path, "b")

    with pytest.raises(EvidenceError, match="duplicate runs"):
        build_concrete_comparison_groups([first, second, other], _spec())


def test_numeric_alignment_supports_linear_and_previous_without_extrapolation() -> None:
    left = [(0.0, 0.0), (0.5, 5.0), (1.0, 10.0)]
    right = [(0.25, 2.0), (0.75, 4.0), (1.25, 8.0)]

    linear = align_numeric_series(left, right, interpolation="linear")
    previous = align_numeric_series(left, right, interpolation="previous")

    assert [row[0] for row in linear] == [0.25, 0.75]
    assert linear[0][1:] == pytest.approx((2.5, 2.0))
    assert previous[0][1:] == pytest.approx((0.0, 2.0))
    assert linear[-1][0] <= 1.0


def test_chunk_contains_overlay_data_and_pairwise_summaries(tmp_path: Path) -> None:
    runs = [
        _run(tmp_path, "a"),
        _run(tmp_path, "b", offset=0.2, outcome="failure", time_ms=150),
    ]
    spec = _spec()
    groups, _ = build_concrete_comparison_groups(runs, spec)

    chunk = build_comparison_chunk(groups[0], spec)

    assert chunk["schema_version"] == 2
    assert chunk["timeline_s"] == [0.0, 0.1, 0.15]
    assert len(chunk["configs"]) == 2
    assert chunk["configs"][0]["timeline_s"] == [0.0, 0.1]
    assert chunk["configs"][1]["timeline_s"] == [0.0, 0.15]
    assert chunk["configs"][0]["trajectory"]
    assert any(item["field"] == "steer" for item in chunk["configs"][0]["series"])
    assert chunk["pairwise_trajectory"]
    assert any(item["field"] == "pair.ttc" for item in chunk["pairwise_series"])


def test_analysis_spec_loads_comparison_detail_settings(tmp_path: Path) -> None:
    path = tmp_path / "analysis.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "comparison": {
                    "detail": {
                        "enabled": True,
                        "max_points_per_series": 100,
                        "trajectory_divergence_m": 0.75,
                        "tolerances": {"steer": 0.01},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    spec = load_analysis_spec(path)

    assert spec.comparison_detail.max_points_per_series == 100
    assert spec.comparison_detail.trajectory_divergence_m == 0.75
    assert spec.comparison_detail.tolerances["steer"] == 0.01
