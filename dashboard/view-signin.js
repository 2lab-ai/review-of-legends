/** Public shell. Rendered before any authenticated request succeeds and again
 *  whenever the session goes away (401/403, or the stream's auth-expired event).
 *  It contains no operational data at all - only the sign-in path.
 *
 *  `onSignIn` fires as the operator leaves for Google: app.js remembers the
 *  page they were on (a `#run/...` status link, a `#retrigger/...` badge), since
 *  the OAuth round trip comes back to `/` without a hash. */

import { el } from './dom.js';

export function renderSignIn({ reason = null, email = null, onSignIn = null } = {}) {
  return el('section', { class: 'panel panel--signin', 'aria-labelledby': 'signin-heading' }, [
    el('h1', { class: 'panel__title', id: 'signin-heading', text: 'Sign in required' }),
    reason
      ? el('p', { class: 'notice', role: 'status' }, [
        el('strong', { text: reason.title }),
        reason.detail ? el('span', { class: 'notice__detail', text: reason.detail }) : null,
      ])
      : null,
    el('p', {
      class: 'prose',
      text:
        'This console shows live and past review attempts for the Review of Legends service, '
        + 'and can dispatch a retry of a failed review. Accounts of the Google Workspace domains this deployment allows '
        + 'can sign in read-only; retries and settings are reserved for the admin account.',
    }),
    el('a', {
      class: 'button button--primary',
      href: '/auth/login',
      text: 'Sign in with Google',
      on: typeof onSignIn === 'function' ? { click: () => onSignIn() } : null,
    }),
    email
      ? el('p', {
        class: 'prose prose--quiet',
        text: `Signed in as ${email}, which is not authorised for this service.`,
      })
      : null,
    el('p', {
      class: 'prose prose--quiet',
      text: 'No review data is loaded on this page until the server has authorised the session.',
    }),
  ]);
}
