from __future__ import annotations

import csv
import json
from pathlib import Path

import yaml

from pisa_sample_tools.evidence import load_analysis_spec
from pisa_sample_tools.evidence.builder import (
    browse_path,
    campaign_document,
    compare_experiments,
    inspect_output,
    preview_experiment,
    scan_reports,
    validate_builder_request,
)
from pisa_sample_tools.evidence.builder_server import create_builder_app
from pisa_sample_tools.evidence.spec import spec_to_dict


def _result_root(path: Path, samples: list[tuple[str, dict[str, float]]]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "summary.csv").write_text("status,count\nfinished,1\n", encoding="utf-8")
    for index, (sample_id, params) in enumerate(samples, start=1):
        monitor = path / f"iteration_{index}" / "monitor"
        monitor.mkdir(parents=True)
        with (monitor / "result.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "run.status",
                    "run.test_outcome",
                    "run.stop_condition",
                    "run.sample_id",
                    "run.params",
                    "ego_to_agent_1.min_ttc_s",
                    "ego_to_agent_1.min_distance_m",
                    "ego_deceleration.max",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "run.status": "finished",
                    "run.test_outcome": "success",
                    "run.stop_condition": "goal",
                    "run.sample_id": sample_id,
                    "run.params": json.dumps(params),
                    "ego_to_agent_1.min_ttc_s": 3,
                    "ego_to_agent_1.min_distance_m": 4,
                    "ego_deceleration.max": 1,
                }
            )
    return path


def _modern_manifest(path: Path) -> None:
    (path / "execution_manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "scenario_name": "cut-in",
                "execution": {"sampler_name": "lhs"},
                "resolved_inputs": {"map_xodr": str(path / "xodr")},
                "metadata": {"map_name": "town"},
                "components": {
                    "simulator": {
                        "wrapper": {"name": "sim-wrapper", "version": "1.2"},
                        "component": {"name": "esmini", "metadata": {"port": 1}},
                    },
                    "av": {
                        "wrapper": {"name": "av-wrapper", "version": "2.3"},
                        "component": {"name": "autoware"},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    xodr = path / "xodr" / "town.xodr"
    xodr.parent.mkdir()
    xodr.write_text("<OpenDRIVE/>", encoding="utf-8")


def test_preview_and_strict_sample_compatibility(tmp_path: Path) -> None:
    metadata = {"logical_scenario_name": "cut-in"}
    left = preview_experiment(_result_root(tmp_path / "left", [("a", {"x": 1})]), metadata)
    right = preview_experiment(_result_root(tmp_path / "right", [("a", {"x": 1})]), metadata)
    mismatch = preview_experiment(_result_root(tmp_path / "bad", [("a", {"x": 2})]), metadata)

    assert compare_experiments([left, right])["compatible"] is True
    result = compare_experiments([left, mismatch])
    assert result["compatible"] is False
    assert result["errors"][0]["parameter_mismatches"] == [f"{left['scenario_name']}:a"]


def test_component_differences_do_not_block_campaign(tmp_path: Path) -> None:
    samples = [("a", {"x": 1})]
    left = preview_experiment(
        _result_root(tmp_path / "left", samples), {"logical_scenario_name": "cut-in", "av_name": "A", "simulator_name": "sim"}
    )
    right = preview_experiment(
        _result_root(tmp_path / "right", samples), {"logical_scenario_name": "cut-in", "av_name": "B", "simulator_name": "sim"}
    )

    result = compare_experiments([left, right])

    assert result["compatible"] is True
    assert result["component_differences"]["av"] == {"left": "A", "right": "B"}


def test_builder_validation_and_campaign_document(tmp_path: Path) -> None:
    experiment = preview_experiment(_result_root(tmp_path / "results", [("a", {"x": 1})]))
    spec = spec_to_dict(load_analysis_spec(None))

    validation = validate_builder_request([experiment], spec)
    campaign = campaign_document([experiment])

    assert validation["valid"] is True
    assert validation["run_count"] == 1
    assert campaign["datasets"][0]["results"] == str(tmp_path / "results")


def test_modern_manifest_prefills_components_sampler_and_xodr(tmp_path: Path) -> None:
    root = _result_root(tmp_path / "result", [("a", {"x": 1})])
    _modern_manifest(root)

    preview = preview_experiment(root)

    assert preview["simulator"] == "esmini"
    assert preview["av"] == "autoware"
    assert preview["sampler"] == "lhs"
    assert preview["simulator_component"]["wrapper_name"] == "sim-wrapper"
    assert preview["av_component"]["wrapper_version"] == "2.3"
    assert preview["xodr_path"].endswith("xodr/town.xodr")
    assert browse_path(root)["is_experiment"] is True


def test_report_catalog_only_includes_evidence_bundles(tmp_path: Path) -> None:
    valid = tmp_path / "nested" / "report-one"
    (valid / "report").mkdir(parents=True)
    (valid / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "tool": "pisa-analysis-tools",
                "generated_at": "2026-01-01T00:00:00Z",
                "run_count": 3,
                "warning_count": 1,
            }
        ),
        encoding="utf-8",
    )
    (valid / "report" / "analysis_report.html").write_text("report", encoding="utf-8")
    (valid / "report" / "analysis_data.json").write_text(
        json.dumps(
            {
                "report_mode": "single",
                "summary": {"experiment_count": 1, "parameter_count": 2},
                "experiments": [{"id": "a", "av": "autoware", "simulator": "esmini"}],
            }
        ),
        encoding="utf-8",
    )
    other = tmp_path / "trajectory"
    (other / "report").mkdir(parents=True)
    (other / "manifest.yaml").write_text("tool: trajectory\n", encoding="utf-8")
    (other / "report" / "analysis_report.html").write_text("other", encoding="utf-8")

    catalog = scan_reports(tmp_path)

    assert [item["name"] for item in catalog["reports"]] == ["report-one"]
    assert catalog["reports"][0]["run_count"] == 3
    assert inspect_output(valid)["state"] == "pisa_report"
    assert inspect_output(other)["state"] == "non_pisa_nonempty"


def test_builder_api_requires_token_and_serves_ui() -> None:
    app = create_builder_app("secret")

    assert app.state.token == "secret"
    paths = {route.path for route in app.routes}
    assert "/api/build" in paths
    assert "/api/reports" in paths
    assert "/api/output" in paths
    assert "/api/jobs/{job_id}/events" in paths
    assert "/reports/{report_token}/{job_id}/{asset_path:path}" in paths
    assert "/library/{report_token}/{report_id}/{asset_path:path}" in paths


def test_builder_ui_starts_with_report_browser_and_has_validated_controls() -> None:
    html = (
        Path(__file__).parents[1]
        / "src"
        / "pisa_sample_tools"
        / "evidence"
        / "builder_web"
        / "index.html"
    ).read_text(encoding="utf-8")

    assert "PISA Report Browser" in html
    assert "Create New Report" in html
    assert 'data-dir="true">↩ ..' in html
    assert 'id="format-svg" type="checkbox"' in html
    assert 'id="format-png" type="checkbox"' in html
    assert "Back to Report Browser" in html
