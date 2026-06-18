from __future__ import annotations

from pathlib import Path
from typing import Any

from pisa_sample_tools.common.yaml import load_mapping_file

from .models import AnalysisSpec, EvidenceError, MetricBinding

DEFAULT_METRICS = {
    "min_ttc": MetricBinding(
        summary="ego_to_agent_1.min_ttc_s",
        series="ego_to_agent_1.ttc_s",
        label="Minimum TTC",
        unit="s",
    ),
    "min_distance": MetricBinding(
        summary="ego_to_agent_1.min_distance_m",
        series="ego_to_agent_1.distance_m",
        label="Minimum distance",
        unit="m",
    ),
    "max_deceleration": MetricBinding(
        summary="ego_deceleration.max",
        series="ego.acceleration",
        label="Maximum deceleration",
        unit="m/s^2",
    ),
}


def load_analysis_spec(path: Path | None) -> AnalysisSpec:
    if path is None:
        return AnalysisSpec(metrics=dict(DEFAULT_METRICS))
    raw = load_mapping_file(path, label="analysis spec", error_type=EvidenceError)
    version = int(raw.get("version", 1))
    if version != 1:
        raise EvidenceError(f"unsupported analysis spec version: {version}")
    parameter_config = _mapping(raw.get("parameters"))
    axes = _mapping(parameter_config.get("axes"))
    units = {
        str(key): str(value)
        for key, value in _mapping(parameter_config.get("units")).items()
    }
    metric_config = _mapping(raw.get("metrics"))
    metrics = dict(DEFAULT_METRICS)
    for name, value in metric_config.items():
        if isinstance(value, str):
            metrics[str(name)] = MetricBinding(summary=value)
            continue
        config = _mapping(value)
        metrics[str(name)] = MetricBinding(
            summary=_optional_str(config.get("summary")),
            series=_optional_str(config.get("series")),
            label=_optional_str(config.get("label")),
            unit=_optional_str(config.get("unit")),
        )
    outcomes = _mapping(raw.get("outcomes"))
    thresholds = _mapping(raw.get("thresholds"))
    heatmap = _mapping(raw.get("heatmap"))
    output = _mapping(raw.get("output"))
    formats = tuple(str(value).lower() for value in output.get("formats", ["svg", "png"]))
    unknown_formats = sorted(set(formats) - {"svg", "png"})
    if unknown_formats:
        raise EvidenceError(f"unsupported output format(s): {', '.join(unknown_formats)}")
    bins = int(heatmap.get("bins", 30))
    if bins <= 0:
        raise EvidenceError("heatmap bins must be greater than 0")
    return AnalysisSpec(
        version=version,
        metadata=_mapping(raw.get("metadata")),
        x_param=_optional_str(axes.get("x")),
        y_param=_optional_str(axes.get("y")),
        parameter_units=units,
        metrics=metrics,
        success_outcomes=_string_set(outcomes.get("success"), {"success"}),
        failure_outcomes=_string_set(
            outcomes.get("failure"),
            {"fail", "failure", "failed", "test_fail", "collision"},
        ),
        invalid_outcomes=_string_set(outcomes.get("invalid"), {"invalid"}),
        collision_reasons=_string_set(
            outcomes.get("collision_reasons"),
            {"collision", "collision_guard"},
        ),
        near_critical_ttc_s=float(thresholds.get("near_critical_ttc_s", 2.0)),
        heatmap_bins=bins,
        output_formats=formats,
    )


def spec_to_dict(spec: AnalysisSpec) -> dict[str, Any]:
    return {
        "version": spec.version,
        "metadata": spec.metadata,
        "parameters": {
            "axes": {"x": spec.x_param, "y": spec.y_param},
            "units": spec.parameter_units,
        },
        "metrics": {
            name: {
                "summary": binding.summary,
                "series": binding.series,
                "label": binding.label,
                "unit": binding.unit,
            }
            for name, binding in spec.metrics.items()
        },
        "outcomes": {
            "success": sorted(spec.success_outcomes),
            "failure": sorted(spec.failure_outcomes),
            "invalid": sorted(spec.invalid_outcomes),
            "collision_reasons": sorted(spec.collision_reasons),
        },
        "thresholds": {"near_critical_ttc_s": spec.near_critical_ttc_s},
        "heatmap": {"bins": spec.heatmap_bins},
        "output": {"formats": list(spec.output_formats)},
    }


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _optional_str(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    return str(value)


def _string_set(value: Any, default: set[str]) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset(item.lower() for item in default)
    return frozenset(str(item).lower() for item in value)
