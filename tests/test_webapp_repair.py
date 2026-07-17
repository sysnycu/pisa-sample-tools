from __future__ import annotations

import asyncio
import csv
import time
from pathlib import Path

import httpx2 as httpx
import yaml

from pisa_sample_tools.webapp import create_app


def _agent_states(root: Path) -> Path:
    path = root / "iteration_1" / "monitor" / "agent_states.csv"
    path.parent.mkdir(parents=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["step_index", "sim_time_ms", "agent_id", "x", "y", "speed"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "step_index": 0,
                "sim_time_ms": 50,
                "agent_id": 0,
                "x": 1,
                "y": 2,
                "speed": 3,
            }
        )
        writer.writerow(
            {
                "step_index": 1,
                "sim_time_ms": 100,
                "agent_id": 0,
                "x": 2,
                "y": 3,
                "speed": 4,
            }
        )
    return path


class _Client:
    def __init__(self, app) -> None:
        self.app = app

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def request(self, method: str, path: str, **kwargs):
        async def send():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://test"
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(send())

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self.request("POST", path, **kwargs)


def _wait(client: _Client, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while True:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_repair_overlay_source_confirmation_backup_and_restore(tmp_path: Path) -> None:
    source = tmp_path / "source"
    original = _agent_states(source)
    original_bytes = original.read_bytes()
    init_state = tmp_path / "initial.yaml"
    init_state.write_text(
        yaml.safe_dump({"agents": {0: {"x": 0, "y": 0, "speed": 0}}}), encoding="utf-8"
    )
    overlay = tmp_path / "overlay"
    app = create_app(
        report_roots=[tmp_path],
        results_roots=[tmp_path],
        state_path=tmp_path / "jobs.sqlite",
    )
    with _Client(app) as client:
        scan = client.post(
            "/api/v1/tools/repair/scan",
            json={
                "source_path": str(source),
                "init_state_path": str(init_state),
                "mode": "overlay",
                "output_path": str(overlay),
            },
        )
        assert scan.status_code == 200
        plan = scan.json()
        assert plan["destructive"] is False
        assert plan["changes"][0]["result_rows"] == 3
        applied = client.post(
            "/api/v1/tools/repair/apply", json={"plan": plan, "dry_run": False}
        ).json()
        completed = _wait(client, applied["id"])
        assert completed["status"] == "succeeded"
        patched = overlay / "iteration_1" / "monitor" / "agent_states.csv"
        assert patched.is_file()
        assert original.read_bytes() == original_bytes
        assert len(list(csv.DictReader(patched.open(encoding="utf-8")))) == 3

        source_plan = client.post(
            "/api/v1/tools/repair/scan",
            json={
                "source_path": str(source),
                "init_state_path": str(init_state),
                "mode": "source",
            },
        ).json()
        rejected = client.post(
            "/api/v1/tools/repair/apply",
            json={"plan": source_plan, "confirm_path": "wrong"},
        ).json()
        assert _wait(client, rejected["id"])["status"] == "failed"
        assert not Path(str(original) + ".bak").exists()

        accepted = client.post(
            "/api/v1/tools/repair/apply",
            json={"plan": source_plan, "confirm_path": str(source)},
        ).json()
        assert _wait(client, accepted["id"])["status"] == "succeeded"
        assert Path(str(original) + ".bak").read_bytes() == original_bytes

        restored = client.post(
            "/api/v1/tools/repair/restore",
            json={"source_path": str(source), "confirm_path": str(source)},
        ).json()
        assert _wait(client, restored["id"])["status"] == "succeeded"
        assert original.read_bytes() == original_bytes
