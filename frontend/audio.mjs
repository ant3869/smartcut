// Pure audio-pass view helpers. Editing/rendering authority lives server-side
// (pipeline/audio.py + blade); these only summarize plan/apply responses.
export function cleanupSummary(plan) {
  const processors = plan.processors || [];
  const head = processors.length ? processors.join(', ') : 'no processors enabled';
  const notes = [...(processors.length ? [plan.chain] : []), ...(plan.warnings || [])].filter(Boolean);
  return notes.length ? `${head} · ${notes.join(' · ')}` : head;
}

export function beatsSummary(result) {
  const beats = (result.beats || []).length, downs = (result.downbeats || []).length;
  return `${result.tempo} BPM · ${beats} beat${beats === 1 ? '' : 's'} · ${downs} downbeat${downs === 1 ? '' : 's'}`;
}

export function bedSummary(summary, options) {
  const clips = summary.clips_added || 0;
  const ducking = options && options.duck ? 'ducking' : 'flat';
  return `${clips} clip${clips === 1 ? '' : 's'} · end ${summary.end}s · ${ducking}`;
}
