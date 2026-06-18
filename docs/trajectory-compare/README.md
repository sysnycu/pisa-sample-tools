# Trajectory Comparison

Command: `pisa-trajectory-compare`

Compare the same concrete scenario across two simulator/result sets. The tool ignores `agent_id == 1` by default, compares overlapping non-ego agents, truncates to the shorter timestep count, and computes ADE/FDE/RMSE/max error/speed delta.

Each SVG overlays both trajectories in one plot. The left result set is solid, the right result set is dashed, and thin dark connector lines show matched timesteps used for the error metrics. Those connector lines are straight by design and are not trajectories. The default `--scale-mode equal` preserves the same x/y scale used by `pisa-sample-trajectory`; use `--scale-mode stretch` to fill the plot area.

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
