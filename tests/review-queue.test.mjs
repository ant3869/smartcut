import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  isModelProposal, ruleOf, reasonText, parseConfidence,
  buildQueue, queueCounts, workflowState, appliedCheck, validateAdjust,
  reviewItemStatus,
} from '../frontend/review-queue.mjs';

const obs = [
  { timestamp: 6.0, score: 2, confidence: 0.9 },
  { timestamp: 8.0, score: 3, confidence: 0.7 },
  { timestamp: 20.0, score: 8, confidence: 0.95 },
];
const job = {
  duration: 100,
  waste_intervals: [
    { start: 7, end: 9, reasons: ['vision-cull:unrelated_banter'] },
    { start: 30, end: 34, reasons: ['editor-review:cut', 'vision-cull:setup'] },
  ],
  review_intervals: [
    { start: 5, end: 7, reasons: ['vision-review:technical_failure', 'confidence:0.20'] },
  ],
  observations: obs,
  review: { cut_intervals: [], keep_intervals: [] },
};
const evOf = () => ({ frames: 2, meanScore: 2.5, strength: 'strong' });
const statusOf = (r, review) => (review?.cut_intervals || []).length ? 'accepted' : 'pending';

test('editor-review echoes are not fresh proposals', () => {
  assert.equal(isModelProposal(job.waste_intervals[0]), true);
  assert.equal(isModelProposal(job.waste_intervals[1]), false);
});

test('buildQueue unifies cuts + review, sorted, with rule + confidence', () => {
  const q = buildQueue(job, { evidenceOf: evOf, statusOf });
  assert.deepEqual(q.map(i => i.kind), ['review', 'cut']);
  assert.equal(q[0].action, 'Review');
  assert.equal(q[0].rule, 'vision-review');
  assert.equal(q[0].confidence, 0.2);
  assert.equal(q[1].rule, 'vision-cull');
  // only the 8.0 sample falls inside [7,9)
  assert.equal(q[1].confidence, 0.7);
  assert.equal(q[1].strength, 'strong');
});

test('confidence falls back to null when nothing is known', () => {
  assert.equal(parseConfidence(['vision-cull:x'], [], 0, 1), null);
  assert.equal(parseConfidence(['confidence:85'], [], 0, 1), 0.85);
  assert.equal(reasonText(['confidence:0.2', 'vision-cull:setup_talk']), 'setup talk');
  assert.equal(ruleOf(['confidence:0.2']), 'model');
});

test('queueCounts separates suggestions from review-needed', () => {
  const q = buildQueue(job, { evidenceOf: evOf, statusOf });
  const c = queueCounts(q);
  assert.equal(c.suggestions, 1);
  assert.equal(c.reviewNeeded, 1);
  assert.equal(c.pending, 2);
  assert.equal(c.pendingCuts, 1);
});

test('decided items count as accepted / rejected (review needs explicit mark)', () => {
  const decided = {
    ...job,
    review: { cut_intervals: [{ start: 7, end: 9, reason: 'Accepted AI cut: x' }], keep_intervals: [{ start: 5, end: 7, reason: 'Kept review flag: y' }] },
  };
  const status = (r, review) => {
    const hit = (list) => (list || []).some(x => x.start <= r.start && x.end >= r.end);
    if (hit(review?.cut_intervals)) return 'accepted';
    if (hit(review?.keep_intervals)) return 'rejected';
    return 'pending';
  };
  const c = queueCounts(buildQueue(decided, { evidenceOf: evOf, statusOf: status }));
  assert.equal(c.accepted, 1);
  assert.equal(c.rejected, 1);
  assert.equal(c.pending, 0);
});

test('workflowState walks Analyze → Review → Apply → Render', () => {
  assert.equal(workflowState({ analyzed: false })[0].state, 'current');
  const mid = workflowState({ analyzed: true, pending: 3, accepted: 0 });
  assert.equal(mid[1].state, 'current');
  assert.equal(mid[1].detail, '3 pending');
  const ready = workflowState({ analyzed: true, pending: 0, accepted: 2, applied: false });
  assert.equal(ready[2].state, 'current');
  assert.equal(workflowState({ analyzed: true, pending: 0, accepted: 2, applied: true })[2].state, 'done');
  assert.equal(workflowState({ analyzed: true, pending: 0, accepted: 0 })[2].detail, 'nothing to apply');
});

test('appliedCheck reports ranges with no surviving mapping', () => {
  const map = (seq, src, s, e) => (s === 7 ? [] : [{ start: s, end: e }]);
  assert.deepEqual(appliedCheck([{ start: 7, end: 9 }, { start: 20, end: 22 }], {}, 'src', map), [true, false]);
  assert.deepEqual(appliedCheck([{ start: 7, end: 9 }], null, 'src', map), [null]);
});

test('REVIEW status needs an explicit review mark, not overlapping cuts', () => {
  const item = { start: 5, end: 7 };
  assert.equal(reviewItemStatus(item, { cut_intervals: [{ start: 5, end: 7, reason: 'Accepted AI cut: x' }], keep_intervals: [] }), 'pending');
  assert.equal(reviewItemStatus(item, { cut_intervals: [{ start: 5, end: 7, reason: 'Accepted review flag: y' }], keep_intervals: [] }), 'accepted');
  assert.equal(reviewItemStatus(item, { cut_intervals: [], keep_intervals: [{ start: 5, end: 7, reason: 'Kept review flag: y' }] }), 'rejected');
});

test('validateAdjust rejects bad ranges only', () => {
  assert.equal(validateAdjust(5, 7, 100), null);
  assert.ok(validateAdjust(7, 5, 100));
  assert.ok(validateAdjust(0, 200, 100));
  assert.ok(validateAdjust(NaN, 5, 100));
});
