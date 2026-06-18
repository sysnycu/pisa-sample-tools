from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from typing import Any

from .ingest import read_trace_rows
from .models import AnalysisSpec, RunRecord, SelectedCase


def as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalized_outcome(run: RunRecord, spec: AnalysisSpec) -> str:
    value = (run.outcome or "unknown").lower()
    reason = (run.termination_reason or "").lower()
    if value in spec.invalid_outcomes:
        return "invalid"
    if value in spec.failure_outcomes or any(token in reason for token in spec.collision_reasons):
        return "failure"
    if value in spec.success_outcomes:
        return "success"
    if run.status and run.status.lower() not in {"finished", "success", "completed"}:
        return "execution_error"
    return value


def safety_region(run: RunRecord, spec: AnalysisSpec) -> str:
    outcome = normalized_outcome(run, spec)
    if outcome == "invalid":
        return "invalid"
    if outcome in {"failure", "execution_error"}:
        return "failure"
    ttc = metric_value(run, spec, "min_ttc")
    if ttc is not None and ttc < spec.near_critical_ttc_s:
        return "near_critical"
    return "safe"


def metric_value(run: RunRecord, spec: AnalysisSpec, name: str) -> float | None:
    binding = spec.metrics.get(name)
    if binding is None or binding.summary is None:
        return None
    return as_float(run.metrics.get(binding.summary))


def numeric_summary(values: list[float]) -> dict[str, float | int | None]:
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "std": None,
            "min": None,
            "max": None,
            "p05": None,
            "p95": None,
        }
    return {
        "count": len(ordered),
        "mean": statistics.fmean(ordered),
        "median": statistics.median(ordered),
        "std": statistics.pstdev(ordered) if len(ordered) > 1 else 0.0,
        "min": ordered[0],
        "max": ordered[-1],
        "p05": percentile(ordered, 0.05),
        "p95": percentile(ordered, 0.95),
    }


def percentile(ordered_values: list[float], quantile: float) -> float:
    if len(ordered_values) == 1:
        return ordered_values[0]
    position = (len(ordered_values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    fraction = position - lower
    return ordered_values[lower] * (1 - fraction) + ordered_values[upper] * fraction


def select_representative_cases(
    runs: list[RunRecord],
    spec: AnalysisSpec,
    x_param: str | None,
    y_param: str | None,
) -> list[SelectedCase]:
    selected: list[SelectedCase] = []
    successful = [run for run in runs if normalized_outcome(run, spec) == "success"]
    failures = [run for run in runs if normalized_outcome(run, spec) == "failure"]
    invalid = [run for run in runs if normalized_outcome(run, spec) == "invalid"]
    non_collision = [
        run
        for run in runs
        if normalized_outcome(run, spec) not in {"failure", "execution_error", "invalid"}
    ]

    safe = _max_metric(successful, spec, "min_ttc", secondary="min_distance")
    if safe:
        selected.append(SelectedCase("safe", safe, "highest min TTC among successful runs"))
    critical = _min_metric(non_collision, spec, "min_ttc")
    if critical and (safe is None or critical.run_id != safe.run_id):
        selected.append(
            SelectedCase("near_critical", critical, "lowest min TTC without failure")
        )
    failure = _earliest_failure(failures)
    if failure:
        selected.append(SelectedCase("failure", failure, "earliest recorded failure"))
    boundary = _boundary_case(runs, spec, x_param, y_param)
    selected_ids = {item.run.run_id for item in selected}
    if boundary and boundary.run_id not in selected_ids:
        selected.append(
            SelectedCase("boundary", boundary, "nearest opposite-class parameter neighbor")
        )
    timeout = _representative_reason(runs, "timeout")
    selected_ids = {item.run.run_id for item in selected}
    if timeout and timeout.run_id not in selected_ids:
        selected.append(SelectedCase("timeout", timeout, "representative timeout"))
    invalid_case = _representative_group(invalid)
    selected_ids = {item.run.run_id for item in selected}
    if invalid_case and invalid_case.run_id not in selected_ids:
        selected.append(SelectedCase("invalid", invalid_case, "representative invalid run"))
    return selected


def grouped_outcomes(runs: list[RunRecord], spec: AnalysisSpec) -> Counter[str]:
    return Counter(normalized_outcome(run, spec) for run in runs)


def repeated_run_rows(runs: list[RunRecord], spec: AnalysisSpec) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[RunRecord]] = defaultdict(list)
    for run in runs:
        key = (
            run.logical_scenario_name,
            tuple(sorted((name, str(value)) for name, value in run.params.items())),
            run.metadata.get("simulator_name"),
            run.metadata.get("av_name"),
            run.metadata.get("sampler_name"),
        )
        groups[key].append(run)
    rows: list[dict[str, Any]] = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        outcomes = [normalized_outcome(run, spec) for run in members]
        majority = Counter(outcomes).most_common(1)[0][1]
        ttc_values = [
            value for run in members if (value := metric_value(run, spec, "min_ttc")) is not None
        ]
        final_positions = [
            position
            for run in members
            if (position := _final_position(run)) is not None
        ]
        x_std = (
            statistics.pstdev(position[0] for position in final_positions)
            if len(final_positions) > 1
            else 0.0 if final_positions else None
        )
        y_std = (
            statistics.pstdev(position[1] for position in final_positions)
            if len(final_positions) > 1
            else 0.0 if final_positions else None
        )
        rows.append(
            {
                "logical_scenario_name": key[0],
                "params": dict(key[1]),
                "simulator_name": key[2],
                "av_name": key[3],
                "sampler_name": key[4],
                "repeat_count": len(members),
                "outcome_consistency": majority / len(members),
                "min_ttc_std": numeric_summary(ttc_values)["std"],
                "final_position_std": (
                    math.hypot(x_std, y_std)
                    if x_std is not None and y_std is not None
                    else None
                ),
                "outcomes": dict(Counter(outcomes)),
            }
        )
    return rows


def _max_metric(
    runs: list[RunRecord],
    spec: AnalysisSpec,
    name: str,
    *,
    secondary: str | None = None,
) -> RunRecord | None:
    candidates = [(metric_value(run, spec, name), run) for run in runs]
    candidates = [(value, run) for value, run in candidates if value is not None]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item[0],
            metric_value(item[1], spec, secondary) or -math.inf if secondary else 0.0,
            item[1].run_id,
        ),
    )[1]


def _min_metric(runs: list[RunRecord], spec: AnalysisSpec, name: str) -> RunRecord | None:
    candidates = [(metric_value(run, spec, name), run) for run in runs]
    candidates = [(value, run) for value, run in candidates if value is not None]
    return min(candidates, key=lambda item: (item[0], item[1].run_id))[1] if candidates else None


def _earliest_failure(runs: list[RunRecord]) -> RunRecord | None:
    if not runs:
        return None
    def time_key(run: RunRecord) -> tuple[float, str]:
        collision_time = as_float(run.metrics.get("collision_time_ms"))
        final_time = as_float(run.metrics.get("run.final_sim_time_ms"))
        return (
            collision_time
            if collision_time is not None
            else final_time if final_time is not None else math.inf,
            run.run_id,
        )

    return min(runs, key=time_key)


def _boundary_case(
    runs: list[RunRecord],
    spec: AnalysisSpec,
    x_param: str | None,
    y_param: str | None,
) -> RunRecord | None:
    if not x_param or not y_param:
        return None
    points = [
        (run, as_float(run.params.get(x_param)), as_float(run.params.get(y_param)))
        for run in runs
    ]
    points = [(run, x, y) for run, x, y in points if x is not None and y is not None]
    if len(points) < 2:
        return None
    xs = [item[1] for item in points]
    ys = [item[2] for item in points]
    x_span = max(xs) - min(xs) or 1.0
    y_span = max(ys) - min(ys) or 1.0
    best: tuple[float, str, RunRecord] | None = None
    for run, x, y in points:
        region = safety_region(run, spec)
        if region in {"invalid", "execution_error"}:
            continue
        for other, ox, oy in points:
            if run is other or safety_region(other, spec) == region:
                continue
            distance = math.hypot((x - ox) / x_span, (y - oy) / y_span)
            candidate = (distance, run.run_id, run)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
    return best[2] if best else None


def _representative_reason(runs: list[RunRecord], token: str) -> RunRecord | None:
    matching = [
        run
        for run in runs
        if token in (run.termination_reason or "").lower()
        or token in (run.stop_reason or "").lower()
    ]
    return _representative_group(matching)


def _representative_group(runs: list[RunRecord]) -> RunRecord | None:
    if not runs:
        return None
    reason_counts = Counter((run.termination_reason or "unknown") for run in runs)
    reason = reason_counts.most_common(1)[0][0]
    matching = [run for run in runs if (run.termination_reason or "unknown") == reason]
    return sorted(matching, key=lambda run: run.run_id)[len(matching) // 2]


def _final_position(run: RunRecord) -> tuple[float, float] | None:
    rows = read_trace_rows(run.agent_states_path)
    candidates = []
    ego_id = str(run.metadata.get("ego_agent_id", "0"))
    for row in rows:
        if str(row.get("agent_id")) != ego_id:
            continue
        x, y = as_float(row.get("x")), as_float(row.get("y"))
        if x is None or y is None:
            continue
        step = as_float(row.get("step_index")) or 0.0
        time = as_float(row.get("sim_time_ms")) or 0.0
        candidates.append((step, time, x, y))
    if not candidates:
        return None
    _, _, x, y = max(candidates)
    return x, y
