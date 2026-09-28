// Background-task timing: elapsed time plus how long since the task last reported
// progress, so a slow model request can be told apart from a stuck one.

// Longer than one normal model reply; a single request times out at 180s and is retried.
export const STALL_SECONDS = 180;

export function formatDuration(seconds) {
  const whole = Math.max(0, Math.floor(seconds));
  const pad = value => String(value).padStart(2, '0');
  if (whole < 60) return `${whole}s`;
  if (whole < 3600) return `${Math.floor(whole / 60)}m ${pad(whole % 60)}s`;
  return `${Math.floor(whole / 3600)}h ${pad(Math.floor((whole % 3600) / 60))}m`;
}

function secondsBetween(from, to) {
  const start = Date.parse(from);
  const end = typeof to === 'number' ? to : Date.parse(to);
  return Number.isFinite(start) && Number.isFinite(end) ? Math.max(0, (end - start) / 1000) : null;
}

export function taskTiming(task, now = Date.now()) {
  const elapsed = secondsBetween(task.started_at, now);
  const idle = secondsBetween(task.updated_at || task.started_at, now);
  return {elapsed, idle, stalled: task.status === 'running' && idle !== null && idle >= STALL_SECONDS};
}

export function timingText(task, now = Date.now()) {
  if (task.status === 'queued') {
    const waiting = secondsBetween(task.created_at, now);
    return waiting === null ? '' : `Queued ${formatDuration(waiting)}`;
  }
  if (task.status === 'running') {
    const {elapsed, idle, stalled} = taskTiming(task, now);
    if (elapsed === null) return '';
    const running = `Running ${formatDuration(elapsed)}`;
    if (idle === null) return running;
    return stalled
      ? `${running} · no progress for ${formatDuration(idle)} · a model request may be slow or retrying`
      : `${running} · updated ${formatDuration(idle)} ago`;
  }
  const took = secondsBetween(task.started_at, task.completed_at);
  if (took === null) return '';
  return task.status === 'succeeded' ? `Took ${formatDuration(took)}` : `Stopped after ${formatDuration(took)}`;
}
