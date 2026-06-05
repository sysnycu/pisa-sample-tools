from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )


def load_mapping_file(path: Path, *, label: str, error_type: type[ValueError]) -> dict[str, Any]:
    path = Path(path).expanduser()
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise error_type(f"failed to read {label} {path}: {exc}") from exc
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise error_type(f"failed to parse {label} {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise error_type(f"{label} must contain a mapping/object")
    return data
