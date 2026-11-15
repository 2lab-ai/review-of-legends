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

    def test_a_quote_ending_in_unchanged_context_lands_on_its_last_added_line(self) -> None:
        self.assertEqual(self.anchors.place("src/app.py", None, "return {}\nreturn parse(data)").line, 14)
        self.assertEqual(self.anchors.place("src/app.py", 15, "return {}\nreturn parse(data)").line, 14)


class NearestFirstTest(unittest.TestCase):
    # "check()" is unchanged at 10 and added at 30 of a.py; "def setup():" is unchanged in a.py and
    # added in b.py.
    FILES = [
        {"filename": "a.py", "patch": "@@ -9,2 +9,2 @@\n def setup():\n     check()\n"
                                      "@@ -28,1 +28,3 @@\n x = 1\n+y = 2\n+    check()\n"},
        {"filename": "b.py", "patch": "@@ -0,0 +1,2 @@\n+def setup():\n+    pass\n"},
    ]

    def setUp(self) -> None:
        self.anchors = anchors.DiffAnchors(self.FILES)

    def test_the_nearest_match_is_chosen_before_unchanged_ones_are_set_aside(self) -> None:
        # The agent points at the unchanged call at 10: the finding is about unchanged code.
        self.assertIsNone(self.anchors.place("a.py", 10, "check()"))
        self.assertEqual(self.anchors.place("a.py", 29, "check()").line, 30)
        self.assertIsNone(self.anchors.place("a.py", None, "check()"))

    def test_an_unchanged_match_in_the_own_file_is_not_moved_to_an_added_copy_elsewhere(self) -> None:
        self.assertIsNone(self.anchors.place("a.py", 9, "def setup():"))
        self.assertEqual(self.anchors.place("c.py", 9, "def setup():"), anchors.Placement("b.py", 1, anchors.MOVED))


class EdgeCaseTest(unittest.TestCase):
    def test_an_ambiguous_own_path_is_neither_placed_nor_moved(self) -> None:
        files = [
            {"filename": "a.py", "patch": "@@ -0,0 +1,1 @@\n+x = 1\n"},
            {"filename": "src/a.py", "patch": "@@ -0,0 +1,1 @@\n+y = 2\n"},
            {"filename": "b.py", "patch": "@@ -0,0 +1,1 @@\n+z = 3\n"},
        ]
        diff = anchors.DiffAnchors(files)
        # "/run/source/src/a.py" ends with both "src/a.py" and "a.py".
        self.assertEqual(diff.own_files("/run/source/src/a.py"), ["a.py", "src/a.py"])
        self.assertIsNone(diff.place("/run/source/src/a.py", 1, "z = 3"))
        self.assertIsNone(diff.place("/run/source/src/a.py", 1, "y = 2"))
        self.assertEqual(diff.place("/run/source/b.py", None, "z = 3"), anchors.Placement("b.py", 1, anchors.CODE))

    def test_a_move_counts_files_and_then_picks_within_the_one_file(self) -> None:
        files = [
            {"filename": "a.py", "patch": "@@ -0,0 +1,1 @@\n+pass\n"},
            {"filename": "c.py", "patch": "@@ -0,0 +1,3 @@\n+    retry()\n+x\n+    retry()\n"},
        ]
        diff = anchors.DiffAnchors(files)
        self.assertEqual(diff.place("a.py", 3, "retry()"), anchors.Placement("c.py", 3, anchors.MOVED))
        self.assertEqual(diff.place("a.py", 1, "retry()"), anchors.Placement("c.py", 1, anchors.MOVED))
        self.assertIsNone(diff.place("a.py", None, "retry()"))

    def test_a_no_newline_marker_does_not_shift_the_new_side(self) -> None:
        patch = "@@ -1,2 +1,2 @@\n keep\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n"
        diff = anchors.DiffAnchors([{"filename": "a.txt", "patch": patch}])
        self.assertEqual(diff.place("a.txt", None, "new"), anchors.Placement("a.txt", 2, anchors.CODE))

    def test_a_multi_line_quote_does_not_span_two_hunks(self) -> None:
        diff = anchors.DiffAnchors(FILES)
        # src/util.py: hunk 1 ends with "return {}" at 3, hunk 2 starts with "def b():" at 40.
        self.assertIsNone(diff.place("src/util.py", 3, "return {}\ndef b():"))
        self.assertEqual(diff.find("src/util.py", ["return {}", "def b():"]), [])

    def test_a_move_also_chooses_nearest_first_including_unchanged_matches(self) -> None:
        files = [
            {"filename": "a.py", "patch": "@@ -0,0 +1,1 @@\n+pass\n"},
            {"filename": "c.py", "patch": "@@ -4,1 +4,1 @@\n     retry()\n@@ -29,1 +29,2 @@\n x\n+    retry()\n"},
        ]
        diff = anchors.DiffAnchors(files)
        # Nearest to line 4 is c.py's unchanged call: the finding is about unchanged code.
        self.assertIsNone(diff.place("a.py", 4, "retry()"))
        self.assertEqual(diff.place("a.py", 30, "retry()"), anchors.Placement("c.py", 30, anchors.MOVED))

    def test_the_quote_beats_another_added_line_the_agent_named(self) -> None:
        diff = anchors.DiffAnchors(FILES)
        # Line 14 is an added line too, but the quote names line 12.
        self.assertEqual(diff.place("src/app.py", 14, "data = read(path)"),
                         anchors.Placement("src/app.py", 12, anchors.CODE))

    def test_a_unicode_line_separator_inside_a_line_does_not_shift_lines(self) -> None:
        patch = "@@ -0,0 +1,2 @@\n+label = 'a b'\n+value = 2\n"
        diff = anchors.DiffAnchors([{"filename": "u.py", "patch": patch}])
        self.assertEqual(diff.place("u.py", None, "value = 2"), anchors.Placement("u.py", 2, anchors.CODE))

    def test_every_file_a_quote_is_in_counts_as_raised(self) -> None:
        files = [
            {"filename": "a.py", "patch": "@@ -0,0 +1,1 @@\n+pass\n"},
            {"filename": "b.py", "patch": "@@ -0,0 +1,1 @@\n+    retry()\n"},
            {"filename": "c.py", "patch": "@@ -0,0 +1,1 @@\n+    retry()\n"},
        ]
        diff = anchors.DiffAnchors(files)
        raw = {"path": "a.py", "line": 1, "priority": 2, "title": "[P2] retry", "body": "b", "existing_code": "retry()"}
        # Ambiguous now (b.py and c.py): no placement, yet a thread moved to b.py earlier stays raised.
        self.assertIsNone(diff.place("a.py", 1, "retry()"))
        keys = svc.raised_finding_keys([{"agent": "c", "findings": [raw]}], ["a.py", "b.py", "c.py"],
                                       allowed=svc.commentable_added_lines(files), diff_anchors=diff)
        title = svc.normalize_finding_title("[P2] retry")
        self.assertTrue({("a.py", title), ("b.py", title), ("c.py", title)} <= keys)


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

    def test_the_unplaced_section_fits_the_budget_or_is_left_out(self) -> None:
        unplaced = [svc.Finding("tests", f"src/m{i}.py", 1, "low", f"[P3] t{i}", "b", 0.0, 3) for i in range(30)]
        tight = [120]
        self.assertEqual(svc.unplaced_findings_section(unplaced, tight), [])
        self.assertEqual(tight, [120])  # nothing taken when nothing is shown
        roomy = [5000]
        section = svc.unplaced_findings_section(unplaced, roomy)
        self.assertEqual(section[-2:], ["</details>", ""])
        self.assertIn("- …and 10 more", section)
        self.assertEqual(roomy[0], 5000 - sum(len(line) + 1 for line in section))

    def test_a_moved_finding_counts_as_raised_under_both_paths(self) -> None:
        raw = {"path": "src/app.py", "line": 3, "priority": 3, "title": "[P3] elsewhere", "body": "b",
               "existing_code": "x = 1"}
        names = [item["filename"] for item in FILES]
        keys = svc.raised_finding_keys(self.result(raw), names, allowed=self.allowed, diff_anchors=self.anchors)
        title = svc.normalize_finding_title("[P3] elsewhere")
        self.assertIn(("src/app.py", title), keys)
        self.assertIn(("src/util.py", title), keys)
        self.assertNotIn(("src/util.py", title), svc.raised_finding_keys(self.result(raw), names))

    def test_a_pathless_finding_placed_by_its_quote_is_raised_under_that_path(self) -> None:
        raw = {"line": "?", "priority": 2, "title": "[P2] no path", "body": "b", "existing_code": "x = 1"}
        findings = svc.normalize_findings(self.result(raw), self.allowed, self.anchors)
        self.assertEqual([(f.path, f.line) for f in findings], [("src/util.py", 2)])
        keys = svc.raised_finding_keys(self.result(raw), [item["filename"] for item in FILES],
                                       allowed=self.allowed, diff_anchors=self.anchors)
        self.assertEqual(keys, frozenset({("src/util.py", svc.normalize_finding_title("[P2] no path"))}))

    def test_a_p1_with_a_line_that_is_not_a_number_is_placed_by_its_quote(self) -> None:
        raw = {"path": "src/app.py", "line": "unknown", "priority": 1, "title": "[P1] none", "body": "b",
               "existing_code": "if data is None:"}
        findings = svc.normalize_findings(self.result(raw), self.allowed, self.anchors)
        self.assertEqual([(f.path, f.line) for f in findings], [("src/app.py", 13)])
        self.assertEqual(svc.unposted_blocking_findings(self.result(raw), self.allowed, self.anchors), [])

    def test_a_crowded_body_stays_under_the_limit_with_its_footer_and_balanced_details(self) -> None:
        pr = {"number": 7, "head": {"sha": "abc1234567"}}
        unplaced = [svc.Finding("tests", f"src/m{i}.py", 1, "medium", "[P2] " + "x" * 400, "b", 0.0, 2)
                    for i in range(60)]

        def body(summary_chars: int, with_unplaced: bool) -> str:
            results = [{"agent": "correctness", "summary": "y" * summary_chars}]
            return svc.build_review_body({"marker": svc.MARKER}, pr, results, [], 0, "v",
                                         unplaced=unplaced if with_unplaced else None)

        fixed = len(body(0, False))
        for spare in (50, 300, 3000, 30000):
            # The rest of the body leaves ``spare`` characters under the limit.
            text = body(svc.MAX_REVIEW_BODY_CHARS - spare - fixed, True)
            self.assertLessEqual(len(text), svc.MAX_REVIEW_BODY_CHARS, spare)
            self.assertTrue(text.endswith("_Generated by Review of Legends._"), spare)
            self.assertEqual(text.count("<details>"), text.count("</details>"), spare)
            # 50 left: no room for the heading, no section; from 300 on it shows, cut with "…and N more".
            self.assertEqual("<details>" in text, spare >= 300, spare)
            if spare >= 300:
                self.assertIn("more", text.split("<details>")[1], spare)


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
            evidence: list = []
            real_plan = svc.plan_review_threads

            def capture_plan(*args, **kwargs):
                evidence.append(kwargs.get("evidence"))
                return real_plan(*args, **kwargs)

            with mock.patch.object(svc, "clone_pr_source", lambda *a, **k: None), \
                    mock.patch.object(svc, "run_agent", fake_run_agent), \
                    mock.patch.object(svc, "plan_review_threads", capture_plan):
                svc.review_pr(client, config, ROOT / "config" / "reviewer.json", pr, {"reviewed": {}})
        body, comments = client.reviews[0]
        self.assertEqual(sorted((c["path"], c["line"]) for c in comments), [("src/app.py", 13), ("src/util.py", 2)])
        self.assertIn("1 finding with no place on an added line", body)
        self.assertIn("- **P2** `src/app.py:15` (correctness): old code", body)
        # The finding moved to src/util.py keeps a thread there open on the next review.
        self.assertIn(("src/util.py", svc.normalize_finding_title("[P3] elsewhere")), evidence[0].raised)


if __name__ == "__main__":
    unittest.main()
