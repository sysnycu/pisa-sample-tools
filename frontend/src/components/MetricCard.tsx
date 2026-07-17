import type { ReactNode } from 'react';
import { Card, Group, Stack, Text, ThemeIcon } from '@mantine/core';

export function MetricCard({ label, value, detail, icon, color = 'indigo' }: { label: string; value: ReactNode; detail?: string; icon: ReactNode; color?: string }) {
  return (
    <Card p="lg" style={{ gridColumn: 'span 3' }}>
      <Group justify="space-between" align="flex-start" wrap="nowrap">
        <Stack gap={5}>
          <Text c="dimmed" size="xs" fw={600} tt="uppercase" lts="0.04em">{label}</Text>
          <Text fz={26} fw={700} lh={1.1}>{value}</Text>
          {detail && <Text c="dimmed" size="xs">{detail}</Text>}
        </Stack>
        <ThemeIcon variant="light" color={color} radius="md" size={40}>{icon}</ThemeIcon>
      </Group>
    </Card>
  );
}
