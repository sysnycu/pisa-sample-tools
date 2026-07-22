from __future__ import annotations

import asyncio
import csv
import json
import time
from pathlib import Path

import httpx2 as httpx
import pytest
import yaml

from pisa_sample_tools.reporting import (
    analyze_deep_consistency,
    build_report_bundle,
    deep_consistency_status,
)
from pisa_sample_tools.webapp import create_app


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _manifest(root: Path, execution_id: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "execution_manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "execution_id": execution_id,
                "scenario_name": "cut-in",
                "completed_at": "2026-01-01T00:00:00Z",
                "dt": 0.05,
                "summary": {"finished": 2},
                "execution": {
                    "sampler_name": "lhs",
                    "observation_identity": "entity_name",
                    "observation_order": "scenario",
                },
                "components": {
                    "simulator": {
                        "wrapper": {"name": "esmini-wrapper", "version": "1"},
                        "component": {"name": "esmini"},
                    },
                    "av": {
                        "wrapper": {"name": "simple-wrapper", "version": "1"},
                        "component": {"name": "simple-av"},
                    },
                },
                "resolved_input_sha256": {
                    "scenario": "scenario-hash",
                    "map_xodr": "map-hash",
                    "stop_conditions": "stop-hash",
                    "monitor_config": "monitor-hash",
                    "sampler_config": "sampler-config-hash",
                    "sampler_source": "sampler-source-hash",
                    "simulator_config": "sim-config-hash",
                    "av_config": "av-config-hash",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _run(
    root: Path,
    iteration: int,
    *,
    actor_id: int,
    offset: float,
    outcome: str,
    wall_time_ms: int,
) -> None:
    monitor = root / f"iteration_{iteration}" / "monitor"
    stop = "goal" if outcome == "success" else "collision_guard"
    _write_csv(
        monitor / "result.csv",
        [
            {
                "run.status": "finished",
                "run.test_outcome": outcome,
                "run.stop_condition": stop,
                "run.stop_reason": stop,
                "run.sample_id": str(iteration),
                "run.parameter_hash": f"hash-{iteration}",
                "run.params": json.dumps({"speed": 10 + iteration}),
                "run.total_steps": 2,
                "run.wall_time_ms": wall_time_ms,
            }
        ],
    )
    _write_csv(
        monitor / "agent_states.csv",
        [
            {"step_index": 0, "sim_time_ms": 0, "agent_id": actor_id, "entity_name": "Ego", "is_ego": True, "x": 0, "y": 0, "speed": 10, "yaw": 0},
            {"step_index": 1, "sim_time_ms": 50, "agent_id": actor_id, "entity_name": "Ego", "is_ego": True, "x": 1 + offset, "y": 0, "speed": 10, "yaw": 0},
        ],
    )
    _write_csv(
        monitor / "control_commands.csv",
        [
            {"step_index": 0, "sim_time_ms": 0, "throttle": 0.2, "brake": 0, "steer": 0},
            {"step_index": 1, "sim_time_ms": 50, "throttle": 0.2, "brake": 0, "steer": offset},
        ],
    )


def _replicate_report(tmp_path: Path) -> Path:
    inputs = tmp_path / "inputs"
    left = inputs / "left"
    right = inputs / "right"
    _manifest(left, "left-execution")
    _manifest(right, "right-execution")
    _run(left, 1, actor_id=101, offset=0, outcome="success", wall_time_ms=100)
    _run(right, 1, actor_id=999, offset=0, outcome="success", wall_time_ms=125)
    _run(left, 2, actor_id=101, offset=0, outcome="success", wall_time_ms=110)
    _run(right, 2, actor_id=999, offset=0.02, outcome="fail", wall_time_ms=140)
    return build_report_bundle(inputs, tmp_path / "report").output_dir


def test_bundle_writes_quick_consistency_without_reading_traces(tmp_path: Path) -> None:
    report = _replicate_report(tmp_path)
    quick = json.loads((report / "summary" / "consistency.json").read_text(encoding="utf-8"))

    assert quick["available"] is True
    assert quick["methodology"]["trace_files_read"] is False
    assert quick["group_count"] == 1
    group = quick["groups"][0]
    assert group["datasets"] == ["left", "right"]
    assert group["common_sample_count"] == 2
    outcome = next(item for item in group["discrete"] if item["key"] == "outcome")
    assert outcome["consistent_count"] == 1
    assert outcome["agreement_ratio"] == pytest.approx(0.5)
    assert any(item["key"] == "run.total_steps" for item in group["continuous"])
    assert any(item["key"] == "run.wall_time_ms" for item in group["runtime"])
    assert (report / "summary" / "consistency_groups.csv").is_file()
    assert (report / "summary" / "consistency_outcomes.csv").is_file()


def test_deep_consistency_uses_semantic_actors_thresholds_cache_and_progress(
    tmp_path: Path,
) -> None:
    report = _replicate_report(tmp_path)
    events: list[tuple[str, float, float, str, int, int]] = []

    result = analyze_deep_consistency(
        report,
        progress=lambda *values: events.append(values),
    )

    assert result["state"] == "ready"
    assert result["cached"] is False
    group = result["summary"]["groups"][0]
    assert group["sample_count"] == 2
    assert group["trajectory_comparable_count"] == 2
    assert group["strict_exact_count"] == 1
    assert group["position_tolerance_counts"] == {"0.001": 1, "0.01": 1, "0.1": 2}
    assert group["max_position_error_m"]["max"] == pytest.approx(0.02)
    assert {event[4] for event in events} == {1, 2, 3, 4, 5}
    assert all(event[5] == 5 for event in events)
    assert all(Path(path).is_file() for path in result["artifacts"])

    cached = analyze_deep_consistency(report)
    assert cached["cached"] is True
    assert deep_consistency_status(report)["state"] == "ready"


def test_consistency_api_runs_job_with_explicit_progress_and_persisted_artifacts(
    tmp_path: Path,
) -> None:
    _replicate_report(tmp_path)
    app = create_app(
        report_roots=[tmp_path],
        results_roots=[tmp_path],
        state_path=tmp_path / "jobs.sqlite",
    )

    def request(method: str, path: str, **kwargs):
        async def send():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(send())

    catalog = request("GET", "/api/v1/reports", params={"search": "report"}).json()
    identifier = next(item["id"] for item in catalog["items"] if item["name"] == "report")
    quick = request("GET", f"/api/v1/reports/{identifier}/consistency")
    assert quick.status_code == 200
    assert quick.json()["quick"]["available"] is True
    assert quick.json()["deep"]["state"] == "not_generated"

    queued = request(
        "POST",
        f"/api/v1/reports/{identifier}/consistency/analyze",
        json={"profile": "full_controls", "outlier_limit": 25},
    )
    assert queued.status_code == 202
    job_id = queued.json()["id"]
    deadline = time.monotonic() + 5
    while True:
        job = request("GET", f"/api/v1/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            break
        assert time.monotonic() < deadline
        time.sleep(0.01)

    assert job["status"] == "succeeded"
    assert job["message"].startswith("Phase 5 / 5")
    assert job["progress"] == {"current": 4.0, "total": 4.0, "unit": "artifacts"}
    ready = request(
        "GET",
        f"/api/v1/reports/{identifier}/consistency",
        params={
            "profile": "full_controls",
            "position_tolerances_m": [0.001, 0.01, 0.1],
            "outlier_limit": 25,
        },
    ).json()
    assert ready["deep"]["state"] == "ready"
    assert all(item["download_url"].startswith("/api/v1/reports/") for item in ready["deep"]["artifacts"])
