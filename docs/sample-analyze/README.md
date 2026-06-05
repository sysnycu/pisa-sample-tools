# Sample Analysis

Command: `pisa-sample-analyze`

Analyze planned samples, generated explicit samples, or completed runner result folders.

```bash
uv run pisa-sample-analyze \
  --results /path/to/results \
  --color-by outcome \
  --post-outcome-config examples/outcome_eval/low_ttc_result.yaml \
  --bins 40 \
  --output analysis/sakura-results
```

Inputs:

- `--runner-spec /path/to/runner_spec.yaml`
- `--samples /path/to/explicit_samples.yaml_or_bundle_root`

Legacy `explicit.yaml` files are still accepted for older exports.
- `--results /path/to/runner/outputs/name`

The generated `report.html` works offline and lets you switch X/Y/Z params, color-by fields, filters, and 1D histogram bin count interactively.

Coloring supports `outcome`, `status`, `stop_condition`, `param:<name>`, and `metric:<name>`.

## Post Outcome Evaluation

When analyzing completed runner results, add `--post-outcome-config` to run an offline outcome condition tree before writing the report:

```bash
uv run pisa-sample-analyze \
  --results /path/to/results \
  --post-outcome-config examples/outcome_eval/low_ttc_result.yaml \
  --post-outcome-mode overlay \
  --output analysis/with-post-outcome
```

The report embeds both original runner outcomes and post-evaluated outcomes. Use the `Outcome source` selector in `report.html` to switch plots, filters, and `outcome` coloring between:

- `Original`
- `Post Eval`
- `Lab Draft`

Post outcome modes:

- `overlay` keeps the original runner outcome unless the post condition tree triggers. This is useful for extra failure filters, such as marking `min_ttc < 1.0` as `fail` while preserving all other original outcomes. This is the analyzer default.
- `replace` treats the post condition tree as the full outcome definition. Write explicit conditions for `success`, `fail`, and `invalid`; records with no triggered condition are shown as `unknown`.

The page also includes a `Post Outcome Lab` for fast what-if analysis over loaded `result.csv` summary metrics and params. It lets you choose a metric/param, operator, threshold, target outcome, and condition name, then recomputes a draft outcome in the browser without rerunning the CLI.

Full offline condition trees that need `frame_metrics.csv` or `agent_states.csv` should still be provided through `--post-outcome-config`, because those large raw CSV logs are evaluated in Python and are not embedded into the HTML.
