from __future__ import annotations

import json
import secrets
import socket
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from .commands import allocate_ports, build_command, common_mounts, docker_run_command
from .config import ConfigError, ConfigStore
from .orchestrator import JobManager, stale_containers, validate_experiment
from .scenario import inspect_scenario_directory
from .spec import build_runner_spec


class ExperimentRequest(BaseModel):
    experiment_id: str
    overrides: dict[str, Any] = Field(default_factory=dict)


class JobRequest(ExperimentRequest):
    action: str = "run_all"


class RegistryRequest(BaseModel):
    registry: dict[str, Any]


class CleanupRequest(BaseModel):
    name: str


class CreatePresetRequest(BaseModel):
    preset_id: str
    template_id: str
    label: str = ""
    simulator_component: str
    av_component: str
    tags: list[str] = Field(default_factory=list)


class UpdatePresetRequest(BaseModel):
    experiment: dict[str, Any]


class RenamePresetRequest(BaseModel):
    new_id: str
    label: str | None = None


class DeletePresetRequest(BaseModel):
    confirm: bool = False


class ScenarioRequest(BaseModel):
    path: str


def create_app(
    store: ConfigStore,
    *,
    token: str | None = None,
    manager: JobManager | None = None,
) -> FastAPI:
    session_token = token or secrets.token_urlsafe(24)
    app = FastAPI(title="PISA Experiment Runner", docs_url=None, redoc_url=None)
    app.state.token = session_token
    app.state.store = store
    app.state.manager = manager or JobManager()

    def authorize(
        token: str | None = Query(default=None), x_pisa_token: str | None = Header(default=None)
    ) -> None:
        if (x_pisa_token or token) != session_token:
            raise HTTPException(status_code=403, detail="invalid experiment-runner session token")

    def resolved(request: ExperimentRequest) -> dict[str, Any]:
        return store.resolve_experiment(request.experiment_id, request.overrides)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        path = Path(__file__).with_name("web") / "index.html"
        return path.read_text(encoding="utf-8").replace("__PISA_TOKEN__", session_token)

    @app.get("/api/registry", dependencies=[Depends(authorize)])
    def registry() -> dict[str, Any]:
        return _handle(store.editable)

    @app.put("/api/registry", dependencies=[Depends(authorize)])
    def save_registry(request: RegistryRequest) -> dict[str, Any]:
        _handle(lambda: store.save(request.registry))
        return {"path": str(store.path.resolve()), "registry": store.editable()}

    @app.post("/api/presets", dependencies=[Depends(authorize)])
    def create_preset(request: CreatePresetRequest) -> dict[str, Any]:
        experiment = _handle(
            lambda: store.create_preset(
                request.preset_id,
                template_id=request.template_id,
                label=request.label,
                simulator_component=request.simulator_component,
                av_component=request.av_component,
                tags=request.tags,
            )
        )
        return {"preset_id": request.preset_id.strip(), "experiment": experiment}

    @app.put("/api/presets/{preset_id}", dependencies=[Depends(authorize)])
    def update_preset(preset_id: str, request: UpdatePresetRequest) -> dict[str, Any]:
        return {
            "preset_id": preset_id,
            "experiment": _handle(lambda: store.update_preset(preset_id, request.experiment)),
        }

    @app.post("/api/presets/{preset_id}/rename", dependencies=[Depends(authorize)])
    def rename_preset(preset_id: str, request: RenamePresetRequest) -> dict[str, Any]:
        return _handle(
            lambda: store.rename_preset(preset_id, new_id=request.new_id, label=request.label)
        )

    @app.post("/api/presets/{preset_id}/delete", dependencies=[Depends(authorize)])
    def delete_preset(preset_id: str, request: DeletePresetRequest) -> dict[str, str]:
        if not request.confirm:
            raise HTTPException(status_code=400, detail="explicit deletion confirmation is required")
        _handle(lambda: store.delete_preset(preset_id))
        return {"deleted": preset_id}

    @app.get("/api/browse", dependencies=[Depends(authorize)])
    def browse(path: str = ".", kind: str = "any") -> dict[str, Any]:
        def scan() -> dict[str, Any]:
            requested = Path(path).expanduser().resolve()
            root = requested.parent if requested.is_file() else requested
            while not root.exists() and root != root.parent:
                root = root.parent
            if not root.is_dir():
                raise ConfigError(f"no browsable parent directory exists for: {requested}")
            entries = []
            for child in sorted(root.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
                if child.name.startswith("."):
                    continue
                if child.is_dir() or kind == "any" or child.suffix.lower() in {".yaml", ".yml", ".json"}:
                    entries.append({"name": child.name, "path": str(child), "is_directory": child.is_dir()})
            return {
                "path": str(root),
                "requested": str(requested),
                "parent": str(root.parent),
                "entries": entries,
            }

        return _handle(scan)

    @app.post("/api/scenarios/inspect", dependencies=[Depends(authorize)])
    def inspect_scenario(request: ScenarioRequest) -> dict[str, Any]:
        return _handle(lambda: inspect_scenario_directory(Path(request.path)))

    @app.post("/api/experiments/resolve", dependencies=[Depends(authorize)])
    def resolve(request: ExperimentRequest) -> dict[str, Any]:
        return _handle(lambda: resolved(request))

    @app.post("/api/experiments/validate", dependencies=[Depends(authorize)])
    def validate(request: ExperimentRequest) -> dict[str, Any]:
        return _handle(lambda: validate_experiment(resolved(request), check_runtime=True))

    @app.post("/api/experiments/preview", dependencies=[Depends(authorize)])
    def preview(request: ExperimentRequest) -> dict[str, Any]:
        def make_preview() -> dict[str, Any]:
            experiment = resolved(request)
            output = Path(experiment["task"]["output_dir"]).expanduser().resolve()
            ports = {role: allocate_ports(experiment[role]) for role in ("simulator", "av")}
            mounts = common_mounts(experiment, output)
            commands: dict[str, Any] = {"build": {}, "run": {}}
            for role in ("simulator", "av"):
                commands["build"][role] = build_command(experiment[role])
                commands["run"][role] = docker_run_command(
                    experiment[role], role=role, job_id="preview", ports=ports[role], mounts=mounts
                )[0]
            return {
                "experiment": experiment,
                "ports": ports,
                "commands": commands,
                "runner_spec": build_runner_spec(experiment, ports, output, "preview"),
                "validation": validate_experiment(experiment, check_ports=False),
            }

        return _handle(make_preview)

    @app.get("/api/jobs", dependencies=[Depends(authorize)])
    def jobs() -> dict[str, Any]:
        return {"jobs": [job.payload() for job in app.state.manager.jobs.values()]}

    @app.post("/api/jobs", dependencies=[Depends(authorize)])
    def submit(request: JobRequest) -> dict[str, Any]:
        experiment = _handle(lambda: resolved(request))
        return _handle(lambda: app.state.manager.submit(experiment, request.action).payload())

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(authorize)])
    def job(job_id: str) -> dict[str, Any]:
        return _handle(lambda: app.state.manager.get(job_id).payload())

    @app.get("/api/jobs/{job_id}/events", dependencies=[Depends(authorize)])
    def events(job_id: str) -> StreamingResponse:
        target = _handle(lambda: app.state.manager.get(job_id))

        def stream():
            cursor = 0
            while True:
                while cursor < len(target.messages):
                    yield f"data: {json.dumps(target.messages[cursor])}\n\n"
                    cursor += 1
                if target.status in {"succeeded", "failed", "cancelled", "report_ready"}:
                    yield f"event: complete\ndata: {json.dumps(target.payload())}\n\n"
                    return
                time.sleep(0.2)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(authorize)])
    def cancel(job_id: str) -> dict[str, Any]:
        return _handle(lambda: app.state.manager.cancel(job_id).payload())

    @app.post("/api/jobs/{job_id}/{action}", dependencies=[Depends(authorize)])
    def resume(job_id: str, action: str) -> dict[str, Any]:
        return _handle(lambda: app.state.manager.resume(job_id, action).payload())

    @app.get("/api/resources", dependencies=[Depends(authorize)])
    def resources() -> dict[str, Any]:
        return {"containers": _handle(stale_containers)}

    @app.post("/api/resources/cleanup", dependencies=[Depends(authorize)])
    def cleanup(request: CleanupRequest) -> dict[str, str]:
        def stop() -> dict[str, str]:
            inspect = subprocess.run(
                ["docker", "inspect", "--format", "{{ index .Config.Labels \"pisa.experiment-runner\" }}", request.name],
                capture_output=True, text=True, timeout=20,
            )
            if inspect.returncode or inspect.stdout.strip() != "true":
                raise ConfigError("refusing to stop a container not owned by PISA Experiment Runner")
            stopped = subprocess.run(["docker", "stop", request.name], capture_output=True, text=True, timeout=30)
            if stopped.returncode:
                raise ConfigError(stopped.stderr.strip() or "docker stop failed")
            return {"stopped": request.name}

        return _handle(stop)

    @app.get("/reports/{report_token}/{job_id}")
    def report_index(report_token: str, job_id: str) -> RedirectResponse:
        if report_token != session_token:
            raise HTTPException(status_code=403, detail="invalid experiment-runner session token")
        target = _handle(lambda: app.state.manager.get(job_id))
        if not target.report or not Path(target.report).is_file():
            raise HTTPException(status_code=404, detail="report is not ready")
        return RedirectResponse(f"/reports/{report_token}/{job_id}/report/analysis_report.html")

    @app.get("/reports/{report_token}/{job_id}/{asset_path:path}")
    def report_asset(report_token: str, job_id: str, asset_path: str) -> FileResponse:
        if report_token != session_token:
            raise HTTPException(status_code=403, detail="invalid experiment-runner session token")
        target = _handle(lambda: app.state.manager.get(job_id))
        if not target.report:
            raise HTTPException(status_code=404, detail="report is not ready")
        root = Path(target.report).resolve().parent.parent
        requested = (root / asset_path).resolve()
        if not requested.is_relative_to(root) or not requested.is_file():
            raise HTTPException(status_code=404, detail="report asset not found")
        return FileResponse(requested)

    return app


def run_server(
    *,
    config: Path,
    local_config: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ConfigError("experiment runner only supports loopback hosts")
    selected_port = port or _available_port()
    token = secrets.token_urlsafe(24)
    app = create_app(ConfigStore(config, local_config), token=token)
    url = f"http://127.0.0.1:{selected_port}/"
    print(f"PISA Experiment Runner: {url}")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=selected_port, log_level="warning")


def _handle(callback):
    try:
        return callback()
    except (ConfigError, OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
