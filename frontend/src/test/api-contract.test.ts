import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient } from '../api/client';
import type { RepairPlan } from '../api/types';

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function bodyOf(call: unknown[]) {
  return JSON.parse(String((call[1] as RequestInit | undefined)?.body)) as Record<string, unknown>;
}

afterEach(() => vi.restoreAllMocks());

describe('API contract adapters', () => {
  it('maps normalized report catalog, overview, runs, comparisons, charts, and media', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(json({ items: [{
        id: 'abc123', name: 'pisa-exp', path: '/reports/pisa-exp', run_count: 23596,
        experiment_count: 20, generated_at: '2026-07-14T00:00:00Z', has_index: true,
      }] }))
      .mockResolvedValueOnce(json({
        id: 'abc123', run_count: 23596, success: 15895, fail: 5459, invalid: 2237,
        unknown: 5, collision: 3841,
        data: { dataset_count: 20, findings: [{ code: 'duplicate_alias', severity: 'warning', message: 'Alias excluded from aggregate' }] },
      }))
      .mockResolvedValueOnce(json({
        items: [{ run_id: 'demo:iteration_2', dataset_id: 'demo', scenario_id: 'iteration_2', outcome_class: 'fail', stop_reason: 'collision', metrics: { min_ttc: '0.4' }, params: { speed: 20 }, has_collision: true }],
        total: 1,
        next_cursor: null,
      }))
      .mockResolvedValueOnce(json({
        items: [{ id: 'pair-1', left_dataset_id: 'base', right_dataset_id: 'candidate', role: 'paired_system_intervention', matched_count: 95, left_only_count: 5, right_only_count: 7, outcome_agreement: 0.91 }],
        cross_experiment: {
          available: true, experiments: ['base', 'candidate'], experiment_count: 2,
          common_sample_count: 95, union_sample_count: 107, excluded_noncommon_sample_count: 12,
          excluded_duplicate_aliases: [], hash_quality: {},
          discrete: [{ key: 'outcome', label: 'Outcome', consistent_count: 86, comparable_count: 95, agreement_ratio: 86 / 95, unavailable_sample_count: 0 }],
          continuous: [{ key: 'min_ttc', label: 'Minimum TTC', unit: 's', eligible_sample_count: 90, partial_sample_count: 5, unavailable_sample_count: 0, valid_execution_count: 185, total_execution_count: 190, missing_execution_count: 5, invalid_execution_count: 0, variation_max: 1.2, variation_p95: 0.8, variation_std: 0.1, variation_median: 0.15, validity_rule: 'finite non-negative values in every experiment' }],
        },
      }))
      .mockResolvedValueOnce(json({ visualizations: [{ id: 'outcomes', title: 'Outcome composition', kind: 'bar', option: { series: [] } }] }))
      .mockResolvedValueOnce(json({ items: [{ id: 'movie', path: 'media/replay.webm', name: 'replay.webm', format: 'webm', media_type: 'video/webm', download_url: '/api/v1/reports/abc123/artifacts/media/replay.webm' }] }));
    const api = new ApiClient('/api/v1');

    const catalog = await api.datasets.list('pisa');
    expect(catalog.items[0]).toMatchObject({ status: 'ready', experiment_count: 20, run_count: 23596 });

    const summary = await api.datasets.summary('abc123');
    expect(summary).toMatchObject({
      experiment_count: 20,
      run_count: 23596,
      outcomes: { success: 15895, fail: 5459, invalid: 2237, unknown: 5 },
      collision_count: 3841,
    });
    expect(summary.health?.[0]).toMatchObject({ code: 'duplicate_alias', detail: 'Alias excluded from aggregate' });

    const runs = await api.datasets.runs('abc123', { search: 'iteration_2', outcome: 'fail', sort: 'scenario_id' });
    expect(runs.items[0]).toMatchObject({ id: 'demo:iteration_2', iteration: 2, outcome: 'fail', min_ttc: 0.4, collision: true });
    expect(String(fetch.mock.calls[2][0])).toContain('query=iteration_2');
    expect(String(fetch.mock.calls[2][0])).not.toContain('search=');

    const comparisons = await api.datasets.comparisons('abc123');
    expect(comparisons.items[0]).toMatchObject({ left: 'base', right: 'candidate', matched: 95, agreement: 0.91 });
    expect(comparisons.cross_experiment).toMatchObject({ common_sample_count: 95, continuous: [{ key: 'min_ttc', eligible_sample_count: 90 }] });
    expect((await api.datasets.charts('abc123', 'outcomes'))[0]).toMatchObject({ id: 'outcomes', kind: 'bar' });
    expect((await api.datasets.media('abc123')).items[0]).toMatchObject({ kind: 'video', source: 'derived', mime_type: 'video/webm' });
  });

  it('submits complete report validation, build, and legacy rebuild contracts', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(json({ valid: true, run_count: 100 }))
      .mockResolvedValueOnce(json({
        id: 'job-1', kind: 'report_build', status: 'queued', phase: 'queued',
        progress: { current: null, total: null, unit: null }, created_at: '2026-07-14T00:00:00Z',
      }))
      .mockResolvedValueOnce(json({ id: 'job-2', kind: 'report_rebuild', status: 'queued', phase: 'queued', created_at: '2026-07-14T00:00:00Z' }))
      .mockResolvedValueOnce(json({ id: 'job-3', kind: 'report_preview', status: 'queued', phase: 'queued', created_at: '2026-07-14T00:00:00Z' }))
      .mockResolvedValueOnce(json({ id: 'job-4', kind: 'report_persist', status: 'queued', phase: 'queued', created_at: '2026-07-14T00:00:00Z' }))
      .mockResolvedValueOnce(json({ id: 'temporary', name: 'Preview', path: '/tmp/preview', storage_kind: 'temporary', expires_at: '2026-07-14T00:10:00Z' }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    const api = new ApiClient('/api/v1');

    await api.datasets.validate({ path: '/opt/sbsvf/outputs/pisa-exp/', deep: true });
    expect(bodyOf(fetch.mock.calls[0])).toEqual({ path: '/opt/sbsvf/outputs/pisa-exp/', deep: true });

    const request = {
      results_paths: ['/opt/sbsvf/outputs/pisa-exp/'], output_dir: './analysis/PISA-paper-report',
      spec_path: './analysis-spec.yaml', overwrite: true, validation_mode: 'permissive' as const,
      deep_validation: true, report_mode: 'static' as const, sensitivity: true,
      engine: 'legacy' as const,
    };
    const job = await api.datasets.build(request);
    expect(job).toMatchObject({ id: 'job-1', state: 'queued', kind: 'report_build' });
    expect(bodyOf(fetch.mock.calls[1])).toEqual(request);

    await api.datasets.rebuild('legacy-report', { output_dir: './analysis/rebuilt', sensitivity: true });
    expect(String(fetch.mock.calls[2][0])).toContain('/reports/legacy-report/rebuild');
    expect(bodyOf(fetch.mock.calls[2])).toEqual({ output_dir: './analysis/rebuilt', sensitivity: true });

    await api.datasets.previewBuild({ results_paths: ['/opt/sbsvf/outputs/pisa-exp/'], report_name: 'Quick look', engine: 'normalized' });
    expect(String(fetch.mock.calls[3][0])).toContain('/reports/previews');
    expect(bodyOf(fetch.mock.calls[3])).not.toHaveProperty('output_dir');
    await api.datasets.persist('temporary', { output_dir: './analysis/Quick-look' });
    expect(bodyOf(fetch.mock.calls[4])).toEqual({ output_dir: './analysis/Quick-look' });
    expect(await api.datasets.lease('temporary')).toMatchObject({ storage_kind: 'temporary', name: 'Preview' });
    await api.datasets.discardPreview('temporary');
    expect((fetch.mock.calls[6][1] as RequestInit).method).toBe('DELETE');
  });

  it('submits paired parameter analysis without deriving input fields', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({
      schema_version: 1, relation_id: 'pair-1', left: 'a', right: 'b', role: 'paired_policy_intervention',
      pairing_key: 'parameter_hash unique within each dataset', parameters: ['Ego_Speed', 'Agent_Speed'], metrics: [],
      selection: { x: 'Ego_Speed', y: 'Agent_Speed', facet: null, view: 'outcome', metric: null, delta_definition: 'right minus left', bin_count: 5, boundaries: {}, minimum_cell_count: 10 },
      overview: { paired_count: 100, agreement_count: 80, disagreement_count: 20, disagreement_rate: 0.2, direct_reversal_count: 18, invalid_related_count: 2, categories: {}, transitions: {}, metric_eligible_count: 0, metric_missing_count: 0 },
      marginals: [], heatmaps: [], observations: [], candidates: [], points: [],
      coverage: { paired_count: 100, complete_parameter_count: 100, included_count: 100, excluded_incomplete_parameters: 0, excluded_by_boundaries: 0, excluded_by_facet: 0, plotted_count: 100, point_limit: 20000, sampled: false },
      disclosure: { input_scope: 'recorded original parameters only', derived_parameters_used: false },
    }));
    const api = new ApiClient('/api/v1');

    const result = await api.datasets.pairedParameterAnalysis('report-1', 'pair-1', {
      x: 'Ego_Speed', y: 'Agent_Speed', bin_count: 5,
      boundaries: { Ego_Speed: [10, 15, 20] },
    });

    expect(result.overview).toMatchObject({ disagreement_count: 20, paired_count: 100 });
    expect(String(fetch.mock.calls[0][0])).toContain('/comparisons/pair-1/parameter-analysis');
    expect(bodyOf(fetch.mock.calls[0])).toEqual({ x: 'Ego_Speed', y: 'Agent_Speed', bin_count: 5, boundaries: { Ego_Speed: [10, 15, 20] } });
  });

  it('submits paired metric agreement with explicit orientation and boundaries', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({
      schema_version: 1, relation_id: 'pair-1', left: 'autoware', right: 'plant', role: 'paired_policy_intervention',
      pairing_key: 'parameter_hash unique within each dataset', metrics: [{ key: 'distance.min', label: 'distance min', unit: 'm' }],
      selection: { metric: 'distance.min', unit: 'm', x_side: 'right', x_dataset: 'plant', y_dataset: 'autoware', outcome_scope: 'success', primary_threshold: 5, secondary_threshold: 10, difference_definition: 'y minus x' },
      summary: { paired_count: 1000, metric_eligible_count: 1000, metric_missing_count: 0, same_outcome_metric_eligible_count: 761, outcome_disagreement_metric_eligible_count: 239, included: { count: 652, thresholds: [] }, categories: {} },
      points: [], coverage: { included_count: 652, plotted_count: 652, point_limit: 20000, sampled: false }, disclosure: {},
    }));
    const api = new ApiClient('/api/v1');
    const request = { metric: 'distance.min', x_side: 'right' as const, outcome_scope: 'success' as const, primary_threshold: 5, secondary_threshold: 10 };

    const result = await api.datasets.pairedMetricAgreement('report-1', 'pair-1', request);

    expect(result.selection).toMatchObject({ x_dataset: 'plant', y_dataset: 'autoware' });
    expect(String(fetch.mock.calls[0][0])).toContain('/comparisons/pair-1/metric-agreement');
    expect(bodyOf(fetch.mock.calls[0])).toEqual(request);
  });

  it('normalizes quick/deep consistency and submits an explicit optional profile', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(json({
        quick: {
          schema_version: 1, available: true, dataset_count: 2, canonical_dataset_count: 2,
          group_count: 1, excluded_duplicate_aliases: [],
          groups: [{
            id: 'g1', datasets: ['a', 'b'], experiment_count: 2, common_sample_count: 10,
            union_sample_count: 10, excluded_noncommon_sample_count: 0,
            information_consistent_count: 8, information_comparable_count: 10,
            information_agreement_ratio: 0.8,
            discrete: [{ key: 'outcome', label: 'Outcome', consistent_count: 9, comparable_count: 10, agreement_ratio: 0.9, unavailable_sample_count: 0 }],
            continuous: [], runtime: [], outcome_patterns: [], pairwise: [], hash_quality: {},
          }],
        },
        deep: {
          state: 'ready', cache_key: 'cache', artifacts: [{ path: 'consistency/derived/cache/summary.json', download_url: '/summary.json' }],
          summary: { generated_at: '2026-07-18T00:00:00Z', profile: 'trajectory_outlier_controls', sample_count: 10, position_tolerances_m: [0.01], groups: [{ id: 'g1', datasets: ['a', 'b'], sample_count: 10, trajectory_comparable_count: 9, outcome_agreement_count: 9, strict_exact_count: 8, lengths_equal_count: 9, position_tolerance_counts: { '0.01': 9 }, max_position_error_m: { p95: 0.005, max: 0.02 } }] },
        },
      }))
      .mockResolvedValueOnce(json({ id: 'consistency-job', kind: 'consistency_analysis', status: 'queued', phase: 'queued', created_at: '2026-07-18T00:00:00Z' }));
    const api = new ApiClient('/api/v1');

    const result = await api.datasets.consistency('report-1');
    expect(result.quick.groups[0]).toMatchObject({ common_sample_count: 10, information_agreement_ratio: 0.8 });
    expect(result.deep.summary?.groups[0]).toMatchObject({ trajectory_comparable_count: 9, strict_exact_count: 8 });
    expect(result.deep.artifacts?.[0]).toEqual({ path: 'consistency/derived/cache/summary.json', download_url: '/summary.json' });

    const request = { profile: 'full_controls' as const, position_tolerances_m: [0.001, 0.01, 0.1], outlier_limit: 50, force: true };
    expect(await api.datasets.analyzeConsistency('report-1', request)).toMatchObject({ id: 'consistency-job', state: 'queued' });
    expect(bodyOf(fetch.mock.calls[1])).toEqual(request);
  });

  it('previews an existing native sampler without rewriting it as an inline request', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({
      source_file: '/data/scenario.xosc', source_type: 'openscenario', sampler_name: 'native',
      total_samples: 4, generated_samples: 2,
      samples: [{ index: 1, params: { speed: 10 } }, { index: 2, params: { speed: 20 } }],
    }));
    const api = new ApiClient('/api/v1');
    const request = {
      source_file: '/data/scenario.xosc', sampler_name: 'native', source_type: 'openscenario',
      config_path: '/data/native.yaml', config: { mode: 'fast' }, max_samples: 2,
    };

    expect(await api.samples.preview(request)).toMatchObject({ method: 'native', count: 2, parameter_names: ['speed'] });
    expect(bodyOf(fetch.mock.calls[0])).toEqual(request);
  });

  it('uses exact sampler, export, and analysis request shapes', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(json({ method: 'lhs', count: 2, parameter_names: ['speed'], samples: [[10], [20]], warnings: [] }))
      .mockResolvedValueOnce(json({ id: 'export-1', kind: 'sample_export', status: 'queued', phase: 'queued', created_at: '2026-07-14T00:00:00Z' }))
      .mockResolvedValueOnce(json({ id: 'analyze-1', kind: 'sample_analyze', status: 'queued', phase: 'queued', created_at: '2026-07-14T00:00:00Z' }));
    const api = new ApiClient('/api/v1');

    const previewRequest = { method: 'lhs' as const, count: 2, seed: 42, parameters: [{ name: 'speed', min: 10, max: 20 }] };
    expect(await api.samples.preview(previewRequest)).toMatchObject({ count: 2, parameter_names: ['speed'], samples: [[10], [20]] });
    expect(bodyOf(fetch.mock.calls[0])).toEqual(previewRequest);

    await api.samples.export({
      output_dir: './samples', sampler_spec_path: './sampler.yaml', scenario_path: './scenario',
      num_shards: 4, source_path_mode: 'relative-to-output', create_zip: true,
      dry_run: false, overwrite: false,
    });
    expect(bodyOf(fetch.mock.calls[1])).toEqual({
      output_dir: './samples', sampler_spec_path: './sampler.yaml', scenario_path: './scenario',
      num_shards: 4, source_path_mode: 'relative-to-output', create_zip: true,
      dry_run: false, overwrite: false,
    });

    await api.samples.analyze({ output_dir: './analysis', results_path: './results', post_outcome_config_path: './outcome.yaml', post_outcome_mode: 'overlay' });
    expect(bodyOf(fetch.mock.calls[2])).toEqual({ output_dir: './analysis', results_path: './results', post_outcome_config_path: './outcome.yaml', post_outcome_mode: 'overlay' });
  });

  it('adapts runner registry data and only cleans explicitly listed owned resources', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(json({
        items: [{ id: 'cutin', label: 'Cut-in', scenario: { name: 'cutin-opt' }, simulator: { component: 'esmini' }, av: { component: 'simple' }, sampler: { name: 'lhs' } }],
        components: { esmini: { kind: 'simulator', label: 'esmini' }, simple: { kind: 'av', label: 'Simple AV' } },
      }))
      .mockResolvedValueOnce(json({ preset_id: 'paper-cutin', experiment: { label: 'Paper cut-in', scenario: { name: 'cutin-opt' }, simulator: { component: 'esmini' }, av: { component: 'simple' } } }))
      .mockResolvedValueOnce(json({ id: 'runner-job', experiment_id: 'paper-cutin', label: 'Paper cut-in', action: 'run_all', status: 'queued', phase: 'queued', created_at: 1783987200 }))
      .mockResolvedValueOnce(json({ containers: [{ id: 'c1', name: 'pisa-sim', status: 'Up 10 minutes' }] }))
      .mockResolvedValueOnce(json({ stopped: 'pisa-sim' }));
    const api = new ApiClient('/api/v1');

    const presets = await api.experiments.presets();
    expect(presets.items[0]).toMatchObject({ id: 'cutin', scenario: 'cutin-opt', simulator: 'esmini', automation: 'simple' });
    expect(presets.components).toHaveLength(2);

    await api.experiments.savePreset({ preset_id: 'paper-cutin', template_id: 'cutin', label: 'Paper cut-in', simulator_component: 'esmini', av_component: 'simple' });
    expect(bodyOf(fetch.mock.calls[1])).toEqual({ preset_id: 'paper-cutin', template_id: 'cutin', label: 'Paper cut-in', simulator_component: 'esmini', av_component: 'simple' });

    const job = await api.experiments.run({ experiment_id: 'paper-cutin', action: 'run_all', overrides: { analysis: { auto: true } } });
    expect(job).toMatchObject({ id: 'runner:runner-job', source: 'runner', state: 'queued' });
    expect(bodyOf(fetch.mock.calls[2])).toEqual({ experiment_id: 'paper-cutin', action: 'run_all', overrides: { analysis: { auto: true } } });

    const resources = await api.experiments.resources();
    expect(resources[0]).toMatchObject({ id: 'c1', type: 'container', name: 'pisa-sim', state: 'Up 10 minutes' });
    await api.experiments.cleanup(resources.map((resource) => resource.name));
    expect(bodyOf(fetch.mock.calls[4])).toEqual({ name: 'pisa-sim' });
  });

  it('updates, renames, deletes, and resumes runner-owned records explicitly', async () => {
    const experiment = { label: 'Candidate', tags: ['paper'], simulator: { component: 'esmini' }, av: { component: 'simple' } };
    const fetch = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(json({ preset_id: 'candidate', experiment }))
      .mockResolvedValueOnce(json({ preset_id: 'candidate-v2', experiment: { ...experiment, label: 'Candidate v2' } }))
      .mockResolvedValueOnce(json({ deleted: 'candidate-v2' }))
      .mockResolvedValueOnce(json({ id: 'runner-job', experiment_id: 'candidate', action: 'report', status: 'queued', phase: 'queued', created_at: 1783987200 }));
    const api = new ApiClient('/api/v1');

    expect(await api.experiments.updatePreset('candidate', experiment)).toMatchObject({ id: 'candidate', name: 'Candidate' });
    expect(String(fetch.mock.calls[0][0])).toContain('/runner/presets/candidate');
    expect(bodyOf(fetch.mock.calls[0])).toEqual({ experiment });

    expect(await api.experiments.renamePreset('candidate', { new_id: 'candidate-v2', label: 'Candidate v2' })).toMatchObject({ id: 'candidate-v2', name: 'Candidate v2' });
    expect(bodyOf(fetch.mock.calls[1])).toEqual({ new_id: 'candidate-v2', label: 'Candidate v2' });

    await api.experiments.deletePreset('candidate-v2');
    expect(bodyOf(fetch.mock.calls[2])).toEqual({ confirm: true });

    expect(await api.experiments.resume('runner:runner-job', 'report')).toMatchObject({ id: 'runner:runner-job', state: 'queued' });
    expect(String(fetch.mock.calls[3][0])).toContain('/runner/jobs/runner-job/resume');
    expect(bodyOf(fetch.mock.calls[3])).toEqual({ action: 'report' });
  });

  it('sends required tool outputs and round-trips the complete signed repair plan', async () => {
    const plan: RepairPlan = {
      version: 1,
      signature: 'signed-plan',
      source_path: '/data/results',
      mode: 'source',
      output_path: null,
      init_state_path: '/data/initial.yaml',
      reference_root: null,
      backup_suffix: '.bak',
      time_step_ms: null,
      findings: [],
      changes: [{ path: 'iteration_1/monitor/agent_states.csv', sha256: 'abc', original_rows: 10, inserted_rows: 2, result_rows: 12 }],
      destructive: true,
    };
    const fetch = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(json({ id: 'trajectory', kind: 'trajectory', status: 'queued', phase: 'queued', created_at: '2026-07-14T00:00:00Z' }))
      .mockResolvedValueOnce(json({ id: 'compare', kind: 'trajectory_compare', status: 'queued', phase: 'queued', created_at: '2026-07-14T00:00:00Z' }))
      .mockResolvedValueOnce(json({ id: 'outcome', kind: 'outcome_eval', status: 'queued', phase: 'queued', created_at: '2026-07-14T00:00:00Z' }))
      .mockResolvedValueOnce(json(plan))
      .mockResolvedValueOnce(json({ id: 'repair', kind: 'repair_apply', status: 'queued', phase: 'queued', created_at: '2026-07-14T00:00:00Z' }));
    const api = new ApiClient('/api/v1');

    await api.tools.trajectory({ input_path: './run', output_dir: './trajectory', width: 1600, height: 900, x_range: [-20, 80], y_range: [-10, 10], equal_scale: false, ignore_agent_ids: ['3'], origin_agent_id: '0', overwrite: true });
    await api.tools.compareTrajectory({ left_path: './left', right_path: './right', output_dir: './comparison', left_label: 'Baseline', right_label: 'Candidate', width: 1800, height: 1000, equal_scale: true, ignore_agent_ids: ['7'], overwrite: true });
    await api.tools.evaluateOutcome({ input_path: './results', config_path: './condition.yaml', output_dir: './outcomes', mode: 'replace', default_outcome: 'invalid', write_monitor_outcome: true, overwrite: true });
    expect(bodyOf(fetch.mock.calls[0])).toEqual({ input_path: './run', output_dir: './trajectory', width: 1600, height: 900, x_range: [-20, 80], y_range: [-10, 10], equal_scale: false, ignore_agent_ids: ['3'], origin_agent_id: '0', overwrite: true });
    expect(bodyOf(fetch.mock.calls[1])).toEqual({ left_path: './left', right_path: './right', output_dir: './comparison', left_label: 'Baseline', right_label: 'Candidate', width: 1800, height: 1000, equal_scale: true, ignore_agent_ids: ['7'], overwrite: true });
    expect(bodyOf(fetch.mock.calls[2])).toEqual({ input_path: './results', config_path: './condition.yaml', output_dir: './outcomes', mode: 'replace', default_outcome: 'invalid', write_monitor_outcome: true, overwrite: true });

    const scanned = await api.tools.scanRepair({ source_path: './results', init_state_path: './initial.yaml', mode: 'source' });
    expect(scanned.signature).toBe('signed-plan');
    await api.tools.applyRepair({ plan: scanned, confirm_path: scanned.source_path });
    expect(bodyOf(fetch.mock.calls[4])).toEqual({ plan, confirm_path: '/data/results' });
  });

  it('merges workbench and runner jobs while routing cancellation to the owner', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith('/jobs') && !url.includes('/runner/')) return json({ items: [{ id: 'work-1', kind: 'report_build', status: 'running', phase: 'indexing', progress: { current: 5, total: 10, unit: 'datasets' }, created_at: '2026-07-14T00:00:00Z' }] });
      if (url.endsWith('/runner/jobs')) return json({ items: [{ id: 'runner-1', label: 'Cut-in', action: 'run_all', status: 'report_ready', phase: 'report_ready', created_at: 1783987200 }] });
      if (url.endsWith('/runner/jobs/runner-1/cancel') && init?.method === 'POST') return json({ id: 'runner-1', label: 'Cut-in', action: 'run_all', status: 'cancelled', phase: 'cancelled', created_at: 1783987200 });
      return json({ code: 'not_found', message: 'not found' }, 404);
    });
    const api = new ApiClient('/api/v1');

    const jobs = await api.jobs.list();
    expect(jobs.items).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'work-1', state: 'running', source: 'workbench' }),
      expect.objectContaining({ id: 'runner:runner-1', state: 'succeeded', source: 'runner' }),
    ]));
    expect(await api.jobs.cancel('runner:runner-1')).toMatchObject({ id: 'runner:runner-1', state: 'cancelled' });
    expect(String(fetch.mock.calls.at(-1)?.[0])).toContain('/runner/jobs/runner-1/cancel');
  });
});
