/** Where to land after signing in.
 *
 *  A commit status links `#run/<id>` and the Retrigger badge links
 *  `#retrigger/owner/name/42`; a signed-out reader who follows one is sent to
 *  Google and comes back to `/` with no hash (the callback redirects there).
 *  The hash is parked in sessionStorage on the way out and taken back once,
 *  on the way in. Storage only ever holds a hash of this origin's own SPA;
 *  `parseHash` still decides whether it names a real page. */

export const RETURN_HASH_KEY = 'rol.return-hash';
const MAX_HASH_CHARS = 512;

/** `window.sessionStorage` itself can throw (a SecurityError, in a sandboxed
 *  iframe or some private-browsing modes) before any method on it is ever
 *  called, so a caller writing `rememberReturnHash(window.sessionStorage, ...)`
 *  throws while evaluating its own arguments - the try/catch inside
 *  `rememberReturnHash`/`takeReturnHash` never runs. Every caller must obtain
 *  storage through this getter instead of touching `window.sessionStorage`
 *  directly. `scope` defaults to `window` and exists so a throwing getter is
 *  testable without touching the real one. */
export function safeSessionStorage(scope = typeof window !== 'undefined' ? window : undefined) {
  try {
    return (scope && scope.sessionStorage) || null;
  } catch {
    return null;
  }
}

function usableHash(value) {
  return typeof value === 'string'
    && value.length >= 2
    && value.length <= MAX_HASH_CHARS
    && value.startsWith('#')
    && !/\s/.test(value);
}

/** Remember `hash` for the next boot; a hash not worth returning to clears any
 *  earlier one. Returns whether something was stored. */
export function rememberReturnHash(storage, hash) {
  if (!storage || typeof storage.setItem !== 'function' || typeof storage.removeItem !== 'function') return false;
  try {
    if (!usableHash(hash)) {
      storage.removeItem(RETURN_HASH_KEY);
      return false;
    }
    storage.setItem(RETURN_HASH_KEY, hash);
    return true;
  } catch {
    return false;
  }
}

/** The remembered hash, or null; always cleared, so it is used at most once. */
export function takeReturnHash(storage) {
  if (!storage || typeof storage.getItem !== 'function' || typeof storage.removeItem !== 'function') return null;
  let value;
  try {
    value = storage.getItem(RETURN_HASH_KEY);
    storage.removeItem(RETURN_HASH_KEY);
  } catch {
    return null;
  }
  return usableHash(value) ? value : null;
}

/** The hash to boot with: the one in the URL when it names a page, else the
 *  remembered one (the OAuth callback lands on `/`), else none. */
export function returnHashAfterSignIn(currentHash, remembered) {
  const current = typeof currentHash === 'string' ? currentHash : '';
  if (current.replace(/^#/, '') !== '') return null;
  return usableHash(remembered) ? remembered : null;
}
