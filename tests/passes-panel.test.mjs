import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PASS_DEFS, orderedPasses, runPass, runEnabled } from '../frontend/passes-panel.mjs';

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
  // The panel must orchestrate, not duplicate: transitions/watermark entries
  // name the exact implementation they drive, and runPass dispatches through it.
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
