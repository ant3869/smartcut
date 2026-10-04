import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PASS_DEFS, orderedPasses, runPass, runEnabled } from '../frontend/passes-panel.mjs';
import { allOrdered, validatePassSettings } from '../frontend/passes-panel.mjs';

const state = (over = {}) => ({
  'audio-cleanup': { enabled: true, order: 1, settings: { normalize: true }, status: 'ready', summary: '' },
  'silence': { enabled: true, order: 0, settings: {}, status: 'ready', summary: '' },
  'qc': { enabled: false, order: 2, settings: {}, status: 'unconfigured', summary: '' },
  ...over,
});

test('catalog order is stable and every pass declares kind + timeline flag', () => {
  const ids = PASS_DEFS.map(p => p.id);
  assert.deepEqual(ids, ['ai-edit', 'audio-cleanup', 'silence', 'music-bed', 'beat-cuts',
    'transitions', 'intro-outro', 'captions', 'watermark', 'qc', 'render']);
  for (const p of PASS_DEFS) {
    assert.ok(['timeline', 'render', 'verify', 'export'].includes(p.kind), p.id);
    assert.equal(typeof p.modifies, 'boolean', p.id);
  }
});

test('orderedPasses returns enabled passes in stored order', () => {
  assert.deepEqual(orderedPasses(state()).map(p => p.id), ['silence', 'audio-cleanup']);
});

test('runPass calls the registered runner with stored settings, not a copy', async () => {
  const seen = [];
  const runners = { applyCleanup: async (settings) => { seen.push(settings); return 'norm on'; } };
  const out = await runPass('audio-cleanup', state(), { runners });
  assert.equal(out, 'norm on');
  assert.deepEqual(seen, [{ normalize: true }]);
});

test('runPass throws a clear error when no runner is registered', async () => {
  await assert.rejects(runPass('captions', state(), { runners: {} }), /no runner/i);
});

test('runEnabled stops on first failure when stopOnError', async () => {
  const calls = [];
  const runners = {
    quickSilence: async () => { calls.push('silence'); throw new Error('boom'); },
    applyCleanup: async () => { calls.push('cleanup'); return 'ok'; },
  };
  const report = await runEnabled(state(), { runners, stopOnError: true });
  assert.deepEqual(calls, ['silence']);
  assert.equal(report.stopped, 'silence');
  assert.equal(report.results.silence.error, 'boom');
});

test('runEnabled continues past failures when stopOnError is false', async () => {
  const calls = [];
  const runners = {
    quickSilence: async () => { calls.push('silence'); throw new Error('boom'); },
    applyCleanup: async () => { calls.push('cleanup'); return 'ok'; },
  };
  const report = await runEnabled(state(), { runners, stopOnError: false });
  assert.deepEqual(calls, ['silence', 'cleanup']);
  assert.equal(report.stopped, null);
  assert.equal(report.results['audio-cleanup'].summary, 'ok');
});

test('runEnabled skips disabled passes and reports per-pass status', async () => {
  const calls = [];
  const runners = { quickSilence: async () => { calls.push('silence'); return 'cut 3'; } };
  const report = await runEnabled(state(), { runners, stopOnError: true });
  assert.deepEqual(calls, ['silence']);
  assert.ok(!('qc' in report.results));
});

test('registry reuses the same runner keys as the Auto menu paths', async () => {
  const defs = Object.fromEntries(PASS_DEFS.map(p => [p.id, p]));
  assert.equal(defs.transitions.reuses, 'applyTransitions');
  assert.equal(defs.watermark.reuses, 'applyWatermark');
  const calls = [];
  const runners = {
    applyTransitions: async (s) => { calls.push(['transitions', s]); return 't ok'; },
    applyWatermark: async (s) => { calls.push(['watermark', s]); return 'w ok'; },
  };
  await runPass('transitions', state({ transitions: { enabled: true, order: 3, settings: { type: 'fade' }, status: 'ready', summary: '' } }), { runners });
  assert.deepEqual(calls, [['transitions', { type: 'fade' }]]);
});

test('allOrdered keeps disabled passes visible in stored order', () => {
  const ids = ['silence', 'qc', 'audio-cleanup'];
  const st = state({ qc: { enabled: false, order: 5, settings: {}, status: 'x', summary: '' } });
  assert.deepEqual(allOrdered(ids, st), ['silence', 'audio-cleanup', 'qc']);
});

test('runEnabled skips planned passes without runners', async () => {
  const calls = [];
  const st = state({
    'audio-cleanup': { enabled: false, order: 9, settings: {}, status: 'ready', summary: '' },
    captions: { enabled: true, order: 0, settings: {}, status: 'ready', summary: '' },
    silence: { enabled: true, order: 1, settings: {}, status: 'ready', summary: '' },
  });
  const runners = { quickSilence: async () => { calls.push('silence'); return 'cut 3'; } };
  const report = await runEnabled(st, { runners, stopOnError: true });
  assert.deepEqual(calls, ['silence']);
  assert.equal(report.stopped, null);
  assert.equal(report.results.captions.skipped, true);
});

test('validatePassSettings requires media before destructive runs', () => {
  assert.match(validatePassSettings('intro-outro', { preset: 'Nexco Standard' }), /intro or outro file/i);
  assert.equal(validatePassSettings('intro-outro', { intro_path: 'a.mp4' }), null);
  assert.match(validatePassSettings('music-bed', {}), /music file/i);
  assert.match(validatePassSettings('beat-cuts', {}), /audio source/i);
  assert.match(validatePassSettings('watermark', {}), /watermark image/i);
  assert.equal(validatePassSettings('qc', {}), null);
});

test('runPass refuses an unconfigured intro/outro instead of wiping sides', async () => {
  const st = state({ 'intro-outro': { enabled: true, order: 0, settings: { preset: 'Nexco Standard' }, status: 'ready', summary: '' } });
  await assert.rejects(runPass('intro-outro', st, { runners: { applyIntroOutro: async () => 'never' } }), /intro or outro file/i);
});

test('runEnabled honors qc block=false and keeps going', async () => {
  const calls = [];
  const st = state({
    'audio-cleanup': { enabled: false, order: 9, settings: {}, status: 'x', summary: '' },
    qc: { enabled: true, order: 0, settings: { block: false }, status: 'ready', summary: '' },
    silence: { enabled: true, order: 1, settings: {}, status: 'ready', summary: '' },
  });
  const runners = {
    runQc: async () => { calls.push('qc'); return '2 errors · 1 warnings'; },
    quickSilence: async () => { calls.push('silence'); return 'cut 3'; },
  };
  const report = await runEnabled(st, { runners, stopOnError: true });
  assert.deepEqual(calls, ['qc', 'silence']);
  assert.equal(report.stopped, null);
});

test('runEnabled stops on qc errors when block is set', async () => {
  const calls = [];
  const st = state({
    'audio-cleanup': { enabled: false, order: 9, settings: {}, status: 'x', summary: '' },
    qc: { enabled: true, order: 0, settings: { block: true }, status: 'ready', summary: '' },
    silence: { enabled: true, order: 1, settings: {}, status: 'ready', summary: '' },
  });
  const runners = {
    runQc: async () => { calls.push('qc'); return '2 errors · 1 warnings'; },
    quickSilence: async () => { calls.push('silence'); return 'cut 3'; },
  };
  const report = await runEnabled(st, { runners, stopOnError: true });
  assert.deepEqual(calls, ['qc']);
  assert.equal(report.stopped, 'qc');
});

test('validatePassSettings requires durations for still intro/outro', () => {
  assert.match(validatePassSettings('intro-outro', { intro_path: 'logo.png' }), /duration in seconds/i);
  assert.equal(validatePassSettings('intro-outro', { intro_path: 'logo.png', intro_duration: 3 }), null);
  assert.equal(validatePassSettings('intro-outro', { intro_path: 'clip.mp4' }), null);
});

test('runEnabled stops after terminal render and skips later passes', async () => {
  const calls = [];
  const st = state({
    'audio-cleanup': { enabled: false, order: 9, settings: {}, status: 'x', summary: '' },
    render: { enabled: true, order: 0, settings: {}, status: 'ready', summary: '' },
    silence: { enabled: true, order: 1, settings: {}, status: 'ready', summary: '' },
  });
  const runners = {
    openRender: async () => { calls.push('render'); return 'dialog opened'; },
    quickSilence: async () => { calls.push('silence'); return 'cut 3'; },
  };
  const report = await runEnabled(st, { runners, stopOnError: true });
  assert.deepEqual(calls, ['render']);
  assert.equal(report.terminated, 'render');
  assert.equal(report.results.silence.skipped, true);
});

test('runEnabled skips analysis-gated passes without a job', async () => {
  const calls = [];
  const st = state({
    'audio-cleanup': { enabled: false, order: 9, settings: {}, status: 'x', summary: '' },
    'ai-edit': { enabled: true, order: 0, settings: {}, status: 'ready', summary: '' },
    silence: { enabled: true, order: 1, settings: {}, status: 'ready', summary: '' },
  });
  const runners = {
    quickWaste: async () => { calls.push('waste'); return 'waste cut'; },
    quickSilence: async () => { calls.push('silence'); return 'cut 3'; },
  };
  const skipped = await runEnabled(st, { runners, stopOnError: true, job: null });
  assert.deepEqual(calls, ['silence']);
  assert.equal(skipped.results['ai-edit'].skipped, true);
  assert.equal(skipped.stopped, null);
  const ran = await runEnabled(st, { runners, stopOnError: true, job: { id: 'j1' } });
  assert.deepEqual(calls, ['silence', 'waste', 'silence']);
});

test('epochMismatch only fails on intervening edits', async () => {
  const { epochMismatch } = await import('../frontend/passes-panel.mjs');
  assert.equal(epochMismatch(null, 5), null);
  assert.equal(epochMismatch(5, 5), null);
  assert.match(epochMismatch(5, 6), /discarded/);
});

test('runEnabled rebases the epoch after each editing pass', async () => {
  const seen = [];
  let version = 0;
  const st = state({
    'audio-cleanup': { enabled: true, order: 0, settings: { normalize: true }, status: 'ready', summary: '' },
    silence: { enabled: true, order: 1, settings: {}, status: 'ready', summary: '' },
  });
  const runners = {
    applyCleanup: async (s, ctx) => { seen.push(ctx.epoch); version += 1; return 'saved'; },
    quickSilence: async (s, ctx) => { seen.push(ctx.epoch); version += 1; return 'cut 1'; },
  };
  const report = await runEnabled(st, { runners, stopOnError: true, epoch: 0, version: () => version });
  assert.deepEqual(seen, [0, 1]);
  assert.equal(report.stopped, null);
});
