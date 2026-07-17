from __future__ import annotations

import pytest

from pisa_sample_tools.webapp.inline_sampling import InlineSamplerError, generate_inline_samples


@pytest.mark.parametrize("method", ["grid", "lhs", "sobol", "random"])
def test_inline_sampling_is_bounded_and_has_requested_count(method: str) -> None:
    result = generate_inline_samples(
        method=method,
        count=17,
        seed=7,
        parameters=[
            {"name": "speed", "min": 10, "max": 30},
            {"name": "gap", "min": 2, "max": 8},
        ],
    )

    assert result["count"] == 17
    assert result["parameter_names"] == ["speed", "gap"]
    assert all(10 <= row[0] <= 30 and 2 <= row[1] <= 8 for row in result["samples"])


def test_inline_sampling_is_deterministic_and_supports_discrete_values() -> None:
    request = {
        "method": "lhs",
        "count": 8,
        "seed": 19,
        "parameters": [{"name": "mode", "values": [1, 3, 9]}],
    }

    left = generate_inline_samples(**request)
    right = generate_inline_samples(**request)

    assert left == right
    assert {row[0] for row in left["samples"]} <= {1.0, 3.0, 9.0}


def test_inline_sampling_rejects_ambiguous_or_invalid_definitions() -> None:
    with pytest.raises(InlineSamplerError, match="unique"):
        generate_inline_samples(
            method="grid",
            count=4,
            parameters=[
                {"name": "speed", "min": 0, "max": 1},
                {"name": "speed", "min": 0, "max": 2},
            ],
        )
    with pytest.raises(InlineSamplerError, match="min < max"):
        generate_inline_samples(
            method="grid",
            count=4,
            parameters=[{"name": "speed", "min": 1, "max": 1}],
        )
