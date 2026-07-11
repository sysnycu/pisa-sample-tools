from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any

from pisa_sample_tools.common.sorting import natural_key

from .axes import axis_rule_for, resolve_axis_limits, series_presentation
from .ingest import read_trace_rows
from .metric_status import metric_coverage, status_points
from .models import AnalysisSpec, ConcreteComparisonGroup, EvidenceError, RunRecord
from .opendrive import discover_xodr, load_map_geometry
from .statistics import as_float, metric_value, normalized_outcome, safety_region

TRACE_EXCLUDED_FIELDS = {"step_index", "sim_time_ms", "control_type", "payload_json"}


def build_concrete_comparison_groups(
    runs: list[RunRecord], spec: AnalysisSpec
) -> tuple[list[ConcreteComparisonGroup], list[str]]:
    buckets: dict[tuple[str, str], list[RunRecord]] = defaultdict(list)
    for run in runs:
        parameter_key = _parameter_key(run.params, spec.pairing_parameter_tolerance)
        buckets[(run.logical_scenario_name, parameter_key)].append(run)
    groups: list[ConcreteComparisonGroup] = []
    warnings: list[str] = []
    for (scenario_name, parameter_key), members in sorted(buckets.items()):
        by_experiment: dict[str, list[RunRecord]] = defaultdict(list)
        for run in members:
            by_experiment[run.experiment_id].append(run)
        ambiguous = sorted(
            experiment_id
            for experiment_id, experiment_runs in by_experiment.items()
            if len(experiment_runs) > 1
        )
        if ambiguous:
            message = (
                f"{scenario_name}:{parameter_key[:12]} has duplicate runs in dataset(s): "
                + ", ".join(ambiguous)
            )
            if spec.validation_mode == "strict":
                raise EvidenceError(message)
            warnings.append(message)
        selected = [
            experiment_runs[0]
            for experiment_id, experiment_runs in sorted(by_experiment.items())
            if experiment_id not in ambiguous
        ]
        if not selected:
            continue
        sample_ids = {str(run.sample_id) for run in selected if run.sample_id is not None}
        pairing_method = (
            "sample_id"
            if len(sample_ids) == 1 and all(run.sample_id is not None for run in selected)
            else "parameters"
        )
        group_id = hashlib.sha256(f"{scenario_name}\0{parameter_key}".encode()).hexdigest()[:16]
        groups.append(
            ConcreteComparisonGroup(
                group_id=group_id,
                logical_scenario_name=scenario_name,
                parameter_key=parameter_key,
                params=dict(selected[0].params),
                pairing_method=pairing_method,
                runs=tuple(selected),
            )
        )
    return groups, warnings


def align_numeric_series(
    left: list[tuple[float, float]],
    right: list[tuple[float, float]],
    *,
    interpolation: str = "linear",
) -> list[tuple[float, float, float]]:
    if interpolation not in {"linear", "previous"}:
        raise ValueError("interpolation must be 'linear' or 'previous'")
    left = sorted(left)
    right = sorted(right)
    if len(left) < 2 or len(right) < 2:
        return []
    start = max(left[0][0], right[0][0])
    end = min(left[-1][0], right[-1][0])
    if end < start:
        return []
    step = max(_median_step(left), _median_step(right))
    if step <= 0:
        return []
    count = int(math.floor((end - start) / step)) + 1
    times = [start + index * step for index in range(count)]
    if times and end - times[-1] > step * 0.5:
        times.append(end)
    return [
        (
            time,
            _interpolate(left, time, interpolation),
            _interpolate(right, time, interpolation),
        )
        for time in times
    ]


def build_comparison_chunk(group: ConcreteComparisonGroup, spec: AnalysisSpec) -> dict[str, Any]:
    configs = [_extract_config(run, spec) for run in group.runs]
    _apply_axis_limits(configs, spec)
    group_warnings = _group_warnings(configs)
    return {
        "schema_version": 3,
        "group": {
            "group_id": group.group_id,
            "logical_scenario_name": group.logical_scenario_name,
            "parameter_key": group.parameter_key,
            "pairing_method": group.pairing_method,
            "params": group.params,
            "warnings": group_warnings,
        },
        "configs": configs,
        "timeline_s": _timeline_union(configs),
        "pairwise_trajectory": _trajectory_summaries(configs, spec),
        "pairwise_series": _series_summaries(configs, spec),
    }


def write_concrete_comparison_data(
    groups: list[ConcreteComparisonGroup],
    spec: AnalysisSpec,
    *,
    report_dir: Path,
    comparison_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    data_dir = report_dir / "comparison_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    index: list[dict[str, Any]] = []
    run_to_group: dict[str, str] = {}
    csv_rows: list[dict[str, Any]] = []
    scenario_ordinals: dict[str, int] = defaultdict(int)
    ordered_groups = sorted(
        groups,
        key=lambda group: (
            group.logical_scenario_name,
            natural_key(str(group.runs[0].sample_id or group.runs[0].scenario_id)),
            group.parameter_key,
        ),
    )
    for group in ordered_groups:
        scenario_ordinals[group.logical_scenario_name] += 1
        sample_ordinal = scenario_ordinals[group.logical_scenario_name]
        chunk = build_comparison_chunk(group, spec)
        chunk["group"]["sample_ordinal"] = sample_ordinal
        chunk_json = _json_text(chunk)
        (data_dir / f"{group.group_id}.json").write_text(chunk_json + "\n", encoding="utf-8")
        (data_dir / f"{group.group_id}.js").write_text(
            "window.PISA_COMPARISON_CHUNKS=window.PISA_COMPARISON_CHUNKS||{};"
            f"window.PISA_COMPARISON_CHUNKS[{json.dumps(group.group_id)}]={chunk_json};\n",
            encoding="utf-8",
        )
        config_summaries = [_config_index(run, spec) for run in group.runs]
        entry = {
            "group_id": group.group_id,
            "logical_scenario_name": group.logical_scenario_name,
            "parameter_key": group.parameter_key,
            "pairing_method": group.pairing_method,
            "params": group.params,
            "sample_ordinal": sample_ordinal,
            "configs": config_summaries,
            "chunk": f"comparison_data/{group.group_id}.js",
        }
        entry["search_text"] = " ".join(
            [
                group.group_id,
                group.logical_scenario_name,
                f"sample {sample_ordinal}",
                json.dumps(group.params, sort_keys=True),
                *[
                    " ".join(
                        str(value)
                        for value in (
                            config["config_id"],
                            config["label"],
                            config["sample_id"],
                            config["outcome"],
                        )
                    )
                    for config in config_summaries
                ],
            ]
        ).lower()
        index.append(entry)
        for run in group.runs:
            run_to_group[run.run_id] = group.group_id
        csv_rows.append(
            {
                "group_id": group.group_id,
                "logical_scenario_name": group.logical_scenario_name,
                "pairing_method": group.pairing_method,
                "sample_ordinal": sample_ordinal,
                "config_count": len(group.runs),
                "configs": json.dumps([run.experiment_id for run in group.runs]),
                "outcomes": json.dumps(
                    {run.experiment_id: normalized_outcome(run, spec) for run in group.runs},
                    sort_keys=True,
                ),
                "params": json.dumps(group.params, sort_keys=True),
            }
        )
    index_payload = {"schema_version": 1, "groups": index}
    index_json = _json_text(index_payload)
    (report_dir / "comparison_index.json").write_text(index_json + "\n", encoding="utf-8")
    (report_dir / "comparison_index.js").write_text(
        f"window.PISA_COMPARISON_INDEX={index_json};\n", encoding="utf-8"
    )
    _write_csv(comparison_dir / "concrete_scenarios.csv", csv_rows)
    return index, run_to_group


def _extract_config(run: RunRecord, spec: AnalysisSpec) -> dict[str, Any]:
    trajectory, trajectory_warnings = _trajectory_payload(run, spec)
    series = _series_payload(run, spec)
    goal_distance = _goal_distance_series(trajectory, run.metadata.get("ego_goal"))
    if goal_distance is not None:
        series.append(goal_distance)
    events = read_trace_rows(run.scenario_events_path)
    collisions = read_trace_rows(run.collision_events_path)
    warnings = trajectory_warnings
    xodr_path = discover_xodr(run.result_path, run.metadata)
    map_geometry = None
    map_status = "unavailable"
    if xodr_path is not None:
        map_geometry, map_warning = load_map_geometry(xodr_path)
        if map_warning:
            warnings.append(map_warning)
            map_status = "error"
        else:
            map_status = "available"
    elif run.metadata.get("xodr_path"):
        warnings.append(f"configured OpenDRIVE file is unavailable: {run.metadata['xodr_path']}")
    if run.frame_metrics_path is None:
        warnings.append("frame_metrics.csv unavailable")
    if run.control_commands_path is None:
        warnings.append("control_commands.csv unavailable")
    config = {
        **_config_index(run, spec),
        "run_id": run.run_id,
        "scenario_id": run.scenario_id,
        "status": run.status,
        "termination_reason": run.termination_reason,
        "stop_reason": run.stop_reason,
        "params": run.params,
        "metrics": run.metrics,
        "canonical_metrics": {name: metric_value(run, spec, name) for name in spec.metrics},
        "ego_goal": run.metadata.get("ego_goal"),
        "map": {
            "status": map_status,
            "name": run.metadata.get("map_name"),
            "source": xodr_path.name if xodr_path else None,
            "geometry": map_geometry,
        },
        "trajectory": trajectory,
        "series": series,
        "events": events,
        "collisions": collisions,
        "warnings": warnings,
    }
    config["timeline_s"] = _timeline_union([config])
    return config


def _goal_distance_series(trajectory: list[dict[str, Any]], goal: Any) -> dict[str, Any] | None:
    if not isinstance(goal, dict):
        return None
    goal_x, goal_y = as_float(goal.get("x")), as_float(goal.get("y"))
    ego = next((actor for actor in trajectory if actor.get("is_ego")), None)
    if goal_x is None or goal_y is None or ego is None:
        return None
    points = [
        [float(point[0]), math.hypot(float(point[1]) - goal_x, float(point[2]) - goal_y)]
        for point in ego.get("points", [])
    ]
    if not points:
        return None
    return {
        "source": "metrics",
        "semantic_name": "ego.distance_to_goal",
        "field": "ego.distance_to_goal_m",
        "label": "Distance to ego goal",
        "unit": "m",
        "interpolation": "linear",
        "coverage": {
            "total": len(points),
            "valid": len(points),
            "not_applicable": 0,
            "status_counts": {},
            "invalid": 0,
            "missing": 0,
            "valid_field": None,
            "status_field": None,
        },
        "statuses": [],
        "points": points,
    }


def _timeline_union(configs: list[dict[str, Any]]) -> list[float]:
    timestamps: set[float] = set()
    for config in configs:
        for actor in config.get("trajectory", []):
            timestamps.update(round(float(point[0]), 9) for point in actor.get("points", []))
        for item in config.get("series", []):
            timestamps.update(round(float(point[0]), 9) for point in item.get("points", []))
        for event in [*config.get("events", []), *config.get("collisions", [])]:
            time_ms = as_float(event.get("sim_time_ms"))
            if time_ms is not None:
                timestamps.add(round(time_ms / 1000.0, 9))
    return sorted(timestamps)


def _config_index(run: RunRecord, spec: AnalysisSpec) -> dict[str, Any]:
    metadata = run.metadata
    labels = [
        metadata.get("av_name"),
        metadata.get("simulator_name"),
        metadata.get("sampler_name"),
    ]
    label = " / ".join(str(value) for value in labels if value not in {None, ""})
    if not label:
        label = run.experiment_id
    if metadata.get("repeat_id") not in {None, ""}:
        label += f" / repeat {metadata['repeat_id']}"
    return {
        "config_id": run.experiment_id,
        "label": label,
        "sample_id": run.sample_id,
        "outcome": normalized_outcome(run, spec),
        "safety_region": safety_region(run, spec),
        "av_name": metadata.get("av_name"),
        "simulator_name": metadata.get("simulator_name"),
        "sampler_name": metadata.get("sampler_name"),
        "repeat_id": metadata.get("repeat_id"),
        "map_name": metadata.get("map_name"),
        "has_trajectory": run.agent_states_path is not None,
        "has_metrics": run.frame_metrics_path is not None,
        "has_controls": run.control_commands_path is not None,
    }


def _trajectory_payload(
    run: RunRecord, spec: AnalysisSpec
) -> tuple[list[dict[str, Any]], list[str]]:
    rows = read_trace_rows(run.agent_states_path)
    grouped: dict[str, list[list[float | None]]] = defaultdict(list)
    identities: dict[str, dict[str, Any]] = {}
    geometry_rows = read_trace_rows(run.agent_geometry_path)
    geometry_by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for geometry in geometry_rows:
        geometry_id = geometry.get("agent_id") or geometry.get("actor_id")
        if geometry_id not in {None, ""}:
            geometry_by_agent[str(geometry_id)].append(_geometry_payload(geometry))
    warnings: list[str] = []
    missing_time = False
    for row in rows:
        time_ms = as_float(row.get("sim_time_ms"))
        step = as_float(row.get("step_index"))
        if time_ms is None:
            missing_time = True
            time_ms = step
        agent_id = row.get("agent_id") or row.get("actor_id")
        x, y = as_float(row.get("x")), as_float(row.get("y"))
        if agent_id in {None, ""} or time_ms is None or x is None or y is None:
            continue
        grouped[str(agent_id)].append(
            [
                time_ms / 1000.0,
                x,
                y,
                as_float(row.get("z")),
                as_float(row.get("speed") or row.get("speed_mps")),
                as_float(row.get("yaw")),
            ]
        )
        identities[str(agent_id)] = {
            "entity_name": row.get("entity_name") or row.get("agent_name"),
            "sim_tracking_id": row.get("sim_tracking_id"),
            "is_ego": _as_bool(row.get("is_ego")),
        }
    if missing_time:
        warnings.append("agent state rows missing sim_time_ms; step_index fallback used")
    ego_id = str(run.metadata.get("ego_agent_id") or "0")
    actors = []
    for agent_id, points in sorted(grouped.items()):
        points.sort(key=lambda point: float(point[0] or 0))
        identity = identities.get(agent_id, {})
        geometries = geometry_by_agent.get(agent_id, [])
        if geometries:
            identity = {
                **{
                    key: geometries[0].get(key)
                    for key in ("entity_name", "sim_tracking_id", "is_ego")
                },
                **{key: value for key, value in identity.items() if value not in {None, ""}},
            }
        actors.append(
            {
                "agent_id": agent_id,
                "entity_name": identity.get("entity_name"),
                "sim_tracking_id": identity.get("sim_tracking_id"),
                "is_ego": identity.get("is_ego")
                if identity.get("is_ego") is not None
                else agent_id == ego_id,
                "geometry": geometries,
                "points": _downsample_even(points, spec.comparison_detail.max_points_per_series),
            }
        )
    return actors, warnings


def _geometry_payload(row: dict[str, str]) -> dict[str, Any]:
    return {
        "step_index": as_float(row.get("step_index")),
        "sim_time_ms": as_float(row.get("sim_time_ms")),
        "entity_name": row.get("entity_name") or row.get("agent_name"),
        "sim_tracking_id": row.get("sim_tracking_id"),
        "is_ego": _as_bool(row.get("is_ego")),
        "shape_type": row.get("shape_type"),
        "length_m": as_float(row.get("length_m")),
        "width_m": as_float(row.get("width_m")),
        "height_m": as_float(row.get("height_m")),
        "reference_point": row.get("reference_point"),
        "center_offset_x": as_float(row.get("center_offset_x")) or 0.0,
        "center_offset_y": as_float(row.get("center_offset_y")) or 0.0,
        "center_offset_z": as_float(row.get("center_offset_z")) or 0.0,
        "yaw_offset": as_float(row.get("yaw_offset")) or 0.0,
        "footprint_json": row.get("footprint_json") or None,
        "source": row.get("source"),
    }


def _as_bool(value: Any) -> bool | None:
    if value in {None, ""}:
        return None
    if str(value).strip().lower() in {"true", "1", "yes"}:
        return True
    if str(value).strip().lower() in {"false", "0", "no"}:
        return False
    return None


def _series_payload(run: RunRecord, spec: AnalysisSpec) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    frame_rows = read_trace_rows(run.frame_metrics_path)
    definitions: list[tuple[str, str, str | None, str | None]] = []
    for metric_name, binding in spec.metrics.items():
        if binding.series:
            definitions.append((metric_name, binding.series, binding.label, binding.unit))
    definitions.extend(
        [
            ("ego.speed", "ego.speed", None, None),
            ("ego.acceleration", "ego.acceleration", None, None),
        ]
    )
    seen: set[tuple[str, str]] = set()
    for semantic_name, field, configured_label, configured_unit in definitions:
        key = ("metrics", field)
        if key in seen:
            continue
        seen.add(key)
        points = _numeric_points(frame_rows, field)
        if points:
            label, unit = series_presentation(
                field,
                semantic_name=semantic_name,
                configured_label=configured_label,
                configured_unit=configured_unit,
            )
            output.append(
                {
                    "source": "metrics",
                    "semantic_name": semantic_name,
                    "field": field,
                    "label": label,
                    "unit": unit,
                    "interpolation": "linear",
                    "coverage": metric_coverage(frame_rows, field),
                    "statuses": status_points(frame_rows, field),
                    "points": _downsample_minmax(
                        points, spec.comparison_detail.max_points_per_series
                    ),
                }
            )
    control_rows = read_trace_rows(run.control_commands_path)
    if control_rows:
        fields = [
            field
            for field in control_rows[0]
            if field not in TRACE_EXCLUDED_FIELDS
            and any(as_float(row.get(field)) is not None for row in control_rows)
        ]
        for field in fields:
            points = _numeric_points(control_rows, field)
            label, unit = series_presentation(field)
            output.append(
                {
                    "source": "controls",
                    "semantic_name": field,
                    "field": field,
                    "label": label,
                    "unit": unit,
                    "interpolation": "previous",
                    "points": _downsample_minmax(
                        points, spec.comparison_detail.max_points_per_series
                    ),
                }
            )
    return output


def _apply_axis_limits(configs: list[dict[str, Any]], spec: AnalysisSpec) -> None:
    shared: dict[tuple[str, str], list[float]] = defaultdict(list)
    for config in configs:
        for item in config["series"]:
            shared[(item["source"], item["field"])].extend(
                float(point[1]) for point in item["points"]
            )
    for config in configs:
        for item in config["series"]:
            rule = axis_rule_for(spec, item["field"], item["semantic_name"])
            own_values = [float(point[1]) for point in item["points"]]
            semantic_values = shared[(item["source"], item["field"])]
            item["semantic_limits"] = resolve_axis_limits(semantic_values, rule).as_dict()
            item["detail_limits"] = resolve_axis_limits(own_values, rule, detail=True).as_dict()


def _trajectory_summaries(
    configs: list[dict[str, Any]], spec: AnalysisSpec
) -> list[dict[str, Any]]:
    rows = []
    for left, right in combinations(configs, 2):
        left_actors = {item["agent_id"]: item for item in left["trajectory"]}
        right_actors = {item["agent_id"]: item for item in right["trajectory"]}
        for agent_id in sorted(set(left_actors) & set(right_actors)):
            left_points = left_actors[agent_id]["points"]
            right_points = right_actors[agent_id]["points"]
            x_values = align_numeric_series(
                [(float(p[0]), float(p[1])) for p in left_points],
                [(float(p[0]), float(p[1])) for p in right_points],
            )
            y_values = align_numeric_series(
                [(float(p[0]), float(p[2])) for p in left_points],
                [(float(p[0]), float(p[2])) for p in right_points],
            )
            if not x_values or len(x_values) != len(y_values):
                continue
            distances = [
                math.hypot(x[2] - x[1], y[2] - y[1])
                for x, y in zip(x_values, y_values, strict=True)
            ]
            first_divergence = next(
                (
                    x_values[index][0]
                    for index, distance in enumerate(distances)
                    if distance >= spec.comparison_detail.trajectory_divergence_m
                ),
                None,
            )
            rows.append(
                {
                    "left_config": left["config_id"],
                    "right_config": right["config_id"],
                    "agent_id": agent_id,
                    "compared_points": len(distances),
                    "overlap_start_s": x_values[0][0],
                    "overlap_end_s": x_values[-1][0],
                    "ade": statistics.fmean(distances),
                    "fde": distances[-1],
                    "rmse": math.sqrt(statistics.fmean(value * value for value in distances)),
                    "max_error": max(distances),
                    "first_divergence_s": first_divergence,
                }
            )
    return rows


def _series_summaries(configs: list[dict[str, Any]], spec: AnalysisSpec) -> list[dict[str, Any]]:
    rows = []
    for left, right in combinations(configs, 2):
        left_series = {(item["source"], item["field"]): item for item in left["series"]}
        right_series = {(item["source"], item["field"]): item for item in right["series"]}
        for key in sorted(set(left_series) & set(right_series)):
            left_item, right_item = left_series[key], right_series[key]
            aligned = align_numeric_series(
                [(float(p[0]), float(p[1])) for p in left_item["points"]],
                [(float(p[0]), float(p[1])) for p in right_item["points"]],
                interpolation=left_item["interpolation"],
            )
            if not aligned:
                continue
            deltas = [right_value - left_value for _, left_value, right_value in aligned]
            absolute = [abs(value) for value in deltas]
            tolerance = spec.comparison_detail.tolerances.get(
                left_item["semantic_name"],
                spec.comparison_detail.tolerances.get(left_item["field"], 0.0),
            )
            first_divergence = (
                next(
                    (
                        aligned[index][0]
                        for index, value in enumerate(absolute)
                        if value >= tolerance
                    ),
                    None,
                )
                if tolerance > 0
                else None
            )
            step = _median_step([(time, value) for time, value, _ in aligned])
            rows.append(
                {
                    "left_config": left["config_id"],
                    "right_config": right["config_id"],
                    "source": key[0],
                    "field": key[1],
                    "label": left_item["label"],
                    "unit": left_item["unit"],
                    "compared_points": len(aligned),
                    "overlap_start_s": aligned[0][0],
                    "overlap_end_s": aligned[-1][0],
                    "mean_delta": statistics.fmean(deltas),
                    "median_delta": statistics.median(deltas),
                    "max_abs_delta": max(absolute),
                    "integral_abs_delta": sum(absolute) * step,
                    "tolerance": tolerance,
                    "first_divergence_s": first_divergence,
                }
            )
    return rows


def _numeric_points(rows: list[dict[str, str]], field: str) -> list[list[float]]:
    points = []
    for row in rows:
        time = as_float(row.get("sim_time_ms"))
        value = as_float(row.get(field))
        if time is not None and value is not None:
            points.append([time / 1000.0, value])
    return points


def _downsample_even(points: list[list[Any]], limit: int) -> list[list[Any]]:
    if len(points) <= limit:
        return points
    indexes = {round(index * (len(points) - 1) / (limit - 1)) for index in range(limit)}
    return [points[index] for index in sorted(indexes)]


def _downsample_minmax(points: list[list[float]], limit: int) -> list[list[float]]:
    if len(points) <= limit:
        return points
    bucket_count = max(1, (limit - 2) // 2)
    sampled = [points[0]]
    interior = points[1:-1]
    for bucket in range(bucket_count):
        start = math.floor(bucket * len(interior) / bucket_count)
        end = math.floor((bucket + 1) * len(interior) / bucket_count)
        values = interior[start:end]
        if not values:
            continue
        extremes = {
            min(range(len(values)), key=lambda i: values[i][1]),
            max(range(len(values)), key=lambda i: values[i][1]),
        }
        sampled.extend(values[index] for index in sorted(extremes, key=lambda i: values[i][0]))
    sampled.append(points[-1])
    return sampled[:limit]


def _median_step(points: list[tuple[float, float]]) -> float:
    deltas = [
        right[0] - left[0]
        for left, right in zip(points, points[1:], strict=False)
        if right[0] > left[0]
    ]
    return statistics.median(deltas) if deltas else 0.0


def _interpolate(points: list[tuple[float, float]], time: float, interpolation: str) -> float:
    if time <= points[0][0]:
        return points[0][1]
    for index in range(1, len(points)):
        right_time, right_value = points[index]
        if time > right_time:
            continue
        left_time, left_value = points[index - 1]
        if math.isclose(time, right_time):
            return right_value
        if interpolation == "previous" or math.isclose(left_time, right_time):
            return left_value
        fraction = (time - left_time) / (right_time - left_time)
        return left_value + fraction * (right_value - left_value)
    return points[-1][1]


def _parameter_key(params: dict[str, Any], tolerance: float) -> str:
    normalized = []
    for name, value in sorted(params.items()):
        number = as_float(value)
        normalized_value: Any = (
            round(number / tolerance) if number is not None and tolerance > 0 else value
        )
        normalized.append((name, normalized_value))
    payload = json.dumps(normalized, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _json_text(value: Any) -> str:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")


def _group_warnings(configs: list[dict[str, Any]]) -> list[str]:
    warnings = []
    maps = {config.get("map_name") for config in configs if config.get("map_name")}
    if len(maps) > 1:
        warnings.append("configs report different map_name values; XY overlay may be invalid")
    map_hashes = {
        geometry.get("sha256")
        for config in configs
        if isinstance((geometry := config.get("map", {}).get("geometry")), dict)
        and geometry.get("sha256")
    }
    if len(map_hashes) > 1:
        warnings.append(
            "configs use different OpenDRIVE geometry; the baseline map is shown"
        )
    actor_sets = [{actor["agent_id"] for actor in config["trajectory"]} for config in configs]
    if actor_sets and any(actor_set != actor_sets[0] for actor_set in actor_sets[1:]):
        warnings.append(
            "actor sets differ across configs; only common actors are enabled by default"
        )
    if len(configs) > 10:
        warnings.append(
            "more than ten configs are available; select a subset for readable overlays"
        )
    return warnings


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
