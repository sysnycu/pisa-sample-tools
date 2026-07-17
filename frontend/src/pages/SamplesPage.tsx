import { useMemo, useState } from 'react';
import {
  Accordion, ActionIcon, Alert, Badge, Button, Card, Checkbox, Group, NumberInput,
  ScrollArea, SegmentedControl, Select, SimpleGrid, Stack, Tabs, Text, Textarea,
  TextInput, ThemeIcon,
} from '@mantine/core';
import {
  IconAnalyze, IconArchive, IconChartDots, IconDownload, IconInfoCircle, IconPlus,
  IconSparkles, IconTrash,
} from '@tabler/icons-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import type {
  InlineSamplePreviewRequest, SampleAnalyzeRequest, SampleExportRequest,
  SamplerParameter, SamplePreviewRequest, VisualizationSpec,
} from '../api/types';
import { InlineError } from '../components/Feedback';
import { PageHeader } from '../components/PageHeader';
import { VisualizationCard } from '../components/VisualizationCard';

const initialParameters: SamplerParameter[] = [
  { name: 'Ego_Speed', min: 10, max: 30 },
  { name: 'Agent_Speed', min: 5, max: 25 },
  { name: 'Cutin_Distance', min: 8, max: 50 },
  { name: 'Cutin_Time', min: 0.5, max: 4 },
];

export function SamplesPage() {
  const [tab, setTab] = useState<string | null>('preview');
  const [previewMode, setPreviewMode] = useState<'inline' | 'source'>('inline');
  const [method, setMethod] = useState<InlineSamplePreviewRequest['method']>('lhs');
  const [count, setCount] = useState<number | string>(256);
  const [seed, setSeed] = useState<number | string>(42);
  const [parameters, setParameters] = useState<SamplerParameter[]>(initialParameters);
  const [sourceFile, setSourceFile] = useState('');
  const [sourceSampler, setSourceSampler] = useState('');
  const [sourceType, setSourceType] = useState('');
  const [sourceModule, setSourceModule] = useState('');
  const [sourceConfigPath, setSourceConfigPath] = useState('');
  const [sourceConfig, setSourceConfig] = useState('');
  const [sourceMaxSamples, setSourceMaxSamples] = useState<number | string>(100);

  const [exportSource, setExportSource] = useState<'runner' | 'sampler'>('runner');
  const [runnerSpec, setRunnerSpec] = useState('');
  const [samplerSpec, setSamplerSpec] = useState('');
  const [scenarioPath, setScenarioPath] = useState('');
  const [outputPath, setOutputPath] = useState('./samples');
  const [shardMode, setShardMode] = useState<'num_shards' | 'shard_size'>('num_shards');
  const [shardValue, setShardValue] = useState<number | string>(1);
  const [pathMode, setPathMode] = useState<SampleExportRequest['source_path_mode']>('relative-to-output');
  const [dryRun, setDryRun] = useState(true);
  const [zip, setZip] = useState(false);
  const [zipPath, setZipPath] = useState('');
  const [overwriteExport, setOverwriteExport] = useState(false);

  const [analysisSourceType, setAnalysisSourceType] = useState<'runner_spec_path' | 'samples_path' | 'results_path'>('results_path');
  const [analysisSource, setAnalysisSource] = useState('');
  const [analysisOutput, setAnalysisOutput] = useState('./analysis/sample-analysis');
  const [outcomeConfig, setOutcomeConfig] = useState('');
  const [postOutcomeMode, setPostOutcomeMode] = useState<'overlay' | 'replace'>('overlay');
  const [analysisParams, setAnalysisParams] = useState('');
  const [analysisColorBy, setAnalysisColorBy] = useState('outcome');
  const [analysisBins, setAnalysisBins] = useState<number | string>(28);
  const [overwriteAnalysis, setOverwriteAnalysis] = useState(false);
  const queryClient = useQueryClient();

  let parsedSourceConfig: Record<string, unknown> | undefined;
  let sourceConfigError = '';
  if (sourceConfig.trim()) {
    try {
      const candidate: unknown = JSON.parse(sourceConfig);
      if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) {
        sourceConfigError = 'Sampler config must be a JSON object.';
      } else {
        parsedSourceConfig = candidate as Record<string, unknown>;
      }
    } catch {
      sourceConfigError = 'Sampler config must be valid JSON.';
    }
  }
  const request: SamplePreviewRequest = previewMode === 'inline' ? {
    method,
    count: Number(count),
    seed: Number(seed),
    parameters,
  } : {
    source_file: sourceFile.trim(),
    ...(sourceSampler.trim() ? { sampler_name: sourceSampler.trim() } : {}),
    ...(sourceType.trim() ? { source_type: sourceType.trim() } : {}),
    ...(sourceModule.trim() ? { module_path: sourceModule.trim() } : {}),
    ...(sourceConfigPath.trim() ? { config_path: sourceConfigPath.trim() } : {}),
    ...(parsedSourceConfig ? { config: parsedSourceConfig } : {}),
    max_samples: Number(sourceMaxSamples),
  };
  const preview = useMutation({ mutationFn: api.samples.preview });
  const exportSamples = useMutation({
    mutationFn: () => {
      const body: SampleExportRequest = {
        output_dir: outputPath.trim(),
        ...(exportSource === 'runner'
          ? { runner_spec_path: runnerSpec.trim() }
          : { sampler_spec_path: samplerSpec.trim(), scenario_path: scenarioPath.trim() }),
        ...(shardMode === 'num_shards'
          ? { num_shards: Number(shardValue) }
          : { shard_size: Number(shardValue) }),
        source_path_mode: pathMode,
        create_zip: zip,
        ...(zipPath.trim() ? { zip_path: zipPath.trim() } : {}),
        dry_run: dryRun,
        overwrite: overwriteExport,
      };
      return api.samples.export(body);
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['jobs'] }),
  });
  const analyze = useMutation({
    mutationFn: () => {
      const body: SampleAnalyzeRequest = {
        output_dir: analysisOutput.trim(),
        [analysisSourceType]: analysisSource.trim(),
        ...(analysisSourceType === 'results_path' && outcomeConfig.trim()
          ? { post_outcome_config_path: outcomeConfig.trim(), post_outcome_mode: postOutcomeMode }
          : {}),
        ...(analysisParams.split(',').map((value) => value.trim()).filter(Boolean).length
          ? { params: analysisParams.split(',').map((value) => value.trim()).filter(Boolean) }
          : {}),
        color_by: analysisColorBy.trim() || 'outcome',
        bins: Number(analysisBins),
        overwrite: overwriteAnalysis,
      };
      return api.samples.analyze(body);
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['jobs'] }),
  });

  const exportSourceValid = exportSource === 'runner'
    ? Boolean(runnerSpec.trim())
    : Boolean(samplerSpec.trim() && scenarioPath.trim());
  const inlineCountValid = Number.isInteger(Number(count)) && Number(count) >= 1 && Number(count) <= 100000;
  const sourceMaxValid = Number.isInteger(Number(sourceMaxSamples)) && Number(sourceMaxSamples) >= 0 && Number(sourceMaxSamples) <= 10000;
  const shardValid = Number.isInteger(Number(shardValue)) && Number(shardValue) >= 1 && Number(shardValue) <= 100000;
  const binsValid = Number.isInteger(Number(analysisBins)) && Number(analysisBins) >= 1 && Number(analysisBins) <= 1000;

  const scatterSpec = useMemo<VisualizationSpec>(() => {
    const result = preview.data;
    return {
      id: 'sample-preview-scatter',
      title: result ? `${result.method.toUpperCase()} coverage` : 'Parameter coverage',
      subtitle: result && result.parameter_names.length >= 2
        ? `${result.parameter_names[0]} × ${result.parameter_names[1]} · ${result.count.toLocaleString()} samples`
        : 'Preview two-dimensional coverage before writing files.',
      kind: 'scatter',
      option: result && result.parameter_names.length >= 2 ? {
        animation: false,
        color: ['#526ff0'],
        grid: { top: 24, right: 28, bottom: 52, left: 64 },
        tooltip: { trigger: 'item' },
        xAxis: { type: 'value', name: result.parameter_names[0], nameLocation: 'middle', nameGap: 32, splitLine: { lineStyle: { color: '#edf0f5' } } },
        yAxis: { type: 'value', name: result.parameter_names[1], nameLocation: 'middle', nameGap: 44, splitLine: { lineStyle: { color: '#edf0f5' } } },
        series: [{
          type: 'scatter',
          symbolSize: result.count > 1000 ? 3 : 7,
          data: result.samples.map((row) => [row[0], row[1]]),
          large: result.count > 1000,
          itemStyle: { opacity: 0.66 },
        }],
      } : {},
    };
  }, [preview.data]);

  function updateParameter(index: number, patch: Partial<SamplerParameter>) {
    setParameters((current) => current.map((parameter, itemIndex) => (
      itemIndex === index ? { ...parameter, ...patch } : parameter
    )));
  }

  return (
    <>
      <PageHeader
        eyebrow="Sampling laboratory"
        title="Design sample sets you can trust"
        description="Preview coverage, export runner-ready shards, and connect completed outcomes without leaving the sampling workflow."
      />
      <Tabs value={tab} onChange={setTab} keepMounted={false}>
        <Tabs.List mb="lg">
          <Tabs.Tab value="preview" leftSection={<IconSparkles size={16} />}>Preview & generate</Tabs.Tab>
          <Tabs.Tab value="export" leftSection={<IconArchive size={16} />}>Export & shard</Tabs.Tab>
          <Tabs.Tab value="analyze" leftSection={<IconAnalyze size={16} />}>Analyze outcomes</Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="preview">
          <SimpleGrid cols={{ base: 1, lg: 5 }}>
            <Card p="lg" style={{ gridColumn: 'span 2' }}>
              <Stack>
                <div><Text fw={650}>Sampler definition</Text><Text size="sm" c="dimmed">Define a quick sampler here or inspect the repository's native sampler implementation.</Text></div>
                <SegmentedControl fullWidth value={previewMode} onChange={(value) => { setPreviewMode(value as 'inline' | 'source'); preview.reset(); }} data={[{ label: 'Define inline', value: 'inline' }, { label: 'Source / native', value: 'source' }]} />
                {previewMode === 'inline' ? (
                  <>
                    <SegmentedControl fullWidth value={method} onChange={(value) => setMethod(value as InlineSamplePreviewRequest['method'])} data={[
                      { label: 'LHS', value: 'lhs' }, { label: 'Sobol', value: 'sobol' },
                      { label: 'Grid', value: 'grid' }, { label: 'Random', value: 'random' },
                    ]} />
                    <SimpleGrid cols={2}>
                      <NumberInput label="Samples" value={count} onChange={setCount} min={1} max={100000} thousandSeparator="," error={!inlineCountValid ? 'Enter 1–100,000' : undefined} />
                      <NumberInput label="Seed" value={seed} onChange={setSeed} min={0} />
                    </SimpleGrid>
                    <Group justify="space-between" mt="xs"><Text size="sm" fw={600}>Parameters</Text><Button size="compact-xs" variant="subtle" leftSection={<IconPlus size={14} />} onClick={() => setParameters((current) => [...current, { name: `Parameter_${current.length + 1}`, min: 0, max: 1 }])}>Add</Button></Group>
                    <Stack gap="xs">
                      {parameters.map((parameter, index) => (
                        <Card key={`${parameter.name}-${index}`} withBorder p="sm" radius="md">
                          <Group wrap="nowrap" align="flex-end">
                            <TextInput label={index === 0 ? 'Name' : undefined} aria-label={`Parameter ${index + 1} name`} value={parameter.name} onChange={(event) => updateParameter(index, { name: event.currentTarget.value })} style={{ flex: 1.4 }} />
                            <NumberInput label={index === 0 ? 'Min' : undefined} aria-label={`${parameter.name} minimum`} value={parameter.min} onChange={(value) => updateParameter(index, { min: Number(value) })} style={{ flex: 1 }} />
                            <NumberInput label={index === 0 ? 'Max' : undefined} aria-label={`${parameter.name} maximum`} value={parameter.max} onChange={(value) => updateParameter(index, { max: Number(value) })} style={{ flex: 1 }} />
                            <ActionIcon color="red" variant="subtle" aria-label={`Remove ${parameter.name}`} disabled={parameters.length <= 1} onClick={() => setParameters((current) => current.filter((_, itemIndex) => itemIndex !== index))}><IconTrash size={16} /></ActionIcon>
                          </Group>
                        </Card>
                      ))}
                    </Stack>
                  </>
                ) : (
                  <>
                    <TextInput label="Parameter-space source file" value={sourceFile} onChange={(event) => setSourceFile(event.currentTarget.value)} placeholder="/path/to/scenario.xosc" required />
                    <SimpleGrid cols={2}>
                      <TextInput label="Sampler name" description="Empty selects a source-aware default; use native for OpenSCENARIO." value={sourceSampler} onChange={(event) => setSourceSampler(event.currentTarget.value)} placeholder="auto / native / explicit" />
                      <TextInput label="Source type" description="Usually inferred from the file." value={sourceType} onChange={(event) => setSourceType(event.currentTarget.value)} placeholder="auto" />
                    </SimpleGrid>
                    <NumberInput label="Maximum preview rows" value={sourceMaxSamples} onChange={setSourceMaxSamples} min={0} max={10000} error={!sourceMaxValid ? 'Enter 0–10,000' : undefined} />
                    <Accordion variant="contained">
                      <Accordion.Item value="sampler-runtime">
                        <Accordion.Control>Custom sampler runtime options</Accordion.Control>
                        <Accordion.Panel><Stack>
                          <TextInput label="Python module path" value={sourceModule} onChange={(event) => setSourceModule(event.currentTarget.value)} placeholder="package.module:Sampler" />
                          <TextInput label="Sampler config file" value={sourceConfigPath} onChange={(event) => setSourceConfigPath(event.currentTarget.value)} placeholder="/path/to/config.yaml" />
                          <Textarea label="Config overrides (JSON)" value={sourceConfig} onChange={(event) => setSourceConfig(event.currentTarget.value)} minRows={3} placeholder={'{"seed": 42}'} error={sourceConfigError || undefined} />
                        </Stack></Accordion.Panel>
                      </Accordion.Item>
                    </Accordion>
                  </>
                )}
                {preview.error && <InlineError error={preview.error} />}
                <Button loading={preview.isPending} disabled={previewMode === 'inline'
                  ? !inlineCountValid || !parameters.length || parameters.some((parameter) => !parameter.name || parameter.min >= parameter.max)
                  : !sourceMaxValid || !sourceFile.trim() || Boolean(sourceConfigError)} onClick={() => preview.mutate(request)}>Generate preview</Button>
              </Stack>
            </Card>
            <div style={{ gridColumn: 'span 3' }}>
              <VisualizationCard spec={scatterSpec} emptyDescription="Define the sampler, then generate a preview to inspect coverage." />
              {preview.data?.warnings?.map((warning) => <Alert mt="sm" key={warning} color="yellow" icon={<IconInfoCircle size={17} />}>{warning}</Alert>)}
              {preview.data && (
                <Card mt="md" p="lg">
                  <Group justify="space-between" mb="sm"><Text fw={650}>Sample preview</Text><Badge variant="light">First {Math.min(20, preview.data.samples.length)} rows</Badge></Group>
                  <ScrollArea>
                    <table className="pisa-data-table"><thead><tr><th>#</th>{preview.data.parameter_names.map((name) => <th key={name}>{name}</th>)}</tr></thead><tbody>{preview.data.samples.slice(0, 20).map((row, index) => <tr key={index}><td>{index + 1}</td>{row.map((value, column) => <td key={column}>{Number.isFinite(value) ? value.toPrecision(6) : 'Missing'}</td>)}</tr>)}</tbody></table>
                  </ScrollArea>
                </Card>
              )}
            </div>
          </SimpleGrid>
        </Tabs.Panel>

        <Tabs.Panel value="export">
          <SimpleGrid cols={{ base: 1, md: 2 }}>
            <Card p="lg">
              <Stack>
                <div><Text fw={650}>Export runner-ready samples</Text><Text size="sm" c="dimmed">Choose an existing runner spec, or pair a sampler spec with its scenario source.</Text></div>
                <Select label="Specification source" value={exportSource} onChange={(value) => setExportSource((value ?? 'runner') as 'runner' | 'sampler')} allowDeselect={false} data={[
                  { value: 'runner', label: 'Runner spec YAML' },
                  { value: 'sampler', label: 'Sampler spec + scenario' },
                ]} />
                {exportSource === 'runner' ? (
                  <TextInput label="Runner spec" value={runnerSpec} onChange={(event) => setRunnerSpec(event.currentTarget.value)} placeholder="/path/to/runner_spec.yaml" required />
                ) : (
                  <>
                    <TextInput label="Sampler spec" value={samplerSpec} onChange={(event) => setSamplerSpec(event.currentTarget.value)} placeholder="/path/to/sampler.yaml" required />
                    <TextInput label="Scenario source" value={scenarioPath} onChange={(event) => setScenarioPath(event.currentTarget.value)} placeholder="/path/to/scenario" required />
                  </>
                )}
                <TextInput label="Output directory" value={outputPath} onChange={(event) => setOutputPath(event.currentTarget.value)} required />
                <SimpleGrid cols={2}>
                  <Select label="Sharding" value={shardMode} onChange={(value) => setShardMode((value ?? 'num_shards') as 'num_shards' | 'shard_size')} allowDeselect={false} data={[{ value: 'num_shards', label: 'Number of shards' }, { value: 'shard_size', label: 'Rows per shard' }]} />
                  <NumberInput label={shardMode === 'num_shards' ? 'Shards' : 'Rows per shard'} min={1} max={100000} value={shardValue} onChange={setShardValue} error={!shardValid ? 'Enter 1–100,000' : undefined} />
                </SimpleGrid>
                <Select label="Source paths in manifest" value={pathMode} onChange={(value) => setPathMode((value ?? 'relative-to-output') as SampleExportRequest['source_path_mode'])} allowDeselect={false} data={[{ value: 'relative-to-output', label: 'Portable · relative to output' }, { value: 'absolute', label: 'Absolute paths' }]} />
                <Checkbox label="Validate only (dry run)" description="Resolve and validate the export without writing files." checked={dryRun} onChange={(event) => setDryRun(event.currentTarget.checked)} />
                <Checkbox label="Create a ZIP bundle" checked={zip} onChange={(event) => setZip(event.currentTarget.checked)} disabled={dryRun} />
                {zip && !dryRun && <TextInput label="ZIP path" description="Optional; defaults beside the export directory." value={zipPath} onChange={(event) => setZipPath(event.currentTarget.value)} />}
                <Checkbox label="Replace an existing PISA-owned export" checked={overwriteExport} onChange={(event) => setOverwriteExport(event.currentTarget.checked)} />
                {exportSamples.error && <InlineError error={exportSamples.error} />}
                <Button leftSection={<IconDownload size={16} />} loading={exportSamples.isPending} disabled={!outputPath.trim() || !exportSourceValid || !shardValid} onClick={() => exportSamples.mutate()}>{dryRun ? 'Validate export' : 'Export samples'}</Button>
              </Stack>
            </Card>
            <Card p="lg">
              <Text fw={650}>Export contract</Text>
              <Stack mt="md" gap="sm">{[
                ['Stable identity', 'Canonical parameter hashes remain unchanged across shards.'],
                ['Explicit inputs', 'The manifest records the runner spec or sampler/scenario pair used to generate rows.'],
                ['Portable paths', 'Relative-to-output mode produces a relocatable manifest without changing the source.'],
                ['Atomic result', 'A failed export cannot leave a bundle that appears complete.'],
              ].map(([title, description], index) => <Group key={title} align="flex-start" wrap="nowrap"><ThemeIcon size={27} radius="xl" variant="light">{index + 1}</ThemeIcon><div><Text size="sm" fw={600}>{title}</Text><Text size="xs" c="dimmed">{description}</Text></div></Group>)}</Stack>
            </Card>
          </SimpleGrid>
        </Tabs.Panel>

        <Tabs.Panel value="analyze">
          <SimpleGrid cols={{ base: 1, md: 2 }}>
            <Card p="lg">
              <Stack>
                <div><Text fw={650}>Connect results</Text><Text size="sm" c="dimmed">Select the source type explicitly so planned samples and completed outcomes are interpreted correctly.</Text></div>
                <Select label="Source type" value={analysisSourceType} onChange={(value) => setAnalysisSourceType((value ?? 'results_path') as typeof analysisSourceType)} allowDeselect={false} data={[
                  { value: 'results_path', label: 'Completed experiment results' },
                  { value: 'samples_path', label: 'Samples file or export' },
                  { value: 'runner_spec_path', label: 'Runner spec' },
                ]} />
                <TextInput label="Source path" value={analysisSource} onChange={(event) => setAnalysisSource(event.currentTarget.value)} placeholder="/path/to/source" required />
                <TextInput label="Analysis output directory" value={analysisOutput} onChange={(event) => setAnalysisOutput(event.currentTarget.value)} required />
                {analysisSourceType === 'results_path' && <TextInput label="Post-outcome config" description="Optional condition YAML evaluated before analysis." value={outcomeConfig} onChange={(event) => setOutcomeConfig(event.currentTarget.value)} />}
                <Accordion variant="contained">
                  <Accordion.Item value="analysis-options"><Accordion.Control>Analysis options</Accordion.Control><Accordion.Panel><Stack>
                    <TextInput label="Parameters" description="Optional comma-separated subset; empty analyzes all parameters." value={analysisParams} onChange={(event) => setAnalysisParams(event.currentTarget.value)} />
                    <SimpleGrid cols={2}>
                      <TextInput label="Color by" value={analysisColorBy} onChange={(event) => setAnalysisColorBy(event.currentTarget.value)} />
                      <NumberInput label="Histogram bins" value={analysisBins} onChange={setAnalysisBins} min={1} max={1000} error={!binsValid ? 'Enter 1–1,000' : undefined} />
                    </SimpleGrid>
                    {analysisSourceType === 'results_path' && outcomeConfig.trim() && <Select label="Post-outcome mode" value={postOutcomeMode} onChange={(value) => setPostOutcomeMode((value ?? 'overlay') as typeof postOutcomeMode)} allowDeselect={false} data={[{ value: 'overlay', label: 'Overlay existing outcomes' }, { value: 'replace', label: 'Replace outcomes' }]} />}
                  </Stack></Accordion.Panel></Accordion.Item>
                </Accordion>
                <Checkbox label="Replace an existing PISA-owned analysis" checked={overwriteAnalysis} onChange={(event) => setOverwriteAnalysis(event.currentTarget.checked)} />
                {analyze.error && <InlineError error={analyze.error} />}
                <Button leftSection={<IconChartDots size={16} />} loading={analyze.isPending} disabled={!analysisSource.trim() || !analysisOutput.trim() || !binsValid} onClick={() => analyze.mutate()}>Analyze samples</Button>
              </Stack>
            </Card>
            <Card p="lg">
              <Text fw={650}>Post Outcome Lab</Text>
              <Text size="sm" c="dimmed" mt="xs">The generated analysis keeps missing and invalid outcomes structurally distinct across:</Text>
              <Group mt="md" gap="xs">{['Histograms', 'Scatter / hexbin', 'Heatmaps', 'Pair matrix', '3D views', 'Outcome filters', 'CSV export'].map((item) => <Badge variant="light" color="gray" key={item}>{item}</Badge>)}</Group>
              <Alert mt="xl" color="blue" icon={<IconInfoCircle size={17} />} title="Missing is not zero">Incomplete metrics remain structurally missing and are disclosed in every chart and export.</Alert>
            </Card>
          </SimpleGrid>
        </Tabs.Panel>
      </Tabs>
    </>
  );
}
