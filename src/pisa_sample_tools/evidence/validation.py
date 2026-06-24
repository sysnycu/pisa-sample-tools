from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from .ingest import read_trace_rows
from .models import AnalysisSpec, EvidenceError, RunRecord
from .statistics import as_float, normalized_outcome


@dataclass(frozen=True)
class DataQualityFinding:
    severity: str
    code: str
    message: str
    run_id: str | None = None
    field: str | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "run_id": self.run_id,
            "field": self.field,
            "message": self.message,
        }


def validate_runs(
    runs: list[RunRecord], spec: AnalysisSpec, *, deep: bool = False
) -> list[DataQualityFinding]:
    findings: list[DataQualityFinding] = []
    run_ids = Counter(run.run_id for run in runs)
    for run_id, count in sorted(run_ids.items()):
        if count > 1:
            findings.append(
                DataQualityFinding("error", "duplicate_run_id", f"run ID occurs {count} times", run_id)
            )

    parameter_names = sorted({name for run in runs for name in run.params})
    numeric_parameters = [
        name
        for name in parameter_names
        if any(as_float(run.params.get(name)) is not None for run in runs)
    ]
    requested = list(spec.parameter_include)
    if spec.parameter_mode == "single":
        requested.extend(item for item in (spec.x_param, spec.y_param) if item)
    for name in dict.fromkeys(requested):
        if name not in parameter_names:
            findings.append(
                DataQualityFinding(
                    "error", "missing_parameter", f"configured parameter '{name}' was not found", field=name
                )
            )
        elif name not in numeric_parameters:
            findings.append(
                DataQualityFinding(
                    "error", "non_numeric_parameter", f"configured parameter '{name}' is not numeric", field=name
                )
            )
        else:
            missing_count = sum(as_float(run.params.get(name)) is None for run in runs)
            if missing_count:
                findings.append(
                    DataQualityFinding(
                        "error",
                        "incomplete_parameter",
                        f"configured parameter '{name}' is missing or non-numeric for "
                        f"{missing_count} run(s)",
                        field=name,
                    )
                )
    if spec.parameter_mode == "single" and spec.x_param and spec.x_param == spec.y_param:
        findings.append(
            DataQualityFinding("error", "duplicate_axes", "configured x and y parameters are identical")
        )
    selected_numeric = requested or [
        name for name in numeric_parameters if name not in spec.parameter_exclude
    ]
    if len(set(selected_numeric)) < 2:
        findings.append(
            DataQualityFinding(
                "warning",
                "insufficient_parameter_dimensions",
                "fewer than two numeric parameters are available; parameter maps will be omitted",
            )
        )

    for metric_name, binding in spec.metrics.items():
        if binding.summary is None:
            findings.append(
                DataQualityFinding(
                    "error", "metric_without_summary", f"metric '{metric_name}' has no summary binding"
                )
            )
            continue
        summary_count = sum(binding.summary in run.metrics for run in runs)
        derivable_count = sum(
            binding.series is not None and run.frame_metrics_path is not None for run in runs
        )
        if summary_count == 0 and derivable_count == 0:
            findings.append(
                DataQualityFinding(
                    "error",
                    "missing_metric",
                    f"metric '{metric_name}' has neither summary nor configured frame series",
                    field=binding.summary,
                )
            )
        elif summary_count < len(runs):
            findings.append(
                DataQualityFinding(
                    "info",
                    "metric_requires_derivation",
                    f"metric '{metric_name}' requires frame-series derivation for "
                    f"{len(runs) - summary_count} run(s)",
                    field=binding.summary,
                )
            )

    unmapped = Counter(
        ((run.outcome or "unknown").lower(), (run.termination_reason or "unknown").lower())
        for run in runs
        if normalized_outcome(run, spec) == "unclassified"
        and (run.termination_reason or "").lower() not in spec.termination_outcomes
    )
    for (outcome, reason), count in sorted(unmapped.items()):
        findings.append(
            DataQualityFinding(
                "error",
                "unmapped_outcome",
                f"outcome '{outcome}' with termination '{reason}' has no explicit mapping "
                f"for {count} run(s)",
            )
        )
    missing_ego = sorted(
        {
            run.experiment_id
            for run in runs
            if run.metadata.get("ego_agent_id") in {None, ""}
        }
    )
    for experiment_id in missing_ego:
        findings.append(
            DataQualityFinding(
                "warning",
                "missing_ego_agent_id",
                f"{experiment_id}: ego_agent_id is missing; actor 0 will be used as fallback",
            )
        )
    for run in runs:
        if deep:
            findings.extend(_validate_trace_alignment(run))
    return _deduplicate(findings)


def enforce_validation(findings: list[DataQualityFinding], spec: AnalysisSpec) -> None:
    errors = [finding for finding in findings if finding.severity == "error"]
    if spec.validation_mode == "strict" and errors:
        preview = "; ".join(finding.message for finding in errors[:5])
        suffix = f"; and {len(errors) - 5} more" if len(errors) > 5 else ""
        raise EvidenceError(f"strict validation failed: {preview}{suffix}")


def _validate_trace_alignment(run: RunRecord) -> list[DataQualityFinding]:
    findings: list[DataQualityFinding] = []
    frames = read_trace_rows(run.frame_metrics_path)
    controls = read_trace_rows(run.control_commands_path)
    states = read_trace_rows(run.agent_states_path)
    collisions = read_trace_rows(run.collision_events_path)
    frame_steps = _integer_values(frames, "step_index")
    control_steps = _integer_values(controls, "step_index")
    if frame_steps and frame_steps != sorted(frame_steps):
        findings.append(
            DataQualityFinding("error", "non_monotonic_frames", "frame steps are not monotonic", run.run_id)
        )
    if frame_steps and control_steps and frame_steps != control_steps:
        findings.append(
            DataQualityFinding(
                "warning",
                "frame_control_mismatch",
                "frame and control step sequences differ",
                run.run_id,
            )
        )
    if states and frame_steps:
        state_counts = Counter(_integer_values(states, "step_index"))
        if set(state_counts) != set(frame_steps):
            findings.append(
                DataQualityFinding(
                    "warning", "frame_state_mismatch", "frame and agent-state steps differ", run.run_id
                )
            )
        elif len(set(state_counts.values())) > 1:
            findings.append(
                DataQualityFinding(
                    "warning", "agent_count_changed", "agent-state cardinality changes by frame", run.run_id
                )
            )
    collision_reason = "collision" in (run.termination_reason or "").lower()
    if collision_reason and not collisions:
        findings.append(
            DataQualityFinding(
                "warning", "missing_collision_event", "collision termination has no event row", run.run_id
            )
        )
    if collisions and not collision_reason:
        findings.append(
            DataQualityFinding(
                "warning",
                "unexpected_collision_event",
                "collision event exists but termination reason is not collision",
                run.run_id,
            )
        )
    return findings


def _integer_values(rows: list[dict[str, str]], field: str) -> list[int]:
    values = []
    for row in rows:
        value = as_float(row.get(field))
        if value is not None:
            values.append(int(value))
    return values


def _deduplicate(findings: list[DataQualityFinding]) -> list[DataQualityFinding]:
    unique = {
        (item.severity, item.code, item.message, item.run_id, item.field): item
        for item in findings
    }
    return sorted(
        unique.values(), key=lambda item: (item.severity, item.code, item.run_id or "", item.message)
    )
