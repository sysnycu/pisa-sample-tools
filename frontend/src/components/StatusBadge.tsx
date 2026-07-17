import { Badge } from '@mantine/core';

const colors: Record<string, string> = {
  ready: 'teal', succeeded: 'teal', success: 'teal',
  building: 'blue', running: 'blue', queued: 'indigo',
  legacy: 'yellow', warning: 'yellow', invalid: 'orange',
  unavailable: 'gray', cancelled: 'gray', unknown: 'gray', info: 'blue',
  failed: 'red', fail: 'red', error: 'red',
};

export function StatusBadge({ value }: { value: string }) {
  return <Badge size="sm" variant="light" color={colors[value] ?? 'gray'}>{value.replaceAll('_', ' ')}</Badge>;
}
