from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class ErrorBody(BaseModel):
    code: str
    message: str
    field: str | None = None
    details: Any | None = None
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class Progress(BaseModel):
    current: float | None = None
    total: float | None = None
    unit: str | None = None


class Job(BaseModel):
    id: str
    kind: str
    status: JobStatus
    phase: str
    message: str | None = None
    progress: Progress = Field(default_factory=Progress)
    request: dict[str, Any] = Field(default_factory=dict)
    result: Any | None = None
    error: ErrorBody | None = None
    cancel_requested: bool = False
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None


class JobEvent(BaseModel):
    sequence: int
    type: Literal[
        "queued", "progress", "log", "artifact", "complete", "failed", "cancelled"
    ]
    data: dict[str, Any]
    created_at: str


class SamplerPreviewRequest(BaseModel):
    source_file: str | None = None
    sampler_name: str | None = None
    source_type: str | None = None
    module_path: str | None = None
    config_path: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    max_samples: int | None = Field(default=100, ge=0, le=10_000)
    method: Literal["grid", "lhs", "sobol", "random"] | None = None
    count: int | None = Field(default=None, ge=1, le=100_000)
    seed: int | None = Field(default=None, ge=0)
    parameters: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def source_or_inline(self) -> SamplerPreviewRequest:
        inline = self.method is not None or self.count is not None or self.parameters is not None
        if bool(self.source_file) == inline:
            raise ValueError("provide either source_file or an inline method/count/parameters definition")
        if inline and (self.method is None or self.count is None or not self.parameters):
            raise ValueError("inline preview requires method, count, and at least one parameter")
        return self


class SampleExportRequest(BaseModel):
    output_dir: str
    runner_spec_path: str | None = None
    sampler_spec_path: str | None = None
    scenario_path: str | None = None
    shard_size: int | None = Field(default=None, gt=0)
    num_shards: int | None = Field(default=None, gt=0)
    source_path_mode: Literal["absolute", "relative-to-output"] = "absolute"
    create_zip: bool = False
    zip_path: str | None = None
    dry_run: bool = False
    overwrite: bool = False

    @model_validator(mode="after")
    def one_spec(self) -> SampleExportRequest:
        if (self.runner_spec_path is None) == (self.sampler_spec_path is None):
            raise ValueError("exactly one of runner_spec_path or sampler_spec_path is required")
        if self.sampler_spec_path is not None and self.scenario_path is None:
            raise ValueError("scenario_path is required with sampler_spec_path")
        if self.shard_size is not None and self.num_shards is not None:
            raise ValueError("shard_size and num_shards are mutually exclusive")
        return self


class SampleAnalyzeRequest(BaseModel):
    output_dir: str
    runner_spec_path: str | None = None
    samples_path: str | None = None
    results_path: str | None = None
    params: list[str] | None = None
    color_by: str = "outcome"
    bins: int = Field(default=28, gt=0, le=1_000)
    post_outcome_config_path: str | None = None
    post_outcome_mode: Literal["overlay", "replace"] = "overlay"
    overwrite: bool = False

    @model_validator(mode="after")
    def one_source(self) -> SampleAnalyzeRequest:
        values = (self.runner_spec_path, self.samples_path, self.results_path)
        if sum(value is not None for value in values) != 1:
            raise ValueError(
                "exactly one of runner_spec_path, samples_path, or results_path is required"
            )
        if self.post_outcome_config_path is not None and self.results_path is None:
            raise ValueError("post_outcome_config_path is only supported with results_path")
        return self


class DatasetDescriptor(BaseModel):
    id: str
    results: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReportValidateRequest(BaseModel):
    experiments: list[dict[str, Any]] = Field(default_factory=list)
    path: str | None = None
    spec: dict[str, Any] = Field(default_factory=dict)
    deep: bool = False

    @model_validator(mode="after")
    def validation_source(self) -> ReportValidateRequest:
        if bool(self.path) == bool(self.experiments):
            raise ValueError("provide either path or experiments")
        return self


class ReportBuildRequest(BaseModel):
    results_paths: list[str] = Field(default_factory=list)
    experiments: list[dict[str, Any]] = Field(default_factory=list)
    campaign_path: str | None = None
    output_dir: str
    spec_path: str | None = None
    overwrite: bool = False
    validation_mode: Literal["strict", "permissive"] | None = None
    deep_validation: bool = False
    report_mode: Literal["interactive", "static"] = "interactive"
    sensitivity: bool | None = None
    engine: Literal["auto", "normalized", "legacy"] = "auto"

    @model_validator(mode="after")
    def source(self) -> ReportBuildRequest:
        sources = sum((bool(self.results_paths), bool(self.experiments), bool(self.campaign_path)))
        if sources != 1:
            raise ValueError("provide exactly one of results_paths, experiments, or campaign_path")
        return self


class DirectoryCreateRequest(BaseModel):
    parent: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=255)


class ExportRequest(BaseModel):
    artifact_path: str | None = None
    visualization_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,160}$")
    format: Literal["svg", "pdf", "png", "csv", "json", "mp4", "webm", "gif"] | None = (
        None
    )
    preset: Literal["paper-single", "paper-double", "slides-hd", "slides-4k"] | None = None
    dpi: Literal[300, 600] | None = None
    background: Literal["white", "transparent"] = "white"
    filters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def export_source(self) -> ExportRequest:
        if bool(self.artifact_path) == bool(self.visualization_id):
            raise ValueError("provide either artifact_path or visualization_id")
        if self.visualization_id and self.format is None:
            raise ValueError("format is required for visualization exports")
        return self


class SnapshotRequest(BaseModel):
    mode: Literal["compact", "full"] = "compact"
    selected_run_ids: list[str] = Field(default_factory=list, max_length=100)


class MediaCreateRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=512)
    run_ids: list[str] = Field(default_factory=list, max_length=32)
    format: Literal["gif", "mp4", "webm", "png"] = "gif"
    fps: int = Field(default=10, ge=1, le=60)
    max_frames: int = Field(default=180, ge=2, le=2_000)
    playback_rate: float | None = Field(default=None, ge=0.05, le=16)
    width: int = Field(default=960, ge=480, le=3_840)
    height: int = Field(default=540, ge=320, le=2_160)
    overwrite: bool = False
    include_map: bool = True
    map_reference: bool = True
    map_boundaries: bool = True
    map_junctions: bool = True
    show_bounding_boxes: bool = True
    follow_cursor: bool = False
    trail_only: bool = True
    render_mode: Literal["standard", "trajectory_view"] = "standard"
    show_ego: bool = True
    show_agents: bool = True
    actor_names: list[str] = Field(default_factory=list, max_length=256)
    show_goal: bool = True
    show_grid: bool = False
    show_axes: bool = True
    x_min: float | None = None
    x_max: float | None = None
    y_min: float | None = None
    y_max: float | None = None

    @model_validator(mode="after")
    def render_budget(self) -> MediaCreateRequest:
        if not (self.show_ego or self.show_agents or self.show_goal):
            raise ValueError("at least one of ego, agents, or ego goal must be visible")
        if (
            self.format != "png"
            and self.width * self.height * self.max_frames > (300_000_000 if self.playback_rate is not None else 100_000_000)
        ):
            raise ValueError(
                "animation exceeds the 100,000,000 frame-pixel render budget"
            )
        ranges = ((self.x_min, self.x_max, "x"), (self.y_min, self.y_max, "y"))
        for minimum, maximum, axis in ranges:
            if (minimum is None) != (maximum is None):
                raise ValueError(f"both {axis}_min and {axis}_max are required")
            if minimum is not None and maximum is not None and minimum >= maximum:
                raise ValueError(f"{axis}_min must be smaller than {axis}_max")
        return self


class RunnerExperimentRequest(BaseModel):
    experiment_id: str
    overrides: dict[str, Any] = Field(default_factory=dict)


class RunnerJobRequest(RunnerExperimentRequest):
    action: Literal["build", "start", "run", "run_all", "report"] = "run_all"


class RunnerRegistryRequest(BaseModel):
    registry: dict[str, Any]


class RunnerPresetCreateRequest(BaseModel):
    preset_id: str
    template_id: str
    label: str = ""
    simulator_component: str
    av_component: str
    tags: list[str] = Field(default_factory=list)


class RunnerPresetUpdateRequest(BaseModel):
    experiment: dict[str, Any]


class RunnerPresetRenameRequest(BaseModel):
    new_id: str
    label: str | None = None


class ConfirmationRequest(BaseModel):
    confirm: bool = False


class RunnerScenarioRequest(BaseModel):
    path: str


class RunnerCleanupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.-]+$")


class RunnerResumeRequest(BaseModel):
    action: Literal["run", "stop", "report"]


class LegacyRebuildRequest(BaseModel):
    output_dir: str | None = None
    sensitivity: bool | None = None
    overwrite: bool = False


class ReportRenameRequest(BaseModel):
    new_name: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[^/\\\x00]+$",
    )


class ReportDeleteRequest(BaseModel):
    confirm_name: str = Field(min_length=1, max_length=160)


class TrajectoryRenderRequest(BaseModel):
    source_path: str
    title: str | None = None
    width: int = Field(default=1100, ge=320, le=8_000)
    height: int = Field(default=760, ge=240, le=8_000)
    x_range: tuple[float, float] | None = None
    y_range: tuple[float, float] | None = None
    equal_scale: bool = True
    ignore_agent_ids: set[str] = Field(default_factory=set)
    origin_agent_id: str | None = None


class TrajectoryRequest(BaseModel):
    input_path: str
    output_dir: str
    overwrite: bool = False
    width: int = Field(default=1100, ge=320, le=8_000)
    height: int = Field(default=760, ge=240, le=8_000)
    x_range: tuple[float, float] | None = None
    y_range: tuple[float, float] | None = None
    equal_scale: bool = True
    ignore_agent_ids: set[str] = Field(default_factory=set)
    origin_agent_id: str | None = None


class TrajectoryCompareRequest(BaseModel):
    left_path: str
    right_path: str
    output_dir: str
    left_label: str | None = None
    right_label: str | None = None
    ignore_agent_ids: set[str] = Field(default_factory=set)
    overwrite: bool = False
    width: int = Field(default=1200, ge=320, le=8_000)
    height: int = Field(default=820, ge=240, le=8_000)
    equal_scale: bool = True


class OutcomeEvalRequest(BaseModel):
    input_path: str
    config_path: str
    output_dir: str
    mode: Literal["overlay", "replace"] = "replace"
    default_outcome: Literal["success", "fail", "invalid", "unknown"] = "unknown"
    overwrite: bool = False
    write_monitor_outcome: bool = False


class RepairPlan(BaseModel):
    version: Literal[1] = 1
    signature: str
    source_path: str
    mode: Literal["overlay", "source"] = "overlay"
    output_path: str | None = None
    init_state_path: str | None = None
    reference_root: str | None = None
    backup_suffix: str = ".bak"
    time_step_ms: float | None = None
    findings: list[dict[str, Any]] = Field(default_factory=list)
    changes: list[dict[str, Any]] = Field(default_factory=list)
    destructive: bool = False


class RepairScanRequest(BaseModel):
    source_path: str
    init_state_path: str | None = None
    reference_root: str | None = None
    mode: Literal["overlay", "source"] = "overlay"
    output_path: str | None = None
    backup_suffix: str = Field(default=".bak", pattern=r"^\.[A-Za-z0-9_-]{1,20}$")
    time_step_ms: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def repair_source(self) -> RepairScanRequest:
        if (self.init_state_path is None) == (self.reference_root is None):
            raise ValueError("provide exactly one of init_state_path or reference_root")
        if self.mode == "overlay" and not self.output_path:
            raise ValueError("output_path is required in overlay mode")
        if self.mode == "source" and self.output_path:
            raise ValueError("output_path is not used in source mode")
        return self


class RepairApplyRequest(BaseModel):
    plan: RepairPlan
    confirm_path: str | None = None
    dry_run: bool = False


class RepairRestoreRequest(BaseModel):
    source_path: str
    confirm_path: str
    backup_suffix: str = Field(default=".bak", pattern=r"^\.[A-Za-z0-9_-]{1,20}$")
    dry_run: bool = False
