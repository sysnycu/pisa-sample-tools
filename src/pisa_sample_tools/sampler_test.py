from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from simcore.sampler import create_sampler, load_parameter_space
from simcore.sampler.loader import infer_source_type
from simcore.utils.util import get_cfg


class SamplerTestError(ValueError):
    """Raised for user-facing sampler preview failures."""


@dataclass(frozen=True)
class SamplePreview:
    index: int
    id: str | None
    params: dict[str, Any]
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class SamplerPreviewResult:
    source_file: Path
    source_type: str
    sampler_name: str
    total_samples: int | None
    samples: list[SamplePreview]

    @property
    def generated_samples(self) -> int:
        return len(self.samples)


def collect_sampler_preview(
    *,
    source_file: Path,
    sampler_name: str | None = None,
    source_type: str | None = None,
    module_path: str | None = None,
    config_path: Path | None = None,
    config: dict[str, Any] | None = None,
    max_samples: int | None = None,
) -> SamplerPreviewResult:
    if max_samples is not None and max_samples < 0:
        raise SamplerTestError("max-samples must be greater than or equal to 0")

    source_file = source_file.expanduser()
    if not source_file.exists():
        raise SamplerTestError(f"source file does not exist: {source_file}")
    if config_path is not None and not config_path.expanduser().exists():
        raise SamplerTestError(f"config file does not exist: {config_path}")

    effective_source_type = source_type or infer_source_type(source_file)
    effective_sampler_name = sampler_name or default_sampler_for_source_type(effective_source_type)

    try:
        parameter_space = load_parameter_space(source_file, effective_source_type)
        file_config = get_cfg(config_path.expanduser()) if config_path is not None else {}
    except Exception as exc:
        raise SamplerTestError(str(exc)) from exc

    if file_config is None:
        file_config = {}
    if not isinstance(file_config, dict):
        raise SamplerTestError(f"Sampler config file {config_path} must contain a mapping/object")

    sampler_spec: dict[str, Any] = {
        "name": effective_sampler_name,
        "source": {"type": effective_source_type, "path": str(source_file)},
        **file_config,
        **(config or {}),
    }
    if module_path is not None:
        sampler_spec["module_path"] = module_path

    try:
        sampler = create_sampler(sampler_spec=sampler_spec, parameter_space=parameter_space)
        total_samples = sampler.total_samples()
        previews: list[SamplePreview] = []
        while max_samples is None or len(previews) < max_samples:
            sample = sampler.next()
            if sample is None:
                break
            previews.append(
                SamplePreview(
                    index=len(previews) + 1,
                    id=None if sample.id is None else str(sample.id),
                    params=sample.params,
                    metadata=sample.metadata or None,
                )
            )
    except Exception as exc:
        raise SamplerTestError(str(exc)) from exc

    return SamplerPreviewResult(
        source_file=source_file,
        source_type=effective_source_type,
        sampler_name=effective_sampler_name,
        total_samples=total_samples,
        samples=previews,
    )


def default_sampler_for_source_type(source_type: str) -> str:
    normalized = source_type.lower()
    if normalized in {"openscenario", "xosc"}:
        return "native"
    if normalized in {"explicit", "sample_list", "samples"}:
        return "explicit"
    return "grid"


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
