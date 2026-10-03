import test from 'node:test';
import assert from 'node:assert/strict';
import {parseRanges, spotlightSummary, weightFields} from '../frontend/spotlight.mjs';

test('parseRanges reads start-end pairs', () => {
  assert.deepEqual(parseRanges('12-18, 40-44'), [[12, 18], [40, 44]]);
  assert.deepEqual(parseRanges('  '), []);
  assert.deepEqual(parseRanges('9-4, abc, 7-9'), [[7, 9]]);
});

test('weightFields lists the five prioritize knobs', () => {
  assert.deepEqual(weightFields(), ['action', 'dialogue', 'emotion', 'quality', 'scores']);
});

test('spotlightSummary narrates a highlight plan', () => {
  const text = spotlightSummary({ranked: [{peak: 1}, {peak: 2}], total_seconds: 9.5,
    skipped: [{peak: 3, reason: 'too close'}], warnings: ['no transcript']});
  assert.match(text, /2 moments · 9\.5s/);
  assert.match(text, /1 skipped/);
  assert.match(text, /no transcript/);
});

test('spotlightSummary narrates shorts', () => {
  const text = spotlightSummary({shorts: [{peaks: [1, 2], total: 8, spec: {aspect: '9:16'}},
    {peaks: [3], total: 4, spec: {aspect: '1:1'}}], warnings: []});
  assert.match(text, /2 shorts/);
  assert.match(text, /9:16/);
});
