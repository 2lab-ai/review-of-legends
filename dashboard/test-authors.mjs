/** PR authors and feedback on the Settings page: projections and the read-only rules. */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

import {
  AUTHOR_EVENT_KINDS,
  FEEDBACK_NOTE,
  FEEDBACK_RESET_NOTE,
  MAX_AUTHOR_PAGES,
  MAX_FEEDBACK_TEXT,
  MAX_REFRESH_PAGES,
  READ_ONLY_NOTE,
  authorsCappedNote,
  authorsRefreshDue,
  createAuthorLoader,
  createLoadGate,
  describeFeedbackSource,
  describeLanguage,
  describeWelcome,
  feedbackLink,
  isAuthorEvent,
  loadAllAuthors,
  loadNewestFeedback,
  loadOlderFeedback,
  mergeFeedback,
  projectAuthorRow,
  projectAuthors,
  projectFeedback,
  projectFeedbackRow,
  refreshFeedback,
} from './authors.js';
import { ApiClient } from './api.js';

const NOW = Date.parse('2026-10-15T12:00:00Z');

describe('projectAuthors', () => {
  it('keeps the contract fields and drops malformed rows', () => {
    const projected = projectAuthors({
      authors: [
        { login: 'Alice', lang: 'ko', lang_set_at: '2026-10-15T11:00:00+00:00', welcomed_at: null, extra: 'x' },
        { login: 'Bob', lang: null, lang_set_at: null, welcomed_at: '2026-10-14T00:00:00+00:00' },
        { login: 'dependabot[bot]', lang: 'en' },
        { login: '<img src=x>', lang: 'en' },
        { login: 'Carol', lang: 'KO; DROP' },
        'junk',
        null,
      ],
      next_offset: 200,
    });
    assert.deepEqual(projected.authors.map((row) => row.login), ['Alice', 'Bob', 'Carol']);
    assert.deepEqual(projected.authors[0], { login: 'Alice', lang: 'ko', lang_set_at: '2026-10-15T11:00:00+00:00', welcomed_at: null });
    assert.equal(projected.authors[2].lang, null, 'a language the server would not send is dropped');
    assert.equal(projected.next_offset, 200);
    assert.deepEqual(projectAuthors(null), { authors: [], next_offset: null });
    assert.equal(projectAuthorRow({ login: 7 }), null);
  });

  it('describes languages and welcomes', () => {
    assert.equal(describeLanguage('ko'), 'ko (Korean)');
    assert.equal(describeLanguage('zh-tw'), 'zh-tw (Traditional Chinese)');
    assert.equal(describeLanguage('pt-br'), 'pt-br');
    assert.equal(describeLanguage(null), 'Service default');
    assert.equal(describeWelcome({ welcomed_at: null }, NOW), 'Not welcomed yet');
    assert.match(describeWelcome({ welcomed_at: '2026-10-15T11:00:00Z' }, NOW), /^Welcomed /);
  });
});

describe('projectFeedback', () => {
  const ROW = {
    id: 9,
    login: 'Bob',
    repo: 'owner/repo',
    pr: 12,
    comment_url: 'https://github.com/owner/repo/pull/12#issuecomment-5',
    text: 'line one\nline two',
    created_at: '2026-10-15T11:00:00+00:00',
  };

  it('keeps the text as written, line breaks included, and bounded', () => {
    const [row] = projectFeedback({ feedback: [ROW], has_more: false }).feedback;
    assert.equal(row.text, 'line one\nline two');
    assert.equal(row.comment_url, ROW.comment_url);
    assert.equal(describeFeedbackSource(row), 'Bob on owner/repo#12');
    const long = projectFeedbackRow({ ...ROW, text: 'x'.repeat(9000) });
    assert.equal(long.text.length, MAX_FEEDBACK_TEXT);
    assert.equal(projectFeedbackRow({ ...ROW, text: 7 }).text, '');
  });

  it('drops bad numbers, repositories and logins', () => {
    const row = projectFeedbackRow({ ...ROW, id: '9', pr: -1, repo: '../../etc' });
    assert.deepEqual([row.id, row.pr, row.repo], [null, null, null]);
    assert.equal(describeFeedbackSource(row), 'Bob');
    assert.equal(projectFeedbackRow({ ...ROW, login: 'a b' }), null);
    assert.equal(projectFeedback({ feedback: 'nope' }).feedback.length, 0);
  });

  it('reads has_more as a boolean, and nothing else as more', () => {
    assert.equal(projectFeedback({ feedback: [], has_more: true }).has_more, true);
    for (const value of [false, 'true', 1, null, undefined]) {
      assert.equal(projectFeedback({ feedback: [], has_more: value }).has_more, false, String(value));
    }
    assert.deepEqual(projectFeedback(null), { feedback: [], has_more: false });
  });

  it('merges any page once per row, newest first', () => {
    const first = projectFeedback({ feedback: [{ ...ROW, id: 9 }, { ...ROW, id: 8 }] }).feedback;
    const older = projectFeedback({ feedback: [{ ...ROW, id: 8 }, { ...ROW, id: 7 }] }).feedback;
    assert.deepEqual(mergeFeedback(first, older).map((row) => row.id), [9, 8, 7]);
    // A page above (after_id answers oldest first) lands on top, in order.
    const above = projectFeedback({ feedback: [{ ...ROW, id: 10 }, { ...ROW, id: 11 }, { ...ROW, id: 9 }] }).feedback;
    assert.deepEqual(mergeFeedback(first, above).map((row) => row.id), [11, 10, 9, 8]);
  });
});

describe('feedback paging by row id while feedback arrives', () => {
  const ROW_TEMPLATE = { login: 'Bob', repo: 'owner/repo', pr: 1, text: 'x', created_at: '2026-10-15T11:00:00+00:00' };
  const ids = (shown) => shown.feedback.map((row) => row.id);
  const range = (top, bottom) => Array.from({ length: top - bottom + 1 }, (_, index) => top - index);

  /** GET /api/feedback over an in-memory table, as dashboard_web answers it:
   * newest first, below `beforeId`, or the rows right above `afterId` oldest first;
   * `has_more` when a row lies past the page in that direction. */
  function feedbackServer(initial, pageSize = 2) {
    const table = new Set(initial);
    const requests = [];
    return {
      requests,
      insert(...added) { for (const id of added) table.add(id); },
      fetchPage: async (keys) => {
        requests.push(keys);
        const all = [...table].sort((a, b) => a - b);
        const rows = keys.afterId
          ? all.filter((id) => id > keys.afterId)
          : all.filter((id) => !keys.beforeId || id < keys.beforeId).reverse();
        return {
          feedback: rows.slice(0, pageSize).map((id) => ({ ...ROW_TEMPLATE, id })),
          has_more: rows.length > pageSize,
        };
      },
    };
  }

  /** Every row from the newest shown down to the oldest shown, each once. */
  function assertNoGapNoRepeat(shown, label = '') {
    const list = ids(shown);
    assert.equal(new Set(list).size, list.length, `a row is listed twice ${label}`);
    if (list.length > 0) assert.deepEqual(list, range(list[0], list[list.length - 1]), `a row is skipped ${label}`);
  }

  it("the review's sequence: an older page and a queued refresh around a new row skip nothing (page size 2)", async () => {
    const server = feedbackServer(range(9, 1));
    let shown = await loadNewestFeedback(server.fetchPage);
    assert.deepEqual([ids(shown), shown.hasOlder], [[9, 8], true]);
    // Row 10 arrives before the older-page request is answered: below 8 is still 7 and 6.
    server.insert(10);
    shown = await loadOlderFeedback(shown, server.fetchPage);
    assert.deepEqual([ids(shown), shown.hasOlder], [[9, 8, 7, 6], true]);
    // The refresh queued during that load reads above 9; the older pages stay.
    shown = await refreshFeedback(shown, server.fetchPage);
    assert.deepEqual([ids(shown), shown.hasOlder, shown.feedbackReset], [[10, 9, 8, 7, 6], true, false]);
    // The next older request starts below 6: 6 is shown once and nothing under it is skipped.
    shown = await loadOlderFeedback(shown, server.fetchPage);
    assert.deepEqual(ids(shown), [10, 9, 8, 7, 6, 5, 4]);
    assert.deepEqual(server.requests, [{}, { beforeId: 8 }, { afterId: 9 }, { beforeId: 6 }]);
    assertNoGapNoRepeat(shown);
    while (shown.hasOlder) shown = await loadOlderFeedback(shown, server.fetchPage);
    assert.deepEqual(ids(shown), range(10, 1), 'paging to the end lists every row once');
  });

  it('any order of new rows, older pages and refreshes lists every row once, without a gap', async () => {
    // Every sequence of six steps over {a row arrives, older page, refresh}, then a final refresh.
    const steps = ['insert', 'older', 'refresh'];
    for (let code = 0; code < steps.length ** 6; code += 1) {
      const plan = Array.from({ length: 6 }, (_, index) => steps[Math.floor(code / steps.length ** index) % steps.length]);
      const server = feedbackServer(range(9, 1));
      let next = 10;
      let shown = await loadNewestFeedback(server.fetchPage);
      for (const step of plan) {
        if (step === 'insert') server.insert(next++);
        else if (step === 'older') shown = await loadOlderFeedback(shown, server.fetchPage);
        else shown = await refreshFeedback(shown, server.fetchPage);
        assertNoGapNoRepeat(shown, plan.join(','));
      }
      shown = await refreshFeedback(shown, server.fetchPage);
      assertNoGapNoRepeat(shown, plan.join(','));
      assert.equal(ids(shown)[0], next - 1, `the refresh reached the newest row: ${plan.join(',')}`);
    }
  });

  it('a refresh follows has_more page by page, oldest first above the newest row shown', async () => {
    const server = feedbackServer(range(9, 1));
    let shown = await loadNewestFeedback(server.fetchPage);
    server.insert(...range(15, 10));
    shown = await refreshFeedback(shown, server.fetchPage);
    assert.deepEqual(ids(shown), range(15, 8));
    assert.deepEqual(server.requests.slice(1), [{ afterId: 9 }, { afterId: 11 }, { afterId: 13 }]);
    // Nothing new: one request, nothing changes.
    const again = await refreshFeedback(shown, server.fetchPage);
    assert.deepEqual([ids(again), again.hasOlder, again.feedbackReset], [ids(shown), true, false]);
    assert.deepEqual(server.requests.at(-1), { afterId: 15 });
  });

  it(`a refresh that cannot catch up in ${MAX_REFRESH_PAGES} pages starts over from the newest and says so`, async () => {
    assert.equal(MAX_REFRESH_PAGES, 10);
    const server = feedbackServer(range(9, 1));
    let shown = await loadNewestFeedback(server.fetchPage);
    shown = await loadOlderFeedback(shown, server.fetchPage);
    server.insert(...range(40, 10)); // 31 new rows: 16 pages of 2
    shown = await refreshFeedback(shown, server.fetchPage);
    assert.deepEqual([ids(shown), shown.hasOlder, shown.feedbackReset], [[40, 39], true, true]);
    assert.equal(server.requests.length, 2 + MAX_REFRESH_PAGES + 1, 'the bound, then the newest page');
    assert.deepEqual(server.requests.at(-1), {});
    assert.match(FEEDBACK_RESET_NOTE, /started again from the newest/);
    // The note stays through a refresh that catches up, and goes with the next older page.
    server.insert(41);
    shown = await refreshFeedback(shown, server.fetchPage);
    assert.deepEqual([ids(shown), shown.feedbackReset], [[41, 40, 39], true]);
    shown = await loadOlderFeedback(shown, server.fetchPage);
    assert.deepEqual([ids(shown), shown.feedbackReset], [[41, 40, 39, 38, 37], false]);
    assertNoGapNoRepeat(shown);
  });

  it('a page that does not move past the last one starts over instead of asking again', async () => {
    const requests = [];
    const stuck = async (keys) => {
      requests.push(keys);
      return keys.afterId ? { feedback: [{ ...ROW_TEMPLATE, id: 9 }], has_more: true } : { feedback: [{ ...ROW_TEMPLATE, id: 12 }], has_more: false };
    };
    const shown = await refreshFeedback({ feedback: [{ ...ROW_TEMPLATE, id: 9 }], hasOlder: false, feedbackReset: false }, stuck);
    assert.deepEqual(requests, [{ afterId: 9 }, {}]);
    assert.deepEqual([ids(shown), shown.feedbackReset], [[12], true]);
  });

  it('with nothing shown, a refresh and an older page both read the newest page', async () => {
    const server = feedbackServer([3, 2, 1]);
    const empty = { feedback: [], hasOlder: false, feedbackReset: false };
    assert.deepEqual(ids(await refreshFeedback(empty, server.fetchPage)), [3, 2]);
    assert.deepEqual(ids(await loadOlderFeedback(empty, server.fetchPage)), [3, 2]);
    assert.deepEqual(server.requests, [{}, {}]);
  });
});

describe('loadAllAuthors', () => {
  const author = (index) => ({ login: `user${index}`, lang: 'ko', lang_set_at: null, welcomed_at: null });

  it('follows next_offset until the last page', async () => {
    const offsets = [];
    const loaded = await loadAllAuthors(async (offset) => {
      offsets.push(offset);
      if (offset === 0) return { authors: [author(1), author(2)], next_offset: 2 };
      // A row that moved up while paging comes back: listed once.
      return { authors: [author(2), author(3)], next_offset: null };
    });
    assert.deepEqual(offsets, [0, 2]);
    assert.deepEqual(loaded.authors.map((row) => row.login), ['user1', 'user2', 'user3']);
    assert.equal(loaded.capped, false);
  });

  it('stops at MAX_AUTHOR_PAGES and says so', async () => {
    let calls = 0;
    const loaded = await loadAllAuthors(async (offset) => {
      calls += 1;
      return { authors: [author(offset)], next_offset: offset + 1 };
    });
    assert.equal(MAX_AUTHOR_PAGES, 20);
    assert.equal(calls, MAX_AUTHOR_PAGES);
    assert.equal(loaded.authors.length, MAX_AUTHOR_PAGES);
    assert.equal(loaded.capped, true);
    assert.match(authorsCappedNote(loaded.authors.length), /first 20 authors/);
  });

  it('never loops on an offset that does not move', async () => {
    let calls = 0;
    const loaded = await loadAllAuthors(async () => {
      calls += 1;
      return { authors: [author(1)], next_offset: 0 };
    });
    assert.equal(calls, 1);
    assert.equal(loaded.capped, false);
  });
});

describe('authorsRefreshDue', () => {
  it('asks for a refresh while the first load runs, so its event is never dropped', () => {
    assert.equal(authorsRefreshDue({ loaded: false, loading: true }), true, 'queued behind the first load');
    assert.equal(authorsRefreshDue({ loaded: true, loading: false }), true);
    assert.equal(authorsRefreshDue({ loaded: false, loading: false, error: null }), false, 'never opened: nothing to refresh');
    assert.equal(authorsRefreshDue({ loaded: false, loading: false, error: { message: 'x' } }), true,
      'a first load that failed: the event still reads what it announced');
    assert.equal(authorsRefreshDue(null), false);
  });

  it('is the loader app.js uses', () => {
    const app = readFileSync(new URL('./app.js', import.meta.url), 'utf8');
    assert.match(app, /const loadAuthorSettings = createAuthorLoader\(\{/);
  });

  it('is what app.js asks before an author event reloads the page', () => {
    const app = readFileSync(new URL('./app.js', import.meta.url), 'utf8');
    assert.match(app, /authors: createCoalescer\(\(\) => \(authorsRefreshDue\(state\.authors\) \? loadAuthorSettings\(\) : null\)\)/);
  });
});

describe('projectFeedbackRow text bound', () => {
  it('cuts by code points, as the server does, never inside a character', () => {
    const text = `${'a'.repeat(MAX_FEEDBACK_TEXT - 1)}😀`;
    assert.equal(projectFeedbackRow({ id: 1, login: 'alice', text }).text, text, 'the server\'s 4,000 characters fit');
    const over = `${'😀'.repeat(MAX_FEEDBACK_TEXT + 1)}`;
    assert.equal(Array.from(projectFeedbackRow({ id: 1, login: 'alice', text: over }).text).length, MAX_FEEDBACK_TEXT);
  });
});

describe('createLoadGate', () => {
  it('runs a refresh asked for during an older-page load after it, not never', () => {
    const gate = createLoadGate();
    assert.equal(gate.begin(false), true, 'an older page starts');
    assert.equal(gate.begin(true), false, 'a refresh waits');
    assert.equal(gate.begin(true), false, 'a second one folds into it');
    assert.equal(gate.end(), true, 'the refresh is due now');
    assert.equal(gate.begin(true), true);
    assert.equal(gate.end(), false, 'and only once');
  });

  it('drops an older-page request during a load: the button asks again', () => {
    const gate = createLoadGate();
    gate.begin(true);
    assert.equal(gate.begin(false), false);
    assert.equal(gate.end(), false);
  });
});

describe('read-only rendering rules', () => {
  it('links only to github.com; everything else stays inert text', () => {
    assert.equal(feedbackLink({ comment_url: 'https://github.com/o/r/pull/1#issuecomment-2' }), 'https://github.com/o/r/pull/1#issuecomment-2');
    for (const url of [
      'javascript:alert(1)',
      'https://github.com.evil.example/o/r',
      'https://evil.example/https://github.com/',
      'http://github.com/o/r',
      'https://github.com/o r',
      'https://github.com/"onmouseover=x',
      null,
      42,
    ]) {
      assert.equal(feedbackLink({ comment_url: url }), null, String(url));
    }
    assert.equal(projectFeedbackRow({ login: 'a', comment_url: 'javascript:alert(1)' }).comment_url, null);
  });

  it('says on the page that changes happen by commenting on GitHub', () => {
    assert.match(READ_ONLY_NOTE, /commenting on a pull request/);
    assert.match(READ_ONLY_NOTE, /@review-of-legends lang ko/);
    assert.match(FEEDBACK_NOTE, /shown as text/i);
  });

  it('says when the author list stopped at its page cap', () => {
    const source = readFileSync(new URL('./view-settings.js', import.meta.url), 'utf8');
    assert.match(source, /model\.authorsCapped/);
    assert.match(source, /authorsCappedNote\(/);
  });

  it('pages older feedback by hasOlder and says when a refresh started over', () => {
    const source = readFileSync(new URL('./view-settings.js', import.meta.url), 'utf8');
    assert.match(source, /model\.hasOlder && model\.loaded/);
    assert.match(source, /model\.feedbackReset\) body\.push\(el\('p', \{[^}]*text: FEEDBACK_RESET_NOTE/);
    assert.equal(source.includes('feedbackNext'), false, 'no offset is left in the section');
  });

  it('the section renders no form, input or mutation control, and text never as markup', () => {
    const source = readFileSync(new URL('./view-settings.js', import.meta.url), 'utf8');
    const start = source.indexOf('function authorRow(');
    const end = source.indexOf('export function renderSettings(');
    assert.ok(start > 0 && end > start, 'the authors section is where this test looks for it');
    const section = source.slice(start, end);
    for (const forbidden of ["el('form'", "el('input'", "el('textarea'", "el('select'", 'innerHTML', 'onSave', 'onRemove', 'csrf']) {
      assert.equal(section.includes(forbidden), false, forbidden);
    }
    // The one button pages older feedback; the one link is the checked comment link.
    assert.equal(section.split("el('button'").length - 1, 1);
    assert.match(section, /id: 'feedback-more'/);
    assert.match(section, /href: link/);
    assert.match(section, /text: row\.text/);
  });

  it('the API client only reads these lists', async () => {
    const calls = [];
    const api = new ApiClient({
      fetchImpl: async (url, options) => {
        calls.push([options.method, url]);
        return { status: 200, ok: true, text: async () => '{}' };
      },
    });
    await api.authors();
    await api.authors({ offset: 400 });
    await api.feedback();
    await api.feedback({ limit: 5000, beforeId: 8 });
    await api.feedback({ afterId: 9 });
    await api.feedback({ beforeId: 0, afterId: -1 });
    await api.feedback({ beforeId: '8', afterId: 1.5 });
    assert.deepEqual(calls, [
      ['GET', '/api/authors?limit=200'],
      ['GET', '/api/authors?limit=200&offset=400'],
      ['GET', '/api/feedback?limit=50'],
      ['GET', '/api/feedback?limit=200&before_id=8'],
      ['GET', '/api/feedback?limit=50&after_id=9'],
      ['GET', '/api/feedback?limit=50'],
      ['GET', '/api/feedback?limit=50'],
    ]);
  });
});

describe('isAuthorEvent', () => {
  it('matches the store events that change this section', () => {
    assert.deepEqual(AUTHOR_EVENT_KINDS, ['author_updated', 'feedback_added']);
    assert.equal(isAuthorEvent({ kind: 'author_updated' }), true);
    assert.equal(isAuthorEvent({ kind: 'feedback_added' }), true);
    assert.equal(isAuthorEvent({ kind: 'setting_updated' }), false);
    assert.equal(isAuthorEvent(null), false);
  });
});

describe('feedback rendering', () => {
  /** A DOM just big enough for dom.js: elements, text nodes, attributes; no HTML parser at all. */
  function fakeDocument() {
    const made = [];
    const node = (tagName) => {
      const item = {
        tagName, children: [], attributes: {}, dataset: {}, className: '', textContent: '',
        setAttribute(name, value) { this.attributes[name] = String(value); },
        addEventListener() {},
        appendChild(child) { this.children.push(child); return child; },
        get firstChild() { return this.children[0] || null; },
        removeChild(child) { this.children.splice(this.children.indexOf(child), 1); },
      };
      made.push(item);
      return item;
    };
    return {
      made,
      createElement: node,
      createElementNS: (_, tag) => node(tag),
      createTextNode: (text) => ({ tagName: '#text', textContent: String(text), children: [] }),
    };
  }

  it('shows a feedback body as text: markup in it never becomes an element', async () => {
    const doc = fakeDocument();
    const previous = globalThis.document;
    globalThis.document = doc;
    try {
      const { renderAuthorSettings } = await import('./view-settings.js');
      const payload = '<img src=x onerror=alert(1)>\nsecond line <script>alert(2)</script>';
      renderAuthorSettings({
        model: {
          authors: [], authorsCapped: false, error: null, loading: false, loaded: true, hasOlder: false,
          feedbackReset: false,
          feedback: [{ id: 1, login: 'alice', repo: 'owner/repo', pr: 7, text: payload,
            comment_url: 'https://github.com/owner/repo/pull/7#issuecomment-1', created_at: '2026-10-15T00:00:00Z' }],
        },
        now: NOW,
      });
      const tags = new Set(doc.made.map((item) => item.tagName.toLowerCase()));
      assert.equal(tags.has('img'), false);
      assert.equal(tags.has('script'), false);
      const text = doc.made.find((item) => item.className === 'feedback__text');
      assert.equal(text.textContent, payload, 'kept literally, line break and all, as text');
    } finally {
      globalThis.document = previous;
    }
  });
});

describe('createAuthorLoader', () => {
  const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
  function setup() {
    const model = { loaded: false, loading: false, error: null, hasOlder: false, feedback: [] };
    const reads = [];
    const read = () => new Promise((resolve, reject) => reads.push({ resolve, reject }));
    const load = createAuthorLoader({
      getModel: () => model, read, render: () => {},
      onError: (error, target) => { target.error = { message: String(error) }; },
    });
    return { model, reads, load };
  }

  it('runs a refresh queued while the first load was pending, after that load fails', async () => {
    const { model, reads, load } = setup();
    load();
    assert.equal(authorsRefreshDue(model), true, 'an event during the load asks');
    load();  // the coalescer's call: queued by the gate
    assert.equal(reads.length, 1);
    reads[0].reject(new Error('502'));
    await flush();
    assert.equal(reads.length, 2, 'read again without another event');
    reads[1].resolve({ feedback: [{ id: 9 }] });
    await flush();
    assert.deepEqual(model.feedback, [{ id: 9 }]);
    assert.equal(model.loaded, true);
    assert.equal(model.error, null);
  });

  it('reads again for an event whose refresh comes due only after a failed first load', async () => {
    const { model, reads, load } = setup();
    load();
    reads[0].reject(new Error('502'));
    await flush();
    assert.equal(reads.length, 1, 'nothing was queued');
    if (authorsRefreshDue(model)) load();  // the coalescer flushes now
    assert.equal(reads.length, 2);
    reads[1].resolve({ feedback: [{ id: 10 }] });
    await flush();
    assert.deepEqual(model.feedback, [{ id: 10 }]);
  });
});
