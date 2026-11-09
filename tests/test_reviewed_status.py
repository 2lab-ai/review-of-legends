"""The ``review-of-legends/reviewed/<base>`` commit status as review_pr sets it.

User rule (2026-10-14): every real project gets the review bot, and a PR can
merge only after the bot has reviewed it at least once; backend servers also
need server-architect to accept (``review-of-legends/required/<base>``). A
ruleset requires the reviewed context on every repository, so:

- it is success once a review has been posted on the PR (any head), and it is
  only ever success: a run that posts nothing sets nothing (PR #32 round 1);
- a review POST whose outcome is unknown counts as posted (the ledger already
  counts that head as reviewed, and no retry may post it again);
- the context names the base, because GitHub keys a status by commit and
  context and two PRs can share a head commit;
- the ``<base>@<head>`` it was set on is kept in the ledger's own
  ``reviewed_status`` map, which review_pr's rewrite of ``reviewed[...]``
  never touches;
- never in a dry run, never when ``reviewed_status`` is false.

The scheduler's half (every new head of a reviewed PR, the backfill, the
backoff) is in test_scheduler.ReviewedStatusWatchTest.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import dashboard_execution as dx
from code_review_service import service as svc
from tests.shipped_config import load_shipped_config
from tests.test_dashboard_integration import FakeStore
from tests.test_required_agents import _config
from tests.test_required_status import HEAD, PR_URL, _StatusClient

OLD = "0ld0ld0ld0ld0ld"
CONFIG_PATH = ROOT / "config" / "reviewer.json"
MAIN = "review-of-legends/reviewed/main"
QUORUM_FAILS = {"a": {"error": "x"}, "b": {"error": "y"}}


class _OrderedClient(_StatusClient):
    """Records every status and the review POST in one ordered list.

    ``fail_review`` raises an error of unknown outcome (no HTTP status: the
    POST may have landed, so the head is parked unverified); ``reject_review``
    raises a 422, a review GitHub certainly did not create.
    """

    def __init__(self, fail_reviewed: bool = False, reject_review: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fail_reviewed = fail_reviewed
        self.reject_review = reject_review
        self.events: List[str] = []

    def create_commit_status(self, sha, state, description, target_url="", context=svc.REQUIRED_STATUS_CONTEXT):
        reviewed = context.startswith(svc.REVIEWED_STATUS_CONTEXT)
        self.events.append("%s:%s" % ("reviewed" if reviewed else "required", state))
        if self.fail_reviewed and reviewed:
            self.reviewed.append((sha, state, description, target_url))
            self.reviewed_contexts.append(context)
            raise svc.ServiceError("GitHub API POST statuses failed: 403")
        return super().create_commit_status(sha, state, description, target_url, context=context)

    def create_review(self, number, commit_id, body, comments, event="COMMENT"):
        self.events.append("review")
        if self.reject_review:
            self.review_calls += 1
            error = svc.ServiceError("GitHub API POST reviews failed: 422 Unprocessable Entity")
            error.status_code = 422  # type: ignore[attr-defined]
            raise error
        return super().create_review(number, commit_id, body, comments, event=event)

    def latest(self, sha: str, context: str) -> Optional[str]:
        """What GitHub shows for (sha, context): the last state set there, or None."""
        states = [
            state for (head, state, _, _), name in zip(self.reviewed, self.reviewed_contexts)
            if head == sha and name == context
        ]
        return states[-1] if states else None


def _clone_fails(repo, n, h, run_dir, **kw):
    raise svc.ServiceError("clone timed out after 900s")


class ReviewedStatusTest(unittest.TestCase):
    def _run(
        self,
        results: Optional[Dict[str, Any]] = None,
        *,
        bucket: Optional[Dict[str, Any]] = None,
        client: Optional[_OrderedClient] = None,
        required: tuple = (),
        dry_run: bool = False,
        force: bool = False,
        clone: Any = None,
        save: bool = False,
        number: int = 3,
        head: str = HEAD,
        base: str = "main",
        **config_extra: Any,
    ):
        results = results or {}
        clone = clone or (lambda repo, n, h, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True))
        client = client or _OrderedClient()
        bucket = bucket if bucket is not None else {"reviewed": {}}
        recorder = dx.DashboardRecorder(FakeStore(), logger=lambda message: None)
        saved = None
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = {**_config(tmp_path / "work", required=required), **config_extra}
            pr = {"number": number, "head": {"sha": head}, "base": {"sha": "b" * 12, "ref": base},
                  "title": "t", "body": "", "user": {"login": "x"}, "html_url": PR_URL}
            full_state = {"repositories": {"owner/repo": bucket}}
            state_path = tmp_path / "state.json" if save else None

            def fake_run_agent(agent, *args, **kwargs):
                return {"agent": agent["id"], "summary": "ok", "findings": [], **results.get(agent["id"], {})}

            with mock.patch.object(svc, "run_agent", fake_run_agent), \
                 mock.patch.object(svc, "clone_pr_source", clone), \
                 mock.patch.object(svc, "load_repository_rules", lambda *a, **k: ""), \
                 mock.patch.object(svc, "log"):
                posted = svc.review_pr(client, config, tmp_path / "config" / "reviewer.json", pr, bucket,
                                       dry_run=dry_run, force=force, recorder=recorder,
                                       state_path=state_path, full_state=full_state)
            if state_path is not None and state_path.exists():
                saved = json.loads(state_path.read_text(encoding="utf-8"))
        return posted, client, bucket, saved

    def reviewed(self, client: _OrderedClient) -> List[tuple]:
        return [(state, description) for _, state, description, _ in client.reviewed]

    # -------------------------------------------------------- success, only

    def test_a_posted_review_sets_success_and_nothing_before_it(self) -> None:
        posted, client, bucket, saved = self._run(save=True)
        self.assertTrue(posted)
        self.assertEqual(self.reviewed(client), [("success", "reviewed (%s)" % HEAD[:12])])
        self.assertEqual(client.events, ["review", "reviewed:success"], "no pending while the first review runs")
        self.assertEqual(client.reviewed_contexts, [MAIN])
        self.assertEqual({sha for sha, *_ in client.reviewed}, {HEAD})
        self.assertEqual({url for *_, url in client.reviewed}, {PR_URL})
        self.assertEqual(bucket["reviewed_status"], {"3": "main@" + HEAD})
        self.assertEqual(saved["repositories"]["owner/repo"]["reviewed_status"], {"3": "main@" + HEAD},
                         "saved under the lock")

    def test_a_run_that_posts_nothing_sets_nothing(self) -> None:
        """No pending, no error: a PR without a posted review simply has no status."""
        failures: List[tuple] = [
            ("quorum", lambda: (_OrderedClient(), {"results": QUORUM_FAILS})),
            ("required agent", lambda: (_OrderedClient(), {"results": {"arch": {"error": "timeout"}}, "required": ("arch",)})),
            ("clone", lambda: (_OrderedClient(), {"clone": _clone_fails})),
            ("review POST rejected", lambda: (_OrderedClient(reject_review=True), {})),
        ]
        buckets: List[tuple] = [
            ("first", lambda: {"reviewed": {}}),
            ("baselined", lambda: {"reviewed": {"3": {"head_sha": OLD, "initial_baseline_without_review": True}}}),
            ("update", lambda: {"reviewed": {"3": {"head_sha": OLD}}, "reviewed_status": {"3": "main@" + OLD}}),
        ]
        for review, make_bucket in buckets:
            for label, make_case in failures:
                with self.subTest(review=review, failure=label):
                    client, kwargs = make_case()
                    bucket = make_bucket()
                    before = dict(bucket.get("reviewed_status") or {})
                    try:
                        self._run(kwargs.pop("results", None), bucket=bucket, client=client, **kwargs)
                    except svc.ServiceError:
                        pass
                    self.assertEqual(client.reviewed, [])
                    self.assertEqual(dict(bucket.get("reviewed_status") or {}), before)

    def test_a_failed_first_run_then_a_posted_review_on_a_new_head_gives_success(self) -> None:
        client = _OrderedClient()
        bucket: Dict[str, Any] = {"reviewed": {}}
        self._run(QUORUM_FAILS, bucket=bucket, client=client, head=OLD)
        self.assertEqual(client.reviewed, [])
        self.assertIn("3", bucket["failed"])
        self._run(bucket=bucket, client=client, head=HEAD)
        self.assertEqual(client.latest(HEAD, MAIN), "success")
        self.assertIsNone(client.latest(OLD, MAIN))
        self.assertEqual(bucket["reviewed_status"], {"3": "main@" + HEAD})

    def test_an_unconfirmed_review_post_sets_success(self) -> None:
        """All agents ran and the POST most likely landed; the ledger counts the head as
        reviewed and no retry may post it again."""
        unconfirmed = ("success", "reviewed (%s); the posted review could not be confirmed" % HEAD[:12])
        for label, bucket in (
            ("first review", {"reviewed": {}}),
            ("update review", {"reviewed": {"3": {"head_sha": OLD}}, "reviewed_status": {"3": "main@" + OLD}}),
        ):
            with self.subTest(review=label):
                client = _OrderedClient(fail_review=True)
                with self.assertRaises(svc.ServiceError):
                    self._run(bucket=bucket, client=client, required=("arch",))
                self.assertEqual(self.reviewed(client), [unconfirmed])
                self.assertTrue(bucket["reviewed"]["3"]["post_outcome_unknown"])
                self.assertEqual(bucket["reviewed_status"], {"3": "main@" + HEAD}, "recorded like a posted review")
                # The required status is unchanged: it ends in error.
                self.assertEqual([state for _, state, _, _ in client.statuses], ["pending", "error"])

    def test_an_unconfirmed_review_post_is_saved_with_the_memo(self) -> None:
        bucket: Dict[str, Any] = {"reviewed": {}}
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "state.json"
            pr = {"number": 3, "head": {"sha": HEAD}, "base": {"sha": "b" * 12, "ref": "main"},
                  "title": "t", "body": "", "user": {"login": "x"}, "html_url": PR_URL}
            with mock.patch.object(svc, "run_agent", lambda agent, *a, **k: {"agent": agent["id"], "summary": "ok", "findings": []}), \
                 mock.patch.object(svc, "clone_pr_source", lambda repo, n, h, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True)), \
                 mock.patch.object(svc, "load_repository_rules", lambda *a, **k: ""), \
                 mock.patch.object(svc, "log"):
                with self.assertRaises(svc.ServiceError):
                    svc.review_pr(_OrderedClient(fail_review=True), _config(tmp_path / "work", required=()),
                                  tmp_path / "config" / "reviewer.json", pr, bucket,
                                  recorder=dx.DashboardRecorder(FakeStore(), logger=lambda message: None),
                                  state_path=state_path, full_state={"repositories": {"owner/repo": bucket}})
            saved = json.loads(state_path.read_text(encoding="utf-8"))["repositories"]["owner/repo"]
        self.assertEqual(saved["reviewed_status"], {"3": "main@" + HEAD})
        self.assertTrue(saved["reviewed"]["3"]["post_outcome_unknown"])

    # ------------------------------------------------------ shared head commits

    def test_a_shared_head_on_another_base_inherits_nothing(self) -> None:
        """PR #32 round 1: one context per commit let reviewed PR A's success cover PR B."""
        client = _OrderedClient()
        self._run(client=client, number=3, base="main")
        self._run(QUORUM_FAILS, client=client, number=4, base="develop")
        self.assertEqual(client.latest(HEAD, MAIN), "success")
        self.assertIsNone(client.latest(HEAD, "review-of-legends/reviewed/develop"))
        # B's own posted review gives B its own context.
        self._run(client=client, number=4, base="develop")
        self.assertEqual(client.latest(HEAD, "review-of-legends/reviewed/develop"), "success")

    def test_a_shared_head_on_the_same_base_failing_first_review_leaves_the_success(self) -> None:
        """B's failed first review must not turn A's success into pending or error.

        Accepted (PR #32 round 1): B, never reviewed, shares A's head and base,
        so GitHub shows it A's success. Merging B gives exactly the result A
        may already merge.
        """
        client = _OrderedClient()
        bucket: Dict[str, Any] = {"reviewed": {}}
        self._run(bucket=bucket, client=client, number=3)
        self._run(QUORUM_FAILS, bucket=bucket, client=client, number=4)
        with self.assertRaises(svc.ServiceError):
            self._run(bucket=bucket, client=client, number=4, clone=_clone_fails)
        self.assertEqual([state for _, state, _, _ in client.reviewed], ["success"])
        self.assertEqual(client.latest(HEAD, MAIN), "success", "B, never reviewed, shows A's success")
        self.assertFalse(svc.ever_reviewed(bucket, 4))

    # ------------------------------------------------------------- skips

    def test_a_dry_run_sets_no_reviewed_status(self) -> None:
        posted, client, bucket, _ = self._run(dry_run=True)
        self.assertTrue(posted)
        self.assertEqual(client.reviewed, [])
        self.assertNotIn("reviewed_status", bucket)

    def test_an_already_reviewed_head_sets_nothing(self) -> None:
        posted, client, _, _ = self._run(bucket={"reviewed": {"3": {"head_sha": HEAD}}})
        self.assertFalse(posted)
        self.assertEqual(client.reviewed, [])

    def test_an_update_review_sets_success_once_posted(self) -> None:
        bucket = {"reviewed": {"3": {"head_sha": OLD}}, "reviewed_status": {"3": "main@" + OLD}}
        _, client, bucket, _ = self._run(bucket=bucket)
        self.assertEqual(client.events, ["review", "reviewed:success"])
        self.assertEqual(bucket["reviewed_status"], {"3": "main@" + HEAD})

    def test_a_forced_rerun_sets_success_only_when_it_posts(self) -> None:
        _, client, _, _ = self._run(bucket={"reviewed": {"3": {"head_sha": HEAD}}}, force=True)
        self.assertEqual([state for state, _ in self.reviewed(client)], ["success"])
        client = _OrderedClient()
        with self.assertRaises(svc.ServiceError):
            self._run(bucket={"reviewed": {"3": {"head_sha": HEAD}}}, client=client, force=True, clone=_clone_fails)
        self.assertEqual(client.reviewed, [])

    # ---------------------------------------------------------------- the memo

    def test_the_memo_survives_review_pr_rewriting_the_ledger_entry(self) -> None:
        bucket = {
            "reviewed": {"3": {"head_sha": OLD, "line_comments": 2}, "9": {"head_sha": "nine"}},
            "reviewed_status": {"3": "main@" + OLD, "9": "main@nine"},
        }
        _, _, bucket, saved = self._run(bucket=bucket, save=True)
        self.assertEqual(bucket["reviewed"]["3"]["head_sha"], HEAD)
        self.assertNotIn("reviewed_status", bucket["reviewed"]["3"])
        self.assertEqual(bucket["reviewed_status"], {"3": "main@" + HEAD, "9": "main@nine"})
        self.assertEqual(saved["repositories"]["owner/repo"]["reviewed_status"],
                         {"3": "main@" + HEAD, "9": "main@nine"})

    def test_a_failing_status_post_never_costs_the_review_and_is_not_recorded(self) -> None:
        posted, client, bucket, _ = self._run(client=_OrderedClient(fail_reviewed=True))
        self.assertTrue(posted)
        self.assertEqual(client.review_calls, 1)
        self.assertEqual([state for _, state, _, _ in client.reviewed], ["success"])
        self.assertNotIn("reviewed_status", bucket, "the scheduler's next watch sets it instead")
        self.assertIn("review-of-legends/reviewed/main commit status (success) could not be set",
                      client.progress_bodies[-1])
        self.assertIn("**Phase:** Complete", client.progress_bodies[-1])

    # -------------------------------------------------------- on, off, config

    def test_both_contexts_on_a_repository_with_a_required_agent(self) -> None:
        _, client, _, _ = self._run(required=("arch",))
        self.assertEqual(client.events, ["required:pending", "review", "required:success", "reviewed:success"])
        self.assertEqual(client.contexts, ["review-of-legends/required/main"] * 2)
        self.assertEqual(client.reviewed_contexts, [MAIN])

    def test_reviewed_status_false_turns_it_off(self) -> None:
        _, client, bucket, _ = self._run(required=("arch",), reviewed_status=False)
        self.assertEqual(client.reviewed, [])
        self.assertNotIn("reviewed_status", bucket)
        self.assertEqual([state for _, state, _, _ in client.statuses], ["pending", "success"], "required unchanged")
        client = _OrderedClient(fail_review=True)
        with self.assertRaises(svc.ServiceError):
            self._run(client=client, reviewed_status=False)
        self.assertEqual(client.reviewed, [], "not for an unconfirmed POST either")

    def test_the_context_and_key_name_the_sanitized_base(self) -> None:
        context, key = svc.reviewed_status_context, svc.reviewed_status_key
        self.assertEqual(context({"base": {"ref": "develop"}}), "review-of-legends/reviewed/develop")
        self.assertEqual(context({"base": {"ref": "feat/x y*z"}}), "review-of-legends/reviewed/feat/x-y-z~e6520f2c2b88fe9628be005cc0b3b97e")
        self.assertEqual(context({"base": {"ref": "b" * 300}}), "review-of-legends/reviewed/" + "b" * 67 + "~dccc1450d6fc4232955fcc5cf81105d8")
        self.assertEqual(context({}), "review-of-legends/reviewed")
        self.assertEqual(key({"base": {"ref": "feat/x y"}, "head": {"sha": "abc"}}), "feat/x-y~106dd400d9fac8e1e5b912a2e3c8d94c@abc")
        self.assertNotEqual(key({"base": {"ref": "rel+1"}, "head": {"sha": "abc"}}),
                            key({"base": {"ref": "rel-1"}, "head": {"sha": "abc"}}))
        # The same suffix as the required context, so both rulesets name the same branch.
        pr = {"base": {"ref": "release/2026.10"}}
        self.assertEqual(context(pr).split("/reviewed/", 1)[1], svc.required_status_context(pr).split("/required/", 1)[1])

    def test_the_config_key_is_a_boolean_defaulting_on(self) -> None:
        self.assertTrue(svc.reviewed_status_enabled({}))
        self.assertTrue(svc.reviewed_status_enabled({"reviewed_status": True}))
        self.assertFalse(svc.reviewed_status_enabled({"reviewed_status": False}))
        for value in ("false", 0, None):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "reviewer.json"
                path.write_text(json.dumps({
                    "repositories": ["o/r"],
                    "agents": [{"id": i, "prompt_path": "x"} for i in ("a", "b", "c")],
                    "reviewed_status": value,
                }), encoding="utf-8")
                with self.assertRaises(svc.ServiceError) as caught:
                    svc.load_config(path)
                self.assertIn("config.reviewed_status must be true or false", str(caught.exception))

    def test_the_shipped_config_sets_it_on_every_repository(self) -> None:
        config = load_shipped_config()
        self.assertIs(config["reviewed_status"], True)
        self.assertIn("_reviewed_status_comment", config)
        self.assertEqual(svc.REVIEWED_STATUS_CONTEXT, "review-of-legends/reviewed")


class OnceTest(unittest.TestCase):
    """``once`` gets review_pr's statuses only; the watcher's backfill is the daemon's."""

    def test_once_sets_no_status_on_a_reviewed_pr_it_does_not_review(self) -> None:
        statuses: List[tuple] = []
        prs = [{"number": 3, "state": "open", "draft": False, "head": {"sha": HEAD},
                "base": {"sha": "b" * 12, "ref": "main"}, "title": "t", "body": "", "html_url": PR_URL}]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "state.json"
            # Reviewed at its listed head before the status existed: no memo.
            state_path.write_text(json.dumps({"repositories": {"owner/repo": {
                "reviewed": {"3": {"head_sha": HEAD}}, "baseline_initialized": True,
            }}}), encoding="utf-8")
            config = {
                "repositories": ["owner/repo"],
                "agents": [{"id": i, "prompt_path": "x"} for i in ("a", "b", "c")],
                "agent_runner": {"command": ["true"]},
                "work_dir": str(tmp_path / "work"),
                "state_path": str(state_path),
            }

            class Client:
                def __init__(self, repo, gh_bin="gh"):
                    self.repo = repo

                def list_open_prs(self):
                    return [dict(item) for item in prs]

                def create_commit_status(self, *args, **kwargs):
                    statuses.append((args, kwargs))

            with mock.patch.object(svc, "GitHubClient", Client), \
                    mock.patch.object(svc, "load_config", return_value=config), \
                    mock.patch.object(svc, "expand_path", side_effect=lambda v, base=None: Path(str(v))), \
                    mock.patch.object(dx, "open_store", return_value=None), \
                    mock.patch.object(svc, "log"):
                rc = svc.run_once(argparse.Namespace(
                    config=str(CONFIG_PATH), repo=None, pr=None, dry_run=False, force=False,
                ))
            saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertEqual(statuses, [])
        self.assertNotIn("reviewed_status", saved["repositories"]["owner/repo"])


if __name__ == "__main__":
    unittest.main()
