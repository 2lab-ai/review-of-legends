/** The Agents card, actually rendered and clicked (dom-shim.js), not source-pinned.
 *
 * renderAgents() is the real view-agents.js entry point app.js calls; the
 * glue in these tests (what happens on a click) is the same calls app.js
 * makes (requestDiscard, cancelDiscard, beginMutation/applyMutationResult/
 * finishMutation), kept local instead of importing app.js: app.js touches
 * `document` at module scope (`const dom = {...}`) before a test gets to
 * install the shim.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { DISCARD_CONFIRM, projectAgentRow } from './agents.js';
import { cancelDiscard, requestDiscard } from './agents-controller.js';
import { createDomShim, findAll, findById } from './dom-shim.js';
import { renderAgents } from './view-agents.js';

const NOW = Date.parse('2026-10-15T10:00:00+00:00');

const SHA_B = 'b'.repeat(64);
const KEPT = {
  prompt: 'Line one\nmy edit\n',
  sha: SHA_B,
  saved_at: '2026-10-14T00:00:00+00:00',
  saved_by: 'owner@company.example',
  superseded_at: '2026-10-15T09:00:00+00:00',
};

const REPLACED_ROW = projectAgentRow({
  id: 'arch',
  focus: 'Server architecture',
  repos: ['acme-corp/Gamma'],
  required: true,
  required_source: 'config',
  config_required: true,
  prompt: 'Line one\nnew default\n',
  prompt_source: 'file',
  prompt_superseded: KEPT,
});

function baseModel(rows) {
  return {
    rows, drafts: {}, maxChars: 48000, configError: null,
    loading: false, saving: null, confirmDiscard: null, error: null, notice: null, loaded: true,
  };
}

const noop = () => {};

function render(model, { readOnly = false } = {}) {
  return renderAgents({
    model, catalog: null, policy: null, readOnly, now: NOW,
    onPromptInput: noop, onRequired: noop, onModel: noop, onSave: noop, onReset: noop,
    onDiscard: noop, onRollback: noop, onKeep: noop, onReload: noop, onReloadModels: noop,
  });
}

function withShim(fn) {
  const previous = globalThis.document;
  globalThis.document = createDomShim().document;
  try {
    return fn();
  } finally {
    globalThis.document = previous;
  }
}

function button(tree, id) {
  return findAll(tree, 'button').find((node) => node.id === id) || null;
}

describe('the Agents card, rendered through view-agents.js', () => {
  it('shows the [!] button, and clicking it opens the prompt disclosure', () => withShim(() => {
    const tree = render(baseModel([REPLACED_ROW]));
    const alert = button(tree, 'agent-arch-alert');
    assert.ok(alert, 'the [!] marker is a real button in the tree');
    assert.equal(alert.textContent, '[!]');
    const details = findById(tree, 'agent-prompt-arch');
    assert.ok(details, 'a stable, findable <details> wraps the compare');
    assert.equal(details.open, false, 'closed until clicked');
    alert.dispatchEvent(new Event('click'));
    assert.equal(details.open, true, 'the marker click opens it (openPrompt)');
  }));

  it('builds no diff rows while closed; opening it (even via a second, idempotent toggle) fills them once', () => withShim(() => {
    const tree = render(baseModel([REPLACED_ROW]));
    assert.equal(findAll(tree, 'li').length, 0, 'the diff is not built at page render');
    const diffList = findAll(tree, 'ol').find((node) => node.className === 'diff');
    assert.ok(diffList, 'the empty <ol class="diff"> is still there, ready to be filled');
    const details = findById(tree, 'agent-prompt-arch');
    details.open = true; // openPrompt's assignment; dispatches `toggle` in the shim too
    const firstBuild = findAll(tree, 'li').length;
    assert.ok(firstBuild > 0, 'opening it builds the rows');
    details.open = false;
    details.open = true; // a second open: must not duplicate the rows
    assert.equal(findAll(tree, 'li').length, firstBuild, 'idempotent: filled once');
  }));

  it('gives a viewer the marker and the compare, but no action buttons', () => withShim(() => {
    const tree = render(baseModel([REPLACED_ROW]), { readOnly: true });
    assert.ok(button(tree, 'agent-arch-alert'), 'a viewer still sees the marker');
    for (const id of ['agent-arch-discard', 'agent-arch-rollback', 'agent-arch-save', 'agent-arch-reset']) {
      assert.ok(button(tree, id) === null, `a viewer gets no ${id} button`);
    }
  }));

  it('gives an admin Discard B, Rollback to B and Save', () => withShim(() => {
    const tree = render(baseModel([REPLACED_ROW]), { readOnly: false });
    assert.ok(button(tree, 'agent-arch-discard'));
    assert.ok(button(tree, 'agent-arch-rollback'));
    assert.ok(button(tree, 'agent-arch-save'));
  }));

  it('renders the confirm step inline after Discard B is clicked once, bound to the B on screen', () => withShim(() => {
    const model = baseModel([REPLACED_ROW]);
    // The same glue app.js runs on a Discard-B click: arm, then render again.
    const onDiscard = (agentId) => {
      const outcome = requestDiscard(model, model.rows.find((row) => row.id === agentId));
      assert.equal(outcome, 'armed', 'the first click on a fresh B arms it, never confirms at once');
    };
    let tree = renderAgents({
      model, catalog: null, policy: null, readOnly: false, now: NOW,
      onPromptInput: noop, onRequired: noop, onModel: noop, onSave: noop, onReset: noop,
      onDiscard, onRollback: noop, onKeep: noop, onReload: noop, onReloadModels: noop,
    });
    button(tree, 'agent-arch-discard').dispatchEvent(new Event('click'));
    assert.deepEqual(model.confirmDiscard, { agentId: 'arch', action: 'discard', token: SHA_B });
    tree = render(model); // app.js re-renders after any state change
    const confirmText = findById(tree, 'agent-arch-discard-confirm');
    assert.ok(confirmText, 'the confirmation renders inline in the card');
    assert.equal(confirmText.textContent, DISCARD_CONFIRM);
    assert.ok(button(tree, 'agent-arch-confirm-discard'));
    assert.ok(button(tree, 'agent-arch-keep'));
    assert.ok(button(tree, 'agent-arch-discard') === null, 'the idle Discard B button is gone while confirming');
    // Keep B cancels it.
    assert.equal(cancelDiscard(model, 'arch'), true);
    assert.equal(model.confirmDiscard, null);
  }));

  it('shows set-aside text read-only with Put back and Drop to the admin, and nothing to a viewer', () => withShim(() => {
    const model = { ...baseModel([REPLACED_ROW]), stash: { arch: 'my unsaved text' } };
    const tree = render(model);
    const stash = findById(tree, 'agent-arch-stash');
    assert.equal(stash.value, 'my unsaved text');
    assert.ok(stash.hasAttribute('readonly'));
    assert.notEqual(stash.className.split(' ').includes('agent__textarea'), true,
      'not a prompt editor: focusing it must not hold back page renders');
    assert.ok(button(tree, 'agent-arch-stash-restore'));
    assert.ok(button(tree, 'agent-arch-stash-drop'));
    const viewer = render(model, { readOnly: true });
    assert.ok(findById(viewer, 'agent-arch-stash') === null);
    assert.ok(findById(render(baseModel([REPLACED_ROW])), 'agent-arch-stash') === null, 'no stash, no block');
  }));

  it('an armed confirmation shows only in the card it was armed in', () => withShim(() => {
    const other = projectAgentRow({ ...REPLACED_ROW, id: 'tests' });
    const model = baseModel([REPLACED_ROW, other]);
    assert.equal(requestDiscard(model, REPLACED_ROW), 'armed');
    const tree = render(model);
    assert.ok(findById(tree, 'agent-arch-discard-confirm'));
    assert.ok(findById(tree, 'agent-tests-discard-confirm') === null);
    assert.ok(button(tree, 'agent-tests-discard'), 'the other card still offers its own Discard B');
  }));

  it('never renders B or C as HTML: a <script> in either lands as a text node, never a parsed element', () => withShim(() => {
    const hostile = projectAgentRow({
      id: 'arch',
      prompt: 'C default <script>window.c = 1</script>\n',
      prompt_source: 'file',
      prompt_superseded: { ...KEPT, prompt: 'B edit <script>window.b = 1</script>' },
    });
    const tree = render(baseModel([hostile]));
    assert.equal(findAll(tree, 'script').length, 0, 'no <script> element exists anywhere in the tree');
    const textarea = findAll(tree, 'textarea')[0];
    assert.equal(textarea.value, 'C default <script>window.c = 1</script>\n', 'C lands verbatim as a value, never parsed');
    const details = findById(tree, 'agent-prompt-arch');
    details.open = true;
    const lineTexts = findAll(tree, 'li').map((line) => line.childNodes.map((child) => child.textContent).join(''));
    assert.ok(lineTexts.some((line) => line.includes('B edit <script>window.b = 1</script>')),
      'B\'s line, including the literal "<script>" text, is a plain text node');
  }));
});


describe('Copy B', () => {
  for (const readOnly of [false, true]) {
    it(`copies the original B including CRLF and a trailing newline (readOnly=${readOnly})`, async () => {
      const previousDocument = globalThis.document;
      const previousNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
      const copied = [];
      globalThis.document = createDomShim().document;
      Object.defineProperty(globalThis, 'navigator', { configurable: true, value: {
        clipboard: { writeText: async (text) => { copied.push(text); } },
      } });
      try {
        const original = '  first line\r\n<script>literal</script>\r\n';
        const row = projectAgentRow({ ...REPLACED_ROW, prompt_superseded: { ...KEPT, prompt: original } });
        const tree = render(baseModel([row]), { readOnly });
        const copy = button(tree, 'agent-arch-copy-b');
        assert.ok(copy, 'the compare offers Copy B');
        copy.dispatchEvent(new Event('click'));
        await new Promise((resolve) => setImmediate(resolve));
        assert.deepEqual(copied, [original]);
        assert.equal(findById(tree, 'agent-arch-compare-right').textContent, 'B (line view)');
        assert.match(findById(tree, 'agent-arch-copy-status').textContent, /copied/i);
      } finally {
        globalThis.document = previousDocument;
        if (previousNavigator) Object.defineProperty(globalThis, 'navigator', previousNavigator);
        else delete globalThis.navigator;
      }
    });
  }
});


for (const mode of ['missing navigator', 'missing clipboard', 'rejected write']) {
  it(`Copy B reports a fallback message for ${mode}`, async () => {
    const previousDocument = globalThis.document;
    const previousNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
    globalThis.document = createDomShim().document;
    Object.defineProperty(globalThis, 'navigator', { configurable: true, value:
      mode === 'missing navigator' ? undefined : mode === 'missing clipboard' ? {} : {
        clipboard: { writeText: async () => { throw new Error('permission denied'); } },
      },
    });
    try {
      const tree = render(baseModel([REPLACED_ROW]));
      button(tree, 'agent-arch-copy-b').dispatchEvent(new Event('click'));
      await new Promise((resolve) => setImmediate(resolve));
      const status = findById(tree, 'agent-arch-copy-status');
      assert.equal(status.getAttribute('role'), 'status');
      assert.match(status.textContent, /could not copy B/i);
      assert.match(status.textContent, /clipboard access/i);
    } finally {
      globalThis.document = previousDocument;
      if (previousNavigator) Object.defineProperty(globalThis, 'navigator', previousNavigator);
      else delete globalThis.navigator;
    }
  });
}
