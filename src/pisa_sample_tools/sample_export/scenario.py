from __future__ import annotations

from pathlib import Path
from typing import Any

from pisa_sample_tools.common.yaml import load_mapping_file

from .models import ExportError, ScenarioAssets


def scenario_base_from_path(scenario_path: Path) -> Path:
    scenario_path = Path(scenario_path).expanduser()
    if scenario_path.exists():
        if scenario_path.is_dir():
            return scenario_path
        return scenario_path.parent
    if scenario_path.suffix:
        return scenario_path.parent
    return scenario_path


def load_export_mapping_file(path: Path, *, label: str) -> dict[str, Any]:
    return load_mapping_file(path, label=label, error_type=ExportError)


def runner_scenario_path(runner_spec: dict[str, Any], runner_spec_path: Path) -> Path:
    scenario = runner_spec.get("scenario")
    if not isinstance(scenario, dict):
        raise ExportError("runner spec must contain scenario.scenario_path")
    raw_path = scenario.get("scenario_path")
    if not raw_path:
        raise ExportError("runner spec must contain scenario.scenario_path")
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return Path(runner_spec_path).expanduser().parent / path


def resolve_scenario_assets(
    *,
    scenario_base: Path | None,
    runner_spec: dict[str, Any] | None,
) -> ScenarioAssets:
    if scenario_base is None:
        raise ExportError("scenario path is required to build output bundles")

    candidate_dirs = [scenario_base]
    stop_conditions_config_path = runner_stop_conditions_path(runner_spec)
    if stop_conditions_config_path is not None:
        candidate_dirs.append(stop_conditions_config_path.parent)

    scenario_name = resolve_scenario_name(runner_spec, candidate_dirs)
    xosc_path = find_required_file(
        candidate_dirs,
        file_names=[f"{scenario_name}.xosc"],
        description=f"{scenario_name}.xosc",
    )
    spec_path = find_required_file(candidate_dirs, file_names=["spec.yaml"], description="spec.yaml")

    if stop_conditions_config_path is not None and stop_conditions_config_path.exists():
        stop_conditions_path = stop_conditions_config_path
    else:
        stop_conditions_path = find_required_file(
            candidate_dirs,
            file_names=["stop_conditions.yaml"],
            description="stop_conditions.yaml",
        )

    return ScenarioAssets(
        name=scenario_name,
        xosc_path=xosc_path,
        spec_path=spec_path,
        stop_conditions_path=stop_conditions_path,
    )


def runner_stop_conditions_path(runner_spec: dict[str, Any] | None) -> Path | None:
    if runner_spec is None:
        return None
    scenario = runner_spec.get("scenario")
    if not isinstance(scenario, dict):
        return None
    raw_path = scenario.get("stop_condition_config_path")
    if not raw_path:
        return None
    return Path(raw_path).expanduser()


def resolve_scenario_name(
    runner_spec: dict[str, Any] | None,
    candidate_dirs: list[Path],
) -> str:
    runner_name = runner_scenario_name(runner_spec)
    if runner_name:
        return runner_name

    for directory in candidate_dirs:
        spec_path = directory / "spec.yaml"
        if spec_path.exists():
            spec = load_export_mapping_file(spec_path, label="scenario spec")
            raw_name = spec.get("scenario_name")
            if raw_name:
                return str(raw_name)

    xosc_paths = sorted({path for directory in candidate_dirs for path in directory.glob("*.xosc")})
    if len(xosc_paths) == 1:
        return xosc_paths[0].stem
    if not xosc_paths:
        raise ExportError("could not infer scenario name because no .xosc file was found")
    names = ", ".join(path.name for path in xosc_paths)
    raise ExportError(f"could not infer scenario name because multiple .xosc files were found: {names}")


def runner_scenario_name(runner_spec: dict[str, Any] | None) -> str | None:
    if runner_spec is None:
        return None
    scenario = runner_spec.get("scenario")
    if isinstance(scenario, dict):
        raw_name = scenario.get("title") or scenario.get("name")
        if raw_name:
            return str(raw_name)
    simulator = runner_spec.get("simulator")
    if isinstance(simulator, dict):
        simulator_scenario = simulator.get("scenario")
        if isinstance(simulator_scenario, dict) and simulator_scenario.get("name"):
            return str(simulator_scenario["name"])
    return None


def find_required_file(
    candidate_dirs: list[Path],
    *,
    file_names: list[str],
    description: str,
) -> Path:
    searched: list[str] = []
    for directory in candidate_dirs:
        for file_name in file_names:
            path = directory / file_name
            searched.append(str(path))
            if path.exists() and path.is_file():
                return path
    raise ExportError(f"required scenario file not found: {description}; searched: {searched}")

