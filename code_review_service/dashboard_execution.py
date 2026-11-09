"""Executor-side coordination for the admin dashboard (W2 of .prd/admin-dashboard/loop.md).

This module owns everything the review executor needs in order to be observable
and remotely retryable *without* giving the web tier any way to run a review:

- ``ExecutorLock``: an OS advisory lock (``flock``) that makes "at most one
  review executor" a property of the kernel rather than of a convention. The
  daemon/once entrypoints hold it across state load and every state write.
- ``DashboardRecorder``: durable attempt lifecycle (start, stages, agent rows,
  terminal outcome) written through the W1 ``DashboardStore``. It never raises
  into the review path, and it never invents a ``succeeded`` outcome: only a
  confirmed GitHub review POST may do that.
- ``drain_retry_requests``: serial consumption of operator retry requests. It
  refuses to run unless the executor lock is held, revalidates the PR against
  GitHub before any review, and refuses to re-post when the previous attempt's
  posting outcome is unknown.
- ``RetryWaiter``: the daemon's interruptible sleep, so a queued request starts
  within a second instead of after the poll interval.
- ``import_legacy_ledger``: truthful backfill of the pre-dashboard JSON ledger
  (baseline entries were never reviewed -> ``skipped``; real reviews ->
  ``succeeded``), with deterministic ids so it is idempotent.

Deliberately stdlib only and Python 3.9 compatible: the daemon runs under
``/usr/bin/python3`` (3.9.6 on the deployment host, see service.sh:107).
"""

from __future__ import annotations

import datetime as dt
import errno
import fcntl
import hashlib
import inspect
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Single source of truth for run statuses lives in the store (W1).
from .dashboard_store import TERMINAL_RUN_STATUSES, DashboardStore
# Per-agent dashboard overrides: key ``agent:<id>`` (dashboard_store.AGENT_SETTING_PREFIX).
from .dashboard_store import AGENT_ID_PATTERN, AGENT_SETTING_PREFIX, MAX_AGENT_PROMPT_CHARS, prompt_text_sha
# Dashboard-added review repositories (service.review_repositories).
from .dashboard_store import REPOSITORIES_SETTING
# A dashboard retry may re-run only the agents a failed attempt did not keep.
from .dashboard_store import RETRY_SCOPE_FAILED, RETRY_SCOPE_OPTION, bounded_agent_results, run_event_payload
# The Retrigger badge asks for a review of a head that may already carry one.
from .dashboard_store import RETRY_KIND_OPTION, RETRY_KIND_REREVIEW, RETRY_SEEN_AT_OPTION

# Retry requests are reviews: consuming them is serial and bounded so a large
# queue cannot starve ordinary polling for a whole cycle.
MAX_RETRIES_PER_CYCLE = 20
# Wake latency for a queued request while the daemon sleeps (<= 1s required).
RETRY_WAKE_POLL_SECONDS = 0.5
WAKE_EVENT_KINDS = ("retry_enqueued",)
# Stage names recorded around the irreversible GitHub review POST.
POSTING_STAGE = "posting"
# The POST failed with a transport/5xx error: GitHub may have created the review
# anyway (see service.post_outcome_is_uncertain).
POSTING_UNVERIFIED_STAGE = "posting-unverified"
# The POST was rejected with an explicit 4xx: no review exists, retry is safe.
POSTING_REJECTED_STAGE = "posting-rejected"
# Stages after which a GitHub review POST may already have happened. An attempt
# that stopped here has an unknown posting outcome and must never be re-posted,
# by the poll loop or by an operator retry, until it is reconciled against
# GitHub (head sha + bot author + review marker).
POSTING_STAGES = (POSTING_STAGE, POSTING_UNVERIFIED_STAGE, "summary")
FAILED_RUN_STATUSES = ("failed", "stale", "interrupted")
MAX_ERROR_CHARS = 500
MAX_AGENT_ROWS = 32
MAX_AGENT_SUMMARY_CHARS = 200
#: Per-agent model metadata carried on the agent row (bounded scalars only).
AGENT_TEXT_FIELDS = (("model", 80), ("effort", 16), ("response_model", 80), ("route", 240), ("outcome", 32))
AGENT_INT_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "attempts")
AGENT_FLOAT_FIELDS = ("elapsed_seconds",)
#: Why an agent or one of its model hops failed (service.attempt_log_from_meta,
#: service.agent_failure_text). Dashboard only: no PR comment reads these keys.
MAX_AGENT_ATTEMPT_LOG = 12
MAX_AGENT_FAILURE_CHARS = 2000
ATTEMPT_LOG_TEXT_FIELDS = (("model", 80), ("outcome", 32))
ATTEMPT_LOG_INT_FIELDS = ("attempt", "http_status")
ATTEMPT_LOG_FLOAT_FIELDS = ("retry_after_seconds", "elapsed_seconds")
#: The store swaps an ``agents`` value longer than ``max_json_chars`` for a
#: truncation marker. That is the rows' own column limit; the recorder fits them
#: tighter, to what the run_updated event leaves (``_fit_agents_to_event``).
AGENT_ROWS_JSON_BUDGET = DashboardStore.max_json_chars
#: (keep succeeded hops, failure text limit, newest failed hops kept, hop fields),
#: tried in order until the rows fit: succeeded hops go first (they explain
#: nothing), then the text shrinks, then older failed hops go (counted in
#: ``attempt_log_omitted``), and last only the newest failed hop's identity and
#: status stays.
LAST_RESORT_HOP_FIELDS = ("model", "attempt", "outcome", "http_status")
FAILURE_DETAIL_STEPS = (
    (True, MAX_AGENT_FAILURE_CHARS, None, None),
    (False, MAX_AGENT_FAILURE_CHARS, None, None),
    (False, 1000, None, None),
    (False, 500, None, None),
    (False, 250, None, None),
    (False, 100, None, None),
    (False, 0, None, None),
    (False, 0, 6, None),
    (False, 0, 3, None),
    (False, 0, 1, None),
    (False, 0, 1, LAST_RESORT_HOP_FIELDS),
)
#: The store key of the dashboard-set model policy (dashboard_store.MODEL_POLICY_SETTING).
MODEL_POLICY_SETTING = "model_policy"
# Bound the startup scan that positions the wake cursor at the newest event.
MAX_EVENT_SCAN_BATCHES = 200
# Bounded attempts to persist a request's terminal status before falling back to
# reconciliation from the attempt row (no sleeps: the executor must not stall).
FINISH_RETRY_ATTEMPTS = 3
# Bound on requests examined per reconciliation pass.
MAX_RECONCILE_REQUESTS = 200
# Fixed filenames so deployment tooling (W5) can find them without importing us.
EXECUTOR_LOCK_FILENAME = "executor.lock"
EXECUTOR_PAUSE_FILENAME = "executor.pause"

Logger = Callable[[str], None]


def _default_log(message: str) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    print("%s %s" % (now, message), flush=True)


def _logger_or_default(logger: Optional[Logger]) -> Logger:
    return logger if logger is not None else _default_log


def _clip(value: Any, limit: int = MAX_ERROR_CHARS) -> str:
    """Bound any operator/upstream string before it reaches the dashboard."""
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _clip_block(value: Any, limit: int, keep_tail: bool = False) -> str:
    """``_clip`` for text read as a block: line breaks survive, blank lines do not."""
    lines = [" ".join(line.split()) for line in str(value).splitlines()]
    text = "\n".join(line for line in lines if line)
    if len(text) <= limit:
        return text
    return "..." + text[3 - limit :] if keep_tail else text[: limit - 3] + "..."


class ExecutorBusy(RuntimeError):
    """Raised when another process already holds the executor lock."""


class ExecutorLock:
    """Non-blocking exclusive ``flock`` guarding the single review executor.

    ``flock`` is associated with the open file description, so a second holder
    fails even inside the same process. That is intentional: nesting the lock
    would be a bug, not a convenience.
    """

    def __init__(self, path: Path, logger: Optional[Logger] = None) -> None:
        self.path = Path(path)
        self._log = _logger_or_default(logger)
        self._fd = None  # type: Optional[int]

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> "ExecutorLock":
        if self._fd is not None:
            raise ExecutorBusy("executor lock already held by this process: %s" % self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(self.path.parent), 0o700)
        except OSError:
            # Best-effort: an existing directory may be intentionally shared.
            pass
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                raise ExecutorBusy(
                    "another review executor holds the lock %s (pid in file); refusing to run" % self.path
                ) from exc
            raise
        self._fd = fd
        try:
            os.ftruncate(fd, 0)
            os.write(fd, ("%d\n" % os.getpid()).encode("ascii"))
        except OSError:
            # Diagnostic only; the lock itself is already ours.
            pass
        return self

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        # Clear the fd first so a failure in flock/close cannot leave the lock
        # object claiming to be held.
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as exc:
            self._log("WARNING executor lock unlock failed for %s: %s" % (self.path, exc))
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def require_held(self, action: str) -> None:
        if not self.held:
            raise RuntimeError("%s requires the executor lock (%s) to be held" % (action, self.path))

    def __enter__(self) -> "ExecutorLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.release()
        return False


def executor_lock_path(config: Dict[str, Any], state_path: Path) -> Path:
    """Lock file location: next to the state file unless explicitly configured.

    Deployment (W5) needs this path to be predictable: it is
    ``<dirname(state_path)>/executor.lock`` (service-local ``data/`` in the
    packaged layout), or exactly ``config.executor_lock_path`` when set.
    """
    configured = config.get("executor_lock_path")
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(str(configured))))
    return Path(state_path).parent / EXECUTOR_LOCK_FILENAME


def executor_pause_path(config: Dict[str, Any], state_path: Path) -> Path:
    """Sentinel file that asks a running executor to stop taking new work.

    The graceful-deploy contract (W5 owns the deploy scripts, this module owns
    the behavior): create the file, wait for the executor lock to become free,
    then swap the source. The executor finishes the PR/attempt it is on, takes
    no new PR and claims no new retry request, and exits the cycle normally.
    Nothing is killed, so no review child process is orphaned mid-POST.
    """
    configured = config.get("executor_pause_path")
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(str(configured))))
    return Path(state_path).parent / EXECUTOR_PAUSE_FILENAME


def stop_requested(pause_path: Optional[Path]) -> bool:
    """True when a graceful stop sentinel exists (deploy/maintenance)."""
    if pause_path is None:
        return False
    try:
        return Path(pause_path).exists()
    except OSError:
        return False


def probe_executor_lock(path: Path) -> Optional[int]:
    """Return the holder pid if the executor lock is held, else ``None``.

    Read-only helper for deployment/health checks: it never keeps the lock. The
    pid is diagnostic (written by the holder) and may be absent -> ``-1``.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                try:
                    raw = path.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    raw = ""
                try:
                    return int(raw)
                except ValueError:
                    return -1
            raise
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def dashboard_db_path(config: Dict[str, Any], state_path: Path) -> Optional[Path]:
    """Resolve the dashboard DB path, or ``None`` when recording is disabled.

    Recording is opt-in so an existing deployment (and the existing test suite)
    keeps its exact behavior until a dashboard path/flag is configured.
    """
    settings = config.get("dashboard") or {}
    if not isinstance(settings, dict):
        return None
    configured = settings.get("db_path") or config.get("dashboard_db_path")
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(str(configured))))
    if settings.get("enabled"):
        return Path(state_path).parent / "dashboard" / "dashboard.sqlite3"
    return None


def open_store(
    config: Dict[str, Any],
    state_path: Path,
    logger: Optional[Logger] = None,
) -> Optional[Any]:
    """Open the dashboard store, or return ``None`` if disabled/unavailable.

    A broken dashboard must degrade observability, never stop reviews, so the
    failure is logged loudly and the executor continues without recording.
    """
    log = _logger_or_default(logger)
    path = dashboard_db_path(config, state_path)
    if path is None:
        return None
    try:
        from .dashboard_store import DashboardStore

        return DashboardStore(path)
    except Exception as exc:  # noqa: BLE001 - dashboard is never load-bearing
        log("WARNING dashboard store unavailable at %s: %r; running without dashboard records" % (path, exc))
        return None


class DashboardUnavailable(RuntimeError):
    """A durable dashboard write that the caller depends on could not be made."""


class RetryDeferred(RuntimeError):
    """Raised by a review runner that did not start the review (executor paused
    or stopping, or the PR changed while it waited). The claimed request goes
    back to the queue and the drain stops claiming."""


class RetryRefused(RuntimeError):
    """Raised by a review runner that refused the request before starting the
    review (a rereview's late check). The request ends with ``status`` and the
    reason as its error, which the Retrigger page shows, instead of the
    generic "was not posted"."""

    def __init__(self, reason: str, status: str = "failed") -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class DashboardRecorder:
    """Records one review attempt's lifecycle. Safe to use with ``store=None``.

    Two failure policies, because the two triggers have different contracts:

    - ``strict=False`` (poll): observability is best effort. A store failure is
      logged, ``degraded`` is set, and the review continues -- an unobserved
      review is better than a skipped one.
    - ``strict=True`` (operator retry): the request is already visible in the
      dashboard as claimed, so an untracked execution would be a lie and would
      also lose the crash/duplicate-POST guard. Writes the caller depends on
      (the attempt row, and the pre-POST posting intent) raise
      ``DashboardUnavailable`` instead, and the caller refuses to run/post.

    Once a terminal status is recorded, later updates are dropped rather than
    fighting the store's terminal-transition guard.
    """

    def __init__(
        self,
        store: Optional[Any],
        trigger: str = "poll",
        retry_of: Optional[str] = None,
        request_id: Optional[str] = None,
        strict: bool = False,
        logger: Optional[Logger] = None,
        seen_at: Optional[str] = None,
    ) -> None:
        self._store = store
        self._trigger = trigger
        self._retry_of = retry_of
        self._request_id = request_id
        self._seen_at = seen_at
        self._strict = bool(strict)
        self._log = _logger_or_default(logger)
        self.run_id = None  # type: Optional[str]
        self.terminal_status = None  # type: Optional[str]
        self.degraded = False

    @property
    def strict(self) -> bool:
        return self._strict

    @property
    def retry_of(self) -> Optional[str]:
        """The attempt this request was made against (the one the operator saw), or None."""
        return self._retry_of

    @property
    def seen_at(self) -> Optional[str]:
        """When the page read the snapshot this request was made against, or None."""
        return self._seen_at

    def begin(
        self,
        repo: str,
        pr: int,
        head_sha: str,
        title: str = "",
        url: str = "",
    ) -> Optional[str]:
        if self.run_id is not None:
            return self.run_id
        if self._store is None:
            if self._strict:
                raise DashboardUnavailable(
                    "no dashboard store: refusing to run an untracked retry for %s pr #%s" % (repo, pr)
                )
            return None
        kwargs = {
            "title": _clip(title or "", 300),
            "url": str(url or ""),
            "trigger": self._trigger,
            "retry_of": self._retry_of,
        }  # type: Dict[str, Any]
        if self._request_id:
            # Linking the claimed request to the attempt in the same
            # transaction is what makes a request traceable to real work.
            kwargs["request_id"] = self._request_id
        try:
            run = self._store.start_run(repo, int(pr), str(head_sha), **kwargs)
            self.run_id = str(run["id"])
        except Exception as exc:  # noqa: BLE001
            self.degraded = True
            self._log("WARNING dashboard degraded: start_run failed for %s pr #%s: %r" % (repo, pr, exc))
            self.run_id = None
            if self._strict:
                raise DashboardUnavailable(
                    "could not record a durable attempt for %s pr #%s: %r" % (repo, pr, exc)
                )
        return self.run_id

    def stage(self, stage: str, required: bool = False, **fields: Any) -> None:
        self._update("running", required=required, stage=stage, **fields)

    def agents(self, entries: Any, stage: Optional[str] = None, **fields: Any) -> None:
        fields = dict(fields)
        fields["agents"] = normalize_agent_rows(entries)
        if stage is not None:
            fields["stage"] = stage
        self._update("running", **fields)

    def setting(self, key: str) -> Optional[Dict[str, Any]]:
        """A dashboard-set operator setting's value, or None (never raises).

        Best effort in both policies: a missing or unreadable setting means
        "use the config", which is exactly the daemon's pre-dashboard behaviour.
        """
        if self._store is None:
            return None
        try:
            row = self._store.get_setting(key)
        except Exception as exc:  # noqa: BLE001 - the setting is advisory
            self._log("WARNING dashboard setting %s unreadable, using config: %r" % (key, exc))
            return None
        if not isinstance(row, dict):
            return None
        value = row.get("value")
        return value if isinstance(value, dict) else None

    def reconcile_agent_setting(
        self, agent_id: str, read_default: Callable[[], Optional[str]], reconcile: Callable[..., Any]
    ) -> Optional[Tuple[Dict[str, Any], Optional[str]]]:
        """Apply the agent's current prompt file to its stored override (store.reconcile_agent_setting).

        ``(the value now stored, the action taken or None)``, with ``{}`` once the
        document is deleted; or None when the store could not do it, which the
        caller treats as "no replacement this time" and runs the stored prompt
        (never raises in either policy: the stored prompt is advisory).
        """
        if self._store is None:
            return None
        try:
            row, action = self._store.reconcile_agent_setting(agent_id, read_default, reconcile)
        except Exception as exc:  # noqa: BLE001 - the setting is advisory
            self._log("WARNING dashboard setting agent:%s not reconciled with its prompt file, keeping it: %r" % (agent_id, exc))
            return None
        value = row.get("value") if isinstance(row, dict) else None
        return (value if isinstance(value, dict) else {}, action)

    def pr_author(self, login: str) -> Optional[Dict[str, Any]]:
        """A PR author's own settings (mentions.py): ``{}`` when there are none, None when unknown.

        Never raises. Unknown is no store, or a store that failed (logged): the
        review then runs in the default language, and no welcome comment is
        posted because it could not be recorded.
        """
        if self._store is None:
            return None
        try:
            row = self._store.get_pr_author(login)
        except Exception as exc:  # noqa: BLE001 - the author's settings are advisory
            self._log("WARNING settings of PR author %s unreadable, reviewing without them: %r" % (login, exc))
            return None
        return dict(row) if isinstance(row, dict) else {}

    def mark_welcomed(self, login: str, pr_ref: str) -> bool:
        """Record that ``login`` got the welcome comment (on ``owner/name#N``); False if it could not be (logged)."""
        if self._store is None:
            return False
        try:
            self._store.mark_author_welcomed(login, pr_ref)
        except Exception as exc:  # noqa: BLE001 - never raised into the review
            self._log("WARNING welcome comment for %s posted on %s but not recorded: %r" % (login, pr_ref, exc))
            return False
        return True

    def succeeded(self, review_url: str = "", **fields: Any) -> None:
        """Only a confirmed GitHub review POST may call this."""
        if review_url:
            fields["review_url"] = _clip(review_url, 500)
        self._update("succeeded", **fields)

    def failed(self, error: Any, **fields: Any) -> None:
        self._update("failed", error=_clip(error), **fields)

    def _update(self, status: str, required: bool = False, **fields: Any) -> None:
        if self._store is None or self.run_id is None:
            if required and self._strict:
                raise DashboardUnavailable(
                    "no durable attempt to record %s on; refusing to continue" % status
                )
            return
        if self.terminal_status is not None:
            # Terminal attempts are immutable history; do not rewrite them.
            return
        terminal = status in TERMINAL_RUN_STATUSES
        fields = _fit_agents_to_event(status, fields)
        # A progress update may be dropped (the next one supersedes it); the
        # terminal outcome is the record itself, so it is retried and, if the
        # store stays down, remembered for replay instead of being lost.
        attempts = FINISH_RETRY_ATTEMPTS if terminal else 1
        last_exc = None  # type: Optional[BaseException]
        for _ in range(attempts):
            try:
                self._store.update_run(self.run_id, status, **fields)
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if terminal and _run_is_terminal(self._store, self.run_id):
                    # The write landed before the error surfaced. Retrying
                    # would re-emit the terminal event (the store only rejects
                    # a *different* terminal status), so stop here.
                    last_exc = None
                    break
        if last_exc is not None:
            self.degraded = True
            self._log(
                "WARNING dashboard degraded: update_run(%s, %s) failed: %r" % (self.run_id, status, last_exc)
            )
            if terminal:
                remember_run_outcome(self.run_id, status, fields, self._log)
            if required and self._strict:
                raise DashboardUnavailable(
                    "could not record %s for attempt %s: %r" % (status, self.run_id, last_exc)
                )
            return
        if terminal:
            self.terminal_status = status


def _agent_llm_fields(value: Dict[str, Any]) -> Dict[str, Any]:
    """Bounded model/usage scalars; absent or malformed fields are simply absent."""
    fields = {}  # type: Dict[str, Any]
    for name, limit in AGENT_TEXT_FIELDS:
        raw = value.get(name)
        if isinstance(raw, str) and raw.strip():
            fields[name] = _clip(raw, limit)
    for name in AGENT_INT_FIELDS:
        raw = value.get(name)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            fields[name] = raw
    for name in AGENT_FLOAT_FIELDS:
        raw = value.get(name)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0 and raw == raw:
            fields[name] = round(float(raw), 3)
    return fields


def normalize_agent_rows(entries: Any, budget: int = AGENT_ROWS_JSON_BUDGET) -> List[Dict[str, Any]]:
    """Project progress entries onto bounded ``{id,status,summary,+llm,+failure detail}`` rows.

    Idempotent on its own output, so the recorder can refit rows to a tighter
    ``budget`` and the web tier can re-project stored rows without shrinking them.
    """
    rows = []  # type: List[Dict[str, Any]]
    if isinstance(entries, dict):
        items = sorted(entries.items(), key=lambda item: str(item[0]))
        pairs = [(str(key), value) for key, value in items]
    elif isinstance(entries, (list, tuple)):
        pairs = []
        for value in entries:
            if isinstance(value, dict):
                pairs.append((str(value.get("id", "")), value))
    else:
        return rows
    sources = []  # type: List[Dict[str, Any]]
    for agent_id, value in pairs[:MAX_AGENT_ROWS]:
        if not isinstance(value, dict):
            continue
        row = {
            "id": agent_id,
            "status": _clip(value.get("status", "") or "", 32),
            "summary": _clip(value.get("summary", "") or "", MAX_AGENT_SUMMARY_CHARS),
        }  # type: Dict[str, Any]
        row.update(_agent_llm_fields(value))
        if value.get("reused") is True:
            # Not run in this attempt: its result came from the attempt retried.
            row["reused"] = True
        rows.append(row)
        sources.append(value)
    return _with_failure_detail(rows, sources, budget)


def _json_chars(value: Any) -> int:
    """The length the store measures (``DashboardStore._bounded_json``)."""
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _attempt_hops(value: Any) -> Tuple[List[Dict[str, Any]], int]:
    """Well-formed hops, newest ``MAX_AGENT_ATTEMPT_LOG`` kept, and how many were cut."""
    hops = []  # type: List[Dict[str, Any]]
    if not isinstance(value, list):
        return hops, 0
    for hop in value:
        if not isinstance(hop, dict):
            continue
        entry = {}  # type: Dict[str, Any]
        for name, limit in ATTEMPT_LOG_TEXT_FIELDS:
            raw = hop.get(name)
            if isinstance(raw, str) and raw.strip():
                entry[name] = _clip(raw, limit)
        for name in ATTEMPT_LOG_INT_FIELDS:
            raw = hop.get(name)
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                entry[name] = raw
        for name in ATTEMPT_LOG_FLOAT_FIELDS:
            raw = hop.get(name)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool) and 0 <= raw < float("inf"):
                entry[name] = round(float(raw), 3)
        if not all(name in entry for name in ("model", "attempt", "outcome")):
            continue
        raw = hop.get("error")
        if isinstance(raw, str) and raw.strip():
            entry["error"] = raw
        hops.append(entry)
    cut = max(0, len(hops) - MAX_AGENT_ATTEMPT_LOG)
    return hops[-MAX_AGENT_ATTEMPT_LOG:], cut


def _shape_hops(
    hops: List[Dict[str, Any]],
    keep_succeeded: bool,
    text_limit: int,
    max_failed: Optional[int],
    fields: Optional[Tuple[str, ...]],
) -> Tuple[List[Dict[str, Any]], int]:
    """One ladder step applied to an agent's hops: the hops shown and the failed hops left out."""
    failed = [hop for hop in hops if not hop["outcome"].startswith("succeeded")]
    if max_failed is None:
        chosen = hops if keep_succeeded else failed
    else:
        chosen = failed[-max_failed:]
    shaped = []  # type: List[Dict[str, Any]]
    for hop in chosen:
        entry = {name: value for name, value in hop.items() if name != "error" and (fields is None or name in fields)}
        if text_limit > 0 and "error" in hop:
            entry["error"] = _clip_block(hop["error"], text_limit)
        shaped.append(entry)
    return shaped, (len(failed) - len(chosen) if max_failed is not None else 0)


#: Fields a row keeps to the end: who, what state (reused or run here), and its newest failed hop.
ROW_CORE_FIELDS = ("id", "status", "reused", "attempt_log", "attempt_log_omitted")
BASE_DEGRADE_LEVELS = 5


def _degrade_base(item: Dict[str, Any], level: int) -> Dict[str, Any]:
    """The row's base metadata given way ``level`` steps deep, in a fixed order.

    The route repeats what the attempt log says, then the summary is trimmed,
    then a response model equal to the model goes, then everything but the
    summary and model, then those too. The core fields always stay.
    """
    row = dict(item)
    if level >= 1 and "route" in row:
        row["route"] = _clip(row["route"], 80)
    if level >= 2:
        row["summary"] = _clip(row.get("summary", ""), 80)
    if level >= 3 and row.get("response_model") == row.get("model"):
        row.pop("response_model", None)
    if level >= 4:
        row = {name: value for name, value in row.items() if name in ROW_CORE_FIELDS + ("summary", "model")}
        row["summary"] = _clip(row.get("summary", ""), 40)
    if level >= 5:
        row = {name: value for name, value in row.items() if name in ROW_CORE_FIELDS}
    return row


def _with_failure_detail(rows: List[Dict[str, Any]], sources: List[Dict[str, Any]], budget: int) -> List[Dict[str, Any]]:
    """Attach each row's attempt log and failure text at the largest size that fits ``budget``.

    Shrinking beats losing the table: past the limit the store would replace
    every row, including the failed ones this detail explains. When even the
    newest failed hop per agent does not fit, the base metadata gives way
    (``_degrade_base``); the last answer is bare ``{id, status}`` rows.
    """
    prepared = []  # type: List[Tuple[List[Dict[str, Any]], int, Optional[str]]]
    for source in sources:
        hops, cut = _attempt_hops(source.get("attempt_log"))
        carried = source.get("attempt_log_omitted")
        carried = carried if isinstance(carried, int) and not isinstance(carried, bool) and carried > 0 else 0
        error = source.get("error")
        prepared.append((hops, carried + cut, error if isinstance(error, str) and error.strip() else None))
    has_detail = any(hops or omitted or error for hops, omitted, error in prepared)
    if not has_detail and _json_chars(rows) <= budget:
        return rows
    minimal = rows
    for keep_succeeded, limit, max_failed, fields in FAILURE_DETAIL_STEPS if has_detail else ():
        trial = []  # type: List[Dict[str, Any]]
        for row, (hops, omitted, error) in zip(rows, prepared):
            item = dict(row)
            log, left_out = _shape_hops(hops, keep_succeeded, limit, max_failed, fields)
            if log:
                item["attempt_log"] = log
            if omitted + left_out:
                item["attempt_log_omitted"] = omitted + left_out
            if limit > 0 and error is not None:
                # The tail is where a runner's stderr says why it gave up.
                item["error"] = _clip_block(error, limit, keep_tail=True)
            trial.append(item)
        if _json_chars(trial) <= budget:
            return trial
        minimal = trial
    for level in range(1, BASE_DEGRADE_LEVELS + 1):
        degraded = [_degrade_base(item, level) for item in minimal]
        if _json_chars(degraded) <= budget:
            return degraded
    return [{"id": row["id"], "status": row["status"]} for row in rows]


def _fit_agents_to_event(status: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Fit the agent rows to what the run_updated event leaves after its other fields.

    The store writes the rows to their column and, next to every other field,
    into the event payload; past ``max_json_chars`` either becomes a truncation
    marker, and a truncated event no longer carries the status the live page
    patches in. Measured here, per write, instead of a fixed headroom.
    """
    agents = fields.get("agents")
    if not isinstance(agents, list):
        return fields
    # The payload the store will emit (kept agent results go as ids only).
    others = run_event_payload(status, dict(fields, agents=[]))
    budget = min(AGENT_ROWS_JSON_BUDGET, DashboardStore.max_json_chars - (_json_chars(others) - len("[]")))
    return dict(fields, agents=normalize_agent_rows(agents, budget=budget))


class RetryDecision(object):
    """Result of revalidating a claimed retry request before execution."""

    __slots__ = ("allow", "force", "status", "reason")

    def __init__(self, allow: bool, force: bool = False, status: str = "", reason: str = "") -> None:
        self.allow = allow
        self.force = force
        self.status = status
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "RetryDecision(allow=%r, force=%r, status=%r, reason=%r)" % (
            self.allow,
            self.force,
            self.status,
            self.reason,
        )


def _ledger_is_baseline(entry: Dict[str, Any]) -> bool:
    return bool(entry.get("initial_baseline_without_review"))


def _ledger_post_unverified(entry: Dict[str, Any]) -> bool:
    return bool(entry.get("post_outcome_unknown"))


def posting_outcome_unknown(run: Optional[Dict[str, Any]]) -> bool:
    """True when an attempt stopped where the review POST may have landed."""
    if not run:
        return False
    if str(run.get("status") or "") == "succeeded":
        return False
    return str(run.get("stage") or "") in POSTING_STAGES or bool(run.get("review_url"))


def retry_decision(
    request: Dict[str, Any],
    previous_run: Optional[Dict[str, Any]],
    ledger_entry: Optional[Dict[str, Any]],
) -> RetryDecision:
    """Decide whether a claimed retry request may execute, and with what force.

    Pure function so the duplicate-POST rules are testable without GitHub:

    - a previous attempt that succeeded, or whose posting outcome is unknown
      (stopped at/after ``posting``, or already carrying a review url), is never
      re-posted -- not by the poll loop and not by an explicit operator retry --
      until it has been reconciled against GitHub;
    - a head that the JSON ledger records as actually reviewed, or as an
      unverified POST, is never re-reviewed, even if a request asks for it;
    - ``force`` (bypassing the ledger's reviewed-head short circuit) is granted
      only when the previous outcome for this exact head was a failure and the
      ledger entry, if any, is a baseline marker (never reviewed).

    A request of kind ``rereview`` (the Retrigger badge) is the one exception
    to the duplicate rules: it asks for a second review of a head on purpose,
    so a succeeded previous attempt and a ledger-reviewed head are allowed,
    with ``force``. It is still refused when the head moved, when the previous
    attempt is still running or its posting outcome is unknown, and when the
    ledger holds an unverified POST for the head: those are not "already
    reviewed", they are "nobody knows", and a second POST could be a third.
    """
    head_sha = str(request.get("head_sha") or "")
    if request_kind(request) == RETRY_KIND_REREVIEW:
        return _rereview_decision(head_sha, previous_run, ledger_entry)
    if previous_run is not None:
        previous_status = str(previous_run.get("status") or "")
        previous_head = str(previous_run.get("head_sha") or "")
        if previous_status == "succeeded":
            return RetryDecision(
                False,
                status="failed",
                reason="previous attempt succeeded; refusing to re-post a review",
            )
        if previous_status not in FAILED_RUN_STATUSES:
            return RetryDecision(
                False,
                status="failed",
                reason="previous attempt is %s, not a failure; refusing retry" % (previous_status or "unknown"),
            )
        if previous_head and head_sha and previous_head != head_sha:
            return RetryDecision(
                False,
                status="stale",
                reason="head moved since the failed attempt (%s -> %s)" % (previous_head[:12], head_sha[:12]),
            )
        if posting_outcome_unknown(previous_run):
            return RetryDecision(
                False,
                status="failed",
                reason=(
                    "previous attempt stopped at stage %s: the GitHub review POST outcome is unknown. "
                    "Reconcile against GitHub before any retry."
                    % (str(previous_run.get("stage") or "posting"))
                ),
            )
        previous_failed = True
    else:
        previous_failed = False

    if ledger_entry and str(ledger_entry.get("head_sha") or "") == head_sha:
        if _ledger_post_unverified(ledger_entry):
            return RetryDecision(
                False,
                status="failed",
                reason=(
                    "head %s has an unverified review POST (outcome unknown); "
                    "reconcile against GitHub before any retry" % head_sha[:12]
                ),
            )
        if not _ledger_is_baseline(ledger_entry):
            return RetryDecision(
                False,
                status="failed",
                reason="head %s is already recorded as reviewed; refusing duplicate review" % head_sha[:12],
            )
        if not previous_failed:
            return RetryDecision(
                False,
                status="failed",
                reason="no failed attempt recorded for head %s; refusing forced review" % head_sha[:12],
            )
        # Baseline entries were recorded without any review being posted, so
        # the ledger short circuit must be bypassed for this attempt only.
        return RetryDecision(True, force=True)

    return RetryDecision(True, force=False)


def request_kind(request: Dict[str, Any]) -> str:
    """``options["kind"]`` of a retry request, or "" for a plain retry."""
    options = request.get("options")
    kind = options.get(RETRY_KIND_OPTION) if isinstance(options, dict) else None
    return str(kind or "")


def _rereview_decision(
    head_sha: str,
    previous_run: Optional[Dict[str, Any]],
    ledger_entry: Optional[Dict[str, Any]],
) -> RetryDecision:
    """The ``rereview`` rules of ``retry_decision``: allowed on a reviewed head, never on an unknown one."""
    if previous_run is not None:
        previous_status = str(previous_run.get("status") or "")
        previous_head = str(previous_run.get("head_sha") or "")
        if previous_head and head_sha and previous_head != head_sha:
            return RetryDecision(
                False,
                status="stale",
                reason="head moved since the previous attempt (%s -> %s)" % (previous_head[:12], head_sha[:12]),
            )
        if previous_status == "running":
            return RetryDecision(
                False,
                status="failed",
                reason="previous attempt is still running; refusing a second review beside it",
            )
        if posting_outcome_unknown(previous_run):
            return RetryDecision(
                False,
                status="failed",
                reason=(
                    "previous attempt stopped at stage %s: the GitHub review POST outcome is unknown. "
                    "Reconcile against GitHub before any rereview."
                    % (str(previous_run.get("stage") or "posting"))
                ),
            )
    if ledger_entry and str(ledger_entry.get("head_sha") or "") == head_sha and _ledger_post_unverified(ledger_entry):
        return RetryDecision(
            False,
            status="failed",
            reason=(
                "head %s has an unverified review POST (outcome unknown); "
                "reconcile against GitHub before any rereview" % head_sha[:12]
            ),
        )
    # A reviewed head is the point: force past the ledger's short circuit.
    return RetryDecision(True, force=True)


def review_finished_at_or_after(
    finished_at: Optional[str], seen_at: str, *, whole_seconds: bool,
) -> bool:
    """Compare completion against a request, conservatively at stored precision."""
    # Python 3.9's fromisoformat does not support GitHub's Z suffix.
    seen = dt.datetime.fromisoformat(str(seen_at).replace("Z", "+00:00"))
    if not finished_at:
        return False
    finished = dt.datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
    return finished >= (seen.replace(microsecond=0) if whole_seconds else seen)


def ledger_reviewed_after(entry: Optional[Dict[str, Any]], head_sha: str, seen_at: Optional[str]) -> bool:
    """True when the ledger records a review of ``head_sha`` posted after
    ``seen_at``. Raises ValueError/TypeError when the times cannot be compared.

    ``reviewed_at_exact`` orders the review to the microsecond. An entry with only
    the whole-second ``reviewed_at`` cannot be ordered within the snapshot's
    second, so a review in that second counts as after. Equality is ambiguous
    at either precision and refuses."""
    if not seen_at or not isinstance(entry, dict) or str(entry.get("head_sha") or "") != head_sha:
        return False
    if entry.get("initial_baseline_without_review"):
        return False
    exact = entry.get("reviewed_at_exact")
    return review_finished_at_or_after(
        exact or entry.get("reviewed_at"), seen_at, whole_seconds=not bool(exact),
    )


def _request_seen_at(request: Dict[str, Any]) -> Optional[str]:
    options = request.get("options")
    seen_at = options.get(RETRY_SEEN_AT_OPTION) if isinstance(options, dict) else None
    return str(seen_at or request.get("created_at") or "") or None


def rereview_late_check(
    store: Any,
    repo: str,
    number: int,
    head_sha: str,
    seen_run_id: Optional[str],
    ledger_lookup: Callable[[str, int], Optional[Dict[str, Any]]],
    seen_at: Optional[str] = None,
) -> None:
    """The rereview's last check, run by the runner once it owns the PR, just
    before ``review_pr``; raises ``RetryRefused`` or returns None.

    ``retry_decision`` allowed the request at claim time, against the attempt
    the operator saw (``seen_run_id``). The request may then have waited (the
    scheduler's PR slot) or another review of the head may have run first
    (the ``once`` poll between its drains), so the same rules are re-derived
    from the newest attempt and the ledger, plus one more: an attempt newer
    than the one the operator saw that already succeeded is the fresh review
    the click asked for, and a second one would run every agent twice. So is a
    review the ledger records after the page read its snapshot (``seen_at``):
    the poll's recorder is best effort, so a posted review can lack its attempt
    row. An unreadable check refuses; it never falls back to allow.
    """
    lookup = getattr(store, "latest_attempt_for_head", None) if store is not None else None
    try:
        latest = lookup(repo, number, head_sha) if callable(lookup) else None
        ledger_entry = ledger_lookup(repo, number)
        reviewed_after = ledger_reviewed_after(ledger_entry, head_sha, seen_at)
    except Exception as exc:  # noqa: BLE001 - an unreadable late check must refuse, never fall back to allow
        # The upstream text goes to the log (via __cause__), not the request row.
        raise RetryRefused("rereview not run: the late check could not read the latest attempt or the ledger") from exc
    decision = _rereview_decision(head_sha, latest, ledger_entry)
    if not decision.allow:
        raise RetryRefused("rereview not run: %s" % decision.reason, decision.status or "failed")
    if latest is not None and latest.get("status") == "succeeded" and str(latest.get("id") or "") != str(seen_run_id or ""):
        raise RetryRefused(
            "rereview not run: %s was reviewed meanwhile (attempt %s)" % (head_sha[:12], latest.get("id")), "stale"
        )
    if reviewed_after:
        raise RetryRefused(
            "rereview not run: %s was reviewed meanwhile (ledger, %s)"
            % (head_sha[:12], ledger_entry.get("reviewed_at_exact") or ledger_entry.get("reviewed_at")),
            "stale",
        )


def reusable_results(
    store: Any,
    request: Dict[str, Any],
    previous_run: Optional[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], str]:
    """What a retry may reuse from the attempt it retries: ``(reuse, why_not)``.

    ``reuse`` is ``{"run_id", "head_sha", "results": {agent id: result}}`` for
    ``service.review_pr(reuse_results=...)``, or None with the reason every agent
    runs. Only a ``"failed"``-scope request reuses anything (a request queued
    before scopes existed has none and runs every agent), and only the results
    the earlier attempt kept for this very head (review_pr also checks the base and diff
    each result was reviewed against and the review language it is written in: the
    reuse identity is the head, base, diff and language). Never raises: an unreadable
    store (or one without kept results) means "run every agent".
    """
    options = request.get("options")
    scope = options.get(RETRY_SCOPE_OPTION) if isinstance(options, dict) else None
    if scope != RETRY_SCOPE_FAILED:
        return None, "the request asks for every agent"
    if previous_run is None:
        return None, "there is no earlier attempt to reuse results from"
    head_sha = str(request.get("head_sha") or "")
    previous_head = str(previous_run.get("head_sha") or "")
    if not head_sha or previous_head != head_sha:
        return None, "the earlier attempt reviewed %s, not %s" % (previous_head[:12] or "unknown", head_sha[:12])
    try:
        results = store.get_agent_results(str(previous_run.get("id")))
    except Exception as exc:  # noqa: BLE001 - reuse is an optimisation, never a reason to fail
        return None, "reading the earlier attempt's agent results failed: %r" % (exc,)
    if not isinstance(results, dict) or not results:
        return None, "the earlier attempt kept no agent results"
    return {"run_id": str(previous_run.get("id")), "head_sha": head_sha, "results": dict(results)}, ""


def _pr_validation_failure(
    request: Dict[str, Any],
    pr_payload: Any,
) -> Optional[Tuple[str, str]]:
    """Return ``(status, reason)`` when the live PR no longer matches the request."""
    head_sha = str(request.get("head_sha") or "")
    if not isinstance(pr_payload, dict) or not pr_payload:
        return ("failed", "GitHub returned no pull request payload")
    pr_state = str(pr_payload.get("state") or "")
    if pr_state != "open":
        return ("stale", "pull request is %s, not open" % (pr_state or "unknown"))
    if pr_payload.get("draft"):
        # Drafts are not an error, but a review request pinned to one is stale.
        return ("stale", "pull request is a draft")
    live_head = str((pr_payload.get("head") or {}).get("sha") or "")
    if not live_head:
        return ("failed", "pull request payload has no head sha")
    if live_head != head_sha:
        return ("stale", "head moved to %s (requested %s)" % (live_head[:12], head_sha[:12]))
    return None


def drain_retry_requests(
    store: Optional[Any],
    lock: ExecutorLock,
    configured_repos: Any,
    client_factory: Callable[[str], Any],
    review_runner: Callable[..., bool],
    ledger_lookup: Callable[[str, int], Optional[Dict[str, Any]]],
    logger: Optional[Logger] = None,
    max_requests: int = MAX_RETRIES_PER_CYCLE,
    pause_path: Optional[Path] = None,
) -> int:
    """Consume queued retry requests serially. Returns the number consumed.

    Requires the executor lock: the web tier has no lock and therefore cannot
    reach this code path even by importing it. Each request is isolated, so one
    exploding request neither aborts the drain nor leaks a ``running`` row.
    A graceful-stop sentinel stops further claims between requests.
    """
    if store is None:
        return 0
    lock.require_held("consuming retry requests")
    log = _logger_or_default(logger)
    repos = set(str(repo) for repo in (configured_repos or []))
    # A previous cycle may have finished an attempt durably but failed to record
    # the request's outcome. Close those from their attempt rows before claiming
    # new work, so a stuck "running" request converges instead of waiting for a
    # restart (where recovery would fail it and lose a real success).
    reconcile_running_requests(store, lock, logger=log)
    consumed = 0
    unfinalized = False
    while consumed < max_requests:
        if stop_requested(pause_path):
            log("retry queue: graceful stop requested (%s); claiming no further requests" % pause_path)
            break
        try:
            request = store.claim_retry()
        except Exception as exc:  # noqa: BLE001 - never let the queue kill the cycle
            log("WARNING retry queue claim failed: %r" % (exc,))
            break
        if not request:
            break
        consumed += 1
        try:
            if not _execute_retry_request(
                store,
                request,
                repos,
                client_factory,
                review_runner,
                ledger_lookup,
                log,
            ):
                unfinalized = True
        except RetryDeferred:
            # Back in the queue, remembered to be, or refused (logged); claiming
            # more now would defer them too.
            if _PENDING_REQUEUES:
                unfinalized = True
            break
        except Exception as exc:  # noqa: BLE001 - isolate per-request failures
            log("WARNING retry request %s failed unexpectedly: %r" % (request.get("id"), exc))
            if not _finish_quietly(
                store, request, "failed", log, error="retry request failed: %r" % (exc,)
            ):
                unfinalized = True
    if consumed >= max_requests:
        log("retry queue: consumed %d requests this cycle; remaining requests wait for the next one" % consumed)
    if unfinalized:
        # Same-cycle second chance: the store may already be writable again.
        reconcile_running_requests(store, lock, logger=log)
    return consumed


#: ``_requeue_quietly`` outcomes: queued again; remembered and retried before
#: the next claim; or not returned at all (nothing will requeue it later).
REQUEUED = "requeued"
REQUEUE_PENDING = "pending"
NOT_REQUEUED = "not-requeued"


def _requeue_quietly(
    store: Any, request: Dict[str, Any], log: Logger, attempts: int = FINISH_RETRY_ATTEMPTS
) -> Tuple[str, str]:
    """Return a claimed, unrun request to ``queued``. Returns ``(outcome, reason)``.

    ``REQUEUED`` once it is queued (again). A requeue the store cannot take now
    is remembered (``_PENDING_REQUEUES``) and retried before every later claim:
    ``REQUEUE_PENDING``; if the process dies first, restart recovery closes the
    still-claimed request as interrupted. ``NOT_REQUEUED``, with the reason, for
    a request nothing returns later: the store refuses it for good (unknown, or
    an attempt is linked), it is no longer ``running``, the store has no
    requeue, or too many requeues are already remembered.
    """
    request_id = str(request.get("id"))
    requeue = getattr(store, "requeue_retry", None)
    if requeue is None:
        log("WARNING store cannot requeue retry request %s; restart recovery closes it" % request_id)
        return NOT_REQUEUED, "the store cannot requeue"
    for attempt in range(1, max(1, int(attempts)) + 1):
        try:
            changed = requeue(request_id)
        except ValueError as exc:
            _PENDING_REQUEUES.pop(request_id, None)
            log("WARNING retry request %s cannot be returned to the queue: %s" % (request_id, exc))
            return NOT_REQUEUED, str(exc)
        except Exception as exc:  # noqa: BLE001
            if _request_status(store, request_id, log) == "queued":
                _PENDING_REQUEUES.pop(request_id, None)
                return REQUEUED, ""
            log(
                "WARNING could not return retry request %s to the queue (attempt %d/%d): %r"
                % (request_id, attempt, attempts, exc)
            )
            continue
        _PENDING_REQUEUES.pop(request_id, None)
        if changed:
            return REQUEUED, ""
        # No longer running: already queued, or closed meanwhile.
        status = _request_status(store, request_id, log)
        if status == "queued":
            return REQUEUED, ""
        state = "already %s" % status if status and status != "running" else "no longer running"
        return NOT_REQUEUED, state
    if request_id in _PENDING_REQUEUES or len(_PENDING_REQUEUES) < MAX_PENDING_FINALIZATIONS:
        _PENDING_REQUEUES[request_id] = dict(request)
        log(
            "WARNING retry request %s stays claimed for now; it is returned to the queue before the next "
            "claim (restart recovery closes it if the daemon stops first)" % request_id
        )
        return REQUEUE_PENDING, ""
    log(
        "WARNING %d requeues are already waiting for the store; retry request %s stays claimed "
        "until restart recovery closes it" % (len(_PENDING_REQUEUES), request_id)
    )
    return NOT_REQUEUED, "%d requeues are already waiting for the store" % len(_PENDING_REQUEUES)


#: Claimed requests that were not run and must go back to ``queued``, whose
#: requeue write failed; keyed by request id. Same process-local lifetime and
#: bound as the outcomes below.
_PENDING_REQUEUES = {}  # type: Dict[str, Dict[str, Any]]


def pending_requeues() -> Dict[str, Dict[str, Any]]:
    return dict(_PENDING_REQUEUES)


def replay_pending_requeues(store: Any, log: Logger) -> int:
    """Retry requeues that failed earlier. Returns the number now back in the queue.

    Runs before any claim: without it a deferred request whose requeue failed
    stays ``running`` with no attempt, which reconciliation skips and
    ``enqueue_retry`` dedups onto, until a restart.
    """
    requeued = 0
    for request_id, request in list(_PENDING_REQUEUES.items()):
        if _requeue_quietly(store, request, log, attempts=1)[0] == REQUEUED:
            requeued += 1
            log("retry request %s returned to the queue (its earlier requeue had failed)" % request_id)
    return requeued


def _request_status(store: Any, request_id: str, log: Logger) -> Optional[str]:
    """Read a request's status, or ``None`` when the store cannot answer."""
    try:
        return store.retry_status(str(request_id))
    except Exception as exc:  # noqa: BLE001 - an unreadable store stays unknown
        log("WARNING could not read the status of retry request %s: %r" % (request_id, exc))
        return None


#: Terminal outcomes whose write failed, keyed by request id, so a later cycle
#: can replay them. Process-local on purpose: the executor lock makes this
#: process the only writer, and a restart hands the same rows to
#: ``recover_interrupted`` (which fails unlinked claimed requests).
_PENDING_FINALIZATIONS = {}  # type: Dict[str, Tuple[Dict[str, Any], str, Optional[str], Optional[str]]]
#: Admission bound. The drain claims at most MAX_RETRIES_PER_CYCLE requests per
#: cycle, so a store that stays broken for many cycles is the only way to reach
#: it; beyond it a lost outcome is logged and left to restart recovery (the
#: pre-existing behaviour), never silently evicted from the remembered set.
MAX_PENDING_FINALIZATIONS = 200


#: Terminal attempt outcomes whose update_run failed, keyed by run id; same
#: lifetime and bound as the request outcomes above.
_PENDING_RUN_OUTCOMES = {}  # type: Dict[str, Tuple[str, Dict[str, Any]]]


def pending_run_outcomes() -> Dict[str, Tuple[str, Dict[str, Any]]]:
    return dict(_PENDING_RUN_OUTCOMES)


def _run_is_terminal(store: Any, run_id: str) -> bool:
    """Whether the attempt row is already terminal; ``False`` if unreadable."""
    try:
        run = store.get_run(str(run_id))
    except Exception:  # noqa: BLE001 - an unreadable store is not proof either way
        return False
    return bool(run) and str(run.get("status") or "") in TERMINAL_RUN_STATUSES


def remember_run_outcome(run_id: str, status: str, fields: Dict[str, Any], log: Logger) -> None:
    if run_id in _PENDING_RUN_OUTCOMES or len(_PENDING_RUN_OUTCOMES) < MAX_PENDING_FINALIZATIONS:
        _PENDING_RUN_OUTCOMES[run_id] = (status, dict(fields))
        return
    log(
        "WARNING %d attempt outcomes are already waiting for a durable write; not remembering %s "
        "(it is closed as interrupted by recovery on restart)" % (len(_PENDING_RUN_OUTCOMES), run_id)
    )


def replay_pending_run_outcomes(store: Any, log: Logger) -> int:
    """Retry terminal attempt writes that failed earlier. Returns the number landed.

    Runs before request reconciliation on purpose: a request's outcome is read
    from its attempt row, so the attempt must land first. A row that became
    terminal by other means (restart recovery) rejects the write with
    ValueError and the remembered outcome is dropped, never forced.
    """
    landed = 0
    for run_id, (status, fields) in list(_PENDING_RUN_OUTCOMES.items()):
        if _run_is_terminal(store, run_id):
            # Landed after all (commit-then-raise) or closed by recovery: a
            # second write would duplicate the terminal event.
            _PENDING_RUN_OUTCOMES.pop(run_id, None)
            continue
        try:
            store.update_run(run_id, status, **fields)
        except ValueError as exc:
            log("attempt %s is already terminal; dropping remembered %s (%s)" % (run_id, status, exc))
            _PENDING_RUN_OUTCOMES.pop(run_id, None)
            continue
        except Exception as exc:  # noqa: BLE001 - still down; keep it for the next cycle
            log("WARNING could not replay %s for attempt %s: %r" % (status, run_id, exc))
            continue
        _PENDING_RUN_OUTCOMES.pop(run_id, None)
        landed += 1
        log("attempt %s recorded as %s from a remembered outcome" % (run_id, status))
    return landed


def pending_finalizations() -> Dict[str, Tuple[str, Optional[str], Optional[str]]]:
    """Outcomes still waiting for a durable write (diagnostics/tests)."""
    return dict((key, value[1:]) for key, value in _PENDING_FINALIZATIONS.items())


def replay_pending_finalizations(store: Any, log: Logger) -> int:
    """Retry terminal writes that failed earlier. Returns the number closed.

    Without this, a claimed request that never got a linked attempt (rejected
    during revalidation) and whose ``finish_retry`` writes all failed stays
    ``running``: reconciliation skips unlinked requests, ``claim_retry`` never
    re-claims a running row, and ``enqueue_retry`` dedups onto it, so that head
    could not be retried until the daemon restarted.
    """
    closed = 0
    for request_id, (request, status, run_id, error) in list(_PENDING_FINALIZATIONS.items()):
        observed = _request_status(store, request_id, log)
        if observed is not None and observed not in ("running", "queued"):
            _PENDING_FINALIZATIONS.pop(request_id, None)
            continue
        if _finish_quietly(store, request, status, log, run_id=run_id, error=error):
            closed += 1
            log("retry request %s finalized as %s from a remembered outcome" % (request_id, status))
    return closed


def _finish_quietly(
    store: Any,
    request: Dict[str, Any],
    status: str,
    log: Logger,
    run_id: Optional[str] = None,
    error: Optional[str] = None,
    attempts: int = FINISH_RETRY_ATTEMPTS,
) -> bool:
    """Record a request's terminal status. Returns whether it is now terminal.

    A swallowed failure here used to leave a request ``running`` forever while
    its attempt had really finished, so the write is retried a bounded number of
    times, and after an ambiguous error the store is re-read: the write may have
    landed before the error surfaced. An already-terminal request is never
    rewritten. The review is never re-run: this function only records.
    """
    request_id = str(request.get("id"))
    for attempt in range(1, max(1, int(attempts)) + 1):
        try:
            store.finish_retry(
                request_id,
                status,
                run_id=run_id,
                error=_clip(error) if error else None,
            )
            _PENDING_FINALIZATIONS.pop(request_id, None)
            return True
        except Exception as exc:  # noqa: BLE001
            observed = _request_status(store, request_id, log)
            if observed is not None and observed != "running" and observed != "queued":
                if observed != status:
                    log(
                        "WARNING retry request %s is already %s; not rewriting it to %s"
                        % (request_id, observed, status)
                    )
                _PENDING_FINALIZATIONS.pop(request_id, None)
                return True
            log(
                "WARNING could not finish retry request %s as %s (attempt %d/%d): %r"
                % (request_id, status, attempt, attempts, exc)
            )
    # Keep the known outcome: a linked attempt row can be reconciled later, an
    # unlinked request cannot, so the outcome itself is what gets replayed.
    if request_id in _PENDING_FINALIZATIONS or len(_PENDING_FINALIZATIONS) < MAX_PENDING_FINALIZATIONS:
        _PENDING_FINALIZATIONS[request_id] = (dict(request), status, run_id, error)
    else:
        log(
            "WARNING %d retry outcomes are already waiting for a durable write; not remembering %s "
            "(it is closed by recovery on restart)" % (len(_PENDING_FINALIZATIONS), request_id)
        )
    log(
        "WARNING retry request %s is still running while its attempt is terminal (%s); "
        "the dashboard store is degraded, so this is reconciled from the attempt row "
        "or the remembered outcome on a later cycle rather than guessed now" % (request_id, status)
    )
    return False


def _request_outcome_for_run(run: Dict[str, Any]) -> Optional[Tuple[str, Optional[str]]]:
    """Map a terminal attempt onto its request outcome, or ``None`` if not terminal.

    Success is only ever copied from a run the executor itself marked
    ``succeeded`` after a confirmed POST. An attempt whose posting outcome is
    unknown becomes a *failed* request with that reason: the request is closed,
    but nothing claims the review landed and the run keeps its own status.
    """
    status = str(run.get("status") or "")
    if status not in TERMINAL_RUN_STATUSES:
        return None
    if status == "succeeded":
        return ("succeeded", None)
    if posting_outcome_unknown(run):
        return (
            "failed",
            "attempt %s stopped at stage %s; review outcome unknown"
            % (run.get("id"), str(run.get("stage") or POSTING_STAGE)),
        )
    return ("failed", str(run.get("error") or "attempt %s ended %s" % (run.get("id"), status)))


def reconcile_running_requests(
    store: Optional[Any],
    lock: ExecutorLock,
    logger: Optional[Logger] = None,
    max_requests: int = MAX_RECONCILE_REQUESTS,
) -> int:
    """Close requests whose linked attempt is already durably terminal.

    This is the convergence path for a finalization write that failed while the
    attempt itself was durable: the outcome is read back from the attempt row,
    never inferred from the JSON ledger, from a missing row, or from the absence
    of an error. A request with no linked run, or one whose run is still
    running, is left alone; a request that is already terminal is not rewritten.

    Requires the executor lock, because it writes terminal request state that
    only the single executor may decide. Returns the number of requests closed.
    """
    if store is None:
        return 0
    lock.require_held("reconciling retry requests")
    log = _logger_or_default(logger)
    # Remembered terminal outcomes land first: a deferred recovery must not
    # mark an attempt interrupted whose confirmed outcome is only waiting for
    # the store to come back (the replay would then drop that outcome).
    replay_pending_run_outcomes(store, log)
    # Before a deferred recovery too: it would close these claimed, unrun
    # requests as interrupted, losing the operator's click.
    replay_pending_requeues(store, log)
    retry_pending_recovery(store, log)
    closed = replay_pending_finalizations(store, log)
    lister = getattr(store, "list_retries", None)
    if lister is None:
        log("WARNING store cannot list retry requests; skipping reconciliation")
        return 0
    try:
        running = lister(status="running", limit=max_requests)
    except Exception as exc:  # noqa: BLE001 - a dead store cannot be reconciled
        log("WARNING could not reconcile running retry requests: %r" % (exc,))
        return closed
    for request in running or []:
        run_id = request.get("run_id")
        if not run_id:
            # No attempt was ever linked: whether work happened is unknown, so
            # leave it to recovery on restart rather than guessing an outcome.
            continue
        try:
            run = store.get_run(str(run_id))
        except Exception as exc:  # noqa: BLE001
            log("WARNING could not read attempt %s while reconciling: %r" % (run_id, exc))
            continue
        if not run:
            continue
        outcome = _request_outcome_for_run(run)
        if outcome is None:
            continue
        status, reason = outcome
        if _finish_quietly(store, request, status, log, run_id=str(run_id), error=reason):
            closed += 1
            log(
                "retry request %s reconciled to %s from its durable attempt %s"
                % (request.get("id"), status, run_id)
            )
    return closed


def _execute_retry_request(
    store: Any,
    request: Dict[str, Any],
    repos: Any,
    client_factory: Callable[[str], Any],
    review_runner: Callable[..., bool],
    ledger_lookup: Callable[[str, int], Optional[Dict[str, Any]]],
    log: Logger,
) -> bool:
    request_id = str(request.get("id"))
    repo = str(request.get("repo") or "")
    try:
        number = int(request.get("pr"))
    except (TypeError, ValueError):
        return _finish_quietly(store, request, "failed", log, error="request has no usable PR number")
    head_sha = str(request.get("head_sha") or "")
    # Case-insensitive, as GitHub is: an attempt recorded under an older
    # spelling is retried under the spelling the cycle polls (one state bucket).
    repo = next((str(listed) for listed in repos if str(listed).lower() == repo.lower()), repo)
    if repo not in repos:
        return _finish_quietly(store, request, "failed", log, error="repository %s is not configured" % repo)
    if not head_sha:
        return _finish_quietly(store, request, "failed", log, error="request has no head sha")

    try:
        client = client_factory(repo)
        pr_payload = client.get_pr(number)
    except Exception as exc:  # noqa: BLE001 - upstream failure is per-request
        return _finish_quietly(store, request, "failed", log, error="could not fetch %s pr #%d: %s" % (repo, number, exc))

    invalid = _pr_validation_failure(request, pr_payload)
    if invalid is not None:
        status, reason = invalid
        log("retry %s: %s pr #%d %s" % (request_id, repo, number, reason))
        return _finish_quietly(store, request, status, log, error=reason)

    previous_run = None  # type: Optional[Dict[str, Any]]
    retry_of = request.get("retry_of")
    if retry_of:
        try:
            previous_run = store.get_run(str(retry_of))
        except Exception as exc:  # noqa: BLE001
            log("WARNING retry %s: could not read previous run %s: %r" % (request_id, retry_of, exc))
            previous_run = None
        if previous_run is None:
            return _finish_quietly(
                store,
                request,
                "failed",
                log,
                error="retried attempt %s no longer exists" % retry_of,
            )

    try:
        ledger_entry = ledger_lookup(repo, number)
    except Exception as exc:  # noqa: BLE001
        log("WARNING retry %s: ledger lookup failed: %r" % (request_id, exc))
        ledger_entry = None

    decision = retry_decision(request, previous_run, ledger_entry)
    if not decision.allow:
        log("retry %s: refused (%s)" % (request_id, decision.reason))
        return _finish_quietly(store, request, decision.status or "failed", log, error=decision.reason)

    rereview = request_kind(request) == RETRY_KIND_REREVIEW
    recorder = DashboardRecorder(
        store,
        # The attempt row names what asked for it; the dashboard labels both.
        trigger=RETRIGGER_TRIGGER if rereview else "retry",
        retry_of=str(previous_run["id"]) if previous_run else None,
        request_id=request_id,
        # The rereview's late check compares the ledger's review time with the
        # page's snapshot; a request queued without one, with its queue time.
        seen_at=_request_seen_at(request),
        # Fail closed: a claimed request that cannot be recorded durably must
        # not run, or the operator sees a claimed request while an untracked
        # review executes without the crash/duplicate-POST guard.
        strict=True,
        logger=log,
    )
    runner_kwargs = {"recorder": recorder, "force": decision.force}  # type: Dict[str, Any]
    if rereview:
        # Forwarded only for a rereview, like the policy: the runner then skips
        # its own reviewed-meanwhile guard and refreshes the visual outline.
        runner_kwargs["rereview"] = True
    options = request.get("options")
    policy = options.get(MODEL_POLICY_SETTING) if isinstance(options, dict) else None
    if isinstance(policy, dict) and policy:
        # Only an operator-chosen policy is forwarded; the poll path and
        # option-less retries keep the runner's own resolution order.
        runner_kwargs["model_policy"] = policy
    reuse, why_not = reusable_results(store, request, previous_run)
    if reuse is not None:
        # Forwarded only when there is something to reuse, like the policy.
        runner_kwargs["reuse_results"] = reuse
        log(
            "retry %s: reusing the results of %s from attempt %s; the other agents run again"
            % (request_id, ", ".join(sorted(reuse["results"])), reuse["run_id"])
        )
    else:
        log("retry %s: running every agent: %s" % (request_id, why_not))
    try:
        posted = review_runner(client, pr_payload, **runner_kwargs)
    except RetryDeferred as exc:
        # Nothing ran: no attempt was begun. Back in the queue, not failed, so
        # the operator's click is not lost; the next claim re-validates it. A
        # requeue that cannot be written now is remembered and retried before
        # the next claim (replay_pending_requeues); one the store refuses, or
        # for a request no longer running, is not, and the line says so.
        outcome, why = _requeue_quietly(store, request, log)
        if outcome == REQUEUED:
            log("retry %s: not run now (%s); returned to the queue" % (request_id, exc))
        elif outcome == REQUEUE_PENDING:
            log(
                "WARNING retry %s: not run now (%s), and not yet back in the queue; it is returned before "
                "the next claim" % (request_id, exc)
            )
        else:
            log("WARNING retry %s: not run now (%s), and not returned to the queue (%s)" % (request_id, exc, why))
        raise
    except RetryRefused as exc:
        # Nothing ran (refused before begin): no attempt to close.
        cause = " (%s)" % exc.__cause__ if exc.__cause__ is not None else ""
        log("retry %s: refused (%s)%s" % (request_id, exc.reason, cause))
        return _finish_quietly(store, request, exc.status, log, error=exc.reason)
    except DashboardUnavailable as exc:
        log("retry %s: refusing untracked review (%s)" % (request_id, exc))
        # The attempt may already exist (begin() succeeded, a required stage
        # write did not): close it too, or it stays running until a restart
        # while its request is already terminal. The recorder retries and, if
        # the store is still down, remembers the outcome for replay.
        recorder.failed("dashboard unavailable, review not run: %s" % exc)
        return _finish_quietly(
            store,
            request,
            "failed",
            log,
            run_id=recorder.run_id,
            error="dashboard unavailable, review not run: %s" % exc,
        )
    except Exception as exc:  # noqa: BLE001 - the review path already isolates PRs
        recorder.failed("review raised: %s" % exc)
        return _finish_quietly(
            store,
            request,
            "failed",
            log,
            run_id=recorder.run_id,
            error="review raised: %s" % exc,
        )
    if posted:
        return _finish_quietly(store, request, "succeeded", log, run_id=recorder.run_id)
    recorder.failed("review was not posted")
    return _finish_quietly(
        store,
        request,
        "failed",
        log,
        run_id=recorder.run_id,
        error="review was not posted (see attempt %s)" % (recorder.run_id or "n/a"),
    )


class RetryWaiter:
    """Interruptible daemon sleep: wakes when a retry request is enqueued.

    Uses the store's persisted event stream as the wake signal, which needs no
    extra store surface and cannot claim work the executor may fail to run. A
    request enqueued during the previous cycle (already drained) can cost one
    spurious early wake; the cursor always advances, so it never spins.
    """

    def __init__(
        self,
        store: Optional[Any],
        poll_interval: float = RETRY_WAKE_POLL_SECONDS,
        sleeper: Callable[[float], Any] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        logger: Optional[Logger] = None,
    ) -> None:
        self._store = store
        self._poll = max(0.01, float(poll_interval))
        self._sleep = sleeper
        self._monotonic = monotonic
        self._log = _logger_or_default(logger)
        self._cursor = 0
        self._probe_failed = False
        self._probe_warned = False
        self.sync()

    @property
    def cursor(self) -> int:
        return self._cursor

    def sync(self) -> int:
        """Move the cursor to the newest persisted event id.

        Asks the store for its tail directly when it can say (one indexed
        read); scanning payloads from zero is only the fallback for a store
        without ``latest_event_id``. The event table is append-only, so a
        scan bounded at MAX_EVENT_SCAN_BATCHES*500 rows would otherwise stop
        short of the tail on a long-lived database and every wake would then
        crawl the backlog 500 events per probe.
        """
        if self._store is None:
            return self._cursor
        tail = getattr(self._store, "latest_event_id", None)
        if tail is not None:
            try:
                self._cursor = max(self._cursor, int(tail() or 0))
            except Exception as exc:  # noqa: BLE001
                self._note_probe_failure(exc)
                return self._cursor
            self._note_probe_ok()
            return self._cursor
        for _ in range(MAX_EVENT_SCAN_BATCHES):
            try:
                events = self._store.events_after(self._cursor, limit=500)
            except Exception as exc:  # noqa: BLE001
                self._note_probe_failure(exc)
                return self._cursor
            if not events:
                break
            self._cursor = max(int(event["id"]) for event in events)
        self._note_probe_ok()
        return self._cursor

    def wait(self, interval: float) -> bool:
        """Sleep up to ``interval`` seconds; return True if woken by a request."""
        interval = max(0.0, float(interval))
        if self._store is None:
            if interval:
                self._sleep(interval)
            return False
        if self._probe_failed:
            # Back off for this interval only; the next wait probes again so
            # a transient store outage does not disable wake-ups for good.
            self._probe_failed = False
            if interval:
                self._sleep(interval)
            return False
        deadline = self._monotonic() + interval
        while True:
            if self._poll_for_request():
                return True
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return False
            if self._probe_failed:
                # The wake signal is gone for now: sleep out this interval
                # rather than spinning on a failing store, then let the next
                # wait probe again so a transient outage is not permanent.
                self._sleep(remaining)
                self._probe_failed = False
                return False
            self._sleep(min(self._poll, remaining))

    def _poll_for_request(self) -> bool:
        try:
            events = self._store.events_after(self._cursor, limit=500)
        except Exception as exc:  # noqa: BLE001
            self._note_probe_failure(exc)
            return False
        self._note_probe_ok()
        woken = False
        for event in events:
            self._cursor = max(self._cursor, int(event["id"]))
            if str(event.get("kind") or "") in WAKE_EVENT_KINDS:
                woken = True
        return woken

    def _note_probe_failure(self, exc: BaseException) -> None:
        if not self._probe_warned:
            self._log("WARNING retry wake probe failed; backing off for this interval: %r" % (exc,))
            self._probe_warned = True
        self._probe_failed = True

    def _note_probe_ok(self) -> None:
        if self._probe_warned:
            self._log("retry wake probe recovered")
        self._probe_failed = False
        self._probe_warned = False


def recover_after_restart(
    store: Optional[Any],
    lock: ExecutorLock,
    logger: Optional[Logger] = None,
) -> None:
    """Mark attempts/requests left running by a crash, once per executor start.

    Only safe while the exclusive executor lock is held, and deliberately not
    called from ordinary cycles: resetting rows that another (or the current)
    run owns would corrupt live attempts.
    """
    if store is None:
        return
    lock.require_held("recovering interrupted attempts")
    log = _logger_or_default(logger)
    global _RECOVERY_PENDING
    _RECOVERY_PENDING = True
    for attempt in range(1, RECOVERY_ATTEMPTS + 1):
        try:
            store.recover_interrupted()
        except Exception as exc:  # noqa: BLE001
            log(
                "WARNING dashboard recovery failed (attempt %d/%d): %r" % (attempt, RECOVERY_ATTEMPTS, exc)
            )
            continue
        _RECOVERY_PENDING = False
        return
    # Still pending: retried from reconcile_running_requests, which also runs
    # under the executor lock at points where this process owns no live
    # attempt, so rows still "running" then can only be crash leftovers.
    # Without this a stranded claimed request stayed running for the daemon's
    # whole lifetime (reconciliation skips unlinked requests, enqueue dedups
    # onto them) because recovery ran exactly once.
    log("WARNING dashboard recovery still pending; it is retried before the next retry drain")


#: Startup recovery attempts before deferring to the next drain.
RECOVERY_ATTEMPTS = 3
#: Whether ``recover_interrupted`` still has to succeed once for this executor.
_RECOVERY_PENDING = False


def recovery_pending() -> bool:
    return _RECOVERY_PENDING


def retry_pending_recovery(store: Any, log: Logger) -> bool:
    """Finish a startup recovery that failed; True once it has succeeded."""
    global _RECOVERY_PENDING
    if not _RECOVERY_PENDING:
        return True
    try:
        store.recover_interrupted()
    except Exception as exc:  # noqa: BLE001
        log("WARNING dashboard recovery still failing: %r" % (exc,))
        return False
    _RECOVERY_PENDING = False
    log("dashboard recovery completed after a deferred retry")
    return True


def make_head_resolver(
    client_factory: Optional[Callable[[str], Any]],
    logger: Optional[Logger] = None,
) -> Optional[Callable[[str, int], Optional[str]]]:
    """Build ``resolve_head(repo, pr) -> full sha | None`` for history import.

    The retained log keeps only a 12-char head prefix, so a historical failure
    can be given a usable 40-char head only by asking GitHub for the PR's
    current head. The daemon owns the GitHub client, so the callback is injected
    from here instead of letting the history module do I/O.

    The answer is *not* trusted as the attempt's head: the importer keeps it only
    while it still starts with the retained prefix, so a PR that moved on stays
    prefix-only (and therefore not retryable) rather than being rewritten to a
    head that was never reviewed. Lookups are bounded by the importer's own
    budget; a failure returns ``None`` with a sanitized log line.
    """
    if client_factory is None:
        return None
    log = _logger_or_default(logger)

    def resolve_head(repo: str, pr: int) -> Optional[str]:
        try:
            payload = client_factory(str(repo)).get_pr(int(pr))
        except Exception as exc:  # noqa: BLE001 - a lookup failure is not fatal
            log("WARNING head lookup failed for %s pr #%s: %s" % (repo, pr, _clip(exc, 200)))
            return None
        if not isinstance(payload, dict):
            return None
        head = payload.get("head")
        if not isinstance(head, dict):
            return None
        sha = str(head.get("sha") or "")
        return sha or None

    return resolve_head


def run_startup_imports(
    store: Optional[Any],
    lock: ExecutorLock,
    state: Optional[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
    client_factory: Optional[Callable[[str], Any]] = None,
    logger: Optional[Logger] = None,
) -> Dict[str, int]:
    """Single startup integration site for historical backfills.

    Runs the bounded JSON-ledger import owned here, then hands off to an
    optional retained-log failure importer so that history is not limited to
    successful reviews. That module is deliberately not part of this one: no log
    parsing happens in the executor path.

    Reserved contract for the history unit:
    ``code_review_service/dashboard_history.py`` exposing
    ``import_retained_failures(store, lock=None, logger=None, ...) -> dict``. It
    is imported only if present, called once per executor start while the
    exclusive executor lock is held, and any failure is logged without affecting
    reviews. Optional keywords it declares are supplied when we can: ``config``,
    ``configured_repos``, and ``resolve_head`` (built from ``client_factory``),
    the last of which is what turns a retained 12-char head prefix into a real
    head the retry gate can accept.
    """
    log = _logger_or_default(logger)
    counts = import_legacy_ledger(store, state, lock=lock, logger=log)
    if store is None:
        return counts
    try:
        from . import dashboard_history  # type: ignore[attr-defined]
    except ImportError:
        return counts
    importer = getattr(dashboard_history, "import_retained_failures", None)
    if importer is None:
        log("WARNING dashboard_history has no import_retained_failures; skipping failure history import")
        return counts
    kwargs = {"lock": lock, "logger": log}  # type: Dict[str, Any]
    # Supply optional keywords only if the importer declares them: the hook
    # contract stays (store, lock, logger) and does not grow with that module.
    try:
        accepted = inspect.signature(importer).parameters
    except (TypeError, ValueError):
        accepted = {}
    if config is not None:
        if "config" in accepted:
            kwargs["config"] = config
        if "configured_repos" in accepted:
            kwargs["configured_repos"] = list(config.get("repositories") or [])
    if "resolve_head" in accepted:
        resolver = make_head_resolver(client_factory, logger=log)
        if resolver is None:
            log(
                "WARNING no GitHub client available for history head lookups; "
                "imported failures stay 12-char prefix only"
            )
        else:
            kwargs["resolve_head"] = resolver
    try:
        extra = importer(store, **kwargs)
    except Exception as exc:  # noqa: BLE001 - history import never breaks reviews
        log("WARNING retained-failure history import failed: %r" % (exc,))
        return counts
    if isinstance(extra, dict):
        for key, value in extra.items():
            try:
                counts["history_" + str(key)] = int(value)
            except (TypeError, ValueError):
                continue
    return counts


#: The attempt trigger of a rereview (the Retrigger badge), next to ``poll`` and ``retry``.
RETRIGGER_TRIGGER = "retrigger"
#: Triggers the executor itself records; anything else is backfilled history.
NATIVE_TRIGGERS = ("poll", "retry", RETRIGGER_TRIGGER)
#: Native attempts inspected per head when matching a ledger outcome. Retries of
#: one head are rare; the bound only keeps a pathological head from paging.
MAX_NATIVE_ROWS_CHECKED = 50


def _natively_recorded(
    store: Any,
    repo: str,
    pr: int,
    head_sha: str,
    log: Logger,
    status: Optional[str] = None,
    stages: Optional[Sequence[str]] = None,
    not_status: Optional[str] = None,
) -> bool:
    """Whether the dashboard already holds a live attempt for this exact head.

    The extra conditions make the check answer "is *this* ledger record
    already represented", not "was the head ever attempted": ``status`` for a
    posted review, ``stages``/``not_status`` for a POST whose outcome is
    unknown. A store without ``attempts_for`` (older double) cannot answer, so
    the import proceeds as before rather than silently importing nothing.
    """
    exists = getattr(store, "has_attempt", None)
    if exists is not None:
        try:
            # One EXISTS query with every condition inside it: no page bound
            # can hide the matching row.
            return bool(
                exists(repo, pr, head_sha, NATIVE_TRIGGERS, status=status, stages=stages, not_status=not_status)
            )
        except Exception as exc:  # noqa: BLE001 - a lookup failure never blocks history
            log("WARNING could not check live attempts for %s pr #%s: %r" % (repo, pr, exc))
            return False
    lookup = getattr(store, "attempts_for", None)
    if lookup is None:
        return False
    try:
        rows = lookup(repo, pr, head_sha, limit=MAX_NATIVE_ROWS_CHECKED, triggers=NATIVE_TRIGGERS)
    except Exception as exc:  # noqa: BLE001 - a lookup failure never blocks history
        log("WARNING could not check live attempts for %s pr #%s: %r" % (repo, pr, exc))
        return False
    for row in rows or []:
        if str(row.get("trigger") or "") not in NATIVE_TRIGGERS:
            continue
        if status is not None and str(row.get("status") or "") != status:
            continue
        if stages is not None and str(row.get("stage") or "") not in stages:
            continue
        if not_status is not None and str(row.get("status") or "") == not_status:
            continue
        return True
    return False


def legacy_run_id(repo: str, pr: Any, head_sha: str, kind: str = "legacy") -> str:
    """Deterministic id so re-importing the JSON ledger cannot duplicate rows.

    ``kind`` (legacy / baseline / unverified) is part of the identity for
    everything but a posted review: a head first seen as baseline-only and
    later actually reviewed is two records, and ``INSERT OR IGNORE`` on one
    shared id would keep the ``skipped`` row and drop the success forever.
    """
    key = "%s|%s|%s" % (repo, pr, head_sha)
    if kind != "legacy":
        key += "|%s" % kind
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return "legacy-%s" % digest[:24]


def import_legacy_ledger(
    store: Optional[Any],
    state: Optional[Dict[str, Any]],
    lock: Optional[ExecutorLock] = None,
    logger: Optional[Logger] = None,
) -> Dict[str, int]:
    """Backfill pre-dashboard history from the JSON ledger, truthfully.

    The ledger holds two very different kinds of rows and the dashboard must
    not conflate them:

    - ``initial_baseline_without_review`` entries were recorded to *suppress* a
      review on first run. They import as ``trigger=baseline, status=skipped``
      and must never be counted as reviewed.
    - ``post_outcome_unknown`` entries parked a head whose review POST failed in
      flight. They import as ``status=interrupted`` at the posting-unverified
      stage: neither reviewed nor retryable until reconciled.
    - every other entry was written only after a confirmed review POST, so it
      imports as ``trigger=legacy, status=succeeded`` with the recorded
      ``reviewed_at``.

    Failed historical attempts are not in the ledger at all; they are not
    invented here (importing them from retained logs is separate work).
    Idempotent via ``legacy_run_id``.
    """
    counts = {"baseline": 0, "legacy": 0, "unverified": 0, "skipped": 0, "native": 0}
    if store is None or not isinstance(state, dict):
        return counts
    if lock is not None:
        lock.require_held("importing the legacy ledger")
    log = _logger_or_default(logger)
    repositories = state.get("repositories")
    if not isinstance(repositories, dict):
        return counts
    for repo in sorted(repositories):
        bucket = repositories.get(repo)
        if not isinstance(bucket, dict):
            continue
        reviewed = bucket.get("reviewed")
        if not isinstance(reviewed, dict):
            continue
        for pr_key in sorted(reviewed, key=lambda value: str(value)):
            entry = reviewed.get(pr_key)
            if not isinstance(entry, dict):
                continue
            head_sha = str(entry.get("head_sha") or "")
            try:
                number = int(pr_key)
            except (TypeError, ValueError):
                number = 0
            if not head_sha or number <= 0:
                counts["skipped"] += 1
                continue
            if _ledger_is_baseline(entry):
                match = {}  # type: Dict[str, Any]  # any native attempt for this head
            elif _ledger_post_unverified(entry):
                # The live path records an unverified POST as *failed* at
                # posting-unverified (service.py) and a crash mid-POST as
                # interrupted at posting: both are "outcome unknown".
                match = {"stages": POSTING_STAGES, "not_status": "succeeded"}
            else:
                match = {"status": "succeeded"}
            if _natively_recorded(store, repo, number, head_sha, log, **match):
                # A dashboard-era review writes both a UUID attempt row and this
                # ledger entry. Importing the entry again would show one review
                # as two attempts and label the live one legacy on every
                # restart; the deterministic legacy id only dedups legacy rows.
                # Matched on outcome, not mere presence: a native *failure* for
                # this head does not stand in for a ledger success that a
                # later, store-degraded attempt posted.
                counts["native"] += 1
                continue
            if _ledger_is_baseline(entry):
                kind = "baseline"
                status = "skipped"
                trigger = "baseline"
                stage = ""
                error = ""
            elif _ledger_post_unverified(entry):
                # The POST failed in flight: not reviewed, not retryable.
                kind = "unverified"
                status = "interrupted"
                trigger = "legacy"
                stage = POSTING_UNVERIFIED_STAGE
                error = _clip(entry.get("post_error") or "review POST outcome unknown")
            else:
                kind = "legacy"
                status = "succeeded"
                trigger = "legacy"
                stage = "posted"
                error = ""
            try:
                store.import_run(
                    legacy_run_id(repo, number, head_sha, kind),
                    repo,
                    number,
                    head_sha,
                    status=status,
                    trigger=trigger,
                    # An unknown historical timestamp stays empty rather than
                    # being backfilled with "now", which would fabricate it.
                    created_at=str(entry.get("reviewed_at") or ""),
                    stage=stage,
                    error=error,
                    total_agents=len(entry.get("agents") or []) or None,
                )
            except Exception as exc:  # noqa: BLE001 - import is best effort
                counts["skipped"] += 1
                log("WARNING legacy import failed for %s pr #%s: %r" % (repo, pr_key, exc))
                continue
            counts[kind] += 1
    if counts["legacy"] or counts["baseline"] or counts["unverified"] or counts["native"]:
        log(
            "dashboard legacy import: %d reviewed, %d baseline-only, %d unverified-post, %d skipped,"
            " %d already recorded live"
            % (counts["legacy"], counts["baseline"], counts["unverified"], counts["skipped"], counts["native"])
        )
    return counts
