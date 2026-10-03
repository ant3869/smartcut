import test from 'node:test';
import assert from 'node:assert/strict';
import {cutTicks, overlayBox, passSummaryText, transitionsAt, TRANSITION_LABELS} from '../frontend/passes.mjs';
import {pruneTransitions} from '../frontend/timeline.mjs';

function clip(id, track, start, source_start, source_end, speed = 1, enabled = true) {
  return {id, track, source: 's.mp4', start, source_start, source_end, speed, enabled, keyframes: {}};
}
function sequence(clips, extra = {}) {
  return {tracks: [{id: 'V2'}, {id: 'V1'}, {id: 'A1'}, {id: 'A2'}], clips, markers: [], transitions: [], overlays: [], ...extra};
}

test('cutTicks finds true adjacent cuts only', () => {
  const s = sequence([clip('a', 'V1', 0, 0, 5), clip('b', 'V1', 5, 5, 10), clip('c', 'V1', 12, 10, 15)]);
  assert.deepEqual(cutTicks(s).map(t => [t.track, t.cut_time]), [['V1', 5]]);
});

test('cutTicks skips disabled clips', () => {
  const s = sequence([clip('a', 'V1', 0, 0, 5), clip('b', 'V1', 5, 5, 10, 1, false), clip('c', 'V1', 5, 5, 10)]);
  assert.deepEqual(cutTicks(s).map(t => t.cut_time), [5]);
});

test('pruneTransitions drops only dead refs', () => {
  const s = sequence([], {transitions: [
    {id: 't1', outgoing_id: 'a', incoming_id: 'b'},
    {id: 't2', outgoing_id: '', incoming_id: 'c'},
    {id: 't3', outgoing_id: 'a', incoming_id: ''},
  ]});
  pruneTransitions(s, new Set(['b']));
  assert.deepEqual(s.transitions.map(t => t.id), ['t2', 't3']);
});

test('transitionsAt groups by track', () => {
  const s = sequence([], {transitions: [
    {id: 't1', track: 'V1'}, {id: 't2', track: 'A1'}, {id: 't3', track: 'V1'},
  ]});
  const map = transitionsAt(s);
  assert.deepEqual(map.get('V1').map(t => t.id), ['t1', 't3']);
  assert.deepEqual(map.get('A1').map(t => t.id), ['t2']);
});

test('overlayBox mirrors the renderer geometry', () => {
  const base = {scale: .15, opacity: .85, margin_x: 24, margin_y: 24, keep_aspect: true};
  assert.deepEqual(overlayBox(640, 360, 200, 100, {position: 'top-left', ...base}), {w: 96, h: 48, x: 24, y: 24});
  assert.deepEqual(overlayBox(640, 360, 200, 100, {position: 'bottom-right', ...base}), {w: 96, h: 48, x: 520, y: 288});
  assert.deepEqual(overlayBox(640, 360, 200, 100, {position: 'center', ...base}), {w: 96, h: 48, x: 272, y: 156});
  assert.deepEqual(overlayBox(640, 360, 200, 100, {position: 'custom', x: 10, y: 900, ...base}), {w: 96, h: 48, x: 10, y: 312});
});

test('labels and summary text stay human', () => {
  assert.equal(TRANSITION_LABELS['dip-black'], 'Dip to Black');
  assert.equal(passSummaryText({summary: {type: 'wipe', will_apply: 2, cuts_found: 3, edges: 0, skipped: 1, trimmed_seconds: 0}}),
    '2 of 3 will receive Wipe · 1 skipped');
});
