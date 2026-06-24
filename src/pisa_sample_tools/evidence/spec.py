from __future__ import annotations

from pathlib import Path
from typing import Any

from pisa_sample_tools.common.yaml import load_mapping_file

from .models import AnalysisSpec, DerivedParameter, EvidenceError, MetricBinding

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

CANONICAL_OUTCOMES = {"success", "failure", "invalid", "execution_error", "unclassified"}
DERIVED_OPERATIONS = {"add", "subtract", "multiply", "divide"}


def load_analysis_spec(path: Path | None) -> AnalysisSpec:
    if path is None:
        return AnalysisSpec(metrics=dict(DEFAULT_METRICS))
    raw = load_mapping_file(path, label="analysis spec", error_type=EvidenceError)
    version = int(raw.get("version", 1))
    if version not in {1, 2}:
        raise EvidenceError(f"unsupported analysis spec version: {version}")

    parameter_config = _mapping(raw.get("parameters"))
    axes = _mapping(parameter_config.get("axes"))
    validation = _mapping(raw.get("validation"))
    default_validation = "strict" if version == 2 else "permissive"
    validation_mode = str(validation.get("mode", default_validation)).lower()
    if validation_mode not in {"strict", "permissive"}:
        raise EvidenceError("validation mode must be 'strict' or 'permissive'")
    parameter_mode = str(
        parameter_config.get("mode", "all_pairwise" if version == 2 else "single")
    ).lower()
    if parameter_mode not in {"single", "all_pairwise"}:
        raise EvidenceError("parameters.mode must be 'single' or 'all_pairwise'")

    units = _string_mapping(parameter_config.get("units"))
    labels = _string_mapping(parameter_config.get("labels"))
    derived = _load_derived_parameters(parameter_config.get("derived"))
    for name, definition in derived.items():
        if definition.unit:
            units.setdefault(name, definition.unit)
        if definition.label:
            labels.setdefault(name, definition.label)

    metric_config = _mapping(raw.get("metrics"))
    metrics = dict(DEFAULT_METRICS)
    for name, value in metric_config.items():
        if isinstance(value, str):
            metrics[str(name)] = MetricBinding(summary=value)
            continue
        config = _mapping(value)
        missing = str(config.get("missing", "unavailable")).lower()
        if missing != "unavailable":
            raise EvidenceError(f"metrics.{name}.missing currently supports only 'unavailable'")
        metrics[str(name)] = MetricBinding(
            summary=_optional_str(config.get("summary")),
            series=_optional_str(config.get("series")),
            label=_optional_str(config.get("label")),
            unit=_optional_str(config.get("unit")),
            missing=missing,
        )

    outcomes = _mapping(raw.get("outcomes"))
    termination_outcomes = {
        str(key).lower(): str(value).lower()
        for key, value in _mapping(outcomes.get("termination")).items()
    }
    invalid_mappings = sorted(set(termination_outcomes.values()) - CANONICAL_OUTCOMES)
    if invalid_mappings:
        raise EvidenceError(
            "unsupported termination outcome mapping(s): " + ", ".join(invalid_mappings)
        )

    thresholds = _mapping(raw.get("thresholds"))
    heatmap = _mapping(raw.get("heatmap"))
    comparison = _mapping(raw.get("comparison"))
    output = _mapping(raw.get("output"))
    formats = tuple(str(value).lower() for value in output.get("formats", ["svg", "png"]))
    unknown_formats = sorted(set(formats) - {"svg", "png"})
    if unknown_formats:
        raise EvidenceError(f"unsupported output format(s): {', '.join(unknown_formats)}")
    bins = int(heatmap.get("bins", 30))
    min_bin_count = int(heatmap.get("min_bin_count", 1))
    if bins <= 0 or min_bin_count <= 0:
        raise EvidenceError("heatmap bins and min_bin_count must be greater than 0")
    pairing_mode = str(comparison.get("pairing", "sample_id_then_parameters")).lower()
    if pairing_mode not in {"sample_id_then_parameters", "parameters"}:
        raise EvidenceError(
            "comparison.pairing must be 'sample_id_then_parameters' or 'parameters'"
        )
    tolerance = float(comparison.get("parameter_tolerance", 1e-9))
    bootstrap_samples = int(comparison.get("bootstrap_samples", 2000))
    if tolerance < 0 or bootstrap_samples < 0:
        raise EvidenceError("comparison tolerance and bootstrap_samples cannot be negative")

    return AnalysisSpec(
        version=version,
        validation_mode=validation_mode,
        metadata=_mapping(raw.get("metadata")),
        parameter_mode=parameter_mode,
        parameter_include=_string_tuple(parameter_config.get("include")),
        parameter_exclude=frozenset(_string_tuple(parameter_config.get("exclude"))),
        x_param=_optional_str(axes.get("x")),
        y_param=_optional_str(axes.get("y")),
        parameter_units=units,
        parameter_labels=labels,
        derived_parameters=derived,
        metrics=metrics,
        success_outcomes=_string_set(outcomes.get("success"), {"success"}),
        failure_outcomes=_string_set(
            outcomes.get("failure"),
            {"fail", "failure", "failed", "test_fail", "collision"},
        ),
        invalid_outcomes=_string_set(outcomes.get("invalid"), {"invalid"}),
        collision_reasons=_string_set(
            outcomes.get("collision_reasons"), {"collision", "collision_guard"}
        ),
        termination_outcomes=termination_outcomes,
        near_critical_ttc_s=float(thresholds.get("near_critical_ttc_s", 2.0)),
        heatmap_bins=bins,
        heatmap_min_bin_count=min_bin_count,
        pairing_mode=pairing_mode,
        pairing_parameter_tolerance=tolerance,
        bootstrap_samples=bootstrap_samples,
        bootstrap_seed=int(comparison.get("bootstrap_seed", 0)),
        output_formats=formats,
    )


def spec_to_dict(spec: AnalysisSpec) -> dict[str, Any]:
    return {
        "version": spec.version,
        "validation": {"mode": spec.validation_mode},
        "metadata": spec.metadata,
        "parameters": {
            "mode": spec.parameter_mode,
            "include": list(spec.parameter_include),
            "exclude": sorted(spec.parameter_exclude),
            "axes": {"x": spec.x_param, "y": spec.y_param},
            "units": spec.parameter_units,
            "labels": spec.parameter_labels,
            "derived": {
                name: {
                    "operation": item.operation,
                    "left": item.left,
                    "right": item.right,
                    "label": item.label,
                    "unit": item.unit,
                }
                for name, item in spec.derived_parameters.items()
            },
        },
        "metrics": {
            name: {
                "summary": binding.summary,
                "series": binding.series,
                "label": binding.label,
                "unit": binding.unit,
                "missing": binding.missing,
            }
            for name, binding in spec.metrics.items()
        },
        "outcomes": {
            "success": sorted(spec.success_outcomes),
            "failure": sorted(spec.failure_outcomes),
            "invalid": sorted(spec.invalid_outcomes),
            "collision_reasons": sorted(spec.collision_reasons),
            "termination": dict(sorted(spec.termination_outcomes.items())),
        },
        "thresholds": {"near_critical_ttc_s": spec.near_critical_ttc_s},
        "heatmap": {
            "bins": spec.heatmap_bins,
            "min_bin_count": spec.heatmap_min_bin_count,
        },
        "comparison": {
            "pairing": spec.pairing_mode,
            "parameter_tolerance": spec.pairing_parameter_tolerance,
            "bootstrap_samples": spec.bootstrap_samples,
            "bootstrap_seed": spec.bootstrap_seed,
        },
        "output": {"formats": list(spec.output_formats)},
    }


def _load_derived_parameters(value: Any) -> dict[str, DerivedParameter]:
    loaded: dict[str, DerivedParameter] = {}
    for name, raw in _mapping(value).items():
        config = _mapping(raw)
        operation = str(config.get("operation", "")).lower()
        if operation not in DERIVED_OPERATIONS:
            raise EvidenceError(
                f"parameters.derived.{name}.operation must be one of "
                + ", ".join(sorted(DERIVED_OPERATIONS))
            )
        left = _optional_str(config.get("left"))
        right = _optional_str(config.get("right"))
        if left is None or right is None:
            raise EvidenceError(f"parameters.derived.{name} must define left and right")
        loaded[str(name)] = DerivedParameter(
            operation=operation,
            left=left,
            right=right,
            label=_optional_str(config.get("label")),
            unit=_optional_str(config.get("unit")),
        )
    return loaded


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _string_mapping(value: Any) -> dict[str, str]:
    return {str(key): str(item) for key, item in _mapping(value).items()}


def _optional_str(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    return str(value)


def _string_set(value: Any, default: set[str]) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset(item.lower() for item in default)
    return frozenset(str(item).lower() for item in value)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)
