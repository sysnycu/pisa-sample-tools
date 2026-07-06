from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from simcore.utils.position import PositionFactory


@dataclass(frozen=True)
class EgoGoal:
    x: float
    y: float
    z: float = 0.0
    heading: float | None = None
    target_speed: float | None = None
    source_type: str = "WorldPosition"

    def as_dict(self) -> dict[str, Any]:
        return {
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "heading": self.heading,
            "target_speed": self.target_speed,
            "source_type": self.source_type,
        }


def load_ego_goal(start: Path) -> tuple[EgoGoal | None, str | None]:
    manifest_path = find_execution_manifest(start)
    if manifest_path is not None:
        try:
            manifest = _load_mapping(manifest_path)
            manifest_goal = _goal_from_manifest(manifest.get("ego_goal"))
            if manifest_goal is not None:
                return manifest_goal, None
        except (OSError, ValueError, TypeError, json.JSONDecodeError, yaml.YAMLError) as exc:
            return None, f"could not read ego goal from {manifest_path}: {exc}"
    spec_path = find_runner_spec(start)
    if spec_path is None:
        return None, None
    try:
        spec = _load_mapping(spec_path)
        goal_config = spec.get("scenario", {}).get("goal_config")
        if not isinstance(goal_config, dict):
            return None, None
        return _parse_goal(goal_config, spec, spec_path), None
    except (
        OSError,
        ValueError,
        RuntimeError,
        TypeError,
        json.JSONDecodeError,
        yaml.YAMLError,
    ) as exc:
        return None, f"could not resolve ego goal from {spec_path}: {exc}"


def find_runner_spec(start: Path) -> Path | None:
    current = start if start.is_dir() else start.parent
    for directory in (current, *current.parents):
        for name in ("runner_spec.json", "runner_spec.yaml", "runner_spec.yml"):
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def find_execution_manifest(start: Path) -> Path | None:
    current = start if start.is_dir() else start.parent
    for directory in (current, *current.parents):
        for name in (
            "execution_manifest.yaml",
            "execution_manifest.yml",
            "execution_manifest.json",
        ):
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def _load_mapping(path: Path) -> dict[str, Any]:
    data = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.suffix == ".json"
        else yaml.safe_load(path.read_text(encoding="utf-8"))
    )
    if not isinstance(data, dict):
        raise ValueError("runner spec must contain a mapping")
    return data


def _parse_goal(config: dict[str, Any], spec: dict[str, Any], spec_path: Path) -> EgoGoal:
    position = (
        config.get("resolved_world_position")
        or config.get("resolved_position")
        or config.get("position")
    )
    if not isinstance(position, dict):
        raise ValueError("scenario.goal_config.position is missing")
    position_type = str(position.get("type") or "WorldPosition")
    value = position.get("value", position)
    target_speed = _optional_float(config.get("target_speed"))
    if position_type.lower() in {"worldposition", "world", "cartesian"}:
        x, y, z, heading = _world_values(value)
        return EgoGoal(x, y, z, heading, target_speed, position_type)
    if position_type.lower() != "laneposition":
        raise ValueError(f"unsupported goal position type {position_type!r}")
    road_id, lane_id, s, offset = _lane_values(value)
    xodr = _resolve_xodr(spec, spec_path)
    library = _resolve_rmlib(spec, spec_path)
    with PositionFactory(library, xodr) as factory:
        resolved = factory.from_lane(road_id, lane_id, s, offset)
    return EgoGoal(resolved.x, resolved.y, resolved.z, resolved.h, target_speed, position_type)


def _goal_from_manifest(raw: Any) -> EgoGoal | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("execution manifest ego_goal must be a mapping")
    world = raw.get("world")
    if not isinstance(world, dict):
        raise ValueError("execution manifest ego_goal.world must be a mapping")
    return EgoGoal(
        x=float(world["x_m"]),
        y=float(world["y_m"]),
        z=float(world.get("z_m", 0.0)),
        heading=_optional_float(world.get("heading_rad")),
        target_speed=_optional_float(raw.get("target_speed_mps")),
        source_type=str(raw.get("source_type") or "resolved"),
    )


def _world_values(value: Any) -> tuple[float, float, float, float | None]:
    if isinstance(value, dict):
        return (
            float(value["x"]),
            float(value["y"]),
            float(value.get("z", 0)),
            _optional_float(value.get("h", value.get("heading"))),
        )
    if isinstance(value, list) and len(value) >= 2:
        return (
            float(value[0]),
            float(value[1]),
            float(value[2] or 0) if len(value) > 2 else 0.0,
            _optional_float(value[3]) if len(value) > 3 else None,
        )
    raise ValueError("WorldPosition value must contain x and y")


def _lane_values(value: Any) -> tuple[int, int, float, float]:
    if isinstance(value, dict):
        return (
            int(value["road_id"]),
            int(value["lane_id"]),
            float(value["s"]),
            float(value.get("offset", 0)),
        )
    if isinstance(value, list) and len(value) >= 3:
        return (
            int(value[0]),
            int(value[1]),
            float(value[2]),
            float(value[3] or 0) if len(value) > 3 else 0.0,
        )
    raise ValueError("LanePosition value must contain road_id, lane_id, and s")


def _resolve_xodr(spec: dict[str, Any], spec_path: Path) -> Path:
    raw = spec.get("map", {}).get("xodr_path")
    map_name = str(spec.get("map", {}).get("name") or "")
    candidate = _xodr_file(Path(raw)) if raw else None
    if candidate:
        return candidate
    for ancestor in spec_path.parents:
        candidate = _xodr_file(ancestor / "map" / map_name / "xodr")
        if candidate:
            return candidate
    raise ValueError(f"OpenDRIVE file for map {map_name!r} is unavailable")


def _resolve_rmlib(spec: dict[str, Any], spec_path: Path) -> Path:
    raw = spec.get("scenario", {}).get("rmlib_path")
    if raw and Path(raw).is_file():
        return Path(raw)
    for ancestor in spec_path.parents:
        candidate = ancestor / "lib" / "libesminiRMLib.so"
        if candidate.is_file():
            return candidate
    raise ValueError("libesminiRMLib.so is unavailable")


def _xodr_file(path: Path) -> Path | None:
    if path.is_file() and path.suffix.lower() == ".xodr":
        return path
    if not path.is_dir():
        return None
    files = sorted(path.glob("*.xodr"))
    preferred = path / f"{path.parent.name}.xodr"
    if preferred in files:
        return preferred
    return files[0] if len(files) == 1 else None


def _optional_float(value: Any) -> float | None:
    return None if value in {None, ""} else float(value)
