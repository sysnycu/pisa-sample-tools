import type { ReactNode } from 'react';
import { Group, Stack, Text, Title } from '@mantine/core';

export function PageHeader({ eyebrow, title, description, actions }: { eyebrow?: string; title: string; description: string; actions?: ReactNode }) {
  return (
    <Group justify="space-between" align="flex-end" wrap="wrap" mb="xl">
      <Stack gap={4} maw={760}>
        {eyebrow && <Text size="xs" fw={700} c="indigo" tt="uppercase" lts="0.08em">{eyebrow}</Text>}
        <Title order={1} fz={{ base: 28, sm: 34 }} lh={1.15}>{title}</Title>
        <Text c="dimmed" size="sm">{description}</Text>
      </Stack>
      {actions && <Group gap="sm" className="pisa-no-print">{actions}</Group>}
    </Group>
  );
}
