/** Agent request and button handlers shared by app.js and DOM integration tests.
 *  The app supplies its current model, render scheduling and session error handling.
 */
import {
  agentEditorState, agentMutationError, agentRequestBody, draftFromAgent, projectAgentRow,
  projectAgentSettings, refreshAgentDrafts,
} from './agents.js';
import {
  ALL_DRAFT_FIELDS, applyMutationResult, beginMutation, cancelDiscard, dropStash, finishMutation, putBackStash,
  followReadInBaselines, reconcileDiscardConfirm, requestDiscard, requestReset, shownToken, stashEditsOnDefaultChange,
  takePendingBaselines,
} from './agents-controller.js';
import { showAgentPrompt, syncAgentEditor } from './view-agents.js';

/** `releasePromptFocus(agentIds)` blurs the focused prompt editor when it is one
 *  of these agents' (app.js); `editingPrompt()` says whether one has focus. */
export function createAgentHandlers({ getModel, api, render, handleError, editingPrompt = () => false, releasePromptFocus = () => {} }) {
  /** `keepError`: a refused Discard/Rollback/Reset re-reads with its error still on screen. */
  async function loadAgentSettings({ keepDrafts = true, keepError = false } = {}) {
    const model = getModel();
    // A read already in flight is marked stale instead, and reads once more: a
    // stale answer (one that started before a Save/Reset/Discard/Rollback
    // landed) must not resurrect the row that mutation just replaced.
    if (!model.sync.startRead()) return;
    model.loading = true;
    if (!keepError) model.error = null;
    render();
    let again = false;
    let readError = null;
    let answered = false;
    let replaced = [];
    let setAside = [];
    try {
      const payload = projectAgentSettings(await api.agentSettings());
      answered = true;
      if (getModel() === model && model.sync.canApplyRead()) {
        const shownBefore = new Map(Object.entries(model.drafts).map(([agentId, draft]) => [agentId, draft && draft.prompt]));
        const previousDrafts = model.drafts;
        // A refresh keeps a prompt the admin is still editing; a first load starts clean.
        model.drafts = refreshAgentDrafts(keepDrafts ? model.rows : [], keepDrafts ? model.drafts : {}, payload.agents);
        model.rows = payload.agents;
        // Mutations whose answers waited for an authoritative read settle now.
        if (keepDrafts) takePendingBaselines(model, previousDrafts);
        model.maxChars = payload.max_prompt_chars;
        model.configError = payload.error;
        model.loaded = true;
        // The default changed under an unsaved prompt edit: show the new
        // default, keep the edit in the card (never lost, never saved on a stale base).
        setAside = stashEditsOnDefaultChange(model);
        // A request still in flight: what this read changed is the server's, not typing.
        if (keepDrafts) followReadInBaselines(model, previousDrafts, setAside);
        // Every editor whose text this read replaced (a new default under an
        // untouched prompt, or unsaved text just set aside) must show it.
        replaced = Object.keys(model.drafts).filter((agentId) => shownBefore.has(agentId)
          && model.drafts[agentId].prompt !== shownBefore.get(agentId));
        // A Discard B confirmation is moot once another tab discarded, restored,
        // saved over it, or the default changed again: the B it was bound to is
        // no longer the one on screen.
        reconcileDiscardConfirm(model);
      }
    } catch (error) {
      handleError(error, (described) => {
        model.error = described;
        readError = described;
      });
    } finally {
      model.loading = false;
      again = model.sync.finishRead(answered);
      // A focused editor whose unsaved text this read set aside is released, so
      // render() redraws its card now: the set-aside text and Put back/Drop show
      // at once (its text was replaced anyway), and the next keystroke cannot
      // land in the new default.
      if (setAside.length && editingPrompt()) releasePromptFocus(setAside);
      render();
      // render() skips a focused editor; one whose text this read replaced must show it anyway.
      for (const agentId of replaced) {
        const row = agentRow(agentId);
        if (!row) continue;
        showAgentPrompt(agentId, model.drafts[agentId].prompt);
        syncAgentEditor(agentId, agentEditorState(row, model.drafts[agentId], { saving: model.saving !== null, maxChars: model.maxChars }),
          { saving: model.saving === agentId });
      }
    }
    // The follow-up clears only this read's own failure: an error a mutation
    // reported meanwhile (its re-read was folded into this one) stays.
    if (again && getModel() === model) {
      return loadAgentSettings({ keepDrafts: true, keepError: model.error !== null && model.error !== readError });
    }
  }

  function agentRow(agentId) {
    return getModel().rows.find((row) => row.id === agentId) || null;
  }

  /** Typing in a prompt: state only, plus the controls that depend on it. A
   *  render() here would rebuild the focused textarea mid-composition. */
  function editAgentPrompt(agentId, prompt) {
    const model = getModel();
    const row = agentRow(agentId);
    if (!row) return;
    model.drafts[agentId] = { ...(model.drafts[agentId] || draftFromAgent(row)), prompt };
    model.notice = null;
    syncAgentEditor(
      agentId,
      agentEditorState(row, model.drafts[agentId], { saving: model.saving !== null, maxChars: model.maxChars }),
      { saving: model.saving === agentId },
    );
  }

  function editAgentRequired(agentId, required) {
    const model = getModel();
    const row = agentRow(agentId);
    if (!row) return;
    model.drafts[agentId] = { ...(model.drafts[agentId] || draftFromAgent(row)), required };
    model.notice = null;
    render();
  }

  /** '' is the default: no override, the agent runs the model policy. */
  function editAgentModel(agentId, modelId) {
    const model = getModel();
    const row = agentRow(agentId);
    if (!row) return;
    model.drafts[agentId] = { ...(model.drafts[agentId] || draftFromAgent(row)), model: modelId };
    model.notice = null;
    render();
  }

  /** `fields`: the draft fields the request changes (beginMutation).
   *  `rereadOnError`: Discard/Rollback/Reset act on the B on screen; a refusal
   *  (409: the server holds another B) is followed by one authoritative read
   *  so the card shows the B there is now. */
  async function mutateAgent(agentId, request, notice, { fields = ALL_DRAFT_FIELDS, rereadOnError = false } = {}) {
    const model = getModel();
    // The in-flight guard (one Save/Reset/Discard/Rollback at a time, across
    // every card): a rapid second click issues no second request.
    const begun = beginMutation(model, agentId, fields);
    if (!begun) return;
    render();
    let readNow = false;
    let failed = false;
    let promptReplaced = false;
    let setAside = [];
    try {
      const saved = projectAgentRow(await request());
      // The prompt stays editable while the request is in flight; what is typed
      // meanwhile must survive the answer (applyMutationResult -> draftAfterSave).
      if (getModel() !== model) return;
      const shownBefore = model.drafts[agentId] ? model.drafts[agentId].prompt : null;
      const canApply = model.sync.canApplyMutation();
      readNow = applyMutationResult(model, agentId, begun.submitted, saved);
      model.notice = notice;
      // An accepted mutation row can bring a changed default too. Recover
      // unsaved text now, even if the authoritative follow-up read fails.
      if (canApply && saved) setAside = stashEditsOnDefaultChange(model);
      promptReplaced = !!model.drafts[agentId] && model.drafts[agentId].prompt !== shownBefore;
    } catch (error) {
      // Not called when the session ended (handleError signs out): no re-read then.
      handleError(error, (described) => {
        model.error = agentMutationError(described);
        failed = true;
      });
    } finally {
      finishMutation(model);
      if (setAside.length && editingPrompt()) releasePromptFocus(setAside);
      render();
      const row = agentRow(agentId);
      // render() skips a focused prompt editor; its Save button still has to leave
      // "Saving...", and a prompt the answer replaced (Rollback to B) must show.
      if (row && model.drafts[agentId] && editingPrompt()) {
        if (promptReplaced) showAgentPrompt(agentId, model.drafts[agentId].prompt);
        syncAgentEditor(agentId, agentEditorState(row, model.drafts[agentId], { maxChars: model.maxChars }));
      }
    }
    if (getModel() !== model) return;
    if (failed && rereadOnError) return loadAgentSettings({ keepDrafts: true, keepError: true });
    if (readNow) return loadAgentSettings({ keepDrafts: true });
  }

  function saveAgent(agentId) {
    const row = agentRow(agentId);
    const draft = getModel().drafts[agentId];
    if (!row || !draft) return;
    const body = agentRequestBody(draft, row);
    if (Object.keys(body).length === 0) return;
    // Only the fields the PUT sends are this save's to settle: one it leaves out
    // (the row already shows the draft's value) may still be an earlier save's.
    return mutateAgent(agentId, () => api.putAgentSetting(agentId, body), `Saved ${agentId}. The next review attempt uses it.`,
      { fields: ALL_DRAFT_FIELDS.filter((field) => field in body) });
  }

  /** Reset to defaults. While a B is kept it deletes B too, so it asks first,
   *  inline, like Discard B (requestReset), and names that B: the server
   *  refuses (409) when its B is another. With no B it acts at once. */
  function resetAgent(agentId, shownSha, intent) {
    const model = getModel();
    if (model.saving) return;
    const row = agentRow(agentId);
    if (requestReset(model, row, shownSha, intent) === 'armed') {
      render();
      return;
    }
    const sha = shownToken(row, shownSha);
    return mutateAgent(agentId, () => api.deleteAgentSetting(agentId, sha),
      `${agentId} is back on its prompt file, the config default and the default model.`, { rereadOnError: true });
  }

  /** Discard B: the first click asks, inline in the card (never window.confirm),
   *  bound to the exact B on screen (requestDiscard); the confirm button's
   *  click, the same handler, performs it. A row whose B changed meanwhile
   *  (another tab, or a later reconcile) re-arms rather than firing stale. */
  /** `shownSha`: the sha of the kept edit the clicked card shows (a focused
   *  card is not rebuilt by a read, so the model may already hold a newer B);
   *  the request names that one, and the server refuses (409) a B it replaced. */
  function discardSupersededPrompt(agentId, shownSha, intent) {
    if (getModel().saving) return;
    const row = agentRow(agentId);
    const outcome = requestDiscard(getModel(), row, shownSha, intent);
    if (outcome === 'noop') return;
    if (outcome === 'armed') {
      render();
      return;
    }
    // Confirmed: the row's B is the one the confirmation was bound to.
    const sha = shownToken(row, shownSha);
    // Discarding B changes no draft field: unsaved edits in the card stay.
    return mutateAgent(agentId, () => api.discardAgentSuperseded(agentId, sha),
      `${agentId}: the replaced edit is discarded; the prompt file runs.`, { fields: [], rereadOnError: true });
  }

  function keepSupersededPrompt(agentId) {
    if (cancelDiscard(getModel(), agentId)) render();
  }

  function rollbackSupersededPrompt(agentId, shownSha) {
    const sha = shownToken(agentRow(agentId), shownSha);
    // Rolling back sets the prompt only: an unsaved Required or model edit stays.
    return mutateAgent(agentId, () => api.restoreAgentSuperseded(agentId, sha),
      `${agentId} is back on your last edit. The next review attempt uses it, until the default changes again.`,
      { fields: ['prompt'], rereadOnError: true });
  }

  /** Text a changed default set aside (stashEditsOnDefaultChange): back into the editor, on the default now shown. */
  function putBackStashedPrompt(agentId) {
    const model = getModel();
    if (!putBackStash(model, agentId)) return;
    model.notice = null;
    render();
    // render() skips a focused editor: it must show the text put back, or the
    // next keystroke would carry the old text back into the draft.
    const row = agentRow(agentId);
    if (row && editingPrompt()) {
      showAgentPrompt(agentId, model.drafts[agentId].prompt);
      syncAgentEditor(agentId, agentEditorState(row, model.drafts[agentId], { saving: model.saving !== null, maxChars: model.maxChars }));
    }
  }

  function dropStashedPrompt(agentId) {
    const model = getModel();
    if (!dropStash(model, agentId)) return;
    model.notice = null;
    render();
  }

  return {
    loadAgentSettings, editAgentPrompt, editAgentRequired, editAgentModel, saveAgent, resetAgent,
    discardSupersededPrompt, keepSupersededPrompt, rollbackSupersededPrompt, putBackStashedPrompt, dropStashedPrompt,
  };
}
