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
from .comparison_page import write_comparison_page
from .concrete_compare import (
    build_concrete_comparison_groups,
    write_concrete_comparison_data,
)
from .ingest import (
    clear_trace_cache,
    load_experiments,
    read_execution_manifest,
    read_trace_rows,
)
from .models import AnalysisSpec, DatasetSpec, EvidenceError, EvidenceResult, RunRecord
from .plots import (
    collect_representative_axis_values,
    render_component_figures,
    render_core_figures,
    render_representative_cases,
    representative_case_series,
)
from .sensitivity import analyze_sensitivity, render_sensitivity_figures
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

_PARTIAL_MANIFEST_NAME = ".pisa-analysis-in-progress.yaml"


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
    experiment_outcome_rows = _experiment_outcome_rows(runs, spec)
    experiment_metric_rows = _experiment_metric_rows(runs, spec)
    experiment_performance_rows = _experiment_performance_rows(runs)
    agent_geometry_rows = _agent_geometry_rows(runs)
    collision_event_rows = _collision_event_rows(runs)
    scenario_event_rows = _scenario_event_rows(runs)
    data_quality_rows = [item.as_row() for item in findings]
    _write_rows(summary_dir / "outcomes.csv", outcome_rows)
    _write_rows(summary_dir / "metrics.csv", metric_rows)
    _write_rows(summary_dir / "parameters.csv", parameter_rows)
    _write_rows(summary_dir / "execution_performance.csv", performance_rows)
    _write_rows(summary_dir / "experiment_outcomes.csv", experiment_outcome_rows)
    _write_rows(summary_dir / "experiment_metrics.csv", experiment_metric_rows)
    _write_rows(
        summary_dir / "experiment_execution_performance.csv",
        experiment_performance_rows,
    )
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
    reporter.step("analyzing parameter sensitivity")
    sensitivity_result = analyze_sensitivity(
        runs,
        spec,
        matched_rows=paired.matched_runs,
        delta_rows=paired.metric_deltas,
        progress=reporter.step,
    )
    for name, rows in (
        ("parameter_sensitivity", sensitivity_result.effects),
        ("parameter_importance", sensitivity_result.importance),
        ("parameter_response_profiles", sensitivity_result.profiles),
        ("parameter_interactions", sensitivity_result.interactions),
        ("sensitivity_model_quality", sensitivity_result.model_quality),
        ("parameter_correlations", sensitivity_result.correlations),
        ("sensitivity_sampling_plan", sensitivity_result.sampling_plan),
    ):
        _write_rows(summary_dir / f"{name}.csv", rows)
    reporter.step("writing concrete scenario comparison data")
    concrete_groups, concrete_warnings = build_concrete_comparison_groups(runs, spec)
    warnings.extend(concrete_warnings)
    if not spec.comparison_detail.enabled:
        concrete_groups = []
    comparison_index, comparison_group_ids = write_concrete_comparison_data(
        concrete_groups,
        spec,
        report_dir=report_dir,
        comparison_dir=comparison_dir,
    )
    write_comparison_page(report_dir / "comparison.html")

    reporter.step("rendering core figures")
    experiment_order = [dataset.dataset_id for dataset in datasets]
    if len(experiment_order) == 1:
        figure_paths = render_core_figures(
            runs,
            spec,
            figures_dir,
            x_param=x_param,
            y_param=y_param,
            parameter_pairs=parameter_pairs,
            progress=reporter.step,
        )
    else:
        figure_paths = []
        for experiment_id in experiment_order:
            experiment_runs = [run for run in runs if run.experiment_id == experiment_id]
            figure_paths.extend(
                render_core_figures(
                    experiment_runs,
                    spec,
                    figures_dir / "experiments" / _slug(experiment_id),
                    x_param=x_param,
                    y_param=y_param,
                    parameter_pairs=parameter_pairs,
                    progress=reporter.step,
                )
            )
    reporter.step("rendering representative cases")
    case_paths, case_warnings = render_representative_cases(cases, spec, cases_dir)
    warnings.extend(case_warnings)
    figure_paths.extend(case_paths)
    reporter.step("rendering component comparisons")
    figure_paths.extend(render_component_figures(runs, spec, comparison_dir))
    reporter.step("rendering sensitivity figures")
    figure_paths.extend(
        render_sensitivity_figures(sensitivity_result, figures_dir / "sensitivity")
    )

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
        "experiments": experiment_order,
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
        experiment_outcome_rows=experiment_outcome_rows,
        experiment_metric_rows=experiment_metric_rows,
        experiment_performance_rows=experiment_performance_rows,
        pairing_rows=paired.pairing_summary,
        paired_summary_rows=paired.paired_summary,
        component_rows=component_rows,
        repeat_rows=repeat_rows,
        matched_rows=paired.matched_runs,
        unmatched_rows=paired.unmatched_runs,
        transition_rows=paired.outcome_transition,
        delta_rows=paired.metric_deltas,
        disagreement_rows=paired.failure_disagreement,
        comparison_index=comparison_index,
        comparison_group_ids=comparison_group_ids,
        data_quality_rows=data_quality_rows,
        figure_paths=figure_paths,
        warnings=warnings,
        sensitivity_payload=sensitivity_result.payload(),
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
        experiment_outcome_rows=experiment_outcome_rows,
        experiment_metric_rows=experiment_metric_rows,
        parameter_rows=parameter_rows,
        paired_summary_rows=paired.paired_summary,
        cases=cases,
        warnings=warnings,
    )
    _write_latex_summary(
        report_dir / "paper_ready_summary.tex",
        outcome_rows=outcome_rows,
        metric_rows=metric_rows,
        experiment_outcome_rows=experiment_outcome_rows,
        experiment_metric_rows=experiment_metric_rows,
        paired_summary_rows=paired.paired_summary,
    )
    _write_limitations(report_dir / "limitations.md", input_manifest, warnings)

    reporter.step("writing stage timings")
    (provenance_dir / "stage_timings.json").write_text(
        json.dumps(reporter.timings, indent=2) + "\n", encoding="utf-8"
    )

    manifest_path = output_dir / "manifest.yaml"
    partial_manifest_path = output_dir / _PARTIAL_MANIFEST_NAME
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
            if path.is_file() and path not in {manifest_path, partial_manifest_path}
        ],
    }
    write_yaml(manifest_path, manifest)
    partial_manifest_path.unlink(missing_ok=True)
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
        _write_partial_manifest(output_dir)
        return
    if not output_dir.is_dir():
        raise EvidenceError(f"output path exists and is not a directory: {output_dir}")
    if not overwrite:
        raise EvidenceError(f"analysis output already exists: {output_dir}")
    manifest_path = output_dir / "manifest.yaml"
    partial_manifest_path = output_dir / _PARTIAL_MANIFEST_NAME
    if not any(output_dir.iterdir()):
        _write_partial_manifest(output_dir)
        return
    if manifest_path.exists():
        manifest = _read_output_manifest(manifest_path, label="existing manifest.yaml")
        if manifest.get("tool") != "pisa-analysis-tools":
            raise EvidenceError("existing manifest.yaml is not PISA analysis output")
    elif partial_manifest_path.exists():
        partial = _read_output_manifest(
            partial_manifest_path, label=f"existing {_PARTIAL_MANIFEST_NAME}"
        )
        if partial.get("tool") != "pisa-analysis-tools" or partial.get("state") != "in_progress":
            raise EvidenceError(
                f"existing {_PARTIAL_MANIFEST_NAME} is not valid PISA partial output"
            )
    else:
        raise EvidenceError(
            "analysis output exists but neither manifest.yaml nor a PISA partial-output "
            "marker was found; refusing to overwrite"
        )
    shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    _write_partial_manifest(output_dir)


def _read_output_manifest(path: Path, *, label: str) -> dict[str, Any]:
    try:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise EvidenceError(f"could not read {label}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise EvidenceError(f"{label} must contain a mapping")
    return manifest


def _write_partial_manifest(output_dir: Path) -> None:
    write_yaml(
        output_dir / _PARTIAL_MANIFEST_NAME,
        {
            "tool": "pisa-analysis-tools",
            "schema_version": 1,
            "state": "in_progress",
            "started_at": datetime.now(UTC).isoformat(),
        },
    )


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


def _experiment_outcome_rows(
    runs: list[RunRecord], spec: AnalysisSpec
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for experiment_id, members in _experiment_groups(runs).items():
        counts = grouped_outcomes(members, spec)
        total = len(members)
        success = counts["success"]
        failure = counts["failure"]
        invalid = total - success - failure
        failure_ci_low, failure_ci_high = wilson_interval(failure, total)
        rows.append(
            {
                "experiment_id": experiment_id,
                "run_count": total,
                "valid_count": success + failure,
                "success_count": success,
                "success_rate": success / total if total else None,
                "failure_count": failure,
                "failure_rate": failure / total if total else None,
                "failure_rate_ci_low": failure_ci_low,
                "failure_rate_ci_high": failure_ci_high,
                "invalid_count": invalid,
                "invalid_rate": invalid / total if total else None,
                "execution_error_count": counts["execution_error"],
                "unclassified_count": counts["unclassified"] + counts["unknown"],
                "near_critical_count": sum(
                    safety_region(run, spec) == "near_critical" for run in members
                ),
            }
        )
    return rows


def _experiment_metric_rows(
    runs: list[RunRecord], spec: AnalysisSpec
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for experiment_id, members in _experiment_groups(runs).items():
        for row in _metric_rows(members, spec):
            rows.append({"experiment_id": experiment_id, **row})
    return rows


def _experiment_performance_rows(runs: list[RunRecord]) -> list[dict[str, Any]]:
    return [
        {"experiment_id": experiment_id, **row}
        for experiment_id, members in _experiment_groups(runs).items()
        for row in _performance_rows(members)
    ]


def _experiment_groups(runs: list[RunRecord]) -> dict[str, list[RunRecord]]:
    groups: dict[str, list[RunRecord]] = {}
    for run in runs:
        groups.setdefault(run.experiment_id, []).append(run)
    return groups


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
    case_data: list[dict[str, Any]] = []
    shared_values = collect_representative_axis_values(cases, spec)
    for case in cases:
        path = cases_dir / f"{_slug(case.case_type)}.json"
        series = representative_case_series(case.run, spec, shared_values)
        payload = {
            "case_type": case.case_type,
            "selection_reason": case.reason,
            "run": _run_payload(case.run, spec),
            "series": series,
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
        case_data.append(
            {
                "case_type": case.case_type,
                "selection_reason": case.reason,
                "run": _run_payload(case.run, spec),
                "series": series,
                "events": read_trace_rows(case.run.scenario_events_path),
                "collisions": read_trace_rows(case.run.collision_events_path),
            }
        )
    aggregate = {"schema_version": 1, "cases": case_data}
    aggregate_json = json.dumps(_json_safe(aggregate), ensure_ascii=True).replace(
        "</", "<\\/"
    )
    report_dir = cases_dir.parent
    (report_dir / "case_data.json").write_text(aggregate_json + "\n", encoding="utf-8")
    (report_dir / "case_data.js").write_text(
        f"window.PISA_CASE_DATA={aggregate_json};\n", encoding="utf-8"
    )
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
    experiment_outcome_rows: list[dict[str, Any]],
    experiment_metric_rows: list[dict[str, Any]],
    experiment_performance_rows: list[dict[str, Any]],
    pairing_rows: list[dict[str, Any]],
    paired_summary_rows: list[dict[str, Any]],
    component_rows: list[dict[str, Any]],
    repeat_rows: list[dict[str, Any]],
    matched_rows: list[dict[str, Any]],
    unmatched_rows: list[dict[str, Any]],
    transition_rows: list[dict[str, Any]],
    delta_rows: list[dict[str, Any]],
    disagreement_rows: list[dict[str, Any]],
    comparison_index: list[dict[str, Any]],
    comparison_group_ids: dict[str, str],
    data_quality_rows: list[dict[str, Any]],
    figure_paths: list[Path],
    warnings: list[str],
    sensitivity_payload: dict[str, Any],
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
    experiment_ids = list(input_manifest.get("experiments") or _experiment_groups(runs))
    report_mode = "compare" if len(experiment_ids) > 1 else "single"
    payload = {
        "schema_version": 4,
        "report_mode": report_mode,
        "experiments": _experiment_descriptors(input_manifest, runs),
        "experiment_summaries": {
            "outcomes": experiment_outcome_rows,
            "metrics": experiment_metric_rows,
            "performance": experiment_performance_rows,
        },
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
        "runs": [
            _run_payload(run, spec, comparison_group_ids=comparison_group_ids)
            for run in runs
        ],
        "parameters": _parameter_payload(parameter_rows, spec, numeric_parameters),
        "metrics": metric_rows,
        "parameter_pairs": [
            {"x": left, "y": right, "key": _pair_key(left, right)}
            for left, right in parameter_pairs
        ],
        "figures": _figure_payloads(figure_paths, output_dir, spec),
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
            "concrete_scenarios": comparison_index,
            "parameter_points": _comparison_parameter_points(
                runs,
                matched_rows,
                unmatched_rows,
                delta_rows,
                comparison_group_ids,
            ),
            "parameter_groups": _comparison_parameter_groups(
                runs,
                spec,
                comparison_group_ids,
                experiment_ids,
            ),
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
        "sensitivity": sensitivity_payload,
    }
    payload["insights"] = (
        _comparison_insight_payload(payload)
        if report_mode == "compare"
        else _insight_payload(payload, runs, spec)
    )
    payload["insights"].extend(_sensitivity_insights(sensitivity_payload))
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


def _run_payload(
    run: RunRecord,
    spec: AnalysisSpec,
    comparison_group_ids: dict[str, str] | None = None,
) -> dict[str, Any]:
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
        "comparison_group_id": (comparison_group_ids or {}).get(run.run_id),
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


def _figure_payloads(
    paths: list[Path], output_dir: Path, spec: AnalysisSpec
) -> list[dict[str, Any]]:
    formats_by_artifact: dict[tuple[str, str], set[str]] = defaultdict(set)
    for path in paths:
        rel = path.relative_to(output_dir)
        formats_by_artifact[(str(rel.parent), path.stem)].add(
            path.suffix.removeprefix(".")
        )
    figures = []
    for path in paths:
        rel = path.relative_to(output_dir)
        parts = rel.parts
        scope = "global"
        experiment_id = None
        comparison_id = None
        if len(parts) >= 3 and parts[0] == "figures" and parts[1] == "experiments":
            scope = "experiment"
            experiment_id = parts[2]
        elif parts and parts[0] == "comparison":
            scope = "comparison"
            comparison_id = parts[1] if len(parts) > 2 else "components"
        pair = _figure_pair(path)
        category = _figure_category(rel, path.stem)
        metric = next(
            (name for name in spec.metrics if path.stem.startswith(name)), None
        )
        tags = [category, path.stem]
        if pair != "global":
            tags.append(pair)
        if metric:
            tags.append(metric)
        figures.append(
            {
                "path": str(rel),
                "name": path.stem,
                "title": path.stem.replace("_", " ").title(),
                "format": path.suffix.removeprefix("."),
                "pair": pair,
                "scope": scope,
                "experiment_id": experiment_id,
                "comparison_id": comparison_id,
                "figure_key": f"{pair}:{path.stem}",
                "category": category,
                "tags": sorted(set(tags)),
                "parameter_pair": None if pair == "global" else pair,
                "metric": metric,
                "available_formats": sorted(
                    formats_by_artifact[(str(rel.parent), path.stem)]
                ),
            }
        )
    return figures


def _figure_category(path: Path, stem: str) -> str:
    if "sensitivity" in path.parts:
        return "Parameter Sensitivity"
    if path.parts and path.parts[0] == "representative_cases":
        return "Representative Case"
    if path.parts and path.parts[0] == "comparison":
        return "Component Comparison"
    if stem.startswith("outcome_"):
        return "Outcome"
    if "failure" in stem or "safety" in stem:
        return "Safety"
    if "heatmap" in stem:
        return "Metric Heatmap"
    if any(token in stem for token in ("histogram", "cdf", "by_outcome")):
        return "Metric Distribution"
    return "Parameter Space"


def _experiment_descriptors(
    input_manifest: dict[str, Any], runs: list[RunRecord]
) -> list[dict[str, Any]]:
    run_groups = _experiment_groups(runs)
    datasets = input_manifest.get("datasets") or []
    descriptors = []
    for index, dataset in enumerate(datasets):
        experiment_id = str(dataset.get("dataset_id"))
        metadata = dict(dataset.get("metadata") or {})
        descriptors.append(
            {
                "id": experiment_id,
                "label": metadata.get("label") or experiment_id,
                "order": index,
                "run_count": len(run_groups.get(experiment_id, [])),
                "figure_key": _slug(experiment_id),
                "av": metadata.get("av_name"),
                "simulator": metadata.get("simulator_name"),
                "sampler": metadata.get("sampler_name"),
                "metadata": metadata,
            }
        )
    return descriptors


def _comparison_parameter_points(
    runs: list[RunRecord],
    matched_rows: list[dict[str, Any]],
    unmatched_rows: list[dict[str, Any]],
    delta_rows: list[dict[str, Any]],
    comparison_group_ids: dict[str, str],
) -> list[dict[str, Any]]:
    run_by_id = {run.run_id: run for run in runs}
    deltas: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in delta_rows:
        deltas[(str(row["comparison"]), str(row["match_key"]))][str(row["metric"])] = {
            "left": row.get("left"),
            "right": row.get("right"),
            "delta": row.get("delta_right_minus_left"),
        }
    points: list[dict[str, Any]] = []
    for row in matched_rows:
        left_id = str(row["left_run_id"])
        right_id = str(row["right_run_id"])
        left_run = run_by_id.get(left_id)
        right_run = run_by_id.get(right_id)
        left_outcome = str(row.get("left_outcome") or "unknown")
        right_outcome = str(row.get("right_outcome") or "unknown")
        key = (str(row["comparison"]), str(row["match_key"]))
        points.append(
            {
                "comparison": row["comparison"],
                "match_key": row["match_key"],
                "pairing_method": row.get("pairing_method"),
                "matched": True,
                "left_experiment": row.get("left_experiment")
                or (left_run.experiment_id if left_run else None),
                "right_experiment": row.get("right_experiment")
                or (right_run.experiment_id if right_run else None),
                "left_run_id": left_id,
                "right_run_id": right_id,
                "left_outcome": left_outcome,
                "right_outcome": right_outcome,
                "left_outcome_family": _outcome_family(left_outcome),
                "right_outcome_family": _outcome_family(right_outcome),
                "transition": f"{_outcome_family(left_outcome)}__{_outcome_family(right_outcome)}",
                "parameters": _json_mapping(row.get("parameters")),
                "metric_deltas": deltas.get(key, {}),
                "comparison_group_id": comparison_group_ids.get(left_id)
                or comparison_group_ids.get(right_id),
            }
        )
    for row in unmatched_rows:
        run_id = str(row["run_id"])
        run = run_by_id.get(run_id)
        points.append(
            {
                "comparison": row["comparison"],
                "matched": False,
                "side": row.get("side"),
                "experiment_id": row.get("experiment_id")
                or (run.experiment_id if run else None),
                "run_id": run_id,
                "outcome": row.get("outcome"),
                "parameters": _json_mapping(row.get("parameters")),
                "comparison_group_id": comparison_group_ids.get(run_id),
            }
        )
    return points


def _comparison_parameter_groups(
    runs: list[RunRecord],
    spec: AnalysisSpec,
    comparison_group_ids: dict[str, str],
    experiment_ids: list[str],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[RunRecord]] = defaultdict(list)
    for run in runs:
        group_id = comparison_group_ids.get(run.run_id)
        if group_id:
            grouped[group_id].append(run)
    order = {experiment_id: index for index, experiment_id in enumerate(experiment_ids)}
    result = []
    for group_id, members in sorted(
        grouped.items(), key=lambda item: json.dumps(item[1][0].params, sort_keys=True)
    ):
        members.sort(key=lambda run: order.get(run.experiment_id, len(order)))
        present = {run.experiment_id for run in members}
        result.append(
            {
                "group_id": group_id,
                "parameters": dict(members[0].params),
                "complete": present == set(experiment_ids),
                "missing_experiments": [
                    experiment_id
                    for experiment_id in experiment_ids
                    if experiment_id not in present
                ],
                "comparison_url": f"comparison.html?group={group_id}",
                "experiments": [
                    {
                        "experiment_id": run.experiment_id,
                        "run_id": run.run_id,
                        "sample_id": run.sample_id,
                        "outcome": normalized_outcome(run, spec),
                        "outcome_family": _outcome_family(normalized_outcome(run, spec)),
                        "safety_region": safety_region(run, spec),
                        "status": run.status,
                        "termination_reason": run.termination_reason,
                        "metrics": {
                            name: metric_value(run, spec, name) for name in spec.metrics
                        },
                        "metadata": run.metadata,
                    }
                    for run in members
                ],
            }
        )
    return result


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _outcome_family(outcome: str) -> str:
    return outcome if outcome in {"success", "failure"} else "invalid"


def _boundary_payload(
    runs: list[RunRecord],
    spec: AnalysisSpec,
    parameter_pairs: list[tuple[str, str]],
) -> dict[str, Any]:
    groups = _experiment_groups(runs)
    return {
        "grid_size": 60,
        "nearest_neighbors": 24,
        "pairs": (
            {
                _pair_key(left, right): _boundary_for_pair(runs, spec, left, right)
                for left, right in parameter_pairs
            }
            if len(groups) == 1
            else {}
        ),
        "by_experiment": {
            experiment_id: {
                _pair_key(left, right): _boundary_for_pair(members, spec, left, right)
                for left, right in parameter_pairs
            }
            for experiment_id, members in groups.items()
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


def _comparison_insight_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    insights: list[dict[str, Any]] = []
    outcome_rows = payload["experiment_summaries"]["outcomes"]
    if len(outcome_rows) >= 2:
        rates = sorted(
            outcome_rows,
            key=lambda row: float(row.get("failure_rate") or 0.0),
        )
        low, high = rates[0], rates[-1]
        difference = float(high.get("failure_rate") or 0.0) - float(
            low.get("failure_rate") or 0.0
        )
        if difference > 0:
            insights.append(
                _insight(
                    "experiment-failure-rate-gap",
                    "high" if difference >= 0.1 else "medium",
                    "Failure rate differs across experiments",
                    f"{high['experiment_id']} has a {difference:.1%} higher failure rate than "
                    f"{low['experiment_id']}.",
                    {
                        "lower_experiment": low["experiment_id"],
                        "higher_experiment": high["experiment_id"],
                        "absolute_difference": difference,
                    },
                )
            )
    points = [
        point
        for point in payload["comparison"]["parameter_points"]
        if point.get("matched")
    ]
    transitions = Counter(point["transition"] for point in points)
    disagreements = [
        point
        for point in points
        if point["left_outcome_family"] != point["right_outcome_family"]
    ]
    if disagreements:
        first = next(
            (point for point in disagreements if point.get("comparison_group_id")),
            disagreements[0],
        )
        actions = []
        if first.get("comparison_group_id"):
            actions.append(
                {
                    "type": "open_comparison",
                    "label": "Open concrete comparison",
                    "group_id": first["comparison_group_id"],
                }
            )
        insights.append(
            _insight(
                "outcome-disagreement",
                "high",
                "Matched scenarios produce different outcomes",
                f"{len(disagreements)} of {len(points)} matched parameter points disagree.",
                {
                    "matched": len(points),
                    "disagreements": len(disagreements),
                    "transitions": dict(sorted(transitions.items())),
                },
                actions,
            )
        )
    unmatched = [
        point
        for point in payload["comparison"]["parameter_points"]
        if not point.get("matched")
    ]
    if unmatched:
        insights.append(
            _insight(
                "unmatched-parameter-points",
                "medium",
                "Some parameter points cannot be compared",
                f"{len(unmatched)} runs have no paired run in at least one comparison.",
                {"unmatched": len(unmatched)},
            )
        )
    return insights


def _sensitivity_insights(payload: dict[str, Any]) -> list[dict[str, Any]]:
    quality = {
        (row.get("experiment_id"), row.get("target")): row
        for row in payload.get("model_quality", [])
    }
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in payload.get("importance", []):
        if row.get("importance_type", "parameter") != "parameter":
            continue
        grouped[(str(row.get("experiment_id")), str(row.get("target")))].append(row)
    insights = []
    for key, rows in grouped.items():
        model = quality.get(key, {})
        if model.get("reliability") not in {"high", "medium"}:
            continue
        top = max(rows, key=lambda row: float(row.get("importance_mean") or 0.0))
        if float(top.get("importance_mean") or 0.0) <= 0:
            continue
        insights.append(
            _insight(
                f"sensitivity-{_slug(key[0])}-{_slug(key[1])}",
                "medium",
                f"{top['parameter']} is the strongest modeled driver of {key[1]}",
                f"Held-out permutation importance ranks {top['parameter']} first for "
                f"{key[0]} with {model.get('reliability')} model reliability.",
                {
                    "experiment_id": key[0],
                    "target": key[1],
                    "parameter": top["parameter"],
                    "importance": top.get("importance_mean"),
                    "importance_ci": [
                        top.get("importance_ci_low"),
                        top.get("importance_ci_high"),
                    ],
                    "model_quality": model,
                },
            )
        )
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
    .button-link { display:inline-block; padding:8px 11px; border-radius:6px; background:var(--navy); color:white; text-decoration:none; }
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
    .segmented { display:inline-flex; border:1px solid #aab7c4; border-radius:6px; overflow:hidden; }
    .segmented button { min-height:32px; border:0; border-radius:0; background:white; color:var(--ink); }
    .segmented button.active { background:var(--navy); color:white; }
    canvas.case-canvas { min-height:380px; margin-top:12px; background:white; }
    .axis-info { margin-top:8px; font-size:12px; color:var(--muted); }
    .figure-browser { display:grid; grid-template-columns:260px minmax(0,1fr); gap:14px; align-items:start; }
    .figure-controls { display:grid; gap:10px; position:sticky; top:72px; }
    .figure-viewer { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }
    .figure-viewer.single { grid-template-columns:minmax(0,1fr); }
    .figure-empty { min-height:260px; display:grid; place-items:center; border:1px dashed #94a3b8; color:var(--muted); }
    .sensitivity-layout { display:grid; grid-template-columns:minmax(0,1fr) 380px; gap:14px; align-items:start; }
    #sensitivity-canvas { min-height:430px; height:430px; background:white; }
    .compare-only[hidden] { display:none; }
    .legend { display:flex; flex-wrap:wrap; gap:8px 14px; padding:10px 0; font-size:12px; }
    .legend-group { display:flex; flex-wrap:wrap; gap:6px 10px; align-items:center; }
    .legend-item { display:inline-flex; align-items:center; gap:5px; }
    .swatch { width:12px; height:12px; border-radius:50%; border:2px solid #475569; display:inline-block; }
    .experiment-actions { display:inline-flex; gap:6px; }
    .experiment-actions button { min-height:28px; padding:3px 8px; font-size:12px; }
    .table-link { color:#0f5f99; font-weight:700; }
    @media (max-width: 1000px) { .shell { grid-template-columns:1fr; } nav { position:relative; height:auto; } .layout,.figure-browser,.figure-viewer,.sensitivity-layout { grid-template-columns:1fr; } .figure-controls { position:relative; top:auto; } }
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
  <a href="#sensitivity">Parameter Sensitivity</a>
  <a href="#boundary">Boundary Explorer</a>
  <a href="#insights">Insights</a>
  <a href="#cases">Representative Cases</a>
  <a href="#comparison">Comparison</a>
  <a href="#figures">Evidence Figures</a>
  <a href="#quality">Data Quality</a>
  <a href="#advanced">Spec Lab</a>
</nav>
<main>
  __STATIC_NOTE__
  <div class="topbar">
    <label>Explorer mode<select id="explorer-mode"><option value="explore">Explore</option><option value="outcome_compare">Compare outcomes</option><option value="metric_delta">Compare metric delta</option></select></label>
    <label id="reference-control">Reference experiment<select id="reference-experiment"></select></label>
    <label class="compare-only" id="left-control">Left experiment<select id="left-experiment"></select></label>
    <label class="compare-only" id="right-control">Right experiment<select id="right-experiment"></select></label>
    <label class="compare-only" id="delta-control">Delta metric<select id="delta-metric"></select></label>
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
    <div class="filters"><strong>Experiments</strong><span id="experiment-filters"></span><span class="experiment-actions"><button id="experiments-all" type="button">Select all</button><button id="experiments-clear" type="button">Clear</button></span></div>
    <div class="filters"><strong>Outcome filters</strong><span id="outcome-filters"></span></div>
    <div class="filters"><strong>Safety filters</strong><span id="safety-filters"></span></div>
    <div class="filters"><strong>Status filters</strong><span id="status-filters"></span></div>
    <div id="space-legend" class="legend" aria-label="Chart legend"></div>
    <div class="layout">
      <canvas id="space-canvas"></canvas>
      <aside class="panel">
        <h3>Selected Set</h3>
        <div id="selection-summary" class="cards"></div>
        <button id="download-filtered" type="button">Download Filtered CSV</button>
        <h3>Run Detail</h3>
        <label>Run<select id="run-select"></select></label>
        <a id="compare-run" class="button-link" href="comparison.html" hidden>Analyze concrete run</a>
        <input id="search" placeholder="Filter by run id, parameter, component, outcome, or reason">
        <div id="group-inspector" hidden></div>
        <pre id="detail">Click a point or choose a run.</pre>
      </aside>
    </div>
  </section>
  <section id="sensitivity">
    <h2>Parameter Sensitivity</h2>
    <p class="muted">Observed associations and held-out surrogate-model sensitivity; these are not causal effects or formal Sobol indices.</p>
    <div class="inline">
      <label>Experiment<select id="sensitivity-experiment"></select></label>
      <label>Target<select id="sensitivity-target"></select></label>
      <label>View<select id="sensitivity-view"><option value="importance">Global importance</option><option value="response">Response profile</option><option value="interaction">Interactions</option></select></label>
      <label>Parameter<select id="sensitivity-parameter"></select></label>
    </div>
    <div id="sensitivity-cards" class="cards"></div>
    <div class="sensitivity-layout">
      <canvas id="sensitivity-canvas"></canvas>
      <aside class="panel"><h3>Empirical effects</h3><div id="sensitivity-effect-table"></div><h3>Model quality</h3><div id="sensitivity-quality-table"></div></aside>
    </div>
    <h3>Interactions</h3><div id="sensitivity-interaction-table"></div>
    <h3>Correlated parameter groups</h3><div id="sensitivity-cluster-table"></div>
    <h3>Cross-experiment ranking</h3><div id="sensitivity-compare-table"></div>
    <h3>Parameter correlations</h3><div id="sensitivity-correlation-table"></div>
    <h3>Formal design budget</h3><div id="sensitivity-sampling-table"></div>
    <div id="sensitivity-warnings" class="notice"></div>
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
    <div class="inline">
      <label>Case<select id="case-select"></select></label>
      <label>Series<select id="case-series-select"></select></label>
      <label>Scale<div class="segmented"><button id="case-semantic" class="active" type="button">Semantic</button><button id="case-detail" type="button">Detail</button></div></label>
    </div>
    <canvas id="case-canvas" class="case-canvas"></canvas>
    <div id="case-axis-info" class="axis-info"></div>
    <div id="case-table"></div>
  </section>
  <section id="comparison">
    <h2>Concrete Scenario Analysis</h2>
    <p><a class="button-link" href="comparison.html">Open concrete scenario viewer</a></p>
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
  <section id="figures">
    <h2>Evidence Figures</h2>
    <div class="figure-browser">
      <aside class="panel figure-controls">
        <label>Category<select id="figure-category"></select></label>
        <label>Figure<select id="figure-type"></select></label>
        <label>Parameter pair<select id="figure-pair"></select></label>
        <label>Metric<select id="figure-metric"></select></label>
        <label>Tags / search<input id="figure-search" placeholder="heatmap, outcome, TTC..."></label>
        <div id="figure-compare-controls">
          <label>Left experiment<select id="figure-left"></select></label>
          <label>Right experiment<select id="figure-right"></select></label>
        </div>
      </aside>
      <div id="figure-list" class="figure-viewer"></div>
    </div>
  </section>
</main>
</div>
<script src="analysis_data.js"></script>
<script src="case_data.js"></script>
<script>
(() => {
  const payload = window.PISA_ANALYSIS_DATA || {runs: [], parameters: [], metrics: [], parameter_pairs: [], boundary: {pairs: {}}};
  const casePayload = window.PISA_CASE_DATA || {cases: []};
  const runs = payload.runs || [];
  const experiments = payload.experiments || [];
  const parameterGroups = payload.comparison?.parameter_groups || [];
  const concreteScenarios = payload.comparison?.concrete_scenarios || [];
  const numericParams = payload.parameters.filter(item => item.numeric).map(item => item.parameter);
  const metricNames = payload.metrics.map(item => item.metric);
  const state = { projected: [], selectedIds: new Set(), draftRuns: null, comparePoints: [] };
  const els = {
    pair: document.getElementById('pair-select'), x: document.getElementById('x-select'), y: document.getElementById('y-select'), z: document.getElementById('z-select'),
    color: document.getElementById('color-select'), view: document.getElementById('view-select'), overlay: document.getElementById('overlay-select'),
    canvas: document.getElementById('space-canvas'), detail: document.getElementById('detail'), runSelect: document.getElementById('run-select'), search: document.getElementById('search'),
    mode: document.getElementById('explorer-mode'), reference: document.getElementById('reference-experiment'), left: document.getElementById('left-experiment'), right: document.getElementById('right-experiment'), deltaMetric: document.getElementById('delta-metric'),
    experimentFilters: document.getElementById('experiment-filters'), legend: document.getElementById('space-legend'),
    outcomeFilters: document.getElementById('outcome-filters'), safetyFilters: document.getElementById('safety-filters'), statusFilters: document.getElementById('status-filters')
  };
  const ctx = els.canvas.getContext('2d');
  const figureEls = {category:document.getElementById('figure-category'),type:document.getElementById('figure-type'),pair:document.getElementById('figure-pair'),metric:document.getElementById('figure-metric'),search:document.getElementById('figure-search'),left:document.getElementById('figure-left'),right:document.getElementById('figure-right'),viewer:document.getElementById('figure-list')};
  const sensitivityEls = {experiment:document.getElementById('sensitivity-experiment'),target:document.getElementById('sensitivity-target'),view:document.getElementById('sensitivity-view'),parameter:document.getElementById('sensitivity-parameter'),canvas:document.getElementById('sensitivity-canvas')};
  const sensitivityCtx=sensitivityEls.canvas.getContext('2d');
  const caseEls = {
    caseSelect: document.getElementById('case-select'), seriesSelect: document.getElementById('case-series-select'),
    semantic: document.getElementById('case-semantic'), detail: document.getElementById('case-detail'),
    canvas: document.getElementById('case-canvas'), info: document.getElementById('case-axis-info')
  };
  const caseCtx = caseEls.canvas.getContext('2d');
  const caseState = {mode:'semantic', hoverIndex:null};
  const semanticColors = {success:'#16a34a', failure:'#dc2626', invalid:'#2563eb', execution_error:'#7f1d1d', unclassified:'#6b7280', safe:'#16a34a', near_critical:'#f59e0b', unknown:'#6b7280', all_success:'#16a34a', all_failure:'#991b1b', all_invalid:'#6b7280', disagreement:'#f59e0b', mixed_with_invalid:'#7c3aed'};
  const palette = ['#7c3aed','#f59e0b','#0891b2','#be123c','#4b5563','#84cc16','#c026d3','#0f766e'];
  const experimentColors = new Map(experiments.map((item,index) => [item.id, palette[index % palette.length]]));
  const transitionColors = {success__success:'#16a34a',failure__failure:'#991b1b',success__failure:'#ef4444',failure__success:'#0284c7',success__invalid:'#a78bfa',failure__invalid:'#7c3aed',invalid__success:'#14b8a6',invalid__failure:'#f97316',invalid__invalid:'#6b7280',unmatched:'#cbd5e1'};
  const transitionLabels = {success__success:'Both success',failure__failure:'Both failure',success__failure:'Left success / Right failure',failure__success:'Left failure / Right success',success__invalid:'Left success / Right invalid',failure__invalid:'Left failure / Right invalid',invalid__success:'Left invalid / Right success',invalid__failure:'Left invalid / Right failure',invalid__invalid:'Both invalid',unmatched:'Unmatched'};
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
    if (run.group_id) return groupFieldValue(run,key);
    if (key === 'normalized_outcome') return run.draft_outcome || run.normalized_outcome || 'unknown';
    if (key === 'safety_region') return draftSafety(run);
    if (key === 'termination_reason') return run.termination_reason || 'unknown';
    if (key === 'status') return run.status || 'unknown';
    if (key.startsWith('param:')) return run.params[key.slice(6)];
    if (key.startsWith('metric:')) return run.metrics[key.slice(7)];
    if (key.startsWith('metadata:')) return run.metadata[key.slice(9)];
    return run[key];
  }
  function groupExperiments(group) { const selected=selectedExperiments(); return group.experiments.filter(item=>selected.has(item.experiment_id)); }
  function consensus(values) {
    const present=values.filter(value=>value!==null&&value!==undefined&&value!=='');
    if (!present.length) return 'unknown';
    const uniqueValues=[...new Set(present)];
    return uniqueValues.length===1?uniqueValues[0]:'disagreement';
  }
  function outcomeConsensus(group) {
    const families=groupExperiments(group).map(item=>item.outcome_family);
    if (!families.length) return 'unknown';
    const values=new Set(families);
    if (values.size===1) return `all_${families[0]}`;
    return values.has('invalid')?'mixed_with_invalid':'disagreement';
  }
  function groupFieldValue(group,key) {
    if (key==='normalized_outcome') return outcomeConsensus(group);
    if (key==='safety_region') return consensus(groupExperiments(group).map(item=>item.safety_region));
    if (key==='status') return consensus(groupExperiments(group).map(item=>item.status));
    if (key==='termination_reason') return consensus(groupExperiments(group).map(item=>item.termination_reason));
    if (key.startsWith('param:')) return group.parameters[key.slice(6)];
    if (key.startsWith('metric:')) return group.experiments.find(item=>item.experiment_id===els.reference.value)?.metrics?.[key.slice(7)];
    return group[key];
  }
  function numericField(run, key) { return number(fieldValue(run, key)); }
  function paramValue(run, param) { return number((run.parameters || run.params || {})[param]); }
  function selectedExperiments() { return checked(els.experimentFilters); }
  function filteredRuns() {
    const selected = selectedExperiments(), outcome = checked(els.outcomeFilters), safety = checked(els.safetyFilters), status = checked(els.statusFilters);
    return activeRuns().filter(run => selected.has(run.experiment_id) && outcome.has(run.draft_outcome || run.normalized_outcome || 'unknown') && safety.has(draftSafety(run)) && status.has(run.status || 'unknown'));
  }
  function filteredParameterGroups() {
    const selected=selectedExperiments(),outcome=checked(els.outcomeFilters),safety=checked(els.safetyFilters),status=checked(els.statusFilters);
    return parameterGroups.filter(group=>group.experiments.some(item=>selected.has(item.experiment_id)&&outcome.has(item.outcome)&&safety.has(item.safety_region||'unknown')&&status.has(item.status||'unknown')));
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
    const rows = payload.report_mode==='compare'&&els.mode.value==='explore'?filteredParameterGroups():filteredRuns();
    const xParam = els.x.value, yParam = els.y.value, zParam = els.z.value;
    clearCanvas(); state.projected = [];
    if (!xParam || !yParam) return;
    if (els.mode.value !== 'explore') {
      drawComparison(xParam, yParam);
      return;
    }
    const colors = colorState(rows);
    renderLegend(colors);
    if (!rows.length) return;
    if (els.view.value === 'heatmap' && payload.report_mode!=='compare') drawHeatmap(rows, xParam, yParam);
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
      drawMarker(sx, sy, 5.2 * devicePixelRatio, colorFor(colors, point.index), point.run.complete===false?'#f8fafc':'#102033', 0, point.run.complete===false);
      state.projected.push(point.run.group_id?{x:sx,y:sy,parameterGroup:point.run}:{x:sx,y:sy,run:point.run});
    });
  }
  function drawMarker(x,y,r,fill,stroke,shape=0,hollow=false) {
    ctx.beginPath(); ctx.arc(x,y,r,0,Math.PI*2);
    ctx.fillStyle = hollow ? '#101820' : fill; ctx.globalAlpha = hollow ? 1 : 0.85; ctx.fill(); ctx.globalAlpha = 1;
    ctx.strokeStyle = stroke; ctx.lineWidth = 2*devicePixelRatio; ctx.stroke();
  }
  function renderLegend(colors) {
    const selected = [...selectedExperiments()];
    const experimentItems = selected.map(id => `<span class="legend-item"><i class="swatch" style="background:transparent;border-color:${experimentColors.get(id) || '#64748b'}"></i>${escapeHtml(id)}</span>`).join('');
    let valueItems = '';
    if (colors.mode === 'continuous') {
      valueItems = `<span>${escapeHtml(els.color.value)}: ${fmt(colors.min)} to ${fmt(colors.max)}</span>`;
    } else {
      valueItems = [...colors.map.entries()].map(([label,color]) => `<span class="legend-item"><i class="swatch" style="background:${color};border-color:${color}"></i>${escapeHtml(label)}</span>`).join('');
    }
    const context=payload.report_mode==='compare'?`<span class="legend-group"><strong>Reference</strong><span>${escapeHtml(els.reference.value)}</span></span>`:`<span class="legend-group"><strong>Experiment</strong>${experimentItems}</span>`;
    els.legend.innerHTML = `${context}<span class="legend-group"><strong>Value</strong>${valueItems}</span>`;
  }
  function orientedComparisonPoints() {
    const left = els.left.value, right = els.right.value;
    return (payload.comparison?.parameter_points || []).flatMap(point => {
      if (point.matched) {
        if (point.left_experiment === left && point.right_experiment === right) return [point];
        if (point.left_experiment === right && point.right_experiment === left) return [{...point,left_experiment:left,right_experiment:right,left_run_id:point.right_run_id,right_run_id:point.left_run_id,left_outcome:point.right_outcome,right_outcome:point.left_outcome,left_outcome_family:point.right_outcome_family,right_outcome_family:point.left_outcome_family,transition:`${point.right_outcome_family}__${point.left_outcome_family}`,metric_deltas:Object.fromEntries(Object.entries(point.metric_deltas || {}).map(([name,item]) => [name,{left:item.right,right:item.left,delta:number(item.delta) === null ? null : -Number(item.delta)}]))}];
        return [];
      }
      const pairMatches = point.comparison === `${left}__vs__${right}` || point.comparison === `${right}__vs__${left}`;
      return pairMatches ? [point] : [];
    });
  }
  function drawComparison(xParam,yParam) {
    const points = orientedComparisonPoints().map(point => ({point,x:number(point.parameters?.[xParam]),y:number(point.parameters?.[yParam])})).filter(item => item.x !== null && item.y !== null);
    state.comparePoints = points.map(item => item.point);
    if (!points.length) {
      els.legend.innerHTML = '<span class="muted">No matched or unmatched points for this experiment pair.</span>';
      document.getElementById('selection-summary').innerHTML = cards([{label:'Compared points',value:0}]);
      return;
    }
    const w=els.canvas.width,h=els.canvas.height,margin=70*devicePixelRatio;
    const [xMin,xMax]=range(points.map(item=>item.x)),[yMin,yMax]=range(points.map(item=>item.y));
    axes(xParam,yParam,w,h,margin);
    const used = new Set();
    let maxAbs=0;
    if (els.mode.value === 'metric_delta') points.forEach(({point}) => { const value=number(point.metric_deltas?.[els.deltaMetric.value]?.delta); if (value !== null) maxAbs=Math.max(maxAbs,Math.abs(value)); });
    points.forEach(({point,x,y}) => {
      const sx=margin+(x-xMin)/(xMax-xMin)*(w-2*margin),sy=h-margin-(y-yMin)/(yMax-yMin)*(h-2*margin);
      let color='#94a3b8',label='unmatched';
      if (point.matched && els.mode.value === 'outcome_compare') { label=point.transition; color=transitionColors[label] || '#94a3b8'; }
      if (point.matched && els.mode.value === 'metric_delta') { const delta=number(point.metric_deltas?.[els.deltaMetric.value]?.delta); label=delta === null ? 'missing' : 'delta'; color=deltaColor(delta,maxAbs); }
      used.add(label); drawMarker(sx,sy,5.5*devicePixelRatio,color,color,0,!point.matched || label === 'missing');
      state.projected.push({x:sx,y:sy,comparePoint:point});
    });
    if (els.mode.value === 'outcome_compare') els.legend.innerHTML = `<span class="legend-group"><strong>Outcome transition</strong>${[...used].map(key => `<span class="legend-item"><i class="swatch" style="background:${transitionColors[key] || '#94a3b8'};border-color:${transitionColors[key] || '#94a3b8'}"></i>${escapeHtml(transitionLabels[key] || key)}</span>`).join('')}</span>`;
    else els.legend.innerHTML = `<span class="legend-group"><strong>${escapeHtml(els.deltaMetric.value)} (Right - Left)</strong><span class="legend-item"><i class="swatch" style="background:#2563eb;border-color:#2563eb"></i>Left higher</span><span class="legend-item"><i class="swatch" style="background:#f8fafc;border-color:#94a3b8"></i>Near zero</span><span class="legend-item"><i class="swatch" style="background:#dc2626;border-color:#dc2626"></i>Right higher</span><span>${fmt(-maxAbs)} to ${fmt(maxAbs)}</span></span>`;
    const matched=points.filter(item=>item.point.matched).length, disagreements=points.filter(item=>item.point.matched && item.point.left_outcome_family !== item.point.right_outcome_family).length;
    document.getElementById('selection-summary').innerHTML=cards([{label:'Matched',value:matched},{label:'Disagreements',value:disagreements},{label:'Unmatched',value:points.length-matched},{label:'Pair',value:`${els.left.value} / ${els.right.value}`}]);
  }
  function deltaColor(value,maxAbs) {
    if (value === null || !maxAbs) return '#f8fafc';
    const t=Math.min(1,Math.abs(value)/maxAbs),base=value<0?[37,99,235]:[220,38,38];
    return `rgb(${Math.round(248+(base[0]-248)*t)},${Math.round(250+(base[1]-250)*t)},${Math.round(252+(base[2]-252)*t)})`;
  }
  function escapeHtml(value) { return text(value).replace(/[&<>\"]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[ch])); }
  function drawHeatmap(rows, xParam, yParam) {
    const groups = [...new Set(rows.map(run => run.experiment_id))];
    if (groups.length > 1) { drawHeatmapFacets(rows,xParam,yParam,groups); return; }
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
  function drawHeatmapFacets(rows,xParam,yParam,groups) {
    const points=rows.map(run=>({run,x:paramValue(run,xParam),y:paramValue(run,yParam),fail:(run.draft_outcome || run.normalized_outcome)==='failure'})).filter(p=>p.x!==null&&p.y!==null);
    if (!points.length) return;
    const w=els.canvas.width,h=els.canvas.height,cols=Math.min(2,groups.length),panelRows=Math.ceil(groups.length/cols),gap=28*devicePixelRatio,pad=42*devicePixelRatio,bins=14;
    const [xMin,xMax]=range(points.map(p=>p.x)),[yMin,yMax]=range(points.map(p=>p.y));
    groups.forEach((experiment,index)=>{
      const col=index%cols,row=Math.floor(index/cols),pw=(w-gap*(cols+1))/cols,ph=(h-gap*(panelRows+1))/panelRows,left=gap+col*(pw+gap),top=gap+row*(ph+gap),innerW=pw-pad*1.4,innerH=ph-pad;
      const cells=Array.from({length:bins*bins},()=>({n:0,f:0}));
      points.filter(p=>p.run.experiment_id===experiment).forEach(p=>{const ix=Math.min(bins-1,Math.max(0,Math.floor((p.x-xMin)/(xMax-xMin)*bins))),iy=Math.min(bins-1,Math.max(0,Math.floor((p.y-yMin)/(yMax-yMin)*bins)));cells[iy*bins+ix].n++;if(p.fail)cells[iy*bins+ix].f++;});
      cells.forEach((cell,cellIndex)=>{const ix=cellIndex%bins,iy=Math.floor(cellIndex/bins),rate=cell.n?cell.f/cell.n:0;ctx.fillStyle=cell.n?`rgba(220,38,38,${0.15+rate*0.75})`:'rgba(148,163,184,0.08)';ctx.fillRect(left+pad+ix*innerW/bins,top+ph-pad-(iy+1)*innerH/bins,innerW/bins+1,innerH/bins+1);});
      ctx.strokeStyle='#d7e0ea';ctx.strokeRect(left+pad,top,innerW,innerH);ctx.fillStyle='#edf3f8';ctx.font=`${12*devicePixelRatio}px system-ui`;ctx.textAlign='left';ctx.fillText(experiment,left+pad,top+ph-12*devicePixelRatio);
    });
    els.legend.innerHTML += '<span class="legend-group"><strong>Heatmap</strong><span>Separate panels, shared axes and failure-rate scale</span></span>';
  }
  function draw3d(rows, xParam, yParam, zParam, colors) {
    const points = rows.map((run, index) => ({run, index, x:paramValue(run,xParam), y:paramValue(run,yParam), z:paramValue(run,zParam)})).filter(p => p.x !== null && p.y !== null && p.z !== null);
    if (!points.length) return;
    const w = els.canvas.width, h = els.canvas.height;
    const ranges = [range(points.map(p => p.x)), range(points.map(p => p.y)), range(points.map(p => p.z))];
    function norm(v, i) { return (v-ranges[i][0])/(ranges[i][1]-ranges[i][0])*2-1; }
    function project(x,y,z) { const cy=Math.cos(yaw), sy=Math.sin(yaw), cp=Math.cos(pitch), sp=Math.sin(pitch); let x1=cy*x+sy*z, z1=-sy*x+cy*z; let y1=cp*y-sp*z1, z2=sp*y+cp*z1; const s=Math.min(w,h)*0.34*zoom/(1.7+z2); return {x:w/2+x1*s, y:h/2-y1*s, z:z2}; }
    ctx.fillStyle = '#edf3f8'; ctx.font = `${13 * devicePixelRatio}px system-ui`; ctx.fillText(`${xParam} / ${yParam} / ${zParam}`, 20*devicePixelRatio, 28*devicePixelRatio);
    points.map(p => ({...p, p:project(norm(p.x,0), norm(p.y,1), norm(p.z,2))})).sort((a,b) => a.p.z-b.p.z).forEach(point => { ctx.beginPath(); ctx.arc(point.p.x, point.p.y, 4.2*devicePixelRatio, 0, Math.PI*2); ctx.fillStyle = colorFor(colors, point.index); ctx.globalAlpha=0.8; ctx.fill(); ctx.globalAlpha=1; state.projected.push(point.run.group_id?{x:point.p.x,y:point.p.y,parameterGroup:point.run}:{x:point.p.x,y:point.p.y,run:point.run}); });
  }
  function currentBoundary() {
    const key = pairKey(els.x.value, els.y.value);
    if (payload.report_mode === 'compare') return [...selectedExperiments()].map(experimentId => ({experimentId,boundary:payload.boundary?.by_experiment?.[experimentId]?.[key]})).filter(item => item.boundary);
    const boundary=payload.boundary?.pairs?.[key];
    return boundary ? [{experimentId:experiments[0]?.id || 'experiment',boundary}] : [];
  }
  function pairKey(x, y) {
    const exact = payload.parameter_pairs.find(pair => pair.x === x && pair.y === y);
    if (exact) return exact.key;
    return `${x.toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_|_$/g,'')}__${y.toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_|_$/g,'')}`;
  }
  function drawBoundaryOverlay() {
    const boundaries = currentBoundary();
    if (!boundaries.length || !state.projected.length) return;
    const rows = filteredRuns();
    const points = rows.map(run => ({x:paramValue(run, els.x.value), y:paramValue(run, els.y.value)})).filter(p => p.x !== null && p.y !== null);
    if (!points.length) return;
    const w = els.canvas.width, h = els.canvas.height, margin = 70 * devicePixelRatio;
    const [xMin,xMax] = range(points.map(p => p.x)), [yMin,yMax] = range(points.map(p => p.y));
    const sx = x => margin + (x-xMin)/(xMax-xMin)*(w-2*margin);
    const sy = y => h - margin - (y-yMin)/(yMax-yMin)*(h-2*margin);
    boundaries.forEach(({experimentId,boundary}) => {
      if (!boundary.available) return;
      const color=experimentColors.get(experimentId) || '#facc15';ctx.strokeStyle=color;ctx.lineWidth=2*devicePixelRatio;ctx.setLineDash([6*devicePixelRatio,5*devicePixelRatio]);
      for (const pair of boundary.nearest_boundary_pairs || []) {const a=pair.nonfailure_params,b=pair.failure_params;if(a[els.x.value]===undefined||b[els.x.value]===undefined)continue;ctx.beginPath();ctx.moveTo(sx(Number(a[els.x.value])),sy(Number(a[els.y.value])));ctx.lineTo(sx(Number(b[els.x.value])),sy(Number(b[els.y.value])));ctx.stroke();}
      ctx.setLineDash([]);for(const cell of (boundary.recommended_resampling_cells||[]).slice(0,20)){ctx.fillStyle=color;ctx.beginPath();ctx.arc(sx(cell.x),sy(cell.y),5*devicePixelRatio,0,Math.PI*2);ctx.fill();}
    });
  }
  function selectNearest(clientX, clientY) {
    const rect = els.canvas.getBoundingClientRect();
    const x = (clientX - rect.left) * devicePixelRatio, y = (clientY - rect.top) * devicePixelRatio;
    let best = null, bestD = Infinity;
    state.projected.forEach(item => { const d = (item.x-x)**2 + (item.y-y)**2; if (d < bestD) { bestD = d; best = item; } });
    if (best && bestD < (18 * devicePixelRatio) ** 2) {
      if (best.parameterGroup) showParameterGroup(best.parameterGroup); else if (best.comparePoint) showComparisonPoint(best.comparePoint); else showRun(best.run);
    }
  }
  function showRun(run) {
    document.getElementById('group-inspector').hidden=true;els.detail.hidden=false;
    els.detail.textContent = JSON.stringify(run || {}, null, 2);
    if (run) els.runSelect.value = run.run_id;
    const compare = document.getElementById('compare-run');
    compare.hidden = !run?.comparison_group_id;
    compare.href = run?.comparison_group_id ? `comparison.html?group=${encodeURIComponent(run.comparison_group_id)}` : 'comparison.html';
    compare.textContent = concreteActionLabel(run?.comparison_group_id);
  }
  function showComparisonPoint(point) {
    const group=parameterGroups.find(item=>item.group_id===point?.comparison_group_id);if(group){showParameterGroup(group);return;}
    document.getElementById('group-inspector').hidden=true;els.detail.hidden=false;
    els.detail.textContent = JSON.stringify(point || {}, null, 2);
    const compare=document.getElementById('compare-run');compare.hidden=!point?.comparison_group_id;compare.href=point?.comparison_group_id?`comparison.html?group=${encodeURIComponent(point.comparison_group_id)}`:'comparison.html';
    compare.textContent=concreteActionLabel(point?.comparison_group_id);
  }
  function showParameterGroup(group) {
    const inspector=document.getElementById('group-inspector');inspector.hidden=false;els.detail.hidden=true;
    const parameterRows=Object.entries(group.parameters||{}).map(([parameter,value])=>({parameter,value}));
    const metricSet=new Set(group.experiments.flatMap(item=>Object.keys(item.metrics||{})));
    const experimentRows=group.experiments.map(item=>({experiment:item.experiment_id,role:item.experiment_id===els.reference.value?'Reference':item.experiment_id===els.left.value?'Left':item.experiment_id===els.right.value?'Right':'',run_id:item.run_id,outcome:item.outcome,status:item.status,safety:item.safety_region,termination:item.termination_reason,...Object.fromEntries([...metricSet].map(metric=>[metric,item.metrics?.[metric]]))}));
    inspector.innerHTML=`<h3>Parameters</h3>${table(parameterRows)}<h3>Experiments</h3>${table(experimentRows)}${group.missing_experiments?.length?`<p class="warning">Missing: ${escapeHtml(group.missing_experiments.join(', '))}</p>`:''}`;
    const compare=document.getElementById('compare-run');compare.hidden=!group.group_id;compare.href=group.comparison_url||`comparison.html?group=${encodeURIComponent(group.group_id)}`;compare.textContent=concreteActionLabel(group.group_id);
  }
  function concreteActionLabel(groupId) {const item=concreteScenarios.find(group=>group.group_id===groupId);return (item?.configs?.length||0)>1?'Compare configs':'Analyze concrete run';}
  function drawSelectionSummary(rows) {
    if (rows.length&&rows[0].group_id) {
      const disagreements=rows.filter(group=>outcomeConsensus(group)==='disagreement'||outcomeConsensus(group)==='mixed_with_invalid').length;
      document.getElementById('selection-summary').innerHTML=cards([{label:'Parameter groups',value:rows.length},{label:'Disagreements',value:disagreements},{label:'Complete',value:rows.filter(group=>group.complete).length},{label:'Reference',value:els.reference.value}]);return;
    }
    const outcomes = rows.reduce((map, run) => { const key = run.draft_outcome || run.normalized_outcome || 'unknown'; map[key] = (map[key] || 0) + 1; return map; }, {});
    const failureRate = rows.length ? ((outcomes.failure || 0) / rows.length * 100).toFixed(1) + '%' : '0%';
    document.getElementById('selection-summary').innerHTML = cards([{label:'Visible', value:rows.length}, {label:'Failure rate', value:failureRate}, {label:'Failures', value:outcomes.failure || 0}, {label:'Invalid', value:outcomes.invalid || 0}]);
  }
  function selectedCase() {
    return casePayload.cases.find(item => item.case_type === caseEls.caseSelect.value) || null;
  }
  function selectedCaseSeries() {
    const current = selectedCase();
    return current?.series.find(item => item.field === caseEls.seriesSelect.value && item.source === caseEls.seriesSelect.selectedOptions[0]?.dataset.source) || null;
  }
  function populateCaseViewer() {
    caseEls.caseSelect.textContent = '';
    casePayload.cases.forEach(item => caseEls.caseSelect.appendChild(new Option(`${item.case_type} - ${item.run.run_id}`, item.case_type)));
    populateCaseSeries();
  }
  function populateCaseSeries() {
    const current = selectedCase();
    caseEls.seriesSelect.textContent = '';
    (current?.series || []).forEach(item => {
      const option = new Option(`${item.source}: ${item.label}${item.unit ? ` (${item.unit})` : ''}`, item.field);
      option.dataset.source = item.source;
      caseEls.seriesSelect.appendChild(option);
    });
    caseState.hoverIndex = null;
    drawCaseSeries();
  }
  function caseResize() {
    const rect = caseEls.canvas.getBoundingClientRect();
    caseEls.canvas.width = Math.max(720, Math.floor(rect.width)) * devicePixelRatio;
    caseEls.canvas.height = 380 * devicePixelRatio;
    drawCaseSeries();
  }
  function setCaseMode(mode) {
    caseState.mode = mode;
    caseEls.semantic.classList.toggle('active', mode === 'semantic');
    caseEls.detail.classList.toggle('active', mode === 'detail');
    caseState.hoverIndex = null;
    drawCaseSeries();
  }
  function drawCaseSeries() {
    const item = selectedCaseSeries(), current = selectedCase();
    const w = caseEls.canvas.width, h = caseEls.canvas.height;
    caseCtx.clearRect(0,0,w,h); caseCtx.fillStyle = '#ffffff'; caseCtx.fillRect(0,0,w,h);
    if (!item || !item.points?.length) {
      caseCtx.fillStyle = '#64748b'; caseCtx.font = `${14 * devicePixelRatio}px system-ui`; caseCtx.fillText('No case series available.', 24*devicePixelRatio, 40*devicePixelRatio);
      caseEls.info.textContent = 'No representative-case time series available.';
      return;
    }
    const limits = caseState.mode === 'semantic' ? item.semantic_limits : item.detail_limits;
    const points = item.points, times = points.map(point => Number(point[0]));
    let xMin = Math.min(...times), xMax = Math.max(...times);
    if (xMin === xMax) xMax = xMin + 1;
    const yMin = Number(limits.lower), yMax = Number(limits.upper);
    const left = 72*devicePixelRatio, right = 24*devicePixelRatio, top = 24*devicePixelRatio, bottom = 54*devicePixelRatio;
    const sx = value => left + (value-xMin)/(xMax-xMin)*(w-left-right);
    const sy = value => h-bottom-(value-yMin)/(yMax-yMin)*(h-top-bottom);
    caseCtx.font = `${11 * devicePixelRatio}px system-ui`; caseCtx.textAlign = 'right'; caseCtx.fillStyle = '#64748b';
    for (let index=0; index<=5; index++) {
      const value = yMin + (yMax-yMin)*index/5, y = sy(value);
      caseCtx.strokeStyle = '#e2e8f0'; caseCtx.lineWidth = devicePixelRatio; caseCtx.beginPath(); caseCtx.moveTo(left,y); caseCtx.lineTo(w-right,y); caseCtx.stroke();
      caseCtx.fillText(fmt(value), left-8*devicePixelRatio, y+4*devicePixelRatio);
    }
    if (yMin <= 0 && yMax >= 0) {
      caseCtx.strokeStyle = '#475569'; caseCtx.lineWidth = 1.4*devicePixelRatio; caseCtx.beginPath(); caseCtx.moveTo(left,sy(0)); caseCtx.lineTo(w-right,sy(0)); caseCtx.stroke();
    }
    if (limits.out_of_range) {
      [limits.nominal_lower, limits.nominal_upper].filter(value => value !== null && value > yMin && value < yMax).forEach(value => {
        caseCtx.setLineDash([5*devicePixelRatio,4*devicePixelRatio]); caseCtx.strokeStyle = '#dc2626'; caseCtx.beginPath(); caseCtx.moveTo(left,sy(value)); caseCtx.lineTo(w-right,sy(value)); caseCtx.stroke(); caseCtx.setLineDash([]);
      });
    }
    const eventRows = [...(current?.events || []), ...(current?.collisions || [])];
    eventRows.forEach(event => {
      const time = number(event.sim_time_ms); if (time === null) return;
      const x = sx(time/1000); if (x < left || x > w-right) return;
      caseCtx.strokeStyle = (event.event_type || '').includes('collision') || event.actor_a !== undefined ? '#dc2626' : '#f59e0b';
      caseCtx.lineWidth = devicePixelRatio; caseCtx.beginPath(); caseCtx.moveTo(x,top); caseCtx.lineTo(x,h-bottom); caseCtx.stroke();
    });
    caseCtx.strokeStyle = '#2563eb'; caseCtx.lineWidth = 2*devicePixelRatio; caseCtx.beginPath();
    points.forEach((point,index) => { const x=sx(Number(point[0])), y=sy(Number(point[1])); if (index===0) caseCtx.moveTo(x,y); else caseCtx.lineTo(x,y); }); caseCtx.stroke();
    caseCtx.strokeStyle = '#94a3b8'; caseCtx.lineWidth = devicePixelRatio; caseCtx.beginPath(); caseCtx.moveTo(left,top); caseCtx.lineTo(left,h-bottom); caseCtx.lineTo(w-right,h-bottom); caseCtx.stroke();
    caseCtx.fillStyle = '#334155'; caseCtx.textAlign = 'center'; caseCtx.fillText('Simulation time (s)', (left+w-right)/2, h-16*devicePixelRatio);
    caseCtx.save(); caseCtx.translate(18*devicePixelRatio,(top+h-bottom)/2); caseCtx.rotate(-Math.PI/2); caseCtx.fillText(`${item.label}${item.unit ? ` (${item.unit})` : ''}`,0,0); caseCtx.restore();
    if (caseState.hoverIndex !== null && points[caseState.hoverIndex]) {
      const point = points[caseState.hoverIndex], x=sx(Number(point[0])), y=sy(Number(point[1]));
      caseCtx.fillStyle = '#dc2626'; caseCtx.beginPath(); caseCtx.arc(x,y,5*devicePixelRatio,0,Math.PI*2); caseCtx.fill();
      const label = `t=${fmt(point[0])} s, value=${fmt(point[1])}`; caseCtx.font = `${12*devicePixelRatio}px system-ui`; const tw=caseCtx.measureText(label).width+16*devicePixelRatio;
      const tx=Math.min(Math.max(left,x-tw/2),w-right-tw), ty=Math.max(top,y-38*devicePixelRatio); caseCtx.fillStyle='#102033'; caseCtx.fillRect(tx,ty,tw,26*devicePixelRatio); caseCtx.fillStyle='white'; caseCtx.textAlign='left'; caseCtx.fillText(label,tx+8*devicePixelRatio,ty+17*devicePixelRatio);
    }
    const shared = caseState.mode === 'semantic' && item.shared_semantic_scale ? 'shared across representative cases' : 'case-local';
    const warning = limits.out_of_range ? ' | values exceed nominal range' : '';
    caseEls.info.textContent = `${caseState.mode === 'semantic' ? 'Semantic' : 'Detail'} scale: ${fmt(yMin)} to ${fmt(yMax)}${item.unit ? ' ' + item.unit : ''} | ${shared}${warning}`;
  }
  function hoverCaseSeries(event) {
    const item = selectedCaseSeries(); if (!item?.points?.length) return;
    const rect = caseEls.canvas.getBoundingClientRect(), x = (event.clientX-rect.left)*devicePixelRatio;
    const times = item.points.map(point => Number(point[0])), xMin=Math.min(...times);
    let xMax=Math.max(...times); if (xMax === xMin) xMax = xMin + 1;
    const left=72*devicePixelRatio, right=24*devicePixelRatio;
    const target=xMin+(x-left)/(caseEls.canvas.width-left-right)*(xMax-xMin);
    let best=0, distance=Infinity; times.forEach((time,index) => { const candidate=Math.abs(time-target); if (candidate<distance) { distance=candidate; best=index; } });
    caseState.hoverIndex=best; drawCaseSeries();
  }
  function disagreementTable(rows) {
    if (!rows?.length) return '<p class="muted">No failure disagreement.</p>';
    return '<table><thead><tr><th>Comparison</th><th>Match key</th><th>Left run</th><th>Right run</th><th>Left failure</th><th>Right failure</th><th>Detail</th></tr></thead><tbody>' + rows.map(row => {
      const run=runs.find(item=>item.run_id===row.left_run_id)||runs.find(item=>item.run_id===row.right_run_id),group=run?.comparison_group_id;
      const link=group?`<a class="table-link" href="comparison.html?group=${encodeURIComponent(group)}">Open comparison</a>`:'<span class="muted">Unavailable</span>';
      return `<tr><td>${escapeHtml(row.comparison)}</td><td>${escapeHtml(row.match_key)}</td><td>${escapeHtml(row.left_run_id)}</td><td>${escapeHtml(row.right_run_id)}</td><td>${escapeHtml(row.left_failure)}</td><td>${escapeHtml(row.right_failure)}</td><td>${link}</td></tr>`;
    }).join('') + '</tbody></table>';
  }
  function renderInsights(items) {
    return (items||[]).map(item => {const actions=(item.actions||[]).map(action=>action.type==='open_comparison'?`<a class="button-link" href="comparison.html?group=${encodeURIComponent(action.group_id)}">${escapeHtml(action.label)}</a>`:'').join('');return `<article class="insight ${item.severity}"><h3>${escapeHtml(item.title)}</h3><p>${escapeHtml(item.description)}</p><pre>${escapeHtml(JSON.stringify(item.evidence,null,2))}</pre>${actions}</article>`;}).join('') || '<p class="muted">No automatic insights.</p>';
  }
  function addFilterOptions(select,values,label='All') { select.textContent='';select.appendChild(new Option(label,''));[...new Set(values.filter(Boolean))].sort().forEach(value=>select.appendChild(new Option(value,value))); }
  function populateSensitivity() {
    const data=payload.sensitivity||{},ids=[...(data.model_quality||[]),...(data.effects||[])].map(row=>row.experiment_id);addOptions(sensitivityEls.experiment,unique(ids));updateSensitivityTargets();document.getElementById('sensitivity-sampling-table').innerHTML=table(data.sampling_plan||[]);document.getElementById('sensitivity-warnings').textContent=(data.warnings||[]).join('\\n')||'No sensitivity warnings.';
  }
  function updateSensitivityTargets() {const data=payload.sensitivity||{},targets=[...(data.model_quality||[]),...(data.effects||[])].filter(row=>row.experiment_id===sensitivityEls.experiment.value).map(row=>row.target);const previous=sensitivityEls.target.value;addOptions(sensitivityEls.target,unique(targets));if([...sensitivityEls.target.options].some(option=>option.value===previous))sensitivityEls.target.value=previous;updateSensitivityParameters();}
  function updateSensitivityParameters() {const data=payload.sensitivity||{},parameters=(data.effects||[]).filter(row=>row.experiment_id===sensitivityEls.experiment.value&&row.target===sensitivityEls.target.value).map(row=>row.parameter);const previous=sensitivityEls.parameter.value;addOptions(sensitivityEls.parameter,unique(parameters));if([...sensitivityEls.parameter.options].some(option=>option.value===previous))sensitivityEls.parameter.value=previous;renderSensitivity();}
  function renderSensitivity() {
    const data=payload.sensitivity||{},experiment=sensitivityEls.experiment.value,target=sensitivityEls.target.value,parameter=sensitivityEls.parameter.value;
    const quality=(data.model_quality||[]).find(row=>row.experiment_id===experiment&&row.target===target)||{};
    const effects=(data.effects||[]).filter(row=>row.experiment_id===experiment&&row.target===target);
    const allImportance=(data.importance||[]).filter(row=>row.experiment_id===experiment&&row.target===target);
    const importance=allImportance.filter(row=>(row.importance_type||'parameter')==='parameter');
    const clusters=allImportance.filter(row=>row.importance_type==='correlated_cluster');
    const profiles=(data.profiles||[]).filter(row=>row.experiment_id===experiment&&row.target===target&&row.parameter===parameter);
    const interactions=(data.interactions||[]).filter(row=>row.experiment_id===experiment&&row.target===target);
    const correlations=(data.correlations||[]).filter(row=>row.experiment_id===experiment);
    const top=[...importance].sort((a,b)=>(b.importance_mean||0)-(a.importance_mean||0))[0],topInteraction=[...interactions].sort((a,b)=>(b.h_statistic||0)-(a.h_statistic||0))[0];
    const experimentIds=experiments.map(item=>item.id),rankingRows=new Map();
    (data.importance||[]).filter(row=>row.target===target&&row.importance_type==='parameter'&&experimentIds.includes(row.experiment_id)).forEach(row=>{if(!rankingRows.has(row.parameter))rankingRows.set(row.parameter,{parameter:row.parameter});const item=rankingRows.get(row.parameter);item[`${row.experiment_id} rank`]=row.rank;item[`${row.experiment_id} importance`]=row.importance_mean;});
    document.getElementById('sensitivity-cards').innerHTML=cards([{label:'Samples',value:quality.sample_count??0},{label:'Reliability',value:quality.reliability||'unavailable'},{label:'Top driver',value:top?.parameter||'unavailable'},{label:'Top interaction',value:topInteraction?`${topInteraction.left_parameter} × ${topInteraction.right_parameter}`:'unavailable'}]);
    document.getElementById('sensitivity-effect-table').innerHTML=table(effects.map(row=>({parameter:row.parameter,effect:row.effect,effect_ci_low:row.effect_ci_low,effect_ci_high:row.effect_ci_high,q_value:row.q_value,characteristic:row.characteristic,n:row.sample_count})));
    document.getElementById('sensitivity-quality-table').innerHTML=table(quality.experiment_id?[quality]:[]);
    document.getElementById('sensitivity-interaction-table').innerHTML=table(interactions.map(row=>({left:row.left_parameter,right:row.right_parameter,H:row.h_statistic})));
    document.getElementById('sensitivity-cluster-table').innerHTML=table(clusters.map(row=>({parameters:row.parameter,group_importance:row.importance_mean,ci_low:row.importance_ci_low,ci_high:row.importance_ci_high,reliability:row.reliability})));
    document.getElementById('sensitivity-compare-table').innerHTML=experimentIds.length>1?table([...rankingRows.values()]):'<p class="muted">Available in compare reports.</p>';
    document.getElementById('sensitivity-correlation-table').innerHTML=table(correlations);
    drawSensitivity(importance,profiles,interactions);
  }
  function drawSensitivity(importance,profiles,interactions) {const canvas=sensitivityEls.canvas,rect=canvas.getBoundingClientRect();canvas.width=Math.max(720,Math.floor(rect.width))*devicePixelRatio;canvas.height=430*devicePixelRatio;const ctx=sensitivityCtx,w=canvas.width,h=canvas.height,m=58*devicePixelRatio;ctx.fillStyle='white';ctx.fillRect(0,0,w,h);ctx.font=`${12*devicePixelRatio}px system-ui`;const view=sensitivityEls.view.value;if(view==='importance'){const rows=[...importance].sort((a,b)=>(b.importance_mean||0)-(a.importance_mean||0)).slice(0,12),max=Math.max(...rows.map(row=>Math.max(0,row.importance_ci_high||row.importance_mean||0)),1e-9),barH=(h-2*m)/Math.max(1,rows.length);rows.forEach((row,index)=>{const value=Math.max(0,row.importance_mean||0),y=m+index*barH;ctx.fillStyle='#2563eb';ctx.fillRect(m,y,(w-2*m)*value/max,barH*.58);ctx.fillStyle='#17202a';ctx.textAlign='right';ctx.fillText(row.parameter,m-8*devicePixelRatio,y+barH*.45);ctx.textAlign='left';ctx.fillText(fmt(value),m+(w-2*m)*value/max+7*devicePixelRatio,y+barH*.45);});if(!rows.length)emptySensitivity('Model importance unavailable.');return;}if(view==='interaction'){const rows=[...interactions].sort((a,b)=>(b.h_statistic||0)-(a.h_statistic||0)).slice(0,10),max=Math.max(...rows.map(row=>row.h_statistic||0),1e-9),barH=(h-2*m)/Math.max(1,rows.length);rows.forEach((row,index)=>{const value=row.h_statistic||0,y=m+index*barH;ctx.fillStyle='#7c3aed';ctx.fillRect(m,y,(w-2*m)*value/max,barH*.58);ctx.fillStyle='#17202a';ctx.textAlign='right';ctx.fillText(`${row.left_parameter} × ${row.right_parameter}`,m-8*devicePixelRatio,y+barH*.45);});if(!rows.length)emptySensitivity('Interaction analysis unavailable.');return;}const empirical=profiles.filter(row=>row.method==='empirical'),numeric=empirical.every(row=>number(row.x)!==null);if(!empirical.length){emptySensitivity('Response profile unavailable.');return;}if(!numeric){const max=Math.max(...empirical.map(row=>Math.abs(row.estimate||0)),1e-9),barW=(w-2*m)/empirical.length;empirical.forEach((row,index)=>{const height=(h-2*m)*Math.abs(row.estimate||0)/max;ctx.fillStyle='#0891b2';ctx.fillRect(m+index*barW,h-m-height,barW*.7,height);ctx.fillStyle='#17202a';ctx.textAlign='center';ctx.fillText(text(row.x),m+index*barW+barW*.35,h-m+18*devicePixelRatio);});return;}const xs=empirical.map(row=>Number(row.x)),ys=empirical.map(row=>Number(row.estimate)),xRange=range(xs),yRange=range(ys),sx=x=>m+(x-xRange[0])/(xRange[1]-xRange[0])*(w-2*m),sy=y=>h-m-(y-yRange[0])/(yRange[1]-yRange[0])*(h-2*m);ctx.strokeStyle='#0891b2';ctx.lineWidth=2*devicePixelRatio;ctx.beginPath();empirical.forEach((row,index)=>index?ctx.lineTo(sx(Number(row.x)),sy(Number(row.estimate))):ctx.moveTo(sx(Number(row.x)),sy(Number(row.estimate))));ctx.stroke();empirical.forEach(row=>{ctx.fillStyle='#0891b2';ctx.beginPath();ctx.arc(sx(Number(row.x)),sy(Number(row.estimate)),4*devicePixelRatio,0,Math.PI*2);ctx.fill();});}
  function emptySensitivity(message){sensitivityCtx.fillStyle='#64748b';sensitivityCtx.textAlign='left';sensitivityCtx.fillText(message,30*devicePixelRatio,45*devicePixelRatio);}
  function populateFigureBrowser() {
    const svgs=payload.figures.filter(item=>item.format==='svg');
    addFilterOptions(figureEls.category,svgs.map(item=>item.category));addFilterOptions(figureEls.pair,svgs.map(item=>item.parameter_pair));addFilterOptions(figureEls.metric,svgs.map(item=>item.metric));
    addOptions(figureEls.left,experiments.map(item=>item.id));addOptions(figureEls.right,experiments.map(item=>item.id));
    figureEls.left.value=els.left.value||experiments[0]?.id||'';figureEls.right.value=els.right.value||experiments[1]?.id||experiments[0]?.id||'';
    document.getElementById('figure-compare-controls').hidden=payload.report_mode!=='compare';refreshFigureTypes();
  }
  function filteredFigureDefinitions() {
    const query=figureEls.search.value.toLowerCase();
    return payload.figures.filter(item=>item.format==='svg'&&(!figureEls.category.value||item.category===figureEls.category.value)&&(!figureEls.pair.value||item.parameter_pair===figureEls.pair.value)&&(!figureEls.metric.value||item.metric===figureEls.metric.value)&&(!query||[item.title,...(item.tags||[])].join(' ').toLowerCase().includes(query)));
  }
  function refreshFigureTypes() {
    const previous=figureEls.type.value,definitions=filteredFigureDefinitions(),uniqueDefinitions=new Map();definitions.forEach(item=>uniqueDefinitions.set(item.figure_key,item));
    figureEls.type.textContent='';uniqueDefinitions.forEach(item=>figureEls.type.appendChild(new Option(item.title,item.figure_key)));if([...uniqueDefinitions].some(([key])=>key===previous))figureEls.type.value=previous;renderFigure();
  }
  function figureArtifact(key,experimentId,format='svg') {
    const figureKey=experiments.find(item=>item.id===experimentId)?.figure_key||experimentId;
    return payload.figures.find(item=>item.figure_key===key&&item.format===format&&(item.scope!=='experiment'||item.experiment_id===figureKey));
  }
  function figureCard(key,experimentId) {
    const artifact=figureArtifact(key,experimentId);if(!artifact)return `<div class="figure-empty">No corresponding figure for ${escapeHtml(experimentId)}</div>`;
    const formats=payload.figures.filter(item=>item.figure_key===key&&item.scope===artifact.scope&&item.experiment_id===artifact.experiment_id).map(item=>`<a href="../${encodeURI(item.path)}">${escapeHtml(item.format.toUpperCase())}</a>`).join(' · ');
    return `<article class="figure"><h3>${escapeHtml(experimentId)}</h3><img loading="lazy" src="../${encodeURI(artifact.path)}" alt="${escapeHtml(artifact.title)}"><p>${formats}</p></article>`;
  }
  function renderFigure() {
    const key=figureEls.type.value;if(!key){figureEls.viewer.className='figure-viewer single';figureEls.viewer.innerHTML='<div class="figure-empty">No figure matches the current filters.</div>';return;}
    const definition=payload.figures.find(item=>item.figure_key===key&&item.format==='svg');
    if(payload.report_mode==='compare'&&payload.figures.some(item=>item.figure_key===key&&item.scope==='experiment')){figureEls.viewer.className='figure-viewer';figureEls.viewer.innerHTML=figureCard(key,figureEls.left.value)+figureCard(key,figureEls.right.value);}
    else {figureEls.viewer.className='figure-viewer single';figureEls.viewer.innerHTML=definition?`<article class="figure"><h3>${escapeHtml(definition.title)}</h3><img loading="lazy" src="../${encodeURI(definition.path)}" alt="${escapeHtml(definition.title)}"></article>`:'<div class="figure-empty">Figure unavailable.</div>';}
  }
  function renderStatic() {
    const s = payload.summary;
    document.getElementById('summary-cards').innerHTML = cards([{label:'Runs', value:s.run_count}, {label:'Experiments', value:s.experiment_count}, {label:'Parameters', value:s.parameter_count}, {label:'Warnings', value:s.warning_count}]);
    document.getElementById('outcome-table').innerHTML = table(payload.report_mode === 'compare' ? payload.experiment_summaries.outcomes : s.outcomes);
    document.getElementById('metric-table').innerHTML = table(payload.report_mode === 'compare' ? payload.experiment_summaries.metrics : payload.metrics);
    document.getElementById('case-table').innerHTML = table(payload.representative_cases);
    document.getElementById('component-table').innerHTML = table(payload.comparison.component_comparison);
    document.getElementById('transition-table').innerHTML = table(payload.comparison.outcome_transition);
    document.getElementById('paired-table').innerHTML = table(payload.comparison.paired_summary);
    document.getElementById('disagreement-table').innerHTML = disagreementTable(payload.comparison.failure_disagreement);
    document.getElementById('quality-table').innerHTML = table(payload.data_quality.findings);
    document.getElementById('warnings').textContent = (payload.data_quality.warnings || []).join('\\n') || 'No warnings.';
    document.getElementById('quality-cards').innerHTML = cards(Object.entries(payload.data_quality.source_files || {}).map(([label,value]) => ({label, value})));
    const boundaryRows=payload.report_mode==='compare'?Object.entries(payload.boundary.by_experiment||{}).flatMap(([experiment,pairs])=>Object.entries(pairs).map(([key,value])=>({experiment_id:experiment,pair:key,available:value.available,recommendations:(value.recommended_resampling_cells||[]).length,nearest_pairs:(value.nearest_boundary_pairs||[]).length,reason:value.reason||''}))):Object.entries(payload.boundary.pairs||{}).map(([key,value])=>({pair:key,available:value.available,recommendations:(value.recommended_resampling_cells||[]).length,nearest_pairs:(value.nearest_boundary_pairs||[]).length,reason:value.reason||''}));
    document.getElementById('boundary-table').innerHTML=table(boundaryRows);
    document.getElementById('insight-list').innerHTML = renderInsights(payload.insights);
    populateSensitivity();
    populateFigureBrowser();
  }
  function updateFigureVisibility() {
    renderFigure();
  }
  function populate() {
    if (payload.report_mode==='compare') els.view.querySelector('option[value="heatmap"]')?.remove();
    addOptions(els.x, numericParams); addOptions(els.y, numericParams); addOptions(els.z, numericParams, true);
    addOptions(els.color, ['normalized_outcome','safety_region','termination_reason','status', ...numericParams.map(p => 'param:' + p), ...metricNames.map(m => 'metric:' + m)]);
    els.pair.textContent = ''; payload.parameter_pairs.forEach(pair => els.pair.appendChild(new Option(`${pair.x} vs ${pair.y}`, pair.key)));
    const axes = payload.summary.default_axes || {}; els.x.value = axes.x || numericParams[0] || ''; els.y.value = axes.y || numericParams.find(p => p !== els.x.value) || ''; els.color.value = 'normalized_outcome';
    checkboxGroup(els.experimentFilters, experiments.map(item => item.id));
    addOptions(els.reference, experiments.map(item => item.id)); addOptions(els.left, experiments.map(item => item.id)); addOptions(els.right, experiments.map(item => item.id)); addOptions(els.deltaMetric, metricNames);
    if (experiments.length) els.reference.value=experiments[0].id;
    if (experiments.length > 1) { els.left.value = experiments[0].id; els.right.value = experiments[1].id; }
    else { els.mode.value = 'explore'; els.mode.disabled = true; }
    checkboxGroup(els.outcomeFilters, runs.map(run => run.normalized_outcome || 'unknown'));
    checkboxGroup(els.safetyFilters, runs.map(run => run.safety_region || 'unknown'));
    checkboxGroup(els.statusFilters, runs.map(run => run.status || 'unknown'));
    runs.forEach(run => els.runSelect.appendChild(new Option(`${run.run_id} - ${run.normalized_outcome}`, run.run_id)));
    document.getElementById('draft-ttc').value = payload.summary.near_critical_ttc_s;
    updateRuleFields();
    updateCompareControls();
  }
  function updateCompareControls() {
    const comparing = els.mode.value !== 'explore';
    document.querySelectorAll('.compare-only').forEach(item => item.hidden = !comparing);
    document.getElementById('reference-control').hidden = payload.report_mode!=='compare'||comparing;
    document.getElementById('delta-control').hidden = els.mode.value !== 'metric_delta';
    els.color.disabled = comparing; els.view.disabled = comparing; els.overlay.disabled = comparing;
  }
  function keepComparisonPairDistinct(changed) {
    if (experiments.length < 2 || els.left.value !== els.right.value) return;
    const replacement = experiments.find(item => item.id !== changed.value);
    if (!replacement) return;
    (changed === els.left ? els.right : els.left).value = replacement.id;
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
  populate(); populateCaseViewer(); renderStatic(); resize(); caseResize();
  els.pair.addEventListener('change', () => { const pair = payload.parameter_pairs.find(item => item.key === els.pair.value); if (pair) { els.x.value = pair.x; els.y.value = pair.y; } updateFigureVisibility(); draw(); });
  [els.x,els.y,els.z,els.color,els.view,els.overlay,els.deltaMetric,els.reference].forEach(el => el.addEventListener('change', () => { updateFigureVisibility(); draw(); }));
  [els.left,els.right].forEach(control => control.addEventListener('change', () => { keepComparisonPairDistinct(control);if(figureEls.left.options.length){figureEls.left.value=els.left.value;figureEls.right.value=els.right.value;}updateFigureVisibility();draw(); }));
  els.mode.addEventListener('change', () => { updateCompareControls(); updateFigureVisibility(); draw(); });
  els.experimentFilters.addEventListener('change', () => { updateFigureVisibility(); draw(); });
  document.getElementById('experiments-all').addEventListener('click', () => { els.experimentFilters.querySelectorAll('input').forEach(input => input.checked = true); updateFigureVisibility(); draw(); });
  document.getElementById('experiments-clear').addEventListener('click', () => { els.experimentFilters.querySelectorAll('input').forEach(input => input.checked = false); updateFigureVisibility(); draw(); });
  [figureEls.category,figureEls.pair,figureEls.metric].forEach(control=>control.addEventListener('change',refreshFigureTypes));
  figureEls.search.addEventListener('input',refreshFigureTypes);figureEls.type.addEventListener('change',renderFigure);
  [figureEls.left,figureEls.right].forEach(control=>control.addEventListener('change',()=>{if(figureEls.left.value===figureEls.right.value&&experiments.length>1){const other=experiments.find(item=>item.id!==control.value);(control===figureEls.left?figureEls.right:figureEls.left).value=other.id;}els.left.value=figureEls.left.value;els.right.value=figureEls.right.value;renderFigure();draw();}));
  sensitivityEls.experiment.addEventListener('change',updateSensitivityTargets);sensitivityEls.target.addEventListener('change',updateSensitivityParameters);[sensitivityEls.view,sensitivityEls.parameter].forEach(control=>control.addEventListener('change',renderSensitivity));
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
  caseEls.caseSelect.addEventListener('change', populateCaseSeries);
  caseEls.seriesSelect.addEventListener('change', () => { caseState.hoverIndex = null; drawCaseSeries(); });
  caseEls.semantic.addEventListener('click', () => setCaseMode('semantic'));
  caseEls.detail.addEventListener('click', () => setCaseMode('detail'));
  caseEls.canvas.addEventListener('mousemove', hoverCaseSeries);
  caseEls.canvas.addEventListener('mouseleave', () => { caseState.hoverIndex = null; drawCaseSeries(); });
  window.addEventListener('resize', () => { resize(); caseResize(); renderSensitivity(); });
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
    experiment_outcome_rows,
    experiment_metric_rows,
    parameter_rows,
    paired_summary_rows,
    cases,
    warnings,
) -> None:
    compare_mode = len({run.experiment_id for run in runs}) > 1
    lines = [
        "# PISA Validation Evidence",
        "",
        f"- Runs: {len(runs)}",
        f"- Experiments: {len({run.experiment_id for run in runs})}",
        "",
        "## Outcomes",
        "",
        _markdown_table(experiment_outcome_rows if compare_mode else outcome_rows),
        "",
        "## Metrics",
        "",
        _markdown_table(experiment_metric_rows if compare_mode else metric_rows),
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


def _write_latex_summary(
    path: Path,
    *,
    outcome_rows,
    metric_rows,
    experiment_outcome_rows,
    experiment_metric_rows,
    paired_summary_rows,
) -> None:
    if experiment_outcome_rows and len(experiment_outcome_rows) > 1:
        _write_comparison_latex_summary(
            path,
            experiment_outcome_rows,
            experiment_metric_rows,
            paired_summary_rows,
        )
        return
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


def _write_comparison_latex_summary(
    path: Path,
    outcome_rows: list[dict[str, Any]],
    metric_rows: list[dict[str, Any]],
    paired_summary_rows: list[dict[str, Any]],
) -> None:
    lines = [
        "% Generated by pisa-analysis-tools",
        "\\begin{tabular}{lrrrr}",
        "\\hline",
        "Experiment & Runs & Success & Failure & Invalid \\\\",
        "\\hline",
    ]
    for row in outcome_rows:
        lines.append(
            f"{_latex(row['experiment_id'])} & {row['run_count']} & "
            f"{row['success_count']} & {row['failure_count']} & {row['invalid_count']} \\\\"
        )
    lines.extend(
        [
            "\\hline",
            "\\end{tabular}",
            "",
            "\\begin{tabular}{llrrrr}",
            "\\hline",
            "Experiment & Metric & Mean & Median & P05 & P95 \\\\",
            "\\hline",
        ]
    )
    for row in metric_rows:
        lines.append(
            f"{_latex(row['experiment_id'])} & {_latex(row['metric'])} & "
            f"{_number(row['mean'])} & {_number(row['median'])} & "
            f"{_number(row['p05'])} & {_number(row['p95'])} \\\\"
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
