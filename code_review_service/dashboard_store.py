"""Durable SQLite store for the admin dashboard (attempts, retries, events).

The review daemon (``service.py``) and the dashboard web API are separate
processes, so dashboard state cannot live in the JSON state file: ``save_state``
(service.py:495-510) atomically replaces the *whole* document and therefore
cannot merge concurrent snapshots. This module owns its own SQLite database in
WAL mode instead, with one short transaction per operation:

* retry requests  - a queue the web API appends to and the daemon drains,
* runs            - one immutable row per review attempt,
* events          - an append-only, monotonically numbered stream for SSE
                    replay after a client reconnects.
* pr_authors      - each PR author's own review language and welcome comment,
                    set by ``@review-of-legends`` comments (mentions.py),
* feedback        - what PR authors told the maintainers (newest rows kept).
* author_review_requests - durable comment receipts and the per-head allowance.

Only the stdlib is used. Every operation opens its own connection (safe across
threads and processes) and every write runs inside ``BEGIN IMMEDIATE`` so that
read-then-write sequences (retry de-duplication, queue claiming) are serialized
rather than racing. Payloads are bounded: the store must never become a log
sink. The database is created 0600 inside a 0700 directory because PR titles,
URLs and agent errors are private.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from .mentions import LANGUAGE_CODE_PATTERN, MAX_FEEDBACK_CHARS

TRUNCATION_MARKER = "...[truncated]"
# A used contributor verification code is kept this long, so the comment that
# used it can be handled again (a failed reply) and answered the same.
USED_CODE_KEPT_SECONDS = 86400

# ``skipped`` is import-only: a legacy baseline entry recorded to *suppress* a
# first-run review. A live attempt never reaches it.
RUN_STATUSES = ("running", "succeeded", "failed", "interrupted", "stale", "skipped")
TERMINAL_RUN_STATUSES = ("succeeded", "failed", "interrupted", "stale", "skipped")
RUN_UPDATE_FIELDS = (
    "stage",
    "agents",
    "review_url",
    "error",
    "successful_agents",
    "total_agents",
    "run_dir",
    "llm",
    "agent_results",
)
RUN_TEXT_FIELDS = ("stage", "review_url", "error", "run_dir")
RUN_INT_FIELDS = ("successful_agents", "total_agents")
#: A failed attempt keeps what its successful agents returned, so a dashboard
#: retry can re-run only the agents that failed (service.review_pr). Each result
#: and the whole list are capped; a result past its cap is not kept and that agent
#: simply runs again. Only the agent ids ever leave the store (``reusable_agents``).
MAX_AGENT_RESULT_CHARS = 32_000
MAX_AGENT_RESULTS_CHARS = 128_000
MAX_AGENT_RESULTS = 32
#: What a dashboard retry re-runs (``options["scope"]``): only the agents without
#: a kept result, or every agent. A request without a scope runs every agent.
RETRY_SCOPE_OPTION = "scope"
RETRY_SCOPE_FAILED = "failed"
RETRY_SCOPE_ALL = "all"
RETRY_SCOPES = (RETRY_SCOPE_FAILED, RETRY_SCOPE_ALL)
#: What kind of request this is (``options["kind"]``). Absent means a retry of
#: a failed attempt; ``rereview`` is the Retrigger badge: a fresh review of a
#: head that may already carry one, every agent, the outline refreshed.
RETRY_KIND_OPTION = "kind"
RETRY_KIND_REREVIEW = "rereview"
#: When the Retrigger page read the snapshot a rereview was requested against
#: (``options["seen_at"]``, UTC ISO 8601): a review the ledger records after it
#: is one the page could not show, so the daemon's late check refuses to repeat it.
RETRY_SEEN_AT_OPTION = "seen_at"
#: A comment-requested rereview: deduplication and the author's per-head limit.
RETRY_SOURCE_KEY_OPTION = "source_key"

ACTIVE_REQUEST_STATUSES = ("queued", "running")
TERMINAL_REQUEST_STATUSES = ("succeeded", "failed", "stale")

#: Raised by ``enqueue_retry`` when a ``rereview`` would stack on activity the
#: PR already has queued or running (any head). A plain ``ValueError`` would
#: map to a generic 400 at the web layer; this is a 409, and it is always
#: refused inside the write transaction, not in a separate check the caller
#: can race.
class PrBusyError(ValueError):
    pass


class AuthorReviewLimitError(ValueError):
    """A comment-requested rereview has already been admitted for this head."""


INTERRUPTED_NOTE = "interrupted before completion (service restart)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS retry_requests (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    id           TEXT NOT NULL UNIQUE,
    repo         TEXT NOT NULL,
    pr           INTEGER NOT NULL,
    head_sha     TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    retry_of     TEXT,
    status       TEXT NOT NULL,
    run_id       TEXT,
    error        TEXT,
    options      TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_status ON retry_requests (status, seq);
CREATE INDEX IF NOT EXISTS idx_requests_target ON retry_requests (repo, pr, head_sha, status);
CREATE TABLE IF NOT EXISTS author_review_requests (
    source_key TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    pr INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    request_id TEXT NOT NULL,
    UNIQUE (repo, pr, head_sha)
);

CREATE TABLE IF NOT EXISTS runs (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    id                TEXT NOT NULL UNIQUE,
    repo              TEXT NOT NULL,
    pr                INTEGER NOT NULL,
    head_sha          TEXT NOT NULL,
    title             TEXT NOT NULL DEFAULT '',
    url               TEXT NOT NULL DEFAULT '',
    trigger_source    TEXT NOT NULL DEFAULT 'poll',
    retry_of          TEXT,
    request_id        TEXT,
    run_dir           TEXT,
    llm               TEXT,
    agent_results     TEXT,
    reusable_agents   TEXT,
    status            TEXT NOT NULL,
    stage             TEXT,
    agents            TEXT,
    review_url        TEXT,
    error             TEXT,
    successful_agents INTEGER,
    total_agents      INTEGER,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_repo ON runs (repo, seq);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs (status, seq);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    run_id     TEXT,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pr_authors (
    login            TEXT PRIMARY KEY,
    display_login    TEXT NOT NULL,
    lang             TEXT,
    lang_set_at      TEXT,
    lang_comment_url TEXT,
    lang_source_at   TEXT,
    lang_source_key  TEXT,
    welcomed_at      TEXT,
    welcomed_pr      TEXT,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo        TEXT NOT NULL,
    pr          INTEGER NOT NULL,
    login       TEXT NOT NULL,
    body        TEXT NOT NULL,
    comment_url TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    source_key  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_source ON feedback (source_key);

CREATE TABLE IF NOT EXISTS contributor_codes (
    code_hash  TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    login      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT,
    used_by    TEXT
);

CREATE TABLE IF NOT EXISTS contributor_identities (
    login         TEXT PRIMARY KEY,
    display_login TEXT NOT NULL,
    email         TEXT NOT NULL,
    user_id       INTEGER NOT NULL DEFAULT 0,
    verified_at   TEXT NOT NULL,
    comment_url   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS pledge_signatures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo        TEXT NOT NULL,
    pr          INTEGER NOT NULL,
    login       TEXT NOT NULL,
    user_id     INTEGER NOT NULL DEFAULT 0,
    comment_url TEXT NOT NULL DEFAULT '',
    pledge_sha  TEXT NOT NULL DEFAULT '',
    signed_at   TEXT NOT NULL,
    pull_url    TEXT,
    source_key  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pledge_source ON pledge_signatures (source_key);
"""

#: Operator settings the store keeps for the review daemon (dashboard-set).
MODEL_POLICY_SETTING = "model_policy"
#: Repositories an admin added on the dashboard: ``{"added": [{repo, added_by, added_at}]}``.
REPOSITORIES_SETTING = "repositories"
MAX_ADDED_REPOSITORIES = 50
#: Addresses the owner granted admin on the dashboard: ``{"added": [{email, added_by, added_at}]}``.
#: The web tier validates each entry (dashboard_auth.normalize_admin_email); the store bounds the list.
ADMINS_SETTING = "admins"
MAX_ADDED_ADMINS = 50
SETTING_KEYS = (MODEL_POLICY_SETTING, REPOSITORIES_SETTING, ADMINS_SETTING)
#: Per-agent overrides (prompt text, required flag) live under ``agent:<agent id>``.
AGENT_SETTING_PREFIX = "agent:"
#: The server-managed agent fields live beside it under ``agent_meta:<agent id>``
#: (with ``doc_sha``, the overrides they describe), never inside ``agent:<id>``:
#: the previous release rejects a document with fields it does not know, so a
#: rollback would otherwise ignore every override, required agents included.
AGENT_META_PREFIX = "agent_meta:"
AGENT_META_FIELDS = ("prompt_base_sha", "prompt_saved_at", "prompt_saved_by", "superseded")
AGENT_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
#: The one prompt limit (code points); service, web API and UI all read it from here.
MAX_AGENT_PROMPT_CHARS = 48_000
#: PR authors' own settings and feedback, from ``@review-of-legends`` comments
#: (mentions.py). A GitHub login (bots never get here); the newest rows kept.
LOGIN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
MAX_FEEDBACK_ROWS = 2000
#: The largest row id SQLite stores (a signed 64-bit integer): a paging key
#: past it means "past every row", never an OverflowError.
MAX_ROW_ID = 2**63 - 1


def _now() -> str:
    """UTC timestamp in the same shape the service already writes to state."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-empty string" % field)
    return value.strip()


def _require_pr(value: Any) -> int:
    # bool is an int subclass; True would silently become PR #1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("pr must be an int, got %r" % (value,))
    if value <= 0:
        raise ValueError("pr must be positive, got %d" % value)
    return value


def _optional_text(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    return _require_text(value, field)


def _optional_row_id(value: Any, field: str) -> Optional[int]:
    """A paging key: None, or a positive int (capped at MAX_ROW_ID), never coerced."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be an int, got %r" % (field, value))
    if value <= 0:
        raise ValueError("%s must be positive, got %d" % (field, value))
    return min(value, MAX_ROW_ID)


def _optional_count(value: Any, field: str) -> Optional[int]:
    """An agent count that may legitimately be unknown (NULL), never coerced."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be an int or None, got %r" % (field, value))
    if value < 0:
        raise ValueError("%s cannot be negative, got %d" % (field, value))
    return value


class DashboardStore:
    """Durable attempts/retry-queue/event store, independent of the JSON state."""

    #: ``list_runs`` never returns more than this many rows.
    max_list_limit = 200
    #: ``events_after`` never returns more than this many rows.
    max_event_limit = 500
    #: Free-text columns (stage, error, review_url, title, url) are truncated here.
    max_text_chars = 2000
    #: Serialized JSON values (agents, event payloads) are replaced past this size.
    max_json_chars = 8000
    #: A setting past its limit is refused, never truncated (encoded characters).
    max_setting_chars = 8000
    #: Each agent key carries one whole prompt (``agent:<id>`` the override,
    #: ``agent_meta:<id>`` the edit a changed prompt file replaced) and a little
    #: metadata. JSON escapes take a character to at most 6 (``\u0001``), so a
    #: prompt within MAX_AGENT_PROMPT_CHARS fits: the only prompt limit an
    #: operator meets is the documented one.
    max_agent_setting_chars = 6 * MAX_AGENT_PROMPT_CHARS + 4_000
    #: 50 worst-case entries (140-char name, 254-char email, timestamp) are ~23,300
    #: characters, so the entry count, not this size, is what an admin runs into.
    max_repositories_setting_chars = 24_000
    #: 50 worst-case entries (254-char email, 254-char added_by, timestamp) are ~29,000
    #: characters: as for repositories, the entry count is the limit an owner meets.
    max_admins_setting_chars = 32_000

    def __init__(self, path: Path, busy_timeout_ms: int = 10_000) -> None:
        self.path = Path(path)
        self._busy_timeout_ms = int(busy_timeout_ms)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(self.path.parent), 0o700)
        except OSError:
            # Best-effort: the directory may be shared and already tighter.
            pass
        if not self.path.exists():
            # Create the file ourselves so SQLite never widens it via umask.
            os.close(os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600))
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            self._add_missing_columns(conn)
        self._harden_permissions()

    @staticmethod
    def _add_missing_columns(conn: sqlite3.Connection) -> None:
        """Bring a database created by an earlier schema up to date.

        ``CREATE TABLE IF NOT EXISTS`` silently keeps an old table, so a column
        added later has to be applied explicitly for init to stay idempotent
        against databases that already exist on disk.
        """
        # ``request_id``/``run_dir``: attempt identity shared with the retained
        # service log. ``llm``: model policy + token totals (JSON, bounded).
        # ``options``: operator choices for a retry (JSON): the model policy and the scope.
        # ``agent_results``/``reusable_agents``: a failed attempt's successful
        # agent results (JSON, bounded) and their ids (bounded_agent_results).
        for table, column in (
            ("runs", "request_id"),
            ("runs", "run_dir"),
            ("runs", "llm"),
            ("runs", "agent_results"),
            ("runs", "reusable_agents"),
            ("retry_requests", "options"),
        ):
            DashboardStore._ensure_column(conn, table, column)

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str) -> None:
        """``ALTER TABLE ADD COLUMN`` that tolerates a concurrent opener.

        SQLite has no ``ADD COLUMN IF NOT EXISTS`` and the daemon and the web
        tier open the same file at deploy time; the loser of that race must
        not lose its store for the life of the process over a column the
        winner already added.
        """

        def present() -> bool:
            return column in {row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}

        if present():
            return
        try:
            conn.execute("ALTER TABLE %s ADD COLUMN %s TEXT" % (table, column))
        except sqlite3.OperationalError:
            if not present():
                raise

    # ----------------------------------------------------------------- settings

    def get_setting(self, key: str) -> Optional[Dict[str, Any]]:
        """One operator setting, or None when it was never set (or was reset)."""
        key = _require_setting_key(key)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM settings WHERE key = ?", (key,)).fetchone()
            return None if row is None else _setting_dict(row)

    def set_setting(self, key: str, value: Dict[str, Any], updated_by: str) -> Dict[str, Any]:
        """Replace a setting and emit ``setting_updated`` in the same transaction.

        The value is a JSON object the *caller* has validated (the web layer
        owns the model-policy and agent rules); the store only bounds its size.
        An oversized value raises ``ValueError``: a truncation marker would
        silently replace what the operator saved.
        """
        key = _require_setting_key(key)
        updated_by = _require_text(updated_by, "updated_by")
        if not isinstance(value, dict):
            raise ValueError("setting value must be a dict")
        with self._write() as conn:
            return self._put_setting(conn, key, value, updated_by)

    # Agent settings are two keys (see AGENT_META_PREFIX): ``agent:<id>`` holds
    # the overrides in the shape every release reads, ``agent_meta:<id>`` the
    # server-managed fields. Callers see one document; every write below
    # reads, decides and writes both keys in ONE write transaction.

    def get_agent_setting(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """The agent's whole document as one row (``agent:<id>`` plus its sidecar), or None."""
        with self._connect() as conn:
            return self._read_agent(conn, str(agent_id))

    def merge_agent_setting(
        self,
        agent_id: str,
        patch: Dict[str, Any],
        updated_by: str,
        normalize: Callable[[Any], Dict[str, Any]],
        prepare: Optional[Callable[[Dict[str, Any], Optional[Dict[str, Any]]], Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Apply a partial agent override; the stored row, or None once it is empty (deleted).

        Read, merge and write run in ONE write transaction: two tabs saving
        different fields at once both land, where a get-then-set would let the
        later write revert the earlier field. ``normalize`` (the service's
        validate_agent_document) owns the field rules: a stored value it rejects
        counts as empty, a merge it rejects raises ``ValueError`` and nothing is
        written. A ``None`` in ``patch`` clears that field. ``prepare(patch, row)``
        (the web tier's prompt-file check) completes the patch inside the
        transaction from the stored row, so the file it reads is the one current
        when this write holds the lock; an exception it raises aborts the write.
        """
        updated_by = _require_text(updated_by, "updated_by")
        if not isinstance(patch, dict):
            raise ValueError("patch must be a dict")
        with self._write() as conn:
            current = self._normalized_agent(conn, str(agent_id), normalize)
            if prepare is not None:
                patch = prepare(dict(patch), self._read_agent(conn, str(agent_id)))
            return self._write_agent(conn, str(agent_id), normalize(dict(current, **patch)), updated_by)

    def reconcile_agent_setting(
        self,
        agent_id: str,
        read_default: Callable[[], Optional[str]],
        reconcile: Callable[..., Tuple[Dict[str, Any], Optional[str]]],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Apply the agent's current prompt file to its stored document; ``(row or None, action)``.

        ``read_default`` reads the prompt file (None: unreadable, nothing is
        done) and ``reconcile`` (the service's reconcile_agent_document) decides,
        from the stored value, that text, now and who saved the overrides. Both
        run inside ONE write transaction: a save that lands between a caller's
        start and this write is compared with the file as it is now, never with
        an older read, and the daemon and the dashboard calling at once are
        serialised so the second finds nothing to do (idempotent). The write
        keeps the row's ``updated_at``/``updated_by``: nobody edited the
        overrides that remain. A replacement (``superseded``) also emits
        ``agent_prompt_superseded`` with the agent id only; no prompt text rides
        any event. A stored value ``reconcile`` rejects is left alone.
        """
        agent_id = str(agent_id)
        with self._write() as conn:
            current = self._read_agent(conn, agent_id)
            if current is None:
                return None, None
            default_text = read_default()
            if not isinstance(default_text, str):
                return current, None
            try:
                value, action = reconcile(current["value"], default_text, _now(), current["updated_at"], current["updated_by"])
            except ValueError:
                return current, None
            if action is None:
                return current, None
            saved = self._write_agent(conn, agent_id, value, current["updated_by"], updated_at=current["updated_at"])
            if action == "superseded":
                self._emit(conn, "agent_prompt_superseded", None, {"key": AGENT_SETTING_PREFIX + agent_id, "agent_id": agent_id})
            return saved, action

    def restore_superseded_prompt(
        self,
        agent_id: str,
        expected_sha: str,
        read_base: Callable[[], Optional[str]],
        updated_by: str,
        normalize: Callable[[Any], Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Roll back to the edit a changed prompt file replaced; the row, or None when there is none.

        ``expected_sha`` names the kept edit the admin is looking at
        (superseded_prompt_sha); a different one raises SupersededConflict and
        nothing changes. In ONE write transaction the kept edit becomes
        ``prompt`` again, saved now by ``updated_by``, its base becomes
        ``read_base()`` (the sha of the file current while this write holds the
        lock; None when it cannot be read, so the edit is adopted on the next
        reconcile) and ``superseded`` is removed. ``normalize`` is the
        service's validate_agent_document.
        """
        updated_by = _require_text(updated_by, "updated_by")
        agent_id = str(agent_id)
        with self._write() as conn:
            current = self._normalized_agent(conn, agent_id, normalize)
            kept = current.pop("superseded", None)
            if not kept:
                return None
            _require_superseded(kept, expected_sha)
            current["prompt"] = kept["prompt"]
            current["prompt_saved_at"] = _now()
            current["prompt_saved_by"] = updated_by
            current.pop("prompt_base_sha", None)
            base_sha = read_base()
            if base_sha:
                current["prompt_base_sha"] = base_sha
            return self._write_agent(conn, agent_id, normalize(current), updated_by)

    def discard_superseded_prompt(
        self,
        agent_id: str,
        expected_sha: str,
        updated_by: str,
        normalize: Callable[[Any], Dict[str, Any]],
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Drop the edit a changed prompt file replaced; ``(found, the row or None once empty)``.

        ``expected_sha`` names the kept edit the admin confirmed; a different
        one raises SupersededConflict and nothing changes.
        """
        updated_by = _require_text(updated_by, "updated_by")
        agent_id = str(agent_id)
        with self._write() as conn:
            current = self._normalized_agent(conn, agent_id, normalize)
            kept = current.pop("superseded", None)
            if kept is None:
                return False, self._read_agent(conn, agent_id)
            _require_superseded(kept, expected_sha)
            return True, self._write_agent(conn, agent_id, current, updated_by)

    def delete_agent_setting(self, agent_id: str, updated_by: str, superseded_sha: Optional[str] = None) -> bool:
        """Reset an agent to its config, prompt file and model policy: both keys go; True if anything existed.

        A kept edit is deleted with the rest only when ``superseded_sha`` names
        it, so a page that never showed it cannot drop it (SupersededConflict).
        """
        updated_by = _require_text(updated_by, "updated_by")
        agent_id = str(agent_id)
        with self._write() as conn:
            current = self._read_agent(conn, agent_id)
            if current is None:
                return False
            kept = current["value"].get("superseded")
            if isinstance(kept, dict):
                _require_superseded(kept, superseded_sha)
            self._write_agent(conn, agent_id, {}, updated_by)
            return True

    def _read_agent(self, conn: sqlite3.Connection, agent_id: str) -> Optional[Dict[str, Any]]:
        """``agent:<id>`` merged with its sidecar, as one row; None when neither exists.

        The sidecar applies only to the exact overrides it was written beside
        (``doc_sha``): when an older release rewrote or reset ``agent:<id>``
        without knowing it, it is stale and ignored, so no base, save stamp or
        kept edit outlives the document it described.
        """
        key = _require_setting_key(AGENT_SETTING_PREFIX + agent_id)
        meta_key = AGENT_META_PREFIX + agent_id
        # One statement, one snapshot: outside a transaction two SELECTs could
        # straddle another process's commit and pair the wrong halves.
        rows = {r["key"]: r for r in conn.execute("SELECT * FROM settings WHERE key IN (?, ?)", (key, meta_key)).fetchall()}
        row, meta_row = rows.get(key), rows.get(meta_key)
        if row is None:
            # The sidecar never stands alone (_write_agent keeps agent:<id> beside
            # it): without the row, an older release reset the agent.
            return None
        base = _setting_dict(row)
        meta = _setting_dict(meta_row)["value"] if meta_row is not None else {}
        if meta.get("doc_sha") != _document_sha(base):
            meta = {}
        return dict(base, value=dict(base["value"], **{field: meta[field] for field in AGENT_META_FIELDS if field in meta}))

    def _normalized_agent(
        self, conn: sqlite3.Connection, agent_id: str, normalize: Callable[[Any], Dict[str, Any]]
    ) -> Dict[str, Any]:
        """The stored agent document under ``normalize``'s rules; a value it rejects counts as empty."""
        row = self._read_agent(conn, agent_id)
        if row is None:
            return {}
        try:
            return dict(normalize(row["value"]))
        except ValueError:
            return {}

    def _write_agent(
        self, conn: sqlite3.Connection, agent_id: str, document: Dict[str, Any], updated_by: str, updated_at: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Split ``document`` into ``agent:<id>`` and its sidecar and write both.

        ``agent:<id>`` is written whenever anything is stored, ``{}`` when only
        server-managed fields remain, so the sidecar always has a row to belong
        to; it records that exact write (``doc_sha`` of value, time and author),
        and any later write by a release that does not know it — a save, or a
        reset and a save of the same text — makes it stale. Returns the merged
        row, or None once nothing is stored (both keys deleted).
        """
        key = _require_setting_key(AGENT_SETTING_PREFIX + agent_id)
        meta_key = AGENT_META_PREFIX + agent_id
        overrides = {field: value for field, value in document.items() if field not in AGENT_META_FIELDS}
        meta = {field: document[field] for field in AGENT_META_FIELDS if field in document}
        if overrides or meta:
            row = self._put_setting(conn, key, overrides, updated_by, updated_at=updated_at)
        else:
            row = None
            if conn.execute("DELETE FROM settings WHERE key = ?", (key,)).rowcount:
                self._emit(conn, "setting_deleted", None, {"key": key, "updated_by": updated_by})
        if meta:
            self._put_setting(conn, meta_key, dict(meta, doc_sha=_document_sha(row)), updated_by, updated_at=updated_at)
        elif conn.execute("DELETE FROM settings WHERE key = ?", (meta_key,)).rowcount:
            self._emit(conn, "setting_deleted", None, {"key": meta_key, "updated_by": updated_by})
        return self._read_agent(conn, agent_id)

    def _put_setting(
        self, conn: sqlite3.Connection, key: str, value: Dict[str, Any], updated_by: str, updated_at: Optional[str] = None
    ) -> Dict[str, Any]:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        limit = self.max_agent_setting_chars if key.startswith((AGENT_SETTING_PREFIX, AGENT_META_PREFIX)) else self.max_setting_chars
        if key == REPOSITORIES_SETTING:
            added = value.get("added")
            if not isinstance(added, list):
                raise ValueError("setting %s must carry an 'added' list" % key)
            if len(added) > MAX_ADDED_REPOSITORIES:
                raise ValueError("setting %s holds %d repositories, limit %d" % (key, len(added), MAX_ADDED_REPOSITORIES))
            limit = self.max_repositories_setting_chars
        elif key == ADMINS_SETTING:
            added = value.get("added")
            if not isinstance(added, list):
                raise ValueError("setting %s must carry an 'added' list" % key)
            if len(added) > MAX_ADDED_ADMINS:
                raise ValueError("setting %s holds %d admins, limit %d" % (key, len(added), MAX_ADDED_ADMINS))
            limit = self.max_admins_setting_chars
        if len(encoded) > limit:
            raise ValueError("setting %s is too large: %d encoded characters, limit %d" % (key, len(encoded), limit))
        conn.execute(
            "INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (key, encoded, updated_at or _now(), updated_by),
        )
        self._emit(conn, "setting_updated", None, _setting_event(key, value, updated_by))
        return _setting_dict(conn.execute("SELECT * FROM settings WHERE key = ?", (key,)).fetchone())

    def delete_setting(self, key: str, updated_by: str) -> bool:
        """Drop a setting (the daemon falls back to its config); True if it existed."""
        key = _require_setting_key(key)
        updated_by = _require_text(updated_by, "updated_by")
        with self._write() as conn:
            deleted = conn.execute("DELETE FROM settings WHERE key = ?", (key,)).rowcount
            if deleted:
                self._emit(conn, "setting_deleted", None, {"key": key, "updated_by": updated_by})
            return bool(deleted)

    # ------------------------------------------------------------------ retries

    def enqueue_retry(
        self,
        repo: str,
        pr: int,
        head_sha: str,
        requested_by: str,
        retry_of: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Queue a retry, collapsing duplicate requests.

        A request that is still ``queued`` or ``running`` for the same
        repo/PR/head is returned as-is, so a double click (or two dashboard
        tabs) cannot dispatch the same review twice; its ``options`` (the
        operator's model and scope choice) are the ones that run. Caller-side concerns
        (configured repository, Google identity, option validity) are
        validated by the web layer; the store only bounds the JSON.

        A rereview carrying ``source_key`` is a comment request. Its durable
        receipt deduplicates that comment for good and consumes the one-per-head
        author allowance atomically with admission. Badge requests omit it.
        """
        repo = _require_text(repo, "repo")
        pr = _require_pr(pr)
        head_sha = _require_text(head_sha, "head_sha")
        requested_by = _require_text(requested_by, "requested_by")
        retry_of = _optional_text(retry_of, "retry_of")
        if options is not None and not isinstance(options, dict):
            raise ValueError("options must be a dict or None")
        encoded_options = self._bounded_json(options) if options else None
        source = options.get(RETRY_SOURCE_KEY_OPTION) if options else None
        if source is not None:
            source = _require_text(source, "source_key")
            if options.get(RETRY_KIND_OPTION) != RETRY_KIND_REREVIEW:
                raise ValueError("a comment source requires a rereview")
        now = _now()

        with self._write() as conn:
            # The durable receipt and queue row commit together. Replaying a
            # comment after a cursor-save failure returns the original request,
            # even after it finished or the live head moved.
            if source is not None:
                receipt = conn.execute(
                    "SELECT request_id FROM author_review_requests WHERE source_key = ?", (source,),
                ).fetchone()
                if receipt is not None:
                    return _request_dict(self._request_row(conn, receipt["request_id"]))
            # A rereview posts a fresh review on purpose, so it is not subject
            # to the same-head dedup below; it must not stack a second one on
            # a PR that already has a queued/running request or a running
            # attempt on ANY head -- including this exact head, so a second
            # POST for the very same head a rereview is already queued for is
            # refused too, not silently handed back the first request as a
            # success. Checked FIRST, before the dedup query, and inside the
            # write transaction the `BEGIN IMMEDIATE` connection holds, so two
            # overlapping POSTs (same head or not) cannot both pass a
            # check-then-insert done outside it.
            if isinstance(options, dict) and options.get(RETRY_KIND_OPTION) == RETRY_KIND_REREVIEW:
                busy_request = conn.execute(
                    "SELECT 1 FROM retry_requests WHERE repo = ? AND pr = ? AND status IN (?, ?) LIMIT 1",
                    (repo, pr) + ACTIVE_REQUEST_STATUSES,
                ).fetchone()
                busy_run = conn.execute(
                    "SELECT 1 FROM runs WHERE repo = ? AND pr = ? AND status = 'running' LIMIT 1",
                    (repo, pr),
                ).fetchone()
                if busy_request is not None or busy_run is not None:
                    raise PrBusyError("a review of this pull request is already queued or running")

            if source is not None and conn.execute(
                "SELECT 1 FROM author_review_requests WHERE repo = ? AND pr = ? AND head_sha = ?",
                (repo.lower(), pr, head_sha),
            ).fetchone() is not None:
                raise AuthorReviewLimitError("one comment-requested rereview per head")

            existing = conn.execute(
                "SELECT * FROM retry_requests WHERE repo = ? AND pr = ? AND head_sha = ?"
                " AND status IN (?, ?) ORDER BY seq LIMIT 1",
                (repo, pr, head_sha) + ACTIVE_REQUEST_STATUSES,
            ).fetchone()
            if existing is not None:
                return _request_dict(existing)

            request_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO retry_requests"
                " (id, repo, pr, head_sha, requested_by, retry_of, options, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                (request_id, repo, pr, head_sha, requested_by, retry_of, encoded_options, now, now),
            )
            if source is not None:
                conn.execute(
                    "INSERT INTO author_review_requests (source_key, repo, pr, head_sha, request_id)"
                    " VALUES (?, ?, ?, ?, ?)", (source, repo.lower(), pr, head_sha, request_id),
                )
            self._emit(
                conn,
                "retry_enqueued",
                None,
                {
                    "request_id": request_id,
                    "status": "queued",
                    "repo": repo,
                    "pr": pr,
                    "head_sha": head_sha,
                    "requested_by": requested_by,
                    "retry_of": retry_of,
                    # The policy that will run: with dedup, the first request's
                    # choice wins, so stream consumers must learn it from here.
                    "model_policy": options.get(MODEL_POLICY_SETTING) if options else None,
                    "scope": options.get(RETRY_SCOPE_OPTION) if options else None,
                    "kind": options.get(RETRY_KIND_OPTION) if options else None,
                },
            )
            return _request_dict(self._request_row(conn, request_id))

    def comment_review_request(self, source_key: str) -> Optional[Dict[str, Any]]:
        """The original request accepted for a comment, independent of status."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT r.* FROM retry_requests r JOIN author_review_requests a ON a.request_id = r.id"
                " WHERE a.source_key = ?", (_require_text(source_key, "source_key"),),
            ).fetchone()
        return _request_dict(row) if row is not None else None

    def latest_completed_review(self, repo: str, pr: int, head_sha: str) -> Optional[Dict[str, Any]]:
        """The newest succeeded attempt, even when a later attempt failed."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE repo = ? AND pr = ? AND head_sha = ? AND status = 'succeeded'"
                " ORDER BY seq DESC LIMIT 1",
                (_require_text(repo, "repo"), _require_pr(pr), _require_text(head_sha, "head_sha")),
            ).fetchone()
        return None if row is None else _run_dict(row)

    def claim_retry(self) -> Optional[Dict[str, Any]]:
        """Atomically take the oldest queued request and mark it ``running``.

        ``BEGIN IMMEDIATE`` makes the select-then-update indivisible, so with
        several consumers exactly one of them receives a given request.
        """
        with self._write() as conn:
            row = conn.execute(
                "SELECT * FROM retry_requests WHERE status = 'queued' ORDER BY seq LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE retry_requests SET status = 'running', updated_at = ? WHERE id = ?",
                (_now(), row["id"]),
            )
            claimed = _request_dict(self._request_row(conn, row["id"]))
            self._emit(
                conn,
                "retry_claimed",
                None,
                {
                    "request_id": claimed["id"],
                    "status": "running",
                    "repo": claimed["repo"],
                    "pr": claimed["pr"],
                    "head_sha": claimed["head_sha"],
                },
            )
            return claimed

    def requeue_retry(self, request_id: str) -> bool:
        """Put a claimed request back in the queue: ``running`` -> ``queued`` only.

        For a request the executor claimed but did not run (it paused or
        stopped while waiting for the PR's review in flight). Its place in the
        queue (``seq``) is kept, so it is claimed first again. Emits
        ``retry_enqueued``, which wakes the executor and refreshes the
        dashboard. Returns False, changing nothing, when the request is not
        ``running``; an attempt linked to it means work ran, so that is refused.
        """
        request_id = _require_text(request_id, "request_id")
        with self._write() as conn:
            row = self._request_row(conn, request_id)
            if row is None:
                raise ValueError("unknown retry request: %s" % request_id)
            if row["status"] != "running":
                return False
            if row["run_id"]:
                raise ValueError("retry request %s already has attempt %s; not requeuing" % (request_id, row["run_id"]))
            conn.execute(
                "UPDATE retry_requests SET status = 'queued', updated_at = ? WHERE id = ?",
                (_now(), request_id),
            )
            self._emit(
                conn,
                "retry_enqueued",
                None,
                {
                    "request_id": request_id,
                    "status": "queued",
                    "repo": row["repo"],
                    "pr": row["pr"],
                    "head_sha": row["head_sha"],
                    "requested_by": row["requested_by"],
                    "retry_of": row["retry_of"],
                    "requeued": True,
                },
            )
            return True

    def finish_retry(
        self,
        request_id: str,
        status: str,
        run_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Close a claimed request. Terminal requests are immutable."""
        request_id = _require_text(request_id, "request_id")
        if status not in TERMINAL_REQUEST_STATUSES:
            raise ValueError(
                "retry status must be one of %s, got %r" % (", ".join(TERMINAL_REQUEST_STATUSES), status)
            )
        with self._write() as conn:
            row = self._request_row(conn, request_id)
            if row is None:
                raise ValueError("unknown retry request: %s" % request_id)
            if row["status"] != "running":
                raise ValueError(
                    "cannot finish retry request in state %r (expected running)" % row["status"]
                )
            conn.execute(
                "UPDATE retry_requests SET status = ?, run_id = ?, error = ?, updated_at = ?"
                " WHERE id = ?",
                (status, _optional_text(run_id, "run_id"), self._bounded_text(error), _now(), request_id),
            )
            self._emit(
                conn,
                "retry_finished",
                _optional_text(run_id, "run_id"),
                {
                    "request_id": request_id,
                    "status": status,
                    "repo": row["repo"],
                    "pr": row["pr"],
                    "error": self._bounded_text(error),
                },
            )

    def retry_status(self, request_id: str) -> Optional[str]:
        """Current status of a retry request, or ``None`` if unknown."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM retry_requests WHERE id = ?", (request_id,)
            ).fetchone()
            return None if row is None else row["status"]

    def get_retry(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Full request detail, including the linked run and failure reason.

        Richer than the shape ``enqueue_retry``/``claim_retry``/``list_retries``
        return, because a single-request view needs the link and the error that
        the queue listing deliberately leaves out.
        """
        with self._connect() as conn:
            row = self._request_row(conn, request_id)
            return None if row is None else _request_detail_dict(row)

    def list_retries(
        self,
        status: Optional[str] = None,
        repo: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Newest-first retry requests for queue hydration, hard-bounded.

        Ordering is newest-first for display only; ``claim_retry`` still consumes
        oldest-first.
        """
        limit = min(max(int(limit), 1), self.max_list_limit)
        offset = max(int(offset), 0)
        clauses: List[str] = []
        values: List[Any] = []
        if status is not None:
            clauses.append("status = ?")
            values.append(status)
        if repo is not None:
            clauses.append("repo = ?")
            values.append(repo)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM retry_requests%s ORDER BY seq DESC LIMIT ? OFFSET ?" % where,
                tuple(values) + (limit, offset),
            ).fetchall()
        # Detail shape, not the frozen 8-key one: the web layer projects
        # ``run_id`` and ``error`` out of these rows (dashboard_web.RETRY_FIELDS), so
        # omitting them nulls the run link and the failure reason on every queue
        # row. ``enqueue_retry``/``claim_retry`` stay frozen.
        return [_request_detail_dict(row) for row in rows]

    # --------------------------------------------------------------------- runs

    def start_run(
        self,
        repo: str,
        pr: int,
        head_sha: str,
        title: str = "",
        url: str = "",
        trigger: str = "poll",
        retry_of: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record a new review attempt. Never mutates or replaces earlier ones.

        Passing ``request_id`` links a claimed retry request to this attempt in
        the same transaction, so the dashboard can follow a clicked retry into
        its live run immediately instead of waiting for the terminal outcome.
        The request must already be claimed (``running``) and unlinked.
        """
        repo = _require_text(repo, "repo")
        pr = _require_pr(pr)
        head_sha = _require_text(head_sha, "head_sha")
        trigger = _require_text(trigger, "trigger")
        retry_of = _optional_text(retry_of, "retry_of")
        request_id = _optional_text(request_id, "request_id")
        now = _now()
        run_id = uuid.uuid4().hex

        with self._write() as conn:
            if request_id is not None:
                request = self._request_row(conn, request_id)
                if request is None:
                    raise ValueError("unknown retry request: %s" % request_id)
                if request["status"] != "running":
                    raise ValueError(
                        "cannot link retry request in state %r (expected running)"
                        % request["status"]
                    )
                if request["run_id"]:
                    raise ValueError(
                        "retry request %s is already linked to run %s"
                        % (request_id, request["run_id"])
                    )
            conn.execute(
                "INSERT INTO runs"
                " (id, repo, pr, head_sha, title, url, trigger_source, retry_of, request_id,"
                "  status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)",
                (
                    run_id,
                    repo,
                    pr,
                    head_sha,
                    self._bounded_text(title) or "",
                    self._bounded_text(url) or "",
                    trigger,
                    retry_of,
                    request_id,
                    now,
                    now,
                ),
            )
            if request_id is not None:
                conn.execute(
                    "UPDATE retry_requests SET run_id = ?, updated_at = ? WHERE id = ?",
                    (run_id, now, request_id),
                )
            self._emit(
                conn,
                "run_started",
                run_id,
                {
                    "status": "running",
                    "repo": repo,
                    "pr": pr,
                    "head_sha": head_sha,
                    "title": self._bounded_text(title) or "",
                    "url": self._bounded_text(url) or "",
                    "trigger": trigger,
                    "retry_of": retry_of,
                    "request_id": request_id,
                },
            )
            return _run_dict(self._run_row(conn, run_id))

    def import_run(
        self,
        run_id: str,
        repo: str,
        pr: int,
        head_sha: str,
        status: str = "succeeded",
        trigger: str = "legacy",
        created_at: str = "",
        stage: str = "",
        error: str = "",
        successful_agents: Optional[int] = None,
        total_agents: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Backfill one historical attempt from the pre-dashboard JSON ledger.

        ``run_id`` is a caller-computed deterministic id (see
        ``dashboard_execution.legacy_run_id``), which makes re-importing the
        ledger on every daemon start idempotent: the first row wins and is never
        rewritten. Imports emit **no** events - a several-hundred-PR ledger would
        otherwise flood the SSE replay stream with fake "just happened" traffic.

        ``created_at`` is stored verbatim, including empty for a ledger entry
        with no ``reviewed_at``: an unknown historical date is labelled unknown
        by the UI rather than invented as today.

        ``status``/``stage``/``error`` carry the ledger's own verdict, so an
        entry whose review POST outcome was never confirmed imports as
        ``interrupted`` with its reason instead of as a success.
        """
        run_id = _require_text(run_id, "run_id")
        repo = _require_text(repo, "repo")
        pr = _require_pr(pr)
        head_sha = _require_text(head_sha, "head_sha")
        trigger = _require_text(trigger, "trigger")
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError(
                "imported runs must be terminal (%s), got %r"
                % (", ".join(TERMINAL_RUN_STATUSES), status)
            )
        # Counts stay optional: a ledger entry that never recorded how many
        # agents succeeded imports as NULL rather than as a fabricated 0.
        successful_agents = _optional_count(successful_agents, "successful_agents")
        total_agents = _optional_count(total_agents, "total_agents")
        recorded_at = self._bounded_text("" if created_at is None else str(created_at)) or ""

        with self._write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO runs"
                " (id, repo, pr, head_sha, trigger_source, status, stage, error,"
                "  successful_agents, total_agents, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    repo,
                    pr,
                    head_sha,
                    trigger,
                    status,
                    self._bounded_text(stage) or "",
                    self._bounded_text(error) or "",
                    successful_agents,
                    total_agents,
                    recorded_at,
                    recorded_at,
                ),
            )
            return _run_dict(self._run_row(conn, run_id))

    def update_run(self, run_id: str, status: str, **fields: Any) -> None:
        """Advance an attempt and emit its event in one transaction.

        Only allowlisted fields may be written, and a terminal attempt cannot be
        rewritten - not back to ``running`` and not into a different outcome, so
        the record of a failure survives the retry that follows it.

        ``agent_results`` is kept within its own caps (``bounded_agent_results``)
        next to the ids it holds; the event carries only those ids.
        """
        run_id = _require_text(run_id, "run_id")
        if status not in RUN_STATUSES:
            raise ValueError("run status must be one of %s, got %r" % (", ".join(RUN_STATUSES), status))
        unsupported = sorted(set(fields) - set(RUN_UPDATE_FIELDS))
        if unsupported:
            raise ValueError(
                "unsupported run fields: %s (allowed: %s)"
                % (", ".join(unsupported), ", ".join(RUN_UPDATE_FIELDS))
            )

        assignments = ["status = ?", "updated_at = ?"]
        values: List[Any] = [status, _now()]
        for field in RUN_UPDATE_FIELDS:
            if field not in fields:
                continue
            if field == "agent_results":
                kept = bounded_agent_results(fields[field])
                assignments.extend(["agent_results = ?", "reusable_agents = ?"])
                values.extend(
                    [
                        json.dumps(kept, ensure_ascii=False, sort_keys=True) if kept else None,
                        json.dumps([item["agent"] for item in kept]) if kept else None,
                    ]
                )
                continue
            assignments.append("%s = ?" % field)
            values.append(self._coerce_run_field(field, fields[field]))

        with self._write() as conn:
            row = self._run_row(conn, run_id)
            if row is None:
                raise ValueError("unknown run: %s" % run_id)
            current = row["status"]
            if current in TERMINAL_RUN_STATUSES and status != current:
                raise ValueError("cannot transition run from %r to %r" % (current, status))
            conn.execute(
                "UPDATE runs SET %s WHERE id = ?" % ", ".join(assignments),
                tuple(values) + (run_id,),
            )
            self._emit(conn, "run_updated", run_id, run_event_payload(status, fields))

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = self._run_row(conn, run_id)
            return None if row is None else _run_dict(row)

    def get_agent_results(self, run_id: str) -> Dict[str, Dict[str, Any]]:
        """The agent results an attempt kept for a retry, by agent id ({} when none).

        A separate read: run rows carry only the ids (``reusable_agents``), so a
        runs page never loads every kept finding, and the web tier never does.
        """
        run_id = _require_text(run_id, "run_id")
        with self._connect() as conn:
            row = conn.execute("SELECT agent_results FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return {}
        return {item["agent"]: item for item in bounded_agent_results(_decode_json(row["agent_results"]))}

    def list_runs(
        self,
        status: Optional[str] = None,
        repo: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Newest-first attempts, hard-bounded so a client cannot ask for the world.

        Ordered by the attempt's own timestamp, not by insertion: startup
        backfills append old history after newer live rows, and insertion order
        would push a recent review behind years-old failures. Timestamps are ISO
        8601 UTC strings of one shape (``_now``/ledger ``reviewed_at``), so text
        order is time order; an unknown date (empty) sorts last, and ``seq``
        breaks ties so the order is stable.
        """
        limit = min(max(int(limit), 1), self.max_list_limit)
        offset = max(int(offset), 0)
        clauses: List[str] = []
        values: List[Any] = []
        if status is not None:
            clauses.append("status = ?")
            values.append(status)
        if repo is not None:
            clauses.append("repo = ?")
            values.append(repo)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs%s ORDER BY created_at DESC, seq DESC LIMIT ? OFFSET ?" % where,
                tuple(values) + (limit, offset),
            ).fetchall()
        return [_run_dict(row) for row in rows]

    def run_status_counts(self, since: str) -> Dict[str, Dict[str, int]]:
        """One consistent snapshot of every status: ``{status: {total, recent}}``.

        A summary reader needs the buckets to partition the same set of rows.
        Separate per-status counts would each open their own read, so an
        attempt that transitions between two of them would be counted in two
        buckets; this single GROUP BY query reads every count from one
        statement, in SQL rather than by paging ``list_runs`` (which would
        stream years of history and still be capped by ``max_list_limit``).

        ``recent`` is the ``updated_at >= since`` window. ``updated_at`` is the
        completion time for anything terminal: ``update_run`` rewrites it on
        every transition and refuses to rewrite a terminal row afterwards. The
        comparison is textual - every timestamp this store writes is an ISO 8601
        UTC string of one shape - and an imported attempt whose date is unknown
        stores an empty string, so it never qualifies.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS total,"
                " SUM(CASE WHEN updated_at >= ? THEN 1 ELSE 0 END) AS recent"
                " FROM runs GROUP BY status",
                (str(since),),
            ).fetchall()
        return {
            str(row["status"]): {"total": int(row["total"]), "recent": int(row["recent"] or 0)}
            for row in rows
        }

    def attempts_for(
        self,
        repo: str,
        pr: int,
        head_sha: str,
        limit: int = 20,
        triggers: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Attempts recorded for one (repo, pr, head), oldest first, bounded.

        ``triggers`` filters in SQL *before* the bound, so an existence check
        for a live ``poll``/``retry`` row cannot be hidden behind a page of
        imported history for the same head. Lets a startup backfill recognise a
        review the dashboard itself already recorded before inserting a legacy
        copy.
        """
        repo = _require_text(repo, "repo")
        pr = _require_pr(pr)
        head_sha = _require_text(head_sha, "head_sha")
        limit = min(max(int(limit), 1), self.max_list_limit)
        clauses = ["repo = ?", "pr = ?", "head_sha = ?"]
        values: List[Any] = [repo, pr, head_sha]
        if triggers is not None:
            wanted = [_require_text(trigger, "trigger") for trigger in triggers]
            if not wanted:
                return []
            clauses.append("trigger_source IN (%s)" % ", ".join("?" * len(wanted)))
            values.extend(wanted)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE %s ORDER BY seq ASC LIMIT ?" % " AND ".join(clauses),
                tuple(values) + (limit,),
            ).fetchall()
        return [_run_dict(row) for row in rows]

    def latest_attempt_for_head(self, repo: str, pr: int, head_sha: str) -> Optional[Dict[str, Any]]:
        """The single newest attempt for one (repo, pr, head), or None.

        Reviewer MUST-FIX: ``attempts_for`` orders oldest-first and bounds the
        result at ``limit``; taking ``rows[-1]`` of that window is the newest
        row *within the window*, not the newest attempt, once a head
        accumulates more attempts than the window holds. This queries the
        newest row directly, so the unknown-outcome refusal and ``retry_of``
        always see the real latest attempt.
        """
        repo = _require_text(repo, "repo")
        pr = _require_pr(pr)
        head_sha = _require_text(head_sha, "head_sha")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE repo = ? AND pr = ? AND head_sha = ? ORDER BY seq DESC LIMIT 1",
                (repo, pr, head_sha),
            ).fetchone()
        return None if row is None else _run_dict(row)

    def active_activity_for_pr(
        self, repo: str, pr: int
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """``(queued/running request, running run)`` for (repo, pr), any head.

        Reviewer MUST-FIX: the web layer used to page ``list_retries``/
        ``list_runs`` one ``MAX_LIST_LIMIT`` page at a time and filter by PR in
        Python, so a busy PR beyond that first page was invisible. This is a
        direct existence query scoped to the PR (the retry_requests side uses
        ``idx_requests_target``'s ``(repo, pr, ...)`` prefix).
        """
        repo = _require_text(repo, "repo")
        pr = _require_pr(pr)
        with self._connect() as conn:
            request_row = conn.execute(
                "SELECT * FROM retry_requests WHERE repo = ? AND pr = ? AND status IN (?, ?)"
                " ORDER BY seq LIMIT 1",
                (repo, pr) + ACTIVE_REQUEST_STATUSES,
            ).fetchone()
            run_row = conn.execute(
                "SELECT * FROM runs WHERE repo = ? AND pr = ? AND status = 'running' ORDER BY seq LIMIT 1",
                (repo, pr),
            ).fetchone()
        request = _request_detail_dict(request_row) if request_row is not None else None
        run = _run_dict(run_row) if run_row is not None else None
        return request, run

    def has_attempt(
        self,
        repo: str,
        pr: int,
        head_prefix: str,
        triggers: Sequence[str],
        status: Optional[str] = None,
        run_dir: Optional[str] = None,
        stages: Optional[Sequence[str]] = None,
        not_status: Optional[str] = None,
    ) -> bool:
        """Whether an attempt matching every given piece of evidence exists.

        One EXISTS query, so the answer cannot be hidden behind a page bound:
        ``head_prefix`` matches ``head_sha`` by prefix (a retained log keeps 12
        chars, a ledger the full 40), ``triggers`` narrows to the executor's
        own rows, ``status`` to the outcome, and ``run_dir`` to one execution
        (the mkdtemp directory name is unique per attempt, so this is identity,
        not proximity).
        """
        repo = _require_text(repo, "repo")
        pr = _require_pr(pr)
        head_prefix = _require_text(head_prefix, "head_prefix")
        wanted = [_require_text(trigger, "trigger") for trigger in triggers]
        if not wanted:
            return False
        clauses = ["repo = ?", "pr = ?", "substr(head_sha, 1, ?) = ?"]
        values: List[Any] = [repo, pr, len(head_prefix), head_prefix]
        clauses.append("trigger_source IN (%s)" % ", ".join("?" * len(wanted)))
        values.extend(wanted)
        if status is not None:
            clauses.append("status = ?")
            values.append(_require_text(status, "status"))
        if run_dir is not None:
            clauses.append("run_dir = ?")
            values.append(_require_text(run_dir, "run_dir"))
        if stages is not None:
            wanted_stages = [_require_text(stage, "stage") for stage in stages]
            if not wanted_stages:
                return False
            clauses.append("stage IN (%s)" % ", ".join("?" * len(wanted_stages)))
            values.extend(wanted_stages)
        if not_status is not None:
            clauses.append("status <> ?")
            values.append(_require_text(not_status, "not_status"))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM runs WHERE %s LIMIT 1" % " AND ".join(clauses), tuple(values)
            ).fetchone()
        return row is not None

    def complete_head(self, run_id: str, head_sha: str, error: Optional[str] = None) -> bool:
        """Replace a prefix-only head on an imported row with its verified full SHA.

        Only widens: the stored head must be a proper prefix of ``head_sha`` and
        the row must be terminal. Status and outcome are untouched, so a failure
        stays a failure; the retry gate can now match GitHub's full head. Emits
        no event (history, not something that just happened). Returns whether a
        row changed.
        """
        run_id = _require_text(run_id, "run_id")
        head_sha = _require_text(head_sha, "head_sha")
        with self._write() as conn:
            row = self._run_row(conn, run_id)
            if row is None:
                return False
            current = str(row["head_sha"] or "")
            if row["status"] not in TERMINAL_RUN_STATUSES:
                return False
            if len(current) >= len(head_sha) or not head_sha.startswith(current):
                return False
            assignments = ["head_sha = ?", "updated_at = ?"]
            values: List[Any] = [head_sha, _now()]
            if error is not None:
                assignments.append("error = ?")
                values.append(self._bounded_text(error))
            conn.execute(
                "UPDATE runs SET %s WHERE id = ?" % ", ".join(assignments),
                tuple(values) + (run_id,),
            )
            return True

    # ------------------------------------------------------- PR authors, feedback
    # Written by the daemon from ``@review-of-legends`` comments and the welcome
    # comment; the web tier only reads them. Events carry the login and ids,
    # never a language or feedback text.

    def get_pr_author(self, login: str) -> Optional[Dict[str, Any]]:
        """One author's settings (matched case-insensitively), or None when there are none."""
        key = _require_login(login).lower()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM pr_authors WHERE login = ?", (key,)).fetchone()
            return None if row is None else dict(row)

    def set_author_language(
        self,
        login: str,
        lang: Optional[str],
        comment_url: str = "",
        source_at: Optional[str] = None,
        source_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Set an author's review language, or clear it with ``None``; emits ``author_updated``.

        ``source_at`` is when the command was written (its comment's
        updated_at, GitHub's ``YYYY-MM-DDTHH:MM:SSZ``): a command older than the
        one the setting came from changes nothing, wherever it was read and
        however late, and the row returned is the setting that stands (its
        ``lang_comment_url`` says whose). Within one second ``source_key`` (the
        comment, mentions.source_key) decides, so the commands of a second end
        the same in any order and a replay never undoes a later one. None
        writes unconditionally.
        """
        display = _require_login(login)
        if lang is not None and (not isinstance(lang, str) or not LANGUAGE_CODE_PATTERN.fullmatch(lang)):
            raise ValueError("lang must be a lowercase language code such as ko or zh-tw, got %r" % (lang,))
        source_at = _optional_text(source_at, "source_at")
        source_key = _optional_text(source_key, "source_key")
        now = _now()
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT INTO pr_authors (login, display_login, lang, lang_set_at, lang_comment_url,"
                " lang_source_at, lang_source_key, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(login) DO UPDATE SET display_login = excluded.display_login, lang = excluded.lang,"
                " lang_set_at = excluded.lang_set_at, lang_comment_url = excluded.lang_comment_url,"
                " lang_source_at = excluded.lang_source_at, lang_source_key = excluded.lang_source_key,"
                " updated_at = excluded.updated_at"
                " WHERE excluded.lang_source_at IS NULL OR pr_authors.lang_source_at IS NULL"
                " OR pr_authors.lang_source_at < excluded.lang_source_at"
                " OR (pr_authors.lang_source_at = excluded.lang_source_at"
                " AND IFNULL(pr_authors.lang_source_key, '') <= IFNULL(excluded.lang_source_key, ''))",
                (display.lower(), display, lang, now, self._bounded_text(comment_url) or None, source_at,
                 source_key, now),
            )
            if cursor.rowcount:
                self._emit(conn, "author_updated", None, {"login": display.lower()})
            row = conn.execute("SELECT * FROM pr_authors WHERE login = ?", (display.lower(),)).fetchone()
            return dict(row)

    def mark_author_welcomed(self, login: str, welcomed_pr: str) -> Dict[str, Any]:
        """Record that the welcome comment went up (``welcomed_pr`` is ``owner/name#N``); emits ``author_updated``."""
        display = _require_login(login)
        welcomed_pr = _require_text(welcomed_pr, "welcomed_pr")
        now = _now()
        with self._write() as conn:
            conn.execute(
                "INSERT INTO pr_authors (login, display_login, welcomed_at, welcomed_pr, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(login) DO UPDATE SET display_login = excluded.display_login,"
                " welcomed_at = excluded.welcomed_at, welcomed_pr = excluded.welcomed_pr,"
                " updated_at = excluded.updated_at",
                (display.lower(), display, now, self._bounded_text(welcomed_pr), now),
            )
            self._emit(conn, "author_updated", None, {"login": display.lower()})
            row = conn.execute("SELECT * FROM pr_authors WHERE login = ?", (display.lower(),)).fetchone()
            return dict(row)

    def list_pr_authors(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """Authors with a language or a welcome, most recently changed first, bounded."""
        limit = min(max(int(limit), 1), self.max_list_limit)
        offset = max(int(offset), 0)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pr_authors WHERE lang IS NOT NULL OR welcomed_at IS NOT NULL"
                " ORDER BY updated_at DESC, login ASC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_feedback(
        self, repo: str, pr: int, login: str, body: str, comment_url: str = "", source_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """Store one piece of feedback (text cut at MAX_FEEDBACK_CHARS) and prune past MAX_FEEDBACK_ROWS.

        Emits ``feedback_added`` with the row id and the login only: the text
        stays in this table, and the event stream is not a place for it.
        ``source_key`` names the comment it came from: a second call with the
        same key returns the stored row and changes nothing, so the daemon can
        replay a comment whose cursor it could not save.
        """
        repo = _require_text(repo, "repo")
        pr = _require_pr(pr)
        display = _require_login(login)
        if not isinstance(body, str):
            raise ValueError("body must be a string")
        source_key = _optional_text(source_key, "source_key")
        text = body.strip()
        if len(text) > MAX_FEEDBACK_CHARS:
            text = text[: MAX_FEEDBACK_CHARS - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT INTO feedback (repo, pr, login, body, comment_url, created_at, source_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source_key) DO NOTHING",
                (repo, pr, display, text, self._bounded_text(comment_url) or "", _now(), source_key),
            )
            if cursor.rowcount == 0:
                # Recorded before: no row, no prune, no event.
                row = conn.execute("SELECT * FROM feedback WHERE source_key = ?", (source_key,)).fetchone()
                return _feedback_dict(row)
            feedback_id = int(cursor.lastrowid)
            conn.execute(
                "DELETE FROM feedback WHERE id NOT IN (SELECT id FROM feedback ORDER BY id DESC LIMIT ?)",
                (MAX_FEEDBACK_ROWS,),
            )
            self._emit(conn, "feedback_added", None, {"feedback_id": feedback_id, "login": display.lower()})
            row = conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
            return _feedback_dict(row)

    # ------------------------------------------------ contributor gate (gate.py)
    # A verification code is shown once on the dashboard and stored hashed, for
    # the one GitHub login the signed-in person named; only that login's
    # ``@review-of-legends verify <code>`` comment consumes it, binding the
    # account (login and immutable user id) to the signed-in email. A code read
    # off a public comment is worthless to anyone else. Pledge signatures are kept for
    # the record and to find the pull request that was opened for them.

    def create_contributor_code(self, email: str, code_hash: str, login: str, ttl_seconds: int = 3600) -> str:
        """Store one code for ``email`` and the GitHub ``login`` it is for; returns when it expires.

        Expired unused codes are pruned, and used ones a day after their use:
        until then the comment that used one can be handled again (its reply
        failed) and get the same answer.
        """
        email = _require_text(email, "email").lower()
        code_hash = _require_text(code_hash, "code_hash")
        login = _require_login(login).lower()
        now = dt.datetime.now(dt.timezone.utc)
        expires = (now + dt.timedelta(seconds=int(ttl_seconds))).isoformat(timespec="seconds")
        stamp = now.isoformat(timespec="seconds")
        with self._write() as conn:
            used_before = (now - dt.timedelta(seconds=USED_CODE_KEPT_SECONDS)).isoformat(timespec="seconds")
            conn.execute(
                "DELETE FROM contributor_codes WHERE (used_at IS NULL AND expires_at < ?) OR used_at < ?",
                (stamp, used_before),
            )
            conn.execute(
                "INSERT OR REPLACE INTO contributor_codes (code_hash, email, login, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (code_hash, email, login, stamp, expires),
            )
        return expires

    def consume_contributor_code(
        self, code_hash: str, login: str, comment_url: str = "", user_id: int = 0
    ) -> Optional[str]:
        """Use a live code once, by the login it was issued for: bind the account to its email and return the email.

        None when the code is unknown, used, expired or issued for another login
        (that comment does not use it up). The comment that used it, handled
        again (its reply failed), gets the email again and changes nothing.
        """
        code_hash = _require_text(code_hash, "code_hash")
        display = _require_login(login)
        now = _now()
        comment_url = self._bounded_text(comment_url) or ""
        with self._write() as conn:
            row = conn.execute(
                "SELECT email FROM contributor_codes WHERE code_hash = ? AND used_at IS NULL AND expires_at >= ?"
                " AND login = ?",
                (code_hash, now, display.lower()),
            ).fetchone()
            if row is None:
                if not comment_url:
                    return None
                replay = conn.execute(
                    "SELECT k.email FROM contributor_codes k JOIN contributor_identities i ON i.login = k.used_by"
                    " WHERE k.code_hash = ? AND k.used_by = ? AND i.comment_url = ? AND i.email = k.email",
                    (code_hash, display.lower(), comment_url),
                ).fetchone()
                return str(replay["email"]) if replay is not None else None
            email = str(row["email"])
            conn.execute(
                "UPDATE contributor_codes SET used_at = ?, used_by = ? WHERE code_hash = ?",
                (now, display.lower(), code_hash),
            )
            conn.execute(
                "INSERT INTO contributor_identities (login, display_login, email, user_id, verified_at, comment_url)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(login) DO UPDATE SET display_login = excluded.display_login,"
                " email = excluded.email, user_id = excluded.user_id, verified_at = excluded.verified_at,"
                " comment_url = excluded.comment_url",
                (display.lower(), display, email, int(user_id or 0), now, comment_url),
            )
            self._emit(conn, "contributor_verified", None, {"login": display.lower()})
            return email

    def contributor_identity(self, login: str) -> Optional[Dict[str, Any]]:
        """``{login, email, user_id, verified_at, comment_url}`` bound to a login, or None."""
        if not isinstance(login, str) or not LOGIN_PATTERN.fullmatch(login):
            return None  # a bot account or nothing: never bound
        with self._connect() as conn:
            row = conn.execute(
                "SELECT display_login AS login, email, user_id, verified_at, comment_url FROM contributor_identities"
                " WHERE login = ?",
                (login.lower(),),
            ).fetchone()
            return None if row is None else dict(row)

    def record_pledge(
        self, repo: str, pr: int, login: str, user_id: int, comment_url: str, pledge_sha: str,
        source_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record a pledge signature; the same ``source_key`` returns the stored row unchanged."""
        repo = _require_text(repo, "repo")
        pr = _require_pr(pr)
        display = _require_login(login)
        source_key = _optional_text(source_key, "source_key")
        with self._write() as conn:
            cursor = conn.execute(
                "INSERT INTO pledge_signatures (repo, pr, login, user_id, comment_url, pledge_sha, signed_at, source_key)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source_key) DO NOTHING",
                (repo, pr, display, int(user_id or 0), self._bounded_text(comment_url) or "",
                 str(pledge_sha or ""), _now(), source_key),
            )
            if cursor.rowcount:
                self._emit(conn, "pledge_signed", None, {"login": display.lower(), "repo": repo, "pr": pr})
                row = conn.execute("SELECT * FROM pledge_signatures WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
            else:
                row = conn.execute("SELECT * FROM pledge_signatures WHERE source_key = ?", (source_key,)).fetchone()
            return dict(row)

    def set_pledge_pull(self, signature_id: int, pull_url: str) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE pledge_signatures SET pull_url = ? WHERE id = ?",
                (self._bounded_text(pull_url) or None, int(signature_id)),
            )

    def list_feedback(
        self, limit: int = 50, before_id: Optional[int] = None, after_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Feedback paged by row id, bounded: the newest rows, newest first; with
        ``before_id`` the rows below it, newest first; with ``after_id`` the rows
        right above it, oldest first, so a reader catching up leaves no gap.

        Ids only grow and pruning only drops the lowest, so a key names the same
        place however much feedback arrives between two pages; an offset would
        shift by every row added on top.
        """
        limit = min(max(int(limit), 1), self.max_list_limit)
        before_id = _optional_row_id(before_id, "before_id")
        after_id = _optional_row_id(after_id, "after_id")
        if before_id is not None and after_id is not None:
            raise ValueError("give before_id or after_id, not both")
        if after_id is not None:
            query, values = "SELECT * FROM feedback WHERE id > ? ORDER BY id ASC LIMIT ?", (after_id, limit)
        elif before_id is not None:
            query, values = "SELECT * FROM feedback WHERE id < ? ORDER BY id DESC LIMIT ?", (before_id, limit)
        else:
            query, values = "SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,)
        with self._connect() as conn:
            rows = conn.execute(query, values).fetchall()
        return [_feedback_dict(row) for row in rows]

    # ------------------------------------------------------------------- events

    def events_after(self, cursor: int = 0, limit: int = 100) -> List[Dict[str, Any]]:
        """Ascending events with id greater than ``cursor`` (SSE replay)."""
        cursor = max(int(cursor or 0), 0)
        limit = min(max(int(limit), 1), self.max_event_limit)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT ?",
                (cursor, limit),
            ).fetchall()
        return [_event_dict(row) for row in rows]

    def latest_event_id(self) -> int:
        """Highest event id, or 0 when the stream is empty.

        A client connecting now starts tailing from here instead of replaying the
        whole history. Read-only: nothing is pruned or deleted.
        """
        with self._connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) AS latest FROM events").fetchone()
            return int(row["latest"])

    def latest_event_at(self) -> Optional[str]:
        """Timestamp of the newest event, or ``None`` when nothing was recorded.

        "When did this deployment last do anything" for a summary reader that
        does not subscribe to the stream. Ids are monotonic, so the newest id is
        the newest event; its ``created_at`` is returned verbatim rather than
        recomputed here.
        """
        with self._connect() as conn:
            row = conn.execute("SELECT created_at FROM events ORDER BY id DESC LIMIT 1").fetchone()
            return None if row is None else row["created_at"]

    # ----------------------------------------------------------------- recovery

    def recover_interrupted(self) -> None:
        """Close out work that a crash or restart left mid-flight.

        The caller MUST already hold the exclusive executor lock: otherwise this
        would mark a live daemon's attempt interrupted. Running attempts become
        ``interrupted``. A claimed request whose linked attempt is already
        terminal takes that attempt's outcome: the crash window between
        ``update_run(succeeded)`` and ``finish_retry`` must not turn a posted
        review into a permanently failed request. Every other claimed request
        becomes ``failed`` - never ``succeeded`` by inference, and never
        re-queued, because it may already have posted its review to GitHub.
        Queued requests are untouched.
        """
        now = _now()
        with self._write() as conn:
            for row in conn.execute(
                "SELECT * FROM runs WHERE status = 'running' ORDER BY seq"
            ).fetchall():
                conn.execute(
                    "UPDATE runs SET status = 'interrupted', updated_at = ?,"
                    " error = COALESCE(error, ?) WHERE id = ?",
                    (now, INTERRUPTED_NOTE, row["id"]),
                )
                self._emit(
                    conn,
                    "run_updated",
                    row["id"],
                    {"status": "interrupted", "error": INTERRUPTED_NOTE},
                )
            for row in conn.execute(
                "SELECT * FROM retry_requests WHERE status = 'running' ORDER BY seq"
            ).fetchall():
                status, error = "failed", INTERRUPTED_NOTE
                linked = self._run_row(conn, row["run_id"]) if row["run_id"] else None
                if linked is not None and linked["status"] == "succeeded":
                    # Durable proof the review was posted; only the request's
                    # own terminal write was lost.
                    status, error = "succeeded", None
                elif linked is not None and linked["status"] in TERMINAL_RUN_STATUSES:
                    error = str(linked["error"] or "") or (
                        "attempt %s ended %s" % (linked["id"], linked["status"])
                    )
                # Assigned, not COALESCEd: the outcome chosen above is the
                # truth, including NULL for a request whose review was posted.
                conn.execute(
                    "UPDATE retry_requests SET status = ?, updated_at = ?, error = ? WHERE id = ?",
                    (status, now, error, row["id"]),
                )
                self._emit(
                    conn,
                    "retry_finished",
                    row["run_id"],
                    {
                        "request_id": row["id"],
                        "status": status,
                        "repo": row["repo"],
                        "pr": row["pr"],
                        "error": error,
                    },
                )

    # -------------------------------------------------------------- plumbing

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """One short-lived connection per operation (thread/process safe)."""
        conn = sqlite3.connect(
            str(self.path), timeout=self._busy_timeout_ms / 1000.0, isolation_level=None
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = %d" % self._busy_timeout_ms)
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Serialized write transaction: read-then-write cannot interleave."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        self._harden_permissions()

    def _harden_permissions(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(str(self.path) + suffix, 0o600)
            except OSError:
                pass

    def _emit(self, conn: sqlite3.Connection, kind: str, run_id: Optional[str], payload: Dict[str, Any]) -> None:
        # A setting event too large to keep still names its setting: open tabs
        # re-read by key (a long admin list must still reach every tab).
        keep = ("key",) if kind in ("setting_updated", "setting_deleted") else ()
        conn.execute(
            "INSERT INTO events (kind, run_id, payload, created_at) VALUES (?, ?, ?, ?)",
            (kind, run_id, self._bounded_json(payload, keep), _now()),
        )

    def _request_row(self, conn: sqlite3.Connection, request_id: str) -> Optional[sqlite3.Row]:
        return conn.execute("SELECT * FROM retry_requests WHERE id = ?", (request_id,)).fetchone()

    def _run_row(self, conn: sqlite3.Connection, run_id: str) -> Optional[sqlite3.Row]:
        return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

    def _coerce_run_field(self, field: str, value: Any) -> Any:
        if value is None:
            return None
        if field in RUN_TEXT_FIELDS:
            return self._bounded_text(value)
        if field in RUN_INT_FIELDS:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("%s must be an int, got %r" % (field, value))
            return value
        return self._bounded_json(value)

    def _bounded_text(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value)
        if len(text) <= self.max_text_chars:
            return text
        return text[: self.max_text_chars - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER

    def _bounded_json(self, value: Any, keep: Tuple[str, ...] = ()) -> str:
        """Serialize, or replace with a marker (plus the ``keep`` fields): the store is not a log sink."""
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if len(encoded) <= self.max_json_chars:
            return encoded
        marker = {"truncated": True, "original_chars": len(encoded)}  # type: Dict[str, Any]
        if isinstance(value, dict):
            marker.update((field, value[field]) for field in keep if field in value)
        return json.dumps(marker)


def bounded_agent_results(value: Any) -> List[Dict[str, Any]]:
    """The successful agent results a failed attempt keeps for a retry, within the caps.

    In order, one per agent id, each kept whole (the dict run_agent returned, which
    service.review_pr stamps with the base and diff it was reviewed against and the
    review language it is written in: with the head, the reuse identity). A
    failed result (it has an ``error`` key), one without a valid agent id, one
    that is not plain JSON, one past MAX_AGENT_RESULT_CHARS and one that would
    take the list past MAX_AGENT_RESULTS_CHARS are left out: that agent runs
    again on a retry. Idempotent on its own output.
    """
    kept: List[Dict[str, Any]] = []
    if not isinstance(value, (list, tuple)):
        return kept
    seen = set()
    total = len("[]")
    for item in value:
        if len(kept) >= MAX_AGENT_RESULTS:
            break
        if not isinstance(item, dict) or "error" in item:
            continue
        agent_id = item.get("agent")
        if not isinstance(agent_id, str) or not AGENT_ID_PATTERN.fullmatch(agent_id) or agent_id in seen:
            continue
        try:
            size = len(json.dumps(item, ensure_ascii=False, sort_keys=True))
        except (TypeError, ValueError):
            continue
        # ", " separates list items in the encoding update_run stores.
        if size > MAX_AGENT_RESULT_CHARS or total + size + len(", ") > MAX_AGENT_RESULTS_CHARS:
            continue
        kept.append(item)
        seen.add(agent_id)
        total += size + len(", ")
    return kept


def run_event_payload(status: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """The ``run_updated`` payload for an update: kept agent results travel as their ids only.

    The recorder measures the same payload to fit the agent rows into the event
    (dashboard_execution._fit_agents_to_event).
    """
    payload: Dict[str, Any] = {"status": status, **{field: fields[field] for field in RUN_UPDATE_FIELDS if field in fields}}
    if "agent_results" in payload:
        payload["reusable_agents"] = [item["agent"] for item in bounded_agent_results(payload.pop("agent_results"))]
    return payload


def _reusable_agent_ids(value: Optional[str]) -> List[str]:
    decoded = _decode_json(value)
    if not isinstance(decoded, list):
        return []
    return [item for item in decoded[:MAX_AGENT_RESULTS] if isinstance(item, str) and AGENT_ID_PATTERN.fullmatch(item)]


def _require_setting_key(key: Any) -> str:
    if key in SETTING_KEYS:
        return str(key)
    for prefix in (AGENT_SETTING_PREFIX, AGENT_META_PREFIX):
        if isinstance(key, str) and key.startswith(prefix) and AGENT_ID_PATTERN.fullmatch(key[len(prefix):]):
            return key
    raise ValueError(
        "unknown setting %r (known: %s, %s<agent id>)" % (key, ", ".join(SETTING_KEYS), AGENT_SETTING_PREFIX)
    )


def _require_login(value: Any) -> str:
    if not isinstance(value, str) or not LOGIN_PATTERN.fullmatch(value):
        raise ValueError("login must be a GitHub login, got %r" % (value,))
    return value


def _feedback_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "repo": row["repo"],
        "pr": row["pr"],
        "login": row["login"],
        "body": row["body"],
        "comment_url": row["comment_url"],
        "created_at": row["created_at"],
    }


def _setting_event(key: str, value: Dict[str, Any], updated_by: str) -> Dict[str, Any]:
    """``setting_updated`` payload: an agent prompt never rides the event stream, only its size."""
    if key == REPOSITORIES_SETTING:
        # Names only: who added each one stays in the settings row.
        names = [str(entry.get("repo")) for entry in value.get("added") or [] if isinstance(entry, dict)]
        return {"key": key, "updated_by": updated_by, "repos": names}
    if key == ADMINS_SETTING:
        # The granted addresses only, like the repository names above.
        emails = [str(entry.get("email")) for entry in value.get("added") or [] if isinstance(entry, dict)]
        return {"key": key, "updated_by": updated_by, "emails": emails}
    if key.startswith(AGENT_META_PREFIX):
        # The sidecar holds a kept prompt: the key alone tells a tab to re-read.
        return {"key": key, "updated_by": updated_by}
    if not key.startswith(AGENT_SETTING_PREFIX):
        return {"key": key, "value": value, "updated_by": updated_by}
    payload = {"key": key, "updated_by": updated_by}  # type: Dict[str, Any]
    if isinstance(value.get("required"), bool):
        payload["required"] = value["required"]
    if isinstance(value.get("prompt"), str):
        payload["prompt_chars"] = len(value["prompt"])
    return payload


class SupersededConflict(Exception):
    """The kept edit an admin acted on is not the one stored now (another tab, or a later replacement)."""


def prompt_text_sha(text: str) -> str:
    """Identity of a prompt text; the same digest as service.prompt_sha (surrounding whitespace ignored)."""
    return hashlib.sha256(str(text).strip().encode("utf-8")).hexdigest()


def _require_superseded(kept: Dict[str, Any], expected_sha: Optional[str]) -> None:
    if expected_sha != prompt_text_sha(kept.get("prompt") or ""):
        raise SupersededConflict("the replaced edit changed since this page loaded it")


def _document_sha(row: Dict[str, Any]) -> str:
    """The identity of one write of ``agent:<id>``: its value, time and author."""
    identity = {"value": row["value"], "updated_at": row["updated_at"], "updated_by": row["updated_by"]}
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _setting_dict(row: sqlite3.Row) -> Dict[str, Any]:
    value = _decode_json(row["value"])
    return {
        "key": row["key"],
        "value": value if isinstance(value, dict) else {},
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


def _request_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """The frozen W1 request shape (enqueue/claim/list)."""
    return {
        "id": row["id"],
        "status": row["status"],
        "repo": row["repo"],
        "pr": row["pr"],
        "head_sha": row["head_sha"],
        "requested_by": row["requested_by"],
        "retry_of": row["retry_of"],
        "options": _decode_json(row["options"]),
        "created_at": row["created_at"],
    }


def _request_detail_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Single-request view: adds the run link, the error and the last change."""
    detail = _request_dict(row)
    detail["run_id"] = row["run_id"]
    detail["error"] = row["error"]
    detail["updated_at"] = row["updated_at"]
    return detail


def _run_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "repo": row["repo"],
        "pr": row["pr"],
        "head_sha": row["head_sha"],
        "title": row["title"],
        "url": row["url"],
        "trigger": row["trigger_source"],
        "retry_of": row["retry_of"],
        "request_id": row["request_id"],
        "run_dir": row["run_dir"],
        "llm": _decode_json(row["llm"]),
        # Ids only: the results themselves are read with get_agent_results.
        "reusable_agents": _reusable_agent_ids(row["reusable_agents"]),
        "status": row["status"],
        "stage": row["stage"],
        "agents": _decode_json(row["agents"]),
        "review_url": row["review_url"],
        "error": row["error"],
        "successful_agents": row["successful_agents"],
        "total_agents": row["total_agents"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _event_dict(row: sqlite3.Row) -> Dict[str, Any]:
    payload = _decode_json(row["payload"])
    return {
        "id": row["id"],
        "kind": row["kind"],
        "run_id": row["run_id"],
        "payload": payload if isinstance(payload, dict) else {"value": payload},
        "created_at": row["created_at"],
    }


def _decode_json(value: Optional[str]) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None
