import { useState } from 'react';
import {
  Accordion, Alert, Badge, Button, Card, Checkbox, Code, Group, Modal, NumberInput,
  Radio, ScrollArea, Select, SimpleGrid, Stack, Tabs, Text, TextInput, ThemeIcon,
} from '@mantine/core';
import { useDisclosure } from '@mantine/hooks';
import {
  IconArrowsDiff, IconBinaryTree, IconCheck, IconRoute, IconShieldExclamation, IconTool,
} from '@tabler/icons-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import type { RepairPlan } from '../api/types';
import { InlineError } from '../components/Feedback';
import { PageHeader } from '../components/PageHeader';

function queueSuccess(queryClient: ReturnType<typeof useQueryClient>) {
  return () => queryClient.invalidateQueries({ queryKey: ['jobs'] });
}

function commaSeparatedIds(value: string) {
  return value.split(',').map((item) => item.trim()).filter(Boolean);
}

function optionalRange(min: number | string, max: number | string): [number, number] | undefined {
  if (min === '' || max === '') return undefined;
  const values: [number, number] = [Number(min), Number(max)];
  return values.every(Number.isFinite) ? values : undefined;
}

function invalidRange(min: number | string, max: number | string) {
  if (min === '' && max === '') return false;
  if (min === '' || max === '') return true;
  return !Number.isFinite(Number(min)) || !Number.isFinite(Number(max)) || Number(min) >= Number(max);
}

function outside(value: number | string, min: number, max: number) {
  const number = Number(value);
  return value === '' || !Number.isFinite(number) || number < min || number > max;
}

export function AdvancedToolsPage() {
  const queryClient = useQueryClient();
  const [trajectorySource, setTrajectorySource] = useState('');
  const [trajectoryOutput, setTrajectoryOutput] = useState('./trajectory');
  const [trajectoryWidth, setTrajectoryWidth] = useState<number | string>(1100);
  const [trajectoryHeight, setTrajectoryHeight] = useState<number | string>(760);
  const [trajectoryXMin, setTrajectoryXMin] = useState<number | string>('');
  const [trajectoryXMax, setTrajectoryXMax] = useState<number | string>('');
  const [trajectoryYMin, setTrajectoryYMin] = useState<number | string>('');
  const [trajectoryYMax, setTrajectoryYMax] = useState<number | string>('');
  const [trajectoryEqualScale, setTrajectoryEqualScale] = useState(true);
  const [trajectoryIgnoreIds, setTrajectoryIgnoreIds] = useState('');
  const [trajectoryOriginId, setTrajectoryOriginId] = useState('');
  const [trajectoryOverwrite, setTrajectoryOverwrite] = useState(false);
  const [leftSource, setLeftSource] = useState('');
  const [rightSource, setRightSource] = useState('');
  const [comparisonOutput, setComparisonOutput] = useState('./trajectory-comparison');
  const [leftLabel, setLeftLabel] = useState('');
  const [rightLabel, setRightLabel] = useState('');
  const [comparisonWidth, setComparisonWidth] = useState<number | string>(1200);
  const [comparisonHeight, setComparisonHeight] = useState<number | string>(820);
  const [comparisonEqualScale, setComparisonEqualScale] = useState(true);
  const [comparisonIgnoreIds, setComparisonIgnoreIds] = useState('');
  const [comparisonOverwrite, setComparisonOverwrite] = useState(false);
  const [outcomeSource, setOutcomeSource] = useState('');
  const [conditionPath, setConditionPath] = useState('');
  const [outcomeOutput, setOutcomeOutput] = useState('./outcome-evaluation');
  const [outcomeMode, setOutcomeMode] = useState<'overlay' | 'replace'>('replace');
  const [defaultOutcome, setDefaultOutcome] = useState<'success' | 'fail' | 'invalid' | 'unknown'>('unknown');
  const [outcomeOverwrite, setOutcomeOverwrite] = useState(false);
  const [writeMonitorOutcome, setWriteMonitorOutcome] = useState(false);

  const [repairSource, setRepairSource] = useState('');
  const [repairReferenceType, setRepairReferenceType] = useState<'init_state_path' | 'reference_root'>('init_state_path');
  const [repairReference, setRepairReference] = useState('');
  const [repairOutput, setRepairOutput] = useState('./agent-state-overlay');
  const [repairPlan, setRepairPlan] = useState<RepairPlan>();
  const [repairMode, setRepairMode] = useState<'overlay' | 'source'>('overlay');
  const [confirmation, setConfirmation] = useState('');
  const [confirmationAction, setConfirmationAction] = useState<'apply' | 'restore'>('apply');
  const [confirmOpened, confirmControls] = useDisclosure(false);

  const invalidatePlan = () => setRepairPlan(undefined);
  const trajectory = useMutation({
    mutationFn: () => api.tools.trajectory({
      input_path: trajectorySource.trim(),
      output_dir: trajectoryOutput.trim(),
      width: Number(trajectoryWidth),
      height: Number(trajectoryHeight),
      ...(optionalRange(trajectoryXMin, trajectoryXMax) ? { x_range: optionalRange(trajectoryXMin, trajectoryXMax) } : {}),
      ...(optionalRange(trajectoryYMin, trajectoryYMax) ? { y_range: optionalRange(trajectoryYMin, trajectoryYMax) } : {}),
      equal_scale: trajectoryEqualScale,
      ignore_agent_ids: commaSeparatedIds(trajectoryIgnoreIds),
      ...(trajectoryOriginId.trim() ? { origin_agent_id: trajectoryOriginId.trim() } : {}),
      overwrite: trajectoryOverwrite,
    }),
    onSuccess: queueSuccess(queryClient),
  });
  const compare = useMutation({
    mutationFn: () => api.tools.compareTrajectory({
      left_path: leftSource.trim(),
      right_path: rightSource.trim(),
      output_dir: comparisonOutput.trim(),
      ...(leftLabel.trim() ? { left_label: leftLabel.trim() } : {}),
      ...(rightLabel.trim() ? { right_label: rightLabel.trim() } : {}),
      width: Number(comparisonWidth),
      height: Number(comparisonHeight),
      equal_scale: comparisonEqualScale,
      ignore_agent_ids: commaSeparatedIds(comparisonIgnoreIds),
      overwrite: comparisonOverwrite,
    }),
    onSuccess: queueSuccess(queryClient),
  });
  const outcome = useMutation({
    mutationFn: () => api.tools.evaluateOutcome({
      input_path: outcomeSource.trim(),
      config_path: conditionPath.trim(),
      output_dir: outcomeOutput.trim(),
      mode: outcomeMode,
      default_outcome: defaultOutcome,
      overwrite: outcomeOverwrite,
      write_monitor_outcome: writeMonitorOutcome,
    }),
    onSuccess: queueSuccess(queryClient),
  });
  const scan = useMutation({
    mutationFn: () => api.tools.scanRepair({
      source_path: repairSource.trim(),
      [repairReferenceType]: repairReference.trim(),
      mode: repairMode,
      ...(repairMode === 'overlay' ? { output_path: repairOutput.trim() } : {}),
    }),
    onSuccess: setRepairPlan,
  });
  const apply = useMutation({
    mutationFn: () => api.tools.applyRepair({
      plan: repairPlan!,
      ...(repairPlan?.mode === 'source' ? { confirm_path: confirmation } : {}),
    }),
    onSuccess: () => { confirmControls.close(); queryClient.invalidateQueries({ queryKey: ['jobs'] }); },
  });
  const restore = useMutation({
    mutationFn: () => api.tools.restoreRepair({
      source_path: repairPlan!.source_path,
      confirm_path: confirmation,
      backup_suffix: repairPlan!.backup_suffix,
    }),
    onSuccess: () => { confirmControls.close(); queryClient.invalidateQueries({ queryKey: ['jobs'] }); },
  });

  const expectedConfirmation = repairPlan?.source_path ?? '';
  const scanValid = Boolean(
    repairSource.trim() && repairReference.trim()
    && (repairMode === 'source' || repairOutput.trim()),
  );
  const blockingFindings = repairPlan?.findings.filter((finding) => finding.severity === 'error').length ?? 0;
  const trajectoryRangesInvalid = invalidRange(trajectoryXMin, trajectoryXMax) || invalidRange(trajectoryYMin, trajectoryYMax);
  const trajectorySizeInvalid = outside(trajectoryWidth, 320, 8000) || outside(trajectoryHeight, 240, 8000);
  const comparisonSizeInvalid = outside(comparisonWidth, 320, 8000) || outside(comparisonHeight, 240, 8000);

  function openConfirmation(action: 'apply' | 'restore') {
    setConfirmationAction(action);
    setConfirmation('');
    confirmControls.open();
  }

  return (
    <>
      <PageHeader
        eyebrow="Advanced tools"
        title="Precise tools, explicit consequences"
        description="Standalone renderers, condition evaluation, and source repair remain available with the same safety contracts as their command-line counterparts."
      />
      <Alert color="yellow" icon={<IconShieldExclamation size={18} />} title="Advanced workspace">These actions expose low-level paths and compatibility controls. Source-writing repairs use a signed dry-run plan, exact path confirmation, and verified backups.</Alert>
      <Tabs defaultValue="trajectory" mt="lg" keepMounted={false}>
        <Tabs.List mb="lg"><Tabs.Tab value="trajectory" leftSection={<IconRoute size={16} />}>Trajectory</Tabs.Tab><Tabs.Tab value="outcome" leftSection={<IconBinaryTree size={16} />}>Outcome evaluator</Tabs.Tab><Tabs.Tab value="repair" leftSection={<IconTool size={16} />}>Agent-state repair</Tabs.Tab></Tabs.List>

        <Tabs.Panel value="trajectory">
          <SimpleGrid cols={{ base: 1, md: 2 }}>
            <Card p="lg">
              <Stack>
                <div><Text fw={650}>Render trajectory</Text><Text size="sm" c="dimmed">Create synchronized SVG views from a manifest, run directory, or monitor CSV.</Text></div>
                <TextInput label="Input path" value={trajectorySource} onChange={(event) => setTrajectorySource(event.currentTarget.value)} placeholder="/path/to/run-or-manifest" required />
                <TextInput label="Output directory" value={trajectoryOutput} onChange={(event) => setTrajectoryOutput(event.currentTarget.value)} required />
                <Accordion variant="contained"><Accordion.Item value="trajectory-options"><Accordion.Control>Rendering options</Accordion.Control><Accordion.Panel><Stack>
                  <SimpleGrid cols={2}>
                    <NumberInput label="Width (px)" value={trajectoryWidth} onChange={setTrajectoryWidth} min={320} max={8000} error={outside(trajectoryWidth, 320, 8000) ? '320–8,000 px' : undefined} />
                    <NumberInput label="Height (px)" value={trajectoryHeight} onChange={setTrajectoryHeight} min={240} max={8000} error={outside(trajectoryHeight, 240, 8000) ? '240–8,000 px' : undefined} />
                  </SimpleGrid>
                  <SimpleGrid cols={2}>
                    <NumberInput label="X minimum" value={trajectoryXMin} onChange={setTrajectoryXMin} placeholder="Auto" />
                    <NumberInput label="X maximum" value={trajectoryXMax} onChange={setTrajectoryXMax} placeholder="Auto" />
                    <NumberInput label="Y minimum" value={trajectoryYMin} onChange={setTrajectoryYMin} placeholder="Auto" />
                    <NumberInput label="Y maximum" value={trajectoryYMax} onChange={setTrajectoryYMax} placeholder="Auto" />
                  </SimpleGrid>
                  {trajectoryRangesInvalid && <Alert color="red">A fixed axis range requires both bounds, with minimum smaller than maximum.</Alert>}
                  <TextInput label="Ignore agent IDs" description="Comma-separated IDs omitted from every view." value={trajectoryIgnoreIds} onChange={(event) => setTrajectoryIgnoreIds(event.currentTarget.value)} />
                  <TextInput label="Origin agent ID" description="Optional agent used as the coordinate origin." value={trajectoryOriginId} onChange={(event) => setTrajectoryOriginId(event.currentTarget.value)} />
                  <Checkbox label="Use equal X/Y scale" checked={trajectoryEqualScale} onChange={(event) => setTrajectoryEqualScale(event.currentTarget.checked)} />
                  <Checkbox label="Replace an existing PISA-owned output" checked={trajectoryOverwrite} onChange={(event) => setTrajectoryOverwrite(event.currentTarget.checked)} />
                </Stack></Accordion.Panel></Accordion.Item></Accordion>
                {trajectory.error && <InlineError error={trajectory.error} />}
                <Button loading={trajectory.isPending} disabled={!trajectorySource.trim() || !trajectoryOutput.trim() || trajectoryRangesInvalid || trajectorySizeInvalid} leftSection={<IconRoute size={16} />} onClick={() => trajectory.mutate()}>Render trajectory</Button>
              </Stack>
            </Card>
            <Card p="lg">
              <Stack>
                <div><Text fw={650}>Compare trajectory sets</Text><Text size="sm" c="dimmed">Pair by canonical parameters and align on simulation time without extrapolation.</Text></div>
                <TextInput label="Left input" value={leftSource} onChange={(event) => setLeftSource(event.currentTarget.value)} required />
                <TextInput label="Right input" value={rightSource} onChange={(event) => setRightSource(event.currentTarget.value)} required />
                <TextInput label="Comparison output directory" value={comparisonOutput} onChange={(event) => setComparisonOutput(event.currentTarget.value)} required />
                <Accordion variant="contained"><Accordion.Item value="comparison-options"><Accordion.Control>Pairing and rendering options</Accordion.Control><Accordion.Panel><Stack>
                  <SimpleGrid cols={2}>
                    <TextInput label="Left label" value={leftLabel} onChange={(event) => setLeftLabel(event.currentTarget.value)} placeholder="Derived from source" />
                    <TextInput label="Right label" value={rightLabel} onChange={(event) => setRightLabel(event.currentTarget.value)} placeholder="Derived from source" />
                    <NumberInput label="Width (px)" value={comparisonWidth} onChange={setComparisonWidth} min={320} max={8000} error={outside(comparisonWidth, 320, 8000) ? '320–8,000 px' : undefined} />
                    <NumberInput label="Height (px)" value={comparisonHeight} onChange={setComparisonHeight} min={240} max={8000} error={outside(comparisonHeight, 240, 8000) ? '240–8,000 px' : undefined} />
                  </SimpleGrid>
                  <TextInput label="Ignore agent IDs" description="Comma-separated IDs omitted from both sets." value={comparisonIgnoreIds} onChange={(event) => setComparisonIgnoreIds(event.currentTarget.value)} />
                  <Checkbox label="Use equal X/Y scale" checked={comparisonEqualScale} onChange={(event) => setComparisonEqualScale(event.currentTarget.checked)} />
                  <Checkbox label="Replace an existing PISA-owned comparison" checked={comparisonOverwrite} onChange={(event) => setComparisonOverwrite(event.currentTarget.checked)} />
                </Stack></Accordion.Panel></Accordion.Item></Accordion>
                <Alert color="blue" icon={<IconArrowsDiff size={17} />}>Ego identity is derived from the manifest or <Code>is_ego</Code>; fallback ID is 0.</Alert>
                {compare.error && <InlineError error={compare.error} />}
                <Button loading={compare.isPending} disabled={!leftSource.trim() || !rightSource.trim() || !comparisonOutput.trim() || comparisonSizeInvalid} leftSection={<IconArrowsDiff size={16} />} onClick={() => compare.mutate()}>Compare trajectories</Button>
              </Stack>
            </Card>
          </SimpleGrid>
        </Tabs.Panel>

        <Tabs.Panel value="outcome">
          <SimpleGrid cols={{ base: 1, md: 2 }}>
            <Card p="lg">
              <Stack>
                <div><Text fw={650}>Evaluate outcome condition tree</Text><Text size="sm" c="dimmed">Apply the full frame/result/agent expression language to existing monitor data.</Text></div>
                <TextInput label="Input results" value={outcomeSource} onChange={(event) => setOutcomeSource(event.currentTarget.value)} placeholder="/path/to/experiment" required />
                <TextInput label="Condition YAML" value={conditionPath} onChange={(event) => setConditionPath(event.currentTarget.value)} placeholder="examples/outcome_eval/condition.yaml" required />
                <TextInput label="Output directory" value={outcomeOutput} onChange={(event) => setOutcomeOutput(event.currentTarget.value)} required />
                <Select label="Evaluation mode" value={outcomeMode} onChange={(value) => setOutcomeMode((value ?? 'replace') as 'overlay' | 'replace')} allowDeselect={false} data={[{ value: 'replace', label: 'Replace generated outcome field' }, { value: 'overlay', label: 'Overlay existing outcome' }]} />
                <Accordion variant="contained"><Accordion.Item value="outcome-options"><Accordion.Control>Outcome and output options</Accordion.Control><Accordion.Panel><Stack>
                  <Select label="Default outcome" description="Used when no condition resolves to a terminal class." value={defaultOutcome} onChange={(value) => setDefaultOutcome((value ?? 'unknown') as typeof defaultOutcome)} allowDeselect={false} data={[
                    { value: 'unknown', label: 'Unknown · recommended' }, { value: 'success', label: 'Success' },
                    { value: 'fail', label: 'Fail' }, { value: 'invalid', label: 'Invalid' },
                  ]} />
                  <Checkbox checked={writeMonitorOutcome} onChange={(event) => setWriteMonitorOutcome(event.currentTarget.checked)} label="Include monitor_outcome in the generated bundle" description="The input monitor data remains read-only; all writes go to the output directory." />
                  <Checkbox checked={outcomeOverwrite} onChange={(event) => setOutcomeOverwrite(event.currentTarget.checked)} label="Replace an existing PISA-owned evaluation" />
                </Stack></Accordion.Panel></Accordion.Item></Accordion>
                {outcome.error && <InlineError error={outcome.error} />}
                <Button loading={outcome.isPending} disabled={!outcomeSource.trim() || !conditionPath.trim() || !outcomeOutput.trim()} leftSection={<IconBinaryTree size={16} />} onClick={() => outcome.mutate()}>Evaluate outcomes</Button>
              </Stack>
            </Card>
            <Card p="lg"><Text fw={650}>Evaluation contract</Text><Stack mt="md">{['Frame, result, and agent-pair predicates', 'Nested all / any / not expressions', 'Explicit invalid and unknown handling', 'Separate owned output directory', 'CSV, Markdown, and LaTeX results'].map((item) => <Group key={item} wrap="nowrap"><ThemeIcon variant="light" color="teal" size={25} radius="xl"><IconCheck size={14} /></ThemeIcon><Text size="sm">{item}</Text></Group>)}</Stack></Card>
          </SimpleGrid>
        </Tabs.Panel>

        <Tabs.Panel value="repair">
          <SimpleGrid cols={{ base: 1, lg: 5 }}>
            <Card p="lg" style={{ gridColumn: 'span 2' }}>
              <Stack>
                <div><Text fw={650}>Build a signed repair plan</Text><Text size="sm" c="dimmed">Scan missing initial-state rows using exactly one trusted source.</Text></div>
                <TextInput label="Experiment source directory" value={repairSource} onChange={(event) => { setRepairSource(event.currentTarget.value); invalidatePlan(); }} placeholder="/path/to/experiment-output" required />
                <Select label="Initial-state source" value={repairReferenceType} onChange={(value) => { setRepairReferenceType((value ?? 'init_state_path') as typeof repairReferenceType); invalidatePlan(); }} allowDeselect={false} data={[{ value: 'init_state_path', label: 'Initial-state YAML' }, { value: 'reference_root', label: 'Reference experiment root' }]} />
                <TextInput label={repairReferenceType === 'init_state_path' ? 'Initial-state YAML' : 'Reference experiment root'} value={repairReference} onChange={(event) => { setRepairReference(event.currentTarget.value); invalidatePlan(); }} required />
                <Radio.Group value={repairMode} onChange={(value) => { setRepairMode(value as 'overlay' | 'source'); invalidatePlan(); }} label="Plan mode">
                  <Stack mt="xs"><Radio value="overlay" label="Create a non-destructive overlay (recommended)" /><Radio value="source" label="Write to source after backup and exact confirmation" color="red" /></Stack>
                </Radio.Group>
                {repairMode === 'overlay' && <TextInput label="Overlay output directory" value={repairOutput} onChange={(event) => { setRepairOutput(event.currentTarget.value); invalidatePlan(); }} required />}
                {scan.error && <InlineError error={scan.error} />}
                <Button variant="light" loading={scan.isPending} disabled={!scanValid} onClick={() => scan.mutate()}>Scan and build dry-run diff</Button>
                {repairPlan && (
                  <>
                    {blockingFindings > 0 && <Alert color="red" title="Plan is blocked">Resolve {blockingFindings} error finding{blockingFindings === 1 ? '' : 's'} and scan again.</Alert>}
                    <Button color={repairPlan.mode === 'source' ? 'red' : 'indigo'} disabled={blockingFindings > 0 || !repairPlan.changes.length} onClick={repairPlan.mode === 'source' ? () => openConfirmation('apply') : () => apply.mutate()} loading={apply.isPending}>Apply {repairPlan.changes.length} planned file changes</Button>
                    {repairPlan.mode === 'source' && <Button variant="subtle" color="orange" onClick={() => openConfirmation('restore')}>Restore verified .bak files</Button>}
                  </>
                )}
              </Stack>
            </Card>
            <Card p="lg" style={{ gridColumn: 'span 3' }}>
              {!repairPlan ? (
                <Stack align="center" justify="center" mih={260}><ThemeIcon size={46} radius="xl" variant="light" color="gray"><IconArrowsDiff size={23} /></ThemeIcon><Text fw={600}>Dry-run diff appears here</Text><Text c="dimmed" size="sm" ta="center">Nothing is written during the scan.</Text></Stack>
              ) : (
                <>
                  <Group justify="space-between" mb="md"><div><Text fw={650}>Signed repair plan</Text><Text size="xs" c="dimmed" className="pisa-code">{repairPlan.signature.slice(0, 20)}…</Text></div><Group gap="xs"><Badge variant="light" color={repairPlan.destructive ? 'red' : 'teal'}>{repairPlan.mode}</Badge><Badge variant="light" color="yellow">{repairPlan.changes.length} files</Badge></Group></Group>
                  <ScrollArea h={420}><table className="pisa-data-table"><thead><tr><th>Path</th><th>Rows</th><th>Insert</th><th>Time shift</th><th>Backup</th></tr></thead><tbody>{repairPlan.changes.map((change, index) => <tr key={`${change.path}-${index}`}><td className="pisa-code">{change.path}</td><td>{change.original_rows ?? '—'} → {change.result_rows ?? '—'}</td><td>{change.inserted_rows ?? '—'}</td><td>{change.time_shift_ms !== undefined ? `${change.time_shift_ms} ms` : 'inferred'}</td><td>{change.backup_exists ? 'Existing .bak' : repairPlan.mode === 'source' ? 'Created on apply' : 'Not needed'}</td></tr>)}</tbody></table></ScrollArea>
                  {repairPlan.findings.length > 0 && <Alert mt="md" color={blockingFindings ? 'red' : 'yellow'}>{repairPlan.findings.length} scan finding{repairPlan.findings.length === 1 ? '' : 's'} recorded; blocking errors must be resolved before apply.</Alert>}
                </>
              )}
            </Card>
          </SimpleGrid>
        </Tabs.Panel>
      </Tabs>

      <Modal opened={confirmOpened} onClose={confirmControls.close} title={confirmationAction === 'apply' ? 'Confirm source repair' : 'Confirm backup restore'} size="lg">
        <Stack>
          <Alert color="red" icon={<IconShieldExclamation size={18} />} title="This writes to the source directory">{confirmationAction === 'apply' ? 'Verified .bak files are created before the signed plan is applied.' : 'Existing verified .bak files replace their corresponding agent-state CSV files.'}</Alert>
          <Text size="sm">Type the complete resolved source path to confirm:</Text>
          <Code>{expectedConfirmation}</Code>
          <TextInput value={confirmation} onChange={(event) => setConfirmation(event.currentTarget.value)} placeholder={expectedConfirmation} />
          {(confirmationAction === 'apply' ? apply.error : restore.error) && <InlineError error={confirmationAction === 'apply' ? apply.error : restore.error} />}
          <Group justify="flex-end"><Button variant="default" onClick={confirmControls.close}>Cancel</Button><Button color="red" loading={confirmationAction === 'apply' ? apply.isPending : restore.isPending} disabled={confirmation !== expectedConfirmation} onClick={() => confirmationAction === 'apply' ? apply.mutate() : restore.mutate()}>{confirmationAction === 'apply' ? 'Back up and repair source' : 'Restore backups'}</Button></Group>
        </Stack>
      </Modal>
    </>
  );
}
