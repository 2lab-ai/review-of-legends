/** PR authors and their feedback on the Settings page: pure projections and the
 * read-only rules.
 *
 * Authors set their own review language by commenting `@review-of-legends lang
 * <code>` on a pull request, and send feedback with `@review-of-legends <text>`;
 * the review service reads those comments and the server only reports them
 * (GET /api/authors, GET /api/feedback). Nothing on this page changes them, for
 * the admin either. Feedback text is author-written: it is shown as text, and a
 * comment link is rendered only when it points at github.com.
 */

import { relativeTime } from './format.js';

export const READ_ONLY_NOTE = 'Read-only, for every account: authors change their own settings by commenting on '
  + 'a pull request, e.g. "@review-of-legends lang ko" or "@review-of-legends lang off". The review service reads '
  + 'those comments and answers on GitHub.';
export const FEEDBACK_NOTE = 'What authors wrote to the maintainers with "@review-of-legends <text>" on a pull '
  + 'request, newest first. Shown as text, as written.';
export const GITHUB_PREFIX = 'https://github.com/';
export const MAX_FEEDBACK_TEXT = 4000;
/** GET /api/authors pages (200 rows each) the section reads at most. */
export const MAX_AUTHOR_PAGES = 20;
/** Pages of new feedback a live refresh reads to catch up before it starts over from the newest. */
export const MAX_REFRESH_PAGES = 10;
export const FEEDBACK_RESET_NOTE = `More feedback arrived at once than a refresh catches up on (${MAX_REFRESH_PAGES} `
  + 'pages), so the list started again from the newest; "Show older feedback" reads further back.';
/** The event kinds that mean this page's data changed (dashboard_store.py). */
export const AUTHOR_EVENT_KINDS = ['author_updated', 'feedback_added'];

const LANGUAGE_NAMES = {
  ko: 'Korean',
  en: 'English',
  ja: 'Japanese',
  zh: 'Chinese',
  'zh-tw': 'Traditional Chinese',
  es: 'Spanish',
  fr: 'French',
  de: 'German',
  vi: 'Vietnamese',
  th: 'Thai',
  id: 'Indonesian',
};
const LOGIN_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/;
const LANGUAGE_PATTERN = /^[a-z]{2,3}(?:-[a-z0-9]{2,8})?$/;
const REPO_PATTERN = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\/[A-Za-z0-9._-]{1,100}$/;

function text(value, max) {
  if (typeof value !== 'string') return null;
  const cleaned = value.replace(/\s+/g, ' ').trim();
  if (cleaned === '') return null;
  return cleaned.length > max ? cleaned.slice(0, max) : cleaned;
}

function positiveInt(value) {
  return Number.isInteger(value) && value > 0 ? value : null;
}

function nextOffset(value) {
  return Number.isInteger(value) && value >= 0 ? value : null;
}

/** A github.com link as given, or null: anything else stays inert text. */
export function feedbackLink(row) {
  const url = row && typeof row.comment_url === 'string' ? row.comment_url : '';
  if (!url.startsWith(GITHUB_PREFIX) || url.length > 512 || /[\s"'<>]/.test(url)) return null;
  try {
    return new URL(url).origin === 'https://github.com' ? url : null;
  } catch {
    return null;
  }
}

/** One GET /api/authors entry -> the fields the table renders; a malformed login drops the row. */
export function projectAuthorRow(row) {
  if (!row || typeof row !== 'object' || typeof row.login !== 'string' || !LOGIN_PATTERN.test(row.login)) return null;
  return {
    login: row.login,
    lang: typeof row.lang === 'string' && LANGUAGE_PATTERN.test(row.lang) ? row.lang : null,
    lang_set_at: text(row.lang_set_at, 64),
    welcomed_at: text(row.welcomed_at, 64),
  };
}

export function projectAuthors(payload) {
  const source = payload && typeof payload === 'object' ? payload : {};
  return {
    authors: (Array.isArray(source.authors) ? source.authors : []).map(projectAuthorRow).filter(Boolean),
    next_offset: nextOffset(source.next_offset),
  };
}

/** One GET /api/feedback entry. The text keeps its line breaks (it is rendered as text). */
export function projectFeedbackRow(row) {
  if (!row || typeof row !== 'object' || typeof row.login !== 'string' || !LOGIN_PATTERN.test(row.login)) return null;
  // Code points, as the server counts them: a UTF-16 slice could split an emoji.
  const body = typeof row.text === 'string' ? Array.from(row.text).slice(0, MAX_FEEDBACK_TEXT).join('') : '';
  return {
    id: positiveInt(row.id),
    login: row.login,
    repo: typeof row.repo === 'string' && REPO_PATTERN.test(row.repo) ? row.repo : null,
    pr: positiveInt(row.pr),
    comment_url: feedbackLink(row),
    text: body,
    created_at: text(row.created_at, 64),
  };
}

/** One GET /api/feedback page; `has_more` says there are rows past it, in the direction asked. */
export function projectFeedback(payload) {
  const source = payload && typeof payload === 'object' ? payload : {};
  return {
    feedback: (Array.isArray(source.feedback) ? source.feedback : []).map(projectFeedbackRow).filter(Boolean),
    has_more: source.has_more === true,
  };
}

/** Rows from any page folded into what is shown: newest first, each id once. */
export function mergeFeedback(current, page) {
  const seen = new Set();
  const merged = [];
  for (const row of [...(current || []), ...(page || [])]) {
    const key = row.id === null ? null : row.id;
    if (key !== null && seen.has(key)) continue;
    if (key !== null) seen.add(key);
    merged.push(row);
  }
  return merged.sort((a, b) => (b.id || 0) - (a.id || 0));
}

function feedbackIds(rows) {
  return (rows || []).map((row) => row.id).filter((id) => id !== null);
}

/* Feedback is paged by row id, never by offset. Ids only grow, so "below the
 * oldest row shown" and "above the newest" name the same rows however much
 * feedback arrives between two pages, and the loads below can run in any order
 * without skipping or repeating a row. Each takes what is shown ({ feedback,
 * hasOlder, feedbackReset }) and `fetchPage(keys)`, which returns the raw GET
 * /api/feedback payload for {}, { beforeId } or { afterId }; each returns what
 * to show next, in the same shape. */

/** The newest page, replacing what is shown (the first load). */
export async function loadNewestFeedback(fetchPage) {
  const page = projectFeedback(await fetchPage({}));
  return { feedback: mergeFeedback([], page.feedback), hasOlder: page.has_more, feedbackReset: false };
}

/** "Show older feedback": the page below the oldest row shown. It also clears
 * the note a reset left, since this is the way back to what the reset dropped. */
export async function loadOlderFeedback(shown, fetchPage) {
  const ids = feedbackIds(shown.feedback);
  if (ids.length === 0) return loadNewestFeedback(fetchPage);
  const page = projectFeedback(await fetchPage({ beforeId: Math.min(...ids) }));
  return { feedback: mergeFeedback(shown.feedback, page.feedback), hasOlder: page.has_more, feedbackReset: false };
}

/** A live refresh (an event, a resync, Reload): the rows above the newest one
 * shown, following `has_more` for at most `maxPages` pages, so the older pages
 * already loaded stay. One that cannot catch up within that, or a page that
 * does not move past the last, starts over from the newest page and says so
 * (`feedbackReset`). */
export async function refreshFeedback(shown, fetchPage, maxPages = MAX_REFRESH_PAGES) {
  const ids = feedbackIds(shown.feedback);
  if (ids.length === 0) return loadNewestFeedback(fetchPage);
  let feedback = shown.feedback;
  let after = Math.max(...ids);
  for (let read = 0; read < maxPages; read += 1) {
    const page = projectFeedback(await fetchPage({ afterId: after }));
    feedback = mergeFeedback(feedback, page.feedback);
    if (!page.has_more) return { feedback, hasOlder: shown.hasOlder === true, feedbackReset: shown.feedbackReset === true };
    const top = Math.max(0, ...feedbackIds(page.feedback));
    if (top <= after) break;
    after = top;
  }
  return { ...(await loadNewestFeedback(fetchPage)), feedbackReset: true };
}

/** Every GET /api/authors page, following next_offset, at most `maxPages`;
 * `capped` when the cap left pages unread. `fetchPage(offset)` returns the raw
 * payload. A row that moved up between two pages is listed once. */
export async function loadAllAuthors(fetchPage, maxPages = MAX_AUTHOR_PAGES) {
  const authors = [];
  const seen = new Set();
  let offset = 0;
  for (let read = 0; read < maxPages; read += 1) {
    const page = projectAuthors(await fetchPage(offset));
    for (const row of page.authors) {
      const key = row.login.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      authors.push(row);
    }
    // An offset that does not move would read the same page forever.
    if (page.next_offset === null || page.next_offset <= offset) return { authors, capped: false };
    offset = page.next_offset;
  }
  return { authors, capped: true };
}

export function authorsCappedNote(count) {
  return `Showing the first ${count} authors, the most recently changed; the rest are not listed here.`;
}

/** This section's loads, one at a time. A refresh asked for while another load
 * (an older feedback page, say) is in flight is kept and due when it ends: the
 * coalescer that asked has already returned and will not ask again. An
 * older-page request during a load is dropped; its button asks again. */
/** Whether an author or feedback event reloads the Settings lists: once they
 *  were asked for at all. Loaded; loading (the gate queues it, since what the
 *  event announces may postdate that load's reads); or failed, so an event
 *  whose refresh comes due after a failed first load still reads it. */
export function authorsRefreshDue(model) {
  return !!model && !!(model.loaded || model.loading || model.error);
}

/** The Settings section's author and feedback loads, one at a time.
 *
 *  `read(older, model)` resolves to the fields to assign (older: the feedback
 *  page below the oldest row shown). A refresh asked for during a load runs
 *  right after it, also after a load that failed: it may be the one read of
 *  what an event announced. `getModel` is read on every call, so a model the
 *  app replaces (a sign-out) is never written by an older load's closure. */
export function createAuthorLoader({ getModel, read, render, onError }) {
  const gate = createLoadGate();
  async function load({ more = false } = {}) {
    if (!gate.begin(!more)) return;
    const model = getModel();
    model.loading = true;
    model.error = null;
    render();
    try {
      Object.assign(model, await read(more && model.hasOlder, model));
      model.loaded = true;
    } catch (error) {
      onError(error, model);
    } finally {
      model.loading = false;
      render();
      if (gate.end()) load();
    }
  }
  return load;
}

export function createLoadGate() {
  let busy = false;
  let pending = false;
  return {
    /** True: start now. False: a load is in flight (a refresh is kept for after it). */
    begin(refresh) {
      if (busy) {
        if (refresh) pending = true;
        return false;
      }
      busy = true;
      return true;
    },
    /** The load ended; true when a refresh is due (the caller starts it). */
    end() {
      busy = false;
      const due = pending;
      pending = false;
      return due;
    },
  };
}

export function describeLanguage(lang) {
  if (!lang) return 'Service default';
  return LANGUAGE_NAMES[lang] ? `${lang} (${LANGUAGE_NAMES[lang]})` : lang;
}

export function describeWelcome(row, now = Date.now()) {
  return row && row.welcomed_at ? `Welcomed ${relativeTime(row.welcomed_at, now)}` : 'Not welcomed yet';
}

/** Where a piece of feedback came from: "Alice on owner/repo#12". */
export function describeFeedbackSource(row) {
  if (!row) return '';
  const place = row.repo ? ` on ${row.repo}${row.pr ? `#${row.pr}` : ''}` : '';
  return `${row.login}${place}`;
}

export function isAuthorEvent(event) {
  return !!event && AUTHOR_EVENT_KINDS.includes(event.kind);
}
