from pisa_sample_tools.evidence.metric_status import metric_coverage, status_points


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
