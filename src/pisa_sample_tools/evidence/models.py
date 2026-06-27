from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class EvidenceError(ValueError):
    """Raised for user-facing evidence build failures."""


@dataclass(frozen=True)
class DatasetSpec:
    dataset_id: str
    results_path: Path
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricBinding:
    summary: str | None = None
    series: str | None = None
    label: str | None = None
    unit: str | None = None
    missing: str = "unavailable"


@dataclass(frozen=True)
class DerivedParameter:
    operation: str
    left: str
    right: str
    label: str | None = None
    unit: str | None = None


@dataclass(frozen=True)
class AxisRule:
    policy: str = "auto"
    detail_policy: str = "auto"
    lower: float | None = None
    upper: float | None = None
    padding_fraction: float = 0.08
    minimum_span: float | None = None
    shared_across_cases: bool = False


def _default_comparison_tolerances() -> dict[str, float]:
    return {
        "steer": 0.02,
        "throttle": 0.05,
        "brake": 0.05,
        "speed": 0.5,
        "acceleration": 0.5,
        "min_ttc": 0.2,
        "min_distance": 0.5,
    }


@dataclass(frozen=True)
class ComparisonDetailSpec:
    enabled: bool = True
    max_points_per_series: int = 2000
    trajectory_divergence_m: float = 0.5
    tolerances: dict[str, float] = field(default_factory=_default_comparison_tolerances)


@dataclass(frozen=True)
class AnalysisSpec:
    version: int = 1
    validation_mode: str = "permissive"
    metadata: dict[str, Any] = field(default_factory=dict)
    parameter_mode: str = "single"
    parameter_include: tuple[str, ...] = ()
    parameter_exclude: frozenset[str] = frozenset()
    x_param: str | None = None
    y_param: str | None = None
    parameter_units: dict[str, str] = field(default_factory=dict)
    parameter_labels: dict[str, str] = field(default_factory=dict)
    derived_parameters: dict[str, DerivedParameter] = field(default_factory=dict)
    metrics: dict[str, MetricBinding] = field(default_factory=dict)
    success_outcomes: frozenset[str] = frozenset({"success"})
    failure_outcomes: frozenset[str] = frozenset(
        {"fail", "failure", "failed", "test_fail", "collision"}
    )
    invalid_outcomes: frozenset[str] = frozenset({"invalid"})
    collision_reasons: frozenset[str] = frozenset({"collision", "collision_guard"})
    termination_outcomes: dict[str, str] = field(default_factory=dict)
    near_critical_ttc_s: float = 2.0
    heatmap_bins: int = 30
    heatmap_min_bin_count: int = 1
    pairing_mode: str = "sample_id_then_parameters"
    pairing_parameter_tolerance: float = 1e-9
    bootstrap_samples: int = 2000
    bootstrap_seed: int = 0
    output_formats: tuple[str, ...] = ("svg", "png")
    visualization_axes: dict[str, AxisRule] = field(default_factory=dict)
    comparison_detail: ComparisonDetailSpec = field(default_factory=ComparisonDetailSpec)


@dataclass(frozen=True)
class RunRecord:
    experiment_id: str
    scenario_id: str
    sample_id: str | None
    logical_scenario_name: str
    params: dict[str, Any]
    metadata: dict[str, Any]
    status: str | None
    outcome: str | None
    termination_reason: str | None
    stop_reason: str | None
    metrics: dict[str, Any]
    result_path: Path
    frame_metrics_path: Path | None = None
    agent_states_path: Path | None = None
    agent_geometry_path: Path | None = None
    collision_events_path: Path | None = None
    scenario_events_path: Path | None = None
    control_commands_path: Path | None = None

    @property
    def run_id(self) -> str:
        return f"{self.experiment_id}:{self.scenario_id}"


@dataclass(frozen=True)
class ConcreteComparisonGroup:
    group_id: str
    logical_scenario_name: str
    parameter_key: str
    params: dict[str, Any]
    pairing_method: str
    runs: tuple[RunRecord, ...]


@dataclass(frozen=True)
class SelectedCase:
    case_type: str
    run: RunRecord
    reason: str


@dataclass(frozen=True)
class EvidenceResult:
    output_dir: Path
    report_path: Path
    manifest_path: Path
    run_count: int
    figure_paths: tuple[Path, ...]
    warning_count: int
