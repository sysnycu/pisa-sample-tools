import { useQuery } from '@tanstack/react-query';
import { api } from './client';

export function useDatasets(search = '', root?: string, recursive = true) {
  return useQuery({ queryKey: ['datasets', search, root, recursive], queryFn: () => api.datasets.list(search, root, recursive), retry: 1 });
}

export function useJobs() {
  return useQuery({
    queryKey: ['jobs'],
    queryFn: api.jobs.list,
    refetchInterval: (query) => query.state.data?.items.some((job) => job.state === 'queued' || job.state === 'running') ? 2_000 : 10_000,
    retry: 1,
  });
}

export function useReportSummary(datasetId?: string) {
  return useQuery({
    queryKey: ['report-summary', datasetId],
    queryFn: () => api.datasets.summary(datasetId!),
    enabled: Boolean(datasetId),
    retry: 1,
  });
}

export function useReportCharts(datasetId: string | undefined, section: string) {
  return useQuery({
    queryKey: ['report-charts', datasetId, section],
    queryFn: () => api.datasets.charts(datasetId!, section),
    enabled: Boolean(datasetId),
    retry: 1,
  });
}
