"""Import prior FAILED review attempts from the retained service log (W2b).

Why this module exists: the JSON ledger is a success ledger. ``review_pull_request``
only writes state after a confirmed GitHub POST (service.py:1967-2018) and returns
``False`` on quorum failure (service.py:1839-1868), so
``dashboard_execution.import_legacy_ledger`` can never show a failed review --
and the user's acceptance clause is that past *failures* are visible, not only
successes. The one durable record of a past failure is the retained daemon
stdout log (``logs/service.out.log``) plus the preserved run directory.

What the log gives us, per attempt (all emitted by ``log()``, service.py:62-64,
which prefixes ``isoformat(timespec="seconds")``)::

    <ts> <repo> pr #<n>: cloning source into <run_dir>/source          service.py:1742
    <ts> <repo> pr #<n>: running <k> agents                            service.py:1778
    <ts> <repo> pr #<n>: only <s>/<k> agents succeeded; not posting review
                                                                       service.py:1841-1843
    <ts> <repo> pr #<n>: preserving run dir for inspection: <run_dir>   service.py:1867

``run_dir`` comes from ``mkdtemp(prefix=f"pr-{pr}-{head_sha[:12]}-")``
(service.py:568), so it is unique per attempt and carries a 12-char head prefix.
That makes it the attempt identity: the deterministic run id is a hash of it, so
re-importing on every daemon start is a no-op and three attempts on one PR stay
three rows.

Deliberate limits, because a history importer is in a position to lie:

* **Explicit terminal failure only.** A row is written only when the log contains
  both the quorum verdict *and* the preserving line naming the run dir. An
  attempt that simply stops (log rotated, daemon killed, exception path at
  service.py:2245) is counted, never imported: absence of a success line is not
  evidence of failure.
* **No invented head SHA.** The log only retains 12 hex chars. The full SHA is
  taken from an injected ``resolve_head(repo, pr)`` callback (the daemon has a
  GitHub client; this module performs no I/O beyond reading the log) and only if
  the answer still starts with the retained prefix. Otherwise the prefix is
  stored as-is and the reason says the head is incomplete, which is exactly the
  state in which W3's retry gate refuses to dispatch (submitted head must equal
  the original failed head).
* **No paths, no agent output.** Only the single quorum sentence and the agent
  counts are stored. Run directories are hashed into the id and then dropped;
  per-agent ``*.stderr.txt`` files are never read.
* **No events.** ``store.import_run`` is event-free by contract
  (dashboard_store.py:447-458) and this module never falls back to
  ``update_run``: a few hundred imported rows would otherwise flood the SSE
  replay with fake "just happened" traffic on every daemon start.

Streaming and bounded: the production log is ~380k lines, so it is read line by
line, parser state is capped at ``MAX_PENDING_ATTEMPTS`` open attempts, and only
the most recent ``limit`` completed failures are retained. Only the stdlib is
used and the module stays Python 3.9 compatible (the daemon runs
``/usr/bin/python3``, service.sh:97-135).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import inspect
import re
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

#: Prefix of the deterministic run id of an imported historical failure.
HISTORICAL_RUN_ID_PREFIX = "historical-failed-"
#: Trigger label so the UI can tell imported failures from live/legacy rows.
LEGACY_FAILURE_TRIGGER = "legacy-failure"
#: Same stage the live recorder uses for a quorum failure (service.py:1846).
QUORUM_STAGE = "quorum"
#: Appended to the reason when only the 12-char head prefix is known.
INCOMPLETE_HEAD_NOTE = "head sha incomplete (12-char prefix from retained run dir)"

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
#: Bounded resolver budget: history import must not become a GitHub crawl.
DEFAULT_MAX_HEAD_LOOKUPS = 50
#: Open (not yet terminal) attempts kept while streaming; the daemon reviews
#: serially, so this is generous, and eviction only drops non-imported state.
MAX_PENDING_ATTEMPTS = 512
#: Lines longer than this cannot be one of the four shapes we parse.
MAX_LINE_CHARS = 4096
#: ``head_sha[:12]`` (service.py:568).
HEAD_PREFIX_CHARS = 12

Logger = Callable[[str], None]
HeadResolver = Callable[[str, int], Optional[str]]

COUNT_KEYS = (
    "native_matched",
    "head_completed",
    "attempts_seen",
    "failed_parsed",
    "imported",
    "already_present",
    "dropped_over_limit",
    "unterminated",
    "error_terminated",
    "succeeded",
    "unknown_repo",
    "malformed",
    "ambiguous",
    "unmatched_preserve",
    "unmatched_quorum",
    "head_resolved",
    "head_prefix_only",
    "head_mismatch",
    "head_lookups",
    "head_lookup_errors",
    "metadata_unsupported",
    "store_errors",
    "store_unsupported",
    "log_missing",
    "unconfigured",
)

#: ``<ts> <owner>/<repo> pr #<n>: <rest>``. Banner box lines ("| PR: #481 |",
#: service.py:2029-2035) cannot match: they have no ``<repo> pr #<n>:`` triple.
_EVENT_RE = re.compile(
    r"^(?P<ts>\S+) (?P<repo>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+) pr #(?P<pr>\d+): (?P<rest>.+)$"
)
_CLONE_RE = re.compile(r"^cloning source into (?P<dir>.+)/source$")
_RUNNING_RE = re.compile(r"^running (?P<agents>\d+) agents$")
_QUORUM_RE = re.compile(
    r"^only (?P<ok>\d+)/(?P<total>\d+) agents succeeded; not posting review$"
)
_PRESERVE_RE = re.compile(r"^preserving run dir for inspection: (?P<dir>.+)$")
_POSTING_RE = re.compile(r"^posting \S+ review with \d+ line comments")
_ERROR_RE = re.compile(r"^skipped due to (?:unexpected )?error: ")
#: ``pr-<n>-<head12>-<mkdtemp suffix>`` (service.py:568).
_RUN_DIR_RE = re.compile(r"^pr-(?P<pr>\d+)-(?P<head>[0-9a-f]{%d})-(?P<suffix>.+)$" % HEAD_PREFIX_CHARS)
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class FailedAttempt(NamedTuple):
    """One historical attempt the log explicitly reports as a quorum failure."""

    run_id: str
    repo: str
    pr: int
    head_prefix: str
    #: Full SHA once a resolver confirmed it starts with ``head_prefix``.
    head_sha: Optional[str]
    created_at: str
    failed_at: str
    successful_agents: int
    total_agents: int
    reason: str
    #: Correlation key only; hashed into ``run_id`` and never stored.
    run_dir: str

    @property
    def stored_head(self) -> str:
        return self.head_sha or self.head_prefix

    @property
    def head_complete(self) -> bool:
        return self.head_sha is not None

    def with_head(self, head_sha: Optional[str]) -> "FailedAttempt":
        return self._replace(head_sha=head_sha)

    def stored_error(self) -> str:
        """The log's own sentence, plus an honest note when the head is partial."""
        if self.head_complete:
            return self.reason
        return "%s [%s]" % (self.reason, INCOMPLETE_HEAD_NOTE)


class _Pending:
    """An attempt seen starting, not yet known to have failed."""

    __slots__ = ("repo", "pr", "run_dir", "head_prefix", "created_at", "total_agents", "quorum")

    def __init__(self, repo: str, pr: int, run_dir: str, head_prefix: str, created_at: str) -> None:
        self.repo = repo
        self.pr = pr
        self.run_dir = run_dir
        self.head_prefix = head_prefix
        self.created_at = created_at
        self.total_agents = 0
        #: ``(successful, total, failed_at, reason)`` once the verdict is logged.
        self.quorum = None  # type: Optional[Tuple[int, int, str, str]]


def new_counts() -> Dict[str, int]:
    return dict((key, 0) for key in COUNT_KEYS)


def historical_run_id(run_dir: str) -> str:
    """Deterministic id for one retained attempt directory.

    The directory is unique per attempt (``mkdtemp``), so hashing it keeps
    re-imports idempotent and three attempts on the same head distinct. The hash
    also means the id carries no filesystem path.
    """
    digest = hashlib.sha256(str(run_dir).encode("utf-8")).hexdigest()
    return HISTORICAL_RUN_ID_PREFIX + digest[:24]


def default_log_path() -> Path:
    """``<install root>/logs/service.out.log`` (service.sh:18,35-36).

    This module lives in ``<install root>/code_review_service/``, which on the
    deployed host is ``/opt/review-of-legends/main/code_review_service/``.
    """
    return Path(__file__).resolve().parents[1] / "logs" / "service.out.log"


def _log_to(logger: Optional[Logger], message: str) -> None:
    if logger is not None:
        logger(message)


def _valid_timestamp(value: str) -> bool:
    """Accept exactly what ``log()`` writes; never coerce a bad date."""
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return False
    return True


def _bounded_limit(limit: Any, default: int = DEFAULT_LIMIT) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, MAX_LIMIT)


# --------------------------------------------------------------------- parsing


def parse_failed_attempts(
    log_path: Any,
    configured_repos: Iterable[str],
    limit: int = DEFAULT_LIMIT,
    counts: Optional[Dict[str, int]] = None,
) -> List[FailedAttempt]:
    """Stream ``log_path`` and return the newest explicitly failed attempts.

    Oldest-first within the retained window. Pure: no store, no network, no
    filesystem access beyond reading the log.
    """
    if counts is None:
        counts = new_counts()
    allowed = frozenset(str(repo) for repo in (configured_repos or ()))
    retained = deque(maxlen=_bounded_limit(limit))  # type: Deque[FailedAttempt]
    pending = OrderedDict()  # type: OrderedDict[Tuple[str, int], _Pending]

    def drop(key: Tuple[str, int], reason_key: str) -> None:
        pending.pop(key, None)
        counts[reason_key] += 1

    def keep(attempt: FailedAttempt) -> None:
        if len(retained) == retained.maxlen:
            counts["dropped_over_limit"] += 1
        retained.append(attempt)

    path = Path(log_path)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if " pr #" not in line or len(line) > MAX_LINE_CHARS:
                continue
            match = _EVENT_RE.match(line.rstrip("\n"))
            if match is None:
                continue
            repo = match.group("repo")
            rest = match.group("rest")
            timestamp = match.group("ts")
            try:
                pr = int(match.group("pr"))
            except ValueError:  # pragma: no cover - the regex guarantees digits
                counts["malformed"] += 1
                continue
            key = (repo, pr)

            clone = _CLONE_RE.match(rest)
            if clone is not None:
                if repo not in allowed:
                    counts["unknown_repo"] += 1
                    continue
                if not _valid_timestamp(timestamp):
                    counts["malformed"] += 1
                    continue
                run_dir = clone.group("dir")
                basename = _RUN_DIR_RE.match(Path(run_dir).name)
                if basename is None:
                    # No 12-char head prefix: the attempt cannot be identified.
                    counts["malformed"] += 1
                    continue
                if int(basename.group("pr")) != pr:
                    # Run dir disagrees with the logged PR: attributing the head
                    # prefix to either PR would be a guess.
                    counts["ambiguous"] += 1
                    continue
                if key in pending:
                    # A previous attempt for this PR never reached a verdict.
                    drop(key, "unterminated")
                if len(pending) >= MAX_PENDING_ATTEMPTS:
                    pending.popitem(last=False)
                    counts["unterminated"] += 1
                pending[key] = _Pending(repo, pr, run_dir, basename.group("head"), timestamp)
                counts["attempts_seen"] += 1
                continue

            if repo not in allowed:
                continue
            open_attempt = pending.get(key)

            running = _RUNNING_RE.match(rest)
            if running is not None:
                if open_attempt is not None:
                    open_attempt.total_agents = int(running.group("agents"))
                continue

            quorum = _QUORUM_RE.match(rest)
            if quorum is not None:
                if open_attempt is None:
                    # No run dir for this verdict: no attempt identity exists.
                    counts["unmatched_quorum"] += 1
                    continue
                if not _valid_timestamp(timestamp):
                    counts["malformed"] += 1
                    drop(key, "unterminated")
                    continue
                open_attempt.quorum = (
                    int(quorum.group("ok")),
                    int(quorum.group("total")),
                    timestamp,
                    rest,
                )
                continue

            preserve = _PRESERVE_RE.match(rest)
            if preserve is not None:
                if open_attempt is None or open_attempt.run_dir != preserve.group("dir"):
                    counts["unmatched_preserve"] += 1
                    continue
                if open_attempt.quorum is None:
                    # Preserved for some other reason; the log never said the
                    # quorum failed, so nothing is imported.
                    continue
                successful, total, failed_at, reason = open_attempt.quorum
                keep(
                    FailedAttempt(
                        run_id=historical_run_id(open_attempt.run_dir),
                        repo=open_attempt.repo,
                        pr=open_attempt.pr,
                        head_prefix=open_attempt.head_prefix,
                        head_sha=None,
                        created_at=open_attempt.created_at,
                        failed_at=failed_at,
                        successful_agents=successful,
                        total_agents=total or open_attempt.total_agents,
                        reason=reason,
                        run_dir=open_attempt.run_dir,
                    )
                )
                counts["failed_parsed"] += 1
                pending.pop(key, None)
                continue

            if _POSTING_RE.match(rest) is not None:
                if open_attempt is not None:
                    drop(key, "succeeded")
                continue

            if _ERROR_RE.match(rest) is not None:
                # An exception outcome, not a quorum verdict: the attempt may
                # even have posted its review. Counted, not imported.
                if open_attempt is not None:
                    drop(key, "error_terminated")
                continue

    counts["unterminated"] += len(pending)
    return list(retained)


# ------------------------------------------------------------------ head SHAs


def resolve_heads(
    attempts: Sequence[FailedAttempt],
    resolve_head: Optional[HeadResolver],
    counts: Dict[str, int],
    max_lookups: int = DEFAULT_MAX_HEAD_LOOKUPS,
    logger: Optional[Logger] = None,
) -> List[FailedAttempt]:
    """Complete head SHAs through the injected resolver, newest PRs first.

    One lookup per unique ``(repo, pr)``, hard-bounded: the newest failures are
    the ones an operator may still retry, so they get the budget. An answer is
    accepted only when it is a 40-char lowercase SHA starting with the retained
    prefix; a moved head keeps the prefix instead of pretending.
    """
    resolved = list(attempts)
    if resolve_head is None or not resolved:
        return resolved
    budget = max(int(max_lookups), 0)
    if budget == 0:
        return resolved

    order = []  # type: List[Tuple[str, int]]
    for attempt in reversed(resolved):
        key = (attempt.repo, attempt.pr)
        if key not in order:
            order.append(key)
        if len(order) >= budget:
            break

    heads = {}  # type: Dict[Tuple[str, int], str]
    for repo, pr in order:
        counts["head_lookups"] += 1
        try:
            answer = resolve_head(repo, pr)
        except Exception as exc:  # noqa: BLE001 - a lookup failure is not fatal
            counts["head_lookup_errors"] += 1
            _log_to(logger, "WARNING head lookup failed for %s pr #%d: %r" % (repo, pr, exc))
            continue
        if not isinstance(answer, str) or not _FULL_SHA_RE.match(answer):
            continue
        heads[(repo, pr)] = answer

    completed = []  # type: List[FailedAttempt]
    for attempt in resolved:
        answer = heads.get((attempt.repo, attempt.pr))
        if answer is None:
            completed.append(attempt)
            continue
        if not answer.startswith(attempt.head_prefix):
            # The PR moved on since the failure: this SHA is not that attempt's.
            counts["head_mismatch"] += 1
            completed.append(attempt)
            continue
        completed.append(attempt.with_head(answer))
    return completed


# -------------------------------------------------------------------- importing

#: Passed to ``import_run`` when the store supports them; W1's shipped signature
#: (dashboard_store.py:436-446) does not carry the failure metadata yet.
_METADATA_KWARGS = ("stage", "error", "successful_agents")
_REQUIRED_KWARGS = ("status", "trigger", "created_at")


def _import_kwargs_support(store: Any) -> Optional[frozenset]:
    """Keyword names ``store.import_run`` accepts, or ``None`` for "anything"."""
    try:
        signature = inspect.signature(store.import_run)
    except (AttributeError, TypeError, ValueError):
        return frozenset()
    for parameter in signature.parameters.values():
        if parameter.kind is parameter.VAR_KEYWORD:
            return None
    return frozenset(signature.parameters)


def import_failed_history(
    store: Any,
    log_path: Any,
    configured_repos: Iterable[str],
    resolve_head: Optional[HeadResolver] = None,
    limit: int = DEFAULT_LIMIT,
    max_head_lookups: int = DEFAULT_MAX_HEAD_LOOKUPS,
    logger: Optional[Logger] = None,
) -> Dict[str, int]:
    """Import the newest explicitly failed attempts from ``log_path``.

    Returns a counts dict (ints only, so the caller can prefix and log them).
    Never raises for an absent/unreadable log or a single failing store write:
    history is observability, and it must not be able to stop reviews.
    """
    counts = new_counts()
    if store is None:
        return counts
    allowed = [str(repo) for repo in (configured_repos or ())]
    if not allowed:
        counts["unconfigured"] = 1
        _log_to(
            logger,
            "WARNING retained-failure import skipped: no configured repositories supplied",
        )
        return counts

    path = Path(log_path)
    if not path.is_file():
        counts["log_missing"] = 1
        return counts

    supported = _import_kwargs_support(store)
    if supported is not None and not set(_REQUIRED_KWARGS).issubset(supported):
        # Without ``status``/``trigger`` the row would default to a *succeeded*
        # legacy review: a failure recorded as a success is worse than no row.
        counts["store_unsupported"] = 1
        _log_to(
            logger,
            "WARNING retained-failure import skipped: store.import_run lacks %s"
            % ", ".join(sorted(set(_REQUIRED_KWARGS) - set(supported or ()))),
        )
        return counts
    missing_metadata = (
        []
        if supported is None
        else [name for name in _METADATA_KWARGS if name not in supported]
    )
    if missing_metadata:
        # Degrade field by field, never all-or-nothing: dropping the quorum
        # reason because one counter is missing would hide *why* it failed.
        counts["metadata_unsupported"] = 1
        _log_to(
            logger,
            "WARNING store.import_run lacks %s; imported failures omit those fields"
            % ", ".join(missing_metadata),
        )

    try:
        attempts = parse_failed_attempts(path, allowed, limit=limit, counts=counts)
    except OSError as exc:
        counts["log_missing"] = 1
        _log_to(logger, "WARNING retained-failure log unreadable at %s: %r" % (path, exc))
        return counts

    # A quorum failure the live recorder already stored (under a UUID) still
    # leaves its clone/quorum/preserve lines in this log; the hash-derived id
    # cannot recognise it. Each parsed failure is matched to a native row by
    # the run directory name both sides carry - one execution, one row - so
    # an execution the recorder missed (store degraded) is still imported.
    kept = []
    for attempt in attempts:
        if _natively_matched(store, attempt, logger):
            counts["native_matched"] += 1
            continue
        kept.append(attempt)
    attempts = kept

    attempts = resolve_heads(
        attempts, resolve_head, counts, max_lookups=max_head_lookups, logger=logger
    )

    get_run = getattr(store, "get_run", None)
    complete_head = getattr(store, "complete_head", None)
    for attempt in attempts:
        if get_run is not None:
            try:
                existing = get_run(attempt.run_id)
            except Exception as exc:  # noqa: BLE001 - fall through to the insert
                _log_to(logger, "WARNING history existence check failed: %r" % (exc,))
                existing = None
            if existing is not None:
                counts["already_present"] += 1
                _completes_prefix(existing, attempt, complete_head, counts, logger)
                continue
        kwargs = {
            "status": "failed",
            "trigger": LEGACY_FAILURE_TRIGGER,
            # The attempt's own start line; inventing "now" would claim the
            # failure happened at this daemon start.
            "created_at": attempt.created_at,
            "total_agents": attempt.total_agents or None,
            "stage": QUORUM_STAGE,
            "error": attempt.stored_error(),
            "successful_agents": attempt.successful_agents,
        }
        if supported is not None:
            kwargs = dict((name, value) for name, value in kwargs.items() if name in supported)
        try:
            store.import_run(
                attempt.run_id, attempt.repo, attempt.pr, attempt.stored_head, **kwargs
            )
        except Exception as exc:  # noqa: BLE001 - one bad row never aborts import
            counts["store_errors"] += 1
            _log_to(
                logger,
                "WARNING retained-failure import failed for %s pr #%d: %r"
                % (attempt.repo, attempt.pr, exc),
            )
            continue
        counts["imported"] += 1
        if attempt.head_complete:
            counts["head_resolved"] += 1
        else:
            counts["head_prefix_only"] += 1

    if counts["imported"] or counts["already_present"] or counts["store_errors"]:
        _log_to(
            logger,
            "dashboard failure history: %d imported, %d already present, %d unresolved head,"
            " %d store errors" % (
                counts["imported"],
                counts["already_present"],
                counts["head_prefix_only"],
                counts["store_errors"],
            ),
        )
    return counts


#: Triggers the executor records for its own attempts (dashboard_execution).
_NATIVE_TRIGGERS = ("poll", "retry", "retrigger")


def _natively_matched(store: Any, attempt: FailedAttempt, logger: Optional[Logger]) -> bool:
    """Whether the live recorder already stored this very execution.

    Identity, not proximity: the recorder stores the review run directory
    *name* on its attempt row (service.py, the "cloning" stage) and this
    module parsed that same directory from the log line. ``mkdtemp`` makes the
    name unique per attempt, so one native row can match exactly one log
    execution, an unrecorded execution never matches, and repeated imports
    stay idempotent. A store without ``has_attempt`` (older double) cannot
    answer, so the row is imported.
    """
    exists = getattr(store, "has_attempt", None)
    if exists is None:
        return False
    name = Path(str(attempt.run_dir)).name
    if not name:
        return False
    try:
        return bool(exists(attempt.repo, attempt.pr, attempt.head_prefix, _NATIVE_TRIGGERS, run_dir=name))
    except Exception as exc:  # noqa: BLE001 - never abort the import over matching
        _log_to(logger, "WARNING native-attempt match failed for %s pr #%d: %r" % (attempt.repo, attempt.pr, exc))
        return False


def _completes_prefix(
    existing: Any,
    attempt: FailedAttempt,
    complete_head: Optional[Callable[..., bool]],
    counts: Dict[str, int],
    logger: Optional[Logger],
) -> bool:
    """Widen a previously imported prefix-only head once a lookup succeeds.

    The first startup may import a failure with only ``head_sha[:12]`` because
    GitHub was unreachable; that row is history and is never re-inserted, so
    without this the retry gate (which needs GitHub's full head) would refuse
    it forever. Only the head and the "incomplete" note change - the failure's
    status, stage and reason are preserved.
    """
    if complete_head is None or not attempt.head_complete:
        return False
    stored = ""
    try:
        stored = str(existing.get("head_sha") or "") if isinstance(existing, dict) else ""
    except Exception:  # noqa: BLE001 - a double without dict rows
        return False
    if not stored or len(stored) >= len(attempt.head_sha or "") or not attempt.head_sha.startswith(stored):
        return False
    try:
        changed = bool(complete_head(attempt.run_id, attempt.head_sha, error=attempt.stored_error()))
    except Exception as exc:  # noqa: BLE001 - never abort the import over enrichment
        _log_to(logger, "WARNING head completion failed for %s pr #%d: %r" % (attempt.repo, attempt.pr, exc))
        return False
    if changed:
        counts["head_completed"] += 1
    return changed


def import_retained_failures(
    store: Any,
    lock: Any = None,
    logger: Optional[Logger] = None,
    config: Optional[Dict[str, Any]] = None,
    log_path: Any = None,
    configured_repos: Optional[Iterable[str]] = None,
    resolve_head: Optional[HeadResolver] = None,
    limit: int = DEFAULT_LIMIT,
    max_head_lookups: int = DEFAULT_MAX_HEAD_LOOKUPS,
) -> Dict[str, int]:
    """Startup hook reserved by ``dashboard_execution.run_startup_imports``.

    That call site (dashboard_execution.py:883-905) passes ``store``, ``lock``
    and ``logger`` only, so the repository allowlist and log path are resolved
    from ``config`` when given and otherwise from the deployed layout. With no
    allowlist available nothing is imported and the reason is reported: guessing
    which repositories are configured would import rows for repositories the
    operator removed.
    """
    if store is None:
        return new_counts()
    if lock is not None:
        require_held = getattr(lock, "require_held", None)
        if require_held is not None:
            require_held("importing retained failure history")

    settings = config.get("dashboard") if isinstance(config, dict) else None
    if not isinstance(settings, dict):
        settings = {}
    if log_path is None:
        configured_path = settings.get("history_log_path") or (
            config.get("history_log_path") if isinstance(config, dict) else None
        )
        log_path = Path(str(configured_path)).expanduser() if configured_path else default_log_path()
    if configured_repos is None:
        repositories = config.get("repositories") if isinstance(config, dict) else None
        configured_repos = repositories if isinstance(repositories, (list, tuple)) else []
    if limit == DEFAULT_LIMIT:
        limit = _bounded_limit(settings.get("history_limit", DEFAULT_LIMIT))

    return import_failed_history(
        store,
        log_path,
        configured_repos,
        resolve_head=resolve_head,
        limit=limit,
        max_head_lookups=max_head_lookups,
        logger=logger,
    )
