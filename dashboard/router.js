/** Hash routing. Pure string <-> route translation, no history access.
 *
 * Filters live in the hash, so Back out of a run detail lands on the same
 * filtered queue the operator left (api-contract deep-link requirement, and the
 * state-preservation rule from the UI research note).
 *
 *   #active
 *   #history?repo=owner%2Fname&q=timeout
 *   #run/6f1c...?repo=owner%2Fname&q=timeout&from=attention
 *   #retrigger/owner/name/42        (the Retrigger badge on a PR comment)
 */

export const QUEUE_VIEWS = ['active', 'attention', 'history'];
export const VIEWS = [...QUEUE_VIEWS, 'accounts', 'agents', 'settings', 'run', 'retrigger'];
export const DEFAULT_VIEW = 'active';

const ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;
/** GitHub owner (user/org) names: alphanumeric or hyphen, never starting with one. */
const OWNER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9-]{0,99}$/;
/** GitHub repository names: alphanumeric, ".", "_" or "-", in any position -
 *  a real repo may start with a dot (`.github`). */
const REPO_NAME_PATTERN = /^[A-Za-z0-9._-]{1,100}$/;
const PR_PATTERN = /^[1-9][0-9]{0,8}$/;

function parseQuery(text) {
  const params = new Map();
  for (const pair of (text || '').split('&')) {
    if (pair === '') continue;
    const index = pair.indexOf('=');
    const rawKey = index === -1 ? pair : pair.slice(0, index);
    const rawValue = index === -1 ? '' : pair.slice(index + 1);
    let key;
    let value;
    try {
      key = decodeURIComponent(rawKey.replace(/\+/g, ' '));
      value = decodeURIComponent(rawValue.replace(/\+/g, ' '));
    } catch {
      continue;
    }
    if (!params.has(key)) params.set(key, value);
  }
  return params;
}

function decodeSegment(segment, pattern) {
  if (typeof segment !== 'string' || segment === '') return null;
  let decoded;
  try {
    decoded = decodeURIComponent(segment);
  } catch {
    return null;
  }
  if (decoded === '.' || decoded === '..' || !pattern.test(decoded)) return null;
  return decoded;
}

/** `{ repo: 'owner/name', pr: 42 }` from the three segments after `retrigger`, or null.
 *
 *  Owner and repository are validated separately (GitHub's own rules differ):
 *  an owner cannot start with "." or "-", a repository name can ("`.github`").
 *  Both still refuse a literal "." or ".." and a decoded "/" (neither pattern's
 *  character set includes it).
 */
export function parseRetriggerTarget(segments) {
  if (!Array.isArray(segments) || segments.length !== 3) return null;
  const owner = decodeSegment(segments[0], OWNER_PATTERN);
  const name = decodeSegment(segments[1], REPO_NAME_PATTERN);
  const pr = typeof segments[2] === 'string' && PR_PATTERN.test(segments[2]) ? Number(segments[2]) : null;
  if (owner === null || name === null || pr === null) return null;
  return { repo: `${owner}/${name}`, pr };
}

/** Unknown routes fall back to Active rather than rendering a dead page. */
export function parseHash(hash) {
  const raw = typeof hash === 'string' ? hash.replace(/^#/, '') : '';
  const queryIndex = raw.indexOf('?');
  const path = queryIndex === -1 ? raw : raw.slice(0, queryIndex);
  const params = parseQuery(queryIndex === -1 ? '' : raw.slice(queryIndex + 1));
  const repo = params.get('repo') || '';
  const query = params.get('q') || '';
  const fromParam = params.get('from');
  const from = QUEUE_VIEWS.includes(fromParam) ? fromParam : DEFAULT_VIEW;

  const segments = path.split('/');
  if (segments[0] === 'run') {
    const runId = segments[1] || '';
    if (ID_PATTERN.test(runId)) {
      return { view: 'run', runId, repo, query, from };
    }
    return { view: DEFAULT_VIEW, runId: null, repo, query, from };
  }
  if (segments[0] === 'retrigger') {
    const target = parseRetriggerTarget(segments.slice(1));
    if (target) {
      return { view: 'retrigger', runId: null, target, repo, query, from };
    }
    return { view: DEFAULT_VIEW, runId: null, repo, query, from };
  }
  // `run` and `retrigger` returned above, valid target or not.
  const view = VIEWS.includes(segments[0]) ? segments[0] : DEFAULT_VIEW;
  return { view, runId: null, repo, query, from };
}

export function serializeHash(route) {
  let view = route && VIEWS.includes(route.view) ? route.view : DEFAULT_VIEW;
  if (view === 'retrigger' && !(route.target && typeof route.target.repo === 'string' && route.target.repo.includes('/'))) {
    view = DEFAULT_VIEW;
  }
  const params = [];
  const repo = route && typeof route.repo === 'string' ? route.repo : '';
  const query = route && typeof route.query === 'string' ? route.query : '';
  if (repo !== '') params.push(`repo=${encodeURIComponent(repo)}`);
  if (query !== '') params.push(`q=${encodeURIComponent(query)}`);
  if (view === 'run' && route.from && QUEUE_VIEWS.includes(route.from) && route.from !== DEFAULT_VIEW) {
    params.push(`from=${encodeURIComponent(route.from)}`);
  }
  let path = view;
  if (view === 'run') path = `run/${route.runId}`;
  if (view === 'retrigger') {
    const [owner, name] = route.target.repo.split('/');
    path = `retrigger/${encodeURIComponent(owner)}/${encodeURIComponent(name)}/${route.target.pr}`;
  }
  return `#${path}${params.length ? `?${params.join('&')}` : ''}`;
}

export function backRoute(route) {
  return {
    view: (route && QUEUE_VIEWS.includes(route.from) && route.from) || DEFAULT_VIEW,
    runId: null,
    repo: (route && route.repo) || '',
    query: (route && route.query) || '',
  };
}
