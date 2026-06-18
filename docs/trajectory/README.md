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
- `--ignore-agent-id 1`
- `--origin-agent-id 1`
- `--overwrite`

`--origin-agent-id` translates every point so the selected agent's first position becomes `x=0,y=0`. The origin is computed before ignored agents are removed, so an agent can define the origin without appearing in the SVG.

Batch mode writes one SVG per `iteration_*` plus `manifest.yaml`.
