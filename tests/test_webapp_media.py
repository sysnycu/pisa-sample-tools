from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import sqlite3
import time
from pathlib import Path

import httpx2 as httpx
import pytest
import yaml

from pisa_sample_tools.reporting import REPORT_INDEX_SCHEMA_VERSION, build_report_index
from pisa_sample_tools.webapp import create_app, media
from pisa_sample_tools.webapp.media import (
    DerivedMediaError,
    MediaCapabilityError,
    generate_schematic_media,
    select_realtime_frames,
    select_schematic_frames,
)


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _normalized_report(tmp_path: Path) -> tuple[Path, Path, str]:
    inputs = tmp_path / "inputs"
    experiment = inputs / "experiment"
    experiment.mkdir(parents=True)
    (experiment / "execution_manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "execution_id": "media-test",
                "scenario_name": "cut-in",
                "completed_at": "2026-07-14T00:00:00Z",
                "summary": {"finished": 1, "failed": 0, "skipped": 0, "aborted": 0},
                "execution": {"sampler_name": "grid"},
                "components": {
                    "simulator": {"component": {"name": "esmini"}},
                    "av": {"component": {"name": "test-av"}},
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monitor = experiment / "iteration_1" / "monitor"
    _write_csv(
        monitor / "result.csv",
        [
            {
                "run.status": "finished",
                "run.test_outcome": "fail",
                "run.stop_condition": "collision_guard",
                "run.stop_reason": "contact",
                "run.attempt": 1,
                "run.params": json.dumps({"speed": 8.0}),
                "ego_collision.collision": True,
            }
        ],
    )
    state_rows: list[dict[str, object]] = []
    for step in range(8):
        state_rows.extend(
            [
                {
                    "step_index": step,
                    "sim_time_ms": step * 100,
                    "agent_id": "ego",
                    "entity_name": "Ego",
                    "is_ego": "true",
                    "x": step * 2,
                    "y": 0,
                    "yaw": 0,
                    "speed": step if step <= 4 else 8 - step,
                },
                {
                    "step_index": step,
                    "sim_time_ms": step * 100,
                    "agent_id": "npc-1",
                    "entity_name": "Cut-in vehicle",
                    "is_ego": "false",
                    "x": 7 + step,
                    "y": 6 - step,
                    "yaw": -0.2,
                    "speed": 5,
                },
            ]
        )
    _write_csv(monitor / "agent_states.csv", state_rows)
    _write_csv(
        monitor / "agent_geometry.csv",
        [
            {
                "step_index": 0,
                "sim_time_ms": 0,
                "agent_id": "ego",
                "entity_name": "Ego",
                "is_ego": "true",
                "length_m": 4.5,
                "width_m": 1.8,
                "reference_point": "esmini_object_reference_point",
                "source": "test",
            },
            {
                "step_index": 0,
                "sim_time_ms": 0,
                "agent_id": "npc-1",
                "entity_name": "Cut-in vehicle",
                "is_ego": "false",
                "length_m": 4.2,
                "width_m": 1.8,
                "reference_point": "esmini_object_reference_point",
                "source": "test",
            },
        ],
    )
    _write_csv(
        monitor / "collision_events.csv",
        [{"step_index": 3, "sim_time_ms": 300, "event": "collision"}],
    )
    _write_csv(
        monitor / "scenario_events.csv",
        [{"step_index": 5, "sim_time_ms": 500, "event": "cut_in"}],
    )
    _write_csv(monitor / "frame_metrics.csv", [{"step_index": 0, "sim_time_ms": 0}])
    _write_csv(monitor / "control_commands.csv", [{"step_index": 0, "sim_time_ms": 0}])

    report_root = tmp_path / "normalized-report"
    (report_root / "report").mkdir(parents=True)
    (report_root / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "tool": "pisa-analysis-tools",
                "generated_at": "2026-07-14T00:00:00Z",
                "report_build_version": 8,
            }
        ),
        encoding="utf-8",
    )
    build_report_index(inputs, report_root / "report" / "index.sqlite")
    return report_root, monitor, "experiment:1"


def _tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(item.read_bytes())
    return digest.hexdigest()


def test_gif_is_content_addressed_labeled_and_does_not_mutate_source(tmp_path: Path) -> None:
    report_root, monitor, run_id = _normalized_report(tmp_path)
    source_hash = _tree_hash(monitor)

    result = generate_schematic_media(
        report_root,
        run_id,
        format="gif",
        fps=5,
        max_frames=6,
        size=(480, 320),
    )

    assert result.cached is False
    assert result.media_path.parent == report_root / "media" / "derived"
    assert result.media_path.read_bytes().startswith(b"GIF8")
    assert result.rendered_frame_count == 6
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert metadata["disclaimer"] == "Reconstructed schematic — not camera footage"
    assert metadata["frames"]["selected_indices"][0] == 0
    assert metadata["frames"]["selected_indices"][-1] == 7
    assert metadata["frames"]["events_preserved"] is True
    assert metadata["frames"]["extrema_preserved"] is True
    assert metadata["data_sha256"] == result.data_sha256
    assert str(monitor) not in result.metadata_path.read_text(encoding="utf-8")
    assert _tree_hash(monitor) == source_hash

    cached = generate_schematic_media(
        report_root,
        run_id,
        format="gif",
        fps=5,
        max_frames=6,
        size=(480, 320),
    )
    assert cached.cached is True
    assert cached.media_path == result.media_path
    assert cached.data_sha256 == result.data_sha256


def test_frame_cap_prioritizes_endpoints_events_and_extrema() -> None:
    selected = select_schematic_frames(
        100,
        8,
        event_indices={30, 60},
        extrema_indices={10, 90},
    )
    assert len(selected) == 8
    assert {0, 10, 30, 60, 90, 99}.issubset(selected)

    overfull = select_schematic_frames(
        100,
        4,
        event_indices={10, 20, 30, 40},
        extrema_indices={50, 60},
    )
    assert len(overfull) == 4
    assert {0, 99}.issubset(overfull)
    assert set(overfull[1:-1]).issubset({10, 20, 30, 40})


def test_realtime_frame_selection_preserves_recorded_clock_and_speed() -> None:
    timeline = (0.0, 100.0, 200.0, 300.0, 400.0)

    assert select_realtime_frames(
        timeline,
        fps=10,
        playback_rate=2.0,
        max_frames=100,
        timeline_uses_time=True,
    ) == (0, 2, 4)

    slowed = select_realtime_frames(
        timeline,
        fps=10,
        playback_rate=0.5,
        max_frames=100,
        timeline_uses_time=True,
    )
    assert len(slowed) == 9
    assert slowed[0] == 0
    assert slowed[-1] == 4
    assert slowed == tuple(sorted(slowed))


def test_validation_and_missing_encoder_have_clear_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_root, _monitor, run_id = _normalized_report(tmp_path)
    with pytest.raises(DerivedMediaError, match="unsupported media format"):
        generate_schematic_media(report_root, run_id, format="avi")
    with pytest.raises(DerivedMediaError, match="frame-pixels"):
        generate_schematic_media(
            report_root,
            run_id,
            format="gif",
            max_frames=2_000,
            size=(3_840, 2_160),
        )
    with pytest.raises(DerivedMediaError, match="not present"):
        generate_schematic_media(report_root, "../../not-a-run", format="gif")

    monkeypatch.setattr(media.shutil, "which", lambda _name: None)
    with pytest.raises(MediaCapabilityError, match="ffmpeg is not installed"):
        generate_schematic_media(
            report_root,
            run_id,
            format="mp4",
            max_frames=4,
            size=(480, 320),
        )


def test_symlinked_trace_is_rejected_without_following_it(tmp_path: Path) -> None:
    report_root, monitor, run_id = _normalized_report(tmp_path)
    states = monitor / "agent_states.csv"
    target = monitor / "agent_states-original.csv"
    states.rename(target)
    states.symlink_to(target.name)

    with pytest.raises(DerivedMediaError, match="must not be a symbolic link"):
        generate_schematic_media(report_root, run_id, format="gif")


def test_newer_report_schema_is_never_mutated(tmp_path: Path) -> None:
    report_root, _monitor, run_id = _normalized_report(tmp_path)
    with sqlite3.connect(report_root / "report" / "index.sqlite") as connection:
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            (str(REPORT_INDEX_SCHEMA_VERSION + 1),),
        )

    with pytest.raises(DerivedMediaError, match="newer schema"):
        generate_schematic_media(report_root, run_id, format="gif")
    assert not (report_root / "media").exists()


def test_media_api_queues_generation_and_indexes_artifact(tmp_path: Path) -> None:
    report_root, _monitor, run_id = _normalized_report(tmp_path)
    app = create_app(
        report_roots=[tmp_path],
        results_roots=[tmp_path],
        state_path=tmp_path / "jobs.sqlite",
    )

    async def request(method: str, path: str, **kwargs: object) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.request(method, path, **kwargs)

    catalog = asyncio.run(request("GET", "/api/v1/reports")).json()
    identifier = next(
        item["id"] for item in catalog["items"] if Path(item["path"]) == report_root
    )
    queued = asyncio.run(
        request(
            "POST",
            f"/api/v1/reports/{identifier}/media",
            json={
                "run_id": run_id,
                "format": "gif",
                "fps": 5,
                "max_frames": 4,
                "width": 480,
                "height": 320,
            },
        )
    )
    assert queued.status_code == 202
    job_id = queued.json()["id"]
    deadline = time.monotonic() + 10
    while True:
        job = asyncio.run(request("GET", f"/api/v1/jobs/{job_id}")).json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            break
        assert time.monotonic() < deadline
        time.sleep(0.02)

    assert job["status"] == "succeeded", job.get("error")
    assert job["result"]["label"] == "Reconstructed schematic — not camera footage"
    assert job["result"]["path"].startswith("media/derived/")
    response = asyncio.run(request("GET", f"/api/v1/reports/{identifier}/media"))
    assert response.status_code == 200
    body = response.json()
    assert body["capabilities"]["gif"]["available"] is True
    assert any(item["path"] == job["result"]["path"] for item in body["items"])


def test_case_api_rejects_unprovenanced_index_trace_path(tmp_path: Path) -> None:
    report_root, _monitor, run_id = _normalized_report(tmp_path)
    secret = tmp_path / "private-trace.csv"
    secret.write_text("time,secret\n0,DO_NOT_EXPOSE\n", encoding="utf-8")
    index_path = report_root / "report" / "index.sqlite"
    with sqlite3.connect(index_path) as connection:
        row = connection.execute(
            "SELECT trace_paths_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        paths = json.loads(row[0])
        paths["agent_states"] = str(secret)
        connection.execute(
            "UPDATE runs SET trace_paths_json=? WHERE run_id=?",
            (json.dumps(paths), run_id),
        )

    app = create_app(
        report_roots=[tmp_path],
        results_roots=[tmp_path],
        state_path=tmp_path / "case-jobs.sqlite",
    )

    async def request(path: str) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get(path)

    catalog = asyncio.run(request("/api/v1/reports")).json()
    identifier = next(
        item["id"] for item in catalog["items"] if Path(item["path"]) == report_root
    )
    response = asyncio.run(
        request(f"/api/v1/reports/{identifier}/cases/{run_id}")
    )
    assert response.status_code == 200
    assert "DO_NOT_EXPOSE" not in response.text
    assert "ego" not in response.json()["traces"]
