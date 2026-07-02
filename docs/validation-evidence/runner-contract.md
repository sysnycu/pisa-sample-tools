# Runner Execution-Data Contract

## Responsibility Boundary

The runner is an execution engine. It should:

- execute concrete scenarios;
- record what was sent to and received from execution components;
- record timing, status, outcomes, parameters, metrics, traces, controls, and events;
- preserve enough provenance to reproduce or audit one execution.

The runner should not:

- decide which result roots form an experiment campaign;
- assign scientific comparison groups;
- decide whether an AV comparison is fair;
- define safe, near-critical, failure, or representative cases;
- choose plot axes, bins, thresholds, or paper figures;
- know how results will be used in a thesis.

Those concerns belong to `pisa-analysis-tools` through `analysis_spec.yaml` and
`analysis_campaign.yaml`.

## Data Requested From Runner

### Already available

The current runner already provides most required information:

```text
iteration_*/monitor/result.csv
iteration_*/monitor/frame_metrics.csv
iteration_*/monitor/agent_states.csv
iteration_*/monitor/agent_geometry.csv
iteration_*/monitor/collision_events.csv
iteration_*/monitor/scenario_events.csv
```

`result.csv` should keep these stable fields:

```text
run.status
run.test_outcome
run.stop_condition
run.stop_reason
run.total_steps
run.final_sim_time_ms
run.wall_time_ms
run.speedup
run.job_id
run.params
```

### Missing but useful

Two generic additions are recommended:

1. `execution_manifest.yaml`: execution provenance for the result root.
2. `control_commands.csv`: the commands actually returned by the AV and passed to the simulator.

Neither addition contains analysis policy.

## Execution Manifest

Write the following at `task.output_dir`:

```text
execution_manifest.yaml
```

This manifest describes execution facts, not experiment semantics:

```yaml
schema_version: 1
execution_id: 4e8f2680-7c72-4f34-8a23-42f5acf7ab2e
created_at: "2026-06-15T12:00:00Z"
completed_at: "2026-06-15T13:30:00Z"
dt: 0.05
seed: 7
scenario_name: sakura_cutin
runner_version: 8f13c2a
pisa_api_version: 31ac880
runner_spec_sha256: "..."
resolved_inputs:
  runner_spec: /absolute/path/spec.json
  scenario: /absolute/path/scenario
  simulator_config: /absolute/path/esmini.yaml
  av_config: /absolute/path/carla_agent.yaml
  sampler_config: /absolute/path/lhs_100.yaml
  monitor_config: /absolute/path/logging.yaml
  stop_conditions: /absolute/path/stop_conditions.yaml
execution:
  job_id: "0"
  permutation: null
  overwrite: true
  max_concrete_retries: 3
software:
  python: 3.14.3
  platform: linux
summary:
  finished: 79
  failed: 12
  skipped: 0
  aborted: 0
metadata:
  map_name: straight_3000m
  ego_agent_id: 0
```

Appropriate runner provenance:

- effective `dt`;
- effective random seed when the runner or sampler uses one;
- resolved input paths and optionally content hashes;
- package/git versions;
- timestamps and platform;
- job/permutation/retry settings;
- logical execution counts;
- raw component config references.

Inappropriate runner metadata:

- `av_comparison_group`;
- `paper_baseline`;
- `representative_case`;
- `near_critical_threshold`;
- `sampler_rank`;
- thesis figure names.

The analyzer accepts legacy `experiment_manifest.yaml`, but new runner work should use
`execution_manifest.yaml`.

### Suggested implementation

Create:

```text
simcore/execution_manifest.py
```

Suggested API:

```python
def build_execution_manifest(
    spec: dict,
    *,
    output_base: Path,
    resolved_inputs: dict[str, Path | None],
) -> dict:
    ...


def validate_existing_manifest(existing: dict, expected: dict) -> None:
    ...


def write_execution_manifest(path: Path, manifest: dict) -> None:
    temporary = path.with_suffix(".yaml.tmp")
    temporary.write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def finalize_execution_manifest(
    path: Path,
    *,
    result: ExecResult,
    monitor_counts: dict,
) -> None:
    ...
```

In `SimulationEngine.__init__`:

1. Preserve the raw/resolved execution specs.
2. Create `output_base`.
3. Build and atomically write the initial manifest.
4. If a manifest already exists, reject incompatible execution facts rather than silently
   replacing provenance because `runtime.overwrite` is enabled.

In `SimulationEngine.exec()` or its finalization path:

1. update `completed_at`;
2. record terminal counts;
3. atomically rewrite the manifest.

`execution_id` should identify one output-root execution lineage. Reusing the same output root
for a restart may retain it; starting a separate repeated run should use a separate output root
and execution ID. Analysis-side `repeat_id` is assigned later in `analysis_campaign.yaml`.

## Control Commands

`AVWrapper.reset()` and `AVWrapper.step()` already return:

```proto
message CtrlCmd {
  CtrlMode mode = 1;
  google.protobuf.Struct payload = 2;
}
```

`SimulationEngine.run_concrete()` already passes that same value to:

```python
self.monitor.update(sim_time_ns, runtime_frame, ctrl_for_sim)
```

`MonitorSample.control` therefore already contains the command. No engine-loop or AV/simulator
API change is required.

### Output

Add:

```text
iteration_*/monitor/control_commands.csv
```

Fixed schema:

```text
step_index
sim_time_ms
control_type
throttle
brake
steer
speed
acceleration
steering_angle
steering_angle_velocity
jerk
payload_json
```

Rules:

- one row per configured monitor sample;
- `control_type` is the protobuf mode name in lowercase;
- known numeric fields are flattened;
- unavailable fields are empty;
- `payload_json` always preserves the complete payload;
- unknown fields or future control modes do not stop execution;
- logging never mutates the command or changes stop behavior.

The protobuf currently spells one mode `THROTTLE_STEER_BREAK`. Do not rename it as part of
logging. The recorder may normalize payload keys `break` or `brake` into output column `brake`.

### Recorder implementation

Create:

```text
simcore/monitoring/recorders/control_commands.py
```

Core structure:

```python
from __future__ import annotations

import json
import math
from typing import Any

from google.protobuf.json_format import MessageToDict
from pisa_api import control_pb2

from simcore.monitoring.log_manager import LogStream
from simcore.monitoring.sample import LogRow, MonitorSample

from .base import Recorder

FIELDS = (
    "step_index",
    "sim_time_ms",
    "control_type",
    "throttle",
    "brake",
    "steer",
    "speed",
    "acceleration",
    "steering_angle",
    "steering_angle_velocity",
    "jerk",
    "payload_json",
)

ALIASES = {
    "brake": ("brake", "break"),
    "steer": ("steer", "steering"),
    "steering_angle": ("steering_angle", "steeringAngle"),
    "steering_angle_velocity": (
        "steering_angle_velocity",
        "steeringAngleVelocity",
    ),
}


class ControlCommandsRecorder(Recorder):
    def streams(self) -> list[LogStream]:
        return [
            LogStream(
                name=self.name,
                filename=self.output,
                fields=FIELDS,
            )
        ]

    def record(self, sample: MonitorSample) -> list[LogRow]:
        mode, payload = control_parts(sample.control)
        row = {
            "step_index": sample.step_index,
            "sim_time_ms": sample.sim_time_ms,
            "control_type": mode,
            "payload_json": json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        for field in FIELDS:
            if field in row:
                continue
            row[field] = first_finite(
                payload,
                ALIASES.get(field, (field,)),
            )
        return [LogRow(stream=self.name, row=row)]


def control_parts(control: Any) -> tuple[str, dict[str, Any]]:
    if control is None:
        return "none", {}
    if hasattr(control, "mode") and hasattr(control, "payload"):
        try:
            mode = control_pb2.CtrlMode.Name(int(control.mode)).lower()
        except (TypeError, ValueError):
            mode = str(control.mode).lower()
        payload = MessageToDict(
            control.payload,
            preserving_proto_field_name=True,
        )
        return mode, payload if isinstance(payload, dict) else {}
    return "unknown", {}


def first_finite(
    payload: dict[str, Any],
    aliases: tuple[str, ...],
) -> float | None:
    for name in aliases:
        try:
            value = float(payload[name])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value
    return None
```

Register it in `simcore/monitoring/recorder_registry.py`:

```python
"control_commands": (
    "simcore.monitoring.recorders.control_commands:"
    "ControlCommandsRecorder"
),
```

Enable it through normal runner logging configuration:

```yaml
logging:
  tables:
    - type: control_commands
      name: control_commands
      output: control_commands.csv
      every_n_steps: 1
```

This is a generic execution trace recorder. It does not know which plots the analyzer will
produce.

## Metric Streams

Runner monitor configuration should record metrics because they are execution observations, not
because a specific plot requests them. A cut-in configuration may include:

```yaml
logging:
  frame:
    every_n_steps: 1
    output: frame_metrics.csv
    recorders:
      - type: ego_state
        name: ego

      - type: pair_ttc
        name: ego_to_agent_1
        actor_id_a: 0
        actor_id_b: 1
        mode: longitudinal
        lateral_threshold_m: 2.0
        fields:
          - distance_m
          - closing_speed_mps
          - ttc_s
          - longitudinal_distance_m
          - lateral_distance_m

  tables:
    - type: agent_states
      name: agent_states
      output: agent_states.csv

    - type: agent_geometry
      name: agent_geometry
      output: agent_geometry.csv
      once: true

    - type: collision_events
      name: collision_events
      output: collision_events.csv
      actor_id_a: 0

    - type: scenario_events
      name: scenario_events
      output: scenario_events.csv

    - type: control_commands
      name: control_commands
      output: control_commands.csv

  summary:
    include_basic: true
    output: result.csv
    recorders:
      - type: collision
        name: ego_collision
        actor_id_a: 0

      - type: min_ttc
        name: ego_to_agent_1
        actor_id_a: 0
        actor_id_b: 1

      - type: numeric_summary
        name: ego_to_agent_1_distance
        source:
          type: pair_ttc
          field: distance_m
          actor_id_a: 0
          actor_id_b: 1
        aggregations: [min, mean]
        include_extrema_location: true

      - type: numeric_summary
        name: ego_deceleration
        source:
          type: kinematic
          actor_id: 0
          field: acceleration
        transforms: [negate, positive_part]
        aggregations: [max, mean, std]
        include_extrema_location: true
```

The runner owns accurate recording and metric calculation. The analyzer owns which of these
fields become official figures, tables, comparisons, and representative cases.

### Agent geometry

`agent_geometry.csv` should record static geometry once per concrete run unless an actor
shape can change during execution. The analyzer expects these columns when available:

```text
step_index
sim_time_ms
agent_id
sim_tracking_id
entity_name
is_ego
shape_type
length_m
width_m
height_m
reference_point
center_offset_x
center_offset_y
center_offset_z
roll_offset
pitch_offset
yaw_offset
footprint_json
source
```

Use `source` to distinguish simulator runtime geometry from defaults or spec-provided
geometry. Keep per-frame pose in `agent_states.csv`; do not repeat static dimensions on
every frame unless they actually change.

The analyzer joins geometry and pose by `agent_id`, uses the latest geometry row at or
before each frame, and applies center/yaw offsets before drawing the oriented footprint.
`entity_name` is a display label; stable comparisons continue to use `agent_id`.

### Metric applicability status

Numeric frame metrics may provide companion `<metric>_valid` and `<metric>_status`
columns. An empty numeric value with an explanatory status such as
`outside_lateral_threshold`, `non_closing`, or `not_ahead` is not missing data. The
analyzer reports these frames as not applicable and excludes them from numeric
aggregation. A `valid` status without a numeric value, an unknown status, or a blank
value without status is reported as a data-quality problem.

### Collision event position

`collision_events.csv` should keep the existing collision pair and timing fields and may
include an estimated contact position:

```text
step_index
sim_time_ms
actor_a
actor_b
x
y
z
position_source
contact_region_json
```

`position_source` should make the provenance explicit:

```text
collision                 # simulator/proto provided a direct position
derived_bbox_overlap      # bbox overlap centroid
derived_bbox_closest      # bbox closest-point fallback
actor_midpoint            # actor center midpoint fallback
unavailable
```

When bbox overlap exists, `contact_region_json` should contain the overlap polygon
vertices. `scenario_events.csv` collision rows should carry the same
`contact_region_json` when available so event timelines can preserve the contact region.

The analyzer copies `agent_geometry.csv`, `collision_events.csv`, and
`scenario_events.csv` into normalized summary tables while retaining
`position_source` and `contact_region_json`.

Metric fields are bound to analysis concepts such as `min_ttc` in the analysis spec:

```yaml
metrics:
  min_ttc:
    summary: ego_to_agent_1.min_ttc_s
    series: ego_to_agent_1.ttc_s
  min_distance:
    summary: ego_to_agent_1_distance.min
    series: ego_to_agent_1.distance_m
```

## Analysis Campaign

Comparison metadata is supplied outside the runner:

```yaml
version: 1
datasets:
  - id: carla-behavior-r1
    results: /outputs/carla-behavior-r1
    logical_scenario_name: cut-in
    labels:
      simulator: CARLA
      av: Behavior Agent
      sampler: Grid
    grouping:
      seed: 7
      repeat_id: 1

  - id: carla-autoware-r1
    results: /outputs/carla-autoware-r1
    logical_scenario_name: cut-in
    labels:
      simulator: CARLA
      av: Autoware
      sampler: Grid
    grouping:
      seed: 7
      repeat_id: 1
```

Run:

```bash
uv run pisa-analysis compare \
  --campaign analysis_campaign.yaml \
  --spec analysis_spec.yaml \
  --output analysis/comparison
```

## Tests

### Runner execution-manifest tests

1. Manifest contains only execution provenance.
2. Effective `dt`, seed, paths, hashes, versions, and timestamps are correct.
3. Existing compatible manifest supports restart.
4. Existing incompatible manifest is rejected.
5. Atomic write never leaves a partially written final manifest.
6. Finalization updates counts without deleting initial provenance.

### Control recorder tests

1. `NONE` writes empty numeric fields.
2. `THROTTLE_STEER` writes throttle and steer.
3. `THROTTLE_STEER_BREAK` maps braking to `brake`.
4. `ACKERMANN` writes speed, acceleration, and steering fields.
5. Unknown payload fields remain in `payload_json`.
6. Missing payload does not crash.
7. `every_n_steps` works.
8. Reset starts a fresh concrete-run file.
9. Logging does not mutate the protobuf command.

### End-to-end acceptance

For a small runner output:

```text
execution_manifest.yaml
iteration_1/monitor/result.csv
iteration_1/monitor/frame_metrics.csv
iteration_1/monitor/agent_states.csv
iteration_1/monitor/control_commands.csv
iteration_1/monitor/collision_events.csv   # when applicable
```

The analyzer should then report:

- no missing execution-provenance warning;
- control plots for selected cases;
- consistent final trace and summary simulation times;
- no missing configured metrics.

## Implementation Order

1. Add `execution_manifest.yaml`.
2. Add manifest validation and atomic-write tests.
3. Add the generic control recorder.
4. Add control-mode tests.
5. Enable existing frame/table/summary recorders in runner configs.
6. Define analysis grouping separately in `analysis_campaign.yaml`.
