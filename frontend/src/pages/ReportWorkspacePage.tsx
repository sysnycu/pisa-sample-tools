import { type CSSProperties, type Dispatch, type SetStateAction, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Accordion, ActionIcon, Alert, Anchor, Badge, Button, Card, Checkbox, Code, ColorInput, Divider, Group, Modal, MultiSelect, NumberInput, Popover, Progress, ScrollArea, Select, SimpleGrid, Stack, Tabs, Text, TextInput, ThemeIcon } from '@mantine/core';
import {
  IconAlertTriangle, IconArrowDown, IconArrowLeft, IconArrowsSort, IconArrowUp, IconCarCrash, IconCheck, IconClock, IconDatabase,
  IconDownload, IconFolder, IconFolderPlus, IconMovie, IconPlayerPlay, IconRefresh, IconRoute, IconSearch, IconShieldCheck,
} from '@tabler/icons-react';
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api } from '../api/client';
import { useReportCharts, useReportSummary } from '../api/query';
import type { CaseDetail, ComparisonClass, ConsistencyAnalyzeRequest, ConsistencyGroup, CrossExperimentComparison, DataHealthFinding, DatasetDescriptor, Job, PairedMetricAgreement as PairedMetricAgreementResult, PairedParameterAnalysis as PairedParameterAnalysisResult, ReportSummary, RunRecord, VisualizationSpec } from '../api/types';
import { EmptyState, InlineError, PageLoading } from '../components/Feedback';
import { MetricCard } from '../components/MetricCard';
import { PageHeader } from '../components/PageHeader';
import { StatusBadge } from '../components/StatusBadge';
import { VisualizationCard } from '../components/VisualizationCard';
import type { SeriesStyleOverride } from '../components/VisualizationCard';

const sections = [
  ['overview', 'Overview'], ['sampling', 'Sampling'], ['outcomes', 'Outcomes & safety'], ['performance', 'Performance'],
  ['compare', 'Compare'], ['consistency', 'Consistency'], ['sensitivity', 'Sensitivity'], ['runs', 'Runs'], ['replay', 'Run detail / replay'],
  ['media', 'Media'], ['provenance', 'Provenance & health'], ['exports', 'Exports'],
] as const;

const sequentialColorRange: [string, string, string] = ['#dce6ff', '#526ff0', '#c92a2a'];
const deltaColorRange: [string, string, string] = ['#00a6a6', '#f8fafc', '#7e2f8e'];

export function interpolateColorRange(value: number, minimum: number, maximum: number, colors: [string, string, string]): string {
  const parse = (color: string) => /^#[0-9a-f]{6}$/i.test(color) ? [1, 3, 5].map((index) => Number.parseInt(color.slice(index, index + 2), 16)) : undefined;
  const parsed = colors.map(parse);
  if (parsed.some((color) => !color) || !Number.isFinite(value) || !Number.isFinite(minimum) || !Number.isFinite(maximum)) return '#6b7280';
  const position = maximum > minimum ? Math.max(0, Math.min(1, (value - minimum) / (maximum - minimum))) : 0.5;
  const segment = position <= 0.5 ? 0 : 1;
  const amount = position <= 0.5 ? position * 2 : (position - 0.5) * 2;
  const left = parsed[segment]!, right = parsed[segment + 1]!;
  const channel = (index: number) => Math.round(left[index] + (right[index] - left[index]) * amount).toString(16).padStart(2, '0');
  return `#${channel(0)}${channel(1)}${channel(2)}`;
}

const ANALYSIS_ROOT = '/home/hcis-s05/ysws/PISA/pisa-sample-tools/analysis';

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

function HorizontalRangeInput({ minimum, maximum, step, value, onChange }: {
  minimum: number;
  maximum: number;
  step: number;
  value: [number, number];
  onChange: (value: [number, number]) => void;
}) {
  const span = Math.max(1e-12, maximum - minimum);
  const start = 100 * (value[0] - minimum) / span;
  const end = 100 * (value[1] - minimum) / span;
  const style = { '--pisa-range-start': `${start}%`, '--pisa-range-end': `${end}%` } as CSSProperties;
  return <div className="pisa-dual-range" style={style}>
    <input className="pisa-horizontal-range pisa-dual-range__input" type="range" aria-label="Filter minimum" min={minimum} max={maximum} step={step} value={value[0]} onChange={(event) => onChange([Math.min(Number(event.currentTarget.value), value[1]), value[1]])} />
    <input className="pisa-horizontal-range pisa-dual-range__input" type="range" aria-label="Filter maximum" min={minimum} max={maximum} step={step} value={value[1]} onChange={(event) => onChange([value[0], Math.max(Number(event.currentTarget.value), value[0])])} />
  </div>;
}

type DisagreementHeatmapPoint = {
  x: number;
  y: number;
  left_outcome?: string;
  right_outcome?: string;
};

export function formatHeatmapCellLabel(disagreement: number, total: number, showCounts: boolean, showPercentage: boolean): string {
  return [
    showCounts ? `${disagreement}/${total}` : '',
    showPercentage ? `${(100 * disagreement / Math.max(1, total)).toFixed(1)}%` : '',
  ].filter(Boolean).join('\n');
}

export function buildDisagreementHeatmap(
  points: DisagreementHeatmapPoint[],
  xBinCount: number,
  yBinCount: number,
  bounds: Partial<{ xMin: number; xMax: number; yMin: number; yMax: number }> = {},
) {
  const boundedXCount = Math.max(2, Math.min(20, Math.round(xBinCount)));
  const boundedYCount = Math.max(2, Math.min(20, Math.round(yBinCount)));
  if (!points.length) return { xLabels: [], yLabels: [], cells: [], disagreementCount: 0, totalCount: 0, excludedCount: 0 };
  const xs = points.map((point) => point.x), ys = points.map((point) => point.y);
  const observedXMin = Math.min(...xs), observedXMax = Math.max(...xs), observedYMin = Math.min(...ys), observedYMax = Math.max(...ys);
  const xMin = bounds.xMin != null && bounds.xMax != null && bounds.xMin < bounds.xMax ? bounds.xMin : observedXMin;
  const xMax = bounds.xMin != null && bounds.xMax != null && bounds.xMin < bounds.xMax ? bounds.xMax : observedXMax;
  const yMin = bounds.yMin != null && bounds.yMax != null && bounds.yMin < bounds.yMax ? bounds.yMin : observedYMin;
  const yMax = bounds.yMin != null && bounds.yMax != null && bounds.yMin < bounds.yMax ? bounds.yMax : observedYMax;
  const xWidth = Math.max(1e-12, (xMax - xMin) / boundedXCount), yWidth = Math.max(1e-12, (yMax - yMin) / boundedYCount);
  const label = (minimum: number, width: number, index: number, count: number) => {
    const lower = Number((minimum + index * width).toPrecision(8));
    const upper = Number((index === count - 1 ? minimum + count * width : minimum + (index + 1) * width).toPrecision(8));
    return `${lower.toLocaleString()}–${upper.toLocaleString()}`;
  };
  const xLabels = Array.from({ length: boundedXCount }, (_, index) => label(xMin, xWidth, index, boundedXCount));
  const yLabels = Array.from({ length: boundedYCount }, (_, index) => label(yMin, yWidth, index, boundedYCount));
  const counts = Array.from({ length: boundedXCount * boundedYCount }, () => ({ total: 0, disagreement: 0 }));
  const included = points.filter((point) => point.x >= xMin && point.x <= xMax && point.y >= yMin && point.y <= yMax);
  for (const point of included) {
    const xIndex = Math.min(boundedXCount - 1, Math.max(0, Math.floor((point.x - xMin) / xWidth)));
    const yIndex = Math.min(boundedYCount - 1, Math.max(0, Math.floor((point.y - yMin) / yWidth)));
    const cell = counts[yIndex * boundedXCount + xIndex];
    cell.total += 1;
    cell.disagreement += Number(point.left_outcome !== point.right_outcome);
  }
  const cells = counts.flatMap((cell, index) => cell.total ? [{
    value: [index % boundedXCount, Math.floor(index / boundedXCount), cell.disagreement / cell.total, cell.disagreement, cell.total],
    disagreement_count: cell.disagreement,
    total_count: cell.total,
  }] : []);
  return {
    xLabels,
    yLabels,
    cells,
    disagreementCount: cells.reduce((total, cell) => total + cell.disagreement_count, 0),
    totalCount: included.length,
    excludedCount: points.length - included.length,
  };
}

const compareOutcomeOrder = [
  'all success', 'all pass', 'all fail', 'all invalid', 'all unknown',
  'success → fail', 'pass → fail', 'success → invalid', 'pass → invalid',
  'fail → success', 'fail → pass', 'invalid → success', 'invalid → pass',
  'fail → invalid', 'invalid → fail',
];

export function compareScatterCategories(selectedColor: string, left: string, right: string): number {
  const normalizedLeft = left.trim().toLowerCase(), normalizedRight = right.trim().toLowerCase();
  if (selectedColor === 'outcome') {
    const leftRank = compareOutcomeOrder.indexOf(normalizedLeft), rightRank = compareOutcomeOrder.indexOf(normalizedRight);
    const rankedLeft = leftRank < 0 ? (normalizedLeft.includes('unknown') ? 100 : 50) : leftRank;
    const rankedRight = rightRank < 0 ? (normalizedRight.includes('unknown') ? 100 : 50) : rightRank;
    if (rankedLeft !== rankedRight) return rankedLeft - rankedRight;
  } else if (selectedColor === 'stop_reason' || selectedColor === 'stop_condition') {
    const categoryRank = (value: string) => value.startsWith('same ·') ? 0 : value.includes('→') ? 1 : value === 'missing' ? 3 : 2;
    const rankDifference = categoryRank(normalizedLeft) - categoryRank(normalizedRight);
    if (rankDifference) return rankDifference;
  } else {
    if (normalizedLeft === 'missing') return normalizedRight === 'missing' ? 0 : 1;
    if (normalizedRight === 'missing') return -1;
  }
  return left.localeCompare(right, undefined, { numeric: true, sensitivity: 'base' });
}

function chartRecord(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

export function replayChartWithVisibleAxes(spec: VisualizationSpec, visibility: Record<string, boolean>, colors: Record<string, string>): VisualizationSpec {
  const rawSeries = Array.isArray(spec.option.series) ? spec.option.series.map(chartRecord).filter((value): value is Record<string, unknown> => Boolean(value)) : [];
  const rawAxes = Array.isArray(spec.option.yAxis) ? spec.option.yAxis.map(chartRecord).filter((value): value is Record<string, unknown> => Boolean(value)) : [];
  if (!rawSeries.length || !rawAxes.length) return spec;
  const visibleAxisIndices = [...new Set(rawSeries.flatMap((series) => {
    const name = typeof series.name === 'string' ? series.name : '';
    if (!name || name.startsWith('__') || visibility[name] === false) return [];
    const index = typeof series.yAxisIndex === 'number' ? series.yAxisIndex : 0;
    return index >= 0 && index < rawAxes.length ? [index] : [];
  }))].sort((left, right) => left - right);
  const axisMap = new Map(visibleAxisIndices.map((original, index) => [original, index]));
  const sideCounts = { left: 0, right: 0 };
  const visibleAxes = visibleAxisIndices.map((index) => {
    const axis = rawAxes[index];
    const side = axis.position === 'right' ? 'right' : 'left';
    const offset = sideCounts[side] * 54;
    sideCounts[side] += 1;
    return { ...axis, position: side, offset: offset || undefined };
  });
  const fallbackAxes = visibleAxes.length ? visibleAxes : [{ type: 'value', show: false }];
  const series = rawSeries.map((entry) => {
    const name = typeof entry.name === 'string' ? entry.name : '';
    const originalAxis = typeof entry.yAxisIndex === 'number' ? entry.yAxisIndex : 0;
    const customColor = name ? colors[name] : undefined;
    return {
      ...entry,
      yAxisIndex: axisMap.get(originalAxis) ?? 0,
      ...(customColor ? { lineStyle: { ...(chartRecord(entry.lineStyle) ?? {}), color: customColor }, itemStyle: { ...(chartRecord(entry.itemStyle) ?? {}), color: customColor } } : {}),
    };
  });
  const grid = chartRecord(spec.option.grid) ?? {};
  return {
    ...spec,
    option: {
      ...spec.option,
      grid: {
        ...grid,
        left: 76 + Math.max(0, sideCounts.left - 1) * 54,
        right: 76 + Math.max(0, sideCounts.right - 1) * 54,
      },
      yAxis: fallbackAxes,
      series,
    },
  };
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

function ReportStorage({ report }: { report: DatasetDescriptor }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [opened, setOpened] = useState(false);
  const [browsePath, setBrowsePath] = useState(ANALYSIS_ROOT);
  const [outputParent, setOutputParent] = useState(ANALYSIS_ROOT);
  const [reportName, setReportName] = useState(report.name);
  const [overwrite, setOverwrite] = useState(false);
  const [newFolderName, setNewFolderName] = useState('');
  const [showNewFolder, setShowNewFolder] = useState(false);
  const [saveJob, setSaveJob] = useState<Job>();
  const browser = useQuery({ queryKey: ['report-persist-browser', browsePath], queryFn: () => api.datasets.browse(browsePath || ANALYSIS_ROOT), enabled: opened, retry: 1 });
  const createDirectory = useMutation({
    mutationFn: () => api.datasets.createDirectory(browsePath || ANALYSIS_ROOT, newFolderName.trim()),
    onSuccess: (data) => {
      setBrowsePath(data.path); setOutputParent(data.path); setNewFolderName(''); setShowNewFolder(false);
      void queryClient.invalidateQueries({ queryKey: ['report-persist-browser'] });
    },
  });
  const persist = useMutation({
    mutationFn: () => api.datasets.persist(report.id, { output_dir: `${outputParent.replace(/[\\/]+$/, '')}/${reportName.trim()}`, overwrite }),
    onSuccess: setSaveJob,
  });
  const job = useQuery({ queryKey: ['job', saveJob?.id], queryFn: () => api.jobs.get(saveJob!.id), enabled: Boolean(saveJob), refetchInterval: (query) => ['queued', 'running'].includes(query.state.data?.state ?? '') ? 750 : false });
  useEffect(() => {
    if (job.data?.state !== 'succeeded' || !job.data.report_id) return;
    void queryClient.invalidateQueries({ queryKey: ['report-browser'] });
    void queryClient.invalidateQueries({ queryKey: ['report-preview-id'] });
    void queryClient.invalidateQueries({ queryKey: ['datasets'] });
    setOpened(false);
    navigate(`/reports/${encodeURIComponent(job.data.report_id)}/overview`, { replace: true });
  }, [job.data?.report_id, job.data?.state, navigate, queryClient]);
  if (report.storage_kind !== 'temporary') {
    return <Card p="md"><Group justify="space-between" align="center" wrap="wrap"><div><Group gap="xs"><Text fw={650}>Report storage</Text><Badge color="teal" variant="light">Saved report</Badge></Group><Text className="pisa-code" size="sm" c="dimmed" mt={4}>{report.path}</Text></div><Button variant="default" size="compact-sm" onClick={() => void navigator.clipboard?.writeText(report.path)}>Copy path</Button></Group></Card>;
  }
  const shownJob = job.data ?? saveJob;
  const invalidName = !reportName.trim() || ['.', '..'].includes(reportName.trim()) || /[\\/]/.test(reportName);
  return <>
    <Alert color="indigo" title="Temporary report preview"><Group justify="space-between" align="center" wrap="wrap"><div><Text size="sm">This complete report is currently stored only for preview. Save it before leaving if you want to keep it.</Text><Text size="xs" c="dimmed">The preview stays active while this workspace is open and is discarded after 10 inactive minutes.</Text></div><Button onClick={() => { setBrowsePath(ANALYSIS_ROOT); setOutputParent(ANALYSIS_ROOT); setReportName(report.name); setSaveJob(undefined); persist.reset(); setOpened(true); }}>Save report</Button></Group></Alert>
    <Modal opened={opened} onClose={() => setOpened(false)} title="Save preview report" size="xl">
      <Stack>
        <Alert color="blue" title="No rebuild required">The complete preview bundle will be moved to the selected report location.</Alert>
        <TextInput label="Report name" value={reportName} onChange={(event) => setReportName(event.currentTarget.value)} error={invalidName ? 'Use one directory name without slashes.' : undefined} />
        <Card withBorder p="md"><Group align="flex-end" wrap="wrap"><ActionIcon aria-label="Parent directory" variant="default" size="lg" disabled={!browser.data?.parent} onClick={() => { if (browser.data?.parent) { setBrowsePath(browser.data.parent); setOutputParent(browser.data.parent); } }}><IconArrowLeft size={17} /></ActionIcon><TextInput label="Report destination browser" value={browsePath} onChange={(event) => setBrowsePath(event.currentTarget.value)} leftSection={<IconFolder size={16} />} style={{ flex: '1 1 440px' }} /><Button variant="default" loading={browser.isFetching} onClick={() => { setOutputParent(browsePath); void browser.refetch(); }}>Open</Button><Button variant="default" leftSection={<IconFolderPlus size={16} />} onClick={() => setShowNewFolder((value) => !value)}>New folder</Button><Button onClick={() => setOutputParent(browsePath)}>Use this folder</Button></Group>
        {showNewFolder && <Group mt="sm" align="flex-end" wrap="nowrap"><TextInput label="New directory name" value={newFolderName} onChange={(event) => setNewFolderName(event.currentTarget.value)} style={{ flex: 1 }} /><Button variant="default" onClick={() => setShowNewFolder(false)}>Cancel</Button><Button loading={createDirectory.isPending} disabled={!newFolderName.trim()} onClick={() => createDirectory.mutate()}>Create and open</Button></Group>}
        {browser.error && <div style={{ marginTop: 12 }}><InlineError error={browser.error} onRetry={() => browser.refetch()} /></div>}
        {browser.data && <ScrollArea mt="md" h={220}><Stack gap={4}>{browser.data.entries.filter((entry) => entry.kind === 'directory' || entry.kind === 'report').map((entry) => <Button key={entry.path} variant="subtle" color={entry.is_report ? 'indigo' : 'gray'} leftSection={<IconFolder size={15} />} onClick={() => { if (entry.is_report) { setOutputParent(browser.data.path); setReportName(entry.name); } else { setBrowsePath(entry.path); setOutputParent(entry.path); } }} style={{ justifyContent: 'flex-start' }}>{entry.name}{entry.is_report ? ' · existing report' : ''}</Button>)}</Stack></ScrollArea>}</Card>
        <Alert color="gray" title="Resolved report path">{`${outputParent.replace(/[\\/]+$/, '')}/${reportName.trim()}`}</Alert>
        <Checkbox label="Replace an existing PISA-owned report" checked={overwrite} onChange={(event) => setOverwrite(event.currentTarget.checked)} />
        {persist.error && <InlineError error={persist.error} />}
        {shownJob && <Alert color={shownJob.state === 'failed' ? 'red' : shownJob.state === 'succeeded' ? 'teal' : 'blue'} title={shownJob.state === 'failed' ? 'Save failed' : shownJob.state === 'succeeded' ? 'Report saved' : `Saving report · ${shownJob.phase}`}><Progress value={shownJob.progress?.total ? 100 * shownJob.progress.current / shownJob.progress.total : 100} animated={!shownJob.progress?.total && ['queued', 'running'].includes(shownJob.state)} /><Text size="xs" mt="xs">{shownJob.message ?? shownJob.phase}</Text></Alert>}
        <Group justify="flex-end"><Button variant="default" onClick={() => setOpened(false)}>Cancel</Button><Button loading={persist.isPending} disabled={invalidName || ['queued', 'running'].includes(shownJob?.state ?? '')} onClick={() => persist.mutate()}>Save report</Button></Group>
      </Stack>
    </Modal>
  </>;
}

function Overview({ datasetId, report }: { datasetId: string; report?: DatasetDescriptor }) {
  const navigate = useNavigate();
  const summary = useReportSummary(datasetId);
  const charts = useReportCharts(datasetId, 'overview');
  type ExperimentSortKey = 'experiment' | 'system' | 'total_samples' | 'success' | 'fail' | 'invalid' | 'unknown' | 'avg_time_seconds' | 'avg_speedup';
  const [experimentSort, setExperimentSort] = useSessionState<{ key: ExperimentSortKey; direction: 'asc' | 'desc' } | null>(`pisa:overview-experiment-sort:${datasetId}`, null);
  const experimentRows = useMemo(() => {
    const rows = [...(summary.data?.experiment_summaries ?? [])];
    if (!experimentSort) return rows;
    const value = (item: (typeof rows)[number]): string | number | null | undefined => experimentSort.key === 'system'
      ? `${item.simulator ?? ''} / ${item.av ?? ''}`
      : item[experimentSort.key];
    return rows.sort((left, right) => {
      const leftValue = value(left), rightValue = value(right);
      if (leftValue == null && rightValue == null) return left.experiment.localeCompare(right.experiment);
      if (leftValue == null) return 1;
      if (rightValue == null) return -1;
      const comparison = typeof leftValue === 'number' && typeof rightValue === 'number'
        ? leftValue - rightValue
        : String(leftValue).localeCompare(String(rightValue), undefined, { numeric: true, sensitivity: 'base' });
      return (experimentSort.direction === 'asc' ? comparison : -comparison) || left.experiment.localeCompare(right.experiment);
    });
  }, [experimentSort, summary.data?.experiment_summaries]);
  if (summary.isLoading) return <PageLoading label="Loading report overview…" />;
  if (summary.error) return <InlineError error={summary.error} onRetry={() => summary.refetch()} />;
  if (!summary.data) return <EmptyState title="Overview is not ready" description="The report index may still be building. Check Jobs for progress." />;
  const data = summary.data;
  const speedup = data.simulated_seconds && data.wall_seconds ? data.simulated_seconds / data.wall_seconds : undefined;
  const generatedCharts = charts.data?.length ? charts.data : undefined;
  const visualizations = generatedCharts ?? summaryCharts(data);
  const healthErrors = data.health?.filter((finding) => finding.severity === 'error') ?? [];
  const healthWarnings = data.health?.filter((finding) => finding.severity !== 'error') ?? [];
  const sortHeader = (key: ExperimentSortKey, label: string) => {
    const active = experimentSort?.key === key;
    const nextDirection = active && experimentSort.direction === 'asc' ? 'descending' : 'ascending';
    return <th><Group gap={3} wrap="nowrap">{label}<ActionIcon size="compact-sm" variant={active ? 'light' : 'subtle'} color={active ? 'indigo' : 'gray'} aria-label={`Sort ${label} ${nextDirection}`} title={`Sort ${nextDirection}`} onClick={() => setExperimentSort((current) => current?.key === key ? { key, direction: current.direction === 'asc' ? 'desc' : 'asc' } : { key, direction: 'asc' })}>{!active ? <IconArrowsSort size={13} /> : experimentSort.direction === 'asc' ? <IconArrowUp size={13} /> : <IconArrowDown size={13} />}</ActionIcon></Group></th>;
  };
  const openExperimentSampling = (experiment: string) => {
    const storageKey = `pisa:sampling:${datasetId}`;
    window.sessionStorage.setItem(`${storageKey}:mode`, JSON.stringify('single'));
    window.sessionStorage.setItem(`${storageKey}:dataset`, JSON.stringify(experiment));
    navigate(`/reports/${encodeURIComponent(datasetId)}/sampling`);
  };
  return (
    <Stack gap="lg">
      {report && <ReportStorage report={report} />}
      <div className="pisa-page-grid">
        <MetricCard label="Runs" value={data.run_count.toLocaleString()} detail={`${data.experiment_count} experiments`} icon={<IconDatabase size={20} />} />
        <MetricCard label="Success" value={data.outcomes.success.toLocaleString()} detail={`${(100 * data.outcomes.success / Math.max(1, data.run_count)).toFixed(1)}% of all runs`} icon={<IconCheck size={20} />} color="teal" />
        <MetricCard label="Collisions" value={(data.collision_count ?? 0).toLocaleString()} detail="Recorded collision events" icon={<IconCarCrash size={20} />} color="red" />
        <MetricCard label="Aggregate speedup" value={speedup ? `${speedup.toFixed(1)}×` : '—'} detail="Σ simulated / Σ wall" icon={<IconClock size={20} />} color="cyan" />
      </div>
      {healthErrors.length > 0 && <Alert color="red" icon={<IconAlertTriangle size={18} />} title={`${healthErrors.length} blocking data-health error${healthErrors.length === 1 ? '' : 's'}`}><Stack gap="xs">{healthErrors.map((finding) => <div key={finding.id}><Text fw={650} size="sm">{finding.title}</Text><Text size="sm">{finding.detail}</Text>{finding.dataset_id && <Text size="xs" className="pisa-code">Experiment: {finding.dataset_id}</Text>}</div>)}</Stack></Alert>}
      {healthWarnings.length > 0 && <Alert color="yellow" icon={<IconAlertTriangle size={18} />} title={`${healthWarnings.length} non-blocking data-health finding${healthWarnings.length === 1 ? '' : 's'}`}>Warnings remain visible in Provenance & health and do not replace blocking errors shown above.</Alert>}
      <Card p={0}><Group p="md" justify="space-between"><div><Text fw={650}>Experiment totals</Text><Text size="xs" c="dimmed">Sort any column, or select an experiment name to open it in the single-experiment Scatter Explorer. Missing values remain at the end when sorted.</Text></div><Badge variant="light">{data.experiment_summaries?.length ?? 0} experiments</Badge></Group><ScrollArea><table className="pisa-data-table"><thead><tr>{sortHeader('experiment', 'Experiment')}{sortHeader('system', 'Simulator / AV')}{sortHeader('total_samples', 'Total samples')}{sortHeader('success', 'Success')}{sortHeader('fail', 'Fail')}{sortHeader('invalid', 'Invalid')}{sortHeader('unknown', 'Unknown')}{sortHeader('avg_time_seconds', 'Avg time')}{sortHeader('avg_speedup', 'Avg speedup')}</tr></thead><tbody>{experimentRows.map((item) => <tr key={item.experiment}><td><button type="button" className="pisa-table-link" onClick={() => openExperimentSampling(item.experiment)} title={`Open ${item.experiment} in Scatter Explorer`}><Text fw={600} size="sm">{item.experiment}</Text><Text size="xs" c="dimmed">{item.sampler ?? 'sampler unknown'}</Text></button></td><td>{item.simulator ?? '—'} / {item.av ?? '—'}</td><td>{item.total_samples.toLocaleString()}</td><td>{item.success.toLocaleString()}</td><td>{item.fail.toLocaleString()}</td><td>{item.invalid.toLocaleString()}</td><td>{item.unknown.toLocaleString()}</td><td>{item.avg_time_seconds == null ? '—' : `${item.avg_time_seconds.toFixed(3)} s`}</td><td>{item.avg_speedup == null ? '—' : `${item.avg_speedup.toFixed(2)}×`}</td></tr>)}</tbody></table></ScrollArea></Card>
      <SimpleGrid cols={{ base: 1, lg: 2 }}>{visualizations.map((chart) => <VisualizationCard key={chart.id} spec={chart} datasetId={generatedCharts ? datasetId : undefined} />)}</SimpleGrid>
    </Stack>
  );
}

function ScatterExplorer({ datasetId, onOpen }: { datasetId: string; onOpen: (id: string, experiments?: string[]) => void }) {
  const storageKey = `pisa:sampling:${datasetId}`;
  const [mode, setMode] = useSessionState<string | null>(`${storageKey}:mode`, 'single');
  const [plotView, setPlotView] = useSessionState<string | null>(`${storageKey}:plot-view`, 'scatter');
  const [xBinCount, setXBinCount] = useSessionState<number | string>(`${storageKey}:x-bin-count`, 5);
  const [yBinCount, setYBinCount] = useSessionState<number | string>(`${storageKey}:y-bin-count`, 5);
  const [heatmapXMin, setHeatmapXMin] = useSessionState<number | string>(`${storageKey}:heatmap-x-min`, '');
  const [heatmapXMax, setHeatmapXMax] = useSessionState<number | string>(`${storageKey}:heatmap-x-max`, '');
  const [heatmapYMin, setHeatmapYMin] = useSessionState<number | string>(`${storageKey}:heatmap-y-min`, '');
  const [heatmapYMax, setHeatmapYMax] = useSessionState<number | string>(`${storageKey}:heatmap-y-max`, '');
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
  const [exportAxisValues, setExportAxisValues] = useSessionState(`${storageKey}:axis-values`, true);
  const [exportAxisEndValues, setExportAxisEndValues] = useSessionState(`${storageKey}:axis-end-values`, true);
  const [exportGrid, setExportGrid] = useSessionState(`${storageKey}:grid`, false);
  const [showNumericColorRange, setShowNumericColorRange] = useSessionState(`${storageKey}:numeric-color-range-visible`, true);
  const [numericColorRangeOverrides, setNumericColorRangeOverrides] = useSessionState<Record<string, [string, string, string]>>(`${storageKey}:numeric-color-ranges-v1`, {});
  const [axesLocked, setAxesLocked] = useSessionState(`${storageKey}:axes-locked`, false);
  const [lockedAxes, setLockedAxes] = useSessionState<{ xMin: number; xMax: number; yMin: number; yMax: number } | null>(`${storageKey}:locked-axes`, null);
  const [axisTitleFontSize, setAxisTitleFontSize] = useSessionState<number | string>(`${storageKey}:axis-title-font-size`, 14);
  const [axisTickFontSize, setAxisTickFontSize] = useSessionState<number | string>(`${storageKey}:axis-tick-font-size`, 12);
  const [heatmapCellFontSize, setHeatmapCellFontSize] = useSessionState<number | string>(`${storageKey}:heatmap-cell-font-size`, 12);
  const [showHeatmapCounts, setShowHeatmapCounts] = useSessionState(`${storageKey}:heatmap-show-counts`, true);
  const [showHeatmapPercentage, setShowHeatmapPercentage] = useSessionState(`${storageKey}:heatmap-show-percentage`, true);
  const [distinctShapes, setDistinctShapes] = useSessionState(`${storageKey}:distinct-shapes-v2`, false);
  const [pointSize, setPointSize] = useSessionState<number | string>(`${storageKey}:point-size`, 10);
  const [exportFormat, setExportFormat] = useSessionState<string | null>(`${storageKey}:format`, 'gif');
  const [filterSource, setFilterSource] = useSessionState<string | null>(`${storageKey}:filter-source`, null);
  const [filterField, setFilterField] = useSessionState<string | null>(`${storageKey}:filter-field`, null);
  const [filterRange, setFilterRange] = useSessionState<[number, number] | null>(`${storageKey}:filter-range`, null);
  const [filterRangeField, setFilterRangeField] = useSessionState<string | null>(`${storageKey}:filter-range-field`, null);
  const [filterValues, setFilterValues] = useSessionState<string[]>(`${storageKey}:filter-values`, []);
  const [exportError, setExportError] = useState<string>();
  const [exporting, setExporting] = useState(false);
  const scatter = useQuery({
    queryKey: ['scatter-explorer', datasetId, x, y, color, filterField],
    queryFn: () => api.datasets.scatter(datasetId, { x: x ?? undefined, y: y ?? undefined, color: color ?? 'outcome', filter_field: filterField ?? undefined }),
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
  useEffect(() => {
    if (mode !== 'compare' && plotView === 'disagreement_heatmap') setPlotView('scatter');
  }, [mode, plotView, setPlotView]);
  const axisFields = useMemo(() => (scatter.data?.fields ?? []).filter((field) => field.source !== 'outcome' && field.source !== 'run' && (field.numeric_count == null || field.numeric_count > 0)), [scatter.data?.fields]);
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
  const filterSourceOptions = [...new Set(colorFields.map((field) => field.source))].map((source) => ({ value: source, label: source === 'outcome' ? 'Outcome' : source === 'run' ? 'Run result' : source === 'order' ? 'Recorded order' : source === 'parameter' ? 'Parameters' : source === 'metric' ? 'Metrics' : source === 'control' ? 'Control' : source }));
  const filterFieldOptions = colorFields.filter((field) => field.source === filterSource).map((field) => ({ value: field.key, label: field.label }));
  useEffect(() => {
    const source = colorFields.find((field) => field.key === color)?.source;
    if (source && source !== colorSource) setColorSource(source);
  }, [color, colorFields, colorSource, setColorSource]);
  const selectedX = x ?? scatter.data?.selection.x ?? 'sample_order';
  const selectedY = y ?? scatter.data?.selection.y ?? 'scenario_order';
  const selectedColor = color ?? 'outcome';
  const rawFilterDescription = scatter.data?.filter;
  const [categoryVisibilityContexts, setCategoryVisibilityContexts] = useSessionState<Record<string, Record<string, boolean>>>(`${storageKey}:category-visibility-v1`, {});
  const [categoryColorOverrides, setCategoryColorOverrides] = useSessionState<Record<string, string>>(`${storageKey}:category-colors-v1`, {});
  const [categoryBorderOverrides, setCategoryBorderOverrides] = useSessionState<Record<string, boolean>>(`${storageKey}:category-borders-v1`, {});
  const [categorySymbolOverrides, setCategorySymbolOverrides] = useSessionState<Record<string, string>>(`${storageKey}:category-symbols-v1`, {});
  const rawPoints = scatter.data?.points ?? [];
  const pairedPoints = useMemo(() => {
    if (mode !== 'compare' || !leftDataset || !rightDataset) return [];
    const key = (point: (typeof rawPoints)[number]) => point.parameter_hash || point.sample_id || point.scenario_id;
    const left = new Map(rawPoints.filter((point) => point.dataset_id === leftDataset).map((point) => [key(point), point]));
    return rawPoints.filter((point) => point.dataset_id === rightDataset).flatMap((right) => { const leftPoint = left.get(key(right)); if (!leftPoint) return []; const leftValue = Number(leftPoint.color), rightValue = Number(right.color), numeric = Number.isFinite(leftValue) && Number.isFinite(rightValue) && !['outcome', 'stop_condition', 'stop_reason'].includes(selectedColor); const transition = leftPoint.outcome === right.outcome ? `All ${leftPoint.outcome}` : `${leftPoint.outcome} → ${right.outcome}`; const categoricalColor = selectedColor === 'stop_reason' ? (leftPoint.stop_reason === right.stop_reason ? `Same · ${leftPoint.stop_reason ?? 'Missing'}` : `${leftPoint.stop_reason ?? 'Missing'} → ${right.stop_reason ?? 'Missing'}`) : selectedColor === 'stop_condition' ? (leftPoint.stop_condition === right.stop_condition ? `Same · ${leftPoint.stop_condition ?? 'Missing'}` : `${leftPoint.stop_condition ?? 'Missing'} → ${right.stop_condition ?? 'Missing'}`) : transition; return [{ ...leftPoint, run_id: leftPoint.run_id, left_run_id: leftPoint.run_id, right_run_id: right.run_id, left_outcome: leftPoint.outcome, right_outcome: right.outcome, left_stop_condition: leftPoint.stop_condition, right_stop_condition: right.stop_condition, left_stop_reason: leftPoint.stop_reason, right_stop_reason: right.stop_reason, left_value: numeric ? leftValue : undefined, right_value: numeric ? rightValue : undefined, dataset_id: `${leftDataset} vs ${rightDataset}`, outcome: numeric ? 'delta' : transition, color: numeric ? rightValue - leftValue : categoricalColor }]; });
  }, [leftDataset, mode, rawPoints, rightDataset, selectedColor]);
  const colorDomainPoints = mode === 'compare' ? pairedPoints : rawPoints.filter((point) => !dataset || point.dataset_id === dataset);
  const filterDescription = useMemo(() => {
    if (!rawFilterDescription || rawFilterDescription.field !== filterField) return undefined;
    const present = colorDomainPoints.map((point) => point.filter).filter((value) => value != null);
    if (rawFilterDescription.kind === 'continuous') {
      const values = present.map(Number).filter(Number.isFinite);
      const minimum = values.length ? Math.min(...values) : null;
      const maximum = values.length ? Math.max(...values) : null;
      const span = minimum != null && maximum != null ? maximum - minimum : 0;
      const step = values.length && values.every(Number.isInteger) ? 1 : Math.max(span / 1000, 1e-9);
      return { ...rawFilterDescription, minimum, maximum, step, present_count: values.length, missing_count: colorDomainPoints.length - values.length };
    }
    return { ...rawFilterDescription, values: [...new Set(present.map(String))].sort(), present_count: present.length, missing_count: colorDomainPoints.length - present.length };
  }, [colorDomainPoints, filterField, rawFilterDescription]);
  useEffect(() => {
    if (!filterField || filterDescription?.field !== filterField) return;
    if (filterDescription.kind === 'continuous' && filterDescription.minimum != null && filterDescription.maximum != null) {
      if (filterRangeField !== filterField) {
        setFilterRange([filterDescription.minimum, filterDescription.maximum]);
        setFilterRangeField(filterField);
      } else if (!filterRange) setFilterRange([filterDescription.minimum, filterDescription.maximum]);
    }
  }, [filterDescription, filterField, filterRange, filterRangeField, setFilterRange, setFilterRangeField]);
  const allPoints = useMemo(() => {
    if (!filterField || filterDescription?.field !== filterField) return colorDomainPoints;
    if (filterDescription.kind === 'continuous') {
      if (!filterRange) return colorDomainPoints;
      return colorDomainPoints.filter((point) => {
        const value = Number(point.filter);
        return Number.isFinite(value) && value >= filterRange[0] && value <= filterRange[1];
      });
    }
    if (!filterValues.length) return colorDomainPoints;
    return colorDomainPoints.filter((point) => filterValues.includes(point.filter == null ? '__missing__' : String(point.filter)));
  }, [colorDomainPoints, filterDescription, filterField, filterRange, filterValues]);
  const heatmap = useMemo(() => buildDisagreementHeatmap(allPoints, Number(xBinCount) || 5, Number(yBinCount) || 5, {
    xMin: typeof heatmapXMin === 'number' ? heatmapXMin : undefined,
    xMax: typeof heatmapXMax === 'number' ? heatmapXMax : undefined,
    yMin: typeof heatmapYMin === 'number' ? heatmapYMin : undefined,
    yMax: typeof heatmapYMax === 'number' ? heatmapYMax : undefined,
  }), [allPoints, heatmapXMax, heatmapXMin, heatmapYMax, heatmapYMin, xBinCount, yBinCount]);
  const colorField = scatter.data?.fields.find((field) => field.key === selectedColor);
  const numericColor = colorField?.source === 'metric' || colorField?.source === 'control' || colorField?.source === 'order' || (colorField?.source === 'parameter' && (colorField.numeric_count ?? 0) > 0);
  const numericColorRangeContext = JSON.stringify([mode, selectedColor]);
  const defaultNumericColorRange = mode === 'compare' ? deltaColorRange : sequentialColorRange;
  const numericColorRange = numericColorRangeOverrides[numericColorRangeContext] ?? defaultNumericColorRange;
  const resolvedNumericColorRange = numericColorRange.map((value, index) => /^#[0-9a-f]{6}$/i.test(value) ? value : defaultNumericColorRange[index]) as [string, string, string];
  const setNumericColorRangeValue = (index: number, value: string) => setNumericColorRangeOverrides((current) => {
    const base = current[numericColorRangeContext] ?? defaultNumericColorRange;
    return { ...current, [numericColorRangeContext]: base.map((color, colorIndex) => colorIndex === index ? value : color) as [string, string, string] };
  });
  const resetNumericColorRange = () => setNumericColorRangeOverrides((current) => {
    const next = { ...current };
    delete next[numericColorRangeContext];
    return next;
  });
  const categoryNames = useMemo(() => numericColor ? [] : [...new Set(allPoints.map((point) => scatterCategory(point, selectedColor)))].sort((left, right) => compareScatterCategories(selectedColor, left, right)), [allPoints, numericColor, selectedColor]);
  const visibilityContext = JSON.stringify([mode, selectedColor, mode === 'single' ? dataset : leftDataset, mode === 'compare' ? rightDataset : null]);
  const categoryVisibility = categoryVisibilityContexts[visibilityContext] ?? {};
  const colorOverridePrefix = `${JSON.stringify([mode, selectedColor]).slice(0, -1)},`;
  const colorOverrideKey = (name: string) => `${colorOverridePrefix}${JSON.stringify(name)}]`;
  const activeCategoryStyleOverrides = useMemo(() => Object.fromEntries(categoryNames.flatMap((name) => {
    const key = colorOverrideKey(name);
    const custom: SeriesStyleOverride = {};
    if (categoryColorOverrides[key]) custom.color = categoryColorOverrides[key];
    if (Object.prototype.hasOwnProperty.call(categoryBorderOverrides, key)) custom.border = categoryBorderOverrides[key];
    if (categorySymbolOverrides[key]) custom.symbol = categorySymbolOverrides[key];
    return Object.keys(custom).length ? [[name, custom]] : [];
  })), [categoryBorderOverrides, categoryColorOverrides, categoryNames, categorySymbolOverrides, mode, selectedColor]);
  const hasCategoryStyleOverrides = Object.keys(categoryColorOverrides).some((key) => key.startsWith(colorOverridePrefix))
    || Object.keys(categoryBorderOverrides).some((key) => key.startsWith(colorOverridePrefix))
    || Object.keys(categorySymbolOverrides).some((key) => key.startsWith(colorOverridePrefix));
  const categoryStyles = useMemo(() => Object.fromEntries(
    categoryNames.map((name, index) => {
      const custom = activeCategoryStyleOverrides[name];
      return [name, {
        color: custom?.color ?? outcomeColors[name.toLowerCase()] ?? categoricalContrastPalette[index % categoricalContrastPalette.length],
        border: custom?.border ?? true,
        symbol: custom?.symbol ?? (distinctShapes ? categoricalSymbols[index % categoricalSymbols.length] : 'circle'),
      }];
    }),
  ), [activeCategoryStyleOverrides, categoryNames, distinctShapes]);
  const shownCount = Math.max(0, Math.min(allPoints.length, visibleCount ?? allPoints.length));
  const shownPoints = allPoints.slice(0, shownCount);
  const dynamicAxes = useMemo(() => {
    if (!shownPoints.length) return null;
    const xs = shownPoints.map((point) => point.x), ys = shownPoints.map((point) => point.y);
    const xMinimum = Math.min(...xs), xMaximum = Math.max(...xs), yMinimum = Math.min(...ys), yMaximum = Math.max(...ys);
    const xPadding = Math.max(1e-9, xMaximum - xMinimum) * 0.04;
    const yPadding = Math.max(1e-9, yMaximum - yMinimum) * 0.04;
    return { xMin: xMinimum - xPadding, xMax: xMaximum + xPadding, yMin: yMinimum - yPadding, yMax: yMaximum + yPadding };
  }, [shownPoints]);
  const activeAxes = axesLocked && lockedAxes ? lockedAxes : dynamicAxes;
  const resolvedAxisTitleFontSize = Math.max(8, Math.min(40, Number(axisTitleFontSize) || 14));
  const resolvedAxisTickFontSize = Math.max(8, Math.min(32, Number(axisTickFontSize) || 12));
  const resolvedHeatmapCellFontSize = Math.max(8, Math.min(32, Number(heatmapCellFontSize) || 12));
  const shownCategoryCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    if (!numericColor) for (const point of shownPoints) {
      const name = scatterCategory(point, selectedColor);
      counts[name] = (counts[name] ?? 0) + 1;
    }
    return counts;
  }, [numericColor, selectedColor, shownPoints]);
  const categoryLabels = useMemo(() => Object.fromEntries(categoryNames.map((name) => [name, `${name}(${(shownCategoryCounts[name] ?? 0).toLocaleString()})`])), [categoryNames, shownCategoryCounts]);
  const visiblePointCount = numericColor ? shownPoints.length : shownPoints.reduce((count, point) => count + Number(categoryVisibility[scatterCategory(point, selectedColor)] !== false), 0);
  useEffect(() => {
    if (!playing || !allPoints.length || plotView !== 'scatter') return;
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
  }, [allPoints.length, durationMode, durationSeconds, playing, plotView, pointsPerSecond]);
  const spec = useMemo<VisualizationSpec>(() => {
    if (plotView === 'disagreement_heatmap') {
      return {
        id: `paired-disagreement-heatmap-${selectedX}-${selectedY}`,
        title: 'Paired outcome disagreement rate',
        subtitle: `${heatmap.disagreementCount.toLocaleString()} / ${heatmap.totalCount.toLocaleString()} included paired samples disagree · equal-width bins in the selected original parameter axes${heatmap.excludedCount ? ` · ${heatmap.excludedCount.toLocaleString()} outside the fixed bin domain` : ''}.`,
        kind: 'heatmap',
        option: heatmap.totalCount ? {
          animation: false,
          tooltip: { trigger: 'item', formatter: (params: { data?: { value?: unknown[] } }) => { const value = params.data?.value ?? []; return `${escapeHtml(selectedX)}: ${escapeHtml(heatmap.xLabels[Number(value[0])])}<br/>${escapeHtml(selectedY)}: ${escapeHtml(heatmap.yLabels[Number(value[1])])}<br/>Disagreement: ${escapeHtml(value[3])} / ${escapeHtml(value[4])}<br/>Rate: ${(100 * Number(value[2] ?? 0)).toFixed(1)}%`; } },
          grid: { top: 28, right: 112, bottom: 100, left: 112 },
          xAxis: { type: 'category', show: true, name: exportAxes ? selectedX : '', nameLocation: 'middle', nameGap: 70, nameTextStyle: { fontSize: resolvedAxisTitleFontSize }, data: heatmap.xLabels, splitArea: { show: exportGrid }, axisLine: { show: exportAxes }, axisTick: { show: exportAxes }, axisLabel: { show: exportAxisValues, rotate: heatmap.xLabels.length > 7 ? 35 : 0, fontSize: resolvedAxisTickFontSize } },
          yAxis: { type: 'category', show: true, name: exportAxes ? selectedY : '', nameLocation: 'middle', nameGap: 86, nameTextStyle: { fontSize: resolvedAxisTitleFontSize }, data: heatmap.yLabels, splitArea: { show: exportGrid }, axisLine: { show: exportAxes }, axisTick: { show: exportAxes }, axisLabel: { show: exportAxisValues, fontSize: resolvedAxisTickFontSize } },
          visualMap: { min: 0, max: 1, dimension: 2, calculable: true, orient: 'vertical', right: 12, top: 'middle', formatter: (value: number) => `${(100 * value).toFixed(0)}%`, inRange: { color: ['#f1f3f5', '#74c0fc', '#f59f00', '#c92a2a'] } },
          series: [{
            type: 'heatmap', name: 'Outcome disagreement rate', data: heatmap.cells,
            label: { show: showHeatmapCounts || showHeatmapPercentage, fontSize: resolvedHeatmapCellFontSize, formatter: (params: { data?: { value?: unknown[] } }) => { const value = params.data?.value ?? []; return formatHeatmapCellLabel(Number(value[3] ?? 0), Number(value[4] ?? 0), showHeatmapCounts, showHeatmapPercentage); } },
            itemStyle: { borderColor: '#ffffff', borderWidth: 2 }, emphasis: { itemStyle: { shadowBlur: 8, shadowColor: 'rgba(0,0,0,0.25)' } },
          }],
        } : {},
      };
    }
    const points = shownPoints;
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
      for (const name of categoryNames) {
        const values = groups.get(name);
        if (values?.length) series.push({ type: 'scatter', name, symbol: categoryStyles[name]?.symbol ?? 'circle', symbolSize: Math.max(3, Number(pointSize) || 10), z: 3, itemStyle: { color: categoryStyles[name]?.color ?? '#6b7280', borderColor: '#17202a', borderWidth: categoryStyles[name]?.border ? 1 : 0 }, data: values.map((point) => datum(point, [point.x, point.y])) });
      }
    }
    const numericValues = allPoints.map((point) => Number(point.color ?? point.collision)).filter(Number.isFinite);
    const deltaExtent = mode === 'compare' && numericValues.length ? Math.max(...numericValues.map(Math.abs), 1e-9) : undefined;
    return {
      id: `scatter-explorer-${selectedX}-${selectedY}`, title: 'Scatter explorer',
      subtitle: `${visiblePointCount.toLocaleString()} visible · ${points.length.toLocaleString()} in current prefix · ${allPoints.length.toLocaleString()} total ${mode === 'compare' ? 'paired samples' : 'concrete samples'} · ${axesLocked ? 'axes locked to the captured scale' : 'axes follow the currently filtered points'}.`, kind: 'scatter',
      option: points.length ? {
        animation: false, legend: numericColor ? undefined : { type: 'scroll', top: 0 }, tooltip: { trigger: 'item', appendTo: 'body', confine: false, className: 'pisa-scatter-tooltip', formatter: (params: { data?: Record<string, unknown> }) => { const item = params.data ?? {}; const value = Array.isArray(item.value) ? item.value : []; const paired = item.left_stop_reason !== undefined || item.right_stop_reason !== undefined; return `Sample ${escapeHtml(item.ordinal)}<br/>${escapeHtml(selectedX)}: ${escapeHtml(value[0])}<br/>${escapeHtml(selectedY)}: ${escapeHtml(value[1])}<br/>Outcome: ${escapeHtml(item.outcome)}<br/>${paired ? `Left stop: ${escapeHtml(item.left_stop_condition)} · ${escapeHtml(item.left_stop_reason)}<br/>Right stop: ${escapeHtml(item.right_stop_condition)} · ${escapeHtml(item.right_stop_reason)}<br/>` : `Stop condition: ${escapeHtml(item.stop_condition)}<br/>Stop reason: ${escapeHtml(item.stop_reason)}<br/>`}Run: ${escapeHtml(item.run_id)}`; } },
        grid: { top: 54, right: numericColor && showNumericColorRange ? 90 : 36, bottom: 76, left: 76 },
        xAxis: { type: 'value', show: true, name: exportAxes ? selectedX : '', nameLocation: 'middle', nameGap: 42, nameTextStyle: { fontSize: resolvedAxisTitleFontSize }, scale: true, min: activeAxes?.xMin, max: activeAxes?.xMax, axisLine: { show: exportAxes }, axisTick: { show: exportAxes }, axisLabel: { show: exportAxisValues, showMinLabel: exportAxisEndValues, showMaxLabel: exportAxisEndValues, formatter: formatAxisTick, fontSize: resolvedAxisTickFontSize }, splitLine: { show: exportGrid } }, yAxis: { type: 'value', show: true, name: exportAxes ? selectedY : '', nameLocation: 'middle', nameGap: 54, nameTextStyle: { fontSize: resolvedAxisTitleFontSize }, scale: true, min: activeAxes?.yMin, max: activeAxes?.yMax, axisLine: { show: exportAxes }, axisTick: { show: exportAxes }, axisLabel: { show: exportAxisValues, showMinLabel: exportAxisEndValues, showMaxLabel: exportAxisEndValues, formatter: formatAxisTick, fontSize: resolvedAxisTickFontSize }, splitLine: { show: exportGrid } },
        visualMap: numericColor && numericValues.length ? { show: showNumericColorRange, min: deltaExtent ? -deltaExtent : Math.min(...numericValues), max: deltaExtent ?? Math.max(...numericValues), dimension: 2, seriesIndex: 0, right: 8, top: 65, calculable: true, inRange: { color: resolvedNumericColorRange } } : undefined,
        series,
      } : {},
    };
  }, [activeAxes, allPoints, axesLocked, categoryNames, categoryStyles, colorField, exportAxes, exportAxisEndValues, exportAxisValues, exportGrid, heatmap, mode, numericColor, plotView, pointSize, resolvedAxisTickFontSize, resolvedAxisTitleFontSize, resolvedHeatmapCellFontSize, resolvedNumericColorRange, selectedColor, selectedX, selectedY, showHeatmapCounts, showHeatmapPercentage, showNumericColorRange, shownPoints, visiblePointCount]);
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
  const setCategoryVisible = useCallback((name: string, visible: boolean) => {
    setCategoryVisibilityContexts((current) => ({ ...current, [visibilityContext]: { ...(current[visibilityContext] ?? {}), [name]: visible } }));
  }, [setCategoryVisibilityContexts, visibilityContext]);
  const setCategoryStyle = useCallback((name: string, custom?: SeriesStyleOverride) => {
    const key = colorOverrideKey(name);
    setCategoryColorOverrides((current) => {
      const next = { ...current };
      if (custom?.color) next[key] = custom.color;
      else delete next[key];
      return next;
    });
    setCategoryBorderOverrides((current) => {
      const next = { ...current };
      if (custom?.border !== undefined) next[key] = custom.border;
      else delete next[key];
      return next;
    });
    setCategorySymbolOverrides((current) => {
      const next = { ...current };
      if (custom?.symbol) next[key] = custom.symbol;
      else delete next[key];
      return next;
    });
  }, [mode, selectedColor, setCategoryBorderOverrides, setCategoryColorOverrides, setCategorySymbolOverrides]);
  const resetCategoryStyles = useCallback(() => {
    setCategoryColorOverrides((current) => Object.fromEntries(Object.entries(current).filter(([key]) => !key.startsWith(colorOverridePrefix))));
    setCategoryBorderOverrides((current) => Object.fromEntries(Object.entries(current).filter(([key]) => !key.startsWith(colorOverridePrefix))));
    setCategorySymbolOverrides((current) => Object.fromEntries(Object.entries(current).filter(([key]) => !key.startsWith(colorOverridePrefix))));
  }, [colorOverridePrefix, setCategoryBorderOverrides, setCategoryColorOverrides, setCategorySymbolOverrides]);
  const visibleCategoryCount = categoryNames.filter((name) => categoryVisibility[name] !== false).length;
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
      const xs = allPoints.map((point) => point.x), ys = allPoints.map((point) => point.y), pad = exportAxes || exportAxisValues ? 88 : 24;
      const minX = activeAxes?.xMin ?? Math.min(...xs), maxX = activeAxes?.xMax ?? Math.max(...xs), minY = activeAxes?.yMin ?? Math.min(...ys), maxY = activeAxes?.yMax ?? Math.max(...ys);
      const exportNumericValues = allPoints.map((point) => Number(point.color ?? point.collision)).filter(Number.isFinite);
      const exportDeltaExtent = mode === 'compare' && exportNumericValues.length ? Math.max(...exportNumericValues.map(Math.abs), 1e-9) : undefined;
      const exportColorMinimum = exportDeltaExtent ? -exportDeltaExtent : Math.min(...exportNumericValues);
      const exportColorMaximum = exportDeltaExtent ?? Math.max(...exportNumericValues);
      const sx = (value: number) => pad + (value - minX) / Math.max(1e-9, maxX - minX) * (width - 2 * pad);
      const sy = (value: number) => height - pad - (value - minY) / Math.max(1e-9, maxY - minY) * (height - 2 * pad);
      const drawFrame = (count: number) => {
        context.fillStyle = '#fff'; context.fillRect(0, 0, width, height);
        if (exportGrid) { context.strokeStyle = '#e2e8f0'; context.lineWidth = 1; for (let index = 1; index < 6; index += 1) { const px = pad + index * (width - 2 * pad) / 6, py = pad + index * (height - 2 * pad) / 6; context.beginPath(); context.moveTo(px, pad); context.lineTo(px, height - pad); context.moveTo(pad, py); context.lineTo(width - pad, py); context.stroke(); } }
        if (exportAxes) {
          context.strokeStyle = '#17202a'; context.lineWidth = 2; context.beginPath(); context.moveTo(pad, pad); context.lineTo(pad, height - pad); context.lineTo(width - pad, height - pad); context.stroke();
          context.fillStyle = '#17202a'; context.font = `${resolvedAxisTitleFontSize * 2}px Arial`; context.textAlign = 'center'; context.fillText(selectedX, width / 2, height - 20); context.save(); context.translate(24, height / 2); context.rotate(-Math.PI / 2); context.fillText(selectedY, 0, 0); context.restore();
        }
        if (exportAxisValues) {
          context.fillStyle = '#17202a'; context.font = `${resolvedAxisTickFontSize * 2}px Arial`;
          for (let index = exportAxisEndValues ? 0 : 1; index <= (exportAxisEndValues ? 5 : 4); index += 1) {
            const fraction = index / 5, px = pad + fraction * (width - 2 * pad), py = height - pad - fraction * (height - 2 * pad);
            context.textAlign = 'center'; context.fillText(formatAxisTick(minX + fraction * (maxX - minX)), px, height - pad + resolvedAxisTickFontSize * 2.4);
            context.textAlign = 'right'; context.fillText(formatAxisTick(minY + fraction * (maxY - minY)), pad - 10, py + resolvedAxisTickFontSize * 0.7);
          }
        }
        for (const point of targetPoints.slice(0, count)) {
          const category = scatterCategory(point, selectedColor);
          if (!numericColor && categoryVisibility[category] === false) continue;
          const numericValue = Number(point.color ?? point.collision);
          const px = sx(point.x), py = sy(point.y), style = numericColor
            ? { color: interpolateColorRange(numericValue, exportColorMinimum, exportColorMaximum, resolvedNumericColorRange), border: false, symbol: 'circle' }
            : categoryStyles[category] ?? { color: '#6b7280', border: true, symbol: 'circle' };
          const symbol = style.symbol;
          const radius = Math.max(1.5, (Number(pointSize) || 10) / 2);
          context.beginPath();
          if (symbol === 'rect' || symbol === 'roundRect') context.rect(px - radius, py - radius, radius * 2, radius * 2);
          else if (symbol === 'triangle' || symbol === 'arrow') { context.moveTo(px, py - radius * 1.2); context.lineTo(px + radius * 1.2, py + radius); context.lineTo(px - radius * 1.2, py + radius); context.closePath(); }
          else if (symbol === 'diamond') { context.moveTo(px, py - radius * 1.35); context.lineTo(px + radius * 1.2, py); context.lineTo(px, py + radius * 1.35); context.lineTo(px - radius * 1.2, py); context.closePath(); }
          else if (symbol === 'pin') { context.arc(px, py - radius * 0.4, radius, 0, Math.PI * 2); context.moveTo(px - radius * 0.6, py + radius * 0.4); context.lineTo(px, py + radius * 1.6); context.lineTo(px + radius * 0.6, py + radius * 0.4); }
          else context.arc(px, py, radius, 0, Math.PI * 2);
          context.fillStyle = style.color; context.fill();
          if (style.border) { context.strokeStyle = '#17202a'; context.lineWidth = 1; context.stroke(); }
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
    <Card p="lg"><SimpleGrid cols={{ base: 1, sm: 2, lg: mode === 'single' ? 6 : 7 }} verticalSpacing="sm">
      <Select label="Mode" value={mode} onChange={setMode} allowDeselect={false} data={[{ value: 'single', label: 'Single experiment' }, { value: 'compare', label: 'Compare paired experiments' }]} />
      <Select label="Display" value={plotView} onChange={setPlotView} allowDeselect={false} data={[{ value: 'scatter', label: 'Concrete-sample scatter' }, { value: 'disagreement_heatmap', label: 'Binned disagreement heatmap', disabled: mode !== 'compare' }]} />
      {plotView === 'disagreement_heatmap' ? <Group wrap="nowrap" style={{ gridColumn: 'span 2' }}><NumberInput label="X bins" min={2} max={20} value={xBinCount} onChange={setXBinCount} w="50%" /><NumberInput label="Y bins" min={2} max={20} value={yBinCount} onChange={setYBinCount} w="50%" /></Group> : <Group wrap="nowrap" style={{ gridColumn: 'span 2' }}><Select label="Color type" data={colorSourceOptions} value={colorSource} onChange={(value) => { setColorSource(value); setColor(colorFields.find((field) => field.source === value)?.key ?? 'outcome'); }} allowDeselect={false} w="32%" /><Select label="Color field" searchable data={colorFieldOptions} value={selectedColor} onChange={setColor} allowDeselect={false} style={{ flex: 1, minWidth: 0 }} /></Group>}
      {mode === 'single' ? <Select label="Experiment" data={(scatter.data?.datasets ?? []).map((value) => ({ value, label: value }))} value={dataset} onChange={setDataset} allowDeselect={false} /> : <><Select label="Left experiment" data={(scatter.data?.datasets ?? []).map((value) => ({ value, label: value, disabled: value === rightDataset }))} value={leftDataset} onChange={setLeftDataset} allowDeselect={false} /><Select label="Right experiment" data={(scatter.data?.datasets ?? []).map((value) => ({ value, label: value, disabled: value === leftDataset }))} value={rightDataset} onChange={setRightDataset} allowDeselect={false} /></>}
      <Select label="Plot ratio" value={aspect} onChange={setAspect} allowDeselect={false} data={[{ value: 'fit', label: 'Fit window' }, { value: '1 / 1', label: '1:1 · square' }, { value: '4 / 3', label: '4:3' }, { value: '16 / 9', label: '16:9' }]} />
    </SimpleGrid><SimpleGrid cols={{ base: 1, md: 2 }} mt="md"><Card withBorder p="sm"><Text fw={600} size="sm" mb="xs">X axis</Text><Group wrap="nowrap"><Select label="Type" data={sourceOptions} value={xSource} onChange={(value) => { setXSource(value); setX(axisFields.find((field) => field.source === value)?.key ?? null); }} allowDeselect={false} w="32%" /><Select label="Field" searchable data={xFieldOptions} value={selectedX} onChange={setX} allowDeselect={false} style={{ flex: 1, minWidth: 0 }} /></Group></Card><Card withBorder p="sm"><Text fw={600} size="sm" mb="xs">Y axis</Text><Group wrap="nowrap"><Select label="Type" data={sourceOptions} value={ySource} onChange={(value) => { setYSource(value); setY(axisFields.find((field) => field.source === value)?.key ?? null); }} allowDeselect={false} w="32%" /><Select label="Field" searchable data={yFieldOptions} value={selectedY} onChange={setY} allowDeselect={false} style={{ flex: 1, minWidth: 0 }} /></Group></Card></SimpleGrid>
    {plotView === 'disagreement_heatmap' && <Accordion mt="md" variant="contained"><Accordion.Item value="heatmap-domain"><Accordion.Control><Text fw={600} size="sm">Optional fixed bin domain</Text></Accordion.Control><Accordion.Panel><Text size="xs" c="dimmed" mb="sm">Leave blank to use the observed filtered range. Set both limits on an axis to reproduce fixed publication intervals, such as 5–30 with five bins.</Text><SimpleGrid cols={{ base: 2, md: 4 }}><NumberInput label="X minimum" value={heatmapXMin} onChange={setHeatmapXMin} /><NumberInput label="X maximum" value={heatmapXMax} onChange={setHeatmapXMax} /><NumberInput label="Y minimum" value={heatmapYMin} onChange={setHeatmapYMin} /><NumberInput label="Y maximum" value={heatmapYMax} onChange={setHeatmapYMax} /></SimpleGrid><Button mt="sm" size="compact-xs" variant="default" onClick={() => { setHeatmapXMin(''); setHeatmapXMax(''); setHeatmapYMin(''); setHeatmapYMax(''); }}>Use observed range</Button></Accordion.Panel></Accordion.Item></Accordion>}
    <Accordion mt="md" variant="contained"><Accordion.Item value="scatter-filter"><Accordion.Control><Group gap="xs"><Text fw={600} size="sm">Filter</Text>{filterField && <Badge variant="light">{colorFields.find((field) => field.key === filterField)?.label ?? filterField} · {allPoints.length.toLocaleString()} / {colorDomainPoints.length.toLocaleString()}</Badge>}</Group></Accordion.Control><Accordion.Panel>
      <Group justify="space-between" align="flex-start" wrap="wrap"><Text size="xs" c="dimmed">Restrict the plotted and exported samples by one recorded field.</Text>{filterField && <Button size="compact-xs" variant="subtle" onClick={() => { setFilterSource(null); setFilterField(null); setFilterRange(null); setFilterRangeField(null); setFilterValues([]); }}>Clear filter</Button>}</Group>
      <Group wrap="nowrap" align="flex-end" mt="xs">
        <Select label="Type" placeholder="No filter" clearable data={filterSourceOptions} value={filterSource} onChange={(value) => { setFilterSource(value); const next = colorFields.find((field) => field.source === value)?.key ?? null; setFilterField(next); setFilterRange(null); setFilterRangeField(null); setFilterValues([]); }} w="20%" />
        <Select label="Field" placeholder="Select a type first" searchable disabled={!filterSource} data={filterFieldOptions} value={filterField} onChange={(value) => { setFilterField(value); setFilterRange(null); setFilterRangeField(null); setFilterValues([]); }} allowDeselect={false} w="30%" />
        {filterDescription?.kind === 'continuous' && filterDescription.minimum != null && filterDescription.maximum != null && filterRange && <>
          <div style={{ flex: 1, minWidth: 180 }}>
            <Text size="xs" fw={500} mb={8}>Included range</Text>
            {filterDescription.minimum < filterDescription.maximum ? <HorizontalRangeInput minimum={Math.min(filterDescription.minimum, filterRange[0])} maximum={Math.max(filterDescription.maximum, filterRange[1])} step={filterDescription.step ?? 1} value={filterRange} onChange={setFilterRange} /> : <Text size="sm" c="dimmed">Only {filterDescription.minimum.toLocaleString()} is present.</Text>}
          </div>
          <Group wrap="nowrap" gap="xs">
            <NumberInput label="Min" value={filterRange[0]} step={filterDescription.step ?? 1} onChange={(value) => { if (typeof value === 'number') setFilterRange([Math.min(value, filterRange[1]), filterRange[1]]); }} w={120} />
            <NumberInput label="Max" value={filterRange[1]} step={filterDescription.step ?? 1} onChange={(value) => { if (typeof value === 'number') setFilterRange([filterRange[0], Math.max(value, filterRange[0])]); }} w={120} />
          </Group>
        </>}
        {filterDescription?.kind === 'discrete' && <MultiSelect label="Included values" placeholder="All values" searchable clearable data={[...(filterDescription.values ?? []).map((value) => ({ value, label: value })), ...(filterDescription.missing_count ? [{ value: '__missing__', label: `Missing (${filterDescription.missing_count.toLocaleString()})` }] : [])]} value={filterValues} onChange={setFilterValues} style={{ flex: 1, minWidth: 240 }} />}
      </Group>
      {filterField && filterDescription && <Text size="xs" c="dimmed" mt="xs">{allPoints.length.toLocaleString()} / {colorDomainPoints.length.toLocaleString()} samples remain. {filterDescription.missing_count.toLocaleString()} samples have no value for this field.{mode === 'compare' && !filterField.startsWith('param:') ? ' Paired output fields are filtered using the Left execution; paired parameter values are shared.' : ''}</Text>}
    </Accordion.Panel></Accordion.Item></Accordion>
    <Divider my="md" label="Display options" />
    <Group align="center" wrap="wrap">
      <Checkbox label="Axes" checked={exportAxes} onChange={(event) => setExportAxes(event.currentTarget.checked)} />
      <Checkbox label="Axis values" checked={exportAxisValues} onChange={(event) => setExportAxisValues(event.currentTarget.checked)} />
      {plotView === 'scatter' && <Checkbox label="Axis end values" checked={exportAxisEndValues} disabled={!exportAxisValues} onChange={(event) => setExportAxisEndValues(event.currentTarget.checked)} />}
      <Checkbox label="Grid" checked={exportGrid} onChange={(event) => setExportGrid(event.currentTarget.checked)} />
      {plotView === 'scatter' && <Checkbox label="Fix coordinate axes" checked={axesLocked} disabled={!dynamicAxes} onChange={(event) => { const checked = event.currentTarget.checked; if (checked && dynamicAxes) setLockedAxes(dynamicAxes); setAxesLocked(checked); }} />}
      {plotView === 'scatter' && <Checkbox label="Distinct shapes" checked={distinctShapes} onChange={(event) => setDistinctShapes(event.currentTarget.checked)} />}
      {plotView === 'scatter' && numericColor && <Checkbox label="Color range legend" checked={showNumericColorRange} onChange={(event) => setShowNumericColorRange(event.currentTarget.checked)} />}
      {plotView === 'disagreement_heatmap' && <Checkbox label="Show d/n" checked={showHeatmapCounts} onChange={(event) => setShowHeatmapCounts(event.currentTarget.checked)} />}
      {plotView === 'disagreement_heatmap' && <Checkbox label="Show percentage" checked={showHeatmapPercentage} onChange={(event) => setShowHeatmapPercentage(event.currentTarget.checked)} />}
    </Group>
    <Group align="flex-end" wrap="wrap" mt="sm">
      <NumberInput label="Axis title size" value={axisTitleFontSize} onChange={setAxisTitleFontSize} min={8} max={40} step={1} clampBehavior="strict" w={122} />
      <NumberInput label="Axis tick size" value={axisTickFontSize} onChange={setAxisTickFontSize} min={8} max={32} step={1} clampBehavior="strict" w={116} />
      {plotView === 'scatter' && <NumberInput label="Point size" value={pointSize} onChange={setPointSize} min={3} max={30} step={1} clampBehavior="strict" w={96} />}
      {plotView === 'scatter' && numericColor && <>
        <ColorInput label={mode === 'compare' ? 'Negative color' : 'Low color'} format="hex" value={numericColorRange[0]} onChange={(value) => setNumericColorRangeValue(0, value)} w={132} />
        <ColorInput label={mode === 'compare' ? 'Zero color' : 'Middle color'} format="hex" value={numericColorRange[1]} onChange={(value) => setNumericColorRangeValue(1, value)} w={132} />
        <ColorInput label={mode === 'compare' ? 'Positive color' : 'High color'} format="hex" value={numericColorRange[2]} onChange={(value) => setNumericColorRangeValue(2, value)} w={132} />
        <Button variant="default" onClick={resetNumericColorRange}>Reset color range</Button>
      </>}
      {plotView === 'disagreement_heatmap' && <NumberInput label="Cell text size" value={heatmapCellFontSize} onChange={setHeatmapCellFontSize} min={8} max={32} step={1} clampBehavior="strict" w={116} />}
      <Button variant="default" onClick={() => {
        setExportAxes(true); setExportAxisValues(true); setExportAxisEndValues(true); setExportGrid(false); setShowNumericColorRange(true); setAxesLocked(false); setLockedAxes(null); setDistinctShapes(false); setPointSize(10); resetNumericColorRange();
        setAxisTitleFontSize(14); setAxisTickFontSize(12); setHeatmapCellFontSize(12); setShowHeatmapCounts(true); setShowHeatmapPercentage(true);
      }}>Reset display</Button>
      {plotView === 'scatter' && axesLocked && lockedAxes && <Text size="xs" c="dimmed">Locked at X {formatAxisTick(lockedAxes.xMin)}–{formatAxisTick(lockedAxes.xMax)}, Y {formatAxisTick(lockedAxes.yMin)}–{formatAxisTick(lockedAxes.yMax)}</Text>}
    </Group>
    {plotView === 'scatter' && <Accordion mt="md" variant="contained"><Accordion.Item value="sample-sequence"><Accordion.Control><Group gap="xs"><Text fw={600} size="sm">Sample sequence and animation export</Text><Badge variant="light">First {shownCount.toLocaleString()} / {allPoints.length.toLocaleString()}</Badge></Group></Accordion.Control><Accordion.Panel>
      <Group justify="space-between" mb="xs"><Text size="sm" fw={600}>First {shownCount.toLocaleString()} samples</Text><Text size="xs" c="dimmed">{allPoints.length.toLocaleString()} filtered samples</Text></Group>
      <input className="pisa-horizontal-range pisa-sample-count-range" type="range" aria-label="Visible sample count" value={shownCount} onChange={(event) => { setPlaying(false); setVisibleCount(Number(event.currentTarget.value)); }} min={0} max={Math.max(1, allPoints.length)} step={1} />
      <Group justify="space-between" align="flex-end" mt="md" wrap="wrap"><Group><Button size="sm" variant={playing ? 'filled' : 'light'} onClick={() => setPlaying((value) => !value)} leftSection={<IconPlayerPlay size={16} />}>{playing ? 'Pause sequence' : 'Play sample sequence'}</Button><Button size="sm" variant="default" onClick={() => { setPlaying(false); setVisibleCount(allPoints.length); }}>Show all</Button></Group><Group justify="flex-end" align="flex-end" wrap="wrap"><Select label="Format" value={exportFormat} onChange={setExportFormat} allowDeselect={false} data={[{ value: 'png', label: 'PNG · current frame' }, { value: 'gif', label: 'GIF animation' }, { value: 'mp4', label: 'MP4 animation' }, { value: 'webm', label: 'WebM animation' }]} />{exportFormat !== 'png' && <Select label="Timing" value={durationMode} onChange={setDurationMode} allowDeselect={false} data={[{ value: 'duration', label: 'Total duration' }, { value: 'rate', label: 'Points per second' }]} />}{exportFormat !== 'png' && (durationMode === 'rate' ? <NumberInput label="Points / second" value={pointsPerSecond} onChange={setPointsPerSecond} min={0.1} /> : <NumberInput label="Total seconds" value={durationSeconds} onChange={setDurationSeconds} min={0.25} />)}<Button loading={exporting} onClick={() => void exportAnimation()} leftSection={<IconDownload size={16} />}>Export first {shownCount}</Button></Group></Group>
      {exportError && <Text role="alert" c="red" size="xs" mt="sm">{exportError}</Text>}
    </Accordion.Panel></Accordion.Item></Accordion>}
    {plotView === 'disagreement_heatmap' && <Text size="xs" c="dimmed" mt="md">The chart export menu provides PNG, SVG, CSV, and JSON for the complete filtered heatmap.</Text>}
    </Card>
    {plotView === 'scatter' && !numericColor && categoryNames.length > 0 && <Group justify="space-between" align="center" wrap="wrap"><Text size="xs" c="dimmed">{visiblePointCount.toLocaleString()} / {shownPoints.length.toLocaleString()} points visible · {visibleCategoryCount} / {categoryNames.length} categories shown · click a style marker to change its color, border, or shape</Text><Group gap="xs"><Button size="compact-xs" variant="default" disabled={visibleCategoryCount === categoryNames.length} onClick={() => setCategoryVisibilityContexts((current) => ({ ...current, [visibilityContext]: {} }))}>Reset filters</Button><Button size="compact-xs" variant="default" disabled={!hasCategoryStyleOverrides} onClick={resetCategoryStyles}>Reset category styles</Button></Group></Group>}
    {scatter.isLoading ? <PageLoading label="Loading concrete sample space…" /> : scatter.error ? <InlineError error={scatter.error} onRetry={() => scatter.refetch()} /> : <VisualizationCard spec={spec} aspectRatio={aspect === 'fit' ? undefined : aspect ?? '1 / 1'} onPointClick={plotView === 'scatter' ? handlePoint : undefined} seriesVisibility={plotView === 'scatter' ? categoryVisibility : undefined} onSeriesVisibilityChange={plotView === 'scatter' ? setCategoryVisible : undefined} seriesStyleOverrides={plotView === 'scatter' && !numericColor ? activeCategoryStyleOverrides : undefined} onSeriesStyleChange={plotView === 'scatter' && !numericColor ? setCategoryStyle : undefined} seriesLabels={plotView === 'scatter' && !numericColor ? categoryLabels : undefined} emptyDescription="No concrete samples contain both selected numeric fields." />}
    {plotView === 'scatter' && representatives.length > 0 && <Card p="lg"><Group justify="space-between" mb="md"><div><Text fw={650}>Representative concrete cases</Text><Text size="xs" c="dimmed">{mode === 'compare' ? 'Paired outcome differences or the largest selected-metric deltas.' : 'Outcome, center, and boundary representatives from the complete selected experiment set.'}</Text></div><Badge variant="light">{representatives.length} cases</Badge></Group><SimpleGrid cols={{ base: 1, md: 2, xl: 3 }}>{representatives.map(({ label, reason, point }) => <Card key={`${label}-${point.run_id}`} withBorder p="md"><Group justify="space-between"><Text fw={600} size="sm">{label}</Text><StatusBadge value={point.outcome} /></Group><Text size="xs" c="dimmed" mt={4}>{reason}</Text><Text size="xs" className="pisa-code" mt="sm">sample {point.ordinal} · ({point.x.toPrecision(5)}, {point.y.toPrecision(5)})</Text><Button mt="sm" size="compact-sm" variant="light" onClick={() => onOpen(point.run_id, mode === 'compare' ? [leftDataset, rightDataset].filter((item): item is string => Boolean(item)) : [point.dataset_id])}>{mode === 'compare' ? 'Open paired replay' : 'Open in replay'}</Button></Card>)}</SimpleGrid></Card>}
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

function CrossExperimentOverview({ datasetId, summary, onOpen }: { datasetId: string; summary?: CrossExperimentComparison; onOpen: (id: string, experiments?: string[]) => void }) {
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
    {!summary.trajectory?.available ? <Alert color="gray" mb="xl">Trajectory comparison unavailable: {summary.trajectory?.reason?.replaceAll('_', ' ') ?? 'ego trajectory paths were not indexed'}.{summary.trajectory?.reason === 'deep_consistency_required' && <> Generate the trace-level result from the <Anchor component={Link} to={`/reports/${encodeURIComponent(datasetId)}/consistency`}>Consistency tab</Anchor>.</>}</Alert> : <ScrollArea mb="xl" type="auto"><table className="pisa-data-table"><thead><tr><th>Measure</th><th>Eligible samples</th><th>Max</th><th>Min</th><th>Mean</th><th>Population std</th><th>Median</th></tr></thead><tbody>{[summary.trajectory.ade, summary.trajectory.fde].filter((value): value is NonNullable<typeof value> => Boolean(value)).map((metric) => {
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

const pairedCategoryLabels: Record<string, string> = {
  same_outcome: 'Same outcome',
  left_success_right_fail: 'Left Success / Right Fail',
  left_fail_right_success: 'Left Fail / Right Success',
  other_disagreement: 'Other disagreement',
};

const pairedCategoryColors: Record<string, string> = {
  same_outcome: '#a9b1bd',
  left_success_right_fail: '#d9485f',
  left_fail_right_success: '#168f77',
  other_disagreement: '#d68b18',
};

const agreementCategoryStyles: Record<string, Required<SeriesStyleOverride>> = {
  'Success / Success': { color: '#20a486', border: true, symbol: 'circle' },
  'Fail / Fail': { color: '#e25555', border: true, symbol: 'triangle' },
  'Invalid / Invalid': { color: '#f59f00', border: true, symbol: 'diamond' },
  'Unknown / Unknown': { color: '#8b95a5', border: true, symbol: 'rect' },
};

const agreementCategoryLabels: Record<string, string> = {
  success_success: 'Success / Success',
  fail_fail: 'Fail / Fail',
  invalid_invalid: 'Invalid / Invalid',
  unknown_unknown: 'Unknown / Unknown',
};

export function pairedAgreementBoundarySegment(minimum: number, maximum: number, offset: number): [[number, number], [number, number]] | undefined {
  const start = Math.max(minimum, minimum - offset);
  const end = Math.min(maximum, maximum - offset);
  return start <= end ? [[start, start + offset], [end, end + offset]] : undefined;
}

function pairedExperimentLabel(dataset: string): string {
  const lowered = dataset.toLowerCase();
  if (lowered.includes('autoware')) return 'Autoware';
  if (lowered.includes('plant')) return 'PlanT';
  if (lowered.includes('carla_agent') || lowered.includes('behavior')) return 'Behavior Agent';
  if (lowered.includes('simple')) return 'Simple Agent';
  return dataset;
}

function PairedMetricAgreementView({ datasetId, comparison, onOpen }: { datasetId: string; comparison: ComparisonClass; onOpen: (id: string, experiments?: string[]) => void }) {
  const storageKey = `pisa:paired-metric-agreement:${datasetId}:${comparison.id}`;
  const [metric, setMetric] = useSessionState<string | null>(`${storageKey}:metric`, null);
  const [xSide, setXSide] = useSessionState<string | null>(`${storageKey}:x-side`, 'right');
  const [outcomeScope, setOutcomeScope] = useSessionState<string | null>(`${storageKey}:outcome-scope`, 'all_same');
  const [primaryThreshold, setPrimaryThreshold] = useSessionState<number | string>(`${storageKey}:primary-threshold`, 5);
  const [secondaryThreshold, setSecondaryThreshold] = useSessionState<number | string>(`${storageKey}:secondary-threshold`, 10);
  const [showEquality, setShowEquality] = useSessionState(`${storageKey}:show-equality`, true);
  const [showPrimary, setShowPrimary] = useSessionState(`${storageKey}:show-primary`, true);
  const [showSecondary, setShowSecondary] = useSessionState(`${storageKey}:show-secondary`, true);
  const [styleOverrides, setStyleOverrides] = useSessionState<Record<string, SeriesStyleOverride>>(`${storageKey}:category-styles-v1`, {});
  const primary = Number(primaryThreshold), secondary = Number(secondaryThreshold);
  const thresholdsValid = Number.isFinite(primary) && primary > 0 && Number.isFinite(secondary) && secondary > primary;
  const request = useMemo(() => ({
    ...(metric ? { metric } : {}),
    x_side: xSide === 'left' ? 'left' as const : 'right' as const,
    outcome_scope: ['success', 'fail', 'invalid', 'unknown'].includes(outcomeScope ?? '') ? outcomeScope as 'success' | 'fail' | 'invalid' | 'unknown' : 'all_same' as const,
    primary_threshold: primary,
    secondary_threshold: secondary,
  }), [metric, outcomeScope, primary, secondary, xSide]);
  const analysis = useQuery({
    queryKey: ['paired-metric-agreement', datasetId, comparison.id, request],
    queryFn: () => api.datasets.pairedMetricAgreement(datasetId, comparison.id, request),
    enabled: thresholdsValid,
    retry: 1,
  });
  useEffect(() => {
    if (!metric && analysis.data?.selection.metric) setMetric(analysis.data.selection.metric);
  }, [analysis.data?.selection.metric, metric, setMetric]);
  const setCategoryStyle = useCallback((name: string, style?: SeriesStyleOverride) => {
    setStyleOverrides((current) => {
      const next = { ...current };
      if (style && Object.keys(style).length) next[name] = style;
      else delete next[name];
      return next;
    });
  }, [setStyleOverrides]);
  const exportData = (format: 'csv' | 'json', data: PairedMetricAgreementResult) => {
    const csvCell = (value: unknown) => { const text = String(value ?? ''); return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text; };
    const content = format === 'json'
      ? `${JSON.stringify({ schema: 'pisa.paired-metric-agreement/v1', request, result: data }, null, 2)}\n`
      : [
        ['parameter_hash', 'left_run_id', 'right_run_id', 'left_outcome', 'right_outcome', 'left_value', 'right_value', 'x', 'y', 'y_minus_x', 'absolute_difference'],
        ...data.points.map((point) => [point.parameter_hash, point.left_run_id, point.right_run_id, point.left_outcome, point.right_outcome, point.left_value, point.right_value, point.x, point.y, point.y_minus_x, point.absolute_difference]),
      ].map((row) => row.map(csvCell).join(',')).join('\r\n');
    const blob = new Blob([content], { type: format === 'json' ? 'application/json' : 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob), anchor = document.createElement('a');
    anchor.href = url; anchor.download = `paired-metric-agreement-${comparison.id}.${format}`; anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  if (!thresholdsValid) return <Card withBorder p="md"><Alert color="red" title="Invalid difference boundaries">Primary must be greater than zero, and secondary must be greater than primary.</Alert><Group mt="md"><NumberInput label="Primary difference" value={primaryThreshold} onChange={setPrimaryThreshold} min={0.000001} /><NumberInput label="Secondary difference" value={secondaryThreshold} onChange={setSecondaryThreshold} min={0.000001} /></Group></Card>;
  if (analysis.isLoading) return <PageLoading label="Computing paired metric agreement…" />;
  if (analysis.error) return <InlineError error={analysis.error} onRetry={() => analysis.refetch()} />;
  const data = analysis.data!;
  const unit = data.selection.unit ? ` ${data.selection.unit}` : '';
  const metricDescriptor = data.metrics.find((item) => item.key === data.selection.metric);
  const metricLabel = metricDescriptor?.label ?? data.selection.metric;
  const values = data.points.flatMap((point) => [point.x, point.y]).filter(Number.isFinite);
  const observedMinimum = values.length ? Math.min(...values) : 0, observedMaximum = values.length ? Math.max(...values) : 1;
  const span = Math.max(1e-9, observedMaximum - observedMinimum);
  const domainMinimum = observedMinimum - span * 0.04, domainMaximum = observedMaximum + span * 0.04;
  const guide = (offset: number, label: string, color: string, type: 'solid' | 'dashed' | 'dotted', opacity: number, width: number) => {
    const segment = pairedAgreementBoundarySegment(domainMinimum, domainMaximum, offset);
    return segment ? [[{ coord: segment[0], name: label, lineStyle: { color, type, opacity, width }, label: { show: true, formatter: label, color } }, { coord: segment[1] }]] : [];
  };
  const guideData = [
    ...(showEquality ? guide(0, 'y = x', '#17202a', 'solid', 0.9, 1.8) : []),
    ...(showPrimary ? guide(primary, `y = x + ${primary}${unit}`, '#526ff0', 'dashed', 0.85, 1.5) : []),
    ...(showPrimary ? guide(-primary, `y = x − ${primary}${unit}`, '#526ff0', 'dashed', 0.85, 1.5) : []),
    ...(showSecondary ? guide(secondary, `y = x + ${secondary}${unit}`, '#8791a5', 'dotted', 0.5, 1.2) : []),
    ...(showSecondary ? guide(-secondary, `y = x − ${secondary}${unit}`, '#8791a5', 'dotted', 0.5, 1.2) : []),
  ];
  const activeCategories = Object.entries(agreementCategoryLabels).filter(([category]) => data.points.some((point) => point.category === category));
  const resolvedStyles = Object.fromEntries(activeCategories.map(([, label]) => [label, { ...agreementCategoryStyles[label], ...styleOverrides[label] }]));
  const series = activeCategories.map(([category, label]) => ({
    type: 'scatter', name: label, symbol: resolvedStyles[label].symbol, symbolSize: 10,
    itemStyle: { color: resolvedStyles[label].color, borderColor: '#17202a', borderWidth: resolvedStyles[label].border ? 1 : 0, opacity: 0.82 },
    data: data.points.filter((point) => point.category === category).map((point) => ({ value: [point.x, point.y], ...point })),
  }));
  const spec: VisualizationSpec = {
    id: `paired-metric-agreement-${comparison.id}-${data.selection.metric}`,
    title: 'Paired metric agreement',
    subtitle: `${data.summary.included.count.toLocaleString()} same-outcome pairs · shared metric ${data.selection.metric} · exact statistics use the complete eligible population.`,
    kind: 'scatter',
    option: data.points.length ? {
      animation: false,
      legend: { type: 'scroll', top: 0, data: activeCategories.map(([, label]) => label) },
      tooltip: { trigger: 'item', formatter: (params: { data?: Record<string, unknown> }) => { const point = params.data ?? {}; return `${escapeHtml(data.selection.x_dataset)}: ${escapeHtml(point.x)}${escapeHtml(unit)}<br/>${escapeHtml(data.selection.y_dataset)}: ${escapeHtml(point.y)}${escapeHtml(unit)}<br/>y − x: ${escapeHtml(point.y_minus_x)}${escapeHtml(unit)}<br/>|Δ|: ${escapeHtml(point.absolute_difference)}${escapeHtml(unit)}<br/>Outcome: ${escapeHtml(point.left_outcome)} / ${escapeHtml(point.right_outcome)}<br/>Pair: ${escapeHtml(point.parameter_hash)}`; } },
      grid: { left: '15%', top: '12%', width: '70%', height: '70%' },
      xAxis: { type: 'value', min: domainMinimum, max: domainMaximum, scale: true, name: `${pairedExperimentLabel(data.selection.x_dataset)} · ${metricLabel}${unit}`, nameLocation: 'middle', nameGap: 44 },
      yAxis: { type: 'value', min: domainMinimum, max: domainMaximum, scale: true, name: `${pairedExperimentLabel(data.selection.y_dataset)} · ${metricLabel}${unit}`, nameLocation: 'middle', nameGap: 58 },
      series: [{ type: 'line', name: '__agreement_guides', data: [], silent: true, symbol: 'none', markLine: { silent: true, symbol: 'none', data: guideData } }, ...series],
    } : {},
  };
  const primarySummary = data.summary.included.thresholds[0], secondarySummary = data.summary.included.thresholds[1];
  return <Stack gap="lg">
    <Alert color="blue" icon={<IconShieldCheck size={17} />} title="Recorded paired output metric">Only uniquely paired executions with equal outcomes and finite values on both sides are plotted. No derived scenario parameters are introduced.</Alert>
    <Card withBorder p="md"><SimpleGrid cols={{ base: 1, sm: 2, xl: 5 }}>
      <Select label="Shared metric" searchable data={data.metrics.map((item) => ({ value: item.key, label: `${item.label}${item.unit ? ` (${item.unit})` : ''}` }))} value={data.selection.metric} onChange={setMetric} />
      <Select label="X experiment" data={[{ value: 'left', label: pairedExperimentLabel(data.left) }, { value: 'right', label: pairedExperimentLabel(data.right) }]} value={data.selection.x_side} onChange={setXSide} allowDeselect={false} />
      <Select label="Outcome scope" data={[{ value: 'all_same', label: 'All same outcomes' }, { value: 'success', label: 'Success / Success' }, { value: 'fail', label: 'Fail / Fail' }, { value: 'invalid', label: 'Invalid / Invalid' }, { value: 'unknown', label: 'Unknown / Unknown' }]} value={data.selection.outcome_scope} onChange={setOutcomeScope} allowDeselect={false} />
      <NumberInput label={`Primary difference${unit ? ` (${unit.trim()})` : ''}`} value={primaryThreshold} onChange={setPrimaryThreshold} min={0.000001} />
      <NumberInput label={`Secondary difference${unit ? ` (${unit.trim()})` : ''}`} value={secondaryThreshold} onChange={setSecondaryThreshold} min={0.000001} />
    </SimpleGrid><Group mt="md"><Checkbox label="Equality line" checked={showEquality} onChange={(event) => setShowEquality(event.currentTarget.checked)} /><Checkbox label="Primary boundaries" checked={showPrimary} onChange={(event) => setShowPrimary(event.currentTarget.checked)} /><Checkbox label="Secondary boundaries" checked={showSecondary} onChange={(event) => setShowSecondary(event.currentTarget.checked)} /></Group></Card>
    <SimpleGrid cols={{ base: 2, md: 4 }}>
      <MetricCard label="Included pairs" value={data.summary.included.count.toLocaleString()} detail={data.selection.outcome_scope.replaceAll('_', ' ')} icon={<IconDatabase size={18} />} />
      <MetricCard label={`|Δ| ≥ ${primary}${unit}`} value={primarySummary.count.toLocaleString()} detail={`${((primarySummary.rate ?? 0) * 100).toFixed(1)}% of included`} icon={<IconArrowsSort size={18} />} color="yellow" />
      <MetricCard label={`|Δ| ≥ ${secondary}${unit}`} value={secondarySummary.count.toLocaleString()} detail={`${((secondarySummary.rate ?? 0) * 100).toFixed(1)}% of included`} icon={<IconArrowsSort size={18} />} color="red" />
      <MetricCard label="Metric missing" value={data.summary.metric_missing_count.toLocaleString()} detail={`${data.summary.outcome_disagreement_metric_eligible_count.toLocaleString()} eligible outcome disagreements excluded`} icon={<IconAlertTriangle size={18} />} color="gray" />
    </SimpleGrid>
    <Card withBorder p="md"><Group justify="space-between" wrap="wrap"><Text size="sm">{primarySummary.count.toLocaleString()} of {data.summary.included.count.toLocaleString()} pairs differ by at least {primary}{unit}; {secondarySummary.count.toLocaleString()} differ by at least {secondary}{unit}.</Text><Group gap="xs"><Button size="compact-xs" variant="default" disabled={!Object.keys(styleOverrides).length} onClick={() => setStyleOverrides({})}>Reset category styles</Button><Button size="compact-xs" variant="default" leftSection={<IconDownload size={14} />} onClick={() => exportData('csv', data)}>Paired CSV</Button><Button size="compact-xs" variant="default" leftSection={<IconDownload size={14} />} onClick={() => exportData('json', data)}>Analysis JSON</Button></Group></Group></Card>
    <VisualizationCard spec={spec} aspectRatio="1 / 1" seriesStyleOverrides={styleOverrides} onSeriesStyleChange={setCategoryStyle} onPointClick={(value) => { const record = chartRecord(value); if (typeof record?.left_run_id === 'string') onOpen(record.left_run_id, [data.left, data.right]); }} emptyDescription="No same-outcome pair has finite values for the selected metric." />
    <Card p={0}><Group p="md" justify="space-between"><div><Text fw={650}>Same-outcome metric differences</Text><Text size="xs" c="dimmed">Counts use the complete eligible population, including points omitted by any plot limit.</Text></div><Badge variant="light">{data.summary.same_outcome_metric_eligible_count.toLocaleString()} eligible</Badge></Group><ScrollArea><table className="pisa-data-table"><thead><tr><th>Outcome pair</th><th>Pairs</th><th>≥ {primary}{unit}</th><th>≥ {secondary}{unit}</th></tr></thead><tbody>{['success', 'fail', 'invalid', 'unknown'].filter((name) => data.summary.categories[name]?.count).map((name) => { const summary = data.summary.categories[name]; return <tr key={name}><td>{agreementCategoryLabels[`${name}_${name}`]}</td><td>{summary.count.toLocaleString()}</td><td>{summary.thresholds[0].count.toLocaleString()} · {((summary.thresholds[0].rate ?? 0) * 100).toFixed(1)}%</td><td>{summary.thresholds[1].count.toLocaleString()} · {((summary.thresholds[1].rate ?? 0) * 100).toFixed(1)}%</td></tr>; })}</tbody></table></ScrollArea></Card>
  </Stack>;
}

function intervalLabel(lower: number, upper: number, inclusive: boolean): string {
  return `[${lower.toLocaleString(undefined, { maximumSignificantDigits: 5 })}, ${upper.toLocaleString(undefined, { maximumSignificantDigits: 5 })}${inclusive ? ']' : ')'}`;
}

function PairedParameterAnalysis({ datasetId, comparison, onOpen }: { datasetId: string; comparison: ComparisonClass; onOpen: (id: string, experiments?: string[]) => void }) {
  const storageKey = `pisa:paired-parameters:${datasetId}:${comparison.id}`;
  const [x, setX] = useSessionState<string | null>(`${storageKey}:x`, null);
  const [y, setY] = useSessionState<string | null>(`${storageKey}:y`, null);
  const [facet, setFacet] = useSessionState<string | null>(`${storageKey}:facet`, null);
  const [view, setView] = useSessionState<string | null>(`${storageKey}:view`, 'outcome');
  const [metric, setMetric] = useSessionState<string | null>(`${storageKey}:metric`, null);
  const [binCount, setBinCount] = useSessionState<number | string>(`${storageKey}:bins`, 5);
  const [minimumCellCount, setMinimumCellCount] = useSessionState<number | string>(`${storageKey}:minimum-cell`, Math.max(10, Math.ceil(comparison.matched * 0.01)));
  const [boundaryDrafts, setBoundaryDrafts] = useState<Record<string, string>>({});
  const [boundaries, setBoundaries] = useSessionState<Record<string, number[]>>(`${storageKey}:boundaries`, {});
  const [boundaryError, setBoundaryError] = useState<string>();
  const request = useMemo(() => ({
    ...(x ? { x } : {}), ...(y ? { y } : {}), ...(facet !== null ? { facet } : {}),
    view: view === 'metric_delta' ? 'metric_delta' as const : 'outcome' as const,
    ...(view === 'metric_delta' && metric ? { metric } : {}),
    bin_count: Math.max(2, Math.min(20, Number(binCount) || 5)),
    minimum_cell_count: Math.max(1, Number(minimumCellCount) || 10),
    boundaries,
  }), [binCount, boundaries, facet, metric, minimumCellCount, view, x, y]);
  const analysis = useQuery({
    queryKey: ['paired-parameter-analysis', datasetId, comparison.id, request],
    queryFn: () => api.datasets.pairedParameterAnalysis(datasetId, comparison.id, request),
    enabled: view !== 'metric_agreement',
    retry: 1,
  });
  useEffect(() => {
    const data = analysis.data;
    if (!data) return;
    if (!x) setX(data.selection.x);
    if (!y) setY(data.selection.y);
    if (facet === null && data.selection.facet) setFacet(data.selection.facet);
    if (view === 'metric_delta' && !metric && data.selection.metric) setMetric(data.selection.metric);
  }, [analysis.data, facet, metric, setFacet, setMetric, setX, setY, view, x, y]);
  if (view === 'metric_agreement') return <Stack gap="lg" mt="md">
    <Group justify="space-between"><Text fw={700}>Paired analysis</Text></Group>
    <Card withBorder p="md"><Select label="View" data={[{ value: 'outcome', label: 'Outcome disagreement in parameter space' }, { value: 'metric_delta', label: 'Metric delta in parameter space' }, { value: 'metric_agreement', label: 'Paired metric agreement' }]} value={view} onChange={setView} allowDeselect={false} /></Card>
    <PairedMetricAgreementView datasetId={datasetId} comparison={comparison} onOpen={onOpen} />
  </Stack>;
  if (analysis.isLoading) return <PageLoading label="Computing paired parameter regions…" />;
  if (analysis.error) return <InlineError error={analysis.error} onRetry={() => analysis.refetch()} />;
  const data = analysis.data!;
  const parameterOptions = data.parameters.map((name) => ({ value: name, label: name }));
  const availableY = parameterOptions.filter((item) => item.value !== (x ?? data.selection.x));
  const availableFacets = [{ value: '', label: 'No facet' }, ...parameterOptions.filter((item) => ![x ?? data.selection.x, y ?? data.selection.y].includes(item.value))];
  const activeParameters = [...new Set([data.selection.x, data.selection.y, data.selection.facet].filter((value): value is string => Boolean(value)))];
  const outcomeSpec: VisualizationSpec = {
    id: `paired-parameter-points-${comparison.id}`,
    title: view === 'metric_delta' ? `Paired Δ ${data.selection.metric}` : 'Outcome disagreement map',
    subtitle: `${data.coverage.plotted_count.toLocaleString()} plotted / ${data.coverage.included_count.toLocaleString()} included pairs · axes use recorded original parameters only.`,
    kind: 'scatter',
    option: view === 'metric_delta' ? (() => {
      const eligible = data.points.filter((point) => point.delta != null && Number.isFinite(point.delta));
      const extent = Math.max(...eligible.map((point) => Math.abs(point.delta!)), 1e-9);
      return { animation: false, tooltip: { trigger: 'item' }, grid: { top: 32, right: 96, bottom: 68, left: 76 }, xAxis: { type: 'value', name: data.selection.x, nameLocation: 'middle', nameGap: 40 }, yAxis: { type: 'value', name: data.selection.y, nameLocation: 'middle', nameGap: 52 }, visualMap: { min: -extent, max: extent, dimension: 2, right: 8, top: 40, calculable: true, inRange: { color: ['#0796a5', '#f8fafc', '#8e3b9d'] } }, series: [{ type: 'scatter', symbolSize: 10, data: eligible.map((point) => ({ value: [point.x, point.y, point.delta], left_run_id: point.left_run_id, right_run_id: point.right_run_id, category: point.category })) }] };
    })() : {
      animation: false, legend: { type: 'scroll', top: 0 }, tooltip: { trigger: 'item' }, grid: { top: 52, right: 32, bottom: 68, left: 76 }, xAxis: { type: 'value', name: data.selection.x, nameLocation: 'middle', nameGap: 40 }, yAxis: { type: 'value', name: data.selection.y, nameLocation: 'middle', nameGap: 52 }, series: Object.keys(pairedCategoryLabels).map((category) => ({ type: 'scatter', name: pairedCategoryLabels[category], symbolSize: category === 'same_outcome' ? 7 : 11, itemStyle: { color: pairedCategoryColors[category], opacity: category === 'same_outcome' ? 0.45 : 0.9 }, data: data.points.filter((point) => point.category === category).map((point) => ({ value: [point.x, point.y], left_run_id: point.left_run_id, right_run_id: point.right_run_id, category })) })),
    },
  };
  const heatmapSpecs: VisualizationSpec[] = data.heatmaps.map((heatmap, index) => {
    const metricMode = view === 'metric_delta';
    const values = heatmap.cells.flatMap((cell) => {
      const value = metricMode ? cell.delta_median : cell.disagreement_rate == null ? null : 100 * cell.disagreement_rate;
      return value == null ? [] : [{ value: [cell.x_index, cell.y_index, value, cell.total, cell.disagreement_count, cell.metric_eligible_count, cell.sparse ? 1 : 0], itemStyle: { opacity: cell.sparse ? 0.35 : 1 } }];
    });
    const extent = metricMode ? Math.max(...values.map((item) => Math.abs(Number(item.value[2]))), 1e-9) : 100;
    const facetText = heatmap.facet_interval ? `${heatmap.facet} ${intervalLabel(heatmap.facet_interval.lower, heatmap.facet_interval.upper, heatmap.facet_interval.upper_inclusive)}` : 'All paired samples';
    const labels = (edges: number[]) => edges.slice(0, -1).map((lower, edgeIndex) => intervalLabel(lower, edges[edgeIndex + 1], edgeIndex === edges.length - 2));
    return {
      id: `paired-parameter-heatmap-${comparison.id}-${index}`,
      title: metricMode ? `Median Δ ${data.selection.metric} · ${facetText}` : `Disagreement rate · ${facetText}`,
      subtitle: `Cells with n < ${data.selection.minimum_cell_count} remain visible with reduced opacity and are excluded from observations.`,
      kind: 'heatmap',
      option: { animation: false, tooltip: { formatter: (params: { value?: unknown[] }) => { const value = params.value ?? []; return `${data.selection.x}: ${labels(heatmap.x_boundaries)[Number(value[0])]}<br/>${data.selection.y}: ${labels(heatmap.y_boundaries)[Number(value[1])]}<br/>${metricMode ? `Median Δ: ${Number(value[2]).toPrecision(5)}<br/>Metric coverage: ${value[5]}/${value[3]}` : `Disagreement: ${value[4]}/${value[3]} (${Number(value[2]).toFixed(1)}%)`}<br/>${Number(value[6]) ? 'Sparse cell' : 'Eligible cell'}`; } }, grid: { top: 32, right: 96, bottom: 92, left: 128 }, xAxis: { type: 'category', name: data.selection.x, nameLocation: 'middle', nameGap: 66, data: labels(heatmap.x_boundaries), axisLabel: { rotate: 25 } }, yAxis: { type: 'category', name: data.selection.y, nameLocation: 'middle', nameGap: 102, data: labels(heatmap.y_boundaries) }, visualMap: { min: metricMode ? -extent : 0, max: extent, right: 8, top: 40, calculable: true, inRange: { color: metricMode ? ['#0796a5', '#f8fafc', '#8e3b9d'] : ['#f4f6f8', '#f1ae54', '#c53c4d'] } }, series: [{ type: 'heatmap', data: values, itemStyle: { borderColor: '#fff', borderWidth: 2 }, emphasis: { itemStyle: { borderColor: '#17202a', borderWidth: 2 } } }] },
    };
  });
  const applyBoundaries = () => {
    try {
      const next: Record<string, number[]> = {};
      for (const [name, raw] of Object.entries(boundaryDrafts)) {
        if (!raw.trim()) continue;
        const edges = raw.split(',').map((value) => Number(value.trim()));
        if (edges.length < 3 || edges.some((value) => !Number.isFinite(value)) || edges.some((value, index) => index > 0 && value <= edges[index - 1])) throw new Error(`${name}: enter at least three increasing comma-separated edges.`);
        next[name] = edges;
      }
      setBoundaryError(undefined); setBoundaries(next);
    } catch (error) { setBoundaryError(error instanceof Error ? error.message : 'Invalid boundaries.'); }
  };
  const exportRegionalData = (format: 'csv' | 'json') => {
    const csvCell = (value: unknown) => { const text = String(value ?? ''); return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text; };
    const content = format === 'json'
      ? `${JSON.stringify({ schema: 'pisa.paired-parameter-analysis/v1', request, result: data }, null, 2)}\n`
      : [
        ['left', 'right', 'parameter', 'lower', 'upper', 'upper_inclusive', 'paired_count', 'disagreement_count', 'disagreement_rate', 'left_success_right_fail', 'left_fail_right_success', 'other_disagreement', 'metric', 'metric_eligible_count', 'metric_missing_count', 'delta_mean', 'delta_median', 'sparse'],
        ...data.marginals.flatMap((marginal) => marginal.bins.map((cell) => [data.left, data.right, marginal.parameter, cell.lower, cell.upper, cell.upper_inclusive, cell.total, cell.disagreement_count, cell.disagreement_rate, cell.categories.left_success_right_fail ?? 0, cell.categories.left_fail_right_success ?? 0, cell.categories.other_disagreement ?? 0, data.selection.metric ?? '', cell.metric_eligible_count, cell.metric_missing_count, cell.delta_mean, cell.delta_median, cell.sparse])),
      ].map((row) => row.map(csvCell).join(',')).join('\r\n');
    const blob = new Blob([content], { type: format === 'json' ? 'application/json' : 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob), anchor = document.createElement('a');
    anchor.href = url; anchor.download = `paired-parameter-${comparison.id}.${format}`; anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return <Stack gap="lg" mt="md">
    <Group justify="space-between"><Text fw={700}>Paired parameter analysis</Text><Group gap="xs"><Button size="compact-xs" variant="default" leftSection={<IconDownload size={14} />} onClick={() => exportRegionalData('csv')}>Regional CSV</Button><Button size="compact-xs" variant="default" leftSection={<IconDownload size={14} />} onClick={() => exportRegionalData('json')}>Analysis JSON</Button></Group></Group>
    <Alert color="blue" icon={<IconShieldCheck size={17} />} title="Original parameter space only">Axes and facets come only from recorded sampled parameters. Regional statistics use every uniquely paired sample; derived inputs are not introduced.</Alert>
    <Card withBorder p="md"><SimpleGrid cols={{ base: 1, sm: 2, xl: 4 }}>
      <Select label="X parameter" searchable data={parameterOptions} value={data.selection.x} onChange={setX} />
      <Select label="Y parameter" searchable data={availableY} value={data.selection.y} onChange={setY} />
      <Select label="Facet parameter" data={availableFacets} value={data.selection.facet ?? ''} onChange={(value) => setFacet(value || '')} />
      <Select label="View" data={[{ value: 'outcome', label: 'Outcome disagreement in parameter space' }, { value: 'metric_delta', label: 'Metric delta in parameter space' }, { value: 'metric_agreement', label: 'Paired metric agreement' }]} value={view ?? 'outcome'} onChange={setView} />
      {view === 'metric_delta' && <Select label="Shared metric" searchable data={data.metrics.map((name) => ({ value: name, label: name }))} value={data.selection.metric ?? metric} onChange={setMetric} />}
      <NumberInput label="Equal-width bins" min={2} max={20} value={binCount} onChange={setBinCount} />
      <NumberInput label="Minimum eligible n" min={1} max={data.overview.paired_count} value={minimumCellCount} onChange={setMinimumCellCount} />
    </SimpleGrid><Accordion mt="md" variant="contained"><Accordion.Item value="boundaries"><Accordion.Control>Optional fixed boundaries</Accordion.Control><Accordion.Panel><Text size="xs" c="dimmed" mb="sm">Comma-separated edges override equal-width bins. Samples outside the edges are reported as excluded.</Text><SimpleGrid cols={{ base: 1, md: 3 }}>{activeParameters.map((name) => <TextInput key={name} label={name} placeholder={(data.selection.boundaries[name] ?? []).map((value) => Number(value.toPrecision(5))).join(', ')} value={boundaryDrafts[name] ?? ''} onChange={(event) => setBoundaryDrafts((current) => ({ ...current, [name]: event.currentTarget.value }))} />)}</SimpleGrid><Group mt="sm"><Button size="xs" onClick={applyBoundaries}>Apply boundaries</Button><Button size="xs" variant="default" onClick={() => { setBoundaryDrafts({}); setBoundaries({}); setBoundaryError(undefined); }}>Reset to equal width</Button></Group>{boundaryError && <Text size="xs" c="red" mt="xs">{boundaryError}</Text>}</Accordion.Panel></Accordion.Item></Accordion></Card>
    <SimpleGrid cols={{ base: 2, md: 5 }}>
      <MetricCard label="Paired" value={data.overview.paired_count.toLocaleString()} detail="Unique parameter hashes" icon={<IconDatabase size={18} />} />
      <MetricCard label="Disagreement" value={data.overview.disagreement_count.toLocaleString()} detail={`${((data.overview.disagreement_rate ?? 0) * 100).toFixed(1)}% of pairs`} icon={<IconAlertTriangle size={18} />} color="yellow" />
      <MetricCard label="Direct reversals" value={data.overview.direct_reversal_count.toLocaleString()} detail="Success / Fail only" icon={<IconArrowsSort size={18} />} color="red" />
      <MetricCard label="Invalid-related" value={data.overview.invalid_related_count.toLocaleString()} detail="Reported separately" icon={<IconAlertTriangle size={18} />} color="gray" />
      <MetricCard label={data.selection.metric ? 'Metric coverage' : 'Included'} value={(data.selection.metric ? data.overview.metric_eligible_count : data.coverage.included_count).toLocaleString()} detail={data.selection.metric ? `${data.overview.metric_missing_count} missing pairs` : `${data.coverage.excluded_by_boundaries + data.coverage.excluded_by_facet} filtered`} icon={<IconCheck size={18} />} color="teal" />
    </SimpleGrid>
    {data.observations.length > 0 && <Card withBorder p="md"><Text fw={650} mb="xs">Observed regional differences</Text><Stack gap="xs">{data.observations.map((item, index) => <Text key={`${item.kind}-${index}`} size="sm">• {item.text}</Text>)}</Stack><Text size="xs" c="dimmed" mt="sm">Only cells meeting n ≥ {data.selection.minimum_cell_count} are summarized. These are descriptive observations, not causal explanations.</Text></Card>}
    <VisualizationCard spec={outcomeSpec} onPointClick={(value) => { const record = chartRecord(value); if (typeof record?.left_run_id === 'string') onOpen(record.left_run_id, [data.left, data.right]); }} />
    <SimpleGrid cols={{ base: 1, xl: 2 }}>{heatmapSpecs.map((spec) => <VisualizationCard key={spec.id} spec={spec} />)}</SimpleGrid>
    <Card p={0}><Group p="md" justify="space-between"><div><Text fw={650}>Marginal parameter intervals</Text><Text size="xs" c="dimmed">Exact numerator and denominator are retained beside each percentage.</Text></div><Badge variant="light">{data.marginals.length} parameters</Badge></Group><ScrollArea><table className="pisa-data-table"><thead><tr><th>Parameter</th><th>Interval</th><th>Disagreement</th><th>Directions</th><th>Metric delta</th><th>Eligibility</th></tr></thead><tbody>{data.marginals.flatMap((marginal) => marginal.bins.map((cell) => <tr key={`${marginal.parameter}-${cell.index}`} style={{ opacity: cell.sparse ? 0.48 : 1 }}><td>{marginal.parameter}</td><td>{intervalLabel(cell.lower, cell.upper, cell.upper_inclusive)}</td><td>{cell.disagreement_count}/{cell.total} · {cell.disagreement_rate == null ? '—' : `${(100 * cell.disagreement_rate).toFixed(1)}%`}</td><td><Text size="xs">L✓/R✕ {cell.categories.left_success_right_fail ?? 0} · L✕/R✓ {cell.categories.left_fail_right_success ?? 0} · other {cell.categories.other_disagreement ?? 0}</Text></td><td>{cell.delta_median == null ? '—' : `median ${cell.delta_median.toPrecision(5)} · ${cell.metric_eligible_count}/${cell.total}`}</td><td>{cell.sparse ? `Sparse (n < ${data.selection.minimum_cell_count})` : 'Eligible'}</td></tr>))}</tbody></table></ScrollArea></Card>
    {data.candidates.length > 0 && <Card p="md"><Text fw={650}>Candidate paired executions</Text><Text size="xs" c="dimmed" mb="md">Candidates support screening and drill-down; the report does not claim repeatability without reruns.</Text><SimpleGrid cols={{ base: 1, md: 3 }}>{data.candidates.map((candidate) => <Card key={candidate.kind} withBorder p="md"><Badge variant="light" mb="xs">{candidate.kind.replaceAll('_', ' ')}</Badge><Text size="sm" fw={600}>{candidate.left_outcome} → {candidate.right_outcome}</Text><Text size="xs" c="dimmed">{candidate.reason}</Text><Text size="xs" className="pisa-code" mt="xs">{Object.entries(candidate.parameters).map(([name, value]) => `${name}=${value?.toPrecision(5) ?? '—'}`).join(' · ')}</Text><Button size="compact-xs" variant="light" mt="sm" onClick={() => onOpen(candidate.left_run_id, [data.left, data.right])}>Open paired detail</Button></Card>)}</SimpleGrid></Card>}
  </Stack>;
}

function Compare({ datasetId, onOpen }: { datasetId: string; onOpen: (id: string, experiments?: string[]) => void }) {
  const comparisons = useQuery({ queryKey: ['comparisons-v2', datasetId], queryFn: () => api.datasets.comparisons(datasetId), retry: 1, refetchOnMount: 'always' });
  const storageKey = `pisa:compare:${datasetId}`;
  const [leftChoice, setLeftChoice] = useSessionState<string | null>(`${storageKey}:left`, null);
  const [rightChoice, setRightChoice] = useSessionState<string | null>(`${storageKey}:right`, null);
  const items = comparisons.data?.items ?? [];
  const leftOptions = [...new Set(items.map((item) => item.left))];
  const resolvedLeft = leftChoice && leftOptions.includes(leftChoice) ? leftChoice : leftOptions[0] ?? null;
  const rightOptions = items.filter((item) => item.left === resolvedLeft).map((item) => item.right);
  const resolvedRight = rightChoice && rightOptions.includes(rightChoice) ? rightChoice : rightOptions[0] ?? null;
  const selected = items.find((item) => item.left === resolvedLeft && item.right === resolvedRight);
  useEffect(() => {
    if (resolvedLeft !== leftChoice) setLeftChoice(resolvedLeft);
    if (resolvedRight !== rightChoice) setRightChoice(resolvedRight);
  }, [leftChoice, resolvedLeft, resolvedRight, rightChoice, setLeftChoice, setRightChoice]);
  if (comparisons.isLoading) return <PageLoading label="Classifying comparisons…" />;
  if (comparisons.error) return <InlineError error={comparisons.error} onRetry={() => comparisons.refetch()} />;
  return <Stack gap="xl">
    <CrossExperimentOverview datasetId={datasetId} summary={comparisons.data?.cross_experiment} onOpen={onOpen} />
    <div><Text fw={700} size="lg">Pairwise comparisons</Text><Text size="sm" c="dimmed">Inspect the original two-experiment classifications, deltas, and visualizations.</Text></div>
    {!items.length ? <Card><EmptyState title="No defensible pairwise comparison found" description="A pairwise comparison requires compatible parameter domains and recorded semantics." /></Card> : <>
      <Card p="lg"><SimpleGrid cols={{ base: 1, md: 2 }}><Select label="Left experiment" searchable data={leftOptions.map((value) => ({ value, label: value }))} value={resolvedLeft} onChange={(value) => { setLeftChoice(value); setRightChoice(items.find((item) => item.left === value)?.right ?? null); }} allowDeselect={false} /><Select label="Right experiment" searchable data={rightOptions.map((value) => ({ value, label: value }))} value={resolvedRight} onChange={setRightChoice} allowDeselect={false} /></SimpleGrid><Text size="xs" c="dimmed" mt="xs">Only report-classified relations for the selected Left experiment are offered; unpaired combinations are not synthesized.</Text></Card>
      <Card p="lg">{selected ? <Stack><Group justify="space-between"><div><Text fw={650}>{selected.left} → {selected.right}</Text><Text size="sm" c="dimmed">{selected.note ?? 'Comparison semantics were classified from recorded provenance.'}</Text></div><StatusBadge value={selected.role} /></Group><Group gap="xs"><Badge variant="light" color="gray">{selected.matched.toLocaleString()} paired</Badge>{selected.information_comparable_count !== undefined && <Badge variant="light" color="teal">{selected.information_consistent_count?.toLocaleString() ?? 0} / {selected.information_comparable_count.toLocaleString()} fully consistent</Badge>}{selected.left_only > 0 && <Badge variant="light" color="yellow">{selected.left_only} left only</Badge>}{selected.right_only > 0 && <Badge variant="light" color="yellow">{selected.right_only} right only</Badge>}</Group><Alert color="blue" icon={<IconShieldCheck size={17} />} title="Interpretation guardrail">Complete pairs are used for paired metrics. Missing left/right values and semantic differences are reported separately.</Alert>{selected.information_comparable_count !== undefined && <Card withBorder p="md"><Text size="xs" c="dimmed" tt="uppercase" fw={650}>Fully identical concrete information</Text><Group align="baseline" gap="xs"><Text fz={30} fw={750}>{selected.information_consistent_count?.toLocaleString() ?? 0}</Text><Text c="dimmed">/ {selected.information_comparable_count.toLocaleString()} paired concrete samples · {selected.information_agreement_ratio == null ? '—' : `${(selected.information_agreement_ratio * 100).toFixed(2)}%`}</Text></Group><Text size="xs">Compared: {selected.information_scope}</Text><Text size="xs" c="dimmed">Excluded: {selected.information_exclusions}</Text></Card>}{selected.agreement !== undefined && <div><Text size="xs" c="dimmed">Outcome agreement</Text><Text fz={32} fw={700}>{(selected.agreement <= 1 ? selected.agreement * 100 : selected.agreement).toFixed(1)}%</Text></div>}<ChartSection datasetId={datasetId} section={`compare:${selected.id}`} />{['paired_replicate', 'paired_system_intervention', 'paired_policy_intervention'].includes(selected.role) && <PairedParameterAnalysis datasetId={datasetId} comparison={selected} onOpen={onOpen} />}</Stack> : <EmptyState title="Select a comparison" description="Classification determines whether the workspace uses paired deltas, agreement, common-domain coverage, or description only." />}</Card>
    </>}
  </Stack>;
}

function consistencyPercent(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? '—' : `${(value * 100).toFixed(2)}%`;
}

function consistencyMetric(group: ConsistencyGroup, key: string) {
  return group.discrete.find((item) => item.key === key);
}

function Consistency({ datasetId }: { datasetId: string }) {
  const queryClient = useQueryClient();
  const [selectedGroupId, setSelectedGroupId] = useState<string | null>(null);
  const [profile, setProfile] = useState<'trajectory_outlier_controls' | 'full_controls'>('trajectory_outlier_controls');
  const [outlierLimit, setOutlierLimit] = useState<number | string>(25);
  const resolvedOutlierLimit = typeof outlierLimit === 'number' ? outlierLimit : 25;
  const consistency = useQuery({
    queryKey: ['report-consistency', datasetId, profile, resolvedOutlierLimit],
    queryFn: () => api.datasets.consistency(datasetId, { profile, position_tolerances_m: [0.001, 0.01, 0.1], outlier_limit: resolvedOutlierLimit }),
    retry: 1,
    refetchOnMount: 'always',
  });
  const analyze = useMutation({
    mutationFn: (request: ConsistencyAnalyzeRequest) => api.datasets.analyzeConsistency(datasetId, request),
  });
  const launchedForCurrentConfig = analyze.variables?.profile === profile
    && analyze.variables?.outlier_limit === resolvedOutlierLimit
    && JSON.stringify(analyze.variables?.position_tolerances_m) === JSON.stringify([0.001, 0.01, 0.1]);
  const activeJobId = (launchedForCurrentConfig ? analyze.data?.id : undefined) ?? consistency.data?.deep.job?.id;
  const job = useQuery({
    queryKey: ['job', activeJobId],
    queryFn: () => api.jobs.get(activeJobId!),
    enabled: Boolean(activeJobId),
    refetchInterval: (query) => ['queued', 'running'].includes(query.state.data?.state ?? '') ? 750 : false,
  });
  const cancel = useMutation({ mutationFn: () => api.jobs.cancel(activeJobId!) });
  const jobState = job.data?.state ?? consistency.data?.deep.job?.state;
  useEffect(() => {
    if (!jobState || ['queued', 'running'].includes(jobState)) return;
    void queryClient.invalidateQueries({ queryKey: ['report-consistency', datasetId] });
  }, [datasetId, jobState, queryClient]);
  const groups = consistency.data?.quick.groups ?? [];
  useEffect(() => {
    if (!groups.length) return setSelectedGroupId(null);
    setSelectedGroupId((current) => current && groups.some((group) => group.id === current) ? current : groups[0].id);
  }, [groups]);

  if (consistency.isLoading) return <PageLoading label="Reading consistency summary…" />;
  if (consistency.error) return <InlineError error={consistency.error} onRetry={() => consistency.refetch()} />;
  const result = consistency.data!;
  if (!result.quick.available || !groups.length) return <Stack gap="lg">
    <Alert color="blue" icon={<IconShieldCheck size={17} />} title="Repeatability is not correctness">This view measures whether compatible replicate settings produce the same recorded evidence. Identical failures are repeatable, but they are not necessarily correct or safe.</Alert>
    <Card><EmptyState title={result.quick.reason === 'normalized_report_index_required' ? 'Normalized index required' : 'No compatible replicate group'} description={result.quick.reason === 'normalized_report_index_required' ? 'This older report remains fully usable in its original views. Rebuild it with the current report version to add indexed Consistency analysis.' : 'This report remains fully usable for overview, sampling, run inspection, replay, and pairwise comparison. Consistency requires at least two canonical datasets classified as paired replicates with unique parameter hashes.'} /></Card>
  </Stack>;

  const group = groups.find((item) => item.id === selectedGroupId) ?? groups[0];
  const deepGroup = result.deep.summary?.groups.find((item) => item.id === group.id);
  const outcomeScore = consistencyMetric(group, 'outcome');
  const stopScore = consistencyMetric(group, 'stop_condition');
  const collisionScore = consistencyMetric(group, 'collision');
  const stepsScore = group.continuous.find((item) => item.key.toLowerCase().includes('total_steps'));
  const scoreItems = [
    { key: 'outcome', label: 'Outcome', ratio: outcomeScore?.agreement_ratio, detail: outcomeScore ? `${outcomeScore.consistent_count.toLocaleString()} / ${outcomeScore.comparable_count.toLocaleString()}` : 'Not indexed' },
    { key: 'stop_condition', label: 'Stop condition', ratio: stopScore?.agreement_ratio, detail: stopScore ? `${stopScore.consistent_count.toLocaleString()} / ${stopScore.comparable_count.toLocaleString()}` : 'Not indexed' },
    { key: 'collision', label: 'Collision', ratio: collisionScore?.agreement_ratio, detail: collisionScore ? `${collisionScore.consistent_count.toLocaleString()} / ${collisionScore.comparable_count.toLocaleString()}` : 'Not indexed' },
    { key: 'total_steps', label: 'Total steps', ratio: stepsScore?.exact_ratio, detail: stepsScore ? `${stepsScore.exact_count.toLocaleString()} / ${stepsScore.eligible_sample_count.toLocaleString()}` : 'Not indexed' },
  ];
  const shownJob = job.data ?? result.deep.job;
  const running = ['queued', 'running'].includes(shownJob?.state ?? '');
  const progressPercent = shownJob?.progress?.total ? Math.min(100, shownJob.progress.current / shownJob.progress.total * 100) : shownJob?.state === 'running' ? 3 : 0;
  const artifacts = result.deep.artifacts ?? [];

  return <Stack gap="lg">
    <Alert color="blue" icon={<IconShieldCheck size={17} />} title="How to read this view">Repeatability is reported only over parameter-hash-matched samples in compatible replicate groups. Agreement does not prove correctness; runtime bookkeeping is separated from behavioral evidence and does not lower the indexed-information score.</Alert>
    <Card p="lg">
      <Group justify="space-between" align="flex-end" mb="md" wrap="wrap">
        <div><Text fw={700} size="lg">Quick indexed consistency</Text><Text size="sm" c="dimmed">Generated during report build without opening trajectory files.</Text></div>
        <Select label="Replicate group" value={group.id} onChange={setSelectedGroupId} allowDeselect={false} data={groups.map((item, index) => ({ value: item.id, label: `Group ${index + 1} · ${item.datasets.join(' / ')}` }))} maw={560} />
      </Group>
      <Group gap="xs" mb="md">{group.datasets.map((dataset) => <Badge key={dataset} variant="outline">{dataset}</Badge>)}</Group>
      <SimpleGrid cols={{ base: 2, md: 5 }} mb="lg">
        {scoreItems.map((item) => <Card key={item.key} withBorder p="md"><Text size="xs" c="dimmed" tt="uppercase" fw={650}>{item.label}</Text><Text fz={25} fw={750}>{consistencyPercent(item.ratio)}</Text><Text size="xs" c="dimmed">{item.detail}</Text></Card>)}
        <Card withBorder p="md"><Text size="xs" c="dimmed" tt="uppercase" fw={650}>All indexed information</Text><Text fz={25} fw={750}>{consistencyPercent(group.information_agreement_ratio)}</Text><Text size="xs" c="dimmed">{group.information_consistent_count.toLocaleString()} / {group.information_comparable_count.toLocaleString()}</Text></Card>
      </SimpleGrid>
      <Group justify="space-between" mb="xs"><Text fw={650}>Outcome patterns</Text><Text size="xs" c="dimmed">{group.common_sample_count.toLocaleString()} common · {group.excluded_noncommon_sample_count.toLocaleString()} non-common excluded</Text></Group>
      <ScrollArea mb="lg"><table className="pisa-data-table"><thead><tr><th>Pattern in dataset order</th><th>Samples</th><th>All replicates agree</th></tr></thead><tbody>{group.outcome_patterns.map((item) => <tr key={item.pattern}><td className="pisa-code">{item.pattern}</td><td>{item.count.toLocaleString()}</td><td><Badge color={item.all_replicates_agree ? 'teal' : 'red'} variant="light">{item.all_replicates_agree ? 'Yes' : 'No'}</Badge></td></tr>)}</tbody></table></ScrollArea>
      <Text fw={650} mb="xs">Behavioral scalar variation</Text>
      <Text size="xs" c="dimmed" mb="sm">Variation is max − min across every replicate. Exact means zero recorded difference; partial and unavailable samples never enter the denominator.</Text>
      <ScrollArea><table className="pisa-data-table"><thead><tr><th>Metric</th><th>Exact</th><th>Eligible</th><th>Median variation</th><th>P95 variation</th><th>Max variation</th><th>Coverage exceptions</th></tr></thead><tbody>{group.continuous.map((item) => <tr key={item.key}><td><Text size="sm" fw={600}>{item.label}</Text><Text size="xs" c="dimmed" className="pisa-code">{item.key}</Text></td><td>{consistencyPercent(item.exact_ratio)}</td><td>{item.eligible_sample_count.toLocaleString()}</td><td>{comparisonValue(item.variation_median, item.unit)}</td><td>{comparisonValue(item.variation_p95, item.unit)}</td><td>{comparisonValue(item.variation_max, item.unit)}</td><td>{item.partial_sample_count.toLocaleString()} partial · {item.unavailable_sample_count.toLocaleString()} unavailable</td></tr>)}</tbody></table></ScrollArea>
      <Accordion mt="lg" variant="contained"><Accordion.Item value="runtime"><Accordion.Control>Runtime and bookkeeping variation ({group.runtime.length})</Accordion.Control><Accordion.Panel><Text size="xs" c="dimmed" mb="sm">These values help diagnose execution infrastructure but are excluded from behavioral identity.</Text><ScrollArea><table className="pisa-data-table"><thead><tr><th>Metric</th><th>Exact</th><th>Median variation</th><th>P95 variation</th><th>Max variation</th></tr></thead><tbody>{group.runtime.map((item) => <tr key={item.key}><td>{item.label}</td><td>{consistencyPercent(item.exact_ratio)}</td><td>{comparisonValue(item.variation_median, item.unit)}</td><td>{comparisonValue(item.variation_p95, item.unit)}</td><td>{comparisonValue(item.variation_max, item.unit)}</td></tr>)}</tbody></table></ScrollArea>{!group.runtime.length && <Text size="sm" c="dimmed">No runtime metrics were indexed.</Text>}</Accordion.Panel></Accordion.Item></Accordion>
    </Card>

    <Card p="lg">
      <Group justify="space-between" align="flex-start" mb="md" wrap="wrap"><div><Text fw={700} size="lg">Deep trajectory and control consistency</Text><Text size="sm" c="dimmed">Generated on demand and cached inside this report. Every actor trajectory is scanned; the default profile limits control-file diagnosis to outcome mismatches and representative outliers.</Text></div><Badge color={result.deep.state === 'ready' ? 'teal' : running ? 'blue' : 'gray'}>{running ? shownJob?.state : result.deep.state.replaceAll('_', ' ')}</Badge></Group>
      <Group align="flex-end" wrap="wrap" mb="md"><Select label="Analysis profile" value={profile} onChange={(value) => setProfile(value === 'full_controls' ? 'full_controls' : 'trajectory_outlier_controls')} allowDeselect={false} disabled={running} data={[{ value: 'trajectory_outlier_controls', label: 'All trajectories + outlier controls' }, { value: 'full_controls', label: 'All trajectories + all controls (slower)' }]} w={330} /><NumberInput label="Outliers per ranking" min={1} max={1000} value={outlierLimit} onChange={setOutlierLimit} disabled={running} w={180} /><Button leftSection={<IconPlayerPlay size={16} />} loading={analyze.isPending} disabled={running} onClick={() => analyze.mutate({ profile, position_tolerances_m: [0.001, 0.01, 0.1], outlier_limit: resolvedOutlierLimit, force: result.deep.state === 'ready' })}>{result.deep.state === 'ready' ? 'Recompute' : 'Analyze now'}</Button>{running && <Button color="red" variant="light" loading={cancel.isPending} onClick={() => cancel.mutate()}>Cancel</Button>}</Group>
      {analyze.error && <InlineError error={analyze.error} />}
      {shownJob && <Alert color={shownJob.state === 'failed' ? 'red' : shownJob.state === 'cancelled' ? 'yellow' : shownJob.state === 'succeeded' ? 'teal' : 'blue'} title={`Analysis ${shownJob.state}`} mb="md"><Stack gap={6}><Progress value={progressPercent} animated={running && !shownJob.progress?.total} /><Group justify="space-between" wrap="wrap"><Text size="sm">{shownJob.message ?? shownJob.phase ?? 'Waiting for analysis worker'}</Text><Text size="xs" fw={650}>{shownJob.progress ? `${shownJob.progress.current.toLocaleString()} / ${shownJob.progress.total?.toLocaleString() ?? '?'} ${shownJob.progress.unit ?? 'items'}` : '0 / ? items'}</Text></Group></Stack></Alert>}
      {deepGroup ? <><SimpleGrid cols={{ base: 2, md: 4 }} mb="lg"><Card withBorder p="md"><Text size="xs" c="dimmed" tt="uppercase">Strict recorded identity</Text><Text fz={25} fw={750}>{consistencyPercent(deepGroup.strict_exact_count / Math.max(1, deepGroup.trajectory_comparable_count))}</Text><Text size="xs">{deepGroup.strict_exact_count.toLocaleString()} / {deepGroup.trajectory_comparable_count.toLocaleString()} comparable</Text></Card>{result.deep.summary!.position_tolerances_m.map((tolerance) => <Card key={tolerance} withBorder p="md"><Text size="xs" c="dimmed" tt="uppercase">Within {comparisonValue(tolerance, 'm')}</Text><Text fz={25} fw={750}>{consistencyPercent((deepGroup.position_tolerance_counts[String(tolerance)] ?? 0) / Math.max(1, deepGroup.trajectory_comparable_count))}</Text><Text size="xs">{(deepGroup.position_tolerance_counts[String(tolerance)] ?? 0).toLocaleString()} / {deepGroup.trajectory_comparable_count.toLocaleString()} comparable</Text></Card>)}</SimpleGrid>{deepGroup.trajectory_comparable_count < deepGroup.sample_count && <Alert color="yellow" mb="md">{(deepGroup.sample_count - deepGroup.trajectory_comparable_count).toLocaleString()} samples had non-matching semantic actor sets and are excluded from trajectory percentages.</Alert>}<Text fw={650} mb="xs">Distribution of each sample's worst replicate-pair position error</Text><SimpleGrid cols={{ base: 2, md: 4 }}><Card withBorder p="md"><Text size="xs" c="dimmed">Median</Text><Text fw={700}>{comparisonValue(deepGroup.max_position_error_m.median, 'm')}</Text></Card><Card withBorder p="md"><Text size="xs" c="dimmed">P95</Text><Text fw={700}>{comparisonValue(deepGroup.max_position_error_m.p95, 'm')}</Text></Card><Card withBorder p="md"><Text size="xs" c="dimmed">P99</Text><Text fw={700}>{comparisonValue(deepGroup.max_position_error_m.p99, 'm')}</Text></Card><Card withBorder p="md"><Text size="xs" c="dimmed">Maximum</Text><Text fw={700}>{comparisonValue(deepGroup.max_position_error_m.max, 'm')}</Text></Card></SimpleGrid><Text size="xs" c="dimmed" mt="md">Alignment: {result.deep.summary!.alignment_rule}. Strict rule: {result.deep.summary!.strict_rule}. Controls: {result.deep.summary!.control_rule}.</Text>{artifacts.length > 0 && <Group gap="xs" mt="md">{artifacts.map((artifact) => { const path = typeof artifact === 'string' ? artifact : artifact.path; const url = typeof artifact === 'string' ? undefined : artifact.download_url; return url ? <Button key={path} component="a" href={url} target="_blank" size="compact-xs" variant="light" leftSection={<IconDownload size={14} />}>{path.split('/').at(-1)}</Button> : <Code key={path}>{path}</Code>; })}</Group>}</> : <Alert color="gray">No deep result has been generated for the default thresholds and profile. Starting analysis does not rebuild or modify the original report evidence.</Alert>}
    </Card>
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
  const [metricSeriesVisibility, setMetricSeriesVisibility] = useSessionState<Record<string, boolean>>(`pisa:replay-metric-series-visibility:${datasetId}`, {});
  const [controlSeriesVisibility, setControlSeriesVisibility] = useSessionState<Record<string, boolean>>(`pisa:replay-control-series-visibility:${datasetId}`, {});
  const [metricSeriesColors, setMetricSeriesColors] = useSessionState<Record<string, string>>(`pisa:replay-metric-series-colors:${datasetId}`, {});
  const [controlSeriesColors, setControlSeriesColors] = useSessionState<Record<string, string>>(`pisa:replay-control-series-colors:${datasetId}`, {});
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
  const selectedMetricChart = replayAnalysisMode === 'compare' ? comparisonCharts.metrics : metricCharts.metrics;
  const selectedControlChart = replayAnalysisMode === 'compare' ? comparisonCharts.controls : metricCharts.controls;
  const visibleMetricChart = useMemo(() => replayChartWithVisibleAxes(selectedMetricChart, metricSeriesVisibility, metricSeriesColors), [metricSeriesColors, metricSeriesVisibility, selectedMetricChart]);
  const visibleControlChart = useMemo(() => replayChartWithVisibleAxes(selectedControlChart, controlSeriesVisibility, controlSeriesColors), [controlSeriesColors, controlSeriesVisibility, selectedControlChart]);
  const setStoredSeriesColor = useCallback((setter: Dispatch<SetStateAction<Record<string, string>>>, name: string, color?: string) => {
    setter((current) => {
      const next = { ...current };
      if (color) next[name] = color;
      else delete next[name];
      return next;
    });
  }, []);
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
            <Stack gap="sm"><Card p="md"><Group justify="space-between" align="flex-start" wrap="wrap"><div><Text fw={650} size="sm">{replayAnalysisMode === 'compare' ? 'Delta metrics' : 'Metrics'}</Text><Text size="xs" c="dimmed">{replayAnalysisMode === 'compare' ? 'Directional ego differences at timestamps recorded by both experiments.' : 'Recorded state and safety values. Speed means measured actor speed, never a control target.'}</Text></div><Group>{replayAnalysisMode !== 'compare' && <Checkbox label="Include selected non-ego actors" checked={includeAgentMetrics} onChange={(event) => setIncludeAgentMetrics(event.currentTarget.checked)} />}<Button size="compact-xs" variant="default" onClick={() => setActiveStateMetrics([])}>Hide all</Button></Group></Group><Group gap="md" mt="md">{stateMetricKeys.map((metric) => <Checkbox key={metric} label={metricDefinitions[metric].label} checked={activeStateMetrics.includes(metric)} onChange={(event) => setActiveStateMetrics((current) => event.currentTarget.checked ? [...current, metric] : current.filter((value) => value !== metric))} />)}</Group></Card><VisualizationCard spec={visibleMetricChart} seriesVisibility={metricSeriesVisibility} onSeriesVisibilityChange={(name, visible) => setMetricSeriesVisibility((current) => ({ ...current, [name]: visible }))} seriesColorOverrides={metricSeriesColors} onSeriesColorChange={(name, color) => setStoredSeriesColor(setMetricSeriesColors, name, color)} animationDurationSeconds={Math.max(0.25, (timeDomain.maximum - timeDomain.minimum) / Math.max(0.01, Number(playbackRate ?? 1)))} emptyDescription={replayAnalysisMode === 'compare' ? 'The two experiments have no common recorded timestamps for the enabled metrics.' : 'Enable a metric or select an ego actor with recorded state data. Non-ego actors are hidden by default.'} /></Stack>
            <Stack gap="sm"><Card p="md"><Group justify="space-between" align="flex-start" wrap="wrap"><div><Text fw={650} size="sm">{replayAnalysisMode === 'compare' ? 'Delta controls' : 'Controls'}</Text><Text size="xs" c="dimmed">{replayAnalysisMode === 'compare' ? 'Directional command differences; T/S/B and Ackermann target semantics remain separate.' : "Options are derived from each experiment's recorded control type; T/S/B and Ackermann target semantics are kept separate."}</Text></div><Button size="compact-xs" variant="default" onClick={() => setActiveControlMetrics([])}>Hide all</Button></Group><Group gap="md" mt="md">{availableControlKeys.map((control) => <Checkbox key={control} label={controlDefinitions[control].label} checked={activeControlMetrics.includes(control)} onChange={(event) => setActiveControlMetrics((current) => event.currentTarget.checked ? [...current, control] : current.filter((value) => value !== control))} />)}{!availableControlKeys.length && <Text size="xs" c="dimmed">No recognized control command fields were recorded.</Text>}</Group></Card><VisualizationCard spec={visibleControlChart} seriesVisibility={controlSeriesVisibility} onSeriesVisibilityChange={(name, visible) => setControlSeriesVisibility((current) => ({ ...current, [name]: visible }))} seriesColorOverrides={controlSeriesColors} onSeriesColorChange={(name, color) => setStoredSeriesColor(setControlSeriesColors, name, color)} animationDurationSeconds={Math.max(0.25, (timeDomain.maximum - timeDomain.minimum) / Math.max(0.01, Number(playbackRate ?? 1)))} emptyDescription={replayAnalysisMode === 'compare' ? 'The two experiments have no common recorded timestamps for the enabled control commands.' : 'Enable a recorded control command. Throttle is preferred for T/S/B; Speed target is preferred for Ackermann.'} /></Stack>
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
  const temporary = reportPreview.data?.storage_kind === 'temporary';
  useQuery({ queryKey: ['report-preview-lease', datasetId], queryFn: () => api.datasets.lease(datasetId!), enabled: Boolean(datasetId && temporary), refetchInterval: 30_000, retry: false });
  const discardPreview = useMutation({ mutationFn: () => api.datasets.discardPreview(datasetId!), onSuccess: () => navigate('/#reports', { replace: true }) });
  const replayStorageKey = datasetId ? `pisa:last-replay:${datasetId}` : '';
  useEffect(() => {
    if (!datasetId || section !== 'replay' || runId) return;
    const remembered = window.sessionStorage.getItem(replayStorageKey);
    if (remembered) navigate(`/reports/${encodeURIComponent(datasetId)}/replay/${encodeURIComponent(remembered)}`, { replace: true });
  }, [datasetId, navigate, replayStorageKey, runId, section]);
  if (!datasetId) return null;
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
      <PageHeader eyebrow="Report workspace" title={selected?.name ?? 'Evidence report'} description={selected ? `${selected.run_count.toLocaleString()} runs across ${selected.experiment_count} experiments · filters and exports retain provenance.` : 'Loading indexed report metadata…'} actions={temporary ? <Button variant="default" color="red" loading={discardPreview.isPending} leftSection={<IconArrowLeft size={16} />} onClick={() => discardPreview.mutate()}>Discard preview</Button> : <Button component={Link} to="/#reports" variant="default" leftSection={<IconArrowLeft size={16} />}>Manage reports</Button>} />
      <Tabs value={section} onChange={setSection} variant="outline" mb="lg">
        <ScrollArea type="never"><Tabs.List style={{ flexWrap: 'nowrap' }}>{sections.map(([value, label]) => <Tabs.Tab key={value} value={value}>{label}</Tabs.Tab>)}</Tabs.List></ScrollArea>
      </Tabs>
      {section === 'overview' && <Overview datasetId={datasetId} report={selected} />}
      {section === 'sampling' && <Stack gap="lg"><ScatterExplorer datasetId={datasetId} onOpen={openRun} /><ChartSection datasetId={datasetId} section={section} /></Stack>}
      {['outcomes', 'performance', 'sensitivity'].includes(section) && <ChartSection datasetId={datasetId} section={section} />}
      {section === 'compare' && <Compare datasetId={datasetId} onOpen={openRun} />}
      {section === 'consistency' && <Consistency datasetId={datasetId} />}
      {section === 'runs' && <Runs datasetId={datasetId} onOpen={openRun} />}
      {section === 'replay' && <Replay datasetId={datasetId} runId={runId} onChoose={() => setSection('runs')} onOpen={openRun} />}
      {section === 'media' && <Media datasetId={datasetId} onChoose={() => setSection('runs')} />}
      {section === 'provenance' && <Provenance datasetId={datasetId} />}
      {section === 'exports' && <Exports datasetId={datasetId} />}
    </>
  );
}
