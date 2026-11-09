"""A review never leaves its PR clone on disk, and old run dirs are swept."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import service as svc


def _fake_run(run_dir: Path, with_source: bool = True, artifacts: bool = True) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    if with_source:
        (run_dir / "source" / "pkg").mkdir(parents=True)
        (run_dir / "source" / "pkg" / "big.bin").write_bytes(b"x" * 4096)
    if artifacts:
        art = run_dir / svc.REVIEW_ARTIFACT_DIR
        art.mkdir(exist_ok=True)
        (art / "correctness.prompt.md").write_text("p")
        (art / ".isolated-home" / "cache").mkdir(parents=True)
        (art / ".isolated-home" / "cache" / "blob").write_bytes(b"y" * 1024)


class StripAndCleanupTest(unittest.TestCase):
    def test_strip_keeps_only_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "pr-1-abc"
            _fake_run(run_dir)
            freed = svc.strip_review_run_dir(run_dir)
            self.assertEqual(freed, 4096 + 1024)  # the clone and the isolated home; the 1-byte prompt stays
            self.assertFalse((run_dir / "source").exists())
            self.assertFalse((run_dir / svc.REVIEW_ARTIFACT_DIR / ".isolated-home").exists())
            self.assertTrue((run_dir / svc.REVIEW_ARTIFACT_DIR / "correctness.prompt.md").exists())
            self.assertEqual(svc.strip_review_run_dir(run_dir), 0)  # idempotent

    def test_strip_never_follows_symlinks_out_of_the_run_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside"
            (outside / "keep").mkdir(parents=True)
            (outside / "keep" / "file").write_bytes(b"o" * 64)
            (outside / ".isolated-x").mkdir()
            (outside / ".isolated-x" / "file").write_bytes(b"i" * 64)
            run_dir = Path(tmp) / "pr-1-abc"
            (run_dir / "source").mkdir(parents=True)
            (run_dir / "source" / "link_dir").symlink_to(outside / "keep", target_is_directory=True)
            (run_dir / "source" / "link_file").symlink_to(outside / "keep" / "file")
            (run_dir / svc.REVIEW_ARTIFACT_DIR).symlink_to(outside, target_is_directory=True)
            svc.strip_review_run_dir(run_dir)
            self.assertFalse((run_dir / "source").exists())
            self.assertTrue((outside / "keep" / "file").exists())  # link targets untouched
            self.assertTrue((outside / ".isolated-x" / "file").exists())  # symlinked artifact dir not traversed
            self.assertTrue((run_dir / svc.REVIEW_ARTIFACT_DIR).is_symlink())
            populated = Path(tmp) / "pr-3-populated"
            _fake_run(populated)
            link = Path(tmp) / "pr-2-link"
            link.symlink_to(populated, target_is_directory=True)
            self.assertEqual(svc.strip_review_run_dir(link), 0)  # a symlinked run dir is refused ...
            self.assertTrue((populated / "source" / "pkg" / "big.bin").exists())  # ... and its target is untouched
            self.assertTrue((populated / svc.REVIEW_ARTIFACT_DIR / ".isolated-home" / "cache" / "blob").exists())
            # Retention pruning of the same shape: the symlink goes, its target survives.
            stale = time.time() - 30 * 86400
            os.utime(run_dir, (stale, stale))
            with mock.patch.object(Path, "home", staticmethod(lambda: Path(tmp) / "nohome")):
                summary = svc.sweep_review_run_dirs(Path(tmp) / "work-with-no-runs", {"run_dir_retention_days": 14})
                self.assertEqual(summary, {"stripped": 0, "freed_bytes": 0, "removed": 0})
                (Path(tmp) / "work" / "runs").mkdir(parents=True)
                run_dir.rename(Path(tmp) / "work" / "runs" / "pr-1-abc")
                summary = svc.sweep_review_run_dirs(Path(tmp) / "work", {"run_dir_retention_days": 14})
            self.assertEqual(summary["removed"], 1)
            self.assertFalse((Path(tmp) / "work" / "runs" / "pr-1-abc").exists())
            self.assertTrue((outside / ".isolated-x" / "file").exists())
            self.assertTrue((outside / "keep" / "file").exists())

    def test_remove_tree_unlinks_a_symlink_instead_of_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            (target / "f").write_bytes(b"t")
            link = Path(tmp) / "link"
            link.symlink_to(target, target_is_directory=True)
            svc._remove_tree(link)
            self.assertFalse(link.is_symlink())
            self.assertTrue((target / "f").exists())

    def test_no_descriptor_leaks_when_a_scan_fails(self) -> None:
        # Fault injection: the Nth os.scandir raises as if the duplicate hit
        # EMFILE. Every directory fd opened by the walk must be closed again.
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "pr-1-abc"
            for i in range(6):
                (run_dir / "source" / f"d{i}" / "e").mkdir(parents=True)
                (run_dir / "source" / f"d{i}" / "e" / "f").write_bytes(b"x")
            real_scandir = os.scandir

            def open_fds():
                return set(os.listdir("/dev/fd"))

            for fail_at in (1, 2, 5):
                calls = {"n": 0}

                def flaky(*args, **kwargs):
                    calls["n"] += 1
                    if calls["n"] == fail_at:
                        raise OSError(24, "Too many open files")
                    return real_scandir(*args, **kwargs)

                before = open_fds()
                with mock.patch.object(svc.os, "scandir", flaky):
                    with self.assertRaises(OSError):
                        svc._rmtree_iterative(str(run_dir / "source"))
                self.assertEqual(open_fds() - before, set(), f"leaked descriptors when scandir #{fail_at} failed")
            self.assertTrue((run_dir / "source").exists())  # a failed delete leaves the rest for the next cycle

    def test_cleanup_respects_opt_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "pr-1-abc"
            _fake_run(run_dir)
            svc.cleanup_review_run_dir(run_dir, {"cleanup_run_dir": False})
            self.assertTrue((run_dir / "source").exists())
            svc.cleanup_review_run_dir(run_dir, {})
            self.assertFalse((run_dir / "source").exists())


class SweepTest(unittest.TestCase):
    """Every case runs with Path.home pointed at an empty temp home: the sweep
    also visits ~/.local/share/review-of-legends/runs, and a unit test must
    never strip or prune real run dirs on the machine it runs on (review-host holds
    the 150 GB backlog this PR exists to remove)."""

    def setUp(self) -> None:
        self._home = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(Path, "home", staticmethod(lambda: Path(self._home.name)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._home.cleanup)

    def test_sweep_strips_leftovers_and_prunes_old_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            fresh = work / "runs" / "pr-1-fresh"
            old = work / "runs" / "pr-2-old"
            clean = work / "runs" / "pr-3-clean"
            _fake_run(fresh)
            _fake_run(old)
            _fake_run(clean, with_source=False)
            stale = time.time() - 30 * 86400
            os.utime(old, (stale, stale))
            summary = svc.sweep_review_run_dirs(work, {"run_dir_retention_days": 14})
            self.assertEqual(summary["removed"], 1)
            self.assertFalse(old.exists())
            self.assertEqual(summary["stripped"], 2)  # fresh (source) + clean (isolated home only)
            self.assertFalse((fresh / "source").exists())
            self.assertTrue((fresh / svc.REVIEW_ARTIFACT_DIR / "correctness.prompt.md").exists())
            # old pruned whole (clone, home, prompt) + fresh's clone and home + clean's home
            self.assertEqual(summary["freed_bytes"], (4096 + 1024 + 1) + (4096 + 1024) + 1024)
            # second pass: nothing left to do
            self.assertEqual(svc.sweep_review_run_dirs(work, {"run_dir_retention_days": 14}), {"stripped": 0, "freed_bytes": 0, "removed": 0})

    def test_retention_zero_disables_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            old = work / "runs" / "pr-2-old"
            _fake_run(old)
            stale = time.time() - 400 * 86400
            os.utime(old, (stale, stale))
            summary = svc.sweep_review_run_dirs(work, {"run_dir_retention_days": 0})
            self.assertTrue(old.exists())  # never pruned
            self.assertFalse((old / "source").exists())  # but the clone still goes
            self.assertEqual((summary["removed"], summary["stripped"]), (0, 1))

    def test_sweep_leaves_clones_alone_when_cleanup_is_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            kept = work / "runs" / "pr-1-kept"
            old = work / "runs" / "pr-2-old"
            _fake_run(kept)
            _fake_run(old)
            stale = time.time() - 30 * 86400
            os.utime(old, (stale, stale))
            summary = svc.sweep_review_run_dirs(work, {"cleanup_run_dir": False, "run_dir_retention_days": 14})
            self.assertTrue((kept / "source" / "pkg" / "big.bin").exists())
            self.assertEqual(summary["stripped"], 0)
            self.assertFalse(old.exists())  # retention is a separate knob
            self.assertEqual(summary["removed"], 1)

    def test_one_poisoned_run_dir_does_not_stop_the_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            bad = work / "runs" / "pr-1-bad"
            good = work / "runs" / "pr-2-good"
            _fake_run(bad)
            _fake_run(good)
            real_strip = svc.strip_review_run_dir

            def strip(run_dir):
                if run_dir.name == "pr-1-bad":
                    raise RecursionError("maximum recursion depth exceeded")
                return real_strip(run_dir)

            with mock.patch.object(svc, "strip_review_run_dir", strip):
                summary = svc.sweep_review_run_dirs(work, {"run_dir_retention_days": 14})
            self.assertFalse((good / "source").exists())
            self.assertTrue((bad / "source").exists())
            self.assertEqual(summary["stripped"], 1)

    def test_failed_prune_still_strips_the_clone_and_is_not_counted_as_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            old = work / "runs" / "pr-2-old"
            _fake_run(old)
            stale = time.time() - 30 * 86400
            os.utime(old, (stale, stale))
            real = svc._rmtree_iterative

            def flaky(name, dir_fd=None):
                if dir_fd is None and Path(name) == old:
                    raise OSError("busy")
                return real(name, dir_fd)

            with mock.patch.object(svc, "_rmtree_iterative", flaky):
                summary = svc.sweep_review_run_dirs(work, {"run_dir_retention_days": 14})
            self.assertTrue(old.exists())  # prune failed ...
            self.assertFalse((old / "source").exists())  # ... but the clone is gone
            self.assertEqual(summary["removed"], 0)
            self.assertEqual(summary["freed_bytes"], 4096 + 1024)  # the clone and the isolated home, not the failed prune

    def test_legacy_home_failure_does_not_skip_the_work_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            fresh = work / "runs" / "pr-1-fresh"
            _fake_run(fresh)
            with mock.patch.object(Path, "home", staticmethod(mock.Mock(side_effect=RuntimeError("no HOME")))):
                summary = svc.sweep_review_run_dirs(work, {"run_dir_retention_days": 14, "sweep_legacy_work_dir": True})
            self.assertFalse((fresh / "source").exists())
            self.assertEqual(summary["stripped"], 1)

    def test_bad_retention_value_falls_back_to_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            old = work / "runs" / "pr-2-old"
            _fake_run(old, with_source=False)
            stale = time.time() - 30 * 86400
            os.utime(old, (stale, stale))
            summary = svc.sweep_review_run_dirs(work, {"run_dir_retention_days": "soon"})
            self.assertEqual(summary["removed"], 1)

    def test_size_walk_and_delete_survive_a_tree_deeper_than_the_recursion_limit(self) -> None:
        # The daemon's interpreter (system python3 on review-host) has a recursive
        # os.walk and shutil.rmtree; an untrusted PR can nest deeper than the
        # recursion limit. Build the tree with a loop, lower the limit below its
        # depth, and both the size walk and the delete must still work.
        # Exactly one directory fd per level may be held during the delete
        # (os.scandir(fd) duplicates the descriptor; the scan must be closed
        # before descending). With the limit raise disabled, a soft limit of
        # depth + 64 fits one fd per level and fails at two. The budget is set
        # outright (raising is allowed up to the hard limit), so the test does
        # not depend on the soft limit it started with. The capacity check
        # comes first, before any tree or limit exists that a skip would leak.
        import resource
        depth = 300
        budget = depth + 64
        nofile = resource.getrlimit(resource.RLIMIT_NOFILE)
        if nofile[1] != resource.RLIM_INFINITY and nofile[1] < budget:
            self.skipTest(f"hard RLIMIT_NOFILE {nofile[1]} < {budget}")
        with tempfile.TemporaryDirectory() as tmp:
            deep = Path(tmp) / "d"
            cur = deep
            for _ in range(depth):
                os.mkdir(cur)
                cur = cur / "d"
            (cur.parent / "leaf").write_bytes(b"k" * 100)
            limit = sys.getrecursionlimit()
            try:
                sys.setrecursionlimit(depth // 2)
                resource.setrlimit(resource.RLIMIT_NOFILE, (budget, nofile[1]))
                with mock.patch.object(svc, "_raise_open_file_limit", lambda *a, **k: None):
                    self.assertEqual(svc._tree_bytes(Path(tmp)), 100)
                    self.assertEqual(svc._remove_tree(deep), 100)
            finally:
                sys.setrecursionlimit(limit)
                resource.setrlimit(resource.RLIMIT_NOFILE, nofile)
            self.assertFalse(deep.exists())  # also keeps TemporaryDirectory teardown shallow

    def test_legacy_default_dir_is_swept_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            legacy_old = home / ".local" / "share" / "review-of-legends" / "runs" / "pr-9-old"
            _fake_run(legacy_old, with_source=False)
            stale = time.time() - 60 * 86400
            os.utime(legacy_old, (stale, stale))
            work = Path(tmp) / "work"
            with mock.patch.object(Path, "home", staticmethod(lambda: home)):
                untouched = svc.sweep_review_run_dirs(work, {"run_dir_retention_days": 14})
                self.assertEqual(untouched["removed"], 0)  # off by default: another config may own it
                self.assertTrue(legacy_old.exists())
                summary = svc.sweep_review_run_dirs(work, {"run_dir_retention_days": 14, "sweep_legacy_work_dir": True})
            self.assertEqual(summary["removed"], 1)
            self.assertFalse(legacy_old.exists())

    def test_missing_runs_dir_is_fine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(svc.sweep_review_run_dirs(Path(tmp) / "nope", {})["stripped"], 0)


class ReviewPrAlwaysCleansTest(unittest.TestCase):
    def _review(self, tmp: Path, client, agents_result):
        config = {
            "agents": [{"id": "correctness", "prompt_path": "../agents/correctness.md"}],
            "agent_runner": {"command": ["true"]},
            "min_successful_agents": 0,
            "max_review_comments": 30,
            "work_dir": str(tmp),
            "progress_comment": {"enabled": False},
            "dashboard": {"enabled": False},
        }
        pr = {"number": 7, "head": {"sha": "abc123456789"}, "base": {"sha": ""}, "title": "t", "body": "", "user": {"login": "x"}}

        def fake_clone(repo, pr_number, head_sha, run_dir, **kw):
            (run_dir / "pkg").mkdir(parents=True)
            (run_dir / "pkg" / "big.bin").write_bytes(b"z" * 2048)
            # what a real run leaves in the artifact dir (the mocked agents write nothing)
            art = run_dir.parent / svc.REVIEW_ARTIFACT_DIR
            art.mkdir(exist_ok=True)
            (art / "correctness.prompt.md").write_text("p")

        with mock.patch.object(svc, "run_agent", lambda agent, *a, **k: agents_result(agent)), \
             mock.patch.object(svc, "clone_pr_source", fake_clone):
            return svc.review_pr(client, config, ROOT / "config" / "reviewer.json", pr, {"reviewed": {}})

    def _runs(self, tmp: Path):
        return sorted((tmp / "runs").iterdir()) if (tmp / "runs").exists() else []

    def test_clone_removed_after_posted_review(self) -> None:
        class Client:
            repo = "o/r"
            def bot_login(self): return "bot"
            def list_files(self, n): return [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0, "patch": "@@ -1 +1,2 @@\n c\n+x\n"}]
            def create_review(self, *a, **k): return {"id": 1, "html_url": "u"}
            def upsert_issue_comment(self, *a, **k): return {"id": 2}
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(self._review(Path(tmp), Client(), lambda agent: {"agent": agent["id"], "summary": "s", "findings": []}))
            runs = self._runs(Path(tmp))
            self.assertEqual(len(runs), 1)
            for run_dir in runs:
                self.assertFalse((run_dir / "source").exists(), run_dir)
                self.assertTrue((run_dir / svc.REVIEW_ARTIFACT_DIR / "correctness.prompt.md").exists())

    def test_clone_removed_when_posting_raises_unexpectedly(self) -> None:
        class Client:
            repo = "o/r"
            def bot_login(self): return "bot"
            def list_files(self, n): return [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0, "patch": "@@ -1 +1,2 @@\n c\n+x\n"}]
            def create_review(self, *a, **k): raise RuntimeError("socket exploded")
            def upsert_issue_comment(self, *a, **k): return {"id": 2}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                self._review(Path(tmp), Client(), lambda agent: {"agent": agent["id"], "summary": "s", "findings": []})
            runs = self._runs(Path(tmp))
            self.assertTrue(runs)
            for run_dir in runs:
                self.assertFalse((run_dir / "source").exists(), run_dir)
                self.assertTrue((run_dir / svc.REVIEW_ARTIFACT_DIR).exists())  # artifacts kept for inspection

    def test_clone_removed_on_quorum_failure(self) -> None:
        class Client:
            repo = "o/r"
            def bot_login(self): return "bot"
            def list_files(self, n): return [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0, "patch": "@@ -1 +1,2 @@\n c\n+x\n"}]
            def create_review(self, *a, **k): raise AssertionError("must not post")
            def upsert_issue_comment(self, *a, **k): return {"id": 2}
        with tempfile.TemporaryDirectory() as tmp:
            config_override = {"min_successful_agents": 1}

            def fake_clone(repo, pr_number, head_sha, run_dir, **kw):
                (run_dir / "pkg").mkdir(parents=True)
                (run_dir / "pkg" / "b").write_bytes(b"q" * 512)
                art = run_dir.parent / svc.REVIEW_ARTIFACT_DIR
                art.mkdir(exist_ok=True)
                (art / "correctness.prompt.md").write_text("p")

            def failing(agent):
                return {"agent": agent["id"], "summary": "boom", "findings": [], "error": "exit 1"}
            with mock.patch.object(svc, "run_agent", lambda agent, *a, **k: failing(agent)), \
                 mock.patch.object(svc, "clone_pr_source", fake_clone):
                config = {"agents": [{"id": "correctness", "prompt_path": "../agents/correctness.md"}], "agent_runner": {"command": ["true"]},
                          "max_review_comments": 30, "work_dir": tmp, "progress_comment": {"enabled": False}, "dashboard": {"enabled": False}, **config_override}
                pr = {"number": 8, "head": {"sha": "abc123456789"}, "base": {"sha": ""}, "title": "t", "body": "", "user": {"login": "x"}}
                self.assertFalse(svc.review_pr(Client(), config, ROOT / "config" / "reviewer.json", pr, {"reviewed": {}}))
            runs = self._runs(Path(tmp))
            self.assertEqual(len(runs), 1)
            for run_dir in runs:
                self.assertFalse((run_dir / "source").exists(), run_dir)
                self.assertTrue((run_dir / svc.REVIEW_ARTIFACT_DIR / "correctness.prompt.md").exists())  # kept for inspection


if __name__ == "__main__":
    unittest.main()
