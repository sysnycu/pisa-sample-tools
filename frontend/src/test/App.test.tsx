import { MantineProvider } from '@mantine/core';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { theme } from '../theme';

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
    expect(screen.getByRole('tab', { name: 'Run detail / replay' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Provenance & health' })).toBeInTheDocument();
  });

  it('keeps advanced source repair behind an explicit workflow', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ items: [] }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    renderApp('/advanced');
    expect(await screen.findByText('Precise tools, explicit consequences')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', { name: /Agent-state repair/i }));
    expect(screen.getByText(/Nothing is written during the scan/i)).toBeInTheDocument();
  });
});
