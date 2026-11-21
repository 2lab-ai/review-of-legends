/** The visual outline and UI diff switches of each repository on the Settings
 *  page: projection, wording, and the rendered checkboxes (dom-shim.js). */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { createDomShim, findAll, findById } from './dom-shim.js';
import {
  describeUiDiffBlocker,
  describeVisualOutline,
  hasOutlineOverride,
  outlineNotice,
  projectRepositorySettings,
  projectVisualOutline,
  repositoryActionError,
} from './repositories.js';
import { renderRepositorySettings } from './view-settings.js';

const OUTLINE = {
  enabled: true, enabled_source: 'default', ui_diff: true, ui_diff_source: 'dashboard',
  ui_diff_effective: true, ui_diff_available: true, ui_diff_config: true,
};

const PAYLOAD = {
  repositories: [
    { repo: 'acme-corp/web', source: 'config', agents: ['a', 'b', 'c'], visual_outline: OUTLINE },
    { repo: 'acme-corp/api', source: 'config', agents: ['a', 'b', 'c'],
      visual_outline: { ...OUTLINE, enabled: false, enabled_source: 'config', ui_diff: false, ui_diff_source: 'default', ui_diff_effective: false } },
  ],
  allowed_owners: ['acme-corp'],
  max_added: 50,
};

function withShim(fn) {
  const previous = globalThis.document;
  globalThis.document = createDomShim().document;
  try {
    return fn();
  } finally {
    globalThis.document = previous;
  }
}

function render({ readOnly = false, saving = null, onOutline = () => {} } = {}) {
  const projected = projectRepositorySettings(PAYLOAD);
  const model = {
    rows: projected.repositories, allowedOwners: projected.allowed_owners, maxAdded: projected.max_added,
    note: projected.note, configError: null, draft: 'acme-corp/', draftTouched: false, loading: false,
    saving, error: null, actionError: null, notice: null, loaded: true,
  };
  return renderRepositorySettings({
    model, readOnly, onDraft: () => {}, onAdd: () => {}, onRemove: () => {}, onOutline, onReload: () => {},
  });
}

describe('repository visual outline projection', () => {
  it('keeps the switches and falls back to off for anything malformed', () => {
    const projected = projectRepositorySettings(PAYLOAD);
    assert.deepEqual(projected.repositories[0].visual_outline, OUTLINE);
    assert.deepEqual(projectVisualOutline({ enabled: 'yes', enabled_source: 'elsewhere' }), {
      enabled: false, enabled_source: 'default', ui_diff: false, ui_diff_source: 'default',
      ui_diff_effective: false, ui_diff_available: false, ui_diff_config: false,
    });
  });

  it('says what runs, where each answer came from, and why a UI diff does not run', () => {
    const [web, api] = projectRepositorySettings(PAYLOAD).repositories;
    assert.equal(describeVisualOutline(web), 'Visual outline: on (config default) · UI diff: on (set here)');
    assert.equal(describeVisualOutline(api), 'Visual outline: off (config/reviewer.json) · UI diff: off (config default)');
    assert.equal(describeUiDiffBlocker(web), null);
    assert.match(describeUiDiffBlocker({ visual_outline: { ...web.visual_outline, enabled: false } }), /turn the outline on/);
    assert.match(describeUiDiffBlocker({ visual_outline: { ...web.visual_outline, ui_diff_available: false } }), /not installed/);
    assert.equal(hasOutlineOverride(web), true);
    assert.equal(hasOutlineOverride(api), false);
    assert.match(outlineNotice('acme-corp/web', web), /^Saved for acme-corp\/web: Visual outline: on/);
    assert.equal(repositoryActionError({ status: 400, title: 'x', detail: 'd' }, 'outline').title,
      'The visual outline setting was not changed');
  });
});

describe('repository visual outline switches, rendered', () => {
  it('sends one flag per click and resets both with Use config', () => withShim(() => {
    const sent = [];
    const tree = render({ onOutline: (repo, flags) => sent.push([repo, flags]) });
    const outline = findById(tree, 'repo-outline-1');
    const uiDiff = findById(tree, 'repo-uidiff-0');
    assert.equal(findById(tree, 'repo-outline-0').checked, true);
    assert.ok(!outline.checked);
    assert.equal(uiDiff.checked, true);
    outline.checked = true;
    outline.dispatchEvent(new Event('change'));
    uiDiff.checked = false;
    uiDiff.dispatchEvent(new Event('change'));
    const reset = findAll(tree, 'button').find((node) => node.id === 'repo-outline-reset-0');
    assert.ok(reset, 'a dashboard choice can be handed back');
    assert.equal(findAll(tree, 'button').find((node) => node.id === 'repo-outline-reset-1'), undefined);
    reset.dispatchEvent(new Event('click'));
    assert.deepEqual(sent, [
      ['acme-corp/api', { enabled: true }],
      ['acme-corp/web', { ui_diff: false }],
      ['acme-corp/web', { enabled: null, ui_diff: null }],
    ]);
  }));

  it('disables every switch while a change is saving', () => withShim(() => {
    const tree = render({ saving: 'outline:acme-corp/web' });
    for (const id of ['repo-outline-0', 'repo-uidiff-0', 'repo-outline-1', 'repo-uidiff-1']) {
      assert.equal(findById(tree, id).disabled, true, id);
    }
  }));

  it('shows a read-only account the same answers as text', () => withShim(() => {
    const tree = render({ readOnly: true });
    assert.equal(findById(tree, 'repo-outline-0'), null);
    const texts = [];
    const walk = (node) => {
      if (node.nodeType === 3) texts.push(node.textContent || node.data || '');
      for (const child of node.childNodes || []) walk(child);
    };
    walk(tree);
    assert.ok(texts.some((text) => text.includes('Visual outline: on (config default) · UI diff: on (set here)')), texts.join('|'));
  }));
});
