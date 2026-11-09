/** Rendered buttons use the same request handlers as app.js. Only HTTP is fake. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { afterEach, beforeEach, describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';
import { draftFromAgent, projectAgentRow } from './agents.js';
import { emptyAgentsModel } from './agents-controller.js';
import { createAgentHandlers } from './agents-handlers.js';
import { createDomShim, findById } from './dom-shim.js';
import { renderAgents } from './view-agents.js';

const SHA_A = 'a'.repeat(64);
const SHA_B = 'b'.repeat(64);
const SHA_B2 = 'e'.repeat(64);
const SHA_C = 'c'.repeat(64);
const kept = { prompt: 'original B', sha: SHA_B, superseded_at: '2026-10-15T09:00:00Z' };
const initial = projectAgentRow({ id: 'arch', prompt: 'default C', default_sha: SHA_C, prompt_source: 'file', prompt_superseded: kept });
const settle = () => new Promise((resolve) => setImmediate(resolve));
let previousDocument;
beforeEach(() => {
  previousDocument = globalThis.document;
  globalThis.document = createDomShim().document;
});
afterEach(() => { globalThis.document = previousDocument; });

/** `focusedPrompt`: like app.js while the admin types, render() leaves the
 *  built card alone (a rebuild would cancel IME input), so only what a handler
 *  patches directly reaches the editor. */
function harness({ focusedPrompt = false } = {}) {
  const calls = [];
  const api = {};
  for (const method of ['agentSettings', 'putAgentSetting', 'deleteAgentSetting', 'discardAgentSuperseded', 'restoreAgentSuperseded']) {
    api[method] = (...args) => new Promise((resolve, reject) => { calls.push({ method, args, resolve, reject }); });
  }
  const model = emptyAgentsModel();
  model.loaded = true;
  model.rows = [initial];
  model.drafts.arch = draftFromAgent(initial);
  let tree;
  const renderedPrompts = [];
  const handlers = createAgentHandlers({
    getModel: () => model, api, render,
    editingPrompt: () => focusedPrompt,
    releasePromptFocus: (agentIds) => { if (agentIds.includes('arch')) focusedPrompt = false; },
    handleError: (error, assign) => assign(error.status ? { status: error.status, title: 'as errors.js words it', detail: error.message } : { title: error.message }),
  });
  function render() {
    renderedPrompts.push(model.rows[0].prompt);
    if (focusedPrompt && tree) return;
    tree = renderAgents({
      model,
      onPromptInput: handlers.editAgentPrompt,
      onRequired: handlers.editAgentRequired,
      onModel: handlers.editAgentModel,
      onSave: handlers.saveAgent,
      onReset: handlers.resetAgent,
      onDiscard: handlers.discardSupersededPrompt,
      onRollback: handlers.rollbackSupersededPrompt,
      onKeep: handlers.keepSupersededPrompt,
      onPutBack: handlers.putBackStashedPrompt,
      onDropStash: handlers.dropStashedPrompt,
      onReload: handlers.loadAgentSettings,
      onReloadModels: () => {},
    });
  }
  render();
  function node(suffix) { return findById(tree, `agent-arch-${suffix}`); }
  function click(suffix) {
    const button = node(suffix);
    assert.ok(button, `${suffix} exists in the rendered card`);
    assert.ok(!button.disabled && !button.hasAttribute('disabled'), `${suffix} is enabled`);
    button.dispatchEvent(new Event('click'));
    return button;
  }
  function type(prompt) {
    const input = node('prompt');
    input.value = prompt;
    input.dispatchEvent(new Event('input'));
  }
  return {
    model, calls, handlers, node, click, type, render, renderedPrompts, text: () => tree.textContent,
    focused: () => focusedPrompt, focus: () => { focusedPrompt = true; },
  };
}

describe('app agent handlers through rendered buttons', () => {
  it('Keep cancels the rendered confirmation without any API request or removal of B', () => {
    const h = harness();
    h.click('discard');
    assert.ok(h.node('discard-confirm'));
    assert.equal(h.calls.length, 0);
    h.click('keep');
    assert.equal(h.model.confirmDiscard, null);
    assert.ok(h.node('discard-confirm') === null);
    assert.equal(h.calls.length, 0);
    assert.ok(h.node('alert'), 'keeping B retains its marker');
    assert.equal(h.model.rows[0].prompt_superseded.prompt, 'original B');
  });

  for (const [action, method, prompt] of [
    ['confirm-discard', 'discardAgentSuperseded', 'default C'],
    ['rollback', 'restoreAgentSuperseded', 'original B'],
    ['save', 'putAgentSetting', 'edited C'],
  ]) {
    it(`${action} sends one mutation, applies the server row and clears the marker and confirmation`, async () => {
      const h = harness();
      if (action === 'save') h.type('edited C');
      h.click('discard');
      assert.ok(h.node('discard-confirm'));
      if (action === 'rollback') h.click('keep');
      const clicked = h.click(action);
      // Exercise the handler's guard even if a queued click reaches the old node.
      clicked.dispatchEvent(new Event('click'));
      assert.equal(h.calls.length, 1);
      assert.equal(h.calls[0].method, method);
      assert.deepEqual(h.calls[0].args, action === 'save' ? ['arch', { prompt: 'edited C', expected_default_sha: SHA_C }] : ['arch', SHA_B],
        'Save names the default it edited; Discard and Rollback name the B on screen by its sha');
      assert.equal(h.model.confirmDiscard, null);
      const saved = projectAgentRow({ id: 'arch', prompt, prompt_source: action === 'confirm-discard' ? 'file' : 'dashboard' });
      h.calls[0].resolve(saved);
      await settle();
      assert.deepEqual(h.model.rows, [saved]);
      assert.ok(h.node('alert') === null);
      assert.ok(h.node('discard-confirm') === null);
      assert.equal(h.model.saving, null);
      assert.equal(h.calls.length, 2, 'exactly one authoritative GET follows the mutation');
      assert.equal(h.calls[1].method, 'agentSettings');
      h.calls[1].resolve({ agents: [saved] });
      await settle();
      assert.equal(h.calls.length, 2);
      assert.deepEqual(h.model.rows, [saved]);
      assert.equal(h.model.drafts.arch.prompt, prompt);
      assert.equal(h.model.loading, false);
    });
  }

  for (const readFirst of [false, true]) {
    it(`${readFirst ? 'GET first' : 'mutation first'}: follows up authoritatively and keeps typing throughout`, async () => {
      const h = harness();
      h.type('submitted');
      h.click('save');
      h.type('unsaved typing');
      const read = h.handlers.loadAgentSettings({ keepDrafts: true });
      const saved = projectAgentRow({ id: 'arch', prompt: 'submitted', prompt_source: 'dashboard' });
      const newer = projectAgentRow({ id: 'arch', prompt: 'another tab edited later', prompt_source: 'dashboard', required: true });
      if (readFirst) {
        h.calls[1].resolve({ agents: [newer] });
        await read;
        assert.equal(h.model.rows[0].prompt, 'another tab edited later');
        h.calls[0].resolve(saved);
        await settle();
        assert.equal(h.model.rows[0].prompt, 'another tab edited later');
        assert.ok(!h.renderedPrompts.includes('submitted'), 'the late row was never even briefly rendered');
      } else {
        h.calls[0].resolve(saved);
        await settle();
        assert.equal(h.model.rows[0].prompt, 'submitted');
        assert.equal(h.calls.length, 2, 'wait for the in-flight GET to finish');
        h.calls[1].resolve({ agents: [initial] });
        await settle();
        assert.equal(h.model.rows[0].prompt, 'submitted', 'the stale read was rejected');
      }
      assert.equal(h.model.drafts.arch.prompt, 'unsaved typing');
      assert.equal(h.calls.length, 3);
      assert.equal(h.calls[2].method, 'agentSettings');
      h.type('typing during follow-up');
      h.calls[2].resolve({ agents: [newer] });
      await read;
      await settle();
      assert.deepEqual(h.model.rows, [newer]);
      assert.equal(h.model.drafts.arch.prompt, 'typing during follow-up');
      assert.equal(h.model.drafts.arch.required, true);
      assert.equal(h.node('prompt').value, 'typing during follow-up');
      assert.ok(h.node('alert') === null);
      assert.equal(h.model.confirmDiscard, null);
      assert.equal(h.calls.length, 3);
    });
  }

  for (const [action, method] of [
    ['confirm-discard', 'discardAgentSuperseded'],
    ['rollback', 'restoreAgentSuperseded'],
    ['confirm-reset', 'deleteAgentSetting'],
  ]) {
    it(`${action} refused (another B landed): shows the error, re-reads once, and the new B appears`, async () => {
      const h = harness();
      if (action === 'confirm-discard') h.click('discard');
      if (action === 'confirm-reset') h.click('reset');
      h.click(action);
      const call = h.calls.find((c) => c.method === method);
      assert.deepEqual(call.args, ['arch', SHA_B]);
      call.reject(new Error('B changed on the server'));
      await settle();
      const reads = h.calls.filter((c) => c.method === 'agentSettings');
      assert.equal(reads.length, 1, 'exactly one authoritative re-read follows the refusal');
      assert.equal(h.model.error.title, 'B changed on the server');
      const newer = projectAgentRow({ ...initial, prompt_superseded: { prompt: 'newer B2', sha: SHA_B2 } });
      reads[0].resolve({ agents: [newer] });
      await settle();
      assert.equal(h.model.rows[0].prompt_superseded.sha, SHA_B2, 'the B now on the server is the one on screen');
      assert.equal(h.model.error.title, 'B changed on the server', 'the error stays on screen through the re-read');
      assert.equal(h.model.saving, null);
      assert.equal(h.calls.length, 2);
    });
  }

  it('Save refused (409, the default changed under the edit) keeps the draft and shows the server\'s error', async () => {
    const h = harness();
    h.type('edited C');
    h.click('save');
    const call = h.calls.find((c) => c.method === 'putAgentSetting');
    assert.deepEqual(call.args, ['arch', { prompt: 'edited C', expected_default_sha: SHA_C }]);
    call.reject(Object.assign(new Error('the default prompt changed since this edit began'), { status: 409 }));
    await settle();
    assert.equal(h.model.drafts.arch.prompt, 'edited C');
    assert.equal(h.node('prompt').value, 'edited C');
    assert.equal(h.model.error.detail, 'the default prompt changed since this edit began');
    assert.equal(h.model.error.title, 'The agent changed on the server');
    assert.equal(h.model.rows[0], initial, 'nothing was saved');
    assert.equal(h.calls.length, 1, 'no automatic read: the editor keeps the text until the admin reloads');
  });

  it('Reset with a B asks inline first, then deletes everything naming that B', async () => {
    const h = harness();
    h.click('reset');
    assert.equal(h.calls.length, 0, 'the first click only asks');
    assert.match(h.node('reset-confirm').textContent, /including B/);
    assert.ok(h.node('reset') === null, 'the idle Reset button gives way to the question');
    assert.ok(h.node('discard-confirm') === null, 'the Discard B question is a different one');
    h.click('cancel-reset');
    assert.ok(h.node('reset-confirm') === null);
    assert.equal(h.calls.length, 0);
    h.click('reset');
    h.click('confirm-reset');
    assert.deepEqual(h.calls.map((c) => [c.method, ...c.args]), [['deleteAgentSetting', 'arch', SHA_B]]);
    const reset = projectAgentRow({ id: 'arch', prompt: 'default C', default_sha: SHA_C, prompt_source: 'file' });
    h.calls[0].resolve(reset);
    await settle();
    assert.deepEqual(h.model.rows, [reset]);
    assert.ok(h.node('alert') === null);
  });

  it('Reset with no B acts at once, as before', () => {
    const h = harness();
    const overridden = projectAgentRow({ id: 'arch', prompt: 'mine', default_sha: SHA_C, prompt_source: 'dashboard' });
    h.model.rows = [overridden];
    h.model.drafts.arch = draftFromAgent(overridden);
    h.render();
    h.click('reset');
    assert.deepEqual(h.calls.map((c) => [c.method, ...c.args]), [['deleteAgentSetting', 'arch', null]]);
    assert.ok(h.node('reset-confirm') === null);
  });

  it('Discard B keeps unsaved edits in the card: the request changed none of them', async () => {
    const h = harness();
    h.type('default C\nmy unsaved line');
    h.handlers.editAgentRequired('arch', true);
    h.click('discard');
    h.click('confirm-discard');
    const call = h.calls.find((c) => c.method === 'discardAgentSuperseded');
    assert.ok(call, 'the DELETE was issued');
    const saved = projectAgentRow({ id: 'arch', prompt: 'default C', default_sha: SHA_C, prompt_source: 'file', required: false, prompt_superseded: null });
    call.resolve(saved);
    await settle();
    assert.equal(h.model.drafts.arch.prompt, 'default C\nmy unsaved line');
    assert.equal(h.model.drafts.arch.required, true);
    h.calls.find((c) => c.method === 'agentSettings').resolve({ agents: [saved] });
    await settle();
    assert.deepEqual([h.model.drafts.arch.prompt, h.model.drafts.arch.required], ['default C\nmy unsaved line', true]);
  });

  it('Rollback to B takes B as the prompt and keeps an unsaved Required change', async () => {
    const h = harness();
    h.handlers.editAgentRequired('arch', true);
    h.click('rollback');
    const call = h.calls.find((c) => c.method === 'restoreAgentSuperseded');
    assert.ok(call, 'the POST was issued');
    const saved = projectAgentRow({ id: 'arch', prompt: 'original B', default_sha: SHA_C, prompt_source: 'dashboard', required: false, prompt_superseded: null });
    call.resolve(saved);
    await settle();
    assert.equal(h.model.drafts.arch.prompt, 'original B');
    assert.equal(h.model.drafts.arch.required, true);
    h.calls.find((c) => c.method === 'agentSettings').resolve({ agents: [saved] });
    await settle();
    assert.deepEqual([h.model.drafts.arch.prompt, h.model.drafts.arch.required], ['original B', true]);
  });

  for (const focusedPrompt of [false, true]) {
    it(`Discard shows a changed default and recovery controls even when its follow-up GET fails (focused: ${focusedPrompt})`, async () => {
      const h = harness();
      h.type('default C\nmy unsaved line');
      h.handlers.editAgentRequired('arch', true);
      h.click('discard');
      h.click('confirm-discard');
      if (focusedPrompt) h.focus();
      const saved = projectAgentRow({ ...initial, prompt: 'default D', default_sha: 'd'.repeat(64), prompt_superseded: null });
      h.calls.find((c) => c.method === 'discardAgentSuperseded').resolve(saved);
      await settle();
      h.calls.find((c) => c.method === 'agentSettings').reject(new Error('offline'));
      await settle();
      assert.equal(h.node('prompt').value, 'default D');
      assert.equal(h.model.drafts.arch.defaultSha, 'd'.repeat(64));
      assert.equal(h.model.drafts.arch.required, true, 'unrelated edits survive');
      assert.equal(h.node('stash').value, 'default C\nmy unsaved line');
      assert.ok(h.node('stash-drop'));
      assert.match(h.model.notice || '', /default prompt of arch changed/i);
      h.click('stash-restore');
      assert.equal(h.node('prompt').value, 'default C\nmy unsaved line');
      h.click('save');
      assert.deepEqual(h.calls.at(-1).args, ['arch', {
        prompt: 'default C\nmy unsaved line', expected_default_sha: 'd'.repeat(64), required: true,
      }]);
    });
  }

  /** The admin edits B (the default then was A); a deploy ships C, and the
   *  next read brings C with B kept aside. */
  /** The admin types on the card of edit B on default A; `focusedPrompt`: the
   *  editor keeps focus, so render() leaves that card alone from then on. */
  function editingOnA({ focusedPrompt = false } = {}) {
    const h = harness();
    const edited = projectAgentRow({ id: 'arch', prompt: 'original B', default_sha: SHA_A, prompt_source: 'dashboard', prompt_superseded: null });
    h.model.rows = [edited];
    h.model.drafts.arch = draftFromAgent(edited);
    h.render();  // the on-A card, with no kept edit, is what is mounted before focus
    if (focusedPrompt) h.focus();
    h.type('original B\nunsaved line');
    return h;
  }

  async function readInitial(h) {
    const read = h.handlers.loadAgentSettings({ keepDrafts: true });
    h.calls.find((c) => c.method === 'agentSettings').resolve({ agents: [initial] });
    await read;
    await settle();
  }

  it('a read that brings a new kept edit shows C in the editor, not a draft based on B', async () => {
    const h = editingOnA();
    await readInitial(h);
    assert.equal(h.model.drafts.arch.prompt, 'default C', 'the editor under "C - new default" holds C');
    assert.equal(h.model.drafts.arch.defaultSha, SHA_C, 'a Save from here edits C');
    assert.equal(h.node('prompt').value, 'default C');
    assert.equal(h.model.stash.arch, 'original B\nunsaved line', 'the unsaved text is set aside, not lost');
    assert.equal(h.node('stash').value, 'original B\nunsaved line');
    assert.ok(h.node('stash').hasAttribute('readonly'));
    assert.match(h.model.notice || '', /default prompt of arch changed/i);
  });

  it('a focused editor whose text a read sets aside is released and redrawn: C, the text and Put back/Drop show at once', async () => {
    const h = editingOnA({ focusedPrompt: true });
    const editor = h.node('prompt');
    assert.equal(editor.value, 'original B\nunsaved line');
    assert.ok(h.node('alert') === null, 'the mounted card is the on-A one');
    await readInitial(h);
    assert.equal(h.focused(), false, 'the next keystroke cannot land in the new default');
    assert.notEqual(h.node('prompt'), editor, 'the card was redrawn');
    assert.equal(h.node('prompt').value, 'default C');
    assert.equal(h.node('stash').value, 'original B\nunsaved line', 'the set-aside text is on screen');
    assert.ok(h.node('stash-restore'), 'Put back in editor is on screen');
    assert.ok(h.node('stash-drop'), 'Drop is on screen');
    assert.equal(h.node('save').disabled, true, 'C unchanged: nothing to save');
  });

  it('app.js hands the handlers a focus release that blurs only a set-aside agent\'s editor', () => {
    const app = readFileSync(fileURLToPath(new URL('./app.js', import.meta.url)), 'utf8');
    assert.match(app, /createAgentHandlers\(\{[^}]*\breleasePromptFocus\b[^}]*\}\)/, 'wired into the handlers');
    const release = app.slice(app.indexOf('function releasePromptFocus('), app.indexOf('/** Set when render() skipped'));
    assert.ok(release.includes("if (editingPrompt() && agentIds.some((agentId) => active.id === `agent-${agentId}-prompt`)) active.blur();"), release);
  });

  it('a focused, untouched editor also shows C: the next keystroke never edits a B nobody sees', async () => {
    const h = harness({ focusedPrompt: true });
    const saved = projectAgentRow({ id: 'arch', prompt: 'original B', default_sha: SHA_A, prompt_source: 'dashboard', prompt_superseded: null });
    h.model.rows = [saved];
    h.model.drafts.arch = draftFromAgent(saved);
    const editor = h.node('prompt');
    editor.value = 'original B';
    await readInitial(h);
    assert.equal(h.model.stash.arch, undefined, 'nothing unsaved to set aside');
    assert.equal(editor.value, 'default C', 'the focused textarea shows what the draft holds');
    h.type('default C\nnext edit');
    h.click('save');
    assert.deepEqual(h.calls.at(-1).args, ['arch', { prompt: 'default C\nnext edit', expected_default_sha: SHA_C }]);
  });

  it('Put back in editor applies the set-aside text on the new default, then Save names C', async () => {
    const h = editingOnA();
    await readInitial(h);
    h.click('stash-restore');
    assert.equal(h.model.drafts.arch.prompt, 'original B\nunsaved line');
    assert.equal(h.model.drafts.arch.defaultSha, SHA_C);
    assert.equal(h.model.stash.arch, undefined);
    assert.ok(h.node('stash') === null);
    assert.equal(h.node('prompt').value, 'original B\nunsaved line');
    h.click('save');
    assert.deepEqual(h.calls.at(-1).args, ['arch', { prompt: 'original B\nunsaved line', expected_default_sha: SHA_C }]);
  });

  it('Reset on a card that showed no kept edit never names one the model got meanwhile', async () => {
    const h = harness();
    const plain = projectAgentRow({ id: 'arch', prompt: 'saved B', default_sha: SHA_A, prompt_source: 'dashboard', prompt_superseded: null,
      updated_at: '2026-10-15T08:00:00Z', updated_by: 'owner@company.example' });
    h.model.rows = [plain];
    h.model.drafts.arch = draftFromAgent(plain);
    h.handlers.loadAgentSettings({ keepDrafts: true });
    h.calls.find((c) => c.method === 'agentSettings').resolve({ agents: [plain] });
    await settle();
    const reset = h.node('reset');
    assert.ok(reset, 'the card offers Reset (no kept edit shown)');
    // A reconcile moved B aside meanwhile; the card on screen is the one
    // rendered before (as a focused card is never rebuilt by a read).
    const read = h.handlers.loadAgentSettings({ keepDrafts: true });
    h.calls.filter((c) => c.method === 'agentSettings').at(-1).resolve({ agents: [initial] });
    await read;
    reset.dispatchEvent(new Event('click'));
    reset.dispatchEvent(new Event('click'));
    const sent = h.calls.filter((c) => c.method === 'deleteAgentSetting');
    assert.equal(sent.length, 1);
    assert.equal(sent[0].args[1], null, 'the server then refuses (409): a kept edit nobody saw is never deleted');
  });

  it('Discard B on a stale card does not cancel a Rollback still settling the editor', async () => {
    const h = harness();
    h.type('unsaved E');
    h.click('rollback');
    const read = h.handlers.loadAgentSettings({ keepDrafts: true });
    h.calls.find((c) => c.method === 'agentSettings').resolve({ agents: [initial] });  // before the rollback committed
    await read;
    const restored = projectAgentRow({ id: 'arch', prompt: 'original B', prompt_source: 'dashboard', prompt_superseded: null });
    h.calls.find((c) => c.method === 'restoreAgentSuperseded').resolve(restored);
    await settle();
    const followUp = h.calls.filter((c) => c.method === 'agentSettings').at(-1);
    // The card still shows B kept: Discard B, confirmed, changes no draft field and the server answers 404.
    h.click('discard');
    h.click('confirm-discard');
    const discard = h.calls.find((c) => c.method === 'discardAgentSuperseded');
    discard.reject(Object.assign(new Error('this agent has no replaced prompt to discard'), { status: 404 }));
    await settle();
    followUp.resolve({ agents: [restored] });
    await settle();
    assert.equal(h.model.drafts.arch.prompt, 'original B', 'the Rollback still settles the editor on B');
  });

  it('a second Rollback that fails does not cancel the first one still settling the editor', async () => {
    const h = harness();
    h.type('unsaved E');
    h.click('rollback');
    const read = h.handlers.loadAgentSettings({ keepDrafts: true });
    h.calls.find((c) => c.method === 'agentSettings').resolve({ agents: [initial] });  // before the rollback committed
    await read;
    const restored = projectAgentRow({ id: 'arch', prompt: 'original B', prompt_source: 'dashboard', prompt_superseded: null });
    h.calls.find((c) => c.method === 'restoreAgentSuperseded').resolve(restored);
    await settle();
    const followUp = h.calls.filter((c) => c.method === 'agentSettings').at(-1);
    // The card still shows B kept: Rollback again; B was already restored, so the server answers 404.
    h.click('rollback');
    h.calls.filter((c) => c.method === 'restoreAgentSuperseded').at(-1)
      .reject(Object.assign(new Error('this agent has no replaced prompt to restore'), { status: 404 }));
    await settle();
    followUp.resolve({ agents: [restored] });
    await settle();
    for (const call of h.calls.filter((c) => c.method === 'agentSettings').slice(2)) call.resolve({ agents: [restored] });
    await settle();
    assert.equal(h.model.drafts.arch.prompt, 'original B', 'the first Rollback still settles the editor on B');
  });

  it('a Save applied directly takes over a Rollback still waiting to settle, so a newer row is not mistaken for typing', async () => {
    const h = harness();
    h.type('unsaved E');
    h.click('rollback');
    const read = h.handlers.loadAgentSettings({ keepDrafts: true });
    h.calls.find((c) => c.method === 'agentSettings').resolve({ agents: [initial] });  // before the rollback committed
    await read;
    const restored = projectAgentRow({ id: 'arch', prompt: 'original B', prompt_source: 'dashboard', prompt_superseded: null });
    h.calls.find((c) => c.method === 'restoreAgentSuperseded').resolve(restored);
    await settle();
    const followUp = h.calls.filter((c) => c.method === 'agentSettings').at(-1);
    h.type('typed F');
    h.click('save');
    const savedF = projectAgentRow({ id: 'arch', prompt: 'typed F', prompt_source: 'dashboard', prompt_superseded: null });
    h.calls.find((c) => c.method === 'putAgentSetting').resolve(savedF);  // answers before the Rollback's follow-up read
    await settle();
    assert.equal(h.model.drafts.arch.prompt, 'typed F');
    followUp.resolve({ agents: [restored] });  // started before the Save committed: read again
    await settle();
    const newest = projectAgentRow({ ...savedF, prompt: 'another admin G' });
    h.calls.filter((c) => c.method === 'agentSettings').at(-1).resolve({ agents: [newest] });
    await settle();
    assert.equal(h.model.drafts.arch.prompt, 'another admin G', 'nothing was typed since the Save');
    assert.ok(h.node('save').disabled || h.node('save').hasAttribute('disabled'), 'nothing left to save');
  });

  it('a successful Discard B carries an earlier Save still settling Required, so a newer row is not mistaken for typing', async () => {
    const h = harness();
    h.handlers.editAgentRequired('arch', true);
    h.click('save');
    assert.deepEqual(h.calls[0].args, ['arch', { required: true }]);
    const read = h.handlers.loadAgentSettings({ keepDrafts: true });
    h.calls.find((c) => c.method === 'agentSettings').resolve({ agents: [initial] });  // before the save committed
    await read;
    const saved = projectAgentRow({ ...initial, required: true, required_source: 'dashboard' });
    h.calls.find((c) => c.method === 'putAgentSetting').resolve(saved);
    await settle();
    const followUp = h.calls.filter((c) => c.method === 'agentSettings').at(-1);
    // The card still shows B kept: Discard B while the Save's follow-up read is out.
    h.click('discard');
    h.click('confirm-discard');
    followUp.resolve({ agents: [saved] });  // settles the Save while Discard runs
    await settle();
    const discarded = projectAgentRow({ ...saved, prompt_superseded: null });
    h.calls.find((c) => c.method === 'discardAgentSuperseded').resolve(discarded);
    await settle();
    // Another admin turned Required off before the authoritative read.
    const newest = projectAgentRow({ ...discarded, required: false, required_source: 'config' });
    h.calls.filter((c) => c.method === 'agentSettings').at(-1).resolve({ agents: [newest] });
    await settle();
    assert.equal(h.model.drafts.arch.required, false, 'the newest row wins: Required was not typed since the Save');
    assert.ok(h.node('save').disabled || h.node('save').hasAttribute('disabled'), 'nothing left to save');
  });

  it('a prompt-only Save keeps an earlier Save still settling Required, and the Required edit made since', async () => {
    const h = harness();
    h.handlers.editAgentRequired('arch', true);
    h.click('save');
    const read = h.handlers.loadAgentSettings({ keepDrafts: true });
    h.calls.find((c) => c.method === 'agentSettings').resolve({ agents: [initial] });  // before the save committed
    await read;
    const saved = projectAgentRow({ ...initial, required: true, required_source: 'dashboard' });
    h.calls.find((c) => c.method === 'putAgentSetting').resolve(saved);
    await settle();
    const followUp = h.calls.filter((c) => c.method === 'agentSettings').at(-1);
    // Before the follow-up read lands, the admin turns Required off again and edits the prompt.
    h.handlers.editAgentRequired('arch', false);
    h.type('edited C');
    h.click('save');
    const second = h.calls.filter((c) => c.method === 'putAgentSetting').at(-1);
    assert.deepEqual(second.args, ['arch', { prompt: 'edited C', expected_default_sha: SHA_C }], 'Required is not sent: the row shows it off');
    followUp.resolve({ agents: [saved] });
    await settle();
    const savedE = projectAgentRow({ ...saved, prompt: 'edited C', prompt_source: 'dashboard' });
    second.resolve(savedE);
    await settle();
    h.calls.filter((c) => c.method === 'agentSettings').at(-1).resolve({ agents: [savedE] });
    await settle();
    assert.equal(h.model.drafts.arch.required, false, 'the Required edit made after the first Save survives');
    assert.equal(h.node('save').disabled || h.node('save').hasAttribute('disabled'), false, 'it is still to save');
  });

  it('Put back while the prompt file is unreadable keeps the default the draft knew, so Save still names it', async () => {
    const h = editingOnA();
    await readInitial(h);
    assert.equal(h.model.drafts.arch.defaultSha, SHA_C);
    const unreadable = projectAgentRow({ ...initial, default_sha: null });
    const read = h.handlers.loadAgentSettings({ keepDrafts: true });
    h.calls.filter((c) => c.method === 'agentSettings').at(-1).resolve({ agents: [unreadable] });
    await read;
    await settle();
    h.click('stash-restore');
    assert.equal(h.model.drafts.arch.defaultSha, SHA_C);
    h.click('save');
    assert.deepEqual(h.calls.at(-1).args, ['arch', { prompt: 'original B\nunsaved line', expected_default_sha: SHA_C }],
      'the server then answers 409 until the file is readable: the edit is never saved without a base');
  });

  it('the Discard B button only asks: confirming takes the confirmation button', () => {
    const h = harness({ focusedPrompt: true });  // a focused card is not redrawn: the question may not be on screen
    const discard = h.node('discard');
    discard.dispatchEvent(new Event('click'));
    discard.dispatchEvent(new Event('click'));
    assert.equal(h.calls.filter((c) => c.method === 'discardAgentSuperseded').length, 0, 'never deleted without the visible confirm');
  });

  it('Put back in editor shows the text in a focused editor too', async () => {
    const h = editingOnA({ focusedPrompt: true });
    await readInitial(h);
    assert.equal(h.node('stash').value, 'original B\nunsaved line');
    assert.ok(h.node('stash-drop'));
    h.focus();  // the admin clicks back into the editor, then puts the text back
    const editor = h.node('prompt');
    h.click('stash-restore');
    assert.equal(h.node('prompt'), editor, 'Put back preserves the focused textarea');
    assert.equal(h.model.drafts.arch.prompt, 'original B\nunsaved line');
    assert.equal(editor.value, 'original B\nunsaved line', 'the next keystroke must not carry C back');
  });

  it('Rollback sends the sha of the kept edit the card shows, not a newer one the model got meanwhile', async () => {
    const h = harness({ focusedPrompt: true });
    const rollback = h.node('rollback');
    // Another tab replaced B; a read lands while the editor is focused, so the card is not rebuilt.
    const newer = projectAgentRow({ ...initial, prompt_superseded: { prompt: 'newer B2', sha: SHA_B2, superseded_at: '2026-10-15T10:00:00Z' } });
    const read = h.handlers.loadAgentSettings({ keepDrafts: true });
    h.calls.find((c) => c.method === 'agentSettings').resolve({ agents: [newer] });
    await read;
    assert.equal(h.node('rollback'), rollback, 'the card on screen still compares the older B');
    rollback.dispatchEvent(new Event('click'));
    assert.deepEqual(h.calls.at(-1).args, ['arch', SHA_B], 'the server then refuses (409) instead of restoring an unseen B2');
  });

  it('Drop forgets the set-aside text and leaves C in the editor', async () => {
    const h = editingOnA();
    await readInitial(h);
    h.click('stash-drop');
    assert.equal(h.model.stash.arch, undefined);
    assert.ok(h.node('stash') === null);
    assert.equal(h.model.drafts.arch.prompt, 'default C');
    assert.equal(h.calls.length, 1, 'no request: the text was never saved');
  });

  it('a new default under an untouched prompt simply shows it: nothing to set aside', async () => {
    const h = harness();
    const onA = projectAgentRow({ id: 'arch', prompt: 'default A', default_sha: SHA_A, prompt_source: 'file' });
    h.model.rows = [onA];
    h.model.drafts.arch = draftFromAgent(onA);
    h.handlers.editAgentRequired('arch', true);
    await readInitial(h);
    assert.deepEqual([h.model.drafts.arch.prompt, h.model.drafts.arch.defaultSha, h.model.drafts.arch.required], ['default C', SHA_C, true]);
    assert.deepEqual(h.model.stash, {});
    assert.equal(h.model.notice, null);
  });

  it('Rollback to B shows B in a focused editor too', async () => {
    const h = harness({ focusedPrompt: true });
    const editor = h.node('prompt');
    h.click('rollback');
    const saved = projectAgentRow({ id: 'arch', prompt: 'original B', prompt_source: 'dashboard', prompt_superseded: null });
    h.calls.find((c) => c.method === 'restoreAgentSuperseded').resolve(saved);
    await settle();
    assert.equal(h.model.drafts.arch.prompt, 'original B');
    assert.equal(editor.value, 'original B', 'the focused textarea is not left on C');
  });

  it('a GET that fails while a Save is in flight does not hide the saved text', async () => {
    const h = harness();
    h.type('edited C');
    h.click('save');
    const read = h.handlers.loadAgentSettings({ keepDrafts: true });
    h.calls.find((c) => c.method === 'agentSettings').reject(new Error('offline'));
    await read;
    const saved = projectAgentRow({ id: 'arch', prompt: 'edited C', prompt_source: 'dashboard', prompt_superseded: null });
    h.calls.find((c) => c.method === 'putAgentSetting').resolve(saved);
    await settle();
    assert.deepEqual([h.model.rows[0].prompt, h.model.drafts.arch.prompt], ['edited C', 'edited C']);
  });

  it('a GET answered before the Save committed never puts the old text back in the editor', async () => {
    const h = harness();
    h.type('edited C');
    h.click('save');
    const read = h.handlers.loadAgentSettings({ keepDrafts: true });
    h.calls.find((c) => c.method === 'agentSettings').resolve({ agents: [initial] });  // the row before the PUT committed
    await read;
    const saved = projectAgentRow({ id: 'arch', prompt: 'edited C', prompt_source: 'dashboard', prompt_superseded: null });
    h.calls.find((c) => c.method === 'putAgentSetting').resolve(saved);
    await settle();
    assert.equal(h.model.drafts.arch.prompt, 'edited C', 'the submitted text stays until an authoritative read');
    h.calls.filter((c) => c.method === 'agentSettings').at(-1).resolve({ agents: [saved] });
    await settle();
    assert.deepEqual([h.model.rows[0].prompt, h.model.drafts.arch.prompt], ['edited C', 'edited C']);
  });

  it('Rollback with an overlapping read still settles the editor on B once the follow-up read lands', async () => {
    const h = harness();
    h.type('unsaved E');
    h.click('rollback');
    const read = h.handlers.loadAgentSettings({ keepDrafts: true });
    h.calls.find((c) => c.method === 'agentSettings').resolve({ agents: [initial] });  // the row before the rollback
    await read;
    h.type('unsaved E');  // nothing typed since dispatch: same text
    const restored = projectAgentRow({ id: 'arch', prompt: 'original B', prompt_source: 'dashboard', prompt_superseded: null });
    h.calls.find((c) => c.method === 'restoreAgentSuperseded').resolve(restored);
    await settle();
    h.calls.filter((c) => c.method === 'agentSettings').at(-1).resolve({ agents: [restored] });
    await settle();
    assert.equal(h.model.drafts.arch.prompt, 'original B', 'the prompt the rollback set, not the text it replaced');
    assert.equal(h.node('prompt').value, 'original B');
    assert.equal(h.node('save').disabled, true, 'nothing left to save');
  });

  it('a Save started before the Rollback\'s follow-up read lands is not undone by the older baseline', async () => {
    const h = harness();
    h.type('unsaved E');
    h.click('rollback');
    const read = h.handlers.loadAgentSettings({ keepDrafts: true });
    h.calls.find((c) => c.method === 'agentSettings').resolve({ agents: [initial] });  // before the rollback committed
    await read;
    const restored = projectAgentRow({ id: 'arch', prompt: 'original B', prompt_source: 'dashboard', prompt_superseded: null });
    h.calls.find((c) => c.method === 'restoreAgentSuperseded').resolve(restored);
    await settle();
    const rollbackFollowUp = h.calls.filter((c) => c.method === 'agentSettings').at(-1);
    h.click('save');  // E is still in the editor: the admin saves it
    rollbackFollowUp.resolve({ agents: [restored] });
    await settle();
    const savedE = projectAgentRow({ id: 'arch', prompt: 'unsaved E', prompt_source: 'dashboard', prompt_superseded: null });
    h.calls.find((c) => c.method === 'putAgentSetting').resolve(savedE);
    await settle();
    h.calls.filter((c) => c.method === 'agentSettings').at(-1).resolve({ agents: [savedE] });
    await settle();
    assert.deepEqual([h.model.rows[0].prompt, h.model.drafts.arch.prompt], ['unsaved E', 'unsaved E'], 'editor and server agree on E');
  });

  it('a read that changes the default during a Rollback is not mistaken for typing', async () => {
    const h = harness();
    const SHA_D = 'd'.repeat(64);
    h.type('unsaved E');
    h.click('rollback');
    // An overlapping read: the default changed to D (B still kept); E is set aside, the editor shows D.
    const onD = projectAgentRow({ id: 'arch', prompt: 'default D', default_sha: SHA_D, prompt_source: 'file', prompt_superseded: kept });
    const read = h.handlers.loadAgentSettings({ keepDrafts: true });
    h.calls.find((c) => c.method === 'agentSettings').resolve({ agents: [onD] });
    await read;
    assert.equal(h.model.stash.arch, 'unsaved E');
    const restored = projectAgentRow({ id: 'arch', prompt: 'original B', default_sha: SHA_D, prompt_source: 'dashboard', prompt_superseded: null });
    h.calls.find((c) => c.method === 'restoreAgentSuperseded').resolve(restored);
    await settle();
    h.calls.filter((c) => c.method === 'agentSettings').at(-1).resolve({ agents: [restored] });
    await settle();
    assert.equal(h.model.drafts.arch.prompt, 'original B', 'B runs: the editor shows B, not the D the read put there');
    assert.equal(h.node('save').disabled, true);
    assert.equal(h.model.stash.arch, 'unsaved E', 'the unsaved text stays set aside');
  });

  it('typing after the Rollback click, then a read that sets it aside for a new default, still ends on B', async () => {
    const h = harness();
    const SHA_D = 'd'.repeat(64);
    h.type('unsaved E');
    h.click('rollback');
    h.type('typed F');
    const onD = projectAgentRow({ id: 'arch', prompt: 'default D', default_sha: SHA_D, prompt_source: 'file', prompt_superseded: kept });
    const read = h.handlers.loadAgentSettings({ keepDrafts: true });
    h.calls.find((c) => c.method === 'agentSettings').resolve({ agents: [onD] });
    await read;
    assert.equal(h.model.stash.arch, 'typed F');
    const restored = projectAgentRow({ id: 'arch', prompt: 'original B', default_sha: SHA_D, prompt_source: 'dashboard', prompt_superseded: null });
    h.calls.find((c) => c.method === 'restoreAgentSuperseded').resolve(restored);
    await settle();
    h.calls.filter((c) => c.method === 'agentSettings').at(-1).resolve({ agents: [restored] });
    await settle();
    assert.equal(h.model.drafts.arch.prompt, 'original B', 'D was the read\'s text, not typing');
    assert.equal(h.node('save').disabled, true);
    assert.equal(h.model.stash.arch, 'typed F', 'the typing stays recoverable');
  });

  it('a read that already shows the committed Rollback, then another admin\'s save, ends on that save', async () => {
    const h = harness();
    h.click('rollback');
    const restored = projectAgentRow({ id: 'arch', prompt: 'original B', prompt_source: 'dashboard', prompt_superseded: null });
    const read = h.handlers.loadAgentSettings({ keepDrafts: true });
    h.calls.find((c) => c.method === 'agentSettings').resolve({ agents: [restored] });  // B committed, answer not here yet
    await read;
    h.calls.find((c) => c.method === 'restoreAgentSuperseded').resolve(restored);
    await settle();
    const savedE = projectAgentRow({ id: 'arch', prompt: 'other admin E', prompt_source: 'dashboard', prompt_superseded: null });
    h.calls.filter((c) => c.method === 'agentSettings').at(-1).resolve({ agents: [savedE] });
    await settle();
    assert.equal(h.model.drafts.arch.prompt, 'other admin E', 'nothing typed here: the newest server prompt, not a dirty B');
    assert.equal(h.node('save').disabled, true);
  });

  it('typing after the Rollback click survives the overlapping-read path', async () => {
    const h = harness();
    h.click('rollback');
    const read = h.handlers.loadAgentSettings({ keepDrafts: true });
    h.calls.find((c) => c.method === 'agentSettings').resolve({ agents: [initial] });
    await read;
    h.type('typed after the click');
    const restored = projectAgentRow({ id: 'arch', prompt: 'original B', prompt_source: 'dashboard', prompt_superseded: null });
    h.calls.find((c) => c.method === 'restoreAgentSuperseded').resolve(restored);
    await settle();
    h.calls.filter((c) => c.method === 'agentSettings').at(-1).resolve({ agents: [restored] });
    await settle();
    assert.equal(h.model.drafts.arch.prompt, 'typed after the click');
  });

  it('a failed authoritative GET leaves the newer row and draft intact and reports the error', async () => {
    const h = harness();
    h.type('submitted');
    h.click('save');
    h.type('keep this draft');
    const read = h.handlers.loadAgentSettings();
    const newer = projectAgentRow({ id: 'arch', prompt: 'newer row' });
    h.calls[1].resolve({ agents: [newer] });
    await read;
    h.calls[0].resolve(projectAgentRow({ id: 'arch', prompt: 'submitted' }));
    await settle();
    h.calls[2].reject(new Error('read failed'));
    await settle();
    assert.deepEqual(h.model.rows, [newer]);
    assert.equal(h.model.drafts.arch.prompt, 'keep this draft');
    assert.equal(h.model.error.title, 'read failed');
    assert.equal(h.model.loading, false);
    assert.equal(h.calls.length, 3);
  });

  it('a successful Save says so on the page; typing withdraws it, and the next render no longer shows it', async () => {
    const h = harness();
    h.type('edited C');
    h.click('save');
    const saved = projectAgentRow({ id: 'arch', prompt: 'edited C', default_sha: SHA_C, prompt_source: 'dashboard', prompt_superseded: null });
    h.calls[0].resolve(saved);
    await settle();
    h.calls[1].resolve({ agents: [saved] });
    await settle();
    const notice = 'Saved arch. The next review attempt uses it.';
    assert.equal(h.model.notice, notice, 'the authoritative read keeps it');
    assert.ok(h.text().includes(notice), 'the notice is on the page');
    h.type('edited C again');
    assert.equal(h.model.notice, null);
    h.render();
    assert.ok(!h.text().includes(notice), 'the next render no longer shows it');
  });

  it('Put back in editor withdraws the notice that the default changed', async () => {
    const h = editingOnA();
    await readInitial(h);
    assert.match(h.text(), /default prompt of arch changed/i);
    h.click('stash-restore');
    assert.equal(h.model.notice, null);
    assert.doesNotMatch(h.text(), /default prompt of arch changed/i, 'the text it announced is back in the editor');
  });

  for (const [ask, question] of [['discard', 'discard-confirm'], ['reset', 'reset-confirm']]) {
    it(`a read that brings another B withdraws the ${ask} question asked about the old one`, async () => {
      const h = harness();
      h.click(ask);
      assert.ok(h.node(question), 'the question is on screen');
      const newer = projectAgentRow({ ...initial, prompt_superseded: { prompt: 'newer B2', sha: SHA_B2 } });
      const read = h.handlers.loadAgentSettings({ keepDrafts: true });
      h.calls[0].resolve({ agents: [newer] });
      await read;
      assert.equal(h.model.confirmDiscard, null);
      assert.ok(h.node(question) === null, 'no question about a B no longer on screen');
      assert.ok(h.node(ask), `${ask} asks afresh, about B2`);
      assert.equal(h.calls.length, 1);
    });
  }

  it('a Rollback refused while a read is in flight keeps its error on screen through the follow-up read', async () => {
    const h = harness();
    const read = h.handlers.loadAgentSettings({ keepDrafts: true });
    h.click('rollback');
    h.calls.find((c) => c.method === 'restoreAgentSuperseded').reject(new Error('B changed on the server'));
    await settle();
    assert.equal(h.model.error && h.model.error.title, 'B changed on the server');
    assert.equal(h.calls.filter((c) => c.method === 'agentSettings').length, 1, 'the re-read waits for the read in flight');
    const newer = projectAgentRow({ ...initial, prompt_superseded: { prompt: 'newer B2', sha: SHA_B2 } });
    h.calls[0].resolve({ agents: [newer] });
    await settle();
    const reads = h.calls.filter((c) => c.method === 'agentSettings');
    assert.equal(reads.length, 2, 'then exactly one more read');
    assert.equal(h.model.error && h.model.error.title, 'B changed on the server', 'the follow-up read does not wipe it');
    reads[1].resolve({ agents: [newer] });
    await read;
    assert.equal(h.model.rows[0].prompt_superseded.sha, SHA_B2);
    assert.equal(h.model.error && h.model.error.title, 'B changed on the server', 'the refusal stays on screen');
    assert.match(h.text(), /B changed on the server/);
  });

  it('a Reset click that reaches the card while a Save is in flight neither asks nor resets', async () => {
    const h = harness();
    h.handlers.editAgentRequired('arch', true);
    const reset = h.node('reset');
    h.click('save');
    // A queued click reaches the Reset button of the card drawn before the Save.
    reset.dispatchEvent(new Event('click'));
    assert.equal(h.model.confirmDiscard, null, 'no Reset question is armed behind the Save');
    assert.deepEqual(h.calls.map((c) => [c.method, ...c.args]), [['putAgentSetting', 'arch', { required: true }]]);
    // A Save that leaves the prompt alone keeps B on the server.
    const saved = projectAgentRow({ ...initial, required: true, required_source: 'dashboard' });
    h.calls[0].resolve(saved);
    await settle();
    assert.ok(h.node('reset-confirm') === null, 'the card shows no question the admin was never asked');
    assert.ok(h.node('reset'), 'Reset is offered as before');
    assert.ok(h.node('alert'), 'B survives the Save answer');
    assert.deepEqual(h.calls.map((c) => c.method), ['putAgentSetting', 'agentSettings']);
    h.calls[1].resolve({ agents: [saved] });
    await settle();
    assert.ok(h.node('reset-confirm') === null, 'the authoritative read brings no question either');
    assert.ok(h.node('reset'), 'Reset is still offered');
    assert.ok(h.node('alert'), 'B is still kept on the card');
    assert.equal(h.calls.length, 2);
  });
});
