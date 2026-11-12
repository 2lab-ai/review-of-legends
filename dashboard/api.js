/** Typed access to the dashboard HTTP contract.
 *
 * Two coalescing mechanisms, because the event stream can fire far faster than
 * the API should be polled:
 *   - identical in-flight GETs share one promise (a burst of events that all want
 *     the same page produces one request),
 *   - `createCoalescer` collapses a burst of refresh requests into one trailing
 *     call and never runs two overlapping refreshes.
 *
 * The CSRF token lives in memory only; it is never written to storage or a URL.
 */

import { ApiError } from './errors.js';

export const MAX_PAGE = 200;
export const DEFAULT_PAGE = 50;

async function parseJson(response) {
  const text = await response.text();
  if (text === '') return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

/** `?name=<sha>` naming the exact kept edit a request acts on, or '' with none
 *  to name (the server then answers 400 where the sha is required). */
function shaQuery(name, sha) {
  return typeof sha === 'string' && sha !== '' ? `?${name}=${encodeURIComponent(sha)}` : '';
}

export class ApiClient {
  constructor({ fetchImpl, basePath = '' } = {}) {
    this.fetchImpl = fetchImpl || ((...args) => globalThis.fetch(...args));
    this.basePath = basePath;
    this.csrfToken = null;
    this.identity = null;
    this.inFlight = new Map();
  }

  /** GETs are deduplicated by URL while in flight. */
  get(path) {
    const existing = this.inFlight.get(path);
    if (existing) return existing;
    const promise = this.request(path, { method: 'GET' }).finally(() => {
      this.inFlight.delete(path);
    });
    this.inFlight.set(path, promise);
    return promise;
  }

  async request(path, { method = 'GET', body = null, csrf = false } = {}) {
    const headers = { Accept: 'application/json' };
    let payload;
    if (body !== null) {
      headers['Content-Type'] = 'application/json';
      payload = JSON.stringify(body);
    }
    if (csrf) {
      if (!this.csrfToken) {
        throw new ApiError(403, { message: 'No CSRF token; sign in again.' });
      }
      headers['X-CSRF-Token'] = this.csrfToken;
    }
    let response;
    try {
      response = await this.fetchImpl(`${this.basePath}${path}`, {
        method,
        headers,
        body: payload,
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
      });
    } catch (cause) {
      throw new ApiError(0, { message: 'network request failed', body: null, code: 'network' });
    }
    if (response.status === 204) return null;
    const data = await parseJson(response);
    if (!response.ok) {
      throw new ApiError(response.status, { body: data });
    }
    return data;
  }

  /** A one-time code for `@review-of-legends verify <code>` (contributor gate): `{code, expires_at, command}`. */
  async contributorCode() {
    return this.request('/api/contributor/code', { method: 'POST', body: {}, csrf: true });
  }

  async me() {
    const data = await this.request('/api/me', { method: 'GET' });
    const identity = data && typeof data === 'object' ? data : {};
    this.csrfToken = typeof identity.csrf_token === 'string' ? identity.csrf_token : null;
    this.identity = {
      email: typeof identity.email === 'string' ? identity.email : null,
      role: typeof identity.role === 'string' ? identity.role : null,
      owner: identity.owner === true,
    };
    return this.identity;
  }

  /** `{status, version, auth_configured}`; public, so it answers before sign-in. */
  health() {
    return this.get('/healthz');
  }

  runs({ status = null, repo = '', limit = DEFAULT_PAGE, offset = 0 } = {}) {
    const params = new URLSearchParams();
    if (status) params.set('status', status);
    if (repo) params.set('repo', repo);
    params.set('limit', String(Math.min(Math.max(1, limit), MAX_PAGE)));
    params.set('offset', String(Math.max(0, offset)));
    return this.get(`/api/runs?${params.toString()}`);
  }

  run(runId) {
    return this.get(`/api/runs/${encodeURIComponent(runId)}`);
  }

  retries({ limit = DEFAULT_PAGE, offset = 0 } = {}) {
    const params = new URLSearchParams();
    params.set('limit', String(Math.min(Math.max(1, limit), MAX_PAGE)));
    params.set('offset', String(Math.max(0, offset)));
    return this.get(`/api/retries?${params.toString()}`);
  }

  usage() {
    return this.get('/api/usage');
  }

  models() {
    return this.get('/api/models');
  }

  modelPolicy() {
    return this.get('/api/settings/model-policy');
  }

  putModelPolicy(policy) {
    return this.request('/api/settings/model-policy', { method: 'PUT', body: policy, csrf: true });
  }

  deleteModelPolicy() {
    return this.request('/api/settings/model-policy', { method: 'DELETE', csrf: true });
  }

  agentSettings() {
    return this.get('/api/settings/agents');
  }

  /** `body` holds only changed fields: `prompt` (string) and/or `required` (bool, null = config). */
  putAgentSetting(agentId, body) {
    return this.request(`/api/settings/agents/${encodeURIComponent(agentId)}`, { method: 'PUT', body, csrf: true });
  }

  /** Reset to defaults. `supersededSha` names the kept edit (B) on screen: while
   *  the server holds a B, a reset without the matching sha is refused (409). */
  deleteAgentSetting(agentId, supersededSha = null) {
    return this.request(`/api/settings/agents/${encodeURIComponent(agentId)}${shaQuery('superseded_sha', supersededSha)}`,
      { method: 'DELETE', csrf: true });
  }

  /** The edit a changed prompt file replaced (`prompt_superseded`): drop it for good.
   *  `sha` is that B's sha; the server refuses (409) when its B is another one. */
  discardAgentSuperseded(agentId, sha) {
    return this.request(`/api/settings/agents/${encodeURIComponent(agentId)}/superseded${shaQuery('sha', sha)}`,
      { method: 'DELETE', csrf: true });
  }

  /** Roll the agent back to that edit; it runs until the prompt file changes again. Same `sha` rule. */
  restoreAgentSuperseded(agentId, sha) {
    return this.request(`/api/settings/agents/${encodeURIComponent(agentId)}/superseded/restore${shaQuery('sha', sha)}`,
      { method: 'POST', csrf: true });
  }

  repositorySettings() {
    return this.get('/api/settings/repositories');
  }

  /** PR authors' own settings and their feedback: read-only, set by commenting on GitHub. */
  authors({ offset = 0 } = {}) {
    const from = Math.max(0, offset);
    return this.get(`/api/authors?limit=${MAX_PAGE}${from > 0 ? `&offset=${from}` : ''}`);
  }

  /** Feedback paged by row id: the newest page; `beforeId`, the page below it
   *  (newest first); `afterId`, the rows right above it (oldest first). */
  feedback({ limit = DEFAULT_PAGE, beforeId = null, afterId = null } = {}) {
    const params = new URLSearchParams();
    params.set('limit', String(Math.min(Math.max(1, limit), MAX_PAGE)));
    if (Number.isInteger(beforeId) && beforeId > 0) params.set('before_id', String(beforeId));
    if (Number.isInteger(afterId) && afterId > 0) params.set('after_id', String(afterId));
    return this.get(`/api/feedback?${params.toString()}`);
  }

  /** The server checks the name on GitHub and stores the spelling GitHub reports. */
  addRepository(repo) {
    return this.request('/api/settings/repositories', { method: 'POST', body: { repo }, csrf: true });
  }

  /** `repo` is `owner/name`; each segment is encoded on its own so the slash stays a path separator. */
  removeRepository(repo) {
    const [owner = '', name = ''] = String(repo).split('/');
    return this.request(
      `/api/settings/repositories/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`,
      { method: 'DELETE', csrf: true },
    );
  }

  adminSettings() {
    return this.get('/api/settings/admins');
  }

  /** Owner only; the server normalizes the address and refuses the owner and duplicates. */
  addAdmin(email) {
    return this.request('/api/settings/admins', { method: 'POST', body: { email }, csrf: true });
  }

  /** The whole address is one encoded path segment ("@", "/" and "#" included). */
  removeAdmin(email) {
    return this.request(`/api/settings/admins/${encodeURIComponent(String(email))}`, { method: 'DELETE', csrf: true });
  }

  /** Mutation: CSRF header plus same-origin cookie; the server also checks Origin.
   *  `modelPolicy` (optional) pins the ladder for this one retry; `scope`
   *  (optional, `failed` or `all`) says which agents run again, and the server
   *  picks `failed` when the attempt kept results to reuse, else `all`. */
  retry(runId, headSha, modelPolicy = null, scope = null) {
    const body = { head_sha: headSha };
    if (modelPolicy) body.model_policy = modelPolicy;
    if (scope) body.scope = scope;
    return this.request(`/api/runs/${encodeURIComponent(runId)}/retry`, {
      method: 'POST',
      body,
      csrf: true,
    });
  }

  /** The Retrigger page's facts for one PR: current head, latest attempt, what is
   *  queued or running, and whether this session may re-run. Any session. */
  retriggerInfo(repo, pr) {
    const [owner = '', name = ''] = String(repo).split('/');
    return this.get(`/api/retrigger/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/${encodeURIComponent(String(pr))}`);
  }

  /** Mutation (admin): queue a fresh review of `headSha`, the head the page showed,
   *  against `latestRunId`, the attempt it showed (null for none), as of `observedAt`
   *  (the GET's `observed_at`); the server refuses when either moved or a review is
   *  already queued or running. */
  retrigger(repo, pr, headSha, latestRunId, observedAt) {
    return this.request('/api/retrigger', {
      method: 'POST',
      body: { repo, pr, head_sha: headSha, latest_run_id: latestRunId, observed_at: observedAt },
      csrf: true,
    });
  }

  async logout() {
    await this.request('/auth/logout', { method: 'POST', body: {}, csrf: true });
    this.csrfToken = null;
    this.identity = null;
  }
}

/** Trailing-edge coalescer: schedule() during a run marks the work dirty and
 *  re-runs exactly once afterwards, so N events cost at most 2 requests. */
export function createCoalescer(task, { waitMs = 350, setTimeoutImpl, clearTimeoutImpl } = {}) {
  const setTimer = setTimeoutImpl || ((fn, ms) => setTimeout(fn, ms));
  const clearTimer = clearTimeoutImpl || ((handle) => clearTimeout(handle));
  let timer = null;
  let running = false;
  let dirty = false;

  async function run() {
    timer = null;
    if (running) {
      dirty = true;
      return;
    }
    running = true;
    try {
      await task();
    } finally {
      running = false;
      if (dirty) {
        dirty = false;
        schedule();
      }
    }
  }

  function schedule() {
    if (running) {
      dirty = true;
      return;
    }
    if (timer !== null) return;
    timer = setTimer(run, waitMs);
  }

  function cancel() {
    if (timer !== null) {
      clearTimer(timer);
      timer = null;
    }
    dirty = false;
  }

  return { schedule, cancel, flush: run };
}
