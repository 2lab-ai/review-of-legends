/** Agents page controller: the state transitions behind the editor, pulled out
 *  of app.js so they are unit-testable without a DOM (see test-agents-controller.mjs).
 *
 * app.js stays thin glue: for each click or answer it calls one of these
 * around the actual API request and render(). Nothing here touches fetch,
 * the DOM, or app.js's own module state — each function takes the agents
 * model (the plain object app.js keeps at `state.agents`) and either reads it
 * or mutates the fields named in its doc comment.
 */

import {
  MAX_PROMPT_CHARS, defaultChangedNotice, defaultChangedUnderEdit, draftAfterSave, draftFromAgent,
  hasSupersededPrompt, replaceAgentRow,
} from './agents.js';

/** The agents model's initial shape. `sync` orders reads against mutation
 *  answers (see createAgentsReadSync); `stash` holds, per agent, unsaved prompt
 *  text a changed default set aside (stashEditsOnDefaultChange). */
export function emptyAgentsModel() {
  return {
    rows: [], drafts: {}, stash: {}, maxChars: MAX_PROMPT_CHARS, configError: null,
    loading: false, saving: null, confirmDiscard: null, error: null, notice: null, loaded: false,
    // agent id -> the baseline of a mutation whose answer could not be applied
    // (a read answered meanwhile); the next applied read rebases onto it.
    pendingBaselines: {},
    // The baseline of the request in flight, { agentId, baseline }: the same
    // object its answer replays, so a read can move it (followReadInBaselines).
    inflight: null,
    sync: createAgentsReadSync(),
  };
}

/** Orders GETs and mutation answers so the page never keeps a row older than
 *  one it has already been told about. The same rule as admins.js's
 *  createAdminListSync (a shared list, there the admin grants; here the agent
 *  rows), kept as its own copy because the two pages do not share a model:
 *
 *  - One read at a time. A refresh asked for while a read runs (the first
 *    load included) marks the model stale, and that read is followed by
 *    exactly one more.
 *  - A read that started before a mutation (Save/Reset/Discard/Rollback)
 *    landed answers with an older list: it is not applied, and it reads
 *    again — the mutation's own answer already carries the newer row, so a
 *    stale GET racing behind it can never resurrect the row it replaced.
 *
 *  The handlers call startRead/canApplyRead/finishRead around each GET.
 *  beginMutation records completed reads so a late answer cannot replace a
 *  newer read; applyMutation requests a follow-up GET in either ordering. */
export function createAgentsReadSync() {
  let applied = 0;
  let readSince = null;
  let stale = false;
  let completedReads = 0;
  let mutationSince = 0;
  return {
    /** True: send the GET now. False: one is in flight, and it will read again. */
    startRead() {
      if (readSince !== null) {
        stale = true;
        return false;
      }
      readSince = applied;
      return true;
    },
    /** The GET answered: true when no mutation answer was applied since it started. */
    canApplyRead() {
      return readSince === applied;
    },
    /** The GET is over: true when it must run once more. Only a read that
     *  answered (`answered`) can carry a newer row; a failed one never makes a
     *  mutation's own answer look stale. */
    finishRead(answered = true) {
      if (answered && readSince === applied) completedReads += 1;
      readSince = null;
      const again = stale;
      stale = false;
      return again;
    },
    beginMutation() {
      mutationSince = completedReads;
    },
    /** A completed read since dispatch may already contain another tab's edit. */
    canApplyMutation() {
      return mutationSince === completedReads;
    },
    /** A mutation answered: request an authoritative GET, or invalidate the
     *  in-flight GET and let its completion start the follow-up. */
    applyMutation() {
      applied += 1;
      if (readSince !== null) {
        stale = true;
        return false;
      }
      return true;
    },
  };
}

/** Identifies the exact B on screen: the sha the server gave it. A confirmation
 *  is bound to it, and Discard/Rollback/Reset send it, so the server refuses
 *  (409) rather than act on a newer B. Null when the row has no kept edit (or
 *  the server named none: no client-side stand-in, the server would not match it). */
export function supersededToken(row) {
  const kept = row && row.prompt_superseded;
  return kept && kept.sha ? kept.sha : null;
}

/** Discard B, and Reset to defaults while a B is kept (it deletes B too), ask
 *  once, inline: the first click arms a confirmation bound to the action and
 *  to the exact B on screen (supersededToken); a second click of that action
 *  on that SAME B confirms. Returns what the caller must do: 'armed' (render
 *  and wait), 'confirmed' (perform the request, then beginMutation/
 *  applyMutationResult/finishMutation), or 'noop' (no row, or no B: nothing to
 *  confirm). Mutates `confirmDiscard` ({agentId, action, token}). */
function requestConfirm(model, row, action, shown, intent) {
  const token = shownToken(row, shown);
  if (!row || !token) return 'noop';
  const armed = model.confirmDiscard;
  // 'ask' (the Discard B / Reset button) only ever arms: confirming takes the
  // confirmation button, which exists only once the question is on screen.
  if (intent !== 'ask' && armed && armed.agentId === row.id && armed.action === action && armed.token === token) return 'confirmed';
  model.confirmDiscard = { agentId: row.id, action, token };
  model.notice = null;
  return 'armed';
}

/** The B a clicked card names: `shown` is the sha the card rendered (null:
 *  the card showed none, even if the model has one since — a focused card is
 *  not rebuilt by a read); undefined, for a caller with no card, is the row's. */
export function shownToken(row, shown) {
  return shown === undefined ? supersededToken(row) : shown;
}

export function requestDiscard(model, row, shown, intent) {
  return requestConfirm(model, row, 'discard', shown, intent);
}

/** 'noop' when the card showed no B: Reset then acts at once, as it always
 *  has, and the server refuses it (409) if a B it never showed exists now. */
export function requestReset(model, row, shown, intent) {
  return requestConfirm(model, row, 'reset', shown, intent);
}

/** Keep B (or Cancel on the Reset question): cancels the armed confirmation for
 *  this agent. True when it did (the caller should render); false when there
 *  was nothing to cancel. */
export function cancelDiscard(model, agentId) {
  if (model.confirmDiscard && model.confirmDiscard.agentId === agentId) {
    model.confirmDiscard = null;
    return true;
  }
  return false;
}

/** A row changed under an armed confirmation — a refresh, another tab's own
 *  mutation, or a later reconcile replacing B again — disarms it: a stale
 *  confirmation must never fire against a B the admin is no longer looking
 *  at. Looks the row up in `model.rows` itself, so the read path can call it
 *  once after assigning a fresh `rows`, without naming which agent. */
export function reconcileDiscardConfirm(model) {
  const armed = model.confirmDiscard;
  if (!armed) return;
  const row = (model.rows || []).find((candidate) => candidate.id === armed.agentId);
  if (!row || !hasSupersededPrompt(row) || supersededToken(row) !== armed.token) {
    model.confirmDiscard = null;
  }
}

/** The draft fields a request changes on the server. Save and Reset send (or
 *  reset) all three; Discard B changes none of them and Rollback to B only the
 *  prompt, so an unsaved edit of any other field must survive their answer. */
export const ALL_DRAFT_FIELDS = Object.freeze(['prompt', 'required', 'model']);

/** One agent mutation (Save/Reset/Discard/Rollback) in flight at a time,
 *  across every card: a rapid second click, on the same agent or another,
 *  must not issue a second request — this is the in-flight guard. Returns
 *  the baseline to replay onto the answer (applyMutationResult): the draft
 *  for each field the request changes (`fields`); for every other field the
 *  baseline an earlier request still waits to settle with (an answer that
 *  could not be applied, in `pendingBaselines`), else the row as shown — so
 *  only what a request set takes the server's value. The waiting baseline
 *  stays until this request succeeds (a failed one settles nothing). Null
 *  when a request is already running, in which case the caller issues none.
 *  Mutates `saving`, `confirmDiscard`, `error`, `notice`. */
export function beginMutation(model, agentId, fields = ALL_DRAFT_FIELDS) {
  if (model.saving) return null;
  const draft = model.drafts[agentId];
  const waiting = (model.pendingBaselines || {})[agentId] || {};
  let submitted = null;
  if (draft) {
    const shown = draftFromAgent(model.rows.find((row) => row.id === agentId));
    submitted = Object.fromEntries(ALL_DRAFT_FIELDS.map((field) => [field,
      fields.includes(field) ? draft[field] : field in waiting ? waiting[field] : shown[field]]));
  }
  model.inflight = submitted ? { agentId, baseline: submitted } : null;
  model.sync.beginMutation();
  model.saving = agentId;
  model.confirmDiscard = null;
  model.error = null;
  model.notice = null;
  return { submitted };
}

/** The request answered with the updated row: merged into `rows`, and the
 *  draft rebased (draftAfterSave) so an edit made while the request was in
 *  flight survives, while a field the admin left alone takes the server's
 *  value. When a read answered while the request ran, its row may be newer
 *  than this answer or older (served before the write committed): neither
 *  row is trusted now, so the mutation's baseline waits in
 *  `pendingBaselines` and the authoritative follow-up read rebases the draft
 *  onto it (takePendingBaselines). Returns whether the caller should start a
 *  follow-up GET. A falsy `savedRow` is a no-op. */
export function applyMutationResult(model, agentId, submitted, savedRow) {
  if (!savedRow) return;
  // `submitted` carries what an older waiting baseline still had to settle
  // (beginMutation), so this answer takes its place.
  const others = { ...model.pendingBaselines };
  delete others[agentId];
  if (model.sync.canApplyMutation()) {
    model.pendingBaselines = others;
    model.rows = replaceAgentRow(model.rows, savedRow);
    model.drafts[agentId] = draftAfterSave(model.drafts[agentId], submitted, savedRow);
  } else if (submitted) {
    model.pendingBaselines = { ...others, [agentId]: submitted };
  }
  reconcileDiscardConfirm(model);
  return model.sync.applyMutation();
}

/** An applied read settles the mutations whose answers waited for it: each
 *  waiting draft is rebased from its mutation's baseline (`previousDrafts`
 *  are the drafts before the read's own rebase), so what the request set
 *  takes the server's value and only typing since dispatch survives. */
export function takePendingBaselines(model, previousDrafts) {
  for (const [agentId, baseline] of Object.entries(model.pendingBaselines || {})) {
    const row = model.rows.find((candidate) => candidate.id === agentId);
    if (row && previousDrafts[agentId]) model.drafts[agentId] = draftAfterSave(previousDrafts[agentId], baseline, row);
  }
  model.pendingBaselines = {};
}

/** A read just rebased the drafts (`previousDrafts` before it): a field it
 *  replaced where the admin had typed nothing since dispatch moves the
 *  in-flight or waiting baseline with it, so the server's change is never
 *  later mistaken for typing. A field typed since dispatch keeps its baseline
 *  — unless the read set that typing aside (`setAside`, kept in the stash) and
 *  put the new default in its place, which is the server's text too. */
export function followReadInBaselines(model, previousDrafts, setAside = []) {
  const waiting = Object.entries(model.pendingBaselines || {});
  if (model.inflight) waiting.push([model.inflight.agentId, model.inflight.baseline]);
  for (const [agentId, baseline] of waiting) {
    const before = previousDrafts[agentId];
    const after = model.drafts[agentId];
    if (!before || !after) continue;
    for (const field of ALL_DRAFT_FIELDS) {
      if (before[field] === baseline[field] || (field === 'prompt' && setAside.includes(agentId))) baseline[field] = after[field];
    }
  }
}

/** The request is over (answered or failed): clears the in-flight guard. */
export function finishMutation(model) {
  model.saving = null;
  model.inflight = null;
}

/** A read landed (rows assigned, drafts rebased onto them). Where the default
 *  prompt changed under an unsaved prompt edit (defaultChangedUnderEdit), the
 *  editor takes the new default and the edit moves to `stash`, shown in the
 *  card until the admin puts it back or drops it: a changed default never
 *  swallows typed text. A second change under a second edit appends to the
 *  first stash rather than replacing it. Returns the ids moved, for the caller
 *  to patch a focused editor render() skips. Mutates `drafts`, `stash`, `notice`. */
export function stashEditsOnDefaultChange(model) {
  const moved = [];
  for (const row of model.rows) {
    const draft = model.drafts[row.id];
    if (!defaultChangedUnderEdit(draft, row)) continue;
    const earlier = Object.hasOwn(model.stash, row.id) ? model.stash[row.id] : null;
    model.stash[row.id] = earlier ? `${earlier}\n\n${draft.prompt}` : draft.prompt;
    model.drafts[row.id] = { ...draft, prompt: draftFromAgent(row).prompt, defaultSha: row.default_sha };
    moved.push(row.id);
  }
  if (moved.length > 0) model.notice = defaultChangedNotice(moved);
  return moved;
}

/** "Put back in editor": the set-aside text becomes the prompt draft, now on
 *  the default the card shows (the admin applies it to that default knowingly,
 *  so a Save names it). A row that names no default (the file is unreadable
 *  just now) keeps the one the draft knew, so the Save still names it and is
 *  refused until the file reads again. True when there was text to put back.
 *  Mutates `drafts`, `stash`. */
export function putBackStash(model, agentId) {
  const row = model.rows.find((candidate) => candidate.id === agentId);
  if (!row || typeof model.stash[agentId] !== 'string') return false;
  const draft = model.drafts[agentId] || draftFromAgent(row);
  model.drafts[agentId] = { ...draft, prompt: model.stash[agentId], defaultSha: row.default_sha || draft.defaultSha };
  delete model.stash[agentId];
  return true;
}

/** "Drop": forget the set-aside text. True when there was some. Mutates `stash`. */
export function dropStash(model, agentId) {
  if (typeof model.stash[agentId] !== 'string') return false;
  delete model.stash[agentId];
  return true;
}
