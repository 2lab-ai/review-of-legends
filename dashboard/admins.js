/** Admins on the Settings page: pure projection, the add form's validation, and
 * which controls a session gets.
 *
 * Mirrors the server (dashboard_auth.normalize_admin_email and the
 * /api/settings/admins routes): an address is stripped and lowercased, at most
 * 254 characters, exactly one "@" with text on both sides, no whitespace,
 * control, format or separator character, and ASCII only. The owner is always admin and is never
 * added; an address on the list is not added twice; at most `max_added` entries.
 * Only the owner adds or removes - the server says so per session (`can_edit`),
 * stays the authority, and its reason is shown verbatim.
 */

import { relativeTime } from './format.js';

export const MAX_ADMINS = 50;
export const MAX_EMAIL_CHARS = 254;
export const OWNER_NOTE = 'Owner — always admin';
export const OWNER_REQUIRED = 'owner role required';
// Python's str.isspace() plus unicodedata categories C* and Z*, as on the server.
const INVISIBLE = /[\s\p{C}\p{Z}]/u;
// Checked before lowercasing, as on the server: toLowerCase() folds the Kelvin
// sign into "k", and a Cyrillic "е" lists exactly like a Latin "e".
const NON_ASCII = /[^\x00-\x7F]/;
const SHAPE_ERROR = 'Write one address with a single "@" and text on both sides, e.g. name@example.com.';

function text(value, max) {
  if (typeof value !== 'string') return null;
  const cleaned = value.replace(/\s+/g, ' ').trim();
  if (cleaned === '') return null;
  return cleaned.length > max ? cleaned.slice(0, max) : cleaned;
}

/** The stored form of an address, or the reason it is refused. Lengths count code
 *  points, as Python does, not UTF-16 units. */
export function normalizeAdminEmail(value) {
  if (typeof value !== 'string' || value.trim() === '') {
    return { email: '', error: 'Write an email address, e.g. name@example.com.' };
  }
  const email = value.trim().toLowerCase();
  if ([...email].length > MAX_EMAIL_CHARS) {
    return { email, error: `An email address is at most ${MAX_EMAIL_CHARS} characters.` };
  }
  if (INVISIBLE.test(email)) {
    return { email, error: 'An email address cannot contain spaces or invisible characters.' };
  }
  if (NON_ASCII.test(value.trim())) {
    return { email, error: 'An email address can contain only ASCII letters, digits and symbols.' };
  }
  const at = email.indexOf('@');
  if (at <= 0 || at === email.length - 1 || email.indexOf('@', at + 1) !== -1) {
    return { email, error: SHAPE_ERROR };
  }
  return { email, error: null };
}

/** One GET entry -> exactly the fields the page renders; an address that is not
 *  in its stored form is dropped, like the server drops it from the grant list. */
export function projectAdminRow(row) {
  if (!row || typeof row !== 'object' || typeof row.email !== 'string') return null;
  const { email, error } = normalizeAdminEmail(row.email);
  if (error || email !== row.email) return null;
  return {
    email,
    added_by: text(row.added_by, MAX_EMAIL_CHARS),
    added_at: text(row.added_at, 64),
  };
}

/** GET/POST/DELETE /api/settings/admins -> {owner, admins, can_edit, max_added, error}. */
export function projectAdminSettings(payload) {
  const source = payload && typeof payload === 'object' ? payload : {};
  const owner = text(source.owner, MAX_EMAIL_CHARS);
  const seen = new Set();
  const admins = [];
  for (const row of Array.isArray(source.admins) ? source.admins : []) {
    const projected = projectAdminRow(row);
    if (!projected || projected.email === owner || seen.has(projected.email)) continue;
    seen.add(projected.email);
    admins.push(projected);
  }
  return {
    owner,
    admins,
    // Only an explicit true: a missing or odd value never shows the controls.
    can_edit: source.can_edit === true,
    max_added: Number.isInteger(source.max_added) && source.max_added > 0 ? source.max_added : MAX_ADMINS,
    error: text(source.error, 300),
  };
}

/** The add form's rules for a Settings model (app.js state.admins). */
export function adminRules(model) {
  return { owner: model.owner, admins: model.rows, maxAdded: model.maxAdded };
}

/** The server's checks, so a typo is caught before the request. A draft still
 *  being typed (no "@" yet, or nothing after it) is not ready, which is not an error. */
export function validateAdminDraft(draft, { owner = null, admins = [], maxAdded = MAX_ADMINS } = {}) {
  const raw = typeof draft === 'string' ? draft.trim() : '';
  if (raw === '' || !raw.includes('@') || raw.endsWith('@')) {
    return { ok: false, email: raw.toLowerCase(), errors: [] };
  }
  const { email, error } = normalizeAdminEmail(raw);
  const errors = [];
  if (error) errors.push(error);
  else if (owner && email === owner) errors.push(`${email} is the owner and is always admin.`);
  else if (admins.some((row) => row.email === email)) errors.push(`${email} is already an admin.`);
  else if (admins.length >= maxAdded) errors.push(`At most ${maxAdded} admins can be added; remove one first.`);
  return { ok: errors.length === 0, email, errors };
}

/** What the add controls show for a draft; also used to patch them while typing. */
export function adminFormState(draft, rules, { busy = false } = {}) {
  const validation = validateAdminDraft(draft, rules);
  return { canAdd: validation.ok && !busy, errors: validation.errors, email: validation.email };
}

/** Which controls a session gets. Independent of the admin role: an added admin
 *  edits repositories and the policy but sees this list read-only. */
export function adminControls(model) {
  const canEdit = !!model && model.canEdit === true;
  const owner = model && model.owner ? model.owner : 'the owner';
  return {
    showRemove: canEdit,
    showForm: canEdit && !!model.loaded,
    note: !canEdit && model && model.loaded ? `Read-only: only the owner (${owner}) can add or remove admins.` : null,
  };
}

export function describeAdminRow(row, now = Date.now()) {
  if (!row) return '';
  const parts = [];
  if (row.added_by) parts.push(`added by ${row.added_by}`);
  if (row.added_at) parts.push(relativeTime(row.added_at, now));
  return parts.length > 0 ? parts.join(', ') : 'added on this dashboard';
}

export function addedAdminNotice(email) {
  return `${email} is now an admin, from their next request; no new sign-in is needed.`;
}

export function removedAdminNotice(email) {
  return `${email} is no longer an admin. Their admin sessions end on their next request.`;
}

/** Orders the list's reads and add/remove answers so the page never keeps a list
 *  older than one it has already been told about.
 *
 *  - One read at a time. A refresh asked for while a read runs (the first load
 *    included) marks the list stale, and that read is followed by exactly one more.
 *  - A read that started before an add/remove answer was applied answers with an
 *    older list: it is not applied, and it reads again.
 *  - An add/remove answer is applied, then read once more: another owner tab may
 *    have changed the list after the write that answer reflects.
 *
 *  app.js calls startRead/canApplyRead/finishRead around each GET and
 *  applyMutation after each applied POST/DELETE answer. */
export function createAdminListSync() {
  let applied = 0;
  let readSince = null;
  let stale = false;
  return {
    /** True: send the GET now. False: one is in flight, and it will read again. */
    startRead() {
      if (readSince !== null) {
        stale = true;
        return false;
      }
      readSince = applied;
      return true;
    },
    /** The GET answered: true when no add/remove answer was applied since it started. */
    canApplyRead() {
      return readSince === applied;
    },
    /** The GET is over (answered or failed): true when it must run once more. */
    finishRead() {
      readSince = null;
      const again = stale;
      stale = false;
      return again;
    },
    /** An add/remove answer was applied: true when a GET should start now
     *  (with one in flight, that one reads again instead). */
    applyMutation() {
      applied += 1;
      if (readSince !== null) {
        stale = true;
        return false;
      }
      return true;
    },
  };
}

/** A setting event for the grant list: the role of this very session may have changed. */
export function isAdminsEvent(event) {
  return !!event
    && (event.kind === 'setting_updated' || event.kind === 'setting_deleted')
    && !!event.payload && event.payload.key === 'admins';
}

/** A refused add or remove leads with the server's own sentence (errors.js titles
 *  every 409 "The pull request moved"). An owner-only refusal gets its own title;
 *  session, other permission and network failures keep their shared wording. */
export function adminActionError(described, action) {
  if (!described) return null;
  if (described.status === 403 && String(described.detail || '').toLowerCase().includes(OWNER_REQUIRED)) {
    return {
      ...described,
      title: 'Owner only',
      recovery: 'Only the owner can add or remove admins; other admins see this list read-only.',
    };
  }
  if (described.status === 0 || described.status === 401 || described.status === 403) return described;
  return {
    ...described,
    title: action === 'remove' ? 'The admin was not removed' : 'The admin was not added',
    recovery: described.status >= 500 && !described.detail ? 'Reload the list to see who is admin, then try again.' : '',
  };
}
