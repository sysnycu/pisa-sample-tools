from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.stats import qmc


class InlineSamplerError(ValueError):
    """Raised when a browser-authored sampler definition is invalid."""


@dataclass(frozen=True)
class InlineParameter:
    name: str
    minimum: float
    maximum: float
    values: tuple[float, ...] = ()


def generate_inline_samples(
    *,
    method: str,
    count: int,
    parameters: list[dict[str, Any]],
    seed: int | None = None,
) -> dict[str, Any]:
    """Generate deterministic UI previews without creating source files.

    This is deliberately a preview/sample-table facility. Runner-ready exports
    continue to use simcore and a runner/sampler spec so scenario assets and
    provenance cannot be silently invented by the browser.
    """

    if not 1 <= count <= 100_000:
        raise InlineSamplerError("count must be between 1 and 100000")
    resolved = [_parameter(item, index) for index, item in enumerate(parameters)]
    if not resolved:
        raise InlineSamplerError("at least one parameter is required")
    if len({item.name for item in resolved}) != len(resolved):
        raise InlineSamplerError("parameter names must be unique")

    normalized_method = method.strip().lower()
    warnings: list[str] = []
    if normalized_method == "grid":
        unit = _grid(count, len(resolved))
    elif normalized_method == "lhs":
        unit = qmc.LatinHypercube(d=len(resolved), seed=seed).random(count)
    elif normalized_method == "sobol":
        exponent = math.ceil(math.log2(count)) if count > 1 else 0
        unit = qmc.Sobol(d=len(resolved), scramble=True, seed=seed).random_base2(exponent)
        unit = unit[:count]
        if count & (count - 1):
            warnings.append(
                "Sobol balance properties are strongest at powers of two; the preview was truncated."
            )
    elif normalized_method == "random":
        unit = np.random.default_rng(seed).random((count, len(resolved)))
    else:
        raise InlineSamplerError(
            "inline preview supports grid, lhs, sobol, and random; use a source file for native or explicit samplers"
        )

    samples = [
        [_scale(float(row[index]), parameter) for index, parameter in enumerate(resolved)]
        for row in unit
    ]
    return {
        "method": normalized_method,
        "count": len(samples),
        "parameter_names": [item.name for item in resolved],
        "samples": samples,
        "warnings": warnings,
    }


def _parameter(value: dict[str, Any], index: int) -> InlineParameter:
    name = str(value.get("name") or "").strip()
    if not name:
        raise InlineSamplerError(f"parameters.{index}.name is required")
    raw_values = value.get("values")
    if raw_values not in (None, []):
        if not isinstance(raw_values, list) or not raw_values:
            raise InlineSamplerError(f"parameters.{index}.values must be a non-empty list")
        try:
            choices = tuple(float(item) for item in raw_values)
        except (TypeError, ValueError) as exc:
            raise InlineSamplerError(f"parameters.{index}.values must be numeric") from exc
        return InlineParameter(name, min(choices), max(choices), choices)
    try:
        minimum = float(value.get("min"))
        maximum = float(value.get("max"))
    except (TypeError, ValueError) as exc:
        raise InlineSamplerError(f"parameters.{index} min/max must be numeric") from exc
    if not math.isfinite(minimum) or not math.isfinite(maximum) or minimum >= maximum:
        raise InlineSamplerError(f"parameters.{index} requires finite min < max")
    return InlineParameter(name, minimum, maximum)


def _grid(count: int, dimensions: int) -> np.ndarray:
    side = max(1, math.ceil(count ** (1 / dimensions)))
    axes = [np.linspace(0.0, 1.0, side) for _ in range(dimensions)]
    rows = itertools.islice(itertools.product(*axes), count)
    return np.asarray(list(rows), dtype=float)


def _scale(value: float, parameter: InlineParameter) -> float:
    if parameter.values:
        index = min(int(value * len(parameter.values)), len(parameter.values) - 1)
        return parameter.values[index]
    return parameter.minimum + value * (parameter.maximum - parameter.minimum)
