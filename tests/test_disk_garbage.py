"""What a full disk (or a failed step) leaves behind must not stay on it.

review-host, 2026-10-27: the disk filled, ``save_state`` failed mid-dump and left
``data/tmp2spm_og3`` (441,862 bytes) next to ``state.json``; a clone that
failed after writing part of a checkout is removed by a silent
``shutil.rmtree``, so nothing in the log says it went.
"""

from __future__ import annotations

import errno
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


def _enospc(*_args, **_kwargs):
    raise OSError(errno.ENOSPC, "No space left on device")


class SaveStateLeavesNoTempTest(unittest.TestCase):
    def _leftovers(self, directory: Path) -> list:
        return sorted(p.name for p in directory.iterdir() if p.name != "state.json")

    def test_a_dump_that_runs_out_of_disk_leaves_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            svc.save_state(path, {"repositories": {"o/r": {"reviewed": {}}}})
            before = path.read_bytes()

            def half_then_full(obj, handle, **kwargs):
                handle.write("{\n  \"repositories\": {")  # a partial dump reached the file
                handle.flush()
                _enospc()

            with mock.patch.object(svc.json, "dump", half_then_full), self.assertRaises(OSError):
                svc.save_state(path, {"repositories": {"o/r": {"reviewed": {"1": {}}}}})
            self.assertEqual(self._leftovers(Path(tmp)), [], "a failed save left its temp file behind")
            self.assertEqual(path.read_bytes(), before, "the last good state was replaced")

    def test_a_replace_that_fails_leaves_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            with mock.patch.object(svc.os, "replace", _enospc), self.assertRaises(OSError):
                svc.save_state(path, {"repositories": {}})
            self.assertEqual(self._leftovers(Path(tmp)), [], "a failed replace left its temp file behind")

    def test_a_failed_legacy_migration_leaves_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "legacy" / "state.json"
            legacy.parent.mkdir()
            legacy.write_text("{\"repositories\": {}}\n", encoding="utf-8")
            target = Path(tmp) / "data" / "state.json"
            with mock.patch.object(svc, "LEGACY_STATE_PATHS", (legacy,)), \
                 mock.patch.object(svc.os, "replace", _enospc), self.assertRaises(OSError):
                svc.migrate_legacy_state_file(target)
            self.assertEqual(sorted(p.name for p in target.parent.iterdir()), [], "a failed migration left its temp file")

    def test_a_legacy_migration_that_runs_out_of_disk_mid_write_leaves_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "legacy" / "state.json"
            legacy.parent.mkdir()
            legacy.write_text("{\"repositories\": {}}\n", encoding="utf-8")
            target = Path(tmp) / "data" / "state.json"
            real = svc.tempfile.NamedTemporaryFile

            def half_written(*args, **kwargs):
                handle = real(*args, **kwargs)
                write = handle.write

                def write_then_full(data):
                    write(data[:8])  # a prefix reaches the file
                    handle.flush()
                    _enospc()

                handle.write = write_then_full
                return handle

            with mock.patch.object(svc, "LEGACY_STATE_PATHS", (legacy,)), \
                 mock.patch.object(svc.tempfile, "NamedTemporaryFile", half_written), \
                 self.assertRaises(OSError) as caught:
                svc.migrate_legacy_state_file(target)
            self.assertEqual(caught.exception.errno, errno.ENOSPC)
            self.assertEqual(sorted(p.name for p in target.parent.iterdir()), [], "a half-written migration left its temp file")
            self.assertEqual(legacy.read_text(encoding="utf-8"), "{\"repositories\": {}}\n")


class FailedCloneIsRemovedVisiblyTest(unittest.TestCase):
    """The clone-failure path: the run dir goes, the log says so, and the clone's own error is what surfaces."""

    def _review(self, tmp: Path, fake_clone, logs: list):
        config = {
            "agents": [{"id": "correctness", "prompt_path": "../agents/correctness.md"}],
            "agent_runner": {"command": ["true"]},
            "min_successful_agents": 0,
            "max_review_comments": 30,
            "work_dir": str(tmp),
            "progress_comment": {"enabled": False},
            "dashboard": {"enabled": False},
        }
        pr = {"number": 9, "head": {"sha": "abc123456789"}, "base": {"sha": ""}, "title": "t", "body": "", "user": {"login": "x"}}

        class Client:
            repo = "o/r"

            def bot_login(self):
                return "bot"

            def list_files(self, n):
                return [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0, "patch": "@@ -1 +1,2 @@\n c\n+x\n"}]

        with mock.patch.object(svc, "clone_pr_source", fake_clone), \
             mock.patch.object(svc, "log", lambda message: logs.append(str(message))):
            return svc.review_pr(Client(), config, ROOT / "config" / "reviewer.json", pr, {"reviewed": {}})

    def test_a_clone_that_fails_after_writing_is_removed_and_logged(self) -> None:
        def fake_clone(repo, pr_number, head_sha, source_dir, **kw):
            (source_dir / "pkg").mkdir(parents=True)
            (source_dir / "pkg" / "big.bin").write_bytes(b"z" * 4096)
            raise svc.ServiceError("checked out PR #9 at unexpected SHA ffff; expected abc123456789")

        logs: list = []
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(svc.ServiceError) as caught:
                self._review(Path(tmp), fake_clone, logs)
            self.assertIn("unexpected SHA", str(caught.exception))
            runs = Path(tmp) / "runs"
            self.assertEqual(sorted(runs.iterdir()) if runs.exists() else [], [], "the failed clone stayed on disk")
        removed = [line for line in logs if "removed" in line and "pr-9-abc123456789-" in line and "failed clone" in line]
        self.assertEqual(len(removed), 1, "the failed clone's removal is not in the log: %r" % logs)

    def test_a_failed_clone_deeper_than_the_recursion_limit_is_still_removed(self) -> None:
        # The daemon runs on the system python3 (3.9) whose shutil.rmtree recurses
        # once per directory level: an untrusted PR nested deeper than the
        # recursion limit made the old cleanup raise RecursionError, which then
        # replaced the clone's own error and left the clone on disk.
        depth = 300

        def fake_clone(repo, pr_number, head_sha, source_dir, **kw):
            # A loop, not mkdir(parents=True): that recurses too, under the lowered limit.
            os.mkdir(source_dir)
            cur = source_dir
            for _ in range(depth):
                cur = cur / "d"
                os.mkdir(cur)
            (cur / "leaf").write_bytes(b"k" * 100)
            raise svc.ServiceError("failed to checkout PR #9 source: boom")

        logs: list = []
        limit = sys.getrecursionlimit()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                sys.setrecursionlimit(depth // 2 + 100)
                with self.assertRaises(svc.ServiceError):
                    self._review(Path(tmp), fake_clone, logs)
            finally:
                sys.setrecursionlimit(limit)
            runs = Path(tmp) / "runs"
            leftover = sorted(runs.iterdir()) if runs.exists() else []
            self.assertEqual(leftover, [], "a deep failed clone stayed on disk")


def _run(runs: Path, name: str, with_source: bool = False) -> Path:
    run_dir = runs / name
    art = run_dir / svc.REVIEW_ARTIFACT_DIR
    art.mkdir(parents=True)
    (art / "correctness.response.json").write_text("{}", encoding="utf-8")
    if with_source:
        (run_dir / "source").mkdir()
        (run_dir / "source" / "f").write_bytes(b"s" * 256)
    return run_dir


class ClosedPrRunDirsTest(unittest.TestCase):
    """prune_review_run_dirs(closed=...): a merged or closed PR's run dirs go at once, whole."""

    def test_a_young_run_dir_of_a_closed_pr_is_removed_whole(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            runs = work / "runs"
            closed = _run(runs, "pr-5-aaaaaaaaaaaa-x1")
            other_head = _run(runs, "pr-5-999999999999-x2")  # same number, a head never seen: another repo's PR
            open_pr = _run(runs, "pr-6-cccccccccccc-x3")
            in_flight = _run(runs, "pr-7-dddddddddddd-x4", with_source=True)
            summary = svc.prune_review_run_dirs(
                work,
                {"run_dir_retention_days": 14},
                keep=lambda name: name.startswith("pr-7-"),
                closed=lambda path: path.name.startswith(("pr-5-aaaaaaaaaaaa-", "pr-7-dddddddddddd-")),
            )
            self.assertFalse(closed.exists(), "a closed PR's run dir stayed")
            self.assertTrue(other_head.exists(), "a run dir of a head never seen closed was removed")
            self.assertTrue(open_pr.exists())
            self.assertTrue((in_flight / "source" / "f").exists(), "a review in flight lost its run dir")
            self.assertEqual(summary["closed"], 1)
            self.assertEqual(summary["removed"], 0)

    def test_closed_removal_runs_with_retention_off_and_stops_with_the_debug_opt_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            runs = work / "runs"
            run_dir = _run(runs, "pr-5-aaaaaaaaaaaa-x1")
            closed = lambda path: path.name.startswith("pr-5-")  # noqa: E731
            svc.prune_review_run_dirs(work, {"run_dir_retention_days": 14, "cleanup_run_dir": False}, closed=closed)
            self.assertTrue(run_dir.exists(), "cleanup_run_dir: false (the debug opt-out) must keep it")
            svc.prune_review_run_dirs(work, {"run_dir_retention_days": 0}, closed=closed)
            self.assertFalse(run_dir.exists(), "retention 0 turns age pruning off, not the closed-PR removal")

    def test_the_log_says_how_many_closed_prs_run_dirs_went(self) -> None:
        logs: list = []
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            _run(work / "runs", "pr-5-aaaaaaaaaaaa-x1")
            with mock.patch.object(svc, "log", lambda message: logs.append(str(message))):
                svc.prune_review_run_dirs(work, {"run_dir_retention_days": 14}, closed=lambda path: True)
        self.assertTrue(any("removed 1 run dir(s) of closed PRs" in line for line in logs), logs)


class RunDirOwnerTest(unittest.TestCase):
    def test_a_run_dir_records_its_repository_in_the_artifacts_that_survive_the_strip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = svc.make_review_run_dir(Path(tmp), 5, "a" * 40, repo="Owner/Repo")
            (run_dir / "source").mkdir()
            (run_dir / "source" / "f").write_bytes(b"s" * 64)
            svc.strip_review_run_dir(run_dir)
            self.assertEqual(svc.run_dir_owner(run_dir), "owner/repo")
            self.assertEqual(svc.run_dir_owner(Path(tmp) / "missing"), "")

    def test_cleanup_still_removes_a_run_dir_that_holds_nothing_but_its_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = svc.make_review_run_dir(Path(tmp), 5, "a" * 40, repo="o/r")
            (run_dir / "source").mkdir()
            svc.cleanup_review_run_dir(run_dir, {})
            self.assertFalse(run_dir.exists(), "a review that wrote no artifact left its run dir")

    def test_the_sweep_removes_a_crashed_reviews_run_dir_that_holds_only_its_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            run_dir = svc.make_review_run_dir(work, 5, "a" * 40, repo="o/r")
            (run_dir / "source").mkdir()
            (run_dir / "source" / "f").write_bytes(b"s" * 64)  # a crash before any agent wrote
            svc.sweep_review_run_dirs(work, {"run_dir_retention_days": 14})
            self.assertFalse(run_dir.exists(), "an owner-only run dir stayed to retention")


class FullDiskEdgesTest(unittest.TestCase):
    def test_an_owner_record_that_cannot_be_written_leaves_no_run_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(svc, "write_private_text", _enospc), self.assertRaises(OSError):
                svc.make_review_run_dir(Path(tmp), 5, "a" * 40, repo="o/r")
            self.assertEqual(sorted((Path(tmp) / "runs").iterdir()), [], "a full disk left an empty run dir")

    def test_a_failed_clone_is_removed_even_when_recording_the_failure_fails(self) -> None:
        class Recorder:
            def __getattr__(self, name):
                def method(*args, **kwargs):
                    if name == "failed":
                        raise OSError(errno.ENOSPC, "database or disk is full")
                return method

        def fake_clone(repo, pr_number, head_sha, source_dir, **kw):
            (source_dir / "pkg").mkdir(parents=True)
            (source_dir / "pkg" / "big.bin").write_bytes(b"z" * 4096)
            raise svc.ServiceError("failed to clone o/r: disk full")

        config = {
            "agents": [{"id": "correctness", "prompt_path": "../agents/correctness.md"}],
            "agent_runner": {"command": ["true"]},
            "min_successful_agents": 0,
            "max_review_comments": 30,
            "progress_comment": {"enabled": False},
            "dashboard": {"enabled": False},
        }
        pr = {"number": 9, "head": {"sha": "abc123456789"}, "base": {"sha": ""}, "title": "t", "body": "", "user": {"login": "x"}}

        class Client:
            repo = "o/r"

            def bot_login(self):
                return "bot"

            def list_files(self, n):
                return [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0, "patch": "@@ -1 +1,2 @@\n c\n+x\n"}]

        with tempfile.TemporaryDirectory() as tmp:
            config["work_dir"] = tmp
            with mock.patch.object(svc, "clone_pr_source", fake_clone), mock.patch.object(svc, "log", lambda message: None), \
                 self.assertRaises(OSError):
                svc.review_pr(Client(), config, ROOT / "config" / "reviewer.json", pr, {"reviewed": {}}, recorder=Recorder())
            runs = Path(tmp) / "runs"
            self.assertEqual(sorted(runs.iterdir()) if runs.exists() else [], [], "the clone stayed when recording failed")


if __name__ == "__main__":
    unittest.main()
