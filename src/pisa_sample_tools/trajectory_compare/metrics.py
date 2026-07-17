from __future__ import annotations

import math
from collections import defaultdict

from pisa_sample_tools.common.sorting import natural_key
from pisa_sample_tools.trajectory import AgentState

from .models import AgentComparison


def compare_states(
    left_states: list[AgentState],
    right_states: list[AgentState],
    *,
    ignore_agent_ids: set[str],
) -> list[AgentComparison]:
    left_by_agent = states_by_agent(left_states)
    right_by_agent = states_by_agent(right_states)
    agent_ids = sorted((set(left_by_agent) & set(right_by_agent)) - ignore_agent_ids, key=natural_key)
    comparisons: list[AgentComparison] = []
    for agent_id in agent_ids:
        left = left_by_agent[agent_id]
        right = right_by_agent[agent_id]
        aligned = align_states(left, right)
        count = len(aligned)
        if count == 0:
            continue
        distances = [math.hypot(lx - rx, ly - ry) for lx, ly, ls, rx, ry, rs in aligned]
        speed_deltas = [abs(ls - rs) for lx, ly, ls, rx, ry, rs in aligned]
        comparisons.append(
            AgentComparison(
                agent_id=agent_id,
                compared_steps=count,
                ade=sum(distances) / count,
                fde=distances[-1],
                rmse=math.sqrt(sum(distance_value * distance_value for distance_value in distances) / count),
                max_error=max(distances),
                mean_speed_delta=sum(speed_deltas) / count,
            )
        )
    return comparisons


def align_states(
    left: list[AgentState], right: list[AgentState]
) -> list[tuple[float, float, float, float, float, float]]:
    """Align two traces by simulation time without extrapolating.

    When both traces carry timestamps, both sampling grids contribute to the
    aligned timeline and linear signals are interpolated within their shared
    interval. Legacy traces without timestamps use matching step indices, then
    fall back to row order only when neither key exists.
    """

    if left and right and all(item.sim_time_ms is not None for item in (*left, *right)):
        left_points = _deduplicate_times(left)
        right_points = _deduplicate_times(right)
        lower = max(left_points[0][0], right_points[0][0])
        upper = min(left_points[-1][0], right_points[-1][0])
        if lower > upper:
            return []
        times = sorted(
            {time for time, _ in left_points + right_points if lower <= time <= upper}
        )
        return [
            (*_interpolate(left_points, time), *_interpolate(right_points, time))
            for time in times
        ]

    left_steps = {
        item.step_index: item for item in left if item.step_index is not None
    }
    right_steps = {
        item.step_index: item for item in right if item.step_index is not None
    }
    shared_steps = sorted(set(left_steps) & set(right_steps))
    if shared_steps:
        return [
            (
                left_steps[step].x,
                left_steps[step].y,
                left_steps[step].speed,
                right_steps[step].x,
                right_steps[step].y,
                right_steps[step].speed,
            )
            for step in shared_steps
        ]
    return [
        (left_item.x, left_item.y, left_item.speed, right_item.x, right_item.y, right_item.speed)
        for left_item, right_item in zip(left, right, strict=False)
    ]


def _deduplicate_times(states: list[AgentState]) -> list[tuple[float, AgentState]]:
    by_time = {float(item.sim_time_ms): item for item in states if item.sim_time_ms is not None}
    return sorted(by_time.items())


def _interpolate(points: list[tuple[float, AgentState]], time: float) -> tuple[float, float, float]:
    for index, (point_time, state) in enumerate(points):
        if point_time == time or index == len(points) - 1:
            return state.x, state.y, state.speed
        next_time, next_state = points[index + 1]
        if point_time < time < next_time:
            fraction = (time - point_time) / (next_time - point_time)
            return (
                state.x + (next_state.x - state.x) * fraction,
                state.y + (next_state.y - state.y) * fraction,
                state.speed + (next_state.speed - state.speed) * fraction,
            )
    raise ValueError("alignment requested outside the trace interval")


def states_by_agent(states: list[AgentState]) -> dict[str, list[AgentState]]:
    grouped: dict[str, list[AgentState]] = defaultdict(list)
    for state in states:
        grouped[state.agent_id].append(state)
    return {agent_id: sorted(values, key=state_time_key) for agent_id, values in grouped.items()}


def state_time_key(state: AgentState) -> tuple[float, int, float, float]:
    time_value = state.sim_time_ms if state.sim_time_ms is not None else math.inf
    step_value = state.step_index if state.step_index is not None else 10**12
    return (time_value, step_value, state.x, state.y)


def distance(left: AgentState, right: AgentState) -> float:
    return math.hypot(left.x - right.x, left.y - right.y)
