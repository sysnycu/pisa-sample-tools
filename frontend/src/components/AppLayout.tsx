import { useState } from 'react';
import { ActionIcon, AppShell, Avatar, Burger, Button, Divider, Group, NavLink, ScrollArea, Stack, Text, ThemeIcon, Tooltip } from '@mantine/core';
import { useDisclosure } from '@mantine/hooks';
import {
  IconAdjustments, IconBell, IconChartDots3, IconChevronLeft, IconChevronRight, IconFlask2, IconLayoutDashboard,
  IconPlayerPlay, IconSettingsAutomation, IconSparkles,
} from '@tabler/icons-react';
import { Link, Outlet, useLocation } from 'react-router-dom';
import { JobsDrawer } from './JobsDrawer';

const nav = [
  { label: 'Dashboard', description: 'Reports and data health', path: '/', icon: IconLayoutDashboard },
  { label: 'Experiments', description: 'Configure and run', path: '/experiments', icon: IconPlayerPlay },
  { label: 'Samples', description: 'Preview, export, analyze', path: '/samples', icon: IconSparkles },
  { label: 'Reports', description: 'Build, manage, and open', path: '/#reports', icon: IconChartDots3 },
  { label: 'Advanced tools', description: 'Trajectory and repair', path: '/advanced', icon: IconSettingsAutomation },
];

export function AppLayout() {
  const [navOpened, navControls] = useDisclosure(false);
  const [navExpanded, setNavExpanded] = useState(false);
  const [jobsOpened, setJobsOpened] = useState(false);
  const location = useLocation();
  const displayExpanded = navExpanded || navOpened;

  return (
    <AppShell
      header={{ height: 64 }}
      navbar={{ width: { base: 268, md: navExpanded ? 268 : 72 }, breakpoint: 'md', collapsed: { mobile: !navOpened } }}
      padding={{ base: 'md', sm: 'xl' }}
    >
      <AppShell.Header px={{ base: 'md', sm: 'lg' }}>
        <Group h="100%" justify="space-between" wrap="nowrap">
          <Group gap="sm" wrap="nowrap">
            <Burger opened={navOpened} onClick={navControls.toggle} hiddenFrom="md" size="sm" aria-label="Toggle navigation" />
            <ThemeIcon size={36} radius="md" variant="gradient" gradient={{ from: 'indigo', to: 'cyan', deg: 135 }}><IconFlask2 size={20} /></ThemeIcon>
            <div>
              <Text fw={750} lh={1.1}>PISA</Text>
              <Text size="xs" c="dimmed">Research Console</Text>
            </div>
          </Group>
          <Group gap="xs">
            <Tooltip label="Jobs, experiments, and exports">
              <Button variant="subtle" color="gray" leftSection={<IconBell size={17} />} onClick={() => setJobsOpened(true)} visibleFrom="xs">Jobs</Button>
            </Tooltip>
            <Avatar size="sm" color="indigo" variant="light">P</Avatar>
          </Group>
        </Group>
      </AppShell.Header>
      <AppShell.Navbar p={displayExpanded ? 'md' : 'xs'} data-expanded={displayExpanded || undefined}>
        <AppShell.Section mb="sm" visibleFrom="md">
          <Group justify={navExpanded ? 'flex-end' : 'center'}>
            <Tooltip label={navExpanded ? 'Collapse workspace' : 'Expand workspace'} position="right">
              <ActionIcon size="xl" variant="subtle" color="gray" onClick={() => setNavExpanded((value) => !value)} aria-label={navExpanded ? 'Collapse workspace navigation' : 'Expand workspace navigation'}>
                {navExpanded ? <IconChevronLeft size={24} /> : <IconChevronRight size={24} />}
              </ActionIcon>
            </Tooltip>
          </Group>
        </AppShell.Section>
        <AppShell.Section grow component={ScrollArea}>
          {displayExpanded && <Text size="xs" c="dimmed" fw={700} tt="uppercase" lts="0.08em" px="sm" mb="xs">Workspace</Text>}
          <Stack gap={4}>
            {nav.map((item) => {
              const active = item.path === '/'
                ? location.pathname === '/' && location.hash !== '#reports'
                : item.path === '/#reports'
                  ? location.pathname.startsWith('/reports') || (location.pathname === '/' && location.hash === '#reports')
                  : location.pathname.startsWith(item.path);
              return <Tooltip key={item.path} label={item.label} position="right" disabled={displayExpanded}>
                <NavLink
                  component={Link}
                  to={item.path}
                  aria-label={item.label}
                  active={active}
                  onClick={navControls.close}
                  className={`pisa-nav-link${displayExpanded ? '' : ' pisa-nav-link--collapsed'}`}
                  label={displayExpanded ? item.label : undefined}
                  description={displayExpanded ? item.description : undefined}
                  leftSection={<item.icon size={displayExpanded ? 19 : 24} stroke={1.7} />}
                  rightSection={displayExpanded ? <IconChevronRight size={14} /> : undefined}
                  variant="light"
                  color="indigo"
                />
              </Tooltip>;
            })}
          </Stack>
        </AppShell.Section>
        <AppShell.Section>
          <Divider mb="md" />
          <Group gap="sm" px={displayExpanded ? 'sm' : 0} justify={displayExpanded ? 'flex-start' : 'center'} wrap="nowrap">
            <ThemeIcon variant="light" color="teal"><IconAdjustments size={17} /></ThemeIcon>
            {displayExpanded && <div>
              <Text size="xs" fw={600}>Local workspace</Text>
              <Text size="xs" c="dimmed">127.0.0.1 · API v1</Text>
            </div>}
          </Group>
        </AppShell.Section>
      </AppShell.Navbar>
      <AppShell.Main>
        <main className="pisa-main"><Outlet /></main>
      </AppShell.Main>
      <JobsDrawer opened={jobsOpened} onClose={() => setJobsOpened(false)} />
    </AppShell>
  );
}
