/** Review repositories on the Settings page: pure projection, the add form's
 * draft and validation, and how a refused add or remove is explained.
 *
 * Mirrors the server's rules (service.validate_repository_name and the add
 * route): `owner/name`, an owner the config already reviews (compared
 * case-insensitively), not already on the list, at most `max_added` dashboard
 * repositories. The server stays the authority - it also asks GitHub whether
 * the review account can read the repository - and its reason is shown verbatim.
 */

import { relativeTime } from './format.js';

export const MAX_ADDED = 50;
export const BASELINE_NOTE = 'Pull requests already open when a repository is first added are not reviewed; their next push is. '
  + 'A repository added back resumes from its earlier record.';
const NAME_PATTERN = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\/[A-Za-z0-9._-]{1,100}$/;
const FORMAT_ERROR = 'Write the repository as owner/name: letters, digits and "-" in the owner; letters, digits, ".", "-" and "_" in the name.';

function text(value, max) {
  if (typeof value !== 'string') return null;
  const cleaned = value.replace(/\s+/g, ' ').trim();
  if (cleaned === '') return null;
  return cleaned.length > max ? cleaned.slice(0, max) : cleaned;
}

function sameName(a, b) {
  return a.toLowerCase() === b.toLowerCase();
}

/** One GET entry -> exactly the fields the page renders; a malformed name is dropped. */
export function projectRepositoryRow(row) {
  if (!row || typeof row !== 'object' || typeof row.repo !== 'string' || !NAME_PATTERN.test(row.repo)) return null;
  return {
    repo: row.repo,
    source: row.source === 'dashboard' ? 'dashboard' : 'config',
    agents: Array.isArray(row.agents) ? row.agents.map((agent) => text(agent, 64)).filter(Boolean).slice(0, 32) : [],
    added_by: text(row.added_by, 254),
    added_at: text(row.added_at, 64),
    // Why the service skips it (too few agents for the quorum), if it does.
    problem: text(row.problem, 300),
  };
}

export function describeRepositoryProblem(row) {
  return row && row.problem ? `Not reviewed: ${row.problem}` : null;
}

/** GET/POST/DELETE /api/settings/repositories -> {repositories, allowed_owners, max_added, note, error}. */
export function projectRepositorySettings(payload) {
  const source = payload && typeof payload === 'object' ? payload : {};
  return {
    repositories: (Array.isArray(source.repositories) ? source.repositories : []).map(projectRepositoryRow).filter(Boolean),
    allowed_owners: (Array.isArray(source.allowed_owners) ? source.allowed_owners : []).map((owner) => text(owner, 39)).filter(Boolean),
    max_added: Number.isInteger(source.max_added) && source.max_added > 0 ? source.max_added : MAX_ADDED,
    note: text(source.note, 300) || BASELINE_NOTE,
    error: text(source.error, 300),
  };
}

/** With a single allowed owner the input starts as `owner/`, ready for the name. */
export function initialRepositoryDraft(allowedOwners) {
  return Array.isArray(allowedOwners) && allowedOwners.length === 1 ? `${allowedOwners[0]}/` : '';
}

/** After a refresh: keep what the admin typed, otherwise follow the owners the server reports. */
export function nextRepositoryDraft(draft, touched, allowedOwners) {
  return touched ? draft : initialRepositoryDraft(allowedOwners);
}

export function addedCount(rows) {
  return (rows || []).filter((row) => row.source === 'dashboard').length;
}

/** The server's checks, so a typo is caught before GitHub is asked. A draft with
 *  no name yet ("", "acme-corp/") is not ready, which is not an error. */
export function validateRepositoryDraft(draft, { rows = [], allowedOwners = [], maxAdded = MAX_ADDED } = {}) {
  const repo = typeof draft === 'string' ? draft.trim() : '';
  const slash = repo.indexOf('/');
  if (slash < 0 || slash === repo.length - 1) return { ok: false, repo, errors: [] };
  const owner = repo.slice(0, slash);
  const name = repo.slice(slash + 1);
  const errors = [];
  if (!NAME_PATTERN.test(repo) || name === '.' || name === '..') {
    errors.push(FORMAT_ERROR);
  } else if (!allowedOwners.some((allowed) => sameName(allowed, owner))) {
    errors.push(`${owner} is not an owner this service reviews (allowed: ${allowedOwners.join(', ') || 'none'}).`);
  } else {
    const existing = rows.find((row) => sameName(row.repo, repo));
    if (existing) {
      errors.push(existing.source === 'config'
        ? `${existing.repo} is already reviewed: it is in config/reviewer.json.`
        : `${existing.repo} is already reviewed: it was added on this dashboard.`);
    } else if (addedCount(rows) >= maxAdded) {
      errors.push(`At most ${maxAdded} repositories can be added on the dashboard; remove one first.`);
    }
  }
  return { ok: errors.length === 0, repo, errors };
}

/** What the add controls show for a draft; also used to patch them while typing. */
export function repositoryFormState(draft, rules, { busy = false } = {}) {
  const validation = validateRepositoryDraft(draft, rules);
  return { canAdd: validation.ok && !busy, errors: validation.errors, repo: validation.repo };
}

export function describeRepositorySource(row, now = Date.now()) {
  if (!row || row.source !== 'dashboard') return 'from config/reviewer.json';
  return `added on this dashboard${row.added_by ? ` by ${row.added_by}` : ''}`
    + `${row.added_at ? `, ${relativeTime(row.added_at, now)}` : ''}`;
}

export function describeRepositoryAgents(row) {
  return row && row.agents.length > 0 ? `Reviewed by: ${row.agents.join(', ')}` : 'No reviewer agent runs on it';
}

export function addedNotice(repo, note = BASELINE_NOTE) {
  return `Added ${repo}. The service starts polling it on its next cycle. ${note}`;
}

export function removedNotice(repo) {
  return `Removed ${repo}. The service stops polling it from its next cycle; a review already running finishes.`;
}

/** A refused add or remove leads with the server's own sentence: errors.js
 *  titles every 409 "The pull request moved", which is wrong here. Session,
 *  permission and network failures keep their shared wording. */
export function repositoryActionError(described, action) {
  if (!described) return null;
  if (described.status === 0 || described.status === 401 || described.status === 403) return described;
  return {
    ...described,
    title: action === 'remove' ? 'The repository was not removed' : 'The repository was not added',
    recovery: described.status >= 500 && !described.detail ? 'Reload the list to see what is in effect, then try again.' : '',
  };
}
