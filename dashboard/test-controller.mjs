/** Controller seams: the two places where a stale cache or an event burst used to
 *  win over the truth.
 *
 * Both are exercised through named helpers rather than through app.js, because
 * the bugs they pin were argument-order and call-frequency mistakes at exactly
 * these seams. A helper whose parameters are named `cached` and `fresh` cannot be
 * called in the wrong order the way a variadic merge could.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { applyRetryUpdates } from './runs.js';
import { captureOpenDetails, createGestureGate, createRenderScheduler, domId, restoreOpenDetails } from './dom.js';

const CACHED_QUEUED = {
  id: 'req-1',
  status: 'queued',
  repo: 'owner/repo',
  pr: 39,
  head_sha: 'abc',
  retry_of: 'run-1',
  created_at: '2026-09-11T06:00:00+00:00',
  run_id: null,
  error: null,
};

describe('retry cache reconciliation', () => {
  it('lets the server answer replace the cached row, not the other way round', () => {
    const fresh = { ...CACHED_QUEUED, status: 'succeeded', run_id: 'run-child' };
    const merged = applyRetryUpdates([CACHED_QUEUED], [fresh]);
    assert.equal(merged.length, 1);
    assert.equal(merged[0].status, 'succeeded', 'a terminal dispatch must not be re-queued by the cache');
    assert.equal(merged[0].run_id, 'run-child');
  });

  it('does not lose cached rows the fresh page did not include', () => {
    const other = { ...CACHED_QUEUED, id: 'req-2', retry_of: 'run-2', created_at: '2026-09-11T05:00:00+00:00' };
    const merged = applyRetryUpdates([CACHED_QUEUED, other], [{ ...CACHED_QUEUED, status: 'failed' }]);
    assert.equal(merged.length, 2);
    assert.equal(merged.find((r) => r.id === 'req-1').status, 'failed');
    assert.equal(merged.find((r) => r.id === 'req-2').status, 'queued');
  });

  it('accepts the 202 response as fresher than anything cached', () => {
    const accepted = { ...CACHED_QUEUED, status: 'running' };
    const merged = applyRetryUpdates([CACHED_QUEUED], [accepted]);
    assert.equal(merged[0].status, 'running');
  });

  it('keeps newest-first ordering', () => {
    const older = { ...CACHED_QUEUED, id: 'req-old', created_at: '2026-09-11T04:00:00+00:00' };
    const newer = { ...CACHED_QUEUED, id: 'req-new', created_at: '2026-09-11T09:00:00+00:00' };
    assert.deepEqual(applyRetryUpdates([older], [newer]).map((r) => r.id), ['req-new', 'req-old']);
  });
});

describe('render scheduling', () => {
  function harness() {
    const frames = [];
    let renders = 0;
    const scheduler = createRenderScheduler(() => { renders += 1; }, {
      requestFrame: (fn) => { frames.push(fn); return frames.length - 1; },
      cancelFrame: (handle) => { if (frames[handle]) frames[handle] = null; },
    });
    return { frames, scheduler, get renders() { return renders; } };
  }

  it('collapses a replay burst into a single render', () => {
    const h = harness();
    for (let index = 0; index < 200; index += 1) h.scheduler.schedule();
    assert.equal(h.frames.length, 1, 'one frame requested for the whole burst');
    assert.equal(h.renders, 0, 'nothing renders until the frame runs');
    h.frames[0]();
    assert.equal(h.renders, 1);
  });

  it('renders again for events that arrive after the frame ran', () => {
    const h = harness();
    h.scheduler.schedule();
    h.frames[0]();
    h.scheduler.schedule();
    assert.equal(h.frames.length, 2);
    h.frames[1]();
    assert.equal(h.renders, 2);
  });

  it('renders immediately when asked to, and drops a pending frame', () => {
    const h = harness();
    h.scheduler.schedule();
    h.scheduler.now();
    assert.equal(h.renders, 1);
    assert.equal(h.frames[0], null, 'the queued frame was cancelled, so it cannot render twice');
  });

  it('can be cancelled without rendering', () => {
    const h = harness();
    h.scheduler.schedule();
    h.scheduler.cancel();
    assert.equal(h.renders, 0);
    assert.equal(h.frames[0], null);
  });
});

describe('blur render after a pointer gesture', () => {
  function gate() {
    const target = new EventTarget();
    const timers = [];
    const gestures = createGestureGate(target, {
      setTimer: (fn) => { timers.push(fn); return timers.length; },
      clearTimer: (handle) => { timers[handle - 1] = null; },
    });
    const fire = (type) => target.dispatchEvent(new Event(type));
    return { gestures, fire, timers };
  }

  it('runs at once after a keyboard blur', () => {
    const { gestures } = gate();
    let ran = 0;
    gestures.whenIdle(() => { ran += 1; });
    assert.equal(ran, 1);
  });

  it('waits for the click a press started, so the pressed button is not replaced first', () => {
    const { gestures, fire } = gate();
    let ran = 0;
    fire('pointerdown');
    gestures.whenIdle(() => { ran += 1; });
    assert.equal(ran, 0, 'blur happened on pointerdown');
    fire('pointerup');
    assert.equal(ran, 0, 'the click has not been dispatched yet');
    fire('click');
    assert.equal(ran, 1);
    fire('click');
    assert.equal(ran, 1, 'runs once');
  });

  it('settles without a click (release elsewhere) and on pointercancel', () => {
    const { gestures, fire, timers } = gate();
    let ran = 0;
    fire('pointerdown');
    gestures.whenIdle(() => { ran += 1; });
    fire('pointerup');
    timers.filter(Boolean).forEach((fn) => fn());
    assert.equal(ran, 1);
    fire('pointerdown');
    gestures.whenIdle(() => { ran += 1; });
    fire('pointercancel');
    assert.equal(ran, 2);
  });
});

describe('open disclosures across a render', () => {
  // The two selectors the helpers use, over plain objects standing in for <details>.
  function root(nodes) {
    return {
      querySelectorAll(selector) {
        if (selector === 'details[open][id]') return nodes.filter((node) => node.open && node.id !== undefined);
        if (selector === 'details[id]') return nodes.filter((node) => node.id !== undefined);
        throw new Error(`unexpected selector ${selector}`);
      },
    };
  }

  it('re-opens what the operator had open and nothing else', () => {
    const before = root([
      { id: 'why-run-1-security', open: true },
      { id: 'why-run-1-tests', open: false },
      { id: '', open: true },
      { id: 'why-run-1-gone', open: true },
    ]);
    const ids = captureOpenDetails(before);
    assert.deepEqual(ids, ['why-run-1-security', 'why-run-1-gone']);
    const rebuilt = [{ id: 'why-run-1-security', open: false }, { id: 'why-run-1-tests', open: false }, { id: 'why-run-2-security', open: false }];
    restoreOpenDetails(root(rebuilt), ids);
    assert.deepEqual(rebuilt.map((node) => node.open), [true, false, false], 'another run with the same agent stays closed');
  });

  it('is a no-op without a root or anything open', () => {
    assert.deepEqual(captureOpenDetails(null), []);
    assert.doesNotThrow(() => restoreOpenDetails(null, ['x']));
    const nodes = [{ id: 'a', open: false }];
    restoreOpenDetails(root(nodes), []);
    assert.equal(nodes[0].open, false);
  });

  it('builds ids that are safe in a selector', () => {
    assert.equal(domId('why', 'run-0123abcd', 'red-team'), 'why-run-0123abcd-red-team');
    assert.equal(domId('why', 'run 1', 'a.b"c'), 'why-run-1-a-b-c');
  });
});
