// Automatic editing commands. Pure functions over the sequence model, shared by the browser and Node tests.
// Every command mutates the sequence it is given (callers pass a clone) and returns a small report.
import {duration, end, locked, uid, validate, sequenceDuration} from './timeline.mjs';

const EPS = 1e-6;
const fix = n => Math.round(n * 1e6) / 1e6;

export function union(ranges, bridge = 0) {
  const sorted = ranges.filter(r => r.end - r.start > EPS).map(r => ({start:r.start, end:r.end})).sort((a,b) => a.start-b.start);
  const out = [];
  for (const r of sorted) {
    const last = out[out.length-1];
    if (last && r.start <= last.end + bridge + EPS) last.end = Math.max(last.end, r.end);
    else out.push(r);
  }
  return out;
}
export const total = ranges => union(ranges).reduce((sum, r) => sum + r.end - r.start, 0);
export function subtract(range, cuts) {
  let pieces = [{start:range.start, end:range.end}];
  for (const c of union(cuts)) pieces = pieces.flatMap(p => c.end <= p.start || c.start >= p.end ? [p] :
    [{start:p.start, end:Math.max(p.start, c.start)}, {start:Math.min(p.end, c.end), end:p.end}].filter(x => x.end - x.start > EPS));
  return pieces;
}
export function coverage(range, ranges) {
  const span = range.end - range.start;
  return span > 0 ? total(ranges.map(r => ({start:Math.max(r.start, range.start), end:Math.min(r.end, range.end)}))) / span : 0;
}

// Clips that carry picture for a source, plus audio clips with no linked picture, so each moment maps once.
function carriers(sequence, source) {
  const clips = sequence.clips.filter(c => c.enabled && (source == null || c.source === source));
  const visualLinks = new Set(clips.filter(c => c.track[0] === 'V' && c.link_id).map(c => c.link_id));
  return clips.filter(c => c.track[0] === 'V' || !c.link_id || !visualLinks.has(c.link_id));
}
export function sourceToSequence(sequence, source, start, finish) {
  const out = [];
  for (const c of carriers(sequence, source)) {
    const a = Math.max(start, c.source_start), b = Math.min(finish, c.source_end);
    if (b - a > EPS) out.push({start:fix(c.start + (a-c.source_start)/c.speed), end:fix(c.start + (b-c.source_start)/c.speed), clip:c.id});
  }
  return out.sort((x,y) => x.start-y.start);
}
export function sourcePoint(sequence, source, time) {
  return carriers(sequence, source).filter(c => c.source_start < time - .01 && c.source_end > time + .01)
    .map(c => fix(c.start + (time-c.source_start)/c.speed));
}
// Source media time under a sequence time; picture on V2 wins over V1, which wins over audio.
export function sequenceToSource(sequence, time, source = null) {
  const order = {V2:0, V1:1, A1:2, A2:3};
  const hit = carriers(sequence, source).filter(c => c.start <= time && end(c) > time).sort((a,b) => order[a.track]-order[b.track])[0];
  return hit ? {clip:hit, source:hit.source, time:hit.source_start + (time-hit.start)*hit.speed} : null;
}

// Stretches of the `before` timeline whose media no longer plays anywhere in `after` (for diff previews).
export function removedMap(before, after) {
  const out = [];
  for (const c of carriers(before, null)) {
    const kept = carriers(after, null).filter(k => k.source === c.source).map(k => ({start:k.source_start, end:k.source_end}));
    for (const gone of subtract({start:c.source_start, end:c.source_end}, kept))
      out.push({start:fix(c.start + (gone.start-c.source_start)/c.speed), end:fix(c.start + (gone.end-c.source_start)/c.speed)});
  }
  return union(out);
}

// Premiere-style Extract with every track targeted: remove sequence ranges and close them up.
export function extract(sequence, ranges) {
  const cuts = union(ranges).filter(r => r.end - r.start > .001);
  if (!cuts.length) return {ranges:[], seconds:0};
  const first = cuts[0].start;
  if (sequence.clips.some(c => locked(sequence, c.track) && end(c) > first + EPS)) throw new Error('Unlock every track with clips after the first cut');
  const removedBefore = t => cuts.reduce((sum, r) => sum + Math.max(0, Math.min(t, r.end) - r.start), 0);
  const shift = t => fix(t - removedBefore(t));
  const links = new Map(), clips = [];
  for (const c of sequence.clips) {
    const keep = subtract({start:c.start, end:end(c)}, cuts);
    const whole = keep.length === 1 && Math.abs(keep[0].start-c.start) < EPS && Math.abs(keep[0].end-end(c)) < EPS;
    for (const k of keep) {
      const piece = {...c, start:shift(k.start), source_start:fix(c.source_start + (k.start-c.start)*c.speed), source_end:fix(c.source_start + (k.end-c.start)*c.speed)};
      if (!whole) {
        const key = c.link_id + '|' + k.start.toFixed(4);
        if (c.link_id && !links.has(key)) links.set(key, uid());
        Object.assign(piece, {id:uid(), link_id:c.link_id ? links.get(key) : null});
      }
      if (duration(piece) >= .02) clips.push(piece);
    }
  }
  sequence.clips = clips;
  sequence.markers = sequence.markers.map(m => ({...m, time:shift(m.time)}));
  validate(sequence);
  return {ranges:cuts, seconds:fix(total(cuts))};
}

// Cut source-time ranges wherever that material plays; tiny keeps between cuts are absorbed.
export function removeSourceRanges(sequence, source, ranges, {minKeep = .3} = {}) {
  const mapped = ranges.flatMap(r => sourceToSequence(sequence, source, r.start, r.end));
  return extract(sequence, union(mapped, minKeep));
}
export function gaps(sequence) {
  const busy = union(sequence.clips.map(c => ({start:c.start, end:end(c)})));
  let cursor = 0; const out = [];
  for (const r of busy) { if (r.start > cursor + .001) out.push({start:cursor, end:r.start}); cursor = Math.max(cursor, r.end); }
  return out;
}
export const closeGaps = sequence => extract(sequence, gaps(sequence));

export const toDb = peak => 20 * Math.log10(Math.max(peak, 1e-5));
const percentile = (values, p) => { const s = [...values].sort((a,b) => a-b); return s.length ? s[Math.min(s.length-1, Math.floor(p*s.length))] : 0; };
// Noise floor and speech level in dB; a threshold a fifth of the way up sits just above room tone.
// A narrow range means constant background sound, where "silence" cuts would clip quiet words.
export function analyzeLevels(peaks) {
  const loud = peaks.filter(p => p > 1e-4);
  if (loud.length < 10) return {floor:null, speech:null, range:0, threshold:-40, reliable:false};
  const floor = toDb(percentile(loud, .1)), speech = toDb(percentile(loud, .9)), range = speech - floor;
  return {floor:Math.round(floor), speech:Math.round(speech), range:Math.round(range),
    threshold:Math.round(Math.min(-18, Math.max(-60, floor + range * .2))), reliable:range >= 18};
}
export const suggestThreshold = peaks => analyzeLevels(peaks).threshold;
// Source-time silent spans (peaks below threshold for at least minSilence), shrunk by pad for breathing room.
export function silentRanges(peaks, rate, {thresholdDb = -40, minSilence = .6, pad = .12} = {}) {
  const limit = 10 ** (thresholdDb / 20), out = [];
  let from = null;
  peaks.concat([Infinity]).forEach((p, i) => {
    if (p < limit) { if (from === null) from = i; return; }
    if (from !== null && (i-from)/rate >= minSilence) {
      const start = from === 0 ? 0 : from/rate + pad, finish = i === peaks.length ? i/rate : i/rate - pad;
      if (finish - start > .05) out.push({start:fix(start), end:fix(finish)});
    }
    from = null;
  });
  return out;
}

// New sequence markers at source scene changes that fall inside kept material (clip edges are already cuts).
export function sceneMarkers(sequence, source, boundaries) {
  const existing = sequence.markers.map(m => m.time), made = [];
  boundaries.forEach((b, i) => sourcePoint(sequence, source, b).forEach(time => {
    if ([...existing, ...made.map(m => m.time)].some(t => Math.abs(t-time) < .25)) return;
    made.push({id:uid(), time, label:`Scene ${i+2}`});
  }));
  return made.sort((a,b) => a.time-b.time);
}
// The best kept moments still in the sequence, spaced apart so markers read as distinct beats.
export function highlightMarkers(sequence, source, observations, {count = 5, minScore = 7, spacing = 6} = {}) {
  const picked = [];
  for (const o of [...observations].filter(o => o.keep !== false && o.score >= minScore).sort((a,b) => b.score-a.score || a.timestamp-b.timestamp)) {
    const time = sourcePoint(sequence, source, o.timestamp)[0];
    if (time === undefined || picked.some(m => Math.abs(m.time-time) < spacing)) continue;
    const text = String(o.description || '').split(/[.;]/)[0].slice(0, 60);
    picked.push({id:uid(), time, label:`★ ${o.score}/10 ${text}`.trim()});
    if (picked.length >= count) break;
  }
  return picked.sort((a,b) => a.time-b.time);
}

// How strongly the frames inside a proposal support cutting it. Model confidence barely varies (0.8-0.9),
// but a cut whose own frames scored well is self-contradictory and deserves a human look.
export function proposalEvidence(proposal, observations) {
  const frames = observations.filter(o => o.timestamp >= proposal.start && o.timestamp < proposal.end);
  if (!frames.length) return {frames:0, meanScore:null, strength:'likely'};
  const meanScore = Math.round(frames.reduce((s, o) => s + (Number(o.score) || 0), 0) / frames.length * 10) / 10;
  return {frames:frames.length, meanScore, strength:meanScore <= 3 ? 'strong' : meanScore >= 6 ? 'weak' : 'likely'};
}
// Transcript segments re-timed to the edit; a segment split by a cut appears once per surviving piece.
export function editCaptions(sequence, source, segments, {minDuration = .2} = {}) {
  return segments.flatMap(s => sourceToSequence(sequence, source, s.start, s.end).map(r => ({start:r.start, end:r.end, text:String(s.text || '').trim()})))
    .filter(c => c.text && c.end - c.start >= minDuration).sort((a,b) => a.start-b.start);
}
export function toSRT(captions) {
  const stamp = n => { const ms = Math.round(n*1000), p = (v, w = 2) => String(v).padStart(w, '0'); return `${p(Math.floor(ms/3600000))}:${p(Math.floor(ms/60000)%60)}:${p(Math.floor(ms/1000)%60)},${p(ms%1000, 3)}`; };
  return captions.map((c, i) => `${i+1}\n${stamp(c.start)} --> ${stamp(c.end)}\n${c.text}\n`).join('\n');
}

// Human decision state for one model proposal, both in source time.
export function proposalStatus(proposal, review) {
  if (coverage(proposal, review?.cut_intervals || []) >= .5) return 'accepted';
  if (coverage(proposal, review?.keep_intervals || []) >= .5) return 'rejected';
  return 'pending';
}
// Scale that makes media fill the frame instead of fitting inside it (render and preview both fit at 1×).
export function fillScale(mediaWidth, mediaHeight, frameWidth, frameHeight) {
  const x = frameWidth / mediaWidth, y = frameHeight / mediaHeight;
  return Math.round(Math.max(x, y) / Math.min(x, y) * 1000) / 1000;
}
export const summary = sequence => ({duration:fix(sequenceDuration(sequence)), clips:sequence.clips.filter(c => c.track[0] === 'V').length, markers:sequence.markers.length});
