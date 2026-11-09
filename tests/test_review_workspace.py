from __future__ import annotations

import argparse
import json
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import service as svc


class _FakeClient:
    def __init__(self) -> None:
        self.repo = "owner/repo"
        self.review_events: list[str] = []

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
        self.review_events.append(event)
        return {"id": 100}

    def upsert_issue_comment(self, number, marker, body):
        return {"id": 101}


def _make_config(work_dir: Path) -> dict:
    return {
        "agents": [{"id": "correctness", "enabled": True, "prompt_path": "../agents/correctness.md"}],
        "agent_runner": {"command": ["true"]},
        "min_successful_agents": 0,
        "max_review_comments": 30,
        "max_diff_chars": 180000,
        "work_dir": str(work_dir),
        "marker": svc.MARKER,
        "summary_marker": svc.SUMMARY_MARKER,
        "progress_marker": svc.PROGRESS_MARKER,
        "progress_comment": {"enabled": False},
        "approval": {"enabled": True, "trusted_authors": ["trusted-author"]},
        "executive_summary": {"enabled": False},
    }


def _trusted_pr(number: int) -> dict:
    return {
        "number": number,
        "head": {"sha": "abcdef1234567890"},
        "title": "t",
        "body": "",
        "user": {"login": "trusted-author"},
    }


class ReviewWorkspaceTest(unittest.TestCase):
    def test_each_review_uses_unique_clone_and_passes_all_repo_rules_to_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = _make_config(tmp_path / "work")
            client = _FakeClient()
            pr = {"number": 8, "head": {"sha": "abcdef1234567890"}, "title": "t", "body": ""}
            cloned_dirs: list[Path] = []
            seen_agent_runs: list[tuple[Path, str]] = []

            def fake_clone(repo, pr_number, head_sha, run_dir, **kwargs):
                cloned_dirs.append(run_dir)
                (run_dir / "rules" / "nested").mkdir(parents=True)
                (run_dir / "rules" / "a.md").write_text("top-level rule", encoding="utf-8")
                (run_dir / "rules" / "nested" / "b.md").write_text("nested rule", encoding="utf-8")

            def fake_run_agent(
                agent,
                config,
                config_dir,
                run_dir,
                repo,
                pr,
                files,
                diff_text,
                repository_rules="",
                artifact_root=None,
                policy=None,
                prompt_text=None,
            ):
                seen_agent_runs.append((run_dir, repository_rules))
                return {"agent": agent["id"], "summary": "ok", "findings": []}

            with mock.patch.object(svc, "clone_pr_source", fake_clone), mock.patch.object(
                svc, "run_agent", fake_run_agent
            ):
                svc.review_pr(client, config, ROOT / "config" / "reviewer.json", pr, {"reviewed": {}})
                svc.review_pr(
                    client,
                    config,
                    ROOT / "config" / "reviewer.json",
                    pr,
                    {"reviewed": {}},
                    force=True,
                )

            self.assertEqual(len(cloned_dirs), 2)
            self.assertNotEqual(cloned_dirs[0], cloned_dirs[1])
            expected_runs_dir = (tmp_path / "work" / "runs").resolve()
            self.assertTrue(all(path.parent.parent == expected_runs_dir for path in cloned_dirs))
            self.assertTrue(all(path.parent.name.startswith("pr-8-abcdef123456-") for path in cloned_dirs))
            self.assertTrue(all(path.name == "source" for path in cloned_dirs))
            self.assertEqual([run_dir for run_dir, _rules in seen_agent_runs], cloned_dirs)
            self.assertTrue(all(not path.exists() for path in cloned_dirs))
            for _run_dir, rules in seen_agent_runs:
                self.assertIn("## rules/a.md", rules)
                self.assertIn("top-level rule", rules)
                self.assertIn("## rules/nested/b.md", rules)
                self.assertIn("nested rule", rules)

    def test_review_is_approved_when_only_p2_findings_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(Path(tmp) / "work")
            client = _FakeClient()
            pr = _trusted_pr(9)

            def fake_run_agent(*args, **kwargs):
                return {
                    "agent": "correctness",
                    "summary": "p2 only",
                    "findings": [
                        {
                            "path": "a.py",
                            "line": 2,
                            "priority": 2,
                            "title": "[P2] Normal bug",
                            "body": "details",
                        }
                    ],
                }

            with mock.patch.object(
                svc,
                "clone_pr_source",
                lambda repo, pr_number, head_sha, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True),
            ), mock.patch.object(svc, "run_agent", fake_run_agent):
                svc.review_pr(client, config, ROOT / "config" / "reviewer.json", pr, {"reviewed": {}})

            self.assertEqual(client.review_events, ["APPROVE"])

    def test_approval_is_comment_when_approval_config_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(Path(tmp) / "work")
            config["approval"] = {"enabled": False}
            client = _FakeClient()
            pr = _trusted_pr(13)

            with mock.patch.object(
                svc,
                "clone_pr_source",
                lambda repo, pr_number, head_sha, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True),
            ), mock.patch.object(
                svc,
                "run_agent",
                lambda *args, **kwargs: {"agent": "correctness", "summary": "ok", "findings": []},
            ):
                svc.review_pr(client, config, ROOT / "config" / "reviewer.json", pr, {"reviewed": {}})

            self.assertEqual(client.review_events, ["COMMENT"])

    def test_approval_is_comment_for_untrusted_author(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(Path(tmp) / "work")
            client = _FakeClient()
            pr = {
                "number": 15,
                "head": {"sha": "abcdef1234567890"},
                "title": "t",
                "body": "",
                "user": {"login": "drive-by-author"},
            }

            with mock.patch.object(
                svc,
                "clone_pr_source",
                lambda repo, pr_number, head_sha, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True),
            ), mock.patch.object(
                svc,
                "run_agent",
                lambda *args, **kwargs: {"agent": "correctness", "summary": "ok", "findings": []},
            ):
                svc.review_pr(client, config, ROOT / "config" / "reviewer.json", pr, {"reviewed": {}})

            self.assertEqual(client.review_events, ["COMMENT"])

    def test_github_client_bot_login_is_cached(self) -> None:
        with mock.patch.object(svc.GitHubClient, "_load_token", return_value="token"):
            client = svc.GitHubClient("owner/repo")
        with mock.patch.object(client, "request", return_value={"login": "review-bot"}) as request:
            self.assertEqual(client.bot_login(), "review-bot")
            self.assertEqual(client.bot_login(), "review-bot")
        request.assert_called_once_with("GET", "user")

    def test_review_retries_as_comment_when_github_rejects_self_approval(self) -> None:
        class RejectingClient(_FakeClient):
            def bot_login(self):
                return "other-user"

            def create_review(self, number, commit_id, body, comments, event="COMMENT"):
                self.review_events.append(event)
                if event == "APPROVE":
                    raise svc.ServiceError("GitHub API POST failed: 422 Can not approve your own pull request")
                return {"id": 100}

        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(Path(tmp) / "work")
            client = RejectingClient()
            pr = _trusted_pr(12)

            with mock.patch.object(
                svc,
                "clone_pr_source",
                lambda repo, pr_number, head_sha, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True),
            ), mock.patch.object(
                svc,
                "run_agent",
                lambda *args, **kwargs: {"agent": "correctness", "summary": "ok", "findings": []},
            ):
                svc.review_pr(client, config, ROOT / "config" / "reviewer.json", pr, {"reviewed": {}})

            self.assertEqual(client.review_events, ["APPROVE", "COMMENT"])

    def test_review_is_not_approved_when_p1_findings_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(Path(tmp) / "work")
            client = _FakeClient()
            pr = _trusted_pr(10)

            def fake_run_agent(*args, **kwargs):
                return {
                    "agent": "correctness",
                    "summary": "p1",
                    "findings": [
                        {
                            "path": "a.py",
                            "line": 2,
                            "priority": 1,
                            "title": "[P1] Urgent bug",
                            "body": "details",
                        }
                    ],
                }

            with mock.patch.object(
                svc,
                "clone_pr_source",
                lambda repo, pr_number, head_sha, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True),
            ), mock.patch.object(svc, "run_agent", fake_run_agent):
                svc.review_pr(client, config, ROOT / "config" / "reviewer.json", pr, {"reviewed": {}})

            self.assertEqual(client.review_events, ["COMMENT"])

    def test_review_event_comments_when_agents_are_incomplete_or_self_authored(self) -> None:
        # Pass can_approve=True for the incomplete-agent case so the COMMENT
        # result specifically proves the all_agents_succeeded gate fires,
        # rather than being explainable by the (default) can_approve=False.
        self.assertEqual(
            svc.review_event_for_findings([], all_agents_succeeded=False, can_approve=True),
            "COMMENT",
        )
        self.assertEqual(svc.review_event_for_findings([], can_approve=False), "COMMENT")
        self.assertEqual(svc.review_event_for_findings([], all_agents_succeeded=True, can_approve=True), "APPROVE")

    def test_review_event_comments_when_raw_findings_have_p0_p1_off_diff(self) -> None:
        self.assertEqual(
            svc.review_event_for_findings(
                [], all_agents_succeeded=True, can_approve=True, blocked_by_raw_p0_p1=True
            ),
            "COMMENT",
        )

    def test_raw_results_have_p0_p1_detects_high_priority_off_diff(self) -> None:
        # A P1 finding whose ``line`` does not land on an added diff line is
        # dropped during normalization, but must still block APPROVE.
        results = [
            {
                "agent": "correctness",
                "findings": [
                    {"path": "renamed.py", "line": 999, "priority": 1, "title": "x", "body": "y"},
                ],
            }
        ]
        self.assertTrue(svc.raw_results_have_p0_p1(results))
        self.assertFalse(
            svc.raw_results_have_p0_p1(
                [{"agent": "x", "findings": [{"path": "a.py", "line": 1, "priority": 2, "title": "t", "body": "b"}]}]
            )
        )

    def test_review_pr_passes_base_sha_to_trusted_rules_loader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(Path(tmp) / "work")
            client = _FakeClient()
            pr = {
                "number": 14,
                "head": {"sha": "abcdef1234567890"},
                "base": {"sha": "base-trusted-sha"},
                "title": "t",
                "body": "",
            }
            seen_rules: list[str] = []
            load_calls: list[tuple[Path, str]] = []

            def fake_load_rules(source_dir, trusted_ref=""):
                load_calls.append((source_dir, trusted_ref))
                return "trusted base rule"

            def fake_run_agent(*args, **kwargs):
                seen_rules.append(args[8])
                return {"agent": "correctness", "summary": "ok", "findings": []}

            with mock.patch.object(
                svc,
                "clone_pr_source",
                lambda repo, pr_number, head_sha, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True),
            ), mock.patch.object(svc, "load_repository_rules", fake_load_rules), mock.patch.object(
                svc, "run_agent", fake_run_agent
            ):
                svc.review_pr(client, config, ROOT / "config" / "reviewer.json", pr, {"reviewed": {}})

            self.assertEqual(load_calls[0][1], "base-trusted-sha")
            self.assertEqual(seen_rules, ["trusted base rule"])

    def test_repository_rules_use_trusted_tree_and_skip_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True, text=True)
            rules_dir = repo / "rules"
            rules_dir.mkdir()
            (rules_dir / "a.md").write_text("trusted rule", encoding="utf-8")
            try:
                (rules_dir / "link.md").symlink_to("/etc/passwd")
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            subprocess.run(["git", "-C", str(repo), "add", "rules"], check=True, capture_output=True, text=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-m",
                    "base rules",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            trusted_ref = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            (rules_dir / "a.md").write_text("untrusted rule", encoding="utf-8")
            (rules_dir / "new.md").write_text("new untrusted rule", encoding="utf-8")

            loaded = svc.load_repository_rules(repo, trusted_ref=trusted_ref)

            self.assertIn("trusted rule", loaded)
            self.assertNotIn("untrusted rule", loaded)
            self.assertNotIn("rules/link.md", loaded)

    def test_repository_rules_from_git_fail_closed_when_blob_read_fails(self) -> None:
        listed = "100644 blob abc123 12\trules/a.md\0"

        def fake_run_command(cmd, timeout=60, **kwargs):
            if "ls-tree" in cmd:
                return subprocess.CompletedProcess(cmd, 0, listed, "")
            if "show" in cmd:
                return subprocess.CompletedProcess(cmd, 1, "", "missing blob")
            raise AssertionError(f"unexpected command: {cmd}")

        with mock.patch.object(svc, "run_command", fake_run_command):
            with self.assertRaises(svc.ServiceError) as ctx:
                svc.load_repository_rules_from_git(ROOT, "trusted")

        self.assertIn("failed to read trusted rule rules/a.md", str(ctx.exception))

    def test_repository_rules_fallback_rejects_symlinked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            rules_dir = source / "rules"
            rules_dir.mkdir(parents=True)
            (rules_dir / "a.md").write_text("regular rule", encoding="utf-8")
            target = source / "secret.txt"
            target.write_text("secret should not load", encoding="utf-8")
            try:
                (rules_dir / "leak.md").symlink_to(target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            loaded = svc.load_repository_rules(source)

            self.assertIn("regular rule", loaded)
            self.assertNotIn("secret should not load", loaded)
            self.assertNotIn("rules/leak.md", loaded)

    def test_private_text_write_replaces_broken_symlink_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.txt"
            outside_target = Path(tmp) / "missing-target.txt"
            try:
                path.symlink_to(outside_target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            svc.write_private_text(path, "artifact")

            self.assertFalse(path.is_symlink())
            self.assertEqual(path.read_text(encoding="utf-8"), "artifact")
            self.assertFalse(outside_target.exists())

    def test_cleanup_removes_source_but_preserves_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            source_dir = run_dir / "source"
            artifact_dir = run_dir / svc.REVIEW_ARTIFACT_DIR
            source_dir.mkdir(parents=True)
            artifact_dir.mkdir()
            (source_dir / "a.py").write_text("source", encoding="utf-8")
            (artifact_dir / "agent.stdout").write_text("diagnostic", encoding="utf-8")

            svc.cleanup_review_run_dir(run_dir, {})

            self.assertFalse(source_dir.exists())
            self.assertEqual((artifact_dir / "agent.stdout").read_text(encoding="utf-8"), "diagnostic")

    def test_quorum_failure_removes_cloned_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(Path(tmp) / "work")
            config["min_successful_agents"] = 1
            client = _FakeClient()
            pr = _trusted_pr(16)
            cloned_dirs: list[Path] = []

            def fake_clone(repo, pr_number, head_sha, run_dir, **kw):
                cloned_dirs.append(run_dir)
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "a.py").write_text("source", encoding="utf-8")

            with mock.patch.object(svc, "clone_pr_source", fake_clone), mock.patch.object(
                svc,
                "run_agent",
                side_effect=RuntimeError("agent failed"),
            ):
                posted = svc.review_pr(client, config, ROOT / "config" / "reviewer.json", pr, {"reviewed": {}})

            self.assertFalse(posted)
            self.assertEqual(len(cloned_dirs), 1)
            self.assertFalse(cloned_dirs[0].exists())

    def test_run_once_resolves_relative_runtime_paths_under_service_root(self) -> None:
        class EmptyClient:
            def __init__(self, repo, gh_bin="gh"):
                self.repo = repo

            def list_open_prs(self):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "service-root"
            config_dir = root / "config"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "reviewer.json"
            config_path.write_text(
                json.dumps(
                    {
                        "repositories": ["owner/repo"],
                        "state_path": "data/state.json",
                        "work_dir": "data/work",
                        "review_existing_open_prs_on_first_run": False,
                        "agents": [
                            {"id": "a", "enabled": True, "prompt_path": "a.md"},
                            {"id": "b", "enabled": True, "prompt_path": "b.md"},
                            {"id": "c", "enabled": True, "prompt_path": "c.md"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            args = argparse.Namespace(config=str(config_path), repo=None, pr=None, dry_run=False, force=False)
            with mock.patch.object(svc, "GitHubClient", EmptyClient):
                self.assertEqual(svc.run_once(args), 0)

            self.assertTrue((root / "data" / "state.json").is_file())

    def test_run_agent_writes_rules_into_generated_prompt_outside_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_dir = tmp_path / "source"
            artifact_root = tmp_path / "artifacts"
            source_dir.mkdir()
            agent = {"id": "fake", "prompt_path": "../agents/correctness.md"}
            config = {"agent_runner": {"command": ["false"]}}
            pr = {"number": 11, "head": {"sha": "abcdef"}, "title": "t", "body": ""}

            svc.run_agent(
                agent,
                config,
                ROOT / "config",
                source_dir,
                "owner/repo",
                pr,
                [],
                "",
                "## rules/a.md\n\nrepo rule",
                artifact_root=artifact_root,
            )

            prompt = (artifact_root / svc.REVIEW_ARTIFACT_DIR / "fake.prompt.md").read_text(encoding="utf-8")
            self.assertIn("## rules/a.md", prompt)
            self.assertIn("repo rule", prompt)
            self.assertFalse((source_dir / svc.REVIEW_ARTIFACT_DIR).exists())


    def test_review_pr_resolves_relative_work_dir_under_service_root(self) -> None:
        # Regression guard for relative ``work_dir`` resolution: if ``review_pr``
        # ever falls back to interpreting ``data/work`` against the daemon CWD
        # instead of the config's service root, the clone lands somewhere
        # outside the expected tree.
        with tempfile.TemporaryDirectory() as tmp:
            service_root = Path(tmp) / "service-root"
            (service_root / "config").mkdir(parents=True)
            config_path = service_root / "config" / "reviewer.json"
            config_path.write_text(
                json.dumps(
                    {
                        "repositories": ["owner/repo"],
                        "state_path": "data/state.json",
                        "work_dir": "data/work",
                        "min_successful_agents": 0,
                        "max_review_comments": 30,
                        "agents": [{"id": "correctness", "enabled": True, "prompt_path": "../agents/correctness.md"}],
                        "agent_runner": {"command": ["true"]},
                        "progress_comment": {"enabled": False},
                        "approval": {"enabled": False},
                        "executive_summary": {"enabled": False},
                    }
                ),
                encoding="utf-8",
            )

            client = _FakeClient()
            pr = {"number": 42, "head": {"sha": "abcdef1234567890"}, "title": "t", "body": ""}
            cloned_dirs: list[Path] = []

            def fake_clone(repo, pr_number, head_sha, run_dir, **kw):
                cloned_dirs.append(run_dir)
                run_dir.mkdir(parents=True, exist_ok=True)

            with mock.patch.object(svc, "clone_pr_source", fake_clone), mock.patch.object(
                svc,
                "run_agent",
                lambda *args, **kwargs: {"agent": "correctness", "summary": "ok", "findings": []},
            ):
                svc.review_pr(client, json.loads(config_path.read_text()), config_path, pr, {"reviewed": {}})

            self.assertEqual(len(cloned_dirs), 1)
            expected_runs = (service_root / "data" / "work" / "runs").resolve()
            self.assertEqual(cloned_dirs[0].parent.parent, expected_runs)


class ClonePrSourceTest(unittest.TestCase):
    def _stub_run_command(self, responses):
        # Returns a fake ``run_command`` that pops the next stub for each call
        # so we can simulate clone, fetch, checkout, rev-parse, and base SHA
        # checks in sequence without invoking real git.
        calls: list[list[str]] = []

        def fake(cmd, timeout=60, **kwargs):
            calls.append(list(cmd))
            try:
                rc, out, err = responses.pop(0)
            except IndexError as exc:
                raise AssertionError(f"unexpected run_command call: {cmd}") from exc
            return subprocess.CompletedProcess(cmd, rc, out, err)

        return fake, calls

    def test_clone_pr_source_checkout_disables_host_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "clone"
            run_dir.mkdir()
            head = "abcdef0123456789"
            calls: list[tuple[list[str], dict]] = []
            responses = [
                (0, "", ""),           # gh repo clone
                (0, "", ""),           # git fetch head
                (0, "", ""),           # git checkout FETCH_HEAD
                (0, head + "\n", ""),  # rev-parse HEAD matches
            ]

            def fake(cmd, timeout=60, **kwargs):
                calls.append((list(cmd), kwargs))
                rc, out, err = responses.pop(0)
                return subprocess.CompletedProcess(cmd, rc, out, err)

            with mock.patch.object(svc, "run_command", fake):
                svc.clone_pr_source("owner/repo", 1, head_sha=head, run_dir=run_dir)

            checkout_call = calls[2]
            self.assertIn("filter.lfs.process=", checkout_call[0])
            self.assertEqual(checkout_call[1]["env"]["GIT_CONFIG_GLOBAL"], svc.os.devnull)
            self.assertEqual(checkout_call[1]["env"]["GIT_LFS_SKIP_SMUDGE"], "1")

    def test_clone_pr_source_raises_when_checkout_sha_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "clone"
            run_dir.mkdir()
            responses = [
                (0, "", ""),               # gh repo clone
                (0, "", ""),               # git fetch refs/pull/.../head
                (0, "", ""),               # git checkout FETCH_HEAD
                (0, "deadbeef\n", ""),     # git rev-parse HEAD — wrong SHA
            ]
            fake, _calls = self._stub_run_command(responses)
            with mock.patch.object(svc, "run_command", fake):
                with self.assertRaises(svc.ServiceError) as ctx:
                    svc.clone_pr_source(
                        "owner/repo",
                        1,
                        head_sha="cafebabecafebabe",
                        run_dir=run_dir,
                    )
            self.assertIn("unexpected SHA", str(ctx.exception))

    def test_clone_pr_source_raises_when_base_sha_fetch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "clone"
            run_dir.mkdir()
            head = "abcdef0123456789"
            base = "fedcba9876543210"
            responses = [
                (0, "", ""),                  # gh repo clone
                (0, "", ""),                  # git fetch head
                (0, "", ""),                  # git checkout FETCH_HEAD
                (0, head + "\n", ""),         # rev-parse HEAD matches
                (1, "", "not a commit"),      # cat-file -e base — missing
                (1, "", "fetch failed"),      # git fetch origin base — fails
            ]
            fake, _calls = self._stub_run_command(responses)
            with mock.patch.object(svc, "run_command", fake):
                with self.assertRaises(svc.ServiceError) as ctx:
                    svc.clone_pr_source(
                        "owner/repo",
                        1,
                        head_sha=head,
                        run_dir=run_dir,
                        base_sha=base,
                    )
            self.assertIn("base", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
