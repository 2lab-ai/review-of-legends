/** Queue model: bucketing, merge order, filters, paging, event application and
 *  retry-request linkage. */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import {
  ATTENTION_STATUSES,
  activeRetryFor,
  applyRunChange,
  applySourcePage,
  bucketAccepts,
  bucketOf,
  bucketStatuses,
  compareRunsDesc,
  createSources,
  eventsForRun,
  filterRuns,
  findRun,
  hasMore,
  isRetryEvent,
  isSettingEvent,
  isRetryable,
  isSameRetryTarget,
  latestRetryFor,
  mergeRetryRequests,
  retryTargetOf,
  mergeRuns,
  pendingSources,
  pushEvent,
  removeRun,
  repoOptions,
  retryAttemptFor,
  retryChoices,
  retryPanelState,
  retryScopeLabel,
  runChangeFromEvent,
  runIdsWithActiveRetry,
  upsertRun,
} from './runs.js';

function run(overrides = {}) {
  return {
    id: 'run-1',
    repo: 'owner/repo',
    pr: 12,
    head_sha: 'aaaaaaaaaaaa',
    title: 'Fix the ladder',
    url: 'https://github.com/owner/repo/pull/12',
    trigger: 'poll',
    retry_of: null,
    status: 'running',
    stage: null,
    agents: null,
    review_url: null,
    error: null,
    successful_agents: null,
    total_agents: null,
    created_at: '2026-09-11T06:00:00+00:00',
    updated_at: '2026-09-11T06:00:00+00:00',
    ...overrides,
  };
}

describe('buckets', () => {
  it('routes each status to one queue', () => {
    assert.equal(bucketOf(run({ status: 'running' })), 'active');
    for (const status of ATTENTION_STATUSES) {
      assert.equal(bucketOf(run({ status })), 'attention');
    }
    assert.equal(bucketOf(run({ status: 'succeeded' })), 'history');
    assert.equal(bucketOf(run({ status: 'skipped' })), 'history');
  });

  it('asks the API only for the statuses a bucket shows', () => {
    assert.deepEqual(bucketStatuses('active'), ['running']);
    assert.deepEqual(bucketStatuses('attention'), ['failed', 'interrupted', 'stale']);
    assert.deepEqual(bucketStatuses('history'), [null]);
  });

  it('keeps history a complete ledger', () => {
    assert.equal(bucketAccepts('history', run({ status: 'running' })), true);
    assert.equal(bucketAccepts('active', run({ status: 'failed' })), false);
  });

  it('offers retry exactly for the unreviewed outcomes', () => {
    assert.equal(isRetryable(run({ status: 'failed' })), true);
    assert.equal(isRetryable(run({ status: 'interrupted' })), true);
    assert.equal(isRetryable(run({ status: 'stale' })), true);
    assert.equal(isRetryable(run({ status: 'running' })), false);
    assert.equal(isRetryable(run({ status: 'succeeded' })), false);
    assert.equal(isRetryable(null), false);
  });
});

describe('merging', () => {
  it('orders newest first and breaks ties deterministically', () => {
    const older = run({ id: 'a', created_at: '2026-09-11T06:00:00+00:00' });
    const newer = run({ id: 'b', created_at: '2026-09-11T07:00:00+00:00' });
    const tie = run({ id: 'c', created_at: '2026-09-11T07:00:00+00:00' });
    // The store writes second-resolution timestamps, so ties are common. Attempt
    // ids are uuid4 hex and carry no time, so the tie-break is arbitrary by
    // design - it only has to be stable, which is what this pins.
    assert.deepEqual(mergeRuns([older, newer, tie]).map((r) => r.id), ['c', 'b', 'a']);
    assert.deepEqual(mergeRuns([tie, older, newer]).map((r) => r.id), ['c', 'b', 'a']);
    assert.equal(compareRunsDesc(newer, older) < 0, true);
  });

  it('deduplicates by id with the later list winning', () => {
    const first = run({ id: 'a', status: 'running' });
    const second = run({ id: 'a', status: 'failed' });
    const merged = mergeRuns([first], [second]);
    assert.equal(merged.length, 1);
    assert.equal(merged[0].status, 'failed');
  });

  it('upserts, removes and finds by id', () => {
    const list = mergeRuns([run({ id: 'a' })]);
    assert.equal(findRun(upsertRun(list, run({ id: 'b' })), 'b').id, 'b');
    assert.equal(removeRun(list, 'a').length, 0);
    assert.equal(findRun(list, 'missing'), null);
  });
});

describe('filters', () => {
  const rows = [
    run({ id: 'a', repo: 'owner/repo', pr: 12, title: 'Fix the ladder', error: null }),
    run({ id: 'b', repo: 'other/repo', pr: 34, title: '리뷰 재시도 처리', error: 'llmux timeout' }),
    run({ id: 'c', repo: 'owner/repo', pr: 56, title: 'Bump deps', stage: 'collecting agents' }),
  ];

  it('narrows by repository', () => {
    assert.deepEqual(filterRuns(rows, { repo: 'owner/repo' }).map((r) => r.id), ['a', 'c']);
  });

  it('matches a PR number with or without the hash', () => {
    assert.deepEqual(filterRuns(rows, { query: '#34' }).map((r) => r.id), ['b']);
    assert.deepEqual(filterRuns(rows, { query: '34' }).map((r) => r.id), ['b']);
  });

  it('searches title, error, stage and sha case-insensitively', () => {
    assert.deepEqual(filterRuns(rows, { query: 'LADDER' }).map((r) => r.id), ['a']);
    assert.deepEqual(filterRuns(rows, { query: 'timeout' }).map((r) => r.id), ['b']);
    assert.deepEqual(filterRuns(rows, { query: 'collecting' }).map((r) => r.id), ['c']);
    assert.deepEqual(filterRuns(rows, { query: '재시도' }).map((r) => r.id), ['b']);
  });

  it('combines repository and text', () => {
    assert.deepEqual(filterRuns(rows, { repo: 'owner/repo', query: 'bump' }).map((r) => r.id), ['c']);
    assert.deepEqual(filterRuns(rows, { repo: 'owner/repo', query: 'timeout' }), []);
  });

  it('returns everything for empty filters and lists the repositories present', () => {
    assert.equal(filterRuns(rows, {}).length, 3);
    assert.equal(filterRuns(rows, { repo: '', query: '   ' }).length, 3);
    assert.deepEqual(repoOptions(rows), ['other/repo', 'owner/repo']);
  });
});

describe('pagination sources', () => {
  it('tracks one cursor per status query', () => {
    const sources = createSources('attention');
    assert.equal(sources.length, 3);
    assert.equal(hasMore(sources), true);
    assert.equal(pendingSources(sources).length, 3);
  });

  it('marks a source done when the server reports no next offset', () => {
    let sources = createSources('attention');
    sources = applySourcePage(sources, 'failed', 50);
    sources = applySourcePage(sources, 'interrupted', null);
    sources = applySourcePage(sources, 'stale', null);
    assert.equal(hasMore(sources), true);
    assert.deepEqual(pendingSources(sources).map((s) => s.status), ['failed']);
    sources = applySourcePage(sources, 'failed', null);
    assert.equal(hasMore(sources), false);
  });

  it('exhausts a single-source bucket', () => {
    const sources = applySourcePage(createSources('history'), null, null);
    assert.equal(hasMore(sources), false);
  });
});

describe('events applied to runs', () => {
  it('builds a complete row from run_started without a refetch', () => {
    const change = runChangeFromEvent({
      id: 10,
      kind: 'run_started',
      run_id: 'new-run',
      created_at: '2026-09-11T08:00:00+00:00',
      payload: {
        status: 'running',
        repo: 'owner/repo',
        pr: 77,
        head_sha: 'bbbbbbbbbbbb',
        title: 'retry me',
        url: 'https://github.com/owner/repo/pull/77',
        trigger: 'retry',
        retry_of: 'run-1',
      },
    });
    assert.equal(change.kind, 'insert');
    assert.equal(change.run.id, 'new-run');
    assert.equal(change.run.pr, 77);
    assert.equal(change.run.retry_of, 'run-1');
    assert.equal(change.run.created_at, '2026-09-11T08:00:00+00:00');
  });

  it('patches only the fields run_updated carried', () => {
    const change = runChangeFromEvent({
      id: 11,
      kind: 'run_updated',
      run_id: 'run-1',
      created_at: '2026-09-11T08:05:00+00:00',
      payload: { status: 'failed', error: 'quorum not reached', successful_agents: 1, total_agents: 3 },
    });
    assert.equal(change.kind, 'patch');
    assert.deepEqual(change.patch, {
      updated_at: '2026-09-11T08:05:00+00:00',
      status: 'failed',
      error: 'quorum not reached',
      successful_agents: 1,
      total_agents: 3,
    });
    assert.equal('stage' in change.patch, false);
  });

  it('carries the llm record on run_updated so the header fills in live', () => {
    const llm = { policy: { model: 'grok-4.6', effort: 'xhigh', fallback_models: [], source: 'dashboard' }, totals: { input_tokens: 5, output_tokens: 1, models_used: ['grok-4.6'] } };
    const change = runChangeFromEvent({
      id: 12,
      kind: 'run_updated',
      run_id: 'run-1',
      created_at: '2026-09-11T08:06:00+00:00',
      payload: { status: 'running', stage: 'agents', llm },
    });
    assert.equal(change.kind, 'patch');
    assert.deepEqual(change.patch.llm, llm);
  });

  it('carries the kept agent ids on run_updated so the retry buttons switch live', () => {
    const change = runChangeFromEvent({
      id: 13,
      kind: 'run_updated',
      run_id: 'run-1',
      created_at: '2026-09-11T08:07:00+00:00',
      payload: { status: 'failed', stage: 'quorum', reusable_agents: ['correctness', 'security'] },
    });
    assert.deepEqual(change.patch.reusable_agents, ['correctness', 'security']);
  });

  it('ignores retry events and unknown kinds for run changes', () => {
    assert.equal(runChangeFromEvent({ id: 1, kind: 'retry_enqueued', run_id: null, payload: {} }), null);
    assert.equal(runChangeFromEvent(null), null);
    assert.equal(isRetryEvent({ kind: 'retry_claimed' }), true);
    assert.equal(isRetryEvent({ kind: 'run_updated' }), false);
    assert.equal(isSettingEvent({ kind: 'setting_updated' }), true);
    assert.equal(isSettingEvent({ kind: 'setting_deleted' }), true);
    assert.equal(isSettingEvent({ kind: 'run_updated' }), false);
    assert.equal(isSettingEvent(null), false);
  });

  it('drops a row from Active once it stops running', () => {
    const rows = [run({ id: 'a', status: 'running' })];
    const change = runChangeFromEvent({
      id: 12, kind: 'run_updated', run_id: 'a', created_at: '2026-09-11T08:05:00+00:00',
      payload: { status: 'failed' },
    });
    const result = applyRunChange(rows, 'active', change);
    assert.deepEqual(result.runs, []);
    assert.equal(result.missing, false);
  });

  it('keeps that same row in History with the new status', () => {
    const rows = [run({ id: 'a', status: 'running' })];
    const change = runChangeFromEvent({
      id: 12, kind: 'run_updated', run_id: 'a', created_at: '2026-09-11T08:05:00+00:00',
      payload: { status: 'failed' },
    });
    const result = applyRunChange(rows, 'history', change);
    assert.equal(result.runs[0].status, 'failed');
  });

  it('reports a patch for an unseen run so the caller refetches', () => {
    const result = applyRunChange([], 'attention', {
      kind: 'patch', runId: 'ghost', patch: { status: 'failed' },
    });
    assert.equal(result.missing, true);
    assert.deepEqual(result.runs, []);
  });

  it('does not insert a new attempt into a bucket that excludes it', () => {
    const change = runChangeFromEvent({
      id: 13, kind: 'run_started', run_id: 'x', created_at: '2026-09-11T08:00:00+00:00',
      payload: { status: 'running', repo: 'owner/repo', pr: 1, head_sha: 'c' },
    });
    assert.deepEqual(applyRunChange([], 'attention', change).runs, []);
    assert.equal(applyRunChange([], 'active', change).runs.length, 1);
  });
});

describe('event ring buffer', () => {
  it('collapses a replayed id and keeps ascending order', () => {
    let buffer = [];
    buffer = pushEvent(buffer, { id: 2, run_id: 'a' });
    buffer = pushEvent(buffer, { id: 1, run_id: 'a' });
    buffer = pushEvent(buffer, { id: 2, run_id: 'a', kind: 'replayed' });
    assert.deepEqual(buffer.map((e) => e.id), [1, 2]);
    assert.equal(buffer[1].kind, 'replayed');
  });

  it('is bounded and drops the oldest first', () => {
    let buffer = [];
    for (let id = 1; id <= 5; id += 1) buffer = pushEvent(buffer, { id }, 3);
    assert.deepEqual(buffer.map((e) => e.id), [3, 4, 5]);
  });

  it('ignores frames without a numeric id', () => {
    assert.deepEqual(pushEvent([], { id: 'x' }), []);
    assert.deepEqual(pushEvent([], null), []);
  });

  it('selects the trail of one attempt', () => {
    const buffer = [{ id: 1, run_id: 'a' }, { id: 2, run_id: 'b' }, { id: 3, run_id: null }];
    assert.deepEqual(eventsForRun(buffer, 'a').map((e) => e.id), [1]);
    assert.deepEqual(eventsForRun(buffer, ''), []);
  });
});

describe('retry requests', () => {
  const HEAD_A = 'aaaaaaaaaaaa';
  const HEAD_B = 'bbbbbbbbbbbb';

  // Attempts A and B are two failed attempts of the SAME commit, which is exactly
  // what the store deduplicates a retry request on (dashboard_store.py:69).
  const attemptA = run({ id: 'run-a', status: 'failed', pr: 12, head_sha: HEAD_A });
  const attemptB = run({ id: 'run-b', status: 'failed', pr: 12, head_sha: HEAD_A });
  const otherHead = run({ id: 'run-c', status: 'failed', pr: 12, head_sha: HEAD_B });
  const otherPr = run({ id: 'run-d', status: 'failed', pr: 99, head_sha: HEAD_A });
  const otherRepo = run({ id: 'run-e', status: 'failed', pr: 12, head_sha: HEAD_A, repo: 'other/repo' });

  function request(overrides) {
    return {
      id: 'req',
      status: 'queued',
      repo: 'owner/repo',
      pr: 12,
      head_sha: HEAD_A,
      retry_of: 'run-a',
      created_at: '2026-09-11T06:00:00+00:00',
      run_id: null,
      error: null,
      ...overrides,
    };
  }

  const requests = [
    request({ id: 'req-old', status: 'failed', created_at: '2026-09-11T05:00:00+00:00' }),
    request({ id: 'req-new', status: 'queued', created_at: '2026-09-11T06:00:00+00:00' }),
    request({
      id: 'req-other', status: 'running', pr: 40, head_sha: 'cccccccccccc',
      retry_of: 'run-z', created_at: '2026-09-11T06:30:00+00:00',
    }),
  ];

  it('merges newest first, and the LAST list wins on an id collision', () => {
    const merged = mergeRetryRequests(requests, [request({ id: 'req-new', status: 'succeeded' })]);
    assert.equal(merged.length, 3);
    assert.equal(merged[0].id, 'req-other');
    assert.equal(
      merged.find((r) => r.id === 'req-new').status,
      'succeeded',
      'fresh must be passed last - applyRetryUpdates is the named-argument form',
    );
  });

  it('finds the active request for an attempt and ignores terminal ones', () => {
    assert.equal(activeRetryFor(requests, attemptA).id, 'req-new');
    assert.equal(activeRetryFor([requests[0]], attemptA), null);
    assert.equal(latestRetryFor(requests, attemptA).id, 'req-new');
  });

  it('shows the shared dispatch on a sibling attempt of the same commit', () => {
    // The request records retry_of=run-a, but it IS the dispatch for this commit,
    // so attempt B must show it queued instead of offering a second dispatch.
    assert.equal(activeRetryFor(requests, attemptB).id, 'req-new');
    assert.equal(latestRetryFor(requests, attemptB).id, 'req-new');
  });

  it('never matches a different commit, pull request or repository', () => {
    assert.equal(activeRetryFor(requests, otherHead), null);
    assert.equal(activeRetryFor(requests, otherPr), null);
    assert.equal(activeRetryFor(requests, otherRepo), null);
  });

  it('refuses to match on an incomplete identity', () => {
    assert.equal(activeRetryFor(requests, run({ id: 'x', head_sha: '' })), null);
    assert.equal(activeRetryFor(requests, run({ id: 'x', pr: null })), null);
    assert.equal(activeRetryFor([request({ head_sha: '' })], run({ head_sha: '' })), null);
    assert.equal(isSameRetryTarget(null, attemptA), false);
    assert.equal(retryTargetOf(run({ pr: 0 })), null);
    assert.deepEqual(retryTargetOf(attemptA), { repo: 'owner/repo', pr: 12, head_sha: HEAD_A });
  });

  it('marks every loaded attempt that shares an in-flight dispatch', () => {
    const ids = runIdsWithActiveRetry(requests, [attemptA, attemptB, otherHead, otherPr]);
    assert.equal(ids.has('run-a'), true);
    assert.equal(ids.has('run-b'), true, 'the sibling attempt is flagged too');
    assert.equal(ids.has('run-c'), false);
    assert.equal(ids.has('run-d'), false);
  });

  it('links a request to the attempt it produced, for both siblings', () => {
    const child = run({ id: 'child', retry_of: 'run-a' });
    const linked = [request({ id: 'req-done', status: 'succeeded', run_id: 'child' })];
    assert.equal(retryAttemptFor([child], linked, attemptA).id, 'child');
    assert.equal(retryAttemptFor([child], linked, attemptB).id, 'child');
    // Before the request row carries run_id, only the true parent has the lineage.
    assert.equal(retryAttemptFor([child], [], attemptA).id, 'child');
    assert.equal(retryAttemptFor([child], [], attemptB), null);
    assert.equal(retryAttemptFor([], [], attemptA), null);
  });
});

describe('retry panel state', () => {
  const failed = run({ status: 'failed' });

  it('is ready with no request recorded', () => {
    const state = retryPanelState({ run: failed });
    assert.equal(state.phase, 'ready');
    assert.equal(state.blocked, false);
  });

  it('hides itself for an attempt that cannot be retried', () => {
    assert.equal(retryPanelState({ run: run({ status: 'succeeded' }) }).phase, 'not-retryable');
    assert.equal(retryPanelState({ run: null }).phase, 'hidden');
  });

  it('blocks the button while the POST is in flight', () => {
    const state = retryPanelState({ run: failed, local: { phase: 'dispatching' } });
    assert.equal(state.phase, 'dispatching');
    assert.equal(state.blocked, true);
  });

  it('blocks the button while a durable request is queued or claimed', () => {
    const queued = retryPanelState({ run: failed, request: { id: 'r', status: 'queued' } });
    assert.equal(queued.phase, 'queued');
    assert.equal(queued.blocked, true);
    const claimed = retryPanelState({ run: failed, request: { id: 'r', status: 'running' } });
    assert.equal(claimed.phase, 'claimed');
    assert.equal(claimed.blocked, true);
  });

  it('keeps the button blocked after a rejection so the error stays readable', () => {
    const state = retryPanelState({
      run: failed,
      local: { phase: 'rejected', error: { title: 'The pull request moved' } },
    });
    assert.equal(state.phase, 'rejected');
    assert.equal(state.blocked, true);
    assert.equal(state.error.title, 'The pull request moved');
  });

  it('re-enables dispatch after a failed or stale dispatch', () => {
    assert.equal(retryPanelState({ run: failed, request: { id: 'r', status: 'failed' } }).blocked, false);
    assert.equal(retryPanelState({ run: failed, request: { id: 'r', status: 'stale' } }).blocked, false);
  });

  it('does not re-offer a dispatch that already completed', () => {
    const state = retryPanelState({ run: failed, request: { id: 'r', status: 'succeeded' } });
    assert.equal(state.phase, 'dispatched');
    assert.equal(state.blocked, true);
  });
});

describe('retry buttons', () => {
  const agents = ['correctness', 'security', 'tests', 'red-team'].map((id) => ({ id, status: 'done' }));

  it('offers the failed agents first and every agent second when results were kept', () => {
    const choices = retryChoices(run({ status: 'failed', agents, reusable_agents: ['correctness', 'security'] }));
    assert.deepEqual(choices.reused, ['correctness', 'security']);
    assert.equal(choices.rerun, 2);
    assert.deepEqual(choices.primary, { scope: 'failed', label: 'Retry failed agents (2)' });
    assert.deepEqual(choices.secondary, { scope: 'all', label: 'Retry all agents' });
  });

  it('counts from the recorded total when the attempt has no agent rows', () => {
    const choices = retryChoices(run({ status: 'failed', agents: null, total_agents: 5, reusable_agents: ['tests'] }));
    assert.equal(choices.primary.label, 'Retry failed agents (4)');
  });

  it('offers the whole review only when nothing was kept, as before', () => {
    for (const kept of [[], null, undefined, 'correctness', [7, '']]) {
      const choices = retryChoices(run({ status: 'interrupted', agents, reusable_agents: kept }));
      assert.deepEqual(choices.reused, []);
      assert.deepEqual(choices.primary, { scope: 'all', label: 'Confirm retry of the entire review' });
      assert.equal(choices.secondary, null);
    }
  });

  it('names the pinned model on either button', () => {
    const kept = retryChoices(run({ status: 'failed', agents, reusable_agents: ['tests'] }), 'grok-4.7[1m]');
    assert.equal(kept.primary.label, 'Retry failed agents (3) with grok-4.7[1m]');
    assert.equal(kept.secondary.label, 'Retry all agents with grok-4.7[1m]');
    assert.equal(retryChoices(run({ status: 'failed' }), 'grok-4.7[1m]').primary.label,
      'Confirm retry of the entire review with grok-4.7[1m]');
  });

  it('describes the scope a request was created with', () => {
    assert.equal(retryScopeLabel('failed'), 'failed agents only; the others are reused');
    assert.equal(retryScopeLabel('all'), 'every agent');
    assert.equal(retryScopeLabel(null), 'every agent', 'a request queued before scopes existed');
  });
});

describe('retry scope wiring (source pin)', () => {
  // view-run.js and app.js are DOM-bound and have no unit harness (see
  // test-roles.mjs). Pin each link that carries a clicked button's scope to the
  // POST, so a callback that drops it cannot turn "Retry all agents" into the
  // server's default (failed agents only, when results were kept).
  const source = (file) => readFileSync(fileURLToPath(new URL(file, import.meta.url)), 'utf8');

  it('each retry button passes its own scope', () => {
    const view = source('./view-run.js');
    assert.match(view, /on: \{ click: \(\) => onRetry\(choices\.primary\.scope\) \}/);
    assert.match(view, /on: \{ click: \(\) => onRetry\(choices\.secondary\.scope\) \}/);
  });

  it('app.js forwards that scope to the retry request', () => {
    const app = source('./app.js');
    assert.match(app, /onRetry: \(scope\) => dispatchRetry\(scope\),/);
    const start = app.indexOf('async function dispatchRetry(scope');
    assert.notEqual(start, -1, 'dispatchRetry takes the scope');
    const dispatch = app.slice(start);
    assert.match(dispatch.slice(0, dispatch.indexOf('\n}\n')), /api\.retry\(run\.id, run\.head_sha, override, scope\)/);
  });
});
