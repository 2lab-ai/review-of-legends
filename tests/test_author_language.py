"""The PR author's own settings in review_pr: the review language and the welcome comment.

``@review-of-legends lang <code>`` stores a language for the commenter; every
later review of that person's pull requests asks the outline, the reviewer
agents and the executive summary to write in it, and says so in the posted
review header. With no setting the prompts are unchanged, and a store that
cannot be read costs the language, never the review. The first review of an
author's pull request (first or update) posts one welcome comment; a failed
post records nothing, so the next one tries again, and bots get none.

The prompts are captured where the real ``run_agent``,
``run_executive_summary_agent`` and ``run_visual_outline_agent`` write them;
only the runner subprocess and the clone are stubbed.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import dashboard_execution as dx
from code_review_service import mentions
from code_review_service import service as svc
from code_review_service.dashboard_store import DashboardStore

KOREAN_BLOCK = (
    "Write every human-readable sentence (summaries, finding titles and bodies, verdict sentences, outline text) "
    "in Korean (ko). Keep code, identifiers, file paths, commands and JSON keys unchanged, and every fixed value "
    'the output format names exactly as written, in English: "patch is correct" or "patch is incorrect", severity '
    "(high, medium, low), priority and the scores. This overrides any language the instructions above ask for."
)


BOT = "review-of-legends[bot]"


class Client:
    """GitHub double: records the review, the comments and the welcome."""

    def __init__(self, repo: str = "owner/repo", fail_comment: bool = False, comments: Optional[List[Dict[str, Any]]] = None) -> None:
        self.repo = repo
        self.fail_comment = fail_comment
        self.comments: List[Dict[str, Any]] = list(comments or [])
        self.reviews: List[str] = []
        self.created: List[tuple] = []

    def list_files(self, number: int) -> List[Dict[str, Any]]:
        return [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0, "changes": 1,
                 "patch": "@@ -1,1 +1,2 @@\n context\n+added\n"}]

    def bot_login(self) -> str:
        return BOT

    def list_issue_comments(self, number: int) -> List[Dict[str, Any]]:
        return list(self.comments)

    def create_issue_comment(self, number: int, body: str) -> Dict[str, Any]:
        self.created.append((number, body))
        if self.fail_comment:
            raise svc.ServiceError("GitHub API POST failed: 502")
        self.comments.append({"id": len(self.comments) + 1, "body": body, "user": {"login": BOT, "type": "Bot"}})
        return {"id": 1}

    def upsert_issue_comment(self, number: int, marker: str, body: str) -> Dict[str, Any]:
        return {"id": 2}

    def create_review(self, number, commit_id, body, comments, event="COMMENT"):
        self.reviews.append(body)
        return {"id": 3, "html_url": "https://github.invalid/review"}

    def create_commit_status(self, sha, state, description, target_url="", context=svc.REQUIRED_STATUS_CONTEXT):
        return {"id": 4}

    def welcomes(self) -> List[tuple]:
        return [item for item in self.created if mentions.WELCOME_MARKER in item[1]]


def pull(number: int = 7, login: str = "Alice", user_type: str = "User", head: str = "abc1234567") -> Dict[str, Any]:
    return {
        "number": number,
        "head": {"sha": head, "ref": "feature"},
        "base": {"sha": "b" * 40, "ref": "main"},
        "title": "t",
        "body": "",
        "html_url": "https://github.com/owner/repo/pull/%d" % number,
        "user": {"login": login, "type": user_type},
    }


def config(work_dir: Path) -> Dict[str, Any]:
    return {
        "agents": [{"id": "fake", "enabled": True, "prompt_path": "../agents/correctness.md"}],
        "agent_runner": {"command": ["true"]},
        "min_successful_agents": 0,
        "work_dir": str(work_dir),
        "progress_comment": {"enabled": False},
        "executive_summary": {"enabled": True, "prompt_path": "../agents/executive-summary.md"},
        "visual_outline": {"enabled": True, "prompt_path": "../agents/visual-outline.md"},
    }


class BrokenStore:
    """A store whose author reads fail; nothing else is called on it in these tests."""

    def __init__(self) -> None:
        self.marked: List[tuple] = []

    def get_pr_author(self, login: str) -> Any:
        raise RuntimeError("database disk image is malformed")

    def mark_author_welcomed(self, login: str, welcomed_pr: str) -> Any:
        self.marked.append((login, welcomed_pr))

    def get_setting(self, key: str) -> Any:
        return None

    def start_run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"id": "run-1"}

    def update_run(self, *args: Any, **kwargs: Any) -> None:
        return None


class AuthorSettingsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.store = DashboardStore(self.tmp / "dashboard" / "dashboard.sqlite3")
        # Welcomes left pending by one test are not another's.
        svc.PENDING_WELCOMES.clear()
        self.addCleanup(svc.PENDING_WELCOMES.clear)
        self.prompts: Dict[str, str] = {}
        self.logged: List[str] = []
        for name, replacement in (("log", self.logged.append), ("log_ascii_banner", lambda *args, **kwargs: None)):
            patcher = mock.patch.object(svc, name, side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def review(self, client: Client, pr: Dict[str, Any], store: Any = "real", dry_run: bool = False) -> bool:
        real_write = svc.write_private_text
        prompts = self.prompts

        def capture(path: Path, content: str) -> None:
            if path.name.endswith(".prompt.md"):
                prompts[path.name[: -len(".prompt.md")]] = content
            real_write(path, content)

        def runner(args, timeout, cwd=None, env=None):
            stdout = '{"summary": "ok", "findings": [], "body": "summary body"}'
            return subprocess.CompletedProcess(args, 0, stdout, "")

        recorder = dx.DashboardRecorder(self.store if store == "real" else store, trigger="poll", logger=self.logged.append)
        with mock.patch.object(svc, "write_private_text", capture), mock.patch.object(svc, "run_command", runner), \
                mock.patch.object(svc, "clone_pr_source",
                                  lambda repo, number, head, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True)):
            return svc.review_pr(
                client, config(self.tmp / "work"), ROOT / "config" / "reviewer.json", pr, {"reviewed": {}},
                dry_run=dry_run, recorder=recorder,
            )


class ReviewLanguageTest(AuthorSettingsTestCase):
    def test_the_authors_language_reaches_the_agent_summary_and_outline_prompts(self) -> None:
        self.store.set_author_language("Alice", "ko", "https://github.com/owner/repo/pull/1#issuecomment-1")
        client = Client()
        self.assertTrue(self.review(client, pull(login="alice")))
        self.assertEqual(sorted(self.prompts), ["executive-summary", "fake", "visual-outline"])
        for name, prompt in self.prompts.items():
            with self.subTest(prompt=name):
                self.assertIn(KOREAN_BLOCK, prompt)
                # Last: it overrides any language the instructions above ask for.
                self.assertTrue(prompt.rstrip().endswith("This overrides any language the instructions above ask for."))
        self.assertIn("Language: ko (set by @alice)", client.reviews[0])
        self.assertIn("owner/repo pr #7: writing the review in ko, set by alice", self.logged)

    def test_the_header_line_sits_with_the_review_metadata(self) -> None:
        self.store.set_author_language("Alice", "zh-tw")
        client = Client()
        self.review(client, pull())
        header = client.reviews[0].split("### Agent summaries")[0]
        self.assertIn("Line comments: 0/0  \nLanguage: zh-tw (set by @Alice)", header)
        self.assertIn("in Traditional Chinese (zh-tw).", self.prompts["fake"])

    def test_no_setting_means_no_block_and_no_header_line(self) -> None:
        self.store.mark_author_welcomed("Alice", "owner/repo#1")
        client = Client()
        self.assertTrue(self.review(client, pull()))
        self.assertEqual(sorted(self.prompts), ["executive-summary", "fake", "visual-outline"])
        for name, prompt in self.prompts.items():
            with self.subTest(prompt=name):
                self.assertNotIn("Output language", prompt)
                self.assertNotIn("overrides any language", prompt)
        self.assertNotIn("Language:", client.reviews[0])

    def test_without_a_language_the_prompts_are_exactly_what_they_were(self) -> None:
        pr = pull()
        self.assertEqual(
            svc.render_prompt("owner/repo", pr, [], "diff", "agent"),
            svc.render_prompt("owner/repo", pr, [], "diff", "agent", language=""),
        )
        self.assertTrue(svc.render_prompt("owner/repo", pr, [], "diff", "agent").endswith("no preamble, no plan, no markdown.\n"))

    def test_a_store_that_cannot_be_read_costs_the_language_not_the_review(self) -> None:
        store = BrokenStore()
        client = Client()
        self.assertTrue(self.review(client, pull(), store=store))
        self.assertEqual(len(client.reviews), 1)
        self.assertNotIn("Language:", client.reviews[0])
        self.assertNotIn("Output language", self.prompts["fake"])
        self.assertEqual(sum("settings of PR author Alice unreadable" in line for line in self.logged), 1)
        self.assertEqual(client.welcomes(), [], "a welcome that cannot be recorded is not posted")
        self.assertEqual(store.marked, [])

    def test_no_store_at_all(self) -> None:
        client = Client()
        self.assertTrue(self.review(client, pull(), store=None))
        self.assertEqual(client.welcomes(), [])
        self.assertNotIn("Language:", client.reviews[0])


class WelcomeCommentTest(AuthorSettingsTestCase):
    def test_posted_once_per_author_on_the_first_review_then_never(self) -> None:
        client = Client()
        self.review(client, pull(number=7))
        [(number, body)] = client.welcomes()
        self.assertEqual(number, 7)
        self.assertTrue(body.startswith(mentions.WELCOME_MARKER))
        self.assertIn("@Alice", body)
        self.assertIn("안녕하세요", body, "Korean unless the author or the config says otherwise")
        row = self.store.get_pr_author("alice")
        self.assertEqual(row["welcomed_pr"], "owner/repo#7")
        self.assertTrue(row["welcomed_at"])
        # An update of the same PR, and another PR of the same author: nothing more.
        self.review(client, pull(number=7, head="def"))
        other = Client()
        self.review(other, pull(number=8, login="ALICE"))
        self.assertEqual(len(client.welcomes()), 1)
        self.assertEqual(other.welcomes(), [])
        # Another author gets their own.
        bob = Client()
        self.review(bob, pull(number=9, login="Bob"))
        self.assertEqual(len(bob.welcomes()), 1)

    def test_an_existing_author_with_a_language_is_welcomed_in_it(self) -> None:
        self.store.set_author_language("Alice", "en")
        client = Client()
        self.review(client, pull())
        self.assertIn("Hi @Alice", client.welcomes()[0][1])

    def test_a_failed_post_records_nothing_and_the_next_review_tries_again(self) -> None:
        failing = Client(fail_comment=True)
        self.assertTrue(self.review(failing, pull(number=7)), "the welcome never costs the review")
        self.assertEqual(len(failing.welcomes()), 1)
        self.assertEqual(len(failing.reviews), 1)
        self.assertIsNone(self.store.get_pr_author("alice"))
        self.assertTrue(any("no welcome comment for Alice" in line for line in self.logged))
        # A 502 may still have been applied: the next review waits until GitHub
        # has surely settled, finds no welcome on PR 7, and posts one on PR 8.
        soon = Client()
        self.review(soon, pull(number=8))
        self.assertEqual(soon.welcomes(), [], "too soon to tell")
        client = Client()
        later = svc._welcome_now() + svc.WELCOME_SETTLE_SECONDS + 1
        with mock.patch.object(svc, "_welcome_now", return_value=later):
            self.review(client, pull(number=8, head="def"))
        self.assertEqual(len(client.welcomes()), 1)
        self.assertEqual(self.store.get_pr_author("alice")["welcomed_pr"], "owner/repo#8")

    def test_a_welcome_already_on_the_pr_is_recorded_not_posted_again(self) -> None:
        # Posted by an earlier attempt whose record failed (the login's case may differ).
        own = {"id": 1, "body": mentions.welcome_body("Alice", "ko"), "user": {"login": "Review-Of-Legends[bot]"}}
        client = Client(comments=[own])
        self.review(client, pull())
        self.assertEqual(client.welcomes(), [])
        self.assertEqual(self.store.get_pr_author("alice")["welcomed_pr"], "owner/repo#7")

    def test_a_marker_pasted_by_someone_else_does_not_count_as_the_welcome(self) -> None:
        pasted = {"id": 1, "body": "> " + mentions.welcome_body("Alice", "en"), "user": {"login": "Alice", "type": "User"}}
        anonymous = {"id": 2, "body": mentions.WELCOME_MARKER, "user": None}
        client = Client(comments=[pasted, anonymous])
        self.review(client, pull())
        self.assertEqual(len(client.welcomes()), 1)
        self.assertEqual(self.store.get_pr_author("alice")["welcomed_pr"], "owner/repo#7")

    def test_a_marker_quoted_in_the_services_own_reply_is_not_a_welcome(self) -> None:
        # Anyone can make the service echo marker text: a reply quotes the command.
        request = mentions.Request(kind=mentions.ISSUE, comment_id=3, number=7, login="Mallory", url="u",
                                   command=mentions.Command(mentions.FEEDBACK, "x"), line=mentions.WELCOME_MARKER)
        echo = {"id": 3, "body": mentions.reply_body(request, None, "en"), "user": {"login": BOT, "type": "Bot"}}
        self.assertIn(mentions.WELCOME_MARKER, echo["body"])
        client = Client(comments=[echo])
        self.review(client, pull())
        self.assertEqual(len(client.welcomes()), 1, "the real welcome is posted")

    def test_bots_are_skipped(self) -> None:
        for login, user_type in (("dependabot[bot]", "Bot"), ("renovate", "Bot"), ("review-of-legends[bot]", "Bot")):
            with self.subTest(login=login):
                client = Client()
                self.assertTrue(self.review(client, pull(login=login, user_type=user_type)))
                self.assertEqual(client.welcomes(), [])
        self.assertEqual(self.store.list_pr_authors(), [])
        self.assertFalse(any("unreadable" in line for line in self.logged), "a bot login is never looked up")

    def test_a_dry_run_posts_nothing(self) -> None:
        client = Client()
        with mock.patch("builtins.print"):
            self.review(client, pull(), dry_run=True)
        self.assertEqual(client.welcomes(), [])

    def test_a_welcome_whose_record_failed_is_recorded_later_never_posted_again(self) -> None:
        # The POST went through but the store could not record it: the next PR
        # of the same author records that first welcome instead of posting another.
        recorder = dx.DashboardRecorder(self.store, logger=self.logged.append)
        first = Client()
        with mock.patch.object(self.store, "mark_author_welcomed", side_effect=RuntimeError("database is locked")):
            svc.post_welcome_comment(first, {}, pull(number=7), "Alice", recorder)
        self.assertEqual(len(first.welcomes()), 1)
        self.assertIsNone(self.store.get_pr_author("alice"))
        second = Client()
        svc.post_welcome_comment(second, {}, pull(number=8), "ALICE", recorder)
        self.assertEqual(second.welcomes(), [], "one welcome per author")
        self.assertEqual(self.store.get_pr_author("alice")["welcomed_pr"], "owner/repo#7")

    def test_a_welcome_whose_post_outcome_is_unknown_is_looked_for_before_another(self) -> None:
        # The POST reached GitHub but its answer was lost (a timeout): the next
        # PR first looks on the earlier one, records it if it is there, and
        # posts only if it is not.
        class Lost(Client):
            def create_issue_comment(self, number: int, body: str) -> Dict[str, Any]:
                super().create_issue_comment(number, body)
                error = svc.ServiceError("GitHub API POST failed: <urlopen error timed out>")
                error.transport_error = True  # type: ignore[attr-defined]
                raise error

        recorder = dx.DashboardRecorder(self.store, logger=self.logged.append)
        lost = Lost()
        svc.post_welcome_comment(lost, {}, pull(number=7), "Alice", recorder)
        self.assertEqual(len(lost.welcomes()), 1)
        self.assertIsNone(self.store.get_pr_author("alice"))
        second = Client(comments=[])  # GitHub may still be committing the first POST: nothing listed yet
        svc.post_welcome_comment(second, {}, pull(number=8), "Alice", recorder)
        self.assertEqual(second.welcomes(), [], "too soon to tell: nothing posted")
        self.assertEqual(svc.PENDING_WELCOMES["alice"][:2], ("owner/repo#7", False), "still waiting")
        later = svc._welcome_now() + svc.WELCOME_SETTLE_SECONDS + 1
        third = Client(comments=lost.comments)  # the same repository: PR 7's comments are readable
        with mock.patch.object(svc, "_welcome_now", return_value=later):
            svc.post_welcome_comment(third, {}, pull(number=8), "Alice", recorder)
        self.assertEqual(third.welcomes(), [], "found on PR 7: not posted again")
        self.assertEqual(self.store.get_pr_author("alice")["welcomed_pr"], "owner/repo#7")

    def test_with_the_pending_welcomes_full_no_new_welcome_is_posted(self) -> None:
        # The store has refused that many records: a pending one is never
        # dropped to make room, and a new welcome could not be recorded either.
        for index in range(svc.MAX_PENDING_WELCOMES):
            svc.PENDING_WELCOMES["user%d" % index] = ("owner/repo#%d" % index, True, 0.0)
        client = Client()
        svc.post_welcome_comment(client, {}, pull(number=9), "Alice", dx.DashboardRecorder(self.store))
        self.assertEqual(client.welcomes(), [])
        self.assertEqual(len(svc.PENDING_WELCOMES), svc.MAX_PENDING_WELCOMES)
        self.assertIn("user0", svc.PENDING_WELCOMES)

    def test_an_unknown_own_login_keeps_an_unconfirmed_welcome_pending(self) -> None:
        # Without the service's own login no comment can be told to be its
        # welcome: that is an unreadable check, not "no welcome there".
        recorder = dx.DashboardRecorder(self.store, logger=self.logged.append)
        svc.PENDING_WELCOMES["alice"] = ("owner/repo#7", False, 0.0)

        class NoLogin(Client):
            def bot_login(self) -> str:
                return ""

        client = NoLogin()
        svc.post_welcome_comment(client, {}, pull(number=8), "Alice", recorder)
        self.assertEqual(client.welcomes(), [], "no second welcome")
        self.assertEqual(svc.PENDING_WELCOMES.get("alice"), ("owner/repo#7", False, 0.0), "still pending")
        self.assertIsNone(self.store.get_pr_author("alice"))

    def test_one_authors_welcome_does_not_wait_for_anothers(self) -> None:
        # The lock is per author: a slow listing for Bob never holds Alice's welcome.
        entered, release = threading.Event(), threading.Event()

        class Slow(Client):
            def list_issue_comments(self, number: int) -> List[Dict[str, Any]]:
                entered.set()
                release.wait(10)
                return []

        recorder = dx.DashboardRecorder(self.store)
        bob = threading.Thread(target=svc.post_welcome_comment, args=(Slow(), {}, pull(number=9, login="Bob"), "Bob", recorder))
        bob.start()
        self.addCleanup(bob.join, 10)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(5))
        alice = Client()
        done = threading.Event()
        threading.Thread(target=lambda: (svc.post_welcome_comment(alice, {}, pull(number=7), "Alice", recorder), done.set())).start()
        self.assertTrue(done.wait(2), "Alice's welcome went up while Bob's listing was slow")
        self.assertEqual(len(alice.welcomes()), 1)

    def test_two_reviews_of_one_new_author_at_once_post_one_welcome(self) -> None:
        clients = [Client(), Client()]
        started = threading.Barrier(2)

        def run(client: Client, number: int) -> None:
            started.wait(5)
            svc.post_welcome_comment(client, {}, pull(number=number), "Alice", dx.DashboardRecorder(self.store))

        threads = [threading.Thread(target=run, args=(client, 7 + index)) for index, client in enumerate(clients)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(sum(len(client.welcomes()) for client in clients), 1)


if __name__ == "__main__":
    unittest.main()
