from __future__ import annotations

from pathlib import Path
from typing import Any

from simcore.sampler import create_sampler, load_parameter_space
from simcore.sampler.loader import infer_source_type
from simcore.utils.util import get_cfg

from .models import SamplePreview, SamplerPreviewResult, SamplerTestError


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

