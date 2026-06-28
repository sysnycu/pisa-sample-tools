# PISA Validation Evidence

Command: `pisa-analysis`

Build a reproducible evidence bundle from one or more completed runner result roots:

```bash
uv run pisa-analysis build \
  --results /path/to/carla-results \
  --spec examples/analysis_spec_v2.yaml \
  --output analysis/cutin \
  --overwrite
```

Repeat `--results` to compare AVs, simulators, samplers, or repeated executions:

```bash
uv run pisa-analysis compare \
  --results /path/to/carla-behavior-agent \
  --results /path/to/carla-autoware \
  --spec examples/analysis_spec_v2.yaml \
  --output analysis/av-comparison
```

For reproducible comparisons, define grouping and display labels on the analysis side:

```bash
uv run pisa-analysis compare \
  --campaign examples/analysis_campaign.yaml \
  --spec examples/analysis_spec_v2.yaml \
  --output analysis/component-comparison
```

Validate schema, outcome mappings, metric bindings, and trace alignment before rendering:

```bash
uv run pisa-analysis validate \
  --results /path/to/results \
  --spec examples/analysis_spec_v2.yaml
```

V2 specs default to strict validation and all-pairwise parameter views. V1 specs remain
supported with permissive fallback behavior. `--validation strict|permissive` overrides the
spec for one invocation.

The runner does not need to know how datasets will be compared. Its optional
`execution_manifest.yaml` contains only execution provenance. AV/simulator/sampler labels,
repeat grouping, and campaign membership belong to `analysis_campaign.yaml`.

The output contains:

```text
summary/                 canonical run, outcome, metric, parameter, and performance CSVs
figures/                 parameter-space and distribution SVG/PNG figures plus plotting CSVs
representative_cases/    selected cases, trajectories, traces, controls, and event timelines
comparison/              aggregate, paired, unmatched, transition, delta, and stability tables
provenance/              source manifests, resolved inputs, data quality, warnings, and timings
report/                  offline HTML, Markdown, LaTeX summary, and limitations
manifest.yaml            complete evidence bundle index
```

The HTML report defaults to an offline interactive dashboard. It reads
`report/analysis_data.js`, with the canonical JSON also written to
`report/analysis_data.json`. The dashboard supports parameter-axis exploration,
outcome/status/safety filters, boundary overlays, representative-case links,
component comparison tables, data-quality review, and draft spec export. Use
`--report-mode static` when a compact, table-first HTML report is preferred for
very large bundles or review workflows.

Representative-case time-series and control plots use semantic axes by default:

- normalized `steer` stays at `-1..1`;
- `throttle` and `brake` stay at `0..1`;
- speed axes include zero and preserve negative reverse values;
- acceleration, steering angle/velocity, and jerk are symmetric around zero;
- shared semantic limits make the same field comparable across selected cases.

The report's case viewer can switch between `Semantic` and case-local `Detail`
scales. Static SVG/PNG evidence always uses the semantic scale. Values outside a
nominal bounded range are not clipped; the plot expands and marks the nominal
boundary. Defaults can be overridden in `analysis_spec.yaml`:

```yaml
visualization:
  axes:
    padding_fraction: 0.08
    fields:
      steer:
        policy: fixed
        detail_policy: auto
        min: -1
        max: 1
      ego.speed:
        policy: include_zero
      ego.acceleration:
        policy: symmetric_zero
        minimum_span: 2.0
```

Supported policies are `auto`, `fixed`, `include_zero`, `nonnegative`, and
`symmetric_zero`.

## Multi-Experiment Report

When a campaign contains more than one dataset, the main report switches to compare
mode. Outcome, metric, performance, boundary, heatmap, and static figure data stay
separated by experiment; pooled CSVs remain available only for compatibility.

The parameter-space explorer defaults to all experiments selected. Point fill shows
the selected value. In compare mode, one circular point represents one canonical
parameter group regardless of experiment count; clicking it opens a table containing
the parameters and every experiment result. Outcome colors describe consensus,
failure, invalid/mixed, or disagreement, while numeric colors use the selected
reference experiment.

Two pairwise modes are available through the **Explorer mode** control:

- **Compare outcomes** classifies matched points across the full success, failure,
  and invalid transition matrix. Unmatched runs use a separate hollow marker.
- **Compare metric delta** colors each matched point by `Right - Left`, using a
  zero-centered diverging scale.

Left and Right can be changed independently when three or more experiments are
present. Clicking a matched point or a failure-disagreement row opens the associated
concrete comparison. Compare bundles additionally write:

```text
summary/experiment_outcomes.csv
summary/experiment_metrics.csv
summary/experiment_execution_performance.csv
figures/experiments/<experiment-id>/...
```

The Evidence Figures section loads one selected artifact at a time. Filters cover
category, parameter pair, metric, and tags. Compare mode pairs the same figure key
from the selected Left and Right experiments and shows explicit unavailable states
when only one side exists.

## Parameter Sensitivity

The interactive report analyzes each experiment independently for failure,
invalidity, and configured numeric metrics. Compare reports additionally analyze
outcome disagreement and `Right - Left` metric deltas at matched parameter settings.
It does not pool experiments into one fitted model.

Three complementary result types are provided:

- empirical effects use rank-biserial correlation or Cramer's V for binary targets,
  and Spearman correlation or Kruskal epsilon-squared for continuous targets;
- response profiles show quantile-binned estimates with confidence intervals and
  identify monotonic, threshold-like, nonlinear, and U-shaped behavior;
- random-forest surrogate models use grouped held-out cross-validation and
  permutation importance, ALE profiles, and approximate Friedman interaction
  strength. Model quality and reliability are always shown with the result.

The report exposes cross-experiment rank tables, correlated-parameter warnings and
group permutation importance. These are especially important for LHS data where
strongly correlated inputs can split or hide individual importance. The generated
tables are:

```text
summary/parameter_sensitivity.csv
summary/parameter_importance.csv
summary/parameter_response_profiles.csv
summary/parameter_interactions.csv
summary/sensitivity_model_quality.csv
summary/parameter_correlations.csv
summary/sensitivity_sampling_plan.csv
```

Observed associations and surrogate importance are screening evidence, not causal
effects and not formal Sobol indices. `sensitivity_sampling_plan.csv` reports the
run budgets for future independent Sobol or Morris campaigns; those methods require
their own structured sampling design. Configure analysis thresholds and target
semantics in the spec:

```yaml
metrics:
  min_ttc:
    summary: ego_to_agent_1.min_ttc_s
    risk_direction: higher_is_safer

sensitivity:
  enabled: true
  targets:
    outcomes: [failure, invalidity]
    metrics: [min_ttc, min_distance]
  minimum_samples: 40
  minimum_minority: 10
  cv_folds: 5
  permutation_repeats: 10
  bootstrap_samples: 500
  top_parameters: 8
```

When sample size, minority outcome count, grouped folds, or predictive quality is
insufficient, the report marks model-based conclusions unavailable or low
reliability while retaining descriptive empirical tables.

The CLI reports sensitivity progress by target and cross-validation fold. Long
surrogate-model stages distinguish model fitting from permutation importance, ALE,
and interaction analysis, and report each target's elapsed time and reliability.
The output is line-oriented so it remains readable in terminals, CI logs, and files.

## Concrete Scenario Analysis

Single- and multi-experiment bundles produce `report/comparison.html`. Open it
directly or select a run in the main parameter-space explorer. Single experiments
show **Analyze concrete run**; matched multi-experiment groups show **Compare
configs**. Concrete scenarios are grouped by logical scenario name and the canonical
parameter hash, with each campaign dataset kept as an independent config. Unmatched
runs remain available as single-config concrete analyses.

The concrete page provides:

- selectable multi-config trajectory overlays with equal XY scale;
- ego and common interacting-actor toggles;
- synchronized simulation-time cursor and optional path-to-cursor display;
- metric and control overlays using stable config colors;
- baseline-relative scalar, trajectory, and series-difference tables;
- control, metric, trajectory, collision, and termination divergence timelines;
- adaptive config selection when many datasets are available.
- a discrete test-step timeline containing only timestamps present in selected data;
- real-time playback at 0.5x, 1x, or 2x simulation speed;
- separate synchronized metric and control panels.

Data stays offline and portable. The main report loads only
`report/comparison_index.js`; selecting one parameter group loads its compact
`report/comparison_data/<group_id>.js` chunk. Numeric metrics and agent states use
simulation-time linear interpolation for pairwise summaries, while controls use
zero-order hold. Values outside the common time interval are not extrapolated.

Configure payload and divergence defaults in `analysis_spec.yaml`:

```yaml
comparison:
  detail:
    enabled: true
    max_points_per_series: 2000
    trajectory_divergence_m: 0.5
    tolerances:
      steer: 0.02
      throttle: 0.05
      brake: 0.05
      speed: 0.5
      acceleration: 0.5
```

Threshold crossings indicate when results begin to differ; they are diagnostic
markers, not pass/fail requirements or causal claims. A map-name mismatch or actor-set
difference remains visible as a data-quality warning.

## Reproducibility

Safety semantics are defined in a versioned `analysis_spec.yaml`. The resolved spec is copied
to the evidence bundle. Browser exploration never changes official evidence; thresholds and
metric bindings must be saved in the spec and the CLI rerun.

Missing summary metrics can be derived from configured frame-series fields. Every derivation
is recorded in `provenance/warnings.txt`.

The analyzer writes `.pisa-analysis-in-progress.yaml` as soon as it takes ownership
of an output directory. Successful builds replace this state with `manifest.yaml`.
If a build is interrupted or fails after output creation, rerunning with `--overwrite`
recognizes the partial marker and safely rebuilds the directory. Empty output
directories are also accepted. Non-empty directories without either ownership marker
remain protected from deletion.

For component comparisons the analyzer pairs runs by `sample_id`, verifies parameter equality,
and falls back to a canonical parameter hash when IDs are unavailable. Unmatched and ambiguous
runs remain visible in comparison artifacts.

## Unified Compatibility Commands

Existing commands remain available. The unified entry point also forwards to them:

```bash
uv run pisa-analysis trajectory ...
uv run pisa-analysis trajectory-compare ...
uv run pisa-analysis outcome-eval ...
uv run pisa-analysis sample preview ...
uv run pisa-analysis sample export ...
```
