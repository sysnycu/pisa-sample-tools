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

## Why Separate

The tool is independent of the simcore runtime loop: it imports only the sampler APIs, produces portable explicit YAML files, and does not start the simulator, AV stack, engine gRPC loop, or result handling. That makes sample generation deterministic, inspectable, and easy to parallelize before execution.
