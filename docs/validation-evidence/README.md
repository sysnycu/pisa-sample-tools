# PISA Validation Evidence

Command: `pisa-analysis`

Build a reproducible evidence bundle from one or more completed runner result roots:

```bash
uv run pisa-analysis build \
  --results /path/to/carla-results \
  --spec examples/analysis_spec.yaml \
  --output analysis/cutin \
  --overwrite
```

Repeat `--results` to compare AVs, simulators, samplers, or repeated executions:

```bash
uv run pisa-analysis compare \
  --results /path/to/carla-behavior-agent \
  --results /path/to/carla-autoware \
  --spec examples/analysis_spec.yaml \
  --output analysis/av-comparison
```

For reproducible comparisons, define grouping and display labels on the analysis side:

```bash
uv run pisa-analysis compare \
  --campaign examples/analysis_campaign.yaml \
  --spec examples/analysis_spec.yaml \
  --output analysis/component-comparison
```

The runner does not need to know how datasets will be compared. Its optional
`execution_manifest.yaml` contains only execution provenance. AV/simulator/sampler labels,
repeat grouping, and campaign membership belong to `analysis_campaign.yaml`.

The output contains:

```text
summary/                 canonical run, outcome, metric, parameter, and performance CSVs
figures/                 parameter-space and distribution SVG/PNG figures plus plotting CSVs
representative_cases/    selected cases, trajectories, traces, controls, and event timelines
comparison/              component and repeated-run tables/figures
provenance/              resolved analysis spec, input manifest, and warnings
report/                  offline HTML, Markdown, LaTeX summary, and limitations
manifest.yaml            complete evidence bundle index
```

## Reproducibility

Safety semantics are defined in a versioned `analysis_spec.yaml`. The resolved spec is copied
to the evidence bundle. Browser exploration never changes official evidence; thresholds and
metric bindings must be saved in the spec and the CLI rerun.

Missing summary metrics can be derived from configured frame-series fields. Every derivation
is recorded in `provenance/warnings.txt`.

## Unified Compatibility Commands

Existing commands remain available. The unified entry point also forwards to them:

```bash
uv run pisa-analysis trajectory ...
uv run pisa-analysis trajectory-compare ...
uv run pisa-analysis outcome-eval ...
uv run pisa-analysis sample preview ...
uv run pisa-analysis sample export ...
```
