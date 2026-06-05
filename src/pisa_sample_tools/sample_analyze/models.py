from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class AnalyzeError(ValueError):
    """Raised for user-facing analysis failures."""


@dataclass(frozen=True)
class SampleRecord:
    sample_id: str
    params: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str | None = None
    outcome: str | None = None
    stop_condition: str | None = None
    stop_reason: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    result_path: Path | None = None
    post_outcome: dict[str, Any] | None = None


@dataclass(frozen=True)
class AnalysisResult:
    output_dir: Path
    report_path: Path
    summary_path: Path
    csv_path: Path
    figure_paths: list[Path]
    record_count: int
    selected_params: tuple[str, ...]


@dataclass(frozen=True)
class ColorSpec:
    color_by: str
    mode: str
    values: list[str]
    palette: dict[str, str]
    numeric_values: list[float | None]
    numeric_min: float | None = None
    numeric_max: float | None = None
