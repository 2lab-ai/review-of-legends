from __future__ import annotations

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
    def __init__(self, fail_review: bool = False) -> None:
        self.repo = "owner/repo"
        self.progress_bodies: list[str] = []
        self.review_calls = 0
        self.review_events: list[str] = []
        self.fail_review = fail_review

    def bot_login(self):
        return "review-of-legends-bot"

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

    def create_review(self, number, commit_id, body, comments, event="COMMENT"):
        self.review_calls += 1
        self.review_events.append(event)
        if self.fail_review:
            raise svc.ServiceError("review POST failed")
        return {"id": 100}

    def upsert_issue_comment(self, number, marker, body):
        if marker == svc.PROGRESS_MARKER:
            self.progress_bodies.append(body)
        return {"id": 101}

    def create_commit_status(self, sha, state, description, target_url="", context=svc.REQUIRED_STATUS_CONTEXT):
        # Commit statuses are tested in test_required_status and test_reviewed_status.
        return {"id": 102}


def _make_config(work_dir: Path) -> dict:
    return {
        "agents": [
            {"id": "correctness", "enabled": True, "prompt_path": "../agents/correctness.md"},
            {"id": "security", "enabled": True, "prompt_path": "../agents/security.md"},
        ],
        "agent_runner": {"command": ["true"]},
        "min_successful_agents": 0,
        "max_review_comments": 30,
        "max_diff_chars": 180000,
        "work_dir": str(work_dir),
        "marker": svc.MARKER,
        "summary_marker": svc.SUMMARY_MARKER,
        "progress_marker": svc.PROGRESS_MARKER,
        "progress_comment": {"enabled": True},
        "approval": {"enabled": True, "trusted_authors": ["trusted-author"]},
        "executive_summary": {"enabled": False},
    }


class ProgressCommentTest(unittest.TestCase):
    def test_progress_body_matches_reference_shape(self) -> None:
        started = svc.dt.datetime(2026, 5, 15, 14, 57, tzinfo=svc.dt.timezone.utc)
        entries = svc.make_progress_entries(
            [
                {"id": "correctness", "prompt_path": "x"},
                {"id": "security", "prompt_path": "x"},
            ]
        )
        entries["correctness"]["status"] = "done"
        entries["correctness"]["summary"] = "P2 x 1"
        body = svc.build_progress_comment_body(
            {"progress_marker": svc.PROGRESS_MARKER},
            {"number": 7},
            "abcdef1234567890",
            entries,
            "Reviewer agents running",
            started,
        )

        self.assertIn("## Auto Review In Progress", body)
        self.assertIn("Reviewer agents **2개**", body)
        self.assertIn("**진행 상황: 1 / 2 완료**", body)
        self.assertIn("| # | Reviewer | Focus | Status | Model · effort | Tokens | 결과 요약 |", body)
        self.assertIn("| 1 | correctness | Correctness / regressions | ✅ Done | — | — | P2 x 1 |", body)
        self.assertIn("| 2 | security | Security / trust boundaries | ⏸ Queued | — | — | — |", body)
        self.assertNotIn("Model policy:", body)
        self.assertNotIn("<details>", body)
        self.assertIn("<!-- review-of-legends-progress-head-sha:abcdef1234567890 -->", body)

    def test_progress_table_cell_sanitizes_pipes_and_newlines(self) -> None:
        self.assertEqual(svc.progress_table_cell("a | b\nc"), "a \\| b c")
        self.assertEqual(svc.progress_table_cell("  "), "—")
        self.assertEqual(svc.progress_table_cell(""), "—")

    def test_progress_body_escapes_unsafe_summary(self) -> None:
        started = svc.dt.datetime(2026, 5, 15, 14, 57, tzinfo=svc.dt.timezone.utc)
        entries = svc.make_progress_entries([{"id": "correctness", "prompt_path": "x"}])
        entries["correctness"]["status"] = "done"
        entries["correctness"]["summary"] = "broke | table\nrow"
        body = svc.build_progress_comment_body(
            {"progress_marker": svc.PROGRESS_MARKER},
            {"number": 7},
            "abcdef1234567890",
            entries,
            "Reviewer agents running",
            started,
        )
        rows = [line for line in body.splitlines() if line.startswith("| 1 |")]
        self.assertEqual(len(rows), 1)
        self.assertIn("broke \\| table row", rows[0])

    def test_review_pr_marks_progress_failed_when_review_post_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            client = _FakeClient(fail_review=True)
            config = _make_config(tmp_path / "work")
            pr = {
                "number": 3,
                "head": {"sha": "abc123456789"},
                "title": "t",
                "body": "",
                "user": {"login": "trusted-author"},
            }

            def fake_run_agent(agent, *args, **kwargs):
                return {
                    "agent": agent["id"],
                    "summary": f"{agent['id']} ok",
                    "findings": [],
                }

            with mock.patch.object(svc, "run_agent", fake_run_agent):
                with mock.patch.object(
                    svc,
                    "clone_pr_source",
                    lambda repo, pr_number, head_sha, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True),
                ), self.assertRaises(svc.ServiceError):
                    svc.review_pr(
                        client, config, ROOT / "config" / "reviewer.json", pr, {"reviewed": {}}
                    )

            self.assertTrue(client.progress_bodies)
            self.assertIn("Failed to post PR review", client.progress_bodies[-1])

    def test_review_pr_skips_progress_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            client = _FakeClient()
            config = _make_config(tmp_path / "work")
            config["progress_comment"] = {"enabled": False}
            pr = {"number": 5, "head": {"sha": "def456789012"}, "title": "t", "body": ""}

            def fake_run_agent(agent, *args, **kwargs):
                return {"agent": agent["id"], "summary": "ok", "findings": []}

            with mock.patch.object(svc, "run_agent", fake_run_agent):
                with mock.patch.object(
                    svc,
                    "clone_pr_source",
                    lambda repo, pr_number, head_sha, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True),
                ):
                    result = svc.review_pr(
                        client, config, ROOT / "config" / "reviewer.json", pr, {"reviewed": {}}
                    )

            self.assertTrue(result)
            self.assertEqual(client.review_calls, 1)
            self.assertEqual(client.progress_bodies, [])

    def test_review_pr_updates_progress_comment_until_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            client = _FakeClient()
            config = _make_config(tmp_path / "work")
            pr = {
                "number": 3,
                "head": {"sha": "abc123456789"},
                "title": "t",
                "body": "",
                "user": {"login": "trusted-author"},
            }

            def fake_run_agent(agent, *args, **kwargs):
                return {
                    "agent": agent["id"],
                    "summary": f"{agent['id']} ok",
                    "findings": [],
                }

            with mock.patch.object(svc, "run_agent", fake_run_agent):
                with mock.patch.object(
                    svc,
                    "clone_pr_source",
                    lambda repo, pr_number, head_sha, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True),
                ):
                    svc.review_pr(client, config, ROOT / "config" / "reviewer.json", pr, {"reviewed": {}})

            self.assertEqual(client.review_calls, 1)
            self.assertEqual(client.review_events, ["APPROVE"])
            self.assertGreaterEqual(len(client.progress_bodies), 4)
            self.assertIn("Dispatching reviewer agents", client.progress_bodies[0])
            self.assertIn("**진행 상황: 2 / 2 완료**", client.progress_bodies[-1])
            self.assertIn("**Phase:** Complete", client.progress_bodies[-1])
            self.assertIn("APPROVE review posted with 0 inline comments.", client.progress_bodies[-1])


if __name__ == "__main__":
    unittest.main()
