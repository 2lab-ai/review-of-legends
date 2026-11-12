"""The daemon's scheduler (code_review_service/scheduler.py).

A PR's review must start within a tick of the PR being opened or marked ready,
even while other reviews run: a first review starts unless the hard cap
(max_concurrent_reviews, retries included) is reached, and updates never
exceed their own cap. Drafts wait, also through a baseline. The PR is re-read
from GitHub just before its review. The pool and the retry thread never review
one PR at the same time, a retry that cannot start goes back to the queue, the
pause sentinel drains the pool before the executor lock is released, and
concurrent reviews never corrupt state.json.

Real threads run fake review jobs; GitHub, the clock and the lock directory
are fakes or temp files. Nothing here touches the network.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.message
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import dashboard_execution as dx
from code_review_service import scheduler as sched_mod
from code_review_service import service as svc
from code_review_service.dashboard_store import DashboardStore
from tests.test_dashboard_integration import FakeClient, FakeStore, _config, _stub_agent, _stub_clone

CONFIG_PATH = ROOT / "config" / "reviewer.json"
REPO = "owner/repo"
OPENED = "2026-10-13T10:09:51Z"
OPENED_TS = dt.datetime(2026, 10, 13, 10, 9, 51, tzinfo=dt.timezone.utc).timestamp()
AGENTS = [{"id": name, "prompt_path": "x"} for name in ("a", "b", "c")]


def wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def pr(number: int, head: str, draft: bool = False, created_at: str = OPENED, state: str = "open") -> Dict[str, Any]:
    return {
        "number": number,
        "state": state,
        "draft": draft,
        "head": {"sha": head},
        "base": {"sha": "b" * 40, "ref": "main"},
        "title": "t",
        "html_url": "https://github.invalid/%s/pull/%d" % (REPO, number),
        "created_at": created_at,
        "body": "",
    }


class RecordingLock(dx.ExecutorLock):
    """The real flock, plus a record of acquire/release order."""

    def __init__(self, path: Path) -> None:
        super().__init__(path, logger=lambda message: None)
        self.events: List[str] = []

    def acquire(self) -> "RecordingLock":
        super().acquire()
        self.events.append("acquire")
        return self

    def release(self) -> None:
        if self.held:
            self.events.append("release")
        super().release()


class GitHub:
    """Fake GitHub: the open-PR listing and the single-PR read; editable mid-test.

    ``prs`` is what the listing returns per repository; ``live`` overrides what
    a single-PR read returns, so a test can change GitHub after the watcher
    queued a PR and before its review starts.
    """

    def __init__(self) -> None:
        self.prs: Dict[str, List[Dict[str, Any]]] = {}
        self.live: Dict[tuple, Dict[str, Any]] = {}
        self.remaining: Any = None
        self.listed: List[str] = []
        self.list_error: Optional[Exception] = None
        self.get_error: Optional[Exception] = None
        # Every commit status POST the watcher's client made: (repo, sha, state, description, url, context).
        self.statuses: List[tuple] = []
        self.status_error: Optional[Exception] = None
        # PR comments per (repository, "issue" | "review") as the repository
        # comment listings return them (updated since the asked time); every
        # listing asked as (repo, kind, since); every reaction and reply.
        self.comments: Dict[tuple, List[Dict[str, Any]]] = {}
        self.comment_reads: List[tuple] = []
        self.comment_error: Optional[Exception] = None
        # GitHub's page size for a comment listing: the client reads one page; None, no limit.
        self.comment_page: Optional[int] = None
        self.comment_writes: List[tuple] = []
        # A comment's edit history by node_id (GitHubClient.comment_edits); absent: never edited.
        self.edits: Dict[str, Any] = {}
        self.edits_error: Optional[Exception] = None
        self.edit_reads: List[str] = []
        # Raised by a reaction or a reply POST, after it is recorded.
        self.react_error: Optional[Exception] = None
        self.reply_error: Optional[Exception] = None
        # GitHub's clock (the Date of the last response), epoch seconds; None: no Date seen.
        self.server_time: Optional[float] = None
        self.bot = "review-of-legends[bot]"
        self._reads: Dict[int, int] = {}
        self._blocked: Dict[tuple, tuple] = {}
        self._guard = threading.Lock()

    def block(self, number: int, call: int = 1) -> tuple:
        """Hold the ``call``-th read of PR ``number`` until the returned gate is set."""
        entered, gate = threading.Event(), threading.Event()
        self._blocked[(number, call)] = (entered, gate)
        return entered, gate

    def read(self, repo: str, number: int) -> Dict[str, Any]:
        with self._guard:
            self._reads[number] = self._reads.get(number, 0) + 1
            blocked = self._blocked.get((number, self._reads[number]))
        if blocked is not None:
            blocked[0].set()
            blocked[1].wait(10)
        if self.get_error is not None:
            raise self.get_error
        if (repo, number) in self.live:
            return dict(self.live[(repo, number)])
        for item in self.prs.get(repo, []):
            if item["number"] == number:
                return dict(item)
        return pr(number, "0" * 40, state="closed")

    def watch_client(self, repo: str, cache: Dict[str, Any]) -> Any:
        github = self

        class WatchClient:
            def __init__(self) -> None:
                self.repo = repo
                self.rate_limit_remaining = None

            def get_pr(self, number: int) -> Dict[str, Any]:
                return github.read(repo, number)

            def list_open_prs(self) -> List[Dict[str, Any]]:
                github.listed.append(repo)
                self.rate_limit_remaining = github.remaining
                if github.list_error is not None:
                    raise github.list_error
                return [dict(item) for item in github.prs.get(repo, [])]

            def create_commit_status(self, sha, state, description, target_url="", context=svc.REQUIRED_STATUS_CONTEXT):
                with github._guard:
                    github.statuses.append((repo, sha, state, description, target_url, context))
                if github.status_error is not None:
                    raise github.status_error
                return {"id": 1}

            def bot_login(self) -> str:
                return github.bot

            @property
            def server_time(self) -> Optional[float]:
                return github.server_time

            def _comments(self, kind: str, since: str) -> List[Dict[str, Any]]:
                github.comment_reads.append((repo, kind, since))
                if github.comment_error is not None:
                    raise github.comment_error
                listed = [dict(item) for item in github.comments.get((repo, kind), []) if item["updated_at"] >= since]
                listed.sort(key=lambda item: (item["updated_at"], item["id"]))
                if github.comment_page and len(listed) > github.comment_page:
                    # Cut short like the client's listing (service.CommentListing).
                    cut = svc.CommentListing(listed[: github.comment_page])
                    cut.truncated = True
                    return cut
                return listed

            def list_repo_issue_comments(self, since: str) -> List[Dict[str, Any]]:
                return self._comments("issue", since)

            def list_repo_review_comments(self, since: str) -> List[Dict[str, Any]]:
                return self._comments("review", since)

            def comment_edits(self, node_id: str) -> Dict[str, Any]:
                # GitHub's copy now: the listed comment unless a test changed it, never edited by default.
                github.edit_reads.append(node_id)
                if github.edits_error is not None:
                    raise github.edits_error
                listed = next((item for (owner, _), items in github.comments.items() if owner == repo
                               for item in items if item.get("node_id") == node_id), {})
                snapshot = {
                    "body": listed.get("body"), "author": (listed.get("user") or {}).get("login"),
                    "updated_at": listed.get("updated_at"), "edited": False, "editors": [], "complete": True,
                }
                snapshot.update(github.edits.get(node_id, {}))
                return snapshot

            def react_to_issue_comment(self, comment_id: int, content: str) -> Any:
                github.comment_writes.append(("react-issue", repo, comment_id, content))
                if github.react_error is not None:
                    raise github.react_error

            def react_to_review_comment(self, comment_id: int, content: str) -> Any:
                github.comment_writes.append(("react-review", repo, comment_id, content))
                if github.react_error is not None:
                    raise github.react_error

            def create_issue_comment(self, number: int, body: str) -> Any:
                github.comment_writes.append(("comment", repo, number, body))
                if github.reply_error is not None:
                    raise github.reply_error

            def reply_to_review_comment(self, number: int, comment_id: int, body: str) -> Any:
                github.comment_writes.append(("reply", repo, number, comment_id, body))
                if github.reply_error is not None:
                    raise github.reply_error

        return WatchClient()

    def client(self, repo: str) -> Any:
        github = self

        class Client:
            def __init__(self) -> None:
                self.repo = repo

            def get_pr(self, number: int) -> Dict[str, Any]:
                return github.read(repo, number)

        return Client()


class Jobs:
    """Fake pool review: records the start, then blocks until released."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.started: List[tuple] = []
        self.gates: Dict[int, threading.Event] = {}
        self.active: Dict[int, str] = {}
        self.max_updates = 0
        self.max_active = 0

    def gate(self, number: int) -> threading.Event:
        with self.lock:
            return self.gates.setdefault(number, threading.Event())

    def release(self, number: int) -> None:
        self.gate(number).set()

    def release_all(self) -> None:
        with self.lock:
            gates = list(self.gates.values())
        for gate in gates:
            gate.set()

    def numbers(self) -> List[int]:
        with self.lock:
            return [item[0] for item in self.started]

    def __call__(self, job: sched_mod.Job) -> None:
        gate = self.gate(job.number)
        with self.lock:
            self.started.append((job.number, job.kind, job.head))
            self.active[job.number] = job.kind
            self.max_updates = max(self.max_updates, sum(1 for kind in self.active.values() if kind == "update"))
            self.max_active = max(self.max_active, len(self.active))
        try:
            gate.wait(10)
        finally:
            with self.lock:
                self.active.pop(job.number, None)


class FastWaiter:
    def wait(self, interval: float) -> bool:
        time.sleep(min(interval, 0.02))
        return False


class SchedulerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state_path = self.tmp / "state.json"
        self.listing = GitHub()
        self.jobs = Jobs()
        self.paused = False
        self.sweeps = 0
        self.now = OPENED_TS + 15
        self.logged: List[str] = []
        patcher = mock.patch.object(svc, "log", side_effect=self.logged.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(
        self,
        reviewed: Optional[Dict[str, Any]] = None,
        state: Optional[Dict[str, Any]] = None,
        hard_cap: int = 8,
        update_cap: int = 2,
        store: Any = None,
        monotonic: Callable[[], float] = time.monotonic,
        config_path: Optional[Path] = None,
        store_opener: Optional[Callable[[], Any]] = None,
        **config: Any,
    ) -> sched_mod.ReviewScheduler:
        if state is None:
            state = {"repositories": {REPO: {"reviewed": reviewed or {}, "baseline_initialized": True}}}
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        self.lock = RecordingLock(self.tmp / "executor.lock")
        self.cfg = {
            "repositories": [REPO],
            "agents": AGENTS,
            "max_concurrent_reviews": hard_cap,
            "max_concurrent_update_reviews": update_cap,
            "watch_interval_seconds": 20,
            "poll_interval_seconds": 180,
            "work_dir": str(self.tmp / "work"),
            **config,
        }

        def sweep() -> None:
            self.sweeps += 1

        scheduler = sched_mod.ReviewScheduler(
            self.cfg,
            config_path or CONFIG_PATH,
            self.state_path,
            store,
            lock=self.lock,
            pause_path=self.tmp / "executor.pause",
            watch_client_factory=self.listing.watch_client,
            client_factory=self.listing.client,
            review_job=self.jobs,
            sweep=sweep,
            retry_waiter=FastWaiter(),
            stop_requested=lambda: self.paused,
            # Re-read every tick: the test's dict unless a file is given.
            config_loader=None if config_path else (lambda: self.cfg),
            clock=lambda: self.now,
            monotonic=monotonic,
            **({} if store_opener is None else {"store_opener": store_opener}),
        )

        def cleanup() -> None:
            self.jobs.release_all()
            scheduler.stop()

        self.addCleanup(cleanup)
        return scheduler

    def started(self, scheduler: sched_mod.ReviewScheduler) -> sched_mod.ReviewScheduler:
        scheduler.control()  # startup: lock, state, sweep
        scheduler.start()
        return scheduler

    def saved(self) -> Dict[str, Any]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))["repositories"][REPO]

    def warnings(self, number: int) -> List[str]:
        return [line for line in self.logged if line.startswith("WARNING owner/repo pr #%d waits" % number)]


class CapsTest(SchedulerTestCase):
    def test_a_first_review_starts_on_the_same_tick_while_other_first_reviews_run(self) -> None:
        scheduler = self.started(self.make())
        self.listing.prs[REPO] = [pr(n, "%040d" % n) for n in (1, 2, 3)]
        scheduler.tick()
        self.assertTrue(wait_for(lambda: sorted(self.jobs.numbers()) == [1, 2, 3]))
        # Three long first reviews run (the old K=3 pool was full); the next starts at once.
        self.listing.prs[REPO].append(pr(10, "f" * 40))
        scheduler.tick()
        self.assertTrue(wait_for(lambda: 10 in self.jobs.numbers(), timeout=1.0), "a first review waited")
        self.assertIn("dispatch owner/repo pr #10 ffffffffffff (first) 15s after opened", self.logged)
        self.assertEqual(self.warnings(10), [])

    def test_a_first_review_starts_while_updates_run_at_their_cap(self) -> None:
        reviewed = {str(n): {"head_sha": "old%d" % n} for n in (1, 2, 3)}
        scheduler = self.started(self.make(reviewed=reviewed))
        self.listing.prs[REPO] = [pr(n, "new%d" % n) for n in (1, 2, 3)]
        scheduler.tick()
        self.assertTrue(wait_for(lambda: len(self.jobs.numbers()) == 2))
        self.listing.prs[REPO].append(pr(10, "f" * 40))
        scheduler.tick()
        self.assertTrue(wait_for(lambda: 10 in self.jobs.numbers(), timeout=1.0), "first review waited")
        self.assertNotIn(3, self.jobs.numbers())
        self.assertEqual([kind for number, kind, _ in self.jobs.started if number == 10], ["first"])

    def test_updates_never_exceed_their_cap_and_a_freed_slot_is_used_at_once(self) -> None:
        reviewed = {str(n): {"head_sha": "old%d" % n} for n in range(1, 6)}
        scheduler = self.started(self.make(reviewed=reviewed))
        self.listing.prs[REPO] = [pr(n, "new%d" % n) for n in range(1, 6)]
        scheduler.tick()
        self.assertTrue(wait_for(lambda: len(self.jobs.numbers()) == 2))
        time.sleep(0.1)
        # Started back to back, so their threads may enter in either order.
        self.assertEqual(sorted(self.jobs.numbers()), [1, 2], "more updates than max_concurrent_update_reviews")
        # The next update starts when one finishes, without waiting for a tick.
        self.jobs.release(1)
        self.assertTrue(wait_for(lambda: self.jobs.numbers()[2:] == [3]))
        self.jobs.release_all()
        self.assertTrue(wait_for(lambda: len(self.jobs.numbers()) == 5))
        self.assertLessEqual(self.jobs.max_updates, 2)

    def test_the_hard_cap_queues_first_reviews_and_warns_once_per_pr(self) -> None:
        scheduler = self.started(self.make(hard_cap=2, update_cap=1))
        self.listing.prs[REPO] = [pr(n, "%040d" % n) for n in (1, 2, 3)]
        scheduler.tick()
        self.assertTrue(wait_for(lambda: len(self.jobs.numbers()) == 2))
        self.assertTrue(wait_for(lambda: len(self.warnings(3)) == 1))
        scheduler.tick()
        time.sleep(0.1)
        self.assertEqual(sorted(self.jobs.numbers()), [1, 2])
        self.assertEqual(len(self.warnings(3)), 1, "warned more than once for one PR")
        self.jobs.release(1)
        self.assertTrue(wait_for(lambda: 3 in self.jobs.numbers()))
        self.assertLessEqual(self.jobs.max_active, 2)

    def test_a_retry_counts_toward_the_hard_cap(self) -> None:
        store = FakeStore()
        self.listing.prs[REPO] = [pr(5, "h" * 40)]
        in_retry = threading.Event()
        leave_retry = threading.Event()
        self.addCleanup(leave_retry.set)

        def fake_review_pr(client, config, config_path, pr_payload, state, **kwargs):
            in_retry.set()
            leave_retry.wait(10)
            return True

        scheduler = self.make(store=store, hard_cap=2, update_cap=1)
        scheduler.control()
        with mock.patch.object(svc, "review_pr", fake_review_pr):
            store.enqueue_retry(REPO, 5, "h" * 40, "owner@company.example")
            scheduler.start()
            self.assertTrue(in_retry.wait(3))
            self.listing.prs[REPO] = [pr(5, "h" * 40), pr(1, "a" * 40), pr(2, "c" * 40)]
            scheduler.tick()
            self.assertTrue(wait_for(lambda: len(self.jobs.numbers()) == 1))
            time.sleep(0.1)
            self.assertEqual(self.jobs.numbers(), [1], "the retry was not counted toward the hard cap")
            self.assertEqual(len(self.warnings(2)), 1)
            leave_retry.set()
            self.assertTrue(wait_for(lambda: self.jobs.numbers() == [1, 2]))

    def test_a_retry_waits_while_the_pool_holds_the_hard_cap(self) -> None:
        store = FakeStore()
        retry_ran = threading.Event()

        def fake_review_pr(client, config, config_path, pr_payload, state, **kwargs):
            retry_ran.set()
            return True

        scheduler = self.started(self.make(store=store, hard_cap=2, update_cap=1))
        self.listing.prs[REPO] = [pr(1, "a" * 40), pr(2, "c" * 40)]
        # Readable for the retry but not listed, so the pool does not take #5 itself.
        self.listing.live[(REPO, 5)] = pr(5, "h" * 40)
        scheduler.tick()
        self.assertTrue(wait_for(lambda: len(self.jobs.numbers()) == 2))
        with mock.patch.object(svc, "review_pr", fake_review_pr):
            store.enqueue_retry(REPO, 5, "h" * 40, "owner@company.example")
            self.assertTrue(wait_for(lambda: next(iter(store.requests.values()))["status"] == "running"))
            time.sleep(0.1)
            self.assertFalse(retry_ran.is_set(), "a retry ran above the hard cap")
            self.jobs.release(1)
            self.assertTrue(retry_ran.wait(3))

    def test_first_reviews_go_before_older_updates_fifo_across_repositories(self) -> None:
        other = "owner/other"
        state = {
            "repositories": {
                REPO: {"reviewed": {"1": {"head_sha": "old"}}, "baseline_initialized": True},
                other: {"reviewed": {}, "baseline_initialized": True},
            }
        }
        scheduler = self.make(state=state, hard_cap=2, update_cap=1, repositories=[REPO, other])
        scheduler.control()
        self.listing.prs[REPO] = [pr(1, "new")]
        scheduler.tick()
        # The second repository in config order, seen a tick earlier, goes first.
        self.listing.prs[other] = [pr(7, "o" * 40)]
        scheduler.tick()
        self.listing.prs[REPO].append(pr(8, "e" * 40))
        scheduler.tick()
        scheduler.start()
        self.assertTrue(wait_for(lambda: len(self.jobs.numbers()) == 2))
        time.sleep(0.1)
        # At the cap of two: the update waits behind both first reviews (started
        # back to back, so their threads may enter in either order).
        self.assertEqual(sorted(self.jobs.numbers()), [7, 8])
        self.jobs.release(7)
        self.assertTrue(wait_for(lambda: self.jobs.numbers()[2:] == [1]))

    def test_within_one_tick_the_earlier_opened_pr_goes_first_whatever_the_config_order(self) -> None:
        other = "owner/other"
        state = {
            "repositories": {
                REPO: {"reviewed": {}, "baseline_initialized": True},
                other: {"reviewed": {}, "baseline_initialized": True},
            }
        }
        scheduler = self.make(state=state, repositories=[REPO, other])
        scheduler.control()
        self.listing.prs[REPO] = [pr(1, "a" * 40, created_at="2026-10-13T10:09:59Z")]
        self.listing.prs[other] = [pr(2, "b" * 40, created_at="2026-10-13T10:09:51Z")]
        scheduler.tick()
        best = min(scheduler._queue.values(), key=lambda job: job.rank())
        self.assertEqual((best.repo, best.number), (other, 2))

    def test_a_baselined_pr_with_a_new_head_is_a_first_review(self) -> None:
        reviewed = {
            "5": {"head_sha": "old", "initial_baseline_without_review": True},
            "6": {"head_sha": "old6"},
            "7": {"head_sha": "old7"},
        }
        scheduler = self.started(self.make(reviewed=reviewed))
        # Two updates fill their cap; the baselined PR is never reviewed, so it is not an update.
        self.listing.prs[REPO] = [pr(6, "n" * 40), pr(7, "m" * 40)]
        scheduler.tick()
        self.assertTrue(wait_for(lambda: len(self.jobs.numbers()) == 2))
        self.listing.prs[REPO].append(pr(5, "5" * 40))
        scheduler.tick()
        self.assertTrue(wait_for(lambda: 5 in self.jobs.numbers()))
        self.assertEqual([kind for number, kind, _ in self.jobs.started if number == 5], ["first"])
        self.assertIn("dispatch owner/repo pr #5 555555555555 (first) 0s after seen", self.logged)


class DraftTest(SchedulerTestCase):
    def test_a_draft_waits_until_it_is_marked_ready(self) -> None:
        scheduler = self.started(self.make())
        self.listing.prs[REPO] = [pr(7, "d" * 40, draft=True)]
        scheduler.tick()
        time.sleep(0.1)
        self.assertEqual(self.jobs.numbers(), [])
        self.assertEqual(scheduler._queue, {})

        self.listing.prs[REPO] = [pr(7, "d" * 40, draft=False)]
        self.now += 40
        scheduler.tick()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [7]))
        self.assertIn("dispatch owner/repo pr #7 dddddddddddd (first) 0s after ready", self.logged)

    def test_review_drafts_true_reviews_a_draft(self) -> None:
        scheduler = self.started(self.make(review_drafts=True))
        self.listing.prs[REPO] = [pr(7, "d" * 40, draft=True)]
        scheduler.tick()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [7]))

    def test_a_queued_job_whose_pr_went_back_to_draft_or_closed_is_dropped(self) -> None:
        scheduler = self.make()
        scheduler.control()
        self.listing.prs[REPO] = [pr(8, "a" * 40), pr(9, "c" * 40)]
        scheduler.tick()
        self.assertEqual(sorted(number for _, number in scheduler._queue), [8, 9])
        self.listing.prs[REPO] = [pr(8, "a" * 40, draft=True)]  # 8 back to draft, 9 closed
        scheduler.tick()
        self.assertEqual(scheduler._queue, {})
        scheduler.start()
        time.sleep(0.1)
        self.assertEqual(self.jobs.numbers(), [])

    def test_a_baseline_leaves_drafts_out_so_marking_one_ready_starts_its_first_review(self) -> None:
        scheduler = self.started(self.make(state={"repositories": {}}))
        self.listing.prs[REPO] = [pr(1, "a" * 40), pr(2, "d" * 40, draft=True)]
        scheduler.tick()
        saved = self.saved()
        self.assertTrue(saved["baseline_initialized"])
        self.assertEqual(sorted(saved["reviewed"]), ["1"], "a draft was recorded in the baseline")
        # Same head, now ready.
        self.listing.prs[REPO] = [pr(1, "a" * 40), pr(2, "d" * 40)]
        scheduler.tick()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [2]))
        self.assertEqual(self.jobs.started, [(2, "first", "d" * 40)])

    def test_a_legacy_baselined_draft_is_cleared_and_reviewed_once_ready(self) -> None:
        reviewed = {
            "2": {"head_sha": "d" * 40, "initial_baseline_without_review": True},
            "3": {"head_sha": "e" * 40, "initial_baseline_without_review": True},
        }
        scheduler = self.started(self.make(reviewed=reviewed))
        self.listing.prs[REPO] = [pr(2, "d" * 40, draft=True), pr(3, "e" * 40)]
        scheduler.tick()
        # The draft's entry is gone; the ready PR's baseline stays.
        self.assertEqual(sorted(self.saved()["reviewed"]), ["3"])
        self.assertIn(
            "owner/repo pr #2: draft was in the baseline; cleared, so marking it ready starts its review", self.logged
        )
        self.listing.prs[REPO] = [pr(2, "d" * 40), pr(3, "e" * 40)]
        scheduler.tick()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [2]))
        time.sleep(0.1)
        self.assertEqual(self.jobs.started, [(2, "first", "d" * 40)])


    def test_a_legacy_baselined_draft_whose_clear_was_refused_is_reviewed_once_ready(self) -> None:
        reviewed = {"7": {"head_sha": "d" * 40, "initial_baseline_without_review": True}}
        scheduler = self.started(self.make(reviewed=reviewed))
        other = dx.ExecutorLock(self.tmp / "executor.lock")
        other.acquire()  # a manual `once` holds the lock: the clear cannot run this tick
        try:
            self.listing.prs[REPO] = [pr(7, "d" * 40, draft=True)]
            scheduler.tick()
            self.assertIn("7", self.saved()["reviewed"], "cleared without the executor lock")
        finally:
            other.release()
        # Marked ready, same head, before the clear could run.
        self.listing.prs[REPO] = [pr(7, "d" * 40)]
        scheduler.tick()
        scheduler.tick()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [7]), self.logged)
        self.assertEqual(self.jobs.started, [(7, "first", "d" * 40)])
        self.assertNotIn("7", self.saved()["reviewed"])


class OnceDraftTest(unittest.TestCase):
    """``once`` skips drafts unless --pr names one, and its baseline leaves them out."""

    def _run(self, state: Dict[str, Any], prs: List[Dict[str, Any]], pr_number: Optional[int] = None):
        calls: List[int] = []
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            config = {
                "repositories": [REPO],
                "agents": AGENTS,
                "agent_runner": {"command": ["true"]},
                "work_dir": str(tmp_path / "work"),
                "state_path": str(state_path),
            }

            class Client:
                def __init__(self, repo, gh_bin="gh"):
                    self.repo = repo

                def list_open_prs(self):
                    return [dict(item) for item in prs]

                def get_pr(self, number):
                    return next(dict(item) for item in prs if item["number"] == number)

            def fake_review_pr(client, config, config_path, pr_payload, bucket, **kwargs):
                calls.append(int(pr_payload["number"]))
                return False

            with mock.patch.object(svc, "GitHubClient", Client), \
                    mock.patch.object(svc, "load_config", return_value=config), \
                    mock.patch.object(svc, "expand_path", side_effect=lambda v, base=None: Path(str(v))), \
                    mock.patch.object(dx, "open_store", return_value=None), \
                    mock.patch.object(svc, "review_pr", fake_review_pr), \
                    mock.patch.object(svc, "log"):
                svc.run_once(argparse.Namespace(
                    config=str(CONFIG_PATH), repo=REPO if pr_number else None, pr=pr_number, dry_run=False, force=False,
                ))
            saved = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        return calls, saved

    def test_once_skips_drafts_without_pr_and_reviews_a_named_draft(self) -> None:
        state = {"repositories": {REPO: {"reviewed": {}, "baseline_initialized": True}}}
        prs = [pr(1, "a" * 40, draft=True), pr(2, "b" * 40)]
        self.assertEqual(self._run(state, prs)[0], [2])
        self.assertEqual(self._run(state, prs, pr_number=1)[0], [1])

    def test_once_baseline_leaves_drafts_out(self) -> None:
        calls, saved = self._run({"repositories": {}}, [pr(1, "a" * 40, draft=True), pr(2, "b" * 40)])
        self.assertEqual(calls, [])
        self.assertEqual(sorted(saved["repositories"][REPO]["reviewed"]), ["2"])


class FreshReadTest(SchedulerTestCase):
    """GitHub changes after the watcher queued a PR and before its review starts."""

    def queued(self, *prs_: Dict[str, Any], **make: Any) -> sched_mod.ReviewScheduler:
        scheduler = self.make(**make)
        scheduler.control()
        self.listing.prs[REPO] = list(prs_)
        scheduler.tick()
        self.assertEqual(sorted(number for _, number in scheduler._queue), sorted(item["number"] for item in prs_))
        return scheduler

    def assert_not_started(self, scheduler: sched_mod.ReviewScheduler, line: str) -> None:
        scheduler.start()
        self.assertTrue(wait_for(lambda: line in self.logged), self.logged)
        time.sleep(0.05)
        self.assertEqual(self.jobs.numbers(), [])
        self.assertEqual(scheduler._in_flight, {})

    def test_a_pr_marked_draft_on_github_after_it_was_queued_is_not_reviewed(self) -> None:
        scheduler = self.queued(pr(8, "a" * 40))
        self.listing.live[(REPO, 8)] = pr(8, "a" * 40, draft=True)
        self.assert_not_started(scheduler, "owner/repo pr #8: not started, back to draft")

    def test_a_pr_closed_or_merged_on_github_after_it_was_queued_is_not_reviewed(self) -> None:
        scheduler = self.queued(pr(8, "a" * 40), pr(9, "c" * 40))
        self.listing.live[(REPO, 8)] = pr(8, "a" * 40, state="closed")
        self.listing.live[(REPO, 9)] = {**pr(9, "c" * 40, state="closed"), "merged_at": "2026-10-13T10:10:00Z"}
        self.assert_not_started(scheduler, "owner/repo pr #8: not started, closed, not open")
        # #9 is read after #8 on the dispatch thread: wait for its line too.
        self.assertTrue(wait_for(lambda: "owner/repo pr #9: not started, merged" in self.logged), self.logged)

    def test_a_head_pushed_after_it_was_queued_is_the_one_reviewed(self) -> None:
        scheduler = self.queued(pr(8, "a" * 40))
        self.listing.live[(REPO, 8)] = pr(8, "c" * 40)
        scheduler.start()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [8]))
        self.assertEqual(self.jobs.started, [(8, "first", "c" * 40)])
        self.assertIn("owner/repo pr #8: head moved to cccccccccccc since it was queued; reviewing that", self.logged)

    def test_a_moved_head_that_is_already_reviewed_is_not_reviewed_again(self) -> None:
        scheduler = self.queued(pr(8, "a" * 40), reviewed={"8": {"head_sha": "c" * 40}})
        self.listing.live[(REPO, 8)] = pr(8, "c" * 40)
        self.assert_not_started(scheduler, "owner/repo pr #8: not started, cccccccccccc already reviewed")

    def test_a_failed_read_skips_the_pr_until_the_next_watch(self) -> None:
        scheduler = self.queued(pr(8, "a" * 40))
        self.listing.get_error = svc.ServiceError("GitHub API GET failed: 502")
        self.assert_not_started(scheduler, "owner/repo pr #8: not started, re-reading the PR failed: GitHub API GET failed: 502")
        self.listing.get_error = None
        scheduler.tick()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [8]))


class DedupeTest(SchedulerTestCase):
    def test_a_reviewed_head_is_not_queued_and_a_new_head_replaces_the_queued_one(self) -> None:
        scheduler = self.make(reviewed={"5": {"head_sha": "r" * 40}})
        scheduler.control()
        self.listing.prs[REPO] = [pr(5, "r" * 40), pr(6, "x" * 40)]
        scheduler.tick()
        self.assertEqual(list(scheduler._queue), [(REPO, 6)])
        self.listing.prs[REPO] = [pr(5, "r" * 40), pr(6, "y" * 40)]
        scheduler.tick()
        self.assertEqual(list(scheduler._queue), [(REPO, 6)])
        self.assertEqual(scheduler._queue[(REPO, 6)].head, "y" * 40)
        scheduler.start()
        self.assertTrue(wait_for(lambda: len(self.jobs.numbers()) == 1))
        time.sleep(0.1)
        self.assertEqual(self.jobs.started, [(6, "first", "y" * 40)])

    def test_a_head_whose_retries_are_used_up_is_skipped_and_logged_once(self) -> None:
        failed = {"4": {"head_sha": "z" * 40, "attempts": 3, "last_failed_at": "2026-10-13T00:00:00+00:00", "stage": "quorum"}}
        state = {"repositories": {REPO: {"reviewed": {}, "failed": failed, "baseline_initialized": True}}}
        scheduler = self.make(state=state)
        scheduler.control()
        self.listing.prs[REPO] = [pr(4, "z" * 40)]
        scheduler.tick()
        scheduler.tick()
        self.assertEqual(scheduler._queue, {})
        skips = [line for line in self.logged if "pr #4: skipping" in line]
        self.assertEqual(len(skips), 1, skips)

    def test_first_sight_of_a_repository_records_a_baseline_and_reviews_nothing(self) -> None:
        scheduler = self.started(self.make(state={"repositories": {}}))
        self.listing.prs[REPO] = [pr(1, "a" * 40), pr(2, "b" * 40)]
        scheduler.tick()
        time.sleep(0.1)
        self.assertEqual(self.jobs.numbers(), [])
        saved = self.saved()
        self.assertTrue(saved["baseline_initialized"])
        self.assertTrue(saved["reviewed"]["1"]["initial_baseline_without_review"])
        self.assertEqual(saved["reviewed"]["2"]["head_sha"], "b" * 40)
        # The next PR after the baseline is reviewed.
        self.listing.prs[REPO].append(pr(3, "c" * 40))
        scheduler.tick()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [3]))


class PauseTest(SchedulerTestCase):
    def test_pause_drains_in_flight_then_releases_and_resume_sweeps_once_then_dispatches(self) -> None:
        scheduler = self.started(self.make())
        self.assertEqual(self.sweeps, 1, "startup sweep")
        self.listing.prs[REPO] = [pr(1, "a" * 40)]
        scheduler.tick()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [1]))

        self.paused = True
        scheduler.control()
        self.listing.prs[REPO].append(pr(2, "b" * 40))
        scheduler.tick()
        time.sleep(0.1)
        self.assertTrue(self.lock.held, "released while a review was in flight")
        self.assertEqual(self.jobs.numbers(), [1])

        self.jobs.release(1)
        self.assertTrue(wait_for(lambda: not self.lock.held))
        time.sleep(0.1)
        self.assertEqual(self.jobs.numbers(), [1], "dispatched while paused")
        self.assertIn((REPO, 2), scheduler._queue, "the queue keeps its entries while paused")

        events_before_resume = len(self.lock.events)
        self.paused = False
        scheduler.control()
        self.assertEqual(self.sweeps, 2, "resume sweeps once")
        self.assertEqual(self.lock.events[events_before_resume:events_before_resume + 2], ["acquire", "release"])
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [1, 2]))
        scheduler.control()
        self.assertEqual(self.sweeps, 2)

    def test_the_lock_is_held_only_while_work_runs(self) -> None:
        scheduler = self.started(self.make())
        self.assertFalse(self.lock.held)
        self.listing.prs[REPO] = [pr(1, "a" * 40)]
        scheduler.tick()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [1]))
        self.assertTrue(self.lock.held)
        self.jobs.release(1)
        self.assertTrue(wait_for(lambda: not self.lock.held))

    def test_a_busy_lock_holds_reviews_until_it_is_free(self) -> None:
        scheduler = self.started(self.make())
        other = dx.ExecutorLock(self.tmp / "executor.lock")
        other.acquire()  # a manual `once`
        try:
            self.listing.prs[REPO] = [pr(1, "a" * 40)]
            scheduler.tick()
            time.sleep(0.1)
            self.assertEqual(self.jobs.numbers(), [])
        finally:
            other.release()
        with scheduler._cond:
            scheduler._cond.notify_all()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [1], timeout=7.0))


class RateLimitTest(SchedulerTestCase):
    def test_the_watcher_slows_below_the_floor_and_recovers(self) -> None:
        scheduler = self.make()
        scheduler.control()
        self.listing.prs[REPO] = []
        self.listing.remaining = 4000
        self.assertEqual(scheduler.tick(), 20.0)
        self.listing.remaining = sched_mod.RATE_LIMIT_FLOOR - 1
        self.assertEqual(scheduler.tick(), 180.0)
        self.assertEqual(scheduler.tick(), 180.0)
        self.listing.remaining = 4000
        self.assertEqual(scheduler.tick(), 20.0)
        self.assertEqual(len([line for line in self.logged if "rate limit low" in line]), 1)
        self.assertEqual(len([line for line in self.logged if "rate limit recovered" in line]), 1)

    def test_a_listing_refused_for_rate_limit_still_slows_the_watcher(self) -> None:
        scheduler = self.make()
        scheduler.control()
        self.listing.remaining = 0
        self.listing.list_error = svc.ServiceError("GitHub API GET failed: 403 API rate limit exceeded")
        self.assertEqual(scheduler.tick(), 180.0)
        self.assertEqual(len([line for line in self.logged if "rate limit low (0 left" in line]), 1)


    def _two_repositories(self) -> sched_mod.ReviewScheduler:
        other = "owner/other"
        state = {
            "repositories": {
                REPO: {"reviewed": {}, "baseline_initialized": True},
                other: {"reviewed": {}, "baseline_initialized": True},
            }
        }
        scheduler = self.make(state=state, repositories=[REPO, other])
        scheduler.control()
        return scheduler

    def test_a_secondary_rate_limit_refusal_slows_the_watcher_and_ends_the_tick(self) -> None:
        scheduler = self._two_repositories()
        self.listing.remaining = 4000  # plenty of primary quota left
        refused = svc.ServiceError(
            "GitHub API GET https://api.github.com/repos/owner/repo/pulls failed: 403 "
            '{"message":"You have exceeded a secondary rate limit. Please wait a few minutes before you try again."}'
        )
        refused.status_code = 403  # type: ignore[attr-defined]
        self.listing.list_error = refused
        self.assertEqual(scheduler.tick(), 180.0)
        self.assertEqual(self.listing.listed, [REPO], "kept listing other repositories while refused")
        self.listing.list_error = None
        self.assertEqual(scheduler.tick(), 20.0)

    def test_a_429_slows_the_watcher_whatever_its_message(self) -> None:
        scheduler = self._two_repositories()
        self.listing.remaining = 4000
        refused = svc.ServiceError("GitHub API GET failed: 429 too many requests")
        refused.status_code = 429  # type: ignore[attr-defined]
        self.listing.list_error = refused
        self.assertEqual(scheduler.tick(), 180.0)

    def test_a_403_that_is_not_a_rate_limit_does_not_slow_the_other_repositories(self) -> None:
        scheduler = self._two_repositories()
        self.listing.remaining = 4000
        denied = svc.ServiceError('GitHub API GET failed: 403 {"message":"Resource not accessible by integration"}')
        denied.status_code = 403  # type: ignore[attr-defined]
        self.listing.list_error = denied
        self.assertEqual(scheduler.tick(), 20.0)
        self.assertEqual(self.listing.listed, [REPO, "owner/other"])


class RetryThreadTest(SchedulerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = FakeStore()
        self.head = "h" * 40
        self.listing.prs[REPO] = [pr(5, self.head)]
        self.retry_ran = threading.Event()
        self.leave_retry = threading.Event()
        self.addCleanup(self.leave_retry.set)
        self.retry_payloads: List[Dict[str, Any]] = []

        def fake_review_pr(client, config, config_path, pr_payload, state, **kwargs):
            self.retry_payloads.append(pr_payload)
            self.retry_ran.set()
            self.leave_retry.wait(10)
            return True

        patcher = mock.patch.object(svc, "review_pr", fake_review_pr)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request_status(self) -> str:
        return next(iter(self.store.requests.values()))["status"]

    def pool_reviewing_5(self) -> sched_mod.ReviewScheduler:
        """The pool reviews PR #5 (blocked); a retry for #5 is claimed and waits for it."""
        scheduler = self.started(self.make(store=self.store))
        scheduler.tick()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [5]))
        self.store.enqueue_retry(REPO, 5, self.head, "owner@company.example")
        self.assertTrue(wait_for(lambda: self.request_status() == "running"))
        time.sleep(0.1)
        self.assertFalse(self.retry_ran.is_set(), "the retry reviewed a PR the pool was reviewing")
        return scheduler

    def test_pool_and_retry_thread_never_review_the_same_pr_at_once(self) -> None:
        self.pool_reviewing_5()
        self.jobs.release(5)
        self.assertTrue(self.retry_ran.wait(3))
        self.leave_retry.set()
        self.assertTrue(wait_for(lambda: self.request_status() == "succeeded"))

    def test_the_pool_does_not_take_a_pr_the_retry_thread_holds(self) -> None:
        scheduler = self.make(store=self.store)
        scheduler.control()
        self.store.enqueue_retry(REPO, 5, self.head, "owner@company.example")
        scheduler.start()
        self.assertTrue(self.retry_ran.wait(3))
        scheduler.tick()
        time.sleep(0.2)
        self.assertEqual(self.jobs.numbers(), [], "the pool reviewed a PR the retry thread holds")
        self.assertEqual(scheduler._queue, {}, "a PR in flight is not queued")
        self.leave_retry.set()
        self.assertTrue(wait_for(lambda: not scheduler._in_flight))
        # The head is still unreviewed in this fake, so the next tick queues it.
        scheduler.tick()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [5]))

    def test_a_pause_while_a_retry_waits_requeues_it_and_resume_runs_it(self) -> None:
        scheduler = self.pool_reviewing_5()
        self.paused = True
        scheduler.control()
        self.assertTrue(wait_for(lambda: self.request_status() == "queued"), "the claimed retry was not requeued")
        self.assertIn(("requeue_retry", next(iter(self.store.requests))), self.store.order)
        self.jobs.release(5)
        self.assertTrue(wait_for(lambda: not self.lock.held), "the lock was not released while paused")
        time.sleep(0.1)
        self.assertFalse(self.retry_ran.is_set(), "a retry started while paused")
        self.assertEqual(self.request_status(), "queued")

        self.paused = False
        scheduler.control()
        self.assertTrue(self.retry_ran.wait(3), "the requeued retry did not run after resume")
        self.leave_retry.set()
        self.assertTrue(wait_for(lambda: self.request_status() == "succeeded"))

    def test_stop_while_a_retry_waits_joins_the_retry_thread(self) -> None:
        scheduler = self.pool_reviewing_5()
        retry_thread = next(thread for thread in scheduler._threads if thread.name == "retry")
        started = time.monotonic()
        scheduler.stop(timeout=2.0)
        self.assertFalse(retry_thread.is_alive(), "the retry thread did not stop")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(self.request_status(), "queued")
        self.assertFalse(self.retry_ran.is_set())

    def test_a_retry_whose_pr_moved_while_it_waited_is_requeued_then_closed_as_stale(self) -> None:
        self.pool_reviewing_5()
        self.listing.live[(REPO, 5)] = pr(5, "n" * 40)
        # The retry thread backs off one PAUSE_POLL_SECONDS before claiming a requeued request.
        with mock.patch.object(sched_mod, "PAUSE_POLL_SECONDS", 0.2):
            self.jobs.release(5)
            self.assertTrue(wait_for(lambda: self.request_status() == "stale"), self.store.requests)
        self.assertFalse(self.retry_ran.is_set(), "a retry reviewed a head it was not asked for")
        self.assertIn(("requeue_retry", next(iter(self.store.requests))), self.store.order)

    def test_a_retry_reviews_the_payload_read_just_before_it_starts(self) -> None:
        self.pool_reviewing_5()
        self.listing.live[(REPO, 5)] = {**pr(5, self.head), "title": "renamed"}
        self.jobs.release(5)
        self.assertTrue(self.retry_ran.wait(3))
        self.leave_retry.set()
        self.assertEqual(self.retry_payloads[0]["title"], "renamed")

    def test_a_failed_agents_retry_hands_the_kept_results_to_the_review(self) -> None:
        kept = [{"agent": "correctness", "summary": "ok", "findings": []}]
        earlier = self.store.start_run(REPO, 5, self.head)
        self.store.update_run(earlier["id"], "failed", stage="quorum", agent_results=kept)
        seen: List[Any] = []

        def fake_review_pr(client, config, config_path, pr_payload, state, **kwargs):
            seen.append(kwargs.get("reuse_results"))
            return True

        with mock.patch.object(svc, "review_pr", fake_review_pr):
            scheduler = self.make(store=self.store)
            scheduler.control()
            self.store.enqueue_retry(REPO, 5, self.head, "owner@company.example", retry_of=earlier["id"], options={"scope": "failed"})
            scheduler.start()
            self.assertTrue(wait_for(lambda: self.request_status() == "succeeded"), self.store.requests)
        self.assertEqual(seen, [{"run_id": earlier["id"], "head_sha": self.head, "results": {"correctness": kept[0]}}])

    def test_a_pending_recovery_waits_for_the_pool_and_blocks_new_dispatch(self) -> None:
        store = FakeStore()
        scheduler = self.make(store=store)
        scheduler.control()
        self.listing.prs[REPO] = [pr(1, "a" * 40)]
        scheduler.tick()
        scheduler.start()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [1]))
        with mock.patch.object(dx, "_RECOVERY_PENDING", True):
            time.sleep(0.2)
            # recover_interrupted would mark the pool's live attempt interrupted.
            self.assertEqual(store.recovered, 0)
            self.listing.prs[REPO].append(pr(2, "b" * 40))
            scheduler.tick()
            time.sleep(0.1)
            self.assertEqual(self.jobs.numbers(), [1], "dispatched while the retry thread waits to recover")
            self.jobs.release(1)
            self.assertTrue(wait_for(lambda: store.recovered == 1))
            self.assertTrue(wait_for(lambda: self.jobs.numbers() == [1, 2]))

    def test_exclusive_mode_is_cleared_when_taking_the_lease_raises(self) -> None:
        scheduler = self.make(store=FakeStore())
        scheduler.control()

        def unreadable() -> bool:
            raise RuntimeError("sentinel unreadable")

        scheduler._stop_requested = unreadable
        with mock.patch.object(dx, "_RECOVERY_PENDING", True):
            with self.assertRaises(RuntimeError):
                scheduler._drain_retries()
        self.assertFalse(scheduler._exclusive, "dispatch would stay blocked forever")


class HaltAfterReadTest(SchedulerTestCase):
    """A pause or stop that arrives while the PR is being re-read still keeps the review from starting."""

    def setUp(self) -> None:
        super().setUp()
        self.retry_ran = threading.Event()
        self.reviewed_by_retry: List[int] = []

        def fake_review_pr(client, config, config_path, pr_payload, state, **kwargs):
            self.reviewed_by_retry.append(int(pr_payload["number"]))
            self.retry_ran.set()
            return True

        patcher = mock.patch.object(svc, "review_pr", fake_review_pr)
        patcher.start()
        self.addCleanup(patcher.stop)

    def both_reading(self, store: FakeStore) -> tuple:
        """Pool review of #1 and retry of #5, each held inside its fresh read."""
        scheduler = self.started(self.make(store=store))
        pool_entered, pool_gate = self.listing.block(1, call=1)
        # Read 1 of #5 is the drain's own validation; read 2 is the retry's fresh read.
        retry_entered, retry_gate = self.listing.block(5, call=2)
        self.addCleanup(pool_gate.set)
        self.addCleanup(retry_gate.set)
        self.listing.prs[REPO] = [pr(1, "a" * 40)]
        self.listing.live[(REPO, 5)] = pr(5, "h" * 40)  # readable, not listed: only the retry reviews it
        scheduler.tick()
        store.enqueue_retry(REPO, 5, "h" * 40, "owner@company.example")
        self.assertTrue(pool_entered.wait(3) and retry_entered.wait(3))
        return scheduler, pool_gate, retry_gate

    def test_a_pause_during_the_fresh_reads_starts_neither_review_and_resume_runs_both(self) -> None:
        store = FakeStore()
        scheduler, pool_gate, retry_gate = self.both_reading(store)
        request_id = next(iter(store.requests))
        self.paused = True
        scheduler.control()
        pool_gate.set()
        retry_gate.set()

        self.assertTrue(wait_for(lambda: store.requests[request_id]["status"] == "queued"), store.requests)
        self.assertTrue(wait_for(lambda: not self.lock.held), "the lock was not released while paused")
        time.sleep(0.1)
        self.assertEqual(self.jobs.numbers(), [], "a pool review started after the pause")
        self.assertFalse(self.retry_ran.is_set(), "a retry started after the pause")
        self.assertEqual(store.runs, {}, "an attempt was begun")
        self.assertIn((REPO, 1), scheduler._queue, "the pool review was not queued again")
        saved = self.saved()
        self.assertNotIn("1", saved["reviewed"])
        self.assertFalse(saved.get("failed"), "a halted review was counted as failed")
        self.assertTrue(any("pr #1: not started, executor paused by" in line for line in self.logged))

        self.paused = False
        scheduler.control()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [1]), "the pool review did not run after resume")
        self.assertTrue(self.retry_ran.wait(3), "the retry did not run after resume")
        self.assertTrue(wait_for(lambda: store.requests[request_id]["status"] == "succeeded"))

    def test_a_stop_during_the_fresh_reads_starts_neither_review(self) -> None:
        store = FakeStore()
        scheduler, pool_gate, retry_gate = self.both_reading(store)
        request_id = next(iter(store.requests))
        stopper = threading.Thread(target=scheduler.stop, kwargs={"timeout": 3.0})
        stopper.start()
        self.assertTrue(wait_for(lambda: scheduler._stopping))
        pool_gate.set()
        retry_gate.set()
        stopper.join(5)
        self.assertFalse(stopper.is_alive())
        self.assertTrue(wait_for(lambda: store.requests[request_id]["status"] == "queued"), store.requests)
        self.assertTrue(wait_for(lambda: not scheduler._in_flight))
        time.sleep(0.1)
        self.assertEqual(self.jobs.numbers(), [])
        self.assertFalse(self.retry_ran.is_set())
        self.assertTrue(any("pr #1: not started, the scheduler is stopping" in line for line in self.logged))


class FlakyRequeueStore(FakeStore):
    """FakeStore whose requeue write fails while ``requeue_down`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.requeue_down = False

    def requeue_retry(self, request_id) -> bool:
        if self.requeue_down:
            raise RuntimeError("database is locked")
        return super().requeue_retry(request_id)


class RereviewThreadTest(SchedulerTestCase):
    """Reviewer: rereview-specific coverage of the retry thread's generic
    machinery -- the hard-cap wait, ``_pr_validation_failure`` staleness, and
    the pause/stop halt checks have no ``rereview``-conditional branch, so
    ``RetryThreadTest`` (plain retry) already exercises the same code path.
    This runs the same three scenarios through a ``rereview`` request instead,
    closing the gap explicitly rather than by code-path inference.
    """

    def setUp(self) -> None:
        super().setUp()
        self.store = FakeStore()
        self.head = "h" * 40
        self.listing.prs[REPO] = [pr(5, self.head)]
        self.retry_ran = threading.Event()
        self.leave_retry = threading.Event()
        self.addCleanup(self.leave_retry.set)

        def fake_review_pr(client, config, config_path, pr_payload, state, **kwargs):
            self.retry_ran.set()
            self.leave_retry.wait(10)
            return True

        patcher = mock.patch.object(svc, "review_pr", fake_review_pr)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request_status(self) -> str:
        return next(iter(self.store.requests.values()))["status"]

    def pool_reviewing_5(self, **options: Any) -> sched_mod.ReviewScheduler:
        """The pool reviews PR #5 (blocked); a rereview for #5 is claimed and waits for it."""
        scheduler = self.started(self.make(store=self.store))
        scheduler.tick()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [5]))
        self.store.enqueue_retry(
            REPO, 5, self.head, "owner@company.example", options={"kind": "rereview", "scope": "all", **options}
        )
        self.assertTrue(wait_for(lambda: self.request_status() == "running"))
        time.sleep(0.1)
        self.assertFalse(self.retry_ran.is_set(), "the rereview reviewed a PR the pool was reviewing")
        return scheduler

    def test_a_rereview_waits_for_a_running_baseline_pool_attempt_on_the_same_pr(self) -> None:
        self.pool_reviewing_5()
        self.jobs.release(5)
        self.assertTrue(self.retry_ran.wait(3))
        self.leave_retry.set()
        self.assertTrue(wait_for(lambda: self.request_status() == "succeeded"))

    def test_a_head_that_moved_while_the_rereview_waited_is_refused_as_stale(self) -> None:
        self.pool_reviewing_5()
        self.listing.live[(REPO, 5)] = pr(5, "n" * 40)
        # The retry thread backs off one PAUSE_POLL_SECONDS before claiming a requeued request.
        with mock.patch.object(sched_mod, "PAUSE_POLL_SECONDS", 0.2):
            self.jobs.release(5)
            self.assertTrue(wait_for(lambda: self.request_status() == "stale"), self.store.requests)
        self.assertFalse(self.retry_ran.is_set(), "a rereview reviewed a head it was not asked for")

    def test_a_pause_while_a_rereview_waits_requeues_it_and_resume_runs_it(self) -> None:
        scheduler = self.pool_reviewing_5()
        self.paused = True
        scheduler.control()
        self.assertTrue(wait_for(lambda: self.request_status() == "queued"), "the claimed rereview was not requeued")
        self.jobs.release(5)
        self.assertTrue(wait_for(lambda: not self.lock.held), "the lock was not released while paused")
        time.sleep(0.1)
        self.assertFalse(self.retry_ran.is_set(), "a rereview started while paused")
        self.assertEqual(self.request_status(), "queued")

        self.paused = False
        scheduler.control()
        self.assertTrue(self.retry_ran.wait(3), "the requeued rereview did not run after resume")
        self.leave_retry.set()
        self.assertTrue(wait_for(lambda: self.request_status() == "succeeded"))

    def test_stop_while_a_rereview_waits_joins_the_retry_thread(self) -> None:
        scheduler = self.pool_reviewing_5()
        retry_thread = next(thread for thread in scheduler._threads if thread.name == "retry")
        scheduler.stop(timeout=2.0)
        self.assertFalse(retry_thread.is_alive(), "the retry thread did not stop")
        self.assertEqual(self.request_status(), "queued")
        self.assertFalse(self.retry_ran.is_set())

    def test_a_pool_attempt_that_finishes_unverified_while_the_rereview_waits_blocks_it(self) -> None:
        """Round 2 real interleaving test (not pre-seeded state): the rereview
        is claimed and genuinely parked (confirmed via ``_in_flight`` contention
        through the real retry thread), waiting for the real pool job holding
        PR #5. Only *after* that wait is observed does the pool attempt's
        outcome land in the store as unverified; the pool job is released only
        after that write. The retry thread must re-read the store at the
        moment it actually gets ownership, not at claim time, and refuse.
        """
        self.pool_reviewing_5()
        # The "pool attempt" finishing with an unknown posting outcome, from
        # the main thread, while the rereview thread is still confirmed
        # blocked above (pool_reviewing_5 asserted retry_ran is not set).
        unverified = self.store.start_run(REPO, 5, self.head, trigger="poll")
        self.store.update_run(unverified["id"], "interrupted", stage=dx.POSTING_STAGE)
        self.jobs.release(5)

        self.assertTrue(wait_for(lambda: self.request_status() == "failed"), self.store.requests)
        self.assertFalse(self.retry_ran.is_set(), "the review ran despite the unverified pool outcome")
        self.assertTrue(any("outcome is unknown" in line for line in self.logged), self.logged)
        self.assertIn("outcome is unknown", next(iter(self.store.requests.values()))["error"])

    def test_an_unverified_ledger_post_recorded_while_the_rereview_waits_blocks_it(self) -> None:
        """The ledger is read after the rereview owns the PR, not at claim:
        a pool review parks the head as an unverified POST (no store row, as
        when the pool's non-strict recorder write failed) while it waits."""
        scheduler = self.pool_reviewing_5()
        with svc.STATE_LOCK:
            svc.repo_state(scheduler.state, REPO)["reviewed"]["5"] = {"head_sha": self.head, "post_outcome_unknown": True}
        self.jobs.release(5)

        self.assertTrue(wait_for(lambda: self.request_status() == "failed"), self.store.requests)
        self.assertFalse(self.retry_ran.is_set(), "a rereview posted beside an unverified POST")
        self.assertIn("unverified", next(iter(self.store.requests.values()))["error"])

    def test_a_late_check_that_cannot_read_refuses_with_its_reason_on_the_request(self) -> None:
        self.pool_reviewing_5()
        with mock.patch.object(self.store, "latest_attempt_for_head", side_effect=OSError("disk gone")):
            self.jobs.release(5)
            self.assertTrue(wait_for(lambda: self.request_status() == "failed"), self.store.requests)
        self.assertFalse(self.retry_ran.is_set(), "a rereview ran on an unreadable late check")
        self.assertIn("late check", next(iter(self.store.requests.values()))["error"])


    def test_a_pool_review_that_posts_while_the_rereview_waits_satisfies_it(self) -> None:
        """Codex P2 on PR #39: a head queued in the scheduler (no store row) is
        admitted for a rereview; the pool then takes the PR first and posts its
        review. The waiting rereview must not post a second full review of the
        same head: an attempt newer than the one the click was made against
        already reviewed it."""
        self.pool_reviewing_5()
        posted = self.store.start_run(REPO, 5, self.head, trigger="poll")
        self.store.update_run(posted["id"], "succeeded", stage="posted")
        self.jobs.release(5)

        self.assertTrue(wait_for(lambda: self.request_status() == "stale"), self.store.requests)
        self.assertFalse(self.retry_ran.is_set(), "a second review of the same head was posted")
        self.assertTrue(any("reviewed meanwhile" in line for line in self.logged), self.logged)
        # The Retrigger page shows the request's error: the reason, not "was not posted".
        self.assertIn("reviewed meanwhile", next(iter(self.store.requests.values()))["error"])

    def _ledger_entry_for_5(self, scheduler: sched_mod.ReviewScheduler, seconds_after_click: int) -> None:
        requested_at = dt.datetime.fromisoformat(next(iter(self.store.requests.values()))["created_at"])
        reviewed_at = (requested_at + dt.timedelta(seconds=seconds_after_click)).isoformat(timespec="seconds")
        with svc.STATE_LOCK:
            svc.repo_state(scheduler.state, REPO)["reviewed"]["5"] = {"head_sha": self.head, "reviewed_at": reviewed_at}

    def test_a_pool_review_only_the_ledger_recorded_while_the_rereview_waits_satisfies_it(self) -> None:
        """Bot red-team P2 on dab3378: the pool's best-effort recorder could not
        write its attempt row (a transient store failure), but its review was
        posted and the ledger records it after the click. With no newer attempt
        to compare, the waiting rereview must still not post a second review."""
        scheduler = self.pool_reviewing_5()
        self._ledger_entry_for_5(scheduler, 5)
        self.jobs.release(5)

        self.assertTrue(wait_for(lambda: self.request_status() == "stale"), self.store.requests)
        self.assertFalse(self.retry_ran.is_set(), "a second review of the same head was posted")
        self.assertIn("reviewed meanwhile", next(iter(self.store.requests.values()))["error"])

    def test_a_ledger_review_after_the_page_snapshot_but_before_queueing_satisfies_it(self) -> None:
        """Bot P2 on 64059fb (red-team, reliability): the pool review finished while
        the POST validated the click, so before the request was queued. The cutoff is
        the page's snapshot (``seen_at``), not the queue time."""
        requested_at = dt.datetime.fromisoformat("2026-09-11T00:00:00+00:00")
        scheduler = self.pool_reviewing_5(
            **{dx.RETRY_SEEN_AT_OPTION: (requested_at - dt.timedelta(seconds=10)).isoformat(timespec="seconds")}
        )
        self._ledger_entry_for_5(scheduler, -5)
        self.jobs.release(5)

        self.assertTrue(wait_for(lambda: self.request_status() == "stale"), self.store.requests)
        self.assertFalse(self.retry_ran.is_set(), "a second review of the same head was posted")
        self.assertIn("reviewed meanwhile", next(iter(self.store.requests.values()))["error"])

    def test_a_ledger_review_later_in_the_snapshot_second_satisfies_it(self) -> None:
        """Bot P2 on 99f4f37 and the review gate: the snapshot and a ledger-only pool
        review fall in the same UTC second; the review came after the snapshot."""
        scheduler = self.pool_reviewing_5(**{dx.RETRY_SEEN_AT_OPTION: "2026-09-10T23:59:50.100000+00:00"})
        with svc.STATE_LOCK:
            svc.repo_state(scheduler.state, REPO)["reviewed"]["5"] = {
                "head_sha": self.head,
                "reviewed_at": "2026-09-10T23:59:50+00:00",
                "reviewed_at_exact": "2026-09-10T23:59:50.900000+00:00",
            }
        self.jobs.release(5)

        self.assertTrue(wait_for(lambda: self.request_status() == "stale"), self.store.requests)
        self.assertFalse(self.retry_ran.is_set(), "a second review of the same head was posted")

    def test_a_ledger_review_from_before_the_click_still_lets_the_rereview_run(self) -> None:
        """The ordinary case: the head was reviewed, then the admin asked for a fresh review."""
        scheduler = self.pool_reviewing_5()
        self._ledger_entry_for_5(scheduler, -5)
        self.jobs.release(5)

        self.assertTrue(self.retry_ran.wait(3), self.logged)
        self.leave_retry.set()
        self.assertTrue(wait_for(lambda: self.request_status() == "succeeded"), self.store.requests)

    def test_a_rereview_against_the_attempt_the_admin_saw_still_runs(self) -> None:
        """The same check must not refuse the ordinary case: the latest attempt
        is the one the rereview was requested against."""
        seen = self.store.start_run(REPO, 5, self.head, trigger="poll")
        self.store.update_run(seen["id"], "succeeded", stage="posted")
        scheduler = self.started(self.make(store=self.store))
        self.store.enqueue_retry(
            REPO, 5, self.head, "owner@company.example", retry_of=seen["id"], options={"kind": "rereview", "scope": "all"}
        )
        self.assertTrue(self.retry_ran.wait(3), self.logged)
        self.leave_retry.set()
        self.assertTrue(wait_for(lambda: self.request_status() == "succeeded"), self.store.requests)


class RequeueRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        dx._PENDING_REQUEUES.clear()
        self.addCleanup(dx._PENDING_REQUEUES.clear)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lock = dx.ExecutorLock(Path(self._tmp.name) / "executor.lock")
        self.lock.acquire()
        self.addCleanup(self.lock.release)

    def drain(self, store: Any, runner: Callable[..., bool], logs: List[str]) -> int:
        return dx.drain_retry_requests(
            store,
            self.lock,
            [REPO],
            lambda repo: FakeClient(repo, pr_payload=pr(5, "h" * 40)),
            runner,
            lambda repo, number: None,
            logger=logs.append,
        )

    def test_a_failed_requeue_is_retried_before_the_next_claim_and_the_retry_then_runs(self) -> None:
        store = FlakyRequeueStore()
        request_id = store.enqueue_retry(REPO, 5, "h" * 40, "owner@company.example")["id"]
        runner_called: List[int] = []
        defer = [True]

        def runner(client, payload, **kwargs):
            if defer[0]:
                raise dx.RetryDeferred("executor paused")
            runner_called.append(int(payload["number"]))
            return True

        store.requeue_down = True
        logs: List[str] = []
        self.drain(store, runner, logs)
        self.assertEqual(store.requests[request_id]["status"], "running")
        self.assertIn(request_id, dx.pending_requeues())
        self.assertFalse([line for line in logs if "returned to the queue" in line and "WARNING" not in line],
                         "a failed requeue was logged as done")
        self.assertIn(
            "WARNING retry %s: not run now (executor paused), and not yet back in the queue; it is returned "
            "before the next claim" % request_id,
            logs,
        )
        # A replay while the store is still down brings nothing back and says nothing came back.
        replay_logs: List[str] = []
        self.assertEqual(dx.replay_pending_requeues(store, replay_logs.append), 0)
        self.assertIn(request_id, dx.pending_requeues())
        self.assertFalse([line for line in replay_logs if "earlier requeue had failed" in line], replay_logs)

        store.requeue_down = False
        defer[0] = False
        logs = []
        self.drain(store, runner, logs)
        self.assertEqual(runner_called, [5])
        self.assertEqual(store.requests[request_id]["status"], "succeeded")
        self.assertEqual(dx.pending_requeues(), {})
        self.assertIn("retry request %s returned to the queue (its earlier requeue had failed)" % request_id, logs)

    def test_restart_recovery_closes_a_request_whose_requeue_never_landed(self) -> None:
        store = DashboardStore(Path(self._tmp.name) / "dashboard.sqlite3")
        request_id = store.enqueue_retry(REPO, 5, "h" * 40, "owner@company.example")["id"]

        def runner(client, payload, **kwargs):
            raise dx.RetryDeferred("executor paused")

        with mock.patch.object(store, "requeue_retry", side_effect=RuntimeError("database is locked")):
            self.drain(store, runner, [])
        self.assertEqual(store.retry_status(request_id), "running")
        # The daemon dies before the store comes back: its memory goes, the row stays.
        dx._PENDING_REQUEUES.clear()
        dx.recover_after_restart(store, self.lock, logger=lambda message: None)
        self.assertEqual(store.retry_status(request_id), "failed")
        self.assertTrue(store.get_retry(request_id)["error"])

    def test_the_store_requeues_only_a_claimed_unrun_request_and_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DashboardStore(Path(tmp) / "dashboard.sqlite3")
            first = store.enqueue_retry(REPO, 5, "h" * 40, "owner@company.example")
            store.enqueue_retry(REPO, 6, "i" * 40, "owner@company.example")
            self.assertFalse(store.requeue_retry(first["id"]), "a queued request is not running")
            claimed = store.claim_retry()
            self.assertEqual(claimed["id"], first["id"])
            self.assertTrue(store.requeue_retry(first["id"]))
            self.assertEqual(store.retry_status(first["id"]), "queued")
            last = store.events_after(0, limit=100)[-1]
            self.assertEqual((last["kind"], last["payload"]["request_id"], last["payload"]["requeued"]),
                             ("retry_enqueued", first["id"], True))
            # Its place in the queue is kept.
            self.assertEqual(store.claim_retry()["id"], first["id"])
            run = store.start_run(REPO, 5, "h" * 40, trigger="retry", request_id=first["id"])
            with self.assertRaises(ValueError):
                store.requeue_retry(first["id"])
            self.assertEqual(store.retry_status(first["id"]), "running")
            self.assertTrue(run["id"])

    def test_a_deferred_retry_is_requeued_and_the_drain_stops_claiming(self) -> None:
        logs: List[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            store = FakeStore()
            first = store.enqueue_retry(REPO, 5, "h" * 40, "owner@company.example")["id"]
            store.enqueue_retry(REPO, 6, "i" * 40, "owner@company.example")
            lock = dx.ExecutorLock(Path(tmp) / "executor.lock")
            lock.acquire()
            self.addCleanup(lock.release)

            def runner(client, payload, **kwargs):
                raise dx.RetryDeferred("executor paused")

            consumed = dx.drain_retry_requests(
                store,
                lock,
                [REPO],
                lambda repo: FakeClient(repo, pr_payload=pr(5, "h" * 40)),
                runner,
                lambda repo, number: None,
                logger=logs.append,
            )
        self.assertEqual(consumed, 1)
        self.assertEqual([request["status"] for request in store.requests.values()], ["queued", "queued"])
        self.assertEqual([run for run in store.runs], [], "no attempt was begun")
        self.assertIn("retry %s: not run now (executor paused); returned to the queue" % first, logs)
        self.assertFalse([line for line in logs if "WARNING" in line], logs)

    def test_a_deferred_retry_the_store_will_not_requeue_is_not_promised_a_later_return(self) -> None:
        def link_an_attempt(row: Dict[str, Any]) -> str:
            row["run_id"] = "run-1"
            return "request %s already has an attempt" % row["id"]

        def close_it(row: Dict[str, Any]) -> str:
            row["status"] = "failed"
            return "already failed"

        for change in (link_an_attempt, close_it):
            with self.subTest(change=change.__name__):
                dx._PENDING_REQUEUES.clear()
                store = FakeStore()
                request_id = store.enqueue_retry(REPO, 5, "h" * 40, "owner@company.example")["id"]
                reasons: List[str] = []

                def runner(client, payload, **kwargs):
                    # The row changes between the claim and the requeue.
                    reasons.append(change(store.requests[request_id]))
                    raise dx.RetryDeferred("executor paused")

                logs: List[str] = []
                self.drain(store, runner, logs)
                self.assertNotIn(request_id, dx.pending_requeues())
                self.assertIn(
                    "WARNING retry %s: not run now (executor paused), and not returned to the queue (%s)"
                    % (request_id, reasons[0]),
                    logs,
                )
                self.assertFalse(
                    [line for line in logs if "not yet back" in line or "before the next claim" in line],
                    "a request nothing will requeue was promised a later return: %r" % logs,
                )

    def test_a_deferred_retry_with_no_requeue_and_no_room_left_is_not_promised_a_later_return(self) -> None:
        class NoRequeueStore(FakeStore):
            requeue_retry = None

        def no_room_left() -> Any:
            # The store is down and the remembered requeues are already at their bound.
            store = FlakyRequeueStore()
            store.requeue_down = True
            dx._PENDING_REQUEUES["parked"] = {"id": "parked"}
            return store

        cases = (
            (NoRequeueStore, "the store cannot requeue"),
            (no_room_left, "1 requeues are already waiting for the store"),
        )
        for make_store, reason in cases:
            with self.subTest(case=make_store.__name__), mock.patch.object(dx, "MAX_PENDING_FINALIZATIONS", 1):
                dx._PENDING_REQUEUES.clear()
                store = make_store()
                request_id = store.enqueue_retry(REPO, 5, "h" * 40, "owner@company.example")["id"]

                def runner(client, payload, **kwargs):
                    raise dx.RetryDeferred("executor paused")

                logs: List[str] = []
                self.drain(store, runner, logs)
                self.assertEqual(store.requests[request_id]["status"], "running")
                self.assertNotIn(request_id, dx.pending_requeues())
                self.assertIn(
                    "WARNING retry %s: not run now (executor paused), and not returned to the queue (%s)"
                    % (request_id, reason),
                    logs,
                )
                self.assertFalse(
                    [line for line in logs if "retry %s:" % request_id in line and "before the next claim" in line],
                    "a request nothing will requeue was promised a later return: %r" % logs,
                )

    def test_a_deferred_retry_already_back_in_the_queue_is_reported_as_returned(self) -> None:
        class LandsThenRaisesStore(FakeStore):
            """The first requeue write lands, then raises; the status may be unreadable meanwhile."""

            def __init__(self, status_readable: bool) -> None:
                super().__init__()
                self.status_readable = status_readable
                self.requeues = 0

            def requeue_retry(self, request_id) -> bool:
                self.requeues += 1
                changed = super().requeue_retry(request_id)
                if self.requeues == 1:
                    raise RuntimeError("database is locked")
                return changed

            def retry_status(self, request_id):
                if not self.status_readable and self.requeues == 1:
                    raise RuntimeError("database is locked")
                return super().retry_status(request_id)

        # Readable: the status read after the error finds it queued. Unreadable: the next
        # attempt finds it no longer running, and the status read then finds it queued.
        for status_readable, requeues in ((True, 1), (False, 2)):
            with self.subTest(status_readable=status_readable):
                dx._PENDING_REQUEUES.clear()
                store = LandsThenRaisesStore(status_readable)
                request_id = store.enqueue_retry(REPO, 5, "h" * 40, "owner@company.example")["id"]

                def runner(client, payload, **kwargs):
                    raise dx.RetryDeferred("executor paused")

                logs: List[str] = []
                self.drain(store, runner, logs)
                self.assertEqual(store.requeues, requeues)
                self.assertEqual(store.requests[request_id]["status"], "queued")
                self.assertEqual(dx.pending_requeues(), {})
                self.assertIn("retry %s: not run now (executor paused); returned to the queue" % request_id, logs)
                self.assertFalse([line for line in logs if "retry %s: not run now" % request_id in line
                                  and "WARNING" in line], logs)


class PruneTest(SchedulerTestCase):
    def test_retention_prune_runs_at_most_hourly_and_never_touches_a_pr_in_flight(self) -> None:
        clock = [1000.0]
        scheduler = self.started(self.make(monotonic=lambda: clock[0]))
        self.listing.prs[REPO] = [pr(1, "a" * 40)]
        scheduler.tick()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [1]))

        runs = self.tmp / "work" / "runs"
        old = time.time() - 15 * 86400
        in_flight = runs / "pr-1-aaaaaaaaaaaa-x1"
        stale = runs / "pr-2-bbbbbbbbbbbb-x2"
        young = runs / "pr-3-cccccccccccc-x3"
        for run_dir in (in_flight, stale, young):
            (run_dir / "source").mkdir(parents=True)
            (run_dir / "source" / "file.txt").write_text("clone", encoding="utf-8")
        for run_dir in (in_flight, stale):
            os.utime(run_dir, (old, old))

        scheduler.tick()
        self.assertTrue(stale.exists(), "pruned before an hour passed")
        clock[0] += sched_mod.PRUNE_INTERVAL_SECONDS
        scheduler.tick()
        self.assertFalse(stale.exists(), "an old run dir was not pruned")
        self.assertTrue((in_flight / "source" / "file.txt").exists(), "a PR in flight lost its run dir")
        self.assertTrue((young / "source" / "file.txt").exists(), "a young clone was stripped")

        later = runs / "pr-4-dddddddddddd-x4"
        later.mkdir()
        os.utime(later, (old, old))
        scheduler.tick()
        self.assertTrue(later.exists(), "pruned twice within an hour")

    def test_a_merged_or_closed_prs_run_dirs_go_at_the_next_prune_not_after_14_days(self) -> None:
        # Run dir names carry no repository (pr-<number>-<head12>-*): a
        # run dir goes only when it records this repository, and only after a
        # successful listing of that repository stopped showing the PR.
        clock = [1000.0]
        reviewed = {"5": {"head_sha": "a" * 40}, "6": {"head_sha": "c" * 40}}
        scheduler = self.started(self.make(reviewed=reviewed, monotonic=lambda: clock[0]))
        work = self.tmp / "work"
        runs = work / "runs"
        closed_dirs = [svc.make_review_run_dir(work, 5, "a" * 40, repo=REPO) for _ in range(2)]
        # A head the listing never showed (the PR moved on between listing and review start).
        closed_dirs.append(svc.make_review_run_dir(work, 5, "e" * 40, repo=REPO))
        # Same number and the same head (a commit two repositories share): owned by another repository.
        other_repo = svc.make_review_run_dir(work, 5, "a" * 40, repo="other/repo")
        # Same number and head, written before run dirs recorded their repository: kept to retention.
        legacy = runs / "pr-5-aaaaaaaaaaaa-legacy"
        (legacy / svc.REVIEW_ARTIFACT_DIR).mkdir(parents=True)
        still_open = svc.make_review_run_dir(work, 6, "c" * 40, repo=REPO)

        self.listing.prs[REPO] = [pr(5, "a" * 40), pr(6, "c" * 40)]
        scheduler.tick()
        # A failed listing is not a close, not even on the tick the prune runs.
        self.listing.list_error = svc.ServiceError("HTTP 502")
        scheduler.tick()
        clock[0] += sched_mod.PRUNE_INTERVAL_SECONDS
        scheduler.tick()
        self.listing.list_error = None
        for run_dir in closed_dirs:
            self.assertTrue(run_dir.exists(), "removed while the PR was still open (or only unlisted by an error)")

        self.listing.prs[REPO] = [pr(6, "c" * 40)]  # #5 merged
        scheduler.tick()
        for run_dir in closed_dirs:
            self.assertTrue(run_dir.exists(), "removed before the hourly prune")
        clock[0] += sched_mod.PRUNE_INTERVAL_SECONDS
        scheduler.tick()
        for run_dir in closed_dirs:
            self.assertFalse(run_dir.exists(), "a merged PR's run dir stayed after the prune")
        self.assertTrue(other_repo.exists(), "another repository's run dir of the same number and head was removed")
        self.assertTrue(legacy.exists(), "a run dir with no recorded repository was removed before its retention")
        self.assertTrue(still_open.exists(), "an open PR's run dir was removed")
        self.assertTrue(any("removed 3 run dir(s) of closed PRs" in line for line in self.logged), self.logged)
        self.assertEqual(scheduler._closed_prs, set(), "a closed PR with nothing left of its own was not forgotten")

    def test_a_closed_prs_run_dir_that_failed_to_delete_in_the_legacy_root_is_tried_again(self) -> None:
        # The deployed config sets sweep_legacy_work_dir: true, so the prune also
        # covers ~/.local/share/review-of-legends/runs; forgetting a closed PR must
        # look at the same roots, or a failed first delete there is never retried.
        clock = [1000.0]
        home = self.tmp / "home"
        home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            scheduler = self.started(self.make(
                reviewed={"5": {"head_sha": "a" * 40}}, monotonic=lambda: clock[0], sweep_legacy_work_dir=True
            ))
            legacy_dir = svc.make_review_run_dir(home / ".local" / "share" / "review-of-legends", 5, "a" * 40, repo=REPO)
            self.listing.prs[REPO] = [pr(5, "a" * 40)]
            scheduler.tick()
            self.listing.prs[REPO] = []
            scheduler.tick()

            real_remove = svc._remove_tree
            fail_once = [True]

            def remove(path):
                if fail_once[0] and Path(path).name == legacy_dir.name:
                    fail_once[0] = False
                    return 0  # what _remove_tree does on a failed delete: logs, returns 0, raises nothing
                return real_remove(path)

            with mock.patch.object(svc, "_remove_tree", remove):
                clock[0] += sched_mod.PRUNE_INTERVAL_SECONDS
                scheduler.tick()
            self.assertTrue(legacy_dir.exists())
            clock[0] += sched_mod.PRUNE_INTERVAL_SECONDS
            scheduler.tick()
            self.assertFalse(legacy_dir.exists(), "a failed delete in the legacy root was never tried again")

    def test_a_pr_closed_before_its_review_made_a_run_dir_is_remembered_until_the_review_ends(self) -> None:
        # The prune can run after the close but before the running review reached
        # make_review_run_dir (still fetching): the close must not be forgotten.
        clock = [1000.0]
        scheduler = self.started(self.make(monotonic=lambda: clock[0]))
        work = self.tmp / "work"
        self.listing.prs[REPO] = [pr(7, "b" * 40)]
        scheduler.tick()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [7]))  # in flight, no run dir yet
        self.listing.prs[REPO] = []
        scheduler.tick()
        clock[0] += sched_mod.PRUNE_INTERVAL_SECONDS
        scheduler.tick()
        late = svc.make_review_run_dir(work, 7, "b" * 40, repo=REPO)  # the review gets there now
        self.jobs.release(7)
        self.assertTrue(wait_for(lambda: not self.jobs.active))
        clock[0] += sched_mod.PRUNE_INTERVAL_SECONDS
        scheduler.tick()
        self.assertFalse(late.exists(), "a PR closed before its review made a run dir kept it to retention")

    def test_a_reopened_pr_keeps_its_run_dirs_and_one_closed_mid_review_goes_at_the_prune_after(self) -> None:
        clock = [1000.0]
        scheduler = self.started(self.make(reviewed={"5": {"head_sha": "a" * 40}}, monotonic=lambda: clock[0]))
        work = self.tmp / "work"
        reopened = svc.make_review_run_dir(work, 5, "a" * 40, repo=REPO)
        mid_review = svc.make_review_run_dir(work, 7, "b" * 40, repo=REPO)

        self.listing.prs[REPO] = [pr(5, "a" * 40), pr(7, "b" * 40)]
        scheduler.tick()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [7]))  # #7's review runs (blocked)
        self.listing.prs[REPO] = []  # both closed
        scheduler.tick()
        self.listing.prs[REPO] = [pr(5, "a" * 40)]  # #5 reopened before the prune
        scheduler.tick()
        clock[0] += sched_mod.PRUNE_INTERVAL_SECONDS
        scheduler.tick()
        self.assertTrue(reopened.exists(), "a reopened PR's run dir was removed")
        self.assertTrue(mid_review.exists(), "the run dir of a review still running was removed")

        self.jobs.release(7)
        self.assertTrue(wait_for(lambda: not self.jobs.active))
        clock[0] += sched_mod.PRUNE_INTERVAL_SECONDS
        scheduler.tick()
        self.assertFalse(mid_review.exists(), "a PR closed mid-review kept its run dir after the review ended")
        self.assertTrue(reopened.exists())


class ConfigReloadTest(SchedulerTestCase):
    def test_caps_from_a_re_read_config_apply_to_new_reviews_only(self) -> None:
        scheduler = self.started(self.make())
        self.listing.prs[REPO] = [pr(n, "%040d" % n) for n in (1, 2, 3)]
        scheduler.tick()
        self.assertTrue(wait_for(lambda: sorted(self.jobs.numbers()) == [1, 2, 3]))
        self.cfg = {**self.cfg, "max_concurrent_reviews": 2, "max_concurrent_update_reviews": 1}
        self.listing.prs[REPO].append(pr(4, "4" * 40))
        scheduler.tick()
        self.assertIn("config: up to 2 reviews at once (1 updates), watching every 20s, drafts wait until ready", self.logged)
        self.assertTrue(wait_for(lambda: len(self.warnings(4)) == 1))
        self.assertEqual(sorted(self.jobs.active), [1, 2, 3], "running reviews were affected")
        self.jobs.release(1)
        time.sleep(0.1)
        self.assertNotIn(4, self.jobs.numbers(), "started above the new cap")
        self.jobs.release(2)
        self.assertTrue(wait_for(lambda: 4 in self.jobs.numbers()))

    def test_the_config_file_is_re_read_every_tick_and_a_bad_edit_keeps_the_last_good_one(self) -> None:
        path = self.tmp / "reviewer.json"

        def write(**values: Any) -> None:
            path.write_text(json.dumps({"repositories": [REPO], "agents": AGENTS, **values}), encoding="utf-8")

        write(watch_interval_seconds=30)
        scheduler = self.make(config_path=path)
        scheduler.control()
        self.listing.prs[REPO] = []
        self.assertEqual(scheduler.tick(), 30.0)
        path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(scheduler.tick(), 30.0)
        self.assertEqual(scheduler.tick(), 30.0)
        write(watch_interval_seconds=30, max_concurrent_reviews=1)
        self.assertEqual(scheduler.tick(), 30.0)
        warnings = [line for line in self.logged if line.startswith("WARNING config re-read failed")]
        self.assertEqual(len(warnings), 2, warnings)  # one per distinct error
        self.assertIn("invalid config JSON", warnings[0])
        self.assertIn("max_concurrent_reviews", warnings[1])
        self.assertEqual(scheduler.hard_cap, 8, "a refused config changed the cap")
        write(watch_interval_seconds=40)
        self.assertEqual(scheduler.tick(), 40.0)
        self.assertIn("config readable again", self.logged)


class DaemonPathTest(SchedulerTestCase):
    """What the daemon itself runs: the real review step, the watcher's wait, and lease hygiene."""

    def test_the_daemon_review_step_runs_run_poll_review_on_the_payload_read_at_dispatch(self) -> None:
        calls: List[tuple] = []
        scheduler = self.make()
        scheduler._review_job = scheduler._run_review  # what the daemon runs
        scheduler.control()
        self.listing.prs[REPO] = [pr(8, "a" * 40)]
        scheduler.tick()
        self.listing.live[(REPO, 8)] = pr(8, "c" * 40)

        def fake(client, config, config_path, pr_payload, repository, bucket, state, state_path, store, **kwargs):
            calls.append((pr_payload["head"]["sha"], repository, bucket is state["repositories"][REPO], state_path))
            return True

        with mock.patch.object(svc, "run_poll_review", fake):
            scheduler.start()
            self.assertTrue(wait_for(lambda: len(calls) == 1))
        self.assertEqual(calls, [("c" * 40, REPO, True, self.state_path)])

    def test_a_daemon_review_that_raises_counts_toward_the_automatic_retry_limit(self) -> None:
        scheduler = self.make()
        scheduler._review_job = scheduler._run_review
        scheduler.control()
        self.listing.prs[REPO] = [pr(8, "a" * 40)]
        scheduler.tick()
        with mock.patch.object(svc, "review_pr", side_effect=svc.ServiceError("clone failed")):
            scheduler.start()
            self.assertTrue(wait_for(lambda: "8" in (self.saved().get("failed") or {})), self.logged)

    def test_a_moved_head_hands_the_review_the_payload_read_at_dispatch(self) -> None:
        seen: List[str] = []
        scheduler = self.make()
        scheduler._review_job = lambda job: seen.append(sched_mod._head(job.pr))
        scheduler.control()
        self.listing.prs[REPO] = [pr(8, "a" * 40)]
        scheduler.tick()
        self.listing.live[(REPO, 8)] = pr(8, "c" * 40)
        scheduler.start()
        self.assertTrue(wait_for(lambda: seen == ["c" * 40]), seen)

    def test_step_waits_the_interval_tick_returned_and_checks_the_pause_every_5s(self) -> None:
        clock = [0.0]
        slept: List[float] = []
        scheduler = self.make(monotonic=lambda: clock[0])
        scheduler.control()

        def sleeper(seconds: float) -> None:
            slept.append(seconds)
            clock[0] += seconds

        scheduler._sleep = sleeper
        self.listing.prs[REPO] = []
        scheduler.step()
        self.assertEqual(sum(slept), 20.0)
        self.assertTrue(all(seconds <= sched_mod.PAUSE_POLL_SECONDS for seconds in slept), slept)

    def test_a_job_dropped_at_dispatch_gives_the_executor_lock_back(self) -> None:
        scheduler = self.make()
        scheduler.control()
        self.listing.prs[REPO] = [pr(6, "x" * 40)]
        scheduler.tick()
        # Reviewed by a manual `once` after it was queued: dropped at dispatch.
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["repositories"][REPO]["reviewed"]["6"] = {"head_sha": "x" * 40}
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        scheduler.start()
        self.assertTrue(wait_for(lambda: (REPO, 6) not in scheduler._queue))
        self.assertTrue(wait_for(lambda: not self.lock.held), "a dropped job kept the executor lock")
        self.assertEqual(scheduler._leases, 0)
        self.assertEqual(self.jobs.numbers(), [])

    def test_resume_while_a_review_still_runs_sweeps_only_after_it_ends(self) -> None:
        in_flight_at_sweep: List[int] = []
        scheduler = self.started(self.make())
        scheduler._sweep = lambda: in_flight_at_sweep.append(len(scheduler._in_flight))
        self.listing.prs[REPO] = [pr(1, "a" * 40)]
        scheduler.tick()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [1]))
        self.paused = True
        scheduler.control()
        self.paused = False  # the deploy's resume runs even after a drain that timed out
        scheduler.control()
        self.assertEqual(in_flight_at_sweep, [], "swept run dirs while a review was in flight")
        self.jobs.release(1)
        self.assertTrue(wait_for(lambda: not self.lock.held))
        scheduler.control()
        self.assertEqual(in_flight_at_sweep, [0])

    def test_no_review_starts_before_the_resume_sweep(self) -> None:
        scheduler = self.started(self.make())
        self.paused = True
        scheduler.control()
        self.listing.prs[REPO] = [pr(1, "a" * 40)]
        scheduler.tick()
        self.paused = False
        with scheduler._cond:
            scheduler._cond.notify_all()  # the dispatcher wakes before the watcher's control()
        time.sleep(0.3)
        self.assertEqual(self.jobs.numbers(), [], "a review started before the resume sweep")
        scheduler.control()
        self.assertEqual(self.sweeps, 2)
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [1]))

    def test_a_queued_pr_is_not_dispatched_while_the_retry_thread_reviews_it(self) -> None:
        store = FakeStore()
        head = "h" * 40
        in_retry, leave = threading.Event(), threading.Event()
        self.addCleanup(leave.set)

        def fake_review_pr(client, config, config_path, payload, state, **kwargs):
            in_retry.set()
            leave.wait(10)
            return True

        reviewed = {"5": {"head_sha": "old5"}, "6": {"head_sha": "old6"}}
        with mock.patch.object(svc, "review_pr", fake_review_pr):
            scheduler = self.started(self.make(reviewed=reviewed, store=store, update_cap=1))
            self.listing.prs[REPO] = [pr(6, "n" * 40), pr(5, head)]
            scheduler.tick()
            self.assertTrue(wait_for(lambda: self.jobs.numbers() == [6]))
            self.assertIn((REPO, 5), scheduler._queue)  # waits for the update slot
            store.enqueue_retry(REPO, 5, head, "owner@company.example")
            self.assertTrue(in_retry.wait(3))
            self.jobs.release(6)
            time.sleep(0.3)
            self.assertEqual(self.jobs.numbers(), [6], "pool and retry thread reviewed PR #5 at once")


class RepositoryProblemLogTest(SchedulerTestCase):
    def test_a_dashboard_repository_failing_the_quorum_is_logged_once_not_every_tick(self) -> None:
        store = FakeStore()
        store.settings["repositories"] = {
            "added": [{"repo": "owner/added", "added_by": "owner@company.example", "added_at": "2026-10-13T00:00:00+00:00"}]
        }
        scheduler = self.make(store=store, min_successful_agents=4)
        scheduler.control()
        self.listing.prs[REPO] = []
        for _ in range(3):
            scheduler.tick()
        problem = [line for line in self.logged if line.startswith("WARNING not reviewing dashboard repository owner/added")]
        self.assertEqual(len(problem), 1, problem)
        store.settings["repositories"] = {"added": []}
        scheduler.tick()
        store.settings["repositories"] = {
            "added": [{"repo": "owner/added", "added_by": "owner@company.example", "added_at": "2026-10-13T00:00:00+00:00"}]
        }
        scheduler.tick()
        problem = [line for line in self.logged if line.startswith("WARNING not reviewing dashboard repository owner/added")]
        self.assertEqual(len(problem), 2, "a problem that came back was not logged again")


class ReclassifyTest(SchedulerTestCase):
    def test_jobs_queued_as_first_reviews_that_a_once_reviewed_meanwhile_keep_to_the_update_cap(self) -> None:
        scheduler = self.started(self.make(update_cap=2))
        other = dx.ExecutorLock(self.tmp / "executor.lock")
        other.acquire()  # a manual `once` runs: the daemon cannot take the lock
        try:
            self.listing.prs[REPO] = [pr(n, "new%d" % n) for n in (1, 2, 3)]
            scheduler.tick()  # queued as first reviews against the ledger the daemon holds
            # The once reviews all three at older heads; then they are pushed again.
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            for n in (1, 2, 3):
                state["repositories"][REPO]["reviewed"][str(n)] = {"head_sha": "old%d" % n, "reviewed_at": "t"}
            self.state_path.write_text(json.dumps(state), encoding="utf-8")
        finally:
            other.release()
        with scheduler._cond:
            scheduler._cond.notify_all()
        self.assertTrue(wait_for(lambda: len(self.jobs.numbers()) == 2, timeout=7.0))
        time.sleep(0.1)
        self.assertEqual(len(self.jobs.started), 2, "more updates than max_concurrent_update_reviews")
        self.assertEqual({kind for _, kind, _ in self.jobs.started}, {"update"})
        self.jobs.release_all()
        self.assertTrue(wait_for(lambda: len(self.jobs.numbers()) == 3))
        self.assertEqual(self.jobs.started[2][1], "update")


class StateRereadTest(SchedulerTestCase):
    def test_a_failed_state_re_read_starts_nothing_and_a_head_once_reviewed_meanwhile_is_not_reviewed_again(self) -> None:
        scheduler = self.started(self.make())
        self.assertFalse(self.lock.held)
        # While the daemon is idle, a manual `once` reviews PR #1 at head a.
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["repositories"][REPO]["reviewed"]["1"] = {"head_sha": "a" * 40, "reviewed_at": "t"}
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        real_load = svc.load_state
        broken = threading.Event()
        broken.set()

        def load_state(path: Path) -> Dict[str, Any]:
            if broken.is_set():
                raise OSError(5, "Input/output error")
            return real_load(path)

        with mock.patch.object(svc, "load_state", load_state):
            self.listing.prs[REPO] = [pr(1, "a" * 40)]
            scheduler.tick()  # the watcher's copy predates the once: #1 is queued
            for _ in range(3):
                with scheduler._cond:
                    scheduler._cond.notify_all()
                time.sleep(0.1)
            self.assertEqual(self.jobs.numbers(), [], "a review started from a ledger that could not be re-read")
            self.assertFalse(self.lock.held)
            broken.clear()
            with scheduler._cond:
                scheduler._cond.notify_all()
            self.assertTrue(wait_for(lambda: (REPO, 1) not in scheduler._queue), "the re-read ledger was not used")
            time.sleep(0.1)
        self.assertEqual(self.jobs.numbers(), [])
        self.assertEqual(self.saved()["reviewed"]["1"]["head_sha"], "a" * 40)
        unreadable = [line for line in self.logged if line.startswith("WARNING state unreadable")]
        self.assertEqual(len(unreadable), 1, unreadable)  # once per distinct error, not per attempt
        self.assertIn("state readable again", self.logged)


class StoreReopenTest(SchedulerTestCase):
    def test_a_dashboard_store_unavailable_at_start_is_reopened_and_the_retry_thread_started(self) -> None:
        clock = {"t": 1000.0}
        store = FakeStore()
        offered: Dict[str, Any] = {"store": None, "calls": 0}

        def opener() -> Any:
            offered["calls"] += 1
            return offered["store"]

        scheduler = self.started(self.make(store=None, store_opener=opener, monotonic=lambda: clock["t"]))
        self.assertEqual([thread.name for thread in scheduler._threads], ["dispatch"])
        self.listing.prs[REPO] = []
        scheduler.tick()
        self.assertEqual(offered["calls"], 0, "re-opened before poll_interval_seconds")
        clock["t"] += 181
        scheduler.tick()
        self.assertEqual(offered["calls"], 1)
        self.assertIsNone(scheduler.store)
        offered["store"] = store
        clock["t"] += 181
        scheduler.tick()
        self.assertIs(scheduler.store, store)
        self.assertIn("dashboard store open again: attempts are recorded and operator retries run", self.logged)
        self.assertTrue(any(thread.name == "retry" and thread.is_alive() for thread in scheduler._threads))
        clock["t"] += 181
        scheduler.tick()
        self.assertEqual(offered["calls"], 2, "re-opened an open store")

        reviewed = threading.Event()

        def fake_review_pr(client, config, config_path, pr_payload, state, **kwargs):
            reviewed.set()
            return True

        self.listing.live[(REPO, 5)] = pr(5, "h" * 40)
        with mock.patch.object(svc, "review_pr", fake_review_pr):
            store.enqueue_retry(REPO, 5, "h" * 40, "owner@company.example")
            self.assertTrue(reviewed.wait(5), "the operator retry did not run on the re-opened store")


class SupervisionTest(SchedulerTestCase):
    def test_a_dispatch_thread_that_died_is_restarted_by_the_next_tick(self) -> None:
        scheduler = self.started(self.make())
        dispatch = scheduler._threads[0]
        real_take = scheduler._take_job_locked
        calls = {"n": 0}

        def dying_take() -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise SystemExit("escapes every `except Exception`")
            return real_take()

        scheduler._take_job_locked = dying_take  # type: ignore[assignment]
        with scheduler._cond:
            scheduler._cond.notify_all()
        self.assertTrue(wait_for(lambda: not dispatch.is_alive()))
        self.listing.prs[REPO] = [pr(1, "a" * 40)]
        scheduler.tick()
        self.assertTrue(wait_for(lambda: self.jobs.numbers() == [1]), "no review starts after the dispatcher died")
        self.assertIn("ERROR dispatch thread stopped; started it again", self.logged)


class ConcurrentStateTest(unittest.TestCase):
    def test_concurrent_review_completions_never_break_save_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = _config(tmp_path / "work")
            config["progress_comment"] = {"enabled": False}
            state_path = tmp_path / "state.json"
            # A large ledger makes json.dump slow enough to overlap other writers.
            history = {str(10000 + n): {"head_sha": "%040d" % n, "agents": ["fake"] * 20} for n in range(3000)}
            state: Dict[str, Any] = {"repositories": {REPO: {"reviewed": history, "baseline_initialized": True}}}
            bucket = state["repositories"][REPO]
            numbers = list(range(1, 13))
            errors: List[BaseException] = []
            barrier = threading.Barrier(len(numbers))
            finished = {"count": 0, "first_dump": True}
            guard = threading.Lock()
            others_finished = threading.Event()

            def review(number: int) -> None:
                client = FakeClient(REPO, pr_payload=pr(number, "%040x" % number))
                try:
                    barrier.wait(5)
                    svc.review_pr(
                        client, config, CONFIG_PATH, client.pr_payload, bucket,
                        state_path=state_path, full_state=state,
                    )
                except BaseException as exc:  # noqa: BLE001 - collected for the assertion
                    errors.append(exc)
                finally:
                    with guard:
                        finished["count"] += 1
                        if finished["count"] == len(numbers) - 1:
                            others_finished.set()

            real_dump = json.dump

            def slow_dump(obj: Any, handle: Any, **kwargs: Any) -> None:
                if not svc.STATE_LOCK._is_owned():  # type: ignore[attr-defined]
                    errors.append(AssertionError("state.json dumped without STATE_LOCK"))
                real_dump(obj, handle, **kwargs)
                with guard:
                    first, finished["first_dump"] = finished["first_dump"], False
                if first:
                    # Hold the first snapshot until every other review is done.
                    # Unserialized, they all save meanwhile and this older
                    # snapshot replaces theirs last; serialized, they wait on
                    # the state lock and this times out.
                    others_finished.wait(1.0)

            with _stub_agent(), _stub_clone(), mock.patch.object(svc, "load_repository_rules", lambda *a, **k: ""), \
                    mock.patch.object(svc, "log"), mock.patch.object(svc.json, "dump", slow_dump):
                threads = [threading.Thread(target=review, args=(number,)) for number in numbers]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(60)
            self.assertEqual(errors, [])
            saved = json.loads(state_path.read_text(encoding="utf-8"))["repositories"][REPO]["reviewed"]
            for number in numbers:
                self.assertEqual(saved[str(number)]["head_sha"], "%040x" % number)


class ConditionalListingTest(unittest.TestCase):
    """The watcher's listing sends If-None-Match; a 304 is served from the cache."""

    @staticmethod
    def _headers(**values: str) -> email.message.Message:
        message = email.message.Message()
        for name, value in values.items():
            message[name.replace("_", "-")] = value
        return message

    def test_second_listing_sends_if_none_match_and_a_304_returns_the_cached_prs(self) -> None:
        page2 = "https://api.github.com/repositories/1/pulls?state=open&per_page=100&page=2"
        bodies = {
            1: ([pr(1, "a" * 40)], self._headers(ETag='W/"p1"', Link='<%s>; rel="next"' % page2, X_RateLimit_Remaining="4999")),
            2: ([pr(2, "b" * 40)], self._headers(ETag='W/"p2"', X_RateLimit_Remaining="4998")),
        }
        sent: List[Dict[str, str]] = []

        class Response(io.BytesIO):
            def __init__(self, body: Any, headers: email.message.Message) -> None:
                super().__init__(json.dumps(body).encode("utf-8"))
                self.headers = headers

        def fake_urlopen(request, timeout=60):
            headers = {name.lower(): value for name, value in request.header_items()}
            sent.append(headers)
            page = 2 if request.full_url == page2 else 1
            body, response_headers = bodies[page]
            if "if-none-match" in headers:
                raise urllib.error.HTTPError(
                    request.full_url, 304, "Not Modified", self._headers(X_RateLimit_Remaining="4321"), None
                )
            return Response(body, response_headers)

        cache: Dict[str, Any] = {}
        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"), \
                mock.patch.object(svc.urllib.request, "urlopen", fake_urlopen):
            client = svc.make_github_client(REPO, {"gh_bin": "gh"}, etag_cache=cache)
            first = client.list_open_prs()
            self.assertEqual(client.rate_limit_remaining, 4998)
            second = client.list_open_prs()
            self.assertEqual(client.rate_limit_remaining, 4321)
            # Fresh objects per call: a caller cannot change the cache through them.
            second[0]["title"] = "mutated"
            third = client.list_open_prs()
        self.assertEqual([item["number"] for item in first], [1, 2])
        self.assertEqual(third, first)
        self.assertEqual(second[1], first[1])
        self.assertEqual(
            [headers.get("if-none-match") for headers in sent],
            [None, None, 'W/"p1"', 'W/"p2"', 'W/"p1"', 'W/"p2"'],
        )

    def test_a_304_with_a_new_link_header_follows_the_new_next_page(self) -> None:
        page2 = "https://api.github.com/repositories/1/pulls?state=open&per_page=100&page=2"
        calls: List[str] = []

        class Response(io.BytesIO):
            def __init__(self, body: Any, headers: email.message.Message) -> None:
                super().__init__(json.dumps(body).encode("utf-8"))
                self.headers = headers

        def fake_urlopen(request, timeout=60):
            calls.append(request.full_url)
            headers = {name.lower(): value for name, value in request.header_items()}
            if request.full_url == page2:
                return Response([pr(101, "z" * 40)], self._headers(ETag='W/"p2"'))
            if "if-none-match" in headers:
                # Page 1 is unchanged, but a reopened older PR now spills onto page 2.
                raise urllib.error.HTTPError(
                    request.full_url, 304, "Not Modified", self._headers(Link='<%s>; rel="next"' % page2), None
                )
            return Response([pr(1, "a" * 40)], self._headers(ETag='W/"p1"'))

        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"), \
                mock.patch.object(svc.urllib.request, "urlopen", fake_urlopen):
            client = svc.make_github_client(REPO, {"gh_bin": "gh"}, etag_cache={})
            self.assertEqual([item["number"] for item in client.list_open_prs()], [1])
            self.assertEqual([item["number"] for item in client.list_open_prs()], [1, 101])
            self.assertEqual([item["number"] for item in client.list_open_prs()], [1, 101], "the new Link was not kept")

    def test_other_requests_and_clients_without_a_cache_stay_unconditional(self) -> None:
        sent: List[Dict[str, str]] = []

        class Response(io.BytesIO):
            def __init__(self) -> None:
                super().__init__(b"[]")
                self.headers = email.message.Message()
                self.headers["ETag"] = '"x"'

        def fake_urlopen(request, timeout=60):
            sent.append({name.lower(): value for name, value in request.header_items()})
            return Response()

        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"), \
                mock.patch.object(svc.urllib.request, "urlopen", fake_urlopen):
            plain = svc.make_github_client(REPO, {"gh_bin": "gh"})
            plain.list_open_prs()
            plain.list_open_prs()
            cached = svc.make_github_client(REPO, {"gh_bin": "gh"}, etag_cache={})
            cached.list_files(1)
            cached.list_files(1)
        self.assertTrue(all("if-none-match" not in headers for headers in sent))

    def test_a_304_without_a_cached_copy_is_an_error(self) -> None:
        def fake_urlopen(request, timeout=60):
            raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", email.message.Message(), io.BytesIO(b""))

        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"), \
                mock.patch.object(svc.urllib.request, "urlopen", fake_urlopen):
            client = svc.make_github_client(REPO, {"gh_bin": "gh"}, etag_cache={})
            with self.assertRaises(svc.ServiceError):
                client.list_open_prs()


class RunDaemonTest(unittest.TestCase):
    def test_the_daemon_runs_startup_maintenance_then_the_scheduler_on_the_executor_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = {
                "repositories": [REPO],
                "agents": [{"id": name, "prompt_path": "x"} for name in ("a", "b", "c")],
                "state_path": str(tmp_path / "data" / "state.json"),
            }
            calls: List[str] = []
            seen: Dict[str, Any] = {}

            def run_forever(scheduler: sched_mod.ReviewScheduler) -> None:
                calls.append("scheduler")
                seen["lock"] = scheduler.lock.path
                seen["pause"] = scheduler.pause_path
                seen["held"] = scheduler.lock.held

            store = FakeStore()
            with mock.patch.object(svc, "load_config", return_value=config), \
                    mock.patch.object(svc, "expand_path", side_effect=lambda v, base=None: Path(str(v))), \
                    mock.patch.object(dx, "open_store", return_value=store), \
                    mock.patch.object(dx, "recover_after_restart", side_effect=lambda *a, **k: calls.append("recover")), \
                    mock.patch.object(dx, "run_startup_imports", side_effect=lambda *a, **k: calls.append("imports")), \
                    mock.patch.object(sched_mod.ReviewScheduler, "run_forever", run_forever), \
                    mock.patch.object(svc, "log"):
                rc = svc.run_daemon(mock.Mock(config=str(CONFIG_PATH)))
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["recover", "imports", "scheduler"])
        self.assertEqual(seen["lock"], tmp_path / "data" / "executor.lock")
        self.assertEqual(seen["pause"], tmp_path / "data" / "executor.pause")
        self.assertFalse(seen["held"], "the startup lock is released before the scheduler starts")




class ConfigTest(unittest.TestCase):
    def _load(self, **extra: Any) -> Dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviewer.json"
            path.write_text(json.dumps({"repositories": [REPO], "agents": AGENTS, **extra}), encoding="utf-8")
            return svc.load_config(path)

    def test_the_caps_are_validated(self) -> None:
        refused = [
            {"max_concurrent_reviews": 1, "max_concurrent_update_reviews": 0},
            {"max_concurrent_update_reviews": 0},
            {"max_concurrent_reviews": True},
            {"max_concurrent_reviews": "8"},
            {"max_concurrent_reviews": None},
            {"max_concurrent_reviews": 2},  # the default 2 updates leave no slot for a first review
            {"max_concurrent_reviews": 4, "max_concurrent_update_reviews": 4},
        ]
        for extra in refused:
            with self.subTest(extra=extra), self.assertRaises(svc.ServiceError) as caught:
                self._load(**extra)
            self.assertIn("max_concurrent", str(caught.exception))
        self.assertEqual(svc.review_caps(self._load()), (8, 2))
        self.assertEqual(svc.review_caps(self._load(max_concurrent_reviews=2, max_concurrent_update_reviews=1)), (2, 1))

    def test_watch_interval_and_review_drafts_are_validated(self) -> None:
        with self.assertRaises(svc.ServiceError):
            self._load(watch_interval_seconds=0)
        with self.assertRaises(svc.ServiceError):
            self._load(review_drafts="no")
        self._load(watch_interval_seconds=5, review_drafts=True)

    def test_the_shipped_config_watches_every_20s_caps_at_8_with_2_updates_and_skips_drafts(self) -> None:
        from tests.shipped_config import load_shipped_config

        config = load_shipped_config()
        self.assertEqual(config["watch_interval_seconds"], 20)
        self.assertEqual(svc.review_caps(config), (8, 2))
        self.assertIs(config["review_drafts"], False)


class ReviewedStatusWatchTest(SchedulerTestCase):
    """The watcher sets ``review-of-legends/reviewed/<base>`` on every head of a reviewed PR.

    A reviewed PR must stay mergeable on a new push without waiting for its
    update review, a retargeted one gets its new base's context, and PRs
    reviewed before the status existed get it once after the deploy. Only
    success, recorded as ``<base>@<head>`` in the ledger's ``reviewed_status``
    map under a lease of the executor lock; at most 20 POSTs a tick, and none
    in a repository for poll_interval_seconds after one there failed.
    """

    OLD = "0" * 40
    NEW = "1" * 40
    MAIN = "review-of-legends/reviewed/main"

    def posts(self) -> List[tuple]:
        return [(repo, sha, state, context) for repo, sha, state, _, _, context in self.listing.statuses]

    def reviewed_state(self, memo: Dict[str, str], **bucket: Any) -> Dict[str, Any]:
        reviewed = bucket.pop("reviewed", {str(n): {"head_sha": key.split("@", 1)[1]} for n, key in memo.items()})
        return {"repositories": {REPO: {"reviewed": reviewed, "reviewed_status": memo, "baseline_initialized": True, **bucket}}}

    def test_an_open_reviewed_pr_without_the_status_is_backfilled_once(self) -> None:
        scheduler = self.started(self.make(reviewed={"1": {"head_sha": self.OLD}}))
        self.listing.prs[REPO] = [pr(1, self.OLD)]
        before = len(self.lock.events)
        scheduler.tick()
        self.assertEqual(self.listing.statuses, [(
            REPO, self.OLD, "success",
            "reviewed at least once (last review %s); this head's own review runs separately" % self.OLD[:12],
            "https://github.invalid/owner/repo/pull/1", self.MAIN,
        )])
        self.assertEqual(self.saved()["reviewed_status"], {"1": "main@" + self.OLD})
        self.assertEqual(self.lock.events[before:], ["acquire", "release"], "written under a lease of the executor lock")
        before = len(self.lock.events)
        scheduler.tick()
        self.assertEqual(len(self.listing.statuses), 1, "a recorded head is not set again")
        self.assertEqual(self.lock.events[before:], [], "a quiet watch takes no lock")
        self.assertEqual(self.jobs.numbers(), [], "nothing to review")

    def test_a_new_head_on_a_reviewed_pr_gets_success_before_its_update_review_runs(self) -> None:
        scheduler = self.started(self.make(state=self.reviewed_state({"1": "main@" + self.OLD})))
        self.listing.prs[REPO] = [pr(1, self.NEW)]
        scheduler.tick()
        self.assertTrue(wait_for(lambda: self.jobs.started == [(1, "update", self.NEW)]))
        # The update review is still running (its gate is closed): the head is green already.
        self.assertEqual(self.posts(), [(REPO, self.NEW, "success", self.MAIN)])
        self.assertIn("last review %s" % self.OLD[:12], self.listing.statuses[0][3])
        self.assertEqual(self.saved()["reviewed_status"], {"1": "main@" + self.NEW})
        self.jobs.release(1)

    def test_a_retargeted_pr_gets_the_new_bases_context(self) -> None:
        scheduler = self.started(self.make(state=self.reviewed_state({"1": "main@" + self.OLD})))
        retargeted = pr(1, self.OLD)
        retargeted["base"] = {"sha": "b" * 40, "ref": "develop"}
        self.listing.prs[REPO] = [retargeted]
        scheduler.tick()
        self.assertEqual(self.posts(), [(REPO, self.OLD, "success", "review-of-legends/reviewed/develop")])
        self.assertEqual(self.saved()["reviewed_status"], {"1": "develop@" + self.OLD})
        scheduler.tick()
        self.assertEqual(len(self.listing.statuses), 1)

    def test_never_reviewed_failed_only_baselined_and_draft_prs_get_nothing(self) -> None:
        reviewed = {
            "2": {"head_sha": "b" * 40, "initial_baseline_without_review": True},
            "3": {"head_sha": "c" * 40},
        }
        # PR 4's only trace is a failed first run: no reviewed entry.
        failed = {"4": {"head_sha": "d" * 40, "attempts": 3, "last_failed_at": "2026-10-14T00:00:00+00:00", "stage": "quorum"}}
        state = {"repositories": {REPO: {"reviewed": reviewed, "failed": failed, "baseline_initialized": True}}}
        scheduler = self.started(self.make(state=state))
        self.listing.prs[REPO] = [pr(1, "a" * 40), pr(2, "b" * 40), pr(3, "c" * 40, draft=True), pr(4, "d" * 40)]
        scheduler.tick()
        scheduler.tick()
        self.assertEqual(self.listing.statuses, [])
        self.assertNotIn("reviewed_status", self.saved())
        # Marked ready: the reviewed draft gets it on the next watch.
        self.listing.prs[REPO][2] = pr(3, "c" * 40)
        scheduler.tick()
        self.assertEqual(self.posts(), [(REPO, "c" * 40, "success", self.MAIN)])

    def test_a_shared_head_sets_only_the_reviewed_prs_base(self) -> None:
        """PR #32 round 1: GitHub keys a status by commit and context, so the context names the base."""
        shared = "5" * 40
        state = {"repositories": {REPO: {"reviewed": {"1": {"head_sha": shared}}, "baseline_initialized": True}}}
        scheduler = self.started(self.make(state=state))
        other_base = pr(2, shared)
        other_base["base"] = {"sha": "b" * 40, "ref": "develop"}
        same_base = pr(3, shared)  # never reviewed, same head and base as #1
        self.listing.prs[REPO] = [pr(1, shared), other_base, same_base]
        scheduler.tick()
        self.assertEqual(self.posts(), [(REPO, shared, "success", self.MAIN)], "one POST, for #1 only")
        self.assertEqual(self.saved()["reviewed_status"], {"1": "main@" + shared})
        scheduler.tick()
        self.assertEqual(len(self.listing.statuses), 1)

    def test_at_most_20_posts_a_tick_across_repositories_and_the_rest_next_tick(self) -> None:
        other = "owner/other"
        state = {"repositories": {
            REPO: {"reviewed": {str(n): {"head_sha": "%040d" % n} for n in range(1, 26)}, "baseline_initialized": True},
            other: {"reviewed": {str(n): {"head_sha": "%040d" % n} for n in range(1, 11)}, "baseline_initialized": True},
        }}
        scheduler = self.started(self.make(state=state, repositories=[REPO, other]))
        self.listing.prs[REPO] = [pr(n, "%040d" % n) for n in range(1, 26)]
        self.listing.prs[other] = [pr(n, "%040d" % n) for n in range(1, 11)]
        scheduler.tick()
        self.assertEqual(len(self.listing.statuses), 20)
        self.assertEqual(sum(1 for repo, *_ in self.listing.statuses if repo == other), 0)
        scheduler.tick()
        self.assertEqual(len(self.listing.statuses), 35)
        self.assertEqual(len({(repo, sha) for repo, sha, *_ in self.listing.statuses}), 35, "nothing set twice")
        scheduler.tick()
        self.assertEqual(len(self.listing.statuses), 35)

    def test_the_memo_is_persisted_and_a_reloaded_scheduler_does_not_post_again(self) -> None:
        scheduler = self.started(self.make(reviewed={"1": {"head_sha": self.OLD}}))
        self.listing.prs[REPO] = [pr(1, self.OLD)]
        scheduler.tick()
        self.assertEqual(len(self.listing.statuses), 1)
        scheduler.stop()
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["repositories"][REPO]["reviewed_status"], {"1": "main@" + self.OLD})
        restarted = self.started(self.make(state=saved))
        restarted.tick()
        restarted.tick()
        self.assertEqual(len(self.listing.statuses), 1, "posted again after a restart")

    def test_nothing_is_posted_or_written_while_another_executor_holds_the_lock(self) -> None:
        scheduler = self.started(self.make(reviewed={"1": {"head_sha": self.OLD}}))
        self.listing.prs[REPO] = [pr(1, self.OLD)]
        other = dx.ExecutorLock(self.tmp / "executor.lock")
        other.acquire()  # a manual `once`
        try:
            scheduler.tick()
            self.assertEqual(self.listing.statuses, [])
            self.assertNotIn("reviewed_status", self.saved())
        finally:
            other.release()
        scheduler.tick()
        self.assertEqual(self.posts(), [(REPO, self.OLD, "success", self.MAIN)])

    def test_the_memo_survives_a_review_rewriting_the_ledger_entry(self) -> None:
        """review_pr replaces reviewed[n] whole; the map of set heads is separate."""
        scheduler = self.make(state=self.reviewed_state({"1": "main@" + self.OLD}))

        def review(job: sched_mod.Job) -> None:
            with svc.STATE_LOCK:
                bucket = svc.repo_state(scheduler.state, job.repo)
                bucket["reviewed"][str(job.number)] = {"head_sha": job.head, "review_event": "COMMENT", "agents": ["a"]}
                svc.save_state(scheduler.state_path, scheduler.state)

        scheduler._review_job = review
        self.started(scheduler)
        self.listing.prs[REPO] = [pr(1, self.NEW)]
        scheduler.tick()
        self.assertTrue(wait_for(lambda: self.saved()["reviewed"]["1"]["head_sha"] == self.NEW))
        self.assertTrue(wait_for(lambda: not self.lock.held))
        scheduler.tick()
        self.assertEqual(self.posts(), [(REPO, self.NEW, "success", self.MAIN)], "set once, kept through the rewrite")
        self.assertEqual(self.saved()["reviewed_status"], {"1": "main@" + self.NEW})
        self.assertNotIn("reviewed_status", self.saved()["reviewed"]["1"])

    def test_a_failed_post_backs_the_repository_off_for_the_poll_interval_and_logs_once(self) -> None:
        clock = [1000.0]
        other = "owner/other"
        state = {"repositories": {
            REPO: {"reviewed": {"1": {"head_sha": self.OLD}, "2": {"head_sha": self.NEW}}, "baseline_initialized": True},
            other: {"reviewed": {"7": {"head_sha": self.OLD}}, "baseline_initialized": True},
        }}
        scheduler = self.started(self.make(
            state=state, repositories=[REPO, other], monotonic=lambda: clock[0], store_opener=lambda: None,
        ))
        self.listing.prs[REPO] = [pr(1, self.OLD), pr(2, self.NEW)]
        self.listing.prs[other] = [pr(7, self.OLD)]
        refusing = [True]  # a lasting 403 in REPO only
        original = self.listing.watch_client

        def watch_client(repo: str, cache: Dict[str, Any]) -> Any:
            client = original(repo, cache)
            if repo == REPO:
                post = client.create_commit_status

                def refused_while_set(*args: Any, **kwargs: Any) -> Any:
                    post(*args, **kwargs)  # recorded as an attempt
                    if refusing[0]:
                        raise svc.ServiceError("GitHub API POST statuses failed: 403")
                    return {"id": 1}

                client.create_commit_status = refused_while_set
            return client

        scheduler._watch_client_factory = watch_client
        scheduler._watch_clients.clear()

        def attempts(repo: str) -> int:
            return sum(1 for item in self.listing.statuses if item[0] == repo)

        scheduler.tick()
        self.assertEqual(attempts(REPO), 1, "the first failure stops the repository's POSTs")
        self.assertEqual(attempts(other), 1, "another repository is not held back")
        scheduler.tick()
        clock[0] += 179
        scheduler.tick()
        self.assertEqual(attempts(REPO), 1, "no POST within poll_interval_seconds")
        clock[0] += 1
        scheduler.tick()
        self.assertEqual(attempts(REPO), 2, "tried again after poll_interval_seconds")
        warned = [line for line in self.logged if "WARNING" in line and "reviewed/main status" in line]
        self.assertEqual(len(warned), 1, warned)
        self.assertIn("owner/repo pr #1: WARNING review-of-legends/reviewed/main status on", warned[0])
        self.assertIn("for 180s", warned[0])
        self.assertNotIn("reviewed_status", self.saved())
        refusing[0] = False
        clock[0] += 180
        scheduler.tick()
        self.assertEqual(attempts(REPO), 4)
        self.assertEqual(self.saved()["reviewed_status"], {"1": "main@" + self.OLD, "2": "main@" + self.NEW})
        self.assertIn("owner/repo: reviewed status POSTs work again", self.logged)

    def test_a_batch_that_succeeds_then_fails_keeps_its_backoff(self) -> None:
        """PR #32 round 2: an earlier success in the batch lifted the backoff the later failure set."""
        clock = [1000.0]
        state = {"repositories": {REPO: {
            "reviewed": {"1": {"head_sha": self.OLD}, "2": {"head_sha": self.NEW}}, "baseline_initialized": True,
        }}}
        scheduler = self.started(self.make(state=state, monotonic=lambda: clock[0], store_opener=lambda: None))
        self.listing.prs[REPO] = [pr(1, self.OLD), pr(2, self.NEW)]
        refusing = [True]  # #2's head is refused; #1's is accepted
        original = self.listing.watch_client

        def watch_client(repo: str, cache: Dict[str, Any]) -> Any:
            client = original(repo, cache)
            post = client.create_commit_status

            def refuse_new(sha: str, *args: Any, **kwargs: Any) -> Any:
                post(sha, *args, **kwargs)  # recorded as an attempt
                if refusing[0] and sha == self.NEW:
                    raise svc.ServiceError("GitHub API POST statuses failed: 403")
                return {"id": 1}

            client.create_commit_status = refuse_new
            return client

        scheduler._watch_client_factory = watch_client

        def attempts() -> List[str]:
            return [sha for _, sha, *_ in self.listing.statuses]

        def warnings() -> List[str]:
            return [line for line in self.logged if "WARNING" in line and "reviewed/main status" in line]

        def recovered() -> List[str]:
            return [line for line in self.logged if line == "owner/repo: reviewed status POSTs work again"]

        scheduler.tick()
        self.assertEqual(attempts(), [self.OLD, self.NEW], "#1 set, then #2 refused")
        self.assertIn(REPO.lower(), scheduler._reviewed_status_backoff, "the refusal's backoff stands")
        self.assertEqual(len(warnings()), 1)
        self.assertEqual(recovered(), [], "no recovery in a batch that ended in a refusal")
        self.assertEqual(self.saved()["reviewed_status"], {"1": "main@" + self.OLD})
        clock[0] += 20
        scheduler.tick()
        clock[0] += 159
        scheduler.tick()
        self.assertEqual(len(attempts()), 2, "no POST before poll_interval_seconds")
        clock[0] += 1
        scheduler.tick()
        self.assertEqual(attempts()[2:], [self.NEW], "retried at +180 s, refused again")
        self.assertEqual(len(warnings()), 1, "one WARNING while it keeps failing")
        self.assertEqual(recovered(), [])
        refusing[0] = False
        clock[0] += 180
        scheduler.tick()
        self.assertEqual(attempts()[3:], [self.NEW])
        self.assertEqual(len(recovered()), 1, "a batch that fully succeeds logs the recovery once")
        self.assertNotIn(REPO.lower(), scheduler._reviewed_status_backoff)
        self.assertEqual(self.saved()["reviewed_status"], {"1": "main@" + self.OLD, "2": "main@" + self.NEW})
        scheduler.tick()
        self.assertEqual(len(recovered()), 1)
        self.assertEqual(len(attempts()), 4)

    def test_a_failing_repository_whose_due_prs_closed_is_cleared_silently(self) -> None:
        """fable R2: the failing flag outlived the PRs it failed on, so a later,
        unrelated failure in that repository never logged its WARNING."""
        clock = [1000.0]
        scheduler = self.started(self.make(
            reviewed={"1": {"head_sha": self.OLD}, "2": {"head_sha": self.NEW}},
            monotonic=lambda: clock[0], store_opener=lambda: None,
        ))
        self.listing.status_error = svc.ServiceError("GitHub API POST statuses failed: 403")

        def warnings() -> List[str]:
            return [line for line in self.logged if "WARNING" in line and "reviewed/main status" in line]

        self.listing.prs[REPO] = [pr(1, self.OLD)]
        scheduler.tick()
        self.assertEqual(len(warnings()), 1)
        self.assertIn(REPO.lower(), scheduler._reviewed_status_failing)
        # #1 closes before any POST worked.
        self.listing.prs[REPO] = []
        clock[0] += 20
        scheduler.tick()
        self.assertIn(REPO.lower(), scheduler._reviewed_status_failing, "not cleared during the backoff")
        clock[0] += 160
        logged = len(self.logged)
        scheduler.tick()
        self.assertNotIn(REPO.lower(), scheduler._reviewed_status_failing, "a zero-due batch past the backoff clears it")
        self.assertNotIn(REPO.lower(), scheduler._reviewed_status_backoff)
        self.assertEqual(self.logged[logged:], [], "cleared silently")
        # A later, unrelated failure logs its WARNING again.
        self.listing.prs[REPO] = [pr(2, self.NEW)]
        scheduler.tick()
        self.assertEqual(len(self.listing.statuses), 2)
        self.assertEqual(len(warnings()), 2)
        self.assertIn("owner/repo pr #2: WARNING", warnings()[1])
        self.assertFalse([line for line in self.logged if "work again" in line])

    def test_closed_prs_leave_the_memo_only_when_the_listing_worked(self) -> None:
        scheduler = self.started(self.make(state=self.reviewed_state({"1": "main@" + self.OLD, "2": "main@" + self.NEW})))
        self.listing.prs[REPO] = [pr(1, self.OLD)]
        self.listing.list_error = svc.ServiceError("GitHub API GET pulls failed: 502")
        scheduler.tick()
        self.assertEqual(self.saved()["reviewed_status"], {"1": "main@" + self.OLD, "2": "main@" + self.NEW})
        self.listing.list_error = None
        scheduler.tick()
        self.assertEqual(self.saved()["reviewed_status"], {"1": "main@" + self.OLD})
        self.assertEqual(self.listing.statuses, [])

    def test_reviewed_status_false_posts_nothing(self) -> None:
        scheduler = self.started(self.make(reviewed={"1": {"head_sha": self.OLD}}, reviewed_status=False))
        self.listing.prs[REPO] = [pr(1, self.OLD), pr(2, self.NEW)]
        scheduler.tick()
        scheduler.tick()
        self.assertEqual(self.listing.statuses, [])
        self.assertNotIn("reviewed_status", self.saved())

    def test_an_empty_repository_is_baselined_and_harmless(self) -> None:
        """CosmosBo (2026-10-14) has no commits yet: listing its pulls returns []."""
        scheduler = self.started(self.make(state={"repositories": {}}))
        self.listing.prs[REPO] = []
        scheduler.tick()
        scheduler.tick()
        saved = self.saved()
        self.assertTrue(saved["baseline_initialized"])
        self.assertEqual(saved["reviewed"], {})
        self.assertEqual(self.listing.statuses, [])
        self.assertEqual(self.jobs.numbers(), [])
        self.assertFalse(self.lock.held)
        self.assertFalse([line for line in self.logged if "WARNING" in line], self.logged)

if __name__ == "__main__":
    unittest.main()
