from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from pisa_sample_tools.webapp.reports import (
    _linear_percentile,
    _normalized_comparisons,
    _normalized_cross_experiment_summary,
)
from pisa_sample_tools.webapp.visualizations import (
    VisualizationError,
    build_visualizations,
    export_visualization,
)


def _normalized_report(tmp_path: Path) -> Path:
    root = tmp_path / "normalized-report"
    database = root / "report" / "index.sqlite"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE datasets (dataset_id TEXT PRIMARY KEY);
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            dataset_id TEXT NOT NULL,
            parameter_hash TEXT,
            outcome_class TEXT NOT NULL,
            has_collision INTEGER NOT NULL DEFAULT 0,
            trace_paths_json TEXT
        );
        CREATE TABLE parameters (
            run_id TEXT NOT NULL,
            name TEXT NOT NULL,
            value_real REAL,
            value_type TEXT NOT NULL,
            PRIMARY KEY(run_id, name)
        );
        CREATE TABLE metrics (
            run_id TEXT NOT NULL,
            name TEXT NOT NULL,
            value_real REAL,
            value_type TEXT NOT NULL,
            PRIMARY KEY(run_id, name)
        );
        CREATE TABLE findings (
            finding_id INTEGER PRIMARY KEY,
            code TEXT NOT NULL,
            dataset_id TEXT
        );
        CREATE TABLE dataset_relations (
            left_dataset_id TEXT NOT NULL,
            right_dataset_id TEXT NOT NULL,
            role TEXT NOT NULL,
            details_json TEXT NOT NULL,
            PRIMARY KEY(left_dataset_id, right_dataset_id)
        );
        """
    )
    connection.execute("INSERT INTO metadata VALUES ('schema_version', '1')")
    connection.executemany(
        "INSERT INTO datasets(dataset_id) VALUES (?)", [("alpha",), ("beta",), ("alias",)]
    )
    outcomes = ("success", "fail", "invalid", "unknown", "success", "fail")
    for index, outcome in enumerate(outcomes):
        dataset = "alpha" if index < 3 else "beta"
        run_id = f"{dataset}:{index}"
        connection.execute(
            "INSERT INTO runs(run_id, dataset_id, parameter_hash, outcome_class, has_collision) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, dataset, f"hash-{index % 3}", outcome, int(index in {1, 5})),
        )
        connection.executemany(
            "INSERT INTO parameters(run_id, name, value_real, value_type) VALUES (?, ?, ?, 'number')",
            ((run_id, "speed", 10.0 + index), (run_id, "gap", 2.0 * index)),
        )
        metrics = [
            (run_id, "run.wall_time_ms", 100.0 + 10.0 * index),
            (run_id, "run.final_sim_time_ms", 1_000.0 - 20.0 * index),
            (run_id, "run.speedup", 2.0 + 0.1 * index),
            (run_id, "min_ttc.min_ttc_s", 0.5 + index),
            (run_id, "ego_to_agent_1_distance.min", 2.0 + index),
            (run_id, "ego_to_agent_1_drac.max", 0.2 + index),
        ]
        if index == 2:
            metrics = [item for item in metrics if item[1] != "min_ttc.min_ttc_s"]
        if index == 5:
            metrics = [item for item in metrics if item[1] != "ego_to_agent_1_distance.min"]
        connection.executemany(
            "INSERT INTO metrics(run_id, name, value_real, value_type) VALUES (?, ?, ?, 'number')",
            metrics,
        )
        trace_dir = root / "traces" / run_id.replace(":", "-")
        trace_dir.mkdir(parents=True)
        trace_path = trace_dir / "agent_states.csv"
        sample_index = index % 3
        endpoint = float(sample_index) if dataset == "alpha" else float(2 * sample_index + 1)
        trace_path.write_text(
            "time,is_ego,agent_id,x,y\n"
            f"0,true,0,{sample_index},0\n"
            f"1,true,0,{endpoint},0\n",
            encoding="utf-8",
        )
        connection.execute(
            "UPDATE runs SET trace_paths_json = ? WHERE run_id = ?",
            (json.dumps({"agent_states": str(trace_path)}), run_id),
        )
    # This duplicate alias remains browsable in the index, but must not inflate aggregates.
    connection.execute(
        "INSERT INTO runs(run_id, dataset_id, parameter_hash, outcome_class, has_collision) "
        "VALUES ('alias:0', 'alias', 'hash-alias', 'success', 0)"
    )
    connection.executemany(
        "INSERT INTO parameters(run_id, name, value_real, value_type) VALUES "
        "('alias:0', ?, ?, 'number')",
        (("speed", 999.0), ("gap", 999.0)),
    )
    connection.executemany(
        "INSERT INTO findings(code, dataset_id) VALUES ('duplicate_alias', ?)",
        [("alpha",), ("alias",)],
    )
    connection.execute(
        "INSERT INTO metrics(run_id, name, value_real, value_type) "
        "VALUES ('alias:0', 'run.wall_time_ms', 999999, 'number')"
    )
    connection.execute(
        "INSERT INTO dataset_relations(left_dataset_id, right_dataset_id, role, details_json) "
        "VALUES ('alpha', 'beta', 'paired_policy_intervention', ?) ",
        (json.dumps({"matched_count": 3, "semantic_compatible": True}),),
    )
    connection.execute(
        "INSERT INTO dataset_relations(left_dataset_id, right_dataset_id, role, details_json) "
        "VALUES ('alpha', 'alias', 'duplicate_alias', '{}')"
    )
    connection.commit()
    connection.close()
    return root


def test_cross_experiment_summary_uses_complete_valid_metric_samples(tmp_path: Path) -> None:
    root = _normalized_report(tmp_path)

    summary = _normalized_cross_experiment_summary(root / "report" / "index.sqlite")

    assert summary["available"] is True
    assert summary["experiments"] == ["alpha", "beta"]
    assert summary["excluded_duplicate_aliases"] == ["alias"]
    assert summary["common_sample_count"] == 3

    discrete = {item["key"]: item for item in summary["discrete"]}
    assert discrete["outcome"]["consistent_count"] == 0
    assert discrete["outcome"]["comparable_count"] == 3
    assert discrete["collision"]["consistent_count"] == 1
    assert discrete["collision"]["agreement_ratio"] == pytest.approx(1 / 3)

    metrics = {item["key"]: item for item in summary["continuous"]}
    ttc = metrics["min_ttc.min_ttc_s"]
    assert ttc["eligible_sample_count"] == 2
    assert ttc["partial_sample_count"] == 1
    assert ttc["missing_execution_count"] == 1
    assert ttc["variation_max"] == pytest.approx(3.0)
    assert ttc["variation_min"] == pytest.approx(3.0)
    assert ttc["variation_p95"] == pytest.approx(3.0)
    assert "variation_mean" not in ttc
    assert ttc["variation_std"] == pytest.approx(0.0)
    assert ttc["variation_median"] == pytest.approx(3.0)
    assert ttc["representatives"]["max"] == {
        "parameter_hash": "hash-0",
        "run_id": "alpha:0",
        "variation": pytest.approx(3.0),
    }
    assert ttc["representatives"]["std"]["run_id"] == "alpha:0"
    assert ttc["representatives"]["p95"]["run_id"] == "alpha:0"
    trajectory = summary["trajectory"]
    assert trajectory["available"] is True
    assert trajectory["eligible_sample_count"] == 3
    assert trajectory["ade"]["max"] == pytest.approx(1.5)
    assert trajectory["fde"]["max"] == pytest.approx(3.0)
    assert trajectory["fde"]["representatives"]["max"]["left_run_id"] == "alpha:2"
    assert trajectory["fde"]["representatives"]["max"]["right_run_id"] == "beta:5"
    assert trajectory["fde"]["representatives"]["max"]["common_steps"] == 2


def test_cross_experiment_percentile_uses_linear_interpolation() -> None:
    assert _linear_percentile([0.0, 10.0], 0.95) == pytest.approx(9.5)
    assert _linear_percentile([], 0.95) is None


def test_specs_are_echarts_compatible_deterministic_and_disclose_sampling(
    tmp_path: Path,
) -> None:
    report = _normalized_report(tmp_path)

    outcomes = build_visualizations(report, section="outcomes")

    assert [item["id"] for item in outcomes[:3]] == [
        "outcomes-overall",
        "outcomes-by-dataset",
        "safety-collision-rate-by-dataset",
    ]
    assert outcomes[0]["kind"] == "bar"
    assert outcomes[0]["option"]["series"][0]["type"] == "bar"
    assert outcomes[0]["disclosure"]["population_count"] == 6
    assert outcomes[0]["disclosure"]["excluded_duplicate_alias_datasets"] == 1
    assert sum(point["value"] for point in outcomes[0]["option"]["series"][0]["data"]) == 6
    assert [row for row in outcomes[1]["option"]["yAxis"]["data"]] == ["alpha", "beta"]
    assert all(item["disclosure"].get("missing_values_are_zero") is False for item in outcomes[2:])

    first = build_visualizations(report, section="sampling", maximum_points=3, maximum_parameters=2)
    second = build_visualizations(
        report, section="sampling", maximum_points=3, maximum_parameters=2
    )

    assert first == second
    assert len(first) == 3  # two histograms and their leading-parameter scatter
    assert len({item["id"] for item in first}) == 3
    assert all(item["data_hash"] for item in first)
    assert all(item["disclosure"]["population_count"] == 6 for item in first)
    assert all(item["disclosure"]["plotted_count"] == 3 for item in first)
    assert all(item["clipped_count"] == 3 for item in first)
    assert all("Deterministic sample" in item["subtitle"] for item in first)
    scatter = next(item for item in first if item["kind"] == "scatter")
    assert sum(len(series["data"]) for series in scatter["option"]["series"]) == 3
    exported = export_visualization(
        report,
        scatter["id"],
        format="json",
        maximum_points=3,
        maximum_parameters=2,
    )
    assert exported["data_hash"] == scatter["data_hash"]


@pytest.mark.parametrize("export_format", ["png", "svg", "pdf", "csv", "json"])
def test_publication_and_data_exports_are_atomic_and_report_relative(
    tmp_path: Path, export_format: str
) -> None:
    report = _normalized_report(tmp_path)

    result = export_visualization(
        report,
        "outcomes-overall",
        format=export_format,
        preset="paper-double",
        dpi=300,
    )

    target = report / result["path"]
    assert target.is_file()
    assert target.resolve().is_relative_to(report.resolve())
    assert result["size"] == target.stat().st_size > 0
    assert result["data_hash"]
    assert not list(target.parent.glob(".*.tmp"))
    if export_format == "png":
        assert target.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        with Image.open(target) as image:
            assert image.size == (2126, 1275)
    elif export_format == "svg":
        text = target.read_text(encoding="utf-8")
        assert "<svg" in text
        assert "<script" not in text
        assert str(tmp_path) not in text
    elif export_format == "pdf":
        assert target.read_bytes().startswith(b"%PDF")
    elif export_format == "csv":
        with target.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert [row["outcome"] for row in rows] == ["success", "fail", "invalid", "unknown"]
        assert all(row["_visualization_id"] == "outcomes-overall" for row in rows)
    else:
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["visualization"]["id"] == "outcomes-overall"
        assert len(payload["data"]) == 4


def test_slide_png_presets_have_exact_pixel_canvases(tmp_path: Path) -> None:
    report = _normalized_report(tmp_path)

    for preset, expected in (("slides-hd", (1920, 1080)), ("slides-4k", (3840, 2160))):
        result = export_visualization(
            report,
            "outcomes-overall",
            format="png",
            preset=preset,
        )
        with Image.open(report / result["path"]) as image:
            assert image.size == expected


def test_every_generated_id_is_exportable_and_inputs_cannot_choose_paths(tmp_path: Path) -> None:
    report = _normalized_report(tmp_path)
    specs = build_visualizations(report)

    for spec in specs:
        exported = export_visualization(report, spec["id"], format="json")
        assert exported["path"].startswith("exports/visualizations/")

    with pytest.raises(VisualizationError, match="safe lowercase identifier"):
        export_visualization(report, "../../outside", format="json")
    with pytest.raises(VisualizationError, match="unsupported export format"):
        export_visualization(report, "outcomes-overall", format="html")
    with pytest.raises(VisualizationError, match="unsupported publication preset"):
        export_visualization(report, "outcomes-overall", format="svg", preset="poster")
    with pytest.raises(VisualizationError, match="background"):
        export_visualization(
            report, "outcomes-overall", format="png", background="url(https://example.test)"
        )
    with pytest.raises(VisualizationError, match="unknown visualization id"):
        export_visualization(report, "missing-chart", format="json")


def test_performance_and_safety_metrics_are_capped_and_never_zero_imputed(
    tmp_path: Path,
) -> None:
    report = _normalized_report(tmp_path)

    first = build_visualizations(report, section="performance", maximum_points=3)
    second = build_visualizations(report, section="performance", maximum_points=3)
    safety = build_visualizations(report, section="outcomes", maximum_points=3)

    assert first == second
    assert any(item["id"].startswith("performance-distribution-") for item in first)
    assert any(item["id"].startswith("performance-scatter-") for item in first)
    assert all(item["disclosure"]["population_count"] == 6 for item in first)
    assert all(item["disclosure"]["plotted_count"] == 3 for item in first)
    assert all(item["disclosure"]["sampled"] is True for item in first)
    assert all(item["kind"] in {"line", "scatter"} for item in first)

    ttc = next(
        item
        for item in safety
        if item["disclosure"].get("semantic_role") == "safety.time_to_collision"
    )
    assert ttc["disclosure"]["population_count"] == 5
    assert ttc["disclosure"]["missing_or_nonnumeric_count"] == 1
    assert ttc["disclosure"]["missing_values_are_zero"] is False
    assert "never imputed as zero" in ttc["subtitle"]
    assert ttc["disclosure"]["risk_direction"] == "lower_is_riskier"

    for spec in [*first, *safety[2:]]:
        for export_format in ("png", "svg", "pdf", "csv", "json"):
            exported = export_visualization(
                report,
                spec["id"],
                format=export_format,
                maximum_points=3,
            )
            assert (report / exported["path"]).stat().st_size > 0


def test_comparison_section_matches_api_ids_and_exports_every_generated_chart(
    tmp_path: Path,
) -> None:
    report = _normalized_report(tmp_path)
    relation_id = hashlib.sha256(b"alpha\0beta").hexdigest()[:20]
    section = f"compare:{relation_id}"

    specs = build_visualizations(report, section=section, maximum_points=2)

    assert specs[0]["id"] == f"comparison-outcomes-{relation_id}"
    assert specs[1]["id"] == f"comparison-transitions-{relation_id}"
    assert any(item["id"].startswith(f"comparison-delta-{relation_id}-") for item in specs)
    outcomes = specs[0]
    assert sum(outcomes["option"]["series"][0]["data"]) == pytest.approx(100)
    assert sum(outcomes["option"]["series"][1]["data"]) == pytest.approx(100)
    transitions = specs[1]
    assert transitions["disclosure"]["unique_pair_count"] == 3
    assert transitions["disclosure"]["comparison_role"] == "paired_policy_intervention"
    assert transitions["disclosure"]["pairing_key"] == "parameter_hash unique within each dataset"
    assert all(item["disclosure"].get("missing_values_are_zero") is False for item in specs)

    for spec in specs:
        for export_format in ("png", "svg", "pdf", "csv", "json"):
            exported = export_visualization(
                report,
                spec["id"],
                format=export_format,
                maximum_points=2,
            )
            assert (report / exported["path"]).stat().st_size > 0

    with pytest.raises(VisualizationError, match="unknown comparison relation"):
        build_visualizations(report, section="compare:00000000000000000000")
    with pytest.raises(VisualizationError, match="unsupported visualization section"):
        build_visualizations(report, section="compare:alpha-beta")


def test_pair_information_agreement_ignores_only_task_bookkeeping(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            dataset_id TEXT NOT NULL,
            parameter_hash TEXT,
            outcome_class TEXT NOT NULL,
            has_collision INTEGER NOT NULL
        );
        CREATE TABLE parameters (
            run_id TEXT NOT NULL,
            name TEXT NOT NULL,
            value_real REAL,
            value_type TEXT NOT NULL
        );
        CREATE TABLE metrics (
            run_id TEXT NOT NULL,
            name TEXT NOT NULL,
            value_real REAL,
            value_type TEXT NOT NULL
        );
        CREATE TABLE dataset_relations (
            left_dataset_id TEXT NOT NULL,
            right_dataset_id TEXT NOT NULL,
            role TEXT NOT NULL,
            details_json TEXT NOT NULL
        );
        """
    )
    for dataset in ("alpha", "beta"):
        for sample in (1, 2):
            run_id = f"{dataset}:{sample}"
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, 'success', 0)",
                (run_id, dataset, f"hash-{sample}"),
            )
            connection.execute(
                "INSERT INTO parameters VALUES (?, 'scenario.speed', 10, 'number')",
                (run_id,),
            )
            connection.executemany(
                "INSERT INTO metrics VALUES (?, ?, ?, 'number')",
                (
                    (run_id, "min_ttc", 2.0 if sample == 1 else 3.0 + (dataset == "beta")),
                    (run_id, "run.wall_time_ms", 100.0 if dataset == "alpha" else 900.0),
                    (run_id, "run.speedup", 2.0 if dataset == "alpha" else 20.0),
                    (run_id, "job_id", 1.0 if dataset == "alpha" else 99.0),
                ),
            )
    connection.execute(
        "INSERT INTO dataset_relations VALUES "
        "('alpha', 'beta', 'paired_replicate', '{\"matched_count\": 2}')"
    )
    connection.commit()
    connection.close()

    comparison = _normalized_comparisons(database)[0]

    assert comparison["information_consistent_count"] == 1
    assert comparison["information_comparable_count"] == 2
    assert comparison["information_agreement_ratio"] == pytest.approx(0.5)
    assert "speedup" in comparison["information_exclusions"]


def test_only_fixed_normalized_index_is_read_and_symlink_escape_is_rejected(tmp_path: Path) -> None:
    report = tmp_path / "report"
    report.mkdir()
    outside = _normalized_report(tmp_path / "outside") / "report" / "index.sqlite"
    (report / "report").mkdir()
    (report / "report" / "index.sqlite").symlink_to(outside)

    with pytest.raises(VisualizationError, match="escapes"):
        build_visualizations(report)
