from __future__ import annotations

import csv
import hashlib
import heapq
import json
import math
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pisa_sample_tools.reporting.index import REPORT_INDEX_SCHEMA_VERSION

DEFAULT_MAXIMUM_POINTS = 20_000
DEFAULT_MAXIMUM_PARAMETERS = 12
SUPPORTED_FORMATS = frozenset({"png", "svg", "pdf", "csv", "json"})
SUPPORTED_PRESETS = frozenset({"paper-single", "paper-double", "slides-hd", "slides-4k"})
SUPPORTED_SECTIONS = frozenset(
    {"all", "overview", "sampling", "outcomes", "performance", "sensitivity"}
)

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_OUTCOMES = ("success", "fail", "invalid", "unknown")
_OUTCOME_LABELS = {
    "success": "Success",
    "fail": "Failure",
    "invalid": "Invalid",
    "unknown": "Unknown",
}
_OUTCOME_COLORS = {
    "success": "#0f9d7a",
    "fail": "#dc4c64",
    "invalid": "#e29a2d",
    "unknown": "#8a94a6",
}
_COMPARISON_SECTION = re.compile(r"^compare:([a-f0-9]{20})$")
_COMPARISON_CHART = re.compile(r"^comparison-(?:outcomes|transitions|delta)-([a-f0-9]{20})(?:-|$)")
_PAIRED_RELATION_ROLES = frozenset(
    {
        "paired_replicate",
        "paired_system_intervention",
        "paired_policy_intervention",
        "partial_pair",
    }
)
_REQUIRED_TABLES = frozenset({"metadata", "datasets", "runs", "parameters", "findings"})
_MIME_TYPES = {
    "png": "image/png",
    "svg": "image/svg+xml",
    "pdf": "application/pdf",
    "csv": "text/csv",
    "json": "application/json",
}


class VisualizationError(ValueError):
    """Raised when a normalized report cannot produce a requested visualization."""


@dataclass(frozen=True)
class _Preset:
    figure_size: tuple[float, float]
    dpi: int
    font_size: float


_PRESETS = {
    # 85 mm and 180 mm are conventional single- and double-column widths.
    "paper-single": _Preset((3.346, 2.55), 300, 8.0),
    "paper-double": _Preset((7.087, 4.25), 300, 9.0),
    # Ten inches at these DPIs gives exact 1920x1080 and 3840x2160 canvases.
    "slides-hd": _Preset((10.0, 5.625), 192, 11.0),
    "slides-4k": _Preset((10.0, 5.625), 384, 11.0),
}


@dataclass(frozen=True)
class _Chart:
    identifier: str
    title: str
    subtitle: str
    kind: str
    option: dict[str, Any]
    rows: tuple[dict[str, Any], ...]
    disclosure: dict[str, Any]
    render_kind: str
    x_label: str | None = None
    y_label: str | None = None
    raw_range: tuple[float, float] | None = None

    def spec(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.identifier,
            "title": self.title,
            "subtitle": self.subtitle,
            "kind": self.kind,
            "option": self.option,
            "data_hash": _data_hash(self.rows),
            "disclosure": self.disclosure,
        }
        clipped = int(self.disclosure.get("clipped_count") or 0)
        if clipped:
            result["clipped_count"] = clipped
        if self.raw_range is not None:
            result["raw_range"] = list(self.raw_range)
        return result


@dataclass(frozen=True)
class _MetricDescriptor:
    name: str
    population_count: int
    semantic_role: str
    unit: str | None
    risk_direction: str | None
    priority: int


@dataclass
class _PrioritySample:
    limit: int
    namespace: str
    population_count: int = 0
    nonfinite_count: int = 0
    minimum: float | None = None
    maximum: float | None = None
    _heap: list[tuple[int, str, dict[str, Any]]] = field(default_factory=list)

    def add(self, stable_key: str, payload: dict[str, Any], *values: float) -> None:
        if not values or not all(math.isfinite(value) for value in values):
            self.nonfinite_count += 1
            return
        self.population_count += 1
        if len(values) == 1:
            value = values[0]
            self.minimum = value if self.minimum is None else min(self.minimum, value)
            self.maximum = value if self.maximum is None else max(self.maximum, value)
        score = _sample_score(self.namespace, stable_key)
        item = (-score, stable_key, payload)
        if len(self._heap) < self.limit:
            heapq.heappush(self._heap, item)
        elif score < -self._heap[0][0]:
            heapq.heapreplace(self._heap, item)

    def rows(self) -> list[dict[str, Any]]:
        return [
            payload
            for _negative_score, _key, payload in sorted(
                self._heap, key=lambda item: (-item[0], item[1])
            )
        ]


def build_visualizations(
    report_dir: str | Path,
    *,
    section: str = "all",
    maximum_points: int = DEFAULT_MAXIMUM_POINTS,
    maximum_parameters: int = DEFAULT_MAXIMUM_PARAMETERS,
) -> list[dict[str, Any]]:
    """Return deterministic ECharts-compatible specs from a normalized report.

    The SQLite store is opened read-only and only the fixed ``report/index.sqlite``
    member of ``report_dir`` is inspected. Duplicate-alias datasets are excluded
    from aggregate charts in the same way as the normalized report summary.
    """

    section = _validate_section(section)
    maximum_points = _bounded_integer(maximum_points, "maximum_points", minimum=1, maximum=100_000)
    maximum_parameters = _bounded_integer(
        maximum_parameters, "maximum_parameters", minimum=1, maximum=50
    )
    with _report_connection(report_dir) as (_root, connection):
        return [
            chart.spec()
            for chart in _build_charts(
                connection,
                section=section,
                maximum_points=maximum_points,
                maximum_parameters=maximum_parameters,
            )
        ]


def export_visualization(
    report_dir: str | Path,
    visualization_id: str,
    *,
    format: str,
    preset: str = "paper-single",
    dpi: int | None = None,
    background: str = "white",
    maximum_points: int = DEFAULT_MAXIMUM_POINTS,
    maximum_parameters: int = DEFAULT_MAXIMUM_PARAMETERS,
) -> dict[str, Any]:
    """Atomically export one generated chart or its underlying plotted data.

    Output names are derived solely from the stable visualization id and validated
    options. Files are always written below ``exports/visualizations`` in the report;
    callers cannot supply an arbitrary output path or filename.
    """

    identifier = _validate_identifier(visualization_id)
    export_format = str(format).strip().lower()
    if export_format not in SUPPORTED_FORMATS:
        raise VisualizationError(
            f"unsupported export format {format!r}; choose one of "
            f"{', '.join(sorted(SUPPORTED_FORMATS))}"
        )
    preset_name = str(preset).strip().lower()
    if preset_name not in SUPPORTED_PRESETS:
        raise VisualizationError(
            f"unsupported publication preset {preset!r}; choose one of "
            f"{', '.join(sorted(SUPPORTED_PRESETS))}"
        )
    if background not in {"white", "transparent"}:
        raise VisualizationError("background must be 'white' or 'transparent'")
    maximum_points = _bounded_integer(maximum_points, "maximum_points", minimum=1, maximum=100_000)
    maximum_parameters = _bounded_integer(
        maximum_parameters, "maximum_parameters", minimum=1, maximum=50
    )
    resolved_dpi = _PRESETS[preset_name].dpi
    if dpi is not None:
        resolved_dpi = _bounded_integer(dpi, "dpi", minimum=72, maximum=1_200)

    with _report_connection(report_dir, require_current_schema=True) as (root, connection):
        section = _section_for_identifier(identifier)
        chart = next(
            (
                item
                for item in _build_charts(
                    connection,
                    section=section,
                    maximum_points=maximum_points,
                    maximum_parameters=maximum_parameters,
                )
                if item.identifier == identifier
            ),
            None,
        )
        if chart is None:
            raise VisualizationError(f"unknown visualization id {identifier!r}")

        output_dir = (root / "exports" / "visualizations").resolve()
        if not output_dir.is_relative_to(root):  # protects against an exported-directory symlink
            raise VisualizationError("report export directory escapes the report root")
        output_dir.mkdir(parents=True, exist_ok=True)
        resolved_output = output_dir.resolve()
        if not resolved_output.is_relative_to(root):
            raise VisualizationError("report export directory escapes the report root")

        if export_format in {"csv", "json"}:
            filename = f"{identifier}.{export_format}"
        else:
            background_suffix = "transparent" if background == "transparent" else "white"
            dpi_suffix = f"-{resolved_dpi}dpi" if export_format == "png" else ""
            filename = (
                f"{identifier}--{preset_name}{dpi_suffix}--{background_suffix}.{export_format}"
            )
        target = (resolved_output / filename).resolve()
        if target.parent != resolved_output:
            raise VisualizationError("invalid generated export path")

        if export_format == "csv":
            _atomic_csv(target, chart)
        elif export_format == "json":
            _atomic_json(target, chart)
        else:
            _atomic_figure(
                target,
                chart,
                export_format=export_format,
                preset=_PRESETS[preset_name],
                dpi=resolved_dpi,
                background=background,
            )

        return {
            "visualization_id": identifier,
            "format": export_format,
            "preset": preset_name if export_format not in {"csv", "json"} else None,
            "dpi": resolved_dpi if export_format == "png" else None,
            "background": background if export_format not in {"csv", "json"} else None,
            "path": target.relative_to(root).as_posix(),
            "size": target.stat().st_size,
            "mime_type": _MIME_TYPES[export_format],
            "data_hash": _data_hash(chart.rows),
            "disclosure": chart.disclosure,
        }


@contextmanager
def _report_connection(
    report_dir: str | Path, *, require_current_schema: bool = False
) -> Iterator[tuple[Path, sqlite3.Connection]]:
    root = Path(report_dir).expanduser().resolve()
    if not root.is_dir():
        raise VisualizationError(f"report directory does not exist: {root}")
    database = (root / "report" / "index.sqlite").resolve()
    if not database.is_relative_to(root) or not database.is_file():
        raise VisualizationError(
            "normalized report index is missing or escapes the report directory"
        )
    uri_path = quote(database.as_posix(), safe="/")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        schema_version = _validate_database(connection)
        if require_current_schema and schema_version > REPORT_INDEX_SCHEMA_VERSION:
            raise VisualizationError(
                "refusing to export into a report with a newer store schema; open it read-only"
            )
    except VisualizationError:
        if connection is not None:
            connection.close()
        raise
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise VisualizationError(f"invalid normalized report index: {exc}") from exc
    try:
        yield root, connection
    except sqlite3.Error as exc:
        raise VisualizationError(f"normalized report query failed: {exc}") from exc
    finally:
        connection.close()


def _validate_database(connection: sqlite3.Connection) -> int:
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
    }
    missing = sorted(_REQUIRED_TABLES - tables)
    if missing:
        raise VisualizationError(f"report index is missing tables: {', '.join(missing)}")
    metadata = {
        str(row[0]): str(row[1]) for row in connection.execute("SELECT key, value FROM metadata")
    }
    try:
        schema_version = int(metadata["schema_version"])
    except (KeyError, ValueError) as exc:
        raise VisualizationError("report index has no valid schema version") from exc
    if schema_version < 1:
        raise VisualizationError(f"unsupported report index schema {schema_version}")
    return schema_version


def _build_charts(
    connection: sqlite3.Connection,
    *,
    section: str,
    maximum_points: int,
    maximum_parameters: int,
) -> list[_Chart]:
    comparison_match = _COMPARISON_SECTION.fullmatch(section)
    if comparison_match:
        return _comparison_charts(
            connection,
            comparison_match.group(1),
            maximum_points=maximum_points,
            maximum_parameters=maximum_parameters,
        )

    charts: list[_Chart] = []
    if section in {"all", "overview", "outcomes"}:
        charts.extend(_outcome_charts(connection))
    if section in {"all", "outcomes"}:
        charts.extend(
            _safety_charts(
                connection,
                maximum_points=maximum_points,
                maximum_parameters=maximum_parameters,
            )
        )
    if section in {"all", "performance"}:
        charts.extend(
            _performance_charts(
                connection,
                maximum_points=maximum_points,
                maximum_parameters=maximum_parameters,
            )
        )
    if section in {"all", "sampling"}:
        charts.extend(
            _parameter_charts(
                connection,
                maximum_points=maximum_points,
                maximum_parameters=maximum_parameters,
            )
        )
    return charts


def _outcome_charts(connection: sqlite3.Connection) -> list[_Chart]:
    alias_count = _alias_count(connection)
    totals = connection.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(r.outcome_class = 'success') AS success,
               SUM(r.outcome_class = 'fail') AS fail,
               SUM(r.outcome_class = 'invalid') AS invalid,
               SUM(r.outcome_class = 'unknown') AS unknown,
               SUM(r.has_collision = 1) AS collision
        FROM runs AS r
        WHERE NOT EXISTS (
            SELECT 1 FROM dataset_relations AS relation
            WHERE relation.role = 'duplicate_alias'
              AND relation.right_dataset_id = r.dataset_id
        )
        """
    ).fetchone()
    total = int(totals["total"] or 0)
    rows = tuple(
        {
            "outcome": outcome,
            "label": _OUTCOME_LABELS[outcome],
            "count": int(totals[outcome] or 0),
            "percentage": (round(100.0 * int(totals[outcome] or 0) / total, 6) if total else 0.0),
        }
        for outcome in _OUTCOMES
    )
    option = {
        "animation": False,
        "aria": {"enabled": True},
        "grid": {"top": 24, "right": 24, "bottom": 54, "left": 64},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "xAxis": {
            "type": "category",
            "name": "Outcome class",
            "nameLocation": "middle",
            "nameGap": 34,
            "data": [row["label"] for row in rows],
        },
        "yAxis": {"type": "value", "name": "Canonical runs", "minInterval": 1},
        "series": [
            {
                "type": "bar",
                "name": "Runs",
                "data": [
                    {
                        "value": row["count"],
                        "itemStyle": {"color": _OUTCOME_COLORS[str(row["outcome"])]},
                    }
                    for row in rows
                ],
                "label": {"show": True, "position": "top"},
            }
        ],
    }
    overall = _Chart(
        identifier="outcomes-overall",
        title="Outcome distribution",
        subtitle=f"{total:,} canonical runs; collision is reported separately as an overlapping flag.",
        kind="bar",
        option=option,
        rows=rows,
        disclosure={
            "population_count": total,
            "plotted_count": total,
            "sampled": False,
            "collision_count": int(totals["collision"] or 0),
            "excluded_duplicate_alias_datasets": alias_count,
            "aggregation": "canonical runs grouped by normalized outcome class",
        },
        render_kind="outcomes-overall",
        x_label="Outcome class",
        y_label="Canonical runs",
    )

    dataset_rows = tuple(
        {
            "dataset_id": str(row["dataset_id"]),
            **{outcome: int(row[outcome] or 0) for outcome in _OUTCOMES},
            "total": int(row["total"] or 0),
        }
        for row in connection.execute(
            """
            SELECT d.dataset_id,
                   COUNT(r.run_id) AS total,
                   SUM(r.outcome_class = 'success') AS success,
                   SUM(r.outcome_class = 'fail') AS fail,
                   SUM(r.outcome_class = 'invalid') AS invalid,
                   SUM(r.outcome_class = 'unknown') AS unknown
            FROM datasets AS d
            LEFT JOIN runs AS r ON r.dataset_id = d.dataset_id
            WHERE NOT EXISTS (
                SELECT 1 FROM dataset_relations AS relation
                WHERE relation.role = 'duplicate_alias'
                  AND relation.right_dataset_id = d.dataset_id
            )
            GROUP BY d.dataset_id
            ORDER BY d.dataset_id
            """
        )
    )
    dataset_option = {
        "animation": False,
        "aria": {"enabled": True},
        "legend": {"top": 0},
        "grid": {"top": 44, "right": 30, "bottom": 48, "left": 150, "containLabel": True},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "xAxis": {"type": "value", "name": "Canonical runs", "minInterval": 1},
        "yAxis": {
            "type": "category",
            "data": [row["dataset_id"] for row in dataset_rows],
        },
        "dataZoom": (
            [{"type": "inside", "yAxisIndex": 0}, {"type": "slider", "yAxisIndex": 0}]
            if len(dataset_rows) > 15
            else []
        ),
        "series": [
            {
                "type": "bar",
                "name": _OUTCOME_LABELS[outcome],
                "stack": "outcomes",
                "itemStyle": {"color": _OUTCOME_COLORS[outcome]},
                "data": [row[outcome] for row in dataset_rows],
            }
            for outcome in _OUTCOMES
        ],
    }
    by_dataset = _Chart(
        identifier="outcomes-by-dataset",
        title="Outcomes by dataset",
        subtitle="Normalized outcome classes are stacked without double-counting duplicate aliases.",
        kind="bar",
        option=dataset_option,
        rows=dataset_rows,
        disclosure={
            "population_count": sum(int(row["total"]) for row in dataset_rows),
            "plotted_count": sum(int(row["total"]) for row in dataset_rows),
            "dataset_count": len(dataset_rows),
            "sampled": False,
            "excluded_duplicate_alias_datasets": alias_count,
            "aggregation": "canonical runs grouped by dataset and normalized outcome class",
        },
        render_kind="outcomes-by-dataset",
        x_label="Canonical runs",
        y_label="Dataset",
    )
    return [overall, by_dataset]


def _safety_charts(
    connection: sqlite3.Connection,
    *,
    maximum_points: int,
    maximum_parameters: int,
) -> list[_Chart]:
    charts = [_collision_rate_chart(connection)]
    descriptors = _rank_metrics(
        connection,
        family="safety",
        limit=min(4, maximum_parameters),
    )
    charts.extend(
        _metric_distribution_charts(
            connection,
            descriptors,
            identifier_prefix="safety",
            maximum_points=maximum_points,
        )
    )
    return charts


def _performance_charts(
    connection: sqlite3.Connection,
    *,
    maximum_points: int,
    maximum_parameters: int,
) -> list[_Chart]:
    descriptors = _rank_metrics(
        connection,
        family="performance",
        limit=min(4, maximum_parameters),
    )
    charts = _metric_distribution_charts(
        connection,
        descriptors,
        identifier_prefix="performance",
        maximum_points=maximum_points,
    )
    wall = next(
        (item for item in descriptors if item.semantic_role == "performance.wall_clock_duration"),
        None,
    )
    simulated = next(
        (item for item in descriptors if item.semantic_role == "performance.simulated_duration"),
        None,
    )
    if wall is not None and simulated is not None:
        charts.append(
            _metric_scatter_chart(
                connection,
                wall,
                simulated,
                maximum_points=maximum_points,
            )
        )
    return charts


def _collision_rate_chart(connection: sqlite3.Connection) -> _Chart:
    alias_count = _alias_count(connection)
    rows = tuple(
        {
            "dataset_id": str(row["dataset_id"]),
            "run_count": int(row["run_count"] or 0),
            "collision_count": int(row["collision_count"] or 0),
            "collision_rate_percent": (
                round(100.0 * int(row["collision_count"] or 0) / int(row["run_count"]), 6)
                if int(row["run_count"] or 0)
                else None
            ),
        }
        for row in connection.execute(
            """
            SELECT d.dataset_id, COUNT(r.run_id) AS run_count,
                   SUM(CASE WHEN r.has_collision = 1 THEN 1 ELSE 0 END) AS collision_count
            FROM datasets AS d
            LEFT JOIN runs AS r ON r.dataset_id = d.dataset_id
            WHERE NOT EXISTS (
                SELECT 1 FROM dataset_relations AS relation
                WHERE relation.role = 'duplicate_alias'
                  AND relation.right_dataset_id = d.dataset_id
            )
            GROUP BY d.dataset_id
            ORDER BY d.dataset_id
            """
        )
    )
    populated = [row for row in rows if int(row["run_count"]) > 0]
    option = {
        "animation": False,
        "aria": {"enabled": True},
        "grid": {"top": 24, "right": 30, "bottom": 58, "left": 72},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "xAxis": {
            "type": "category",
            "name": "Dataset",
            "nameLocation": "middle",
            "nameGap": 40,
            "data": [row["dataset_id"] for row in rows],
            "axisLabel": {"rotate": 28 if len(rows) > 6 else 0},
        },
        "yAxis": {"type": "value", "name": "Collision rate (%)", "min": 0, "max": 100},
        "dataZoom": (
            [{"type": "inside", "xAxisIndex": 0}, {"type": "slider", "xAxisIndex": 0}]
            if len(rows) > 15
            else []
        ),
        "series": [
            {
                "type": "bar",
                "name": "Collision rate",
                "itemStyle": {"color": "#dc4c64"},
                "data": [row["collision_rate_percent"] for row in rows],
            }
        ],
    }
    population = sum(int(row["run_count"]) for row in populated)
    return _Chart(
        identifier="safety-collision-rate-by-dataset",
        title="Collision rate by dataset",
        subtitle="Collision is an overlapping run flag; datasets with no runs remain undefined.",
        kind="bar",
        option=option,
        rows=rows,
        disclosure={
            "population_count": population,
            "plotted_count": population,
            "dataset_count": len(populated),
            "undefined_empty_dataset_count": len(rows) - len(populated),
            "sampled": False,
            "missing_values_are_zero": False,
            "semantic_role": "safety.collision_rate",
            "aggregation": "collision-flagged canonical runs divided by all canonical runs",
            "excluded_duplicate_alias_datasets": alias_count,
        },
        render_kind="collision-rate",
        x_label="Dataset",
        y_label="Collision rate (%)",
    )


def _rank_metrics(
    connection: sqlite3.Connection,
    *,
    family: str,
    limit: int,
    dataset_ids: tuple[str, str] | None = None,
) -> list[_MetricDescriptor]:
    if not _has_table(connection, "metrics"):
        return []
    arguments: list[Any] = []
    dataset_clause = ""
    alias_clause = """
          AND NOT EXISTS (
              SELECT 1 FROM dataset_relations AS relation
              WHERE relation.role = 'duplicate_alias'
                AND relation.right_dataset_id = r.dataset_id
          )
    """
    if dataset_ids is not None:
        dataset_clause = "AND r.dataset_id IN (?, ?)"
        arguments.extend(dataset_ids)
        alias_clause = ""
    rows = connection.execute(
        f"""
        SELECT m.name, COUNT(*) AS numeric_count
        FROM metrics AS m
        JOIN runs AS r ON r.run_id = m.run_id
        WHERE m.value_type = 'number' AND m.value_real IS NOT NULL
          {dataset_clause}
          {alias_clause}
        GROUP BY m.name
        """,
        arguments,
    ).fetchall()
    descriptors: list[_MetricDescriptor] = []
    for row in rows:
        name = str(row["name"])
        semantics = _metric_semantics(name, family)
        if semantics is None:
            continue
        priority, role, unit, risk_direction = semantics
        descriptors.append(
            _MetricDescriptor(
                name=name,
                population_count=int(row["numeric_count"] or 0),
                semantic_role=role,
                unit=unit,
                risk_direction=risk_direction,
                priority=priority,
            )
        )
    descriptors.sort(
        key=lambda item: (item.priority, -item.population_count, item.name.casefold(), item.name)
    )
    selected: list[_MetricDescriptor] = []
    seen_roles: set[str] = set()
    for descriptor in descriptors:
        if descriptor.semantic_role in seen_roles:
            continue
        selected.append(descriptor)
        seen_roles.add(descriptor.semantic_role)
        if len(selected) == limit:
            return selected
    for descriptor in descriptors:
        if descriptor not in selected:
            selected.append(descriptor)
        if len(selected) == limit:
            break
    return selected


def _metric_semantics(name: str, family: str) -> tuple[int, str, str | None, str | None] | None:
    lowered = name.casefold()
    unit = _metric_unit(lowered)
    if family == "performance":
        if "wall" in lowered and any(token in lowered for token in ("time", "duration")):
            return 0, "performance.wall_clock_duration", unit, None
        if any(
            token in lowered
            for token in ("final_sim_time", "simulated_time", "simulation_time", "sim_duration")
        ):
            return 1, "performance.simulated_duration", unit, None
        if "speedup" in lowered or "real_time_factor" in lowered:
            return 2, "performance.throughput_ratio", unit or "ratio", None
        if "runtime" in lowered or "duration" in lowered or "elapsed" in lowered:
            return 3, "performance.runtime_metric", unit, None
        if "total_steps" in lowered or lowered.endswith(".steps"):
            return 4, "performance.work_units", unit or "steps", None
        return None
    if family != "safety":  # pragma: no cover - internal invariant
        raise VisualizationError(f"unknown metric family {family!r}")
    if any(token in lowered for token in ("step_index", ".count", "_count")):
        return None
    if "collision" in lowered:
        return None  # the normalized collision flag is authoritative and plotted separately
    if "ttc" in lowered or "time_to_collision" in lowered:
        return 0, "safety.time_to_collision", unit or "s", "lower_is_riskier"
    if "distance" in lowered:
        priority = 1 if any(token in lowered for token in (".min", "minimum", "min_")) else 2
        return priority, "safety.separation_distance", unit or "m", "lower_is_riskier"
    if "thw" in lowered or "time_headway" in lowered:
        return 3, "safety.time_headway", unit or "s", "lower_is_riskier"
    if "drac" in lowered:
        return 4, "safety.required_deceleration", unit or "m/s²", "higher_is_riskier"
    if "deceleration" in lowered:
        if "sim_time" in lowered:
            return None
        return 5, "safety.deceleration", unit or "m/s²", "higher_is_riskier"
    return None


def _metric_unit(lowered_name: str) -> str | None:
    if "mps2" in lowered_name or "m_s2" in lowered_name:
        return "m/s²"
    if "mps" in lowered_name:
        return "m/s"
    if lowered_name.endswith("_ms") or "time_ms" in lowered_name:
        return "ms"
    if lowered_name.endswith("_s") or "time_s" in lowered_name:
        return "s"
    if "distance" in lowered_name:
        return "m"
    if "speedup" in lowered_name or "real_time_factor" in lowered_name:
        return "ratio"
    if "steps" in lowered_name:
        return "steps"
    return None


def _metric_distribution_charts(
    connection: sqlite3.Connection,
    descriptors: Sequence[_MetricDescriptor],
    *,
    identifier_prefix: str,
    maximum_points: int,
) -> list[_Chart]:
    if not descriptors:
        return []
    total_runs = _aggregate_run_count(connection)
    accumulators = {
        descriptor.name: _PrioritySample(
            maximum_points,
            f"{identifier_prefix}-metric-distribution\0{descriptor.name}",
        )
        for descriptor in descriptors
    }
    names = list(accumulators)
    placeholders = ",".join("?" for _ in names)
    for row in connection.execute(
        f"""
        SELECT m.name, m.run_id, r.dataset_id, m.value_real
        FROM metrics AS m
        JOIN runs AS r ON r.run_id = m.run_id
        WHERE m.name IN ({placeholders})
          AND m.value_type = 'number' AND m.value_real IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM dataset_relations AS relation
              WHERE relation.role = 'duplicate_alias'
                AND relation.right_dataset_id = r.dataset_id
          )
        """,
        names,
    ):
        name = str(row["name"])
        value = float(row["value_real"])
        accumulators[name].add(
            str(row["run_id"]),
            {
                "run_id": str(row["run_id"]),
                "dataset_id": str(row["dataset_id"]),
                "value": value,
            },
            value,
        )

    charts: list[_Chart] = []
    for descriptor in descriptors:
        accumulator = accumulators[descriptor.name]
        sampled = accumulator.rows()
        by_dataset: dict[str, list[float]] = {}
        for row in sampled:
            by_dataset.setdefault(str(row["dataset_id"]), []).append(float(row["value"]))
        summary_rows = tuple(
            {
                "dataset_id": dataset_id,
                **_five_number_summary(values),
                "sample_count": len(values),
            }
            for dataset_id, values in sorted(by_dataset.items())
        )
        clipped = max(0, accumulator.population_count - len(sampled))
        disclosure = {
            "metric": descriptor.name,
            "semantic_role": descriptor.semantic_role,
            "unit": descriptor.unit,
            "risk_direction": descriptor.risk_direction,
            "population_count": accumulator.population_count,
            "plotted_count": len(sampled),
            "sample_cap": maximum_points,
            "sampled": clipped > 0,
            "clipped_count": clipped,
            "sampling_method": (
                "deterministic SHA-256 priority sample" if clipped else "complete population"
            ),
            "missing_or_nonnumeric_count": max(0, total_runs - accumulator.population_count),
            "nonfinite_count": accumulator.nonfinite_count,
            "missing_values_are_zero": False,
            "aggregation": "five-number distribution summary by dataset over plotted finite values",
            "excluded_duplicate_alias_datasets": _alias_count(connection),
        }
        labels = [row["dataset_id"] for row in summary_rows]
        option = {
            "animation": False,
            "aria": {"enabled": True},
            "legend": {"top": 0},
            "grid": {"top": 44, "right": 30, "bottom": 64, "left": 72},
            "tooltip": {"trigger": "axis"},
            "xAxis": {
                "type": "category",
                "name": "Dataset",
                "nameLocation": "middle",
                "nameGap": 44,
                "data": labels,
                "axisLabel": {"rotate": 28 if len(labels) > 6 else 0},
            },
            "yAxis": {
                "type": "value",
                "name": _axis_metric_label(descriptor),
                "scale": True,
            },
            "dataZoom": (
                [{"type": "inside", "xAxisIndex": 0}, {"type": "slider", "xAxisIndex": 0}]
                if len(labels) > 15
                else []
            ),
            "series": [
                {
                    "type": "line",
                    "name": label,
                    "symbol": "circle" if key == "median" else "none",
                    "lineStyle": {
                        "width": 3 if key == "median" else 1,
                        "opacity": 1 if key == "median" else 0.55,
                    },
                    "data": [row[key] for row in summary_rows],
                }
                for key, label in (
                    ("minimum", "Minimum"),
                    ("q1", "Q1"),
                    ("median", "Median"),
                    ("q3", "Q3"),
                    ("maximum", "Maximum"),
                )
            ],
        }
        raw_range = (
            (accumulator.minimum, accumulator.maximum)
            if accumulator.minimum is not None and accumulator.maximum is not None
            else None
        )
        charts.append(
            _Chart(
                identifier=(f"{identifier_prefix}-distribution-{_stable_suffix(descriptor.name)}"),
                title=f"{_display_metric_name(descriptor.name)} by dataset",
                subtitle=_sampling_subtitle(disclosure)
                + " Missing or nonnumeric measurements are excluded, never imputed as zero.",
                kind="line",
                option=option,
                rows=summary_rows,
                disclosure=disclosure,
                render_kind="metric-distribution",
                x_label="Dataset",
                y_label=_axis_metric_label(descriptor),
                raw_range=raw_range,
            )
        )
    return charts


def _metric_scatter_chart(
    connection: sqlite3.Connection,
    x_metric: _MetricDescriptor,
    y_metric: _MetricDescriptor,
    *,
    maximum_points: int,
) -> _Chart:
    accumulator = _PrioritySample(
        maximum_points, f"performance-scatter\0{x_metric.name}\0{y_metric.name}"
    )
    for row in connection.execute(
        """
        SELECT r.run_id, r.dataset_id, r.outcome_class,
               x.value_real AS x_value, y.value_real AS y_value
        FROM runs AS r
        JOIN metrics AS x ON x.run_id = r.run_id AND x.name = ?
        JOIN metrics AS y ON y.run_id = r.run_id AND y.name = ?
        WHERE x.value_type = 'number' AND x.value_real IS NOT NULL
          AND y.value_type = 'number' AND y.value_real IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM dataset_relations AS relation
              WHERE relation.role = 'duplicate_alias'
                AND relation.right_dataset_id = r.dataset_id
          )
        """,
        (x_metric.name, y_metric.name),
    ):
        x_value = float(row["x_value"])
        y_value = float(row["y_value"])
        outcome = str(row["outcome_class"])
        if outcome not in _OUTCOMES:
            outcome = "unknown"
        accumulator.add(
            str(row["run_id"]),
            {
                "run_id": str(row["run_id"]),
                "dataset_id": str(row["dataset_id"]),
                "outcome": outcome,
                "x": x_value,
                "y": y_value,
            },
            x_value,
            y_value,
        )
    rows = tuple(accumulator.rows())
    clipped = max(0, accumulator.population_count - len(rows))
    disclosure = {
        "x_metric": x_metric.name,
        "y_metric": y_metric.name,
        "x_semantic_role": x_metric.semantic_role,
        "y_semantic_role": y_metric.semantic_role,
        "population_count": accumulator.population_count,
        "plotted_count": len(rows),
        "sample_cap": maximum_points,
        "sampled": clipped > 0,
        "clipped_count": clipped,
        "sampling_method": (
            "deterministic SHA-256 priority sample" if clipped else "complete population"
        ),
        "missing_values_are_zero": False,
        "unpaired_or_nonnumeric_count": max(
            0, _aggregate_run_count(connection) - accumulator.population_count
        ),
        "color_encoding": "normalized outcome class",
    }
    x_label = _axis_metric_label(x_metric)
    y_label = _axis_metric_label(y_metric)
    option = {
        "animation": False,
        "aria": {"enabled": True},
        "color": [_OUTCOME_COLORS[outcome] for outcome in _OUTCOMES],
        "legend": {"top": 0},
        "grid": {"top": 44, "right": 24, "bottom": 62, "left": 76},
        "tooltip": {"trigger": "item"},
        "xAxis": {
            "type": "value",
            "name": x_label,
            "nameLocation": "middle",
            "nameGap": 40,
            "scale": True,
        },
        "yAxis": {
            "type": "value",
            "name": y_label,
            "nameLocation": "middle",
            "nameGap": 52,
            "scale": True,
        },
        "series": [
            {
                "type": "scatter",
                "name": _OUTCOME_LABELS[outcome],
                "symbolSize": 6 if len(rows) <= 2_000 else 3,
                "large": len(rows) > 2_000,
                "itemStyle": {"color": _OUTCOME_COLORS[outcome], "opacity": 0.7},
                "data": [
                    [row["x"], row["y"], row["run_id"], row["dataset_id"]]
                    for row in rows
                    if row["outcome"] == outcome
                ],
            }
            for outcome in _OUTCOMES
        ],
    }
    return _Chart(
        identifier=f"performance-scatter-{_stable_suffix(f'{x_metric.name}\0{y_metric.name}')}",
        title=f"{_display_metric_name(x_metric.name)} × {_display_metric_name(y_metric.name)}",
        subtitle=_sampling_subtitle(disclosure)
        + " Runs missing either measure are omitted, not placed at zero.",
        kind="scatter",
        option=option,
        rows=rows,
        disclosure=disclosure,
        render_kind="metric-scatter",
        x_label=x_label,
        y_label=y_label,
    )


def _comparison_charts(
    connection: sqlite3.Connection,
    relation_id: str,
    *,
    maximum_points: int,
    maximum_parameters: int,
) -> list[_Chart]:
    if not _has_table(connection, "dataset_relations"):
        raise VisualizationError("normalized report has no dataset comparison relations")
    relation: sqlite3.Row | None = None
    for row in connection.execute(
        """
        SELECT left_dataset_id, right_dataset_id, role, details_json
        FROM dataset_relations
        ORDER BY left_dataset_id, right_dataset_id
        """
    ):
        if (
            _comparison_identifier(str(row["left_dataset_id"]), str(row["right_dataset_id"]))
            == relation_id
        ):
            relation = row
            break
    if relation is None:
        raise VisualizationError(f"unknown comparison relation id {relation_id!r}")

    left = str(relation["left_dataset_id"])
    right = str(relation["right_dataset_id"])
    role = str(relation["role"])
    try:
        decoded_details = json.loads(str(relation["details_json"] or "{}"))
    except (json.JSONDecodeError, TypeError):
        decoded_details = {}
    details = decoded_details if isinstance(decoded_details, dict) else {}
    charts = [
        _comparison_outcome_chart(
            connection,
            relation_id=relation_id,
            left=left,
            right=right,
            role=role,
            details=details,
        )
    ]
    if role not in _PAIRED_RELATION_ROLES:
        return charts

    transitions, paired_count = _paired_outcome_transitions(connection, left, right)
    if paired_count:
        charts.append(
            _comparison_transition_chart(
                relation_id=relation_id,
                left=left,
                right=right,
                role=role,
                details=details,
                transitions=transitions,
                paired_count=paired_count,
            )
        )
        descriptors = _shared_comparison_metrics(
            connection,
            left,
            right,
            limit=min(3, maximum_parameters),
        )
        for descriptor in descriptors:
            chart = _comparison_delta_chart(
                connection,
                relation_id=relation_id,
                left=left,
                right=right,
                role=role,
                descriptor=descriptor,
                paired_count=paired_count,
                maximum_points=maximum_points,
            )
            if chart is not None:
                charts.append(chart)
    return charts


def _comparison_outcome_chart(
    connection: sqlite3.Connection,
    *,
    relation_id: str,
    left: str,
    right: str,
    role: str,
    details: Mapping[str, Any],
) -> _Chart:
    counts = {dataset_id: {outcome: 0 for outcome in _OUTCOMES} for dataset_id in (left, right)}
    for row in connection.execute(
        """
        SELECT dataset_id, outcome_class, COUNT(*) AS count
        FROM runs
        WHERE dataset_id IN (?, ?)
        GROUP BY dataset_id, outcome_class
        """,
        (left, right),
    ):
        dataset_id = str(row["dataset_id"])
        outcome = str(row["outcome_class"])
        if outcome not in _OUTCOMES:
            outcome = "unknown"
        counts[dataset_id][outcome] += int(row["count"] or 0)
    totals = {dataset_id: sum(values.values()) for dataset_id, values in counts.items()}
    rows = tuple(
        {
            "outcome": outcome,
            "label": _OUTCOME_LABELS[outcome],
            "left_dataset_id": left,
            "right_dataset_id": right,
            "left_count": counts[left][outcome],
            "right_count": counts[right][outcome],
            "left_rate_percent": (
                round(100.0 * counts[left][outcome] / totals[left], 6) if totals[left] else None
            ),
            "right_rate_percent": (
                round(100.0 * counts[right][outcome] / totals[right], 6) if totals[right] else None
            ),
        }
        for outcome in _OUTCOMES
    )
    option = {
        "animation": False,
        "aria": {"enabled": True},
        "legend": {"top": 0},
        "grid": {"top": 44, "right": 24, "bottom": 56, "left": 68},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "xAxis": {
            "type": "category",
            "name": "Outcome class",
            "nameLocation": "middle",
            "nameGap": 36,
            "data": [_OUTCOME_LABELS[outcome] for outcome in _OUTCOMES],
        },
        "yAxis": {"type": "value", "name": "Within-dataset rate (%)", "min": 0, "max": 100},
        "series": [
            {
                "type": "bar",
                "name": dataset_id,
                "data": [row[value_key] for row in rows],
                "itemStyle": {"color": color},
            }
            for dataset_id, value_key, color in (
                (left, "left_rate_percent", "#526ff0"),
                (right, "right_rate_percent", "#e05887"),
            )
        ],
    }
    return _Chart(
        identifier=f"comparison-outcomes-{relation_id}",
        title="Normalized outcome rates",
        subtitle=f"{left} versus {right}; rates use each dataset's own canonical-run denominator.",
        kind="bar",
        option=option,
        rows=rows,
        disclosure={
            "population_count": totals[left] + totals[right],
            "plotted_count": totals[left] + totals[right],
            "left_population_count": totals[left],
            "right_population_count": totals[right],
            "sampled": False,
            "relation_id": relation_id,
            "comparison_role": role,
            "semantic_compatible": details.get("semantic_compatible"),
            "normalization": "outcome count divided by canonical runs within each dataset",
            "claim_scope": "descriptive normalized outcome rates; no pairing or causal effect implied",
            "missing_values_are_zero": False,
        },
        render_kind="comparison-outcomes",
        x_label="Outcome class",
        y_label="Within-dataset rate (%)",
    )


def _paired_outcome_transitions(
    connection: sqlite3.Connection, left: str, right: str
) -> tuple[dict[tuple[str, str], int], int]:
    transitions: dict[tuple[str, str], int] = {}
    paired_count = 0
    for row in connection.execute(
        """
        WITH left_unique AS (
            SELECT parameter_hash, MIN(run_id) AS run_id
            FROM runs
            WHERE dataset_id = ? AND parameter_hash IS NOT NULL AND parameter_hash <> ''
            GROUP BY parameter_hash HAVING COUNT(*) = 1
        ),
        right_unique AS (
            SELECT parameter_hash, MIN(run_id) AS run_id
            FROM runs
            WHERE dataset_id = ? AND parameter_hash IS NOT NULL AND parameter_hash <> ''
            GROUP BY parameter_hash HAVING COUNT(*) = 1
        )
        SELECT left_run.outcome_class AS left_outcome,
               right_run.outcome_class AS right_outcome,
               COUNT(*) AS count
        FROM left_unique
        JOIN right_unique USING (parameter_hash)
        JOIN runs AS left_run ON left_run.run_id = left_unique.run_id
        JOIN runs AS right_run ON right_run.run_id = right_unique.run_id
        GROUP BY left_run.outcome_class, right_run.outcome_class
        """,
        (left, right),
    ):
        left_outcome = str(row["left_outcome"])
        right_outcome = str(row["right_outcome"])
        if left_outcome not in _OUTCOMES:
            left_outcome = "unknown"
        if right_outcome not in _OUTCOMES:
            right_outcome = "unknown"
        count = int(row["count"] or 0)
        transitions[(left_outcome, right_outcome)] = (
            transitions.get((left_outcome, right_outcome), 0) + count
        )
        paired_count += count
    return transitions, paired_count


def _comparison_transition_chart(
    *,
    relation_id: str,
    left: str,
    right: str,
    role: str,
    details: Mapping[str, Any],
    transitions: Mapping[tuple[str, str], int],
    paired_count: int,
) -> _Chart:
    left_totals = {
        left_outcome: sum(
            transitions.get((left_outcome, right_outcome), 0) for right_outcome in _OUTCOMES
        )
        for left_outcome in _OUTCOMES
    }
    active_left = [outcome for outcome in _OUTCOMES if left_totals[outcome]]
    rows = tuple(
        {
            "left_outcome": left_outcome,
            "right_outcome": right_outcome,
            "count": transitions.get((left_outcome, right_outcome), 0),
            "percentage_of_left_outcome": round(
                100.0
                * transitions.get((left_outcome, right_outcome), 0)
                / left_totals[left_outcome],
                6,
            ),
        }
        for left_outcome in active_left
        for right_outcome in _OUTCOMES
    )
    option = {
        "animation": False,
        "aria": {"enabled": True},
        "legend": {"top": 0},
        "grid": {"top": 44, "right": 24, "bottom": 58, "left": 72},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "xAxis": {
            "type": "category",
            "name": f"Outcome in {left}",
            "nameLocation": "middle",
            "nameGap": 38,
            "data": [_OUTCOME_LABELS[outcome] for outcome in active_left],
        },
        "yAxis": {"type": "value", "name": "Transition share (%)", "min": 0, "max": 100},
        "series": [
            {
                "type": "bar",
                "name": f"To {_OUTCOME_LABELS[right_outcome]}",
                "stack": "right-outcome",
                "itemStyle": {"color": _OUTCOME_COLORS[right_outcome]},
                "data": [
                    next(
                        float(row["percentage_of_left_outcome"])
                        for row in rows
                        if row["left_outcome"] == left_outcome
                        and row["right_outcome"] == right_outcome
                    )
                    for left_outcome in active_left
                ],
            }
            for right_outcome in _OUTCOMES
        ],
    }
    recorded_matched = details.get("matched_count")
    return _Chart(
        identifier=f"comparison-transitions-{relation_id}",
        title="Paired outcome transitions",
        subtitle=f"{paired_count:,} uniquely matched parameter hashes from {left} to {right}.",
        kind="bar",
        option=option,
        rows=rows,
        disclosure={
            "population_count": paired_count,
            "plotted_count": paired_count,
            "sampled": False,
            "relation_id": relation_id,
            "comparison_role": role,
            "pairing_key": "parameter_hash unique within each dataset",
            "recorded_matched_count": (
                int(recorded_matched) if isinstance(recorded_matched, (int, float)) else None
            ),
            "unique_pair_count": paired_count,
            "normalization": "within each left-side outcome class",
            "claim_scope": (
                "paired descriptive transition; causal claims require the recorded intervention role"
            ),
            "missing_values_are_zero": False,
        },
        render_kind="comparison-transitions",
        x_label=f"Outcome in {left}",
        y_label="Transition share (%)",
    )


def _shared_comparison_metrics(
    connection: sqlite3.Connection,
    left: str,
    right: str,
    *,
    limit: int,
) -> list[_MetricDescriptor]:
    if not _has_table(connection, "metrics"):
        return []
    rows = connection.execute(
        """
        SELECT m.name,
               SUM(CASE WHEN r.dataset_id = ? THEN 1 ELSE 0 END) AS left_count,
               SUM(CASE WHEN r.dataset_id = ? THEN 1 ELSE 0 END) AS right_count
        FROM metrics AS m
        JOIN runs AS r ON r.run_id = m.run_id
        WHERE r.dataset_id IN (?, ?)
          AND m.value_type = 'number' AND m.value_real IS NOT NULL
        GROUP BY m.name
        HAVING SUM(CASE WHEN r.dataset_id = ? THEN 1 ELSE 0 END) > 0
           AND SUM(CASE WHEN r.dataset_id = ? THEN 1 ELSE 0 END) > 0
        """,
        (left, right, left, right, left, right),
    ).fetchall()
    candidates: list[tuple[int, _MetricDescriptor]] = []
    for row in rows:
        name = str(row["name"])
        safety = _metric_semantics(name, "safety")
        performance = _metric_semantics(name, "performance")
        family_order = 0 if safety is not None else 1
        semantics = safety or performance
        if semantics is None:
            continue
        priority, role, unit, risk_direction = semantics
        coverage = min(int(row["left_count"] or 0), int(row["right_count"] or 0))
        candidates.append(
            (
                family_order,
                _MetricDescriptor(
                    name=name,
                    population_count=coverage,
                    semantic_role=role,
                    unit=unit,
                    risk_direction=risk_direction,
                    priority=priority,
                ),
            )
        )
    candidates.sort(
        key=lambda item: (
            item[0],
            item[1].priority,
            -item[1].population_count,
            item[1].name.casefold(),
        )
    )
    if not candidates:
        return []
    # Keep both safety and runtime effects visible when both families exist.
    selected: list[_MetricDescriptor] = []
    for family in (0, 1):
        match = next((item for order, item in candidates if order == family), None)
        if match is not None and len(selected) < limit:
            selected.append(match)
    for _family, item in candidates:
        if item not in selected and len(selected) < limit:
            selected.append(item)
    return selected


def _comparison_delta_chart(
    connection: sqlite3.Connection,
    *,
    relation_id: str,
    left: str,
    right: str,
    role: str,
    descriptor: _MetricDescriptor,
    paired_count: int,
    maximum_points: int,
) -> _Chart | None:
    accumulator = _PrioritySample(
        maximum_points,
        f"comparison-delta\0{left}\0{right}\0{descriptor.name}",
    )
    for row in connection.execute(
        """
        WITH left_unique AS (
            SELECT parameter_hash, MIN(run_id) AS run_id
            FROM runs
            WHERE dataset_id = ? AND parameter_hash IS NOT NULL AND parameter_hash <> ''
            GROUP BY parameter_hash HAVING COUNT(*) = 1
        ),
        right_unique AS (
            SELECT parameter_hash, MIN(run_id) AS run_id
            FROM runs
            WHERE dataset_id = ? AND parameter_hash IS NOT NULL AND parameter_hash <> ''
            GROUP BY parameter_hash HAVING COUNT(*) = 1
        )
        SELECT left_unique.parameter_hash,
               left_metric.value_real AS left_value,
               right_metric.value_real AS right_value
        FROM left_unique
        JOIN right_unique USING (parameter_hash)
        JOIN metrics AS left_metric
          ON left_metric.run_id = left_unique.run_id AND left_metric.name = ?
        JOIN metrics AS right_metric
          ON right_metric.run_id = right_unique.run_id AND right_metric.name = ?
        WHERE left_metric.value_type = 'number' AND left_metric.value_real IS NOT NULL
          AND right_metric.value_type = 'number' AND right_metric.value_real IS NOT NULL
        """,
        (left, right, descriptor.name, descriptor.name),
    ):
        left_value = float(row["left_value"])
        right_value = float(row["right_value"])
        delta = right_value - left_value
        accumulator.add(
            str(row["parameter_hash"]),
            {
                "parameter_hash": str(row["parameter_hash"]),
                "left_value": left_value,
                "right_value": right_value,
                "delta": delta,
            },
            delta,
        )
    sampled = accumulator.rows()
    if not sampled:
        return None
    values = [float(row["delta"]) for row in sampled]
    rows = tuple(_histogram(values))
    clipped = max(0, accumulator.population_count - len(sampled))
    disclosure = {
        "metric": descriptor.name,
        "semantic_role": descriptor.semantic_role,
        "unit": descriptor.unit,
        "risk_direction": descriptor.risk_direction,
        "delta_definition": "right minus left",
        "population_count": accumulator.population_count,
        "plotted_count": len(sampled),
        "paired_outcome_population_count": paired_count,
        "paired_missing_or_nonnumeric_count": max(0, paired_count - accumulator.population_count),
        "sample_cap": maximum_points,
        "sampled": clipped > 0,
        "clipped_count": clipped,
        "sampling_method": (
            "deterministic SHA-256 priority sample" if clipped else "complete paired population"
        ),
        "relation_id": relation_id,
        "comparison_role": role,
        "pairing_key": "parameter_hash unique within each dataset",
        "claim_scope": "paired descriptive delta; right-minus-left sign follows the recorded metric",
        "missing_values_are_zero": False,
        "aggregation": "equal-width histogram of finite paired deltas",
    }
    metric_label = _axis_metric_label(descriptor)
    option = {
        "animation": False,
        "aria": {"enabled": True},
        "grid": {"top": 24, "right": 24, "bottom": 60, "left": 64},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "xAxis": {
            "type": "value",
            "name": f"Δ {metric_label} ({right} − {left})",
            "nameLocation": "middle",
            "nameGap": 40,
        },
        "yAxis": {"type": "value", "name": "Paired runs", "minInterval": 1},
        "series": [
            {
                "type": "bar",
                "name": "Paired delta",
                "itemStyle": {"color": "#7357d8"},
                "data": [[row["center"], row["count"]] for row in rows],
                "barWidth": "94%",
                "markLine": {"silent": True, "data": [{"xAxis": 0}]},
            }
        ],
    }
    return _Chart(
        identifier=f"comparison-delta-{relation_id}-{_stable_suffix(descriptor.name)}",
        title=f"Paired Δ {_display_metric_name(descriptor.name)}",
        subtitle=_sampling_subtitle(disclosure)
        + f" Delta is {right} minus {left}; missing pairs are excluded, not zero-filled.",
        kind="bar",
        option=option,
        rows=rows,
        disclosure=disclosure,
        render_kind="metric-histogram",
        x_label=f"Δ {metric_label} ({right} − {left})",
        y_label="Paired runs",
        raw_range=(accumulator.minimum, accumulator.maximum),
    )


def _parameter_charts(
    connection: sqlite3.Connection,
    *,
    maximum_points: int,
    maximum_parameters: int,
) -> list[_Chart]:
    total_runs = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM runs AS r
            WHERE NOT EXISTS (
                SELECT 1 FROM dataset_relations AS relation
                WHERE relation.role = 'duplicate_alias'
                  AND relation.right_dataset_id = r.dataset_id
            )
            """
        ).fetchone()[0]
    )
    ranked = connection.execute(
        """
        SELECT p.name, COUNT(*) AS numeric_count
        FROM parameters AS p
        JOIN runs AS r ON r.run_id = p.run_id
        WHERE p.value_type = 'number' AND p.value_real IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM dataset_relations AS relation
              WHERE relation.role = 'duplicate_alias'
                AND relation.right_dataset_id = r.dataset_id
          )
        GROUP BY p.name
        ORDER BY numeric_count DESC, p.name
        LIMIT ?
        """,
        (maximum_parameters,),
    ).fetchall()
    names = [str(row["name"]) for row in ranked]
    if not names:
        return []

    accumulators = {
        name: _PrioritySample(maximum_points, f"parameter-histogram\0{name}") for name in names
    }
    placeholders = ",".join("?" for _ in names)
    cursor = connection.execute(
        f"""
        SELECT p.name, p.run_id, p.value_real
        FROM parameters AS p
        JOIN runs AS r ON r.run_id = p.run_id
        WHERE p.name IN ({placeholders})
          AND p.value_type = 'number' AND p.value_real IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM dataset_relations AS relation
              WHERE relation.role = 'duplicate_alias'
                AND relation.right_dataset_id = r.dataset_id
          )
        """,
        names,
    )
    for row in cursor:
        name = str(row["name"])
        value = float(row["value_real"])
        accumulators[name].add(
            str(row["run_id"]), {"run_id": str(row["run_id"]), "value": value}, value
        )

    charts: list[_Chart] = []
    for name in names:
        accumulator = accumulators[name]
        sampled_rows = accumulator.rows()
        values = [float(row["value"]) for row in sampled_rows]
        bins = tuple(_histogram(values))
        clipped = max(0, accumulator.population_count - len(values))
        identifier = f"parameter-hist-{_stable_suffix(name)}"
        raw_range = (
            (accumulator.minimum, accumulator.maximum)
            if accumulator.minimum is not None and accumulator.maximum is not None
            else None
        )
        disclosure = {
            "parameter": name,
            "population_count": accumulator.population_count,
            "plotted_count": len(values),
            "sample_cap": maximum_points,
            "sampled": clipped > 0,
            "clipped_count": clipped,
            "sampling_method": (
                "deterministic SHA-256 priority sample" if clipped else "complete population"
            ),
            "missing_or_nonnumeric_count": max(0, total_runs - accumulator.population_count),
            "nonfinite_count": accumulator.nonfinite_count,
            "aggregation": "equal-width histogram of plotted finite values",
        }
        option = {
            "animation": False,
            "aria": {"enabled": True},
            "grid": {"top": 24, "right": 24, "bottom": 58, "left": 64},
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
            "xAxis": {
                "type": "value",
                "name": name,
                "nameLocation": "middle",
                "nameGap": 36,
                **(
                    {"min": raw_range[0], "max": raw_range[1]}
                    if raw_range and raw_range[0] != raw_range[1]
                    else {}
                ),
            },
            "yAxis": {"type": "value", "name": "Plotted runs", "minInterval": 1},
            "series": [
                {
                    "type": "bar",
                    "name": name,
                    "itemStyle": {"color": "#526ff0"},
                    "data": [[row["center"], row["count"]] for row in bins],
                    "barWidth": "94%",
                }
            ],
        }
        charts.append(
            _Chart(
                identifier=identifier,
                title=f"Distribution of {name}",
                subtitle=_sampling_subtitle(disclosure),
                kind="bar",
                option=option,
                rows=bins,
                disclosure=disclosure,
                render_kind="parameter-histogram",
                x_label=name,
                y_label="Plotted runs",
                raw_range=raw_range,
            )
        )

    if len(names) >= 2:
        charts.append(
            _scatter_chart(
                connection,
                names[0],
                names[1],
                maximum_points=maximum_points,
                total_runs=total_runs,
            )
        )
    return charts


def _scatter_chart(
    connection: sqlite3.Connection,
    x_name: str,
    y_name: str,
    *,
    maximum_points: int,
    total_runs: int,
) -> _Chart:
    accumulator = _PrioritySample(maximum_points, f"parameter-scatter\0{x_name}\0{y_name}")
    for row in connection.execute(
        """
        SELECT r.run_id, r.dataset_id, r.outcome_class,
               x.value_real AS x_value, y.value_real AS y_value
        FROM runs AS r
        JOIN parameters AS x ON x.run_id = r.run_id AND x.name = ?
        JOIN parameters AS y ON y.run_id = r.run_id AND y.name = ?
        WHERE x.value_type = 'number' AND x.value_real IS NOT NULL
          AND y.value_type = 'number' AND y.value_real IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM dataset_relations AS relation
              WHERE relation.role = 'duplicate_alias'
                AND relation.right_dataset_id = r.dataset_id
          )
        """,
        (x_name, y_name),
    ):
        x_value = float(row["x_value"])
        y_value = float(row["y_value"])
        outcome = str(row["outcome_class"])
        if outcome not in _OUTCOMES:
            outcome = "unknown"
        accumulator.add(
            str(row["run_id"]),
            {
                "run_id": str(row["run_id"]),
                "dataset_id": str(row["dataset_id"]),
                "outcome": outcome,
                "x": x_value,
                "y": y_value,
            },
            x_value,
            y_value,
        )
    rows = tuple(accumulator.rows())
    clipped = max(0, accumulator.population_count - len(rows))
    disclosure = {
        "x_parameter": x_name,
        "y_parameter": y_name,
        "population_count": accumulator.population_count,
        "plotted_count": len(rows),
        "sample_cap": maximum_points,
        "sampled": clipped > 0,
        "clipped_count": clipped,
        "sampling_method": (
            "deterministic SHA-256 priority sample" if clipped else "complete population"
        ),
        "unpaired_or_nonnumeric_count": max(0, total_runs - accumulator.population_count),
        "nonfinite_count": accumulator.nonfinite_count,
        "color_encoding": "normalized outcome class",
    }
    option = {
        "animation": False,
        "aria": {"enabled": True},
        "color": [_OUTCOME_COLORS[outcome] for outcome in _OUTCOMES],
        "legend": {"top": 0},
        "grid": {"top": 44, "right": 24, "bottom": 58, "left": 72},
        "tooltip": {"trigger": "item"},
        "xAxis": {
            "type": "value",
            "name": x_name,
            "nameLocation": "middle",
            "nameGap": 36,
            "scale": True,
        },
        "yAxis": {
            "type": "value",
            "name": y_name,
            "nameLocation": "middle",
            "nameGap": 48,
            "scale": True,
        },
        "series": [
            {
                "type": "scatter",
                "name": _OUTCOME_LABELS[outcome],
                "symbolSize": 6 if len(rows) <= 2_000 else 3,
                "large": len(rows) > 2_000,
                "itemStyle": {"color": _OUTCOME_COLORS[outcome], "opacity": 0.7},
                "data": [
                    [row["x"], row["y"], row["run_id"], row["dataset_id"]]
                    for row in rows
                    if row["outcome"] == outcome
                ],
            }
            for outcome in _OUTCOMES
        ],
    }
    return _Chart(
        identifier=f"parameter-scatter-{_stable_suffix(f'{x_name}\0{y_name}')}",
        title=f"{x_name} × {y_name}",
        subtitle=_sampling_subtitle(disclosure),
        kind="scatter",
        option=option,
        rows=rows,
        disclosure=disclosure,
        render_kind="parameter-scatter",
        x_label=x_name,
        y_label=y_name,
    )


def _five_number_summary(values: Sequence[float]) -> dict[str, float]:
    ordered = sorted(values)
    if not ordered:  # pragma: no cover - caller only emits populated datasets
        raise VisualizationError("cannot summarize an empty metric distribution")
    return {
        "minimum": ordered[0],
        "q1": _quantile(ordered, 0.25),
        "median": _quantile(ordered, 0.5),
        "q3": _quantile(ordered, 0.75),
        "maximum": ordered[-1],
    }


def _quantile(ordered: Sequence[float], fraction: float) -> float:
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)


def _histogram(values: Sequence[float]) -> list[dict[str, Any]]:
    if not values:
        return []
    minimum = min(values)
    maximum = max(values)
    if minimum == maximum:
        padding = max(abs(minimum) * 0.05, 0.5)
        return [
            {
                "bin_start": minimum - padding,
                "bin_end": maximum + padding,
                "center": minimum,
                "count": len(values),
            }
        ]
    bin_count = min(40, max(1, round(math.sqrt(len(values)))))
    scale = max(abs(minimum), abs(maximum), 1.0)
    scaled_minimum = minimum / scale
    scaled_maximum = maximum / scale
    width = (scaled_maximum - scaled_minimum) / bin_count
    counts = [0] * bin_count
    for value in values:
        scaled = value / scale
        index = min(bin_count - 1, max(0, int((scaled - scaled_minimum) / width)))
        counts[index] += 1
    rows: list[dict[str, Any]] = []
    for index, count in enumerate(counts):
        low = (scaled_minimum + index * width) * scale
        high = (scaled_minimum + (index + 1) * width) * scale
        center = (scaled_minimum + (index + 0.5) * width) * scale
        rows.append({"bin_start": low, "bin_end": high, "center": center, "count": count})
    return rows


def _alias_count(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            "SELECT COUNT(DISTINCT right_dataset_id) FROM dataset_relations "
            "WHERE role = 'duplicate_alias'"
        ).fetchone()[0]
    )


def _aggregate_run_count(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            """
            SELECT COUNT(*) FROM runs AS r
            WHERE NOT EXISTS (
                SELECT 1 FROM dataset_relations AS relation
                WHERE relation.role = 'duplicate_alias'
                  AND relation.right_dataset_id = r.dataset_id
            )
            """
        ).fetchone()[0]
    )


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _comparison_identifier(left: str, right: str) -> str:
    return hashlib.sha256(f"{left}\0{right}".encode()).hexdigest()[:20]


def _display_metric_name(name: str) -> str:
    return name.replace("_", " ").replace(".", " · ").strip()


def _axis_metric_label(descriptor: _MetricDescriptor) -> str:
    display = _display_metric_name(descriptor.name)
    return f"{display} ({descriptor.unit})" if descriptor.unit else display


def _atomic_csv(target: Path, chart: _Chart) -> None:
    metadata = {
        "_visualization_id": chart.identifier,
        "_data_hash": _data_hash(chart.rows),
        "_population_count": chart.disclosure.get("population_count"),
        "_plotted_count": chart.disclosure.get("plotted_count"),
        "_sampling_method": chart.disclosure.get("sampling_method", "complete population"),
    }
    rows = [{**row, **metadata} for row in chart.rows]
    columns = list(dict.fromkeys(key for row in rows for key in row))
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _atomic_json(target: Path, chart: _Chart) -> None:
    payload = {
        "visualization": chart.spec(),
        "disclosure": chart.disclosure,
        "data": chart.rows,
    }
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _atomic_figure(
    target: Path,
    chart: _Chart,
    *,
    export_format: str,
    preset: _Preset,
    dpi: int,
    background: str,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    transparent = background == "transparent"
    rc = {
        "font.family": "DejaVu Sans",
        "font.size": preset.font_size,
        "axes.titlesize": preset.font_size + 1.5,
        "axes.labelsize": preset.font_size,
        "xtick.labelsize": max(6.0, preset.font_size - 1.0),
        "ytick.labelsize": max(6.0, preset.font_size - 1.0),
        "legend.fontsize": max(6.0, preset.font_size - 1.0),
        "axes.edgecolor": "#667085",
        "axes.labelcolor": "#344054",
        "text.color": "#1d2939",
        "xtick.color": "#475467",
        "ytick.color": "#475467",
        "axes.grid": True,
        "grid.color": "#e4e7ec",
        "grid.linewidth": 0.6,
        "grid.alpha": 0.85,
        "figure.facecolor": "none" if transparent else "white",
        "axes.facecolor": "none" if transparent else "white",
        "savefig.facecolor": "none" if transparent else "white",
    }
    with plt.rc_context(rc):
        figure_size = preset.figure_size
        if chart.render_kind == "outcomes-by-dataset" and preset.figure_size[0] < 9.0:
            figure_size = (
                figure_size[0],
                max(figure_size[1], 1.35 + 0.23 * len(chart.rows)),
            )
        fig, ax = plt.subplots(figsize=figure_size)
        _render_chart(ax, chart)
        ax.set_title(chart.title, loc="left", fontweight="bold", pad=9)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        fig.tight_layout(pad=0.7)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        os.close(descriptor)
        metadata: dict[str, Any]
        if export_format == "pdf":
            metadata = {
                "Title": chart.title,
                "Creator": "PISA Analysis Tools",
                "Producer": "PISA Analysis Tools",
                "CreationDate": None,
                "ModDate": None,
            }
        elif export_format == "svg":
            metadata = {"Title": chart.title, "Creator": "PISA Analysis Tools", "Date": None}
        else:
            metadata = {"Title": chart.title, "Software": "PISA Analysis Tools"}
        try:
            fig.savefig(
                temporary_name,
                format=export_format,
                dpi=dpi,
                bbox_inches=None,
                transparent=transparent,
                metadata=metadata,
            )
            with Path(temporary_name).open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
        except BaseException:
            Path(temporary_name).unlink(missing_ok=True)
            raise
        finally:
            plt.close(fig)


def _render_chart(ax: Any, chart: _Chart) -> None:
    if chart.render_kind == "outcomes-overall":
        labels = [str(row["label"]) for row in chart.rows]
        values = [int(row["count"]) for row in chart.rows]
        colors = [_OUTCOME_COLORS[str(row["outcome"])] for row in chart.rows]
        bars = ax.bar(labels, values, color=colors, width=0.68)
        ax.bar_label(bars, padding=2, fmt="%d")
        ax.grid(axis="x", visible=False)
    elif chart.render_kind == "outcomes-by-dataset":
        labels = [str(row["dataset_id"]) for row in chart.rows]
        positions = list(range(len(labels)))
        left = [0] * len(labels)
        for outcome in _OUTCOMES:
            values = [int(row[outcome]) for row in chart.rows]
            ax.barh(
                positions,
                values,
                left=left,
                label=_OUTCOME_LABELS[outcome],
                color=_OUTCOME_COLORS[outcome],
                height=0.72,
            )
            left = [previous + value for previous, value in zip(left, values, strict=True)]
        ax.set_yticks(positions, labels)
        ax.invert_yaxis()
        ax.legend(
            frameon=False,
            ncols=min(4, len(_OUTCOMES)),
            loc="lower center",
            bbox_to_anchor=(0.5, 1.0),
        )
        ax.grid(axis="y", visible=False)
    elif chart.render_kind in {"parameter-histogram", "metric-histogram"}:
        centers = [float(row["center"]) for row in chart.rows]
        counts = [int(row["count"]) for row in chart.rows]
        widths = [float(row["bin_end"]) - float(row["bin_start"]) for row in chart.rows]
        color = "#7357d8" if chart.render_kind == "metric-histogram" else "#526ff0"
        ax.bar(centers, counts, width=[width * 0.94 for width in widths], color=color)
        if chart.render_kind == "metric-histogram":
            ax.axvline(0.0, color="#344054", linewidth=0.9, linestyle="--")
        ax.grid(axis="x", visible=False)
    elif chart.render_kind in {"parameter-scatter", "metric-scatter"}:
        for outcome in _OUTCOMES:
            rows = [row for row in chart.rows if row["outcome"] == outcome]
            if not rows:
                continue
            ax.scatter(
                [float(row["x"]) for row in rows],
                [float(row["y"]) for row in rows],
                s=12 if len(chart.rows) <= 2_000 else 5,
                color=_OUTCOME_COLORS[outcome],
                alpha=0.68,
                linewidths=0,
                label=_OUTCOME_LABELS[outcome],
            )
        ax.legend(frameon=False, ncols=2, loc="best")
    elif chart.render_kind == "collision-rate":
        labels = [str(row["dataset_id"]) for row in chart.rows]
        positions = list(range(len(labels)))
        values = [
            float(row["collision_rate_percent"])
            if row["collision_rate_percent"] is not None
            else math.nan
            for row in chart.rows
        ]
        ax.bar(positions, values, color="#dc4c64", width=0.68)
        ax.set_xticks(
            positions,
            labels,
            rotation=28 if len(labels) > 6 else 0,
            ha="right" if len(labels) > 6 else "center",
        )
        ax.set_ylim(0, 100)
        ax.grid(axis="x", visible=False)
    elif chart.render_kind == "metric-distribution":
        labels = [str(row["dataset_id"]) for row in chart.rows]
        positions = list(range(len(labels)))
        minimum = [float(row["minimum"]) for row in chart.rows]
        q1 = [float(row["q1"]) for row in chart.rows]
        median = [float(row["median"]) for row in chart.rows]
        q3 = [float(row["q3"]) for row in chart.rows]
        maximum = [float(row["maximum"]) for row in chart.rows]
        ax.fill_between(positions, q1, q3, color="#526ff0", alpha=0.2, label="Q1–Q3")
        ax.plot(
            positions, minimum, color="#8a94a6", linewidth=0.8, linestyle="--", label="Min / max"
        )
        ax.plot(positions, maximum, color="#8a94a6", linewidth=0.8, linestyle="--")
        ax.plot(
            positions,
            median,
            color="#526ff0",
            linewidth=2.0,
            marker="o",
            markersize=3,
            label="Median",
        )
        ax.set_xticks(
            positions,
            labels,
            rotation=28 if len(labels) > 6 else 0,
            ha="right" if len(labels) > 6 else "center",
        )
        ax.legend(frameon=False, ncols=3, loc="lower center", bbox_to_anchor=(0.5, 1.0))
        ax.grid(axis="x", visible=False)
    elif chart.render_kind == "comparison-outcomes":
        labels = [str(row["label"]) for row in chart.rows]
        positions = list(range(len(labels)))
        width = 0.36
        left_values = [
            float(row["left_rate_percent"]) if row["left_rate_percent"] is not None else math.nan
            for row in chart.rows
        ]
        right_values = [
            float(row["right_rate_percent"]) if row["right_rate_percent"] is not None else math.nan
            for row in chart.rows
        ]
        left_label = str(chart.rows[0]["left_dataset_id"]) if chart.rows else "Left"
        right_label = str(chart.rows[0]["right_dataset_id"]) if chart.rows else "Right"
        ax.bar(
            [value - width / 2 for value in positions],
            left_values,
            width=width,
            color="#526ff0",
            label=left_label,
        )
        ax.bar(
            [value + width / 2 for value in positions],
            right_values,
            width=width,
            color="#e05887",
            label=right_label,
        )
        ax.set_xticks(positions, labels)
        ax.set_ylim(0, 100)
        ax.legend(frameon=False, ncols=2, loc="lower center", bbox_to_anchor=(0.5, 1.0))
        ax.grid(axis="x", visible=False)
    elif chart.render_kind == "comparison-transitions":
        active_left = [
            outcome
            for outcome in _OUTCOMES
            if any(row["left_outcome"] == outcome for row in chart.rows)
        ]
        positions = list(range(len(active_left)))
        bottom = [0.0] * len(active_left)
        for right_outcome in _OUTCOMES:
            values = [
                next(
                    float(row["percentage_of_left_outcome"])
                    for row in chart.rows
                    if row["left_outcome"] == left_outcome and row["right_outcome"] == right_outcome
                )
                for left_outcome in active_left
            ]
            ax.bar(
                positions,
                values,
                bottom=bottom,
                color=_OUTCOME_COLORS[right_outcome],
                label=f"To {_OUTCOME_LABELS[right_outcome]}",
                width=0.68,
            )
            bottom = [previous + value for previous, value in zip(bottom, values, strict=True)]
        ax.set_xticks(positions, [_OUTCOME_LABELS[outcome] for outcome in active_left])
        ax.set_ylim(0, 100)
        ax.legend(frameon=False, ncols=2, loc="lower center", bbox_to_anchor=(0.5, 1.0))
        ax.grid(axis="x", visible=False)
    else:  # pragma: no cover - internal invariant
        raise VisualizationError(f"no renderer for {chart.render_kind!r}")
    if chart.x_label:
        ax.set_xlabel(chart.x_label)
    if chart.y_label:
        ax.set_ylabel(chart.y_label)


def _validate_section(section: str) -> str:
    normalized = str(section).strip().lower()
    if normalized not in SUPPORTED_SECTIONS and not _COMPARISON_SECTION.fullmatch(normalized):
        raise VisualizationError(
            f"unsupported visualization section {section!r}; choose one of "
            f"{', '.join(sorted(SUPPORTED_SECTIONS))}, or compare:<20-hex-id>"
        )
    return normalized


def _section_for_identifier(identifier: str) -> str:
    comparison = _COMPARISON_CHART.match(identifier)
    if comparison:
        return f"compare:{comparison.group(1)}"
    if identifier.startswith("outcomes-") or identifier.startswith("safety-"):
        return "outcomes"
    if identifier.startswith("performance-"):
        return "performance"
    return "sampling"


def _validate_identifier(identifier: str) -> str:
    normalized = str(identifier).strip().lower()
    if not _IDENTIFIER.fullmatch(normalized):
        raise VisualizationError("visualization_id must be a safe lowercase identifier")
    return normalized


def _bounded_integer(value: Any, field_name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise VisualizationError(f"{field_name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise VisualizationError(f"{field_name} must be an integer") from exc
    if parsed != value or not minimum <= parsed <= maximum:
        raise VisualizationError(f"{field_name} must be between {minimum} and {maximum}")
    return parsed


def _sampling_subtitle(disclosure: Mapping[str, Any]) -> str:
    plotted = int(disclosure.get("plotted_count") or 0)
    population = int(disclosure.get("population_count") or 0)
    if disclosure.get("sampled"):
        return f"Deterministic sample of {plotted:,} from {population:,} eligible runs."
    return f"All {population:,} eligible runs are shown."


def _sample_score(namespace: str, stable_key: str) -> int:
    digest = hashlib.sha256(f"{namespace}\0{stable_key}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _stable_suffix(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _data_hash(rows: Sequence[Mapping[str, Any]]) -> str:
    encoded = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
