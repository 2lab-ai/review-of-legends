"""The contributor gate at work (gate.py) and in the scheduler.

A fake GitHub client answers the gate's reads and records its writes; the store
is the real SQLite one. Scheduler tests reuse test_scheduler's harness with a
watch client that also answers the gate's calls.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import contributors as c
from code_review_service import gate as gate_mod
from code_review_service import mentions
from code_review_service import service as svc
from code_review_service.dashboard_store import DashboardStore
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
        self.prs: Dict[int, Dict[str, Any]] = {}
        self.pulls: Dict[str, Dict[str, Any]] = {}
        self._next_id = 100

    # reads
    def bot_login(self) -> str:
        return BOT

    def get_file(self, path: str, ref: str):
        return self.files.get((path, ref))

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
        return "f" * 40

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
        self.writes.append(("approve", run_id))

    def create_branch(self, branch: str, sha: str):
        if self.branch_error is not None:
            raise self.branch_error
        # GitHub's new branch holds the default branch's files.
        for (path, ref), value in list(self.files.items()):
            if ref == "main":
                self.files[(path, branch)] = value
        self.writes.append(("branch", branch, sha))

    def update_branch(self, branch: str, sha: str):
        for (path, ref), value in list(self.files.items()):
            if ref == "main":
                self.files[(path, branch)] = value
        self.writes.append(("reset", branch, sha))

    def put_file(self, path: str, branch: str, text: str, message: str, sha: str = ""):
        self.files[(path, branch)] = (text, "new-sha")
        self.writes.append(("put", path, branch, text, sha))

    def create_pull(self, head: str, base: str, title: str, body: str):
        pull = {"html_url": "https://github.com/%s/pull/77" % self.repo, "head": head, "body": body}
        self.pulls[head] = pull
        self.writes.append(("pull", head, base, title))
        return pull


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
        self.assertIn("already used", self.answer(1, "mallory", "verify " + code))  # used: refused
        self.assertIsNotNone(self.store.contributor_identity("Mallory"))

    def test_a_code_read_off_a_public_comment_is_worthless_to_someone_else(self) -> None:
        code = c.new_verify_code()
        self.store.create_contributor_code("mallory@company.example", c.code_hash(code), "mallory")
        self.answer(1, "eve", "verify " + code)
        self.assertIsNone(self.store.contributor_identity("eve"))
        # Not used up by eve: mallory still can.
        self.assertIn("now reviewed", self.answer(1, "mallory", "verify " + code))

    def test_verify_with_an_unknown_code(self) -> None:
        reply = self.answer(1, "mallory", "verify rol-00000000")
        self.assertIsNone(self.store.contributor_identity("mallory"))
        self.assertTrue(reply.startswith(mentions.REPLY_MARKER))

    def test_pledge_opens_the_pull_request_that_adds_the_login(self) -> None:
        reply = self.answer(3, "mallory", c.PLEDGE_SENTENCE)
        self.assertIn("https://github.com/%s/pull/77" % REPO, reply)
        branch = gate_mod.PLEDGE_BRANCH_PREFIX + "mallory"
        self.assertEqual(("branch", branch, "f" * 40), self.gh.writes[0])
        put = [w for w in self.gh.writes if w[0] == "put"][0]
        self.assertEqual("alice\n*@example.com\nmallory  # pledge signed in %s#3\n" % REPO, put[3])
        self.assertEqual("list-sha", put[4])
        pull = self.gh.pulls[branch]
        self.assertIn("pledge-sha", pull["body"])
        self.assertIn("5003", pull["body"])

    def test_a_branch_left_from_a_declined_pull_request_starts_again_from_main(self) -> None:
        self.gh.branch_error = status_error(422)
        self.answer(3, "mallory", c.PLEDGE_SENTENCE)
        self.assertIn(("reset", gate_mod.PLEDGE_BRANCH_PREFIX + "mallory", "f" * 40), self.gh.writes)
        self.assertIn(gate_mod.PLEDGE_BRANCH_PREFIX + "mallory", self.gh.pulls)
        # Signing again (another comment) points at the same pull request, nothing new.
        writes = len(self.gh.writes)
        again = self.keeper.handle_command(self.gh, self.store, self.config, REPO, request(3, "mallory", c.PLEDGE_SENTENCE)._replace(comment_id=999))
        self.assertIn("pull/77", again)
        self.assertEqual(writes, len(self.gh.writes))

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
                            "list_waiting_runs", "approve_run"):
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
        self.assertIn("approved contributor", self.listing.gate.comments[9][0]["body"])

    def test_gate_off_queues_everyone_without_any_gate_call(self) -> None:
        scheduler = self.make()
        scheduler.control()
        self.listing.prs[REPO] = [self.authored(9, "c" * 40, "mallory")]
        scheduler.tick()
        self.assertEqual([9], sorted(number for _, number in scheduler._queue))
        self.assertEqual([], self.listing.gate.writes)
        self.assertEqual([], self.listing.gate.permission_reads)


if __name__ == "__main__":
    unittest.main()
