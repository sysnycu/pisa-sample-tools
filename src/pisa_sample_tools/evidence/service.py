from __future__ import annotations

import csv
import json
import shutil
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from pisa_sample_tools.common.yaml import write_yaml

from .campaign import load_campaign
from .ingest import load_experiments, read_trace_rows
from .models import AnalysisSpec, DatasetSpec, EvidenceError, EvidenceResult, RunRecord
from .plots import (
    render_component_figures,
    render_core_figures,
    render_representative_cases,
)
from .spec import load_analysis_spec, spec_to_dict
from .statistics import (
    as_float,
    grouped_outcomes,
    metric_value,
    normalized_outcome,
    numeric_summary,
    repeated_run_rows,
    safety_region,
    select_representative_cases,
)


def build_evidence(
    *,
    results_paths: list[Path] | None = None,
    campaign_path: Path | None = None,
    output_dir: Path,
    spec_path: Path | None = None,
    overwrite: bool = False,
    progress: Callable[[str], None] | None = None,
) -> EvidenceResult:
    reporter = _ProgressReporter(progress)
    reporter.step("loading analysis spec")
    spec = load_analysis_spec(spec_path)
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
    reporter.step(f"preparing output directory {output_dir}")
    _prepare_output_dir(output_dir, overwrite=overwrite)
    reporter.step(f"loading {len(datasets)} dataset(s)")
    runs, warnings = load_experiments(datasets, spec, progress=reporter.step)
    reporter.step("deriving missing summary metrics")
    runs, derived_warnings = _derive_summary_metrics(runs, spec)
    warnings.extend(derived_warnings)
    reporter.step("selecting parameter axes")
    x_param, y_param = _select_axes(runs, spec)
    if spec.x_param and x_param != spec.x_param:
        warnings.append(
            f"configured x parameter '{spec.x_param}' was not found; using '{x_param}'"
        )
    if spec.y_param and y_param != spec.y_param:
        warnings.append(
            f"configured y parameter '{spec.y_param}' was not found; using '{y_param}'"
        )
    reporter.step("selecting representative cases")
    cases = select_representative_cases(runs, spec, x_param, y_param)

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
    _write_rows(summary_dir / "outcomes.csv", outcome_rows)
    _write_rows(summary_dir / "metrics.csv", metric_rows)
    _write_rows(summary_dir / "parameters.csv", parameter_rows)
    _write_rows(summary_dir / "execution_performance.csv", performance_rows)
    _write_rows(cases_dir / "selected_cases.csv", _selected_case_rows(cases, spec))

    reporter.step("writing comparison tables")
    component_rows = _component_rows(runs, spec)
    repeat_rows = repeated_run_rows(runs, spec)
    _write_rows(comparison_dir / "component_comparison.csv", component_rows)
    _write_rows(comparison_dir / "repeated_run_stability.csv", repeat_rows)

    reporter.step("rendering core figures")
    figure_paths = render_core_figures(
        runs,
        spec,
        figures_dir,
        x_param=x_param,
        y_param=y_param,
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
    write_yaml(provenance_dir / "analysis_spec.yaml", resolved_spec)
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
            "collision_events_csv": sum(run.collision_events_path is not None for run in runs),
            "control_commands_csv": sum(run.control_commands_path is not None for run in runs),
        },
    }
    write_yaml(provenance_dir / "input_manifest.yaml", input_manifest)
    (provenance_dir / "warnings.txt").write_text(
        "\n".join(f"- {warning}" for warning in warnings) + ("\n" if warnings else ""),
        encoding="utf-8",
    )

    reporter.step("writing reports")
    report_path = report_dir / "analysis_report.html"
    _write_html_report(
        report_path,
        output_dir=output_dir,
        runs=runs,
        spec=spec,
        x_param=x_param,
        y_param=y_param,
        cases=cases,
        outcome_rows=outcome_rows,
        metric_rows=metric_rows,
        performance_rows=performance_rows,
        figure_paths=figure_paths,
        warnings=warnings,
    )
    _write_markdown_report(
        report_dir / "analysis_report.md",
        runs=runs,
        outcome_rows=outcome_rows,
        metric_rows=metric_rows,
        cases=cases,
        warnings=warnings,
    )
    _write_latex_summary(
        report_dir / "paper_ready_summary.tex",
        outcome_rows=outcome_rows,
        metric_rows=metric_rows,
    )
    _write_limitations(report_dir / "limitations.md", input_manifest, warnings)

    manifest_path = output_dir / "manifest.yaml"
    manifest = {
        "tool": "pisa-analysis-tools",
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "run_count": len(runs),
        "warning_count": len(warnings),
        "analysis_spec": str(provenance_dir / "analysis_spec.yaml"),
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
    return EvidenceResult(
        output_dir=output_dir,
        report_path=report_path,
        manifest_path=manifest_path,
        run_count=len(runs),
        figure_paths=tuple(figure_paths),
        warning_count=len(warnings),
    )


class _ProgressReporter:
    def __init__(self, emit: Callable[[str], None] | None) -> None:
        self._emit = emit
        self._started = time.perf_counter()
        self._last = self._started

    def step(self, message: str) -> None:
        if self._emit is None:
            return
        now = time.perf_counter()
        elapsed = now - self._started
        delta = now - self._last
        self._last = now
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
        collision_rows = read_trace_rows(run.collision_events_path)
        if collision_rows and "collision_time_ms" not in metrics:
            times = [
                value
                for row in collision_rows
                if (value := as_float(row.get("sim_time_ms"))) is not None
            ]
            if times:
                metrics["collision_time_ms"] = min(times)
        updated.append(replace(run, metrics=metrics))
    warnings = [
        f"derived {summary} from {series} time series for {count} run(s)"
        for (summary, series), count in sorted(derivation_counts.items())
    ]
    return updated, warnings


def _select_axes(runs: list[RunRecord], spec: AnalysisSpec) -> tuple[str | None, str | None]:
    names = sorted({name for run in runs for name in run.params})
    numeric = [
        name
        for name in names
        if any(as_float(run.params.get(name)) is not None for run in runs)
    ]
    x_param = spec.x_param if spec.x_param in names else (numeric[0] if numeric else None)
    y_param = spec.y_param if spec.y_param in names else (
        numeric[1] if len(numeric) > 1 else None
    )
    return x_param, y_param


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


def _component_rows(runs: list[RunRecord], spec: AnalysisSpec) -> list[dict[str, Any]]:
    rows = []
    for field in ("sampler_name", "av_name", "simulator_name"):
        groups: dict[str, list[RunRecord]] = defaultdict(list)
        for run in runs:
            if run.metadata.get(field) not in {None, ""}:
                groups[str(run.metadata[field])].append(run)
        for value, members in sorted(groups.items()):
            counts = grouped_outcomes(members, spec)
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
    runs: list[RunRecord],
    spec: AnalysisSpec,
    x_param: str | None,
    y_param: str | None,
    cases,
    outcome_rows,
    metric_rows,
    performance_rows,
    figure_paths: list[Path],
    warnings: list[str],
) -> None:
    images = [
        figure_path
        for figure_path in figure_paths
        if figure_path.suffix == ".svg"
    ]
    sections = "\n".join(
        f'<article class="figure"><h3>{_escape(image.stem.replace("_", " ").title())}</h3>'
        f'<a href="../{_escape(str(image.relative_to(output_dir)))}">'
        f'<img loading="lazy" src="../{_escape(str(image.relative_to(output_dir)))}"></a></article>'
        for image in images
    )
    outcome_table = _html_table(outcome_rows)
    metric_table = _html_table(metric_rows)
    performance_table = _html_table(performance_rows)
    case_table = _html_table(_selected_case_rows(cases, spec))
    warning_html = "".join(f"<li>{_escape(value)}</li>" for value in warnings)
    payload = json.dumps(
        [
            {
                "run_id": run.run_id,
                "params": run.params,
                "metrics": run.metrics,
                "metadata": run.metadata,
                "outcome": normalized_outcome(run, spec),
                "termination_reason": run.termination_reason,
                "result_path": str(run.result_path),
            }
            for run in runs
        ],
        ensure_ascii=True,
    ).replace("</", "<\\/")
    run_options = "".join(
        f'<option value="{index}">{_escape(run.run_id)} - '
        f'{_escape(normalized_outcome(run, spec))}</option>'
        for index, run in enumerate(runs)
    )
    path.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>PISA Validation Evidence</title>
  <style>
    :root {{ color-scheme: light; --navy:#102033; --line:#d9e1ea; --paper:#fff; --bg:#f4f7fa; }}
    body {{ margin:0; font-family:Inter,system-ui,sans-serif; color:#17202a; background:var(--bg); }}
    header {{ padding:28px 36px; background:var(--navy); color:white; }}
    main {{ max-width:1440px; margin:auto; padding:24px 36px 60px; }}
    nav a {{ color:#dbeafe; margin-right:18px; }}
    section {{ background:var(--paper); border:1px solid var(--line); border-radius:9px; padding:18px; margin:18px 0; }}
    .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; }}
    .card {{ background:white; border:1px solid var(--line); border-radius:8px; padding:14px; }}
    .card b {{ display:block; font-size:25px; margin-top:4px; }}
    .figures {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(460px,1fr)); gap:16px; }}
    .figure {{ border:1px solid var(--line); border-radius:8px; padding:12px; background:white; }}
    img {{ width:100%; height:auto; }}
    table {{ border-collapse:collapse; width:100%; font-size:13px; overflow:auto; display:block; }}
    th,td {{ border-bottom:1px solid #e5e7eb; padding:7px 9px; text-align:left; white-space:nowrap; }}
    input,select,button {{ box-sizing:border-box; padding:9px; border:1px solid #aab7c4; border-radius:6px; background:white; }}
    input,select {{ width:100%; }}
    button {{ cursor:pointer; background:var(--navy); color:white; }}
    .controls {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:10px; align-items:end; }}
    pre {{ max-height:360px; overflow:auto; background:#f8fafc; border:1px solid var(--line); padding:12px; }}
    .muted {{ color:#64748b; }}
  </style>
</head>
<body>
<header>
  <h1>PISA Validation Evidence</h1>
  <p>Reproducible scenario-level safety analysis</p>
  <nav><a href="#overview">Overview</a><a href="#maps">Evidence</a><a href="#cases">Critical cases</a><a href="#advanced">Advanced</a></nav>
</header>
<main>
  <div class="cards">
    <div class="card">Runs<b>{len(runs)}</b></div>
    <div class="card">Experiments<b>{len({run.experiment_id for run in runs})}</b></div>
    <div class="card">Parameters<b>{len({key for run in runs for key in run.params})}</b></div>
    <div class="card">Warnings<b>{len(warnings)}</b></div>
  </div>
  <section id="overview"><h2>Experiment overview</h2>
    <p>Axes: <code>{_escape(x_param or "none")}</code> / <code>{_escape(y_param or "none")}</code>.
    Threshold: near-critical TTC &lt; {_escape(spec.near_critical_ttc_s)} s.</p>
    <h3>Outcomes</h3>{outcome_table}
    <h3>Safety metrics</h3>{metric_table}
    <h3>Execution performance</h3>{performance_table}
  </section>
  <section id="maps"><h2>Evidence figures</h2><div class="figures">{sections}</div></section>
  <section id="cases"><h2>Representative cases</h2>{case_table}</section>
  <section><h2>Data quality and limitations</h2><ul>{warning_html or "<li>No warnings.</li>"}</ul></section>
  <section id="advanced"><h2>Advanced run explorer</h2>
    <p class="muted">The evidence dashboard is primary. Select a run or search the canonical record without changing official evidence.</p>
    <label>Run<select id="run-select">{run_options}</select></label>
    <input id="search" placeholder="Filter by run id, parameter, component, outcome, or reason">
    <pre id="detail"></pre>
    <h3>Analysis spec draft</h3>
    <p class="muted">Draft changes are exported as YAML. Rerun the CLI with that spec to produce official evidence.</p>
    <div class="controls">
      <label>X parameter<input id="draft-x" value="{_escape(x_param or '')}"></label>
      <label>Y parameter<input id="draft-y" value="{_escape(y_param or '')}"></label>
      <label>Near-critical TTC (s)<input id="draft-ttc" type="number" step="0.1" value="{_escape(spec.near_critical_ttc_s)}"></label>
      <button id="draft-download" type="button">Download YAML spec</button>
    </div>
  </section>
</main>
<script id="run-data" type="application/json">{payload}</script>
<script>
const runs=JSON.parse(document.getElementById('run-data').textContent);
const search=document.getElementById('search'), detail=document.getElementById('detail'), runSelect=document.getElementById('run-select');
function render() {{
  const q=search.value.toLowerCase();
  if (!q) {{
    detail.textContent=JSON.stringify(runs[Number(runSelect.value)] || {{}}, null, 2);
    return;
  }}
  detail.textContent=JSON.stringify(runs.filter(run => JSON.stringify(run).toLowerCase().includes(q)).slice(0,200), null, 2);
}}
search.addEventListener('input',render); runSelect.addEventListener('change',()=>{{search.value='';render();}});
document.getElementById('draft-download').addEventListener('click',()=>{{
  const x=document.getElementById('draft-x').value, y=document.getElementById('draft-y').value;
  const ttc=document.getElementById('draft-ttc').value;
  const yaml=`version: 1
parameters:
  axes:
    x: ${{x}}
    y: ${{y}}
thresholds:
  near_critical_ttc_s: ${{ttc}}
output:
  formats: [svg, png]
`;
  const blob=new Blob([yaml],{{type:'text/yaml'}});
  const link=document.createElement('a'); link.href=URL.createObjectURL(blob);
  link.download='analysis_spec.yaml'; link.click(); URL.revokeObjectURL(link.href);
}});
render();
</script>
</body></html>
""",
        encoding="utf-8",
    )


def _write_markdown_report(
    path: Path,
    *,
    runs: list[RunRecord],
    outcome_rows,
    metric_rows,
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


def _write_latex_summary(path: Path, *, outcome_rows, metric_rows) -> None:
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
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_limitations(path: Path, manifest: dict[str, Any], warnings: list[str]) -> None:
    sources = manifest["source_files"]
    lines = [
        "# Data Quality and Limitations",
        "",
        f"- Result summaries: {sources['result_csv']} / {manifest['run_count']}",
        f"- Frame metric traces: {sources['frame_metrics_csv']} / {manifest['run_count']}",
        f"- Agent-state traces: {sources['agent_states_csv']} / {manifest['run_count']}",
        f"- Collision event streams: {sources['collision_events_csv']} / {manifest['run_count']}",
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
