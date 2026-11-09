/** Hash routing: deep links, filter preservation, hostile input. */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { backRoute, parseHash, serializeHash } from './router.js';

describe('parsing', () => {
  it('defaults to the active queue', () => {
    for (const hash of ['', '#', undefined, null, '#nonsense', '#run']) {
      assert.equal(parseHash(hash).view, 'active');
    }
  });

  it('reads the queue views and accounts', () => {
    assert.equal(parseHash('#active').view, 'active');
    assert.equal(parseHash('#attention').view, 'attention');
    assert.equal(parseHash('#history').view, 'history');
    assert.equal(parseHash('#accounts').view, 'accounts');
    assert.equal(parseHash('#agents').view, 'agents');
    assert.equal(parseHash('#settings').view, 'settings');
    assert.equal(serializeHash({ view: 'agents' }), '#agents');
    assert.equal(parseHash('#run/abc?from=settings').from, 'active');
  });

  it('reads a retrigger deep link and refuses a bare one (the rest is in test-retrigger.mjs)', () => {
    const route = parseHash('#retrigger/owner/repo/42');
    assert.equal(route.view, 'retrigger');
    assert.deepEqual(route.target, { repo: 'owner/repo', pr: 42 });
    assert.equal(parseHash('#retrigger').view, 'active');
    assert.equal(parseHash('#retrigger?repo=x').view, 'active');
  });

  it('reads an attempt deep link', () => {
    const route = parseHash('#run/6f1c2d3e4f5a');
    assert.equal(route.view, 'run');
    assert.equal(route.runId, '6f1c2d3e4f5a');
  });

  it('refuses a run id that is not an opaque token', () => {
    for (const bad of ['#run/../etc/passwd', '#run/has space', '#run/', `#run/${'a'.repeat(65)}`, '#run/a%2Fb']) {
      const route = parseHash(bad);
      assert.equal(route.view, 'active', bad);
      assert.equal(route.runId, null, bad);
    }
  });

  it('decodes filters, including a slash in a repository name', () => {
    const route = parseHash('#history?repo=owner%2Fname&q=llmux%20timeout');
    assert.equal(route.repo, 'owner/name');
    assert.equal(route.query, 'llmux timeout');
  });

  it('decodes + as a space and survives broken percent escapes', () => {
    assert.equal(parseHash('#history?q=two+words').query, 'two words');
    assert.equal(parseHash('#history?q=%E0%A4%A').query, '');
  });

  it('ignores an unknown from value', () => {
    assert.equal(parseHash('#run/abc?from=accounts').from, 'active');
    assert.equal(parseHash('#run/abc?from=attention').from, 'attention');
  });
});

describe('serialising', () => {
  it('round-trips a filtered queue', () => {
    const route = { view: 'history', repo: 'owner/name', query: 'llmux timeout' };
    const hash = serializeHash(route);
    assert.equal(hash, '#history?repo=owner%2Fname&q=llmux%20timeout');
    const parsed = parseHash(hash);
    assert.equal(parsed.view, 'history');
    assert.equal(parsed.repo, 'owner/name');
    assert.equal(parsed.query, 'llmux timeout');
  });

  it('omits empty filters', () => {
    assert.equal(serializeHash({ view: 'active', repo: '', query: '' }), '#active');
  });

  // An attempt link is built the way view-queue.js titleCell builds it
  // (test-view-queue.mjs asserts the rendered href itself).
  const attemptHash = (runId, route) => serializeHash({
    view: 'run',
    runId,
    repo: route.repo,
    query: route.query,
    from: route.view,
  });

  it('keeps filters and origin on an attempt link so Back restores them', () => {
    const queue = parseHash('#attention?repo=owner%2Fname&q=timeout');
    const hash = attemptHash('run-9', queue);
    assert.equal(hash, '#run/run-9?repo=owner%2Fname&q=timeout&from=attention');
    const back = backRoute(parseHash(hash));
    assert.equal(serializeHash(back), '#attention?repo=owner%2Fname&q=timeout');
  });

  it('falls back to the active queue when the origin was not a queue', () => {
    const hash = attemptHash('run-9', parseHash('#accounts'));
    assert.equal(serializeHash(backRoute(parseHash(hash))), '#active');
  });
});
