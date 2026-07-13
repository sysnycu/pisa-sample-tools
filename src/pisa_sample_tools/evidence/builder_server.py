from __future__ import annotations

import json
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from pisa_sample_tools.common.yaml import write_yaml

from .builder import (
    browse_path,
    campaign_document,
    compare_experiments,
    default_spec,
    export_yaml,
    inspect_output,
    preview_campaign,
    preview_experiment,
    preview_spec,
    scan_reports,
    validate_builder_request,
)
from .models import EvidenceError
from .service import build_evidence, enrich_sensitivity_bundle


class PathRequest(BaseModel):
    path: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class DraftRequest(BaseModel):
    experiments: list[dict[str, Any]]
    spec: dict[str, Any] = Field(default_factory=default_spec)


class ExportRequest(BaseModel):
    path: str
    data: dict[str, Any]


class RenameReportRequest(BaseModel):
    name: str


class DeleteReportRequest(BaseModel):
    confirm: bool = False


class BatchUpdateRequest(BaseModel):
    report_ids: list[str]
    confirm: bool = False


class BuildRequest(DraftRequest):
    output: str
    overwrite: bool = False
    validation: str | None = None
    report_mode: str = "interactive"
    sensitivity: bool | None = None


@dataclass
class BuildJob:
    job_id: str
    status: str = "queued"
    messages: list[dict[str, Any]] = field(default_factory=list)
    output: str | None = None
    report: str | None = None
    error: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    estimated_duration_seconds: float | None = None

    def event(self, message: str) -> None:
        self.messages.append({"index": len(self.messages), "time": time.time(), "message": message})

    def payload(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "messages": self.messages,
            "output": self.output,
            "report": self.report,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "estimated_duration_seconds": self.estimated_duration_seconds,
        }


def create_builder_app(token: str | None = None) -> FastAPI:
    session_token = token or secrets.token_urlsafe(24)
    app = FastAPI(title="PISA Report Builder", docs_url=None, redoc_url=None)
    app.state.token = session_token
    app.state.jobs = {}
    app.state.reports = {}
    app.state.lock = threading.Lock()

    def authorize(
        token: str | None = Query(default=None), x_pisa_token: str | None = Header(default=None)
    ) -> None:
        if (x_pisa_token or token) != session_token:
            raise HTTPException(status_code=403, detail="invalid builder session token")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        path = Path(__file__).with_name("builder_web") / "index.html"
        return path.read_text(encoding="utf-8").replace("__PISA_TOKEN__", session_token)

    @app.get("/api/browse", dependencies=[Depends(authorize)])
    def browse(path: str = "/opt/sbsvf/outputs/", kind: str = "directory") -> dict[str, Any]:
        return _handle(lambda: browse_path(Path(path), kind=kind))

    @app.get("/api/reports", dependencies=[Depends(authorize)])
    def reports(root: str = "./analysis") -> dict[str, Any]:
        result = _handle(lambda: scan_reports(Path(root)))
        app.state.reports = {item["report_id"]: item["path"] for item in result["reports"]}
        return result

    @app.get("/api/output", dependencies=[Depends(authorize)])
    def output_status(path: str) -> dict[str, Any]:
        return _handle(lambda: inspect_output(Path(path)))

    @app.post("/api/reports/{report_id}/rename", dependencies=[Depends(authorize)])
    def rename_report(report_id: str, request: RenameReportRequest) -> dict[str, str]:
        root = _report_root(app, report_id)
        name = request.name.strip()
        if not name or name in {".", ".."} or Path(name).name != name:
            raise HTTPException(status_code=400, detail="report name must be one folder name")
        destination = root.with_name(name)
        if destination.exists():
            raise HTTPException(status_code=409, detail="a file or report with that name exists")
        root.rename(destination)
        app.state.reports.pop(report_id, None)
        return {"path": str(destination), "name": destination.name}

    @app.post("/api/reports/{report_id}/delete", dependencies=[Depends(authorize)])
    def delete_report(report_id: str, request: DeleteReportRequest) -> dict[str, str]:
        if not request.confirm:
            raise HTTPException(status_code=400, detail="explicit deletion confirmation is required")
        root = _report_root(app, report_id)
        shutil.rmtree(root)
        app.state.reports.pop(report_id, None)
        return {"deleted": str(root)}

    @app.post("/api/reports/{report_id}/update", dependencies=[Depends(authorize)])
    def update_report(report_id: str) -> dict[str, Any]:
        root = _report_root(app, report_id)
        with app.state.lock:
            active = next(
                (job for job in app.state.jobs.values() if job.status in {"queued", "running"}),
                None,
            )
            if active:
                raise HTTPException(status_code=409, detail=f"build {active.job_id} is active")
            estimate = _previous_build_duration(root)
            job = BuildJob(
                secrets.token_hex(8),
                output=str(root),
                estimated_duration_seconds=estimate,
            )
            app.state.jobs[job.job_id] = job
            threading.Thread(target=_run_report_update, args=(job, root), daemon=True).start()
        return job.payload()

    @app.post("/api/reports/update-all", dependencies=[Depends(authorize)])
    def update_all_reports(request: BatchUpdateRequest) -> dict[str, Any]:
        if not request.confirm or not request.report_ids:
            raise HTTPException(status_code=400, detail="confirmed report IDs are required")
        roots = [_report_root(app, report_id) for report_id in request.report_ids]
        with app.state.lock:
            active = next(
                (job for job in app.state.jobs.values() if job.status in {"queued", "running"}),
                None,
            )
            if active:
                raise HTTPException(status_code=409, detail=f"build {active.job_id} is active")
            estimate = sum(_previous_build_duration(root) or 0 for root in roots) or None
            job = BuildJob(
                secrets.token_hex(8), estimated_duration_seconds=estimate
            )
            app.state.jobs[job.job_id] = job
            threading.Thread(target=_run_batch_update, args=(job, roots), daemon=True).start()
        return job.payload()

    @app.post("/api/reports/{report_id}/sensitivity", dependencies=[Depends(authorize)])
    def update_report_sensitivity(report_id: str) -> dict[str, Any]:
        root = _report_root(app, report_id)
        with app.state.lock:
            active = next(
                (job for job in app.state.jobs.values() if job.status in {"queued", "running"}),
                None,
            )
            if active:
                raise HTTPException(status_code=409, detail=f"build {active.job_id} is active")
            job = BuildJob(secrets.token_hex(8), output=str(root))
            app.state.jobs[job.job_id] = job
            threading.Thread(target=_run_sensitivity_update, args=(job, root), daemon=True).start()
        return job.payload()

    @app.post("/api/experiments/preview", dependencies=[Depends(authorize)])
    def experiment_preview(request: PathRequest) -> dict[str, Any]:
        return _handle(lambda: preview_experiment(Path(request.path), request.metadata))

    @app.post("/api/campaigns/preview", dependencies=[Depends(authorize)])
    def campaign_preview(request: PathRequest) -> dict[str, Any]:
        return _handle(lambda: preview_campaign(Path(request.path)))

    @app.post("/api/compatibility", dependencies=[Depends(authorize)])
    def compatibility(request: DraftRequest) -> dict[str, Any]:
        return compare_experiments(request.experiments)

    @app.get("/api/spec/default", dependencies=[Depends(authorize)])
    def spec_default() -> dict[str, Any]:
        return default_spec()

    @app.post("/api/spec/preview", dependencies=[Depends(authorize)])
    def spec_preview(request: PathRequest) -> dict[str, Any]:
        return _handle(lambda: preview_spec(Path(request.path)))

    @app.post("/api/validate", dependencies=[Depends(authorize)])
    def validate(request: DraftRequest, deep: bool = False) -> dict[str, Any]:
        return _handle(
            lambda: validate_builder_request(request.experiments, request.spec, deep=deep)
        )

    @app.post("/api/export/campaign", dependencies=[Depends(authorize)])
    def export_campaign(request: ExportRequest) -> dict[str, str]:
        experiments = request.data.get("experiments") or []
        return {"path": _handle(lambda: export_yaml(Path(request.path), campaign_document(experiments)))}

    @app.post("/api/export/spec", dependencies=[Depends(authorize)])
    def export_spec(request: ExportRequest) -> dict[str, str]:
        return {"path": _handle(lambda: export_yaml(Path(request.path), request.data))}

    @app.post("/api/animation/transcode", dependencies=[Depends(authorize)])
    async def transcode_animation(request: Request, format: str = "mp4") -> Response:
        if format not in {"mp4", "gif"}:
            raise HTTPException(status_code=400, detail="format must be mp4 or gif")
        source = await request.body()
        if not source or len(source) > 200 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="animation recording is empty or too large")
        with tempfile.TemporaryDirectory(prefix="pisa-animation-") as temporary:
            root = Path(temporary)
            input_path = root / "capture.webm"
            output_path = root / f"animation.{format}"
            input_path.write_bytes(source)
            command = ["ffmpeg", "-y", "-i", str(input_path)]
            if format == "mp4":
                command += ["-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
            else:
                command += [
                    "-an",
                    "-vf",
                    "fps=15,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer",
                    "-loop",
                    "0",
                ]
            command.append(str(output_path))
            completed = subprocess.run(command, capture_output=True, text=True, timeout=300)
            if completed.returncode or not output_path.is_file():
                detail = completed.stderr[-2000:] or "ffmpeg failed"
                raise HTTPException(status_code=500, detail=detail)
            return Response(
                output_path.read_bytes(),
                media_type="video/mp4" if format == "mp4" else "image/gif",
                headers={"Content-Disposition": f'attachment; filename="pisa-animation.{format}"'},
            )

    @app.post("/api/build", dependencies=[Depends(authorize)])
    def start_build(request: BuildRequest) -> dict[str, Any]:
        with app.state.lock:
            active = next(
                (job for job in app.state.jobs.values() if job.status in {"queued", "running"}),
                None,
            )
            if active:
                raise HTTPException(status_code=409, detail=f"build {active.job_id} is active")
            output = Path(request.output).expanduser().resolve()
            output_state = inspect_output(output)
            if output_state["state"] == "non_pisa_nonempty":
                raise HTTPException(
                    status_code=409, detail="output is a non-empty non-PISA directory"
                )
            if output_state["state"] == "not_directory":
                raise HTTPException(status_code=409, detail="output is not a directory")
            if output_state["state"] == "pisa_report" and not request.overwrite:
                raise HTTPException(status_code=409, detail="output exists; confirm overwrite")
            job = BuildJob(secrets.token_hex(8), output=str(output))
            app.state.jobs[job.job_id] = job
            thread = threading.Thread(target=_run_build, args=(job, request), daemon=True)
            thread.start()
            return job.payload()

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(authorize)])
    def job_status(job_id: str) -> dict[str, Any]:
        return _job(app, job_id).payload()

    @app.get("/api/jobs/{job_id}/events", dependencies=[Depends(authorize)])
    def job_events(job_id: str) -> StreamingResponse:
        job = _job(app, job_id)

        def stream():
            cursor = 0
            while True:
                while cursor < len(job.messages):
                    yield f"data: {json.dumps(job.messages[cursor])}\n\n"
                    cursor += 1
                if job.status in {"complete", "failed"}:
                    yield f"event: complete\ndata: {json.dumps(job.payload())}\n\n"
                    return
                time.sleep(0.2)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/reports/{report_token}/{job_id}/{asset_path:path}")
    def report_asset(
        report_token: str, job_id: str, asset_path: str = "report/analysis_report.html"
    ) -> FileResponse:
        if report_token != session_token:
            raise HTTPException(status_code=403, detail="invalid builder session token")
        job = _job(app, job_id)
        if job.status != "complete" or not job.output:
            raise HTTPException(status_code=409, detail="report is not ready")
        root = Path(job.output).resolve()
        requested = (root / asset_path).resolve()
        if not requested.is_relative_to(root) or not requested.is_file():
            raise HTTPException(status_code=404, detail="report asset not found")
        return FileResponse(requested)

    @app.get("/library/{report_token}/{report_id}/{asset_path:path}")
    def library_asset(
        report_token: str,
        report_id: str,
        asset_path: str = "report/analysis_report.html",
    ) -> FileResponse:
        if report_token != session_token:
            raise HTTPException(status_code=403, detail="invalid builder session token")
        report_root = app.state.reports.get(report_id)
        if not report_root:
            raise HTTPException(status_code=404, detail="unknown report")
        root = Path(report_root).resolve()
        requested = (root / asset_path).resolve()
        if not requested.is_relative_to(root) or not requested.is_file():
            raise HTTPException(status_code=404, detail="report asset not found")
        return FileResponse(requested)

    return app


def run_builder(*, host: str = "127.0.0.1", port: int = 0, open_browser: bool = True) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise EvidenceError("report builder only supports loopback hosts")
    selected_port = port or _available_port()
    token = secrets.token_urlsafe(24)
    app = create_builder_app(token)
    url = f"http://127.0.0.1:{selected_port}/"
    print(f"PISA Report Builder: {url}")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=selected_port, log_level="warning")


def _run_build(job: BuildJob, request: BuildRequest) -> None:
    job.status = "running"
    job.started_at = time.time()
    try:
        validation = validate_builder_request(request.experiments, request.spec, deep=True)
        if not validation["valid"]:
            raise EvidenceError("builder validation failed; resolve blocking findings")
        with tempfile.TemporaryDirectory(prefix="pisa-report-builder-") as temporary:
            root = Path(temporary)
            campaign_path = root / "campaign.yaml"
            spec_path = root / "analysis_spec.yaml"
            write_yaml(campaign_path, campaign_document(request.experiments))
            write_yaml(spec_path, request.spec)
            result = build_evidence(
                campaign_path=campaign_path,
                output_dir=Path(request.output),
                spec_path=spec_path,
                overwrite=request.overwrite,
                progress=job.event,
                validation_mode=request.validation,
                report_mode=request.report_mode,
                sensitivity=request.sensitivity,
            )
        job.report = str(result.report_path)
        job.status = "complete"
        job.event("report ready")
    except Exception as exc:  # job boundary intentionally captures user-facing failures
        job.error = str(exc)
        job.status = "failed"
        job.event(f"build failed: {exc}")
    finally:
        job.completed_at = time.time()


def _job(app: FastAPI, job_id: str) -> BuildJob:
    job = app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown build job")
    return job


def _report_root(app: FastAPI, report_id: str) -> Path:
    value = app.state.reports.get(report_id)
    if not value:
        raise HTTPException(status_code=404, detail="unknown report")
    root = Path(value).resolve()
    if not inspect_output(root).get("state") == "pisa_report":
        raise HTTPException(status_code=409, detail="report is no longer a valid PISA bundle")
    return root


def _run_report_update(job: BuildJob, root: Path) -> None:
    job.status = "running"
    job.started_at = time.time()
    try:
        result = _rebuild_report(root, job.event)
        job.report = str(result.report_path)
        job.status = "complete"
        job.event("report updated")
    except Exception as exc:
        job.error = str(exc)
        job.status = "failed"
        job.event(f"update failed: {exc}")
    finally:
        job.completed_at = time.time()


def _run_batch_update(job: BuildJob, roots: list[Path]) -> None:
    job.status = "running"
    job.started_at = time.time()
    try:
        for index, root in enumerate(roots, start=1):
            job.event(f"updating report {index}/{len(roots)}: {root.name}")
            _rebuild_report(root, lambda message, i=index: job.event(f"[{i}/{len(roots)}] {message}"))
        job.status = "complete"
        job.event(f"updated {len(roots)} report(s)")
    except Exception as exc:
        job.error = str(exc)
        job.status = "failed"
        job.event(f"batch update failed: {exc}")
    finally:
        job.completed_at = time.time()


def _rebuild_report(root: Path, progress) -> Any:
    provenance = root / "provenance"
    campaign = provenance / "resolved_campaign.yaml"
    spec = provenance / "resolved_analysis_spec.yaml"
    input_manifest = yaml.safe_load((provenance / "input_manifest.yaml").read_text()) or {}
    inputs = [Path(value) for value in input_manifest.get("inputs") or []]
    return build_evidence(
        campaign_path=campaign if campaign.is_file() else None,
        results_paths=None if campaign.is_file() else inputs,
        output_dir=root,
        spec_path=spec,
        overwrite=True,
        progress=progress,
        report_mode="interactive",
    )


def _previous_build_duration(root: Path) -> float | None:
    path = root / "provenance" / "stage_timings.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    elapsed = [float(row.get("elapsed_seconds", 0)) for row in rows if isinstance(row, dict)]
    return max(elapsed, default=0) or None


def _run_sensitivity_update(job: BuildJob, root: Path) -> None:
    job.status = "running"
    job.started_at = time.time()
    try:
        enrich_sensitivity_bundle(root, progress=job.event)
        job.status = "complete"
        job.event("sensitivity updated")
    except Exception as exc:
        job.error = str(exc)
        job.status = "failed"
        job.event(f"sensitivity update failed: {exc}")
    finally:
        job.completed_at = time.time()


def _handle(callback):
    try:
        return callback()
    except EvidenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
