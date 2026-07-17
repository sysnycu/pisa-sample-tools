export type JobState = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';

export interface ApiErrorPayload {
  code: string;
  message: string;
  field?: string;
  details?: unknown;
  request_id?: string;
}

export interface Page<T> {
  items: T[];
  next_cursor?: string | null;
  total?: number;
}

export interface DataHealthFinding {
  id: string;
  severity: 'info' | 'warning' | 'error';
  code: string;
  title: string;
  detail: string;
  affected_runs?: number;
  dataset_id?: string | null;
  run_id?: string | null;
}

export interface DatasetDescriptor {
  id: string;
  name: string;
  path: string;
  experiment_count: number;
  run_count: number;
  updated_at?: string;
  status: 'ready' | 'building' | 'legacy' | 'unavailable';
  health?: DataHealthFinding[];
  tags?: string[];
  scenario_names?: string[];
  sampler_names?: string[];
  simulator_names?: string[];
  av_names?: string[];
  generated_at?: string;
  report_build_version?: number;
  latest_report_build_version?: number;
}

export interface OutcomeCounts {
  success: number;
  fail: number;
  invalid: number;
  unknown: number;
}

export interface ReportSummary {
  dataset_id: string;
  generated_at?: string;
  experiment_count: number;
  run_count: number;
  outcomes: OutcomeCounts;
  collision_count?: number;
  simulated_seconds?: number;
  wall_seconds?: number;
  parameters?: Array<{ name: string; min: number; max: number; unit?: string }>;
  health?: DataHealthFinding[];
  experiment_summaries?: Array<{ experiment: string; simulator?: string | null; av?: string | null; sampler?: string | null; total_samples: number; success: number; fail: number; invalid: number; unknown: number; avg_time_seconds?: number | null; avg_speedup?: number | null }>;
}

export interface ReportValidateRequest {
  path?: string;
  experiments?: Array<Record<string, unknown>>;
  spec?: Record<string, unknown>;
  deep?: boolean;
}

export interface ReportBuildRequest {
  results_paths?: string[];
  experiments?: Array<Record<string, unknown>>;
  campaign_path?: string;
  output_dir: string;
  spec_path?: string;
  overwrite?: boolean;
  validation_mode?: 'strict' | 'permissive';
  deep_validation?: boolean;
  report_mode?: 'interactive' | 'static';
  sensitivity?: boolean;
  engine?: 'auto' | 'normalized' | 'legacy';
}

export interface ExperimentPreview {
  dataset_id: string;
  results: string;
  scenario_name?: string;
  map_name?: string;
  simulator?: string;
  av?: string;
  sampler?: string;
  xodr_path?: string;
  run_count: number;
  parameters?: string[];
  metrics?: string[];
  warnings?: string[];
  suggested_report_name?: string;
  [key: string]: unknown;
}

export interface LegacyRebuildRequest {
  output_dir?: string;
  sensitivity?: boolean;
  overwrite?: boolean;
}

export interface ExperimentPreset {
  id: string;
  name: string;
  description?: string;
  scenario?: string;
  simulator?: string;
  automation?: string;
  sampler?: string;
  sample_count?: number;
  updated_at?: string;
  raw: Record<string, unknown>;
}

export interface RunnerComponent {
  id: string;
  name: string;
  kind: 'simulator' | 'av';
}

export interface PresetCatalog extends Page<ExperimentPreset> {
  components: RunnerComponent[];
}

export type RunnerAction = 'build' | 'start' | 'run_all' | 'report';
export type RunnerResumeAction = 'run' | 'stop' | 'report';

export interface RunRecord {
  id: string;
  parameter_hash?: string;
  sample_id?: string;
  scenario_id?: string;
  iteration?: number;
  experiment: string;
  outcome: 'success' | 'fail' | 'invalid' | 'unknown';
  stop_reason?: string;
  duration_seconds?: number;
  min_ttc?: number | null;
  collision?: boolean;
  parameters?: Record<string, number | string | null>;
}

export interface TracePoint {
  time: number;
  x?: number;
  y?: number;
  yaw?: number;
  speed?: number;
  ttc?: number | null;
  throttle?: number | null;
  brake?: number | null;
  steer?: number | null;
  acceleration?: number | null;
  yaw_rate?: number | null;
  values?: Record<string, string | number | boolean | null>;
}

export interface CaseDetail {
  run: RunRecord;
  traces: Record<string, TracePoint[]>;
  events?: Array<{ time: number; type: string; label: string; severity?: string; x?: number | null; y?: number | null; details?: Record<string, string | number | boolean | null> }>;
  geometry?: Array<Record<string, string | number | boolean | null>>;
  trace_channels?: Record<string, { point_count?: number; fields?: string[] }>;
  attempts?: Array<Record<string, unknown>>;
  navigation?: { previous_run_id?: string | null; next_run_id?: string | null; ordinal?: number; total?: number; sample_key?: { field: string; value: string }; comparison_runs?: Array<{ run_id: string; dataset_id: string; scenario_id: string; outcome_class: string }> };
  map?: {
    status?: string;
    name?: string;
    source?: string;
    warning?: string;
    polyline?: Array<[number, number]>;
    geometry?: {
      roads?: Array<{
        road_id?: string;
        name?: string;
        junction?: boolean;
        reference_line?: Array<[number, number]>;
        boundaries?: Array<Array<[number, number]>>;
      }>;
    };
  };
  ego_goal?: { x?: number; y?: number; target_speed?: number; source_type?: string } | null;
  ego_goal_warning?: string | null;
}

export interface ReportBrowserEntry {
  name: string;
  path: string;
  kind: string;
  is_report: boolean;
  looks_like_output: boolean;
}

export interface ReportBrowserResult {
  path: string;
  parent?: string | null;
  roots: string[];
  current_report?: DatasetDescriptor | null;
  looks_like_output?: boolean;
  entries: ReportBrowserEntry[];
  truncated?: boolean;
}

export interface ReportSourceInspection {
  path: string;
  valid: boolean;
  dataset_count: number;
  run_count: number;
  missing_run_count: number;
  suggested_output_dir: string;
  warnings: string[];
  datasets: Array<Record<string, unknown>>;
}

export interface ScatterField {
  key: string;
  label: string;
  source: 'parameter' | 'metric' | 'control' | 'order' | 'outcome' | 'run';
  numeric_count?: number | null;
  total_count?: number;
}

export interface ScatterResult {
  fields: ScatterField[];
  datasets: string[];
  stop_reasons: string[];
  stop_conditions: string[];
  selection: { x: string; y: string; color: string; dataset?: string | null };
  points: Array<{ run_id: string; dataset_id: string; scenario_id: string; sample_id?: string | null; parameter_hash?: string | null; ordinal: number; outcome: string; collision: boolean; stop_condition?: string | null; stop_reason?: string | null; x: number; y: number; color?: unknown }>;
  returned: number;
  scanned: number;
  limit?: number | null;
  truncated: boolean;
}

export interface ComparisonClass {
  id: string;
  left: string;
  right: string;
  role:
    | 'duplicate_alias'
    | 'paired_replicate'
    | 'paired_system_intervention'
    | 'paired_policy_intervention'
    | 'partial_pair'
    | 'unpaired_common_domain'
    | 'descriptive_only'
    | 'incompatible';
  matched: number;
  left_only: number;
  right_only: number;
  agreement?: number;
  information_consistent_count?: number;
  information_comparable_count?: number;
  information_agreement_ratio?: number | null;
  information_scope?: string;
  information_exclusions?: string;
  note?: string;
}

export interface CrossDiscreteComparison {
  key: string;
  label: string;
  consistent_count: number;
  comparable_count: number;
  agreement_ratio?: number | null;
  unavailable_sample_count: number;
}

export interface CrossContinuousComparison {
  key: string;
  label: string;
  unit?: string | null;
  eligible_sample_count: number;
  partial_sample_count: number;
  unavailable_sample_count: number;
  valid_execution_count: number;
  total_execution_count: number;
  missing_execution_count: number;
  invalid_execution_count: number;
  variation_max?: number | null;
  variation_min?: number | null;
  variation_p95?: number | null;
  variation_std?: number | null;
  variation_median?: number | null;
  representatives?: Partial<Record<'max' | 'min' | 'p95' | 'std' | 'median', {
    parameter_hash: string;
    run_id: string;
    variation: number;
  }>>;
  validity_rule: string;
}

export interface CrossTrajectoryRepresentative {
  parameter_hash: string;
  variation: number;
  distance_to_statistic?: number;
  left_experiment: string;
  right_experiment: string;
  left_run_id: string;
  right_run_id: string;
  common_steps: number;
}

export interface CrossTrajectoryStatistic {
  key: 'ade' | 'fde';
  max?: number | null;
  min?: number | null;
  mean?: number | null;
  std?: number | null;
  median?: number | null;
  representatives: Partial<Record<'max' | 'min' | 'mean' | 'std' | 'median', CrossTrajectoryRepresentative>>;
}

export interface CrossTrajectoryComparison {
  available: boolean;
  reason?: string;
  eligible_sample_count: number;
  partial_sample_count: number;
  unavailable_sample_count: number;
  experiment_pair_count: number;
  alignment_rule?: string;
  ade?: CrossTrajectoryStatistic;
  fde?: CrossTrajectoryStatistic;
}

export interface CrossExperimentComparison {
  available: boolean;
  reason?: string;
  experiments: string[];
  experiment_count: number;
  excluded_duplicate_aliases: string[];
  pairing_key?: string;
  common_sample_count: number;
  union_sample_count: number;
  excluded_noncommon_sample_count: number;
  hash_quality: Record<string, { run_count: number; missing_hash_runs: number; ambiguous_hashes: number }>;
  discrete: CrossDiscreteComparison[];
  continuous: CrossContinuousComparison[];
  trajectory?: CrossTrajectoryComparison;
  most_similar_pair?: {
    left: string;
    right: string;
    information_consistent_count: number;
    information_comparable_count: number;
    information_agreement_ratio?: number | null;
    information_scope?: string;
    information_exclusions?: string;
  } | null;
  variation_definition?: string;
  std_definition?: string;
  missing_value_rule?: string;
}

export interface ComparisonResult {
  items: ComparisonClass[];
  cross_experiment?: CrossExperimentComparison;
}

export interface MediaItem {
  id: string;
  run_id?: string;
  name: string;
  kind: 'image' | 'video' | 'animation' | 'keyframes';
  source: 'recorded' | 'derived';
  mime_type: string;
  url?: string;
  thumbnail_url?: string;
  created_at?: string;
}

export interface MediaCreateRequest {
  run_id: string;
  run_ids?: string[];
  format: 'gif' | 'mp4' | 'webm' | 'png';
  fps?: number;
  max_frames?: number;
  playback_rate?: number;
  width?: number;
  height?: number;
  overwrite?: boolean;
  include_map?: boolean;
  map_reference?: boolean;
  map_boundaries?: boolean;
  map_junctions?: boolean;
  show_bounding_boxes?: boolean;
  follow_cursor?: boolean;
  trail_only?: boolean;
  render_mode?: 'standard' | 'trajectory_view';
  show_ego?: boolean;
  show_agents?: boolean;
  actor_names?: string[];
  show_goal?: boolean;
  show_grid?: boolean;
  show_axes?: boolean;
  x_min?: number;
  x_max?: number;
  y_min?: number;
  y_max?: number;
}

export interface Job {
  id: string;
  kind: string;
  title: string;
  state: JobState;
  phase?: string;
  progress?: { current: number; total?: number; unit?: string };
  created_at: string;
  updated_at?: string;
  message?: string;
  artifacts?: Array<{ name: string; url: string }>;
  source?: 'workbench' | 'runner';
  report_id?: string;
}

export interface RuntimeResource {
  id: string;
  type: string;
  name: string;
  state?: string;
}

export interface SamplerParameter {
  name: string;
  min: number;
  max: number;
  values?: number[];
}

export interface InlineSamplePreviewRequest {
  method: 'grid' | 'lhs' | 'sobol' | 'random';
  count: number;
  seed?: number;
  parameters: SamplerParameter[];
  source_file?: never;
}

export interface SourceSamplePreviewRequest {
  source_file: string;
  sampler_name?: string;
  source_type?: string;
  module_path?: string;
  config_path?: string;
  config?: Record<string, unknown>;
  max_samples?: number;
  method?: never;
  count?: never;
  seed?: never;
  parameters?: never;
}

export type SamplePreviewRequest = InlineSamplePreviewRequest | SourceSamplePreviewRequest;

export interface SamplePreview {
  method: string;
  count: number;
  parameter_names: string[];
  samples: number[][];
  warnings?: string[];
}

export interface SampleExportRequest {
  output_dir: string;
  runner_spec_path?: string;
  sampler_spec_path?: string;
  scenario_path?: string;
  shard_size?: number;
  num_shards?: number;
  source_path_mode: 'absolute' | 'relative-to-output';
  create_zip: boolean;
  zip_path?: string;
  dry_run: boolean;
  overwrite: boolean;
}

export interface SampleAnalyzeRequest {
  output_dir: string;
  runner_spec_path?: string;
  samples_path?: string;
  results_path?: string;
  params?: string[];
  color_by?: string;
  bins?: number;
  post_outcome_config_path?: string;
  post_outcome_mode?: 'overlay' | 'replace';
  overwrite?: boolean;
}

export interface ExportRequest {
  visualization_id?: string;
  artifact_path?: string;
  format: 'svg' | 'pdf' | 'png' | 'csv' | 'json' | 'mp4' | 'webm' | 'gif';
  preset?: 'paper-single' | 'paper-double' | 'slides-hd' | 'slides-4k';
  dpi?: 300 | 600;
  background?: 'white' | 'transparent';
  filters?: Record<string, unknown>;
}

export interface VisualizationSpec {
  id: string;
  title: string;
  subtitle?: string;
  kind: 'bar' | 'line' | 'scatter' | 'heatmap' | 'pie' | 'trajectory' | 'image';
  option: Record<string, unknown>;
  data_hash?: string;
  clipped_count?: number;
  raw_range?: [number, number];
  artifact_path?: string;
  source_url?: string;
}

export interface SnapshotResult {
  available: boolean;
  path: string;
  url: string;
  portable: boolean;
}

export interface RepairChange {
  path: string;
  sha256?: string;
  input_path?: string;
  input_sha256?: string;
  original_rows?: number;
  inserted_rows?: number;
  result_rows?: number;
  time_shift_ms?: number;
  backup_exists?: boolean;
  [key: string]: unknown;
}

/** The complete signed payload returned by /tools/repair/scan. */
export interface RepairPlan {
  version: 1;
  signature: string;
  source_path: string;
  mode: 'overlay' | 'source';
  output_path?: string | null;
  init_state_path?: string | null;
  reference_root?: string | null;
  backup_suffix: string;
  time_step_ms?: number | null;
  findings: Array<Record<string, unknown>>;
  changes: RepairChange[];
  destructive: boolean;
}

export interface RepairScanRequest {
  source_path: string;
  init_state_path?: string;
  reference_root?: string;
  mode: 'overlay' | 'source';
  output_path?: string;
  backup_suffix?: string;
  time_step_ms?: number;
}

export interface TrajectoryRequest {
  input_path: string;
  output_dir: string;
  overwrite?: boolean;
  width?: number;
  height?: number;
  x_range?: [number, number];
  y_range?: [number, number];
  equal_scale?: boolean;
  ignore_agent_ids?: string[];
  origin_agent_id?: string;
}

export interface TrajectoryCompareRequest {
  left_path: string;
  right_path: string;
  output_dir: string;
  left_label?: string;
  right_label?: string;
  ignore_agent_ids?: string[];
  overwrite?: boolean;
  width?: number;
  height?: number;
  equal_scale?: boolean;
}

export interface OutcomeEvalRequest {
  input_path: string;
  config_path: string;
  output_dir: string;
  mode?: 'overlay' | 'replace';
  default_outcome?: 'success' | 'fail' | 'invalid' | 'unknown';
  overwrite?: boolean;
  write_monitor_outcome?: boolean;
}
