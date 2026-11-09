/** The attempt detail, actually rendered (dom-shim.js), not source-pinned. */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { createDomShim, walkElements } from './dom-shim.js';
import { parseHash } from './router.js';
import { renderRunDetail } from './view-run.js';

const BASELINE_NOTE = 'Baseline record: this pull request was never reviewed by the service.';

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
    status: 'succeeded',
    stage: null,
    agents: [],
    review_url: null,
    error: null,
    created_at: '2026-09-11T06:00:00+00:00',
    updated_at: '2026-09-11T06:05:00+00:00',
    ...overrides,
  };
}

const noop = () => {};

function render(attempt) {
  const previous = globalThis.document;
  globalThis.document = createDomShim().document;
  try {
    return renderRunDetail({
      run: attempt,
      route: parseHash('#run/run-1'),
      retryState: null,
      parentRun: null,
      childRun: null,
      events: [],
      sessionStartedAt: '2026-09-11T06:00:00+00:00',
      streamPhase: 'live',
      loading: false,
      error: null,
      onBack: noop,
      onRetry: noop,
      onReload: noop,
    });
  } finally {
    globalThis.document = previous;
  }
}

function hasText(tree, text) {
  return [...walkElements(tree)].some((node) => node.textContent === text);
}

describe('the attempt detail, rendered through view-run.js', () => {
  it('marks only a baseline attempt as never reviewed by the service', () => {
    assert.equal(hasText(render(run({ trigger: 'baseline' })), BASELINE_NOTE), true);
    for (const trigger of ['poll', 'retrigger', 'legacy']) {
      assert.equal(hasText(render(run({ trigger })), BASELINE_NOTE), false, trigger);
    }
  });
});
