"""The contributor gate at work (gate.py) and in the scheduler.

A fake GitHub client answers the gate's reads and records its writes; the store
is the real SQLite one. Scheduler tests reuse test_scheduler's harness with a
watch client that also answers the gate's calls.
"""

from __future__ import annotations

import argparse
import contextlib
import io
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

from code_review_service import contributors as c
from code_review_service import dashboard_execution as dx
from code_review_service import gate as gate_mod
from code_review_service import mentions
from code_review_service import service as svc
from code_review_service.dashboard_store import DashboardStore
from tests.shipped_config import load_shipped_config
from tests.test_scheduler import REPO, GitHub, SchedulerTestCase, pr

BOT = "review-of-legends[bot]"
GATE = {
    "contributor_gate": {"enabled": True, "trusted_email_domains": ["company.example"]},
    "mentions": {"default_reply_language": "en"},
    "dashboard": {"enabled": True, "public_url": "https://reviews.example.com"},
}


def status_error(code: int) -> svc.ServiceError:
    error = svc.ServiceError("GitHub API failed: %d" % code)
    error.status_code = code  # type: ignore[attr-defined]
    return error


class FakeGitHub:
    """What the gate reads and writes on one repository."""

    def __init__(self, repo: str = REPO) -> None:
        self.repo = repo
        self.files: Dict[tuple, tuple] = {}  # (path, ref) -> (text, sha)
        self.permissions: Dict[str, str] = {}
        self.permission_reads: List[str] = []
        self.comments: Dict[int, List[Dict[str, Any]]] = {}
        self.writes: List[tuple] = []
        self.runs: Dict[str, List[int]] = {}
        self.runs_error: Optional[Exception] = None
        self.branch_error: Optional[Exception] = None
        self.approve_errors: Dict[int, Exception] = {}
        self.prs: Dict[int, Dict[str, Any]] = {}
        self.pulls: Dict[str, Dict[str, Any]] = {}
        self.commits: Dict[str, tuple] = {}  # sha -> (parent, path, text)
        self.main_sha = "f" * 40  # the default branch's tip; a test moves it when main changes
        self.head_pulls: List[Dict[str, Any]] = []  # pulls_for_head: every PR from the fork branch, closed too
        self.head_pulls_complete = True
        self.run_state: Dict[int, Dict[str, Any]] = {}
        self._next_id = 100

    # reads
    def bot_login(self) -> str:
        return BOT

    def get_file(self, path: str, ref: str):
        # main's tip reads main; any other sha (an old tip) reads nothing.
        return self.files.get((path, "main" if ref == self.main_sha else ref))

    def collaborator_permission(self, login: str):
        self.permission_reads.append(login)
        return self.permissions.get(login.lower())

    def list_issue_comments(self, number: int):
        return list(self.comments.get(number, []))

    def get_pr(self, number: int):
        return self.prs[number]

    def list_waiting_runs(self, head_sha: str):
        if self.runs_error is not None:
            raise self.runs_error
        return [
            {"id": run_id, "head_repository": {"full_name": source}, "head_branch": branch}
            for run_id, source, branch in self.runs.get(head_sha, [])
        ]

    def open_pull_for_branch(self, branch: str):
        return self.pulls.get(branch)

    def branch_sha(self, branch: str) -> str:
        return self.main_sha if branch == "main" else "b" * 40

    def pulls_for_head(self, owner: str, branch: str):
        found = [p for p in self.head_pulls if p["head"]["repo"]["full_name"].split("/")[0] == owner and p["head"]["ref"] == branch]
        return found, self.head_pulls_complete

    def get_run(self, run_id: int):
        return self.run_state.get(run_id, {"id": run_id, "conclusion": "action_required"})

    # writes
    def create_issue_comment(self, number: int, body: str):
        self._next_id += 1
        self.comments.setdefault(number, []).append({"id": self._next_id, "body": body, "user": {"login": BOT}})
        self.writes.append(("comment", number, body))
        return {"id": self._next_id}

    def update_issue_comment(self, comment_id: int, body: str):
        for comments in self.comments.values():
            for comment in comments:
                if comment["id"] == comment_id:
                    comment["body"] = body
        self.writes.append(("update", comment_id, body))

    def approve_run(self, run_id: int):
        if run_id in self.approve_errors:
            raise self.approve_errors[run_id]
        self.writes.append(("approve", run_id))

    def commit_file(self, parent_sha: str, path: str, text: str, message: str) -> str:
        sha = "c%039d" % (len(self.commits) + 1)
        self.commits[sha] = (parent_sha, path, text)
        self.writes.append(("commit", parent_sha, path, text))
        return sha

    def _point(self, branch: str, sha: str) -> None:
        # The branch then holds the default branch's files plus the commit's one file.
        for key in [k for k in self.files if k[1] == branch]:
            del self.files[key]
        for (path, ref), value in list(self.files.items()):
            if ref == "main":
                self.files[(path, branch)] = value
        parent, path, text = self.commits[sha]
        assert parent == self.main_sha, "built on an old base"
        self.files[(path, branch)] = (text, "blob-" + sha)

    def create_branch(self, branch: str, sha: str):
        if self.branch_error is not None:
            raise self.branch_error
        self._point(branch, sha)
        self.writes.append(("branch", branch, sha))

    def update_branch(self, branch: str, sha: str):
        self._point(branch, sha)
        self.writes.append(("point", branch, sha))

    def create_pull(self, head: str, base: str, title: str, body: str):
        pull = {"html_url": "https://github.com/%s/pull/77" % self.repo, "head": head, "body": body}
        self.pulls[head] = pull
        self.writes.append(("pull", head, base, title))
        return pull


def gate_mod_writes() -> int:
    from code_review_service import scheduler as sched
    return sched.GATE_WRITES_PER_TICK


def make_pr(number: int, login: str, fork: bool = False, head: str = "a" * 40, user_id: int = 0) -> Dict[str, Any]:
    head_repo = {"full_name": "someone/fork" if fork else REPO}
    return {
        "number": number,
        "state": "open",
        "user": {"login": login, "type": "User", "id": user_id or 5000 + number},
        "head": {"sha": head, "ref": "topic", "repo": head_repo},
        "base": {"ref": "main", "repo": {"full_name": REPO, "default_branch": "main"}},
    }


def request(number: int, login: str, line: str, user_id: int = 0) -> mentions.Request:
    user_id = user_id or 5000 + number  # make_pr's author id: the commenter is the PR author
    return mentions.Request(
        kind=mentions.ISSUE, comment_id=900 + number, number=number, login=login,
        url="https://github.com/%s/pull/%d#issuecomment-%d" % (REPO, number, 900 + number),
        command=mentions.parse_command(line), line=line, user_id=user_id,
    )


class GateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = DashboardStore(Path(tmp.name) / "dashboard.sqlite3")
        self.gh = FakeGitHub()
        self.gh.files[(c.APPROVED_FILE, "main")] = ("alice\n*@example.com\n", "list-sha")
        self.gh.files[(c.PLEDGE_FILE, "main")] = ("# pledge\n", "pledge-sha")
        self.now = 1000.0
        self.logged: List[str] = []
        self.keeper = gate_mod.Gatekeeper(clock=lambda: self.now, log=self.logged.append)
        self.config = dict(GATE)


class DecideTest(GateTestCase):
    def decide(self, login: str, **kwargs: Any) -> c.Decision:
        return self.keeper.decide(self.gh, self.config, REPO, make_pr(1, login, **kwargs), self.store)

    def test_listed_login_is_trusted_without_a_permission_read(self) -> None:
        self.assertEqual(c.Decision(True, "approved list"), self.decide("Alice"))
        self.assertEqual([], self.gh.permission_reads)

    def test_write_permission(self) -> None:
        self.gh.permissions["carol"] = "write"
        self.assertEqual(c.Decision(True, "write permission"), self.decide("carol"))

    def test_a_login_the_deployment_approves(self) -> None:
        self.config = dict(GATE, contributor_gate={"enabled": True, "approved_logins": ["Grace"]})
        self.assertEqual(c.Decision(True, "approved by the deployment"), self.decide("grace"))
        self.assertEqual([], self.gh.permission_reads)

    def test_this_bot_and_trusted_bots(self) -> None:
        self.assertEqual("this bot", self.decide(BOT).reason)
        self.assertEqual("trusted bot", self.decide("dependabot[bot]").reason)

    def test_a_read_failure_propagates_and_is_not_cached(self) -> None:
        calls = []

        def broken(login):
            calls.append(login)
            raise status_error(502)

        self.gh.collaborator_permission = broken
        with self.assertRaises(svc.ServiceError):
            self.decide("mallory")
        del self.gh.collaborator_permission
        self.gh.permissions["mallory"] = "write"
        self.assertTrue(self.decide("mallory").trusted)
        self.assertEqual(["mallory"], calls)

    def test_a_reassigned_login_is_decided_afresh_within_the_ttl(self) -> None:
        self.gh.permissions["dave"] = "write"
        self.assertTrue(self.keeper.decide(self.gh, self.config, REPO, make_pr(1, "dave", user_id=1), self.store).trusted)
        self.gh.permissions["dave"] = "read"
        self.assertFalse(self.keeper.decide(self.gh, self.config, REPO, make_pr(1, "dave", user_id=2), self.store).trusted)

    def test_a_removed_domain_line_revokes_a_cached_trust_after_the_trusted_ttl(self) -> None:
        code = c.new_verify_code()
        self.store.create_contributor_code("dave@example.com", c.code_hash(code), "dave")
        self.store.consume_contributor_code(c.code_hash(code), "dave", "https://x/1", user_id=5007)
        self.assertTrue(self.keeper.decide(self.gh, self.config, REPO, make_pr(7, "dave"), self.store).trusted)
        self.gh.files[(c.APPROVED_FILE, "main")] = ("alice\n", "list-sha-2")  # *@example.com removed
        self.now += gate_mod.TRUSTED_TTL - 1
        self.assertTrue(self.keeper.decide(self.gh, self.config, REPO, make_pr(7, "dave"), self.store).trusted)
        self.now += 2
        self.assertFalse(self.keeper.decide(self.gh, self.config, REPO, make_pr(7, "dave"), self.store).trusted)
        self.assertIsNotNone(self.store.contributor_identity("dave"))  # the binding stays; it approves nothing alone

    def test_unknown_author_is_cached_for_the_untrusted_ttl(self) -> None:
        self.assertFalse(self.decide("mallory").trusted)
        self.assertFalse(self.decide("mallory").trusted)
        self.assertEqual(["mallory"], self.gh.permission_reads)
        self.now += gate_mod.UNTRUSTED_TTL + 1
        self.gh.permissions["mallory"] = "write"
        self.assertTrue(self.decide("mallory").trusted)

    def test_verified_email_at_a_listed_or_configured_domain(self) -> None:
        for login, email in (("dave", "dave@example.com"), ("erin", "erin@company.example")):
            self.store.create_contributor_code(email, c.code_hash("rol-" + login + "0000"), login)
            self.assertEqual(email, self.store.consume_contributor_code(c.code_hash("rol-" + login + "0000"), login, user_id=5001))
            self.assertTrue(self.decide(login).trusted, login)

    def test_a_binding_does_not_follow_a_reassigned_login(self) -> None:
        self.store.create_contributor_code("dave@example.com", c.code_hash("rol-dave0000"), "dave")
        self.store.consume_contributor_code(c.code_hash("rol-dave0000"), "dave", user_id=5001)
        self.assertFalse(self.keeper.decide(self.gh, self.config, REPO, make_pr(1, "dave", user_id=9999), self.store).trusted)

    def test_verified_email_at_another_domain_is_not_enough(self) -> None:
        self.store.create_contributor_code("frank@other.example", c.code_hash("rol-frank0000"), "frank")
        self.store.consume_contributor_code(c.code_hash("rol-frank0000"), "frank")
        self.assertFalse(self.decide("frank").trusted)

    def test_gate_applies_only_when_enabled(self) -> None:
        self.assertTrue(self.keeper.applies(self.config, REPO))
        self.assertFalse(self.keeper.applies({}, REPO))
        self.assertFalse(self.keeper.applies({"contributor_gate": {"enabled": "yes"}}, REPO))


class CommentTest(GateTestCase):
    def test_gate_comment_is_posted_once_and_updated_only_when_it_changes(self) -> None:
        pull = make_pr(5, "mallory")
        self.assertTrue(self.keeper.post_gate_comment(self.gh, self.config, REPO, pull, self.store))
        self.assertFalse(self.keeper.post_gate_comment(self.gh, self.config, REPO, pull, self.store))
        # A restart forgets what it wrote; the comment on GitHub is the same: no write.
        fresh = gate_mod.Gatekeeper(clock=lambda: self.now, log=self.logged.append)
        self.assertFalse(fresh.post_gate_comment(self.gh, self.config, REPO, pull, self.store))
        self.assertEqual(1, len([w for w in self.gh.writes if w[0] == "comment"]))
        body = self.gh.comments[5][0]["body"]
        self.assertTrue(body.startswith(c.GATE_MARKER))
        self.assertIn("@review-of-legends " + c.PLEDGE_SENTENCE, body)
        self.assertIn("`@company.example`", body)
        self.assertIn("https://reviews.example.com/#contributor", body)

    def test_a_copied_marker_by_someone_else_is_never_edited(self) -> None:
        self.gh.comments[5] = [{"id": 1, "body": c.GATE_MARKER + " copied", "user": {"login": "mallory"}}]
        self.keeper.post_gate_comment(self.gh, self.config, REPO, make_pr(5, "mallory"), self.store)
        self.assertEqual(c.GATE_MARKER + " copied", self.gh.comments[5][0]["body"])
        self.assertEqual("comment", self.gh.writes[-1][0])

    def test_approved_turns_the_gate_comment_into_a_note_once(self) -> None:
        pull = make_pr(5, "mallory")
        self.keeper.post_gate_comment(self.gh, self.config, REPO, pull, self.store)
        decision = c.Decision(True, "approved list")
        self.assertTrue(self.keeper.mark_approved(self.gh, self.config, REPO, pull, decision))
        self.assertFalse(self.keeper.mark_approved(self.gh, self.config, REPO, pull, decision))
        self.assertIn("is an approved contributor (approved list)", self.gh.comments[5][0]["body"])

    def test_no_gate_comment_means_nothing_to_approve(self) -> None:
        self.assertFalse(self.keeper.mark_approved(self.gh, self.config, REPO, make_pr(6, "alice"), c.Decision(True, "x")))
        self.assertEqual([], self.gh.writes)


class ForkCiTest(GateTestCase):
    def test_waiting_runs_of_a_fork_head_are_approved(self) -> None:
        self.gh.runs["a" * 40] = [(11, "someone/fork", "topic"), (12, "someone/fork", "topic")]
        self.assertEqual(2, self.keeper.approve_fork_runs(self.gh, self.config, REPO, make_pr(1, "alice", fork=True)))
        self.assertEqual([("approve", 11), ("approve", 12)], self.gh.writes)

    def test_a_run_naming_other_pull_requests_is_not_approved(self) -> None:
        self.gh.runs["a" * 40] = [(11, "someone/fork", "topic")]
        listed = self.gh.list_waiting_runs
        self.gh.list_waiting_runs = lambda head: [dict(run, pull_requests=[{"number": 99}]) for run in listed(head)]
        self.assertEqual(0, self.keeper.approve_fork_runs(self.gh, self.config, REPO, make_pr(1, "alice", fork=True)))

    def test_another_prs_runs_on_the_same_commit_are_not_approved(self) -> None:
        # An unapproved author can open a pull request from another fork or branch at the very same commit.
        self.gh.runs["a" * 40] = [(11, "someone/fork", "topic"), (12, "mallory/fork", "topic"), (13, "someone/fork", "other")]
        self.assertEqual(1, self.keeper.approve_fork_runs(self.gh, self.config, REPO, make_pr(1, "alice", fork=True)))
        self.assertEqual([("approve", 11)], self.gh.writes)

    def test_a_branch_of_the_repository_itself_is_left_alone(self) -> None:
        self.gh.runs["a" * 40] = [(11, REPO, "topic")]
        self.assertEqual(0, self.keeper.approve_fork_runs(self.gh, self.config, REPO, make_pr(1, "alice")))

    def test_a_refusal_backs_off_and_says_why(self) -> None:
        self.gh.runs_error = status_error(403)
        self.assertEqual(0, self.keeper.approve_fork_runs(self.gh, self.config, REPO, make_pr(1, "alice", fork=True)))
        self.assertIn("Actions: read and write", self.logged[-1])
        self.gh.runs_error = None
        self.gh.runs["a" * 40] = [(11, "someone/fork", "topic")]
        self.assertEqual(0, self.keeper.approve_fork_runs(self.gh, self.config, REPO, make_pr(1, "alice", fork=True)))
        self.now += gate_mod.NO_PERMISSION_BACKOFF + 1
        self.assertEqual(1, self.keeper.approve_fork_runs(self.gh, self.config, REPO, make_pr(1, "alice", fork=True)))

    def test_an_unlinked_run_waits_while_a_closed_pr_of_an_unapproved_author_shares_the_commit(self) -> None:
        self.gh.runs["a" * 40] = [(11, "someone/fork", "topic")]
        closed = make_pr(2, "mallory", fork=True)
        closed["state"] = "closed"
        closed["base"]["ref"] = "release"
        self.gh.head_pulls = [make_pr(1, "alice", fork=True), closed]
        self.assertEqual(0, self.keeper.approve_fork_runs(self.gh, self.config, REPO, make_pr(1, "alice", fork=True), self.store))
        self.assertIn("#2 (mallory) shares the commit", self.logged[-1])
        # A run GitHub links to this PR is provably its own: approved.
        listed = self.gh.list_waiting_runs
        self.gh.list_waiting_runs = lambda head: [dict(run, pull_requests=[{"number": 1}]) for run in listed(head)]
        self.assertEqual(1, self.keeper.approve_fork_runs(self.gh, self.config, REPO, make_pr(1, "alice", fork=True), self.store))
        self.assertFalse(self.keeper.ci_blocked(REPO))

    def test_a_run_linked_to_this_pr_and_a_closed_one_of_an_unapproved_author_waits(self) -> None:
        self.gh.runs["a" * 40] = [(11, "someone/fork", "topic")]
        closed = make_pr(2, "mallory", fork=True)
        closed["state"] = "closed"
        self.gh.prs[2] = closed
        listed = self.gh.list_waiting_runs
        self.gh.list_waiting_runs = lambda head: [dict(run, pull_requests=[{"number": 1}, {"number": 2}]) for run in listed(head)]
        self.assertEqual(0, self.keeper.approve_fork_runs(self.gh, self.config, REPO, make_pr(1, "alice", fork=True), self.store))
        self.assertTrue(self.keeper.waits_on_others(REPO, 1))
        self.assertIn("also belongs to #2", self.logged[-1])
        self.gh.files[(c.APPROVED_FILE, "main")] = ("alice\nmallory\n", "list-sha-2")
        self.now += gate_mod.UNTRUSTED_TTL + gate_mod.LISTS_TTL + 1
        self.assertEqual(1, self.keeper.approve_fork_runs(self.gh, self.config, REPO, make_pr(1, "alice", fork=True), self.store))
        self.assertFalse(self.keeper.waits_on_others(REPO, 1))

    def test_an_unlinked_run_waits_when_the_prs_of_its_branch_cannot_all_be_read(self) -> None:
        self.gh.runs["a" * 40] = [(11, "someone/fork", "topic")]
        self.gh.head_pulls_complete = False
        self.assertEqual(0, self.keeper.approve_fork_runs(self.gh, self.config, REPO, make_pr(1, "alice", fork=True), self.store))
        self.assertIn("too many pull requests", self.logged[-1])

    def test_an_unlinked_run_goes_when_every_pr_at_the_commit_is_approved(self) -> None:
        self.gh.runs["a" * 40] = [(11, "someone/fork", "topic")]
        other = make_pr(2, "bob", fork=True)
        other["state"] = "closed"
        old = make_pr(3, "mallory", fork=True, head="d" * 40)  # an earlier commit of the branch
        self.gh.permissions["bob"] = "write"
        self.gh.head_pulls = [make_pr(1, "alice", fork=True), other, old]
        self.assertEqual(1, self.keeper.approve_fork_runs(self.gh, self.config, REPO, make_pr(1, "alice", fork=True), self.store))

    def test_a_shared_run_is_approved_once(self) -> None:
        # Two approved PRs share the run; GitHub's listing still shows it for the second.
        self.gh.runs["a" * 40] = [(11, "someone/fork", "topic")]
        self.assertEqual(1, self.keeper.approve_fork_runs(self.gh, self.config, REPO, make_pr(1, "alice", fork=True)))
        self.assertEqual(0, self.keeper.approve_fork_runs(self.gh, self.config, REPO, make_pr(2, "alice", fork=True)))
        self.assertEqual([("approve", 11)], self.gh.writes)

    def test_a_run_someone_approved_meanwhile_is_no_refusal(self) -> None:
        self.gh.runs["a" * 40] = [(11, "someone/fork", "topic"), (12, "someone/fork", "topic")]
        self.gh.approve_errors[11] = status_error(403)
        self.gh.run_state[11] = {"id": 11, "status": "queued", "conclusion": None}
        self.assertEqual(1, self.keeper.approve_fork_runs(self.gh, self.config, REPO, make_pr(1, "alice", fork=True)))
        self.assertFalse(self.keeper.ci_blocked(REPO))
        self.assertIn(("approve", 12), self.gh.writes)

    def test_a_run_gone_before_its_approval_is_skipped_without_a_backoff(self) -> None:
        self.gh.runs["a" * 40] = [(11, "someone/fork", "topic"), (12, "someone/fork", "topic")]
        self.gh.approve_errors[11] = status_error(404)
        self.assertEqual(1, self.keeper.approve_fork_runs(self.gh, self.config, REPO, make_pr(1, "alice", fork=True)))
        self.assertIn(("approve", 12), self.gh.writes)
        self.assertFalse(self.keeper.ci_blocked(REPO))

    def test_a_refused_approval_backs_off(self) -> None:
        self.gh.runs["a" * 40] = [(11, "someone/fork", "topic"), (12, "someone/fork", "topic")]
        self.gh.approve_errors[11] = status_error(403)
        self.assertEqual(0, self.keeper.approve_fork_runs(self.gh, self.config, REPO, make_pr(1, "alice", fork=True)))
        self.assertTrue(self.keeper.ci_blocked(REPO))
        self.assertNotIn(("approve", 12), self.gh.writes)

    def test_a_transient_failure_propagates(self) -> None:
        self.gh.runs["a" * 40] = [(11, "someone/fork", "topic")]
        self.gh.approve_errors[11] = status_error(502)
        with self.assertRaises(svc.ServiceError):
            self.keeper.approve_fork_runs(self.gh, self.config, REPO, make_pr(1, "alice", fork=True))
        self.assertFalse(self.keeper.ci_blocked(REPO))

    def test_switched_off(self) -> None:
        self.config = dict(GATE, contributor_gate={"enabled": True, "approve_fork_ci": False})
        self.gh.runs["a" * 40] = [(11, "someone/fork", "topic")]
        self.assertEqual(0, self.keeper.approve_fork_runs(self.gh, self.config, REPO, make_pr(1, "alice", fork=True)))


class CommandTest(GateTestCase):
    def answer(self, number: int, login: str, line: str) -> str:
        self.gh.prs.setdefault(number, make_pr(number, "mallory"))
        return self.keeper.handle_command(self.gh, self.store, self.config, REPO, request(number, login, line))

    def test_verify_binds_and_approves_a_trusted_domain(self) -> None:
        code = c.new_verify_code()
        self.store.create_contributor_code("mallory@company.example", c.code_hash(code), "mallory")
        self.assertFalse(self.keeper.decide(self.gh, self.config, REPO, make_pr(1, "mallory"), self.store).trusted)
        reply = self.answer(1, "mallory", "verify " + code)
        self.assertIn("company.example", reply)
        self.assertIn("now reviewed", reply)
        # The cached refusal is gone: trusted at once.
        self.assertTrue(self.keeper.decide(self.gh, self.config, REPO, make_pr(1, "mallory"), self.store).trusted)
        other = request(1, "mallory", "verify " + code)._replace(comment_id=990, url="https://github.com/%s/pull/1#issuecomment-990" % REPO)
        self.assertIn("already used", self.keeper.handle_command(self.gh, self.store, self.config, REPO, other))  # used: refused
        self.assertIsNotNone(self.store.contributor_identity("Mallory"))

    def test_a_code_read_off_a_public_comment_is_worthless_to_someone_else(self) -> None:
        code = c.new_verify_code()
        self.store.create_contributor_code("mallory@company.example", c.code_hash(code), "mallory")
        self.answer(1, "eve", "verify " + code)
        self.assertIsNone(self.store.contributor_identity("eve"))
        # Not used up by eve: mallory still can.
        self.assertIn("now reviewed", self.answer(1, "mallory", "verify " + code))

    def test_a_failed_read_leaves_the_code_for_the_retry_and_a_replay_gets_the_same_answer(self) -> None:
        code = c.new_verify_code()
        self.store.create_contributor_code("mallory@company.example", c.code_hash(code), "mallory")
        self.gh.prs[1] = make_pr(1, "mallory")
        real = self.gh.get_pr
        self.gh.get_pr = lambda number: (_ for _ in ()).throw(status_error(502))
        with self.assertRaises(svc.ServiceError):
            self.answer(1, "mallory", "verify " + code)
        self.assertIsNone(self.store.contributor_identity("mallory"))
        self.gh.get_pr = real
        self.assertIn("now reviewed", self.answer(1, "mallory", "verify " + code))
        # The same comment handled again (its reply failed to post): the same answer.
        self.assertIn("now reviewed", self.answer(1, "mallory", "verify " + code))
        # Another comment with the used code: refused.
        other = request(1, "mallory", "verify " + code)._replace(comment_id=990, url="https://github.com/%s/pull/1#issuecomment-990" % REPO)
        self.assertIn("already used", self.keeper.handle_command(self.gh, self.store, self.config, REPO, other))

    def test_a_replay_still_works_after_another_code_was_issued(self) -> None:
        code = c.new_verify_code()
        self.store.create_contributor_code("mallory@company.example", c.code_hash(code), "mallory")
        self.assertIn("now reviewed", self.answer(1, "mallory", "verify " + code))
        self.store.create_contributor_code("erin@company.example", c.code_hash(c.new_verify_code()), "erin")
        self.assertIn("now reviewed", self.answer(1, "mallory", "verify " + code))

    def test_verify_with_an_unknown_code(self) -> None:
        reply = self.answer(1, "mallory", "verify rol-00000000")
        self.assertIsNone(self.store.contributor_identity("mallory"))
        self.assertTrue(reply.startswith(mentions.REPLY_MARKER))

    def test_pledge_opens_the_pull_request_that_adds_the_login(self) -> None:
        reply = self.answer(3, "mallory", c.PLEDGE_SENTENCE)
        self.assertIn("https://github.com/%s/pull/77" % REPO, reply)
        branch = gate_mod.PLEDGE_BRANCH_PREFIX + "mallory"
        wanted = "alice\n*@example.com\nmallory  # pledge signed in %s#3\n" % REPO
        self.assertEqual(("commit", "f" * 40, c.APPROVED_FILE, wanted), self.gh.writes[0])
        self.assertEqual("branch", self.gh.writes[1][0])
        self.assertEqual(wanted, self.gh.files[(c.APPROVED_FILE, branch)][0])
        pull = self.gh.pulls[branch]
        self.assertIn("pledge-sha", pull["body"])
        self.assertIn("5003", pull["body"])

    def test_a_branch_left_from_a_declined_pull_request_starts_again_from_main(self) -> None:
        branch = gate_mod.PLEDGE_BRANCH_PREFIX + "mallory"
        # The old branch still lists a login a maintainer has since removed from main.
        self.gh.files[(c.APPROVED_FILE, branch)] = ("alice\nobsolete\nmallory\n", "old-sha")
        self.gh.main_sha = "e" * 40  # main moved on since that branch was cut
        self.gh.files[(c.APPROVED_FILE, "main")] = ("alice\nzed\n", "list-sha-2")
        self.gh.branch_error = status_error(422)
        self.answer(3, "mallory", c.PLEDGE_SENTENCE)
        wanted = "alice\nzed\nmallory  # pledge signed in %s#3\n" % REPO
        self.assertIn(("commit", "e" * 40, c.APPROVED_FILE, wanted), self.gh.writes)
        self.assertEqual("point", [w for w in self.gh.writes if w[0] in ("branch", "point")][0][0])
        self.assertEqual(wanted, self.gh.files[(c.APPROVED_FILE, branch)][0])
        self.assertIn(branch, self.gh.pulls)
        # Signing again (another comment) with nothing changed points at the same pull request, nothing new.
        writes = len(self.gh.writes)
        again = self.keeper.handle_command(self.gh, self.store, self.config, REPO, request(3, "mallory", c.PLEDGE_SENTENCE)._replace(comment_id=999))
        self.assertIn("pull/77", again)
        self.assertEqual(writes, len(self.gh.writes))

    def test_signing_again_rebuilds_a_pull_request_a_merged_sibling_put_in_conflict(self) -> None:
        branch = gate_mod.PLEDGE_BRANCH_PREFIX + "mallory"
        self.answer(3, "mallory", c.PLEDGE_SENTENCE)
        # Another contributor's pledge pull request is merged: main moved and gained a line at the same place.
        self.gh.main_sha = "e" * 40
        self.gh.files[(c.APPROVED_FILE, "main")] = ("alice\n*@example.com\nzed  # pledge signed\n", "list-sha-2")
        writes = len(self.gh.writes)
        again = self.keeper.handle_command(self.gh, self.store, self.config, REPO, request(3, "mallory", c.PLEDGE_SENTENCE)._replace(comment_id=999))
        self.assertIn("pull/77", again)
        new = self.gh.writes[writes:]
        wanted = "alice\n*@example.com\nzed  # pledge signed\nmallory  # pledge signed in %s#3\n" % REPO
        self.assertEqual([("commit", "e" * 40, c.APPROVED_FILE, wanted)], new[:1])
        self.assertEqual(["commit", "point"], [w[0] for w in new])  # no second pull request
        self.assertEqual(wanted, self.gh.files[(c.APPROVED_FILE, branch)][0])

    def test_signing_again_from_another_pull_request_does_not_touch_the_open_one(self) -> None:
        self.answer(3, "mallory", c.PLEDGE_SENTENCE)
        writes = len(self.gh.writes)
        for number in (4, 3, 4):
            self.gh.prs[number] = make_pr(number, "mallory", user_id=5003)
            again = self.keeper.handle_command(
                self.gh, self.store, self.config, REPO,
                request(number, "mallory", c.PLEDGE_SENTENCE, user_id=5003)._replace(comment_id=980 + number + len(self.gh.writes)),
            )
            self.assertIn("pull/77", again)
        self.assertEqual(writes, len(self.gh.writes))  # no new head, so no CI run on the onboarding PR

    def test_a_pledge_keeps_other_authors_cached_decisions(self) -> None:
        self.gh.permissions["bob"] = "write"
        self.assertTrue(self.keeper.decide(self.gh, self.config, REPO, make_pr(5, "bob"), self.store).trusted)
        self.answer(3, "mallory", c.PLEDGE_SENTENCE)
        self.assertTrue(self.keeper.decide(self.gh, self.config, REPO, make_pr(5, "bob"), self.store).trusted)
        self.assertEqual(["bob"], self.gh.permission_reads)

    def test_a_signer_listed_meanwhile_is_told_so(self) -> None:
        # The cached list said no; main lists the signer by the time the pull request is built.
        self.keeper.lists(self.gh, REPO, "main")
        with mock.patch.object(self.keeper, "lists", return_value=(c.parse_list("alice\n"), "pledge-sha")):
            self.gh.files[(c.APPROVED_FILE, "main")] = ("alice\nmallory\n", "list-sha-2")
            reply = self.answer(3, "mallory", c.PLEDGE_SENTENCE)
        self.assertIn("already an approved contributor", reply)
        self.assertEqual([], [w for w in self.gh.writes if w[0] in ("branch", "point", "pull")])

    def test_only_the_author_signs(self) -> None:
        reply = self.answer(3, "eve", c.PLEDGE_SENTENCE)
        self.assertNotIn("pull/77", reply)
        self.assertEqual([], self.gh.writes)

    def test_no_pledge_file(self) -> None:
        del self.gh.files[(c.PLEDGE_FILE, "main")]
        self.answer(3, "mallory", c.PLEDGE_SENTENCE)
        self.assertEqual([], self.gh.writes)

    def test_without_contents_write_the_signature_is_kept_and_a_maintainer_is_asked(self) -> None:
        self.gh.branch_error = status_error(403)
        reply = self.answer(3, "mallory", c.PLEDGE_SENTENCE)
        self.assertIn("mallory", reply)
        self.assertNotIn("pull/", reply)
        self.assertIn("Contents: read and write", self.logged[-1])
        source = mentions.source_key(REPO, request(3, "mallory", c.PLEDGE_SENTENCE))
        signature = self.store.record_pledge(REPO, 3, "mallory", 0, "x", "x", source_key=source)
        self.assertEqual(("mallory", "pledge-sha", None), (signature["login"], signature["pledge_sha"], signature["pull_url"]))

    def test_gate_off_answers_and_changes_nothing(self) -> None:
        self.config = {}
        reply = self.answer(3, "mallory", c.PLEDGE_SENTENCE)
        self.assertTrue(reply.startswith(mentions.REPLY_MARKER))
        self.assertEqual([], self.gh.writes)


class StoreAndParsingTest(GateTestCase):
    def test_an_expired_code_binds_nothing(self) -> None:
        self.store.create_contributor_code("dave@example.com", c.code_hash("rol-dave0000"), "dave", ttl_seconds=-5)
        self.assertIsNone(self.store.consume_contributor_code(c.code_hash("rol-dave0000"), "dave"))
        self.assertIsNone(self.store.contributor_identity("dave"))

    def test_a_bot_login_is_never_bound(self) -> None:
        self.assertIsNone(self.store.contributor_identity("dependabot[bot]"))

    def test_a_pledge_signature_is_recorded_once_per_comment(self) -> None:
        first = self.store.record_pledge(REPO, 3, "mallory", 7, "https://x/1", "sha", source_key="k1")
        again = self.store.record_pledge(REPO, 3, "mallory", 7, "https://x/1", "sha", source_key="k1")
        self.assertEqual(first["id"], again["id"])
        self.store.set_pledge_pull(int(first["id"]), "https://github.com/acme-corp/public/pull/9")
        self.assertEqual(
            "https://github.com/acme-corp/public/pull/9",
            self.store.record_pledge(REPO, 3, "mallory", 7, "https://x/1", "sha", source_key="k1")["pull_url"],
        )

    def test_comment_commands_carry_the_commenter_id(self) -> None:
        comment = {
            "id": 55, "user": {"login": "mallory", "id": 4242, "type": "User"},
            "body": "@review-of-legends " + c.PLEDGE_SENTENCE,
            "html_url": "https://github.com/%s/pull/3#issuecomment-55" % REPO,
        }
        parsed = mentions.request_from(comment, mentions.ISSUE, BOT)
        self.assertEqual((mentions.PLEDGE, 4242, 3), (parsed.command.kind, parsed.user_id, parsed.number))
        self.assertEqual(mentions.VERIFY_INVALID, mentions.parse_command("verify").kind)


class GatedListing(GitHub):
    """test_scheduler's fake, with the gate's calls on the watch client."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = FakeGitHub()

    def watch_client(self, repo: str, cache: Dict[str, Any]) -> Any:
        inner = super().watch_client(repo, cache)
        gate = self.gate

        class Client:
            def __getattr__(self, name: str) -> Any:
                if name in ("get_file", "collaborator_permission", "list_issue_comments", "update_issue_comment",
                            "list_waiting_runs", "approve_run", "pulls_for_head", "get_run"):
                    return getattr(gate, name)
                if name == "create_issue_comment":
                    return gate.create_issue_comment
                return getattr(inner, name)

        return Client()


class SchedulerGateTest(SchedulerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.clock = 50_000.0
        self.listing = GatedListing()
        self.listing.gate.files[(c.APPROVED_FILE, "main")] = ("alice\n", "s")

    def authored(self, number: int, head: str, login: str) -> Dict[str, Any]:
        item = pr(number, head)
        item["user"] = {"login": login, "type": "User"}
        item["head"]["repo"] = {"full_name": "someone/fork"}
        item["head"]["ref"] = "topic"
        item["base"]["repo"] = {"full_name": REPO, "default_branch": "main"}
        return item

    def test_only_approved_authors_are_queued_and_the_rest_get_the_gate_comment(self) -> None:
        scheduler = self.make(**GATE)
        scheduler.control()
        self.listing.gate.runs["a" * 40] = [(31, "someone/fork", "topic")]
        self.listing.prs[REPO] = [self.authored(8, "a" * 40, "alice"), self.authored(9, "c" * 40, "mallory")]
        scheduler.tick()
        self.assertEqual([8], sorted(number for _, number in scheduler._queue))
        writes = self.listing.gate.writes
        self.assertIn(("approve", 31), writes)
        comments = [w for w in writes if w[0] == "comment"]
        self.assertEqual([9], [w[1] for w in comments])
        self.assertTrue(any("mallory is not an approved contributor" in line for line in self.logged))
        # The next tick neither comments again nor queues it.
        scheduler.tick()
        self.assertEqual(1, len([w for w in self.listing.gate.writes if w[0] == "comment"]))
        self.assertEqual([8], sorted(number for _, number in scheduler._queue))

    def test_an_unapproved_authors_runs_stay_waiting(self) -> None:
        scheduler = self.make(monotonic=lambda: self.clock, **GATE)
        scheduler.control()
        self.listing.gate.runs["c" * 40] = [(41, "someone/fork", "topic")]
        self.listing.prs[REPO] = [self.authored(9, "c" * 40, "mallory")]
        scheduler.tick()
        scheduler.tick()
        self.assertNotIn(("approve", 41), self.listing.gate.writes)

    def test_a_pledge_accepted_after_the_window_still_gets_its_ci(self) -> None:
        scheduler = self.make(monotonic=lambda: self.clock, **GATE)
        scheduler.control()
        self.listing.gate.runs["c" * 40] = [(41, "someone/fork", "topic")]
        self.listing.prs[REPO] = [self.authored(9, "c" * 40, "mallory")]
        scheduler.tick()
        # Hours later a maintainer merges the pledge pull request.
        self.clock += gate_mod.CI_APPROVAL_WINDOW * 4
        self.listing.gate.files[(c.APPROVED_FILE, "main")] = ("alice\nmallory\n", "s2")
        scheduler.tick()
        self.assertIn(("approve", 41), self.listing.gate.writes)

    def test_a_permission_backoff_does_not_use_up_the_window(self) -> None:
        scheduler = self.make(monotonic=lambda: self.clock, **GATE)
        scheduler.control()
        self.listing.prs[REPO] = [self.authored(8, "a" * 40, "alice")]
        self.listing.gate.runs_error = status_error(403)
        scheduler.tick()
        self.listing.gate.runs_error = None
        self.listing.gate.runs["a" * 40] = [(31, "someone/fork", "topic")]
        # The App gets Actions write; the hour-long backoff ends after the 30-minute window would have.
        self.clock += gate_mod.NO_PERMISSION_BACKOFF + 1
        scheduler.tick()
        self.assertIn(("approve", 31), self.listing.gate.writes)

    def test_prs_sharing_a_branch_and_commit_are_approved_together_or_not_at_all(self) -> None:
        scheduler = self.make(monotonic=lambda: self.clock, **GATE)
        scheduler.control()
        self.listing.gate.runs["a" * 40] = [(31, "someone/fork", "topic")]
        twin = self.authored(9, "a" * 40, "mallory")  # same fork, branch and commit; another base branch
        twin["base"]["ref"] = "release"
        self.listing.prs[REPO] = [self.authored(8, "a" * 40, "alice"), twin]
        scheduler.tick()
        self.assertNotIn(("approve", 31), self.listing.gate.writes)
        self.listing.gate.files[(c.APPROVED_FILE, "main")] = ("alice\nmallory\n", "s2")
        self.clock += gate_mod.UNTRUSTED_TTL + 1
        scheduler.tick()
        self.assertIn(("approve", 31), self.listing.gate.writes)

    def test_an_unapproved_draft_on_the_same_branch_and_commit_keeps_the_runs_waiting(self) -> None:
        scheduler = self.make(monotonic=lambda: self.clock, **GATE)
        scheduler.control()
        self.listing.gate.runs["a" * 40] = [(31, "someone/fork", "topic")]
        draft = self.authored(9, "a" * 40, "mallory")
        draft["draft"] = True
        draft["base"]["ref"] = "release"
        self.listing.prs[REPO] = [self.authored(8, "a" * 40, "alice"), draft]
        scheduler.tick()
        scheduler.tick()
        self.assertNotIn(("approve", 31), self.listing.gate.writes)
        # mallory approved: the ready PR's runs go, the draft itself still gets no CI approval of its own.
        self.listing.gate.files[(c.APPROVED_FILE, "main")] = ("alice\nmallory\n", "s2")
        self.clock += gate_mod.UNTRUSTED_TTL + 1
        scheduler.tick()
        self.assertEqual(1, self.listing.gate.writes.count(("approve", 31)))

    def test_a_run_held_for_a_closed_prs_author_keeps_its_window(self) -> None:
        scheduler = self.make(monotonic=lambda: self.clock, **GATE)
        scheduler.control()
        self.listing.gate.runs["a" * 40] = [(31, "someone/fork", "topic")]
        closed = self.authored(9, "a" * 40, "mallory")
        closed["state"] = "closed"
        self.listing.gate.head_pulls = [self.authored(8, "a" * 40, "alice"), closed]
        self.listing.prs[REPO] = [self.authored(8, "a" * 40, "alice")]
        for _ in range(3):
            scheduler.tick()
            self.clock += gate_mod.CI_APPROVAL_WINDOW / 2
        self.assertNotIn(("approve", 31), self.listing.gate.writes)
        # Hours later mallory is accepted: the run still goes.
        self.listing.gate.files[(c.APPROVED_FILE, "main")] = ("alice\nmallory\n", "s2")
        self.clock += gate_mod.UNTRUSTED_TTL + 1
        scheduler.tick()
        self.assertIn(("approve", 31), self.listing.gate.writes)

    def test_an_actions_outage_does_not_use_up_the_window(self) -> None:
        scheduler = self.make(monotonic=lambda: self.clock, **GATE)
        scheduler.control()
        self.listing.prs[REPO] = [self.authored(8, "a" * 40, "alice")]
        self.listing.gate.runs_error = status_error(502)
        for _ in range(3):
            scheduler.tick()
            self.clock += gate_mod.CI_APPROVAL_WINDOW / 2
        self.listing.gate.runs_error = None
        self.listing.gate.runs["a" * 40] = [(31, "someone/fork", "topic")]
        scheduler.tick()
        self.assertIn(("approve", 31), self.listing.gate.writes)

    def test_an_approved_note_left_by_the_write_budget_follows_after_the_review(self) -> None:
        scheduler = self.make(monotonic=lambda: self.clock, **GATE)
        scheduler.control()
        numbers = [9, 10, 11, 12]
        self.listing.prs[REPO] = [self.authored(n, ("%x" % n) * 40, "mallory") for n in numbers]
        for _ in numbers:
            scheduler.tick()  # three gate comments a tick
        self.assertEqual(numbers, sorted(self.listing.gate.comments))
        self.listing.gate.files[(c.APPROVED_FILE, "main")] = ("alice\nmallory\n", "s2")
        self.clock += gate_mod.UNTRUSTED_TTL + 1
        updates = len([w for w in self.listing.gate.writes if w[0] == "update"])
        scheduler.tick()
        self.assertEqual(numbers, sorted(number for _, number in scheduler._queue))
        self.assertEqual(updates + gate_mod_writes(), len([w for w in self.listing.gate.writes if w[0] == "update"]))
        self.assertNotIn("is an approved contributor", self.listing.gate.comments[12][0]["body"])
        # The last one waits for budget; its head is reviewed (no longer a candidate) before the next tick.
        scheduler._queue.clear()
        with svc.STATE_LOCK:
            reviewed = svc.repo_state(scheduler.state, REPO)["reviewed"]
            for n in numbers:
                reviewed[str(n)] = {"head_sha": ("%x" % n) * 40, "reviewed_at": "2026-11-12T00:00:00+00:00"}
        scheduler.tick()
        self.assertEqual({}, scheduler._queue)
        for n in numbers:
            self.assertIn("is an approved contributor", self.listing.gate.comments[n][0]["body"], n)

    def test_failing_comment_writes_use_up_the_tick_budget(self) -> None:
        scheduler = self.make(monotonic=lambda: self.clock, **GATE)
        scheduler.control()
        self.listing.prs[REPO] = [self.authored(n, ("%x" % n) * 40, "mallory") for n in (9, 10, 11, 12, 13)]
        attempts: List[int] = []

        def failing(number: int, body: str):
            attempts.append(number)
            raise status_error(502)

        self.listing.gate.create_issue_comment = failing
        scheduler.tick()
        self.assertEqual(gate_mod_writes(), len(attempts))
        # The next tick tries the ones not tried yet first.
        self.clock += 1
        scheduler.tick()
        self.assertEqual({9, 10, 11, 12, 13}, set(attempts))

    def test_an_author_requested_review_is_refused_for_an_unapproved_author(self) -> None:
        scheduler = self.make(monotonic=lambda: self.clock, **GATE)
        scheduler.control()
        client = self.listing.watch_client(REPO, {})
        self.listing.live[(REPO, 9)] = self.authored(9, "c" * 40, "mallory")
        outcome = scheduler._author_review(client, REPO, request(9, "mallory", "review"))
        self.assertEqual(("gated", ""), outcome)
        self.assertIn("gated", mentions._REVIEW_REPLIES["en"])
        self.assertIn("gated", mentions._REVIEW_REPLIES["ko"])

    def test_fork_ci_is_approved_after_the_review_too(self) -> None:
        reviewed = {"8": {"head_sha": "a" * 40, "reviewed_at": "2026-10-13T10:00:00+00:00"}}
        scheduler = self.make(reviewed=reviewed, monotonic=lambda: self.clock, **GATE)
        scheduler.control()
        self.listing.prs[REPO] = [self.authored(8, "a" * 40, "alice")]
        scheduler.tick()
        self.assertEqual({}, scheduler._queue)  # reviewed already
        self.listing.gate.runs["a" * 40] = [(31, "someone/fork", "topic")]  # a workflow that started late
        scheduler.tick()
        self.assertIn(("approve", 31), self.listing.gate.writes)
        # Past the window the head is left alone.
        self.listing.gate.runs["a" * 40] = [(32, "someone/fork", "topic")]
        self.clock += gate_mod.CI_APPROVAL_WINDOW + 1
        scheduler.tick()
        self.assertNotIn(("approve", 32), self.listing.gate.writes)

    def test_a_gate_read_failure_skips_this_tick_and_recovers(self) -> None:
        scheduler = self.make(monotonic=lambda: self.clock, **GATE)
        scheduler.control()
        self.listing.prs[REPO] = [self.authored(9, "c" * 40, "carol")]
        self.listing.gate.permissions["carol"] = "write"
        real = self.listing.gate.collaborator_permission
        self.listing.gate.collaborator_permission = lambda login: (_ for _ in ()).throw(status_error(502))
        scheduler.tick()
        self.assertEqual({}, scheduler._queue)
        self.assertTrue(any("contributor gate could not be read" in line for line in self.logged))
        self.listing.gate.collaborator_permission = real
        scheduler.tick()
        self.assertEqual([9], sorted(number for _, number in scheduler._queue))

    def test_listing_the_author_lets_the_pr_in_and_notes_it(self) -> None:
        scheduler = self.make(monotonic=lambda: self.clock, **GATE)
        scheduler.control()
        self.listing.prs[REPO] = [self.authored(9, "c" * 40, "mallory")]
        scheduler.tick()
        self.assertEqual({}, scheduler._queue)
        self.listing.gate.files[(c.APPROVED_FILE, "main")] = ("alice\nmallory\n", "s2")
        # No hand-holding: the caches expire by the gatekeeper's own clock.
        self.clock += gate_mod.UNTRUSTED_TTL + 1
        scheduler.tick()
        self.assertEqual([9], sorted(number for _, number in scheduler._queue))
        self.assertIn("is an approved contributor", self.listing.gate.comments[9][0]["body"])

    def test_gate_off_queues_everyone_without_any_gate_call(self) -> None:
        scheduler = self.make()
        scheduler.control()
        self.listing.prs[REPO] = [self.authored(9, "c" * 40, "mallory")]
        scheduler.tick()
        self.assertEqual([9], sorted(number for _, number in scheduler._queue))
        self.assertEqual([], self.listing.gate.writes)
        self.assertEqual([], self.listing.gate.permission_reads)


class ClientContractTest(unittest.TestCase):
    """The real GitHubClient calls behind the gate's fake (what the fakes take for granted)."""

    def client(self) -> svc.GitHubClient:
        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"):
            return svc.GitHubClient(REPO)

    def test_waiting_runs_are_asked_for_this_commit_and_read_to_the_last_page(self) -> None:
        client = self.client()
        calls: List[str] = []
        pages = {
            "first": ({"workflow_runs": [{"id": 1}, "junk"]}, "https://api.github.com/next-page"),
            "https://api.github.com/next-page": ({"workflow_runs": [{"id": 2}]}, None),
        }

        def request(method, endpoint, body=None, paginate=False, conditional=False, with_next=False):
            calls.append(endpoint)
            self.assertTrue(with_next)
            return pages["first" if len(calls) == 1 else endpoint]

        with mock.patch.object(client, "request", side_effect=request):
            runs = client.list_waiting_runs("a" * 40)
        self.assertEqual([1, 2], [run["id"] for run in runs])
        query = dict(svc.urllib.parse.parse_qsl(svc.urllib.parse.urlparse(calls[0]).query))
        self.assertEqual(
            {"event": "pull_request", "status": "action_required", "head_sha": "a" * 40, "per_page": "100"}, query,
        )
        self.assertTrue(calls[0].startswith("repos/%s/actions/runs?" % REPO))

    def test_waiting_runs_stop_at_the_page_limit(self) -> None:
        client = self.client()
        with mock.patch.object(client, "request", return_value=({"workflow_runs": [{"id": 1}]}, "https://api.github.com/more")) as request:
            runs = client.list_waiting_runs("a" * 40)
        self.assertEqual(svc.WAITING_RUN_PAGES, request.call_count)
        self.assertEqual(svc.WAITING_RUN_PAGES, len(runs))

    def test_pulls_for_head_reads_every_page_and_says_when_it_stopped_short(self) -> None:
        client = self.client()
        calls: List[str] = []

        def request(method, endpoint, body=None, paginate=False, conditional=False, with_next=False):
            calls.append(endpoint)
            return ([{"number": len(calls)}], "https://api.github.com/p%d" % len(calls) if len(calls) < 3 else None)

        with mock.patch.object(client, "request", side_effect=request):
            pulls, complete = client.pulls_for_head("someone", "topic")
        self.assertEqual(([1, 2, 3], True), ([p["number"] for p in pulls], complete))
        query = dict(svc.urllib.parse.parse_qsl(svc.urllib.parse.urlparse(calls[0]).query))
        self.assertEqual({"head": "someone:topic", "state": "all", "per_page": "100"}, query)
        with mock.patch.object(client, "request", return_value=([{"number": 1}], "https://api.github.com/more")):
            pulls, complete = client.pulls_for_head("someone", "topic")
        self.assertFalse(complete)
        self.assertEqual(svc.WAITING_RUN_PAGES, len(pulls))

    def test_commit_file_builds_on_the_parent_without_moving_a_branch(self) -> None:
        client = self.client()
        answers = iter([{"tree": {"sha": "tree-0"}}, {"sha": "tree-1"}, {"sha": "commit-1"}])
        with mock.patch.object(client, "request", side_effect=lambda *a, **k: next(answers)) as request:
            self.assertEqual("commit-1", client.commit_file("f" * 40, c.APPROVED_FILE, "alice\n", "msg"))
        methods = [(call.args[0], call.args[1]) for call in request.call_args_list]
        self.assertEqual([
            ("GET", "repos/%s/git/commits/%s" % (REPO, "f" * 40)),
            ("POST", "repos/%s/git/trees" % REPO),
            ("POST", "repos/%s/git/commits" % REPO),
        ], methods)
        tree = request.call_args_list[1].args[2]
        self.assertEqual({"base_tree": "tree-0", "tree": [{"path": c.APPROVED_FILE, "mode": "100644", "type": "blob", "content": "alice\n"}]}, tree)
        self.assertEqual(["f" * 40], request.call_args_list[2].args[2]["parents"])


class DoctorGateTest(unittest.TestCase):
    def doctor(self, gate: Dict[str, Any]) -> List[str]:
        config = load_shipped_config()
        config["contributor_gate"] = gate
        out = io.StringIO()
        done = mock.Mock(returncode=1, stdout="", stderr="")
        with mock.patch.object(svc, "load_config", return_value=config), \
             mock.patch.object(svc.shutil, "which", return_value="/bin/gh"), \
             mock.patch.object(svc, "run_command", return_value=done), contextlib.redirect_stdout(out):
            svc.run_doctor(argparse.Namespace(config=str(ROOT / "config" / "reviewer.json")))
        return [line for line in out.getvalue().splitlines() if line.startswith("contributor gate:")]

    def test_scope_every_repository_listed_or_none(self) -> None:
        self.assertIn("on for every review repository", self.doctor({"enabled": True})[0])
        self.assertIn("on for acme-corp/public;", self.doctor({"enabled": True, "repositories": ["acme-corp/public"]})[0])
        self.assertIn("on for no repository (repositories is empty)", self.doctor({"enabled": True, "repositories": []})[0])
        self.assertEqual(["contributor gate: off"], self.doctor({"enabled": False}))


class PledgeFileTest(unittest.TestCase):
    def test_the_repositorys_pledge_shows_the_exact_command(self) -> None:
        text = (ROOT / c.PLEDGE_FILE).read_text(encoding="utf-8")
        self.assertIn("@review-of-legends " + c.PLEDGE_SENTENCE, text)
        line = next(line for line in text.splitlines() if line.startswith("@review-of-legends "))
        self.assertEqual(mentions.PLEDGE, mentions.parse_command(line[len("@review-of-legends "):]).kind)


class OnceGateTest(unittest.TestCase):
    """``rol once`` (svc._run_once_locked) applies the same gate; ``--force`` is the operator's override."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state_path = self.tmp / "state.json"
        self.state_path.write_text(json.dumps({"repositories": {REPO: {"reviewed": {}, "baseline_initialized": True}}}), encoding="utf-8")

    def run_once(self, force: bool) -> List[int]:
        reviewed: List[int] = []
        config = {
            "repositories": [REPO],
            "agents": [{"id": i, "prompt_path": "x"} for i in ("a", "b", "c")],
            "agent_runner": {"command": ["true"]},
            "work_dir": str(self.tmp / "work"),
            "state_path": str(self.state_path),
            **GATE,
        }
        class Client(FakeGitHub):
            def __init__(self, repository, *args, **kwargs):
                super().__init__(repository)
                self.files[(c.APPROVED_FILE, "main")] = ("alice\n", "s")
                self.prs[7] = make_pr(7, "mallory", fork=True)
                self.permissions["mallory"] = "read"

            def list_open_prs(self):
                return list(self.prs.values())

        def fake_review_pr(client, config, config_path, pr, state, **kwargs):
            reviewed.append(int(pr["number"]))
            return False

        with mock.patch.object(svc, "GitHubClient", Client), \
             mock.patch.object(svc, "load_config", return_value=config), \
             mock.patch.object(svc, "expand_path", side_effect=lambda v, base=None: Path(str(v))), \
             mock.patch.object(dx, "open_store", return_value=None), \
             mock.patch.object(svc, "review_pr", fake_review_pr), \
             mock.patch.object(svc, "log"):
            svc.run_once(argparse.Namespace(config=str(ROOT / "config" / "reviewer.json"), repo=REPO, pr=7, dry_run=False, force=force))
        return reviewed

    def test_an_explicit_pr_of_an_unapproved_author_is_skipped(self) -> None:
        self.assertEqual([], self.run_once(force=False))

    def test_force_overrides_the_gate(self) -> None:
        self.assertEqual([7], self.run_once(force=True))


if __name__ == "__main__":
    unittest.main()
