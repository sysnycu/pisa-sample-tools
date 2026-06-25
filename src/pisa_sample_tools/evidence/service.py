from __future__ import annotations

import csv
import json
import math
import shutil
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any

import yaml
from scipy.spatial import cKDTree

from pisa_sample_tools.common.yaml import write_yaml

from .campaign import load_campaign
from .comparison import build_paired_comparisons, wilson_interval
from .ingest import (
    clear_trace_cache,
    load_experiments,
    read_execution_manifest,
    read_trace_rows,
)
from .models import AnalysisSpec, DatasetSpec, EvidenceError, EvidenceResult, RunRecord
from .plots import (
    render_component_figures,
    render_core_figures,
    render_representative_cases,
)
from .spec import load_analysis_spec, spec_to_dict
from .statistics import (
    apply_derived_parameters,
    as_float,
    grouped_outcomes,
    metric_value,
    normalized_outcome,
    numeric_summary,
    repeated_run_rows,
    safety_region,
    select_representative_cases,
)
from .validation import DataQualityFinding, enforce_validation, validate_runs


def build_evidence(
    *,
    results_paths: list[Path] | None = None,
    campaign_path: Path | None = None,
    output_dir: Path,
    spec_path: Path | None = None,
    overwrite: bool = False,
    progress: Callable[[str], None] | None = None,
    validation_mode: str | None = None,
    deep_validation: bool = False,
    report_mode: str = "interactive",
) -> EvidenceResult:
    clear_trace_cache()
    reporter = _ProgressReporter(progress)
    if report_mode not in {"interactive", "static"}:
        raise EvidenceError("report_mode must be 'interactive' or 'static'")
    reporter.step("loading analysis spec")
    spec = load_analysis_spec(spec_path)
    if validation_mode is not None:
        if validation_mode not in {"strict", "permissive"}:
            raise EvidenceError("validation_mode must be 'strict' or 'permissive'")
        spec = replace(spec, validation_mode=validation_mode)
    if campaign_path is not None and results_paths:
        raise EvidenceError("campaign_path and results_paths are mutually exclusive")
    if campaign_path is not None:
        datasets = load_campaign(campaign_path)
    elif results_paths:
        datasets = [
            DatasetSpec(dataset_id=path.expanduser().resolve().name, results_path=path)
            for path in results_paths
        ]
    else:
        raise EvidenceError("at least one results path or an analysis campaign is required")
    reporter.step(f"loading {len(datasets)} dataset(s)")
    runs, warnings = load_experiments(datasets, spec, progress=reporter.step)
    reporter.step("applying derived parameters")
    runs = apply_derived_parameters(runs, spec)
    reporter.step("validating inputs")
    findings = validate_runs(runs, spec, deep=deep_validation)
    enforce_validation(findings, spec)
    warnings.extend(
        finding.message
        for finding in findings
        if finding.severity != "info" or finding.code == "metric_requires_derivation"
    )
    reporter.step("deriving missing summary metrics")
    runs, derived_warnings = _derive_summary_metrics(runs, spec)
    warnings.extend(derived_warnings)
    reporter.step("selecting parameter pairs")
    parameter_pairs, pair_warnings = _select_parameter_pairs(runs, spec)
    warnings.extend(pair_warnings)
    x_param, y_param = parameter_pairs[0] if parameter_pairs else (None, None)
    reporter.step("selecting representative cases")
    cases = select_representative_cases(runs, spec, x_param, y_param)

    reporter.step(f"preparing output directory {output_dir}")
    _prepare_output_dir(output_dir, overwrite=overwrite)

    summary_dir = output_dir / "summary"
    figures_dir = output_dir / "figures"
    cases_dir = output_dir / "representative_cases"
    comparison_dir = output_dir / "comparison"
    report_dir = output_dir / "report"
    provenance_dir = output_dir / "provenance"
    for path in (
        summary_dir,
        figures_dir,
        cases_dir,
        comparison_dir,
        report_dir,
        provenance_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)

    reporter.step("writing summary tables")
    _write_rows(summary_dir / "runs.csv", _run_rows(runs, spec))
    outcome_rows = _outcome_rows(runs, spec)
    metric_rows = _metric_rows(runs, spec)
    parameter_rows = _parameter_rows(runs)
    performance_rows = _performance_rows(runs)
    agent_geometry_rows = _agent_geometry_rows(runs)
    collision_event_rows = _collision_event_rows(runs)
    scenario_event_rows = _scenario_event_rows(runs)
    data_quality_rows = [item.as_row() for item in findings]
    _write_rows(summary_dir / "outcomes.csv", outcome_rows)
    _write_rows(summary_dir / "metrics.csv", metric_rows)
    _write_rows(summary_dir / "parameters.csv", parameter_rows)
    _write_rows(summary_dir / "execution_performance.csv", performance_rows)
    _write_rows(summary_dir / "agent_geometry.csv", agent_geometry_rows)
    _write_rows(summary_dir / "collision_events.csv", collision_event_rows)
    _write_rows(summary_dir / "scenario_events.csv", scenario_event_rows)
    _write_rows(cases_dir / "selected_cases.csv", _selected_case_rows(cases, spec))
    _write_rows(provenance_dir / "data_quality.csv", data_quality_rows)
    (provenance_dir / "data_quality.json").write_text(
        json.dumps(data_quality_rows, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )

    reporter.step("writing comparison tables")
    component_rows = _component_rows(runs, spec)
    repeat_rows = repeated_run_rows(runs, spec)
    _write_rows(comparison_dir / "component_comparison.csv", component_rows)
    _write_rows(comparison_dir / "repeated_run_stability.csv", repeat_rows)
    paired = build_paired_comparisons(runs, spec)
    warnings.extend(paired.warnings)
    for name, rows in (
        ("pairing_summary", paired.pairing_summary),
        ("matched_runs", paired.matched_runs),
        ("unmatched_runs", paired.unmatched_runs),
        ("outcome_transition", paired.outcome_transition),
        ("metric_deltas", paired.metric_deltas),
        ("failure_disagreement", paired.failure_disagreement),
        ("paired_summary", paired.paired_summary),
    ):
        _write_rows(comparison_dir / f"{name}.csv", rows)

    reporter.step("rendering core figures")
    figure_paths = render_core_figures(
        runs,
        spec,
        figures_dir,
        x_param=x_param,
        y_param=y_param,
        parameter_pairs=parameter_pairs,
        progress=reporter.step,
    )
    reporter.step("rendering representative cases")
    case_paths, case_warnings = render_representative_cases(cases, spec, cases_dir)
    warnings.extend(case_warnings)
    figure_paths.extend(case_paths)
    reporter.step("rendering component comparisons")
    figure_paths.extend(render_component_figures(runs, spec, comparison_dir))

    reporter.step("writing provenance")
    resolved_spec = spec_to_dict(spec)
    resolved_spec["parameters"]["axes"] = {"x": x_param, "y": y_param}
    resolved_spec["parameters"]["resolved_pairs"] = [
        {"x": left, "y": right} for left, right in parameter_pairs
    ]
    write_yaml(provenance_dir / "resolved_analysis_spec.yaml", resolved_spec)
    write_yaml(provenance_dir / "analysis_spec.yaml", resolved_spec)
    _preserve_source_manifests(datasets, provenance_dir)
    if campaign_path is not None:
        write_yaml(
            provenance_dir / "resolved_campaign.yaml",
            {
                "version": 1,
                "datasets": [
                    {
                        "id": dataset.dataset_id,
                        "results": str(dataset.results_path.expanduser().resolve()),
                        "metadata": dataset.metadata,
                    }
                    for dataset in datasets
                ],
            },
        )
    input_manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "inputs": [str(dataset.results_path) for dataset in datasets],
        "campaign": str(campaign_path.expanduser().resolve()) if campaign_path else None,
        "datasets": [
            {
                "dataset_id": dataset.dataset_id,
                "results_path": str(dataset.results_path),
                "metadata": dataset.metadata,
            }
            for dataset in datasets
        ],
        "run_count": len(runs),
        "experiments": sorted({run.experiment_id for run in runs}),
        "source_files": {
            "result_csv": len(runs),
            "frame_metrics_csv": sum(run.frame_metrics_path is not None for run in runs),
            "agent_states_csv": sum(run.agent_states_path is not None for run in runs),
            "agent_geometry_csv": sum(run.agent_geometry_path is not None for run in runs),
            "collision_events_csv": sum(run.collision_events_path is not None for run in runs),
            "scenario_events_csv": sum(run.scenario_events_path is not None for run in runs),
            "control_commands_csv": sum(run.control_commands_path is not None for run in runs),
        },
    }
    write_yaml(provenance_dir / "input_manifest.yaml", input_manifest)
    (provenance_dir / "warnings.txt").write_text(
        "\n".join(f"- {warning}" for warning in warnings) + ("\n" if warnings else ""),
        encoding="utf-8",
    )

    reporter.step("writing report data")
    case_artifacts = _write_case_payloads(report_dir / "cases", output_dir, cases, spec)
    report_payload = _build_report_payload(
        output_dir=output_dir,
        runs=runs,
        spec=spec,
        input_manifest=input_manifest,
        resolved_spec=resolved_spec,
        parameter_pairs=parameter_pairs,
        cases=cases,
        case_artifacts=case_artifacts,
        outcome_rows=outcome_rows,
        metric_rows=metric_rows,
        parameter_rows=parameter_rows,
        performance_rows=performance_rows,
        pairing_rows=paired.pairing_summary,
        paired_summary_rows=paired.paired_summary,
        component_rows=component_rows,
        repeat_rows=repeat_rows,
        matched_rows=paired.matched_runs,
        unmatched_rows=paired.unmatched_runs,
        transition_rows=paired.outcome_transition,
        delta_rows=paired.metric_deltas,
        disagreement_rows=paired.failure_disagreement,
        data_quality_rows=data_quality_rows,
        figure_paths=figure_paths,
        warnings=warnings,
    )
    _write_report_payload(report_dir, report_payload)

    reporter.step("writing reports")
    report_path = report_dir / "analysis_report.html"
    _write_html_report(
        report_path,
        output_dir=output_dir,
        payload=report_payload,
        mode=report_mode,
    )
    _write_markdown_report(
        report_dir / "analysis_report.md",
        runs=runs,
        outcome_rows=outcome_rows,
        metric_rows=metric_rows,
        parameter_rows=parameter_rows,
        paired_summary_rows=paired.paired_summary,
        cases=cases,
        warnings=warnings,
    )
    _write_latex_summary(
        report_dir / "paper_ready_summary.tex",
        outcome_rows=outcome_rows,
        metric_rows=metric_rows,
        paired_summary_rows=paired.paired_summary,
    )
    _write_limitations(report_dir / "limitations.md", input_manifest, warnings)

    reporter.step("writing stage timings")
    (provenance_dir / "stage_timings.json").write_text(
        json.dumps(reporter.timings, indent=2) + "\n", encoding="utf-8"
    )

    manifest_path = output_dir / "manifest.yaml"
    manifest = {
        "tool": "pisa-analysis-tools",
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "run_count": len(runs),
        "warning_count": len(warnings),
        "analysis_spec": str(provenance_dir / "resolved_analysis_spec.yaml"),
        "report": str(report_path),
        "figures": [str(path) for path in figure_paths],
        "outputs": [
            str(path)
            for path in sorted(output_dir.rglob("*"))
            if path.is_file() and path != manifest_path
        ],
    }
    write_yaml(manifest_path, manifest)
    reporter.step("analysis complete")
    clear_trace_cache()
    return EvidenceResult(
        output_dir=output_dir,
        report_path=report_path,
        manifest_path=manifest_path,
        run_count=len(runs),
        figure_paths=tuple(figure_paths),
        warning_count=len(warnings),
    )


def validate_evidence_inputs(
    *,
    results_paths: list[Path] | None = None,
    campaign_path: Path | None = None,
    spec_path: Path | None = None,
    validation_mode: str | None = None,
    deep: bool = True,
    progress: Callable[[str], None] | None = None,
) -> tuple[int, list[DataQualityFinding]]:
    spec = load_analysis_spec(spec_path)
    if validation_mode is not None:
        if validation_mode not in {"strict", "permissive"}:
            raise EvidenceError("validation_mode must be 'strict' or 'permissive'")
        spec = replace(spec, validation_mode=validation_mode)
    if campaign_path is not None and results_paths:
        raise EvidenceError("campaign_path and results_paths are mutually exclusive")
    if campaign_path is not None:
        datasets = load_campaign(campaign_path)
    elif results_paths:
        datasets = [
            DatasetSpec(dataset_id=path.expanduser().resolve().name, results_path=path)
            for path in results_paths
        ]
    else:
        raise EvidenceError("at least one results path or an analysis campaign is required")
    clear_trace_cache()
    runs, ingest_warnings = load_experiments(datasets, spec, progress=progress)
    runs = apply_derived_parameters(runs, spec)
    findings = validate_runs(runs, spec, deep=deep)
    findings.extend(
        DataQualityFinding("warning", "ingest_warning", warning)
        for warning in ingest_warnings
    )
    clear_trace_cache()
    return len(runs), findings


class _ProgressReporter:
    def __init__(self, emit: Callable[[str], None] | None) -> None:
        self._emit = emit
        self._started = time.perf_counter()
        self._last = self._started
        self.timings: list[dict[str, Any]] = []

    def step(self, message: str) -> None:
        now = time.perf_counter()
        elapsed = now - self._started
        delta = now - self._last
        self._last = now
        self.timings.append(
            {"stage": message, "elapsed_seconds": elapsed, "delta_seconds": delta}
        )
        if self._emit is not None:
            self._emit(f"[+{elapsed:6.1f}s | {delta:5.1f}s] {message}")


def _prepare_output_dir(output_dir: Path, *, overwrite: bool) -> None:
    output_dir = output_dir.expanduser()
    if not output_dir.exists():
        output_dir.mkdir(parents=True)
        return
    if not output_dir.is_dir():
        raise EvidenceError(f"output path exists and is not a directory: {output_dir}")
    if not overwrite:
        raise EvidenceError(f"analysis output already exists: {output_dir}")
    manifest_path = output_dir / "manifest.yaml"
    if not manifest_path.exists():
        raise EvidenceError(
            "analysis output exists but manifest.yaml was not found; refusing to overwrite"
        )
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise EvidenceError(f"could not read existing manifest.yaml: {exc}") from exc
    if manifest.get("tool") != "pisa-analysis-tools":
        raise EvidenceError("existing manifest.yaml is not PISA analysis output")
    shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)


def _derive_summary_metrics(
    runs: list[RunRecord], spec: AnalysisSpec
) -> tuple[list[RunRecord], list[str]]:
    updated: list[RunRecord] = []
    derivation_counts: Counter[tuple[str, str]] = Counter()
    for run in runs:
        metrics = dict(run.metrics)
        frame_rows: list[dict[str, str]] | None = None
        for name, binding in spec.metrics.items():
            if binding.summary is None or binding.summary in metrics or binding.series is None:
                continue
            if frame_rows is None:
                frame_rows = read_trace_rows(run.frame_metrics_path)
            values = [
                value
                for row in frame_rows
                if (value := as_float(row.get(binding.series))) is not None
            ]
            if not values:
                continue
            if name == "max_deceleration":
                derived = max(max(-value, 0.0) for value in values)
            else:
                derived = min(values)
            metrics[binding.summary] = derived
            derivation_counts[(binding.summary, binding.series)] += 1
        if "collision_time_ms" not in metrics and "collision" in (
            run.termination_reason or ""
        ).lower():
            final_time = as_float(metrics.get("run.final_sim_time_ms"))
            if final_time is not None:
                metrics["collision_time_ms"] = final_time
            else:
                times = [
                    value
                    for row in read_trace_rows(run.collision_events_path)
                    if (value := as_float(row.get("sim_time_ms"))) is not None
                ]
                if times:
                    metrics["collision_time_ms"] = min(times)
        updated.append(replace(run, metrics=metrics))
    warnings = [
        f"derived {summary} from {series} time series for {count} run(s)"
        for (summary, series), count in sorted(derivation_counts.items())
    ]
    for name, binding in spec.metrics.items():
        if binding.summary is None:
            continue
        missing = sum(binding.summary not in run.metrics for run in updated)
        if missing:
            warnings.append(
                f"metric '{name}' remains unavailable for {missing} run(s) after derivation"
            )
    return updated, warnings


def _select_parameter_pairs(
    runs: list[RunRecord], spec: AnalysisSpec
) -> tuple[list[tuple[str, str]], list[str]]:
    names = sorted({name for run in runs for name in run.params})
    numeric = [
        name
        for name in names
        if any(as_float(run.params.get(name)) is not None for run in runs)
    ]
    selected = list(spec.parameter_include) if spec.parameter_include else numeric
    selected = [
        name for name in selected if name in numeric and name not in spec.parameter_exclude
    ]
    if spec.parameter_mode == "all_pairwise":
        return list(combinations(dict.fromkeys(selected), 2)), []

    warnings: list[str] = []
    x_param = spec.x_param if spec.x_param in numeric else None
    if x_param is None and numeric:
        x_param = numeric[0]
        if spec.x_param:
            warnings.append(
                f"configured x parameter '{spec.x_param}' was not found; using '{x_param}'"
            )
    y_param = spec.y_param if spec.y_param in numeric and spec.y_param != x_param else None
    if y_param is None:
        y_param = next((name for name in numeric if name != x_param), None)
        if spec.y_param and y_param != spec.y_param:
            warnings.append(
                f"configured y parameter '{spec.y_param}' was unavailable or duplicated; "
                f"using '{y_param}'"
            )
    return ([(x_param, y_param)] if x_param and y_param else []), warnings


def _preserve_source_manifests(
    datasets: list[DatasetSpec], provenance_dir: Path
) -> None:
    destination = provenance_dir / "source_execution_manifests"
    destination.mkdir(parents=True, exist_ok=True)
    for dataset in datasets:
        manifest_path, _ = read_execution_manifest(dataset.results_path.expanduser().resolve())
        if manifest_path is None:
            continue
        safe_id = "".join(
            character if character.isalnum() or character in {"-", "_"} else "_"
            for character in dataset.dataset_id
        )
        shutil.copy2(manifest_path, destination / f"{safe_id}{manifest_path.suffix}")


def _run_rows(runs: list[RunRecord], spec: AnalysisSpec) -> list[dict[str, Any]]:
    param_names = sorted({name for run in runs for name in run.params})
    metric_names = sorted({name for run in runs for name in run.metrics})
    metadata_names = sorted({name for run in runs for name in run.metadata})
    rows = []
    for run in runs:
        row = {
            "run_id": run.run_id,
            "experiment_id": run.experiment_id,
            "scenario_id": run.scenario_id,
            "sample_id": run.sample_id,
            "logical_scenario_name": run.logical_scenario_name,
            "status": run.status,
            "outcome": run.outcome,
            "normalized_outcome": normalized_outcome(run, spec),
            "safety_region": safety_region(run, spec),
            "termination_reason": run.termination_reason,
            "stop_reason": run.stop_reason,
            "result_path": run.result_path,
        }
        row.update({f"metadata.{name}": run.metadata.get(name, "") for name in metadata_names})
        row.update({f"param.{name}": run.params.get(name, "") for name in param_names})
        row.update({f"metric.{name}": run.metrics.get(name, "") for name in metric_names})
        rows.append(row)
    return rows


def _outcome_rows(runs: list[RunRecord], spec: AnalysisSpec) -> list[dict[str, Any]]:
    counts = grouped_outcomes(runs, spec)
    total = len(runs)
    return [
        {"outcome": outcome, "count": count, "percentage": count / total * 100}
        for outcome, count in counts.most_common()
    ]


def _metric_rows(runs: list[RunRecord], spec: AnalysisSpec) -> list[dict[str, Any]]:
    rows = []
    for name, binding in spec.metrics.items():
        values = [
            value for run in runs if (value := metric_value(run, spec, name)) is not None
        ]
        summary = numeric_summary(values)
        rows.append(
            {
                "metric": name,
                "source": binding.summary,
                "label": binding.label,
                "unit": binding.unit,
                "missing": len(runs) - len(values),
                **summary,
            }
        )
    return rows


def _parameter_rows(runs: list[RunRecord]) -> list[dict[str, Any]]:
    rows = []
    for name in sorted({name for run in runs for name in run.params}):
        values = [run.params.get(name) for run in runs if run.params.get(name) not in {None, ""}]
        numeric = [value for item in values if (value := as_float(item)) is not None]
        summary = numeric_summary(numeric)
        rows.append(
            {
                "parameter": name,
                "type": "numeric" if numeric else "categorical",
                "count": len(values),
                "unique": len({str(value) for value in values}),
                "missing": len(runs) - len(values),
                "min": summary["min"],
                "max": summary["max"],
                "mean": summary["mean"],
            }
        )
    return rows


def _performance_rows(runs: list[RunRecord]) -> list[dict[str, Any]]:
    metric_map = {
        "total_steps": "run.total_steps",
        "simulated_duration_ms": "run.final_sim_time_ms",
        "wall_time_ms": "run.wall_time_ms",
        "real_time_factor": "run.speedup",
    }
    rows = []
    for name, field in metric_map.items():
        values = [value for run in runs if (value := as_float(run.metrics.get(field))) is not None]
        rows.append({"metric": name, "source": field, **numeric_summary(values)})
    return rows


def _agent_geometry_rows(runs: list[RunRecord]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run in runs:
        for row in read_trace_rows(run.agent_geometry_path):
            rows.append(
                {
                    "run_id": run.run_id,
                    "experiment_id": run.experiment_id,
                    "scenario_id": run.scenario_id,
                    "sample_id": run.sample_id,
                    "result_path": run.result_path,
                    "step_index": row.get("step_index"),
                    "sim_time_ms": row.get("sim_time_ms"),
                    "agent_id": row.get("agent_id") or row.get("actor_id"),
                    "shape_type": row.get("shape_type"),
                    "length_m": row.get("length_m"),
                    "width_m": row.get("width_m"),
                    "height_m": row.get("height_m"),
                    "reference_point": row.get("reference_point"),
                    "footprint_json": row.get("footprint_json"),
                    "source": row.get("source"),
                }
            )
    return rows


def _collision_event_rows(runs: list[RunRecord]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run in runs:
        for row in read_trace_rows(run.collision_events_path):
            rows.append(
                {
                    "run_id": run.run_id,
                    "experiment_id": run.experiment_id,
                    "scenario_id": run.scenario_id,
                    "sample_id": run.sample_id,
                    "result_path": run.result_path,
                    "step_index": row.get("step_index"),
                    "sim_time_ms": row.get("sim_time_ms"),
                    "actor_a": row.get("actor_a") or row.get("actor_id_a"),
                    "actor_b": row.get("actor_b") or row.get("actor_id_b"),
                    "x": row.get("x"),
                    "y": row.get("y"),
                    "z": row.get("z"),
                    "position_source": row.get("position_source"),
                    "contact_region_json": row.get("contact_region_json"),
                }
            )
    return rows


def _scenario_event_rows(runs: list[RunRecord]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run in runs:
        for row in read_trace_rows(run.scenario_events_path):
            rows.append(
                {
                    "run_id": run.run_id,
                    "experiment_id": run.experiment_id,
                    "scenario_id": run.scenario_id,
                    "sample_id": run.sample_id,
                    "result_path": run.result_path,
                    "step_index": row.get("step_index"),
                    "sim_time_ms": row.get("sim_time_ms"),
                    "event_type": row.get("event_type")
                    or row.get("event")
                    or row.get("name")
                    or row.get("type"),
                    "actor_id": row.get("actor_id"),
                    "actor_id_b": row.get("actor_id_b"),
                    "x": row.get("x"),
                    "y": row.get("y"),
                    "z": row.get("z"),
                    "source": row.get("source"),
                    "position_source": row.get("position_source"),
                    "contact_region_json": row.get("contact_region_json"),
                    "details_json": row.get("details_json"),
                }
            )
    return rows


def _selected_case_rows(cases, spec: AnalysisSpec) -> list[dict[str, Any]]:
    return [
        {
            "case_type": case.case_type,
            "run_id": case.run.run_id,
            "scenario_id": case.run.scenario_id,
            "params": json.dumps(case.run.params, sort_keys=True),
            "outcome": normalized_outcome(case.run, spec),
            "termination_reason": case.run.termination_reason,
            "min_ttc": metric_value(case.run, spec, "min_ttc"),
            "min_distance": metric_value(case.run, spec, "min_distance"),
            "selection_reason": case.reason,
            "result_path": case.run.result_path,
        }
        for case in cases
    ]


def _write_case_payloads(
    cases_dir: Path,
    output_dir: Path,
    cases,
    spec: AnalysisSpec,
) -> dict[str, str]:
    cases_dir.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, str] = {}
    for case in cases:
        path = cases_dir / f"{_slug(case.case_type)}.json"
        payload = {
            "case_type": case.case_type,
            "selection_reason": case.reason,
            "run": _run_payload(case.run, spec),
            "traces": {
                "trajectory": read_trace_rows(case.run.agent_states_path),
                "timeseries": read_trace_rows(case.run.frame_metrics_path),
                "controls": read_trace_rows(case.run.control_commands_path),
                "events": read_trace_rows(case.run.scenario_events_path),
                "collisions": read_trace_rows(case.run.collision_events_path),
            },
        }
        path.write_text(
            json.dumps(_json_safe(payload), indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        artifacts[case.case_type] = str(path.relative_to(output_dir))
    return artifacts


def _build_report_payload(
    *,
    output_dir: Path,
    runs: list[RunRecord],
    spec: AnalysisSpec,
    input_manifest: dict[str, Any],
    resolved_spec: dict[str, Any],
    parameter_pairs: list[tuple[str, str]],
    cases,
    case_artifacts: dict[str, str],
    outcome_rows: list[dict[str, Any]],
    metric_rows: list[dict[str, Any]],
    parameter_rows: list[dict[str, Any]],
    performance_rows: list[dict[str, Any]],
    pairing_rows: list[dict[str, Any]],
    paired_summary_rows: list[dict[str, Any]],
    component_rows: list[dict[str, Any]],
    repeat_rows: list[dict[str, Any]],
    matched_rows: list[dict[str, Any]],
    unmatched_rows: list[dict[str, Any]],
    transition_rows: list[dict[str, Any]],
    delta_rows: list[dict[str, Any]],
    disagreement_rows: list[dict[str, Any]],
    data_quality_rows: list[dict[str, Any]],
    figure_paths: list[Path],
    warnings: list[str],
) -> dict[str, Any]:
    x_param, y_param = parameter_pairs[0] if parameter_pairs else (None, None)
    numeric_parameters = [
        row["parameter"]
        for row in parameter_rows
        if row.get("type") == "numeric"
    ]
    case_rows = _selected_case_rows(cases, spec)
    artifact_by_case = {
        row["case_type"]: case_artifacts.get(row["case_type"], "")
        for row in case_rows
    }
    for row in case_rows:
        row["case_json"] = artifact_by_case.get(row["case_type"], "")
    payload = {
        "schema_version": 1,
        "summary": {
            "run_count": len(runs),
            "experiment_count": len({run.experiment_id for run in runs}),
            "parameter_count": len({key for run in runs for key in run.params}),
            "metric_count": len({key for run in runs for key in run.metrics}),
            "warning_count": len(warnings),
            "default_axes": {"x": x_param, "y": y_param},
            "near_critical_ttc_s": spec.near_critical_ttc_s,
            "outcomes": outcome_rows,
            "performance": performance_rows,
        },
        "runs": [_run_payload(run, spec) for run in runs],
        "parameters": _parameter_payload(parameter_rows, spec, numeric_parameters),
        "metrics": metric_rows,
        "parameter_pairs": [
            {"x": left, "y": right, "key": _pair_key(left, right)}
            for left, right in parameter_pairs
        ],
        "figures": _figure_payloads(figure_paths, output_dir),
        "representative_cases": case_rows,
        "comparison": {
            "pairing_summary": pairing_rows,
            "paired_summary": paired_summary_rows,
            "component_comparison": component_rows,
            "repeated_run_stability": repeat_rows,
            "matched_runs": matched_rows,
            "unmatched_runs": unmatched_rows,
            "outcome_transition": transition_rows,
            "metric_deltas": delta_rows,
            "failure_disagreement": disagreement_rows,
        },
        "data_quality": {
            "findings": data_quality_rows,
            "warnings": warnings,
            "source_files": input_manifest["source_files"],
        },
        "provenance": {
            "input_manifest": input_manifest,
            "resolved_spec": resolved_spec,
        },
        "boundary": _boundary_payload(runs, spec, parameter_pairs),
    }
    payload["insights"] = _insight_payload(payload, runs, spec)
    return _json_safe(payload)


def _write_report_payload(report_dir: Path, payload: dict[str, Any]) -> None:
    payload_json = json.dumps(payload, ensure_ascii=True).replace("</", "<\\/")
    (report_dir / "analysis_data.json").write_text(payload_json + "\n", encoding="utf-8")
    (report_dir / "analysis_data.js").write_text(
        f"window.PISA_ANALYSIS_DATA={payload_json};\n", encoding="utf-8"
    )
    runs_json = json.dumps(payload["runs"], ensure_ascii=True).replace("</", "<\\/")
    (report_dir / "runs.json").write_text(runs_json + "\n", encoding="utf-8")
    (report_dir / "runs.js").write_text(f"window.PISA_RUNS={runs_json};\n", encoding="utf-8")


def _run_payload(run: RunRecord, spec: AnalysisSpec) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "experiment_id": run.experiment_id,
        "scenario_id": run.scenario_id,
        "sample_id": run.sample_id,
        "logical_scenario_name": run.logical_scenario_name,
        "status": run.status,
        "outcome": run.outcome,
        "normalized_outcome": normalized_outcome(run, spec),
        "safety_region": safety_region(run, spec),
        "termination_reason": run.termination_reason,
        "stop_reason": run.stop_reason,
        "params": run.params,
        "metrics": run.metrics,
        "metadata": run.metadata,
        "result_path": str(run.result_path),
        "artifacts": {
            "frame_metrics": str(run.frame_metrics_path) if run.frame_metrics_path else None,
            "agent_states": str(run.agent_states_path) if run.agent_states_path else None,
            "agent_geometry": str(run.agent_geometry_path) if run.agent_geometry_path else None,
            "collision_events": str(run.collision_events_path) if run.collision_events_path else None,
            "scenario_events": str(run.scenario_events_path) if run.scenario_events_path else None,
            "control_commands": str(run.control_commands_path) if run.control_commands_path else None,
        },
    }


def _parameter_payload(
    parameter_rows: list[dict[str, Any]],
    spec: AnalysisSpec,
    numeric_parameters: list[str],
) -> list[dict[str, Any]]:
    numeric = set(numeric_parameters)
    return [
        {
            **row,
            "label": spec.parameter_labels.get(str(row["parameter"]), row["parameter"]),
            "unit": spec.parameter_units.get(str(row["parameter"])),
            "numeric": row["parameter"] in numeric,
        }
        for row in parameter_rows
    ]


def _figure_payloads(paths: list[Path], output_dir: Path) -> list[dict[str, Any]]:
    figures = []
    for path in paths:
        rel = path.relative_to(output_dir)
        figures.append(
            {
                "path": str(rel),
                "name": path.stem,
                "title": path.stem.replace("_", " ").title(),
                "format": path.suffix.removeprefix("."),
                "pair": _figure_pair(path),
            }
        )
    return figures


def _boundary_payload(
    runs: list[RunRecord],
    spec: AnalysisSpec,
    parameter_pairs: list[tuple[str, str]],
) -> dict[str, Any]:
    return {
        "grid_size": 60,
        "nearest_neighbors": 24,
        "pairs": {
            _pair_key(left, right): _boundary_for_pair(runs, spec, left, right)
            for left, right in parameter_pairs
        },
    }


def _boundary_for_pair(
    runs: list[RunRecord],
    spec: AnalysisSpec,
    x_param: str,
    y_param: str,
) -> dict[str, Any]:
    classified: list[tuple[RunRecord, float, float, float]] = []
    for run in runs:
        x = as_float(run.params.get(x_param))
        y = as_float(run.params.get(y_param))
        region = safety_region(run, spec)
        if x is None or y is None or region not in {"safe", "near_critical", "failure"}:
            continue
        classified.append((run, x, y, 1.0 if region == "failure" else 0.0))
    if len(classified) < 2:
        return {
            "x_param": x_param,
            "y_param": y_param,
            "available": False,
            "reason": "at least two classified numeric runs are required",
            "grid": {},
            "nearest_boundary_pairs": [],
            "recommended_resampling_cells": [],
        }
    xs = [item[1] for item in classified]
    ys = [item[2] for item in classified]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    if math.isclose(x_min, x_max) or math.isclose(y_min, y_max):
        return {
            "x_param": x_param,
            "y_param": y_param,
            "available": False,
            "reason": "both axes need non-zero numeric span",
            "grid": {},
            "nearest_boundary_pairs": [],
            "recommended_resampling_cells": [],
        }
    x_span = x_max - x_min
    y_span = y_max - y_min
    coordinates = [((x - x_min) / x_span, (y - y_min) / y_span) for _, x, y, _ in classified]
    labels = [item[3] for item in classified]
    tree = cKDTree(coordinates)
    grid_size = 60
    k = min(24, len(classified))
    x_values = _linspace(x_min, x_max, grid_size)
    y_values = _linspace(y_min, y_max, grid_size)
    probabilities: list[float] = []
    densities: list[float] = []
    uncertainties: list[float] = []
    recommendations: list[dict[str, Any]] = []
    for y in y_values:
        for x in x_values:
            normalized = ((x - x_min) / x_span, (y - y_min) / y_span)
            distances, indexes = tree.query(normalized, k=k)
            if k == 1:
                distance_values = [float(distances)]
                index_values = [int(indexes)]
            else:
                distance_values = [float(value) for value in distances]
                index_values = [int(value) for value in indexes]
            weights = [1.0 / (distance + 1e-6) for distance in distance_values]
            total_weight = sum(weights)
            probability = (
                sum(labels[index] * weight for index, weight in zip(index_values, weights, strict=True))
                / total_weight
            )
            mean_distance = sum(distance_values) / len(distance_values)
            density = 1.0 / (1.0 + mean_distance)
            confidence = min(1.0, k / 24.0) * density
            uncertainty = min(1.0, probability * (1.0 - probability) + (1.0 - confidence) * 0.25)
            probabilities.append(_round(probability))
            densities.append(_round(density))
            uncertainties.append(_round(uncertainty))
            if 0.25 <= probability <= 0.75:
                recommendations.append(
                    {
                        "x": _round(x),
                        "y": _round(y),
                        "failure_probability": _round(probability),
                        "density": _round(density),
                        "uncertainty": _round(uncertainty),
                    }
                )
    recommendations.sort(
        key=lambda item: (
            -float(item["uncertainty"]),
            float(item["density"]),
            float(item["x"]),
            float(item["y"]),
        )
    )
    return {
        "x_param": x_param,
        "y_param": y_param,
        "available": True,
        "grid": {
            "x_values": [_round(value) for value in x_values],
            "y_values": [_round(value) for value in y_values],
            "failure_probability": _matrix(probabilities, grid_size),
            "sample_density": _matrix(densities, grid_size),
            "uncertainty": _matrix(uncertainties, grid_size),
        },
        "nearest_boundary_pairs": _nearest_boundary_pairs(classified, x_min, x_span, y_min, y_span),
        "recommended_resampling_cells": recommendations[:20],
    }


def _nearest_boundary_pairs(
    classified: list[tuple[RunRecord, float, float, float]],
    x_min: float,
    x_span: float,
    y_min: float,
    y_span: float,
) -> list[dict[str, Any]]:
    failures = [item for item in classified if item[3] == 1.0]
    nonfailures = [item for item in classified if item[3] == 0.0]
    if not failures or not nonfailures:
        return []
    failure_coordinates = [
        ((item[1] - x_min) / x_span, (item[2] - y_min) / y_span) for item in failures
    ]
    tree = cKDTree(failure_coordinates)
    pairs = []
    for nonfailure in nonfailures:
        coordinate = ((nonfailure[1] - x_min) / x_span, (nonfailure[2] - y_min) / y_span)
        distance, index = tree.query(coordinate, k=1)
        failure = failures[int(index)]
        pairs.append(
            {
                "distance": _round(float(distance)),
                "nonfailure_run_id": nonfailure[0].run_id,
                "failure_run_id": failure[0].run_id,
                "nonfailure_params": nonfailure[0].params,
                "failure_params": failure[0].params,
            }
        )
    pairs.sort(key=lambda item: (float(item["distance"]), item["nonfailure_run_id"], item["failure_run_id"]))
    return pairs[:20]


def _insight_payload(
    payload: dict[str, Any],
    runs: list[RunRecord],
    spec: AnalysisSpec,
) -> list[dict[str, Any]]:
    insights: list[dict[str, Any]] = []
    run_count = len(runs)
    outcomes = Counter(run["normalized_outcome"] for run in payload["runs"])
    if run_count:
        failure_rate = outcomes["failure"] / run_count
        if failure_rate >= 0.25:
            insights.append(
                _insight(
                    "failure-rate",
                    "high",
                    "High failure rate",
                    f"{outcomes['failure']} of {run_count} runs are classified as failure.",
                    {"run_count": run_count, "failure_rate": failure_rate},
                )
            )
        invalid_rate = outcomes["invalid"] / run_count
        if invalid_rate >= 0.1:
            insights.append(
                _insight(
                    "invalid-rate",
                    "medium",
                    "Invalid cases are common",
                    f"{outcomes['invalid']} of {run_count} runs are invalid; inspect sampler constraints.",
                    {"run_count": run_count, "invalid_rate": invalid_rate},
                )
            )
    for metric in payload["metrics"]:
        if metric.get("metric") == "min_ttc" and metric.get("missing"):
            missing = int(metric["missing"])
            if run_count and missing / run_count >= 0.1:
                insights.append(
                    _insight(
                        "missing-min-ttc",
                        "medium",
                        "Minimum TTC is missing for many runs",
                        f"{missing} runs do not have a usable min TTC summary.",
                        {"missing": missing, "run_count": run_count},
                    )
                )
    for key, boundary in payload["boundary"]["pairs"].items():
        if not boundary.get("available"):
            continue
        recommendations = boundary.get("recommended_resampling_cells") or []
        pairs = boundary.get("nearest_boundary_pairs") or []
        if recommendations:
            cell = recommendations[0]
            insights.append(
                _insight(
                    f"resample-{key}",
                    "medium",
                    f"Uncertain boundary region in {boundary['x_param']} vs {boundary['y_param']}",
                    "Add samples near the highest-uncertainty boundary cell.",
                    {"pair": key, "cell": cell},
                    [{"type": "show_boundary", "label": "Show boundary", "pair": key}],
                )
            )
        if pairs:
            pair = pairs[0]
            insights.append(
                _insight(
                    f"boundary-pair-{key}",
                    "high",
                    f"Nearest safe/failure boundary pair in {boundary['x_param']} vs {boundary['y_param']}",
                    f"{pair['nonfailure_run_id']} and {pair['failure_run_id']} are close in normalized parameter space.",
                    {"pair": key, **pair},
                    [{"type": "select_runs", "label": "Inspect pair", "run_ids": [pair["nonfailure_run_id"], pair["failure_run_id"]]}],
                )
            )
    near_critical = [
        run for run in payload["runs"] if run["safety_region"] == "near_critical"
    ]
    if near_critical:
        insights.append(
            _insight(
                "near-critical",
                "medium",
                "Near-critical successes exist",
                f"{len(near_critical)} successful runs are below the TTC near-critical threshold.",
                {
                    "run_count": len(near_critical),
                    "threshold_s": spec.near_critical_ttc_s,
                    "run_ids": [run["run_id"] for run in near_critical[:50]],
                },
            )
        )
    severity_order = {"high": 0, "medium": 1, "low": 2}
    insights.sort(key=lambda item: (severity_order.get(item["severity"], 3), item["id"]))
    return insights


def _insight(
    insight_id: str,
    severity: str,
    title: str,
    description: str,
    evidence: dict[str, Any],
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": insight_id,
        "severity": severity,
        "title": title,
        "description": description,
        "evidence": evidence,
        "actions": actions or [],
    }


def _linspace(lower: float, upper: float, count: int) -> list[float]:
    if count <= 1:
        return [lower]
    step = (upper - lower) / (count - 1)
    return [lower + step * index for index in range(count)]


def _matrix(values: list[float], width: int) -> list[list[float]]:
    return [values[index : index + width] for index in range(0, len(values), width)]


def _round(value: float) -> float:
    return round(float(value), 6)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def _component_rows(runs: list[RunRecord], spec: AnalysisSpec) -> list[dict[str, Any]]:
    rows = []
    for field in ("sampler_name", "av_name", "simulator_name"):
        groups: dict[str, list[RunRecord]] = defaultdict(list)
        for run in runs:
            if run.metadata.get(field) not in {None, ""}:
                groups[str(run.metadata[field])].append(run)
        for value, members in sorted(groups.items()):
            counts = grouped_outcomes(members, spec)
            ci_low, ci_high = wilson_interval(counts["failure"], len(members))
            ttc_values = [
                item
                for run in members
                if (item := metric_value(run, spec, "min_ttc")) is not None
            ]
            rows.append(
                {
                    "component_type": field.removesuffix("_name"),
                    "component": value,
                    "run_count": len(members),
                    "valid_count": len(members) - counts["invalid"],
                    "failure_count": counts["failure"],
                    "failure_rate": counts["failure"] / len(members),
                    "failure_rate_ci_low": ci_low,
                    "failure_rate_ci_high": ci_high,
                    "min_ttc_min": min(ttc_values) if ttc_values else None,
                    "min_ttc_mean": numeric_summary(ttc_values)["mean"],
                }
            )
    return rows


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _write_html_report(
    path: Path,
    *,
    output_dir: Path,
    payload: dict[str, Any],
    mode: str,
) -> None:
    static_note = (
        "<p class=\"notice\">Static report mode requested. Official tables and artifact links "
        "are shown; interactive controls read the same frozen payload.</p>"
        if mode == "static"
        else ""
    )
    html_text = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>PISA Validation Evidence</title>
  <style>
    :root { color-scheme: light; --navy:#102033; --ink:#17202a; --muted:#64748b; --line:#d9e1ea; --paper:#fff; --bg:#f4f7fa; --green:#16a34a; --red:#dc2626; --blue:#2563eb; --orange:#f59e0b; --gray:#6b7280; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:Inter,system-ui,sans-serif; color:var(--ink); background:var(--bg); }
    header { padding:24px 32px; background:var(--navy); color:white; }
    header h1 { margin:0 0 6px; font-size:28px; }
    header p { margin:0; color:#dbeafe; }
    .shell { display:grid; grid-template-columns:220px minmax(0,1fr); gap:0; }
    nav { position:sticky; top:0; height:100vh; padding:18px; background:#0f1c2d; color:white; overflow:auto; }
    nav a { display:block; color:#dbeafe; text-decoration:none; padding:9px 8px; border-radius:6px; }
    nav a:hover { background:#1f3450; }
    main { min-width:0; padding:18px 28px 56px; }
    section { background:var(--paper); border:1px solid var(--line); border-radius:8px; padding:16px; margin:16px 0; }
    h2 { margin:0 0 12px; font-size:20px; }
    h3 { margin:18px 0 8px; font-size:15px; }
    .topbar { position:sticky; top:0; z-index:3; background:#eef4f9; border:1px solid var(--line); border-radius:8px; padding:10px; display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:10px; }
    label { display:grid; gap:4px; font-size:12px; font-weight:700; color:#314155; }
    select,input,button { min-height:34px; border:1px solid #aab7c4; border-radius:6px; padding:6px 8px; background:white; }
    button { cursor:pointer; background:var(--navy); color:white; border-color:var(--navy); }
    .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; }
    .card { background:white; border:1px solid var(--line); border-radius:8px; padding:12px; }
    .card b { display:block; margin-top:4px; font-size:24px; }
    .layout { display:grid; grid-template-columns:minmax(0,1fr) 380px; gap:14px; align-items:start; }
    canvas { width:100%; min-height:620px; display:block; background:#101820; border:1px solid var(--line); border-radius:6px; }
    .panel { border:1px solid var(--line); border-radius:8px; padding:12px; background:#f8fafc; }
    .filters { display:flex; flex-wrap:wrap; gap:8px 14px; margin:8px 0; }
    .filters label { display:inline-flex; gap:6px; align-items:center; font-weight:500; }
    table { border-collapse:collapse; width:100%; font-size:13px; display:block; overflow:auto; }
    th,td { border-bottom:1px solid #e5e7eb; padding:7px 9px; text-align:left; white-space:nowrap; }
    pre { max-height:360px; overflow:auto; background:white; border:1px solid var(--line); border-radius:6px; padding:10px; font-size:12px; }
    .muted { color:var(--muted); }
    .notice { border-left:4px solid var(--orange); background:#fffbeb; padding:10px 12px; }
    .insights { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:10px; }
    .insight { border:1px solid var(--line); border-radius:8px; padding:12px; background:white; }
    .high { border-left:4px solid var(--red); } .medium { border-left:4px solid var(--orange); } .low { border-left:4px solid var(--gray); }
    .figures { display:grid; grid-template-columns:repeat(auto-fit,minmax(360px,1fr)); gap:12px; }
    .figure { border:1px solid var(--line); border-radius:8px; padding:10px; background:white; }
    .figure img { width:100%; height:auto; }
    .inline { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; align-items:end; }
    @media (max-width: 1000px) { .shell { grid-template-columns:1fr; } nav { position:relative; height:auto; } .layout { grid-template-columns:1fr; } }
  </style>
</head>
<body>
<header>
  <h1>PISA Validation Evidence</h1>
  <p>Reproducible scenario-level safety analysis with offline exploration</p>
</header>
<div class="shell">
<nav>
  <a href="#overview">Overview</a>
  <a href="#explorer">Parameter Space</a>
  <a href="#boundary">Boundary Explorer</a>
  <a href="#insights">Insights</a>
  <a href="#cases">Representative Cases</a>
  <a href="#comparison">Comparison</a>
  <a href="#quality">Data Quality</a>
  <a href="#advanced">Spec Lab</a>
</nav>
<main>
  __STATIC_NOTE__
  <div class="topbar">
    <label>Parameter pair<select id="pair-select"></select></label>
    <label>X axis<select id="x-select"></select></label>
    <label>Y axis<select id="y-select"></select></label>
    <label>Z axis<select id="z-select"></select></label>
    <label>Color by<select id="color-select"></select></label>
    <label>View<select id="view-select"><option value="scatter">scatter</option><option value="heatmap">heatmap</option><option value="3d">3D</option></select></label>
    <label><span>Overlay</span><select id="overlay-select"><option value="none">none</option><option value="boundary">boundary</option><option value="uncertainty">uncertainty</option></select></label>
  </div>
  <section id="overview">
    <h2>Overview</h2>
    <div id="summary-cards" class="cards"></div>
    <h3>Outcomes</h3><div id="outcome-table"></div>
    <h3>Safety metrics</h3><div id="metric-table"></div>
  </section>
  <section id="explorer">
    <h2>Parameter Space Explorer</h2>
    <div class="filters"><strong>Outcome filters</strong><span id="outcome-filters"></span></div>
    <div class="filters"><strong>Safety filters</strong><span id="safety-filters"></span></div>
    <div class="filters"><strong>Status filters</strong><span id="status-filters"></span></div>
    <div class="layout">
      <canvas id="space-canvas"></canvas>
      <aside class="panel">
        <h3>Selected Set</h3>
        <div id="selection-summary" class="cards"></div>
        <button id="download-filtered" type="button">Download Filtered CSV</button>
        <h3>Run Detail</h3>
        <label>Run<select id="run-select"></select></label>
        <input id="search" placeholder="Filter by run id, parameter, component, outcome, or reason">
        <pre id="detail">Click a point or choose a run.</pre>
      </aside>
    </div>
  </section>
  <section id="boundary">
    <h2>Boundary Explorer</h2>
    <p class="muted">Boundary overlays use deterministic k-nearest-neighbor grids generated by the CLI. High-uncertainty cells are candidates for additional sampling.</p>
    <div id="boundary-table"></div>
  </section>
  <section id="insights">
    <h2>Insights</h2>
    <div id="insight-list" class="insights"></div>
  </section>
  <section id="cases">
    <h2>Representative Cases</h2>
    <div id="case-table"></div>
  </section>
  <section id="comparison">
    <h2>Comparison</h2>
    <h3>Component comparison</h3><div id="component-table"></div>
    <h3>Outcome transition</h3><div id="transition-table"></div>
    <h3>Paired statistics</h3><div id="paired-table"></div>
    <h3>Failure disagreement</h3><div id="disagreement-table"></div>
  </section>
  <section id="quality">
    <h2>Data Quality and Provenance</h2>
    <div id="quality-cards" class="cards"></div>
    <h3>Findings</h3><div id="quality-table"></div>
    <h3>Warnings</h3><pre id="warnings"></pre>
  </section>
  <section id="advanced">
    <h2>Advanced run explorer and Spec Lab</h2>
    <p class="muted">Draft changes never modify official evidence. Export YAML and rerun the CLI to produce official evidence.</p>
    <div class="inline">
      <label>Near-critical TTC (s)<input id="draft-ttc" type="number" step="0.1"></label>
      <label>Rule source<select id="rule-source"><option value="metric">metric</option><option value="param">parameter</option></select></label>
      <label>Field<select id="rule-field"></select></label>
      <label>Operator<select id="rule-op"><option value="lt">&lt;</option><option value="le">&lt;=</option><option value="gt">&gt;</option><option value="ge">&gt;=</option><option value="eq">==</option><option value="between">between</option><option value="outside">outside</option></select></label>
      <label>Value<input id="rule-value" placeholder="1.0 or min,max"></label>
      <label>Draft outcome<select id="rule-outcome"><option value="failure">failure</option><option value="invalid">invalid</option><option value="success">success</option><option value="unclassified">unclassified</option></select></label>
      <button id="apply-draft" type="button">Apply Draft</button>
      <button id="draft-download" type="button">Download YAML spec</button>
    </div>
    <pre id="draft-summary">No draft rule applied.</pre>
  </section>
  <section>
    <h2>Evidence Figures</h2>
    <div id="figure-list" class="figures"></div>
  </section>
</main>
</div>
<script src="analysis_data.js"></script>
<script>
(() => {
  const payload = window.PISA_ANALYSIS_DATA || {runs: [], parameters: [], metrics: [], parameter_pairs: [], boundary: {pairs: {}}};
  const runs = payload.runs || [];
  const numericParams = payload.parameters.filter(item => item.numeric).map(item => item.parameter);
  const metricNames = payload.metrics.map(item => item.metric);
  const state = { projected: [], selectedIds: new Set(), draftRuns: null };
  const els = {
    pair: document.getElementById('pair-select'), x: document.getElementById('x-select'), y: document.getElementById('y-select'), z: document.getElementById('z-select'),
    color: document.getElementById('color-select'), view: document.getElementById('view-select'), overlay: document.getElementById('overlay-select'),
    canvas: document.getElementById('space-canvas'), detail: document.getElementById('detail'), runSelect: document.getElementById('run-select'), search: document.getElementById('search'),
    outcomeFilters: document.getElementById('outcome-filters'), safetyFilters: document.getElementById('safety-filters'), statusFilters: document.getElementById('status-filters')
  };
  const ctx = els.canvas.getContext('2d');
  const semanticColors = {success:'#16a34a', failure:'#dc2626', invalid:'#2563eb', execution_error:'#7f1d1d', unclassified:'#6b7280', safe:'#16a34a', near_critical:'#f59e0b', unknown:'#6b7280'};
  const palette = ['#7c3aed','#f59e0b','#0891b2','#be123c','#4b5563','#84cc16','#c026d3','#0f766e'];
  let yaw = -0.65, pitch = 0.55, zoom = 1, dragging = false, lastX = 0, lastY = 0;

  function text(value) { return value === null || value === undefined ? '' : String(value); }
  function number(value) { const n = Number(value); return Number.isFinite(n) ? n : null; }
  function fmt(value) { const n = Number(value); return Number.isFinite(n) ? Number(n.toPrecision(4)).toString() : text(value); }
  function addOptions(select, values, includeNone=false) {
    select.textContent = '';
    if (includeNone) select.appendChild(new Option('(none)', ''));
    values.forEach(value => select.appendChild(new Option(value, value)));
  }
  function unique(values) { return [...new Set(values.map(value => text(value || 'unknown')))].sort(); }
  function checkboxGroup(container, values) {
    container.textContent = '';
    unique(values).forEach(value => {
      const label = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'checkbox'; input.value = value; input.checked = true; input.addEventListener('change', draw);
      label.append(input, document.createTextNode(' ' + value));
      container.appendChild(label);
    });
  }
  function checked(container) { return new Set([...container.querySelectorAll('input:checked')].map(input => input.value)); }
  function table(rows, limit=500) {
    if (!rows || !rows.length) return '<p class="muted">No data.</p>';
    const columns = [...new Set(rows.flatMap(row => Object.keys(row)))];
    const esc = value => text(value).replace(/[&<>"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));
    const body = rows.slice(0, limit).map(row => '<tr>' + columns.map(col => '<td>' + esc(row[col]) + '</td>').join('') + '</tr>').join('');
    return '<table><thead><tr>' + columns.map(col => '<th>' + esc(col) + '</th>').join('') + '</tr></thead><tbody>' + body + '</tbody></table>';
  }
  function cards(items) {
    return items.map(item => '<div class="card">' + item.label + '<b>' + item.value + '</b></div>').join('');
  }
  function fieldValue(run, key) {
    if (!key) return null;
    if (key === 'normalized_outcome') return run.draft_outcome || run.normalized_outcome || 'unknown';
    if (key === 'safety_region') return draftSafety(run);
    if (key === 'termination_reason') return run.termination_reason || 'unknown';
    if (key === 'status') return run.status || 'unknown';
    if (key.startsWith('param:')) return run.params[key.slice(6)];
    if (key.startsWith('metric:')) return run.metrics[key.slice(7)];
    if (key.startsWith('metadata:')) return run.metadata[key.slice(9)];
    return run[key];
  }
  function numericField(run, key) { return number(fieldValue(run, key)); }
  function paramValue(run, param) { return number(run.params[param]); }
  function filteredRuns() {
    const outcome = checked(els.outcomeFilters), safety = checked(els.safetyFilters), status = checked(els.statusFilters);
    return activeRuns().filter(run => outcome.has(run.draft_outcome || run.normalized_outcome || 'unknown') && safety.has(draftSafety(run)) && status.has(run.status || 'unknown'));
  }
  function activeRuns() { return state.draftRuns || runs; }
  function draftSafety(run) {
    const threshold = number(document.getElementById('draft-ttc').value) ?? payload.summary.near_critical_ttc_s;
    const ttc = number(run.metrics[payload.metrics.find(m => m.metric === 'min_ttc')?.source || '']);
    if ((run.draft_outcome || run.normalized_outcome) === 'invalid') return 'invalid';
    if ((run.draft_outcome || run.normalized_outcome) === 'failure') return 'failure';
    if ((run.draft_outcome || run.normalized_outcome) === 'success' && ttc !== null && ttc < threshold) return 'near_critical';
    return run.safety_region || 'unknown';
  }
  function colorState(rows) {
    const key = els.color.value;
    const numericValues = rows.map(run => numericField(run, key));
    const present = numericValues.filter(value => value !== null);
    if ((key.startsWith('param:') || key.startsWith('metric:')) && present.length) {
      let min = Math.min(...present), max = Math.max(...present);
      if (min === max) { min -= 0.5; max += 0.5; }
      return {mode:'continuous', numericValues, min, max};
    }
    const values = rows.map(run => text(fieldValue(run, key) || 'unknown'));
    const keys = unique(values);
    const map = new Map(keys.map((value, index) => [value, semanticColors[value] || palette[index % palette.length]]));
    return {mode:'categorical', values, map};
  }
  function colorFor(colors, index) {
    if (colors.mode === 'continuous') {
      const value = colors.numericValues[index];
      if (value === null) return '#9ca3af';
      const t = Math.max(0, Math.min(1, (value - colors.min) / (colors.max - colors.min)));
      return `rgb(${Math.round(239 - t * 198)},${Math.round(246 - t * 111)},${Math.round(255 - t * 35)})`;
    }
    return colors.map.get(colors.values[index]) || '#9ca3af';
  }
  function range(values) {
    let min = Math.min(...values), max = Math.max(...values);
    if (min === max) { min -= 0.5; max += 0.5; }
    const pad = (max - min) * 0.04;
    return [min - pad, max + pad];
  }
  function resize() {
    const rect = els.canvas.getBoundingClientRect();
    els.canvas.width = Math.max(720, Math.floor(rect.width)) * devicePixelRatio;
    els.canvas.height = 620 * devicePixelRatio;
    draw();
  }
  function clearCanvas() { ctx.clearRect(0,0,els.canvas.width,els.canvas.height); ctx.fillStyle = '#101820'; ctx.fillRect(0,0,els.canvas.width,els.canvas.height); }
  function axes(xLabel, yLabel, w, h, margin) {
    ctx.strokeStyle = '#d7e0ea'; ctx.lineWidth = devicePixelRatio; ctx.beginPath(); ctx.moveTo(margin,h-margin); ctx.lineTo(w-margin,h-margin); ctx.moveTo(margin,margin); ctx.lineTo(margin,h-margin); ctx.stroke();
    ctx.fillStyle = '#edf3f8'; ctx.font = `${13 * devicePixelRatio}px system-ui`; ctx.textAlign = 'center'; ctx.fillText(xLabel, w/2, h - 18 * devicePixelRatio);
    ctx.save(); ctx.translate(18 * devicePixelRatio, h/2); ctx.rotate(-Math.PI/2); ctx.fillText(yLabel, 0, 0); ctx.restore();
  }
  function draw() {
    const rows = filteredRuns();
    const xParam = els.x.value, yParam = els.y.value, zParam = els.z.value;
    const colors = colorState(rows);
    clearCanvas(); state.projected = [];
    if (!rows.length || !xParam || !yParam) return;
    if (els.view.value === 'heatmap') drawHeatmap(rows, xParam, yParam);
    else if (els.view.value === '3d' && zParam) draw3d(rows, xParam, yParam, zParam, colors);
    else drawScatter(rows, xParam, yParam, colors);
    if (els.overlay.value !== 'none') drawBoundaryOverlay();
    drawSelectionSummary(rows);
  }
  function drawScatter(rows, xParam, yParam, colors) {
    const points = rows.map((run, index) => ({run, index, x:paramValue(run, xParam), y:paramValue(run, yParam)})).filter(p => p.x !== null && p.y !== null);
    if (!points.length) return;
    const w = els.canvas.width, h = els.canvas.height, margin = 70 * devicePixelRatio;
    const [xMin,xMax] = range(points.map(p => p.x)), [yMin,yMax] = range(points.map(p => p.y));
    axes(xParam, yParam, w, h, margin);
    points.forEach(point => {
      const sx = margin + (point.x - xMin) / (xMax - xMin) * (w - 2*margin);
      const sy = h - margin - (point.y - yMin) / (yMax - yMin) * (h - 2*margin);
      ctx.beginPath(); ctx.arc(sx, sy, 4.2 * devicePixelRatio, 0, Math.PI*2);
      ctx.fillStyle = colorFor(colors, point.index); ctx.globalAlpha = 0.78; ctx.fill(); ctx.globalAlpha = 1;
      state.projected.push({x:sx, y:sy, run:point.run});
    });
  }
  function drawHeatmap(rows, xParam, yParam) {
    const points = rows.map(run => ({run, x:paramValue(run, xParam), y:paramValue(run, yParam), fail:(run.draft_outcome || run.normalized_outcome) === 'failure'})).filter(p => p.x !== null && p.y !== null);
    if (!points.length) return;
    const w = els.canvas.width, h = els.canvas.height, margin = 70 * devicePixelRatio, bins = 30;
    const [xMin,xMax] = range(points.map(p => p.x)), [yMin,yMax] = range(points.map(p => p.y));
    const cells = Array.from({length: bins*bins}, () => ({n:0, f:0}));
    points.forEach(p => { const ix = Math.min(bins-1, Math.max(0, Math.floor((p.x-xMin)/(xMax-xMin)*bins))); const iy = Math.min(bins-1, Math.max(0, Math.floor((p.y-yMin)/(yMax-yMin)*bins))); cells[iy*bins+ix].n++; if (p.fail) cells[iy*bins+ix].f++; });
    const cw = (w-2*margin)/bins, ch = (h-2*margin)/bins;
    cells.forEach((cell, index) => { const ix = index % bins, iy = Math.floor(index/bins), rate = cell.n ? cell.f/cell.n : 0; ctx.fillStyle = cell.n ? `rgba(220,38,38,${0.15 + rate*0.75})` : 'rgba(148,163,184,0.08)'; ctx.fillRect(margin + ix*cw, h-margin-(iy+1)*ch, cw+1, ch+1); });
    axes(xParam, yParam, w, h, margin);
  }
  function draw3d(rows, xParam, yParam, zParam, colors) {
    const points = rows.map((run, index) => ({run, index, x:paramValue(run,xParam), y:paramValue(run,yParam), z:paramValue(run,zParam)})).filter(p => p.x !== null && p.y !== null && p.z !== null);
    if (!points.length) return;
    const w = els.canvas.width, h = els.canvas.height;
    const ranges = [range(points.map(p => p.x)), range(points.map(p => p.y)), range(points.map(p => p.z))];
    function norm(v, i) { return (v-ranges[i][0])/(ranges[i][1]-ranges[i][0])*2-1; }
    function project(x,y,z) { const cy=Math.cos(yaw), sy=Math.sin(yaw), cp=Math.cos(pitch), sp=Math.sin(pitch); let x1=cy*x+sy*z, z1=-sy*x+cy*z; let y1=cp*y-sp*z1, z2=sp*y+cp*z1; const s=Math.min(w,h)*0.34*zoom/(1.7+z2); return {x:w/2+x1*s, y:h/2-y1*s, z:z2}; }
    ctx.fillStyle = '#edf3f8'; ctx.font = `${13 * devicePixelRatio}px system-ui`; ctx.fillText(`${xParam} / ${yParam} / ${zParam}`, 20*devicePixelRatio, 28*devicePixelRatio);
    points.map(p => ({...p, p:project(norm(p.x,0), norm(p.y,1), norm(p.z,2))})).sort((a,b) => a.p.z-b.p.z).forEach(point => { ctx.beginPath(); ctx.arc(point.p.x, point.p.y, 4.2*devicePixelRatio, 0, Math.PI*2); ctx.fillStyle = colorFor(colors, point.index); ctx.globalAlpha=0.8; ctx.fill(); ctx.globalAlpha=1; state.projected.push({x:point.p.x, y:point.p.y, run:point.run}); });
  }
  function currentBoundary() {
    const key = pairKey(els.x.value, els.y.value);
    return payload.boundary?.pairs?.[key] || null;
  }
  function pairKey(x, y) {
    const exact = payload.parameter_pairs.find(pair => pair.x === x && pair.y === y);
    if (exact) return exact.key;
    return `${x.toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_|_$/g,'')}__${y.toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_|_$/g,'')}`;
  }
  function drawBoundaryOverlay() {
    const boundary = currentBoundary();
    if (!boundary || !boundary.available || !state.projected.length) return;
    const rows = filteredRuns();
    const points = rows.map(run => ({x:paramValue(run, els.x.value), y:paramValue(run, els.y.value)})).filter(p => p.x !== null && p.y !== null);
    if (!points.length) return;
    const w = els.canvas.width, h = els.canvas.height, margin = 70 * devicePixelRatio;
    const [xMin,xMax] = range(points.map(p => p.x)), [yMin,yMax] = range(points.map(p => p.y));
    const sx = x => margin + (x-xMin)/(xMax-xMin)*(w-2*margin);
    const sy = y => h - margin - (y-yMin)/(yMax-yMin)*(h-2*margin);
    ctx.strokeStyle = '#facc15'; ctx.lineWidth = 2 * devicePixelRatio; ctx.setLineDash([6*devicePixelRatio, 5*devicePixelRatio]);
    for (const pair of boundary.nearest_boundary_pairs || []) {
      const a = pair.nonfailure_params, b = pair.failure_params;
      if (a[els.x.value] === undefined || b[els.x.value] === undefined) continue;
      ctx.beginPath(); ctx.moveTo(sx(Number(a[els.x.value])), sy(Number(a[els.y.value]))); ctx.lineTo(sx(Number(b[els.x.value])), sy(Number(b[els.y.value]))); ctx.stroke();
    }
    ctx.setLineDash([]);
    for (const cell of (boundary.recommended_resampling_cells || []).slice(0, 20)) {
      ctx.fillStyle = els.overlay.value === 'uncertainty' ? '#f59e0b' : '#facc15';
      ctx.beginPath(); ctx.arc(sx(cell.x), sy(cell.y), 6*devicePixelRatio, 0, Math.PI*2); ctx.fill();
    }
  }
  function selectNearest(clientX, clientY) {
    const rect = els.canvas.getBoundingClientRect();
    const x = (clientX - rect.left) * devicePixelRatio, y = (clientY - rect.top) * devicePixelRatio;
    let best = null, bestD = Infinity;
    state.projected.forEach(item => { const d = (item.x-x)**2 + (item.y-y)**2; if (d < bestD) { bestD = d; best = item; } });
    if (best && bestD < (18 * devicePixelRatio) ** 2) showRun(best.run);
  }
  function showRun(run) {
    els.detail.textContent = JSON.stringify(run || {}, null, 2);
    if (run) els.runSelect.value = run.run_id;
  }
  function drawSelectionSummary(rows) {
    const outcomes = rows.reduce((map, run) => { const key = run.draft_outcome || run.normalized_outcome || 'unknown'; map[key] = (map[key] || 0) + 1; return map; }, {});
    const failureRate = rows.length ? ((outcomes.failure || 0) / rows.length * 100).toFixed(1) + '%' : '0%';
    document.getElementById('selection-summary').innerHTML = cards([{label:'Visible', value:rows.length}, {label:'Failure rate', value:failureRate}, {label:'Failures', value:outcomes.failure || 0}, {label:'Invalid', value:outcomes.invalid || 0}]);
  }
  function renderStatic() {
    const s = payload.summary;
    document.getElementById('summary-cards').innerHTML = cards([{label:'Runs', value:s.run_count}, {label:'Experiments', value:s.experiment_count}, {label:'Parameters', value:s.parameter_count}, {label:'Warnings', value:s.warning_count}]);
    document.getElementById('outcome-table').innerHTML = table(s.outcomes);
    document.getElementById('metric-table').innerHTML = table(payload.metrics);
    document.getElementById('case-table').innerHTML = table(payload.representative_cases);
    document.getElementById('component-table').innerHTML = table(payload.comparison.component_comparison);
    document.getElementById('transition-table').innerHTML = table(payload.comparison.outcome_transition);
    document.getElementById('paired-table').innerHTML = table(payload.comparison.paired_summary);
    document.getElementById('disagreement-table').innerHTML = table(payload.comparison.failure_disagreement);
    document.getElementById('quality-table').innerHTML = table(payload.data_quality.findings);
    document.getElementById('warnings').textContent = (payload.data_quality.warnings || []).join('\\n') || 'No warnings.';
    document.getElementById('quality-cards').innerHTML = cards(Object.entries(payload.data_quality.source_files || {}).map(([label,value]) => ({label, value})));
    document.getElementById('boundary-table').innerHTML = table(Object.entries(payload.boundary.pairs || {}).map(([key,value]) => ({pair:key, available:value.available, recommendations:(value.recommended_resampling_cells || []).length, nearest_pairs:(value.nearest_boundary_pairs || []).length, reason:value.reason || ''})));
    document.getElementById('insight-list').innerHTML = (payload.insights || []).map(item => `<article class="insight ${item.severity}"><h3>${item.title}</h3><p>${item.description}</p><pre>${JSON.stringify(item.evidence, null, 2)}</pre></article>`).join('') || '<p class="muted">No automatic insights.</p>';
    document.getElementById('figure-list').innerHTML = payload.figures.filter(f => f.format === 'svg').map(f => `<article class="figure" data-pair="${f.pair}"><h3>${f.title}</h3><a href="../${f.path}"><img loading="lazy" src="../${f.path}" alt="${f.title}"></a></article>`).join('');
  }
  function updateFigureVisibility() {
    const key = pairKey(els.x.value, els.y.value);
    document.querySelectorAll('.figure').forEach(item => { item.hidden = item.dataset.pair !== 'global' && item.dataset.pair !== key; });
  }
  function populate() {
    addOptions(els.x, numericParams); addOptions(els.y, numericParams); addOptions(els.z, numericParams, true);
    addOptions(els.color, ['normalized_outcome','safety_region','termination_reason','status', ...numericParams.map(p => 'param:' + p), ...metricNames.map(m => 'metric:' + m)]);
    els.pair.textContent = ''; payload.parameter_pairs.forEach(pair => els.pair.appendChild(new Option(`${pair.x} vs ${pair.y}`, pair.key)));
    const axes = payload.summary.default_axes || {}; els.x.value = axes.x || numericParams[0] || ''; els.y.value = axes.y || numericParams.find(p => p !== els.x.value) || ''; els.color.value = 'normalized_outcome';
    checkboxGroup(els.outcomeFilters, runs.map(run => run.normalized_outcome || 'unknown'));
    checkboxGroup(els.safetyFilters, runs.map(run => run.safety_region || 'unknown'));
    checkboxGroup(els.statusFilters, runs.map(run => run.status || 'unknown'));
    runs.forEach(run => els.runSelect.appendChild(new Option(`${run.run_id} - ${run.normalized_outcome}`, run.run_id)));
    document.getElementById('draft-ttc').value = payload.summary.near_critical_ttc_s;
    updateRuleFields();
  }
  function updateRuleFields() {
    addOptions(document.getElementById('rule-field'), document.getElementById('rule-source').value === 'metric' ? metricNames : numericParams);
  }
  function compare(value, op, raw) {
    const n = number(value); if (n === null) return false;
    if (op === 'between' || op === 'outside') { const parts = raw.split(',').map(v => number(v.trim())); if (parts.length !== 2 || parts.some(v => v === null)) return false; const inside = n >= parts[0] && n <= parts[1]; return op === 'between' ? inside : !inside; }
    const threshold = number(raw); if (threshold === null) return false;
    return op === 'lt' ? n < threshold : op === 'le' ? n <= threshold : op === 'gt' ? n > threshold : op === 'ge' ? n >= threshold : Math.abs(n - threshold) < 1e-9;
  }
  function applyDraft() {
    const source = document.getElementById('rule-source').value, field = document.getElementById('rule-field').value, op = document.getElementById('rule-op').value, raw = document.getElementById('rule-value').value, outcome = document.getElementById('rule-outcome').value;
    let changed = 0, triggered = 0;
    state.draftRuns = runs.map(run => {
      const clone = JSON.parse(JSON.stringify(run));
      const value = source === 'metric' ? clone.metrics[field] : clone.params[field];
      if (compare(value, op, raw)) { triggered++; clone.draft_outcome = outcome; if (outcome !== run.normalized_outcome) changed++; }
      return clone;
    });
    document.getElementById('draft-summary').textContent = `Rule: ${source}.${field} ${op} ${raw} -> ${outcome}\\nTriggered: ${triggered} / ${runs.length}\\nChanged official outcome: ${changed}`;
    checkboxGroup(els.outcomeFilters, activeRuns().map(run => run.draft_outcome || run.normalized_outcome || 'unknown'));
    draw();
  }
  function downloadFiltered() {
    const rows = filteredRuns();
    const paramSet = new Set(), metricSet = new Set();
    rows.forEach(run => { Object.keys(run.params).forEach(k => paramSet.add(k)); Object.keys(run.metrics).forEach(k => metricSet.add(k)); });
    const cols = ['run_id','sample_id','status','normalized_outcome','draft_outcome','safety_region','termination_reason', ...[...paramSet].map(k => 'param.'+k), ...[...metricSet].map(k => 'metric.'+k)];
    const esc = value => '"' + text(value).replace(/"/g, '""') + '"';
    const lines = [cols.join(',')];
    rows.forEach(run => lines.push(cols.map(col => col.startsWith('param.') ? esc(run.params[col.slice(6)]) : col.startsWith('metric.') ? esc(run.metrics[col.slice(7)]) : esc(run[col])).join(',')));
    const blob = new Blob([lines.join('\\n')], {type:'text/csv'});
    const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = 'filtered_runs.csv'; link.click(); URL.revokeObjectURL(link.href);
  }
  function downloadSpec() {
    const yaml = `version: 2\\nvalidation:\\n  mode: strict\\nparameters:\\n  mode: single\\n  axes:\\n    x: ${els.x.value}\\n    y: ${els.y.value}\\nthresholds:\\n  near_critical_ttc_s: ${document.getElementById('draft-ttc').value}\\noutput:\\n  formats: [svg, png]\\n`;
    const blob = new Blob([yaml], {type:'text/yaml'});
    const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = 'analysis_spec.yaml'; link.click(); URL.revokeObjectURL(link.href);
  }
  populate(); renderStatic(); resize();
  els.pair.addEventListener('change', () => { const pair = payload.parameter_pairs.find(item => item.key === els.pair.value); if (pair) { els.x.value = pair.x; els.y.value = pair.y; } updateFigureVisibility(); draw(); });
  [els.x,els.y,els.z,els.color,els.view,els.overlay].forEach(el => el.addEventListener('change', () => { updateFigureVisibility(); draw(); }));
  els.runSelect.addEventListener('change', () => showRun(activeRuns().find(run => run.run_id === els.runSelect.value)));
  els.search.addEventListener('input', () => { const q = els.search.value.toLowerCase(); if (!q) return; els.detail.textContent = JSON.stringify(activeRuns().filter(run => JSON.stringify(run).toLowerCase().includes(q)).slice(0, 100), null, 2); });
  els.canvas.addEventListener('click', event => selectNearest(event.clientX, event.clientY));
  els.canvas.addEventListener('mousedown', event => { dragging = true; lastX = event.clientX; lastY = event.clientY; });
  window.addEventListener('mouseup', () => dragging = false);
  window.addEventListener('mousemove', event => { if (!dragging || els.view.value !== '3d') return; yaw += (event.clientX-lastX)*0.01; pitch += (event.clientY-lastY)*0.01; lastX=event.clientX; lastY=event.clientY; draw(); });
  els.canvas.addEventListener('wheel', event => { if (els.view.value !== '3d') return; event.preventDefault(); zoom *= Math.exp(-event.deltaY*0.001); draw(); });
  document.getElementById('rule-source').addEventListener('change', updateRuleFields);
  document.getElementById('apply-draft').addEventListener('click', applyDraft);
  document.getElementById('download-filtered').addEventListener('click', downloadFiltered);
  document.getElementById('draft-download').addEventListener('click', downloadSpec);
  window.addEventListener('resize', resize);
  updateFigureVisibility();
})();
</script>
</body></html>
"""
    path.write_text(
        html_text.replace("__STATIC_NOTE__", static_note),
        encoding="utf-8",
    )


def _write_markdown_report(
    path: Path,
    *,
    runs: list[RunRecord],
    outcome_rows,
    metric_rows,
    parameter_rows,
    paired_summary_rows,
    cases,
    warnings,
) -> None:
    lines = [
        "# PISA Validation Evidence",
        "",
        f"- Runs: {len(runs)}",
        f"- Experiments: {len({run.experiment_id for run in runs})}",
        "",
        "## Outcomes",
        "",
        _markdown_table(outcome_rows),
        "",
        "## Metrics",
        "",
        _markdown_table(metric_rows),
        "",
        "## Parameter Coverage",
        "",
        _markdown_table(parameter_rows),
        "",
        "## Paired Comparison",
        "",
        _markdown_table(paired_summary_rows),
        "",
        "## Representative Cases",
        "",
        _markdown_table(
            [
                {
                    "case": case.case_type,
                    "run_id": case.run.run_id,
                    "reason": case.reason,
                }
                for case in cases
            ]
        ),
        "",
        "## Limitations",
        "",
        *[f"- {warning}" for warning in warnings],
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_latex_summary(path: Path, *, outcome_rows, metric_rows, paired_summary_rows) -> None:
    lines = [
        "% Generated by pisa-analysis-tools",
        "\\begin{tabular}{lrr}",
        "\\hline",
        "Outcome & Count & Percentage \\\\",
        "\\hline",
    ]
    for row in outcome_rows:
        lines.append(
            f"{_latex(row['outcome'])} & {row['count']} & {row['percentage']:.2f}\\% \\\\"
        )
    lines.extend(["\\hline", "\\end{tabular}", "", "\\begin{tabular}{lrrrr}", "\\hline"])
    lines.append("Metric & Mean & Median & Min & Max \\\\")
    lines.append("\\hline")
    for row in metric_rows:
        lines.append(
            f"{_latex(row['metric'])} & {_number(row['mean'])} & {_number(row['median'])} & "
            f"{_number(row['min'])} & {_number(row['max'])} \\\\"
        )
    lines.extend(["\\hline", "\\end{tabular}", ""])
    if paired_summary_rows:
        lines.extend(
            [
                "\\begin{tabular}{llrr}",
                "\\hline",
                "Comparison & Metric & Matched & Mean delta \\\\",
                "\\hline",
            ]
        )
        for row in paired_summary_rows:
            lines.append(
                f"{_latex(row.get('comparison'))} & {_latex(row.get('metric'))} & "
                f"{row.get('matched', '')} & {_number(row.get('mean_delta'))} \\\\"
            )
        lines.extend(["\\hline", "\\end{tabular}", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_limitations(path: Path, manifest: dict[str, Any], warnings: list[str]) -> None:
    sources = manifest["source_files"]
    lines = [
        "# Data Quality and Limitations",
        "",
        f"- Result summaries: {sources['result_csv']} / {manifest['run_count']}",
        f"- Frame metric traces: {sources['frame_metrics_csv']} / {manifest['run_count']}",
        f"- Agent-state traces: {sources['agent_states_csv']} / {manifest['run_count']}",
        f"- Agent geometry streams: {sources['agent_geometry_csv']} / {manifest['run_count']}",
        f"- Collision event streams: {sources['collision_events_csv']} / {manifest['run_count']}",
        f"- Scenario event streams: {sources['scenario_events_csv']} / {manifest['run_count']}",
        f"- Control command streams: {sources['control_commands_csv']} / {manifest['run_count']}",
        "",
        "## Findings",
        "",
        *([f"- {warning}" for warning in warnings] or ["- No automatic warnings."]),
        "",
        "Component comparisons demonstrate integration under a common workflow; they are not "
        "automatically a fair capability ranking. Simulator trajectory differences retain "
        "backend-specific execution semantics.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _figure_pair(path: Path) -> str:
    return path.parent.name if path.parent.parent.name == "parameter_space" else "global"


def _pair_key(left: str, right: str) -> str:
    return f"{_slug(left)}__{_slug(right)}"


def _slug(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value.lower()).strip("_")


def _html_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p>No data.</p>"
    columns = list(rows[0])
    head = "".join(f"<th>{_escape(column)}</th>" for column in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{_escape(_display(row.get(column)))}</td>" for column in columns) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _markdown_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_No data._"
    columns = list(rows[0])
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(_display(row.get(column))) for column in columns) + " |")
    return "\n".join(lines)


def _escape(value: Any) -> str:
    import html

    return html.escape(str(value), quote=True)


def _latex(value: Any) -> str:
    return str(value).replace("_", "\\_").replace("%", "\\%")


def _number(value: Any) -> str:
    return "" if value is None else f"{float(value):.4g}"


def _display(value: Any) -> Any:
    if isinstance(value, float):
        return f"{value:.5g}"
    return "" if value is None else value
