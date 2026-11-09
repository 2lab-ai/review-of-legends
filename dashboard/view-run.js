/** Attempt detail: header, retry dispatch panel, agent rows, event trail.
 *
 * Honesty rules encoded here:
 *   - The retry panel names what runs again. An attempt that kept the results of
 *     its successful agents (`reusable_agents`) offers "Retry failed agents (N)"
 *     and "Retry all agents" and lists the agents whose results are reused; an
 *     attempt that kept nothing offers the whole review only (retryChoices).
 *   - The dispatch state comes from the durable retry request (/api/retries), so
 *     it survives a reload and a reconnect instead of living in this tab.
 *   - The event trail is explicitly scoped to what this page received; it never
 *     implies it is the complete history of the attempt.
 *   - A minority of succeeded agents never makes the attempt look successful:
 *     the attempt status and the agent fractions are separate lines.
 */

import { cell, domId, el, field, headerCell, statusChip } from './dom.js';
import { failureDetail, hopFacts, omittedNote } from './attempts.js';
import {
  agentProgress,
  agentStatusLabel,
  durationBetween,
  formatTimestamp,
  prLabel,
  relativeTime,
  retryStatusLabel,
  safeHttpUrl,
  shortRunId,
  shortSha,
  statusLabel,
  statusTone,
  triggerLabel,
} from './format.js';
import { isRetryable, retryChoices, retryScopeLabel } from './runs.js';
import { serializeHash } from './router.js';
import { catalogUsable, describePolicy, formatTokens, projectRequestPolicy } from './models.js';
import { effortSelect, modelOptions } from './view-settings.js';

const PHASE_COPY = {
  ready: {
    heading: 'Retry this review',
    tone: 'ready',
  },
  dispatching: {
    heading: 'Submitting the retry request',
    tone: 'busy',
    note: 'Waiting for the server to accept the request.',
  },
  queued: {
    heading: 'Retry queued',
    tone: 'busy',
    note: 'The request is durable. It is waiting for the review service to pick it up between '
      + 'pull requests, which can take until the current poll cycle finishes.',
  },
  claimed: {
    heading: 'Retry claimed by the executor',
    tone: 'busy',
    note: 'The review service has taken the request. A new attempt appears below as soon as it starts.',
  },
  dispatched: {
    heading: 'Retry already dispatched',
    tone: 'done',
    note: 'This attempt was retried. Open the newer attempt to follow it.',
  },
  'dispatch-failed': {
    heading: 'Previous dispatch failed',
    tone: 'attention',
    note: 'The queued request ended in failure without producing a review. You can dispatch again.',
  },
  'dispatch-stale': {
    heading: 'Previous dispatch was dropped as stale',
    tone: 'attention',
    note: 'The service discarded the request, usually because the pull request moved on. '
      + 'Check the current head before dispatching again.',
  },
  rejected: {
    heading: 'Retry was refused',
    tone: 'attention',
  },
};

function lineageLinks(run, { route, parentRun, childRun }) {
  const links = [];
  if (run.retry_of) {
    links.push(el('a', {
      class: 'link',
      href: serializeHash({ view: 'run', runId: run.retry_of, repo: route.repo, query: route.query, from: route.from }),
      text: parentRun && parentRun.status
        ? `Earlier attempt ${shortRunId(run.retry_of)} (${statusLabel(parentRun.status)})`
        : `Earlier attempt ${shortRunId(run.retry_of)}`,
    }));
  }
  if (childRun && childRun.id) {
    links.push(el('a', {
      class: 'link',
      href: serializeHash({ view: 'run', runId: childRun.id, repo: route.repo, query: route.query, from: route.from }),
      text: childRun.status
        ? `Retry attempt ${shortRunId(childRun.id)} (${statusLabel(childRun.status)})`
        : `Retry attempt ${shortRunId(childRun.id)}`,
    }));
  }
  if (links.length === 0) return null;
  return el('div', { class: 'lineage' }, [
    el('span', { class: 'lineage__label', text: 'Attempt chain' }),
    ...links,
  ]);
}

function header(run, { route, parentRun, childRun, onBack }) {
  const prUrl = safeHttpUrl(run.url);
  const reviewUrl = safeHttpUrl(run.review_url);
  const progress = agentProgress(run);
  return el('div', { class: 'detail__head' }, [
    el('button', { class: 'button button--quiet', type: 'button', text: 'Back to queue', on: { click: onBack } }),
    el('h1', { class: 'panel__title', id: 'run-heading', text: run.title && run.title.trim() !== '' ? run.title : `${run.repo} ${prLabel(run)}` }),
    el('div', { class: 'detail__status' }, [
      statusChip(statusLabel(run.status), statusTone(run.status), { live: run.status === 'running' }),
      run.stage ? el('span', { class: 'detail__stage', text: run.stage }) : null,
      progress ? el('span', { class: 'detail__stage', text: progress.label }) : null,
    ]),
    el('div', { class: 'fields' }, [
      field('Repository', run.repo || 'unknown', { mono: true }),
      field('Pull request', prLabel(run), { mono: true }),
      field('Head commit', shortSha(run.head_sha), { mono: true, title: run.head_sha || '' }),
      field('Attempt id', shortRunId(run.id, 12), { mono: true, title: run.id }),
      field('Trigger', triggerLabel(run.trigger)),
      field('Started', relativeTime(run.created_at), { title: formatTimestamp(run.created_at) }),
      field('Last update', relativeTime(run.updated_at), { title: formatTimestamp(run.updated_at) }),
      field('Elapsed', durationBetween(run.created_at, run.updated_at)),
      run.llm && run.llm.policy
        ? field('Model policy', `${describePolicy(run.llm.policy)}${run.llm.policy.source ? ` (source: ${run.llm.policy.source})` : ''}`, { mono: true })
        : null,
      run.llm && run.llm.totals && formatTokens(run.llm.totals)
        ? field('Tokens', `${formatTokens(run.llm.totals)}${typeof run.llm.totals.agents_with_usage === 'number' ? ` · ${run.llm.totals.agents_with_usage} agent(s) reported` : ''}`, { mono: true })
        : null,
      run.llm && run.llm.totals && Array.isArray(run.llm.totals.models_used) && run.llm.totals.models_used.length > 0
        ? field('Models used', run.llm.totals.models_used.join(', '), { mono: true })
        : null,
    ].filter(Boolean)),
    el('div', { class: 'detail__links' }, [
      prUrl ? el('a', { class: 'link', href: prUrl, rel: 'noreferrer noopener', text: 'Pull request on GitHub' }) : null,
      reviewUrl ? el('a', { class: 'link', href: reviewUrl, rel: 'noreferrer noopener', text: 'Posted review' }) : null,
      run.trigger === 'baseline'
        ? el('span', { class: 'quiet', text: 'Baseline record: this pull request was never reviewed by the service.' })
        : null,
    ]),
    lineageLinks(run, { route, parentRun, childRun }),
    run.error
      ? el('div', { class: 'errorbox' }, [
        el('span', { class: 'errorbox__label', text: 'Failure reported by the service' }),
        el('p', { class: 'errorbox__text', text: run.error }),
      ])
      : null,
  ]);
}

/** Pinned at the head of the detail so an operator never scrolls to find it, and
 *  so the scope disclosure is read before the button is reachable. */
function retryModelChoice({ catalog, policy, choice, disabled, onChoice, onLoadCatalog, catalogLoading }) {
  const current = policy ? describePolicy(policy) : 'as configured on the service';
  const catalogReady = catalogUsable(catalog);
  return el('div', { class: 'retry__model' }, [
    el('div', { class: 'filters__group filters__group--grow' }, [
      el('label', { class: 'control__label', for: 'retry-model', text: 'Model for this retry' }),
      el('select', {
        class: 'control control--select',
        id: 'retry-model',
        disabled: disabled || !catalogReady,
        on: { change: (event) => onChoice({ model: event.target.value, effort: '' }) },
      }, modelOptions(catalog, choice.model, { allowEmpty: true, emptyLabel: `Service policy: ${current}` })),
      catalogReady
        ? null
        : el('span', { class: 'quiet' }, [
          el('span', { text: catalogLoading ? 'Loading the model catalog... ' : 'Model catalog not loaded; the retry uses the service policy. ' }),
          catalogLoading
            ? null
            : el('button', { class: 'button button--quiet', type: 'button', id: 'retry-load-catalog', text: 'Load model list', on: { click: onLoadCatalog } }),
        ]),
      el('span', { class: 'quiet', text: 'Claude models are not available to this service and cannot be chosen.' }),
    ]),
    choice.model
      ? el('div', { class: 'filters__group' }, [
        el('label', { class: 'control__label', for: 'retry-effort', text: 'Effort' }),
        effortSelect('retry-effort', catalog, choice.model, choice.effort, (effort) => onChoice({ ...choice, effort }), { disabled }),
        el('span', { class: 'quiet', text: 'This model only; no fallback for a pinned retry.' }),
      ])
      : null,
  ]);
}

function retryPanel({ run, state, route, childRun, catalog, policy, choice, onChoice, onRetry, onReload, onLoadCatalog, catalogLoading, canRetry = true }) {
  if (state.phase === 'hidden') return null;
  if (!canRetry) {
    // A viewer sees the failure, the chain and the latest request's state;
    // the picker and the button never render, whatever the request history.
    const request = state && state.request;
    return el('section', { class: 'retry retry--quiet', 'aria-labelledby': 'retry-heading' }, [
      el('h2', { class: 'retry__heading', id: 'retry-heading', text: 'Retry is available to the admin account' }),
      el('p', { class: 'prose', text: 'This account is read-only. The admin can dispatch a retry of this review from the same page.' }),
      request
        ? el('p', { class: 'prose prose--quiet', text: `Latest retry request: ${request.status || state.phase}${request.requested_by ? ` · Requested by ${request.requested_by}` : ''}${request.model_policy ? ` · ${describePolicy(request.model_policy)}` : ''}` })
        : null,
    ]);
  }
  if (state.phase === 'not-retryable') {
    if (run.status === 'running') {
      return el('section', { class: 'retry retry--quiet', 'aria-labelledby': 'retry-heading' }, [
        el('h2', { class: 'retry__heading', id: 'retry-heading', text: 'Retry unavailable while running' }),
        el('p', { class: 'prose', text: 'This attempt is still executing. Retry becomes available if it ends failed, interrupted or stale.' }),
      ]);
    }
    return null;
  }

  const copy = PHASE_COPY[state.phase] || PHASE_COPY.ready;
  const request = state.request;
  const busy = state.phase === 'dispatching';
  const choices = retryChoices(run, choice && choice.model);

  const disclosure = el('ul', { class: 'retry__scope' }, [
    choices.secondary
      ? el('li', { text: 'Scope: "Retry failed agents" runs again only the agents this attempt kept no result for; the results of the others are reused as they are, with the models they ran on, and count toward the new review like fresh ones. "Retry all agents" runs every agent again and consumes provider usage again.' })
      : el('li', { text: 'Scope: the entire review runs again. This attempt kept no agent results to reuse, so every agent re-runs and consumes provider usage again.' }),
    el('li', {}, [
      el('span', { text: 'Commit submitted: ' }),
      el('span', { class: 'mono', text: shortSha(run.head_sha), title: run.head_sha || '' }),
      el('span', { text: '. If the pull request has moved to a newer commit, the server refuses the retry and a new review is needed instead.' }),
    ]),
    el('li', { text: 'This attempt and its failure stay in history. The retry creates a new attempt linked to this one.' }),
    el('li', { text: 'Dispatching queues the request. The review service claims it between pull requests, so the new attempt may not start immediately.' }),
    el('li', { text: 'One request per commit: if a retry for this commit is already queued or running, the server reuses it, with the model and the agents it was created with. What is shown under "Model for this request" and "Agents run again" is what runs.' }),
  ]);

  const status = [];
  if (copy.note) status.push(el('p', { class: 'prose', text: copy.note }));
  if (request) {
    status.push(el('div', { class: 'fields fields--tight' }, [
      field('Dispatch state', retryStatusLabel(request.status)),
      field('Request id', shortRunId(request.id, 12), { mono: true, title: request.id }),
      field('Requested', relativeTime(request.created_at), { title: formatTimestamp(request.created_at) }),
      request.requested_by ? field('Requested by', request.requested_by) : null,
      field('Model for this request', (() => {
        const pinned = projectRequestPolicy(request.model_policy);
        return pinned ? describePolicy(pinned) : 'service policy at dispatch time';
      })(), { mono: true }),
      field('Agents run again', retryScopeLabel(request.scope)),
      request.error ? field('Dispatch error', request.error) : null,
    ].filter(Boolean)));
  }
  if (state.phase === 'rejected' && state.error) {
    status.push(el('div', { class: 'notice notice--strong', role: 'alert' }, [
      el('strong', { text: state.error.title }),
      state.error.detail ? el('span', { class: 'notice__detail', text: state.error.detail }) : null,
      el('span', { class: 'notice__detail', text: state.error.recovery }),
      el('button', { class: 'button', type: 'button', text: 'Reload attempt', on: { click: onReload } }),
    ]));
  }
  if (childRun && childRun.id) {
    status.push(el('a', {
      class: 'button',
      href: serializeHash({ view: 'run', runId: childRun.id, repo: route.repo, query: route.query, from: route.from }),
      text: 'Open the new attempt',
    }));
  }

  return el('section', { class: `retry retry--${copy.tone}`, 'aria-labelledby': 'retry-heading' }, [
    el('h2', { class: 'retry__heading', id: 'retry-heading', text: copy.heading }),
    disclosure,
    state.blocked ? null : retryModelChoice({ catalog, policy, choice: choice || { model: '', effort: '' }, disabled: busy, onChoice, onLoadCatalog, catalogLoading }),
    el('div', { class: 'retry__status', 'aria-live': 'polite' }, status),
    choices.reused.length > 0 && !state.blocked
      ? el('p', { class: 'prose prose--quiet', id: 'retry-reuses', text: `Reuses: ${choices.reused.join(', ')}` })
      : null,
    el('div', { class: 'retry__actions' }, [
      el('button', {
        class: 'button button--primary',
        type: 'button',
        id: 'retry-confirm',
        disabled: state.blocked,
        'aria-describedby': 'retry-heading',
        'aria-busy': busy ? 'true' : null,
        text: busy
          ? 'Dispatching...'
          : state.blocked
            ? 'Retry unavailable'
            : choices.primary.label,
        on: { click: () => onRetry(choices.primary.scope) },
      }),
      choices.secondary && !state.blocked
        ? el('button', {
          class: 'button',
          type: 'button',
          id: 'retry-all',
          'aria-describedby': 'retry-heading',
          text: choices.secondary.label,
          on: { click: () => onRetry(choices.secondary.scope) },
        })
        : null,
    ]),
    state.blocked && !busy
      ? el('p', { class: 'prose prose--quiet', text: 'The button stays disabled while a dispatch for this attempt is recorded, so a second click cannot create a duplicate.' })
      : null,
  ]);
}

/** Why the agent or a model hop failed: each failed hop's cause, status and the
 *  text the proxy or model sent back. Collapsed by default; rendered as text only.
 *  The id is per run and agent so app.js keeps it open across live re-renders. */
function failureDetails(agent, runId) {
  const detail = failureDetail(agent);
  if (!detail) return null;
  const agentId = typeof agent.id === 'string' ? agent.id : 'unknown';
  return el('details', { class: 'why', id: domId('why', runId || 'run', agentId) }, [
    el('summary', { class: 'why__summary', text: detail.summary }),
    detail.omitted > 0 ? el('p', { class: 'why__omitted quiet', text: omittedNote(detail.omitted) }) : null,
    ...detail.hops.map((hop) => el('div', { class: 'why__hop' }, [
      el('p', { class: 'why__cause', text: `${hop.model} — ${hop.cause}` }),
      el('div', { class: 'row__meta' }, hopFacts(hop).map((fact) => el('span', { text: fact }))),
      hop.text
        ? el('pre', { class: 'why__text', text: hop.text })
        : el('p', { class: 'quiet', text: 'no response text recorded' }),
    ])),
    detail.error
      ? el('div', { class: 'why__hop' }, [
        el('p', { class: 'why__cause', text: 'Agent failure output' }),
        el('pre', { class: 'why__text', text: detail.error }),
      ])
      : null,
  ]);
}

function agentsTable(run) {
  const agents = Array.isArray(run.agents) ? run.agents : [];
  const progress = agentProgress(run);
  return el('section', { class: 'panel__section', 'aria-labelledby': 'agents-heading' }, [
    el('h2', { class: 'section__heading', id: 'agents-heading', text: 'Agents' }),
    el('p', { class: 'tablenote' }, [
      progress ? el('span', { text: progress.label }) : el('span', { text: 'agent totals not reported for this attempt' }),
      el('span', { text: 'agent success does not imply the review was posted; the attempt state above is the outcome' }),
    ]),
    agents.length === 0
      ? el('p', { class: 'empty', text: 'No agent rows recorded for this attempt.' })
      : el('div', { class: 'tablewrap' }, [
        el('table', { class: 'table' }, [
          el('caption', { class: 'sr-only', text: 'Agents in this attempt' }),
          el('thead', {}, [el('tr', {}, [headerCell('Agent'), headerCell('State'), headerCell('Model', { mono: true }), headerCell('Tokens', { mono: true }), headerCell('Summary')])]),
          el('tbody', {}, agents.map((agent) => el('tr', { class: 'row' }, [
            cell('Agent', [el('span', { class: 'mono', text: typeof agent.id === 'string' ? agent.id : 'unknown' })], { mono: true }),
            cell('State', [
              statusChip(agentStatusLabel(agent.status), statusTone(agent.status), { live: agent.status === 'running' }),
              // Not run in this attempt: a "failed agents" retry kept the earlier result.
              agent.reused === true ? el('div', { class: 'row__meta' }, [el('span', { text: 'reused from the previous attempt' })]) : null,
            ]),
            cell('Model', [
              typeof agent.model === 'string' && agent.model !== ''
                ? el('span', { class: 'mono', text: `${agent.model}${agent.effort ? ` · ${agent.effort}` : ''}` })
                : el('span', { class: 'quiet', text: 'not reported' }),
              el('div', { class: 'row__meta' }, [
                typeof agent.route === 'string' && agent.route !== '' ? el('span', { text: agent.route }) : null,
                typeof agent.elapsed_seconds === 'number' ? el('span', { text: `${agent.elapsed_seconds.toFixed(1)}s` }) : null,
              ]),
              failureDetails(agent, run.id),
            ], { mono: true }),
            cell('Tokens', [
              formatTokens(agent)
                ? el('span', { class: 'mono', text: formatTokens(agent) })
                : el('span', { class: 'quiet', text: 'not reported' }),
            ], { mono: true }),
            cell('Summary', [
              typeof agent.summary === 'string' && agent.summary.trim() !== ''
                ? el('span', { text: agent.summary })
                : el('span', { class: 'quiet', text: 'no summary' }),
            ], { wide: true }),
          ]))),
        ]),
      ]),
  ]);
}

function eventTrail({ events, sessionStartedAt, streamPhase }) {
  return el('section', { class: 'panel__section', 'aria-labelledby': 'events-heading' }, [
    el('h2', { class: 'section__heading', id: 'events-heading', text: 'Event trail' }),
    el('p', { class: 'tablenote' }, [
      el('span', { text: `events received by this page since ${formatTimestamp(sessionStartedAt)}` }),
      el('span', { text: 'earlier events are held by the server but are not loaded into this view' }),
      el('span', { text: `stream: ${streamPhase}` }),
    ]),
    events.length === 0
      ? el('p', { class: 'empty', text: 'No events for this attempt have arrived on this page yet.' })
      : el('ol', { class: 'trail' }, events.map((event) => el('li', { class: 'trail__item' }, [
        el('span', { class: 'trail__time mono', text: relativeTime(event.created_at), title: formatTimestamp(event.created_at) }),
        el('span', { class: 'trail__kind', text: event.kind }),
        el('span', { class: 'trail__body', text: describeEventPayload(event) }),
        el('span', { class: 'trail__id mono', text: `#${event.id}` }),
      ]))),
  ]);
}

/** Payload rendering is a flat, bounded key=value line. Raw prompts and paths are
 *  not part of the contract, and nothing here is interpreted as HTML. */
export function describeEventPayload(event) {
  const payload = event && event.payload && typeof event.payload === 'object' ? event.payload : {};
  const parts = [];
  for (const [key, value] of Object.entries(payload)) {
    if (value === null || value === undefined) continue;
    if (key === 'agents' && Array.isArray(value)) {
      parts.push(`agents=${value.length}`);
      continue;
    }
    if (typeof value === 'object') continue;
    const text = String(value);
    parts.push(`${key}=${text.length > 120 ? `${text.slice(0, 120)}...` : text}`);
  }
  return parts.length === 0 ? 'no payload fields' : parts.join('  ');
}

export function renderRunDetail({
  run,
  route,
  retryState,
  parentRun,
  childRun,
  events,
  sessionStartedAt,
  streamPhase,
  loading,
  error,
  catalog = null,
  policy = null,
  retryChoice = null,
  onRetryChoice = () => {},
  onLoadCatalog = () => {},
  catalogLoading = false,
  canRetry = true,
  onBack,
  onRetry,
  onReload,
}) {
  if (!run) {
    return el('section', { class: 'panel', 'aria-labelledby': 'run-heading' }, [
      el('h1', { class: 'panel__title', id: 'run-heading', text: 'Attempt' }),
      error
        ? el('div', { class: 'notice notice--strong', role: 'alert' }, [
          el('strong', { text: error.title }),
          error.detail ? el('span', { class: 'notice__detail', text: error.detail }) : null,
          el('span', { class: 'notice__detail', text: error.recovery }),
          el('button', { class: 'button', type: 'button', text: 'Back to queue', on: { click: onBack } }),
        ])
        : el('p', { class: 'loading', role: 'status', text: 'Loading attempt...' }),
    ]);
  }

  return el('section', { class: 'panel', 'aria-labelledby': 'run-heading' }, [
    header(run, { route, parentRun, childRun, onBack }),
    isRetryable(run) || (retryState && retryState.request)
      ? retryPanel({ run, state: retryState, route, childRun, catalog, policy, choice: retryChoice, onChoice: onRetryChoice, onRetry, onReload, onLoadCatalog, catalogLoading, canRetry })
      : null,
    error
      ? el('div', { class: 'notice notice--strong', role: 'alert' }, [
        el('strong', { text: error.title }),
        error.detail ? el('span', { class: 'notice__detail', text: error.detail }) : null,
        el('span', { class: 'notice__detail', text: error.recovery }),
        el('button', { class: 'button', type: 'button', text: 'Reload attempt', on: { click: onReload } }),
      ])
      : null,
    agentsTable(run),
    eventTrail({ events, sessionStartedAt, streamPhase }),
    loading ? el('p', { class: 'loading', role: 'status', text: 'Refreshing attempt...' }) : null,
  ]);
}
