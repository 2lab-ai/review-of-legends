/** Queue model: bucketing, merging, filtering, paging and retry linkage.
 *
 * Pure functions over plain data so the whole queue behaviour is unit-testable
 * without a DOM. The server is the only source of truth; nothing here invents a
 * status, a count or an ordering the API did not provide.
 */

import { ACTIVE_RETRY_STATUSES } from './format.js';

/** Attention = an attempt that ended without a posted review (dashboard_store.py:36). */
export const ATTENTION_STATUSES = ['failed', 'interrupted', 'stale'];

export const BUCKETS = ['active', 'attention', 'history'];

export const BUCKET_LABELS = {
  active: 'Active',
  attention: 'Needs attention',
  history: 'History',
};

/** Which /api/runs?status= queries feed a bucket. `null` means "no filter". */
export function bucketStatuses(bucket) {
  if (bucket === 'active') return ['running'];
  if (bucket === 'attention') return ATTENTION_STATUSES.slice();
  return [null];
}

export function bucketOf(run) {
  if (!run) return 'history';
  if (run.status === 'running') return 'active';
  if (ATTENTION_STATUSES.includes(run.status)) return 'attention';
  return 'history';
}

/** History holds every attempt, including the ones still open elsewhere. */
export function bucketAccepts(bucket, run) {
  if (bucket === 'history') return true;
  return bucketOf(run) === bucket;
}

export function isRetryable(run) {
  return !!run && ATTENTION_STATUSES.includes(run.status);
}

function timeValue(iso) {
  const ms = Date.parse(typeof iso === 'string' ? iso : '');
  return Number.isFinite(ms) ? ms : 0;
}

/** Newest first; id breaks ties so the order is stable across merges (the store
 *  writes second-resolution timestamps, so ties are common). */
export function compareRunsDesc(a, b) {
  const delta = timeValue(b && b.created_at) - timeValue(a && a.created_at);
  if (delta !== 0) return delta;
  const left = (a && a.id) || '';
  const right = (b && b.id) || '';
  if (left === right) return 0;
  return left < right ? 1 : -1;
}

/** Later occurrences win, so a freshly fetched row replaces a stale cached one. */
export function mergeRuns(...lists) {
  const byId = new Map();
  for (const list of lists) {
    for (const run of list || []) {
      if (!run || typeof run.id !== 'string') continue;
      byId.set(run.id, run);
    }
  }
  return Array.from(byId.values()).sort(compareRunsDesc);
}

export function upsertRun(runs, run) {
  return mergeRuns(runs, [run]);
}

export function removeRun(runs, runId) {
  return (runs || []).filter((run) => run.id !== runId);
}

export function findRun(runs, runId) {
  return (runs || []).find((run) => run.id === runId) || null;
}

function haystack(run) {
  return [
    run.id,
    run.repo,
    run.title,
    run.status,
    run.stage,
    run.error,
    run.head_sha,
    run.trigger,
    typeof run.pr === 'number' ? `#${run.pr}` : '',
    typeof run.pr === 'number' ? String(run.pr) : '',
  ]
    .filter((part) => typeof part === 'string' && part !== '')
    .join('\u0000')
    .toLowerCase();
}

/** Client-side narrowing of the rows already loaded. The UI must say so: this is
 *  not a server-side search over the full history. */
export function filterRuns(runs, { repo = '', query = '' } = {}) {
  const needle = typeof query === 'string' ? query.trim().toLowerCase() : '';
  return (runs || []).filter((run) => {
    if (!run) return false;
    if (repo && run.repo !== repo) return false;
    if (needle === '') return true;
    return haystack(run).includes(needle);
  });
}

export function repoOptions(runs) {
  const repos = new Set();
  for (const run of runs || []) {
    if (run && typeof run.repo === 'string' && run.repo !== '') repos.add(run.repo);
  }
  return Array.from(repos).sort();
}

/* --------------------------------------------------------------- pagination */

/** One paging cursor per status query that feeds a bucket. A bucket is exhausted
 *  only when every one of its sources is. */
export function createSources(bucket) {
  return bucketStatuses(bucket).map((status) => ({ status, nextOffset: 0, done: false }));
}

export function pendingSources(sources) {
  return (sources || []).filter((source) => !source.done && source.nextOffset !== null);
}

export function hasMore(sources) {
  return pendingSources(sources).length > 0;
}

export function applySourcePage(sources, status, nextOffset) {
  return (sources || []).map((source) => {
    if (source.status !== status) return source;
    const exhausted = nextOffset === null || nextOffset === undefined;
    return {
      status: source.status,
      nextOffset: exhausted ? null : nextOffset,
      done: exhausted,
    };
  });
}

/* ------------------------------------------------------------------- events */

const RUN_PATCH_FIELDS = [
  'status',
  'stage',
  'agents',
  'review_url',
  'error',
  'successful_agents',
  'total_agents',
  // Model policy + token totals travel on the same run_updated payload
  // (dashboard_web.EVENT_PAYLOAD_FIELDS), so the header can fill in live.
  'llm',
  // The agents a failed attempt kept results for: the open attempt's retry
  // buttons switch to "Retry failed agents (N)" the moment it fails.
  'reusable_agents',
];

/** Translate one SSE event into a run-shaped change.
 *
 * `run_started` payloads carry every column the list needs (dashboard_store.py:335),
 * so a brand new attempt appears without a refetch. `run_updated` only carries the
 * fields that changed, so an unseen run id means we must fetch the row. */
export function runChangeFromEvent(event) {
  if (!event || typeof event !== 'object') return null;
  const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
  if (event.kind === 'run_started' && typeof event.run_id === 'string') {
    return {
      kind: 'insert',
      runId: event.run_id,
      run: {
        id: event.run_id,
        repo: payload.repo,
        pr: payload.pr,
        head_sha: payload.head_sha,
        title: typeof payload.title === 'string' ? payload.title : '',
        url: typeof payload.url === 'string' ? payload.url : '',
        trigger: payload.trigger,
        retry_of: payload.retry_of === undefined ? null : payload.retry_of,
        status: payload.status || 'running',
        stage: null,
        agents: null,
        review_url: null,
        error: null,
        successful_agents: null,
        total_agents: null,
        created_at: event.created_at,
        updated_at: event.created_at,
      },
    };
  }
  if (event.kind === 'run_updated' && typeof event.run_id === 'string') {
    const patch = { updated_at: event.created_at };
    for (const field of RUN_PATCH_FIELDS) {
      if (field in payload) patch[field] = payload[field];
    }
    return { kind: 'patch', runId: event.run_id, patch };
  }
  return null;
}

/** A setting changed in another tab or session, or the daemon replaced an agent
 *  prompt with its changed file (agent_prompt_superseded). */
export function isSettingEvent(event) {
  return !!event && (event.kind === 'setting_updated' || event.kind === 'setting_deleted' || event.kind === 'agent_prompt_superseded');
}

export function isRetryEvent(event) {
  return (
    !!event &&
    (event.kind === 'retry_enqueued' ||
      event.kind === 'retry_claimed' ||
      event.kind === 'retry_finished')
  );
}

/** Apply a run change to a list, keeping bucket membership honest: an attempt
 *  that leaves a bucket (running -> failed) is dropped from it instead of
 *  lingering as a stale row. */
export function applyRunChange(runs, bucket, change) {
  if (!change) return { runs: runs || [], missing: false };
  const current = findRun(runs, change.runId);
  if (change.kind === 'insert') {
    if (!bucketAccepts(bucket, change.run)) {
      return { runs: removeRun(runs, change.runId), missing: false };
    }
    return { runs: upsertRun(runs, { ...(current || {}), ...change.run }), missing: false };
  }
  if (!current) return { runs: runs || [], missing: true };
  const updated = { ...current, ...change.patch };
  if (!bucketAccepts(bucket, updated)) {
    return { runs: removeRun(runs, change.runId), missing: false };
  }
  return { runs: upsertRun(runs, updated), missing: false };
}

/** Bounded ring buffer for the live trail. Duplicate ids (a replay after
 *  reconnect) are collapsed, and ordering follows the store's monotonic id. */
export function pushEvent(buffer, event, max = 1000) {
  if (!event || typeof event.id !== 'number') return buffer || [];
  const kept = (buffer || []).filter((existing) => existing.id !== event.id);
  kept.push(event);
  kept.sort((a, b) => a.id - b.id);
  return kept.length > max ? kept.slice(kept.length - max) : kept;
}

export function eventsForRun(buffer, runId) {
  if (typeof runId !== 'string' || runId === '') return [];
  return (buffer || []).filter((event) => event && event.run_id === runId);
}

/* ------------------------------------------------------- retry requests */

/** Newest-first union of retry requests, later lists winning on id collisions
 *  (the 202 response and the next /api/retries page describe the same row). */
export function mergeRetryRequests(...lists) {
  const byId = new Map();
  for (const list of lists) {
    for (const request of list || []) {
      if (!request || typeof request.id !== 'string') continue;
      byId.set(request.id, request);
    }
  }
  return Array.from(byId.values()).sort((a, b) => {
    const delta = timeValue(b.created_at) - timeValue(a.created_at);
    if (delta !== 0) return delta;
    return a.id < b.id ? 1 : -1;
  });
}

/** Reconcile what this tab remembers with what the server just said.
 *
 *  The parameters are named because the underlying merge is "last list wins":
 *  passing them the other way round let a cached `queued` row keep overwriting the
 *  terminal one, which blocked the dispatch panel forever. */
export function applyRetryUpdates(cached, fresh) {
  return mergeRetryRequests(cached, fresh);
}

/** Dispatch identity.
 *
 *  `enqueue_retry` deduplicates on repo + pr + head_sha (dashboard_store.py:69),
 *  so two failed attempts of the same commit share ONE request - and that request
 *  carries `retry_of` of whichever attempt was retried first. Matching the panel
 *  on `retry_of` alone therefore hid the dispatch on the sibling attempt and let
 *  its button stay enabled. Active-dispatch tracking matches the target instead;
 *  `retry_of` is still what lineage display uses, and the server is untouched.
 *
 *  Incomplete identities never match: a row without a repo, a positive pr and a
 *  head sha is not evidence of the same target. */
export function retryTargetOf(value) {
  if (!value || typeof value !== 'object') return null;
  const repo = typeof value.repo === 'string' ? value.repo.trim() : '';
  const headSha = typeof value.head_sha === 'string' ? value.head_sha.trim() : '';
  const pr = value.pr;
  if (repo === '' || headSha === '') return null;
  if (typeof pr !== 'number' || !Number.isFinite(pr) || pr <= 0) return null;
  return { repo, pr, head_sha: headSha };
}

export function isSameRetryTarget(left, right) {
  const a = retryTargetOf(left);
  const b = retryTargetOf(right);
  if (a === null || b === null) return false;
  return a.repo === b.repo && a.pr === b.pr && a.head_sha === b.head_sha;
}

function requestsForTarget(requests, run) {
  const matches = (requests || []).filter((request) => isSameRetryTarget(request, run));
  matches.sort((a, b) => timeValue(b.created_at) - timeValue(a.created_at));
  return matches;
}

/** Retry requests come from GET /api/retries, so the dispatch panel survives a
 *  reload or a reconnect instead of living only in this tab's memory. */
export function activeRetryFor(requests, run) {
  return requestsForTarget(requests, run)
    .find((request) => ACTIVE_RETRY_STATUSES.includes(request.status)) || null;
}

export function latestRetryFor(requests, run) {
  return requestsForTarget(requests, run)[0] || null;
}

/** Loaded runs that already have a dispatch in flight for their own commit - used
 *  to mark queue rows, including the sibling attempt that shares the request. */
export function runIdsWithActiveRetry(requests, runs) {
  const active = (requests || []).filter(
    (request) => request && ACTIVE_RETRY_STATUSES.includes(request.status),
  );
  const ids = new Set();
  for (const run of runs || []) {
    if (!run || typeof run.id !== 'string') continue;
    if (active.some((request) => isSameRetryTarget(request, run))) ids.add(run.id);
  }
  return ids;
}

/** The attempt a retry produced, if the daemon has started it yet. */
export function retryAttemptFor(runs, requests, run) {
  const request = latestRetryFor(requests, run);
  if (request && typeof request.run_id === 'string' && request.run_id !== '') {
    return findRun(runs, request.run_id) || { id: request.run_id };
  }
  const runId = run && typeof run.id === 'string' ? run.id : null;
  if (runId === null) return null;
  return (runs || []).find((candidate) => candidate && candidate.retry_of === runId) || null;
}

/** The retry buttons for one attempt.
 *
 *  An attempt that kept agent results (`reusable_agents`, ids only) offers
 *  "Retry failed agents (N)" first, N being its agents without a kept result,
 *  and "Retry all agents" second. An attempt that kept nothing (interrupted,
 *  stale, or failed before its agents finished) offers the whole review only,
 *  as before. `model` is the model pinned for the retry, if any; it applies to
 *  either button. */
export function retryChoices(run, model = '') {
  const suffix = typeof model === 'string' && model !== '' ? ` with ${model}` : '';
  const kept = Array.isArray(run && run.reusable_agents)
    ? run.reusable_agents.filter((id) => typeof id === 'string' && id !== '')
    : [];
  if (kept.length === 0) {
    return {
      reused: [],
      rerun: null,
      primary: { scope: 'all', label: `Confirm retry of the entire review${suffix}` },
      secondary: null,
    };
  }
  const ids = Array.isArray(run.agents)
    ? run.agents.map((agent) => (agent && typeof agent.id === 'string' ? agent.id : null)).filter((id) => id !== null)
    : [];
  // The attempt's own agent rows name what has no kept result; without rows,
  // the recorded total minus the kept ones.
  const rerun = ids.length > 0
    ? ids.filter((id) => !kept.includes(id)).length
    : Math.max(0, (typeof run.total_agents === 'number' ? run.total_agents : 0) - kept.length);
  return {
    reused: kept,
    rerun,
    primary: { scope: 'failed', label: `Retry failed agents (${rerun})${suffix}` },
    secondary: { scope: 'all', label: `Retry all agents${suffix}` },
  };
}

/** How a retry request's scope reads; a request queued before scopes existed ran every agent. */
export function retryScopeLabel(scope) {
  return scope === 'failed' ? 'failed agents only; the others are reused' : 'every agent';
}

/** Panel state for one run: optimistic local phases plus the durable request.
 *  `blocked` is the only thing the button may trust for disabling. */
export function retryPanelState({ run, request = null, local = null } = {}) {
  if (!run) return { phase: 'hidden', blocked: true };
  if (!isRetryable(run)) {
    return { phase: 'not-retryable', blocked: true, request, status: run ? run.status : null };
  }
  if (local && local.phase === 'dispatching') {
    return { phase: 'dispatching', blocked: true, request };
  }
  if (local && local.phase === 'rejected') {
    return { phase: 'rejected', blocked: true, request, error: local.error || null };
  }
  if (request && ACTIVE_RETRY_STATUSES.includes(request.status)) {
    return { phase: request.status === 'queued' ? 'queued' : 'claimed', blocked: true, request };
  }
  if (request && request.status === 'failed') {
    return { phase: 'dispatch-failed', blocked: false, request };
  }
  if (request && request.status === 'stale') {
    return { phase: 'dispatch-stale', blocked: false, request };
  }
  if (request && request.status === 'succeeded') {
    return { phase: 'dispatched', blocked: true, request };
  }
  return { phase: 'ready', blocked: false, request: null };
}
