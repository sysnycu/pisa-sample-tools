from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import yaml

from pisa_sample_tools.reporting import ReportIndex, build_report_index, discover_experiments


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _write_experiment(
    root: Path,
    *,
    completed: bool,
    geometry: tuple[str, ...],
    provenance_file: Path | None = None,
    expected_hash: str | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "execution_id": f"execution-{root.name}",
        "scenario_name": "cut-in",
        "completed_at": "2026-01-01T00:00:00Z" if completed else None,
        "summary": {"finished": len(geometry) + 1},
    }
    if provenance_file is not None:
        manifest["resolved_inputs"] = {"sampler_source": str(provenance_file)}
        manifest["resolved_input_sha256"] = {"sampler_source": expected_hash}
    (root / "execution_manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    for scenario_id, reference in enumerate(geometry, start=1):
        monitor = root / f"iteration_{scenario_id}" / "monitor"
        row = {
            "run.status": "finished",
            "run.test_outcome": "success",
            "run.sample_id": str(scenario_id),
            "run.attempt": 1,
            "run.parameter_hash": f"hash-{scenario_id}",
            "run.params": json.dumps({"x": scenario_id}),
        }
        _write_csv(monitor / "result.csv", [row])
        _write_csv(
            monitor / "agent_geometry.csv",
            [{"agent_id": 0, "reference_point": reference, "source": "observation"}],
        )
    (root / f"iteration_{len(geometry) + 1}" / "monitor").mkdir(parents=True)


def test_health_detects_mixed_partial_missing_and_hash_drift(tmp_path: Path) -> None:
    provenance_file = tmp_path / "current-range.yaml"
    provenance_file.write_text("changed: true\n", encoding="utf-8")
    experiment = tmp_path / "inputs" / "mixed"
    _write_experiment(
        experiment,
        completed=False,
        geometry=("esmini_object_reference_point", "carla_actor_origin"),
        provenance_file=provenance_file,
        expected_hash=hashlib.sha256(b"original: true\n").hexdigest(),
    )

    database = tmp_path / "report.sqlite"
    build_report_index(tmp_path / "inputs", database)

    with ReportIndex(database) as index:
        findings = index.findings(dataset_id="mixed")
        codes = {finding.code for finding in findings}
        assert {
            "mixed_provenance",
            "partial_dataset",
            "missing_results",
            "missing_trace_files",
            "provenance_hash_drift",
        } <= codes
        mixed = next(finding for finding in findings if finding.code == "mixed_provenance")
        assert mixed.details["signatures"] == {"carla": 1, "esmini": 1}
        fingerprint = next(
            item
            for item in index.source_fingerprints(dataset_id="mixed")
            if item.kind == "resolved_input:sampler_source"
        )
        assert fingerprint.status == "drifted"


def test_duplicate_aliases_use_canonical_results_not_manifest_identity(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    for name in ("original", "alias"):
        _write_experiment(
            inputs / name,
            completed=True,
            geometry=("esmini_object_reference_point",),
        )
    database = tmp_path / "report.sqlite"
    build_report_index(inputs, database)

    with ReportIndex(database) as index:
        duplicate_findings = [
            finding for finding in index.findings() if finding.code == "duplicate_alias"
        ]
        assert {finding.dataset_id for finding in duplicate_findings} == {"alias", "original"}
        assert duplicate_findings[0].details["datasets"] == ["alias", "original"]


def test_recursive_discovery_keeps_manifestless_result_roots(tmp_path: Path) -> None:
    monitor = tmp_path / "inputs" / "legacy" / "iteration_1" / "monitor"
    _write_csv(
        monitor / "result.csv",
        [{"run.status": "finished", "run.test_outcome": "success", "run.params": "{}"}],
    )

    sources = discover_experiments(tmp_path / "inputs")

    assert len(sources) == 1
    assert sources[0].dataset_id == "legacy"
    assert sources[0].manifest_path is None
