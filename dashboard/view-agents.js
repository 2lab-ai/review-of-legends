/** Agents: each reviewer agent's prompt, required flag and model.
 *
 * Every signed-in account reads the cards; only the admin edits them. Like
 * Settings, a card edits a draft and nothing changes on the server until Save.
 * The model control offers the llmux catalog; "Default" means no override, so
 * the agent runs the model policy shown on the Settings page.
 */

import {
  REQUIRED_NOTE, agentEditorState, compareLines, compareView, describeAgentModel, describeRuns, resetControls, shownDraft,
  supersededControls,
} from './agents.js';
import { domId, el, replace, srOnly, statusChip } from './dom.js';
import { formatTimestamp, relativeTime } from './format.js';
import { modelOptions } from './view-settings.js';

function agentProvenance(row, now) {
  const parts = [];
  if (row.required_source === 'dashboard') parts.push('required');
  if (row.prompt_source === 'dashboard') parts.push('prompt');
  if (row.model_source === 'dashboard') parts.push('model');
  if (parts.length === 0) return 'from config/reviewer.json';
  const fields = parts.length > 1 ? `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}` : parts[0];
  return `${fields} set on this dashboard${row.updated_by ? ` by ${row.updated_by}` : ''}`
    + `${row.updated_at ? `, ${relativeTime(row.updated_at, now)}` : ''}`;
}

/** The model select and the line saying what runs. `policy` is the model policy
 *  in effect ({model, effort, fallback_models, source}) or null when unknown. */
function modelControl(row, shown, { catalog, policy, readOnly, busy, onModel }) {
  const base = `agent-${row.id}`;
  return el('div', { class: 'filters__group' }, [
    el('label', { class: 'control__label', for: `${base}-model`, text: 'Model' }),
    el('select', {
      class: 'control control--select',
      id: `${base}-model`,
      disabled: readOnly || busy,
      'aria-describedby': `${base}-model-runs`,
      on: readOnly ? null : { change: (event) => onModel(row.id, event.target.value) },
    }, modelOptions(catalog, shown.model, {
      allowEmpty: true,
      emptyLabel: policy && policy.model ? `Default — ${policy.model}` : 'Default',
    })),
    el('p', { class: 'prose prose--quiet', id: `${base}-model-runs`, text: describeAgentModel(policy, shown.model) }),
  ]);
}

/** Open an agent's prompt disclosure (the [!] marker's click): the compare lives
 *  inside it. Setting `.open` queues the browser's own `toggle` notification
 *  (fired for a script change exactly as for a click), which is what actually
 *  builds the diff rows (see comparePanes/ensureDiffBuilt below) — this
 *  function only has to open the box. */
function openPrompt(detailsId) {
  const details = document.getElementById(detailsId);
  if (!details) return;
  details.open = true;
  if (typeof details.scrollIntoView === 'function') details.scrollIntoView({ block: 'nearest' });
}

/** A line of B, marked whether it is also in C ('equal') or not ('delete'). */
function diffLine(line) {
  const notInC = line.kind === 'delete';
  return el('li', { class: `diff__line diff__line--${line.kind}` }, [
    el('span', { class: 'diff__marker', 'aria-hidden': 'true', text: notInC ? '−' : ' ' }),
    notInC ? srOnly('not in C: ') : null,
    el('span', { class: 'diff__text', text: line.text }),
  ]);
}

/** The compare: C (the new default, the editable textarea) on the left, B (the
 *  replaced edit) as a line view on the right, its lines not in C marked. Stacks on
 *  a narrow screen (styles.css). The textarea keeps its id and class, so
 *  typing, scroll restore and the deferred render treat it like any prompt
 *  editor.
 *
 *  The `<ol class="diff">` starts empty: `list` is returned so the caller can
 *  fill it lazily, when the disclosure opens, instead of computing the line
 *  view (compareLines) on every page render. */
function comparePanes(row, compare, textarea) {
  const base = `agent-${row.id}`;
  const copyStatus = el('p', { class: 'prose prose--quiet', id: `${base}-copy-status`, role: 'status' });
  const copyButton = el('button', {
    class: 'button', type: 'button', id: `${base}-copy-b`, text: 'Copy B',
    on: { click: async () => {
      try {
        const clipboard = globalThis.navigator && globalThis.navigator.clipboard;
        if (!clipboard || typeof clipboard.writeText !== 'function') throw new Error('Clipboard unavailable');
        await clipboard.writeText(compare.right.text);
        copyStatus.textContent = 'B copied exactly.';
      } catch {
        copyStatus.textContent = 'Could not copy B. Allow clipboard access in your browser and try again.';
      }
    } },
  });
  const list = el('ol', {
    class: 'diff',
    'aria-labelledby': `${base}-compare-right`,
    'aria-describedby': `${base}-compare-legend`,
  }, []);
  const node = el('div', { class: 'compare' }, [
    el('div', { class: 'compare__pane' }, [
      el('h4', { class: 'compare__title', id: `${base}-compare-left`, text: compare.left.title }),
      textarea,
    ]),
    el('div', { class: 'compare__pane' }, [
      el('h4', { class: 'compare__title', id: `${base}-compare-right`, text: compare.right.title }),
      el('p', {
        class: 'prose prose--quiet',
        text: compare.right.detail,
        title: compare.right.saved_at ? formatTimestamp(compare.right.saved_at) : null,
      }),
      copyButton,
      copyStatus,
      list,
      el('p', { class: 'prose prose--quiet', id: `${base}-compare-legend`, text: 'Marked lines: − not in C (only in B, your edit).' }),
    ]),
  ]);
  return { node, list };
}

/** Fills the empty `<ol class="diff">` the first time the disclosure opens,
 *  then does nothing: the lines do not change without a full page render,
 *  which rebuilds this box from scratch anyway. */
function ensureDiffBuilt(panes, compare) {
  if (!panes || panes.list.childNodes.length > 0) return;
  replace(panes.list, compareLines(compare.right.text, compare.left.text).map(diffLine));
}

/** One of a card's action buttons; `describedBy` names the inline question it answers. */
function actionButton(base, button, onClick, describedBy = null) {
  return el('button', {
    class: button.primary ? 'button button--primary' : 'button',
    type: 'button',
    id: `${base}-${button.id}`,
    disabled: button.disabled,
    'aria-describedby': describedBy,
    text: button.label,
    on: { click: onClick },
  });
}

/** Discard B (asks once, inline, never a browser dialog) and Rollback to B. */
function supersededActions(row, kept, { onDiscard, onRollback, onKeep }) {
  const base = `agent-${row.id}`;
  if (kept.buttons.length === 0) return null;
  // The B this card compares: a request names it, even if the model moved on.
  const shownSha = row.prompt_superseded ? row.prompt_superseded.sha : null;
  const handler = (id) => {
    if (id === 'rollback') return () => onRollback(row.id, shownSha);
    if (id === 'keep') return () => onKeep(row.id);
    // 'discard' asks for the confirmation; 'confirm-discard' performs it.
    return () => onDiscard(row.id, shownSha, id === 'confirm-discard' ? 'confirm' : 'ask');
  };
  const confirmId = kept.confirm ? `${base}-discard-confirm` : null;
  return el('div', { class: 'agent__superseded' }, [
    kept.confirm ? el('p', { class: 'notice', role: 'alert', id: confirmId, text: kept.confirm }) : null,
    el('div', { class: 'policy__actions' }, kept.buttons.map((button) => actionButton(base, button, handler(button.id), confirmId))),
    kept.hint ? el('p', { class: 'prose prose--quiet', id: `${base}-superseded-hint`, text: kept.hint }) : null,
  ]);
}

/** Reset's question while it would also delete B (resetControls), in place of
 *  the Reset button: 'confirm-reset' performs it, 'cancel-reset' disarms it as
 *  Keep B does. */
function resetQuestion(row, reset, { onReset, onKeep }) {
  const base = `agent-${row.id}`;
  const confirmId = `${base}-reset-confirm`;
  const shownSha = row.prompt_superseded ? row.prompt_superseded.sha : null;
  const handler = (id) => (id === 'cancel-reset' ? () => onKeep(row.id) : () => onReset(row.id, shownSha, 'confirm'));
  return el('div', { class: 'agent__superseded' }, [
    el('p', { class: 'notice', role: 'alert', id: confirmId, text: reset.confirm }),
    el('div', { class: 'policy__actions' }, reset.buttons.map((button) => actionButton(base, button, handler(button.id), confirmId))),
  ]);
}

/** The prompt disclosure: a plain textarea when there is nothing to compare,
 *  else the compare's two panes (its diff rows filled lazily, on `toggle`) and
 *  Discard B / Rollback to B. A stable id: render() keeps an opened prompt
 *  open across re-renders (captureOpenDetails), which fires `toggle` again
 *  (the browser queues it for any `open` change, scripted or not) — the fill
 *  is idempotent, so that costs nothing. */
function buildPromptDetails(row, { base, detailsId, compare, textarea, kept, editor, onDiscard, onRollback, onKeep }) {
  const panes = compare ? comparePanes(row, compare, textarea) : null;
  return el('details', {
    class: 'agent__prompt',
    id: detailsId,
    on: compare ? { toggle: () => ensureDiffBuilt(panes, compare) } : null,
  }, [
    el('summary', { class: 'agent__summary', id: `${base}-count`, text: editor.summary }),
    panes ? panes.node : textarea,
    kept ? supersededActions(row, kept, { onDiscard, onRollback, onKeep }) : null,
  ]);
}

/** Unsaved prompt text a changed default set aside (agents-controller.js
 *  stashEditsOnDefaultChange): read-only, with Put back and Drop. Not an
 *  `agent__textarea`: app.js holds page renders only for a focused prompt editor. */
function stashBlock(row, text, { onPutBack, onDropStash }) {
  const base = `agent-${row.id}`;
  return el('div', { class: 'agent__superseded', role: 'group', 'aria-labelledby': `${base}-stash-title` }, [
    el('h4', { class: 'compare__title', id: `${base}-stash-title`, text: 'Your unsaved text, set aside when the default prompt changed' }),
    el('textarea', {
      class: 'control agent__stash', id: `${base}-stash`, readonly: true, spellcheck: 'false', rows: '8',
      'aria-labelledby': `${base}-stash-title`, value: text,
    }),
    el('div', { class: 'policy__actions' }, [
      el('button', { class: 'button', type: 'button', id: `${base}-stash-restore`, text: 'Put back in editor', on: { click: () => onPutBack(row.id) } }),
      el('button', { class: 'button', type: 'button', id: `${base}-stash-drop`, text: 'Drop', on: { click: () => onDropStash(row.id) } }),
    ]),
  ]);
}

function agentCard(row, {
  draft, stash, saving, readOnly, maxChars, now, catalog, policy, confirmingDiscard,
  onPromptInput, onRequired, onModel, onSave, onReset, onDiscard, onRollback, onKeep, onPutBack, onDropStash,
}) {
  const base = `agent-${row.id}`;
  const detailsId = domId('agent-prompt', row.id);
  const shown = shownDraft(row, draft, readOnly);
  const editor = agentEditorState(row, shown, { saving: saving !== null, maxChars });
  const busy = saving !== null;
  // The default prompt changed and replaced this admin's edit: the card shows
  // the [!] and the prompt area compares the two (supersededControls).
  const armed = confirmingDiscard && confirmingDiscard.agentId === row.id ? confirmingDiscard.action : null;
  const kept = supersededControls(row, { readOnly, busy, confirming: armed === 'discard' });
  const reset = readOnly ? null : resetControls(row, { busy, confirming: armed === 'reset' });
  const compare = kept ? compareView(row, now) : null;
  const textarea = el('textarea', {
    class: 'control agent__textarea',
    id: `${base}-prompt`,
    'aria-label': compare ? `Prompt for ${row.id}: the new default, in use` : `Prompt for ${row.id}`,
    readonly: readOnly,
    spellcheck: 'false',
    rows: '18',
    value: shown.prompt,
    on: readOnly ? null : { input: (event) => onPromptInput(row.id, event.target.value) },
  });
  return el('article', { class: 'agent', 'aria-labelledby': `${base}-title` }, [
    el('div', { class: 'agent__head' }, [
      el('h3', { class: 'agent__title mono', id: `${base}-title`, text: row.id }),
      row.focus ? el('span', { class: 'agent__focus', text: row.focus }) : null,
      kept ? el('button', {
        class: 'agent__alert',
        type: 'button',
        id: `${base}-alert`,
        'aria-label': kept.marker.label,
        'aria-controls': detailsId,
        title: kept.marker.title,
        text: '[!]',
        on: { click: () => openPrompt(detailsId) },
      }) : null,
    ]),
    el('div', { class: 'agent__meta' }, [
      statusChip(row.required ? 'required' : 'optional', row.required ? 'active' : 'muted'),
      el('span', { class: 'quiet', text: agentProvenance(row, now), title: row.updated_at ? formatTimestamp(row.updated_at) : null }),
    ]),
    el('p', { class: 'prose prose--quiet', text: describeRuns(row) }),
    row.error ? el('p', { class: 'notice', role: 'status', text: row.error }) : null,
    modelControl(row, shown, { catalog, policy, readOnly, busy, onModel }),
    buildPromptDetails(row, { base, detailsId, compare, textarea, kept, editor, onDiscard, onRollback, onKeep }),
    !readOnly && typeof stash === 'string' ? stashBlock(row, stash, { onPutBack, onDropStash }) : null,
    readOnly ? null : el('label', { class: 'agent__required', for: `${base}-required` }, [
      el('input', {
        type: 'checkbox',
        id: `${base}-required`,
        checked: shown.required,
        disabled: busy,
        on: { change: (event) => onRequired(row.id, event.target.checked) },
      }),
      el('span', { text: 'Required review' }),
    ]),
    readOnly ? null : el('ul', { class: 'policy__errors', id: `${base}-errors`, role: 'alert', hidden: editor.errors.length === 0 },
      editor.errors.map((message) => el('li', { text: message }))),
    readOnly ? null : el('div', { class: 'policy__actions' }, [
      el('button', {
        class: 'button button--primary',
        type: 'button',
        id: `${base}-save`,
        disabled: !editor.canSave,
        'aria-busy': saving === row.id ? 'true' : null,
        text: saving === row.id ? 'Saving...' : 'Save',
        on: { click: () => onSave(row.id) },
      }),
      // Also when only a replaced edit is stored: reset removes the whole document.
      reset && !reset.confirm ? actionButton(base, reset.buttons[0],
        () => onReset(row.id, row.prompt_superseded ? row.prompt_superseded.sha : null, 'ask')) : null,
    ]),
    reset && reset.confirm ? resetQuestion(row, reset, { onReset, onKeep }) : null,
  ]);
}

/** A read replaced this agent's prompt draft (the default changed under it).
 *  render() skips a focused editor, so its text is set here; the caller then
 *  patches the controls that depend on it (syncAgentEditor). */
export function showAgentPrompt(agentId, prompt) {
  const textarea = document.getElementById(`agent-${agentId}-prompt`);
  if (textarea) textarea.value = prompt;
}

/** Patch what depends on a prompt draft without rebuilding its textarea: a
 *  rebuild per keystroke would cancel IME composition and wipe the undo history. */
export function syncAgentEditor(agentId, editor, { saving = false } = {}) {
  const base = `agent-${agentId}`;
  const save = document.getElementById(`${base}-save`);
  if (save) {
    save.disabled = !editor.canSave;
    save.textContent = saving ? 'Saving...' : 'Save';
  }
  const count = document.getElementById(`${base}-count`);
  if (count) count.textContent = editor.summary;
  const errors = document.getElementById(`${base}-errors`);
  if (errors) {
    replace(errors, editor.errors.map((message) => el('li', { text: message })));
    errors.hidden = editor.errors.length === 0;
  }
}

export function renderAgents({
  model,
  catalog = null,
  policy = null,
  readOnly = false,
  now = Date.now(),
  modelsError = null,
  modelsLoading = false,
  onPromptInput,
  onRequired,
  onModel,
  onSave,
  onReset,
  onDiscard,
  onRollback,
  onKeep,
  onPutBack,
  onDropStash,
  onReload,
  onReloadModels,
}) {
  const body = [el('p', { class: 'prose prose--quiet', text: REQUIRED_NOTE })];
  // The Model controls need the catalog and the policy (the Settings page's
  // loads); without them every control offers only Default, so say why.
  const modelsProblem = modelsError
    ? [modelsError.title, modelsError.detail].filter(Boolean).join(' — ')
    : (catalog && !catalog.available ? (catalog.error || 'llmux did not answer the catalog request.') : null);
  if (modelsProblem && !modelsLoading) {
    body.push(el('div', { class: 'notice notice--strong', role: 'alert' }, [
      el('strong', { text: 'The model list could not be loaded' }),
      el('span', { class: 'notice__detail', text: modelsProblem }),
      el('span', { class: 'notice__detail', text: 'Each Model control offers only Default until it loads; saved agent models still apply.' }),
      el('button', { class: 'button', type: 'button', text: 'Reload models', on: { click: onReloadModels } }),
    ]));
  }
  if (model.error) {
    body.push(el('div', { class: 'notice notice--strong', role: 'alert' }, [
      el('strong', { text: model.error.title }),
      model.error.detail ? el('span', { class: 'notice__detail', text: model.error.detail }) : null,
      el('span', { class: 'notice__detail', text: model.error.recovery }),
      el('button', { class: 'button', type: 'button', text: 'Reload', on: { click: onReload } }),
    ]));
  }
  if (model.configError) body.push(el('p', { class: 'notice', role: 'status', text: model.configError }));
  if (model.notice) body.push(el('div', { class: 'notice', role: 'status' }, [el('span', { text: model.notice })]));
  if (model.rows.length > 0) {
    body.push(el('div', { class: 'agents' }, model.rows.map((row) => agentCard(row, {
      draft: model.drafts[row.id],
      stash: model.stash ? model.stash[row.id] : undefined,
      saving: model.saving,
      readOnly,
      maxChars: model.maxChars,
      now,
      catalog,
      policy,
      confirmingDiscard: model.confirmDiscard,
      onPromptInput,
      onRequired,
      onModel,
      onSave,
      onReset,
      onDiscard,
      onRollback,
      onKeep,
      onPutBack,
      onDropStash,
    }))));
  } else if (model.loaded) {
    body.push(el('p', { class: 'empty', text: 'The service reported no reviewer agents.' }));
  }
  if (model.loading) body.push(el('p', { class: 'loading', role: 'status', text: 'Loading the reviewer agents...' }));
  body.push(el('p', { class: 'prose prose--quiet', text: readOnly
    ? 'Read-only account: prompts, required flags and models are shown; only the admin account can change them.'
    : 'Saving applies to the next review attempt the service starts; attempts already running keep their prompts and models. A retry that picks a model runs every agent on it.' }));
  return el('section', { class: 'panel', 'aria-labelledby': 'agents-heading' }, [
    el('div', { class: 'panel__head' }, [
      el('h1', { class: 'panel__title', id: 'agents-heading', text: 'Agents' }),
      el('p', { class: 'panel__intent', text: 'Each reviewer agent: its prompt, whether it must finish, and the model it runs on.' }),
    ]),
    ...body,
  ]);
}
