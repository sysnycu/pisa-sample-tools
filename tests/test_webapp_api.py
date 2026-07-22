from __future__ import annotations

import asyncio
import csv
import json
import shutil
import threading
import time
from pathlib import Path

import httpx2 as httpx
import pytest
import yaml

from pisa_sample_tools.reporting import build_report_bundle
from pisa_sample_tools.webapp import create_app
from pisa_sample_tools.webapp.jobs import TERMINAL_STATES, JobManager
from pisa_sample_tools.webapp.models import RunnerResumeRequest
from pisa_sample_tools.webapp.reports import ensure_report_index
from pisa_sample_tools.webapp.server import run_server


def _report(root: Path) -> Path:
    bundle = root / "group" / "nested-report"
    (bundle / "report" / "cases").mkdir(parents=True)
    (bundle / "figures" / "outcomes").mkdir(parents=True)
    (bundle / "media").mkdir()
    (bundle / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "tool": "pisa-analysis-tools",
                "generated_at": "2026-01-01T00:00:00Z",
                "run_count": 2,
                "report_build_version": 7,
            }
        ),
        encoding="utf-8",
    )
    runs = [
        {
            "run_id": "demo:iteration_1",
            "experiment_id": "demo",
            "scenario_id": "iteration_1",
            "sample_id": "1",
            "normalized_outcome": "success",
            "params": {"speed": 10},
            "metrics": {"min_ttc": 2.5},
        },
        {
            "run_id": "demo:iteration_2",
            "experiment_id": "demo",
            "scenario_id": "iteration_2",
            "sample_id": "2",
            "normalized_outcome": "failure",
            "params": {"speed": 20},
            "metrics": {"min_ttc": 0.4},
        },
    ]
    (bundle / "report" / "runs.json").write_text(json.dumps(runs), encoding="utf-8")
    (bundle / "report" / "analysis_data.json").write_text(
        json.dumps(
            {
                "report_mode": "compare",
                "summary": {"run_count": 2, "parameter_count": 1},
                "experiments": [{"id": "demo"}],
                "runs": runs,
                "comparison": {
                    "paired_summary": [{"metric": "min_ttc"}],
                    "concrete_scenarios": [{"group_id": "g1"}],
                },
            }
        ),
        encoding="utf-8",
    )
    (bundle / "report" / "analysis_report.html").write_text(
        "<!doctype html><title>report</title>", encoding="utf-8"
    )
    (bundle / "report" / "case_data.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cases": [
                    {
                        "case_type": "critical",
                        "run": runs[1],
                        "series": {"ttc": [0.4]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (bundle / "figures" / "outcomes" / "counts.svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8"
    )
    (bundle / "media" / "replay.gif").write_bytes(b"GIF89a")
    return bundle


def _record(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "execution_manifest.yaml").write_text(
        yaml.safe_dump({
            "execution_id": "preview-source", "scenario_name": "cut-in",
            "summary": {"finished": 1}, "execution": {"sampler_name": "lhs"},
            "components": {"simulator": {"component": {"name": "esmini"}}, "av": {"component": {"name": "simple-av"}}},
        }),
        encoding="utf-8",
    )
    monitor = root / "iteration_1" / "monitor"
    monitor.mkdir(parents=True)
    with (monitor / "result.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["run.status", "run.test_outcome", "run.sample_id", "run.params"])
        writer.writeheader()
        writer.writerow({"run.status": "finished", "run.test_outcome": "success", "run.sample_id": "1", "run.params": json.dumps({"x": 1})})
    return root


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

    def delete(self, path: str, **kwargs):
        return self.request("DELETE", path, **kwargs)


def _client(tmp_path: Path, **kwargs) -> _Client:
    return _Client(
        create_app(
            report_roots=[tmp_path],
            results_roots=[tmp_path],
            state_path=tmp_path / "jobs.sqlite",
            **kwargs,
        )
    )


def test_health_capabilities_preview_and_standard_error(tmp_path: Path) -> None:
    source = tmp_path / "params.yaml"
    source.write_text(
        yaml.safe_dump(
            {"parameters": [{"name": "speed", "type": "int", "values": [10, 20]}]}
        ),
        encoding="utf-8",
    )
    with _client(tmp_path) as client:
        assert client.get("/api/v1/health").json()["status"] == "ok"
        capabilities = client.get("/api/v1/capabilities").json()
        assert capabilities["samples"]["preview"] is True
        assert capabilities["tools"]["repair"] is True

        response = client.post(
            "/api/v1/samples/preview",
            json={"source_file": str(source), "max_samples": 1},
        )
        assert response.status_code == 200
        assert response.json()["samples"][0]["params"] == {"speed": 10}

        inline = client.post(
            "/api/v1/samples/preview",
            json={
                "method": "lhs",
                "count": 8,
                "seed": 7,
                "parameters": [{"name": "speed", "min": 10, "max": 30}],
            },
        )
        assert inline.status_code == 200
        assert inline.json()["parameter_names"] == ["speed"]
        assert len(inline.json()["samples"]) == 8

        blocked = client.post(
            "/api/v1/samples/preview", json={"source_file": "/etc/passwd"}
        )
        assert blocked.status_code == 403
        assert blocked.json()["code"] == "path_not_allowed"
        assert blocked.json()["field"] == "source_file"
        assert blocked.headers["X-Request-ID"] == blocked.json()["request_id"]


def test_legacy_report_keeps_workspace_and_explains_consistency_unavailability(
    tmp_path: Path,
) -> None:
    _report(tmp_path)
    with _client(tmp_path) as client:
        descriptor = client.get("/api/v1/reports").json()["items"][0]
        response = client.get(f"/api/v1/reports/{descriptor['id']}/consistency")

    assert response.status_code == 200
    assert response.json()["quick"] == {
        "schema_version": 1,
        "available": False,
        "reason": "normalized_report_index_required",
        "dataset_count": 0,
        "canonical_dataset_count": 0,
        "group_count": 0,
        "groups": [],
        "excluded_duplicate_aliases": [],
    }
    assert response.json()["deep"]["state"] == "not_generated"


def test_recursive_report_library_index_pagination_and_artifacts(tmp_path: Path) -> None:
    bundle = _report(tmp_path)
    (bundle / "media" / "recorded.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    ensure_report_index(bundle)
    with _client(tmp_path) as client:
        catalog = client.get("/api/v1/reports").json()
        assert len(catalog["items"]) == 1
        identifier = catalog["items"][0]["id"]
        assert catalog["items"][0]["has_index"] is True

        overview = client.get(f"/api/v1/reports/{identifier}/overview").json()
        assert overview["run_count"] == 2
        assert "runs" not in overview["data"]

        first = client.get(
            f"/api/v1/reports/{identifier}/runs", params={"limit": 1}
        ).json()
        assert first["source"] == "sqlite"
        assert first["items"][0]["id"] == "demo:iteration_1"
        assert first["next_cursor"] == "1"
        second = client.get(
            f"/api/v1/reports/{identifier}/runs",
            params={"limit": 1, "cursor": first["next_cursor"], "outcome": "failure"},
        ).json()
        assert second["total"] == 1

        charts = client.get(
            f"/api/v1/reports/{identifier}/charts", params={"section": "outcomes"}
        ).json()
        assert charts["items"][0]["format"] == "svg"
        media = client.get(f"/api/v1/reports/{identifier}/media").json()
        assert media["derived_media_label"] == "reconstructed schematic"
        assert any(item["format"] == "gif" for item in media["items"])
        jpeg = next(item for item in media["items"] if item["format"] == "jpg")
        assert client.app.state.report_library.artifact(
            identifier, jpeg["path"]
        ).read_bytes() == b"\xff\xd8\xff\xd9"

        case = client.get(
            f"/api/v1/reports/{identifier}/cases/demo:iteration_2"
        ).json()
        assert case["source"] == "representative_case"
        comparisons = client.get(
            f"/api/v1/reports/{identifier}/comparisons"
        ).json()
        assert comparisons["items"] == [{"group_id": "g1"}]
        snapshot = client.post(f"/api/v1/reports/{identifier}/snapshot").json()
        assert snapshot["portable"] is True

        traversal = client.get(
            f"/api/v1/reports/{identifier}/artifacts/../../manifest.yaml"
        )
        assert traversal.status_code in {403, 404}


def test_temporary_report_can_be_used_saved_and_discarded(tmp_path: Path) -> None:
    source = _report(tmp_path)
    with _client(tmp_path) as client:
        library = client.app.state.report_library
        temporary_path = library.temporary_output("Quick preview")
        shutil.copytree(source, temporary_path)
        temporary = library.register_temporary(temporary_path, name="Quick preview")

        descriptor = client.get(f"/api/v1/reports/{temporary['id']}/preview")
        assert descriptor.status_code == 200
        assert descriptor.json()["storage_kind"] == "temporary"
        assert descriptor.json()["name"] == "Quick preview"
        assert client.get(f"/api/v1/reports/{temporary['id']}/overview").status_code == 200
        assert client.post(f"/api/v1/reports/{temporary['id']}/lease").status_code == 200
        assert all(item["id"] != temporary["id"] for item in client.get("/api/v1/reports").json()["items"])

        target = tmp_path / "saved-preview"
        queued = client.post(
            f"/api/v1/reports/{temporary['id']}/persist",
            json={"output_dir": str(target)},
        ).json()
        deadline = time.monotonic() + 3
        while True:
            job = client.get(f"/api/v1/jobs/{queued['id']}").json()
            if job["status"] in TERMINAL_STATES:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert job["status"] == "succeeded"
        assert target.is_dir()
        saved_id = job["result"]["report_id"]
        assert client.get(f"/api/v1/reports/{saved_id}/preview").json()["storage_kind"] == "saved"
        assert client.get(f"/api/v1/reports/{temporary['id']}/preview").status_code == 404

        discard_path = library.temporary_output("Discard me")
        shutil.copytree(source, discard_path)
        discard = library.register_temporary(discard_path, name="Discard me")
        assert client.delete(f"/api/v1/reports/{discard['id']}/preview").status_code == 204
        assert not discard_path.exists()


def test_preview_build_creates_complete_unlisted_temporary_report(tmp_path: Path) -> None:
    record = _record(tmp_path / "record")
    with _client(tmp_path) as client:
        queued = client.post(
            "/api/v1/reports/previews",
            json={
                "experiments": [{"id": "demo", "results": str(record)}],
                "report_name": "Quick look",
                "engine": "normalized",
            },
        )
        assert queued.status_code == 202
        deadline = time.monotonic() + 5
        while True:
            job = client.get(f"/api/v1/jobs/{queued.json()['id']}").json()
            if job["status"] in TERMINAL_STATES:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert job["status"] == "succeeded", job.get("error")
        identifier = job["result"]["report_id"]
        descriptor = client.get(f"/api/v1/reports/{identifier}/preview").json()
        assert descriptor["name"] == "Quick look"
        assert descriptor["storage_kind"] == "temporary"
        assert descriptor["has_index"] is True
        assert all(item["id"] != identifier for item in client.get("/api/v1/reports").json()["items"])


def test_job_sse_sequence_and_last_event_id(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "standalone-jobs.sqlite")
    job = manager.submit(
        "test",
        {"value": 1},
        lambda context: (
            context.progress("half", current=1, total=2, unit="steps"),
            {"ok": True},
        )[1],
    )
    deadline = time.monotonic() + 3
    while manager.get(job.id).status not in {"succeeded", "failed"}:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    app = create_app(
        report_roots=[tmp_path],
        results_roots=[tmp_path],
        job_manager=manager,
    )
    with _Client(app) as client:
        response = client.get(f"/api/v1/jobs/{job.id}/events")
        assert response.status_code == 200
        assert "event: queued" in response.text
        assert "event: complete" in response.text
        sequences = [event.sequence for event in manager.events(job.id)]
        assert sequences == list(range(1, len(sequences) + 1))

        resumed = client.get(
            f"/api/v1/jobs/{job.id}/events",
            headers={"Last-Event-ID": str(sequences[-2])},
        )
        assert "event: queued" not in resumed.text
        assert "event: complete" in resumed.text


def test_job_manager_bounds_concurrent_background_work(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "bounded-jobs.sqlite", max_workers=2)
    lock = threading.Lock()
    release = threading.Event()
    two_started = threading.Event()
    counters = {"active": 0, "maximum": 0}

    def task(_context) -> dict[str, bool]:
        with lock:
            counters["active"] += 1
            counters["maximum"] = max(counters["maximum"], counters["active"])
            if counters["active"] == 2:
                two_started.set()
        release.wait(timeout=3)
        with lock:
            counters["active"] -= 1
        return {"ok": True}

    jobs = [manager.submit("bounded", {"index": index}, task) for index in range(6)]
    assert two_started.wait(timeout=2)
    time.sleep(0.05)
    assert counters["maximum"] == 2
    assert sum(manager.get(job.id).status == "running" for job in jobs) == 2
    release.set()
    deadline = time.monotonic() + 3
    while any(manager.get(job.id).status not in TERMINAL_STATES for job in jobs):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert all(manager.get(job.id).status == "succeeded" for job in jobs)


def test_vite_ui_base_and_api_fallback(tmp_path: Path) -> None:
    frontend = tmp_path / "dist"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("<title>Workbench</title>", encoding="utf-8")
    (frontend / "assets" / "main.js").write_text("window.ready=true", encoding="utf-8")
    with _client(tmp_path, frontend_dir=frontend) as client:
        root = client.get("/")
        assert root.status_code == 307
        assert root.headers["location"] == "/ui/"
        assert "Workbench" in client.get("/ui/").text
        assert "window.ready" in client.get("/ui/assets/main.js").text
        missing = client.get("/api/v1/not-a-route")
        assert missing.status_code == 404
        assert missing.json()["code"] == "route_not_found"


def test_workbench_server_refuses_non_loopback_bind() -> None:
    with pytest.raises(ValueError, match="only supports loopback"):
        run_server(host="0.0.0.0", open_browser=False)


def test_runner_resume_actions_match_the_runner_state_machine() -> None:
    assert RunnerResumeRequest(action="stop").action == "stop"
    with pytest.raises(ValueError):
        RunnerResumeRequest(action="run_all")


def test_normalized_report_contract_and_publication_export(tmp_path: Path) -> None:
    source = tmp_path / "source" / "experiment"
    monitor = source / "iteration_1" / "monitor"
    monitor.mkdir(parents=True)
    (source / "execution_manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "execution_id": "normalized-contract",
                "scenario_name": "cut-in",
                "completed_at": "2026-01-01T00:00:00Z",
                "summary": {"finished": 1},
                "execution": {"sampler_name": "lhs"},
            }
        ),
        encoding="utf-8",
    )
    with (monitor / "result.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "run.status",
                "run.test_outcome",
                "run.sample_id",
                "run.parameter_hash",
                "run.params",
                "run.final_sim_time_ms",
                "min_ttc.min_ttc_s",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "run.status": "finished",
                "run.test_outcome": "success",
                "run.sample_id": "1",
                "run.parameter_hash": "hash-1",
                "run.params": json.dumps({"speed": 12.5, "gap": 8.0}),
                "run.final_sim_time_ms": 12_500,
                "min_ttc.min_ttc_s": 0.75,
            }
        )
    bundle = build_report_bundle(tmp_path / "source", tmp_path / "normalized-report")

    with _client(tmp_path) as client:
        catalog = client.get("/api/v1/reports", params={"search": "normalized-report"}).json()
        assert len(catalog["items"]) == 1
        descriptor = catalog["items"][0]
        assert descriptor["status"] == "ready"
        assert descriptor["run_count"] == 1
        identifier = descriptor["id"]

        overview = client.get(f"/api/v1/reports/{identifier}/overview").json()
        assert overview["outcomes"] == {
            "success": 1,
            "fail": 0,
            "invalid": 0,
            "unknown": 0,
        }
        assert overview["experiment_summaries"][0]["avg_time_seconds"] == 12.5
        run = client.get(f"/api/v1/reports/{identifier}/runs").json()["items"][0]
        assert run["duration_seconds"] == 12.5
        assert run["min_ttc"] == 0.75
        charts = client.get(
            f"/api/v1/reports/{identifier}/charts", params={"section": "outcomes"}
        ).json()
        assert charts["visualizations"][0]["id"] == "outcomes-overall"

        queued = client.post(
            f"/api/v1/reports/{identifier}/export",
            json={
                "visualization_id": "outcomes-overall",
                "format": "svg",
                "preset": "paper-single",
            },
        )
        assert queued.status_code == 202
        job = _wait_for_job(client, queued.json()["id"])
        assert job["status"] == "succeeded"
        assert (bundle.output_dir / job["result"]["path"]).is_file()
        refreshed = client.get(
            f"/api/v1/reports/{identifier}/charts", params={"section": "outcomes"}
        ).json()
        assert any(item["path"] == job["result"]["path"] for item in refreshed["items"])
        artifact = client.app.state.report_library.artifact(
            identifier, job["result"]["path"]
        )
        assert b"<svg" in artifact.read_bytes()[:500]


def test_report_browser_inspection_scatter_case_and_management(tmp_path: Path) -> None:
    source = tmp_path / "outputs" / "experiment-a"
    manifest = {
        "execution_id": "browser-test",
        "scenario_name": "cut-in",
        "completed_at": "2026-01-01T00:00:00Z",
        "summary": {"finished": 2},
        "execution": {"sampler_name": "lhs"},
        "metadata": {"map_name": "test-map"},
        "components": {
            "simulator": {"component": {"name": "esmini"}},
            "av": {"component": {"name": "simple-av"}},
        },
    }
    source.mkdir(parents=True)
    (source / "execution_manifest.yaml").write_text(
        yaml.safe_dump(manifest), encoding="utf-8"
    )
    for scenario, speed in ((1, 10.0), (2, 20.0)):
        monitor = source / f"iteration_{scenario}" / "monitor"
        monitor.mkdir(parents=True)
        with (monitor / "result.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "run.status",
                    "run.test_outcome",
                    "run.sample_id",
                    "run.params",
                    "run.stop_condition",
                    "run.stop_reason",
                    "metric.score",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "run.status": "finished",
                    "run.test_outcome": "success",
                    "run.sample_id": scenario,
                        "run.params": json.dumps({"speed": speed, "gap": 5 + scenario, "weather": "clear" if scenario == 1 else "rain"}),
                    "run.stop_condition": "scenario_complete",
                    "run.stop_reason": f"completed_{scenario}",
                    "metric.score": speed / 2,
                }
            )
        (monitor / "frame_metrics.csv").write_text(
            "step_index,sim_time_ms,ego.speed,pair.ttc_s,ego.throttle,ego.brake\n"
            "0,0,10,2.5,0.2,0.0\n1,50,11,2.0,0.8,0.4\n",
            encoding="utf-8",
        )
        (monitor / "agent_states.csv").write_text(
            "step_index,sim_time_ms,agent_id,entity_name,is_ego,x,y,speed\n"
            "0,0,0,Ego,True,0,0,10\n1,50,0,Ego,True,1,0,11\n",
            encoding="utf-8",
        )
        (monitor / "agent_geometry.csv").write_text(
            "step_index,sim_time_ms,agent_id,entity_name,is_ego,length_m,width_m,height_m,reference_point,source\n"
            "0,0,0,Ego,True,4.5,2.1,1.8,esmini_object_reference_point,observation\n",
            encoding="utf-8",
        )
    bundle = build_report_bundle(tmp_path / "outputs", tmp_path / "reports" / "current")

    with _client(tmp_path) as client:
        browser = client.get(
            "/api/v1/reports/browser", params={"path": str(tmp_path / "reports")}
        ).json()
        assert browser["entries"][0]["is_report"] is True
        created = client.post(
            "/api/v1/reports/browser/directory",
            json={"parent": str(tmp_path / "reports"), "name": "new-report-parent"},
        )
        assert created.status_code == 200
        assert created.json()["path"] == str(tmp_path / "reports" / "new-report-parent")
        assert (tmp_path / "reports" / "new-report-parent").is_dir()
        duplicate = client.post(
            "/api/v1/reports/browser/directory",
            json={"parent": str(tmp_path / "reports"), "name": "new-report-parent"},
        )
        assert duplicate.status_code == 409
        traversal = client.post(
            "/api/v1/reports/browser/directory",
            json={"parent": str(tmp_path / "reports"), "name": "../escape"},
        )
        assert traversal.status_code == 400
        preview = client.get(
            "/api/v1/reports/preview", params={"path": str(bundle.output_dir)}
        ).json()
        assert preview["scenario_names"] == ["cut-in"]
        assert preview["sampler_names"] == ["lhs"]
        assert preview["simulator_names"] == ["esmini"]
        assert preview["av_names"] == ["simple-av"]
        catalog = client.get(
            "/api/v1/reports",
            params={"root": str(tmp_path / "reports"), "recursive": False},
        ).json()
        assert [item["name"] for item in catalog["items"]] == ["current"]

        inspection = client.get(
            "/api/v1/reports/inspect", params={"path": str(tmp_path / "outputs")}
        ).json()
        assert inspection["valid"] is True
        assert inspection["datasets"][0]["scenario"] == "cut-in"
        assert inspection["datasets"][0]["simulator"] == "esmini"
        assert inspection["datasets"][0]["av"] == "simple-av"

        identifier = catalog["items"][0]["id"]
        details = client.get(f"/api/v1/reports/{identifier}/details").json()
        assert details["experiments"][0]["manifest"]["scenario_name"] == "cut-in"
        scatter = client.get(f"/api/v1/reports/{identifier}/scatter").json()
        assert len(scatter["points"]) == 2
        assert {field["key"] for field in scatter["fields"]} >= {
            "param:speed",
            "metric:metric.score",
            "sample_order",
            "stop_condition",
            "stop_reason",
        }
        assert scatter["points"][0]["stop_condition"] == "scenario_complete"
        control_fields = {
            field["key"]: field for field in scatter["fields"] if field["source"] == "control"
        }
        assert control_fields["metric:control.max_throttle"]["numeric_count"] == 2
        assert control_fields["metric:control.max_brake"]["numeric_count"] == 2
        assert scatter["points"][0]["stop_reason"] == "completed_1"
        assert scatter["stop_reasons"] == ["completed_1", "completed_2"]
        continuous_filter = client.get(
            f"/api/v1/reports/{identifier}/scatter",
            params={"filter_field": "param:speed"},
        ).json()
        assert continuous_filter["filter"] == {
            "field": "param:speed",
            "kind": "continuous",
            "minimum": 10.0,
            "maximum": 20.0,
            "step": 1.0,
            "present_count": 2,
            "missing_count": 0,
        }
        assert [point["filter"] for point in continuous_filter["points"]] == [10.0, 20.0]
        discrete_filter = client.get(
            f"/api/v1/reports/{identifier}/scatter",
            params={"filter_field": "stop_reason"},
        ).json()
        assert discrete_filter["filter"]["kind"] == "discrete"
        assert discrete_filter["filter"]["values"] == ["completed_1", "completed_2"]
        assert [point["filter"] for point in discrete_filter["points"]] == [
            "completed_1",
            "completed_2",
        ]
        categorical_parameter_filter = client.get(
            f"/api/v1/reports/{identifier}/scatter",
            params={"filter_field": "param:weather"},
        ).json()
        assert categorical_parameter_filter["filter"]["kind"] == "discrete"
        assert categorical_parameter_filter["filter"]["values"] == ["clear", "rain"]
        filtered_scatter = client.get(
            f"/api/v1/reports/{identifier}/scatter",
            params={"stop_reason": "completed_2"},
        ).json()
        assert [point["stop_reason"] for point in filtered_scatter["points"]] == [
            "completed_2"
        ]
        case = client.get(
            f"/api/v1/reports/{identifier}/cases/experiment-a:1"
        ).json()
        assert case["traces"]["metrics"][0]["values"]["pair.ttc_s"] == 2.5
        assert case["geometry"][0]["length_m"] == 4.5
        assert case["navigation"]["next_run_id"] == "experiment-a:2"
        without_map = client.get(
            f"/api/v1/reports/{identifier}/cases/experiment-a:1",
            params={"include_map": False},
        ).json()
        assert without_map["map"] == {"status": "omitted"}

        renamed = client.post(
            f"/api/v1/reports/{identifier}/rename", json={"new_name": "renamed"}
        ).json()
        assert renamed["name"] == "renamed"
        assert not bundle.output_dir.exists()
        mismatch = client.delete(
            f"/api/v1/reports/{renamed['id']}", json={"confirm_name": "wrong"}
        )
        assert mismatch.status_code == 409
        removed = client.delete(
            f"/api/v1/reports/{renamed['id']}", json={"confirm_name": "renamed"}
        )
        assert removed.status_code == 204
        assert not (tmp_path / "reports" / "renamed").exists()


def test_legacy_rebuild_publishes_current_normalized_report(tmp_path: Path) -> None:
    source = tmp_path / "source"
    monitor = source / "iteration_1" / "monitor"
    monitor.mkdir(parents=True)
    (source / "execution_manifest.yaml").write_text(
        yaml.safe_dump({"scenario_name": "rebuild", "summary": {"finished": 1}}),
        encoding="utf-8",
    )
    (monitor / "result.csv").write_text(
        'run.status,run.test_outcome,run.params\nfinished,success,"{""x"":1}"\n',
        encoding="utf-8",
    )
    legacy = _report(tmp_path / "legacy-root")
    provenance = legacy / "provenance"
    provenance.mkdir()
    (provenance / "input_manifest.yaml").write_text(
        yaml.safe_dump({"inputs": [str(source)]}), encoding="utf-8"
    )
    destination = tmp_path / "rebuilt-current"

    with _client(tmp_path) as client:
        catalog = client.get("/api/v1/reports", params={"search": "nested-report"}).json()
        identifier = catalog["items"][0]["id"]
        queued = client.post(
            f"/api/v1/reports/{identifier}/rebuild",
            json={"output_dir": str(destination)},
        )
        assert queued.status_code == 202
        job = _wait_for_job(client, queued.json()["id"])
        assert job["status"] == "succeeded"
        rebuilt = client.get("/api/v1/reports", params={"search": "rebuilt-current"}).json()
        assert rebuilt["items"][0]["status"] == "ready"
        assert rebuilt["items"][0]["has_index"] is True

        # Current normalized reports contain portable input_manifest.json rather
        # than the legacy YAML. Their local source paths are recovered from the
        # normalized index when an in-place update is requested.
        normalized_identifier = rebuilt["items"][0]["id"]
        assert not (destination / "provenance" / "input_manifest.yaml").exists()
        assert (destination / "provenance" / "input_manifest.json").is_file()
        normalized_update = client.post(
            f"/api/v1/reports/{normalized_identifier}/rebuild",
            json={"overwrite": True},
        )
        assert normalized_update.status_code == 202
        normalized_job = _wait_for_job(client, normalized_update.json()["id"])
        assert normalized_job["status"] == "succeeded"
        assert normalized_job["result"]["report_id"] == normalized_identifier

        replaced = client.post(
            f"/api/v1/reports/{identifier}/rebuild", json={"overwrite": True}
        )
        assert replaced.status_code == 202
        replacement_job = _wait_for_job(client, replaced.json()["id"])
        assert replacement_job["status"] == "succeeded"
        assert replacement_job["result"]["report_id"] == identifier
        refreshed = client.get(f"/api/v1/reports/{identifier}/preview").json()
        assert refreshed["status"] == "ready"
        assert legacy.is_dir()


def test_animation_transcode_rejects_empty_upload(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/v1/tools/animation/transcode",
            params={"format": "gif"},
            content=b"",
        )
    assert response.status_code == 400
    assert response.json()["code"] == "empty_animation"


def _wait_for_job(client: _Client, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while True:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        assert time.monotonic() < deadline
        time.sleep(0.01)
