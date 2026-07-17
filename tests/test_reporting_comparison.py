from __future__ import annotations

import pytest

from pisa_sample_tools.reporting import (
    ComparisonRole,
    classify_comparison,
    semantic_compatibility,
    structured_diff,
)
from pisa_sample_tools.reporting.index import _pair_semantics


@pytest.mark.parametrize(
    ("arguments", "role"),
    [
        (
            {"left_count": 10, "right_count": 10, "matched_count": 10, "exact_duplicate": True},
            ComparisonRole.DUPLICATE_ALIAS,
        ),
        (
            {
                "left_count": 10,
                "right_count": 10,
                "matched_count": 10,
                "semantic_compatible": True,
                "replicate": True,
            },
            ComparisonRole.PAIRED_REPLICATE,
        ),
        (
            {
                "left_count": 10,
                "right_count": 10,
                "matched_count": 10,
                "semantic_compatible": True,
                "system_changed": True,
            },
            ComparisonRole.PAIRED_SYSTEM_INTERVENTION,
        ),
        (
            {
                "left_count": 10,
                "right_count": 10,
                "matched_count": 10,
                "semantic_compatible": True,
                "policy_changed": True,
            },
            ComparisonRole.PAIRED_POLICY_INTERVENTION,
        ),
        (
            {"left_count": 10, "right_count": 8, "matched_count": 8, "semantic_compatible": True},
            ComparisonRole.PARTIAL_PAIR,
        ),
        (
            {
                "left_count": 10,
                "right_count": 10,
                "matched_count": 0,
                "common_parameter_domain": True,
            },
            ComparisonRole.UNPAIRED_COMMON_DOMAIN,
        ),
        (
            {"left_count": 10, "right_count": 10, "matched_count": 10},
            ComparisonRole.DESCRIPTIVE_ONLY,
        ),
        (
            {
                "left_count": 10,
                "right_count": 10,
                "matched_count": 10,
                "semantic_compatible": False,
            },
            ComparisonRole.INCOMPATIBLE,
        ),
    ],
)
def test_comparison_roles(arguments: dict[str, object], role: ComparisonRole) -> None:
    assessment = classify_comparison(**arguments)  # type: ignore[arg-type]
    assert assessment.role is role
    assert assessment.model_dump()["role"] == role.value


def test_comparison_rejects_impossible_pair_count() -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        classify_comparison(left_count=2, right_count=3, matched_count=3)


def test_semantic_compatibility_is_unknown_when_provenance_is_missing() -> None:
    manifest = {"dt": 0.05}
    compatible, differences = semantic_compatibility(manifest, manifest)
    assert compatible is None
    assert differences == {}


def test_structured_diff_ignores_only_explicit_nonbehavior_field() -> None:
    left = {"task": {"output_dir": "/a"}, "system": {"name": "simple"}}
    right = {"task": {"output_dir": "/b"}, "system": {"name": "autoware"}}
    assert structured_diff(left, right) == {"system.name": ("simple", "autoware")}


def _complete_semantic_manifest() -> dict[str, object]:
    return {
        "dt": 0.05,
        "resolved_input_sha256": {
            "scenario": "scenario",
            "map_xodr": "map",
            "stop_conditions": "stops",
            "monitor_config": "monitor",
            "sampler_config": "sampler-config",
            "sampler_source": "sampler-source",
            "simulator_config": "sim-config",
            "av_config": "av-config",
        },
        "execution": {
            "sampler_name": "lhs",
            "observation_identity": "full",
            "observation_order": "stable",
        },
        "components": {
            "simulator": {
                "wrapper": {"name": "esmini-wrapper", "version": "1"},
                "component": {"name": "esmini"},
            },
            "av": {
                "wrapper": {"name": "simple-av-wrapper", "version": "1"},
                "component": {"name": "simple-av"},
            },
        },
    }


@pytest.mark.parametrize(
    ("missing_section", "missing_key"),
    [
        ("components", "simulator"),
        ("components", "av"),
        ("resolved_input_sha256", "simulator_config"),
        ("resolved_input_sha256", "av_config"),
        ("resolved_input_sha256", "sampler_config"),
        ("resolved_input_sha256", "sampler_source"),
        ("execution", "sampler_name"),
    ],
)
def test_pair_semantics_does_not_infer_claims_from_missing_component_or_sampler_provenance(
    missing_section: str, missing_key: str
) -> None:
    left = _complete_semantic_manifest()
    right = _complete_semantic_manifest()
    section = right[missing_section]
    assert isinstance(section, dict)
    del section[missing_key]

    compatible, _differences, system_changed, policy_changed = _pair_semantics(left, right)

    assert compatible is None
    assert system_changed is False
    assert policy_changed is False


def test_pair_semantics_recognizes_intervention_only_with_complete_provenance() -> None:
    left = _complete_semantic_manifest()
    right = _complete_semantic_manifest()
    resolved = right["resolved_input_sha256"]
    assert isinstance(resolved, dict)
    resolved["av_config"] = "changed-av-config"

    compatible, differences, system_changed, policy_changed = _pair_semantics(left, right)

    assert compatible is True
    assert differences == {"av_config": ("av-config", "changed-av-config")}
    assert system_changed is False
    assert policy_changed is True
