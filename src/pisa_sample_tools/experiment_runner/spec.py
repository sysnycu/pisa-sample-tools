from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def _goal(scenario_path: str) -> dict[str, Any]:
    path = Path(scenario_path) / "spec.yaml"
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    ego = data.get("ego", {}) if isinstance(data, dict) else {}
    if not isinstance(ego, dict):
        return {}
    ego = dict(ego)
    if "position" not in ego and "goal" in ego:
        ego["position"] = ego.pop("goal")
    return ego


def build_runner_spec(
    experiment: dict[str, Any], ports: dict[str, dict[str, int]], output_dir: Path, job_id: str
) -> dict[str, Any]:
    scenario = experiment["scenario"]
    map_spec = experiment["map"]
    runner_scenario = {
        "title": scenario["name"],
        "scenario_path": scenario["path"],
        "rmlib_path": experiment["runner"].get("rmlib_path", ""),
        "goal_config": scenario.get("goal_config") or _goal(scenario["path"]),
    }
    if scenario.get("stop_condition_config_path"):
        runner_scenario["stop_condition_config_path"] = scenario["stop_condition_config_path"]
    result: dict[str, Any] = {
        "runtime": experiment.get("runtime", {"dt": 0.05}),
        "task": {"job_id": job_id, "output_dir": str(output_dir)},
        "map": map_spec,
        "scenario": runner_scenario,
        "sampler": experiment.get("sampler", {}),
        "monitor": experiment["monitor"],
    }
    for role in ("simulator", "av"):
        component = experiment[role]
        result[role] = {
            "url": f"localhost:{ports[role]['service']}",
            "config_path": component["config_path"],
            "map": {
                "xodr_path": "/mnt/map/xodr",
                "osm_path": "/mnt/map/osm",
            },
            "output_path": "/mnt/output",
        }
    result["simulator"]["scenario"] = {
        "format": scenario.get("format", "open_scenario1"),
        "name": scenario["name"],
        "path": "/mnt/scenario",
    }
    for key in ("timeout", "observation_identity", "observation_order"):
        if key in experiment["av"]:
            result["av"][key] = experiment["av"][key]
    return result
