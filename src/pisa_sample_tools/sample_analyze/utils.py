from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any


def as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value in {None, ""}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def coerce_scalar(value: str) -> Any:
    if value == "":
        return ""
    number = as_float(value)
    return number if number is not None else value


def parse_json_mapping(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def none_if_empty(value: str | None) -> str | None:
    return value if value not in {None, ""} else None


def read_csv_dicts(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows: list[dict[str, str]] = []
        for row in csv.DictReader(handle, skipinitialspace=True):
            clean = {
                key.strip(): value.strip() if isinstance(value, str) else value
                for key, value in row.items()
                if key is not None
            }
            if any(value not in {"", None} for value in clean.values()):
                rows.append(clean)
        return rows


def iteration_sort_key(path: Path) -> tuple[int, str]:
    suffix = path.name.removeprefix("iteration_")
    return (int(suffix), suffix) if suffix.isdigit() else (10**12, suffix)

