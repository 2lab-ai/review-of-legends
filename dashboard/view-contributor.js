/** Contributor page (#contributor): domain verification for the contributor gate.
 *
 *  A signed-in account (its email and Workspace domain already verified by the
 *  Google sign-in) names its GitHub login and gets a one-time code for that
 *  login, then comments `@review-of-legends verify <code>` on its pull request
 *  from that account. Only that login's comment uses the code, so a code read
 *  off a public comment is worthless to anyone else. The daemon binds the
 *  account to this email; a trusted domain then approves its pull requests.
 *  The page is routable by every role and is not a nav entry: the gate comment
 *  on a pull request links here. */

import { el, field } from './dom.js';
import { formatTimestamp } from './format.js';

export function renderContributor({
  email = null,
  login = '',
  code = null,
  codeLogin = null,
  expiresAt = null,
  loading = false,
  error = null,
  onLogin = null,
  onRequest = null,
} = {}) {
  const command = code ? `@review-of-legends verify ${code}` : '';
  return el('section', { class: 'panel', 'aria-labelledby': 'contributor-heading' }, [
    el('h1', { class: 'panel__title', id: 'contributor-heading', text: 'Verify your email for pull request reviews' }),
    el('p', {
      class: 'prose',
      text:
        'Repositories with a contributor gate review pull requests only from approved contributors. '
        + 'An account at a trusted email domain is approved once its GitHub login is bound to the email you signed in with.',
    }),
    email ? field('Signed in as', email) : null,
    el('label', { class: 'field', for: 'contributor-login' }, [
      el('span', { class: 'field__label', text: 'Your GitHub login' }),
      el('input', {
        class: 'input',
        id: 'contributor-login',
        type: 'text',
        value: login,
        autocomplete: 'username',
        spellcheck: 'false',
        placeholder: 'octocat',
        on: typeof onLogin === 'function' ? { input: (event) => onLogin(event.target.value) } : null,
      }),
    ]),
    error ? el('p', { class: 'notice notice--error', role: 'alert', text: error }) : null,
    code
      ? el('div', { class: 'contributor-code' }, [
        el('p', { class: 'prose', text: `Comment this line on your pull request, signed in to GitHub as ${codeLogin || 'that login'}:` }),
        el('pre', { class: 'code-block' }, [el('code', { text: command })]),
        expiresAt ? field('Expires', formatTimestamp(expiresAt)) : null,
        el('p', {
          class: 'prose prose--quiet',
          text: 'The code works once. The bot replies on the pull request; reviews start within a minute when the domain is trusted there.',
        }),
      ])
      : null,
    el('button', {
      class: 'button button--primary',
      type: 'button',
      // Not disabled on an empty login: typing does not re-render (the input
      // would be replaced mid-typing); the request says what is missing.
      disabled: loading,
      text: loading ? 'Getting a code...' : code ? 'Get a new code' : 'Get a verification code',
      on: typeof onRequest === 'function' ? { click: () => onRequest() } : null,
    }),
  ]);
}
