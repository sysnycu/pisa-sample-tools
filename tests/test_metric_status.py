import csv

from pisa_sample_tools.evidence.metric_status import metric_coverage, status_points
from pisa_sample_tools.evidence.models import MetricBinding, RunRecord
from pisa_sample_tools.evidence.service import _metric_unavailable_coverage


def test_ttc_status_distinguishes_not_applicable_from_missing() -> None:
    rows = [
        {
            "sim_time_ms": "0",
            "pair.ttc_s": "",
            "pair.ttc_valid": "False",
            "pair.ttc_status": "outside_lateral_threshold",
        },
        {
            "sim_time_ms": "50",
            "pair.ttc_s": "2.5",
            "pair.ttc_valid": "True",
            "pair.ttc_status": "valid",
        },
        {
            "sim_time_ms": "100",
            "pair.ttc_s": "",
            "pair.ttc_valid": "True",
            "pair.ttc_status": "valid",
        },
        {"sim_time_ms": "150", "pair.ttc_s": "", "pair.ttc_valid": "", "pair.ttc_status": ""},
    ]
    coverage = metric_coverage(rows, "pair.ttc_s")
    assert coverage["valid"] == 1
    assert coverage["not_applicable"] == 1
    assert coverage["invalid"] == 1
    assert coverage["missing"] == 1
    assert status_points(rows, "pair.ttc_s")[0]["status"] == "outside_lateral_threshold"


def test_report_coverage_preserves_run_level_not_applicable_reasons(tmp_path) -> None:
    frame_path = tmp_path / "frame_metrics.csv"
    with frame_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["pair.ttc_s", "pair.ttc_valid", "pair.ttc_status"],
        )
        writer.writeheader()
        writer.writerows(
            [
                {
                    "pair.ttc_s": "",
                    "pair.ttc_valid": "False",
                    "pair.ttc_status": "outside_lateral_threshold",
                },
                {
                    "pair.ttc_s": "",
                    "pair.ttc_valid": "False",
                    "pair.ttc_status": "non_closing",
                },
            ]
        )
    run = RunRecord(
        experiment_id="experiment",
        scenario_id="1",
        sample_id="1",
        logical_scenario_name="cutin",
        params={},
        metadata={},
        status="finished",
        outcome="success",
        termination_reason="goal",
        stop_reason="goal",
        metrics={},
        result_path=tmp_path,
        frame_metrics_path=frame_path,
    )

    coverage = _metric_unavailable_coverage(
        [run], MetricBinding(summary="pair.min_ttc_s", series="pair.ttc_s")
    )

    assert coverage == {
        "not_applicable": 1,
        "not_applicable_reasons": {
            "non_closing": 1,
            "outside_lateral_threshold": 1,
        },
    }
