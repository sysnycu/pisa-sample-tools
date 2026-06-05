from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from pisa_sample_tools.analyze import (
    analyze_samples,
    load_records_from_results,
    load_records_from_samples,
)
from pisa_sample_tools.analyze_cli import main


def _write_yaml(path: Path, data: Any) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _write_runner_fixture(tmp_path: Path, *, sample_count: int = 4) -> Path:
    scenario_dir = tmp_path / "scenario"
    config_dir = tmp_path / "configs"
    scenario_dir.mkdir(parents=True)
    config_dir.mkdir()
    _write_yaml(
        scenario_dir / "params.yaml",
        {
            "parameters": [
                {"name": "x", "type": "int", "values": list(range(sample_count))},
                {"name": "y", "type": "double", "values": [1.0, 2.0]},
            ]
        },
    )
    sampler_config = config_dir / "grid.yaml"
    _write_yaml(sampler_config, {"source": {"type": "param_range", "path": "params.yaml"}})
    runner_spec = tmp_path / "runner.yaml"
    _write_yaml(
        runner_spec,
        {
            "scenario": {"scenario_path": str(scenario_dir)},
            "sampler": {"name": "grid", "config_path": str(sampler_config)},
        },
    )
    return runner_spec


def _write_explicit_samples(path: Path) -> None:
    _write_yaml(
        path,
        {
            "samples": [
                {"id": "a", "params": {"x": 0, "y": 1, "z": 2}},
                {"id": "b", "params": {"x": 1, "y": 3, "z": 5}},
                {"id": "c", "params": {"x": 2, "y": 5, "z": 8}},
            ]
        },
    )


def _write_result_iteration(
    root: Path,
    sample_id: str,
    params: dict[str, Any],
    outcome: str,
    *,
    min_ttc: float = 1.25,
) -> None:
    monitor_dir = root / f"iteration_{sample_id}" / "monitor"
    monitor_dir.mkdir(parents=True)
    result_path = monitor_dir / "result.csv"
    with result_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "run.status",
                "run.test_outcome",
                "run.stop_condition",
                "run.stop_reason",
                "run.params",
                "ego_to_agent_1.min_ttc_s",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "run.status": "finished",
                "run.test_outcome": outcome,
                "run.stop_condition": "timeout" if outcome == "success" else "collision",
                "run.stop_reason": "done",
                "run.params": json.dumps(params),
                "ego_to_agent_1.min_ttc_s": str(min_ttc),
            }
        )


def test_load_records_from_explicit_file(tmp_path: Path) -> None:
    samples_path = tmp_path / "explicit.yaml"
    _write_explicit_samples(samples_path)

    records = load_records_from_samples(samples_path)

    assert [record.sample_id for record in records] == ["a", "b", "c"]
    assert records[1].params["z"] == 5


def test_load_records_from_csv_file(tmp_path: Path) -> None:
    samples_path = tmp_path / "samples.csv"
    with samples_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["sample_id", "param.x", "y", "outcome", "metric.min_ttc"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "sample_id": "case_1",
                "param.x": "1.5",
                "y": "2",
                "outcome": "success",
                "metric.min_ttc": "3.25",
            }
        )

    records = load_records_from_samples(samples_path)

    assert records[0].sample_id == "case_1"
    assert records[0].params == {"x": 1.5, "y": 2.0}
    assert records[0].outcome == "success"
    assert records[0].metrics == {"min_ttc": 3.25}


def test_load_records_from_generated_bundle_root_uses_explicit_samples_name(tmp_path: Path) -> None:
    bundle_root = tmp_path / "bundles"
    bundle_dir = bundle_root / "demo-grid1"
    bundle_dir.mkdir(parents=True)
    _write_explicit_samples(bundle_dir / "explicit_samples.yaml")

    records = load_records_from_samples(bundle_root)

    assert [record.sample_id for record in records] == ["a", "b", "c"]
    assert {record.result_path for record in records} == {bundle_dir}


def test_analyze_explicit_samples_writes_report_and_figures(tmp_path: Path) -> None:
    samples_path = tmp_path / "explicit.yaml"
    output_dir = tmp_path / "analysis"
    _write_explicit_samples(samples_path)

    result = analyze_samples(
        samples_path=samples_path,
        output_dir=output_dir,
        params=["x", "y", "z"],
    )

    assert result.record_count == 3
    assert result.selected_params == ("x", "y", "z")
    assert (output_dir / "samples.csv").exists()
    assert (output_dir / "summary.yaml").exists()
    assert (output_dir / "report.html").exists()
    assert (output_dir / "figures" / "scatter_3d.html").exists()
    report = (output_dir / "report.html").read_text(encoding="utf-8")
    assert "Dynamic Explorer" in report
    assert "All discovered parameters and metrics" in report
    assert 'id="dyn-x"' in report
    assert 'id="dyn-color"' in report
    assert 'id="pisa-analysis-data"' in report
    assert '"paramNames": ["x", "y", "z"]' in report
    assert '"success":"#16a34a"' in report
    assert '"invalid":"#2563eb"' in report
    assert '"fail":"#dc2626"' in report
    assert "Download Filtered CSV" in report


def test_analyze_runner_spec_materializes_samples(tmp_path: Path) -> None:
    runner_spec = _write_runner_fixture(tmp_path, sample_count=2)
    output_dir = tmp_path / "analysis"

    result = analyze_samples(
        runner_spec_path=runner_spec,
        output_dir=output_dir,
        params=["x", "y"],
    )

    assert result.record_count == 4
    summary = yaml.safe_load((output_dir / "summary.yaml").read_text(encoding="utf-8"))
    assert summary["source_type"] == "runner_spec"
    assert summary["record_count"] == 4


def test_load_records_from_results_parses_params_outcomes_and_metrics(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    _write_result_iteration(results_dir, "1", {"x": 1, "y": 2}, "success")
    _write_result_iteration(results_dir, "2", {"x": 3, "y": 4}, "test_fail")

    records = load_records_from_results(results_dir)

    assert [record.sample_id for record in records] == ["1", "2"]
    assert records[0].outcome == "success"
    assert records[1].stop_condition == "collision"
    assert records[0].metrics["ego_to_agent_1.min_ttc_s"] == 1.25


def test_load_records_from_results_uses_last_summary_row(tmp_path: Path) -> None:
    monitor_dir = tmp_path / "results" / "iteration_1" / "monitor"
    monitor_dir.mkdir(parents=True)
    with (monitor_dir / "result.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "run.status",
                "run.test_outcome",
                "run.stop_condition",
                "run.stop_reason",
                "run.params",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "run.status": "error",
                "run.test_outcome": "unknown",
                "run.stop_condition": "",
                "run.stop_reason": "retry",
                "run.params": json.dumps({"x": 1}),
            }
        )
        writer.writerow(
            {
                "run.status": "finished",
                "run.test_outcome": "success",
                "run.stop_condition": "goal",
                "run.stop_reason": "done",
                "run.params": json.dumps({"x": 2}),
            }
        )

    records = load_records_from_results(tmp_path / "results")

    assert len(records) == 1
    assert records[0].status == "finished"
    assert records[0].outcome == "success"
    assert records[0].params == {"x": 2}


def test_load_records_from_results_strips_padded_monitor_csv(tmp_path: Path) -> None:
    monitor_dir = tmp_path / "results" / "iteration_1" / "monitor"
    monitor_dir.mkdir(parents=True)
    (monitor_dir / "result.csv").write_text(
        'run.status, run.test_outcome, run.stop_condition, run.stop_reason, run.params\n'
        'finished  , success         , goal, done, "{""x"": 2}"\n'
        '  \n',
        encoding="utf-8",
    )

    records = load_records_from_results(tmp_path / "results")

    assert len(records) == 1
    assert records[0].status == "finished"
    assert records[0].outcome == "success"
    assert records[0].params == {"x": 2}


def test_analyze_results_colors_by_outcome(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    output_dir = tmp_path / "analysis"
    _write_result_iteration(results_dir, "1", {"x": 1, "y": 2, "z": 3}, "success")
    _write_result_iteration(results_dir, "2", {"x": 3, "y": 4, "z": 5}, "invalid")
    _write_result_iteration(results_dir, "3", {"x": 5, "y": 6, "z": 7}, "test_fail")

    result = analyze_samples(
        results_path=results_dir,
        output_dir=output_dir,
        params=["x", "y", "z"],
        color_by="outcome",
    )

    assert result.record_count == 3
    summary = yaml.safe_load((output_dir / "summary.yaml").read_text(encoding="utf-8"))
    assert summary["outcomes"] == {"success": 1, "invalid": 1, "test_fail": 1}
    scatter_svg = (output_dir / "figures" / "scatter_2d.svg").read_text(encoding="utf-8")
    assert "#16a34a" in scatter_svg
    assert "#2563eb" in scatter_svg
    assert "#dc2626" in scatter_svg


def test_analyze_results_colors_numeric_metric_with_continuous_scale(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    output_dir = tmp_path / "analysis"
    _write_result_iteration(
        results_dir,
        "1",
        {"x": 1, "y": 2, "z": 3},
        "success",
        min_ttc=0.0,
    )
    _write_result_iteration(
        results_dir,
        "2",
        {"x": 3, "y": 4, "z": 5},
        "success",
        min_ttc=5.0,
    )
    _write_result_iteration(
        results_dir,
        "3",
        {"x": 5, "y": 6, "z": 7},
        "success",
        min_ttc=10.0,
    )

    analyze_samples(
        results_path=results_dir,
        output_dir=output_dir,
        params=["x", "y", "z"],
        color_by="metric:ego_to_agent_1.min_ttc_s",
    )

    overview_svg = (output_dir / "figures" / "class_counts.svg").read_text(encoding="utf-8")
    scatter_svg = (output_dir / "figures" / "scatter_2d.svg").read_text(encoding="utf-8")
    report = (output_dir / "report.html").read_text(encoding="utf-8")
    assert "Color scale: metric:ego_to_agent_1.min_ttc_s" in overview_svg
    assert "#eff6ff" in scatter_svg
    assert "#2987dc" in scatter_svg
    assert "continuousBlue" in report
    assert "gradient" in report


def test_analyze_results_embeds_post_outcome_eval(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    output_dir = tmp_path / "analysis"
    config_path = tmp_path / "post_conditions.yaml"
    _write_result_iteration(
        results_dir,
        "1",
        {"x": 1, "y": 2, "z": 3},
        "success",
        min_ttc=0.5,
    )
    _write_yaml(
        config_path,
        {
            "condition": {
                "type": "result_metric_threshold",
                "name": "low_summary_ttc",
                "outcome": "Fail",
                "metric": "ego_to_agent_1.min_ttc_s",
                "rule": "lt",
                "value": 1.0,
            }
        },
    )

    analyze_samples(
        results_path=results_dir,
        output_dir=output_dir,
        params=["x", "y", "z"],
        post_outcome_config_path=config_path,
    )

    summary = yaml.safe_load((output_dir / "summary.yaml").read_text(encoding="utf-8"))
    samples_csv = (output_dir / "samples.csv").read_text(encoding="utf-8")
    report = (output_dir / "report.html").read_text(encoding="utf-8")
    assert summary["post_outcome"]["triggered_count"] == 1
    assert summary["post_outcome"]["outcomes"] == {"fail": 1}
    assert "post_outcome,post_stop_condition" in samples_csv
    assert "fail,low_summary_ttc" in samples_csv
    assert 'id="dyn-outcome-source"' in report
    assert "Post Outcome Lab" in report
    assert '"post_outcome": {"test_outcome": "fail"' in report


def test_analyze_post_outcome_modes_handle_non_triggered_records(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    overlay_output_dir = tmp_path / "overlay_analysis"
    replace_output_dir = tmp_path / "replace_analysis"
    config_path = tmp_path / "post_conditions.yaml"
    _write_result_iteration(
        results_dir,
        "1",
        {"x": 1, "y": 2, "z": 3},
        "invalid",
        min_ttc=2.0,
    )
    _write_yaml(
        config_path,
        {
            "condition": {
                "type": "result_metric_threshold",
                "name": "low_summary_ttc",
                "outcome": "Fail",
                "metric": "ego_to_agent_1.min_ttc_s",
                "rule": "lt",
                "value": 1.0,
            }
        },
    )

    analyze_samples(
        results_path=results_dir,
        output_dir=overlay_output_dir,
        params=["x", "y", "z"],
        post_outcome_config_path=config_path,
    )
    analyze_samples(
        results_path=results_dir,
        output_dir=replace_output_dir,
        params=["x", "y", "z"],
        post_outcome_config_path=config_path,
        post_outcome_mode="replace",
    )

    overlay_summary = yaml.safe_load((overlay_output_dir / "summary.yaml").read_text(encoding="utf-8"))
    replace_summary = yaml.safe_load((replace_output_dir / "summary.yaml").read_text(encoding="utf-8"))
    assert overlay_summary["post_outcome"]["mode"] == "overlay"
    assert overlay_summary["post_outcome"]["outcomes"] == {"invalid": 1}
    assert replace_summary["post_outcome"]["mode"] == "replace"
    assert replace_summary["post_outcome"]["outcomes"] == {"unknown": 1}


def test_cli_analyze_samples(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    samples_path = tmp_path / "explicit.yaml"
    output_dir = tmp_path / "analysis"
    _write_explicit_samples(samples_path)

    assert (
        main(
            [
                "--samples",
                str(samples_path),
                "--output",
                str(output_dir),
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert "records: 3" in captured.out
    assert "initial_params:" in captured.out
    assert (output_dir / "report.html").exists()


def test_cli_analyze_accepts_histogram_bins(tmp_path: Path) -> None:
    samples_path = tmp_path / "explicit.yaml"
    output_dir = tmp_path / "analysis"
    _write_explicit_samples(samples_path)

    assert (
        main(
            [
                "--samples",
                str(samples_path),
                "--bins",
                "7",
                "--output",
                str(output_dir),
            ]
        )
        == 0
    )

    summary = yaml.safe_load((output_dir / "summary.yaml").read_text(encoding="utf-8"))
    report = (output_dir / "report.html").read_text(encoding="utf-8")
    assert summary["bins"] == 7
    assert 'id="dyn-bins"' in report
    assert '"defaultBins": 7' in report
    assert "const bins = Math.max" in report


def test_analyze_rejects_non_positive_bins(tmp_path: Path) -> None:
    samples_path = tmp_path / "explicit.yaml"
    _write_explicit_samples(samples_path)

    with pytest.raises(ValueError, match="bins must be greater than 0"):
        analyze_samples(
            samples_path=samples_path,
            output_dir=tmp_path / "analysis",
            bins=0,
        )


def test_cli_analyze_samples_keeps_all_params_available_when_initial_params_are_set(
    tmp_path: Path,
) -> None:
    samples_path = tmp_path / "explicit.yaml"
    output_dir = tmp_path / "analysis"
    _write_explicit_samples(samples_path)

    assert (
        main(
            [
                "--samples",
                str(samples_path),
                "--params",
                "x,y",
                "--output",
                str(output_dir),
            ]
        )
        == 0
    )

    report = (output_dir / "report.html").read_text(encoding="utf-8")
    summary = yaml.safe_load((output_dir / "summary.yaml").read_text(encoding="utf-8"))
    assert summary["selected_params"] == ["x", "y"]
    assert '"paramNames": ["x", "y", "z"]' in report


def test_analyze_uses_first_three_initial_params(tmp_path: Path) -> None:
    samples_path = tmp_path / "explicit.yaml"
    output_dir = tmp_path / "analysis"
    _write_explicit_samples(samples_path)

    result = analyze_samples(
        samples_path=samples_path,
        output_dir=output_dir,
        params=["x", "y", "z", "x"],
    )

    assert result.selected_params == ("x", "y", "z")
