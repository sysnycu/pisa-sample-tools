from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            item.name: _jsonable(getattr(value, item.name)) for item in dataclasses.fields(value)
        }
    if isinstance(value, StrEnum):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


class Serializable:
    """Small dependency-free serialization surface compatible with API models.

    ``model_dump`` deliberately mirrors the common Pydantic call used by the API
    layer.  The reporting package itself remains independent from Pydantic.
    """

    def as_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return self.as_dict()


class FindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ComparisonRole(StrEnum):
    DUPLICATE_ALIAS = "duplicate_alias"
    PAIRED_REPLICATE = "paired_replicate"
    PAIRED_SYSTEM_INTERVENTION = "paired_system_intervention"
    PAIRED_POLICY_INTERVENTION = "paired_policy_intervention"
    PARTIAL_PAIR = "partial_pair"
    UNPAIRED_COMMON_DOMAIN = "unpaired_common_domain"
    DESCRIPTIVE_ONLY = "descriptive_only"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True)
class SourceFingerprint(Serializable):
    path: Path
    kind: str
    size: int | None = None
    mtime_ns: int | None = None
    sha256: str | None = None
    expected_sha256: str | None = None
    status: str = "recorded"


@dataclass(frozen=True)
class DataHealthFinding(Serializable):
    code: str
    severity: FindingSeverity
    message: str
    dataset_id: str | None = None
    run_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DatasetDescriptor(Serializable):
    dataset_id: str
    source_path: Path
    manifest_path: Path | None
    execution_id: str | None
    scenario_name: str | None
    simulator: str | None
    av: str | None
    sampler: str | None
    completed_at: str | None
    expected_runs: int | None
    run_count: int
    attempt_count: int
    canonical_digest: str
    source_fingerprint: str
    health_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class DatasetRelation(Serializable):
    left_dataset_id: str
    right_dataset_id: str
    role: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IndexedRun(Serializable):
    run_id: str
    dataset_id: str
    scenario_id: str
    attempt: int
    sample_id: str | None
    parameter_hash: str | None
    params: dict[str, Any]
    status: str | None
    outcome: str | None
    outcome_class: str
    stop_condition: str | None
    stop_reason: str | None
    metrics: dict[str, Any]
    result_path: Path
    trace_paths: dict[str, Path]
    provenance_signature: str | None
    has_collision: bool


@dataclass(frozen=True)
class IndexedAttempt(Serializable):
    dataset_id: str
    scenario_id: str
    attempt: int
    row_index: int
    sample_id: str | None
    parameter_hash: str | None
    params: dict[str, Any]
    status: str | None
    outcome: str | None
    stop_condition: str | None
    stop_reason: str | None
    metrics: dict[str, Any]
    result_path: Path
    row_digest: str


@dataclass(frozen=True)
class RunFilter(Serializable):
    dataset_ids: tuple[str, ...] = ()
    outcomes: tuple[str, ...] = ()
    outcome_classes: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    parameter_hash: str | None = None
    parameter_values: dict[str, Any] = field(default_factory=dict)
    search: str | None = None


@dataclass(frozen=True)
class RunPage(Serializable):
    items: tuple[IndexedRun, ...]
    total: int
    limit: int
    next_cursor: str | None


@dataclass(frozen=True)
class OutcomeSummary(Serializable):
    total: int
    success: int
    fail: int
    invalid: int
    unknown: int
    collision: int


@dataclass(frozen=True)
class StageTiming(Serializable):
    stage: str
    started_at: str
    finished_at: str
    duration_seconds: float


@dataclass(frozen=True)
class IndexBuildResult(Serializable):
    database_path: Path
    source_roots: tuple[Path, ...]
    source_fingerprint: str
    rebuilt: bool
    dataset_count: int
    run_count: int
    attempt_count: int
    finding_count: int
    timings: tuple[StageTiming, ...]


@dataclass(frozen=True)
class ReportBundleResult(Serializable):
    output_dir: Path
    report_path: Path
    index_path: Path
    manifest_path: Path
    summary_json_path: Path
    dataset_count: int
    aggregate_dataset_count: int
    run_count: int
    finding_count: int
    source_fingerprint: str
    index_build: IndexBuildResult


@dataclass(frozen=True)
class ComparisonAssessment(Serializable):
    role: ComparisonRole
    matched_count: int
    left_count: int
    right_count: int
    left_only_count: int
    right_only_count: int
    semantic_compatible: bool | None
    reason: str
    warnings: tuple[str, ...] = ()
