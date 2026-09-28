import test from 'node:test';
import assert from 'node:assert/strict';
import * as Tasks from '../frontend/tasks.mjs';

const at = seconds => new Date(Date.UTC(2026, 8, 28, 7, 0, 0) + seconds * 1000).toISOString();
const now = Date.parse(at(0)) + 400 * 1000;

test('durations read as seconds, minutes or hours', () => {
  assert.deepEqual([0, 45.6, 65, 3725].map(Tasks.formatDuration), ['0s', '45s', '1m 05s', '1h 02m']);
});

test('a running task shows elapsed time and how recently it reported progress', () => {
  const task = {status: 'running', created_at: at(0), started_at: at(8), updated_at: at(388)};
  assert.equal(Tasks.timingText(task, now), 'Running 6m 32s · updated 12s ago');
  assert.equal(Tasks.taskTiming(task, now).stalled, false);
});

test('a running task with no progress past the threshold is flagged, not failed', () => {
  const task = {status: 'running', created_at: at(0), started_at: at(8), updated_at: at(100)};
  const timing = Tasks.taskTiming(task, now);
  assert.equal(timing.stalled, true);
  assert.equal(Tasks.timingText(task, now), 'Running 6m 32s · no progress for 5m 00s · a model request may be slow or retrying');
});

test('queued and finished tasks describe their own timing', () => {
  assert.equal(Tasks.timingText({status: 'queued', created_at: at(395)}, now), 'Queued 5s');
  assert.equal(Tasks.timingText({status: 'succeeded', started_at: at(8), completed_at: at(440)}, now), 'Took 7m 12s');
  assert.equal(Tasks.timingText({status: 'failed', started_at: at(8), completed_at: at(20)}, now), 'Stopped after 12s');
});

test('missing or invalid timestamps produce no timing text', () => {
  assert.equal(Tasks.timingText({status: 'running'}, now), '');
  assert.equal(Tasks.timingText({status: 'succeeded', completed_at: 'nope'}, now), '');
});
