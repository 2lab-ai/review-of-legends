/** Admins on Settings: projection, validation mirroring the server, who gets which controls, refusals. */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  MAX_ADMINS,
  OWNER_NOTE,
  addedAdminNotice,
  adminActionError,
  adminControls,
  adminFormState,
  adminRules,
  createAdminListSync,
  describeAdminRow,
  isAdminsEvent,
  normalizeAdminEmail,
  projectAdminRow,
  projectAdminSettings,
  removedAdminNotice,
  validateAdminDraft,
} from './admins.js';
import { describeApiError } from './errors.js';

const OWNER = 'owner@company.example';
const PAYLOAD = {
  owner: OWNER,
  admins: [
    { email: 'ops@example.com', added_by: OWNER, added_at: '2026-10-14T00:00:00+00:00' },
    { email: 'dev@company.example', added_by: OWNER, added_at: '2026-10-14T00:01:00+00:00' },
  ],
  can_edit: true,
  max_added: 50,
};

function model(payload, extra = {}) {
  const projected = projectAdminSettings(payload);
  return {
    owner: projected.owner, rows: projected.admins, canEdit: projected.can_edit, maxAdded: projected.max_added, loaded: true, ...extra,
  };
}

function rules(payload = PAYLOAD) {
  return adminRules(model(payload));
}

describe('normalizeAdminEmail mirrors dashboard_auth.normalize_admin_email', () => {
  it('strips and lowercases, counting length in code points', () => {
    assert.deepEqual(normalizeAdminEmail('  Ops@Example.COM '), { email: 'ops@example.com', error: null });
    const longest = `${'a'.repeat(242)}@example.com`;
    assert.equal(longest.length, 254);
    assert.equal(normalizeAdminEmail(longest).error, null);
    assert.match(normalizeAdminEmail(`${'a'.repeat(243)}@example.com`).error, /at most 254 characters/);
    // 254 code points, 255 UTF-16 units: counted as the server counts, so the
    // refusal names the same problem the server would (not the length).
    assert.match(normalizeAdminEmail(`\u{1D400}${'a'.repeat(241)}@example.com`).error, /ASCII/);
  });

  it('refuses the same inputs the server refuses', () => {
    for (const value of [
      null, 7, '', '   ', 'ops', 'ops@', '@example.com', 'ops@@example.com', 'o@ps@example.com',
      'o ps@example.com', 'ops@exa\tmple.com', 'ops\u0000@example.com', 'ops​@example.com',
      'ops@example.com‮', 'ops @example.com',
    ]) {
      assert.notEqual(normalizeAdminEmail(value).error, null, JSON.stringify(value));
    }
    assert.match(normalizeAdminEmail('ops​@example.com').error, /spaces or invisible characters/);
    assert.match(normalizeAdminEmail('o@ps@example.com').error, /single "@"/);
  });

  it('refuses non-ASCII addresses, before lowercasing folds one into another', () => {
    // KELVIN SIGN lowercases to "k"; a Cyrillic "е" lists exactly like a Latin "e".
    for (const value of ['\u212Aate@example.com', ' \u212AATE@EXAMPLE.COM ', 'd\u0435v@company.example', 'ops@exampl\u00e9.com', '\u00f6ps@example.com']) {
      const { error } = normalizeAdminEmail(value);
      assert.match(error || '', /ASCII/, JSON.stringify(value));
      assert.doesNotMatch(error, /kate|dev|ops/i, 'the message never echoes the input');
    }
    assert.deepEqual(normalizeAdminEmail('Kate@Example.com'), { email: 'kate@example.com', error: null });
    assert.match(validateAdminDraft('\u212Aate@example.com', rules()).errors[0], /ASCII/);
    assert.equal(projectAdminRow({ email: 'd\u0435v@company.example' }), null);
  });
});

describe('projectAdminSettings', () => {
  it('keeps well-formed rows and drops the rest, never listing the owner', () => {
    const projected = projectAdminSettings({
      ...PAYLOAD,
      admins: [
        ...PAYLOAD.admins,
        { email: 'Upper@Example.com' },
        { email: ' spaced@example.com' },
        { email: OWNER },
        { email: 'ops@example.com' },
        { email: 7 },
        'loose@example.com',
        null,
      ],
    });
    assert.deepEqual(projected.admins.map((row) => row.email), ['ops@example.com', 'dev@company.example']);
    assert.deepEqual(projected.admins[0], { email: 'ops@example.com', added_by: OWNER, added_at: '2026-10-14T00:00:00+00:00' });
    assert.equal(projected.owner, OWNER);
    assert.equal(projected.can_edit, true);
    assert.equal(projected.max_added, 50);
    assert.equal(projected.error, null);
    assert.equal(projectAdminRow({ email: 'ops@example.com' }).added_by, null);
  });

  it('shows the controls only for an explicit can_edit true', () => {
    for (const can_edit of [false, 'true', 1, undefined, null]) {
      assert.equal(projectAdminSettings({ ...PAYLOAD, can_edit }).can_edit, false, String(can_edit));
    }
  });

  it('falls back to defaults on an empty or failed answer', () => {
    const projected = projectAdminSettings({ owner: OWNER, error: 'The dashboard store could not be read; only the owner is admin until it can.' });
    assert.deepEqual(projected.admins, []);
    assert.equal(projected.can_edit, false);
    assert.equal(projected.max_added, MAX_ADMINS);
    assert.match(projected.error, /only the owner is admin/);
    assert.deepEqual(projectAdminSettings(null), { owner: null, admins: [], can_edit: false, max_added: MAX_ADMINS, error: null });
  });

  it('describes who added each admin and when', () => {
    const now = Date.parse('2026-10-14T00:05:00Z');
    const [ops] = projectAdminSettings(PAYLOAD).admins;
    assert.equal(describeAdminRow(ops, now), `added by ${OWNER}, 5m ago`);
    assert.equal(describeAdminRow({ ...ops, added_by: null, added_at: null }, now), 'added on this dashboard');
    assert.equal(OWNER_NOTE, 'Owner — always admin');
  });
});

describe('admin draft', () => {
  it('treats a draft still being typed as not ready, without an error', () => {
    for (const draft of ['', '   ', 'ops', 'ops@']) {
      assert.deepEqual(validateAdminDraft(draft, rules()), { ok: false, email: draft.trim(), errors: [] }, draft);
    }
  });

  it('mirrors the server: shape, the owner, duplicates in any case, and the cap', () => {
    assert.deepEqual(validateAdminDraft('  New@Example.com ', rules()), { ok: true, email: 'new@example.com', errors: [] });
    assert.deepEqual(validateAdminDraft('@example.com', rules()).errors, [
      'Write one address with a single "@" and text on both sides, e.g. name@example.com.',
    ]);
    assert.match(validateAdminDraft('new user@example.com', rules()).errors[0], /spaces or invisible characters/);
    assert.deepEqual(validateAdminDraft(' Owner@Company.EXAMPLE ', rules()).errors, [`${OWNER} is the owner and is always admin.`]);
    assert.deepEqual(validateAdminDraft('OPS@example.com', rules()).errors, ['ops@example.com is already an admin.']);
    const full = { ...PAYLOAD, admins: Array.from({ length: 50 }, (_, index) => ({ email: `user${index}@example.com` })) };
    assert.deepEqual(validateAdminDraft('one-more@example.com', rules(full)).errors,
      ['At most 50 admins can be added; remove one first.']);
  });

  it('enables Add only for a valid draft while nothing is saving', () => {
    assert.equal(adminFormState('new@example.com', rules()).canAdd, true);
    assert.equal(adminFormState('new@example.com', rules(), { busy: true }).canAdd, false);
    assert.equal(adminFormState('ops@example.com', rules()).canAdd, false);
    assert.equal(adminFormState(' NEW@example.com ', rules()).email, 'new@example.com');
  });
});

describe('who gets which controls', () => {
  it('gives the owner Remove buttons and the add form', () => {
    assert.deepEqual(adminControls(model(PAYLOAD)), { showRemove: true, showForm: true, note: null });
    assert.equal(adminControls(model(PAYLOAD, { loaded: false })).showForm, false, 'no form before the list loads');
  });

  it('shows everyone else the list read-only, including an added admin who edits the rest of Settings', () => {
    // A viewer and an added admin get the same answer from the server: can_edit false.
    const readOnly = adminControls(model({ ...PAYLOAD, can_edit: false }));
    assert.deepEqual(readOnly, {
      showRemove: false,
      showForm: false,
      note: `Read-only: only the owner (${OWNER}) can add or remove admins.`,
    });
    assert.equal(adminControls(model({ ...PAYLOAD, can_edit: false }, { loaded: false })).note, null);
    assert.equal(adminControls(null).showRemove, false);
    assert.equal(adminControls(null).showForm, false);
  });

  it('renders the section on both the read-only and the editable Settings page', () => {
    // view-settings.js is DOM-bound: pin that the admins section is not tied to
    // the policy/repositories readOnly flag (an added admin edits those, not this).
    const view = readFileSync(fileURLToPath(new URL('./view-settings.js', import.meta.url)), 'utf8');
    assert.equal(view.match(/admins \? renderAdminSettings\(\{ \.\.\.admins, now \}\) : null/g).length, 2);
    const section = view.slice(view.indexOf('export function renderAdminSettings('), view.indexOf('export function renderSettings('));
    assert.ok(section.includes('adminControls(model)'));
    assert.doesNotMatch(section, /readOnly/);
    // The list itself is shown to every role once loaded; only the controls depend on canEdit.
    assert.equal(section.split('if (model.loaded && model.owner) {').length, 2, 'the list guard does not depend on canEdit');
  });

  it('tells the owner which addresses a grant can work for', () => {
    const view = readFileSync(fileURLToPath(new URL('./view-settings.js', import.meta.url)), 'utf8');
    const form = view.slice(view.indexOf('function adminForm('), view.indexOf('export function syncAdminForm('));
    assert.match(form, /a Gmail address or a Google Workspace account/);
  });

  it('builds the add rules in one place', () => {
    assert.deepEqual(adminRules({ owner: OWNER, rows: PAYLOAD.admins, maxAdded: 7, draft: 'x' }),
      { owner: OWNER, admins: PAYLOAD.admins, maxAdded: 7 });
    for (const file of ['./app.js', './view-settings.js']) {
      const source = readFileSync(fileURLToPath(new URL(file, import.meta.url)), 'utf8');
      assert.doesNotMatch(source, /function adminRules\(/, file);
      assert.match(source, /\badminRules,?\n/, `${file} imports adminRules`);
    }
  });

  it('re-reads this session\'s role on an admins event and re-routes when it changed', () => {
    const app = readFileSync(fileURLToPath(new URL('./app.js', import.meta.url)), 'utf8');
    assert.equal(app.split('if (isAdminsEvent(event)) refreshers.identity.schedule();').length, 2);
    const refresh = app.slice(app.indexOf('async function refreshIdentity('), app.indexOf('/* ----------------------------------------------------------------- actions */'));
    for (const statement of [
      'const identity = await api.me();',
      'state.identity = identity;',
      'if (!before || before.role !== identity.role || before.owner !== identity.owner) onRouteChange();',
    ]) assert.equal(refresh.split(statement).length, 2, statement);
    const applied = refresh.indexOf('state.identity = identity;');
    const rerouted = refresh.indexOf('onRouteChange();');
    assert.ok(applied >= 0 && rerouted >= 0 && applied < rerouted, 'the new identity is applied before the route re-runs');
  });

  it('forgets the list and its controls on sign-out', () => {
    const app = readFileSync(fileURLToPath(new URL('./app.js', import.meta.url)), 'utf8');
    const toSignIn = app.slice(app.indexOf('function toSignIn('), app.indexOf('function handleError('));
    assert.match(toSignIn, /state\.admins = emptyAdmins\(\);/);
  });
});

describe('events, notices and refusals', () => {
  it('recognises a change to the admin list, and only that', () => {
    assert.equal(isAdminsEvent({ kind: 'setting_updated', payload: { key: 'admins' } }), true);
    assert.equal(isAdminsEvent({ kind: 'setting_deleted', payload: { key: 'admins' } }), true);
    assert.equal(isAdminsEvent({ kind: 'setting_updated', payload: { key: 'repositories' } }), false);
    assert.equal(isAdminsEvent({ kind: 'run_updated', payload: { key: 'admins' } }), false);
    assert.equal(isAdminsEvent({ kind: 'setting_updated' }), false);
    assert.equal(isAdminsEvent(null), false);
  });

  it('says when a change takes effect', () => {
    assert.equal(addedAdminNotice('ops@example.com'), 'ops@example.com is now an admin, from their next request; no new sign-in is needed.');
    assert.equal(removedAdminNotice('ops@example.com'), 'ops@example.com is no longer an admin. Their admin sessions end on their next request.');
  });

  it("leads with the server's reason, and names an owner-only refusal", () => {
    const owner = adminActionError(describeApiError({ status: 403, body: { error: 'owner role required' } }), 'add');
    assert.equal(owner.title, 'Owner only');
    assert.match(owner.recovery, /Only the owner can add or remove admins/);

    const csrf = describeApiError({ status: 403, body: { error: 'invalid CSRF token' } });
    assert.equal(adminActionError(csrf, 'add'), csrf, 'other permission failures keep the shared wording');

    const duplicate = adminActionError(describeApiError({ status: 400, body: { error: 'ops@example.com is already an admin.' } }), 'add');
    assert.deepEqual([duplicate.title, duplicate.detail, duplicate.recovery], ['The admin was not added', 'ops@example.com is already an admin.', '']);
    const ownerRemoval = adminActionError(describeApiError({ status: 409, body: { error: `${OWNER} is the owner and is always admin; it cannot be removed.` } }), 'remove');
    assert.equal(ownerRemoval.title, 'The admin was not removed');
    assert.doesNotMatch(`${ownerRemoval.title} ${ownerRemoval.recovery}`, /pull request/i);
    assert.equal(adminActionError(describeApiError({ status: 500, body: null }), 'remove').recovery,
      'Reload the list to see who is admin, then try again.');
    const signedOut = describeApiError({ status: 401, body: null });
    assert.equal(adminActionError(signedOut, 'add'), signedOut);
    assert.equal(adminActionError(null, 'add'), null);
  });
});

describe('admin list ordering (createAdminListSync)', () => {
  /** A Settings page reduced to the list and its sync, driven exactly as app.js drives it. */
  function page(rows) {
    const sync = createAdminListSync();
    const view = { rows, reads: 0 };
    return {
      view,
      sync,
      /** Starts a GET; returns its answer callback, or null when it only marked the list stale. */
      read() {
        if (!sync.startRead()) return null;
        view.reads += 1;
        return (answer) => {
          if (sync.canApplyRead()) view.rows = answer;
          return sync.finishRead();
        };
      },
      /** An add/remove answered: its list is applied; true when a fresh GET must start now. */
      mutation(answer) {
        view.rows = answer;
        return sync.applyMutation();
      },
    };
  }

  for (const [action, before, after] of [
    ['add', ['ops@example.com'], ['ops@example.com', 'new@example.com']],
  ]) {
    it(`keeps the ${action} answer when a GET that started before it answers late`, () => {
      const tab = page(before);
      const answerOld = tab.read();
      assert.equal(tab.mutation(after), false, 'a GET is in flight: it re-reads instead of a second GET');
      const again = answerOld(before);
      assert.deepEqual(tab.view.rows, after, 'the pre-mutation list is not shown');
      assert.equal(again, true, 'one fresh GET follows');
      const answerFresh = tab.read();
      assert.equal(answerFresh(after), false);
      assert.deepEqual(tab.view.rows, after);
      assert.equal(tab.view.reads, 2);
    });
  }

  it('reads once more after an add/remove answer, which may be older than a list another tab wrote', () => {
    const tab = page(['ops@example.com']);
    // Another owner tab removed ops after this tab's add committed; this tab's GET already showed that.
    tab.view.rows = ['new@example.com'];
    assert.equal(tab.mutation(['ops@example.com', 'new@example.com']), true, 'a fresh GET starts now');
    const answer = tab.read();
    assert.equal(answer(['new@example.com']), false);
    assert.deepEqual(tab.view.rows, ['new@example.com']);
  });

  it('a refresh during a read (the first load included) marks the list stale: exactly one re-read', () => {
    const sync = createAdminListSync();
    assert.equal(sync.startRead(), true);
    assert.equal(sync.startRead(), false);
    assert.equal(sync.startRead(), false);
    assert.equal(sync.canApplyRead(), true, 'a refresh alone does not make the running answer older');
    assert.equal(sync.finishRead(), true);
    assert.equal(sync.startRead(), true);
    assert.equal(sync.finishRead(), false, 'exactly one re-read');
    assert.equal(sync.startRead(), true, 'the next refresh reads again');
    assert.equal(sync.finishRead(), false);
  });

  it('app.js sends every list read and add/remove answer through the sync', () => {
    const app = readFileSync(fileURLToPath(new URL('./app.js', import.meta.url)), 'utf8');
    const load = app.slice(app.indexOf('async function loadAdminSettings('), app.indexOf('async function refreshIdentity('));
    // Whole statements, not method names: a reversed or dropped guard must fail here.
    for (const statement of [
      'if (!model.sync.startRead()) return;',
      'if (model.sync.canApplyRead()) applyAdminPayload(model, payload);',
      'again = model.sync.finishRead();',
      'if (again && state.admins === model) loadAdminSettings();',
    ]) assert.equal(load.split(statement).length, 2, statement);
    const mutate = app.slice(app.indexOf('async function mutateAdmins('), app.indexOf('function addAdmin('));
    for (const statement of [
      'readNow = model.sync.applyMutation();',
      'if (readNow && state.admins === model) loadAdminSettings();',
    ]) assert.equal(mutate.split(statement).length, 2, statement);
    const applied = mutate.indexOf('applyAdminPayload(model, payload);');
    assert.ok(applied >= 0 && applied < mutate.indexOf('readNow = model.sync.applyMutation();'), 'the answer is applied, then counted');
    // Refreshes during the first load are kept, not dropped until it is loaded.
    assert.match(app, /admins: createCoalescer\(\(\) => \(state\.admins\.loaded \|\| state\.admins\.loading \? loadAdminSettings\(\) : null\)\)/);
    const empty = app.slice(app.indexOf('function emptyAdmins('), app.indexOf('const state = {'));
    assert.match(empty, /sync: createAdminListSync\(\)/, 'sign-out starts a fresh sync');
  });
});
