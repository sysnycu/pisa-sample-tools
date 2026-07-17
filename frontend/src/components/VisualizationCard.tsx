import { useCallback, useEffect, useRef, useState } from 'react';
import * as echarts from 'echarts/core';
import { BarChart, LineChart, PieChart, ScatterChart } from 'echarts/charts';
import { CanvasRenderer, SVGRenderer } from 'echarts/renderers';
import { DataZoomComponent, DatasetComponent, GridComponent, LegendComponent, MarkLineComponent, TitleComponent, TooltipComponent, TransformComponent, VisualMapComponent } from 'echarts/components';
import type { EChartsOption, EChartsType } from 'echarts';
import { ActionIcon, Badge, Card, Divider, Group, Menu, Stack, Text, Tooltip } from '@mantine/core';
import { IconChevronDown, IconDownload, IconFileCode, IconFileSpreadsheet, IconMovie, IconPhoto, IconPresentation, IconRefresh } from '@tabler/icons-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import type { ExportRequest, Job, VisualizationSpec } from '../api/types';
import { EmptyState } from './Feedback';

echarts.use([
  BarChart, LineChart, PieChart, ScatterChart,
  CanvasRenderer, SVGRenderer,
  DataZoomComponent, DatasetComponent, GridComponent, LegendComponent, MarkLineComponent, TitleComponent, TooltipComponent, TransformComponent, VisualMapComponent,
]);

type JsonMap = Record<string, unknown>;
type LocalExportFormat = 'png' | 'svg' | 'csv' | 'json';

const EXPORT_POLL_INTERVAL_MS = 1_250;
const EXPORT_POLL_TIMEOUT_MS = 15 * 60 * 1_000;

function isMap(value: unknown): value is JsonMap {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function asList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : value === undefined ? [] : [value];
}

function scalar(value: unknown): string | number | boolean | null {
  if (value === null || typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return value;
  return JSON.stringify(value);
}

function csvCell(value: unknown): string {
  const plain = String(scalar(value) ?? '');
  // A leading apostrophe prevents spreadsheet formula execution without changing numeric cells.
  const safe = /^[=+@]/.test(plain) || /^-\D/.test(plain) ? `'${plain}` : plain;
  return /[",\r\n]/.test(safe) ? `"${safe.replaceAll('"', '""')}"` : safe;
}

function tableToCsv(rows: Array<Array<unknown>>): string {
  return `${rows.map((row) => row.map(csvCell).join(',')).join('\r\n')}\r\n`;
}

function datasetRows(option: JsonMap): Array<Array<unknown>> | undefined {
  for (const datasetValue of asList(option.dataset)) {
    if (!isMap(datasetValue) || !Array.isArray(datasetValue.source) || !datasetValue.source.length) continue;
    const source = datasetValue.source;
    if (source.every(isMap)) {
      const headers = [...new Set(source.flatMap((row) => Object.keys(row)))];
      return [headers, ...source.map((row) => headers.map((header) => row[header]))];
    }
    if (source.every(Array.isArray)) {
      const rows = source as unknown[][];
      const sourceHeader = datasetValue.sourceHeader;
      const firstIsHeader = sourceHeader !== false && rows[0]?.every((value) => typeof value === 'string');
      const width = Math.max(...rows.map((row) => row.length));
      return firstIsHeader ? rows : [Array.from({ length: width }, (_, index) => `dimension_${index + 1}`), ...rows];
    }
  }
  return undefined;
}

/** Build a clean, long-form CSV from the values actually supplied to ECharts. */
export function visualizationToCsv(option: Record<string, unknown>): string {
  const directDataset = datasetRows(option);
  if (directDataset) return tableToCsv(directDataset);

  const xAxis = asList(option.xAxis).find(isMap);
  const categories = xAxis && Array.isArray(xAxis.data) ? xAxis.data : [];
  const rows: Array<Array<unknown>> = [['series', 'index', 'x', 'y', 'name', 'value']];

  for (const [seriesIndex, value] of asList(option.series).entries()) {
    if (!isMap(value)) continue;
    const seriesName = scalar(value.name) ?? `series_${seriesIndex + 1}`;
    for (const [index, item] of asList(value.data).entries()) {
      const raw = isMap(item) && 'value' in item ? item.value : item;
      const name = isMap(item) ? item.name : undefined;
      if (Array.isArray(raw)) {
        rows.push([seriesName, index, raw[0], raw[1], name, raw.length > 2 ? raw : raw[1]]);
      } else {
        rows.push([seriesName, index, categories[index] ?? index, raw, name, raw]);
      }
    }
  }

  return tableToCsv(rows);
}

export function safeVisualizationFilename(title: string, id: string, extension: string): string {
  const slug = `${title}-${id}`
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 96) || 'pisa-visualization';
  return `${slug}.${extension}`;
}

function waitForPoll(signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException('Export polling was cancelled.', 'AbortError'));
    };
    const timer = window.setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, EXPORT_POLL_INTERVAL_MS);
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

async function pollExportJob(initial: Job, signal: AbortSignal, onUpdate: (job: Job) => void): Promise<Job> {
  let job = initial;
  const deadline = Date.now() + EXPORT_POLL_TIMEOUT_MS;
  onUpdate(job);
  while (job.state === 'queued' || job.state === 'running') {
    if (Date.now() >= deadline) throw new Error('Export did not finish within 15 minutes. It remains available in Jobs & exports.');
    await waitForPoll(signal);
    job = await api.jobs.get(job.id);
    onUpdate(job);
  }
  if (job.state !== 'succeeded') throw new Error(job.message || `Export ${job.state}.`);
  if (!job.artifacts?.length) throw new Error('Export completed, but the server did not return a downloadable artifact.');
  return job;
}

function dataUrlToBlob(dataUrl: string): Blob {
  const [header, payload = ''] = dataUrl.split(',', 2);
  const mimeType = header.match(/^data:([^;,]+)/)?.[1] ?? 'application/octet-stream';
  if (!header.includes(';base64')) return new Blob([decodeURIComponent(payload)], { type: mimeType });
  const bytes = atob(payload);
  const output = new Uint8Array(bytes.length);
  for (let index = 0; index < bytes.length; index += 1) output[index] = bytes.charCodeAt(index);
  return new Blob([output], { type: mimeType });
}

function legendNames(option: EChartsOption): string[] {
  const raw = option as JsonMap;
  const legend = asList(raw.legend).find(isMap);
  if (!legend) return [];
  const explicit = asList(legend.data).map((item) => isMap(item) ? String(item.name ?? '') : String(item)).filter(Boolean);
  return explicit.length ? explicit : [...new Set(asList(raw.series).filter(isMap).map((series) => String(series.name ?? '')).filter(Boolean))];
}

function seriesColor(option: EChartsOption, name: string): string {
  const series = asList((option as JsonMap).series).filter(isMap).find((item) => item.name === name);
  const lineStyle = isMap(series?.lineStyle) ? series.lineStyle : {};
  const itemStyle = isMap(series?.itemStyle) ? series.itemStyle : {};
  if (typeof lineStyle.color === 'string') return lineStyle.color;
  if (typeof itemStyle.color === 'string') return itemStyle.color;
  const palette = asList((option as JsonMap).color);
  const index = Math.max(0, legendNames(option).indexOf(name));
  return typeof palette[index % Math.max(1, palette.length)] === 'string' ? String(palette[index % palette.length]) : '#526ff0';
}

function Chart({ option, compact = false, onReady, onPointClick, aspectRatio }: { option: EChartsOption; compact?: boolean; onReady: (chart?: EChartsType) => void; onPointClick?: (value: unknown) => void; aspectRatio?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const instance = useRef<EChartsType | undefined>(undefined);
  const selectionRef = useRef<Record<string, boolean>>({});
  const [selectionRevision, setSelectionRevision] = useState(0);

  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current, undefined, { renderer: 'canvas' });
    instance.current = chart;
    if (onPointClick) chart.on('click', (event) => onPointClick(event.data));
    onReady(chart);
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => {
      observer.disconnect();
      instance.current = undefined;
      onReady(undefined);
      chart.dispose();
    };
  }, [onPointClick, onReady]);

  useEffect(() => {
    const chart = instance.current;
    if (!chart) return;
    const raw = option as JsonMap;
    const legend = raw.legend;
    const names = legendNames(option);
    const previous = selectionRef.current;
    selectionRef.current = Object.fromEntries(names.map((name) => [name, previous[name] ?? true]));
    const hiddenCanvasLegend = isMap(legend) ? { ...legend, show: false, selected: selectionRef.current } : legend;
    chart.setOption({ ...option, legend: hiddenCanvasLegend } as EChartsOption, { notMerge: true, lazyUpdate: true });
    setSelectionRevision((value) => value + 1);
  }, [option]);

  const ratioParts = aspectRatio?.split('/').map((value) => Number(value.trim()));
  const ratio = ratioParts?.length === 2 && ratioParts[0] > 0 && ratioParts[1] > 0 ? ratioParts[0] / ratioParts[1] : undefined;
  const names = legendNames(option);
  return <>
    {names.length > 0 && <div className="pisa-chart-legend" aria-label="Visible chart series" data-revision={selectionRevision}>{names.map((name) => {
      const visible = selectionRef.current[name] ?? true;
      return <button key={name} type="button" className={`pisa-chart-legend-item${visible ? '' : ' pisa-chart-legend-item--hidden'}`} aria-pressed={visible} onClick={() => {
        selectionRef.current = { ...selectionRef.current, [name]: !visible };
        instance.current?.setOption({ legend: { selected: selectionRef.current } } as EChartsOption);
        setSelectionRevision((value) => value + 1);
      }}><span className="pisa-chart-legend-swatch" style={{ background: seriesColor(option, name) }} />{name}</button>;
    })}</div>}
    <div ref={ref} className={`pisa-chart${compact ? ' pisa-chart--compact' : ''}`} style={ratio ? { aspectRatio, height: 'auto', maxWidth: `${Math.round(760 * ratio)}px`, marginInline: 'auto' } : undefined} role="img" aria-label="Interactive data visualization" />
  </>;
}

function safeArtifactHref(raw: string): string | undefined {
  try {
    const parsed = new URL(raw, window.location.origin);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : undefined;
  } catch {
    return undefined;
  }
}

export function VisualizationCard({
  spec,
  datasetId,
  compact,
  emptyDescription,
  onPointClick,
  aspectRatio,
  onChartReady,
  animationDurationSeconds,
  animationOptionAtProgress,
}: {
  spec: VisualizationSpec;
  datasetId?: string;
  compact?: boolean;
  emptyDescription?: string;
  onPointClick?: (value: unknown) => void;
  aspectRatio?: string;
  onChartReady?: (chart?: EChartsType) => void;
  animationDurationSeconds?: number;
  animationOptionAtProgress?: (progress: number) => EChartsOption;
}) {
  const chartRef = useRef<EChartsType | undefined>(undefined);
  const activePoll = useRef<AbortController | undefined>(undefined);
  const objectUrls = useRef(new Set<string>());
  const queryClient = useQueryClient();
  const [requestedFormat, setRequestedFormat] = useState<string>();
  const [serverJob, setServerJob] = useState<Job>();
  const [localMessage, setLocalMessage] = useState<string>();
  const [localError, setLocalError] = useState<string>();
  const [animationExporting, setAnimationExporting] = useState(false);

  useEffect(() => () => {
    activePoll.current?.abort();
    for (const url of objectUrls.current) URL.revokeObjectURL(url);
    objectUrls.current.clear();
  }, []);

  const exportMutation = useMutation({
    mutationFn: async ({ request, controller }: { request: ExportRequest; controller: AbortController }) => {
      const queued = await api.datasets.export(datasetId!, request);
      void queryClient.invalidateQueries({ queryKey: ['jobs'] });
      return pollExportJob(queued, controller.signal, setServerJob);
    },
    onSuccess: (job) => {
      setServerJob(job);
      void queryClient.invalidateQueries({ queryKey: ['jobs'] });
    },
  });

  function queue(format: ExportRequest['format'], preset?: ExportRequest['preset'], dpi?: 300 | 600) {
    if (!datasetId) return;
    activePoll.current?.abort();
    const controller = new AbortController();
    activePoll.current = controller;
    setRequestedFormat(format.toUpperCase());
    setServerJob(undefined);
    setLocalError(undefined);
    setLocalMessage(undefined);
    exportMutation.mutate({
      request: {
        ...(spec.artifact_path
          ? { artifact_path: spec.artifact_path }
          : { visualization_id: spec.id }),
        format,
        preset,
        dpi,
        background: 'white',
      },
      controller,
    });
  }

  function downloadBlob(blob: Blob, extension: string) {
    const url = URL.createObjectURL(blob);
    objectUrls.current.add(url);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = safeVisualizationFilename(spec.title, spec.id, extension);
    anchor.rel = 'noopener';
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => {
      URL.revokeObjectURL(url);
      objectUrls.current.delete(url);
    }, 1_000);
  }

  function exportLocal(format: LocalExportFormat) {
    setLocalError(undefined);
    setLocalMessage(undefined);
    setRequestedFormat(format.toUpperCase());
    try {
      const currentOption = (chartRef.current?.getOption?.() ?? spec.option) as EChartsOption;
      if (format === 'csv') {
        downloadBlob(new Blob([visualizationToCsv(currentOption as JsonMap)], { type: 'text/csv;charset=utf-8' }), 'csv');
      } else if (format === 'json') {
        const payload = { schema: 'pisa.visualization/v1', id: spec.id, title: spec.title, subtitle: spec.subtitle, kind: spec.kind, data_hash: spec.data_hash, option: currentOption };
        downloadBlob(new Blob([`${JSON.stringify(payload, null, 2)}\n`], { type: 'application/json' }), 'json');
      } else if (format === 'png') {
        if (!chartRef.current) throw new Error('The chart is not ready to export yet.');
        const dataUrl = chartRef.current.getDataURL({ type: 'png', pixelRatio: 4, backgroundColor: '#ffffff' });
        downloadBlob(dataUrlToBlob(dataUrl), 'png');
      } else {
        const host = document.createElement('div');
        host.setAttribute('aria-hidden', 'true');
        host.style.cssText = 'position:fixed;left:-10000px;top:0;width:1600px;height:1000px;pointer-events:none';
        document.body.append(host);
        let vectorChart: EChartsType | undefined;
        try {
          vectorChart = echarts.init(host, undefined, { renderer: 'svg', width: 1600, height: 1000 });
          vectorChart.setOption({
            ...currentOption,
            animation: false,
            legend: { ...((asList((currentOption as JsonMap).legend).find(isMap) ?? {})), show: false },
            backgroundColor: '#ffffff',
            textStyle: { fontFamily: 'Arial, Helvetica, sans-serif', ...(isMap(spec.option.textStyle) ? spec.option.textStyle : {}) },
          } as EChartsOption, { notMerge: true });
          downloadBlob(new Blob([vectorChart.renderToSVGString({ useViewBox: true })], { type: 'image/svg+xml;charset=utf-8' }), 'svg');
        } finally {
          vectorChart?.dispose();
          host.remove();
        }
      }
      setLocalMessage(format === 'png' || format === 'svg'
        ? `${format.toUpperCase()} downloaded with a clean white background.`
        : `${format.toUpperCase()} downloaded for reproducible analysis.`);
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : 'The visualization could not be exported.');
    }
  }

  async function exportAnimatedChart(format: 'gif' | 'mp4') {
    const chart = chartRef.current;
    if (!chart || animationExporting) return;
    setAnimationExporting(true);
    setLocalError(undefined);
    setLocalMessage(undefined);
    setRequestedFormat(format.toUpperCase());
    const liveOption = chart.getOption() as EChartsOption;
    const liveLegend = asList((liveOption as JsonMap).legend).find(isMap) ?? {};
    const originalSeries = asList(spec.option.series).filter(isMap);
    let exportChart: EChartsType | undefined;
    let exportHost: HTMLDivElement | undefined;
    try {
      if (!originalSeries.length) throw new Error('This chart has no series to animate.');
      const ratioParts = aspectRatio?.split('/').map((value) => Number(value.trim()));
      const ratio = ratioParts?.length === 2 && ratioParts[0] > 0 && ratioParts[1] > 0 ? ratioParts[0] / ratioParts[1] : 16 / 9;
      const width = 1600, height = Math.max(720, Math.min(1600, Math.round(width / ratio)));
      exportHost = document.createElement('div');
      exportHost.setAttribute('aria-hidden', 'true');
      exportHost.style.cssText = `position:fixed;left:-10000px;top:0;width:${width}px;height:${height}px;pointer-events:none;background:#fff`;
      document.body.append(exportHost);
      exportChart = echarts.init(exportHost, undefined, { renderer: 'canvas', width, height, devicePixelRatio: 1 });
      const initialOption = animationOptionAtProgress ? animationOptionAtProgress(0) : spec.option as EChartsOption;
      exportChart.setOption({ ...initialOption, animation: false, backgroundColor: '#ffffff', legend: { ...((asList((initialOption as JsonMap).legend).find(isMap) ?? {})), show: false, selected: liveLegend.selected } } as EChartsOption, { notMerge: true, lazyUpdate: false });
      exportChart.getZr().refreshImmediately();
      const canvas = exportChart.getDom().querySelector('canvas');
      if (!(canvas instanceof HTMLCanvasElement) || typeof canvas.captureStream !== 'function') throw new Error('Animated chart export is not supported by this browser.');
      const stream = canvas.captureStream(30);
      const mimeType = MediaRecorder.isTypeSupported('video/webm;codecs=vp9') ? 'video/webm;codecs=vp9' : 'video/webm';
      const duration = Math.max(0.25, animationDurationSeconds ?? 5);
      const recorder = new MediaRecorder(stream, { mimeType, videoBitsPerSecond: duration > 60 ? 8_000_000 : 16_000_000 });
      const chunks: Blob[] = [];
      recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
      recorder.start();
      const started = performance.now();
      await new Promise<void>((resolve) => {
        const frame = () => {
          const progress = Math.min(1, (performance.now() - started) / 1000 / duration);
          const frameOption = animationOptionAtProgress
            ? animationOptionAtProgress(progress)
            : ({ ...spec.option, animation: false, series: originalSeries.map((series) => ({ ...series, data: asList(series.data).slice(0, Math.max(1, Math.ceil(asList(series.data).length * progress))) })) } as EChartsOption);
          exportChart!.setOption({ ...frameOption, animation: false, backgroundColor: '#ffffff', legend: { ...((asList((frameOption as JsonMap).legend).find(isMap) ?? {})), show: false, selected: liveLegend.selected } } as EChartsOption, { notMerge: true, lazyUpdate: false });
          exportChart!.getZr().refreshImmediately();
          if (progress >= 1) resolve(); else requestAnimationFrame(frame);
        };
        frame();
      });
      const stopped = new Promise<void>((resolve) => { recorder.onstop = () => resolve(); });
      recorder.stop();
      await stopped;
      const webm = new Blob(chunks, { type: 'video/webm' });
      const response = await fetch(`/api/v1/tools/animation/transcode?format=${format}`, { method: 'POST', headers: { 'Content-Type': 'video/webm' }, body: webm });
      if (!response.ok) throw new Error(`Unable to encode ${format.toUpperCase()}.`);
      downloadBlob(await response.blob(), format);
      setLocalMessage(`${format.toUpperCase()} animation exported at the current replay speed.`);
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : 'Animated chart export failed.');
    } finally {
      exportChart?.dispose();
      exportHost?.remove();
      setAnimationExporting(false);
    }
  }

  const handleChartReady = useCallback((chart?: EChartsType) => { chartRef.current = chart; onChartReady?.(chart); }, [onChartReady]);
  const hasChart = Object.keys(spec.option).length > 0;
  const busy = exportMutation.isPending || animationExporting;
  const artifactLinks = serverJob?.artifacts?.flatMap((artifact) => {
    const href = safeArtifactHref(artifact.url);
    return href ? [{ ...artifact, href }] : [];
  }) ?? [];
  const serverError = exportMutation.error instanceof Error ? exportMutation.error.message : undefined;
  const unsafeArtifactError = serverJob?.state === 'succeeded' && serverJob.artifacts?.length && !artifactLinks.length
    ? 'Export completed, but its artifact URL was not safe to open.'
    : undefined;

  return (
    <Card p={0} style={{ overflow: 'hidden' }}>
      <Group justify="space-between" align="flex-start" p="md" wrap="nowrap">
        <Stack gap={2}>
          <Group gap="xs">
            <Text fw={650} size="sm">{spec.title}</Text>
            {spec.clipped_count ? <Badge size="xs" color="yellow" variant="light">{spec.clipped_count} clipped</Badge> : null}
          </Group>
          {spec.subtitle && <Text c="dimmed" size="xs">{spec.subtitle}</Text>}
          {spec.raw_range && <Text c="dimmed" size="xs">Raw range: {spec.raw_range[0].toLocaleString()}–{spec.raw_range[1].toLocaleString()}</Text>}
        </Stack>
        <Menu shadow="md" width={240} position="bottom-end" keepMounted>
          <Menu.Target>
            <Tooltip label={!hasChart && !datasetId ? 'No chart data available to export' : datasetId ? 'Export visualization on the server' : 'Export visualization in this browser'}>
              <ActionIcon aria-label="Export visualization" variant="subtle" color="gray" disabled={(!hasChart && !datasetId) || busy}>
                {busy ? <IconRefresh className="pisa-spin" size={17} /> : <IconDownload size={17} />}
              </ActionIcon>
            </Tooltip>
          </Menu.Target>
          <Menu.Dropdown>
            {animationDurationSeconds !== undefined && (spec.kind === 'line' || animationOptionAtProgress) && <><Menu.Label>Animated replay</Menu.Label><Menu.Item leftSection={<IconMovie size={15} />} onClick={() => void exportAnimatedChart('gif')}>GIF · current speed</Menu.Item><Menu.Item leftSection={<IconMovie size={15} />} onClick={() => void exportAnimatedChart('mp4')}>MP4 · current speed</Menu.Item><Menu.Divider /></>}
            {datasetId ? (
              <>
                <Menu.Label>Publication vector</Menu.Label>
                <Menu.Item leftSection={<IconPhoto size={15} />} rightSection={<IconChevronDown size={13} />} onClick={() => queue('svg', 'paper-single')}>SVG · 85 mm</Menu.Item>
                <Menu.Item leftSection={<IconPhoto size={15} />} onClick={() => queue('pdf', 'paper-double')}>PDF · 180 mm</Menu.Item>
                <Menu.Label>Raster</Menu.Label>
                <Menu.Item leftSection={<IconPresentation size={15} />} onClick={() => queue('png', 'slides-hd')}>PNG · 1920 × 1080</Menu.Item>
                <Menu.Item leftSection={<IconPhoto size={15} />} onClick={() => queue('png', 'paper-double', 600)}>PNG · 600 DPI</Menu.Item>
                <Menu.Divider />
                <Menu.Item leftSection={<IconFileSpreadsheet size={15} />} onClick={() => queue('csv')}>Underlying data · CSV</Menu.Item>
                <Menu.Item leftSection={<IconFileCode size={15} />} onClick={() => queue('json')}>View specification · JSON</Menu.Item>
              </>
            ) : (
              <>
                <Menu.Label>Publication image</Menu.Label>
                <Menu.Item leftSection={<IconPresentation size={15} />} onClick={() => exportLocal('png')}>PNG · high resolution</Menu.Item>
                <Menu.Item leftSection={<IconPhoto size={15} />} onClick={() => exportLocal('svg')}>SVG · editable vector</Menu.Item>
                <Menu.Divider />
                <Menu.Label>Reproducibility</Menu.Label>
                <Menu.Item leftSection={<IconFileSpreadsheet size={15} />} onClick={() => exportLocal('csv')}>Underlying data · CSV</Menu.Item>
                <Menu.Item leftSection={<IconFileCode size={15} />} onClick={() => exportLocal('json')}>View specification · JSON</Menu.Item>
              </>
            )}
          </Menu.Dropdown>
        </Menu>
      </Group>
      <Divider />
      {spec.kind === 'image' && spec.source_url ? (
        <div className={`pisa-chart${compact ? ' pisa-chart--compact' : ''}`} style={{ display: 'grid', placeItems: 'center', padding: 24 }}>
          <img src={spec.source_url} alt={spec.title} style={{ maxWidth: '100%', maxHeight: '100%', objectFit: 'contain' }} />
        </div>
      ) : hasChart ? <Chart option={spec.option as EChartsOption} compact={compact} onReady={handleChartReady} onPointClick={onPointClick} aspectRatio={aspectRatio} /> : (
        <EmptyState title="No values for this view" description={emptyDescription ?? 'Adjust the report filters or choose another experiment.'} />
      )}
      {(requestedFormat || serverError || unsafeArtifactError || localMessage || localError) && (
        <Stack px="md" pb="sm" gap={4} align="flex-end">
          {serverError || unsafeArtifactError || localError ? <Text role="alert" size="xs" c="red">{serverError ?? unsafeArtifactError ?? localError}</Text> : null}
          {busy && <Text role="status" aria-live="polite" size="xs" c="blue">{requestedFormat} export · {serverJob?.phase ?? serverJob?.state ?? 'queueing'}…</Text>}
          {!busy && localMessage && <Text role="status" aria-live="polite" size="xs" c="teal">{localMessage}</Text>}
          {!busy && serverJob?.state === 'succeeded' && artifactLinks.map((artifact) => (
            <Text key={artifact.href} component="a" href={artifact.href} target="_blank" rel="noopener noreferrer" size="xs" c="teal" td="underline" aria-label={`Open exported artifact ${artifact.name}`}>
              Open {artifact.name}
            </Text>
          ))}
        </Stack>
      )}
    </Card>
  );
}
