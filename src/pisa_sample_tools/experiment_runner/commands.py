from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def allocate_ports(component: dict[str, Any]) -> dict[str, int]:
    allocated: dict[str, int] = {}
    for name, configured in component.get("run", {}).get("ports", {"service": "auto"}).items():
        allocated[name] = free_port() if configured in {None, "auto", 0, "0"} else int(configured)
    allocated.setdefault("service", free_port())
    return allocated


def build_command(component: dict[str, Any], *, force: bool = False) -> list[str]:
    build = component.get("build", {})
    builder = build.get("builder", "docker build")
    command = builder.split()
    if builder == "docker buildx build" and build.get("load", True):
        command.append("--load")
    if build.get("dockerfile"):
        command.extend(["--file", str(build["dockerfile"])])
    if build.get("target"):
        command.extend(["--target", str(build["target"])])
    if build.get("platform"):
        command.extend(["--platform", str(build["platform"])])
    if build.get("pull"):
        command.append("--pull")
    if force or build.get("no_cache"):
        command.append("--no-cache")
    for key, value in build.get("args", {}).items():
        command.extend(["--build-arg", f"{key}={value}"])
    command.extend(str(item) for item in build.get("extra_args", []))
    command.extend(["--tag", str(component["image"]), str(build.get("context", "."))])
    return command


def common_mounts(experiment: dict[str, Any], output_dir: Path) -> list[dict[str, str]]:
    mounts = [
        {"source": experiment["scenario"]["path"], "target": "/mnt/scenario", "mode": "ro"},
        {"source": str(output_dir), "target": "/mnt/output", "mode": "rw"},
    ]
    for key, target in (("xodr_path", "/mnt/map/xodr"), ("osm_path", "/mnt/map/osm")):
        if experiment["map"].get(key):
            mounts.append({"source": experiment["map"][key], "target": target, "mode": "ro"})
    return mounts


def docker_run_command(
    component: dict[str, Any],
    *,
    role: str,
    job_id: str,
    ports: dict[str, int],
    mounts: list[dict[str, str]],
) -> tuple[list[str], str]:
    run = component.get("run", {})
    name = f"pisa-{role}-{job_id}-{ports['service']}"
    command = [
        "docker", "run", "-d", "--rm",
        "--label", "pisa.experiment-runner=true",
        "--label", f"pisa.experiment-runner.job={job_id}",
        "--label", f"pisa.experiment-runner.role={role}",
        "--name", name,
        "--hostname", name,
        "--user", str(run.get("user", f"{os.getuid()}:{os.getgid()}")),
    ]
    if run.get("log_driver"):
        command.extend(["--log-driver", str(run["log_driver"])])
        for key, value in run.get("log_options", {}).items():
            command.extend(["--log-opt", f"{key}={value}"])
    network = run.get("network", "bridge")
    command.extend(["--network", str(network)])
    env = {str(key): str(value) for key, value in run.get("env", {}).items()}
    env["PORT"] = str(ports["service"])
    role_env = {"carla": "CARLA_PORT", "traffic_manager": "CARLA_TM_PORT"}
    for port_role, env_name in role_env.items():
        if port_role in ports:
            env[env_name] = str(ports[port_role])
    if run.get("ros_domain") is not None:
        value = run["ros_domain"]
        env["ROS_DOMAIN_ID"] = str(abs(hash(job_id)) % 232 if value == "auto" else value)
    for key, value in env.items():
        command.extend(["--env", f"{key}={value}"])
    if network != "host":
        for port in ports.values():
            command.extend(["--publish", f"{port}:{port}"])
    combined_mounts = [*mounts, *run.get("mounts", [])]
    for mount in combined_mounts:
        source = str(Path(mount["source"]).expanduser().resolve())
        spec = f"{source}:{mount['target']}"
        if mount.get("mode"):
            spec += f":{mount['mode']}"
        command.extend(["--volume", spec])
    if run.get("gpu"):
        command.extend(["--gpus", str(run.get("gpu_request", "all"))])
    if run.get("runtime"):
        command.extend(["--runtime", str(run["runtime"])])
    if run.get("entrypoint"):
        command.extend(["--entrypoint", str(run["entrypoint"])])
    command.extend(str(item) for item in run.get("extra_args", []))
    command.append(str(component["image"]))
    command.extend(str(item) for item in run.get("args", []))
    return command, name
