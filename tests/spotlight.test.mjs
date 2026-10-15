import test from 'node:test';
import assert from 'node:assert/strict';
import {parseRanges, projectMedia, spotlightSummary, weightFields, sigText, dedupGap} from '../frontend/spotlight.mjs';

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

test('projectMedia keeps only the open project assets', () => {
  const jobs = [{id: 'j1', source: 'E:/vid/a.mp4'}, {id: 'j2', source: 'E:/vid/b.mp4'}];
  const assets = [{path: 'E:/vid/b.mp4'}];
  assert.deepEqual(projectMedia(jobs, assets).map(j => j.id), ['j2']);
  assert.deepEqual(projectMedia(jobs, []), []);
});

test('spotlightSummary narrates shorts', () => {
  const text = spotlightSummary({shorts: [{peaks: [1, 2], total: 8, spec: {aspect: '9:16'}},
    {peaks: [3], total: 4, spec: {aspect: '1:1'}}], warnings: []});
  assert.match(text, /2 shorts/);
  assert.match(text, /9:16/);
});

test('sigText renders backend signal dicts, arrays, and fallbacks', () => {
  assert.equal(sigText({signals: {action: 0.8, dialogue: 0.25}}), 'action 80% · dialogue 25%');
  assert.equal(sigText({signals: ['peak', 'speech']}), 'peak · speech');
  assert.equal(sigText({signals: {}}), 'ai score');
  assert.equal(sigText({}), 'ai score');
  assert.equal(sigText({signals: null}), 'ai score');
});

test('dedupGap honors an explicit zero', () => {
  assert.equal(dedupGap('0'), 0);
  assert.equal(dedupGap(0), 0);
  assert.equal(dedupGap('6'), 6);
  assert.equal(dedupGap('2.5'), 2.5);
  assert.equal(dedupGap(''), 6);
  assert.equal(dedupGap(null), 6);
  assert.equal(dedupGap('abc'), 6);
  assert.equal(dedupGap('-3'), 6);
});
