from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

QUICK_CONSISTENCY_SCHEMA_VERSION = 1
RUNTIME_METRIC_TOKENS = (
    "job_id",
    "wall_time",
    "wall_clock",
    "speedup",
    "queue_time",
    "worker_id",
    "cpu_time",
    "memory_usage",
)


def is_runtime_metric(name: str) -> bool:
    normalized = name.casefold().replace("-", "_").replace(" ", "_")
    return any(token in normalized for token in RUNTIME_METRIC_TOKENS) or normalized in {
        "created_at",
        "completed_at",
        "host",
        "hostname",
    }


def metric_unit(name: str) -> str | None:
    lowered = name.casefold()
    if lowered.endswith("_ms"):
        return "ms"
    if "total_steps" in lowered or lowered.endswith(".count"):
        return "steps"
    if "ttc" in lowered or "thw" in lowered or lowered.endswith("_s"):
        return "s"
    if "distance" in lowered or "clearance" in lowered:
        return "m"
    if "speed" in lowered and "speedup" not in lowered:
        return "m/s"
    if "acceleration" in lowered or "deceleration" in lowered or "drac" in lowered:
        return "m/s²"
    if "steer" in lowered or "yaw" in lowered:
        return "rad"
    if "speedup" in lowered:
        return "×"
    return None


def build_quick_consistency(
    index_path: Path,
    *,
    progress: Callable[[str, float, float, str], None] | None = None,
) -> dict[str, Any]:
    """Compute inexpensive N-way repeatability from the normalized SQLite index.

    This function deliberately never opens trace files. It uses only canonical
    run rows and indexed scalar metrics, so it is safe to call during every
    normalized report build.
    """

    notify = progress or (lambda _phase, _current, _total, _message: None)
    uri = f"file:{index_path.expanduser().resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        notify("consistency_groups", 0, 3, "Discovering compatible replicate groups")
        metadata = {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key, value FROM metadata")
        }
        datasets = [
            str(row[0])
            for row in connection.execute("SELECT dataset_id FROM datasets ORDER BY dataset_id")
        ]
        aliases = {
            str(row["right_dataset_id"])
            for row in connection.execute(
                "SELECT right_dataset_id FROM dataset_relations WHERE role='duplicate_alias'"
            )
        }
        canonical = [dataset for dataset in datasets if dataset not in aliases]
        paired_edges = {
            tuple(sorted((str(row[0]), str(row[1]))))
            for row in connection.execute(
                "SELECT left_dataset_id, right_dataset_id FROM dataset_relations "
                "WHERE role='paired_replicate'"
            )
        }
        groups = _maximal_replicate_cliques(canonical, paired_edges)
        notify("consistency_samples", 1, 3, "Loading canonical paired samples")
        run_rows = connection.execute(
            "SELECT run_id,dataset_id,scenario_id,parameter_hash,status,outcome_class,"
            "stop_condition,stop_reason,has_collision FROM runs ORDER BY dataset_id,run_id"
        ).fetchall()
        runs_by_dataset: dict[str, dict[str, sqlite3.Row]] = defaultdict(dict)
        hash_counts: dict[str, Counter[str]] = defaultdict(Counter)
        run_counts: Counter[str] = Counter()
        missing_hash_counts: Counter[str] = Counter()
        for row in run_rows:
            dataset_id = str(row["dataset_id"])
            run_counts[dataset_id] += 1
            parameter_hash = str(row["parameter_hash"] or "")
            if not parameter_hash:
                missing_hash_counts[dataset_id] += 1
                continue
            hash_counts[dataset_id][parameter_hash] += 1
            runs_by_dataset[dataset_id][parameter_hash] = row
        unique_runs = {
            dataset: {
                parameter_hash: row
                for parameter_hash, row in rows.items()
                if hash_counts[dataset][parameter_hash] == 1
            }
            for dataset, rows in runs_by_dataset.items()
        }

        metric_rows = connection.execute(
            "SELECT r.dataset_id,r.parameter_hash,m.name,m.value_real,m.value_type "
            "FROM metrics m JOIN runs r USING(run_id) "
            "WHERE r.parameter_hash IS NOT NULL AND r.parameter_hash<>'' "
            "ORDER BY m.name,r.parameter_hash,r.dataset_id"
        ).fetchall()
        metric_cells: dict[str, dict[str, dict[str, float]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        for row in metric_rows:
            if row["value_type"] != "number" or row["value_real"] is None:
                continue
            value = float(row["value_real"])
            if not math.isfinite(value):
                continue
            dataset = str(row["dataset_id"])
            parameter_hash = str(row["parameter_hash"])
            if hash_counts[dataset][parameter_hash] != 1:
                continue
            metric_cells[str(row["name"])][parameter_hash][dataset] = value

        notify("consistency_summarize", 2, 3, f"Summarizing {len(groups)} replicate groups")
        summaries = [
            _summarize_group(
                group,
                unique_runs,
                hash_counts,
                metric_cells,
                run_counts,
                missing_hash_counts,
            )
            for group in groups
        ]
        notify("consistency_complete", 3, 3, "Quick consistency summary is ready")
        return {
            "schema_version": QUICK_CONSISTENCY_SCHEMA_VERSION,
            "available": bool(summaries),
            "reason": None if summaries else "no_compatible_replicate_group",
            "source_fingerprint": metadata.get("source_fingerprint"),
            "dataset_count": len(datasets),
            "canonical_dataset_count": len(canonical),
            "excluded_duplicate_aliases": sorted(aliases),
            "group_count": len(summaries),
            "groups": summaries,
            "methodology": {
                "pairing_key": "parameter_hash unique within every replicate dataset",
                "group_rule": "every dataset pair is classified as paired_replicate",
                "variation_definition": "per-sample maximum minus minimum",
                "trace_files_read": False,
                "runtime_metrics_separated": True,
            },
        }
    finally:
        connection.close()


def write_quick_consistency_artifacts(
    summary: dict[str, Any], summary_dir: Path
) -> tuple[Path, Path, Path]:
    json_path = summary_dir / "consistency.json"
    groups_path = summary_dir / "consistency_groups.csv"
    outcomes_path = summary_dir / "consistency_outcomes.csv"
    json_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    with groups_path.open("w", newline="", encoding="utf-8") as handle:
        fields = [
            "group_id",
            "datasets",
            "experiment_count",
            "common_sample_count",
            "union_sample_count",
            "excluded_noncommon_sample_count",
            "outcome_agreement_count",
            "outcome_comparable_count",
            "outcome_agreement_ratio",
            "information_consistent_count",
            "information_comparable_count",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for group in summary.get("groups", []):
            outcome = next(
                (item for item in group.get("discrete", []) if item.get("key") == "outcome"),
                {},
            )
            writer.writerow(
                {
                    "group_id": group.get("id"),
                    "datasets": "|".join(group.get("datasets", [])),
                    "experiment_count": group.get("experiment_count"),
                    "common_sample_count": group.get("common_sample_count"),
                    "union_sample_count": group.get("union_sample_count"),
                    "excluded_noncommon_sample_count": group.get(
                        "excluded_noncommon_sample_count"
                    ),
                    "outcome_agreement_count": outcome.get("consistent_count"),
                    "outcome_comparable_count": outcome.get("comparable_count"),
                    "outcome_agreement_ratio": outcome.get("agreement_ratio"),
                    "information_consistent_count": group.get(
                        "information_consistent_count"
                    ),
                    "information_comparable_count": group.get(
                        "information_comparable_count"
                    ),
                }
            )
    with outcomes_path.open("w", newline="", encoding="utf-8") as handle:
        fields = ["group_id", "pattern", "count", "all_replicates_agree"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for group in summary.get("groups", []):
            for item in group.get("outcome_patterns", []):
                writer.writerow({"group_id": group.get("id"), **item})
    return json_path, groups_path, outcomes_path


def _summarize_group(
    datasets: tuple[str, ...],
    unique_runs: dict[str, dict[str, sqlite3.Row]],
    hash_counts: dict[str, Counter[str]],
    metric_cells: dict[str, dict[str, dict[str, float]]],
    run_counts: Counter[str],
    missing_hash_counts: Counter[str],
) -> dict[str, Any]:
    hashes = [set(unique_runs.get(dataset, {})) for dataset in datasets]
    common = set.intersection(*hashes) if hashes else set()
    union = set.union(*hashes) if hashes else set()
    group_id = hashlib.sha256("\0".join(datasets).encode()).hexdigest()[:16]
    hash_quality = {
        dataset: {
            "run_count": run_counts[dataset],
            "unique_hash_count": len(unique_runs.get(dataset, {})),
            "missing_hash_runs": missing_hash_counts[dataset],
            "ambiguous_hashes": sum(
                count > 1 for count in hash_counts.get(dataset, {}).values()
            ),
        }
        for dataset in datasets
    }
    definitions = (
        ("outcome", "Outcome", "outcome_class"),
        ("collision", "Collision", "has_collision"),
        ("status", "Run status", "status"),
        ("stop_condition", "Stop condition", "stop_condition"),
    )
    discrete: list[dict[str, Any]] = []
    for key, label, column in definitions:
        comparable = consistent = unavailable = 0
        for parameter_hash in common:
            values = [unique_runs[dataset][parameter_hash][column] for dataset in datasets]
            if any(value is None or str(value).strip() == "" for value in values):
                unavailable += 1
                continue
            comparable += 1
            consistent += len({str(value) for value in values}) == 1
        discrete.append(
            {
                "key": key,
                "label": label,
                "consistent_count": consistent,
                "comparable_count": comparable,
                "agreement_ratio": consistent / comparable if comparable else None,
                "unavailable_sample_count": unavailable,
            }
        )

    patterns: Counter[tuple[str, ...]] = Counter()
    for parameter_hash in common:
        patterns[
            tuple(
                str(unique_runs[dataset][parameter_hash]["outcome_class"] or "unknown")
                for dataset in datasets
            )
        ] += 1
    outcome_patterns = [
        {
            "pattern": "/".join(pattern),
            "count": count,
            "all_replicates_agree": len(set(pattern)) == 1,
        }
        for pattern, count in sorted(patterns.items(), key=lambda item: (-item[1], item[0]))
    ]

    pairwise = []
    for left, right in itertools.combinations(datasets, 2):
        pair_hashes = set(unique_runs.get(left, {})) & set(unique_runs.get(right, {}))
        outcome_agreement = sum(
            unique_runs[left][parameter_hash]["outcome_class"]
            == unique_runs[right][parameter_hash]["outcome_class"]
            for parameter_hash in pair_hashes
        )
        pairwise.append(
            {
                "left": left,
                "right": right,
                "matched_count": len(pair_hashes),
                "outcome_agreement_count": outcome_agreement,
                "outcome_agreement_ratio": (
                    outcome_agreement / len(pair_hashes) if pair_hashes else None
                ),
            }
        )

    behavior_metrics: list[dict[str, Any]] = []
    runtime_metrics: list[dict[str, Any]] = []
    per_sample_metric_equal: dict[str, list[bool]] = {parameter_hash: [] for parameter_hash in common}
    per_sample_metric_comparable: dict[str, bool] = {
        parameter_hash: True for parameter_hash in common
    }
    for name in sorted(metric_cells, key=_metric_sort_key):
        samples: list[tuple[str, float]] = []
        partial = unavailable = 0
        for parameter_hash in common:
            cells = metric_cells[name].get(parameter_hash, {})
            values = [cells.get(dataset) for dataset in datasets]
            valid = [value for value in values if value is not None and math.isfinite(value)]
            if len(valid) == len(datasets):
                spread = max(valid) - min(valid)
                samples.append((parameter_hash, spread))
                if not is_runtime_metric(name):
                    per_sample_metric_equal[parameter_hash].append(spread == 0)
            else:
                if valid:
                    partial += 1
                    if not is_runtime_metric(name):
                        per_sample_metric_comparable[parameter_hash] = False
                else:
                    unavailable += 1
        variations = [spread for _parameter_hash, spread in samples]
        exact_count = sum(value == 0 for value in variations)
        item = {
            "key": name,
            "label": name.replace(".", " · ").replace("_", " ").title(),
            "unit": metric_unit(name),
            "eligible_sample_count": len(samples),
            "partial_sample_count": partial,
            "unavailable_sample_count": unavailable,
            "exact_count": exact_count,
            "exact_ratio": exact_count / len(variations) if variations else None,
            "variation_min": min(variations) if variations else None,
            "variation_median": statistics.median(variations) if variations else None,
            "variation_p95": _linear_percentile(variations, 0.95) if variations else None,
            "variation_max": max(variations) if variations else None,
            "representatives": {
                "max": _representative(samples, max(variations)) if variations else None,
                "p95": _representative(samples, _linear_percentile(variations, 0.95))
                if variations
                else None,
            },
        }
        (runtime_metrics if is_runtime_metric(name) else behavior_metrics).append(item)

    information_comparable = sum(per_sample_metric_comparable.values())
    information_consistent = sum(
        per_sample_metric_comparable[parameter_hash]
        and all(per_sample_metric_equal[parameter_hash])
        and all(
            len(
                {
                    str(unique_runs[dataset][parameter_hash][column])
                    for dataset in datasets
                }
            )
            == 1
            for column in ("status", "outcome_class", "stop_condition", "has_collision")
        )
        for parameter_hash in common
    )
    return {
        "id": group_id,
        "datasets": list(datasets),
        "experiment_count": len(datasets),
        "common_sample_count": len(common),
        "union_sample_count": len(union),
        "excluded_noncommon_sample_count": max(0, len(union) - len(common)),
        "hash_quality": hash_quality,
        "discrete": discrete,
        "outcome_patterns": outcome_patterns,
        "pairwise": pairwise,
        "continuous": behavior_metrics,
        "runtime": runtime_metrics,
        "information_consistent_count": information_consistent,
        "information_comparable_count": information_comparable,
        "information_agreement_ratio": (
            information_consistent / information_comparable
            if information_comparable
            else None
        ),
    }


def _maximal_replicate_cliques(
    datasets: Iterable[str], edges: set[tuple[str, str]]
) -> list[tuple[str, ...]]:
    nodes = set(datasets)
    neighbors = {
        node: {
            other
            for other in nodes
            if other != node and tuple(sorted((node, other))) in edges
        }
        for node in nodes
    }
    cliques: list[frozenset[str]] = []

    def visit(r: set[str], p: set[str], x: set[str]) -> None:
        if not p and not x:
            if len(r) >= 2:
                cliques.append(frozenset(r))
            return
        for node in sorted(tuple(p)):
            visit(r | {node}, p & neighbors[node], x & neighbors[node])
            p.remove(node)
            x.add(node)

    visit(set(), set(nodes), set())
    unique = sorted(set(cliques), key=lambda clique: (-len(clique), sorted(clique)))
    maximal = [clique for clique in unique if not any(clique < other for other in unique)]
    return [tuple(sorted(clique)) for clique in sorted(maximal, key=lambda value: sorted(value))]


def _representative(
    samples: list[tuple[str, float]], target: float | None
) -> dict[str, Any] | None:
    if target is None or not samples:
        return None
    parameter_hash, value = min(
        samples, key=lambda item: (abs(item[1] - target), item[0])
    )
    return {"parameter_hash": parameter_hash, "variation": value}


def _linear_percentile(values: list[float], fraction: float) -> float | None:
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


def _metric_sort_key(name: str) -> tuple[int, str]:
    lowered = name.casefold()
    priorities = (
        ("total_steps", 0),
        ("final_sim_time", 1),
        ("collision", 2),
        ("ttc", 3),
        ("distance.min", 4),
        ("deceleration.max", 5),
        ("wall_time", 20),
        ("speedup", 21),
    )
    return next(
        ((priority, lowered) for token, priority in priorities if token in lowered),
        (10, lowered),
    )
