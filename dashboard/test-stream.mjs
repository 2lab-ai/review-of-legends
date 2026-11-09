/** Event stream: frame validation, monotonic cursor, backoff, and the
 *  heartbeat-based detection of a half-open connection. */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  DEFAULT_STALE_AFTER_MS,
  createFreshness,
  describeFreshness,
  isSignalStale,
  isValidEventId,
  nextBackoffDelay,
  openEventStream,
  parseReadyCursor,
  parseStreamEvent,
} from './stream.js';

/** Minimal EventSource stand-in: records the URL it was opened with and lets the
 *  test drive open/message/error transitions. */
class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.closed = false;
    this.handlers = new Map();
    FakeEventSource.instances.push(this);
  }

  addEventListener(type, handler) {
    const list = this.handlers.get(type) || [];
    list.push(handler);
    this.handlers.set(type, list);
  }

  close() {
    this.closed = true;
    this.readyState = 2;
  }

  emitOpen() {
    this.readyState = 1;
    if (this.onopen) this.onopen({});
  }

  emit(type, data) {
    for (const handler of this.handlers.get(type) || []) handler({ data });
  }

  emitError(readyState) {
    this.readyState = readyState;
    if (this.onerror) this.onerror({});
  }
}
FakeEventSource.instances = [];

function harness({ staleAfterMs = DEFAULT_STALE_AFTER_MS } = {}) {
  FakeEventSource.instances = [];
  let clock = 1000;
  const timers = [];
  const states = [];
  const updates = [];
  const readyCalls = [];
  let authExpired = 0;
  let stale = 0;

  const stream = openEventStream({
    EventSourceImpl: FakeEventSource,
    staleAfterMs,
    now: () => clock,
    setTimeoutImpl: (fn, ms) => {
      timers.push({ fn, at: clock + ms, ms });
      return timers.length - 1;
    },
    clearTimeoutImpl: (handle) => {
      if (timers[handle]) timers[handle].cancelled = true;
    },
    onUpdate: (event) => updates.push(event),
    onState: (snapshot) => states.push(snapshot),
    onAuthExpired: () => { authExpired += 1; },
    onStale: () => { stale += 1; },
    onReady: (info) => readyCalls.push(info),
  });

  return {
    stream,
    states,
    updates,
    readyCalls,
    timers,
    get authExpired() { return authExpired; },
    get staleCalls() { return stale; },
    get current() { return FakeEventSource.instances[FakeEventSource.instances.length - 1]; },
    get opened() { return FakeEventSource.instances.length; },
    advance(ms) { clock += ms; },
    fire() {
      const pending = timers.filter((timer) => !timer.cancelled && !timer.done && timer.at <= clock);
      for (const timer of pending) {
        timer.done = true;
        timer.fn();
      }
      return pending.length;
    },
  };
}

describe('frame validation', () => {
  it('accepts a well-formed frame and normalises the payload', () => {
    const parsed = parseStreamEvent(JSON.stringify({
      id: 7, kind: 'run_updated', run_id: 'a', payload: { status: 'failed' }, created_at: 'now',
    }));
    assert.deepEqual(parsed, {
      id: 7, kind: 'run_updated', run_id: 'a', payload: { status: 'failed' }, created_at: 'now',
    });
  });

  it('rejects broken frames rather than throwing', () => {
    assert.equal(parseStreamEvent('{not json'), null);
    assert.equal(parseStreamEvent(''), null);
    assert.equal(parseStreamEvent(null), null);
    assert.equal(parseStreamEvent('[]'), null);
    assert.equal(parseStreamEvent(JSON.stringify({ kind: 'x' })), null);
  });

  it('requires a non-negative safe integer id', () => {
    assert.equal(isValidEventId(0), true);
    assert.equal(isValidEventId(1), true);
    assert.equal(isValidEventId(-1), false);
    assert.equal(isValidEventId(1.5), false);
    assert.equal(isValidEventId(Number.MAX_SAFE_INTEGER + 2), false);
    assert.equal(isValidEventId('3'), false);
    assert.equal(parseStreamEvent(JSON.stringify({ id: -5, kind: 'x' })), null);
    assert.equal(parseStreamEvent(JSON.stringify({ id: 2 ** 60, kind: 'x' })), null);
  });

  it('coerces a non-object payload to an empty object', () => {
    assert.deepEqual(parseStreamEvent(JSON.stringify({ id: 1, payload: 'nope' })).payload, {});
  });
});

describe('backoff', () => {
  it('doubles and caps', () => {
    assert.equal(nextBackoffDelay(1), 1000);
    assert.equal(nextBackoffDelay(2), 2000);
    assert.equal(nextBackoffDelay(3), 4000);
    assert.equal(nextBackoffDelay(10), 30000);
    assert.equal(nextBackoffDelay(0), 1000);
  });
});

describe('freshness reporting', () => {
  it('does not judge silence when the server never sent a heartbeat', () => {
    let clock = 1000;
    const freshness = createFreshness({ now: () => clock });
    freshness.open();
    clock += 10 * 60000;
    const snapshot = freshness.snapshot();
    assert.equal(snapshot.heartbeatSeen, false);
    assert.equal(isSignalStale(snapshot, { now: clock }), false);
    const described = describeFreshness(snapshot, { now: clock });
    assert.equal(described.phase, 'live');
    assert.match(described.detail, /heartbeat not reported/);
  });

  it('calls a live-looking connection suspect once heartbeats stop', () => {
    let clock = 1000;
    const freshness = createFreshness({ now: () => clock });
    freshness.open();
    freshness.heartbeat();
    clock += 20000;
    assert.equal(isSignalStale(freshness.snapshot(), { now: clock }), false);
    clock += 40000;
    assert.equal(isSignalStale(freshness.snapshot(), { now: clock }), true);
    const described = describeFreshness(freshness.snapshot(), {
      now: clock,
      relative: () => '1m ago',
    });
    assert.equal(described.phase, 'suspect');
    assert.equal(described.suspect, true);
    assert.match(described.detail, /heartbeat overdue/);
  });

  it('never calls a closed or reconnecting stream suspect', () => {
    let clock = 1000;
    const freshness = createFreshness({ now: () => clock });
    freshness.open();
    freshness.heartbeat();
    freshness.offline();
    clock += 10 * 60000;
    assert.equal(isSignalStale(freshness.snapshot(), { now: clock }), false);
  });

  it('reports reconnect attempts and separates them from event age', () => {
    let clock = 1000;
    const freshness = createFreshness({ now: () => clock });
    freshness.connecting();
    freshness.open();
    freshness.message();
    freshness.retrying();
    const described = describeFreshness(freshness.snapshot(), { now: clock, relative: () => '0s ago' });
    assert.equal(described.phase, 'reconnecting');
    assert.match(described.detail, /last event 0s ago/);
    assert.match(described.detail, /attempt 1/);
  });

  it('describes an absent snapshot as idle', () => {
    const described = describeFreshness(null, { now: 0 });
    assert.equal(described.phase, 'idle');
    assert.match(described.detail, /no events received yet/);
  });
});

describe('stream client', () => {
  it('connects without a cursor and reconnects with one', () => {
    const h = harness();
    assert.equal(h.current.url, '/api/events');
    h.current.emitOpen();
    h.current.emit('update', JSON.stringify({ id: 12, kind: 'run_updated', run_id: 'a' }));
    assert.equal(h.updates.length, 1);
    assert.equal(h.stream.lastEventId, 12);

    h.current.emitError(2); // CLOSED: the browser gave up
    h.advance(1000); // the backoff delay elapses
    assert.equal(h.fire() > 0, true);
    assert.equal(h.current.url, '/api/events?after=12');
  });

  it('keeps the cursor monotonic across a replay', () => {
    const h = harness();
    h.current.emitOpen();
    h.current.emit('update', JSON.stringify({ id: 12 }));
    h.current.emit('update', JSON.stringify({ id: 5 }));
    assert.equal(h.stream.lastEventId, 12);
    assert.equal(h.updates.length, 2, 'the replayed event is still delivered');
  });

  it('leaves the browser alone while it retries by itself', () => {
    const h = harness();
    h.current.emitOpen();
    const opened = h.opened;
    h.current.emitError(0); // CONNECTING
    assert.equal(h.opened, opened);
    assert.equal(h.states[h.states.length - 1].phase, 'reconnecting');
  });

  it('backs off when the browser stops retrying', () => {
    const h = harness();
    h.current.emitOpen();
    h.current.emitError(2);
    const scheduled = h.timers.filter((t) => !t.cancelled);
    assert.equal(scheduled[scheduled.length - 1].ms, 1000);

    h.advance(1000);
    assert.equal(h.fire(), 1, 'the first reconnect ran');
    h.current.emitError(2); // the replacement never even opened
    const again = h.timers.filter((t) => !t.cancelled && !t.done);
    assert.equal(again[again.length - 1].ms, 2000);
  });

  it('resets the backoff once a connection actually opens', () => {
    const h = harness();
    h.current.emitOpen();
    h.current.emitError(2);
    h.advance(1000);
    h.fire();
    h.current.emitOpen(); // the replacement is healthy
    h.current.emitError(2);
    const pending = h.timers.filter((t) => !t.cancelled && !t.done);
    assert.equal(pending[pending.length - 1].ms, 1000);
  });

  it('re-opens a half-open connection once a heartbeat goes overdue', () => {
    const h = harness({ staleAfterMs: 45000 });
    h.current.emitOpen();
    h.current.emit('heartbeat', '');
    const before = h.opened;
    h.advance(46000);
    h.fire();
    assert.equal(h.staleCalls, 1);
    assert.equal(h.opened, before + 1, 'a fresh EventSource replaced the dead one');
  });

  it('does not re-open when heartbeats keep arriving', () => {
    const h = harness({ staleAfterMs: 45000 });
    h.current.emitOpen();
    h.current.emit('heartbeat', '');
    const before = h.opened;
    h.advance(20000);
    h.current.emit('heartbeat', '');
    h.advance(30000);
    h.fire();
    assert.equal(h.opened, before);
  });

  it('never arms the watchdog for a server that sends no heartbeat', () => {
    const h = harness({ staleAfterMs: 45000 });
    h.current.emitOpen();
    const before = h.opened;
    h.advance(10 * 60000);
    h.fire();
    assert.equal(h.opened, before);
    assert.equal(h.staleCalls, 0);
    assert.equal(h.timers.length, 0, 'no watchdog timer was ever scheduled');
  });

  it('anchors the cursor on the ready frame and resyncs after it', () => {
    const h = harness();
    assert.equal(h.current.url, '/api/events', 'the first connect asks for the tail, not a replay');
    h.current.emitOpen();
    h.current.emit('ready', JSON.stringify({ cursor: 4210 }));
    assert.equal(h.readyCalls.length, 1);
    assert.deepEqual(h.readyCalls[0], { cursor: 4210 });
    assert.equal(h.stream.lastEventId, 4210);

    // The anchor is what a later reconnect resumes from: nothing replays from the
    // start of the table, and nothing between hydrate and tail is skipped.
    h.current.emitError(2);
    h.advance(1000);
    h.fire();
    assert.equal(h.current.url, '/api/events?after=4210');
  });

  it('keeps the ready anchor monotonic and survives a malformed one', () => {
    const h = harness();
    h.current.emitOpen();
    h.current.emit('update', JSON.stringify({ id: 99 }));
    h.current.emit('ready', JSON.stringify({ cursor: 12 }));
    assert.equal(h.stream.lastEventId, 99, 'an older anchor must not rewind the cursor');
    h.current.emit('ready', '{"cursor":-1}');
    h.current.emit('ready', 'not json');
    assert.equal(h.stream.lastEventId, 99);
    assert.equal(h.readyCalls.length, 3, 'the callback still fires, so the resync still happens');
  });

  it('treats ready as connectivity only, not an event and not a heartbeat promise', () => {
    const h = harness({ staleAfterMs: 45000 });
    h.current.emitOpen();
    h.current.emit('ready', JSON.stringify({ cursor: 7 }));
    const snapshot = h.stream.snapshot();
    assert.equal(snapshot.lastEventAt, null, 'ready is not a domain event');
    assert.equal(snapshot.heartbeatSeen, false, 'ready does not promise heartbeats');
    assert.notEqual(snapshot.lastSignalAt, null, 'but bytes did arrive');
    const before = h.opened;
    h.advance(10 * 60000);
    h.fire();
    assert.equal(h.opened, before, 'ready alone arms no watchdog');
  });

  it('parses the ready cursor defensively', () => {
    assert.equal(parseReadyCursor('{"cursor":0}'), 0);
    assert.equal(parseReadyCursor('{"cursor":4210}'), 4210);
    assert.equal(parseReadyCursor('{"cursor":-1}'), null);
    assert.equal(parseReadyCursor('{"cursor":1.5}'), null);
    assert.equal(parseReadyCursor('{"cursor":"4210"}'), null);
    assert.equal(parseReadyCursor('{}'), null);
    assert.equal(parseReadyCursor('[]'), null);
    assert.equal(parseReadyCursor('nope'), null);
    assert.equal(parseReadyCursor(''), null);
    assert.equal(parseReadyCursor(null), null);
  });

  it('closes for good on auth-expired', () => {
    const h = harness();
    h.current.emitOpen();
    h.current.emit('auth-expired', '');
    assert.equal(h.authExpired, 1);
    assert.equal(h.stream.readyState, 2);
    assert.equal(h.stream.reconnectNow(), false, 'a closed stream stays closed');
  });

  it('reconnects on demand only when the socket is not healthy', () => {
    const h = harness();
    h.current.emitOpen();
    assert.equal(h.stream.reconnectNow(), false);
    h.current.readyState = 2;
    assert.equal(h.stream.reconnectNow(), true);
  });
});
