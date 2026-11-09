/** Retrigger page model. Pure functions only - no DOM, no network.
 *
 *  The page behind the Retrigger badge on a PR comment (`#retrigger/owner/name/42`).
 *  `GET /api/retrigger/...` answers with the PR's current head, the latest
 *  attempt for that head and what is queued or running for the PR; an admin may
 *  then queue a rereview of the head the page showed. The rules below decide
 *  what the card says and whether the button may be pressed. `blocked` is the
 *  only thing the button trusts for disabling, as on the attempt page.
 */

import { ACTIVE_RETRY_STATUSES } from './format.js';

/** The run-shaped identity `runs.js` matches retry requests on (repo + pr + head). */
export function retriggerTarget(info) {
  if (!info || typeof info !== 'object') return null;
  const repo = typeof info.repo === 'string' ? info.repo.trim() : '';
  const headSha = typeof info.head_sha === 'string' ? info.head_sha.trim() : '';
  const pr = info.pr;
  if (repo === '' || headSha === '' || typeof pr !== 'number' || !Number.isFinite(pr) || pr <= 0) return null;
  return { repo, pr, head_sha: headSha };
}

function activePhase(request) {
  return request.status === 'queued' ? 'queued' : 'claimed';
}

function sameTarget(a, b) {
  return !!(a && b && a.repo === b.repo && a.pr === b.pr);
}

/** The next `state.retrigger` when `loadRetrigger(target)` starts, and the
 *  generation to check its response against. `state: null` means a load for
 *  this exact target is already in flight: the caller does nothing.
 *
 *  A -> B -> A back to the *same* target still gets a new generation, so a
 *  slow response from the first A visit cannot be mistaken for the second's
 *  once it resolves (checked only by target equality, before this).
 *
 *  A same-target refresh mutates the existing model IN PLACE rather than
 *  replacing it with a new object. Round 2 MUST-FIX: a replacing refresh
 *  (`{ ...model }`) silently detached any reference a caller already held -
 *  an in-flight `dispatchRetrigger`'s `model` - from the live slot, so the
 *  POST later settled on the stale, orphaned object and the live model
 *  (which nothing ever touched again) stayed `dispatching` forever. A
 *  different target still gets a fresh object: that is a different page,
 *  not a refresh, and a dispatch in flight for the page just left is
 *  correctly left mutating its own now-detached snapshot. */
export function beginRetriggerLoad(model, target) {
  const already = sameTarget(model && model.target, target);
  if (already && model.loading) {
    // The GET in flight may predate whatever asked for this refresh (a
    // terminal stream event): owe one more load instead of dropping it.
    model.refreshOwed = true;
    return { state: null, generation: model.generation };
  }
  const generation = ((model && model.generation) || 0) + 1;
  if (already) {
    model.loading = true;
    model.refreshOwed = false;
    model.generation = generation;
    return { state: model, generation };
  }
  return { state: { target, info: null, loading: true, error: null, local: null, generation }, generation };
}

/** Whether a `loadRetrigger` response for (target, generation) is still the
 *  one the live model wants, checked against the model at settlement time (not
 *  a snapshot), so it holds under either completion order of two overlapping
 *  loads. */
export function isCurrentRetriggerLoad(model, target, generation) {
  return sameTarget(model && model.target, target) && model.generation === generation;
}

/** The attempt to link as "New attempt": only the selected request's own run
 *  (`retriggerPanelState(...).request`), never another request's. */
export function retriggerChildRun(request) {
  const runId = request && typeof request.run_id === 'string' && request.run_id !== '' ? request.run_id : null;
  return runId === null ? null : { id: runId };
}

/** An explicit Reload acknowledges a settled refusal right away: the load it
 *  asks for may only be owed behind a silent refresh already in flight
 *  (which keeps refusals), so clearing at settlement would never happen. An
 *  active dispatch is never touched. */
export function acknowledgeRefusal(model) {
  if (model && model.local && model.local.phase === 'rejected') model.local = null;
}

/** Whether a stream-driven refresh of the Retrigger page is wanted: only while
 *  that page, for the same PR, is the current route. Its GET reads GitHub, so a
 *  tab that once followed a badge must not keep re-reading it after leaving. */
export function retriggerRefreshWanted(route, model) {
  return !!(route && route.view === 'retrigger' && model && sameTarget(model.target, route.target));
}

/** Whether `dispatchRetrigger` may start a new POST: an admin dispatch is not
 *  already in flight. Two immediate calls (a double click, or two bound
 *  handlers) must produce exactly one POST. */
export function canDispatchRetrigger(model) {
  return !!(model && model.info && model.target && !(model.local && model.local.phase === 'dispatching'));
}

/** The async half of `loadRetrigger`, extracted so it has no DOM dependency
 *  and an integration test can drive it in node with a fake `api` of
 *  deferred promises: app.js is not importable in node (it touches
 *  `document` at module scope), so the control flow a real race needs to
 *  exercise lives here instead.
 *
 *  `getLive()` is called fresh after every await - never a snapshot - so a
 *  response this exact (target, generation) no longer owns (a newer load
 *  replaced it, by target or by a fresh same-target generation) is detected
 *  against the CURRENT live model, not against a reference taken before the
 *  await. `describeError(error)` is the caller's `describeApiError`; it is
 *  never called with side effects here, so the caller is free to act on
 *  `signedOut` in the returned outcome (`{ stale, error }`) itself - `stale`
 *  true means this call touched nothing, because a newer one already owns
 *  the slot. Neither a settled refusal (`local.phase === 'rejected'`) nor an
 *  active dispatch (`'dispatching'`) is touched here: Reload recovers from a
 *  refusal through `acknowledgeRefusal`, at click time. */
export async function settleRetriggerLoad(target, generation, getLive, api, describeError) {
  let outcome;
  try {
    const info = await api.retriggerInfo(target.repo, target.pr);
    const current = getLive();
    if (!isCurrentRetriggerLoad(current, target, generation)) return { stale: true, error: null, again: false };
    current.info = info && typeof info === 'object' ? info : null;
    const error = current.info ? null : describeError({ status: 502 });
    current.error = error;
    // A refusal is never cleared here: the refusal path schedules a refresh
    // itself, and one may settle after a refusal the operator has not seen.
    // Reload acknowledges it when clicked (acknowledgeRefusal).
    outcome = { stale: false, error, again: false };
  } catch (caught) {
    const current = getLive();
    if (!isCurrentRetriggerLoad(current, target, generation)) return { stale: true, error: null, again: false };
    const error = describeError(caught);
    current.error = error;
    outcome = { stale: false, error, again: false };
  } finally {
    const current = getLive();
    if (isCurrentRetriggerLoad(current, target, generation)) current.loading = false;
  }
  // A refresh asked for while this GET was in flight (beginRetriggerLoad)
  // is the caller's to run now: `again`.
  const current = getLive();
  if (current && current.refreshOwed) {
    current.refreshOwed = false;
    outcome.again = true;
  }
  return outcome;
}

/** The async half of `dispatchRetrigger`, extracted for the same reason as
 *  `settleRetriggerLoad`. Unlike the loader, it mutates `model` directly
 *  rather than re-reading a live slot: `beginRetriggerLoad`'s same-target
 *  identity preservation is what keeps `model` the live object across a
 *  concurrent refresh (round 2 MUST-FIX); a dispatch for a page the operator
 *  has since navigated away from correctly keeps mutating its own, by-then
 *  detached, snapshot, exactly as before extraction.
 *
 *  Returns `null` when nothing started (already dispatching), otherwise
 *  `{ request, error }` - `request` the accepted row from a 202, `error` the
 *  described refusal (with `.signedOut` when the session itself is gone). */
export async function settleRetriggerDispatch(model, api, describeError) {
  if (!canDispatchRetrigger(model)) return null;
  const info = model.info;
  model.local = { phase: 'dispatching' };
  try {
    const shown = info.latest_run && typeof info.latest_run.id === 'string' ? info.latest_run.id : null;
    // When the server read that snapshot: the daemon's cutoff for "reviewed meanwhile".
    const observedAt = typeof info.observed_at === 'string' ? info.observed_at : null;
    const data = await api.retrigger(model.target.repo, model.target.pr, info.head_sha, shown, observedAt);
    model.local = null;
    return { request: data && data.request ? data.request : null, error: null };
  } catch (caught) {
    const error = describeError(caught);
    if (!error.signedOut) model.local = { phase: 'rejected', error };
    return { request: null, error };
  }
}

/** The activity projection: what is queued, running or just finished for this
 *  PR, independent of who is looking. Authorization (`canAct`) is applied
 *  separately in `retriggerPanelState`, so a viewer sees exactly the same
 *  queued/running status and attempt links an admin sees - only the ability
 *  to press the button differs. */
function activityPhase(info, request) {
  if (request && ACTIVE_RETRY_STATUSES.includes(request.status)) {
    return { phase: activePhase(request), blocked: true, request };
  }
  const serverRequest = info.active_request;
  if (serverRequest && ACTIVE_RETRY_STATUSES.includes(serverRequest.status)) {
    // Queued for another head of the same PR, or not yet in this tab's queue page.
    return { phase: activePhase(serverRequest), blocked: true, request: serverRequest };
  }
  if (typeof info.running_run_id === 'string' && info.running_run_id !== '') {
    return { phase: 'running', blocked: true, request, runId: info.running_run_id };
  }
  if (info.state !== 'open') {
    return { phase: 'not-open', blocked: true, request };
  }
  if (info.draft === true) {
    return { phase: 'draft', blocked: true, request };
  }
  if (typeof info.head_sha !== 'string' || info.head_sha === '') {
    return { phase: 'no-head', blocked: true, request };
  }
  if (request && request.status === 'succeeded') {
    // An earlier rereview of this head ran; a fresh one is allowed again.
    return { phase: 'dispatched', blocked: false, request };
  }
  if (request && request.status === 'failed') {
    return { phase: 'dispatch-failed', blocked: false, request };
  }
  if (request && request.status === 'stale') {
    return { phase: 'dispatch-stale', blocked: false, request };
  }
  return { phase: 'ready', blocked: false, request: null };
}

/** Reconciles a terminal retry-queue event (`retry_finished`) into `info`
 *  directly, so the panel does not stay blocked on a stale, server-snapshot
 *  `active_request` while waiting for the next `GET /api/retrigger/...` to
 *  land. `info.active_request` is only ever queued/running by contract; once
 *  the stream says that exact request (matched by id) went terminal, it is no
 *  longer active for this PR. Anything else - a different request, a
 *  non-terminal event, no active request to begin with - is left untouched;
 *  the caller's own async refresh (scheduled regardless) still resyncs
 *  anything this does not cover (e.g. `running_run_id`). */
export function reconcileRetriggerInfo(info, event) {
  if (!info || typeof info !== 'object') return info;
  if (!event || typeof event !== 'object' || event.kind !== 'retry_finished') return info;
  const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
  const requestId = typeof payload.request_id === 'string' ? payload.request_id : null;
  const active = info.active_request;
  if (!requestId || !active || active.id !== requestId) return info;
  return { ...info, active_request: null };
}

/** Panel state for one PR.
 *
 *  `info` is the server's answer; `request` the retry request for this PR and
 *  head as the queue knows it now (fresher than `info.active_request` after a
 *  202 or a stream event); `local` this tab's optimistic phase (an admin's own
 *  in-flight dispatch or its refusal - meaningless for a viewer, who never
 *  dispatches); `canAct` whether the session is an admin. A non-admin gets the
 *  same activity phase an admin would see, always blocked (the view's own
 *  `canAct` check decides whether the button or the read-only note renders). */
export function retriggerPanelState({ info = null, request = null, local = null, canAct = false } = {}) {
  if (!info) return { phase: 'hidden', blocked: true, request: null };
  if (canAct && local && local.phase === 'dispatching') {
    return { phase: 'dispatching', blocked: true, request };
  }
  if (canAct && local && local.phase === 'rejected') {
    return { phase: 'rejected', blocked: true, request, error: local.error || null };
  }
  const activity = activityPhase(info, request);
  return canAct ? activity : { ...activity, blocked: true };
}
