import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

// Functional regression coverage for timelineSection() + timelineDecisionsBrowser()
// in frontend/app.js: executes the real functions with DOM stubs against
// self-contained inline fixtures (no host paths, no fixture server).
// Covers: real totals, KEEP-by-default, judged failed frames, hidden archives,
// failed-only analyses, All-CUT gating, and missing score/confidence.
const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const src = readFileSync(path.join(root, 'frontend', 'app.js'), 'utf8');

function loadFns(analysis) {
  const start = src.indexOf('function timelineSection(){');
  const end = src.indexOf('function timelineDecisionCount');
  const fns = src.slice(start, end);
  const state = { timelineAnalysis: analysis, timelineFilter: 'all', tab: 'review' };
  const tc = (t) => `tc(${t})`;
  const esc = (s) => String(s);
  const short = (t) => `short(${t})`;
  const basename = (p) => String(p).split(/[\\/]/).pop();
  const badge = (t, k) => `[${k}:${t}]`;
  const icon = (n) => '';
  const button = (action, label, cls = '', extra = '') =>
    `<button data-action="${action}" class="${cls}" ${extra}>${label}</button>`;
  const factory = new Function('state', 'tc', 'esc', 'short', 'basename', 'badge', 'icon', 'button',
    `${fns}; return { timelineSection, timelineDecisionsBrowser };`);
  return { ...factory(state, tc, esc, short, basename, badge, icon, button), state };
}

const obs = (timestamp, extra = {}) => ({
  timestamp, span: [timestamp, timestamp + 2], score: 2, confidence: 0.9,
  cull_reason: 'blank_or_obstructed', description: 'dark frame',
  source: 'GX010003.MP4', source_timestamp: 720 + timestamp, clip_id: 'clip1',
  ...extra,
});
const prov = (timeline) => ({
  timeline, source: 'GX010003.MP4', source_timestamp: 720 + timeline, clip_id: 'clip1',
});

// All-CUT analysis with one unjudged sample and one failed frame.
const ALL_CUT = {
  scope: { mode: 'entire' }, sequence_revision: 1, settings: { sample_every_s: 2 },
  sample_count: 4, judged_count: 3,
  observations: [obs(0, { keep: false }), obs(2, { keep: false }), obs(4, {})],
  provenance: [prov(0), prov(2), prov(4)],
  review_intervals: [], eye_failed_frames: [{ timestamp: 6, error: 'budget hit' }],
  decisions: {}, archived_decisions: [], stale: false,
};

// Mixed verdicts driving two findings, a kept decision, and history.
const MIXED = {
  ...ALL_CUT,
  observations: [obs(0, { keep: true }), obs(2, { keep: false }), obs(4, { keep: false })],
  review_intervals: [{ start: 2, end: 6, reasons: ['vision-review: setup'] }, { start: 8, end: 10, reasons: ['x'] }],
  eye_failed_frames: [],
  decisions: { 0: { action: 'keep', at: '2026-10-22' } },
  archived_decisions: [{ interval: { start: 0, end: 2, reasons: ['old'] },
    action: 'accept', at: '2026-10-21', reason: 'finding-changed' }],
};

// Nothing judged: only unresolved frames.
const FAILED_ONLY = {
  scope: { mode: 'entire' }, sequence_revision: 1, settings: {},
  sample_count: 2, judged_count: 0, observations: [],
  provenance: [prov(0), prov(2)],
  review_intervals: [],
  eye_failed_frames: [
    { timestamp: 0, error: 'budget hit at 0.0s' },
    { timestamp: 2, error: 'budget hit at 2.0s' },
  ],
  decisions: {}, archived_decisions: [], stale: false,
};

test('all-CUT analysis: real total, per-filter behavior, no KEEP-by-default', () => {
  const { timelineSection, state } = loadFns(ALL_CUT);
  const html = timelineSection();
  assert.ok(html.includes('All (3)'), 'All filter shows the real total');
  assert.ok(!html.includes('All (undefined)'), 'no undefined count');
  assert.ok(!html.includes('All CUT'), 'no All-CUT banner while a sample is unjudged');
  assert.ok(html.includes('no cuts have been applied'), 'recommendations-only notice');
  state.timelineFilter = 'failed';
  const failed = timelineSection();
  assert.ok(failed.includes('UNRESOLVED'), 'failed filter shows real unresolved record');
  assert.ok(!/CUT · tc\(/.test(failed), 'failed view carries no CUT classification');
  state.timelineFilter = 'all';
  const all = timelineSection();
  assert.ok(all.includes('UNJUDGED'), 'missing verdict labeled UNJUDGED, never KEEP');
  assert.ok(!all.includes('>KEEP ·'), 'no KEEP verdict rows in this fixture');
});

test('All CUT appears only when every sample is truly CUT', () => {
  const pure = { ...ALL_CUT, observations: [obs(0, { keep: false }), obs(2, { keep: false })],
    eye_failed_frames: [] };
  assert.ok(loadFns(pure).timelineSection().includes('All CUT'), 'banner for unanimous CUT');
  assert.ok(!loadFns(ALL_CUT).timelineSection().includes('All CUT'), 'no banner with unjudged present');
  const withKeep = { ...pure, observations: [obs(0, { keep: false }), obs(2, { keep: true })] };
  assert.ok(!loadFns(withKeep).timelineSection().includes('All CUT'), 'no banner with a KEEP present');
});

test('All CUT requires zero failed frames', () => {
  const allCut = { ...ALL_CUT, observations: [obs(0, { keep: false }), obs(2, { keep: false })],
    eye_failed_frames: [] };
  assert.ok(loadFns(allCut).timelineSection().includes('All CUT'), 'banner with zero failed');
  const oneFailed = { ...allCut, eye_failed_frames: [{ timestamp: 4, error: 'budget hit' }] };
  assert.ok(!loadFns(oneFailed).timelineSection().includes('All CUT'), 'no banner with one failed');
  assert.ok(loadFns(oneFailed).timelineSection().includes('1 unresolved'), 'failed still counted');
});

test('missing score/confidence are never invented', () => {
  const thin = { ...ALL_CUT, observations: [obs(0, { keep: false, score: undefined, confidence: undefined })] };
  delete thin.observations[0].score;
  delete thin.observations[0].confidence;
  const html = loadFns(thin).timelineSection();
  assert.ok(html.includes('score n/a'), 'missing score shown as n/a');
  assert.ok(html.includes('confidence n/a'), 'missing confidence shown as n/a');
  assert.ok(!html.includes('100%'), 'no invented 100% confidence');
  assert.ok(!html.includes('undefined/10'), 'no undefined score leak');
});

test('failed-only analysis: Failed filter works with zero observations', () => {
  const { timelineSection, state } = loadFns(FAILED_ONLY);
  state.timelineFilter = 'failed';
  const html = timelineSection();
  assert.ok(html.includes('UNRESOLVED'), 'failed records render with no observations');
  assert.ok(html.includes('budget hit at 2.0s'), 'second failed record present');
  assert.ok(html.includes('tc(720)'), 'source provenance resolves for failed frames');
  assert.ok(!/CUT · tc\(/.test(html), 'no CUT classification invented');
  assert.ok(!html.includes('>KEEP ·'), 'no KEEP classification invented');
  assert.strictEqual((html.match(/UNRESOLVED ·/g) || []).length, 2, 'no duplicated unresolved rows');
});

test('uncertain filter narrows to in-review samples only', () => {
  const { timelineSection, state } = loadFns(MIXED);
  state.timelineFilter = 'review';
  const html = timelineSection();
  assert.ok(!html.includes('UNRESOLVED'), 'review filter hides failed records');
  assert.ok(html.includes('tc(2)'), 'in-review sample shown');
});

test('mixed verdicts: findings counts, kept decisions, read-only history', () => {
  const { timelineSection } = loadFns(MIXED);
  const html = timelineSection();
  assert.ok(html.includes('2 to review'), 'findings count uses intervals');
  assert.ok(html.includes('1 keep'), 'keep count shown');
  assert.ok(html.includes('Archived human decisions'), 'archive renders');
  assert.ok(html.includes('read-only'), 'marked read-only');
  assert.ok(!html.includes('data-timeline-decision'), 'no decision buttons on archive');
});

test('zero observations still shows archived decisions read-only', () => {
  const only = { ...FAILED_ONLY, observations: [], eye_failed_frames: [],
    archived_decisions: MIXED.archived_decisions };
  const { timelineSection } = loadFns(only);
  const html = timelineSection();
  assert.ok(html.includes('Archived human decisions'), 'archive renders with no observations');
  assert.ok(html.includes('read-only'), 'marked read-only');
  assert.ok(!html.includes('data-timeline-decision'), 'no decision buttons on archive');
});
