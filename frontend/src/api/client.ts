import type {
  ApiErrorPayload,
  CaseDetail,
  ComparisonClass,
  ComparisonResult,
  CrossExperimentComparison,
  DataHealthFinding,
  DatasetDescriptor,
  ExperimentPreset,
  ExportRequest,
  Job,
  JobState,
  MediaItem,
  MediaCreateRequest,
  LegacyRebuildRequest,
  OutcomeEvalRequest,
  Page,
  PresetCatalog,
  RepairPlan,
  RepairScanRequest,
  ReportBuildRequest,
  ReportBrowserResult,
  ReportSourceInspection,
  ReportSummary,
  ReportValidateRequest,
  RunRecord,
  RunnerAction,
  RunnerResumeAction,
  RuntimeResource,
  SampleAnalyzeRequest,
  SampleExportRequest,
  SamplePreview,
  SamplePreviewRequest,
  SnapshotResult,
  ScatterResult,
  TracePoint,
  TrajectoryCompareRequest,
  TrajectoryRequest,
  VisualizationSpec,
} from './types';

type JsonMap = Record<string, unknown>;

export class ApiError extends Error {
  readonly status: number;
  readonly payload?: ApiErrorPayload;

  constructor(status: number, payload?: ApiErrorPayload) {
    super(payload?.message ?? `Request failed (${status})`);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

function isMap(value: unknown): value is JsonMap {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function map(value: unknown): JsonMap {
  return isMap(value) ? value : {};
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function stringValue(value: unknown, fallback = ''): string {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : fallback;
}

function numberValue(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return undefined;
}

function booleanValue(value: unknown): boolean | undefined {
  if (typeof value === 'boolean') return value;
  if (value === 1 || value === '1' || value === 'true') return true;
  if (value === 0 || value === '0' || value === 'false') return false;
  return undefined;
}

function queryString(values: Record<string, string | number | boolean | undefined>) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== '') params.set(key, String(value));
  }
  const query = params.toString();
  return query ? `?${query}` : '';
}

function titleCase(value: string) {
  return value.replace(/[-_]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function basename(value: string) {
  const parts = value.replace(/[\\/]+$/, '').split(/[\\/]/);
  return parts.at(-1) || 'report';
}

function normalizeFinding(value: unknown, index: number): DataHealthFinding {
  const raw = map(value);
  const code = stringValue(raw.code, `finding_${index + 1}`);
  const severityValue = stringValue(raw.severity, 'warning');
  const severity = severityValue === 'error' || severityValue === 'info' ? severityValue : 'warning';
  const details = map(raw.details);
  return {
    id: stringValue(raw.id, `${code}-${index}`),
    severity,
    code,
    title: stringValue(raw.title, titleCase(code)),
    detail: stringValue(raw.detail ?? raw.message, 'This finding was recorded during report indexing.'),
    affected_runs: numberValue(raw.affected_runs ?? details.affected_runs),
    dataset_id: stringValue(raw.dataset_id ?? details.dataset_id) || null,
    run_id: stringValue(raw.run_id ?? details.run_id) || null,
  };
}

function normalizeDataset(value: unknown): DatasetDescriptor {
  const raw = map(value);
  const path = stringValue(raw.path ?? raw.results);
  const updateAvailable = Boolean(raw.update_available);
  const explicitStatus = stringValue(raw.status);
  const status: DatasetDescriptor['status'] = explicitStatus === 'building' || explicitStatus === 'unavailable'
    ? explicitStatus
    : explicitStatus === 'legacy' || updateAvailable
      ? 'legacy'
      : 'ready';
  const warningCount = numberValue(raw.warning_count) ?? 0;
  const explicitHealth = list(raw.health ?? raw.findings).map(normalizeFinding);
  const health = explicitHealth.length
    ? explicitHealth
    : warningCount
      ? [{ id: 'report-warnings', severity: 'warning' as const, code: 'report_warnings', title: 'Report warnings', detail: `${warningCount} warning${warningCount === 1 ? '' : 's'} were recorded while building this report.` }]
      : [];
  return {
    id: stringValue(raw.id),
    name: stringValue(raw.name, basename(path)),
    path,
    experiment_count: numberValue(raw.experiment_count ?? raw.dataset_count) ?? 0,
    run_count: numberValue(raw.run_count ?? raw.total) ?? 0,
    updated_at: stringValue(raw.updated_at ?? raw.generated_at) || undefined,
    status,
    health,
    tags: list(raw.tags).map(String),
    scenario_names: list(raw.scenario_names).map(String),
    sampler_names: list(raw.sampler_names).map(String),
    simulator_names: list(raw.simulator_names).map(String),
    av_names: list(raw.av_names).map(String),
    generated_at: stringValue(raw.generated_at) || undefined,
    report_build_version: numberValue(raw.report_build_version),
    latest_report_build_version: numberValue(raw.latest_report_build_version),
  };
}

function normalizeSummary(value: unknown): ReportSummary {
  const raw = map(value);
  const report = map(raw.report);
  const data = map(raw.data);
  const nested = map(data.summary ?? raw.summary);
  const outcomes = map(raw.outcomes ?? nested.outcomes);
  const runCount = numberValue(raw.run_count ?? raw.total ?? nested.run_count ?? nested.total ?? report.run_count) ?? 0;
  const success = numberValue(raw.success ?? outcomes.success ?? nested.success) ?? 0;
  const fail = numberValue(raw.fail ?? raw.failure ?? outcomes.fail ?? outcomes.failure ?? nested.fail ?? nested.failure) ?? 0;
  const invalid = numberValue(raw.invalid ?? outcomes.invalid ?? nested.invalid) ?? 0;
  const explicitUnknown = numberValue(raw.unknown ?? outcomes.unknown ?? nested.unknown);
  const known = success + fail + invalid;
  const healthValues = list(raw.health).length ? list(raw.health) : list(data.findings ?? raw.findings);
  return {
    dataset_id: stringValue(raw.dataset_id ?? raw.id ?? report.id),
    generated_at: stringValue(raw.generated_at ?? data.generated_at ?? report.generated_at) || undefined,
    experiment_count: numberValue(raw.experiment_count ?? data.aggregate_dataset_count ?? data.dataset_count ?? report.experiment_count) ?? 0,
    run_count: runCount,
    outcomes: {
      success,
      fail,
      invalid,
      unknown: explicitUnknown ?? Math.max(0, runCount - known),
    },
    collision_count: numberValue(raw.collision_count ?? raw.collision ?? outcomes.collision ?? nested.collision),
    simulated_seconds: numberValue(raw.simulated_seconds ?? nested.simulated_seconds),
    wall_seconds: numberValue(raw.wall_seconds ?? nested.wall_seconds),
    parameters: list(raw.parameters ?? data.parameters) as ReportSummary['parameters'],
    health: healthValues.map(normalizeFinding),
    experiment_summaries: list(raw.experiment_summaries).map((value) => map(value)) as ReportSummary['experiment_summaries'],
  };
}

function canonicalOutcome(value: unknown): RunRecord['outcome'] {
  const outcome = stringValue(value, 'unknown').toLowerCase();
  if (['success', 'passed', 'pass'].includes(outcome)) return 'success';
  if (['fail', 'failure', 'failed', 'test_fail'].includes(outcome)) return 'fail';
  if (outcome === 'invalid') return 'invalid';
  return 'unknown';
}

function normalizeRun(value: unknown): RunRecord {
  const raw = map(value);
  const metrics = map(raw.metrics ?? raw.metrics_json);
  const id = stringValue(raw.id ?? raw.run_id ?? raw.scenario_id);
  const iteration = numberValue(raw.iteration) ?? numberValue(id.match(/(?:iteration[_:-]?)(\d+)/i)?.[1]);
  return {
    id,
    parameter_hash: stringValue(raw.parameter_hash) || undefined,
    sample_id: stringValue(raw.sample_id) || undefined,
    scenario_id: stringValue(raw.scenario_id) || undefined,
    iteration,
    experiment: stringValue(raw.experiment ?? raw.experiment_id ?? raw.dataset_id, 'Unlabeled experiment'),
    outcome: canonicalOutcome(raw.outcome ?? raw.normalized_outcome ?? raw.outcome_class),
    stop_reason: stringValue(raw.stop_reason ?? raw.stop_condition ?? raw.termination_reason) || undefined,
    duration_seconds: numberValue(raw.duration_seconds ?? metrics.duration_seconds ?? metrics.simulation_time_seconds ?? metrics.sim_time_seconds),
    min_ttc: numberValue(raw.min_ttc ?? metrics.min_ttc ?? metrics.minimum_ttc) ?? null,
    collision: booleanValue(raw.collision ?? raw.has_collision) ?? ((numberValue(metrics.collision_count) ?? 0) > 0),
    parameters: map(raw.parameters ?? raw.params ?? raw.params_json) as RunRecord['parameters'],
  };
}

function traceNumber(raw: JsonMap, keys: string[]): number | undefined {
  for (const key of keys) {
    const value = numberValue(raw[key]);
    if (value !== undefined) return value;
  }
  return undefined;
}

function normalizeTracePoint(value: unknown, index: number): TracePoint {
  const raw = map(value);
  const millis = traceNumber(raw, ['sim_time_ms', 'timestamp_ms']);
  const scalarValues: Record<string, string | number | boolean | null> = {};
  for (const [key, item] of Object.entries(map(raw.values))) {
    if (item === null || ['string', 'number', 'boolean'].includes(typeof item)) {
      scalarValues[key] = item as string | number | boolean | null;
    }
  }
  return {
    time: traceNumber(raw, ['time', 'time_s', 'sim_time_s', 'timestamp']) ?? (millis !== undefined ? millis / 1000 : index),
    x: traceNumber(raw, ['x', 'position_x', 'location_x']),
    y: traceNumber(raw, ['y', 'position_y', 'location_y']),
    yaw: traceNumber(raw, ['yaw', 'heading']),
    speed: traceNumber(raw, ['speed', 'velocity', 'speed_mps']),
    ttc: traceNumber(raw, ['ttc', 'min_ttc', 'ttc_s']) ?? null,
    throttle: traceNumber(raw, ['throttle']),
    brake: traceNumber(raw, ['brake']),
    steer: traceNumber(raw, ['steer', 'steering']),
    acceleration: traceNumber(raw, ['acceleration', 'acceleration_mps2']),
    yaw_rate: traceNumber(raw, ['yaw_rate']),
    values: scalarValues,
  };
}

function normalizeCase(value: unknown): CaseDetail {
  const raw = map(value);
  const traces: Record<string, TracePoint[]> = {};
  for (const [baseName, rows] of Object.entries(map(raw.traces))) {
    for (const [index, rowValue] of list(rows).entries()) {
      const row = map(rowValue);
      const agent = stringValue(row.agent_id ?? row.actor_id);
      const name = agent && !baseName.match(/(^|[ _·-])(ego|agent[_ -]?\d+)($|[ _·-])/i) ? `${baseName} · agent ${agent}` : baseName;
      (traces[name] ??= []).push(normalizeTracePoint(row, index));
    }
  }
  if (!Object.keys(traces).length) {
    for (const [name, values] of Object.entries(map(raw.series))) {
      const points = list(values).map((item, index) => {
        if (isMap(item)) return normalizeTracePoint(item, index);
        return { time: index, ttc: numberValue(item) ?? null } satisfies TracePoint;
      });
      if (points.length) traces[name] = points;
    }
  }
  const events = list(raw.events).map((value, index) => {
    const event = map(value);
    const millis = numberValue(event.sim_time_ms);
    return {
      time: numberValue(event.time ?? event.time_s) ?? (millis !== undefined ? millis / 1000 : index),
      type: stringValue(event.type ?? event.event_type, 'event'),
      label: stringValue(event.label ?? event.message ?? event.type, 'Recorded event'),
      severity: stringValue(event.severity) || undefined,
      x: numberValue(event.x) ?? null,
      y: numberValue(event.y) ?? null,
      details: map(event.details) as Record<string, string | number | boolean | null>,
    };
  });
  return {
    run: normalizeRun(raw.run ?? raw),
    traces,
    events,
    geometry: list(raw.geometry).map((item) => map(item) as Record<string, string | number | boolean | null>),
    trace_channels: map(raw.trace_channels) as CaseDetail['trace_channels'],
    attempts: list(raw.attempts).map(map),
    navigation: isMap(raw.navigation) ? raw.navigation as CaseDetail['navigation'] : undefined,
    map: isMap(raw.map) ? raw.map as CaseDetail['map'] : undefined,
    ego_goal: isMap(raw.ego_goal) ? raw.ego_goal as CaseDetail['ego_goal'] : null,
    ego_goal_warning: stringValue(raw.ego_goal_warning) || null,
  };
}

const comparisonRoles = new Set<ComparisonClass['role']>([
  'duplicate_alias', 'paired_replicate', 'paired_system_intervention', 'paired_policy_intervention',
  'partial_pair', 'unpaired_common_domain', 'descriptive_only', 'incompatible',
]);

function normalizeComparison(value: unknown, index: number): ComparisonClass {
  const raw = map(value);
  const roleValue = stringValue(raw.role ?? raw.comparison_role ?? raw.classification, 'descriptive_only') as ComparisonClass['role'];
  const role = comparisonRoles.has(roleValue) ? roleValue : 'descriptive_only';
  const id = stringValue(raw.id ?? raw.group_id, `comparison-${index + 1}`);
  return {
    id,
    left: stringValue(raw.left ?? raw.left_dataset_id ?? raw.baseline, `Comparison ${index + 1}`),
    right: stringValue(raw.right ?? raw.right_dataset_id ?? raw.candidate, id),
    role,
    matched: numberValue(raw.matched ?? raw.matched_count) ?? 0,
    left_only: numberValue(raw.left_only ?? raw.left_only_count) ?? 0,
    right_only: numberValue(raw.right_only ?? raw.right_only_count) ?? 0,
    agreement: numberValue(raw.agreement ?? raw.outcome_agreement),
    information_consistent_count: numberValue(raw.information_consistent_count),
    information_comparable_count: numberValue(raw.information_comparable_count),
    information_agreement_ratio: numberValue(raw.information_agreement_ratio) ?? null,
    information_scope: stringValue(raw.information_scope) || undefined,
    information_exclusions: stringValue(raw.information_exclusions) || undefined,
    note: stringValue(raw.note ?? raw.reason) || undefined,
  };
}

function normalizeCrossExperiment(value: unknown): CrossExperimentComparison {
  const raw = map(value);
  const hashQuality = Object.fromEntries(Object.entries(map(raw.hash_quality)).map(([dataset, value]) => {
    const quality = map(value);
    return [dataset, {
      run_count: numberValue(quality.run_count) ?? 0,
      missing_hash_runs: numberValue(quality.missing_hash_runs) ?? 0,
      ambiguous_hashes: numberValue(quality.ambiguous_hashes) ?? 0,
    }];
  }));
  const normalizeTrajectoryStatistic = (value: unknown, fallbackKey: 'ade' | 'fde') => {
    const statistic = map(value);
    return {
      key: (stringValue(statistic.key, fallbackKey) === 'fde' ? 'fde' : 'ade') as 'ade' | 'fde',
      max: numberValue(statistic.max) ?? null,
      min: numberValue(statistic.min) ?? null,
      mean: numberValue(statistic.mean) ?? null,
      std: numberValue(statistic.std) ?? null,
      median: numberValue(statistic.median) ?? null,
      representatives: Object.fromEntries(Object.entries(map(statistic.representatives)).flatMap(([key, value]) => {
        const representative = map(value);
        const variation = numberValue(representative.variation);
        const leftRunId = stringValue(representative.left_run_id);
        const rightRunId = stringValue(representative.right_run_id);
        return variation !== undefined && leftRunId && rightRunId ? [[key, {
          parameter_hash: stringValue(representative.parameter_hash), variation,
          distance_to_statistic: numberValue(representative.distance_to_statistic),
          left_experiment: stringValue(representative.left_experiment), right_experiment: stringValue(representative.right_experiment),
          left_run_id: leftRunId, right_run_id: rightRunId, common_steps: numberValue(representative.common_steps) ?? 0,
        }]] : [];
      })),
    };
  };
  const trajectory = map(raw.trajectory);
  const mostSimilar = map(raw.most_similar_pair);
  return {
    available: Boolean(raw.available),
    reason: stringValue(raw.reason) || undefined,
    experiments: list(raw.experiments).map(String),
    experiment_count: numberValue(raw.experiment_count) ?? list(raw.experiments).length,
    excluded_duplicate_aliases: list(raw.excluded_duplicate_aliases).map(String),
    pairing_key: stringValue(raw.pairing_key) || undefined,
    common_sample_count: numberValue(raw.common_sample_count) ?? 0,
    union_sample_count: numberValue(raw.union_sample_count) ?? 0,
    excluded_noncommon_sample_count: numberValue(raw.excluded_noncommon_sample_count) ?? 0,
    hash_quality: hashQuality,
    discrete: list(raw.discrete).map((value) => {
      const item = map(value);
      return {
        key: stringValue(item.key),
        label: stringValue(item.label, titleCase(stringValue(item.key))),
        consistent_count: numberValue(item.consistent_count) ?? 0,
        comparable_count: numberValue(item.comparable_count) ?? 0,
        agreement_ratio: numberValue(item.agreement_ratio) ?? null,
        unavailable_sample_count: numberValue(item.unavailable_sample_count) ?? 0,
      };
    }),
    continuous: list(raw.continuous).map((value) => {
      const item = map(value);
      return {
        key: stringValue(item.key),
        label: stringValue(item.label, titleCase(stringValue(item.key))),
        unit: stringValue(item.unit) || null,
        eligible_sample_count: numberValue(item.eligible_sample_count) ?? 0,
        partial_sample_count: numberValue(item.partial_sample_count) ?? 0,
        unavailable_sample_count: numberValue(item.unavailable_sample_count) ?? 0,
        valid_execution_count: numberValue(item.valid_execution_count) ?? 0,
        total_execution_count: numberValue(item.total_execution_count) ?? 0,
        missing_execution_count: numberValue(item.missing_execution_count) ?? 0,
        invalid_execution_count: numberValue(item.invalid_execution_count) ?? 0,
        variation_max: numberValue(item.variation_max) ?? null,
        variation_min: numberValue(item.variation_min) ?? null,
        variation_p95: numberValue(item.variation_p95) ?? null,
        variation_std: numberValue(item.variation_std) ?? null,
        variation_median: numberValue(item.variation_median) ?? null,
        representatives: Object.fromEntries(Object.entries(map(item.representatives)).flatMap(([key, value]) => {
          const representative = map(value);
          const runId = stringValue(representative.run_id);
          const variation = numberValue(representative.variation);
          return runId && variation !== undefined ? [[key, { parameter_hash: stringValue(representative.parameter_hash), run_id: runId, variation }]] : [];
        })),
        validity_rule: stringValue(item.validity_rule, 'finite numeric values in every experiment'),
      };
    }),
    trajectory: Object.keys(trajectory).length ? {
      available: Boolean(trajectory.available),
      reason: stringValue(trajectory.reason) || undefined,
      eligible_sample_count: numberValue(trajectory.eligible_sample_count) ?? 0,
      partial_sample_count: numberValue(trajectory.partial_sample_count) ?? 0,
      unavailable_sample_count: numberValue(trajectory.unavailable_sample_count) ?? 0,
      experiment_pair_count: numberValue(trajectory.experiment_pair_count) ?? 0,
      alignment_rule: stringValue(trajectory.alignment_rule) || undefined,
      ade: Object.keys(map(trajectory.ade)).length ? normalizeTrajectoryStatistic(trajectory.ade, 'ade') : undefined,
      fde: Object.keys(map(trajectory.fde)).length ? normalizeTrajectoryStatistic(trajectory.fde, 'fde') : undefined,
    } : undefined,
    most_similar_pair: Object.keys(mostSimilar).length ? {
      left: stringValue(mostSimilar.left), right: stringValue(mostSimilar.right),
      information_consistent_count: numberValue(mostSimilar.information_consistent_count) ?? 0,
      information_comparable_count: numberValue(mostSimilar.information_comparable_count) ?? 0,
      information_agreement_ratio: numberValue(mostSimilar.information_agreement_ratio) ?? null,
      information_scope: stringValue(mostSimilar.information_scope) || undefined,
      information_exclusions: stringValue(mostSimilar.information_exclusions) || undefined,
    } : null,
    variation_definition: stringValue(raw.variation_definition) || undefined,
    std_definition: stringValue(raw.std_definition) || undefined,
    missing_value_rule: stringValue(raw.missing_value_rule) || undefined,
  };
}

function normalizeMedia(value: unknown): MediaItem {
  const raw = map(value);
  const format = stringValue(raw.format).toLowerCase();
  const mimeType = stringValue(raw.mime_type ?? raw.media_type, 'application/octet-stream');
  const kind: MediaItem['kind'] = format === 'gif'
    ? 'animation'
    : ['mp4', 'webm'].includes(format) || mimeType.startsWith('video/')
      ? 'video'
      : 'image';
  const url = stringValue(raw.url ?? raw.download_url) || undefined;
  return {
    id: stringValue(raw.id ?? raw.path),
    run_id: stringValue(raw.run_id) || undefined,
    name: stringValue(raw.name, basename(stringValue(raw.path))),
    kind,
    source: stringValue(raw.source) === 'recorded' ? 'recorded' : 'derived',
    mime_type: mimeType,
    url,
    thumbnail_url: stringValue(raw.thumbnail_url) || (kind === 'image' ? url : undefined),
    created_at: stringValue(raw.created_at) || undefined,
  };
}

function normalizeChart(value: unknown, index: number): VisualizationSpec | undefined {
  const raw = map(value);
  const id = stringValue(raw.id ?? raw.path, `visualization-${index + 1}`);
  if (isMap(raw.option)) {
    const rawKind = stringValue(raw.kind, 'bar') as VisualizationSpec['kind'];
    const kinds = new Set<VisualizationSpec['kind']>(['bar', 'line', 'scatter', 'heatmap', 'pie', 'trajectory', 'image']);
    return {
      id,
      title: stringValue(raw.title ?? raw.name, titleCase(id)),
      subtitle: stringValue(raw.subtitle) || undefined,
      kind: kinds.has(rawKind) ? rawKind : 'bar',
      option: raw.option,
      data_hash: stringValue(raw.data_hash) || undefined,
      clipped_count: numberValue(raw.clipped_count),
      raw_range: Array.isArray(raw.raw_range) && raw.raw_range.length === 2 ? raw.raw_range as [number, number] : undefined,
      artifact_path: stringValue(raw.artifact_path ?? raw.path) || undefined,
      source_url: stringValue(raw.source_url ?? raw.download_url) || undefined,
    };
  }
  const format = stringValue(raw.format).toLowerCase();
  if (!['svg', 'png', 'jpg', 'jpeg', 'webp', 'gif'].includes(format)) return undefined;
  return {
    id,
    title: stringValue(raw.title ?? raw.name, titleCase(id)),
    subtitle: 'Pre-rendered report artifact',
    kind: 'image',
    option: {},
    artifact_path: stringValue(raw.path),
    source_url: stringValue(raw.download_url),
  };
}

function isoTime(value: unknown): string {
  if (typeof value === 'number') return new Date(value * 1000).toISOString();
  const text = stringValue(value);
  return text || new Date().toISOString();
}

function normalizeJob(value: unknown, source: 'workbench' | 'runner' = 'workbench'): Job {
  const raw = map(value);
  const rawStatus = stringValue(raw.status ?? raw.state, 'queued');
  const state: JobState = rawStatus === 'report_ready' ? 'succeeded'
    : ['queued', 'running', 'succeeded', 'failed', 'cancelled'].includes(rawStatus)
      ? rawStatus as JobState
      : 'failed';
  const rawId = stringValue(raw.id ?? raw.job_id);
  const id = source === 'runner' ? `runner:${rawId}` : rawId;
  const progressRaw = map(raw.progress);
  const current = numberValue(progressRaw.current);
  const lastMessage = list(raw.messages).at(-1);
  const error = isMap(raw.error) ? stringValue(raw.error.message) : stringValue(raw.error);
  const kind = stringValue(raw.kind ?? raw.action, source === 'runner' ? 'experiment' : 'job');
  const result = map(raw.result);
  const resultUrl = stringValue(result.download_url ?? result.url);
  const artifacts = list(raw.artifacts).flatMap((value) => {
    const artifact = map(value);
    const url = stringValue(artifact.url ?? artifact.download_url);
    return url ? [{ name: stringValue(artifact.name, basename(url)), url }] : [];
  });
  if (resultUrl) {
    artifacts.push({ name: basename(stringValue(result.path, resultUrl)), url: resultUrl });
  }
  return {
    id,
    kind,
    title: stringValue(raw.title ?? raw.label ?? raw.experiment_id, titleCase(kind)),
    state,
    phase: stringValue(raw.phase) || undefined,
    progress: current !== undefined ? {
      current,
      total: numberValue(progressRaw.total),
      unit: stringValue(progressRaw.unit) || undefined,
    } : undefined,
    created_at: isoTime(raw.created_at),
    updated_at: raw.completed_at || raw.started_at ? isoTime(raw.completed_at ?? raw.started_at) : undefined,
    message: error || stringValue(raw.message) || (isMap(lastMessage) ? stringValue(lastMessage.message) : '') || undefined,
    artifacts: artifacts.length ? artifacts : undefined,
    report_id: stringValue(result.report_id) || undefined,
    source,
  };
}

function completedJob(value: unknown, kind: string): Job {
  const raw = map(value);
  if (raw.id && (raw.status || raw.state)) return normalizeJob(raw);
  const url = stringValue(raw.download_url ?? raw.url);
  return {
    id: stringValue(raw.path ?? raw.download_url, `${kind}-${Date.now()}`),
    kind,
    title: titleCase(kind),
    state: 'succeeded',
    phase: 'complete',
    created_at: new Date().toISOString(),
    message: stringValue(raw.download_url ?? raw.path, 'Ready'),
    artifacts: url ? [{ name: basename(stringValue(raw.path, url)), url }] : undefined,
    source: 'workbench',
  };
}

function normalizePreset(value: unknown, fallbackId = ''): ExperimentPreset {
  const raw = map(value);
  const id = stringValue(raw.id ?? raw.preset_id, fallbackId);
  const scenario = map(raw.scenario);
  const simulator = map(raw.simulator);
  const av = map(raw.av);
  const sampler = map(raw.sampler);
  return {
    id,
    name: stringValue(raw.label ?? raw.name, id),
    description: stringValue(raw.description) || undefined,
    scenario: stringValue(scenario.name ?? scenario.path) || undefined,
    simulator: stringValue(simulator.component) || undefined,
    automation: stringValue(av.component) || undefined,
    sampler: stringValue(sampler.name) || undefined,
    sample_count: numberValue(sampler.count ?? sampler.sample_count),
    updated_at: stringValue(raw.updated_at) || undefined,
    raw,
  };
}

function normalizeSamplePreview(value: unknown, request: SamplePreviewRequest): SamplePreview {
  const raw = map(value);
  const rawSamples = list(raw.samples);
  let names = list(raw.parameter_names).map(String);
  if (!names.length && rawSamples.length && isMap(rawSamples[0])) {
    names = Object.keys(map(map(rawSamples[0]).params ?? rawSamples[0]));
  }
  if (!names.length && 'parameters' in request && request.parameters) {
    names = request.parameters.map((parameter) => parameter.name);
  }
  const samples = rawSamples.map((value) => {
    if (Array.isArray(value)) return value.map((item) => numberValue(item) ?? Number.NaN);
    const params = map(map(value).params ?? value);
    return names.map((name) => numberValue(params[name]) ?? Number.NaN);
  });
  return {
    method: stringValue(
      raw.method ?? raw.sampler_name ?? raw.sampler,
      'method' in request ? request.method : request.sampler_name ?? 'auto',
    ),
    count: numberValue(raw.count ?? raw.generated_samples ?? raw.total_count ?? raw.total_samples) ?? samples.length,
    parameter_names: names,
    samples,
    warnings: list(raw.warnings).map(String),
  };
}

export class ApiClient {
  constructor(private readonly baseUrl = '/api/v1') {}

  private async request<T = unknown>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: {
        Accept: 'application/json',
        ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
        ...init?.headers,
      },
    });
    if (!response.ok) {
      let payload: ApiErrorPayload | undefined;
      try {
        const body = await response.json() as unknown;
        payload = (isMap(body) && isMap(body.error) ? body.error : body) as ApiErrorPayload;
      } catch {
        payload = undefined;
      }
      throw new ApiError(response.status, payload);
    }
    if (response.status === 204) return undefined as T;
    return await response.json() as T;
  }

  datasets = {
    list: async (search = '', root?: string, recursive = true): Promise<Page<DatasetDescriptor>> => {
      const raw = map(await this.request(`/reports${queryString({ search: search || undefined, root, recursive })}`));
      let items = list(raw.items ?? raw.reports).map(normalizeDataset);
      const needle = search.trim().toLowerCase();
      if (needle) items = items.filter((item) => `${item.name} ${item.path}`.toLowerCase().includes(needle));
      return { items, total: items.length };
    },
    browse: async (path?: string): Promise<ReportBrowserResult> => {
      const raw = map(await this.request(`/reports/browser${queryString({ path })}`));
      return {
        path: stringValue(raw.path),
        parent: stringValue(raw.parent) || null,
        roots: list(raw.roots).map(String),
        current_report: isMap(raw.current_report) ? normalizeDataset(raw.current_report) : null,
        looks_like_output: Boolean(raw.looks_like_output),
        entries: list(raw.entries).map((value) => {
          const item = map(value);
          return { name: stringValue(item.name), path: stringValue(item.path), kind: stringValue(item.kind), is_report: Boolean(item.is_report), looks_like_output: Boolean(item.looks_like_output) };
        }),
        truncated: Boolean(raw.truncated),
      };
    },
    createDirectory: async (parent: string, name: string): Promise<ReportBrowserResult> => {
      const raw = map(await this.request('/reports/browser/directory', {
        method: 'POST', body: JSON.stringify({ parent, name }),
      }));
      return {
        path: stringValue(raw.path),
        parent: stringValue(raw.parent) || null,
        roots: list(raw.roots).map(String),
        current_report: isMap(raw.current_report) ? normalizeDataset(raw.current_report) : null,
        looks_like_output: Boolean(raw.looks_like_output),
        entries: list(raw.entries).map((value) => {
          const item = map(value);
          return { name: stringValue(item.name), path: stringValue(item.path), kind: stringValue(item.kind), is_report: Boolean(item.is_report), looks_like_output: Boolean(item.looks_like_output) };
        }),
        truncated: Boolean(raw.truncated),
      };
    },
    previewReport: async (path: string): Promise<DatasetDescriptor> => normalizeDataset(
      await this.request(`/reports/preview${queryString({ path })}`),
    ),
    previewById: async (id: string): Promise<DatasetDescriptor> => normalizeDataset(
      await this.request(`/reports/${encodeURIComponent(id)}/preview`),
    ),
    inspect: async (path: string): Promise<ReportSourceInspection> => {
      const raw = map(await this.request(`/reports/inspect${queryString({ path })}`));
      return {
        path: stringValue(raw.path), valid: Boolean(raw.valid),
        dataset_count: numberValue(raw.dataset_count) ?? 0, run_count: numberValue(raw.run_count) ?? 0,
        missing_run_count: numberValue(raw.missing_run_count) ?? 0,
        suggested_output_dir: stringValue(raw.suggested_output_dir), warnings: list(raw.warnings).map(String),
        datasets: list(raw.datasets).map(map),
      };
    },
    previewExperiment: (path: string): Promise<import('./types').ExperimentPreview> =>
      this.request(`/reports/experiment-preview${queryString({ path })}`),
    compatibility: (experiments: Array<Record<string, unknown>>): Promise<{ compatible: boolean; errors: Array<Record<string, unknown>>; component_differences?: Record<string, unknown> }> =>
      this.request('/reports/compatibility', { method: 'POST', body: JSON.stringify({ experiments }) }),
    validate: (body: ReportValidateRequest) =>
      this.request<JsonMap>('/reports/validate', { method: 'POST', body: JSON.stringify(body) }),
    build: async (body: ReportBuildRequest): Promise<Job> => normalizeJob(
      await this.request('/reports/build', { method: 'POST', body: JSON.stringify(body) }),
    ),
    rebuild: async (id: string, body: LegacyRebuildRequest): Promise<Job> => normalizeJob(
      await this.request(`/reports/${encodeURIComponent(id)}/rebuild`, { method: 'POST', body: JSON.stringify(body) }),
    ),
    details: (id: string): Promise<Record<string, unknown>> => this.request(`/reports/${encodeURIComponent(id)}/details`),
    rename: async (id: string, newName: string): Promise<DatasetDescriptor> => normalizeDataset(
      await this.request(`/reports/${encodeURIComponent(id)}/rename`, { method: 'POST', body: JSON.stringify({ new_name: newName }) }),
    ),
    delete: async (id: string, confirmName: string): Promise<void> => {
      await this.request(`/reports/${encodeURIComponent(id)}`, { method: 'DELETE', body: JSON.stringify({ confirm_name: confirmName }) });
    },
    scatter: async (id: string, options: { x?: string; y?: string; color?: string; dataset?: string; stop_reason?: string; limit?: number } = {}): Promise<ScatterResult> =>
      this.request(`/reports/${encodeURIComponent(id)}/scatter${queryString(options)}`),
    summary: async (id: string) => normalizeSummary(await this.request(`/reports/${encodeURIComponent(id)}/overview`)),
    runs: async (id: string, options: { cursor?: string; limit?: number; search?: string; outcome?: string; experiment?: string; sort?: string; descending?: boolean } = {}): Promise<Page<RunRecord>> => {
      const raw = map(await this.request(`/reports/${encodeURIComponent(id)}/runs${queryString({
        cursor: options.cursor,
        limit: options.limit,
        query: options.search,
        outcome: options.outcome,
        experiment: options.experiment,
        sort: options.sort,
        descending: options.descending,
      })}`));
      return {
        items: list(raw.items).map(normalizeRun),
        next_cursor: stringValue(raw.next_cursor) || null,
        total: numberValue(raw.total),
      };
    },
    case: async (id: string, runId: string, maximumPoints = 2_000, includeMap = true) => normalizeCase(await this.request(`/reports/${encodeURIComponent(id)}/cases/${encodeURIComponent(runId)}${queryString({ maximum_points: maximumPoints, include_map: includeMap })}`)),
    charts: async (id: string, section: string): Promise<VisualizationSpec[]> => {
      const rawValue = await this.request(`/reports/${encodeURIComponent(id)}/charts${queryString({ section })}`);
      const raw = map(rawValue);
      const generated = list(raw.visualizations);
      const values = Array.isArray(rawValue) ? rawValue : generated.length ? generated : list(raw.items);
      return values.map(normalizeChart).filter((item): item is VisualizationSpec => Boolean(item));
    },
    comparisons: async (id: string): Promise<ComparisonResult> => {
      const rawValue = await this.request(`/reports/${encodeURIComponent(id)}/comparisons`);
      const raw = map(rawValue);
      const values = Array.isArray(rawValue) ? rawValue : list(raw.items);
      return {
        items: values.map(normalizeComparison),
        cross_experiment: isMap(raw.cross_experiment) ? normalizeCrossExperiment(raw.cross_experiment) : undefined,
      };
    },
    media: async (id: string): Promise<Page<MediaItem>> => {
      const rawValue = await this.request(`/reports/${encodeURIComponent(id)}/media`);
      const values = Array.isArray(rawValue) ? rawValue : list(map(rawValue).items);
      const items = values.map(normalizeMedia);
      return { items, total: items.length };
    },
    createMedia: async (id: string, body: MediaCreateRequest): Promise<Job> => normalizeJob(
      await this.request(`/reports/${encodeURIComponent(id)}/media`, { method: 'POST', body: JSON.stringify(body) }),
    ),
    export: async (id: string, body: ExportRequest) => completedJob(
      await this.request(`/reports/${encodeURIComponent(id)}/export`, { method: 'POST', body: JSON.stringify(body) }),
      `${body.format}_export`,
    ),
    snapshot: async (id: string): Promise<SnapshotResult> => {
      const raw = map(await this.request(`/reports/${encodeURIComponent(id)}/snapshot`, { method: 'POST' }));
      return {
        available: Boolean(raw.available),
        path: stringValue(raw.path),
        url: stringValue(raw.url, `/api/v1/reports/${encodeURIComponent(id)}/snapshot`),
        portable: Boolean(raw.portable),
      };
    },
  };

  experiments = {
    presets: async (): Promise<PresetCatalog> => {
      const raw = map(await this.request('/runner/presets'));
      const itemValues = list(raw.items).length
        ? list(raw.items)
        : Object.entries(map(raw.experiments)).map(([id, experiment]) => ({ id, ...map(experiment) }));
      const components = Object.entries(map(raw.components)).map(([id, value]) => {
        const component = map(value);
        const kind = stringValue(component.kind) === 'simulator' ? 'simulator' as const : 'av' as const;
        return { id, name: stringValue(component.label, id), kind };
      });
      return { items: itemValues.map((item) => normalizePreset(item)), total: itemValues.length, components };
    },
    savePreset: async (body: { preset_id: string; template_id: string; label: string; simulator_component: string; av_component: string; tags?: string[] }): Promise<ExperimentPreset> => {
      const raw = map(await this.request('/runner/presets', { method: 'POST', body: JSON.stringify(body) }));
      return normalizePreset(raw.experiment, stringValue(raw.preset_id, body.preset_id));
    },
    updatePreset: async (id: string, experiment: Record<string, unknown>): Promise<ExperimentPreset> => {
      const raw = map(await this.request(`/runner/presets/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify({ experiment }) }));
      return normalizePreset(raw.experiment, stringValue(raw.preset_id, id));
    },
    renamePreset: async (id: string, body: { new_id: string; label?: string }): Promise<ExperimentPreset> => {
      const raw = map(await this.request(`/runner/presets/${encodeURIComponent(id)}/rename`, { method: 'POST', body: JSON.stringify(body) }));
      return normalizePreset(raw.experiment, stringValue(raw.preset_id, body.new_id));
    },
    deletePreset: async (id: string): Promise<void> => {
      await this.request(`/runner/presets/${encodeURIComponent(id)}/delete`, { method: 'POST', body: JSON.stringify({ confirm: true }) });
    },
    run: async (body: { experiment_id: string; action: RunnerAction; overrides?: Record<string, unknown> }): Promise<Job> =>
      normalizeJob(await this.request('/runner/jobs', { method: 'POST', body: JSON.stringify(body) }), 'runner'),
    resume: async (jobId: string, action: RunnerResumeAction): Promise<Job> => {
      const rawId = jobId.startsWith('runner:') ? jobId.slice(7) : jobId;
      return normalizeJob(await this.request(`/runner/jobs/${encodeURIComponent(rawId)}/resume`, { method: 'POST', body: JSON.stringify({ action }) }), 'runner');
    },
    resources: async (): Promise<RuntimeResource[]> => {
      const raw = map(await this.request('/runner/resources'));
      return list(raw.containers ?? raw.items).map((value) => {
        const resource = map(value);
        return {
          id: stringValue(resource.id ?? resource.name),
          type: stringValue(resource.type, 'container'),
          name: stringValue(resource.name),
          state: stringValue(resource.state ?? resource.status) || undefined,
        };
      });
    },
    cleanup: async (names: string[]) => Promise.all(names.map((name) =>
      this.request<JsonMap>('/runner/resources/cleanup', { method: 'POST', body: JSON.stringify({ name }) }),
    )),
  };

  samples = {
    preview: async (body: SamplePreviewRequest) => normalizeSamplePreview(
      await this.request('/samples/preview', { method: 'POST', body: JSON.stringify(body) }),
      body,
    ),
    export: async (body: SampleExportRequest) => normalizeJob(
      await this.request('/samples/export', { method: 'POST', body: JSON.stringify(body) }),
    ),
    analyze: async (body: SampleAnalyzeRequest) => normalizeJob(
      await this.request('/samples/analyze', { method: 'POST', body: JSON.stringify(body) }),
    ),
  };

  jobs = {
    list: async (): Promise<Page<Job>> => {
      const [workbenchResult, runnerResult] = await Promise.allSettled([
        this.request('/jobs'),
        this.request('/runner/jobs'),
      ]);
      if (workbenchResult.status === 'rejected' && runnerResult.status === 'rejected') throw workbenchResult.reason;
      const workbenchRaw = workbenchResult.status === 'fulfilled' ? map(workbenchResult.value) : {};
      const runnerRaw = runnerResult.status === 'fulfilled' ? map(runnerResult.value) : {};
      const items = [
        ...list(workbenchRaw.items ?? workbenchRaw.jobs).map((item) => normalizeJob(item)),
        ...list(runnerRaw.items ?? runnerRaw.jobs).map((item) => normalizeJob(item, 'runner')),
      ].sort((left, right) => right.created_at.localeCompare(left.created_at));
      return { items, total: items.length };
    },
    get: async (id: string): Promise<Job> => {
      if (id.startsWith('runner:')) {
        return normalizeJob(await this.request(`/runner/jobs/${encodeURIComponent(id.slice(7))}`), 'runner');
      }
      return normalizeJob(await this.request(`/jobs/${encodeURIComponent(id)}`));
    },
    cancel: async (id: string) => {
      if (id.startsWith('runner:')) {
        return normalizeJob(await this.request(`/runner/jobs/${encodeURIComponent(id.slice(7))}/cancel`, { method: 'POST' }), 'runner');
      }
      return normalizeJob(await this.request(`/jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST' }));
    },
  };

  tools = {
    trajectory: async (body: TrajectoryRequest) => normalizeJob(
      await this.request('/tools/trajectory', { method: 'POST', body: JSON.stringify(body) }),
    ),
    compareTrajectory: async (body: TrajectoryCompareRequest) => normalizeJob(
      await this.request('/tools/trajectory-compare', { method: 'POST', body: JSON.stringify(body) }),
    ),
    evaluateOutcome: async (body: OutcomeEvalRequest) => normalizeJob(
      await this.request('/tools/outcome-eval', { method: 'POST', body: JSON.stringify(body) }),
    ),
    scanRepair: async (body: RepairScanRequest): Promise<RepairPlan> =>
      await this.request('/tools/repair/scan', { method: 'POST', body: JSON.stringify(body) }) as RepairPlan,
    applyRepair: async (body: { plan: RepairPlan; confirm_path?: string; dry_run?: boolean }) => normalizeJob(
      await this.request('/tools/repair/apply', { method: 'POST', body: JSON.stringify(body) }),
    ),
    restoreRepair: async (body: { source_path: string; confirm_path: string; backup_suffix?: string; dry_run?: boolean }) => normalizeJob(
      await this.request('/tools/repair/restore', { method: 'POST', body: JSON.stringify(body) }),
    ),
  };
}

export const api = new ApiClient();
