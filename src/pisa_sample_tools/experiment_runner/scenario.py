from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import ConfigError

STOP_CONDITION_NAMES = ("stop_conditions.yaml", "stop_condition.yaml")


def inspect_scenario_directory(path: Path) -> dict[str, Any]:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ConfigError(f"scenario directory does not exist: {root}")

    findings: list[dict[str, str]] = []

    def finding(severity: str, code: str, message: str) -> None:
        findings.append({"severity": severity, "code": code, "message": message})

    spec_path = root / "spec.yaml"
    spec: dict[str, Any] = {}
    if not spec_path.is_file():
        finding("error", "scenario_spec", f"required file is missing: {spec_path}")
    else:
        try:
            loaded = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            finding("error", "scenario_spec_invalid", f"failed to parse {spec_path}: {exc}")
        else:
            if isinstance(loaded, dict):
                spec = loaded
            else:
                finding("error", "scenario_spec_invalid", "spec.yaml must contain a mapping")

    declared_name = str(spec.get("scenario_name") or "").strip()
    xosc_files = sorted(root.glob("*.xosc"))
    preferred = None
    for candidate_name in (declared_name, root.name):
        candidate = root / f"{candidate_name}.xosc" if candidate_name else None
        if candidate and candidate.is_file():
            preferred = candidate
            break
    scenario_candidates = [item for item in xosc_files if item.name != "param.xosc"]
    if preferred is None and len(scenario_candidates) == 1:
        preferred = scenario_candidates[0]
    if not xosc_files:
        finding("error", "scenario_xosc", f"no .xosc file found in {root}")
    elif preferred is None:
        finding(
            "warning",
            "scenario_xosc_ambiguous",
            "multiple scenario .xosc files exist; fill Scenario name so it matches one filename",
        )

    stop_path = next((root / name for name in STOP_CONDITION_NAMES if (root / name).is_file()), None)
    if stop_path is None:
        finding(
            "error",
            "stop_conditions",
            "required stop_conditions.yaml or stop_condition.yaml is missing",
        )

    inferred_name = declared_name or (preferred.stem if preferred else "")
    if not inferred_name:
        finding("warning", "scenario_name", "Scenario name could not be inferred; fill it manually")
    elif not (root / f"{inferred_name}.xosc").is_file():
        finding(
            "warning",
            "scenario_name_mismatch",
            f"inferred scenario name {inferred_name!r} has no matching {inferred_name}.xosc",
        )

    map_name = str(spec.get("map_name") or "").strip()
    if not map_name:
        finding("warning", "map_name", "Map name is missing from spec.yaml; fill it manually")

    ego = spec.get("ego") if isinstance(spec.get("ego"), dict) else {}
    position = ego.get("position", ego.get("goal")) if isinstance(ego, dict) else None
    goal_config: dict[str, Any] = {}
    if isinstance(position, dict) and position.get("type") and isinstance(position.get("value"), list):
        goal_config["position"] = {
            "type": str(position["type"]),
            "value": position["value"],
        }
    else:
        finding(
            "warning",
            "goal_position",
            "ego.position could not be inferred from spec.yaml; fill goal type and values manually",
        )
    if isinstance(ego, dict) and ego.get("target_speed") is not None:
        goal_config["target_speed"] = ego["target_speed"]
    else:
        finding(
            "warning",
            "goal_target_speed",
            "ego.target_speed is missing from spec.yaml; fill it manually",
        )

    return {
        "valid": not any(item["severity"] == "error" for item in findings),
        "path": str(root),
        "spec_path": str(spec_path),
        "scenario_name": inferred_name or None,
        "map_name": map_name or None,
        "goal_config": goal_config,
        "stop_condition_config_path": stop_path.name if stop_path else None,
        "xosc_path": str(preferred) if preferred else None,
        "xosc_files": [str(item) for item in xosc_files],
        "findings": findings,
    }
