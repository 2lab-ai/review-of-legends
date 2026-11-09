/** Controller: session gate, routing, data loading, stream wiring.
 *
 * Shape of the thing: one in-memory model, one render pass into <main>, and
 * coalesced refreshes driven by the event stream. Events never carry the UI on
 * their own - they mark data dirty and the durable API is re-read - so a page
 * that was asleep, reconnected or freshly loaded converges on the same state.
 */

import { ApiClient, createCoalescer } from './api.js';
import { captureOpenDetails, clear, createGestureGate, createRenderScheduler, replace, restoreOpenDetails } from './dom.js';
import { describeApiError } from './errors.js';
import { footerText, formatCount, relativeFromEpochMillis } from './format.js';
import {
  BUCKETS,
  applyRetryUpdates,
  applyRunChange,
  applySourcePage,
  bucketAccepts,
  bucketStatuses,
  createSources,
  findRun,
  hasMore,
  isRetryEvent,
  isSettingEvent,
  latestRetryFor,
  mergeRuns,
  pendingSources,
  pushEvent,
  retryAttemptFor,
  activeRetryFor,
  retryPanelState,
  isRetryable,
  runChangeFromEvent,
  runIdsWithActiveRetry,
  eventsForRun,
  upsertRun,
} from './runs.js';
import { backRoute, parseHash, serializeHash } from './router.js';
import { describeFreshness, openEventStream } from './stream.js';
import { projectUsage } from './usage.js';
import {
  addDraftFallback,
  draftFromPolicy,
  moveDraftFallback,
  policyRequestBody,
  projectCatalog,
  projectPolicyPayload,
  removeDraftFallback,
  setDraftFallback,
  setDraftModel,
  shouldLoadCatalog,
} from './models.js';
import { agentsNavBadge } from './agents.js';
import { emptyAgentsModel as emptyAgents } from './agents-controller.js';
import { createAgentHandlers } from './agents-handlers.js';
import {
  BASELINE_NOTE,
  MAX_ADDED,
  addedNotice,
  initialRepositoryDraft,
  nextRepositoryDraft,
  projectRepositorySettings,
  removedNotice,
  repositoryActionError,
  repositoryFormState,
} from './repositories.js';
import {
  MAX_ADMINS,
  addedAdminNotice,
  adminActionError,
  adminFormState,
  adminRules,
  createAdminListSync,
  isAdminsEvent,
  projectAdminSettings,
  removedAdminNotice,
} from './admins.js';
import {
  authorsRefreshDue,
  createAuthorLoader,
  isAuthorEvent,
  loadAllAuthors,
  loadOlderFeedback,
  refreshFeedback,
} from './authors.js';
import { renderAccounts } from './view-accounts.js';
import { renderAgents } from './view-agents.js';
import { renderSettings, syncAdminForm, syncRepositoryForm } from './view-settings.js';
import { renderQueue } from './view-queue.js';
import { renderRunDetail } from './view-run.js';
import { renderSignIn } from './view-signin.js';
import { renderRetrigger } from './view-retrigger.js';
import {
  acknowledgeRefusal,
  beginRetriggerLoad,
  reconcileRetriggerInfo,
  retriggerChildRun,
  retriggerPanelState,
  retriggerRefreshWanted,
  retriggerTarget,
  settleRetriggerDispatch,
  settleRetriggerLoad,
} from './retrigger.js';
import { rememberReturnHash, returnHashAfterSignIn, safeSessionStorage, takeReturnHash } from './signin-return.js';
import { canAct, describeRole, resolveView, visibleViews } from './roles.js';

const PAGE_SIZE = 50;

const api = new ApiClient();

const dom = {
  main: document.getElementById('main'),
  nav: document.getElementById('nav'),
  session: document.getElementById('session'),
  email: document.getElementById('session-email'),
  signout: document.getElementById('signout'),
  streamLabel: document.getElementById('stream-label'),
  streamDetail: document.getElementById('stream-detail'),
  footerVersion: document.getElementById('footer-version'),
  counts: {
    active: document.getElementById('count-active'),
    attention: document.getElementById('count-attention'),
    history: document.getElementById('count-history'),
    accounts: document.getElementById('count-accounts'),
    agents: document.getElementById('count-agents'),
    settings: document.getElementById('count-settings'),
  },
  navItems: {
    active: document.getElementById('nav-active'),
    attention: document.getElementById('nav-attention'),
    history: document.getElementById('nav-history'),
    accounts: document.getElementById('nav-accounts'),
    agents: document.getElementById('nav-agents'),
    settings: document.getElementById('nav-settings'),
  },
};

function emptyBucket(bucket) {
  return { runs: [], sources: createSources(bucket), loading: false, error: null, loaded: false };
}


/** Review repositories on the Settings page; `saving` is 'add' or the repository
 *  being removed; `actionError` is a refused add/remove, `error` a failed load. */
function emptyRepositories() {
  return {
    rows: [], allowedOwners: [], maxAdded: MAX_ADDED, note: BASELINE_NOTE, configError: null,
    draft: '', draftTouched: false, loading: false, saving: null, error: null, actionError: null,
    notice: null, loaded: false,
  };
}

/** Who is admin, on the Settings page; `canEdit` is the server's verdict for this
 *  session (owner only), `storeError` the server's note when the list cannot be
 *  read, `error` a failed load and `actionError` a refused add/remove. `sync`
 *  orders list reads against add/remove answers (admins.js createAdminListSync). */
function emptyAdmins() {
  return {
    owner: null, rows: [], canEdit: false, maxAdded: MAX_ADMINS, storeError: null,
    draft: '', loading: false, saving: null, error: null, actionError: null, notice: null, loaded: false,
    sync: createAdminListSync(),
  };
}

/** The Retrigger page (`#retrigger/owner/name/42`): the server's answer for
 *  `target`, and this tab's optimistic dispatch phase, as on the attempt page. */
function emptyRetrigger() {
  return { target: null, info: null, loading: false, error: null, local: null };
}

/** PR authors' settings and feedback on the Settings page: read-only, so no
 *  draft and no saving; `hasOlder` says there is feedback below the oldest row
 *  shown, `feedbackReset` that a refresh could not catch up and started over,
 *  `authorsCapped` that the author list stopped at MAX_AUTHOR_PAGES. */
function emptyAuthors() {
  return {
    authors: [], authorsCapped: false, feedback: [], hasOlder: false, feedbackReset: false,
    loading: false, error: null, loaded: false,
  };
}

const state = {
  phase: 'booting',
  signInReason: null,
  identity: null,
  route: parseHash(window.location.hash),
  renderedKey: null,
  sessionStartedAt: new Date().toISOString(),
  buckets: {
    active: emptyBucket('active'),
    attention: emptyBucket('attention'),
    history: emptyBucket('history'),
  },
  retries: { requests: [], loading: false, error: null, loaded: false },
  detail: { runId: null, run: null, loading: false, error: null, local: null },
  usage: { data: null, loading: false, error: null, loaded: false },
  settings: { catalog: null, policy: null, draft: null, loading: false, saving: false, error: null, notice: null, loaded: false },
  agents: emptyAgents(),
  repositories: emptyRepositories(),
  admins: emptyAdmins(),
  retrigger: emptyRetrigger(),
  authors: emptyAuthors(),
  retryChoice: { model: '', effort: '' },
  events: [],
  streamSnapshot: { phase: 'idle', attempts: 0, lastEventAt: null, lastSignalAt: null, heartbeatSeen: false },
  streamDescribed: { phase: 'idle', label: 'Stream idle', detail: '', suspect: false },
};

let stream = null;

/* ------------------------------------------------------------------ helpers */

function knownRuns() {
  const lists = BUCKETS.map((bucket) => state.buckets[bucket].runs);
  if (state.detail.run) lists.push([state.detail.run]);
  return mergeRuns(...lists);
}

function currentBucket() {
  return BUCKETS.includes(state.route.view) ? state.route.view : null;
}

/** A session failure anywhere collapses the whole shell back to sign-in: there is
 *  no half-authorised state worth rendering. */
function toSignIn(reason) {
  state.phase = 'signed-out';
  state.signInReason = reason || null;
  state.identity = null;
  state.detail = { runId: null, run: null, loading: false, error: null, local: null };
  state.usage = { data: null, loading: false, error: null, loaded: false };
  state.settings = { catalog: null, policy: null, draft: null, loading: false, saving: false, error: null, notice: null, loaded: false };
  state.agents = emptyAgents();
  state.repositories = emptyRepositories();
  state.admins = emptyAdmins();
  state.retrigger = emptyRetrigger();
  state.authors = emptyAuthors();
  state.retryChoice = { model: '', effort: '' };
  for (const bucket of BUCKETS) state.buckets[bucket] = emptyBucket(bucket);
  state.retries = { requests: [], loading: false, error: null, loaded: false };
  state.events = [];
  if (stream) {
    stream.close();
    stream = null;
  }
  render();
}

function handleError(error, assign) {
  const described = describeApiError(error);
  if (described.signedOut) {
    toSignIn(described);
    return null;
  }
  if (typeof assign === 'function') assign(described);
  return described;
}

/* ------------------------------------------------------------------ loading */

async function loadBucketPage(bucket, { more = false } = {}) {
  const model = state.buckets[bucket];
  if (model.loading) return;
  const sources = more ? pendingSources(model.sources) : model.sources.map((s) => ({ ...s, nextOffset: 0, done: false }));
  if (more && sources.length === 0) return;
  model.loading = true;
  model.error = null;
  render();
  try {
    const pages = await Promise.all(sources.map((source) => api.runs({
      status: source.status,
      limit: PAGE_SIZE,
      offset: more ? source.nextOffset : 0,
    })));
    let runs = more ? model.runs : [];
    sources.forEach((source, index) => {
      const page = pages[index] || {};
      const rows = Array.isArray(page.runs) ? page.runs : [];
      runs = mergeRuns(runs, rows);
      model.sources = applySourcePage(model.sources, source.status, page.next_offset);
    });
    model.runs = runs;
    model.loaded = true;
  } catch (error) {
    handleError(error, (described) => {
      model.error = described;
    });
  } finally {
    model.loading = false;
    render();
  }
}

/** Refresh keeps the paging cursors and re-reads only the newest page, which is
 *  what an event burst actually invalidates. */
async function refreshBucket(bucket) {
  const model = state.buckets[bucket];
  if (!model.loaded || model.loading) return;
  try {
    const pages = await Promise.all(
      bucketStatuses(bucket).map((status) => api.runs({ status, limit: PAGE_SIZE, offset: 0 })),
    );
    let runs = model.runs;
    pages.forEach((page) => {
      const rows = Array.isArray(page && page.runs) ? page.runs : [];
      runs = mergeRuns(runs, rows);
    });
    // Drop rows that no longer belong to this bucket (running -> failed).
    model.runs = runs.filter((run) => bucketAccepts(bucket, run));
    model.error = null;
  } catch (error) {
    handleError(error, (described) => {
      model.error = described;
    });
  }
  render();
}

async function loadRetries() {
  if (state.retries.loading) return;
  state.retries.loading = true;
  try {
    const data = await api.retries({ limit: PAGE_SIZE });
    const rows = Array.isArray(data && data.requests) ? data.requests : [];
    state.retries.requests = applyRetryUpdates(state.retries.requests, rows);
    state.retries.loaded = true;
    state.retries.error = null;
  } catch (error) {
    handleError(error, (described) => {
      state.retries.error = described;
    });
  } finally {
    state.retries.loading = false;
    render();
  }
}

async function loadRun(runId, { silent = false } = {}) {
  if (state.detail.runId !== runId) {
    state.detail = { runId, run: findRun(knownRuns(), runId), loading: true, error: null, local: null };
  } else {
    state.detail.loading = true;
  }
  if (!silent) render();
  try {
    const data = await api.run(runId);
    const run = data && data.run ? data.run : null;
    if (state.detail.runId !== runId) return;
    state.detail.run = run;
    state.detail.error = run ? null : describeApiError({ status: 404 });
    // Judge the retry combobox's catalog need on the run that actually loaded.
    if (shouldLoadCatalog(state.settings, isRetryable(run))) loadSettings({ keepDraft: true });
    for (const bucket of BUCKETS) {
      const model = state.buckets[bucket];
      if (run && model.loaded && findRun(model.runs, runId)) model.runs = upsertRun(model.runs, run);
    }
  } catch (error) {
    if (state.detail.runId !== runId) return;
    handleError(error, (described) => {
      state.detail.error = described;
    });
  } finally {
    if (state.detail.runId === runId) state.detail.loading = false;
    render();
  }
}

async function loadUsage() {
  if (state.usage.loading) return;
  state.usage.loading = true;
  state.usage.error = null;
  render();
  try {
    const data = await api.usage();
    state.usage.data = projectUsage(data);
    state.usage.loaded = true;
  } catch (error) {
    handleError(error, (described) => {
      state.usage.error = described;
    });
  } finally {
    state.usage.loading = false;
    render();
  }
}

/* -------------------------------------------------------------- coalescers */

const refreshers = {
  active: createCoalescer(() => refreshBucket('active')),
  attention: createCoalescer(() => refreshBucket('attention')),
  history: createCoalescer(() => refreshBucket('history')),
  retries: createCoalescer(() => loadRetries()),
  detail: createCoalescer(() => (state.detail.runId ? loadRun(state.detail.runId, { silent: true }) : null)),
  retrigger: createCoalescer(() => (retriggerRefreshWanted(state.route, state.retrigger) ? loadRetrigger(state.retrigger.target, { silent: true }) : null)),
  usage: createCoalescer(() => loadUsage(), { waitMs: 2000 }),
  // Re-read the policy in effect (draft kept) once it is held at all.
  settings: createCoalescer(() => (state.settings.loaded ? loadSettings({ keepDraft: true }) : null)),
  agents: createCoalescer(() => (state.agents.loaded ? loadAgentSettings({ keepDrafts: true }) : null)),
  repositories: createCoalescer(() => (state.repositories.loaded ? loadRepositorySettings() : null)),
  // Also while the first load runs: the change it announces may postdate that read.
  admins: createCoalescer(() => (state.admins.loaded || state.admins.loading ? loadAdminSettings() : null)),
  // A grant or revocation changes this session's own role without a new sign-in.
  identity: createCoalescer(() => refreshIdentity()),
  // Also while the first load runs: the change it announces may postdate that read.
  authors: createCoalescer(() => (authorsRefreshDue(state.authors) ? loadAuthorSettings() : null)),
};

function scheduleQueueRefresh() {
  for (const bucket of BUCKETS) {
    if (state.buckets[bucket].loaded) refreshers[bucket].schedule();
  }
}

async function loadSettings({ keepDraft = false } = {}) {
  const model = state.settings;
  if (model.loading) {
    // A route change may land while a fetch is in flight; the view still swaps.
    render();
    return;
  }
  model.loading = true;
  model.error = null;
  render();
  try {
    const [catalog, policy] = await Promise.all([api.models(), api.modelPolicy()]);
    model.catalog = projectCatalog(catalog);
    model.policy = projectPolicyPayload(policy);
    if (!keepDraft || !model.draft) model.draft = draftFromPolicy(model.policy.policy);
    model.loaded = true;
  } catch (error) {
    handleError(error, (described) => {
      model.error = described;
    });
  } finally {
    model.loading = false;
    render();
  }
}

const {
  loadAgentSettings, editAgentPrompt, editAgentRequired, editAgentModel, saveAgent, resetAgent,
  discardSupersededPrompt, keepSupersededPrompt, rollbackSupersededPrompt, putBackStashedPrompt, dropStashedPrompt,
} = createAgentHandlers({
  getModel: () => state.agents, api, render, handleError, editingPrompt, releasePromptFocus,
});

function applyRepositoryPayload(model, payload) {
  const projected = projectRepositorySettings(payload);
  model.rows = projected.repositories;
  model.allowedOwners = projected.allowed_owners;
  model.maxAdded = projected.max_added;
  model.note = projected.note;
  model.configError = projected.error;
  model.draft = nextRepositoryDraft(model.draft, model.draftTouched, model.allowedOwners);
  model.loaded = true;
}

async function loadRepositorySettings() {
  const model = state.repositories;
  if (model.loading) return;
  model.loading = true;
  model.error = null;
  render();
  try {
    applyRepositoryPayload(model, await api.repositorySettings());
  } catch (error) {
    handleError(error, (described) => {
      model.error = described;
    });
  } finally {
    model.loading = false;
    render();
  }
}

function applyAdminPayload(model, payload) {
  const projected = projectAdminSettings(payload);
  model.owner = projected.owner;
  model.rows = projected.admins;
  model.canEdit = projected.can_edit;
  model.maxAdded = projected.max_added;
  model.storeError = projected.error;
  model.loaded = true;
}

async function loadAdminSettings() {
  const model = state.admins;
  // A read already in flight is marked stale instead, and reads once more.
  if (!model.sync.startRead()) return;
  model.loading = true;
  model.error = null;
  render();
  let again = false;
  try {
    const payload = await api.adminSettings();
    // Not when an add/remove answer was applied meanwhile: this list is older.
    if (model.sync.canApplyRead()) applyAdminPayload(model, payload);
  } catch (error) {
    handleError(error, (described) => {
      model.error = described;
    });
  } finally {
    model.loading = false;
    again = model.sync.finishRead();
    render();
  }
  if (again && state.admins === model) loadAdminSettings();
}

/** Re-read /api/me. A revoked admin answers 401 (handleError signs the tab out);
 *  a changed role or owner flag re-runs the route, which re-renders the controls
 *  and moves off a view the role can no longer open. */
async function refreshIdentity() {
  if (state.phase !== 'signed-in') return;
  try {
    const identity = await api.me();
    if (state.phase !== 'signed-in') return;
    const before = state.identity;
    state.identity = identity;
    if (!before || before.role !== identity.role || before.owner !== identity.owner) onRouteChange();
  } catch (error) {
    handleError(error);
  }
}

/** Every author and the feedback that arrived since the newest row shown, or
 *  (`more`) the feedback page below the oldest one; paged by row id (authors.js). */
const loadAuthorSettings = createAuthorLoader({
  getModel: () => state.authors,
  read: async (older, model) => {
    const fetchFeedback = (keys) => api.feedback(keys);
    if (older) return loadOlderFeedback(model, fetchFeedback);
    const [authors, feedback] = await Promise.all([
      loadAllAuthors((offset) => api.authors({ offset })),
      refreshFeedback(model, fetchFeedback),
    ]);
    return { authors: authors.authors, authorsCapped: authors.capped, ...feedback };
  },
  render: () => render(),
  onError: (error, model) => handleError(error, (described) => {
    model.error = described;
  }),
});

/* ----------------------------------------------------------------- actions */

function repositoryRules(model) {
  return { rows: model.rows, allowedOwners: model.allowedOwners, maxAdded: model.maxAdded };
}

/** Typing a repository: state plus the controls that depend on it, like the
 *  prompt editor; a render() per keystroke would rebuild the focused input. */
function editRepositoryDraft(value) {
  const model = state.repositories;
  model.draft = value;
  model.draftTouched = true;
  model.notice = null;
  syncRepositoryForm(repositoryFormState(value, repositoryRules(model), { busy: model.saving !== null }));
}

async function mutateRepositories(saving, action, request, notice) {
  const model = state.repositories;
  if (model.saving) return;
  model.saving = saving;
  model.actionError = null;
  model.notice = null;
  render();
  try {
    const payload = await request();
    applyRepositoryPayload(model, payload);
    model.notice = notice(payload || {});
    if (action === 'add') {
      model.draftTouched = false;
      model.draft = initialRepositoryDraft(model.allowedOwners);
    }
  } catch (error) {
    handleError(error, (described) => {
      model.actionError = repositoryActionError(described, action);
    });
  } finally {
    model.saving = null;
    render();
  }
}

function addRepository() {
  const model = state.repositories;
  const form = repositoryFormState(model.draft, repositoryRules(model), { busy: model.saving !== null });
  if (!form.canAdd) return;
  mutateRepositories('add', 'add', () => api.addRepository(form.repo),
    (payload) => addedNotice(typeof payload.added === 'string' ? payload.added : form.repo, model.note));
}

function removeRepository(repo) {
  mutateRepositories(repo, 'remove', () => api.removeRepository(repo),
    (payload) => removedNotice(typeof payload.removed === 'string' ? payload.removed : repo));
}

/** Typing an address: state plus the controls that depend on it, like the
 *  repository input; a render() per keystroke would rebuild the focused input. */
function editAdminDraft(value) {
  const model = state.admins;
  model.draft = value;
  model.notice = null;
  syncAdminForm(adminFormState(value, adminRules(model), { busy: model.saving !== null }));
}

async function mutateAdmins(saving, action, request, notice) {
  const model = state.admins;
  if (model.saving) return;
  model.saving = saving;
  model.actionError = null;
  model.notice = null;
  render();
  let readNow = false;
  try {
    const payload = await request();
    applyAdminPayload(model, payload);
    // This answer may be older than a list another owner tab wrote since:
    // read once more (after the read in flight, if there is one).
    readNow = model.sync.applyMutation();
    model.notice = notice(payload || {});
    if (action === 'add') model.draft = '';
  } catch (error) {
    handleError(error, (described) => {
      model.actionError = adminActionError(described, action);
    });
  } finally {
    model.saving = null;
    render();
  }
  if (readNow && state.admins === model) loadAdminSettings();
}

function addAdmin() {
  const model = state.admins;
  const form = adminFormState(model.draft, adminRules(model), { busy: model.saving !== null });
  if (!form.canAdd) return;
  mutateAdmins('add', 'add', () => api.addAdmin(form.email),
    (payload) => addedAdminNotice(typeof payload.added === 'string' ? payload.added : form.email));
}

function removeAdmin(email) {
  mutateAdmins(email, 'remove', () => api.removeAdmin(email),
    (payload) => removedAdminNotice(typeof payload.removed === 'string' ? payload.removed : email));
}

function editPolicyDraft(change) {
  const model = state.settings;
  if (!model.draft) return;
  const draft = model.draft;
  if (change.kind === 'model') model.draft = setDraftModel(draft, change.model, model.catalog);
  else if (change.kind === 'effort') model.draft = { ...draft, effort: change.effort };
  else if (change.kind === 'fallback') model.draft = setDraftFallback(draft, change.index, change.model);
  else if (change.kind === 'add') model.draft = addDraftFallback(draft);
  else if (change.kind === 'remove') model.draft = removeDraftFallback(draft, change.index);
  else if (change.kind === 'move') model.draft = moveDraftFallback(draft, change.index, change.delta);
  model.notice = null;
  render();
}

async function savePolicy() {
  const model = state.settings;
  if (!model.draft || model.saving) return;
  model.saving = true;
  model.error = null;
  model.notice = null;
  render();
  try {
    const payload = await api.putModelPolicy(policyRequestBody(model.draft));
    model.policy = projectPolicyPayload(payload);
    model.draft = draftFromPolicy(model.policy.policy);
    model.notice = 'Policy saved. The next review attempt uses it.';
  } catch (error) {
    handleError(error, (described) => {
      model.error = described;
    });
  } finally {
    model.saving = false;
    render();
  }
}

async function resetPolicy() {
  const model = state.settings;
  if (model.saving) return;
  model.saving = true;
  model.error = null;
  model.notice = null;
  render();
  try {
    const payload = await api.deleteModelPolicy();
    model.policy = projectPolicyPayload(payload);
    model.draft = draftFromPolicy(model.policy.policy);
    model.notice = 'Dashboard policy removed. The service is back on its config default.';
  } catch (error) {
    handleError(error, (described) => {
      model.error = described;
    });
  } finally {
    model.saving = false;
    render();
  }
}

function setRetryChoice(choice) {
  state.retryChoice = { model: choice && choice.model ? choice.model : '', effort: choice && choice.effort ? choice.effort : '' };
  render();
}

/** `scope` is the button pressed: `failed` re-runs only the agents the attempt
 *  kept no result for, `all` every agent (retryChoices). */
async function dispatchRetry(scope = null) {
  const run = state.detail.run;
  if (!run) return;
  state.detail.local = { phase: 'dispatching' };
  render();
  try {
    const choice = state.retryChoice;
    const override = choice && choice.model
      ? { model: choice.model, effort: choice.effort || '', fallback_models: [] }
      : null;
    // The buttons pass a fixed scope (retryChoices); the server validates it.
    const data = await api.retry(run.id, run.head_sha, override, scope);
    const request = data && data.request ? data.request : null;
    state.detail.local = null;
    // The pin was for this one dispatch; a later retry starts from the service policy again.
    state.retryChoice = { model: '', effort: '' };
    // The 202 body is fresher than anything cached, including a request the
    // server deduplicated onto a sibling attempt of the same commit.
    if (request) state.retries.requests = applyRetryUpdates(state.retries.requests, [request]);
    refreshers.retries.schedule();
  } catch (error) {
    const described = describeApiError(error);
    // Only a proven session loss (401) may discard the page; a 403 CSRF/origin
    // refusal must leave the operator looking at the error on the retry panel.
    if (described.signedOut) {
      toSignIn(described);
      return;
    }
    state.detail.local = { phase: 'rejected', error: described };
  }
  render();
}

/** The Retrigger page's facts for `target` (`{ repo, pr }`). `silent` keeps the
 *  card up while a stream-driven refresh re-reads the server.
 *
 *  The synchronous "begin, assign, render the loading state" step stays
 *  here (it owns `state` and `render`); the async settling against the
 *  generation counter - the part a real race needs to exercise - is
 *  `settleRetriggerLoad` (retrigger.js), DOM-free and so importable in a
 *  node integration test, unlike this module. */
async function loadRetrigger(target, { silent = false } = {}) {
  // A -> B -> A back to the same target is a new generation (beginRetriggerLoad),
  // so a slow response from the first A visit cannot be mistaken for a fresher
  // one just because the target string matches again (isCurrentRetriggerLoad
  // checks the live model's generation too, at settlement time).
  if (!silent) acknowledgeRefusal(state.retrigger);
  const { state: next, generation } = beginRetriggerLoad(state.retrigger, target);
  if (!next) {
    if (!silent) render();
    return;
  }
  state.retrigger = next;
  if (!silent) render();
  const outcome = await settleRetriggerLoad(target, generation, () => state.retrigger, api, describeApiError);
  if (!outcome.stale && outcome.error && outcome.error.signedOut) {
    toSignIn(outcome.error);
    return;
  }
  // A refresh asked for while this GET was in flight runs now, so an older
  // response cannot leave the page on activity a stream event already ended.
  if (outcome.again) refreshers.retrigger.schedule();
  render();
}

/** Queue a rereview of the head the page showed; the server re-reads GitHub
 *  and refuses when the head moved or a review is already queued or running.
 *
 *  As with `loadRetrigger`, the DOM-touching parts (render, the queue/retries
 *  side effects) stay here; `settleRetriggerDispatch` (retrigger.js) is the
 *  async POST-and-settle step an integration test drives directly. */
async function dispatchRetrigger() {
  const model = state.retrigger;
  // settleRetriggerDispatch also refuses while a previous dispatch is still
  // in flight (canDispatchRetrigger), so two immediate calls (a double
  // click, two bound handlers) produce exactly one POST. Calling it here
  // (not awaiting yet) runs its synchronous prefix - the check and setting
  // `model.local = { phase: 'dispatching' }` - before the render below, so
  // the busy state shows immediately; the actual POST is the first await
  // inside it.
  const pending = settleRetriggerDispatch(model, api, describeApiError);
  render();
  const outcome = await pending;
  if (!outcome) return;
  if (outcome.error) {
    if (outcome.error.signedOut) {
      toSignIn(outcome.error);
      return;
    }
    // A refusal usually means the page is stale (head moved, something queued).
    refreshers.retrigger.schedule();
    render();
    return;
  }
  // The 202 body is fresher than anything cached; the queue view follows it.
  if (outcome.request) state.retries.requests = applyRetryUpdates(state.retries.requests, [outcome.request]);
  refreshers.retries.schedule();
  refreshers.retrigger.schedule();
  render();
}

function navigate(route) {
  const hash = serializeHash(route);
  if (window.location.hash === hash) {
    state.route = parseHash(hash);
    onRouteChange();
    return;
  }
  window.location.hash = hash;
}

/** Filter edits replace the history entry instead of pushing one, so Back leaves
 *  the queue rather than walking backwards through keystrokes - while the final
 *  filter state still lives in the URL for the Back trip out of a run detail. */
function updateFilters({ repo, query }) {
  const next = { ...state.route, repo, query };
  const hash = serializeHash(next);
  window.history.replaceState(null, '', hash);
  state.route = parseHash(hash);
  render();
}

async function signOut() {
  try {
    await api.logout();
  } catch (error) {
    // A failed logout still means this tab must stop showing operational data.
  }
  toSignIn({ title: 'Signed out', detail: null, recovery: 'Sign in again to continue.' });
}

/* ------------------------------------------------------------------ render */

function captureFocus() {
  const active = document.activeElement;
  if (!active || !active.id) return null;
  const snapshot = { id: active.id, start: null, end: null };
  if (typeof active.selectionStart === 'number') {
    snapshot.start = active.selectionStart;
    snapshot.end = active.selectionEnd;
  }
  return snapshot;
}

function restoreFocus(snapshot) {
  if (!snapshot) return;
  const node = document.getElementById(snapshot.id);
  if (!node) return;
  node.focus();
  if (snapshot.start !== null && typeof node.setSelectionRange === 'function') {
    try {
      node.setSelectionRange(snapshot.start, snapshot.end);
    } catch {
      /* inputs that do not support selection ranges are fine as-is */
    }
  }
}

function renderShellChrome() {
  const signedIn = state.phase === 'signed-in';
  dom.session.hidden = !signedIn;
  dom.nav.hidden = !signedIn;
  if (!signedIn) return;

  dom.email.textContent = (state.identity && state.identity.email) || '';
  const roleLabel = describeRole(state.identity);
  const roleNode = document.getElementById('session-role');
  if (roleNode) {
    roleNode.textContent = roleLabel;
    roleNode.hidden = !roleLabel;
  }
  const visible = new Set(visibleViews(state.identity));
  for (const [view, node] of Object.entries(dom.navItems)) {
    if (node) node.hidden = !visible.has(view);
  }
  for (const bucket of BUCKETS) {
    const model = state.buckets[bucket];
    dom.counts[bucket].textContent = model.loaded
      ? formatCount(model.runs.length, hasMore(model.sources))
      : '';
  }
  dom.counts.accounts.textContent = state.usage.loaded && state.usage.data && state.usage.data.available
    ? formatCount(state.usage.data.accounts.length, false)
    : '';
  const agentsBadge = agentsNavBadge(state.agents.rows);
  dom.counts.agents.textContent = agentsBadge.text;
  if (agentsBadge.label) {
    // role="img" makes the [!] glyph itself the accessible name's replaced
    // content, so a screen reader reads the label instead of the bare glyph.
    dom.counts.agents.setAttribute('role', 'img');
    dom.counts.agents.setAttribute('aria-label', agentsBadge.label);
    dom.counts.agents.title = agentsBadge.label;
  } else {
    dom.counts.agents.removeAttribute('role');
    dom.counts.agents.removeAttribute('aria-label');
    dom.counts.agents.title = '';
  }
  dom.counts.settings.textContent = (state.settings.loaded && state.settings.policy && state.settings.policy.source === 'dashboard')
    || state.repositories.rows.some((row) => row.source === 'dashboard')
    ? 'custom'
    : '';

  const activeView = state.route.view === 'run' ? state.route.from : state.route.view;
  for (const [name, node] of Object.entries(dom.navItems)) {
    if (name === activeView) node.setAttribute('aria-current', 'page');
    else node.removeAttribute('aria-current');
  }

  const described = describeFreshness(state.streamSnapshot, {
    now: Date.now(),
    relative: (at, now) => relativeFromEpochMillis(at, now),
  });
  state.streamDescribed = described;
  dom.streamLabel.textContent = described.label;
  dom.streamDetail.textContent = described.detail;
  dom.streamLabel.parentElement.dataset.phase = described.phase;
}

function renderMain() {
  if (state.phase === 'booting') {
    replace(dom.main, []);
    return;
  }
  if (state.phase !== 'signed-in') {
    replace(dom.main, [renderSignIn({
      reason: state.signInReason,
      // A status link or a Retrigger badge brought the reader here signed out;
      // the OAuth round trip returns to "/" and boot() restores this hash.
      onSignIn: () => rememberReturnHash(safeSessionStorage(), window.location.hash),
    })]);
    return;
  }

  const route = state.route;
  if (route.view === 'accounts') {
    replace(dom.main, [renderAccounts({
      usage: state.usage.data,
      loading: state.usage.loading,
      error: state.usage.error,
      onReload: () => loadUsage(),
    })]);
    return;
  }

  if (route.view === 'agents') {
    replace(dom.main, [renderAgents({
      model: state.agents,
      catalog: state.settings.catalog,
      policy: state.settings.policy ? state.settings.policy.policy : null,
      readOnly: !canAct(state.identity),
      // Only a failed load: a Settings save error also lands in settings.error.
      modelsError: state.settings.loaded ? null : state.settings.error,
      modelsLoading: state.settings.loading,
      onPromptInput: editAgentPrompt,
      onRequired: editAgentRequired,
      onModel: editAgentModel,
      onSave: saveAgent,
      onReset: resetAgent,
      onDiscard: discardSupersededPrompt,
      onRollback: rollbackSupersededPrompt,
      onKeep: keepSupersededPrompt,
      onPutBack: putBackStashedPrompt,
      onDropStash: dropStashedPrompt,
      onReload: () => loadAgentSettings(),
      onReloadModels: () => loadSettings({ keepDraft: true }),
    })]);
    return;
  }

  if (route.view === 'settings') {
    const model = state.settings;
    replace(dom.main, [renderSettings({
      settings: model.policy,
      catalog: model.catalog,
      draft: model.draft,
      loading: model.loading,
      saving: model.saving,
      error: model.error,
      notice: model.notice,
      onDraft: editPolicyDraft,
      onSave: () => savePolicy(),
      onReset: () => resetPolicy(),
      onReload: () => loadSettings(),
      readOnly: !canAct(state.identity),
      repositories: {
        model: state.repositories,
        onDraft: editRepositoryDraft,
        onAdd: addRepository,
        onRemove: removeRepository,
        onReload: () => loadRepositorySettings(),
      },
      admins: {
        model: state.admins,
        onDraft: editAdminDraft,
        onAdd: addAdmin,
        onRemove: removeAdmin,
        onReload: () => loadAdminSettings(),
      },
      authors: {
        model: state.authors,
        onReload: () => loadAuthorSettings(),
        onMoreFeedback: () => loadAuthorSettings({ more: true }),
      },
    })]);
    return;
  }

  if (route.view === 'run') {
    const runs = knownRuns();
    const run = state.detail.run;
    // Dispatch state is looked up by target (repo + pr + head), because the store
    // deduplicates on the target: a sibling attempt of the same commit shares the
    // request, and matching on retry_of alone hid it here.
    const request = run ? activeRetryFor(state.retries.requests, run) : null;
    const latest = run ? (request || latestRetryFor(state.retries.requests, run)) : null;
    replace(dom.main, [renderRunDetail({
      run,
      route,
      retryState: retryPanelState({ run, request: latest, local: state.detail.local }),
      parentRun: run && run.retry_of ? findRun(runs, run.retry_of) : null,
      childRun: run ? retryAttemptFor(runs, state.retries.requests, run) : null,
      events: run ? eventsForRun(state.events, run.id) : [],
      sessionStartedAt: state.sessionStartedAt,
      streamPhase: state.streamDescribed.label,
      loading: state.detail.loading,
      error: state.detail.error,
      catalog: state.settings.catalog,
      policy: state.settings.policy ? state.settings.policy.policy : null,
      retryChoice: state.retryChoice,
      onRetryChoice: setRetryChoice,
      onLoadCatalog: () => loadSettings({ keepDraft: true }),
      catalogLoading: state.settings.loading,
      canRetry: canAct(state.identity),
      onBack: () => navigate(backRoute(route)),
      onRetry: (scope) => dispatchRetry(scope),
      onReload: () => loadRun(route.runId),
    })]);
    return;
  }

  if (route.view === 'retrigger') {
    const model = state.retrigger;
    const target = model.target || route.target;
    // Dispatch state by target (repo + pr + head), like the attempt page: the
    // store deduplicates requests on it, so this is where the 202 row lands.
    const identity = retriggerTarget(model.info);
    const request = identity
      ? (activeRetryFor(state.retries.requests, identity) || latestRetryFor(state.retries.requests, identity))
      : null;
    const panel = retriggerPanelState({ info: model.info, request, local: model.local, canAct: canAct(state.identity) });
    replace(dom.main, [renderRetrigger({
      target,
      info: model.info,
      route,
      state: panel,
      childRun: retriggerChildRun(panel.request),
      loading: model.loading,
      error: model.error,
      canAct: canAct(state.identity),
      onRetrigger: () => dispatchRetrigger(),
      onReload: () => loadRetrigger(target),
    })]);
    return;
  }

  const bucket = currentBucket() || 'active';
  const model = state.buckets[bucket];
  replace(dom.main, [renderQueue({
    bucket,
    route,
    runs: model.runs,
    loading: model.loading,
    error: model.error,
    retryRunIds: runIdsWithActiveRetry(state.retries.requests, model.runs),
    hasMore: hasMore(model.sources),
    onFilters: updateFilters,
    onLoadMore: () => loadBucketPage(bucket, { more: true }),
    onRetryLoad: () => loadBucketPage(bucket),
  })]);
}

/** A focused prompt editor is never rebuilt: that would cancel IME composition
 *  and wipe its undo history. Its draft is in state, so a later render catches up. */
function editingPrompt() {
  if (state.phase !== 'signed-in' || state.route.view !== 'agents') return false;
  const active = document.activeElement;
  return !!active && !!active.classList && active.classList.contains('agent__textarea') && dom.main.contains(active);
}

/** Blur the focused prompt editor when it belongs to one of `agentIds` (a read
 *  set its unsaved text aside), so the next render() redraws its card. */
function releasePromptFocus(agentIds) {
  const active = document.activeElement;
  if (editingPrompt() && agentIds.some((agentId) => active.id === `agent-${agentId}-prompt`)) active.blur();
}

/** Set when render() skipped <main> for a focused prompt; the blur catches up. */
let promptRenderDeferred = false;
const gestures = createGestureGate(window);

function render() {
  if (editingPrompt()) {
    // Not even restoreFocus: resetting the selection would break IME input too.
    renderShellChrome();
    promptRenderDeferred = true;
    return;
  }
  const focus = captureFocus();
  const openDetails = captureOpenDetails(dom.main);
  renderShellChrome();
  // Event-driven renders land every few seconds during a review; keep a reader's
  // place inside a long prompt.
  const scrolls = [...dom.main.querySelectorAll('.agent__textarea')]
    .filter((node) => node.id && node.scrollTop > 0)
    .map((node) => [node.id, node.scrollTop]);
  renderMain();
  // Reopen first: a textarea inside a closed <details> has no box to scroll or focus.
  restoreOpenDetails(dom.main, openDetails);
  for (const [id, top] of scrolls) {
    const node = document.getElementById(id);
    if (node) node.scrollTop = top;
  }
  restoreFocus(focus);
}

/** Event-driven renders go through a frame, so a reconnect replay of hundreds of
 *  events rebuilds <main> once instead of once per event. Operator-initiated state
 *  keeps calling render() directly - a click must not wait for a frame. */
const renderScheduler = createRenderScheduler(render);

/* ------------------------------------------------------------------ routing */

function onRouteChange() {
  const route = state.route;
  const key = `${route.view}:${route.runId || ''}`;
  const changed = key !== state.renderedKey;
  state.renderedKey = key;

  if (state.phase !== 'signed-in') {
    render();
    return;
  }

  if (resolveView(state.identity, route.view) !== route.view) {
    // A viewer deep-linking an admin-only view is sent to the queue instead of
    // polling a route that answers 403. Checked before any view loads data.
    navigate({ view: resolveView(state.identity, route.view) });
    return;
  }
  if (route.view === 'accounts') {
    if (!state.usage.loaded) loadUsage();
    else render();
  } else if (route.view === 'agents') {
    // A saved/failed notice describes a past action; do not re-announce it on every visit.
    state.agents.notice = null;
    state.agents.error = null;
    if (!state.agents.loaded) loadAgentSettings();
    // The model control offers the catalog and names the policy's model; a
    // policy draft left on the Settings page is kept.
    if (!state.settings.loaded) loadSettings({ keepDraft: true });
    else render();
  } else if (route.view === 'settings') {
    // A saved/failed notice describes a past action; do not re-announce it on every visit.
    state.settings.notice = null;
    state.settings.error = null;
    state.repositories.notice = null;
    state.repositories.actionError = null;
    state.admins.notice = null;
    state.admins.actionError = null;
    if (!state.repositories.loaded) loadRepositorySettings();
    if (!state.admins.loaded) loadAdminSettings();
    if (!state.authors.loaded) loadAuthorSettings();
    if (!state.settings.loaded) loadSettings();
    else render();
  } else if (route.view === 'run') {
    if (state.detail.runId !== route.runId) {
      state.retryChoice = { model: '', effort: '' };
      loadRun(route.runId);
    } else render();
    if (!state.retries.loaded) loadRetries();
    // The retry panel offers the catalog; load it without touching a draft the
    // operator may be editing on the Settings page. Only a retryable run needs it,
    // and loadRun re-judges this once the run itself has arrived.
    if (shouldLoadCatalog(state.settings, isRetryable(state.detail.run))) loadSettings({ keepDraft: true });
  } else if (route.view === 'retrigger') {
    const current = state.retrigger.target;
    const same = current && current.repo === route.target.repo && current.pr === route.target.pr;
    if (!same) loadRetrigger(route.target);
    else {
      refreshers.retrigger.schedule();
      render();
    }
    // The dispatch state comes from the queue, so the card can link the new attempt.
    if (!state.retries.loaded) loadRetries();
  } else {
    const bucket = currentBucket();
    const model = state.buckets[bucket];
    if (!model.loaded) loadBucketPage(bucket);
    else {
      refreshers[bucket].schedule();
      render();
    }
    if (!state.retries.loaded) loadRetries();
  }

  // Move focus to the region that changed, for keyboard and screen-reader users.
  if (changed && dom.main) dom.main.focus();
}

/* ------------------------------------------------------------------- stream */

function onStreamEvent(event) {
  state.events = pushEvent(state.events, event);

  const change = runChangeFromEvent(event);
  if (change) {
    for (const bucket of BUCKETS) {
      const model = state.buckets[bucket];
      if (!model.loaded) continue;
      const result = applyRunChange(model.runs, bucket, change);
      model.runs = result.runs;
      if (result.missing) refreshers[bucket].schedule();
    }
    if (state.detail.runId && change.runId === state.detail.runId) {
      if (change.kind === 'patch' && state.detail.run) {
        state.detail.run = { ...state.detail.run, ...change.patch };
        // A run open in the tab can fail live; the retry panel then needs the catalog.
        if (shouldLoadCatalog(state.settings, isRetryable(state.detail.run))) loadSettings({ keepDraft: true });
      } else {
        refreshers.detail.schedule();
      }
    }
    // A new attempt can be the child of the open one; its request row links them.
    if (change.kind === 'insert' && state.detail.runId && change.run.retry_of === state.detail.runId) {
      refreshers.retries.schedule();
    }
  }

  if (isRetryEvent(event)) {
    refreshers.retries.schedule();
    scheduleQueueRefresh();
  }
  // A terminal request event can name the very request the Retrigger page's
  // last snapshot still shows as queued; reconcile that synchronously so the
  // panel is not stuck blocked for the round trip the refresh below takes.
  if (state.retrigger.info) {
    state.retrigger.info = reconcileRetriggerInfo(state.retrigger.info, event);
  }
  // The Retrigger page reports what is queued or running for its PR; any
  // attempt or request change can move that, so it re-reads the server.
  if (retriggerRefreshWanted(state.route, state.retrigger) && (change || isRetryEvent(event))) refreshers.retrigger.schedule();
  // Another tab saved or reset the policy: what this page calls "in effect"
  // and what the retry controls offer must follow, without touching a draft.
  if (isSettingEvent(event)) {
    refreshers.settings.schedule();
    refreshers.agents.schedule();
    refreshers.repositories.schedule();
    refreshers.admins.schedule();
  }
  if (isAdminsEvent(event)) refreshers.identity.schedule();
  // An author set a language, got the welcome comment, or sent feedback.
  if (isAuthorEvent(event)) refreshers.authors.schedule();

  // Batched: a replay burst must not rebuild <main> once per event.
  renderScheduler.schedule();
}

function connectStream() {
  if (stream) return;
  stream = openEventStream({
    onUpdate: onStreamEvent,
    onAuthExpired: () => toSignIn({
      title: 'Session expired',
      detail: 'The server closed the event stream because this session is no longer authorised.',
      recovery: 'Sign in again with your Google account.',
    }),
    onState: (snapshot) => {
      const previous = state.streamSnapshot.phase;
      state.streamSnapshot = snapshot;
      renderShellChrome();
      // Coming back from a gap means the durable data, not the stream, is truth.
      if (previous !== 'live' && snapshot.phase === 'live') resyncFromDurableData();
    },
    // `ready` carries the tail this stream started from. Re-read AFTER that anchor
    // exists: the initial hydrate may have completed before the server snapshotted
    // the tail, and a transition in that window is never streamed to us.
    onReady: () => resyncFromDurableData(),
    // Heartbeat overdue: the socket looked open but nothing arrived.
    onStale: () => renderShellChrome(),
  });
}

/** Everything the stream could have missed is re-read from the API. */
function resyncFromDurableData() {
  scheduleQueueRefresh();
  refreshers.retries.schedule();
  if (state.detail.runId) refreshers.detail.schedule();
  if (retriggerRefreshWanted(state.route, state.retrigger)) refreshers.retrigger.schedule();
  if (state.route.view === 'accounts') refreshers.usage.schedule();
  refreshers.settings.schedule();
  refreshers.agents.schedule();
  refreshers.repositories.schedule();
  refreshers.admins.schedule();
  refreshers.identity.schedule();
  refreshers.authors.schedule();
}

/** A laptop that slept wakes with a stream that looks open and is not. */
function onWindowFocus() {
  if (state.phase !== 'signed-in') return;
  if (stream) stream.reconnectNow();
  resyncFromDurableData();
}

/* --------------------------------------------------------------------- boot */

async function boot() {
  // Before api.me(), so the sign-in page names the version too. A failure keeps
  // the static text: the footer is informational, and deploy checks /healthz.
  api.health()
    .then((health) => { dom.footerVersion.textContent = footerText(health && health.version); })
    .catch(() => {});
  clear(dom.main);
  window.addEventListener('hashchange', () => {
    state.route = parseHash(window.location.hash);
    onRouteChange();
  });
  dom.signout.addEventListener('click', signOut);
  // A refresh or save answer that arrived while a prompt had focus lands on blur,
  // after any click that caused the blur (createGestureGate).
  document.addEventListener('focusout', (event) => {
    if (!promptRenderDeferred || !event.target.classList || !event.target.classList.contains('agent__textarea')) return;
    promptRenderDeferred = false;
    gestures.whenIdle(() => renderScheduler.schedule());
  });
  window.addEventListener('focus', onWindowFocus);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') onWindowFocus();
  });

  try {
    const identity = await api.me();
    state.identity = identity;
    state.phase = 'signed-in';
  } catch (error) {
    const described = describeApiError(error);
    toSignIn(described.status === 401
      ? { title: 'Not signed in', detail: null, recovery: 'Sign in with your Google account.' }
      : described);
    return;
  }

  // Back from Google on "/": land on the page the reader left for sign-in
  // (a status link, a Retrigger badge). An explicit hash in the URL wins.
  const restored = returnHashAfterSignIn(window.location.hash, takeReturnHash(safeSessionStorage()));
  if (restored) {
    window.history.replaceState(null, '', restored);
    state.route = parseHash(restored);
  }

  connectStream();
  // The indicator ages ("last event 3m ago", heartbeat overdue) without any new
  // event arriving, so the chrome is re-rendered on a slow tick.
  window.setInterval(() => {
    if (state.phase === 'signed-in') renderShellChrome();
  }, 10000);
  onRouteChange();
}

boot();
