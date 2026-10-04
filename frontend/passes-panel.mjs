// Passes panel: pure orchestration over existing SmartCut features.
//
// Registration: each entry names the implementation it drives via `reuses`
// (an existing Auto-menu function or API route). New passes register by
// appending one descriptor + one runner — the panel, ordering, recipes and
// persistence never change. No DOM here so node --test covers it; app.js
// injects the real runners and renders the UI.

export const PASS_DEFS = [
  { id: 'ai-edit', label: 'AI Edit / Waste Cleanup', kind: 'timeline', modifies: true, executor: 'client', reuses: 'quickWaste' },
  { id: 'audio-cleanup', label: 'Audio Cleanup', kind: 'render', modifies: false, executor: 'server', reuses: 'applyCleanup', route: 'audio/cleanup/apply', clear: 'audio/cleanup/clear' },
  { id: 'silence', label: 'Silence Cleanup', kind: 'timeline', modifies: true, executor: 'client', reuses: 'quickSilence' },
  { id: 'music-bed', label: 'Music Bed', kind: 'timeline', modifies: true, executor: 'server', reuses: 'applyBed', route: 'audio/bed/apply', clear: 'audio/bed' },
  { id: 'beat-cuts', label: 'Beat Cuts', kind: 'timeline', modifies: true, executor: 'server', reuses: 'applyBeatCuts', route: 'audio/beats/cut' },
  { id: 'transitions', label: 'Transitions', kind: 'timeline', modifies: true, executor: 'server', reuses: 'applyTransitions', route: 'transitions/apply', clear: 'transitions/clear' },
  { id: 'intro-outro', label: 'Intro / Outro', kind: 'timeline', modifies: true, executor: 'server', reuses: 'applyIntroOutro', route: 'intro-outro/apply', clear: 'intro-outro/remove' },
  { id: 'captions', label: 'Captions', kind: 'timeline', modifies: true, executor: 'none', reuses: null },
  { id: 'watermark', label: 'Watermark', kind: 'timeline', modifies: true, executor: 'server', reuses: 'applyWatermark', route: 'watermark', clear: 'watermark' },
  { id: 'qc', label: 'QC', kind: 'verify', modifies: false, executor: 'server', reuses: 'runQc', route: 'qc/run' },
  { id: 'render', label: 'Render', kind: 'export', modifies: false, executor: 'client', reuses: 'openRender' },
];

const BY_ID = Object.fromEntries(PASS_DEFS.map(p => [p.id, p]));

export function passDef(id) {
  const def = BY_ID[id];
  if (!def) throw new Error(`Unknown pass: ${id}`);
  return def;
}

// Register a future pass without touching the panel. Throws on dupes so a
// double-registration fails loudly instead of silently shadowing.
export function registerPass(def) {
  if (!def?.id || BY_ID[def.id]) throw new Error(`Cannot register pass: ${def?.id || '(missing id)'}`);
  if (!['timeline', 'render', 'verify', 'export'].includes(def.kind)) throw new Error(`Bad kind for pass ${def.id}`);
  const entry = { executor: 'none', modifies: true, reuses: null, ...def };
  PASS_DEFS.push(entry);
  BY_ID[entry.id] = entry;
  return entry;
}

export function orderedPasses(state) {
  return Object.entries(state)
    .filter(([, s]) => s?.enabled)
    .sort((a, b) => (a[1].order ?? 0) - (b[1].order ?? 0))
    .map(([id, s]) => ({ id, ...s }));
}

// Run one pass through its registered runner. Runners are injected by app.js
// and are the exact functions the Auto menu uses — never panel-local copies.
export async function runPass(id, state, ctx = {}) {
  const def = passDef(id);
  const entry = state[id] || {};
  const key = def.reuses;
  const fn = key && (ctx.runners || {})[key];
  if (!fn) throw new Error(`Pass ${id} has no runner registered${def.executor === 'none' ? ' (planned pass)' : ''}`);
  (ctx.onStatus || (() => {}))(id, 'running');
  try {
    const summary = await fn(entry.settings || {}, ctx);
    (ctx.onStatus || (() => {}))(id, 'complete', summary);
    return summary;
  } catch (err) {
    (ctx.onStatus || (() => {}))(id, 'error', err?.message || String(err));
    throw err;
  }
}

// Run every enabled pass in stored order. stopOnError=true halts at the
// first failure (a QC error counts as a failure); false collects all
// results and keeps going. Returns {results, stopped}.
export async function runEnabled(state, ctx = {}) {
  const { stopOnError = true } = ctx;
  const results = {};
  let stopped = null;
  for (const { id } of orderedPasses(state)) {
    try {
      const summary = await runPass(id, state, ctx);
      results[id] = { ok: true, summary: summary ?? '' };
      if (id === 'qc' && typeof summary === 'string' && /[1-9]\d* error/.test(summary) && stopOnError) {
        stopped = id;
        break;
      }
    } catch (err) {
      results[id] = { ok: false, error: err?.message || String(err) };
      if (stopOnError) {
        stopped = id;
        break;
      }
    }
  }
  return { results, stopped };
}
