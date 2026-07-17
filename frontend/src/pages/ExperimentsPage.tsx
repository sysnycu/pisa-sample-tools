import { useEffect, useMemo, useState } from 'react';
import {
  ActionIcon, Alert, Badge, Button, Card, Checkbox, Code, Divider, Group, Menu,
  Modal, Select, SimpleGrid, Stack, Stepper, Tabs, Text, TextInput, ThemeIcon,
} from '@mantine/core';
import {
  IconBox, IconDeviceFloppy, IconDotsVertical, IconEdit, IconPlayerPlay, IconRefresh,
  IconRestore, IconServer, IconSettings, IconTrash,
} from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import type { ExperimentPreset, RunnerAction, RunnerResumeAction } from '../api/types';
import { EmptyState, InlineError, PageLoading } from '../components/Feedback';
import { PageHeader } from '../components/PageHeader';

const actions: Array<{ value: RunnerAction; label: string; description: string }> = [
  { value: 'run_all', label: 'Full workflow', description: 'Validate, build images, start services, run samples, and stop services.' },
  { value: 'build', label: 'Build images only', description: 'Validate the preset and build its simulator and AV images.' },
  { value: 'start', label: 'Start services only', description: 'Start the configured simulator and AV containers without executing samples.' },
  { value: 'report', label: 'Report existing output', description: 'Build the preset report from an already completed output directory.' },
];

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

export function ExperimentsPage() {
  const presets = useQuery({ queryKey: ['presets'], queryFn: api.experiments.presets, retry: 1 });
  const resources = useQuery({ queryKey: ['resources'], queryFn: api.experiments.resources, retry: 1 });
  const jobs = useQuery({ queryKey: ['jobs'], queryFn: api.jobs.list, retry: 1, refetchInterval: 5_000 });
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<ExperimentPreset>();
  const [activeStep, setActiveStep] = useState(0);
  const [presetId, setPresetId] = useState('cutin-validation');
  const [label, setLabel] = useState('Cut-in validation');
  const [templateId, setTemplateId] = useState<string | null>(null);
  const [simulatorComponent, setSimulatorComponent] = useState<string | null>(null);
  const [avComponent, setAvComponent] = useState<string | null>(null);
  const [action, setAction] = useState<RunnerAction>('run_all');
  const [createReport, setCreateReport] = useState(true);
  const [managePreset, setManagePreset] = useState<ExperimentPreset>();
  const [manageTab, setManageTab] = useState<string | null>('update');
  const [editLabel, setEditLabel] = useState('');
  const [editSimulator, setEditSimulator] = useState<string | null>(null);
  const [editAv, setEditAv] = useState<string | null>(null);
  const [editTags, setEditTags] = useState('');
  const [renameId, setRenameId] = useState('');
  const [renameLabel, setRenameLabel] = useState('');
  const [deleteConfirmation, setDeleteConfirmation] = useState('');
  const [resumeJobId, setResumeJobId] = useState<string | null>(null);
  const [resumeAction, setResumeAction] = useState<RunnerResumeAction>('run');

  const simulatorOptions = useMemo(() => presets.data?.components.filter((component) => component.kind === 'simulator').map((component) => ({ value: component.id, label: component.name })) ?? [], [presets.data]);
  const avOptions = useMemo(() => presets.data?.components.filter((component) => component.kind === 'av').map((component) => ({ value: component.id, label: component.name })) ?? [], [presets.data]);
  const presetOptions = presets.data?.items.map((preset) => ({ value: preset.id, label: preset.name })) ?? [];
  const terminalJobs = useMemo(() => jobs.data?.items.filter((job) => (
    job.source === 'runner' && ['succeeded', 'failed', 'cancelled'].includes(job.state)
  )) ?? [], [jobs.data]);

  useEffect(() => {
    if (!presets.data?.items.length) return;
    const template = presets.data.items.find((preset) => preset.id === templateId) ?? presets.data.items[0];
    if (!templateId) setTemplateId(template.id);
    if (!simulatorComponent) setSimulatorComponent(template.simulator ?? simulatorOptions[0]?.value ?? null);
    if (!avComponent) setAvComponent(template.automation ?? avOptions[0]?.value ?? null);
  }, [avComponent, avOptions, presets.data, simulatorComponent, simulatorOptions, templateId]);

  const run = useMutation({
    mutationFn: () => api.experiments.run({
      experiment_id: selected!.id,
      action,
      ...(action === 'run_all' ? { overrides: { analysis: { auto: createReport } } } : {}),
    }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['jobs'] }),
  });
  const save = useMutation({
    mutationFn: () => api.experiments.savePreset({
      preset_id: presetId.trim(),
      template_id: templateId!,
      label: label.trim(),
      simulator_component: simulatorComponent!,
      av_component: avComponent!,
      tags: [],
    }),
    onSuccess: (preset) => {
      setSelected(preset);
      setActiveStep(2);
      queryClient.invalidateQueries({ queryKey: ['presets'] });
    },
  });
  const cleanup = useMutation({
    mutationFn: () => api.experiments.cleanup(resources.data?.map((resource) => resource.name) ?? []),
    onSuccess: () => resources.refetch(),
  });
  const update = useMutation({
    mutationFn: () => {
      const raw = { ...managePreset!.raw };
      delete raw.id;
      delete raw.preset_id;
      const experiment = {
        ...raw,
        label: editLabel.trim() || managePreset!.id,
        tags: editTags.split(',').map((value) => value.trim()).filter(Boolean),
        simulator: { ...record(raw.simulator), component: editSimulator },
        av: { ...record(raw.av), component: editAv },
      };
      return api.experiments.updatePreset(managePreset!.id, experiment);
    },
    onSuccess: (preset) => {
      if (selected?.id === managePreset?.id) setSelected(preset);
      setManagePreset(undefined);
      queryClient.invalidateQueries({ queryKey: ['presets'] });
    },
  });
  const rename = useMutation({
    mutationFn: () => api.experiments.renamePreset(managePreset!.id, {
      new_id: renameId.trim(),
      ...(renameLabel.trim() ? { label: renameLabel.trim() } : {}),
    }),
    onSuccess: (preset) => {
      if (selected?.id === managePreset?.id) setSelected(preset);
      setManagePreset(undefined);
      queryClient.invalidateQueries({ queryKey: ['presets'] });
    },
  });
  const removePreset = useMutation({
    mutationFn: () => api.experiments.deletePreset(managePreset!.id),
    onSuccess: () => {
      if (selected?.id === managePreset?.id) { setSelected(undefined); setActiveStep(0); }
      setManagePreset(undefined);
      queryClient.invalidateQueries({ queryKey: ['presets'] });
    },
  });
  const resume = useMutation({
    mutationFn: () => api.experiments.resume(resumeJobId!, resumeAction),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['jobs'] }),
  });

  function openPresetManager(preset: ExperimentPreset, tab: 'update' | 'rename' | 'delete') {
    setManagePreset(preset);
    setManageTab(tab);
    setEditLabel(preset.name);
    setEditSimulator(preset.simulator ?? simulatorOptions[0]?.value ?? null);
    setEditAv(preset.automation ?? avOptions[0]?.value ?? null);
    const tags = Array.isArray(preset.raw.tags) ? preset.raw.tags.map(String) : [];
    setEditTags(tags.join(', '));
    setRenameId(preset.id);
    setRenameLabel(preset.name);
    setDeleteConfirmation('');
    update.reset();
    rename.reset();
    removePreset.reset();
  }

  function choosePreset(preset: ExperimentPreset) {
    setSelected(preset);
    setTemplateId(preset.id);
    setSimulatorComponent(preset.simulator ?? simulatorOptions[0]?.value ?? null);
    setAvComponent(preset.automation ?? avOptions[0]?.value ?? null);
    setActiveStep(2);
  }

  const createValid = Boolean(
    /^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(presetId.trim())
    && templateId && simulatorComponent && avComponent,
  );

  return (
    <>
      <PageHeader
        eyebrow="Experiment orchestration"
        title="Configure once. Run with confidence."
        description="Reuse validated registry presets, create explicit variants, and supervise PISA-owned runtime resources from one auditable workflow."
        actions={<Button variant="default" leftSection={<IconRefresh size={16} />} onClick={() => { presets.refetch(); resources.refetch(); }}>Refresh</Button>}
      />

      <SimpleGrid cols={{ base: 1, xl: 3 }} mb="xl">
        <Card p="lg" style={{ gridColumn: 'span 2' }}>
          <Stepper active={activeStep} onStepClick={setActiveStep} allowNextStepsSelect={false} size="sm" mb="xl">
            <Stepper.Step label="Preset" description="Registered baseline" />
            <Stepper.Step label="Variant" description="Template and components" />
            <Stepper.Step label="Action" description="Exact runner operation" />
            <Stepper.Step label="Review" description="Submit safely" />
          </Stepper>

          {activeStep === 0 && (
            presets.isLoading ? <PageLoading label="Loading presets…" /> : presets.error ? <InlineError error={presets.error} onRetry={() => presets.refetch()} /> : !presets.data?.items.length ? (
              <EmptyState title="No registry presets available" description="Add a valid experiment registry through the experiment-runner configuration before submitting work." />
            ) : (
              <>
                <SimpleGrid cols={{ base: 1, sm: 2 }}>
                  {presets.data.items.map((preset) => (
                    <Card key={preset.id} padding="md" withBorder bg={selected?.id === preset.id ? 'indigo.0' : undefined} onClick={() => choosePreset(preset)} style={{ cursor: 'pointer' }}>
                      <Group justify="space-between" mb="xs"><ThemeIcon variant="light"><IconSettings size={17} /></ThemeIcon><Group gap={4}><Badge variant="light" color="gray">{preset.sampler ?? 'configured sampler'}</Badge><Menu withinPortal position="bottom-end">
                        <Menu.Target><ActionIcon aria-label={`Manage ${preset.name}`} variant="subtle" color="gray" onClick={(event) => event.stopPropagation()}><IconDotsVertical size={16} /></ActionIcon></Menu.Target>
                        <Menu.Dropdown>
                          <Menu.Label>Preset controls</Menu.Label>
                          <Menu.Item leftSection={<IconEdit size={15} />} onClick={() => openPresetManager(preset, 'update')}>Edit configuration</Menu.Item>
                          <Menu.Item leftSection={<IconRestore size={15} />} onClick={() => openPresetManager(preset, 'rename')}>Rename</Menu.Item>
                          <Menu.Divider />
                          <Menu.Item color="red" leftSection={<IconTrash size={15} />} onClick={() => openPresetManager(preset, 'delete')}>Delete preset</Menu.Item>
                        </Menu.Dropdown>
                      </Menu></Group></Group>
                      <Text fw={600} size="sm">{preset.name}</Text>
                      <Text size="xs" c="dimmed">{preset.simulator ?? 'Simulator not set'} · {preset.automation ?? 'AV not set'}</Text>
                      <Text size="xs" c="dimmed" mt={4}>{preset.scenario ?? 'Scenario inherited from registry'}</Text>
                    </Card>
                  ))}
                </SimpleGrid>
                <Button mt="lg" variant="light" onClick={() => setActiveStep(1)}>Create a variant from a template</Button>
              </>
            )
          )}

          {activeStep === 1 && (
            <Stack>
              <Text size="sm" c="dimmed">Variants copy a complete registered experiment, then safely switch only its simulator and AV components.</Text>
              <SimpleGrid cols={{ base: 1, sm: 2 }}>
                <TextInput label="Preset ID" description="Letters, numbers, dot, underscore, or dash" value={presetId} onChange={(event) => setPresetId(event.currentTarget.value)} required />
                <TextInput label="Display label" value={label} onChange={(event) => setLabel(event.currentTarget.value)} required />
              </SimpleGrid>
              <Select label="Template preset" searchable value={templateId} onChange={(value) => {
                setTemplateId(value);
                const template = presets.data?.items.find((preset) => preset.id === value);
                if (template?.simulator) setSimulatorComponent(template.simulator);
                if (template?.automation) setAvComponent(template.automation);
              }} data={presetOptions} allowDeselect={false} required />
              <SimpleGrid cols={{ base: 1, sm: 2 }}>
                <Select label="Simulator component" searchable value={simulatorComponent} onChange={setSimulatorComponent} data={simulatorOptions} allowDeselect={false} required />
                <Select label="AV component" searchable value={avComponent} onChange={setAvComponent} data={avOptions} allowDeselect={false} required />
              </SimpleGrid>
              {save.error && <InlineError error={save.error} />}
            </Stack>
          )}

          {activeStep === 2 && (
            <Stack>
              {!selected ? <EmptyState title="Choose a registered preset" description="Return to Preset, or save the variant on the previous step." /> : (
                <>
                  <Card withBorder bg="gray.0">
                    <Text size="sm" fw={600}>{selected.name}</Text>
                    <Text size="xs" c="dimmed" mt={4}>{selected.scenario ?? 'Registered scenario'} · {selected.simulator} / {selected.automation}</Text>
                  </Card>
                  <Select label="Runner action" value={action} onChange={(value) => setAction((value ?? 'run_all') as RunnerAction)} allowDeselect={false} data={actions.map((item) => ({ value: item.value, label: item.label }))} />
                  <Text size="sm" c="dimmed">{actions.find((item) => item.value === action)?.description}</Text>
                  {action === 'run_all' && <Checkbox checked={createReport} onChange={(event) => setCreateReport(event.currentTarget.checked)} label="Build the configured report when the run completes" description="Sent as an explicit analysis.auto override for this job only." />}
                </>
              )}
            </Stack>
          )}

          {activeStep === 3 && (
            <Stack>
              <Card withBorder bg="gray.0">
                <Group justify="space-between"><Text size="sm" fw={600}>{selected?.name ?? 'No preset selected'}</Text><Badge variant="light">{actions.find((item) => item.value === action)?.label}</Badge></Group>
                <Text size="xs" c="dimmed" mt="xs">Preset ID: <Code>{selected?.id ?? '—'}</Code></Text>
                {action === 'run_all' && <Text size="xs" c="dimmed" mt={4}>Automatic report: {createReport ? 'enabled' : 'disabled'}</Text>}
              </Card>
              {run.error && <InlineError error={run.error} />}
            </Stack>
          )}

          <Divider my="xl" />
          <Group justify="space-between">
            <Button variant="default" disabled={activeStep === 0} onClick={() => setActiveStep((value) => Math.max(0, value - 1))}>Back</Button>
            <Group>
              {activeStep === 1 && <Button variant="light" leftSection={<IconDeviceFloppy size={16} />} loading={save.isPending} disabled={!createValid} onClick={() => save.mutate()}>Save variant</Button>}
              {activeStep < 3 && activeStep !== 1 && <Button disabled={activeStep === 2 && !selected} onClick={() => setActiveStep((value) => Math.min(3, value + 1))}>Continue</Button>}
              {activeStep === 3 && <Button leftSection={<IconPlayerPlay size={16} />} loading={run.isPending} disabled={!selected} onClick={() => run.mutate()}>Submit runner job</Button>}
            </Group>
          </Group>
        </Card>

        <Card p="lg">
          <Group justify="space-between" mb="md"><div><Text fw={650}>Runtime resources</Text><Text size="xs" c="dimmed">PISA-owned containers only</Text></div><ThemeIcon variant="light" color="cyan"><IconServer size={18} /></ThemeIcon></Group>
          {resources.isLoading ? <PageLoading label="Inspecting runtime…" /> : resources.error ? <InlineError error={resources.error} onRetry={() => resources.refetch()} /> : !resources.data?.length ? (
            <EmptyState title="Runtime is clean" description="No PISA-owned container resources are currently active." icon={<IconBox size={23} />} />
          ) : (
            <Stack>{resources.data.map((resource) => <Group key={resource.id} justify="space-between"><div><Text size="sm" fw={500}>{resource.name}</Text><Code fz={10}>{resource.type}</Code></div><Badge variant="light" color={resource.state?.toLowerCase().includes('up') || resource.state === 'running' ? 'teal' : 'gray'}>{resource.state ?? 'available'}</Badge></Group>)}</Stack>
          )}
          {cleanup.error && <InlineError error={cleanup.error} />}
          <Button mt="lg" fullWidth variant="light" color="red" leftSection={<IconTrash size={16} />} loading={cleanup.isPending} disabled={!resources.data?.length} onClick={() => cleanup.mutate()}>Stop listed PISA containers</Button>
          <Text size="xs" c="dimmed" mt="sm">Each container name is re-verified by the API before it is stopped; non-owned resources are refused.</Text>
          <Divider my="lg" />
          <Text fw={650}>Resume a terminal job</Text>
          <Text size="xs" c="dimmed" mt={2}>Continue only the runner stage you select; completed work is not silently repeated.</Text>
          <Stack mt="md" gap="sm">
            <Select label="Runner job" placeholder={jobs.isLoading ? 'Loading jobs…' : 'Choose a completed, failed, or cancelled job'} value={resumeJobId} onChange={setResumeJobId} searchable clearable data={terminalJobs.map((job) => ({ value: job.id, label: `${job.title} · ${job.state} · ${job.id.replace(/^runner:/, '')}` }))} />
            <Select label="Resume at" value={resumeAction} onChange={(value) => setResumeAction((value ?? 'run') as RunnerResumeAction)} allowDeselect={false} data={[{ value: 'run', label: 'Run samples' }, { value: 'stop', label: 'Stop services' }, { value: 'report', label: 'Build report' }]} />
            {resume.error && <InlineError error={resume.error} />}
            <Button variant="light" leftSection={<IconRestore size={16} />} loading={resume.isPending} disabled={!resumeJobId} onClick={() => resume.mutate()}>Resume selected stage</Button>
          </Stack>
        </Card>
      </SimpleGrid>

      <Modal opened={Boolean(managePreset)} onClose={() => setManagePreset(undefined)} title={managePreset ? `Manage ${managePreset.name}` : 'Manage preset'} size="lg">
        <Tabs value={manageTab} onChange={setManageTab} keepMounted={false}>
          <Tabs.List grow><Tabs.Tab value="update">Update</Tabs.Tab><Tabs.Tab value="rename">Rename</Tabs.Tab><Tabs.Tab value="delete" color="red">Delete</Tabs.Tab></Tabs.List>
          <Tabs.Panel value="update" pt="lg"><Stack>
            <Text size="sm" c="dimmed">The scenario, task, sampler, analysis, and other registered fields are preserved; only the fields below are changed.</Text>
            <TextInput label="Display label" value={editLabel} onChange={(event) => setEditLabel(event.currentTarget.value)} required />
            <SimpleGrid cols={{ base: 1, sm: 2 }}>
              <Select label="Simulator component" searchable value={editSimulator} onChange={setEditSimulator} data={simulatorOptions} allowDeselect={false} required />
              <Select label="AV component" searchable value={editAv} onChange={setEditAv} data={avOptions} allowDeselect={false} required />
            </SimpleGrid>
            <TextInput label="Tags" description="Comma-separated; duplicates are normalized by the registry." value={editTags} onChange={(event) => setEditTags(event.currentTarget.value)} />
            {update.error && <InlineError error={update.error} />}
            <Group justify="flex-end"><Button variant="default" onClick={() => setManagePreset(undefined)}>Cancel</Button><Button loading={update.isPending} disabled={!editLabel.trim() || !editSimulator || !editAv} onClick={() => update.mutate()}>Save changes</Button></Group>
          </Stack></Tabs.Panel>
          <Tabs.Panel value="rename" pt="lg"><Stack>
            <Alert color="blue">References to this preset ID are not guessed or rewritten outside the experiment registry.</Alert>
            <TextInput label="New preset ID" description="Letters, numbers, dot, underscore, or dash" value={renameId} onChange={(event) => setRenameId(event.currentTarget.value)} required />
            <TextInput label="Display label" description="Optional; leave empty to preserve the current label." value={renameLabel} onChange={(event) => setRenameLabel(event.currentTarget.value)} />
            {rename.error && <InlineError error={rename.error} />}
            <Group justify="flex-end"><Button variant="default" onClick={() => setManagePreset(undefined)}>Cancel</Button><Button loading={rename.isPending} disabled={!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(renameId.trim())} onClick={() => rename.mutate()}>Rename preset</Button></Group>
          </Stack></Tabs.Panel>
          <Tabs.Panel value="delete" pt="lg"><Stack>
            <Alert color="red" title="Permanent registry change">This removes the preset definition. Existing experiment output and reports are not deleted.</Alert>
            <Text size="sm">Type <Code>{managePreset?.id}</Code> to confirm:</Text>
            <TextInput value={deleteConfirmation} onChange={(event) => setDeleteConfirmation(event.currentTarget.value)} />
            {removePreset.error && <InlineError error={removePreset.error} />}
            <Group justify="flex-end"><Button variant="default" onClick={() => setManagePreset(undefined)}>Cancel</Button><Button color="red" loading={removePreset.isPending} disabled={deleteConfirmation !== managePreset?.id} onClick={() => removePreset.mutate()}>Delete preset</Button></Group>
          </Stack></Tabs.Panel>
        </Tabs>
      </Modal>
    </>
  );
}
