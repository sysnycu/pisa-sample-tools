# Trajectory Comparison

Command: `pisa-trajectory-compare`

Compare the same concrete scenario across two simulator/result sets. The tool ignores `agent_id == 1` by default, compares overlapping non-ego agents, truncates to the shorter timestep count, and computes ADE/FDE/RMSE/max error/speed delta.

Single comparison:

```bash
uv run pisa-trajectory-compare \
  --left /path/to/carla-carla-lhs1234/iteration_1 \
  --right /path/to/carla-esmini-lhs1234/iteration_1 \
  --left-label carla \
  --right-label esmini \
  --output-dir analysis/trajectory-compare-one
```

Batch comparison:

```bash
uv run pisa-trajectory-compare \
  --left /path/to/carla-carla-lhs1234 \
  --right /path/to/carla-esmini-lhs1234 \
  --output-dir analysis/trajectory-compare
```

Output includes one SVG per matched `iteration_*`, `summary.csv`, and `manifest.yaml`.

