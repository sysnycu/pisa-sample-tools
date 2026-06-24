from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
import yaml

from pisa_sample_tools.evidence import build_evidence, load_analysis_spec
from pisa_sample_tools.evidence.cli import main
from pisa_sample_tools.evidence.models import EvidenceError


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _write_run(
    root: Path,
    run_id: int,
    *,
    x: float,
    y: float,
    outcome: str,
    stop_condition: str,
    min_ttc: float,
) -> None:
    monitor = root / f"iteration_{run_id}" / "monitor"
    _write_csv(
        monitor / "result.csv",
        [
            {
                "run.status": "finished",
                "run.test_outcome": outcome,
                "run.stop_condition": stop_condition,
                "run.stop_reason": stop_condition,
                "run.params": json.dumps({"x": x, "y": y}),
                "run.total_steps": 3,
                "run.final_sim_time_ms": 100,
                "run.wall_time_ms": 120,
                "run.speedup": 0.8333,
                "pair.min_ttc_s": min_ttc,
            }
        ],
    )
    _write_csv(
        monitor / "frame_metrics.csv",
        [
            {
                "step_index": 0,
                "sim_time_ms": 0,
                "pair.ttc_s": min_ttc + 1,
                "pair.distance_m": 8,
                "ego.speed": 10,
                "ego.acceleration": 0,
            },
            {
                "step_index": 1,
                "sim_time_ms": 50,
                "pair.ttc_s": min_ttc,
                "pair.distance_m": 3,
                "ego.speed": 8,
                "ego.acceleration": -4,
            },
        ],
    )
    _write_csv(
        monitor / "agent_states.csv",
        [
            {"step_index": 0, "sim_time_ms": 0, "agent_id": 0, "x": 0, "y": 0},
            {"step_index": 1, "sim_time_ms": 50, "agent_id": 0, "x": 1, "y": 0},
            {"step_index": 0, "sim_time_ms": 0, "agent_id": 1, "x": 5, "y": 1},
            {"step_index": 1, "sim_time_ms": 50, "agent_id": 1, "x": 4, "y": 0},
        ],
    )
    _write_csv(
        monitor / "agent_geometry.csv",
        [
            {
                "step_index": 0,
                "sim_time_ms": 0,
                "agent_id": 0,
                "shape_type": "BOUNDING_BOX",
                "length_m": 4.5,
                "width_m": 1.8,
                "height_m": 1.5,
                "reference_point": "center",
                "footprint_json": "",
                "source": "simulator_runtime_shape",
            },
            {
                "step_index": 0,
                "sim_time_ms": 0,
                "agent_id": 1,
                "shape_type": "BOUNDING_BOX",
                "length_m": 4.5,
                "width_m": 1.8,
                "height_m": 1.5,
                "reference_point": "center",
                "footprint_json": "",
                "source": "simulator_runtime_shape",
            },
        ],
    )
    _write_csv(
        monitor / "control_commands.csv",
        [
            {
                "step_index": 0,
                "sim_time_ms": 0,
                "control_type": "vehicle",
                "throttle": 0.5,
                "brake": 0,
                "steer": 0,
            },
            {
                "step_index": 1,
                "sim_time_ms": 50,
                "control_type": "vehicle",
                "throttle": 0,
                "brake": 0.8,
                "steer": 0.1,
            },
        ],
    )
    if "collision" in stop_condition:
        contact_region = json.dumps([[3.8, -0.8], [4.1, -0.8], [4.1, 0.8], [3.8, 0.8]])
        _write_csv(
            monitor / "collision_events.csv",
            [
                {
                    "step_index": 1,
                    "sim_time_ms": 50,
                    "actor_a": 0,
                    "actor_b": 1,
                    "x": 3.95,
                    "y": 0.0,
                    "z": 0.0,
                    "position_source": "derived_bbox_overlap",
                    "contact_region_json": contact_region,
                }
            ],
        )
        _write_csv(
            monitor / "scenario_events.csv",
            [
                {
                    "step_index": 0,
                    "sim_time_ms": 0,
                    "event_type": "scenario_start",
                    "actor_id": "",
                    "actor_id_b": "",
                    "x": "",
                    "y": "",
                    "z": "",
                    "source": "runner",
                    "details_json": "{}",
                    "contact_region_json": "",
                },
                {
                    "step_index": 1,
                    "sim_time_ms": 50,
                    "event_type": "collision",
                    "actor_id": 0,
                    "actor_id_b": 1,
                    "x": 3.95,
                    "y": 0.0,
                    "z": 0.0,
                    "source": "derived_bbox_overlap",
                    "details_json": "{}",
                    "contact_region_json": contact_region,
                },
            ],
        )


def _write_experiment(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "execution_manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "execution_id": f"execution-{root.name}",
                "scenario_name": "cut-in",
                "dt": 0.05,
                "seed": 7,
                "runner_version": "test",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _write_run(
        root,
        1,
        x=10,
        y=5,
        outcome="success",
        stop_condition="goal",
        min_ttc=3.0,
    )
    _write_run(
        root,
        2,
        x=5,
        y=10,
        outcome="test_fail",
        stop_condition="collision_guard",
        min_ttc=0.5,
    )


def _write_spec(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "parameters": {"axes": {"x": "x", "y": "y"}},
                "metrics": {
                    "min_ttc": {
                        "summary": "pair.min_ttc_s",
                        "series": "pair.ttc_s",
                        "unit": "s",
                    },
                    "min_distance": {
                        "summary": "pair.min_distance_m",
                        "series": "pair.distance_m",
                        "unit": "m",
                    },
                    "max_deceleration": {
                        "summary": "ego.max_deceleration",
                        "series": "ego.acceleration",
                    },
                },
                "thresholds": {"near_critical_ttc_s": 1.5},
                "heatmap": {"bins": 4},
                "output": {"formats": ["svg", "png"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_v2_spec(path: Path, *, map_av_quit: bool = True) -> None:
    termination = {"goal": "success", "collision_guard": "failure"}
    if map_av_quit:
        termination["av_should_quit"] = "unclassified"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "validation": {"mode": "strict"},
                "parameters": {
                    "mode": "all_pairwise",
                    "include": ["x", "y", "relative"],
                    "derived": {
                        "relative": {
                            "operation": "subtract",
                            "left": "x",
                            "right": "y",
                        }
                    },
                },
                "metrics": {
                    "min_ttc": {"summary": "pair.min_ttc_s", "series": "pair.ttc_s"},
                    "min_distance": {
                        "summary": "pair.min_distance_m",
                        "series": "pair.distance_m",
                    },
                    "max_deceleration": {
                        "summary": "ego.max_deceleration",
                        "series": "ego.acceleration",
                    },
                },
                "outcomes": {
                    "success": ["success"],
                    "failure": ["fail", "test_fail"],
                    "invalid": ["invalid"],
                    "termination": termination,
                },
                "thresholds": {"near_critical_ttc_s": 1.5},
                "heatmap": {"bins": 4, "min_bin_count": 1},
                "comparison": {"bootstrap_samples": 20, "bootstrap_seed": 0},
                "output": {"formats": ["svg"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_load_analysis_spec_reads_metric_bindings(tmp_path: Path) -> None:
    spec_path = tmp_path / "analysis.yaml"
    _write_spec(spec_path)

    spec = load_analysis_spec(spec_path)

    assert spec.x_param == "x"
    assert spec.metrics["min_ttc"].summary == "pair.min_ttc_s"
    assert spec.near_critical_ttc_s == 1.5


def test_build_evidence_writes_paper_ready_bundle(tmp_path: Path) -> None:
    results = tmp_path / "experiment"
    spec_path = tmp_path / "analysis.yaml"
    output = tmp_path / "evidence"
    _write_experiment(results)
    _write_spec(spec_path)

    result = build_evidence(
        results_paths=[results],
        output_dir=output,
        spec_path=spec_path,
    )

    assert result.run_count == 2
    assert (output / "summary" / "runs.csv").exists()
    assert (output / "summary" / "outcomes.csv").exists()
    assert (output / "summary" / "metrics.csv").exists()
    assert (output / "figures" / "outcome_scatter.svg").exists()
    assert (output / "figures" / "outcome_scatter.png").exists()
    assert (output / "figures" / "failure_rate_heatmap.csv").exists()
    assert (output / "figures" / "min_ttc_cdf.svg").exists()
    assert (output / "representative_cases" / "selected_cases.csv").exists()
    assert (output / "representative_cases" / "safe_trajectory.svg").exists()
    assert (output / "representative_cases" / "failure_controls.png").exists()
    assert (output / "summary" / "agent_geometry.csv").exists()
    assert (output / "summary" / "collision_events.csv").exists()
    assert (output / "summary" / "scenario_events.csv").exists()
    assert (output / "report" / "analysis_report.html").exists()
    assert (output / "report" / "paper_ready_summary.tex").exists()
    assert (output / "provenance" / "analysis_spec.yaml").exists()
    assert (output / "manifest.yaml").exists()
    metrics = (output / "summary" / "metrics.csv").read_text(encoding="utf-8")
    assert "pair.min_distance_m" in metrics
    assert ",3.0," in metrics
    report = (output / "report" / "analysis_report.html").read_text(encoding="utf-8")
    assert "PISA Validation Evidence" in report
    assert "Advanced run explorer" in report
    assert "Download YAML spec" in report
    assert 'id="run-select"' in report
    geometry = (output / "summary" / "agent_geometry.csv").read_text(encoding="utf-8")
    assert "simulator_runtime_shape" in geometry
    collision_events = (output / "summary" / "collision_events.csv").read_text(
        encoding="utf-8"
    )
    assert "derived_bbox_overlap" in collision_events
    assert "contact_region_json" in collision_events
    timeline = (
        output / "representative_cases" / "failure_event_timeline.csv"
    ).read_text(encoding="utf-8")
    assert "collision (derived_bbox_overlap)" in timeline
    assert "contact_region_json" in timeline


def test_build_evidence_compares_multiple_components(tmp_path: Path) -> None:
    left = tmp_path / "behavior"
    right = tmp_path / "autoware"
    spec_path = tmp_path / "analysis.yaml"
    output = tmp_path / "comparison"
    _write_experiment(left)
    _write_experiment(right)
    _write_spec(spec_path)
    campaign_path = tmp_path / "campaign.yaml"
    campaign_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "datasets": [
                    {
                        "id": "behavior-r1",
                        "results": str(left),
                        "logical_scenario_name": "cut-in",
                        "labels": {
                            "simulator": "CARLA",
                            "av": "Behavior Agent",
                            "sampler": "Grid",
                        },
                        "grouping": {"repeat_id": 1, "seed": 7},
                    },
                    {
                        "id": "autoware-r1",
                        "results": str(right),
                        "logical_scenario_name": "cut-in",
                        "labels": {
                            "simulator": "CARLA",
                            "av": "Autoware",
                            "sampler": "Grid",
                        },
                        "grouping": {"repeat_id": 1, "seed": 7},
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    build_evidence(
        campaign_path=campaign_path,
        output_dir=output,
        spec_path=spec_path,
    )

    comparison = (output / "comparison" / "component_comparison.csv").read_text(
        encoding="utf-8"
    )
    assert "Behavior Agent" in comparison
    assert "Autoware" in comparison
    assert (output / "comparison" / "av_name_outcome_comparison.svg").exists()
    assert (output / "comparison" / "repeated_run_stability.csv").exists()
    pairing = (output / "comparison" / "pairing_summary.csv").read_text(encoding="utf-8")
    assert ",2,0,0" in pairing
    assert (output / "comparison" / "outcome_transition.csv").exists()


def test_unified_cli_builds_evidence(tmp_path: Path, capsys) -> None:
    results = tmp_path / "experiment"
    spec_path = tmp_path / "analysis.yaml"
    output = tmp_path / "evidence"
    _write_experiment(results)
    _write_spec(spec_path)

    assert (
        main(
            [
                "build",
                "--results",
                str(results),
                "--spec",
                str(spec_path),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert "runs: 2" in capsys.readouterr().out


def test_unified_cli_emits_progress_logs(tmp_path: Path, capsys) -> None:
    results = tmp_path / "experiment"
    spec_path = tmp_path / "analysis.yaml"
    output = tmp_path / "evidence"
    _write_experiment(results)
    _write_spec(spec_path)

    assert (
        main(
            [
                "compare",
                "--results",
                str(results),
                "--spec",
                str(spec_path),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "loading analysis spec" in captured.err
    assert "rendering core figures" in captured.err
    assert "analysis complete" in captured.err


def test_v2_builds_all_pairwise_views_and_keeps_unknown_unclassified(tmp_path: Path) -> None:
    results = tmp_path / "experiment"
    spec_path = tmp_path / "analysis-v2.yaml"
    output = tmp_path / "evidence"
    _write_experiment(results)
    _write_run(
        results,
        3,
        x=8,
        y=7,
        outcome="unknown",
        stop_condition="av_should_quit",
        min_ttc=4.0,
    )
    _write_v2_spec(spec_path)

    build_evidence(results_paths=[results], output_dir=output, spec_path=spec_path)

    pair_root = output / "figures" / "parameter_space"
    assert sorted(path.name for path in pair_root.iterdir()) == [
        "x__relative",
        "x__y",
        "y__relative",
    ]
    outcomes = (output / "summary" / "outcomes.csv").read_text(encoding="utf-8")
    assert "unclassified,1" in outcomes
    cases = (output / "representative_cases" / "selected_cases.csv").read_text(
        encoding="utf-8"
    )
    assert "near_critical" not in cases
    assert (output / "provenance" / "source_execution_manifests" / "experiment.yaml").exists()
    assert (output / "report" / "runs.json").exists()


def test_v2_strict_rejects_unmapped_outcome(tmp_path: Path) -> None:
    results = tmp_path / "experiment"
    spec_path = tmp_path / "analysis-v2.yaml"
    output = tmp_path / "evidence"
    _write_experiment(results)
    _write_run(
        results,
        3,
        x=8,
        y=7,
        outcome="unknown",
        stop_condition="av_should_quit",
        min_ttc=4.0,
    )
    _write_v2_spec(spec_path, map_av_quit=False)

    with pytest.raises(EvidenceError, match="strict validation failed"):
        build_evidence(results_paths=[results], output_dir=output, spec_path=spec_path)


def test_validate_command_reports_trace_quality(tmp_path: Path, capsys) -> None:
    results = tmp_path / "experiment"
    spec_path = tmp_path / "analysis-v2.yaml"
    _write_experiment(results)
    _write_v2_spec(spec_path)

    assert (
        main(
            [
                "validate",
                "--results",
                str(results),
                "--spec",
                str(spec_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "errors: 0" in captured.out
    assert "runs: 2" in captured.out
