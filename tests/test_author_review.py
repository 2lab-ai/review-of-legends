"""Author-comment admission with real SQLite receipts and queue rows."""

import concurrent.futures
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from code_review_service import author_review, dashboard_execution as dx, mentions as m
from code_review_service.dashboard_store import DashboardStore, PrBusyError
from tests.test_mentions import REPO, issue_comment, review_comment

HEAD = "a" * 40
TIME = "2026-10-15T01:00:00Z"


class AuthorReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DashboardStore(Path(self.tmp.name) / "dashboard.sqlite3")
        self.pr = {"number": 7, "user": {"login": "ALICE"}, "state": "open", "draft": False,
                   "head": {"sha": HEAD}, "merged": False}
        self.client = mock.Mock()
        self.client.get_pr.side_effect = lambda number: dict(self.pr)
        self.ledger = None
        self.store.import_run("first", REPO, 7, HEAD)

    def request(self, comment_id=1, **extra):
        return m.request_from(issue_comment(comment_id, "@review-of-legends review", **extra), m.ISSUE, "bot")

    def apply(self, request=None, language="en"):
        return m.apply_command(
            self.store, REPO, request or self.request(), {"mentions": {"default_reply_language": language}},
            review=lambda command: author_review.apply(
                self.store, REPO, command, self.client, lambda repo, number: self.ledger,
            ),
        )

    def finish(self):
        queued = self.store.claim_retry()
        self.store.finish_retry(queued["id"], "succeeded")

    def complete_run(self, finished_at):
        with mock.patch("code_review_service.dashboard_store._now", return_value="2026-10-15T00:59:00Z"):
            run = self.store.start_run(REPO, 7, HEAD)
        with mock.patch("code_review_service.dashboard_store._now", return_value=finished_at):
            self.store.update_run(run["id"], "succeeded")
        return run

    def assert_no_queue_or_receipt(self):
        self.assertEqual(self.store.list_retries(), [])
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM author_review_requests").fetchone()[0], 0)

    def test_aliases_and_case_only_match_whole_first_word(self):
        for word in ("review", "rereview", "retrigger", "REVIEW", "ReReview", "RETRIGGER", "review please"):
            with self.subTest(word=word):
                self.assertEqual(m.parse_command(word), m.Command(m.REVIEW))
        self.assertEqual(m.parse_command("reviewer foo"), m.Command(m.FEEDBACK, "reviewer foo"))

    def test_author_and_latest_attempt_are_read_live(self):
        self.store.import_run("newer", REPO, 7, HEAD, status="failed", stage="reviewing")
        self.assertIn("queued", self.apply())
        self.client.get_pr.assert_called_once_with(7)
        queued = self.store.list_retries()[0]
        self.assertEqual(queued["retry_of"], "newer")
        self.assertEqual(queued["requested_by"], "github:Alice")
        self.assertEqual(queued["options"], {"kind": "rereview", "scope": "all", "seen_at": TIME,
                                             "source_key": "owner/repo#issue#1"})

    def test_non_author_refused_in_both_languages(self):
        for language, expected in (("en", "Only the PR author"), ("ko", "PR 작성자뿐")):
            body = self.apply(self.request(login="Bob"), language)
            self.assertIn(expected, body)
            self.assertIn("Retrigger", body)
        self.assertEqual(self.store.list_retries(), [])

    def test_closed_merged_and_draft_refused(self):
        for change, reason in (({"state": "closed"}, "closed or merged"),
                               ({"merged": True}, "closed or merged"), ({"draft": True}, "draft")):
            with self.subTest(change=change):
                original = dict(self.pr)
                self.pr.update(change)
                self.assertIn(reason, self.apply())
                self.assertEqual(self.store.list_retries(), [])
                self.pr = original

    def test_active_request_or_run_on_any_head_refused(self):
        queued = self.store.enqueue_retry(REPO, 7, "old-head", "admin")
        self.assertIn("already queued or running", self.apply())
        self.assertEqual([row["id"] for row in self.store.list_retries()], [queued["id"]])
        self.finish()
        self.store.start_run(REPO, 7, "other-head")
        self.assertIn("already queued or running", self.apply())
        self.assertEqual(len(self.store.list_retries()), 1)

    def test_enqueue_busy_race_is_a_reply(self):
        with mock.patch.object(self.store, "enqueue_retry", side_effect=PrBusyError("busy")):
            self.assertIn("already queued or running", self.apply())
        self.assertEqual(self.store.list_retries(), [])

    def test_unknown_failed_and_baseline_heads_wait_for_first_review(self):
        self.pr["head"] = {"sha": "new"}
        for status in (None, "failed", "skipped"):
            with self.subTest(status=status):
                if status:
                    self.store.import_run(status, REPO, 7, "new", status=status)
                self.ledger = {"head_sha": "new", "initial_baseline_without_review": True}
                self.assertIn("on its way", self.apply())
                self.assertEqual(self.store.list_retries(), [])

    def test_old_head_ledger_waits_for_current_heads_first_review(self):
        self.pr["head"] = {"sha": "new"}
        self.ledger = {"head_sha": HEAD, "reviewed_at": "2026-10-14T01:00:00+00:00"}
        self.assertIn("on its way", self.apply())
        self.assert_no_queue_or_receipt()

    def test_completed_after_comment_without_ledger_survives_restart(self):
        self.complete_run("2026-10-15T01:00:01Z")
        # The durable success survives a daemon restart even if saving the
        # JSON ledger failed before the author command was processed.
        self.store = DashboardStore(self.store.path)
        self.assertIsNone(self.ledger)
        for language, expected in (("en", "already answers this request"), ("ko", "이미 처리되었습니다")):
            with self.subTest(language=language):
                body = self.apply(language=language)
                self.assertIn(expected, body)
                self.assertIn(HEAD[:12], body)
                self.assert_no_queue_or_receipt()

    def test_completed_before_comment_queues_with_completed_run_baseline(self):
        run = self.complete_run("2026-10-15T00:59:59Z")
        self.assertIn("queued", self.apply())
        queued = self.store.list_retries()[0]
        self.assertEqual(queued["retry_of"], run["id"])
        self.assertEqual(queued["options"]["seen_at"], TIME)

    def test_completed_at_exact_comment_time_is_already_answered(self):
        at = "2026-10-15T01:00:00.123456Z"
        self.complete_run(at)
        self.assertIn("already answers this request", self.apply(self.request(updated_at=at)))
        self.assert_no_queue_or_receipt()

    def test_whole_second_completion_compares_to_truncated_comment_time(self):
        self.complete_run("2026-10-15T01:00:00Z")
        request = self.request(updated_at="2026-10-15T01:00:00.999999Z")
        self.assertIn("already answers this request", self.apply(request))
        self.assert_no_queue_or_receipt()

    def test_precise_completion_before_comment_in_same_second_still_queues(self):
        run = self.complete_run("2026-10-15T01:00:00.123456Z")
        request = self.request(updated_at="2026-10-15T01:00:00.654321Z")
        self.assertIn("queued", self.apply(request))
        queued = self.store.list_retries()[0]
        self.assertEqual(queued["retry_of"], run["id"])
        self.assertEqual(queued["options"]["seen_at"], request.updated)

    def test_explicit_zero_microseconds_are_exact_completion_time(self):
        run = self.complete_run("2026-10-15T01:00:00.000000Z")
        request = self.request(updated_at="2026-10-15T01:00:00.654321Z")
        self.assertIn("queued", self.apply(request))
        self.assertEqual(self.store.list_retries()[0]["retry_of"], run["id"])

    def test_unreadable_completion_time_propagates_without_consuming_request(self):
        self.complete_run("unreadable")
        with self.assertRaisesRegex(RuntimeError, "could not compare"):
            self.apply()
        self.assert_no_queue_or_receipt()

    def test_completed_after_comment_is_found_before_newer_failed_attempt(self):
        self.complete_run("2026-10-15T01:00:01Z")
        self.store.import_run("later-failure", REPO, 7, HEAD, status="failed",
                              created_at="2026-10-15T01:00:02Z")
        self.assertIn("already answers this request", self.apply())
        self.assert_no_queue_or_receipt()

    def test_ledger_review_without_attempt_is_allowed(self):
        self.pr["head"] = {"sha": "new"}
        self.ledger = {"head_sha": "new", "reviewed_at": "2026-10-14T01:00:00+00:00"}
        self.assertIn("queued", self.apply())
        self.assertIsNone(self.store.list_retries()[0]["retry_of"])

    def test_ledger_review_after_comment_without_attempt_is_already_answered(self):
        # The poll's recorder is best effort: a posted review can lack its
        # attempt row, and the late check would refuse the queued request as stale.
        self.pr["head"] = {"sha": "new"}
        self.ledger = {"head_sha": "new", "reviewed_at_exact": "2026-10-15T01:00:00.500000+00:00"}
        body = self.apply()
        self.assertIn("already answers this request", body)
        self.assertIn("`new`", body)
        self.assert_no_queue_or_receipt()

    def test_ledger_review_after_comment_answers_despite_earlier_completed_run(self):
        self.complete_run("2026-10-15T00:59:59Z")
        self.ledger = {"head_sha": HEAD, "reviewed_at_exact": "2026-10-15T01:00:00.500000+00:00"}
        body = self.apply()
        self.assertIn("already answers this request", body)
        self.assertIn(HEAD[:12], body)
        self.assert_no_queue_or_receipt()

    def test_unreadable_ledger_review_time_propagates_without_consuming_request(self):
        self.ledger = {"head_sha": HEAD, "reviewed_at": "unreadable"}
        with self.assertRaisesRegex(RuntimeError, "could not compare"):
            self.apply()
        self.assert_no_queue_or_receipt()

    def test_unknown_posting_outcomes_are_refused(self):
        self.store.import_run("posting", REPO, 7, HEAD, status="interrupted", stage="posting")
        self.assertIn("outcome is unknown", self.apply())
        self.assertEqual(self.store.list_retries(), [])

    def test_unverified_ledger_is_refused_even_after_success(self):
        self.ledger = {"head_sha": HEAD, "post_outcome_unknown": True}
        self.assertIn("outcome is unknown", self.apply())
        self.assertEqual(self.store.list_retries(), [])

    def test_same_comment_replayed_after_completion_and_head_move_queues_once(self):
        first = self.apply()
        self.assertEqual(self.apply(), first)
        self.finish()
        self.pr["head"] = {"sha": "new"}
        self.store.import_run("new", REPO, 7, "new")
        self.store = DashboardStore(self.store.path)
        self.assertEqual(self.apply(), first)
        self.assertEqual(len(self.store.list_retries()), 1)

    def test_limit_survives_restart_but_new_head_and_badge_are_allowed(self):
        self.apply()
        self.finish()
        self.store = DashboardStore(self.store.path)
        self.assertIn("push a commit", self.apply(self.request(2)))
        badge = self.store.enqueue_retry(REPO, 7, HEAD, "admin@example.com", options={"kind": "rereview"})
        self.assertEqual(badge["status"], "queued")
        self.finish()
        self.pr["head"] = {"sha": "new"}
        self.store.import_run("new", REPO, 7, "new")
        self.assertIn("queued", self.apply(self.request(3)))
        self.assertEqual(len(self.store.list_retries()), 3)

    def test_created_at_fallback_and_review_thread_source(self):
        item = review_comment(1, "@review-of-legends review")
        item.pop("updated_at")
        request = m.request_from(item, m.REVIEW, "bot")
        self.assertIn("queued", self.apply(request))
        options = self.store.list_retries()[0]["options"]
        self.assertEqual(options["seen_at"], TIME)
        self.assertEqual(options["source_key"], "owner/repo#review#1")

    def test_help_and_welcome_explain_author_and_per_commit_limit(self):
        for language, words in (("ko", ("작성자", "커밋당 1회")), ("en", ("author only", "once per commit"))):
            help_request = self.request()._replace(command=m.Command(m.SETTINGS))
            help_body = m.apply_command(self.store, REPO, help_request, {"mentions": {"default_reply_language": language}})
            for body in (help_body, m.welcome_body("Alice", language)):
                self.assertIn("@review-of-legends review", body)
                for word in words:
                    self.assertIn(word, body)

    def test_comment_time_is_used_by_late_check_on_python39(self):
        self.apply()
        queued = self.store.list_retries()[0]
        entry = {"head_sha": HEAD, "reviewed_at_exact": "2026-10-15T00:59:59.999999+00:00"}
        dx.rereview_late_check(self.store, REPO, 7, HEAD, "first", lambda repo, pr: entry,
                               seen_at=queued["options"]["seen_at"])
        entry["reviewed_at_exact"] = "2026-10-15T01:00:00.000001+00:00"
        with self.assertRaises(dx.RetryRefused):
            dx.rereview_late_check(self.store, REPO, 7, HEAD, "first", lambda repo, pr: entry,
                                   seen_at=queued["options"]["seen_at"])

    def test_concurrent_replays_return_one_request(self):
        def enqueue(_):
            return self.store.enqueue_retry(REPO, 7, HEAD, "github:Alice", options={
                "kind": "rereview", "source_key": "owner/repo#issue#1", "scope": "all", "seen_at": TIME,
            })["id"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(enqueue, range(2)))
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(len(self.store.list_retries()), 1)

    def test_failed_transaction_leaves_no_queue_or_consumed_allowance(self):
        with mock.patch.object(self.store, "_emit", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(self.store.list_retries(), [])
        self.assertIsNone(self.store.comment_review_request("owner/repo#issue#1"))
        self.assertIn("queued", self.apply(self.request(2)))

    def test_different_concurrent_comments_admit_only_one(self):
        def apply(comment_id):
            return self.apply(self.request(comment_id))
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(apply, (1, 2)))
        self.assertEqual(sum("A fresh review" in body for body in replies), 1)
        self.assertEqual(sum("already queued or running" in body for body in replies), 1)
        self.assertEqual(len(self.store.list_retries()), 1)

    def test_reply_uses_commenters_saved_language(self):
        self.store.set_author_language("Alice", "ko")
        self.assertIn("대기열에 등록", self.apply())
        self.finish()
        self.assertIn("새 커밋을 푸시", self.apply(self.request(2)))


if __name__ == "__main__":
    unittest.main()
