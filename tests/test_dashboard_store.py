"""Durable dashboard store: attempts, retry queue, replayable events.

W1 of .prd/admin-dashboard/loop.md. The dashboard must survive daemon restarts
and serve two concurrent writers (the review daemon and the web API), so the
store is tested for transactional enqueue/claim, immutability of past attempts,
bounded reads/payloads, terminal-transition guards and private permissions.

Concurrency is exercised with real threads against a real SQLite file (each
operation opens its own connection), not with mocks: the property under test is
the transaction boundary, which a mock cannot show.
"""

from __future__ import annotations

import datetime as dt
import json
import multiprocessing
import stat
import sys
import tempfile
import contextlib
import threading
from unittest import mock
import time
import unittest
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import dashboard_execution as dx
from code_review_service import dashboard_store
from code_review_service import service as svc
from code_review_service.dashboard_store import DashboardStore, SupersededConflict, prompt_text_sha
from tests.test_dashboard_web import requires_dashboard_deps


def _enqueue_in_child(db_path: str, barrier, results) -> None:
    """Worker for the cross-process test; must stay importable for spawn()."""
    store = DashboardStore(Path(db_path))
    barrier.wait(30)
    results.put(store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")["id"])


def _claim_in_child(db_path: str, barrier, results) -> None:
    store = DashboardStore(Path(db_path))
    barrier.wait(30)
    claimed = store.claim_retry()
    results.put("" if claimed is None else claimed["id"])


class StoreTestCase(unittest.TestCase):
    """Fresh store on a real temporary file for every test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "dashboard" / "dashboard.sqlite3"
        self.store = DashboardStore(self.db_path)


class RetryQueueTest(StoreTestCase):
    def test_enqueue_returns_queued_request_with_contract_fields(self) -> None:
        request = self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
        self.assertEqual(
            set(request),
            {"id", "status", "repo", "pr", "head_sha", "requested_by", "retry_of", "options", "created_at"},
        )
        self.assertEqual(request["status"], "queued")
        self.assertEqual(request["repo"], "owner/repo")
        self.assertEqual(request["pr"], 7)
        self.assertEqual(request["head_sha"], "abc123")
        self.assertEqual(request["requested_by"], "owner@company.example")
        self.assertIsNone(request["retry_of"])
        self.assertTrue(request["id"])
        self.assertTrue(request["created_at"])

    def test_duplicate_click_reuses_queued_request(self) -> None:
        first = self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
        second = self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["status"], "queued")

    def test_duplicate_click_while_running_reuses_running_request(self) -> None:
        first = self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
        self.store.claim_retry()
        second = self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["status"], "running")

    def test_new_head_sha_is_a_separate_request(self) -> None:
        first = self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
        second = self.store.enqueue_retry("owner/repo", 7, "def456", "owner@company.example")
        self.assertNotEqual(second["id"], first["id"])

    def test_finished_request_does_not_block_a_new_one(self) -> None:
        first = self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
        self.store.claim_retry()
        self.store.finish_retry(first["id"], "failed", error="quorum not reached")
        second = self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(second["status"], "queued")

    def test_concurrent_enqueue_of_same_target_yields_one_request(self) -> None:
        workers = 8
        barrier = threading.Barrier(workers)
        results: list = []
        lock = threading.Lock()

        def enqueue() -> None:
            barrier.wait()
            request = self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
            with lock:
                results.append(request)

        threads = [threading.Thread(target=enqueue) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(results), workers)
        self.assertEqual(len({request["id"] for request in results}), 1, results)

    def test_claim_marks_oldest_queued_request_running(self) -> None:
        first = self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
        second = self.store.enqueue_retry("owner/repo", 8, "bbb222", "owner@company.example")
        claimed = self.store.claim_retry()
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed["id"], first["id"])
        self.assertEqual(claimed["status"], "running")
        second_claim = self.store.claim_retry()
        assert second_claim is not None
        self.assertEqual(second_claim["id"], second["id"])

    def test_claim_returns_none_when_queue_empty(self) -> None:
        self.assertIsNone(self.store.claim_retry())

    def test_concurrent_claims_hand_the_request_to_one_consumer(self) -> None:
        self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
        workers = 8
        barrier = threading.Barrier(workers)
        claims: list = []
        lock = threading.Lock()

        def claim() -> None:
            barrier.wait()
            claimed = self.store.claim_retry()
            with lock:
                claims.append(claimed)

        threads = [threading.Thread(target=claim) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        winners = [claimed for claimed in claims if claimed is not None]
        self.assertEqual(len(winners), 1, claims)

    def test_enqueue_rejects_empty_and_nonpositive_fields(self) -> None:
        with self.assertRaises(ValueError):
            self.store.enqueue_retry("", 7, "abc123", "owner@company.example")
        with self.assertRaises(ValueError):
            self.store.enqueue_retry("owner/repo", 0, "abc123", "owner@company.example")
        with self.assertRaises(ValueError):
            self.store.enqueue_retry("owner/repo", -1, "abc123", "owner@company.example")
        with self.assertRaises(ValueError):
            self.store.enqueue_retry("owner/repo", 7, "   ", "owner@company.example")
        with self.assertRaises(ValueError):
            self.store.enqueue_retry("owner/repo", 7, "abc123", "")

    def test_finish_requires_running_request_and_terminal_status(self) -> None:
        request = self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
        with self.assertRaises(ValueError):
            self.store.finish_retry(request["id"], "succeeded")  # still queued
        self.store.claim_retry()
        with self.assertRaises(ValueError):
            self.store.finish_retry(request["id"], "running")  # not terminal
        self.store.finish_retry(request["id"], "succeeded", run_id="run-1")
        with self.assertRaises(ValueError):
            self.store.finish_retry(request["id"], "failed")  # already terminal

    def test_finish_rejects_unknown_request(self) -> None:
        with self.assertRaises(ValueError):
            self.store.finish_retry("does-not-exist", "failed")


class CrossProcessTest(StoreTestCase):
    """The daemon and the web API are separate processes, not threads.

    Thread tests can pass on a store that only holds a Python-level lock, so the
    file-level transaction boundary is proven with real subprocesses.
    """

    workers = 4

    def _run_workers(self, target) -> list:
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(self.workers)
        results = context.Queue()
        processes = [
            context.Process(target=target, args=(str(self.db_path), barrier, results))
            for _ in range(self.workers)
        ]
        for process in processes:
            process.start()
        collected = [results.get(timeout=60) for _ in range(self.workers)]
        for process in processes:
            process.join(timeout=60)
            self.assertEqual(process.exitcode, 0, "worker exited %s" % process.exitcode)
        return collected

    def test_concurrent_processes_enqueue_one_request(self) -> None:
        ids = self._run_workers(_enqueue_in_child)
        self.assertEqual(len(set(ids)), 1, ids)

    def test_concurrent_processes_claim_one_request(self) -> None:
        self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
        claims = self._run_workers(_claim_in_child)
        self.assertEqual(len([claim for claim in claims if claim]), 1, claims)


class RunAttemptTest(StoreTestCase):
    def test_repeated_attempts_are_independent_and_preserved(self) -> None:
        first = self.store.start_run("owner/repo", 7, "abc123", title="Add dashboard")
        self.store.update_run(first["id"], "failed", error="quorum not reached")
        second = self.store.start_run("owner/repo", 7, "abc123", trigger="retry", retry_of=first["id"])

        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(second["status"], "running")
        self.assertEqual(second["retry_of"], first["id"])

        preserved = self.store.get_run(first["id"])
        assert preserved is not None
        self.assertEqual(preserved["status"], "failed")
        self.assertEqual(preserved["error"], "quorum not reached")
        self.assertEqual(preserved["title"], "Add dashboard")
        self.assertEqual(preserved["trigger"], "poll")

        listed = self.store.list_runs(repo="owner/repo")
        self.assertEqual([run["id"] for run in listed], [second["id"], first["id"]])

    def test_get_run_returns_none_for_unknown_run(self) -> None:
        self.assertIsNone(self.store.get_run("nope"))

    def test_update_run_allows_only_allowlisted_fields(self) -> None:
        run = self.store.start_run("owner/repo", 7, "abc123")
        self.store.update_run(
            run["id"],
            "running",
            stage="agents",
            agents=[{"id": "astra", "status": "running"}],
            successful_agents=1,
            total_agents=3,
        )
        stored = self.store.get_run(run["id"])
        assert stored is not None
        self.assertEqual(stored["stage"], "agents")
        self.assertEqual(stored["agents"], [{"id": "astra", "status": "running"}])
        self.assertEqual(stored["successful_agents"], 1)
        self.assertEqual(stored["total_agents"], 3)

        with self.assertRaises(ValueError):
            self.store.update_run(run["id"], "running", head_sha="rewritten")

    def test_rejected_update_leaves_no_partial_write_and_no_event(self) -> None:
        run = self.store.start_run("owner/repo", 7, "abc123")
        self.store.update_run(run["id"], "running", stage="fetching")
        cursor = self.store.events_after(0)[-1]["id"]

        with self.assertRaises(ValueError):
            self.store.update_run(run["id"], "running", stage="agents", head_sha="rewritten")

        stored = self.store.get_run(run["id"])
        assert stored is not None
        self.assertEqual(stored["stage"], "fetching")
        self.assertEqual(self.store.events_after(cursor), [])

    def test_update_run_rejects_unknown_status_and_unknown_run(self) -> None:
        run = self.store.start_run("owner/repo", 7, "abc123")
        with self.assertRaises(ValueError):
            self.store.update_run(run["id"], "cancelled")
        with self.assertRaises(ValueError):
            self.store.update_run("nope", "failed")

    def test_terminal_run_cannot_regress_to_running(self) -> None:
        run = self.store.start_run("owner/repo", 7, "abc123")
        self.store.update_run(run["id"], "succeeded", review_url="https://example.invalid/r/1")
        with self.assertRaises(ValueError):
            self.store.update_run(run["id"], "running")
        stored = self.store.get_run(run["id"])
        assert stored is not None
        self.assertEqual(stored["status"], "succeeded")

    def test_terminal_run_cannot_be_rewritten_to_another_outcome(self) -> None:
        run = self.store.start_run("owner/repo", 7, "abc123")
        self.store.update_run(run["id"], "failed", error="quorum not reached")
        with self.assertRaises(ValueError):
            self.store.update_run(run["id"], "succeeded")
        stored = self.store.get_run(run["id"])
        assert stored is not None
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["error"], "quorum not reached")

    def test_start_run_rejects_empty_and_nonpositive_fields(self) -> None:
        with self.assertRaises(ValueError):
            self.store.start_run("", 7, "abc123")
        with self.assertRaises(ValueError):
            self.store.start_run("owner/repo", 0, "abc123")
        with self.assertRaises(ValueError):
            self.store.start_run("owner/repo", 7, "")


class ImportLegacyRunTest(StoreTestCase):
    """Backfill of the pre-dashboard JSON ledger.

    Signature and status vocabulary are pinned by the real caller,
    ``dashboard_execution.import_legacy_ledger`` (its ``store.import_run`` call).
    Imports must be idempotent (the daemon re-imports on every start) and must
    not emit events, or a several-hundred-PR ledger would flood the SSE replay.
    """

    LEGACY_ID = "legacy-" + "a" * 24

    def test_baseline_entry_imports_as_skipped_never_reviewed(self) -> None:
        imported = self.store.import_run(
            self.LEGACY_ID,
            "owner/repo",
            7,
            "abc123",
            status="skipped",
            trigger="baseline",
            created_at="2024-01-02T03:04:05+00:00",
            total_agents=None,
        )
        self.assertEqual(imported["id"], self.LEGACY_ID)
        self.assertEqual(imported["status"], "skipped")
        self.assertEqual(imported["trigger"], "baseline")
        self.assertIsNone(imported["total_agents"])
        self.assertEqual(
            [run["id"] for run in self.store.list_runs(status="skipped")], [self.LEGACY_ID]
        )

    def test_reviewed_entry_imports_as_succeeded_legacy(self) -> None:
        imported = self.store.import_run(
            self.LEGACY_ID,
            "owner/repo",
            7,
            "abc123",
            status="succeeded",
            trigger="legacy",
            created_at="2024-01-02T03:04:05+00:00",
            total_agents=3,
        )
        self.assertEqual(imported["status"], "succeeded")
        self.assertEqual(imported["trigger"], "legacy")
        self.assertEqual(imported["total_agents"], 3)

    def test_original_timestamp_is_preserved_verbatim(self) -> None:
        self.store.import_run(
            self.LEGACY_ID,
            "owner/repo",
            7,
            "abc123",
            created_at="2024-01-02T03:04:05+00:00",
        )
        stored = self.store.get_run(self.LEGACY_ID)
        assert stored is not None
        self.assertEqual(stored["created_at"], "2024-01-02T03:04:05+00:00")
        self.assertEqual(stored["updated_at"], "2024-01-02T03:04:05+00:00")

    def test_unknown_timestamp_stays_unknown_and_is_not_invented(self) -> None:
        self.store.import_run(self.LEGACY_ID, "owner/repo", 7, "abc123", created_at="")
        stored = self.store.get_run(self.LEGACY_ID)
        assert stored is not None
        # A ledger entry without reviewed_at has no date; inventing "now" would
        # claim a review happened today. The UI labels this unknown instead.
        self.assertEqual(stored["created_at"], "")
        self.assertEqual(stored["updated_at"], "")

    def test_reimport_is_idempotent_and_keeps_the_first_row(self) -> None:
        first = self.store.import_run(
            self.LEGACY_ID, "owner/repo", 7, "abc123", created_at="2024-01-02T03:04:05+00:00"
        )
        again = self.store.import_run(
            self.LEGACY_ID, "owner/repo", 7, "abc123", created_at="2026-09-11T00:00:00+00:00"
        )
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["created_at"], "2024-01-02T03:04:05+00:00")
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_import_emits_no_events(self) -> None:
        self.store.import_run(self.LEGACY_ID, "owner/repo", 7, "abc123")
        self.store.import_run(self.LEGACY_ID, "owner/repo", 7, "abc123")
        self.assertEqual(self.store.events_after(0), [])

    def test_imported_rows_are_immutable_history(self) -> None:
        self.store.import_run(self.LEGACY_ID, "owner/repo", 7, "abc123", status="skipped")
        with self.assertRaises(ValueError):
            self.store.update_run(self.LEGACY_ID, "running")
        with self.assertRaises(ValueError):
            self.store.update_run(self.LEGACY_ID, "succeeded")
        stored = self.store.get_run(self.LEGACY_ID)
        assert stored is not None
        self.assertEqual(stored["status"], "skipped")

    def test_import_rejects_live_status_and_invalid_identity(self) -> None:
        with self.assertRaises(ValueError):
            self.store.import_run(self.LEGACY_ID, "owner/repo", 7, "abc123", status="running")
        with self.assertRaises(ValueError):
            self.store.import_run("", "owner/repo", 7, "abc123")
        with self.assertRaises(ValueError):
            self.store.import_run(self.LEGACY_ID, "", 7, "abc123")
        with self.assertRaises(ValueError):
            self.store.import_run(self.LEGACY_ID, "owner/repo", 0, "abc123")
        with self.assertRaises(ValueError):
            self.store.import_run(self.LEGACY_ID, "owner/repo", 7, "")

    def test_accepts_the_exact_call_the_daemon_makes(self) -> None:
        # Mirrors dashboard_execution.import_legacy_ledger's store.import_run
        # call argument-for-argument.
        self.store.import_run(
            self.LEGACY_ID,
            "owner/repo",
            7,
            "abc123",
            status="skipped",
            trigger="baseline",
            created_at="",
            stage="",
            error="",
            total_agents=None,
        )
        self.assertIsNotNone(self.store.get_run(self.LEGACY_ID))

    def test_unverified_post_imports_with_its_stage_and_reason(self) -> None:
        """A legacy entry whose review POST outcome was unknown is not a success."""
        self.store.import_run(
            self.LEGACY_ID,
            "owner/repo",
            7,
            "abc123",
            status="interrupted",
            trigger="legacy",
            created_at="2024-01-02T03:04:05+00:00",
            stage="posting-unverified",
            error="review POST outcome unknown",
            total_agents=3,
        )
        stored = self.store.get_run(self.LEGACY_ID)
        assert stored is not None
        self.assertEqual(stored["status"], "interrupted")
        self.assertEqual(stored["stage"], "posting-unverified")
        self.assertEqual(stored["error"], "review POST outcome unknown")

    def test_import_preserves_a_known_agent_success_count(self) -> None:
        self.store.import_run(
            self.LEGACY_ID,
            "owner/repo",
            7,
            "abc123",
            status="succeeded",
            trigger="legacy",
            successful_agents=2,
            total_agents=3,
        )
        stored = self.store.get_run(self.LEGACY_ID)
        assert stored is not None
        self.assertEqual(stored["successful_agents"], 2)
        self.assertEqual(stored["total_agents"], 3)

    def test_import_leaves_an_unknown_success_count_null(self) -> None:
        self.store.import_run(self.LEGACY_ID, "owner/repo", 7, "abc123", total_agents=3)
        stored = self.store.get_run(self.LEGACY_ID)
        assert stored is not None
        self.assertIsNone(stored["successful_agents"])

    def test_import_rejects_a_non_numeric_success_count(self) -> None:
        with self.assertRaises(ValueError):
            self.store.import_run(
                self.LEGACY_ID, "owner/repo", 7, "abc123", successful_agents="two"
            )
        with self.assertRaises(ValueError):
            self.store.import_run(
                self.LEGACY_ID, "owner/repo", 7, "abc123", successful_agents=True
            )

    def test_imported_stage_and_error_are_bounded(self) -> None:
        self.store.import_run(
            self.LEGACY_ID,
            "owner/repo",
            7,
            "abc123",
            stage="s" * 100_000,
            error="e" * 100_000,
        )
        stored = self.store.get_run(self.LEGACY_ID)
        assert stored is not None
        self.assertLessEqual(len(stored["stage"]), self.store.max_text_chars)
        self.assertLessEqual(len(stored["error"]), self.store.max_text_chars)

    def test_history_is_ordered_by_attempt_time_with_unknown_dates_last(self) -> None:
        # RoL review on PR #12: insertion order pushed a recent review behind
        # backfilled years-old failures.
        live = self.store.start_run("owner/repo", 8, "bbb222")
        self.store.import_run("legacy-old", "owner/repo", 7, "abc123", created_at="2024-01-02T03:04:05+00:00")
        self.store.import_run("legacy-unknown", "owner/repo", 6, "abc000", created_at="")
        self.store.import_run("legacy-newer", "owner/repo", 5, "abc999", created_at="2025-06-01T00:00:00+00:00")
        self.assertEqual(
            [run["id"] for run in self.store.list_runs()],
            [live["id"], "legacy-newer", "legacy-old", "legacy-unknown"],
        )

    def test_attempts_for_finds_every_row_of_one_head(self) -> None:
        live = self.store.start_run("owner/repo", 7, "abc123")
        self.store.import_run(self.LEGACY_ID, "owner/repo", 7, "abc123")
        self.store.start_run("owner/repo", 7, "other")
        rows = self.store.attempts_for("owner/repo", 7, "abc123")
        self.assertEqual([row["id"] for row in rows], [live["id"], self.LEGACY_ID])
        self.assertEqual(self.store.attempts_for("owner/repo", 7, "nope"), [])

    def test_attempts_for_trigger_filter_is_applied_before_the_bound(self) -> None:
        # Reviewer MUST-FIX: an existence check must not be hidden behind a
        # page of imported history for the same head.
        for index in range(25):
            self.store.import_run("legacy-%d" % index, "owner/repo", 7, "abc123", trigger="legacy-failure", status="failed")
        live = self.store.start_run("owner/repo", 7, "abc123", trigger="retry")
        self.assertEqual(len(self.store.attempts_for("owner/repo", 7, "abc123", limit=20)), 20)
        rows = self.store.attempts_for("owner/repo", 7, "abc123", limit=1, triggers=("poll", "retry"))
        self.assertEqual([row["id"] for row in rows], [live["id"]])
        self.assertEqual(self.store.attempts_for("owner/repo", 7, "abc123", triggers=()), [])

    def test_has_attempt_is_an_existence_query_over_all_evidence(self) -> None:
        full = "c899b3ada7776f91714384601f8a0c8883dd3fe3"
        for index in range(30):
            self.store.import_run("hist-%d" % index, "owner/repo", 7, full, trigger="legacy-failure", status="failed")
        live = self.store.start_run("owner/repo", 7, full, trigger="poll")
        self.store.update_run(live["id"], "failed", error="quorum")
        native = ("poll", "retry")
        self.assertTrue(self.store.has_attempt("owner/repo", 7, full[:12], native))
        self.assertTrue(self.store.has_attempt("owner/repo", 7, full, native, status="failed"))
        self.assertFalse(self.store.has_attempt("owner/repo", 7, full, native, status="succeeded"))
        self.assertFalse(self.store.has_attempt("owner/repo", 7, "deadbeef", native))
        self.assertFalse(self.store.has_attempt("owner/repo", 8, full[:12], native))
        self.assertFalse(self.store.has_attempt("owner/repo", 7, full, ("legacy",)), "history rows are not evidence")
        self.assertFalse(self.store.has_attempt("owner/repo", 7, full, ()))
        self.store.update_run(live["id"], "failed", run_dir="pr-7-c899b3ada777-abc123")
        self.assertEqual(self.store.get_run(live["id"])["run_dir"], "pr-7-c899b3ada777-abc123")
        self.assertTrue(self.store.has_attempt("owner/repo", 7, full[:12], native, run_dir="pr-7-c899b3ada777-abc123"))
        self.assertFalse(self.store.has_attempt("owner/repo", 7, full[:12], native, run_dir="pr-7-c899b3ada777-other"))

    def test_run_dir_column_is_added_to_a_database_created_before_it(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("ALTER TABLE runs DROP COLUMN run_dir")
        reopened = DashboardStore(self.db_path)
        run = reopened.start_run("owner/repo", 1, "abc")
        reopened.update_run(run["id"], "running", run_dir="pr-1-abc-x")
        self.assertEqual(reopened.get_run(run["id"])["run_dir"], "pr-1-abc-x")

    def test_complete_head_only_widens_a_prefix_on_a_terminal_row(self) -> None:
        full = "c899b3ada7776f91714384601f8a0c8883dd3fe3"
        self.store.import_run("hist-1", "owner/repo", 9, full[:12], status="failed", error="boom [partial]")
        self.assertFalse(self.store.complete_head("hist-1", "deadbeef" + full[8:]), "must start with the prefix")
        self.assertFalse(self.store.complete_head("hist-1", full[:12]), "no shorter or equal head")
        self.assertTrue(self.store.complete_head("hist-1", full, error="boom"))
        row = self.store.get_run("hist-1")
        self.assertEqual((row["head_sha"], row["status"], row["error"]), (full, "failed", "boom"))
        self.assertFalse(self.store.complete_head("hist-1", full), "idempotent")
        live = self.store.start_run("owner/repo", 10, full[:12])
        self.assertFalse(self.store.complete_head(live["id"], full), "live rows are the recorder's")
        self.assertFalse(self.store.complete_head("missing", full))
        self.assertEqual([e for e in self.store.events_after(0) if e["run_id"] == "hist-1"], [])


def _agent_result(agent_id: str, body_chars: int = 10) -> dict:
    """What run_agent returns for a success (findings, verdict, the runner's llm fields)."""
    return {
        "agent": agent_id,
        "summary": "ok",
        "overall_correctness": "patch is correct",
        "findings": [{"path": "a.py", "line": 1, "priority": 2, "title": "[P2] t", "body": "x" * body_chars}],
        "llm": {"model": "grok-4.7[1m]", "input_tokens": 10, "output_tokens": 2},
    }


class AgentResultsTest(StoreTestCase):
    """A failed attempt keeps its successful agents' results for a "failed agents" retry."""

    def test_results_round_trip_and_only_their_ids_reach_the_row_and_the_event(self) -> None:
        run = self.store.start_run("owner/repo", 7, "abc123")
        cursor = self.store.events_after(0)[-1]["id"]
        kept = [_agent_result("correctness"), _agent_result("security")]
        self.store.update_run(run["id"], "failed", stage="quorum", agent_results=kept)

        self.assertEqual(self.store.get_agent_results(run["id"]), {"correctness": kept[0], "security": kept[1]})
        row = self.store.get_run(run["id"])
        self.assertEqual(row["reusable_agents"], ["correctness", "security"])
        self.assertNotIn("agent_results", row, "run rows never carry the results")
        self.assertEqual(self.store.list_runs()[0]["reusable_agents"], ["correctness", "security"])
        event = self.store.events_after(cursor)[0]
        self.assertEqual(event["payload"], {"status": "failed", "stage": "quorum", "reusable_agents": ["correctness", "security"]})

    def test_a_run_without_kept_results_has_none(self) -> None:
        run = self.store.start_run("owner/repo", 7, "abc123")
        self.store.update_run(run["id"], "failed", error="clone failed")
        self.assertEqual(self.store.get_agent_results(run["id"]), {})
        self.assertEqual(self.store.get_run(run["id"])["reusable_agents"], [])
        self.assertEqual(self.store.get_agent_results("missing"), {})
        empty = self.store.start_run("owner/repo", 8, "abc123")
        self.store.update_run(empty["id"], "failed", agent_results=[])
        self.assertEqual((self.store.get_agent_results(empty["id"]), self.store.get_run(empty["id"])["reusable_agents"]), ({}, []))

    def test_only_successes_within_the_caps_are_kept(self) -> None:
        from code_review_service import dashboard_store as ds

        big = ds.MAX_AGENT_RESULT_CHARS - 1_000
        candidates = [
            {"agent": "failed", "summary": "timed out", "findings": [], "error": "timeout"},
            {"agent": "Not An Id", "summary": "ok", "findings": []},
            {"agent": "null-findings", "summary": "ok", "findings": None},  # a success, kept whole as returned
            {"agent": "unencodable", "summary": "ok", "findings": [], "llm": {"x": {1, 2}}},
            _agent_result("oversized", ds.MAX_AGENT_RESULT_CHARS),
            _agent_result("a", big),
            _agent_result("a", 5),  # a second result for one agent: the first wins
            _agent_result("b", big),
            _agent_result("c", big),
            _agent_result("d", big),
            _agent_result("e", big),  # would take the list past MAX_AGENT_RESULTS_CHARS
            _agent_result("small"),  # still fits after the one left out
        ]
        kept = ds.bounded_agent_results(candidates)
        self.assertEqual([item["agent"] for item in kept], ["null-findings", "a", "b", "c", "d", "small"])
        self.assertEqual(kept[1], candidates[5])
        self.assertLessEqual(len(json.dumps(kept, ensure_ascii=False, sort_keys=True)), ds.MAX_AGENT_RESULTS_CHARS)
        self.assertEqual(ds.bounded_agent_results(kept), kept, "idempotent")
        for junk in (None, "x", {"agent": "a"}, 7):
            self.assertEqual(ds.bounded_agent_results(junk), [])

        run = self.store.start_run("owner/repo", 7, "abc123")
        self.store.update_run(run["id"], "failed", agent_results=candidates)
        self.assertEqual(list(self.store.get_agent_results(run["id"])), ["null-findings", "a", "b", "c", "d", "small"])
        many = [_agent_result("agent-%d" % index) for index in range(ds.MAX_AGENT_RESULTS + 5)]
        self.assertEqual(len(ds.bounded_agent_results(many)), ds.MAX_AGENT_RESULTS)

    def test_columns_are_added_to_a_database_created_before_them(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("ALTER TABLE runs DROP COLUMN agent_results")
            conn.execute("ALTER TABLE runs DROP COLUMN reusable_agents")
        reopened = DashboardStore(self.db_path)
        run = reopened.start_run("owner/repo", 1, "abc")
        reopened.update_run(run["id"], "failed", agent_results=[_agent_result("tests")])
        self.assertEqual(reopened.get_run(run["id"])["reusable_agents"], ["tests"])
        self.assertEqual(reopened.get_agent_results(run["id"])["tests"]["summary"], "ok")

    def test_kept_results_do_not_squeeze_the_agent_rows_out_of_the_terminal_event(self) -> None:
        """The recorder fits the rows to the event; the results travel as ids, so they cost nothing there."""
        recorder = dx.DashboardRecorder(self.store, logger=lambda message: None)
        recorder.begin("owner/repo", 7, "abc123")
        rows = {agent_id: {"agent": agent_id, "status": "done", "summary": "P2 x 1", "model": "grok-4.7[1m]"}
                for agent_id in ("correctness", "security", "tests")}
        recorder.failed(
            "only 3/4 agents succeeded",
            stage="quorum",
            agents=dx.normalize_agent_rows(rows),
            agent_results=[_agent_result(agent_id, 20_000) for agent_id in rows],
        )
        stored = self.store.get_run(recorder.run_id)
        self.assertEqual([row.get("summary") for row in stored["agents"]], ["P2 x 1"] * 3, "rows keep their summaries")
        event = self.store.events_after(0)[-1]
        self.assertEqual(event["payload"]["status"], "failed", "the terminal event is not a truncation marker")
        self.assertEqual(event["payload"]["reusable_agents"], ["correctness", "security", "tests"])
        self.assertEqual(sorted(self.store.get_agent_results(recorder.run_id)), ["correctness", "security", "tests"])


class RetryRunLinkTest(StoreTestCase):
    """A clicked retry must be traceable to its live run immediately."""

    def _claimed_request(self) -> dict:
        self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
        claimed = self.store.claim_retry()
        assert claimed is not None
        return claimed

    def test_start_run_links_the_claimed_request_atomically(self) -> None:
        request = self._claimed_request()
        cursor = self.store.events_after(0)[-1]["id"]
        run = self.store.start_run(
            "owner/repo", 7, "abc123", trigger="retry", request_id=request["id"]
        )

        self.assertEqual(run["request_id"], request["id"])
        detail = self.store.get_retry(request["id"])
        assert detail is not None
        self.assertEqual(detail["run_id"], run["id"])

        events = self.store.events_after(cursor)
        self.assertEqual([event["kind"] for event in events], ["run_started"])
        self.assertEqual(events[0]["payload"]["request_id"], request["id"])

    def test_run_without_request_has_no_link(self) -> None:
        run = self.store.start_run("owner/repo", 7, "abc123")
        self.assertIsNone(run["request_id"])

    def test_unknown_request_aborts_the_whole_start(self) -> None:
        events_before = self.store.events_after(0)
        with self.assertRaises(ValueError):
            self.store.start_run("owner/repo", 7, "abc123", request_id="does-not-exist")
        self.assertEqual(self.store.list_runs(), [])
        self.assertEqual(self.store.events_after(0), events_before)

    def test_unclaimed_request_cannot_be_linked(self) -> None:
        request = self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
        with self.assertRaises(ValueError):
            self.store.start_run("owner/repo", 7, "abc123", request_id=request["id"])
        self.assertEqual(self.store.list_runs(), [])

    def test_request_cannot_be_relinked_to_a_second_run(self) -> None:
        request = self._claimed_request()
        first = self.store.start_run("owner/repo", 7, "abc123", request_id=request["id"])
        with self.assertRaises(ValueError):
            self.store.start_run("owner/repo", 7, "abc123", request_id=request["id"])
        detail = self.store.get_retry(request["id"])
        assert detail is not None
        self.assertEqual(detail["run_id"], first["id"])
        self.assertEqual(len(self.store.list_runs()), 1)


class RetryReadTest(StoreTestCase):
    """Queue hydration for the dashboard (W3 reads these)."""

    def test_get_retry_returns_detail_including_link_and_error(self) -> None:
        request = self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
        detail = self.store.get_retry(request["id"])
        assert detail is not None
        self.assertEqual(
            set(detail),
            {
                "id",
                "status",
                "repo",
                "pr",
                "head_sha",
                "requested_by",
                "retry_of",
                "options",
                "created_at",
                "run_id",
                "error",
                "updated_at",
            },
        )
        self.assertIsNone(detail["run_id"])
        self.assertIsNone(detail["error"])

        self.store.claim_retry()
        self.store.finish_retry(request["id"], "failed", run_id="run-1", error="quorum not reached")
        detail = self.store.get_retry(request["id"])
        assert detail is not None
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["run_id"], "run-1")
        self.assertEqual(detail["error"], "quorum not reached")

    def test_get_retry_returns_none_for_unknown_request(self) -> None:
        self.assertIsNone(self.store.get_retry("nope"))

    def test_list_retries_newest_first_with_detail_row_shape(self) -> None:
        first = self.store.enqueue_retry("owner/one", 1, "aaa", "owner@company.example")
        second = self.store.enqueue_retry("owner/two", 2, "bbb", "owner@company.example")
        rows = self.store.list_retries()
        self.assertEqual([row["id"] for row in rows], [second["id"], first["id"]])
        self.assertEqual(
            set(rows[0]),
            {
                "id",
                "status",
                "repo",
                "pr",
                "head_sha",
                "requested_by",
                "retry_of",
                "options",
                "created_at",
                "run_id",
                "error",
                "updated_at",
            },
        )

    def test_list_retries_exposes_the_linked_run_and_terminal_error(self) -> None:
        """The queue listing is what the dashboard renders; a row must explain itself.

        Without these columns the API projects ``run_id``/``error`` as null, so a
        running retry has no link to its attempt and a failed one shows no reason.
        """
        request = self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
        self.store.claim_retry()
        run = self.store.start_run("owner/repo", 7, "abc123", request_id=request["id"])

        running_row = self.store.list_retries(status="running")[0]
        self.assertEqual(running_row["run_id"], run["id"])
        self.assertIsNone(running_row["error"])

        self.store.finish_retry(
            request["id"], "failed", run_id=run["id"], error="quorum not reached"
        )
        failed_row = self.store.list_retries(status="failed")[0]
        self.assertEqual(failed_row["run_id"], run["id"])
        self.assertEqual(failed_row["error"], "quorum not reached")

    @requires_dashboard_deps  # reads the web tier's own tuple, not a copy of it
    def test_list_retries_covers_every_field_the_api_projects(self) -> None:
        # dashboard_web.RETRY_FIELDS - project_retry_request reads exactly these
        # and nulls whatever the store omits.
        from code_review_service import dashboard_web

        request = self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
        listed = self.store.list_retries()[0]
        detail = self.store.get_retry(request["id"])
        assert detail is not None
        projected = set(dashboard_web.RETRY_FIELDS)
        self.assertEqual(projected - set(listed), set(), "list_retries rows miss fields the API projects")
        self.assertEqual(projected - set(detail), set(), "get_retry misses fields the API projects")

    def test_list_retries_filters_by_status_and_repo(self) -> None:
        first = self.store.enqueue_retry("owner/one", 1, "aaa", "owner@company.example")
        second = self.store.enqueue_retry("owner/two", 2, "bbb", "owner@company.example")
        self.store.claim_retry()  # oldest first: `first` becomes running

        self.assertEqual(
            [row["id"] for row in self.store.list_retries(status="queued")], [second["id"]]
        )
        self.assertEqual(
            [row["id"] for row in self.store.list_retries(status="running")], [first["id"]]
        )
        self.assertEqual(
            [row["id"] for row in self.store.list_retries(repo="owner/two")], [second["id"]]
        )
        self.assertEqual(self.store.list_retries(status="queued", repo="owner/one"), [])

    def test_list_retries_is_bounded_and_clamped(self) -> None:
        for index in range(5):
            self.store.enqueue_retry("owner/repo", index + 1, "sha%d" % index, "owner@company.example")
        self.assertEqual(len(self.store.list_retries(limit=2)), 2)
        self.assertEqual(len(self.store.list_retries(limit=2, offset=4)), 1)
        self.assertEqual(len(self.store.list_retries(limit=10_000)), 5)
        self.assertEqual(len(self.store.list_retries(limit=0)), 1)
        self.assertEqual(len(self.store.list_retries(limit=-5)), 1)
        self.assertEqual(len(self.store.list_retries(offset=-5)), 5)


class LatestAttemptForHeadTest(StoreTestCase):
    """Reviewer MUST-FIX: ``rows[-1]`` of a bounded ``attempts_for`` window is the
    newest *within the window*, not the newest attempt, once a head accumulates
    more attempts than the window. ``latest_attempt_for_head`` queries the newest
    row directly and must not drop that distinction.
    """

    def test_the_true_newest_attempt_is_returned_past_the_old_window(self) -> None:
        window = 200  # dashboard_web.py's MAX_LIST_LIMIT, the bound the old code used
        for index in range(window + 5):
            run = self.store.start_run("owner/repo", 7, "abc123", trigger="poll")
            self.store.update_run(run["id"], "succeeded", review_url="https://github.invalid/r/%d" % index)
        newest = self.store.start_run("owner/repo", 7, "abc123", trigger="retry")
        self.store.update_run(newest["id"], "interrupted", stage=dx.POSTING_STAGE)

        # The bug this replaces: oldest-first, bounded, then take the last row.
        old_way = self.store.attempts_for("owner/repo", 7, "abc123", limit=window)
        self.assertNotEqual(old_way[-1]["id"], newest["id"], "the bug: the window never reaches the true newest row")

        latest = self.store.latest_attempt_for_head("owner/repo", 7, "abc123")
        assert latest is not None
        self.assertEqual(latest["id"], newest["id"])
        self.assertEqual(latest["stage"], dx.POSTING_STAGE)

    def test_no_attempts_for_the_head_is_none(self) -> None:
        self.store.start_run("owner/repo", 7, "abc123")
        self.assertIsNone(self.store.latest_attempt_for_head("owner/repo", 7, "nope"))


class ActiveActivityForPrTest(StoreTestCase):
    """Reviewer MUST-FIX: a PR-scoped existence query, not a page of repo-wide rows."""

    def test_a_queued_request_beyond_one_page_is_still_found(self) -> None:
        target = self.store.enqueue_retry("owner/repo", 9, "h" * 40, "owner@company.example")
        for index in range(210):
            self.store.enqueue_retry("owner/repo", 1000 + index, "a" * 40, "owner@company.example")

        request, run = self.store.active_activity_for_pr("owner/repo", 9)
        assert request is not None
        self.assertEqual(request["id"], target["id"])
        self.assertIsNone(run)

    def test_a_running_attempt_beyond_one_page_is_still_found(self) -> None:
        target = self.store.start_run("owner/repo", 9, "h" * 40)
        for index in range(210):
            self.store.start_run("owner/repo", 2000 + index, "a" * 40)

        request, run = self.store.active_activity_for_pr("owner/repo", 9)
        self.assertIsNone(request)
        assert run is not None
        self.assertEqual(run["id"], target["id"])

    def test_no_activity_is_none_and_none(self) -> None:
        self.assertEqual(self.store.active_activity_for_pr("owner/repo", 1), (None, None))

    def test_another_pr_does_not_block(self) -> None:
        self.store.enqueue_retry("owner/repo", 1, "a" * 40, "owner@company.example")
        self.assertEqual(self.store.active_activity_for_pr("owner/repo", 2), (None, None))


class RereviewAtomicAdmissionTest(StoreTestCase):
    """Reviewer MUST-FIX: admission for a rereview is atomic inside the write
    transaction, not a separate check-then-enqueue the web layer can race.
    """

    def test_a_second_rereview_of_the_same_pr_on_any_head_is_refused(self) -> None:
        first = self.store.enqueue_retry(
            "owner/repo", 7, "a" * 40, "owner@company.example", options={"kind": "rereview", "scope": "all"}
        )
        self.assertEqual(first["status"], "queued")
        with self.assertRaises(dashboard_store.PrBusyError):
            self.store.enqueue_retry(
                "owner/repo", 7, "b" * 40, "owner@company.example", options={"kind": "rereview", "scope": "all"}
            )

    def test_a_claimed_rereview_still_refuses_another_on_any_head(self) -> None:
        """A claimed (running) request is as busy as a queued one, same head or not:
        the same head must not be handed back the running request as a success."""
        first = self.store.enqueue_retry(
            "owner/repo", 7, "a" * 40, "owner@company.example", options={"kind": "rereview", "scope": "all"}
        )
        claimed = self.store.claim_retry()
        self.assertEqual((claimed["id"], claimed["status"]), (first["id"], "running"))
        for head in ("a" * 40, "b" * 40):
            with self.subTest(head=head[:1]), self.assertRaises(dashboard_store.PrBusyError):
                self.store.enqueue_retry(
                    "owner/repo", 7, head, "owner@company.example", options={"kind": "rereview", "scope": "all"}
                )

    def test_a_running_attempt_of_the_pr_on_any_head_refuses_a_rereview(self) -> None:
        self.store.start_run("owner/repo", 7, "c" * 40)
        with self.assertRaises(dashboard_store.PrBusyError):
            self.store.enqueue_retry(
                "owner/repo", 7, "a" * 40, "owner@company.example", options={"kind": "rereview", "scope": "all"}
            )

    def test_a_plain_retry_is_not_subject_to_the_pr_wide_check(self) -> None:
        """Regression guard: only the rereview kind gets the PR-wide refusal."""
        self.store.start_run("owner/repo", 7, "c" * 40)
        queued = self.store.enqueue_retry("owner/repo", 7, "a" * 40, "owner@company.example")
        self.assertEqual(queued["status"], "queued")

    def test_a_rereview_of_a_different_pr_is_unaffected(self) -> None:
        self.store.enqueue_retry(
            "owner/repo", 7, "a" * 40, "owner@company.example", options={"kind": "rereview", "scope": "all"}
        )
        other = self.store.enqueue_retry(
            "owner/repo", 8, "b" * 40, "owner@company.example", options={"kind": "rereview", "scope": "all"}
        )
        self.assertEqual(other["status"], "queued")

    def test_concurrent_rereview_enqueues_on_the_same_pr_yield_exactly_one_winner(self) -> None:
        workers = 8
        barrier = threading.Barrier(workers)
        results: list = []
        lock = threading.Lock()

        def enqueue(head: str) -> None:
            barrier.wait()
            try:
                request = self.store.enqueue_retry(
                    "owner/repo", 7, head, "owner@company.example", options={"kind": "rereview", "scope": "all"}
                )
                with lock:
                    results.append(("ok", request))
            except dashboard_store.PrBusyError as exc:
                with lock:
                    results.append(("busy", exc))

        threads = [threading.Thread(target=enqueue, args=(str(index) * 40,)) for index in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        winners = [outcome for outcome in results if outcome[0] == "ok"]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(results) - len(winners), workers - 1)

    def test_an_identical_head_duplicate_rereview_is_refused_not_silently_returned(self) -> None:
        """Round 2 MUST-FIX: the pre-existing same-head dedup used to early-return
        the first request *before* the busy check ever ran, so a second POST
        for the exact same head got the first request back with success
        instead of PrBusyError. The busy check (any head, including this one)
        must run first.
        """
        head = "a" * 40
        first = self.store.enqueue_retry(
            "owner/repo", 7, head, "owner@company.example", options={"kind": "rereview", "scope": "all"}
        )
        self.assertEqual(first["status"], "queued")
        with self.assertRaises(dashboard_store.PrBusyError):
            self.store.enqueue_retry(
                "owner/repo", 7, head, "owner@company.example", options={"kind": "rereview", "scope": "all"}
            )

    def test_concurrent_identical_head_rereview_enqueues_yield_exactly_one_winner(self) -> None:
        head = "a" * 40
        workers = 8
        barrier = threading.Barrier(workers)
        results: list = []
        lock = threading.Lock()

        def enqueue() -> None:
            barrier.wait()
            try:
                request = self.store.enqueue_retry(
                    "owner/repo", 7, head, "owner@company.example", options={"kind": "rereview", "scope": "all"}
                )
                with lock:
                    results.append(("ok", request))
            except dashboard_store.PrBusyError as exc:
                with lock:
                    results.append(("busy", exc))

        threads = [threading.Thread(target=enqueue) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        winners = [outcome for outcome in results if outcome[0] == "ok"]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(results) - len(winners), workers - 1)


class ListRunsTest(StoreTestCase):
    def test_filters_by_status_and_repo_newest_first(self) -> None:
        one = self.store.start_run("owner/one", 1, "aaa")
        two = self.store.start_run("owner/two", 2, "bbb")
        three = self.store.start_run("owner/one", 3, "ccc")
        self.store.update_run(two["id"], "succeeded")

        self.assertEqual(
            [run["id"] for run in self.store.list_runs()],
            [three["id"], two["id"], one["id"]],
        )
        self.assertEqual(
            [run["id"] for run in self.store.list_runs(repo="owner/one")],
            [three["id"], one["id"]],
        )
        self.assertEqual(
            [run["id"] for run in self.store.list_runs(status="succeeded")],
            [two["id"]],
        )
        self.assertEqual(
            [run["id"] for run in self.store.list_runs(status="running", repo="owner/one")],
            [three["id"], one["id"]],
        )

    def test_limit_offset_and_hard_bound(self) -> None:
        runs = [self.store.start_run("owner/repo", index + 1, "sha%d" % index) for index in range(5)]
        newest_first = list(reversed([run["id"] for run in runs]))

        self.assertEqual([run["id"] for run in self.store.list_runs(limit=2)], newest_first[:2])
        self.assertEqual(
            [run["id"] for run in self.store.list_runs(limit=2, offset=2)],
            newest_first[2:4],
        )
        self.assertEqual(len(self.store.list_runs(limit=10_000)), 5)
        self.assertEqual(self.store.max_list_limit, 200)

    def test_limit_is_clamped_into_range(self) -> None:
        self.store.start_run("owner/repo", 1, "aaa")
        self.assertEqual(len(self.store.list_runs(limit=0)), 1)
        self.assertEqual(len(self.store.list_runs(limit=-5)), 1)
        self.assertEqual(len(self.store.list_runs(offset=-5)), 1)


class RunStatusCountsTest(StoreTestCase):
    """Bucket sizes for a summary reader, counted without paging the history."""

    def test_counts_every_attempt_and_each_status(self) -> None:
        running = self.store.start_run("owner/repo", 1, "aaa")
        self.store.start_run("owner/repo", 2, "bbb")
        done = self.store.start_run("owner/repo", 3, "ccc")
        self.store.update_run(done["id"], "succeeded")
        failed = self.store.start_run("owner/repo", 4, "ddd")
        self.store.update_run(failed["id"], "failed", error="quorum not reached")

        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat(timespec="seconds")
        counts = self.store.run_status_counts(cutoff)
        # Every attempt (4) in exactly one bucket; a status with no attempt (stale) has none.
        self.assertEqual(counts, {
            "running": {"total": 2, "recent": 2},
            "succeeded": {"total": 1, "recent": 1},
            "failed": {"total": 1, "recent": 1},
        })
        self.assertEqual(counts["running"]["total"], len(self.store.list_runs(status="running")))
        self.assertIn(running["id"], [run["id"] for run in self.store.list_runs(status="running")])

    def test_since_window_is_the_completion_time_not_the_start_time(self) -> None:
        # An attempt started long ago but finished just now is recent work; the
        # window must follow updated_at, which update_run rewrites on finish.
        old = "2020-01-01T00:00:00+00:00"
        run = self.store.import_run("legacy-1", "owner/repo", 1, "aaa", status="succeeded", created_at=old)
        self.assertEqual(run["updated_at"], old)
        live = self.store.start_run("owner/repo", 2, "bbb")
        self.store.update_run(live["id"], "succeeded")

        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat(timespec="seconds")
        # Two succeeded, one of them recent, and no other status holds a recent attempt.
        self.assertEqual(self.store.run_status_counts(cutoff), {"succeeded": {"total": 2, "recent": 1}})

    def test_status_counts_are_one_snapshot_with_the_same_window(self) -> None:
        old = "2020-01-01T00:00:00+00:00"
        # Started long ago (imported as still running), finished just now: the
        # completion time is what makes it recent, not the start time.
        stale_start = self.store.start_run("owner/repo", 3, "ccc")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE runs SET created_at = ?, updated_at = ? WHERE id = ?", (old, old, stale_start["id"])
            )
        self.assertEqual(self.store.get_run(stale_start["id"])["updated_at"], old)
        self.store.update_run(stale_start["id"], "succeeded")
        self.assertGreater(self.store.get_run(stale_start["id"])["updated_at"], old)
        self.store.import_run("legacy-4", "owner/repo", 4, "ddd", status="succeeded", created_at=old)
        live = self.store.start_run("owner/repo", 5, "eee")
        self.store.update_run(live["id"], "failed", error="quorum not reached")
        self.store.start_run("owner/repo", 6, "fff")

        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat(timespec="seconds")
        counts = self.store.run_status_counts(cutoff)
        self.assertEqual(counts["succeeded"], {"total": 2, "recent": 1})
        self.assertEqual(counts["failed"], {"total": 1, "recent": 1})
        self.assertEqual(counts["running"], {"total": 1, "recent": 1})
        self.assertNotIn("stale", counts)
        # The whole snapshot: no other bucket, and the four attempts each counted once.
        self.assertEqual(counts, {
            "succeeded": {"total": 2, "recent": 1},
            "failed": {"total": 1, "recent": 1},
            "running": {"total": 1, "recent": 1},
        })

    def test_imported_attempt_with_an_unknown_date_is_never_counted_as_recent(self) -> None:
        imported = self.store.import_run("legacy-2", "owner/repo", 9, "eee", status="succeeded", created_at="")
        self.assertEqual(imported["updated_at"], "")
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat(timespec="seconds")
        self.assertEqual(self.store.run_status_counts(cutoff), {"succeeded": {"total": 1, "recent": 0}})


class EventStreamTest(StoreTestCase):
    def test_lifecycle_emits_ascending_replayable_events(self) -> None:
        request = self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
        self.store.claim_retry()
        run = self.store.start_run("owner/repo", 7, "abc123", trigger="retry")
        self.store.update_run(run["id"], "running", stage="agents")
        self.store.update_run(run["id"], "succeeded", review_url="https://example.invalid/r/1")
        self.store.finish_retry(request["id"], "succeeded", run_id=run["id"])

        events = self.store.events_after(0)
        self.assertEqual(
            [event["kind"] for event in events],
            [
                "retry_enqueued",
                "retry_claimed",
                "run_started",
                "run_updated",
                "run_updated",
                "retry_finished",
            ],
        )
        ids = [event["id"] for event in events]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), len(ids))
        for event in events:
            self.assertEqual(
                set(event),
                {"id", "kind", "run_id", "payload", "created_at"},
            )
            self.assertIsInstance(event["payload"], dict)
            self.assertTrue(event["created_at"])

        run_started = events[2]
        self.assertEqual(run_started["run_id"], run["id"])
        self.assertEqual(run_started["payload"]["repo"], "owner/repo")
        self.assertEqual(run_started["payload"]["pr"], 7)
        self.assertEqual(run_started["payload"]["trigger"], "retry")

        self.assertEqual(
            [event["id"] for event in self.store.events_after(ids[2])],
            ids[3:],
        )
        self.assertEqual(self.store.events_after(ids[-1]), [])

    def test_event_ids_keep_ascending_after_reopen(self) -> None:
        run = self.store.start_run("owner/repo", 7, "abc123")
        first_ids = [event["id"] for event in self.store.events_after(0)]
        reopened = DashboardStore(self.db_path)
        reopened.update_run(run["id"], "succeeded")
        later_ids = [event["id"] for event in reopened.events_after(first_ids[-1])]
        self.assertTrue(later_ids)
        self.assertGreater(later_ids[0], first_ids[-1])

    def test_events_after_is_bounded(self) -> None:
        for index in range(12):
            self.store.start_run("owner/repo", index + 1, "sha%d" % index)
        self.assertEqual(len(self.store.events_after(0, limit=5)), 5)
        self.assertEqual(len(self.store.events_after(0, limit=10_000)), 12)
        self.assertEqual(self.store.max_event_limit, 500)
        self.assertEqual(len(self.store.events_after(-10, limit=1)), 1)

    def test_latest_event_id_is_zero_on_an_empty_stream(self) -> None:
        self.assertEqual(self.store.latest_event_id(), 0)

    def test_latest_event_at_is_none_on_an_empty_stream(self) -> None:
        self.assertIsNone(self.store.latest_event_at())

    def test_latest_event_at_returns_the_newest_events_timestamp(self) -> None:
        run = self.store.start_run("owner/repo", 7, "abc123")
        self.store.update_run(run["id"], "succeeded")
        events = self.store.events_after(0)
        self.assertEqual(self.store.latest_event_at(), events[-1]["created_at"])

    def test_latest_event_id_lets_a_new_client_tail_without_replaying(self) -> None:
        self.store.start_run("owner/repo", 7, "abc123")
        emitted = self.store.events_after(0)
        tail = self.store.latest_event_id()
        self.assertEqual(tail, emitted[-1]["id"])
        # A page opened now starts here: no backlog, and it still sees what follows.
        self.assertEqual(self.store.events_after(tail), [])
        later = self.store.start_run("owner/repo", 8, "bbb222")
        self.assertEqual(
            [event["run_id"] for event in self.store.events_after(tail)], [later["id"]]
        )

    def test_oversized_payloads_are_truncated(self) -> None:
        run = self.store.start_run("owner/repo", 7, "abc123")
        huge_error = "x" * 100_000
        self.store.update_run(run["id"], "failed", error=huge_error, agents=[{"id": "a" * 50_000}])

        stored = self.store.get_run(run["id"])
        assert stored is not None
        self.assertLess(len(stored["error"]), len(huge_error))
        self.assertLessEqual(len(stored["error"]), self.store.max_text_chars)

        event = self.store.events_after(0)[-1]
        self.assertLessEqual(len(repr(event["payload"])), 4 * self.store.max_json_chars)


class RecoveryTest(StoreTestCase):
    def test_recovery_interrupts_running_work_without_inventing_success(self) -> None:
        finished = self.store.start_run("owner/repo", 1, "aaa")
        self.store.update_run(finished["id"], "succeeded")
        in_flight = self.store.start_run("owner/repo", 2, "bbb")

        claimed = self.store.enqueue_retry("owner/repo", 2, "bbb", "owner@company.example")
        self.store.claim_retry()
        pending = self.store.enqueue_retry("owner/repo", 3, "ccc", "owner@company.example")

        self.store.recover_interrupted()

        recovered = self.store.get_run(in_flight["id"])
        assert recovered is not None
        self.assertEqual(recovered["status"], "interrupted")
        untouched = self.store.get_run(finished["id"])
        assert untouched is not None
        self.assertEqual(untouched["status"], "succeeded")

        # The claimed request may already have posted a review: it fails, it is
        # not silently re-queued.
        self.assertEqual(self.store.retry_status(claimed["id"]), "failed")
        # A never-claimed request stays exactly once in the queue.
        self.assertEqual(self.store.retry_status(pending["id"]), "queued")
        still_claimable = self.store.claim_retry()
        assert still_claimable is not None
        self.assertEqual(still_claimable["id"], pending["id"])
        self.assertIsNone(self.store.claim_retry())

    def test_recovery_takes_the_outcome_of_a_terminal_linked_attempt(self) -> None:
        # RoL review on PR #12: crash between update_run(succeeded) and
        # finish_retry() must not turn a posted review into a failed request.
        self.store.enqueue_retry("owner/repo", 1, "aaa", "owner@company.example")
        posted = self.store.claim_retry()
        run = self.store.start_run("owner/repo", 1, "aaa", trigger="retry", request_id=posted["id"])
        self.store.update_run(run["id"], "succeeded", review_url="https://github.invalid/r/1")

        self.store.enqueue_retry("owner/repo", 2, "bbb", "owner@company.example")
        lost = self.store.claim_retry()
        lost_run = self.store.start_run("owner/repo", 2, "bbb", trigger="retry", request_id=lost["id"])
        self.store.update_run(lost_run["id"], "failed", error="only 1/6 agents succeeded")

        self.store.enqueue_retry("owner/repo", 3, "ccc", "owner@company.example")
        unlinked = self.store.claim_retry()
        # A stale error on a running request must not survive a decided outcome.
        with self.store._write() as conn:
            conn.execute("UPDATE retry_requests SET error = 'stale note' WHERE id = ?", (posted["id"],))

        self.store.recover_interrupted()

        self.assertEqual(self.store.get_retry(posted["id"])["status"], "succeeded")
        self.assertIsNone(self.store.get_retry(posted["id"])["error"])
        self.assertEqual(self.store.get_retry(lost["id"])["status"], "failed")
        self.assertEqual(self.store.get_retry(lost["id"])["error"], "only 1/6 agents succeeded")
        # No linked attempt: whether the review posted is unknown, so it fails.
        self.assertEqual(self.store.get_retry(unlinked["id"])["status"], "failed")
        kinds = [(e["kind"], e["payload"]["status"]) for e in self.store.events_after(0) if e["kind"] == "retry_finished"]
        self.assertIn(("retry_finished", "succeeded"), kinds)

    def test_recovery_emits_events_and_is_idempotent(self) -> None:
        run = self.store.start_run("owner/repo", 1, "aaa")
        cursor = self.store.events_after(0)[-1]["id"]
        self.store.recover_interrupted()
        kinds = [event["kind"] for event in self.store.events_after(cursor)]
        self.assertEqual(kinds, ["run_updated"])

        cursor = self.store.events_after(0)[-1]["id"]
        self.store.recover_interrupted()
        self.assertEqual(self.store.events_after(cursor), [])
        stored = self.store.get_run(run["id"])
        assert stored is not None
        self.assertEqual(stored["status"], "interrupted")


class DurabilityTest(StoreTestCase):
    def test_data_survives_reopen_and_schema_init_is_idempotent(self) -> None:
        request = self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
        run = self.store.start_run("owner/repo", 7, "abc123")
        self.store.update_run(run["id"], "failed", error="boom")

        reopened = DashboardStore(self.db_path)
        stored = reopened.get_run(run["id"])
        assert stored is not None
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["error"], "boom")
        self.assertEqual(reopened.retry_status(request["id"]), "queued")
        self.assertEqual(len(reopened.events_after(0)), 3)

    def test_storage_is_private_to_the_owner(self) -> None:
        directory_mode = stat.S_IMODE(self.db_path.parent.stat().st_mode)
        self.assertEqual(directory_mode, 0o700, oct(directory_mode))
        file_mode = stat.S_IMODE(self.db_path.stat().st_mode)
        self.assertEqual(file_mode, 0o600, oct(file_mode))

        self.store.start_run("owner/repo", 7, "abc123")
        for sidecar in sorted(self.db_path.parent.glob(self.db_path.name + "-*")):
            mode = stat.S_IMODE(sidecar.stat().st_mode)
            self.assertEqual(mode, 0o600, "%s %s" % (sidecar.name, oct(mode)))




class SettingsAndRetryOptionsTest(unittest.TestCase):
    """Operator settings + retry options (.prd/model-policy/trace.md B/C)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = DashboardStore(Path(self._tmp.name) / "dashboard.sqlite3")

    def test_setting_round_trip_emits_events_and_reset_returns_to_none(self) -> None:
        self.assertIsNone(self.store.get_setting("model_policy"))
        policy = {"model": "gpt-6-astra[1m]", "effort": "xhigh", "fallback_models": ["grok-4.6"]}
        saved = self.store.set_setting("model_policy", policy, "owner@company.example")
        self.assertEqual(saved["key"], "model_policy")
        self.assertEqual(saved["value"], policy)
        self.assertEqual(saved["updated_by"], "owner@company.example")
        self.assertTrue(saved["updated_at"])
        self.assertEqual(self.store.get_setting("model_policy")["value"], policy)

        # The daemon reads the row through the recorder: the seam is advisory
        # (fail-open), so only this end-to-end check proves the Settings page
        # and the reviews agree on which policy runs.
        recorder = dx.DashboardRecorder(self.store, logger=lambda message: None)
        self.assertEqual(recorder.setting("model_policy"), policy)
        config = {"model_policy": {"model": "gpt-5.6-sol[1m]", "effort": "max", "fallback_models": []}}
        resolved = svc.resolve_model_policy(config, stored=recorder.setting("model_policy"))
        self.assertEqual((resolved["source"], resolved["model"], resolved["fallback_models"]), ("dashboard", policy["model"], policy["fallback_models"]))

        replaced = self.store.set_setting("model_policy", {"model": "grok-4.6", "effort": "", "fallback_models": []}, "owner@company.example")
        self.assertEqual(replaced["value"]["model"], "grok-4.6")
        self.assertTrue(self.store.delete_setting("model_policy", "owner@company.example"))
        self.assertFalse(self.store.delete_setting("model_policy", "owner@company.example"))
        self.assertIsNone(self.store.get_setting("model_policy"))

        kinds = [event["kind"] for event in self.store.events_after(0, 50)]
        self.assertEqual(kinds, ["setting_updated", "setting_updated", "setting_deleted"])
        first = self.store.events_after(0, 1)[0]
        self.assertEqual(first["payload"]["key"], "model_policy")
        self.assertEqual(first["payload"]["value"], policy)

    def test_agent_setting_round_trips_and_its_event_never_carries_the_prompt(self) -> None:
        prompt = "Review the server architecture.\n" * 1400  # ~44,800 chars, past the 8,000 policy cap
        saved = self.store.set_setting("agent:server-architect", {"prompt": prompt, "required": False}, "owner@company.example")
        self.assertEqual(saved["value"], {"prompt": prompt, "required": False})
        # The daemon reads it through the recorder exactly like the model policy.
        recorder = dx.DashboardRecorder(self.store, logger=lambda message: None)
        self.assertEqual(recorder.setting("agent:server-architect"), {"prompt": prompt, "required": False})
        self.assertTrue(self.store.delete_setting("agent:server-architect", "owner@company.example"))

        updated, deleted = self.store.events_after(0, 50)
        self.assertEqual(updated["kind"], "setting_updated")
        self.assertEqual(updated["payload"], {"key": "agent:server-architect", "updated_by": "owner@company.example",
                                              "required": False, "prompt_chars": len(prompt)})
        self.assertNotIn("Review the server architecture", repr(updated["payload"]))
        self.assertEqual(deleted["payload"], {"key": "agent:server-architect", "updated_by": "owner@company.example"})

    def test_bad_agent_keys_are_rejected(self) -> None:
        for key in ("agent:", "agent:Server", "agent:-x", "agent:x\n", "agent:a/b", "agent:" + "a" * 65, "agents:x"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.store.get_setting(key)

    def test_an_oversized_setting_is_refused_never_truncated(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.store.set_setting("model_policy", {"model": "x" * 9000}, "owner@company.example")
        self.assertIn("too large", str(caught.exception))
        with self.assertRaises(ValueError):
            # Past the agent cap: two worst-case prompts (the override and a kept edit) plus the fields.
            self.store.set_setting("agent:arch", {"prompt": "x" * 600_000}, "owner@company.example")
        self.assertIsNone(self.store.get_setting("model_policy"))
        self.assertIsNone(self.store.get_setting("agent:arch"))
        self.assertEqual(self.store.events_after(0, 50), [])

    def test_every_prompt_within_the_documented_limit_fits_however_it_escapes(self) -> None:
        # One limit (PR #23 review): 48,000 code points, whatever JSON escaping
        # does to them (a control character encodes as 6, a newline or quote as 2).
        for char in ("x", "\n", '"', "\x01", "\u3131", "\U0001F600"):
            with self.subTest(char=repr(char)):
                prompt = char * svc.MAX_AGENT_PROMPT_CHARS
                self.store.set_setting("agent:arch", {"prompt": prompt, "required": True}, "owner@company.example")
                self.assertEqual(self.store.get_setting("agent:arch")["value"]["prompt"], prompt)

    def test_merge_agent_setting_applies_a_partial_patch_and_deletes_when_empty(self) -> None:
        by = "owner@company.example"
        row = self.store.merge_agent_setting("arch", {"prompt": " p "}, by, svc.validate_agent_override)
        self.assertEqual(row["value"], {"prompt": "p"})
        row = self.store.merge_agent_setting("arch", {"required": True}, by, svc.validate_agent_override)
        self.assertEqual(row["value"], {"prompt": "p", "required": True})
        row = self.store.merge_agent_setting("arch", {"prompt": None}, by, svc.validate_agent_override)
        self.assertEqual(row["value"], {"required": True})
        with self.assertRaises(ValueError):
            self.store.merge_agent_setting("arch", {"required": "yes"}, by, svc.validate_agent_override)
        self.assertEqual(self.store.get_setting("agent:arch")["value"], {"required": True}, "a rejected patch writes nothing")
        self.assertIsNone(self.store.merge_agent_setting("arch", {"required": None}, by, svc.validate_agent_override))
        self.assertIsNone(self.store.get_setting("agent:arch"))
        kinds = [event["kind"] for event in self.store.events_after(0, 50)]
        self.assertEqual(kinds, ["setting_updated"] * 3 + ["setting_deleted"])
        # A stored value the rules reject counts as empty rather than blocking the merge.
        self.store.set_setting("agent:arch", {"required": "garbage"}, by)
        self.assertEqual(self.store.merge_agent_setting("arch", {"prompt": "q"}, by, svc.validate_agent_override)["value"], {"prompt": "q"})

    def test_concurrent_merges_on_different_fields_both_land(self) -> None:
        by = "owner@company.example"
        entered = threading.Event()

        def slow_normalize(candidate):
            normalized = svc.validate_agent_override(candidate)
            if normalized.get("prompt") == "from tab A" and "required" not in normalized:
                entered.set()
                time.sleep(0.3)  # tab B tries to write inside this window
            return normalized

        errors = []

        def tab_a():
            try:
                self.store.merge_agent_setting("arch", {"prompt": "from tab A"}, by, slow_normalize)
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        thread = threading.Thread(target=tab_a)
        thread.start()
        self.assertTrue(entered.wait(5))
        self.store.merge_agent_setting("arch", {"required": False}, by, svc.validate_agent_override)
        thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(self.store.get_setting("agent:arch")["value"], {"prompt": "from tab A", "required": False})

    def test_repositories_setting_round_trips_and_its_event_carries_names_only(self) -> None:
        added = [
            {"repo": "acme-corp/MiniMoon", "added_by": "owner@company.example", "added_at": "2026-10-13T00:00:00+00:00"},
            {"repo": "acme-corp/MiniMoonBo", "added_by": "owner@company.example", "added_at": "2026-10-13T00:01:00+00:00"},
        ]
        saved = self.store.set_setting("repositories", {"added": added}, "owner@company.example")
        self.assertEqual(saved["value"], {"added": added})
        # The daemon reads it through the same best-effort reader the cycle uses.
        config = {"repositories": ["acme-corp/Gamma"], "agents": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
        self.assertEqual(
            svc.review_repositories(config, self.store),
            ["acme-corp/Gamma", "acme-corp/MiniMoon", "acme-corp/MiniMoonBo"],
        )
        self.assertTrue(self.store.delete_setting("repositories", "owner@company.example"))
        updated, deleted = self.store.events_after(0, 50)
        self.assertEqual(updated["payload"], {
            "key": "repositories",
            "updated_by": "owner@company.example",
            "repos": ["acme-corp/MiniMoon", "acme-corp/MiniMoonBo"],
        })
        self.assertEqual(deleted["payload"], {"key": "repositories", "updated_by": "owner@company.example"})
        self.assertEqual(svc.review_repositories(config, self.store), ["acme-corp/Gamma"])

    def test_repositories_setting_is_capped_by_count_and_size_never_truncated(self) -> None:
        def entry(index: int) -> dict:
            return {"repo": "acme-corp/repo-%d" % index, "added_by": "owner@company.example", "added_at": "2026-10-13T00:00:00+00:00"}

        with self.assertRaises(ValueError) as caught:
            self.store.set_setting("repositories", {"added": [entry(i) for i in range(51)]}, "owner@company.example")
        self.assertIn("51 repositories, limit 50", str(caught.exception))
        with self.assertRaises(ValueError):
            self.store.set_setting("repositories", {"added": "acme-corp/MiniMoon"}, "owner@company.example")
        with self.assertRaises(ValueError) as caught:
            self.store.set_setting("repositories", {"added": [entry(0)], "padding": "x" * 30_000}, "owner@company.example")
        self.assertIn("too large", str(caught.exception))
        self.assertIsNone(self.store.get_setting("repositories"))
        self.assertEqual(self.store.events_after(0, 50), [])
        # 50 worst-case entries (longest name, longest email) still fit: the count binds first.
        longest = [
            {"repo": "a" * 39 + "/" + "b" * 98 + "%02d" % i, "added_by": "e" * 254, "added_at": "2026-10-13T00:00:00+00:00"}
            for i in range(50)
        ]
        self.store.set_setting("repositories", {"added": longest}, "owner@company.example")
        self.assertEqual(len(self.store.get_setting("repositories")["value"]["added"]), 50)

    def test_admins_setting_round_trips_and_its_event_carries_addresses_only(self) -> None:
        added = [
            {"email": "ops@example.com", "added_by": "owner@company.example", "added_at": "2026-10-14T00:00:00+00:00"},
            {"email": "dev@company.example", "added_by": "owner@company.example", "added_at": "2026-10-14T00:01:00+00:00"},
        ]
        self.assertIsNone(self.store.get_setting("admins"))
        saved = self.store.set_setting("admins", {"added": added}, "owner@company.example")
        self.assertEqual(saved["value"], {"added": added})
        self.assertEqual(self.store.get_setting("admins")["value"], {"added": added})
        self.assertTrue(self.store.delete_setting("admins", "owner@company.example"))
        self.assertIsNone(self.store.get_setting("admins"))
        updated, deleted = self.store.events_after(0, 50)
        self.assertEqual(updated["payload"], {
            "key": "admins",
            "updated_by": "owner@company.example",
            "emails": ["ops@example.com", "dev@company.example"],
        })
        self.assertEqual(deleted["payload"], {"key": "admins", "updated_by": "owner@company.example"})

    def test_admins_setting_is_capped_by_count_and_size_never_truncated(self) -> None:
        def entry(index: int) -> dict:
            return {"email": "user%d@example.com" % index, "added_by": "owner@company.example", "added_at": "2026-10-14T00:00:00+00:00"}

        with self.assertRaises(ValueError) as caught:
            self.store.set_setting("admins", {"added": [entry(i) for i in range(51)]}, "owner@company.example")
        self.assertIn("51 admins, limit 50", str(caught.exception))
        for value in ({"added": "ops@example.com"}, {}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.store.set_setting("admins", value, "owner@company.example")
        with self.assertRaises(ValueError) as caught:
            self.store.set_setting("admins", {"added": [entry(0)], "padding": "x" * 40_000}, "owner@company.example")
        self.assertIn("too large", str(caught.exception))
        self.assertIsNone(self.store.get_setting("admins"))
        self.assertEqual(self.store.events_after(0, 50), [])
        # 50 worst-case entries (254-character address and granter) still fit: the count binds first.
        longest = [
            {"email": "%02d" % i + "a" * 240 + "@example.com", "added_by": "e" * 254, "added_at": "2026-10-14T00:00:00+00:00"}
            for i in range(50)
        ]
        self.assertEqual({len(item["email"]) for item in longest}, {254})
        self.store.set_setting("admins", {"added": longest}, "owner@company.example")
        self.assertEqual(len(self.store.get_setting("admins")["value"]["added"]), 50)
        # Its event outgrows max_json_chars: the addresses are dropped, never the key
        # every open tab needs to re-read its own role.
        event = self.store.events_after(0, 50)[-1]
        self.assertEqual(event["kind"], "setting_updated")
        self.assertEqual(event["payload"]["key"], "admins")
        self.assertIs(event["payload"]["truncated"], True)
        self.assertNotIn("emails", event["payload"])

    def test_unknown_setting_key_and_non_object_value_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.get_setting("api_key")
        with self.assertRaises(ValueError):
            self.store.set_setting("model_policy", ["not", "an", "object"], "owner@company.example")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.store.set_setting("model_policy", {"model": "x"}, "")

    def test_retry_options_survive_enqueue_claim_and_listing(self) -> None:
        options = {"model_policy": {"model": "grok-4.6", "effort": "xhigh", "fallback_models": []}}
        queued = self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example", options=options)
        self.assertEqual(queued["options"], options)
        # A duplicate for the same target keeps the first request's options.
        again = self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example", options={"model_policy": {"model": "other"}})
        self.assertEqual(again["id"], queued["id"])
        self.assertEqual(again["options"], options)
        claimed = self.store.claim_retry()
        self.assertEqual(claimed["options"], options)
        self.assertEqual(self.store.get_retry(queued["id"])["options"], options)
        self.assertEqual(self.store.list_retries()[0]["options"], options)

    def test_retry_without_options_reads_back_as_none(self) -> None:
        queued = self.store.enqueue_retry("owner/repo", 7, "abc123", "owner@company.example")
        self.assertIsNone(queued["options"])
        with self.assertRaises(ValueError):
            self.store.enqueue_retry("owner/repo", 8, "abc123", "owner@company.example", options=["nope"])  # type: ignore[arg-type]

    def test_run_llm_field_is_stored_as_json_and_projected(self) -> None:
        run = self.store.start_run("owner/repo", 7, "a" * 40, title="t", url="u")
        llm = {"policy": {"model": "gpt-6-astra[1m]", "effort": "xhigh", "fallback_models": ["grok-4.6"], "source": "config"},
               "totals": {"input_tokens": 10, "output_tokens": 2}}
        self.store.update_run(run["id"], "running", stage="agents", llm=llm)
        self.assertEqual(self.store.get_run(run["id"])["llm"], llm)
        self.assertEqual(self.store.events_after(0, 10)[-1]["payload"]["llm"], llm)

    def test_old_database_gains_the_new_columns_on_open(self) -> None:
        path = Path(self._tmp.name) / "old.sqlite3"
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE retry_requests (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
              repo TEXT NOT NULL, pr INTEGER NOT NULL, head_sha TEXT NOT NULL, requested_by TEXT NOT NULL,
              retry_of TEXT, status TEXT NOT NULL, run_id TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE runs (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, repo TEXT NOT NULL,
              pr INTEGER NOT NULL, head_sha TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
              trigger_source TEXT NOT NULL DEFAULT 'poll', retry_of TEXT, status TEXT NOT NULL, stage TEXT, agents TEXT,
              review_url TEXT, error TEXT, successful_agents INTEGER, total_agents INTEGER, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL);
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, run_id TEXT,
              payload TEXT NOT NULL, created_at TEXT NOT NULL);
            """
        )
        conn.commit()
        conn.close()
        store = DashboardStore(path)
        queued = store.enqueue_retry("owner/repo", 1, "abc", "owner@company.example", options={"model_policy": {"model": "grok-4.6"}})
        self.assertEqual(queued["options"]["model_policy"]["model"], "grok-4.6")
        run = store.start_run("owner/repo", 1, "abc")
        store.update_run(run["id"], "running", llm={"policy": {"model": "grok-4.6"}})
        self.assertEqual(store.get_run(run["id"])["llm"]["policy"]["model"], "grok-4.6")
        self.assertIsNone(store.get_setting("model_policy"))

    def test_migration_survives_losing_the_alter_race_to_another_opener(self) -> None:
        """The daemon and the web tier open the file together at deploy time.

        The loser sees the column missing, its ALTER fails with ``duplicate
        column``, and it must carry on with a store rather than run for the
        rest of its life without one.
        """
        path = Path(self._tmp.name) / "race.sqlite3"
        DashboardStore(path)  # the winner: fully migrated
        real = sqlite3.connect(str(path))
        real.row_factory = sqlite3.Row

        class StaleConn:
            """Answers the first table_info from before the winner's ALTER."""

            def __init__(self) -> None:
                self.stale_reads = 1

            def execute(self, sql, *args):
                if sql.startswith("PRAGMA table_info") and self.stale_reads:
                    self.stale_reads -= 1
                    return real.execute("SELECT name FROM sqlite_master WHERE 0")
                return real.execute(sql, *args)

        DashboardStore._ensure_column(StaleConn(), "runs", "llm")  # type: ignore[arg-type]
        columns = {row["name"] for row in real.execute("PRAGMA table_info(runs)").fetchall()}
        self.assertIn("llm", columns)
        with self.assertRaises(sqlite3.OperationalError):
            # A failure that is not the race still surfaces.
            DashboardStore._ensure_column(real, "no_such_table", "llm")
        real.close()


class AgentPromptSupersededTest(StoreTestCase):
    """A changed prompt file replaces a dashboard prompt once; the edit is kept for compare, rollback or discard."""

    BY = "owner@company.example"
    KEPT = {"prompt": "Kept edit",
            "saved_at": "2026-10-14T00:00:00+00:00", "saved_by": BY, "superseded_at": "2026-10-15T00:00:00+00:00"}

    def test_reconcile_replaces_the_prompt_in_one_transaction_and_its_event_carries_no_text(self) -> None:
        self.store.merge_agent_setting(
            "arch", {"prompt": "Edited B", "prompt_base_sha": svc.prompt_sha("Default A"), "required": True}, self.BY, svc.validate_agent_document
        )
        before = self.store.get_agent_setting("arch")
        self.assertEqual(self.store.reconcile_agent_setting("arch", lambda: "Default A", svc.reconcile_agent_document), (before, None))
        row, action = self.store.reconcile_agent_setting("arch", lambda: "Default C", svc.reconcile_agent_document)
        self.assertEqual(action, "superseded")
        self.assertEqual(row["value"]["required"], True)
        self.assertNotIn("prompt", row["value"])
        self.assertEqual(row["value"]["superseded"]["prompt"], "Edited B")
        self.assertEqual((row["value"]["superseded"]["saved_by"], row["value"]["superseded"]["saved_at"]), (self.BY, before["updated_at"]))
        self.assertEqual((row["updated_at"], row["updated_by"]), (before["updated_at"], before["updated_by"]),
                         "a system write keeps who saved the overrides that remain, and when")
        events = self.store.events_after(0, 50)
        self.assertEqual([(event["kind"], event["payload"]["key"]) for event in events], [
            ("setting_updated", "agent:arch"), ("setting_updated", "agent_meta:arch"),
            ("setting_updated", "agent:arch"), ("setting_updated", "agent_meta:arch"),
            ("agent_prompt_superseded", "agent:arch"),
        ])
        self.assertEqual(events[-1]["payload"], {"key": "agent:arch", "agent_id": "arch"})
        self.assertNotIn("Edited B", json.dumps(events))
        # Idempotent: the other process finds nothing left to replace.
        self.assertEqual(self.store.reconcile_agent_setting("arch", lambda: "Default C", svc.reconcile_agent_document), (row, None))
        self.assertEqual(len(self.store.events_after(0, 50)), 5)
        self.assertEqual(self.store.reconcile_agent_setting("b", lambda: "Default C", svc.reconcile_agent_document), (None, None), "no document")

    def test_reconcile_holds_the_write_lock_across_read_decide_write(self) -> None:
        """Two real connections to the same file, as the daemon and the dashboard are.

        The first reconcile's ``reconcile`` callable is paused with a
        threading.Event after it is called with the document SQLite just read,
        i.e. inside the write transaction, between the read and the write.

        The lock is proved with an observable probe, not inferred from timing
        (round-2 review: "the second worker has not finished within 0.3s" also
        passes for a late-scheduled worker on broken code). While paused, a raw
        ``sqlite3.connect(..., timeout=0)`` tries ``BEGIN IMMEDIATE`` on the same
        file and must fail at once with "database is locked" — timeout=0 means
        SQLite does not even poll, so this cannot pass by scheduling luck. That
        proves the write lock is held across read -> decide; the test then
        releases, joins the worker and re-raises anything it caught, before
        checking a second writer (its own connection, found idempotent) and the
        final state.
        """
        other = DashboardStore(self.db_path)
        self.store.merge_agent_setting(
            "arch", {"prompt": "Edited B", "prompt_base_sha": svc.prompt_sha("Default A")}, self.BY, svc.validate_agent_document
        )
        entered, release = threading.Event(), threading.Event()
        worker_errors: list[BaseException] = []

        def paused_reconcile(value, default_text, now, saved_at, saved_by):
            entered.set()
            if not release.wait(5):
                raise AssertionError("the test never released the first reconcile")
            return svc.reconcile_agent_document(value, default_text, now, saved_at=saved_at, saved_by=saved_by)

        first_result: list = []

        def run_first() -> None:
            try:
                first_result.append(self.store.reconcile_agent_setting("arch", lambda: "Default C", paused_reconcile))
            except BaseException as exc:  # captured here, re-raised on the main thread below
                worker_errors.append(exc)

        first_thread = threading.Thread(target=run_first)
        first_thread.start()
        try:
            self.assertTrue(entered.wait(5), "the first reconcile's decide step never ran")
            # The deterministic probe: BEGIN IMMEDIATE on a fresh connection,
            # with no busy wait at all, must collide with the first
            # connection's still-open write transaction right now.
            probe = sqlite3.connect(str(self.db_path), timeout=0, isolation_level=None)
            try:
                with self.assertRaises(sqlite3.OperationalError) as raised:
                    probe.execute("BEGIN IMMEDIATE")
                self.assertIn("locked", str(raised.exception).lower())
            finally:
                probe.close()
        finally:
            release.set()
            first_thread.join(10)
        self.assertFalse(first_thread.is_alive(), "the first reconcile never finished")
        if worker_errors:
            raise worker_errors[0]
        self.assertEqual(first_result[0][1], "superseded")

        # The probe above already proved exclusivity; a second writer now
        # (its own connection) must still find nothing left to do.
        second_result = other.reconcile_agent_setting("arch", lambda: "Default C", svc.reconcile_agent_document)
        self.assertEqual(second_result, (first_result[0][0], None), "idempotent: nothing left once the first committed")

        events = self.store.events_after(0, 50)
        superseded_events = [event for event in events if event["kind"] == "agent_prompt_superseded"]
        self.assertEqual(len(superseded_events), 1, "no stale overwrite: exactly one replacement happened")
        self.assertEqual(self.store.get_agent_setting("arch")["value"]["superseded"]["prompt"], "Edited B")

    def test_reconcile_rolls_back_the_whole_write_on_a_mid_transaction_failure(self) -> None:
        self.store.merge_agent_setting(
            "arch", {"prompt": "Edited B", "prompt_base_sha": svc.prompt_sha("Default A")}, self.BY, svc.validate_agent_document
        )
        before_doc = self.store.get_agent_setting("arch")
        before_events = self.store.events_after(0, 50)
        real_emit = self.store._emit

        def boom_emit(conn, kind, run_id, payload):
            if kind == "agent_prompt_superseded":
                raise RuntimeError("boom: the event write fails mid-transaction")
            return real_emit(conn, kind, run_id, payload)

        self.store._emit = boom_emit  # type: ignore[method-assign]
        try:
            with self.assertRaises(RuntimeError):
                self.store.reconcile_agent_setting("arch", lambda: "Default C", svc.reconcile_agent_document)
        finally:
            self.store._emit = real_emit  # type: ignore[method-assign]
        # The settings UPDATE that preceded the failed event write is rolled
        # back too, in the same transaction: the document and the events are
        # exactly as before the attempt, not left half-applied.
        self.assertEqual(self.store.get_agent_setting("arch"), before_doc)
        self.assertEqual(self.store.events_after(0, 50), before_events)

    def test_restore_rolls_back_on_a_failure_after_the_real_update(self) -> None:
        """The failure fires in ``_emit``, AFTER the real UPDATE already ran in
        the same transaction (round-2 review: injecting it in ``normalize``
        fires BEFORE ``_put_setting`` runs, so the old version of this test
        proved nothing had ever rolled back a write — because none had
        happened yet). This is what actually proves the ROLLBACK undoes a
        write that already executed.
        """
        self.store.set_setting("agent:arch", {"prompt": "Edited B", "prompt_base_sha": svc.prompt_sha("A"), "model": "gpt-6-astra[1m]"}, self.BY)
        self.store.reconcile_agent_setting("arch", lambda: "C", svc.reconcile_agent_document)
        before_doc = self.store.get_agent_setting("arch")
        before_events = self.store.events_after(0, 50)
        real_emit = self.store._emit

        def boom_emit(conn, kind, run_id, payload):
            raise RuntimeError("boom: the event write fails after the real UPDATE")

        self.store._emit = boom_emit  # type: ignore[method-assign]
        try:
            with self.assertRaises(RuntimeError):
                self.store.restore_superseded_prompt("arch", svc.prompt_sha("Edited B"), lambda: svc.prompt_sha("C"), self.BY, svc.validate_agent_document)
        finally:
            self.store._emit = real_emit  # type: ignore[method-assign]
        self.assertEqual(self.store.get_agent_setting("arch"), before_doc)
        self.assertEqual(self.store.events_after(0, 50), before_events)

    def test_discard_rolls_back_on_a_failure_after_the_real_update(self) -> None:
        """Discard where other fields remain: the write is an UPDATE, not a DELETE."""
        self.store.set_setting("agent:arch", {"prompt": "Edited B", "prompt_base_sha": svc.prompt_sha("A"), "required": True}, self.BY)
        self.store.reconcile_agent_setting("arch", lambda: "C", svc.reconcile_agent_document)
        before_doc = self.store.get_agent_setting("arch")
        before_events = self.store.events_after(0, 50)
        real_emit = self.store._emit

        def boom_emit(conn, kind, run_id, payload):
            raise RuntimeError("boom: the event write fails after the real UPDATE")

        self.store._emit = boom_emit  # type: ignore[method-assign]
        try:
            with self.assertRaises(RuntimeError):
                self.store.discard_superseded_prompt("arch", svc.prompt_sha("Edited B"), self.BY, svc.validate_agent_document)
        finally:
            self.store._emit = real_emit  # type: ignore[method-assign]
        self.assertEqual(self.store.get_agent_setting("arch"), before_doc)
        self.assertEqual(self.store.events_after(0, 50), before_events)

    def test_discard_rolls_back_on_a_failure_after_the_real_delete(self) -> None:
        """Discard where the document becomes empty: the write is a DELETE, not an UPDATE."""
        self.store.set_setting("agent:arch", {"superseded": self.KEPT}, self.BY)
        before_doc = self.store.get_agent_setting("arch")
        before_events = self.store.events_after(0, 50)
        real_emit = self.store._emit

        def boom_emit(conn, kind, run_id, payload):
            raise RuntimeError("boom: the event write fails after the real DELETE")

        self.store._emit = boom_emit  # type: ignore[method-assign]
        try:
            with self.assertRaises(RuntimeError):
                self.store.discard_superseded_prompt("arch", svc.prompt_sha(self.KEPT["prompt"]), self.BY, svc.validate_agent_document)
        finally:
            self.store._emit = real_emit  # type: ignore[method-assign]
        self.assertEqual(self.store.get_agent_setting("arch"), before_doc)
        self.assertEqual(self.store.events_after(0, 50), before_events)

    def test_reconcile_deletes_a_document_left_empty_and_leaves_a_malformed_one_alone(self) -> None:
        self.store.set_setting("agent:arch", {"prompt": "Default C", "prompt_base_sha": svc.prompt_sha("Default A")}, self.BY)
        self.assertEqual(self.store.reconcile_agent_setting("arch", lambda: "Default C\n", svc.reconcile_agent_document), (None, "cleared"))
        self.assertIsNone(self.store.get_agent_setting("arch"))
        self.assertEqual([event["kind"] for event in self.store.events_after(0, 50)], ["setting_updated", "setting_deleted"])
        self.store.set_setting("agent:arch", {"prompt": "x", "required": "garbage"}, self.BY)
        row = self.store.get_agent_setting("arch")
        self.assertEqual(self.store.reconcile_agent_setting("arch", lambda: "Default C", svc.reconcile_agent_document), (row, None))

    def test_restore_and_discard(self) -> None:
        self.store.set_setting("agent:arch", {"prompt": "Edited B", "prompt_base_sha": svc.prompt_sha("A"), "model": "gpt-6-astra[1m]"}, self.BY)
        self.store.reconcile_agent_setting("arch", lambda: "C", svc.reconcile_agent_document)
        # An agent without a kept edit has nothing to restore or discard.
        b_sha = svc.prompt_sha("Edited B")
        self.assertIsNone(self.store.restore_superseded_prompt("b", b_sha, lambda: svc.prompt_sha("C"), self.BY, svc.validate_agent_document))
        self.assertEqual(self.store.discard_superseded_prompt("b", b_sha, self.BY, svc.validate_agent_document), (False, None))
        # A kept edit other than the one named is never touched.
        for act in (lambda: self.store.restore_superseded_prompt("arch", "0" * 64, lambda: None, self.BY, svc.validate_agent_document),
                    lambda: self.store.discard_superseded_prompt("arch", "0" * 64, self.BY, svc.validate_agent_document)):
            with self.assertRaises(SupersededConflict):
                act()
        self.assertEqual(self.store.get_agent_setting("arch")["value"]["superseded"]["prompt"], "Edited B")
        with mock.patch("code_review_service.dashboard_store._now", return_value="2026-10-16T00:00:00+00:00"):
            row = self.store.restore_superseded_prompt("arch", b_sha, lambda: svc.prompt_sha("C"), "admin@company.example", svc.validate_agent_document)
        self.assertEqual(row["value"], {"prompt": "Edited B", "prompt_base_sha": svc.prompt_sha("C"), "model": "gpt-6-astra[1m]",
                                        "prompt_saved_at": "2026-10-16T00:00:00+00:00", "prompt_saved_by": "admin@company.example"})
        self.assertEqual(row["updated_by"], "admin@company.example")
        self.assertIsNone(self.store.restore_superseded_prompt("arch", b_sha, lambda: svc.prompt_sha("C"), self.BY, svc.validate_agent_document))
        # A restore without a readable file records no base: the edit is adopted on the next reconcile.
        self.store.reconcile_agent_setting("arch", lambda: "D", svc.reconcile_agent_document)
        row = self.store.restore_superseded_prompt("arch", b_sha, lambda: None, self.BY, svc.validate_agent_document)
        self.assertEqual({key: row["value"][key] for key in ("prompt", "model")}, {"prompt": "Edited B", "model": "gpt-6-astra[1m]"})
        self.assertNotIn("prompt_base_sha", row["value"])
        self.assertEqual(self.store.reconcile_agent_setting("arch", lambda: "D", svc.reconcile_agent_document)[1], "adopted")
        # The restored edit is replaced once more when the file changes again; then it is discarded.
        row, action = self.store.reconcile_agent_setting("arch", lambda: "E", svc.reconcile_agent_document)
        self.assertEqual(action, "superseded")
        found, row = self.store.discard_superseded_prompt("arch", b_sha, self.BY, svc.validate_agent_document)
        self.assertEqual((found, row["value"], row["updated_by"]), (True, {"model": "gpt-6-astra[1m]"}, self.BY))
        self.assertEqual(self.store.discard_superseded_prompt("arch", b_sha, self.BY, svc.validate_agent_document), (False, row))
        # Discarding the only field deletes the document.
        self.store.set_setting("agent:b", {"superseded": self.KEPT}, self.BY)
        self.assertEqual(self.store.discard_superseded_prompt("b", svc.prompt_sha(self.KEPT["prompt"]), self.BY, svc.validate_agent_document), (True, None))
        self.assertIsNone(self.store.get_agent_setting("b"))
        self.assertEqual(self.store.events_after(0, 50)[-1]["kind"], "setting_deleted")

    def test_a_prompt_save_clears_the_kept_edit_and_a_field_only_save_keeps_it(self) -> None:
        self.store.set_setting("agent:arch", {"prompt": "Edited B", "prompt_base_sha": svc.prompt_sha("A")}, self.BY)
        self.store.reconcile_agent_setting("arch", lambda: "C", svc.reconcile_agent_document)
        row = self.store.merge_agent_setting("arch", {"required": True}, self.BY, svc.validate_agent_document)
        self.assertEqual((row["value"]["required"], row["value"]["superseded"]["prompt"]), (True, "Edited B"))
        row = self.store.merge_agent_setting(
            "arch", {"prompt": "Edited E", "prompt_base_sha": svc.prompt_sha("C"), "superseded": None}, self.BY, svc.validate_agent_document
        )
        self.assertEqual(row["value"], {"prompt": "Edited E", "prompt_base_sha": svc.prompt_sha("C"), "required": True})
        self.assertNotIn("Edited", json.dumps(self.store.events_after(0, 50)))

    def test_a_kept_edit_at_the_prompt_limit_fits_beside_a_new_one(self) -> None:
        # The limit an operator meets stays the documented prompt limit, even with
        # the worst JSON escape and a kept edit of the same size in the document.
        kept = dict(self.KEPT, prompt="\x01" * svc.MAX_AGENT_PROMPT_CHARS, saved_by="x" * 254)
        value = {"prompt": "\x01" * svc.MAX_AGENT_PROMPT_CHARS, "prompt_base_sha": "c" * 64, "required": True, "model": "x" * 80,
                 "prompt_saved_at": "t" * 64, "prompt_saved_by": "y" * 254, "superseded": kept}
        self.assertEqual(self.store.merge_agent_setting("arch", value, self.BY, lambda doc: dict(doc))["value"], value)


class AgentMetaSidecarTest(StoreTestCase):
    """Server-managed agent fields live in agent_meta:<id>, so the previous release still reads agent:<id>."""

    BY = "owner@company.example"

    def old_release_reads(self) -> dict:
        """What the previous release does with agent:<id>: validate_agent_override, unknown fields rejected."""
        row = self.store.get_setting("agent:arch")
        return svc.validate_agent_override(row["value"]) if row else {}

    def test_every_state_keeps_agent_key_in_the_previous_release_shape(self) -> None:
        self.store.merge_agent_setting("arch", {"prompt": "Edited B", "prompt_base_sha": svc.prompt_sha("A"), "prompt_saved_by": self.BY,
                                                "required": True}, self.BY, svc.validate_agent_document)
        self.assertEqual(self.old_release_reads(), {"prompt": "Edited B", "required": True})
        self.store.reconcile_agent_setting("arch", lambda: "C", svc.reconcile_agent_document)
        self.assertEqual(self.old_release_reads(), {"required": True})
        self.assertEqual(self.store.get_agent_setting("arch")["value"]["superseded"]["prompt"], "Edited B")
        self.store.restore_superseded_prompt("arch", svc.prompt_sha("Edited B"), lambda: svc.prompt_sha("C"), self.BY, svc.validate_agent_document)
        self.assertEqual(self.old_release_reads(), {"prompt": "Edited B", "required": True})
        meta = self.store.get_setting("agent_meta:arch")["value"]
        self.assertEqual(set(meta), {"prompt_base_sha", "prompt_saved_at", "prompt_saved_by", "doc_sha"})
        # The sidecar's event names the key only: a kept prompt never rides the event stream.
        self.assertNotIn("Edited B", json.dumps([e for e in self.store.events_after(0, 50) if e["payload"].get("key") == "agent_meta:arch"]))

    def test_an_old_release_edit_makes_the_sidecar_stale_so_nothing_is_replaced_by_mistake(self) -> None:
        # Upgrade: B is saved against A, the file changes to C, B is kept.
        self.store.merge_agent_setting("arch", {"prompt": "Edited B", "prompt_base_sha": svc.prompt_sha("A")}, self.BY, svc.validate_agent_document)
        self.store.reconcile_agent_setting("arch", lambda: "C", svc.reconcile_agent_document)
        # Downgrade: the previous release saves a new prompt into agent:<id> only (its merge), unaware of the sidecar.
        self.store.set_setting("agent:arch", {"prompt": "Old-release edit"}, "old@company.example")
        # Upgrade again: the stale sidecar (base, kept B) no longer applies to this document.
        self.assertEqual(self.store.get_agent_setting("arch")["value"], {"prompt": "Old-release edit"})
        row, action = self.store.reconcile_agent_setting("arch", lambda: "D", svc.reconcile_agent_document)
        self.assertEqual((action, row["value"]["prompt"]), ("adopted", "Old-release edit"), "adopted, not replaced by a stale base")
        # An old-release Reset (agent:<id> deleted) drops the stale kept edit too.
        self.store.merge_agent_setting("arch", {"prompt": "Edited E", "prompt_base_sha": svc.prompt_sha("D")}, self.BY, svc.validate_agent_document)
        self.store.reconcile_agent_setting("arch", lambda: "F", svc.reconcile_agent_document)
        self.store.merge_agent_setting("arch", {"required": True}, self.BY, svc.validate_agent_document)
        self.store.delete_setting("agent:arch", "old@company.example")
        self.assertIsNone(self.store.get_agent_setting("arch"))

    def test_an_old_release_reset_and_identical_resave_cannot_revive_the_sidecar(self) -> None:
        # Prompt-only B against A, then B is kept: agent:<id> stays as the sidecar's anchor.
        self.store.merge_agent_setting("arch", {"prompt": "Edited B", "prompt_base_sha": svc.prompt_sha("A")}, self.BY, svc.validate_agent_document)
        self.store.reconcile_agent_setting("arch", lambda: "C", svc.reconcile_agent_document)
        self.assertEqual(self.store.get_setting("agent:arch")["value"], {})
        anchor = self.store.get_setting("agent:arch")
        # Rollback: the previous release resets the agent, then saves exactly what was there.
        self.store.delete_setting("agent:arch", "old@company.example")
        self.assertIsNone(self.store.get_agent_setting("arch"), "no anchor: the kept edit is gone with the reset")
        with mock.patch("code_review_service.dashboard_store._now", return_value="2099-01-01T00:00:00+00:00"):
            self.store.set_setting("agent:arch", anchor["value"], anchor["updated_by"])
        self.assertEqual(self.store.get_agent_setting("arch")["value"], {}, "same content, another write: still stale")

    def test_reset_deletes_both_keys_and_a_kept_edit_only_when_named(self) -> None:
        self.store.merge_agent_setting("arch", {"prompt": "Edited B", "prompt_base_sha": svc.prompt_sha("A"), "required": True}, self.BY,
                                       svc.validate_agent_document)
        self.store.reconcile_agent_setting("arch", lambda: "C", svc.reconcile_agent_document)
        for named in (None, "0" * 64):
            with self.assertRaises(SupersededConflict):
                self.store.delete_agent_setting("arch", self.BY, named)
        self.assertEqual(self.store.get_agent_setting("arch")["value"]["superseded"]["prompt"], "Edited B")
        self.assertTrue(self.store.delete_agent_setting("arch", self.BY, svc.prompt_sha("Edited B")))
        self.assertEqual((self.store.get_setting("agent:arch"), self.store.get_setting("agent_meta:arch")), (None, None))
        self.assertFalse(self.store.delete_agent_setting("arch", self.BY))
        # Without a kept edit, Reset needs no name.
        self.store.merge_agent_setting("arch", {"prompt": "Edited B", "prompt_base_sha": svc.prompt_sha("C")}, self.BY, svc.validate_agent_document)
        self.assertTrue(self.store.delete_agent_setting("arch", self.BY))

    def test_a_reconcile_write_keeps_who_and_when_of_the_remaining_overrides(self) -> None:
        with mock.patch("code_review_service.dashboard_store._now", return_value="2026-10-01T00:00:00+00:00"):
            self.store.merge_agent_setting("arch", {"prompt": "Edited B", "prompt_base_sha": svc.prompt_sha("A"), "required": True}, self.BY,
                                           svc.validate_agent_document)
        with mock.patch("code_review_service.dashboard_store._now", return_value="2026-10-15T00:00:00+00:00"):
            row, action = self.store.reconcile_agent_setting("arch", lambda: "C", svc.reconcile_agent_document)
        self.assertEqual(action, "superseded")
        self.assertEqual((row["updated_at"], row["updated_by"]), ("2026-10-01T00:00:00+00:00", self.BY))
        self.assertEqual(row["value"]["superseded"]["superseded_at"], "2026-10-15T00:00:00+00:00")

    def test_the_two_keys_are_read_from_one_snapshot(self) -> None:
        # Rollback commits between the reader's statements: the reader must see
        # the state before or after it, never the halves of both.
        self.store.merge_agent_setting("arch", {"prompt": "Edited B", "prompt_base_sha": svc.prompt_sha("A")}, self.BY, svc.validate_agent_document)
        self.store.reconcile_agent_setting("arch", lambda: "C", svc.reconcile_agent_document)
        before = self.store.get_agent_setting("arch")
        other = DashboardStore(self.db_path)
        real_connect = DashboardStore._connect
        fired = []

        class Interleaving:
            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def execute(self, sql, *args):
                cursor = self._conn.execute(sql, *args)
                if "FROM settings" in sql and not fired:
                    fired.append(sql)
                    other.restore_superseded_prompt("arch", svc.prompt_sha("Edited B"), lambda: svc.prompt_sha("C"), self_by, svc.validate_agent_document)
                return cursor

        self_by = self.BY

        @contextlib.contextmanager
        def connect(store):
            with real_connect(store) as conn:
                yield Interleaving(conn) if store is self.store else conn

        with mock.patch.object(DashboardStore, "_connect", connect):
            seen = self.store.get_agent_setting("arch")
        after = self.store.get_agent_setting("arch")
        self.assertTrue(fired)
        self.assertIn(seen, (before, after), "a torn read pairs the old override with the new sidecar")

    def test_a_legacy_prompt_keeps_who_saved_it_across_a_later_field_save(self) -> None:
        with mock.patch("code_review_service.dashboard_store._now", return_value="2026-10-01T00:00:00+00:00"):
            self.store.set_setting("agent:arch", {"prompt": "Legacy B"}, "alice@company.example")  # saved before this release
        self.assertEqual(self.store.reconcile_agent_setting("arch", lambda: "A", svc.reconcile_agent_document)[1], "adopted")
        self.store.merge_agent_setting("arch", {"required": True}, "bob@company.example", svc.validate_agent_document)
        row, action = self.store.reconcile_agent_setting("arch", lambda: "C", svc.reconcile_agent_document)
        kept = row["value"]["superseded"]
        self.assertEqual((action, kept["saved_by"], kept["saved_at"]), ("superseded", "alice@company.example", "2026-10-01T00:00:00+00:00"))

    def test_rollback_bases_the_edit_on_the_file_current_inside_its_transaction(self) -> None:
        self.store.merge_agent_setting("arch", {"prompt": "Edited B", "prompt_base_sha": svc.prompt_sha("A")}, self.BY, svc.validate_agent_document)
        self.store.reconcile_agent_setting("arch", lambda: "C", svc.reconcile_agent_document)
        reads = []

        def read_base():
            reads.append(self.store_in_transaction())
            return svc.prompt_sha("D")

        row = self.store.restore_superseded_prompt("arch", svc.prompt_sha("Edited B"), read_base, self.BY, svc.validate_agent_document)
        self.assertEqual((row["value"]["prompt_base_sha"], reads), (svc.prompt_sha("D"), [True]))

    def store_in_transaction(self) -> bool:
        """True while another connection cannot take the write lock: the caller runs inside BEGIN IMMEDIATE."""
        probe = sqlite3.connect(str(self.db_path), timeout=0, isolation_level=None)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("ROLLBACK")
            return False
        except sqlite3.OperationalError:
            return True
        finally:
            probe.close()

    def test_the_kept_edit_sha_is_the_service_prompt_sha(self) -> None:
        for text in ("Edited B", "  Edited B\n", "\x01 ü"):
            self.assertEqual(prompt_text_sha(text), svc.prompt_sha(text))


class RetryEnqueueEventPolicyTest(unittest.TestCase):
    def test_retry_enqueued_event_carries_the_policy_that_will_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DashboardStore(Path(tmp) / "d.sqlite3")
            policy = {"model": "grok-4.6", "effort": "xhigh", "fallback_models": []}
            store.enqueue_retry("owner/repo", 7, "abc", "owner@company.example", options={"model_policy": policy})
            event = store.events_after(0, 5)[-1]
            self.assertEqual(event["kind"], "retry_enqueued")
            self.assertEqual(event["payload"]["model_policy"], policy)
            store.enqueue_retry("owner/repo", 8, "abc", "owner@company.example")
            self.assertIsNone(store.events_after(0, 5)[-1]["payload"]["model_policy"])


if __name__ == "__main__":
    unittest.main()
