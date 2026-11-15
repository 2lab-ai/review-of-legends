from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import anchors
from code_review_service import service as svc

# New side: 10 def load(path): / 11 (blank) / 12 data = read(path) [added] / 13 if data is None: [added]
# / 14     return {} [added] / 15 return parse(data); the old "data = open(path)" line is deleted.
APP_PATCH = (
    "@@ -10,4 +10,6 @@\n"
    " def load(path):\n"
    " \n"
    "-    data = open(path)\n"
    "+    data = read(path)\n"
    "+    if data is None:\n"
    "+        return {}\n"
    "     return parse(data)\n"
)
# Two hunks; "return {}" is added at 3 and at 41.
UTIL_PATCH = (
    "@@ -1,2 +1,3 @@\n"
    " def a():\n"
    "+    x = 1\n"
    "+    return {}\n"
    "@@ -40,1 +40,2 @@\n"
    " def b():\n"
    "+    return {}\n"
)
FILES = [
    {"filename": "src/app.py", "status": "modified", "patch": APP_PATCH},
    {"filename": "src/util.py", "status": "modified", "patch": UTIL_PATCH},
    {"filename": "logo.png", "status": "added"},
]


class DiffAnchorsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.anchors = anchors.DiffAnchors(FILES)

    def test_the_new_side_skips_deleted_and_blank_lines(self) -> None:
        hunk = self.anchors.hunks["src/app.py"][0]
        self.assertEqual(
            [(item.line, item.text, item.added) for item in hunk],
            [(10, "def load(path):", False), (12, "data = read(path)", True), (13, "if data is None:", True),
             (14, "return {}", True), (15, "return parse(data)", False)],
        )
        self.assertNotIn("logo.png", self.anchors.hunks)

    def test_the_quote_corrects_a_miscounted_line(self) -> None:
        placed = self.anchors.place("src/app.py", 11, "    data = read(path)")
        self.assertEqual(placed, anchors.Placement("src/app.py", 12, anchors.CODE))

    def test_a_multi_line_quote_keeps_the_agents_line_inside_it_else_its_last_added_line(self) -> None:
        quote = "if data is None:\n    return {}"
        self.assertEqual(self.anchors.place("src/app.py", 13, quote).line, 13)
        self.assertEqual(self.anchors.place("src/app.py", 99, quote).line, 14)
        self.assertEqual(self.anchors.place("src/app.py", None, quote).line, 14)

    def test_whitespace_blank_lines_and_diff_markers_are_tolerated(self) -> None:
        self.assertEqual(self.anchors.place("src/app.py", None, "data   =  read(path)\n\n").line, 12)
        self.assertEqual(self.anchors.place("src/app.py", None, "+    data = read(path)\n+    if data is None:").line, 13)

    def test_a_quote_of_unchanged_code_places_nothing(self) -> None:
        self.assertIsNone(self.anchors.place("src/app.py", 15, "return parse(data)"))
        self.assertIsNone(self.anchors.place("src/app.py", 12, "    data = open(path)"))  # the deleted line

    def test_a_repeated_quote_takes_the_match_nearest_the_agents_line(self) -> None:
        self.assertEqual(self.anchors.place("src/util.py", 40, "return {}").line, 41)
        self.assertEqual(self.anchors.place("src/util.py", 2, "return {}").line, 3)
        self.assertIsNone(self.anchors.place("src/util.py", None, "return {}"))
        self.assertIsNone(self.anchors.place("src/util.py", 22, "return {}"))  # equally far: no pick

    def test_a_quote_from_another_file_moves_the_finding_only_when_one_file_has_it(self) -> None:
        moved = self.anchors.place("src/app.py", 3, "x = 1")
        self.assertEqual(moved, anchors.Placement("src/util.py", 2, anchors.MOVED))
        # "return {}" is in src/util.py (twice) and src/app.py: from a third name, two files have it.
        self.assertIsNone(self.anchors.place("src/other.py", 3, "return {}"))

    def test_an_absolute_path_resolves_and_bad_quotes_place_nothing(self) -> None:
        self.assertEqual(self.anchors.place("/tmp/run/source/src/app.py", None, "data = read(path)").path, "src/app.py")
        for quote in (None, "", "   \n ", 42, ["data = read(path)"]):
            self.assertIsNone(self.anchors.place("src/app.py", 12, quote), repr(quote))


class ServicePlacementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.allowed = svc.commentable_added_lines(FILES)
        self.anchors = anchors.DiffAnchors(FILES)

    def result(self, *findings: dict) -> list[dict]:
        return [{"agent": "correctness", "findings": list(findings)}]

    def test_a_p1_on_a_blank_line_with_a_quote_is_posted_not_left_unposted(self) -> None:
        raw = {"path": "src/app.py", "line": 11, "priority": 1, "title": "[P1] none", "body": "b",
               "existing_code": "if data is None:"}
        findings = svc.normalize_findings(self.result(raw), self.allowed, self.anchors)
        self.assertEqual([(f.path, f.line) for f in findings], [("src/app.py", 13)])
        self.assertEqual(svc.unposted_blocking_findings(self.result(raw), self.allowed, self.anchors), [])
        # Without anchors the old behaviour holds: line 11 is no added line.
        self.assertEqual(svc.normalize_findings(self.result(raw), self.allowed), [])
        self.assertEqual(len(svc.unposted_blocking_findings(self.result(raw), self.allowed)), 1)

    def test_the_agents_line_still_places_a_finding_without_a_usable_quote(self) -> None:
        raw = {"path": "src/app.py", "line": 14, "priority": 2, "title": "[P2] x", "body": "b",
               "existing_code": "not in the diff"}
        findings = svc.normalize_findings(self.result(raw), self.allowed, self.anchors)
        self.assertEqual([(f.path, f.line) for f in findings], [("src/app.py", 14)])

    def test_findings_without_a_place_are_kept_for_the_body(self) -> None:
        minor = {"path": "src/app.py", "line": 15, "priority": 2, "title": "[P2] old code", "body": "b"}
        blocker = {"path": "src/app.py", "line": 15, "priority": 0, "title": "[P0] old code", "body": "b"}
        placed = {"path": "src/app.py", "line": 12, "priority": 3, "title": "[P3] ok", "body": "b"}
        results = self.result(minor, blocker, placed)
        unplaced = svc.unplaced_findings(results, self.allowed, self.anchors)
        self.assertEqual([(f.title, f.line) for f in unplaced], [("[P2] old code", 15)])
        self.assertEqual([f.priority for f in svc.unposted_blocking_findings(results, self.allowed, self.anchors)], [0])
        self.assertEqual(svc.placement_counts(results, self.allowed, self.anchors),
                         {"code": 0, "moved": 0, "line": 1, "none": 2})

    def test_the_body_lists_unplaced_findings_collapsed(self) -> None:
        finding = svc.Finding("tests", "src/app.py", 15, "medium", "[P2] <b>old</b> code", "b", 0.0, 2)
        pr = {"number": 7, "head": {"sha": "abc1234567"}}
        body = svc.build_review_body({"marker": svc.MARKER}, pr, [], [], 0, "v", unplaced=[finding])
        self.assertIn("<details>\n<summary>1 finding with no place on an added line</summary>", body)
        self.assertIn("- **P2** `src/app.py:15` (tests): &lt;b&gt;old&lt;/b&gt; code", body)
        self.assertIn("</details>", body)
        self.assertNotIn("<details>", svc.build_review_body({"marker": svc.MARKER}, pr, [], [], 0, "v"))

    def test_the_output_contract_asks_for_the_quoted_code(self) -> None:
        pr = {"number": 7, "title": "t", "body": "", "base": {"ref": "main"}, "head": {"ref": "f"}}
        prompt = svc.render_prompt("acme-corp/app", pr, FILES, "diff", "agent")
        self.assertIn('"existing_code": "the changed line(s) the finding is about', prompt)
        self.assertIn("Set existing_code to the code the finding is about", prompt)


class _Client:
    repo = "acme-corp/app"

    def __init__(self) -> None:
        self.reviews: list[tuple[str, list]] = []

    def bot_login(self):
        return "review-of-legends-bot"

    def list_files(self, number):
        return [dict(item) for item in FILES]

    def create_review(self, number, commit_id, body, comments, event="COMMENT"):
        self.reviews.append((body, comments))
        return {"id": 1}

    def upsert_issue_comment(self, number, marker, body):
        return {"id": 2}

    def create_commit_status(self, *args, **kwargs):
        return None


class ReviewPrPlacementTest(unittest.TestCase):
    def test_a_review_posts_on_the_quoted_line_and_lists_the_unplaced(self) -> None:
        def fake_run_agent(agent, *args, **kwargs):
            return {"agent": agent["id"], "summary": "ok", "findings": [
                {"path": "src/app.py", "line": 10, "priority": 2, "title": "[P2] None check", "body": "b",
                 "existing_code": "if data is None:"},
                {"path": "src/app.py", "line": 3, "priority": 3, "title": "[P3] elsewhere", "body": "b",
                 "existing_code": "x = 1"},
                {"path": "src/app.py", "line": 15, "priority": 2, "title": "[P2] old code", "body": "b"},
            ]}

        client = _Client()
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "agents": [{"id": "correctness", "enabled": True, "prompt_path": "../agents/correctness.md"}],
                "agent_runner": {"command": ["true"]},
                "min_successful_agents": 0,
                "max_review_comments": 30,
                "max_diff_chars": 180000,
                "work_dir": str(Path(tmp) / "work"),
                "progress_comment": {"enabled": False},
                "executive_summary": {"enabled": False},
                "visual_outline": {"enabled": False},
            }
            pr = {"number": 5, "head": {"sha": "abcdef1234567890"}, "base": {"ref": "main"},
                  "title": "t", "body": "", "user": {"login": "someone"}}
            with mock.patch.object(svc, "clone_pr_source", lambda *a, **k: None), \
                    mock.patch.object(svc, "run_agent", fake_run_agent):
                svc.review_pr(client, config, ROOT / "config" / "reviewer.json", pr, {"reviewed": {}})
        body, comments = client.reviews[0]
        self.assertEqual(sorted((c["path"], c["line"]) for c in comments), [("src/app.py", 13), ("src/util.py", 2)])
        self.assertIn("1 finding with no place on an added line", body)
        self.assertIn("- **P2** `src/app.py:15` (correctness): old code", body)


if __name__ == "__main__":
    unittest.main()
