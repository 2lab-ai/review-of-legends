/** Retrigger page model, the sign-in return hash, and the app.js seams that use them. */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import {
  acknowledgeRefusal,
  beginRetriggerLoad,
  canDispatchRetrigger,
  isCurrentRetriggerLoad,
  reconcileRetriggerInfo,
  retriggerChildRun,
  retriggerPanelState,
  retriggerRefreshWanted,
  retriggerTarget,
  settleRetriggerDispatch,
  settleRetriggerLoad,
} from './retrigger.js';
import { RETURN_HASH_KEY, rememberReturnHash, returnHashAfterSignIn, safeSessionStorage, takeReturnHash } from './signin-return.js';
import { parseHash, serializeHash } from './router.js';
import { activeRetryFor, latestRetryFor, retryAttemptFor } from './runs.js';
import { renderRetrigger } from './view-retrigger.js';

function info(overrides = {}) {
  return {
    repo: 'owner/repo',
    pr: 7,
    title: 'Add dashboard',
    url: 'https://github.com/owner/repo/pull/7',
    state: 'open',
    draft: false,
    head_sha: 'a'.repeat(40),
    latest_run: { id: 'run-1', status: 'succeeded', trigger: 'poll' },
    active_request: null,
    running_run_id: null,
    busy: false,
    can_retrigger: true,
    ...overrides,
  };
}

function request(overrides = {}) {
  return {
    id: 'req-1',
    status: 'queued',
    repo: 'owner/repo',
    pr: 7,
    head_sha: 'a'.repeat(40),
    retry_of: 'run-1',
    created_at: '2026-10-15T00:00:00+00:00',
    run_id: null,
    error: null,
    kind: 'rereview',
    scope: 'all',
    ...overrides,
  };
}

describe('retrigger panel state', () => {
  it('is hidden without the server answer', () => {
    assert.deepEqual(retriggerPanelState({}), { phase: 'hidden', blocked: true, request: null });
  });

  it('a viewer is always blocked, but sees the same activity phase an admin would', () => {
    // Reviewer MUST-FIX: a viewer must not be forced to a blanket 'read-only'
    // that drops info.active_request / info.running_run_id. Authorization
    // (canAct) only decides whether the button can be pressed; the activity
    // projection itself (phase, request, runId) is the same for both.
    const ready = retriggerPanelState({ info: info(), canAct: false });
    assert.equal(ready.phase, 'ready', 'nothing queued or running: the same phase an admin sees');
    assert.equal(ready.blocked, true, 'a viewer is always blocked, whatever the phase');

    const queuedByServer = retriggerPanelState({ info: info({ active_request: request({ id: 'req-9' }) }), canAct: false });
    assert.equal(queuedByServer.phase, 'queued');
    assert.equal(queuedByServer.request.id, 'req-9');
    assert.equal(queuedByServer.blocked, true);

    const running = retriggerPanelState({ info: info({ running_run_id: 'run-live' }), canAct: false });
    assert.equal(running.phase, 'running');
    assert.equal(running.runId, 'run-live');
    assert.equal(running.blocked, true);

    // A viewer has no local optimistic phase: it never dispatches, so a
    // `local` value (left over from some other state) must not leak through.
    const withStrayLocal = retriggerPanelState({ info: info(), local: { phase: 'dispatching' }, canAct: false });
    assert.equal(withStrayLocal.phase, 'ready');
    assert.equal(withStrayLocal.blocked, true);
  });

  it('blocks while this tab dispatches or after a refusal', () => {
    assert.equal(retriggerPanelState({ info: info(), canAct: true, local: { phase: 'dispatching' } }).blocked, true);
    const rejected = retriggerPanelState({ info: info(), canAct: true, local: { phase: 'rejected', error: { title: 'no' } } });
    assert.equal(rejected.phase, 'rejected');
    assert.equal(rejected.blocked, true);
    assert.equal(rejected.error.title, 'no');
  });

  it('blocks on a queued or claimed request, from the tab queue or from the server', () => {
    const queued = retriggerPanelState({ info: info(), request: request(), canAct: true });
    assert.equal(queued.phase, 'queued');
    assert.equal(queued.blocked, true);
    assert.equal(queued.request.id, 'req-1');
    assert.equal(retriggerPanelState({ info: info(), request: request({ status: 'running' }), canAct: true }).phase, 'claimed');
    // The server saw a request for another head of the same PR.
    const other = retriggerPanelState({ info: info({ active_request: request({ id: 'req-9', head_sha: 'b'.repeat(40) }) }), canAct: true });
    assert.equal(other.phase, 'queued');
    assert.equal(other.request.id, 'req-9');
  });

  it('blocks while an attempt of the PR runs and names it', () => {
    const state = retriggerPanelState({ info: info({ running_run_id: 'run-live' }), canAct: true });
    assert.equal(state.phase, 'running');
    assert.equal(state.blocked, true);
    assert.equal(state.runId, 'run-live');
  });

  it('blocks a closed or draft PR and a PR without a head', () => {
    // Reviewer: each branch needs its own fixture - reusing the closed one for
    // all three proves nothing about 'draft' or 'no-head'.
    const fixtures = {
      'not-open': info({ state: 'closed' }),
      draft: info({ draft: true }),
      'no-head': info({ head_sha: '' }),
    };
    for (const [phase, fixture] of Object.entries(fixtures)) {
      const state = retriggerPanelState({ info: fixture, canAct: true });
      assert.equal(state.phase, phase, phase);
      assert.equal(state.blocked, true, phase);
    }
  });

  it('lets an admin run again after a finished, failed or stale request', () => {
    const done = retriggerPanelState({ info: info(), request: request({ status: 'succeeded', run_id: 'run-2' }), canAct: true });
    assert.equal(done.phase, 'dispatched');
    assert.equal(done.blocked, false);
    assert.equal(done.request.run_id, 'run-2');
    for (const [status, phase] of [['failed', 'dispatch-failed'], ['stale', 'dispatch-stale']]) {
      const state = retriggerPanelState({ info: info(), request: request({ status }), canAct: true });
      assert.equal(state.phase, phase);
      assert.equal(state.blocked, false, `${status}: the admin can run again`);
    }
  });

  it('turns the server answer into the identity the queue matches requests on', () => {
    assert.deepEqual(retriggerTarget(info()), { repo: 'owner/repo', pr: 7, head_sha: 'a'.repeat(40) });
    assert.equal(retriggerTarget(info({ head_sha: '' })), null);
    assert.equal(retriggerTarget(info({ pr: 0 })), null);
    assert.equal(retriggerTarget(null), null);
    const target = retriggerTarget(info());
    assert.equal(activeRetryFor([request()], target).id, 'req-1');
    assert.equal(activeRetryFor([request({ head_sha: 'b'.repeat(40) })], target), null, 'another head is another target');
    assert.equal(latestRetryFor([request({ status: 'succeeded' })], target).status, 'succeeded');
    const child = retryAttemptFor([{ id: 'run-2' }], [request({ status: 'succeeded', run_id: 'run-2' })], target);
    assert.equal(child.id, 'run-2', 'the attempt a finished request produced is found by run_id');
    assert.equal(retryAttemptFor([], [], target), null, 'a target without an id never matches by retry_of');
  });
});

// ---------------------------------------------------------------------------
// renderRetrigger: a minimal DOM shim, enough of `el()`'s surface
// (createElement/createElementNS/createTextNode and the node properties it
// touches) to run the real view and inspect the tree it builds. There is no
// jsdom dependency in this package (dashboard/package.json has no runtime
// deps), so this is deliberately small, not a general DOM implementation.
// ---------------------------------------------------------------------------

class FakeNode {
  constructor(tagName, ns = null) {
    this.tagName = tagName.toUpperCase();
    this.namespaceURI = ns;
    this.attributes = new Map();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.className = '';
    this.disabled = false;
    this.checked = false;
    this.selected = false;
    this.value = '';
    this._text = '';
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  addEventListener() {}

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  removeChild(child) {
    this.children = this.children.filter((node) => node !== child);
    return child;
  }

  get firstChild() {
    return this.children[0] || null;
  }

  set textContent(value) {
    this.children = [];
    this._text = String(value);
  }

  get textContent() {
    if (this.children.length === 0) return this._text;
    return this.children.map((child) => child.textContent).join('');
  }

  set innerHTML(_value) {
    // Round 2: `el()` (dom.js) must only ever go through textContent/
    // setAttribute, never an HTML sink. A throwing setter makes any
    // accidental `.innerHTML =` use fail the test loudly instead of
    // silently "working" against a shim that happens to accept strings.
    throw new Error('FakeNode.innerHTML must never be assigned: text is a sink only through textContent');
  }
}

class FakeTextNode {
  constructor(data) {
    this.data = String(data);
    this.parentNode = null;
  }

  get textContent() {
    return this.data;
  }
}

const fakeDocument = {
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (ns, tag) => new FakeNode(tag, ns),
  createTextNode: (data) => new FakeTextNode(data),
};

/** Runs `fn()` with `globalThis.document` set to the shim above, restoring
 *  whatever (if anything) was there before. */
function withDocument(fn) {
  const previous = globalThis.document;
  globalThis.document = fakeDocument;
  try {
    return fn();
  } finally {
    if (previous === undefined) delete globalThis.document;
    else globalThis.document = previous;
  }
}

/** Every element node (not text) under `root`, root first, depth-first. */
function allNodes(root) {
  return root instanceof FakeNode ? [root, ...root.children.flatMap(allNodes)] : [];
}

function findById(root, id) {
  return allNodes(root).find((node) => node.getAttribute('id') === id) || null;
}

function findAnchors(root) {
  return allNodes(root).filter((node) => node.tagName === 'A');
}

const ROUTE = { repo: '', query: '', from: 'active' };

function render(overrides = {}) {
  const target = { repo: 'owner/repo', pr: 7 };
  const panelInfo = overrides.info !== undefined ? overrides.info : info();
  const state = overrides.state || retriggerPanelState({
    info: panelInfo,
    request: overrides.request,
    local: overrides.local,
    canAct: overrides.canAct !== undefined ? overrides.canAct : true,
  });
  return withDocument(() => renderRetrigger({
    target,
    info: panelInfo,
    state,
    route: ROUTE,
    childRun: overrides.childRun || null,
    loading: overrides.loading || false,
    error: overrides.error || null,
    canAct: overrides.canAct !== undefined ? overrides.canAct : true,
    onRetrigger: overrides.onRetrigger || (() => {}),
    onReload: overrides.onReload || (() => {}),
  }));
}

describe('renderRetrigger', () => {
  it('shows who requested an author rereview to admins and viewers', () => {
    for (const canAct of [true, false]) {
      const queued = request({ requested_by: 'github:Alice' });
      const root = render({ canAct, info: info({ active_request: queued }) });
      assert.match(root.textContent, /Requested by/);
      assert.match(root.textContent, /github:Alice/);
    }
  });

  it('shows the admin a pressable button; a viewer the read-only note instead', () => {
    const adminRoot = render({ canAct: true });
    const button = findById(adminRoot, 'retrigger-confirm');
    assert.ok(button, 'admin gets the button');
    assert.equal(button.tagName, 'BUTTON');
    assert.equal(button.textContent, 'Re-run review');
    assert.equal(button.disabled, false);
    assert.ok(findById(adminRoot, 'retrigger-read-only') === null, 'no read-only note for an admin');

    const viewerRoot = render({ canAct: false });
    assert.ok(findById(viewerRoot, 'retrigger-confirm') === null, 'a viewer never gets the button');
    const note = findById(viewerRoot, 'retrigger-read-only');
    assert.ok(note, 'viewer gets the read-only note');
    assert.equal(note.textContent, 'Only dashboard admins can re-run a review.');
  });

  it('a server-only queued request (no local tab state) renders for a viewer', () => {
    // Reviewer MUST-FIX regression check: info.active_request must reach the
    // viewer's render, not just the admin's.
    const queued = request({ id: 'req-server-only' });
    const root = render({ canAct: false, info: info({ active_request: queued }) });
    const text = root.textContent;
    assert.match(text, /Re-run queued/);
    assert.match(text, /req-server-o/, 'the request id (truncated), proving info.active_request reached the render');
    assert.equal(findById(root, 'retrigger-read-only').textContent, 'Only dashboard admins can re-run a review.');
  });

  it('a running attempt on another head renders its link for both an admin and a viewer', () => {
    for (const canAct of [true, false]) {
      const root = render({ canAct, info: info({ running_run_id: 'run-live-123' }) });
      assert.match(root.textContent, /running/i, `canAct=${canAct}`);
      assert.match(root.textContent, /run-live-123/, `canAct=${canAct}`);
      // Round 2: not just text containing the id - a real anchor whose href
      // is the `#run/{id}` route.
      const anchor = findAnchors(root).find((node) => node.textContent.includes('run-live-123'));
      assert.ok(anchor, `canAct=${canAct}: no anchor links the running attempt`);
      assert.equal(anchor.getAttribute('href'), '#run/run-live-123', `canAct=${canAct}`);
    }
  });

  it('hostile strings (title, repo, error) survive literally as text, never as markup', () => {
    // Reviewer: the DOM shim's innerHTML setter throws (see FakeNode above),
    // so this fails loudly if any HTML-sink path exists in the view.
    const hostileTitle = '<img src=x onerror=alert(1)> &amp; "quoted" <script>x</script>';
    const hostileRepo = 'owner/<b>repo</b>&amp;x';
    const root = render({
      canAct: true,
      info: info({ title: hostileTitle, repo: hostileRepo }),
      error: { title: '<img src=x onerror=alert(2)>', detail: '&amp; raw', recovery: 'retry' },
    });
    const text = root.textContent;
    assert.match(text, /<img src=x onerror=alert\(1\)>/, 'the hostile title survives literally');
    assert.match(text, /&amp; "quoted" <script>x<\/script>/, 'entities and tags are not decoded or stripped');
    assert.match(text, /<img src=x onerror=alert\(2\)>/, 'the hostile error title survives literally');
    assert.match(text, /&amp; raw/);
    // Never a real <img> or <script> element: every node is one `el()` built,
    // and text always goes through textContent (dom.js's own invariant).
    assert.equal(allNodes(root).filter((node) => node.tagName === 'IMG' || node.tagName === 'SCRIPT').length, 0);
  });
});

describe('reconcileRetriggerInfo (stale queued info.active_request)', () => {
  it('clears a terminal request from info.active_request as soon as the event names it', () => {
    // Reviewer MUST-FIX: info.active_request is a GET snapshot; it must not
    // sit on 'queued' until the next server round trip when the stream
    // already said the very request named there went terminal.
    const queuedInfo = info({ active_request: request({ id: 'req-1', status: 'queued' }) });
    const event = { kind: 'retry_finished', payload: { request_id: 'req-1', status: 'succeeded', repo: 'owner/repo', pr: 7 } };
    const reconciled = reconcileRetriggerInfo(queuedInfo, event);
    assert.equal(reconciled.active_request, null);
    // The panel built from the reconciled info is not blocked.
    const state = retriggerPanelState({ info: reconciled, canAct: true });
    assert.equal(state.phase, 'ready');
    assert.equal(state.blocked, false);
  });

  it('leaves info untouched when the event names a different request, or is not a retry_finished', () => {
    const activeRequest = request({ id: 'req-1', status: 'queued' });
    const queuedInfo = info({ active_request: activeRequest });
    const otherRequest = { kind: 'retry_finished', payload: { request_id: 'req-9', status: 'succeeded' } };
    assert.deepEqual(reconcileRetriggerInfo(queuedInfo, otherRequest), queuedInfo);
    const wrongKind = { kind: 'retry_claimed', payload: { request_id: 'req-1', status: 'running' } };
    assert.deepEqual(reconcileRetriggerInfo(queuedInfo, wrongKind), queuedInfo);
    assert.equal(reconcileRetriggerInfo(null, otherRequest), null);
    assert.deepEqual(reconcileRetriggerInfo(queuedInfo, null), queuedInfo);
    assert.deepEqual(reconcileRetriggerInfo(queuedInfo, {}), queuedInfo);
  });

  it('is a no-op when info has no active_request to reconcile', () => {
    const plain = info();
    const event = { kind: 'retry_finished', payload: { request_id: 'req-1', status: 'succeeded' } };
    assert.deepEqual(reconcileRetriggerInfo(plain, event), plain);
  });

  it('is wired into app.js onStreamEvent, before the async retrigger refresh', () => {
    const app = readFileSync(fileURLToPath(new URL('./app.js', import.meta.url)), 'utf8');
    const start = app.indexOf('function onStreamEvent(');
    // Bounded to the handler: a schedule call in a later function must not count.
    const handler = app.slice(start, app.indexOf('\nfunction ', start + 1));
    const reconcile = handler.indexOf('reconcileRetriggerInfo(');
    const scheduled = handler.indexOf('refreshers.retrigger.schedule();');
    assert.ok(reconcile !== -1, 'onStreamEvent calls reconcileRetriggerInfo');
    assert.ok(scheduled !== -1 && reconcile < scheduled, 'the synchronous reconcile runs before the async refresh is scheduled');
  });
});

describe('retrigger load race guard: an older load that FAILS', () => {
  it('a superseded GET that rejects (even signed out) touches nothing', async () => {
    // Bot (tests) on deec39d: the A -> B -> A tests only resolved; the catch
    // branch's generation check was untested.
    const target = { repo: 'owner/repo', pr: 7 };
    const api = queuedApi();
    const dOld = api.queueRetriggerInfo();
    const dNew = api.queueRetriggerInfo();
    const live = {};
    const getLive = () => live.model;
    const first = beginRetriggerLoad(null, target);
    live.model = first.state;
    const oldLoad = settleRetriggerLoad(target, first.generation, getLive, api, describeSignedOut);
    // The first settles nothing yet; a fresh generation of the same target takes over.
    live.model.loading = false;
    const second = beginRetriggerLoad(live.model, target);
    const newLoad = settleRetriggerLoad(target, second.generation, getLive, api, describeSignedOut);
    dNew.resolve(info({ head_sha: 'b'.repeat(40) }));
    await newLoad;
    dOld.reject(Object.assign(new Error('unauthorized'), { status: 401 }));
    const outcome = await oldLoad;
    assert.equal(outcome.stale, true);
    assert.equal(outcome.error, null, 'no signed-out error reaches the caller');
    assert.equal(live.model.info.head_sha, 'b'.repeat(40));
    assert.equal(live.model.error, null);
    assert.equal(live.model.loading, false);
  });
});

describe('retrigger load: the current load fails', () => {
  // Bot (tests) on 71c6cbb: only a superseded rejection was covered.
  it('stores and returns the described error, ends loading, keeps signedOut, and a later load clears it', async () => {
    const target = { repo: 'owner/repo', pr: 7 };
    for (const [error, signedOut] of [[Object.assign(new Error('bad gateway'), { status: 502 }), false], [Object.assign(new Error('unauthorized'), { status: 401 }), true]]) {
      const api = queuedApi();
      const d = api.queueRetriggerInfo();
      const live = {};
      const getLive = () => live.model;
      const begin = beginRetriggerLoad(null, target);
      live.model = begin.state;
      const pending = settleRetriggerLoad(target, begin.generation, getLive, api, describeSignedOut);
      d.reject(error);
      const outcome = await pending;
      assert.equal(outcome.stale, false);
      assert.equal(outcome.error.title, error.message);
      assert.equal(outcome.error.signedOut, signedOut);
      assert.equal(live.model.error.title, error.message, 'the page shows it');
      assert.equal(live.model.loading, false);

      const d2 = api.queueRetriggerInfo();
      const again = beginRetriggerLoad(live.model, target);
      const reload = settleRetriggerLoad(target, again.generation, getLive, api, describeSignedOut);
      d2.resolve(info());
      await reload;
      assert.equal(live.model.error, null, 'a successful Reload clears the load error');
    }
  });
});

describe('retrigger load race guard (app.js loadRetrigger seam)', () => {
  const targetA = { repo: 'owner/repo', pr: 7 };
  const targetB = { repo: 'owner/other', pr: 1 };

  it('assigns a new generation on every load, even A -> B -> A to the same target', () => {
    const first = beginRetriggerLoad(null, targetA);
    assert.equal(first.generation, 1);
    assert.equal(first.state.loading, true);
    assert.deepEqual(first.state.target, targetA);
    const away = beginRetriggerLoad(first.state, targetB);
    assert.equal(away.generation, 2);
    const back = beginRetriggerLoad(away.state, targetA);
    assert.equal(back.generation, 3);
    assert.notEqual(back.generation, first.generation, 'the second visit to A is a new generation');
  });

  it('a stale response from the first A visit cannot clobber the second, in either arrival order', () => {
    // Reviewer MUST-FIX: A -> B -> A guarded only by target equality lets a
    // slow first-A response land after the second A's state is live.
    const loadA1 = beginRetriggerLoad(null, targetA);
    const loadB = beginRetriggerLoad(loadA1.state, targetB);
    const loadA2 = beginRetriggerLoad(loadB.state, targetA);
    const model = loadA2.state;
    // The second (current) A load is recognised...
    assert.equal(isCurrentRetriggerLoad(model, targetA, loadA2.generation), true);
    // ...and the first (stale) one is not, regardless of which resolves first:
    // the check is made against the live model at resolution time, not a
    // snapshot, so the order the two promises settle in cannot change it.
    assert.equal(isCurrentRetriggerLoad(model, targetA, loadA1.generation), false, 'completion order 1 (stale first)');
    model.info = { fresh: true };
    model.loading = false;
    assert.equal(isCurrentRetriggerLoad(model, targetA, loadA1.generation), false, 'completion order 2 (stale last)');
  });

  it('a load for a different target is never current, same generation or not', () => {
    const loaded = beginRetriggerLoad(null, targetA);
    assert.equal(isCurrentRetriggerLoad(loaded.state, targetB, loaded.generation), false);
  });
});

describe('canDispatchRetrigger (app.js dispatchRetrigger seam); the in-flight refusal is tested with settleRetriggerDispatch below', () => {
  it('allows a dispatch again once the previous one settled', () => {
    const model = { info: info(), target: { repo: 'owner/repo', pr: 7 }, local: { phase: 'rejected', error: {} } };
    assert.equal(canDispatchRetrigger(model), true);
    assert.equal(canDispatchRetrigger({ info: info(), target: { repo: 'owner/repo', pr: 7 }, local: null }), true);
  });

  it('refuses without an info or a target', () => {
    assert.equal(canDispatchRetrigger({ info: null, target: { repo: 'owner/repo', pr: 7 }, local: null }), false);
    assert.equal(canDispatchRetrigger({ info: info(), target: null, local: null }), false);
  });
});

// ---------------------------------------------------------------------------
// settleRetriggerLoad / settleRetriggerDispatch: real async interleaving,
// driven with deferred promises (not pre-seeded state), against a counting
// fake api. These are the extracted async halves of app.js's loadRetrigger/
// dispatchRetrigger (app.js itself cannot be imported in node: it touches
// `document` at module scope).
// ---------------------------------------------------------------------------

function deferredPromise() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** A fake `api` whose `retriggerInfo`/`retrigger` return promises taken from a
 *  queue in call order, and count every call. */
function queuedApi() {
  const retriggerInfoQueue = [];
  const retriggerQueue = [];
  return {
    retriggerInfoCalls: [],
    retriggerCalls: [],
    queueRetriggerInfo() {
      const d = deferredPromise();
      retriggerInfoQueue.push(d);
      return d;
    },
    queueRetrigger() {
      const d = deferredPromise();
      retriggerQueue.push(d);
      return d;
    },
    retriggerInfo(repo, pr) {
      this.retriggerInfoCalls.push({ repo, pr });
      const next = retriggerInfoQueue.shift();
      if (!next) throw new Error('queuedApi: no queued retriggerInfo response');
      return next.promise;
    },
    retrigger(repo, pr, head_sha, latest_run_id, observed_at) {
      this.retriggerCalls.push({ repo, pr, head_sha, latest_run_id, observed_at });
      const next = retriggerQueue.shift();
      if (!next) throw new Error('queuedApi: no queued retrigger response');
      return next.promise;
    },
  };
}

const describeError = (error) => ({ title: String(error && error.message ? error.message : error), signedOut: false });
const describeSignedOut = (error) => ({ title: String(error && error.message), signedOut: !!(error && error.status === 401) });

describe('settleRetriggerLoad: the A -> B -> A race, in both completion orders', () => {
  const targetA = { repo: 'owner/repo', pr: 7 };
  const targetB = { repo: 'owner/other', pr: 1 };

  function startThree(api) {
    const live = {};
    const getLive = () => live.model;
    const beginA1 = beginRetriggerLoad(null, targetA);
    live.model = beginA1.state;
    const pA1 = settleRetriggerLoad(targetA, beginA1.generation, getLive, api, describeError);
    const beginB = beginRetriggerLoad(live.model, targetB);
    live.model = beginB.state;
    const pB = settleRetriggerLoad(targetB, beginB.generation, getLive, api, describeError);
    const beginA2 = beginRetriggerLoad(live.model, targetA);
    live.model = beginA2.state;
    const pA2 = settleRetriggerLoad(targetA, beginA2.generation, getLive, api, describeError);
    return { live, pA1, pB, pA2 };
  }

  it('completion order 1: the stale A1 response resolves FIRST and is ignored', async () => {
    const api = queuedApi();
    const dA1 = api.queueRetriggerInfo();
    const dB = api.queueRetriggerInfo();
    const dA2 = api.queueRetriggerInfo();
    const { live, pA1, pB, pA2 } = startThree(api);

    dA1.resolve({ repo: 'owner/repo', pr: 7, head_sha: 'a'.repeat(40), state: 'open', draft: false, marker: 'A1-stale' });
    const outcome1 = await pA1;
    assert.equal(outcome1.stale, true, 'A1 settles after A2 already owns the slot');
    assert.notEqual(live.model.info && live.model.info.marker, 'A1-stale', 'the stale response never touched the live model');

    dB.resolve({ repo: 'owner/other', pr: 1, head_sha: 'b'.repeat(40), state: 'open', draft: false, marker: 'B' });
    await pB;
    dA2.resolve({ repo: 'owner/repo', pr: 7, head_sha: 'a'.repeat(40), state: 'open', draft: false, marker: 'A2-live' });
    const outcome2 = await pA2;
    assert.equal(outcome2.stale, false);
    assert.equal(live.model.info.marker, 'A2-live');
    assert.equal(live.model.loading, false);
    assert.equal(api.retriggerInfoCalls.length, 3);
  });

  it('completion order 2: the stale A1 response resolves LAST and is still ignored', async () => {
    const api = queuedApi();
    const dA1 = api.queueRetriggerInfo();
    const dB = api.queueRetriggerInfo();
    const dA2 = api.queueRetriggerInfo();
    const { live, pA1, pB, pA2 } = startThree(api);

    dB.resolve({ repo: 'owner/other', pr: 1, head_sha: 'b'.repeat(40), state: 'open', draft: false, marker: 'B' });
    await pB;
    dA2.resolve({ repo: 'owner/repo', pr: 7, head_sha: 'a'.repeat(40), state: 'open', draft: false, marker: 'A2-live' });
    const outcome2 = await pA2;
    assert.equal(live.model.info.marker, 'A2-live');
    assert.equal(live.model.loading, false);

    // The stale A1 response lands last, after A2 already settled.
    dA1.resolve({ repo: 'owner/repo', pr: 7, head_sha: 'a'.repeat(40), state: 'open', draft: false, marker: 'A1-stale' });
    const outcome1 = await pA1;
    assert.equal(outcome1.stale, true);
    assert.equal(live.model.info.marker, 'A2-live', 'the stale, late response did not clobber the live A2 model');
    assert.equal(live.model.loading, false);
    assert.equal(outcome2.stale, false);
  });
});

describe('settleRetriggerDispatch: two immediate calls make one POST', () => {
  it('the second immediate call is a no-op while the first is in flight; both settle correctly', async () => {
    const api = queuedApi();
    const d = api.queueRetrigger();
    const model = { info: info(), target: { repo: 'owner/repo', pr: 7 }, local: null };

    const first = settleRetriggerDispatch(model, api, describeError);
    assert.equal(model.local.phase, 'dispatching', 'the synchronous prefix ran before any await');
    const second = settleRetriggerDispatch(model, api, describeError);

    const secondOutcome = await second;
    assert.equal(secondOutcome, null, 'the immediate second call started nothing');

    d.resolve({ request: { id: 'req-only-one' } });
    const firstOutcome = await first;
    assert.equal(firstOutcome.request.id, 'req-only-one');
    assert.equal(api.retriggerCalls.length, 1, 'exactly one POST reached the api');
    assert.equal(model.local, null);
  });
});

describe('round 2 MUST-FIX #6/#7: a same-target refresh never detaches an in-flight dispatch', () => {
  function liveAfterInitialLoad(api) {
    const live = {};
    const getLive = () => live.model;
    const begin = beginRetriggerLoad(null, { repo: 'owner/repo', pr: 7 });
    live.model = begin.state;
    return { live, getLive, begin };
  }

  it('a dispatch in flight survives a concurrent same-target refresh and settles on the LIVE model (success)', async () => {
    const loadApi = queuedApi();
    const dInitial = loadApi.queueRetriggerInfo();
    const target = { repo: 'owner/repo', pr: 7 };
    const { live, getLive, begin } = liveAfterInitialLoad(loadApi);
    dInitial.resolve(info());
    await settleRetriggerLoad(target, begin.generation, getLive, loadApi, describeError);

    const dispatchApi = queuedApi();
    const dDispatch = dispatchApi.queueRetrigger();
    const model = live.model; // the reference dispatchRetrigger in app.js would capture
    const dispatchPromise = settleRetriggerDispatch(model, dispatchApi, describeError);
    assert.equal(live.model.local.phase, 'dispatching');

    // A same-target refresh starts and completes WHILE the dispatch above is
    // still in flight (round 1's reconcileRetriggerInfo/stream-driven path is
    // one real-world trigger for this).
    const refreshApi = queuedApi();
    const dRefresh = refreshApi.queueRetriggerInfo();
    const beginRefresh = beginRetriggerLoad(live.model, target);
    assert.equal(beginRefresh.state, model, 'round 2 MUST-FIX: same-target refresh preserves model identity');
    live.model = beginRefresh.state;
    dRefresh.resolve(info());
    await settleRetriggerLoad(target, beginRefresh.generation, getLive, refreshApi, describeError);
    assert.equal(live.model.loading, false);
    assert.equal(live.model.local.phase, 'dispatching', 'the refresh must not clear an active dispatch');

    dDispatch.resolve({ request: { id: 'req-after-refresh' } });
    const outcome = await dispatchPromise;
    assert.equal(outcome.request.id, 'req-after-refresh');
    assert.equal(live.model.local, null, 'the dispatch settled on the SAME object the refresh touched');
  });

  it('a dispatch in flight survives a concurrent same-target refresh and settles on the LIVE model (failure)', async () => {
    const loadApi = queuedApi();
    const dInitial = loadApi.queueRetriggerInfo();
    const target = { repo: 'owner/repo', pr: 7 };
    const { live, getLive, begin } = liveAfterInitialLoad(loadApi);
    dInitial.resolve(info());
    await settleRetriggerLoad(target, begin.generation, getLive, loadApi, describeError);

    const dispatchApi = queuedApi();
    const dDispatch = dispatchApi.queueRetrigger();
    const model = live.model;
    const dispatchPromise = settleRetriggerDispatch(model, dispatchApi, describeError);

    const refreshApi = queuedApi();
    const dRefresh = refreshApi.queueRetriggerInfo();
    const beginRefresh = beginRetriggerLoad(live.model, target);
    live.model = beginRefresh.state;
    dRefresh.resolve(info());
    await settleRetriggerLoad(target, beginRefresh.generation, getLive, refreshApi, describeError);
    assert.equal(live.model.local.phase, 'dispatching');

    dDispatch.reject(new Error('head moved'));
    const outcome = await dispatchPromise;
    assert.equal(outcome.error.title, 'head moved');
    assert.equal(live.model.local.phase, 'rejected', 'the failure settled on the SAME live object');
  });

  it('a refresh never clears a settled refusal; Reload acknowledges it at click time', async () => {
    // The refusal path schedules a refresh itself; clearing at settlement would
    // wipe the 409 reason about a second after it showed. Reload recovers via
    // acknowledgeRefusal, which runs when the button is clicked.
    const api = queuedApi();
    const d = api.queueRetriggerInfo();
    const target = { repo: 'owner/repo', pr: 7 };
    const { live, getLive, begin } = liveAfterInitialLoad(api);
    live.model.local = { phase: 'rejected', error: { title: 'refused' } };
    d.resolve(info());
    await settleRetriggerLoad(target, begin.generation, getLive, api, describeError);
    assert.equal(live.model.local.phase, 'rejected');
    acknowledgeRefusal(live.model);
    assert.equal(live.model.local, null, 'Reload recovers');
  });

  it('a refusal that arrives after Reload started survives that Reload', async () => {
    // Bot (maintainability) on 446d5d1: dispatch in flight, Reload clicked
    // (nothing to acknowledge yet), POST answers 409, then the Reload GET
    // settles and used to clear the refusal the operator never saw.
    const target = { repo: 'owner/repo', pr: 7 };
    const loadApi = queuedApi();
    const dInitial = loadApi.queueRetriggerInfo();
    const { live, getLive, begin } = liveAfterInitialLoad(loadApi);
    dInitial.resolve(info());
    await settleRetriggerLoad(target, begin.generation, getLive, loadApi, describeError);

    const dispatchApi = queuedApi();
    const dDispatch = dispatchApi.queueRetrigger();
    const dispatching = settleRetriggerDispatch(live.model, dispatchApi, describeError);
    acknowledgeRefusal(live.model); // the Reload click: a dispatch, not a refusal
    const reloadApi = queuedApi();
    const dReload = reloadApi.queueRetriggerInfo();
    const reload = beginRetriggerLoad(live.model, target);
    const reloading = settleRetriggerLoad(target, reload.generation, getLive, reloadApi, describeError);
    dDispatch.reject(new Error('a newer attempt of this head exists since the page was shown; reload it'));
    await dispatching;
    assert.equal(live.model.local.phase, 'rejected');
    dReload.resolve(info());
    await reloading;
    assert.equal(live.model.local.phase, 'rejected', 'the refusal stays until it is acknowledged');
  });

  it('loadRetrigger has no settlement-time refusal clearing in app.js', () => {
    // app.js cannot be imported in node (it touches the DOM at module scope),
    // so the loader's orchestration is pinned: the refusal is acknowledged only
    // by an explicit load, once, before the load can begin or be owed, and
    // nothing in the loader clears it after a load settles.
    const app = readFileSync(fileURLToPath(new URL('./app.js', import.meta.url)), 'utf8');
    assert.ok(!app.includes('keepRefusal'), 'refusals are cleared only by acknowledgeRefusal');
    const start = app.indexOf('async function loadRetrigger(');
    const loader = app.slice(start, app.indexOf('\nasync function ', start + 1));
    const acks = loader.split('acknowledgeRefusal(').length - 1;
    assert.equal(acks, 1, 'one acknowledgement in the loader');
    const ack = loader.indexOf('if (!silent) acknowledgeRefusal(state.retrigger);');
    assert.ok(ack !== -1 && ack < loader.indexOf('beginRetriggerLoad('), 'before the load begins');
    assert.ok(!/local\s*=\s*null/.test(loader), 'the loader never clears local state itself');
    const dispatch = app.slice(app.indexOf('async function dispatchRetrigger('), app.indexOf('\nfunction ', app.indexOf('async function dispatchRetrigger(')));
    assert.ok(!dispatch.includes('acknowledgeRefusal('), 'a refusal is not acknowledged by the dispatch path');
  });

  it('a refresh asked for while a same-target load is in flight is owed, not dropped', async () => {
    // Bot P2 on PR #39: Reload's GET captured a queued request; retry_finished
    // arrives and its refresh fires before that GET settles. Dropping the
    // refresh let the older response re-install the queued request for good.
    const api = queuedApi();
    const d = api.queueRetriggerInfo();
    const target = { repo: 'owner/repo', pr: 7 };
    const { live, getLive, begin } = liveAfterInitialLoad(api);
    const during = beginRetriggerLoad(live.model, target);
    assert.equal(during.state, null, 'no second GET while one is in flight');
    d.resolve(info({ active_request: { id: 'req-1', status: 'queued' } }));
    const outcome = await settleRetriggerLoad(target, begin.generation, getLive, api, describeError);
    assert.equal(outcome.again, true, 'the settled load reports the refresh it still owes');
    assert.equal(live.model.loading, false);

    // The owed refresh is paid once: the next load starts and owes nothing.
    const d2 = api.queueRetriggerInfo();
    const next = beginRetriggerLoad(live.model, target);
    assert.ok(next.state);
    d2.resolve(info());
    const second = await settleRetriggerLoad(target, next.generation, getLive, api, describeError);
    assert.equal(second.again, false);
  });

  it('an explicit Reload clears a refusal even while a silent refresh is in flight', async () => {
    // Trinity (astra-zhuge) on 13ff95b: Reload during the silent refresh the
    // refusal path schedules only became an owed silent refresh, so the
    // refusal and the disabled button outlived both GETs.
    const api = queuedApi();
    const target = { repo: 'owner/repo', pr: 7 };
    const dSilent = api.queueRetriggerInfo();
    const { live, getLive, begin } = liveAfterInitialLoad(api);
    live.model.info = info();
    live.model.local = { phase: 'rejected', error: { title: 'refused' } };
    const silent = settleRetriggerLoad(target, begin.generation, getLive, api, describeError);

    acknowledgeRefusal(live.model); // the Reload click
    assert.equal(beginRetriggerLoad(live.model, target).state, null, 'the click is owed behind the GET in flight');
    dSilent.resolve(info());
    await silent;
    const state = retriggerPanelState({ info: live.model.info, local: live.model.local, canAct: true });
    assert.equal(live.model.local, null);
    assert.equal(state.blocked, false);
    acknowledgeRefusal(live.model); // a no-op without a refusal
    live.model.local = { phase: 'dispatching' };
    acknowledgeRefusal(live.model);
    assert.equal(live.model.local.phase, 'dispatching', 'never touches an active dispatch');
  });

  it('loadRetrigger schedules the owed refresh in app.js', () => {
    const app = readFileSync(fileURLToPath(new URL('./app.js', import.meta.url)), 'utf8');
    const loader = app.slice(app.indexOf('async function loadRetrigger('), app.indexOf('async function dispatchRetrigger('));
    assert.match(loader, /if \(outcome\.again\) refreshers\.retrigger\.schedule\(\);/);
  });

  it('a successful refresh never clears an active dispatch', async () => {
    const api = queuedApi();
    const d = api.queueRetriggerInfo();
    const target = { repo: 'owner/repo', pr: 7 };
    const { live, getLive, begin } = liveAfterInitialLoad(api);
    live.model.local = { phase: 'dispatching' };
    d.resolve(info());
    await settleRetriggerLoad(target, begin.generation, getLive, api, describeError);
    assert.equal(live.model.local.phase, 'dispatching');
  });
});

describe('retrigger dispatch snapshot and the linked attempt', () => {
  it('app.js Reload retains a 409 arriving after the click until the next explicit Reload', async () => {
    // Execute the actual orchestration, with real model and view functions.
    // Only its browser startup and HTTP transport are outside this harness.
    const app = readFileSync(new URL('./app.js', import.meta.url), 'utf8');
    const source = app.slice(app.indexOf('async function loadRetrigger('), app.indexOf('\nfunction navigate('));
    const target = { repo: 'owner/repo', pr: 7 };
    const state = { retrigger: { target, info: info(), local: null, generation: 1 } };
    const api = queuedApi();
    let tree;
    const redraw = () => { tree = render({ info: state.retrigger.info, local: state.retrigger.local }); };
    const context = {
      state, api, render: redraw, acknowledgeRefusal, beginRetriggerLoad,
      settleRetriggerLoad, settleRetriggerDispatch, describeApiError: describeError,
      toSignIn: () => assert.fail('409 is not a lost session'),
      refreshers: { retrigger: { schedule() {} } },
    };
    const handlers = new Function(...Object.keys(context), `${source}\nreturn { loadRetrigger, dispatchRetrigger };`)(...Object.values(context));
    const post = api.queueRetrigger();
    const dispatching = handlers.dispatchRetrigger();
    const get = api.queueRetriggerInfo();
    const loading = handlers.loadRetrigger(target);
    post.reject(Object.assign(new Error('newer attempt; reload'), { status: 409 }));
    await dispatching;
    get.resolve(info());
    await loading;
    assert.equal(state.retrigger.local?.phase, 'rejected');
    assert.match(tree.textContent, /newer attempt; reload/);
    assert.equal(findById(tree, 'retrigger-confirm').disabled, true);

    const silentGet = api.queueRetriggerInfo();
    const silent = handlers.loadRetrigger(target, { silent: true });
    silentGet.resolve(info());
    await silent;
    assert.equal(state.retrigger.local?.phase, 'rejected', 'silent refresh does not acknowledge');
    const nextGet = api.queueRetriggerInfo();
    const next = handlers.loadRetrigger(target);
    nextGet.resolve(info());
    await next;
    assert.equal(state.retrigger.local, null);
    assert.equal(findById(tree, 'retrigger-confirm').disabled, false);
  });

  it('the POST carries the attempt the page displayed (or null)', async () => {
    for (const [latest, expected] of [[{ id: 'run-9', status: 'succeeded' }, 'run-9'], [null, null]]) {
      const api = queuedApi();
      const d = api.queueRetrigger();
      const model = { target: { repo: 'owner/repo', pr: 7 }, info: info({ latest_run: latest }), local: null };
      const pending = settleRetriggerDispatch(model, api, describeError);
      d.resolve({ request: { id: 'req-1' } });
      await pending;
      assert.equal(api.retriggerCalls[0].latest_run_id, expected);
    }
  });

  it('the POST carries when the page read its snapshot (observed_at), or null', async () => {
    // Bot P2 on 64059fb: the daemon's ledger cutoff is the page's snapshot, not the queue time.
    for (const [observed, expected] of [['2026-10-15T17:00:00+00:00', '2026-10-15T17:00:00+00:00'], [undefined, null]]) {
      const api = queuedApi();
      const d = api.queueRetrigger();
      const model = { target: { repo: 'owner/repo', pr: 7 }, info: info({ observed_at: observed }), local: null };
      const pending = settleRetriggerDispatch(model, api, describeError);
      d.resolve({ request: { id: 'req-1' } });
      await pending;
      assert.equal(api.retriggerCalls[0].observed_at, expected);
    }
  });

  it("links only the selected request's own attempt", () => {
    // Bot (tests) on 13ff95b: the panel selected the server's queued req-2 but
    // the card linked req-1's run as req-2's "New attempt".
    assert.equal(retriggerChildRun({ id: 'req-2', status: 'queued' }), null);
    assert.equal(retriggerChildRun({ id: 'req-2', status: 'running', run_id: 'run-2' }).id, 'run-2');
    assert.deepEqual(retriggerChildRun({ id: 'req-3', run_id: 'run-x' }), { id: 'run-x' });
    assert.equal(retriggerChildRun(null), null);
    for (const run_id of ['', null, 42]) assert.equal(retriggerChildRun({ run_id }), null);
  });

  it('app.js derives the linked attempt from the panel request', () => {
    const app = readFileSync(fileURLToPath(new URL('./app.js', import.meta.url)), 'utf8');
    const branch = app.slice(app.indexOf("if (route.view === 'retrigger') {"), app.indexOf("const bucket = currentBucket() || 'active';"));
    assert.ok(branch.includes('childRun: retriggerChildRun(panel.request)'));
    assert.ok(!branch.includes('retryAttemptFor('), 'no independent attempt lookup');
  });
});

describe('retrigger refresh scope', () => {
  const target = { repo: 'owner/repo', pr: 7 };
  const model = { target, info: null };

  it('refreshes only while the Retrigger page for the same PR is the route', () => {
    assert.equal(retriggerRefreshWanted({ view: 'retrigger', target }, model), true);
    assert.equal(retriggerRefreshWanted({ view: 'queue', target: null }, model), false, 'left the page');
    assert.equal(retriggerRefreshWanted({ view: 'retrigger', target: { repo: 'owner/repo', pr: 8 } }, model), false);
    assert.equal(retriggerRefreshWanted({ view: 'retrigger', target }, { target: null }), false);
    assert.equal(retriggerRefreshWanted(null, model), false);
  });

  it('gates every scheduled retrigger refresh in app.js', () => {
    // The page's GET reads GitHub; a tab that once followed a badge must not
    // keep re-reading it on every review event after leaving the page.
    const app = readFileSync(fileURLToPath(new URL('./app.js', import.meta.url)), 'utf8');
    assert.doesNotMatch(app, /if \(state\.retrigger\.target\) refreshers\.retrigger\.schedule\(\)/);
    assert.doesNotMatch(app, /if \(state\.retrigger\.target && \(change/);
    assert.match(app, /retrigger: createCoalescer\(\(\) => \(retriggerRefreshWanted\(state\.route, state\.retrigger\)/);
    const fn = (name) => {
      const start = app.indexOf(`function ${name}(`);
      return app.slice(start, app.indexOf('\nfunction ', start + 1));
    };
    const handler = fn('onStreamEvent');
    assert.match(handler, /retriggerRefreshWanted\(state\.route, state\.retrigger\) && \(change \|\| isRetryEvent\(event\)\)\) refreshers\.retrigger\.schedule\(\);/);
    const resync = fn('resyncFromDurableData');
    assert.match(resync, /if \(retriggerRefreshWanted\(state\.route, state\.retrigger\)\) refreshers\.retrigger\.schedule\(\);/);
  });
});

describe('retrigger hash route', () => {
  it('parses the badge link, decoding each part', () => {
    const route = parseHash('#retrigger/acme-corp/review.of_legends/42');
    assert.equal(route.view, 'retrigger');
    assert.deepEqual(route.target, { repo: 'acme-corp/review.of_legends', pr: 42 });
    assert.equal(route.runId, null);
    const encoded = parseHash('#retrigger/own%2Der/re%2Epo/7?repo=owner%2Fname');
    assert.deepEqual(encoded.target, { repo: 'own-er/re.po', pr: 7 });
    assert.equal(encoded.repo, 'owner/name', 'queue filters still ride along');
  });

  it('falls back to the queue on malformed input', () => {
    for (const bad of [
      '#retrigger',
      '#retrigger/owner',
      '#retrigger/owner/repo',
      '#retrigger/owner/repo/0',
      '#retrigger/owner/repo/abc',
      '#retrigger/owner/repo/7/extra',
      '#retrigger/owner/repo/01',
      '#retrigger/owner/repo/1234567890',
      '#retrigger/../repo/7',
      '#retrigger/%2E%2E/repo/7',
      '#retrigger/owner/re%2Fpo/7',
      '#retrigger/owner/re%E0%A4%A/7',
      '#retrigger//repo/7',
      '#retrigger/ow ner/repo/7',
      `#retrigger/${'a'.repeat(101)}/repo/7`,
      '#retrigger/.github/repo/7', // an owner may not start with a dot or hyphen
      '#retrigger/-owner/repo/7',
    ]) {
      const route = parseHash(bad);
      assert.equal(route.view, 'active', bad);
      assert.equal(route.target, undefined, bad);
    }
  });

  it('accepts a repository name that starts with a dot, like .github', () => {
    // Reviewer MUST-FIX: NAME_PATTERN required the first character of BOTH
    // owner and repo to be alphanumeric, which rejects a real GitHub repo name.
    const route = parseHash('#retrigger/owner/.github/7');
    assert.deepEqual(route.target, { repo: 'owner/.github', pr: 7 });
    const encoded = parseHash('#retrigger/owner/%2Egithub/7');
    assert.deepEqual(encoded.target, { repo: 'owner/.github', pr: 7 });
    assert.equal(
      serializeHash({ view: 'retrigger', target: { repo: 'owner/.github', pr: 7 } }),
      '#retrigger/owner/.github/7',
    );
    // The literal single/double dot is still refused: a repo cannot BE "." or "..".
    for (const bad of ['#retrigger/owner/./7', '#retrigger/owner/../7', '#retrigger/owner/%2E/7', '#retrigger/owner/%2E%2E/7']) {
      assert.equal(parseHash(bad).target, undefined, bad);
    }
  });

  it('serialises the target with each part encoded and round-trips', () => {
    const route = { view: 'retrigger', target: { repo: 'own er/re.po', pr: 7 } };
    assert.equal(serializeHash(route), '#retrigger/own%20er/re.po/7');
    const parsed = parseHash('#retrigger/owner/repo/7?q=x');
    assert.equal(serializeHash(parsed), '#retrigger/owner/repo/7?q=x');
    assert.equal(serializeHash({ view: 'retrigger' }), '#active', 'no target, no page');
  });
});

function fakeStorage({ throwing = false } = {}) {
  const map = new Map();
  return {
    map,
    getItem: (key) => { if (throwing) throw new Error('blocked'); return map.has(key) ? map.get(key) : null; },
    setItem: (key, value) => { if (throwing) throw new Error('blocked'); map.set(key, String(value)); },
    removeItem: (key) => { if (throwing) throw new Error('blocked'); map.delete(key); },
  };
}

describe('sign-in return hash', () => {
  it('remembers a page hash and hands it back exactly once', () => {
    const storage = fakeStorage();
    assert.equal(rememberReturnHash(storage, '#retrigger/owner/repo/7'), true);
    assert.equal(storage.map.get(RETURN_HASH_KEY), '#retrigger/owner/repo/7');
    assert.equal(takeReturnHash(storage), '#retrigger/owner/repo/7');
    assert.equal(takeReturnHash(storage), null, 'taken means gone');
    assert.equal(rememberReturnHash(storage, '#run/6f1c2d3e?from=attention'), true);
    assert.equal(takeReturnHash(storage), '#run/6f1c2d3e?from=attention');
  });

  it('refuses hashes not worth returning to and clears a stale one', () => {
    const storage = fakeStorage();
    for (const bad of ['', '#', 'run/abc', '#has space', `#${'a'.repeat(600)}`, null, 7]) {
      rememberReturnHash(storage, '#run/abc'); // a stale destination for every invalid input
      assert.equal(rememberReturnHash(storage, bad), false, String(bad));
      assert.equal(storage.map.has(RETURN_HASH_KEY), false, String(bad));
    }
    storage.map.set(RETURN_HASH_KEY, 'not a hash');
    assert.equal(takeReturnHash(storage), null);
    assert.equal(storage.map.has(RETURN_HASH_KEY), false, 'junk is cleared too');
  });

  it('survives a blocked or missing storage', () => {
    assert.equal(rememberReturnHash(fakeStorage({ throwing: true }), '#run/abc'), false);
    assert.equal(takeReturnHash(fakeStorage({ throwing: true })), null);
    assert.equal(rememberReturnHash(null, '#run/abc'), false);
    assert.equal(takeReturnHash(undefined), null);
  });

  it('safeSessionStorage survives a throwing getter, not just a throwing method', () => {
    // Reviewer: `window.sessionStorage` itself can throw (SecurityError in a
    // sandboxed/private context) before setItem/getItem are ever reached, so
    // `rememberReturnHash(window.sessionStorage, ...)` throws while evaluating
    // its own arguments - a try inside rememberReturnHash/takeReturnHash never
    // runs. safeSessionStorage must catch that at the access point itself.
    const throwingScope = {};
    Object.defineProperty(throwingScope, 'sessionStorage', { get() { throw new Error('blocked'); } });
    assert.equal(safeSessionStorage(throwingScope), null);
    assert.equal(safeSessionStorage({}), null, 'no sessionStorage at all');
    assert.equal(safeSessionStorage(undefined), null, 'no scope at all');
    const real = fakeStorage();
    assert.equal(safeSessionStorage({ sessionStorage: real }), real);
    // And the callers compose with it safely end to end.
    assert.equal(rememberReturnHash(safeSessionStorage(throwingScope), '#run/abc'), false);
    assert.equal(takeReturnHash(safeSessionStorage(throwingScope)), null);
  });

  it('prefers a hash already in the URL over the remembered one', () => {
    assert.equal(returnHashAfterSignIn('', '#retrigger/owner/repo/7'), '#retrigger/owner/repo/7');
    assert.equal(returnHashAfterSignIn('#', '#retrigger/owner/repo/7'), '#retrigger/owner/repo/7');
    assert.equal(returnHashAfterSignIn('#history', '#retrigger/owner/repo/7'), null, 'the operator navigated on purpose');
    assert.equal(returnHashAfterSignIn('', null), null);
    assert.equal(returnHashAfterSignIn('', 'junk'), null);
  });

  it('is wired into the sign-in link and the boot path of app.js (caller ordering)', () => {
    // app.js is DOM-bound and has no unit harness: pin the two seams. The hash
    // must be remembered where the sign-in view is rendered, and taken back
    // after the session check and before the first route change.
    const app = readFileSync(fileURLToPath(new URL('./app.js', import.meta.url)), 'utf8');
    // Reviewer: `window.sessionStorage` itself can throw, so the caller must
    // go through the safe getter, never touch `window.sessionStorage` raw.
    assert.match(app, /renderSignIn\(\{[^}]*onSignIn: \(\) => rememberReturnHash\(safeSessionStorage\(\), window\.location\.hash\)/);
    assert.doesNotMatch(app, /rememberReturnHash\(window\.sessionStorage/);
    assert.doesNotMatch(app, /takeReturnHash\(window\.sessionStorage/);
    const boot = app.slice(app.indexOf('async function boot('));
    const me = boot.indexOf('await api.me()');
    const restore = boot.indexOf('returnHashAfterSignIn(window.location.hash, takeReturnHash(safeSessionStorage()))');
    // The hashchange listener at the top of boot() calls onRouteChange() too;
    // the first route change of the session is the last call in boot().
    const routed = boot.lastIndexOf('onRouteChange();');
    assert.ok(me !== -1 && restore !== -1 && routed !== -1, 'all three seams exist in boot()');
    assert.ok(me < restore && restore < routed, 'restore after the session check, before the first route change');
    assert.match(boot.slice(restore, routed), /window\.history\.replaceState\(null, '', restored\)/);
    assert.match(boot.slice(restore, routed), /state\.route = parseHash\(restored\)/);
  });
});
