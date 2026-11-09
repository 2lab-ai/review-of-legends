"""The ``review-of-legends/required`` commit status and required-verdict failures.

User decision on PR #23: "required" covers both the run (an agent that does
not finish: no review is posted) and the verdict (an agent that finishes but
raises a P0/P1: the review IS posted and the PR head gets a failing status).
The verdict is derived from the findings: "patch is incorrect" iff the agent
raised a P0/P1 (or a priority-less high severity), whatever free text the agent
wrote (a production PR: "patch is incorrect" with only P2 findings failed the
gate). Repositories without a required agent never see a
required status call, and a status call that fails never costs the review.
The ``review-of-legends/reviewed`` status every repository gets is recorded
apart (``_StatusClient.reviewed``) and tested in test_reviewed_status.py.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import dashboard_execution as dx
from code_review_service import service as svc
from tests.shipped_config import load_shipped_config
from tests.test_dashboard_integration import FakeStore
from tests.test_progress_comment import _FakeClient
from tests.test_required_agents import _config

HEAD = "abc123456789"
PR_URL = "https://github.invalid/owner/repo/pull/3"
P1 = {"path": "a.py", "line": 2, "priority": 1, "title": "lost write", "body": "b"}
# A required verdict failure: arch raises a P1 (the finding fails it, not the free text).
ARCH_P1 = {"arch": {"overall_correctness": "patch is correct", "findings": [P1]}}


class _StatusClient(_FakeClient):
    def __init__(self, fail_status: bool = False, fail_review: bool = False) -> None:
        super().__init__(fail_review=fail_review)
        self.statuses: list[tuple[str, str, str, str]] = []
        self.contexts: list[str] = []
        self.reviewed: list[tuple[str, str, str, str]] = []
        self.reviewed_contexts: list[str] = []
        self.review_bodies: list[str] = []
        self.fail_status = fail_status

    def create_commit_status(self, sha, state, description, target_url="", context=svc.REQUIRED_STATUS_CONTEXT):
        if context.startswith(svc.REVIEWED_STATUS_CONTEXT):
            self.reviewed.append((sha, state, description, target_url))
            self.reviewed_contexts.append(context)
        else:
            self.statuses.append((sha, state, description, target_url))
            self.contexts.append(context)
        if self.fail_status:
            raise svc.ServiceError("GitHub API POST statuses failed: 403")
        return {"id": 1}

    def create_review(self, number, commit_id, body, comments, event="COMMENT"):
        self.review_bodies.append(body)
        return super().create_review(number, commit_id, body, comments, event=event)


class RequiredStatusTest(unittest.TestCase):
    def _run(self, results=None, *, required=("arch",), client=None, dry_run=False, public_url=None, store=True,
             clone=None, files=None, state=None, force=False, base=None, approval=False, reuse=None):
        """One review_pr; ``store`` is True (a fresh FakeStore), False (none) or the FakeStore to record in;
        ``reuse`` is the earlier attempt's kept results, as a dashboard retry passes them."""
        results = results or {}
        clone = clone or (lambda repo, n, h, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True))
        client = client or _StatusClient()
        store_obj = None if store is False else FakeStore() if store is True else store
        recorder = dx.DashboardRecorder(store_obj, logger=lambda message: None)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = _config(tmp_path / "work", required=required)
            if public_url:
                config["dashboard"] = {"public_url": public_url}
            if approval:
                # The PR author below ("x") is trusted, so a clean run approves.
                config["approval"] = {"enabled": True, "trusted_authors": ["x"]}
            pr = {"number": 3, "head": {"sha": HEAD}, "base": {"sha": "b" * 12, **({"ref": base} if base else {})},
                  "title": "t", "body": "", "user": {"login": "x"}, "html_url": PR_URL}

            def fake_run_agent(agent, *args, **kwargs):
                return {"agent": agent["id"], "summary": "ok", "findings": [], **results.get(agent["id"], {})}

            if files is not None:
                client.list_files = files
            with mock.patch.object(svc, "run_agent", fake_run_agent), \
                 mock.patch.object(svc, "clone_pr_source", clone), \
                 mock.patch.object(svc, "load_repository_rules", lambda *a, **k: ""):
                posted = svc.review_pr(client, config, tmp_path / "config" / "reviewer.json", pr,
                                       state if state is not None else {"reviewed": {}},
                                       dry_run=dry_run, force=force, recorder=recorder, reuse_results=reuse)
        return posted, client, recorder

    def states(self, client) -> list[tuple[str, str]]:
        return [(state, description) for _, state, description, _ in client.statuses]

    def test_pending_then_success_when_every_required_agent_passes(self) -> None:
        posted, client, _ = self._run({"b": {"overall_correctness": "patch is incorrect", "findings": [P1]}})
        self.assertTrue(posted)
        self.assertEqual(self.states(client), [
            ("pending", "required reviewers running: arch"),
            ("success", "required reviewers passed: arch"),
        ], "a non-required agent's P1 does not fail the status")
        self.assertTrue(all(sha == HEAD for sha, *_ in client.statuses))
        self.assertNotIn("Required review failed", client.review_bodies[-1])

    def test_a_bare_incorrect_verdict_passes_and_is_shown_as_derived(self) -> None:
        """A production PR: "patch is incorrect" with no P0/P1 used to fail the gate."""
        posted, client, _ = self._run({"arch": {"overall_correctness": "patch is incorrect",
                                                "overall_explanation": "the retry may drop a write"}})
        self.assertTrue(posted)
        self.assertEqual(client.review_calls, 1)
        self.assertEqual(self.states(client)[-1], ("success", "required reviewers passed: arch"))
        body = client.review_bodies[-1]
        self.assertNotIn("Required review failed", body)
        self.assertNotIn("Required review failed", client.progress_bodies[-1])
        self.assertIn('- **arch**: patch is correct (no P0/P1 finding; the agent wrote "patch is incorrect"): '
                      "the retry may drop a write", body)

    def test_a_bare_incorrect_verdict_does_not_block_approval(self) -> None:
        """Approval reads the settled verdict: with no P0/P1 anywhere the review approves."""
        posted, client, _ = self._run({"arch": {"overall_correctness": "patch is incorrect"}}, approval=True)
        self.assertTrue(posted)
        self.assertEqual(client.review_events, ["APPROVE"])
        self.assertEqual(self.states(client)[-1], ("success", "required reviewers passed: arch"))
        posted, client, _ = self._run(ARCH_P1, approval=True)
        self.assertTrue(posted)
        self.assertEqual(client.review_events, ["COMMENT"], "a P1 still blocks it")

    def test_an_incorrect_verdict_with_only_a_p2_passes(self) -> None:
        p2 = {**P1, "priority": 2, "title": "slow retry"}
        posted, client, _ = self._run({"arch": {"overall_correctness": "patch is incorrect", "findings": [p2]}})
        self.assertTrue(posted)
        self.assertEqual(self.states(client)[-1], ("success", "required reviewers passed: arch"))
        self.assertNotIn("Required review failed", client.review_bodies[-1])
        self.assertIn('- **arch**: patch is correct (no P0/P1 finding; the agent wrote "patch is incorrect")',
                      client.review_bodies[-1])

    def test_a_priority_less_high_severity_finding_fails_the_status(self) -> None:
        high = {"path": "a.py", "line": 2, "severity": "high", "title": "lost write", "body": "b"}
        posted, client, _ = self._run({"arch": {"findings": [high]}})
        self.assertTrue(posted)
        self.assertEqual(self.states(client)[-1], ("failure", "arch: high-severity finding"))
        body = client.review_bodies[-1]
        self.assertIn("Required review failed: arch reported a high-severity finding", body)
        self.assertIn("- **arch**: patch is incorrect", body, "the derived verdict is shown when the agent wrote none")

    def test_a_p1_finding_from_a_required_agent_fails_the_status(self) -> None:
        posted, client, _ = self._run(ARCH_P1)
        self.assertTrue(posted)
        self.assertEqual(client.review_calls, 1)
        self.assertEqual(self.states(client)[-1], ("failure", "arch: P1 finding"))
        self.assertIn("**Required review failed: arch reported a P1 finding**", client.review_bodies[-1])
        self.assertIn("Required review failed: arch reported a P1 finding.", client.progress_bodies[-1])
        self.assertIn("**Phase:** Complete", client.progress_bodies[-1])

    def test_a_p1_under_a_correct_verdict_is_shown_as_incorrect(self) -> None:
        posted, client, _ = self._run({"arch": {"overall_correctness": "patch is correct", "findings": [P1],
                                                "overall_explanation": "looks fine"}})
        self.assertTrue(posted)
        self.assertEqual(self.states(client)[-1], ("failure", "arch: P1 finding"))
        self.assertIn('- **arch**: patch is incorrect (a P0/P1 finding; the agent wrote "patch is correct"): looks fine',
                      client.review_bodies[-1])

    def test_a_verdict_that_agrees_with_the_findings_is_shown_as_written(self) -> None:
        posted, client, _ = self._run({"arch": {"overall_correctness": "patch is incorrect", "findings": [P1],
                                                "overall_explanation": "loses a write"}})
        self.assertTrue(posted)
        body = client.review_bodies[-1]
        self.assertIn("- **arch**: patch is incorrect: loses a write", body)
        self.assertNotIn("the agent wrote", body)

    def _body_results(self, build) -> dict:
        """The results review_pr handed build_review_body (its third argument), by agent."""
        return {str(result.get("agent")): result for result in build.call_args.args[2]}

    def test_each_finished_result_carries_the_derived_verdict(self) -> None:
        with mock.patch.object(svc, "build_review_body", wraps=svc.build_review_body) as build:
            posted, _, _ = self._run({
                "a": {"error": "runner exited 1"},
                "b": {"overall_correctness": "patch is correct", "findings": [P1]},
                "arch": {"overall_correctness": "patch is incorrect"},
            }, required=())
        self.assertTrue(posted)
        results = self._body_results(build)
        self.assertEqual(results["arch"]["overall_correctness"], "patch is correct")
        self.assertEqual(results["arch"]["agent_overall_correctness"], "patch is incorrect")
        self.assertEqual(results["b"]["overall_correctness"], "patch is incorrect")
        self.assertEqual(results["b"]["agent_overall_correctness"], "patch is correct")
        self.assertNotIn("overall_correctness", results["a"], "a failed result gets no verdict")

    def test_an_unchanged_or_missing_verdict_keeps_no_original(self) -> None:
        with mock.patch.object(svc, "build_review_body", wraps=svc.build_review_body) as build:
            self._run({"b": {"overall_correctness": "patch is correct"}, "arch": {}}, required=())
        results = self._body_results(build)
        for agent_id in ("b", "arch"):
            with self.subTest(agent=agent_id):
                self.assertEqual(results[agent_id]["overall_correctness"], "patch is correct")
                self.assertNotIn("agent_overall_correctness", results[agent_id])

    def test_a_reused_result_gets_the_derived_verdict(self) -> None:
        """A retry reuses what a not-posted attempt kept, as the agent wrote it; the verdict is settled then."""
        p2 = {**P1, "priority": 2, "title": "slow retry"}
        store = FakeStore()
        posted, _, _ = self._run({"arch": {"overall_correctness": "patch is incorrect", "findings": [p2]},
                                  "b": {"error": "runner exited 1"}}, required=("arch", "b"), store=store)
        self.assertFalse(posted, "required agent b failed")
        failed = next(iter(store.runs.values()))
        kept = {item["agent"]: item for item in failed["agent_results"]}
        self.assertEqual(kept["arch"]["overall_correctness"], "patch is incorrect", "kept as the agent wrote it")
        posted, client, _ = self._run(required=("arch", "b"),
                                      reuse={"run_id": failed["id"], "head_sha": HEAD, "results": kept})
        self.assertTrue(posted)
        self.assertIn("Reused from the previous attempt (not run again): a, arch.", client.progress_bodies[-1])
        self.assertIn('- **arch**: patch is correct (no P0/P1 finding; the agent wrote "patch is incorrect")',
                      client.review_bodies[-1])
        self.assertNotIn("Required review failed", client.review_bodies[-1])
        self.assertEqual(self.states(client)[-1], ("success", "required reviewers passed: b, arch"))

    def test_error_when_the_review_is_not_posted(self) -> None:
        _, client, _ = self._run({"arch": {"error": "timeout", "summary": "agent timed out after 1800s"}})
        self.assertEqual(self.states(client), [
            ("pending", "required reviewers running: arch"),
            ("error", "arch did not complete; retry from the dashboard"),
        ])
        _, client, _ = self._run({"a": {"error": "x"}, "b": {"error": "y"}})
        self.assertEqual(self.states(client)[-1],
                         ("error", "review not posted: only 1/3 agents succeeded; retry from the dashboard"))

    def test_error_when_the_review_raises_after_dispatch(self) -> None:
        client = _StatusClient(fail_review=True)
        with self.assertRaises(svc.ServiceError):
            self._run(client=client)
        self.assertEqual(self.states(client)[-1], ("error", "review did not finish; check the dashboard"))

    def test_a_failure_before_dispatch_ends_pending_as_error(self) -> None:
        """PR #23 R2: pending used to go up at dispatch, so a fetch or clone failure
        left no status at all (or a stale earlier one)."""

        def clone_times_out(repo, n, h, run_dir, **kw):
            raise svc.ServiceError("clone timed out after 900s")

        def files_fail(number):
            raise svc.ServiceError("GitHub API GET files failed: 502")

        for label, kwargs in (("clone", {"clone": clone_times_out}), ("fetch", {"files": files_fail})):
            with self.subTest(step=label):
                client = _StatusClient()
                with self.assertRaises(svc.ServiceError):
                    self._run(client=client, **kwargs)
                self.assertEqual(self.states(client), [
                    ("pending", "required reviewers running: arch"),
                    ("error", "review did not finish; check the dashboard"),
                ])
                self.assertEqual(client.review_calls, 0)

    def test_a_rerun_that_fails_early_replaces_an_earlier_success(self) -> None:
        client = _StatusClient()
        posted, _, _ = self._run(client=client)
        self.assertTrue(posted)
        self.assertEqual(self.states(client)[-1][0], "success")

        def clone_fails(repo, n, h, run_dir, **kw):
            raise svc.ServiceError("clone failed")

        with self.assertRaises(svc.ServiceError):
            self._run(client=client, clone=clone_fails, force=True)  # same head, e.g. `once --force`
        self.assertEqual([state for state, _ in self.states(client)], ["pending", "success", "pending", "error"])

    def test_skips_make_no_status_calls(self) -> None:
        reviewed = {"reviewed": {"3": {"head_sha": HEAD}}}
        for label, kwargs in (
            ("already reviewed", {"state": reviewed}),
            ("dry run", {"dry_run": True}),
            ("no required agent", {"required": ()}),
        ):
            with self.subTest(skip=label):
                _, client, _ = self._run(ARCH_P1, **kwargs)
                self.assertEqual(client.statuses, [])

    def test_prs_sharing_a_head_with_different_bases_get_distinct_contexts(self) -> None:
        """GitHub keys a status by (repo, sha, context): one context would let a pass on
        one base overwrite a failure on the other base's larger diff."""
        client = _StatusClient()
        self._run(ARCH_P1, client=client, base="release/2026.10")
        self._run(client=client, base="main")
        by_context: dict = {}
        for context, (_, state, _, _) in zip(client.contexts, client.statuses):
            by_context[context] = state
        self.assertEqual(by_context, {
            "review-of-legends/required/release/2026.10": "failure",
            "review-of-legends/required/main": "success",
        })

    def test_no_status_calls_without_a_required_agent(self) -> None:
        posted, client, _ = self._run(ARCH_P1, required=())
        self.assertTrue(posted)
        self.assertEqual(client.statuses, [])
        self.assertNotIn("Required review failed", client.review_bodies[-1])

    def test_a_failing_status_call_never_costs_the_review(self) -> None:
        posted, client, _ = self._run(ARCH_P1, client=_StatusClient(fail_status=True))
        self.assertTrue(posted)
        self.assertEqual(client.review_calls, 1)
        self.assertEqual([state for _, state, _, _ in client.statuses], ["pending", "failure"])
        self.assertIn("commit status (pending) could not be set", client.progress_bodies[-1])
        self.assertIn("commit status (failure) could not be set", client.progress_bodies[-1])

    def test_the_status_links_the_dashboard_attempt_or_else_the_pr(self) -> None:
        _, client, recorder = self._run(public_url="https://rol.example")
        self.assertTrue(recorder.run_id)
        self.assertEqual({url for *_, url in client.statuses}, {f"https://rol.example/#run/{recorder.run_id}"})
        _, client, _ = self._run()
        self.assertEqual({url for *_, url in client.statuses}, {PR_URL})
        _, client, _ = self._run(public_url="https://rol.example", store=False)
        self.assertEqual({url for *_, url in client.statuses}, {PR_URL}, "no recorded attempt, no attempt page")

    def test_a_dashboard_override_decides_whether_statuses_are_posted(self) -> None:
        client = _StatusClient()
        store = FakeStore()
        store.settings["agent:a"] = {"required": True}
        recorder = dx.DashboardRecorder(store, logger=lambda message: None)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pr = {"number": 3, "head": {"sha": HEAD}, "base": {"sha": "b" * 12}, "title": "t", "body": "", "html_url": PR_URL}
            with mock.patch.object(svc, "run_agent", lambda agent, *a, **k: {"agent": agent["id"], "summary": "ok", "findings": []}), \
                 mock.patch.object(svc, "clone_pr_source", lambda repo, n, h, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True)), \
                 mock.patch.object(svc, "load_repository_rules", lambda *a, **k: ""):
                svc.review_pr(client, _config(tmp_path / "work", required=()), tmp_path / "config" / "reviewer.json", pr,
                              {"reviewed": {}}, recorder=recorder)
        self.assertEqual(self.states(client)[-1], ("success", "required reviewers passed: a"))


class StatusHelpersTest(unittest.TestCase):
    def test_the_context_names_a_sanitized_base_branch(self) -> None:
        context = svc.required_status_context
        self.assertEqual(context({"base": {"ref": "main"}}), "review-of-legends/required/main")
        # A changed name ends in "~" and 32 hex characters of the raw name's SHA-256.
        self.assertEqual(context({"base": {"ref": "feat/x y*z"}}), "review-of-legends/required/feat/x-y-z~e6520f2c2b88fe9628be005cc0b3b97e")
        self.assertEqual(context({"base": {"ref": "b" * 300}}), "review-of-legends/required/" + "b" * 67 + "~dccc1450d6fc4232955fcc5cf81105d8")
        self.assertEqual(context({"base": {}}), "review-of-legends/required")
        self.assertEqual(context({}), "review-of-legends/required")

    def test_names_left_unchanged_keep_their_context(self) -> None:
        # Existing rulesets require these exact contexts.
        context = svc.required_status_context
        for ref in ("main", "develop", "deploy/dev2", "release/1.2", "b" * svc.MAX_STATUS_BRANCH_CHARS):
            with self.subTest(ref=ref):
                self.assertEqual(context({"base": {"ref": ref}}), "review-of-legends/required/" + ref)

    def test_distinct_bases_get_distinct_contexts(self) -> None:
        # One context per base exists so a second review of a shared head cannot
        # overwrite the first's verdict; colliding suffixes would undo that.
        context = svc.required_status_context
        prefix = "b" * svc.MAX_STATUS_BRANCH_CHARS
        for first, second in (("rel+1", "rel-1"), ("rel@x", "rel-x"), (prefix + "1", prefix + "2")):
            with self.subTest(first=first, second=second):
                self.assertNotEqual(context({"base": {"ref": first}}), context({"base": {"ref": second}}))

    def test_a_searched_short_digest_collision_stays_apart(self) -> None:
        # Two 101-char names that differ inside their first 100 characters. An
        # 8-hex (32-bit) suffix collides for this pair (found by hashing ~2^18 names,
        # PR #56 review); whoever can create branches could do the same.
        first, second = "a" * 91 + "000084940x", "a" * 91 + "000229303x"
        for name in ("required_status_context", "reviewed_status_context"):
            with self.subTest(name=name):
                function = getattr(svc, name)
                self.assertNotEqual(function({"base": {"ref": first}}), function({"base": {"ref": second}}))

    def test_a_changed_name_never_equals_a_branch_left_as_is(self) -> None:
        # "rel-1-<digest>" is a legal branch name used as is; "rel+1" must not
        # become it. Git forbids "~" in branch names, so the digest goes after one.
        literal_ref = "rel-1-c40776d5bc6cb59fe0d432ad5e832c96"
        generated, literal = {"base": {"ref": "rel+1"}, "head": {"sha": "abc"}}, {"base": {"ref": literal_ref}, "head": {"sha": "abc"}}
        for name in ("required_status_context", "reviewed_status_context", "reviewed_status_key"):
            with self.subTest(name=name):
                function = getattr(svc, name)
                self.assertNotEqual(function(generated), function(literal))

    def test_the_base_suffix_fits_the_branch_limit(self) -> None:
        for ref in ("rel+1", "b" * 300, "x+" * 80, "/" + "b" * 120, "기능/x"):
            with self.subTest(ref=ref[:20]):
                self.assertLessEqual(len(svc.status_base({"base": {"ref": ref}})), svc.MAX_STATUS_BRANCH_CHARS)

    def test_descriptions_fit_githubs_limit(self) -> None:
        text = svc.status_description("x " * 200)
        self.assertEqual(len(text), svc.MAX_STATUS_DESCRIPTION)
        self.assertTrue(text.endswith("…"))

    def test_public_config_links_the_pr_without_a_dashboard(self) -> None:
        config = load_shipped_config()
        self.assertEqual(svc.required_status_target(config, "run1", {"html_url": PR_URL}),
                         PR_URL)

    def test_load_config_rejects_a_non_https_public_url(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviewer.json"
            path.write_text(json.dumps({
                "repositories": ["o/r"],
                "agents": [{"id": i, "prompt_path": "x"} for i in ("a", "b", "c")],
                "dashboard": {"public_url": "http://rol.example"},
            }), encoding="utf-8")
            with self.assertRaisesRegex(svc.ServiceError, "public_url"):
                svc.load_config(path)


if __name__ == "__main__":
    unittest.main()
