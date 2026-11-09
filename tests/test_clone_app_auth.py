"""Cloning as the GitHub App: with ``github_app`` configured the clone needs no host gh login.

2026-11-06: the host's default gh login was removed on purpose (bot accounts
should not hold a person's GitHub token). Posting kept working because it already
goes out as the App, but every clone failed with "gh auth login". These tests pin
the clone to the App's installation token, handed over in the environment only.
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import service as svc

TOKEN = "ghs_cloneTestToken0123456789"
APP = {"app_id": 1, "installation_id": 2, "private_key_path": "/nonexistent.pem", "bot_login": "rol[bot]"}


class AppGitAuthEnvTest(unittest.TestCase):
    def test_token_goes_to_gh_and_git_through_the_environment(self) -> None:
        env = svc.app_git_auth_env(TOKEN, base={"PATH": "/usr/bin", "HOME": "/home/x"})
        self.assertEqual(env["GH_TOKEN"], TOKEN)
        self.assertEqual(env["PATH"], "/usr/bin")
        count = int(env["GIT_CONFIG_COUNT"])
        pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}
        basic = base64.b64encode(f"x-access-token:{TOKEN}".encode("ascii")).decode("ascii")
        self.assertEqual(pairs["http.https://github.com/.extraheader"], f"AUTHORIZATION: basic {basic}")

    def test_host_git_config_and_prompts_are_kept_out(self) -> None:
        env = svc.app_git_auth_env(TOKEN, base={"GITHUB_TOKEN": "ghs_other", "GH_CONFIG_DIR": "/elsewhere"})
        # Another tenant's global git config (url rewrites, helpers, usernames) must not
        # decide which credential the clone sends; no prompt may hang a review.
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        # gh prefers GH_TOKEN, but an inherited GITHUB_TOKEN / config dir must not compete.
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("GH_CONFIG_DIR", env)

    def test_base_is_not_modified(self) -> None:
        base = {"PATH": "/usr/bin"}
        svc.app_git_auth_env(TOKEN, base=base)
        self.assertEqual(base, {"PATH": "/usr/bin"})


class ClonePrSourceEnvTest(unittest.TestCase):
    def _run(self, env):
        calls: list = []

        def fake_run(args, timeout, cwd=None, env=None):
            calls.append((list(args), env))
            stdout = "abc123456789\n" if "rev-parse" in args else ""
            code = 1 if "cat-file" in args else 0  # base missing → base fetch runs
            return mock.Mock(returncode=code, stdout=stdout, stderr="")

        with mock.patch.object(svc, "run_command", fake_run), tempfile.TemporaryDirectory() as tmp:
            svc.clone_pr_source("o/r", 9, "abc123456789", Path(tmp) / "src", base_sha="b" * 40, env=env)
        return calls

    def test_clone_and_every_fetch_get_the_auth_env(self) -> None:
        auth = svc.app_git_auth_env(TOKEN, base={})
        calls = self._run(auth)
        network = [(args, env) for args, env in calls if "clone" in args or "fetch" in args]
        self.assertEqual(len(network), 3, calls)  # clone, PR head fetch, base fetch
        for args, env in network:
            self.assertIs(env, auth, args)
            self.assertNotIn(TOKEN, " ".join(args), "the token reached argv")

    def test_local_steps_never_see_the_token(self) -> None:
        calls = self._run(svc.app_git_auth_env(TOKEN, base={}))
        for args, env in calls:
            if "clone" in args or "fetch" in args:
                continue
            self.assertNotIn(TOKEN, " ".join(args))
            self.assertNotEqual((env or {}).get("GH_TOKEN"), TOKEN, args)

    def test_without_env_the_host_login_path_is_unchanged(self) -> None:
        calls = self._run(None)
        for args, env in calls:
            if "clone" in args or "fetch" in args:
                self.assertIsNone(env, args)


class ReviewClonesAsTheAppTest(unittest.TestCase):
    def _review(self, tmp: Path, fake_clone, github_app=None, logs=None):
        config = {
            "agents": [{"id": "correctness", "prompt_path": "../agents/correctness.md"}],
            "agent_runner": {"command": ["true"]},
            "min_successful_agents": 0,
            "max_review_comments": 30,
            "work_dir": str(tmp),
            "progress_comment": {"enabled": False},
            "dashboard": {"enabled": False},
        }
        if github_app is not None:
            config["github_app"] = github_app
        pr = {"number": 9, "head": {"sha": "abc123456789"}, "base": {"sha": ""}, "title": "t", "body": "", "user": {"login": "x"}}

        class Client:
            repo = "o/r"

            def bot_login(self):
                return "bot"

            def list_files(self, n):
                return [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0, "patch": "@@ -1 +1,2 @@\n c\n+x\n"}]

        sink = logs if logs is not None else []
        with mock.patch.object(svc, "clone_pr_source", fake_clone), \
             mock.patch.object(svc, "log", lambda message: sink.append(str(message))):
            return svc.review_pr(Client(), config, ROOT / "config" / "reviewer.json", pr, {"reviewed": {}})

    def _capturing_clone(self, seen: dict):
        def fake_clone(repo, pr_number, head_sha, source_dir, **kw):
            seen.update(kw)
            raise svc.ServiceError("stop after clone")  # the clone call is all this test needs

        return fake_clone

    def test_with_github_app_the_clone_carries_the_installation_token(self) -> None:
        seen: dict = {}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(svc, "github_app_token", lambda app, *a, **k: TOKEN):
            with self.assertRaises(svc.ServiceError):
                self._review(Path(tmp), self._capturing_clone(seen), github_app=APP)
        self.assertIn("env", seen, "the clone was not given an environment")
        self.assertEqual(seen["env"]["GH_TOKEN"], TOKEN)

    def test_without_github_app_the_clone_keeps_the_host_login(self) -> None:
        seen: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(svc.ServiceError):
                self._review(Path(tmp), self._capturing_clone(seen))
        self.assertIsNone(seen.get("env"))

    def test_a_failed_mint_is_a_visible_clone_failure(self) -> None:
        def no_mint(app, *a, **k):
            raise svc.ServiceError("GitHub App token could not be minted: TimeoutError")

        called: list = []
        logs: list = []
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(svc, "github_app_token", no_mint):
            with self.assertRaises(svc.ServiceError) as caught:
                self._review(Path(tmp), lambda *a, **k: called.append(1), github_app=APP, logs=logs)
            runs = Path(tmp) / "runs"
            self.assertEqual(sorted(runs.iterdir()) if runs.exists() else [], [], "the run dir stayed on disk")
        self.assertIn("could not be minted", str(caught.exception))
        self.assertEqual(called, [], "cloned without a token")
        self.assertTrue(any("failed clone" in line for line in logs), logs)


if __name__ == "__main__":
    unittest.main()
