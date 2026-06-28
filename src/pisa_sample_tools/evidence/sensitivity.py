from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
from scipy import stats
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.metrics import (
    balanced_accuracy_score,
    brier_score_loss,
    get_scorer,
    mean_absolute_error,
    r2_score,
    roc_auc_score,
)
from sklearn.model_selection import GroupKFold, StratifiedGroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from .models import AnalysisSpec, RunRecord
from .statistics import as_float, metric_value, normalized_outcome


@dataclass(frozen=True)
class SensitivityResult:
    effects: list[dict[str, Any]]
    importance: list[dict[str, Any]]
    profiles: list[dict[str, Any]]
    interactions: list[dict[str, Any]]
    correlations: list[dict[str, Any]]
    model_quality: list[dict[str, Any]]
    sampling_plan: list[dict[str, Any]]
    warnings: list[str]

    def payload(self) -> dict[str, Any]:
        return {
            "effects": self.effects,
            "importance": self.importance,
            "profiles": self.profiles,
            "interactions": self.interactions,
            "correlations": self.correlations,
            "model_quality": self.model_quality,
            "sampling_plan": self.sampling_plan,
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class _TargetData:
    experiment_id: str
    target: str
    target_type: str
    records: list[tuple[dict[str, Any], float, str]]
    risk_direction: str = "neutral"


def analyze_sensitivity(
    runs: list[RunRecord],
    spec: AnalysisSpec,
    *,
    matched_rows: list[dict[str, Any]] | None = None,
    delta_rows: list[dict[str, Any]] | None = None,
    progress: Callable[[str], None] | None = None,
) -> SensitivityResult:
    if not spec.sensitivity.enabled:
        _progress(progress, "sensitivity: disabled by analysis spec")
        return SensitivityResult([], [], [], [], [], [], [], [])
    parameter_names = _parameter_names(runs, spec)
    effects: list[dict[str, Any]] = []
    importance: list[dict[str, Any]] = []
    profiles: list[dict[str, Any]] = []
    interactions: list[dict[str, Any]] = []
    correlations: list[dict[str, Any]] = []
    quality: list[dict[str, Any]] = []
    warnings: list[str] = []
    targets = _experiment_targets(runs, spec)
    targets.extend(_comparison_targets(matched_rows or [], delta_rows or [], spec))
    _progress(
        progress,
        f"sensitivity: prepared {len(targets)} target(s) across "
        f"{len(_group_runs(runs))} experiment(s) and {len(parameter_names)} parameter(s)",
    )
    for index, target in enumerate(targets, start=1):
        started = time.perf_counter()
        label = f"{target.experiment_id}/{target.target}"
        prefix = f"sensitivity target {index}/{len(targets)} ({index / len(targets):.0%})"
        _progress(
            progress,
            f"{prefix}: {label} [{target.target_type}, {len(target.records)} samples]",
        )
        _progress(progress, f"{prefix}: computing empirical effects and profiles")
        target_effects, target_profiles = _empirical_analysis(
            target, parameter_names, spec
        )
        effects.extend(target_effects)
        profiles.extend(target_profiles)
        _progress(progress, f"{prefix}: fitting and validating surrogate model")
        model = _model_analysis(
            target,
            parameter_names,
            spec,
            progress=progress,
            progress_prefix=prefix,
        )
        quality.append(model[0])
        importance.extend(model[1])
        profiles.extend(model[2])
        interactions.extend(model[3])
        if model[0]["reliability"] == "unavailable":
            warnings.append(
                f"{target.experiment_id}/{target.target}: {model[0]['reason']}"
            )
        _progress(
            progress,
            f"{prefix}: complete in {time.perf_counter() - started:.1f}s "
            f"[reliability={model[0]['reliability']}]",
        )
    _progress(progress, "sensitivity: computing parameter correlations")
    for experiment_id, members in _group_runs(runs).items():
        correlations.extend(_correlation_rows(experiment_id, members, parameter_names))
    _progress(progress, "sensitivity: analysis complete")
    return SensitivityResult(
        effects=effects,
        importance=importance,
        profiles=profiles,
        interactions=interactions,
        correlations=correlations,
        model_quality=quality,
        sampling_plan=_sampling_plan(len(parameter_names), spec),
        warnings=warnings,
    )


def render_sensitivity_figures(
    result: SensitivityResult, output_dir: Path
) -> list[Path]:
    paths: list[Path] = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in result.importance:
        if row.get("importance_type") != "parameter":
            continue
        grouped[(str(row["experiment_id"]), str(row["target"]))].append(row)
    for (experiment_id, target), rows in grouped.items():
        available = [row for row in rows if row.get("importance_mean") is not None]
        if not available:
            continue
        available.sort(key=lambda row: float(row["importance_mean"]))
        root = output_dir / _slug(experiment_id) / _slug(target)
        root.mkdir(parents=True, exist_ok=True)
        fig, ax = plt.subplots(figsize=(8.4, max(3.2, len(available) * 0.38)))
        values = [float(row["importance_mean"]) for row in available]
        lower = [max(0.0, value - float(row["importance_ci_low"])) for value, row in zip(values, available, strict=True)]
        upper = [max(0.0, float(row["importance_ci_high"]) - value) for value, row in zip(values, available, strict=True)]
        ax.barh([str(row["parameter"]) for row in available], values, color="#2563eb")
        ax.errorbar(values, range(len(values)), xerr=[lower, upper], fmt="none", ecolor="#102033", capsize=3)
        ax.set_xlabel("Held-out permutation importance")
        ax.set_title(f"{experiment_id}: {target} parameter sensitivity")
        fig.tight_layout()
        for suffix in ("svg", "png"):
            path = root / f"importance_ranking.{suffix}"
            fig.savefig(path, dpi=180 if suffix == "png" else None)
            paths.append(path)
        plt.close(fig)
    return paths


def _experiment_targets(runs: list[RunRecord], spec: AnalysisSpec) -> list[_TargetData]:
    targets: list[_TargetData] = []
    metric_targets = spec.sensitivity.metric_targets or tuple(spec.metrics)
    for experiment_id, members in _group_runs(runs).items():
        if "failure" in spec.sensitivity.outcome_targets:
            records = []
            for run in members:
                outcome = normalized_outcome(run, spec)
                if outcome in {"success", "failure"}:
                    records.append((run.params, float(outcome == "failure"), _group_key(run.params)))
            targets.append(_TargetData(experiment_id, "failure", "binary", records, "higher_is_riskier"))
        if "invalidity" in spec.sensitivity.outcome_targets:
            records = [
                (
                    run.params,
                    float(normalized_outcome(run, spec) in {"invalid", "execution_error"}),
                    _group_key(run.params),
                )
                for run in members
            ]
            targets.append(_TargetData(experiment_id, "invalidity", "binary", records, "higher_is_riskier"))
        for metric_name in metric_targets:
            if metric_name not in spec.metrics:
                continue
            records = [
                (run.params, value, _group_key(run.params))
                for run in members
                if (value := metric_value(run, spec, metric_name)) is not None
            ]
            targets.append(
                _TargetData(
                    experiment_id,
                    metric_name,
                    "continuous",
                    records,
                    spec.metrics[metric_name].risk_direction,
                )
            )
    return targets


def _comparison_targets(
    matched_rows: list[dict[str, Any]],
    delta_rows: list[dict[str, Any]],
    spec: AnalysisSpec,
) -> list[_TargetData]:
    targets: list[_TargetData] = []
    by_comparison: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in matched_rows:
        by_comparison[str(row["comparison"])].append(row)
    for comparison, rows in by_comparison.items():
        records = [
            (
                _json_params(row.get("parameters")),
                float(row.get("left_outcome") != row.get("right_outcome")),
                str(row.get("match_key")),
            )
            for row in rows
        ]
        targets.append(_TargetData(comparison, "outcome_disagreement", "binary", records))
    deltas_by_target: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    params_by_key = {
        (str(row["comparison"]), str(row["match_key"])): _json_params(row.get("parameters"))
        for row in matched_rows
    }
    for row in delta_rows:
        deltas_by_target[(str(row["comparison"]), str(row["metric"]))].append(row)
    for (comparison, metric), rows in deltas_by_target.items():
        records = [
            (
                params_by_key.get((comparison, str(row["match_key"])), {}),
                float(row["delta_right_minus_left"]),
                str(row["match_key"]),
            )
            for row in rows
        ]
        targets.append(_TargetData(comparison, f"delta:{metric}", "continuous", records))
    return targets


def _empirical_analysis(
    target: _TargetData, parameter_names: list[str], spec: AnalysisSpec
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    profiles: list[dict[str, Any]] = []
    p_values: list[float | None] = []
    for parameter in parameter_names:
        pairs = [(params.get(parameter), y) for params, y, _ in target.records if params.get(parameter) not in {None, ""}]
        numeric = [(value, y) for raw, y in pairs if (value := as_float(raw)) is not None]
        if len(numeric) == len(pairs) and len({value for value, _ in numeric}) >= 2:
            effect, p_value, method = _numeric_effect(numeric, target.target_type)
            effect_low, effect_high = _bootstrap_numeric_effect(
                numeric,
                target.target_type,
                spec.sensitivity.bootstrap_samples,
                spec.sensitivity.random_seed + len(rows),
            )
            profile = _numeric_profile(numeric, target, parameter, spec)
            profiles.extend(profile)
            characteristic = _profile_characteristic(profile)
            kind = "numeric"
        elif len({str(value) for value, _ in pairs}) >= 2:
            effect, p_value, method = _categorical_effect(pairs, target.target_type)
            effect_low, effect_high = None, None
            profiles.extend(_categorical_profile(pairs, target, parameter))
            characteristic = "categorical"
            kind = "categorical"
        else:
            effect, p_value, method, characteristic, kind = None, None, "unavailable", "constant", "constant"
            effect_low, effect_high = None, None
        p_values.append(p_value)
        rows.append(
            {
                "experiment_id": target.experiment_id,
                "target": target.target,
                "target_type": target.target_type,
                "parameter": parameter,
                "parameter_type": kind,
                "method": method,
                "sample_count": len(pairs),
                "effect": effect,
                "effect_abs": abs(effect) if effect is not None else None,
                "effect_ci_low": effect_low,
                "effect_ci_high": effect_high,
                "p_value": p_value,
                "q_value": None,
                "characteristic": characteristic,
                "risk_direction": target.risk_direction,
            }
        )
    q_values = _benjamini_hochberg(p_values)
    for row, q_value in zip(rows, q_values, strict=True):
        row["q_value"] = q_value
    return rows, profiles


def _numeric_effect(values: list[tuple[float, float]], target_type: str) -> tuple[float | None, float | None, str]:
    x = np.asarray([item[0] for item in values], dtype=float)
    y = np.asarray([item[1] for item in values], dtype=float)
    if target_type == "binary":
        positive, negative = x[y == 1], x[y == 0]
        if not len(positive) or not len(negative):
            return None, None, "rank_biserial"
        test = stats.mannwhitneyu(positive, negative, alternative="two-sided")
        effect = 2 * float(test.statistic) / (len(positive) * len(negative)) - 1
        return effect, float(test.pvalue), "rank_biserial"
    if len(set(y)) < 2:
        return None, None, "spearman"
    result = stats.spearmanr(x, y)
    return float(result.statistic), float(result.pvalue), "spearman"


def _categorical_effect(values: list[tuple[Any, float]], target_type: str) -> tuple[float | None, float | None, str]:
    categories = sorted({str(item[0]) for item in values})
    if target_type == "binary":
        table = np.asarray([[sum(str(x) == category and y == outcome for x, y in values) for outcome in (0, 1)] for category in categories])
        try:
            chi2, p_value, _, _ = stats.chi2_contingency(table)
        except ValueError:
            return None, None, "cramers_v"
        denominator = len(values) * max(1, min(table.shape) - 1)
        return math.sqrt(float(chi2) / denominator), float(p_value), "cramers_v"
    groups = [[y for x, y in values if str(x) == category] for category in categories]
    result = stats.kruskal(*groups)
    effect = max(0.0, (float(result.statistic) - len(groups) + 1) / max(1, len(values) - len(groups)))
    return effect, float(result.pvalue), "kruskal_epsilon_squared"


def _bootstrap_numeric_effect(
    values: list[tuple[float, float]],
    target_type: str,
    samples: int,
    seed: int,
) -> tuple[float | None, float | None]:
    if samples <= 0 or len(values) < 3:
        return None, None
    rng = np.random.default_rng(seed)
    estimates = []
    for _ in range(samples):
        selected = [values[index] for index in rng.integers(0, len(values), len(values))]
        effect, _, _ = _numeric_effect(selected, target_type)
        if effect is not None and math.isfinite(effect):
            estimates.append(effect)
    if not estimates:
        return None, None
    return float(np.quantile(estimates, 0.025)), float(np.quantile(estimates, 0.975))


def _numeric_profile(values: list[tuple[float, float]], target: _TargetData, parameter: str, spec: AnalysisSpec) -> list[dict[str, Any]]:
    ordered = sorted(values)
    bins = min(spec.sensitivity.bins, max(1, len(values) // spec.sensitivity.minimum_bin_count))
    if bins < 2:
        return []
    edges = np.unique(np.quantile([x for x, _ in ordered], np.linspace(0, 1, bins + 1)))
    rows = []
    for index, (lower, upper) in enumerate(zip(edges[:-1], edges[1:], strict=True)):
        members = [y for x, y in ordered if lower <= x <= upper and (index == len(edges) - 2 or x < upper)]
        if not members:
            continue
        estimate = float(np.mean(members))
        if target.target_type == "binary":
            low, high = _wilson(sum(value == 1 for value in members), len(members))
        else:
            low, high = _mean_interval(members)
        rows.append(_profile_row(target, parameter, "empirical", (lower + upper) / 2, estimate, low, high, len(members)))
    return rows


def _categorical_profile(values: list[tuple[Any, float]], target: _TargetData, parameter: str) -> list[dict[str, Any]]:
    rows = []
    for category in sorted({str(item[0]) for item in values}):
        members = [y for x, y in values if str(x) == category]
        estimate = float(np.mean(members))
        low, high = _wilson(sum(value == 1 for value in members), len(members)) if target.target_type == "binary" else _mean_interval(members)
        rows.append(_profile_row(target, parameter, "empirical", category, estimate, low, high, len(members)))
    return rows


def _model_analysis(
    target: _TargetData,
    parameter_names: list[str],
    spec: AnalysisSpec,
    *,
    progress: Callable[[str], None] | None = None,
    progress_prefix: str = "sensitivity",
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    n = len(target.records)
    unavailable = _model_unavailable_reason(target, parameter_names, spec)
    base_quality = {"experiment_id": target.experiment_id, "target": target.target, "target_type": target.target_type, "sample_count": n}
    if unavailable:
        _progress(progress, f"{progress_prefix}: model unavailable: {unavailable}")
        return ({**base_quality, "reliability": "unavailable", "reason": unavailable}, [], [], [])
    X, y, groups, numeric_indexes, categorical_indexes = _model_arrays(target, parameter_names)
    folds = min(spec.sensitivity.cv_folds, len(set(groups)))
    if target.target_type == "binary":
        folds = min(folds, min(Counter(y).values()))
        splitter = StratifiedGroupKFold(n_splits=folds, shuffle=True, random_state=spec.sensitivity.random_seed)
        model = RandomForestClassifier(n_estimators=200, min_samples_leaf=max(2, n // 500), class_weight="balanced_subsample", random_state=spec.sensitivity.random_seed, n_jobs=-1)
        scoring = "roc_auc"
    else:
        splitter = GroupKFold(n_splits=folds)
        model = RandomForestRegressor(n_estimators=200, min_samples_leaf=max(2, n // 500), random_state=spec.sensitivity.random_seed, n_jobs=-1)
        scoring = "r2"
    pipeline = _pipeline(model, numeric_indexes, categorical_indexes)
    permutation_values: dict[str, list[float]] = defaultdict(list)
    grouped_values: dict[str, list[float]] = defaultdict(list)
    correlated_clusters = _correlated_clusters(X, numeric_indexes, parameter_names)
    predictions = np.full(n, np.nan)
    splits = list(splitter.split(X, y, groups))
    if target.target_type == "binary":
        splits = [
            (train, test)
            for train, test in splits
            if len(set(y[train])) == 2 and len(set(y[test])) == 2
        ]
        if len(splits) < 2:
            return (
                {
                    **base_quality,
                    "reliability": "unavailable",
                    "reason": "grouped cross-validation could not form two folds with both classes",
                },
                [],
                [],
                [],
            )
    for fold, (train, test) in enumerate(splits):
        fold_label = f"fold {fold + 1}/{len(splits)}"
        _progress(
            progress,
            f"{progress_prefix}: {fold_label} fitting "
            f"[{len(train)} train, {len(test)} test]",
        )
        pipeline.fit(X[train], y[train])
        predictions[test] = pipeline.predict_proba(X[test])[:, 1] if target.target_type == "binary" else pipeline.predict(X[test])
        _progress(
            progress,
            f"{progress_prefix}: {fold_label} computing permutation importance "
            f"[{spec.sensitivity.permutation_repeats} repeats]",
        )
        perm = permutation_importance(pipeline, X[test], y[test], scoring=scoring, n_repeats=spec.sensitivity.permutation_repeats, random_state=spec.sensitivity.random_seed + fold, n_jobs=-1)
        for index, parameter in enumerate(parameter_names):
            permutation_values[parameter].extend(float(value) for value in perm.importances[index])
        scorer = get_scorer(scoring)
        baseline_score = float(scorer(pipeline, X[test], y[test]))
        rng = np.random.default_rng(spec.sensitivity.random_seed + fold)
        for cluster in correlated_clusters:
            label = " + ".join(cluster)
            indexes = [parameter_names.index(name) for name in cluster]
            for _ in range(spec.sensitivity.permutation_repeats):
                shuffled = X[test].copy()
                order = rng.permutation(len(test))
                shuffled[:, indexes] = shuffled[order][:, indexes]
                grouped_values[label].append(
                    baseline_score - float(scorer(pipeline, shuffled, y[test]))
                )
    quality = _quality_row(base_quality, target.target_type, y, predictions)
    reliability = "high" if (quality.get("roc_auc", 0) >= 0.7 if target.target_type == "binary" else quality.get("r2", -1) >= 0.3) else "medium" if (quality.get("roc_auc", 0) >= 0.6 if target.target_type == "binary" else quality.get("r2", -1) >= 0.1) else "low"
    quality.update({"reliability": reliability, "reason": "" if reliability != "low" else "predictive quality is below the reporting threshold"})
    importance = []
    for parameter, values in permutation_values.items():
        importance.append({"experiment_id": target.experiment_id, "target": target.target, "parameter": parameter, "importance_type": "parameter", "importance_mean": float(np.mean(values)), "importance_ci_low": float(np.quantile(values, 0.025)), "importance_ci_high": float(np.quantile(values, 0.975)), "reliability": reliability})
    importance.sort(key=lambda row: float(row["importance_mean"]), reverse=True)
    positive_total = sum(max(0.0, float(row["importance_mean"])) for row in importance)
    for rank, row in enumerate(importance, start=1):
        row["rank"] = rank
        row["normalized_importance"] = max(0.0, float(row["importance_mean"])) / positive_total if positive_total else 0.0
    for label, values in grouped_values.items():
        importance.append({"experiment_id": target.experiment_id, "target": target.target, "parameter": label, "importance_type": "correlated_cluster", "importance_mean": float(np.mean(values)), "importance_ci_low": float(np.quantile(values, 0.025)), "importance_ci_high": float(np.quantile(values, 0.975)), "reliability": reliability, "rank": None, "normalized_importance": None})
    if reliability == "low":
        _progress(
            progress,
            f"{progress_prefix}: skipping ALE and interactions because model reliability is low",
        )
        return quality, importance, [], []
    _progress(progress, f"{progress_prefix}: fitting final surrogate on all {n} samples")
    pipeline.fit(X, y)
    top = [
        row["parameter"]
        for row in importance
        if row.get("importance_type") == "parameter" and row["importance_mean"] > 0
    ][: spec.sensitivity.top_parameters]
    _progress(
        progress,
        f"{progress_prefix}: computing ALE profiles for {len(top)} top parameter(s)",
    )
    ale_profiles = _ale_profiles(pipeline, X, target, parameter_names, top, numeric_indexes, spec)
    interaction_count = len(list(combinations(top[:6], 2)))
    _progress(
        progress,
        f"{progress_prefix}: computing up to {interaction_count} pairwise interaction(s)",
    )
    interaction_rows = _interaction_rows(pipeline, X, target, parameter_names, top, numeric_indexes)
    return quality, importance, ale_profiles, interaction_rows


def _progress(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is not None:
        callback(message)


def _model_unavailable_reason(target: _TargetData, parameter_names: list[str], spec: AnalysisSpec) -> str | None:
    if len(target.records) < spec.sensitivity.minimum_samples:
        return f"requires at least {spec.sensitivity.minimum_samples} samples"
    if len(parameter_names) < 1:
        return "no eligible parameters"
    if target.target_type == "binary":
        counts = Counter(value for _, value, _ in target.records)
        if len(counts) < 2:
            return "target contains only one class"
        if min(counts.values()) < spec.sensitivity.minimum_minority:
            return f"minority class requires at least {spec.sensitivity.minimum_minority} samples"
    if len({group for _, _, group in target.records}) < 3:
        return "requires at least three independent parameter groups"
    if target.target_type == "continuous" and len(
        {value for _, value, _ in target.records}
    ) < 2:
        return "target is constant"
    return None


def _model_arrays(target: _TargetData, parameter_names: list[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[int], list[int]]:
    numeric_indexes = [index for index, name in enumerate(parameter_names) if all(params.get(name) in {None, ""} or as_float(params.get(name)) is not None for params, _, _ in target.records)]
    categorical_indexes = [index for index in range(len(parameter_names)) if index not in numeric_indexes]
    rows = []
    for params, _, _ in target.records:
        rows.append([as_float(params.get(name)) if index in numeric_indexes else (None if params.get(name) in {None, ""} else str(params.get(name))) for index, name in enumerate(parameter_names)])
    return np.asarray(rows, dtype=object), np.asarray([value for _, value, _ in target.records], dtype=float), np.asarray([group for _, _, group in target.records]), numeric_indexes, categorical_indexes


def _pipeline(model: Any, numeric: list[int], categorical: list[int]) -> Pipeline:
    transformers = []
    if numeric:
        transformers.append(("numeric", SimpleImputer(strategy="median"), numeric))
    if categorical:
        transformers.append(("categorical", Pipeline([("imputer", SimpleImputer(strategy="most_frequent")), ("onehot", OneHotEncoder(handle_unknown="ignore"))]), categorical))
    return Pipeline([("preprocess", ColumnTransformer(transformers)), ("model", model)])


def _correlated_clusters(
    X: np.ndarray, numeric_indexes: list[int], parameter_names: list[str]
) -> list[list[str]]:
    graph: dict[int, set[int]] = defaultdict(set)
    for left, right in combinations(numeric_indexes, 2):
        pairs = [
            (float(row[left]), float(row[right]))
            for row in X
            if row[left] is not None and row[right] is not None
        ]
        if len(pairs) < 3:
            continue
        rho = stats.spearmanr(
            [item[0] for item in pairs], [item[1] for item in pairs]
        ).statistic
        if math.isfinite(float(rho)) and abs(float(rho)) >= 0.85:
            graph[left].add(right)
            graph[right].add(left)
    clusters = []
    visited: set[int] = set()
    for node in graph:
        if node in visited:
            continue
        stack, component = [node], set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(graph[current] - component)
        visited.update(component)
        if len(component) > 1:
            clusters.append([parameter_names[index] for index in sorted(component)])
    return clusters


def _quality_row(base: dict[str, Any], target_type: str, y: np.ndarray, predictions: np.ndarray) -> dict[str, Any]:
    valid = np.isfinite(predictions)
    if target_type == "binary":
        labels = (predictions[valid] >= 0.5).astype(int)
        return {**base, "roc_auc": float(roc_auc_score(y[valid], predictions[valid])), "balanced_accuracy": float(balanced_accuracy_score(y[valid], labels)), "brier_score": float(brier_score_loss(y[valid], predictions[valid])), "r2": None, "mae": None}
    return {**base, "roc_auc": None, "balanced_accuracy": None, "brier_score": None, "r2": float(r2_score(y[valid], predictions[valid])), "mae": float(mean_absolute_error(y[valid], predictions[valid]))}


def _ale_profiles(pipeline: Pipeline, X: np.ndarray, target: _TargetData, parameter_names: list[str], top: list[str], numeric_indexes: list[int], spec: AnalysisSpec) -> list[dict[str, Any]]:
    rows = []
    for parameter in top:
        index = parameter_names.index(parameter)
        if index not in numeric_indexes:
            continue
        values = np.asarray([float(value) for value in X[:, index] if value is not None], dtype=float)
        edges = np.unique(np.quantile(values, np.linspace(0, 1, min(spec.sensitivity.bins, len(values)) + 1)))
        if len(edges) < 3:
            continue
        effects = []
        counts = []
        for lower, upper in zip(edges[:-1], edges[1:], strict=True):
            members = np.where(np.asarray([value is not None and lower <= float(value) <= upper for value in X[:, index]]))[0]
            counts.append(len(members))
            if not len(members):
                effects.append(0.0)
                continue
            low_x, high_x = X[members].copy(), X[members].copy()
            low_x[:, index], high_x[:, index] = lower, upper
            effects.append(float(np.mean(_predict(pipeline, high_x, target.target_type) - _predict(pipeline, low_x, target.target_type))))
        accumulated = np.cumsum(effects)
        accumulated -= np.average(accumulated, weights=np.maximum(1, counts))
        for idx, estimate in enumerate(accumulated):
            rows.append(_profile_row(target, parameter, "ale", float((edges[idx] + edges[idx + 1]) / 2), float(estimate), None, None, counts[idx]))
    return rows


def _interaction_rows(pipeline: Pipeline, X: np.ndarray, target: _TargetData, parameter_names: list[str], top: list[str], numeric_indexes: list[int]) -> list[dict[str, Any]]:
    candidates = [name for name in top[:6] if parameter_names.index(name) in numeric_indexes]
    eligible = [
        row
        for row in X
        if all(row[parameter_names.index(name)] is not None for name in candidates)
    ]
    base = np.asarray(eligible[: min(300, len(eligible))], dtype=object)
    if not len(base):
        return []
    rows = []
    for left, right in combinations(candidates, 2):
        left_index, right_index = parameter_names.index(left), parameter_names.index(right)
        left_grid = np.unique(np.quantile(np.asarray(base[:, left_index], dtype=float), np.linspace(0.1, 0.9, 6)))
        right_grid = np.unique(np.quantile(np.asarray(base[:, right_index], dtype=float), np.linspace(0.1, 0.9, 6)))
        surface = np.zeros((len(left_grid), len(right_grid)))
        for i, left_value in enumerate(left_grid):
            for j, right_value in enumerate(right_grid):
                modified = base.copy()
                modified[:, left_index], modified[:, right_index] = left_value, right_value
                surface[i, j] = float(np.mean(_predict(pipeline, modified, target.target_type)))
        residual = surface - surface.mean(axis=1, keepdims=True) - surface.mean(axis=0, keepdims=True) + surface.mean()
        denominator = float(np.var(surface))
        score = math.sqrt(max(0.0, float(np.var(residual)) / denominator)) if denominator else 0.0
        rows.append({"experiment_id": target.experiment_id, "target": target.target, "left_parameter": left, "right_parameter": right, "h_statistic": min(1.0, score), "grid": {"left": left_grid.tolist(), "right": right_grid.tolist(), "response": surface.tolist()}})
    rows.sort(key=lambda row: float(row["h_statistic"]), reverse=True)
    return rows


def _predict(pipeline: Pipeline, X: np.ndarray, target_type: str) -> np.ndarray:
    return pipeline.predict_proba(X)[:, 1] if target_type == "binary" else pipeline.predict(X)


def _correlation_rows(experiment_id: str, runs: list[RunRecord], parameter_names: list[str]) -> list[dict[str, Any]]:
    rows = []
    for left, right in combinations(parameter_names, 2):
        pairs = [(x, y) for run in runs if (x := as_float(run.params.get(left))) is not None and (y := as_float(run.params.get(right))) is not None]
        if len(pairs) < 3 or len({item[0] for item in pairs}) < 2 or len(
            {item[1] for item in pairs}
        ) < 2:
            continue
        result = stats.spearmanr([item[0] for item in pairs], [item[1] for item in pairs])
        rows.append({"experiment_id": experiment_id, "left_parameter": left, "right_parameter": right, "spearman": float(result.statistic), "p_value": float(result.pvalue), "high_correlation": abs(float(result.statistic)) >= 0.85, "sample_count": len(pairs)})
    return rows


def _sampling_plan(dimensions: int, spec: AnalysisSpec) -> list[dict[str, Any]]:
    rows = []
    for base in spec.sensitivity.sobol_base_sizes:
        rows.extend([{"design": "sobol_first_total", "base_size": base, "dimensions": dimensions, "run_count": base * (dimensions + 2)}, {"design": "sobol_second_order", "base_size": base, "dimensions": dimensions, "run_count": base * (2 * dimensions + 2)}])
    for trajectories in spec.sensitivity.morris_trajectories:
        rows.append({"design": "morris", "trajectories": trajectories, "dimensions": dimensions, "run_count": trajectories * (dimensions + 1)})
    return rows


def _parameter_names(runs: list[RunRecord], spec: AnalysisSpec) -> list[str]:
    discovered = sorted({name for run in runs for name in run.params})
    selected = list(spec.parameter_include) if spec.parameter_include else discovered
    return [name for name in selected if name in discovered and name not in spec.parameter_exclude]


def _group_runs(runs: list[RunRecord]) -> dict[str, list[RunRecord]]:
    groups: dict[str, list[RunRecord]] = defaultdict(list)
    for run in runs:
        groups[run.experiment_id].append(run)
    return groups


def _group_key(params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()


def _json_params(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _profile_row(target: _TargetData, parameter: str, method: str, x: Any, estimate: float, low: float | None, high: float | None, count: int) -> dict[str, Any]:
    return {"experiment_id": target.experiment_id, "target": target.target, "parameter": parameter, "method": method, "x": x, "estimate": estimate, "ci_low": low, "ci_high": high, "sample_count": count}


def _profile_characteristic(rows: list[dict[str, Any]]) -> str:
    if len(rows) < 3:
        return "insufficient"
    values = [float(row["estimate"]) for row in rows]
    if math.isclose(min(values), max(values), rel_tol=1e-12, abs_tol=1e-12):
        return "constant_response"
    rho = stats.spearmanr(range(len(values)), values).statistic
    if abs(float(rho)) >= 0.8:
        return "monotonic_increasing" if rho > 0 else "monotonic_decreasing"
    middle_values = values[max(0, len(values) // 2 - 1) : len(values) // 2 + 1]
    middle = float(np.mean(middle_values))
    endpoint_span = abs(values[-1] - values[0])
    if middle < min(values[0], values[-1]) - endpoint_span * 0.1 or middle > max(
        values[0], values[-1]
    ) + endpoint_span * 0.1:
        return "u_shaped_or_inverted"
    differences = np.abs(np.diff(values))
    if len(differences) and np.max(differences) > 2 * max(1e-12, float(np.median(differences))):
        return "threshold_like"
    return "nonlinear"


def _benjamini_hochberg(values: list[float | None]) -> list[float | None]:
    indexed = sorted((value, index) for index, value in enumerate(values) if value is not None)
    output: list[float | None] = [None] * len(values)
    running = 1.0
    count = len(indexed)
    for rank in range(count, 0, -1):
        value, index = indexed[rank - 1]
        running = min(running, float(value) * count / rank)
        output[index] = min(1.0, running)
    return output


def _wilson(successes: int, total: int) -> tuple[float, float]:
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total**2)) / denominator
    return center - margin, center + margin


def _mean_interval(values: list[float]) -> tuple[float, float]:
    if len(values) < 2:
        return values[0], values[0]
    mean = float(np.mean(values))
    margin = 1.96 * float(stats.sem(values))
    return mean - margin, mean + margin


def _slug(value: str) -> str:
    result = "".join(character.lower() if character.isalnum() else "_" for character in value)
    return result.strip("_") or "item"
