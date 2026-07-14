# pisa-analysis-tools

Offline sampling and scenario-based validation evidence tools for PISA experiments.

The distribution is being renamed from `pisa-sample-tools` because it now covers the full
offline workflow: sampler inspection/export, outcome re-evaluation, trajectory analysis,
component comparison, and paper-ready validation evidence. Existing Python imports and CLI
commands remain compatible.

## Install

```bash
uv sync
```

`pyproject.toml` depends on `simcore` through an editable path dependency:

```toml
[tool.uv.sources]
simcore = { path = "/home/hcis-s05/ysws/PISA/runner", editable = true }
```

If the runner repo moves, update that path and run `uv sync` again. The runner repo must be buildable as an editable package.

## CLI

This repo provides these commands:

- `pisa-analysis`: build reproducible evidence bundles and access the unified CLI. See [docs/validation-evidence](docs/validation-evidence/README.md).
- `pisa-sample-test`: preview raw sampler output from one sampler source file. See [docs/sampler-preview](docs/sampler-preview/README.md).
- `pisa-sample-export`: materialize samples and split them into runner-ready bundle folders. See [docs/sample-export](docs/sample-export/README.md).
- `pisa-sample-analyze`: inspect planned samples, generated explicit samples, or completed runner results. See [docs/sample-analyze](docs/sample-analyze/README.md).
- `pisa-sample-trajectory`: render agent trajectory SVGs from completed runner results. See [docs/trajectory](docs/trajectory/README.md).
- `pisa-trajectory-compare`: compare non-ego trajectories between two simulator result sets. See [docs/trajectory-compare](docs/trajectory-compare/README.md).
- `pisa-outcome-eval`: evaluate offline condition trees against completed monitor logs. See [docs/outcome-eval](docs/outcome-eval/README.md).

Launch the interactive local Report Builder when you prefer to configure experiments,
campaigns, specs, validation, and output without writing CLI commands:

```bash
uv run pisa-analysis builder
```

## Local Experiment Runner

Launch the standalone Docker-based experiment runner:

```bash
uv run pisa-experiment-runner
```

The runner has its own localhost-only web application. It builds and starts simulator/AV
containers, allocates ports, generates the runner spec, streams execution logs, cleans up
owned containers, and can build an evidence report from the completed results. It is kept
separate from the Report Builder so execution presets and container lifecycle state do not
enter the report-authoring workflow.

Bundled profiles cover CARLA, esmini, Simple AV, Autoware, CARLA Agent, and PCLA. Copy
`examples/experiment_runner.yaml` to `config/experiment_runner.yaml` for versioned overrides;
use `config/experiment_runner.local.yaml` for machine-only paths.

See [docs/experiment-runner](docs/experiment-runner/README.md) for the registry structure,
execution stages, safety behavior, and component options.

## Validation Evidence

```bash
uv run pisa-analysis build \
  --results /path/to/runner/results \
  --spec examples/analysis_spec_v2.yaml \
  --output analysis/cutin
```

This produces normalized summary tables, parameter-space safety maps, metric distributions,
representative trajectories and traces, component/repeated-run comparisons, an offline
evidence dashboard, Markdown/LaTeX report artifacts, and complete provenance.

Use `pisa-analysis validate` before large builds. V2 specs provide strict validation,
all-pairwise parameter views, derived parameters, explicit termination mapping, and paired
component statistics; V1 specs remain supported.

## Sampler Preview

`pisa-sample-test` is the quick sampler smoke-test tool. It reads one sampler source file, builds a `simcore.sampler`, and prints the generated params without creating bundle output.

Preview a param range file with the default grid sampler:

```bash
uv run pisa-sample-test /path/to/params.yaml --max-samples 10
```

Preview an LHS sampler with inline sampler options:

```bash
uv run pisa-sample-test /path/to/params.yaml \
  --method lhs \
  --n-samples 100 \
  --seed 7 \
  --max-samples 10
```

Use a sampler config file and machine-readable output:

```bash
uv run pisa-sample-test /path/to/params.yaml \
  --method lhs \
  --config-path /path/to/lhs_sampler.yaml \
  --format yaml
```

Supported output formats are `table`, `yaml`, and `json`. If `--method` is omitted, the tool uses `native` for OpenSCENARIO sources, `explicit` for explicit sample sources, and `grid` for parameter range sources.

The root `sampler_tester.py` file is kept as a compatibility wrapper around this command:

```bash
uv run python sampler_tester.py /path/to/params.yaml --max-samples 10
```

## Sample Export

Generate bundles by shard size:

```bash
uv run pisa-sample-export \
  --runner-spec /path/to/runner_spec.json \
  --shard-size 50
```

Generate a fixed number of bundles:

```bash
uv run pisa-sample-export \
  --runner-spec /path/to/runner_spec.json \
  --num-shards 20
```

By default output goes to:

```text
output/{scenario_name}-{sampler_name}-{total_samples}/
```

You can override that with `--output-dir /path/to/generated_samples`.

`--shard-size` and `--num-shards` are mutually exclusive. Existing output directories are rejected by default. `--overwrite` only replaces an existing directory when it contains this tool's `manifest.yaml`; directories without a manifest are treated as user-owned and are refused.

Use `--dry-run` to resolve inputs, generate the sample plan, and print a summary without writing files:

```bash
uv run pisa-sample-export \
  --runner-spec /path/to/runner_spec.json \
  --shard-size 50 \
  --dry-run
```

Use `--summary` to print a summary after a real export:

```bash
uv run pisa-sample-export \
  --runner-spec /path/to/runner_spec.json \
  --shard-size 50 \
  --summary
```

For scripts, use JSON:

```bash
uv run pisa-sample-export \
  --runner-spec /path/to/runner_spec.json \
  --shard-size 50 \
  --dry-run \
  --summary json
```

Add `--zip` to create an archive next to the output directory:

```bash
uv run pisa-sample-export \
  --runner-spec /path/to/runner_spec.json \
  --shard-size 50 \
  --zip
```

The default archive path is `{output_dir}.zip`. Use `--zip-path /path/to/archive.zip` to choose a specific path.

You can also use a sampler runtime spec directly when you provide a scenario path:

```bash
uv run pisa-sample-export \
  --sampler-spec /path/to/sampler.yaml \
  --scenario-path /path/to/scenario_folder \
  --shard-size 50
```

See [examples/minimal_runner_spec.yaml](examples/minimal_runner_spec.yaml) for the smallest runner-style input.

## Input

A runner-style input needs `scenario` and `sampler`:

```json
{
  "scenario": {
    "scenario_path": "/path/to/scenario_folder",
    "stop_condition_config_path": "/path/to/scenario_folder/stop_conditions.yaml"
  },
  "sampler": {
    "name": "lhs",
    "config_path": "/path/to/lhs_sampler.yaml"
  }
}
```

The tool computes the scenario base from `scenario.scenario_path` and calls:

```python
load_sampler_spec(runner_spec["sampler"], source_base_path=scenario_base)
```

For bundle output, these source files must exist:

- `{scenario_name}.xosc`
- `spec.yaml`
- `stop_conditions.yaml`

The tool first looks in `scenario.scenario_path`. If `scenario.stop_condition_config_path` is present, it also uses that file and its parent directory as a fallback for `spec.yaml`. Missing files are reported as errors.

## Output

Each shard is a bundle folder:

```text
output/
  sakura_cutin_1-lhs-1000/
    sakura_cutin_1-lhs1/
      sakura_cutin_1.xosc
      explicit_samples.yaml
      spec.yaml
      stop_conditions.yaml
    sakura_cutin_1-lhs2/
      sakura_cutin_1.xosc
      explicit_samples.yaml
      spec.yaml
      stop_conditions.yaml
    manifest.yaml
```

Every bundle uses the same file names. `explicit_samples.yaml` contains that bundle's explicit samples:

```yaml
samples:
  - id: '1'
    params:
      ego_speed: 10.0
      agent_speed: 15.0
  - id: '2'
    params:
      ego_speed: 11.0
```

The exported `params` are `Sample.sim_params`: only simulator-facing parameters are written. Sampler metadata and intermediate sampled parameters are intentionally omitted from bundle explicit files.

Sample ids are strings. When the source sampler returns no id, ids start at `'1'` with no zero padding.

`manifest.yaml` records every bundle:

```yaml
total_samples: 1000
shard_count: 20
shards:
  - bundle_id: 1
    sample_count: 50
    bundle_path: output/sakura_cutin_1-lhs-1000/sakura_cutin_1-lhs1
    sample_file_path: output/sakura_cutin_1-lhs-1000/sakura_cutin_1-lhs1/explicit_samples.yaml
    first_sample_id: '1'
    last_sample_id: '50'
```

When `--zip` is used, the archive contains the generated bundle folders and excludes `manifest.yaml`.

## Runner Use

Give each machine a different bundle directory. Point that run at the bundle's copied `{scenario_name}.xosc`, `spec.yaml`, and `stop_conditions.yaml`, and use the bundle `explicit_samples.yaml` as the explicit sample source.

If your runner invocation expects a sampler config file, create one that points at the bundle:

```yaml
source:
  type: explicit
  path: /path/to/bundle/explicit_samples.yaml
max_samples: null
```

Then use sampler name `explicit` in the runner spec.

## Sample Analysis

`pisa-sample-analyze` creates an offline analysis folder for planned samples, exported samples, or completed runner results.

Supported inputs:

- `--runner-spec`: materializes samples from a runner JSON/YAML spec using `simcore.sampler`.
- `--samples`: reads an `explicit_samples.yaml`, a legacy `explicit.yaml`, a generated bundle output directory, a single bundle directory, or a CSV sample table.
- `--results`: reads a runner output directory containing `iteration_*/monitor/result.csv`.

Analyze planned sampler output:

```bash
uv run pisa-sample-analyze \
  --runner-spec /path/to/runner_spec.json \
  --output analysis/sakura-planned
```

Analyze generated bundles:

```bash
uv run pisa-sample-analyze \
  --samples output/sakura_cutin_1-lhs-1000 \
  --output analysis/sakura-bundles
```

Analyze completed runner results and color points by outcome:

```bash
uv run pisa-sample-analyze \
  --results /home/hcis-s05/ysws/PISA/runner/outputs/carla-esmini-lhs1000 \
  --color-by outcome \
  --post-outcome-config examples/outcome_eval/low_ttc_result.yaml \
  --bins 40 \
  --output analysis/sakura-results
```

The analyzer discovers every parameter and metric in the input. `--params` is optional and only controls the initial X/Y/Z axes shown when the report opens; all discovered parameters remain selectable in `report.html`.
`--bins` controls the default 1D histogram bin count for the static SVG histograms and for the report's dynamic explorer. You can still adjust the 1D bin count directly inside `report.html` without regenerating the report.
With `--post-outcome-config`, the analyzer runs offline outcome evaluation and embeds both original and post-evaluated outcomes. In `report.html`, use `Outcome source` to switch the analysis view, or use `Post Outcome Lab` to draft quick metric/param rules directly in the browser.

Post outcome evaluation has two modes:

- `--post-outcome-mode overlay` keeps the original runner outcome unless the post condition tree triggers. Use this for extra filters such as "mark existing results as fail when `min_ttc < 1.0`". This is the analysis default.
- `--post-outcome-mode replace` treats the post condition tree as the complete outcome definition. Conditions must explicitly produce `success`, `fail`, or `invalid`; records with no triggered condition become `unknown`.

Coloring supports:

- `--color-by none`
- `--color-by outcome`
- `--color-by status`
- `--color-by stop_condition`
- `--color-by param:<name>`
- `--color-by metric:<name>`

Numeric `param:<name>` and `metric:<name>` values use a continuous light-to-dark blue scale instead of discrete class colors. This is useful for trends such as plotting `Ego_Speed` while coloring by `metric:ego_to_agent_1.min_ttc_s`.

CSV sample input can use either explicit prefixes or plain parameter columns:

```csv
sample_id,param.Agent_S,Ego_Speed,outcome,metric.min_ttc
case_1,2970.0,12.5,success,3.1
case_2,2955.0,25.0,fail,0.4
```

Analysis output:

```text
analysis/sakura-results/
  summary.yaml
  samples.csv
  report.html
  figures/
    class_counts.svg
    hist_Agent_S.svg
    hist_Ego_Speed.svg
    scatter_2d.svg
    coverage_heatmap.svg
    pair_matrix.svg
    scatter_3d.html
```

`summary.yaml` contains counts, selected params, per-parameter stats, metric stats, outcomes, statuses, stop conditions, and missing result counts. `samples.csv` is the flattened table with `param.*` and `metric.*` columns. `report.html` is a self-contained static report that links to SVG figures and an interactive vanilla-JS 3D scatter view.

`report.html` also includes a dynamic explorer. It embeds the loaded sample records, so it works offline without a server. In the browser you can:

- choose X, Y, and optional Z parameters from dropdowns
- switch between auto, 1D, 2D, and 3D views
- adjust the 1D histogram bin count
- recolor by outcome, status, stop condition, parameter value, or metric value
- filter visible samples by outcome and status
- click a point to inspect the full sample row
- download the currently filtered rows as CSV

For runner results, the analyzer reads each `iteration_<id>/monitor/result.csv`, parses `run.params` as sample parameters, uses `run.status`, `run.test_outcome`, `run.stop_condition`, and `run.stop_reason` for classification, and treats non-`run.*` columns as summary metrics.

## Trajectory SVGs

`pisa-sample-trajectory` visualizes `agent_state.csv` or `agent_states.csv` files from completed concrete scenarios.

Render one concrete scenario:

```bash
uv run pisa-sample-trajectory \
  --input /path/to/results/iteration_1 \
  --output-dir analysis/trajectories
```

Render every concrete scenario in a runner results folder:

```bash
uv run pisa-sample-trajectory \
  --input /path/to/results \
  --output-dir analysis/trajectories
```

The tool finds `iteration_*/monitor/agent_states.csv` first. It also supports the singular filename `agent_state.csv` and can accept a CSV file directly through `--input`.

Limit the plotted area with x/y ranges:

```bash
uv run pisa-sample-trajectory \
  --input /path/to/results \
  --output-dir analysis/trajectories-window \
  --x-range -20,80 \
  --y-range -10,30
```

Only points inside the requested ranges are drawn. The SVG plot uses equal x/y scaling, so one meter on x occupies the same pixel distance as one meter on y; if the requested range is wide or tall, the plot area is resized inside the SVG instead of stretching the trajectory.

If you prefer filling the plot area even when x/y scale differs, use stretch mode:

```bash
uv run pisa-sample-trajectory \
  --input /path/to/results \
  --output-dir analysis/trajectories-stretched \
  --x-range -20,80 \
  --y-range -10,30 \
  --scale-mode stretch
```

The default is `--scale-mode equal`.

Use an agent's first position as the origin when you want relative coordinates:

```bash
uv run pisa-sample-trajectory \
  --input /path/to/results \
  --output-dir analysis/trajectories-relative \
  --origin-agent-id 1
```

The tool translates every point by that first `agent_id` position before plotting. If you also pass `--ignore-agent-id`, the origin is still computed from the original data first, so you can use an agent as the origin without drawing it.

Each SVG draws `x/y` trajectories for all agents in one concrete scenario:

- every `agent_id` gets a distinct color
- the legend maps color to `agent_id`
- line opacity represents speed; faster segments are darker
- hollow circles mark trajectory starts and filled circles mark ends
- the right side also shows the parameter combination and run result when `monitor/result.csv` is available

Batch output:

```text
analysis/trajectories/
  iteration_1_trajectory.svg
  iteration_2_trajectory.svg
  manifest.yaml
```

Existing output directories are rejected by default. Use `--overwrite` only for directories previously generated by this tool; non-tool directories are refused.

## Trajectory Comparison

`pisa-trajectory-compare` compares the same concrete scenario across two simulator/result sets. It reads `agent_states.csv` from each side, ignores `agent_id == 1` by default, compares overlapping non-ego agents, truncates each agent to the shorter timestep count, and writes metrics plus overlaid trajectory SVGs.

Compare one concrete scenario:

```bash
uv run pisa-trajectory-compare \
  --left /path/to/carla-carla-lhs1234/iteration_1 \
  --right /path/to/carla-esmini-lhs1234/iteration_1 \
  --left-label carla \
  --right-label esmini \
  --output-dir analysis/trajectory-compare-one
```

Compare every shared parameter combination in a logical scenario result folder:

```bash
uv run pisa-trajectory-compare \
  --left /path/to/carla-carla-lhs1234 \
  --right /path/to/carla-esmini-lhs1234 \
  --left-label carla \
  --right-label esmini \
  --output-dir analysis/trajectory-compare
```

The batch mode pairs shared `iteration_*` directories by name. If one result set has extra iterations, those are skipped because there is no matching parameter combination to compare.

Metrics:

- `ADE`: average displacement error across compared timesteps
- `FDE`: final displacement error at the last compared timestep
- `RMSE`: root mean square position error
- `max_error`: largest timestep position error
- `mean_speed_delta`: average absolute speed difference

Each comparison SVG overlays both trajectories in one plot: solid lines for the left result set and dashed lines for the right result set. Thin dark connector lines show the matched timesteps used for error metrics; those straight segments are not trajectories. The default `--scale-mode equal` preserves the same x/y scale used by `pisa-sample-trajectory`; use `--scale-mode stretch` only when you want the plot stretched to fill the available area. The side panel lists per-agent metrics, ignored agents, sample params, and compact run results when `monitor/result.csv` is available.

Output:

```text
analysis/trajectory-compare/
  iteration_1_comparison.svg
  iteration_2_comparison.svg
  summary.csv
  manifest.yaml
```

`summary.csv` contains per-agent metrics for downstream analysis. `manifest.yaml` contains overall metrics and one entry per comparison. Existing output directories are rejected by default; `--overwrite` only replaces directories previously generated by `pisa-trajectory-compare`.

## Offline Outcome Evaluation

`pisa-outcome-eval` evaluates a new condition tree after a scenario has already run. It reads completed runner monitor logs and produces a new analysis outcome without rerunning the simulator.

The base version supports offline leaf conditions for thresholds, expressions, and agent-pair comparisons:

- `agent_state_threshold`: reads `monitor/agent_states.csv`, filters one `agent_id`, and checks a column such as `x`, `y`, `speed`, or `z`.
- `frame_metric_threshold`: reads `monitor/frame_metrics.csv` and checks a per-frame metric such as `ego_to_agent_1.ttc_s`.
- `result_metric_threshold`: reads `monitor/result.csv` and checks summary metrics such as `ego_to_agent_1.min_ttc_s`.
- `agent_state_expression`, `frame_metric_expression`, `result_metric_expression`: evaluate runner-style numeric expressions over CSV row values.
- `agent_pair_expression`: compares two agents from `agent_states.csv` on shared timesteps.

The config intentionally looks like runner `stop_conditions.yaml`: a top-level list is treated as OR, and each triggering condition can set `outcome: Success`, `Fail`, or `Invalid`. Numeric rules reuse runner `simcore.metrics.rules.NumericRule`; expressions reuse runner `simcore.metrics.expressions.evaluate_numeric_expression`; `and`/`or` nodes reuse runner logical condition nodes.

Example: mark a completed run as failed if any frame has TTC below 1.0 second:

```yaml
- type: frame_metric_threshold
  name: low_ttc_reanalysis
  outcome: Fail
  metric: ego_to_agent_1.ttc_s
  rule: lt
  value: 1.0
```

Example: mark a run invalid if agent `0` ever leaves an x range:

```yaml
condition:
  type: agent_state_threshold
  name: agent_0_x_out_of_range
  outcome: Invalid
  agent_id: 0
  metric: x
  rule: outside
  values: [-20, 120]
```

Example: use a summary metric from `result.csv`:

```yaml
condition:
  type: result_metric_threshold
  name: low_summary_ttc
  outcome: Fail
  metric: ego_to_agent_1.min_ttc_s
  rule: lt
  value: 1.0
```

See reusable YAML examples in [`examples/outcome_eval`](examples/outcome_eval).

Run it on one concrete scenario:

```bash
uv run pisa-outcome-eval \
  --input /path/to/results/iteration_1 \
  --config analysis_conditions.yaml \
  --output-dir analysis/outcomes-one
```

Run it on every `iteration_*` under a logical scenario result folder:

```bash
uv run pisa-outcome-eval \
  --input /path/to/results \
  --config analysis_conditions.yaml \
  --output-dir analysis/outcomes
```

Output:

```text
analysis/outcomes/
  offline_outcomes.csv
  manifest.yaml
```

By default this does not change the original runner logs. Add `--write-monitor-outcome` to also write `monitor/offline_outcome.csv` beside each evaluated `result.csv`. This creates a separate analysis outcome file with `run.analysis_test_outcome`, `run.analysis_stop_condition`, and `run.analysis_stop_reason`; it does not overwrite the original `run.test_outcome` columns.

Supported operators:

- `<`, `<=`, `>`, `>=`, `==`
- aliases from runner `NumericRule`, including `lt`, `le`, `gt`, `ge`, `eq`
- `between` with `values: [min, max]`
- `outside` or `out_of_range` with `values: [min, max]`

For frame-like CSVs, conditions default to `aggregation: any`. You can also use `all`, `min`, `max`, `first`, or `last`.

If a condition references a required file or column that is not present, the tool fails instead of silently treating the condition as false.

## Why Separate

The tool is independent of the simcore runtime loop: it imports only the sampler APIs, produces portable explicit YAML files, and does not start the simulator, AV stack, engine gRPC loop, or result handling. That makes sample generation deterministic, inspectable, and easy to parallelize before execution.
