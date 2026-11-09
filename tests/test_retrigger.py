"""Retrigger: the badge on PR comments and the daemon's rereview path.

A rereview is a dashboard request with ``options.kind == "rereview"``: a fresh
review of the PR's current head on purpose, posted again even though the head
already carries one. The web layer (test_dashboard_web.py) decides who may ask;
this module covers what the daemon does with the request and what the posted
comments link.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import dashboard_execution as dx
from code_review_service import dashboard_history
from code_review_service import service as svc
from tests.test_dashboard_integration import FakeClient, FakeStore
from tests.test_scheduler import REPO, SchedulerTestCase, pr as scheduler_pr
from tests.test_visual_outline import VISUAL_MARKER, _FakeClient as _OutlineClient, _ReviewHarness, _make_config, _visual_block

PUBLIC_URL = "https://rol.company.example"
HEAD = "abc1234567"


def _dashboard_config(public_url: str | None = PUBLIC_URL, **dashboard) -> dict:
    block = dict(dashboard)
    if public_url is not None:
        block["public_url"] = public_url
    return {"dashboard": block} if block else {}


class _SummaryClient:
    def __init__(self, repo: str = "owner/repo") -> None:
        self.repo = repo


# ---------------------------------------------------------------------------
# The badge on the review and the executive summary
# ---------------------------------------------------------------------------


class RetriggerBadgeTest(unittest.TestCase):
    def _review(self, config: dict, repo: str = "owner/repo") -> str:
        return svc.build_review_body(
            config, {"number": 7, "head": {"sha": HEAD}}, [{"agent": "a", "summary": "s", "findings": []}], [], 0,
            version="v-test", repo=repo,
        )

    def _summary(self, config: dict, agent_body: str | None, repo: str = "owner/repo") -> str:
        return svc.build_executive_summary_body(
            config, _SummaryClient(repo), {"number": 7, "title": "t", "head": {"sha": HEAD}}, [], [], [], agent_body
        )

    def test_the_review_heading_carries_the_badge_link(self) -> None:
        body = self._review(_dashboard_config())
        self.assertIn(
            '<h2><a href="https://rol.company.example/#retrigger/owner/repo/7"><picture>'
            '<source media="(prefers-color-scheme: dark)" srcset="' + svc.DEFAULT_RETRIGGER_BADGE["dark"] + '">'
            '<source media="(prefers-color-scheme: light)" srcset="' + svc.DEFAULT_RETRIGGER_BADGE["light"] + '">'
            '<img alt="Retrigger" src="' + svc.DEFAULT_RETRIGGER_BADGE["light"] + '" align="right">'
            "</picture></a>Review of Legends</h2>",
            body,
        )
        self.assertNotIn("## Review of Legends", body)
        self.assertTrue(body.startswith(svc.MARKER))

    def test_the_summary_heading_carries_the_badge_in_the_agent_and_fallback_bodies(self) -> None:
        agent = "<h3>Review of Legends Summary</h3>\n\ntext\n\n<h3>Confidence Score: 5/5</h3>"
        with_agent = self._summary(_dashboard_config(), agent)
        self.assertIn('<h3><a href="https://rol.company.example/#retrigger/owner/repo/7">', with_agent)
        self.assertEqual(with_agent.count("<picture>"), 1, "one badge, on the first heading")
        self.assertIn("<h3>Confidence Score: 5/5</h3>", with_agent, "only the first heading carries it")
        fallback = self._summary(_dashboard_config(), None)
        self.assertIn('<h3><a href="https://rol.company.example/#retrigger/owner/repo/7">', fallback)
        self.assertIn("Review of Legends Summary</h3>", fallback)
        without_heading = self._summary(_dashboard_config(), "plain text only")
        self.assertTrue(without_heading.split("\n\n")[1].startswith('<a href="https://rol.company'), without_heading)

    def test_the_href_is_the_repository_and_number_url_encoded(self) -> None:
        body = self._review(_dashboard_config(public_url="https://rol.example/"), repo="own er/re po.v2")
        self.assertIn('href="https://rol.example/#retrigger/own%20er/re%20po.v2/7"', body)
        self.assertEqual(svc.retrigger_page_url(_dashboard_config(), "owner", 7), "", "no slash, no page")
        self.assertEqual(svc.retrigger_page_url(_dashboard_config(), "owner/repo", None), "")

    def test_no_public_url_means_no_badge(self) -> None:
        for config in ({}, {"dashboard": {}}, _dashboard_config(public_url="http://rol.example")):
            with self.subTest(config=config):
                review = self._review(config)
                self.assertIn("## Review of Legends", review)
                self.assertNotIn("Retrigger", review)
                summary = self._summary(config, "<h3>Review of Legends Summary</h3>\ntext")
                self.assertIn("<h3>Review of Legends Summary</h3>", summary)
                self.assertNotIn("Retrigger", summary)
        self.assertEqual(svc.retrigger_badge_html({}, "owner/repo", 7), "")

    def test_attribute_values_are_html_escaped(self) -> None:
        config = _dashboard_config(
            public_url='https://rol.example/"><script>x</script>',
            retrigger_badge={"light": 'https://img.example/l.svg?a=1&b="x"', "dark": "https://img.example/d.svg"},
        )
        body = self._review(config, repo='own"er/re<po')
        self.assertNotIn('"><script>', body)
        self.assertNotIn('b="x"', body)
        self.assertIn("&quot;", body)
        self.assertIn("&amp;", body)
        self.assertIn("&lt;", body)

    def test_the_dark_image_url_is_html_escaped_too(self) -> None:
        config = _dashboard_config(
            retrigger_badge={"light": "https://img.example/l.svg", "dark": 'https://img.example/d.svg"><img src=x onerror=alert(1)>'},
        )
        body = self._review(config)
        self.assertNotIn('"><img src=x', body)
        self.assertIn('srcset="https://img.example/d.svg&quot;&gt;&lt;img src=x onerror=alert(1)&gt;"', body)

    def test_the_badge_images_can_be_configured(self) -> None:
        config = _dashboard_config(retrigger_badge={"light": "https://img.example/l.svg", "dark": "https://img.example/d.svg"})
        body = self._review(config)
        self.assertIn('srcset="https://img.example/d.svg"', body)
        self.assertIn('srcset="https://img.example/l.svg"', body)
        self.assertIn('src="https://img.example/l.svg"', body)
        self.assertNotIn("shields.io", body)
        partial = self._review(_dashboard_config(retrigger_badge={"dark": "https://img.example/d.svg"}))
        self.assertIn('srcset="https://img.example/d.svg"', partial)
        self.assertIn(svc.DEFAULT_RETRIGGER_BADGE["light"], partial, "the other image keeps its default")


class RetriggerBadgeConfigTest(unittest.TestCase):
    def _load(self, dashboard: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviewer.json"
            path.write_text(json.dumps({
                "repositories": ["o/r"],
                "agents": [{"id": i, "prompt_path": "x"} for i in ("a", "b", "c")],
                "dashboard": dashboard,
            }), encoding="utf-8")
            return svc.load_config(path)

    def test_a_valid_override_loads(self) -> None:
        config = self._load({"public_url": PUBLIC_URL, "retrigger_badge": {"light": "https://a/l.svg", "dark": "https://a/d.svg"}})
        self.assertEqual(config["dashboard"]["retrigger_badge"]["dark"], "https://a/d.svg")
        self._load({"public_url": PUBLIC_URL})  # absent block: the defaults apply

    def test_a_non_https_or_malformed_override_is_refused_at_load(self) -> None:
        for bad in (
            {"light": "http://a/l.svg", "dark": "https://a/d.svg"},
            {"light": "https://a/l.svg", "dark": "ftp://a/d.svg"},
            {"light": 7},
            "https://a/l.svg",
            [],
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(svc.ServiceError) as caught:
                    self._load({"public_url": PUBLIC_URL, "retrigger_badge": bad})
                self.assertIn("retrigger_badge", str(caught.exception))


# ---------------------------------------------------------------------------
# retry_decision: the rereview kind
# ---------------------------------------------------------------------------


class RereviewDecisionTest(unittest.TestCase):
    """A rereview may re-post on a reviewed head; it keeps every unknown-outcome refusal."""

    request = {"id": "req-1", "repo": "owner/repo", "pr": 1, "head_sha": HEAD,
               "options": {"kind": "rereview", "scope": "all"}}
    plain = {"id": "req-1", "repo": "owner/repo", "pr": 1, "head_sha": HEAD, "options": {"scope": "all"}}

    def test_allowed_with_force_after_a_successful_attempt(self) -> None:
        previous = {"id": "run-1", "status": "succeeded", "head_sha": HEAD, "stage": "posted",
                    "review_url": "https://github.invalid/r/1"}
        decision = dx.retry_decision(self.request, previous, None)
        self.assertTrue(decision.allow, decision)
        self.assertTrue(decision.force)

    def test_allowed_with_force_on_a_ledger_reviewed_head(self) -> None:
        ledger = {"head_sha": HEAD, "review_event": "COMMENT"}
        for previous in (None, {"id": "run-1", "status": "failed", "head_sha": HEAD, "stage": "agents"}):
            with self.subTest(previous=previous):
                decision = dx.retry_decision(self.request, previous, ledger)
                self.assertTrue(decision.allow, decision)
                self.assertTrue(decision.force)

    def test_refused_when_the_head_moved(self) -> None:
        previous = {"id": "run-1", "status": "succeeded", "head_sha": "0000000000", "stage": "posted"}
        decision = dx.retry_decision(self.request, previous, None)
        self.assertFalse(decision.allow)
        self.assertEqual(decision.status, "stale")

    def test_refused_when_the_posting_outcome_is_unknown(self) -> None:
        for previous in (
            {"id": "run-1", "status": "interrupted", "head_sha": HEAD, "stage": dx.POSTING_STAGE},
            {"id": "run-1", "status": "failed", "head_sha": HEAD, "stage": "agents", "review_url": "https://github.invalid/r/1"},
        ):
            with self.subTest(previous=previous):
                decision = dx.retry_decision(self.request, previous, None)
                self.assertFalse(decision.allow)
                self.assertIn("unknown", decision.reason)

    def test_refused_while_the_previous_attempt_is_still_running(self) -> None:
        previous = {"id": "run-1", "status": "running", "head_sha": HEAD, "stage": "agents"}
        self.assertFalse(dx.retry_decision(self.request, previous, None).allow)

    def test_refused_on_an_unverified_ledger_post(self) -> None:
        ledger = {"head_sha": HEAD, "post_outcome_unknown": True}
        for previous in (None, {"id": "run-1", "status": "succeeded", "head_sha": HEAD, "stage": "posted"}):
            with self.subTest(previous=previous):
                decision = dx.retry_decision(self.request, previous, ledger)
                self.assertFalse(decision.allow)
                self.assertIn("unverified", decision.reason)

    def test_a_plain_retry_is_still_refused_after_a_success(self) -> None:
        """Regression guard: only the rereview kind relaxes the duplicate-POST rules."""
        previous = {"id": "run-1", "status": "succeeded", "head_sha": HEAD, "stage": "posted"}
        for request in (self.plain, {**self.plain, "options": {"kind": "retry"}}, {**self.plain, "options": None}):
            with self.subTest(request=request):
                decision = dx.retry_decision(request, previous, None)
                self.assertFalse(decision.allow)
                self.assertIn("previous attempt succeeded", decision.reason)
        ledger = {"head_sha": HEAD, "review_event": "COMMENT"}
        failed = {"id": "run-1", "status": "failed", "head_sha": HEAD, "stage": "agents"}
        self.assertFalse(dx.retry_decision(self.plain, failed, ledger).allow)


# ---------------------------------------------------------------------------
# The executor: trigger, runner flag, native rows
# ---------------------------------------------------------------------------


class RereviewExecutorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = FakeStore()
        self.lock = dx.ExecutorLock(Path(self._tmp.name) / "executor.lock")
        self.lock.acquire()
        self.addCleanup(self.lock.release)
        self.client = FakeClient()
        self.calls: list = []

    def _drain(self, ledger=None, **request) -> int:
        def runner(client, pr, recorder=None, force=False, **kwargs):
            self.calls.append({"force": force, **kwargs})
            recorder.begin(client.repo, pr["number"], pr["head"]["sha"])
            recorder.succeeded(review_url="https://github.invalid/r/2")
            return True

        self.store.enqueue_retry("owner/repo", 1, HEAD, "owner@company.example", **request)
        return dx.drain_retry_requests(
            self.store, self.lock, ["owner/repo"], lambda repo: self.client, runner, lambda repo, pr: ledger,
            logger=lambda message: None,
        )

    def test_the_executor_refuses_a_rereview_at_claim_without_running_it(self) -> None:
        """The claim-time rules, through the executor, not only as a pure function."""
        unknown = self.store.start_run("owner/repo", 1, HEAD)
        self.store.update_run(unknown["id"], "interrupted", stage=dx.POSTING_STAGE)
        self._drain(retry_of=unknown["id"], options={"kind": "rereview", "scope": "all"})
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.retry_status("req-1"), "failed")

        self._drain(ledger={"head_sha": HEAD, "post_outcome_unknown": True}, options={"kind": "rereview", "scope": "all"})
        self.assertEqual(self.calls, [], "an unverified ledger POST must refuse at claim too")

    def test_a_rereview_runs_forced_with_the_rereview_flag_and_a_retrigger_trigger(self) -> None:
        earlier = self.store.start_run("owner/repo", 1, HEAD)
        self.store.update_run(earlier["id"], "succeeded", stage="posted", review_url="https://github.invalid/r/1")
        self.assertEqual(self._drain(retry_of=earlier["id"], options={"kind": "rereview", "scope": "all"}), 1)
        self.assertEqual(self.calls, [{"force": True, "rereview": True}])
        attempt = self.store.get_run("run-2")
        self.assertEqual(attempt["trigger"], "retrigger")
        self.assertEqual(attempt["retry_of"], earlier["id"])
        self.assertEqual(self.store.retry_status("req-1"), "succeeded")

    def test_a_rereview_without_an_earlier_attempt_runs_too(self) -> None:
        self.assertEqual(self._drain(options={"kind": "rereview", "scope": "all"}), 1)
        self.assertEqual(self.calls, [{"force": True, "rereview": True}])
        self.assertEqual(self.store.get_run("run-1")["trigger"], "retrigger")

    def test_a_plain_retry_keeps_its_trigger_and_passes_no_flag(self) -> None:
        earlier = self.store.start_run("owner/repo", 1, HEAD)
        self.store.update_run(earlier["id"], "failed", stage="agents")
        self.assertEqual(self._drain(retry_of=earlier["id"], options={"scope": "all"}), 1)
        self.assertEqual(self.calls, [{"force": False}])
        self.assertEqual(self.store.get_run("run-2")["trigger"], "retry")

    def test_the_recorder_carries_the_page_snapshot_time(self) -> None:
        """The late check compares the ledger's review time with the page's snapshot
        (``seen_at``, bot P2 on 64059fb); a request queued without one falls back to
        when it was queued."""
        seen = []

        def runner(client, pr, recorder=None, force=False, **kwargs):
            seen.append(recorder.seen_at)
            recorder.begin(client.repo, pr["number"], pr["head"]["sha"])
            recorder.succeeded(review_url="https://github.invalid/r/2")
            return True

        snapshot = "2026-09-10T23:59:00+00:00"
        self.store.enqueue_retry(
            "owner/repo", 1, HEAD, "owner@company.example",
            options={"kind": "rereview", "scope": "all", dx.RETRY_SEEN_AT_OPTION: snapshot},
        )
        self.store.enqueue_retry("owner/repo", 2, HEAD, "owner@company.example", options={"kind": "rereview", "scope": "all"})
        dx.drain_retry_requests(
            self.store, self.lock, ["owner/repo"], lambda repo: self.client, runner, lambda repo, pr: None,
            logger=lambda message: None,
        )
        self.assertEqual(seen, [snapshot, self.store.requests["req-2"]["created_at"]])

    def test_retrigger_attempts_are_native_rows_for_the_history_import(self) -> None:
        self.assertIn("retrigger", dx.NATIVE_TRIGGERS)
        self.assertIn("retrigger", dashboard_history._NATIVE_TRIGGERS)


class RereviewLateCheckLedgerTest(unittest.TestCase):
    """``rereview_late_check`` against the ledger's review time (bot red-team P2 on dab3378):
    ``seen_at`` is when the page read the snapshot the click was made against."""

    CLICK = "2026-09-11T00:00:00+00:00"

    def _check(self, entry: Optional[Dict[str, Any]], seen_at: Optional[str] = CLICK) -> None:
        dx.rereview_late_check(FakeStore(), "owner/repo", 1, HEAD, None, lambda repo, pr: entry, seen_at)

    def test_a_review_posted_after_the_click_refuses_as_stale(self) -> None:
        with self.assertRaises(dx.RetryRefused) as caught:
            self._check({"head_sha": HEAD, "reviewed_at": "2026-09-11T00:00:01+00:00"})
        self.assertEqual(caught.exception.status, "stale")
        self.assertIn("reviewed meanwhile", caught.exception.reason)

    def test_a_review_from_before_the_click_or_of_another_head_lets_it_run(self) -> None:
        self._check({"head_sha": HEAD, "reviewed_at": "2026-09-10T23:59:59+00:00"})
        self._check({"head_sha": "o" * 40, "reviewed_at": "2026-09-11T00:00:01+00:00"})
        self._check(None)

    def test_the_snapshot_second_is_split_by_the_exact_review_time(self) -> None:
        """Bot P2 on 99f4f37 and the review gate: whole seconds cannot order a review
        against a snapshot taken in the same second; the ledger's exact time can."""
        seen = "2026-09-11T00:00:00.100000+00:00"
        with self.assertRaises(dx.RetryRefused) as caught:
            self._check({"head_sha": HEAD, "reviewed_at": self.CLICK, "reviewed_at_exact": "2026-09-11T00:00:00.900000+00:00"}, seen)
        self.assertEqual(caught.exception.status, "stale")
        self._check({"head_sha": HEAD, "reviewed_at": self.CLICK, "reviewed_at_exact": "2026-09-11T00:00:00.050000+00:00"}, seen)

    def test_an_exact_review_time_equal_to_the_snapshot_refuses(self) -> None:
        """Bot P2 on 0f004f8: microseconds are not unique ordering tokens; equality is
        ambiguous and refuses, like the whole-second fallback."""
        seen = "2026-09-11T00:00:00.100000+00:00"
        with self.assertRaises(dx.RetryRefused) as caught:
            self._check({"head_sha": HEAD, "reviewed_at": self.CLICK, "reviewed_at_exact": seen}, seen)
        self.assertEqual(caught.exception.status, "stale")

    def test_a_whole_second_review_in_the_snapshot_second_refuses(self) -> None:
        """An entry with only whole seconds (written before reviewed_at_exact) cannot be
        ordered within its second: that ambiguity refuses rather than allows."""
        for seen in (self.CLICK, "2026-09-11T00:00:00.900000+00:00"):
            with self.subTest(seen=seen):
                with self.assertRaises(dx.RetryRefused) as caught:
                    self._check({"head_sha": HEAD, "reviewed_at": self.CLICK}, seen)
                self.assertEqual(caught.exception.status, "stale")

    def test_a_baseline_entry_is_not_a_review(self) -> None:
        self._check({"head_sha": HEAD, "reviewed_at": "2026-09-11T00:00:01+00:00", "initial_baseline_without_review": True})

    def test_an_unreadable_review_time_refuses_rather_than_allows(self) -> None:
        with self.assertRaises(dx.RetryRefused) as caught:
            self._check({"head_sha": HEAD, "reviewed_at": "not a time"})
        self.assertEqual(caught.exception.status, "failed")
        self.assertIn("late check", caught.exception.reason)


class RereviewSchedulerRunnerTest(SchedulerTestCase):
    """The retry thread's own "reviewed meanwhile" guard must let a rereview through."""

    def test_a_rereview_reviews_a_head_the_ledger_already_records(self) -> None:
        head = "h" * 40
        seen: list = []

        def fake_review_pr(client, config, config_path, pr_payload, state, **kwargs):
            seen.append(kwargs)
            return True

        self.listing.prs[REPO] = [scheduler_pr(5, head)]
        with mock.patch.object(svc, "review_pr", fake_review_pr):
            scheduler = self.make(reviewed={"5": {"head_sha": head, "review_event": "COMMENT", "reviewed_at": "t"}})
            self.assertTrue(scheduler._reload_state())
            client = self.listing.client(REPO)
            recorder = dx.DashboardRecorder(FakeStore(), logger=lambda message: None)
            # Baseline: a forced plain retry of a reviewed head is not run twice.
            self.assertFalse(scheduler._retry_review_runner(client, scheduler_pr(5, head), recorder=recorder, force=True))
            self.assertEqual(seen, [])
            self.assertTrue(
                scheduler._retry_review_runner(client, scheduler_pr(5, head), recorder=recorder, force=True, rereview=True)
            )
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0]["force"])
        self.assertTrue(seen[0]["rereview"])
        self.assertIsNone(seen[0]["reuse_results"])


class RereviewLateRefusalTest(SchedulerTestCase):
    """Reviewer MUST-FIX: a rereview bypasses the stale ``reviewed_meanwhile``
    guard on purpose, but it must not bypass freshness: between
    ``retry_decision`` allowing the request and the rereview actually owning
    the PR (it may wait behind the hard cap or a running pool attempt), a pool
    attempt for the same PR can land with an unknown posting outcome, or the
    ledger can record an unverified POST. The scheduler re-checks both, with
    ``retry_decision``'s own reason text, after it reserves the PR and before
    ``review_pr`` runs.
    """

    def _run(self, head: str, store, reviewed: Optional[Dict[str, Any]] = None):
        seen: list = []

        def fake_review_pr(client, config, config_path, pr_payload, state, **kwargs):
            seen.append(kwargs)
            return True

        self.listing.prs[REPO] = [scheduler_pr(5, head)]
        with mock.patch.object(svc, "review_pr", fake_review_pr):
            scheduler = self.make(reviewed=reviewed, store=store)
            self.assertTrue(scheduler._reload_state())
            client = self.listing.client(REPO)
            recorder = dx.DashboardRecorder(FakeStore(), logger=lambda message: None)
            try:
                result = scheduler._retry_review_runner(
                    client, scheduler_pr(5, head), recorder=recorder, force=True, rereview=True
                )
            except dx.RetryRefused as refused:
                # A late refusal carries its reason to the request row.
                result = refused
        return result, seen

    def test_a_pool_attempt_completing_meanwhile_with_an_unknown_outcome_blocks_the_rereview(self) -> None:
        head = "h" * 40
        store = FakeStore()
        run = store.start_run(REPO, 5, head, trigger="poll")
        store.update_run(run["id"], "interrupted", stage=dx.POSTING_STAGE)

        result, seen = self._run(head, store)

        self.assertIsInstance(result, dx.RetryRefused)
        self.assertIn("outcome is unknown", result.reason)
        self.assertEqual(seen, [])

    def test_a_ledger_unverified_post_for_the_head_meanwhile_blocks_the_rereview(self) -> None:
        head = "h" * 40
        result, seen = self._run(head, FakeStore(), reviewed={"5": {"head_sha": head, "post_outcome_unknown": True}})

        self.assertIsInstance(result, dx.RetryRefused)
        self.assertIn("unverified", result.reason)
        self.assertEqual(seen, [])

    def test_an_ordinary_reviewed_ledger_entry_still_runs(self) -> None:
        """Regression guard: the late check must not re-introduce the stale-head refusal."""
        head = "h" * 40
        result, seen = self._run(
            head, FakeStore(), reviewed={"5": {"head_sha": head, "review_event": "COMMENT", "reviewed_at": "t"}}
        )

        self.assertTrue(result)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0]["force"])

    def test_a_raising_late_lookup_fails_closed_even_on_an_otherwise_permissive_ledger(self) -> None:
        """Round 2 MUST-FIX: swallowing the lookup's exception left ``latest=None``,
        which ``_rereview_decision`` treats as "no previous attempt" -- allowed,
        with force, on a ledger that is otherwise permissive. An unreadable late
        check must refuse, not silently fall back to allow.
        """
        head = "h" * 40

        def boom(repo: str, pr: int, head_sha: str) -> None:
            raise RuntimeError("store unreachable")

        store = FakeStore()
        store.latest_attempt_for_head = boom  # type: ignore[method-assign]
        result, seen = self._run(
            head, store, reviewed={"5": {"head_sha": head, "review_event": "COMMENT", "reviewed_at": "t"}}
        )

        self.assertIsInstance(result, dx.RetryRefused)
        self.assertEqual(result.status, "failed")
        self.assertEqual(seen, [])
        # The upstream text rides on __cause__ to the executor's log, not the request row.
        self.assertIn("store unreachable", str(result.__cause__))
        self.assertNotIn("store unreachable", result.reason)


# ---------------------------------------------------------------------------
# review_pr: a rereview refreshes the visual outline
# ---------------------------------------------------------------------------


class RereviewOutlineRefreshTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.pr = {"number": 7, "head": {"sha": HEAD}, "title": "t", "body": ""}
        self.existing = [{"id": 1, "body": f"{VISUAL_MARKER}\nold outline", "user": {"login": "review-of-legends[bot]"}}]

    def test_a_rereview_runs_the_outline_agent_again_and_edits_the_comment_in_place(self) -> None:
        client = _OutlineClient(comments=list(self.existing))
        config = _make_config(self.tmp_path / "work", _visual_block())
        harness = _ReviewHarness(unittest.TestCase(), outline="## 왜 이 변경인가\n새 문장.\n\n## 다이어그램\n\n```mermaid\nflowchart TD\n  A[\"a\"] --> B[\"b\"]\n```")

        self.assertTrue(harness.run(client, config, self.pr, {"reviewed": {}}, force=True, rereview=True))

        self.assertEqual(harness.outline_calls, 1)
        upserts = [call for call in client.calls if call[0] == "upsert_issue_comment" and call[1] == VISUAL_MARKER]
        self.assertEqual(len(upserts), 1, client.calls)
        self.assertIn("```mermaid", upserts[0][2])
        self.assertIn(("create_review", "COMMENT"), client.calls)
        # The refresh goes through upsert, so the service edits only its own
        # marker comment; it never lists comments to decide whether to run.
        self.assertEqual(client.list_comments_calls, 0)

    def test_a_normal_review_still_skips_an_existing_outline(self) -> None:
        client = _OutlineClient(comments=list(self.existing))
        config = _make_config(self.tmp_path / "work", _visual_block())
        harness = _ReviewHarness(unittest.TestCase())

        self.assertTrue(harness.run(client, config, self.pr, {"reviewed": {}}, force=True))

        self.assertEqual(harness.outline_calls, 0)
        self.assertNotIn(VISUAL_MARKER, client.markers())

    def test_a_rereview_with_the_outline_disabled_posts_no_outline(self) -> None:
        client = _OutlineClient(comments=list(self.existing))
        config = _make_config(self.tmp_path / "work")
        harness = _ReviewHarness(unittest.TestCase())

        self.assertTrue(harness.run(client, config, self.pr, {"reviewed": {}}, force=True, rereview=True))

        self.assertEqual(harness.outline_calls, 0)
        self.assertNotIn(VISUAL_MARKER, client.markers())


if __name__ == "__main__":
    unittest.main()
