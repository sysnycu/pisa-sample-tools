import { MantineProvider } from '@mantine/core';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useJobs } from '../api/query';
import { JobsDrawer } from '../components/JobsDrawer';
import { theme } from '../theme';

vi.mock('../api/query', () => ({ useJobs: vi.fn() }));

describe('JobsDrawer artifact links', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('opens safe artifacts in an isolated tab and hides unsafe schemes', () => {
    vi.mocked(useJobs).mockReturnValue({
      data: {
        items: [{
          id: 'job-1', kind: 'export', title: 'Paper figure', state: 'succeeded', created_at: '2026-07-14T00:00:00Z',
          artifacts: [
            { name: 'figure.svg', url: '/api/v1/artifacts/figure.svg' },
            { name: 'unsafe.html', url: 'javascript:alert(1)' },
          ],
        }],
      },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof useJobs>);

    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(
      <MantineProvider theme={theme}>
        <QueryClientProvider client={client}><JobsDrawer opened onClose={() => undefined} /></QueryClientProvider>
      </MantineProvider>,
    );

    const link = screen.getByRole('link', { name: 'Open artifact figure.svg' });
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.getByText('Artifact link unavailable: unsafe.html')).toBeInTheDocument();
  });
});
