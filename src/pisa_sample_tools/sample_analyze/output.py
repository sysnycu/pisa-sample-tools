from __future__ import annotations

import csv
import shutil
from pathlib import Path
from typing import Any

import yaml

from .models import AnalyzeError


def prepare_analysis_dir(output_dir: Path, *, overwrite: bool) -> None:
    if output_dir.exists():
        if not output_dir.is_dir():
            raise AnalyzeError(f"output path exists and is not a directory: {output_dir}")
        if not overwrite:
            raise AnalyzeError(f"analysis output already exists: {output_dir}")
        marker = output_dir / "summary.yaml"
        if not marker.exists():
            raise AnalyzeError(
                "analysis output exists but summary.yaml was not found; refusing to overwrite"
            )
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=False), encoding="utf-8")
