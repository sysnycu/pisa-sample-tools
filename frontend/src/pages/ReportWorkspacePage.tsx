import { type Dispatch, type SetStateAction, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Accordion, ActionIcon, Alert, Anchor, Badge, Button, Card, Checkbox, Code, Divider, Group, NumberInput, Popover, Progress, ScrollArea, Select, SimpleGrid, Stack, Tabs, Text, TextInput, ThemeIcon } from '@mantine/core';
import {
  IconAlertTriangle, IconArrowLeft, IconCarCrash, IconCheck, IconClock, IconDatabase,
  IconDownload, IconFileAnalytics, IconFolder, IconMovie, IconPlayerPlay, IconRefresh, IconRoute, IconSearch, IconShieldCheck,
} from '@tabler/icons-react';
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api } from '../api/client';
import { useDatasets, useReportCharts, useReportSummary } from '../api/query';
import type { CaseDetail, ComparisonClass, CrossExperimentComparison, DataHealthFinding, ReportSummary, RunRecord, VisualizationSpec } from '../api/types';
import { EmptyState, InlineError, PageLoading } from '../components/Feedback';
import { MetricCard } from '../components/MetricCard';
import { PageHeader } from '../components/PageHeader';
import { StatusBadge } from '../components/StatusBadge';
import { VisualizationCard } from '../components/VisualizationCard';

const sections = [
  ['overview', 'Overview'], ['sampling', 'Sampling'], ['outcomes', 'Outcomes & safety'], ['performance', 'Performance'],
  ['compare', 'Compare'], ['sensitivity', 'Sensitivity'], ['runs', 'Runs'], ['replay', 'Run detail / replay'],
  ['media', 'Media'], ['provenance', 'Provenance & health'], ['exports', 'Exports'],
] as const;

type ReplayScalar = string | number | boolean | null;

type ReplayPoint = {
  time: number;
  x?: number;
  y?: number;
  yaw?: number;
  speed?: number | null;
  ttc?: number | null;
  throttle?: number | null;
  brake?: number | null;
  steer?: number | null;
  acceleration?: number | null;
  yaw_rate?: number | null;
  values?: Record<string, ReplayScalar>;
};

type ReplayGeometry = {
  agent_id?: string;
  entity_name?: string;
  is_ego?: boolean;
  length_m?: number | null;
  width_m?: number | null;
  height_m?: number | null;
  center_offset_x?: number | null;
  center_offset_y?: number | null;
  yaw_offset?: number | null;
  reference_point?: string;
  source?: string;
};

type ReplayEvent = {
  time: number;
  type: string;
  label: string;
  severity?: string;
  x?: number | null;
  y?: number | null;
  details?: Record<string, ReplayScalar>;
};

type ReplayChannel = { point_count?: number; fields?: string[] };

type ReplayCaseExtras = {
  traces: Record<string, ReplayPoint[]>;
  events?: ReplayEvent[];
  geometry?: ReplayGeometry[];
  trace_channels?: Record<string, ReplayChannel>;
  map?: { name?: string; polyline?: Array<[number, number]> };
  ego_goal?: { x?: number; y?: number; target_speed?: number; source_type?: string } | null;
  ego_goal_warning?: string | null;
};

type MetricKey = 'x' | 'y' | 'speed' | 'acceleration' | 'ttc' | 'distance' | 'thw' | 'drac' | 'clearance';
type ControlKey = 'throttle_command' | 'brake_command' | 'steer_command' | 'ackermann_speed_target' | 'ackermann_acceleration_target' | 'ackermann_steer_target' | 'ackermann_steering_angle' | 'ackermann_steering_angle_velocity' | 'ackermann_jerk';

const metricDefinitions: Record<MetricKey, { label: string; unit: string; exact: string[]; patterns: RegExp[] }> = {
  x: { label: 'X', unit: 'm', exact: ['x', 'position_x', 'location_x'], patterns: [/(^|_)position_x$/, /(^|_)location_x$/] },
  y: { label: 'Y', unit: 'm', exact: ['y', 'position_y', 'location_y'], patterns: [/(^|_)position_y$/, /(^|_)location_y$/] },
  speed: { label: 'Speed', unit: 'm/s', exact: ['speed', 'speed_mps', 'velocity'], patterns: [/(^|_)speed(_mps)?$/, /velocity(_mps)?$/] },
  acceleration: { label: 'Acceleration', unit: 'm/s²', exact: ['acceleration', 'acceleration_mps2', 'longitudinal_acceleration'], patterns: [/(^|_)acceleration(_mps2)?$/, /longitudinal_acceleration/] },
  ttc: { label: 'TTC', unit: 's', exact: ['ttc', 'ttc_s', 'min_ttc', 'minimum_ttc', 'time_to_collision'], patterns: [/(^|_)ttc(_s)?$/, /time_to_collision/] },
  distance: { label: 'Distance', unit: 'm', exact: ['distance', 'distance_m', 'relative_distance', 'longitudinal_distance', 'minimum_distance'], patterns: [/(relative|longitudinal|minimum|lead|object)_distance(_m)?$/, /distance_to/] },
  thw: { label: 'THW', unit: 's', exact: ['thw', 'thw_s', 'time_headway', 'time_headway_s'], patterns: [/(^|_)thw(_s)?$/, /time_headway/] },
  drac: { label: 'DRAC', unit: 'm/s²', exact: ['drac', 'drac_mps2', 'required_deceleration'], patterns: [/(^|_)drac(_mps2)?$/, /deceleration_rate_to_avoid/, /required_deceleration/] },
  clearance: { label: 'Clearance', unit: 'm', exact: ['clearance', 'clearance_m', 'minimum_clearance', 'gap_distance'], patterns: [/clearance(_m)?$/, /(^|_)gap(_distance)?(_m)?$/, /separation(_m)?$/] },
};

const stateMetricKeys: MetricKey[] = ['x', 'y', 'speed', 'acceleration', 'ttc', 'distance', 'thw', 'drac', 'clearance'];
const controlDefinitions: Record<ControlKey, { label: string; unit: string }> = {
  throttle_command: { label: 'Throttle', unit: '' }, brake_command: { label: 'Brake', unit: '' }, steer_command: { label: 'Steer', unit: '' },
  ackermann_speed_target: { label: 'Speed target', unit: 'm/s' }, ackermann_acceleration_target: { label: 'Acceleration target', unit: 'm/s²' }, ackermann_steer_target: { label: 'Steer target', unit: '' },
  ackermann_steering_angle: { label: 'Steering angle target', unit: 'rad' }, ackermann_steering_angle_velocity: { label: 'Steering angle velocity target', unit: 'rad/s' }, ackermann_jerk: { label: 'Jerk target', unit: 'm/s³' },
};

const actorPalette = ['#0057b8', '#d81b60', '#00876c', '#7e2f8e', '#c47f00', '#00a6a6', '#7a3e00', '#111827', '#84a900'];
const outcomeColors: Record<string, string> = {
  success: '#16a34a', fail: '#dc2626', failure: '#dc2626', invalid: '#2563eb', unknown: '#6b7280',
  'all success': '#16a34a', 'all fail': '#991b1b', 'all invalid': '#2563eb', 'all unknown': '#6b7280',
  'success → fail': '#6d28d9', 'fail → success': '#111827', 'success → invalid': '#ec4899',
  'invalid → success': '#84cc16', 'fail → invalid': '#06b6d4', 'invalid → fail': '#78350f',
};
// Ordered so the first categories—the common case—are widely separated in hue
// and luminance. Point shapes repeat only after the palette is exhausted.
const categoricalContrastPalette = ['#0057b8', '#d55e00', '#008450', '#7f3c8d', '#1a1a1a', '#b38f00', '#00a6a6', '#e6007e', '#6b3f00', '#6f7f00'];
const categoricalSymbols = ['circle', 'rect', 'triangle', 'diamond', 'pin', 'arrow', 'roundRect'] as const;

function actorColor(name: string): string {
  let hash = 2166136261;
  for (const character of name) hash = Math.imul(hash ^ character.charCodeAt(0), 16777619);
  return actorPalette[(hash >>> 0) % actorPalette.length];
}

function useSessionState<T>(key: string, fallback: T): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    try {
      const stored = window.sessionStorage.getItem(key);
      return stored === null ? fallback : JSON.parse(stored) as T;
    } catch {
      return fallback;
    }
  });
  useEffect(() => {
    try { window.sessionStorage.setItem(key, JSON.stringify(value)); } catch { /* Session storage can be unavailable in privacy mode. */ }
  }, [key, value]);
  return [value, setValue];
}

function finiteNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

function escapeHtml(value: unknown): string {
  return String(value ?? '—').replace(/[&<>'"]/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[character]!);
}

function scatterCategory(point: { outcome: string; color?: unknown }, selectedColor: string): string {
  return String(selectedColor === 'outcome' ? point.outcome : point.color ?? 'Missing');
}

function formatAxisTick(value: unknown): string {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  const formatted = numeric.toFixed(2).replace(/\.?(0+)$/, '');
  return formatted === '' || formatted === '-0' ? '0' : formatted;
}

function canonicalField(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
}

function metricValue(point: ReplayPoint, metric: MetricKey): number | undefined {
  const direct: Partial<Record<MetricKey, unknown>> = {
    x: point.x, y: point.y, ttc: point.ttc, speed: point.speed, acceleration: point.acceleration,
  };
  const directValue = finiteNumber(direct[metric]);
  if (directValue !== undefined) return directValue;
  const definition = metricDefinitions[metric];
  const entries = Object.entries(point.values ?? {}).map(([name, value]) => [canonicalField(name), value] as const);
  for (const exact of definition.exact) {
    const match = entries.find(([name]) => name === exact || name.endsWith(`_${exact}`));
    const value = finiteNumber(match?.[1]);
    if (value !== undefined) return value;
  }
  for (const pattern of definition.patterns) {
    const match = entries.find(([name]) => pattern.test(name));
    const value = finiteNumber(match?.[1]);
    if (value !== undefined) return value;
  }
  return undefined;
}

function controlValue(point: ReplayPoint, control: ControlKey): number | undefined {
  const values = Object.fromEntries(Object.entries(point.values ?? {}).map(([name, value]) => [canonicalField(name), value]));
  const first = (...names: string[]) => names.map((name) => finiteNumber(values[name])).find((value) => value !== undefined);
  if (control === 'throttle_command') return first('throttle', 'accelerator', 'accelerator_pedal');
  if (control === 'brake_command') return first('brake', 'brake_pedal');
  if (control === 'steer_command') return first('steer', 'steering');
  if (control === 'ackermann_speed_target') return first('speed', 'target_speed', 'speed_target');
  if (control === 'ackermann_acceleration_target') return first('acceleration', 'target_acceleration', 'acceleration_target');
  if (control === 'ackermann_steer_target') return first('steer', 'steer_target');
  if (control === 'ackermann_steering_angle') return first('steering_angle', 'target_steering_angle');
  if (control === 'ackermann_steering_angle_velocity') return first('steering_angle_velocity', 'steer_speed', 'target_steering_angle_velocity');
  return first('jerk', 'target_jerk');
}

function isAckermannControl(point: ReplayPoint): boolean {
  return String(point.values?.control_type ?? '').toLowerCase().includes('ackermann');
}

function boundedSeries(rows: Array<[number, number | null]>, limit = 1200): Array<[number, number | null]> {
  if (rows.length <= limit) return rows;
  const required = new Set<number>([0, rows.length - 1]);
  let minIndex: number | undefined;
  let maxIndex: number | undefined;
  for (let index = 0; index < rows.length; index += 1) {
    const value = rows[index][1];
    if (value !== null && (minIndex === undefined || value < (rows[minIndex][1] as number))) minIndex = index;
    if (value !== null && (maxIndex === undefined || value > (rows[maxIndex][1] as number))) maxIndex = index;
    if (index > 0 && (rows[index - 1][1] === null) !== (value === null)) {
      required.add(index - 1);
      required.add(index);
    }
  }
  if (minIndex !== undefined) required.add(minIndex);
  if (maxIndex !== undefined) required.add(maxIndex);
  if (required.size >= limit) {
    const structural = [...required].sort((left, right) => left - right);
    const selected = new Set<number>([0, structural.length - 1]);
    if (minIndex !== undefined) selected.add(structural.indexOf(minIndex));
    if (maxIndex !== undefined) selected.add(structural.indexOf(maxIndex));
    const available = Math.max(0, limit - selected.size);
    for (let slot = 1; slot <= available; slot += 1) selected.add(Math.round(slot * (structural.length - 1) / (available + 1)));
    return [...selected].sort((left, right) => left - right).slice(0, limit).map((index) => rows[structural[index]]);
  }
  const remaining = Math.max(0, limit - required.size);
  for (let slot = 1; slot <= remaining; slot += 1) required.add(Math.round(slot * (rows.length - 1) / (remaining + 1)));
  return [...required].sort((left, right) => left - right).slice(0, limit).map((index) => rows[index]);
}

type DeltaRow = [time: number, delta: number];
type DeltaSummary = {
  count: number;
  mean: number;
  mae: number;
  rmse: number;
  minimum: DeltaRow;
  maximum: DeltaRow;
  p95Absolute: { time: number; delta: number; value: number };
};

function normalizedTraceTime(value: number): number {
  return Number(value.toFixed(9));
}

function alignedDirectionalDelta(
  left: Array<[number, number | null]>,
  right: Array<[number, number | null]>,
): DeltaRow[] {
  const leftValues = new Map(left.flatMap(([time, value]) => value === null ? [] : [[normalizedTraceTime(time), value] as const]));
  const rightValues = new Map(right.flatMap(([time, value]) => value === null ? [] : [[normalizedTraceTime(time), value] as const]));
  return [...leftValues.keys()].filter((time) => rightValues.has(time)).sort((a, b) => a - b).map((time) => [time, rightValues.get(time)! - leftValues.get(time)!]);
}

function linearPercentile(values: number[], fraction: number): number | undefined {
  if (!values.length) return undefined;
  const ordered = [...values].sort((left, right) => left - right);
  const position = (ordered.length - 1) * fraction;
  const lower = Math.floor(position), upper = Math.ceil(position);
  if (lower === upper) return ordered[lower];
  const weight = position - lower;
  return ordered[lower] * (1 - weight) + ordered[upper] * weight;
}

function summarizeDelta(rows: DeltaRow[]): DeltaSummary | undefined {
  if (!rows.length) return undefined;
  const minimum = rows.reduce((best, row) => row[1] < best[1] ? row : best);
  const maximum = rows.reduce((best, row) => row[1] > best[1] ? row : best);
  const mean = rows.reduce((sum, row) => sum + row[1], 0) / rows.length;
  const mae = rows.reduce((sum, row) => sum + Math.abs(row[1]), 0) / rows.length;
  const rmse = Math.sqrt(rows.reduce((sum, row) => sum + row[1] ** 2, 0) / rows.length);
  const p95Value = linearPercentile(rows.map((row) => Math.abs(row[1])), 0.95)!;
  const p95Row = rows.reduce((best, row) => Math.abs(Math.abs(row[1]) - p95Value) < Math.abs(Math.abs(best[1]) - p95Value) ? row : best);
  return { count: rows.length, mean, mae, rmse, minimum, maximum, p95Absolute: { time: p95Row[0], delta: p95Row[1], value: p95Value } };
}

function positionAtSortedPoints(points: ReplayPoint[], time: number): [number, number] | undefined {
  const recorded = [...points].reverse().find((point) => point.time <= time);
  return recorded && recorded.x !== undefined && recorded.y !== undefined ? [recorded.x, recorded.y] : undefined;
}

function formatGeometryDimension(value: number | null | undefined): string {
  return finiteNumber(value) === undefined ? '—' : `${value!.toFixed(2)} m`;
}

function eventDetails(event: ReplayEvent): string {
  const details = Object.entries(event.details ?? {})
    .filter(([, value]) => value !== null && value !== '')
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${String(value)}`);
  if (finiteNumber(event.x) !== undefined && finiteNumber(event.y) !== undefined) details.unshift(`(${event.x!.toFixed(1)}, ${event.y!.toFixed(1)})`);
  return details.join(' · ');
}

function summaryCharts(summary: ReportSummary): VisualizationSpec[] {
  const outcomes = summary.outcomes;
  return [
    {
      id: 'overview-outcomes', title: 'Outcome composition', subtitle: 'Invalid and unknown runs remain separate from failures.', kind: 'bar',
      option: {
        animationDuration: 350, color: ['#20a486', '#ef5b5b', '#f59f00', '#8b95a5'],
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } }, grid: { top: 24, right: 24, bottom: 42, left: 64 },
        xAxis: { type: 'category', data: ['Success', 'Fail', 'Invalid', 'Unknown'], axisTick: { show: false } },
        yAxis: { type: 'value', name: 'Runs', splitLine: { lineStyle: { color: '#edf0f5' } } },
        series: [{ type: 'bar', data: [outcomes.success, outcomes.fail, outcomes.invalid, outcomes.unknown].map((value, index) => ({ value, itemStyle: { color: ['#20a486', '#ef5b5b', '#f59f00', '#8b95a5'][index], borderRadius: [5, 5, 0, 0] } })), barMaxWidth: 64 }],
      },
    },
    {
      id: 'overview-runtime', title: 'Simulation throughput', subtitle: 'Aggregate speedup uses total simulated time divided by total wall time.', kind: 'bar',
      option: summary.simulated_seconds !== undefined && summary.wall_seconds !== undefined ? {
        color: ['#526ff0'], tooltip: { trigger: 'axis' }, grid: { top: 24, right: 24, bottom: 42, left: 70 },
        xAxis: { type: 'category', data: ['Simulated', 'Wall'] }, yAxis: { type: 'value', name: 'Seconds', splitLine: { lineStyle: { color: '#edf0f5' } } },
        series: [{ type: 'bar', barMaxWidth: 72, data: [summary.simulated_seconds, summary.wall_seconds], itemStyle: { borderRadius: [5, 5, 0, 0] } }],
      } : {},
    },
  ];
}

function SelectReport() {
  const [path, setPath] = useState('');
  const browser = useQuery({ queryKey: ['report-browser-select', path], queryFn: () => api.datasets.browse(path || undefined), retry: 1 });
  const reports = useDatasets('', path || browser.data?.path, false);
  const navigate = useNavigate();
  return (
    <>
      <PageHeader eyebrow="Evidence explorer" title="Choose a report workspace" description="Open an indexed report to explore sampling, outcomes, concrete runs, comparisons, and publication exports." />
      <Card p="md" mb="lg"><Group align="flex-end" wrap="nowrap"><Button variant="default" leftSection={<IconArrowLeft size={15} />} disabled={!browser.data?.parent} onClick={() => browser.data?.parent && setPath(browser.data.parent)}>Up</Button><TextInput label="Current report directory" value={path || browser.data?.path || ''} onChange={(event) => setPath(event.currentTarget.value)} leftSection={<IconFolder size={15} />} style={{ flex: 1 }} /><Button variant="default" loading={browser.isFetching} onClick={() => browser.refetch()}>Open</Button></Group>{browser.data && <ScrollArea mt="md" type="auto"><Group wrap="nowrap" gap="xs">{browser.data.entries.filter((entry) => entry.kind === 'directory').map((entry) => <Button key={entry.path} variant="subtle" leftSection={<IconFolder size={14} />} onClick={() => setPath(entry.path)}>{entry.name}</Button>)}</Group></ScrollArea>}</Card>
      {reports.isLoading ? <PageLoading label="Loading report library…" /> : reports.error ? <InlineError error={reports.error} onRetry={() => reports.refetch()} /> : !reports.data?.items.length ? (
        <Card><EmptyState title="No report is ready" description="Build a report from the Dashboard first. It will appear here as soon as indexing completes." action={<Button component={Link} to="/">Open Dashboard</Button>} /></Card>
      ) : (
        <SimpleGrid cols={{ base: 1, md: 2, xl: 3 }}>{reports.data.items.map((report) => <Card key={report.id} p="lg"><Group justify="space-between"><ThemeIcon variant="light" size={40}><IconFileAnalytics size={20} /></ThemeIcon><StatusBadge value={report.status} /></Group><Text fw={650} mt="md">{report.name}</Text><Text size="xs" c="dimmed" className="pisa-code" lineClamp={1}>{report.path}</Text><Group gap="xs" my="md"><Badge variant="light" color="gray">{report.experiment_count} experiments</Badge><Badge variant="light" color="gray">{report.run_count.toLocaleString()} runs</Badge></Group><Button fullWidth variant="light" onClick={() => navigate(`/reports/${encodeURIComponent(report.id)}/overview`)}>Open workspace</Button></Card>)}</SimpleGrid>
      )}
    </>
  );
}

function Overview({ datasetId }: { datasetId: string }) {
  const summary = useReportSummary(datasetId);
  const charts = useReportCharts(datasetId, 'overview');
  if (summary.isLoading) return <PageLoading label="Loading report overview…" />;
  if (summary.error) return <InlineError error={summary.error} onRetry={() => summary.refetch()} />;
  if (!summary.data) return <EmptyState title="Overview is not ready" description="The report index may still be building. Check Jobs for progress." />;
  const data = summary.data;
  const speedup = data.simulated_seconds && data.wall_seconds ? data.simulated_seconds / data.wall_seconds : undefined;
  const generatedCharts = charts.data?.length ? charts.data : undefined;
  const visualizations = generatedCharts ?? summaryCharts(data);
  const healthErrors = data.health?.filter((finding) => finding.severity === 'error') ?? [];
  const healthWarnings = data.health?.filter((finding) => finding.severity !== 'error') ?? [];
  return (
    <Stack gap="lg">
      <div className="pisa-page-grid">
        <MetricCard label="Runs" value={data.run_count.toLocaleString()} detail={`${data.experiment_count} experiments`} icon={<IconDatabase size={20} />} />
        <MetricCard label="Success" value={data.outcomes.success.toLocaleString()} detail={`${(100 * data.outcomes.success / Math.max(1, data.run_count)).toFixed(1)}% of all runs`} icon={<IconCheck size={20} />} color="teal" />
        <MetricCard label="Collisions" value={(data.collision_count ?? 0).toLocaleString()} detail="Recorded collision events" icon={<IconCarCrash size={20} />} color="red" />
        <MetricCard label="Aggregate speedup" value={speedup ? `${speedup.toFixed(1)}×` : '—'} detail="Σ simulated / Σ wall" icon={<IconClock size={20} />} color="cyan" />
      </div>
      {healthErrors.length > 0 && <Alert color="red" icon={<IconAlertTriangle size={18} />} title={`${healthErrors.length} blocking data-health error${healthErrors.length === 1 ? '' : 's'}`}><Stack gap="xs">{healthErrors.map((finding) => <div key={finding.id}><Text fw={650} size="sm">{finding.title}</Text><Text size="sm">{finding.detail}</Text>{finding.dataset_id && <Text size="xs" className="pisa-code">Experiment: {finding.dataset_id}</Text>}</div>)}</Stack></Alert>}
      {healthWarnings.length > 0 && <Alert color="yellow" icon={<IconAlertTriangle size={18} />} title={`${healthWarnings.length} non-blocking data-health finding${healthWarnings.length === 1 ? '' : 's'}`}>Warnings remain visible in Provenance & health and do not replace blocking errors shown above.</Alert>}
      <Card p={0}><Group p="md" justify="space-between"><div><Text fw={650}>Experiment totals</Text><Text size="xs" c="dimmed">Direct numeric summary before visualization.</Text></div><Badge variant="light">{data.experiment_summaries?.length ?? 0} experiments</Badge></Group><ScrollArea><table className="pisa-data-table"><thead><tr><th>Experiment</th><th>Simulator / AV</th><th>Total samples</th><th>Success</th><th>Fail</th><th>Invalid</th><th>Unknown</th><th>Avg time</th><th>Avg speedup</th></tr></thead><tbody>{data.experiment_summaries?.map((item) => <tr key={item.experiment}><td><Text fw={600} size="sm">{item.experiment}</Text><Text size="xs" c="dimmed">{item.sampler ?? 'sampler unknown'}</Text></td><td>{item.simulator ?? '—'} / {item.av ?? '—'}</td><td>{item.total_samples.toLocaleString()}</td><td>{item.success.toLocaleString()}</td><td>{item.fail.toLocaleString()}</td><td>{item.invalid.toLocaleString()}</td><td>{item.unknown.toLocaleString()}</td><td>{item.avg_time_seconds == null ? '—' : `${item.avg_time_seconds.toFixed(3)} s`}</td><td>{item.avg_speedup == null ? '—' : `${item.avg_speedup.toFixed(2)}×`}</td></tr>)}</tbody></table></ScrollArea></Card>
      <SimpleGrid cols={{ base: 1, lg: 2 }}>{visualizations.map((chart) => <VisualizationCard key={chart.id} spec={chart} datasetId={generatedCharts ? datasetId : undefined} />)}</SimpleGrid>
    </Stack>
  );
}

function ScatterExplorer({ datasetId, onOpen }: { datasetId: string; onOpen: (id: string, experiments?: string[]) => void }) {
  const storageKey = `pisa:sampling:${datasetId}`;
  const [mode, setMode] = useSessionState<string | null>(`${storageKey}:mode`, 'single');
  const [x, setX] = useSessionState<string | null>(`${storageKey}:x`, null);
  const [y, setY] = useSessionState<string | null>(`${storageKey}:y`, null);
  const [color, setColor] = useSessionState<string | null>(`${storageKey}:color`, 'outcome');
  const [colorSource, setColorSource] = useSessionState<string | null>(`${storageKey}:color-source`, 'outcome');
  const [dataset, setDataset] = useSessionState<string | null>(`${storageKey}:dataset`, null);
  const [leftDataset, setLeftDataset] = useSessionState<string | null>(`${storageKey}:left`, null);
  const [rightDataset, setRightDataset] = useSessionState<string | null>(`${storageKey}:right`, null);
  const [aspect, setAspect] = useSessionState<string | null>(`${storageKey}:aspect`, '1 / 1');
  const [visibleCount, setVisibleCount] = useSessionState<number | undefined>(`${storageKey}:visible-count`, undefined);
  const [playing, setPlaying] = useState(false);
  const [pointsPerSecond, setPointsPerSecond] = useSessionState<number | string>(`${storageKey}:points-per-second`, 20);
  const [durationSeconds, setDurationSeconds] = useSessionState<number | string>(`${storageKey}:duration`, 5);
  const [durationMode, setDurationMode] = useSessionState<string | null>(`${storageKey}:duration-mode`, 'duration');
  const [exportAxes, setExportAxes] = useSessionState(`${storageKey}:axes`, true);
  const [exportGrid, setExportGrid] = useSessionState(`${storageKey}:grid`, false);
  const [distinctShapes, setDistinctShapes] = useSessionState(`${storageKey}:distinct-shapes-v2`, false);
  const [pointSize, setPointSize] = useSessionState<number | string>(`${storageKey}:point-size`, 10);
  const [exportFormat, setExportFormat] = useSessionState<string | null>(`${storageKey}:format`, 'gif');
  const [exportError, setExportError] = useState<string>();
  const [exporting, setExporting] = useState(false);
  const scatter = useQuery({
    queryKey: ['scatter-explorer', datasetId, x, y, color],
    queryFn: () => api.datasets.scatter(datasetId, { x: x ?? undefined, y: y ?? undefined, color: color ?? 'outcome' }),
    retry: 1,
  });
  useEffect(() => {
    if (!scatter.data) return;
    if (!x) setX(scatter.data.selection.x);
    if (!y) setY(scatter.data.selection.y);
    const datasets = scatter.data.datasets;
    if (!dataset || !datasets.includes(dataset)) setDataset(datasets[0] ?? null);
    if (!leftDataset || !datasets.includes(leftDataset)) setLeftDataset(datasets[0] ?? null);
    if (!rightDataset || !datasets.includes(rightDataset) || rightDataset === (leftDataset ?? datasets[0])) setRightDataset(datasets.find((value) => value !== (leftDataset ?? datasets[0])) ?? null);
  }, [dataset, leftDataset, rightDataset, scatter.data, x, y]);
  const axisFields = useMemo(() => (scatter.data?.fields ?? []).filter((field) => field.source !== 'outcome' && field.source !== 'run'), [scatter.data?.fields]);
  const fieldSource = (key: string) => axisFields.find((field) => field.key === key)?.source ?? 'order';
  const [xSource, setXSource] = useSessionState<string | null>(`${storageKey}:x-source`, 'order');
  const [ySource, setYSource] = useSessionState<string | null>(`${storageKey}:y-source`, 'order');
  useEffect(() => { if (x) setXSource(fieldSource(x)); if (y) setYSource(fieldSource(y)); }, [x, y, axisFields]);
  const sourceOptions = [...new Set(axisFields.map((field) => field.source))].map((source) => ({ value: source, label: source === 'order' ? 'Recorded order' : source === 'parameter' ? 'Parameters' : source === 'metric' ? 'Metrics' : source === 'control' ? 'Control' : source }));
  const xFieldOptions = axisFields.filter((field) => field.source === xSource).map((field) => ({ value: field.key, label: field.label }));
  const yFieldOptions = axisFields.filter((field) => field.source === ySource).map((field) => ({ value: field.key, label: field.label }));
  const colorFields = scatter.data?.fields ?? [];
  const colorSourceOptions = [...new Set(colorFields.map((field) => field.source))].map((source) => ({ value: source, label: source === 'outcome' ? 'Outcome' : source === 'run' ? 'Run result' : source === 'order' ? 'Recorded order' : source === 'parameter' ? 'Parameters' : source === 'metric' ? 'Metrics' : source === 'control' ? 'Control' : source }));
  const colorFieldOptions = colorFields.filter((field) => field.source === colorSource).map((field) => ({ value: field.key, label: field.label }));
  useEffect(() => {
    const source = colorFields.find((field) => field.key === color)?.source;
    if (source && source !== colorSource) setColorSource(source);
  }, [color, colorFields, colorSource, setColorSource]);
  const selectedX = x ?? scatter.data?.selection.x ?? 'sample_order';
  const selectedY = y ?? scatter.data?.selection.y ?? 'scenario_order';
  const selectedColor = color ?? 'outcome';
  const rawPoints = scatter.data?.points ?? [];
  const pairedPoints = useMemo(() => {
    if (mode !== 'compare' || !leftDataset || !rightDataset) return [];
    const key = (point: (typeof rawPoints)[number]) => point.parameter_hash || point.sample_id || point.scenario_id;
    const left = new Map(rawPoints.filter((point) => point.dataset_id === leftDataset).map((point) => [key(point), point]));
    return rawPoints.filter((point) => point.dataset_id === rightDataset).flatMap((right) => { const leftPoint = left.get(key(right)); if (!leftPoint) return []; const leftValue = Number(leftPoint.color), rightValue = Number(right.color), numeric = Number.isFinite(leftValue) && Number.isFinite(rightValue) && !['outcome', 'stop_condition', 'stop_reason'].includes(selectedColor); const transition = leftPoint.outcome === right.outcome ? `All ${leftPoint.outcome}` : `${leftPoint.outcome} → ${right.outcome}`; const categoricalColor = selectedColor === 'stop_reason' ? (leftPoint.stop_reason === right.stop_reason ? `Same · ${leftPoint.stop_reason ?? 'Missing'}` : `${leftPoint.stop_reason ?? 'Missing'} → ${right.stop_reason ?? 'Missing'}`) : selectedColor === 'stop_condition' ? (leftPoint.stop_condition === right.stop_condition ? `Same · ${leftPoint.stop_condition ?? 'Missing'}` : `${leftPoint.stop_condition ?? 'Missing'} → ${right.stop_condition ?? 'Missing'}`) : transition; return [{ ...leftPoint, run_id: leftPoint.run_id, left_run_id: leftPoint.run_id, right_run_id: right.run_id, left_outcome: leftPoint.outcome, right_outcome: right.outcome, left_stop_condition: leftPoint.stop_condition, right_stop_condition: right.stop_condition, left_stop_reason: leftPoint.stop_reason, right_stop_reason: right.stop_reason, left_value: numeric ? leftValue : undefined, right_value: numeric ? rightValue : undefined, dataset_id: `${leftDataset} vs ${rightDataset}`, outcome: numeric ? 'delta' : transition, color: numeric ? rightValue - leftValue : categoricalColor }]; });
  }, [leftDataset, mode, rawPoints, rightDataset, selectedColor]);
  const colorDomainPoints = mode === 'compare' ? pairedPoints : rawPoints.filter((point) => !dataset || point.dataset_id === dataset);
  const allPoints = colorDomainPoints;
  const categoryStyles = useMemo(() => Object.fromEntries(
    [...new Set(colorDomainPoints.map((point) => scatterCategory(point, selectedColor)))].sort().map((name, index) => [name, {
      color: outcomeColors[name.toLowerCase()] ?? categoricalContrastPalette[index % categoricalContrastPalette.length],
      symbol: categoricalSymbols[index % categoricalSymbols.length],
    }]),
  ), [colorDomainPoints, selectedColor]);
  const shownCount = Math.max(0, Math.min(allPoints.length, visibleCount ?? allPoints.length));
  const shownPoints = allPoints.slice(0, shownCount);
  useEffect(() => {
    if (!playing || !allPoints.length) return;
    const rate = durationMode === 'duration' ? allPoints.length / Math.max(0.1, Number(durationSeconds) || 10) : Math.max(0.1, Number(pointsPerSecond) || 20);
    const started = performance.now();
    const initial = shownCount >= allPoints.length ? 0 : shownCount;
    setVisibleCount(initial);
    const timer = window.setInterval(() => {
      const count = Math.min(allPoints.length, initial + Math.floor((performance.now() - started) / 1000 * rate));
      setVisibleCount(count);
      if (count >= allPoints.length) { window.clearInterval(timer); setPlaying(false); }
    }, 33);
    return () => window.clearInterval(timer);
  }, [allPoints.length, durationMode, durationSeconds, playing, pointsPerSecond]);
  const spec = useMemo<VisualizationSpec>(() => {
    const points = shownPoints;
    const colorField = scatter.data?.fields.find((field) => field.key === selectedColor);
    const numericColor = colorField?.source === 'metric' || colorField?.source === 'control' || colorField?.source === 'parameter' || selectedColor === 'collision';
    const series: Array<Record<string, unknown>> = [];
    const datum = (point: (typeof points)[number], value: unknown[]) => ({
      value, run_id: point.run_id, ordinal: point.ordinal, dataset_id: point.dataset_id,
      scenario_id: point.scenario_id, sample_id: point.sample_id, outcome: point.outcome,
      stop_condition: point.stop_condition, stop_reason: point.stop_reason,
      left_stop_condition: 'left_stop_condition' in point ? point.left_stop_condition : undefined,
      right_stop_condition: 'right_stop_condition' in point ? point.right_stop_condition : undefined,
      left_stop_reason: 'left_stop_reason' in point ? point.left_stop_reason : undefined,
      right_stop_reason: 'right_stop_reason' in point ? point.right_stop_reason : undefined,
    });
    if (numericColor) {
      series.push({
        type: 'scatter', name: colorField?.label ?? selectedColor, symbolSize: Math.max(3, Number(pointSize) || 10), z: 3,
        data: points.map((point) => datum(point, [point.x, point.y, Number(point.color ?? point.collision)])),
      });
    } else {
      const groups = new Map<string, typeof points>();
      for (const point of points) {
        const key = scatterCategory(point, selectedColor);
        groups.set(key, [...(groups.get(key) ?? []), point]);
      }
      for (const [name, values] of groups) series.push({ type: 'scatter', name, symbol: distinctShapes ? (categoryStyles[name]?.symbol ?? 'circle') : 'circle', symbolSize: Math.max(3, Number(pointSize) || 10), z: 3, itemStyle: { color: categoryStyles[name]?.color ?? '#6b7280', borderColor: '#17202a', borderWidth: 1 }, data: values.map((point) => datum(point, [point.x, point.y])) });
    }
    const numericValues = allPoints.map((point) => Number(point.color ?? point.collision)).filter(Number.isFinite);
    const deltaExtent = mode === 'compare' && numericValues.length ? Math.max(...numericValues.map(Math.abs), 1e-9) : undefined;
    const allX = allPoints.map((point) => point.x), allY = allPoints.map((point) => point.y);
    const xPadding = allX.length ? Math.max(1e-9, Math.max(...allX) - Math.min(...allX)) * 0.04 : 0;
    const yPadding = allY.length ? Math.max(1e-9, Math.max(...allY) - Math.min(...allY)) * 0.04 : 0;
    return {
      id: `scatter-explorer-${selectedX}-${selectedY}`, title: 'Scatter explorer',
      subtitle: `${points.length.toLocaleString()} / ${allPoints.length.toLocaleString()} ${mode === 'compare' ? 'paired samples' : 'concrete samples'} · axes stay fixed to the final filtered extent.`, kind: 'scatter',
      option: points.length ? {
        animation: false, legend: numericColor ? undefined : { type: 'scroll', top: 0 }, tooltip: { trigger: 'item', appendTo: 'body', confine: false, className: 'pisa-scatter-tooltip', formatter: (params: { data?: Record<string, unknown> }) => { const item = params.data ?? {}; const value = Array.isArray(item.value) ? item.value : []; const paired = item.left_stop_reason !== undefined || item.right_stop_reason !== undefined; return `Sample ${escapeHtml(item.ordinal)}<br/>${escapeHtml(selectedX)}: ${escapeHtml(value[0])}<br/>${escapeHtml(selectedY)}: ${escapeHtml(value[1])}<br/>Outcome: ${escapeHtml(item.outcome)}<br/>${paired ? `Left stop: ${escapeHtml(item.left_stop_condition)} · ${escapeHtml(item.left_stop_reason)}<br/>Right stop: ${escapeHtml(item.right_stop_condition)} · ${escapeHtml(item.right_stop_reason)}<br/>` : `Stop condition: ${escapeHtml(item.stop_condition)}<br/>Stop reason: ${escapeHtml(item.stop_reason)}<br/>`}Run: ${escapeHtml(item.run_id)}`; } },
        grid: { top: 54, right: numericColor ? 90 : 36, bottom: 76, left: 76 },
        xAxis: { type: 'value', show: exportAxes, name: selectedX, nameLocation: 'middle', nameGap: 42, scale: true, min: allX.length ? Math.min(...allX) - xPadding : undefined, max: allX.length ? Math.max(...allX) + xPadding : undefined, axisLabel: { formatter: formatAxisTick }, splitLine: { show: exportGrid } }, yAxis: { type: 'value', show: exportAxes, name: selectedY, nameLocation: 'middle', nameGap: 54, scale: true, min: allY.length ? Math.min(...allY) - yPadding : undefined, max: allY.length ? Math.max(...allY) + yPadding : undefined, axisLabel: { formatter: formatAxisTick }, splitLine: { show: exportGrid } },
        visualMap: numericColor && numericValues.length ? { min: deltaExtent ? -deltaExtent : Math.min(...numericValues), max: deltaExtent ?? Math.max(...numericValues), dimension: 2, seriesIndex: 0, right: 8, top: 65, calculable: true, inRange: { color: deltaExtent ? ['#00a6a6', '#f8fafc', '#7e2f8e'] : ['#dce6ff', '#526ff0', '#c92a2a'] } } : undefined,
        series,
      } : {},
    };
  }, [allPoints, categoryStyles, distinctShapes, exportAxes, exportGrid, mode, pointSize, scatter.data, selectedColor, selectedX, selectedY, shownPoints]);
  const handlePoint = useCallback((value: unknown) => {
    if (value && typeof value === 'object' && 'run_id' in value && typeof value.run_id === 'string') onOpen(value.run_id, mode === 'compare' ? [leftDataset, rightDataset].filter((item): item is string => Boolean(item)) : 'dataset_id' in value && typeof value.dataset_id === 'string' ? [value.dataset_id] : undefined);
  }, [leftDataset, mode, onOpen, rightDataset]);
  const representatives = useMemo(() => {
    if (!allPoints.length) return [];
    const selected: Array<{ label: string; reason: string; point: (typeof allPoints)[number] }> = [];
    if (mode === 'compare') {
      if (selectedColor === 'outcome') {
        const disagreements = allPoints.filter((point) => point.outcome.includes('→'));
        for (const transition of [...new Set(disagreements.map((point) => point.outcome))]) {
          const point = disagreements.find((item) => item.outcome === transition);
          if (point) selected.push({ label: transition, reason: `Representative paired sample with different outcomes in ${leftDataset} and ${rightDataset}`, point });
        }
        return selected.slice(0, 9);
      }
      if (selectedColor === 'stop_reason' || selectedColor === 'stop_condition') {
        const disagreements = allPoints.filter((point) => String(point.color ?? '').includes('→'));
        for (const transition of [...new Set(disagreements.map((point) => String(point.color)))]) {
          const point = disagreements.find((item) => item.color === transition);
          if (point) selected.push({ label: transition, reason: `Paired ${selectedColor === 'stop_reason' ? 'stop reason' : 'stop condition'} disagreement`, point });
        }
        return selected.slice(0, 9);
      }
      const metricLabel = scatter.data?.fields.find((field) => field.key === selectedColor)?.label ?? selectedColor;
      return [...allPoints]
        .filter((point) => Number.isFinite(Number(point.color)))
        .sort((left, right) => Math.abs(Number(right.color)) - Math.abs(Number(left.color)))
        .slice(0, 9)
        .map((point, index) => ({ label: `${metricLabel} disagreement #${index + 1}`, reason: `Absolute paired delta ${Math.abs(Number(point.color)).toPrecision(5)} (${rightDataset} − ${leftDataset})`, point }));
    }
    for (const outcome of ['success', 'fail', 'invalid']) {
      const point = allPoints.find((item) => item.outcome.toLowerCase() === outcome);
      if (point) selected.push({ label: `${outcome} example`, reason: `Earliest recorded ${outcome} sample`, point });
    }
    const xs = allPoints.map((point) => point.x), ys = allPoints.map((point) => point.y);
    const xScale = Math.max(1e-9, Math.max(...xs) - Math.min(...xs)), yScale = Math.max(1e-9, Math.max(...ys) - Math.min(...ys));
    const safe = allPoints.filter((point) => point.outcome.toLowerCase() === 'success'), failures = allPoints.filter((point) => point.outcome.toLowerCase() === 'fail');
    const boundaryPairs = safe.flatMap((safePoint) => failures.map((failurePoint) => ({ safePoint, failurePoint, distance: Math.hypot((safePoint.x - failurePoint.x) / xScale, (safePoint.y - failurePoint.y) / yScale) }))).sort((left, right) => left.distance - right.distance);
    if (boundaryPairs[0]) {
      selected.push({ label: 'near critical', reason: 'Safe sample nearest to a recorded failure in normalized parameter space', point: boundaryPairs[0].safePoint });
      selected.push({ label: 'boundary safe', reason: 'Safe side of the nearest observed success/failure boundary', point: boundaryPairs[0].safePoint });
      selected.push({ label: 'boundary failure', reason: 'Failure side of the nearest observed success/failure boundary', point: boundaryPairs[0].failurePoint });
    }
    const centerX = (Math.min(...xs) + Math.max(...xs)) / 2, centerY = (Math.min(...ys) + Math.max(...ys)) / 2;
    const center = [...allPoints].sort((left, right) => Math.hypot(left.x - centerX, left.y - centerY) - Math.hypot(right.x - centerX, right.y - centerY))[0];
    if (center && !selected.some((item) => item.point.run_id === center.run_id)) selected.push({ label: 'central case', reason: 'Nearest sample to the visible parameter-space center', point: center });
    for (const [label, point] of [['minimum X', allPoints.reduce((best, item) => item.x < best.x ? item : best)], ['maximum X', allPoints.reduce((best, item) => item.x > best.x ? item : best)]] as const) if (!selected.some((item) => item.point.run_id === point.run_id)) selected.push({ label, reason: `${selectedX} boundary case`, point });
    return selected.slice(0, 9);
  }, [allPoints, leftDataset, mode, rightDataset, scatter.data?.fields, selectedColor, selectedX]);
  async function exportAnimation() {
    const targetPoints = allPoints.slice(0, shownCount);
    if (!targetPoints.length || exporting) return;
    setExporting(true);
    setExportError(undefined);
    try {
      const ratio = aspect === '16 / 9' ? 16 / 9 : aspect === '4 / 3' ? 4 / 3 : 1;
      const width = 1200, height = Math.round(width / ratio), canvas = document.createElement('canvas');
      canvas.width = width; canvas.height = height;
      const context = canvas.getContext('2d')!;
      const stream = canvas.captureStream(30);
      const recorder = new MediaRecorder(stream, { mimeType: MediaRecorder.isTypeSupported('video/webm;codecs=vp9') ? 'video/webm;codecs=vp9' : 'video/webm' });
      const chunks: Blob[] = []; recorder.ondataavailable = (event) => event.data.size && chunks.push(event.data);
      const duration = durationMode === 'duration' ? Math.max(0.25, Number(durationSeconds) || 5) : targetPoints.length / Math.max(0.1, Number(pointsPerSecond) || 20);
      const xs = allPoints.map((point) => point.x), ys = allPoints.map((point) => point.y), pad = exportAxes ? 88 : 24;
      const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
      const sx = (value: number) => pad + (value - minX) / Math.max(1e-9, maxX - minX) * (width - 2 * pad);
      const sy = (value: number) => height - pad - (value - minY) / Math.max(1e-9, maxY - minY) * (height - 2 * pad);
      const drawFrame = (count: number) => {
        context.fillStyle = '#fff'; context.fillRect(0, 0, width, height);
        if (exportGrid) { context.strokeStyle = '#e2e8f0'; context.lineWidth = 1; for (let index = 1; index < 6; index += 1) { const px = pad + index * (width - 2 * pad) / 6, py = pad + index * (height - 2 * pad) / 6; context.beginPath(); context.moveTo(px, pad); context.lineTo(px, height - pad); context.moveTo(pad, py); context.lineTo(width - pad, py); context.stroke(); } }
        if (exportAxes) { context.strokeStyle = '#17202a'; context.lineWidth = 2; context.beginPath(); context.moveTo(pad, pad); context.lineTo(pad, height - pad); context.lineTo(width - pad, height - pad); context.stroke(); context.fillStyle = '#17202a'; context.font = '24px Arial'; context.textAlign = 'center'; context.fillText(selectedX, width / 2, height - 25); context.save(); context.translate(28, height / 2); context.rotate(-Math.PI / 2); context.fillText(selectedY, 0, 0); context.restore(); }
        for (const point of targetPoints.slice(0, count)) {
          const px = sx(point.x), py = sy(point.y), style = categoryStyles[scatterCategory(point, selectedColor)] ?? { color: '#6b7280', symbol: 'circle' };
          const symbol = distinctShapes ? style.symbol : 'circle';
          const radius = Math.max(1.5, (Number(pointSize) || 10) / 2);
          context.beginPath();
          if (symbol === 'rect' || symbol === 'roundRect') context.rect(px - radius, py - radius, radius * 2, radius * 2);
          else if (symbol === 'triangle' || symbol === 'arrow') { context.moveTo(px, py - radius * 1.2); context.lineTo(px + radius * 1.2, py + radius); context.lineTo(px - radius * 1.2, py + radius); context.closePath(); }
          else if (symbol === 'diamond') { context.moveTo(px, py - radius * 1.35); context.lineTo(px + radius * 1.2, py); context.lineTo(px, py + radius * 1.35); context.lineTo(px - radius * 1.2, py); context.closePath(); }
          else if (symbol === 'pin') { context.arc(px, py - radius * 0.4, radius, 0, Math.PI * 2); context.moveTo(px - radius * 0.6, py + radius * 0.4); context.lineTo(px, py + radius * 1.6); context.lineTo(px + radius * 0.6, py + radius * 0.4); }
          else context.arc(px, py, radius, 0, Math.PI * 2);
          context.fillStyle = style.color; context.fill(); context.strokeStyle = '#17202a'; context.lineWidth = 1; context.stroke();
        }
      };
      if (exportFormat === 'png') { drawFrame(targetPoints.length); const blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob((value) => value ? resolve(value) : reject(new Error('PNG encoding failed')), 'image/png')); const url = URL.createObjectURL(blob); const anchor = document.createElement('a'); anchor.href = url; anchor.download = `sampling-${selectedX}-${selectedY}.png`; anchor.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000); return; }
      recorder.start(); const started = performance.now();
      await new Promise<void>((resolve) => { const draw = () => { const elapsed = (performance.now() - started) / 1000; const count = Math.min(targetPoints.length, Math.floor(elapsed / duration * targetPoints.length)); drawFrame(count); if (elapsed >= duration) resolve(); else requestAnimationFrame(draw); }; draw(); });
      const stopped = new Promise<void>((resolve) => { recorder.onstop = () => resolve(); }); recorder.stop(); await stopped;
      let blob = new Blob(chunks, { type: 'video/webm' });
      const format = exportFormat ?? 'gif';
      if (format !== 'webm') { const response = await fetch(`/api/v1/tools/animation/transcode?format=${encodeURIComponent(format)}`, { method: 'POST', headers: { 'Content-Type': 'video/webm' }, body: blob }); if (!response.ok) { const failure = await response.json().catch(() => ({})) as { message?: string }; throw new Error(failure.message ?? `Unable to create ${format.toUpperCase()}`); } blob = await response.blob(); }
      const url = URL.createObjectURL(blob); const anchor = document.createElement('a'); anchor.href = url; anchor.download = `sampling-${selectedX}-${selectedY}.${format}`; anchor.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { setExportError(error instanceof Error ? error.message : 'Animation export failed.'); } finally { setExporting(false); }
  }
  return <Stack gap="md">
    <Card p="lg"><SimpleGrid cols={{ base: 1, sm: 2, lg: mode === 'single' ? 5 : 6 }} verticalSpacing="sm">
      <Select label="Mode" value={mode} onChange={setMode} allowDeselect={false} data={[{ value: 'single', label: 'Single experiment' }, { value: 'compare', label: 'Compare paired experiments' }]} />
      <Group wrap="nowrap" style={{ gridColumn: 'span 2' }}><Select label="Color type" data={colorSourceOptions} value={colorSource} onChange={(value) => { setColorSource(value); setColor(colorFields.find((field) => field.source === value)?.key ?? 'outcome'); }} allowDeselect={false} w="32%" /><Select label="Color field" searchable data={colorFieldOptions} value={selectedColor} onChange={setColor} allowDeselect={false} style={{ flex: 1, minWidth: 0 }} /></Group>
      {mode === 'single' ? <Select label="Experiment" data={(scatter.data?.datasets ?? []).map((value) => ({ value, label: value }))} value={dataset} onChange={setDataset} allowDeselect={false} /> : <><Select label="Left experiment" data={(scatter.data?.datasets ?? []).map((value) => ({ value, label: value, disabled: value === rightDataset }))} value={leftDataset} onChange={setLeftDataset} allowDeselect={false} /><Select label="Right experiment" data={(scatter.data?.datasets ?? []).map((value) => ({ value, label: value, disabled: value === leftDataset }))} value={rightDataset} onChange={setRightDataset} allowDeselect={false} /></>}
      <Select label="Plot ratio" value={aspect} onChange={setAspect} allowDeselect={false} data={[{ value: 'fit', label: 'Fit window' }, { value: '1 / 1', label: '1:1 · square' }, { value: '4 / 3', label: '4:3' }, { value: '16 / 9', label: '16:9' }]} />
    </SimpleGrid><SimpleGrid cols={{ base: 1, md: 2 }} mt="md"><Card withBorder p="sm"><Text fw={600} size="sm" mb="xs">X axis</Text><Group wrap="nowrap"><Select label="Type" data={sourceOptions} value={xSource} onChange={(value) => { setXSource(value); setX(axisFields.find((field) => field.source === value)?.key ?? null); }} allowDeselect={false} w="32%" /><Select label="Field" searchable data={xFieldOptions} value={selectedX} onChange={setX} allowDeselect={false} style={{ flex: 1, minWidth: 0 }} /></Group></Card><Card withBorder p="sm"><Text fw={600} size="sm" mb="xs">Y axis</Text><Group wrap="nowrap"><Select label="Type" data={sourceOptions} value={ySource} onChange={(value) => { setYSource(value); setY(axisFields.find((field) => field.source === value)?.key ?? null); }} allowDeselect={false} w="32%" /><Select label="Field" searchable data={yFieldOptions} value={selectedY} onChange={setY} allowDeselect={false} style={{ flex: 1, minWidth: 0 }} /></Group></Card></SimpleGrid><Divider my="md" label="Visible sample prefix" /><Group justify="space-between" mb="xs"><Text size="sm" fw={600}>First {shownCount.toLocaleString()} samples</Text><Text size="xs" c="dimmed">{allPoints.length.toLocaleString()} total · fixed final axes</Text></Group><input className="pisa-horizontal-range pisa-sample-count-range" type="range" aria-label="Visible sample count" value={shownCount} onChange={(event) => { setPlaying(false); setVisibleCount(Number(event.currentTarget.value)); }} min={0} max={Math.max(1, allPoints.length)} step={1} /><Group justify="space-between" align="flex-end" mt="md" wrap="wrap"><Group><Button size="sm" variant={playing ? 'filled' : 'light'} onClick={() => setPlaying((value) => !value)} leftSection={<IconPlayerPlay size={16} />}>{playing ? 'Pause sequence' : 'Play sample sequence'}</Button><Button size="sm" variant="default" onClick={() => { setPlaying(false); setVisibleCount(allPoints.length); }}>Show all</Button></Group><Group justify="flex-end" align="flex-end" wrap="wrap"><Checkbox label="Axes" checked={exportAxes} onChange={(event) => setExportAxes(event.currentTarget.checked)} /><Checkbox label="Grid" checked={exportGrid} onChange={(event) => setExportGrid(event.currentTarget.checked)} /><Checkbox label="Distinct shapes" checked={distinctShapes} onChange={(event) => setDistinctShapes(event.currentTarget.checked)} /><NumberInput label="Point size" value={pointSize} onChange={setPointSize} min={3} max={30} step={1} clampBehavior="strict" w={96} /><Select label="Format" value={exportFormat} onChange={setExportFormat} allowDeselect={false} data={[{ value: 'png', label: 'PNG · current frame' }, { value: 'gif', label: 'GIF animation' }, { value: 'mp4', label: 'MP4 animation' }, { value: 'webm', label: 'WebM animation' }]} />{exportFormat !== 'png' && <Select label="Timing" value={durationMode} onChange={setDurationMode} allowDeselect={false} data={[{ value: 'duration', label: 'Total duration' }, { value: 'rate', label: 'Points per second' }]} />}{exportFormat !== 'png' && (durationMode === 'rate' ? <NumberInput label="Points / second" value={pointsPerSecond} onChange={setPointsPerSecond} min={0.1} /> : <NumberInput label="Total seconds" value={durationSeconds} onChange={setDurationSeconds} min={0.25} />)}<Button loading={exporting} onClick={() => void exportAnimation()} leftSection={<IconDownload size={16} />}>Export first {shownCount}</Button></Group></Group>{exportError && <Text role="alert" c="red" size="xs" mt="sm">{exportError}</Text>}</Card>
    {scatter.isLoading ? <PageLoading label="Loading concrete sample space…" /> : scatter.error ? <InlineError error={scatter.error} onRetry={() => scatter.refetch()} /> : <VisualizationCard spec={spec} aspectRatio={aspect === 'fit' ? undefined : aspect ?? '1 / 1'} onPointClick={handlePoint} emptyDescription="No concrete samples contain both selected numeric fields." />}
    {representatives.length > 0 && <Card p="lg"><Group justify="space-between" mb="md"><div><Text fw={650}>Representative concrete cases</Text><Text size="xs" c="dimmed">{mode === 'compare' ? 'Paired outcome differences or the largest selected-metric deltas.' : 'Outcome, center, and boundary representatives from the complete selected experiment set.'}</Text></div><Badge variant="light">{representatives.length} cases</Badge></Group><SimpleGrid cols={{ base: 1, md: 2, xl: 3 }}>{representatives.map(({ label, reason, point }) => <Card key={`${label}-${point.run_id}`} withBorder p="md"><Group justify="space-between"><Text fw={600} size="sm">{label}</Text><StatusBadge value={point.outcome} /></Group><Text size="xs" c="dimmed" mt={4}>{reason}</Text><Text size="xs" className="pisa-code" mt="sm">sample {point.ordinal} · ({point.x.toPrecision(5)}, {point.y.toPrecision(5)})</Text><Button mt="sm" size="compact-sm" variant="light" onClick={() => onOpen(point.run_id, mode === 'compare' ? [leftDataset, rightDataset].filter((item): item is string => Boolean(item)) : [point.dataset_id])}>{mode === 'compare' ? 'Open paired replay' : 'Open in replay'}</Button></Card>)}</SimpleGrid></Card>}
  </Stack>;
}

function ChartSection({ datasetId, section }: { datasetId: string; section: string }) {
  const charts = useReportCharts(datasetId, section);
  const descriptions: Record<string, string> = {
    sampling: 'Marginal distributions, pair coverage, density, nearest-neighbour distance, and outcome boundaries.',
    outcomes: 'Outcome rates, safety metrics, parameter regions, and failure discovery without conflating invalid runs.',
    performance: 'Wall time, simulation time, stage durations, throughput, and resource use.',
    sensitivity: 'Effect ranking and response surfaces computed as a background analysis job.',
  };
  if (charts.isLoading) return <PageLoading label={`Loading ${section} views…`} />;
  if (charts.error) return <InlineError error={charts.error} onRetry={() => charts.refetch()} />;
  if (!charts.data?.length) return <Card><EmptyState title={`No ${section} views yet`} description={`${descriptions[section]} This section becomes available when its report stage completes.`} action={<Button variant="light" leftSection={<IconRefresh size={16} />} onClick={() => charts.refetch()}>Check again</Button>} /></Card>;
  return <SimpleGrid cols={{ base: 1, xl: 2 }}>{charts.data.map((chart) => <VisualizationCard key={chart.id} spec={chart} datasetId={datasetId} />)}</SimpleGrid>;
}

function comparisonValue(value: number | null | undefined, unit?: string | null): string {
  if (value == null || !Number.isFinite(value)) return '—';
  const formatted = value.toLocaleString(undefined, { maximumSignificantDigits: 6 });
  return unit ? `${formatted} ${unit}` : formatted;
}

function CrossExperimentOverview({ summary, onOpen }: { summary?: CrossExperimentComparison; onOpen: (id: string, experiments?: string[]) => void }) {
  if (!summary) return <Alert color="yellow" icon={<IconAlertTriangle size={17} />} title="Workbench server update required">The comparisons API did not return a cross-experiment summary. This does not mean the report is incompatible. Restart the Workbench server, then reload this page.</Alert>;
  if (!summary.available) return <Alert color="gray" title="Cross-experiment summary unavailable">{summary.reason?.replaceAll('_', ' ') ?? 'The normalized index does not contain at least two safely pairable canonical experiments.'}</Alert>;
  return <Card p="lg">
    <Group justify="space-between" align="flex-start" mb="md">
      <div><Text fw={700} size="lg">Cross-experiment consistency</Text><Text size="sm" c="dimmed">Every canonical experiment is compared together on parameter-hash-matched samples.</Text></div>
      <Badge variant="light" size="lg">{summary.experiment_count} experiments</Badge>
    </Group>
    <SimpleGrid cols={{ base: 2, md: 4 }} mb="md">
      <Card withBorder p="md"><Text size="xs" c="dimmed" tt="uppercase" fw={600}>Experiments</Text><Text fz={26} fw={700}>{summary.experiment_count.toLocaleString()}</Text><Text size="xs" c="dimmed">Canonical datasets</Text></Card>
      <Card withBorder p="md"><Text size="xs" c="dimmed" tt="uppercase" fw={600}>Common samples</Text><Text fz={26} fw={700}>{summary.common_sample_count.toLocaleString()}</Text><Text size="xs" c="dimmed">Unique in every experiment</Text></Card>
      <Card withBorder p="md"><Text size="xs" c="dimmed" tt="uppercase" fw={600}>Union samples</Text><Text fz={26} fw={700}>{summary.union_sample_count.toLocaleString()}</Text><Text size="xs" c="dimmed">{summary.excluded_noncommon_sample_count.toLocaleString()} not common to all</Text></Card>
      <Card withBorder p="md"><Text size="xs" c="dimmed" tt="uppercase" fw={600}>Aliases excluded</Text><Text fz={26} fw={700}>{summary.excluded_duplicate_aliases.length.toLocaleString()}</Text><Text size="xs" c="dimmed">Duplicate results are not reweighted</Text></Card>
    </SimpleGrid>
    <Group gap="xs" mb="md">{summary.experiments.map((experiment) => <Badge key={experiment} variant="outline" color="gray">{experiment}</Badge>)}</Group>
    <Alert color="blue" icon={<IconShieldCheck size={17} />} title="Comparison rule" mb="lg">Variation is the per-sample maximum minus minimum. Only samples with a valid value in every experiment enter max, 95th percentile, population std, and median. Partial, unavailable, missing, and invalid values remain separate coverage counts.</Alert>
    {summary.most_similar_pair && <Alert color="teal" icon={<IconShieldCheck size={17} />} title={`Most similar experiments · ${summary.most_similar_pair.left} and ${summary.most_similar_pair.right}`} mb="lg"><Text size="sm" fw={650}>{summary.most_similar_pair.information_consistent_count.toLocaleString()} / {summary.most_similar_pair.information_comparable_count.toLocaleString()} concrete samples have identical comparable information ({summary.most_similar_pair.information_agreement_ratio == null ? '—' : `${(summary.most_similar_pair.information_agreement_ratio * 100).toFixed(2)}%`}).</Text><Text size="xs" mt={4}>{summary.most_similar_pair.information_scope}</Text><Text size="xs" c="dimmed">Excluded: {summary.most_similar_pair.information_exclusions}</Text></Alert>}

    <Text fw={650} mb="xs">Discrete agreement</Text>
    <Text size="xs" c="dimmed" mb="sm">The denominator contains only common samples where every experiment recorded the field.</Text>
    <ScrollArea mb="xl"><table className="pisa-data-table"><thead><tr><th>Field</th><th>All agree</th><th>Comparable total</th><th>Agreement</th><th>Unavailable samples</th></tr></thead><tbody>{summary.discrete.map((item) => <tr key={item.key}><td><Text fw={600} size="sm">{item.label}</Text></td><td>{item.consistent_count.toLocaleString()}</td><td>{item.comparable_count.toLocaleString()}</td><td>{item.agreement_ratio == null ? '—' : `${(item.agreement_ratio * 100).toFixed(2)}%`}</td><td>{item.unavailable_sample_count.toLocaleString()}</td></tr>)}</tbody></table></ScrollArea>

    <Text fw={650} mb="xs">Ego trajectory disagreement</Text>
    <Text size="xs" c="dimmed" mb="sm">For each common sample, every experiment pair is compared and the largest pairwise ADE/FDE becomes that sample's trajectory variation. Recorded timestamps are intersected exactly; no states are interpolated.</Text>
    {!summary.trajectory?.available ? <Alert color="gray" mb="xl">Trajectory comparison unavailable: {summary.trajectory?.reason?.replaceAll('_', ' ') ?? 'ego trajectory paths were not indexed'}.</Alert> : <ScrollArea mb="xl" type="auto"><table className="pisa-data-table"><thead><tr><th>Measure</th><th>Eligible samples</th><th>Max</th><th>Min</th><th>Mean</th><th>Population std</th><th>Median</th></tr></thead><tbody>{[summary.trajectory.ade, summary.trajectory.fde].filter((value): value is NonNullable<typeof value> => Boolean(value)).map((metric) => {
      const cell = (key: 'max' | 'min' | 'mean' | 'std' | 'median') => {
        const value = metric[key];
        const representative = metric.representatives[key];
        if (!representative) return comparisonValue(value, 'm');
        const open = () => onOpen(representative.left_run_id, [representative.left_experiment, representative.right_experiment]);
        if (key === 'max' || key === 'min') return <Button size="compact-xs" variant="subtle" onClick={open}>{comparisonValue(value, 'm')}</Button>;
        return <Stack gap={2} align="flex-start"><Text size="sm">{comparisonValue(value, 'm')}</Text><Button size="compact-xs" variant="subtle" px={0} title={`Actual variation ${comparisonValue(representative.variation, 'm')} · distance to statistic ${comparisonValue(representative.distance_to_statistic, 'm')} · ${representative.common_steps} common recorded steps`} onClick={open}>Nearest sample · {comparisonValue(representative.variation, 'm')}</Button></Stack>;
      };
      return <tr key={metric.key}><td><Text fw={600}>{metric.key.toUpperCase()}</Text><Text size="xs" c="dimmed">{metric.key === 'ade' ? 'Mean position distance over common steps' : 'Position distance at the last common step'}</Text></td><td><Text size="sm">{summary.trajectory!.eligible_sample_count.toLocaleString()} / {summary.common_sample_count.toLocaleString()}</Text><Text size="xs" c="dimmed">{summary.trajectory!.experiment_pair_count} pairs · {summary.trajectory!.partial_sample_count} partial · {summary.trajectory!.unavailable_sample_count} unavailable</Text></td><td>{cell('max')}</td><td>{cell('min')}</td><td>{cell('mean')}</td><td>{cell('std')}</td><td>{cell('median')}</td></tr>;
    })}</tbody></table></ScrollArea>}

    <Text fw={650} mb="xs">Continuous variation across all experiments</Text>
    <Text size="xs" c="dimmed" mb="sm">Each row uses the metric's recorded unit. A sample is eligible only when all {summary.experiment_count} experiments have a valid value.</Text>
    <ScrollArea type="auto"><table className="pisa-data-table"><thead><tr><th>Metric</th><th>Eligible samples</th><th>Coverage exceptions</th><th>Max variation</th><th>Min variation</th><th>95th-percentile variation</th><th>Std variation</th><th>Median variation</th></tr></thead><tbody>{summary.continuous.map((item) => {
      const statistic = (key: 'max' | 'min' | 'p95' | 'std' | 'median', value: number | null | undefined) => {
        const representative = item.representatives?.[key];
        if (!representative) return comparisonValue(value, item.unit);
        const exact = key === 'max' || key === 'min';
        if (exact) return <Button size="compact-xs" variant="subtle" title={`Open the exact ${key} sample`} onClick={() => onOpen(representative.run_id, summary.experiments)}>{comparisonValue(value, item.unit)}</Button>;
        const distance = value == null ? undefined : Math.abs(representative.variation - value);
        return <Stack gap={2} align="flex-start"><Text size="sm">{comparisonValue(value, item.unit)}</Text><Button size="compact-xs" variant="subtle" px={0} title={`This statistic is an aggregate, not a concrete run. Actual sample variation ${comparisonValue(representative.variation, item.unit)} · distance ${comparisonValue(distance, item.unit)}`} onClick={() => onOpen(representative.run_id, summary.experiments)}>Nearest sample · {comparisonValue(representative.variation, item.unit)}</Button></Stack>;
      };
      return <tr key={item.key}><td><Text fw={600} size="sm">{item.label}</Text><Text size="xs" c="dimmed" className="pisa-code">{item.key}</Text><Text size="xs" c="dimmed">{item.validity_rule}</Text></td><td><Text size="sm">{item.eligible_sample_count.toLocaleString()} / {summary.common_sample_count.toLocaleString()}</Text><Text size="xs" c="dimmed">{item.valid_execution_count.toLocaleString()} / {item.total_execution_count.toLocaleString()} executions valid</Text></td><td><Text size="xs">Partial samples: {item.partial_sample_count.toLocaleString()}</Text><Text size="xs">Unavailable samples: {item.unavailable_sample_count.toLocaleString()}</Text><Text size="xs" c="dimmed">Missing executions: {item.missing_execution_count.toLocaleString()} · invalid: {item.invalid_execution_count.toLocaleString()}</Text></td><td>{statistic('max', item.variation_max)}</td><td>{statistic('min', item.variation_min)}</td><td>{statistic('p95', item.variation_p95)}</td><td>{statistic('std', item.variation_std)}</td><td>{statistic('median', item.variation_median)}</td></tr>;
    })}</tbody></table></ScrollArea>

    <Accordion mt="lg" variant="contained"><Accordion.Item value="pairing-audit"><Accordion.Control>Pairing and data-availability audit</Accordion.Control><Accordion.Panel><Text size="sm" mb="sm"><b>Pairing key:</b> {summary.pairing_key}</Text><ScrollArea><table className="pisa-data-table"><thead><tr><th>Experiment</th><th>Runs</th><th>Missing parameter hash</th><th>Ambiguous hashes</th></tr></thead><tbody>{summary.experiments.map((experiment) => { const quality = summary.hash_quality[experiment]; return <tr key={experiment}><td>{experiment}</td><td>{quality?.run_count.toLocaleString() ?? '—'}</td><td>{quality?.missing_hash_runs.toLocaleString() ?? '—'}</td><td>{quality?.ambiguous_hashes.toLocaleString() ?? '—'}</td></tr>; })}</tbody></table></ScrollArea>{summary.excluded_duplicate_aliases.length > 0 && <Text size="xs" c="dimmed" mt="sm">Excluded duplicate aliases: {summary.excluded_duplicate_aliases.join(', ')}</Text>}</Accordion.Panel></Accordion.Item></Accordion>
  </Card>;
}

function Compare({ datasetId, onOpen }: { datasetId: string; onOpen: (id: string, experiments?: string[]) => void }) {
  const comparisons = useQuery({ queryKey: ['comparisons-v2', datasetId], queryFn: () => api.datasets.comparisons(datasetId), retry: 1, refetchOnMount: 'always' });
  const [selected, setSelected] = useState<ComparisonClass>();
  if (comparisons.isLoading) return <PageLoading label="Classifying comparisons…" />;
  if (comparisons.error) return <InlineError error={comparisons.error} onRetry={() => comparisons.refetch()} />;
  const items = comparisons.data?.items ?? [];
  return <Stack gap="xl">
    <CrossExperimentOverview summary={comparisons.data?.cross_experiment} onOpen={onOpen} />
    <div><Text fw={700} size="lg">Pairwise comparisons</Text><Text size="sm" c="dimmed">Inspect the original two-experiment classifications, deltas, and visualizations.</Text></div>
    {!items.length ? <Card><EmptyState title="No defensible pairwise comparison found" description="A pairwise comparison requires compatible parameter domains and recorded semantics." /></Card> : <SimpleGrid cols={{ base: 1, lg: 5 }}>
      <Card p="lg" style={{ gridColumn: 'span 2' }}><Text fw={650} mb="md">Available comparisons</Text><Stack gap="xs">{items.map((comparison) => <Card key={comparison.id} withBorder p="md" bg={selected?.id === comparison.id ? 'indigo.0' : undefined} onClick={() => setSelected(comparison)} style={{ cursor: 'pointer' }}><Group justify="space-between" wrap="nowrap"><div><Text size="sm" fw={600}>{comparison.left}</Text><Text size="xs" c="dimmed">vs {comparison.right}</Text></div><StatusBadge value={comparison.role} /></Group><Group gap="xs" mt="xs"><Badge variant="light" color="gray">{comparison.matched.toLocaleString()} paired</Badge>{comparison.information_comparable_count !== undefined && <Badge variant="light" color="teal">{comparison.information_consistent_count?.toLocaleString() ?? 0} / {comparison.information_comparable_count.toLocaleString()} fully consistent</Badge>}{comparison.left_only > 0 && <Badge variant="light" color="yellow">{comparison.left_only} left only</Badge>}{comparison.right_only > 0 && <Badge variant="light" color="yellow">{comparison.right_only} right only</Badge>}</Group></Card>)}</Stack></Card>
      <Card p="lg" style={{ gridColumn: 'span 3' }}>{selected ? <Stack><Group justify="space-between"><div><Text fw={650}>{selected.left} → {selected.right}</Text><Text size="sm" c="dimmed">{selected.note ?? 'Comparison semantics were classified from recorded provenance.'}</Text></div><StatusBadge value={selected.role} /></Group><Alert color="blue" icon={<IconShieldCheck size={17} />} title="Interpretation guardrail">Complete pairs are used for paired metrics. Missing left/right values and semantic differences are reported separately.</Alert>{selected.information_comparable_count !== undefined && <Card withBorder p="md"><Text size="xs" c="dimmed" tt="uppercase" fw={650}>Fully identical concrete information</Text><Group align="baseline" gap="xs"><Text fz={30} fw={750}>{selected.information_consistent_count?.toLocaleString() ?? 0}</Text><Text c="dimmed">/ {selected.information_comparable_count.toLocaleString()} paired concrete samples · {selected.information_agreement_ratio == null ? '—' : `${(selected.information_agreement_ratio * 100).toFixed(2)}%`}</Text></Group><Text size="xs">Compared: {selected.information_scope}</Text><Text size="xs" c="dimmed">Excluded: {selected.information_exclusions}</Text></Card>}{selected.agreement !== undefined && <div><Text size="xs" c="dimmed">Outcome agreement</Text><Text fz={32} fw={700}>{(selected.agreement <= 1 ? selected.agreement * 100 : selected.agreement).toFixed(1)}%</Text></div>}<ChartSection datasetId={datasetId} section={`compare:${selected.id}`} /></Stack> : <EmptyState title="Select a comparison" description="Classification determines whether the workspace uses paired deltas, agreement, common-domain coverage, or description only." />}</Card>
    </SimpleGrid>}
  </Stack>;
}

function Runs({ datasetId, onOpen }: { datasetId: string; onOpen: (id: string) => void }) {
  const [search, setSearch] = useState('');
  const [outcome, setOutcome] = useState<string | null>(null);
  const [experiment, setExperiment] = useState<string | null>(null);
  const [cursor, setCursor] = useState<string>();
  const [cursorHistory, setCursorHistory] = useState<Array<string | undefined>>([]);
  const [sort, setSort] = useState<string | null>('scenario_id');
  const [descending, setDescending] = useState(false);
  const fields = useQuery({ queryKey: ['run-filter-fields', datasetId], queryFn: () => api.datasets.scatter(datasetId, { limit: 100 }), retry: 1 });
  const metricSorts = (fields.data?.fields ?? []).filter((field) => field.source === 'metric').map((field) => ({ value: `metric:${field.key.replace(/^metric:/, '')}`, label: `Metric · ${field.label}` }));
  const resetPage = () => { setCursor(undefined); setCursorHistory([]); };
  const runs = useQuery({ queryKey: ['runs', datasetId, cursor, search, outcome, experiment, sort, descending], queryFn: () => api.datasets.runs(datasetId, { cursor, limit: 100, search, outcome: outcome ?? undefined, experiment: experiment ?? undefined, sort: sort ?? 'scenario_id', descending }), placeholderData: (previous) => previous, retry: 1 });
  return (
    <Card p={0}>
      <Group p="md" justify="space-between"><Group><Select placeholder="All outcomes" clearable data={['success', 'fail', 'invalid', 'unknown']} value={outcome} onChange={(value) => { setOutcome(value); resetPage(); }} /><Select placeholder="All experiments" clearable searchable data={(fields.data?.datasets ?? []).map((value) => ({ value, label: value }))} value={experiment} onChange={(value) => { setExperiment(value); resetPage(); }} /><Select aria-label="Run sort" value={sort} onChange={(value) => { setSort(value); resetPage(); }} allowDeselect={false} searchable data={[{ value: 'scenario_id', label: 'Iteration' }, { value: 'duration', label: 'Duration' }, ...metricSorts]} /><Button variant="default" size="sm" onClick={() => { setDescending((value) => !value); resetPage(); }}>{descending ? 'Descending' : 'Ascending'}</Button></Group><Text size="xs" c="dimmed">Server-side filtering · 100 rows per page</Text></Group>
      {runs.isLoading ? <PageLoading label="Querying runs…" /> : runs.error ? <div style={{ padding: 16 }}><InlineError error={runs.error} onRetry={() => runs.refetch()} /></div> : <><ScrollArea><table className="pisa-data-table pisa-runs-table"><thead><tr><th><Group gap={4} wrap="nowrap">Iteration<Popover width={250} position="bottom-start" shadow="md" keepMounted><Popover.Target><ActionIcon size="compact-sm" variant={search ? 'light' : 'subtle'} color={search ? 'indigo' : 'gray'} aria-label="Filter by iteration ID"><IconSearch size={14} /></ActionIcon></Popover.Target><Popover.Dropdown><TextInput autoFocus label="Iteration ID" placeholder="e.g. 128" value={search} onChange={(event) => { setSearch(event.currentTarget.value); resetPage(); }} /><Group justify="space-between" mt={6}><Text size="xs" c="dimmed">Matches only iteration/scenario ID.</Text><Button size="compact-xs" variant="subtle" disabled={!search} onClick={() => { setSearch(''); resetPage(); }}>Clear</Button></Group></Popover.Dropdown></Popover>{runs.isFetching && <Badge size="xs" variant="dot" color="blue">Filtering</Badge>}</Group></th><th>Experiment</th><th>Outcome</th><th>Parameters</th><th>Stop reason</th><th>Duration</th><th>Min TTC</th><th>Collision</th><th /></tr></thead><tbody>{runs.data?.items.map((run) => <tr key={run.id}><td>{run.iteration ?? run.id}</td><td>{run.experiment}</td><td><StatusBadge value={run.outcome} /></td><td><Text size="xs" className="pisa-code" maw={360} truncate title={JSON.stringify(run.parameters)}>{Object.entries(run.parameters ?? {}).map(([key, value]) => `${key}=${String(value)}`).join(', ') || '—'}</Text></td><td><Text size="xs" maw={260} truncate title={run.stop_reason}>{run.stop_reason ?? '—'}</Text></td><td>{run.duration_seconds !== undefined ? `${run.duration_seconds.toFixed(2)} s` : '—'}</td><td>{run.min_ttc == null ? <Text c="dimmed" size="sm">Missing</Text> : `${run.min_ttc.toFixed(3)} s`}</td><td>{run.collision ? <Badge color="red" variant="light">Yes</Badge> : 'No'}</td><td><Button size="compact-xs" variant="subtle" onClick={() => onOpen(run.id)}>Open concrete</Button></td></tr>)}{!runs.data?.items.length && <tr><td colSpan={9}><Text ta="center" c="dimmed" size="sm" py="xl">No matching runs. Adjust or clear the iteration, outcome, or experiment filter.</Text></td></tr>}</tbody></table></ScrollArea><Group justify="space-between" p="md"><Text size="xs" c="dimmed">{runs.data?.total !== undefined ? `${runs.data.total.toLocaleString()} matching runs` : 'Cursor-paginated results'}</Text><Group><Button size="xs" variant="default" disabled={!cursorHistory.length} onClick={() => { const history = [...cursorHistory]; setCursor(history.pop()); setCursorHistory(history); }}>Previous 100</Button><Button size="xs" variant="light" disabled={!runs.data?.next_cursor} onClick={() => { setCursorHistory((history) => [...history, cursor]); setCursor(runs.data?.next_cursor ?? undefined); }}>Next 100</Button></Group></Group></>}
    </Card>
  );
}

function Replay({ datasetId, runId, onChoose, onOpen }: { datasetId: string; runId?: string; onChoose: () => void; onOpen: (id: string, experiments?: string[]) => void }) {
  const queryClient = useQueryClient();
  const detail = useQuery({ queryKey: ['case', datasetId, runId], queryFn: () => api.datasets.case(datasetId, runId!, 2_000), enabled: Boolean(runId), staleTime: 300_000, retry: 1 });
  const pairedDescriptors = detail.data?.navigation?.comparison_runs ?? [];
  const availableExperiments = [...new Set([detail.data?.run.experiment, ...pairedDescriptors.map((item) => item.dataset_id)].filter((item): item is string => Boolean(item)))];
  const availableExperimentKey = availableExperiments.join('\0');
  const [selectedExperiments, setSelectedExperiments] = useState<string[]>([]);
  const [replayAnalysisMode, setReplayAnalysisMode] = useState<string | null>('overlay');
  const [compareLeft, setCompareLeft] = useState<string | null>(null);
  const [compareRight, setCompareRight] = useState<string | null>(null);
  const initializedExperimentRun = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (!runId || !detail.data || !availableExperiments.length) return;
    if (initializedExperimentRun.current !== runId) {
      initializedExperimentRun.current = runId;
      let requested: string[] = [];
      try {
        const stored = JSON.parse(window.sessionStorage.getItem(`pisa:replay-context:${datasetId}`) ?? '{}') as { runId?: string; experiments?: string[] };
        if (stored.runId === runId && Array.isArray(stored.experiments)) requested = stored.experiments;
      } catch { /* Ignore malformed navigation context and use the selected run. */ }
      const valid = requested.filter((experiment) => availableExperiments.includes(experiment));
      setSelectedExperiments(valid.length ? valid : [detail.data.run.experiment]);
      return;
    }
    setSelectedExperiments((current) => current.filter((experiment) => availableExperiments.includes(experiment)));
  }, [availableExperimentKey, datasetId, detail.data, runId]);
  useEffect(() => {
    if (!availableExperiments.length) return;
    setCompareLeft((current) => current && availableExperiments.includes(current) ? current : detail.data?.run.experiment ?? availableExperiments[0]);
    setCompareRight((current) => current && availableExperiments.includes(current) && current !== (detail.data?.run.experiment ?? availableExperiments[0]) ? current : availableExperiments.find((experiment) => experiment !== (detail.data?.run.experiment ?? availableExperiments[0])) ?? null);
  }, [availableExperimentKey, detail.data?.run.experiment]);
  useEffect(() => {
    if (replayAnalysisMode !== 'compare' || !compareLeft || !compareRight || compareLeft === compareRight) return;
    setSelectedExperiments((current) => current.length === 2 && current.includes(compareLeft) && current.includes(compareRight) ? current : [compareLeft, compareRight]);
  }, [compareLeft, compareRight, replayAnalysisMode]);
  // Keep one stable query slot per paired experiment. Filtering the query array
  // itself can make useQueries temporarily associate a newly checked experiment
  // with the previous slot's result, leaving its checkbox checked but no trace rendered.
  const pairedQueries = useQueries({ queries: pairedDescriptors.filter((item) => item.run_id !== runId).map((item) => ({ queryKey: ['case', datasetId, item.run_id, 'without-map'], queryFn: () => api.datasets.case(datasetId, item.run_id, 2_000, false), enabled: selectedExperiments.includes(item.dataset_id), staleTime: 300_000, retry: 1 })) });
  const pairedQueriesPending = pairedQueries.some((query) => query.isFetching);
  const pairedCases = useMemo(() => [detail.data, ...pairedQueries.map((query) => query.data)].filter((value): value is CaseDetail => Boolean(value)), [detail.data, ...pairedQueries.map((query) => query.data)]);
  const [time, setTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const playbackFrame = useRef<number | undefined>(undefined);
  const [playbackRate, setPlaybackRate] = useState<string | null>('1');
  const [mapReference, setMapReference] = useState(true);
  const [mapBoundaries, setMapBoundaries] = useState(true);
  const [mapJunctions, setMapJunctions] = useState(true);
  const [showMap, setShowMap] = useState(true);
  const [followCursor, setFollowCursor] = useState(false);
  const [trailOnly, setTrailOnly] = useState(true);
  const [showBoundingBoxes, setShowBoundingBoxes] = useState(true);
  const [showCollisionPositions, setShowCollisionPositions] = useState(true);
  const [showEgo, setShowEgo] = useState(true);
  const [showAgents, setShowAgents] = useState(true);
  const [showGoal, setShowGoal] = useState(true);
  const [showTrajectoryGrid, setShowTrajectoryGrid] = useState(false);
  const [showTrajectoryAxes, setShowTrajectoryAxes] = useState(true);
  const [focusActor, setFocusActor] = useState<string | null>(null);
  const [focusView, setFocusView] = useState<string | null>('centered');
  const [manualViewport, setManualViewport] = useState(false);
  const [viewportXMin, setViewportXMin] = useState<number | string>('');
  const [viewportXMax, setViewportXMax] = useState<number | string>('');
  const [viewportYMin, setViewportYMin] = useState<number | string>('');
  const [viewportYMax, setViewportYMax] = useState<number | string>('');
  const [selectedActors, setSelectedActors] = useState<string[]>([]);
  const [includeAgentMetrics, setIncludeAgentMetrics] = useSessionState<boolean>(`pisa:replay-include-agent-metrics:${datasetId}`, false);
  const [activeStateMetrics, setActiveStateMetrics] = useSessionState<MetricKey[]>(`pisa:replay-state-metric-types-v3:${datasetId}`, ['distance']);
  const [activeControlMetrics, setActiveControlMetrics] = useSessionState<ControlKey[]>(`pisa:replay-control-types-v3:${datasetId}`, []);
  const [genericChannel, setGenericChannel] = useState<string | null>(null);
  const [genericField, setGenericField] = useState<string | null>(null);
  const [mediaFormat, setMediaFormat] = useState<string | null>('gif');
  const [mediaMode, setMediaMode] = useState<string | null>('standard');
  const [mediaTiming, setMediaTiming] = useState<string | null>('realtime');
  const [mediaFps, setMediaFps] = useState<number | string>(10);
  const [mediaFrames, setMediaFrames] = useState<number | string>(180);
  const replay = detail.data as (typeof detail.data & ReplayCaseExtras) | undefined;
  const selectedCases = useMemo(() => {
    const experiments = replayAnalysisMode === 'compare' && compareLeft && compareRight ? [compareLeft, compareRight] : selectedExperiments;
    return pairedCases.filter((item) => experiments.includes(item.run.experiment));
  }, [compareLeft, compareRight, pairedCases, replayAnalysisMode, selectedExperiments]);
  const realtimeMediaTiming = useMemo(() => {
    const recorded = selectedCases.flatMap((item) => Object.values(item.traces).flatMap((points) => points.map((point) => finiteNumber(point.time)).filter((value): value is number => value !== undefined)));
    const duration = recorded.length > 1 ? (Math.max(...recorded) - Math.min(...recorded)) / Math.max(0.05, Number(playbackRate ?? 1)) : 0;
    // Keep the requested wall-clock duration. Long recordings lower their output
    // frame rate rather than silently accelerating when they reach the frame cap.
    const fps = duration > 0 ? Math.max(1, Math.min(10, Math.floor(999 / duration))) : 10;
    return { fps, frames: Math.max(2, Math.min(1000, Math.ceil(duration * fps) + 1)) };
  }, [playbackRate, selectedCases]);
  const mediaMutation = useMutation({
    mutationFn: () => api.datasets.createMedia(datasetId, { run_id: runId!, run_ids: selectedCases.map((item) => item.run.id), actor_names: selectedActors, format: (mediaFormat ?? 'gif') as 'gif' | 'mp4' | 'webm' | 'png', fps: mediaTiming === 'realtime' ? realtimeMediaTiming.fps : Number(mediaFps) || 10, max_frames: mediaTiming === 'realtime' ? realtimeMediaTiming.frames : Number(mediaFrames) || 180, playback_rate: mediaTiming === 'realtime' ? Math.max(0.05, Number(playbackRate ?? 1)) : undefined, width: mediaTiming === 'realtime' ? 720 : undefined, height: mediaTiming === 'realtime' ? 405 : undefined, include_map: showMap, map_reference: mapReference, map_boundaries: mapBoundaries, map_junctions: mapJunctions, show_bounding_boxes: showBoundingBoxes, follow_cursor: followCursor, trail_only: trailOnly, render_mode: (mediaMode ?? 'standard') as 'standard' | 'trajectory_view', show_ego: showEgo, show_agents: showAgents, show_goal: showGoal, show_grid: showTrajectoryGrid, show_axes: showTrajectoryAxes, x_min: manualViewport && typeof viewportXMin === 'number' ? viewportXMin : undefined, x_max: manualViewport && typeof viewportXMax === 'number' ? viewportXMax : undefined, y_min: manualViewport && typeof viewportYMin === 'number' ? viewportYMin : undefined, y_max: manualViewport && typeof viewportYMax === 'number' ? viewportYMax : undefined }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['jobs'] }),
  });
  const mediaJob = useQuery({
    queryKey: ['job', mediaMutation.data?.id],
    queryFn: () => api.jobs.get(mediaMutation.data!.id),
    enabled: Boolean(mediaMutation.data?.id),
    refetchInterval: (query) => ['queued', 'running'].includes(query.state.data?.state ?? '') ? 750 : false,
  });
  const traces = useMemo<Record<string, ReplayPoint[]>>(() => Object.fromEntries(selectedCases.flatMap((item) => Object.entries(item.traces).map(([name, points]) => [`${item.run.experiment} · ${name}`, points as ReplayPoint[]]))), [selectedCases]);
  const events = useMemo(() => selectedCases.flatMap((item) => (item.events ?? []).map((event) => ({ ...event, label: `${item.run.experiment} · ${event.label}` }))).filter((event) => finiteNumber(event.time) !== undefined).sort((left, right) => left.time - right.time), [selectedCases]);
  const geometry = useMemo(() => {
    const unique = new Map<string, ReplayGeometry>();
    for (const item of selectedCases.flatMap((value) => value.geometry ?? []) as ReplayGeometry[]) {
      const key = `${item.agent_id ?? ''}\0${item.entity_name ?? ''}`;
      if (!unique.has(key)) unique.set(key, item);
    }
    return [...unique.values()].sort((left, right) => Number(Boolean(right.is_ego)) - Number(Boolean(left.is_ego)) || String(left.entity_name).localeCompare(String(right.entity_name)));
  }, [selectedCases]);
  const timeDomain = useMemo(() => {
    let minimum = Number.POSITIVE_INFINITY;
    let maximum = Number.NEGATIVE_INFINITY;
    for (const points of Object.values(traces)) {
      for (const point of points) {
        if (finiteNumber(point.time) === undefined) continue;
        minimum = Math.min(minimum, point.time);
        maximum = Math.max(maximum, point.time);
      }
    }
    for (const event of events) {
      minimum = Math.min(minimum, event.time);
      maximum = Math.max(maximum, event.time);
    }
    return Number.isFinite(minimum) && Number.isFinite(maximum) ? { minimum, maximum } : { minimum: 0, maximum: 0 };
  }, [events, traces]);
  const timeline = useMemo(() => [...new Set([...Object.values(traces).flatMap((points) => points.map((point) => point.time)), ...events.map((event) => event.time)].filter((value) => Number.isFinite(value)))].sort((left, right) => left - right), [events, traces]);
  const timeIndex = Math.max(0, timeline.findIndex((value) => value === time));
  const initializedRun = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (!timeline.length) return;
    if (initializedRun.current !== runId) {
      initializedRun.current = runId;
      setTime(timeline[timeline.length - 1]);
      return;
    }
    setTime((current) => {
      if (current <= timeline[0]) return timeline[0];
      if (current >= timeline[timeline.length - 1]) return timeline[timeline.length - 1];
      return timeline.reduce((nearest, value) => Math.abs(value - current) < Math.abs(nearest - current) ? value : nearest, timeline[0]);
    });
  }, [runId, timeline]);
  useEffect(() => {
    setPlaying(false);
  }, [runId]);
  useEffect(() => {
    if (!playing || timeline.length < 2) return;
    const rate = Math.max(0.01, Number(playbackRate ?? 1));
    const realStart = performance.now(), simStart = time, lastTime = timeline[timeline.length - 1];
    const tick = (now: number) => {
      const target = simStart + (now - realStart) / 1000 * rate;
      if (target >= lastTime) { setTime(lastTime); setPlaying(false); return; }
      let low = 0, high = timeline.length - 1;
      while (low < high) { const middle = Math.ceil((low + high) / 2); if (timeline[middle] <= target) low = middle; else high = middle - 1; }
      setTime((current) => current === timeline[low] ? current : timeline[low]);
      playbackFrame.current = requestAnimationFrame(tick);
    };
    playbackFrame.current = requestAnimationFrame(tick);
    return () => { if (playbackFrame.current !== undefined) cancelAnimationFrame(playbackFrame.current); };
  }, [playbackRate, playing, timeline]);
  const currentTime = Math.max(timeDomain.minimum, Math.min(timeDomain.maximum, time));
  const positionalTraces = useMemo(() => Object.entries(traces)
    .filter(([name]) => !/(metric|control|bounding|geometry|goal|current)/i.test(name))
    .map(([name, points]) => ({
      name,
      points: points.filter((point) => finiteNumber(point.time) !== undefined && finiteNumber(point.x) !== undefined && finiteNumber(point.y) !== undefined).sort((left, right) => left.time - right.time),
    }))
    .filter((trace) => trace.points.length > 0)
    .filter((trace) => (/ego/i.test(trace.name) ? showEgo : showAgents))
    .filter((trace) => selectedActors.includes(trace.name))
    .sort((left, right) => Number(/ego/i.test(right.name)) - Number(/ego/i.test(left.name)) || right.points.length - left.points.length)
    .slice(0, 48), [selectedActors, showAgents, showEgo, traces]);
  const availableActors = useMemo(() => Object.entries(traces).filter(([name, points]) => !/(metric|control|bounding|geometry|goal|current)/i.test(name) && points.some((point) => finiteNumber(point.x) !== undefined && finiteNumber(point.y) !== undefined)).map(([name]) => name), [traces]);
  const actorColorRegistry = useRef<Record<string, string>>({});
  const actorColors = useMemo(() => {
    const registry = actorColorRegistry.current;
    const assigned = new Set(Object.values(registry));
    for (const actor of [...availableActors].sort()) {
      if (registry[actor]) continue;
      const preferred = actorColor(actor);
      let index = actorPalette.indexOf(preferred);
      while (assigned.has(actorPalette[index]) && assigned.size < actorPalette.length) index = (index + 1) % actorPalette.length;
      registry[actor] = actorPalette[index];
      assigned.add(registry[actor]);
    }
    return { ...registry };
  }, [availableActors]);
  const availableActorKey = availableActors.join('\0');
  useEffect(() => { setSelectedActors(availableActors); }, [availableActorKey]);
  useEffect(() => {
    if (focusActor && !availableActors.includes(focusActor)) setFocusActor(null);
  }, [availableActorKey, availableActors, focusActor]);
  useEffect(() => {
    if (focusActor && !selectedActors.includes(focusActor)) setFocusActor(null);
  }, [focusActor, selectedActors]);
  const channelInfo = useMemo(() => Object.entries(traces).map(([name, points]) => {
      const fields = new Set<string>();
      for (const point of points.slice(0, 250)) {
        for (const [key, value] of Object.entries(point)) if (key !== 'values' && value !== undefined && value !== null) fields.add(key);
        for (const key of Object.keys(point.values ?? {})) fields.add(key);
      }
      return { name, pointCount: points.length, fields: [...fields].sort() };
    }), [traces]);
  useEffect(() => {
    if (!genericChannel || !traces[genericChannel]) {
      setGenericChannel(channelInfo[0]?.name ?? null);
      setGenericField(null);
    }
  }, [channelInfo, genericChannel, traces]);
  const genericFields = useMemo(() => {
    const fields = new Set<string>();
    for (const point of traces[genericChannel ?? ''] ?? []) {
      for (const [key, value] of Object.entries(point.values ?? {})) if (finiteNumber(value) !== undefined) fields.add(key);
      for (const key of ['x', 'y', 'yaw', 'speed', 'ttc', 'throttle', 'brake', 'steer', 'acceleration', 'yaw_rate'] as const) if (finiteNumber(point[key]) !== undefined) fields.add(key);
    }
    return [...fields].sort();
  }, [genericChannel, traces]);
  useEffect(() => {
    if (!genericField || !genericFields.includes(genericField)) setGenericField(genericFields[0] ?? null);
  }, [genericField, genericFields]);
  const genericSpec = useMemo<VisualizationSpec>(() => {
    const points = traces[genericChannel ?? ''] ?? [];
    const rows = points.map((point) => {
      const direct = point[genericField as keyof ReplayPoint];
      const value = finiteNumber(direct) ?? finiteNumber(point.values?.[genericField ?? '']);
      return [point.time, value ?? null] as [number, number | null];
    }).filter((row) => finiteNumber(row[0]) !== undefined);
    const valid = rows.filter((row) => row[1] !== null).length;
    return { id: `raw-channel-${genericChannel}-${genericField}`, title: 'Raw scalar channel explorer', subtitle: `${genericChannel ?? 'No channel'} · ${genericField ?? 'no field'} · ${valid.toLocaleString()} recorded numeric values; gaps remain missing.`, kind: 'line', option: valid ? { animation: false, tooltip: { trigger: 'axis' }, grid: { top: 24, right: 24, bottom: 54, left: 72 }, dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 14 }], xAxis: { type: 'value', name: 'Time (s)', scale: true }, yAxis: { type: 'value', name: genericField ?? 'Value', scale: true }, series: [{ type: 'line', name: genericField ?? 'Value', data: boundedSeries(rows), showSymbol: false, connectNulls: false, lineStyle: { width: 2, color: '#526ff0' } }] } : {} };
  }, [genericChannel, genericField, traces]);
  const metricActors = useMemo(() => {
    const actors = replayAnalysisMode === 'compare' ? availableActors : selectedActors;
    return actors.filter((actor) => includeAgentMetrics || /(^| · )ego($| · )/i.test(actor));
  }, [availableActors, includeAgentMetrics, replayAnalysisMode, selectedActors]);
  const metricSeriesByType = useMemo(() => Object.fromEntries((Object.keys(metricDefinitions) as MetricKey[]).map((metric) => {
    const series = metricActors.flatMap((actor) => {
      const actorPoints = traces[actor] ?? [];
      const experiment = actor.split(' · ')[0];
      const actorId = actorPoints.map((point) => point.values?.agent_id ?? point.values?.actor_id).find((value) => value !== undefined && value !== null);
      const candidates: Array<{ channel: string; rows: Array<[number, number | null]>; count: number }> = [];
      const addCandidate = (channel: string, points: ReplayPoint[]) => {
        const rows = points.map((point) => [point.time, metricValue(point, metric) ?? null] as [number, number | null]).filter(([rowTime]) => finiteNumber(rowTime) !== undefined).sort((left, right) => left[0] - right[0]);
        const count = rows.reduce((total, row) => total + Number(row[1] !== null), 0);
        if (count) candidates.push({ channel, rows, count });
      };
      addCandidate(actor, actorPoints);
      for (const [channel, points] of Object.entries(traces)) {
        if (channel === actor || !channel.startsWith(`${experiment} · `) || /control/i.test(channel)) continue;
        const channelMatchesActor = actorId !== undefined && (channel.endsWith(`_${String(actorId)}`) || points.some((point) => String(point.values?.agent_id ?? point.values?.actor_id ?? '') === String(actorId)));
        if (channelMatchesActor) addCandidate(channel, points);
      }
      if (!candidates.length && /ego/i.test(actor) && !['x', 'y', 'speed', 'acceleration'].includes(metric)) {
        for (const [channel, points] of Object.entries(traces)) if (channel.startsWith(`${experiment} · `) && /metric/i.test(channel) && !/control/i.test(channel)) addCandidate(channel, points);
      }
      candidates.sort((left, right) => right.count - left.count || left.channel.localeCompare(right.channel));
      const selected = candidates[0];
      return selected ? [{ metric, actor, experiment, selected }] : [];
    });
    return [metric, series];
  })), [metricActors, traces]);
  const controlSeriesByType = useMemo(() => Object.fromEntries((Object.keys(controlDefinitions) as ControlKey[]).map((control) => {
    const ackermann = control.startsWith('ackermann_');
    const series = Object.entries(traces).filter(([channel]) => /control/i.test(channel)).flatMap(([channel, points]) => {
      const experiment = channel.split(' · ')[0];
      const rows = points.filter((point) => isAckermannControl(point) === ackermann).map((point) => [point.time, controlValue(point, control) ?? null] as [number, number | null]).filter(([rowTime]) => finiteNumber(rowTime) !== undefined).sort((left, right) => left[0] - right[0]);
      const count = rows.reduce((total, row) => total + Number(row[1] !== null), 0);
      return count ? [{ control, actor: `${experiment} · ego command`, experiment, rows }] : [];
    });
    return [control, series];
  })), [traces]);
  const availableControlKeys = useMemo(() => (Object.keys(controlDefinitions) as ControlKey[]).filter((control) => (controlSeriesByType[control] ?? []).length > 0), [controlSeriesByType]);
  useEffect(() => {
    setActiveControlMetrics((current) => {
      const valid = current.filter((control) => availableControlKeys.includes(control));
      if (valid.length || !availableControlKeys.length) return valid;
      return [availableControlKeys.includes('throttle_command') ? 'throttle_command' : availableControlKeys.includes('ackermann_speed_target') ? 'ackermann_speed_target' : availableControlKeys[0]];
    });
  }, [availableControlKeys.join('\0')]);
  const replayComparison = useMemo(() => {
    if (replayAnalysisMode !== 'compare' || !compareLeft || !compareRight || compareLeft === compareRight) return undefined;
    const metricRows = stateMetricKeys.flatMap((metric) => {
      const entries = metricSeriesByType[metric] ?? [];
      const left = entries.find((entry) => entry.experiment === compareLeft && /ego/i.test(entry.actor));
      const right = entries.find((entry) => entry.experiment === compareRight && /ego/i.test(entry.actor));
      if (!left || !right) return [];
      const rows = alignedDirectionalDelta(left.selected.rows, right.selected.rows);
      const summary = summarizeDelta(rows);
      return summary ? [{ category: 'Metric' as const, key: metric, label: metricDefinitions[metric].label, unit: metricDefinitions[metric].unit, rows, summary, leftCount: left.selected.count, rightCount: right.selected.count }] : [];
    });
    const controlRows = (Object.keys(controlDefinitions) as ControlKey[]).flatMap((control) => {
      const entries = controlSeriesByType[control] ?? [];
      const left = entries.find((entry) => entry.experiment === compareLeft);
      const right = entries.find((entry) => entry.experiment === compareRight);
      if (!left || !right) return [];
      const rows = alignedDirectionalDelta(left.rows, right.rows);
      const summary = summarizeDelta(rows);
      const leftCount = left.rows.reduce((count, row) => count + Number(row[1] !== null), 0);
      const rightCount = right.rows.reduce((count, row) => count + Number(row[1] !== null), 0);
      return summary ? [{ category: 'Control' as const, key: control, label: controlDefinitions[control].label, unit: controlDefinitions[control].unit, rows, summary, leftCount, rightCount }] : [];
    });
    const egoPosition = (experiment: string) => Object.entries(traces)
      .filter(([channel, points]) => channel.startsWith(`${experiment} · `) && !/(metric|control|bounding|geometry|goal|current)/i.test(channel) && points.some((point) => finiteNumber(point.x) !== undefined && finiteNumber(point.y) !== undefined))
      .sort(([leftName, leftPoints], [rightName, rightPoints]) => {
        const score = (name: string, points: ReplayPoint[]) => Number(/ego/i.test(name)) * 10 + Number(points.some((point) => point.values?.is_ego === true || String(point.values?.is_ego).toLowerCase() === 'true')) * 10 + points.length / 1_000_000;
        return score(rightName, rightPoints) - score(leftName, leftPoints);
      })[0]?.[1] ?? [];
    const leftTrajectory = egoPosition(compareLeft);
    const rightTrajectory = egoPosition(compareRight);
    const leftPositions = new Map(leftTrajectory.flatMap((point) => finiteNumber(point.x) === undefined || finiteNumber(point.y) === undefined ? [] : [[normalizedTraceTime(point.time), [point.x!, point.y!] as const] as const]));
    const rightPositions = new Map(rightTrajectory.flatMap((point) => finiteNumber(point.x) === undefined || finiteNumber(point.y) === undefined ? [] : [[normalizedTraceTime(point.time), [point.x!, point.y!] as const] as const]));
    const trajectoryRows: DeltaRow[] = [...leftPositions.keys()].filter((rowTime) => rightPositions.has(rowTime)).sort((a, b) => a - b).map((rowTime) => {
      const left = leftPositions.get(rowTime)!, right = rightPositions.get(rowTime)!;
      return [rowTime, Math.hypot(right[0] - left[0], right[1] - left[1])];
    });
    const trajectorySummary = summarizeDelta(trajectoryRows);
    return { left: compareLeft, right: compareRight, metricRows, controlRows, trajectoryRows, trajectorySummary, trajectoryFde: trajectoryRows.at(-1), leftTrajectoryCount: leftPositions.size, rightTrajectoryCount: rightPositions.size };
  }, [compareLeft, compareRight, controlSeriesByType, metricSeriesByType, replayAnalysisMode, traces]);
  const buildTrajectory = useCallback((frameTime: number): VisualizationSpec => {
    const series: Array<Record<string, unknown>> = [];
    const focusedTrace = positionalTraces.find((trace) => trace.name === focusActor);
    const focusedState = focusedTrace ? [...focusedTrace.points].reverse().find((point) => point.time <= frameTime && finiteNumber(point.x) !== undefined && finiteNumber(point.y) !== undefined) : undefined;
    const focusedYawState = focusedTrace ? [...focusedTrace.points].reverse().find((point) => point.time <= frameTime && finiteNumber(point.yaw) !== undefined) : undefined;
    const focusedPosition: [number, number] | undefined = focusedState ? [focusedState.x!, focusedState.y!] : undefined;
    const rawFocusedYaw = finiteNumber(focusedYawState?.yaw);
    const focusedYaw = rawFocusedYaw === undefined ? undefined : Math.abs(rawFocusedYaw) > Math.PI * 2 ? rawFocusedYaw * Math.PI / 180 : rawFocusedYaw;
    const egoCentric = Boolean(focusedPosition && focusedYaw !== undefined && focusView === 'ego-centric');
    const transformPoint = (point: [number, number]): [number, number] => {
      if (!egoCentric || !focusedPosition || focusedYaw === undefined) return point;
      const dx = point[0] - focusedPosition[0], dy = point[1] - focusedPosition[1];
      // Screen x is actor-right and screen y is actor-forward. This is a rigid
      // rotation/translation, so distances and vehicle geometry stay exact.
      return [Math.sin(focusedYaw) * dx - Math.cos(focusedYaw) * dy, Math.cos(focusedYaw) * dx + Math.sin(focusedYaw) * dy];
    };
    const mapPolyline = replay?.map?.polyline?.filter((point) => finiteNumber(point[0]) !== undefined && finiteNumber(point[1]) !== undefined);
    if (showMap && mapPolyline?.length) series.push({ type: 'line', name: replay?.map?.name ? `Map · ${replay.map.name}` : 'Recorded map path', data: mapPolyline.map(transformPoint), showSymbol: false, silent: true, lineStyle: { width: 1, color: '#c8ceda', type: 'dashed' }, z: 0 });
    for (const road of showMap ? replay?.map?.geometry?.roads ?? [] : []) {
      if (!mapJunctions && road.junction) continue;
      if (mapReference && road.reference_line?.length) series.push({ type: 'line', name: `Road ${road.road_id ?? ''} reference`, data: road.reference_line.map(transformPoint), showSymbol: false, silent: true, lineStyle: { width: road.junction ? 1.6 : 1, color: road.junction ? '#f59f00' : '#9aa4b2', type: 'dashed' }, z: 0 });
      if (mapBoundaries) road.boundaries?.forEach((boundary, boundaryIndex) => { if (boundary.length) series.push({ type: 'line', name: `Road ${road.road_id ?? ''} lane ${boundaryIndex + 1}`, data: boundary.map(transformPoint), showSymbol: false, silent: true, lineStyle: { width: 1.1, color: '#7c8798' }, z: 0 }); });
    }
    const goalX = finiteNumber(replay?.ego_goal?.x), goalY = finiteNumber(replay?.ego_goal?.y);
    if (showGoal && goalX !== undefined && goalY !== undefined) {
      const circle = Array.from({ length: 49 }, (_, index) => { const angle = index / 48 * Math.PI * 2; return transformPoint([goalX + Math.cos(angle) * 2, goalY + Math.sin(angle) * 2]); });
      const horizontal = [transformPoint([goalX - 2, goalY]), transformPoint([goalX + 2, goalY])];
      const vertical = [transformPoint([goalX, goalY - 2]), transformPoint([goalX, goalY + 2])];
      for (const data of [circle, horizontal, vertical]) series.push({ type: 'line', name: 'Ego goal', data, showSymbol: false, silent: true, lineStyle: { color: '#111827', width: 2 }, z: 8 });
    }
    let collisionCount = 0;
    if (showCollisionPositions) {
      for (const event of events.filter((item) => item.time <= frameTime && /collision|contact|impact/i.test(`${item.type} ${item.label}`))) {
        const collisionX = finiteNumber(event.x) ?? finiteNumber(event.details?.x) ?? finiteNumber(event.details?.collision_x);
        const collisionY = finiteNumber(event.y) ?? finiteNumber(event.details?.y) ?? finiteNumber(event.details?.collision_y);
        if (collisionX === undefined || collisionY === undefined) continue;
        const radius = 1.5;
        const diagonals = [
          [transformPoint([collisionX - radius, collisionY - radius]), transformPoint([collisionX + radius, collisionY + radius])],
          [transformPoint([collisionX - radius, collisionY + radius]), transformPoint([collisionX + radius, collisionY - radius])],
        ];
        for (const data of diagonals) series.push({ type: 'line', name: 'Collision positions', data, showSymbol: false, silent: true, lineStyle: { color: '#ff006e', width: 3 }, z: 10 });
        collisionCount += 1;
      }
    }
    positionalTraces.forEach(({ name, points }) => {
      const reached = points.filter((point) => point.time <= frameTime);
      const displayed = trailOnly ? reached : points;
      const stride = Math.max(1, Math.ceil(displayed.length / 900));
      const trail = displayed.filter((_, pointIndex) => pointIndex % stride === 0 || pointIndex === displayed.length - 1).map((point) => transformPoint([point.x!, point.y!]));
      const worldPosition = positionAtSortedPoints(points, frameTime);
      const position = worldPosition ? transformPoint(worldPosition) : undefined;
      const color = actorColors[name] ?? actorColor(name);
      if (trail.length) series.push({ type: 'line', name, data: trail, showSymbol: false, lineStyle: { width: /ego/i.test(name) ? 3 : 1.7, color, opacity: /ego/i.test(name) ? 1 : 0.78 }, z: /ego/i.test(name) ? 3 : 2 });
      if (position) series.push({ type: 'scatter', name, data: [position], symbolSize: /ego/i.test(name) ? 13 : 9, itemStyle: { color, borderColor: '#fff', borderWidth: 2 }, tooltip: { formatter: `${name}<br/>t = ${frameTime.toFixed(3)} s<br/>x = ${position[0].toFixed(2)} m<br/>y = ${position[1].toFixed(2)} m` }, z: 5 });
      if (position && showBoundingBoxes) {
        const yawRecord = [...reached].reverse().find((point) => finiteNumber(point.yaw) !== undefined);
        const shape = geometry.find((item) => [item.agent_id, item.entity_name].some((value) => value && name.toLowerCase().includes(String(value).toLowerCase())));
        const length = finiteNumber(shape?.length_m) ?? 4.5, width = finiteNumber(shape?.width_m) ?? 1.9;
        const rawYaw = finiteNumber(yawRecord?.yaw) ?? 0, stateYaw = Math.abs(rawYaw) > Math.PI * 2 ? rawYaw * Math.PI / 180 : rawYaw;
        const rawYawOffset = finiteNumber(shape?.yaw_offset) ?? 0, yawOffset = Math.abs(rawYawOffset) > Math.PI * 2 ? rawYawOffset * Math.PI / 180 : rawYawOffset;
        const centerOffsetX = finiteNumber(shape?.center_offset_x) ?? 0, centerOffsetY = finiteNumber(shape?.center_offset_y) ?? 0;
        const centerX = worldPosition![0] + centerOffsetX * Math.cos(stateYaw) - centerOffsetY * Math.sin(stateYaw);
        const centerY = worldPosition![1] + centerOffsetX * Math.sin(stateYaw) + centerOffsetY * Math.cos(stateYaw);
        const yaw = stateYaw + yawOffset;
        const corners = [[length / 2, width / 2], [length / 2, -width / 2], [-length / 2, -width / 2], [-length / 2, width / 2], [length / 2, width / 2]].map(([dx, dy]) => transformPoint([centerX + dx * Math.cos(yaw) - dy * Math.sin(yaw), centerY + dx * Math.sin(yaw) + dy * Math.cos(yaw)]));
        series.push({ type: 'line', name, data: corners, showSymbol: false, silent: true, lineStyle: { width: 2, color }, z: 6 });
      }
    });
    const visiblePositions = positionalTraces.flatMap(({ points }) => (followCursor ? points.filter((point) => point.time <= frameTime).slice(-1) : points).map((point) => transformPoint([point.x!, point.y!])));
    if (showGoal && goalX !== undefined && goalY !== undefined) visiblePositions.push(transformPoint([goalX, goalY]));
    const xs = visiblePositions.map((point) => point[0]), ys = visiblePositions.map((point) => point[1]);
    const xMin = xs.length ? Math.min(...xs) : undefined, xMax = xs.length ? Math.max(...xs) : undefined, yMin = ys.length ? Math.min(...ys) : undefined, yMax = ys.length ? Math.max(...ys) : undefined;
    // The ECharts grid occupies 84% of the canvas width and 86% of its height.
    // Matching the data-span ratio to that physical plot area keeps rotated
    // rectangles rectangular instead of turning them into parallelograms.
    const chartRatio = (16 / 9) * (0.84 / 0.86), rawXSpan = Math.max((xMax ?? 0) - (xMin ?? 0), followCursor ? 30 : 10), rawYSpan = Math.max((yMax ?? 0) - (yMin ?? 0), followCursor ? 18 : 6);
    const xSpan = Math.max(rawXSpan, rawYSpan * chartRatio) * 1.12, ySpan = xSpan / chartRatio;
    const xCenter = ((xMin ?? 0) + (xMax ?? 0)) / 2, yCenter = ((yMin ?? 0) + (yMax ?? 0)) / 2;
    const manualXMin = finiteNumber(viewportXMin), manualXMax = finiteNumber(viewportXMax), manualYMin = finiteNumber(viewportYMin), manualYMax = finiteNumber(viewportYMax);
    const useManual = manualViewport && manualXMin !== undefined && manualXMax !== undefined && manualYMin !== undefined && manualYMax !== undefined && manualXMin < manualXMax && manualYMin < manualYMax;
    const focusRadius = 25;
    const cameraCenter = focusedPosition ? transformPoint(focusedPosition) : undefined;
    const rawXAxisMin = cameraCenter ? cameraCenter[0] - focusRadius * chartRatio : useManual ? manualXMin : xCenter - xSpan / 2;
    const rawXAxisMax = cameraCenter ? cameraCenter[0] + focusRadius * chartRatio : useManual ? manualXMax : xCenter + xSpan / 2;
    const rawYAxisMin = cameraCenter ? cameraCenter[1] - focusRadius : useManual ? manualYMin : yCenter - ySpan / 2;
    const rawYAxisMax = cameraCenter ? cameraCenter[1] + focusRadius : useManual ? manualYMax : yCenter + ySpan / 2;
    // Automatic replay viewports expand outwards to whole metres. Manual
    // bounds remain exact because they are an explicit user instruction.
    const xAxisMin = useManual ? rawXAxisMin : Math.floor(rawXAxisMin);
    const xAxisMax = useManual ? rawXAxisMax : Math.ceil(rawXAxisMax);
    const yAxisMin = useManual ? rawYAxisMin : Math.floor(rawYAxisMin);
    const yAxisMax = useManual ? rawYAxisMax : Math.ceil(rawYAxisMax);
    const legendNames = [...positionalTraces.map((trace) => trace.name), ...(showGoal && goalX !== undefined ? ['Ego goal'] : []), ...(collisionCount ? ['Collision positions'] : [])];
    const frameIndex = Math.max(0, timeline.findIndex((value) => value === frameTime));
    return {
      id: `trajectory-${runId}`, title: 'Synchronized trajectory', subtitle: `Cursor uses recorded step ${frameIndex + 1} / ${timeline.length} at ${frameTime.toFixed(3)} s; ${egoCentric ? `${focusActor} is fixed at center with forward pointing up.` : focusView === 'ego-centric' && focusActor ? 'Focus yaw is unavailable at this step; using centered north-up view.' : 'no intermediate states are invented.'}`, kind: 'trajectory',
      option: series.length ? { animation: false, color: legendNames.map((name) => actorColors[name] ?? actorColor(name)), legend: { type: 'plain', data: legendNames }, tooltip: { trigger: 'item' }, grid: { top: '4%', right: '8%', bottom: '10%', left: '8%' }, xAxis: { type: 'value', show: showTrajectoryAxes, name: egoCentric ? 'right (m)' : 'x (m)', nameLocation: 'middle', nameGap: 38, scale: true, min: xAxisMin, max: xAxisMax, axisLabel: { formatter: formatAxisTick }, splitLine: { show: showTrajectoryGrid } }, yAxis: { type: 'value', show: showTrajectoryAxes, name: egoCentric ? 'forward (m)' : 'y (m)', nameLocation: 'middle', nameGap: 48, scale: true, min: yAxisMin, max: yAxisMax, axisLabel: { formatter: formatAxisTick }, splitLine: { show: showTrajectoryGrid } }, series } : {},
    };
  }, [actorColors, events, focusActor, focusView, followCursor, geometry, manualViewport, mapBoundaries, mapJunctions, mapReference, positionalTraces, replay?.ego_goal, replay?.map, runId, showBoundingBoxes, showCollisionPositions, showGoal, showMap, showTrajectoryAxes, showTrajectoryGrid, timeline, trailOnly, viewportXMax, viewportXMin, viewportYMax, viewportYMin]);
  const trajectory = useMemo(() => buildTrajectory(currentTime), [buildTrajectory, currentTime]);
  const trajectoryAnimationOptionAtProgress = useCallback((progress: number) => {
    const index = Math.min(timeline.length - 1, Math.max(0, Math.round(progress * Math.max(0, timeline.length - 1))));
    return buildTrajectory(timeline[index] ?? currentTime).option;
  }, [buildTrajectory, currentTime, timeline]);
  const metricCharts = useMemo(() => {
    const cursorSeries = {
      type: 'line', name: '__replay_cursor__', data: [], silent: true, tooltip: { show: false },
      markLine: { silent: true, symbol: 'none', label: { formatter: `${currentTime.toFixed(2)} s` }, lineStyle: { color: '#182033', type: 'dashed', width: 1.5 }, data: [{ xAxis: currentTime }] },
    };
    const buildStateChart = (metrics: MetricKey[]): VisualizationSpec => {
      const rows = metrics.flatMap((metric) => (metricSeriesByType[metric] ?? []).map((entry) => ({ ...entry, metric })));
      const legendNames = rows.map(({ actor, metric }) => `${actor} · ${metricDefinitions[metric].label}`);
      const lineTypes = ['solid', 'dashed', 'dotted'] as const;
      return {
        id: `replay-metrics-${runId}`, title: 'Metrics over time',
        subtitle: rows.length ? `${rows.length} visible actor/category traces · recorded gaps remain missing.` : 'No currently visible actor exposes an enabled category.',
        kind: 'line',
        option: rows.length ? {
          animation: false,
          legend: { type: 'plain', data: legendNames, top: 0 },
          tooltip: { trigger: 'axis' },
          grid: { top: 58, right: 92, bottom: 52, left: 76 },
          xAxis: { type: 'value', name: 'Time (s)', min: timeDomain.minimum, max: timeDomain.maximum },
          series: [
            ...rows.map(({ actor, metric, selected }) => ({
              type: 'line', name: `${actor} · ${metricDefinitions[metric].label}`, data: boundedSeries(selected.rows), connectNulls: false, showSymbol: false,
              yAxisIndex: ['x', 'y', 'distance', 'clearance'].includes(metric) ? 0 : metric === 'speed' ? 1 : ['acceleration', 'drac'].includes(metric) ? 2 : 3,
              lineStyle: { width: 2, color: actorColors[actor] ?? actorColor(actor), type: lineTypes[metrics.indexOf(metric) % lineTypes.length] },
            })),
            cursorSeries,
          ], yAxis: [
            { type: 'value', name: 'Position / distance (m)', scale: true, splitLine: { lineStyle: { color: '#edf0f5' } } },
            { type: 'value', name: 'Speed (m/s)', scale: true, position: 'right', splitLine: { show: false } },
            { type: 'value', name: 'Acceleration / DRAC (m/s²)', scale: true, position: 'right', offset: 54, splitLine: { show: false } },
            { type: 'value', name: 'Time metric (s)', scale: true, position: 'left', offset: 54, splitLine: { show: false } },
          ],
        } : {},
      };
    };
    const controlRows = activeControlMetrics.flatMap((control) => (controlSeriesByType[control] ?? []).map((entry) => ({ ...entry, control })));
    const controlLegend = controlRows.map(({ actor, control }) => `${actor} · ${controlDefinitions[control].label}`);
    const controls: VisualizationSpec = {
      id: `replay-controls-${runId}`, title: 'Control commands over time', subtitle: controlRows.length ? `${controlRows.length} ego command traces · measured vehicle speed is intentionally excluded.` : 'No enabled command is recorded by the selected experiments.', kind: 'line',
      option: controlRows.length ? { animation: false, legend: { type: 'plain', data: controlLegend, top: 0 }, tooltip: { trigger: 'axis' }, grid: { top: 58, right: 110, bottom: 52, left: 82 }, xAxis: { type: 'value', name: 'Time (s)', min: timeDomain.minimum, max: timeDomain.maximum }, yAxis: [
        { type: 'value', name: 'Normalized command', scale: true, splitLine: { lineStyle: { color: '#edf0f5' } } },
        { type: 'value', name: 'Speed target (m/s)', scale: true, position: 'right', splitLine: { show: false } },
        { type: 'value', name: 'Acceleration / jerk target', scale: true, position: 'right', offset: 54, splitLine: { show: false } },
        { type: 'value', name: 'Steering target', scale: true, position: 'left', offset: 54, splitLine: { show: false } },
      ], series: [...controlRows.map(({ actor, control, rows }) => ({ type: 'line', name: `${actor} · ${controlDefinitions[control].label}`, data: boundedSeries(rows), connectNulls: false, showSymbol: false, yAxisIndex: control === 'ackermann_speed_target' ? 1 : ['ackermann_acceleration_target', 'ackermann_jerk'].includes(control) ? 2 : ['ackermann_steer_target', 'ackermann_steering_angle', 'ackermann_steering_angle_velocity'].includes(control) ? 3 : 0, step: ['throttle_command', 'brake_command', 'steer_command'].includes(control) ? 'end' : false, lineStyle: { width: 2, color: actorColors[actor] ?? actorColor(actor) } })), cursorSeries] } : {},
    };
    return {
      metrics: buildStateChart(activeStateMetrics), controls,
    };
  }, [activeControlMetrics, activeStateMetrics, actorColors, controlSeriesByType, currentTime, metricSeriesByType, runId, timeDomain.maximum, timeDomain.minimum]);
  const comparisonCharts = useMemo(() => {
    const cursorAndZero = {
      type: 'line', name: '__comparison_reference__', data: [], silent: true, tooltip: { show: false },
      markLine: { silent: true, symbol: 'none', label: { show: false }, data: [{ yAxis: 0, lineStyle: { color: '#8791a5', width: 1 } }, { xAxis: currentTime, lineStyle: { color: '#182033', type: 'dashed', width: 1.5 }, label: { show: true, formatter: `${currentTime.toFixed(2)} s` } }] },
    };
    const metricRows = replayComparison?.metricRows.filter((row) => activeStateMetrics.includes(row.key as MetricKey)) ?? [];
    const controlRows = replayComparison?.controlRows.filter((row) => activeControlMetrics.includes(row.key as ControlKey)) ?? [];
    const metricSpec: VisualizationSpec = {
      id: `replay-delta-metrics-${runId}`, title: 'Delta metrics over time', subtitle: replayComparison ? `${replayComparison.right} − ${replayComparison.left}; positive values mean the right experiment recorded a larger value.` : 'Choose two experiments.', kind: 'line',
      option: metricRows.length ? { animation: false, legend: { type: 'plain', data: metricRows.map((row) => `Δ ${row.label}`), top: 0 }, tooltip: { trigger: 'axis' }, grid: { top: 58, right: 100, bottom: 52, left: 82 }, xAxis: { type: 'value', name: 'Time (s)', min: timeDomain.minimum, max: timeDomain.maximum }, yAxis: [
        { type: 'value', name: 'Δ position / distance (m)', scale: true, splitLine: { lineStyle: { color: '#edf0f5' } } },
        { type: 'value', name: 'Δ speed (m/s)', scale: true, position: 'right', splitLine: { show: false } },
        { type: 'value', name: 'Δ acceleration (m/s²)', scale: true, position: 'right', offset: 48, splitLine: { show: false } },
        { type: 'value', name: 'Δ time metric (s)', scale: true, position: 'left', offset: 48, splitLine: { show: false } },
      ], series: [...metricRows.map((row, index) => ({ type: 'line', name: `Δ ${row.label}`, data: boundedSeries(row.rows), showSymbol: false, connectNulls: false, yAxisIndex: ['x', 'y', 'distance', 'clearance'].includes(row.key) ? 0 : row.key === 'speed' ? 1 : ['acceleration', 'drac'].includes(row.key) ? 2 : 3, lineStyle: { width: 2, color: categoricalContrastPalette[index % categoricalContrastPalette.length] } })), cursorAndZero] } : {},
    };
    const controlSpec: VisualizationSpec = {
      id: `replay-delta-controls-${runId}`, title: 'Delta control commands over time', subtitle: replayComparison ? `${replayComparison.right} − ${replayComparison.left}; measured speed is not mixed with speed targets.` : 'Choose two experiments.', kind: 'line',
      option: controlRows.length ? { animation: false, legend: { type: 'plain', data: controlRows.map((row) => `Δ ${row.label}`), top: 0 }, tooltip: { trigger: 'axis' }, grid: { top: 58, right: 110, bottom: 52, left: 82 }, xAxis: { type: 'value', name: 'Time (s)', min: timeDomain.minimum, max: timeDomain.maximum }, yAxis: [
        { type: 'value', name: 'Δ normalized command', scale: true, splitLine: { lineStyle: { color: '#edf0f5' } } },
        { type: 'value', name: 'Δ speed target (m/s)', scale: true, position: 'right', splitLine: { show: false } },
        { type: 'value', name: 'Δ acceleration / jerk', scale: true, position: 'right', offset: 48, splitLine: { show: false } },
        { type: 'value', name: 'Δ steering target', scale: true, position: 'left', offset: 48, splitLine: { show: false } },
      ], series: [...controlRows.map((row, index) => ({ type: 'line', name: `Δ ${row.label}`, data: boundedSeries(row.rows), showSymbol: false, connectNulls: false, yAxisIndex: row.key === 'ackermann_speed_target' ? 1 : ['ackermann_acceleration_target', 'ackermann_jerk'].includes(row.key) ? 2 : ['ackermann_steer_target', 'ackermann_steering_angle', 'ackermann_steering_angle_velocity'].includes(row.key) ? 3 : 0, step: ['throttle_command', 'brake_command', 'steer_command'].includes(row.key) ? 'end' : false, lineStyle: { width: 2, color: categoricalContrastPalette[index % categoricalContrastPalette.length] } })), cursorAndZero] } : {},
    };
    return { metrics: metricSpec, controls: controlSpec };
  }, [activeControlMetrics, activeStateMetrics, currentTime, replayComparison, runId, timeDomain.maximum, timeDomain.minimum]);
  const reachedEvents = events.filter((event) => event.time <= currentTime).slice(-100);
  const nextEvent = events.find((event) => event.time > currentTime);
  const previousEvent = [...events].reverse().find((event) => event.time < currentTime - 1e-9);
  const manualWidth = typeof viewportXMin === 'number' && typeof viewportXMax === 'number' ? viewportXMax - viewportXMin : undefined;
  const manualHeight = typeof viewportYMin === 'number' && typeof viewportYMax === 'number' ? viewportYMax - viewportYMin : undefined;
  const trajectoryAspectRatio = manualViewport && !focusActor && manualWidth && manualHeight && manualWidth > 0 && manualHeight > 0 ? `${manualWidth / manualHeight * 0.86 / 0.84} / 1` : '16 / 9';
  const jumpToComparisonTime = (target: number) => {
    if (!timeline.length) return;
    const nearest = timeline.reduce((best, value) => Math.abs(value - target) < Math.abs(best - target) ? value : best, timeline[0]);
    setPlaying(false);
    setTime(nearest);
  };
  const replayComparisonRows = replayComparison ? [
    ...(replayComparison.trajectorySummary ? [{ category: 'Trajectory', key: 'trajectory_distance', label: 'Ego trajectory distance', unit: 'm', rows: replayComparison.trajectoryRows, summary: replayComparison.trajectorySummary, leftCount: replayComparison.leftTrajectoryCount, rightCount: replayComparison.rightTrajectoryCount }] : []),
    ...replayComparison.metricRows,
    ...replayComparison.controlRows,
  ] : [];
  if (!runId) return <Card><EmptyState title="Choose a run to replay" description="Open a run from the Runs tab. States, controls, metrics, events, and map geometry load only for that selected case." action={<Button onClick={onChoose}>Browse runs</Button>} icon={<IconPlayerPlay size={23} />} /></Card>;
  if (detail.isLoading) return <PageLoading label="Loading selected run traces…" />;
  if (detail.error) return <InlineError error={detail.error} onRetry={() => detail.refetch()} />;
  if (!detail.data) return null;
  return (
    <Stack gap="lg">
      <Card p="lg" withBorder>
        <Group justify="space-between" align="flex-start" mb="md"><div><Text fw={700} size="lg">Concrete scenario parameters</Text><Text size="sm" c="dimmed">Exact parameter values used to generate this sample. Paired experiments are grouped by the recorded comparison key.</Text></div><Badge variant="light" color="indigo">{Object.keys(detail.data.run.parameters ?? {}).length} parameters</Badge></Group>
        {Object.keys(detail.data.run.parameters ?? {}).length ? <SimpleGrid cols={{ base: 1, sm: 2, lg: 4 }}>{Object.entries(detail.data.run.parameters ?? {}).map(([name, value]) => <Card key={name} p="sm" bg="gray.0" withBorder><Text size="xs" c="dimmed" fw={600}>{name}</Text><Text fw={700} className="pisa-code" mt={3}>{typeof value === 'number' ? value.toLocaleString(undefined, { maximumSignificantDigits: 10 }) : String(value ?? '—')}</Text></Card>)}</SimpleGrid> : <Text size="sm" c="dimmed">No concrete parameter values were recorded for this run.</Text>}
      </Card>
      <Group justify="space-between">
        <Group><Text fw={650}>Parameter sample</Text><Code>{replay?.navigation?.sample_key?.value ?? detail.data.run.parameter_hash ?? detail.data.run.sample_id ?? detail.data.run.scenario_id ?? detail.data.run.id}</Code>{selectedCases.map((item) => <Badge key={item.run.id} variant="light" color={item.run.outcome === 'success' ? 'green' : item.run.outcome === 'fail' ? 'red' : 'yellow'}>{item.run.experiment}: {item.run.outcome}</Badge>)}{replay?.navigation?.ordinal && <Badge variant="outline">sample {replay.navigation.ordinal} / {replay.navigation.total}</Badge>}</Group>
        <Group gap="xs"><Button size="compact-sm" variant="subtle" disabled={!replay?.navigation?.previous_run_id} onClick={() => replay?.navigation?.previous_run_id && onOpen(replay.navigation.previous_run_id)}>Previous sample</Button><Button size="compact-sm" variant="subtle" disabled={!replay?.navigation?.next_run_id} onClick={() => replay?.navigation?.next_run_id && onOpen(replay.navigation.next_run_id)}>Next sample</Button></Group>
      </Group>
      {mediaMutation.error && <InlineError error={mediaMutation.error} />}
      {mediaMutation.data && <Alert color={mediaJob.data?.state === 'failed' ? 'red' : mediaJob.data?.state === 'succeeded' ? 'teal' : 'blue'} title={`Render ${mediaJob.data?.state ?? 'queued'}`}><Stack gap={6}><Text size="sm">{mediaJob.data?.message ?? mediaJob.data?.phase ?? 'The render job has started.'}</Text>{['queued', 'running'].includes(mediaJob.data?.state ?? 'queued') && <Progress animated value={mediaJob.data?.progress?.total ? mediaJob.data.progress.current / mediaJob.data.progress.total * 100 : mediaJob.data?.state === 'running' ? 45 : 8} />}{mediaJob.data?.progress && <Text size="xs">{mediaJob.data.progress.current}{mediaJob.data.progress.total ? ` / ${mediaJob.data.progress.total}` : ''} {mediaJob.data.progress.unit ?? ''}</Text>}</Stack></Alert>}
      <Card p="md"><Group justify="space-between" align="flex-end"><div><Text fw={650} size="sm">Schematic and synchronized-view export</Text><Text size="xs" c="dimmed">Uses the checked experiments, actors, OpenDRIVE layers, viewport and trajectory settings.</Text></div><Group align="flex-end"><Select label="View" value={mediaMode} onChange={setMediaMode} allowDeselect={false} data={[{ value: 'standard', label: 'Standard schematic' }, { value: 'trajectory_view', label: 'Current trajectory view' }]} w={200} /><Select label="Timing" value={mediaTiming} onChange={setMediaTiming} allowDeselect={false} data={[{ value: 'realtime', label: `Recorded time · ${playbackRate}×` }, { value: 'frame_budget', label: 'Frame budget (legacy)' }]} w={190} /><Select label="Format" value={mediaFormat} onChange={setMediaFormat} data={[{ value: 'gif', label: 'GIF' }, { value: 'mp4', label: 'MP4' }, { value: 'webm', label: 'WebM' }, { value: 'png', label: 'PNG keyframe' }]} allowDeselect={false} w={120} />{mediaTiming === 'frame_budget' && <><NumberInput label="FPS" value={mediaFps} onChange={setMediaFps} min={1} max={60} w={85} /><NumberInput label="Max frames" value={mediaFrames} onChange={setMediaFrames} min={2} max={2000} w={110} /></>}<Button size="md" variant="light" loading={mediaMutation.isPending} onClick={() => mediaMutation.mutate()} leftSection={<IconMovie size={17} />}>Render</Button></Group></Group></Card>
      <Card p="md"><Stack gap="md"><Group align="flex-end" wrap="wrap"><Select label="Replay analysis" value={replayAnalysisMode} onChange={setReplayAnalysisMode} allowDeselect={false} data={[{ value: 'overlay', label: 'Overlay mode' }, { value: 'compare', label: 'Compare two experiments', disabled: availableExperiments.length < 2 }]} w={220} />{replayAnalysisMode === 'compare' && <><Select label="Left experiment" searchable value={compareLeft} onChange={(value) => { setCompareLeft(value); if (value === compareRight) setCompareRight(availableExperiments.find((experiment) => experiment !== value) ?? null); }} allowDeselect={false} data={availableExperiments} style={{ flex: '1 1 240px' }} /><Select label="Right experiment" searchable value={compareRight} onChange={setCompareRight} allowDeselect={false} data={availableExperiments.filter((experiment) => experiment !== compareLeft)} style={{ flex: '1 1 240px' }} /></>}</Group>{replayAnalysisMode === 'compare' && <Alert color="blue" icon={<IconShieldCheck size={17} />} title="Directional comparison">All deltas are Right − Left and use only timestamps recorded by both experiments. Missing points are excluded, never filled or interpolated.</Alert>}<Divider /><Group justify="space-between"><div><Text fw={650} size="sm">Experiments in synchronized trajectory</Text><Text size="xs" c="dimmed">{replayAnalysisMode === 'compare' ? 'The selected left and right experiments are overlaid and included in export.' : 'Every checked experiment is overlaid for this same parameter sample and is included in export.'}</Text></div>{replayAnalysisMode !== 'compare' && <Group><Button size="compact-xs" variant="default" onClick={() => setSelectedExperiments(availableExperiments)}>Select all</Button><Button size="compact-xs" variant="default" onClick={() => setSelectedExperiments([])}>Select none</Button></Group>}</Group><Group>{availableExperiments.map((experimentName) => <Checkbox key={experimentName} label={experimentName} checked={selectedExperiments.includes(experimentName)} disabled={replayAnalysisMode === 'compare'} onChange={(event) => setSelectedExperiments((current) => event.currentTarget.checked ? [...current, experimentName] : current.filter((value) => value !== experimentName))} />)}</Group>{pairedQueriesPending && <Group gap="xs"><Progress animated value={55} style={{ flex: 1 }} /><Text size="xs" c="dimmed">Loading selected experiment traces…</Text></Group>}<Divider /><Group justify="space-between"><div><Text fw={650} size="sm">Actors shown</Text><Text size="xs" c="dimmed">Agents use recorded entity names and can be controlled independently.</Text></div><Group><Button size="compact-xs" variant="default" onClick={() => setSelectedActors(availableActors)}>Select all</Button><Button size="compact-xs" variant="default" onClick={() => setSelectedActors([])}>Select none</Button><Checkbox label="Ego goal" checked={showGoal} disabled={!replay?.ego_goal} onChange={(event) => setShowGoal(event.currentTarget.checked)} /></Group></Group><Group>{availableActors.map((actor) => <Checkbox key={actor} label={actor} checked={selectedActors.includes(actor)} onChange={(event) => setSelectedActors((current) => event.currentTarget.checked ? [...current, actor] : current.filter((value) => value !== actor))} />)}</Group>{!replay?.ego_goal && replay?.ego_goal_warning && <Text size="xs" c="yellow">{replay.ego_goal_warning}</Text>}</Stack></Card>
      <div className="pisa-trajectory-layout">
        <Stack gap="sm" className="pisa-trajectory-sidebar">
          <Card p="md"><Text fw={650} size="sm" mb="sm">OpenDRIVE layers</Text><Stack gap={7}><Checkbox label="Show map" checked={showMap} onChange={(event) => setShowMap(event.currentTarget.checked)} /><Checkbox label="Reference line" checked={mapReference} onChange={(event) => setMapReference(event.currentTarget.checked)} /><Checkbox label="Lane lines" checked={mapBoundaries} onChange={(event) => setMapBoundaries(event.currentTarget.checked)} /><Checkbox label="Junctions" checked={mapJunctions} onChange={(event) => setMapJunctions(event.currentTarget.checked)} /></Stack></Card>
          <Card p="md"><Text fw={650} size="sm" mb="sm">Trajectory display</Text><Stack gap={7}><Select label="Focus agent · 25 m BEV" placeholder="Full trajectory view" clearable searchable value={focusActor} onChange={setFocusActor} data={selectedActors.map((actor) => ({ value: actor, label: actor }))} /><Select label="Focus orientation" value={focusView} onChange={setFocusView} allowDeselect={false} disabled={!focusActor} data={[{ value: 'centered', label: 'Centered · north-up' }, { value: 'ego-centric', label: 'Ego-centric · forward-up' }]} /><Checkbox label="Follow cursor" checked={followCursor} onChange={(event) => setFollowCursor(event.currentTarget.checked)} /><Checkbox label="Trail only" checked={trailOnly} onChange={(event) => setTrailOnly(event.currentTarget.checked)} /><Checkbox label="Bounding boxes" checked={showBoundingBoxes} onChange={(event) => setShowBoundingBoxes(event.currentTarget.checked)} /><Checkbox label="Collision positions" checked={showCollisionPositions} onChange={(event) => setShowCollisionPositions(event.currentTarget.checked)} /><Checkbox label="Axes" checked={showTrajectoryAxes} onChange={(event) => setShowTrajectoryAxes(event.currentTarget.checked)} /><Checkbox label="Grid" checked={showTrajectoryGrid} onChange={(event) => setShowTrajectoryGrid(event.currentTarget.checked)} /><Checkbox label="Manual XY range" checked={manualViewport} disabled={Boolean(focusActor)} onChange={(event) => setManualViewport(event.currentTarget.checked)} />{manualViewport && !focusActor && <SimpleGrid cols={2} spacing="xs"><NumberInput label="X min" value={viewportXMin} onChange={setViewportXMin} allowDecimal /><NumberInput label="X max" value={viewportXMax} onChange={setViewportXMax} allowDecimal /><NumberInput label="Y min" value={viewportYMin} onChange={setViewportYMin} allowDecimal /><NumberInput label="Y max" value={viewportYMax} onChange={setViewportYMax} allowDecimal /></SimpleGrid>}</Stack></Card>
          <Card p="md"><Group justify="space-between" mb="sm"><Text fw={650} size="sm">Timeline</Text><Badge variant="light">{timeIndex + 1} / {timeline.length}</Badge></Group><Text size="xs" c="dimmed" mb={6}>{currentTime.toFixed(3)} s</Text><div className="pisa-replay-slider"><input className="pisa-horizontal-range" type="range" aria-label="Replay timeline" value={timeIndex} onChange={(event) => { setPlaying(false); setTime(timeline[Number(event.currentTarget.value)] ?? timeline[0] ?? 0); }} min={0} max={Math.max(0, timeline.length - 1)} step={1} disabled={timeline.length < 2} /></div><Group gap="xs" mt="sm"><Button style={{ flex: 1 }} size="md" variant={playing ? 'filled' : 'light'} onClick={() => { if (timeIndex >= timeline.length - 1) setTime(timeline[0] ?? 0); setPlaying((value) => !value); }} leftSection={<IconPlayerPlay size={18} />}>{playing ? 'Pause' : 'Play'}</Button><Select aria-label="Playback speed" value={playbackRate} onChange={setPlaybackRate} allowDeselect={false} data={['0.25', '0.5', '1', '2', '4', '8'].map((value) => ({ value, label: `${value}×` }))} w={82} /></Group><Group grow mt="xs"><Button size="compact-xs" variant="default" disabled={!previousEvent} onClick={() => previousEvent && setTime(previousEvent.time)}>Prev event</Button><Button size="compact-xs" variant="default" disabled={!nextEvent} onClick={() => nextEvent && setTime(nextEvent.time)}>Next event</Button></Group></Card>
        </Stack>
        <Stack gap="lg">
          <VisualizationCard spec={trajectory} aspectRatio={trajectoryAspectRatio} animationDurationSeconds={Math.max(0.25, (timeDomain.maximum - timeDomain.minimum) / Math.max(0.01, Number(playbackRate ?? 1)))} animationOptionAtProgress={trajectoryAnimationOptionAtProgress} emptyDescription="No selected actor trace contains both recorded x and y positions." />
          <SimpleGrid cols={{ base: 1, xl: 2 }}>
            <Stack gap="sm"><Card p="md"><Group justify="space-between" align="flex-start" wrap="wrap"><div><Text fw={650} size="sm">{replayAnalysisMode === 'compare' ? 'Delta metrics' : 'Metrics'}</Text><Text size="xs" c="dimmed">{replayAnalysisMode === 'compare' ? 'Directional ego differences at timestamps recorded by both experiments.' : 'Recorded state and safety values. Speed means measured actor speed, never a control target.'}</Text></div><Group>{replayAnalysisMode !== 'compare' && <Checkbox label="Include selected non-ego actors" checked={includeAgentMetrics} onChange={(event) => setIncludeAgentMetrics(event.currentTarget.checked)} />}<Button size="compact-xs" variant="default" onClick={() => setActiveStateMetrics([])}>Hide all</Button></Group></Group><Group gap="md" mt="md">{stateMetricKeys.map((metric) => <Checkbox key={metric} label={metricDefinitions[metric].label} checked={activeStateMetrics.includes(metric)} onChange={(event) => setActiveStateMetrics((current) => event.currentTarget.checked ? [...current, metric] : current.filter((value) => value !== metric))} />)}</Group></Card><VisualizationCard spec={replayAnalysisMode === 'compare' ? comparisonCharts.metrics : metricCharts.metrics} animationDurationSeconds={Math.max(0.25, (timeDomain.maximum - timeDomain.minimum) / Math.max(0.01, Number(playbackRate ?? 1)))} emptyDescription={replayAnalysisMode === 'compare' ? 'The two experiments have no common recorded timestamps for the enabled metrics.' : 'Enable a metric or select an ego actor with recorded state data. Non-ego actors are hidden by default.'} /></Stack>
            <Stack gap="sm"><Card p="md"><Group justify="space-between" align="flex-start" wrap="wrap"><div><Text fw={650} size="sm">{replayAnalysisMode === 'compare' ? 'Delta controls' : 'Controls'}</Text><Text size="xs" c="dimmed">{replayAnalysisMode === 'compare' ? 'Directional command differences; T/S/B and Ackermann target semantics remain separate.' : "Options are derived from each experiment's recorded control type; T/S/B and Ackermann target semantics are kept separate."}</Text></div><Button size="compact-xs" variant="default" onClick={() => setActiveControlMetrics([])}>Hide all</Button></Group><Group gap="md" mt="md">{availableControlKeys.map((control) => <Checkbox key={control} label={controlDefinitions[control].label} checked={activeControlMetrics.includes(control)} onChange={(event) => setActiveControlMetrics((current) => event.currentTarget.checked ? [...current, control] : current.filter((value) => value !== control))} />)}{!availableControlKeys.length && <Text size="xs" c="dimmed">No recognized control command fields were recorded.</Text>}</Group></Card><VisualizationCard spec={replayAnalysisMode === 'compare' ? comparisonCharts.controls : metricCharts.controls} animationDurationSeconds={Math.max(0.25, (timeDomain.maximum - timeDomain.minimum) / Math.max(0.01, Number(playbackRate ?? 1)))} emptyDescription={replayAnalysisMode === 'compare' ? 'The two experiments have no common recorded timestamps for the enabled control commands.' : 'Enable a recorded control command. Throttle is preferred for T/S/B; Speed target is preferred for Ackermann.'} /></Stack>
          </SimpleGrid>
          {replayAnalysisMode === 'compare' && replayComparison && <Card p="lg"><Group justify="space-between" align="flex-start" mb="md"><div><Text fw={700}>Detailed two-experiment difference</Text><Text size="sm" c="dimmed">{replayComparison.right} − {replayComparison.left} · exact common recorded timestamps only</Text></div><Badge variant="light" color="indigo">{replayComparisonRows.length} comparable signals</Badge></Group>{replayComparison.trajectorySummary && <SimpleGrid cols={{ base: 2, md: 4 }} mb="lg"><Card withBorder p="md"><Text size="xs" c="dimmed">ADE</Text><Text fz={24} fw={700}>{comparisonValue(replayComparison.trajectorySummary.mean, 'm')}</Text></Card><Card withBorder p="md"><Text size="xs" c="dimmed">FDE</Text><Button variant="subtle" px={0} onClick={() => replayComparison.trajectoryFde && jumpToComparisonTime(replayComparison.trajectoryFde[0])}>{comparisonValue(replayComparison.trajectoryFde?.[1], 'm')}</Button></Card><Card withBorder p="md"><Text size="xs" c="dimmed">Trajectory RMSE</Text><Text fz={24} fw={700}>{comparisonValue(replayComparison.trajectorySummary.rmse, 'm')}</Text></Card><Card withBorder p="md"><Text size="xs" c="dimmed">Aligned trajectory steps</Text><Text fz={24} fw={700}>{replayComparison.trajectorySummary.count.toLocaleString()}</Text><Text size="xs" c="dimmed">left {replayComparison.leftTrajectoryCount.toLocaleString()} · right {replayComparison.rightTrajectoryCount.toLocaleString()}</Text></Card></SimpleGrid>}<Alert color="gray" mb="md" title="How to read the table">Mean Δ preserves direction. MAE, RMSE, and P95 |Δ| describe magnitude. For trajectory distance, ADE equals the mean distance and FDE is shown above. Click any reported time to move the synchronized trajectory to that recorded step.</Alert>{replayComparisonRows.length ? <ScrollArea type="auto"><table className="pisa-data-table"><thead><tr><th>Signal</th><th>Aligned / left / right</th><th>Mean Δ</th><th>MAE</th><th>RMSE</th><th>Minimum Δ · time</th><th>Maximum Δ · time</th><th>P95 |Δ| · nearest time</th></tr></thead><tbody>{replayComparisonRows.map((row) => { const value = (number: number) => comparisonValue(number, row.unit); const at = (rowTime: number) => <Button size="compact-xs" variant="subtle" px={2} onClick={() => jumpToComparisonTime(rowTime)}>{rowTime.toFixed(3)} s</Button>; return <tr key={`${row.category}-${row.key}`}><td><Badge size="xs" variant="light" color={row.category === 'Trajectory' ? 'violet' : row.category === 'Metric' ? 'indigo' : 'cyan'}>{row.category}</Badge><Text size="sm" fw={600}>{row.label}</Text><Text size="xs" c="dimmed">{row.unit || 'unitless'}</Text></td><td>{row.summary.count.toLocaleString()} / {row.leftCount.toLocaleString()} / {row.rightCount.toLocaleString()}</td><td>{value(row.summary.mean)}</td><td>{value(row.summary.mae)}</td><td>{value(row.summary.rmse)}</td><td><Text size="sm">{value(row.summary.minimum[1])}</Text>{at(row.summary.minimum[0])}</td><td><Text size="sm">{value(row.summary.maximum[1])}</Text>{at(row.summary.maximum[0])}</td><td><Text size="sm">{value(row.summary.p95Absolute.value)}</Text><Text size="xs" c="dimmed">signed {value(row.summary.p95Absolute.delta)}</Text>{at(row.summary.p95Absolute.time)}</td></tr>; })}</tbody></table></ScrollArea> : <EmptyState title="No aligned comparison values" description="The selected experiments do not share recorded timestamps for trajectory, metrics, or compatible control commands." />}</Card>}
        </Stack>
      </div>
      <Card p="lg">
        <Group justify="space-between" mb="sm"><div><Text fw={650}>Trace channel availability</Text><Text size="xs" c="dimmed">Hover a channel badge for its recorded scalar fields. Only the most useful recognized channels are plotted above.</Text></div><Badge variant="light" color="gray">{channelInfo.length} channels</Badge></Group>
        {channelInfo.length ? <Group gap="xs">{channelInfo.slice(0, 12).map((channel) => <Badge key={channel.name} variant="light" color={/ego|metrics/i.test(channel.name) ? 'indigo' : 'gray'} title={channel.fields.length ? channel.fields.join(', ') : 'No scalar field metadata'}>{channel.name} · {channel.pointCount.toLocaleString()} pts</Badge>)}{channelInfo.length > 12 && <Badge variant="outline" color="gray">+{channelInfo.length - 12} more</Badge>}</Group> : <Text size="sm" c="dimmed">No trace channel metadata was recorded.</Text>}
      </Card>
      <Card p="lg"><Group align="flex-end" mb="md"><Select label="Trace channel" searchable data={channelInfo.map((channel) => ({ value: channel.name, label: `${channel.name} · ${channel.pointCount.toLocaleString()} points` }))} value={genericChannel} onChange={(value) => { setGenericChannel(value); setGenericField(null); }} style={{ flex: 1 }} /><Select label="Scalar field" searchable data={genericFields.map((field) => ({ value: field, label: field }))} value={genericField} onChange={setGenericField} style={{ flex: 1 }} /></Group><VisualizationCard spec={genericSpec} emptyDescription="The selected channel field has no recorded numeric values." /></Card>
      <Card p="lg"><Accordion multiple variant="contained"><Accordion.Item value="case-metadata"><Accordion.Control>All run, parameter, attempt, geometry, map, event, and trace-channel metadata</Accordion.Control><Accordion.Panel><ScrollArea h={480}><Code block>{JSON.stringify({ run: detail.data.run, attempts: replay?.attempts, navigation: replay?.navigation, geometry: replay?.geometry, map: replay?.map, events: replay?.events, trace_channels: replay?.trace_channels }, null, 2)}</Code></ScrollArea></Accordion.Panel></Accordion.Item></Accordion></Card>
      <Card p="lg">
        <Group justify="space-between" mb="md"><div><Text fw={650}>Events reached by cursor</Text><Text size="xs" c="dimmed">The latest 100 reached events are shown; future events remain hidden until the cursor reaches them.</Text></div><Badge variant="light" color="gray">{reachedEvents.length} / {events.length}</Badge></Group>
        {nextEvent && <Alert color="gray" mb="md" title={`Next: ${nextEvent.label}`}>{nextEvent.time.toFixed(3)} s · {nextEvent.type}</Alert>}
        {reachedEvents.length ? <ScrollArea><table className="pisa-data-table"><thead><tr><th>Time</th><th>Type</th><th>Event</th><th>Recorded detail</th><th>Severity</th></tr></thead><tbody>{reachedEvents.map((event, index) => <tr key={`${event.time}-${event.type}-${index}`}><td>{event.time.toFixed(3)} s</td><td><Text className="pisa-code" size="xs">{event.type}</Text></td><td>{event.label}</td><td><Text size="xs" c="dimmed">{eventDetails(event) || '—'}</Text></td><td><StatusBadge value={event.severity ?? 'info'} /></td></tr>)}</tbody></table></ScrollArea> : <EmptyState title={events.length ? 'No event reached yet' : 'No discrete events recorded'} description={events.length ? 'Move the replay cursor forward or jump to the next event.' : 'Continuous state, safety, and control traces remain available above.'} />}
      </Card>
      <Card p="lg">
        <Group justify="space-between" mb="md"><div><Text fw={650}>Recorded agent geometry</Text><Text size="xs" c="dimmed">Dimensions, reference points, and provenance are intentionally placed after trace and event evidence.</Text></div><Badge variant="light" color={geometry.length ? 'indigo' : 'gray'}>{geometry.length} agents</Badge></Group>
        {geometry.length ? <Stack gap="sm">{geometry.map((item, index) => <Card key={`${item.agent_id}-${item.entity_name}-${index}`} withBorder p="sm"><Group justify="space-between" wrap="nowrap"><div><Group gap="xs"><Text size="sm" fw={600}>{item.entity_name ?? item.agent_id ?? `Agent ${index + 1}`}</Text>{item.is_ego && <Badge size="xs" color="indigo" variant="light">Ego</Badge>}</Group><Text size="xs" c="dimmed">L {formatGeometryDimension(item.length_m)} × W {formatGeometryDimension(item.width_m)} × H {formatGeometryDimension(item.height_m)}</Text></div><div style={{ textAlign: 'right' }}><Text size="xs">{item.reference_point ?? 'Reference not recorded'}</Text><Text size="xs" c="dimmed">{item.source ?? 'Source not recorded'}</Text></div></Group></Card>)}</Stack> : <EmptyState title="Geometry was not recorded" description="Trajectories still use the state coordinates, but no vehicle dimensions or reference-point convention can be claimed." />}
      </Card>
    </Stack>
  );
}

function Media({ datasetId, onChoose }: { datasetId: string; onChoose: () => void }) {
  const media = useQuery({ queryKey: ['media', datasetId], queryFn: () => api.datasets.media(datasetId), retry: 1 });
  if (media.isLoading) return <PageLoading label="Indexing media…" />;
  if (media.error) return <InlineError error={media.error} onRetry={() => media.refetch()} />;
  if (!media.data?.items.length) return <Card><EmptyState title="No media in this report yet" description="Choose a recorded run, then create a clearly labeled schematic animation or keyframe from its indexed trajectory." action={<Button variant="light" onClick={onChoose} leftSection={<IconMovie size={16} />}>Choose a run</Button>} icon={<IconMovie size={23} />} /></Card>;
  return <SimpleGrid cols={{ base: 1, sm: 2, xl: 3 }}>{media.data.items.map((item) => <Card key={item.id} p={0} style={{ overflow: 'hidden' }}><div style={{ height: 200, background: '#edf0f7', display: 'grid', placeItems: 'center' }}>{item.url && item.kind === 'video' ? <video src={item.url} controls preload="metadata" style={{ width: '100%', height: '100%', objectFit: 'contain' }} /> : item.url && (item.kind === 'image' || item.kind === 'animation') ? <img src={item.url} alt={item.name} loading="lazy" style={{ width: '100%', height: '100%', objectFit: 'contain' }} /> : <IconMovie size={36} color="#8b95a5" />}</div><Stack p="md" gap="xs"><Group justify="space-between" wrap="nowrap"><Text fw={600} size="sm" lineClamp={1}>{item.name}</Text><Badge variant="light" color={item.source === 'derived' ? 'indigo' : 'teal'}>{item.source}</Badge></Group><Text size="xs" c="dimmed">{item.kind} · {item.mime_type}</Text>{item.url && <Button component="a" href={item.url} target="_blank" rel="noreferrer" size="xs" variant="light" leftSection={<IconDownload size={14} />}>Open artifact</Button>}</Stack></Card>)}</SimpleGrid>;
}

function Finding({ finding }: { finding: DataHealthFinding }) {
  return <Card withBorder p="md"><Group justify="space-between" align="flex-start" wrap="nowrap"><div><Group gap="xs"><StatusBadge value={finding.severity} /><Text size="sm" fw={600}>{finding.title}</Text></Group><Text size="xs" c="dimmed" mt="xs">{finding.detail}</Text><Text className="pisa-code" size="xs" c="dimmed" mt="xs">{finding.code}</Text></div>{finding.affected_runs !== undefined && <Badge variant="light" color="gray">{finding.affected_runs.toLocaleString()} runs</Badge>}</Group></Card>;
}

function Provenance({ datasetId }: { datasetId: string }) {
  const summary = useReportSummary(datasetId);
  const details = useQuery({ queryKey: ['report-details', datasetId], queryFn: () => api.datasets.details(datasetId), retry: 1 });
  if (summary.isLoading) return <PageLoading label="Reconciling provenance…" />;
  if (summary.error) return <InlineError error={summary.error} onRetry={() => summary.refetch()} />;
  return <Stack><Alert color="blue" icon={<IconShieldCheck size={17} />} title="Source-aware analysis">Hashes, attempts, compatibility decisions, excluded runs, inferred epochs, and source relinks remain visible and are included in every export.</Alert><Card p="lg"><Text fw={650}>Recorded source</Text><SimpleGrid cols={{ base: 1, sm: 3 }} mt="md"><div><Text size="xs" c="dimmed">Dataset</Text><Text size="sm" fw={600}>{datasetId}</Text></div><div><Text size="xs" c="dimmed">Generated</Text><Text size="sm" fw={600}>{summary.data?.generated_at ? new Date(summary.data.generated_at).toLocaleString() : 'In progress'}</Text></div><div><Text size="xs" c="dimmed">Report schema</Text><Text size="sm" fw={600}>v3 · normalized store</Text></div></SimpleGrid></Card><div><Text fw={650}>Data-health findings</Text><Text size="sm" c="dimmed" mb="md">Findings change eligibility and presentation; they never rewrite the source.</Text><Stack>{summary.data?.health?.map((finding) => <Finding finding={finding} key={finding.id} />) ?? <Card><EmptyState title="No findings recorded" description="The reconciliation stage did not report provenance or data-quality concerns." /></Card>}</Stack></div><Card p="lg"><Text fw={650} mb="md">Complete report metadata</Text>{details.isLoading ? <PageLoading label="Loading manifest, experiments, components, scenarios, index, and provenance…" /> : details.error ? <InlineError error={details.error} onRetry={() => details.refetch()} /> : <Accordion multiple variant="contained" defaultValue={['metadata']}><Accordion.Item value="metadata"><Accordion.Control>Manifest, all experiment descriptors, simulator/AV/sampler configuration, source hashes, and index metadata</Accordion.Control><Accordion.Panel><ScrollArea h={520}><Code block>{JSON.stringify(details.data, null, 2)}</Code></ScrollArea></Accordion.Panel></Accordion.Item></Accordion>}</Card></Stack>;
}

function Exports({ datasetId }: { datasetId: string }) {
  const snapshot = useMutation({ mutationFn: () => api.datasets.snapshot(datasetId) });
  const included = [
    'Aggregate outcomes, data-health findings, and recorded provenance',
    'A self-contained HTML entry point that opens directly from disk',
    'No machine-specific absolute source paths in snapshot-visible metadata',
    'Run-level traces remain lazy in the interactive server workspace',
  ];
  return <SimpleGrid cols={{ base: 1, lg: 2 }}><Card p="lg"><Stack><div><Text fw={650}>Portable report snapshot</Text><Text size="sm" c="dimmed">Open the immutable snapshot produced with this report bundle.</Text></div><Alert color="blue" icon={<IconShieldCheck size={17} />}>The compact snapshot is generated atomically with the report. This check does not rebuild or mutate the report.</Alert>{snapshot.error && <InlineError error={snapshot.error} />}{snapshot.data?.available && <Alert color="teal" title="Portable snapshot is ready"><Anchor href={snapshot.data.url} target="_blank" rel="noreferrer">Open {snapshot.data.path}</Anchor></Alert>}<Button loading={snapshot.isPending} leftSection={<IconDownload size={16} />} onClick={() => snapshot.mutate()}>{snapshot.data?.available ? 'Check again' : 'Check portable snapshot'}</Button></Stack></Card><Card p="lg"><Text fw={650}>What is included</Text><Stack mt="md" gap="md">{included.map((item) => <Group key={item} wrap="nowrap"><ThemeIcon size={25} radius="xl" variant="light" color="teal"><IconCheck size={14} /></ThemeIcon><Text size="sm">{item}</Text></Group>)}</Stack></Card></SimpleGrid>;
}

export function ReportWorkspacePage() {
  const { datasetId, section = 'overview', runId } = useParams();
  const navigate = useNavigate();
  const reportPreview = useQuery({ queryKey: ['report-preview-id', datasetId], queryFn: () => api.datasets.previewById(datasetId!), enabled: Boolean(datasetId), staleTime: 300_000, retry: 1 });
  const replayStorageKey = datasetId ? `pisa:last-replay:${datasetId}` : '';
  useEffect(() => {
    if (!datasetId || section !== 'replay' || runId) return;
    const remembered = window.sessionStorage.getItem(replayStorageKey);
    if (remembered) navigate(`/reports/${encodeURIComponent(datasetId)}/replay/${encodeURIComponent(remembered)}`, { replace: true });
  }, [datasetId, navigate, replayStorageKey, runId, section]);
  if (!datasetId) return <SelectReport />;
  const selected = reportPreview.data;
  const setSection = (value: string | null) => {
    if (!value) return;
    const remembered = value === 'replay' ? window.sessionStorage.getItem(replayStorageKey) : null;
    navigate(`/reports/${encodeURIComponent(datasetId)}/${value}${remembered ? `/${encodeURIComponent(remembered)}` : ''}`);
  };
  const openRun = (id: string, experiments?: string[]) => {
    window.sessionStorage.setItem(replayStorageKey, id);
    const contextKey = `pisa:replay-context:${datasetId}`;
    if (experiments?.length) window.sessionStorage.setItem(contextKey, JSON.stringify({ runId: id, experiments }));
    else window.sessionStorage.removeItem(contextKey);
    navigate(`/reports/${encodeURIComponent(datasetId)}/replay/${encodeURIComponent(id)}`);
  };

  return (
    <>
      <PageHeader eyebrow="Report workspace" title={selected?.name ?? 'Evidence report'} description={selected ? `${selected.run_count.toLocaleString()} runs across ${selected.experiment_count} experiments · filters and exports retain provenance.` : 'Loading indexed report metadata…'} actions={<Button component={Link} to="/reports" variant="default" leftSection={<IconArrowLeft size={16} />}>All reports</Button>} />
      <Tabs value={section} onChange={setSection} variant="outline" mb="lg">
        <ScrollArea type="never"><Tabs.List style={{ flexWrap: 'nowrap' }}>{sections.map(([value, label]) => <Tabs.Tab key={value} value={value}>{label}</Tabs.Tab>)}</Tabs.List></ScrollArea>
      </Tabs>
      {section === 'overview' && <Overview datasetId={datasetId} />}
      {section === 'sampling' && <Stack gap="lg"><ScatterExplorer datasetId={datasetId} onOpen={openRun} /><ChartSection datasetId={datasetId} section={section} /></Stack>}
      {['outcomes', 'performance', 'sensitivity'].includes(section) && <ChartSection datasetId={datasetId} section={section} />}
      {section === 'compare' && <Compare datasetId={datasetId} onOpen={openRun} />}
      {section === 'runs' && <Runs datasetId={datasetId} onOpen={openRun} />}
      {section === 'replay' && <Replay datasetId={datasetId} runId={runId} onChoose={() => setSection('runs')} onOpen={openRun} />}
      {section === 'media' && <Media datasetId={datasetId} onChoose={() => setSection('runs')} />}
      {section === 'provenance' && <Provenance datasetId={datasetId} />}
      {section === 'exports' && <Exports datasetId={datasetId} />}
    </>
  );
}
