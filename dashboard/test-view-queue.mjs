/** The queue table, actually rendered (dom-shim.js), not source-pinned. */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { createDomShim, findAll } from './dom-shim.js';
import { parseHash } from './router.js';
import { renderQueue } from './view-queue.js';

function run(overrides = {}) {
  return {
    id: 'run-1',
    repo: 'owner/name',
    pr: 12,
    head_sha: 'aaaaaaaaaaaa',
    title: 'Fix the timeout',
    url: 'https://github.com/owner/name/pull/12',
    trigger: 'poll',
    retry_of: null,
    status: 'failed',
    stage: null,
    review_url: null,
    error: null,
    created_at: '2026-09-11T06:00:00+00:00',
    updated_at: '2026-09-11T06:05:00+00:00',
    ...overrides,
  };
}

const noop = () => {};

function render(runs, route = parseHash('#history')) {
  const previous = globalThis.document;
  globalThis.document = createDomShim().document;
  try {
    return renderQueue({
      bucket: route.view,
      route,
      runs,
      loading: false,
      error: null,
      retryRunIds: new Set(),
      hasMore: false,
      onFilters: noop,
      onLoadMore: noop,
      onRetryLoad: noop,
    });
  } finally {
    globalThis.document = previous;
  }
}

function row(tree, runId) {
  return findAll(tree, 'tr').find((node) => node.dataset.runId === runId) || null;
}

function reviewText(tree, runId) {
  const reviewCell = findAll(row(tree, runId), 'td').find((node) => node.dataset.label === 'Review');
  return reviewCell ? reviewCell.textContent : null;
}

describe('the queue table, rendered through view-queue.js', () => {
  it('says a baseline row was never reviewed, and every other trigger had no review posted', () => {
    const tree = render([
      run({ id: 'run-baseline', trigger: 'baseline' }),
      run({ id: 'run-poll', trigger: 'poll' }),
      run({ id: 'run-retrigger', trigger: 'retrigger' }),
      run({ id: 'run-legacy', trigger: 'legacy' }),
    ]);
    assert.equal(reviewText(tree, 'run-baseline'), 'not reviewed');
    assert.equal(reviewText(tree, 'run-poll'), 'no review posted');
    assert.equal(reviewText(tree, 'run-retrigger'), 'no review posted');
    assert.equal(reviewText(tree, 'run-legacy'), 'no review posted');
  });

  it('links an attempt with the queue filters and origin, so Back restores them', () => {
    const tree = render([run({ id: 'run-9' })], parseHash('#attention?repo=owner%2Fname&q=timeout'));
    const link = findAll(row(tree, 'run-9'), 'a').find((node) => node.getAttribute('href').startsWith('#run/'));
    assert.equal(link.getAttribute('href'), '#run/run-9?repo=owner%2Fname&q=timeout&from=attention');
  });
});
