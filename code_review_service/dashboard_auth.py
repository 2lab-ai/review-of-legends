"""Verified Google identity and opaque server-side sessions (W3 of .prd/admin-dashboard/loop.md).

Two responsibilities, both deliberately framework-free so they can be reasoned
about (and tested) without an HTTP stack:

- ``verify_identity`` / ``resolve_session``: the *only* places an identity
  becomes "admin". Sign-in accepts nothing but the claims Authlib already
  validated (issuer, signature, audience, exp, nonce) and then demands a
  nonempty ``sub``, ``email_verified`` that is literally ``True``, and an exact
  lowercased match on the configured ``ROL_DASHBOARD_OWNER`` or on an address the
  owner granted admin on the dashboard. A truthy-but-not-True
  ``email_verified`` (``"true"``, ``1``) is rejected: providers that stringify
  booleans must not be able to promote an unverified mailbox. A grant counts
  only for an address Google is authoritative for (Gmail, or a Workspace
  account with ``hd``), and every non-owner email must be ASCII. Every request
  then re-derives the role from the current grant list, so a revoked grant
  ends the session on its next request.
- ``SessionStore``: opaque random cookie values, revocable server-side rows in a
  private SQLite file. The cookie value never appears in the database (only its
  SHA-256), so a leaked DB file cannot be replayed as a session, and revocation
  (logout, login rotation, expiry) is a row delete rather than a hope that a
  signed token expires.

Deployment environment variables select the owner and viewer domains; they do
not bypass verified Google authentication. With no policy configured no owner or
viewer is authorized; the only inputs are validated OIDC claims, a cookie value and the owner's
grant list (read by the web tier from the dashboard store and passed in here).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional

# The single authorized operator. Exact match after lowercasing; never a domain
# or prefix rule (``owner@company.example.evil.com`` must not pass).
ADMIN_EMAIL = os.environ.get("ROL_DASHBOARD_OWNER", "").strip().lower()
ADMIN_ROLE = "admin"
# Read-only access for the rest of the company: the Google Workspace hosted
# domain (``hd`` claim) must match, not just the address text, so a consumer
# account that merely displays a company-looking email never qualifies.
VIEWER_ROLE = "viewer"
VIEWER_DOMAINS = tuple(d.strip().lower() for d in os.environ.get("ROL_DASHBOARD_VIEWER_DOMAINS", "").split(",") if d.strip())
# The role a session is *stored* with when it signed in through a dashboard
# grant. Each request resolves it to admin while the grant stands
# (resolve_session). A build without grants does not know this role and deletes
# such a session on its first read, so a rollback can never leave a revoked
# grant with an 8-hour admin session; an "admin" row would be trusted there.
GRANTED_ROLE = "granted"
ROLES = (ADMIN_ROLE, VIEWER_ROLE, GRANTED_ROLE)
# Addresses the owner granted admin on the dashboard (store key ``admins``) are
# matched exactly like the owner. RFC 5321 caps a forward path at 254 octets.
MAX_ADMIN_EMAIL_CHARS = 254
# Google is authoritative for who holds an address only for Gmail and for a
# Workspace account (an ``hd`` claim). For any other address ``email_verified``
# can stay true after the mailbox changes hands, so the former holder could
# still sign in with the grant made for the new one.
GOOGLE_AUTHORITATIVE_DOMAIN = "gmail.com"
GRANT_NEEDS_AUTHORITY = (
    "an admin grant needs a Gmail address or a Google Workspace account; "
    "Google does not vouch for who holds this address now"
)

SESSION_COOKIE = "rol_session"
# The framework-signed cookie holds only the temporary OAuth state/nonce/PKCE
# verifier, never a provider token.
OAUTH_STATE_COOKIE = "rol_oauth"
OAUTH_STATE_TTL_SECONDS = 600

SESSION_TTL_SECONDS = 8 * 60 * 60
SESSION_ID_BYTES = 32
CSRF_TOKEN_BYTES = 32

CSRF_HEADER = "x-csrf-token"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id_hash    TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    sub        TEXT NOT NULL,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL,
    role       TEXT NOT NULL DEFAULT 'admin'
);
CREATE INDEX IF NOT EXISTS idx_sessions_email ON sessions (email);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions (expires_at);

CREATE TABLE IF NOT EXISTS oauth_states (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oauth_states_expiry ON oauth_states (expires_at);
"""


class AuthError(Exception):
    """An authentication/authorization failure whose message is safe to return."""

    def __init__(self, message: str = "not authorized") -> None:
        super().__init__(message)
        self.safe_message = message


def _now() -> float:
    """Wall clock, isolated so tests can move time without touching sessions."""
    return time.time()


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _same_text(left: str, right: str) -> bool:
    # Compare bytes: a homoglyph address (cyrillic "о") must fail the check, not
    # raise out of ``compare_digest``'s ASCII-only string path.
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def is_owner(email: Any) -> bool:
    """Whether ``email`` (already lowercased) is the owner: exact, never a prefix or domain."""
    return isinstance(email, str) and bool(email) and _same_text(email, ADMIN_EMAIL)


def _listed(email: str, admin_emails: Iterable[str]) -> bool:
    return bool(email) and any(
        isinstance(candidate, str) and candidate and _same_text(email, candidate) for candidate in admin_emails
    )


def normalize_admin_email(value: Any) -> str:
    """The stored form of an address the owner grants admin, or ``ValueError``.

    Stripped and lowercased; at most 254 characters; exactly one ``@`` with a
    nonempty local part and domain; no whitespace, control, format or separator
    character anywhere (a zero-width or bidi character would make two different
    addresses look identical on the Settings page); ASCII only, checked before
    lowercasing (a Cyrillic letter looks identical too, and ``lower()`` folds
    the Kelvin sign into ``k``; Gmail and Workspace addresses are ASCII). The
    message is safe to return: it never echoes the value.
    """
    if not isinstance(value, str):
        raise ValueError("an admin is written as an email address, e.g. name@example.com")
    stripped = value.strip()
    email = stripped.lower()
    if not email:
        raise ValueError("an admin is written as an email address, e.g. name@example.com")
    if len(email) > MAX_ADMIN_EMAIL_CHARS:
        raise ValueError("an email address is at most %d characters" % MAX_ADMIN_EMAIL_CHARS)
    if any(char.isspace() or unicodedata.category(char)[0] in ("C", "Z") for char in email):
        raise ValueError("an email address cannot contain spaces or invisible characters")
    if not stripped.isascii():
        raise ValueError("an email address can contain only ASCII letters, digits and symbols")
    local, sep, domain = email.partition("@")
    if not sep or not local or not domain or "@" in domain:
        raise ValueError("an email address has exactly one @ with text on both sides, e.g. name@example.com")
    return email


def admin_emails_from_setting(value: Any) -> List[str]:
    """The granted addresses in a stored ``admins`` value; anything malformed grants nothing.

    Only an entry whose ``email`` is already in its stored form counts: a row
    edited by hand to ``Dev@Example.com`` or `` dev@example.com`` is ignored
    rather than normalized into a grant nobody made on the dashboard. The owner
    is never listed (always admin regardless).
    """
    added = value.get("added") if isinstance(value, Mapping) else None
    if not isinstance(added, list):
        return []
    emails = []  # type: List[str]
    for entry in added:
        email = entry.get("email") if isinstance(entry, Mapping) else None
        try:
            normalized = normalize_admin_email(email)
        except ValueError:
            continue
        if normalized == email and not is_owner(normalized) and normalized not in emails:
            emails.append(normalized)
    return emails


def verify_identity(userinfo: Optional[Mapping[str, Any]], admin_emails: Iterable[str] = ()) -> Dict[str, str]:
    """Return ``{email, sub, role}`` for an authorized account, or raise ``AuthError``.

    ``userinfo`` must be the claim set Authlib produced from a validated
    ``id_token`` (``token["userinfo"]``). Claims fetched from a userinfo endpoint
    with a bearer token are *not* acceptable here: the caller must never hand
    them to this function.

    The owner address is matched exactly, and so is every address in
    ``admin_emails`` (the owner's grants, already normalized; the caller reads
    them from the store, so this stays pure), but a grant counts only when
    Google is authoritative for the address: it ends in ``@gmail.com`` or the
    token carries a nonempty ``hd``. Every other account gets the viewer role
    only when its email domain *and* its Workspace ``hd`` claim are one of
    ``VIEWER_DOMAINS``. A non-owner email that is not ASCII is refused before
    lowercasing could fold it into another address.
    """
    if not isinstance(userinfo, Mapping):
        raise AuthError("no verified identity in the provider response")

    sub = str(userinfo.get("sub") or "").strip()
    if not sub:
        raise AuthError("verified identity has no subject")

    if userinfo.get("email_verified") is not True:
        raise AuthError("google account email is not verified")

    claimed = str(userinfo.get("email") or "").strip()
    email = claimed.lower()
    if is_owner(email):
        return {"email": email, "sub": sub, "role": ADMIN_ROLE}
    if not claimed.isascii():
        # Before any match: a folded address must not take a grant, nor sign
        # in as a viewer that a grant for the folded spelling promotes later.
        raise AuthError("a Google account email with non-ASCII characters cannot sign in here")
    if _listed(email, admin_emails):
        hd = userinfo.get("hd")
        workspace = isinstance(hd, str) and bool(hd.strip())
        if not (workspace or email.rpartition("@")[2] == GOOGLE_AUTHORITATIVE_DOMAIN):
            # Refused, not passed on to the viewer rule: that needs ``hd`` too.
            raise AuthError(GRANT_NEEDS_AUTHORITY)
        return {"email": email, "sub": sub, "role": GRANTED_ROLE}

    hosted = str(userinfo.get("hd") or "").strip().lower()
    local, sep, domain = email.rpartition("@")
    if (
        sep
        and local
        and domain
        and hosted
        and any(
            hmac.compare_digest(domain.encode("utf-8"), allowed.encode("utf-8"))
            and hmac.compare_digest(hosted.encode("utf-8"), allowed.encode("utf-8"))
            for allowed in VIEWER_DOMAINS
        )
    ):
        return {"email": email, "sub": sub, "role": VIEWER_ROLE}

    # The rejected address is deliberately absent from the message: this
    # string reaches a browser and an access log.
    raise AuthError("account is not authorized for this dashboard")


def resolve_session(session: Mapping[str, Any], admin_emails: Iterable[str] = ()) -> Optional[Dict[str, Any]]:
    """The session with its role for *this* request, or None when it must end.

    - The owner is admin.
    - An address on the grant list is admin, so a viewer granted admin needs
      no new sign-in.
    - A session minted as admin for anyone else lost its grant: None, and the
      caller deletes it. It is never downgraded to viewer, because it never
      proved viewer eligibility (the Workspace ``hd`` claim) at sign-in.
    - Otherwise the role minted at sign-in stands (viewer).

    ``owner`` is added for the one power an added admin does not get: changing
    the grant list.
    """
    email = str(session.get("email") or "")
    owner = is_owner(email)
    if owner or _listed(email, admin_emails):
        role = ADMIN_ROLE
    elif session.get("role") in (ADMIN_ROLE, GRANTED_ROLE):
        return None
    else:
        role = session.get("role")
        if role == VIEWER_ROLE and email.rpartition("@")[2] not in VIEWER_DOMAINS:
            # A deployment may remove a viewer domain between restarts.
            # Do not preserve sessions minted under the retired policy.
            return None
    if role not in (ADMIN_ROLE, VIEWER_ROLE):
        return None
    return {**session, "role": role, "owner": owner}


def hash_session_id(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


class SessionStore:
    """Revocable server-side sessions in a private SQLite file."""

    def __init__(self, path: Path, ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
        self.path = Path(path)
        self.ttl_seconds = int(ttl_seconds)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        # Create the file 0600 *before* sqlite ever opens it: a session table must
        # never exist, even for a moment, with a group/world readable mode.
        if not self.path.exists():
            os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        os.chmod(self.path, 0o600)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            if "role" not in columns:
                # Rows that predate roles were all minted for the admin.
                try:
                    conn.execute("ALTER TABLE sessions ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")
                except sqlite3.OperationalError:
                    if "role" not in {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}:
                        raise

    # ------------------------------------------------------------------ writes
    def create(
        self,
        email: str,
        sub: str,
        previous_session_id: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
        role: str = ADMIN_ROLE,
    ) -> Dict[str, Any]:
        """Mint a session, rotating only the caller's own previous session.

        Other live sessions for the same admin survive on purpose: the dashboard
        is meant to be open in two browsers/tabs at once, and one of them signing
        in again must not log the other out.
        """
        if role not in ROLES:
            raise ValueError("unknown session role")
        ttl = int(self.ttl_seconds if ttl_seconds is None else ttl_seconds)
        session_id = secrets.token_urlsafe(SESSION_ID_BYTES)
        csrf_token = secrets.token_urlsafe(CSRF_TOKEN_BYTES)
        expires_at = _now() + ttl
        with self._connect() as conn:
            # Revoke-and-mint in one transaction: no window where the caller has
            # two valid cookies, and none where it has zero.
            conn.execute("BEGIN IMMEDIATE")
            try:
                if previous_session_id:
                    conn.execute(
                        "DELETE FROM sessions WHERE id_hash = ?", (hash_session_id(previous_session_id),)
                    )
                conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_now(),))
                conn.execute(
                    "INSERT INTO sessions (id_hash, email, sub, csrf_token, created_at, expires_at, role)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (hash_session_id(session_id), email, sub, csrf_token, _iso_now(), expires_at, role),
                )
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return {
            "id": session_id,
            "email": email,
            "sub": sub,
            "role": role,
            "csrf_token": csrf_token,
            "expires_at": expires_at,
        }

    def revoke(self, session_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM sessions WHERE id_hash = ?", (hash_session_id(session_id),))
            return cursor.rowcount > 0

    def revoke_admin_sessions(self, email: Any) -> int:
        """Delete the admin sessions of one address whose grant was removed; the count deleted.

        Exact match on the lowercased address, rows stored as ``granted`` or
        ``admin`` only: that address's ``viewer`` sessions stay (they proved
        viewer eligibility at sign-in), and the owner's sessions are never
        touched. Without this a removed grant's idle session would come back to
        life when the address is granted again.
        """
        if not isinstance(email, str) or not email:
            return 0
        email = email.lower()
        if is_owner(email):
            return 0
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE email = ? AND role IN (?, ?)", (email, GRANTED_ROLE, ADMIN_ROLE)
            )
            return cursor.rowcount

    # ------------------------------------------------------------------- reads
    def get(self, session_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Return the live session for a cookie value, deleting it once expired."""
        if not session_id:
            return None
        id_hash = hash_session_id(session_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT email, sub, csrf_token, created_at, expires_at, role FROM sessions WHERE id_hash = ?",
                (id_hash,),
            ).fetchone()
            if row is None:
                return None
            if float(row["expires_at"]) <= _now():
                conn.execute("DELETE FROM sessions WHERE id_hash = ?", (id_hash,))
                return None
            if row["role"] not in ROLES:
                # A role this build does not know (e.g. minted by a newer build
                # before a rollback) must never resolve upward: the browser
                # re-authenticates and the role is derived again from Google.
                conn.execute("DELETE FROM sessions WHERE id_hash = ?", (id_hash,))
                return None
        return {
            "email": row["email"],
            "sub": row["sub"],
            "role": row["role"],
            "csrf_token": row["csrf_token"],
            "created_at": row["created_at"],
            "expires_at": float(row["expires_at"]),
        }

    @staticmethod
    def verify_csrf(session: Mapping[str, Any], token: Optional[str]) -> bool:
        expected = str(session.get("csrf_token") or "")
        if not expected or not token:
            return False
        # Bytes, not str: ``compare_digest`` raises TypeError on a non-ASCII
        # str, and a header can carry one; a forged token must be *refused*,
        # not turned into a 500 before the handler answers.
        return hmac.compare_digest(str(token).encode("utf-8"), expected.encode("utf-8"))

    # ---------------------------------------------------------------- plumbing
    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """A fresh connection per operation: the web tier is multi-threaded."""
        with _connect(self.path) as connection:
            yield connection


@contextlib.contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=10000")
        yield connection
    finally:
        connection.close()


class OAuthStateCache:
    """Server-side one-time records for the OAuth state/nonce/PKCE verifier.

    Authlib's Starlette integration keeps the temporary flow data in the signed
    session cookie unless it is given a cache. A signed cookie alone is
    replayable by whoever captures it, so the authoritative record lives here and
    the cookie only carries a marker: a callback must present *both*, and the row
    is deleted the first time it is read (``clear_state_data``), so a captured
    state cannot be replayed. Entries expire regardless.

    The async signature is Authlib's (``await cache.get/set/delete``); the bodies
    are ordinary sqlite calls because each one is a single local row touched only
    on the two login endpoints.
    """

    def __init__(self, path: Path, ttl_seconds: int = OAUTH_STATE_TTL_SECONDS) -> None:
        self.path = Path(path)
        self.ttl_seconds = int(ttl_seconds)

    async def get(self, key: str) -> Optional[str]:
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT value, expires_at FROM oauth_states WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            if float(row["expires_at"]) <= _now():
                conn.execute("DELETE FROM oauth_states WHERE key = ?", (key,))
                return None
        return str(row["value"])

    async def set(self, key: str, value: str, expires_in: Optional[int] = None) -> None:
        # Bound the lifetime locally: Authlib's default (3600s) is longer than a
        # login flow needs.
        ttl = min(int(expires_in or self.ttl_seconds), self.ttl_seconds)
        with _connect(self.path) as conn:
            conn.execute("DELETE FROM oauth_states WHERE expires_at <= ?", (_now(),))
            conn.execute(
                "INSERT OR REPLACE INTO oauth_states (key, value, expires_at) VALUES (?, ?, ?)",
                (key, value, _now() + ttl),
            )

    async def delete(self, key: str) -> None:
        with _connect(self.path) as conn:
            conn.execute("DELETE FROM oauth_states WHERE key = ?", (key,))

    def count(self) -> int:
        """Live record count (tests assert the one-time consumption)."""
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM oauth_states WHERE expires_at > ?", (_now(),)
            ).fetchone()
        return int(row["total"])
