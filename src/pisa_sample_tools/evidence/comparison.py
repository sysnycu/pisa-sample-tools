from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
from typing import Any

import numpy as np
from scipy.stats import binomtest

from .models import AnalysisSpec, EvidenceError, RunRecord
from .statistics import as_float, metric_value, normalized_outcome


@dataclass(frozen=True)
class ComparisonTables:
    pairing_summary: list[dict[str, Any]]
    matched_runs: list[dict[str, Any]]
    unmatched_runs: list[dict[str, Any]]
    outcome_transition: list[dict[str, Any]]
    metric_deltas: list[dict[str, Any]]
    failure_disagreement: list[dict[str, Any]]
    paired_summary: list[dict[str, Any]]
    warnings: list[str]


def build_paired_comparisons(
    runs: list[RunRecord], spec: AnalysisSpec
) -> ComparisonTables:
    groups: dict[str, list[RunRecord]] = defaultdict(list)
    for run in runs:
        groups[run.experiment_id].append(run)
    pairing_rows: list[dict[str, Any]] = []
    matched_rows: list[dict[str, Any]] = []
    unmatched_rows: list[dict[str, Any]] = []
    transition_rows: list[dict[str, Any]] = []
    delta_rows: list[dict[str, Any]] = []
    disagreement_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    warnings: list[str] = []

    for left_id, right_id in combinations(sorted(groups), 2):
        left, right = groups[left_id], groups[right_id]
        matched, unmatched, pair_warnings = _pair_runs(left, right, spec)
        warnings.extend(f"{left_id} vs {right_id}: {item}" for item in pair_warnings)
        pair_name = f"{left_id}__vs__{right_id}"
        pairing_rows.append(
            {
                "comparison": pair_name,
                "left_dataset": left_id,
                "right_dataset": right_id,
                "left_runs": len(left),
                "right_runs": len(right),
                "matched": len(matched),
                "unmatched_left": sum(side == "left" for side, _ in unmatched),
                "unmatched_right": sum(side == "right" for side, _ in unmatched),
            }
        )
        for side, run in unmatched:
            unmatched_rows.append(
                {
                    "comparison": pair_name,
                    "side": side,
                    "experiment_id": run.experiment_id,
                    "run_id": run.run_id,
                    "sample_id": run.sample_id,
                    "outcome": normalized_outcome(run, spec),
                    "parameters": json.dumps(run.params, sort_keys=True),
                }
            )

        transitions: Counter[tuple[str, str]] = Counter()
        left_only_failure = 0
        right_only_failure = 0
        agreement = 0
        metric_values: dict[str, list[float]] = defaultdict(list)
        for match_key, method, left_run, right_run in matched:
            left_outcome = normalized_outcome(left_run, spec)
            right_outcome = normalized_outcome(right_run, spec)
            transitions[(left_outcome, right_outcome)] += 1
            agreement += left_outcome == right_outcome
            left_failure = left_outcome == "failure"
            right_failure = right_outcome == "failure"
            left_only_failure += left_failure and not right_failure
            right_only_failure += right_failure and not left_failure
            matched_rows.append(
                {
                    "comparison": pair_name,
                    "match_key": match_key,
                    "pairing_method": method,
                    "left_experiment": left_id,
                    "right_experiment": right_id,
                    "left_run_id": left_run.run_id,
                    "right_run_id": right_run.run_id,
                    "left_outcome": left_outcome,
                    "right_outcome": right_outcome,
                    "parameters": json.dumps(left_run.params, sort_keys=True),
                }
            )
            if left_failure != right_failure:
                disagreement_rows.append(
                    {
                        "comparison": pair_name,
                        "match_key": match_key,
                        "left_experiment": left_id,
                        "right_experiment": right_id,
                        "left_run_id": left_run.run_id,
                        "right_run_id": right_run.run_id,
                        "left_failure": left_failure,
                        "right_failure": right_failure,
                    }
                )
            for metric_name in spec.metrics:
                left_value = metric_value(left_run, spec, metric_name)
                right_value = metric_value(right_run, spec, metric_name)
                if left_value is None or right_value is None:
                    continue
                delta = right_value - left_value
                metric_values[metric_name].append(delta)
                delta_rows.append(
                    {
                        "comparison": pair_name,
                        "match_key": match_key,
                        "metric": metric_name,
                        "left": left_value,
                        "right": right_value,
                        "delta_right_minus_left": delta,
                    }
                )
        for (left_outcome, right_outcome), count in sorted(transitions.items()):
            transition_rows.append(
                {
                    "comparison": pair_name,
                    "left_outcome": left_outcome,
                    "right_outcome": right_outcome,
                    "count": count,
                    "ratio": count / len(matched) if matched else None,
                }
            )
        discordant = left_only_failure + right_only_failure
        mcnemar_p = (
            binomtest(min(left_only_failure, right_only_failure), discordant, 0.5).pvalue
            if discordant
            else 1.0
        )
        summary_rows.append(
            {
                "comparison": pair_name,
                "metric": "outcome",
                "matched": len(matched),
                "outcome_agreement": agreement / len(matched) if matched else None,
                "left_only_failure": left_only_failure,
                "right_only_failure": right_only_failure,
                "mcnemar_exact_p": mcnemar_p,
            }
        )
        for metric_name, values in sorted(metric_values.items()):
            summary_rows.append(
                {
                    "comparison": pair_name,
                    "metric": metric_name,
                    "matched": len(values),
                    **_delta_summary(values, spec),
                }
            )

    return ComparisonTables(
        pairing_summary=pairing_rows,
        matched_runs=matched_rows,
        unmatched_runs=unmatched_rows,
        outcome_transition=transition_rows,
        metric_deltas=delta_rows,
        failure_disagreement=disagreement_rows,
        paired_summary=summary_rows,
        warnings=warnings,
    )


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total**2)) / denominator
    return center - margin, center + margin


def _pair_runs(
    left: list[RunRecord], right: list[RunRecord], spec: AnalysisSpec
) -> tuple[
    list[tuple[str, str, RunRecord, RunRecord]],
    list[tuple[str, RunRecord]],
    list[str],
]:
    matched: list[tuple[str, str, RunRecord, RunRecord]] = []
    warnings: list[str] = []
    left_remaining = {run.run_id: run for run in left}
    right_remaining = {run.run_id: run for run in right}

    if spec.pairing_mode == "sample_id_then_parameters":
        left_by_id = _unique_index([run for run in left if run.sample_id], lambda run: str(run.sample_id))
        right_by_id = _unique_index([run for run in right if run.sample_id], lambda run: str(run.sample_id))
        duplicates = left_by_id[1] | right_by_id[1]
        if duplicates:
            message = "duplicate sample_id(s): " + ", ".join(sorted(duplicates))
            if spec.validation_mode == "strict":
                raise EvidenceError(message)
            warnings.append(message)
        for sample_id in sorted(set(left_by_id[0]) & set(right_by_id[0]) - duplicates):
            left_run, right_run = left_by_id[0][sample_id], right_by_id[0][sample_id]
            if not _parameters_match(left_run, right_run, spec.pairing_parameter_tolerance):
                message = f"sample_id '{sample_id}' has mismatched parameters"
                if spec.validation_mode == "strict":
                    raise EvidenceError(message)
                warnings.append(message)
                continue
            matched.append((sample_id, "sample_id", left_run, right_run))
            left_remaining.pop(left_run.run_id, None)
            right_remaining.pop(right_run.run_id, None)

    left_by_params = _unique_index(
        list(left_remaining.values()),
        lambda run: _parameter_key(run, spec.pairing_parameter_tolerance),
    )
    right_by_params = _unique_index(
        list(right_remaining.values()),
        lambda run: _parameter_key(run, spec.pairing_parameter_tolerance),
    )
    duplicate_parameters = left_by_params[1] | right_by_params[1]
    if duplicate_parameters:
        message = f"{len(duplicate_parameters)} ambiguous parameter key(s)"
        if spec.validation_mode == "strict":
            raise EvidenceError(message)
        warnings.append(message)
    for key in sorted(set(left_by_params[0]) & set(right_by_params[0]) - duplicate_parameters):
        left_run, right_run = left_by_params[0][key], right_by_params[0][key]
        matched.append((key, "parameters", left_run, right_run))
        left_remaining.pop(left_run.run_id, None)
        right_remaining.pop(right_run.run_id, None)

    unmatched = [("left", run) for run in left_remaining.values()]
    unmatched.extend(("right", run) for run in right_remaining.values())
    return matched, unmatched, warnings


def _unique_index(runs: list[RunRecord], key) -> tuple[dict[str, RunRecord], set[str]]:
    index: dict[str, RunRecord] = {}
    duplicates: set[str] = set()
    for run in runs:
        value = key(run)
        if value in index:
            duplicates.add(value)
        else:
            index[value] = run
    return index, duplicates


def _parameters_match(left: RunRecord, right: RunRecord, tolerance: float) -> bool:
    if set(left.params) != set(right.params):
        return False
    for name in left.params:
        left_number, right_number = as_float(left.params[name]), as_float(right.params[name])
        if left_number is not None and right_number is not None:
            if not math.isclose(left_number, right_number, rel_tol=tolerance, abs_tol=tolerance):
                return False
        elif str(left.params[name]) != str(right.params[name]):
            return False
    return True


def _parameter_key(run: RunRecord, tolerance: float) -> str:
    normalized = []
    for name, value in sorted(run.params.items()):
        number = as_float(value)
        if number is not None and tolerance > 0:
            normalized_value: Any = round(number / tolerance)
        else:
            normalized_value = value
        normalized.append((name, normalized_value))
    payload = json.dumps(normalized, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _delta_summary(values: list[float], spec: AnalysisSpec) -> dict[str, float | None]:
    array = np.asarray(values, dtype=float)
    result: dict[str, float | None] = {
        "mean_delta": float(np.mean(array)),
        "median_delta": float(np.median(array)),
        "mean_ci_low": None,
        "mean_ci_high": None,
        "median_ci_low": None,
        "median_ci_high": None,
    }
    if not len(array) or spec.bootstrap_samples <= 0:
        return result
    rng = np.random.default_rng(spec.bootstrap_seed)
    means = np.empty(spec.bootstrap_samples)
    medians = np.empty(spec.bootstrap_samples)
    for index in range(spec.bootstrap_samples):
        sample = rng.choice(array, size=len(array), replace=True)
        means[index] = np.mean(sample)
        medians[index] = np.median(sample)
    result.update(
        {
            "mean_ci_low": float(np.quantile(means, 0.025)),
            "mean_ci_high": float(np.quantile(means, 0.975)),
            "median_ci_low": float(np.quantile(medians, 0.025)),
            "median_ci_high": float(np.quantile(medians, 0.975)),
        }
    )
    return result
