"""Artifact write helpers should create private (0600) files.

The PR review flagged review prompts and responses being written with default
permissions, which exposes private PR diffs to other local users on shared
machines.
"""

from __future__ import annotations

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

from code_review_service.service import write_private_text


class WritePrivateTextTest(unittest.TestCase):
    def test_creates_file_with_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "subdir" / "secret.txt"
            write_private_text(target, "secret contents")
            mode = stat.S_IMODE(target.stat().st_mode)
            self.assertEqual(mode, 0o600, oct(mode))
            self.assertEqual(target.read_text(encoding="utf-8"), "secret contents")

    def test_overwrites_existing_file_keeps_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "secret.txt"
            target.write_text("loose", encoding="utf-8")
            os.chmod(target, 0o644)
            write_private_text(target, "tight")
            mode = stat.S_IMODE(target.stat().st_mode)
            self.assertEqual(mode, 0o600, oct(mode))
            self.assertEqual(target.read_text(encoding="utf-8"), "tight")

    def test_write_error_never_closes_an_already_closed_fd(self) -> None:
        # Review runs write artifacts from a thread pool: closing an fd number
        # twice can close another thread's file that reused it in between.
        # The write fails after os.fdopen owns the fd, so the file object
        # closes it and os.close must not be called on it again.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "secret.txt"
            with mock.patch("os.fdopen", wraps=os.fdopen) as fdopen, mock.patch("os.close", wraps=os.close) as close, \
                    self.assertRaises(UnicodeEncodeError):
                write_private_text(target, "\ud800")
            close.assert_not_called()
            fdopen.assert_called_once()
            fd = fdopen.call_args.args[0]
            # Checked before the temp dir's cleanup opens directory fds that
            # could reuse the number; nothing else in this thread opens files.
            with self.assertRaises(OSError, msg="fd %d leaked after the write error" % fd):
                os.fstat(fd)


if __name__ == "__main__":
    unittest.main()
