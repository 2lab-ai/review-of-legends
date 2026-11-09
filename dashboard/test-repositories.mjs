/** Review repositories: projection, the add draft, validation mirroring the server, refusals. */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  BASELINE_NOTE,
  MAX_ADDED,
  addedCount,
  addedNotice,
  describeRepositoryAgents,
  describeRepositoryProblem,
  describeRepositorySource,
  initialRepositoryDraft,
  nextRepositoryDraft,
  projectRepositoryRow,
  projectRepositorySettings,
  removedNotice,
  repositoryActionError,
  repositoryFormState,
  validateRepositoryDraft,
} from './repositories.js';
import { describeApiError } from './errors.js';

const PAYLOAD = {
  repositories: [
    { repo: 'acme-corp/Gamma', source: 'config', agents: ['a', 'b', 'c', 'arch'], added_by: null, added_at: null },
    { repo: 'acme-corp/review-of-legends', source: 'config', agents: ['a', 'b', 'c'], added_by: null, added_at: null },
    { repo: 'acme-corp/MiniMoon', source: 'dashboard', agents: ['a', 'b', 'c'], added_by: 'owner@company.example', added_at: '2026-10-13T00:00:00+00:00' },
  ],
  allowed_owners: ['acme-corp'],
  max_added: 50,
  note: BASELINE_NOTE,
};

function rules(payload = PAYLOAD) {
  const projected = projectRepositorySettings(payload);
  return { rows: projected.repositories, allowedOwners: projected.allowed_owners, maxAdded: projected.max_added };
}

describe('projectRepositorySettings', () => {
  it('keeps well-formed rows and drops the rest', () => {
    const projected = projectRepositorySettings({
      ...PAYLOAD,
      repositories: [...PAYLOAD.repositories, { repo: 'no-slash' }, { repo: 'a/..x y' }, 'junk', null, { repo: 'acme-corp/x', source: 'other', agents: ['a', 7] }],
    });
    assert.deepEqual(projected.repositories.map((row) => row.repo), [
      'acme-corp/Gamma', 'acme-corp/review-of-legends', 'acme-corp/MiniMoon', 'acme-corp/x',
    ]);
    const last = projected.repositories[3];
    assert.equal(last.source, 'config', 'only "dashboard" marks an added repository');
    assert.deepEqual(last.agents, ['a']);
    assert.equal(projected.repositories[2].added_by, 'owner@company.example');
    assert.deepEqual(projected.allowed_owners, ['acme-corp']);
    assert.equal(projected.max_added, 50);
    assert.equal(projected.note, BASELINE_NOTE);
    assert.equal(projected.error, null);
  });

  it('carries why the service skips a repository', () => {
    const problem = 'at least 3 agents must review acme-corp/MiniMoon; check opt_in and repository_agents';
    const row = projectRepositoryRow({ ...PAYLOAD.repositories[2], agents: ['a', 'b'], problem });
    assert.equal(row.problem, problem);
    assert.equal(describeRepositoryProblem(row), `Not reviewed: ${problem}`);
    const fine = projectRepositoryRow(PAYLOAD.repositories[2]);
    assert.equal(fine.problem, null);
    assert.equal(describeRepositoryProblem(fine), null);
  });

  it('falls back to defaults on an empty or failed answer', () => {
    const projected = projectRepositorySettings({ error: 'service config unavailable' });
    assert.deepEqual(projected.repositories, []);
    assert.deepEqual(projected.allowed_owners, []);
    assert.equal(projected.max_added, MAX_ADDED);
    assert.equal(projected.note, BASELINE_NOTE);
    assert.equal(projected.error, 'service config unavailable');
    assert.equal(projectRepositoryRow(null), null);
    assert.equal(projectRepositorySettings(null).repositories.length, 0);
  });

  it('describes where a repository comes from and who reviews it', () => {
    const [gamma, , minimoon] = projectRepositorySettings(PAYLOAD).repositories;
    const now = Date.parse('2026-10-13T00:05:00Z');
    assert.equal(describeRepositorySource(gamma, now), 'from config/reviewer.json');
    assert.match(describeRepositorySource(minimoon, now), /^added on this dashboard by owner@company\.example, 5m ago$/);
    assert.equal(describeRepositorySource({ ...minimoon, added_by: null, added_at: null }, now), 'added on this dashboard');
    assert.equal(describeRepositoryAgents(gamma), 'Reviewed by: a, b, c, arch');
    assert.equal(describeRepositoryAgents({ ...gamma, agents: [] }), 'No reviewer agent runs on it');
    assert.equal(addedCount(projectRepositorySettings(PAYLOAD).repositories), 1);
  });
});

describe('repository draft', () => {
  it('starts as owner/ only when exactly one owner is allowed, and keeps what was typed', () => {
    assert.equal(initialRepositoryDraft(['acme-corp']), 'acme-corp/');
    assert.equal(initialRepositoryDraft(['acme-corp', '2lab-ai']), '');
    assert.equal(initialRepositoryDraft([]), '');
    assert.equal(nextRepositoryDraft('acme-corp/Mini', true, ['acme-corp']), 'acme-corp/Mini');
    assert.equal(nextRepositoryDraft('', true, ['acme-corp']), '', 'a cleared input stays cleared');
    assert.equal(nextRepositoryDraft('', false, ['acme-corp']), 'acme-corp/');
  });

  it('treats a draft without a name as not ready, without an error', () => {
    for (const draft of ['', '   ', 'acme-corp', 'acme-corp/']) {
      assert.deepEqual(validateRepositoryDraft(draft, rules()), { ok: false, repo: draft.trim(), errors: [] }, draft);
    }
  });

  it('mirrors the server: format, owner, duplicates in any case, and the cap', () => {
    const ok = validateRepositoryDraft('  acme-corp/MiniMoonBo ', rules());
    assert.deepEqual(ok, { ok: true, repo: 'acme-corp/MiniMoonBo', errors: [] });
    assert.equal(validateRepositoryDraft('Acme-Corp/MiniMoonFront', rules()).ok, true, 'owners compare case-insensitively');
    for (const bad of ['acme-corp/Mini Gold', 'acme-corp/..', 'acme-corp/.', '-x/y', 'a/b/c', `acme-corp/${'x'.repeat(101)}`]) {
      assert.match(validateRepositoryDraft(bad, rules()).errors[0], /^Write the repository as owner\/name/, bad);
    }
    assert.deepEqual(validateRepositoryDraft('octocat/hello', rules()).errors,
      ['octocat is not an owner this service reviews (allowed: acme-corp).']);
    assert.deepEqual(validateRepositoryDraft('acme-corp/gamma', rules()).errors,
      ['acme-corp/Gamma is already reviewed: it is in config/reviewer.json.']);
    assert.deepEqual(validateRepositoryDraft('ACME-CORP/minimoon', rules()).errors,
      ['acme-corp/MiniMoon is already reviewed: it was added on this dashboard.']);
    const full = {
      ...PAYLOAD,
      repositories: Array.from({ length: 50 }, (_, index) => ({ repo: `acme-corp/r${index}`, source: 'dashboard', agents: [] })),
    };
    assert.deepEqual(validateRepositoryDraft('acme-corp/one-more', rules(full)).errors,
      ['At most 50 repositories can be added on the dashboard; remove one first.']);
  });

  it('enables Add only for a valid draft while nothing is saving', () => {
    assert.equal(repositoryFormState('acme-corp/MiniMoonBo', rules()).canAdd, true);
    assert.equal(repositoryFormState('acme-corp/MiniMoonBo', rules(), { busy: true }).canAdd, false);
    assert.equal(repositoryFormState('acme-corp/', rules()).canAdd, false);
    assert.equal(repositoryFormState('acme-corp/MiniMoonBo ', rules()).repo, 'acme-corp/MiniMoonBo');
  });
});

describe('notices and refusals', () => {
  it('says what happens next, including the baseline rule', () => {
    assert.equal(addedNotice('acme-corp/MiniMoon'),
      `Added acme-corp/MiniMoon. The service starts polling it on its next cycle. ${BASELINE_NOTE}`);
    assert.match(BASELINE_NOTE, /already open when a repository is first added are not reviewed; their next push is\. A repository added back resumes from its earlier record\.$/);
    assert.match(removedNotice('acme-corp/MiniMoon'), /^Removed acme-corp\/MiniMoon\. The service stops polling it/);
  });

  it("leads with the server's reason instead of the generic 409 wording", () => {
    const reason = 'The review account cannot see acme-corp/dashboard on GitHub (gh: Not Found (HTTP 404)). Give it read access to the repository, then add it again.';
    const added = repositoryActionError(describeApiError({ status: 400, body: { error: reason } }), 'add');
    assert.deepEqual([added.title, added.detail, added.recovery], ['The repository was not added', reason, '']);

    const config = 'acme-corp/Gamma is configured in config/reviewer.json; remove it there.';
    const removed = repositoryActionError(describeApiError({ status: 409, body: { error: config } }), 'remove');
    assert.equal(removed.title, 'The repository was not removed');
    assert.equal(removed.detail, config);
    assert.doesNotMatch(`${removed.title} ${removed.recovery}`, /pull request/i);

    const slow = repositoryActionError(describeApiError({ status: 503, body: { error: 'GitHub did not answer within 20 seconds while checking acme-corp/x; try again.' } }), 'add');
    assert.equal(slow.detail, 'GitHub did not answer within 20 seconds while checking acme-corp/x; try again.');
    assert.equal(repositoryActionError(describeApiError({ status: 500, body: null }), 'add').recovery,
      'Reload the list to see what is in effect, then try again.');

    const forbidden = describeApiError({ status: 403, body: { error: 'admin role required' } });
    assert.equal(repositoryActionError(forbidden, 'add'), forbidden, 'permission failures keep the shared wording');
    assert.equal(repositoryActionError(null, 'add'), null);
  });
});
