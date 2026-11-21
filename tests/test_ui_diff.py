"""The visual outline's UI diff (ui_diff.py) and its per-repository switches.

ui-diff itself is replaced by a small script that records what it was run
with: the stage must give ``run`` (which starts the PR's own code) no
credential, pin where ``publish`` uploads, fall back to a text-only section
when the push is refused, and never let a failure reach the review.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import service as svc
from code_review_service import ui_diff

REPO = "acme-corp/web"
HEAD = "b" * 40
BASE = "a" * 40

FAKE_UI_DIFF = textwrap.dedent(
    """
    import json, os, sys, time
    from pathlib import Path
    args = sys.argv[1:]
    command = args[0]
    record = Path(os.environ.get("FAKE_RECORD") or Path(__file__).with_name("record.jsonl"))
    behaviour = json.loads(Path(__file__).with_name("behaviour.json").read_text())
    config_read = json.loads(Path(args[args.index("--config") + 1]).read_text()) if "--config" in args else None
    with record.open("a") as handle:
        handle.write(json.dumps({"args": args, "env": dict(os.environ), "cwd": os.getcwd(), "config": config_read}) + "\\n")
    out = Path(args[args.index("--out") + 1])
    out.mkdir(parents=True, exist_ok=True)
    if command == "run":
        if behaviour.get("spawn_child"):
            import subprocess
            # Like a dev server: detached, working in a worktree under ui-diff's TMPDIR.
            worktree = Path(os.environ["TMPDIR"]) / "ui-diff-head"
            worktree.mkdir(parents=True, exist_ok=True)
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True,
                                     cwd=str(worktree))
            Path(behaviour["spawn_child"]).write_text(str(child.pid))
        if behaviour.get("plant_symlink"):
            (out / "compare").mkdir(parents=True, exist_ok=True)
            os.symlink(behaviour["plant_symlink"], out / "compare" / "secret.png")
        if behaviour.get("replace_config_with"):
            config_file = Path(args[args.index("--config") + 1])
            config_file.unlink()
            config_file.write_text(behaviour["replace_config_with"])
        if behaviour.get("run_sleep"):
            time.sleep(behaviour["run_sleep"])
        if behaviour.get("exit_right_away"):
            sys.exit(0 if (out / "report.json").write_text("{}") else 0)
        if behaviour.get("run_exit"):
            print(json.dumps({"error": "Server never became ready", "hint": "check startCommand"}))
            sys.exit(behaviour["run_exit"])
        (out / "report.json").write_text(json.dumps({"stats": {"changed": 1}}))
        print(json.dumps({"stats": {"changed": 1}}))
        sys.exit(0)
    if command == "publish":
        upload = "--no-upload" not in args
        if upload and behaviour.get("publish_exit"):
            print(json.dumps({"error": "git could not access the remote"}))
            sys.exit(behaviour["publish_exit"])
        link = args[args.index("--link-base") + 1] if upload else ""
        image = f"![after]({link}/pr-7/default/run/compare/home.png)" if upload else "(images not uploaded)"
        (out / "comment.md").write_text(
            "<!-- ui-diff:sticky id=default -->\\n### UI diff — 1 changed, 3 unchanged\\n\\n" + image + "\\n"
        )
        print(json.dumps({"pushed": upload, "posted": False}))
        sys.exit(0)
    sys.exit(9)
    """
)


def _gone(pid: int, wait: float = 5.0) -> bool:
    """True once ``pid`` is dead (a zombie counts: init has not reaped it yet); kills it otherwise."""
    import time as _time
    deadline = _time.monotonic() + wait
    while _time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        state = os.popen(f"ps -o stat= -p {pid}").read().strip()
        if not state or state.startswith("Z"):
            return True
        _time.sleep(0.1)
    try:
        os.kill(pid, 9)
    except OSError:
        pass
    return False


def _config(tmp: Path, **ui_diff_settings) -> dict:
    script = tmp / "fake-ui-diff.py"
    script.write_text(FAKE_UI_DIFF, encoding="utf-8")
    settings = {
        "command": [sys.executable, str(script)],
        "cache_dir": str(tmp / "cache"),
        "configs": {REPO: {"startCommand": "npm run dev -- --port {port}", "routes": ["/"]}},
    }
    settings.update(ui_diff_settings)
    return {
        "repositories": [REPO],
        "visual_outline": {"enabled": True, "ui_diff": settings},
    }


def _pr(head_repo: str = REPO) -> dict:
    return {
        "number": 7,
        "head": {"sha": HEAD, "repo": {"full_name": head_repo}},
        "base": {"sha": BASE, "repo": {"full_name": REPO}},
    }


class RepositorySettingsTest(unittest.TestCase):
    def test_the_dashboard_beats_the_config_repositories_beat_the_default(self) -> None:
        config = {
            "repositories": ["acme-corp/web", "acme-corp/api"],
            "visual_outline": {
                "enabled": True,
                "repositories": {"acme-corp/api": {"enabled": False, "ui_diff": True}},
                "ui_diff": {"command": ["node", "ui-diff.mjs"]},
            },
        }
        web = ui_diff.repository_settings(config, "acme-corp/web")
        self.assertEqual((web["enabled"], web["enabled_source"], web["ui_diff"], web["ui_diff_source"]),
                         (True, "default", False, "default"))
        api = ui_diff.repository_settings(config, "ACME-CORP/API")
        self.assertEqual((api["enabled"], api["enabled_source"], api["ui_diff"], api["ui_diff_effective"]),
                         (False, "config", True, False), "the UI diff runs only with the outline")
        stored = {"repositories": {"acme-corp/Api": {"enabled": True}, "acme-corp/web": {"ui_diff": True, "x": 1}}}
        api = ui_diff.repository_settings(config, "acme-corp/api", stored)
        self.assertEqual((api["enabled"], api["enabled_source"], api["ui_diff_source"], api["ui_diff_effective"]),
                         (True, "dashboard", "config", True))
        web = ui_diff.repository_settings(config, "acme-corp/web", stored)
        self.assertEqual((web["ui_diff"], web["ui_diff_source"], web["ui_diff_effective"]), (True, "dashboard", True))

    def test_without_a_command_the_ui_diff_never_runs(self) -> None:
        config = {"repositories": [REPO], "visual_outline": {"enabled": True}}
        settings = ui_diff.repository_settings(config, REPO, {"repositories": {REPO: {"ui_diff": True}}})
        self.assertEqual((settings["ui_diff"], settings["ui_diff_available"], settings["ui_diff_effective"]),
                         (True, False, False))

    def test_an_absent_block_keeps_the_outline_off(self) -> None:
        settings = ui_diff.repository_settings({}, REPO)
        self.assertEqual((settings["enabled"], settings["ui_diff"]), (False, False))
        self.assertFalse(ui_diff.any_repository_enabled({}))
        self.assertTrue(ui_diff.any_repository_enabled(
            {"visual_outline": {"enabled": False, "repositories": {REPO: {"enabled": True}}}}
        ))

    def test_validation_refuses_malformed_blocks(self) -> None:
        base = {"repositories": [REPO]}
        bad = [
            {"visual_outline": []},
            {"visual_outline": {"repositories": {"acme-corp/other": {"enabled": True}}}},
            {"visual_outline": {"repositories": {REPO: {"enabled": "yes"}}}},
            {"visual_outline": {"repositories": {REPO: {"ui": True}}}},
            {"visual_outline": {"ui_diff": {"command": "node ui-diff.mjs"}}},
            {"visual_outline": {"ui_diff": {"command": []}}},
            {"visual_outline": {"ui_diff": {"timeout_seconds": 0}}},
            {"visual_outline": {"ui_diff": {"max_concurrent": True}}},
            {"visual_outline": {"ui_diff": {"assets_branch": "../main"}}},
            {"visual_outline": {"ui_diff": {"env": {"HOME": "/tmp"}}}},
            {"visual_outline": {"ui_diff": {"env": {"GIT_CONFIG_COUNT": "1"}}}},
            {"visual_outline": {"ui_diff": {"env": {"VITE_MODE": 1}}}},
            {"visual_outline": {"ui_diff": {"configs": {REPO: []}}}},
            {"visual_outline": {"ui_diff": {"upload": "no"}}},
        ]
        for block in bad:
            with self.subTest(block=block), self.assertRaises(ValueError):
                ui_diff.validate({**base, **block})
        ui_diff.validate({**base, "visual_outline": {
            "enabled": True,
            "repositories": {"_comment": "x", REPO.upper(): {"enabled": False, "ui_diff": True}},
            "ui_diff": {"command": ["node", "x.mjs"], "env": {"VITE_MODE": "dev"}, "assets_branch": "ui-diff-assets",
                        "configs": {REPO: {"routes": ["/"]}}, "timeout_seconds": 60, "upload": False},
        }})

    def test_the_stored_setting_is_validated_against_the_review_list(self) -> None:
        value = ui_diff.validate_stored({"repositories": {"ACME-corp/WEB": {"ui_diff": True}}}, [REPO])
        self.assertEqual(value, {"repositories": {REPO: {"ui_diff": True}}})
        for bad in (
            {"repositories": {"acme-corp/other": {"enabled": True}}},
            {"repositories": {REPO: {}}},
            {"repositories": {REPO: {"enabled": 1}}},
            {"repositories": {REPO: {"model": True}}},
            {"repositories": [], "extra": 1},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ui_diff.validate_stored(bad, [REPO])

    def test_load_config_refuses_a_bad_ui_diff_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = json.loads((ROOT / "config" / "reviewer.json").read_text(encoding="utf-8"))
            raw.pop("github_app", None)
            raw["visual_outline"] = {"enabled": True, "ui_diff": {"command": "not-a-list"}}
            path = Path(tmp) / "reviewer.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(svc.ServiceError) as caught:
                svc.load_config(path)
            self.assertIn("visual_outline.ui_diff.command", str(caught.exception))

    def test_the_outline_runner_decides_publication_when_one_repository_has_it(self) -> None:
        config = {
            "agents": [{"id": "a"}],
            "agent_runner": {"command": ["runner", "{model_policy_args}"]},
            "visual_outline": {"enabled": False, "repositories": {REPO: {"enabled": True}},
                               "runner": {"command": ["codex", "exec"]}},
        }
        self.assertIn(["codex", "exec"], svc.runner_commands(config))
        # The review's own answer (the dashboard included) decides when given.
        config["visual_outline"]["repositories"] = {}
        self.assertNotIn(["codex", "exec"], svc.runner_commands(config))
        self.assertIn(["codex", "exec"], svc.runner_commands(config, outline_enabled=True))
        policy = {"model": "m", "source": "config"}
        self.assertFalse(svc.policy_applies_everywhere(config, policy, outline_enabled=True))
        self.assertTrue(svc.policy_applies_everywhere(config, policy, outline_enabled=False))


class SectionTest(unittest.TestCase):
    def test_a_new_section_replaces_the_old_and_keeps_the_outline(self) -> None:
        outline = "<!-- marker -->\n\n## 왜 이 변경인가\n한 문장.\n\n— footer"
        first = ui_diff.build_section("<!-- ui-diff:sticky id=default -->\n### UI diff — 1 changed", [], HEAD, 65)
        self.assertNotIn("ui-diff:sticky", first)
        self.assertIn("PR head `bbbbbbb` · 1m05s", first)
        body = ui_diff.merge_section(outline, first)
        self.assertTrue(body.startswith(outline))
        second = ui_diff.build_section("### UI diff — 0 changed", ["Screenshots were not uploaded: x."], "c" * 40, 3)
        body = ui_diff.merge_section(body, second)
        self.assertEqual(body.count(ui_diff.SECTION_START), 1)
        self.assertIn("0 changed", body)
        self.assertNotIn("1 changed", body)
        self.assertIn("> Screenshots were not uploaded: x.", body)
        self.assertIn("## 왜 이 변경인가", body)
        self.assertEqual(ui_diff.strip_section(body).strip(), outline)

    def test_a_section_that_cannot_fit_with_its_markers_is_left_out(self) -> None:
        body = ui_diff.merge_section("x" * (ui_diff.MAX_COMMENT_CHARS - 500), ui_diff.build_section("y" * 5000, [], HEAD, 1))
        self.assertNotIn(ui_diff.SECTION_END, body)
        self.assertNotIn("review-of-legends:ui-diff", body)

    def test_an_oversized_section_is_cut_to_fit_one_comment(self) -> None:
        section = ui_diff.build_section("x" * 80000, [], HEAD, 1)
        body = ui_diff.merge_section("outline", section)
        self.assertLessEqual(len(body), ui_diff.MAX_COMMENT_CHARS)
        self.assertTrue(body.rstrip().endswith(ui_diff.SECTION_END))
        self.assertIn("truncated", body)

    def test_a_pending_comment_does_not_count_as_an_outline(self) -> None:
        pending = ui_diff.merge_section(ui_diff.pending_outline_body(svc.VISUAL_MARKER), "section")
        client = mock.Mock()
        client.list_issue_comments.return_value = [{"body": pending}]
        self.assertFalse(svc.visual_outline_exists(client, 7, svc.VISUAL_MARKER))
        client.list_issue_comments.return_value = [{"body": svc.VISUAL_MARKER + "\n\n## outline"}]
        self.assertTrue(svc.visual_outline_exists(client, 7, svc.VISUAL_MARKER))


class RunStageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.source = self.tmp / "source"
        self.source.mkdir()
        self.artifacts = self.tmp / "artifacts"
        self.artifacts.mkdir()
        self.logs: list[str] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def behaviour(self, **values) -> None:
        (self.tmp / "behaviour.json").write_text(json.dumps(values), encoding="utf-8")

    def records(self) -> list[dict]:
        path = self.tmp / "record.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def run_stage(self, config: dict, pr: dict | None = None) -> ui_diff.Outcome:
        def publish_env(env: dict) -> dict:
            return {**env, "GH_TOKEN": "token-for-publish"}

        def write(path: Path, text: str) -> None:
            path.write_text(text, encoding="utf-8")

        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "host-token", "AWS_SECRET_ACCESS_KEY": "nope"}):
            return ui_diff.run_stage(
                config, self.tmp, self.source, self.artifacts, REPO, pr or _pr(), publish_env, self.logs.append, write,
            )

    def test_run_gets_no_credential_and_publish_is_pinned_to_the_repository(self) -> None:
        self.behaviour()
        outcome = self.run_stage(_config(self.tmp))
        self.assertEqual(outcome.status, "ok", outcome.detail)
        run, publish = self.records()
        self.assertEqual(run["args"][:2], ["run", "--config"])
        self.assertEqual(run["args"][run["args"].index("--base") + 1], BASE)
        self.assertEqual(run["args"][run["args"].index("--head") + 1], HEAD)
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY"):
            self.assertNotIn(name, run["env"], f"run must not see {name}")
        self.assertEqual(run["env"]["HOME"], str(self.tmp / "cache" / "home"))
        self.assertEqual(run["env"]["PLAYWRIGHT_BROWSERS_PATH"], str(self.tmp / "cache" / "ms-playwright"))
        self.assertEqual(run["env"]["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(publish["env"]["GH_TOKEN"], "token-for-publish")
        args = publish["args"]
        self.assertIn("--no-comment", args)
        self.assertEqual(args[args.index("--remote") + 1], f"https://github.com/{REPO}.git")
        self.assertEqual(args[args.index("--link-base") + 1], f"https://github.com/{REPO}/raw/ui-diff-assets")
        self.assertEqual(args[args.index("--repo") + 1], REPO)
        self.assertEqual(args[args.index("--pr") + 1], "7")
        # The config came from visual_outline.ui_diff.configs, written to the clone root.
        written = json.loads((self.source / "ui-diff.config.json").read_text(encoding="utf-8"))
        self.assertEqual(written["routes"], ["/"])
        self.assertIn(f"https://github.com/{REPO}/raw/ui-diff-assets/", outcome.section)
        self.assertTrue(outcome.section.startswith(ui_diff.SECTION_START))
        self.assertTrue((self.artifacts / "ui-diff.comment.md").exists())
        self.assertTrue((self.artifacts / "ui-diff.report.json").exists())

    def test_the_repositorys_own_config_wins(self) -> None:
        self.behaviour()
        (self.source / "ui-diff.config.json").write_text('{"routes": ["/own"]}', encoding="utf-8")
        outcome = self.run_stage(_config(self.tmp))
        self.assertEqual(outcome.status, "ok", outcome.detail)
        run, publish = self.records()
        self.assertEqual(run["config"], {"routes": ["/own"]}, "run reads the repository's config")
        self.assertEqual(publish["config"], {"routes": ["/own"]}, "publish gets the same choice")

    def test_publish_gets_the_config_as_read_before_the_prs_code_ran(self) -> None:
        self.behaviour(replace_config_with='{"routes": ["/changed"], "publish": {"allowPublicRepo": true}}')
        outcome = self.run_stage(_config(self.tmp))
        self.assertEqual(outcome.status, "ok", outcome.detail)
        _, publish = self.records()
        self.assertEqual(publish["config"]["routes"], ["/"])
        self.assertNotIn("publish", publish["config"])

    def test_a_repository_config_that_is_not_a_regular_file_is_refused(self) -> None:
        self.behaviour()
        os.mkfifo(str(self.source / "ui-diff.config.json"))
        outcome = self.run_stage(_config(self.tmp))
        self.assertEqual(outcome.status, "failed")
        self.assertIn("not a regular file", outcome.section)
        self.assertEqual(self.records(), [])

    def test_a_detached_child_left_behind_by_a_finished_run_is_swept(self) -> None:
        pid_file = self.tmp / "child.pid"
        self.behaviour(run_sleep=ui_diff.POLL_SECONDS * 2, spawn_child=str(pid_file))
        outcome = self.run_stage(_config(self.tmp))
        self.assertEqual(outcome.status, "ok", outcome.detail)
        self.assertTrue(_gone(int(pid_file.read_text())), "the leftover dev server was killed")

    def test_a_child_detached_just_before_a_fast_exit_is_swept(self) -> None:
        pid_file = self.tmp / "child.pid"
        self.behaviour(spawn_child=str(pid_file), exit_right_away=True)
        self.run_stage(_config(self.tmp))
        self.assertTrue(_gone(int(pid_file.read_text())), "found by its working directory, not the tree")

    def test_a_reviewer_working_in_the_clone_is_never_swept(self) -> None:
        import subprocess
        reviewer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
                                    cwd=str(self.source))
        try:
            pid_file = self.tmp / "child.pid"
            self.behaviour(spawn_child=str(pid_file), exit_right_away=True)
            self.run_stage(_config(self.tmp))
            self.assertTrue(_gone(int(pid_file.read_text())))
            self.assertIsNone(reviewer.poll(), "a reviewer process in the clone was killed")
        finally:
            reviewer.kill()
            reviewer.wait()

    def test_a_root_symlink_is_never_copied_for_publishing(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "report.json").write_text("{}", encoding="utf-8")
        os.symlink(str(elsewhere), str(self.source / ".ui-diff"))
        with self.assertRaises(ValueError):
            ui_diff.publish_workspace(self.source / ".ui-diff", {}, {"PATH": os.environ.get("PATH", "")})

    def test_a_worker_that_ignores_sigterm_is_killed(self) -> None:
        script = self.tmp / "stubborn.py"
        script.write_text("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n")
        with mock.patch.object(ui_diff, "TERM_GRACE_SECONDS", 0.5):
            code, _, _ = ui_diff._run_group([sys.executable, str(script)], self.tmp, dict(os.environ), 1)
        self.assertIsNone(code)

    def test_a_queued_stage_gives_up_on_cancel_without_running(self) -> None:
        self.behaviour()
        slots = ui_diff._slots(1)
        held = slots.acquire(timeout=5)
        self.assertTrue(held)
        try:
            def write(path: Path, text: str) -> None:
                path.write_text(text, encoding="utf-8")

            job = ui_diff.Job(lambda event: ui_diff.run_stage(
                _config(self.tmp), self.tmp, self.source, self.artifacts, REPO, _pr(),
                lambda env: env, self.logs.append, write, cancel=event,
            ), HEAD).start()
            self.assertIsNone(job.result(0.3), "it waits for the slot")
            self.assertTrue(job.cancel(10))
            outcome = job.result(0)
            self.assertEqual(outcome.status, "failed")
            self.assertIn("slot", outcome.section)
            self.assertEqual(self.records(), [], "ui-diff never ran")
        finally:
            slots.release()

    def test_a_cancelled_stage_stops_its_processes(self) -> None:
        pid_file = self.tmp / "child.pid"
        self.behaviour(run_sleep=60, spawn_child=str(pid_file))

        def write(path: Path, text: str) -> None:
            path.write_text(text, encoding="utf-8")

        job = ui_diff.Job(lambda event: ui_diff.run_stage(
            _config(self.tmp), self.tmp, self.source, self.artifacts, REPO, _pr(),
            lambda env: env, self.logs.append, write, cancel=event,
        ), HEAD).start()
        for _ in range(100):
            if pid_file.exists():
                break
            import time as _time
            _time.sleep(0.1)
        self.assertIsNone(job.result(0.1))
        self.assertTrue(job.cancel(30))
        outcome = job.result(0)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("could not wait for it any longer", outcome.section)
        self.assertTrue(_gone(int(pid_file.read_text())))

    def test_a_refused_push_falls_back_to_a_text_only_section(self) -> None:
        self.behaviour(publish_exit=3)
        outcome = self.run_stage(_config(self.tmp))
        self.assertEqual(outcome.status, "text-only")
        self.assertIn("Contents: write", outcome.section)
        self.assertIn("(images not uploaded)", outcome.section)
        commands = [record["args"] for record in self.records()]
        self.assertEqual([args[0] for args in commands], ["run", "publish", "publish"])
        self.assertIn("--no-upload", commands[2])
        self.assertNotIn("GH_TOKEN", self.records()[2]["env"], "the text-only render needs no credential")

    def test_upload_off_never_hands_out_the_credential(self) -> None:
        self.behaviour()
        outcome = self.run_stage(_config(self.tmp, upload=False))
        self.assertEqual(outcome.status, "text-only")
        self.assertTrue(all("GH_TOKEN" not in record["env"] for record in self.records()))

    def test_a_failed_run_says_why_and_publishes_nothing(self) -> None:
        self.behaviour(run_exit=1)
        outcome = self.run_stage(_config(self.tmp))
        self.assertEqual(outcome.status, "failed")
        self.assertIn("Server never became ready (check startCommand)", outcome.section)
        self.assertEqual([record["args"][0] for record in self.records()], ["run"])

    def test_a_run_past_its_timeout_is_stopped(self) -> None:
        self.behaviour(run_sleep=30)
        outcome = self.run_stage(_config(self.tmp, timeout_seconds=1))
        self.assertEqual(outcome.status, "failed")
        self.assertIn("timed out after 1 seconds", outcome.section)

    def test_a_fork_is_never_run(self) -> None:
        self.behaviour()
        outcome = self.run_stage(_config(self.tmp), _pr(head_repo="someone/web"))
        self.assertEqual((outcome.status, outcome.section), ("skipped", None))
        self.assertEqual(self.records(), [])

    def test_a_head_whose_fork_was_deleted_is_never_run(self) -> None:
        self.behaviour()
        pr = _pr()
        pr["head"]["repo"] = None
        outcome = self.run_stage(_config(self.tmp), pr)
        self.assertEqual((outcome.status, outcome.section), ("skipped", None))
        self.assertIn("deleted", outcome.detail)
        self.assertEqual(self.records(), [])

    def test_publish_runs_outside_the_clone_with_regular_files_only(self) -> None:
        secret = self.tmp / "app-key.pem"
        secret.write_text("PRIVATE KEY", encoding="utf-8")
        self.behaviour(plant_symlink=str(secret))
        config = _config(self.tmp)
        config["visual_outline"]["ui_diff"]["configs"][REPO]["publish"] = {"allowPublicRepo": True, "remote": "evil"}
        outcome = self.run_stage(config)
        self.assertEqual(outcome.status, "ok", outcome.detail)
        run, publish = self.records()
        self.assertEqual(Path(run["cwd"]).resolve(), self.source.resolve())
        workspace = Path(publish["cwd"])
        self.assertNotEqual(workspace.resolve(), self.source.resolve())
        self.assertFalse(str(workspace.resolve()).startswith(str(self.source.resolve())))
        args = publish["args"]
        self.assertEqual(Path(args[args.index("--out") + 1]).parent.resolve(), workspace.resolve())
        self.assertFalse(workspace.exists(), "the publish workspace is removed afterwards")
        self.assertEqual(publish["env"]["GIT_CONFIG_KEY_0"], "core.hooksPath")

    def test_the_publish_copy_drops_symlinks_and_the_publish_block(self) -> None:
        out = self.source / ".ui-diff"
        (out / "compare").mkdir(parents=True)
        (out / "compare" / "home.png").write_bytes(b"png")
        secret = self.tmp / "app-key.pem"
        secret.write_text("PRIVATE KEY", encoding="utf-8")
        os.symlink(str(secret), str(out / "compare" / "secret.png"))
        os.symlink(str(self.tmp), str(out / "linked-dir"))
        os.mkfifo(str(out / "compare" / "blocking.png"))
        settings = {"routes": ["/"], "publish": {"allowPublicRepo": True}}
        workspace = ui_diff.publish_workspace(out, settings, {"PATH": os.environ.get("PATH", "")})
        try:
            self.assertTrue((workspace / ".git").is_dir())
            self.assertEqual((workspace / ".ui-diff" / "compare" / "home.png").read_bytes(), b"png")
            self.assertFalse((workspace / ".ui-diff" / "compare" / "secret.png").exists())
            self.assertFalse((workspace / ".ui-diff" / "linked-dir").exists())
            self.assertFalse((workspace / ".ui-diff" / "compare" / "blocking.png").exists())
            copied = json.loads((workspace / "ui-diff.config.json").read_text(encoding="utf-8"))
            self.assertEqual(copied, {"routes": ["/"]})
        finally:
            import shutil
            shutil.rmtree(str(workspace), ignore_errors=True)

    def test_a_dry_run_never_hands_out_the_credential(self) -> None:
        self.behaviour()
        calls = []

        def write(path: Path, text: str) -> None:
            path.write_text(text, encoding="utf-8")

        outcome = ui_diff.run_stage(
            _config(self.tmp), self.tmp, self.source, self.artifacts, REPO, _pr(),
            lambda env: calls.append(env) or env, self.logs.append, write, upload=False,
        )
        self.assertEqual(outcome.status, "text-only")
        self.assertEqual(calls, [])
        self.assertIn("Dry run", outcome.section)
        self.assertIn("--no-upload", self.records()[-1]["args"])

    def test_a_timeout_also_stops_the_detached_children(self) -> None:
        pid_file = self.tmp / "child.pid"
        self.behaviour(run_sleep=30, spawn_child=str(pid_file))
        outcome = self.run_stage(_config(self.tmp, timeout_seconds=2))
        self.assertEqual(outcome.status, "failed")
        self.assertTrue(_gone(int(pid_file.read_text())), "the detached child outlived the timeout")

    def test_no_config_anywhere_is_explained(self) -> None:
        self.behaviour()
        config = _config(self.tmp)
        config["visual_outline"]["ui_diff"]["configs"] = {}
        outcome = self.run_stage(config)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("no ui-diff config", outcome.section)
        self.assertEqual(self.records(), [])

    def test_a_job_turns_an_escaping_error_into_a_failure_section(self) -> None:
        def boom(cancel) -> ui_diff.Outcome:
            raise RuntimeError("worker died")

        outcome = ui_diff.Job(boom, HEAD).start().result(5)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("RuntimeError: worker died", outcome.section)


class _Client:
    """GitHub client fake: the bot's marker comment and every write in order."""

    def __init__(self, comments: list[dict] | None = None) -> None:
        self.repo = REPO
        self.comments = comments if comments is not None else []
        self.calls: list[tuple] = []

    def bot_login(self) -> str:
        return "rol[bot]"

    def list_files(self, number):
        return [{"filename": "src/Home.tsx", "status": "modified", "additions": 1, "deletions": 0,
                 "changes": 1, "patch": "@@ -1,1 +1,2 @@\n context\n+added\n"}]

    def list_issue_comments(self, number):
        self.calls.append(("list",))
        return list(self.comments)

    def upsert_issue_comment(self, number, marker, body):
        self.calls.append(("upsert", body))
        self.comments = [c for c in self.comments if marker not in c["body"]]
        self.comments.append({"id": 55, "body": body, "user": {"login": "rol[bot]"}})
        return {"id": 55}

    def update_issue_comment(self, comment_id, body):
        self.calls.append(("update", comment_id, body))
        return {"id": comment_id}

    def create_issue_comment(self, number, body):
        self.calls.append(("create", body))
        return {"id": 66}

    def create_review(self, number, commit_id, body, comments, event="COMMENT"):
        self.calls.append(("review",))
        return {"id": 999}


class ReviewIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.uploads: list[bool] = []
        self.stage_hook = None

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def config(self, outline_enabled: bool = True, ui_diff_on: bool = True) -> dict:
        return {
            "repositories": [REPO],
            "agents": [{"id": "fake", "enabled": True, "prompt_path": "../agents/correctness.md"}],
            "agent_runner": {"command": ["true"]},
            "min_successful_agents": 0,
            "max_review_comments": 30,
            "max_diff_chars": 180000,
            "work_dir": str(self.tmp / "work"),
            "marker": svc.MARKER,
            "summary_marker": svc.SUMMARY_MARKER,
            "visual_marker": svc.VISUAL_MARKER,
            "progress_comment": {"enabled": False},
            "executive_summary": {"enabled": False},
            "visual_outline": {
                "enabled": outline_enabled,
                "repositories": {REPO: {"ui_diff": ui_diff_on}},
                "ui_diff": {"command": ["ui-diff"]},
            },
        }

    def review(self, client: _Client, config: dict, outline: str | None = "## outline", stage=None, dry_run=False):
        stages: list[tuple] = []

        def fake_stage(config, config_dir, source_dir, artifact_dir, repo, pr, publish_env, log, write_text, **kw):
            stages.append((repo, pr["head"]["sha"], source_dir.exists()))
            self.uploads.append(kw.get("upload", True))
            if self.stage_hook is not None:
                self.stage_hook(**kw)
            if stage is not None:
                return stage
            return ui_diff.Outcome(ui_diff.build_section("### UI diff — 1 changed", [], HEAD, 2), "ok")

        def run_agent(*args, **kwargs):
            client.calls.append(("agent",))
            return {"agent": "fake", "summary": "ok", "findings": []}

        with mock.patch.object(svc, "run_agent", run_agent), \
                mock.patch.object(svc, "run_visual_outline_agent", lambda *a, **k: outline), \
                mock.patch.object(svc.ui_diff, "run_stage", fake_stage), \
                mock.patch.object(svc, "load_repository_rules", lambda *a, **k: ""), \
                mock.patch.object(svc, "clone_pr_source",
                                  lambda repo, n, sha, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True)):
            posted = svc.review_pr(client, config, ROOT / "config" / "reviewer.json", _pr(), {}, dry_run=dry_run)
        return posted, stages

    def test_the_section_lands_on_the_outline_comment_after_the_review(self) -> None:
        import threading
        client = _Client()
        released = threading.Event()
        reviewed = threading.Event()
        original = client.create_review

        def create_review(*args, **kwargs):
            reviewed.set()
            return original(*args, **kwargs)

        client.create_review = create_review

        def hold(**kw):
            # The worker stays busy until the review is posted: a review that
            # waited for it would never get there, and the bounded wait fails.
            self.assertTrue(reviewed.wait(10), "the review waited for the UI diff")
            released.set()

        self.stage_hook = hold
        posted, stages = self.review(client, self.config())
        self.assertTrue(released.is_set())
        self.assertTrue(posted)
        self.assertEqual(stages, [(REPO, HEAD, True)], "ui-diff ran on the clone while it existed")
        kinds = [call[0] for call in client.calls]
        self.assertLess(kinds.index("upsert"), kinds.index("agent"), "the outline still leads")
        self.assertLess(kinds.index("review"), kinds.index("update"), "the review is not held for the UI diff")
        update = next(call for call in client.calls if call[0] == "update")
        self.assertEqual(update[1], 55)
        self.assertIn("## outline", update[2])
        self.assertIn("### UI diff — 1 changed", update[2])

    def test_a_new_head_replaces_the_section_of_the_existing_outline(self) -> None:
        old = ui_diff.merge_section(svc.VISUAL_MARKER + "\n\n## outline",
                                    ui_diff.build_section("### UI diff — 9 changed", [], "c" * 40, 1))
        client = _Client([{"id": 41, "body": old, "user": {"login": "rol[bot]"}}])
        self.review(client, self.config())
        self.assertNotIn("upsert", [call[0] for call in client.calls], "the outline is not drafted again")
        update = next(call for call in client.calls if call[0] == "update")
        self.assertEqual(update[1], 41)
        self.assertIn("1 changed", update[2])
        self.assertNotIn("9 changed", update[2])

    def test_someone_elses_marker_comment_is_never_edited(self) -> None:
        old = svc.VISUAL_MARKER + "\n\n## pasted by a person"
        client = _Client([{"id": 41, "body": old, "user": {"login": "someone"}}])
        svc.post_ui_diff_section(client, {"visual_marker": svc.VISUAL_MARKER}, 7, "section")
        self.assertEqual([call[0] for call in client.calls if call[0] in ("update", "create")], ["create"])
        self.assertIn(ui_diff.OUTLINE_PENDING, client.calls[-1][1])

    def test_without_an_outline_the_section_is_posted_as_pending(self) -> None:
        client = _Client()
        self.review(client, self.config(), outline=None)
        create = next(call for call in client.calls if call[0] == "create")
        self.assertIn(ui_diff.OUTLINE_PENDING, create[1])
        self.assertIn(svc.VISUAL_MARKER, create[1])
        self.assertIn("1 changed", create[1])

    def test_a_skipped_stage_posts_nothing(self) -> None:
        client = _Client()
        self.review(client, self.config(), stage=ui_diff.Outcome(None, "skipped", "fork"))
        self.assertEqual([call for call in client.calls if call[0] in ("update", "create")], [])

    def test_switched_off_repositories_never_start_ui_diff(self) -> None:
        for config in (self.config(ui_diff_on=False), self.config(outline_enabled=False)):
            client = _Client()
            _, stages = self.review(client, config)
            self.assertEqual(stages, [])

    def test_the_dashboard_switch_is_read_per_attempt(self) -> None:
        client = _Client()
        stored = {"repositories": {REPO: {"ui_diff": False}}}
        with mock.patch.object(svc.dashboard_execution.DashboardRecorder, "setting",
                               lambda self, key: stored if key == ui_diff.SETTING else None):
            _, stages = self.review(client, self.config())
        self.assertEqual(stages, [])

    def test_a_dry_run_prints_the_section_and_posts_nothing(self) -> None:
        client = _Client()
        with mock.patch("builtins.print") as printed:
            self.review(client, self.config(), dry_run=True)
        self.assertEqual([call for call in client.calls if call[0] in ("update", "create")], [])
        self.assertTrue(any("### UI diff — 1 changed" in str(call) for call in printed.call_args_list))
        self.assertEqual(self.uploads, [False], "a dry run never uploads")

    def test_a_ui_diff_still_running_after_the_wait_is_stopped_before_the_clone_goes(self) -> None:
        import threading
        client = _Client()
        stopped = threading.Event()

        def hold(**kw):
            self.assertTrue(kw["cancel"].wait(10), "the review never stopped the UI diff")
            stopped.set()

        self.stage_hook = hold
        removed: list[bool] = []
        real_cleanup = svc.cleanup_review_run_dir

        def cleanup(run_dir, config):
            removed.append(stopped.is_set())
            return real_cleanup(run_dir, config)

        with mock.patch.object(svc.ui_diff, "join_timeout", lambda config: 0.05), \
                mock.patch.object(svc, "cleanup_review_run_dir", cleanup):
            posted, stages = self.review(client, self.config())
        self.assertTrue(posted)
        self.assertEqual(len(stages), 1)
        self.assertTrue(removed and all(removed), "the clone was removed only after the UI diff stopped")

    def test_a_failed_review_post_still_joins_the_ui_diff_before_removing_the_clone(self) -> None:
        import threading
        client = _Client()
        finished = threading.Event()

        def boom(*args, **kwargs):
            raise svc.ServiceError("GitHub said no")

        client.create_review = boom

        def hold(**kw):
            import time as _time
            _time.sleep(0.3)
            finished.set()

        self.stage_hook = hold
        removed: list[bool] = []
        real_cleanup = svc.cleanup_review_run_dir

        def cleanup(run_dir, config):
            removed.append(finished.is_set())
            return real_cleanup(run_dir, config)

        with mock.patch.object(svc, "cleanup_review_run_dir", cleanup), self.assertRaises(svc.ServiceError):
            self.review(client, self.config())
        self.assertTrue(removed and all(removed), "the clone outlived the running UI diff")
        self.assertIn("update", [call[0] for call in client.calls], "the section still lands")

    def test_a_dashboard_switch_runs_the_outline_without_a_config_block(self) -> None:
        client = _Client()
        config = self.config()
        del config["visual_outline"]
        calls = []
        stored = {"repositories": {REPO: {"enabled": True}}}

        def runner(args, timeout, cwd=None, env=None):
            calls.append(args)
            import subprocess as _subprocess
            for arg in args:
                if str(arg).endswith("visual-outline.response.md"):
                    Path(arg).write_text("## outline")
            return _subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        config["agent_runner"] = {"command": ["runner", "{response_file}"]}
        with mock.patch.object(svc.dashboard_execution.DashboardRecorder, "setting",
                               lambda self, key: stored if key == ui_diff.SETTING else None), \
                mock.patch.object(svc, "run_command", runner), \
                mock.patch.object(svc, "run_agent", lambda *a, **k: {"agent": "fake", "summary": "ok", "findings": []}), \
                mock.patch.object(svc, "load_repository_rules", lambda *a, **k: ""), \
                mock.patch.object(svc, "clone_pr_source",
                                  lambda repo, n, sha, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True)):
            svc.review_pr(client, config, ROOT / "config" / "reviewer.json", _pr(), {})
        self.assertTrue(any(any(str(a).endswith("visual-outline.response.md") for a in args) for args in calls),
                        "the outline runner ran")
        upsert = next(call for call in client.calls if call[0] == "upsert")
        self.assertIn("## outline", upsert[1])


if __name__ == "__main__":
    unittest.main()
