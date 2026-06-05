# Trajectory SVGs

Command: `pisa-sample-trajectory`

Render `agent_state.csv` or `agent_states.csv` from completed runner outputs.

```bash
uv run pisa-sample-trajectory \
  --input /path/to/results \
  --output-dir analysis/trajectories
```

Useful options:

- `--x-range -20,80`
- `--y-range -10,30`
- `--scale-mode equal|stretch`
- `--overwrite`

Batch mode writes one SVG per `iteration_*` plus `manifest.yaml`.

