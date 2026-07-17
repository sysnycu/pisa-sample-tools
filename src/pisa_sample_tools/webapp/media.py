"""Safe, derived schematic media for normalized report runs.

The renderer deliberately produces a schematic, not a simulated camera view.  It
opens only the fixed normalized report index in read-only mode, resolves a run by
its indexed identifier, and writes cacheable artifacts below ``media/derived``.
Source experiment files are never modified.
"""

from __future__ import annotations

import bisect
import csv
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pisa_sample_tools.common.goal import load_ego_goal
from pisa_sample_tools.evidence.opendrive import load_map_geometry
from pisa_sample_tools.reporting import ReportIndex, ReportIndexError
from pisa_sample_tools.trajectory.io import (
    load_agent_geometry_for_state_file,
    load_agent_states,
)
from pisa_sample_tools.trajectory.models import AGENT_COLORS, AgentGeometry, AgentState
from pisa_sample_tools.trajectory.render import footprint_world

SCHEMATIC_DISCLAIMER = "Reconstructed schematic — not camera footage"
DERIVED_MEDIA_SCHEMA_VERSION = 1
_FORMATS = frozenset({"gif", "mp4", "webm", "png"})
_TRACE_NAMES = {
    "agent_states": frozenset({"agent_states.csv", "agent_state.csv"}),
    "agent_geometry": frozenset({"agent_geometry.csv"}),
    "collision_events": frozenset({"collision_events.csv"}),
    "scenario_events": frozenset({"scenario_events.csv"}),
}


class DerivedMediaError(ValueError):
    """A safe, user-facing derived-media error."""


class MediaCapabilityError(DerivedMediaError):
    """Raised when an optional local encoder is unavailable."""


@dataclass(frozen=True)
class DerivedMediaResult:
    report_root: Path
    run_id: str
    format: str
    media_path: Path
    metadata_path: Path
    data_sha256: str
    frame_count: int
    rendered_frame_count: int
    fps: int
    width: int
    height: int
    cached: bool

    def model_dump(self, **_kwargs: object) -> dict[str, Any]:
        return {
            "report_root": str(self.report_root),
            "run_id": self.run_id,
            "format": self.format,
            "media_path": str(self.media_path),
            "metadata_path": str(self.metadata_path),
            "data_sha256": self.data_sha256,
            "frame_count": self.frame_count,
            "rendered_frame_count": self.rendered_frame_count,
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            "cached": self.cached,
        }


@dataclass(frozen=True)
class _Scene:
    run_id: str
    outcome: str | None
    states: tuple[AgentState, ...]
    geometries: tuple[AgentGeometry, ...]
    timeline: tuple[float, ...]
    timeline_uses_time: bool
    states_by_agent: dict[str, tuple[tuple[int, AgentState], ...]]
    event_indices: frozenset[int]
    extrema_indices: frozenset[int]
    map_geometry: dict[str, Any] | None
    map_note: str | None
    follow_cursor: bool
    trail_only: bool
    render_mode: str
    ego_goal: tuple[float, float] | None
    show_ego: bool
    show_agents: bool
    actor_names: tuple[str, ...]
    show_goal: bool
    show_grid: bool
    show_axes: bool
    x_range: tuple[float, float] | None
    y_range: tuple[float, float] | None


def media_capabilities() -> dict[str, dict[str, Any]]:
    """Return cheap local capability information without invoking an encoder."""

    pillow = importlib.util.find_spec("PIL") is not None
    ffmpeg = shutil.which("ffmpeg")
    return {
        "gif": {
            "available": pillow,
            "backend": "Pillow" if pillow else None,
            "reason": None if pillow else "Pillow is not installed",
        },
        "png": {
            "available": pillow,
            "backend": "Matplotlib/Pillow" if pillow else None,
            "reason": None if pillow else "Pillow is not installed",
        },
        "mp4": {
            "available": ffmpeg is not None,
            "backend": "ffmpeg" if ffmpeg else None,
            "reason": None if ffmpeg else "ffmpeg is not installed or not on PATH",
        },
        "webm": {
            "available": ffmpeg is not None,
            "backend": "ffmpeg" if ffmpeg else None,
            "reason": None if ffmpeg else "ffmpeg is not installed or not on PATH",
        },
    }


def generate_schematic_media(
    report_root: Path,
    run_id: str,
    *,
    run_ids: list[str] | None = None,
    format: str = "gif",
    fps: int = 10,
    max_frames: int = 180,
    playback_rate: float | None = None,
    size: tuple[int, int] = (960, 540),
    overwrite: bool = False,
    include_map: bool = True,
    map_reference: bool = True,
    map_boundaries: bool = True,
    map_junctions: bool = True,
    show_bounding_boxes: bool = True,
    follow_cursor: bool = False,
    trail_only: bool = True,
    render_mode: str = "standard",
    show_ego: bool = True,
    show_agents: bool = True,
    actor_names: list[str] | None = None,
    show_goal: bool = True,
    show_grid: bool = False,
    show_axes: bool = True,
    x_range: tuple[float, float] | None = None,
    y_range: tuple[float, float] | None = None,
) -> DerivedMediaResult:
    """Generate a publication-clean schematic animation or static keyframe.

    ``report_root`` must be a normalized report bundle with the fixed index at
    ``report/index.sqlite``.  Stable output names are content-addressed by the
    indexed run data and render settings, so identical requests are cache hits.
    """

    output_format = str(format).strip().lower().lstrip(".")
    _validate_options(output_format, fps=fps, max_frames=max_frames, size=size)
    root, index_path = _validate_report(report_root)
    normalized_run_id = _validate_run_id(run_id)
    requested_run_ids = list(dict.fromkeys(
        [normalized_run_id, *(_validate_run_id(value) for value in (run_ids or []))]
    ))

    try:
        with ReportIndex(index_path) as index:
            if index.is_newer_schema:
                raise DerivedMediaError(
                    "the normalized report uses a newer schema; derived media is read-only "
                    "until this workbench is upgraded"
                )
            runs = []
            fingerprints_by_dataset: dict[str, dict[str, Any]] = {}
            for requested_run_id in requested_run_ids:
                selected_run = index.run(requested_run_id)
                if selected_run is None:
                    raise DerivedMediaError(
                        f"run is not present in the normalized report: {requested_run_id}"
                    )
                runs.append(selected_run)
                fingerprints_by_dataset[selected_run.dataset_id] = index.source_fingerprints(
                    dataset_id=selected_run.dataset_id
                )
    except ReportIndexError as exc:
        raise DerivedMediaError(f"invalid normalized report index: {exc}") from exc

    run = runs[0]
    traces: dict[str, Path] = {}
    input_paths: dict[str, Path] = {}
    states: list[AgentState] = []
    geometries: list[AgentGeometry] = []
    goal = None
    for selected_run in runs:
        selected_traces = _validated_trace_paths(
            selected_run.result_path,
            selected_run.trace_paths,
            fingerprints_by_dataset[selected_run.dataset_id],
        )
        state_path = selected_traces.get("agent_states")
        if state_path is None:
            raise DerivedMediaError(
                f"run has no indexed agent_states trace: {selected_run.run_id}"
            )
        try:
            loaded_states = load_agent_states(state_path)
        except (OSError, ValueError) as exc:
            raise DerivedMediaError(f"agent_states trace cannot be loaded: {exc}") from exc
        if not loaded_states:
            raise DerivedMediaError(
                f"agent_states trace is empty for run: {selected_run.run_id}"
            )
        prefix = selected_run.dataset_id
        states.extend(
            replace(
                state,
                agent_id=f"{prefix}::{state.agent_id}",
                entity_name=f"{prefix} · {state.entity_name or state.agent_id}",
            )
            for state in loaded_states
        )
        if goal is None:
            goal = load_ego_goal(state_path)[0]
        geometry_path = selected_traces.get("agent_geometry")
        try:
            loaded_geometries = (
                load_agent_geometry_for_state_file(state_path)
                if geometry_path is not None else []
            )
        except (OSError, ValueError) as exc:
            raise DerivedMediaError(f"agent_geometry trace cannot be loaded: {exc}") from exc
        geometries.extend(
            replace(
                geometry,
                agent_id=f"{prefix}::{geometry.agent_id}",
                entity_name=f"{prefix} · {geometry.entity_name or geometry.agent_id}",
            )
            for geometry in loaded_geometries
        )
        for name, path in selected_traces.items():
            input_paths[f"{prefix}:{name}"] = path
        if not traces:
            traces = selected_traces

    map_path, map_note = _indexed_map_path(fingerprints_by_dataset[run.dataset_id])
    map_geometry: dict[str, Any] | None = None
    if map_path is not None:
        map_geometry, warning = load_map_geometry(map_path, step_m=3.0)
        if warning:
            map_note = warning.replace(str(map_path), map_path.name)

    scene = _build_scene(
        run_id=" + ".join(item.run_id for item in runs),
        outcome="; ".join(f"{item.dataset_id}: {item.outcome or 'unknown'}" for item in runs),
        states=states,
        geometries=geometries,
        traces=traces,
        map_geometry=map_geometry,
        map_note=map_note,
    )
    if map_geometry is not None and include_map:
        map_geometry = {
            **map_geometry,
            "roads": [
                {
                    **road,
                    "reference_line": road.get("reference_line", []) if map_reference else [],
                    "boundaries": road.get("boundaries", []) if map_boundaries else [],
                }
                for road in map_geometry.get("roads", [])
                if map_junctions or not road.get("junction")
            ],
        }
    scene = replace(
        scene,
        map_geometry=map_geometry if include_map else None,
        geometries=scene.geometries if show_bounding_boxes else (),
        follow_cursor=follow_cursor,
        trail_only=trail_only,
        render_mode=render_mode,
        ego_goal=(goal.x, goal.y) if goal is not None else None,
        show_ego=show_ego,
        show_agents=show_agents,
        actor_names=tuple(actor_names or ()),
        show_goal=show_goal,
        show_grid=show_grid,
        show_axes=show_axes,
        x_range=x_range,
        y_range=y_range,
    )
    selected = (
        select_realtime_frames(scene.timeline, fps=fps, playback_rate=playback_rate, max_frames=max_frames, timeline_uses_time=scene.timeline_uses_time)
        if playback_rate is not None and output_format != "png"
        else select_schematic_frames(len(scene.timeline), max_frames, event_indices=scene.event_indices, extrema_indices=scene.extrema_indices)
    )
    if output_format == "png":
        selected = (_keyframe_index(selected, scene.event_indices),)

    if map_path is not None:
        input_paths["map_xodr"] = map_path
    semantic_run = [{
        "run_id": item.run_id, "dataset_id": item.dataset_id,
        "scenario_id": item.scenario_id, "attempt": item.attempt,
        "outcome": item.outcome, "outcome_class": item.outcome_class,
        "status": item.status, "stop_condition": item.stop_condition,
        "stop_reason": item.stop_reason, "has_collision": item.has_collision,
        "params": item.params, "metrics": item.metrics,
    } for item in runs]
    data_sha256 = _data_hash(input_paths, semantic_run)
    request = {
        "schema_version": DERIVED_MEDIA_SCHEMA_VERSION,
        "run_ids": [item.run_id for item in runs],
        "data_sha256": data_sha256,
        "format": output_format,
        "fps": fps,
        "max_frames": max_frames,
        "playback_rate": playback_rate,
        "size": [size[0], size[1]],
        "selected_frames": list(selected),
        "view": {"include_map": include_map, "map_reference": map_reference,
                 "map_boundaries": map_boundaries, "map_junctions": map_junctions,
                 "show_bounding_boxes": show_bounding_boxes,
                 "follow_cursor": follow_cursor, "trail_only": trail_only},
        "render_mode": render_mode,
        "actors": {"show_ego": show_ego, "show_agents": show_agents,
                   "actor_names": list(actor_names or ()),
                   "show_goal": show_goal, "show_grid": show_grid,
                   "show_axes": show_axes, "x_range": x_range, "y_range": y_range},
    }
    render_sha256 = hashlib.sha256(_canonical_json(request)).hexdigest()
    derived_dir = _prepare_derived_dir(root)
    safe_run = _safe_stem(run.run_id)
    stem = f"{safe_run}--schematic-{render_sha256[:16]}"
    media_path = _safe_output_path(derived_dir, f"{stem}.{output_format}")
    metadata_path = _safe_output_path(derived_dir, f"{stem}.json")

    if not overwrite and media_path.is_file() and metadata_path.is_file():
        cached_metadata = _read_json(metadata_path)
        cached_artifact_hash = cached_metadata.get("artifact_sha256")
        if (
            cached_metadata.get("render_sha256") == render_sha256
            and isinstance(cached_artifact_hash, str)
            and _file_sha256(media_path) == cached_artifact_hash
        ):
            return _result(
                root,
                run.run_id,
                output_format,
                media_path,
                metadata_path,
                data_sha256,
                scene,
                selected,
                fps,
                size,
                cached=True,
            )

    capabilities = media_capabilities()
    if not capabilities[output_format]["available"]:
        raise MediaCapabilityError(
            f"cannot create {output_format.upper()} schematic: "
            f"{capabilities[output_format]['reason']}"
        )

    _render_atomic(scene, selected, media_path, output_format, fps=fps, size=size)
    artifact_sha256 = _file_sha256(media_path)
    metadata = {
        "artifact_type": "pisa-derived-schematic-media",
        "schema_version": DERIVED_MEDIA_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "disclaimer": SCHEMATIC_DISCLAIMER,
        "run_id": run.run_id,
        "dataset_id": run.dataset_id,
        "scenario_id": run.scenario_id,
        "outcome": run.outcome,
        "format": output_format,
        "mime_type": _mime_type(output_format),
        "filename": media_path.name,
        "data_sha256": data_sha256,
        "artifact_sha256": artifact_sha256,
        "render_sha256": render_sha256,
        "render": {"fps": fps, "width": size[0], "height": size[1],
                   "playback_rate": playback_rate,
                   "include_map": include_map, "map_reference": map_reference,
                   "map_boundaries": map_boundaries, "map_junctions": map_junctions,
                   "show_bounding_boxes": show_bounding_boxes,
                   "follow_cursor": follow_cursor, "trail_only": trail_only},
        "render_mode": render_mode,
        "actors": {"show_ego": show_ego, "show_agents": show_agents,
                   "actor_names": list(actor_names or ()),
                   "show_goal": show_goal, "show_grid": show_grid,
                   "show_axes": show_axes, "x_range": x_range, "y_range": y_range},
        "frames": {
            "available": len(scene.timeline),
            "rendered": len(selected),
            "selected_indices": list(selected),
            "event_indices": sorted(scene.event_indices),
            "extrema_indices": sorted(scene.extrema_indices),
            "events_preserved": scene.event_indices.issubset(selected),
            "extrema_preserved": scene.extrema_indices.issubset(selected),
            "capped": len(selected) < len(scene.timeline),
        },
        "source": {
            "trace_names": sorted(input_paths),
            "map": map_path.name if map_path is not None else None,
            "map_note": scene.map_note,
        },
    }
    _write_json_atomic(metadata_path, metadata)
    return _result(
        root,
        run.run_id,
        output_format,
        media_path,
        metadata_path,
        data_sha256,
        scene,
        selected,
        fps,
        size,
        cached=False,
    )


def select_realtime_frames(
    timeline: tuple[float, ...], *, fps: int, playback_rate: float,
    max_frames: int, timeline_uses_time: bool,
) -> tuple[int, ...]:
    """Resample recorded states against a real-time playback clock."""

    if not timeline:
        raise DerivedMediaError("timeline must contain at least one recorded state")
    if len(timeline) == 1:
        return (0,)
    units_per_second = 1000.0 if timeline_uses_time else 1.0
    duration_seconds = (timeline[-1] - timeline[0]) / units_per_second / playback_rate
    frame_count = max(2, min(max_frames, math.ceil(duration_seconds * fps) + 1))
    selected: list[int] = []
    for frame in range(frame_count):
        elapsed = min(duration_seconds, frame / fps)
        target = timeline[0] + elapsed * playback_rate * units_per_second
        selected.append(max(0, min(len(timeline) - 1, bisect.bisect_right(timeline, target) - 1)))
    selected[-1] = len(timeline) - 1
    return tuple(selected)


def select_schematic_frames(
    frame_count: int,
    max_frames: int,
    *,
    event_indices: frozenset[int] | set[int] | tuple[int, ...] = (),
    extrema_indices: frozenset[int] | set[int] | tuple[int, ...] = (),
) -> tuple[int, ...]:
    """Select deterministic frames, prioritizing endpoints, events, and extrema.

    All important frames are retained whenever the cap has enough capacity.  If
    the important set itself exceeds the cap, endpoints are retained first,
    followed by evenly distributed event frames and then extrema frames.
    """

    if frame_count < 1:
        raise DerivedMediaError("frame_count must be at least 1")
    if max_frames < 1:
        raise DerivedMediaError("max_frames must be at least 1")
    if frame_count == 1:
        return (0,)
    if max_frames < 2:
        raise DerivedMediaError("max_frames must be at least 2 for an animation")
    if frame_count <= max_frames:
        return tuple(range(frame_count))

    events = _valid_indices(event_indices, frame_count)
    extrema = _valid_indices(extrema_indices, frame_count)
    endpoints = {0, frame_count - 1}
    important = endpoints | events | extrema
    if len(important) <= max_frames:
        selected = set(important)
        candidates = [index for index in range(frame_count) if index not in selected]
        selected.update(_uniform_pick(candidates, max_frames - len(selected)))
        return tuple(sorted(selected))

    selected = set(endpoints)
    capacity = max_frames - len(selected)
    event_candidates = sorted(events - selected)
    picked_events = _uniform_pick(event_candidates, capacity)
    selected.update(picked_events)
    capacity = max_frames - len(selected)
    extrema_candidates = sorted(extrema - selected)
    selected.update(_uniform_pick(extrema_candidates, capacity))
    return tuple(sorted(selected))


def _validate_options(
    output_format: str, *, fps: int, max_frames: int, size: tuple[int, int]
) -> None:
    if output_format not in _FORMATS:
        raise DerivedMediaError(
            f"unsupported media format {output_format!r}; choose gif, mp4, webm, or png"
        )
    if not isinstance(fps, int) or isinstance(fps, bool) or not 1 <= fps <= 60:
        raise DerivedMediaError("fps must be an integer between 1 and 60")
    if not isinstance(max_frames, int) or isinstance(max_frames, bool):
        raise DerivedMediaError("max_frames must be an integer")
    if not 2 <= max_frames <= 2_000:
        raise DerivedMediaError("max_frames must be between 2 and 2000")
    if not isinstance(size, tuple) or len(size) != 2:
        raise DerivedMediaError("size must be a (width, height) tuple")
    width, height = size
    if any(not isinstance(value, int) or isinstance(value, bool) for value in size):
        raise DerivedMediaError("size width and height must be integers")
    if not 480 <= width <= 3_840 or not 320 <= height <= 2_160:
        raise DerivedMediaError("size must be between 480x320 and 3840x2160")
    if width * height > 8_294_400:
        raise DerivedMediaError("size must not exceed 8,294,400 pixels")
    if output_format != "png" and width * height * max_frames > 100_000_000:
        raise DerivedMediaError(
            "animation render budget exceeds 100,000,000 frame-pixels; reduce size or frames"
        )


def _validate_report(report_root: Path) -> tuple[Path, Path]:
    supplied = Path(report_root).expanduser()
    if supplied.is_symlink():
        raise DerivedMediaError("report root must not be a symbolic link")
    try:
        root = supplied.resolve(strict=True)
    except OSError as exc:
        raise DerivedMediaError(f"report root does not exist: {report_root}") from exc
    if not root.is_dir():
        raise DerivedMediaError(f"report root is not a directory: {report_root}")
    index_path = root / "report" / "index.sqlite"
    if index_path.is_symlink():
        raise DerivedMediaError("normalized report index must not be a symbolic link")
    try:
        resolved_index = index_path.resolve(strict=True)
    except OSError as exc:
        raise DerivedMediaError(f"normalized report index is missing: {index_path}") from exc
    if not resolved_index.is_relative_to(root) or not resolved_index.is_file():
        raise DerivedMediaError("normalized report index must be a regular file inside the report")
    return root, resolved_index


def _validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str):
        raise DerivedMediaError("run_id must be a string")
    normalized = run_id.strip()
    if not normalized or len(normalized) > 512:
        raise DerivedMediaError("run_id must contain between 1 and 512 characters")
    if any(ord(character) < 32 or character == "\x7f" for character in normalized):
        raise DerivedMediaError("run_id must not contain control characters")
    return normalized


def _validated_trace_paths(
    result_path: Path,
    raw_paths: dict[str, Path],
    fingerprints: tuple[Any, ...],
) -> dict[str, Path]:
    recorded_results = {
        fingerprint.path.expanduser().resolve()
        for fingerprint in fingerprints
        if fingerprint.kind == "result_csv"
    }
    supplied_result = result_path.expanduser()
    if supplied_result.is_symlink():
        raise DerivedMediaError("indexed result path must not be a symbolic link")
    try:
        resolved_result = supplied_result.resolve(strict=True)
    except OSError as exc:
        raise DerivedMediaError("indexed result path no longer exists") from exc
    if resolved_result not in recorded_results or not resolved_result.is_file():
        raise DerivedMediaError("run result is not backed by normalized index provenance")
    monitor = resolved_result.parent

    traces: dict[str, Path] = {}
    for logical_name, allowed_names in _TRACE_NAMES.items():
        raw = raw_paths.get(logical_name)
        if raw is None:
            continue
        supplied = Path(raw).expanduser()
        if supplied.is_symlink():
            raise DerivedMediaError(f"indexed {logical_name} trace must not be a symbolic link")
        try:
            path = supplied.resolve(strict=True)
        except OSError as exc:
            raise DerivedMediaError(f"indexed {logical_name} trace no longer exists") from exc
        if path.parent != monitor or path.name not in allowed_names or not path.is_file():
            raise DerivedMediaError(
                f"indexed {logical_name} trace is outside its run monitor directory"
            )
        traces[logical_name] = path
    return traces


def _indexed_map_path(fingerprints: tuple[Any, ...]) -> tuple[Path | None, str | None]:
    map_sources = [
        item
        for item in fingerprints
        if item.kind.startswith("resolved_input:")
        and any(token in item.kind.lower() for token in ("map", "xodr"))
    ]
    drifted = [item for item in map_sources if item.status not in {"verified", "recorded"}]
    candidates: list[Path] = []
    for item in map_sources:
        if item.status not in {"verified", "recorded"}:
            continue
        supplied = item.path.expanduser()
        if supplied.is_symlink():
            continue
        try:
            path = supplied.resolve(strict=True)
        except OSError:
            continue
        if path.is_file() and path.suffix.lower() == ".xodr":
            candidates.append(path)
        elif path.is_dir():
            candidates.extend(
                child.resolve()
                for child in sorted(path.glob("*.xodr"))
                if child.is_file() and not child.is_symlink()
            )
    unique = tuple(sorted(set(candidates), key=str))
    if len(unique) == 1:
        return unique[0], None
    if len(unique) > 1:
        return None, "Multiple indexed OpenDRIVE maps were available; map backdrop was omitted."
    if drifted:
        return None, "Indexed map provenance is missing or drifted; map backdrop was omitted."
    return None, "No indexed OpenDRIVE map was available."


def _build_scene(
    *,
    run_id: str,
    outcome: str | None,
    states: list[AgentState],
    geometries: list[AgentGeometry],
    traces: dict[str, Path],
    map_geometry: dict[str, Any] | None,
    map_note: str | None,
) -> _Scene:
    uses_time = any(state.sim_time_ms is not None for state in states)
    step_to_time = {
        state.step_index: state.sim_time_ms
        for state in states
        if state.step_index is not None and state.sim_time_ms is not None
    }

    def value_for_state(state: AgentState) -> float:
        if uses_time and state.sim_time_ms is not None:
            return state.sim_time_ms
        if uses_time and state.step_index in step_to_time:
            return float(step_to_time[state.step_index])
        if state.step_index is not None:
            return float(state.step_index)
        raise DerivedMediaError("agent state rows need sim_time_ms or step_index for animation")

    state_values = [value_for_state(state) for state in states]
    if any(not math.isfinite(value) for value in state_values):
        raise DerivedMediaError("agent state timeline contains a non-finite value")
    timeline = tuple(sorted(set(state_values)))
    timeline_lookup = {value: index for index, value in enumerate(timeline)}
    grouped: dict[str, list[tuple[int, AgentState]]] = defaultdict(list)
    state_indices: dict[int, int] = {}
    for state, value in zip(states, state_values, strict=True):
        index = timeline_lookup[value]
        grouped[state.agent_id].append((index, state))
        state_indices[id(state)] = index
    for values in grouped.values():
        values.sort(key=lambda item: item[0])

    event_indices: set[int] = set()
    for logical_name in ("collision_events", "scenario_events"):
        path = traces.get(logical_name)
        if path is not None:
            event_indices.update(
                _event_frame_indices(
                    path,
                    timeline,
                    timeline_uses_time=uses_time,
                    step_to_time=step_to_time,
                )
            )
    extrema_indices: set[int] = set()
    for values in grouped.values():
        agent_states = [state for _, state in values]
        for attribute in ("x", "y", "speed"):
            extrema_indices.add(state_indices[id(min(agent_states, key=lambda s: getattr(s, attribute)))])
            extrema_indices.add(state_indices[id(max(agent_states, key=lambda s: getattr(s, attribute)))])
    return _Scene(
        run_id=run_id,
        outcome=outcome,
        states=tuple(states),
        geometries=tuple(geometries),
        timeline=timeline,
        timeline_uses_time=uses_time,
        states_by_agent={key: tuple(value) for key, value in grouped.items()},
        event_indices=frozenset(event_indices),
        extrema_indices=frozenset(extrema_indices),
        map_geometry=map_geometry,
        map_note=map_note,
        follow_cursor=False,
        trail_only=True,
        render_mode="standard",
        ego_goal=None,
        show_ego=True,
        show_agents=True,
        actor_names=(),
        show_goal=True,
        show_grid=False,
        show_axes=True,
        x_range=None,
        y_range=None,
    )


def _event_frame_indices(
    path: Path,
    timeline: tuple[float, ...],
    *,
    timeline_uses_time: bool,
    step_to_time: dict[int | None, float | None],
) -> set[int]:
    output: set[int] = set()
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, skipinitialspace=True)
            for raw in reader:
                row = {
                    (key or "").strip(): (value.strip() if isinstance(value, str) else value)
                    for key, value in raw.items()
                    if key is not None
                }
                if not any(value not in {None, ""} for value in row.values()):
                    continue
                time_value = _optional_number(
                    row.get("sim_time_ms") or row.get("time_ms") or row.get("timestamp_ms")
                )
                step = _optional_int(row.get("step_index") or row.get("step"))
                if timeline_uses_time:
                    value = time_value if time_value is not None else step_to_time.get(step)
                else:
                    value = float(step) if step is not None else time_value
                if value is not None and math.isfinite(value):
                    output.add(_nearest_index(timeline, value))
    except (OSError, csv.Error, UnicodeError) as exc:
        raise DerivedMediaError(f"event trace cannot be loaded: {path.name}: {exc}") from exc
    return output


def _render_atomic(
    scene: _Scene,
    selected: tuple[int, ...],
    destination: Path,
    output_format: str,
    *,
    fps: int,
    size: tuple[int, int],
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}.", suffix=f".{output_format}.rendering", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        if output_format == "gif":
            _render_gif(scene, selected, temporary, fps=fps, size=size)
        elif output_format == "png":
            image = _render_frame(scene, selected[0], size=size)
            try:
                image.save(temporary, format="PNG", optimize=True)
            finally:
                image.close()
        else:
            _render_ffmpeg(scene, selected, temporary, output_format, fps=fps, size=size)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise DerivedMediaError(f"{output_format.upper()} encoder produced an empty artifact")
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _render_gif(
    scene: _Scene,
    selected: tuple[int, ...],
    destination: Path,
    *,
    fps: int,
    size: tuple[int, int],
) -> None:
    try:
        images = [_render_frame(scene, index, size=size) for index in selected]
        first, rest = images[0], images[1:]
        first.save(
            destination,
            format="GIF",
            save_all=True,
            append_images=rest,
            duration=max(1, round(1000 / fps)),
            loop=0,
            disposal=2,
            optimize=False,
        )
    except (ImportError, OSError) as exc:
        raise MediaCapabilityError(f"Pillow could not encode GIF: {exc}") from exc
    finally:
        for image in locals().get("images", []):
            image.close()


def _render_ffmpeg(
    scene: _Scene,
    selected: tuple[int, ...],
    destination: Path,
    output_format: str,
    *,
    fps: int,
    size: tuple[int, int],
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise MediaCapabilityError(
            f"cannot create {output_format.upper()} schematic: ffmpeg is not installed or not on PATH"
        )
    with tempfile.TemporaryDirectory(prefix=".schematic-frames-", dir=destination.parent) as raw:
        frame_dir = Path(raw)
        for ordinal, index in enumerate(selected):
            image = _render_frame(scene, index, size=size)
            try:
                image.save(frame_dir / f"frame-{ordinal:06d}.png", format="PNG")
            finally:
                image.close()
        common = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-framerate",
            str(fps),
            "-i",
            str(frame_dir / "frame-%06d.png"),
        ]
        if output_format == "mp4":
            command = [
                *common,
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                str(destination),
            ]
        else:
            command = [
                *common,
                "-c:v",
                "libvpx-vp9",
                "-crf",
                "30",
                "-b:v",
                "0",
                "-pix_fmt",
                "yuv420p",
                "-f",
                "webm",
                str(destination),
            ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaCapabilityError(f"ffmpeg could not encode {output_format.upper()}: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip()[-1_000:] or "unknown encoder error"
            raise MediaCapabilityError(
                f"ffmpeg could not encode {output_format.upper()} (required codec may be missing): {detail}"
            )


def _render_frame(scene: _Scene, frame_index: int, *, size: tuple[int, int]) -> Any:
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        from matplotlib.lines import Line2D
        from matplotlib.patches import Polygon
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - dependencies are declared
        raise MediaCapabilityError(f"Matplotlib/Pillow rendering is unavailable: {exc}") from exc

    width, height = size
    dpi = 100
    figure = Figure(figsize=(width / dpi, height / dpi), dpi=dpi, facecolor="#ffffff")
    canvas = FigureCanvasAgg(figure)
    trajectory_only = scene.render_mode == "trajectory_view"
    axis = figure.add_axes((0.08, 0.10, 0.88 if trajectory_only else 0.76, 0.84 if trajectory_only else 0.73))
    if not trajectory_only:
        figure.suptitle(SCHEMATIC_DISCLAIMER, y=0.965, fontsize=14, fontweight="bold", color="#111827")
        axis.set_title(
            f"Run {scene.run_id}  ·  outcome: {scene.outcome or 'unknown'}",
            loc="left",
            fontsize=10,
            color="#475569",
            pad=10,
        )

    if scene.map_geometry:
        for road in scene.map_geometry.get("roads", []):
            for boundary in road.get("boundaries", []):
                if boundary:
                    axis.plot(
                        [point[0] for point in boundary],
                        [point[1] for point in boundary],
                        color="#cbd5e1",
                        linewidth=1.0,
                        zorder=0,
                    )
            reference = road.get("reference_line", [])
            if reference:
                axis.plot(
                    [point[0] for point in reference],
                    [point[1] for point in reference],
                    color="#e2e8f0",
                    linewidth=0.8,
                    linestyle="--",
                    zorder=0,
                )

    geometry_by_agent: dict[str, list[AgentGeometry]] = defaultdict(list)
    for geometry in scene.geometries:
        geometry_by_agent[geometry.agent_id].append(geometry)
    handles: list[Any] = []
    all_points: list[tuple[float, float]] = [] if scene.follow_cursor else [(state.x, state.y) for state in scene.states]
    if scene.map_geometry and not scene.follow_cursor:
        map_points = [
            tuple(point)
            for road in scene.map_geometry.get("roads", [])
            for line in [road.get("reference_line", []), *road.get("boundaries", [])]
            for point in line
        ]
        if map_points:
            route_x = [point[0] for point in all_points]
            route_y = [point[1] for point in all_points]
            min_x, max_x = min(route_x), max(route_x)
            min_y, max_y = min(route_y), max(route_y)
            span = max(max_x - min_x, max_y - min_y, 10.0)
            all_points.extend(
                point
                for point in map_points
                if min_x - span <= point[0] <= max_x + span
                and min_y - span <= point[1] <= max_y + span
            )

    for ordinal, agent_id in enumerate(sorted(scene.states_by_agent, key=_natural_key)):
        color = AGENT_COLORS[ordinal % len(AGENT_COLORS)]
        entries = scene.states_by_agent[agent_id]
        complete = [state for _, state in entries]
        visible = [state for index, state in entries if index <= frame_index]
        if not visible:
            continue
        current = visible[-1]
        geometry = _geometry_at(geometry_by_agent.get(agent_id, []), current)
        is_ego = current.is_ego if current.is_ego is not None else bool(geometry and geometry.is_ego)
        if (is_ego and not scene.show_ego) or (not is_ego and not scene.show_agents):
            continue
        actor_name = current.entity_name or (geometry.entity_name if geometry else None)
        if scene.actor_names and actor_name not in scene.actor_names:
            continue
        if not scene.trail_only:
            axis.plot(
                [state.x for state in complete],
                [state.y for state in complete],
                color=color,
                linewidth=1.0,
                alpha=0.35,
                zorder=1,
            )
        axis.plot(
            [state.x for state in visible],
            [state.y for state in visible],
            color=color,
            linewidth=2.2,
            alpha=0.85,
            zorder=2,
        )
        all_points.append((current.x, current.y))
        footprint = footprint_world(current, geometry)
        if footprint:
            axis.add_patch(
                Polygon(
                    footprint,
                    closed=True,
                    facecolor=color,
                    edgecolor="#ffffff",
                    linewidth=0.9,
                    alpha=0.9,
                    zorder=4,
                )
            )
            all_points.extend(footprint)
        else:
            axis.scatter([current.x], [current.y], s=48, color=color, edgecolor="white", zorder=4)
        heading_length = max(1.5, (geometry.length_m / 2 if geometry and geometry.length_m else 2.0))
        axis.plot(
            [current.x, current.x + heading_length * math.cos(current.yaw)],
            [current.y, current.y + heading_length * math.sin(current.yaw)],
            color="#0f172a",
            linewidth=1.2,
            zorder=5,
        )
        label = actor_name or f"agent {agent_id}"
        if is_ego:
            label += " (ego)"
        axis.annotate(
            label,
            (current.x, current.y),
            xytext=(5, 6),
            textcoords="offset points",
            fontsize=8,
            color="#0f172a",
            zorder=6,
        )
        handles.append(Line2D([0], [0], color=color, linewidth=3, label=label))

    if scene.show_goal and scene.ego_goal is not None:
        goal_x, goal_y = scene.ego_goal
        axis.scatter([goal_x], [goal_y], marker="D", s=90, facecolor="white", edgecolor="#111827", linewidth=2.0, zorder=7)
        axis.annotate("ego goal", (goal_x, goal_y), xytext=(6, 7), textcoords="offset points", fontsize=8, color="#111827")
        all_points.append((goal_x, goal_y))

    if not all_points:
        raise DerivedMediaError("the selected actor visibility settings leave no trajectory to render")
    xs = [point[0] for point in all_points]
    ys = [point[1] for point in all_points]
    min_x, max_x = _expanded_range(min(xs), max(xs))
    min_y, max_y = _expanded_range(min(ys), max(ys))
    axis.set_xlim(*(scene.x_range or (min_x, max_x)))
    axis.set_ylim(*(scene.y_range or (min_y, max_y)))
    axis.set_aspect("equal", adjustable="box")
    if scene.show_axes:
        axis.set_xlabel("World x (m)")
        axis.set_ylabel("World y (m)")
    else:
        axis.set_axis_off()
    if scene.show_grid:
        axis.grid(True, color="#e2e8f0", linewidth=0.7)
    else:
        axis.grid(False)
    axis.set_facecolor("#f8fafc")
    for spine in axis.spines.values():
        spine.set_color("#94a3b8")
    if handles and not trajectory_only:
        axis.legend(
            handles=handles,
            loc="upper left",
            bbox_to_anchor=(1.015, 1.0),
            borderaxespad=0,
            frameon=False,
            fontsize=8,
        )
    value = scene.timeline[frame_index]
    time_label = f"t = {value / 1000:.3f} s" if scene.timeline_uses_time else f"step = {value:g}"
    if not trajectory_only:
        figure.text(0.09, 0.055, time_label, fontsize=10, fontweight="bold", color="#334155")
    if not trajectory_only and frame_index in scene.event_indices:
        figure.text(
            0.50,
            0.055,
            "EVENT FRAME",
            ha="center",
            fontsize=9,
            fontweight="bold",
            color="#b91c1c",
        )
    if not trajectory_only:
        figure.text(
            0.99,
            0.018,
            "Derived from indexed trajectory traces",
            ha="right",
            fontsize=7,
            color="#64748b",
        )
    canvas.draw()
    image = Image.frombytes("RGBA", canvas.get_width_height(), canvas.buffer_rgba()).convert("RGB")
    figure.clear()
    return image


def _geometry_at(geometries: list[AgentGeometry], state: AgentState) -> AgentGeometry | None:
    eligible = [
        geometry
        for geometry in geometries
        if (geometry.sim_time_ms is None or state.sim_time_ms is None or geometry.sim_time_ms <= state.sim_time_ms)
        and (geometry.step_index is None or state.step_index is None or geometry.step_index <= state.step_index)
    ]
    return eligible[-1] if eligible else (geometries[0] if geometries else None)


def _prepare_derived_dir(root: Path) -> Path:
    media = root / "media"
    derived = media / "derived"
    for directory in (media, derived):
        if directory.is_symlink():
            raise DerivedMediaError(f"derived media directory must not be a symbolic link: {directory}")
        directory.mkdir(mode=0o755, exist_ok=True)
        resolved = directory.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_dir():
            raise DerivedMediaError("derived media output escaped the report root")
    return derived.resolve()


def _safe_output_path(directory: Path, filename: str) -> Path:
    if Path(filename).name != filename or not re.fullmatch(r"[A-Za-z0-9._-]+", filename):
        raise DerivedMediaError("unsafe derived media filename")
    output = directory / filename
    if output.is_symlink() or output.resolve(strict=False).parent != directory:
        raise DerivedMediaError("derived media output must be a regular path inside media/derived")
    return output


def _safe_stem(run_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", run_id).strip("-._")[:72]
    return value or "run"


def _data_hash(paths: dict[str, Path], semantic_run: Any) -> str:
    digest = hashlib.sha256()
    digest.update(b"pisa-derived-schematic-media-v1\0")
    digest.update(_canonical_json(semantic_run))
    for logical_name, path in sorted(paths.items()):
        digest.update(b"\0")
        digest.update(logical_name.encode())
        digest.update(b"\0")
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise DerivedMediaError(f"indexed source changed while hashing: {path.name}: {exc}") from exc
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}.", suffix=".json.writing", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()


def _result(
    root: Path,
    run_id: str,
    output_format: str,
    media_path: Path,
    metadata_path: Path,
    data_sha256: str,
    scene: _Scene,
    selected: tuple[int, ...],
    fps: int,
    size: tuple[int, int],
    *,
    cached: bool,
) -> DerivedMediaResult:
    return DerivedMediaResult(
        report_root=root,
        run_id=run_id,
        format=output_format,
        media_path=media_path,
        metadata_path=metadata_path,
        data_sha256=data_sha256,
        frame_count=len(scene.timeline),
        rendered_frame_count=len(selected),
        fps=fps,
        width=size[0],
        height=size[1],
        cached=cached,
    )


def _valid_indices(values: Any, frame_count: int) -> set[int]:
    output = set()
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < frame_count:
            output.add(value)
    return output


def _uniform_pick(values: list[int], count: int) -> set[int]:
    if count <= 0 or not values:
        return set()
    if count >= len(values):
        return set(values)
    if count == 1:
        return {values[len(values) // 2]}
    picked = {
        values[round(position * (len(values) - 1) / (count - 1))] for position in range(count)
    }
    if len(picked) < count:  # round-to-even can collide for very small lists
        picked.update(value for value in values if value not in picked and len(picked) < count)
    return picked


def _nearest_index(values: tuple[float, ...], target: float) -> int:
    return min(range(len(values)), key=lambda index: (abs(values[index] - target), index))


def _keyframe_index(selected: tuple[int, ...], event_indices: frozenset[int]) -> int:
    events = [index for index in selected if index in event_indices]
    return events[0] if events else selected[-1]


def _optional_number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        output = float(value)
    except (TypeError, ValueError):
        return None
    return output if math.isfinite(output) else None


def _optional_int(value: Any) -> int | None:
    number = _optional_number(value)
    return int(number) if number is not None and number.is_integer() else None


def _expanded_range(minimum: float, maximum: float) -> tuple[float, float]:
    if math.isclose(minimum, maximum):
        delta = max(5.0, abs(minimum) * 0.1)
        return minimum - delta, maximum + delta
    padding = max(2.0, (maximum - minimum) * 0.08)
    return minimum - padding, maximum + padding


def _natural_key(value: str) -> tuple[Any, ...]:
    return tuple(
        int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)
    )


def _mime_type(output_format: str) -> str:
    return {
        "gif": "image/gif",
        "png": "image/png",
        "mp4": "video/mp4",
        "webm": "video/webm",
    }[output_format]
