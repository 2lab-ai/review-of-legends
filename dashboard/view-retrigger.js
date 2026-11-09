/** Retrigger page: the card behind the Retrigger badge on a PR comment.
 *
 *  Shows the PR, its current head and the latest attempt of that head, and what
 *  is queued or running. An admin gets the "Re-run review" button; everyone
 *  else reads the same card. The dispatch state comes from the durable retry
 *  request, as on the attempt page, so it survives a reload. */

import { el, field, statusChip } from './dom.js';
import {
  formatTimestamp,
  relativeTime,
  retryStatusLabel,
  safeHttpUrl,
  shortRunId,
  shortSha,
  statusLabel,
  statusTone,
  triggerLabel,
} from './format.js';
import { serializeHash } from './router.js';

const PHASE_COPY = {
  ready: { heading: 'Re-run the review', tone: 'ready' },
  dispatching: {
    heading: 'Submitting the request',
    tone: 'busy',
    note: 'Waiting for the server to accept the request.',
  },
  queued: {
    heading: 'Re-run queued',
    tone: 'busy',
    note: 'The request is durable. The review service picks it up between pull requests; the new attempt appears here as soon as it starts.',
  },
  claimed: {
    heading: 'Re-run claimed by the executor',
    tone: 'busy',
    note: 'The review service has taken the request. A new attempt appears here as soon as it starts.',
  },
  running: {
    heading: 'A review of this pull request is running',
    tone: 'busy',
    note: 'Wait for it to finish; this page follows the live stream.',
  },
  'not-open': {
    heading: 'Pull request is not open',
    tone: 'attention',
    note: 'Only an open pull request can be reviewed again.',
  },
  draft: {
    heading: 'Pull request is a draft',
    tone: 'attention',
    note: 'Mark it ready for review first; the service does not review drafts.',
  },
  'no-head': {
    heading: 'No head commit',
    tone: 'attention',
    note: 'GitHub reported no head commit for this pull request.',
  },
  dispatched: {
    heading: 'Review re-run',
    tone: 'done',
    note: 'The last request ran. Open the attempt it produced, or re-run once more.',
  },
  'dispatch-failed': {
    heading: 'Previous re-run failed',
    tone: 'attention',
    note: 'The queued request ended in failure without producing a review. You can dispatch again.',
  },
  'dispatch-stale': {
    heading: 'Previous re-run was dropped as stale',
    tone: 'attention',
    note: 'The service discarded the request: the pull request moved on, or another review of this head ran first (the dispatch error says which). Reload and try again.',
  },
  rejected: { heading: 'Re-run was refused', tone: 'attention' },
};

function runLink(runId, route, text) {
  return el('a', {
    class: 'link',
    href: serializeHash({ view: 'run', runId, repo: route.repo, query: route.query, from: route.from }),
    text,
  });
}

function prFacts(info, route) {
  const latest = info.latest_run;
  const prUrl = safeHttpUrl(info.url);
  return el('div', { class: 'fields' }, [
    field('Repository', info.repo, { mono: true }),
    field('Pull request', `#${info.pr}`, { mono: true }),
    field('State', info.state === 'open' ? (info.draft ? 'open, draft' : 'open') : (info.state || 'unknown')),
    field('Current head', shortSha(info.head_sha), { mono: true, title: info.head_sha || '' }),
    latest
      ? el('div', { class: 'field' }, [
        el('span', { class: 'field__label', text: 'Latest attempt of this head' }),
        el('span', { class: 'field__value' }, [
          statusChip(statusLabel(latest.status), statusTone(latest.status), { live: latest.status === 'running' }),
          el('span', { text: ` · ${triggerLabel(latest.trigger)} · ` }),
          runLink(latest.id, route, `attempt ${shortRunId(latest.id, 12)}`),
        ]),
      ])
      : field('Latest attempt of this head', 'none recorded'),
    prUrl ? el('div', { class: 'field' }, [
      el('span', { class: 'field__label', text: 'GitHub' }),
      el('a', { class: 'link', href: prUrl, rel: 'noreferrer noopener', text: 'Pull request on GitHub' }),
    ]) : null,
  ].filter(Boolean));
}

function requestFacts(request, childRun, route) {
  return el('div', { class: 'fields fields--tight' }, [
    field('Dispatch state', retryStatusLabel(request.status)),
    field('Request id', shortRunId(request.id, 12), { mono: true, title: request.id }),
    field('Requested', relativeTime(request.created_at), { title: formatTimestamp(request.created_at) }),
    request.requested_by ? field('Requested by', request.requested_by) : null,
    field('Head', shortSha(request.head_sha), { mono: true, title: request.head_sha || '' }),
    request.error ? field('Dispatch error', request.error) : null,
    childRun && childRun.id
      ? el('div', { class: 'field' }, [
        el('span', { class: 'field__label', text: 'New attempt' }),
        runLink(childRun.id, route, `open attempt ${shortRunId(childRun.id, 12)}`),
      ])
      : null,
  ].filter(Boolean));
}

export function renderRetrigger({
  target,
  info,
  state,
  route,
  childRun = null,
  loading = false,
  error = null,
  canAct = false,
  onRetrigger,
  onReload,
}) {
  const heading = `Re-run the review of ${target.repo}#${target.pr}`;
  const errorNotice = (problem) => el('div', { class: 'notice notice--strong', role: 'alert' }, [
    el('strong', { text: problem.title }),
    problem.detail ? el('span', { class: 'notice__detail', text: problem.detail }) : null,
    el('span', { class: 'notice__detail', text: problem.recovery }),
    el('button', { class: 'button', type: 'button', text: 'Reload', on: { click: onReload } }),
  ]);
  const errorBox = error ? errorNotice(error) : null;

  if (!info) {
    return el('section', { class: 'panel', 'aria-labelledby': 'retrigger-heading' }, [
      el('h1', { class: 'panel__title', id: 'retrigger-heading', text: heading }),
      errorBox || el('p', { class: 'loading', role: 'status', text: 'Reading the pull request...' }),
    ]);
  }

  const copy = PHASE_COPY[state.phase] || PHASE_COPY.ready;
  const request = state.request;
  const busy = state.phase === 'dispatching';
  const status = [];
  if (copy.note) status.push(el('p', { class: 'prose', text: copy.note }));
  if (state.phase === 'running' && state.runId) {
    status.push(el('p', { class: 'prose' }, [runLink(state.runId, route, `Open the running attempt ${shortRunId(state.runId, 12)}`)]));
  }
  if (request) status.push(requestFacts(request, childRun, route));
  if (state.phase === 'rejected' && state.error) status.push(errorNotice(state.error));

  const actions = canAct
    ? el('div', { class: 'retry__actions' }, [
      el('button', {
        class: 'button button--primary',
        type: 'button',
        id: 'retrigger-confirm',
        disabled: state.blocked,
        'aria-describedby': 'retrigger-panel-heading',
        'aria-busy': busy ? 'true' : null,
        text: busy ? 'Dispatching...' : state.blocked ? 'Re-run unavailable' : 'Re-run review',
        on: { click: () => onRetrigger() },
      }),
      el('button', { class: 'button', type: 'button', text: 'Reload', on: { click: onReload } }),
    ])
    : el('p', { class: 'prose prose--quiet', id: 'retrigger-read-only', text: 'Only dashboard admins can re-run a review.' });

  return el('section', { class: 'panel', 'aria-labelledby': 'retrigger-heading' }, [
    el('h1', { class: 'panel__title', id: 'retrigger-heading', text: heading }),
    info.title ? el('p', { class: 'prose', text: info.title }) : null,
    errorBox,
    prFacts(info, route),
    el('section', { class: `retry retry--${copy.tone}`, 'aria-labelledby': 'retrigger-panel-heading' }, [
      el('h2', { class: 'retry__heading', id: 'retrigger-panel-heading', text: copy.heading }),
      el('ul', { class: 'retry__scope' }, [
        el('li', { text: 'A re-run reviews the current head again with every agent and posts a new review, a new executive summary and the commit statuses, even though the head may already carry a review.' }),
        el('li', { text: 'The visual outline is drafted again and the existing outline comment is updated in place.' }),
        el('li', { text: 'The server refuses the request when the pull request moved to a newer commit, is closed or a draft, when a review of it is already queued or running, or when the previous attempt\'s review outcome is unknown.' }),
      ]),
      el('div', { class: 'retry__status', 'aria-live': 'polite' }, status),
      actions,
      canAct && state.blocked && !busy
        ? el('p', { class: 'prose prose--quiet', text: 'The button stays disabled while a review of this pull request is queued or running, so a second click cannot create a duplicate.' })
        : null,
    ]),
    loading ? el('p', { class: 'loading', role: 'status', text: 'Refreshing...' }) : null,
  ]);
}
