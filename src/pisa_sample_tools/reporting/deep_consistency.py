from __future__ import annotations

import csv
import fcntl
import hashlib
import itertools
import json
import math
import os
import shutil
import sqlite3
import statistics
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from pisa_sample_tools.trajectory import AgentState, load_agent_states
from pisa_sample_tools.trajectory_compare.metrics import compare_states, states_by_agent

from .consistency import build_quick_consistency

DEEP_CONSISTENCY_ANALYZER_VERSION = 1
DEFAULT_POSITION_TOLERANCES_M = (0.001, 0.01, 0.1)
DEEP_PROFILES = frozenset({"trajectory_outlier_controls", "full_controls"})

ProgressCallback = Callable[[str, float, float, str, int, int], None]
CancelCallback = Callable[[], None]
TraceResolver = Callable[[Path], Path]


class DeepConsistencyError(ValueError):
    """Raised for safe, user-facing deep consistency failures."""


def deep_consistency_cache_key(
    source_fingerprint: str,
    *,
    profile: str,
    position_tolerances_m: Iterable[float],
    outlier_limit: int,
) -> str:
    payload = {
        "analyzer_version": DEEP_CONSISTENCY_ANALYZER_VERSION,
        "source_fingerprint": source_fingerprint,
        "profile": profile,
        "position_tolerances_m": list(position_tolerances_m),
        "outlier_limit": outlier_limit,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]


def deep_consistency_status(
    report_root: Path,
    *,
    profile: str = "trajectory_outlier_controls",
    position_tolerances_m: Iterable[float] = DEFAULT_POSITION_TOLERANCES_M,
    outlier_limit: int = 25,
) -> dict[str, Any]:
    root = report_root.expanduser().resolve()
    source_fingerprint = _source_fingerprint(root)
    key = deep_consistency_cache_key(
        source_fingerprint,
        profile=profile,
        position_tolerances_m=position_tolerances_m,
        outlier_limit=outlier_limit,
    )
    directory = root / "consistency" / "derived" / key
    manifest = _read_json(directory / "manifest.json")
    summary = _read_json(directory / "summary.json")
    ready = bool(
        manifest.get("complete") is True
        and manifest.get("source_fingerprint") == source_fingerprint
        and summary
    )
    return {
        "state": "ready" if ready else "not_generated",
        "cache_key": key,
        "source_fingerprint": source_fingerprint,
        "profile": profile,
        "position_tolerances_m": list(position_tolerances_m),
        "outlier_limit": outlier_limit,
        "generated_at": manifest.get("generated_at") if ready else None,
        "analyzer_version": DEEP_CONSISTENCY_ANALYZER_VERSION,
        "summary": summary if ready else None,
        "artifacts": manifest.get("artifacts", []) if ready else [],
    }


def analyze_deep_consistency(
    report_root: Path,
    *,
    profile: str = "trajectory_outlier_controls",
    position_tolerances_m: Iterable[float] = DEFAULT_POSITION_TOLERANCES_M,
    outlier_limit: int = 25,
    force: bool = False,
    progress: ProgressCallback | None = None,
    check_cancelled: CancelCallback | None = None,
    resolve_trace: TraceResolver | None = None,
) -> dict[str, Any]:
    if profile not in DEEP_PROFILES:
        raise DeepConsistencyError(f"unsupported deep consistency profile: {profile}")
    tolerances = tuple(sorted({float(value) for value in position_tolerances_m}))
    if not tolerances or any(not math.isfinite(value) or value < 0 for value in tolerances):
        raise DeepConsistencyError("position tolerances must be finite non-negative numbers")
    if not 1 <= outlier_limit <= 1_000:
        raise DeepConsistencyError("outlier_limit must be between 1 and 1000")
    notify = progress or (
        lambda _phase, _current, _total, _message, _stage, _stages: None
    )
    cancel = check_cancelled or (lambda: None)
    root = report_root.expanduser().resolve()
    index_path = root / "report" / "index.sqlite"
    if not index_path.is_file():
        raise DeepConsistencyError("normalized report index is required")
    quick = _load_quick(root, index_path)
    if not quick.get("available"):
        raise DeepConsistencyError("no compatible replicate group is available")
    source_fingerprint = _source_fingerprint(root)
    key = deep_consistency_cache_key(
        source_fingerprint,
        profile=profile,
        position_tolerances_m=tolerances,
        outlier_limit=outlier_limit,
    )
    target = root / "consistency" / "derived" / key
    cached = deep_consistency_status(
        root,
        profile=profile,
        position_tolerances_m=tolerances,
        outlier_limit=outlier_limit,
    )
    if cached["state"] == "ready" and not force:
        return {**cached, "cached": True, "output_dir": str(target)}

    rows_by_group = _indexed_group_rows(index_path, quick)
    sample_total = sum(len(rows) for rows in rows_by_group.values())
    file_total = sum(
        len(sample_rows)
        for group_rows in rows_by_group.values()
        for sample_rows in group_rows.values()
    )
    notify(
        "inventory",
        file_total,
        file_total,
        f"Planned {sample_total} samples across {file_total} trajectory files",
        1,
        5,
    )
    cancel()

    loaded = 0
    compared = 0
    all_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for group in quick["groups"]:
        group_id = str(group["id"])
        datasets = tuple(str(value) for value in group["datasets"])
        for parameter_hash, indexed_rows in rows_by_group[group_id].items():
            states: dict[str, list[AgentState]] = {}
            for dataset in datasets:
                path = _trace_path(indexed_rows[dataset], "agent_states")
                if path is not None and resolve_trace is not None:
                    path = resolve_trace(path)
                if path is None or not path.is_file():
                    raise DeepConsistencyError(
                        f"agent state trace is unavailable for {dataset}:{parameter_hash}"
                    )
                states[dataset] = _semantic_agent_ids(load_agent_states(path))
                loaded += 1
                if loaded == file_total or loaded % 25 == 0:
                    notify(
                        "loading_trajectories",
                        loaded,
                        file_total,
                        f"Loaded {loaded} / {file_total} trajectory files",
                        2,
                        5,
                    )
                cancel()
            record = _compare_sample(
                parameter_hash,
                datasets,
                indexed_rows,
                states,
                tolerances,
            )
            all_records[group_id].append(record)
            compared += 1
            if compared == sample_total or compared % 10 == 0:
                notify(
                    "comparing_samples",
                    compared,
                    sample_total,
                    f"Compared {compared} / {sample_total} parameter samples",
                    3,
                    5,
                )
            cancel()

    selected = _select_control_samples(all_records, outlier_limit, profile)
    control_total = sum(len(item["datasets"]) for item in selected)
    control_done = 0
    control_results: dict[tuple[str, str], dict[str, Any]] = {}
    for item in selected:
        traces: dict[str, dict[float, dict[str, float]]] = {}
        group_rows = rows_by_group[item["group_id"]][item["parameter_hash"]]
        for dataset in item["datasets"]:
            path = _trace_path(group_rows[dataset], "control_commands")
            if path is not None and resolve_trace is not None:
                path = resolve_trace(path)
            traces[dataset] = _load_controls(path) if path and path.is_file() else {}
            control_done += 1
            if control_done == control_total or control_done % 10 == 0:
                notify(
                    "diagnosing_controls",
                    control_done,
                    max(1, control_total),
                    f"Inspected {control_done} / {control_total} control files",
                    4,
                    5,
                )
            cancel()
        control_results[(item["group_id"], item["parameter_hash"])] = _compare_controls(
            traces
        )

    generated_at = datetime.now(UTC).isoformat()
    group_summaries = [
        _deep_group_summary(group, all_records[str(group["id"])], tolerances)
        for group in quick["groups"]
    ]
    for records in all_records.values():
        for record in records:
            record["control"] = control_results.get(
                (record["group_id"], record["parameter_hash"])
            )
    summary = {
        "schema_version": 1,
        "analyzer_version": DEEP_CONSISTENCY_ANALYZER_VERSION,
        "generated_at": generated_at,
        "source_fingerprint": source_fingerprint,
        "profile": profile,
        "position_tolerances_m": list(tolerances),
        "outlier_limit": outlier_limit,
        "group_count": len(group_summaries),
        "sample_count": sample_total,
        "groups": group_summaries,
        "alignment_rule": "simulation-time interpolation without extrapolation",
        "strict_rule": "recorded step/time/position/speed/yaw equality; tracking IDs ignored",
        "control_rule": "exact common recorded timestamps; incompatible command fields omitted",
    }
    notify(
        "writing_artifacts",
        0,
        4,
        "Writing versioned consistency artifacts",
        5,
        5,
    )
    artifact_paths = _publish_artifacts(
        root,
        key,
        summary,
        all_records,
        source_fingerprint=source_fingerprint,
        generated_at=generated_at,
        force=force,
        progress=lambda current, message: notify(
            "writing_artifacts", current, 4, message, 5, 5
        ),
    )
    return {
        "state": "ready",
        "cached": False,
        "cache_key": key,
        "source_fingerprint": source_fingerprint,
        "profile": profile,
        "generated_at": generated_at,
        "analyzer_version": DEEP_CONSISTENCY_ANALYZER_VERSION,
        "summary": summary,
        "artifacts": artifact_paths,
        "output_dir": str(target),
    }


def _compare_sample(
    parameter_hash: str,
    datasets: tuple[str, ...],
    rows: dict[str, sqlite3.Row],
    states: dict[str, list[AgentState]],
    tolerances: tuple[float, ...],
) -> dict[str, Any]:
    grouped = {dataset: states_by_agent(values) for dataset, values in states.items()}
    actor_sets = [set(value) for value in grouped.values()]
    common_agents = set.intersection(*actor_sets)
    actor_sets_equal = bool(common_agents) and all(
        value == actor_sets[0] for value in actor_sets[1:]
    )
    lengths_equal = actor_sets_equal and all(
        len(grouped[datasets[0]][agent])
        == len(grouped[dataset][agent])
        for agent in common_agents
        for dataset in datasets[1:]
    )
    strict = lengths_equal and all(
        _state_key(left) == _state_key(right)
        for agent in common_agents
        for dataset in datasets[1:]
        for left, right in zip(
            grouped[datasets[0]][agent], grouped[dataset][agent], strict=False
        )
    )
    max_error = 0.0
    ade_numerator = rmse_numerator = speed_numerator = 0.0
    aligned_steps = 0
    worst_pair: tuple[str, str] | None = None
    first_divergence: dict[str, dict[str, Any] | None] = {
        str(value): None for value in tolerances
    }
    for left, right in itertools.combinations(datasets, 2):
        comparisons = compare_states(states[left], states[right], ignore_agent_ids=set())
        pair_max = max((item.max_error for item in comparisons), default=0.0)
        if pair_max >= max_error:
            max_error = pair_max
            worst_pair = (left, right)
        for item in comparisons:
            ade_numerator += item.ade * item.compared_steps
            rmse_numerator += item.rmse * item.rmse * item.compared_steps
            speed_numerator += (item.mean_speed_delta or 0.0) * item.compared_steps
            aligned_steps += item.compared_steps
        _update_first_divergence(
            first_divergence,
            grouped[left],
            grouped[right],
            left,
            right,
            tolerances,
        )
    durations = [
        max((state.sim_time_ms or 0.0 for state in states[dataset]), default=0.0)
        for dataset in datasets
    ]
    outcomes = [str(rows[dataset]["outcome_class"] or "unknown") for dataset in datasets]
    return {
        "group_id": hashlib.sha256("\0".join(datasets).encode()).hexdigest()[:16],
        "parameter_hash": parameter_hash,
        "scenario_id": str(rows[datasets[0]]["scenario_id"]),
        "datasets": list(datasets),
        "outcomes": outcomes,
        "outcome_agree": len(set(outcomes)) == 1,
        "strict_exact": strict,
        "lengths_equal": lengths_equal,
        "actor_sets_equal": actor_sets_equal,
        "common_actor_count": len(common_agents),
        "aligned_steps": aligned_steps,
        "max_position_error_m": max_error,
        "ade_m": ade_numerator / aligned_steps if aligned_steps else None,
        "rmse_m": math.sqrt(rmse_numerator / aligned_steps) if aligned_steps else None,
        "mean_speed_delta_mps": speed_numerator / aligned_steps if aligned_steps else None,
        "duration_spread_ms": max(durations) - min(durations),
        "worst_pair": list(worst_pair) if worst_pair else None,
        "first_divergence": first_divergence,
    }


def _update_first_divergence(
    destination: dict[str, dict[str, Any] | None],
    left_agents: dict[str, list[AgentState]],
    right_agents: dict[str, list[AgentState]],
    left_name: str,
    right_name: str,
    tolerances: tuple[float, ...],
) -> None:
    for agent in set(left_agents) & set(right_agents):
        left_times = {
            float(item.sim_time_ms): item
            for item in left_agents[agent]
            if item.sim_time_ms is not None
        }
        right_times = {
            float(item.sim_time_ms): item
            for item in right_agents[agent]
            if item.sim_time_ms is not None
        }
        for time in sorted(set(left_times) & set(right_times)):
            distance = math.hypot(
                left_times[time].x - right_times[time].x,
                left_times[time].y - right_times[time].y,
            )
            for tolerance in tolerances:
                key = str(tolerance)
                current = destination[key]
                if distance > tolerance and (
                    current is None or time < float(current["sim_time_ms"])
                ):
                    destination[key] = {
                        "sim_time_ms": time,
                        "actor": agent,
                        "distance_m": distance,
                        "pair": [left_name, right_name],
                    }


def _deep_group_summary(
    group: dict[str, Any], records: list[dict[str, Any]], tolerances: tuple[float, ...]
) -> dict[str, Any]:
    comparable = [item for item in records if item["actor_sets_equal"]]
    maxima = [float(item["max_position_error_m"]) for item in comparable]
    return {
        "id": group["id"],
        "datasets": group["datasets"],
        "sample_count": len(records),
        "trajectory_comparable_count": len(comparable),
        "outcome_agreement_count": sum(item["outcome_agree"] for item in records),
        "strict_exact_count": sum(item["strict_exact"] for item in records),
        "lengths_equal_count": sum(item["lengths_equal"] for item in records),
        "position_tolerance_counts": {
            str(tolerance): sum(value <= tolerance for value in maxima)
            for tolerance in tolerances
        },
        "max_position_error_m": {
            "median": statistics.median(maxima) if maxima else None,
            "p95": _percentile(maxima, 0.95),
            "p99": _percentile(maxima, 0.99),
            "max": max(maxima) if maxima else None,
        },
    }


def _select_control_samples(
    records_by_group: dict[str, list[dict[str, Any]]], outlier_limit: int, profile: str
) -> list[dict[str, Any]]:
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for group_id, records in records_by_group.items():
        candidates = records if profile == "full_controls" else []
        if profile != "full_controls":
            candidates.extend(item for item in records if not item["outcome_agree"])
            candidates.extend(
                sorted(
                    records,
                    key=lambda item: float(item["max_position_error_m"]),
                    reverse=True,
                )[:outlier_limit]
            )
            candidates.extend(
                sorted(
                    records,
                    key=lambda item: float(item["duration_spread_ms"]),
                    reverse=True,
                )[:outlier_limit]
            )
            maxima = [float(item["max_position_error_m"]) for item in records]
            for fraction in (0.95, 0.99):
                target = _percentile(maxima, fraction)
                if target is not None and records:
                    candidates.append(
                        min(
                            records,
                            key=lambda item: abs(
                                float(item["max_position_error_m"]) - target
                            ),
                        )
                    )
        for item in candidates:
            selected[(group_id, item["parameter_hash"])] = {
                "group_id": group_id,
                "parameter_hash": item["parameter_hash"],
                "datasets": item["datasets"],
            }
    return list(selected.values())


def _load_controls(path: Path) -> dict[float, dict[str, float]]:
    output: dict[float, dict[str, float]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            try:
                time = float(raw.get("sim_time_ms") or raw.get("timestamp_ms") or "")
            except ValueError:
                continue
            values: dict[str, float] = {}
            for name, value in raw.items():
                if name in {"step_index", "sim_time_ms", "timestamp_ms", "payload_json"}:
                    continue
                try:
                    number = float(value or "")
                except ValueError:
                    continue
                if math.isfinite(number):
                    values[str(name)] = number
            output[time] = values
    return output


def _compare_controls(traces: dict[str, dict[float, dict[str, float]]]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for left, right in itertools.combinations(sorted(traces), 2):
        common_times = sorted(set(traces[left]) & set(traces[right]))
        common_fields = set.intersection(
            *(
                set(traces[dataset][time])
                for dataset in (left, right)
                for time in common_times
            )
        ) if common_times else set()
        for field in sorted(common_fields):
            deltas = [traces[right][time][field] - traces[left][time][field] for time in common_times]
            if not deltas:
                continue
            first = next(
                (
                    {"sim_time_ms": time, "delta": delta}
                    for time, delta in zip(common_times, deltas, strict=True)
                    if delta != 0
                ),
                None,
            )
            results.append(
                {
                    "pair": [left, right],
                    "field": field,
                    "aligned_count": len(deltas),
                    "mae": statistics.fmean(abs(value) for value in deltas),
                    "rmse": math.sqrt(statistics.fmean(value * value for value in deltas)),
                    "max_abs": max(abs(value) for value in deltas),
                    "first_divergence": first,
                }
            )
    return {"available": bool(results), "signals": results}


def _indexed_group_rows(
    index_path: Path, quick: dict[str, Any]
) -> dict[str, dict[str, dict[str, sqlite3.Row]]]:
    connection = sqlite3.connect(f"file:{index_path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        output: dict[str, dict[str, dict[str, sqlite3.Row]]] = {}
        for group in quick["groups"]:
            datasets = list(group["datasets"])
            placeholders = ",".join("?" for _ in datasets)
            rows = connection.execute(
                f"SELECT run_id,dataset_id,scenario_id,parameter_hash,outcome_class,"
                f"stop_condition,has_collision,trace_paths_json FROM runs "
                f"WHERE dataset_id IN ({placeholders}) AND parameter_hash IS NOT NULL "
                f"AND parameter_hash<>'' ORDER BY parameter_hash,dataset_id",
                datasets,
            ).fetchall()
            grouped: dict[str, dict[str, sqlite3.Row]] = defaultdict(dict)
            counts: Counter[tuple[str, str]] = Counter(
                (str(row["dataset_id"]), str(row["parameter_hash"])) for row in rows
            )
            for row in rows:
                key = (str(row["dataset_id"]), str(row["parameter_hash"]))
                if counts[key] == 1:
                    grouped[key[1]][key[0]] = row
            output[str(group["id"])] = {
                parameter_hash: values
                for parameter_hash, values in grouped.items()
                if all(dataset in values for dataset in datasets)
            }
        return output
    finally:
        connection.close()


def _publish_artifacts(
    root: Path,
    key: str,
    summary: dict[str, Any],
    records_by_group: dict[str, list[dict[str, Any]]],
    *,
    source_fingerprint: str,
    generated_at: str,
    force: bool,
    progress: Callable[[int, str], None],
) -> list[str]:
    derived = root / "consistency" / "derived"
    derived.mkdir(parents=True, exist_ok=True)
    if derived.is_symlink() or not derived.resolve().is_relative_to(root.resolve()):
        raise DeepConsistencyError("unsafe derived consistency directory")
    target = derived / key
    stage = Path(tempfile.mkdtemp(prefix=f".{key}.building-", dir=derived))
    flat = [item for records in records_by_group.values() for item in records]
    try:
        (stage / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        progress(1, "Wrote deep consistency summary")
        _write_sample_csv(stage / "samples.csv", flat)
        progress(2, "Wrote per-sample consistency table")
        outliers = sorted(
            flat,
            key=lambda item: (
                item["outcome_agree"],
                -float(item["max_position_error_m"]),
            ),
        )
        _write_sample_csv(stage / "outliers.csv", outliers)
        progress(3, "Wrote ranked outlier table")
        artifacts = ["summary.json", "samples.csv", "outliers.csv"]
        manifest = {
            "artifact_type": "pisa-derived-consistency",
            "complete": True,
            "analyzer_version": DEEP_CONSISTENCY_ANALYZER_VERSION,
            "source_fingerprint": source_fingerprint,
            "generated_at": generated_at,
            "artifacts": artifacts,
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        progress(4, "Published derived consistency manifest")
        lock_path = derived / f".{key}.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            backup: Path | None = None
            if target.exists():
                if not force:
                    shutil.rmtree(stage)
                    return [str(target / name) for name in artifacts]
                backup = derived / f".{key}.replaced-{os.getpid()}"
                if backup.exists():
                    shutil.rmtree(backup)
                os.replace(target, backup)
            try:
                os.replace(stage, target)
            except BaseException:
                if backup is not None:
                    os.replace(backup, target)
                raise
            if backup is not None:
                shutil.rmtree(backup)
        return [str(target / name) for name in artifacts]
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _write_sample_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "group_id",
        "parameter_hash",
        "scenario_id",
        "datasets",
        "outcomes",
        "outcome_agree",
        "strict_exact",
        "lengths_equal",
        "actor_sets_equal",
        "common_actor_count",
        "aligned_steps",
        "max_position_error_m",
        "ade_m",
        "rmse_m",
        "mean_speed_delta_mps",
        "duration_spread_ms",
        "worst_pair",
        "first_divergence_json",
        "control_json",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in rows:
            writer.writerow(
                {
                    **{field: item.get(field) for field in fields},
                    "datasets": "|".join(item.get("datasets", [])),
                    "outcomes": "|".join(item.get("outcomes", [])),
                    "worst_pair": "|".join(item.get("worst_pair") or []),
                    "first_divergence_json": json.dumps(
                        item.get("first_divergence"), sort_keys=True
                    ),
                    "control_json": json.dumps(item.get("control"), sort_keys=True),
                }
            )


def _semantic_agent_ids(states: list[AgentState]) -> list[AgentState]:
    output = []
    for state in states:
        identity = state.entity_name or state.agent_id
        if state.is_ego is True:
            identity = f"ego:{identity}"
        output.append(replace(state, agent_id=identity))
    return output


def _state_key(state: AgentState) -> tuple[Any, ...]:
    return (
        state.step_index,
        state.sim_time_ms,
        state.agent_id,
        state.x,
        state.y,
        state.speed,
        state.yaw,
        state.entity_name,
        state.is_ego,
    )


def _trace_path(row: sqlite3.Row, name: str) -> Path | None:
    try:
        values = json.loads(str(row["trace_paths_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    raw = values.get(name) if isinstance(values, dict) else None
    return Path(str(raw)).expanduser() if raw else None


def _load_quick(root: Path, index_path: Path) -> dict[str, Any]:
    value = _read_json(root / "summary" / "consistency.json")
    return value if value else build_quick_consistency(index_path)


def _source_fingerprint(root: Path) -> str:
    manifest_path = root / "manifest.yaml"
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise DeepConsistencyError("report manifest is unavailable") from exc
    value = manifest.get("source_fingerprint")
    if not isinstance(value, str) or not value:
        raise DeepConsistencyError("report source fingerprint is unavailable")
    return value


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight
