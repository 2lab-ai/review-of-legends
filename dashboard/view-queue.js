/** Queue view: Active / Needs attention / History.
 *
 * One row per attempt, newest first, exactly in the order the API returned. No
 * stat tiles and no charts: the nav carries the labelled counts, and each row
 * carries its own state, so an invented visualisation would only add unverifiable
 * numbers to the screen.
 */

import { cell, el, headerCell, statusChip } from './dom.js';
import {
  agentProgress,
  formatCount,
  formatTimestamp,
  prLabel,
  relativeTime,
  safeHttpUrl,
  shortRunId,
  shortSha,
  statusLabel,
  statusTone,
  triggerLabel,
} from './format.js';
import { BUCKET_LABELS, filterRuns, repoOptions } from './runs.js';
import { serializeHash } from './router.js';

const BUCKET_INTENT = {
  active: 'Attempts the service is executing right now.',
  attention: 'Attempts that ended without a posted review: failed, interrupted or stale. '
    + 'Retry is dispatched from the attempt page.',
  history: 'Every recorded attempt, including superseded ones. Earlier attempts are kept, '
    + 'never overwritten by a retry.',
};

const EMPTY_TEXT = {
  active: 'No attempt is running.',
  attention: 'No attempt is waiting for an operator.',
  history: 'No attempts recorded yet.',
};

function filterBar({ route, runs, onFilters }) {
  const repos = repoOptions(runs);
  const select = el('select', {
    class: 'control control--select',
    id: 'filter-repo',
    on: {
      change: (event) => onFilters({ repo: event.target.value, query: route.query }),
    },
  }, [
    el('option', { value: '', text: 'All repositories', selected: route.repo === '' }),
    ...repos.map((repo) => el('option', { value: repo, text: repo, selected: repo === route.repo })),
    route.repo && !repos.includes(route.repo)
      ? el('option', { value: route.repo, text: route.repo, selected: true })
      : null,
  ]);

  const search = el('input', {
    class: 'control control--text',
    id: 'filter-query',
    type: 'search',
    value: route.query,
    placeholder: 'PR number, title, commit, error',
    autocomplete: 'off',
    spellcheck: 'false',
    on: {
      input: (event) => onFilters({ repo: route.repo, query: event.target.value }),
    },
  });

  return el('form', {
    class: 'filters',
    role: 'search',
    on: { submit: (event) => event.preventDefault() },
  }, [
    el('div', { class: 'filters__group' }, [
      el('label', { class: 'control__label', for: 'filter-repo', text: 'Repository' }),
      select,
    ]),
    el('div', { class: 'filters__group filters__group--grow' }, [
      el('label', { class: 'control__label', for: 'filter-query', text: 'Search loaded attempts' }),
      search,
    ]),
    route.repo || route.query
      ? el('button', {
        class: 'button button--quiet filters__clear',
        type: 'button',
        text: 'Clear filters',
        on: { click: () => onFilters({ repo: '', query: '' }) },
      })
      : null,
  ]);
}

/** Filters are carried into a run detail so returning restores them even when
 *  the operator arrived by a shared deep link and uses the in-page Back. */
function titleCell(run, route) {
  const href = serializeHash({
    view: 'run',
    runId: run.id,
    repo: route.repo,
    query: route.query,
    from: route.view,
  });
  const prUrl = safeHttpUrl(run.url);
  return cell('Pull request', [
    el('a', {
      class: 'link link--strong',
      href,
      text: run.title && run.title.trim() !== '' ? run.title : `${run.repo} ${prLabel(run)}`,
    }),
    el('div', { class: 'row__meta' }, [
      el('span', { class: 'mono', text: `${run.repo} ${prLabel(run)}` }),
      prUrl ? el('a', { class: 'link link--quiet', href: prUrl, rel: 'noreferrer noopener', text: 'open on GitHub' }) : null,
    ]),
  ], { wide: true });
}

function statusCell(run, { hasActiveRetry }) {
  const progress = agentProgress(run);
  return cell('State', [
    statusChip(statusLabel(run.status), statusTone(run.status), { live: run.status === 'running' }),
    el('div', { class: 'row__meta' }, [
      run.stage ? el('span', { text: run.stage }) : null,
      progress ? el('span', { text: progress.label }) : null,
      hasActiveRetry ? el('span', { class: 'row__flag', text: 'retry dispatch in queue' }) : null,
    ]),
  ]);
}

function outcomeCell(run) {
  const reviewUrl = safeHttpUrl(run.review_url);
  return cell('Review', [
    reviewUrl
      ? el('a', { class: 'link', href: reviewUrl, rel: 'noreferrer noopener', text: 'Posted review' })
      : el('span', { class: 'quiet', text: run.trigger === 'baseline' ? 'not reviewed' : 'no review posted' }),
    run.error
      ? el('div', { class: 'row__meta row__meta--error', text: run.error })
      : null,
  ], { wide: true });
}

function runRow(run, { route, retryRunIds }) {
  return el('tr', { class: 'row', dataset: { runId: run.id } }, [
    titleCell(run, route),
    statusCell(run, { hasActiveRetry: retryRunIds.has(run.id) }),
    outcomeCell(run),
    cell('Commit', [el('span', { class: 'mono', text: shortSha(run.head_sha), title: run.head_sha || '' })], { mono: true }),
    cell('Attempt', [
      el('span', { class: 'mono', text: shortRunId(run.id), title: run.id }),
      el('div', { class: 'row__meta' }, [
        el('span', { text: triggerLabel(run.trigger) }),
        run.retry_of ? el('span', { text: 'retry of an earlier attempt' }) : null,
        run.llm && run.llm.totals && Array.isArray(run.llm.totals.models_used) && run.llm.totals.models_used.length > 0
          ? el('span', { class: 'mono', text: run.llm.totals.models_used.join(', ') })
          : null,
      ]),
    ], { mono: true }),
    cell('Started', [
      el('span', { text: relativeTime(run.created_at), title: formatTimestamp(run.created_at) }),
      el('div', { class: 'row__meta' }, [
        el('span', { text: `updated ${relativeTime(run.updated_at)}`, title: formatTimestamp(run.updated_at) }),
      ]),
    ]),
  ]);
}

export function renderQueue({
  bucket,
  route,
  runs,
  loading,
  error,
  retryRunIds,
  hasMore,
  onFilters,
  onLoadMore,
  onRetryLoad,
}) {
  const visible = filterRuns(runs, { repo: route.repo, query: route.query });
  const filtered = visible.length !== runs.length;

  const body = el('tbody', {}, visible.map((run) => runRow(run, { route, retryRunIds })));

  return el('section', { class: 'panel', 'aria-labelledby': 'queue-heading' }, [
    el('div', { class: 'panel__head' }, [
      el('h1', { class: 'panel__title', id: 'queue-heading', text: BUCKET_LABELS[bucket] }),
      el('p', { class: 'panel__intent', text: BUCKET_INTENT[bucket] }),
    ]),
    filterBar({ route, runs, onFilters }),
    error
      ? el('div', { class: 'notice notice--strong', role: 'alert' }, [
        el('strong', { text: error.title }),
        error.detail ? el('span', { class: 'notice__detail', text: error.detail }) : null,
        el('span', { class: 'notice__detail', text: error.recovery }),
        el('button', { class: 'button', type: 'button', text: 'Retry loading', on: { click: onRetryLoad } }),
      ])
      : null,
    el('p', { class: 'tablenote' }, [
      el('span', {
        text: `${formatCount(visible.length, hasMore)} of ${formatCount(runs.length, hasMore)} loaded attempts shown`,
      }),
      filtered ? el('span', { text: 'search narrows the attempts already loaded, not the full history' }) : null,
      el('span', { text: 'newest first, as ordered by the server' }),
    ]),
    visible.length === 0 && !loading
      ? el('p', { class: 'empty', text: filtered ? 'No loaded attempt matches these filters.' : EMPTY_TEXT[bucket] })
      : el('div', { class: 'tablewrap' }, [
        el('table', { class: 'table' }, [
          el('caption', { class: 'sr-only', text: `${BUCKET_LABELS[bucket]} review attempts` }),
          el('thead', {}, [
            el('tr', {}, [
              headerCell('Pull request'),
              headerCell('State'),
              headerCell('Review'),
              headerCell('Commit', { mono: true }),
              headerCell('Attempt', { mono: true }),
              headerCell('Started'),
            ]),
          ]),
          body,
        ]),
      ]),
    loading ? el('p', { class: 'loading', role: 'status', text: 'Loading attempts...' }) : null,
    hasMore && !loading
      ? el('button', { class: 'button', type: 'button', text: 'Load older attempts', on: { click: onLoadMore } })
      : null,
    !hasMore && runs.length > 0
      ? el('p', { class: 'tablenote', text: 'All attempts the server holds for this view are loaded.' })
      : null,
  ]);
}
