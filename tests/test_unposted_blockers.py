"""A P0/P1 that cannot be a line comment still shows on the review.

A production PR (2026-10-20): correctness raised a P1 on line 137, an unchanged
blank line. ``normalize_findings`` keeps only added lines, so the P1 got no
line comment and no entry under "### Findings", and the executive summary,
which reads the normalized findings, wrote that no P0/P1 findings remained.
The required status still failed on the raw verdict. The author saw a failing
gate and no reason for it. The gate rule stays as it is (a raw P0/P1 blocks);
these tests pin that the finding behind it is always shown.
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
from tests import test_required_status as trs
from tests.test_dashboard_integration import FakeStore
from tests.test_required_agents import _config

SECTION = "### Blocking findings not on an added line"


def _finding(line, priority=1, title="[P1] nullable value passed to Assert.True", body="ReadOtpSelected returns bool?; CS1503.", path="a.py"):
    raw = {"path": path, "priority": priority, "title": title, "body": body}
    if line is not None:
        raw["line"] = line
    return raw


class ReviewBodyTest(unittest.TestCase):
    # The fake diff (test_progress_comment._FakeClient) is "a.py": line 1 is
    # context, line 2 is the only added line.
    _run = trs.RequiredStatusTest._run

    def _body(self, results):
        posted, client, _ = self._run(results)
        self.assertTrue(posted)
        return client.review_bodies[-1], client

    def test_a_required_p1_on_a_context_line_is_listed_with_its_reason(self) -> None:
        body, client = self._body({"arch": {"overall_correctness": "patch is incorrect", "findings": [_finding(1)]}})
        # The gate is unchanged: the required status still fails.
        self.assertEqual(trs.RequiredStatusTest.states(self, client)[-1], ("failure", "arch: P1 finding"))
        self.assertIn(SECTION, body)
        self.assertIn("- **P1** `a.py:1` (arch): nullable value passed to Assert.True", body)
        self.assertIn("> ReadOtpSelected returns bool?; CS1503.", body)
        # It is shown before the agent summaries, under the failed-gate note.
        self.assertLess(body.index("Required review failed"), body.index(SECTION))
        self.assertLess(body.index(SECTION), body.index("### Agent summaries"))

    def test_a_p1_on_an_added_line_stays_a_line_comment_only(self) -> None:
        body, _ = self._body({"arch": {"overall_correctness": "patch is incorrect", "findings": [_finding(2)]}})
        self.assertNotIn(SECTION, body)
        self.assertIn("`a.py:2` (arch)", body)

    def test_a_p2_off_the_diff_is_not_listed(self) -> None:
        body, _ = self._body({"arch": {"findings": [_finding(1, priority=2, title="[P2] minor")]}})
        self.assertNotIn(SECTION, body)

    def test_every_reason_a_blocker_is_dropped_is_covered(self) -> None:
        findings = {
            "arch": {"findings": [
                _finding(None, title="[P1] file-level blocker"),            # no line
                _finding(2, priority=0, title="[P0] empty body", body=""),  # added line, no body
            ]},
            "a": {"findings": [_finding(5, title="[P1] outside the PR", path="other.py")]},  # non-required agent
            "b": {"findings": [{"path": "a.py", "line": 1, "severity": "high", "title": "no number", "body": "x"}]},
        }
        body, _ = self._body(findings)
        self.assertIn("- **P1** `a.py` (arch): file-level blocker", body)
        self.assertIn("- **P0** `a.py:2` (arch): empty body", body)
        self.assertIn("- **P1** `other.py:5` (a): outside the PR", body)
        self.assertIn("- **HIGH** `a.py:1` (b): no number", body)

    def test_a_body_with_fenced_code_cannot_swallow_the_review(self) -> None:
        text = "Before the code.\n```csharp\nAssert.True(x);\n```\nAfter the code."
        body, _ = self._body({"arch": {"findings": [_finding(1, body=text)]}})
        section = body[body.index(SECTION):body.index("### Agent summaries")]
        self.assertIn("> Before the code.", section)
        self.assertNotIn("```", section)
        self.assertNotIn("After the code.", section)

    def test_a_body_with_raw_html_cannot_swallow_the_review(self) -> None:
        text = "Before the tag. <details>open and never closed"
        body, _ = self._body({"arch": {"findings": [_finding(1, body=text)]}})
        section = body[body.index(SECTION):body.index("### Agent summaries")]
        self.assertIn("> Before the tag.", section)
        self.assertNotIn("<details>", section)
        self.assertNotIn("never closed", section)

    def test_an_overflowing_number_does_not_stop_the_review(self) -> None:
        findings = [
            {**_finding(1, title="[P1] huge confidence"), "confidence": 10**400},
            {**_finding(None, title="[P1] infinite line"), "line": float("inf")},
        ]
        body, client = self._body({"arch": {"findings": findings}})
        self.assertEqual(client.review_calls, 1)
        self.assertIn("- **P1** `a.py:1` (arch): huge confidence", body)
        self.assertIn("- **P1** `a.py` (arch): infinite line", body)

    def test_an_infinite_priority_does_not_stop_the_review(self) -> None:
        # JSON allows Infinity; int(inf) raises OverflowError. The title's [P1] then gives the priority.
        findings = [
            {**_finding(1, title="[P1] context line"), "priority": float("inf")},
            {**_finding(2, title="[P1] added line"), "priority": float("inf")},
        ]
        body, client = self._body({"arch": {"overall_correctness": "patch is correct", "findings": findings}})
        self.assertEqual(client.review_calls, 1)
        self.assertEqual(trs.RequiredStatusTest.states(self, client)[-1], ("failure", "arch: P1 finding"))
        self.assertIn("- **P1** `a.py:1` (arch): context line", body)
        self.assertIn("`a.py:2` (arch)", body)

    def test_the_list_is_bounded(self) -> None:
        many = [_finding(1, title=f"[P1] blocker {n}", body="b" * 5000) for n in range(30)]
        body, _ = self._body({"arch": {"findings": many}})
        section = body[body.index(SECTION):body.index("### Agent summaries")]
        self.assertLessEqual(section.count("- **P1**"), svc.MAX_UNPOSTED_BLOCKERS_SHOWN)
        self.assertIn(f"{30 - svc.MAX_UNPOSTED_BLOCKERS_SHOWN} more", section)
        self.assertLess(len(section), 20000)


    def test_an_agent_supplied_path_cannot_break_the_span_or_the_size_limit(self) -> None:
        huge = "dir/" + "x" * 70000 + ".cs"
        findings = [_finding(3, title="[P1] huge path", path=huge), _finding(None, title="[P1] tick", path="a`b\nc.py")]
        body, client = self._body({"arch": {"findings": findings}})
        self.assertEqual(client.review_calls, 1)
        self.assertLess(len(body), svc.MAX_REVIEW_BODY_CHARS)
        section = body[body.index(SECTION):body.index("### Agent summaries")]
        self.assertLess(len(section), 5000)
        self.assertIn("…:3` (arch): huge path", section)
        self.assertIn("- **P1** `a'b c.py` (arch): tick", section)

    def test_an_agent_supplied_line_or_title_cannot_break_the_review(self) -> None:
        findings = [
            _finding(10**100000, title="[P1] huge line"),  # not int("9" * N): 3.11+ caps str->int at 4300 digits
            _finding(None, title="[P1] use <details> here"),
        ]
        body, client = self._body({"arch": {"findings": findings}})
        self.assertEqual(client.review_calls, 1)
        section = body[body.index(SECTION):body.index("### Agent summaries")]
        self.assertLess(len(section), 5000)
        self.assertIn("- **P1** `a.py` (arch): huge line", section)
        self.assertIn("- **P1** `a.py` (arch): use &lt;details&gt; here", section)
        self.assertNotIn("<details>", section)


class ExecutiveSummaryTest(unittest.TestCase):
    def test_the_summary_prompt_carries_the_unposted_blockers(self) -> None:
        blocker = svc.Finding("arch", "a.py", 1, "high", "[P1] nullable value", "CS1503", 0.9, 1)
        pr = {"number": 3, "head": {"sha": trs.HEAD}, "title": "t", "body": ""}
        prompt = svc.render_summary_prompt("o/r", pr, [], [], [], "instr", unposted_blockers=[blocker])
        self.assertIn("Blocking findings not on an added line JSON", prompt)
        payload = prompt.split("Blocking findings not on an added line JSON", 1)[1]
        self.assertIn('"line": 1', payload)
        self.assertIn("[P1] nullable value", payload)
        self.assertIn("never write that no P0/P1 findings remain", prompt)

    def test_review_pr_gives_the_summary_the_dropped_p1(self) -> None:
        seen = {}

        def fake_summary(config, config_dir, run_dir, repo, pr, files, results, findings, **kwargs):
            seen["findings"] = findings
            seen["unposted"] = kwargs.get("unposted_blockers")
            return None  # the fallback summary is built instead

        def fake_run_agent(agent, *args, **kwargs):
            extra = {"overall_correctness": "patch is incorrect", "findings": [_finding(1)]} if agent["id"] == "arch" else {}
            return {"agent": agent["id"], "summary": "ok", "findings": [], **extra}

        client = trs._StatusClient()
        summaries = []
        original = client.upsert_issue_comment

        def capture(number, marker, body):
            if marker == svc.SUMMARY_MARKER:
                summaries.append(body)
            return original(number, marker, body)

        client.upsert_issue_comment = capture
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = _config(tmp_path / "work")
            config["executive_summary"] = {"enabled": True}
            pr = {"number": 3, "head": {"sha": trs.HEAD}, "base": {"sha": "b" * 12}, "title": "t", "body": "",
                  "user": {"login": "x"}, "html_url": trs.PR_URL}
            with mock.patch.object(svc, "run_agent", fake_run_agent), \
                 mock.patch.object(svc, "run_executive_summary_agent", fake_summary), \
                 mock.patch.object(svc, "clone_pr_source", lambda repo, n, h, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True)), \
                 mock.patch.object(svc, "load_repository_rules", lambda *a, **k: ""):
                posted = svc.review_pr(client, config, tmp_path / "config" / "reviewer.json", pr, {"reviewed": {}},
                                       recorder=dx.DashboardRecorder(FakeStore(), logger=lambda message: None))
        self.assertTrue(posted)
        self.assertEqual(seen["findings"], [])
        self.assertEqual([(f.path, f.line, f.priority) for f in seen["unposted"]], [("a.py", 1, 1)])
        # The fallback summary does not claim there are no blocking findings.
        self.assertTrue(summaries)
        self.assertNotIn("No blocking findings were reported", summaries[-1])
        self.assertIn("`a.py:1`", summaries[-1])

    def test_the_fallback_summary_shows_a_blocker_without_a_line_as_its_path(self) -> None:
        blocker = svc.Finding("arch", "a.py", 0, "high", "[P1] file-level", "b", 0.9, 1)
        text = svc.build_fallback_executive_summary({"title": "t"}, [], [], [blocker])
        self.assertIn("`a.py` — [P1] file-level", text)
        self.assertNotIn("a.py:0", text)

    def test_the_fallback_summary_escapes_the_title(self) -> None:
        blocker = svc.Finding("arch", "a.py", 0, "high", "[P1] use <details> here", "b", 0.9, 1)
        text = svc.build_fallback_executive_summary({"title": "t"}, [], [], [blocker])
        self.assertIn("`a.py` — [P1] use &lt;details&gt; here", text)
        self.assertNotIn("<details>", text)


if __name__ == "__main__":
    unittest.main()
