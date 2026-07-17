from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest
import yaml

import pisa_sample_tools.reporting.index as reporting_index
from pisa_sample_tools.reporting import ReportIndex, ReportIndexError, RunFilter, build_report_index


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _write_run(
    root: Path,
    scenario_id: int,
    rows: list[dict[str, object]],
    *,
    provenance: str = "esmini_object_reference_point",
    collision: bool = False,
) -> None:
    monitor = root / f"iteration_{scenario_id}" / "monitor"
    _write_csv(monitor / "result.csv", rows)
    _write_csv(
        monitor / "agent_geometry.csv",
        [
            {
                "step_index": 0,
                "agent_id": 0,
                "reference_point": provenance,
                "source": "observation",
            }
        ],
    )
    for name in (
        "frame_metrics.csv",
        "agent_states.csv",
        "scenario_events.csv",
        "control_commands.csv",
    ):
        _write_csv(monitor / name, [{"step_index": 0, "sim_time_ms": 0}])
    _write_csv(
        monitor / "collision_events.csv",
        ([{"step_index": 1, "sim_time_ms": 50}] if collision else [{"step_index": ""}]),
    )


def _row(
    scenario_id: int,
    *,
    attempt: int = 1,
    outcome: str = "success",
    x: float | None = None,
) -> dict[str, object]:
    x = float(scenario_id) if x is None else x
    return {
        "run.status": "finished",
        "run.test_outcome": outcome,
        "run.stop_condition": "collision_guard" if outcome == "fail" else "goal",
        "run.stop_reason": "done",
        "run.sample_id": f"sample-{scenario_id}",
        "run.attempt": attempt,
        "run.parameter_hash": f"hash-{scenario_id}",
        "run.params": json.dumps({"x": x, "label": f"case-{scenario_id}"}),
        "run.wall_time_ms": 100 + scenario_id,
        "ego_collision.collision": outcome == "fail",
    }


def _manifest(root: Path, *, run_count: int, completed: bool = True) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "execution_manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "execution_id": f"execution-{root.name}",
                "scenario_name": "cut-in",
                "completed_at": "2026-01-01T00:00:00Z" if completed else None,
                "summary": {"finished": run_count, "failed": 0, "skipped": 0, "aborted": 0},
                "execution": {"sampler_name": "lhs"},
                "components": {
                    "simulator": {"component": {"name": "esmini"}},
                    "av": {"component": {"name": "simple-av"}},
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_index_selects_highest_attempt_and_queries_without_loading_traces(tmp_path: Path) -> None:
    source = tmp_path / "inputs"
    experiment = source / "nested" / "experiment"
    _manifest(experiment, run_count=3)
    _write_run(
        experiment,
        1,
        [
            _row(1, attempt=1, outcome="success"),
            _row(1, attempt=3, outcome="fail"),
            _row(1, attempt=2, outcome="invalid"),
        ],
        collision=True,
    )
    _write_run(experiment, 2, [_row(2, outcome="invalid")])
    _write_run(experiment, 3, [_row(3, outcome="success")])

    database = tmp_path / "report.sqlite"
    built = build_report_index(source, database)

    assert built.rebuilt is True
    assert built.dataset_count == 1
    assert built.run_count == 3
    assert built.attempt_count == 5
    assert [timing.stage for timing in built.timings] == [
        "discover",
        "fingerprint",
        "cache_check",
        "schema",
        "index",
        "data_health",
        "verify_source",
        "finalize",
    ]
    with ReportIndex(database) as index:
        dataset = index.datasets()[0]
        assert dataset.dataset_id == "nested/experiment"
        assert dataset.run_count == 3
        run = index.run("nested/experiment:1")
        assert run is not None
        assert run.attempt == 3
        assert run.outcome == "fail"
        assert run.params == {"label": "case-1", "x": 1.0}
        assert run.trace_paths["agent_states"].name == "agent_states.csv"
        assert run.has_collision is True
        assert [attempt.attempt for attempt in index.attempts(run.run_id)] == [1, 2, 3]
        assert index.attempts(run.run_id)[-1].outcome == "fail"
        assert index.outcome_summary().as_dict() == {
            "total": 3,
            "success": 1,
            "fail": 1,
            "invalid": 1,
            "unknown": 0,
            "collision": 1,
        }


def test_index_derives_control_extrema_for_sampling(tmp_path: Path) -> None:
    experiment = tmp_path / "inputs" / "experiment"
    _manifest(experiment, run_count=1)
    _write_run(experiment, 1, [_row(1)])
    _write_csv(
        experiment / "iteration_1" / "monitor" / "control_commands.csv",
        [
            {"step_index": 0, "throttle": 0.2, "brake": 0.0, "steering": -0.3},
            {"step_index": 1, "throttle": 0.8, "brake": 0.6, "steering": 0.5},
        ],
    )

    database = tmp_path / "report.sqlite"
    build_report_index(tmp_path / "inputs", database)

    with ReportIndex(database) as index:
        run = index.run("experiment:1")
        assert run is not None
        assert run.metrics["control.max_throttle"] == pytest.approx(0.8)
        assert run.metrics["control.max_brake"] == pytest.approx(0.6)
        assert run.metrics["control.max_abs_steer"] == pytest.approx(0.5)


def test_keyset_pagination_and_filters_are_stable(tmp_path: Path) -> None:
    experiment = tmp_path / "inputs" / "experiment"
    _manifest(experiment, run_count=5)
    outcomes = ["success", "fail", "invalid", "unknown", "success"]
    scenario_ids = [1, 2, 3, 4, 10]
    for scenario_id, outcome in zip(scenario_ids, outcomes, strict=True):
        _write_run(experiment, scenario_id, [_row(scenario_id, outcome=outcome)])
    database = tmp_path / "report.sqlite"
    build_report_index(tmp_path / "inputs", database)

    with ReportIndex(database) as index:
        first = index.page_runs(limit=2)
        second = index.page_runs(limit=2, cursor=first.next_cursor)
        third = index.page_runs(limit=2, cursor=second.next_cursor)
        assert [run.scenario_id for run in (*first.items, *second.items, *third.items)] == [
            "1",
            "2",
            "3",
            "4",
            "10",
        ]
        assert first.total == second.total == third.total == 5
        assert third.next_cursor is None
        filtered = index.page_runs(
            filters=RunFilter(outcome_classes=("success",), parameter_values={"x": 10.0})
        )
        assert [run.scenario_id for run in filtered.items] == ["10"]
        searched = index.page_runs(filters=RunFilter(search="3"))
        assert [run.scenario_id for run in searched.items] == ["3"]
        assert index.page_runs(filters=RunFilter(search="sample-3")).total == 0
        by_duration = index.page_runs(sort_by="duration", sort_direction="desc")
        assert [run.scenario_id for run in by_duration.items] == ["10", "4", "3", "2", "1"]


def test_unchanged_index_is_a_fingerprint_cache_hit(tmp_path: Path) -> None:
    experiment = tmp_path / "inputs" / "experiment"
    _manifest(experiment, run_count=1)
    _write_run(experiment, 1, [_row(1)])
    database = tmp_path / "report.sqlite"

    first = build_report_index(tmp_path / "inputs", database)
    database_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    second = build_report_index(tmp_path / "inputs", database)

    assert first.rebuilt is True
    assert second.rebuilt is False
    assert second.source_fingerprint == first.source_fingerprint
    assert hashlib.sha256(database.read_bytes()).hexdigest() == database_hash
    assert [timing.stage for timing in second.timings] == [
        "discover",
        "fingerprint",
        "cache_check",
    ]


def test_index_aborts_without_replacing_database_when_sources_change_mid_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = tmp_path / "inputs" / "experiment"
    _manifest(experiment, run_count=1)
    _write_run(experiment, 1, [_row(1)])
    database = tmp_path / "report.sqlite"
    build_report_index(tmp_path / "inputs", database)
    original_database = database.read_bytes()
    result_path = experiment / "iteration_1" / "monitor" / "result.csv"
    original_index_sources = reporting_index._index_sources

    def index_then_mutate(*args: object, **kwargs: object) -> object:
        findings = original_index_sources(*args, **kwargs)  # type: ignore[arg-type]
        result_path.write_text(
            result_path.read_text(encoding="utf-8") + "\n", encoding="utf-8"
        )
        return findings

    monkeypatch.setattr(reporting_index, "_index_sources", index_then_mutate)

    with pytest.raises(ReportIndexError, match="sources changed while the index was being built"):
        build_report_index(tmp_path / "inputs", database, force=True)

    assert database.read_bytes() == original_database
    assert not list(tmp_path.glob(".report.sqlite.*.building"))


def test_comparison_pair_count_excludes_ambiguous_parameter_hashes(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    left = inputs / "left"
    right = inputs / "right"
    _manifest(left, run_count=3)
    _manifest(right, run_count=2)

    left_hashes = ("ambiguous", "ambiguous", "shared-unique")
    right_hashes = ("ambiguous", "shared-unique")
    for scenario_id, parameter_hash in enumerate(left_hashes, start=1):
        row = _row(scenario_id)
        row["run.parameter_hash"] = parameter_hash
        _write_run(left, scenario_id, [row])
    for scenario_id, parameter_hash in enumerate(right_hashes, start=1):
        row = _row(scenario_id)
        row["run.parameter_hash"] = parameter_hash
        _write_run(right, scenario_id, [row])

    database = tmp_path / "report.sqlite"
    build_report_index(inputs, database)

    with ReportIndex(database) as index:
        relation = next(
            item
            for item in index.dataset_relations()
            if {item.left_dataset_id, item.right_dataset_id} == {"left", "right"}
        )
    assert relation.details["matched_count"] == 1
    assert relation.details["pairing_key"] == "parameter_hash unique within each dataset"
