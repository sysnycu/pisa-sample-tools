import { useEffect, useMemo, useState } from 'react';
import { Accordion, ActionIcon, Alert, Badge, Button, Card, Checkbox, Code, Divider, Group, Modal, Progress, ScrollArea, Select, SimpleGrid, Stack, Tabs, Text, TextInput, ThemeIcon, Tooltip } from '@mantine/core';
import { useDisclosure } from '@mantine/hooks';
import { IconArrowLeft, IconArrowRight, IconChartDots, IconDatabase, IconEdit, IconEye, IconFolder, IconFolderPlus, IconFolderSearch, IconHeartbeat, IconHistory, IconPlus, IconRefresh, IconSearch, IconTrash } from '@tabler/icons-react';
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { useLocation, useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import type { DatasetDescriptor, ExperimentPreview, Job, ReportBuildRequest, ReportPreviewBuildRequest } from '../api/types';
import { EmptyState, InlineError, PageLoading } from '../components/Feedback';
import { MetricCard } from '../components/MetricCard';
import { PageHeader } from '../components/PageHeader';
import { StatusBadge } from '../components/StatusBadge';

const DEFAULT_RECORD_PATH = '/opt/sbsvf/outputs';
const ANALYSIS_ROOT = '/home/hcis-s05/ysws/PISA/pisa-sample-tools/analysis';

function JobProgress({ job }: { job: Job }) {
  const percent = job.progress?.total ? Math.min(100, 100 * job.progress.current / job.progress.total) : undefined;
  return <Alert color={job.state === 'failed' ? 'red' : job.state === 'succeeded' ? 'teal' : 'blue'} title={job.state === 'succeeded' ? 'Report is ready' : job.state === 'failed' ? 'Build failed' : `Build progress · ${job.phase ?? job.state}`}>
    <Stack gap="xs"><Progress value={percent ?? 100} animated={percent === undefined && ['queued', 'running'].includes(job.state)} /><Group justify="space-between"><Text size="xs">{job.message ?? job.phase ?? job.state}</Text><Text size="xs" c="dimmed">{job.progress ? `${job.progress.current} / ${job.progress.total ?? '?'} ${job.progress.unit ?? ''}` : 'Preparing…'}</Text></Group>{job.artifacts?.map((artifact) => <Text key={artifact.url} component="a" href={artifact.url} target="_blank" size="xs" c="teal" td="underline">Open {artifact.name}</Text>)}</Stack>
  </Alert>;
}

export function DashboardPage() {
  const [search, setSearch] = useState('');
  const [libraryPath, setLibraryPath] = useState('');
  const [buildBrowsePath, setBuildBrowsePath] = useState(DEFAULT_RECORD_PATH);
  const [newFolderName, setNewFolderName] = useState('');
  const [showNewFolder, setShowNewFolder] = useState(false);
  const [reportOpened, reportControls] = useDisclosure(false);
  const [buildStep, setBuildStep] = useState<'experiments' | 'destination'>('experiments');
  const [sourceKind, setSourceKind] = useState<'results' | 'campaign'>('results');
  const [experiments, setExperiments] = useState<ExperimentPreview[]>([]);
  const [candidate, setCandidate] = useState<ExperimentPreview>();
  const [campaignPath, setCampaignPath] = useState('');
  const [outputParent, setOutputParent] = useState(ANALYSIS_ROOT);
  const [destinationBrowsePath, setDestinationBrowsePath] = useState(ANALYSIS_ROOT);
  const [destinationFolderName, setDestinationFolderName] = useState('');
  const [showDestinationFolder, setShowDestinationFolder] = useState(false);
  const [reportName, setReportName] = useState('pisa-report');
  const [activeBuildJob, setActiveBuildJob] = useState<Job>();
  const [activeRebuildJob, setActiveRebuildJob] = useState<Job>();
  const [specPath, setSpecPath] = useState('');
  const [validationMode, setValidationMode] = useState<'default' | 'strict' | 'permissive'>('default');
  const [deepValidation, setDeepValidation] = useState(false);
  const [reportMode, setReportMode] = useState<'interactive' | 'static'>('interactive');
  const [engine, setEngine] = useState<NonNullable<ReportBuildRequest['engine']>>('auto');
  const [overwrite, setOverwrite] = useState(false);
  const [sensitivityMode, setSensitivityMode] = useState<'default' | 'enabled' | 'disabled'>('default');
  const [rebuildTarget, setRebuildTarget] = useState<DatasetDescriptor>();
  const [rebuildOutput, setRebuildOutput] = useState('');
  const [rebuildSensitivity, setRebuildSensitivity] = useState<'default' | 'enabled' | 'disabled'>('default');
  const [rebuildOverwrite, setRebuildOverwrite] = useState(false);
  const [manageTarget, setManageTarget] = useState<DatasetDescriptor>();
  const [manageTab, setManageTab] = useState<string | null>('preview');
  const [renameName, setRenameName] = useState('');
  const [deleteConfirmation, setDeleteConfirmation] = useState('');
  const browser = useQuery({ queryKey: ['report-browser', libraryPath], queryFn: () => api.datasets.browse(libraryPath || undefined), retry: 1 });
  const buildBrowser = useQuery({ queryKey: ['report-build-browser', buildBrowsePath], queryFn: () => api.datasets.browse(buildBrowsePath || undefined), enabled: reportOpened && buildStep === 'experiments', retry: 1 });
  const destinationBrowser = useQuery({ queryKey: ['report-destination-browser', destinationBrowsePath], queryFn: () => api.datasets.browse(destinationBrowsePath || ANALYSIS_ROOT), enabled: reportOpened && buildStep === 'destination', retry: 1 });
  const reportPaths = useMemo(() => {
    const values = [browser.data?.current_report?.path, ...(browser.data?.entries.filter((entry) => entry.is_report).map((entry) => entry.path) ?? [])];
    return [...new Set(values.filter((value): value is string => Boolean(value)))];
  }, [browser.data]);
  const reportQueries = useQueries({ queries: reportPaths.map((path) => ({ queryKey: ['report-preview', path], queryFn: () => api.datasets.previewReport(path), staleTime: 300_000, retry: 1 })) });
  const sourceInspection = useMutation({
    mutationFn: api.datasets.previewExperiment,
    onSuccess: (data) => {
      setCandidate(data);
      if (!experiments.length && data.suggested_report_name) setReportName(data.suggested_report_name);
      setSourceKind('results');
      validation.reset();
    },
  });
  const createDirectory = useMutation({
    mutationFn: () => api.datasets.createDirectory(buildBrowsePath || buildBrowser.data?.path || DEFAULT_RECORD_PATH, newFolderName.trim()),
    onSuccess: (data) => {
      setBuildBrowsePath(data.path);
      setNewFolderName('');
      setShowNewFolder(false);
      void queryClient.invalidateQueries({ queryKey: ['report-build-browser'] });
      void queryClient.invalidateQueries({ queryKey: ['report-browser'] });
    },
  });
  const createDestinationDirectory = useMutation({
    mutationFn: () => api.datasets.createDirectory(destinationBrowsePath || ANALYSIS_ROOT, destinationFolderName.trim()),
    onSuccess: (data) => {
      setDestinationBrowsePath(data.path);
      setOutputParent(data.path);
      setDestinationFolderName('');
      setShowDestinationFolder(false);
      void queryClient.invalidateQueries({ queryKey: ['report-destination-browser'] });
    },
  });
  const experimentDescriptors = useMemo(() => experiments.map((item) => ({ id: item.dataset_id, results: item.results })), [experiments]);
  const compatibility = useQuery({ queryKey: ['report-compatibility', experimentDescriptors], queryFn: () => api.datasets.compatibility(experimentDescriptors), enabled: experimentDescriptors.length > 1, retry: 1 });
  const reportDetails = useQuery({ queryKey: ['report-details', manageTarget?.id], queryFn: () => api.datasets.details(manageTarget!.id), enabled: Boolean(manageTarget), retry: 1 });
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const build = useMutation({
    mutationFn: api.datasets.build,
    onSuccess: (job) => {
      setActiveBuildJob(job);
      queryClient.invalidateQueries({ queryKey: ['datasets'] });
      queryClient.invalidateQueries({ queryKey: ['jobs'] });
    },
  });
  const previewBuild = useMutation({
    mutationFn: api.datasets.previewBuild,
    onSuccess: (job) => {
      setActiveBuildJob(job);
      queryClient.invalidateQueries({ queryKey: ['jobs'] });
    },
  });
  const validation = useMutation({ mutationFn: api.datasets.validate });
  const rebuild = useMutation({
    mutationFn: () => api.datasets.rebuild(rebuildTarget!.id, {
      ...(rebuildOutput.trim() ? { output_dir: rebuildOutput.trim() } : {}),
      ...(rebuildSensitivity !== 'default' ? { sensitivity: rebuildSensitivity === 'enabled' } : {}),
      overwrite: rebuildOverwrite,
    }),
    onSuccess: (job) => {
      setActiveRebuildJob(job);
      queryClient.invalidateQueries({ queryKey: ['datasets'] });
      queryClient.invalidateQueries({ queryKey: ['jobs'] });
    },
  });
  const renameReport = useMutation({
    mutationFn: () => api.datasets.rename(manageTarget!.id, renameName.trim()),
    onSuccess: () => { setManageTarget(undefined); queryClient.invalidateQueries({ queryKey: ['datasets'] }); queryClient.invalidateQueries({ queryKey: ['report-browser'] }); },
  });
  const deleteReport = useMutation({
    mutationFn: () => api.datasets.delete(manageTarget!.id, deleteConfirmation),
    onSuccess: () => { setManageTarget(undefined); queryClient.invalidateQueries({ queryKey: ['datasets'] }); queryClient.invalidateQueries({ queryKey: ['report-browser'] }); },
  });
  const outputDir = `${outputParent.replace(/[\\/]+$/, '')}/${reportName.trim()}`;
  const parsedResults = experiments.map((item) => item.results);
  const analysisValid = Boolean(sourceKind === 'results' ? parsedResults.length && compatibility.data?.compatible !== false : campaignPath.trim());
  const reportNameValid = Boolean(reportName.trim() && !['.', '..'].includes(reportName.trim()) && !/[\\/]/.test(reportName));
  const buildValid = Boolean(analysisValid && outputParent.trim() && reportNameValid);
  const buildJob = useQuery({ queryKey: ['job', activeBuildJob?.id], queryFn: () => api.jobs.get(activeBuildJob!.id), enabled: Boolean(activeBuildJob), refetchInterval: (query) => ['queued', 'running'].includes(query.state.data?.state ?? '') ? 750 : false });
  const buildRunning = ['queued', 'running'].includes(buildJob.data?.state ?? activeBuildJob?.state ?? '');
  const rebuildJob = useQuery({ queryKey: ['job', activeRebuildJob?.id], queryFn: () => api.jobs.get(activeRebuildJob!.id), enabled: Boolean(activeRebuildJob), refetchInterval: (query) => ['queued', 'running'].includes(query.state.data?.state ?? '') ? 750 : false });
  useEffect(() => {
    if (buildJob.data?.state === 'succeeded' || rebuildJob.data?.state === 'succeeded') {
      void queryClient.invalidateQueries({ queryKey: ['datasets'] });
      void queryClient.invalidateQueries({ queryKey: ['report-browser'] });
      void queryClient.invalidateQueries({ queryKey: ['report-preview'] });
      void queryClient.invalidateQueries({ queryKey: ['report-preview-id'] });
      void queryClient.invalidateQueries({ queryKey: ['report-details'] });
    }
  }, [buildJob.data?.state, queryClient, rebuildJob.data?.state]);
  useEffect(() => {
    if (location.hash !== '#reports') return;
    const frame = window.requestAnimationFrame(() => document.getElementById('reports')?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
    return () => window.cancelAnimationFrame(frame);
  }, [location.hash]);
  const normalizedUnavailable = sourceKind === 'campaign' || Boolean(specPath.trim())
    || validationMode !== 'default' || deepValidation || reportMode === 'static' || sensitivityMode !== 'default';

  function analysisRequest(): Omit<ReportBuildRequest, 'output_dir' | 'overwrite'> {
    return {
      ...(sourceKind === 'results' ? { experiments: experimentDescriptors } : { campaign_path: campaignPath.trim() }),
      ...(specPath.trim() ? { spec_path: specPath.trim() } : {}),
      ...(validationMode !== 'default' ? { validation_mode: validationMode } : {}),
      deep_validation: deepValidation,
      report_mode: reportMode,
      engine,
      ...(sensitivityMode !== 'default' ? { sensitivity: sensitivityMode === 'enabled' } : {}),
    };
  }

  function buildReport() {
    const body: ReportBuildRequest = {
      ...analysisRequest(), output_dir: outputDir.trim(), overwrite,
    };
    build.mutate(body);
  }

  function previewReport() {
    const body: ReportPreviewBuildRequest = { ...analysisRequest(), report_name: reportName.trim() };
    previewBuild.mutate(body);
  }

  function validateResults() {
    validation.mutate({ experiments: experimentDescriptors, deep: deepValidation });
  }
  function openBuildReport() {
    setBuildStep('experiments');
    setOutputParent(ANALYSIS_ROOT);
    setDestinationBrowsePath(ANALYSIS_ROOT);
    setActiveBuildJob(undefined);
    build.reset();
    previewBuild.reset();
    setBuildBrowsePath(DEFAULT_RECORD_PATH);
    setNewFolderName('');
    setShowNewFolder(false);
    reportControls.open();
  }
  const items = reportQueries.flatMap((query) => query.data ? [query.data] : []).filter((item) => !search.trim() || `${item.name} ${item.path}`.toLowerCase().includes(search.trim().toLowerCase())).sort((left, right) => Number(right.status === 'ready') - Number(left.status === 'ready') || String(right.generated_at ?? '').localeCompare(String(left.generated_at ?? '')));
  const reportsLoading = browser.isLoading || reportQueries.some((query) => query.isLoading);
  const reportsError = browser.error ?? reportQueries.find((query) => query.error)?.error;
  const totalRuns = items.reduce((sum, item) => sum + item.run_count, 0);
  const findings = items.reduce((sum, item) => sum + (item.health?.length ?? 0), 0);
  const managedExperiments = Array.isArray(reportDetails.data?.experiments)
    ? reportDetails.data.experiments.filter((value): value is Record<string, unknown> => Boolean(value) && typeof value === 'object' && !Array.isArray(value))
    : [];
  const relativeReportPath = (path: string) => {
    const base = browser.data?.path?.replace(/[\\/]+$/, '');
    return base && path.startsWith(`${base}/`) ? path.slice(base.length + 1) : path === base ? '.' : path;
  };

  return (
    <>
      <PageHeader
        eyebrow="Research workspace"
        title="From experiments to defensible evidence"
        description="Build, inspect, and publish simulation evidence without moving between command-line tools. Source data remains read-only unless an advanced action is explicitly confirmed."
        actions={<Button leftSection={<IconPlus size={17} />} onClick={openBuildReport}>Build report</Button>}
      />

      <div className="pisa-page-grid">
        <MetricCard label="Reports" value={items.length.toLocaleString()} detail="Indexed workspaces" icon={<IconChartDots size={20} />} />
        <MetricCard label="Runs" value={totalRuns.toLocaleString()} detail="Across available reports" icon={<IconDatabase size={20} />} color="cyan" />
        <MetricCard label="Data findings" value={findings.toLocaleString()} detail="Warnings remain visible" icon={<IconHeartbeat size={20} />} color={findings ? 'yellow' : 'teal'} />
        <MetricCard label="API" value={reportsError ? 'Offline' : reportsLoading ? 'Loading' : 'Ready'} detail="Cached local analysis service" icon={<IconRefresh size={20} />} color={reportsError ? 'gray' : 'teal'} />
      </div>

      <Group id="reports" justify="space-between" mt="xl" mb="md" align="flex-end" style={{ scrollMarginTop: 84 }}>
        <div><Text fw={650}>Report browser</Text><Text size="sm" c="dimmed">Only reports directly inside the current directory are listed. Navigate deliberately instead of flattening every nested report.</Text></div>
        <TextInput value={search} onChange={(event) => setSearch(event.currentTarget.value)} placeholder="Search reports…" leftSection={<IconSearch size={16} />} w={{ base: '100%', sm: 280 }} />
      </Group>

      <Card p="md" mb="md">
        <Group align="flex-end" wrap="nowrap">
          <ActionIcon aria-label="Parent directory" variant="default" size="lg" disabled={!browser.data?.parent} onClick={() => browser.data?.parent && setLibraryPath(browser.data.parent)}><IconArrowLeft size={17} /></ActionIcon>
          <TextInput label="Current directory" value={libraryPath || browser.data?.path || ''} onChange={(event) => setLibraryPath(event.currentTarget.value)} onKeyDown={(event) => { if (event.key === 'Enter') browser.refetch(); }} leftSection={<IconFolder size={16} />} style={{ flex: 1 }} />
          <Button variant="default" loading={browser.isFetching} onClick={() => browser.refetch()}>Open</Button>
        </Group>
        {browser.error && <div style={{ marginTop: 12 }}><InlineError error={browser.error} onRetry={() => browser.refetch()} /></div>}
        {browser.data && <ScrollArea mt="md" type="auto"><Group gap="xs" wrap="nowrap">{browser.data.entries.filter((entry) => entry.kind === 'directory').map((entry) => <Button key={entry.path} variant="subtle" color={entry.looks_like_output ? 'indigo' : 'gray'} leftSection={<IconFolder size={15} />} onClick={() => setLibraryPath(entry.path)}>{entry.name}{entry.looks_like_output ? ' · output' : ''}</Button>)}</Group></ScrollArea>}
      </Card>

      {reportsError && !items.length ? (
        <InlineError error={reportsError} onRetry={() => browser.refetch()} />
      ) : reportsLoading && !items.length ? <PageLoading label="Finding reports… the first report will appear immediately." />
      : !items.length ? (
        <Card><EmptyState title="No reports indexed yet" description="Choose a PISA experiment output directory to validate it and build an interactive report." action={<Button variant="light" leftSection={<IconFolderSearch size={17} />} onClick={openBuildReport}>Choose output directory</Button>} /></Card>
      ) : (
        <Stack gap="sm">{reportsLoading && <Group gap="xs"><Progress value={100} animated style={{ flex: 1 }} /><Text size="xs" c="dimmed">Loading remaining reports… {items.length} ready</Text></Group>}<SimpleGrid cols={{ base: 1, md: 2, xl: 3 }}>
          {items.map((item) => {
            const severe = item.health?.filter((finding) => finding.severity !== 'info').length ?? 0;
            return (
              <Card key={item.id} p="lg">
                <Stack gap="md">
                  <Group justify="space-between" align="flex-start" wrap="nowrap">
                    <ThemeIcon size={42} radius="md" variant="light" color={item.status === 'legacy' ? 'yellow' : 'indigo'}><IconChartDots size={21} /></ThemeIcon>
                    <StatusBadge value={item.status} />
                  </Group>
                  <div>
                    <Text fw={650} lineClamp={1}>{item.name}</Text>
                    <Text className="pisa-code" c="dimmed" lineClamp={1} title={item.path}>{relativeReportPath(item.path)}</Text>
                    <Text size="xs" c="dimmed" mt={4}>Created {item.generated_at ? new Date(item.generated_at).toLocaleString() : 'time not recorded'}</Text>
                  </div>
                  <Group gap="xs">
                    <Badge variant="light" color="gray">{item.experiment_count.toLocaleString()} experiments</Badge>
                    <Badge variant="light" color="gray">{item.run_count.toLocaleString()} runs</Badge>
                    {severe > 0 && <Badge variant="light" color="yellow">{severe} findings</Badge>}
                  </Group>
                  <Stack gap={5}>
                    {[['Scenario', item.scenario_names], ['Sampler', item.sampler_names], ['Simulator', item.simulator_names], ['AV', item.av_names]].map(([label, values]) => <Group key={String(label)} gap="xs" wrap="nowrap"><Text size="xs" c="dimmed" w={62}>{String(label)}</Text><Group gap={5}>{(values as string[] | undefined)?.length ? (values as string[]).map((value) => <Badge key={value} size="xs" variant="outline" color="gray">{value}</Badge>) : <Text size="xs" c="dimmed">—</Text>}</Group></Group>)}
                  </Stack>
                  <Group justify="flex-end">
                    <Group gap="xs">
                      {item.status === 'legacy' && <Tooltip label="Update this report to the latest data index and interface"><Button aria-label={`Update ${item.name}`} size="compact-sm" color="yellow" variant="light" leftSection={<IconHistory size={16} />} onClick={() => { setRebuildOutput(''); setRebuildSensitivity('default'); setRebuildOverwrite(true); setActiveRebuildJob(undefined); rebuild.reset(); setRebuildTarget(item); }}>Update</Button></Tooltip>}
                      <Tooltip label="Preview and manage"><ActionIcon aria-label={`Manage ${item.name}`} variant="light" color="gray" onClick={() => { setManageTarget(item); setManageTab('preview'); setRenameName(item.name); setDeleteConfirmation(''); }}><IconEye size={17} /></ActionIcon></Tooltip>
                      <Tooltip label="Open report"><ActionIcon aria-label={`Open ${item.name}`} variant="light" onClick={() => navigate(`/reports/${encodeURIComponent(item.id)}/overview`)}><IconArrowRight size={17} /></ActionIcon></Tooltip>
                    </Group>
                  </Group>
                </Stack>
              </Card>
            );
          })}
        </SimpleGrid></Stack>
      )}

      <Modal opened={reportOpened} onClose={reportControls.close} title="Build report" size="xl">
        <Stack>
          <Group gap="xs"><Badge variant={buildStep === 'experiments' ? 'filled' : 'light'}>1 · Experiments</Badge><Badge variant={buildStep === 'destination' ? 'filled' : 'light'}>2 · Save or preview</Badge></Group>
          {buildStep === 'experiments' ? <>
          <Text size="sm" c="dimmed">Choose the input source first, then browse only valid record folders. Existing source data is never modified.</Text>
          <Select label="Input source" value={sourceKind} onChange={(value) => {
            const next = (value ?? 'results') as 'results' | 'campaign';
            setSourceKind(next);
            if (next === 'campaign' && engine === 'normalized') setEngine('auto');
            validation.reset();
          }} allowDeselect={false} data={[{ value: 'results', label: 'One or more record folders' }, { value: 'campaign', label: 'Campaign YAML' }]} />
          {sourceKind === 'campaign' && <TextInput label="Campaign YAML" value={campaignPath} onChange={(event) => setCampaignPath(event.currentTarget.value)} placeholder="/path/to/campaign.yaml" required />}
          {sourceKind === 'results' && <>
          <Card withBorder p="md">
            <Group align="flex-end" wrap="wrap">
              <ActionIcon aria-label="Parent build directory" variant="default" size="lg" disabled={!buildBrowser.data?.parent} onClick={() => buildBrowser.data?.parent && setBuildBrowsePath(buildBrowser.data.parent)}><IconArrowLeft size={17} /></ActionIcon>
              <TextInput label="Record folder browser" value={buildBrowsePath || buildBrowser.data?.path || ''} onChange={(event) => setBuildBrowsePath(event.currentTarget.value)} leftSection={<IconFolderSearch size={16} />} style={{ flex: '1 1 420px' }} />
              <Button variant="default" loading={buildBrowser.isFetching} onClick={() => buildBrowser.refetch()}>Open</Button>
              <Button variant="default" leftSection={<IconFolderPlus size={16} />} onClick={() => { createDirectory.reset(); setShowNewFolder((value) => !value); }}>New folder</Button>
              {buildBrowser.data?.looks_like_output && <Button loading={sourceInspection.isPending} onClick={() => sourceInspection.mutate(buildBrowsePath || buildBrowser.data?.path || '')}>Preview current record</Button>}
            </Group>
            {showNewFolder && <Group mt="sm" align="flex-end" wrap="nowrap"><TextInput autoFocus label="New directory name" placeholder="report-output" value={newFolderName} onChange={(event) => setNewFolderName(event.currentTarget.value)} onKeyDown={(event) => { if (event.key === 'Enter' && newFolderName.trim()) createDirectory.mutate(); }} error={createDirectory.error instanceof Error ? createDirectory.error.message : undefined} style={{ flex: 1 }} /><Button variant="default" onClick={() => { setShowNewFolder(false); setNewFolderName(''); createDirectory.reset(); }}>Cancel</Button><Button loading={createDirectory.isPending} disabled={!newFolderName.trim()} onClick={() => createDirectory.mutate()}>Create and open</Button></Group>}
            {buildBrowser.error && <div style={{ marginTop: 12 }}><InlineError error={buildBrowser.error} onRetry={() => buildBrowser.refetch()} /></div>}
            {buildBrowser.data && <ScrollArea mt="md" h={180}><Stack gap={4}>{buildBrowser.data.entries.filter((entry) => entry.kind === 'directory').map((entry) => <Group key={entry.path} justify="space-between" wrap="nowrap"><Button variant="subtle" color={entry.looks_like_output ? 'indigo' : 'gray'} leftSection={<IconFolder size={15} />} onClick={() => setBuildBrowsePath(entry.path)} style={{ flex: 1, justifyContent: 'flex-start' }}>{entry.name}</Button>{entry.looks_like_output && <Button size="compact-xs" variant="light" onClick={() => sourceInspection.mutate(entry.path)}>Preview experiment</Button>}</Group>)}</Stack></ScrollArea>}
          </Card>
          {sourceInspection.error && <InlineError error={sourceInspection.error} />}
          {candidate && <Alert color="blue" title={`Detected ${candidate.dataset_id} · ${candidate.run_count.toLocaleString()} runs`}><Stack gap="xs"><Text size="sm">scenario {candidate.scenario_name ?? 'unknown'} · map {candidate.map_name ?? 'unknown'} · sim {candidate.simulator ?? 'unknown'} · AV {candidate.av ?? 'unknown'} · sampler {candidate.sampler ?? 'unknown'}</Text><Group justify="space-between"><Text size="xs" c="dimmed">{candidate.results}</Text><Button size="compact-sm" disabled={experiments.some((item) => item.results === candidate.results)} onClick={() => { setExperiments((items) => [...items, candidate]); setCandidate(undefined); validation.reset(); }}>Add experiment</Button></Group><Accordion variant="contained"><Accordion.Item value="candidate-detail"><Accordion.Control>All automatically detected configuration</Accordion.Control><Accordion.Panel><ScrollArea h={260}><Code block>{JSON.stringify(candidate, null, 2)}</Code></ScrollArea></Accordion.Panel></Accordion.Item></Accordion></Stack></Alert>}
          <Card p="md"><Group justify="space-between" mb="sm"><div><Text fw={650}>Experiments included in report</Text><Text size="xs" c="dimmed">A multi-experiment report keeps every normal report view. Compatible scenario, map, sampler, and sample points are still required so shared analyses remain defensible.</Text></div><Group gap="xs"><Button size="compact-sm" variant="default" leftSection={<IconFolderSearch size={15} />} onClick={() => setBuildBrowsePath(DEFAULT_RECORD_PATH)}>Browse records</Button><Badge>{experiments.length}</Badge></Group></Group>{experiments.length ? <Stack gap="xs">{experiments.map((item, index) => <Card key={item.results} withBorder p="sm"><Group justify="space-between" wrap="nowrap"><div><Text size="sm" fw={600}>{index + 1}. {item.dataset_id}</Text><Text size="xs" c="dimmed">{item.simulator ?? 'unknown sim'} · {item.av ?? 'unknown AV'} · {item.sampler ?? 'unknown sampler'} · {item.run_count} runs</Text><Text className="pisa-code" size="xs" c="dimmed">{buildBrowser.data?.path && item.results.startsWith(`${buildBrowser.data.path.replace(/[\\/]+$/, '')}/`) ? item.results.slice(buildBrowser.data.path.replace(/[\\/]+$/, '').length + 1) : item.results}</Text></div><Group gap="xs" wrap="nowrap"><Button variant="subtle" size="compact-xs" leftSection={<IconFolderSearch size={14} />} onClick={() => setBuildBrowsePath(item.results)}>Browse</Button><Button color="red" variant="subtle" size="compact-xs" onClick={() => setExperiments((values) => values.filter((entry) => entry.results !== item.results))}>Remove</Button></Group></Group></Card>)}</Stack> : <Text size="sm" c="dimmed">Browse and preview a record folder, then add it here.</Text>}{compatibility.isLoading && <Text size="xs" c="blue" mt="sm">Checking report compatibility…</Text>}{compatibility.data && <Alert mt="sm" color={compatibility.data.compatible ? 'teal' : 'red'} title={compatibility.data.compatible ? 'Experiments can share one report' : 'Experiments cannot share one report'}>{compatibility.data.compatible ? 'Sampler points, scenario, map, sampler and OpenDRIVE provenance match. Compare and Consistency views will be available alongside the original report views.' : <Code block>{JSON.stringify(compatibility.data.errors, null, 2)}</Code>}</Alert>}</Card>
          </>}
          <Divider label="Build settings" labelPosition="center" />
          <Accordion variant="contained">
            <Accordion.Item value="advanced-report-options">
              <Accordion.Control>Advanced validation and build options</Accordion.Control>
              <Accordion.Panel>
                <Stack>
                  <TextInput label="Analysis spec YAML" description="Not required for the normalized interactive report; leave empty to use the built-in evidence specification. Set a path only for a custom legacy/static analysis." value={specPath} onChange={(event) => { const value = event.currentTarget.value; setSpecPath(value); if (value.trim() && engine === 'normalized') setEngine('auto'); }} placeholder="Optional · /path/to/analysis-spec.yaml" />
                  <SimpleGrid cols={{ base: 1, sm: 2 }}>
                    <Select label="Validation policy" value={validationMode} onChange={(value) => { const next = (value ?? 'default') as typeof validationMode; setValidationMode(next); if (next !== 'default' && engine === 'normalized') setEngine('auto'); }} allowDeselect={false} data={[{ value: 'default', label: 'Default policy' }, { value: 'strict', label: 'Strict · stop on findings' }, { value: 'permissive', label: 'Permissive · disclose findings' }]} />
                    <Select label="Report mode" value={reportMode} onChange={(value) => {
                      const next = (value ?? 'interactive') as 'interactive' | 'static';
                      setReportMode(next);
                      if (next === 'static' && engine !== 'legacy') setEngine('legacy');
                    }} allowDeselect={false} data={[{ value: 'interactive', label: 'Interactive workbench' }, { value: 'static', label: 'Static portable report' }]} />
                    <Select label="Report engine" value={engine} onChange={(value) => setEngine((value ?? 'auto') as NonNullable<ReportBuildRequest['engine']>)} allowDeselect={false} data={[
                      { value: 'auto', label: 'Auto · recommended' },
                      { value: 'normalized', label: 'Normalized high-performance index', disabled: normalizedUnavailable },
                      { value: 'legacy', label: 'Legacy evidence pipeline' },
                    ]} />
                  </SimpleGrid>
                  <Checkbox label="Deep validation" description="Inspect monitor files and run-level evidence; slower on large campaigns." checked={deepValidation} onChange={(event) => { const checked = event.currentTarget.checked; setDeepValidation(checked); if (checked && engine === 'normalized') setEngine('auto'); validation.reset(); }} />
                  <Select label="Sensitivity analysis" description="Explicitly enable or disable it, or inherit the analysis specification." value={sensitivityMode} onChange={(value) => { const next = (value ?? 'default') as typeof sensitivityMode; setSensitivityMode(next); if (next !== 'default' && engine === 'normalized') setEngine('auto'); }} allowDeselect={false} data={[{ value: 'default', label: 'Use analysis-spec default' }, { value: 'enabled', label: 'Enabled' }, { value: 'disabled', label: 'Disabled' }]} />
                </Stack>
              </Accordion.Panel>
            </Accordion.Item>
          </Accordion>
          {validation.isSuccess && <Alert color="teal" title="Validation passed"><Stack gap="xs"><Text size="sm">The selected result directories satisfy the report input contract.</Text><Accordion variant="contained"><Accordion.Item value="validation-result"><Accordion.Control>Validation details</Accordion.Control><Accordion.Panel><ScrollArea h={260}><Code block>{JSON.stringify(validation.data, null, 2)}</Code></ScrollArea></Accordion.Panel></Accordion.Item></Accordion></Stack></Alert>}
          {validation.error && <InlineError error={validation.error} />}
          <Group justify="space-between">
            <Button variant="subtle" loading={validation.isPending} disabled={sourceKind !== 'results' || !parsedResults.length} onClick={validateResults}>Validate source</Button>
            <Group><Button variant="default" onClick={reportControls.close}>Close</Button><Button rightSection={<IconArrowRight size={15} />} disabled={!analysisValid} onClick={() => { setDestinationBrowsePath(ANALYSIS_ROOT); setOutputParent(ANALYSIS_ROOT); setActiveBuildJob(undefined); build.reset(); previewBuild.reset(); setBuildStep('destination'); }}>Continue</Button></Group>
          </Group>
          </> : <>
            <Alert color="blue" title="Analysis ready"><Text size="sm">{experiments.length.toLocaleString()} experiment{experiments.length === 1 ? '' : 's'} · {experiments.reduce((sum, item) => sum + item.run_count, 0).toLocaleString()} runs · {engine} engine · {reportMode} report</Text><Text size="xs" mt={4}>{experiments.map((item) => `${item.dataset_id} (${item.simulator ?? 'unknown sim'} / ${item.av ?? 'unknown AV'} / ${item.sampler ?? 'unknown sampler'})`).join(' · ') || campaignPath}</Text></Alert>
            <TextInput label="Report name" description="Automatically suggested from the selected experiment configuration." value={reportName} onChange={(event) => setReportName(event.currentTarget.value)} error={!reportNameValid ? 'Use one directory name without slashes.' : undefined} required />
            <Card withBorder p="md">
              <Group align="flex-end" wrap="wrap">
                <ActionIcon aria-label="Parent destination directory" variant="default" size="lg" disabled={!destinationBrowser.data?.parent} onClick={() => { if (destinationBrowser.data?.parent) { setDestinationBrowsePath(destinationBrowser.data.parent); setOutputParent(destinationBrowser.data.parent); } }}><IconArrowLeft size={17} /></ActionIcon>
                <TextInput label="Report destination browser" value={destinationBrowsePath || destinationBrowser.data?.path || ANALYSIS_ROOT} onChange={(event) => setDestinationBrowsePath(event.currentTarget.value)} leftSection={<IconFolder size={16} />} style={{ flex: '1 1 440px' }} />
                <Button variant="default" loading={destinationBrowser.isFetching} onClick={() => { setOutputParent(destinationBrowsePath); void destinationBrowser.refetch(); }}>Open</Button>
                <Button variant="default" leftSection={<IconFolderPlus size={16} />} onClick={() => { createDestinationDirectory.reset(); setShowDestinationFolder((value) => !value); }}>New folder</Button>
                <Button onClick={() => setOutputParent(destinationBrowsePath || destinationBrowser.data?.path || ANALYSIS_ROOT)}>Use this folder</Button>
              </Group>
              {showDestinationFolder && <Group mt="sm" align="flex-end" wrap="nowrap"><TextInput autoFocus label="New directory name" value={destinationFolderName} onChange={(event) => setDestinationFolderName(event.currentTarget.value)} style={{ flex: 1 }} /><Button variant="default" onClick={() => setShowDestinationFolder(false)}>Cancel</Button><Button loading={createDestinationDirectory.isPending} disabled={!destinationFolderName.trim()} onClick={() => createDestinationDirectory.mutate()}>Create and open</Button></Group>}
              {destinationBrowser.error && <div style={{ marginTop: 12 }}><InlineError error={destinationBrowser.error} onRetry={() => destinationBrowser.refetch()} /></div>}
              {destinationBrowser.data && <ScrollArea mt="md" h={220}><Stack gap={4}>{destinationBrowser.data.entries.filter((entry) => entry.kind === 'directory' || entry.kind === 'report').map((entry) => <Button key={entry.path} variant="subtle" color={entry.is_report ? 'indigo' : 'gray'} leftSection={<IconFolder size={15} />} onClick={() => { if (entry.is_report) { setOutputParent(destinationBrowser.data.path); setReportName(entry.name); } else { setDestinationBrowsePath(entry.path); setOutputParent(entry.path); } }} style={{ justifyContent: 'flex-start' }}>{entry.name}{entry.is_report ? ' · existing report' : ''}</Button>)}</Stack></ScrollArea>}
            </Card>
            <SimpleGrid cols={{ base: 1, sm: 2 }}><Alert color="gray" title="Selected output parent">{outputParent}</Alert><Alert color="gray" title="Resolved report path">{outputDir}</Alert></SimpleGrid>
            <Checkbox label="Replace an existing PISA-owned report" checked={overwrite} onChange={(event) => setOverwrite(event.currentTarget.checked)} />
            <Alert color="gray" title="Preview without saving">Preview builds the complete report in temporary storage. You can save it later from Overview; otherwise it is removed after you leave.</Alert>
            {build.error && <InlineError error={build.error} />}
            {previewBuild.error && <InlineError error={previewBuild.error} />}
            {activeBuildJob && <JobProgress job={buildJob.data ?? activeBuildJob} />}
            <Group justify="space-between">
              <Button variant="default" leftSection={<IconArrowLeft size={15} />} disabled={buildRunning} onClick={() => setBuildStep('experiments')}>Back</Button>
              {buildJob.data?.state === 'succeeded' && buildJob.data.report_id ? <Button onClick={() => { reportControls.close(); navigate(`/reports/${encodeURIComponent(buildJob.data!.report_id!)}/overview`); }}>Open report</Button> : <Group><Button variant="light" loading={previewBuild.isPending} disabled={!analysisValid || !reportNameValid || build.isPending || buildRunning} onClick={previewReport}>Preview without saving</Button><Button loading={build.isPending} disabled={!buildValid || previewBuild.isPending || buildRunning} onClick={buildReport}>Create report</Button></Group>}
            </Group>
          </>}
        </Stack>
      </Modal>

      <Modal opened={Boolean(rebuildTarget)} onClose={() => setRebuildTarget(undefined)} title="Update report" size="md">
        <Stack>
          <Alert color={rebuildOverwrite ? 'yellow' : 'blue'} title={rebuildOverwrite ? 'Replace the legacy report after a successful rebuild' : 'Non-destructive normalized upgrade'}>{rebuildOverwrite ? 'The replacement is staged completely before the old report is atomically replaced. Recorded experiment sources are not modified.' : 'The current report remains untouched. A timestamped sibling report is created by default.'}</Alert>
          <Checkbox label="Replace the old report in place" checked={rebuildOverwrite} onChange={(event) => setRebuildOverwrite(event.currentTarget.checked)} />
          <TextInput label="New output directory" disabled={rebuildOverwrite} description={rebuildOverwrite ? 'The existing report path will be replaced atomically.' : 'Optional; a timestamped sibling directory is used when empty.'} value={rebuildOutput} onChange={(event) => setRebuildOutput(event.currentTarget.value)} />
          {rebuild.error && <InlineError error={rebuild.error} />}
          {activeRebuildJob && <JobProgress job={rebuildJob.data ?? activeRebuildJob} />}
          <Group justify="flex-end"><Button variant="default" onClick={() => setRebuildTarget(undefined)}>Cancel</Button>{rebuildJob.data?.state === 'succeeded' && rebuildJob.data.report_id ? <Button onClick={() => { setRebuildTarget(undefined); navigate(`/reports/${encodeURIComponent(rebuildJob.data!.report_id!)}/overview`); }}>Open report</Button> : <Button loading={rebuild.isPending} disabled={['queued', 'running'].includes(rebuildJob.data?.state ?? '')} onClick={() => rebuild.mutate()}>Queue rebuild</Button>}</Group>
        </Stack>
      </Modal>

      <Modal opened={Boolean(manageTarget)} onClose={() => setManageTarget(undefined)} title={manageTarget ? `Manage ${manageTarget.name}` : 'Manage report'} size="xl">
        <Tabs value={manageTab} onChange={setManageTab}>
          <Tabs.List grow><Tabs.Tab value="preview" leftSection={<IconEye size={15} />}>Preview</Tabs.Tab><Tabs.Tab value="rename" leftSection={<IconEdit size={15} />}>Rename</Tabs.Tab><Tabs.Tab value="delete" color="red" leftSection={<IconTrash size={15} />}>Delete</Tabs.Tab></Tabs.List>
          <Tabs.Panel value="preview" pt="lg"><Stack>
            {manageTarget && <SimpleGrid cols={{ base: 1, sm: 4 }}><MetricCard label="Experiments" value={manageTarget.experiment_count.toLocaleString()} detail="Recorded configurations" icon={<IconDatabase size={18} />} /><MetricCard label="Runs" value={manageTarget.run_count.toLocaleString()} detail="Browsable concrete cases" icon={<IconChartDots size={18} />} /><MetricCard label="Status" value={manageTarget.status.toUpperCase()} detail={`build v${manageTarget.report_build_version ?? 'unknown'}`} icon={<IconHeartbeat size={18} />} /><MetricCard label="Findings" value={(manageTarget.health?.length ?? 0).toLocaleString()} detail="Data-health records" icon={<IconHistory size={18} />} /></SimpleGrid>}
            <Text className="pisa-code" size="xs">{manageTarget?.path}</Text>
            {reportDetails.isLoading ? <PageLoading label="Loading scenarios, experiments, provenance, and index metadata…" /> : reportDetails.error ? <InlineError error={reportDetails.error} onRetry={() => reportDetails.refetch()} /> : <><SimpleGrid cols={{ base: 1, md: 2 }}>{managedExperiments.map((experiment) => <Card key={String(experiment.dataset_id)} withBorder p="md"><Group justify="space-between"><div><Text fw={650} size="sm">{String(experiment.dataset_id)}</Text><Text size="xs" c="dimmed">Scenario {String(experiment.scenario ?? 'not recorded')}</Text></div><Badge variant="light">{Number(experiment.run_count ?? 0).toLocaleString()} runs</Badge></Group><SimpleGrid cols={3} mt="md"><div><Text size="xs" c="dimmed">Simulator</Text><Text size="sm">{String(experiment.simulator ?? 'unknown')}</Text></div><div><Text size="xs" c="dimmed">AV</Text><Text size="sm">{String(experiment.av ?? 'unknown')}</Text></div><div><Text size="xs" c="dimmed">Sampler</Text><Text size="sm">{String(experiment.sampler ?? 'unknown')}</Text></div></SimpleGrid><Text size="xs" c="dimmed" mt="sm">{Number(experiment.attempt_count ?? 0).toLocaleString()} attempts · expected {experiment.expected_runs == null ? 'not recorded' : Number(experiment.expected_runs).toLocaleString()}</Text></Card>)}</SimpleGrid><Accordion multiple variant="contained" defaultValue={['all-metadata']}>
              <Accordion.Item value="all-metadata"><Accordion.Control>All recorded report, scenario, experiment, component, index, and provenance metadata</Accordion.Control><Accordion.Panel><ScrollArea h={430}><Code block>{JSON.stringify(reportDetails.data, null, 2)}</Code></ScrollArea></Accordion.Panel></Accordion.Item>
            </Accordion></>}
            <Group justify="flex-end"><Button variant="default" onClick={() => setManageTarget(undefined)}>Close</Button>{manageTarget && <Button onClick={() => navigate(`/reports/${encodeURIComponent(manageTarget.id)}/overview`)}>Open report workspace</Button>}</Group>
          </Stack></Tabs.Panel>
          <Tabs.Panel value="rename" pt="lg"><Stack><Alert color="blue" title="Rename report directory">The bundle contents and source outputs are unchanged. The report receives a new ID because its canonical path changes.</Alert><TextInput label="New report name" value={renameName} onChange={(event) => setRenameName(event.currentTarget.value)} />{renameReport.error && <InlineError error={renameReport.error} />}<Group justify="flex-end"><Button variant="default" onClick={() => setManageTarget(undefined)}>Cancel</Button><Button loading={renameReport.isPending} disabled={!renameName.trim() || renameName.trim() === manageTarget?.name} onClick={() => renameReport.mutate()}>Rename report</Button></Group></Stack></Tabs.Panel>
          <Tabs.Panel value="delete" pt="lg"><Stack><Alert color="red" title="Permanent report deletion">Only this generated PISA report bundle is deleted. Its referenced experiment outputs are not touched.</Alert><Text size="sm">Type <Code>{manageTarget?.name}</Code> to confirm.</Text><TextInput label="Confirmation" value={deleteConfirmation} onChange={(event) => setDeleteConfirmation(event.currentTarget.value)} />{deleteReport.error && <InlineError error={deleteReport.error} />}<Group justify="flex-end"><Button variant="default" onClick={() => setManageTarget(undefined)}>Cancel</Button><Button color="red" loading={deleteReport.isPending} disabled={deleteConfirmation !== manageTarget?.name} onClick={() => deleteReport.mutate()}>Delete report</Button></Group></Stack></Tabs.Panel>
        </Tabs>
      </Modal>
    </>
  );
}
