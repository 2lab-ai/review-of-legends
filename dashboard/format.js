/** Value formatting. Pure functions only - no DOM, no network, no globals.
 *
 * Two rules drive everything here:
 *   1. Unknown is not zero. A missing number renders as "unknown", never "0".
 *   2. Status is carried by text, never by colour alone (the stylesheet is
 *      monochrome, so a label is the only signal a reader gets).
 */

export const UNKNOWN = 'unknown';

/** Execution statuses the store can write (dashboard_store.py:35 + baseline rows). */
export const RUN_STATUS_LABELS = {
  running: 'Running',
  succeeded: 'Succeeded',
  failed: 'Failed',
  interrupted: 'Interrupted',
  stale: 'Stale',
  skipped: 'Skipped',
};

/** Visual emphasis tier. Never a colour - the stylesheet maps these to
 *  border/fill/opacity treatments only. */
export const RUN_STATUS_TONES = {
  running: 'active',
  succeeded: 'quiet',
  failed: 'attention',
  interrupted: 'attention',
  stale: 'attention',
  skipped: 'muted',
};

export const TRIGGER_LABELS = {
  poll: 'Polled',
  retry: 'Retry',
  retrigger: 'Retrigger',
  legacy: 'Legacy record',
  baseline: 'Baseline, not reviewed',
};

/** Retry-request lifecycle (dashboard_store.py:48-49). A request is a *dispatch*
 *  record; it is not the review outcome, so the wording stays about dispatch. */
export const RETRY_STATUS_LABELS = {
  queued: 'Queued for dispatch',
  running: 'Claimed by the executor',
  succeeded: 'Dispatch completed',
  failed: 'Dispatch failed',
  stale: 'Dispatch dropped as stale',
};

export const ACTIVE_RETRY_STATUSES = ['queued', 'running'];

const AGENT_STATUS_LABELS = {
  queued: 'Queued',
  running: 'Running',
  succeeded: 'Succeeded',
  failed: 'Failed',
  canceled: 'Canceled',
  skipped: 'Skipped',
};

/** Unknown tokens are shown verbatim rather than hidden: the server may grow a
 *  status before this bundle is redeployed, and silently dropping it would lie. */
export function statusLabel(status) {
  if (typeof status !== 'string' || status.trim() === '') return 'Unknown';
  return RUN_STATUS_LABELS[status] || status;
}

export function statusTone(status) {
  if (typeof status !== 'string') return 'muted';
  return RUN_STATUS_TONES[status] || 'muted';
}

export function agentStatusLabel(status) {
  if (typeof status !== 'string' || status.trim() === '') return 'Unknown';
  return AGENT_STATUS_LABELS[status] || status;
}

export function triggerLabel(trigger) {
  if (typeof trigger !== 'string' || trigger.trim() === '') return 'Unknown';
  return TRIGGER_LABELS[trigger] || trigger;
}

export function retryStatusLabel(status) {
  if (typeof status !== 'string' || status.trim() === '') return 'Unknown';
  return RETRY_STATUS_LABELS[status] || status;
}

export function shortSha(sha, length = 7) {
  if (typeof sha !== 'string') return UNKNOWN;
  const trimmed = sha.trim();
  if (trimmed === '') return UNKNOWN;
  return trimmed.slice(0, length);
}

const INTEGER_FORMAT = new Intl.NumberFormat('en-US');

export function formatInt(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return UNKNOWN;
  return INTEGER_FORMAT.format(value);
}

/** llmux reports window utilization as a 0..1 fraction
 *  (llmux-islands-linux/fixtures/dashboard.json:30). Values are never clamped -
 *  an out-of-range number is upstream's data, not ours to hide. */
export function formatPercent(fraction) {
  if (typeof fraction !== 'number' || !Number.isFinite(fraction)) return UNKNOWN;
  const percent = fraction * 100;
  const decimals = Math.abs(percent) < 10 && percent !== 0 ? 1 : 0;
  return `${percent.toFixed(decimals)}%`;
}

function parseIso(iso) {
  if (typeof iso !== 'string' || iso.trim() === '') return null;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : null;
}

/** Absolute UTC rendering, used for title= tooltips beside every relative time. */
export function formatTimestamp(iso) {
  const ms = parseIso(iso);
  if (ms === null) return UNKNOWN;
  return `${new Date(ms).toISOString().slice(0, 19).replace('T', ' ')} UTC`;
}

export function formatEpochSeconds(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return UNKNOWN;
  return formatTimestamp(new Date(seconds * 1000).toISOString());
}

export function formatEpochMillis(ms) {
  if (typeof ms !== 'number' || !Number.isFinite(ms)) return UNKNOWN;
  return formatTimestamp(new Date(ms).toISOString());
}

function relativeFromDelta(deltaMs) {
  const future = deltaMs < 0;
  const seconds = Math.floor(Math.abs(deltaMs) / 1000);
  let magnitude;
  if (seconds < 10) return future ? 'in a moment' : 'just now';
  if (seconds < 60) magnitude = `${seconds}s`;
  else if (seconds < 3600) magnitude = `${Math.floor(seconds / 60)}m`;
  else if (seconds < 86400) magnitude = `${Math.floor(seconds / 3600)}h`;
  else magnitude = `${Math.floor(seconds / 86400)}d`;
  return future ? `in ${magnitude}` : `${magnitude} ago`;
}

export function relativeTime(iso, now = Date.now()) {
  const ms = parseIso(iso);
  if (ms === null) return UNKNOWN;
  return relativeFromDelta(now - ms);
}

export function relativeFromEpochMillis(epochMs, now = Date.now()) {
  if (typeof epochMs !== 'number' || !Number.isFinite(epochMs)) return UNKNOWN;
  return relativeFromDelta(now - epochMs);
}

/** Coarse duration ("2h 3m"), used for attempt elapsed time and reset windows. */
export function formatDurationSeconds(totalSeconds) {
  if (typeof totalSeconds !== 'number' || !Number.isFinite(totalSeconds)) return UNKNOWN;
  const seconds = Math.max(0, Math.floor(totalSeconds));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) {
    const minutes = Math.floor(seconds / 60);
    const rest = seconds % 60;
    return rest === 0 ? `${minutes}m` : `${minutes}m ${rest}s`;
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return minutes === 0 ? `${hours}h` : `${hours}h ${minutes}m`;
}

export function durationBetween(startIso, endIso) {
  const start = parseIso(startIso);
  const end = parseIso(endIso);
  if (start === null || end === null) return UNKNOWN;
  return formatDurationSeconds((end - start) / 1000);
}

/** Counts in the navigation are *loaded* counts, so "50+" is the honest label
 *  whenever the server still has a next page. */
export function formatCount(loaded, hasMore) {
  if (typeof loaded !== 'number' || !Number.isFinite(loaded)) return UNKNOWN;
  const base = INTEGER_FORMAT.format(Math.max(0, Math.floor(loaded)));
  return hasMore ? `${base}+` : base;
}

/** Only http(s) links are rendered as links; anything else stays inert text. */
export function safeHttpUrl(value) {
  if (typeof value !== 'string' || value.trim() === '') return null;
  let parsed;
  try {
    parsed = new URL(value.trim());
  } catch {
    return null;
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
  return parsed.href;
}

/** "3/5 agents succeeded" - and never "succeeded" when only a minority did. */
export function agentProgress(run) {
  if (!run) return null;
  const total = run.total_agents;
  const done = run.successful_agents;
  if (typeof total !== 'number' || !Number.isFinite(total) || total <= 0) return null;
  const succeeded = typeof done === 'number' && Number.isFinite(done) ? done : null;
  return {
    succeeded,
    total,
    label: succeeded === null
      ? `${UNKNOWN}/${total} agents succeeded`
      : `${succeeded}/${total} agents succeeded`,
  };
}

export function prLabel(run) {
  if (!run || typeof run.pr !== 'number' || !Number.isFinite(run.pr)) return UNKNOWN;
  return `#${run.pr}`;
}

/** Attempt ids are uuid4 hex; operators only ever need the head to compare. */
export function shortRunId(runId, length = 8) {
  if (typeof runId !== 'string' || runId.trim() === '') return UNKNOWN;
  return runId.trim().slice(0, length);
}

/** The footer names the deployed version once /healthz reported one; until
 *  then, or without one, it keeps the static text index.html ships. */
export function footerText(version) {
  const base = 'Review of Legends admin dashboard';
  return typeof version === 'string' && version !== '' ? `${base} · ${version}` : base;
}
