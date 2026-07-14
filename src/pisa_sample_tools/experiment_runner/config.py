from __future__ import annotations

import copy
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Invalid experiment-runner configuration."""


_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _expand(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _expand(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item, variables) for item in value]
    if not isinstance(value, str):
        return value
    expanded = value
    for _ in range(10):
        updated = _VARIABLE.sub(lambda match: variables.get(match.group(1), match.group(0)), expanded)
        if updated == expanded:
            break
        expanded = updated
    return os.path.expanduser(expanded)


def validate_registry(registry: dict[str, Any]) -> None:
    if registry.get("version") != 1:
        raise ConfigError("experiment-runner registry version must be 1")
    for section in ("components", "experiments"):
        if not isinstance(registry.get(section), dict):
            raise ConfigError(f"{section} must be a mapping")
    for component_id, component in registry["components"].items():
        if not isinstance(component, dict) or component.get("kind") not in {"simulator", "av"}:
            raise ConfigError(f"component {component_id!r} must have kind simulator or av")
        if not isinstance(component.get("build", {}), dict) or not isinstance(
            component.get("run", {}), dict
        ):
            raise ConfigError(f"component {component_id!r} build/run must be mappings")
    for experiment_id, experiment in registry["experiments"].items():
        if not isinstance(experiment, dict):
            raise ConfigError(f"experiment {experiment_id!r} must be a mapping")
        missing = [
            key
            for key in ("scenario", "map", "simulator", "av", "runner", "task", "monitor")
            if not isinstance(experiment.get(key), dict)
        ]
        if missing:
            raise ConfigError(f"experiment {experiment_id!r} is missing mappings: {', '.join(missing)}")
        tags = experiment.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ConfigError(f"experiment {experiment_id!r} tags must be a list of strings")


def resolve_registry(
    defaults: dict[str, Any],
    registry: dict[str, Any] | None = None,
    local: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = deep_merge(defaults, registry or {})
    merged = deep_merge(merged, local or {})
    validate_registry(merged)
    variables = {
        "HOME": str(Path.home()),
        "UID": str(os.getuid()),
        "GID": str(os.getgid()),
        **{str(key): str(value) for key, value in merged.get("variables", {}).items()},
        **{key: value for key, value in os.environ.items() if isinstance(value, str)},
    }
    return _expand(merged, variables)


def resolve_experiment(
    registry: dict[str, Any], experiment_id: str, overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    try:
        experiment = registry["experiments"][experiment_id]
    except KeyError as exc:
        raise ConfigError(f"unknown experiment preset: {experiment_id}") from exc
    resolved = deep_merge(experiment, overrides or {})
    for role in ("simulator", "av"):
        component_id = resolved[role].get("component")
        component = registry["components"].get(component_id)
        if component is None:
            raise ConfigError(f"unknown {role} component: {component_id}")
        if component.get("kind") != role:
            raise ConfigError(f"component {component_id!r} is not a {role}")
        resolved[role] = deep_merge(component, resolved[role])
        resolved[role]["component"] = component_id
    resolved["id"] = experiment_id
    return resolved


class ConfigStore:
    def __init__(self, path: Path, local_path: Path | None = None):
        self.path = Path(path).expanduser()
        self.local_path = Path(local_path).expanduser() if local_path else None
        self.defaults_path = Path(__file__).with_name("defaults.yaml")

    @staticmethod
    def _read(path: Path, *, required: bool = False) -> dict[str, Any]:
        if not path.is_file():
            if required:
                raise ConfigError(f"configuration file does not exist: {path}")
            return {}
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"failed to read {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"configuration must contain a mapping: {path}")
        return data

    def load(self) -> dict[str, Any]:
        defaults = self._read(self.defaults_path, required=True)
        configured = self._read(self.path)
        local = self._read(self.local_path) if self.local_path else {}
        return resolve_registry(defaults, configured, local)

    def editable(self) -> dict[str, Any]:
        """Return the versioned registry without applying machine-local values or expansion."""
        defaults = self._read(self.defaults_path, required=True)
        configured = self._read(self.path)
        registry = deep_merge(defaults, configured)
        disabled = configured.get("disabled_experiments", [])
        if isinstance(disabled, list):
            for experiment_id in disabled:
                registry["experiments"].pop(str(experiment_id), None)
        validate_registry(registry)
        return registry

    def resolve_experiment(
        self, experiment_id: str, overrides: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        editable = self.editable()
        if overrides:
            if experiment_id not in editable["experiments"]:
                raise ConfigError(f"unknown experiment preset: {experiment_id}")
            editable["experiments"][experiment_id] = deep_merge(
                editable["experiments"][experiment_id], overrides
            )
        local = self._read(self.local_path) if self.local_path else {}
        return resolve_experiment(resolve_registry(editable, local=local), experiment_id)

    def save(self, registry: dict[str, Any]) -> None:
        registry = copy.deepcopy(registry)
        validate_registry(registry)
        defaults = self._read(self.defaults_path, required=True)
        registry["disabled_experiments"] = sorted(
            set(defaults.get("experiments", {})) - set(registry.get("experiments", {}))
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump(registry, sort_keys=False, allow_unicode=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, delete=False, prefix=f".{self.path.name}."
        ) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        temporary.replace(self.path)

    def create_preset(
        self,
        preset_id: str,
        *,
        template_id: str,
        label: str,
        simulator_component: str,
        av_component: str,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        preset_id = _validate_preset_id(preset_id)
        registry = self.editable()
        if preset_id in registry["experiments"]:
            raise ConfigError(f"experiment preset already exists: {preset_id}")
        if template_id not in registry["experiments"]:
            raise ConfigError(f"unknown template preset: {template_id}")
        experiment = copy.deepcopy(registry["experiments"][template_id])
        experiment["label"] = label.strip() or preset_id
        experiment["tags"] = _normalize_tags(tags or [])
        _apply_component(registry, experiment, "simulator", simulator_component)
        _apply_component(registry, experiment, "av", av_component)
        experiment.setdefault("task", {})["output_dir"] = f"${{OUTPUT_ROOT}}/{preset_id}"
        experiment.setdefault("analysis", {})["output_dir"] = f"${{REPORT_ROOT}}/{preset_id}"
        registry["experiments"][preset_id] = experiment
        self.save(registry)
        return self.editable()["experiments"][preset_id]

    def update_preset(self, preset_id: str, experiment: dict[str, Any]) -> dict[str, Any]:
        registry = self.editable()
        if preset_id not in registry["experiments"]:
            raise ConfigError(f"unknown experiment preset: {preset_id}")
        experiment = copy.deepcopy(experiment)
        experiment["tags"] = _normalize_tags(experiment.get("tags", []))
        registry["experiments"][preset_id] = experiment
        self.save(registry)
        return self.editable()["experiments"][preset_id]

    def rename_preset(
        self, preset_id: str, *, new_id: str, label: str | None = None
    ) -> dict[str, Any]:
        new_id = _validate_preset_id(new_id)
        registry = self.editable()
        if preset_id not in registry["experiments"]:
            raise ConfigError(f"unknown experiment preset: {preset_id}")
        if new_id != preset_id and new_id in registry["experiments"]:
            raise ConfigError(f"experiment preset already exists: {new_id}")
        experiment = registry["experiments"].pop(preset_id)
        if label is not None:
            experiment["label"] = label.strip() or new_id
        registry["experiments"][new_id] = experiment
        self.save(registry)
        return {"preset_id": new_id, "experiment": self.editable()["experiments"][new_id]}

    def delete_preset(self, preset_id: str) -> None:
        registry = self.editable()
        if preset_id not in registry["experiments"]:
            raise ConfigError(f"unknown experiment preset: {preset_id}")
        registry["experiments"].pop(preset_id)
        self.save(registry)


def _validate_preset_id(value: str) -> str:
    value = value.strip()
    if not value or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ConfigError("preset ID must use letters, numbers, dot, underscore, or dash")
    return value


def _normalize_tags(tags: list[str]) -> list[str]:
    return sorted({tag.strip() for tag in tags if tag.strip()})


def _apply_component(
    registry: dict[str, Any], experiment: dict[str, Any], role: str, component_id: str
) -> None:
    component = registry["components"].get(component_id)
    if component is None or component.get("kind") != role:
        raise ConfigError(f"unknown {role} component: {component_id}")
    experiment.setdefault(role, {})["component"] = component_id
    defaults = component.get("defaults", {})
    if defaults.get("config_path"):
        experiment[role]["config_path"] = defaults["config_path"]
