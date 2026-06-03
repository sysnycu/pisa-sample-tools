# pisa-sample-tools

Offline sampling and sharding tools for PISA runner sampler specs.

This project reuses the runner package `simcore` sampler API to materialize every sample from one logical scenario, then writes bundle folders that can be distributed across machines.

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

This repo provides three commands:

- `pisa-sample-test`: preview raw sampler output from one sampler source file.
- `pisa-sample-export`: materialize samples and split them into runner-ready bundle folders.
- `pisa-sample-analyze`: inspect planned samples, generated explicit samples, or completed runner results.

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
      explicit.yaml
      spec.yaml
      stop_conditions.yaml
    sakura_cutin_1-lhs2/
      sakura_cutin_1.xosc
      explicit.yaml
      spec.yaml
      stop_conditions.yaml
    manifest.yaml
```

Every bundle uses the same file names. `explicit.yaml` contains that bundle's explicit samples:

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

Sample ids are strings. When the source sampler returns no id, ids start at `'1'` with no zero padding.

`manifest.yaml` records every bundle:

```yaml
total_samples: 1000
shard_count: 20
shards:
  - bundle_id: 1
    sample_count: 50
    bundle_path: output/sakura_cutin_1-lhs-1000/sakura_cutin_1-lhs1
    sample_file_path: output/sakura_cutin_1-lhs-1000/sakura_cutin_1-lhs1/explicit.yaml
    first_sample_id: '1'
    last_sample_id: '50'
```

When `--zip` is used, the archive contains the generated bundle folders and excludes `manifest.yaml`.

## Runner Use

Give each machine a different bundle directory. Point that run at the bundle's copied `{scenario_name}.xosc`, `spec.yaml`, and `stop_conditions.yaml`, and use the bundle `explicit.yaml` as the explicit sample source.

If your runner invocation expects a sampler config file, create one that points at the bundle:

```yaml
source:
  type: explicit
  path: /path/to/bundle/explicit.yaml
max_samples: null
```

Then use sampler name `explicit` in the runner spec.

## Sample Analysis

`pisa-sample-analyze` creates an offline analysis folder for planned samples, exported samples, or completed runner results.

Supported inputs:

- `--runner-spec`: materializes samples from a runner JSON/YAML spec using `simcore.sampler`.
- `--samples`: reads an `explicit.yaml`, a generated bundle output directory, a single bundle directory, or a CSV sample table.
- `--results`: reads a runner output directory containing `iteration_*/monitor/result.csv`.

Analyze planned sampler output:

```bash
uv run pisa-sample-analyze \
  --runner-spec /path/to/runner_spec.json \
  --params Agent_S,Ego_Speed,Agent_Cutin_Distance \
  --output analysis/sakura-planned
```

Analyze generated bundles:

```bash
uv run pisa-sample-analyze \
  --samples output/sakura_cutin_1-lhs-1000 \
  --params Agent_S,Ego_Speed \
  --output analysis/sakura-bundles
```

Analyze completed runner results and color points by outcome:

```bash
uv run pisa-sample-analyze \
  --results /home/hcis-s05/ysws/PISA/runner/outputs/carla-esmini-lhs1000 \
  --params Agent_S,Ego_Speed,Agent_Cutin_Distance \
  --color-by outcome \
  --output analysis/sakura-results
```

`--params` accepts at most 3 parameters. If omitted, the tool auto-selects up to 3 numeric parameters.

Coloring supports:

- `--color-by none`
- `--color-by outcome`
- `--color-by status`
- `--color-by stop_condition`
- `--color-by param:<name>`
- `--color-by metric:<name>`

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
- recolor by outcome, status, stop condition, parameter value, or metric value
- filter visible samples by outcome and status
- click a point to inspect the full sample row
- download the currently filtered rows as CSV

For runner results, the analyzer reads each `iteration_<id>/monitor/result.csv`, parses `run.params` as sample parameters, uses `run.status`, `run.test_outcome`, `run.stop_condition`, and `run.stop_reason` for classification, and treats non-`run.*` columns as summary metrics.

## Why Separate

The tool is independent of the simcore runtime loop: it imports only the sampler APIs, produces portable explicit YAML files, and does not start the simulator, AV stack, engine gRPC loop, or result handling. That makes sample generation deterministic, inspectable, and easy to parallelize before execution.
