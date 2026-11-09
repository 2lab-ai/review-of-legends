/** Server-sent event client plus the connection-freshness state machine.
 *
 * What this layer may and may not claim:
 *   - It reports *server connectivity* ("this page is connected to the dashboard
 *     API and is receiving its event stream").
 *   - It must never present that as review-executor health. A live stream with a
 *     dead daemon looks identical from here, so the label stays about the stream
 *     and the executor is only ever described by durable data (/api/retries,
 *     /api/runs).
 *
 * Half-open connections: a comment heartbeat (`: ping`) keeps a proxy alive but is
 * invisible to EventSource, so a TCP connection that died silently still reads as
 * OPEN forever. The contract therefore needs a *named* `heartbeat` event; when one
 * is observed this client arms a watchdog and, after `staleAfterMs` without any
 * signal, reports the connection as suspect and re-opens it. Until a heartbeat has
 * ever been seen the watchdog stays disarmed and the indicator says so - silence
 * from a server that never promised a heartbeat is not evidence of a dead link.
 */

export const DEFAULT_STALE_AFTER_MS = 45000;

const PHASE_LABELS = {
  idle: 'Stream idle',
  connecting: 'Stream connecting',
  live: 'Stream live',
  reconnecting: 'Stream reconnecting',
  offline: 'Stream offline',
  suspect: 'Stream may be dead',
};

export function nextBackoffDelay(attempt, { base = 1000, max = 30000 } = {}) {
  const n = Math.max(1, Math.floor(attempt));
  return Math.min(max, base * 2 ** (n - 1));
}

/** Pure state machine, so every indicator transition is unit-testable.
 *
 *  `lastSignalAt` is the newest of open/event/heartbeat: the one timestamp that
 *  actually says "bytes arrived from the server". */
export function createFreshness({ now = () => Date.now() } = {}) {
  let phase = 'idle';
  let attempts = 0;
  let lastEventAt = null;
  let lastHeartbeatAt = null;
  let lastSignalAt = null;
  let heartbeatSeen = false;
  let lastChangeAt = null;

  function set(next) {
    if (phase !== next) {
      phase = next;
      lastChangeAt = now();
    }
    return snapshot();
  }

  function snapshot() {
    return { phase, attempts, lastEventAt, lastHeartbeatAt, lastSignalAt, heartbeatSeen, lastChangeAt };
  }

  return {
    snapshot,
    connecting() {
      return set(attempts > 0 ? 'reconnecting' : 'connecting');
    },
    open() {
      attempts = 0;
      lastSignalAt = now();
      return set('live');
    },
    message() {
      lastEventAt = now();
      lastSignalAt = lastEventAt;
      return set('live');
    },
    heartbeat() {
      heartbeatSeen = true;
      lastHeartbeatAt = now();
      lastSignalAt = lastHeartbeatAt;
      return set('live');
    },
    /** Bytes arrived, but as a control frame (`ready`) - not a domain event and
     *  not a heartbeat promise, so only `lastSignalAt` may move. */
    signal() {
      lastSignalAt = now();
      return set('live');
    },
    retrying() {
      attempts += 1;
      return set('reconnecting');
    },
    offline() {
      attempts += 1;
      return set('offline');
    },
    stopped() {
      attempts = 0;
      return set('idle');
    },
  };
}

/** Text for the header indicator. Connectivity, event recency and heartbeat
 *  availability stay three separate facts; none of them is executor health. */
export function describeFreshness(snapshot, {
  now = Date.now(),
  relative,
  staleAfterMs = DEFAULT_STALE_AFTER_MS,
} = {}) {
  const state = snapshot || {
    phase: 'idle', attempts: 0, lastEventAt: null, lastSignalAt: null, heartbeatSeen: false,
  };
  const suspect = isSignalStale(state, { now, staleAfterMs });
  const phase = suspect ? 'suspect' : state.phase;
  const parts = [];
  if (state.lastEventAt === null) {
    parts.push('no events received yet');
  } else if (typeof relative === 'function') {
    parts.push(`last event ${relative(state.lastEventAt, now)}`);
  }
  if (state.heartbeatSeen) {
    if (typeof relative === 'function' && state.lastSignalAt !== null) {
      parts.push(`last signal ${relative(state.lastSignalAt, now)}`);
    }
    if (suspect) parts.push('heartbeat overdue, reconnecting');
  } else if (state.phase === 'live') {
    parts.push('server heartbeat not reported, silence cannot be judged');
  }
  if (state.phase === 'reconnecting' && state.attempts > 0) {
    parts.push(`attempt ${state.attempts}`);
  }
  return {
    phase,
    suspect,
    label: PHASE_LABELS[phase] || PHASE_LABELS.idle,
    detail: parts.join(' - '),
  };
}

/** Only a server that sends named heartbeats can be judged by its silence. */
export function isSignalStale(snapshot, { now = Date.now(), staleAfterMs = DEFAULT_STALE_AFTER_MS } = {}) {
  if (!snapshot || snapshot.phase !== 'live') return false;
  if (!snapshot.heartbeatSeen) return false;
  if (typeof snapshot.lastSignalAt !== 'number') return false;
  return now - snapshot.lastSignalAt > staleAfterMs;
}

/** SSE ids are the store's AUTOINCREMENT rowids: non-negative safe integers that
 *  only ever move forward. A replayed or malformed id must not rewind our cursor. */
export function isValidEventId(value) {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}

/** The `ready` control frame the server sends on a cursor-less connect: it names
 *  the tail it started from, so a later reconnect resumes at that anchor instead
 *  of replaying the whole table. */
export function parseReadyCursor(raw) {
  if (typeof raw !== 'string' || raw === '') return null;
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!data || typeof data !== 'object') return null;
  return isValidEventId(data.cursor) ? data.cursor : null;
}

/** `event.data` is attacker-visible only to the server, but a malformed frame
 *  must not kill the stream loop. */
export function parseStreamEvent(raw) {
  if (typeof raw !== 'string' || raw === '') return null;
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!data || typeof data !== 'object') return null;
  if (!isValidEventId(data.id)) return null;
  return {
    id: data.id,
    kind: typeof data.kind === 'string' ? data.kind : '',
    run_id: typeof data.run_id === 'string' ? data.run_id : null,
    payload: data.payload && typeof data.payload === 'object' ? data.payload : {},
    created_at: typeof data.created_at === 'string' ? data.created_at : null,
  };
}

/**
 * Open the single global event stream.
 *
 * The browser reconnects an EventSource by itself and resends Last-Event-ID, so
 * the manual path here only handles the case where it gives up (readyState
 * CLOSED, e.g. a non-2xx response): then we re-open with `?after=<lastId>` so no
 * persisted event is skipped.
 */
export function openEventStream({
  path = '/api/events',
  EventSourceImpl,
  onUpdate,
  onAuthExpired,
  onState,
  onStale,
  onReady,
  now = () => Date.now(),
  setTimeoutImpl = (fn, ms) => setTimeout(fn, ms),
  clearTimeoutImpl = (handle) => clearTimeout(handle),
  freshness = createFreshness({ now }),
  staleAfterMs = DEFAULT_STALE_AFTER_MS,
} = {}) {
  const Impl = EventSourceImpl || globalThis.EventSource;
  let source = null;
  let timer = null;
  let watchdog = null;
  let closed = false;
  let lastId = null;

  function publish(snapshot) {
    if (typeof onState === 'function') onState(snapshot);
  }

  function clearTimer() {
    if (timer !== null) {
      clearTimeoutImpl(timer);
      timer = null;
    }
  }

  function clearWatchdog() {
    if (watchdog !== null) {
      clearTimeoutImpl(watchdog);
      watchdog = null;
    }
  }

  /** Armed only after a named heartbeat proved the server sends them. */
  function armWatchdog() {
    clearWatchdog();
    if (closed || !freshness.snapshot().heartbeatSeen) return;
    watchdog = setTimeoutImpl(() => {
      watchdog = null;
      if (closed) return;
      if (!isSignalStale(freshness.snapshot(), { now: now(), staleAfterMs })) {
        armWatchdog();
        return;
      }
      publish(freshness.snapshot());
      if (typeof onStale === 'function') onStale(freshness.snapshot());
      // The socket claims OPEN but nothing arrives: treat it as dead and re-open.
      detach();
      connect();
    }, staleAfterMs);
  }

  function noteSignal(snapshot) {
    publish(snapshot);
    armWatchdog();
  }

  function url() {
    return lastId === null ? path : `${path}?after=${encodeURIComponent(String(lastId))}`;
  }

  function connect() {
    if (closed) return;
    clearTimer();
    publish(freshness.connecting());
    source = new Impl(url(), { withCredentials: true });
    source.onopen = () => {
      noteSignal(freshness.open());
    };
    source.addEventListener('update', (event) => {
      const parsed = parseStreamEvent(event && event.data);
      noteSignal(freshness.message());
      if (parsed === null) return;
      // Monotonic: a replayed or out-of-order frame must not rewind the cursor.
      if (lastId === null || parsed.id > lastId) lastId = parsed.id;
      if (typeof onUpdate === 'function') onUpdate(parsed);
    });
    source.addEventListener('heartbeat', () => {
      noteSignal(freshness.heartbeat());
    });
    // `ready` names the tail this stream started from. The durable refresh must
    // happen AFTER that anchor exists: an API hydrate that finished before the
    // server snapshotted the tail can have missed a transition that will never be
    // streamed, and only a re-read closes that window.
    source.addEventListener('ready', (event) => {
      const cursor = parseReadyCursor(event && event.data);
      if (cursor !== null && (lastId === null || cursor > lastId)) lastId = cursor;
      noteSignal(freshness.signal());
      if (typeof onReady === 'function') onReady({ cursor });
    });
    source.addEventListener('auth-expired', () => {
      close();
      if (typeof onAuthExpired === 'function') onAuthExpired();
    });
    source.onerror = () => {
      if (closed) return;
      clearWatchdog();
      // CONNECTING (0) means the browser is retrying on its own; leave it alone.
      if (source && source.readyState === 0) {
        publish(freshness.retrying());
        return;
      }
      const snapshot = freshness.offline();
      publish(snapshot);
      detach();
      const delay = nextBackoffDelay(snapshot.attempts);
      timer = setTimeoutImpl(connect, delay);
    };
  }

  function detach() {
    if (source) {
      source.onopen = null;
      source.onerror = null;
      try {
        source.close();
      } catch {
        /* closing a dead stream is not an error worth surfacing */
      }
      source = null;
    }
  }

  function close() {
    closed = true;
    clearTimer();
    clearWatchdog();
    detach();
    publish(freshness.stopped());
  }

  /** Called on window focus / visibility change: a laptop that slept usually
   *  wakes with a silently dead stream. */
  function reconnectNow({ force = false } = {}) {
    if (closed) return false;
    const stale = isSignalStale(freshness.snapshot(), { now: now(), staleAfterMs });
    if (!force && !stale && source && source.readyState === 1) return false;
    detach();
    clearTimer();
    clearWatchdog();
    connect();
    return true;
  }

  connect();

  return {
    close,
    reconnectNow,
    get lastEventId() {
      return lastId;
    },
    get readyState() {
      return source ? source.readyState : 2;
    },
    snapshot: () => freshness.snapshot(),
  };
}
