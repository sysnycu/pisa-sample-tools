from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def natural_key(value: Any) -> list[Any]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(value))]


def natural_path_key(path: Path) -> list[Any]:
    return natural_key(str(path))

