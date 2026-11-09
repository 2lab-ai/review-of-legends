"""The daemon's review scheduler: a PR's review starts seconds after it is opened.

The daemon used to run ``once`` in a loop: list a repository's open PRs, review
them one after another, move to the next repository, sleep. A PR opened just
after its repository was listed waited for the whole cycle (a production PR on
2026-10-13: opened 10:09:51, review started 11:00:29). This module replaces
that loop for the daemon only; ``once`` keeps it.

- **Watcher** (the main thread): every ``watch_interval_seconds`` it re-reads
  the config and lists each review repository's open PRs. The listing is a
  conditional request, so an unchanged list is answered 304 and costs no rate
  limit. Every head that needs a review is queued. Drafts are not, unless
  ``review_drafts`` is true.
- **Dispatch**: a PR's first review starts at once, on its own thread, unless
  ``max_concurrent_reviews`` reviews (operator retries included) are already
  running, the hard safety cap. Updates of already-reviewed PRs run at most
  ``max_concurrent_update_reviews`` at a time. Just before a review starts the
  PR is read again from GitHub: a PR closed, merged or back to draft meanwhile
  is dropped, and a moved head is checked and reviewed instead.
- **Retries**: operator retry requests run on their own thread
  (``dashboard_execution.drain_retry_requests``). A PR is never reviewed by two
  threads at once; a retry that cannot start (paused, stopping, the PR changed)
  goes back to the queue.
- **Reviewed status**: after each listing the watcher sets the
  ``review-of-legends/reviewed/<base>`` commit status to success on every
  open, non-draft PR that has had a review, on a base and head it was not yet
  set on: a new push to a reviewed PR stays mergeable without waiting for its
  update review, a retargeted PR gets the new base's context, and PRs reviewed
  before the status existed get it once. At most
  ``MAX_REVIEWED_STATUS_POSTS_PER_TICK`` per tick; after a failed POST, none
  for that repository for ``poll_interval_seconds``.
- **Mentions**: after the listings, while the dashboard store is open, the
  watcher reads each listed repository's PR comments updated since its cursors
  (two conditional requests, 304 while nothing is new) and answers the
  ``@review-of-legends`` commands in them (mentions.py): GitHub never tells an
  App's bot it was mentioned. Both kinds of comment are answered in one
  oldest-first order, so a newer command is never applied before an older one.
  The repositories take turns going first; a command costs two of the tick's
  ``MAX_REVIEWED_STATUS_POSTS_PER_TICK`` content POSTs, and one command a tick
  is always allowed. A watch that was refused for the rate limit reads no
  comments; a refusal of a comment listing, a reaction or a reply ends it.

The executor lock is held while anything runs (a review, a retry drain, a
baseline or ledger write, the run dir sweep or prune) and released when nothing
does, so the dashboard's executor state and a manual ``once`` keep working.
state.json is re-read every time the lock is taken from idle, because a
``once`` may have written it meanwhile. The pause sentinel stops new work;
reviews in flight finish, then the lock is released (the deploy drain waits for
exactly that).

Python 3.9, stdlib only: the daemon runs on the host's /usr/bin/python3.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import dashboard_execution
from . import mentions
from . import author_review
from . import service as svc

FIRST = "first"
UPDATE = "update"
RETRY = "retry"
# Below this many GitHub API calls left, the watcher lists every
# poll_interval_seconds instead of every watch_interval_seconds.
RATE_LIMIT_FLOOR = 300
# How often an idle thread re-checks the pause sentinel and a busy lock.
PAUSE_POLL_SECONDS = 5.0
# How often the watcher prunes run dirs past their retention (never strips clones).
PRUNE_INTERVAL_SECONDS = 3600.0
# The most reviewed-status POSTs one watch tick makes, across repositories;
# the rest go out on the next tick (the one-time backfill after a deploy).
# 20 per 20 s tick is 60 a minute, under GitHub's ~80/min secondary limit on
# content-creating requests.
MAX_REVIEWED_STATUS_POSTS_PER_TICK = 20
# A command's content POSTs (a reaction and a reply), out of the same budget per tick.
COMMAND_POSTS = 2
# Watches in a row a command's GitHub copy may differ from the same listed
# revision before it is passed over: an edit lists a new revision and starts
# the count again, so only a difference that never goes away (a deleted
# author's account, say) reaches this.
MAX_SNAPSHOT_MISMATCHES = 3
# A comment cursor starts at GitHub's clock (the Date of the PR listing just
# read); without one, this long before the daemon's, so a daemon clock that
# runs ahead does not take new comments for history. Comments in the margin
# are answered, each once (the cursor's seen ids).
CURSOR_START_MARGIN_SECONDS = 120.0

Key = Tuple[str, int]


def _log(message: str) -> None:
    # Looked up per call so a test patching service.log sees scheduler lines too.
    svc.log(message)


def _created_at(pr: Dict[str, Any]) -> Optional[float]:
    raw = pr.get("created_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _rate_limit_refusal(exc: Exception) -> bool:
    """A listing refused for a rate limit: a 429, or a 403 that says so.

    A secondary limit refuses with plenty of primary quota left, so the
    X-RateLimit-Remaining header alone would keep the watcher listing every
    tick through the cooldown. A 403 for anything else (a lost permission)
    must not slow the other repositories.
    """
    status = getattr(exc, "status_code", None)
    return status == 429 or (status == 403 and "rate limit" in str(exc).lower())


def _head(pr: Any) -> str:
    if not isinstance(pr, dict):
        return ""
    return str((pr.get("head") or {}).get("sha") or "")


class Job:
    """One queued review: the newest listed payload of a PR that needs one."""

    def __init__(
        self,
        repo: str,
        number: int,
        head: str,
        pr: Dict[str, Any],
        kind: str,
        seq: int,
        origin: str,
        origin_at: float,
        tick: int = 0,
    ) -> None:
        self.repo = repo
        self.number = number
        self.head = head
        self.pr = pr
        self.kind = kind
        self.seq = seq
        self.tick = tick
        # What the dispatch log measures from: "opened" (PR created_at),
        # "ready" (when the watcher saw the PR leave draft) or "seen" (when
        # the watcher saw the head).
        self.origin = origin
        self.origin_at = origin_at
        self.cap_warned = False
        # The GitHub client that re-read the PR at dispatch; the review uses it.
        self.client: Any = None
        # Not started because the executor paused or stopped meanwhile: it goes
        # back in the queue, with nothing recorded for it.
        self.halted = False

    @property
    def key(self) -> Key:
        return (self.repo.lower(), self.number)

    def rank(self) -> Tuple[bool, int, float, int]:
        # First reviews before updates; then the tick that saw it; then the
        # earlier-opened PR. The dispatcher picks among what is queued when a
        # slot frees, and each repository's listing is queued as it arrives, so
        # at the hard cap a later repository's older PR can still lose a slot to
        # a newer PR listed earlier in the same tick.
        return (self.kind != FIRST, self.tick, self.origin_at, self.seq)


class ReviewScheduler:
    def __init__(
        self,
        config: Dict[str, Any],
        config_path: Path,
        state_path: Path,
        store: Optional[Any] = None,
        lock: Optional[dashboard_execution.ExecutorLock] = None,
        pause_path: Optional[Path] = None,
        watch_client_factory: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        client_factory: Optional[Callable[[str], Any]] = None,
        review_job: Optional[Callable[[Job], Any]] = None,
        sweep: Optional[Callable[[], Any]] = None,
        prune: Optional[Callable[..., Any]] = None,
        retry_waiter: Optional[Any] = None,
        stop_requested: Optional[Callable[[], bool]] = None,
        config_loader: Optional[Callable[[], Dict[str, Any]]] = None,
        store_opener: Optional[Callable[[], Any]] = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Any] = time.sleep,
    ) -> None:
        # Guards the queue, the in-flight registry, the leases and the caps.
        # Lock order: _cond, then STATE_LOCK; never the reverse.
        self._cond = threading.Condition()
        self.config: Dict[str, Any] = {}
        self.hard_cap = 0
        self.update_cap = 0
        self.watch_interval = float(svc.DEFAULT_WATCH_INTERVAL_SECONDS)
        self.slow_interval = 180.0
        self.review_drafts = False
        self._apply_config(config)
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)
        self.store = store
        # Fixed for the daemon's life: the deploy drain finds them by these paths.
        self.pause_path = (
            Path(pause_path) if pause_path is not None
            else dashboard_execution.executor_pause_path(config, self.state_path)
        )
        self.lock = lock or dashboard_execution.ExecutorLock(
            dashboard_execution.executor_lock_path(config, self.state_path), logger=_log
        )
        # The default factories read self.config when called, so a re-read
        # config (gh_bin, github_app) applies to the next client.
        self._watch_client_factory = watch_client_factory or (
            lambda repo, cache: svc.make_github_client(repo, self.config, etag_cache=cache)
        )
        self._client_factory = client_factory or (lambda repo: svc.make_github_client(repo, self.config))
        self._review_job = review_job or self._run_review
        self._sweep = sweep or self._sweep_run_dirs
        self._prune = prune or self._prune_run_dirs
        self._waiter = retry_waiter
        self._stop_requested = stop_requested or (lambda: dashboard_execution.stop_requested(self.pause_path))
        self._config_loader = config_loader or (lambda: svc.load_config(self.config_path))
        self._store_opener = store_opener or (
            lambda: dashboard_execution.open_store(self.config, self.state_path, logger=_log)
        )
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleeper

        # The ledger (state.json), loaded when the lock is first taken. Read and
        # written only under service.STATE_LOCK; replaced only with no lease out.
        self.state: Optional[Dict[str, Any]] = None
        self._queue: Dict[Key, Job] = {}
        self._in_flight: Dict[Key, str] = {}  # every review running: pool jobs and the retry
        # The PRs a successful listing showed open (this process only), and
        # those a later successful listing of the same repository no longer showed
        # (merged or closed): the next prune removes their run dirs, the ones whose
        # recorded repository is theirs. Read and written by the watcher thread only.
        self._open_prs: Set[Key] = set()
        self._closed_prs: Set[Key] = set()
        self._pool_busy = {FIRST: 0, UPDATE: 0}
        self._leases = 0  # holders of the executor lock in this process
        self._resume_pending = True  # startup counts as a resume: sweep before any review
        self._paused = False
        self._exclusive = False  # the retry thread needs every pool review finished
        self._stopping = False
        self._seq = 0
        self._ticks = 0
        self._busy_logged = False
        self._seen_draft: Set[Key] = set()
        self._skip_logged: Dict[Key, Tuple[str, str]] = {}
        self._threads: List[threading.Thread] = []
        # The dispatch and retry threads by name, so a stopped one can be started again.
        self._workers: Dict[str, Tuple[Callable[[], None], threading.Thread]] = {}
        self._started = False
        self._retry_deferred = False
        self._state_error: Optional[str] = None
        # The quorum problem last logged per dashboard repository (asked every tick and lease).
        self._repository_problems: Dict[str, str] = {}
        # Watcher-thread only.
        self._watch_clients: Dict[str, Any] = {}
        self._etag_caches: Dict[str, Dict[str, Any]] = {}
        self._listing_failed: Set[str] = set()
        # Repository (lower case) -> monotonic time its reviewed-status POSTs may
        # resume after a failed one; and the repositories whose failure was logged.
        self._reviewed_status_backoff: Dict[str, float] = {}
        self._reviewed_status_failing: Set[str] = set()
        # (repository lower case, comment kind) whose comment listing failed, logged once.
        self._mentions_failing: Set[Tuple[str, str]] = set()
        # GitHub's clock (epoch seconds) at each repository's last PR listing: where a new cursor starts.
        self._listed_at: Dict[str, Any] = {}
        # A cursor start whose save failed: the same time is tried again, so a
        # command written before the save works is not taken for history.
        self._cursor_starts: Dict[str, str] = {}
        # A reviewed-status POST refused for the rate limit this tick: no mention work either.
        self._status_rate_limited = False
        # Repositories (lower case) in the order they go first in a mention
        # phase; the one that went first moves to the back (_poll_all_mentions).
        self._mention_turns: List[str] = []
        # Monotonic time before which no command is answered: GitHub refused a
        # reviewed-status POST for its content-creation limit, which a command's
        # reaction and reply would hit too (after its cursor is saved).
        self._mentions_resume_at = 0.0
        # (repository lower case, kind, comment id) -> (the listed revision, watches in
        # a row GitHub's copy differed from that same revision); at
        # MAX_SNAPSHOT_MISMATCHES it is passed over. A new listed revision restarts it.
        self._snapshot_mismatches: Dict[Tuple[str, str, int], Tuple[Tuple[str, str, str], int]] = {}
        self._rate_limited = False
        self._config_error: Optional[str] = None
        # The startup sweep prunes too; the watcher's first prune is an hour later.
        self._last_prune = self._monotonic()
        # run_daemon has just tried to open the store; the next try is a poll interval later.
        self._last_store_open = self._monotonic()

    # ---------------------------------------------------------------- config

    def _apply_config(self, config: Dict[str, Any]) -> bool:
        """Take ``config`` (ServiceError if its caps are invalid); True if a cap or interval changed.

        Applies to what starts next: reviews already running keep going.
        """
        hard_cap, update_cap = svc.review_caps(config)
        watch = float(config.get("watch_interval_seconds", svc.DEFAULT_WATCH_INTERVAL_SECONDS))
        slow = max(float(config.get("poll_interval_seconds", 180)), watch)
        drafts = bool(config.get("review_drafts", False))
        with self._cond:
            before = (self.hard_cap, self.update_cap, self.watch_interval, self.review_drafts)
            self.config = config
            self.hard_cap, self.update_cap = hard_cap, update_cap
            self.watch_interval, self.slow_interval, self.review_drafts = watch, slow, drafts
            # A raised cap may let queued work start now.
            self._cond.notify_all()
        return before != (hard_cap, update_cap, watch, drafts)

    def _reload_config(self) -> None:
        """Re-read the config file; on an error keep the last good one (one WARNING per error)."""
        previous = self.config
        try:
            changed = self._apply_config(self._config_loader())
        except Exception as exc:  # noqa: BLE001 - a bad edit must not stop reviews
            message = str(exc)
            if message != self._config_error:
                self._config_error = message
                _log("WARNING config re-read failed, keeping the last good config: %s" % message)
            return
        if self._config_error is not None:
            self._config_error = None
            _log("config readable again")
        if changed:
            _log(
                "config: up to %d reviews at once (%d updates), watching every %gs, drafts %s"
                % (
                    self.hard_cap,
                    self.update_cap,
                    self.watch_interval,
                    "reviewed" if self.review_drafts else "wait until ready",
                )
            )
        if (previous.get("gh_bin"), previous.get("github_app")) != (
            self.config.get("gh_bin"),
            self.config.get("github_app"),
        ):
            self._watch_clients.clear()

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._started = True
        self._spawn(self._dispatcher, "dispatch")
        if self.store is not None:
            self._start_retry_thread()

    def _start_retry_thread(self) -> None:
        if self._waiter is None:
            self._waiter = dashboard_execution.RetryWaiter(self.store, logger=_log)
        self._spawn(self._retry_loop, "retry")

    def _spawn(self, target: Callable[[], None], name: str) -> threading.Thread:
        thread = threading.Thread(target=target, name=name, daemon=True)
        self._threads.append(thread)
        self._workers[name] = (target, thread)
        thread.start()
        return thread

    def _supervise(self) -> None:
        """Start again a dispatch or retry thread that stopped.

        Both loops log and go on after any Exception, but whatever escapes them
        (a failing log write, say) ends the thread while the watcher keeps the
        process alive, so launchd would never restart it. Neither loop keeps
        state of its own, so a fresh thread simply carries on.
        """
        if self._stopping:
            return
        for name, (target, thread) in list(self._workers.items()):
            if not thread.is_alive():
                _log("ERROR %s thread stopped; started it again" % name)
                self._spawn(target, name)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop taking work; join the dispatch and retry threads (tests; the daemon runs until killed)."""
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        for thread in self._threads:
            thread.join(timeout)

    def run_forever(self) -> None:
        self.start()
        while True:
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001 - the watcher must outlive any one tick
                _log("WARNING scheduler tick failed: %s" % (exc,))
                self._sleep(PAUSE_POLL_SECONDS)

    def step(self) -> None:
        """One watcher tick, then the wait before the next one (pause checked every 5 s)."""
        self.control()
        interval = self.tick()
        deadline = self._monotonic() + interval
        while not self._stopping:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            self._sleep(min(remaining, PAUSE_POLL_SECONDS))
            self.control()

    def _idle(self, seconds: float) -> None:
        """Wait up to ``seconds``; any state change (a review ending, resume, stop) wakes it."""
        with self._cond:
            if not self._stopping:
                self._cond.wait(seconds)

    # ------------------------------------------------- executor lock (leases)

    def _lease_locked(self, kind: str) -> bool:
        """Take a share of the executor lock; the caller holds ``_cond``.

        The first share acquires the lock and re-reads the state. None is
        granted while the pause sentinel exists, and none but the resume's
        own until the sweep after a pause (or startup) has run.
        """
        if self._stopping:
            return False
        if self._stop_requested():
            self._note_pause_locked()
            return False
        if self._resume_pending and kind != "resume":
            return False
        if self._leases == 0 and not self._acquire_from_idle_locked():
            return False
        self._leases += 1
        return True

    def _acquire_from_idle_locked(self) -> bool:
        try:
            self.lock.acquire()
        except Exception as exc:  # noqa: BLE001 - ExecutorBusy (a manual once) or an OSError
            if not self._busy_logged:
                _log("executor lock unavailable (%s); reviews wait until it is free" % (exc,))
                self._busy_logged = True
            return False
        if self._busy_logged:
            _log("executor lock free again")
            self._busy_logged = False
        if self._stop_requested():
            # The sentinel appeared between the check and the acquire: the
            # deploy drain must never see a new review start after it.
            self.lock.release()
            self._note_pause_locked()
            return False
        if not self._reload_state():
            self.lock.release()
            return False
        return True

    def _unlease_locked(self) -> None:
        self._leases -= 1
        if self._leases == 0:
            self.lock.release()
            if self._paused:
                _log("executor paused: nothing in flight, lock released")
        self._cond.notify_all()

    def _note_pause_locked(self) -> None:
        self._resume_pending = True
        if not self._paused:
            self._paused = True
            in_flight = len(self._in_flight)
            _log(
                "executor paused by %s: no new reviews%s"
                % (self.pause_path, "; %d in flight finish first" % in_flight if in_flight else "")
            )
            # A retry waiting for a PR's review must hear about it now.
            self._cond.notify_all()

    def _reload_state(self) -> bool:
        """Re-read state.json; the lock was just taken from idle, so nothing of ours is in flight.

        False when it cannot be read, and nothing runs under this lease: the
        copy this process holds may predate a ``once`` that ran while the lock
        was free, and working from it could post that head again and overwrite
        the ledger entries the ``once`` saved. Logged once per distinct error.
        """
        try:
            svc.migrate_legacy_state_file(self.state_path)
            loaded = svc.load_state(self.state_path)
            loaded = svc.migrate_state(loaded, self._repositories())
        except Exception as exc:  # noqa: BLE001 - a corrupt file is moved aside by load_state
            message = str(exc)
            if message != self._state_error:
                self._state_error = message
                _log("WARNING state unreadable, no review starts until it reads: %s" % message)
            return False
        if self._state_error is not None:
            self._state_error = None
            _log("state readable again")
        with svc.STATE_LOCK:
            self.state = loaded
        return True

    def control(self) -> None:
        """Pause and resume. Resuming (and starting) sweeps run dirs before any review."""
        with self._cond:
            if self._stop_requested():
                self._note_pause_locked()
                return
            # A lease still out means work from before the pause is finishing;
            # the sweep must wait until the lock is released and re-taken.
            if not self._resume_pending or self._leases:
                return
            if not self._lease_locked("resume"):
                return
        try:
            self._sweep()
        except Exception as exc:  # noqa: BLE001 - housekeeping never stops reviews
            _log("WARNING run dir sweep failed: %s" % (exc,))
        with self._cond:
            resumed = self._paused
            self._paused = False
            self._resume_pending = False
            self._unlease_locked()
        _log(
            "executor resumed" if resumed else
            "scheduler started: watching every %gs, up to %d reviews at once (%d updates)"
            % (self.watch_interval, self.hard_cap, self.update_cap)
        )

    def _repositories(self) -> List[str]:
        return svc.review_repositories(self.config, self.store, logged=self._repository_problems)

    def _work_dir(self) -> Path:
        service_root = svc.service_root_for_config(self.config_path)
        return svc.expand_path(self.config.get("work_dir", svc.LEGACY_WORK_DIR), service_root)

    def _sweep_run_dirs(self) -> None:
        svc.sweep_review_run_dirs(self._work_dir(), self.config)

    def _prune_run_dirs(self, keep: Callable[[str], bool], closed: Optional[Callable[[Path], bool]] = None) -> None:
        svc.prune_review_run_dirs(self._work_dir(), self.config, keep=keep, closed=closed)

    def _maybe_prune(self) -> None:
        """At most hourly, remove run dirs past their retention while reviews run.

        Only whole old dirs go (never a clone of a running review), and a dir of
        a PR in flight is kept whatever its age.
        """
        now = self._monotonic()
        if now - self._last_prune < PRUNE_INTERVAL_SECONDS:
            return
        with self._cond:
            if not self._lease_locked("prune"):
                return
        self._last_prune = now
        try:
            self._prune(self._run_dir_in_flight, self._run_dir_closed)
            self._forget_closed_prs()
        except Exception as exc:  # noqa: BLE001 - housekeeping never stops reviews
            _log("WARNING run dir prune failed: %s" % (exc,))
        finally:
            with self._cond:
                self._unlease_locked()

    def _note_open_prs(self, repo: str, prs: List[Dict[str, Any]]) -> None:
        """After a successful listing of ``repo``: a PR it showed open before and
        no longer shows was merged or closed. A failed or refused listing never
        gets here, so it never closes anything; a PR listed again (reopened) is
        no longer closed."""
        listed: Set[Key] = set()
        for item in prs:
            try:
                listed.add((repo.lower(), int(item["number"])))
            except (KeyError, TypeError, ValueError):
                continue
        gone = {key for key in self._open_prs if key[0] == repo.lower() and key not in listed}
        self._open_prs -= gone
        self._closed_prs = (self._closed_prs | gone) - listed
        self._open_prs |= listed

    def _run_dir_closed(self, run_dir: Path) -> bool:
        """A ``pr-<number>-*`` run dir recorded as the repository of a closed PR of
        that number that no review is running for. A run dir with no recorded
        repository (written before the record existed) waits for its retention."""
        parts = run_dir.name.split("-", 2)
        if len(parts) < 3 or parts[0] != "pr" or not parts[1].isdigit():
            return False
        number = int(parts[1])
        with self._cond:
            in_flight = set(self._in_flight)
        candidates = {key[0] for key in self._closed_prs if key[1] == number and key not in in_flight}
        if not candidates:
            return False
        return svc.run_dir_owner(run_dir) in candidates

    def _forget_closed_prs(self) -> None:
        """Drop the closed PRs with no run dir of their own left and no review
        running (one still fetching may create its run dir after this prune). One
        kept because a review still runs, or one that failed to delete, is tried
        again next prune."""
        if not self._closed_prs:
            return
        if not self.config.get("cleanup_run_dir", True):
            self._closed_prs.clear()  # the debugging opt-out keeps them; nothing to retry
            return
        # In flight first, the run dirs second: a review that ends in between
        # made its run dir before it ended, so the scan sees it.
        with self._cond:
            in_flight = set(self._in_flight)
        # The same roots the prune covers (the legacy one too, with
        # sweep_legacy_work_dir): a failed delete in any of them is retried.
        run_dirs: List[Path] = []
        for runs_dir in svc.run_dir_roots(self._work_dir(), self.config):
            try:
                if runs_dir.is_dir():
                    run_dirs.extend(runs_dir.iterdir())
            except OSError:
                return  # cannot tell what is left: forget nothing

        def left(key: Key) -> bool:
            prefix = "pr-%d-" % key[1]
            return any(path.name.startswith(prefix) and svc.run_dir_owner(path) == key[0] for path in run_dirs)

        self._closed_prs = {key for key in self._closed_prs if key in in_flight or left(key)}

    def _run_dir_in_flight(self, name: str) -> bool:
        # Run dirs are named pr-<number>-<head12>-<random> (make_review_run_dir);
        # read live, so a review that starts during the prune is covered too.
        with self._cond:
            return any(name.startswith("pr-%d-" % number) for _, number in self._in_flight)

    # ---------------------------------------------------------------- watcher

    def tick(self) -> float:
        """Re-read the config, list every review repository once and queue what needs a review.

        Returns how long to wait before the next tick: watch_interval_seconds,
        or poll_interval_seconds while the GitHub rate limit is low.
        """
        self._supervise()
        self._reload_config()
        self._maybe_reopen_store()
        if self.state is None:
            # Not started yet (or the lock was never free): nothing to compare with.
            return self.watch_interval
        repositories = self._repositories()
        self._ticks += 1
        lowest: Optional[int] = None
        status_budget = MAX_REVIEWED_STATUS_POSTS_PER_TICK
        listed: List[str] = []
        refused = False
        self._status_rate_limited = False
        for repo in repositories:
            prs, remaining = self._list_open_prs(repo)
            if remaining is not None:
                lowest = remaining if lowest is None else min(lowest, remaining)
            if prs is not None:
                # GitHub's clock at the listing, before the status POSTs move the client's on.
                self._listed_at[repo] = getattr(self._watch_clients.get(repo), "server_time", None)
                # The first sight of a repository fixes where its cursors start, even
                # when a rate limit or a busy lock holds the mention phase back.
                with svc.STATE_LOCK:
                    bucket = svc.repo_state(self.state, repo)
                    unstarted = any(mentions.read_cursor(bucket, kind) is None for kind in mentions.COMMENT_KINDS)
                if unstarted:
                    self._cursor_starts.setdefault(repo.lower(), mentions.github_time(self._github_now(repo)))
                self._absorb(repo, prs)
                self._note_open_prs(repo, prs)
                # One command's reaction and reply stay reserved for the mention phase.
                reserve = COMMAND_POSTS if self.store is not None else 0
                status_budget -= self._post_reviewed_statuses(repo, prs, status_budget - reserve)
                listed.append(repo)
                if self._status_rate_limited:
                    # The token's content-creation limit: like a refused listing,
                    # nothing more this tick and a slower watch.
                    lowest = 0
                    refused = True
                    break
            elif remaining == 0:
                # Refused for the rate limit: every repository shares the token, so
                # listing the rest now would only add refused requests.
                refused = True
                break
        # With the store down nothing could be recorded: the cursors stay put.
        if self._status_rate_limited:
            self._mentions_resume_at = self._monotonic() + self.slow_interval
        # A refused status POST means the content-creation limit: a command's
        # reaction and reply would be refused too, after its cursor was saved.
        # So no command is answered until that cooldown ends.
        cooling = self._monotonic() < self._mentions_resume_at
        if self.store is not None and listed and not refused and not cooling:
            if self._poll_all_mentions(listed, status_budget):
                lowest = 0  # same as a refused listing: the token is shared, back off
        self._forget_unconfigured(repositories)
        self._maybe_prune()
        return self._next_interval(lowest)

    def _maybe_reopen_store(self) -> None:
        """Open the dashboard store again when it was unavailable, at most every poll interval.

        The serial daemon opened it every cycle. Without this, a store that
        could not be opened at startup would stay off for the daemon's life: no
        attempt rows, no dashboard-added repositories, no operator retries.
        """
        if self.store is not None:
            return
        now = self._monotonic()
        if now - self._last_store_open < self.slow_interval:
            return
        self._last_store_open = now
        store = self._store_opener()
        if store is None:
            return
        self.store = store
        _log("dashboard store open again: attempts are recorded and operator retries run")
        if self._started:
            self._start_retry_thread()

    def _list_open_prs(self, repo: str) -> Tuple[Optional[List[Dict[str, Any]]], Optional[int]]:
        """``(open PRs, or None when the listing failed; X-RateLimit-Remaining if known)``."""
        client = self._watch_clients.get(repo)
        try:
            if client is None:
                client = self._watch_client_factory(repo, self._etag_caches.setdefault(repo, {}))
                self._watch_clients[repo] = client
            prs = client.list_open_prs()
            if not isinstance(prs, list):
                raise svc.ServiceError("open PR listing is not a list")
        except Exception as exc:  # noqa: BLE001 - one repository never stops the others
            # Read before the client goes: a 403/429 carries the exhausted limit.
            remaining = getattr(client, "rate_limit_remaining", None)
            if _rate_limit_refusal(exc):
                remaining = 0  # back off to poll_interval_seconds, as for an exhausted quota
            # A fresh client next tick: a gh token may have rotated.
            self._watch_clients.pop(repo, None)
            if repo not in self._listing_failed:
                self._listing_failed.add(repo)
                _log("%s: listing open PRs failed, retrying every tick: %s" % (repo, exc))
            return None, remaining if isinstance(remaining, int) else None
        if repo in self._listing_failed:
            self._listing_failed.discard(repo)
            _log("%s: listing open PRs works again" % repo)
        remaining = getattr(client, "rate_limit_remaining", None)
        return prs, remaining if isinstance(remaining, int) else None

    def _next_interval(self, remaining: Optional[int]) -> float:
        if remaining is not None:
            low = remaining < RATE_LIMIT_FLOOR
            if low and not self._rate_limited:
                _log(
                    "GitHub rate limit low (%d left, floor %d): listing every %gs until it recovers"
                    % (remaining, RATE_LIMIT_FLOOR, self.slow_interval)
                )
            elif not low and self._rate_limited:
                _log("GitHub rate limit recovered (%d left): listing every %gs" % (remaining, self.watch_interval))
            self._rate_limited = low
        return self.slow_interval if self._rate_limited else self.watch_interval

    def _absorb(self, repo: str, prs: List[Dict[str, Any]]) -> None:
        """Queue (or refresh, or drop) the jobs one repository listing implies."""
        with svc.STATE_LOCK:
            bucket = svc.repo_state(self.state, repo)
            baseline = svc.needs_baseline(self.config, bucket)
        if baseline:
            self._baseline(repo, prs)
            return
        review_drafts = self.review_drafts
        candidates: List[Tuple[Key, int, str, Dict[str, Any], str, bool]] = []
        skips: List[Tuple[Key, int, str, str]] = []
        listed: Set[Key] = set()
        drafts: Set[Key] = set()
        baselined_drafts: List[int] = []
        with self._cond:
            seen_draft = set(self._seen_draft)
        with svc.STATE_LOCK:
            bucket = svc.repo_state(self.state, repo)
            failed = bucket.get("failed") if isinstance(bucket.get("failed"), dict) else {}
            for pr in prs:
                try:
                    number = int(pr["number"])
                except (KeyError, TypeError, ValueError):
                    continue
                key = (repo.lower(), number)
                listed.add(key)
                head = _head(pr)
                if not head:
                    continue
                entry = bucket["reviewed"].get(str(number))
                if svc.is_draft(pr) and not review_drafts:
                    drafts.add(key)
                    if isinstance(entry, dict) and entry.get("initial_baseline_without_review"):
                        baselined_drafts.append(number)
                    continue
                if key in seen_draft and isinstance(entry, dict) and entry.get("initial_baseline_without_review"):
                    # Marked ready before its baseline entry could be cleared
                    # (the lock was busy): clear it now, review it next tick.
                    baselined_drafts.append(number)
                    continue
                if svc.head_already_reviewed(bucket, number, head):
                    continue
                skip = svc.auto_retry_skip_reason(self.config, failed.get(str(number)), head)
                if skip:
                    skips.append((key, number, head, skip))
                    continue
                kind = UPDATE if svc.ever_reviewed(bucket, number) else FIRST
                # "opened" only for a PR the service never touched: a failed or
                # baselined one has been waiting since it was seen, not created.
                may_be_opened = entry is None and str(number) not in failed
                candidates.append((key, number, head, pr, kind, may_be_opened))
        if baselined_drafts:
            self._clear_draft_baselines(repo, baselined_drafts)
        now = self._clock()
        with self._cond:
            for key in list(self._queue):
                if key[0] == repo.lower() and (key not in listed or key in drafts):
                    # Closed, merged, or back to draft: never review it.
                    del self._queue[key]
            for key in list(self._seen_draft):
                if key[0] == repo.lower() and key not in listed:
                    self._seen_draft.discard(key)
            self._seen_draft.update(drafts)
            for key, number, head, pr, kind, may_be_opened in candidates:
                was_draft = key in self._seen_draft
                self._seen_draft.discard(key)
                self._skip_logged.pop(key, None)
                if key in self._in_flight:
                    # The running review finishes first; the next tick queues
                    # this head if it still needs one.
                    continue
                queued = self._queue.get(key)
                if queued is not None:
                    queued.pr, queued.head, queued.kind = pr, head, kind
                    continue
                created_at = _created_at(pr)
                if kind == FIRST and was_draft:
                    origin, origin_at = "ready", now
                elif kind == FIRST and may_be_opened and created_at is not None:
                    origin, origin_at = "opened", created_at
                else:
                    origin, origin_at = "seen", now
                self._seq += 1
                self._queue[key] = Job(repo, number, head, pr, kind, self._seq, origin, origin_at, self._ticks)
            fresh_skips = [
                item for item in skips if self._skip_logged.get(item[0]) != (item[2], item[3])
            ]
            for key, _, head, reason in fresh_skips:
                self._skip_logged[key] = (head, reason)
            for key in list(self._skip_logged):
                if key[0] == repo.lower() and key not in listed:
                    del self._skip_logged[key]
            self._cond.notify_all()
        for _, number, _, reason in fresh_skips:
            _log("%s pr #%d: skipping, %s" % (repo, number, reason))

    def _baseline(self, repo: str, prs: List[Dict[str, Any]]) -> None:
        """First sight of a repository: record its open PRs, review none of them this tick."""
        with self._cond:
            if not self._lease_locked("baseline"):
                return
        try:
            with svc.STATE_LOCK:
                bucket = svc.repo_state(self.state, repo)
                # Re-checked: taking the lock may have re-read the state.
                if svc.needs_baseline(self.config, bucket):
                    svc.record_baseline(self.config, self.state, self.state_path, repo, bucket, prs)
        except Exception as exc:  # noqa: BLE001 - retried next tick
            _log("%s: WARNING baseline failed: %s" % (repo, exc))
        finally:
            with self._cond:
                self._unlease_locked()

    def _clear_draft_baselines(self, repo: str, numbers: List[int]) -> None:
        """Drop the baseline entries of PRs that are drafts now.

        A baseline recorded before drafts were left out holds a draft's head as
        seen; without this, marking it ready would never start its review.
        """
        with self._cond:
            if not self._lease_locked("ledger"):
                return  # next tick
        cleared: List[int] = []
        try:
            with svc.STATE_LOCK:
                reviewed = svc.repo_state(self.state, repo)["reviewed"]
                for number in numbers:
                    entry = reviewed.get(str(number))
                    if isinstance(entry, dict) and entry.get("initial_baseline_without_review"):
                        del reviewed[str(number)]
                        cleared.append(number)
                if cleared:
                    svc.save_state(self.state_path, self.state)
        except Exception as exc:  # noqa: BLE001 - retried next tick
            _log("%s: WARNING clearing baselined drafts failed: %s" % (repo, exc))
        finally:
            with self._cond:
                self._unlease_locked()
        for number in cleared:
            _log("%s pr #%d: draft was in the baseline; cleared, so marking it ready starts its review" % (repo, number))

    def _forget_unconfigured(self, repositories: List[str]) -> None:
        configured = {repo.lower() for repo in repositories}
        with self._cond:
            for key in list(self._queue):
                if key[0] not in configured:
                    del self._queue[key]
            self._seen_draft = {key for key in self._seen_draft if key[0] in configured}
            self._skip_logged = {key: value for key, value in self._skip_logged.items() if key[0] in configured}
        self._reviewed_status_backoff = {
            key: value for key, value in self._reviewed_status_backoff.items() if key in configured
        }
        self._reviewed_status_failing = {key for key in self._reviewed_status_failing if key in configured}
        self._mentions_failing = {key for key in self._mentions_failing if key[0] in configured}
        self._mention_turns = [repo for repo in self._mention_turns if repo in configured]
        for repo in list(self._watch_clients):
            if repo.lower() not in configured:
                self._watch_clients.pop(repo, None)
                self._etag_caches.pop(repo, None)

    def _reviewed_status_due(
        self, repo: str, prs: List[Dict[str, Any]]
    ) -> Tuple[List[Tuple[int, str, str, Dict[str, Any]]], List[str]]:
        """``(due, gone)`` for one complete listing; the caller holds STATE_LOCK.

        ``due``: (number, head, last reviewed head, payload) of each open,
        non-draft PR with a real review whose base and head the reviewed status
        was not set on yet. ``gone``: ledger keys of PRs no longer open.
        """
        bucket = svc.repo_state(self.state, repo)
        set_on = svc.reviewed_status_heads(bucket)
        listed: Set[str] = set()
        due: List[Tuple[int, str, str, Dict[str, Any]]] = []
        for pr in prs:
            try:
                number = int(pr["number"])
            except (KeyError, TypeError, ValueError):
                continue
            listed.add(str(number))
            head = _head(pr)
            if not head or svc.is_draft(pr) or not svc.ever_reviewed(bucket, number):
                continue
            if set_on.get(str(number)) == svc.reviewed_status_key(pr):
                continue
            last = str(bucket["reviewed"][str(number)].get("head_sha") or "")
            due.append((number, head, last, pr))
        return due, [key for key in set_on if key not in listed]

    def _post_reviewed_statuses(self, repo: str, prs: List[Dict[str, Any]], budget: int) -> int:
        """Set the reviewed status on the listed PRs that need it; returns the POSTs made.

        The one-time backfill of PRs reviewed before the status existed, every
        new head of a reviewed PR, which must not wait for its own update
        review, and a reviewed PR moved to another base. Each base and head is
        recorded in the ledger once set, so it is not set again. Up to
        ``budget`` POSTs, with the watcher's GitHub client. A failed POST stops
        this repository's POSTs for poll_interval_seconds (one WARNING until a
        batch ends without a refusal), so a lasting refusal cannot spend the
        shared token every tick.
        """
        if not svc.reviewed_status_enabled(self.config):
            return 0
        resume = self._reviewed_status_backoff.get(repo.lower())
        backing_off = resume is not None and self._monotonic() < resume
        if backing_off:
            budget = 0
        with svc.STATE_LOCK:
            due, gone = self._reviewed_status_due(repo, prs)
        if not gone and (not due or budget <= 0):
            if not due and not backing_off:
                # Nothing left to set (the PRs it failed on closed, say).
                self._reviewed_status_settled(repo, posted=False)
            return 0
        client = self._watch_clients.get(repo)
        if client is None:
            return 0
        # A ledger write: under a lease of the executor lock, like the baseline.
        with self._cond:
            if not self._lease_locked("reviewed-status"):
                return 0  # paused, or a once holds the lock: next tick
        posts = 0
        set_on: List[Tuple[int, str, str, str, str]] = []
        refused = False  # a POST of this batch failed: the backoff it set stands
        try:
            with svc.STATE_LOCK:
                # Again: taking the lock from idle re-reads the state.
                due, gone = self._reviewed_status_due(repo, prs)
            for number, head, last, pr in due[: max(budget, 0)]:
                posts += 1
                context = svc.reviewed_status_context(pr)
                try:
                    client.create_commit_status(
                        head,
                        "success",
                        svc.status_description(
                            "reviewed at least once (last review %s); this head's own review runs separately"
                            % last[:12]
                        ),
                        svc.required_status_target(self.config, None, pr),
                        context=context,
                    )
                except Exception as exc:  # noqa: BLE001 - one status never stops the watcher
                    refused = True
                    if _rate_limit_refusal(exc):
                        self._status_rate_limited = True
                    self._reviewed_status_backoff[repo.lower()] = self._monotonic() + self.slow_interval
                    if repo.lower() not in self._reviewed_status_failing:
                        self._reviewed_status_failing.add(repo.lower())
                        _log(
                            "%s pr #%d: WARNING %s status on %s failed; no reviewed statuses for this "
                            "repository for %gs at a time until one works: %s"
                            % (repo, number, context, head[:12], self.slow_interval, exc)
                        )
                    break
                set_on.append((number, head, last, svc.reviewed_status_key(pr), context))
            if set_on or gone:
                with svc.STATE_LOCK:
                    bucket = svc.repo_state(self.state, repo)
                    for number, _, _, key, _ in set_on:
                        svc.record_reviewed_status(bucket, number, key)
                    heads = svc.reviewed_status_heads(bucket)
                    for key in gone:
                        heads.pop(key, None)
                    svc.save_state(self.state_path, self.state)
        except Exception as exc:  # noqa: BLE001 - retried next tick
            _log("%s: WARNING setting the reviewed status failed: %s" % (repo, exc))
        finally:
            with self._cond:
                self._unlease_locked()
        # Only a batch that ended without a refusal settles: an earlier success
        # in a batch whose later POST failed must not lift its backoff.
        if not refused and not backing_off and (posts or not due):
            self._reviewed_status_settled(repo, posted=bool(set_on))
        for number, head, last, _, context in set_on:
            _log("%s pr #%d: %s success on %s (last review %s)" % (repo, number, context, head[:12], last[:12]))
        return posts

    def _reviewed_status_settled(self, repo: str, posted: bool) -> None:
        """A batch for ``repo`` ended without a refusal, past any backoff: it is not failing now.

        The recovery is logged only when a POST of the batch succeeded; a batch
        with nothing to set clears it silently, so the next failure there logs
        its WARNING again.
        """
        key = repo.lower()
        self._reviewed_status_backoff.pop(key, None)
        if key in self._reviewed_status_failing:
            self._reviewed_status_failing.discard(key)
            if posted:
                _log("%s: reviewed status POSTs work again" % repo)

    # ---------------------------------------------------------------- mentions

    def _poll_all_mentions(self, listed: List[str], status_budget: int) -> bool:
        """Answer the listed repositories' new commands; True when GitHub refused for a rate limit.

        Each command costs a reaction and a reply, two content POSTs like a
        status, so the commands get what the statuses left of the tick's
        budget; the statuses leave one command's POSTs (COMMAND_POSTS), so it is
        never less than one. The repositories take turns going first, so one
        busy repository cannot hold the budget every tick.

        A review command also reads the live PR once (GET). That read does not
        consume this content-POST budget; the client tracks GitHub's read limit.
        """
        budget = min(mentions.MAX_COMMANDS_PER_TICK, status_budget // COMMAND_POSTS)
        # Turns are kept across phases, whichever repositories a phase lists: the
        # one that goes first now goes last next time (a skipped phase is no turn).
        # A repository not seen before has had no turn: it goes before the others.
        new = [repo.lower() for repo in listed if repo.lower() not in self._mention_turns]
        turns = self._mention_turns = new + self._mention_turns
        ordered = sorted(listed, key=lambda repo: turns.index(repo.lower()))
        turns.remove(ordered[0].lower())
        turns.append(ordered[0].lower())
        for repo in ordered:
            answered, refused = self._poll_mentions(repo, budget)
            budget -= answered
            if refused:
                return True
        return False

    def _poll_mentions(self, repo: str, budget: int) -> Tuple[int, bool]:
        """Read ``repo``'s new PR comments and answer the ``@review-of-legends`` commands in them.

        Returns (commands answered, refused for a rate limit). Both listings go
        through the watch client from cursors in the repository's ledger
        (mentions.py), so a repository with no new comment costs two 304s and
        takes no lock. The first sight of a repository starts its cursors now,
        whatever is left of the budget: earlier comments are never answered.
        Nothing is answered unless both listings were read, so the two kinds
        stay in one order; a failed listing is logged once and retried next tick.
        """
        client = self._watch_clients.get(repo)
        if client is None:
            return 0, False
        with svc.STATE_LOCK:
            bucket = svc.repo_state(self.state, repo)
            missing = [kind for kind in mentions.COMMENT_KINDS if mentions.read_cursor(bucket, kind) is None]
        if missing:
            # The time is fixed at the first attempt, before the lease: a busy
            # lock or a failed save delays the start, never moves it later.
            self._cursor_starts.setdefault(repo.lower(), mentions.github_time(self._github_now(repo)))
            if not self._start_mention_cursors(repo):
                return 0, False
        if budget <= 0:
            return 0, False  # the commands wait for a later tick's turn
        listings: Dict[str, List[Any]] = {}
        for kind in mentions.COMMENT_KINDS:
            with svc.STATE_LOCK:
                cursor = mentions.read_cursor(svc.repo_state(self.state, repo), kind)
                since = str(cursor["since"]) if cursor else ""
            if not since:
                return 0, False  # the state was re-read without it: started again next tick
            failing = (repo.lower(), kind)
            try:
                if kind == mentions.REVIEW:
                    listings[kind] = client.list_repo_review_comments(since)
                else:
                    listings[kind] = client.list_repo_issue_comments(since)
            except Exception as exc:  # noqa: BLE001 - one repository never stops the others
                if failing not in self._mentions_failing:
                    self._mentions_failing.add(failing)
                    _log("%s: reading %s comments for @%s commands failed, retrying every tick: %s"
                         % (repo, kind, mentions.BOT_NAME, exc))
                if _rate_limit_refusal(exc):
                    return 0, True
                continue
            if failing in self._mentions_failing:
                self._mentions_failing.discard(failing)
                _log("%s: reading %s comments works again" % (repo, kind))
        if len(listings) < len(mentions.COMMENT_KINDS):
            return 0, False  # answering one kind alone could apply a newer command first
        with svc.STATE_LOCK:
            # Decides only whether to take the lease: nothing new, no lock.
            bucket = svc.repo_state(self.state, repo)
            quiet = not any(
                mentions.pending(mentions.read_cursor(bucket, kind) or {}, listings[kind])
                for kind in mentions.COMMENT_KINDS
            )
        if quiet:
            return 0, False
        return self._answer_mentions(repo, client, listings, budget)

    def _start_mention_cursors(self, repo: str) -> bool:
        """Start the repository's missing comment cursors at now, under a lease (a ledger write).

        "Now" is fixed at the first attempt: a save that fails is retried with
        the same time, never a later one.
        """
        with self._cond:
            if not self._lease_locked("mentions"):
                return False  # paused, or a once holds the lock: next tick
        try:
            with svc.STATE_LOCK:
                # Again: taking the lock from idle re-reads the state.
                bucket = svc.repo_state(self.state, repo)
                now = self._cursor_starts.setdefault(repo.lower(), mentions.github_time(self._github_now(repo)))
                started = [
                    kind for kind in mentions.COMMENT_KINDS if mentions.read_cursor(bucket, kind) is None
                ]
                for kind in started:
                    mentions.start_cursor(bucket, kind, now)
                if started:
                    try:
                        svc.save_state(self.state_path, self.state)
                    except Exception:
                        # Not kept in memory either: the next watch starts them again
                        # (the same time) and saves, so a restart cannot start them later.
                        for kind in started:
                            bucket.get("mentions", {}).pop(kind, None)
                        if not bucket.get("mentions"):
                            bucket.pop("mentions", None)
                        raise
                self._cursor_starts.pop(repo.lower(), None)
        except Exception as exc:  # noqa: BLE001 - retried next tick
            _log("%s: WARNING starting the comment cursors failed: %s" % (repo, exc))
            return False
        finally:
            with self._cond:
                self._unlease_locked()
        if started:
            _log("%s: reading PR comments for @%s commands from %s on" % (repo, mentions.BOT_NAME, now))
        return True

    def _github_now(self, repo: str) -> float:
        """GitHub's time, as near as the watcher knows it: where a new cursor starts.

        The Date of this tick's PR listing (the watch client's clock as it
        answered, before any status POST): GitHub's own clock, which stamps the
        comments the cursor is compared with. Without one, the daemon's clock
        less CURSOR_START_MARGIN_SECONDS.
        """
        server = self._listed_at.get(repo)
        if isinstance(server, (int, float)) and not isinstance(server, bool) and server > 0:
            return float(server)
        return self._clock() - CURSOR_START_MARGIN_SECONDS

    def _answer_mentions(
        self, repo: str, client: Any, listings: Dict[str, List[Any]], budget: int
    ) -> Tuple[int, bool]:
        """Answer the commands in both listings, one order for both: oldest update first.

        Returns (commands answered, refused for a rate limit). Every comment
        read moves its kind's cursor (mentions.advance), a command only once it
        is answered or passed over, so a command left for the next tick (the
        budget is spent, or the store refused it) is read again then, with
        everything after it. A listing cut short (``truncated``) ends the order
        at its last comment: the other kind waits past that time.

        Each command is recorded in the store, then the ledger saved, then the
        reaction and the reply sent. A ledger that cannot be saved sends
        neither: the comment is unread again, in memory as on disk, and the
        next tick applies it again, which changes nothing in the store
        (mentions.apply_command). A crash after the save and before the
        acknowledgement loses the acknowledgement; it is never sent twice (at
        most once, on purpose). A failed reaction or reply is logged, not
        retried; one refused for a rate limit ends the mention work of this
        watch, the command applied and saved.
        """
        with self._cond:
            if not self._lease_locked("mentions"):
                return 0, False  # paused, or a once holds the lock: next tick
        answered = 0
        unsaved = False
        refused = False
        try:
            try:
                bot_login = str(client.bot_login() or "")
            except Exception as exc:  # noqa: BLE001 - bots are still told apart by their type
                if _rate_limit_refusal(exc):
                    # The token is refused: so would the reactions and replies be,
                    # after their cursors were saved. Nothing is answered this watch.
                    _log("%s: GitHub refused the bot's login for a rate limit; @%s commands wait: %s"
                         % (repo, mentions.BOT_NAME, exc))
                    refused = True
                    return answered, refused
                bot_login = ""
            # Under the lease: taking it from idle re-reads the state, so the
            # cursors are read again here, and again for each comment below.
            with svc.STATE_LOCK:
                bucket = svc.repo_state(self.state, repo)
                items = mentions.merged(
                    {kind: (mentions.read_cursor(bucket, kind) or {}, listings[kind]) for kind in listings}
                )
            for kind, comment in items:
                with svc.STATE_LOCK:
                    cursor = mentions.read_cursor(svc.repo_state(self.state, repo), kind)
                    if cursor is None:
                        break
                    if not mentions.pending(cursor, [comment]):
                        continue  # read meanwhile
                    handled = mentions.is_answered(cursor, comment) or mentions.is_history(cursor, comment)
                request = None if handled else mentions.request_from(comment, kind, bot_login)
                if request is None:
                    with svc.STATE_LOCK:
                        mentions.advance(cursor, comment)
                        unsaved = True
                    continue
                if answered >= budget:
                    break  # waits for the next tick, and so does everything after it
                try:
                    edits = client.comment_edits(request.node_id) if request.node_id else None
                except Exception as exc:  # noqa: BLE001 - read again next tick, with everything after it
                    failing = (repo.lower(), "edits")
                    if failing not in self._mentions_failing:
                        self._mentions_failing.add(failing)
                        _log("%s: reading a comment's edit history failed; @%s commands wait: %s"
                             % (repo, mentions.BOT_NAME, exc))
                    if _rate_limit_refusal(exc):
                        refused = True
                    break
                self._mentions_failing.discard((repo.lower(), "edits"))
                mismatch = (repo.lower(), kind, request.comment_id)
                if not mentions.same_comment(edits, comment):
                    # Changed since it was listed: the next listing brings it back as it is now.
                    revision = (str(comment.get("updated_at") or ""), str(comment.get("body") or ""), request.login)
                    last, count = self._snapshot_mismatches.get(mismatch, (revision, 0))
                    count = count + 1 if last == revision else 1  # an author still editing starts over
                    if count < MAX_SNAPSHOT_MISMATCHES:
                        self._snapshot_mismatches[mismatch] = (revision, count)
                        _log("%s pr #%d: @%s %s from %s changed while it was read; read again next tick"
                             % (repo, request.number, mentions.BOT_NAME, request.command.kind, request.login))
                        break
                    self._snapshot_mismatches.pop(mismatch, None)
                    _log("%s pr #%d: @%s %s from %s passed over: GitHub's copy differed from the listing"
                         " %d watches in a row" % (repo, request.number, mentions.BOT_NAME, request.command.kind,
                                                   request.login, count))
                    with svc.STATE_LOCK:
                        mentions.mark_answered(cursor, comment)
                        unsaved = True
                    continue
                self._snapshot_mismatches.pop(mismatch, None)
                if mentions.edited_by_others(edits, request.login):
                    # GitHub keeps the author when a collaborator edits: never theirs to answer.
                    _log("%s pr #%d: @%s %s from %s passed over: edited by someone else"
                         % (repo, request.number, mentions.BOT_NAME, request.command.kind, request.login))
                    with svc.STATE_LOCK:
                        mentions.mark_answered(cursor, comment)
                        unsaved = True
                    continue
                body = ""
                try:
                    body = mentions.apply_command(
                        self.store, repo, request, self.config,
                        review=lambda command: author_review.apply(
                            self.store, repo, command, client, self._ledger_lookup,
                        ),
                    )
                except ValueError as exc:
                    # The store refuses this comment's data, and would every
                    # time: pass it, or it would hold back everything after it.
                    _log(
                        "%s pr #%d: WARNING @%s %s from %s not recorded, passed over: %s"
                        % (repo, request.number, mentions.BOT_NAME, request.command.kind, request.login, exc)
                    )
                    with svc.STATE_LOCK:
                        mentions.mark_answered(cursor, comment)
                        unsaved = True
                    continue
                except Exception as exc:  # noqa: BLE001 - read again next tick
                    refused = _rate_limit_refusal(exc)
                    _log(
                        "%s pr #%d: WARNING recording @%s %s from %s failed; tried again next tick: %s"
                        % (repo, request.number, mentions.BOT_NAME, request.command.kind, request.login, exc)
                    )
                    break
                with svc.STATE_LOCK:
                    before = mentions.snapshot(cursor)
                    mentions.mark_answered(cursor, comment)
                    unsaved = not self._save_ledger(repo)
                    if unsaved:
                        # Unread again: a later save must not record it as answered.
                        mentions.restore(cursor, before)
                if unsaved:
                    _log(
                        "%s pr #%d: @%s %s from %s recorded but not acknowledged; answered next tick"
                        % (repo, request.number, mentions.BOT_NAME, request.command.kind, request.login)
                    )
                    break  # a later comment would move the cursor past this one
                answered += 1
                _log(
                    "%s pr #%d: @%s %s from %s"
                    % (repo, request.number, mentions.BOT_NAME, request.command.kind, request.login)
                )
                try:
                    mentions.acknowledge(client, request, body, _log, repo, refused=_rate_limit_refusal)
                except Exception as exc:  # noqa: BLE001 - acknowledge raises only a rate-limit refusal
                    _log(
                        "%s: GitHub refused an @%s acknowledgement for a rate limit; the other commands"
                        " wait for a later watch: %s" % (repo, mentions.BOT_NAME, exc)
                    )
                    refused = True
                    break
            if unsaved:
                with svc.STATE_LOCK:
                    self._save_ledger(repo)
        except Exception as exc:  # noqa: BLE001 - retried next tick
            _log("%s: WARNING answering @%s commands failed: %s" % (repo, mentions.BOT_NAME, exc))
        finally:
            with self._cond:
                self._unlease_locked()
        return answered, refused

    def _save_ledger(self, repo: str) -> bool:
        try:
            svc.save_state(self.state_path, self.state)
        except OSError as exc:
            _log("%s: WARNING saving the comment cursor failed: %s" % (repo, exc))
            return False
        return True

    # ---------------------------------------------------------------- dispatch

    def _dispatcher(self) -> None:
        """Start every job the caps allow, the moment they allow it, each on its own thread."""
        while True:
            with self._cond:
                job = None
                while job is None:
                    if self._stopping:
                        return
                    try:
                        job = self._take_job_locked()
                    except Exception as exc:  # noqa: BLE001 - the dispatcher never dies
                        _log("WARNING review dispatch failed: %s" % (exc,))
                    if job is None:
                        self._cond.wait(PAUSE_POLL_SECONDS)
            try:
                threading.Thread(
                    target=self._run_job, args=(job,), name="review-%s#%d" % (job.repo, job.number), daemon=True
                ).start()
            except Exception as exc:  # noqa: BLE001 - e.g. no thread left; the next tick queues it again
                _log("%s pr #%d: WARNING could not start the review: %s" % (job.repo, job.number, exc))
                self._finish_job(job)

    def _take_job_locked(self) -> Optional[Job]:
        """The next job the caps allow, with its lease taken; the caller holds ``_cond``."""
        if self._exclusive:
            return None
        leased = False
        try:
            while True:
                job = self._next_candidate_locked()
                if job is None:
                    return None
                if not leased:
                    if not self._lease_locked("pool"):
                        return None
                    leased = True
                del self._queue[job.key]
                if self._stale_reason(job):
                    continue
                # Classified against the ledger the watcher held; taking the lease
                # may have re-read it (a manual once reviewed the PR meanwhile).
                kind = self._ledger_kind(job.repo, job.number)
                if kind != job.kind:
                    job.kind = kind
                    if kind == UPDATE and self._pool_busy[UPDATE] >= self.update_cap:
                        self._queue[job.key] = job  # waits for an update slot
                        continue
                self._in_flight[job.key] = job.kind
                self._pool_busy[job.kind] += 1
                leased = False  # the job owns the lease now
                return job
        finally:
            if leased:
                self._unlease_locked()

    def _next_candidate_locked(self) -> Optional[Job]:
        if len(self._in_flight) >= self.hard_cap:
            self._warn_capped_locked()
            return None
        updates_allowed = self._pool_busy[UPDATE] < self.update_cap
        best: Optional[Job] = None
        for job in self._queue.values():
            if job.key in self._in_flight:
                continue
            if job.kind == UPDATE and not updates_allowed:
                continue
            if best is None or job.rank() < best.rank():
                best = job
        return best

    def _warn_capped_locked(self) -> None:
        for job in self._queue.values():
            if job.kind == FIRST and not job.cap_warned and job.key not in self._in_flight:
                job.cap_warned = True
                _log(
                    "WARNING %s pr #%d waits: %d reviews already running (max_concurrent_reviews)"
                    % (job.repo, job.number, self.hard_cap)
                )

    def _stale_reason(self, job: Job) -> str:
        """Why a queued job must not run now, judged on its queued payload and the state now."""
        if svc.is_draft(job.pr) and not self.review_drafts:
            return "draft"
        return self._ledger_reason(job.repo, job.number, job.head)

    def _ledger_reason(self, repo: str, number: int, head: str) -> str:
        with svc.STATE_LOCK:
            bucket = svc.repo_state(self.state, repo)
            if svc.head_already_reviewed(bucket, number, head):
                return "%s already reviewed" % head[:12]
            failed = bucket.get("failed")
            return svc.auto_retry_skip_reason(
                self.config, failed.get(str(number)) if isinstance(failed, dict) else None, head
            )

    def _ledger_kind(self, repo: str, number: int) -> str:
        with svc.STATE_LOCK:
            return UPDATE if svc.ever_reviewed(svc.repo_state(self.state, repo), number) else FIRST

    def _live_reason(self, pr: Any) -> str:
        """Why the PR as GitHub has it now must not be reviewed ("" = review it)."""
        if not isinstance(pr, dict) or not pr:
            return "GitHub returned no pull request"
        if pr.get("merged") or pr.get("merged_at"):
            return "merged"
        state = str(pr.get("state") or "")
        if state != "open":
            return "%s, not open" % (state or "state unknown")
        if svc.is_draft(pr) and not self.review_drafts:
            return "back to draft"
        if not _head(pr):
            return "no head sha"
        return ""

    def _refresh(self, job: Job) -> bool:
        """Read the PR again just before its review; False drops the job (the next tick re-checks)."""
        try:
            client = self._client_factory(job.repo)
            fresh = client.get_pr(job.number)
        except Exception as exc:  # noqa: BLE001 - the next tick queues it again
            _log("%s pr #%d: not started, re-reading the PR failed: %s" % (job.repo, job.number, exc))
            return False
        head = _head(fresh)
        reason = self._live_reason(fresh) or self._ledger_reason(job.repo, job.number, head)
        if reason:
            _log("%s pr #%d: not started, %s" % (job.repo, job.number, reason))
            if svc.is_draft(fresh):
                with self._cond:
                    self._seen_draft.add(job.key)
            return False
        if head != job.head:
            _log("%s pr #%d: head moved to %s since it was queued; reviewing that" % (job.repo, job.number, head[:12]))
        job.pr, job.head, job.client = fresh, head, client
        return True

    def _halt_reason(self) -> str:
        """Why nothing may start now ("" = go): checked after network I/O, right before a review begins."""
        with self._cond:
            if self._stopping:
                return "the scheduler is stopping"
            if self._stop_requested():
                self._note_pause_locked()
                return "executor paused by %s" % self.pause_path
        return ""

    def _run_job(self, job: Job) -> None:
        try:
            if self._refresh(job):
                # The read above can take seconds: a pause or stop that came
                # meanwhile must still keep this review from starting.
                halted = self._halt_reason()
                if halted:
                    job.halted = True
                    _log("%s pr #%d: not started, %s; queued again" % (job.repo, job.number, halted))
                else:
                    self._log_dispatch(job)
                    self._review_job(job)
        except Exception as exc:  # noqa: BLE001 - a job never takes the scheduler down
            _log("%s pr #%d: WARNING review job failed: %s" % (job.repo, job.number, exc))
        finally:
            self._finish_job(job)

    def _finish_job(self, job: Job) -> None:
        with self._cond:
            self._in_flight.pop(job.key, None)
            self._pool_busy[job.kind] -= 1
            if job.halted and job.key not in self._queue:
                # Back where it was, so it starts as soon as the executor
                # resumes (a newer listing would have replaced it anyway).
                job.halted, job.client = False, None
                self._queue[job.key] = job
            self._unlease_locked()

    def _log_dispatch(self, job: Job) -> None:
        waited = max(0.0, self._clock() - job.origin_at)
        _log(
            "dispatch %s pr #%d %s (%s) %.0fs after %s"
            % (job.repo, job.number, job.head[:12], job.kind, waited, job.origin)
        )

    def _run_review(self, job: Job) -> None:
        """A pool review: the same per-PR body ``once`` runs, on the payload read at dispatch."""
        client = job.client if job.client is not None else self._client_factory(job.repo)
        config = self.config
        with svc.STATE_LOCK:
            state = self.state
            bucket = svc.repo_state(state, job.repo)
        svc.run_poll_review(
            client, config, self.config_path, job.pr, job.repo, bucket, state, self.state_path, self.store
        )

    # ---------------------------------------------------------------- retries

    def _retry_loop(self) -> None:
        while not self._stopping:
            try:
                self._retry_deferred = False
                ran = self._drain_retries()
                if self._stopping:
                    return
                if self._retry_deferred:
                    # The requeue already woke the waiter; back off instead of re-claiming at once.
                    self._idle(PAUSE_POLL_SECONDS)
                else:
                    # Woken within a second by a new request; while paused, re-checked every 5 s.
                    self._waiter.wait(self.slow_interval if ran else PAUSE_POLL_SECONDS)
            except Exception as exc:  # noqa: BLE001 - the retry thread outlives any one drain
                _log("WARNING retry thread: %s" % (exc,))
                self._idle(PAUSE_POLL_SECONDS)

    def _drain_retries(self) -> bool:
        """Consume queued operator retries; False when the lock could not be taken."""
        exclusive = False
        leased = False
        try:
            with self._cond:
                if dashboard_execution.recovery_pending():
                    # The drain retries a failed startup recovery, which marks every
                    # running attempt interrupted: no pool review may be in flight.
                    exclusive = self._exclusive = True
                    while sum(self._pool_busy.values()) and not self._stopping:
                        self._cond.wait(PAUSE_POLL_SECONDS)
                leased = self._lease_locked("retry")
            if not leased:
                return False
            try:
                dashboard_execution.drain_retry_requests(
                    self.store,
                    self.lock,
                    self._repositories(),
                    self._client_factory,
                    self._retry_review_runner,
                    self._ledger_lookup,
                    logger=_log,
                    pause_path=self.pause_path,
                )
            except Exception as exc:  # noqa: BLE001 - the queue never stops the scheduler
                _log("WARNING retry queue drain failed: %s" % (exc,))
            return True
        finally:
            # Also when taking the lease raised: a stuck _exclusive would stop all dispatch.
            with self._cond:
                if exclusive:
                    self._exclusive = False
                if leased:
                    self._unlease_locked()
                elif exclusive:
                    self._cond.notify_all()

    def _defer_retry(self, reason: str) -> dashboard_execution.RetryDeferred:
        self._retry_deferred = True
        return dashboard_execution.RetryDeferred(reason)

    def _retry_review_runner(
        self,
        client: Any,
        pr: Dict[str, Any],
        recorder: Optional[dashboard_execution.DashboardRecorder] = None,
        force: bool = False,
        model_policy: Optional[Dict[str, Any]] = None,
        reuse_results: Optional[Dict[str, Any]] = None,
        rereview: bool = False,
    ) -> bool:
        number = int(pr["number"])
        key = (str(client.repo).lower(), number)
        with self._cond:
            while True:
                if self._stopping:
                    raise self._defer_retry("the scheduler is stopping")
                if self._stop_requested():
                    self._note_pause_locked()
                    raise self._defer_retry("executor paused by %s" % self.pause_path)
                # Never next to a review of the same PR; and a retry counts
                # toward the hard cap like any other review.
                if key not in self._in_flight and len(self._in_flight) < self.hard_cap:
                    break
                self._cond.wait(PAUSE_POLL_SECONDS)
            self._in_flight[key] = RETRY
        try:
            requested = _head(pr)
            try:
                fresh = client.get_pr(number)
            except Exception as exc:  # noqa: BLE001 - re-validated on the next claim
                raise self._defer_retry("re-reading the PR failed: %s" % exc)
            invalid = dashboard_execution._pr_validation_failure({"head_sha": requested}, fresh)
            if invalid is not None:
                # The next claim validates it again and closes it as stale or failed.
                raise self._defer_retry("the PR changed: %s" % invalid[1])
            config = self.config
            with svc.STATE_LOCK:
                state = self.state
                bucket = svc.repo_state(state, client.repo)
                reviewed_meanwhile = (
                    force
                    and not rereview
                    and svc.head_already_reviewed(bucket, number, requested)
                    and svc.ever_reviewed(bucket, number)
                )
            if reviewed_meanwhile:
                # force was granted for a baseline head; the pool has since
                # posted (or parked) a real review of it. Never post twice. A
                # rereview is the one request whose point is a reviewed head.
                _log("%s pr #%d: retry not run, %s was reviewed meanwhile" % (client.repo, number, requested[:12]))
                return False
            if rereview:
                # retry_decision's allow was decided at claim time; this
                # request may then have waited behind the hard cap or a
                # running pool attempt before reaching here. Re-derive the
                # same decision from fresh data now that the rereview owns
                # the PR (this ``_in_flight`` reservation), so a pool attempt
                # that completed meanwhile with an unknown posting outcome, or
                # an unverified ledger POST recorded meanwhile, still refuses
                # -- with the same reason text -- instead of posting a second
                # review beside one GitHub may not have actually received.
                # The click asked for a fresh review of the head as the page
                # showed it (``retry_of``); a pool review that took the PR
                # first and posted meanwhile already is that review.
                # A refusal (RetryRefused) is logged and recorded on the request
                # by _execute_retry_request.
                dashboard_execution.rereview_late_check(
                    self.store,
                    client.repo,
                    number,
                    requested,
                    recorder.retry_of if recorder is not None else None,
                    self._ledger_lookup,
                    recorder.seen_at if recorder is not None else None,
                )
            # The read above can take seconds: a pause or stop that came
            # meanwhile sends the request back to the queue, attempt unbegun.
            halted = self._halt_reason()
            if halted:
                raise self._defer_retry(halted)
            return svc.review_pr(
                client,
                config,
                self.config_path,
                fresh,
                bucket,
                dry_run=False,
                force=force,
                state_path=self.state_path,
                full_state=state,
                recorder=recorder,
                model_policy=model_policy,
                # Checked against the head of ``fresh`` inside review_pr.
                reuse_results=reuse_results,
                rereview=rereview,
            )
        finally:
            with self._cond:
                self._in_flight.pop(key, None)
                self._cond.notify_all()

    def _ledger_lookup(self, repository: str, number: int) -> Optional[Dict[str, Any]]:
        with svc.STATE_LOCK:
            entry = svc.repo_state(self.state, repository)["reviewed"].get(str(number))
            return dict(entry) if isinstance(entry, dict) else None
