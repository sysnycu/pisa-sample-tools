from __future__ import annotations

import hashlib
import math
import sqlite3
import statistics
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .consistency import metric_unit

PAIRED_RELATION_ROLES = {
    "paired_replicate",
    "paired_system_intervention",
    "paired_policy_intervention",
}


class PairedParameterError(ValueError):
    """Raised when a paired parameter request cannot be evaluated safely."""


class PairedMetricAgreementError(ValueError):
    """Raised when a paired metric agreement request is not defensible."""


def comparison_identifier(left: str, right: str) -> str:
    return hashlib.sha256(f"{left}\0{right}".encode()).hexdigest()[:20]


def analyze_paired_metric_agreement(
    database: Path | sqlite3.Connection,
    relation_id: str,
    request: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    options = dict(request or {})
    owns_connection = not isinstance(database, sqlite3.Connection)
    connection = (
        sqlite3.connect(f"file:{Path(database)}?mode=ro", uri=True)
        if owns_connection
        else database
    )
    connection.row_factory = sqlite3.Row
    try:
        try:
            relation = _relation(connection, relation_id)
        except PairedParameterError as exc:
            raise PairedMetricAgreementError(str(exc)) from exc
        left = str(relation["left_dataset_id"])
        right = str(relation["right_dataset_id"])
        role = str(relation["role"])
        if role not in PAIRED_RELATION_ROLES:
            raise PairedMetricAgreementError(
                f"comparison relation {relation_id!r} is not uniquely pairable"
            )
        pairs = _paired_runs(connection, left, right)
        if not pairs:
            raise PairedMetricAgreementError("comparison has no unique parameter-hash pairs")

        metrics = _shared_numeric_names(connection, left, right, "metrics")
        if not metrics:
            raise PairedMetricAgreementError("comparison has no shared numeric output metric")
        metric = str(options.get("metric") or metrics[0])
        if metric not in metrics:
            raise PairedMetricAgreementError("metric must be a shared numeric output metric")
        x_side = str(options.get("x_side") or "right")
        if x_side not in {"left", "right"}:
            raise PairedMetricAgreementError("x_side must be left or right")
        outcome_scope = str(options.get("outcome_scope") or "all_same")
        if outcome_scope not in {"all_same", "success", "fail", "invalid", "unknown"}:
            raise PairedMetricAgreementError(
                "outcome_scope must be all_same, success, fail, invalid, or unknown"
            )
        primary = _positive_number(options.get("primary_threshold", 5), "primary_threshold")
        secondary = _positive_number(options.get("secondary_threshold", 10), "secondary_threshold")
        if secondary <= primary:
            raise PairedMetricAgreementError(
                "secondary_threshold must be greater than primary_threshold"
            )
        point_limit = _bounded_integer(
            options.get("point_limit", 20_000), 100, 100_000, "point_limit"
        )

        metric_values = _pair_values(
            connection, left, right, {metric}, "metrics", pairs
        )
        records: list[dict[str, Any]] = []
        metric_missing_count = 0
        outcome_disagreement_metric_eligible_count = 0
        for pair in pairs:
            parameter_hash = str(pair["parameter_hash"])
            left_value = metric_values.get((left, parameter_hash, metric))
            right_value = metric_values.get((right, parameter_hash, metric))
            if left_value is None or right_value is None:
                metric_missing_count += 1
                continue
            left_outcome = _canonical_outcome(str(pair["left_outcome"] or "unknown"))
            right_outcome = _canonical_outcome(str(pair["right_outcome"] or "unknown"))
            if left_outcome != right_outcome:
                outcome_disagreement_metric_eligible_count += 1
                continue
            x_value, y_value = (
                (left_value, right_value) if x_side == "left" else (right_value, left_value)
            )
            signed_difference = y_value - x_value
            records.append(
                {
                    "parameter_hash": parameter_hash,
                    "left_run_id": str(pair["left_run_id"]),
                    "right_run_id": str(pair["right_run_id"]),
                    "left_outcome": left_outcome,
                    "right_outcome": right_outcome,
                    "category": f"{left_outcome}_{right_outcome}",
                    "left_value": left_value,
                    "right_value": right_value,
                    "x": x_value,
                    "y": y_value,
                    "y_minus_x": signed_difference,
                    "absolute_difference": abs(signed_difference),
                }
            )

        category_names = ("success", "fail", "invalid", "unknown")
        categories = {
            name: _metric_agreement_summary(
                [record for record in records if record["left_outcome"] == name],
                primary,
                secondary,
            )
            for name in category_names
        }
        included = (
            records
            if outcome_scope == "all_same"
            else [record for record in records if record["left_outcome"] == outcome_scope]
        )
        plotted = sorted(included, key=lambda item: item["parameter_hash"])[:point_limit]
        x_dataset = left if x_side == "left" else right
        y_dataset = right if x_side == "left" else left
        return {
            "schema_version": 1,
            "relation_id": relation_id,
            "left": left,
            "right": right,
            "role": role,
            "pairing_key": "parameter_hash unique within each dataset",
            "metrics": [
                {
                    "key": name,
                    "label": _metric_label(name),
                    "unit": metric_unit(name),
                }
                for name in metrics
            ],
            "selection": {
                "metric": metric,
                "unit": metric_unit(metric),
                "x_side": x_side,
                "x_dataset": x_dataset,
                "y_dataset": y_dataset,
                "outcome_scope": outcome_scope,
                "primary_threshold": primary,
                "secondary_threshold": secondary,
                "difference_definition": "y minus x",
            },
            "summary": {
                "paired_count": len(pairs),
                "metric_eligible_count": len(pairs) - metric_missing_count,
                "metric_missing_count": metric_missing_count,
                "same_outcome_metric_eligible_count": len(records),
                "outcome_disagreement_metric_eligible_count": outcome_disagreement_metric_eligible_count,
                "included": _metric_agreement_summary(included, primary, secondary),
                "categories": categories,
            },
            "points": plotted,
            "coverage": {
                "included_count": len(included),
                "plotted_count": len(plotted),
                "point_limit": point_limit,
                "sampled": len(plotted) < len(included),
            },
            "disclosure": {
                "input_scope": "recorded paired output metric",
                "derived_parameters_used": False,
                "metric_missing_rule": "pairs require finite recorded values on both sides; missing values are not zero-filled",
                "outcome_rule": "only equal canonical outcome classes are included",
                "threshold_rule": "absolute difference greater than or equal to each threshold",
                "claim_scope": "paired descriptive metric agreement; no internal-cause or overall-safety claim",
            },
        }
    finally:
        if owns_connection:
            connection.close()


def _positive_number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PairedMetricAgreementError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise PairedMetricAgreementError(f"{name} must be finite and greater than zero")
    return number


def _canonical_outcome(value: str) -> str:
    lowered = value.casefold().strip()
    if lowered in {"success", "pass", "passed"}:
        return "success"
    if lowered in {"fail", "failed", "failure"}:
        return "fail"
    if lowered == "invalid":
        return "invalid"
    return "unknown"


def _metric_label(name: str) -> str:
    return name.replace("_", " ").replace(".", " · ")


def _metric_agreement_summary(
    records: list[dict[str, Any]], primary: float, secondary: float
) -> dict[str, Any]:
    count = len(records)
    differences = [float(record["absolute_difference"]) for record in records]
    threshold_rows = []
    for threshold in (primary, secondary):
        outside = sum(value >= threshold for value in differences)
        above = sum(float(record["y_minus_x"]) >= threshold for record in records)
        below = sum(float(record["y_minus_x"]) <= -threshold for record in records)
        threshold_rows.append(
            {
                "threshold": threshold,
                "count": outside,
                "rate": outside / count if count else None,
                "y_greater_count": above,
                "x_greater_count": below,
            }
        )
    return {
        "count": count,
        "mean_absolute_difference": statistics.fmean(differences) if differences else None,
        "median_absolute_difference": statistics.median(differences) if differences else None,
        "thresholds": threshold_rows,
    }


def analyze_paired_parameters(
    database: Path | sqlite3.Connection,
    relation_id: str,
    request: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    options = dict(request or {})
    owns_connection = not isinstance(database, sqlite3.Connection)
    connection = (
        sqlite3.connect(f"file:{Path(database)}?mode=ro", uri=True)
        if owns_connection
        else database
    )
    connection.row_factory = sqlite3.Row
    try:
        relation = _relation(connection, relation_id)
        left = str(relation["left_dataset_id"])
        right = str(relation["right_dataset_id"])
        role = str(relation["role"])
        if role not in PAIRED_RELATION_ROLES:
            raise PairedParameterError(
                f"comparison relation {relation_id!r} is not uniquely pairable"
            )
        pairs = _paired_runs(connection, left, right)
        if not pairs:
            raise PairedParameterError("comparison has no unique parameter-hash pairs")

        parameters = _shared_numeric_names(connection, left, right, "parameters")
        if len(parameters) < 2:
            raise PairedParameterError(
                "paired parameter analysis requires at least two shared numeric parameters"
            )
        metrics = _shared_numeric_names(connection, left, right, "metrics")
        x_default = next(
            (
                name
                for name in parameters
                if "ego" in name.casefold() and "speed" in name.casefold()
            ),
            parameters[0],
        )
        x_name = _selected_name(options.get("x"), parameters, x_default, "x")
        y_default = next(
            (
                name
                for name in parameters
                if name != x_name
                and "agent" in name.casefold()
                and "speed" in name.casefold()
            ),
            next(name for name in parameters if name != x_name),
        )
        y_name = _selected_name(options.get("y"), parameters, y_default, "y")
        if x_name == y_name:
            raise PairedParameterError("x and y must be different original parameters")
        facet_default = next(
            (name for name in parameters if name not in {x_name, y_name}), None
        )
        facet_raw = options.get("facet", facet_default)
        facet_name = None
        if facet_raw not in {None, ""}:
            facet_name = _selected_name(facet_raw, parameters, facet_default, "facet")
            if facet_name in {x_name, y_name}:
                raise PairedParameterError("facet must differ from x and y")

        view = str(options.get("view") or "outcome")
        if view not in {"outcome", "metric_delta"}:
            raise PairedParameterError("view must be outcome or metric_delta")
        metric = options.get("metric")
        if view == "metric_delta":
            if metric is None:
                metric = metrics[0] if metrics else None
            if metric not in metrics:
                raise PairedParameterError("metric must be a shared numeric output metric")
            metric = str(metric)
        else:
            metric = None

        bin_count = _bounded_integer(options.get("bin_count", 5), 2, 20, "bin_count")
        point_limit = _bounded_integer(
            options.get("point_limit", 20_000), 100, 100_000, "point_limit"
        )
        parameter_values = _pair_values(
            connection, left, right, set(parameters), "parameters", pairs
        )
        metric_values = (
            _pair_values(connection, left, right, {metric}, "metrics", pairs)
            if metric
            else {}
        )
        records = _records(
            pairs, left, right, parameters, parameter_values, metric, metric_values
        )
        incomplete_parameter_count = sum(
            record["parameter_missing"] for record in records
        )
        parameter_mismatch_count = sum(record["parameter_mismatch"] for record in records)
        complete_records = [
            record
            for record in records
            if not record["parameter_missing"] and not record["parameter_mismatch"]
        ]
        if not complete_records:
            raise PairedParameterError("paired samples do not contain complete numeric parameters")

        custom_boundaries = options.get("boundaries") or {}
        if not isinstance(custom_boundaries, Mapping):
            raise PairedParameterError("boundaries must map parameter names to numeric edges")
        unknown_boundaries = sorted(set(custom_boundaries) - set(parameters))
        if unknown_boundaries:
            raise PairedParameterError(
                "boundaries contain unknown or non-original parameters: "
                + ", ".join(unknown_boundaries)
            )
        boundaries = {
            name: _boundaries(
                name,
                [float(record["parameters"][name]) for record in complete_records],
                custom_boundaries.get(name),
                bin_count,
            )
            for name in parameters
        }
        minimum_cell_count = options.get("minimum_cell_count")
        if minimum_cell_count is None:
            minimum_cell_count = max(10, math.ceil(len(pairs) * 0.01))
        minimum_cell_count = _bounded_integer(
            minimum_cell_count, 1, 1_000_000, "minimum_cell_count"
        )
        facet_range = _optional_range(options.get("facet_range"), "facet_range")

        included: list[dict[str, Any]] = []
        excluded_by_boundaries = 0
        excluded_by_facet = 0
        for record in complete_records:
            indices = {
                name: _bin_index(float(record["parameters"][name]), boundaries[name])
                for name in parameters
            }
            if any(value is None for value in indices.values()):
                excluded_by_boundaries += 1
                continue
            if facet_name and facet_range:
                value = float(record["parameters"][facet_name])
                if value < facet_range[0] or value > facet_range[1]:
                    excluded_by_facet += 1
                    continue
            included.append({**record, "bin_indices": indices})

        overview = _overview(records, metric)
        marginals = [
            _marginal(name, boundaries[name], included, minimum_cell_count)
            for name in parameters
        ]
        heatmaps = _heatmaps(
            x_name,
            y_name,
            facet_name,
            boundaries,
            included,
            minimum_cell_count,
        )
        observations = _observations(
            left, right, metric, overview, marginals, heatmaps, minimum_cell_count
        )
        candidates = _candidates(records, metric)
        plotted = sorted(included, key=lambda item: item["parameter_hash"])[:point_limit]
        points = [
            {
                "parameter_hash": record["parameter_hash"],
                "left_run_id": record["left_run_id"],
                "right_run_id": record["right_run_id"],
                "x": record["parameters"][x_name],
                "y": record["parameters"][y_name],
                "facet": record["parameters"].get(facet_name) if facet_name else None,
                "left_outcome": record["left_outcome"],
                "right_outcome": record["right_outcome"],
                "category": record["category"],
                "left_value": record.get("left_metric"),
                "right_value": record.get("right_metric"),
                "delta": record.get("delta"),
            }
            for record in plotted
        ]
        return {
            "schema_version": 1,
            "relation_id": relation_id,
            "left": left,
            "right": right,
            "role": role,
            "pairing_key": "parameter_hash unique within each dataset",
            "parameters": parameters,
            "metrics": metrics,
            "selection": {
                "x": x_name,
                "y": y_name,
                "facet": facet_name,
                "view": view,
                "metric": metric,
                "delta_definition": "right minus left",
                "bin_count": bin_count,
                "boundaries": boundaries,
                "facet_range": facet_range,
                "minimum_cell_count": minimum_cell_count,
            },
            "overview": overview,
            "marginals": marginals,
            "heatmaps": heatmaps,
            "observations": observations,
            "candidates": candidates,
            "points": points,
            "coverage": {
                "paired_count": len(pairs),
                "complete_parameter_count": len(complete_records),
                "included_count": len(included),
                "excluded_incomplete_parameters": incomplete_parameter_count,
                "excluded_parameter_mismatch": parameter_mismatch_count,
                "excluded_by_boundaries": excluded_by_boundaries,
                "excluded_by_facet": excluded_by_facet,
                "plotted_count": len(points),
                "point_limit": point_limit,
                "sampled": len(points) < len(included),
            },
            "disclosure": {
                "input_scope": "recorded original parameters only",
                "derived_parameters_used": False,
                "interval_rule": "[lower, upper), with the final interval upper-inclusive",
                "sparse_rule": "cells below minimum_cell_count remain visible but are excluded from observations",
                "metric_missing_rule": "paired metric deltas require finite values on both sides; missing values are not zero-filled",
                "claim_scope": "descriptive paired observations; no internal-cause or overall-safety claim",
            },
        }
    finally:
        if owns_connection:
            connection.close()


def build_portable_paired_parameter_summary(database: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        relations = [
            (
                comparison_identifier(str(row["left_dataset_id"]), str(row["right_dataset_id"])),
                str(row["role"]),
            )
            for row in connection.execute(
                "SELECT left_dataset_id, right_dataset_id, role FROM dataset_relations "
                "ORDER BY left_dataset_id, right_dataset_id"
            )
            if str(row["role"]) in PAIRED_RELATION_ROLES
        ]
        items = []
        for relation_id, _role in relations:
            try:
                result = analyze_paired_parameters(
                    connection,
                    relation_id,
                    {"view": "outcome", "point_limit": 100},
                )
            except PairedParameterError:
                continue
            items.append(
                {
                    key: value
                    for key, value in result.items()
                    if key not in {"points", "candidates", "metrics"}
                }
            )
        return {
            "schema_version": 1,
            "items": items,
            "methodology": {
                "pairing_key": "parameter_hash unique within each dataset",
                "input_scope": "recorded original parameters only",
                "derived_parameters_used": False,
                "default_binning": "five equal-width bins over the paired common observed range",
            },
        }
    finally:
        connection.close()


def portable_region_rows(summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in summary.get("items", []):
        for marginal in item.get("marginals", []):
            for cell in marginal.get("bins", []):
                rows.append(
                    {
                        "relation_id": item["relation_id"],
                        "left": item["left"],
                        "right": item["right"],
                        "parameter": marginal["parameter"],
                        "lower": cell["lower"],
                        "upper": cell["upper"],
                        "upper_inclusive": cell["upper_inclusive"],
                        "paired_count": cell["total"],
                        "disagreement_count": cell["disagreement_count"],
                        "disagreement_rate": cell["disagreement_rate"],
                        "sparse": cell["sparse"],
                    }
                )
    return rows


def _relation(connection: sqlite3.Connection, relation_id: str) -> sqlite3.Row:
    for row in connection.execute(
        "SELECT left_dataset_id, right_dataset_id, role FROM dataset_relations"
    ):
        if comparison_identifier(
            str(row["left_dataset_id"]), str(row["right_dataset_id"])
        ) == relation_id:
            return row
    raise PairedParameterError(f"unknown comparison relation {relation_id!r}")


def _paired_runs(
    connection: sqlite3.Connection, left: str, right: str
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            WITH left_unique AS (
                SELECT parameter_hash, MIN(run_id) AS run_id
                FROM runs WHERE dataset_id = ? AND parameter_hash IS NOT NULL
                  AND parameter_hash <> '' GROUP BY parameter_hash HAVING COUNT(*) = 1
            ), right_unique AS (
                SELECT parameter_hash, MIN(run_id) AS run_id
                FROM runs WHERE dataset_id = ? AND parameter_hash IS NOT NULL
                  AND parameter_hash <> '' GROUP BY parameter_hash HAVING COUNT(*) = 1
            )
            SELECT left_unique.parameter_hash,
                   left_run.run_id AS left_run_id, right_run.run_id AS right_run_id,
                   left_run.outcome_class AS left_outcome,
                   right_run.outcome_class AS right_outcome,
                   left_run.stop_condition AS left_stop_condition,
                   right_run.stop_condition AS right_stop_condition,
                   left_run.stop_reason AS left_stop_reason,
                   right_run.stop_reason AS right_stop_reason
            FROM left_unique JOIN right_unique USING(parameter_hash)
            JOIN runs AS left_run ON left_run.run_id = left_unique.run_id
            JOIN runs AS right_run ON right_run.run_id = right_unique.run_id
            ORDER BY left_unique.parameter_hash
            """,
            (left, right),
        )
    ]


def _shared_numeric_names(
    connection: sqlite3.Connection, left: str, right: str, table: str
) -> list[str]:
    if table not in {"parameters", "metrics"}:
        raise AssertionError(table)
    rows = connection.execute(
        f"""
        SELECT v.name,
               SUM(CASE WHEN r.dataset_id = ? AND v.value_real IS NOT NULL THEN 1 ELSE 0 END) left_count,
               SUM(CASE WHEN r.dataset_id = ? AND v.value_real IS NOT NULL THEN 1 ELSE 0 END) right_count
        FROM {table} AS v JOIN runs AS r ON r.run_id = v.run_id
        WHERE r.dataset_id IN (?, ?) GROUP BY v.name
        HAVING left_count > 0 AND right_count > 0 ORDER BY v.name
        """,
        (left, right, left, right),
    )
    names = [str(row["name"]) for row in rows]
    if table == "metrics":
        names = [name for name in names if not _raw_control_metric(name)]
        names.sort(key=_metric_priority)
    return names


def _raw_control_metric(name: str) -> bool:
    lowered = name.casefold()
    return any(
        token in lowered
        for token in ("control.", "throttle", "accelerator", "brake", "steer")
    )


def _metric_priority(name: str) -> tuple[int, str]:
    lowered = name.casefold()
    if "distance.min" in lowered or ("distance" in lowered and lowered.endswith(".min")):
        return (0, lowered)
    if "ttc" in lowered:
        return (1, lowered)
    if "drac" in lowered:
        return (2, lowered)
    if "collision" in lowered:
        return (3, lowered)
    if "deceleration" in lowered:
        return (4, lowered)
    if lowered.startswith("run."):
        return (9, lowered)
    return (6, lowered)


def _pair_values(
    connection: sqlite3.Connection,
    left: str,
    right: str,
    names: set[str | None],
    table: str,
    pairs: list[dict[str, Any]],
) -> dict[tuple[str, str, str], float]:
    actual_names = sorted(str(name) for name in names if name)
    if not actual_names:
        return {}
    placeholders = ",".join("?" for _ in actual_names)
    valid_hashes = {str(pair["parameter_hash"]) for pair in pairs}
    output: dict[tuple[str, str, str], float] = {}
    for row in connection.execute(
        f"""
        SELECT r.dataset_id, r.parameter_hash, v.name, v.value_real
        FROM runs AS r JOIN {table} AS v ON v.run_id = r.run_id
        WHERE r.dataset_id IN (?, ?) AND v.name IN ({placeholders})
          AND v.value_real IS NOT NULL
        """,
        (left, right, *actual_names),
    ):
        parameter_hash = str(row["parameter_hash"] or "")
        if parameter_hash in valid_hashes:
            value = float(row["value_real"])
            if math.isfinite(value):
                output[(str(row["dataset_id"]), parameter_hash, str(row["name"]))] = value
    return output


def _records(
    pairs: list[dict[str, Any]],
    left_dataset: str,
    right_dataset: str,
    parameters: list[str],
    values: dict[tuple[str, str, str], float],
    metric: str | None,
    metric_values: dict[tuple[str, str, str], float],
) -> list[dict[str, Any]]:
    records = []
    for pair in pairs:
        parameter_hash = str(pair["parameter_hash"])
        left_outcome = str(pair["left_outcome"] or "unknown").casefold()
        right_outcome = str(pair["right_outcome"] or "unknown").casefold()
        category = _outcome_category(left_outcome, right_outcome)
        left_metric = metric_values.get((left_dataset, parameter_hash, metric)) if metric else None
        right_metric = metric_values.get((right_dataset, parameter_hash, metric)) if metric else None
        left_parameters = {
            name: values.get((left_dataset, parameter_hash, name)) for name in parameters
        }
        right_parameters = {
            name: values.get((right_dataset, parameter_hash, name)) for name in parameters
        }
        records.append(
            {
                **pair,
                "parameter_hash": parameter_hash,
                "left_outcome": left_outcome,
                "right_outcome": right_outcome,
                "category": category,
                "parameters": left_parameters,
                "parameter_missing": any(
                    left_parameters[name] is None or right_parameters[name] is None
                    for name in parameters
                ),
                "parameter_mismatch": any(
                    left_parameters[name] is not None
                    and right_parameters[name] is not None
                    and left_parameters[name] != right_parameters[name]
                    for name in parameters
                ),
                "left_metric": left_metric,
                "right_metric": right_metric,
                "delta": (
                    right_metric - left_metric
                    if left_metric is not None and right_metric is not None
                    else None
                ),
            }
        )
    return records


def _outcome_category(left: str, right: str) -> str:
    if left == right:
        return "same_outcome"
    if left in {"success", "pass"} and right == "fail":
        return "left_success_right_fail"
    if left == "fail" and right in {"success", "pass"}:
        return "left_fail_right_success"
    return "other_disagreement"


def _overview(records: list[dict[str, Any]], metric: str | None) -> dict[str, Any]:
    categories = Counter(record["category"] for record in records)
    transitions = Counter(
        f"{record['left_outcome']} -> {record['right_outcome']}" for record in records
    )
    disagreement_count = len(records) - categories["same_outcome"]
    metric_eligible = sum(record.get("delta") is not None for record in records)
    return {
        "paired_count": len(records),
        "agreement_count": categories["same_outcome"],
        "disagreement_count": disagreement_count,
        "disagreement_rate": disagreement_count / len(records) if records else None,
        "direct_reversal_count": categories["left_success_right_fail"]
        + categories["left_fail_right_success"],
        "invalid_related_count": sum(
            1
            for record in records
            if record["left_outcome"] != record["right_outcome"]
            and "invalid" in {record["left_outcome"], record["right_outcome"]}
        ),
        "categories": dict(categories),
        "transitions": dict(transitions),
        "metric": metric,
        "metric_eligible_count": metric_eligible,
        "metric_missing_count": len(records) - metric_eligible if metric else 0,
    }


def _boundaries(
    name: str, values: list[float], supplied: Any, bin_count: int
) -> list[float]:
    if supplied is not None:
        if not isinstance(supplied, (list, tuple)) or not 3 <= len(supplied) <= 21:
            raise PairedParameterError(
                f"boundaries for {name} must contain 3 to 21 numeric edges"
            )
        edges = [float(value) for value in supplied]
        if not all(math.isfinite(value) for value in edges) or any(
            right <= left for left, right in zip(edges, edges[1:], strict=False)
        ):
            raise PairedParameterError(f"boundaries for {name} must be finite and increasing")
        return edges
    minimum = min(values)
    maximum = max(values)
    if minimum == maximum:
        padding = max(abs(minimum) * 0.01, 0.5)
        minimum -= padding
        maximum += padding
    width = (maximum - minimum) / bin_count
    return [minimum + width * index for index in range(bin_count)] + [maximum]


def _bin_index(value: float, edges: list[float]) -> int | None:
    if value < edges[0] or value > edges[-1]:
        return None
    if value == edges[-1]:
        return len(edges) - 2
    for index, (lower, upper) in enumerate(zip(edges, edges[1:], strict=False)):
        if lower <= value < upper:
            return index
    return None


def _cell(records: list[dict[str, Any]], minimum: int) -> dict[str, Any]:
    categories = Counter(record["category"] for record in records)
    disagreement = len(records) - categories["same_outcome"]
    deltas = [float(record["delta"]) for record in records if record.get("delta") is not None]
    return {
        "total": len(records),
        "disagreement_count": disagreement,
        "disagreement_rate": disagreement / len(records) if records else None,
        "categories": dict(categories),
        "metric_eligible_count": len(deltas),
        "metric_missing_count": len(records) - len(deltas),
        "delta_mean": statistics.fmean(deltas) if deltas else None,
        "delta_median": statistics.median(deltas) if deltas else None,
        "sparse": len(records) < minimum,
    }


def _marginal(
    parameter: str,
    edges: list[float],
    records: list[dict[str, Any]],
    minimum: int,
) -> dict[str, Any]:
    bins = []
    for index, (lower, upper) in enumerate(zip(edges, edges[1:], strict=False)):
        selected = [record for record in records if record["bin_indices"][parameter] == index]
        bins.append(
            {
                "index": index,
                "lower": lower,
                "upper": upper,
                "upper_inclusive": index == len(edges) - 2,
                **_cell(selected, minimum),
            }
        )
    return {"parameter": parameter, "boundaries": edges, "bins": bins}


def _heatmaps(
    x_name: str,
    y_name: str,
    facet_name: str | None,
    boundaries: dict[str, list[float]],
    records: list[dict[str, Any]],
    minimum: int,
) -> list[dict[str, Any]]:
    facet_indices: list[int | None] = [None]
    if facet_name:
        facet_indices.extend(range(len(boundaries[facet_name]) - 1))
    heatmaps = []
    for facet_index in facet_indices:
        selected_facet = [
            record
            for record in records
            if facet_index is None or record["bin_indices"][facet_name] == facet_index
        ]
        cells = []
        for y_index in range(len(boundaries[y_name]) - 1):
            for x_index in range(len(boundaries[x_name]) - 1):
                selected = [
                    record
                    for record in selected_facet
                    if record["bin_indices"][x_name] == x_index
                    and record["bin_indices"][y_name] == y_index
                ]
                cells.append(
                    {"x_index": x_index, "y_index": y_index, **_cell(selected, minimum)}
                )
        facet_interval = None
        if facet_name and facet_index is not None:
            facet_interval = {
                "lower": boundaries[facet_name][facet_index],
                "upper": boundaries[facet_name][facet_index + 1],
                "upper_inclusive": facet_index == len(boundaries[facet_name]) - 2,
            }
        heatmaps.append(
            {
                "x": x_name,
                "y": y_name,
                "x_boundaries": boundaries[x_name],
                "y_boundaries": boundaries[y_name],
                "facet": facet_name,
                "facet_index": facet_index,
                "facet_interval": facet_interval,
                "total": len(selected_facet),
                "cells": cells,
            }
        )
    return heatmaps


def _observations(
    left: str,
    right: str,
    metric: str | None,
    overview: dict[str, Any],
    marginals: list[dict[str, Any]],
    heatmaps: list[dict[str, Any]],
    minimum: int,
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for marginal in marginals:
        eligible = [
            cell
            for cell in marginal["bins"]
            if cell["total"] >= minimum and cell["disagreement_rate"] is not None
        ]
        if not eligible:
            continue
        maximum = max(eligible, key=lambda cell: (cell["disagreement_rate"], cell["total"]))
        observations.append(
            {
                "kind": "marginal_disagreement",
                "text": (
                    f"The highest eligible observed disagreement interval for "
                    f"{marginal['parameter']} was {_interval_text(maximum)}: "
                    f"{maximum['disagreement_count']}/{maximum['total']} "
                    f"({100 * maximum['disagreement_rate']:.1f}%)."
                ),
                "parameter": marginal["parameter"],
                "numerator": maximum["disagreement_count"],
                "denominator": maximum["total"],
            }
        )
    direction_counts = {
        "left_success_right_fail": overview["categories"].get("left_success_right_fail", 0),
        "left_fail_right_success": overview["categories"].get("left_fail_right_success", 0),
    }
    if sum(direction_counts.values()):
        dominant = max(direction_counts, key=direction_counts.get)
        label = (
            f"{left} Success / {right} Fail"
            if dominant == "left_success_right_fail"
            else f"{left} Fail / {right} Success"
        )
        observations.append(
            {
                "kind": "direction",
                "text": (
                    f"Direct Success/Fail reversals were mainly {label}: "
                    f"{direction_counts[dominant]}/{sum(direction_counts.values())}."
                ),
                "numerator": direction_counts[dominant],
                "denominator": sum(direction_counts.values()),
            }
        )
    eligible_cells = [
        (heatmap, cell)
        for heatmap in heatmaps
        for cell in heatmap["cells"]
        if cell["total"] >= minimum
    ]
    overall_cells = [
        item
        for item in eligible_cells
        if item[0].get("facet_index") is None
        and item[1].get("disagreement_rate") is not None
    ]
    if overall_cells:
        heatmap, cell = max(
            overall_cells,
            key=lambda item: (item[1]["disagreement_rate"], item[1]["total"]),
        )
        x_index = int(cell["x_index"])
        y_index = int(cell["y_index"])
        x_interval = _edges_interval_text(heatmap["x_boundaries"], x_index)
        y_interval = _edges_interval_text(heatmap["y_boundaries"], y_index)
        observations.append(
            {
                "kind": "joint_disagreement",
                "text": (
                    f"The highest eligible observed {heatmap['x']} by {heatmap['y']} cell "
                    f"was {heatmap['x']} {x_interval} and {heatmap['y']} {y_interval}: "
                    f"{cell['disagreement_count']}/{cell['total']} "
                    f"({100 * cell['disagreement_rate']:.1f}%)."
                ),
                "numerator": cell["disagreement_count"],
                "denominator": cell["total"],
            }
        )
    if metric:
        delta_cells = [
            item for item in eligible_cells if item[1].get("delta_median") is not None
        ]
        if delta_cells:
            heatmap, cell = max(delta_cells, key=lambda item: abs(item[1]["delta_median"]))
            observations.append(
                {
                    "kind": "metric_delta",
                    "text": (
                        f"The largest eligible absolute regional median delta for {metric} "
                        f"was {cell['delta_median']:.6g} ({right} minus {left}), with "
                        f"{cell['metric_eligible_count']}/{cell['total']} paired metric values "
                        f"and {cell['disagreement_count']}/{cell['total']} outcome disagreements."
                    ),
                    "numerator": cell["metric_eligible_count"],
                    "denominator": cell["total"],
                }
            )
    return observations


def _candidates(records: list[dict[str, Any]], metric: str | None) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    reversal = next(
        (
            record
            for record in records
            if record["category"]
            in {"left_success_right_fail", "left_fail_right_success"}
        ),
        None,
    )
    if reversal:
        candidates.append(_candidate("outcome_reversal", reversal, "Direct outcome reversal"))
    if metric:
        same_metric = [
            record
            for record in records
            if record["category"] == "same_outcome" and record.get("delta") is not None
        ]
        if same_metric:
            record = max(same_metric, key=lambda item: abs(item["delta"]))
            candidates.append(
                _candidate(
                    "same_outcome_large_metric_delta",
                    record,
                    f"Same outcome with |delta|={abs(record['delta']):.6g}",
                )
            )
    different_stop = next(
        (
            record
            for record in records
            if record["left_outcome"] == record["right_outcome"]
            and (
                record.get("left_stop_condition") != record.get("right_stop_condition")
                or record.get("left_stop_reason") != record.get("right_stop_reason")
            )
        ),
        None,
    )
    if different_stop:
        candidates.append(
            _candidate("different_termination", different_stop, "Same outcome with different termination")
        )
    return candidates


def _candidate(kind: str, record: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "reason": reason,
        "parameter_hash": record["parameter_hash"],
        "left_run_id": record["left_run_id"],
        "right_run_id": record["right_run_id"],
        "left_outcome": record["left_outcome"],
        "right_outcome": record["right_outcome"],
        "delta": record.get("delta"),
        "parameters": record["parameters"],
    }


def _selected_name(value: Any, available: list[str], default: str | None, label: str) -> str:
    selected = str(value) if value not in {None, ""} else default
    if selected is None or selected not in available:
        raise PairedParameterError(f"{label} must be a shared original numeric parameter")
    return selected


def _bounded_integer(value: Any, minimum: int, maximum: int, label: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise PairedParameterError(f"{label} must be an integer") from exc
    if result < minimum or result > maximum:
        raise PairedParameterError(f"{label} must be between {minimum} and {maximum}")
    return result


def _optional_range(value: Any, label: str) -> list[float] | None:
    if value is None or value == "":
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise PairedParameterError(f"{label} must contain two numeric values")
    lower, upper = float(value[0]), float(value[1])
    if not math.isfinite(lower) or not math.isfinite(upper) or lower > upper:
        raise PairedParameterError(f"{label} must be finite and increasing")
    return [lower, upper]


def _interval_text(cell: Mapping[str, Any]) -> str:
    closing = "]" if cell.get("upper_inclusive") else ")"
    return f"[{cell['lower']:.6g}, {cell['upper']:.6g}{closing}"


def _edges_interval_text(edges: list[float], index: int) -> str:
    return _interval_text(
        {
            "lower": edges[index],
            "upper": edges[index + 1],
            "upper_inclusive": index == len(edges) - 2,
        }
    )
