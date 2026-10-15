// Pure spotlight view helpers. Ranking/rendering authority lives server-side
// (pipeline/spotlight.py); these only parse dialog inputs and narrate plans.
export function weightFields() {
  return ['action', 'dialogue', 'emotion', 'quality', 'scores'];
}

export function projectMedia(jobs, assets) {
  const allowed = new Set((assets || []).map(a => String(a.path).replace(/\\/g, '/')));
  return (jobs || []).filter(j => allowed.has(String(j.source).replace(/\\/g, '/')));
}

export function parseRanges(text) {
  const ranges = [];
  for (const part of String(text || '').split(',')) {
    const match = part.trim().match(/^(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)$/);
    if (!match) continue;
    const start = Number(match[1]), end = Number(match[2]);
    if (Number.isFinite(start) && Number.isFinite(end) && end > start) ranges.push([start, end]);
  }
  return ranges;
}

export function spotlightSummary(plan) {
  const bits = [];
  if (plan.shorts) {
    bits.push(`${plan.shorts.length} short${plan.shorts.length === 1 ? '' : 's'}`);
    for (const [index, short] of plan.shorts.entries())
      bits.push(`Short ${index + 1}: ${short.peaks.length} moments · ${short.total}s · ${short.spec?.aspect || ''}`);
  } else {
    const ranked = plan.ranked || [];
    bits.push(`${ranked.length} moments · ${plan.total_seconds || 0}s`);
    if ((plan.skipped || []).length) bits.push(`${plan.skipped.length} skipped`);
  }
  for (const warning of plan.warnings || []) bits.push(warning);
  return bits.join(' · ');
}

export function sigText(r) {
  const s = r.signals;
  if (Array.isArray(s) && s.length) return s.join(' · ');
  if (s && typeof s === 'object') {
    const e = Object.entries(s).filter(([, v]) => typeof v === 'number');
    if (e.length) return e.map(([k, v]) => `${k} ${Math.round(v * 100)}%`).join(' · ');
  }
  return 'ai score';
}

export function dedupGap(value) {
  if (value == null || value === '') return 6;
  const v = Number(value);
  return Number.isFinite(v) && v >= 0 ? v : 6;
}
