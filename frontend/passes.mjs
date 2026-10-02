// Pure transition/watermark view helpers shared by the browser and Node tests.
// Authoritative detection/validation lives server-side (pipeline/passes.py);
// cutTicks only decorates the timeline, overlayBox only narrates the dialog.
import {duration} from './timeline.mjs';

export const TRANSITION_TYPES = ['cross-dissolve', 'dip-black', 'dip-white', 'fade', 'wipe', 'slide'];
export const TRANSITION_LABELS = {
  'cross-dissolve': 'Cross Dissolve', 'dip-black': 'Dip to Black', 'dip-white': 'Dip to White',
  fade: 'Fade', wipe: 'Wipe', slide: 'Slide',
};
export const OVERLAY_POSITIONS = ['top-left', 'top-center', 'top-right', 'center-left', 'center',
  'center-right', 'bottom-left', 'bottom-center', 'bottom-right', 'custom'];

export function cutTicks(sequence) {
  const ticks = [];
  for (const track of sequence.tracks || []) {
    const clips = (sequence.clips || []).filter(c => c.track === track.id && c.enabled !== false)
      .sort((a, b) => a.start - b.start);
    for (let i = 0; i + 1 < clips.length; i++) {
      const cut = clips[i].start + duration(clips[i]);
      if (Math.abs(cut - clips[i + 1].start) <= .001)
        ticks.push({track: track.id, cut_time: cut, outgoing: clips[i].id, incoming: clips[i + 1].id});
    }
  }
  return ticks;
}

export function transitionsAt(sequence) {
  const map = new Map();
  for (const t of sequence.transitions || []) {
    if (!map.has(t.track)) map.set(t.track, []);
    map.get(t.track).push(t);
  }
  return map;
}

export function overlayBox(canvasW, canvasH, imgW, imgH, overlay) {
  const w = Math.max(1, Math.round(canvasW * overlay.scale));
  const h = overlay.keep_aspect !== false && imgW && imgH
    ? Math.max(1, Math.round(w * imgH / imgW)) : Math.max(1, Math.round(canvasH * overlay.scale));
  let x, y;
  if (overlay.position === 'custom') { x = overlay.x || 0; y = overlay.y || 0; }
  else {
    const [vertical, horizontal] = overlay.position.split('-');
    x = horizontal === 'left' ? overlay.margin_x : horizontal === 'right' ? canvasW - w - overlay.margin_x : (canvasW - w) / 2;
    y = vertical === 'top' ? overlay.margin_y : vertical === 'bottom' ? canvasH - h - overlay.margin_y : (canvasH - h) / 2;
    if (overlay.position === 'center') x = (canvasW - w) / 2;
  }
  x = Math.min(Math.max(0, Math.round(x)), Math.max(0, canvasW - w));
  y = Math.min(Math.max(0, Math.round(y)), Math.max(0, canvasH - h));
  return {w, h, x, y};
}

export function passSummaryText(plan) {
  const s = plan.summary || {};
  const bits = [`${s.will_apply || 0} of ${(s.cuts_found || 0) + (s.edges || 0)} will receive ${TRANSITION_LABELS[s.type] || s.type}`];
  if (s.trimmed_seconds) bits.push(`${s.trimmed_seconds}s trimmed for handles`);
  if (s.skipped) bits.push(`${s.skipped} skipped`);
  return bits.join(' · ');
}
