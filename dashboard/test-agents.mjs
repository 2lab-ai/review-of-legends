/** Reviewer agent settings: projection, drafts, validation, request bodies. */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  MAX_PROMPT_CHARS,
  NAV_SUPERSEDED_LABEL,
  RESET_CONFIRM,
  SUPERSEDED_HINT,
  SUPERSEDED_NOTE,
  agentDraftChanged,
  agentEditorState,
  agentMutationError,
  agentRequestBody,
  agentsNavBadge,
  compareLines,
  compareView,
  defaultChangedUnderEdit,
  describeAgentModel,
  describeRuns,
  draftAfterSave,
  draftFromAgent,
  hasOverride,
  hasStoredSetting,
  hasSupersededPrompt,
  projectAgentRow,
  projectAgentSettings,
  promptLength,
  refreshAgentDrafts,
  replaceAgentRow,
  resetControls,
  shownDraft,
  supersededControls,
  validateAgentDraft,
} from './agents.js';
import { CLAUDE_NOTE, MAX_FALLBACKS } from './models.js';
import { isSettingEvent } from './runs.js';

const ARCH = {
  id: 'server-architect',
  focus: 'Server architecture / DB / network',
  repos: ['acme-corp/Gamma'],
  required: true,
  required_source: 'config',
  config_required: true,
  prompt: 'Line one\n  indented line\n\nRead /Users/operator/x.md\n',
  prompt_source: 'file',
  model: null,
  model_source: 'default',
  updated_at: null,
  updated_by: null,
};

const POLICY = { model: 'gpt-6.1-sol[1m]', effort: 'max', fallback_models: ['grok-4.7[1m]'], source: 'dashboard' };

describe('projectAgentSettings', () => {
  it('keeps the prompt verbatim and drops malformed rows', () => {
    const projected = projectAgentSettings({
      agents: [ARCH, { id: '' }, 'junk', { id: 'tests', repos: ['a/b', 7], required: 'yes', prompt: 3 }],
      max_prompt_chars: 1000,
    });
    assert.deepEqual(projected.agents.map((row) => row.id), ['server-architect', 'tests']);
    assert.equal(projected.agents[0].prompt, ARCH.prompt);
    assert.equal(projected.max_prompt_chars, 1000, 'the server limit, not the client default');
    const tests = projected.agents[1];
    assert.deepEqual(tests.repos, ['a/b']);
    assert.equal(tests.required, false, 'only a real boolean true is required');
    assert.equal(tests.prompt, null);
    assert.equal(tests.prompt_source, 'file');
  });

  it('falls back to the default limit and reports a config error', () => {
    const projected = projectAgentSettings({ error: 'service config unavailable' });
    assert.deepEqual(projected.agents, []);
    assert.equal(projected.max_prompt_chars, MAX_PROMPT_CHARS);
    assert.equal(projected.error, 'service config unavailable');
    assert.equal(projectAgentRow(null), null);
  });

  it('describes where an agent runs and whether the dashboard overrides it', () => {
    const arch = projectAgentRow(ARCH);
    assert.equal(describeRuns(arch), 'Runs on: acme-corp/Gamma');
    assert.equal(describeRuns({ ...arch, repos: [] }), 'Runs on no repository (opt-in)');
    assert.equal(hasOverride(arch), false);
    assert.equal(hasOverride({ ...arch, required_source: 'dashboard' }), true);
    assert.equal(hasOverride({ ...arch, prompt_source: 'dashboard' }), true);
    assert.equal(hasOverride({ ...arch, model_source: 'dashboard' }), true);
  });

  it('projects default_sha, the sha of the prompt file now, or null when the server has none', () => {
    assert.equal(projectAgentRow({ ...ARCH, default_sha: 'c'.repeat(64) }).default_sha, 'c'.repeat(64));
    for (const sha of [undefined, null, 'not a sha', 'C'.repeat(64)]) {
      assert.equal(projectAgentRow({ ...ARCH, default_sha: sha }).default_sha, null);
    }
  });

  it('projects the model override and its source', () => {
    const arch = projectAgentRow(ARCH);
    assert.equal(arch.model, null);
    assert.equal(arch.model_source, 'default');
    const overridden = projectAgentRow({ ...ARCH, model: 'gpt-6-astra[1m]', model_source: 'dashboard' });
    assert.equal(overridden.model, 'gpt-6-astra[1m]');
    assert.equal(overridden.model_source, 'dashboard');
    const junk = projectAgentRow({ ...ARCH, model: 7, model_source: 'elsewhere' });
    assert.equal(junk.model, null);
    assert.equal(junk.model_source, 'default');
  });
});

describe('agent drafts', () => {
  const row = projectAgentRow(ARCH);

  it('starts unchanged and ignores whitespace-only edits at the ends', () => {
    const draft = draftFromAgent(row);
    assert.deepEqual(draft, { prompt: ARCH.prompt, required: true, model: '', defaultSha: null });
    assert.equal(agentDraftChanged(draft, row), false);
    assert.equal(agentDraftChanged({ ...draft, prompt: `  ${ARCH.prompt}  \n` }, row), false);
    assert.equal(agentDraftChanged({ ...draft, prompt: 'New prompt' }, row), true);
    assert.equal(agentDraftChanged({ ...draft, required: false }, row), true);
  });

  it('validates an edited prompt with the server limits, counting code points', () => {
    assert.deepEqual(validateAgentDraft({ prompt: '   ', required: true }, row).errors, ['The prompt cannot be empty.']);
    assert.match(validateAgentDraft({ prompt: 'x'.repeat(MAX_PROMPT_CHARS + 1), required: true }, row).errors[0], /48001 characters; the limit is 48000/);
    assert.equal(validateAgentDraft({ prompt: '가'.repeat(MAX_PROMPT_CHARS), required: true }, row).ok, true);
    assert.equal(promptLength('😀'), 1);
    assert.equal(validateAgentDraft({ prompt: '😀'.repeat(MAX_PROMPT_CHARS), required: true }, row).ok, true);
  });

  it('does not block a required toggle when the prompt file is unreadable', () => {
    const unreadable = projectAgentRow({ ...ARCH, prompt: null, error: 'prompt file is unreadable' });
    const draft = { ...draftFromAgent(unreadable), required: false };
    assert.equal(draft.prompt, '');
    assert.equal(validateAgentDraft(draft, unreadable).ok, true);
    assert.deepEqual(agentRequestBody(draft, unreadable), { required: false });
    assert.equal(agentEditorState(unreadable, draft).summary, 'Prompt — not available');
  });

  it('sends only what changed, and null when required returns to the config value', () => {
    const draft = draftFromAgent(row);
    assert.deepEqual(agentRequestBody(draft, row), {});
    assert.deepEqual(agentRequestBody({ ...draft, prompt: '  New prompt\n' }, row), { prompt: 'New prompt' });
    assert.deepEqual(agentRequestBody({ ...draft, required: false }, row), { required: false });
    const overridden = projectAgentRow({ ...ARCH, required: false, required_source: 'dashboard' });
    assert.deepEqual(agentRequestBody({ ...draftFromAgent(overridden), required: true }, overridden), { required: null });
  });

  it('summarises the editor state for the save button and the prompt header', () => {
    const draft = draftFromAgent(row);
    const idle = agentEditorState(row, draft);
    assert.equal(idle.canSave, false);
    assert.equal(idle.summary, `Prompt — ${promptLength(ARCH.prompt)} chars, from file`);
    const edited = agentEditorState(row, { ...draft, prompt: 'x'.repeat(1200) });
    assert.equal(edited.canSave, true);
    assert.equal(edited.summary, 'Prompt — 1,200 chars, edited');
    assert.equal(agentEditorState(row, { ...draft, prompt: 'x' }, { saving: true }).canSave, false);
    assert.deepEqual(agentEditorState(row, { ...draft, prompt: ' ' }).errors, ['The prompt cannot be empty.']);
  });

  it('shows a viewer an unreadable prompt as not available, not as 0 chars', () => {
    const unreadable = projectAgentRow({ ...ARCH, prompt: null, error: 'prompt file is unreadable' });
    for (const readOnly of [true, false]) {
      const shown = shownDraft(unreadable, readOnly ? undefined : draftFromAgent(unreadable), readOnly);
      assert.equal(agentEditorState(unreadable, shown).summary, 'Prompt — not available');
    }
    const draft = { prompt: 'admin draft', required: true };
    assert.deepEqual(shownDraft(row, draft, true), draftFromAgent(row), 'a viewer always sees the row');
    assert.equal(shownDraft(row, draft, false), draft);
  });

  it('keeps text typed while a save is pending, and the next save sends it', () => {
    const submitted = { prompt: 'Version B', required: true };
    const saved = projectAgentRow({ ...ARCH, prompt: 'Version B', prompt_source: 'dashboard' });
    // Typed during the request: the answer must not overwrite it.
    const typed = draftAfterSave({ prompt: 'Version B, then more', required: true }, submitted, saved);
    assert.deepEqual(typed, { prompt: 'Version B, then more', required: true, model: '', defaultSha: null });
    assert.equal(agentEditorState(saved, typed).canSave, true);
    assert.deepEqual(agentRequestBody(typed, saved), { prompt: 'Version B, then more' });
    // Nothing typed meanwhile: the server's (normalised) row becomes the draft.
    const untouched = draftAfterSave({ ...submitted }, submitted, projectAgentRow({ ...saved, prompt: 'Version B' }));
    assert.deepEqual(untouched, { prompt: 'Version B', required: true, model: '', defaultSha: null });
    assert.equal(agentEditorState(saved, untouched).canSave, false);
    // A reset answers with the file prompt; an edit made during it survives too.
    const reset = draftAfterSave({ prompt: 'typed during reset', required: true }, submitted, row);
    assert.equal(reset.prompt, 'typed during reset');
    assert.deepEqual(draftAfterSave(null, null, row), draftFromAgent(row));
  });

  it('rebases a refreshed draft per field, so another tab\'s required change is not reverted', () => {
    // Tab A edits the prompt while required is false; tab B switches required on.
    const before = projectAgentRow({ ...ARCH, required: false, config_required: false, prompt: 'P' });
    const drafts = { 'server-architect': { prompt: 'P edited in tab A', required: false } };
    const afterB = projectAgentRow({ ...ARCH, required: true, required_source: 'dashboard', config_required: false, prompt: 'P' });
    const next = refreshAgentDrafts([before], drafts, [afterB]);
    assert.deepEqual(next['server-architect'], { prompt: 'P edited in tab A', required: true, model: '', defaultSha: null });
    // A's Save sends only its own edit; B's override stays.
    assert.deepEqual(agentRequestBody(next['server-architect'], afterB), { prompt: 'P edited in tab A' });
    // A field A did change keeps A's value even when B changed it too.
    const both = refreshAgentDrafts([before], { 'server-architect': { prompt: 'P', required: true } },
      [projectAgentRow({ ...ARCH, required: false, config_required: false, prompt: 'P from tab B', prompt_source: 'dashboard' })]);
    assert.deepEqual(both['server-architect'], { prompt: 'P from tab B', required: true, model: '', defaultSha: null });
  });

  it('keeps a dirty draft across a refresh and resets clean ones', () => {
    const other = projectAgentRow({ ...ARCH, id: 'tests', required: false, config_required: false, prompt: 'tests prompt' });
    const drafts = { 'server-architect': { prompt: 'half typed', required: true }, tests: draftFromAgent(other) };
    const refreshed = [
      projectAgentRow({ ...ARCH, prompt: 'saved in another tab', prompt_source: 'dashboard' }),
      projectAgentRow({ ...ARCH, id: 'tests', required: true, required_source: 'dashboard', config_required: false, prompt: 'tests prompt' }),
    ];
    const next = refreshAgentDrafts([row, other], drafts, refreshed);
    assert.deepEqual(next['server-architect'], { prompt: 'half typed', required: true, model: '', defaultSha: null });
    assert.deepEqual(next.tests, { prompt: 'tests prompt', required: true, model: '', defaultSha: null });
    assert.deepEqual(replaceAgentRow([row, other], refreshed[1]).map((item) => item.required), [true, true]);
  });
});

describe('the default a prompt draft was started from (defaultSha)', () => {
  const SHA_A = 'a'.repeat(64);
  const SHA_C = 'c'.repeat(64);
  const onA = projectAgentRow({ ...ARCH, prompt: 'default A', default_sha: SHA_A });
  const onC = projectAgentRow({ ...ARCH, prompt: 'default C', default_sha: SHA_C });

  it('a draft takes default_sha from the row it is created from', () => {
    assert.equal(draftFromAgent(onA).defaultSha, SHA_A);
    assert.equal(draftFromAgent(projectAgentRow(ARCH)).defaultSha, null, 'no default_sha from the server: none');
  });

  it('a dirty prompt keeps the default it was started from; an untouched one takes the fresh row\'s', () => {
    const dirty = { ...draftFromAgent(onA), prompt: 'default A, edited' };
    assert.equal(refreshAgentDrafts([onA], { 'server-architect': dirty }, [onC])['server-architect'].defaultSha, SHA_A);
    const clean = draftFromAgent(onA);
    const rebased = refreshAgentDrafts([onA], { 'server-architect': clean }, [onC])['server-architect'];
    assert.deepEqual([rebased.prompt, rebased.defaultSha], ['default C', SHA_C]);
    // The same rule after a mutation answers: typed meanwhile keeps its base.
    const submitted = { ...draftFromAgent(onA), required: false };
    assert.equal(draftAfterSave({ ...submitted, prompt: 'typed meanwhile' }, submitted, onC).defaultSha, SHA_A);
    assert.equal(draftAfterSave({ ...submitted }, submitted, onC).defaultSha, SHA_C);
  });

  it('an edit that already is the fresh default takes its sha, so the next save is not a spurious 409', () => {
    const typedC = { ...draftFromAgent(onA), prompt: 'default C\n', required: false };
    const rebased = refreshAgentDrafts([onA], { 'server-architect': typedC }, [onC])['server-architect'];
    assert.equal(rebased.defaultSha, SHA_C);
    const body = agentRequestBody({ ...rebased, prompt: 'default C, edited' }, onC);
    assert.deepEqual([body.prompt, body.expected_default_sha], ['default C, edited', SHA_C]);
  });

  it('a refresh while the prompt file is unreadable keeps the default the draft knew', () => {
    const unreadable = projectAgentRow({ ...ARCH, prompt: 'default A' });  // default_sha null
    const rebased = refreshAgentDrafts([onA], { 'server-architect': draftFromAgent(onA) }, [unreadable])['server-architect'];
    assert.equal(rebased.defaultSha, SHA_A, 'a later save still names A, so a changed file is a 409, not a silent rebase');
  });

  it('the default changed under an edit only when the prompt is edited and the row now names another default', () => {
    const editedOnA = { ...draftFromAgent(onA), prompt: 'my edit' };
    assert.equal(defaultChangedUnderEdit(editedOnA, onC), true);
    assert.equal(defaultChangedUnderEdit(editedOnA, onA), false, 'same default: the edit stays');
    assert.equal(defaultChangedUnderEdit({ ...editedOnA, prompt: '  default C\n' }, onC), false, 'the edit already is C');
    // Checked after the read rebased the drafts: an untouched one already took C.
    const untouched = refreshAgentDrafts([onA], { 'server-architect': draftFromAgent(onA) }, [onC])['server-architect'];
    assert.equal(defaultChangedUnderEdit(untouched, onC), false, 'nothing typed: nothing to set aside');
    assert.equal(defaultChangedUnderEdit(editedOnA, projectAgentRow({ ...ARCH, prompt: 'x' })), false,
      'the server names no default (unreadable file): no new default to show');
    assert.equal(defaultChangedUnderEdit(null, onC), false);
  });

  it('a PUT that sends a prompt names the default it was edited from; nothing else does', () => {
    const draft = draftFromAgent(onA);
    assert.deepEqual(agentRequestBody({ ...draft, prompt: 'edited' }, onA), { prompt: 'edited', expected_default_sha: SHA_A });
    assert.deepEqual(agentRequestBody({ ...draft, required: false }, onA), { required: false }, 'no prompt, no base to check');
    const noBase = draftFromAgent(projectAgentRow(ARCH));
    assert.deepEqual(agentRequestBody({ ...noBase, prompt: 'edited' }, projectAgentRow(ARCH)), { prompt: 'edited' },
      'a draft with no known default sends none (the server then has nothing to compare)');
  });
});

describe('agent model override', () => {
  const row = projectAgentRow(ARCH);
  const astra = projectAgentRow({ ...ARCH, model: 'gpt-6-astra[1m]', model_source: 'dashboard' });

  it('drafts the model, with \'\' for the default, and counts a change', () => {
    assert.equal(draftFromAgent(row).model, '');
    assert.equal(draftFromAgent(astra).model, 'gpt-6-astra[1m]');
    assert.equal(agentDraftChanged({ ...draftFromAgent(row), model: 'gpt-6-astra[1m]' }, row), true);
    assert.equal(agentDraftChanged(draftFromAgent(astra), astra), false);
    assert.equal(agentDraftChanged({ ...draftFromAgent(astra), model: '' }, astra), true);
    assert.equal(agentEditorState(row, { ...draftFromAgent(row), model: 'gpt-6-astra[1m]' }).canSave, true);
  });

  it('sends the model when set and null when it returns to the default', () => {
    assert.deepEqual(agentRequestBody({ ...draftFromAgent(row), model: 'gpt-6-astra[1m]' }, row), { model: 'gpt-6-astra[1m]' });
    assert.deepEqual(agentRequestBody({ ...draftFromAgent(astra), model: '' }, astra), { model: null });
    assert.deepEqual(agentRequestBody({ ...draftFromAgent(astra), model: 'grok-4.7[1m]' }, astra), { model: 'grok-4.7[1m]' });
    assert.deepEqual(agentRequestBody(draftFromAgent(astra), astra), {}, 'an unchanged override is not re-sent');
    assert.deepEqual(agentRequestBody({ ...draftFromAgent(row), required: false, model: 'gpt-6-astra[1m]' }, row),
      { required: false, model: 'gpt-6-astra[1m]' });
  });

  it('accepts the default or a model id and refuses Claude', () => {
    assert.equal(validateAgentDraft({ ...draftFromAgent(astra), model: '' }, astra).ok, true);
    assert.equal(validateAgentDraft({ ...draftFromAgent(row), model: 'gpt-6-astra[1m]' }, row).ok, true);
    assert.deepEqual(validateAgentDraft({ ...draftFromAgent(row), model: 'not a model' }, row).errors, ['"not a model" is not a valid model id.']);
    assert.deepEqual(validateAgentDraft({ ...draftFromAgent(row), model: 'claude-opus-4' }, row).errors, [CLAUDE_NOTE]);
    assert.equal(agentEditorState(row, { ...draftFromAgent(row), model: 'claude-opus-4' }).canSave, false);
  });

  it('rebases the model per field after a save or a refresh', () => {
    // Model picked while a prompt save was in flight: the pick survives the answer.
    const submitted = { prompt: 'Version B', required: true, model: '' };
    const saved = projectAgentRow({ ...ARCH, prompt: 'Version B', prompt_source: 'dashboard' });
    const picked = draftAfterSave({ ...submitted, model: 'gpt-6-astra[1m]' }, submitted, saved);
    assert.equal(picked.model, 'gpt-6-astra[1m]');
    assert.deepEqual(agentRequestBody(picked, saved), { model: 'gpt-6-astra[1m]' });
    // Another tab set the model; this tab only edited the prompt.
    const next = refreshAgentDrafts([row], { 'server-architect': { ...draftFromAgent(row), prompt: 'edited here' } }, [astra]);
    assert.deepEqual(next['server-architect'], { prompt: 'edited here', required: true, model: 'gpt-6-astra[1m]', defaultSha: null });
    assert.deepEqual(agentRequestBody(next['server-architect'], astra), { prompt: 'edited here' });
    // A reset answers with no override: an untouched model goes back to the default.
    assert.equal(draftAfterSave(draftFromAgent(astra), draftFromAgent(astra), row).model, '');
  });

  it('mirrors service.agent_model_ladder: the policy follows once, never cut', () => {
    assert.equal(describeAgentModel(POLICY, 'grok-4.7[1m]'), 'Runs on grok-4.7[1m], then gpt-6.1-sol[1m] if it fails');
    assert.equal(describeAgentModel(POLICY, 'gpt-6.1-sol[1m]'), 'Runs on gpt-6.1-sol[1m], then grok-4.7[1m] if it fails');
    const full = { model: 'm0', effort: '', fallback_models: Array.from({ length: MAX_FALLBACKS }, (_v, i) => `m${i + 1}`), source: 'config' };
    const rungs = Array.from({ length: MAX_FALLBACKS + 1 }, (_v, i) => `m${i}`);
    assert.equal(describeAgentModel(full, 'astra'), `Runs on astra, then ${rungs.join(' → ')} if it fails`);
    assert.deepEqual(POLICY.fallback_models, ['grok-4.7[1m]'], 'the policy is never changed');
  });

  it('says what runs', () => {
    assert.equal(describeAgentModel(POLICY, ''), 'Runs on the default ladder');
    assert.equal(describeAgentModel(POLICY, 'gpt-6-astra[1m]'), 'Runs on gpt-6-astra[1m], then gpt-6.1-sol[1m] → grok-4.7[1m] if it fails');
    assert.equal(describeAgentModel(null, 'gpt-6-astra[1m]'), 'Runs on gpt-6-astra[1m], then the default ladder if it fails');
    assert.equal(describeAgentModel({ model: 'x', effort: '', fallback_models: [], source: 'config' }, 'x'), 'Runs on x, with no fallback');
    assert.equal(describeAgentModel({ model: 'grok-4.7[1m]', effort: '', fallback_models: [], source: 'request' }, 'gpt-6-astra[1m]'),
      "Runs on grok-4.7[1m]: the retry's pinned model beats this agent's model");
  });
});

describe('a replaced prompt (the default changed)', () => {
  const KEPT = {
    prompt: 'Line one\nmy edit\n',
    sha: 'b'.repeat(64),
    saved_at: '2026-10-14T00:00:00+00:00',
    saved_by: 'owner@company.example',
    superseded_at: '2026-10-15T09:00:00+00:00',
  };
  const replaced = projectAgentRow({ ...ARCH, prompt: 'Line one\nnew default\n', prompt_superseded: KEPT });
  const plain = projectAgentRow(ARCH);

  it('projects the kept edit verbatim and shows the marker only then', () => {
    assert.deepEqual(replaced.prompt_superseded, KEPT);
    assert.equal(plain.prompt_superseded, null);
    assert.equal(projectAgentRow({ ...ARCH, prompt_superseded: { prompt: 7 } }).prompt_superseded, null, 'no text, nothing to compare');
    assert.equal(projectAgentRow({ ...ARCH, prompt_superseded: { prompt: 'B', saved_by: 9 } }).prompt_superseded.saved_by, null);
    for (const sha of [undefined, 7, 'B'.repeat(64), 'b'.repeat(63), ` ${'b'.repeat(64)}`]) {
      assert.equal(projectAgentRow({ ...ARCH, prompt_superseded: { ...KEPT, sha } }).prompt_superseded.sha, null,
        `only a sha256 hex digest identifies B (${String(sha)})`);
    }
    assert.equal(hasSupersededPrompt(replaced), true);
    assert.equal(hasSupersededPrompt(plain), false);
    assert.equal(hasSupersededPrompt(null), false);
    assert.equal(hasOverride(replaced), false, 'the file runs: there is no override to reset');
    assert.equal(replaced.prompt_source, 'file');
  });

  it('"Reset to defaults" still has something to remove: only a replaced edit is stored', () => {
    assert.equal(hasStoredSetting(replaced), true, 'the kept edit B is a stored document even with no override');
    assert.equal(hasStoredSetting(plain), false);
    const overridden = projectAgentRow({ ...ARCH, prompt_source: 'dashboard' });
    assert.equal(hasStoredSetting(overridden), true);
    assert.equal(hasStoredSetting(null), false);
  });

  it('the Agents nav entry gets [!] with an accessible label while any row is replaced, else the custom count', () => {
    assert.deepEqual(agentsNavBadge([replaced, plain]), { text: '[!]', label: NAV_SUPERSEDED_LABEL });
    assert.deepEqual(agentsNavBadge([plain, projectAgentRow({ ...ARCH, prompt_source: 'dashboard' })]), { text: 'custom', label: null });
    assert.deepEqual(agentsNavBadge([plain]), { text: '', label: null });
    assert.deepEqual(agentsNavBadge([]), { text: '', label: null });
    assert.deepEqual(agentsNavBadge(null), { text: '', label: null });
  });

  it('builds the compare view: C left, B right', () => {
    const view = compareView(replaced, Date.parse('2026-10-15T10:00:00+00:00'));
    assert.equal(view.left.title, 'C — new default (in use)');
    assert.equal(view.left.text, 'Line one\nnew default\n');
    assert.equal(view.right.title, 'B (line view)');
    assert.equal(view.right.text, KEPT.prompt);
    assert.equal(view.right.saved_by, 'owner@company.example');
    assert.equal(view.right.saved_at, KEPT.saved_at);
    assert.match(view.right.detail, /^Saved by owner@company\.example, /);
    assert.equal(view.lines, undefined, 'the line view is not part of compareView: it is built lazily (compareLines)');
    // The diff is against the default in use, never the admin's draft: typing
    // does not re-render the compare (the textarea keeps focus and IME state).
    assert.equal(compareView(plain, Date.now()), null);
    const unreadable = compareView(projectAgentRow({ ...ARCH, prompt: null, prompt_superseded: { ...KEPT, saved_by: null, superseded_at: null } }), Date.now());
    assert.equal(unreadable.left.text, '');
    assert.equal(unreadable.right.title, 'B (line view)');
    assert.match(unreadable.right.detail, /^Saved \d/, 'no saved_by: who is left out, when stays');
  });

  it('compareLines: B normalized and in order, with the lines not in C marked; no C-only line ever appears', () => {
    const lines = compareLines(KEPT.prompt, 'Line one\nnew default\n');
    assert.deepEqual(lines, [
      { kind: 'equal', text: 'Line one' },
      { kind: 'delete', text: 'my edit' },
    ]);
    // The line view normalizes whitespace; Copy B must use the stored string instead.
    assert.equal(lines.map((line) => line.text).join('\n'), KEPT.prompt.trim());
    assert.ok(lines.every((line) => line.kind !== 'insert'), 'a C-only line is never shown on the right');
    const unreadable = compareLines(KEPT.prompt, '');
    assert.deepEqual(unreadable.map((line) => line.kind), ['delete', 'delete']);
    assert.deepEqual(compareLines('same\n', 'same\n'), [{ kind: 'equal', text: 'same' }]);
  });

  it('compareLines marks a line of B "not in C" only when C truly lacks it', () => {
    // A moved line is in C: the compare marks nothing.
    assert.deepEqual(compareLines('x\na\nb', 'a\nb\nx'), ['x', 'a', 'b'].map((text) => ({ kind: 'equal', text })));
    // B has X twice, C once: exactly one X is marked, and B keeps its order.
    const repeated = compareLines('X\nX\nY', 'Y\nX');
    assert.deepEqual(repeated.map((line) => line.text), ['X', 'X', 'Y']);
    assert.deepEqual(repeated.filter((line) => line.kind === 'delete').map((line) => line.text), ['X']);
  });

  it('compareLines on a long prompt: a line still in C is not marked, every line only in B is', () => {
    const side = 5000;
    const b = ['same', 'moved', ...Array.from({ length: side }, (_v, i) => `b${i}`), 'tail'];
    const c = ['same', ...Array.from({ length: side }, (_v, i) => `a${i}`), 'moved', 'tail'];
    const lines = compareLines(b.join('\n'), c.join('\n'));
    assert.deepEqual(lines.map((line) => line.text), b);
    assert.deepEqual(lines.filter((line) => line.text === 'moved').map((line) => line.kind), ['equal']);
    assert.equal(lines.filter((line) => line.kind === 'delete').length, side);
  });

  it('offers discard and rollback to the admin, with a hint that Discard B is how to keep C; confirms inline; nothing to a viewer', () => {
    const idle = supersededControls(replaced, { readOnly: false, busy: false, confirming: false });
    assert.equal(idle.marker.label, SUPERSEDED_NOTE);
    assert.deepEqual(idle.buttons.map((button) => [button.id, button.label, button.disabled]),
      [['discard', 'Discard B', false], ['rollback', 'Rollback to B', false]]);
    assert.equal(idle.confirm, null);
    assert.equal(idle.hint, SUPERSEDED_HINT);
    const confirming = supersededControls(replaced, { readOnly: false, busy: false, confirming: true });
    assert.deepEqual(confirming.buttons.map((button) => button.id), ['confirm-discard', 'keep']);
    assert.match(confirming.confirm, /cannot be recovered/);
    assert.equal(confirming.hint, null, 'the confirm message already says what discarding does');
    const busy = supersededControls(replaced, { readOnly: false, busy: true, confirming: true });
    assert.ok(busy.buttons.every((button) => button.disabled), 'a request in flight disables every action');
    const viewer = supersededControls(replaced, { readOnly: true, busy: false, confirming: true });
    assert.deepEqual(viewer.buttons, []);
    assert.equal(viewer.confirm, null);
    assert.equal(viewer.hint, null, 'a viewer has no buttons to hint at');
    assert.equal(viewer.marker.label, SUPERSEDED_NOTE, 'a viewer still sees the marker and the compare');
    assert.equal(supersededControls(plain, { readOnly: false, busy: false, confirming: false }), null);
  });

  it('Reset to defaults asks first, inline, when it would also delete B; otherwise it acts at once', () => {
    assert.match(RESET_CONFIRM, /all .*dashboard settings/i);
    assert.match(RESET_CONFIRM, /including B/);
    const idle = resetControls(replaced, { busy: false, confirming: false });
    assert.deepEqual(idle, { confirm: null, buttons: [{ id: 'reset', label: 'Reset to defaults', disabled: false, primary: false }] });
    const asking = resetControls(replaced, { busy: false, confirming: true });
    assert.equal(asking.confirm, RESET_CONFIRM);
    assert.deepEqual(asking.buttons.map((button) => [button.id, button.label, button.primary]),
      [['confirm-reset', 'Confirm reset', true], ['cancel-reset', 'Cancel', false]]);
    assert.ok(resetControls(replaced, { busy: true, confirming: true }).buttons.every((button) => button.disabled));
    const overridden = projectAgentRow({ ...ARCH, prompt_source: 'dashboard' });
    assert.deepEqual(resetControls(overridden, { busy: false, confirming: true }).buttons.map((button) => button.id), ['reset'],
      'no B: nothing extra to lose, no question');
    assert.equal(resetControls(plain, { busy: false, confirming: false }), null, 'nothing stored: nothing to reset');
  });

  it('(amended spec) Save requires a change from C; saving C unchanged is not a third way to accept it', () => {
    const draft = draftFromAgent(replaced);
    assert.equal(draft.prompt, 'Line one\nnew default\n');
    // C unchanged: Save is disabled. Accepting C as it is is Discard B's job.
    assert.equal(agentEditorState(replaced, draft).canSave, false);
    assert.equal(agentEditorState(replaced, { ...draft, prompt: 'Line one\nnew default, tuned\n' }).canSave, true);
    assert.deepEqual(agentRequestBody({ ...draft, prompt: KEPT.prompt }, replaced), { prompt: KEPT.prompt.trim() });
    const restored = projectAgentRow({ ...ARCH, prompt: KEPT.prompt.trim(), prompt_source: 'dashboard', prompt_superseded: null });
    assert.deepEqual(draftAfterSave(draft, draft, restored), draftFromAgent(restored));
    assert.equal(hasSupersededPrompt(restored), false, 'the card returns to its normal state');
  });

  it('words a refused (409) agent request as a change on the server, keeping the server\'s message', () => {
    const described = { status: 409, title: 'The pull request moved', detail: 'the kept edit changed', recovery: 'retry text' };
    const shown = agentMutationError(described);
    assert.equal(shown.detail, 'the kept edit changed', 'the server says what changed');
    assert.doesNotMatch(`${shown.title} ${shown.recovery}`, /pull request|retry/i);
    assert.match(shown.recovery, /nothing was changed/i);
    const other = { status: 400, title: 'Request rejected', detail: 'x' };
    assert.equal(agentMutationError(other), other, 'any other error is shown as errors.js words it');
  });

  it('re-reads the agents when the daemon replaces a prompt', () => {
    assert.equal(isSettingEvent({ kind: 'agent_prompt_superseded' }), true);
  });
});
