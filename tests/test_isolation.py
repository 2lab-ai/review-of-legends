"""Failure-isolation tests.

- A repository whose ``list_open_prs`` raises must not block subsequent
  repositories during the same cycle.
- A PR whose ``review_pr`` raises must not block subsequent PRs in the
  same repository.
- ``--pr`` must require ``--repo`` when more than one repository is configured.
- ``run_agent`` must remove stale response artifacts before invoking the agent
  so that a non-zero exit on rerun does not republish prior-run findings.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import service as svc


def _base_config(repos: list[str], work_dir: Path, state_path: Path) -> dict:
    return {
        "repositories": repos,
        "agents": [{"id": "fake", "enabled": True, "prompt_path": "../agents/correctness.md"}],
        "agent_runner": {"command": ["true"]},
        "min_successful_agents": 0,
        "max_review_comments": 30,
        "max_diff_chars": 180000,
        "work_dir": str(work_dir),
        "state_path": str(state_path),
        "marker": svc.MARKER,
        "summary_marker": svc.SUMMARY_MARKER,
        "review_existing_open_prs_on_first_run": True,
        "executive_summary": {"enabled": False},
    }


class RepositoryIsolationTest(unittest.TestCase):
    def test_failing_repo_does_not_block_next_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "state.json"
            config_path = ROOT / "config" / "reviewer.json"
            config = _base_config(["owner/a", "owner/b"], tmp_path / "work", state_path)

            seen_repos: list[str] = []

            class FakeClient:
                def __init__(self, repo, gh_bin="gh"):  # noqa: D401
                    self.repo = repo

                def list_open_prs(self):
                    seen_repos.append(self.repo)
                    if self.repo == "owner/a":
                        raise svc.ServiceError("simulated failure")
                    return []

                def get_pr(self, number):
                    return {"number": number, "head": {"sha": "x"}}

            with mock.patch.object(svc, "GitHubClient", FakeClient), mock.patch.object(
                svc, "load_config", return_value=config
            ), mock.patch.object(svc, "expand_path", side_effect=lambda v, base=None: Path(str(v))):
                rc = svc.run_once(
                    argparse.Namespace(
                        config=str(config_path),
                        repo=None,
                        pr=None,
                        dry_run=False,
                        force=False,
                    )
                )
            self.assertIn("owner/b", seen_repos, f"second repo must still be polled (saw {seen_repos})")
            self.assertEqual(rc, 0)

    def test_single_pr_requires_repo_when_multi_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "state.json"
            config_path = ROOT / "config" / "reviewer.json"
            config = _base_config(["owner/a", "owner/b"], tmp_path / "work", state_path)

            with mock.patch.object(svc, "load_config", return_value=config), mock.patch.object(
                svc, "expand_path", side_effect=lambda v, base=None: Path(str(v))
            ):
                with self.assertRaises(svc.ServiceError):
                    svc.run_once(
                        argparse.Namespace(
                            config=str(config_path),
                            repo=None,
                            pr=99,
                            dry_run=False,
                            force=False,
                        )
                    )


class PrIsolationTest(unittest.TestCase):
    def test_failing_pr_does_not_block_next_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_path = tmp_path / "state.json"
            config_path = ROOT / "config" / "reviewer.json"
            config = _base_config(["owner/a"], tmp_path / "work", state_path)

            class FakeClient:
                def __init__(self, repo, gh_bin="gh"):
                    self.repo = repo

                def list_open_prs(self):
                    return [
                        {"number": 1, "head": {"sha": "aaa"}},
                        {"number": 2, "head": {"sha": "bbb"}},
                    ]

            saw = []

            def fake_review_pr(client, cfg, cfg_path, pr, state, **kwargs):
                saw.append(pr["number"])
                if pr["number"] == 1:
                    raise RuntimeError("simulated")
                return True

            with mock.patch.object(svc, "GitHubClient", FakeClient), mock.patch.object(
                svc, "load_config", return_value=config
            ), mock.patch.object(svc, "review_pr", fake_review_pr), mock.patch.object(
                svc, "expand_path", side_effect=lambda v, base=None: Path(str(v))
            ):
                svc.run_once(
                    argparse.Namespace(
                        config=str(config_path),
                        repo=None,
                        pr=None,
                        dry_run=False,
                        force=False,
                    )
                )
            self.assertEqual(saw, [1, 2], f"second PR must still be reviewed (saw {saw})")


class StaleResponseTest(unittest.TestCase):
    def test_stale_response_file_is_cleared_before_run(self) -> None:
        """Old ``{agent_id}.response.json`` from a prior run must not be reused
        when the new agent invocation returns non-zero without writing one."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            stale = run_dir / svc.REVIEW_ARTIFACT_DIR / "fake.response.json"
            stale.parent.mkdir()
            stale.write_text('{"summary":"stale","findings":[{"path":"a.py","line":1,"title":"old","body":"x"}]}', encoding="utf-8")

            agent = {"id": "fake", "prompt_path": "../agents/correctness.md"}
            config = {"agent_runner": {"command": ["false"]}}
            config_dir = ROOT / "config"
            pr = {"number": 1, "head": {"sha": "x"}, "title": "t", "body": ""}

            result = svc.run_agent(agent, config, config_dir, run_dir, "owner/repo", pr, [], "")
            self.assertIn("error", result, result)
            # The stale file must have been removed at the start of the run, so
            # findings must be empty rather than being inherited from prior run.
            self.assertEqual(result.get("findings", []), [])


if __name__ == "__main__":
    unittest.main()
