from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from pisa_sample_tools.evidence.service import build_evidence

from .commands import allocate_ports, build_command, common_mounts, docker_run_command
from .config import ConfigError
from .scenario import inspect_scenario_directory
from .spec import build_runner_spec

TERMINAL = {"succeeded", "failed", "cancelled", "report_ready"}


@dataclass
class ExperimentJob:
    experiment: dict[str, Any]
    action: str = "run_all"
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "queued"
    phase: str = "queued"
    messages: list[dict[str, Any]] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)
    ports: dict[str, dict[str, int]] = field(default_factory=dict)
    containers: dict[str, str] = field(default_factory=dict)
    output: str | None = None
    report: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    process: subprocess.Popen[str] | None = field(default=None, repr=False)
    log_processes: list[subprocess.Popen[str]] = field(default_factory=list, repr=False)

    def event(self, message: str, *, stream: str = "system") -> None:
        self.messages.append(
            {"index": len(self.messages), "time": time.time(), "stream": stream, "message": message}
        )

    def payload(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "experiment_id": self.experiment.get("id"),
            "label": self.experiment.get("label") or self.experiment.get("id"),
            "action": self.action,
            "status": self.status,
            "phase": self.phase,
            "messages": self.messages,
            "commands": self.commands,
            "ports": self.ports,
            "containers": self.containers,
            "output": self.output,
            "report": self.report,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


def validate_experiment(
    experiment: dict[str, Any], *, check_ports: bool = True, check_runtime: bool = False
) -> dict[str, Any]:
    findings: list[dict[str, str]] = []

    def finding(severity: str, code: str, message: str) -> None:
        findings.append({"severity": severity, "code": code, "message": message})

    if check_runtime:
        if shutil.which("docker") is None:
            finding("error", "docker_missing", "Docker CLI is not installed or not on PATH")
        else:
            try:
                docker_info = subprocess.run(
                    ["docker", "info"], capture_output=True, text=True, timeout=20
                )
                if docker_info.returncode:
                    finding(
                        "error",
                        "docker_unavailable",
                        docker_info.stderr.strip() or "Docker daemon is unavailable",
                    )
            except (OSError, subprocess.TimeoutExpired) as exc:
                finding("error", "docker_unavailable", f"failed to query Docker daemon: {exc}")

    paths = {
        "scenario": experiment.get("scenario", {}).get("path"),
        "xodr": experiment.get("map", {}).get("xodr_path"),
        "osm": experiment.get("map", {}).get("osm_path"),
        "runner": experiment.get("runner", {}).get("repo_path"),
        "simulator_config": experiment.get("simulator", {}).get("config_path"),
        "av_config": experiment.get("av", {}).get("config_path"),
        "monitor_config": experiment.get("monitor", {}).get("config_path"),
        "sampler_config": experiment.get("sampler", {}).get("config_path"),
    }
    for label, value in paths.items():
        if not value:
            if label not in {"osm", "sampler_config"}:
                finding("error", f"missing_{label}", f"{label} path is required")
            continue
        if not Path(value).expanduser().exists():
            severity = "warning" if label == "osm" else "error"
            finding(severity, f"path_{label}", f"{label} path does not exist: {value}")
    scenario = experiment.get("scenario", {})
    scenario_path = Path(scenario.get("path", ".")).expanduser()
    name = scenario.get("name", "")
    if scenario_path.is_dir():
        inspection = inspect_scenario_directory(scenario_path)
        findings.extend(inspection["findings"])
    if not name:
        finding("error", "scenario_name", "scenario name is required; inspect the folder or fill it manually")
    elif not (scenario_path / f"{name}.xosc").is_file():
        finding("error", "scenario_xosc", f"scenario file does not exist: {scenario_path / f'{name}.xosc'}")
    stop = scenario.get("stop_condition_config_path")
    if stop and not (Path(stop) if Path(stop).is_absolute() else scenario_path / stop).is_file():
        finding("error", "stop_conditions", f"stop-condition config does not exist: {stop}")
    for role in ("simulator", "av"):
        component = experiment.get(role, {})
        if not component.get("image"):
            finding("error", f"{role}_image", f"{role} image is required")
        context = component.get("build", {}).get("context")
        if context and not Path(context).exists():
            finding("error", f"{role}_build_context", f"build context does not exist: {context}")
        for mount in component.get("run", {}).get("mounts", []):
            source = mount.get("source") if isinstance(mount, dict) else None
            if not source or not Path(str(source)).expanduser().exists():
                finding("error", f"{role}_mount", f"mount source does not exist: {source}")
        if component.get("run", {}).get("gpu") and not Path("/dev/nvidiactl").exists():
            finding("warning", "gpu_unverified", f"{role} requests a GPU but /dev/nvidiactl is absent")
        if check_ports:
            for key, value in component.get("run", {}).get("ports", {}).items():
                if value not in {None, "auto", 0, "0"}:
                    with socket.socket() as sock:
                        try:
                            sock.bind(("127.0.0.1", int(value)))
                        except OSError:
                            finding("error", "port_in_use", f"{role} {key} port is unavailable: {value}")
    output_value = experiment.get("task", {}).get("output_dir")
    if output_value:
        output = Path(output_value).expanduser()
        if output.exists() and not output.is_dir():
            finding("error", "output_not_directory", f"output path is not a directory: {output}")
        elif output.is_dir() and any(output.iterdir()) and not any(
            (output / marker).exists()
            for marker in (
                ".pisa-experiment-runner.yaml",
                "resolved_experiment.yaml",
                "execution_manifest.yaml",
            )
        ):
            if experiment.get("task", {}).get("adopt_existing_output"):
                finding(
                    "warning",
                    "output_adopted",
                    f"existing output will be adopted by this experiment: {output}",
                )
            else:
                finding(
                    "error",
                    "output_not_owned",
                    f"non-empty output is not recognized as a PISA execution: {output}; "
                    "enable Adopt existing output only after reviewing its contents",
                )
    return {"valid": not any(row["severity"] == "error" for row in findings), "findings": findings}


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, ExperimentJob] = {}
        self.queue: deque[ExperimentJob] = deque()
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.worker = threading.Thread(target=self._worker, daemon=True)
        self.worker.start()

    def submit(self, experiment: dict[str, Any], action: str = "run_all") -> ExperimentJob:
        if action not in {"run_all", "build", "start", "report"}:
            raise ConfigError(f"unsupported job action: {action}")
        job = ExperimentJob(experiment=experiment, action=action)
        with self.lock:
            self.jobs[job.job_id] = job
            self.queue.append(job)
            self.wake.set()
        return job

    def resume(self, job_id: str, action: str) -> ExperimentJob:
        job = self.get(job_id)
        if action not in {"run", "stop", "report"}:
            raise ConfigError(f"unsupported resume action: {action}")
        if job.status not in TERMINAL:
            raise ConfigError("job is already active")
        job.action = action
        job.status = "queued"
        job.phase = "queued"
        job.error = None
        job.completed_at = None
        job.cancel_event.clear()
        with self.lock:
            self.queue.append(job)
            self.wake.set()
        return job

    def get(self, job_id: str) -> ExperimentJob:
        try:
            return self.jobs[job_id]
        except KeyError as exc:
            raise ConfigError(f"unknown job: {job_id}") from exc

    def cancel(self, job_id: str) -> ExperimentJob:
        job = self.get(job_id)
        job.cancel_event.set()
        with self.lock:
            if job.status == "queued" and job in self.queue:
                self.queue.remove(job)
                job.status = "cancelled"
                job.phase = "cancelled"
                job.completed_at = time.time()
        if job.process and job.process.poll() is None:
            with suppress(OSError):
                os.killpg(job.process.pid, 15)
        job.event("cancellation requested")
        return job

    def _worker(self) -> None:
        while True:
            self.wake.wait()
            with self.lock:
                job = self.queue.popleft() if self.queue else None
                if not self.queue:
                    self.wake.clear()
            if job is not None:
                self._execute(job)

    def _execute(self, job: ExperimentJob) -> None:
        job.started_at = time.time()
        job.status = "running"
        try:
            validation = validate_experiment(
                job.experiment,
                check_ports=job.action in {"build", "start", "run_all"},
                check_runtime=job.action not in {"report"},
            )
            if not validation["valid"] and job.action not in {"stop", "report"}:
                raise ConfigError("preflight failed: " + "; ".join(
                    row["message"] for row in validation["findings"] if row["severity"] == "error"
                ))
            if job.action in {"build", "run_all"}:
                self._raise_if_cancelled(job)
                self._build(job)
            if job.action in {"start", "run_all"}:
                self._raise_if_cancelled(job)
                self._start(job)
            if job.action in {"run", "run_all"}:
                self._raise_if_cancelled(job)
                self._run(job)
            if job.action in {"stop", "run_all"}:
                self._cleanup(job)
            if job.action == "report" or (
                job.action == "run_all" and job.experiment.get("analysis", {}).get("auto")
            ):
                self._report(job)
            if job.cancel_event.is_set():
                job.status = "cancelled"
                job.phase = "cancelled"
            elif job.phase != "report_ready":
                job.status = "succeeded"
                job.phase = "complete"
            job.event("job complete")
        except Exception as exc:
            job.error = str(exc)
            job.status = "cancelled" if job.cancel_event.is_set() else "failed"
            job.phase = job.status
            job.event(f"job {job.status}: {exc}")
            if job.containers:
                self._cleanup(job, suppress=True)
        finally:
            job.completed_at = time.time()

    @staticmethod
    def _raise_if_cancelled(job: ExperimentJob) -> None:
        if job.cancel_event.is_set():
            raise RuntimeError("job cancelled")

    def _command(self, job: ExperimentJob, command: list[str], *, cwd: str | None = None) -> None:
        job.commands.append(command)
        job.event("$ " + shlex.join(command))
        process = subprocess.Popen(
            command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True,
        )
        job.process = process
        assert process.stdout is not None
        for line in process.stdout:
            job.event(line.rstrip(), stream="process")
            if job.cancel_event.is_set() and process.poll() is None:
                os.killpg(process.pid, 15)
        returncode = process.wait()
        job.process = None
        if returncode:
            raise RuntimeError(f"command exited with status {returncode}: {shlex.join(command)}")

    def _build(self, job: ExperimentJob) -> None:
        job.phase = "building"
        force = bool(job.experiment.get("force_rebuild"))
        for role in ("simulator", "av"):
            component = job.experiment[role]
            image = str(component["image"])
            exists = subprocess.run(
                ["docker", "image", "inspect", image], capture_output=True, timeout=20
            ).returncode == 0
            if exists and not force:
                job.event(f"reusing image {image}")
                continue
            self._command(job, build_command(component, force=force), cwd=component["build"].get("repo_path"))

    def _start(self, job: ExperimentJob) -> None:
        job.phase = "starting"
        output = Path(job.experiment["task"]["output_dir"]).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        job.output = str(output)
        write_ownership_marker(job, output, "starting")
        mounts = common_mounts(job.experiment, output)
        for role in ("av", "simulator"):
            component = job.experiment[role]
            ports = allocate_ports(component)
            command, name = docker_run_command(
                component, role=role, job_id=job.job_id, ports=ports, mounts=mounts
            )
            self._command(job, command)
            job.ports[role] = ports
            job.containers[role] = name
            self._start_log_stream(job, name, role)
            timeout = int(component.get("run", {}).get("startup_timeout", 120))
            self._wait_port(job, ports["service"], timeout, role)

    def _start_log_stream(self, job: ExperimentJob, name: str, role: str) -> None:
        process = subprocess.Popen(
            ["docker", "logs", "--follow", "--tail", "50", name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        job.log_processes.append(process)

        def consume() -> None:
            if process.stdout:
                for line in process.stdout:
                    job.event(line.rstrip(), stream=role)

        threading.Thread(target=consume, daemon=True).start()

    @staticmethod
    def _wait_port(job: ExperimentJob, port: int, timeout: int, role: str) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if job.cancel_event.is_set():
                raise RuntimeError("cancelled during service startup")
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    job.event(f"{role} ready on localhost:{port}")
                    return
            time.sleep(0.5)
        raise RuntimeError(f"{role} did not become ready on port {port} within {timeout}s")

    def _run(self, job: ExperimentJob) -> None:
        if not job.containers or not job.ports:
            raise ConfigError("start the AV and simulator before running")
        job.phase = "running"
        output = Path(job.output or job.experiment["task"]["output_dir"]).resolve()
        output.mkdir(parents=True, exist_ok=True)
        spec = build_runner_spec(job.experiment, job.ports, output, job.job_id)
        spec_path = output / "runner_spec.json"
        spec_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        (output / "resolved_experiment.yaml").write_text(
            yaml.safe_dump(job.experiment, sort_keys=False), encoding="utf-8"
        )
        command = [str(item).replace("{runner_spec}", str(spec_path)) for item in job.experiment["runner"]["command"]]
        self._command(job, command, cwd=job.experiment["runner"]["repo_path"])
        write_ownership_marker(job, output, "runner_complete")

    def _cleanup(self, job: ExperimentJob, *, suppress: bool = False) -> None:
        job.phase = "cleaning"
        errors = []
        for role, name in list(job.containers.items())[::-1]:
            result = subprocess.run(["docker", "stop", name], capture_output=True, text=True, timeout=30)
            if result.returncode and "No such container" not in result.stderr:
                errors.append(f"{role}: {result.stderr.strip()}")
            else:
                job.event(f"stopped {role} container {name}")
            job.containers.pop(role, None)
        for process in job.log_processes:
            if process.poll() is None:
                process.terminate()
        job.log_processes.clear()
        if job.output:
            write_ownership_marker(job, Path(job.output), "cleaned")
        if errors and not suppress:
            raise RuntimeError("container cleanup failed: " + "; ".join(errors))

    def _report(self, job: ExperimentJob) -> None:
        job.phase = "reporting"
        analysis = job.experiment.get("analysis", {})
        results = Path(job.output or job.experiment["task"]["output_dir"])
        report_output = Path(analysis.get("output_dir") or f"{results}-report").expanduser().resolve()
        result = build_evidence(
            results_paths=[results], output_dir=report_output,
            spec_path=Path(analysis["spec_path"]) if analysis.get("spec_path") else None,
            overwrite=bool(analysis.get("overwrite")),
            progress=job.event,
            report_mode=analysis.get("report_mode", "interactive"),
            sensitivity=analysis.get("sensitivity"),
        )
        job.report = str(result.report_path)
        job.phase = "report_ready"
        job.status = "report_ready"
        job.event(f"report ready: {job.report}")


def stale_containers() -> list[dict[str, str]]:
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", "label=pisa.experiment-runner=true", "--format", "{{.ID}}\t{{.Names}}\t{{.Status}}"],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode:
        return []
    rows = []
    for line in result.stdout.splitlines():
        container_id, name, status = (line.split("\t", 2) + ["", ""])[:3]
        rows.append({"id": container_id, "name": name, "status": status})
    return rows


def write_ownership_marker(job: ExperimentJob, output: Path, status: str) -> Path:
    marker = output / ".pisa-experiment-runner.yaml"
    previous: dict[str, Any] = {}
    if marker.is_file():
        try:
            previous = yaml.safe_load(marker.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            previous = {}
    marker.write_text(
        yaml.safe_dump(
            {
                "tool": "pisa-experiment-runner",
                "job_id": job.job_id,
                "experiment_id": job.experiment.get("id"),
                "created_at": previous.get("created_at", time.time()),
                "updated_at": time.time(),
                "status": status,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return marker
