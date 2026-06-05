# Offline Outcome Evaluation

Command: `pisa-outcome-eval`

Evaluate a new condition tree after a scenario has already run. The tool reads completed runner monitor logs and produces a new analysis outcome without rerunning the simulator.

```bash
uv run pisa-outcome-eval \
  --input /path/to/results \
  --config examples/outcome_eval/low_ttc_result.yaml \
  --mode replace \
  --output-dir analysis/outcomes
```

By default this does not change original runner logs. Add `--write-monitor-outcome` to create `monitor/offline_outcome.csv` next to each evaluated `result.csv`.

Evaluation modes:

- `replace` fully re-evaluates the scenario outcome from the new condition tree. This is the command default. If no condition triggers, the outcome is `unknown` unless `--default-outcome` is provided.
- `overlay` starts from the original `run.test_outcome` in `result.csv` and only changes it when the new condition tree triggers. Use this for extra filters such as "also fail when `min_ttc < 1.0`".

Supported leaf conditions:

- `agent_state_threshold`: check a column in `agent_states.csv` for selected agent ids.
- `frame_metric_threshold`: check a column in `frame_metrics.csv`.
- `result_metric_threshold`: check a summary metric in `result.csv`.
- `agent_state_expression`, `frame_metric_expression`, `result_metric_expression`: evaluate runner-style numeric expressions on CSV row values.
- `agent_pair_expression`: compare two agents from `agent_states.csv` on shared timesteps.

Rule parsing uses runner `simcore.metrics.rules.NumericRule`; expression parsing uses runner `simcore.metrics.expressions.evaluate_numeric_expression`; `and`/`or` nodes reuse runner logical condition nodes.

Example configs are in [`examples/outcome_eval`](../../examples/outcome_eval).
