/** Session roles as the server reports them on /api/me.
 *
 *  `admin` can dispatch retries, edit the model policy and read llmux account
 *  usage. `viewer` (any verified account of a configured Workspace domain) reads the
 *  queue, attempt detail and the live stream only. Admin is the owner plus every
 *  address the owner granted it on the Settings page; `owner` alone may change
 *  that list. The role is re-derived per request, so it can change mid-session.
 *  The server enforces all of this; the client merely hides what would be refused. */

export const ADMIN_ROLE = 'admin';
export const VIEWER_ROLE = 'viewer';

export function roleOf(identity) {
  const role = identity && typeof identity.role === 'string' ? identity.role : null;
  return role === ADMIN_ROLE || role === VIEWER_ROLE ? role : null;
}

export function canAct(identity) {
  return roleOf(identity) === ADMIN_ROLE;
}

/** The owner: an admin who may also grant and revoke admin. */
export function isOwner(identity) {
  return canAct(identity) && identity.owner === true;
}

export function describeRole(identity) {
  const role = roleOf(identity);
  if (isOwner(identity)) return 'owner';
  if (role === ADMIN_ROLE) return 'admin';
  if (role === VIEWER_ROLE) return 'read-only';
  return '';
}

/** Navigation entries a role may open; `accounts` reads /api/usage, admin only. */
export function visibleViews(identity) {
  return canAct(identity)
    ? ['active', 'attention', 'history', 'accounts', 'agents', 'settings']
    : ['active', 'attention', 'history', 'agents', 'settings'];
}

/** Every navigation entry that exists; `run` (attempt detail) is routable by
 *  every role but is not a nav entry, so it is deliberately absent here. */
const NAV_VIEWS = ['active', 'attention', 'history', 'accounts', 'agents', 'settings'];

/** The view a role actually lands on: a nav entry the role may not open falls
 *  back to the queue instead of polling a route that answers 403. Views that
 *  are not nav entries (`run`) pass through untouched for every role. */
export function resolveView(identity, view) {
  if (!NAV_VIEWS.includes(view)) return view;
  return visibleViews(identity).includes(view) ? view : 'active';
}
