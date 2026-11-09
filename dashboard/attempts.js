/** Why an agent, or one of its model hops, failed. Pure functions only - no DOM.
 *
 * Input is the bounded agent row the API projects (dashboard_web.project_agents):
 * `attempt_log` is every hop the runner recorded, `error` the agent-level failure
 * output. Succeeded hops explain nothing, so they are never listed as failures.
 * The cause labels separate "out of quota" from everything else, which is the
 * question an operator opens this for.
 */

function seconds(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

function wholeSeconds(value) {
  return `${Math.round(value)}s`;
}

/** The runner records a 2xx it could not use (non-JSON, empty, truncated) as a
 *  permanent error too: only a 4xx is the upstream refusing the request. */
function permanentKind(hop) {
  const status = hop && Number.isInteger(hop.http_status) ? hop.http_status : null;
  if (status !== null && status >= 400) return 'rejected';
  if (status !== null && status >= 200 && status < 300) return 'unusable';
  return 'failed';
}

/** Long label for one hop's outcome; `hop` supplies the retry-after window. */
export function hopCause(hop) {
  const outcome = hop && typeof hop.outcome === 'string' ? hop.outcome : '';
  const retryAfter = seconds(hop && hop.retry_after_seconds);
  switch (outcome) {
    case 'quota_exhausted':
      return 'Out of quota — the proxy has no usage left for this model'
        + (retryAfter === null ? '' : ` (retry after ${wholeSeconds(retryAfter)})`);
    case 'rate_limited':
      // Recorded before the runner knows whether another attempt fits, so this
      // states the window the proxy announced, not that a retry happened.
      return 'Rate-limited (burst)'
        + (retryAfter === null ? '' : ` — the proxy asked to wait ${wholeSeconds(retryAfter)}`);
    case 'transient_error':
      return 'Transient — timeout / 5xx / connection';
    case 'permanent_error':
      return {
        rejected: 'Rejected — the upstream refused the request (not a quota problem)',
        unusable: 'Unusable response — the upstream answered but the reply could not be used (not a quota problem)',
        failed: 'Failed — not a quota problem',
      }[permanentKind(hop)];
    case 'no_json':
    case 'no_json_after_nudge':
      return 'Reply had no JSON object';
    case 'nudge_failed':
      return 'The follow-up asking for JSON failed';
    case 'effort_rejected':
      return 'The effort setting was rejected — retried without it';
    case 'failed_without_effort':
      return 'Failed again without the effort setting';
    default:
      return outcome ? outcome.replace(/_/g, ' ') : 'unknown outcome';
  }
}

const PERMANENT_SHORT_CAUSES = {
  rejected: 'rejected by upstream',
  unusable: 'unusable response',
  failed: 'failed',
};

const SHORT_CAUSES = {
  quota_exhausted: 'out of quota',
  rate_limited: 'rate-limited',
  transient_error: 'transient error',
  no_json: 'no JSON in the reply',
  no_json_after_nudge: 'no JSON in the reply',
  nudge_failed: 'JSON follow-up failed',
  effort_rejected: 'effort rejected',
  failed_without_effort: 'failed without effort',
};

function shortCause(hop) {
  if (hop.outcome === 'permanent_error') return PERMANENT_SHORT_CAUSES[permanentKind(hop)];
  if (hop.outcome === 'rate_limited' && hop.retry_after_seconds !== null) {
    return `rate-limited (asked to wait ${wholeSeconds(hop.retry_after_seconds)})`;
  }
  return SHORT_CAUSES[hop.outcome] || hop.outcome.replace(/_/g, ' ');
}

/** The store keeps the newest failed hops when a row runs out of room; the rest are counted. */
export function omittedNote(count) {
  return count === 1 ? '1 earlier hop not shown' : `${count} earlier hops not shown`;
}

/** `attempt 1 · HTTP 400 · retry after 3600s · 0.4s`, as separate facts. */
export function hopFacts(hop) {
  const facts = [];
  if (hop.attempt !== null) facts.push(`attempt ${hop.attempt}`);
  if (hop.http_status !== null) facts.push(`HTTP ${hop.http_status}`);
  if (hop.retry_after_seconds !== null) facts.push(`retry after ${wholeSeconds(hop.retry_after_seconds)}`);
  if (hop.elapsed_seconds !== null) facts.push(`${hop.elapsed_seconds.toFixed(1)}s`);
  return facts;
}

/** `{summary, hops, error, omitted}` for an agent row with something to explain, else null. */
export function failureDetail(agent) {
  if (!agent || typeof agent !== 'object') return null;
  const log = Array.isArray(agent.attempt_log) ? agent.attempt_log : [];
  const hops = [];
  for (const hop of log) {
    if (!hop || typeof hop !== 'object' || typeof hop.outcome !== 'string' || hop.outcome === '') continue;
    if (hop.outcome.startsWith('succeeded')) continue;
    hops.push({
      model: typeof hop.model === 'string' && hop.model !== '' ? hop.model : 'unknown model',
      outcome: hop.outcome,
      cause: hopCause(hop),
      attempt: Number.isInteger(hop.attempt) ? hop.attempt : null,
      http_status: Number.isInteger(hop.http_status) ? hop.http_status : null,
      retry_after_seconds: seconds(hop.retry_after_seconds),
      elapsed_seconds: seconds(hop.elapsed_seconds),
      text: typeof hop.error === 'string' && hop.error.trim() !== '' ? hop.error : null,
    });
  }
  const error = typeof agent.error === 'string' && agent.error.trim() !== '' ? agent.error : null;
  const omitted = Number.isInteger(agent.attempt_log_omitted) && agent.attempt_log_omitted > 0 ? agent.attempt_log_omitted : 0;
  if (hops.length === 0 && error === null && omitted === 0) return null;
  // One line per distinct failure: retries of the same refusal read once.
  const lines = [];
  for (const hop of hops) {
    const line = `${hop.model} — ${shortCause(hop)}${hop.http_status === null ? '' : ` (HTTP ${hop.http_status})`}`;
    if (lines[lines.length - 1] !== line) lines.push(line);
  }
  let summary = `Why: ${lines.join('; ')}`;
  if (lines.length === 0) summary = error !== null ? 'Why: the agent failed (output below)' : `Why: ${omittedNote(omitted)}`;
  return { summary, hops, error, omitted };
}
