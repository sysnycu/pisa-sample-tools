# Sample Export

Command: `pisa-sample-export`

Materialize all samples from a runner sampler config, then split them into runner-ready bundle folders.

```bash
uv run pisa-sample-export \
  --runner-spec /path/to/runner_spec.yaml \
  --output-dir output/sakura_cutin-lhs-1000 \
  --shard-size 50
```

Alternative split mode:

```bash
uv run pisa-sample-export \
  --runner-spec /path/to/runner_spec.yaml \
  --num-shards 20
```

Each generated bundle contains:

- `{scenario_name}.xosc`
- `explicit_samples.yaml`
- `spec.yaml`
- `stop_conditions.yaml`

`explicit_samples.yaml` writes only `Sample.sim_params`, so simulator-facing params are kept while sampler metadata and intermediate sampled params are omitted.

Useful options:

- `--overwrite`
- `--dry-run`
- `--summary json`
- `--zip`
