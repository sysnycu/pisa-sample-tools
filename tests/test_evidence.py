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
    assert spec.metrics["min_ttc"].risk_direction == "higher_is_safer"
    assert spec.sensitivity.enabled is False
    assert spec.sensitivity.outcome_targets == ("failure", "invalidity")
    assert spec.near_critical_ttc_s == 1.5


def test_load_analysis_spec_accepts_flat_sensitivity_compatibility(
    tmp_path: Path,
) -> None:
    spec_path = tmp_path / "analysis.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "sensitivity": {
                    "outcome_targets": ["failure"],
                    "metric_targets": ["min_ttc"],
                    "sobol_base_sizes": [64],
                    "morris_trajectories": [12],
                },
            }
        ),
        encoding="utf-8",
    )

    spec = load_analysis_spec(spec_path)

    assert spec.sensitivity.outcome_targets == ("failure",)
    assert spec.sensitivity.metric_targets == ("min_ttc",)
    assert spec.sensitivity.sobol_base_sizes == (64,)
    assert spec.sensitivity.morris_trajectories == (12,)


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
        sensitivity=True,
    )

    assert result.run_count == 2
    assert (output / "summary" / "runs.csv").exists()
    assert (output / "summary" / "outcomes.csv").exists()
    assert (output / "summary" / "metrics.csv").exists()
    assert (output / "summary" / "parameter_sensitivity.csv").exists()
    assert (output / "summary" / "sensitivity_model_quality.csv").exists()
    assert (output / "summary" / "sensitivity_sampling_plan.csv").exists()
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
    assert (output / "summary" / "experiment_timing.csv").exists()
    assert (output / "report" / "analysis_report.html").exists()
    assert (output / "report" / "analysis_data.json").exists()
    assert (output / "report" / "analysis_data.js").exists()
    assert (output / "report" / "case_data.json").exists()
    assert (output / "report" / "case_data.js").exists()
    assert (output / "report" / "cases" / "safe.json").exists()
    assert (output / "report" / "cases" / "failure.json").exists()
    assert (output / "report" / "paper_ready_summary.tex").exists()
    assert (output / "provenance" / "analysis_spec.yaml").exists()
    assert (output / "manifest.yaml").exists()
    metrics = (output / "summary" / "metrics.csv").read_text(encoding="utf-8")
    assert "pair.min_distance_m" in metrics
    assert ",3.0," in metrics
    report = (output / "report" / "analysis_report.html").read_text(encoding="utf-8")
    assert "PISA Validation Evidence" in report
    assert "Parameter Space Explorer" in report
    assert "Parameter Sensitivity" in report
    assert 'id="sensitivity-cluster-table"' in report
    assert 'id="sensitivity-compare-table"' in report
    assert "Boundary Explorer" in report
    assert "Semantic" in report
    assert "Detail" in report
    assert 'id="case-canvas"' in report
    assert "Advanced run explorer and Spec Lab" in report
    assert "Download YAML spec" in report
    assert 'id="run-select"' in report
    assert "run.metrics[metric?.source || key.slice(7)]" in report
    assert "N/A (${colors.unavailable})" in report
    assert "mode:'ttc_gradient'" in report
    assert "1 critical · 2 near-critical" in report
    assert 'id="aspect-select"' in report
    assert '<option value="equal" selected>1:1 data scale</option>' in report
    assert 'id="background-select"' in report
    assert 'id="scatter-export"' in report
    assert 'id="scatter-export-crop"' in report
    assert 'id="scatter-export-axes"' in report
    assert 'id="scatter-export-labels"' in report
    assert 'id="scatter-export-grid"' in report
    assert "function exportScatter" in report
    assert "iterationState.active?iterationState.points.slice(0,iterationState.index)" in report
    assert "iterationProgress=iterationState.active?iterationState.index:null" in report
    assert "ctx.lineWidth = 0.65*devicePixelRatio" in report
    assert "Publication PNG" in report
    assert "function scatterExportRect" in report
    assert "rightPad=options.axes?56:8" in report
    assert "options.grid" in report
    assert '<option value="square">Square plot</option>' in report
    assert "function scatterBounds" in report
    assert "ctx.fillText(fmt(xv)" in report
    assert "function saveExplorerState" in report
    assert "sessionStorage.setItem(explorerStateKey" in report
    assert 'id="iteration-play"' in report
    assert 'id="iteration-timing-mode"' in report
    assert 'id="iteration-timing-value"' in report
    assert "function iterationRate" in report
    assert "function prepareIteration" in report
    assert "recordCanvasAnimation" in report
    assert 'id="run-sensitivity"' in report
    assert "updateSensitivityOnline" in report
    assert "Open visualization" in report
    assert "Total sim time" in report
    assert "Timing coverage" in report
    assert "run.metrics?.['run.total_steps']" in report
    payload = json.loads((output / "report" / "analysis_data.json").read_text())
    timing = payload["experiment_summaries"]["timing"][0]
    assert timing["total_steps"] == 6
    assert timing["total_sim_time_ms"] == 200
    assert timing["total_wall_time_ms"] == 240
    assert timing["speedup"] == pytest.approx(200 / 240)
    assert timing["speedup_run_count"] == 2
    comparison = (output / "report" / "comparison.html").read_text(encoding="utf-8")
    assert "total_steps:c.metrics['run.total_steps']" in comparison
    assert "sim_time:durationMs(c.metrics['run.final_sim_time_ms'])" in comparison
    assert "wall_time:durationMs(c.metrics['run.wall_time_ms'])" in comparison
    assert "speed_up:" in comparison
    comparison_page = (output / "report" / "comparison.html").read_text(encoding="utf-8")
    assert 'id="trajectory-export"' in comparison_page
    assert "function exportTrajectory" in comparison_page
    data = json.loads((output / "report" / "analysis_data.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == 6
    assert data["sensitivity"]["effects"]
    assert data["sensitivity"]["model_quality"]
    assert data["report_mode"] == "single"
    assert data["summary"]["run_count"] == 2
    assert data["runs"][0]["normalized_outcome"] == "success"
    assert all(run["comparison_group_id"] for run in data["runs"])
    assert len(data["comparison"]["concrete_scenarios"]) == 2
    single_group = data["comparison"]["concrete_scenarios"][0]
    assert len(single_group["configs"]) == 1
    single_chunk = json.loads(
        (
            output
            / "report"
            / "comparison_data"
            / f"{single_group['group_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert len(single_chunk["configs"]) == 1
    assert single_chunk["configs"][0]["trajectory"]
    assert data["boundary"]["pairs"]["x__y"]["available"] is True
    assert data["representative_cases"][0]["case_json"]
    assert data["insights"]
    assert "Analyze concrete run" in report
    case_data = json.loads((output / "report" / "case_data.json").read_text(encoding="utf-8"))
    safe_case = next(item for item in case_data["cases"] if item["case_type"] == "safe")
    failure_case = next(
        item for item in case_data["cases"] if item["case_type"] == "failure"
    )
    steer = next(item for item in safe_case["series"] if item["field"] == "steer")
    speed = next(item for item in safe_case["series"] if item["field"] == "ego.speed")
    acceleration = next(
        item for item in safe_case["series"] if item["field"] == "ego.acceleration"
    )
    assert steer["semantic_limits"]["lower"] == -1.0
    assert steer["semantic_limits"]["upper"] == 1.0
    assert speed["semantic_limits"]["lower"] == 0.0
    assert acceleration["semantic_limits"]["lower"] == -acceleration["semantic_limits"][
        "upper"
    ]
    failure_speed = next(
        item for item in failure_case["series"] if item["field"] == "ego.speed"
    )
    failure_acceleration = next(
        item for item in failure_case["series"] if item["field"] == "ego.acceleration"
    )
    assert failure_speed["semantic_limits"] == speed["semantic_limits"]
    assert failure_acceleration["semantic_limits"] == acceleration["semantic_limits"]
    controls_csv = (
        output / "representative_cases" / "failure_controls.csv"
    ).read_text(encoding="utf-8")
    assert controls_csv.splitlines()[0] == "time_s,series,value"
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


def test_static_profile_build_embeds_compact_snapshot_and_detailed_profile(
    tmp_path: Path,
) -> None:
    results = tmp_path / "experiment"
    spec_path = tmp_path / "analysis.yaml"
    output = tmp_path / "portable"
    _write_experiment(results)
    _write_spec(spec_path)

    assert main(
        [
            "build",
            "--results",
            str(results),
            "--spec",
            str(spec_path),
            "--output",
            str(output),
            "--report-mode",
            "static",
            "--profile",
        ]
    ) == 0

    html = (output / "report" / "analysis_report.html").read_text(encoding="utf-8")
    manifest = yaml.safe_load((output / "manifest.yaml").read_text(encoding="utf-8"))
    timings = json.loads(
        (output / "provenance" / "stage_timings.json").read_text(encoding="utf-8")
    )
    assert '<script src="analysis_data.js"></script>' not in html
    assert "window.PISA_ANALYSIS_DATA=" in html
    assert '"kind": "portable"' in html
    assert manifest["schema_version"] == 3
    assert manifest["report_build_version"] == 9
    assert (output / "provenance" / "build_profile.pstats").is_file()
    assert (output / "provenance" / "build_profile.txt").is_file()
    assert all(item["stage"] and item["duration_seconds"] >= 0 for item in timings)


def test_build_evidence_compares_multiple_components(tmp_path: Path) -> None:
    left = tmp_path / "behavior"
    right = tmp_path / "autoware"
    spec_path = tmp_path / "analysis.yaml"
    output = tmp_path / "comparison"
    _write_experiment(left)
    _write_experiment(right)
    _write_run(
        right,
        1,
        x=10,
        y=5,
        outcome="test_fail",
        stop_condition="collision_guard",
        min_ttc=0.25,
    )
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
        sensitivity=True,
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
    assert (output / "comparison" / "concrete_scenarios.csv").exists()
    assert (output / "report" / "comparison.html").exists()
    assert (output / "report" / "comparison_index.json").exists()
    comparison_index = json.loads(
        (output / "report" / "comparison_index.json").read_text(encoding="utf-8")
    )
    assert len(comparison_index["groups"]) == 2
    group_id = comparison_index["groups"][0]["group_id"]
    assert (output / "report" / "comparison_data" / f"{group_id}.json").exists()
    chunk = json.loads(
        (output / "report" / "comparison_data" / f"{group_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(chunk["configs"]) == 2
    assert chunk["pairwise_trajectory"]
    report_data = json.loads(
        (output / "report" / "analysis_data.json").read_text(encoding="utf-8")
    )
    assert report_data["report_mode"] == "compare"
    sensitivity_targets = {
        (row["experiment_id"], row["target"])
        for row in report_data["sensitivity"]["model_quality"]
    }
    assert any(target == "outcome_disagreement" for _, target in sensitivity_targets)
    assert any(target.startswith("delta:") for _, target in sensitivity_targets)
    assert [item["id"] for item in report_data["experiments"]] == [
        "behavior-r1",
        "autoware-r1",
    ]
    assert len(report_data["experiment_summaries"]["outcomes"]) == 2
    assert report_data["boundary"]["pairs"] == {}
    assert set(report_data["boundary"]["by_experiment"]) == {
        "behavior-r1",
        "autoware-r1",
    }
    points = report_data["comparison"]["parameter_points"]
    assert any(point.get("transition") == "failure__success" for point in points)
    assert all(point.get("comparison_group_id") for point in points if point["matched"])
    parameter_groups = report_data["comparison"]["parameter_groups"]
    assert len(parameter_groups) == 2
    assert all(group["complete"] for group in parameter_groups)
    assert all(len(group["experiments"]) == 2 for group in parameter_groups)
    experiment_figures = [
        figure for figure in report_data["figures"] if figure["scope"] == "experiment"
    ]
    assert all(
        {"figure_key", "category", "tags", "available_formats"} <= figure.keys()
        for figure in experiment_figures
    )
    assert {figure["experiment_id"] for figure in experiment_figures} == {
        "behavior_r1",
        "autoware_r1",
    }
    assert not (output / "figures" / "outcome_scatter.svg").exists()
    assert (
        output
        / "figures"
        / "experiments"
        / "behavior_r1"
        / "outcome_scatter.svg"
    ).exists()
    assert all(run["comparison_group_id"] for run in report_data["runs"])
    report_html = (output / "report" / "analysis_report.html").read_text(encoding="utf-8")
    assert "Compare configs" in report_html
    assert "Compare outcomes" in report_html
    assert "Compare metric delta" in report_html
    assert "Open comparison" in report_html
    assert "Reference experiment" in report_html
    assert "figure-category" in report_html
    assert "group-inspector" in report_html
    assert "ctx.rect" not in report_html
    comparison_html = (output / "report" / "comparison.html").read_text(
        encoding="utf-8"
    )
    assert 'id="play"' in comparison_html
    assert 'id="metric-canvas"' in comparison_html
    assert 'id="control-canvas"' in comparison_html
    assert "Step ${state.timeIndex+1}" in comparison_html


def test_build_evidence_overwrite_recovers_partial_output(tmp_path: Path) -> None:
    results = tmp_path / "experiment"
    spec_path = tmp_path / "analysis.yaml"
    output = tmp_path / "evidence"
    _write_experiment(results)
    _write_spec(spec_path)
    output.mkdir()
    (output / ".pisa-analysis-in-progress.yaml").write_text(
        yaml.safe_dump(
            {
                "tool": "pisa-analysis-tools",
                "schema_version": 1,
                "state": "in_progress",
            }
        ),
        encoding="utf-8",
    )
    (output / "stale.txt").write_text("partial output", encoding="utf-8")

    build_evidence(
        results_paths=[results],
        output_dir=output,
        spec_path=spec_path,
        overwrite=True,
    )

    assert not (output / "stale.txt").exists()
    assert not (output / ".pisa-analysis-in-progress.yaml").exists()
    manifest = yaml.safe_load((output / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["tool"] == "pisa-analysis-tools"
    assert not any(
        ".pisa-analysis-in-progress.yaml" in path for path in manifest["outputs"]
    )


def test_build_evidence_overwrite_rejects_unowned_output(tmp_path: Path) -> None:
    results = tmp_path / "experiment"
    spec_path = tmp_path / "analysis.yaml"
    output = tmp_path / "evidence"
    _write_experiment(results)
    _write_spec(spec_path)
    output.mkdir()
    (output / "user-data.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(EvidenceError, match="neither manifest.yaml nor a PISA partial"):
        build_evidence(
            results_paths=[results],
            output_dir=output,
            spec_path=spec_path,
            overwrite=True,
        )

    assert (output / "user-data.txt").read_text(encoding="utf-8") == "keep"


def test_build_evidence_overwrite_accepts_empty_output_directory(tmp_path: Path) -> None:
    results = tmp_path / "experiment"
    spec_path = tmp_path / "analysis.yaml"
    output = tmp_path / "evidence"
    _write_experiment(results)
    _write_spec(spec_path)
    output.mkdir()

    result = build_evidence(
        results_paths=[results],
        output_dir=output,
        spec_path=spec_path,
        overwrite=True,
    )

    assert result.manifest_path.exists()
    assert not (output / ".pisa-analysis-in-progress.yaml").exists()


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
    report_data = json.loads(
        (output / "report" / "analysis_data.json").read_text(encoding="utf-8")
    )
    assert report_data["sensitivity"]["generated"] is False
    assert report_data["sensitivity"]["model_quality"] == []


def test_unified_cli_enriches_existing_bundle_with_sensitivity(
    tmp_path: Path, capsys
) -> None:
    results = tmp_path / "experiment"
    spec_path = tmp_path / "analysis.yaml"
    output = tmp_path / "evidence"
    _write_experiment(results)
    _write_spec(spec_path)
    build_evidence(results_paths=[results], output_dir=output, spec_path=spec_path)

    assert main(["sensitivity", "--bundle", str(output)]) == 0

    report_data = json.loads(
        (output / "report" / "analysis_data.json").read_text(encoding="utf-8")
    )
    assert report_data["sensitivity"]["generated"] is True
    assert report_data["sensitivity"]["model_quality"]
    assert "sensitivity targets:" in capsys.readouterr().out


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
    assert (output / "report" / "analysis_data.json").exists()


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
