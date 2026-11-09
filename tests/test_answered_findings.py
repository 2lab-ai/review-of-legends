"""Findings people already answered go into the next review's agent prompt.

A production PR: the author replied to a bot finding with a reason ("현행
유지합니다 — …", "…추후 필요 시 추가하겠습니다") and resolved the thread; every
later review raised the same finding on the same line again, 7-10 times. The
agents never saw the reply. answered_findings_section turns the bot's finding
threads that a person answered into a prompt block, render_prompt carries it,
and review_pr reads the threads before the agents run (best effort).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import service as svc
from tests.test_finding_lifecycle import BOT, _LifecycleClient, _meta_line, _review, _thread

HEADING = "Earlier findings people answered on this pull request:"
REPLY = "현행 유지합니다 — 재시도 키는 base를 포함하지 않아도 됩니다."
PR = {"number": 1, "title": "t", "body": "", "base": {"ref": "main"}, "head": {"ref": "f"}}


def _answered(thread_id: str, text: str = REPLY, login: str = "devacme", association: Any = "MEMBER",
              **kwargs: Any) -> Dict[str, Any]:
    """A bot finding thread a person (a repository ``association``) replied to with ``text``."""
    return _thread(thread_id, replies=kwargs.pop("replies", ()) + ((login, "User", text, association),), **kwargs)


class _PersonClient(_LifecycleClient):
    """Posts as a person (the gh login), not as the GitHub App."""

    def bot_login(self) -> str:
        return "review-of-legends"


def _section(threads: List[Dict[str, Any]], login: str = BOT, **kwargs: Any) -> str:
    return svc.answered_findings_section(threads, login, **kwargs)


def _section_with(patches: Dict[str, int], threads: List[Dict[str, Any]], **kwargs: Any) -> str:
    """_section with the module's caps patched to ``patches``."""
    with mock.patch.multiple(svc, **patches):
        return _section(threads, **kwargs)


def _reply_tail(section: str, login: str) -> str:
    """What follows ``Reply from <login>: `` on its line."""
    marker = f"Reply from {login}: "
    lines = [line for line in section.splitlines() if marker in line]
    assert len(lines) == 1, section
    return lines[0].split(marker, 1)[1]


def _reply_lines(section: str) -> List[str]:
    return [line for line in section.splitlines() if line.startswith("  Reply from ")]


def _length_at(section: str, cut: int) -> int:
    """The length of ``section`` (replies cut at 400) with every reply cut at ``cut`` instead; 0 leaves them out.

    For replies under ``cut`` characters or cut at 400 (ending in "…").
    """
    length = len(section)
    for line in _reply_lines(section):
        tail = line.split(": ", 1)[1]
        if cut == 0:
            length -= len(line) + 1
        elif tail.endswith("…") and len(tail) > cut + 1:
            length -= len(tail) - (cut + 1)
    return length


def _count_line(total: int) -> str:
    """The longest closing count line the block sets room aside for: every entry left out."""
    return f"\n- …and {total} more earlier answered findings"


def _long_current(count: int, first_line: int = 10) -> List[Dict[str, Any]]:
    """``count`` answered threads on current code, oldest first, each answered with about 2,000 characters."""
    return [_answered(f"C{i}", text=f"answer-c{i} " + "r" * 2000, line=first_line + i) for i in range(count)]


def _long_outdated(count: int) -> List[Dict[str, Any]]:
    """``count`` answered outdated threads (one title each), each answered with about 2,000 characters."""
    return [_answered(f"O{i}", text=f"answer-o{i} " + "o" * 2000, outdated=True, title=f"Outdated finding {i}")
            for i in range(count)]


class AnsweredFindingsSectionTest(unittest.TestCase):
    def test_an_answered_bot_thread_becomes_an_entry(self) -> None:
        section = _section([_answered("T1", resolved=True)])
        self.assertEqual(section.strip().splitlines()[0], HEADING)
        for text in ("[P2]", "correctness", "`a.py:2`", "Retry key misses the base",
                     f"Reply from devacme: {REPLY}"):
            self.assertIn(text, section)
        self.assertRegex(section, r"(?<![\w-])resolved\b")

    def test_an_open_answered_thread_is_not_called_resolved(self) -> None:
        section = _section([_answered("T1", label="P3")])
        self.assertIn("[P3]", section)
        self.assertIn(f"Reply from devacme: {REPLY}", section)
        self.assertNotRegex(section, r"(?<![\w-])resolved\b")

    def test_nothing_answered_gives_an_empty_string(self) -> None:
        for label, threads in (
            ("no threads", []),
            ("unanswered", [_thread("T1"), _thread("T2", resolved=True)]),
            ("bot follow-up only", [_thread("T1", resolved=True, replies=(("review-of-legends", "Bot", "bot again"),))]),
        ):
            with self.subTest(case=label):
                self.assertEqual(_section(threads), "")

    def test_threads_the_bot_did_not_open_are_ignored(self) -> None:
        threads = [
            _answered("T1", author="devacme", author_type="User"),
            _answered("T2", author="chatgpt-codex-connector"),
            # Bot identity, but not a finding comment.
            _answered("T3", body="I think this needs a second look."),
        ]
        self.assertEqual(_section(threads), "")

    def test_no_app_bot_login_claims_nothing(self) -> None:
        self.assertEqual(_section([_answered("T1")], login="review-of-legends"), "")

    def test_the_latest_human_reply_is_shown_and_bot_follow_ups_are_not_replies(self) -> None:
        thread = _thread("T1", resolved=True, replies=(
            ("alice", "User", "answer-one"),
            ("bob", "User", "answer-two"),
            ("review-of-legends", "Bot", "bot follow-up text"),
        ))
        section = _section([thread])
        self.assertIn("Reply from bob: answer-two", section)
        self.assertNotIn("answer-one", section)
        self.assertNotIn("bot follow-up text", section)

    def test_repeats_on_one_line_collapse_to_the_newest_thread(self) -> None:
        # Threads arrive oldest first; the same (path, line, agents) is one finding asked again.
        threads = [
            _answered("T1", text="answer-one", title="First wording", resolved=True),
            _answered("T2", text="answer-two", title="Second wording", resolved=True),
            _answered("T3", text="answer-three", title="Third wording", resolved=True),
        ]
        section = _section(threads)
        self.assertIn("answer-three", section)
        self.assertIn("Third wording", section)
        for gone in ("answer-one", "answer-two", "First wording", "Second wording"):
            self.assertNotIn(gone, section)

    def test_another_agent_on_the_same_line_is_its_own_entry(self) -> None:
        threads = [_answered("T1", text="answer-correctness", agent="correctness"),
                   _answered("T2", text="answer-security", agent="security", title="Reused results cross bases")]
        section = _section(threads)
        self.assertIn("answer-correctness", section)
        self.assertIn("answer-security", section)

    def test_newest_first(self) -> None:
        threads = [_answered("T1", text="answer-older", line=2, title="Older one"),
                   _answered("T2", text="answer-newer", line=3, title="Newer one")]
        section = _section(threads)
        self.assertLess(section.index("answer-newer"), section.index("answer-older"))

    def test_findings_on_current_code_come_first_newest_first_in_each(self) -> None:
        """A production PR (v1.0.4): 9 answered threads on code still there were crowded out by 20 outdated entries.

        A finding on code that is still there is the one an agent can raise
        again; an outdated thread's code is gone. Threads arrive oldest first.
        """
        threads = [
            _answered("T1", text="answer-current-old", line=5),
            _answered("T2", text="answer-outdated-old", outdated=True, title="Outdated one"),
            _answered("T3", text="answer-current-new", line=6, resolved=True),
            _answered("T4", text="answer-outdated-new", outdated=True, title="Outdated two"),
        ]
        section = _section(threads)
        order = ["answer-current-new", "answer-current-old", "answer-outdated-new", "answer-outdated-old"]
        self.assertEqual(sorted(order, key=section.index), order, section)

    def test_a_thread_without_a_current_line_goes_with_the_outdated(self) -> None:
        # Current code = GitHub does not mark the thread outdated and still maps it onto a line.
        no_line = _answered("T2", text="answer-no-line", line=7)
        no_line["line"] = None
        outdated_on_a_line = _answered("T3", text="answer-outdated-line", line=8)
        outdated_on_a_line["isOutdated"] = True
        threads = [_answered("T1", text="answer-current", line=5), no_line, outdated_on_a_line]
        section = _section(threads)
        self.assertLess(section.index("answer-current"), section.index("answer-outdated-line"))
        self.assertLess(section.index("answer-current"), section.index("answer-no-line"))
        self.assertLess(section.index("answer-outdated-line"), section.index("answer-no-line"), "newest first in each")

    def test_the_caps_keep_the_findings_on_current_code(self) -> None:
        current = [_answered("C1", text="answer-c1", line=5), _answered("C2", text="answer-c2", line=6)]
        outdated = [_answered(f"O{i}", text=f"answer-o{i}", outdated=True, title=f"Outdated finding {i}")
                    for i in range(3)]
        threads = current + outdated  # the outdated ones are the newer
        # Room for the two current entries, not for a third.
        cap = len(_section(current)) + 60
        for name, patch in (("MAX_ANSWERED_FINDINGS", 2), ("MAX_ANSWERED_CHARS", cap)):
            with self.subTest(cap=name), mock.patch.object(svc, name, patch):
                section = _section(threads)
                self.assertIn("Reply from devacme: answer-c1", section)
                self.assertIn("Reply from devacme: answer-c2", section)
                self.assertNotIn("answer-o", section)
                self.assertEqual(section.splitlines()[-1], "- …and 3 more earlier answered findings")

    def test_outdated_threads_collapse_by_normalized_title(self) -> None:
        threads = [
            _answered("T1", text="answer-old", outdated=True, title="Retry key misses the base"),
            _answered("T2", text="answer-new", outdated=True, title="Retry  KEY misses the BASE"),
            _answered("T3", text="answer-other", outdated=True, title="Another finding"),
        ]
        section = _section(threads)
        self.assertIn("answer-new", section)
        self.assertIn("answer-other", section)
        self.assertNotIn("answer-old", section)
        self.assertIn("`a.py`", section, "an outdated thread has no line")
        self.assertNotIn("`a.py:", section)

    def test_a_long_reply_is_one_line_cut_to_400_characters(self) -> None:
        text = "line one\n\n   line two\t" + "x" * 1000
        section = _section([_answered("T1", text=text, login="alice")])
        tail = _reply_tail(section, "alice")
        self.assertTrue(tail.startswith("line one line two x"), tail[:40])
        self.assertEqual(len(tail), 401, "400 characters, plus an ellipsis")
        self.assertTrue(tail.endswith("x…"), tail[-5:])
        self.assertNotIn("x" * 400, section)

    def test_a_p0_or_p1_entry_is_not_settled_by_a_reason(self) -> None:
        """The required gate reads only P0/P1: a required agent obeying the block must not let a P1 pass."""
        section = _section([_answered("T1", label="P1", text="추후 필요 시 추가하겠습니다", resolved=True)])
        self.assertIn("[P1]", section)
        self.assertEqual(section.count(HEADING), 1)
        text = " ".join(section.split())
        self.assertIn(
            'An entry marked [P0] or [P1] is reported again while the diff below still shows the defect, '
            'unless the reply shows it is not a defect: "kept as is", "intended" or "a later follow-up" '
            "do not settle a P0 or P1. The body then quotes the reply and says why it does not hold.",
            text,
        )
        self.assertIn("An entry marked [P2] or [P3] that a reply declined with a reason", text)
        # An entry left without its reply (CurrentCodeFitTest) is a P0/P1 all the same.
        self.assertIn("An entry shown without its reply was still answered by a person: treat a [P2] or [P3] one as "
                      "settled, and a [P0] or [P1] one as the [P0]/[P1] rule above says.", text)

    def test_only_an_owner_member_collaborator_or_contributor_answers(self) -> None:
        for association in ("OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"):
            with self.subTest(association=association):
                self.assertIn(f"Reply from devacme: {REPLY}", _section([_answered("T1", association=association)]))
        # None: a comment read without the field.
        for association in ("FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "MANNEQUIN", "NONE", None):
            with self.subTest(association=association):
                self.assertEqual(_section([_answered("T1", association=association)]), "")

    def test_the_pr_authors_reply_answers_whatever_its_association(self) -> None:
        """The PR author decides on the findings of their PR; their login is compared case-insensitively."""
        for association in ("NONE", "FIRST_TIME_CONTRIBUTOR", None):
            thread = _answered("T1", resolved=True, association=association)
            for pr_author in ("devacme", "DevAcme"):
                with self.subTest(association=association, pr_author=pr_author):
                    self.assertIn(f"Reply from devacme: {REPLY}", _section([thread], pr_author=pr_author))
            for kwargs in ({}, {"pr_author": ""}, {"pr_author": "someone"}):
                with self.subTest(association=association, **kwargs):
                    self.assertEqual(_section([thread], **kwargs), "")

    def test_the_pr_authors_later_reply_is_the_latest_answer(self) -> None:
        thread = _thread("T1", resolved=True, replies=(("alice", "User", "현행 유지", "MEMBER"),
                                                       ("devacme", "User", "추후 반영", "NONE")))
        self.assertEqual(_reply_tail(_section([thread], pr_author="devacme"), "devacme"), "추후 반영")
        self.assertEqual(_reply_tail(_section([thread]), "alice"), "현행 유지", "without the author, alice's stands")

    def test_without_an_association_only_the_pr_author_answers(self) -> None:
        for reply in (("drive-by", "User", "keep it", "NONE"),
                      ("drive-by", "User", "keep it", "FIRST_TIME_CONTRIBUTOR"),
                      ("devacme", "Bot", "keep it", "NONE")):
            with self.subTest(reply=reply):
                thread = _thread("T1", resolved=True, replies=(reply,))
                self.assertEqual(_section([thread], pr_author="devacme"), "")

    def test_another_github_apps_comment_is_no_answer(self) -> None:
        for reply in (("chatgpt-codex-connector", "Bot", "Looks intentional.", "NONE"),
                      ("coderabbitai", "Bot", "Looks intentional.", "MEMBER")):
            with self.subTest(app=reply[0]):
                self.assertEqual(_section([_thread("T1", resolved=True, replies=(reply,))]), "")
        thread = _thread("T1", resolved=True, replies=(("alice", "User", "answer-alice"),
                                                       ("coderabbitai", "Bot", "app follow-up")))
        section = _section([thread])
        self.assertIn("Reply from alice: answer-alice", section)
        self.assertNotIn("app follow-up", section)

    def test_a_later_reply_that_is_no_answer_does_not_replace_the_members(self) -> None:
        # The member decided; a later word from someone the repository gave no say does not.
        for later in (("drive-by", "User", "수정됨", "FIRST_TIME_CONTRIBUTOR"),
                      ("drive-by", "User", "수정됨", "NONE"),
                      ("chatgpt-codex-connector", "Bot", "수정됨", "NONE")):
            with self.subTest(later=later):
                thread = _thread("T1", resolved=True, replies=(("devacme", "User", "현행 유지", "MEMBER"), later))
                section = _section([thread])
                self.assertEqual(_reply_tail(section, "devacme"), "현행 유지")
                self.assertNotIn("수정됨", section)
                self.assertNotIn(f"Reply from {later[0]}", section)

    def test_a_thread_with_comments_left_unlisted_is_left_out(self) -> None:
        # comments(first: 100): a later answer may be among the comments not listed.
        self.assertEqual(_section([_answered("T1", resolved=True, total=150)]), "")

    def test_at_most_80_entries_newest_first(self) -> None:
        threads = [_answered(f"T{i}", text=f"answer-{i}", line=i + 1) for i in range(81)]
        section = _section(threads)
        entries = [line for line in section.splitlines() if line.startswith("- [P")]
        self.assertEqual(len(entries), 80)
        self.assertIn("`a.py:81`", entries[0], "the newest thread leads")
        self.assertIn("`a.py:2`", entries[-1])
        self.assertNotIn("`a.py:1`", section, "the oldest is the one left out")
        self.assertEqual(section.splitlines()[-1], "- …and 1 more earlier answered findings")

    def test_the_whole_block_stays_within_32000_characters(self) -> None:
        # 100 outdated entries of about 490 characters: the characters, not the 80 entries, are the bound.
        # (Findings on current code get shorter replies first: CurrentCodeFitTest.)
        threads = [_answered(f"T{i}", text="y" * 2000, outdated=True, title=f"Finding number {i}")
                   for i in range(100)]
        section = _section(threads)
        self.assertEqual(svc.MAX_ANSWERED_CHARS, 32000)
        self.assertLessEqual(len(section), 32000, "heading, guide and count line included")
        self.assertGreater(len(section), 32000 - 1000, "the room is used")
        shown = sum(1 for line in section.splitlines() if line.startswith("- [P"))
        self.assertTrue(0 < shown < svc.MAX_ANSWERED_FINDINGS, shown)
        self.assertIn(": Finding number 99\n", section, "newest kept")
        self.assertEqual(section.splitlines()[-1], f"- …and {100 - shown} more earlier answered findings")

    def test_a_merged_thread_shows_every_title_it_carries(self) -> None:
        titles = ["Retry key misses the base", "Reused results cross bases"]
        body = ("**P2 - Retry key misses the base**\n\n_2 findings on this line._\n\n"
                "**correctness · P2 · Retry key misses the base**\n\nfirst\n\n"
                "**security · P2 · Reused results cross bases**\n\nsecond\n\n"
                + _meta_line(["correctness", "security"], titles))
        section = _section([_answered("T1", body=body, outdated=True, resolved=True)])
        entries = [line for line in section.splitlines() if line.startswith("- [P")]
        self.assertEqual(len(entries), 1, section)
        self.assertIn("correctness, security `a.py` resolved: Retry key misses the base / reused results cross bases",
                      entries[0])

    def test_a_huge_title_is_cut_and_crowds_out_nothing(self) -> None:
        threads = [_answered(f"T{i}", text=f"answer-{i}", line=i + 1) for i in range(3)]
        threads.append(_answered("T9", text="answer-huge", line=9, title="Huge " + "h" * 24000))
        section = _section(threads)
        entries = [line for line in section.splitlines() if line.startswith("- [P")]
        self.assertEqual(len(entries), 4, "the older entries still fit")
        self.assertIn("`a.py:9`", entries[0], "newest first")
        title = entries[0].split("`: ", 1)[1]
        self.assertLessEqual(len(title), svc.MAX_ANSWERED_TITLE_CHARS)
        self.assertTrue(title.startswith("Huge hhh") and title.endswith("…"), title[:20])
        for i in range(3):
            self.assertIn(f"answer-{i}", section)
        self.assertNotIn("more earlier answered findings", section)
        self.assertLessEqual(len(section), svc.MAX_ANSWERED_CHARS)

    def test_an_entry_that_does_not_fit_is_skipped_and_an_older_one_still_fits(self) -> None:
        # Outdated entries: those on current code get shorter replies first (CurrentCodeFitTest).
        short = _answered("T1", text="short answer", outdated=True, title="Short one")
        long = _answered("T2", text="L" * 2000, outdated=True, title="Long one")  # the newer: about 490 characters
        # Room for the short entry and the count line, not for the long one.
        cap = len(_section([short])) + 100
        with mock.patch.object(svc, "MAX_ANSWERED_CHARS", cap):
            section = _section([short, long])
        self.assertLessEqual(len(section), cap)
        self.assertIn("Reply from devacme: short answer", section)
        self.assertNotIn("LLLL", section)
        self.assertEqual(section.splitlines()[-1], "- …and 1 more earlier answered findings")


class CurrentCodeFitTest(unittest.TestCase):
    """Findings on current code are kept by cutting the replies shorter, not by dropping them.

    A production PR (v1.0.5): the block, 61 entries on current code plus
    outdated ones, took 31,956 of the 32,000 characters. With more answered
    findings on current code, once those alone filled the block the oldest of
    them would be dropped again, and those are the findings an agent can
    raise again. The replies of every entry on current code are cut to 400,
    200, 100 characters, then left out (the title line alone), whichever is
    the longest at which they all fit; the outdated ones take the room left.
    """

    def test_everything_fitting_at_400_is_the_block_as_before(self) -> None:
        threads = [
            _answered("C1", text="현행 유지합니다", line=5),
            _answered("O1", text="Fixed in the next commit.", login="alice", outdated=True, title="Old finding",
                      resolved=True),
            _answered("C2", text="z" * 450, line=6, label="P1", resolved=True),
            _answered("O2", text="Out of scope.", outdated=True, title="Other old finding", agent="security"),
        ]
        entries = [
            "- [P1] correctness `a.py:6` resolved: Retry key misses the base",
            "  Reply from devacme: " + "z" * 400 + "…",
            "- [P2] correctness `a.py:5`: Retry key misses the base",
            "  Reply from devacme: 현행 유지합니다",
            "- [P2] security `a.py`: Other old finding",
            "  Reply from devacme: Out of scope.",
            "- [P2] correctness `a.py` resolved: Old finding",
            "  Reply from alice: Fixed in the next commit.",
        ]
        self.assertEqual(_section(threads), "\n".join([HEADING, svc.ANSWERED_FINDINGS_GUIDE, *entries]))

    def test_replies_on_current_code_are_cut_to_200_when_400_does_not_fit(self) -> None:
        current, outdated = _long_current(5), _long_outdated(3)
        at_400 = _section(current)
        cap = _length_at(at_400, 200) + len(_count_line(8)) + 20
        self.assertGreater(len(at_400) + len(_count_line(8)), cap, "400 does not fit")
        caps = {"MAX_ANSWERED_CHARS": cap}
        section = _section_with(caps, current + outdated)
        self.assertLessEqual(len(section), cap)
        for line in range(10, 15):
            self.assertIn(f"`a.py:{line}`", section)
        replies = _reply_lines(section)
        self.assertEqual(len(replies), 5, section)
        for reply in replies:
            tail = reply.split(": ", 1)[1]
            self.assertEqual(len(tail), 201, "200 characters, plus an ellipsis")
            self.assertTrue(tail.startswith("answer-c") and tail.endswith("r…"), tail[:20])
        self.assertNotIn("answer-o", section, "the outdated ones do not fit at 400 in what is left")
        self.assertEqual(section.splitlines()[-1], "- …and 3 more earlier answered findings")
        stats: Dict[str, int] = {}
        self.assertEqual(_section_with(caps, current + outdated, stats=stats), section)
        self.assertEqual(stats, {"current": 5, "current_shown": 5, "outdated": 3, "outdated_shown": 0,
                                 "reply_cut": 200})

    def test_replies_on_current_code_are_cut_to_100_when_200_does_not_fit(self) -> None:
        current, outdated = _long_current(5), _long_outdated(3)
        at_400 = _section(current)
        cap = _length_at(at_400, 100) + len(_count_line(8)) + 20
        self.assertGreater(_length_at(at_400, 200) + len(_count_line(8)), cap, "200 does not fit")
        caps = {"MAX_ANSWERED_CHARS": cap}
        section = _section_with(caps, current + outdated)
        self.assertLessEqual(len(section), cap)
        entries = [line for line in section.splitlines() if line.startswith("- [P")]
        self.assertEqual(entries, [f"- [P2] correctness `a.py:{line}`: Retry key misses the base"
                                   for line in range(14, 9, -1)], "every entry on current code, newest first")
        replies = _reply_lines(section)
        self.assertEqual(len(replies), 5, section)
        for reply in replies:
            tail = reply.split(": ", 1)[1]
            self.assertEqual(len(tail), 101, "100 characters, plus an ellipsis")
            self.assertTrue(tail.startswith("answer-c") and tail.endswith("r…"), tail[:20])
        self.assertNotIn("answer-o", section, "the outdated ones do not fit at 400 in what is left")
        self.assertEqual(section.splitlines()[-1], "- …and 3 more earlier answered findings")
        stats: Dict[str, int] = {}
        self.assertEqual(_section_with(caps, current + outdated, stats=stats), section)
        self.assertEqual(stats, {"current": 5, "current_shown": 5, "outdated": 3, "outdated_shown": 0,
                                 "reply_cut": 100})

    def test_entries_that_fill_the_room_exactly_keep_400(self) -> None:
        # The room set aside for the entries includes the longest count line: at
        # the exact cap the entries at 400 leave none of it; one character less
        # and they are cut to 200.
        current = _long_current(3)
        at_400 = _section(current)
        exact = len(at_400) + len(_count_line(3))
        for cap, reply_cut in ((exact, 400), (exact - 1, 200)):
            with self.subTest(cap=cap - exact):
                caps = {"MAX_ANSWERED_CHARS": cap}
                stats: Dict[str, int] = {}
                section = _section_with(caps, current, stats=stats)
                self.assertLessEqual(len(section), cap)
                self.assertEqual([len(reply.split(": ", 1)[1]) for reply in _reply_lines(section)],
                                 [reply_cut + 1] * 3)
                self.assertNotIn("more earlier answered findings", section)
                self.assertEqual(stats, {"current": 3, "current_shown": 3, "outdated": 0, "outdated_shown": 0,
                                         "reply_cut": reply_cut})
                if reply_cut == 400:
                    self.assertEqual(section, at_400, "the block as without the cap")

    def test_the_outdated_ones_keep_400_while_those_on_current_code_are_cut(self) -> None:
        current = _long_current(6)
        outdated = [_answered("O1", text="answer-o1 " + "o" * 2000, login="alice", outdated=True,
                              title="Outdated finding")]
        at_400 = _section(current)
        # The outdated entry at 400, with its newline.
        outdated_entry = len(_section(current + outdated)) - len(at_400)
        count_line = len(_count_line(7))
        for cut, longer in ((200, 400), (100, 200), (0, 100)):
            with self.subTest(cut=cut):
                # Room for those on current code at ``cut`` and for the outdated entry at 400.
                cap = _length_at(at_400, cut) + outdated_entry + count_line + 20
                self.assertGreater(_length_at(at_400, longer) + count_line, cap, f"{longer} does not fit")
                stats: Dict[str, int] = {}
                section = _section_with({"MAX_ANSWERED_CHARS": cap}, current + outdated, stats=stats)
                self.assertLessEqual(len(section), cap)
                current_tails = [line.split(": ", 1)[1] for line in _reply_lines(section)
                                 if line.startswith("  Reply from devacme: ")]
                self.assertEqual([len(tail) for tail in current_tails], [cut + 1] * 6 if cut else [])
                tail = _reply_tail(section, "alice")
                self.assertEqual(len(tail), 401, "the outdated reply: 400 characters, plus an ellipsis")
                self.assertTrue(tail.startswith("answer-o1 ooo"), tail[:20])
                self.assertNotIn("more earlier answered findings", section)
                self.assertEqual(stats, {"current": 6, "current_shown": 6, "outdated": 1, "outdated_shown": 1,
                                         "reply_cut": cut})

    def test_replies_on_current_code_are_left_out_when_100_does_not_fit(self) -> None:
        current, outdated = _long_current(5), _long_outdated(3)
        at_400 = _section(current)
        cap = _length_at(at_400, 0) + len(_count_line(8)) + 20
        self.assertGreater(_length_at(at_400, 100) + len(_count_line(8)), cap, "100 does not fit")
        caps = {"MAX_ANSWERED_CHARS": cap}
        section = _section_with(caps, current + outdated)
        self.assertLessEqual(len(section), cap)
        entries = [line for line in section.splitlines() if line.startswith("- [P")]
        self.assertEqual(entries, [f"- [P2] correctness `a.py:{line}`: Retry key misses the base"
                                   for line in range(14, 9, -1)], "newest first, the title line alone")
        self.assertNotIn("Reply from", section)
        self.assertNotIn("answer-", section)
        self.assertEqual(section.splitlines()[-1], "- …and 3 more earlier answered findings")
        stats: Dict[str, int] = {}
        self.assertEqual(_section_with(caps, current + outdated, stats=stats), section)
        self.assertEqual(stats, {"current": 5, "current_shown": 5, "outdated": 3, "outdated_shown": 0,
                                 "reply_cut": 0})

    def test_when_even_the_title_lines_do_not_fit_the_newest_title_lines_are_kept(self) -> None:
        # Five title lines miss the cap by 20 characters: the four newest are kept
        # without replies, the oldest and both outdated entries are counted.
        current = [_answered("S", text="short answer", line=9)] + _long_current(4)
        outdated = _long_outdated(2)
        cap = _length_at(_section(current), 0) + len(_count_line(7)) - 20
        caps = {"MAX_ANSWERED_CHARS": cap}
        section = _section_with(caps, current + outdated)
        self.assertLessEqual(len(section), cap)
        self.assertNotIn("`a.py:9`", section)
        self.assertEqual(_reply_lines(section), [])
        self.assertNotIn("answer-o", section)
        self.assertEqual(section.splitlines()[-1], "- …and 3 more earlier answered findings")
        stats: Dict[str, int] = {}
        self.assertEqual(_section_with(caps, current + outdated, stats=stats), section)
        self.assertEqual(stats, {"current": 5, "current_shown": 4, "outdated": 2, "outdated_shown": 0,
                                 "reply_cut": 0})

    def test_the_entry_cap_keeps_the_newest_on_current_code_with_shorter_replies(self) -> None:
        current = _long_current(3)  # a.py:10, 11, 12; the cap of 2 keeps 12 and 11
        cap = _length_at(_section(current[1:]), 200) + len(_count_line(3)) + 20
        caps = {"MAX_ANSWERED_FINDINGS": 2, "MAX_ANSWERED_CHARS": cap}
        section = _section_with(caps, current)
        self.assertLessEqual(len(section), cap)
        self.assertIn("`a.py:12`", section)
        self.assertIn("`a.py:11`", section)
        self.assertNotIn("`a.py:10`", section)
        self.assertEqual([len(reply.split(": ", 1)[1]) for reply in _reply_lines(section)], [201, 201])
        self.assertEqual(section.splitlines()[-1], "- …and 1 more earlier answered findings")
        stats: Dict[str, int] = {}
        self.assertEqual(_section_with(caps, current, stats=stats), section)
        self.assertEqual(stats, {"current": 3, "current_shown": 2, "outdated": 0, "outdated_shown": 0,
                                 "reply_cut": 200})

    def test_100_findings_on_current_code_keep_80_at_200_characters(self) -> None:
        # At 400 characters a reply, about 64 of them fit in 32,000 characters.
        threads = [_answered(f"T{i}", text="y" * 2000, line=i + 1) for i in range(100)]
        section = _section(threads)
        self.assertLessEqual(len(section), svc.MAX_ANSWERED_CHARS)
        entries = [line for line in section.splitlines() if line.startswith("- [P")]
        self.assertEqual(len(entries), svc.MAX_ANSWERED_FINDINGS)
        self.assertIn("`a.py:100`", entries[0])
        self.assertIn("`a.py:21`", entries[-1])
        self.assertNotIn("`a.py:20`", section)
        self.assertEqual({len(reply.split(": ", 1)[1]) for reply in _reply_lines(section)}, {201})
        self.assertEqual(section.splitlines()[-1], "- …and 20 more earlier answered findings")

    def test_the_counts_for_the_log(self) -> None:
        stats: Dict[str, int] = {"stale": 1}
        self.assertEqual(_section([], stats=stats), "")
        self.assertEqual(stats, {"current": 0, "current_shown": 0, "outdated": 0, "outdated_shown": 0,
                                 "reply_cut": 400})
        threads = [_answered("C1", line=5), _answered("O1", outdated=True, title="Old finding"),
                   _answered("O2", outdated=True, title="Other old finding")]
        stats = {}
        self.assertEqual(_section(threads, stats=stats), _section(threads))
        self.assertEqual(stats, {"current": 1, "current_shown": 1, "outdated": 2, "outdated_shown": 2,
                                 "reply_cut": 400})


class RenderPromptAnsweredTest(unittest.TestCase):
    BLOCK = f"{HEADING}\n- [P2] correctness `a.py:2` resolved: Retry key misses the base\n  Reply from alice: keep as is"

    def test_the_block_goes_in_before_the_output_contract(self) -> None:
        prompt = svc.render_prompt("o/r", PR, [], "diff", "agent", answered=self.BLOCK)
        self.assertIn(self.BLOCK, prompt)
        self.assertLess(prompt.index(self.BLOCK), prompt.index("Return only valid JSON"))
        self.assertEqual(prompt.count(HEADING), 1)

    def test_an_empty_block_changes_nothing(self) -> None:
        plain = svc.render_prompt("o/r", PR, [], "diff", "agent")
        self.assertEqual(svc.render_prompt("o/r", PR, [], "diff", "agent", answered=""), plain)
        self.assertNotIn(HEADING, plain)

    def test_the_block_survives_the_language_block(self) -> None:
        prompt = svc.render_prompt("o/r", PR, [], "diff", "agent", language="Korean", answered=self.BLOCK)
        self.assertIn(self.BLOCK, prompt)

    def test_a_p2_or_p3_never_makes_the_patch_incorrect(self) -> None:
        text = " ".join(svc.render_prompt("o/r", PR, [], "diff", "agent").split())
        self.assertIn("A P2 or P3 never makes the patch incorrect", text)

    def test_the_priority_mapping_no_longer_calls_p2_a_normal_bug(self) -> None:
        text = " ".join(svc.render_prompt("o/r", PR, [], "diff", "agent").split())
        self.assertNotIn("P2=normal bug", text)
        # The output contract keeps both verdict literals (test_author_language, test_mentions).
        self.assertIn("patch is correct|patch is incorrect", text)


class _Stop(Exception):
    pass


class RunAgentAnsweredTest(unittest.TestCase):
    def test_run_agent_passes_the_block_to_render_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(svc, "render_prompt", side_effect=_Stop) as render:
            tmp_path = Path(tmp)
            with self.assertRaises(_Stop):
                svc.run_agent({"id": "correctness", "prompt_path": "x"}, {"agent_runner": {"command": ["true"]}},
                              tmp_path, tmp_path / "run", "o/r", PR, [], "diff", prompt_text="agent",
                              answered="BLOCK")
        self.assertEqual(render.call_args.kwargs["answered"], "BLOCK")


class ReviewPrAnsweredTest(unittest.TestCase):
    """review_pr reads the threads before the agents run and hands each agent the block."""

    def _review(self, client, **kwargs):
        captured: Dict[str, Dict[str, Any]] = {}

        def capture(agent, *args, **agent_kwargs):
            client.calls.append("run_agent")
            captured[agent["id"]] = agent_kwargs
            return {"agent": agent["id"], "summary": "ok", "findings": []}

        posted = _review(client, {}, run_agent=capture, **kwargs)
        return posted, captured

    def test_each_agent_gets_the_answered_findings(self) -> None:
        client = _LifecycleClient([_answered("T1", line=2, resolved=True)])
        posted, captured = self._review(client)
        self.assertTrue(posted)
        self.assertEqual(set(captured), {"correctness", "security", "tests"})
        for agent_id, kwargs in captured.items():
            with self.subTest(agent=agent_id):
                self.assertIn("answered", kwargs)
                self.assertIn(HEADING, kwargs["answered"])
                self.assertIn(REPLY, kwargs["answered"])
        self.assertLess(client.calls.index("list_review_threads"), client.calls.index("run_agent"),
                        "the threads are read before the agents run")

    def test_the_log_says_how_many_answered_findings_the_agents_get(self) -> None:
        client = _LifecycleClient([_answered("T1", line=2, resolved=True),
                                   _answered("T2", outdated=True, title="Old finding", resolved=True)])
        logged: List[str] = []
        with mock.patch.object(svc, "log", side_effect=logged.append):
            posted, captured = self._review(client)
        self.assertTrue(posted)
        self.assertEqual(set(captured), {"correctness", "security", "tests"})
        self.assertIn("owner/repo pr #5: the agents get the earlier findings people answered: "
                      "1/1 on current code, 1/1 outdated, replies cut at 400", logged)

    def test_the_pr_authors_contributor_answer_reaches_the_agents(self) -> None:
        """A production PR as the App reads it: the author's reply on a resolved thread is CONTRIBUTOR."""
        client = _LifecycleClient([_answered("T1", line=2, resolved=True, association="CONTRIBUTOR")])
        posted, captured = self._review(client, pr_user={"login": "devacme", "type": "User"})
        self.assertTrue(posted)
        self.assertEqual(set(captured), {"correctness", "security", "tests"})
        for agent_id, kwargs in captured.items():
            with self.subTest(agent=agent_id):
                self.assertIn(HEADING, kwargs.get("answered", ""))
                self.assertIn(f"Reply from devacme: {REPLY}", kwargs["answered"])

    def test_the_block_reads_the_pr_authors_login(self) -> None:
        """review_pr passes the PR's ``user.login``: the author's reply counts with any association."""
        for pr_user, given in (({"login": "devacme", "type": "User"}, True),
                               ({"login": "DEVACME", "type": "User"}, True),
                               ({"login": "someone", "type": "User"}, False),
                               # A bot's PR: bots answer nothing (mentions.pr_author).
                               ({"login": "devacme", "type": "Bot"}, False)):
            with self.subTest(pr_user=pr_user):
                client = _LifecycleClient([_answered("T1", line=2, resolved=True, association="NONE")])
                posted, captured = self._review(client, pr_user=pr_user)
                self.assertTrue(posted)
                self.assertEqual(set(captured), {"correctness", "security", "tests"})
                for kwargs in captured.values():
                    if given:
                        self.assertIn(f"Reply from devacme: {REPLY}", kwargs.get("answered", ""))
                    else:
                        self.assertNotIn("answered", kwargs)

    def test_a_listing_failure_still_posts_the_review_without_a_block(self) -> None:
        client = _LifecycleClient([_answered("T1", line=2, resolved=True)],
                                  list_error=svc.ServiceError("GitHub GraphQL failed: 502"))
        posted, captured = self._review(client)
        self.assertTrue(posted)
        self.assertEqual(len(client.reviews), 1)
        self.assertEqual(set(captured), {"correctness", "security", "tests"})
        for kwargs in captured.values():
            self.assertNotIn("answered", kwargs)
        self.assertLess(client.calls.index("list_review_threads"), client.calls.index("run_agent"),
                        "the listing was tried before the agents ran")

    def test_no_block_when_nobody_answered(self) -> None:
        client = _LifecycleClient([_thread("T1", line=2, resolved=True), _thread("T2", line=3)])
        posted, captured = self._review(client)
        self.assertTrue(posted)
        for kwargs in captured.values():
            self.assertNotIn("answered", kwargs)

    def test_skip_open_duplicates_off_reads_nothing_before_the_agents(self) -> None:
        client = _LifecycleClient([_answered("T1", line=2, resolved=True)])
        posted, captured = self._review(client, lifecycle={"skip_open_duplicates": False})
        self.assertTrue(posted)
        for kwargs in captured.values():
            self.assertNotIn("answered", kwargs)
        self.assertNotIn("list_review_threads", client.calls[:client.calls.index("run_agent")])

    def test_without_a_github_app_nothing_is_read_before_the_agents(self) -> None:
        # Posting as a person, no thread is the bot's (parse_bot_thread), so the read would be wasted.
        client = _PersonClient([_answered("T1", line=2, resolved=True)])
        posted, captured = self._review(client)
        self.assertTrue(posted)
        self.assertEqual(set(captured), {"correctness", "security", "tests"})
        for kwargs in captured.values():
            self.assertNotIn("answered", kwargs)
        self.assertNotIn("list_review_threads", client.calls[:client.calls.index("run_agent")])

    def test_nothing_is_read_for_the_agents_when_none_runs(self) -> None:
        client = _LifecycleClient([_answered("T1", line=2, resolved=True)])
        diff = svc.review_diff_fingerprint(client.list_files(5))
        kept = {agent_id: {"agent": agent_id, "summary": "kept", "findings": [],
                           svc.REVIEWED_BASE_KEY: "b" * 12, svc.REVIEWED_DIFF_KEY: diff}
                for agent_id in ("correctness", "security", "tests")}
        posted, captured = self._review(client, reuse={"run_id": "run-earlier", "head_sha": "head12345678",
                                                       "results": kept})
        self.assertTrue(posted)
        self.assertEqual(captured, {}, "every result was reused")
        self.assertEqual(client.calls.count("list_review_threads"), 1, "only the read that plans the threads")


if __name__ == "__main__":
    unittest.main()
