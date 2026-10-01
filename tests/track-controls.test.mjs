import test from 'node:test';
import assert from 'node:assert/strict';
import * as T from '../frontend/timeline.mjs';

function sequence() {
  return {
    tracks: ['V2', 'V1', 'A1', 'A2'].map(id => ({id, locked: false, muted: false})),
    markers: [],
    clips: ['V1', 'A1'].map((track, i) => ({
      id: 'c' + i, track, source: 'fixture.mp4', source_start: 0, source_end: 10,
      start: 0, speed: 1, enabled: true, link_id: 'pair', keyframes: {},
    })),
  };
}

test('addTrack adds V3 then A3 with numbering and caps', () => {
  const s = sequence();
  T.addTrack(s, 'video');
  assert.deepEqual(s.tracks.map(t => t.id), ['V2', 'V1', 'A1', 'A2', 'V3']);
  T.addTrack(s, 'audio');
  assert.ok(s.tracks.some(t => t.id === 'A3'));
  for (let i = 4; i <= 8; i++) T.addTrack(s, 'video');
  assert.throws(() => T.addTrack(s, 'video'), /Maximum eight video/);
});

test('removeTrack deletes the top extra track and its clips', () => {
  const s = sequence();
  T.addTrack(s, 'video');
  s.clips.push({...s.clips[0], id: 'top', track: 'V3'});
  T.removeTrack(s, 'V3');
  assert.ok(!s.tracks.some(t => t.id === 'V3'));
  assert.ok(!s.clips.some(c => c.track === 'V3'));
  assert.equal(s.clips.length, 2);
});

test('removeTrack refuses base tracks, locked tracks and non-top tracks', () => {
  const s = sequence();
  for (const id of ['V1', 'V2', 'A1', 'A2'])
    assert.throws(() => T.removeTrack(s, id), /required/);
  T.addTrack(s, 'video'); T.addTrack(s, 'video');
  assert.throws(() => T.removeTrack(s, 'V3'), /V4 first/);
  s.tracks.find(t => t.id === 'V4').locked = true;
  assert.throws(() => T.removeTrack(s, 'V4'), /Unlock/);
  assert.throws(() => T.removeTrack(s, 'V9'), /not found/);
});

test('unlink breaks the pair so delete removes one clip only', () => {
  const s = sequence();
  T.unlink(s, 'c0', true);
  assert.equal(s.clips[0].link_id, null);
  assert.equal(s.clips[1].link_id, null);
  T.remove(s, 'c0', false, true);
  assert.deepEqual(s.clips.map(c => c.id), ['c1']);
});

test('linked delete still removes both when never unlinked', () => {
  const s = sequence();
  T.remove(s, 'c0', false, true);
  assert.deepEqual(s.clips, []);
});
