from __future__ import annotations

import csv
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from pisa_sample_tools.reporting import (
    ReportBundleError,
    build_report_bundle,
    rebuild_legacy_report,
)


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _experiment(
    root: Path, *, execution: str, outcome: str = "success", av: str = "simple-av"
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "execution_manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "execution_id": execution,
                "scenario_name": "cut-in",
                "completed_at": "2026-01-01T00:00:00Z",
                "summary": {"finished": 1},
                "execution": {"sampler_name": "lhs"},
                "components": {
                    "simulator": {
                        "component": {"name": "esmini"},
                        "wrapper": {"name": "esmini-wrapper", "version": "1.0"},
                    },
                    "av": {
                        "component": {"name": av},
                        "wrapper": {"name": "av-wrapper", "version": "1.0"},
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monitor = root / "iteration_1" / "monitor"
    _write_csv(
        monitor / "result.csv",
        [
            {
                "run.status": "finished",
                "run.test_outcome": outcome,
                "run.sample_id": "1",
                "run.attempt": 1,
                "run.parameter_hash": "same-hash",
                "run.params": json.dumps({"x": 1, "y": 2}),
            }
        ],
    )


def test_bundle_is_atomic_compact_and_collapses_duplicate_aliases(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    _experiment(inputs / "original", execution="one")
    _experiment(inputs / "alias", execution="two")
    output = tmp_path / "bundle"

    result = build_report_bundle(inputs, output, title="Research <Report>")

    assert result.output_dir == output
    assert result.index_path.is_file()
    assert result.index_build.database_path == result.index_path
    assert result.report_path.is_file()
    manifest = yaml.safe_load(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["tool"] == "pisa-analysis-tools"
    assert manifest["schema_version"] == 3
    assert manifest["report_build_version"] == 11
    assert manifest["report_store_schema"] == 2
    assert manifest["store_schema_version"] == 2
    assert manifest["dataset_count"] == 2
    assert manifest["run_count"] == 2
    assert manifest["aggregate_run_count"] == 1
    summary = json.loads(result.summary_json_path.read_text(encoding="utf-8"))
    assert summary["summary"]["total"] == 1
    assert summary["all_browsable_runs"] == 2
    assert sum(item["aggregate_included"] for item in summary["datasets"]) == 1
    assert any(item["alias_of"] for item in summary["datasets"])
    report = result.report_path.read_text(encoding="utf-8")
    assert "Research &lt;Report&gt;" in report
    assert 'src="http' not in report
    assert "Portable aggregate snapshot" in report
    assert str(tmp_path) not in report
    assert str(tmp_path) not in result.summary_json_path.read_text(encoding="utf-8")
    assert not list(output.parent.glob(f".{output.name}.building-*"))


def test_bundle_embeds_portable_paired_parameter_regions(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    _experiment(inputs / "left", execution="left", outcome="success", av="av-left")
    _experiment(inputs / "right", execution="right", outcome="fail", av="av-right")
    output = tmp_path / "bundle"

    result = build_report_bundle(inputs, output)

    regions = json.loads(
        (output / "summary" / "paired_parameter_regions.json").read_text(
            encoding="utf-8"
        )
    )
    assert regions["schema_version"] == 1
    assert (output / "summary" / "paired_parameter_regions.csv").is_file()
    html = result.report_path.read_text(encoding="utf-8")
    assert "Paired parameter regions" in html
    assert "derived_parameters_used" in html


def test_bundle_overwrite_requires_owned_manifest(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    _experiment(inputs / "experiment", execution="one")
    output = tmp_path / "bundle"
    output.mkdir()
    (output / "user.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(ReportBundleError, match="not owned"):
        build_report_bundle(inputs, output, overwrite=True)

    assert (output / "user.txt").read_text(encoding="utf-8") == "keep"


def test_owned_bundle_can_be_atomically_replaced(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    experiment = inputs / "experiment"
    _experiment(experiment, execution="one")
    output = tmp_path / "bundle"
    build_report_bundle(inputs, output)
    old_fingerprint = yaml.safe_load((output / "manifest.yaml").read_text(encoding="utf-8"))[
        "source_fingerprint"
    ]
    result_path = experiment / "iteration_1" / "monitor" / "result.csv"
    text = result_path.read_text(encoding="utf-8").replace("success", "invalid")
    result_path.write_text(text, encoding="utf-8")

    replaced = build_report_bundle(inputs, output, overwrite=True)

    new_fingerprint = yaml.safe_load(replaced.manifest_path.read_text(encoding="utf-8"))[
        "source_fingerprint"
    ]
    assert new_fingerprint != old_fingerprint
    assert not list(output.parent.glob(f".{output.name}.replaced-*"))


def test_concurrent_owned_bundle_publishers_are_serialized(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    _experiment(inputs / "experiment", execution="one")
    output = tmp_path / "bundle"
    build_report_bundle(inputs, output)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda title: build_report_bundle(inputs, output, title=title, overwrite=True),
                ("Concurrent report A", "Concurrent report B"),
            )
        )

    assert all(result.output_dir == output for result in results)
    manifest = yaml.safe_load((output / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["tool"] == "pisa-analysis-tools"
    report = (output / "report" / "analysis_report.html").read_text(encoding="utf-8")
    assert "Concurrent report A" in report or "Concurrent report B" in report
    assert not list(output.parent.glob(f".{output.name}.replaced-*"))


def test_newer_owned_bundle_is_read_only_and_never_downgraded(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    _experiment(inputs / "experiment", execution="one")
    output = tmp_path / "bundle"
    output.mkdir()
    marker = output / "future-data.bin"
    marker.write_bytes(b"keep")
    (output / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "tool": "pisa-analysis-tools",
                "schema_version": 999,
                "report_build_version": 999,
                "report_store_schema": 999,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ReportBundleError, match="newer schema/build"):
        build_report_bundle(inputs, output, overwrite=True)

    assert marker.read_bytes() == b"keep"


def test_legacy_rebuild_is_a_sibling_and_never_modifies_original(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    _experiment(inputs / "experiment", execution="one")
    legacy = tmp_path / "legacy-report"
    legacy.mkdir()
    marker = legacy / "original.txt"
    marker.write_text("untouched", encoding="utf-8")

    rebuilt = rebuild_legacy_report(legacy, source_roots=inputs)

    assert rebuilt.output_dir.parent == legacy.parent
    assert rebuilt.output_dir.name.startswith("legacy-report--rebuilt-")
    assert rebuilt.output_dir != legacy
    assert marker.read_text(encoding="utf-8") == "untouched"
    manifest = yaml.safe_load(rebuilt.manifest_path.read_text(encoding="utf-8"))
    assert manifest["lineage"] == {
        "operation": "non_destructive_legacy_rebuild",
        "source_report_name": "legacy-report",
    }
