from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pisa_sample_tools.evidence.axes import axis_rule_for, resolve_axis_limits
from pisa_sample_tools.evidence.models import AnalysisSpec, AxisRule, EvidenceError
from pisa_sample_tools.evidence.spec import load_analysis_spec


def test_steer_uses_fixed_semantic_range_and_autoscales_detail() -> None:
    spec = load_analysis_spec(None)
    rule = axis_rule_for(spec, "steer")

    semantic = resolve_axis_limits([-0.000004, 0.000006], rule)
    detail = resolve_axis_limits([-0.000004, 0.000006], rule, detail=True)

    assert (semantic.lower, semantic.upper) == (-1.0, 1.0)
    assert detail.lower > -1.0
    assert detail.upper < 1.0


def test_fixed_axis_expands_instead_of_clipping_out_of_range_values() -> None:
    limits = resolve_axis_limits(
        [-1.4, 1.2],
        AxisRule(policy="fixed", lower=-1.0, upper=1.0),
    )

    assert limits.lower <= -1.4
    assert limits.upper >= 1.2
    assert limits.nominal_lower == -1.0
    assert limits.nominal_upper == 1.0
    assert limits.out_of_range is True


def test_speed_axis_includes_zero_and_preserves_reverse_values() -> None:
    rule = AxisRule(policy="include_zero")

    forward = resolve_axis_limits([15.0, 18.0], rule)
    reverse = resolve_axis_limits([-3.0, 5.0], rule)

    assert forward.lower == 0.0
    assert forward.upper >= 18.0
    assert reverse.lower <= -3.0
    assert reverse.upper >= 5.0


def test_acceleration_axis_is_symmetric_and_handles_all_zero() -> None:
    rule = AxisRule(policy="symmetric_zero", minimum_span=2.0)

    varying = resolve_axis_limits([-0.2, 0.4], rule)
    zero = resolve_axis_limits([0.0, 0.0], rule)

    assert varying.lower == -varying.upper
    assert varying.upper >= 0.4
    assert (zero.lower, zero.upper) == (-1.0, 1.0)


def test_analysis_spec_can_override_builtin_axis_rule(tmp_path: Path) -> None:
    path = tmp_path / "analysis.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "visualization": {
                    "axes": {
                        "padding_fraction": 0.1,
                        "fields": {
                            "steer": {
                                "policy": "fixed",
                                "detail_policy": "include_zero",
                                "min": -0.5,
                                "max": 0.5,
                            }
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    spec = load_analysis_spec(path)

    assert spec.visualization_axes["steer"].lower == -0.5
    assert spec.visualization_axes["steer"].upper == 0.5
    assert spec.visualization_axes["steer"].detail_policy == "include_zero"
    assert spec.visualization_axes["speed"].padding_fraction == 0.1


def test_analysis_spec_rejects_invalid_fixed_axis(tmp_path: Path) -> None:
    path = tmp_path / "analysis.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "visualization": {
                    "axes": {
                        "fields": {
                            "custom": {"policy": "fixed", "min": 1, "max": 1}
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(EvidenceError, match="requires min < max"):
        load_analysis_spec(path)


def test_unknown_field_keeps_auto_policy() -> None:
    rule = axis_rule_for(AnalysisSpec(), "custom.signal")

    limits = resolve_axis_limits([10.0, 12.0], rule)

    assert limits.policy == "auto"
    assert limits.lower < 10.0
    assert limits.upper > 12.0
