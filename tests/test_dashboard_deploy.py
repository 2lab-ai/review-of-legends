"""Deployment coupling tests for the admin dashboard (W5).

Everything here is static text analysis or runs against mocked seams: no
``launchctl``, no ``pip``, no network, no writes outside a temp dir. The
subprocess seam is ``install_dashboard.run``; the HTTP seam is
``install_dashboard.http_probe``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCRIPT = ROOT / "scripts" / "install-dashboard.py"
CI_SCRIPT = ROOT / "scripts" / "dashboard-ci.py"
SERVICE_SH = ROOT / "service.sh"


def load_installer():
    spec = importlib.util.spec_from_file_location("install_dashboard", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_ci():
    spec = importlib.util.spec_from_file_location("dashboard_ci", CI_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = load_installer()
ci = load_ci()


class FakeRun:
    """Records commands instead of executing them.

    ``python -m venv <dir>`` creates the directory tree so the installer's
    post-conditions (marker file, interpreter path) can be asserted without a
    real venv build.
    """

    def __init__(self, version_output: str = "3.12"):
        self.calls = []
        self.version_output = version_output
        # What ``launchctl print`` reports as the loaded job's program; None: no program line.
        self.loaded_program = None

    def __call__(self, cmd, check=True, capture=False):
        cmd = [str(part) for part in cmd]
        self.calls.append(cmd)
        stdout = ""
        if "-m" in cmd and "venv" in cmd:
            target = Path(cmd[-1])
            (target / "bin").mkdir(parents=True, exist_ok=True)
            (target / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
        elif len(cmd) >= 3 and cmd[1] == "-c":
            stdout = self.version_output + "\n"
        elif cmd[:2] == ["launchctl", "print"] and self.loaded_program:
            stdout = "\tstate = running\n\tprogram = %s\n" % self.loaded_program
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    def commands_containing(self, needle: str):
        """Commands with ``needle`` as a whole argument (``-m pip``, ``-m venv``).

        Not a substring of one: a temp dir such as ``tmpipxs_4hd`` contains
        "pip", and every venv path contains "venv".
        """
        return [cmd for cmd in self.calls if needle in cmd]


def make_tree(root: Path, version: str = "deploy-main-20260911055934-2252e94") -> Path:
    (root / "code_review_service").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    (root / "requirements-dashboard.txt").write_text("starlette==1.6.0\n", encoding="utf-8")
    return root


class ServiceShContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SERVICE_SH.read_text(encoding="utf-8")

    def test_syntax(self) -> None:
        subprocess.run(["bash", "-n", str(SERVICE_SH)], check=True)

    def test_daemon_still_runs_stock_python3(self) -> None:
        self.assertIn("<string>/usr/bin/python3</string>", self.text)
        self.assertIn("ai.2lab.review-of-legends", self.text)

    def test_install_also_installs_dashboard(self) -> None:
        install = self.text.split("cmd_install()", 1)[1].split("cmd_uninstall()", 1)[0]
        self.assertIn("dashboard_cmd install", install)

    def test_stop_and_uninstall_account_for_dashboard(self) -> None:
        stop = self.text.split("cmd_stop()", 1)[1].split("cmd_restart()", 1)[0]
        self.assertIn("dashboard_cmd stop", stop)
        uninstall = self.text.split("cmd_uninstall()", 1)[1].split("cmd_start()", 1)[0]
        self.assertIn("dashboard_cmd uninstall", uninstall)

    def test_uninstall_never_deletes_data(self) -> None:
        uninstall = self.text.split("cmd_uninstall()", 1)[1].split("cmd_start()", 1)[0]
        self.assertNotIn("rm -rf", uninstall)
        self.assertNotIn("$DATA_DIR", uninstall.replace("preserved", ""))

    def test_dashboard_passthrough_command(self) -> None:
        self.assertIn("dashboard) dashboard_cmd \"$@\" ;;", self.text)

    def test_install_forwards_operator_flags(self) -> None:
        self.assertIn("install) cmd_install \"$@\" ;;", self.text)
        self.assertIn("dashboard_cmd install \"$@\"", self.text)

    def test_start_repairs_dashboard_even_when_daemon_already_running(self) -> None:
        # Codex P2 on PR #12: the early "already running" return skipped the
        # dashboard, so `start` reported success with the web job down.
        start = self.text.split("cmd_start()", 1)[1].split("cmd_stop()", 1)[0]
        already = start.split("already running", 1)[1].split("return 0", 1)[0]
        self.assertIn("start_dashboard", already)
        helper = self.text.split("start_dashboard()", 1)[1].split("}", 1)[0]
        self.assertIn("dashboard_cmd start", helper)


# ------------------------------------------------------------- dashboard-ci.py


class FakeInstaller:
    """The two installer calls plan makes; select_python() reports on stdout as the real one does."""

    INTERPRETER = Path("/opt/homebrew/bin/python3.12")
    REPORT = "[dashboard] interpreter: /opt/homebrew/bin/python3.12 (3.12)"

    def __init__(self, fail_in=None):
        self.fail_in = fail_in
        self.requirements = []

    def select_python(self):
        print(self.REPORT)
        if self.fail_in == "select_python":
            raise SystemExit(1)
        return self.INTERPRETER

    def pip_install_args(self, requirements):
        self.requirements.append(requirements)
        if self.fail_in == "pip_install_args":
            raise SystemExit(1)
        return ["install", "--no-input", "-r", str(requirements)]


class DashboardCiScriptTest(unittest.TestCase):
    """scripts/dashboard-ci.py: what plan prints and how run-tests judges a run."""

    PLAN = ["/opt/homebrew/bin/python3.12", "install", "--no-input", "-r", "requirements-dashboard.txt"]

    def capture(self, func, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            value = func(*args)
        return value, stdout.getvalue(), stderr.getvalue()

    def test_plan_lines_are_the_interpreter_then_the_pip_arguments(self) -> None:
        installer = FakeInstaller()
        lines, stdout, stderr = self.capture(ci.plan_lines, installer, Path("requirements-dashboard.txt"))
        self.assertEqual(self.PLAN, lines)
        self.assertEqual([Path("requirements-dashboard.txt")], installer.requirements)
        # The installer's report would otherwise be read as the interpreter.
        self.assertEqual("", stdout)
        self.assertEqual(FakeInstaller.REPORT + "\n", stderr)

    def test_plan_prints_the_interpreter_then_one_pip_argument_per_line_on_stdout_only(self) -> None:
        installer = FakeInstaller()
        with mock.patch.object(ci, "load_installer", return_value=installer):
            code, stdout, stderr = self.capture(ci.main, ["plan"])
        self.assertEqual(0, code)
        # The step's read loop takes line 1 as the interpreter and every later
        # line as a pip argument, in this order.
        self.assertEqual("".join(line + "\n" for line in self.PLAN), stdout)
        self.assertEqual(FakeInstaller.REPORT + "\n", stderr)
        self.assertEqual(
            [Path("requirements-dashboard.txt")],
            installer.requirements,
            "relative to the repository root the step runs in",
        )

    def test_a_failing_plan_prints_nothing_on_stdout(self) -> None:
        # Half an answer (the interpreter without its pip arguments) must not
        # reach the read loop: plan prints only once both calls returned.
        for where in ("select_python", "pip_install_args"):
            with self.subTest(where):
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(ci, "load_installer", return_value=FakeInstaller(fail_in=where)):
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        with self.assertRaises(SystemExit) as raised:
                            ci.main(["plan"])
                self.assertEqual(1, raised.exception.code)
                self.assertEqual("", stdout.getvalue())

    def test_plan_loads_the_installer_beside_it(self) -> None:
        installer = ci.load_installer()
        self.assertEqual("install_dashboard", installer.__name__)
        self.assertEqual(SCRIPT.resolve(), Path(installer.__file__).resolve())
        for name in ("select_python", "pip_install_args"):
            self.assertTrue(callable(getattr(installer, name, None)), name)

    @staticmethod
    def result(run=0, failed=0, skipped=0, expected_failures=0) -> unittest.TestResult:
        """A real TestResult filled in directly, without running anything."""
        result = unittest.TestResult()
        result.testsRun = run
        test = unittest.FunctionTestCase(lambda: None)
        try:
            raise AssertionError("boom")
        except AssertionError:
            err = sys.exc_info()
        for _ in range(failed):
            result.addFailure(test, err)
        for _ in range(skipped):
            result.addSkip(test, "no")
        for _ in range(expected_failures):
            result.addExpectedFailure(test, err)
        return result

    def test_judge(self) -> None:
        failed = "dashboard venv test run failed"
        cases = {
            "clean": (self.result(run=3), None),
            "expected failure only": (self.result(run=2, expected_failures=1), None),
            "failing": (self.result(run=3, failed=1), failed),
            # A failure is reported as one, not as the skip beside it.
            "failing and skipped": (self.result(run=3, failed=1, skipped=1), failed),
            "empty": (self.result(run=0), "the dashboard venv test run found no tests"),
            "skipped": (
                self.result(run=3, skipped=2),
                "2 tests skipped in the dashboard venv test run; every test must run here",
            ),
            # An expected failure counts as a success; the skip beside it still fails the run.
            "skipped beside an expected failure": (
                self.result(run=3, skipped=1, expected_failures=1),
                "1 tests skipped in the dashboard venv test run; every test must run here",
            ),
        }
        for label, (result, message) in cases.items():
            with self.subTest(label):
                self.assertEqual(message, ci.judge(result))

    def run_tests_with(self, result):
        calls = []

        def fake_main(*args, **kwargs):
            calls.append((args, kwargs, sys.path[0]))
            return SimpleNamespace(result=result)

        # A sys.path[0] that is not the working directory, as running the
        # script by path leaves it (scripts/). The fake main never discovers
        # anything: a real one here would run this suite inside itself.
        with mock.patch.object(sys, "path", ["/nowhere/scripts"] + sys.path[1:]):
            with mock.patch.object(ci.unittest, "main", side_effect=fake_main):
                code, stdout, _ = self.capture(ci.main, ["run-tests"])
        return code, stdout, calls

    def test_run_tests_discovers_tests_from_the_working_directory(self) -> None:
        code, stdout, calls = self.run_tests_with(self.result(run=1))
        self.assertEqual(0, code, stdout)
        self.assertEqual("", stdout)
        self.assertEqual(
            [((), {"module": None, "argv": ["unittest", "discover", "-s", "tests"], "exit": False}, os.getcwd())],
            calls,
        )

    def test_run_tests_fails_with_the_judgement(self) -> None:
        cases = {
            "failing": (self.result(run=3, failed=1), "::error::dashboard venv test run failed\n"),
            "empty": (self.result(run=0), "::error::the dashboard venv test run found no tests\n"),
            "skipped": (
                self.result(run=3, skipped=1),
                "::error::1 tests skipped in the dashboard venv test run; every test must run here\n",
            ),
        }
        for label, (result, error) in cases.items():
            with self.subTest(label):
                code, stdout, _ = self.run_tests_with(result)
                self.assertEqual(1, code)
                self.assertEqual(error, stdout)


# ------------------------------------------------------------ interpreter pick


class InterpreterSelectionTest(unittest.TestCase):
    def test_rejects_stock_python39_and_requires_310(self) -> None:
        self.assertEqual((3, 10), mod.MIN_PYTHON)
        self.assertNotIn("/usr/bin", mod.PYTHON_SEARCH_DIRS)

    def _select(self, available):
        def fake_version(path):
            return available.get(str(path))

        with mock.patch.object(mod, "python_candidates", return_value=[Path(p) for p in available]):
            with mock.patch.object(mod, "interpreter_version", side_effect=fake_version):
                return mod.select_python()

    def test_prefers_newest_verified_minor_over_bleeding_edge(self) -> None:
        chosen = self._select(
            {
                "/opt/homebrew/bin/python3.14": (3, 14),
                "/opt/homebrew/bin/python3.13": (3, 13),
                "/opt/homebrew/bin/python3.11": (3, 11),
            }
        )
        self.assertEqual(Path("/opt/homebrew/bin/python3.13"), chosen)

    def test_accepts_newer_than_verified_when_it_is_all_there_is(self) -> None:
        chosen = self._select({"/opt/homebrew/bin/python3.14": (3, 14)})
        self.assertEqual(Path("/opt/homebrew/bin/python3.14"), chosen)

    def test_ignores_too_old_interpreters(self) -> None:
        chosen = self._select(
            {"/usr/local/bin/python3": (3, 9), "/opt/homebrew/bin/python3.12": (3, 12)}
        )
        self.assertEqual(Path("/opt/homebrew/bin/python3.12"), chosen)

    def test_fails_loudly_when_no_supported_interpreter(self) -> None:
        with self.assertRaises(SystemExit):
            self._select({"/usr/local/bin/python3": (3, 9)})

    def test_explicit_python_must_satisfy_minimum(self) -> None:
        with mock.patch.object(mod, "interpreter_version", return_value=(3, 9)):
            with self.assertRaises(SystemExit):
                mod.select_python("/usr/bin/python3")


# -------------------------------------------------------------------- the venv


class FakeRunTest(unittest.TestCase):
    def test_commands_are_found_by_a_whole_argument_not_a_path_substring(self) -> None:
        """A temp dir such as ``tmpipxs_4hd`` contains "pip": the venv command used to pass for the pip one."""
        runner = FakeRun()
        with tempfile.TemporaryDirectory() as tmp:
            venv = Path(tmp) / "tmpipxs_4hd" / "venvs" / "v1"
            create = ["/opt/homebrew/bin/python3.12", "-m", "venv", str(venv)]
            install = [str(venv / "bin" / "python"), "-m", "pip", "install", "-r", "requirements-dashboard.txt"]
            runner(create)
            runner(install)
        self.assertEqual(runner.commands_containing("pip"), [install])
        self.assertEqual(runner.commands_containing("venv"), [create], "the venvs/ directory is not the venv module")


class VenvTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_tree(Path(self.tmp.name) / "tree")
        self.venvs = Path(self.tmp.name) / "venvs"
        self.runner = FakeRun()
        patcher = mock.patch.object(mod, "run", self.runner)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_venv_is_created_outside_the_deployed_tree(self) -> None:
        venv = mod.ensure_venv(self.root, "v1", self.venvs, Path("/opt/homebrew/bin/python3.12"))
        self.assertEqual(self.venvs / "v1", venv)
        self.assertNotIn(str(self.root), str(venv))
        self.assertEqual(0o700, self.venvs.stat().st_mode & 0o777)
        create = self.runner.commands_containing("venv")[0]
        self.assertEqual(["/opt/homebrew/bin/python3.12", "-m", "venv", str(venv)], create)

    def test_dependencies_installed_from_pinned_requirements(self) -> None:
        venv = mod.ensure_venv(self.root, "v1", self.venvs, Path("/opt/homebrew/bin/python3.12"))
        pip = self.runner.commands_containing("pip")[0]
        self.assertEqual(str(mod.venv_python(venv)), pip[0])
        self.assertIn("-r", pip)
        self.assertIn(str(self.root / "requirements-dashboard.txt"), pip)
        self.assertNotIn("--require-hashes", pip)

    def test_hashed_requirements_enable_require_hashes(self) -> None:
        (self.root / "requirements-dashboard.txt").write_text(
            "starlette==1.6.0 --hash=sha256:abc\n", encoding="utf-8"
        )
        mod.ensure_venv(self.root, "v1", self.venvs, Path("/opt/homebrew/bin/python3.12"))
        pip = self.runner.commands_containing("pip")[0]
        self.assertIn("--require-hashes", pip)

    def test_pip_install_args_for_unhashed_requirements(self) -> None:
        requirements = self.root / "requirements-dashboard.txt"
        self.assertEqual(
            [
                "install",
                "--no-input",
                "--disable-pip-version-check",
                "--upgrade-strategy",
                "only-if-needed",
                "-r",
                str(requirements),
            ],
            mod.pip_install_args(requirements),
        )

    def test_pip_install_args_for_hashed_requirements(self) -> None:
        requirements = self.root / "requirements-dashboard.txt"
        requirements.write_text("starlette==1.6.0 --hash=sha256:abc\n", encoding="utf-8")
        self.assertEqual(
            [
                "install",
                "--require-hashes",
                "--no-input",
                "--disable-pip-version-check",
                "--upgrade-strategy",
                "only-if-needed",
                "-r",
                str(requirements),
            ],
            mod.pip_install_args(requirements),
        )

    def test_ensure_venv_takes_its_pip_arguments_from_pip_install_args(self) -> None:
        # deploy.yml's web tier step installs with pip_install_args() too, so
        # this delegation is what makes CI install what the deploy installs.
        # Stand-in arguments, not an equality with the real list: the same
        # flags spelled out again inside ensure_venv() must fail here.
        sentinel = ["install", "SENTINEL-FLAG", "-r", "SENTINEL-PATH"]
        with mock.patch.object(mod, "pip_install_args", return_value=sentinel) as pip_install_args:
            venv = mod.ensure_venv(self.root, "v1", self.venvs, Path("/opt/homebrew/bin/python3.12"))
        pip_install_args.assert_called_once_with(self.root / "requirements-dashboard.txt")
        self.assertEqual(
            [[str(mod.venv_python(venv)), "-m", "pip", "install", "SENTINEL-FLAG", "-r", "SENTINEL-PATH"]],
            self.runner.commands_containing("pip"),
        )

    def test_missing_requirements_fails_instead_of_skipping(self) -> None:
        (self.root / "requirements-dashboard.txt").unlink()
        with self.assertRaises(SystemExit):
            mod.ensure_venv(self.root, "v1", self.venvs, Path("/opt/homebrew/bin/python3.12"))

    def test_completed_venv_is_reused_and_partial_one_rebuilt(self) -> None:
        first = mod.ensure_venv(self.root, "v1", self.venvs, Path("/opt/homebrew/bin/python3.12"))
        self.assertTrue((first / mod.VENV_MARKER).is_file())
        before = len(self.runner.calls)
        mod.ensure_venv(self.root, "v1", self.venvs, Path("/opt/homebrew/bin/python3.12"))
        self.assertEqual(before, len(self.runner.calls), "completed venv must not be rebuilt")

        (first / mod.VENV_MARKER).unlink()
        mod.ensure_venv(self.root, "v1", self.venvs, Path("/opt/homebrew/bin/python3.12"))
        self.assertGreater(len(self.runner.calls), before)
        self.assertTrue((first / mod.VENV_MARKER).is_file())

    def test_prune_venvs_keeps_the_given_venvs_and_the_newest_previous_ones(self) -> None:
        # Venvs no longer accumulate forever, but a rollback runtime is
        # still kept: the ones named in ``keep`` whatever their age, plus the
        # newest ``keep_previous`` others. Only directories holding a pyvenv.cfg go.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            names = ["v%d" % index for index in range(1, 7)]  # v1 oldest ... v6 newest
            for age, name in enumerate(reversed(names), start=1):
                (base / name).mkdir()
                (base / name / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
                stamp = time.time() - age * 86400
                os.utime(str(base / name), (stamp, stamp))
            removed = mod.prune_venvs(base, keep=[base / "v1", None], keep_previous=2)
            self.assertEqual(sorted(removed), ["v2", "v3", "v4"])
            self.assertEqual(sorted(path.name for path in base.iterdir()), ["v1", "v5", "v6"])
            self.assertEqual(mod.prune_venvs(base, keep=[], keep_previous=5), [], "fewer than keep_previous: none go")
            # A directory whose pyvenv.cfg is a symlink is not a venv this installer made.
            fake = base / "fake"
            fake.mkdir()
            (fake / "pyvenv.cfg").symlink_to(base / "v1" / "pyvenv.cfg")
            os.utime(str(fake), (1, 1))
            self.assertEqual(sorted(mod.prune_venvs(base, keep=[], keep_previous=0)), ["v1", "v5", "v6"])
            self.assertTrue(fake.is_dir())

    def test_version_directory_name_is_validated(self) -> None:
        (self.root / "VERSION").write_text("../../escape\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            mod.read_version(self.root)


# ----------------------------------------------------------------- settings io


class SettingsMergeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "secrets" / "dashboard-secrets.json"
        self.managed = {
            "public_url": mod.CANONICAL_PUBLIC_URL,
            "config_path": "/opt/review-of-legends/main/config/reviewer.json",
        }

    def test_creates_defaults_without_inventing_provider_credentials(self) -> None:
        settings, missing = mod.merge_settings(self.path, self.managed)
        self.assertEqual(mod.CANONICAL_PUBLIC_URL, settings["public_url"])
        self.assertEqual(
            sorted(list(mod.MANAGED_SETTING_KEYS) + list(mod.GENERATED_SETTING_KEYS)),
            sorted(settings),
        )
        self.assertEqual(["google_client_id", "google_client_secret"], list(mod.AUTH_SETTING_KEYS))
        self.assertEqual(list(mod.AUTH_SETTING_KEYS), missing)
        for key in mod.AUTH_SETTING_KEYS:
            self.assertNotIn(key, settings, "Google credentials are the operator's to provision")
        self.assertNotIn("llmux_api_key", settings)

    def test_session_secret_is_generated_once_and_never_rotated(self) -> None:
        first, _ = mod.merge_settings(self.path, self.managed)
        generated = first["session_secret"]
        self.assertGreaterEqual(len(generated), 32)
        second, _ = mod.merge_settings(self.path, self.managed)
        self.assertEqual(generated, second["session_secret"], "rotating would drop live sessions")
        self.assertEqual(generated, json.loads(self.path.read_text(encoding="utf-8"))["session_secret"])

    def test_generated_session_secret_is_not_printed(self) -> None:
        with mock.patch("builtins.print") as printer:
            settings, _ = mod.merge_settings(self.path, self.managed)
        rendered = " ".join(str(call) for call in printer.call_args_list)
        self.assertNotIn(settings["session_secret"], rendered)
        self.assertIn("session_secret", rendered)

    def test_permissions_are_private(self) -> None:
        mod.merge_settings(self.path, self.managed)
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        self.assertEqual(0o700, self.path.parent.stat().st_mode & 0o777)

    def test_existing_credentials_are_preserved(self) -> None:
        self.path.parent.mkdir(parents=True)
        original = {
            "google_client_id": "cid",
            "google_client_secret": "shhh",
            "session_secret": "s" * 40,
            "llmux_api_key": "key",
            "llmux_base_url": "http://127.0.0.1:3456",
            "config_path": "/stale/reviewer.json",
            "store_path": "/operator/chosen/dashboard.sqlite3",
        }
        self.path.write_text(json.dumps(original), encoding="utf-8")
        settings, missing = mod.merge_settings(self.path, self.managed)
        self.assertEqual([], missing)
        self.assertEqual(self.managed["config_path"], settings["config_path"], "deploy paths refresh")
        for key in (
            "google_client_id",
            "google_client_secret",
            "session_secret",
            "llmux_api_key",
            "llmux_base_url",
            "store_path",
        ):
            self.assertEqual(original[key], settings[key])

    def test_public_url_is_refreshed_to_canonical(self) -> None:
        # dashboard_web.validate_settings rejects any other origin, so a stale
        # value would only crash-loop the job.
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"public_url": "https://rol.other.internal"}), encoding="utf-8")
        settings, _ = mod.merge_settings(self.path, self.managed)
        self.assertEqual(mod.CANONICAL_PUBLIC_URL, settings["public_url"])

    def test_store_path_is_left_to_the_web_tier_derivation(self) -> None:
        self.assertNotIn("store_path", mod.MANAGED_SETTING_KEYS)
        settings, _ = mod.merge_settings(self.path, self.managed)
        self.assertNotIn("store_path", settings)

    def test_provider_secret_values_are_never_printed(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"google_client_secret": "TOPSECRET"}), encoding="utf-8")
        with mock.patch("builtins.print") as printer:
            mod.merge_settings(self.path, self.managed)
        rendered = " ".join(str(call) for call in printer.call_args_list)
        self.assertNotIn("TOPSECRET", rendered)
        self.assertIn("google_client_secret", rendered, "key names are safe and useful")

    def test_unparsable_file_is_not_overwritten(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(SystemExit):
            mod.merge_settings(self.path, self.managed)
        self.assertEqual("{not json", self.path.read_text(encoding="utf-8"))

    def test_write_is_atomic_and_leaves_no_temp_files(self) -> None:
        mod.merge_settings(self.path, self.managed)
        leftovers = [child.name for child in self.path.parent.iterdir() if child.name != self.path.name]
        self.assertEqual([], leftovers)

    def test_settings_live_outside_the_rsynced_tree(self) -> None:
        args = mod.build_parser().parse_args(["install"])
        with mock.patch.dict(os.environ, {"HOME": "/Users/operator"}, clear=False):
            resolved = mod.settings_path(args)
            self.assertEqual(Path("/Users/operator/review-of-legends/dashboard-secrets.json"), resolved)
            self.assertEqual(Path("/Users/operator/review-of-legends/venvs"), mod.venv_root(args))


# ---------------------------------------------------------------------- plist


class PlistTest(unittest.TestCase):
    def test_plist_runs_venv_python_against_external_settings(self) -> None:
        spec = mod.build_plist(
            Path("/opt/review-of-legends/main"),
            Path("/Users/operator/review-of-legends/venvs/v1"),
            Path("/Users/operator/review-of-legends/dashboard-secrets.json"),
        )
        self.assertEqual(mod.LABEL, spec["Label"])
        self.assertNotEqual(mod.DAEMON_LABEL, mod.LABEL)
        self.assertEqual(
            [
                "/Users/operator/review-of-legends/venvs/v1/bin/python",
                "-m",
                "code_review_service.dashboard_web",
                "--settings",
                "/Users/operator/review-of-legends/dashboard-secrets.json",
            ],
            spec["ProgramArguments"],
        )
        self.assertEqual("/opt/review-of-legends/main", spec["WorkingDirectory"])
        self.assertEqual("/opt/review-of-legends/main", spec["EnvironmentVariables"]["PYTHONPATH"])
        self.assertTrue(spec["KeepAlive"])
        self.assertTrue(spec["RunAtLoad"])

    def test_plistlib_escapes_hostile_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            weird = Path(tmp) / "a&b<c>'d"
            spec = mod.build_plist(weird, weird / "venv", weird / "settings.json")
            destination = Path(tmp) / "job.plist"
            mod.write_plist(spec, destination)
            with destination.open("rb") as handle:
                parsed = plistlib.load(handle)
            self.assertEqual(spec, parsed)
            self.assertEqual(0o600, destination.stat().st_mode & 0o777)
            self.assertIn("&amp;", destination.read_text(encoding="utf-8"))

    def test_bind_address_is_fixed_loopback(self) -> None:
        self.assertEqual("127.0.0.1", mod.BIND_HOST)
        self.assertEqual(8840, mod.BIND_PORT)
        self.assertEqual("http://127.0.0.1:8840", mod.BASE_URL)


# ------------------------------------------------------------------- preflight


class ExecutorPathContractTest(unittest.TestCase):
    """Lock/pause/store paths come from the live tree, not from copied strings."""

    def test_paths_match_dashboard_execution_for_this_worktree(self) -> None:
        from code_review_service import dashboard_execution as de
        from code_review_service import service as svc
        from tests.shipped_config import app_key_stand_in

        config_path = ROOT / "config" / "reviewer.json"
        # executor_paths loads the same config itself, so the stand-in covers both.
        with app_key_stand_in():
            config = svc.load_config(config_path)
            paths = mod.executor_paths(ROOT)
        state_path = svc.expand_path(
            config.get("state_path", "data/state.json"), svc.service_root_for_config(config_path)
        )
        self.assertTrue(paths["contract"], "the deployed tree must expose the drain contract")
        self.assertEqual(de.executor_lock_path(config, state_path), Path(paths["lock"]))
        self.assertEqual(de.executor_pause_path(config, state_path), Path(paths["pause"]))
        self.assertEqual(de.dashboard_db_path(config, state_path), paths["store"])
        self.assertIs(de.probe_executor_lock, paths["probe"])

    def test_fallback_is_announced_when_the_tree_predates_the_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bare = make_tree(Path(tmp) / "old-tree")
            with mock.patch.object(mod, "warn") as warner:
                paths = mod.executor_paths(bare)
            self.assertFalse(paths["contract"])
            self.assertEqual(bare / "data" / "executor.lock", Path(paths["lock"]))
            self.assertEqual(bare / "data" / "executor.pause", Path(paths["pause"]))
            self.assertTrue(warner.call_args_list, "a missing contract must be reported")


class DaemonStatusTest(unittest.TestCase):
    """launchctl classification: only "not loaded" counts as stopped."""

    def _status(self, returncode: int, stdout: str) -> str:
        def fake_run(cmd, check=True, capture=False):
            return subprocess.CompletedProcess(list(cmd), returncode, stdout=stdout, stderr="")

        with mock.patch.object(mod, "run", side_effect=fake_run):
            return mod.daemon_status()

    def test_running(self) -> None:
        self.assertEqual(mod.DAEMON_RUNNING, self._status(0, "\tstate = running\n\tpid = 42\n"))

    def test_not_loaded_is_stopped(self) -> None:
        self.assertEqual(mod.DAEMON_STOPPED, self._status(113, "Could not find service in domain\n"))

    def test_launchctl_error_is_unknown(self) -> None:
        self.assertEqual(mod.DAEMON_UNKNOWN, self._status(1, "Bootstrap failed: 5: Input/output error\n"))

    def test_missing_state_line_is_unknown(self) -> None:
        self.assertEqual(mod.DAEMON_UNKNOWN, self._status(0, "\tpid = 42\n"))

    def test_other_states_are_unknown_not_stopped(self) -> None:
        self.assertEqual(mod.DAEMON_UNKNOWN, self._status(0, "\tstate = waiting\n"))


class DrainPreflightTest(unittest.TestCase):
    """Pause the executor first, then wait for the review to finish itself."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = make_tree(Path(self.tmp.name) / "tree")
        self.lock = self.root / "data" / "executor.lock"
        self.pause = self.root / "data" / "executor.pause"
        self.store = self.root / "data" / "dashboard" / "dashboard.sqlite3"
        self.paths = {
            "lock": self.lock,
            "pause": self.pause,
            "store": self.store,
            "contract": True,
            "probe": None,
        }
        self.daemon = mod.DAEMON_RUNNING
        patches = [
            mock.patch.object(mod, "daemon_status", side_effect=lambda: self.daemon),
            mock.patch.object(mod, "executor_paths", side_effect=lambda root: self.paths),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _preflight(self, extra=("--drain-timeout", "1", "--poll-interval", "0.05")):
        args = mod.build_parser().parse_args(["--root", str(self.root), "preflight"] + list(extra))
        return mod.cmd_preflight(args)

    def _hold_lock(self):
        import fcntl

        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text("%d\n" % os.getpid(), encoding="utf-8")
        handle = self.lock.open("r+b")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(handle.close)
        return handle

    def test_pause_sentinel_is_created_before_any_waiting(self) -> None:
        self._hold_lock()
        self.assertEqual(mod.EXIT_BUSY, self._preflight(), "a held lock refuses the deploy instead of killing the review")
        self.assertTrue(self.pause.is_file(), "sentinel must be in place while we wait")
        payload = json.loads(self.pause.read_text(encoding="utf-8"))
        self.assertIn("requested_at", payload)
        self.assertEqual(0o600, self.pause.stat().st_mode & 0o777)

    def test_free_lock_with_the_sentinel_in_place_passes(self) -> None:
        self.assertEqual(0, self._preflight())
        self.assertTrue(self.pause.is_file())

    def test_lock_must_be_free_on_consecutive_checks(self) -> None:
        observations = [False, True, True]

        def flaky(paths):
            free = observations.pop(0) if observations else True
            return (free, None if free else 4242)

        with mock.patch.object(mod, "lock_is_free", side_effect=flaky):
            self.assertEqual(0, self._preflight())
        self.assertEqual([], observations, "a single free observation is not enough")

    def test_live_probe_is_used_when_the_tree_provides_one(self) -> None:
        from code_review_service import dashboard_execution as de

        self.paths["probe"] = de.probe_executor_lock
        handle = self._hold_lock()
        self.assertEqual((False, os.getpid()), mod.lock_is_free(self.paths))
        handle.close()
        self.assertEqual((True, None), mod.lock_is_free(self.paths))

    def test_stopped_daemon_with_a_free_lock_passes(self) -> None:
        self.daemon = mod.DAEMON_STOPPED
        self.assertEqual(0, self._preflight())
        self.assertTrue(self.pause.is_file(), "sentinel stays so install's kickstart stays paused")

    def test_stopped_daemon_with_a_really_held_lock_is_refused(self) -> None:
        # `code-review-service once` (service.py run_once) takes this same flock
        # from a plain shell, so a not-loaded launchd job is not evidence that
        # nothing is reviewing. Real flock, no mocked lock probe.
        self.daemon = mod.DAEMON_STOPPED
        self._hold_lock()
        self.assertFalse(mod.lock_is_free(self.paths)[0])
        self.assertEqual(mod.EXIT_BUSY, self._preflight())

    def test_every_daemon_status_probes_the_lock(self) -> None:
        for status in (mod.DAEMON_RUNNING, mod.DAEMON_STOPPED, mod.DAEMON_UNKNOWN):
            with self.subTest(status=status):
                self.daemon = status
                probes = []

                def counting(paths):
                    probes.append(status)
                    return (True, None)

                with mock.patch.object(mod, "lock_is_free", side_effect=counting):
                    self.assertEqual(0, self._preflight())
                self.assertGreaterEqual(len(probes), 2, "the lock must be probed, never assumed free")

    def test_unknown_daemon_state_is_drained_like_a_running_one(self) -> None:
        self.daemon = mod.DAEMON_UNKNOWN
        self._hold_lock()
        self.assertEqual(mod.EXIT_BUSY, self._preflight(), "unknown must never mean idle")

    def test_unknown_daemon_state_still_passes_once_the_lock_is_free(self) -> None:
        self.daemon = mod.DAEMON_UNKNOWN
        self.assertEqual(0, self._preflight())

    # ------------------------------------------------ legacy (undrainable) tree

    def test_legacy_tree_with_running_daemon_is_refused(self) -> None:
        self.paths["contract"] = False
        self.daemon = mod.DAEMON_RUNNING
        with mock.patch("builtins.print") as printer:
            self.assertEqual(mod.EXIT_BUSY, self._preflight())
        rendered = " ".join(str(call) for call in printer.call_args_list)
        self.assertIn("Refusing to replace the tree", rendered)
        self.assertIn("service.sh stop", rendered, "the operator needs the deliberate-stop step")
        self.assertFalse(self.pause.exists(), "a sentinel a legacy daemon ignores must not be left behind")

    def test_legacy_tree_with_unknown_daemon_state_is_refused(self) -> None:
        self.paths["contract"] = False
        self.daemon = mod.DAEMON_UNKNOWN
        self.assertEqual(mod.EXIT_BUSY, self._preflight())
        self.assertFalse(self.pause.exists())

    def test_legacy_tree_with_stopped_daemon_and_free_lock_may_be_bootstrapped(self) -> None:
        self.paths["contract"] = False
        self.daemon = mod.DAEMON_STOPPED
        self.assertEqual(0, self._preflight())

    def test_legacy_tree_with_stopped_daemon_but_held_lock_is_refused(self) -> None:
        self.paths["contract"] = False
        self.daemon = mod.DAEMON_STOPPED
        self._hold_lock()
        with mock.patch("builtins.print") as printer:
            self.assertEqual(mod.EXIT_BUSY, self._preflight())
        rendered = " ".join(str(call) for call in printer.call_args_list)
        self.assertIn("executor lock is held", rendered)
        self.assertIn("manual `once`", rendered)

    def test_legacy_refusal_keeps_a_sentinel_it_did_not_create(self) -> None:
        self.pause.parent.mkdir(parents=True, exist_ok=True)
        self.pause.write_text("{}", encoding="utf-8")
        self.paths["contract"] = False
        self.daemon = mod.DAEMON_RUNNING
        self.assertEqual(mod.EXIT_BUSY, self._preflight())
        self.assertTrue(self.pause.is_file(), "only our own artifact is cleaned up")

    def test_no_launchctl_means_unknown_not_stopped(self) -> None:
        self.paths["contract"] = False
        with mock.patch.object(mod, "daemon_status", side_effect=AssertionError("must not be called")):
            self.assertEqual(
                mod.EXIT_BUSY,
                self._preflight(("--no-launchctl", "--drain-timeout", "1", "--poll-interval", "0.05")),
            )

    def test_preflight_never_stops_or_kills_anything(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        drain = source.split("def cmd_preflight", 1)[1].split("def cmd_resume", 1)[0]
        # Code-level calls only; the words also appear in explanatory comments.
        for forbidden in ('"bootout"', '"kickstart"', "bootstrap_job(", "os.kill", "terminate("):
            self.assertNotIn(forbidden, drain)

    def test_running_attempt_rows_are_reported_not_aged_out(self) -> None:
        self.store.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.store))
        conn.execute("CREATE TABLE runs (id TEXT, status TEXT, updated_at TEXT)")
        conn.execute("INSERT INTO runs VALUES ('run-1', 'running', '1999-01-01T00:00:00+00:00')")
        conn.commit()
        conn.close()
        self.assertEqual(["run-1"], mod.running_attempts(self.store))
        with mock.patch.object(mod, "warn") as warner:
            self.assertEqual(0, self._preflight())
        self.assertTrue(any("run-1" in str(call) for call in warner.call_args_list))
        self.assertFalse(hasattr(mod, "ACTIVE_RUN_MAX_AGE_SECS"), "no timestamp age-out anywhere")

    def test_no_timestamp_is_consulted_for_liveness(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        drain = source.split("def cmd_preflight", 1)[1].split("def cmd_resume", 1)[0]
        for forbidden in ("fromisoformat", "total_seconds", "max_age", "os.kill", "SIGTERM", "SIGKILL"):
            self.assertNotIn(forbidden, drain)

    def test_resume_clears_the_sentinel_but_never_the_lock(self) -> None:
        self._preflight()
        self._hold_lock()
        args = mod.build_parser().parse_args(["--root", str(self.root), "resume"])
        self.assertEqual(0, mod.cmd_resume(args))
        self.assertFalse(self.pause.exists())
        self.assertTrue(self.lock.exists(), "the lock file belongs to the executor")
        self.assertEqual(0, mod.cmd_resume(args), "resume is idempotent")

    def test_lock_holder_pid_and_missing_files(self) -> None:
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text("4242\n", encoding="utf-8")
        self.assertEqual(4242, mod.lock_holder_pid(self.lock))
        self.assertEqual([], mod.running_attempts(self.store))
        self.assertFalse(mod.executor_lock_busy(self.root / "data" / "absent.lock"))


# ---------------------------------------------------------------------- verify


class VerifyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.version = "deploy-main-20260911055934-2252e94"
        self.root = make_tree(Path(self.tmp.name) / "tree", self.version)
        self.home = Path(self.tmp.name) / "home"
        (self.home / "review-of-legends").mkdir(parents=True)
        self.settings = self.home / "review-of-legends" / "dashboard-secrets.json"
        self.settings.write_text(
            json.dumps(
                {
                    "public_url": mod.CANONICAL_PUBLIC_URL,
                    "config_path": str(self.root / "config" / "reviewer.json"),
                    "google_client_id": "cid",
                    "google_client_secret": "secret",
                    "session_secret": "s" * 64,
                    "llmux_api_key": "key",
                    "llmux_base_url": "http://127.0.0.1:3456",
                }
            ),
            encoding="utf-8",
        )
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.responses = {
            "/healthz": (200, json.dumps({"status": "ok", "version": self.version, "auth_configured": True}).encode()),
            "/api/me": (401, b'{"error":"unauthenticated"}'),
            "/api/runs": (401, b'{"error":"unauthenticated"}'),
            "/api/runs?status=failed": (401, b'{"error":"unauthenticated"}'),
            "/api/usage": (401, b'{"error":"unauthenticated"}'),
            "/api/events": (401, b'{"error":"unauthenticated"}'),
        }
        state = mock.patch.object(mod, "launchctl_state", return_value="running")
        state.start()
        self.addCleanup(state.stop)

    def _verify(self, extra=()):
        def probe(url, timeout):
            path = url.split(mod.BASE_URL, 1)[1]
            return self.responses[path]

        args = mod.build_parser().parse_args(
            ["--root", str(self.root), "verify", "--timeout", "1"] + list(extra)
        )
        with mock.patch.object(mod, "http_probe", side_effect=probe):
            return mod.cmd_verify(args)

    def test_healthy_and_closed_dashboard_passes(self) -> None:
        self.assertEqual(0, self._verify())

    def test_unauthenticated_identity_route_must_be_401(self) -> None:
        self.responses["/api/me"] = (200, b'{"email":"owner@company.example","role":"admin"}')
        self.assertEqual(1, self._verify())

    def test_unauthenticated_data_route_leak_fails(self) -> None:
        self.responses["/api/runs"] = (200, b'{"runs":[{"id":"r1"}],"next_offset":null}')
        self.assertEqual(1, self._verify())

    def test_run_data_inside_a_401_body_still_fails(self) -> None:
        self.responses["/api/runs"] = (401, b'{"runs":[{"id":"r1"}]}')
        self.assertEqual(1, self._verify())

    def test_unhealthy_dashboard_fails(self) -> None:
        self.responses["/healthz"] = (503, b'{"status":"degraded"}')
        self.assertEqual(1, self._verify())

    def test_stale_process_version_fails(self) -> None:
        self.responses["/healthz"] = (
            200,
            json.dumps({"status": "ok", "version": "deploy-main-old", "auth_configured": True}).encode(),
        )
        self.assertEqual(1, self._verify())

    def test_missing_google_credentials_warn_by_default_and_gate_with_flag(self) -> None:
        self.responses["/healthz"] = (
            200,
            json.dumps({"status": "ok", "version": self.version, "auth_configured": False}).encode(),
        )
        self.assertEqual(0, self._verify(), "deploy still ships the dashboard; login fails closed")
        self.assertEqual(1, self._verify(["--require-auth"]))

    def test_stopped_job_fails(self) -> None:
        with mock.patch.object(mod, "launchctl_state", return_value=None):
            self.assertEqual(1, self._verify())

    def test_unprovisioned_dashboard_fails_with_names_only(self) -> None:
        self.settings.write_text(json.dumps({"public_url": mod.CANONICAL_PUBLIC_URL}), encoding="utf-8")
        self.responses["/healthz"] = (503, b"")
        with mock.patch("builtins.print") as printer:
            self.assertEqual(1, self._verify())
        rendered = " ".join(str(call) for call in printer.call_args_list)
        self.assertIn("google_client_id", rendered)
        self.assertIn("secret setting(s) (names withheld)", rendered)
        self.assertNotIn("google_client_secret", rendered, "secret names stay withheld")

    def test_unprovisioned_dashboard_can_be_accepted_explicitly(self) -> None:
        self.settings.write_text(json.dumps({}), encoding="utf-8")
        self.responses["/healthz"] = (503, b"")
        self.assertEqual(0, self._verify(["--allow-unconfigured"]))

    def test_provisioned_but_broken_dashboard_always_fails(self) -> None:
        self.responses["/healthz"] = (503, b"")
        self.assertEqual(1, self._verify(["--allow-unconfigured"]))

    def test_env_opt_out_is_honoured_and_announced(self) -> None:
        self.settings.write_text(json.dumps({}), encoding="utf-8")
        self.responses["/healthz"] = (503, b"")
        with mock.patch.dict(os.environ, {"ROL_DASHBOARD_ALLOW_UNCONFIGURED": "1"}, clear=False):
            with mock.patch("builtins.print") as printer:
                self.assertEqual(0, self._verify())
        rendered = " ".join(str(call) for call in printer.call_args_list)
        self.assertIn("ROL_DASHBOARD_ALLOW_UNCONFIGURED", rendered, "an opt-out must never be silent")

    def test_verifier_does_not_follow_redirects(self) -> None:
        self.assertIsNone(
            mod.NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://elsewhere/")
        )


# --------------------------------------------------------------- install flow


class InstallFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.root = make_tree(Path(self.tmp.name) / "tree", "deploy-main-1")
        self.runner = FakeRun()
        patches = [
            mock.patch.object(mod, "run", self.runner),
            mock.patch.object(mod, "select_python", return_value=Path("/opt/homebrew/bin/python3.12")),
            mock.patch.object(mod, "launchctl_state", return_value="running"),
            mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=False),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _install(self):
        args = mod.build_parser().parse_args(["--root", str(self.root), "install", "--timeout", "1"])
        return mod.cmd_install(args)

    def test_install_writes_job_settings_and_venv(self) -> None:
        self.assertEqual(0, self._install())
        plist = self.home / "Library" / "LaunchAgents" / ("%s.plist" % mod.LABEL)
        with plist.open("rb") as handle:
            spec = plistlib.load(handle)
        venv = self.home / "review-of-legends" / "venvs" / "deploy-main-1"
        settings = self.home / "review-of-legends" / "dashboard-secrets.json"
        self.assertEqual(str(venv / "bin" / "python"), spec["ProgramArguments"][0])
        self.assertEqual(str(settings), spec["ProgramArguments"][-1])
        self.assertEqual(str(self.root.resolve()), spec["WorkingDirectory"])
        self.assertTrue(settings.is_file())
        payload = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(str(self.root.resolve() / "config" / "reviewer.json"), payload["config_path"])
        self.assertNotIn("store_path", payload, "the web tier derives it from config_path")
        self.assertEqual(0o600, settings.stat().st_mode & 0o777)

    def test_install_kickstarts_the_job_and_touches_no_other_label(self) -> None:
        self._install()
        launchctl = [cmd for cmd in self.runner.calls if cmd[0] == "launchctl"]
        self.assertTrue(any(cmd[1] == "bootstrap" for cmd in launchctl))
        self.assertTrue(any(cmd[1] == "kickstart" for cmd in launchctl))
        joined = " ".join(" ".join(cmd) for cmd in launchctl)
        self.assertNotIn("%s/%s " % (mod.domain(), mod.DAEMON_LABEL), joined + " ")

    def test_install_fails_when_job_never_reaches_running(self) -> None:
        # No credentials are provisioned in this tmp home, so the failure must
        # still be a failure unless the operator opts out explicitly.
        with mock.patch.object(mod, "launchctl_state", return_value="waiting"):
            with self.assertRaises(SystemExit):
                self._install()

    def test_unconfigured_job_may_be_accepted_explicitly(self) -> None:
        args = mod.build_parser().parse_args(
            ["--root", str(self.root), "install", "--timeout", "1", "--allow-unconfigured"]
        )
        with mock.patch.object(mod, "launchctl_state", return_value="waiting"):
            self.assertEqual(0, mod.cmd_install(args))
        self.assertTrue((self.home / "Library" / "LaunchAgents" / ("%s.plist" % mod.LABEL)).is_file())

    def _old_venvs(self, count: int) -> list:
        """``count`` previous per-version venvs, oldest first by mtime (a day apart)."""
        venvs = self.home / "review-of-legends" / "venvs"
        olds = [venvs / ("deploy-main-0%02d" % index) for index in range(1, count + 1)]
        for age, old in enumerate(reversed(olds), start=1):
            (old / "bin").mkdir(parents=True)
            (old / "pyvenv.cfg").write_text("home = /opt/homebrew/bin\n", encoding="utf-8")
            (old / "keep-me").write_text("", encoding="utf-8")
            stamp = time.time() - age * 86400
            os.utime(str(old), (stamp, stamp))
        return olds

    def _live_plist(self, venv_dir: Path) -> None:
        plist = self.home / "Library" / "LaunchAgents" / ("%s.plist" % mod.LABEL)
        plist.parent.mkdir(parents=True, exist_ok=True)
        with plist.open("wb") as handle:
            plistlib.dump({"Label": mod.LABEL, "ProgramArguments": [str(venv_dir / "bin" / "python"), "-m", "x"]}, handle)

    def test_install_keeps_the_new_the_live_and_the_newest_previous_venvs_and_removes_the_rest(self) -> None:
        # Venvs had piled up on a long-running host, one per deploy.
        # Rollback still needs no network for the venv the job ran before this
        # deploy and the newest previous ones; older ones go.
        olds = self._old_venvs(mod.VENV_KEEP_PREVIOUS + 4)
        live = olds[0]  # the oldest one is what the job runs right now: kept whatever its age
        self._live_plist(live)
        self.runner.loaded_program = str(live / "bin" / "python")
        venvs = live.parent
        outside = Path(self.tmp.name) / "outside"
        (outside / "data").mkdir(parents=True)
        (outside / "data" / "file").write_text("x", encoding="utf-8")
        (venvs / "linked").symlink_to(outside, target_is_directory=True)
        (venvs / "notes.txt").write_text("operator file", encoding="utf-8")
        operator_dir = venvs / "operator-scratch"  # not a venv (no pyvenv.cfg), old: never a candidate
        operator_dir.mkdir()
        os.utime(str(operator_dir), (1, 1))

        self.assertEqual(0, self._install())

        newest_previous = olds[-mod.VENV_KEEP_PREVIOUS:]
        kept = {path.name for path in venvs.iterdir()}
        expected = {"deploy-main-1", live.name, "linked", "notes.txt", "operator-scratch", mod.LAST_RUNNING_FILE} | {
            path.name for path in newest_previous
        }
        self.assertEqual(kept, expected)
        for old in newest_previous + [live]:
            self.assertTrue((old / "keep-me").is_file(), "a rollback runtime that must stay was removed: %s" % old.name)
        self.assertTrue((outside / "data" / "file").is_file(), "a symlink under the venv root was followed")
        self.assertTrue((venvs / "linked").is_symlink())

    def test_a_reused_venv_counts_as_recent(self) -> None:
        # A redeploy of an older version (a rollback) reuses its venv; prune_venvs
        # keeps the newest by mtime, so reuse must make it recent.
        self.assertEqual(0, self._install())
        venv = self.home / "review-of-legends" / "venvs" / "deploy-main-1"
        os.utime(str(venv), (1, 1))
        self.assertEqual(0, self._install())
        self.assertGreater(venv.stat().st_mtime, time.time() - 3600)

    def test_a_plist_only_install_never_hides_the_venv_the_job_last_ran(self) -> None:
        # install --no-launchctl rewrites the plist without restarting the job, so the
        # plist no longer names what launchd last ran. The venv a confirmed-running
        # install recorded is kept by the next real install (trinity review, astra).
        olds = self._old_venvs(mod.VENV_KEEP_PREVIOUS + 4)
        ran = olds[0]  # the oldest: only a record can keep it
        self._live_plist(ran)
        venvs = ran.parent
        (venvs / mod.LAST_RUNNING_FILE).write_text(ran.name + "\n", encoding="utf-8")
        staged = mod.build_parser().parse_args(["--root", str(self.root), "install", "--no-launchctl"])
        self.assertEqual(0, mod.cmd_install(staged))
        self.assertEqual(0, self._install())
        self.assertTrue((ran / "keep-me").is_file(), "the venv the job last ran was removed")
        self.assertEqual((venvs / mod.LAST_RUNNING_FILE).read_text(encoding="utf-8").strip(), "deploy-main-1")

    def test_with_no_record_the_venv_launchd_has_loaded_is_kept_after_a_plist_only_install(self) -> None:
        # First deploy with this installer: no .last-running yet. launchctl still
        # reports what the job runs, whatever the plist now says (trinity, astra).
        olds = self._old_venvs(mod.VENV_KEEP_PREVIOUS + 4)
        ran = olds[0]
        self._live_plist(ran)
        self.runner.loaded_program = str(ran / "bin" / "python")
        staged = mod.build_parser().parse_args(["--root", str(self.root), "install", "--no-launchctl"])
        self.assertEqual(0, mod.cmd_install(staged))
        self.assertEqual(0, self._install())
        self.assertTrue((ran / "keep-me").is_file(), "the venv launchd had loaded was removed")
        self.assertFalse(olds[1].exists(), "nothing was pruned")

    def test_with_no_record_and_no_launchd_program_no_venv_is_removed(self) -> None:
        olds = self._old_venvs(mod.VENV_KEEP_PREVIOUS + 4)
        self._live_plist(olds[0])
        self.assertEqual(0, self._install())  # loaded_program None: what ran before is unknown
        for old in olds:
            self.assertTrue(old.is_dir(), "removed %s without knowing what the job ran" % old.name)
        venvs = olds[0].parent
        self.assertEqual((venvs / mod.LAST_RUNNING_FILE).read_text(encoding="utf-8").strip(), "deploy-main-1")

    def test_install_removes_no_venv_unless_the_new_job_is_running(self) -> None:
        olds = self._old_venvs(mod.VENV_KEEP_PREVIOUS + 3)
        cases = (
            ("no launchctl", ["--no-launchctl"], "running"),
            ("accepted down", ["--allow-unconfigured", "--timeout", "1"], "waiting"),
        )
        for label, extra, state in cases:
            with self.subTest(label):
                args = mod.build_parser().parse_args(["--root", str(self.root), "install"] + extra)
                with mock.patch.object(mod, "launchctl_state", return_value=state):
                    self.assertEqual(0, mod.cmd_install(args))
                for old in olds:
                    self.assertTrue(old.is_dir(), "%s: removed %s without a running job" % (label, old.name))

    def test_a_venv_that_cannot_be_removed_never_fails_the_deploy(self) -> None:
        olds = self._old_venvs(mod.VENV_KEEP_PREVIOUS + 2)
        stuck = olds[0]
        # launchd runs this same version already (a redeploy): known, and kept anyway as the install.
        self.runner.loaded_program = str(olds[0].parent / "deploy-main-1" / "bin" / "python")
        real_rmtree = mod.shutil.rmtree

        def rmtree(path, *args, **kwargs):
            if Path(path).name == stuck.name:
                raise PermissionError(13, "Operation not permitted", str(path))
            return real_rmtree(path, *args, **kwargs)

        with mock.patch.object(mod.shutil, "rmtree", rmtree):
            self.assertEqual(0, self._install())
        self.assertTrue(stuck.is_dir())
        self.assertFalse(olds[1].exists(), "one stuck venv stopped the others from going")

    def test_install_refuses_a_tree_that_is_not_the_service(self) -> None:
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        args = mod.build_parser().parse_args(["--root", str(empty), "install"])
        with self.assertRaises(SystemExit):
            mod.cmd_install(args)

    def test_uninstall_removes_the_job_but_preserves_data_and_secrets(self) -> None:
        self._install()
        plist = self.home / "Library" / "LaunchAgents" / ("%s.plist" % mod.LABEL)
        settings = self.home / "review-of-legends" / "dashboard-secrets.json"
        (self.root / "data" / "state.json").write_text("{}", encoding="utf-8")
        args = mod.build_parser().parse_args(["--root", str(self.root), "uninstall"])
        self.assertEqual(0, mod.cmd_uninstall(args))
        self.assertFalse(plist.exists())
        self.assertTrue(settings.is_file())
        self.assertTrue((self.root / "data" / "state.json").is_file())
        self.assertTrue((self.home / "review-of-legends" / "venvs" / "deploy-main-1").is_dir())


class DeployEnvTest(unittest.TestCase):
    """config/deploy.env: launchd labels and dashboard policy are settings, not code."""

    KEYS = ("ROL_SERVICE_LABEL", "ROL_DASHBOARD_LABEL", "ROL_DASHBOARD_OWNER",
            "ROL_DASHBOARD_VIEWER_DOMAINS", "ROL_DASHBOARD_PUBLIC_URL")

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "tree"
        (self.root / "config").mkdir(parents=True)
        shutil.copy(SERVICE_SH, self.root / "service.sh")
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        saved = {name: getattr(mod, name) for name in ("LABEL", "DAEMON_LABEL", "CANONICAL_PUBLIC_URL")}
        self.addCleanup(lambda: [setattr(mod, k, v) for k, v in saved.items()])

    def env_without_keys(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in self.KEYS}
        env["HOME"] = str(self.home)
        # Never reach the real launchd: a fake launchctl records and refuses.
        fake_bin = Path(self.tmp.name) / "bin"
        if not fake_bin.is_dir():
            fake_bin.mkdir()
            launchctl = fake_bin / "launchctl"
            launchctl.write_text('#!/bin/sh\necho "$@" >> "%s"\nexit 1\n' % (Path(self.tmp.name) / "launchctl.log"))
            launchctl.chmod(0o755)
        env["PATH"] = "%s:%s" % (fake_bin, env.get("PATH", ""))
        return env

    def write_env(self, text: str) -> None:
        (self.root / "config" / "deploy.env").write_text(text, encoding="utf-8")

    def labels(self, env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(self.root / "service.sh"), "labels"], env=env,
                              capture_output=True, text=True, check=False)

    def test_service_sh_defaults_without_a_deploy_env(self) -> None:
        result = self.labels(self.env_without_keys())
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("service=ai.2lab.review-of-legends\ndashboard=ai.2lab.review-of-legends.dashboard\n", result.stdout)

    def test_service_sh_reads_labels_from_deploy_env(self) -> None:
        self.write_env("# this host\nROL_SERVICE_LABEL=com.example.rol\n\nROL_DASHBOARD_OWNER=owner@company.example\n")
        result = self.labels(self.env_without_keys())
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("service=com.example.rol\ndashboard=com.example.rol.dashboard\n", result.stdout)

    def test_service_sh_environment_wins_over_the_file(self) -> None:
        self.write_env("ROL_SERVICE_LABEL=com.example.file\nROL_DASHBOARD_LABEL=com.example.file.web\n")
        env = self.env_without_keys()
        env["ROL_SERVICE_LABEL"] = "com.example.env"
        result = self.labels(env)
        self.assertEqual("service=com.example.env\ndashboard=com.example.file.web\n", result.stdout)

    def test_service_sh_refuses_unknown_keys_and_malformed_lines(self) -> None:
        for text in ("PATH=/tmp/evil\n", "ROL_SERVICE_LABEL\n", "export ROL_SERVICE_LABEL=x\n",
                     "ROL_SERVICE_LABEL=a\nROL_SERVICE_LABEL=b\n"):
            with self.subTest(text=text):
                self.write_env(text)
                result = self.labels(self.env_without_keys())
                self.assertNotEqual(0, result.returncode)
                self.assertIn("deploy.env", result.stderr)

    def test_service_sh_never_executes_the_file(self) -> None:
        marker = Path(self.tmp.name) / "ran"
        self.write_env("ROL_DASHBOARD_OWNER=$(touch %s)\nROL_SERVICE_LABEL=`touch %s`\n" % (marker, marker))
        result = self.labels(self.env_without_keys())
        # The label is refused as a label; neither value ran.
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertFalse(marker.exists())
        self.write_env("ROL_DASHBOARD_OWNER=$(touch %s)\n" % marker)
        result = self.labels(self.env_without_keys())
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(marker.exists())

    @unittest.skipUnless(Path("/usr/bin/plutil").exists(), "plutil reads LaunchAgent plists on macOS")
    def test_install_refuses_a_second_job_on_the_same_tree(self) -> None:
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        for working_directory in (self.root, self.root.resolve()):
            with self.subTest(working_directory=str(working_directory)):
                with (agents / "com.example.old.plist").open("wb") as fh:
                    plistlib.dump({"Label": "com.example.old", "WorkingDirectory": str(working_directory)}, fh)
                result = subprocess.run(["bash", str(self.root / "service.sh"), "install"], env=self.env_without_keys(),
                                        capture_output=True, text=True, check=False)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("com.example.old", result.stdout + result.stderr)
                self.assertFalse((agents / "ai.2lab.review-of-legends.plist").exists())
                self.assertFalse((Path(self.tmp.name) / "launchctl.log").exists(), "launchctl must not run")

    def test_installer_reads_the_same_file(self) -> None:
        self.write_env("ROL_SERVICE_LABEL=com.example.rol\nROL_DASHBOARD_PUBLIC_URL=https://rol.company.example\n"
                       "ROL_DASHBOARD_OWNER=owner@company.example\n")
        with mock.patch.dict(os.environ, self.env_without_keys(), clear=True):
            mod.apply_deploy_env(self.root)
            self.assertEqual("com.example.rol", mod.DAEMON_LABEL)
            self.assertEqual("com.example.rol.dashboard", mod.LABEL)
            self.assertEqual("https://rol.company.example", mod.CANONICAL_PUBLIC_URL)
            plist = mod.build_plist(self.root, self.root / "venv", self.root / "settings.json")
            self.assertEqual("com.example.rol.dashboard", plist["Label"])
            self.assertEqual("owner@company.example", plist["EnvironmentVariables"]["ROL_DASHBOARD_OWNER"])

    def test_installer_refuses_unknown_and_repeated_keys(self) -> None:
        for text in ("ROL_DASHBOARD_OWNER=a@b.c\nSOMETHING_ELSE=1\n", "ROL_SERVICE_LABEL=a\nROL_SERVICE_LABEL=b\n"):
            with self.subTest(text=text):
                self.write_env(text)
                with mock.patch.dict(os.environ, self.env_without_keys(), clear=True):
                    with self.assertRaises(SystemExit):
                        mod.read_deploy_env(self.root)

    def test_both_readers_refuse_one_label_for_both_jobs_or_an_unsafe_label(self) -> None:
        for text in ("ROL_SERVICE_LABEL=com.example.rol\nROL_DASHBOARD_LABEL=com.example.rol\n",
                     "ROL_SERVICE_LABEL=../evil\n", "ROL_DASHBOARD_LABEL=a b\n"):
            with self.subTest(text=text):
                self.write_env(text)
                result = self.labels(self.env_without_keys())
                self.assertNotEqual(0, result.returncode, result.stdout)
                self.assertIn("[ERROR]", result.stderr)
                with mock.patch.dict(os.environ, self.env_without_keys(), clear=True):
                    with self.assertRaises(SystemExit):
                        mod.apply_deploy_env(self.root)

    def test_an_empty_environment_variable_does_not_override_the_file(self) -> None:
        # Same rule in both readers: set and non-empty wins, empty counts as unset.
        self.write_env("ROL_SERVICE_LABEL=com.example.file\n")
        env = self.env_without_keys()
        env["ROL_SERVICE_LABEL"] = ""
        self.assertIn("service=com.example.file\n", self.labels(env).stdout)
        with mock.patch.dict(os.environ, env, clear=True):
            mod.apply_deploy_env(self.root)
            self.assertEqual("com.example.file", mod.DAEMON_LABEL)

    def test_example_lists_every_key(self) -> None:
        example = (ROOT / "config" / "deploy.env.example").read_text(encoding="utf-8")
        for key in mod.DEPLOY_ENV_KEYS:
            self.assertIn(key + "=", example)
        self.assertIn(" ".join(mod.DEPLOY_ENV_KEYS), SERVICE_SH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
