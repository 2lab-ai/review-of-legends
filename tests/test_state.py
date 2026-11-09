"""State file handling tests.

The PR review flagged the malformed-state silent reset behavior. We require
``load_state`` to fail closed on invalid JSON and to back up the corrupt file.
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

from code_review_service.service import (
    ServiceError,
    load_state,
    migrate_legacy_state_file,
    migrate_state,
    save_state,
)
from code_review_service import service as svc


class LoadStateTest(unittest.TestCase):
    def test_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = load_state(path)
            self.assertEqual(state, {"repositories": {}})

    def test_valid_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            data = {"repositories": {"owner/repo": {"reviewed": {"1": {"head_sha": "abc"}}}}}
            save_state(path, data)
            loaded = load_state(path)
            self.assertEqual(loaded["repositories"]["owner/repo"]["reviewed"]["1"]["head_sha"], "abc")

    def test_malformed_state_fails_closed_and_backs_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ServiceError):
                load_state(path)
            backups = sorted(path.parent.glob("state.json.corrupt-*"))
            self.assertEqual(len(backups), 1, f"expected one backup, got {backups}")
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "{not json")
            self.assertFalse(path.exists(), "corrupt state should be moved out of place")

    def test_migrate_legacy_reviewed(self) -> None:
        state = {"reviewed": {"1": {"head_sha": "abc"}}}
        migrated = migrate_state(state, ["owner/repo"])
        self.assertEqual(migrated["repositories"]["owner/repo"]["reviewed"]["1"]["head_sha"], "abc")

    def test_migrate_legacy_state_file_copies_before_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            legacy = tmp_path / "legacy-state.json"
            target = tmp_path / "data" / "state.json"
            legacy.write_text(
                json.dumps({"repositories": {"owner/repo": {"reviewed": {"1": {"head_sha": "abc"}}}}}),
                encoding="utf-8",
            )

            with mock.patch.object(svc, "LEGACY_STATE_PATHS", (legacy,)):
                self.assertTrue(migrate_legacy_state_file(target))

            loaded = load_state(target)
            self.assertEqual(loaded["repositories"]["owner/repo"]["reviewed"]["1"]["head_sha"], "abc")
            self.assertEqual(legacy.read_text(encoding="utf-8"), target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
