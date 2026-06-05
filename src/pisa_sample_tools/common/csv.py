from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


def read_csv_dicts(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, skipinitialspace=True)
        rows: list[dict[str, str]] = []
        for raw_row in reader:
            row = {
                (key or "").strip(): (value.strip() if isinstance(value, str) else "")
                for key, value in raw_row.items()
                if key is not None
            }
            if any(value not in {"", None} for value in row.values()):
                rows.append(row)
        return rows


def read_csv_dicts_required(path: Path, *, error_type: type[ValueError]) -> list[dict[str, str]]:
    if not path.exists():
        raise error_type(f"required log file not found: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, skipinitialspace=True)
        if reader.fieldnames is None:
            raise error_type(f"CSV has no header: {path}")
        rows: list[dict[str, str]] = []
        for raw_row in reader:
            clean = {
                (key or "").strip(): (value.strip() if isinstance(value, str) else "")
                for key, value in raw_row.items()
                if key is not None
            }
            if any(value not in {"", None} for value in clean.values()):
                rows.append(clean)
    return rows


def write_csv_rows(path: Path, rows: list[dict[str, Any]], *, error_type: type[ValueError]) -> None:
    if not rows:
        raise error_type("cannot write empty CSV")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

