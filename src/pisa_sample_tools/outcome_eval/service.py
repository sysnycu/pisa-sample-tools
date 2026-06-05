from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from simcore.conditions import ConditionCode, ConditionNode, EvaluationResult
from simcore.conditions.logical_nodes import AndNode, OrNode
from simcore.metrics.expressions import evaluate_numeric_expression
from simcore.metrics.rules import NumericRule

from pisa_sample_tools.common.sorting import natural_key

from .logs import ScenarioLog, discover_scenario_logs
from .models import (
    OUTCOME_ALIASES,
    OutcomeEvalError,
    OutcomeEvalMode,
    OutcomeEvalResult,
    ScenarioOutcome,
)
from .output import (
    condition_code_label,
    prepare_output_dir,
    write_manifest,
    write_summary_csv,
)
from .output import (
    write_monitor_outcome as write_monitor_outcome_file,
)


class OfflineCondition(ConditionNode):
    """ConditionNode variant evaluated from one completed ScenarioLog."""

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.config = config
        self._context: ScenarioLog | None = None

    def put(self, data) -> None:
        if not isinstance(data, ScenarioLog):
            raise OutcomeEvalError("offline conditions expect ScenarioLog data")
        self._context = data
        return None

    def reset(self) -> None:
        self._context = None
        return None

    def _scenario_log(self) -> ScenarioLog:
        if self._context is None:
            raise OutcomeEvalError("offline condition was evaluated before put()")
        return self._context


class AgentStateThresholdCondition(OfflineCondition):
    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.agent_ids = _parse_agent_ids(config)
        self.metric = _required_str(config, "metric")
        self.rule = _parse_numeric_rule(config)

    def evaluate(self) -> EvaluationResult:
        context = self._scenario_log()
        rows = _agent_state_rows_for_agents(context, self.agent_ids)
        _require_column(rows, self.metric, context.agent_states_path)
        values = _row_values(rows, self.metric, source="agent_states.csv")
        return _evaluate_values(
            self,
            values,
            rule=self.rule,
            value_name=self.metric,
            source="agent_states.csv",
        )


class FrameMetricThresholdCondition(OfflineCondition):
    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.metric = _required_str(config, "metric")
        self.rule = _parse_numeric_rule(config)

    def evaluate(self) -> EvaluationResult:
        context = self._scenario_log()
        rows = context.frame_metric_rows()
        _require_column(rows, self.metric, context.frame_metrics_path)
        values = _row_values(rows, self.metric, source="frame_metrics.csv")
        return _evaluate_values(
            self,
            values,
            rule=self.rule,
            value_name=self.metric,
            source="frame_metrics.csv",
        )


class ResultMetricThresholdCondition(OfflineCondition):
    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.metric = _required_str(config, "metric")
        self.rule = _parse_numeric_rule(config)

    def evaluate(self) -> EvaluationResult:
        context = self._scenario_log()
        rows = context.result_rows()
        _require_column(rows, self.metric, context.result_path)
        values = _row_values(rows, self.metric, source="result.csv")
        return _evaluate_values(
            self,
            values,
            rule=self.rule,
            value_name=self.metric,
            source="result.csv",
        )


class AgentStateExpressionCondition(OfflineCondition):
    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.agent_ids = _parse_agent_ids(config)
        self.expression = _required_str(config, "expression", fallback_keys=("expr",))
        self.rule = _parse_optional_numeric_rule(config)

    def evaluate(self) -> EvaluationResult:
        context = self._scenario_log()
        rows = _agent_state_rows_for_agents(context, self.agent_ids)
        values = _expression_values(self, rows, source="agent_states.csv")
        return _evaluate_values(
            self,
            values,
            rule=self.rule,
            value_name=self.expression,
            source="agent_states.csv",
        )


class FrameMetricExpressionCondition(OfflineCondition):
    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.expression = _required_str(config, "expression", fallback_keys=("expr",))
        self.rule = _parse_optional_numeric_rule(config)

    def evaluate(self) -> EvaluationResult:
        context = self._scenario_log()
        values = _expression_values(self, context.frame_metric_rows(), source="frame_metrics.csv")
        return _evaluate_values(
            self,
            values,
            rule=self.rule,
            value_name=self.expression,
            source="frame_metrics.csv",
        )


class ResultMetricExpressionCondition(OfflineCondition):
    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.expression = _required_str(config, "expression", fallback_keys=("expr",))
        self.rule = _parse_optional_numeric_rule(config)

    def evaluate(self) -> EvaluationResult:
        context = self._scenario_log()
        values = _expression_values(self, context.result_rows(), source="result.csv")
        return _evaluate_values(
            self,
            values,
            rule=self.rule,
            value_name=self.expression,
            source="result.csv",
        )


class AgentPairExpressionCondition(OfflineCondition):
    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.source_agent_id = str(_required(config, "source_agent_id", fallback_keys=("actor_id_a", "source_actor_id")))
        self.target_agent_id = str(_required(config, "target_agent_id", fallback_keys=("actor_id_b", "target_actor_id")))
        metric = config.get("metric")
        expression = config.get("expression", config.get("expr"))
        if expression is None:
            if metric is None:
                raise OutcomeEvalError("agent_pair_expression requires expression or metric")
            expression = f"source_{_variable_name(str(metric))} - target_{_variable_name(str(metric))}"
        self.expression = str(expression).strip()
        if not self.expression:
            raise OutcomeEvalError("agent_pair_expression requires a non-empty expression")
        self.rule = _parse_optional_numeric_rule(config)

    def evaluate(self) -> EvaluationResult:
        context = self._scenario_log()
        rows = context.agent_state_rows()
        _require_column(rows, "agent_id", context.agent_states_path)
        source_rows = [
            row for row in rows if str(row.get("agent_id", "")).strip() == self.source_agent_id
        ]
        target_rows = [
            row for row in rows if str(row.get("agent_id", "")).strip() == self.target_agent_id
        ]
        if not source_rows:
            return self.result(ConditionCode.NOT_EVALUATED, f"source agent {self.source_agent_id} not found")
        if not target_rows:
            return self.result(ConditionCode.NOT_EVALUATED, f"target agent {self.target_agent_id} not found")

        pairs = _paired_agent_rows(source_rows, target_rows)
        if not pairs:
            return self.result(
                ConditionCode.NOT_EVALUATED,
                f"no shared timesteps for agents {self.source_agent_id} and {self.target_agent_id}",
            )
        values = _pair_expression_values(self, pairs)
        return _evaluate_values(
            self,
            values,
            rule=self.rule,
            value_name=self.expression,
            source="agent_states.csv",
        )


def evaluate_outcomes(
    *,
    input_path: Path,
    config_path: Path,
    output_dir: Path,
    mode: OutcomeEvalMode | str = OutcomeEvalMode.REPLACE,
    default_outcome: str = "unknown",
    overwrite: bool = False,
    write_monitor_outcome: bool = False,
) -> OutcomeEvalResult:
    input_path = input_path.expanduser()
    output_dir = output_dir.expanduser()
    config = _load_config(config_path)
    condition = build_offline_condition_tree(config)
    logs = discover_scenario_logs(input_path)
    if not logs:
        raise OutcomeEvalError(f"no scenario monitor logs found in {input_path}")
    mode = OutcomeEvalMode(mode)
    default_outcome = _normalize_outcome(default_outcome, allow_unknown=True)
    prepare_output_dir(output_dir, overwrite=overwrite)

    outcomes = [
        _evaluate_one(log, condition, mode=mode, default_outcome=default_outcome)
        for log in logs
    ]
    if write_monitor_outcome:
        outcomes = [write_monitor_outcome_file(outcome) for outcome in outcomes]

    summary_csv_path = output_dir / "offline_outcomes.csv"
    write_summary_csv(summary_csv_path, outcomes)
    manifest_path = output_dir / "manifest.yaml"
    write_manifest(
        manifest_path,
        input_path=input_path,
        config_path=config_path,
        mode=mode,
        default_outcome=default_outcome,
        write_monitor_outcome=write_monitor_outcome,
        outcomes=outcomes,
        summary_csv_path=summary_csv_path,
    )
    return OutcomeEvalResult(
        input_path=input_path,
        output_dir=output_dir,
        manifest_path=manifest_path,
        summary_csv_path=summary_csv_path,
        outcomes=outcomes,
    )


def build_condition_tree(config: Any) -> ConditionNode:
    return build_offline_condition_tree(config)


def build_offline_condition_tree(config: Any) -> ConditionNode:
    return _build_node(_normalize_condition_config(config))


def _build_node(config: dict[str, Any]) -> ConditionNode:
    node_type = str(config.get("type", "")).strip().lower()
    if node_type == "and":
        return AndNode(config, [_build_node(_expect_mapping(child, label="child condition")) for child in _children(config)])
    if node_type == "or":
        return OrNode(config, [_build_node(_expect_mapping(child, label="child condition")) for child in _children(config)])

    leaf_types: dict[str, type[OfflineCondition]] = {
        "agent_state_threshold": AgentStateThresholdCondition,
        "agent_states_threshold": AgentStateThresholdCondition,
        "agent_state": AgentStateThresholdCondition,
        "frame_metric_threshold": FrameMetricThresholdCondition,
        "frame_metrics_threshold": FrameMetricThresholdCondition,
        "frame_metric": FrameMetricThresholdCondition,
        "result_metric_threshold": ResultMetricThresholdCondition,
        "result_threshold": ResultMetricThresholdCondition,
        "result_metric": ResultMetricThresholdCondition,
        "agent_state_expression": AgentStateExpressionCondition,
        "frame_metric_expression": FrameMetricExpressionCondition,
        "frame_metrics_expression": FrameMetricExpressionCondition,
        "result_metric_expression": ResultMetricExpressionCondition,
        "agent_pair_expression": AgentPairExpressionCondition,
        "agent_state_compare": AgentPairExpressionCondition,
        "agent_pair_compare": AgentPairExpressionCondition,
    }
    try:
        return leaf_types[node_type](config)
    except KeyError as exc:
        raise OutcomeEvalError(f"unknown offline condition type: {node_type}") from exc
    except ValueError as exc:
        raise OutcomeEvalError(str(exc)) from exc


def _evaluate_one(
    log: ScenarioLog,
    condition: ConditionNode,
    *,
    mode: OutcomeEvalMode,
    default_outcome: str,
) -> ScenarioOutcome:
    condition.reset()
    condition.put(log)
    result = condition.evaluate()
    triggered = result.code == ConditionCode.TRIGGERED
    original = _original_result_fields(log)
    if triggered:
        outcome = result.test_outcome if result.test_outcome else default_outcome
    elif mode == OutcomeEvalMode.OVERLAY:
        outcome = original["test_outcome"] or default_outcome
    else:
        outcome = default_outcome
    stop_condition = result.trigger_name if triggered and result.trigger_name else ""
    stop_reason = (
        f"Offline condition '{stop_condition}' triggered: {result.detail}"
        if triggered
        else (
            f"Offline condition not triggered; original outcome kept: {result.detail}"
            if mode == OutcomeEvalMode.OVERLAY
            else f"Offline condition not triggered: {result.detail}"
        )
    )
    if not triggered and mode == OutcomeEvalMode.OVERLAY:
        stop_condition = original["stop_condition"]
    return ScenarioOutcome(
        scenario_path=log.scenario_path,
        monitor_path=log.monitor_path,
        condition_name=result.condition_name,
        code=result.code,
        test_outcome=outcome,
        stop_condition=stop_condition,
        stop_reason=stop_reason,
        detail=result.detail,
        triggered=triggered,
    )


def _evaluate_values(
    condition: OfflineCondition,
    values: list[tuple[int, float | bool]],
    *,
    rule: NumericRule | None,
    value_name: str,
    source: str,
) -> EvaluationResult:
    aggregation = str(condition.config.get("aggregation", "any")).strip().lower()
    if not values:
        return condition.result(ConditionCode.NOT_EVALUATED, f"no values for {value_name} in {source}")

    def matches(value: float | bool) -> bool:
        if isinstance(value, bool):
            if rule is not None:
                raise OutcomeEvalError("boolean expressions cannot also use a numeric rule")
            return value
        if rule is None:
            raise OutcomeEvalError("numeric conditions require rule/op/operator")
        return rule.matches(value)

    if aggregation == "any":
        for index, value in values:
            if matches(value):
                return condition.result(
                    ConditionCode.TRIGGERED,
                    f"{source} {value_name}={_format_value(value)} matched at row {index}",
                )
        return condition.result(ConditionCode.NOT_TRIGGERED, f"no {source} rows matched {value_name}")

    if aggregation == "all":
        for index, value in values:
            if not matches(value):
                return condition.result(
                    ConditionCode.NOT_TRIGGERED,
                    f"{source} {value_name}={_format_value(value)} did not match at row {index}",
                )
        return condition.result(ConditionCode.TRIGGERED, f"all {len(values)} {source} rows matched {value_name}")

    if aggregation in {"min", "max", "first", "last"}:
        numeric_values = [(index, _as_float(value, label=value_name)) for index, value in values]
        index, value = _aggregate_value(numeric_values, aggregation)
        if matches(value):
            return condition.result(
                ConditionCode.TRIGGERED,
                f"{source} {aggregation}({value_name})={value:.6g} matched at row {index}",
            )
        return condition.result(
            ConditionCode.NOT_TRIGGERED,
            f"{source} {aggregation}({value_name})={value:.6g} did not match at row {index}",
        )

    raise OutcomeEvalError("aggregation must be one of any, all, min, max, first, last")


def _row_values(
    rows: list[dict[str, str]],
    metric: str,
    *,
    source: str,
) -> list[tuple[int, float]]:
    values: list[tuple[int, float]] = []
    for index, row in enumerate(rows, start=1):
        raw = row.get(metric)
        if raw in {None, ""}:
            continue
        values.append((index, _as_float(raw, label=f"{source} column {metric}")))
    return values


def _expression_values(
    condition: OfflineCondition,
    rows: list[dict[str, str]],
    *,
    source: str,
) -> list[tuple[int, float | bool]]:
    values: list[tuple[int, float | bool]] = []
    for index, row in enumerate(rows, start=1):
        variables = _row_variables(row, condition.config)
        try:
            values.append((index, evaluate_numeric_expression(condition.expression, variables)))
        except ValueError as exc:
            raise OutcomeEvalError(
                f"could not evaluate expression {condition.expression!r} for {source} row {index}: {exc}"
            ) from exc
    return values


def _pair_expression_values(
    condition: AgentPairExpressionCondition,
    pairs: list[tuple[int, dict[str, str], dict[str, str]]],
) -> list[tuple[int, float | bool]]:
    values: list[tuple[int, float | bool]] = []
    for index, source_row, target_row in pairs:
        variables = _pair_variables(source_row, target_row, condition.config)
        try:
            values.append((index, evaluate_numeric_expression(condition.expression, variables)))
        except ValueError as exc:
            raise OutcomeEvalError(
                f"could not evaluate expression {condition.expression!r} for agent pair row {index}: {exc}"
            ) from exc
    return values


def _parse_numeric_rule(config: dict[str, Any]) -> NumericRule:
    rule = _parse_optional_numeric_rule(config)
    if rule is None:
        raise OutcomeEvalError("threshold condition requires rule/op/operator")
    return rule


def _parse_optional_numeric_rule(config: dict[str, Any]) -> NumericRule | None:
    rule_config = config.get("rule")
    if isinstance(rule_config, dict):
        raw_rule = rule_config.get("rule", rule_config.get("op", rule_config.get("operator")))
        raw_value = rule_config.get("value", config.get("value"))
        raw_values = rule_config.get("values", config.get("values"))
        eps = rule_config.get("eps", config.get("eps"))
        if raw_values is None and "min" in rule_config and "max" in rule_config:
            raw_values = [rule_config["min"], rule_config["max"]]
    else:
        raw_rule = rule_config if rule_config is not None else config.get("op", config.get("operator"))
        raw_value = config.get("value")
        raw_values = config.get("values")
        eps = config.get("eps")
        if raw_values is None and "min" in config and "max" in config:
            raw_values = [config["min"], config["max"]]
    if raw_rule is None:
        return None
    try:
        return NumericRule.from_config(
            raw_rule,
            raw_value=raw_value,
            raw_values=raw_values,
            eps=eps,
            field_name="value",
        )
    except ValueError as exc:
        raise OutcomeEvalError(str(exc)) from exc


def _agent_state_rows_for_agents(
    context: ScenarioLog,
    agent_ids: frozenset[str] | None,
) -> list[dict[str, str]]:
    rows = context.agent_state_rows()
    _require_column(rows, "agent_id", context.agent_states_path)
    if agent_ids is None:
        return rows
    filtered = [row for row in rows if str(row.get("agent_id", "")).strip() in agent_ids]
    if not filtered:
        return []
    return filtered


def _paired_agent_rows(
    source_rows: list[dict[str, str]],
    target_rows: list[dict[str, str]],
) -> list[tuple[int, dict[str, str], dict[str, str]]]:
    key_name = "step_index"
    if key_name not in source_rows[0] or key_name not in target_rows[0]:
        key_name = "sim_time_ms"
    source_by_key = {row.get(key_name): row for row in source_rows if row.get(key_name) not in {"", None}}
    target_by_key = {row.get(key_name): row for row in target_rows if row.get(key_name) not in {"", None}}
    shared_keys = [key for key in source_by_key if key in target_by_key]
    if not shared_keys:
        count = min(len(source_rows), len(target_rows))
        return [(index + 1, source_rows[index], target_rows[index]) for index in range(count)]
    return [
        (index + 1, source_by_key[key], target_by_key[key])
        for index, key in enumerate(sorted(shared_keys, key=_natural_key))
    ]


def _row_variables(row: dict[str, str], config: dict[str, Any]) -> dict[str, float]:
    raw_variables = config.get("variables")
    if isinstance(raw_variables, dict):
        variables: dict[str, float] = {}
        for name, column in raw_variables.items():
            column_name = str(column)
            if column_name not in row:
                raise OutcomeEvalError(f"required expression column '{column_name}' not found")
            variables[str(name)] = _as_float(row[column_name], label=f"expression variable {name}")
        return variables
    return {
        _variable_name(column): _as_float(value, label=f"expression column {column}")
        for column, value in row.items()
        if value not in {"", None} and _is_float(value)
    }


def _pair_variables(
    source_row: dict[str, str],
    target_row: dict[str, str],
    config: dict[str, Any],
) -> dict[str, float]:
    raw_variables = config.get("variables")
    if isinstance(raw_variables, dict):
        row_map = {"source": source_row, "target": target_row}
        variables: dict[str, float] = {}
        for name, spec in raw_variables.items():
            if isinstance(spec, str) and "." in spec:
                side, column = spec.split(".", 1)
            elif isinstance(spec, dict):
                side = str(spec.get("side", "source"))
                column = str(spec.get("column", ""))
            else:
                raise OutcomeEvalError("agent pair variables must be 'source.column'/'target.column' or mappings")
            if side not in row_map:
                raise OutcomeEvalError("agent pair variable side must be source or target")
            if column not in row_map[side]:
                raise OutcomeEvalError(f"required expression column '{side}.{column}' not found")
            variables[str(name)] = _as_float(row_map[side][column], label=f"expression variable {name}")
        return variables

    variables = {}
    for prefix, row in (("source", source_row), ("target", target_row)):
        for column, value in row.items():
            if value in {"", None} or not _is_float(value):
                continue
            variables[f"{prefix}_{_variable_name(column)}"] = float(value)
    for column in set(source_row) & set(target_row):
        if column == "agent_id":
            continue
        if _is_float(source_row.get(column)) and _is_float(target_row.get(column)):
            variables[f"delta_{_variable_name(column)}"] = float(source_row[column]) - float(target_row[column])
    return variables


def _original_result_fields(log: ScenarioLog) -> dict[str, str]:
    try:
        rows = log.result_rows()
    except OutcomeEvalError:
        return {"test_outcome": "", "stop_condition": ""}
    if not rows:
        return {"test_outcome": "", "stop_condition": ""}
    row = rows[-1]
    return {
        "test_outcome": _normalize_outcome(row.get("run.test_outcome"), allow_unknown=True)
        if row.get("run.test_outcome") not in {None, ""}
        else "",
        "stop_condition": row.get("run.stop_condition", ""),
    }


def _normalize_condition_config(config: Any) -> dict[str, Any]:
    if isinstance(config, list):
        return {"type": "or", "name": "offline_conditions", "children": config}
    if not isinstance(config, dict):
        raise OutcomeEvalError("condition config must be a mapping or list")
    if "condition" in config:
        condition = config["condition"]
        if not isinstance(condition, dict | list):
            raise OutcomeEvalError("condition must be a mapping or list")
        return _normalize_condition_config(condition)
    return config


def _children(config: dict[str, Any]) -> list[Any]:
    children = config.get("children")
    if not isinstance(children, list):
        raise OutcomeEvalError(f"{config.get('type')} condition requires children list")
    return children


def _load_config(path: Path) -> Any:
    path = path.expanduser()
    if not path.exists():
        raise OutcomeEvalError(f"config path does not exist: {path}")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise OutcomeEvalError(f"could not parse config: {exc}") from exc


def _require_column(rows: list[dict[str, str]], column: str, path: Path) -> None:
    if not rows:
        raise OutcomeEvalError(f"{path} has no rows")
    if column not in rows[0]:
        raise OutcomeEvalError(f"required column '{column}' not found in {path}")


def _parse_agent_ids(config: dict[str, Any]) -> frozenset[str] | None:
    raw = config.get("agent_ids", config.get("agents", config.get("actor_ids")))
    if raw is None:
        raw = config.get("agent_id", config.get("actor_id"))
    if raw is None or _is_any_agent(raw):
        return None
    if isinstance(raw, int | str):
        return frozenset({str(raw)})
    if not isinstance(raw, list | tuple | set):
        raise OutcomeEvalError("agent filter must be an id, list of ids, or any")
    values = {str(value) for value in raw if not _is_any_agent(value)}
    return frozenset(values) if values else None


def _parse_outcome(config: dict[str, Any]) -> str | None:
    raw = config.get("test_outcome", config.get("outcome", config.get("result_status", config.get("result"))))
    if raw is None:
        return None
    return _normalize_outcome(raw)


def _normalize_outcome(value: Any, *, allow_unknown: bool = False) -> str:
    normalized = str(value).strip().lower()
    if allow_unknown and normalized in {"", "unknown", "none", "null"}:
        return "unknown"
    try:
        return OUTCOME_ALIASES[normalized]
    except KeyError as exc:
        raise OutcomeEvalError(f"outcome must be Success, Fail, or Invalid, got: {value!r}") from exc


def _required(config: dict[str, Any], key: str, *, fallback_keys: tuple[str, ...] = ()) -> Any:
    for candidate in (key, *fallback_keys):
        if candidate in config:
            return config[candidate]
    raise OutcomeEvalError(f"{config.get('type', 'condition')} requires {key}")


def _required_str(config: dict[str, Any], key: str, *, fallback_keys: tuple[str, ...] = ()) -> str:
    value = str(_required(config, key, fallback_keys=fallback_keys)).strip()
    if not value:
        raise OutcomeEvalError(f"{config.get('type', 'condition')} requires non-empty {key}")
    return value


def _expect_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OutcomeEvalError(f"{label} must be a mapping")
    return value


def _aggregate_value(values: list[tuple[int, float]], aggregation: str) -> tuple[int, float]:
    if aggregation == "min":
        return min(values, key=lambda item: item[1])
    if aggregation == "max":
        return max(values, key=lambda item: item[1])
    if aggregation == "first":
        return values[0]
    if aggregation == "last":
        return values[-1]
    raise OutcomeEvalError(f"unsupported aggregation: {aggregation}")


def _as_float(value: Any, *, label: str) -> float:
    if isinstance(value, bool):
        raise OutcomeEvalError(f"{label} must be numeric, got bool")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise OutcomeEvalError(f"{label} must be numeric, got: {value!r}") from exc


def _is_float(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _format_value(value: float | bool) -> str:
    return str(value).lower() if isinstance(value, bool) else f"{value:.6g}"


def _variable_name(value: str) -> str:
    name = re.sub(r"[^0-9A-Za-z_]+", "_", value.strip()).strip("_")
    if not name:
        return "value"
    if name[0].isdigit():
        return f"v_{name}"
    return name


def _natural_key(value: Any) -> list[Any]:
    return natural_key(value)


def _is_any_agent(raw_value: Any) -> bool:
    return isinstance(raw_value, str) and raw_value.strip().lower() in {"any", "*", "all"}


def _condition_code_label(code: ConditionCode) -> str:
    return condition_code_label(code)
