/** Reviewer agents on the Agents page: pure projection, drafts and validation.
 *
 * Mirrors the server's rules (service.validate_agent_override): a prompt is
 * non-empty once trimmed and at most `max_prompt_chars` characters; `required`
 * is a boolean; a model is a model id that is not Claude, or '' in a draft for
 * "the model policy". The server remains the authority and its 400 is shown verbatim.
 *
 * The prompt is shown and saved back verbatim, so it is never collapsed or
 * clipped here the way one-line labels are.
 */

import { splitLines } from './diff.js';
import { relativeTime } from './format.js';
import { CLAUDE_NOTE, MODEL_ID, isClaudeId } from './models.js';

export const MAX_PROMPT_CHARS = 48000;
export const REQUIRED_NOTE = 'If a required agent does not finish, the review is not posted and the attempt fails. '
  + 'If it judges the patch incorrect or reports a P0/P1, the review is posted and the review-of-legends/required/<base branch> commit status fails.';
/** The [!] marker's label and tooltip on a card whose edit a changed default replaced. */
export const SUPERSEDED_NOTE = 'The default prompt changed, so your edit was replaced by the new default. Click to compare.';
export const DISCARD_CONFIRM = 'Discard B? Your last edit will be deleted and cannot be recovered.';
export const RESET_CONFIRM = 'Reset to defaults? This deletes all dashboard settings of this agent, including B, your last edit. '
  + 'This cannot be undone.';
/** Save requires a change from C; accepting C unchanged is Discard B, not a save. */
export const SUPERSEDED_HINT = 'To keep the new default as is, discard B.';

function text(value, max) {
  if (typeof value !== 'string') return null;
  const cleaned = value.replace(/\s+/g, ' ').trim();
  if (cleaned === '') return null;
  return cleaned.length > max ? cleaned.slice(0, max) : cleaned;
}

/** A sha256 hex digest as the server writes it (service.prompt_sha), else null. */
function sha256Hex(value) {
  return typeof value === 'string' && /^[0-9a-f]{64}$/.test(value) ? value : null;
}

/** Characters as the server counts them (code points, not UTF-16 units). */
export function promptLength(prompt) {
  return typeof prompt === 'string' ? Array.from(prompt).length : 0;
}

/** One GET entry, or a PUT/DELETE answer -> exactly the fields the page renders. */
export function projectAgentRow(row) {
  if (!row || typeof row !== 'object') return null;
  const id = text(row.id, 64);
  if (!id) return null;
  return {
    id,
    focus: text(row.focus, 120) || '',
    repos: Array.isArray(row.repos) ? row.repos.map((repo) => text(repo, 140)).filter(Boolean).slice(0, 50) : [],
    required: row.required === true,
    required_source: row.required_source === 'dashboard' ? 'dashboard' : 'config',
    config_required: row.config_required === true,
    prompt: typeof row.prompt === 'string' ? row.prompt : null,
    // The prompt file's sha now: a draft remembers the one it started from.
    default_sha: sha256Hex(row.default_sha),
    prompt_source: row.prompt_source === 'dashboard' ? 'dashboard' : 'file',
    prompt_superseded: projectSuperseded(row.prompt_superseded),
    model: text(row.model, 80),
    model_source: row.model_source === 'dashboard' ? 'dashboard' : 'default',
    updated_at: text(row.updated_at, 64),
    updated_by: text(row.updated_by, 254),
    error: text(row.error, 300),
  };
}

/** The edit a changed default replaced (`prompt_superseded`): kept verbatim,
 *  like the prompt. `sha` is the server's name for this exact B: Discard,
 *  Rollback and Reset send it back so they never act on a newer B. */
function projectSuperseded(value) {
  if (!value || typeof value !== 'object' || typeof value.prompt !== 'string') return null;
  return {
    prompt: value.prompt,
    sha: sha256Hex(value.sha),
    saved_at: text(value.saved_at, 64),
    saved_by: text(value.saved_by, 254),
    superseded_at: text(value.superseded_at, 64),
  };
}

/** The [!] marker rule: the default changed and replaced this admin's edit. */
export function hasSupersededPrompt(row) {
  return !!row && !!row.prompt_superseded;
}

/** The compare inside the card: C (the new default, in use) on the left, B (the
 *  replaced edit) on the right. The line differences (compareLines) are not
 *  computed here: they are the one part of the compare costly enough to defer
 *  until the disclosure opens, so this stays cheap at page render. Null when
 *  there is nothing to compare. */
export function compareView(row, now = Date.now()) {
  if (!hasSupersededPrompt(row)) return null;
  const kept = row.prompt_superseded;
  const left = typeof row.prompt === 'string' ? row.prompt : '';
  const replaced = kept.superseded_at ? relativeTime(kept.superseded_at, now) : 'earlier';
  const who = kept.saved_by ? ` by ${kept.saved_by}` : '';
  const when = kept.saved_at ? ` ${relativeTime(kept.saved_at, now)}` : '';
  return {
    left: { title: 'C — new default (in use)', text: left },
    right: {
      title: 'B (line view)',
      text: kept.prompt,
      saved_by: kept.saved_by,
      saved_at: kept.saved_at,
      detail: `Saved${who}${who && when ? ',' : ''}${when}. Your last edit, replaced ${replaced}.`,
    },
  };
}

/** B's normalized lines in order, each marked whether it is also in C
 *  (`equal`) or not (`delete`, "not in C"). A line only in C never appears
 *  here. Copy B uses the original stored text, not this line view. Built lazily
 *  by the caller (the compare disclosure's `toggle` handler), not at render.
 *
 *  "Not in C" is literally true: lines are counted, so a moved line is not
 *  marked and a line B has twice and C once is marked once. Linear in the
 *  lines of B and C. */
export function compareLines(b, c) {
  const inC = new Map();
  for (const text of splitLines(c)) inC.set(text, (inC.get(text) || 0) + 1);
  return splitLines(b).map((text) => {
    const left = inC.get(text) || 0;
    inC.set(text, left - 1);
    return { kind: left > 0 ? 'equal' : 'delete', text };
  });
}

/** What a card with a replaced edit offers: the marker for everyone; Discard B
 *  (asked once, inline) and Rollback to B for the admin; nothing for a viewer.
 *
 *  (amended after review) Save keeps C and only lands when the admin's draft
 *  differs from it; accepting C as it is, without editing it, is what Discard
 *  B does. The idle row's `hint` says so, next to the buttons. */
export function supersededControls(row, { readOnly = false, busy = false, confirming = false } = {}) {
  if (!hasSupersededPrompt(row)) return null;
  const marker = { label: SUPERSEDED_NOTE, title: SUPERSEDED_NOTE };
  if (readOnly) return { marker, confirm: null, hint: null, buttons: [] };
  if (confirming) {
    return {
      marker,
      confirm: DISCARD_CONFIRM,
      hint: null,
      buttons: [
        { id: 'confirm-discard', label: 'Confirm discard', disabled: busy, primary: true },
        { id: 'keep', label: 'Keep B', disabled: busy, primary: false },
      ],
    };
  }
  return {
    marker,
    confirm: null,
    hint: SUPERSEDED_HINT,
    buttons: [
      { id: 'discard', label: 'Discard B', disabled: busy, primary: false },
      { id: 'rollback', label: 'Rollback to B', disabled: busy, primary: false },
    ],
  };
}

/** errors.js words every 409 for a retry ("The pull request moved"). Here a
 *  409 means the agent changed on the server since this page read it (another
 *  B, a new default under the edit), so say that; the server's own message
 *  stays as the detail. Any other error is shown as errors.js words it. */
export function agentMutationError(described) {
  if (!described || described.status !== 409) return described;
  return {
    ...described,
    title: 'The agent changed on the server',
    recovery: 'Nothing was changed. Reload to see the agent as it is now; unsaved text in the card is kept.',
  };
}

/** GET /api/settings/agents -> {agents, max_prompt_chars, error}. */
export function projectAgentSettings(payload) {
  const source = payload && typeof payload === 'object' ? payload : {};
  const max = Number.isInteger(source.max_prompt_chars) && source.max_prompt_chars > 0
    ? source.max_prompt_chars
    : MAX_PROMPT_CHARS;
  return {
    agents: (Array.isArray(source.agents) ? source.agents : []).map(projectAgentRow).filter(Boolean),
    max_prompt_chars: max,
    error: text(source.error, 300),
  };
}

export function hasOverride(row) {
  return !!row && (row.prompt_source === 'dashboard' || row.required_source === 'dashboard' || row.model_source === 'dashboard');
}

/** The server holds a document for this agent: an override, or only the edit a
 *  changed default replaced. Either way "Reset to defaults" has something to
 *  remove (the whole document, kept edit included). */
export function hasStoredSetting(row) {
  return hasOverride(row) || hasSupersededPrompt(row);
}

/** The card's Reset to defaults: nothing when the server stores nothing for
 *  the agent. While a B is kept, Reset deletes it too, so the first click asks
 *  inline (RESET_CONFIRM) the way Discard B does; without a B it acts at once. */
export function resetControls(row, { busy = false, confirming = false } = {}) {
  if (!hasStoredSetting(row)) return null;
  if (confirming && hasSupersededPrompt(row)) {
    return {
      confirm: RESET_CONFIRM,
      buttons: [
        { id: 'confirm-reset', label: 'Confirm reset', disabled: busy, primary: true },
        { id: 'cancel-reset', label: 'Cancel', disabled: busy, primary: false },
      ],
    };
  }
  return { confirm: null, buttons: [{ id: 'reset', label: 'Reset to defaults', disabled: busy, primary: false }] };
}

export const NAV_SUPERSEDED_LABEL = 'A default prompt changed and replaced an edit; open Agents to compare.';

/** The Agents nav entry's badge: `[!]` with an accessible label while any agent
 *  shows a replaced edit (viewers included), else `custom` while any agent is
 *  overridden, else nothing. */
export function agentsNavBadge(rows) {
  const list = Array.isArray(rows) ? rows : [];
  if (list.some(hasSupersededPrompt)) return { text: '[!]', label: NAV_SUPERSEDED_LABEL };
  if (list.some(hasOverride)) return { text: 'custom', label: null };
  return { text: '', label: null };
}

export function describeRuns(row) {
  return row && row.repos.length > 0 ? `Runs on: ${row.repos.join(', ')}` : 'Runs on no repository (opt-in)';
}

/** `defaultSha` is the prompt file the draft's prompt was started from: a Save
 *  sends it back (expected_default_sha), so an edit of an older default is
 *  refused instead of silently hiding the new one. */
export function draftFromAgent(row) {
  return {
    prompt: row && typeof row.prompt === 'string' ? row.prompt : '',
    required: !!(row && row.required),
    model: row && typeof row.model === 'string' ? row.model : '',
    defaultSha: row && typeof row.default_sha === 'string' ? row.default_sha : null,
  };
}

/** A draft's model; '' is "no override: the model policy". */
function draftModel(draft) {
  return draft && typeof draft.model === 'string' ? draft.model : '';
}

/** The default a draft's prompt was started from; null when unknown. */
function draftDefaultSha(draft) {
  return draft && typeof draft.defaultSha === 'string' ? draft.defaultSha : null;
}

/** What a card shows: the admin's draft, or the row itself for a viewer. */
export function shownDraft(row, draft, readOnly) {
  return readOnly || !draft ? draftFromAgent(row) : draft;
}

/** Per field: an edit relative to `baseline` survives, an untouched field takes
 *  `fresh` (the server's newer value). The default a prompt was started from
 *  goes with the prompt: an edited prompt keeps its own, a fresh one takes the
 *  row's — and so does an edit that already is the fresh prompt (as Save
 *  compares, trimmed), since there is nothing of the older default left in it. */
function rebaseDraft(current, baseline, fresh) {
  const promptEdited = current.prompt !== baseline.prompt;
  const ownBase = promptEdited && current.prompt.trim() !== (fresh.prompt || '').trim();
  return {
    prompt: promptEdited ? current.prompt : fresh.prompt,
    required: current.required !== baseline.required ? current.required : fresh.required,
    model: draftModel(current) !== draftModel(baseline) ? draftModel(current) : draftModel(fresh),
    // A fresh row with no default (its file unreadable just now) does not erase
    // the one the draft knew: a later save still names it and gets its 409.
    defaultSha: draftDefaultSha(ownBase ? current : fresh) || draftDefaultSha(current),
  };
}

/** The draft once a save or reset answers. A field edited while the request was
 *  in flight keeps the edit (the next Save sends it); an untouched field takes
 *  the server's value. */
export function draftAfterSave(current, submitted, saved) {
  const fresh = draftFromAgent(saved);
  if (!current || !submitted) return fresh;
  return rebaseDraft(current, submitted, fresh);
}

function promptChanged(draft, row) {
  return draft.prompt.trim() !== (row.prompt || '').trim();
}

function modelChanged(draft, row) {
  return draftModel(draft).trim() !== (row.model || '');
}

/** The default prompt changed under an unsaved prompt edit: the server now
 *  names another prompt file than the one the edit was started from. Such an
 *  edit can no longer be saved (409), and left in the editor titled "C — new
 *  default" it would hide C, so a read sets it aside (stashEditsOnDefaultChange).
 *  A row naming no default (the file is unreadable) has no new default to show. */
export function defaultChangedUnderEdit(draft, row) {
  return !!draft && !!row && typeof row.default_sha === 'string'
    && draftDefaultSha(draft) !== row.default_sha && promptChanged(draft, row);
}

/** The notice for the agents whose unsaved prompt a read set aside. */
export function defaultChangedNotice(agentIds) {
  return `The default prompt of ${agentIds.join(', ')} changed while you were editing; `
    + 'the editor shows the new default. Your unsaved text is kept below.';
}

/** True when saving the draft would change something on the server. */
export function agentDraftChanged(draft, row) {
  if (!draft || !row) return false;
  return promptChanged(draft, row) || draft.required !== row.required || modelChanged(draft, row);
}

/** Only an edited prompt is checked: toggling `required` on an agent whose
 *  prompt file is unreadable must still be possible. */
export function validateAgentDraft(draft, row, maxChars = MAX_PROMPT_CHARS) {
  const errors = [];
  if (draft && row && promptChanged(draft, row)) {
    const trimmed = draft.prompt.trim();
    const length = promptLength(trimmed);
    if (length === 0) errors.push('The prompt cannot be empty.');
    else if (length > maxChars) errors.push(`The prompt is ${length} characters; the limit is ${maxChars}.`);
  }
  if (draft && row && modelChanged(draft, row)) {
    const model = draftModel(draft).trim();
    if (model !== '' && !MODEL_ID.test(model)) errors.push(`"${model}" is not a valid model id.`);
    if (isClaudeId(model)) errors.push(CLAUDE_NOTE);
  }
  return { ok: errors.length === 0, errors };
}

/** PUT body with only what changed. Returning `required` to the config value
 *  clears the override (null) instead of pinning a copy of the config; so does
 *  choosing the default model. A prompt names the default it was edited from:
 *  the server refuses it (409) when the prompt file changed since. */
export function agentRequestBody(draft, row) {
  const body = {};
  if (promptChanged(draft, row)) {
    body.prompt = draft.prompt.trim();
    if (draftDefaultSha(draft)) body.expected_default_sha = draftDefaultSha(draft);
  }
  if (draft.required !== row.required) {
    body.required = draft.required === row.config_required ? null : draft.required;
  }
  if (modelChanged(draft, row)) {
    const model = draftModel(draft).trim();
    body.model = model === '' ? null : model;
  }
  return body;
}

/** The quiet line under an agent's model control: what runs, in order. Mirrors
 *  service.agent_model_ladder: the agent's model leads, then every model of the
 *  policy's ladder once, never cut. `policy` is {model, fallback_models, source},
 *  or null when unknown. */
export function describeAgentModel(policy, model) {
  if (!model) return 'Runs on the default ladder';
  if (!policy || !policy.model) return `Runs on ${model}, then the default ladder if it fails`;
  // A retry's pinned model is what runs; say so instead of the agent's model.
  if (policy.source === 'request') return `Runs on ${policy.model}: the retry's pinned model beats this agent's model`;
  const fallbacks = [...new Set([policy.model, ...(policy.fallback_models || [])])]
    .filter((candidate) => candidate !== model);
  return fallbacks.length > 0
    ? `Runs on ${model}, then ${fallbacks.join(' → ')} if it fails`
    : `Runs on ${model}, with no fallback`;
}

/** What the editor controls show for a draft; also used to patch them while typing. */
export function agentEditorState(row, draft, { saving = false, maxChars = MAX_PROMPT_CHARS } = {}) {
  const changed = agentDraftChanged(draft, row);
  const validation = validateAgentDraft(draft, row, maxChars);
  const edited = !!draft && promptChanged(draft, row);
  const summary = row.prompt === null && !edited
    ? 'Prompt — not available'
    : `Prompt — ${promptLength(draft ? draft.prompt : row.prompt).toLocaleString('en-US')} chars, ${edited ? 'edited' : `from ${row.prompt_source}`}`;
  return { changed, errors: validation.errors, canSave: changed && validation.ok && !saving, summary };
}

/** After a refresh (another tab may have saved): rebase each draft on the new
 *  row field by field. What this operator edited survives; every field they did
 *  not touch takes the server's value, so their next Save cannot send a stale
 *  copy of a field another tab changed. */
export function refreshAgentDrafts(previousRows, drafts, rows) {
  const before = new Map((previousRows || []).map((row) => [row.id, row]));
  const next = {};
  for (const row of rows) {
    const draft = drafts ? drafts[row.id] : null;
    const old = before.get(row.id);
    next[row.id] = draft && old ? rebaseDraft(draft, draftFromAgent(old), draftFromAgent(row)) : draftFromAgent(row);
  }
  return next;
}

export function replaceAgentRow(rows, row) {
  return rows.map((existing) => (existing.id === row.id ? row : existing));
}
