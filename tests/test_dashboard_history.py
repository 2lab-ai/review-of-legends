"""Retained-log import of prior FAILED review attempts (W2b).

The JSON ledger only ever recorded *successful* reviews (service.py:1967-2018
saves state after the GitHub POST), so ``import_legacy_ledger``
(dashboard_execution.py:921-1018) cannot show a failure. The user's acceptance
clause is "현재 진행중인 실패하면 리뷰를 확인할수 있다 (지난 리뷰도 확인할 수 잇따)":
past failures must be visible, not only the successful ledger. The only durable
record of a past failure is the retained daemon stdout log plus the preserved
run directory, so this unit parses that log.

Fixtures below are anonymized copies of the *exact* shapes the producer emits:

* ``log`` prefixes every line with ``isoformat(timespec="seconds")``  (service.py:62-64)
* ``{repo} pr #{n}: cloning source into {run_dir}/source``            (service.py:1742)
* ``{repo} pr #{n}: running {k} agents``                              (service.py:1778)
* ``{repo} pr #{n}: only {s}/{k} agents succeeded; not posting review`` (service.py:1841-1843)
* ``{repo} pr #{n}: preserving run dir for inspection: {run_dir}``    (service.py:1867)
* run dir basename ``pr-{n}-{head_sha[:12]}-{mkdtemp suffix}``        (service.py:568)

Verified against production excerpts (380322-line log, a production PR one attempt,
Pluto #481 three distinct attempt dirs, "only 1/6" and "only 2/6" outcomes).

Two properties are tested harder than the rest because they are the ways this
importer could lie:

1. a failure is imported only when the log *says* it failed - never inferred
   from the absence of a success line;
2. a 12-char head prefix is never grown into a fabricated full SHA; only a
   caller-supplied resolver whose answer still starts with that prefix does it.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import dashboard_history
from code_review_service.dashboard_store import DashboardStore

REPO_A = "example-org/Alpha"
REPO_B = "example-org/Beta"
CONFIGURED = [REPO_A, REPO_B, "example-org/Gamma"]
RUNS = "/var/lib/review-of-legends/data/work/runs"


# --------------------------------------------------------------------- fixtures


def run_dir(pr: int, head12: str, suffix: str) -> str:
    """A run directory exactly as ``make_review_run_dir`` names it."""
    return "%s/pr-%d-%s-%s" % (RUNS, pr, head12, suffix)


def clone(ts: str, repo: str, pr: int, directory: str) -> str:
    return "%s %s pr #%d: cloning source into %s/source" % (ts, repo, pr, directory)


def running(ts: str, repo: str, pr: int, agents: int = 6) -> str:
    return "%s %s pr #%d: running %d agents" % (ts, repo, pr, agents)


def quorum_failed(ts: str, repo: str, pr: int, ok: int = 1, total: int = 6) -> str:
    return "%s %s pr #%d: only %d/%d agents succeeded; not posting review" % (
        ts,
        repo,
        pr,
        ok,
        total,
    )


def preserving(ts: str, repo: str, pr: int, directory: str) -> str:
    return "%s %s pr #%d: preserving run dir for inspection: %s" % (ts, repo, pr, directory)


def posting(ts: str, repo: str, pr: int) -> str:
    return "%s %s pr #%d: posting COMMENT review with 4 line comments (version deploy-main-1)" % (
        ts,
        repo,
        pr,
    )


def failed_attempt_lines(
    repo: str,
    pr: int,
    head12: str,
    suffix: str,
    hour: int,
    ok: int = 1,
    total: int = 6,
) -> List[str]:
    """The four-line shape of one observed quorum failure."""
    directory = run_dir(pr, head12, suffix)
    return [
        clone("2026-09-11T%02d:20:36+00:00" % hour, repo, pr, directory),
        running("2026-09-11T%02d:20:46+00:00" % hour, repo, pr, total),
        quorum_failed("2026-09-11T%02d:48:21+00:00" % hour, repo, pr, ok, total),
        preserving("2026-09-11T%02d:48:24+00:00" % hour, repo, pr, directory),
    ]


NOISE = [
    "2026-09-11T00:00:01+00:00",
    "+--------------------------+",
    "|  POSTING EXECUTIVE SUMMARY  |",
    "| Repo: example-org/Beta |",
    "| PR: #481 |",
    "+--------------------------+",
    "2026-09-11T00:00:02+00:00 example-org/Beta pr #400: already reviewed 0b1c478fe4b9",
    "2026-09-11T00:00:03+00:00 example-org/Beta pr #401: fetching changed files",
    "2026-09-11T00:00:04+00:00 polling 5 repositories",
    "",
    "   ",
]


class _StoreDouble:
    """Records the exact import calls; ``import_run`` mirrors the real store.

    Supported keyword arguments are declared per subclass so the importer can be
    tested against W1's *shipped* narrow signature (dashboard_store.py:436-446)
    and against the additive metadata signature requested for it.
    """

    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.calls: List[Dict[str, Any]] = []
        self.updates: List[Any] = []

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self.rows.get(run_id)

    def update_run(self, run_id: str, status: str, **fields: Any) -> None:
        # Recorded so a test can prove startup never emits events this way.
        self.updates.append((run_id, status, fields))

    def _record(self, run_id: str, repo: str, pr: int, head_sha: str, **kwargs: Any) -> Dict[str, Any]:
        call = {"run_id": run_id, "repo": repo, "pr": pr, "head_sha": head_sha}
        call.update(kwargs)
        self.calls.append(call)
        row = dict(call, id=run_id)
        self.rows.setdefault(run_id, row)
        return self.rows[run_id]


class NarrowStore(_StoreDouble):
    """W1's shipped signature: no stage/error/successful_agents yet."""

    def import_run(
        self,
        run_id: str,
        repo: str,
        pr: int,
        head_sha: str,
        status: str = "succeeded",
        trigger: str = "legacy",
        created_at: str = "",
        total_agents: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self._record(
            run_id,
            repo,
            pr,
            head_sha,
            status=status,
            trigger=trigger,
            created_at=created_at,
            total_agents=total_agents,
        )


class ShippedStore(_StoreDouble):
    """W1's signature as of dashboard_store.py:436-448: stage/error, no counter."""

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
        total_agents: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self._record(
            run_id,
            repo,
            pr,
            head_sha,
            status=status,
            trigger=trigger,
            created_at=created_at,
            stage=stage,
            error=error,
            total_agents=total_agents,
        )


class FullStore(_StoreDouble):
    """The additive metadata signature requested from W1."""

    def import_run(
        self,
        run_id: str,
        repo: str,
        pr: int,
        head_sha: str,
        status: str = "succeeded",
        trigger: str = "legacy",
        created_at: str = "",
        total_agents: Optional[int] = None,
        stage: Optional[str] = None,
        error: Optional[str] = None,
        successful_agents: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self._record(
            run_id,
            repo,
            pr,
            head_sha,
            status=status,
            trigger=trigger,
            created_at=created_at,
            total_agents=total_agents,
            stage=stage,
            error=error,
            successful_agents=successful_agents,
        )


class HistoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def write_log(self, lines: List[str]) -> Path:
        path = self.tmp / "service.out.log"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def import_lines(
        self,
        lines: List[str],
        store: Optional[_StoreDouble] = None,
        **kwargs: Any,
    ):
        store = FullStore() if store is None else store
        counts = dashboard_history.import_failed_history(
            store, self.write_log(lines), CONFIGURED, **kwargs
        )
        return store, counts


# ------------------------------------------------------------------- parse only


class ParseTest(HistoryTestCase):
    def test_parses_one_observed_failure_with_run_dir_identity(self) -> None:
        lines = failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 3)
        attempts = dashboard_history.parse_failed_attempts(self.write_log(lines), CONFIGURED)
        self.assertEqual(len(attempts), 1)
        attempt = attempts[0]
        self.assertEqual(attempt.repo, REPO_A)
        self.assertEqual(attempt.pr, 4352)
        self.assertEqual(attempt.head_prefix, "c899b3ada777")
        self.assertIsNone(attempt.head_sha)
        self.assertEqual(attempt.successful_agents, 1)
        self.assertEqual(attempt.total_agents, 6)
        # created_at is the attempt's own start line, verbatim from the log.
        self.assertEqual(attempt.created_at, "2026-09-11T03:20:36+00:00")
        self.assertEqual(attempt.failed_at, "2026-09-11T03:48:21+00:00")
        self.assertEqual(attempt.reason, "only 1/6 agents succeeded; not posting review")
        self.assertEqual(attempt.run_dir, run_dir(4352, "c899b3ada777", "nzr6mstv"))

    def test_three_attempts_on_one_pr_stay_three_distinct_rows(self) -> None:
        # Observed: Pluto #481 had three attempt dirs on the same head.
        lines = (
            failed_attempt_lines(REPO_B, 481, "0b1c478fe4b9", "8sss8eas", 1)
            + failed_attempt_lines(REPO_B, 481, "0b1c478fe4b9", "e2xvc9bo", 2)
            + failed_attempt_lines(REPO_B, 481, "0b1c478fe4b9", "ljdaa54v", 4, ok=2)
        )
        attempts = dashboard_history.parse_failed_attempts(self.write_log(lines), CONFIGURED)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len({attempt.run_id for attempt in attempts}), 3)
        self.assertEqual([attempt.successful_agents for attempt in attempts], [1, 1, 2])
        self.assertEqual(
            [attempt.created_at for attempt in attempts],
            [
                "2026-09-11T01:20:36+00:00",
                "2026-09-11T02:20:36+00:00",
                "2026-09-11T04:20:36+00:00",
            ],
        )

    def test_ignores_banner_and_unrelated_lines(self) -> None:
        lines = NOISE + failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 3) + NOISE
        attempts = dashboard_history.parse_failed_attempts(self.write_log(lines), CONFIGURED)
        self.assertEqual(len(attempts), 1)

    def test_run_id_is_deterministic_and_leaks_no_path(self) -> None:
        directory = run_dir(4352, "c899b3ada777", "nzr6mstv")
        run_id = dashboard_history.historical_run_id(directory)
        self.assertEqual(run_id, dashboard_history.historical_run_id(directory))
        self.assertTrue(run_id.startswith("historical-failed-"))
        self.assertNotEqual(run_id, dashboard_history.historical_run_id(directory + "x"))
        self.assertNotIn("/", run_id)
        self.assertNotIn("pr-4352", run_id)


class NoInferenceTest(HistoryTestCase):
    """Absence of evidence is never imported as a failure."""

    def test_attempt_without_terminal_lines_is_not_imported(self) -> None:
        directory = run_dir(4352, "c899b3ada777", "nzr6mstv")
        store, counts = self.import_lines(
            [
                clone("2026-09-11T03:20:36+00:00", REPO_A, 4352, directory),
                running("2026-09-11T03:20:46+00:00", REPO_A, 4352),
            ]
        )
        self.assertEqual(store.calls, [])
        self.assertEqual(counts["imported"], 0)
        self.assertEqual(counts["unterminated"], 1)

    def test_attempt_that_posted_a_review_is_not_imported_as_failed(self) -> None:
        directory = run_dir(4352, "c899b3ada777", "nzr6mstv")
        store, counts = self.import_lines(
            [
                clone("2026-09-11T03:20:36+00:00", REPO_A, 4352, directory),
                running("2026-09-11T03:20:46+00:00", REPO_A, 4352),
                posting("2026-09-11T03:48:21+00:00", REPO_A, 4352),
            ]
        )
        self.assertEqual(store.calls, [])
        self.assertEqual(counts["imported"], 0)
        self.assertEqual(counts["succeeded"], 1)

    def test_restarted_attempt_does_not_fabricate_a_failure_for_the_first_one(self) -> None:
        first = run_dir(4352, "c899b3ada777", "aaaaaaaa")
        store, counts = self.import_lines(
            [clone("2026-09-11T03:20:36+00:00", REPO_A, 4352, first)]
            + failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 5)
        )
        self.assertEqual([call["run_id"] for call in store.calls],
                         [dashboard_history.historical_run_id(run_dir(4352, "c899b3ada777", "nzr6mstv"))])
        self.assertEqual(counts["unterminated"], 1)
        self.assertEqual(counts["imported"], 1)

    def test_preserved_dir_without_a_quorum_line_is_not_a_failure(self) -> None:
        directory = run_dir(4352, "c899b3ada777", "nzr6mstv")
        store, counts = self.import_lines(
            [
                clone("2026-09-11T03:20:36+00:00", REPO_A, 4352, directory),
                running("2026-09-11T03:20:46+00:00", REPO_A, 4352),
                preserving("2026-09-11T03:48:24+00:00", REPO_A, 4352, directory),
            ]
        )
        self.assertEqual(store.calls, [])
        self.assertEqual(counts["imported"], 0)

    def test_quorum_line_without_preserved_dir_is_not_imported(self) -> None:
        # Without the preserving line there is no run dir, hence no attempt
        # identity: importing it would have to invent one.
        store, counts = self.import_lines(
            [quorum_failed("2026-09-11T03:48:21+00:00", REPO_A, 4352)]
        )
        self.assertEqual(store.calls, [])
        self.assertEqual(counts["unmatched_quorum"], 1)

    def test_crash_terminated_attempt_is_counted_but_not_imported(self) -> None:
        # service.py:2245 logs an exception outcome with no quorum verdict; the
        # attempt may have posted a review before failing.
        directory = run_dir(4352, "c899b3ada777", "nzr6mstv")
        store, counts = self.import_lines(
            [
                clone("2026-09-11T03:20:36+00:00", REPO_A, 4352, directory),
                running("2026-09-11T03:20:46+00:00", REPO_A, 4352),
                "2026-09-11T03:21:00+00:00 %s pr #4352: skipped due to error: boom" % REPO_A,
            ]
        )
        self.assertEqual(store.calls, [])
        self.assertEqual(counts["error_terminated"], 1)


class RejectionTest(HistoryTestCase):
    def test_unconfigured_repository_is_rejected(self) -> None:
        store, counts = self.import_lines(
            failed_attempt_lines("other-org/Rogue", 1, "aaaaaaaaaaaa", "zzzzzzzz", 3)
        )
        self.assertEqual(store.calls, [])
        self.assertEqual(counts["unknown_repo"], 1)
        self.assertEqual(counts["imported"], 0)

    def test_malformed_timestamp_is_rejected(self) -> None:
        directory = run_dir(4352, "c899b3ada777", "nzr6mstv")
        lines = [
            clone("not-a-timestamp", REPO_A, 4352, directory),
            running("2026-09-11T03:20:46+00:00", REPO_A, 4352),
            quorum_failed("2026-09-11T03:48:21+00:00", REPO_A, 4352),
            preserving("2026-09-11T03:48:24+00:00", REPO_A, 4352, directory),
        ]
        store, counts = self.import_lines(lines)
        self.assertEqual(store.calls, [])
        self.assertEqual(counts["malformed"], 1)

    def test_run_dir_naming_that_disagrees_with_the_pr_is_rejected(self) -> None:
        # A dir whose basename PR differs from the logged PR is ambiguous: the
        # head prefix cannot be attributed to either PR.
        mismatched = run_dir(999, "c899b3ada777", "nzr6mstv")
        store, counts = self.import_lines(
            [
                clone("2026-09-11T03:20:36+00:00", REPO_A, 4352, mismatched),
                quorum_failed("2026-09-11T03:48:21+00:00", REPO_A, 4352),
                preserving("2026-09-11T03:48:24+00:00", REPO_A, 4352, mismatched),
            ]
        )
        self.assertEqual(store.calls, [])
        self.assertEqual(counts["ambiguous"], 1)

    def test_run_dir_without_a_head_prefix_is_rejected(self) -> None:
        directory = "%s/pr-4352-not-hex-nzr6mstv" % RUNS
        store, counts = self.import_lines(
            [
                clone("2026-09-11T03:20:36+00:00", REPO_A, 4352, directory),
                quorum_failed("2026-09-11T03:48:21+00:00", REPO_A, 4352),
                preserving("2026-09-11T03:48:24+00:00", REPO_A, 4352, directory),
            ]
        )
        self.assertEqual(store.calls, [])
        self.assertEqual(counts["malformed"], 1)

    def test_preserved_dir_from_another_attempt_is_not_matched(self) -> None:
        opened = run_dir(4352, "c899b3ada777", "nzr6mstv")
        other = run_dir(4352, "c899b3ada777", "unrelated")
        store, counts = self.import_lines(
            [
                clone("2026-09-11T03:20:36+00:00", REPO_A, 4352, opened),
                quorum_failed("2026-09-11T03:48:21+00:00", REPO_A, 4352),
                preserving("2026-09-11T03:48:24+00:00", REPO_A, 4352, other),
            ]
        )
        self.assertEqual(store.calls, [])
        self.assertEqual(counts["unmatched_preserve"], 1)

    def test_missing_log_file_is_reported_not_raised(self) -> None:
        store = FullStore()
        counts = dashboard_history.import_failed_history(
            store, self.tmp / "absent.log", CONFIGURED
        )
        self.assertEqual(counts["log_missing"], 1)
        self.assertEqual(counts["imported"], 0)
        self.assertEqual(store.calls, [])

    def test_no_configured_repositories_imports_nothing(self) -> None:
        store = FullStore()
        counts = dashboard_history.import_failed_history(
            store, self.write_log(failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "n", 3)), []
        )
        self.assertEqual(counts["unconfigured"], 1)
        self.assertEqual(store.calls, [])


class BoundsTest(HistoryTestCase):
    def test_only_the_newest_failures_within_the_limit_are_imported(self) -> None:
        lines: List[str] = []
        for index in range(5):
            lines += failed_attempt_lines(
                REPO_B, 500 + index, "0b1c478fe4b%d" % index, "s%d" % index, index + 1
            )
        store, counts = self.import_lines(lines, limit=2)
        self.assertEqual([call["pr"] for call in store.calls], [503, 504])
        self.assertEqual(counts["imported"], 2)
        self.assertEqual(counts["dropped_over_limit"], 3)

    def test_limit_is_capped_and_sanitized(self) -> None:
        lines = failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 3)
        for bad_limit in (0, -5):
            store, counts = self.import_lines(lines, limit=bad_limit)
            self.assertEqual(counts["imported"], 1, "limit=%r" % (bad_limit,))
        self.assertLessEqual(dashboard_history.MAX_LIMIT, 1000)
        attempts = dashboard_history.parse_failed_attempts(
            self.write_log(lines), CONFIGURED, limit=10 ** 9
        )
        self.assertEqual(len(attempts), 1)

    def test_pending_attempts_are_bounded_while_streaming(self) -> None:
        # A long log full of attempts that never terminate must not accumulate
        # unbounded parser state.
        lines: List[str] = []
        for index in range(dashboard_history.MAX_PENDING_ATTEMPTS + 50):
            lines.append(
                clone(
                    "2026-09-11T00:00:00+00:00",
                    REPO_B,
                    1000 + index,
                    run_dir(1000 + index, "0b1c478fe4b9", "s%d" % index),
                )
            )
        lines += failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 3)
        store, counts = self.import_lines(lines)
        self.assertEqual(counts["imported"], 1)
        self.assertGreaterEqual(counts["unterminated"], 50)


class HeadResolutionTest(HistoryTestCase):
    def test_unresolved_head_keeps_the_prefix_and_is_marked_incomplete(self) -> None:
        store, counts = self.import_lines(
            failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 3)
        )
        call = store.calls[0]
        self.assertEqual(call["head_sha"], "c899b3ada777")
        self.assertEqual(counts["head_prefix_only"], 1)
        self.assertEqual(counts["head_resolved"], 0)
        self.assertIn(dashboard_history.INCOMPLETE_HEAD_NOTE, call["error"])

    def test_resolver_answer_matching_the_prefix_completes_the_head(self) -> None:
        full = "c899b3ada7776f91714384601f8a0c8883dd3fe3"
        seen: List[Any] = []

        def resolve(repo: str, pr: int) -> str:
            seen.append((repo, pr))
            return full

        store, counts = self.import_lines(
            failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 3),
            resolve_head=resolve,
        )
        self.assertEqual(seen, [(REPO_A, 4352)])
        self.assertEqual(store.calls[0]["head_sha"], full)
        self.assertEqual(counts["head_resolved"], 1)
        self.assertEqual(counts["head_prefix_only"], 0)
        self.assertNotIn(dashboard_history.INCOMPLETE_HEAD_NOTE, store.calls[0]["error"] or "")

    def test_resolver_answer_for_a_moved_head_is_refused(self) -> None:
        moved = "ffffffffffff6f91714384601f8a0c8883dd3fe3"
        store, counts = self.import_lines(
            failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 3),
            resolve_head=lambda repo, pr: moved,
        )
        self.assertEqual(store.calls[0]["head_sha"], "c899b3ada777")
        self.assertEqual(counts["head_prefix_only"], 1)
        self.assertEqual(counts["head_mismatch"], 1)

    def test_resolver_garbage_and_failure_never_invent_a_head(self) -> None:
        for answer in (None, "", "zz", 12345, "C899B3ADA777" + "0" * 28):
            store, counts = self.import_lines(
                failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 3),
                resolve_head=lambda repo, pr, value=answer: value,
            )
            self.assertEqual(store.calls[0]["head_sha"], "c899b3ada777", "answer=%r" % (answer,))
            self.assertEqual(counts["head_resolved"], 0, "answer=%r" % (answer,))

        def explode(repo: str, pr: int) -> str:
            raise RuntimeError("network is not available in the importer")

        store, counts = self.import_lines(
            failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 3),
            resolve_head=explode,
        )
        self.assertEqual(store.calls[0]["head_sha"], "c899b3ada777")
        self.assertEqual(counts["head_lookup_errors"], 1)
        self.assertEqual(counts["imported"], 1)

    def test_lookups_are_deduplicated_per_pr_and_hard_bounded(self) -> None:
        lines = (
            failed_attempt_lines(REPO_B, 481, "0b1c478fe4b9", "8sss8eas", 1)
            + failed_attempt_lines(REPO_B, 481, "0b1c478fe4b9", "e2xvc9bo", 2)
            + failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 3)
            + failed_attempt_lines(REPO_A, 4353, "c899b3ada777", "other111", 4)
        )
        seen: List[Any] = []

        def resolve(repo: str, pr: int) -> None:
            seen.append((repo, pr))
            return None

        store, counts = self.import_lines(lines, resolve_head=resolve, max_head_lookups=2)
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(set(seen)), 2)
        # The newest failures are the ones worth resolving: they are retryable.
        self.assertEqual(set(seen), {(REPO_A, 4353), (REPO_A, 4352)})
        self.assertEqual(counts["head_lookups"], 2)
        self.assertEqual(counts["imported"], 4)

    def test_resolution_does_not_change_the_run_id(self) -> None:
        lines = failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 3)
        plain, _ = self.import_lines(lines)
        resolved, _ = self.import_lines(
            lines, resolve_head=lambda repo, pr: "c899b3ada7776f91714384601f8a0c8883dd3fe3"
        )
        self.assertEqual(plain.calls[0]["run_id"], resolved.calls[0]["run_id"])


class ImportContractTest(HistoryTestCase):
    def test_import_uses_failed_status_and_legacy_failure_trigger(self) -> None:
        store, counts = self.import_lines(
            failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 3)
        )
        call = store.calls[0]
        self.assertEqual(call["status"], "failed")
        self.assertEqual(call["trigger"], "legacy-failure")
        self.assertEqual(call["created_at"], "2026-09-11T03:20:36+00:00")
        self.assertEqual(call["stage"], "quorum")
        self.assertEqual(call["successful_agents"], 1)
        self.assertEqual(call["total_agents"], 6)
        self.assertTrue(call["error"].startswith("only 1/6 agents succeeded; not posting review"))
        self.assertEqual(counts["imported"], 1)

    def test_no_filesystem_path_or_prompt_text_reaches_the_store(self) -> None:
        # A preserved run dir that exists, with the agent's stderr where run_agent
        # writes it (<run dir>/.review-of-legends/<agent>.stderr.txt).
        runs = self.tmp / "runs"
        directory = runs / "pr-4352-c899b3ada777-nzr6mstv"
        (directory / ".review-of-legends").mkdir(parents=True)
        (directory / ".review-of-legends" / "correctness.stderr.txt").write_text(
            "llm_runner: HTTP 529 from http://localhost:3456: overloaded\n", encoding="utf-8"
        )
        store, counts = self.import_lines([
            clone("2026-09-11T03:20:36+00:00", REPO_A, 4352, str(directory)),
            running("2026-09-11T03:20:46+00:00", REPO_A, 4352),
            quorum_failed("2026-09-11T03:48:21+00:00", REPO_A, 4352),
            preserving("2026-09-11T03:48:24+00:00", REPO_A, 4352, str(directory)),
        ])
        self.assertEqual(counts["imported"], 1)
        payload = json.dumps(store.calls, default=str)
        self.assertNotIn(str(directory), payload)
        self.assertNotIn(str(runs), payload)
        self.assertNotIn("pr-4352-", payload)
        self.assertNotIn("nzr6mstv", payload)
        # The stored reason is the one log sentence, not agent output.
        self.assertNotIn("stderr", payload)
        self.assertNotIn("llm_runner", payload)

    def test_reimport_is_a_no_op_without_touching_the_store(self) -> None:
        lines = failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 3)
        store = FullStore()
        first = dashboard_history.import_failed_history(
            store, self.write_log(lines), CONFIGURED
        )
        second = dashboard_history.import_failed_history(
            store, self.write_log(lines), CONFIGURED
        )
        self.assertEqual(first["imported"], 1)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["already_present"], 1)
        self.assertEqual(len(store.calls), 1)
        self.assertEqual(store.updates, [])

    def test_unsupported_metadata_degrades_field_by_field(self) -> None:
        # W1 ships stage/error but not successful_agents yet: the quorum reason
        # must still reach the dashboard, only the missing counter is dropped.
        store, counts = self.import_lines(
            failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 3),
            store=ShippedStore(),
        )
        call = store.calls[0]
        self.assertEqual(call["stage"], "quorum")
        self.assertTrue(call["error"].startswith("only 1/6 agents succeeded; not posting review"))
        self.assertEqual(call["total_agents"], 6)
        self.assertNotIn("successful_agents", call)
        self.assertEqual(counts["imported"], 1)
        self.assertEqual(counts["metadata_unsupported"], 1)
        self.assertEqual(store.updates, [])

    def test_narrow_store_signature_still_imports_and_reports_the_gap(self) -> None:
        store, counts = self.import_lines(
            failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 3),
            store=NarrowStore(),
        )
        call = store.calls[0]
        self.assertEqual(call["status"], "failed")
        self.assertEqual(call["trigger"], "legacy-failure")
        self.assertEqual(call["total_agents"], 6)
        self.assertNotIn("error", call)
        self.assertEqual(counts["imported"], 1)
        self.assertEqual(counts["metadata_unsupported"], 1)
        # Never fall back to update_run: that would emit an event per row and
        # flood the SSE replay on every daemon start.
        self.assertEqual(store.updates, [])

    def test_store_failure_is_counted_and_does_not_abort_the_import(self) -> None:
        class Flaky(FullStore):
            def import_run(self, run_id, repo, pr, head_sha, **kwargs):  # type: ignore[override]
                if pr == 481:
                    raise RuntimeError("db locked")
                return super().import_run(run_id, repo, pr, head_sha, **kwargs)

        lines = (
            failed_attempt_lines(REPO_B, 481, "0b1c478fe4b9", "8sss8eas", 1)
            + failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 3)
        )
        store, counts = self.import_lines(lines, store=Flaky())
        self.assertEqual([call["pr"] for call in store.calls], [4352])
        self.assertEqual(counts["imported"], 1)
        self.assertEqual(counts["store_errors"], 1)

    def test_store_without_status_support_imports_nothing(self) -> None:
        class Ancient:
            def __init__(self) -> None:
                self.calls: List[Any] = []

            def import_run(self, run_id, repo, pr, head_sha):
                self.calls.append(run_id)
                return {"id": run_id}

        store = Ancient()
        counts = dashboard_history.import_failed_history(
            store,
            self.write_log(failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 3)),
            CONFIGURED,
        )
        # Importing without ``status`` would record the failure as a success.
        self.assertEqual(store.calls, [])
        self.assertEqual(counts["store_unsupported"], 1)
        self.assertEqual(counts["imported"], 0)


class RealStoreTest(HistoryTestCase):
    """Against the real SQLite store, not a double."""

    def setUp(self) -> None:
        super().setUp()
        self.store = DashboardStore(self.tmp / "dashboard" / "dashboard.sqlite3")
        self.lines = (
            failed_attempt_lines(REPO_B, 481, "0b1c478fe4b9", "8sss8eas", 1)
            + failed_attempt_lines(REPO_B, 481, "0b1c478fe4b9", "e2xvc9bo", 2)
            + failed_attempt_lines(REPO_B, 481, "0b1c478fe4b9", "ljdaa54v", 4, ok=2)
            + failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 3)
        )

    def test_failed_history_becomes_visible_and_is_labelled_legacy_failure(self) -> None:
        counts = dashboard_history.import_failed_history(
            self.store, self.write_log(self.lines), CONFIGURED
        )
        self.assertEqual(counts["imported"], 4)
        failed = self.store.list_runs(status="failed")
        self.assertEqual(len(failed), 4)
        self.assertEqual({run["trigger"] for run in failed}, {"legacy-failure"})
        self.assertEqual({run["pr"] for run in failed}, {481, 4352})
        # Newest attempt first by its own timestamp (not by insertion order),
        # and the real timestamps are preserved (not "now").
        self.assertEqual([run["created_at"] for run in failed], [
            "2026-09-11T04:20:36+00:00",
            "2026-09-11T03:20:36+00:00",
            "2026-09-11T02:20:36+00:00",
            "2026-09-11T01:20:36+00:00",
        ])
        self.assertEqual([run["pr"] for run in failed], [481, 4352, 481, 481])
        # The operator must see WHY it failed, and that the head is partial.
        alpha = failed[1]
        self.assertEqual(alpha["stage"], "quorum")
        self.assertEqual(
            alpha["error"],
            "only 1/6 agents succeeded; not posting review [%s]"
            % dashboard_history.INCOMPLETE_HEAD_NOTE,
        )
        self.assertEqual(alpha["head_sha"], "c899b3ada777")
        self.assertNotIn("/var/lib", json.dumps(failed, default=str))

    def test_import_emits_no_events(self) -> None:
        dashboard_history.import_failed_history(self.store, self.write_log(self.lines), CONFIGURED)
        dashboard_history.import_failed_history(self.store, self.write_log(self.lines), CONFIGURED)
        self.assertEqual(self.store.events_after(0), [])

    def test_reimport_keeps_exactly_one_row_per_attempt(self) -> None:
        first = dashboard_history.import_failed_history(
            self.store, self.write_log(self.lines), CONFIGURED
        )
        second = dashboard_history.import_failed_history(
            self.store, self.write_log(self.lines), CONFIGURED
        )
        self.assertEqual(first["imported"], 4)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["already_present"], 4)
        self.assertEqual(len(self.store.list_runs()), 4)

    def test_imported_failures_are_immutable_history(self) -> None:
        dashboard_history.import_failed_history(self.store, self.write_log(self.lines), CONFIGURED)
        run_id = self.store.list_runs(status="failed")[0]["id"]
        with self.assertRaises(ValueError):
            self.store.update_run(run_id, "succeeded")

    def test_a_later_import_completes_a_prefix_only_head_without_rewriting_the_outcome(self) -> None:
        # RoL review on PR #12: the first startup's lookup failed (prefix-only
        # row); on the next startup the resolver answers, but the existence
        # check discarded it, so the failure could never be retried.
        full = "c899b3ada7776f91714384601f8a0c8883dd3fe3"
        lines = failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 3)
        first = dashboard_history.import_failed_history(
            self.store, self.write_log(lines), CONFIGURED, resolve_head=lambda repo, pr: None
        )
        self.assertEqual(first["head_prefix_only"], 1)
        row = self.store.list_runs(status="failed")[0]
        self.assertEqual(row["head_sha"], "c899b3ada777")
        second = dashboard_history.import_failed_history(
            self.store, self.write_log(lines), CONFIGURED, resolve_head=lambda repo, pr: full
        )
        self.assertEqual((second["imported"], second["already_present"], second["head_completed"]), (0, 1, 1))
        row = self.store.get_run(row["id"])
        self.assertEqual(row["head_sha"], full)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["stage"], "quorum")
        self.assertEqual(row["error"], "only 1/6 agents succeeded; not posting review")
        self.assertEqual(self.store.events_after(0), [])
        # A moved head (resolver answer with another prefix) leaves the row alone.
        third = dashboard_history.import_failed_history(
            self.store, self.write_log(lines), CONFIGURED, resolve_head=lambda repo, pr: "0" * 40
        )
        self.assertEqual(third["head_completed"], 0)
        self.assertEqual(self.store.get_run(row["id"])["head_sha"], full)

    def _lines_at(self, repo, pr, head12, suffix, started, ok=1, total=6):
        """One failure whose log timestamps start at ``started`` (ISO UTC)."""
        base = dt.datetime.fromisoformat(started)
        stamp = lambda seconds: (base + dt.timedelta(seconds=seconds)).isoformat(timespec="seconds")
        directory = run_dir(pr, head12, suffix)
        return [
            clone(stamp(2), repo, pr, directory),
            running(stamp(12), repo, pr, total),
            quorum_failed(stamp(1600), repo, pr, ok, total),
            preserving(stamp(1603), repo, pr, directory),
        ]

    def _native_failure(self, repo, pr, full, directory):
        """A quorum failure as the live recorder stores it (run_dir = dir name)."""
        live = self.store.start_run(repo, pr, full, trigger="poll")
        self.store.update_run(live["id"], "running", stage="cloning", run_dir=Path(directory).name)
        self.store.update_run(live["id"], "failed", stage="quorum", error="only 1/6 agents succeeded")
        return live

    def test_a_failure_the_recorder_already_stored_is_matched_not_reimported(self) -> None:
        # RoL review on PR #12: a quorum failure the recorder stored under a
        # UUID still leaves its clone/quorum/preserve lines in the log, and
        # the hash-derived id could not recognise it. Identity = run dir name.
        full = "c899b3ada7776f91714384601f8a0c8883dd3fe3"
        directory = run_dir(4352, full[:12], "zz99era1")
        live = self._native_failure(REPO_A, 4352, full, directory)
        same = self._lines_at(REPO_A, 4352, full[:12], "zz99era1", live["created_at"])
        counts = dashboard_history.import_failed_history(
            self.store, self.write_log(self.lines + same), CONFIGURED
        )
        self.assertEqual((counts["imported"], counts["native_matched"]), (4, 1))
        self.assertEqual(len(self.store.attempts_for(REPO_A, 4352, full)), 1)
        self.assertEqual(len(self.store.list_runs(status="failed")), 5, "pre-dashboard history still lands")
        again = dashboard_history.import_failed_history(
            self.store, self.write_log(self.lines + same), CONFIGURED
        )
        self.assertEqual((again["imported"], again["native_matched"], again["already_present"]), (0, 1, 4))

    def test_one_native_row_matches_exactly_one_execution(self) -> None:
        # Reviewer MUST-FIX: one native row must not suppress two distinct log
        # executions of the same head; an unrecorded execution stays importable.
        full = "0b1c478fe4b9" + "0" * 28
        recorded_dir = run_dir(481, full[:12], "zz99nat1")
        live = self._native_failure(REPO_B, 481, full, recorded_dir)
        recorded = self._lines_at(REPO_B, 481, full[:12], "zz99nat1", live["created_at"])
        unrecorded = self._lines_at(REPO_B, 481, full[:12], "zz99nat2", live["created_at"])
        counts = dashboard_history.import_failed_history(
            self.store, self.write_log(recorded + unrecorded), CONFIGURED
        )
        self.assertEqual((counts["imported"], counts["native_matched"]), (1, 1))
        imported = [run for run in self.store.list_runs(status="failed") if run["trigger"] == "legacy-failure"]
        self.assertEqual([run["id"] for run in imported], [dashboard_history.historical_run_id(run_dir(481, full[:12], "zz99nat2"))])
        # Two native rows, two executions: both matched, nothing imported.
        second_dir = run_dir(481, full[:12], "zz99nat2")
        self._native_failure(REPO_B, 481, full, second_dir)
        counts = dashboard_history.import_failed_history(
            self.store, self.write_log(recorded + unrecorded), CONFIGURED
        )
        self.assertEqual((counts["imported"], counts["native_matched"], counts["already_present"]), (0, 2, 0))
        # A native *success* for the same head and a different outcome row do not match a failure.
        posted = self.store.start_run(REPO_A, 9999, "abcdef123456" + "0" * 28, trigger="retry")
        self.store.update_run(posted["id"], "running", stage="cloning", run_dir=Path(run_dir(9999, "abcdef123456", "zz99ok01")).name)
        self.store.update_run(posted["id"], "succeeded")
        other = self._lines_at(REPO_A, 9999, "abcdef123456", "zz99ok02", posted["created_at"])
        counts = dashboard_history.import_failed_history(self.store, self.write_log(other), CONFIGURED)
        self.assertEqual((counts["imported"], counts["native_matched"]), (1, 0))

    def test_live_attempts_are_unaffected_by_the_import(self) -> None:
        live = self.store.start_run(REPO_A, 4352, "c899b3ada7776f91714384601f8a0c8883dd3fe3")
        dashboard_history.import_failed_history(self.store, self.write_log(self.lines), CONFIGURED)
        self.assertEqual(self.store.get_run(live["id"])["status"], "running")
        self.assertEqual(len(self.store.list_runs(status="running")), 1)


class StartupHookTest(HistoryTestCase):
    """The hook ``dashboard_execution.run_startup_imports`` reserves.

    dashboard_execution.py:883-905 imports this module only if present and calls
    ``import_retained_failures(store, lock=lock, logger=log)``.
    """

    class Lock:
        def __init__(self) -> None:
            self.required: List[str] = []

        def require_held(self, purpose: str) -> None:
            self.required.append(purpose)

    def test_hook_signature_matches_the_reserved_call(self) -> None:
        store = FullStore()
        lock = self.Lock()
        messages: List[str] = []
        counts = dashboard_history.import_retained_failures(
            store, lock=lock, logger=messages.append
        )
        self.assertIsInstance(counts, dict)
        self.assertTrue(all(isinstance(value, int) for value in counts.values()))
        self.assertEqual(len(lock.required), 1)
        # No config and no log in a test tree: nothing is imported, and the
        # reason is explicit rather than silent.
        self.assertEqual(store.calls, [])
        self.assertTrue(counts.get("unconfigured") or counts.get("log_missing"))

    def test_hook_reads_repositories_and_log_path_from_config(self) -> None:
        store = FullStore()
        log_path = self.write_log(failed_attempt_lines(REPO_A, 4352, "c899b3ada777", "nzr6mstv", 3))
        counts = dashboard_history.import_retained_failures(
            store,
            lock=self.Lock(),
            logger=lambda message: None,
            config={"repositories": CONFIGURED, "dashboard": {"history_log_path": str(log_path)}},
        )
        self.assertEqual(counts["imported"], 1)
        self.assertEqual(store.calls[0]["repo"], REPO_A)

    def test_hook_tolerates_a_missing_store(self) -> None:
        counts = dashboard_history.import_retained_failures(None, lock=self.Lock())
        self.assertEqual(counts["imported"], 0)

    def test_hook_never_raises_on_a_broken_log(self) -> None:
        broken = self.tmp / "broken.out.log"
        broken.write_bytes(b"\xff\xfe not utf8 \n2026-09-11T03:20:36+00:00 partial line")
        store = FullStore()
        counts = dashboard_history.import_retained_failures(
            store, log_path=broken, configured_repos=CONFIGURED
        )
        self.assertEqual(counts["imported"], 0)
        self.assertEqual(store.calls, [])

    def test_default_log_path_points_at_the_service_log(self) -> None:
        # service.sh:18,35-36 -> <install root>/logs/service.out.log, and the
        # package lives at <install root>/code_review_service/.
        default = dashboard_history.default_log_path()
        self.assertEqual(default.name, "service.out.log")
        self.assertEqual(default.parent.name, "logs")
        self.assertEqual(default.parent.parent, ROOT)


if __name__ == "__main__":
    unittest.main()
