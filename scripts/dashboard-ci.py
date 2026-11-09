#!/usr/bin/env python3
"""The two Python halves of deploy.yml's "Test dashboard web tier" step.

  plan       under /usr/bin/python3: print the interpreter the installer's
             select_python() would build the dashboard venv from on its first
             line, then pip_install_args() for requirements-dashboard.txt one
             argument per line, so CI builds and installs what a deploy does.
  run-tests  under that venv's python: run the whole suite and fail on a
             failure, on an empty run, or on any skipped test.

Stdlib-only and runnable by macOS ``/usr/bin/python3`` (3.9), like
install-dashboard.py beside it: ``plan`` runs there. Both subcommands run from
the repository root.

What "any skipped test fails" costs: every way unittest skips lands in
``result.skipped`` -- a module-level ``raise unittest.SkipTest``, ``@skip``,
``SkipTest`` raised in ``setUpClass`` or ``setUpModule``, and
``self.skipTest()`` inside a ``subTest`` (measured on 3.9.6 and 3.14.6), and
``@skipIf``/``@skipUnless``, which are ``@skip`` with a condition. So a
legitimate conditional skip added later blocks every deploy until it becomes an
assertion or its test leaves this run. A test marked ``@expectedFailure`` that
fails is not a skip: it counts as a success here.
"""

import argparse
import contextlib
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from typing import List, Optional, Sequence

INSTALLER = Path(__file__).resolve().parent / "install-dashboard.py"


def load_installer():
    spec = importlib.util.spec_from_file_location("install_dashboard", str(INSTALLER))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plan_lines(installer, requirements: Path) -> List[str]:
    """The interpreter, then the pip arguments: one stdout line each.

    Both are asked for before anything is printed, so a failure in either
    leaves stdout empty instead of half an answer. The installer reports what
    it chose with info/warn lines on stdout; they go to stderr here, where the
    job log still shows them.
    """
    with contextlib.redirect_stdout(sys.stderr):
        chosen = installer.select_python()
        args = installer.pip_install_args(requirements)
    return [str(chosen)] + list(args)


def judge(result: unittest.TestResult) -> Optional[str]:
    """Why the run must fail the step, or None when it may pass."""
    if not result.wasSuccessful():
        return "dashboard venv test run failed"
    if result.testsRun == 0:
        return "the dashboard venv test run found no tests"
    if result.skipped:
        return "%d tests skipped in the dashboard venv test run; every test must run here" % len(result.skipped)
    return None


def cmd_plan(args: argparse.Namespace) -> int:
    print(*plan_lines(load_installer(), Path("requirements-dashboard.txt")), sep="\n")
    return 0


def cmd_run_tests(args: argparse.Namespace) -> int:
    # Discovery start dir, pattern, top-level dir and warnings are those of
    # `python -m unittest discover -s tests` run from the repository root.
    # Running this file puts scripts/ at sys.path[0] where `-m` puts the
    # working directory, so it is set to the working directory explicitly:
    # tests import `tests.*` from there. sys.argv still differs (it is this
    # script's), and the run returns its result to be judged instead of
    # exiting. The judgement reads unittest's result object, not its output:
    # tests write to the same streams, and a buffered stdout "OK" can land
    # after the summary line.
    sys.path[0] = os.getcwd()
    result = unittest.main(module=None, argv=["unittest", "discover", "-s", "tests"], exit=False).result
    message = judge(result)
    if message:
        print("::error::%s" % message)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CI helpers for the dashboard web tier step in deploy.yml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="print the venv interpreter, then the pip arguments, one per line").set_defaults(
        func=cmd_plan
    )
    sub.add_parser("run-tests", help="run the suite; fail on a failure, no tests or any skip").set_defaults(
        func=cmd_run_tests
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
