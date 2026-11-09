"""Tests for ``commentable_added_lines`` diff parsing.

These cover the cases the PR review flagged:
- hunk header range parsing
- "@@" hunk headers, and "---"/"+++" file headers before the first hunk, not
  counted; inside a hunk "+++i;" is the added line "++i;"
- removed lines ("-") not advancing the new-side cursor
- "\\ No newline at end of file" not advancing the new-side cursor
- blank/context lines advancing the cursor
- multiple hunks in the same file
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service.service import commentable_added_lines


class CommentableAddedLinesTest(unittest.TestCase):
    def test_added_lines_in_single_hunk(self) -> None:
        patch = (
            "@@ -1,3 +1,4 @@\n"
            " context_one\n"
            "+added_two\n"
            " context_three\n"
            "+added_four\n"
        )
        result = commentable_added_lines([{"filename": "a.py", "patch": patch}])
        self.assertEqual(result, {"a.py": {2, 4}})

    def test_removed_lines_do_not_advance_new_side(self) -> None:
        patch = (
            "@@ -1,3 +1,2 @@\n"
            " context_one\n"
            "-removed_two\n"
            "+added_two\n"
        )
        result = commentable_added_lines([{"filename": "b.py", "patch": patch}])
        self.assertEqual(result, {"b.py": {2}})

    def test_no_newline_marker_is_ignored(self) -> None:
        patch = (
            "@@ -1,2 +1,2 @@\n"
            " context_one\n"
            "-removed_two\n"
            "\\ No newline at end of file\n"
            "+added_two\n"
            "\\ No newline at end of file\n"
        )
        result = commentable_added_lines([{"filename": "c.py", "patch": patch}])
        self.assertEqual(result, {"c.py": {2}})

    def test_multiple_hunks(self) -> None:
        patch = (
            "@@ -1,2 +1,2 @@\n"
            " context_one\n"
            "+added_two\n"
            "@@ -10,2 +11,3 @@\n"
            " context_eleven\n"
            "+added_twelve\n"
            "+added_thirteen\n"
        )
        result = commentable_added_lines([{"filename": "d.py", "patch": patch}])
        self.assertEqual(result, {"d.py": {2, 12, 13}})

    def test_added_at_start_of_hunk(self) -> None:
        patch = (
            "@@ -0,0 +1,2 @@\n"
            "+first\n"
            "+second\n"
        )
        result = commentable_added_lines([{"filename": "e.py", "patch": patch}])
        self.assertEqual(result, {"e.py": {1, 2}})

    def test_added_line_starting_with_plus_plus_is_counted(self) -> None:
        # GitHub's per-file patch has no file headers, so "+++i;" is the
        # added line "++i;" and must not shift the lines after it.
        patch = (
            "@@ -0,0 +1,3 @@\n"
            "+a\n"
            "+++i;\n"
            "+b\n"
        )
        result = commentable_added_lines([{"filename": "g.c", "patch": patch}])
        self.assertEqual(result, {"g.c": {1, 2, 3}})

    def test_added_lines_starting_with_increment_and_decrement(self) -> None:
        patch = (
            "@@ -1,2 +1,4 @@\n"
            " a\n"
            "+++i;\n"
            "+--j;\n"
            " b\n"
        )
        result = commentable_added_lines([{"filename": "h.c", "patch": patch}])
        self.assertEqual(result, {"h.c": {2, 3}})

    def test_file_headers_before_first_hunk_are_ignored(self) -> None:
        patch = (
            "--- a/x.c\n"
            "+++ b/x.c\n"
            "@@ -1,2 +1,3 @@\n"
            " a\n"
            "+++i;\n"
            " b\n"
        )
        result = commentable_added_lines([{"filename": "x.c", "patch": patch}])
        self.assertEqual(result, {"x.c": {2}})

    def test_skip_file_without_patch(self) -> None:
        result = commentable_added_lines([{"filename": "f.py"}])
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
