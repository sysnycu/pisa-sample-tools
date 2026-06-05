from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class ExportError(ValueError):
    """Raised for user-facing export failures."""


class SourcePathMode(StrEnum):
    ABSOLUTE = "absolute"
    RELATIVE_TO_OUTPUT = "relative-to-output"


EXPLICIT_SAMPLE_FILE_NAME = "explicit_samples.yaml"


@dataclass(frozen=True)
class ExportResult:
    output_dir: Path
    manifest_path: Path
    total_samples: int
    shard_count: int
    zip_path: Path | None = None
    dry_run: bool = False
    summary: dict[str, Any] | None = None


@dataclass(frozen=True)
class ScenarioAssets:
    name: str
    xosc_path: Path
    spec_path: Path
    stop_conditions_path: Path

