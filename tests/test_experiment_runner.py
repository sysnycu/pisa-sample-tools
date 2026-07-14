from __future__ import annotations

import json
from pathlib import Path

import yaml

from pisa_sample_tools.experiment_runner.commands import build_command, docker_run_command
from pisa_sample_tools.experiment_runner.config import (
    ConfigStore,
    deep_merge,
    resolve_experiment,
    resolve_registry,
)
from pisa_sample_tools.experiment_runner.orchestrator import (
    ExperimentJob,
    validate_experiment,
    write_ownership_marker,
)
from pisa_sample_tools.experiment_runner.scenario import inspect_scenario_directory
from pisa_sample_tools.experiment_runner.server import create_app
from pisa_sample_tools.experiment_runner.spec import build_runner_spec


def _registry(tmp_path: Path) -> dict:
    scenario = tmp_path / "scenario"
    scenario.mkdir()
    (scenario / "demo.xosc").write_text("<OpenSCENARIO/>", encoding="utf-8")
    (scenario / "stop_conditions.yaml").write_text("conditions: []\n", encoding="utf-8")
    (scenario / "spec.yaml").write_text(
        yaml.safe_dump(
            {"ego": {"goal": {"type": "LanePosition", "value": [1, 1, 10, 0]}, "target_speed": 5}}
        ),
        encoding="utf-8",
    )
    xodr = tmp_path / "map" / "xodr"
    osm = tmp_path / "map" / "osm"
    xodr.mkdir(parents=True)
    osm.mkdir(parents=True)
    runner = tmp_path / "runner"
    runner.mkdir()
    configs = tmp_path / "configs"
    configs.mkdir()
    for name in ("sim.yaml", "av.yaml", "monitor.yaml", "sampler.yaml"):
        (configs / name).write_text("{}\n", encoding="utf-8")
    return {
        "version": 1,
        "variables": {},
        "components": {
            "sim": {
                "kind": "simulator",
                "image": "sim:test",
                "defaults": {"config_path": str(configs / "sim.yaml")},
                "build": {"context": str(tmp_path), "builder": "docker build"},
                "run": {"network": "host", "ports": {"service": 9001, "carla": 2001}},
            },
            "av": {
                "kind": "av",
                "image": "av:test",
                "defaults": {"config_path": str(configs / "av.yaml")},
                "build": {"context": str(tmp_path), "builder": "docker build"},
                "run": {"network": "bridge", "ports": {"service": 9002}},
            },
        },
        "experiments": {
            "demo": {
                "label": "Demo",
                "runtime": {"dt": 0.05},
                "task": {"output_dir": str(tmp_path / "output")},
                "simulator": {"component": "sim", "config_path": str(configs / "sim.yaml")},
                "av": {"component": "av", "config_path": str(configs / "av.yaml")},
                "map": {"name": "map", "xodr_path": str(xodr), "osm_path": str(osm)},
                "scenario": {
                    "name": "demo",
                    "path": str(scenario),
                    "format": "open_scenario1",
                    "stop_condition_config_path": "stop_conditions.yaml",
                },
                "sampler": {"name": "grid", "config_path": str(configs / "sampler.yaml")},
                "monitor": {"config_path": str(configs / "monitor.yaml")},
                "runner": {
                    "repo_path": str(runner),
                    "command": ["uv", "run", "python", "main.py", "--runner_spec", "{runner_spec}"],
                    "rmlib_path": str(tmp_path / "libesminiRMLib.so"),
                },
                "analysis": {"output_dir": str(tmp_path / "report")},
            }
        },
    }


def test_deep_merge_and_variable_resolution(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PISA_TEST_ROOT", str(tmp_path))
    defaults = _registry(tmp_path)
    defaults["variables"]["ROOT"] = "${PISA_TEST_ROOT}"
    defaults["experiments"]["demo"]["task"]["output_dir"] = "${ROOT}/resolved"

    resolved = resolve_registry(defaults, {"experiments": {"demo": {"runtime": {"dt": 0.1}}}})

    assert resolved["experiments"]["demo"]["task"]["output_dir"] == str(tmp_path / "resolved")
    assert resolved["experiments"]["demo"]["runtime"]["dt"] == 0.1
    assert deep_merge({"a": {"b": 1}}, {"a": {"c": 2}}) == {"a": {"b": 1, "c": 2}}


def test_resolve_experiment_inherits_component_profiles(tmp_path: Path) -> None:
    experiment = resolve_experiment(_registry(tmp_path), "demo")

    assert experiment["simulator"]["image"] == "sim:test"
    assert experiment["simulator"]["config_path"].endswith("sim.yaml")
    assert experiment["av"]["run"]["network"] == "bridge"


def test_build_and_run_commands_are_structured(tmp_path: Path) -> None:
    experiment = resolve_experiment(_registry(tmp_path), "demo")
    build = build_command(experiment["simulator"], force=True)
    run, name = docker_run_command(
        experiment["simulator"],
        role="simulator",
        job_id="abc",
        ports={"service": 9001, "carla": 2001},
        mounts=[{"source": str(tmp_path), "target": "/mnt/output", "mode": "rw"}],
    )

    assert build == ["docker", "build", "--no-cache", "--tag", "sim:test", str(tmp_path)]
    assert name == "pisa-simulator-abc-9001"
    assert "--network" in run and run[run.index("--network") + 1] == "host"
    assert "--publish" not in run
    assert "PORT=9001" in run and "CARLA_PORT=2001" in run
    assert "pisa.experiment-runner=true" in run


def test_runner_spec_uses_host_and_container_paths(tmp_path: Path) -> None:
    experiment = resolve_experiment(_registry(tmp_path), "demo")

    spec = build_runner_spec(
        experiment,
        {"simulator": {"service": 9101}, "av": {"service": 9102}},
        tmp_path / "output",
        "job-1",
    )

    assert spec["simulator"]["url"] == "localhost:9101"
    assert spec["simulator"]["scenario"]["path"] == "/mnt/scenario"
    assert spec["scenario"]["scenario_path"].endswith("scenario")
    assert spec["scenario"]["goal_config"]["position"]["type"] == "LanePosition"
    assert spec["scenario"]["stop_condition_config_path"] == "stop_conditions.yaml"


def test_scenario_directory_inspection_infers_metadata(tmp_path: Path) -> None:
    scenario = tmp_path / "cutin"
    scenario.mkdir()
    (scenario / "cutin.xosc").write_text("<OpenSCENARIO/>", encoding="utf-8")
    (scenario / "param.xosc").write_text("<OpenSCENARIO/>", encoding="utf-8")
    (scenario / "stop_condition.yaml").write_text("conditions: []\n", encoding="utf-8")
    (scenario / "spec.yaml").write_text(
        yaml.safe_dump(
            {
                "scenario_name": "cutin",
                "map_name": "straight_3000m",
                "ego": {
                    "position": {"type": "LanePosition", "value": [1, -1, 100, 0]},
                    "target_speed": 8.5,
                },
            }
        ),
        encoding="utf-8",
    )

    result = inspect_scenario_directory(scenario)

    assert result["valid"] is True
    assert result["scenario_name"] == "cutin"
    assert result["map_name"] == "straight_3000m"
    assert result["xosc_path"].endswith("cutin.xosc")
    assert result["stop_condition_config_path"] == "stop_condition.yaml"
    assert result["goal_config"] == {
        "position": {"type": "LanePosition", "value": [1, -1, 100, 0]},
        "target_speed": 8.5,
    }


def test_scenario_directory_inspection_explains_manual_fields(tmp_path: Path) -> None:
    scenario = tmp_path / "ambiguous"
    scenario.mkdir()
    (scenario / "a.xosc").write_text("<OpenSCENARIO/>", encoding="utf-8")
    (scenario / "b.xosc").write_text("<OpenSCENARIO/>", encoding="utf-8")
    (scenario / "stop_conditions.yaml").write_text("conditions: []\n", encoding="utf-8")
    (scenario / "spec.yaml").write_text("ego: {}\n", encoding="utf-8")

    result = inspect_scenario_directory(scenario)
    codes = {finding["code"] for finding in result["findings"]}

    assert result["valid"] is True
    assert {"scenario_xosc_ambiguous", "scenario_name", "map_name", "goal_position"} <= codes


def test_config_store_writes_atomically(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    defaults = tmp_path / "defaults.yaml"
    defaults.write_text(yaml.safe_dump(registry), encoding="utf-8")
    store = ConfigStore(tmp_path / "registry.yaml")
    store.defaults_path = defaults

    store.save(registry)

    assert yaml.safe_load(store.path.read_text(encoding="utf-8"))["version"] == 1
    assert not list(tmp_path.glob(".registry.yaml.*"))


def test_preset_crud_applies_component_defaults_and_tombstones(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    defaults = tmp_path / "defaults.yaml"
    defaults.write_text(yaml.safe_dump(registry), encoding="utf-8")
    store = ConfigStore(tmp_path / "registry.yaml")
    store.defaults_path = defaults

    created = store.create_preset(
        "autoware-smoke",
        template_id="demo",
        label="Autoware smoke",
        simulator_component="sim",
        av_component="av",
        tags=["smoke", "autoware", "smoke"],
    )
    assert created["tags"] == ["autoware", "smoke"]
    assert created["av"]["config_path"].endswith("av.yaml")
    assert created["task"]["output_dir"] == "${OUTPUT_ROOT}/autoware-smoke"

    renamed = store.rename_preset("demo", new_id="demo-renamed", label="Renamed")
    assert renamed["preset_id"] == "demo-renamed"
    assert "demo" not in store.editable()["experiments"]

    store.delete_preset("demo-renamed")
    assert "demo-renamed" not in store.editable()["experiments"]


def test_output_adoption_and_early_ownership_marker(tmp_path: Path) -> None:
    experiment = resolve_experiment(_registry(tmp_path), "demo")
    output = Path(experiment["task"]["output_dir"])
    output.mkdir()
    (output / "carla_server").mkdir()

    blocked = validate_experiment(experiment, check_ports=False)
    assert any(row["code"] == "output_not_owned" for row in blocked["findings"])

    experiment["task"]["adopt_existing_output"] = True
    adopted = validate_experiment(experiment, check_ports=False)
    assert adopted["valid"] is True
    assert any(row["code"] == "output_adopted" for row in adopted["findings"])

    marker = write_ownership_marker(ExperimentJob(experiment), output, "starting")
    experiment["task"]["adopt_existing_output"] = False
    recognized = validate_experiment(experiment, check_ports=False)
    assert marker.is_file()
    assert recognized["valid"] is True


def test_bundled_registry_loads() -> None:
    registry = ConfigStore(Path("/does/not/exist.yaml")).load()

    assert set(registry["components"]) == {
        "carla",
        "esmini",
        "simple",
        "autoware",
        "carla-agent",
        "pcla",
    }
    assert "cutin-esmini-simple" in registry["experiments"]


def test_standalone_app_routes_and_ui(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    defaults = tmp_path / "defaults.yaml"
    defaults.write_text(yaml.safe_dump(registry), encoding="utf-8")
    store = ConfigStore(tmp_path / "registry.yaml")
    store.defaults_path = defaults
    app = create_app(store, token="secret")
    paths = {route.path for route in app.routes}

    assert app.title == "PISA Experiment Runner"
    assert "/api/registry" in paths
    assert "/api/presets" in paths
    assert "/api/presets/{preset_id}/rename" in paths
    assert "/api/presets/{preset_id}/delete" in paths
    assert "/api/scenarios/inspect" in paths
    assert "/api/experiments/preview" in paths
    assert "/api/jobs/{job_id}/events" in paths
    assert "/api/resources/cleanup" in paths
    assert "/reports/{report_token}/{job_id}/{asset_path:path}" in paths
    assert not any("reports/{report_id}" in path for path in paths)
    html = (
        Path(__file__).parents[1]
        / "src/pisa_sample_tools/experiment_runner/web/index.html"
    ).read_text(encoding="utf-8")
    assert "PISA Experiment Runner" in html
    assert "Run All" in html
    assert "Generate Report" in html
    assert "Experiment form" in html
    assert "Adopt reviewed non-empty output" in html
    assert "Tags (comma separated)" in html
    assert "Inspect folder" in html
    assert 'id="goal-values"' in html
    for sampler in ("grid", "native", "lhs", "sobol", "random", "feedback_boundary"):
        assert f'<option value="{sampler}">' in html
    assert '<option value="explicit">explicit_sample</option>' in html
    assert "Report Builder" not in html
    assert json.loads(json.dumps(registry))["version"] == 1
