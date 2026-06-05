from __future__ import annotations

from typing import Any

import yaml

from .models import SamplePreview, SamplerPreviewResult


def result_to_dict(result: SamplerPreviewResult) -> dict[str, Any]:
    return {
        "source_file": str(result.source_file),
        "source_type": result.source_type,
        "sampler": result.sampler_name,
        "total_samples": result.total_samples,
        "generated_samples": result.generated_samples,
        "samples": [
            {
                key: value
                for key, value in {
                    "index": sample.index,
                    "id": sample.id,
                    "params": sample.params,
                    "metadata": sample.metadata,
                }.items()
                if value is not None
            }
            for sample in result.samples
        ],
    }


def format_table(result: SamplerPreviewResult) -> str:
    lines = [
        f"Source: {result.source_file}",
        f"Source type: {result.source_type}",
        f"Sampler: {result.sampler_name}",
        f"Total samples: {'unknown' if result.total_samples is None else result.total_samples}",
        f"Generated samples: {result.generated_samples}",
        "",
    ]
    if not result.samples:
        lines.append("No samples generated.")
        return "\n".join(lines)

    param_columns = _ordered_param_columns(result.samples)
    include_id = any(sample.id is not None for sample in result.samples)
    columns = ["#", *(["id"] if include_id else []), *param_columns]
    rows: list[list[str]] = []
    for sample in result.samples:
        row = [str(sample.index)]
        if include_id:
            row.append(sample.id or "")
        row.extend(_format_value(sample.params.get(column, "")) for column in param_columns)
        rows.append(row)

    widths = [
        max(len(column), *(len(row[column_index]) for row in rows))
        for column_index, column in enumerate(columns)
    ]
    header = " | ".join(column.ljust(widths[index]) for index, column in enumerate(columns))
    divider = "-+-".join("-" * width for width in widths)
    lines.extend([header, divider])
    lines.extend(
        " | ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in rows
    )
    return "\n".join(lines)


def format_yaml(result: SamplerPreviewResult) -> str:
    return yaml.safe_dump(result_to_dict(result), sort_keys=False)


def _ordered_param_columns(samples: list[SamplePreview]) -> list[str]:
    columns: list[str] = []
    seen: set[str] = set()
    for sample in samples:
        for key in sample.params:
            if key not in seen:
                columns.append(key)
                seen.add(key)
    return columns


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)

