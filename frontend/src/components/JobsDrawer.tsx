import { ActionIcon, Badge, Button, Divider, Drawer, Group, Progress, ScrollArea, Stack, Text } from '@mantine/core';
import { IconExternalLink, IconRefresh, IconX } from '@tabler/icons-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { useJobs } from '../api/query';
import { EmptyState, InlineError, PageLoading } from './Feedback';
import { StatusBadge } from './StatusBadge';

function safeArtifactHref(raw: string): string | undefined {
  try {
    const parsed = new URL(raw, window.location.origin);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : undefined;
  } catch {
    return undefined;
  }
}

export function JobsDrawer({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const jobs = useJobs();
  const queryClient = useQueryClient();
  const cancel = useMutation({
    mutationFn: api.jobs.cancel,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['jobs'] }),
  });

  return (
    <Drawer opened={opened} onClose={onClose} position="right" size="md" title={<Text fw={650}>Jobs & exports</Text>}>
      <Group justify="space-between" mb="md">
        <Text c="dimmed" size="sm">Long-running work continues safely in the background.</Text>
        <ActionIcon variant="subtle" aria-label="Refresh jobs" onClick={() => jobs.refetch()}><IconRefresh size={17} /></ActionIcon>
      </Group>
      {jobs.isLoading ? <PageLoading label="Loading jobs…" /> : jobs.error ? <InlineError error={jobs.error} onRetry={() => jobs.refetch()} /> : !jobs.data?.items.length ? (
        <EmptyState title="No background work" description="Report builds, experiments, media, and exports will appear here." />
      ) : (
        <ScrollArea h="calc(100vh - 150px)" offsetScrollbars>
          <Stack gap={0}>
            {jobs.data.items.map((job, index) => {
              const progress = job.progress?.total ? Math.min(100, (job.progress.current / job.progress.total) * 100) : undefined;
              const active = job.state === 'queued' || job.state === 'running';
              return (
                <div key={job.id}>
                  {index > 0 && <Divider my="md" />}
                  <Stack gap="xs">
                    <Group justify="space-between" wrap="nowrap">
                      <div>
                        <Text size="sm" fw={600}>{job.title}</Text>
                        <Text size="xs" c="dimmed">{job.kind} · {job.phase ?? 'Waiting'}</Text>
                      </div>
                      <StatusBadge value={job.state} />
                    </Group>
                    {active && <Progress value={progress ?? 100} animated={!progress} size="sm" aria-label="Job progress" />}
                    <Group justify="space-between">
                      <Text size="xs" c="dimmed">{job.progress ? `${job.progress.current.toLocaleString()}${job.progress.total ? ` / ${job.progress.total.toLocaleString()}` : ''} ${job.progress.unit ?? ''}` : job.message}</Text>
                      {active && <Button variant="subtle" color="red" size="compact-xs" leftSection={<IconX size={13} />} onClick={() => cancel.mutate(job.id)}>Cancel</Button>}
                    </Group>
                    {job.artifacts?.map((artifact) => {
                      const href = safeArtifactHref(artifact.url);
                      return href ? (
                        <Badge
                          component="a"
                          href={href}
                          target="_blank"
                          rel="noopener noreferrer"
                          key={artifact.url}
                          variant="light"
                          rightSection={<IconExternalLink size={11} aria-hidden="true" />}
                          aria-label={`Open artifact ${artifact.name}`}
                        >
                          {artifact.name}
                        </Badge>
                      ) : <Text key={artifact.url} size="xs" c="red">Artifact link unavailable: {artifact.name}</Text>;
                    })}
                  </Stack>
                </div>
              );
            })}
          </Stack>
        </ScrollArea>
      )}
    </Drawer>
  );
}
