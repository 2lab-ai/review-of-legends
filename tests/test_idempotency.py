"""Review posting idempotency tests (P1).

``review_pr`` must record the reviewed head SHA to ``state`` and persist it to
disk *immediately* after the GitHub review POST succeeds. Failures of the
optional executive summary or other downstream side effects must not unwind
that record.
"""

from __future__ import annotations

import json
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
    def __init__(self, repo: str = "owner/repo") -> None:
        self.repo = repo
        self.create_review_calls = 0
        self.summary_calls = 0
        self.summary_should_fail = False

    def list_files(self, number):  # noqa: D401
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
        self.create_review_calls += 1
        return {"id": 999}

    def upsert_issue_comment(self, number, marker, body):
        self.summary_calls += 1
        if self.summary_should_fail:
            raise svc.ServiceError("summary boom")
        return {"id": 998}


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
        "executive_summary": {"enabled": True, "prompt_path": "../agents/executive-summary.md"},
    }


class IdempotencyTest(unittest.TestCase):
    def _stub_agents(self):
        return mock.patch.object(
            svc,
            "run_agent",
            lambda *args, **kwargs: {
                "agent": "fake",
                "summary": "ok",
                "findings": [],
            },
        )

    def test_state_persisted_immediately_after_review_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "state.json"
            config = _make_config(tmp_path / "work")
            config_path = ROOT / "config" / "reviewer.json"
            client = _FakeClient()
            state = {"reviewed": {}}
            pr = {"number": 1, "head": {"sha": "abc1234567"}, "title": "t", "body": ""}

            with self._stub_agents(), mock.patch.object(
                svc, "run_executive_summary_agent", lambda *a, **kw: "summary"
            ), mock.patch.object(
                svc,
                "clone_pr_source",
                lambda repo, pr_number, head_sha, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True),
            ):
                svc.review_pr(client, config, config_path, pr, state, state_path=state_path)

            self.assertEqual(client.create_review_calls, 1)
            self.assertTrue(state_path.exists(), "state.json must be written immediately")
            on_disk = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("1", on_disk["reviewed"])
            self.assertEqual(on_disk["reviewed"]["1"]["head_sha"], "abc1234567")

    def test_summary_failure_does_not_unwind_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "state.json"
            config = _make_config(tmp_path / "work")
            config_path = ROOT / "config" / "reviewer.json"
            client = _FakeClient()
            client.summary_should_fail = True
            state = {"reviewed": {}}
            pr = {"number": 2, "head": {"sha": "deadbeef00"}, "title": "t", "body": ""}

            with self._stub_agents(), mock.patch.object(
                svc, "run_executive_summary_agent", lambda *a, **kw: "summary"
            ), mock.patch.object(
                svc,
                "clone_pr_source",
                lambda repo, pr_number, head_sha, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True),
            ):
                svc.review_pr(client, config, config_path, pr, state, state_path=state_path)

            on_disk = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("2", on_disk["reviewed"])
            self.assertEqual(on_disk["reviewed"]["2"]["head_sha"], "deadbeef00")


if __name__ == "__main__":
    unittest.main()
