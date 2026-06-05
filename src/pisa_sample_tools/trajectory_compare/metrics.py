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
        count = min(len(left), len(right))
        if count == 0:
            continue
        distances = [distance(left[index], right[index]) for index in range(count)]
        speed_deltas = [abs(left[index].speed - right[index].speed) for index in range(count)]
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

