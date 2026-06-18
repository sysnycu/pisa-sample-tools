from __future__ import annotations

import csv
import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np

from .ingest import read_trace_rows
from .models import AnalysisSpec, RunRecord, SelectedCase
from .statistics import as_float, metric_value, normalized_outcome, safety_region

OUTCOME_COLORS = {
    "success": "#16a34a",
    "failure": "#dc2626",
    "invalid": "#2563eb",
    "execution_error": "#7f1d1d",
    "unknown": "#6b7280",
}
REGION_COLORS = {
    "safe": "#16a34a",
    "near_critical": "#f59e0b",
    "failure": "#dc2626",
    "invalid": "#2563eb",
}


def render_core_figures(
    runs: list[RunRecord],
    spec: AnalysisSpec,
    output_dir: Path,
    *,
    x_param: str | None,
    y_param: str | None,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    paths.extend(_outcome_counts(runs, spec, output_dir))
    if x_param and y_param:
        paths.extend(_parameter_scatter(runs, spec, output_dir, x_param, y_param))
        paths.extend(
            _binned_heatmap(
                runs,
                spec,
                output_dir,
                x_param,
                y_param,
                name="failure_rate_heatmap",
                value=lambda run: 1.0 if normalized_outcome(run, spec) == "failure" else 0.0,
                label="Failure rate",
                cmap="Reds",
                lower=0,
                upper=1,
            )
        )
        for metric_name, file_name, cmap in (
            ("min_ttc", "min_ttc_heatmap", "viridis_r"),
            ("min_distance", "min_distance_heatmap", "viridis"),
        ):
            if any(metric_value(run, spec, metric_name) is not None for run in runs):
                paths.extend(
                    _binned_heatmap(
                        runs,
                        spec,
                        output_dir,
                        x_param,
                        y_param,
                        name=file_name,
                        value=lambda run, metric_name=metric_name: metric_value(
                            run, spec, metric_name
                        ),
                        label=spec.metrics[metric_name].label or metric_name,
                        cmap=cmap,
                    )
                )
        paths.extend(_categorical_map(runs, output_dir, x_param, y_param, "termination_reason"))
        paths.extend(
            _categorical_map(
                runs,
                output_dir,
                x_param,
                y_param,
                "safety_region",
                categories=[safety_region(run, spec) for run in runs],
                colors=REGION_COLORS,
            )
        )
    for metric_name in ("min_ttc", "min_distance"):
        values = [metric_value(run, spec, metric_name) for run in runs]
        if any(value is not None for value in values):
            paths.extend(_metric_distribution(runs, spec, output_dir, metric_name))
    return paths


def render_representative_cases(
    cases: list[SelectedCase],
    spec: AnalysisSpec,
    output_dir: Path,
) -> tuple[list[Path], list[str]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    warnings: list[str] = []
    for case in cases:
        prefix = _slug(case.case_type)
        trajectory_paths = _trajectory_plot(case.run, output_dir, prefix)
        if trajectory_paths:
            paths.extend(trajectory_paths)
        else:
            warnings.append(f"{case.run.run_id}: no agent_states.csv for trajectory plot")
        series_paths = _timeseries_plot(case.run, spec, output_dir, prefix)
        if series_paths:
            paths.extend(series_paths)
        else:
            warnings.append(f"{case.run.run_id}: no frame_metrics.csv for time-series plot")
        control_paths = _control_plot(case.run, output_dir, prefix)
        if control_paths:
            paths.extend(control_paths)
        elif case.run.control_commands_path is None:
            warnings.append(f"{case.run.run_id}: no control_commands.csv; control timeline omitted")
        paths.extend(_event_timeline(case.run, output_dir, prefix))
    return paths, warnings


def render_component_figures(
    runs: list[RunRecord],
    spec: AnalysisSpec,
    output_dir: Path,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for field, title in (
        ("av_name", "AV"),
        ("simulator_name", "Simulator"),
        ("sampler_name", "Sampler"),
    ):
        groups: dict[str, list[RunRecord]] = defaultdict(list)
        for run in runs:
            value = run.metadata.get(field)
            if value not in {None, ""}:
                groups[str(value)].append(run)
        if len(groups) < 2:
            continue
        rows: list[dict[str, Any]] = []
        labels = sorted(groups)
        outcomes = ["success", "failure", "invalid", "execution_error", "unknown"]
        data = np.zeros((len(labels), len(outcomes)))
        for index, label in enumerate(labels):
            counts = Counter(normalized_outcome(run, spec) for run in groups[label])
            total = len(groups[label])
            for outcome_index, outcome in enumerate(outcomes):
                data[index, outcome_index] = counts[outcome] / total
                rows.append(
                    {
                        field: label,
                        "outcome": outcome,
                        "count": counts[outcome],
                        "ratio": data[index, outcome_index],
                    }
                )
        fig, ax = plt.subplots(figsize=(9, 5.5))
        bottoms = np.zeros(len(labels))
        for outcome_index, outcome in enumerate(outcomes):
            values = data[:, outcome_index]
            if not np.any(values):
                continue
            ax.bar(
                labels,
                values,
                bottom=bottoms,
                label=outcome,
                color=OUTCOME_COLORS.get(outcome, "#6b7280"),
            )
            bottoms += values
        ax.set_ylabel("Run ratio")
        ax.set_ylim(0, 1)
        ax.set_title(f"Outcome composition by {title}")
        ax.legend(loc="upper right")
        ax.tick_params(axis="x", rotation=20)
        paths.extend(_save_figure(fig, output_dir / f"{field}_outcome_comparison", spec, rows))
    repeat_groups: dict[str, list[RunRecord]] = defaultdict(list)
    for run in runs:
        key = json_group_key(run)
        repeat_groups[key].append(run)
    repeat_groups = {
        key: members for key, members in repeat_groups.items() if len(members) > 1
    }
    if repeat_groups:
        labels = [f"group {index + 1}" for index in range(len(repeat_groups))]
        consistencies = []
        ttc_stds = []
        rows = []
        for label, members in zip(labels, repeat_groups.values(), strict=True):
            outcomes = Counter(normalized_outcome(run, spec) for run in members)
            consistency = outcomes.most_common(1)[0][1] / len(members)
            ttc_values = [
                value
                for run in members
                if (value := metric_value(run, spec, "min_ttc")) is not None
            ]
            ttc_std = float(np.std(ttc_values)) if ttc_values else math.nan
            consistencies.append(consistency)
            ttc_stds.append(ttc_std)
            rows.append(
                {
                    "group": label,
                    "repeat_count": len(members),
                    "outcome_consistency": consistency,
                    "min_ttc_std": ttc_std,
                }
            )
        fig, left = plt.subplots(figsize=(9, 5.5))
        positions = np.arange(len(labels))
        left.bar(positions - 0.18, consistencies, 0.36, label="Outcome consistency")
        left.set_ylabel("Outcome consistency")
        left.set_ylim(0, 1.05)
        right = left.twinx()
        right.bar(positions + 0.18, ttc_stds, 0.36, color="#f59e0b", label="Min TTC std")
        right.set_ylabel("Min TTC standard deviation")
        left.set_xticks(positions, labels, rotation=20)
        left.set_title("Repeated-run stability")
        handles_left, labels_left = left.get_legend_handles_labels()
        handles_right, labels_right = right.get_legend_handles_labels()
        left.legend(handles_left + handles_right, labels_left + labels_right)
        paths.extend(_save_figure(fig, output_dir / "repeated_run_stability", spec, rows))
    return paths


def _outcome_counts(
    runs: list[RunRecord], spec: AnalysisSpec, output_dir: Path
) -> list[Path]:
    counts = Counter(normalized_outcome(run, spec) for run in runs)
    labels = list(counts)
    values = [counts[label] for label in labels]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.bar(labels, values, color=[OUTCOME_COLORS.get(label, "#6b7280") for label in labels])
    ax.set_ylabel("Runs")
    ax.set_title("Run outcome summary")
    for index, value in enumerate(values):
        ax.text(index, value, str(value), ha="center", va="bottom")
    rows = [{"outcome": label, "count": counts[label]} for label in labels]
    return _save_figure(fig, output_dir / "outcome_summary", spec, rows)


def _parameter_scatter(
    runs: list[RunRecord],
    spec: AnalysisSpec,
    output_dir: Path,
    x_param: str,
    y_param: str,
) -> list[Path]:
    rows = []
    fig, ax = plt.subplots(figsize=(8.5, 6.5))
    for outcome in sorted({normalized_outcome(run, spec) for run in runs}):
        points = []
        for run in runs:
            if normalized_outcome(run, spec) != outcome:
                continue
            x = as_float(run.params.get(x_param))
            y = as_float(run.params.get(y_param))
            if x is None or y is None:
                continue
            points.append((x, y))
            rows.append(
                {
                    "run_id": run.run_id,
                    x_param: x,
                    y_param: y,
                    "outcome": outcome,
                    "termination_reason": run.termination_reason,
                }
            )
        if points:
            ax.scatter(
                [point[0] for point in points],
                [point[1] for point in points],
                s=28,
                alpha=0.8,
                label=outcome,
                color=OUTCOME_COLORS.get(outcome, "#6b7280"),
            )
    ax.set_xlabel(_axis_label(x_param, spec))
    ax.set_ylabel(_axis_label(y_param, spec))
    ax.set_title("Outcome in parameter space")
    ax.legend()
    return _save_figure(fig, output_dir / "outcome_scatter", spec, rows)


def _binned_heatmap(
    runs: list[RunRecord],
    spec: AnalysisSpec,
    output_dir: Path,
    x_param: str,
    y_param: str,
    *,
    name: str,
    value,
    label: str,
    cmap: str,
    lower: float | None = None,
    upper: float | None = None,
) -> list[Path]:
    points = []
    for run in runs:
        x = as_float(run.params.get(x_param))
        y = as_float(run.params.get(y_param))
        metric = value(run)
        if x is not None and y is not None and metric is not None:
            points.append((x, y, float(metric), run.run_id))
    if not points:
        return []
    x_values = np.array([point[0] for point in points])
    y_values = np.array([point[1] for point in points])
    metric_values = np.array([point[2] for point in points])
    x_edges = _bin_edges(x_values, spec.heatmap_bins)
    y_edges = _bin_edges(y_values, spec.heatmap_bins)
    sums, _, _ = np.histogram2d(y_values, x_values, bins=(y_edges, x_edges), weights=metric_values)
    counts, _, _ = np.histogram2d(y_values, x_values, bins=(y_edges, x_edges))
    grid = np.divide(sums, counts, out=np.full_like(sums, np.nan), where=counts > 0)
    fig, ax = plt.subplots(figsize=(8.5, 6.5))
    image = ax.pcolormesh(
        x_edges,
        y_edges,
        grid,
        shading="auto",
        cmap=cmap,
        vmin=lower,
        vmax=upper,
    )
    fig.colorbar(image, ax=ax, label=label)
    ax.set_xlabel(_axis_label(x_param, spec))
    ax.set_ylabel(_axis_label(y_param, spec))
    ax.set_title(label)
    rows = []
    for yi in range(grid.shape[0]):
        for xi in range(grid.shape[1]):
            if counts[yi, xi] == 0:
                continue
            rows.append(
                {
                    "x_min": x_edges[xi],
                    "x_max": x_edges[xi + 1],
                    "y_min": y_edges[yi],
                    "y_max": y_edges[yi + 1],
                    "count": int(counts[yi, xi]),
                    "value": grid[yi, xi],
                }
            )
    return _save_figure(fig, output_dir / name, spec, rows)


def _categorical_map(
    runs: list[RunRecord],
    output_dir: Path,
    x_param: str,
    y_param: str,
    name: str,
    *,
    categories: list[str] | None = None,
    colors: dict[str, str] | None = None,
) -> list[Path]:
    categories = categories or [run.termination_reason or "unknown" for run in runs]
    palette = colors or {
        category: plt.get_cmap("tab20")(index % 20)
        for index, category in enumerate(sorted(set(categories)))
    }
    fig, ax = plt.subplots(figsize=(8.5, 6.5))
    rows = []
    for category in sorted(set(categories)):
        points = []
        for run, run_category in zip(runs, categories, strict=True):
            if run_category != category:
                continue
            x = as_float(run.params.get(x_param))
            y = as_float(run.params.get(y_param))
            if x is None or y is None:
                continue
            points.append((x, y))
            rows.append(
                {
                    "run_id": run.run_id,
                    x_param: x,
                    y_param: y,
                    "category": category,
                }
            )
        if points:
            ax.scatter(
                [point[0] for point in points],
                [point[1] for point in points],
                label=category,
                color=palette[category],
                s=28,
                alpha=0.8,
            )
    ax.set_xlabel(x_param)
    ax.set_ylabel(y_param)
    ax.set_title(name.replace("_", " ").title())
    ax.legend(fontsize=8, loc="best")
    return _save_figure(fig, output_dir / name, AnalysisSpec(), rows, formats=("svg", "png"))


def _metric_distribution(
    runs: list[RunRecord],
    spec: AnalysisSpec,
    output_dir: Path,
    metric_name: str,
) -> list[Path]:
    values = [
        (run, metric_value(run, spec, metric_name))
        for run in runs
        if metric_value(run, spec, metric_name) is not None
    ]
    numeric = np.array([value for _, value in values], dtype=float)
    label = spec.metrics[metric_name].label or metric_name
    rows = [
        {
            "run_id": run.run_id,
            "value": value,
            "outcome": normalized_outcome(run, spec),
        }
        for run, value in values
    ]
    paths: list[Path] = []
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.hist(numeric, bins=min(30, max(5, round(math.sqrt(len(numeric))))), color="#2563eb")
    ax.set_xlabel(label)
    ax.set_ylabel("Runs")
    ax.set_title(f"{label} distribution")
    paths.extend(_save_figure(fig, output_dir / f"{metric_name}_histogram", spec, rows))

    ordered = np.sort(numeric)
    cdf = np.arange(1, len(ordered) + 1) / len(ordered)
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.step(ordered, cdf, where="post", color="#2563eb")
    ax.set_xlabel(label)
    ax.set_ylabel("Cumulative probability")
    ax.set_ylim(0, 1)
    ax.set_title(f"{label} CDF")
    cdf_rows = [
        {"value": value, "cdf": probability}
        for value, probability in zip(ordered, cdf, strict=True)
    ]
    paths.extend(_save_figure(fig, output_dir / f"{metric_name}_cdf", spec, cdf_rows))

    grouped: dict[str, list[float]] = defaultdict(list)
    for run, value in values:
        grouped[normalized_outcome(run, spec)].append(value)
    if len(grouped) > 1:
        labels = sorted(grouped)
        fig, ax = plt.subplots(figsize=(8, 5))
        ax.boxplot([grouped[item] for item in labels], tick_labels=labels)
        ax.set_ylabel(label)
        ax.set_title(f"{label} by outcome")
        paths.extend(
            _save_figure(fig, output_dir / f"{metric_name}_by_outcome", spec, rows)
        )
    return paths


def _trajectory_plot(run: RunRecord, output_dir: Path, prefix: str) -> list[Path]:
    rows = read_trace_rows(run.agent_states_path)
    if not rows:
        return []
    by_agent: dict[str, list[tuple[float, float]]] = defaultdict(list)
    output_rows = []
    for row in rows:
        x, y = as_float(row.get("x")), as_float(row.get("y"))
        if x is None or y is None:
            continue
        agent = str(row.get("agent_id", "unknown"))
        by_agent[agent].append((x, y))
        output_rows.append(
            {
                "step_index": row.get("step_index"),
                "sim_time_ms": row.get("sim_time_ms"),
                "agent_id": agent,
                "x": x,
                "y": y,
            }
        )
    if not by_agent:
        return []
    fig, ax = plt.subplots(figsize=(8, 6.5))
    for agent, points in sorted(by_agent.items()):
        ax.plot([item[0] for item in points], [item[1] for item in points], label=f"agent {agent}")
        ax.scatter([points[0][0]], [points[0][1]], marker="o", s=35)
        ax.scatter([points[-1][0]], [points[-1][1]], marker="x", s=45)
    collision_rows = read_trace_rows(run.collision_events_path)
    for collision in collision_rows:
        x, y = as_float(collision.get("x")), as_float(collision.get("y"))
        if x is not None and y is not None:
            ax.scatter([x], [y], marker="X", color="#dc2626", s=80, label="collision")
    ax.set_aspect("equal", adjustable="datalim")
    ax.set_xlabel("x (m)")
    ax.set_ylabel("y (m)")
    ax.set_title(f"{prefix.replace('_', ' ').title()} trajectory: {run.run_id}")
    ax.legend()
    return _save_figure(
        fig,
        output_dir / f"{prefix}_trajectory",
        AnalysisSpec(),
        output_rows,
        formats=("svg", "png"),
    )


def _timeseries_plot(
    run: RunRecord, spec: AnalysisSpec, output_dir: Path, prefix: str
) -> list[Path]:
    rows = read_trace_rows(run.frame_metrics_path)
    if not rows:
        return []
    series = []
    for metric_name in ("min_ttc", "min_distance"):
        binding = spec.metrics.get(metric_name)
        if binding and binding.series and any(row.get(binding.series) not in {None, ""} for row in rows):
            series.append((metric_name, binding.series, binding.label or metric_name))
    for field, label in (("ego.speed", "Ego speed"), ("ego.acceleration", "Ego acceleration")):
        if any(row.get(field) not in {None, ""} for row in rows):
            series.append((field.replace(".", "_"), field, label))
    if not series:
        return []
    fig, axes = plt.subplots(len(series), 1, figsize=(9, 2.8 * len(series)), sharex=True)
    axes_array = np.atleast_1d(axes)
    output_rows = []
    time_values = [as_float(row.get("sim_time_ms")) for row in rows]
    for axis, (_, field, label) in zip(axes_array, series, strict=True):
        values = [as_float(row.get(field)) for row in rows]
        points = [
            (time / 1000.0, value)
            for time, value in zip(time_values, values, strict=True)
            if time is not None and value is not None
        ]
        if points:
            axis.plot([point[0] for point in points], [point[1] for point in points])
        axis.set_ylabel(label)
        for time, value in points:
            output_rows.append({"time_s": time, "series": field, "value": value})
    axes_array[-1].set_xlabel("Simulation time (s)")
    fig.suptitle(f"{prefix.replace('_', ' ').title()} trace: {run.run_id}")
    fig.tight_layout()
    return _save_figure(
        fig,
        output_dir / f"{prefix}_timeseries",
        AnalysisSpec(),
        output_rows,
        formats=("svg", "png"),
    )


def _control_plot(run: RunRecord, output_dir: Path, prefix: str) -> list[Path]:
    rows = read_trace_rows(run.control_commands_path)
    if not rows:
        return []
    excluded = {"step_index", "sim_time_ms", "control_type"}
    fields = [
        field
        for field in rows[0]
        if field not in excluded and any(as_float(row.get(field)) is not None for row in rows)
    ]
    if not fields:
        return []
    fig, axes = plt.subplots(len(fields), 1, figsize=(9, 2.5 * len(fields)), sharex=True)
    axes_array = np.atleast_1d(axes)
    output_rows = []
    for axis, field in zip(axes_array, fields, strict=True):
        points = []
        for row in rows:
            time = as_float(row.get("sim_time_ms"))
            value = as_float(row.get(field))
            if time is not None and value is not None:
                points.append((time / 1000.0, value))
                output_rows.append({"time_s": time / 1000.0, "series": field, "value": value})
        axis.plot([item[0] for item in points], [item[1] for item in points])
        axis.set_ylabel(field)
    axes_array[-1].set_xlabel("Simulation time (s)")
    fig.suptitle(f"{prefix.replace('_', ' ').title()} controls: {run.run_id}")
    fig.tight_layout()
    return _save_figure(
        fig,
        output_dir / f"{prefix}_controls",
        AnalysisSpec(),
        output_rows,
        formats=("svg", "png"),
    )


def _event_timeline(run: RunRecord, output_dir: Path, prefix: str) -> list[Path]:
    events = []
    for row in read_trace_rows(run.collision_events_path):
        time = as_float(row.get("sim_time_ms"))
        if time is not None:
            events.append((time / 1000.0, "collision"))
    final_time = as_float(run.metrics.get("run.final_sim_time_ms"))
    if final_time is not None:
        events.append((final_time / 1000.0, run.termination_reason or "run end"))
    if not events:
        return []
    fig, ax = plt.subplots(figsize=(9, 2.8))
    ax.hlines(0, 0, max(time for time, _ in events) or 1, color="#64748b")
    for index, (time, label) in enumerate(sorted(events)):
        ax.vlines(time, -0.15, 0.15, color="#dc2626" if "collision" in label else "#2563eb")
        ax.text(time, 0.2 + (index % 2) * 0.14, label, rotation=30, ha="left")
    ax.set_ylim(-0.4, 0.65)
    ax.set_yticks([])
    ax.set_xlabel("Simulation time (s)")
    ax.set_title(f"{prefix.replace('_', ' ').title()} event timeline: {run.run_id}")
    rows = [{"time_s": time, "event": label} for time, label in sorted(events)]
    return _save_figure(
        fig,
        output_dir / f"{prefix}_event_timeline",
        AnalysisSpec(),
        rows,
        formats=("svg", "png"),
    )


def _save_figure(
    fig,
    stem: Path,
    spec: AnalysisSpec,
    rows: list[dict[str, Any]],
    *,
    formats: tuple[str, ...] | None = None,
) -> list[Path]:
    fig.tight_layout()
    output_paths = []
    for suffix in formats or spec.output_formats:
        path = stem.with_suffix(f".{suffix}")
        fig.savefig(path, dpi=180, bbox_inches="tight")
        output_paths.append(path)
    plt.close(fig)
    csv_path = stem.with_suffix(".csv")
    _write_rows(csv_path, rows)
    output_paths.append(csv_path)
    return output_paths


def _write_rows(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _bin_edges(values: np.ndarray, requested_bins: int) -> np.ndarray:
    unique = np.unique(values)
    if len(unique) <= requested_bins and len(unique) > 1:
        midpoints = (unique[:-1] + unique[1:]) / 2
        first = unique[0] - (midpoints[0] - unique[0])
        last = unique[-1] + (unique[-1] - midpoints[-1])
        return np.concatenate(([first], midpoints, [last]))
    lower, upper = float(np.min(values)), float(np.max(values))
    if math.isclose(lower, upper):
        lower -= 0.5
        upper += 0.5
    return np.linspace(lower, upper, requested_bins + 1)


def _axis_label(name: str, spec: AnalysisSpec) -> str:
    unit = spec.parameter_units.get(name)
    return f"{name} ({unit})" if unit else name


def _slug(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value.lower()).strip("_")


def json_group_key(run: RunRecord) -> str:
    return "|".join(
        [
            run.logical_scenario_name,
            repr(sorted(run.params.items())),
            str(run.metadata.get("simulator_name")),
            str(run.metadata.get("av_name")),
            str(run.metadata.get("sampler_name")),
        ]
    )
