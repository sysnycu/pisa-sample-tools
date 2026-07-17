import { MantineProvider } from '@mantine/core';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api/client';
import { safeVisualizationFilename, VisualizationCard, visualizationToCsv } from '../components/VisualizationCard';
import { theme } from '../theme';

const chartMocks = vi.hoisted(() => {
  const chart = {
    setOption: vi.fn(),
    resize: vi.fn(),
    dispose: vi.fn(),
    getDataURL: vi.fn(() => 'data:image/png;base64,iVBORw0KGgo='),
    renderToSVGString: vi.fn(() => '<svg viewBox="0 0 1600 1000" />'),
    on: vi.fn(),
  };
  return { chart, init: vi.fn(() => chart), use: vi.fn() };
});

vi.mock('echarts/core', () => ({ init: chartMocks.init, use: chartMocks.use }));
vi.mock('echarts/charts', () => ({ BarChart: {}, LineChart: {}, PieChart: {}, ScatterChart: {} }));
vi.mock('echarts/renderers', () => ({ CanvasRenderer: {}, SVGRenderer: {} }));
vi.mock('echarts/components', () => ({
  DataZoomComponent: {}, DatasetComponent: {}, GridComponent: {}, LegendComponent: {}, MarkLineComponent: {},
  TitleComponent: {}, TooltipComponent: {}, TransformComponent: {}, VisualMapComponent: {},
}));

const spec = {
  id: 'sample-preview',
  title: 'Sample coverage',
  kind: 'scatter' as const,
  option: {
    xAxis: { type: 'value' },
    yAxis: { type: 'value' },
    series: [{ name: 'Samples', type: 'scatter', data: [[1, 2], [3, 4]] }],
  },
};

function renderCard(datasetId?: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <MantineProvider theme={theme}>
      <QueryClientProvider client={client}><VisualizationCard spec={spec} datasetId={datasetId} /></QueryClientProvider>
    </MantineProvider>,
  );
}

describe('VisualizationCard exports', () => {
  beforeEach(() => {
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:pisa-export') });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('exports a local chart as a high-resolution PNG without requiring a report', async () => {
    renderCard();
    fireEvent.click(screen.getByRole('button', { name: 'Export visualization' }));
    fireEvent.click(screen.getByText('PNG · high resolution'));

    expect(chartMocks.chart.getDataURL).toHaveBeenCalledWith(expect.objectContaining({ pixelRatio: 4, backgroundColor: '#ffffff' }));
    expect(URL.createObjectURL).toHaveBeenCalledWith(expect.objectContaining({ type: 'image/png' }));
    expect(screen.getByRole('status')).toHaveTextContent('PNG downloaded');
  });

  it('polls a queued server export and exposes the completed artifact', async () => {
    vi.spyOn(api.datasets, 'export').mockResolvedValue({
      id: 'export-1', kind: 'svg_export', title: 'SVG export', state: 'queued', created_at: '2026-07-14T00:00:00Z',
    });
    vi.spyOn(api.jobs, 'get').mockResolvedValue({
      id: 'export-1', kind: 'svg_export', title: 'SVG export', state: 'succeeded', created_at: '2026-07-14T00:00:00Z',
      artifacts: [{ name: 'coverage.svg', url: '/api/v1/jobs/export-1/artifacts/coverage.svg' }],
    });

    renderCard('report-1');
    fireEvent.click(screen.getByRole('button', { name: 'Export visualization' }));
    fireEvent.click(screen.getByText('SVG · 85 mm'));

    const link = await screen.findByRole('link', { name: 'Open exported artifact coverage.svg' }, { timeout: 3_000 });
    expect(api.jobs.get).toHaveBeenCalledWith('export-1');
    expect(link).toHaveAttribute('href', 'http://localhost:3000/api/v1/jobs/export-1/artifacts/coverage.svg');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });
});

describe('visualization export serialization', () => {
  it('serializes plotted series in clean long-form CSV', () => {
    expect(visualizationToCsv(spec.option)).toBe(
      'series,index,x,y,name,value\r\nSamples,0,1,2,,2\r\nSamples,1,3,4,,4\r\n',
    );
  });

  it('uses stable filesystem-safe names', () => {
    expect(safeVisualizationFilename('Minimum TTC (秒)', '../unsafe id', 'svg')).toBe('minimum-ttc-unsafe-id.svg');
  });
});
