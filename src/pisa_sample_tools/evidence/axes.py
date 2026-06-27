from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .models import AnalysisSpec, AxisRule

AXIS_POLICIES = {"auto", "fixed", "include_zero", "nonnegative", "symmetric_zero"}

SERIES_PRESENTATION: dict[str, tuple[str, str | None]] = {
    "min_ttc": ("Minimum TTC", "s"),
    "min_distance": ("Minimum distance", "m"),
    "ego.speed": ("Ego speed", "m/s"),
    "ego.acceleration": ("Ego acceleration", "m/s^2"),
    "throttle": ("Throttle command", None),
    "brake": ("Brake command", None),
    "steer": ("Steer command", None),
    "speed": ("Speed command", "m/s"),
    "acceleration": ("Acceleration command", "m/s^2"),
    "steering_angle": ("Steering angle", "rad"),
    "steering_angle_velocity": ("Steering angular velocity", "rad/s"),
    "jerk": ("Jerk command", "m/s^3"),
}


@dataclass(frozen=True)
class AxisLimits:
    lower: float
    upper: float
    policy: str
    nominal_lower: float | None = None
    nominal_upper: float | None = None
    out_of_range: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "lower": self.lower,
            "upper": self.upper,
            "policy": self.policy,
            "nominal_lower": self.nominal_lower,
            "nominal_upper": self.nominal_upper,
            "out_of_range": self.out_of_range,
        }


def default_axis_rules(padding_fraction: float = 0.08) -> dict[str, AxisRule]:
    shared = {"padding_fraction": padding_fraction, "shared_across_cases": True}
    return {
        "steer": AxisRule(
            policy="fixed",
            detail_policy="auto",
            lower=-1.0,
            upper=1.0,
            minimum_span=0.02,
            **shared,
        ),
        "throttle": AxisRule(
            policy="fixed",
            detail_policy="include_zero",
            lower=0.0,
            upper=1.0,
            **shared,
        ),
        "brake": AxisRule(
            policy="fixed",
            detail_policy="include_zero",
            lower=0.0,
            upper=1.0,
            **shared,
        ),
        "speed": AxisRule(policy="include_zero", detail_policy="include_zero", **shared),
        "ego.speed": AxisRule(policy="include_zero", detail_policy="include_zero", **shared),
        "acceleration": AxisRule(
            policy="symmetric_zero",
            detail_policy="symmetric_zero",
            minimum_span=2.0,
            **shared,
        ),
        "ego.acceleration": AxisRule(
            policy="symmetric_zero",
            detail_policy="symmetric_zero",
            minimum_span=2.0,
            **shared,
        ),
        "steering_angle": AxisRule(
            policy="symmetric_zero", detail_policy="symmetric_zero", **shared
        ),
        "steering_angle_velocity": AxisRule(
            policy="symmetric_zero", detail_policy="symmetric_zero", **shared
        ),
        "jerk": AxisRule(policy="symmetric_zero", detail_policy="symmetric_zero", **shared),
        "min_ttc": AxisRule(
            policy="nonnegative",
            detail_policy="nonnegative",
            padding_fraction=padding_fraction,
        ),
        "min_distance": AxisRule(
            policy="nonnegative",
            detail_policy="nonnegative",
            padding_fraction=padding_fraction,
        ),
    }


def axis_rule_for(spec: AnalysisSpec, field: str, semantic_name: str | None = None) -> AxisRule:
    defaults = default_axis_rules()
    return (
        spec.visualization_axes.get(field)
        or (spec.visualization_axes.get(semantic_name) if semantic_name else None)
        or defaults.get(field)
        or (defaults.get(semantic_name) if semantic_name else None)
        or AxisRule()
    )


def resolve_axis_limits(
    values: list[float],
    rule: AxisRule,
    *,
    detail: bool = False,
) -> AxisLimits:
    finite = [float(value) for value in values if math.isfinite(value)]
    policy = rule.detail_policy if detail else rule.policy
    if not finite:
        return AxisLimits(-0.5, 0.5, policy)
    lower_data, upper_data = min(finite), max(finite)
    if policy == "fixed":
        assert rule.lower is not None and rule.upper is not None
        nominal_lower, nominal_upper = rule.lower, rule.upper
        out_of_range = lower_data < nominal_lower or upper_data > nominal_upper
        lower = nominal_lower
        upper = nominal_upper
        if lower_data < nominal_lower:
            lower = _nice_floor(lower_data - abs(lower_data) * rule.padding_fraction)
        if upper_data > nominal_upper:
            upper = _nice_ceiling(upper_data + abs(upper_data) * rule.padding_fraction)
        return AxisLimits(
            lower,
            upper,
            policy,
            nominal_lower=nominal_lower,
            nominal_upper=nominal_upper,
            out_of_range=out_of_range,
        )
    if policy == "symmetric_zero":
        magnitude = max(abs(lower_data), abs(upper_data))
        if math.isclose(magnitude, 0.0):
            magnitude = (rule.minimum_span or 1.0) / 2.0
        else:
            magnitude *= 1.0 + rule.padding_fraction
        magnitude = _nice_ceiling(magnitude)
        return AxisLimits(-magnitude, magnitude, policy)
    if policy in {"include_zero", "nonnegative"}:
        nominal_lower = 0.0 if policy == "nonnegative" else None
        out_of_range = policy == "nonnegative" and lower_data < 0
        lower = min(0.0, lower_data)
        upper = max(0.0, upper_data)
        if lower < 0:
            lower = _nice_floor(lower - abs(lower) * rule.padding_fraction)
        if upper > 0:
            upper = _nice_ceiling(upper + abs(upper) * rule.padding_fraction)
        if math.isclose(lower, upper):
            upper = (rule.minimum_span or 1.0)
        return AxisLimits(
            lower,
            upper,
            policy,
            nominal_lower=nominal_lower,
            out_of_range=out_of_range,
        )
    return _auto_limits(lower_data, upper_data, rule)


def series_presentation(
    field: str,
    *,
    semantic_name: str | None = None,
    configured_label: str | None = None,
    configured_unit: str | None = None,
) -> tuple[str, str | None]:
    default_label, default_unit = SERIES_PRESENTATION.get(
        semantic_name or field,
        SERIES_PRESENTATION.get(field, (field.replace("_", " ").title(), None)),
    )
    return configured_label or default_label, configured_unit or default_unit


def _auto_limits(lower_data: float, upper_data: float, rule: AxisRule) -> AxisLimits:
    if math.isclose(lower_data, upper_data):
        span = rule.minimum_span or max(abs(lower_data) * 0.2, 1.0)
        return AxisLimits(lower_data - span / 2.0, upper_data + span / 2.0, "auto")
    padding = (upper_data - lower_data) * rule.padding_fraction
    return AxisLimits(lower_data - padding, upper_data + padding, "auto")


def _nice_ceiling(value: float) -> float:
    if value <= 0:
        return 0.0
    exponent = math.floor(math.log10(value))
    scale = 10.0**exponent
    fraction = value / scale
    nice = next(candidate for candidate in (1.0, 2.0, 5.0, 10.0) if fraction <= candidate)
    return nice * scale


def _nice_floor(value: float) -> float:
    return -_nice_ceiling(abs(value)) if value < 0 else 0.0
