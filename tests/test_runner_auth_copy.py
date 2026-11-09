from __future__ import annotations

import json
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


class RunnerAuthCopyTest(unittest.TestCase):
    """The Codex credential copy must be scoped to Codex runner commands."""

    def _build_env(self, command: list[str], run_dir: Path, fake_home: Path) -> dict[str, str]:
        codex_dir = fake_home / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        (codex_dir / "auth.json").write_text(json.dumps({"token": "secret"}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}, clear=False):
            os.environ.pop("CODEX_HOME", None)
            return svc.build_agent_environ(
                {"command": command, "isolate_home": True}, run_dir, ROOT / "config"
            )

    def test_codex_command_copies_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            env = self._build_env(["codex", "exec"], run_dir, Path(tmp) / "home")
            self.assertTrue((Path(env["CODEX_HOME"]) / "auth.json").is_file())

    def test_codex_substring_wrapper_never_touches_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            env = self._build_env(["my-codex-shim", "exec"], run_dir, Path(tmp) / "home")
            self.assertFalse((Path(env["CODEX_HOME"]) / "auth.json").exists())

    def test_non_codex_command_never_touches_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            env = self._build_env(
                ["python3", "{service_root}/code_review_service/llm_runner.py"],
                run_dir,
                Path(tmp) / "home",
            )
            self.assertFalse((Path(env["CODEX_HOME"]) / "auth.json").exists())


if __name__ == "__main__":
    unittest.main()
