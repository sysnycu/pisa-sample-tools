# PISA Experiment Runner

`pisa-experiment-runner` is the standalone local execution UI. It does not share routes,
state, or frontend code with `pisa-analysis builder`.

## Start

```bash
uv run pisa-experiment-runner
```

The server binds to loopback and opens a token-protected browser session. Use `--no-open`,
`--port`, `--config`, and `--local-config` to override startup behavior.

## Configuration

The editable registry is `config/experiment_runner.yaml`. Machine-only changes belong in
`config/experiment_runner.local.yaml`, which remains ignored by Git. Both files deep-merge
over the bundled defaults. Environment variables override registry variables during path
expansion.

```yaml
version: 1
variables:
  PISA_ROOT: /path/to/PISA
  PISA_DATA_DIR: /opt/sbsvf
components:
  my-av:
    kind: av
    image: my-av:latest
    build:
      repo_path: ${PISA_ROOT}/my-av
      context: ${PISA_ROOT}/my-av
      builder: docker build
    run:
      network: bridge
      ports: {service: auto}
      env: {}
      mounts: []
experiments:
  demo:
    simulator: {component: esmini, config_path: /path/to/sim.yaml}
    av: {component: my-av, config_path: /path/to/av.yaml}
    map: {name: map, xodr_path: /path/to/xodr, osm_path: /path/to/osm}
    scenario: {name: demo, path: /path/to/scenario, format: open_scenario1}
    sampler: {name: lhs, config_path: /path/to/lhs.yaml}
    monitor: {config_path: /path/to/monitor.yaml}
    runner:
      repo_path: ${PISA_ROOT}/runner
      command: [uv, run, python, main.py, --runner_spec, "{runner_spec}"]
    task: {output_dir: /path/to/results}
```

Build settings support `docker build` and `docker buildx build`, Dockerfile, target,
platform, build arguments, pull, no-cache, load, and extra argv. Runtime settings support
entrypoint/args, environment, mounts, user, bridge/host networking, GPU/runtime, ROS domain,
service/CARLA/TrafficManager ports, log driver, and startup timeout.

## Execution lifecycle

- **Run All** builds missing images, starts AV and simulator, runs sim-core, and cleans up.
- **Build**, **Start**, **Run**, and **Stop** expose the same stages independently.
- Only one job runs at a time; additional jobs remain queued.
- Cancel terminates the runner process group and then stops owned containers.
- Containers are labeled with `pisa.experiment-runner=true`; stale-resource cleanup refuses
  containers without that label.
- The preflight checks Docker, source/config/data paths, scenario files, mounts, fixed ports,
  and output ownership before execution.

The main screen edits presets through separate metadata, component, scenario, map,
sampler/monitor, runtime, runner, and report sections. Selecting a simulator or AV applies its
profile's default config path. The advanced experiment JSON and complete component registry are
still available for uncommon wrapper-specific settings.

Select the scenario directory before editing its individual fields. A scenario directory should
contain its primary `<scenario-name>.xosc`, `spec.yaml`, and either `stop_conditions.yaml` or
`stop_condition.yaml`. The folder inspector reads `scenario_name`, `map_name`, `ego.position`, and
`ego.target_speed` from `spec.yaml`, fills the form and derived map directories, and identifies any
fields that still need manual input. `param.xosc` is not treated as the primary scenario file.

The sampler selector exposes `grid`, `native`, `lhs`, `sobol`, `random`, `feedback_boundary`, and
`explicit_sample`. The last option is emitted as the runner's built-in sampler name `explicit`.

Presets support creation from a template, duplication, rename, deletion, display labels, and
searchable tags. A newly created preset receives output/report paths derived from its preset ID.

The generated `runner_spec.json` and `resolved_experiment.yaml` are stored in the result root.
Existing runner outputs with an `execution_manifest.yaml` are accepted for resume; unrelated
non-empty output directories are rejected.

If a previous attempt stopped after a wrapper wrote logs but before sim-core created its manifest,
review the directory and select **Adopt reviewed non-empty output for this run**. This confirmation
is intentionally not saved in the preset. Once accepted, the runner writes
`.pisa-experiment-runner.yaml` before starting any new containers, so later retries are recognized
automatically.

## Reports

After execution, **Generate Report** invokes the existing evidence build API using the selected
analysis spec and a separate report output directory. Automatic report generation is opt-in per
preset. The runner only serves the resulting offline report; campaign authoring and report
library management remain in the separate Report Builder.
