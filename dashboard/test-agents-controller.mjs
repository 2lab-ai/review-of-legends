/** Agents page controller: the state transitions behind the editor, exercised
 *  directly (no source regex, no DOM) — see agents-controller.js's own doc
 *  comment for why app.js stays thin glue around these.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { draftFromAgent, refreshAgentDrafts, projectAgentRow } from './agents.js';
import {
  applyMutationResult, beginMutation, cancelDiscard, createAgentsReadSync, dropStash, emptyAgentsModel,
  finishMutation, followReadInBaselines, putBackStash, reconcileDiscardConfirm, requestDiscard, requestReset,
  stashEditsOnDefaultChange, supersededToken, takePendingBaselines,
} from './agents-controller.js';

const SHA_B = 'b'.repeat(64);
const SHA_B2 = 'e'.repeat(64);
const KEPT = {
  prompt: 'Line one\nmy edit\n',
  sha: SHA_B,
  saved_at: '2026-10-14T00:00:00+00:00',
  saved_by: 'owner@company.example',
  superseded_at: '2026-10-15T09:00:00+00:00',
};

function supersededRow(overrides = {}) {
  return projectAgentRow({
    id: 'arch',
    prompt: 'Line one\nnew default\n',
    prompt_source: 'file',
    prompt_superseded: { ...KEPT, ...overrides },
  });
}

function plainRow(id = 'arch') {
  return projectAgentRow({ id, prompt: 'A prompt.\n', prompt_source: 'file' });
}

/** A GET answer applied as loadAgentSettings applies one (agents-handlers.js,
 *  keepDrafts): the same controller calls in the same order, between
 *  startRead and finishRead. */
function applyRead(model, agents) {
  assert.equal(model.sync.startRead(), true);
  assert.equal(model.sync.canApplyRead(), true);
  const previousDrafts = model.drafts;
  model.drafts = refreshAgentDrafts(model.rows, model.drafts, agents);
  model.rows = agents;
  takePendingBaselines(model, previousDrafts);
  const setAside = stashEditsOnDefaultChange(model);
  followReadInBaselines(model, previousDrafts, setAside);
  reconcileDiscardConfirm(model);
  return model.sync.finishRead();
}

describe('stash for an agent whose id is an Object property name', () => {
  it('starts empty for "constructor"', () => {
    const model = emptyAgentsModel();
    const onA = projectAgentRow({ id: 'constructor', prompt: 'default A', default_sha: 'a'.repeat(64) });
    const onC = projectAgentRow({ id: 'constructor', prompt: 'default C', default_sha: 'c'.repeat(64) });
    model.rows = [onC];
    model.drafts.constructor = { ...draftFromAgent(onA), prompt: 'my edit' };
    stashEditsOnDefaultChange(model);
    assert.equal(model.stash.constructor, 'my edit');
  });
});

describe('emptyAgentsModel', () => {
  it('starts with no rows, no in-flight work and its own read sync', () => {
    const model = emptyAgentsModel();
    assert.deepEqual(model.rows, []);
    assert.equal(model.saving, null);
    assert.equal(model.confirmDiscard, null);
    assert.equal(model.loaded, false);
    assert.equal(typeof model.sync.startRead, 'function');
  });
});

describe('confirmDiscard: arm, cancel, confirm', () => {
  it('arms on the first Discard-B click, confirms on the second, bound to the agent and the B shown', () => {
    const model = emptyAgentsModel();
    const row = supersededRow();
    model.rows = [row];
    model.notice = 'stale notice';
    assert.equal(requestDiscard(model, row), 'armed');
    assert.deepEqual(model.confirmDiscard, { agentId: 'arch', action: 'discard', token: supersededToken(row) });
    assert.equal(model.notice, null, 'arming clears a stale notice');
    assert.equal(requestDiscard(model, row), 'confirmed', 'the second click on the same B confirms');
  });

  it('is a no-op for a row with nothing to discard, or no row at all', () => {
    const model = emptyAgentsModel();
    assert.equal(requestDiscard(model, plainRow()), 'noop');
    assert.equal(model.confirmDiscard, null);
    assert.equal(requestDiscard(model, null), 'noop');
  });

  it('Reset with a B arms its own confirmation; a click of the other action re-arms, never confirms', () => {
    const model = emptyAgentsModel();
    const row = supersededRow();
    model.rows = [row];
    assert.equal(requestReset(model, plainRow()), 'noop', 'no B: Reset needs no question');
    assert.equal(requestReset(model, row), 'armed');
    assert.deepEqual(model.confirmDiscard, { agentId: 'arch', action: 'reset', token: SHA_B });
    assert.equal(requestDiscard(model, row), 'armed', 'an armed reset does not confirm a discard');
    assert.equal(model.confirmDiscard.action, 'discard');
    assert.equal(requestReset(model, row), 'armed');
    assert.equal(requestReset(model, row), 'confirmed');
    assert.equal(cancelDiscard(model, 'arch'), true, 'Cancel disarms it like Keep B');
  });

  it('a second click on a card that now names another B asks again for that B, never confirms the first', () => {
    const model = emptyAgentsModel();
    const firstB = supersededRow();
    model.rows = [firstB];
    assert.equal(requestDiscard(model, firstB), 'armed');
    const secondB = supersededRow({ prompt: 'Line one\nyet another edit\n', sha: SHA_B2 });
    assert.equal(requestDiscard(model, secondB), 'armed', 'the confirmation was for the first B');
    assert.deepEqual(model.confirmDiscard, { agentId: 'arch', action: 'discard', token: SHA_B2 });
    assert.equal(requestDiscard(model, secondB), 'confirmed', 'a click on that same B again confirms it');
    // The same by the sha the clicked card rendered (a focused card is not rebuilt by a read).
    assert.equal(requestReset(model, secondB, SHA_B), 'armed');
    assert.equal(requestReset(model, secondB, SHA_B2), 'armed', 'Reset armed on the first B does not reset another');
  });

  it('Keep B cancels an armed confirmation, and only the one it names', () => {
    const model = emptyAgentsModel();
    const row = supersededRow();
    requestDiscard(model, row);
    assert.equal(cancelDiscard(model, 'someone-else'), false, 'cancelling a different agent does nothing');
    assert.ok(model.confirmDiscard, 'and leaves the armed one alone');
    assert.equal(cancelDiscard(model, 'arch'), true);
    assert.equal(model.confirmDiscard, null);
    assert.equal(cancelDiscard(model, 'arch'), false, 'nothing left to cancel');
  });
});

describe('the discard confirmation is bound to the exact B on screen (supersededToken)', () => {
  it('tokens by the sha the server gives B; a different B has a different token; no sha, no token', () => {
    assert.equal(supersededToken(supersededRow()), SHA_B, 'the same value Discard/Rollback/Reset send back');
    assert.equal(supersededToken(supersededRow({ superseded_at: null })), SHA_B, 'superseded_at plays no part');
    assert.equal(supersededToken(supersededRow({ sha: SHA_B2, prompt: 'different edit' })), SHA_B2);
    assert.equal(supersededToken(supersededRow({ sha: undefined })), null, 'no client-side hash stands in for the server sha');
    assert.equal(supersededToken(plainRow()), null, 'nothing to discard: no token');
  });

  it('a refresh that brings a DIFFERENT B disarms the confirmation, even for the same agent', () => {
    const model = emptyAgentsModel();
    const firstB = supersededRow();
    model.rows = [firstB];
    assert.equal(requestDiscard(model, firstB), 'armed');
    assert.ok(model.confirmDiscard);
    // The default changed again (or another tab rolled back/discarded and the
    // file changed once more): the row now carries a different kept edit.
    const secondB = supersededRow({ prompt: 'Line one\nyet another edit\n', sha: SHA_B2, superseded_at: '2026-10-15T11:00:00+00:00' });
    model.rows = [secondB];
    reconcileDiscardConfirm(model);
    assert.equal(model.confirmDiscard, null, 'bound to a B that is no longer on screen: disarmed, not left dangling');
  });

  it('a refresh that brings the SAME B leaves the confirmation armed', () => {
    const model = emptyAgentsModel();
    const row = supersededRow();
    model.rows = [row];
    requestDiscard(model, row);
    model.rows = [supersededRow()]; // a fresh GET answer, same B
    reconcileDiscardConfirm(model);
    assert.ok(model.confirmDiscard, 'the exact same B: still the thing the admin agreed to discard');
  });

  it('the kept edit disappearing (discarded/rolled back elsewhere, or the row itself gone) disarms it', () => {
    const model = emptyAgentsModel();
    const row = supersededRow();
    model.rows = [row];
    requestDiscard(model, row);
    model.rows = [plainRow()];
    reconcileDiscardConfirm(model);
    assert.equal(model.confirmDiscard, null);

    const model2 = emptyAgentsModel();
    model2.rows = [supersededRow()];
    requestDiscard(model2, model2.rows[0]);
    model2.rows = [];
    reconcileDiscardConfirm(model2);
    assert.equal(model2.confirmDiscard, null);

    // Nothing armed: a no-op that does not throw. It needs a row — with
    // rows=[] the lookup never runs, so a missing guard would go unnoticed.
    const model3 = emptyAgentsModel();
    model3.rows = [supersededRow()];
    reconcileDiscardConfirm(model3);
    assert.equal(model3.confirmDiscard, null);
  });
});

describe('the in-flight guard: one mutation at a time, across every agent', () => {
  it('a second beginMutation (same or another agent) is refused while one is running', () => {
    const model = emptyAgentsModel();
    model.rows = [plainRow('arch'), plainRow('tests')];
    const first = beginMutation(model, 'arch');
    assert.ok(first, 'the first request starts');
    assert.equal(model.saving, 'arch');
    assert.equal(beginMutation(model, 'arch'), null, 'a rapid second click on the same agent issues no second request');
    assert.equal(beginMutation(model, 'tests'), null, 'nor does a click on a different agent while one is in flight');
    finishMutation(model);
    assert.equal(model.saving, null);
    assert.ok(beginMutation(model, 'tests'), 'free again once the first is over');
  });

  it('clears an armed Discard-B confirmation, the error and the notice', () => {
    const model = emptyAgentsModel();
    const row = supersededRow();
    model.rows = [row];
    requestDiscard(model, row);
    model.error = { title: 'old' };
    model.notice = 'old notice';
    beginMutation(model, 'arch');
    assert.equal(model.confirmDiscard, null);
    assert.equal(model.error, null);
    assert.equal(model.notice, null);
  });
});

describe('applying a server row after Save / Rollback / Discard', () => {
  it('merges the answered row into rows, by id', () => {
    const model = emptyAgentsModel();
    model.rows = [plainRow('arch'), plainRow('tests')];
    const begun = beginMutation(model, 'arch');
    const saved = projectAgentRow({ id: 'arch', prompt: 'Edited.\n', prompt_source: 'dashboard' });
    applyMutationResult(model, 'arch', begun.submitted, saved);
    assert.deepEqual(model.rows.map((row) => [row.id, row.prompt_source]), [['arch', 'dashboard'], ['tests', 'file']]);
  });

  it('a falsy answer (nothing to apply) is a no-op', () => {
    const model = emptyAgentsModel();
    model.rows = [plainRow('arch')];
    const begun = beginMutation(model, 'arch');
    applyMutationResult(model, 'arch', begun.submitted, null);
    assert.deepEqual(model.rows, [plainRow('arch')]);
  });

  it('the confirmed click takes the question down; a Discard/Rollback answer (no more prompt_superseded) disarms one asked again while it ran', () => {
    const model = emptyAgentsModel();
    const row = supersededRow();
    model.rows = [row];
    requestDiscard(model, row); // armed
    const begun = beginMutation(model, 'arch'); // confirmed click: begins the DELETE
    assert.equal(model.confirmDiscard, null, 'the confirmed click takes the question down');
    // A question on the same B, armed while the DELETE runs: set directly, since
    // whether a click may arm one mid-request is the handlers' decision.
    model.confirmDiscard = { agentId: 'arch', action: 'discard', token: supersededToken(row) };
    const saved = projectAgentRow({ id: 'arch', prompt: row.prompt, prompt_source: 'file' }); // superseded gone
    applyMutationResult(model, 'arch', begun.submitted, saved);
    assert.equal(model.confirmDiscard, null, 'the answer leaves no B to discard: the question goes');
  });
});

describe('refresh ordering: a stale GET answer cannot resurrect a row a mutation already replaced', () => {
  it('startRead/canApplyRead/finishRead order a read against a mutation landed mid-flight', () => {
    const sync = createAgentsReadSync();
    assert.equal(sync.startRead(), true);
    assert.equal(sync.startRead(), false, 'one read at a time: a second caller is told one is already in flight');
    // The mutation's own answer lands while the read above is still out.
    assert.equal(sync.applyMutation(), false, 'a read is in flight: do not start a second one, mark it stale instead');
    assert.equal(sync.canApplyRead(), false, 'the in-flight read started before the mutation: its answer is older');
    assert.equal(sync.finishRead(), true, 'stale: read once more');
    assert.equal(sync.startRead(), true);
    assert.equal(sync.canApplyRead(), true, 'this read started after the mutation: safe to apply');
    assert.equal(sync.finishRead(), false, 'nothing landed meanwhile: no further read needed');
  });

  it('a mutation with no read in flight requests an authoritative read', () => {
    const sync = createAgentsReadSync();
    assert.equal(sync.applyMutation(), true);
  });

  it('end to end: a GET started before a Save lands is not applied, and the model keeps the saved row', () => {
    const model = emptyAgentsModel();
    model.rows = [plainRow('arch')];
    assert.equal(model.sync.startRead(), true); // a GET starts (e.g. a reconnect replay)
    const begun = beginMutation(model, 'arch'); // the admin's Save fires and lands first
    const saved = projectAgentRow({ id: 'arch', prompt: 'Saved by the admin.\n', prompt_source: 'dashboard' });
    applyMutationResult(model, 'arch', begun.submitted, saved);
    finishMutation(model);
    // The slow GET above finally answers, with the OLD (pre-save) row.
    const stalePayload = [plainRow('arch')];
    if (model.sync.canApplyRead()) model.rows = stalePayload; // must NOT run
    assert.equal(model.sync.canApplyRead(), false, 'disqualified: it started before the save it would overwrite');
    assert.equal(model.rows[0].prompt, 'Saved by the admin.\n', 'the saved row survives the stale answer');
    assert.equal(model.sync.finishRead(), true, 'and it reads once more, to catch up for real');
  });

  it('inverse order: a finished newer GET survives a late mutation answer, with the unsaved draft intact', () => {
    const model = emptyAgentsModel();
    model.rows = [plainRow()];
    model.drafts.arch = { ...draftFromAgent(model.rows[0]), prompt: 'submitted edit' };
    const begun = beginMutation(model, 'arch');
    model.drafts.arch.prompt = 'unsaved typing after Save';
    assert.equal(model.sync.startRead(), true);
    const newer = projectAgentRow({ id: 'arch', prompt: 'another tab edited later', required: true });
    assert.equal(model.sync.canApplyRead(), true);
    model.drafts = refreshAgentDrafts(model.rows, model.drafts, [newer]);
    model.rows = [newer];
    assert.equal(model.sync.finishRead(), false);

    const refresh = applyMutationResult(model, 'arch', begun.submitted,
      projectAgentRow({ id: 'arch', prompt: 'submitted edit' }));
    assert.equal(model.rows[0].prompt, 'another tab edited later', 'the late response must never replace the newer read');
    assert.equal(model.drafts.arch.prompt, 'unsaved typing after Save');
    assert.equal(model.drafts.arch.required, true);
    assert.equal(refresh, true, 'the caller must fetch an authoritative row');
  });

  it('a GET answered too late to apply does not hold back the answer of a Save sent before it answered', () => {
    const model = emptyAgentsModel();
    model.rows = [plainRow('arch')];
    const first = beginMutation(model, 'arch');
    assert.equal(model.sync.startRead(), true); // a GET goes out while the first Save is out
    applyMutationResult(model, 'arch', first.submitted,
      projectAgentRow({ id: 'arch', prompt: 'First save.\n', prompt_source: 'dashboard' }));
    finishMutation(model);
    const second = beginMutation(model, 'arch');
    // The GET answers with the list from before the first save: dropped, and read again.
    assert.equal(model.sync.canApplyRead(), false);
    assert.equal(model.sync.finishRead(), true);
    assert.equal(model.sync.startRead(), true);
    assert.equal(model.sync.canApplyMutation(), true, 'a dropped answer put nothing newer on screen');
    applyMutationResult(model, 'arch', second.submitted,
      projectAgentRow({ id: 'arch', prompt: 'Second save.\n', prompt_source: 'dashboard' }));
    assert.equal(model.rows[0].prompt, 'Second save.\n', 'the second Save shows its own row at once');
  });

});

describe('draft preservation while an edit happens during a pending request', () => {
  it('a field the admin kept typing after Save fired survives; an untouched field takes the server value', () => {
    const model = emptyAgentsModel();
    const row = plainRow('arch');
    model.rows = [row];
    model.drafts.arch = { prompt: 'typed before Save', required: false, model: '' };
    const begun = beginMutation(model, 'arch'); // submitted = {prompt: 'typed before Save', ...}
    // The admin keeps typing while the request is in flight (editAgentPrompt, in app.js).
    model.drafts.arch = { ...model.drafts.arch, prompt: 'typed WHILE the request was in flight' };
    const saved = projectAgentRow({ id: 'arch', prompt: 'typed before Save', prompt_source: 'dashboard', required: true, required_source: 'dashboard' });
    applyMutationResult(model, 'arch', begun.submitted, saved);
    assert.equal(model.drafts.arch.prompt, 'typed WHILE the request was in flight', 'the later edit is not clobbered');
    assert.equal(model.drafts.arch.required, true, 'required was not touched meanwhile: it takes the server value');
  });

  it('a mutation answer that waited for a read settles once: a later read does not rebase onto it again', () => {
    const model = emptyAgentsModel();
    model.rows = [plainRow('arch')];
    model.drafts.arch = { ...draftFromAgent(model.rows[0]), required: true };
    const begun = beginMutation(model, 'arch'); // Reset, with Required ticked
    model.drafts.arch = { ...model.drafts.arch, required: false }; // unticked while it runs
    applyRead(model, [plainRow('arch')]); // a GET lands meanwhile, so the answer waits
    assert.equal(applyMutationResult(model, 'arch', begun.submitted, plainRow('arch')), true);
    finishMutation(model);
    applyRead(model, [plainRow('arch')]); // the follow-up read settles it
    assert.equal(model.drafts.arch.required, false);
    // Another tab ticks Required; this admin has not touched it since the Reset settled.
    applyRead(model, [projectAgentRow({ id: 'arch', prompt: 'A prompt.\n', prompt_source: 'file', required: true, required_source: 'dashboard' })]);
    assert.equal(model.drafts.arch.required, true, 'the server value shows, not an unsaved untick the next Save would send');
  });
});

describe('an unsaved prompt edit the default changed under is set aside, never lost', () => {
  const SHA_A = 'a'.repeat(64);
  const SHA_C = 'c'.repeat(64);
  const onC = (id = 'arch') => projectAgentRow({ id, prompt: 'default C', default_sha: SHA_C, prompt_source: 'file' });

  function readLanded(drafts, rows = [onC()]) {
    const model = emptyAgentsModel();
    model.rows = rows;
    model.drafts = drafts;
    return model;
  }

  it('moves the edit to the stash, puts C in the editor and names the agent in the notice', () => {
    const model = readLanded({ arch: { prompt: 'my edit', required: true, model: '', defaultSha: SHA_A } });
    assert.deepEqual(stashEditsOnDefaultChange(model), ['arch']);
    assert.equal(model.stash.arch, 'my edit');
    assert.deepEqual(model.drafts.arch, { prompt: 'default C', required: true, model: '', defaultSha: SHA_C },
      'only the prompt and its default change: an unsaved Required edit stays');
    assert.match(model.notice, /^The default prompt of arch changed while you were editing; the editor shows the new default\. Your unsaved text is kept below\.$/);
  });

  it('leaves an edit made on the current default, and an untouched draft, alone', () => {
    const drafts = { arch: { prompt: 'my edit', required: true, model: '', defaultSha: SHA_C }, tests: { ...draftFromAgent(onC('tests')) } };
    const model = readLanded(drafts, [onC(), onC('tests')]);
    assert.deepEqual(stashEditsOnDefaultChange(model), []);
    assert.deepEqual(model.stash, {});
    assert.equal(model.notice, null);
    assert.equal(model.drafts.arch.prompt, 'my edit');
  });

  it('a second change under a second edit keeps the first set-aside text as well', () => {
    const model = readLanded({ arch: { prompt: 'second edit', required: true, model: '', defaultSha: SHA_A } });
    model.stash.arch = 'first edit';
    stashEditsOnDefaultChange(model);
    assert.equal(model.stash.arch, 'first edit\n\nsecond edit');
  });

  it('Put back applies the text on the default now shown; Drop forgets it; both clear the stash', () => {
    const model = readLanded({ arch: { ...draftFromAgent(onC()), required: false } });
    model.stash.arch = 'my edit';
    assert.equal(putBackStash(model, 'arch'), true);
    assert.deepEqual(model.drafts.arch, { prompt: 'my edit', required: false, model: '', defaultSha: SHA_C });
    assert.equal('arch' in model.stash, false);
    assert.equal(putBackStash(model, 'arch'), false, 'nothing left to put back');
    model.stash.arch = 'again';
    assert.equal(dropStash(model, 'arch'), true);
    assert.equal('arch' in model.stash, false);
    assert.equal(model.drafts.arch.prompt, 'my edit', 'Drop leaves the editor as it is');
    assert.equal(dropStash(model, 'arch'), false);
  });
});
