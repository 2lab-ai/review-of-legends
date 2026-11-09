"""``@review-of-legends`` commands (code_review_service/mentions.py).

GitHub never tells the App's bot it was mentioned, so the daemon reads PR
comments itself. These tests pin what counts as a command, what each command
does in the store, how it is acknowledged on GitHub (a reaction plus a reply,
on both comment endpoints), which language the reply is in, and the GitHub
client calls the poller makes. The poller itself is tested in test_scheduler.py.
"""

from __future__ import annotations

import email.message
import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import mentions as m
from code_review_service import service as svc
from code_review_service.dashboard_store import DashboardStore

REPO = "owner/repo"


def issue_comment(comment_id: int, body: str, login: str = "Alice", number: int = 7, **extra: Any) -> Dict[str, Any]:
    comment = {
        "id": comment_id,
        "body": body,
        "user": {"login": login, "type": "User"},
        "html_url": "https://github.com/%s/pull/%d#issuecomment-%d" % (REPO, number, comment_id),
        "issue_url": "https://api.github.com/repos/%s/issues/%d" % (REPO, number),
        "created_at": "2026-10-15T01:00:00Z",
        "updated_at": "2026-10-15T01:00:00Z",
        "node_id": "IC_%d" % comment_id,
    }
    comment.update(extra)
    return comment


def review_comment(comment_id: int, body: str, login: str = "Alice", number: int = 7) -> Dict[str, Any]:
    return {
        "id": comment_id,
        "body": body,
        "user": {"login": login, "type": "User"},
        "html_url": "https://github.com/%s/pull/%d#discussion_r%d" % (REPO, number, comment_id),
        "pull_request_url": "https://api.github.com/repos/%s/pulls/%d" % (REPO, number),
        "created_at": "2026-10-15T01:00:00Z",
        "updated_at": "2026-10-15T01:00:00Z",
    }


class FindMentionTest(unittest.TestCase):
    """A command is a comment whose first non-blank line starts with the mention (COMMAND_LINE)."""

    def test_the_rest_of_the_first_line_is_the_command(self) -> None:
        self.assertEqual(m.find_mention("@review-of-legends lang ko"), ("lang ko", "lang ko"))
        self.assertEqual(m.find_mention("@review-of-legends   settings  ")[0], "settings")
        self.assertEqual(m.find_mention("@review-of-legends: lang en")[0], "lang en")
        self.assertEqual(m.find_mention("\n\n   @review-of-legends lang ja\n")[0], "lang ja", "blank lines and 3 spaces")
        self.assertEqual(m.find_mention("@review-of-legends the `foo` check is noisy"),
                         ("the `foo` check is noisy", "the `foo` check is noisy"), "the line as typed")

    def test_case_and_the_bot_suffix_do_not_matter(self) -> None:
        for body in ("@Review-Of-Legends lang ko", "@REVIEW-OF-LEGENDS lang ko", "@review-of-legends[bot] lang ko",
                     "@Review-of-Legends[BOT] lang ko"):
            with self.subTest(body=body):
                self.assertEqual(m.find_mention(body)[0], "lang ko")

    def test_only_the_whole_token_counts(self) -> None:
        for body in ("@review-of-legends-dev lang ko", "@review-of-legend lang ko", "review-of-legends lang ko",
                     "@review-of-legendsx", "@review-of-legends_x lang ko"):
            with self.subTest(body=body):
                self.assertIsNone(m.find_mention(body))

    def test_a_mention_anywhere_but_the_start_of_the_comment_never_counts(self) -> None:
        # Whatever Markdown makes of the rest, the first line decides: an
        # example in code, a quote, a list or the middle of a comment is never a command.
        for body in (
            "thanks! @review-of-legends lang ko",
            "Context first.\n@review-of-legends lang ko",
            "> @review-of-legends lang ko",
            "> Example command:\n@review-of-legends lang en",
            "```\n@review-of-legends lang ko\n```",
            "~~~\n@review-of-legends lang ko\n~~~",
            "    @review-of-legends lang ko",
            "\t@review-of-legends lang ko",
            "- @review-of-legends lang ko",
            "1. > @review-of-legends lang ko",
            "`@review-of-legends lang ko`",
            "\\@review-of-legends lang ko",
            "<!-- x -->\n@review-of-legends lang ko",
            "mail me at x@review-of-legends.io",
            "\u00a0@review-of-legends lang ko",
        ):
            with self.subTest(body=body):
                self.assertIsNone(m.find_mention(body))

    def test_the_bots_own_texts_never_start_with_a_command(self) -> None:
        for language in ("ko", "en"):
            with self.subTest(language=language):
                self.assertIsNone(m.find_mention(m.welcome_body("Alice", language)))
                quoted = "\n".join("> " + line for line in m.welcome_body("Alice", language).splitlines())
                self.assertIsNone(m.find_mention(quoted))

    def test_only_commonmark_line_endings_and_blank_lines_count(self) -> None:
        # U+2028 is no line ending on GitHub: this is one indented code line.
        self.assertIsNone(m.find_mention("    \u2028@review-of-legends lang en"))
        self.assertIsNone(m.find_mention("\u00a0\n@review-of-legends lang en"), "U+00A0 is not blank")
        self.assertEqual(m.find_mention(" \t\r\n@review-of-legends lang en")[0], "lang en")

    def test_feedback_text_runs_to_the_end_of_the_comment(self) -> None:
        body = "@review-of-legends the summary was too long\nand it missed the migration."
        self.assertEqual(
            m.find_mention(body),
            ("the summary was too long", "the summary was too long\nand it missed the migration."),
        )

    def test_not_a_string_or_empty(self) -> None:
        self.assertIsNone(m.find_mention(None))
        self.assertIsNone(m.find_mention("  \n\n"))


class EditedByOthersTest(unittest.TestCase):
    def test_only_the_authors_own_edits_keep_a_command(self) -> None:
        self.assertFalse(m.edited_by_others({"edited": False, "editors": [], "complete": True}, "Alice"))
        self.assertFalse(m.edited_by_others({"edited": True, "editors": ["alice", "Alice"], "complete": True}, "Alice"))
        # Every edit counts, not only the last: a collaborator's edit the author edited again.
        self.assertTrue(m.edited_by_others({"edited": True, "editors": ["alice", "mallory", "alice"], "complete": True},
                                           "Alice"))
        self.assertTrue(m.edited_by_others({"edited": True, "editors": ["alice", None], "complete": True}, "Alice"),
                        "a deleted account")
        self.assertTrue(m.edited_by_others({"edited": True, "editors": ["alice"], "complete": False}, "Alice"),
                        "a history cut short")
        self.assertTrue(m.edited_by_others(None, "Alice"), "no answer: someone else, never a guess")


class ParseCommandTest(unittest.TestCase):
    def test_lang_codes_are_stored_lowercase(self) -> None:
        self.assertEqual(m.parse_command("lang ko"), m.Command(m.LANG, "ko"))
        self.assertEqual(m.parse_command("LANG JA"), m.Command(m.LANG, "ja"))
        self.assertEqual(m.parse_command("lang zh-TW"), m.Command(m.LANG, "zh-tw"))
        self.assertEqual(m.parse_command("lang pt-br"), m.Command(m.LANG, "pt-br"))
        self.assertEqual(m.parse_command("lang fil"), m.Command(m.LANG, "fil"))

    def test_aliases(self) -> None:
        for value, code in (("korean", "ko"), ("한국어", "ko"), ("KR", "ko"), ("English", "en"), ("영어", "en"),
                            ("japanese", "ja"), ("일본어", "ja"), ("chinese", "zh"), ("중국어", "zh")):
            with self.subTest(value=value):
                self.assertEqual(m.parse_command("lang " + value), m.Command(m.LANG, code))

    def test_invalid_codes_change_nothing(self) -> None:
        for value in ("k", "korea", "zh_TW", "en-", "en-x", "ko ja", "12", "en-abcdefghi"):
            with self.subTest(value=value):
                self.assertEqual(m.parse_command("lang " + value), m.Command(m.LANG_INVALID, value))

    def test_off_default_and_reset_clear_it(self) -> None:
        for value in ("off", "default", "reset", "OFF"):
            with self.subTest(value=value):
                self.assertEqual(m.parse_command("lang " + value), m.Command(m.LANG_CLEAR))

    def test_lang_alone_shows_it(self) -> None:
        self.assertEqual(m.parse_command("lang"), m.Command(m.LANG_SHOW))
        self.assertEqual(m.parse_command("  Lang  "), m.Command(m.LANG_SHOW))

    def test_settings_help_config_and_an_empty_mention(self) -> None:
        for command in ("settings", "help", "config", "Settings?", "HELP", ""):
            with self.subTest(command=command):
                self.assertEqual(m.parse_command(command), m.Command(m.SETTINGS))

    def test_anything_else_is_feedback(self) -> None:
        self.assertEqual(m.parse_command("the review missed a bug"), m.Command(m.FEEDBACK, "the review missed a bug"))
        self.assertEqual(m.parse_command("help me with this", "help me with this\nmore"),
                         m.Command(m.FEEDBACK, "help me with this\nmore"))
        self.assertEqual(m.parse_command("language ko").kind, m.FEEDBACK)
        self.assertEqual(len(m.parse_command("x", "y" * 5000).value), m.MAX_FEEDBACK_CHARS)


class RequestFromTest(unittest.TestCase):
    BOT = "review-of-legends[bot]"

    def test_a_pr_issue_comment_and_a_review_comment(self) -> None:
        request = m.request_from(issue_comment(11, "@review-of-legends lang ko"), m.ISSUE, self.BOT)
        self.assertEqual((request.kind, request.comment_id, request.number, request.login), (m.ISSUE, 11, 7, "Alice"))
        self.assertEqual(request.command, m.Command(m.LANG, "ko"))
        self.assertEqual(request.url, "https://github.com/owner/repo/pull/7#issuecomment-11")
        review = m.request_from(review_comment(12, "@review-of-legends settings", number=9), m.REVIEW, self.BOT)
        self.assertEqual((review.kind, review.number, review.command.kind), (m.REVIEW, 9, m.SETTINGS))

    def test_comments_on_plain_issues_are_skipped(self) -> None:
        comment = issue_comment(11, "@review-of-legends lang ko", html_url="https://github.com/owner/repo/issues/7#issuecomment-11")
        self.assertIsNone(m.request_from(comment, m.ISSUE, self.BOT))

    def test_the_bot_and_bot_accounts_are_skipped(self) -> None:
        own = issue_comment(1, "@review-of-legends lang ko", login=self.BOT)
        own["user"]["type"] = "Bot"
        self.assertIsNone(m.request_from(own, m.ISSUE, self.BOT))
        gh_identity = issue_comment(2, "@review-of-legends lang ko", login="review-service")
        self.assertIsNone(m.request_from(gh_identity, m.ISSUE, "Review-Service"), "the gh login, any case")
        other_bot = issue_comment(3, "@review-of-legends lang ko", login="dependabot[bot]")
        self.assertIsNone(m.request_from(other_bot, m.ISSUE, self.BOT))
        typed_bot = issue_comment(4, "@review-of-legends lang ko", login="ci-helper")
        typed_bot["user"]["type"] = "Bot"
        self.assertIsNone(m.request_from(typed_bot, m.ISSUE, self.BOT))
        no_user = issue_comment(5, "@review-of-legends lang ko")
        no_user["user"] = None
        self.assertIsNone(m.request_from(no_user, m.ISSUE, self.BOT))

    def test_the_bots_own_texts_never_count_even_from_a_user_account(self) -> None:
        for body in (m.welcome_body("Alice", "ko"), m.REPLY_MARKER + "\n@review-of-legends lang ko"):
            with self.subTest(body=body[:40]):
                self.assertIsNone(m.request_from(issue_comment(6, body), m.ISSUE, ""))

    def test_feedback_that_quotes_the_bots_own_text_is_still_a_command(self) -> None:
        body = "@review-of-legends the welcome came twice:\n\n" + m.welcome_body("Alice", "en")
        request = m.request_from(issue_comment(9, body), m.ISSUE, "review-of-legends[bot]")
        self.assertEqual(request.command.kind, m.FEEDBACK)

    def test_an_issue_in_a_repository_named_pull_is_not_a_pr(self) -> None:
        plain = issue_comment(4, "@review-of-legends lang ko", html_url="https://github.com/owner/pull/issues/7#issuecomment-4",
                              issue_url="https://api.github.com/repos/owner/pull/issues/7")
        self.assertIsNone(m.request_from(plain, m.ISSUE, "review-of-legends[bot]"))
        pull = issue_comment(5, "@review-of-legends lang ko", html_url="https://github.com/owner/pull/pull/7#issuecomment-5")
        self.assertEqual(m.request_from(pull, m.ISSUE, "review-of-legends[bot]").number, 7)

    def test_a_comment_without_a_mention_is_not_a_request(self) -> None:
        self.assertIsNone(m.request_from(issue_comment(7, "LGTM"), m.ISSUE, self.BOT))
        self.assertIsNone(m.request_from({"id": "x"}, m.ISSUE, self.BOT))


class FakeClient:
    def __init__(self, fail_reaction: bool = False, fail_reply: bool = False, message: str = "refused") -> None:
        self.calls: List[tuple] = []
        self.fail_reaction = fail_reaction
        self.fail_reply = fail_reply
        self.message = message

    def react_to_issue_comment(self, comment_id: int, content: str) -> None:
        self.calls.append(("react-issue", comment_id, content))
        if self.fail_reaction:
            raise svc.ServiceError("reaction " + self.message)

    def react_to_review_comment(self, comment_id: int, content: str) -> None:
        self.calls.append(("react-review", comment_id, content))
        if self.fail_reaction:
            raise svc.ServiceError("reaction " + self.message)

    def create_issue_comment(self, number: int, body: str) -> None:
        self.calls.append(("comment", number, body))
        if self.fail_reply:
            raise svc.ServiceError("reply " + self.message)

    def reply_to_review_comment(self, number: int, comment_id: int, body: str) -> None:
        self.calls.append(("reply", number, comment_id, body))
        if self.fail_reply:
            raise svc.ServiceError("reply " + self.message)


class CursorTest(unittest.TestCase):
    """The comment cursor (mentions.py): what is pending, and the one order of both kinds."""

    @staticmethod
    def item(comment_id: int, updated: str) -> Dict[str, Any]:
        return {"id": comment_id, "updated_at": updated, "created_at": updated}

    def test_the_cursor_moves_through_every_comment_and_keeps_its_own_seconds_ids(self) -> None:
        cursor = {"since": "T1", "floor": "T0", "at_since": []}
        self.assertEqual([c["id"] for c in m.pending(cursor, [self.item(2, "T2"), self.item(1, "T1")])], [1, 2])
        m.advance(cursor, self.item(1, "T1"))
        self.assertEqual((cursor["since"], [entry[0] for entry in cursor["at_since"]]), ("T1", [1]))
        m.advance(cursor, self.item(2, "T2"))
        self.assertEqual((cursor["since"], [entry[0] for entry in cursor["at_since"]]), ("T2", [2]))
        # ?since= is inclusive: the second's own comments come back, and are not pending again.
        self.assertEqual(m.pending(cursor, [self.item(2, "T2")]), [])
        # An edit brings a comment back: pending (the cursor moves through it), never a command.
        edited = {"id": 2, "updated_at": "T3", "created_at": "T2"}
        self.assertEqual(m.pending(cursor, [edited]), [edited])

    def test_one_order_for_both_kinds_that_stops_where_a_listing_was_cut_short(self) -> None:
        cursor = {"since": "T0", "floor": "T0", "at_since": []}
        issues = [self.item(1, "T1"), self.item(3, "T3"), self.item(5, "T5")]
        reviews = svc.CommentListing([self.item(2, "T2"), self.item(4, "T3")])
        self.assertEqual([(k, c["id"]) for k, c in m.merged({m.ISSUE: (cursor, issues), m.REVIEW: (cursor, list(reviews))})],
                         [(m.ISSUE, 1), (m.REVIEW, 2), (m.ISSUE, 3), (m.REVIEW, 4), (m.ISSUE, 5)])
        reviews.truncated = True  # GitHub had more reviews after T3: none of them listed yet
        self.assertEqual([(k, c["id"]) for k, c in m.merged({m.ISSUE: (cursor, issues), m.REVIEW: (cursor, reviews)})],
                         [(m.ISSUE, 1), (m.REVIEW, 2), (m.REVIEW, 4)],
                         "the cut listing's own comments to its last; the other kind only before that second")


class HandlerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = DashboardStore(Path(self._tmp.name) / "dashboard.sqlite3")
        self.logged: List[str] = []

    def answer(self, comment: Dict[str, Any], kind: str = m.ISSUE, config: Dict[str, Any] | None = None, client: Any = None):
        request = m.request_from(comment, kind, "review-of-legends[bot]")
        body = m.apply_command(self.store, REPO, request, config or {})
        client = client or FakeClient()
        m.acknowledge(client, request, body, self.logged.append, REPO)
        return request, body, client

    def test_lang_sets_and_clears_the_commenters_own_setting(self) -> None:
        self.answer(issue_comment(1, "@review-of-legends lang ko", login="Alice"))
        row = self.store.get_pr_author("alice")
        self.assertEqual((row["login"], row["display_login"], row["lang"]), ("alice", "Alice", "ko"))
        self.assertEqual(row["lang_comment_url"], "https://github.com/owner/repo/pull/7#issuecomment-1")
        self.answer(issue_comment(2, "@review-of-legends lang off", login="ALICE"))
        self.assertIsNone(self.store.get_pr_author("Alice")["lang"])

    def test_an_older_language_command_changes_nothing_and_says_what_stands(self) -> None:
        newer = issue_comment(2, "@review-of-legends lang en", login="Alice",
                              created_at="2026-10-15T01:00:05Z", updated_at="2026-10-15T01:00:05Z")
        older = issue_comment(1, "@review-of-legends lang ko", login="Alice")
        self.answer(newer)
        _, body, _ = self.answer(older, config={"mentions": {"default_reply_language": "en"}})
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "en")
        self.assertIn("a newer language command of yours already applies", body)
        self.assertIn("`en` (English)", body)
        # The reply is in the language that stands.
        bob_newer = issue_comment(4, "@review-of-legends lang ko", login="Bob",
                                  created_at="2026-10-15T01:00:05Z", updated_at="2026-10-15T01:00:05Z")
        bob_older = issue_comment(3, "@review-of-legends lang off", login="Bob")
        self.answer(bob_newer)
        _, body, _ = self.answer(bob_older)
        self.assertEqual(self.store.get_pr_author("bob")["lang"], "ko", "an older clear changes nothing")
        self.assertIn("더 최근에 남긴 언어 명령", body)

    def test_commands_of_one_second_end_the_same_way_whatever_order_they_are_applied_in(self) -> None:
        # Second-precision times tie: the comment's identity breaks the tie, so a
        # replay (a cursor that could not be saved) never undoes a later answer.
        first = issue_comment(1, "@review-of-legends lang ko", login="Alice")
        second = issue_comment(2, "@review-of-legends lang en", login="Alice")
        self.answer(second)
        self.answer(first)
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "en")
        self.answer(second)
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "en", "a replay of the winner changes nothing")
        other = DashboardStore(Path(self._tmp.name) / "other.sqlite3")
        for comment in (first, second):
            m.apply_command(other, REPO, m.request_from(comment, m.ISSUE, "review-of-legends[bot]"), {})
        self.assertEqual(other.get_pr_author("alice")["lang"], "en", "the other order ends the same")

    def test_an_invalid_code_and_a_show_change_nothing(self) -> None:
        self.answer(issue_comment(1, "@review-of-legends lang ko"))
        _, body, _ = self.answer(issue_comment(2, "@review-of-legends lang klingon-language"))
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "ko")
        self.assertIn("`ko`", body)
        _, body, _ = self.answer(issue_comment(3, "@review-of-legends lang"))
        self.assertIn("`ko` (Korean)", body)
        self.assertEqual([event["kind"] for event in self.store.events_after(0)], ["author_updated"])

    def test_feedback_is_stored_with_its_pr_and_link(self) -> None:
        self.answer(review_comment(5, "@review-of-legends this finding is wrong\nsee line 3", number=12), kind=m.REVIEW)
        [row] = self.store.list_feedback()
        self.assertEqual((row["repo"], row["pr"], row["login"]), (REPO, 12, "Alice"))
        self.assertEqual(row["body"], "this finding is wrong\nsee line 3")
        self.assertEqual(row["comment_url"], "https://github.com/owner/repo/pull/12#discussion_r5")

    def test_applying_a_comment_again_changes_nothing(self) -> None:
        # The poller applies a comment again when its cursor could not be saved.
        for _ in range(2):
            self.answer(review_comment(5, "@review-of-legends this finding is wrong", number=12), kind=m.REVIEW)
            self.answer(issue_comment(5, "@review-of-legends this finding is wrong", number=12))
            self.answer(issue_comment(6, "@review-of-legends lang ja"))
        self.assertEqual(len(self.store.list_feedback()), 2, "one row per comment: a review and an issue comment")
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "ja")
        self.assertEqual(m.source_key("Owner/Repo", m.request_from(issue_comment(5, "@review-of-legends x"), m.ISSUE, "")),
                         "owner/repo#issue#5")

    def test_an_issue_comment_gets_a_reaction_and_a_pr_comment_mentioning_the_commenter(self) -> None:
        request, body, client = self.answer(issue_comment(1, "@review-of-legends lang en"))
        self.assertEqual(client.calls[0], ("react-issue", 1, "+1"))
        self.assertEqual(client.calls[1][:2], ("comment", 7))
        reply = client.calls[1][2]
        self.assertEqual(reply, body)
        self.assertTrue(reply.startswith(m.REPLY_MARKER + "\n@Alice\n"))
        self.assertIn("> `@review-of-legends lang en`", reply)
        self.assertIsNone(m.find_mention(reply), "the bot's reply is never a command")

    def test_a_review_comment_gets_a_reaction_and_a_thread_reply(self) -> None:
        _, body, client = self.answer(review_comment(9, "@review-of-legends settings", number=3), kind=m.REVIEW)
        self.assertEqual(client.calls, [("react-review", 9, "+1"), ("reply", 3, 9, body)])
        self.assertIn("@review-of-legends lang <", body)

    def test_a_reply_to_a_reply_goes_under_the_threads_first_comment(self) -> None:
        # GitHub refuses POST .../comments/{id}/replies for an id that is itself a reply.
        comment = review_comment(9, "@review-of-legends settings", number=3)
        comment["in_reply_to_id"] = 4
        request, body, client = self.answer(comment, kind=m.REVIEW)
        self.assertEqual(request.reply_to, 4)
        self.assertEqual(client.calls, [("react-review", 9, "+1"), ("reply", 3, 4, body)])

    def test_a_rate_limit_refusal_propagates_and_sends_nothing_more(self) -> None:
        refused = lambda exc: "rate limit" in str(exc)  # noqa: E731
        for fail_reaction, expected in ((True, ["react-issue"]), (False, ["react-issue", "comment"])):
            with self.subTest(fail_reaction=fail_reaction):
                client = FakeClient(fail_reaction=fail_reaction, fail_reply=not fail_reaction, message="secondary rate limit")
                request = m.request_from(issue_comment(1, "@review-of-legends lang ko"), m.ISSUE, "")
                with self.assertRaises(svc.ServiceError):
                    m.acknowledge(client, request, "body", self.logged.append, REPO, refused=refused)
                self.assertEqual([call[0] for call in client.calls], expected)
        self.assertEqual(self.logged, [], "the caller says what happened")

    def test_a_quoted_command_cannot_mention_anyone_or_break_its_code_span(self) -> None:
        _, body, _ = self.answer(issue_comment(1, "@review-of-legends ping @everyone `x` " + "y" * 400))
        quoted = [line for line in body.splitlines() if line.startswith("> ")][0]
        self.assertEqual(quoted.count("`"), 2)
        self.assertLess(len(quoted), m.MAX_QUOTED_CHARS + 10)

    def test_reply_failures_are_logged_once_and_never_raised(self) -> None:
        self.answer(issue_comment(1, "@review-of-legends lang ko"), client=FakeClient(fail_reaction=True, fail_reply=True))
        self.assertEqual(len(self.logged), 2)
        self.assertIn("reacting to comment 1 failed", self.logged[0])
        self.assertIn("replying to comment 1 failed", self.logged[1])

    def test_reply_language(self) -> None:
        # No setting: the configured default, Korean unless the config says otherwise.
        _, body, _ = self.answer(issue_comment(1, "@review-of-legends settings"))
        self.assertIn("내 설정", body)
        _, body, _ = self.answer(issue_comment(2, "@review-of-legends settings"), config={"mentions": {"default_reply_language": "en"}})
        self.assertIn("Your settings", body)
        # The commenter's language once set, also on the reply that sets it.
        _, body, _ = self.answer(issue_comment(3, "@review-of-legends lang en"))
        self.assertIn("Done: reviews of your pull requests are now written in `en` (English)", body)
        _, body, _ = self.answer(issue_comment(4, "@review-of-legends lang ko"))
        self.assertIn("설정했습니다", body)
        # Any other language: English.
        _, body, _ = self.answer(issue_comment(5, "@review-of-legends lang ja"))
        self.assertIn("`ja` (Japanese)", body)
        self.assertIn("Done:", body)
        _, body, _ = self.answer(issue_comment(6, "@review-of-legends great reviews"))
        self.assertIn("Thanks: your feedback is recorded", body)

    def test_reply_and_welcome_language_rules(self) -> None:
        en = {"mentions": {"default_reply_language": "en"}}
        self.assertEqual(m.reply_language(None, {}), "ko")
        self.assertEqual(m.reply_language(None, en), "en")
        self.assertEqual(m.reply_language("ko", en), "ko")
        self.assertEqual(m.reply_language("fr", {}), "en")
        self.assertEqual(m.welcome_language("en", {}), "en")
        self.assertEqual(m.welcome_language("fr", {}), "ko", "the welcome falls back to the default reply language")
        self.assertEqual(m.welcome_language(None, en), "en")


class LanguageTextTest(unittest.TestCase):
    def test_instruction_keeps_the_fixed_values_of_the_output_format(self) -> None:
        # The required check reads "patch is correct" literally: a translated verdict breaks it.
        text = m.language_instruction("ko")
        for kept in ('"patch is correct"', '"patch is incorrect"', "priority", "severity"):
            with self.subTest(kept=kept):
                self.assertIn(kept, text)

    def test_instruction_names_the_language_and_its_code(self) -> None:
        block = m.language_instruction("ko")
        self.assertIn("in Korean (ko).", block)
        self.assertIn("Keep code, identifiers, file paths, commands and JSON keys unchanged,", block)
        self.assertIn("This overrides any language the instructions above ask for.", block)
        self.assertIn("in Traditional Chinese (zh-tw).", m.language_instruction("zh-tw"))
        self.assertIn("in pt-br (pt-br).", m.language_instruction("pt-br"), "an unmapped code is named by itself")

    def test_stored_language_is_validated(self) -> None:
        self.assertEqual(m.stored_language("ko"), "ko")
        for value in (None, "", "KO", "ko ko", 7, "x" * 20):
            self.assertIsNone(m.stored_language(value))

    def test_welcome_covers_every_setting_in_both_languages(self) -> None:
        for language in ("ko", "en"):
            body = m.welcome_body("Alice", language)
            with self.subTest(language=language):
                self.assertTrue(body.startswith(m.WELCOME_MARKER))
                self.assertIn("@Alice", body)
                for text in ("review-of-legends/reviewed/<branch>", "review-of-legends/required/<branch>",
                             "@review-of-legends lang ko", "@review-of-legends lang off", "@review-of-legends settings",
                             "20"):
                    self.assertIn(text, body)
        self.assertIn("자동완성되지 않습니다", m.welcome_body("Alice", "ko"))
        self.assertIn("does not autocomplete", m.welcome_body("Alice", "en"))
        self.assertIn("drafts are skipped", m.welcome_body("Alice", "en"))


class ConfigTest(unittest.TestCase):
    def test_default_reply_language_must_be_ko_or_en(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviewer.json"
            base = {"repositories": ["o/r"], "agents": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
            for mentions_block, ok in (({"default_reply_language": "en"}, True), ({}, True),
                                       ({"default_reply_language": "ja"}, False), ("ko", False)):
                with self.subTest(mentions=mentions_block):
                    path.write_text(json.dumps({**base, "mentions": mentions_block}), encoding="utf-8")
                    if ok:
                        self.assertEqual(svc.load_config(path)["mentions"], mentions_block)
                    else:
                        with self.assertRaises(svc.ServiceError):
                            svc.load_config(path)


class ClientEndpointTest(unittest.TestCase):
    """The GitHub calls behind the poller: URLs, the conditional listing, and its one cache entry per listing."""

    @staticmethod
    def _headers(**values: str) -> email.message.Message:
        message = email.message.Message()
        for name, value in values.items():
            message[name.replace("_", "-")] = value
        return message

    def test_listings_are_conditional_and_keep_one_cache_entry_per_endpoint(self) -> None:
        sent: List[tuple] = []

        class Response(io.BytesIO):
            def __init__(self, body: Any, headers: email.message.Message) -> None:
                super().__init__(json.dumps(body).encode("utf-8"))
                self.headers = headers

        def fake_urlopen(request, timeout=60):
            headers = {name.lower(): value for name, value in request.header_items()}
            sent.append((request.get_method(), request.full_url, headers.get("if-none-match")))
            if headers.get("if-none-match"):
                raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", self._headers(), None)
            return Response([{"id": len(sent)}], self._headers(ETag='W/"%d"' % len(sent)))

        cache: Dict[str, Any] = {}
        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"), \
                mock.patch.object(svc.urllib.request, "urlopen", fake_urlopen):
            client = svc.make_github_client(REPO, {"gh_bin": "gh"}, etag_cache=cache)
            first = client.list_repo_issue_comments("2026-10-15T01:00:00Z")
            again = client.list_repo_issue_comments("2026-10-15T01:00:00Z")
            client.list_repo_review_comments("2026-10-15T01:00:00Z")
            client.list_repo_issue_comments("2026-10-15T02:00:00Z")
        base = "https://api.github.com/repos/owner/repo/"
        query = "?since=2026-10-15T0%d%%3A00%%3A00Z&sort=updated&direction=asc&per_page=100"
        self.assertEqual(sent, [
            ("GET", base + "issues/comments" + query % 1, None),
            ("GET", base + "issues/comments" + query % 1, 'W/"1"'),
            ("GET", base + "pulls/comments" + query % 1, None),
            ("GET", base + "issues/comments" + query % 2, None),
        ])
        self.assertEqual(again, first, "a 304 is served from the cache")
        self.assertEqual(sorted(cache), [base + "issues/comments" + query % 2, base + "pulls/comments" + query % 1],
                         "the cursor's old URL is dropped from the cache")

    def test_pages_are_followed_while_they_hold_only_the_cursors_own_second(self) -> None:
        # ?since= is inclusive: rows sharing the cursor's second come back every
        # time. However many share it, the listing drains them, then stops at
        # the first page that reaches a later second.
        since = "2026-10-15T01:00:00Z"
        sent: List[int] = []

        class Response(io.BytesIO):
            def __init__(self, body: Any, headers: email.message.Message) -> None:
                super().__init__(json.dumps(body).encode("utf-8"))
                self.headers = headers

        def fake_urlopen(request, timeout=60):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
            page = int(query.get("page", ["1"])[0])
            sent.append(page)
            # 15 pages of the cursor's second, then later ones; a next link always.
            updated = since if page <= 15 else "2026-10-15T01:00:%02dZ" % page
            headers = self._headers(ETag='W/"%d"' % page)
            headers["Link"] = '<https://api.github.com/repos/owner/repo/issues/comments?since=x&page=%d>; rel="next"' % (page + 1)
            return Response([{"id": page, "updated_at": updated}], headers)

        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"), \
                mock.patch.object(svc.urllib.request, "urlopen", fake_urlopen):
            client = svc.make_github_client(REPO, {"gh_bin": "gh"}, etag_cache={})
            comments = client.list_repo_issue_comments(since)
            self.assertEqual([item["id"] for item in comments], list(range(1, 17)), "drained, then one later page")
            del sent[:]
            client.list_repo_issue_comments("2026-10-15T00:59:00Z")
            self.assertEqual(sent, [1], "a page past the cursor's second ends the listing")

    def test_a_paged_second_is_read_twice_and_must_agree(self) -> None:
        # Offset pages shift when a row of an earlier page is edited between two
        # requests: a row can be skipped. A second read that differs means "again later".
        since = "2026-10-15T01:00:00Z"
        reads = {"count": 0}
        shifted = {"on": True}

        class Response(io.BytesIO):
            def __init__(self, body: Any, headers: email.message.Message) -> None:
                super().__init__(json.dumps(body).encode("utf-8"))
                self.headers = headers

        def fake_urlopen(request, timeout=60):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
            page = int(query.get("page", ["1"])[0])
            reads["count"] += 1
            first_pass = reads["count"] <= 2
            if page == 1:
                ids = [1, 2] if first_pass or not shifted["on"] else [2, 3]
                rows = [{"id": i, "updated_at": since} for i in ids]
            else:
                rows = [{"id": 3 if first_pass or not shifted["on"] else 1, "updated_at": since if first_pass or not shifted["on"] else "2026-10-15T01:00:05Z"}]
            headers = self._headers(ETag='W/"%d"' % reads["count"])
            if page == 1:
                headers["Link"] = '<https://api.github.com/repos/owner/repo/issues/comments?since=x&page=2>; rel="next"'
            return Response(rows, headers)

        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"), \
                mock.patch.object(svc.urllib.request, "urlopen", fake_urlopen):
            client = svc.make_github_client(REPO, {"gh_bin": "gh"}, etag_cache={})
            with self.assertRaises(svc.ServiceError):
                client.list_repo_issue_comments(since)  # comment 1 was edited between the reads
            shifted["on"] = False
            reads["count"] = 0
            rows = client.list_repo_issue_comments(since)
        self.assertEqual([row["id"] for row in rows], [1, 2, 3], "two agreeing reads")
        self.assertEqual(reads["count"], 4)

    def test_the_second_read_must_repeat_the_whole_first_one(self) -> None:
        # A command past the boundary second can be the row a shift drops: the
        # second read must start with every row of the first, in order.
        since = "2026-10-15T01:00:00Z"
        later = "2026-10-15T01:00:05Z"
        state = {"reads": 0, "shift": True}

        class Response(io.BytesIO):
            def __init__(self, body: Any, headers: email.message.Message) -> None:
                super().__init__(json.dumps(body).encode("utf-8"))
                self.headers = headers

        def fake_urlopen(request, timeout=60):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
            page = int(query.get("page", ["1"])[0])
            state["reads"] += 1
            second = state["reads"] > 2
            if page == 1:
                rows = [{"id": 1, "updated_at": since}, {"id": 2, "updated_at": since}]
            elif second and state["shift"]:
                # Row 1 was edited after page 1: the command (101) shifted onto page 1 unseen.
                rows = [{"id": 102, "updated_at": later}, {"id": 1, "updated_at": "2026-10-15T01:00:09Z"}]
            else:
                rows = [{"id": 101, "updated_at": later}, {"id": 102, "updated_at": later}]
            headers = self._headers(ETag='W/"%d"' % state["reads"])
            if page == 1:
                headers["Link"] = '<https://api.github.com/repos/owner/repo/issues/comments?since=x&page=2>; rel="next"'
            return Response(rows, headers)

        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"), \
                mock.patch.object(svc.urllib.request, "urlopen", fake_urlopen):
            client = svc.make_github_client(REPO, {"gh_bin": "gh"}, etag_cache={})
            with self.assertRaises(svc.ServiceError):
                client.list_repo_issue_comments(since)
            state.update(reads=0, shift=False)
            self.assertEqual([row["id"] for row in client.list_repo_issue_comments(since)], [1, 2, 101, 102])

    def test_a_body_edited_between_the_two_reads_is_read_again(self) -> None:
        # Same id, same second (an edit keeps updated_at), new text: the reads do not agree.
        since = "2026-10-15T01:00:00Z"
        state = {"reads": 0, "edit": True}

        class Response(io.BytesIO):
            def __init__(self, body: Any, headers: email.message.Message) -> None:
                super().__init__(json.dumps(body).encode("utf-8"))
                self.headers = headers

        def fake_urlopen(request, timeout=60):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
            page = int(query.get("page", ["1"])[0])
            state["reads"] += 1
            if page == 1:
                body = "@review-of-legends lang ko" if state["reads"] > 2 and state["edit"] else "hello"
                rows = [{"id": 1, "updated_at": since, "body": body}, {"id": 2, "updated_at": since, "body": "x"}]
            else:
                rows = [{"id": 3, "updated_at": "2026-10-15T01:00:01Z", "body": "y"}]
            headers = self._headers(ETag='W/"%d"' % state["reads"])
            if page == 1:
                headers["Link"] = '<https://api.github.com/repos/owner/repo/issues/comments?since=x&page=2>; rel="next"'
            return Response(rows, headers)

        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"), \
                mock.patch.object(svc.urllib.request, "urlopen", fake_urlopen):
            client = svc.make_github_client(REPO, {"gh_bin": "gh"}, etag_cache={})
            with self.assertRaises(svc.ServiceError):
                client.list_repo_issue_comments(since)
            state.update(reads=0, edit=False)
            self.assertEqual([row["id"] for row in client.list_repo_issue_comments(since)], [1, 2, 3])

    def test_a_full_page_of_the_cursors_second_without_a_next_link_is_probed(self) -> None:
        # Exactly 100 rows in the cursor's second: GitHub sends no next link,
        # and a cached page 1 answers 304 with it. A later comment sits on page 2.
        since = "2026-10-15T01:00:00Z"
        sent: List[int] = []

        class Response(io.BytesIO):
            def __init__(self, body: Any, headers: email.message.Message) -> None:
                super().__init__(json.dumps(body).encode("utf-8"))
                self.headers = headers

        def fake_urlopen(request, timeout=60):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
            page = int(query.get("page", ["1"])[0])
            sent.append(page)
            if page == 1:
                rows = [{"id": index, "updated_at": since, "body": "x"} for index in range(1, 101)]
            else:
                rows = [{"id": 101, "updated_at": "2026-10-15T01:00:07Z", "body": "@review-of-legends lang ko"}]
            return Response(rows, self._headers(ETag='W/"%d"' % page))  # no Link header at all

        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"), \
                mock.patch.object(svc.urllib.request, "urlopen", fake_urlopen):
            client = svc.make_github_client(REPO, {"gh_bin": "gh"}, etag_cache={})
            rows = client.list_repo_issue_comments(since)
        self.assertEqual(rows[-1]["id"], 101, "page 2 was asked for")
        self.assertIn(2, sent)

    def test_a_full_page_without_a_next_link_counts_as_cut_short(self) -> None:
        # A cached page's Link can be stale: a full page may hide more, so the
        # other comment kind waits past its last time.
        sent: List[int] = []

        class Response(io.BytesIO):
            def __init__(self, body: Any, headers: email.message.Message) -> None:
                super().__init__(json.dumps(body).encode("utf-8"))
                self.headers = headers

        def fake_urlopen(request, timeout=60):
            sent.append(1)
            rows = [{"id": index, "updated_at": "2026-10-15T01:%02d:%02dZ" % (index // 60, index % 60), "body": "x"}
                    for index in range(1, 101)]
            return Response(rows, self._headers(ETag='W/"1"'))

        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"), \
                mock.patch.object(svc.urllib.request, "urlopen", fake_urlopen):
            client = svc.make_github_client(REPO, {"gh_bin": "gh"}, etag_cache={})
            rows = client.list_repo_issue_comments("2026-10-15T00:59:00Z")
        self.assertEqual(len(rows), 100)
        self.assertTrue(rows.truncated)
        self.assertEqual(len(sent), 1, "no extra request: the page ends past the cursor's second")

    def test_a_verification_read_that_found_more_keeps_the_listing_cut_short(self) -> None:
        since = "2026-10-15T01:00:00Z"
        state = {"reads": 0}

        class Response(io.BytesIO):
            def __init__(self, body: Any, headers: email.message.Message) -> None:
                super().__init__(json.dumps(body).encode("utf-8"))
                self.headers = headers

        def fake_urlopen(request, timeout=60):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
            page = int(query.get("page", ["1"])[0])
            state["reads"] += 1
            headers = self._headers(ETag='W/"%d"' % state["reads"])
            if page == 1:
                rows = [{"id": 1, "updated_at": since, "body": "a"}]
                headers["Link"] = '<https://api.github.com/repos/owner/repo/issues/comments?since=x&page=2>; rel="next"'
            else:
                rows = [{"id": 2, "updated_at": "2026-10-15T01:00:05Z", "body": "b"}]
                if state["reads"] > 2:  # by the second read, GitHub has a page 3
                    headers["Link"] = '<https://api.github.com/repos/owner/repo/issues/comments?since=x&page=3>; rel="next"'
            return Response(rows, headers)

        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"), \
                mock.patch.object(svc.urllib.request, "urlopen", fake_urlopen):
            client = svc.make_github_client(REPO, {"gh_bin": "gh"}, etag_cache={})
            rows = client.list_repo_issue_comments(since)
        self.assertEqual([row["id"] for row in rows], [1, 2])
        self.assertTrue(rows.truncated, "the second read saw more: the merged order stops here")

    def test_the_client_keeps_githubs_clock_from_the_date_header(self) -> None:
        # Where a new comment cursor starts (the scheduler's _github_now): GitHub's clock, not the host's.
        dates = iter(["Thu, 15 Oct 2026 01:00:00 GMT", "Thu, 15 Oct 2026 01:00:20 GMT", "not a date"])

        class Response(io.BytesIO):
            def __init__(self, headers: email.message.Message) -> None:
                super().__init__(b"[]")
                self.headers = headers

        def fake_urlopen(request, timeout=60):
            headers = self._headers(ETag='W/"1"', Date=next(dates))
            if request.get_header("If-none-match"):
                raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", headers, None)
            return Response(headers)

        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"), \
                mock.patch.object(svc.urllib.request, "urlopen", fake_urlopen):
            client = svc.make_github_client(REPO, {"gh_bin": "gh"}, etag_cache={})
            self.assertIsNone(client.server_time)
            client.list_open_prs()
            self.assertEqual(client.server_time, 1792026000.0)
            client.list_open_prs()  # a 304 carries the Date too
            self.assertEqual(client.server_time, 1792026020.0)
            client.list_open_prs()
            self.assertEqual(client.server_time, 1792026020.0, "an unreadable Date keeps the last one")

    def test_a_comments_edit_history_comes_from_graphql(self) -> None:
        sent: List[tuple] = []
        answers = iter([
            {"data": {"node": {"body": "@review-of-legends lang ko", "updatedAt": "2026-10-15T01:00:00Z",
                               "author": {"login": "Alice"}, "lastEditedAt": None,
                               "userContentEdits": {"totalCount": 0, "nodes": []}}}},
            {"data": {"node": {"body": "x", "updatedAt": "2026-10-15T01:00:09.512Z", "author": {"login": "Alice"},
                               "lastEditedAt": "2026-10-15T01:00:09Z", "userContentEdits": {
                "totalCount": 3, "nodes": [{"editor": {"login": "alice"}}, {"editor": None}]}}}},
            {"data": {"node": None}, "errors": [{"message": "Could not resolve to a node"}]},
            {"data": None, "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]},
        ])
        client = svc.GitHubClient.__new__(svc.GitHubClient)
        client.repo = REPO
        client.request = lambda method, endpoint, body=None, **kw: (sent.append((method, endpoint, body["variables"])),
                                                                      next(answers))[1]  # type: ignore[assignment]
        self.assertEqual(client.comment_edits("IC_1"), {
            "body": "@review-of-legends lang ko", "author": "Alice", "updated_at": "2026-10-15T01:00:00Z",
            "edited": False, "editors": [], "complete": True})
        self.assertEqual(client.comment_edits("IC_2"), {
            "body": "x", "author": "Alice", "updated_at": "2026-10-15T01:00:09Z",
            "edited": True, "editors": ["alice", None], "complete": False})
        with self.assertRaises(svc.ServiceError) as missing:
            client.comment_edits("IC_3")
        self.assertIsNone(getattr(missing.exception, "status_code", None))
        with self.assertRaises(svc.ServiceError) as limited:
            client.comment_edits("IC_4")  # data: null, a rate limit: read as one, so the watch backs off
        from code_review_service.scheduler import _rate_limit_refusal
        self.assertTrue(_rate_limit_refusal(limited.exception))
        self.assertEqual([item[:2] for item in sent], [("POST", "graphql")] * 4)
        self.assertEqual(sent[0][2], {"id": "IC_1"})

    def test_a_rate_limited_login_lookup_is_raised_and_not_remembered(self) -> None:
        # The real client (gh mode): a refused GET /user is not "no login" for
        # good; it is raised, so the watcher backs off and asks again later.
        answers = iter([429, 200])
        sent: List[str] = []

        class Response(io.BytesIO):
            def __init__(self, body: Any) -> None:
                super().__init__(json.dumps(body).encode("utf-8"))
                self.headers = email.message.Message()

        def fake_urlopen(request, timeout=60):
            sent.append(request.full_url)
            status = next(answers)
            if status != 200:
                raise urllib.error.HTTPError(request.full_url, status, "Too Many Requests", email.message.Message(),
                                             io.BytesIO(b'{"message": "API rate limit exceeded"}'))
            return Response({"login": "review-bot"})

        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"), \
                mock.patch.object(svc.urllib.request, "urlopen", fake_urlopen):
            client = svc.make_github_client(REPO, {"gh_bin": "gh"}, etag_cache={})
            with self.assertRaises(svc.ServiceError) as refused:
                client.bot_login()
            from code_review_service.scheduler import _rate_limit_refusal
            self.assertTrue(_rate_limit_refusal(refused.exception))
            self.assertEqual(client.bot_login(), "review-bot", "asked again, not cached as empty")
        self.assertEqual(len(sent), 2)

    def test_another_failed_login_lookup_is_still_no_login(self) -> None:
        def fake_urlopen(request, timeout=60):
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", email.message.Message(), io.BytesIO(b"{}"))

        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"), \
                mock.patch.object(svc.urllib.request, "urlopen", fake_urlopen):
            client = svc.make_github_client(REPO, {"gh_bin": "gh"}, etag_cache={})
            self.assertEqual(client.bot_login(), "")

    def test_reactions_and_replies(self) -> None:
        calls: List[tuple] = []
        client = svc.GitHubClient.__new__(svc.GitHubClient)
        client.repo = REPO
        client.request = lambda method, endpoint, body=None, **kw: calls.append((method, endpoint, body))  # type: ignore[assignment]
        client.react_to_issue_comment(11, "+1")
        client.react_to_review_comment(12, "+1")
        client.reply_to_review_comment(7, 12, "hi")
        self.assertEqual(calls, [
            ("POST", "repos/owner/repo/issues/comments/11/reactions", {"content": "+1"}),
            ("POST", "repos/owner/repo/pulls/comments/12/reactions", {"content": "+1"}),
            ("POST", "repos/owner/repo/pulls/7/comments/12/replies", {"body": "hi"}),
        ])


if __name__ == "__main__":
    unittest.main()
