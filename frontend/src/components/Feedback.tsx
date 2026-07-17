import type { ReactNode } from 'react';
import { Alert, Button, Center, Loader, Stack, Text, ThemeIcon } from '@mantine/core';
import { IconAlertTriangle, IconDatabaseOff } from '@tabler/icons-react';

export function PageLoading({ label = 'Loading workspace…' }: { label?: string }) {
  return <Center mih={280}><Stack align="center" gap="sm"><Loader size="sm" /><Text c="dimmed" size="sm">{label}</Text></Stack></Center>;
}

export function InlineError({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : 'The request could not be completed.';
  return (
    <Alert color="red" variant="light" icon={<IconAlertTriangle size={18} />} title="Unable to load data">
      <Text size="sm" mb={onRetry ? 'sm' : 0}>{message}</Text>
      {onRetry && <Button variant="light" color="red" size="xs" onClick={onRetry}>Try again</Button>}
    </Alert>
  );
}

export function EmptyState({ title, description, action, icon }: { title: string; description: string; action?: ReactNode; icon?: ReactNode }) {
  return (
    <Center mih={220} p="xl">
      <Stack align="center" gap="xs" maw={440} ta="center">
        <ThemeIcon size={46} radius="xl" variant="light" color="gray">{icon ?? <IconDatabaseOff size={23} />}</ThemeIcon>
        <Text fw={600}>{title}</Text>
        <Text size="sm" c="dimmed">{description}</Text>
        {action}
      </Stack>
    </Center>
  );
}
