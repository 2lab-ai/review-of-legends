/** API client: contract shape, CSRF discipline, request coalescing. */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { ApiClient, createCoalescer } from './api.js';

function jsonResponse(status, body) {
  return {
    status,
    ok: status >= 200 && status < 300,
    text: async () => (body === undefined ? '' : JSON.stringify(body)),
  };
}

function recorder(handler) {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, options });
    return handler ? handler(url, options) : jsonResponse(200, {});
  };
  return { calls, fetchImpl };
}

describe('request construction', () => {
  it('bounds limit and offset on the runs query', async () => {
    const { calls, fetchImpl } = recorder(() => jsonResponse(200, { runs: [], next_offset: null }));
    const api = new ApiClient({ fetchImpl });
    await api.runs({ limit: 5000, offset: -10 });
    assert.match(calls[0].url, /limit=200/);
    assert.match(calls[0].url, /offset=0/);
  });

  it('omits absent filters and encodes the ones present', async () => {
    const { calls, fetchImpl } = recorder(() => jsonResponse(200, {}));
    const api = new ApiClient({ fetchImpl });
    await api.runs({ status: 'failed', repo: 'owner/name' });
    assert.match(calls[0].url, /status=failed/);
    assert.match(calls[0].url, /repo=owner%2Fname/);
    await api.runs({});
    assert.equal(/status=/.test(calls[1].url), false);
    assert.equal(/repo=/.test(calls[1].url), false);
  });

  it('encodes the attempt id into the path', async () => {
    const { calls, fetchImpl } = recorder(() => jsonResponse(200, { run: null }));
    const api = new ApiClient({ fetchImpl });
    await api.run('a/b');
    assert.equal(calls[0].url, '/api/runs/a%2Fb');
  });

  it('reads the retry queue with a bounded page', async () => {
    const { calls, fetchImpl } = recorder(() => jsonResponse(200, { requests: [], next_offset: null }));
    const api = new ApiClient({ fetchImpl });
    await api.retries();
    assert.match(calls[0].url, /^\/api\/retries\?limit=50&offset=0$/);
  });

  it('never follows a redirect and never sends cross-origin credentials', async () => {
    const { calls, fetchImpl } = recorder(() => jsonResponse(200, {}));
    const api = new ApiClient({ fetchImpl });
    await api.usage();
    assert.equal(calls[0].options.redirect, 'error');
    assert.equal(calls[0].options.credentials, 'same-origin');
    assert.equal(calls[0].options.cache, 'no-store');
  });

  it('reads the deployed version from the public /healthz', async () => {
    const health = { status: 'ok', version: 'v1.0.3', auth_configured: true };
    const { calls, fetchImpl } = recorder(() => jsonResponse(200, health));
    const api = new ApiClient({ fetchImpl });
    assert.deepEqual(await api.health(), health);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, '/healthz');
    assert.equal(calls[0].options.method, 'GET');
  });
});

describe('identity and CSRF', () => {
  it('keeps the CSRF token in memory after /api/me', async () => {
    const { fetchImpl } = recorder(() => jsonResponse(200, {
      email: 'owner@company.example', role: 'admin', csrf_token: 'tok-1',
    }));
    const api = new ApiClient({ fetchImpl });
    const identity = await api.me();
    // No owner flag (an older server) never reads as the owner.
    assert.deepEqual(identity, { email: 'owner@company.example', role: 'admin', owner: false });
    assert.equal(api.csrfToken, 'tok-1');
  });

  it('reads the owner flag only when it is literally true', async () => {
    for (const [owner, expected] of [[true, true], [false, false], ['true', false], [1, false]]) {
      const { fetchImpl } = recorder(() => jsonResponse(200, { email: 'a@example.com', role: 'admin', owner, csrf_token: 't' }));
      assert.equal((await new ApiClient({ fetchImpl }).me()).owner, expected, String(owner));
    }
  });

  it('sends the CSRF header and a JSON body on retry', async () => {
    const { calls, fetchImpl } = recorder((url) => (url === '/api/me'
      ? jsonResponse(200, { email: 'owner@company.example', role: 'admin', csrf_token: 'tok-1' })
      : jsonResponse(202, { request: { id: 'req-1', status: 'queued' } })));
    const api = new ApiClient({ fetchImpl });
    await api.me();
    const body = await api.retry('run-1', 'abc123');
    const call = calls[1];
    assert.equal(call.url, '/api/runs/run-1/retry');
    assert.equal(call.options.method, 'POST');
    assert.equal(call.options.headers['X-CSRF-Token'], 'tok-1');
    assert.equal(call.options.headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(call.options.body), { head_sha: 'abc123' });
    assert.equal(body.request.id, 'req-1');
  });

  it('sends the Retrigger snapshot: head, displayed attempt and observed_at', async () => {
    const { calls, fetchImpl } = recorder((url) => (url === '/api/me'
      ? jsonResponse(200, { email: 'owner@company.example', role: 'admin', csrf_token: 'tok-1' })
      : jsonResponse(202, { request: { id: 'req-1', status: 'queued' } })));
    const api = new ApiClient({ fetchImpl });
    await api.me();
    await api.retrigger('owner/repo', 7, 'abc123', 'run-9', '2026-10-15T17:00:00+00:00');
    assert.equal(calls[1].url, '/api/retrigger');
    assert.equal(calls[1].options.headers['X-CSRF-Token'], 'tok-1');
    assert.deepEqual(JSON.parse(calls[1].options.body), {
      repo: 'owner/repo', pr: 7, head_sha: 'abc123', latest_run_id: 'run-9', observed_at: '2026-10-15T17:00:00+00:00',
    });
  });

  it('sends the retry scope and the pinned model when given', async () => {
    const { calls, fetchImpl } = recorder((url) => (url === '/api/me'
      ? jsonResponse(200, { email: 'owner@company.example', role: 'admin', csrf_token: 'tok-1' })
      : jsonResponse(202, { request: { id: 'req-1', status: 'queued', scope: 'failed' } })));
    const api = new ApiClient({ fetchImpl });
    await api.me();
    const policy = { model: 'grok-4.7[1m]', effort: '', fallback_models: [] };
    await api.retry('run-1', 'abc123', null, 'failed');
    await api.retry('run-1', 'abc123', policy, 'all');
    assert.deepEqual(JSON.parse(calls[1].options.body), { head_sha: 'abc123', scope: 'failed' });
    assert.deepEqual(JSON.parse(calls[2].options.body), { head_sha: 'abc123', model_policy: policy, scope: 'all' });
  });

  it('adds and removes a repository with the CSRF header, encoding each path segment', async () => {
    const { calls, fetchImpl } = recorder((url) => (url === '/api/me'
      ? jsonResponse(200, { email: 'owner@company.example', role: 'admin', csrf_token: 'tok-1' })
      : jsonResponse(200, { repositories: [] })));
    const api = new ApiClient({ fetchImpl });
    await api.me();
    await api.repositorySettings();
    await api.addRepository('acme-corp/MiniMoon');
    await api.removeRepository('acme-corp/my.repo#1');
    assert.equal(calls[1].url, '/api/settings/repositories');
    assert.equal(calls[1].options.method, 'GET');
    assert.equal(calls[2].options.method, 'POST');
    assert.equal(calls[2].options.headers['X-CSRF-Token'], 'tok-1');
    assert.deepEqual(JSON.parse(calls[2].options.body), { repo: 'acme-corp/MiniMoon' });
    assert.equal(calls[3].url, '/api/settings/repositories/acme-corp/my.repo%231');
    assert.equal(calls[3].options.method, 'DELETE');
    assert.equal(calls[3].options.headers['X-CSRF-Token'], 'tok-1');
  });

  it('lists, adds and removes an admin with the CSRF header, the address as one encoded segment', async () => {
    const { calls, fetchImpl } = recorder((url) => (url === '/api/me'
      ? jsonResponse(200, { email: 'owner@company.example', role: 'admin', owner: true, csrf_token: 'tok-1' })
      : jsonResponse(200, { admins: [] })));
    const api = new ApiClient({ fetchImpl });
    await api.me();
    await api.adminSettings();
    await api.addAdmin('ops@example.com');
    await api.removeAdmin('a/b#c@example.com');
    assert.equal(calls[1].url, '/api/settings/admins');
    assert.equal(calls[1].options.method, 'GET');
    assert.equal(calls[2].url, '/api/settings/admins');
    assert.equal(calls[2].options.method, 'POST');
    assert.equal(calls[2].options.headers['X-CSRF-Token'], 'tok-1');
    assert.deepEqual(JSON.parse(calls[2].options.body), { email: 'ops@example.com' });
    assert.equal(calls[3].url, '/api/settings/admins/a%2Fb%23c%40example.com');
    assert.equal(calls[3].options.method, 'DELETE');
    assert.equal(calls[3].options.headers['X-CSRF-Token'], 'tok-1');
  });

  it('binds Discard B, Rollback to B and Reset to the B on screen by its sha, encoded in the query', async () => {
    const { calls, fetchImpl } = recorder((url) => (url === '/api/me'
      ? jsonResponse(200, { email: 'owner@company.example', role: 'admin', csrf_token: 'tok-1' })
      : jsonResponse(200, { id: 'a/b' })));
    const api = new ApiClient({ fetchImpl });
    await api.me();
    await api.discardAgentSuperseded('a/b', 'x&y=1');
    await api.restoreAgentSuperseded('a/b', 'abc');
    await api.deleteAgentSetting('a/b', 'x&y=1');
    await api.deleteAgentSetting('a/b', null);
    assert.deepEqual(calls.slice(1).map((call) => [call.options.method, call.url]), [
      ['DELETE', '/api/settings/agents/a%2Fb/superseded?sha=x%26y%3D1'],
      ['POST', '/api/settings/agents/a%2Fb/superseded/restore?sha=abc'],
      ['DELETE', '/api/settings/agents/a%2Fb?superseded_sha=x%26y%3D1'],
      ['DELETE', '/api/settings/agents/a%2Fb'],
    ]);
    assert.ok(calls.slice(1).every((call) => call.options.headers['X-CSRF-Token'] === 'tok-1'));
  });

  it('refuses to mutate without a token instead of sending an unprotected request', async () => {
    const { calls, fetchImpl } = recorder(() => jsonResponse(202, {}));
    const api = new ApiClient({ fetchImpl });
    await assert.rejects(() => api.retry('run-1', 'abc'), (error) => error.status === 403);
    assert.equal(calls.length, 0);
  });

  it('clears the identity on logout', async () => {
    const { fetchImpl } = recorder((url) => (url === '/api/me'
      ? jsonResponse(200, { email: 'owner@company.example', role: 'admin', csrf_token: 'tok-1' })
      : jsonResponse(204)));
    const api = new ApiClient({ fetchImpl });
    await api.me();
    await api.logout();
    assert.equal(api.csrfToken, null);
    assert.equal(api.identity, null);
  });
});

describe('failure mapping', () => {
  it('turns a non-2xx into an ApiError carrying the parsed body', async () => {
    const { fetchImpl } = recorder(() => jsonResponse(409, { error: 'head moved' }));
    const api = new ApiClient({ fetchImpl });
    await assert.rejects(() => api.run('x'), (error) => {
      assert.equal(error.status, 409);
      assert.deepEqual(error.body, { error: 'head moved' });
      return true;
    });
  });

  it('turns a transport failure into status 0', async () => {
    const api = new ApiClient({ fetchImpl: async () => { throw new TypeError('offline'); } });
    await assert.rejects(() => api.usage(), (error) => error.status === 0);
  });

  it('tolerates an empty or non-JSON body', async () => {
    const api = new ApiClient({
      fetchImpl: async () => ({ status: 200, ok: true, text: async () => 'not json' }),
    });
    assert.equal(await api.usage(), null);
  });
});

describe('coalescing', () => {
  it('shares one promise between identical in-flight GETs', async () => {
    let resolve;
    let calls = 0;
    const api = new ApiClient({
      fetchImpl: () => {
        calls += 1;
        return new Promise((r) => { resolve = r; });
      },
    });
    const a = api.runs({ status: 'failed' });
    const b = api.runs({ status: 'failed' });
    assert.equal(calls, 1);
    resolve(jsonResponse(200, { runs: [], next_offset: null }));
    await Promise.all([a, b]);
    // Once settled the entry is released, so a later refresh really refetches.
    api.runs({ status: 'failed' });
    assert.equal(calls, 2);
  });

  it('does not share different URLs', async () => {
    const { calls, fetchImpl } = recorder(() => jsonResponse(200, {}));
    const api = new ApiClient({ fetchImpl });
    await Promise.all([api.runs({ status: 'failed' }), api.runs({ status: 'stale' })]);
    assert.equal(calls.length, 2);
  });

  it('collapses a burst of scheduled refreshes into one call', async () => {
    const timers = [];
    let runs = 0;
    const coalescer = createCoalescer(async () => { runs += 1; }, {
      waitMs: 100,
      setTimeoutImpl: (fn) => { timers.push(fn); return timers.length - 1; },
      clearTimeoutImpl: () => {},
    });
    coalescer.schedule();
    coalescer.schedule();
    coalescer.schedule();
    assert.equal(timers.length, 1);
    await timers[0]();
    assert.equal(runs, 1);
  });

  it('re-runs once when work was requested while running', async () => {
    const timers = [];
    let runs = 0;
    let release;
    const coalescer = createCoalescer(() => {
      runs += 1;
      return new Promise((resolve) => { release = resolve; });
    }, {
      waitMs: 100,
      setTimeoutImpl: (fn) => { timers.push(fn); return timers.length - 1; },
      clearTimeoutImpl: () => {},
    });
    coalescer.schedule();
    const first = timers[0]();
    coalescer.schedule();
    coalescer.schedule();
    release();
    await first;
    assert.equal(runs, 1);
    assert.equal(timers.length, 2, 'exactly one trailing run was scheduled');
    // The trailing run calls the task again, which hands back a fresh pending
    // promise; release that one too or the await below never settles.
    const second = timers[1]();
    release();
    await second;
    assert.equal(runs, 2);
    assert.equal(timers.length, 2, 'a quiet trailing run does not schedule another');
  });

  it('cancels a pending schedule', async () => {
    const timers = [];
    const cleared = [];
    let runs = 0;
    const coalescer = createCoalescer(async () => { runs += 1; }, {
      waitMs: 100,
      setTimeoutImpl: (fn) => { timers.push(fn); return timers.length - 1; },
      clearTimeoutImpl: (handle) => { cleared.push(handle); },
    });
    coalescer.schedule();
    const pending = timers.length - 1;
    coalescer.cancel();
    assert.deepEqual(cleared, [pending], 'cancel clears the pending timer, exactly once');
    coalescer.schedule();
    assert.equal(timers.length, 2);
    assert.equal(runs, 0);
  });
});
