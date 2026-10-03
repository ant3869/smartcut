import test from 'node:test';
import assert from 'node:assert/strict';
import {beatsSummary, bedSummary, cleanupSummary} from '../frontend/audio.mjs';

test('cleanupSummary narrates processors', () => {
  assert.equal(cleanupSummary({processors: ['normalize', 'limiter'], chain: 'loudnorm,alimiter', warnings: []}), 'normalize, limiter · loudnorm,alimiter');
  assert.equal(cleanupSummary({processors: [], chain: '', warnings: ['de-esser skipped']}), 'no processors enabled · de-esser skipped');
});

test('beatsSummary narrates tempo and counts', () => {
  assert.equal(beatsSummary({tempo: 120, beats: [0, .5, 1], downbeats: [0]}), '120 BPM · 3 beats · 1 downbeat');
});

test('bedSummary narrates clips and ducking', () => {
  assert.match(bedSummary({clips_added: 3, end: 6}, {duck: true}), /3 clips · end 6s · ducking/);
  assert.match(bedSummary({clips_added: 1, end: 2}, {}), /1 clip · end 2s · flat/);
});
