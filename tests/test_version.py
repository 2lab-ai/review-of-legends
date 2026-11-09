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


class VersionTest(unittest.TestCase):
    def test_current_version_reads_version_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "VERSION").write_text("deploy-main-20260520010101-abcdef1\n", encoding="utf-8")
            self.assertEqual(svc.current_version(root), "deploy-main-20260520010101-abcdef1")

    def test_review_body_includes_current_version(self) -> None:
        with mock.patch.object(svc, "current_version", return_value="deploy-main-test"):
            body = svc.build_review_body(
                {"marker": svc.MARKER},
                {"number": 12, "head": {"sha": "abc123"}},
                [{"agent": "correctness", "summary": "ok"}],
                [],
                0,
            )
        self.assertIn("Version: `deploy-main-test`", body)

    def test_version_command_prints_the_deployed_version(self) -> None:
        # bin/code-review-service always passes --config; printing the version
        # must not need the config to exist, only the tree's VERSION file.
        with tempfile.TemporaryDirectory() as tmp:
            version_file = Path(tmp) / "VERSION"
            version_file.write_text("v1.0.3\n", encoding="utf-8")
            stdout = io.StringIO()
            with mock.patch.object(svc, "DEFAULT_VERSION_FILE", version_file):
                with contextlib.redirect_stdout(stdout):
                    code = svc.main(["--config", str(Path(tmp) / "missing.json"), "version"])
        self.assertEqual(0, code)
        self.assertEqual("v1.0.3\n", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
