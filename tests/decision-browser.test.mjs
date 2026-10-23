import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

// Functional regression coverage for timelineSection() + timelineDecisionsBrowser()
// in frontend/app.js: executes the real functions with DOM stubs against the
// actual round-1/round-2/archived-only fixtures. Fails loudly on the fixed
// defects: undefined counts, KEEP-by-default, judged failed frames, hidden
// archives, and swallowed review findings.
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

const SCRATCH = 'C:/Users/SuperHands/AppData/Local/hermes/profiles/mera/cache/scratch/dec-test/work/projects';
const R1 = JSON.parse(readFileSync(`${SCRATCH}/r1round/timeline_analysis.json`, 'utf8'));
const R2 = JSON.parse(readFileSync(`${SCRATCH}/77017ebeb84d413c/timeline_analysis.json`, 'utf8'));

test('round 1: all-CUT browser, real total, per-filter behavior', () => {
  const { timelineSection, state } = loadFns(R1);
  const html = timelineSection();
  assert.ok(!html.includes('All (undefined)'), 'All filter shows the real total');
  assert.ok(html.includes('All CUT'), 'all-CUT banner');
  assert.ok(html.includes('no cuts have been applied'), 'recommendations-only notice');
  state.timelineFilter = 'failed';
  const failed = timelineSection();
  assert.ok(failed.includes('UNRESOLVED'), 'failed filter shows real unresolved record');
  assert.ok(!/CUT · tc\(/.test(failed), 'failed view carries no CUT classification');
  state.timelineFilter = 'all';
  assert.ok(timelineSection().includes('UNJUDGED'), 'missing verdict labeled UNJUDGED, never KEEP');
});

test('round 1: uncertain filter narrows to in-review samples only', () => {
  const { timelineSection, state } = loadFns(R1);
  state.timelineFilter = 'review';
  const html = timelineSection();
  assert.ok(!html.includes('UNRESOLVED'), 'review filter hides failed records');
});

test('round 2: kept decisions and findings counts render', () => {
  const r2 = { ...R2, decisions: { 0: { action: 'keep', at: 'x' } }, archived_decisions: [] };
  const { timelineSection } = loadFns(r2);
  const html = timelineSection();
  assert.ok(html.includes('2 to review'), 'findings count uses intervals');
  assert.ok(html.includes('89 keep'), 'keep count shown');
});

test('zero observations still shows archived decisions read-only', () => {
  const only = { scope: { mode: 'entire' }, sequence_revision: 2, settings: {},
    sample_count: 0, judged_count: 0, observations: [], provenance: [],
    review_intervals: [], eye_failed_frames: [], decisions: {},
    archived_decisions: [{ interval: { start: 10, end: 12, reasons: ['old'] },
      action: 'keep', at: '2026-10-21', reason: 'finding-changed' }] };
  const { timelineSection } = loadFns(only);
  const html = timelineSection();
  assert.ok(html.includes('Archived human decisions'), 'archive renders with no observations');
  assert.ok(html.includes('read-only'), 'marked read-only');
  assert.ok(!html.includes('data-timeline-decision'), 'no decision buttons on archive');
});
