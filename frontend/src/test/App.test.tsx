import { MantineProvider } from '@mantine/core';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { buildDisagreementHeatmap, compareScatterCategories, formatHeatmapCellLabel, interpolateColorRange, pairedAgreementBoundarySegment, replayChartWithVisibleAxes } from '../pages/ReportWorkspacePage';
import { theme } from '../theme';

vi.mock('../components/VisualizationCard', () => ({
  VisualizationCard: ({ seriesLabels, seriesVisibility, onSeriesVisibilityChange }: { seriesLabels?: Record<string, string>; seriesVisibility?: Record<string, boolean>; onSeriesVisibilityChange?: (name: string, visible: boolean) => void }) => <div>{Object.entries(seriesLabels ?? {}).map(([name, label]) => <button key={name} type="button" onClick={() => onSeriesVisibilityChange?.(name, seriesVisibility?.[name] === false)}>{label}</button>)}</div>,
}));

function renderApp(path = '/') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <MantineProvider theme={theme}>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[path]}><App /></MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
});

describe('PISA Research Console', () => {
  it('renders the dashboard shell and handles an empty report library', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      const payload = url.includes('/reports/browser')
        ? { path: '/tmp/reports', parent: '/tmp', roots: ['/tmp'], entries: [] }
        : { items: [], total: 0 };
      return new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } });
    });
    renderApp('/');
    expect(await screen.findByText('From experiments to defensible evidence')).toBeInTheDocument();
    expect(await screen.findByText('No reports indexed yet')).toBeInTheDocument();
  });

  it('navigates to the dedicated Samples workspace', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ items: [] }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    renderApp('/');
    await userEvent.click(await screen.findByRole('link', { name: /Samples/i }));
    expect(await screen.findByText('Design sample sets you can trust', {}, { timeout: 3_000 })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Preview & generate/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Export & shard/i })).toBeInTheDocument();
  });

  it('shows every report workspace section', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ items: [] }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    renderApp('/reports/dataset-1/overview');
    expect(await screen.findByRole('tab', { name: 'Overview' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Outcomes & safety' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Consistency' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Run detail / replay' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Provenance & health' })).toBeInTheDocument();
  });

  it('uses the Dashboard as the only report browser', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      const payload = url.includes('/reports/browser')
        ? { path: '/tmp/reports', parent: '/tmp', roots: ['/tmp'], entries: [] }
        : { items: [], total: 0 };
      return new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } });
    });
    renderApp('/reports');
    expect(await screen.findByText('From experiments to defensible evidence')).toBeInTheDocument();
    expect(screen.getByText('Report browser')).toBeInTheDocument();
    expect(screen.queryByText('Choose a report workspace')).not.toBeInTheDocument();
  });

  it('separates experiment selection from the save-or-preview destination step', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      let payload: unknown = { items: [] };
      if (url.includes('/experiment-preview')) payload = { dataset_id: 'demo', results: '/opt/sbsvf/outputs/demo', scenario_name: 'cut-in', simulator: 'esmini', av: 'simple-av', sampler: 'lhs', run_count: 10, suggested_report_name: 'cut-in-esmini-simple-av-lhs10' };
      else if (url.includes('/reports/browser') && url.includes(encodeURIComponent('/opt/sbsvf/outputs'))) payload = { path: '/opt/sbsvf/outputs', parent: '/opt/sbsvf', roots: ['/opt/sbsvf/outputs'], entries: [{ name: 'demo', path: '/opt/sbsvf/outputs/demo', kind: 'directory', looks_like_output: true }] };
      else if (url.includes('/reports/browser') && url.includes('analysis')) payload = { path: '/home/hcis-s05/ysws/PISA/pisa-sample-tools/analysis', parent: '/home/hcis-s05/ysws/PISA/pisa-sample-tools', roots: ['/home/hcis-s05/ysws/PISA/pisa-sample-tools'], entries: [] };
      else if (url.includes('/reports/browser')) payload = { path: '/tmp/reports', parent: '/tmp', roots: ['/tmp'], entries: [] };
      return new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } });
    });
    renderApp('/');
    await userEvent.click(await screen.findByRole('button', { name: 'Build report' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Preview experiment' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Add experiment' }));
    await userEvent.click(screen.getByRole('button', { name: 'Continue' }));
    expect(await screen.findByLabelText('Report destination browser')).toHaveValue('/home/hcis-s05/ysws/PISA/pisa-sample-tools/analysis');
    expect(screen.getByRole('button', { name: 'Preview without saving' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Create report' })).toBeEnabled();
  });

  it('shows temporary storage controls in report Overview', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      const payload = url.includes('/preview') || url.includes('/lease')
        ? { id: 'temporary-1', name: 'Quick look', path: '/tmp/preview', storage_kind: 'temporary', expires_at: '2026-07-21T01:00:00Z', experiment_count: 1, run_count: 10, status: 'ready' }
        : url.includes('/charts')
          ? { items: [] }
          : url.includes('/reports/browser')
            ? { path: '/home/hcis-s05/ysws/PISA/pisa-sample-tools/analysis', parent: '/home/hcis-s05/ysws/PISA/pisa-sample-tools', roots: [], entries: [] }
            : { experiment_count: 1, run_count: 10, outcomes: { success: 10, fail: 0, invalid: 0, unknown: 0 }, experiment_summaries: [] };
      return new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } });
    });
    renderApp('/reports/temporary-1/overview');
    expect(await screen.findByText('Temporary report preview')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Discard preview' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Save report' }));
    expect(await screen.findByRole('dialog', { name: 'Save preview report' })).toBeInTheDocument();
    expect(screen.getByLabelText('Report destination browser')).toHaveValue('/home/hcis-s05/ysws/PISA/pisa-sample-tools/analysis');
  });

  it('sorts experiment totals in both directions', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      const payload = url.includes('/preview')
        ? { id: 'dataset-1', name: 'Report', path: '/tmp/report', experiment_count: 2, run_count: 20, status: 'ready' }
        : url.includes('/scatter')
          ? { fields: [], datasets: ['alpha', 'beta'], stop_reasons: [], stop_conditions: [], selection: { x: 'sample_order', y: 'scenario_order', color: 'outcome' }, points: [], returned: 0, scanned: 0, truncated: false }
        : url.includes('/charts')
          ? { items: [] }
          : {
              experiment_count: 2, run_count: 20, outcomes: { success: 10, fail: 10, invalid: 0, unknown: 0 },
              experiment_summaries: [
                { experiment: 'alpha', total_samples: 10, success: 9, fail: 1, invalid: 0, unknown: 0, avg_time_seconds: 2, avg_speedup: 3 },
                { experiment: 'beta', total_samples: 10, success: 1, fail: 9, invalid: 0, unknown: 0, avg_time_seconds: 1, avg_speedup: 2 },
              ],
            };
      return new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } });
    });
    renderApp('/reports/dataset-1/overview');
    await screen.findByText('Experiment totals');
    await userEvent.click(screen.getByRole('button', { name: 'Sort Success ascending' }));
    let rows = screen.getAllByRole('row');
    expect(within(rows[1]).getByText('beta')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Sort Success descending' }));
    rows = screen.getAllByRole('row');
    expect(within(rows[1]).getByText('alpha')).toBeInTheDocument();
    await userEvent.click(within(rows[1]).getByRole('button', { name: /alpha/i }));
    expect(window.sessionStorage.getItem('pisa:sampling:dataset-1:mode')).toBe('"single"');
    expect(window.sessionStorage.getItem('pisa:sampling:dataset-1:dataset')).toBe('"alpha"');
    cleanup();
    renderApp('/reports/dataset-1/overview');
    await screen.findByText('Experiment totals');
    rows = screen.getAllByRole('row');
    expect(within(rows[1]).getByText('alpha')).toBeInTheDocument();
  });

  it('shows scatter category counts and updates the visible point total', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      const payload = url.includes('/preview')
        ? { id: 'dataset-1', name: 'Report', path: '/tmp/report', experiment_count: 1, run_count: 3, status: 'ready' }
        : url.includes('/scatter')
          ? {
              fields: [{ key: 'sample_order', label: 'Sample order', source: 'order' }, { key: 'scenario_order', label: 'Scenario order', source: 'order' }, { key: 'outcome', label: 'Outcome', source: 'outcome' }],
              datasets: ['demo'], stop_reasons: [], stop_conditions: [], selection: { x: 'sample_order', y: 'scenario_order', color: 'outcome' }, returned: 3, scanned: 3, truncated: false,
              points: [
                { x: 1, y: 1, color: 'success', outcome: 'success', ordinal: 1, dataset_id: 'demo', run_id: 'demo:1', scenario_id: '1', sample_id: '1' },
                { x: 2, y: 2, color: 'success', outcome: 'success', ordinal: 2, dataset_id: 'demo', run_id: 'demo:2', scenario_id: '2', sample_id: '2' },
                { x: 3, y: 3, color: 'fail', outcome: 'fail', ordinal: 3, dataset_id: 'demo', run_id: 'demo:3', scenario_id: '3', sample_id: '3' },
              ],
            }
          : url.includes('/charts') ? { items: [] } : { items: [] };
      return new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } });
    });
    renderApp('/reports/dataset-1/sampling');
    await waitFor(() => {
      expect(screen.getByText(/3 \/ 3 points visible/)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'success(2)' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'fail(1)' })).toBeInTheDocument();
    });
    await userEvent.click(screen.getByRole('button', { name: 'success(2)' }));
    await waitFor(() => expect(screen.getByText(/1 \/ 3 points visible/)).toBeInTheDocument());
  });

  it('orders compare outcomes by meaning instead of alphabetically', () => {
    const categories = ['invalid → fail', 'All invalid', 'success → fail', 'All success', 'fail → success', 'All fail'];
    expect(categories.sort((left, right) => compareScatterCategories('outcome', left, right))).toEqual([
      'All success', 'All fail', 'All invalid', 'success → fail', 'fail → success', 'invalid → fail',
    ]);
  });

  it('aggregates paired outcome disagreements into equal-width heatmap cells', () => {
    const result = buildDisagreementHeatmap([
      { x: 0, y: 0, left_outcome: 'success', right_outcome: 'fail' },
      { x: 1, y: 1, left_outcome: 'success', right_outcome: 'success' },
      { x: 10, y: 10, left_outcome: 'fail', right_outcome: 'success' },
    ], 2, 2);
    expect(result).toMatchObject({ xLabels: ['0–5', '5–10'], yLabels: ['0–5', '5–10'], disagreementCount: 2, totalCount: 3 });
    expect(result.cells.map((cell) => cell.value)).toEqual([
      [0, 0, 0.5, 1, 2],
      [1, 1, 1, 1, 1],
    ]);
  });

  it('controls heatmap count and percentage labels independently', () => {
    expect(formatHeatmapCellLabel(34, 37, true, true)).toBe('34/37\n91.9%');
    expect(formatHeatmapCellLabel(34, 37, true, false)).toBe('34/37');
    expect(formatHeatmapCellLabel(34, 37, false, true)).toBe('91.9%');
    expect(formatHeatmapCellLabel(34, 37, false, false)).toBe('');
  });

  it('interpolates the configured three-stop numeric scatter color range', () => {
    const colors: [string, string, string] = ['#000000', '#808080', '#ffffff'];
    expect(interpolateColorRange(0, 0, 1, colors)).toBe('#000000');
    expect(interpolateColorRange(0.25, 0, 1, colors)).toBe('#404040');
    expect(interpolateColorRange(0.5, 0, 1, colors)).toBe('#808080');
    expect(interpolateColorRange(1, 0, 1, colors)).toBe('#ffffff');
  });

  it('clips paired agreement boundaries to the shared square domain', () => {
    expect(pairedAgreementBoundarySegment(0, 20, 0)).toEqual([[0, 0], [20, 20]]);
    expect(pairedAgreementBoundarySegment(0, 20, 5)).toEqual([[0, 5], [15, 20]]);
    expect(pairedAgreementBoundarySegment(0, 20, -5)).toEqual([[5, 0], [20, 15]]);
    expect(pairedAgreementBoundarySegment(0, 5, 10)).toBeUndefined();
  });

  it('keeps only axes used by visible replay series and applies custom colors', () => {
    const chart = replayChartWithVisibleAxes({
      id: 'metrics', title: 'Metrics', kind: 'line',
      option: {
        grid: { left: 82, right: 100 },
        yAxis: [
          { type: 'value', name: 'Distance', position: 'left' },
          { type: 'value', name: 'Speed', position: 'right', offset: 54 },
        ],
        series: [
          { type: 'line', name: 'ego · Distance', yAxisIndex: 0, lineStyle: { width: 2 } },
          { type: 'line', name: 'ego · Speed', yAxisIndex: 1, lineStyle: { width: 2 } },
          { type: 'line', name: '__replay_cursor__', silent: true },
        ],
      },
    }, { 'ego · Distance': false, 'ego · Speed': true }, { 'ego · Speed': '#ff00ff' });

    expect(chart.option.yAxis).toEqual([{ type: 'value', name: 'Speed', position: 'right', offset: undefined }]);
    const series = chart.option.series as Array<Record<string, unknown>>;
    expect(series[1]).toMatchObject({ yAxisIndex: 0, lineStyle: { color: '#ff00ff' }, itemStyle: { color: '#ff00ff' } });
  });

  it('interprets consistency without hiding the original report workspace', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      const payload = url.includes('/consistency') ? {
        quick: {
          schema_version: 1, available: true, dataset_count: 2, canonical_dataset_count: 2, group_count: 1, excluded_duplicate_aliases: [],
          groups: [{ id: 'g1', datasets: ['replicate-a', 'replicate-b'], experiment_count: 2, common_sample_count: 10, union_sample_count: 10, excluded_noncommon_sample_count: 0, information_consistent_count: 8, information_comparable_count: 10, information_agreement_ratio: 0.8, discrete: [{ key: 'outcome', label: 'Outcome', consistent_count: 9, comparable_count: 10, agreement_ratio: 0.9, unavailable_sample_count: 0 }], continuous: [], runtime: [], outcome_patterns: [{ pattern: 'success/success', count: 9, all_replicates_agree: true }], pairwise: [], hash_quality: {} }],
        },
        deep: { state: 'not_generated', profile: 'trajectory_outlier_controls', artifacts: [] },
      } : { id: 'dataset-1', name: 'Repeatability report', path: '/reports/repeatability', run_count: 20, experiment_count: 2, has_index: true };
      return new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } });
    });

    renderApp('/reports/dataset-1/consistency');
    expect(await screen.findByText('Quick indexed consistency')).toBeInTheDocument();
    expect(screen.getByText('How to read this view')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Overview' })).toBeInTheDocument();
    expect(screen.getByText('Deep trajectory and control consistency')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Analyze now' })).toBeInTheDocument();
  });

  it('keeps advanced source repair behind an explicit workflow', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ items: [] }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    renderApp('/advanced');
    expect(await screen.findByText('Precise tools, explicit consequences')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', { name: /Agent-state repair/i }));
    expect(screen.getByText(/Nothing is written during the scan/i)).toBeInTheDocument();
  });
});
