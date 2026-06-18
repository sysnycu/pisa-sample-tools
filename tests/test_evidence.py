from __future__ import annotations

import csv
import json
from pathlib import Path

import yaml

from pisa_sample_tools.evidence import build_evidence, load_analysis_spec
from pisa_sample_tools.evidence.cli import main


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
        _write_csv(
            monitor / "collision_events.csv",
            [{"step_index": 1, "sim_time_ms": 50, "actor_id_a": 0, "actor_id_b": 1}],
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
