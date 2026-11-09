"""Finding lifecycle (.prd/04): same-line merge, open-thread repeats, resolving fixed threads.

G1 merges the findings that land on one ``path:line`` into one inline comment.
G2 keeps a finding off the diff when the bot already has an open, current thread
on that line (unless the new finding is of a higher priority), and a P2/P3 the
same agent raised in a file where a person answered that agent's thread, resolved
on the same line or under a title sharing enough of its words. G3 resolves, after
the review is posted, the bot's own threads GitHub marks outdated that nobody
else answered and that this review does not raise again. G4 strips the
priority prefix agents put in titles so the comment header carries it once.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import service as svc
from code_review_service.service import Finding

ALL_ON = {"merge_same_line": True, "skip_open_duplicates": True, "resolve_outdated_threads": True,
          "feedback_tally": True}
BOT = "review-of-legends[bot]"
# The instructions key review_pr computes (review_instructions_key); _review patches it to this.
INSTR = "instr-test-key"


def _finding(agent: str = "correctness", path: str = "a.py", line: int = 2, priority: Optional[int] = 2,
             title: str = "Retry key misses the base", body: str = "details", confidence: float = 0.9) -> Finding:
    severity = svc.severity_from_priority(priority) or "low"
    return Finding(agent, path, line, severity, title, body, confidence, priority)


def _line_comments(findings: List[Finding], limit: int, merge_same_line: bool = False, invite_feedback: bool = False,
                   instructions: str = "") -> List[Dict[str, Any]]:
    """The inline comments review_pr posts for these findings: its line_comment_groups + render_line_comment."""
    return [svc.render_line_comment(group, invite_feedback, instructions)
            for group in svc.line_comment_groups(findings, limit, merge_same_line)]


def _visible(body: str) -> str:
    """A bot comment as GitHub shows it: without its closing metadata line (an HTML comment)."""
    text, _, last = body.rpartition("\n\n")
    assert svc._THREAD_META.fullmatch(last), body
    return text


def _meta_line(agents, titles, instr: str = INSTR, version: Any = 1) -> str:
    """The metadata line a comment of this format ends with (svc.thread_meta_line)."""
    meta = {"v": version, "instr": instr, "agents": list(agents),
            "titles": [svc.normalize_finding_title(title) for title in titles]}
    return "<!-- review-of-legends-finding %s -->" % base64.b64encode(json.dumps(meta).encode("utf-8")).decode("ascii")


def _thread(thread_id: str, path: str = "a.py", line: int = 2, *, resolved: bool = False, outdated: bool = False,
            label: str = "P2", title: str = "Retry key misses the base", author: str = "review-of-legends",
            replies: tuple = (), body: Optional[str] = None, total: Optional[int] = None,
            up: int = 0, down: int = 0, agent: str = "correctness", author_type: str = "Bot",
            instr: Optional[str] = INSTR) -> Dict[str, Any]:
    """A GraphQL ``reviewThreads`` node shaped like the 2026-10-15 PR #35 measurement.

    The first comment ends with the metadata line of THREAD_META_VERSION in
    instructions key ``instr``; ``instr=None`` gives a comment posted before
    that line existed. A reply is a login (a User), a (login, type) pair, a
    (login, type, body) triple or a (login, type, body, authorAssociation)
    quadruple; its body is "reply" and its association "MEMBER" unless given
    (``None`` leaves the field out, as a node read before it was queried).
    """
    first_body = body if body is not None else (
        f"**{label} - {title}**\n\nold details\n\n_Agent: {agent}; confidence: 0.90_"
        + ("" if instr is None else "\n\n" + _meta_line([agent], [title], instr))
    )
    url = f"https://github.com/owner/repo/pull/5#discussion_r{thread_id}"
    reactions = [{"content": "THUMBS_UP", "reactors": {"totalCount": up}},
                 {"content": "THUMBS_DOWN", "reactors": {"totalCount": down}},
                 {"content": "HEART", "reactors": {"totalCount": 4}}]
    comments = [{"author": {"__typename": author_type, "login": author}, "body": first_body, "url": url,
                 "reactionGroups": reactions}]
    for reply in replies:
        given = reply if isinstance(reply, tuple) else (reply,)
        login, kind, text, association = given + ("User", "reply", "MEMBER")[len(given) - 1:]
        comment = {"author": {"__typename": kind, "login": login}, "body": text, "url": url + "-reply"}
        if association is not None:
            comment["authorAssociation"] = association
        comments.append(comment)
    return {
        "id": thread_id,
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": path,
        "line": None if outdated else line,
        "comments": {"totalCount": len(comments) if total is None else total, "nodes": comments},
    }


class StripPriorityPrefixTest(unittest.TestCase):
    """G4: the title loses the priority the agent wrote into it."""

    def test_strips_bracket_dash_and_colon_prefixes(self) -> None:
        cases = {
            "[P2] Retry key misses the base": "Retry key misses the base",
            "[p1] lower case": "lower case",
            "P2 - Dash form": "Dash form",
            "P0: Colon form": "Colon form",
            "P3 — Em dash form": "Em dash form",
            "[P2]: bracket then colon": "bracket then colon",
            "[P2] P2 - twice": "twice",
            "  [P1]   padded  ": "padded",
            "P2 – En dash form": "En dash form",
            "P2- tight dash": "tight dash",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(svc.strip_priority_prefix(raw), expected)

    def test_leaves_titles_without_a_priority_prefix_alone(self) -> None:
        for raw in ("P2P reconnect loses state", "[P4] not a priority", "Plain title", "Uses P2 - inside",
                    "P1 -> P2 downgrade is not detected", "P3 -- double dash", "P2 :: scope", "P4 - not a priority"):
            with self.subTest(raw=raw):
                self.assertEqual(svc.strip_priority_prefix(raw), raw)

    def test_a_title_that_is_only_a_prefix_is_kept(self) -> None:
        self.assertEqual(svc.strip_priority_prefix("[P2]"), "[P2]")

    def test_the_inline_header_carries_the_priority_once(self) -> None:
        comments = _line_comments([_finding(title="[P2] Retry key misses the base")], 30)
        self.assertEqual(len(comments), 1)
        self.assertTrue(comments[0]["body"].startswith("**P2 - Retry key misses the base**\n\n"), comments[0]["body"])
        self.assertNotIn("[P2]", comments[0]["body"])


class SameLineMergeTest(unittest.TestCase):
    """G1: one inline comment per path:line."""

    def _three_on_one_line(self) -> List[Finding]:
        return [
            _finding("correctness", priority=1, title="[P1] Retry key misses the base", body="body one", confidence=0.9),
            _finding("security", priority=2, title="Reused results cross bases", body="body two", confidence=0.8),
            _finding("tests", priority=3, title="P3 - No test for base change", body="body three", confidence=0.7),
        ]

    def test_three_agents_on_one_line_post_one_comment(self) -> None:
        comments = _line_comments(self._three_on_one_line(), 30, merge_same_line=True)
        self.assertEqual(len(comments), 1)
        comment = comments[0]
        self.assertEqual((comment["path"], comment["line"], comment["side"]), ("a.py", 2, "RIGHT"))
        body = comment["body"]
        self.assertTrue(body.startswith("**P1 - Retry key misses the base**\n\n"), body)
        for agent, label, title, text in (
            ("correctness", "P1", "Retry key misses the base", "body one"),
            ("security", "P2", "Reused results cross bases", "body two"),
            ("tests", "P3", "No test for base change", "body three"),
        ):
            self.assertIn(f"**{agent} · {label} · {title}**", body)
            self.assertIn(text, body)
        self.assertNotIn("[P1]", body)
        self.assertNotIn("P3 - No test", body)

    def test_the_highest_priority_leads_whatever_the_input_order(self) -> None:
        reordered = list(reversed(self._three_on_one_line()))
        body = _line_comments(reordered, 30, merge_same_line=True)[0]["body"]
        self.assertTrue(body.startswith("**P1 - Retry key misses the base**"), body)

    def test_other_lines_stay_separate_and_the_limit_counts_comments(self) -> None:
        findings = self._three_on_one_line() + [_finding("correctness", line=3, title="Other line")]
        comments = _line_comments(findings, 30, merge_same_line=True)
        self.assertEqual([(c["path"], c["line"]) for c in comments], [("a.py", 2), ("a.py", 3)])
        limited = _line_comments(findings, 1, merge_same_line=True)
        self.assertEqual(len(limited), 1)
        self.assertEqual((limited[0]["path"], limited[0]["line"]), ("a.py", 2))
        for text in ("body one", "body two", "body three"):
            self.assertIn(text, limited[0]["body"], "the limit drops comments, never findings inside one")
        self.assertNotIn("Other line", limited[0]["body"])

    def test_merge_off_posts_one_comment_per_finding(self) -> None:
        comments = _line_comments(self._three_on_one_line(), 30, merge_same_line=False)
        self.assertEqual(len(comments), 3)

    def test_a_lone_finding_keeps_the_single_comment_format(self) -> None:
        finding = _finding(title="Lone", body="text", confidence=0.5)
        comment = _line_comments([finding], 30, merge_same_line=True)[0]
        self.assertEqual(comment["body"], "**P2 - Lone**\n\ntext\n\n_Agent: correctness; confidence: 0.50_\n\n"
                         + svc.thread_meta_line([finding]))
        self.assertEqual(_visible(comment["body"]), "**P2 - Lone**\n\ntext\n\n_Agent: correctness; confidence: 0.50_",
                         "the metadata line is an HTML comment GitHub does not show")


class PlanThreadLifecycleTest(unittest.TestCase):
    """G2 and G3 decided from GitHub's thread state and this review's findings only."""

    def plan(self, threads, findings, settings=None, login=BOT):
        return svc.plan_thread_lifecycle(threads, findings, login, settings or ALL_ON)

    def held(self, plan) -> List[str]:
        return [finding.title for finding, _ in plan.held_back]

    def resolved(self, plan) -> List[str]:
        return [thread.id for thread in plan.resolve]

    # G2 ---------------------------------------------------------------
    def test_an_open_thread_on_the_line_holds_back_equal_and_lower_priorities(self) -> None:
        threads = [_thread("T1", label="P2")]
        findings = [_finding(priority=2, title="Same"), _finding(priority=3, title="Lower")]
        plan = self.plan(threads, findings)
        self.assertEqual(self.held(plan), ["Same", "Lower"])
        self.assertEqual(plan.held_back[0][1].url, "https://github.com/owner/repo/pull/5#discussion_rT1")

    def test_a_higher_priority_still_posts(self) -> None:
        plan = self.plan([_thread("T1", label="P2")], [_finding(priority=1, title="Worse now")])
        self.assertEqual(plan.held_back, ())

    def test_outdated_threads_never_hold_back(self) -> None:
        threads = [_thread("T1", outdated=True), _thread("T2", outdated=True, resolved=True, replies=("devacme",))]
        plan = self.plan(threads, [_finding(priority=3)])
        self.assertEqual(plan.held_back, ())

    def test_a_resolved_thread_nobody_answered_holds_nothing_back(self) -> None:
        threads = [_thread("T2", resolved=True), _thread("T3", resolved=True, replies=(("review-of-legends", "Bot"),))]
        plan = self.plan(threads, [_finding(priority=3)])
        self.assertEqual(plan.held_back, ())

    def test_only_the_same_path_and_line_holds_back(self) -> None:
        threads = [_thread("T1", line=3), _thread("T2", path="b.py", line=2)]
        self.assertEqual(self.plan(threads, [_finding(priority=3)]).held_back, ())

    def test_threads_of_humans_and_other_bots_hold_nothing_back(self) -> None:
        threads = [_thread("T1", author="devacme"), _thread("T2", author="chatgpt-codex-connector")]
        self.assertEqual(self.plan(threads, [_finding(priority=3)]).held_back, ())

    def test_the_best_ranked_open_thread_on_a_line_is_the_reference(self) -> None:
        threads = [_thread("T3", label="P3"), _thread("T1", label="P1")]
        plan = self.plan(threads, [_finding(priority=2, title="Middle")])
        self.assertEqual([thread.id for _, thread in plan.held_back], ["T1"])

    def test_legacy_severity_headers_are_recognized(self) -> None:
        threads = [_thread("T1", label="HIGH")]
        self.assertEqual(self.held(self.plan(threads, [_finding(priority=1, title="Still high")])), ["Still high"])
        self.assertEqual(self.plan(threads, [_finding(priority=0, title="Critical")]).held_back, ())

    def test_skip_open_duplicates_off_holds_nothing_back(self) -> None:
        settings = {**ALL_ON, "skip_open_duplicates": False}
        self.assertEqual(self.plan([_thread("T1")], [_finding(priority=3)], settings).held_back, ())

    # G3 ---------------------------------------------------------------
    def test_an_outdated_unanswered_unraised_bot_thread_is_resolved(self) -> None:
        plan = self.plan([_thread("T1", outdated=True)], [])
        self.assertEqual(self.resolved(plan), ["T1"])
        thread = plan.resolve[0]
        self.assertEqual((thread.path, thread.title), ("a.py", "Retry key misses the base"))

    def test_a_person_identity_claims_no_threads(self) -> None:
        # Without a GitHub App the service posts as a person: nothing proves which of
        # that person's comments the service wrote, so the lifecycle claims none.
        threads = [_thread("T1", outdated=True, author="devacme", author_type="User")]
        self.assertEqual(self.resolved(self.plan(threads, [], login="devacme")), [])
        self.assertEqual(self.resolved(self.plan([_thread("T2", outdated=True)], [], login="review-of-legends")), [])

    def test_a_user_with_the_bot_login_is_not_the_bot(self) -> None:
        impostor = _thread("T1", outdated=True, author_type="User")
        self.assertEqual(self.resolved(self.plan([impostor], [])), [])
        held = svc.plan_thread_lifecycle([_thread("T2", author_type="User")], [_finding()], BOT, ALL_ON)
        self.assertEqual(held.held_back, ())

    def test_a_user_reply_with_the_bot_login_counts_as_a_reply(self) -> None:
        threads = [_thread("T1", outdated=True, replies=(("review-of-legends", "User"),))]
        self.assertEqual(self.resolved(self.plan(threads, [])), [])

    def test_a_thread_still_on_current_code_is_not_resolved(self) -> None:
        self.assertEqual(self.resolved(self.plan([_thread("T1")], [])), [])

    def test_an_already_resolved_thread_is_left_alone(self) -> None:
        self.assertEqual(self.resolved(self.plan([_thread("T1", outdated=True, resolved=True)], [])), [])

    def test_a_human_reply_keeps_the_thread_open(self) -> None:
        threads = [_thread("T1", outdated=True, replies=("devacme",))]
        self.assertEqual(self.resolved(self.plan(threads, [])), [])

    def test_a_bot_follow_up_does_not_count_as_a_reply(self) -> None:
        threads = [_thread("T1", outdated=True, replies=(("review-of-legends", "Bot"),))]
        self.assertEqual(self.resolved(self.plan(threads, [])), ["T1"])

    def test_comments_not_all_listed_keep_the_thread_open(self) -> None:
        threads = [_thread("T1", outdated=True, total=150)]
        self.assertEqual(self.resolved(self.plan(threads, [])), [])

    def test_a_re_raised_title_in_the_same_file_keeps_the_thread_open(self) -> None:
        threads = [_thread("T1", outdated=True, title="Retry key misses the base")]
        findings = [_finding(line=9, title="[P2] retry KEY misses the base")]
        self.assertEqual(self.resolved(self.plan(threads, findings)), [])

    def test_the_same_title_in_another_file_does_not_keep_it_open(self) -> None:
        threads = [_thread("T1", outdated=True)]
        findings = [_finding(path="b.py", title="Retry key misses the base")]
        self.assertEqual(self.resolved(self.plan(threads, findings)), ["T1"])

    def test_every_title_of_a_merged_thread_counts_as_re_raised(self) -> None:
        merged = _line_comments(
            [_finding("correctness", priority=1, title="Header title"), _finding("security", priority=2, title="Second title")],
            30,
            merge_same_line=True,
            instructions=INSTR,
        )[0]["body"]
        threads = [_thread("T1", outdated=True, body=merged)]
        self.assertEqual(self.resolved(self.plan(threads, [_finding(title="Second title")])), [])
        self.assertEqual(self.resolved(self.plan(threads, [_finding(title="Unrelated")])), ["T1"])

    def test_old_double_prefixed_headers_match_the_stripped_title(self) -> None:
        threads = [_thread("T1", outdated=True, title="[P2] Retry key misses the base")]
        self.assertEqual(self.resolved(self.plan(threads, [_finding(title="Retry key misses the base")])), [])

    def test_threads_not_opened_by_the_bot_are_never_resolved(self) -> None:
        threads = [
            _thread("T1", outdated=True, author="devacme"),
            _thread("T2", outdated=True, author="chatgpt-codex-connector"),
            # Bot identity but not a finding comment (gh-token mode posts as a person).
            _thread("T3", outdated=True, body="I think this needs a second look."),
        ]
        self.assertEqual(self.resolved(self.plan(threads, [])), [])

    def test_no_bot_login_means_no_bot_threads(self) -> None:
        self.assertEqual(self.resolved(self.plan([_thread("T1", outdated=True)], [], login="")), [])

    def test_resolve_outdated_threads_off_resolves_nothing(self) -> None:
        settings = {**ALL_ON, "resolve_outdated_threads": False}
        self.assertEqual(self.resolved(self.plan([_thread("T1", outdated=True)], [], settings)), [])


# A resolved bot thread on current code that a person answered (a production PR:
# "현행 유지합니다 — …", then resolved; every later review posted it again).
ANSWERED = ("devacme", "User", "현행 유지합니다 — 재시도 키는 base를 포함하지 않아도 됩니다.")


class AnsweredThreadBackstopTest(unittest.TestCase):
    """G2 backstop: a P2/P3 the same agent raised on a resolved, answered thread's line (or under its title) is not posted again."""

    def plan(self, threads, findings, settings=None, evidence=svc.ReviewEvidence()):
        return svc.plan_thread_lifecycle(threads, findings, BOT, settings or ALL_ON, evidence=evidence)

    def answered(self, plan) -> List[tuple]:
        return [(finding.title, thread.id) for finding, thread in plan.answered]

    def test_a_p2_on_an_answered_resolved_line_is_answered_not_held_back(self) -> None:
        plan = self.plan([_thread("T1", resolved=True, replies=(ANSWERED,))], [_finding(priority=2, title="Same again")])
        self.assertEqual(self.answered(plan), [("Same again", "T1")])
        self.assertEqual(plan.held_back, (), "the thread is resolved, not open")
        self.assertEqual(plan.answered[0][1].url, "https://github.com/owner/repo/pull/5#discussion_rT1")

    def test_a_p3_is_answered_too(self) -> None:
        plan = self.plan([_thread("T1", resolved=True, replies=(ANSWERED,))], [_finding(priority=3, title="Lower")])
        self.assertEqual(self.answered(plan), [("Lower", "T1")])

    def test_a_severity_only_medium_or_low_is_answered(self) -> None:
        findings = [Finding("correctness", "a.py", 2, "medium", "Medium one", "b", 0.9, None),
                    Finding("correctness", "a.py", 2, "low", "Low one", "b", 0.9, None)]
        plan = self.plan([_thread("T1", resolved=True, replies=(ANSWERED,))], findings)
        self.assertEqual(self.answered(plan), [("Medium one", "T1"), ("Low one", "T1")])

    def test_a_p0_or_p1_is_still_posted(self) -> None:
        threads = [_thread("T1", resolved=True, replies=(ANSWERED,))]
        for priority in (0, 1):
            with self.subTest(priority=priority):
                plan = self.plan(threads, [_finding(priority=priority, title="Worse")])
                self.assertEqual(plan.answered, ())
                self.assertEqual(plan.held_back, ())
        high = Finding("correctness", "a.py", 2, "high", "High one", "b", 0.9, None)
        self.assertEqual(self.plan(threads, [high]).answered, ())

    def test_another_agents_finding_on_that_line_is_still_posted(self) -> None:
        plan = self.plan([_thread("T1", resolved=True, replies=(ANSWERED,))],
                         [_finding(agent="security", priority=3, title="Other lens")])
        self.assertEqual(plan.answered, ())
        self.assertEqual(plan.held_back, ())

    def test_a_resolved_thread_without_a_human_reply_holds_nothing(self) -> None:
        for replies in ((), (("review-of-legends", "Bot", "bot follow-up"),)):
            with self.subTest(replies=replies):
                plan = self.plan([_thread("T1", resolved=True, replies=replies)], [_finding(priority=3)])
                self.assertEqual(plan.answered, ())
                self.assertEqual(plan.held_back, ())

    def test_an_open_answered_thread_on_the_line_stays_g2(self) -> None:
        open_plan = self.plan([_thread("T1", replies=(ANSWERED,))], [_finding(priority=3)])
        self.assertEqual(open_plan.answered, (), "held back on the open line, not listed twice")
        self.assertEqual([finding.title for finding, _ in open_plan.held_back], ["Retry key misses the base"])

    def test_an_answered_outdated_thread_holds_the_same_title(self) -> None:
        plan = self.plan([_thread("T1", outdated=True, resolved=True, replies=(ANSWERED,))], [_finding(priority=3)])
        self.assertEqual(self.answered(plan), [("Retry key misses the base", "T1")], "no line, but the same title")

    def test_on_another_line_or_file_an_unlike_title_holds_nothing(self) -> None:
        threads = [_thread("T1", line=3, resolved=True, replies=(ANSWERED,), title="Cache eviction drops warm entries"),
                   _thread("T2", path="b.py", resolved=True, replies=(ANSWERED,))]
        self.assertEqual(self.plan(threads, [_finding(priority=3)]).answered, ())

    def test_a_thread_read_for_another_head_counts_for_nothing(self) -> None:
        node = {**_thread("T1", resolved=True, replies=(ANSWERED,)),
                "pullRequest": {"headRefOid": "older0000000", "baseRefOid": "b" * 12}}
        evidence = svc.ReviewEvidence(head="head12345678", base="b" * 12)
        self.assertEqual(self.plan([node], [_finding(priority=3)], evidence=evidence).answered, ())
        current = {**node, "pullRequest": {"headRefOid": "head12345678", "baseRefOid": "b" * 12}}
        self.assertEqual(len(self.plan([current], [_finding(priority=3)], evidence=evidence).answered), 1)

    def test_skip_open_duplicates_off_holds_nothing(self) -> None:
        settings = {**ALL_ON, "skip_open_duplicates": False}
        plan = self.plan([_thread("T1", resolved=True, replies=(ANSWERED,))], [_finding(priority=3)], settings)
        self.assertEqual(plan.answered, ())
        self.assertEqual(plan.held_back, ())

    def test_a_reply_that_is_no_answer_holds_nothing(self) -> None:
        """Another GitHub App, or a person the repository gave no say, does not answer a finding."""
        for reply in (("chatgpt-codex-connector", "Bot", "Looks intentional."),
                      ("coderabbitai", "Bot", "Looks intentional.", "MEMBER"),
                      ("drive-by", "User", "keep it", "FIRST_TIME_CONTRIBUTOR"),
                      ("drive-by", "User", "keep it", "FIRST_TIMER"),
                      ("drive-by", "User", "keep it", "MANNEQUIN"),
                      ("drive-by", "User", "keep it", "NONE"),
                      ("devacme", "User", "keep it", None)):
            with self.subTest(reply=reply):
                node = _thread("T1", resolved=True, replies=(reply,))
                plan = self.plan([node], [_finding(priority=3)])
                self.assertEqual(plan.answered, ())
                self.assertEqual(plan.held_back, ())
                thread = svc.parse_bot_thread(node, BOT)
                self.assertFalse(thread.answered)
                self.assertTrue(thread.replied, "G3 still counts it as a reply")

    def test_a_contributor_reply_answers(self) -> None:
        """A production PR, 2026-10-21: read with the App's installation token, members who committed there read CONTRIBUTOR.

        All 191 replies on the bot's threads there came back ("User", "CONTRIBUTOR"),
        the same replies read with a person's token MEMBER, so nothing counted as answered.
        """
        node = _thread("T1", resolved=True, replies=(("devacme", "User", "현행 유지합니다", "CONTRIBUTOR"),))
        plan = self.plan([node], [_finding(priority=2, title="Same again")])
        self.assertEqual(self.answered(plan), [("Same again", "T1")])
        self.assertEqual(plan.held_back, ())
        self.assertTrue(svc.parse_bot_thread(node, BOT).answered)

    def test_the_pr_authors_reply_answers_whatever_its_association(self) -> None:
        """The PR author's answer counts with any association GitHub reports, the login compared case-insensitively."""
        for association in ("NONE", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "MANNEQUIN", None):
            node = _thread("T1", resolved=True, replies=(("devacme", "User", "현행 유지합니다", association),))
            for pr_author in ("devacme", "DevAcme"):
                with self.subTest(association=association, pr_author=pr_author):
                    plan = self.plan([node], [_finding(priority=2, title="Same again")],
                                     evidence=svc.ReviewEvidence(pr_author=pr_author))
                    self.assertEqual(self.answered(plan), [("Same again", "T1")])
                    self.assertTrue(svc.parse_bot_thread(node, BOT, pr_author).answered)
            for pr_author in ("", "someone"):
                with self.subTest(association=association, pr_author=pr_author):
                    plan = self.plan([node], [_finding(priority=2)], evidence=svc.ReviewEvidence(pr_author=pr_author))
                    self.assertEqual(plan.answered, ())
                    self.assertFalse(svc.parse_bot_thread(node, BOT, pr_author).answered)
            with self.subTest(association=association, pr_author="(not given)"):
                self.assertEqual(self.plan([node], [_finding(priority=2)]).answered, ())
                self.assertFalse(svc.parse_bot_thread(node, BOT).answered)

    def test_without_an_association_only_the_pr_author_answers(self) -> None:
        """Someone else's NONE reply, or another App's under the PR author's login, is still no answer."""
        evidence = svc.ReviewEvidence(pr_author="devacme")
        for reply in (("drive-by", "User", "keep it", "NONE"),
                      ("drive-by", "User", "keep it", "FIRST_TIME_CONTRIBUTOR"),
                      ("devacme-alt", "User", "keep it", "NONE"),
                      ("devacme", "Bot", "keep it", "NONE"),
                      ("devacme", "Bot", "keep it", "MEMBER")):
            with self.subTest(reply=reply):
                node = _thread("T1", resolved=True, replies=(reply,))
                plan = self.plan([node], [_finding(priority=3)], evidence=evidence)
                self.assertEqual(plan.answered, ())
                self.assertEqual(plan.held_back, ())
                thread = svc.parse_bot_thread(node, BOT, "devacme")
                self.assertFalse(thread.answered)
                self.assertTrue(thread.replied, "G3 still counts it as a reply")

    def test_answered_by_person_reads_the_login_then_the_association(self) -> None:
        def comment(author: Any, association: Any) -> Dict[str, Any]:
            node: Dict[str, Any] = {"author": author, "body": "keep it"}
            if association is not None:
                node["authorAssociation"] = association
            return node

        user = {"__typename": "User", "login": "Alice"}
        cases = (
            (comment(user, "NONE"), "alice", True),
            (comment(user, "NONE"), "ALICE", True),
            (comment(user, None), "alice", True),
            (comment(user, "NONE"), "", False),
            (comment(user, "NONE"), "bob", False),
            (comment(user, "CONTRIBUTOR"), "", True),
            (comment({"__typename": "Bot", "login": "Alice"}, "OWNER"), "alice", False),
            # A deleted account (GitHub's "ghost"): no author, the association still reads.
            (comment(None, "MEMBER"), "alice", True),
            (comment(None, "NONE"), "alice", False),
            # No login never matches an unknown PR author.
            (comment({"__typename": "User", "login": ""}, "NONE"), "", False),
        )
        for node, pr_author, expected in cases:
            with self.subTest(comment=node, pr_author=pr_author):
                self.assertIs(svc.answered_by_person(node, pr_author), expected)

    def test_a_thread_with_comments_left_unlisted_holds_nothing(self) -> None:
        # comments(first: 100): the answer listed may not be the latest one.
        node = _thread("T1", resolved=True, replies=(ANSWERED,), total=150)
        self.assertEqual(self.plan([node], [_finding(priority=3)]).answered, ())
        self.assertFalse(svc.parse_bot_thread(node, BOT).answered)

    def test_the_review_body_lists_answered_findings(self) -> None:
        plan = self.plan([_thread("T1", resolved=True, replies=(ANSWERED,))],
                         [_finding(priority=2, title="[P2] Same again", body="The retry key still drops the base ref.")])
        pr = {"number": 7, "head": {"sha": "abc"}}
        results = [{"agent": "a", "summary": "s", "findings": []}]
        plain = svc.build_review_body({}, pr, results, [], 0, version="v")
        self.assertNotIn("answered on an earlier thread", plain)
        body = svc.build_review_body({}, pr, results, [], 0, version="v",
                                     thread_plan=svc.ThreadPlan(answered=plan.answered))
        heading = "### Not posted again: answered on an earlier thread"
        self.assertIn(heading, body)
        section = body.split(heading, 1)[1].split("###", 1)[0]
        for text in ("**P2**", "`a.py:2`", "(correctness)", "Same again",
                     "https://github.com/owner/repo/pull/5#discussion_rT1",
                     "  > The retry key still drops the base ref."):
            self.assertIn(text, section)
        self.assertNotIn("[P2] Same again", section, "the title shows without its prefix")
        self.assertNotIn("the line has an open earlier thread", body)
        self.assertLess(body.index(heading), body.index("_Generated by Review of Legends._"))

    def test_the_answered_section_quotes_each_body_cut_before_code(self) -> None:
        """The backstop matches the agent and the line, not the finding: nothing it keeps off loses its text."""
        code = "Wrap the call.\n\n```suggestion\nretry(key, base)\n```"
        findings = [_finding(priority=2, title="With code", body=code),
                    _finding(priority=3, title="Long one", body="z" * 1000),
                    _finding(priority=3, title="Raw <details> title", body="Plain.")]
        plan = self.plan([_thread("T1", resolved=True, replies=(ANSWERED,))], findings)
        self.assertEqual(len(plan.answered), 3)
        body = svc.build_review_body({}, {"number": 7, "head": {"sha": "abc"}}, [], [], 0, version="v",
                                     thread_plan=svc.ThreadPlan(answered=plan.answered))
        section = body.split("### Not posted again: answered on an earlier thread", 1)[1].split("###", 1)[0]
        self.assertIn("  > Wrap the call.", section)
        self.assertNotIn("```", section, "cut before the fence")
        self.assertNotIn("retry(key, base)", section)
        self.assertIn("z" * 600, section)
        self.assertNotIn("z" * 601, section)
        self.assertIn("cut; the full text is in this run's agent results", section)
        self.assertIn("Raw &lt;details&gt; title", section, "a title cannot open raw HTML")
        self.assertIn("  > Plain.", section)


# A production PR (2026-10-19..21): a person answered server-architect's finding
# on ISportSummary.cs 「현행을 유지합니다…」 eight times, and the agent raised it
# again in other words, on other lines and on threads by then outdated.
LOCKING = "net: Materialize the league in the locking query"
SUMMARY = "ISportSummary.cs"
KEPT = ("devacme", "User", "현행을 유지합니다 — 잠금 조회 결과를 그대로 씁니다.", "CONTRIBUTOR")


class AnsweredTitleBackstopTest(unittest.TestCase):
    """G2 backstop by title: the same agent's P2/P3 sharing words with an answered thread's title in that file."""

    def plan(self, threads, findings, evidence=svc.ReviewEvidence()):
        return svc.plan_thread_lifecycle(threads, findings, BOT, ALL_ON, evidence=evidence)

    def answered(self, plan) -> List[tuple]:
        return [(finding.title, thread.id) for finding, thread in plan.answered]

    @staticmethod
    def locking(thread_id: str = "T1", **kwargs: Any) -> Dict[str, Any]:
        """server-architect's answered thread on ISportSummary.cs: outdated and resolved unless given."""
        options = {"path": SUMMARY, "line": 20, "outdated": True, "resolved": True, "replies": (KEPT,),
                   "agent": "server-architect", "title": LOCKING, **kwargs}
        return _thread(thread_id, **options)

    @staticmethod
    def again(title: str, agent: str = "server-architect", path: str = SUMMARY, line: int = 40,
              priority: Optional[int] = 2) -> Finding:
        return _finding(agent=agent, path=path, line=line, priority=priority, title=title)

    def test_title_words_drop_the_prefixes_and_the_small_words(self) -> None:
        self.assertEqual(svc.title_words("[P2] net: Materialize the league in the locking query"),
                         frozenset({"materialize", "league", "locking", "query"}))
        self.assertEqual(svc.title_words("shrink: Cover a 2nd case for MySQL"), frozenset({"2nd", "case", "mysql"}))
        self.assertEqual(svc.title_words("db: 잠금 조회로 리그를 불러오기"), frozenset({"잠금", "조회로", "리그를", "불러오기"}))
        self.assertEqual(svc.title_words("net: the"), frozenset())
        self.assertEqual(svc.title_words("net:"), frozenset())

    def test_title_words_drop_one_letter_tokens_and_the_agents_template_words(self) -> None:
        """PR #73 review: a particle split off next to an identifier, or "Add a test for", is no shared word."""
        self.assertEqual(svc.title_words("`GetLeague`의 결과를 재사용"), frozenset({"getleague", "결과를", "재사용"}))
        self.assertEqual(svc.title_words("`retry_key`가 `base_ref`를 누락"), frozenset({"retry_key", "base_ref"}))
        self.assertEqual(svc.title_words("빈 리그 케이스에 대한 테스트를 추가하세요"), frozenset({"리그", "케이스에"}))
        self.assertEqual(svc.title_words("Add a test for the empty league case"), frozenset({"empty", "league", "case"}))
        self.assertEqual(svc.title_words("reuse: Use the existing league_mapper helper"), frozenset({"league_mapper"}))
        self.assertEqual(svc.title_words("x y z"), frozenset())
        for word in ("add", "missing", "test", "tests", "no", "not", "cover", "covers", "use", "unused", "existing",
                     "helper", "into", "check", "verify", "exercise", "assert", "remove", "drop", "keep", "preserve",
                     "ensure", "avoid", "handle", "make", "only", "do", "new",
                     "테스트", "테스트를", "추가", "추가하세요", "추가하라", "검증", "검증하세요", "확인", "확인하세요", "누락",
                     "없음", "대한", "사용", "사용하지", "않는", "헬퍼", "제거", "제거하세요", "삭제", "삭제하세요"):
            with self.subTest(word=word):
                self.assertIn(word, svc.TITLE_STOPWORDS)

    def assertMatches(self, cases) -> None:
        for title, other, expected in cases:
            with self.subTest(title=title, other=other):
                self.assertIs(svc.titles_match(title, other), expected)
                self.assertIs(svc.titles_match(other, title), expected)

    def test_titles_match_on_half_the_shorter_titles_words(self) -> None:
        """Shared words over the shorter title's: a production PR's rewordings (LOCKING has 4 words)."""
        self.assertMatches((
            (LOCKING, "net: Return the league entity from the locking read", True),  # 2 of 4
            (LOCKING, "net: Load league entities in the locking query", True),  # 3 of 4
            (LOCKING, "net: Fetch the league through the locking query", True),  # 3 of 4
            (LOCKING, "Preserve manual league fields in team-driven saves", False),  # 1 shared
            ("Cover destructive seed reconciliation with a MySQL test",
             "Test the destructive seed reconciliation on MySQL", True),  # 4 of 4
            ("Remove the redundant Docker availability probes",
             "Drop the redundant Docker availability probe", True),  # 3 of 4
            ("Collapse the catalog-filter branch matrices", "Collapse repeated catalog-filter branches", True),  # 3 of 5
            ("잠금 조회로 리그를 불러오기", "리그를 잠금 조회로 불러오기", True),
            ("리그 목록 정렬 테스트 추가", "리그 목록 정렬 검증 누락", True),  # 3 of 3 under two templates
            ("The", "the", False),  # no words on either side
            ("net:", "net:", False),
        ))

    def test_distinct_findings_in_the_agents_template_words_do_not_match(self) -> None:
        """PR #73 review: template words and split-off particles were all these pairs shared."""
        self.assertMatches((
            # A production PR after v1.0.6, the tests agent on one file: "reconciliation" alone.
            ("Cover sport-only mismatches in seed reconciliation",
             "Cover a multi-batch delete-only reconciliation", False),
            ("Add a test for the empty league case", "Add a test for the rename path", False),
            ("Missing test for retry backoff", "Missing test for timeout path", False),
            ("delete: unused helper parse_row", "delete: unused helper format_cell", False),
            ("reuse: Use the existing league_mapper helper", "reuse: Use the existing date_utils helper", False),
            ("`retry_key`가 `base_ref`를 누락", "`cache_key`가 `ttl`를 무시", False),  # no word in common
            ("`GetLeague`의 결과를 재사용", "`GetTeam`의 결과를 캐시", False),  # "결과를" alone
            # The same templates under a Korean review language: 테스트 and 추가, 누락, 검증 and 없음, …
            ("빈 리그 케이스 테스트 추가", "이름 변경 경로 테스트 추가", False),
            ("빈 리그 케이스에 대한 테스트를 추가하세요", "이름 변경 경로에 대한 테스트를 추가하세요", False),
            ("재시도 백오프 테스트 누락", "타임아웃 경로 테스트 누락", False),
            ("리그 ID 검증 없음", "팀 ID 검증 없음", False),  # "id" alone
            ("사용하지 않는 헬퍼 parse_row 제거", "사용하지 않는 헬퍼 format_cell 제거", False),
            # A production PR after v1.0.6, simplify: "lock" and "single" (2 of 4), but a yagni and a reuse cut.
            ("yagni: Remove the single-table catalog-lock layer",
             "reuse: Remove the duplicate single-league lock helper", False),
        ))

    def test_title_lens_is_the_prefix_title_words_drops(self) -> None:
        for title, lens in (("net: Materialize the league", "net"), ("[P2] reuse: Use the mapper", "reuse"),
                            ("YAGNI:  Remove the layer", "yagni"), ("delete-only: x", "delete-only"),
                            ("Materialize the league", ""), ("P2: Materialize the league", ""),
                            ("Cover retry_key: base", "")):
            with self.subTest(title=title):
                self.assertEqual(svc.title_lens(title), lens)

    def test_titles_in_different_simplify_lenses_never_match(self) -> None:
        """Two of simplify's remedy tags must be the same one; any other lens, or none, compares by words.

        agents/server-architect.md puts locking under both db: and net:, so #4834's
        locking topic can come back in the other viewpoint.
        """
        same = "Return the duplicate single-league lock helper"
        self.assertMatches((
            ("reuse: Remove the duplicate single-league lock helper", f"reuse: {same}", True),
            ("yagni: Remove the duplicate single-league lock helper", f"reuse: {same}", False),  # all 4 words
            ("delete: Collapse the duplicate single-league lock helper", f"shrink: {same}", False),
            ("Remove the duplicate single-league lock helper", f"reuse: {same}", True),
            (LOCKING, "Load league entities in the locking query", True),
            (LOCKING, "db: Load league entities in the locking query", True),  # 3 of 4
            ("capacity: League lock held across the network call", "net: League lock held across a network call",
             True),
            (LOCKING, "reuse: Load league entities in the locking query", True),  # one remedy tag, one viewpoint
        ))
        self.assertEqual(svc.SIMPLIFY_LENSES, frozenset({"delete", "stdlib", "native", "reuse", "yagni", "shrink"}))

    def test_an_identifier_is_one_word(self) -> None:
        """A backticked span, or a token joined by "_" or "." between word characters, is kept whole."""
        self.assertEqual(svc.title_words("`retry_key` ignores the base"), frozenset({"retry_key", "ignores", "base"}))
        self.assertEqual(svc.title_words("net: ISportSummary.GetLeague reads league_id twice."),
                         frozenset({"isportsummary.getleague", "reads", "league_id", "twice"}))
        self.assertEqual(svc.title_words("`GetLeague(id)` 결과를 재사용"), frozenset({"getleague(id)", "결과를", "재사용"}))
        self.assertEqual(svc.title_words("`league_id`가 null일 때 예외"), frozenset({"league_id", "null", "예외"}))
        self.assertEqual(svc.title_words("`x` and `_`"), frozenset(), "a one-letter span is no word either")
        self.assertMatches((
            ("`retry_key` ignores the base", "`retry_key` not cleared on reset", False),  # retry_key alone
            # retry and key, 2 of the shorter title's 4: the words rule cannot tell these apart.
            ("Make retry key survive eviction", "Retry key loses the base ref across pushes", True),
            ("`league_id`가 null일 때 예외", "`team_id`가 null일 때 크래시", False),  # null alone
            # The identifier is not "retry" and "key", but ignores, base and ref are 3 of 4.
            ("`retry_key` ignores the base ref", "Retry key ignores the base ref", True),
        ))

    def test_titles_match_needs_two_shared_words(self) -> None:
        self.assertMatches((
            ("Cache keys", "Cache entries", False),  # 1 of 2: half, but one word
            ("Cache keys", "Keys cache", True),  # 2 of 2
            ("Cache", "cache", False),  # the same one word
            ("Cache keys", "Cache keys drop the base on every retry of a stale entry", True),  # 2 of 2
        ))

    def test_titles_match_at_half_the_shorter_title(self) -> None:
        five = "Retry key drops the base ref"  # retry, key, drops, base, ref
        self.assertMatches((
            (five, "Retry key ignores stale cache entries", False),  # 2 of 5
            (five, "Retry key base skips stale entries", True),  # 3 of 5
            ("Retry key drops base", "Retry key ignores stale cache entries", True),  # 2 of 4
        ))

    def test_the_finding_in_other_words_on_an_outdated_thread_is_answered(self) -> None:
        thread = svc.parse_bot_thread(self.locking(), BOT)
        self.assertEqual((thread.title, thread.line, thread.answered), (LOCKING, None, True))
        for title in ("net: Load league entities in the locking query",
                      "net: Fetch the league through the locking query",
                      "[P2] net: Load league entities in the locking query",
                      "net: Return the league entity from the locking read"):  # 2 of 4: half the shorter title
            with self.subTest(title=title):
                plan = self.plan([self.locking()], [self.again(title)])
                self.assertEqual(self.answered(plan), [(title, "T1")])
                self.assertEqual(plan.held_back, ())

    def test_resolved_or_not_outdated_or_not_an_answered_thread_counts(self) -> None:
        finding = self.again("net: Load league entities in the locking query", priority=3)
        for outdated, resolved in ((True, False), (False, True), (False, False)):
            with self.subTest(outdated=outdated, resolved=resolved):
                plan = self.plan([self.locking(outdated=outdated, resolved=resolved)], [finding])
                self.assertEqual(self.answered(plan), [(finding.title, "T1")])
                self.assertEqual(plan.held_back, (), "the thread is on line 20, the finding on 40")

    def test_the_tests_agents_seed_finding_in_other_words_is_answered(self) -> None:
        thread = _thread("T7", path="sport_reference.seed.cs", line=12, resolved=True, replies=(KEPT,), agent="tests",
                         title="Cover destructive seed reconciliation with a MySQL test")
        finding = self.again("Test the destructive seed reconciliation on MySQL", agent="tests",
                             path="sport_reference.seed.cs", line=30)
        self.assertEqual(self.answered(self.plan([thread], [finding])), [(finding.title, "T7")])

    @staticmethod
    def merged(thread_id: str, group: List[Finding], **kwargs: Any) -> Dict[str, Any]:
        """An answered, resolved, outdated thread opened by the comment review_pr posts for ``group`` on one line."""
        (comment,) = _line_comments(group, 30, merge_same_line=True, instructions=INSTR)
        return _thread(thread_id, path=group[0].path, outdated=True, resolved=True, replies=(KEPT,),
                       body=comment["body"], **kwargs)

    def test_one_agents_merged_comment_matches_through_its_metadata_titles(self) -> None:
        """The header shows the worst finding's title; the other one is only in the metadata line."""
        group = [_finding(agent="server-architect", path=SUMMARY, line=20, priority=2,
                          title="Preserve manual league fields in team-driven saves"),
                 _finding(agent="server-architect", path=SUMMARY, line=20, priority=3, title=LOCKING)]
        node = self.merged("T1", group)
        thread = svc.parse_bot_thread(node, BOT)
        self.assertEqual(thread.title, "Preserve manual league fields in team-driven saves")
        self.assertEqual(thread.agents, frozenset({"server-architect"}))
        self.assertIn(svc.normalize_finding_title(LOCKING), thread.titles)
        title = "net: Load league entities in the locking query"
        self.assertFalse(svc.titles_match(title, thread.title), "not through the header")
        self.assertEqual(self.answered(self.plan([node], [self.again(title)])), [(title, "T1")])

    def test_a_merged_comment_of_several_agents_holds_no_title(self) -> None:
        """PR #73 review: its agents and titles are not paired, so security's title is no answer to correctness."""
        group = [_finding(agent="correctness", line=2, priority=2, title="Fix retry keys"),
                 _finding(agent="security", line=2, priority=3, title="Reject expired tokens")]
        node = self.merged("T1", group)
        self.assertEqual(svc.parse_bot_thread(node, BOT).agents, frozenset({"correctness", "security"}))
        for agent in ("correctness", "security"):
            with self.subTest(agent=agent):
                plan = self.plan([node], [_finding(agent=agent, line=3, priority=2, title="Reject expired tokens")])
                self.assertEqual(plan.answered, ())
                self.assertEqual(plan.held_back, ())
        # On its current line the same-line rule still holds either agent's P2/P3.
        current = {**node, "isOutdated": False, "line": 2}
        plan = self.plan([current], [_finding(agent="correctness", line=2, priority=2, title="Reject expired tokens")])
        self.assertEqual(self.answered(plan), [("Reject expired tokens", "T1")])

    def test_the_thread_keeps_the_latest_answer(self) -> None:
        """ReviewThread.answer: the body of the last reply answered_by_person counts, "" when none does."""
        replies = (("devacme", "User", "현행 유지합니다", "MEMBER"),
                   ("devacme", "User", "  수정했습니다. 커밋 abc123", "CONTRIBUTOR"),
                   ("drive-by", "User", "Fixed?", "NONE"),
                   ("review-of-legends", "Bot", "Done."))
        self.assertEqual(svc.parse_bot_thread(self.locking(replies=replies), BOT).answer, "  수정했습니다. 커밋 abc123")
        author = svc.parse_bot_thread(self.locking(replies=replies), BOT, "drive-by")
        self.assertEqual(author.answer, "Fixed?", "the PR author's reply answers")
        for node in (self.locking(replies=()), self.locking(replies=(("drive-by", "User", "Fixed", "NONE"),)),
                     self.locking(total=150)):
            with self.subTest(node=node["comments"]["nodes"][1:]):
                thread = svc.parse_bot_thread(node, BOT)
                self.assertEqual((thread.answered, thread.answer), (False, ""))

    def test_answer_says_fixed_reads_how_the_answer_starts(self) -> None:
        """Only a fixed marker the answer starts with: one later in it (a reason for keeping) is no fix."""
        fixed = ("수정했습니다.", "추가했습니다(MySQL). 200건 경계 테스트입니다.", "반영했습니다", "고쳤어요", "바꿨습니다",
                 "삭제했습니다", "제거했습니다", "옮겼습니다", "적용했습니다", "  \n수정했습니다", "수정했지만 일부는 유지합니다",
                 "Fixed in abc123", "fixed.", "Added a MySQL test.", "Done", "done!", "Addressed in the last push",
                 "Updated.", "Removed it.", "Changed to a join.", "Applied the suggestion.",
                 # Markdown before the first word (PR #73 review).
                 "**Added** a MySQL test.", "__Added__ a MySQL test.", "_done_", "`fixed` in abc123", "__수정했습니다__",
                 "**수정했습니다**", "- 수정했습니다", "* 수정했습니다", "1. 수정했습니다", "2) Fixed", "# Fixed",
                 "- **Fixed** in abc123", "\n  - 수정했습니다")
        kept = ("현행을 유지합니다 — 잠금 조회 결과를 그대로 씁니다.", "유지합니다", "추후 반영하겠습니다", "Won't fix: intended.",
                "won't fix", "Not fixed: by design.", "Intended; a fix would add a round trip.",
                "이 부분은 수정했지만 유지합니다", "Kept as is. Fixed the other call site instead.", "Donegal is fine",
                "Fixedness is intended", "", "   ",
                # A quote is someone else's words; a strikethrough takes the word back.
                "> 수정했습니다 — 라고 하셨는데 현행 유지합니다", "> Fixed?\n\nWon't fix: intended.",
                "~~수정했습니다~~ 유지합니다", "~~Fixed~~ Kept as is.", "**Kept** as is.", "- 유지합니다",
                "__Donegal__ is fine")
        for text in fixed:
            with self.subTest(text=text):
                self.assertTrue(svc.answer_says_fixed(text))
        for text in kept:
            with self.subTest(text=text):
                self.assertFalse(svc.answer_says_fixed(text))

    def test_a_fixed_answer_holds_nothing_by_title(self) -> None:
        """A production PR, the tests agent: a later gap in the test added for an answered finding is a new finding.

        "Exercise reconciliation across the 200-league batch boundary" was answered
        「추가했습니다(MySQL). …」; "Cover a multi-batch delete-only reconciliation"
        then shares batch and reconciliation with it, and the author fixed that too.
        """
        title = "Cover a multi-batch delete-only reconciliation"
        added = ("devacme", "User", "추가했습니다(MySQL). 200개 리그 경계를 넘는 배치 테스트입니다.", "CONTRIBUTOR")
        thread = _thread("T3", path="SeedReconcilerTests.cs", outdated=True, resolved=True, replies=(added,),
                         agent="tests", title="Exercise reconciliation across the 200-league batch boundary")
        finding = self.again(title, agent="tests", path="SeedReconcilerTests.cs")
        self.assertTrue(svc.titles_match(title, "Exercise reconciliation across the 200-league batch boundary"))
        self.assertEqual(self.plan([thread], [finding]).answered, ())
        for answer in ("현행을 유지합니다 — 경계는 한 번만 검사합니다.", "유지합니다", "추후 보강하겠습니다", "Won't fix: covered by the e2e run.",
                       "이 부분은 수정했지만 유지합니다"):
            with self.subTest(answer=answer):
                node = _thread("T3", path="SeedReconcilerTests.cs", outdated=True, resolved=True, agent="tests",
                               replies=(("devacme", "User", answer, "CONTRIBUTOR"),),
                               title="Exercise reconciliation across the 200-league batch boundary")
                self.assertEqual(self.answered(self.plan([node], [finding])), [(title, "T3")])

    def test_a_fixed_answer_still_holds_on_its_line(self) -> None:
        """The same-line rule reads any answer: a P2/P3 the same agent raises there again is listed, not posted."""
        node = self.locking(outdated=False, line=40, replies=(("devacme", "User", "수정했습니다", "MEMBER"),))
        plan = self.plan([node], [self.again("net: Preserve manual league fields in team-driven saves")])
        self.assertEqual([thread.id for _, thread in plan.answered], ["T1"])
        plan = self.plan([node], [self.again("net: Load league entities in the locking query", line=41)])
        self.assertEqual(plan.answered, (), "by title it holds nothing")

    def test_another_agents_finding_is_posted(self) -> None:
        plan = self.plan([self.locking()], [self.again("net: Load league entities in the locking query", agent="tests")])
        self.assertEqual(plan.answered, ())

    def test_a_p0_or_p1_is_posted(self) -> None:
        title = "net: Load league entities in the locking query"
        for finding in (self.again(title, priority=0), self.again(title, priority=1),
                        Finding("server-architect", SUMMARY, 40, "high", title, "b", 0.9, None)):
            with self.subTest(finding=finding):
                self.assertEqual(self.plan([self.locking()], [finding]).answered, ())
        medium = Finding("server-architect", SUMMARY, 40, "medium", title, "b", 0.9, None)
        self.assertEqual(self.answered(self.plan([self.locking()], [medium])), [(title, "T1")])

    def test_a_thread_no_person_answered_holds_nothing(self) -> None:
        finding = self.again("net: Load league entities in the locking query")
        for node in (self.locking(replies=()),
                     self.locking(replies=(("review-of-legends", "Bot", "bot follow-up"),)),
                     self.locking(replies=(("drive-by", "User", "현행을 유지합니다", "NONE"),)),
                     self.locking(total=150)):
            with self.subTest(node=node["comments"]["nodes"][1:], total=node["comments"]["totalCount"]):
                self.assertEqual(self.plan([node], [finding]).answered, ())
        author = self.locking(replies=(("drive-by", "User", "현행을 유지합니다", "NONE"),))
        plan = self.plan([author], [finding], evidence=svc.ReviewEvidence(pr_author="drive-by"))
        self.assertEqual(self.answered(plan), [(finding.title, "T1")], "the PR author's reply answers")

    def test_an_unlike_title_in_the_same_file_is_posted(self) -> None:
        plan = self.plan([self.locking()], [self.again("net: Preserve manual league fields in team-driven saves")])
        self.assertEqual(plan.answered, ())

    def test_only_the_same_file_by_its_current_name_counts(self) -> None:
        title = "net: Load league entities in the locking query"
        self.assertEqual(self.plan([self.locking()], [self.again(title, path="SportSummary.cs")]).answered, ())
        renamed = svc.ReviewEvidence(renamed=(("ISportSummaryOld.cs", SUMMARY),), files=frozenset({SUMMARY}))
        plan = self.plan([self.locking(path="ISportSummaryOld.cs")], [self.again(title)], evidence=renamed)
        self.assertEqual(self.answered(plan), [(title, "T1")], "a thread from before the rename")
        # ISportSummary.cs is the file now and the one renamed away: the thread may be about either.
        ambiguous = svc.ReviewEvidence(renamed=((SUMMARY, "Moved.cs"), ("ISportSummaryOld.cs", SUMMARY)),
                                       files=frozenset({SUMMARY, "Moved.cs"}))
        self.assertEqual(self.plan([self.locking()], [self.again(title)], evidence=ambiguous).answered, ())

    def test_the_newest_like_thread_is_named(self) -> None:
        # T2 is the repeat a person answered again; T3 does not match.
        threads = [self.locking("T1"), self.locking("T2", title="net: Load league entities in the locking query"),
                   self.locking("T3", title="net: Preserve manual league fields in team-driven saves")]
        plan = self.plan(threads, [self.again("net: Load the league entities in the locking query")])
        self.assertEqual([thread.id for _, thread in plan.answered], ["T2"])
        plan = self.plan(threads[:1] + threads[2:], [self.again("net: Load the league entities in the locking query")])
        self.assertEqual([thread.id for _, thread in plan.answered], ["T1"])

    def test_the_line_rules_come_first_and_list_a_finding_once(self) -> None:
        title = "net: Load league entities in the locking query"
        on_line = self.locking("T5", outdated=False, line=40, title="net: Preserve manual league fields")
        plan = self.plan([on_line, self.locking("T6")], [self.again(title)])
        self.assertEqual(self.answered(plan), [(title, "T5")], "the same-line answered thread")
        still_open = self.locking("T8", outdated=False, resolved=False, line=40, replies=(), title="Other lens")
        plan = self.plan([still_open, self.locking("T6")], [self.again(title)])
        self.assertEqual([(finding.title, thread.id) for finding, thread in plan.held_back], [(title, "T8")])
        self.assertEqual(plan.answered, ())

    def test_review_pr_keeps_the_reworded_repeat_off_the_diff_and_lists_it(self) -> None:
        client = _LifecycleClient([_thread("T1", outdated=True, resolved=True, replies=(KEPT,), title=LOCKING)])
        self.assertTrue(_review(client, {
            "correctness": [_raw(line=3, title="net: Load league entities in the locking query",
                                 body="Load the league in the locking read.")],
            "security": [_raw(line=4, title="Reused results cross bases")],
        }))
        review = client.reviews[0]
        self.assertEqual(len(review["comments"]), 1, "only the other agent's finding is posted inline")
        self.assertNotIn("locking query", review["comments"][0]["body"])
        heading = "### Not posted again: answered on an earlier thread"
        section = review["body"].split(heading, 1)[1].split("###", 1)[0]
        for text in ("**P2**", "`a.py:3`", "(correctness)", "net: Load league entities in the locking query",
                     "https://github.com/owner/repo/pull/5#discussion_rT1", "  > Load the league in the locking read."):
            self.assertIn(text, section)
        self.assertNotIn("Reused results cross bases", section)
        self.assertEqual(client.resolves(), [], "an answered thread is not resolved")

    def test_review_pr_holds_a_repeat_of_the_second_title_in_one_agents_merged_comment(self) -> None:
        """A merged comment of correctness alone: its header is the P2, LOCKING is the P3 under it."""
        group = [_finding(line=2, priority=2, title="Preserve manual league fields in team-driven saves"),
                 _finding(line=2, priority=3, title=LOCKING)]
        client = _LifecycleClient([self.merged("T1", group)])
        title = "net: Load league entities in the locking query"
        self.assertTrue(_review(client, {
            "correctness": [_raw(line=3, title=title, body="Load the league in the locking read.")],
            "security": [_raw(line=4, title="Reused results cross bases")],
        }))
        review = client.reviews[0]
        self.assertEqual(len(review["comments"]), 1, "only the other agent's finding is posted inline")
        self.assertNotIn("locking query", review["comments"][0]["body"])
        heading = "### Not posted again: answered on an earlier thread"
        section = review["body"].split(heading, 1)[1].split("###", 1)[0]
        for text in ("**P2**", "`a.py:3`", "(correctness)", title,
                     "https://github.com/owner/repo/pull/5#discussion_rT1", "  > Load the league in the locking read."):
            self.assertIn(text, section)
        self.assertNotIn("Reused results cross bases", section)


class LifecycleSettingsTest(unittest.TestCase):
    def _write(self, tmp: Path, payload: dict) -> Path:
        path = tmp / "reviewer.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _base(self) -> dict:
        return {"repositories": ["o/r"], "agents": [{"id": i, "prompt_path": "x"} for i in ("a", "b", "c")]}

    def test_absent_means_every_behavior_on(self) -> None:
        self.assertEqual(svc.finding_lifecycle_settings({}), ALL_ON)
        self.assertEqual(svc.finding_lifecycle_settings({"finding_lifecycle": {"_comment": "x"}}), ALL_ON)

    def test_each_switch_turns_off_alone(self) -> None:
        for key in ALL_ON:
            with self.subTest(key=key):
                settings = svc.finding_lifecycle_settings({"finding_lifecycle": {key: False}})
                self.assertEqual(settings, {**ALL_ON, key: False})

    def test_load_config_refuses_a_bad_shape(self) -> None:
        for block, message in (
            ([], "config.finding_lifecycle must be an object"),
            ({"merge_same_line": "no"}, "config.finding_lifecycle.merge_same_line must be true or false"),
            ({"resolve_outdated_threads": None}, "config.finding_lifecycle.resolve_outdated_threads must be true or false"),
        ):
            with self.subTest(block=block), tempfile.TemporaryDirectory() as tmp:
                payload = {**self._base(), "finding_lifecycle": block}
                with self.assertRaises(svc.ServiceError) as caught:
                    svc.load_config(self._write(Path(tmp), payload))
                self.assertIn(message, str(caught.exception))

    def test_the_shipped_config_turns_every_behavior_on(self) -> None:
        from tests.shipped_config import load_shipped_config

        config = load_shipped_config()
        self.assertIn("_finding_lifecycle_comment", config)
        self.assertEqual({key: config["finding_lifecycle"][key] for key in ALL_ON}, ALL_ON)
        self.assertEqual(svc.finding_lifecycle_settings(config), ALL_ON)


class GraphQLClientTest(unittest.TestCase):
    def _client(self) -> svc.GitHubClient:
        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"):
            return svc.GitHubClient("owner/repo")

    def test_graphql_posts_to_the_graphql_endpoint_and_returns_data(self) -> None:
        client = self._client()
        with mock.patch.object(client, "request", return_value={"data": {"viewer": {"login": "x"}}}) as request:
            self.assertEqual(client.graphql("query { viewer { login } }", {"a": 1}), {"viewer": {"login": "x"}})
        request.assert_called_once_with("POST", "graphql", {"query": "query { viewer { login } }", "variables": {"a": 1}})

    def test_graphql_errors_raise_service_error(self) -> None:
        client = self._client()
        for response in (
            {"errors": [{"message": "Resource not accessible by integration"}], "data": None},
            {"errors": [{"message": "Something went wrong"}], "data": {"repository": {"pullRequest": None}}},
            {"data": None},
            ["not", "an", "object"],
            None,
        ):
            with self.subTest(response=response), mock.patch.object(client, "request", return_value=response):
                with self.assertRaises(svc.ServiceError):
                    client.graphql("query { x }")

    def test_list_review_threads_walks_every_page(self) -> None:
        client = self._client()
        pages = [
            {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": True, "endCursor": "C1"}, "nodes": [_thread("T1")]}}}}},
            {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": "C2"}, "nodes": [_thread("T2")]}}}}},
        ]
        with mock.patch.object(client, "request", side_effect=pages) as request:
            threads = client.list_review_threads(35)
        self.assertEqual([thread["id"] for thread in threads], ["T1", "T2"])
        first, second = (call.args[2]["variables"] for call in request.call_args_list)
        self.assertEqual(first, {"owner": "owner", "name": "repo", "number": 35, "cursor": None})
        self.assertEqual(second["cursor"], "C1")
        self.assertIn("reviewThreads(first: 100", request.call_args_list[0].args[2]["query"])

    def test_list_review_threads_refuses_a_missing_pull_request(self) -> None:
        client = self._client()
        with mock.patch.object(client, "request", return_value={"data": {"repository": {"pullRequest": None}}}):
            with self.assertRaises(svc.ServiceError):
                client.list_review_threads(35)

    def test_resolve_review_thread_sends_the_mutation_and_checks_the_answer(self) -> None:
        client = self._client()
        answer = {"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
        with mock.patch.object(client, "request", return_value=answer) as request:
            client.resolve_review_thread("T1")
        payload = request.call_args.args[2]
        self.assertIn("resolveReviewThread", payload["query"])
        self.assertEqual(payload["variables"], {"threadId": "T1"})
        not_resolved = {"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": False}}}}
        with mock.patch.object(client, "request", return_value=not_resolved):
            with self.assertRaises(svc.ServiceError):
                client.resolve_review_thread("T1")


class _LifecycleClient:
    """A fake GitHub that has review threads, records the order of calls and what was posted."""

    def __init__(self, threads=None, *, list_error=None, resolve_fail=(), review_error=None) -> None:
        self.repo = "owner/repo"
        self.threads = threads or []
        self.list_error = list_error
        self.resolve_fail = set(resolve_fail)
        self.review_error = review_error
        self.head = "head12345678"
        self.base = "b" * 12
        self.calls: List[Any] = []
        self.reviews: List[Dict[str, Any]] = []
        self.progress_bodies: List[str] = []

    def bot_login(self) -> str:
        return BOT

    def _with_refs(self, thread):
        """GitHub gives every thread with its pull request's head and base of the moment."""
        return {**thread, "pullRequest": {"headRefOid": self.head, "baseRefOid": self.base}}

    def list_files(self, number):
        return [{
            "filename": "a.py",
            "status": "modified",
            "additions": 3,
            "deletions": 0,
            "changes": 3,
            "patch": "@@ -1,1 +1,4 @@\n context\n+two\n+three\n+four\n",
        }]

    def list_review_threads(self, number):
        self.calls.append("list_review_threads")
        if self.list_error is not None:
            raise self.list_error
        return [self._with_refs(thread) for thread in self.threads]

    def review_thread(self, thread_id):
        """GraphQL ``node(id)``: the thread as it is now, with the PR's current head and base."""
        self.calls.append(("review_thread", thread_id))
        if self.list_error is not None:
            raise self.list_error
        for thread in self.threads:
            if thread["id"] == thread_id:
                return self._with_refs(thread)
        raise svc.ServiceError(f"no review thread {thread_id}")

    def resolve_review_thread(self, thread_id):
        self.calls.append(("resolve", thread_id))
        if thread_id in self.resolve_fail:
            raise svc.ServiceError("GitHub GraphQL failed: boom")
        return {"id": thread_id, "isResolved": True}

    def create_review(self, number, commit_id, body, comments, event="COMMENT"):
        self.calls.append("create_review")
        if self.review_error is not None:
            raise self.review_error
        self.reviews.append({"body": body, "comments": comments, "event": event})
        return {"id": 1, "html_url": "https://github.com/owner/repo/pull/5#pullrequestreview-1"}

    def upsert_issue_comment(self, number, marker, body):
        if marker == svc.PROGRESS_MARKER:
            self.progress_bodies.append(body)
        return {"id": 2}

    def create_commit_status(self, sha, state, description, target_url="", context=svc.REQUIRED_STATUS_CONTEXT):
        return {"id": 3}

    def resolves(self) -> List[str]:
        return [call[1] for call in self.calls if isinstance(call, tuple) and call[0] == "resolve"]


def _review(client, findings_by_agent: Dict[str, list], *, lifecycle=None, dry_run: bool = False,
            failing: tuple = (), reuse=None, instructions: Any = INSTR, run_agent: Any = None,
            pr_user: Optional[Dict[str, Any]] = None):
    """review_pr on PR 5, opened by ``pr_user`` (the REST ``user``; login "someone" unless given)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        config = {
            "agents": [{"id": agent_id, "prompt_path": "x"} for agent_id in ("correctness", "security", "tests")],
            "agent_runner": {"command": ["true"]},
            "min_successful_agents": 0,
            "max_review_comments": 30,
            "max_diff_chars": 180000,
            "work_dir": str(tmp_path / "work"),
            "marker": svc.MARKER,
            "summary_marker": svc.SUMMARY_MARKER,
            "progress_marker": svc.PROGRESS_MARKER,
            "progress_comment": {"enabled": True},
            "approval": {"enabled": False},
            "executive_summary": {"enabled": False},
        }
        if lifecycle is not None:
            config["finding_lifecycle"] = lifecycle
        pr = {"number": 5, "head": {"sha": "head12345678"}, "base": {"sha": "b" * 12}, "title": "t", "body": "",
              "user": pr_user if pr_user is not None else {"login": "someone"}}

        def fake_run_agent(agent, *args, **kwargs):
            if agent["id"] in failing:
                return {"agent": agent["id"], "error": "runner exited 1"}
            return {"agent": agent["id"], "summary": "ok", "findings": findings_by_agent.get(agent["id"], [])}

        with mock.patch.object(svc, "run_agent", run_agent or fake_run_agent), \
             mock.patch.object(svc, "clone_pr_source", lambda repo, n, h, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True)), \
             mock.patch.object(svc, "load_repository_rules", lambda *a, **k: ""), \
             mock.patch.object(svc, "review_instructions_key",
                               instructions if callable(instructions) else lambda *a, **k: instructions):
            return svc.review_pr(client, config, tmp_path / "config" / "reviewer.json", pr, {"reviewed": {}},
                                 state_path=tmp_path / "state.json", dry_run=dry_run, reuse_results=reuse)


def _raw(line: int = 2, priority: int = 2, title: str = "Retry key misses the base", body: str = "details") -> dict:
    return {"path": "a.py", "line": line, "priority": priority, "title": title, "body": body, "confidence": 0.8}


SAME_LINE = {
    "correctness": [_raw(priority=1, title="[P1] Retry key misses the base", body="one")],
    "security": [_raw(priority=2, title="Reused results cross bases", body="two")],
    "tests": [_raw(priority=3, title="No test for a base change", body="three")],
}


class ReviewPrLifecycleTest(unittest.TestCase):
    def test_same_line_findings_post_one_comment_and_keep_every_finding_in_the_body(self) -> None:
        client = _LifecycleClient()
        self.assertTrue(_review(client, SAME_LINE))
        review = client.reviews[0]
        self.assertEqual(len(review["comments"]), 1)
        self.assertTrue(review["comments"][0]["body"].startswith("**P1 - Retry key misses the base**"))
        body = review["body"]
        for line in ("(correctness): [P1] Retry key misses the base", "(security): Reused results cross bases",
                     "(tests): No test for a base change"):
            self.assertIn(line, body, "the Findings list stays per finding")
        self.assertIn("Line comments: 3/3", body, "all three findings are carried by the one comment")
        self.assertNotIn("Not posted again: the line has an open earlier thread", body)
        self.assertNotIn("Resolving earlier threads", body)

    def test_an_open_thread_keeps_a_repeat_off_the_diff_and_names_it_in_the_body(self) -> None:
        client = _LifecycleClient([_thread("T1", line=2, label="P2")])
        self.assertTrue(_review(client, {"correctness": [_raw(priority=2)]}))
        review = client.reviews[0]
        self.assertEqual(review["comments"], [])
        self.assertIn("### Not posted again: the line has an open earlier thread", review["body"])
        self.assertIn("`a.py:2`", review["body"])
        self.assertIn("https://github.com/owner/repo/pull/5#discussion_rT1", review["body"])
        self.assertEqual(client.resolves(), [])

    def test_a_higher_priority_on_an_open_line_still_posts(self) -> None:
        client = _LifecycleClient([_thread("T1", line=2, label="P2")])
        self.assertTrue(_review(client, {"correctness": [_raw(priority=1, title="Now worse")]}))
        self.assertEqual(len(client.reviews[0]["comments"]), 1)

    def test_an_answered_resolved_thread_keeps_the_repeat_off_the_diff_and_names_it_in_the_body(self) -> None:
        client = _LifecycleClient([_thread("T1", line=2, label="P2", resolved=True, replies=(ANSWERED,))])
        self.assertTrue(_review(client, {
            "correctness": [_raw(priority=2, body="The retry key still drops the base ref.")],
            "security": [_raw(priority=2, title="Reused results cross bases")],
        }))
        review = client.reviews[0]
        self.assertEqual(len(review["comments"]), 1, "only the other agent's finding is posted inline")
        self.assertTrue(review["comments"][0]["body"].startswith("**P2 - Reused results cross bases**"),
                        review["comments"][0]["body"])
        self.assertNotIn("Retry key misses the base", review["comments"][0]["body"])
        heading = "### Not posted again: answered on an earlier thread"
        self.assertIn(heading, review["body"])
        section = review["body"].split(heading, 1)[1].split("###", 1)[0]
        for text in ("**P2**", "`a.py:2`", "(correctness)", "Retry key misses the base",
                     "https://github.com/owner/repo/pull/5#discussion_rT1",
                     "  > The retry key still drops the base ref."):
            self.assertIn(text, section)
        self.assertNotIn("Reused results cross bases", section)
        self.assertNotIn("Not posted again: the line has an open earlier thread", review["body"])
        self.assertEqual(client.resolves(), [])

    def test_the_pr_authors_answer_keeps_the_repeat_off_the_diff(self) -> None:
        """review_pr hands G2 the PR's ``user.login``: the author's reply answers with any association."""
        reply = ("devacme", "User", "현행 유지합니다", "NONE")
        heading = "### Not posted again: answered on an earlier thread"
        for pr_user, held in (({"login": "devacme", "type": "User"}, True),
                              ({"login": "DevAcme", "type": "User"}, True),
                              ({"login": "someone", "type": "User"}, False),
                              # A bot's PR: bots answer nothing (mentions.pr_author).
                              ({"login": "devacme", "type": "Bot"}, False)):
            with self.subTest(pr_user=pr_user):
                client = _LifecycleClient([_thread("T1", line=2, label="P2", resolved=True, replies=(reply,))])
                self.assertTrue(_review(client, {"correctness": [_raw(priority=2)]}, pr_user=pr_user))
                review = client.reviews[0]
                self.assertEqual(len(review["comments"]), 0 if held else 1)
                self.assertEqual(heading in review["body"], held)

    def test_an_outdated_thread_is_resolved_after_the_review_is_posted(self) -> None:
        client = _LifecycleClient([_thread("T1", outdated=True, title="Fixed bug")])
        self.assertTrue(_review(client, {"correctness": [_raw(line=3, title="Something new")]}))
        self.assertEqual(client.resolves(), ["T1"])
        self.assertLess(client.calls.index("create_review"), client.calls.index(("resolve", "T1")))
        body = client.reviews[0]["body"]
        self.assertIn("### Resolving earlier threads", body)
        self.assertIn("`a.py`: Fixed bug", body)
        self.assertIn("https://github.com/owner/repo/pull/5#discussion_rT1", body)
        self.assertIn("**Phase:** Complete", client.progress_bodies[-1])
        self.assertIn("resolved 1/1 earlier threads", client.progress_bodies[-1])

    def test_a_reply_that_arrives_during_posting_keeps_the_thread_open(self) -> None:
        client = _LifecycleClient([_thread("T1", outdated=True, title="Fixed bug"),
                                   _thread("T2", line=4, outdated=True, path="a.py", title="Other fixed")])
        original = client.create_review

        def create_review_then_reply(*args, **kwargs):
            result = original(*args, **kwargs)
            client.threads = [_thread("T1", outdated=True, title="Fixed bug", replies=("devacme",)),
                              _thread("T2", line=4, outdated=True, path="a.py", title="Other fixed")]
            return result

        client.create_review = create_review_then_reply
        self.assertTrue(_review(client, {"correctness": [_raw(line=3, title="Something new")]}))
        self.assertEqual(client.resolves(), ["T2"], "the answered thread is re-checked and left open")
        self.assertIn("1 skipped", client.progress_bodies[-1])

    def test_a_failed_re_check_resolves_nothing(self) -> None:
        client = _LifecycleClient([_thread("T1", outdated=True, title="Fixed bug")])
        original = client.create_review

        def create_review_then_break(*args, **kwargs):
            result = original(*args, **kwargs)
            client.list_error = svc.ServiceError("GitHub GraphQL failed: boom")
            return result

        client.create_review = create_review_then_break
        self.assertTrue(_review(client, {"correctness": [_raw(line=3, title="Something new")]}))
        self.assertEqual(client.resolves(), [])
        self.assertIn(("review_thread", "T1"), client.calls, "the thread is read again before resolving")

    def test_one_failed_resolve_does_not_stop_the_rest(self) -> None:
        threads = [_thread("T1", outdated=True, title="One"), _thread("T2", outdated=True, title="Two")]
        client = _LifecycleClient(threads, resolve_fail=("T1",))
        self.assertTrue(_review(client, {}))
        self.assertEqual(client.resolves(), ["T1", "T2"])
        self.assertIn("resolved 1/2 earlier threads", client.progress_bodies[-1])

    def test_a_listing_failure_posts_as_before_and_resolves_nothing(self) -> None:
        client = _LifecycleClient([_thread("T1", outdated=True)], list_error=svc.ServiceError("GitHub GraphQL failed: 502"))
        self.assertTrue(_review(client, SAME_LINE))
        self.assertEqual(len(client.reviews), 1)
        self.assertEqual(len(client.reviews[0]["comments"]), 1, "merging does not depend on the listing")
        self.assertEqual(client.resolves(), [])
        self.assertIn("Earlier review threads could not be read", client.progress_bodies[-1])

    def test_a_client_without_thread_support_still_posts(self) -> None:
        class _Legacy:
            """Only what review_pr needed before the lifecycle: no bot_login, no threads."""

            repo = "owner/repo"
            list_files = _LifecycleClient.list_files

            def __init__(self) -> None:
                self.reviews: List[Dict[str, Any]] = []

            def create_review(self, number, commit_id, body, comments, event="COMMENT"):
                self.reviews.append({"body": body, "comments": comments})
                return {"id": 1}

            def upsert_issue_comment(self, number, marker, body):
                return {"id": 2}

        client = _Legacy()
        self.assertTrue(_review(client, SAME_LINE))
        self.assertEqual(len(client.reviews[0]["comments"]), 1)

    def test_no_resolve_when_github_rejects_the_review(self) -> None:
        rejected = svc.ServiceError("GitHub API POST failed: 422 Unprocessable")
        rejected.status_code = 422  # type: ignore[attr-defined]
        client = _LifecycleClient([_thread("T1", outdated=True)], review_error=rejected)
        with self.assertRaises(svc.ServiceError):
            _review(client, {})
        self.assertEqual(client.resolves(), [])

    def test_no_resolve_when_the_review_outcome_is_unknown(self) -> None:
        unknown = svc.ServiceError("GitHub API POST failed: connection reset")
        unknown.transport_error = True  # type: ignore[attr-defined]
        self.assertTrue(svc.post_outcome_is_uncertain(unknown))
        client = _LifecycleClient([_thread("T1", outdated=True)], review_error=unknown)
        with self.assertRaises(svc.ServiceError):
            _review(client, {})
        self.assertEqual(client.resolves(), [])

    def test_a_dry_run_shows_the_plan_but_resolves_nothing(self) -> None:
        client = _LifecycleClient([_thread("T1", outdated=True, title="Fixed bug")])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(_review(client, {}, dry_run=True))
        self.assertEqual(client.resolves(), [])
        self.assertNotIn("create_review", client.calls)
        self.assertIn("### Resolving earlier threads", out.getvalue())

    def test_every_switch_off_is_todays_behavior(self) -> None:
        off = {"merge_same_line": False, "skip_open_duplicates": False, "resolve_outdated_threads": False,
               "feedback_tally": False}
        client = _LifecycleClient([_thread("T1", line=2), _thread("T2", outdated=True)])
        self.assertTrue(_review(client, SAME_LINE, lifecycle=off))
        self.assertNotIn("list_review_threads", client.calls, "no listing when nothing needs it")
        self.assertEqual(len(client.reviews[0]["comments"]), 3)
        for comment in client.reviews[0]["comments"]:
            self.assertNotIn("React with", comment["body"])
            self.assertTrue(_visible(comment["body"]).endswith("; confidence: 0.80_"), comment["body"])
        self.assertEqual(client.resolves(), [])

    def test_each_switch_off_alone(self) -> None:
        client = _LifecycleClient([_thread("T1", line=2, label="P2"), _thread("T2", outdated=True, title="Gone")])
        self.assertTrue(_review(client, {"correctness": [_raw(priority=2)]}, lifecycle={"skip_open_duplicates": False}))
        self.assertEqual(len(client.reviews[0]["comments"]), 1, "repeat posted when the skip is off")
        self.assertEqual(client.resolves(), ["T2"])

        client = _LifecycleClient([_thread("T1", line=2, label="P2"), _thread("T2", outdated=True, title="Gone")])
        self.assertTrue(_review(client, {"correctness": [_raw(priority=2)]}, lifecycle={"resolve_outdated_threads": False}))
        self.assertEqual(client.reviews[0]["comments"], [])
        self.assertEqual(client.resolves(), [])
        self.assertNotIn("Resolving earlier threads", client.reviews[0]["body"])

        client = _LifecycleClient()
        self.assertTrue(_review(client, SAME_LINE, lifecycle={"merge_same_line": False}))
        self.assertEqual(len(client.reviews[0]["comments"]), 3)


class ReviewBodySectionsTest(unittest.TestCase):
    def test_sections_appear_only_when_non_empty(self) -> None:
        pr = {"number": 7, "head": {"sha": "abc"}}
        results = [{"agent": "a", "summary": "s", "findings": []}]
        plain = svc.build_review_body({}, pr, results, [], 0, version="v")
        self.assertNotIn("Not posted again: the line has an open earlier thread", plain)
        self.assertNotIn("Resolving earlier threads", plain)
        plan = svc.plan_thread_lifecycle(
            [_thread("T1", line=2, title="Open one"), _thread("T2", outdated=True, path="b.py", title="Fixed one")],
            [_finding(priority=3, title="[P3] Repeat")],
            BOT,
            ALL_ON,
        )
        body = svc.build_review_body({}, pr, results, [], 0, version="v",
                                     thread_plan=svc.ThreadPlan(held_back=plan.held_back, resolve=plan.resolve))
        self.assertIn("### Not posted again: the line has an open earlier thread", body)
        self.assertIn("- **P3** `a.py:2` (correctness): Repeat", body)
        self.assertIn("https://github.com/owner/repo/pull/5#discussion_rT1", body)
        self.assertIn("### Resolving earlier threads", body)
        self.assertIn("- `b.py`: Fixed one", body)
        self.assertIn("https://github.com/owner/repo/pull/5#discussion_rT2", body)
        self.assertLess(body.index("Not posted again"), body.index("_Generated by Review of Legends._"))


class FeedbackTallyTest(unittest.TestCase):
    """G5: people rate each bot finding with a reaction; the next review counts them per agent."""

    def test_reactions_on_bot_threads_are_counted_per_agent(self) -> None:
        threads = [
            _thread("T1", up=2, down=0, agent="correctness"),
            _thread("T2", line=3, up=0, down=1, agent="security", resolved=True),
            _thread("T3", line=4, up=1, down=1, agent="correctness", outdated=True),
            _thread("T4", line=5, agent="tests"),
        ]
        plan = svc.plan_thread_lifecycle(threads, [], BOT, ALL_ON)
        self.assertEqual(plan.feedback, (("correctness", 3, 1), ("security", 0, 1)))
        self.assertEqual(plan.feedback_findings, 3, "threads without a thumbs reaction are not counted")

    def test_hearts_and_other_reactions_are_ignored(self) -> None:
        plan = svc.plan_thread_lifecycle([_thread("T1")], [], BOT, ALL_ON)
        self.assertEqual(plan.feedback, ())
        self.assertEqual(plan.feedback_findings, 0)

    def test_a_merged_comment_credits_every_agent_listed(self) -> None:
        body = svc.render_line_comment([_finding("correctness", priority=1, body="one"),
                                        _finding("security", title="Reused results cross bases", body="two")],
                                       invite_feedback=True)["body"]
        plan = svc.plan_thread_lifecycle([_thread("T1", body=body, down=1)], [], BOT, ALL_ON)
        self.assertEqual(plan.feedback, (("correctness", 0, 1), ("security", 0, 1)))
        self.assertEqual((plan.feedback_findings, plan.feedback_up, plan.feedback_down), (1, 0, 1),
                         "one reaction on one comment counts once in the PR total")
        pr = {"number": 7, "head": {"sha": "abc"}}
        rendered = svc.build_review_body({}, pr, [{"agent": "a", "summary": "s", "findings": []}], [], 0, version="v",
                                         thread_plan=plan)
        self.assertIn("👍 0 · 👎 1 on 1 earlier finding", rendered)
        self.assertIn("- **correctness**: 👍 0 · 👎 1", rendered)
        self.assertIn("- **security**: 👍 0 · 👎 1", rendered)

    def test_threads_of_humans_and_other_bots_are_not_counted(self) -> None:
        threads = [_thread("T1", author="someone", up=5), _thread("T2", author="chatgpt-codex-connector", up=5)]
        plan = svc.plan_thread_lifecycle(threads, [], BOT, ALL_ON)
        self.assertEqual(plan.feedback, ())

    def test_feedback_tally_off_counts_nothing(self) -> None:
        plan = svc.plan_thread_lifecycle([_thread("T1", up=3)], [], BOT, dict(ALL_ON, feedback_tally=False))
        self.assertEqual(plan.feedback, ())

    def test_the_review_body_reports_the_tally(self) -> None:
        pr = {"number": 7, "head": {"sha": "abc"}}
        results = [{"agent": "a", "summary": "s", "findings": []}]
        plain = svc.build_review_body({}, pr, results, [], 0, version="v")
        self.assertNotIn("Feedback on earlier findings", plain)
        body = svc.build_review_body({}, pr, results, [], 0, version="v", thread_plan=svc.ThreadPlan(
                                     feedback=(("correctness", 3, 1), ("security", 0, 1)), feedback_findings=3,
                                     feedback_up=3, feedback_down=2))
        self.assertIn("### Feedback on earlier findings", body)
        self.assertIn("👍 3 · 👎 2 on 3 earlier findings", body)
        self.assertIn("- **correctness**: 👍 3 · 👎 1", body)
        self.assertIn("- **security**: 👍 0 · 👎 1", body)

    def test_inline_comments_invite_a_rating_only_when_the_tally_is_on(self) -> None:
        lone = [_finding()]
        merged = [_finding(agent="correctness"), _finding(agent="security", title="Other", priority=3)]
        self.assertNotIn("👍", svc.render_line_comment(lone)["body"])
        self.assertIn("React with 👍 or 👎", svc.render_line_comment(lone, invite_feedback=True)["body"])
        rated = svc.render_line_comment(merged, invite_feedback=True)["body"]
        self.assertEqual(rated.count("React with 👍 or 👎"), 1)

    def test_a_posted_review_carries_the_tally_and_the_invitation(self) -> None:
        client = _LifecycleClient([_thread("T1", line=4, up=1, down=2, agent="security")])
        self.assertTrue(_review(client, {"correctness": [_raw(line=2, title="New one")]}))
        review = client.reviews[0]
        self.assertIn("👍 1 · 👎 2 on 1 earlier finding", review["body"])
        self.assertIn("React with 👍 or 👎", review["comments"][0]["body"])

    def test_the_query_asks_for_reaction_counts(self) -> None:
        self.assertIn("reactionGroups", svc.REVIEW_THREADS_QUERY)
        self.assertIn("feedback_tally", svc.FINDING_LIFECYCLE_KEYS)


class ResolveOnlyOnCompleteEvidenceTest(unittest.TestCase):
    """G3 resolves a thread only on what this review really re-checked (PR #37 review)."""

    def test_a_thread_of_an_agent_that_failed_this_run_stays_open(self) -> None:
        client = _LifecycleClient([_thread("T1", outdated=True, title="Unfixed", agent="security"),
                                   _thread("T2", line=4, outdated=True, title="Fixed", agent="correctness")])
        self.assertTrue(_review(client, {"correctness": [_raw(line=3, title="Something new")]}, failing=("security",)))
        self.assertEqual(client.resolves(), ["T2"], "security did not run to the end, so its thread was not re-checked")
        body = client.reviews[0]["body"]
        self.assertNotIn("`a.py`: Unfixed", body)
        self.assertIn("`a.py`: Fixed", body)

    def test_a_thread_whose_agent_cannot_be_read_stays_open(self) -> None:
        body = "**P2 - Fixed bug**\n\nold details"
        plan = svc.plan_thread_lifecycle([_thread("T1", outdated=True, body=body)], [], BOT, ALL_ON,
                                         evidence=svc.ReviewEvidence(succeeded=frozenset({"correctness"})))
        self.assertEqual(plan.resolve, ())

    def test_a_re_raise_on_a_line_the_diff_did_not_add_keeps_the_thread_open(self) -> None:
        client = _LifecycleClient([_thread("T1", outdated=True, title="Retry key misses the base")])
        # Line 1 is a context line of the fake patch: no inline comment is possible there.
        self.assertTrue(_review(client, {"correctness": [_raw(line=1, title="[P2] Retry key misses the base")]}))
        self.assertEqual(client.resolves(), [])
        self.assertNotIn("Resolving earlier threads", client.reviews[0]["body"])

    def test_an_absolute_path_re_raise_keeps_the_thread_open(self) -> None:
        results = [{"agent": "correctness", "findings": [
            {"code_location": {"absolute_file_path": "/tmp/run/src/a.py", "line_range": {"start": 9}},
             "title": "P2 - Retry key misses the base", "body": "b"}]}]
        names = ["a.py", "src/a.py"]
        threads = [_thread("T1", path="src/a.py", outdated=True), _thread("T2", path="a.py", line=3, outdated=True)]

        def resolved(raised):
            plan = svc.plan_thread_lifecycle(threads, [], BOT, ALL_ON, evidence=svc.ReviewEvidence(raised=raised))
            return [thread.id for thread in plan.resolve]

        # Checked out at /tmp/run/src, the path names the root-level a.py, not src/a.py.
        under_src = svc.raised_finding_keys(results, names, roots=["/tmp/run/src"])
        self.assertEqual(under_src, frozenset({("a.py", "retry key misses the base")}))
        self.assertEqual(resolved(under_src), ["T1"])
        under_run = svc.raised_finding_keys(results, names, roots=["/tmp/run", "/private/tmp/run"])
        self.assertEqual(resolved(under_run), ["T2"])
        # Without the checkout root, every file the path may name keeps its thread open.
        unknown = svc.raised_finding_keys(results, names)
        self.assertEqual(unknown, frozenset({("a.py", "retry key misses the base"),
                                             ("src/a.py", "retry key misses the base")}))
        self.assertEqual(resolved(unknown), [])
        self.assertEqual(svc.raised_finding_keys([{"agent": "c", "findings": [
            {"path": "a.py", "line": 1, "title": "T", "body": "b"}]}], names, roots=["/tmp/run"]),
            frozenset({("a.py", "t")}), "a repository path stays exact")

    def test_review_pr_maps_absolute_paths_under_its_clone(self) -> None:
        client = _LifecycleClient([_thread("T1", outdated=True, title="Unfixed")])
        captured = {}
        original = svc.raised_finding_keys

        def spy(results, filenames=(), roots=()):
            captured["roots"] = tuple(roots)
            return original(results, filenames, roots)

        with mock.patch.object(svc, "raised_finding_keys", spy):
            self.assertTrue(_review(client, {}))
        self.assertTrue(captured["roots"] and all(root.endswith("/source") for root in captured["roots"]),
                        captured)

    def test_nothing_is_resolved_once_the_pull_request_moved_to_another_head(self) -> None:
        client = _LifecycleClient([_thread("T1", outdated=True, title="Fixed bug")])
        original = client.create_review

        def create_review_then_push(*args, **kwargs):
            result = original(*args, **kwargs)
            client.head = "newer0000000"
            return result

        client.create_review = create_review_then_push
        self.assertTrue(_review(client, {"correctness": [_raw(line=3, title="Something new")]}))
        self.assertEqual(client.resolves(), [])
        self.assertIn("resolved 0/1", client.progress_bodies[-1])
        self.assertIn("newer head", client.progress_bodies[-1])

    def test_each_thread_is_read_again_right_before_it_is_resolved(self) -> None:
        threads = [_thread("T1", outdated=True, title="One"), _thread("T2", line=4, outdated=True, title="Two")]
        client = _LifecycleClient(threads)
        original = client.resolve_review_thread

        def resolve_then_someone_answers_t2(thread_id):
            result = original(thread_id)
            if thread_id == "T1":
                client.threads = [threads[0], _thread("T2", line=4, outdated=True, title="Two",
                                                      replies=("devacme",))]
            return result

        client.resolve_review_thread = resolve_then_someone_answers_t2
        self.assertTrue(_review(client, {}))
        self.assertEqual(client.resolves(), ["T1"], "the reply on T2 during T1's resolve keeps T2 open")
        self.assertIn(("review_thread", "T2"), client.calls)

    def test_the_review_thread_query_reads_the_head_and_the_same_fields(self) -> None:
        self.assertIn("headRefOid", svc.REVIEW_THREAD_QUERY)
        self.assertIn("reactionGroups", svc.REVIEW_THREAD_QUERY)
        self.assertNotIn("originalLine", svc.REVIEW_THREADS_QUERY)

    def test_review_thread_returns_the_node_or_raises(self) -> None:
        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"):
            client = svc.GitHubClient("owner/repo")
        node = {**_thread("T1"), "pullRequest": {"headRefOid": "abc"}}
        with mock.patch.object(client, "request", return_value={"data": {"node": node}}) as request:
            self.assertEqual(client.review_thread("T1")["pullRequest"]["headRefOid"], "abc")
        self.assertEqual(request.call_args.args[2]["variables"], {"id": "T1"})
        with mock.patch.object(client, "request", return_value={"data": {"node": None}}):
            with self.assertRaises(svc.ServiceError):
                client.review_thread("T1")


class SizeBoundsTest(unittest.TestCase):
    """GitHub refuses a review whose body or any comment exceeds 65,536 characters."""

    def test_the_earlier_thread_sections_list_at_most_twenty_entries(self) -> None:
        threads = [_thread(f"T{i}", line=i + 1, outdated=True, title=f"Fixed {i} " + "x" * 120) for i in range(300)]
        plan = svc.plan_thread_lifecycle(threads, [], BOT, ALL_ON)
        self.assertEqual(len(plan.resolve), 300, "the plan still resolves every thread")
        open_threads = [_thread(f"O{i}", line=i + 1, title=f"Open {i}") for i in range(300)]
        findings = [_finding(line=i + 1, title=f"Open {i}") for i in range(300)]
        held = svc.plan_thread_lifecycle(open_threads, findings, BOT, ALL_ON).held_back
        self.assertEqual(len(held), 300)
        pr = {"number": 7, "head": {"sha": "abc"}}
        body = svc.build_review_body({}, pr, [{"agent": "a", "summary": "s", "findings": []}], [], 0, version="v",
                                     thread_plan=svc.ThreadPlan(held_back=held, resolve=plan.resolve))
        resolving = body.split("### Resolving earlier threads", 1)[1]
        self.assertEqual(resolving.count("discussion_rT"), svc.MAX_THREAD_SECTION_ITEMS)
        self.assertIn("…and 280 more", resolving)
        not_posted = body.split("### Not posted again", 1)[1].split("###", 1)[0]
        self.assertEqual(not_posted.count("discussion_rO"), svc.MAX_THREAD_SECTION_ITEMS)
        self.assertIn("…and 280 more", not_posted)
        self.assertLess(len(body), 20000)

    def test_a_merged_comment_stays_under_githubs_limit_and_keeps_every_entry(self) -> None:
        group = [_finding(agent=f"agent-{i}", title=f"Finding {i}", body="y" * 5000) for i in range(30)]
        body = svc.render_line_comment(group, invite_feedback=True)["body"]
        self.assertLessEqual(len(body), svc.MAX_COMMENT_CHARS)
        self.assertLess(svc.MAX_COMMENT_CHARS, 65536)
        for i in range(30):
            self.assertIn(f"**agent-{i} · P2 · Finding {i}**", body)
        self.assertTrue(_visible(body).endswith("React with 👍 or 👎 to rate these findings._"))
        thread = svc.parse_bot_thread(_thread("T1", body=body), BOT)
        self.assertEqual(len(thread.agents), 30)

    def test_a_line_with_more_findings_than_one_comment_holds_is_split(self) -> None:
        # Longest agent id the config accepts, longest title normalize_findings keeps:
        # their entry lines alone would pass the limit in one comment.
        findings = [_finding(agent="a" * 63 + str(i % 10), title=f"{i:03d} " + "t" * 156, body="b")
                    for i in range(300)]
        groups = svc.line_comment_groups(findings, 30, merge_same_line=True)
        self.assertEqual([len(group) for group in groups],
                         [svc.MAX_FINDINGS_PER_COMMENT] * 7 + [300 - 7 * svc.MAX_FINDINGS_PER_COMMENT])
        self.assertEqual(sum(len(group) for group in groups), 300, "every finding is still carried inline")
        for group in groups:
            body = svc.render_line_comment(group, invite_feedback=True)["body"]
            self.assertLessEqual(len(body), svc.MAX_COMMENT_CHARS)
            self.assertTrue(_visible(body).endswith("to rate these findings._"))
            thread = svc.parse_bot_thread(_thread("T1", body=body), BOT)
            self.assertEqual(len(thread.titles), len(group))
        self.assertEqual(len(svc.line_comment_groups(findings, 3, merge_same_line=True)), 3,
                         "the comment limit counts the split comments")

    def test_a_lone_finding_over_the_limit_is_cut_but_still_parses(self) -> None:
        body = svc.render_line_comment([_finding(body="z" * 70000)], invite_feedback=True)["body"]
        self.assertLessEqual(len(body), svc.MAX_COMMENT_CHARS)
        thread = svc.parse_bot_thread(_thread("T1", body=body), BOT)
        self.assertEqual(thread.agents, frozenset({"correctness"}))


class EmptyTitleTest(unittest.TestCase):
    def test_an_empty_title_gets_the_default_and_its_thread_is_recognized(self) -> None:
        for title in ("", "   ", None):
            with self.subTest(title=title):
                raw = {"path": "a.py", "line": 2, "priority": 2, "title": title, "body": "b"}
                findings = svc.normalize_findings([{"agent": "correctness", "findings": [raw]}], {"a.py": {2}})
                self.assertEqual(findings[0].title, "Review finding")
                body = svc.render_line_comment(findings)["body"]
                self.assertIsNotNone(svc.parse_bot_thread(_thread("T1", body=body), BOT))


class ReviewFollowUpTest(unittest.TestCase):
    """Second-pass review of PR #37: files the agents never saw, person tokens, title prefixes."""

    def test_a_thread_on_a_file_the_agents_did_not_see_stays_open(self) -> None:
        files = [
            {"filename": "a.py", "status": "modified", "patch": "@@ -1,1 +1,2 @@\n x\n+" + "y" * 400},
            {"filename": "b.py", "status": "modified", "patch": "@@ -1,1 +1,2 @@\n x\n+z"},
            {"filename": "big.lock", "status": "modified"},
        ]
        shown = svc.diff_and_shown_files(files, max_chars=300)[1]
        self.assertEqual(shown, frozenset(), "a.py was cut, b.py never reached, big.lock has no patch")
        self.assertEqual(svc.diff_and_shown_files(files, max_chars=180000)[1], frozenset({"a.py", "b.py"}))
        evidence = svc.ReviewEvidence(seen=frozenset({"a.py"}))
        threads = [_thread("T1", path="b.py", outdated=True), _thread("T2", path="big.lock", outdated=True),
                   _thread("T3", path="gone.py", outdated=True), _thread("T4", path="a.py", outdated=True)]
        plan = svc.plan_thread_lifecycle(threads, [], BOT, ALL_ON, evidence=evidence)
        self.assertEqual([thread.id for thread in plan.resolve], ["T4"],
                         "only a file whose whole patch the agents read is re-checked; gone.py may have moved")

    def test_review_pr_keeps_threads_on_unseen_files_open(self) -> None:
        client = _LifecycleClient([_thread("T1", path="huge.lock", outdated=True, title="Old"),
                                   _thread("T2", outdated=True, title="Fixed")])
        original = client.list_files

        def files_with_an_omitted_patch(number):
            return original(number) + [{"filename": "huge.lock", "status": "modified"}]

        client.list_files = files_with_an_omitted_patch
        self.assertTrue(_review(client, {}))
        self.assertEqual(client.resolves(), ["T2"])

    def test_without_a_github_app_no_rating_is_asked_for(self) -> None:
        client = _LifecycleClient()
        client.bot_login = lambda: "devacme"
        self.assertTrue(_review(client, SAME_LINE))
        self.assertNotIn("React with", client.reviews[0]["comments"][0]["body"])

    def test_a_severity_header_keeps_the_agents_priority_in_the_title(self) -> None:
        findings = svc.normalize_findings(
            [{"agent": "correctness", "findings": [{"path": "a.py", "line": 2, "title": "P0: crash on empty diff",
                                                     "body": "b", "severity": "low"}]}], {"a.py": {2}})
        self.assertIsNone(findings[0].priority)
        body = svc.render_line_comment(findings)["body"]
        self.assertTrue(body.startswith("**LOW - P0: crash on empty diff**"), body)
        thread = svc.parse_bot_thread(_thread("T1", body=body), BOT)
        self.assertIn("crash on empty diff", thread.titles, "the veto still compares stripped titles")

    def test_both_queries_ask_for_every_field_the_parser_reads(self) -> None:
        for query in (svc.REVIEW_THREADS_QUERY, svc.REVIEW_THREAD_QUERY):
            for field in ("id", "isResolved", "isOutdated", "path", "line", "__typename", "login", "body", "url",
                          "reactionGroups", "content", "authorAssociation"):
                with self.subTest(field=field):
                    self.assertRegex(query, rf"\b{field}\b")
            self.assertRegex(query, r"comments\(first: 100\)\s*\{\s*totalCount")

    def test_a_reply_by_a_deleted_account_keeps_the_thread_open(self) -> None:
        thread = _thread("T1", outdated=True, title="Fixed bug")
        thread["comments"]["nodes"].append({"author": None, "body": "reply", "url": "u"})
        thread["comments"]["totalCount"] = 2
        self.assertEqual(svc.plan_thread_lifecycle([thread], [], BOT, ALL_ON).resolve, ())
        client = _LifecycleClient([thread])
        self.assertTrue(_review(client, {}))
        self.assertEqual(len(client.reviews), 1)
        self.assertEqual(client.resolves(), [])

    def test_the_comment_limit_keeps_the_worst_lines(self) -> None:
        findings = [_finding(line=2, priority=3, title="Minor"), _finding(line=5, priority=0, title="Crash")]
        comments = _line_comments(findings, 1, merge_same_line=True)
        self.assertEqual([comment["line"] for comment in comments], [5])

    def test_a_multi_line_title_round_trips(self) -> None:
        body = svc.render_line_comment([_finding(title="Retry key\nmisses the base")], invite_feedback=True)["body"]
        thread = svc.parse_bot_thread(_thread("T1", body=body), BOT)
        self.assertIsNotNone(thread)
        self.assertEqual(thread.title, "Retry key misses the base")
        self.assertEqual(thread.agents, frozenset({"correctness"}))

    def test_a_finding_without_priority_ranks_by_severity(self) -> None:
        high = Finding("correctness", "a.py", 2, "high", "Sev high", "b", 0.9, None)
        low = Finding("correctness", "a.py", 2, "low", "Sev low", "b", 0.9, None)
        plan = svc.plan_thread_lifecycle([_thread("T1", label="P2")], [high, low], BOT, ALL_ON)
        self.assertEqual([finding.title for finding, _ in plan.held_back], ["Sev low"])

    def test_the_listing_follows_every_cursor(self) -> None:
        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"):
            client = svc.GitHubClient("owner/repo")
        pages = [{"data": {"repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": {"hasNextPage": index < 2, "endCursor": f"C{index + 1}"}, "nodes": [_thread(f"T{index + 1}")]}}}}}
            for index in range(3)]
        with mock.patch.object(client, "request", side_effect=pages) as request:
            threads = client.list_review_threads(35)
        self.assertEqual([thread["id"] for thread in threads], ["T1", "T2", "T3"])
        self.assertEqual([call.args[2]["variables"]["cursor"] for call in request.call_args_list], [None, "C1", "C2"])

    def test_a_null_resolve_answer_is_not_a_resolve(self) -> None:
        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"):
            client = svc.GitHubClient("owner/repo")
        with mock.patch.object(client, "request", return_value={"data": {"resolveReviewThread": None}}):
            with self.assertRaises(svc.ServiceError):
                client.resolve_review_thread("T1")


class GateRoundTwoTest(unittest.TestCase):
    """Rule 8 review gate, round 1 (astra-zhuge): reused results and split-comment ranking."""

    def test_a_reused_result_gives_its_agent_no_resolve_credit(self) -> None:
        client = _LifecycleClient([_thread("T1", outdated=True, title="Old", agent="security"),
                                   _thread("T2", line=4, outdated=True, title="Fixed", agent="correctness")])
        kept = {"agent": "security", "summary": "kept", "findings": [],
                svc.REVIEWED_BASE_KEY: "b" * 12, svc.REVIEWED_DIFF_KEY: svc.review_diff_fingerprint(client.list_files(5))}
        reuse = {"run_id": "run-earlier", "head_sha": "head12345678", "results": {"security": kept}}
        self.assertTrue(_review(client, {}, reuse=reuse))
        self.assertIn("Reused from the previous attempt", client.progress_bodies[-1])
        self.assertEqual(client.resolves(), ["T2"], "security read an earlier attempt's diff, not this one")

    def test_a_comment_with_a_reused_finding_records_no_instructions_key(self) -> None:
        client = _LifecycleClient([])
        kept = {"agent": "security", "summary": "kept",
                "findings": [_raw(line=2, title="Kept from the earlier attempt")],
                svc.REVIEWED_BASE_KEY: "b" * 12, svc.REVIEWED_DIFF_KEY: svc.review_diff_fingerprint(client.list_files(5))}
        reuse = {"run_id": "run-earlier", "head_sha": "head12345678", "results": {"security": kept}}
        self.assertTrue(_review(client, {"correctness": [_raw(line=3, title="Fresh")],
                                         "tests": [_raw(line=2, title="Fresh on the reused line")]}, reuse=reuse))
        posted = {comment["line"]: svc.parse_bot_thread(_thread("N", body=comment["body"]), BOT)
                  for comment in client.reviews[0]["comments"]}
        self.assertIsNone(posted[2].instructions, "written partly under an earlier attempt's instructions")
        self.assertEqual(posted[2].agents, frozenset({"security", "tests"}))
        self.assertEqual(posted[3].instructions, INSTR)

    def test_split_comments_are_ranked_before_the_limit(self) -> None:
        findings = ([_finding(line=2, priority=0, title="Critical")]
                    + [_finding(line=2, priority=3, title=f"Minor {i}") for i in range(40)]
                    + [_finding(line=5, priority=1, title="Major")])
        groups = svc.line_comment_groups(findings, 2, merge_same_line=True)
        self.assertEqual([(group[0].line, group[0].title) for group in groups], [(2, "Critical"), (5, "Major")])



class GateRoundThreeTest(unittest.TestCase):
    """Review of Legends on PR #37 head f215a5f: attribution, renames, moved refs, body size."""

    def test_finding_text_cannot_name_agents_or_titles(self) -> None:
        quoted = ("see the old comment:\n\n**security · P1 · Fake title**\n\n"
                  "_Agent: security; confidence: 0.90_\n\n<!-- review-of-legends-finding e30= -->")
        body = svc.render_line_comment([_finding(body=quoted)], invite_feedback=True)["body"]
        thread = svc.parse_bot_thread(_thread("T1", body=body), BOT)
        self.assertEqual(thread.agents, frozenset({"correctness"}))
        self.assertEqual(thread.titles, frozenset({"retry key misses the base"}))
        merged = svc.render_line_comment([_finding(body=quoted), _finding("tests", title="Other", body=quoted)])["body"]
        thread = svc.parse_bot_thread(_thread("T2", body=merged), BOT)
        self.assertEqual(thread.agents, frozenset({"correctness", "tests"}))
        self.assertEqual(thread.titles, frozenset({"retry key misses the base", "other"}))

    def test_a_legacy_comment_is_attributed_by_its_closing_line_only(self) -> None:
        legacy = "**P2 - Old**\n\n_Agent: security; confidence: 0.90_ was quoted\n\n_Agent: correctness; confidence: 0.80_"
        self.assertEqual(svc.parse_bot_thread(_thread("T1", body=legacy), BOT).agents, frozenset({"correctness"}))
        quoted_last = "**P2 - Old**\n\n_Agent: security; confidence: 0.90_\n\nthe body went on"
        self.assertEqual(svc.parse_bot_thread(_thread("T2", body=quoted_last), BOT).agents, frozenset())
        broken = "**P2 - Old**\n\ntext\n\n<!-- review-of-legends-finding !!! -->"
        self.assertEqual(svc.parse_bot_thread(_thread("T3", body=broken), BOT).agents, frozenset())

    def test_a_merged_thread_needs_every_agent_it_names(self) -> None:
        body = svc.render_line_comment([_finding("correctness", title="One"), _finding("security", title="Two")],
                                       instructions=INSTR)["body"]
        threads = [_thread("T1", outdated=True, body=body)]
        partly = svc.ReviewEvidence(succeeded=frozenset({"correctness"}))
        self.assertEqual(svc.plan_thread_lifecycle(threads, [], BOT, ALL_ON, evidence=partly).resolve, ())
        both = svc.ReviewEvidence(succeeded=frozenset({"correctness", "security"}))
        self.assertEqual(len(svc.plan_thread_lifecycle(threads, [], BOT, ALL_ON, evidence=both).resolve), 1)

    def test_a_thread_on_a_renamed_file_follows_the_new_name(self) -> None:
        threads = [_thread("T1", path="old.py", outdated=True)]
        renamed = (("old.py", "new.py"),)
        seen = svc.ReviewEvidence(seen=frozenset({"new.py"}), renamed=renamed)
        self.assertEqual(len(svc.plan_thread_lifecycle(threads, [], BOT, ALL_ON, evidence=seen).resolve), 1)
        again = [_finding(path="new.py", title="Retry key misses the base")]
        self.assertEqual(svc.plan_thread_lifecycle(threads, again, BOT, ALL_ON, evidence=seen).resolve, (),
                         "raised again under the new name")
        unseen = svc.ReviewEvidence(seen=frozenset(), renamed=renamed)
        self.assertEqual(svc.plan_thread_lifecycle(threads, [], BOT, ALL_ON, evidence=unseen).resolve, (),
                         "the renamed file's patch was not read")
        no_alias = svc.ReviewEvidence(seen=frozenset({"new.py"}))
        self.assertEqual(svc.plan_thread_lifecycle(threads, [], BOT, ALL_ON, evidence=no_alias).resolve, (),
                         "a path the review does not know was not re-checked")

    def test_review_pr_reads_renames_from_the_file_list(self) -> None:
        client = _LifecycleClient([_thread("T1", path="old.py", outdated=True, title="Unfixed"),
                                   _thread("T2", path="old.py", line=3, outdated=True, title="Fixed")])
        client.list_files = lambda number: [{
            "filename": "new.py", "previous_filename": "old.py", "status": "renamed", "additions": 1,
            "deletions": 0, "changes": 1, "patch": "@@ -1,1 +1,2 @@\n context\n+two\n"}]
        raw = {"path": "new.py", "line": 1, "priority": 2, "title": "Unfixed", "body": "still", "confidence": 0.8}
        self.assertTrue(_review(client, {"correctness": [raw]}))
        self.assertEqual(client.resolves(), ["T2"])

    def test_threads_of_another_head_or_base_hold_nothing_back_and_resolve_nothing(self) -> None:
        threads = [{**_thread("T1", line=2), "pullRequest": {"headRefOid": "h2", "baseRefOid": "b1"}},
                   {**_thread("T2", outdated=True, title="Gone"), "pullRequest": {"headRefOid": "h2", "baseRefOid": "b1"}}]
        evidence = svc.ReviewEvidence(head="h1", base="b1")
        plan = svc.plan_thread_lifecycle(threads, [_finding()], BOT, ALL_ON, evidence=evidence)
        self.assertEqual((plan.held_back, plan.resolve), ((), ()))
        rebased = svc.ReviewEvidence(head="h2", base="b0")
        plan = svc.plan_thread_lifecycle(threads, [_finding()], BOT, ALL_ON, evidence=rebased)
        self.assertEqual((plan.held_back, plan.resolve), ((), ()))
        current = svc.ReviewEvidence(head="h2", base="b1")
        plan = svc.plan_thread_lifecycle(threads, [_finding()], BOT, ALL_ON, evidence=current)
        self.assertEqual((len(plan.held_back), len(plan.resolve)), (1, 1))

    def test_a_push_before_the_listing_posts_every_finding(self) -> None:
        client = _LifecycleClient([_thread("T1", line=2, title="Retry key misses the base")])
        client.head = "newer0000000"
        self.assertTrue(_review(client, {"correctness": [_raw(line=2)]}))
        self.assertEqual(len(client.reviews[0]["comments"]), 1, "the listing's lines belong to another head")
        self.assertNotIn("Not posted again", client.reviews[0]["body"])

    def test_a_base_change_after_posting_resolves_nothing(self) -> None:
        client = _LifecycleClient([_thread("T1", outdated=True, title="Fixed bug")])
        original = client.create_review

        def create_review_then_retarget(*args, **kwargs):
            result = original(*args, **kwargs)
            client.base = "c" * 12
            return result

        client.create_review = create_review_then_retarget
        self.assertTrue(_review(client, {}))
        self.assertIn("Resolving earlier threads", client.reviews[0]["body"])
        self.assertEqual(client.resolves(), [])
        self.assertIn("newer head or base", client.progress_bodies[-1])

    def test_a_push_between_two_resolves_leaves_the_rest_open(self) -> None:
        threads = [_thread("T1", outdated=True, title="One"), _thread("T2", line=3, outdated=True, title="Two"),
                   _thread("T3", line=4, outdated=True, title="Three")]
        client = _LifecycleClient(threads)
        original = client.resolve_review_thread

        def resolve_then_push(thread_id):
            result = original(thread_id)
            client.head = "newer0000000"
            return result

        client.resolve_review_thread = resolve_then_push
        self.assertTrue(_review(client, {}))
        self.assertEqual(client.resolves(), ["T1"])
        self.assertIn("resolved 1/3", client.progress_bodies[-1])
        self.assertIn("2 left open", client.progress_bodies[-1])

    def test_the_sections_fit_what_the_rest_of_the_body_leaves(self) -> None:
        pr = {"number": 7, "head": {"sha": "abc"}}
        resolving = svc.plan_thread_lifecycle(
            [_thread(f"T{i}", line=i + 1, outdated=True, title=f"Fixed {i} " + "x" * 120) for i in range(300)],
            [], BOT, ALL_ON).resolve
        for summary_size in (10, 55000, 59400, 70000):
            with self.subTest(summary_size=summary_size):
                results = [{"agent": "a", "summary": "s" * summary_size, "findings": []}]
                plain = svc.build_review_body({}, pr, results, [], 0, version="v")
                body = svc.build_review_body({}, pr, results, [], 0, version="v", thread_plan=svc.ThreadPlan(
                                             resolve=resolving, feedback=(("a", 1, 0),), feedback_findings=1,
                                             feedback_up=1))
                if len(plain) <= svc.MAX_REVIEW_BODY_CHARS:
                    self.assertLessEqual(len(body), svc.MAX_REVIEW_BODY_CHARS)
                else:
                    self.assertEqual(body, plain, "a body already over the limit gets no section")
                listed = body.count("discussion_rT")
                if "### Resolving earlier threads" in body:
                    self.assertIn(f"…and {300 - listed} more", body)
                    self.assertLessEqual(listed, svc.MAX_THREAD_SECTION_ITEMS)
                self.assertTrue(body.endswith("_Generated by Review of Legends._"))


class GateRoundFourTest(unittest.TestCase):
    """Review of Legends on PR #37 head 3152a36: metadata shape, long prefixed titles, dot paths, renames in G2."""

    @staticmethod
    def _meta_body(data: Any) -> str:
        encoded = base64.b64encode(json.dumps(data).encode("utf-8")).decode("ascii")
        return f"**P2 - Old**\n\ntext\n\n<!-- review-of-legends-finding {encoded} -->"

    def test_a_metadata_field_of_the_wrong_shape_claims_nothing(self) -> None:
        cases = (({"agents": 1, "titles": ["x"]}, False),
                 ({"agents": ["correctness"], "titles": 5}, False),
                 ({"agents": "correctness", "titles": "t"}, False),
                 ({"agents": {"correctness": 1}, "titles": None}, False),
                 ({"agents": ["correctness"], "titles": ["x"]}, True))
        evidence = svc.ReviewEvidence(succeeded=frozenset({"correctness"}))
        for fields, valid in cases:
            with self.subTest(fields=fields):
                node = _thread("T1", outdated=True, body=self._meta_body({"v": 1, "instr": INSTR, **fields}))
                thread = svc.parse_bot_thread(node, BOT)
                self.assertEqual(thread.instructions, INSTR if valid else None)
                if not valid:
                    self.assertEqual((thread.agents, thread.titles), (frozenset(), frozenset()))
                plan = svc.plan_thread_lifecycle([node], [], BOT, ALL_ON, evidence=evidence)
                self.assertEqual(len(plan.resolve), 1 if valid else 0, "a line that names nothing is never resolved")

    def test_a_thread_plan_failure_posts_as_before(self) -> None:
        client = _LifecycleClient([_thread("T1", outdated=True)])
        with mock.patch.object(svc, "plan_thread_lifecycle", side_effect=TypeError("bad thread")):
            plan, note = svc.plan_review_threads(client, 5, [], ALL_ON)
        self.assertEqual(plan, svc.ThreadPlan())
        self.assertIn("could not be read", note)

    def test_a_long_prefixed_title_matches_its_unprefixed_re_raise(self) -> None:
        title = "Retry key " + "x" * 150
        self.assertEqual(len(title), 160)
        first = svc.normalize_findings([{"agent": "correctness", "findings": [
            {"path": "a.py", "line": 2, "priority": 2, "title": "[P2] " + title, "body": "b"}]}], {"a.py": {2}})
        body = svc.render_line_comment(first, invite_feedback=True, instructions=INSTR)["body"]
        threads = [_thread("T1", outdated=True, body=body)]
        results = [{"agent": "correctness", "findings": [{"path": "a.py", "line": 9, "priority": 2,
                                                          "title": title, "body": "b"}]}]
        evidence = svc.ReviewEvidence(raised=svc.raised_finding_keys(results, ["a.py"]),
                                      succeeded=frozenset({"correctness"}), seen=frozenset({"a.py"}),
                                      instructions=INSTR)
        self.assertEqual(svc.plan_thread_lifecycle(threads, [], BOT, ALL_ON, evidence=evidence).resolve, (),
                         "the same finding raised again without its prefix")
        control = svc.ReviewEvidence(succeeded=frozenset({"correctness"}), seen=frozenset({"a.py"}), instructions=INSTR)
        self.assertEqual(len(svc.plan_thread_lifecycle(threads, [], BOT, ALL_ON, evidence=control).resolve), 1,
                         "the fixture resolves when nothing is raised again")
        again = svc.normalize_findings(results, {"a.py": {9}})
        self.assertEqual(svc.plan_thread_lifecycle(threads, again, BOT, ALL_ON).resolve, ())
        self.assertLessEqual(len(svc.display_title(first[0].title)), 160)

    def test_dot_segments_in_a_checkout_path_name_the_repository_file(self) -> None:
        names = ["a.py", "src/b.py"]
        for raw_path, expected in (("/tmp/run/source/./a.py", "a.py"), ("/tmp/run/source/src/../a.py", "a.py"),
                                   ("/tmp/run/source/src//b.py", "src/b.py"), ("./a.py", "a.py"),
                                   ("src/./b.py", "src/b.py")):
            with self.subTest(path=raw_path):
                results = [{"agent": "c", "findings": [{"path": raw_path, "line": 1, "title": "T", "body": "b"}]}]
                keys = svc.raised_finding_keys(results, names, roots=["/tmp/run/source"])
                self.assertEqual(keys, frozenset({(expected, "t")}))
        outside = [{"agent": "c", "findings": [{"path": "/tmp/run/source/../other/a.py", "line": 1,
                                                 "title": "T", "body": "b"}]}]
        self.assertEqual(svc.raised_finding_keys(outside, names, roots=["/tmp/run/source"]),
                         frozenset({("a.py", "t")}), "a path that leaves the checkout still counts where it ends")

    def test_an_open_thread_on_a_renamed_file_holds_back_the_same_line_finding(self) -> None:
        threads = [_thread("T1", path="old.py", line=2)]
        evidence = svc.ReviewEvidence(renamed=(("old.py", "new.py"),))
        plan = svc.plan_thread_lifecycle(threads, [_finding(path="new.py", priority=3)], BOT, ALL_ON,
                                         evidence=evidence)
        self.assertEqual([thread.id for _, thread in plan.held_back], ["T1"])


class GateRoundFiveTest(unittest.TestCase):
    """Rule 8 gate on PR #37 head c5157b6: threads already posted, prefix chains, deep metadata."""

    TITLE = "Retry key " + "x" * 150  # 160 characters

    def _raise(self, title: str, line: int = 9):
        results = [{"agent": "correctness", "findings": [{"path": "a.py", "line": line, "priority": 2,
                                                          "title": title, "body": "b"}]}]
        evidence = svc.ReviewEvidence(raised=svc.raised_finding_keys(results, ["a.py"]),
                                      succeeded=frozenset({"correctness"}), seen=frozenset({"a.py"}))
        return svc.normalize_findings(results, {"a.py": {line}}), evidence

    def test_a_thread_posted_before_the_lifecycle_is_left_to_people(self) -> None:
        # main cut "[P2] " + title at 160 characters and wrote it into the header as it was.
        legacy = f"**P2 - {('[P2] ' + self.TITLE)[:160]}**\n\nold details\n\n_Agent: correctness; confidence: 0.90_"
        threads = [_thread("T1", outdated=True, body=legacy)]
        for title in ("[P2] " + self.TITLE, self.TITLE, "Another finding"):
            with self.subTest(title=title[:12]):
                findings, evidence = self._raise(title)
                for found in (findings, []):
                    plan = svc.plan_thread_lifecycle(threads, found, BOT, ALL_ON, evidence=evidence)
                    self.assertEqual((plan.resolve, plan.left_to_people), ((), 1))

    def test_a_title_keeps_160_characters_whatever_its_prefixes(self) -> None:
        for title in ("[P2] " * 300 + "t", "[P2] " + self.TITLE + "tail", self.TITLE + "tail"):
            with self.subTest(title=title[:12]):
                kept = svc.finding_title({"title": title})
                self.assertLessEqual(len(svc.display_title(kept)), 160)
                self.assertLessEqual(len(kept), 201, "a prefix of at most 40 characters, a space, 160 characters")
        self.assertEqual(svc.finding_title({"title": "[P2] " * 300 + "t"}), "t", "a prefix chain is dropped")

    def test_metadata_nested_too_deep_claims_nothing(self) -> None:
        deep = "[" * 100000 + "]" * 100000
        encoded = base64.b64encode(deep.encode("ascii")).decode("ascii")
        body = f"**P2 - Old**\n\ntext\n\n<!-- review-of-legends-finding {encoded} -->"
        thread = svc.parse_bot_thread(_thread("T1", outdated=True, body=body), BOT)
        self.assertEqual((thread.agents, thread.titles, thread.instructions), (frozenset(), frozenset(), None))

    def test_a_thread_that_cannot_be_judged_again_stays_open(self) -> None:
        threads = [_thread("T1", outdated=True, title="One"), _thread("T2", line=4, outdated=True, title="Two")]
        client = _LifecycleClient(threads)
        plan = svc.plan_thread_lifecycle(threads, [], BOT, ALL_ON)
        self.assertEqual(len(plan.resolve), 2)
        real = svc.plan_thread_lifecycle

        def judge(nodes, *args, **kwargs):
            if nodes and nodes[0].get("id") == "T1":
                raise RecursionError("maximum recursion depth exceeded")
            return real(nodes, *args, **kwargs)

        with mock.patch.object(svc, "plan_thread_lifecycle", side_effect=judge):
            note = svc.resolve_review_threads(client, 5, plan.resolve, [], client.head)
        self.assertEqual(client.resolves(), ["T2"])
        self.assertIn("resolved 1/2", note)
        self.assertIn("1 skipped", note)


class GateRoundSevenTest(unittest.TestCase):
    """PR #37 round 7 (option A): G3 compares only comments of this format in this review's language."""

    def _raise(self, title: str, line: int = 9, instructions: str = INSTR):
        results = [{"agent": "correctness", "findings": [{"path": "a.py", "line": line, "priority": 2,
                                                          "title": title, "body": "b"}]}]
        evidence = svc.ReviewEvidence(raised=svc.raised_finding_keys(results, ["a.py"]),
                                      succeeded=frozenset({"correctness"}), seen=frozenset({"a.py"}),
                                      instructions=instructions)
        return svc.normalize_findings(results, {"a.py": {line}}), evidence

    def test_the_same_words_give_the_same_key_on_every_path(self) -> None:
        long_words = "Retry key " + "word   " * 30 + "end"
        for raw, again in (("[P2] " + long_words, long_words), ("P1 -  Retry   key\n misses", "Retry key misses"),
                           ("[P2] " * 9 + long_words, long_words), ("[P2] " * 30 + "Short one", "Short one"),
                           ("[P2] Retry key " + "word   " * 30 + "end", "Retry key " + "word " * 30 + "end")):
            with self.subTest(raw=raw[:16]):
                first, _ = self._raise(raw)
                body = svc.render_line_comment(first, instructions="key-ko")["body"]
                thread = svc.parse_bot_thread(_thread("T1", outdated=True, body=body), BOT)
                key = svc.normalize_finding_title(svc.finding_title({"title": again}))
                self.assertEqual(thread.titles, frozenset({key}), "metadata line")
                self.assertEqual(svc.normalize_finding_title(first[0].title), key, "this review's finding")
                _, evidence = self._raise(again, instructions="key-ko")
                self.assertIn(("a.py", key), evidence.raised, "raised findings")
                self.assertEqual(thread.instructions, "key-ko")
                self.assertEqual(svc.plan_thread_lifecycle([_thread("T1", outdated=True, body=body)], [], BOT, ALL_ON,
                                                           evidence=evidence).resolve, ())

    def test_other_instructions_are_left_to_people(self) -> None:
        thread = _thread("T1", outdated=True, instr="key-")
        for instructions, resolved in (("key-", 1), ("key-ko", 0), ("", 0)):
            with self.subTest(instructions=instructions):
                _, evidence = self._raise("Another finding", instructions=instructions)
                plan = svc.plan_thread_lifecycle([thread], [], BOT, ALL_ON, evidence=evidence)
                self.assertEqual((len(plan.resolve), plan.left_to_people), (resolved, 1 - resolved))
        _, korean = self._raise("다른 지적", instructions="key-ko")
        self.assertEqual(len(svc.plan_thread_lifecycle([_thread("T2", outdated=True, instr="key-ko")], [], BOT,
                                                       ALL_ON, evidence=korean).resolve), 1)
        unknown = _thread("T3", outdated=True, body="**P2 - Old**\n\ntext\n\n" + _meta_line(["correctness"], ["Old"], ""))
        self.assertIsNone(svc.parse_bot_thread(unknown, BOT).instructions)
        self.assertEqual(svc.parse_bot_thread(unknown, BOT).agents, frozenset({"correctness"}),
                         "a line without a key still names its agents for the tally")
        _, any_key = self._raise("Another finding", instructions="key-")
        self.assertEqual(svc.plan_thread_lifecycle([unknown], [], BOT, ALL_ON, evidence=any_key).resolve, ())

    def test_only_this_metadata_format_is_resolved(self) -> None:
        def body(version: Any, **extra: Any) -> str:
            meta = {"v": version, "instr": INSTR, "agents": ["correctness"], "titles": ["old"], **extra}
            encoded = base64.b64encode(json.dumps(meta).encode("utf-8")).decode("ascii")
            return f"**P2 - Old**\n\ntext\n\n<!-- review-of-legends-finding {encoded} -->"

        _, evidence = self._raise("Another finding")
        for text, resolved in ((body(1), 1), (body(2), 0), (body(True), 0), (body("1"), 0),
                               (body(1, instr=None), 0), (body(1, instr=5), 0), (body(1, agents=[]), 0),
                               (body(1, titles=[]), 0)):
            with self.subTest(body=text[-60:]):
                node = _thread("T1", outdated=True, body=text)
                self.assertEqual(len(svc.plan_thread_lifecycle([node], [], BOT, ALL_ON, evidence=evidence).resolve),
                                 resolved)
        not_an_object = base64.b64encode(b"[1, 2]").decode("ascii")
        node = _thread("T2", outdated=True, body=f"**P2 - Old**\n\ntext\n\n<!-- review-of-legends-finding {not_an_object} -->")
        self.assertEqual((svc.parse_bot_thread(node, BOT).agents, svc.parse_bot_thread(node, BOT).instructions),
                         (frozenset(), None))

    def test_a_thread_left_to_people_still_holds_back_and_counts_feedback(self) -> None:
        legacy = _thread("T1", instr=None, up=1)
        plan = svc.plan_thread_lifecycle([legacy], [_finding(priority=3)], BOT, ALL_ON)
        self.assertEqual([thread.id for _, thread in plan.held_back], ["T1"])
        self.assertEqual((plan.feedback_findings, plan.feedback), (1, (("correctness", 1, 0),)))

    def test_a_rename_chain_counts_a_re_raise_under_either_name(self) -> None:
        thread = _thread("T1", path="a.py", outdated=True)
        renamed = (("a.py", "b.py"), ("b.py", "c.py"))
        for raised_path in ("a.py", "b.py", "c.py"):
            with self.subTest(path=raised_path):
                results = [{"agent": "correctness", "findings": [{"path": raised_path, "line": 9, "priority": 2,
                                                                  "title": "Retry key misses the base", "body": "b"}]}]
                evidence = svc.ReviewEvidence(raised=svc.raised_finding_keys(results, ["b.py", "c.py", "a.py"]),
                                              succeeded=frozenset({"correctness"}), seen=frozenset({"b.py", "c.py"}),
                                              renamed=renamed)
                plan = svc.plan_thread_lifecycle([thread], [], BOT, ALL_ON, evidence=evidence)
                # c.py is the file that was b.py in the base: another file than the thread's.
                self.assertEqual(len(plan.resolve), 0 if raised_path in ("a.py", "b.py") else 1)


class BotRoundSevenTest(unittest.TestCase):
    """PR #37 round 7: bodies with code or HTML comments are never merged, a cut never stops inside code."""

    def test_a_body_with_code_or_an_html_comment_is_not_merged(self) -> None:
        plain = ("plain", "a < b", "x <= y and 3<4", "use `x` inline", "mid-line ```x``` is a code span",
                 "    indented code")
        other = ("```py\nx\n```", "a <!-- b --> c", "`<!--` inline", "<div><!--", "~~~\nx", "- ```py\n  x\n  ```",
                 "> ```\n> x", "1. ```", "```literal```", "<!--", "```suggestion\nx = 1")
        for text in plain:
            with self.subTest(plain=text):
                self.assertTrue(svc.plain_finding_body(text))
        for text in other:
            with self.subTest(other=text):
                self.assertFalse(svc.plain_finding_body(text))
                group = [_finding(body=text), _finding("tests", title="Other", body="details"),
                         _finding("security", title="Third", body="more")]
                groups = svc.line_comment_groups(group, 10, merge_same_line=True)
                self.assertEqual(sorted(len(g) for g in groups), [1, 2])
                alone = next(g for g in groups if len(g) == 1)
                self.assertEqual(alone[0].body, text)

    def test_a_cut_never_stops_inside_code(self) -> None:
        for fence in ("```", "`" * 32):
            with self.subTest(fence=len(fence)):
                text = "intro\n" + fence + "suggestion\n" + "x = 1\n" * 12000 + fence
                body = svc.render_line_comment([_finding(body=text)])["body"]
                self.assertLessEqual(len(body), svc.MAX_COMMENT_CHARS)
                self.assertNotIn("x = 1", body)
                self.assertIn("intro\n\n… (cut to fit", body)
                thread = svc.parse_bot_thread(_thread("T1", body=body), BOT)
                self.assertEqual((thread.agents, thread.instructions), (frozenset({"correctness"}), None))
        merged = [_finding(body="a" * 40000), _finding("tests", title="Other", body="b" * 40000)]
        body = svc.render_line_comment(merged)["body"]
        self.assertLessEqual(len(body), svc.MAX_COMMENT_CHARS)
        self.assertEqual(body.count("cut to fit"), 2)
        self.assertEqual(svc.parse_bot_thread(_thread("T2", body=body), BOT).agents,
                         frozenset({"correctness", "tests"}))

    def test_cut_finding_body_stops_before_an_html_comment(self) -> None:
        self.assertEqual(svc.cut_finding_body("keep <!-- hidden " + "z" * 100, 50), "keep" + svc._CUT_NOTE)
        self.assertEqual(svc.cut_finding_body("```\ncode", 5), svc._CUT_NOTE)

    def test_review_pr_compares_and_records_its_instructions_key(self) -> None:
        client = _LifecycleClient([_thread("T1", outdated=True, title="English one", instr="key-"),
                                   _thread("T2", line=3, outdated=True, title="한국어", instr="key-ko")])
        seen = []

        def key(prompts, rules, language):
            seen.append((sorted(prompts), rules, language))
            return "key-" + language

        with mock.patch.object(svc.mentions, "stored_language", lambda value: "ko"):
            self.assertTrue(_review(client, {"correctness": [_raw(line=2, title="새 지적")]}, instructions=key))
        self.assertEqual(seen, [(["correctness", "security", "tests"], "", "ko")])
        self.assertEqual(client.resolves(), ["T2"], "the thread written under other instructions is left to people")
        posted = client.reviews[0]["comments"][0]["body"]
        self.assertEqual(svc.parse_bot_thread(_thread("N", body=posted), BOT).instructions, "key-ko")

    def test_the_instructions_key_follows_language_rules_and_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "a.md").write_text("Review for bugs.", encoding="utf-8")
            agents = [{"id": "a", "prompt_path": "a.md"}]

            def key(settings=None, rules="rules", language="", agent_list=agents):
                prompts = svc.agent_prompt_texts(agent_list, settings or {}, config_dir)
                return svc.review_instructions_key(prompts, rules, language)

            base = key()
            self.assertEqual(len(base), 16)
            self.assertEqual(base, key({"a": {"prompt": None}}))
            self.assertEqual(svc.agent_prompt_texts(agents, {"a": {"prompt": "Korean please."}}, config_dir),
                             {"a": "Korean please."}, "a dashboard prompt overrides the file")
            changed = {"language": key(language="ko"), "rules": key(rules="Write in Korean."),
                       "dashboard prompt": key({"a": {"prompt": "Korean please."}})}
            for what, other in changed.items():
                with self.subTest(changed=what):
                    self.assertNotEqual(other, base)
            (config_dir / "a.md").write_text("Answer in Korean.", encoding="utf-8")
            self.assertNotEqual(key(), base, "prompt file")
            missing = [{"id": "b", "prompt_path": "missing.md"}]
            self.assertEqual(svc.agent_prompt_texts(missing, {}, config_dir), {"b": None})
            self.assertEqual(key(agent_list=missing), "", "an unreadable prompt gives no key")

    def test_the_agents_run_on_the_prompts_the_key_hashes(self) -> None:
        client = _LifecycleClient([])
        snapshot = {"correctness": "Prompt C", "security": "Prompt S", "tests": None}
        hashed, ran = [], {}

        def key(prompts, rules, language):
            hashed.append(dict(prompts))
            return "k"

        def run_agent(agent, *args, **kwargs):
            ran[agent["id"]] = kwargs.get("prompt_text")
            return {"agent": agent["id"], "summary": "ok", "findings": []}

        with mock.patch.object(svc, "agent_prompt_texts", lambda agents, settings, config_dir: dict(snapshot)), \
             mock.patch.object(svc, "run_agent", run_agent):
            # _review patches run_agent too; this one wins because it is entered last.
            self.assertTrue(_review(client, {}, instructions=key, run_agent=run_agent))
        self.assertEqual(hashed, [snapshot])
        self.assertEqual(ran, {"correctness": "Prompt C", "security": "Prompt S", "tests": None})

    def test_the_metadata_line_records_the_instructions_key(self) -> None:
        body = svc.render_line_comment([_finding()], instructions="key-ko")["body"]
        self.assertEqual(svc.parse_bot_thread(_thread("T1", body=body), BOT).instructions, "key-ko")
        comments = _line_comments([_finding()], 5, merge_same_line=True, instructions="key-ja")
        self.assertEqual(svc.parse_bot_thread(_thread("T2", body=comments[0]["body"]), BOT).instructions, "key-ja")

    def test_nested_markers_and_raw_html_blocks_are_not_merged(self) -> None:
        for text in ("- > ```suggestion\n  > x", "> - ```py", "1. > 2. ~~~", "<pre>\nkept", "<script>", "<STYLE>",
                     "<textarea>", "<?php", "<!DOCTYPE html>", "<![CDATA[ x", "- <pre>"):
            with self.subTest(text=text):
                self.assertFalse(svc.plain_finding_body(text))
        for text in ("<div>\nfine</div>", "<b>bold</b>", "use <pre> inline", "a <b> c", "List<String>", "</div>"):
            with self.subTest(raw_html=text):
                self.assertFalse(svc.plain_finding_body(text))
        nested = "keep\n- > ```suggestion\n  > value = \"" + "x" * 70000 + "\"\n  > ```"
        self.assertEqual(svc.cut_finding_body(nested, 60000), "keep" + svc._CUT_NOTE)


class BotRoundEightTest(unittest.TestCase):
    """Review of Legends on PR #37 head 3810799: raw HTML containers, ambiguous renames, rename lookup scale."""

    def test_a_title_with_raw_html_gets_its_own_comment(self) -> None:
        unsafe = _finding("tests", title="Handle an unclosed <details> tag", body="plain")
        groups = svc.line_comment_groups([_finding(body="plain"), unsafe, _finding("security", title="Third",
                                                                                  body="plain")], 10, True)
        self.assertIn([unsafe], groups)
        self.assertEqual(sorted(len(group) for group in groups), [1, 2])

    def test_a_metadata_list_with_one_bad_member_names_nothing(self) -> None:
        evidence = svc.ReviewEvidence(succeeded=frozenset({"correctness"}), instructions=INSTR)
        for fields in ({"agents": ["correctness", 1]}, {"agents": ["correctness", " "]},
                       {"titles": ["old", " "]}, {"titles": ["old", None]}):
            with self.subTest(fields=fields):
                meta = {"v": 1, "instr": INSTR, "agents": ["correctness"], "titles": ["old"], **fields}
                encoded = base64.b64encode(json.dumps(meta).encode("utf-8")).decode("ascii")
                node = _thread("T1", outdated=True,
                               body=f"**P2 - Old**\n\ntext\n\n<!-- review-of-legends-finding {encoded} -->")
                thread = svc.parse_bot_thread(node, BOT)
                self.assertEqual((thread.agents, thread.titles, thread.instructions), (frozenset(), frozenset(), None))
                self.assertEqual(svc.plan_thread_lifecycle([node], [], BOT, ALL_ON, evidence=evidence).resolve, ())

    def test_an_unclosed_html_container_gets_its_own_comment(self) -> None:
        details = _finding(body="<details><summary>Reproduction</summary>\n\nsteps")
        groups = svc.line_comment_groups([details, _finding("tests", title="Other", body="plain"),
                                          _finding("security", title="Third", body="also plain")], 10, True)
        alone = [group for group in groups if len(group) == 1]
        self.assertEqual([group[0].body for group in alone], [details.body])
        self.assertEqual(svc.cut_finding_body("keep this <details>" + "z" * 100, 60), "keep this" + svc._CUT_NOTE)

    def test_an_ambiguous_rename_name_is_left_alone(self) -> None:
        renamed = (("a.py", "b.py"), ("b.py", "c.py"))
        evidence = svc.ReviewEvidence(renamed=renamed, succeeded=frozenset({"correctness"}),
                                      seen=frozenset({"b.py", "c.py"}))
        self.assertTrue(evidence.ambiguous("b.py"))
        self.assertFalse(evidence.ambiguous("a.py"))
        self.assertEqual([evidence.current_path(path) for path in ("a.py", "b.py", "c.py", "d.py")],
                         ["b.py", "b.py", "c.py", "d.py"])
        open_b = _thread("T1", path="b.py", line=2)
        held = svc.plan_thread_lifecycle([open_b], [_finding(path="c.py", priority=3)], BOT, ALL_ON, evidence=evidence)
        self.assertEqual(held.held_back, (), "an open thread on b.py says nothing about c.py")
        held = svc.plan_thread_lifecycle([open_b], [_finding(path="b.py", priority=3)], BOT, ALL_ON, evidence=evidence)
        self.assertEqual(held.held_back, (), "b.py may name either file")
        outdated_b = _thread("T2", path="b.py", outdated=True)
        plan = svc.plan_thread_lifecycle([outdated_b], [], BOT, ALL_ON, evidence=evidence)
        self.assertEqual((plan.resolve, plan.left_to_people), ((), 1))
        outdated_a = _thread("T3", path="a.py", outdated=True)
        self.assertEqual(len(svc.plan_thread_lifecycle([outdated_a], [], BOT, ALL_ON, evidence=evidence).resolve), 1)

    def test_a_re_raise_under_an_ambiguous_name_counts_for_both_files(self) -> None:
        renamed = (("a.py", "b.py"), ("b.py", "c.py"))
        thread_c = _thread("T-c", path="c.py", outdated=True, title="Still broken")
        for raised_path, resolved in (("b.py", 0), ("c.py", 0), ("d.py", 1)):
            with self.subTest(raised=raised_path):
                results = [{"agent": "correctness", "findings": [{"path": raised_path, "line": 9, "priority": 2,
                                                                  "title": "Still broken", "body": "b"}]}]
                evidence = svc.ReviewEvidence(raised=svc.raised_finding_keys(results, ["b.py", "c.py", "d.py"]),
                                              succeeded=frozenset({"correctness"}), seen=frozenset({"b.py", "c.py"}),
                                              renamed=renamed)
                plan = svc.plan_thread_lifecycle([thread_c], [], BOT, ALL_ON, evidence=evidence)
                self.assertEqual(len(plan.resolve), resolved)

    def test_the_rename_lookup_is_built_once(self) -> None:
        renamed = tuple((f"old{i}.py", f"new{i}.py") for i in range(3000))
        results = [{"agent": "correctness", "findings": [{"path": f"new{i}.py", "line": 9, "priority": 2,
                                                          "title": f"Finding {i}", "body": "b"} for i in range(300)]}]
        evidence = svc.ReviewEvidence(raised=svc.raised_finding_keys(results, [new for _, new in renamed]),
                                      succeeded=frozenset({"correctness"}), renamed=renamed)
        threads = [_thread(f"T{i}", path=f"old{i}.py", outdated=True, title=f"Other {i}") for i in range(3000)]
        started = time.monotonic()
        plan = svc.plan_thread_lifecycle(threads, [], BOT, ALL_ON, evidence=evidence)
        elapsed = time.monotonic() - started
        self.assertEqual(len(plan.resolve), 3000)
        self.assertLess(elapsed, 10.0)
        self.assertIs(evidence._renames, evidence._renames, "one lookup per evidence")


class BotRoundTenTest(unittest.TestCase):
    """Review of Legends on PR #37 head 1b13f02: bare CR line ends, a rename source added again."""

    def test_a_fence_after_a_bare_carriage_return_is_found(self) -> None:
        for text in ("Explanation\r```suggestion\rreplacement", "a\r\n~~~\r\nx", "x\r- > ```py"):
            with self.subTest(text=text):
                self.assertFalse(svc.plain_finding_body(text))
        body = "keep\r```suggestion\r" + "x = 1\r" * 20
        self.assertEqual(svc.cut_finding_body(body, 60), "keep" + svc._CUT_NOTE)

    def test_a_rename_source_that_is_a_file_again_is_ambiguous(self) -> None:
        evidence = svc.ReviewEvidence(renamed=(("old.py", "new.py"),), files=frozenset({"old.py", "new.py"}),
                                      succeeded=frozenset({"correctness"}), seen=frozenset({"old.py", "new.py"}))
        self.assertTrue(evidence.ambiguous("old.py"))
        self.assertEqual(evidence.current_path("old.py"), "old.py")
        open_old = _thread("T1", path="old.py", line=2)
        plan = svc.plan_thread_lifecycle([open_old], [_finding(path="new.py", priority=3)], BOT, ALL_ON,
                                         evidence=evidence)
        self.assertEqual(plan.held_back, ())
        plan = svc.plan_thread_lifecycle([_thread("T2", path="old.py", outdated=True)], [], BOT, ALL_ON,
                                         evidence=evidence)
        self.assertEqual((plan.resolve, plan.left_to_people), ((), 1))

    def test_review_pr_passes_every_current_file_name(self) -> None:
        client = _LifecycleClient([_thread("T1", path="old.py", outdated=True, title="Old")])
        client.list_files = lambda number: [
            {"filename": "new.py", "previous_filename": "old.py", "status": "renamed", "additions": 1,
             "deletions": 0, "patch": "@@ -1,0 +1,1 @@\n+x"},
            {"filename": "old.py", "status": "added", "additions": 1, "deletions": 0, "patch": "@@ -0,0 +1,1 @@\n+y"},
        ]
        self.assertTrue(_review(client, {}))
        self.assertEqual(client.resolves(), [], "old.py names a file now and the one renamed away")


class AppCannotResolveTest(unittest.TestCase):
    """Live receipt PR #42 (2026-10-15): the App's installation has Contents: read,
    and GitHub refused every resolveReviewThread with "Resource not accessible by
    integration" after the review body had listed the thread under "Resolving"."""

    APP = {"app_id": 11, "installation_id": 22, "private_key_path": "/nonexistent/app.pem", "bot_login": BOT}

    def setUp(self) -> None:
        svc._github_app_permissions.clear()
        self.addCleanup(svc._github_app_permissions.clear)

    def _client(self, threads, permissions):
        client = _LifecycleClient(threads)
        client.github_app = self.APP
        if permissions is not None:
            svc._github_app_permissions[(11, 22)] = permissions
        return client

    def test_the_token_exchange_keeps_the_installation_permissions(self) -> None:
        answer = {"token": "ghs_token", "expires_at": "2099-01-01T00:00:00Z",
                  "permissions": {"contents": "read", "pull_requests": "write"}}
        with mock.patch.object(svc, "github_app_jwt", return_value="jwt"), \
             mock.patch.object(svc.urllib.request, "urlopen",
                               lambda request, timeout=None: io.BytesIO(json.dumps(answer).encode("utf-8"))):
            svc.exchange_github_app_token(self.APP)
        self.assertEqual(svc._github_app_permissions[(11, 22)], {"contents": "read", "pull_requests": "write"})

    def test_a_token_without_permissions_forgets_the_last_ones(self) -> None:
        answers = [
            {"token": "ghs_one", "expires_at": "2099-01-01T00:00:00Z", "permissions": {"contents": "write"}},
            {"token": "ghs_two", "expires_at": "2099-01-01T00:00:00Z"},
        ]
        with mock.patch.object(svc, "github_app_jwt", return_value="jwt"), \
             mock.patch.object(svc.urllib.request, "urlopen",
                               lambda request, timeout=None: io.BytesIO(json.dumps(answers.pop(0)).encode("utf-8"))):
            svc.exchange_github_app_token(self.APP)
            self.assertEqual(svc._github_app_permissions[(11, 22)], {"contents": "write"})
            svc.exchange_github_app_token(self.APP)
        self.assertNotIn((11, 22), svc._github_app_permissions, "the new token's permissions are unknown")
        client = self._client([_thread("T1", outdated=True, title="Fixed bug")], None)
        self.assertTrue(_review(client, {"correctness": [_raw(line=3, title="Something new")]}))
        self.assertEqual(client.resolves(), [])
        self.assertIn("### Looks fixed, for people to resolve", client.reviews[0]["body"])

    def test_each_token_s_permissions_decide_the_next_review(self) -> None:
        """A grant takes effect at the next token, and so does a downgrade."""
        def exchange(contents):
            answer = {"token": "ghs_t", "expires_at": "2099-01-01T00:00:00Z", "permissions": {"contents": contents}}
            with mock.patch.object(svc, "github_app_jwt", return_value="jwt"), \
                 mock.patch.object(svc.urllib.request, "urlopen",
                                   lambda request, timeout=None: io.BytesIO(json.dumps(answer).encode("utf-8"))):
                svc.exchange_github_app_token(self.APP)

        for contents, resolves in (("read", []), ("write", ["T1"]), ("read", [])):
            exchange(contents)
            client = self._client([_thread("T1", outdated=True, title="Fixed bug")], None)
            self.assertTrue(_review(client, {"correctness": [_raw(line=3, title="Something new")]}))
            self.assertEqual(client.resolves(), resolves, contents)
            body = client.reviews[0]["body"]
            self.assertEqual("### Looks fixed, for people to resolve" in body, not resolves, contents)
            self.assertEqual("### Resolving earlier threads" in body, bool(resolves), contents)

    def test_without_contents_write_fixed_threads_are_left_for_people(self) -> None:
        client = self._client([_thread("T1", outdated=True, title="Fixed bug")],
                              {"contents": "read", "pull_requests": "write"})
        self.assertTrue(_review(client, {"correctness": [_raw(line=3, title="Something new")]}))
        self.assertEqual(client.resolves(), [], "no mutation GitHub would refuse")
        body = client.reviews[0]["body"]
        self.assertNotIn("### Resolving earlier threads", body)
        self.assertIn("### Looks fixed, for people to resolve", body)
        self.assertIn("Contents: write", body)
        self.assertIn("- `a.py`: Fixed bug — [earlier thread](https://github.com/owner/repo/pull/5#discussion_rT1)", body)
        self.assertNotIn("resolved 0/", client.progress_bodies[-1])

    def test_unknown_permissions_leave_the_threads_for_people(self) -> None:
        client = self._client([_thread("T1", outdated=True, title="Fixed bug")], None)
        self.assertTrue(_review(client, {"correctness": [_raw(line=3, title="Something new")]}))
        self.assertEqual(client.resolves(), [])
        body = client.reviews[0]["body"]
        self.assertIn("### Looks fixed, for people to resolve", body)
        self.assertIn("could not confirm", body)

    def test_with_contents_write_the_thread_is_resolved(self) -> None:
        client = self._client([_thread("T1", outdated=True, title="Fixed bug")], {"contents": "write"})
        self.assertTrue(_review(client, {"correctness": [_raw(line=3, title="Something new")]}))
        self.assertEqual(client.resolves(), ["T1"])
        body = client.reviews[0]["body"]
        self.assertIn("### Resolving earlier threads", body)
        self.assertNotIn("for people to resolve", body)

    def test_a_refused_resolve_stops_the_rest_and_says_why(self) -> None:
        client = self._client([_thread("T1", outdated=True, title="Fixed bug"),
                               _thread("T2", line=4, outdated=True, title="Other fixed")], {"contents": "write"})

        def refuse(thread_id):
            client.calls.append(("resolve", thread_id))
            raise svc.ServiceError("GitHub GraphQL failed: Resource not accessible by integration")

        client.resolve_review_thread = refuse
        self.assertTrue(_review(client, {"correctness": [_raw(line=3, title="Something new")]}))
        self.assertEqual(client.resolves(), ["T1"], "one refusal is enough: the rest are not tried")
        note = client.progress_bodies[-1]
        self.assertIn("resolved 0/2 earlier threads", note)
        self.assertIn("GitHub refused", note)
        self.assertIn("Resource not accessible by integration", note)

    def test_another_resolve_failure_still_tries_the_rest(self) -> None:
        client = self._client([_thread("T1", outdated=True, title="Fixed bug"),
                               _thread("T2", line=4, outdated=True, title="Other fixed")], {"contents": "write"})
        client.resolve_fail = {"T1"}
        self.assertTrue(_review(client, {"correctness": [_raw(line=3, title="Something new")]}))
        self.assertEqual(client.resolves(), ["T1", "T2"])
        self.assertIn("resolved 1/2 earlier threads", client.progress_bodies[-1])


if __name__ == "__main__":
    unittest.main()
