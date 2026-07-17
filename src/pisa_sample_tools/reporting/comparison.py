from __future__ import annotations

from typing import Any

from .models import ComparisonAssessment, ComparisonRole

NON_BEHAVIOR_RUNNER_SPEC_PATHS = frozenset({"task.output_dir"})


def classify_comparison(
    *,
    left_count: int,
    right_count: int,
    matched_count: int,
    exact_duplicate: bool = False,
    semantic_compatible: bool | None = None,
    system_changed: bool = False,
    policy_changed: bool = False,
    replicate: bool = False,
    common_parameter_domain: bool = False,
) -> ComparisonAssessment:
    """Classify what claims are justified for a dataset comparison.

    Pairing and semantic compatibility are intentionally independent.  A shared
    parameter hash is not enough to claim a controlled comparison when semantic
    provenance is unknown or incompatible.
    """

    if min(left_count, right_count, matched_count) < 0:
        raise ValueError("comparison counts must be non-negative")
    if matched_count > min(left_count, right_count):
        raise ValueError("matched_count cannot exceed either dataset count")

    left_only = left_count - matched_count
    right_only = right_count - matched_count
    warnings: list[str] = []

    if exact_duplicate:
        role = ComparisonRole.DUPLICATE_ALIAS
        reason = "The canonical run sets are identical and must not be counted twice."
    elif semantic_compatible is False:
        role = ComparisonRole.INCOMPATIBLE
        reason = "Recorded behavior-affecting provenance is incompatible."
    elif matched_count and (left_only or right_only):
        role = ComparisonRole.PARTIAL_PAIR
        reason = "Only a subset of canonical inputs is paired."
        if semantic_compatible is None:
            warnings.append("Pairing exists, but semantic compatibility is unconfirmed.")
    elif matched_count:
        if semantic_compatible is None:
            role = ComparisonRole.DESCRIPTIVE_ONLY
            reason = "Inputs pair, but semantic compatibility is unconfirmed."
            warnings.append("Do not make intervention or replicate claims.")
        elif policy_changed:
            role = ComparisonRole.PAIRED_POLICY_INTERVENTION
            reason = "Inputs pair and the recorded policy is the intended intervention."
        elif system_changed:
            role = ComparisonRole.PAIRED_SYSTEM_INTERVENTION
            reason = "Inputs pair and the recorded system is the intended intervention."
        elif replicate or semantic_compatible:
            role = ComparisonRole.PAIRED_REPLICATE
            reason = "Inputs pair and behavior-affecting provenance is compatible."
        else:  # pragma: no cover - retained for forward-compatible booleans
            role = ComparisonRole.DESCRIPTIVE_ONLY
            reason = "The comparison is descriptive."
    elif common_parameter_domain:
        role = ComparisonRole.UNPAIRED_COMMON_DOMAIN
        reason = "Datasets share a parameter domain but have no verified input pairs."
    else:
        role = ComparisonRole.DESCRIPTIVE_ONLY
        reason = "No verified pairing or common-domain relationship was supplied."

    return ComparisonAssessment(
        role=role,
        matched_count=matched_count,
        left_count=left_count,
        right_count=right_count,
        left_only_count=left_only,
        right_only_count=right_only,
        semantic_compatible=semantic_compatible,
        reason=reason,
        warnings=tuple(warnings),
    )


def semantic_projection(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return behavior-affecting manifest fields used by comparison callers."""

    resolved = _mapping(manifest.get("resolved_input_sha256"))
    components = _mapping(manifest.get("components"))
    execution = _mapping(manifest.get("execution"))
    return {
        "scenario": resolved.get("scenario"),
        "map": resolved.get("map_xodr") or resolved.get("map_osm"),
        "stop_conditions": resolved.get("stop_conditions"),
        "monitor_config": resolved.get("monitor_config"),
        "sampler_config": resolved.get("sampler_config"),
        "sampler_source": resolved.get("sampler_source"),
        "simulator": _component_projection(components.get("simulator")),
        "av": _component_projection(components.get("av")),
        "dt": manifest.get("dt"),
        "sampler_name": execution.get("sampler_name"),
        "observation_identity": execution.get("observation_identity"),
        "observation_order": execution.get("observation_order"),
    }


def semantic_compatibility(
    left_manifest: dict[str, Any], right_manifest: dict[str, Any]
) -> tuple[bool | None, dict[str, tuple[Any, Any]]]:
    """Compare recorded semantics without guessing through missing provenance."""

    left = semantic_projection(left_manifest)
    right = semantic_projection(right_manifest)
    differences = {
        key: (left.get(key), right.get(key))
        for key in sorted(left)
        if left.get(key) != right.get(key)
    }
    missing = any(value is None for value in left.values()) or any(
        value is None for value in right.values()
    )
    if differences:
        return False, differences
    return (None if missing else True), {}


def structured_diff(
    left: Any,
    right: Any,
    *,
    ignored_paths: frozenset[str] = NON_BEHAVIOR_RUNNER_SPEC_PATHS,
    _prefix: str = "",
) -> dict[str, tuple[Any, Any]]:
    """Produce a deterministic leaf diff with a narrow explicit ignore-list."""

    if _prefix in ignored_paths:
        return {}
    if isinstance(left, dict) and isinstance(right, dict):
        result: dict[str, tuple[Any, Any]] = {}
        for key in sorted(set(left) | set(right), key=str):
            path = f"{_prefix}.{key}" if _prefix else str(key)
            result.update(
                structured_diff(
                    left.get(key), right.get(key), ignored_paths=ignored_paths, _prefix=path
                )
            )
        return result
    if left != right:
        return {_prefix or "$": (left, right)}
    return {}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _component_projection(value: Any) -> dict[str, Any] | None:
    descriptor = _mapping(value)
    if not descriptor:
        return None
    wrapper = _mapping(descriptor.get("wrapper"))
    component = _mapping(descriptor.get("component"))
    return {
        "wrapper_name": wrapper.get("name"),
        "wrapper_version": wrapper.get("version"),
        "component_name": component.get("name"),
    }
