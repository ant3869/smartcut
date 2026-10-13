// Unified Review Queue: pure data layer over the review-first workflow.
//
// Sources (never mutated here — all reads):
//   edit_plan.json  → waste_intervals (model CUT proposals), review_intervals (needs-eyes)
//   editor_review.json → cut_intervals / keep_intervals (human decisions, persisted via
//     POST /api/jobs/{id}/review and reloaded by summarize_job as job.review)
//   project.json    → sequence state (mapping + apply checks live in app.js)
//
// app.js renders this; auto.mjs supplies proposalEvidence/proposalStatus so no
// cutter logic is duplicated. No DOM here so node --test covers it.
import { coverage } from './auto.mjs';

// A waste interval the model itself decided (editor-review prefixed reasons are
// human decisions echoed back into the plan, not fresh proposals).
export function isModelProposal(range) {
  return !(range?.reasons || []).some(r => String(r).startsWith('editor-review'));
}

function cleanReason(r) {
  return String(r || '').replace(/vision-(cull|review):/g, '').replace(/_/g, ' ').trim();
}

// First reason token before ':' is the rule that fired ('vision-cull',
// 'transcript-waste', 'confidence', …). Confidence tokens are metadata, not rules.
export function ruleOf(reasons = []) {
  for (const r of reasons) {
    const s = String(r || '').trim();
    if (!s || /^confidence:/i.test(s)) continue;
    const head = s.split(':')[0].trim();
    if (head) return head;
  }
  return 'model';
}

export function reasonText(reasons = []) {
  const parts = (reasons || []).filter(r => !/^confidence:/i.test(String(r || ''))).map(cleanReason).filter(Boolean);
  return parts.join(' · ') || 'model proposal';
}

// Confidence for one interval: an explicit `confidence:X` reason token wins,
// otherwise the mean observation confidence inside the range, else null
// (unknown — never guessed).
export function parseConfidence(reasons = [], observations = [], start = 0, end = 0) {
  for (const r of reasons || []) {
    const m = /^confidence:\s*([0-9.]+)/i.exec(String(r || '').trim());
    if (m) {
      const v = Number(m[1]);
      if (Number.isFinite(v)) return Math.max(0, Math.min(1, v > 1 ? v / 100 : v));
    }
  }
  const inside = (observations || []).filter(o =>
    Number.isFinite(o?.timestamp) && o.timestamp >= start && o.timestamp < end &&
    Number.isFinite(Number(o?.confidence)));
  if (!inside.length) return null;
  return Math.round(inside.reduce((s, o) => s + Number(o.confidence), 0) / inside.length * 100) / 100;
}

// One unified, start-sorted queue item per waste proposal + review interval.
// evidence/status are injected (app.js passes A.proposalEvidence / A.proposalStatus)
// so this module never reimplements cutter scoring.
export function buildQueue(job, { evidenceOf = null, statusOf = null } = {}) {
  const review = job?.review || { cut_intervals: [], keep_intervals: [] };
  const obs = job?.observations || [];
  const items = [];
  for (const r of (job?.waste_intervals || []).filter(isModelProposal)) {
    const ev = evidenceOf ? evidenceOf(r, obs) : {};
    items.push({
      key: `cut:${r.start}-${r.end}`,
      kind: 'cut',
      action: 'Cut',
      start: r.start, end: r.end,
      reasons: r.reasons || [],
      text: reasonText(r.reasons),
      rule: ruleOf(r.reasons),
      confidence: parseConfidence(r.reasons, obs, r.start, r.end),
      frames: ev.frames ?? null,
      meanScore: ev.meanScore ?? null,
      strength: ev.strength ?? 'likely',
      status: statusOf ? statusOf(r, review) : 'pending',
    });
  }
  for (const r of job?.review_intervals || []) {
    const ev = evidenceOf ? evidenceOf(r, obs) : {};
    items.push({
      key: `review:${r.start}-${r.end}`,
      kind: 'review',
      action: 'Review',
      start: r.start, end: r.end,
      reasons: r.reasons || [],
      text: reasonText(r.reasons),
      rule: ruleOf(r.reasons),
      confidence: parseConfidence(r.reasons, obs, r.start, r.end),
      frames: ev.frames ?? null,
      meanScore: ev.meanScore ?? null,
      strength: ev.strength ?? 'likely',
      // REVIEW items track explicit human marks only: an overlapping accepted
      // CUT decides the footage outcome but must not masquerade as a reviewed
      // decision, or the queue could clear itself without a single explicit
      // review. decideQueue tags review marks with 'review flag' in the reason.
      status: statusOf ? reviewItemStatus(r, review) : 'pending',
    });
  }
  items.sort((a, b) => a.start - b.start || a.end - b.end);
  return items;
}

// Explicit human mark on one REVIEW interval: a covering cut/keep interval
// whose reason carries the 'review flag' tag decideQueue writes.
export function reviewItemStatus(item, review) {
  const marked = (list) => (list || []).filter(x => String(x?.reason || '').includes('review flag'));
  if (coverage(item, marked(review?.cut_intervals)) >= 0.5) return 'accepted';
  if (coverage(item, marked(review?.keep_intervals)) >= 0.5) return 'rejected';
  return 'pending';
}

export function queueCounts(items = []) {
  const cuts = items.filter(i => i.kind === 'cut');
  const eyes = items.filter(i => i.kind === 'review');
  const pending = items.filter(i => i.status === 'pending');
  return {
    suggestions: cuts.length,
    reviewNeeded: eyes.length,
    accepted: items.filter(i => i.status === 'accepted').length,
    rejected: items.filter(i => i.status === 'rejected').length,
    pending: pending.length,
    pendingCuts: cuts.filter(i => i.status === 'pending').length,
    appliedCandidates: cuts.filter(i => i.status === 'accepted').length,
  };
}

// Workflow stepper state. Inputs are facts the caller already derived — this
// only orders Analyze → Review → Apply → Render and names each stage's state.
export function workflowState({ analyzed = false, pending = 0, accepted = 0, applied = null, renderClean = true } = {}) {
  return [
    { id: 'analyze', label: 'Analyze', state: analyzed ? 'done' : 'current', detail: analyzed ? 'plan ready' : 'analyze a source' },
    { id: 'review', label: 'Review', state: !analyzed ? 'todo' : pending > 0 ? 'current' : 'done', detail: !analyzed ? 'waiting on analysis' : pending > 0 ? `${pending} pending` : 'all decided' },
    { id: 'apply', label: 'Apply', state: !analyzed || pending > 0 ? 'todo' : accepted > 0 ? (applied === true ? 'done' : 'current') : 'done', detail: accepted > 0 ? (applied === true ? 'accepted cuts are in the sequence' : 'accepted cuts not yet applied') : 'nothing to apply' },
    { id: 'render', label: 'Render', state: renderClean ? 'todo' : 'current', detail: renderClean ? 'render uses current edits' : 'unsaved edits — save first' },
  ];
}

// Which accepted ranges no longer play anywhere (i.e. applied to the sequence).
// sourceToSequence is injected (app.js passes A.sourceToSequence) so mapping
// logic lives in exactly one place.
export function appliedCheck(ranges = [], sequence, source, sourceToSequence) {
  if (!sequence || typeof sourceToSequence !== 'function') return ranges.map(() => null);
  return ranges.map(r => {
    try {
      return sourceToSequence(sequence, source, r.start, r.end).length === 0;
    } catch {
      return null;
    }
  });
}

// Adjusted In/Out for the Adjust action. Returns null when valid, reason otherwise.
export function validateAdjust(start, end, duration) {
  if (!Number.isFinite(start) || !Number.isFinite(end)) return 'Enter numeric In and Out times';
  if (!(start >= 0 && end > start)) return 'Out must be after In';
  if (Number.isFinite(duration) && end > duration) return 'Range exceeds the source duration';
  if (end - start < 0.02) return 'Range is too short to review';
  return null;
}
