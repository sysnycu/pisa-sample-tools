from __future__ import annotations


def mean(values) -> float | None:
    values = list(values)
    if not values:
        return None
    return sum(values) / len(values)


def weighted_mean(values_and_weights) -> float | None:
    total = 0.0
    weight_sum = 0
    for value, weight in values_and_weights:
        if value is None or weight <= 0:
            continue
        total += value * weight
        weight_sum += weight
    if weight_sum == 0:
        return None
    return total / weight_sum

