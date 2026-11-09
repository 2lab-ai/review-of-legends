"""Tests for ``normalize_findings``.

Covers:
- JSON ``[P#]`` extraction in title and severity mapping
- ``code_location.absolute_file_path`` path extraction
- ``code_location.line_range.start`` line extraction
- dedupe on (path, line, normalize_finding_title), keeping the finding that
  ranks first (finding_sort_key) and, among equals, the first in agent order
- a null ``path``/``absolute_file_path``/``body`` counts as missing, never "None"
- off-diff (line not in ``allowed``) findings are filtered out
- suffix path resolution when the agent returned an absolute path
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service.service import Finding, normalize_findings


class NormalizeFindingsTest(unittest.TestCase):
    def test_basic_finding_passes_when_in_allowed_range(self) -> None:
        results = [
            {
                "agent": "correctness",
                "findings": [
                    {
                        "path": "a.py",
                        "line": 10,
                        "title": "[P1] Bad",
                        "body": "details",
                    }
                ],
            }
        ]
        out = normalize_findings(results, {"a.py": {10}})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].path, "a.py")
        self.assertEqual(out[0].line, 10)
        self.assertEqual(out[0].priority, 1)
        self.assertEqual(out[0].severity, "high")

    def test_off_diff_finding_is_filtered(self) -> None:
        results = [
            {
                "agent": "correctness",
                "findings": [
                    {
                        "path": "a.py",
                        "line": 999,
                        "title": "Outside",
                        "body": "x",
                    }
                ],
            }
        ]
        out = normalize_findings(results, {"a.py": {10}})
        self.assertEqual(out, [])

    def test_dedupe_same_path_line_title(self) -> None:
        results = [
            {
                "agent": "correctness",
                "findings": [
                    {"path": "a.py", "line": 5, "title": "Dup", "body": "x"},
                ],
            },
            {
                "agent": "security",
                "findings": [
                    {"path": "a.py", "line": 5, "title": "DUP", "body": "x"},
                ],
            },
        ]
        out = normalize_findings(results, {"a.py": {5}})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].agent, "correctness", "equal findings keep agent order")

    def test_dedupe_ignores_the_priority_prefix(self) -> None:
        results = [
            {"agent": "correctness", "findings": [{"path": "a.py", "line": 5, "title": "Bad", "body": "x"}]},
            {"agent": "security", "findings": [{"path": "a.py", "line": 5, "title": "[P1] Bad", "body": "x"}]},
        ]
        out = normalize_findings(results, {"a.py": {5}})
        self.assertEqual([(f.agent, f.priority) for f in out], [("security", 1)])

    def test_dedupe_keeps_the_highest_priority_duplicate(self) -> None:
        results = [
            {"agent": "c", "findings": [{"path": "a.py", "line": 5, "title": "Dup", "body": "x", "priority": 3}]},
            {"agent": "s", "findings": [{"path": "a.py", "line": 5, "title": "dup", "body": "x", "priority": 0}]},
        ]
        out = normalize_findings(results, {"a.py": {5}})
        self.assertEqual([(f.agent, f.priority, f.severity) for f in out], [("s", 0, "high")])

    def test_dedupe_ranks_duplicates_by_the_fields_the_comment_shows(self) -> None:
        # The kept duplicate is labeled, sorted, gated and held back by its own
        # priority and severity, so it must also be chosen by them. A title-only
        # "P0: " is not a priority (extract_priority reads "[P0]"; see
        # test_a_severity_header_keeps_the_agents_priority_in_the_title), so it
        # never replaces a numeric P0, whatever its confidence.
        numeric_p0 = {"agent": "c", "findings": [
            {"path": "a.py", "line": 5, "title": "crash", "body": "x", "priority": 0, "confidence": 0.5}]}
        title_p0 = {"agent": "s", "findings": [
            {"path": "a.py", "line": 5, "title": "P0: crash", "body": "y", "confidence": 0.9}]}
        bracket_p2 = {"agent": "b", "findings": [{"path": "a.py", "line": 5, "title": "[P2] crash", "body": "z"}]}
        for results, kept in (([numeric_p0, title_p0], ("c", 0, "high")), ([title_p0, numeric_p0], ("c", 0, "high")),
                              ([title_p0, bracket_p2], ("b", 2, "medium")), ([bracket_p2, title_p0], ("b", 2, "medium"))):
            with self.subTest(order=[r["agent"] for r in results]):
                out = normalize_findings(results, {"a.py": {5}})
                self.assertEqual([(f.agent, f.priority, f.severity) for f in out], [kept])

    def test_null_path_falls_back_to_code_location(self) -> None:
        raw = {"code_location": {"absolute_file_path": "/r/a.py", "line_range": {"start": 3}},
               "title": "t", "body": "b", "priority": 1}
        for record in (raw, dict(raw, path=None)):
            with self.subTest(null_path="path" in record):
                out = normalize_findings([{"agent": "correctness", "findings": [record]}], {"r/a.py": {3}})
                self.assertEqual([f.path for f in out], ["r/a.py"])

    def test_null_path_is_not_a_file_named_none(self) -> None:
        results = [
            {
                "agent": "correctness",
                "findings": [
                    {"path": None, "line": 1, "title": "x", "body": "y"},
                    {"code_location": {"absolute_file_path": None, "line_range": {"start": 1}}, "title": "z", "body": "y"},
                ],
            }
        ]
        self.assertEqual(normalize_findings(results, {"None": {1}}), [])

    def test_code_location_path_and_line_range(self) -> None:
        results = [
            {
                "agent": "correctness",
                "findings": [
                    {
                        "code_location": {
                            "absolute_file_path": "/repo/foo/a.py",
                            "line_range": {"start": 7, "end": 9},
                        },
                        "title": "[P2] Issue",
                        "body": "x",
                    }
                ],
            }
        ]
        out = normalize_findings(results, {"foo/a.py": {7}})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].path, "foo/a.py")
        self.assertEqual(out[0].line, 7)
        self.assertEqual(out[0].priority, 2)
        self.assertEqual(out[0].severity, "medium")

    def test_empty_or_null_body_is_dropped(self) -> None:
        # A null body must not post an inline comment reading "None".
        results = [
            {
                "agent": "correctness",
                "findings": [
                    {"path": "a.py", "line": 1, "title": "x", "body": ""},
                    {"path": "a.py", "line": 1, "title": "y", "body": None},
                ],
            }
        ]
        out = normalize_findings(results, {"a.py": {1}})
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
