from __future__ import annotations

from collections import Counter
from typing import Any

from .statistics import as_float

KNOWN_NOT_APPLICABLE_STATUSES = {
    "outside_lateral_threshold",
    "non_closing",
    "not_ahead",
    "collision",
    "not_applicable",
}
VALID_STATUSES = {"valid", "ok"}


def companion_fields(field: str) -> tuple[str, str]:
    base = field.removesuffix("_s") if field.endswith(".ttc_s") else field
    return f"{base}_valid", f"{base}_status"


def metric_coverage(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    valid_field, status_field = companion_fields(field)
    counts: Counter[str] = Counter()
    invalid = true_missing = 0
    for row in rows:
        value, status, valid_flag = (
            as_float(row.get(field)),
            str(row.get(status_field) or "").strip().lower(),
            _bool(row.get(valid_field)),
        )
        if value is not None:
            counts["valid"] += 1
        elif status in KNOWN_NOT_APPLICABLE_STATUSES or (status and valid_flag is False):
            counts[status or "not_applicable"] += 1
        elif status in VALID_STATUSES or valid_flag is True or status:
            invalid += 1
        else:
            true_missing += 1
    return {
        "total": len(rows),
        "valid": counts.pop("valid", 0),
        "not_applicable": sum(counts.values()),
        "status_counts": dict(sorted(counts.items())),
        "invalid": invalid,
        "missing": true_missing,
        "valid_field": valid_field if rows and valid_field in rows[0] else None,
        "status_field": status_field if rows and status_field in rows[0] else None,
    }


def status_points(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    _, status_field = companion_fields(field)
    output = []
    for row in rows:
        status, time = (
            str(row.get(status_field) or "").strip().lower(),
            as_float(row.get("sim_time_ms")),
        )
        if status and time is not None:
            output.append({"time_s": time / 1000.0, "status": status})
    return output


def _bool(value: Any) -> bool | None:
    if value in {None, ""}:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None
