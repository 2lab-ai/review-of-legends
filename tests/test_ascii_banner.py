from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import service as svc


class _FakeClient:
    repo = "owner/repo"

    def list_files(self, number):
        return [
            {
                "filename": "a.py",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
                "changes": 1,
                "patch": "@@ -1,1 +1,2 @@\n context\n+added\n",
            }
        ]

    def create_review(self, number, commit_id, body, comments):
        raise AssertionError("dry-run must not post reviews")


def _make_config(work_dir: Path) -> dict:
    return {
        "agents": [{"id": "fake", "enabled": True, "prompt_path": "../agents/correctness.md"}],
        "agent_runner": {"command": ["true"]},
        "min_successful_agents": 0,
        "max_review_comments": 30,
        "max_diff_chars": 180000,
        "work_dir": str(work_dir),
        "marker": svc.MARKER,
        "summary_marker": svc.SUMMARY_MARKER,
        "progress_comment": {"enabled": False},
        "executive_summary": {"enabled": True, "prompt_path": "../agents/executive-summary.md"},
    }


class AsciiBannerTest(unittest.TestCase):
    def test_format_ascii_banner_uses_stable_box_shape(self) -> None:
        banner = svc.format_ascii_banner("Executive Summary", ["Repo: owner/repo", "PR: #12"])

        self.assertIn("|    EXECUTIVE SUMMARY     |", banner)
        self.assertIn("| Repo: owner/repo         |", banner)
        self.assertIn("| PR: #12                  |", banner)
        self.assertTrue(all(ord(char) < 128 for char in banner))

    def test_format_ascii_banner_preserves_long_metadata(self) -> None:
        long_repo = "acme-corp/review-of-legends-with-a-noticeably-long-suffix"
        long_line = f"Repo: {long_repo}"
        banner = svc.format_ascii_banner("Executive Summary", [long_line, "PR: #99"])

        self.assertIn(long_line, banner)
        rows = [row for row in banner.splitlines() if row.startswith("| Repo:")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], f"| {long_line} |")
        expected_len = len(long_line) + 4
        for row in banner.splitlines():
            self.assertEqual(len(row), expected_len)

    def test_dry_run_prints_executive_summary_with_ascii_banner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = _make_config(tmp_path / "work")
            pr = {"number": 12, "head": {"sha": "abcdef1234567890"}, "title": "t", "body": ""}

            with mock.patch.object(
                svc,
                "run_agent",
                lambda *args, **kwargs: {"agent": "fake", "summary": "ok", "findings": []},
            ), mock.patch.object(
                svc, "run_executive_summary_agent", lambda *args, **kwargs: "summary body"
            ), mock.patch.object(
                svc,
                "clone_pr_source",
                lambda repo, number, head_sha, run_dir, **kwargs: Path(run_dir).mkdir(
                    parents=True, exist_ok=True
                ),
            ):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    result = svc.review_pr(
                        _FakeClient(),
                        config,
                        ROOT / "config" / "reviewer.json",
                        pr,
                        {"reviewed": {}},
                        dry_run=True,
                    )

        output = stdout.getvalue()
        self.assertTrue(result)
        self.assertIn("EXECUTIVE SUMMARY", output)
        self.assertIn("| Repo: owner/repo", output)
        self.assertIn("| PR: #12", output)
        self.assertIn("| Head: abcdef123456", output)
        self.assertIn("| Findings: 0", output)
        self.assertIn("summary body", output)


if __name__ == "__main__":
    unittest.main()
