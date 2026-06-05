# Sampler Preview

Command: `pisa-sample-test`

Use this for quick sampler smoke tests. It reads one sampler source file, builds a `simcore.sampler`, and prints generated params without creating bundle output.

```bash
uv run pisa-sample-test /path/to/params.yaml --max-samples 10
```

Useful options:

- `--method grid|lhs|sobol|explicit|native`
- `--config-path /path/to/sampler.yaml`
- `--n-samples 100`
- `--seed 1234`
- `--format table|yaml|json`

