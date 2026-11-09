"""Executor integration for the admin dashboard (W2 of .prd/admin-dashboard/loop.md).

What is under test here is not the store (W1 owns that) but the *executor's*
behavior around it:

- exactly one review executor: an OS advisory lock held from before the state
  file is read until after the last write, failing non-blocking and loudly;
- durable attempt lifecycle with a truthful terminal state: ``succeeded`` only
  after a confirmed GitHub review POST, failures recorded even when the clone
  never finished;
- duplicate-POST safety: an attempt whose POST outcome is unknown (transport
  error, crash while posting) is never re-posted by the poll loop nor by an
  operator retry, and a moved/closed head is stale rather than reviewed;
- retry requests consumed serially by the locked executor only, waking a
  sleeping daemon within a second;
- legacy ledger imported with honest provenance (baseline entries were never
  reviewed).

``FakeStore`` is a double for the W1 store *surface*, not a replacement store:
these properties are about the calls the executor makes and the work it refuses
to do, which a real SQLite file would not show any better. Durability,
transactions and concurrency of the store itself are tests/test_dashboard_store.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import code_review_service
from code_review_service import dashboard_execution as dx
from code_review_service import service as svc
from code_review_service.dashboard_store import DashboardStore


class FakeStore:
    """In-memory stand-in for the W1 ``DashboardStore`` surface W2 calls."""

    def __init__(self) -> None:
        self.runs: dict = {}
        self.requests: dict = {}
        self.events: list = []
        self.imported: list = []
        self.order: list = []
        self._next_id = 0
        # Failure injection for the fail-open / fail-closed distinction.
        self.fail_start_run = False
        self.fail_update_stages: set = set()
        self.recovered = 0
        # Per instance: a setting one test stores must not leak into the next.
        self.settings: dict = {}
        self.fail_settings = False

    # -- events ---------------------------------------------------------
    def _emit(self, kind: str, run_id, payload) -> None:
        self._next_id += 1
        self.events.append(
            {
                "id": self._next_id,
                "kind": kind,
                "run_id": run_id,
                "payload": payload,
                "created_at": "2026-09-11T00:00:00+00:00",
            }
        )

    def events_after(self, cursor: int = 0, limit: int = 100) -> list:
        return [event for event in self.events if event["id"] > max(0, int(cursor))][: max(1, int(limit))]

    # -- settings -------------------------------------------------------
    def get_setting(self, key):
        if self.fail_settings:
            raise RuntimeError("settings unavailable")
        value = self.settings.get(key)
        return None if value is None else {"key": key, "value": value, "updated_at": "t", "updated_by": "z"}

    def reconcile_agent_setting(self, agent_id, read_default, reconcile):
        """W1 contract: a changed prompt file replaces the stored prompt once (single-threaded here)."""
        if self.fail_settings:
            raise RuntimeError("settings unavailable")
        row = self.get_setting("agent:" + agent_id)
        if row is None:
            return None, None
        default_text = read_default()
        if not isinstance(default_text, str):
            return row, None
        try:
            value, action = reconcile(row["value"], default_text, "2026-09-11T00:00:00+00:00", row["updated_at"], row["updated_by"])
        except ValueError:
            return row, None
        if action is None:
            return row, None
        if value:
            self.settings["agent:" + agent_id] = value
        else:
            self.settings.pop("agent:" + agent_id, None)
        if action == "superseded":
            self._emit("agent_prompt_superseded", None, {"key": "agent:" + agent_id, "agent_id": agent_id})
        return self.get_setting("agent:" + agent_id), action

    # -- retry queue ----------------------------------------------------
    def enqueue_retry(self, repo, pr, head_sha, requested_by, retry_of=None, options=None) -> dict:
        request = {
            "id": "req-%d" % (len(self.requests) + 1),
            "status": "queued",
            "repo": repo,
            "pr": int(pr),
            "head_sha": head_sha,
            "requested_by": requested_by,
            "retry_of": retry_of,
            "options": options,
            "run_id": None,
            "error": None,
            "created_at": "2026-09-11T00:00:00+00:00",
        }
        self.requests[request["id"]] = request
        self._emit("retry_enqueued", None, {"request_id": request["id"]})
        return dict(request)

    def claim_retry(self):
        for request in self.requests.values():
            if request["status"] == "queued":
                request["status"] = "running"
                self._emit("retry_claimed", None, {"request_id": request["id"]})
                return dict(request)
        return None

    def requeue_retry(self, request_id) -> bool:
        request = self.requests[str(request_id)]
        if request["status"] != "running":
            return False
        if request["run_id"]:
            raise ValueError("request %s already has an attempt" % request_id)
        request["status"] = "queued"
        self.order.append(("requeue_retry", request_id))
        self._emit("retry_enqueued", None, {"request_id": request_id, "status": "queued", "requeued": True})
        return True

    def finish_retry(self, request_id, status, run_id=None, error=None) -> None:
        request = self.requests[str(request_id)]
        if request["status"] != "running":
            raise ValueError("request %s is %s, not running" % (request_id, request["status"]))
        request["status"] = status
        request["error"] = error
        if run_id:
            request["run_id"] = run_id
        self.order.append(("finish_retry", request_id, status))
        self._emit("retry_finished", run_id, {"request_id": request_id, "status": status})

    def retry_status(self, request_id):
        request = self.requests.get(str(request_id))
        return None if request is None else request["status"]

    def get_retry(self, request_id):
        request = self.requests.get(str(request_id))
        return None if request is None else dict(request)

    def list_retries(self, status=None, repo=None, limit=50, offset=0) -> list:
        rows = [dict(request) for request in self.requests.values()]
        if status is not None:
            rows = [row for row in rows if row["status"] == status]
        if repo is not None:
            rows = [row for row in rows if row["repo"] == repo]
        return list(reversed(rows))[: max(1, int(limit))]

    # -- runs -----------------------------------------------------------
    def start_run(self, repo, pr, head_sha, title="", url="", trigger="poll", retry_of=None, request_id=None):
        if self.fail_start_run:
            raise RuntimeError("store is down")
        run_id = "run-%d" % (len(self.runs) + 1)
        run = {
            "id": run_id,
            "repo": repo,
            "pr": int(pr),
            "head_sha": head_sha,
            "title": title,
            "url": url,
            "trigger": trigger,
            "retry_of": retry_of,
            "request_id": request_id,
            "status": "running",
            "stage": "",
            "agents": [],
            "review_url": "",
            "error": "",
            "successful_agents": None,
            "total_agents": None,
        }
        self.runs[run_id] = run
        self.order.append(("start_run", trigger, request_id))
        self._emit("run_started", run_id, {"repo": repo, "pr": int(pr), "trigger": trigger})
        if request_id is not None:
            request = self.requests[str(request_id)]
            if request["status"] != "running":
                raise ValueError("request %s is not claimed" % request_id)
            request["run_id"] = run_id
        return dict(run)

    def update_run(self, run_id, status, **fields) -> None:
        run = self.runs[str(run_id)]
        if fields.get("stage") in self.fail_update_stages:
            raise RuntimeError("store is down for stage %s" % fields.get("stage"))
        if run["status"] in dx.TERMINAL_RUN_STATUSES and status != run["status"]:
            raise ValueError("terminal run cannot become %s" % status)
        run["status"] = status
        run.update(fields)
        self.order.append(("update_run", status, fields.get("stage", "")))
        self._emit("run_updated", run_id, {"status": status, "stage": fields.get("stage", "")})

    def get_run(self, run_id):
        run = self.runs.get(str(run_id))
        return None if run is None else dict(run)

    def get_agent_results(self, run_id) -> dict:
        # update_run keeps fields as given; the real store bounds them first.
        run = self.runs.get(str(run_id)) or {}
        return {item["agent"]: item for item in run.get("agent_results") or []}

    def list_runs(self, status=None, repo=None, limit=50, offset=0) -> list:
        return [dict(run) for run in reversed(list(self.runs.values()))]

    def latest_attempt_for_head(self, repo, pr, head_sha):
        """W1 contract: the single newest attempt for (repo, pr, head)."""
        matching = [
            run for run in self.runs.values()
            if run["repo"] == repo and run["pr"] == int(pr) and run["head_sha"] == head_sha
        ]
        return dict(matching[-1]) if matching else None

    def recover_interrupted(self) -> None:
        self.recovered += 1

    def import_run(self, run_id, repo, pr, head_sha, **fields) -> dict:
        for existing in self.imported:
            if existing["id"] == run_id:
                return dict(existing)
        row = {"id": run_id, "repo": repo, "pr": int(pr), "head_sha": head_sha}
        row.update(fields)
        self.imported.append(row)
        return dict(row)

    # -- helpers for assertions ----------------------------------------
    def stages(self) -> list:
        return [entry[2] for entry in self.order if entry[0] == "update_run"]

    def statuses(self) -> list:
        return [entry[1] for entry in self.order if entry[0] == "update_run"]


class FakeClient:
    """GitHub double: never touches the network."""

    def __init__(self, repo: str = "owner/repo", pr_payload=None) -> None:
        self.repo = repo
        self.create_review_calls = 0
        self.review_error = None
        self.pr_payload = pr_payload or {
            "number": 1,
            "state": "open",
            "head": {"sha": "abc1234567"},
            "title": "t",
            "html_url": "https://github.invalid/owner/repo/pull/1",
            "body": "",
        }

    def get_pr(self, number):
        return dict(self.pr_payload)

    def list_files(self, number):
        return [
            {
                "filename": "a.py",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
                "changes": 1,
                "patch": "@@ -1,1 +1,2 @@\n context\n+added\n",
            }
        ]

    def create_review(self, number, commit_id, body, comments, event="COMMENT"):
        self.create_review_calls += 1
        if self.review_error is not None:
            raise self.review_error
        return {"id": 999, "html_url": "https://github.invalid/owner/repo/pull/1#pullrequestreview-999"}

    def upsert_issue_comment(self, number, marker, body):
        # Progress comments are enabled by default; they are not under test here.
        return {"id": 998}

    def create_commit_status(self, sha, state, description, target_url="", context=svc.REQUIRED_STATUS_CONTEXT):
        # Every repository gets the reviewed status; it is not under test here.
        return {"id": 997}


def _config(work_dir: Path, min_successful: int = 0) -> dict:
    return {
        "repositories": ["owner/repo"],
        "agents": [{"id": "fake", "enabled": True, "prompt_path": "../agents/correctness.md"}],
        "agent_runner": {"command": ["true"]},
        "min_successful_agents": min_successful,
        "max_review_comments": 30,
        "max_diff_chars": 180000,
        "work_dir": str(work_dir),
        "marker": svc.MARKER,
        "summary_marker": svc.SUMMARY_MARKER,
        "review_existing_open_prs_on_first_run": True,
        "executive_summary": {"enabled": False},
    }


def _stub_agent(error: str = ""):
    result = {"agent": "fake", "summary": "ok", "findings": []}
    if error:
        result["error"] = error
    return mock.patch.object(svc, "run_agent", lambda *args, **kwargs: dict(result))


def _stub_clone():
    return mock.patch.object(
        svc,
        "clone_pr_source",
        lambda repo, pr_number, head_sha, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True),
    )


def _http_error(code: int) -> svc.ServiceError:
    error = svc.ServiceError("GitHub API POST failed: %d boom" % code)
    error.status_code = code
    return error


def _transport_error() -> svc.ServiceError:
    error = svc.ServiceError("GitHub API POST failed: <urlopen error timed out>")
    error.transport_error = True
    return error


class ExecutorLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "data" / "executor.lock"

    def test_second_holder_fails_nonblocking_and_names_the_lock(self) -> None:
        first = dx.ExecutorLock(self.path)
        first.acquire()
        self.addCleanup(first.release)
        second = dx.ExecutorLock(self.path)
        with self.assertRaises(dx.ExecutorBusy) as caught:
            second.acquire()
        self.assertIn(str(self.path), str(caught.exception))
        self.assertFalse(second.held)

    def test_release_frees_the_lock_and_context_manager_releases_on_error(self) -> None:
        with self.assertRaises(RuntimeError):
            with dx.ExecutorLock(self.path):
                raise RuntimeError("boom")
        # The lock must be reusable after a failure inside the block.
        with dx.ExecutorLock(self.path) as lock:
            self.assertTrue(lock.held)
        self.assertIsNone(dx.probe_executor_lock(self.path))

    def test_probe_reports_the_holder_without_taking_the_lock(self) -> None:
        self.assertIsNone(dx.probe_executor_lock(self.path))
        lock = dx.ExecutorLock(self.path)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertEqual(dx.probe_executor_lock(self.path), os.getpid())
        # Probing must not have stolen or released the lock.
        self.assertTrue(lock.held)
        with self.assertRaises(dx.ExecutorBusy):
            dx.ExecutorLock(self.path).acquire()

    def test_lock_storage_is_private(self) -> None:
        lock = dx.ExecutorLock(self.path)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_require_held_refuses_work_without_the_lock(self) -> None:
        lock = dx.ExecutorLock(self.path)
        with self.assertRaises(RuntimeError):
            lock.require_held("consuming retry requests")


class RunOnceLockingTest(unittest.TestCase):
    """``run_once`` must serialize state load -> writes against other executors."""

    def _run(self, tmp_path: Path, client_factory) -> int:
        config = _config(tmp_path / "work")
        config["state_path"] = str(tmp_path / "state.json")
        with mock.patch.object(svc, "GitHubClient", client_factory), mock.patch.object(
            svc, "load_config", return_value=config
        ), mock.patch.object(svc, "expand_path", side_effect=lambda v, base=None: Path(str(v))):
            return svc.run_once(
                argparse.Namespace(
                    config=str(ROOT / "config" / "reviewer.json"),
                    repo=None,
                    pr=None,
                    dry_run=False,
                    force=False,
                )
            )

    def test_busy_executor_fails_clearly_and_does_not_poll(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            holder = dx.ExecutorLock(tmp_path / "executor.lock")
            holder.acquire()
            self.addCleanup(holder.release)
            polled = []

            class Client:
                def __init__(self, repo, gh_bin="gh"):
                    polled.append(repo)
                    self.repo = repo

                def list_open_prs(self):
                    return []

            with self.assertRaises(svc.ServiceError) as caught:
                self._run(tmp_path, Client)
            self.assertIn("executor", str(caught.exception))
            self.assertEqual(polled, [], "a busy executor must not poll GitHub")
            self.assertFalse((tmp_path / "state.json").exists())

    def test_lock_is_held_while_polling_and_released_afterwards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            observed = []

            class Client:
                def __init__(self, repo, gh_bin="gh"):
                    self.repo = repo

                def list_open_prs(self):
                    # Mid-cycle: a second executor must be locked out.
                    try:
                        dx.ExecutorLock(tmp_path / "executor.lock").acquire()
                    except dx.ExecutorBusy:
                        observed.append("locked")
                    else:  # pragma: no cover - would be a locking bug
                        observed.append("NOT LOCKED")
                    return []

            self.assertEqual(self._run(tmp_path, Client), 0)
            self.assertEqual(observed, ["locked"])
            # Released on the way out, so the next cycle can run.
            self.assertIsNone(dx.probe_executor_lock(tmp_path / "executor.lock"))

    def test_unrecorded_review_crash_is_closed_as_failed_by_the_cycle(self) -> None:
        # Codex P2 on PR #12, generalized: whatever escapes review_pr after
        # begin() must not leave the attempt "running" until the next restart.
        store = FakeStore()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            class Client:
                def __init__(self, repo, gh_bin="gh"):
                    self.repo = repo

                def list_open_prs(self):
                    return [dict(FakeClient().pr_payload)]

            def crashing_review(client, config, config_path, pr, state, **kwargs):
                kwargs["recorder"].begin(client.repo, int(pr["number"]), pr["head"]["sha"])
                raise RuntimeError("boom after begin")

            with mock.patch.object(dx, "open_store", return_value=store), mock.patch.object(
                svc, "review_pr", crashing_review
            ):
                self.assertEqual(self._run(tmp_path, Client), 0)
        run = store.get_run("run-1")
        self.assertEqual(run["status"], "failed")
        self.assertIn("review raised", run["error"])

    def test_cycle_guard_never_rewrites_a_terminal_attempt(self) -> None:
        # The poll-loop guard must be a no-op once the attempt is terminal:
        # a posted review stays succeeded with its URL, an earlier failure
        # keeps its original error (DashboardRecorder._update terminal guard).
        for terminal, raiser in (("succeeded", RuntimeError), ("failed", svc.ServiceError)):
            with self.subTest(terminal=terminal):
                store = FakeStore()
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)

                    class Client:
                        def __init__(self, repo, gh_bin="gh"):
                            self.repo = repo

                        def list_open_prs(self):
                            return [dict(FakeClient().pr_payload)]

                    def review_then_crash(client, config, config_path, pr, state, **kwargs):
                        recorder = kwargs["recorder"]
                        recorder.begin(client.repo, int(pr["number"]), pr["head"]["sha"])
                        if terminal == "succeeded":
                            recorder.succeeded("https://github.invalid/r/1")
                        else:
                            recorder.failed("original failure")
                        raise raiser("boom after terminal")

                    with mock.patch.object(dx, "open_store", return_value=store), mock.patch.object(
                        svc, "review_pr", review_then_crash
                    ):
                        self.assertEqual(self._run(tmp_path, Client), 0)
                run = store.get_run("run-1")
                self.assertEqual(run["status"], terminal)
                if terminal == "succeeded":
                    self.assertEqual(run["review_url"], "https://github.invalid/r/1")
                else:
                    self.assertEqual(run["error"], "original failure")

    def test_pause_sentinel_stops_the_cycle_before_polling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "executor.pause").write_text("deploy\n", encoding="utf-8")
            polled = []

            class Client:
                def __init__(self, repo, gh_bin="gh"):
                    polled.append(repo)
                    self.repo = repo

                def list_open_prs(self):  # pragma: no cover - must not be reached
                    return []

            self.assertEqual(self._run(tmp_path, Client), 0)
            self.assertEqual(polled, [], "a paused executor takes no new work")


class PollLifecycleTest(unittest.TestCase):
    """Durable lifecycle for ordinary poll attempts."""

    def _review(self, store, config_overrides=None, client=None, dry_run=False, state=None):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = _config(tmp_path / "work")
            config.update(config_overrides or {})
            state_path = tmp_path / "state.json"
            client = client or FakeClient()
            state = state if state is not None else {"reviewed": {}}
            recorder = dx.DashboardRecorder(store, trigger="poll")
            posted = svc.review_pr(
                client,
                config,
                ROOT / "config" / "reviewer.json",
                dict(client.pr_payload),
                state,
                dry_run=dry_run,
                state_path=state_path,
                recorder=recorder,
            )
            on_disk = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            return posted, recorder, on_disk

    def test_successful_review_records_stages_and_captures_the_review_url(self) -> None:
        store = FakeStore()
        with _stub_agent(), _stub_clone():
            posted, recorder, on_disk = self._review(store)
        self.assertTrue(posted)
        run = store.get_run(recorder.run_id)
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["trigger"], "poll")
        self.assertEqual(
            run["review_url"], "https://github.invalid/owner/repo/pull/1#pullrequestreview-999"
        )
        self.assertEqual(run["agents"], [{"id": "fake", "status": "done", "summary": "ok"}])
        self.assertIn("fetching", store.stages())
        self.assertIn("cloning", store.stages())
        self.assertIn("posting", store.stages())
        # succeeded is recorded only after the POST, never before.
        self.assertEqual(store.statuses()[-1], "succeeded")
        self.assertEqual(store.statuses().count("succeeded"), 1)
        self.assertEqual(on_disk["reviewed"]["1"]["head_sha"], "abc1234567")
        # The rereview late check orders a review against a page snapshot to the
        # microsecond; reviewed_at keeps its whole-second shape for text ordering.
        exact = dt.datetime.fromisoformat(on_disk["reviewed"]["1"]["reviewed_at_exact"])
        self.assertIsNotNone(exact.tzinfo)
        self.assertEqual(on_disk["reviewed"]["1"]["reviewed_at"], exact.replace(microsecond=0).isoformat())

    def test_quorum_failure_is_recorded_as_failed(self) -> None:
        store = FakeStore()
        client = FakeClient()
        with _stub_agent(error="agent exploded"), _stub_clone():
            posted, recorder, on_disk = self._review(
                store, config_overrides={"min_successful_agents": 1}, client=client
            )
        self.assertFalse(posted)
        run = store.get_run(recorder.run_id)
        self.assertEqual(run["status"], "failed")
        self.assertIn("agents succeeded", run["error"])
        self.assertEqual(run["successful_agents"], 0)
        self.assertEqual(run["total_agents"], 1)
        self.assertEqual(client.create_review_calls, 0)
        self.assertNotIn("succeeded", store.statuses())
        # Never recorded as reviewed; only counted for the poll loop's retry limit.
        self.assertEqual(on_disk["reviewed"], {})
        self.assertEqual(
            (on_disk["failed"]["1"]["head_sha"], on_disk["failed"]["1"]["attempts"], on_disk["failed"]["1"]["stage"]),
            ("abc1234567", 1, "quorum"),
        )

    def test_clone_failure_is_recorded_before_unwinding(self) -> None:
        store = FakeStore()

        def exploding_clone(repo, pr_number, head_sha, run_dir, **kwargs):
            run_dir.mkdir(parents=True, exist_ok=True)
            raise svc.ServiceError("clone exploded")

        with _stub_agent(), mock.patch.object(svc, "clone_pr_source", exploding_clone):
            with self.assertRaises(svc.ServiceError):
                self._review(store)
        run = store.get_run("run-1")
        self.assertEqual(run["status"], "failed")
        self.assertIn("clone failed", run["error"])

    def test_review_records_the_run_directory_name_as_execution_identity(self) -> None:
        store = FakeStore()
        with _stub_agent(), _stub_clone():
            self._review(store)
        run = store.get_run("run-1")
        self.assertRegex(run.get("run_dir") or "", r"^pr-1-abc1234567-")
        self.assertNotIn("/", run["run_dir"], "a name, never a path")

    def test_fetch_failure_is_recorded_before_unwinding(self) -> None:
        # Codex P2 on PR #12: list_files raising after begin() left the attempt
        # "running" forever (recovery only runs at daemon start).
        store = FakeStore()
        client = FakeClient()

        def exploding_files(number):
            raise svc.ServiceError("files exploded")

        client.list_files = exploding_files
        with _stub_agent(), _stub_clone():
            with self.assertRaises(svc.ServiceError):
                self._review(store, client=client)
        run = store.get_run("run-1")
        self.assertEqual(run["status"], "failed")
        self.assertIn("fetch failed", run["error"])

    def test_transport_error_parks_the_head_unverified_and_blocks_reuse(self) -> None:
        store = FakeStore()
        client = FakeClient()
        client.review_error = _transport_error()
        with _stub_agent(), _stub_clone():
            with self.assertRaises(svc.ServiceError):
                self._review(store, client=client)
        run = store.get_run("run-1")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["stage"], dx.POSTING_UNVERIFIED_STAGE)
        self.assertTrue(dx.posting_outcome_unknown(run))
        # An unverified POST must not be re-posted by an operator retry either.
        decision = dx.retry_decision({"head_sha": "abc1234567"}, run, None)
        self.assertFalse(decision.allow)
        self.assertIn("unknown", decision.reason)

    def test_transport_error_writes_an_unverified_ledger_entry_that_stops_the_next_poll(self) -> None:
        store = FakeStore()
        client = FakeClient()
        client.review_error = _transport_error()
        state = {"reviewed": {}}
        with _stub_agent(), _stub_clone():
            with self.assertRaises(svc.ServiceError):
                self._review(store, client=client, state=state)
        entry = state["reviewed"]["1"]
        self.assertTrue(entry["post_outcome_unknown"])
        self.assertEqual(entry["head_sha"], "abc1234567")
        # The next poll short-circuits on the parked head: no second POST.
        client.review_error = None
        with _stub_agent(), _stub_clone():
            posted, _recorder, _on_disk = self._review(store, client=client, state=state)
        self.assertFalse(posted)
        self.assertEqual(client.create_review_calls, 1)

    def test_rejected_post_is_an_ordinary_failure_and_parks_nothing(self) -> None:
        store = FakeStore()
        client = FakeClient()
        client.review_error = _http_error(422)
        state = {"reviewed": {}}
        with _stub_agent(), _stub_clone():
            with self.assertRaises(svc.ServiceError):
                self._review(store, client=client, state=state)
        run = store.get_run("run-1")
        self.assertEqual(run["stage"], dx.POSTING_REJECTED_STAGE)
        self.assertFalse(dx.posting_outcome_unknown(run))
        self.assertEqual(state["reviewed"], {}, "a rejected POST created no review to protect")
        self.assertTrue(dx.retry_decision({"head_sha": "abc1234567"}, run, None).allow)

    def test_dry_run_creates_no_attempt(self) -> None:
        store = FakeStore()
        with _stub_agent(), _stub_clone():
            posted, recorder, _on_disk = self._review(store, dry_run=True)
        self.assertTrue(posted)
        self.assertIsNone(recorder.run_id)
        self.assertEqual(store.runs, {})
        self.assertEqual(store.events, [])

    def test_store_failure_degrades_a_poll_review_without_stopping_it(self) -> None:
        store = FakeStore()
        store.fail_start_run = True
        client = FakeClient()
        with _stub_agent(), _stub_clone():
            posted, recorder, on_disk = self._review(store, client=client)
        self.assertTrue(posted, "a broken dashboard must not stop the review")
        self.assertTrue(recorder.degraded, "degradation must be surfaced, not silent")
        self.assertIsNone(recorder.run_id)
        self.assertEqual(client.create_review_calls, 1)
        self.assertEqual(on_disk["reviewed"]["1"]["head_sha"], "abc1234567")


class StrictRetryFailClosedTest(unittest.TestCase):
    """An operator retry is traceable or it does not happen."""

    def test_missing_durable_attempt_stops_a_retry_before_any_work(self) -> None:
        store = FakeStore()
        store.fail_start_run = True
        recorder = dx.DashboardRecorder(store, trigger="retry", request_id="req-1", strict=True)
        with self.assertRaises(dx.DashboardUnavailable):
            recorder.begin("owner/repo", 1, "abc1234567")
        self.assertTrue(recorder.degraded)

    def test_absent_store_stops_a_retry_but_not_a_poll(self) -> None:
        with self.assertRaises(dx.DashboardUnavailable):
            dx.DashboardRecorder(None, trigger="retry", strict=True).begin("owner/repo", 1, "abc")
        self.assertIsNone(dx.DashboardRecorder(None).begin("owner/repo", 1, "abc"))

    def test_failed_posting_intent_blocks_the_post_on_a_retry(self) -> None:
        store = FakeStore()
        store.fail_update_stages = {"posting"}
        client = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recorder = dx.DashboardRecorder(store, trigger="retry", strict=True)
            with _stub_agent(), _stub_clone():
                with self.assertRaises(dx.DashboardUnavailable):
                    svc.review_pr(
                        client,
                        _config(tmp_path / "work"),
                        ROOT / "config" / "reviewer.json",
                        dict(client.pr_payload),
                        {"reviewed": {}},
                        state_path=tmp_path / "state.json",
                        recorder=recorder,
                    )
        self.assertEqual(client.create_review_calls, 0, "no untracked POST may happen")

    def test_failed_posting_intent_does_not_block_a_poll_post(self) -> None:
        store = FakeStore()
        store.fail_update_stages = {"posting"}
        client = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recorder = dx.DashboardRecorder(store, trigger="poll")
            with _stub_agent(), _stub_clone():
                posted = svc.review_pr(
                    client,
                    _config(tmp_path / "work"),
                    ROOT / "config" / "reviewer.json",
                    dict(client.pr_payload),
                    {"reviewed": {}},
                    state_path=tmp_path / "state.json",
                    recorder=recorder,
                )
        self.assertTrue(posted)
        self.assertEqual(client.create_review_calls, 1)
        self.assertTrue(recorder.degraded)


class RetryDecisionTest(unittest.TestCase):
    """Pure rules for "may this request execute, and may it force a review?"."""

    request = {"id": "req-1", "repo": "owner/repo", "pr": 1, "head_sha": "abc1234567"}

    def test_failed_attempt_on_the_same_head_is_retryable_without_force(self) -> None:
        previous = {"id": "run-1", "status": "failed", "head_sha": "abc1234567", "stage": "agents"}
        decision = dx.retry_decision(self.request, previous, None)
        self.assertTrue(decision.allow)
        self.assertFalse(decision.force)

    def test_successful_attempt_is_never_retried(self) -> None:
        previous = {"id": "run-1", "status": "succeeded", "head_sha": "abc1234567"}
        decision = dx.retry_decision(self.request, previous, None)
        self.assertFalse(decision.allow)
        self.assertEqual(decision.status, "failed")

    def test_running_attempt_is_not_retried(self) -> None:
        previous = {"id": "run-1", "status": "running", "head_sha": "abc1234567"}
        self.assertFalse(dx.retry_decision(self.request, previous, None).allow)

    def test_posting_uncertain_attempts_are_never_retried(self) -> None:
        for stage in (dx.POSTING_STAGE, dx.POSTING_UNVERIFIED_STAGE, "summary"):
            previous = {"id": "run-1", "status": "interrupted", "head_sha": "abc1234567", "stage": stage}
            decision = dx.retry_decision(self.request, previous, None)
            self.assertFalse(decision.allow, stage)
            self.assertIn("unknown", decision.reason)
        with_url = {
            "id": "run-1",
            "status": "failed",
            "head_sha": "abc1234567",
            "stage": "agents",
            "review_url": "https://github.invalid/r/1",
        }
        self.assertFalse(dx.retry_decision(self.request, with_url, None).allow)

    def test_moved_head_is_stale(self) -> None:
        previous = {"id": "run-1", "status": "failed", "head_sha": "0000000000", "stage": "agents"}
        decision = dx.retry_decision(self.request, previous, None)
        self.assertFalse(decision.allow)
        self.assertEqual(decision.status, "stale")

    def test_reviewed_ledger_head_blocks_a_duplicate_review(self) -> None:
        previous = {"id": "run-1", "status": "failed", "head_sha": "abc1234567", "stage": "agents"}
        ledger = {"head_sha": "abc1234567", "review_event": "COMMENT"}
        decision = dx.retry_decision(self.request, previous, ledger)
        self.assertFalse(decision.allow)
        self.assertIn("already recorded as reviewed", decision.reason)

    def test_unverified_ledger_head_blocks_a_retry(self) -> None:
        previous = {"id": "run-1", "status": "failed", "head_sha": "abc1234567", "stage": "agents"}
        ledger = {"head_sha": "abc1234567", "post_outcome_unknown": True}
        decision = dx.retry_decision(self.request, previous, ledger)
        self.assertFalse(decision.allow)
        self.assertIn("unverified", decision.reason)

    def test_baseline_ledger_head_allows_a_forced_first_review(self) -> None:
        previous = {"id": "run-1", "status": "failed", "head_sha": "abc1234567", "stage": "agents"}
        ledger = {"head_sha": "abc1234567", "initial_baseline_without_review": True}
        decision = dx.retry_decision(self.request, previous, ledger)
        self.assertTrue(decision.allow)
        self.assertTrue(decision.force, "a baseline head was never reviewed")

    def test_baseline_head_without_a_failed_attempt_is_not_forced(self) -> None:
        ledger = {"head_sha": "abc1234567", "initial_baseline_without_review": True}
        decision = dx.retry_decision(self.request, None, ledger)
        self.assertFalse(decision.allow)
        self.assertIn("no failed attempt", decision.reason)


class RetryDrainTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = FakeStore()
        self.lock = dx.ExecutorLock(Path(self._tmp.name) / "executor.lock")
        self.lock.acquire()
        self.addCleanup(self.lock.release)
        self.client = FakeClient()
        self.reviewed: list = []

    def _drain(self, review_runner=None, ledger=None, **kwargs) -> int:
        def runner(client, pr, recorder=None, force=False):
            self.reviewed.append((client.repo, pr["number"], force))
            recorder.begin(client.repo, pr["number"], pr["head"]["sha"])
            recorder.succeeded(review_url="https://github.invalid/r/1")
            return True

        return dx.drain_retry_requests(
            self.store,
            self.lock,
            ["owner/repo"],
            lambda repo: self.client,
            review_runner or runner,
            ledger or (lambda repo, pr: None),
            logger=lambda message: None,
            **kwargs,
        )

    def test_drain_refuses_to_run_without_the_executor_lock(self) -> None:
        self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")
        unheld = dx.ExecutorLock(Path(self._tmp.name) / "other.lock")
        with self.assertRaises(RuntimeError):
            dx.drain_retry_requests(
                self.store,
                unheld,
                ["owner/repo"],
                lambda repo: self.client,
                lambda *a, **kw: True,
                lambda repo, pr: None,
            )
        self.assertEqual(self.reviewed, [])

    def test_successful_retry_links_the_run_to_the_request(self) -> None:
        request = self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")
        self.assertEqual(self._drain(), 1)
        self.assertEqual(self.store.retry_status(request["id"]), "succeeded")
        self.assertEqual(self.store.requests[request["id"]]["run_id"], "run-1")
        self.assertEqual(self.reviewed, [("owner/repo", 1, False)])

    def test_requests_are_consumed_serially_until_the_queue_is_empty(self) -> None:
        first = self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")
        second = self.store.enqueue_retry("owner/repo", 2, "bbb2222222", "owner@company.example")
        self.client.pr_payload = {
            "number": 1,
            "state": "open",
            "head": {"sha": "abc1234567"},
            "title": "t",
            "html_url": "",
        }

        def runner(client, pr, recorder=None, force=False):
            self.reviewed.append(pr["number"])
            recorder.begin(client.repo, pr["number"], pr["head"]["sha"])
            recorder.succeeded()
            return True

        # The live head for #2 differs from the request, so it is stale, not a
        # review: what matters here is that both requests were handled in order.
        self.assertEqual(self._drain(review_runner=runner), 2)
        self.assertEqual(self.store.retry_status(first["id"]), "succeeded")
        self.assertEqual(self.store.retry_status(second["id"]), "stale")

    def test_closed_pull_request_is_stale_and_never_reviewed(self) -> None:
        request = self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")
        self.client.pr_payload = dict(self.client.pr_payload, state="closed")
        self.assertEqual(self._drain(), 1)
        self.assertEqual(self.store.retry_status(request["id"]), "stale")
        self.assertEqual(self.reviewed, [])
        self.assertEqual(self.client.create_review_calls, 0)

    def test_moved_head_is_stale_and_never_reviewed(self) -> None:
        request = self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")
        self.client.pr_payload = dict(self.client.pr_payload, head={"sha": "ffffffffff"})
        self.assertEqual(self._drain(), 1)
        self.assertEqual(self.store.retry_status(request["id"]), "stale")
        self.assertIn("head moved", self.store.requests[request["id"]]["error"])
        self.assertEqual(self.reviewed, [])

    def test_unconfigured_repository_request_fails_without_a_client(self) -> None:
        request = self.store.enqueue_retry("other/repo", 1, "abc1234567", "owner@company.example")
        self.assertEqual(self._drain(), 1)
        self.assertEqual(self.store.retry_status(request["id"]), "failed")
        self.assertIn("not configured", self.store.requests[request["id"]]["error"])
        self.assertEqual(self.reviewed, [])

    def test_reviewed_head_is_refused_without_touching_github(self) -> None:
        request = self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")
        self.assertEqual(
            self._drain(ledger=lambda repo, pr: {"head_sha": "abc1234567", "review_event": "COMMENT"}),
            1,
        )
        self.assertEqual(self.store.retry_status(request["id"]), "failed")
        self.assertIn("already recorded as reviewed", self.store.requests[request["id"]]["error"])
        self.assertEqual(self.reviewed, [])

    def test_a_failing_request_is_isolated_and_the_drain_continues(self) -> None:
        first = self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")
        second = self.store.enqueue_retry("owner/repo", 2, "abc1234567", "owner@company.example")
        calls = []

        def runner(client, pr, recorder=None, force=False):
            calls.append(pr["number"])
            recorder.begin(client.repo, pr["number"], pr["head"]["sha"])
            if len(calls) == 1:
                raise svc.ServiceError("review exploded")
            recorder.succeeded()
            return True

        self.client.pr_payload = dict(self.client.pr_payload, number=1)
        self.assertEqual(self._drain(review_runner=runner), 2)
        self.assertEqual(self.store.retry_status(first["id"]), "failed")
        self.assertIn("review raised", self.store.requests[first["id"]]["error"])
        self.assertEqual(self.store.retry_status(second["id"]), "succeeded")
        self.assertTrue(self.lock.held, "the executor lock survives a failing request")

    def test_review_that_posts_nothing_fails_the_request(self) -> None:
        request = self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")

        def runner(client, pr, recorder=None, force=False):
            recorder.begin(client.repo, pr["number"], pr["head"]["sha"])
            return False

        self.assertEqual(self._drain(review_runner=runner), 1)
        self.assertEqual(self.store.retry_status(request["id"]), "failed")
        self.assertEqual(self.store.get_run("run-1")["status"], "failed")

    def test_untrackable_retry_is_refused_instead_of_run(self) -> None:
        request = self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")

        def runner(client, pr, recorder=None, force=False):
            # Same shape as review_pr: the attempt row comes first.
            self.store.fail_start_run = True
            recorder.begin(client.repo, pr["number"], pr["head"]["sha"])
            self.reviewed.append(pr["number"])  # pragma: no cover - must not run
            return True

        self.assertEqual(self._drain(review_runner=runner), 1)
        self.assertEqual(self.store.retry_status(request["id"]), "failed")
        self.assertIn("dashboard unavailable", self.store.requests[request["id"]]["error"])
        self.assertEqual(self.reviewed, [])

    def test_drain_is_bounded_per_cycle(self) -> None:
        for number in range(4):
            self.store.enqueue_retry("owner/repo", number + 1, "abc1234567", "owner@company.example")
        self.assertEqual(self._drain(max_requests=2), 2)
        self.assertEqual(
            [request["status"] for request in self.store.requests.values()],
            ["succeeded", "succeeded", "queued", "queued"],
            "the rest of the queue waits for the next cycle instead of starving polling",
        )

    def test_pause_sentinel_stops_claiming_new_requests(self) -> None:
        request = self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")
        pause = Path(self._tmp.name) / "executor.pause"
        pause.write_text("deploy\n", encoding="utf-8")
        self.assertEqual(self._drain(pause_path=pause), 0)
        self.assertEqual(self.store.retry_status(request["id"]), "queued")


class RetryWakeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeStore()
        self.slept: list = []
        self.now = [0.0]

    def _waiter(self) -> dx.RetryWaiter:
        def sleeper(seconds):
            self.slept.append(seconds)
            self.now[0] += seconds

        return dx.RetryWaiter(
            self.store,
            sleeper=sleeper,
            monotonic=lambda: self.now[0],
            logger=lambda message: None,
        )

    def test_queued_request_wakes_the_daemon_within_one_second(self) -> None:
        waiter = self._waiter()

        def sleeper(seconds):
            # An operator click lands in the middle of the 180s daemon sleep.
            self.slept.append(seconds)
            self.now[0] += seconds
            if len(self.slept) == 1:
                self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")

        waiter._sleep = sleeper
        self.assertTrue(waiter.wait(180))
        self.assertLessEqual(sum(self.slept), 1.0, self.slept)
        self.assertTrue(self.slept, "the daemon must actually sleep between polls")

    def test_no_request_sleeps_the_whole_interval(self) -> None:
        waiter = self._waiter()
        self.assertFalse(waiter.wait(2.0))
        self.assertAlmostEqual(sum(self.slept), 2.0, places=6)

    def test_waiter_starts_at_the_event_tail_without_scanning_history(self) -> None:
        # RoL review on PR #12: sync() scanned from id 0 in 500-row batches, so
        # a long-lived event table put every wake behind the whole backlog.
        with tempfile.TemporaryDirectory() as tmp:
            store = DashboardStore(Path(tmp) / "dashboard.sqlite3")
            for index in range(5):
                store.start_run("owner/repo", index + 1, "abc%07d" % index)
            tail = store.latest_event_id()
            self.assertGreater(tail, 0)
            with mock.patch.object(store, "events_after", side_effect=AssertionError("scanned history")):
                waiter = dx.RetryWaiter(store, sleeper=lambda seconds: None, logger=lambda message: None)
            self.assertEqual(waiter.cursor, tail)
            store.enqueue_retry("owner/repo", 9, "abc9999999", "owner@company.example")
            self.assertTrue(waiter.wait(1.0), "only events after the tail are examined")

    def test_a_transient_probe_failure_backs_off_once_and_then_probes_again(self) -> None:
        # RoL review on PR #12: one failed probe disabled wake-ups for the
        # waiter's lifetime.
        with tempfile.TemporaryDirectory() as tmp:
            store = DashboardStore(Path(tmp) / "dashboard.sqlite3")
            slept = []
            messages = []
            waiter = dx.RetryWaiter(store, sleeper=slept.append, monotonic=lambda: 0.0, logger=messages.append)
            with mock.patch.object(store, "events_after", side_effect=sqlite3.OperationalError("locked")):
                self.assertFalse(waiter.wait(2.0))
            self.assertEqual(slept, [2.0], "one probe, then sleep out the interval")
            store.enqueue_retry("owner/repo", 3, "abc1234567", "owner@company.example")
            self.assertTrue(waiter.wait(1.0), "the next wait probes again and wakes")
            self.assertEqual(sum("backing off" in m for m in messages), 1)
            self.assertEqual(sum("recovered" in m for m in messages), 1)

    def test_an_already_seen_request_does_not_wake_again(self) -> None:
        self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")
        waiter = self._waiter()  # sync() consumes existing history at startup
        self.assertFalse(waiter.wait(1.0))
        self.store.enqueue_retry("owner/repo", 2, "bbb2222222", "owner@company.example")
        self.assertTrue(waiter.wait(1.0))
        # The wake event is consumed: the next sleep is not interrupted again.
        self.assertFalse(waiter.wait(1.0))

    def test_broken_event_probe_falls_back_to_a_plain_sleep(self) -> None:
        waiter = self._waiter()

        def boom(cursor=0, limit=100):
            raise RuntimeError("store is down")

        self.store.events_after = boom
        self.assertFalse(waiter.wait(3.0))
        self.assertEqual(self.slept, [3.0])

    def test_no_store_means_a_plain_sleep(self) -> None:
        waiter = dx.RetryWaiter(None, sleeper=self.slept.append, logger=lambda message: None)
        self.assertFalse(waiter.wait(7.0))
        self.assertEqual(self.slept, [7.0])


class RunOnceRereviewRunnerTest(unittest.TestCase):
    """The ``once`` cycle's retry runner must take every keyword the executor forwards."""

    def test_rereview_request_reaches_review_pr_through_run_once(self) -> None:
        # _execute_retry_request forwards rereview=True for a Retrigger badge
        # request; the serial cycle's review_runner must pass it to review_pr
        # instead of raising TypeError and failing the admin's request.
        store = FakeStore()
        seen = []

        def fake_drain(store_arg, lock, configured, client_factory, review_runner, ledger_lookup, **kwargs):
            client = types.SimpleNamespace(repo="owner/repo")
            review_runner(client, dict(FakeClient().pr_payload), recorder=None, force=True, rereview=True)

        def fake_review(client, config, config_path, pr, state, **kwargs):
            seen.append(kwargs)
            return True

        self._run_once_with(store, fake_drain, fake_review)
        self.assertTrue(seen, "the rereview never reached review_pr")
        self.assertTrue(seen[0].get("rereview"))
        self.assertTrue(seen[0].get("force"))


    def _run_once_with(self, store, fake_drain, fake_review, state=None):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = _config(tmp_path / "work")
            config["state_path"] = str(tmp_path / "state.json")
            if state is not None:
                (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")

            class Client:
                def __init__(self, repo, gh_bin="gh"):
                    self.repo = repo

                def list_open_prs(self):
                    return []

            with mock.patch.object(svc, "GitHubClient", Client), mock.patch.object(
                svc, "load_config", return_value=config
            ), mock.patch.object(
                svc, "expand_path", side_effect=lambda v, base=None: Path(str(v))
            ), mock.patch.object(
                dx, "open_store", lambda *args, **kwargs: store
            ), mock.patch.object(dx, "drain_retry_requests", fake_drain), mock.patch.object(
                svc, "review_pr", fake_review
            ):
                svc.run_once(
                    argparse.Namespace(
                        config=str(ROOT / "config" / "reviewer.json"),
                        repo=None,
                        pr=None,
                        dry_run=False,
                        force=False,
                    )
                )

    def test_once_refuses_a_rereview_of_a_head_its_own_poll_reviewed_meanwhile(self) -> None:
        # Trinity (astra-zhuge) on 13ff95b: the rereview is enqueued after the
        # first drain; the poll reviews the head; the between-PR drain then ran
        # the rereview with force and posted a second review of the same head.
        store = FakeStore()
        payload = dict(FakeClient().pr_payload)
        head = payload["head"]["sha"]
        posted = store.start_run("owner/repo", int(payload["number"]), head, trigger="poll")
        store.update_run(posted["id"], "succeeded", stage="posted")
        seen, refused = [], []

        def fake_drain(store_arg, lock, configured, client_factory, review_runner, ledger_lookup, **kwargs):
            client = types.SimpleNamespace(repo="owner/repo")
            recorder = dx.DashboardRecorder(store_arg, trigger="retrigger", retry_of=None, logger=lambda m: None)
            try:
                review_runner(client, dict(payload), recorder=recorder, force=True, rereview=True)
            except dx.RetryRefused as exc:
                refused.append(exc)

        def fake_review(client, config, config_path, pr, state, **kwargs):
            seen.append(kwargs)
            return True

        self._run_once_with(store, fake_drain, fake_review)
        self.assertEqual(seen, [], "a second review of the same head was posted")
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0].status, "stale")
        self.assertIn("reviewed meanwhile", refused[0].reason)


    def test_once_refuses_a_rereview_of_a_head_only_the_ledger_shows_reviewed_after_the_click(self) -> None:
        # Bot red-team P2 on dab3378: the poll's best-effort recorder could not
        # write an attempt row, but the review posted and the ledger has it.
        store = FakeStore()
        payload = dict(FakeClient().pr_payload)
        head = payload["head"]["sha"]
        number = str(payload["number"])
        state = {
            "repositories": {
                "owner/repo": {
                    "baseline_initialized": True,
                    "reviewed": {number: {"head_sha": head, "reviewed_at": "2026-09-11T00:00:05+00:00"}},
                }
            }
        }
        seen, refused = [], []

        def fake_drain(store_arg, lock, configured, client_factory, review_runner, ledger_lookup, **kwargs):
            client = types.SimpleNamespace(repo="owner/repo")
            recorder = dx.DashboardRecorder(
                store_arg,
                trigger="retrigger",
                retry_of=None,
                seen_at="2026-09-11T00:00:00+00:00",
                logger=lambda m: None,
            )
            try:
                review_runner(client, dict(payload), recorder=recorder, force=True, rereview=True)
            except dx.RetryRefused as exc:
                refused.append(exc)

        def fake_review(client, config, config_path, pr, state, **kwargs):
            seen.append(kwargs)
            return True

        self._run_once_with(store, fake_drain, fake_review, state=state)
        self.assertEqual(seen, [], "a second review of the same head was posted")
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0].status, "stale")
        self.assertIn("reviewed meanwhile", refused[0].reason)


class RecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lock = dx.ExecutorLock(Path(self._tmp.name) / "executor.lock")

    def test_recovery_requires_the_exclusive_lock(self) -> None:
        store = FakeStore()
        with self.assertRaises(RuntimeError):
            dx.recover_after_restart(store, self.lock)
        self.assertEqual(store.recovered, 0)
        self.lock.acquire()
        self.addCleanup(self.lock.release)
        dx.recover_after_restart(store, self.lock, logger=lambda message: None)
        self.assertEqual(store.recovered, 1)

    def test_ordinary_cycles_never_recover(self) -> None:
        store = FakeStore()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = _config(tmp_path / "work")
            config["state_path"] = str(tmp_path / "state.json")

            class Client:
                def __init__(self, repo, gh_bin="gh"):
                    self.repo = repo

                def list_open_prs(self):
                    return []

            with mock.patch.object(svc, "GitHubClient", Client), mock.patch.object(
                svc, "load_config", return_value=config
            ), mock.patch.object(
                svc, "expand_path", side_effect=lambda v, base=None: Path(str(v))
            ), mock.patch.object(
                dx, "open_store", lambda *args, **kwargs: store
            ):
                svc.run_once(
                    argparse.Namespace(
                        config=str(ROOT / "config" / "reviewer.json"),
                        repo=None,
                        pr=None,
                        dry_run=False,
                        force=False,
                    )
                )
        self.assertEqual(store.recovered, 0, "run_once must not reset rows it does not own")


class LegacyProvenanceTest(unittest.TestCase):
    state = {
        "repositories": {
            "owner/repo": {
                "reviewed": {
                    "1": {
                        "head_sha": "aaa1111111",
                        "reviewed_at": "2026-08-01T10:00:00+00:00",
                        "review_event": "COMMENT",
                        "agents": ["a", "b", "c"],
                    },
                    "2": {
                        "head_sha": "bbb2222222",
                        "reviewed_at": "2026-08-02T10:00:00+00:00",
                        "initial_baseline_without_review": True,
                        "agents": ["a", "b", "c"],
                    },
                    "3": {
                        "head_sha": "ccc3333333",
                        "post_outcome_unknown": True,
                        "post_error": "urlopen error timed out",
                        "agents": ["a"],
                    },
                }
            }
        }
    }

    def _import(self, store) -> dict:
        return dx.import_legacy_ledger(store, self.state, logger=lambda message: None)

    def test_provenance_is_truthful_per_entry_kind(self) -> None:
        store = FakeStore()
        counts = self._import(store)
        rows = {row["pr"]: row for row in store.imported}
        self.assertEqual(rows[1]["status"], "succeeded")
        self.assertEqual(rows[1]["trigger"], "legacy")
        self.assertEqual(rows[1]["created_at"], "2026-08-01T10:00:00+00:00")
        self.assertEqual(rows[2]["status"], "skipped")
        self.assertEqual(rows[2]["trigger"], "baseline")
        self.assertEqual(counts, {"legacy": 1, "baseline": 1, "unverified": 1, "skipped": 0, "native": 0})

    def test_unverified_post_never_imports_as_succeeded(self) -> None:
        store = FakeStore()
        self._import(store)
        row = {row["pr"]: row for row in store.imported}[3]
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(row["stage"], dx.POSTING_UNVERIFIED_STAGE)
        self.assertIn("timed out", row["error"])

    def test_unknown_timestamp_is_not_invented(self) -> None:
        store = FakeStore()
        self._import(store)
        row = {row["pr"]: row for row in store.imported}[3]
        self.assertEqual(row["created_at"], "")

    def test_ids_are_deterministic_and_reimport_adds_nothing(self) -> None:
        store = FakeStore()
        self._import(store)
        first = [row["id"] for row in store.imported]
        self._import(store)
        self.assertEqual([row["id"] for row in store.imported], first)
        self.assertEqual(
            first[0], dx.legacy_run_id("owner/repo", 1, "aaa1111111"), "ids must be reproducible"
        )

    def test_entries_without_a_head_are_skipped_not_guessed(self) -> None:
        store = FakeStore()
        counts = dx.import_legacy_ledger(
            store,
            {"repositories": {"owner/repo": {"reviewed": {"4": {"reviewed_at": "2026-08-04T00:00:00+00:00"}}}}},
            logger=lambda message: None,
        )
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(store.imported, [])

    def test_import_requires_the_lock_when_one_is_given(self) -> None:
        store = FakeStore()
        with tempfile.TemporaryDirectory() as tmp:
            lock = dx.ExecutorLock(Path(tmp) / "executor.lock")
            with self.assertRaises(RuntimeError):
                dx.import_legacy_ledger(store, self.state, lock=lock, logger=lambda message: None)
        self.assertEqual(store.imported, [])

    def _startup_imports(self, store, history_module, config=None, client_factory=None) -> dict:
        """Drive the startup hook with a stub history unit (W2b owns the real one)."""
        with tempfile.TemporaryDirectory() as tmp:
            lock = dx.ExecutorLock(Path(tmp) / "executor.lock")
            lock.acquire()
            try:
                # ``from . import dashboard_history`` resolves through the
                # package attribute once the real module has been imported by
                # another test, so both bindings are replaced.
                with mock.patch.dict(
                    sys.modules, {"code_review_service.dashboard_history": history_module}
                ), mock.patch.object(
                    code_review_service, "dashboard_history", history_module, create=True
                ):
                    return dx.run_startup_imports(
                        store,
                        lock,
                        self.state,
                        config=config,
                        client_factory=client_factory,
                        logger=lambda message: None,
                    )
            finally:
                lock.release()

    def test_startup_imports_call_the_reserved_history_hook(self) -> None:
        """The retained-log failure importer is a separate, optional unit."""
        seen: dict = {}

        class HistoryModule:
            @staticmethod
            def import_retained_failures(store, lock=None, logger=None, config=None):
                seen["lock_held"] = lock.held
                seen["config"] = config
                return {"imported": 2}

        store = FakeStore()
        counts = self._startup_imports(
            store, HistoryModule, config={"repositories": ["owner/repo"]}
        )
        self.assertEqual(counts["legacy"], 1, "the ledger import still runs")
        self.assertEqual(counts["history_imported"], 2)
        self.assertTrue(seen["lock_held"], "history import runs under the executor lock")
        self.assertEqual(seen["config"], {"repositories": ["owner/repo"]})

    def test_a_broken_history_hook_does_not_break_the_ledger_import(self) -> None:
        class HistoryModule:
            @staticmethod
            def import_retained_failures(store, lock=None, logger=None):
                raise RuntimeError("log parsing exploded")

        counts = self._startup_imports(FakeStore(), HistoryModule)
        self.assertEqual(counts["legacy"], 1)
        self.assertNotIn("history_imported", counts)

    def test_history_hook_receives_a_head_resolver_built_from_the_client(self) -> None:
        seen: dict = {}

        class HistoryModule:
            @staticmethod
            def import_retained_failures(store, lock=None, logger=None, resolve_head=None):
                seen["resolve_head"] = resolve_head
                return {"imported": 0}

        self._startup_imports(FakeStore(), HistoryModule)
        self.assertIsNone(seen["resolve_head"], "without a GitHub client no resolver is invented")

        client = FakeClient()
        self._startup_imports(FakeStore(), HistoryModule, client_factory=lambda repo: client)
        resolver = seen["resolve_head"]
        self.assertIsNotNone(resolver)
        self.assertEqual(resolver("owner/repo", 1), "abc1234567")

    def test_head_resolver_is_sanitized_and_never_raises(self) -> None:
        class Boom:
            def get_pr(self, number):
                raise svc.ServiceError("GitHub API GET failed: 500 " + "x" * 4000)

        logged: list = []
        resolver = dx.make_head_resolver(lambda repo: Boom(), logger=logged.append)
        assert resolver is not None
        self.assertIsNone(resolver("owner/repo", 1))
        self.assertEqual(len(logged), 1)
        self.assertLessEqual(len(logged[0]), 300, logged[0])

        class NoHead:
            def get_pr(self, number):
                return {"number": number}

        empty = dx.make_head_resolver(lambda repo: NoHead())
        assert empty is not None
        self.assertIsNone(empty("owner/repo", 1))
        self.assertIsNone(dx.make_head_resolver(None), "no client means no resolver")


class ShippedConfigTest(unittest.TestCase):
    """The deployed config must actually turn dashboard recording on."""

    def setUp(self) -> None:
        self.config_path = ROOT / "config" / "reviewer.json"
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))

    def test_dashboard_recording_is_enabled_in_the_shipped_config(self) -> None:
        # The history store is what author commands (lang, settings, review,
        # feedback) read; with it off the daemon never polls them.
        dashboard = self.config.get("dashboard")
        self.assertIs(dashboard.get("enabled"), True)
        self.assertNotIn("public_url", dashboard, "the template names no deployment's dashboard")
        # public_url only feeds the required commit status link (PR #23); no other key.
        self.assertLessEqual(set(dashboard), {"enabled", "public_url", "_comment"})

    def test_db_path_is_derived_from_the_state_path_only(self) -> None:
        self.config["dashboard"]["enabled"] = True
        service_root = svc.service_root_for_config(self.config_path)
        state_path = svc.expand_path(self.config["state_path"], service_root)
        self.assertEqual(
            dx.dashboard_db_path(self.config, state_path),
            state_path.parent / "dashboard" / "dashboard.sqlite3",
        )
        # One source of truth for the location: no second configured path that
        # the daemon and the web tier could disagree about.
        self.assertNotIn("dashboard_db_path", self.config)
        self.assertNotIn("db_path", self.config["dashboard"])

    def test_lock_and_pause_paths_live_beside_the_state_file(self) -> None:
        service_root = svc.service_root_for_config(self.config_path)
        state_path = svc.expand_path(self.config["state_path"], service_root)
        self.assertEqual(
            dx.executor_lock_path(self.config, state_path), state_path.parent / "executor.lock"
        )
        self.assertEqual(
            dx.executor_pause_path(self.config, state_path), state_path.parent / "executor.pause"
        )

    def test_disabled_or_absent_dashboard_records_nothing(self) -> None:
        state_path = Path("/tmp/does-not-matter/state.json")
        self.assertIsNone(dx.dashboard_db_path({}, state_path))
        self.assertIsNone(dx.dashboard_db_path({"dashboard": {"enabled": False}}, state_path))


class PostOutcomeClassificationTest(unittest.TestCase):
    def test_transport_and_server_errors_are_uncertain(self) -> None:
        self.assertTrue(svc.post_outcome_is_uncertain(_transport_error()))
        for code in (500, 502, 503, 408, 429):
            self.assertTrue(svc.post_outcome_is_uncertain(_http_error(code)), code)

    def test_client_rejections_are_certain(self) -> None:
        for code in (400, 401, 403, 404, 422):
            self.assertFalse(svc.post_outcome_is_uncertain(_http_error(code)), code)

    def test_unrecognized_error_shape_fails_closed(self) -> None:
        self.assertTrue(svc.post_outcome_is_uncertain(svc.ServiceError("no status")))

    def test_error_raised_before_sending_is_certain(self) -> None:
        # e.g. the GitHub App token could not be minted: the POST never left.
        error = svc.ServiceError("GitHub App token for installation 1 failed: 503 unavailable")
        error.request_sent = False
        self.assertFalse(svc.post_outcome_is_uncertain(error))


class RealStoreIntegrationTest(unittest.TestCase):
    """The executor against the real SQLite store, not the double."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.db_path = self.tmp_path / "dashboard" / "dashboard.sqlite3"
        self.store = DashboardStore(self.db_path)
        self.client = FakeClient()

    def _review(self, recorder, state=None, client=None):
        return svc.review_pr(
            client or self.client,
            _config(self.tmp_path / "work"),
            ROOT / "config" / "reviewer.json",
            dict((client or self.client).pr_payload),
            state if state is not None else {"reviewed": {}},
            state_path=self.tmp_path / "state.json",
            recorder=recorder,
        )

    def test_poll_attempt_is_durable_and_replayable(self) -> None:
        recorder = dx.DashboardRecorder(self.store, trigger="poll", logger=lambda message: None)
        with _stub_agent(), _stub_clone():
            self.assertTrue(self._review(recorder))

        reopened = DashboardStore(self.db_path)
        run = reopened.get_run(recorder.run_id)
        assert run is not None
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["trigger"], "poll")
        self.assertEqual(
            run["review_url"], "https://github.invalid/owner/repo/pull/1#pullrequestreview-999"
        )
        self.assertEqual(run["total_agents"], 1)
        kinds = [event["kind"] for event in reopened.events_after(0)]
        self.assertEqual(kinds[0], "run_started")
        self.assertEqual(kinds[-1], "run_updated")
        ids = [event["id"] for event in reopened.events_after(0)]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual([listed["id"] for listed in reopened.list_runs(status="succeeded")], [run["id"]])

    def test_crashed_posting_attempt_recovers_as_interrupted_and_is_not_retryable(self) -> None:
        recorder = dx.DashboardRecorder(self.store, trigger="poll", logger=lambda message: None)
        # A kill during the POST: no terminal status is ever written, exactly
        # like the process dying between the request and the response.
        self.client.review_error = KeyboardInterrupt()
        with _stub_agent(), _stub_clone():
            with self.assertRaises(KeyboardInterrupt):
                self._review(recorder)
        run_id = recorder.run_id
        crashed = self.store.get_run(run_id)
        assert crashed is not None
        self.assertEqual(crashed["status"], "running")
        self.assertEqual(crashed["stage"], dx.POSTING_STAGE)

        restarted = DashboardStore(self.db_path)  # fresh connection, like a restart
        lock = dx.ExecutorLock(self.tmp_path / "executor.lock")
        lock.acquire()
        try:
            dx.recover_after_restart(restarted, lock, logger=lambda message: None)
        finally:
            lock.release()
        run = restarted.get_run(run_id)
        assert run is not None
        self.assertEqual(run["status"], "interrupted")
        decision = dx.retry_decision({"head_sha": "abc1234567"}, run, None)
        self.assertFalse(decision.allow, "an unverified POST is never re-posted")
        self.assertIn("unknown", decision.reason)

    def test_legacy_ledger_import_into_the_real_store(self) -> None:
        lock = dx.ExecutorLock(self.tmp_path / "executor.lock")
        lock.acquire()
        self.addCleanup(lock.release)
        counts = dx.run_startup_imports(
            self.store, lock, LegacyProvenanceTest.state, logger=lambda message: None
        )
        self.assertEqual(
            (counts["legacy"], counts["baseline"], counts["unverified"], counts["skipped"]),
            (1, 1, 1, 0),
        )
        rows = {run["pr"]: run for run in self.store.list_runs()}
        self.assertEqual((rows[1]["status"], rows[1]["trigger"]), ("succeeded", "legacy"))
        self.assertEqual((rows[2]["status"], rows[2]["trigger"]), ("skipped", "baseline"))
        self.assertEqual((rows[3]["status"], rows[3]["stage"]), ("interrupted", dx.POSTING_UNVERIFIED_STAGE))
        self.assertEqual(rows[3]["created_at"], "", "an unknown historical date is not invented")
        self.assertEqual(self.store.events_after(0), [], "history must not flood the live event stream")
        # Idempotent across daemon restarts.
        dx.run_startup_imports(self.store, lock, LegacyProvenanceTest.state, logger=lambda message: None)
        self.assertEqual(len(self.store.list_runs()), 3)

    def test_backfill_skips_reviews_the_dashboard_already_recorded(self) -> None:
        # RoL review on PR #12: a dashboard-era success is in both the SQLite
        # attempts and the JSON ledger; every restart re-imported it as a
        # second "legacy" success.
        lock = dx.ExecutorLock(self.tmp_path / "executor.lock")
        lock.acquire()
        self.addCleanup(lock.release)
        live = self.store.start_run("owner/repo", 1, "aaa1111111", trigger="poll")
        self.store.update_run(live["id"], "succeeded", review_url="https://github.invalid/r/1")
        counts = dx.run_startup_imports(
            self.store, lock, LegacyProvenanceTest.state, logger=lambda message: None
        )
        self.assertEqual(counts["native"], 1)
        self.assertEqual(counts["legacy"], 0)
        rows = self.store.attempts_for("owner/repo", 1, "aaa1111111")
        self.assertEqual([row["id"] for row in rows], [live["id"]], "one review, one attempt")
        # Pre-dashboard history for other heads still lands.
        self.assertEqual(len(self.store.list_runs()), 3)

    def test_backfill_dedup_sees_a_live_attempt_behind_a_page_of_history(self) -> None:
        # Reviewer MUST-FIX: 20+ imported rows for the head must not hide it.
        lock = dx.ExecutorLock(self.tmp_path / "executor.lock")
        lock.acquire()
        self.addCleanup(lock.release)
        for index in range(25):
            self.store.import_run("hist-%d" % index, "owner/repo", 1, "aaa1111111", trigger="legacy-failure", status="failed")
        live = self.store.start_run("owner/repo", 1, "aaa1111111", trigger="poll")
        self.store.update_run(live["id"], "succeeded")
        counts = dx.run_startup_imports(self.store, lock, LegacyProvenanceTest.state, logger=lambda message: None)
        self.assertEqual((counts["native"], counts["legacy"]), (1, 0))
        self.assertEqual(self.store.get_run(dx.legacy_run_id("owner/repo", 1, "aaa1111111")), None)

    def test_a_lost_terminal_attempt_write_is_retried_then_replayed(self) -> None:
        # RoL review on PR #12: a transient failure of update_run(succeeded)
        # dropped the outcome; the request could then be succeeded while its
        # attempt stayed running until a restart labelled it interrupted.
        dx._PENDING_RUN_OUTCOMES.clear()
        self.addCleanup(dx._PENDING_RUN_OUTCOMES.clear)
        lock = dx.ExecutorLock(self.tmp_path / "executor.lock")
        lock.acquire()
        self.addCleanup(lock.release)
        store = self.store
        messages = []
        recorder = dx.DashboardRecorder(store, trigger="poll", logger=messages.append)
        recorder.begin("owner/repo", 4, "abc1234567")
        real_update = store.update_run
        calls = {"n": 0}

        def flaky_update(run_id, status, **fields):
            if status == "succeeded" and calls["n"] < 3:
                calls["n"] += 1
                raise sqlite3.OperationalError("disk I/O error")
            return real_update(run_id, status, **fields)

        with mock.patch.object(store, "update_run", flaky_update):
            recorder.succeeded("https://github.invalid/r/4")
        self.assertEqual(calls["n"], 3, "terminal writes are retried before giving up")
        self.assertEqual(store.get_run(recorder.run_id)["status"], "running")
        self.assertEqual(list(dx.pending_run_outcomes()), [recorder.run_id])
        # Progress updates are not remembered: the next one supersedes them.
        with mock.patch.object(store, "update_run", side_effect=RuntimeError("down")):
            dx.DashboardRecorder(store, trigger="poll", logger=messages.append)
            recorder2 = dx.DashboardRecorder(store, trigger="poll", logger=messages.append)
        recorder2.begin("owner/repo", 5, "abc1234567")
        with mock.patch.object(store, "update_run", side_effect=RuntimeError("down")):
            recorder2.stage("cloning")
        self.assertEqual(list(dx.pending_run_outcomes()), [recorder.run_id])
        # Next cycle, store healthy: the outcome lands with its fields.
        dx.reconcile_running_requests(store, lock, logger=messages.append)
        run = store.get_run(recorder.run_id)
        self.assertEqual((run["status"], run["review_url"]), ("succeeded", "https://github.invalid/r/4"))
        self.assertEqual(dx.pending_run_outcomes(), {})
        # A row already closed by restart recovery is never overwritten.
        dx.remember_run_outcome(recorder2.run_id, "succeeded", {}, messages.append)
        store.recover_interrupted()
        dx.reconcile_running_requests(store, lock, logger=messages.append)
        self.assertEqual(store.get_run(recorder2.run_id)["status"], "interrupted")
        self.assertEqual(dx.pending_run_outcomes(), {})

    def test_a_terminal_write_that_commits_then_raises_is_not_repeated(self) -> None:
        # Reviewer note: the store only rejects a *different* terminal status,
        # so a blind retry after an ambiguous error would emit the terminal
        # event twice. The recorder re-reads the row instead.
        dx._PENDING_RUN_OUTCOMES.clear()
        self.addCleanup(dx._PENDING_RUN_OUTCOMES.clear)
        lock = dx.ExecutorLock(self.tmp_path / "executor.lock")
        lock.acquire()
        self.addCleanup(lock.release)
        store = self.store
        recorder = dx.DashboardRecorder(store, trigger="poll", logger=lambda message: None)
        recorder.begin("owner/repo", 6, "abc1234567")
        real_update = store.update_run
        calls = []

        def commit_then_raise(run_id, status, **fields):
            calls.append(status)
            real_update(run_id, status, **fields)
            raise sqlite3.OperationalError("connection dropped after commit")

        with mock.patch.object(store, "update_run", commit_then_raise):
            recorder.succeeded("https://github.invalid/r/6")
        self.assertEqual(calls, ["succeeded"], "no blind retry after an observed terminal row")
        self.assertEqual(recorder.terminal_status, "succeeded")
        self.assertEqual(dx.pending_run_outcomes(), {})
        terminal_events = [
            e for e in store.events_after(0)
            if e["run_id"] == recorder.run_id and e["kind"] == "run_updated" and e["payload"]["status"] == "succeeded"
        ]
        self.assertEqual(len(terminal_events), 1)
        # Same guard on replay: a remembered outcome for a row that landed is dropped.
        dx.remember_run_outcome(recorder.run_id, "succeeded", {}, lambda message: None)
        self.assertEqual(dx.replay_pending_run_outcomes(store, lambda message: None), 0)
        self.assertEqual(dx.pending_run_outcomes(), {})
        self.assertEqual(len([e for e in store.events_after(0) if e["run_id"] == recorder.run_id]), 2)

    def test_pending_finalizations_are_bounded_and_loud(self) -> None:
        dx._PENDING_FINALIZATIONS.clear()
        self.addCleanup(dx._PENDING_FINALIZATIONS.clear)
        messages = []
        broken = mock.Mock()
        broken.finish_retry.side_effect = RuntimeError("down")
        broken.retry_status.return_value = "running"
        with mock.patch.object(dx, "MAX_PENDING_FINALIZATIONS", 2):
            for index in range(3):
                dx._finish_quietly(broken, {"id": "req-%d" % index}, "failed", messages.append, error="x", attempts=1)
        self.assertEqual(sorted(dx.pending_finalizations()), ["req-0", "req-1"])
        self.assertTrue(any("not remembering req-2" in m for m in messages))

    def test_backfill_dedup_matches_the_ledger_outcome_not_just_the_head(self) -> None:
        # RoL review on PR #12: a native *failure* for a head must not
        # suppress the ledger's recorded success from a later, store-degraded
        # attempt that still posted.
        lock = dx.ExecutorLock(self.tmp_path / "executor.lock")
        lock.acquire()
        self.addCleanup(lock.release)
        failed = self.store.start_run("owner/repo", 1, "aaa1111111", trigger="poll")
        self.store.update_run(failed["id"], "failed", error="quorum")
        counts = dx.run_startup_imports(self.store, lock, LegacyProvenanceTest.state, logger=lambda message: None)
        self.assertEqual((counts["native"], counts["legacy"]), (0, 1))
        rows = self.store.attempts_for("owner/repo", 1, "aaa1111111")
        self.assertEqual([row["status"] for row in rows], ["failed", "succeeded"])
        self.assertEqual(rows[1]["trigger"], "legacy")

    def test_backfill_outcome_match_is_not_limited_to_a_page_of_native_rows(self) -> None:
        # Reviewer note: the matching succeeded row may sit behind many native
        # failures for the same head; the existence check runs in SQL.
        lock = dx.ExecutorLock(self.tmp_path / "executor.lock")
        lock.acquire()
        self.addCleanup(lock.release)
        for _ in range(dx.MAX_NATIVE_ROWS_CHECKED + 5):
            run = self.store.start_run("owner/repo", 1, "aaa1111111", trigger="retry")
            self.store.update_run(run["id"], "failed", error="quorum")
        posted = self.store.start_run("owner/repo", 1, "aaa1111111", trigger="retry")
        self.store.update_run(posted["id"], "succeeded")
        counts = dx.run_startup_imports(self.store, lock, LegacyProvenanceTest.state, logger=lambda message: None)
        self.assertEqual((counts["native"], counts["legacy"]), (1, 0))

    def test_startup_recovery_that_fails_is_retried_before_the_next_drain(self) -> None:
        # RoL review on PR #12: recovery ran exactly once per start; a transient
        # error left claimed requests running for the daemon's lifetime.
        lock = dx.ExecutorLock(self.tmp_path / "executor.lock")
        lock.acquire()
        self.addCleanup(lock.release)
        store = self.store
        store.enqueue_retry("owner/repo", 8, "abc1234567", "owner@company.example")
        stranded = store.claim_retry()
        messages = []
        with mock.patch.object(store, "recover_interrupted", side_effect=sqlite3.OperationalError("locked")):
            dx.recover_after_restart(store, lock, logger=messages.append)
        self.assertTrue(dx.recovery_pending())
        self.assertEqual(store.get_retry(stranded["id"])["status"], "running")
        self.assertEqual(sum("recovery failed" in m for m in messages), dx.RECOVERY_ATTEMPTS)
        dx.reconcile_running_requests(store, lock, logger=messages.append)
        self.assertFalse(dx.recovery_pending())
        self.assertEqual(store.get_retry(stranded["id"])["status"], "failed")
        # A healthy start clears the flag immediately.
        dx.recover_after_restart(store, lock, logger=messages.append)
        self.assertFalse(dx.recovery_pending())

    def test_pending_recovery_runs_through_the_real_drain_before_any_claim(self) -> None:
        # Reviewer note: pin the scheduling contract through the actual caller.
        # The drain reconciles (recovery, remembered outcomes) before claiming,
        # and at that point this process owns no live attempt.
        dx._PENDING_RUN_OUTCOMES.clear()
        self.addCleanup(dx._PENDING_RUN_OUTCOMES.clear)
        lock = dx.ExecutorLock(self.tmp_path / "executor.lock")
        lock.acquire()
        self.addCleanup(lock.release)
        store = self.store
        store.enqueue_retry("owner/repo", 8, "abc1234567", "owner@company.example")
        stranded = store.claim_retry()
        crashed = store.start_run("owner/repo", 8, "abc1234567", trigger="retry", request_id=stranded["id"])
        dx.remember_run_outcome(crashed["id"], "succeeded", {"review_url": "https://github.invalid/r/8"}, lambda m: None)
        with mock.patch.object(store, "recover_interrupted", side_effect=sqlite3.OperationalError("locked")):
            dx.recover_after_restart(store, lock, logger=lambda message: None)
        self.assertTrue(dx.recovery_pending())
        order = []
        real_recover = store.recover_interrupted
        real_claim = store.claim_retry

        def recover_then_note():
            order.append("recover")
            return real_recover()

        def claim_then_note():
            order.append("claim")
            return real_claim()

        with mock.patch.object(store, "recover_interrupted", recover_then_note), mock.patch.object(
            store, "claim_retry", claim_then_note
        ):
            consumed = dx.drain_retry_requests(
                store, lock, ["owner/repo"], lambda repo: FakeClient(), lambda *a, **k: True,
                lambda repo, pr: None, logger=lambda message: None,
            )
        self.assertEqual(consumed, 0)
        self.assertEqual(order[:1], ["recover"], "recovery precedes any claim")
        self.assertFalse(dx.recovery_pending())
        # The remembered (confirmed) success lands *before* the deferred
        # recovery runs, so recovery finds a succeeded attempt and closes the
        # request from it instead of stamping a posted review interrupted.
        run = store.get_run(crashed["id"])
        self.assertEqual((run["status"], run["review_url"]), ("succeeded", "https://github.invalid/r/8"))
        self.assertEqual(dx.pending_run_outcomes(), {})
        self.assertEqual(store.get_retry(stranded["id"])["status"], "succeeded")

    def test_backfill_recognises_a_native_unverified_post_for_a_ledger_unknown_entry(self) -> None:
        # RoL review on PR #12: the live path records an unverified POST as
        # failed@posting-unverified, not interrupted; the ledger's
        # post_outcome_unknown entry must match it.
        lock = dx.ExecutorLock(self.tmp_path / "executor.lock")
        lock.acquire()
        self.addCleanup(lock.release)
        native = self.store.start_run("owner/repo", 3, "ccc3333333", trigger="poll")
        self.store.update_run(native["id"], "failed", stage=dx.POSTING_UNVERIFIED_STAGE, error="urlopen error timed out")
        counts = dx.run_startup_imports(self.store, lock, LegacyProvenanceTest.state, logger=lambda message: None)
        self.assertEqual((counts["native"], counts["unverified"]), (1, 0))
        rows = self.store.attempts_for("owner/repo", 3, "ccc3333333")
        self.assertEqual([(r["id"], r["status"], r["stage"]) for r in rows], [(native["id"], "failed", dx.POSTING_UNVERIFIED_STAGE)])
        # A native *succeeded* row for that head is not an unknown outcome.
        posted = self.store.start_run("owner/repo", 3, "ccc3333333", trigger="retry")
        self.store.update_run(posted["id"], "succeeded")
        self.assertTrue(dx._natively_recorded(self.store, "owner/repo", 3, "ccc3333333", lambda m: None, stages=dx.POSTING_STAGES, not_status="succeeded"))
        self.assertFalse(dx._natively_recorded(self.store, "owner/repo", 3, "other", lambda m: None, stages=dx.POSTING_STAGES, not_status="succeeded"))

    def test_baseline_and_posted_ledger_records_get_distinct_import_ids(self) -> None:
        # RoL review on PR #12: a baseline (skipped) import occupied the id a
        # later confirmed review needed; INSERT OR IGNORE then kept "skipped".
        lock = dx.ExecutorLock(self.tmp_path / "executor.lock")
        lock.acquire()
        self.addCleanup(lock.release)
        baseline = {"repositories": {"owner/repo": {"reviewed": {"2": {
            "head_sha": "bbb2222222", "initial_baseline_without_review": True, "agents": ["a"]}}}}}
        dx.run_startup_imports(self.store, lock, baseline, logger=lambda message: None)
        reviewed = {"repositories": {"owner/repo": {"reviewed": {"2": {
            "head_sha": "bbb2222222", "reviewed_at": "2026-09-01T00:00:00+00:00", "agents": ["a"]}}}}}
        counts = dx.run_startup_imports(self.store, lock, reviewed, logger=lambda message: None)
        self.assertEqual(counts["legacy"], 1)
        rows = self.store.attempts_for("owner/repo", 2, "bbb2222222")
        self.assertEqual(sorted((r["status"], r["trigger"]) for r in rows), [("skipped", "baseline"), ("succeeded", "legacy")])
        self.assertNotEqual(dx.legacy_run_id("owner/repo", 2, "bbb2222222", "baseline"), dx.legacy_run_id("owner/repo", 2, "bbb2222222"))
        self.assertEqual(dx.legacy_run_id("owner/repo", 2, "bbb2222222", "legacy"), dx.legacy_run_id("owner/repo", 2, "bbb2222222"))

    def test_refusing_an_untracked_retry_still_closes_its_attempt(self) -> None:
        # RoL review on PR #12: the DashboardUnavailable handler finalized the
        # request but left the already-begun attempt running until restart.
        dx._PENDING_RUN_OUTCOMES.clear()
        self.addCleanup(dx._PENDING_RUN_OUTCOMES.clear)
        store = self.store
        store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")
        request = store.claim_retry()

        def runner(client, pr_payload, recorder=None, force=False):
            recorder.begin(client.repo, 1, "abc1234567")
            raise dx.DashboardUnavailable("could not record posting")

        finalized = dx._execute_retry_request(
            store, request, {"owner/repo"}, lambda repo: FakeClient(), runner, lambda repo, pr: None, lambda m: None
        )
        self.assertTrue(finalized)
        detail = store.get_retry(request["id"])
        self.assertEqual(detail["status"], "failed")
        run = store.get_run(detail["run_id"])
        self.assertEqual(run["status"], "failed")
        self.assertIn("dashboard unavailable", run["error"])
        self.assertEqual(store.list_runs(status="running"), [])

    def test_lost_terminal_write_for_an_unlinked_request_is_replayed(self) -> None:
        # RoL review on PR #12: a claimed request rejected during revalidation
        # has no run row; if all finish_retry writes fail it stayed "running"
        # until restart (never re-claimed, enqueue dedups onto it).
        dx._PENDING_FINALIZATIONS.clear()
        lock = dx.ExecutorLock(self.tmp_path / "executor.lock")
        lock.acquire()
        self.addCleanup(lock.release)
        store = self.store
        store.enqueue_retry("owner/repo", 5, "abc1234567", "owner@company.example")
        real_finish = store.finish_retry
        # 3 attempts in _finish_quietly plus 3 more in the drain's own
        # same-cycle replay: the store stays broken for the whole cycle.
        failures = {"left": 6}

        def flaky_finish(*args, **kwargs):
            if failures["left"] > 0:
                failures["left"] -= 1
                raise sqlite3.OperationalError("disk I/O error")
            return real_finish(*args, **kwargs)

        with mock.patch.object(store, "finish_retry", flaky_finish):
            consumed = dx.drain_retry_requests(
                store,
                lock,
                ["other/repo"],  # owner/repo is not configured -> rejected before any run
                lambda repo: None,
                lambda *a, **k: True,
                lambda repo, pr: None,
                logger=lambda message: None,
            )
        self.assertEqual(consumed, 1)
        request = store.list_retries(status="running")[0]
        self.assertIsNone(request.get("run_id"))
        self.assertEqual(list(dx.pending_finalizations()), [request["id"]])
        # Next cycle, store healthy again: the remembered outcome lands.
        self.assertEqual(dx.reconcile_running_requests(store, lock, logger=lambda message: None), 1)
        self.assertEqual(store.get_retry(request["id"])["status"], "failed")
        self.assertIn("not configured", store.get_retry(request["id"])["error"])
        self.assertEqual(dx.pending_finalizations(), {})
        self.assertIsNotNone(store.enqueue_retry("owner/repo", 5, "abc1234567", "owner@company.example"))
        self.assertEqual(store.claim_retry()["status"], "running", "the head is retryable again")

    def test_history_startup_completes_heads_and_keeps_moved_ones_unknown(self) -> None:
        """Real store + real history importer + injected resolver, no GitHub I/O.

        The retained log only holds ``head_sha[:12]``. The startup hook must hand
        the importer a resolver so a still-current head becomes the full 40 chars
        (retryable), while a PR whose head moved keeps the 12-char prefix (the
        retry gate then refuses it instead of reviewing a head nobody reviewed).
        """
        current_head = "a" * 40
        moved_head = "b" * 40
        log_path = self.tmp_path / "service.out.log"
        log_path.write_text(
            "\n".join(
                [
                    "2026-08-01T00:00:00+00:00 owner/repo pr #11: cloning source into "
                    "/w/runs/pr-11-%s-xyz/source" % current_head[:12],
                    "2026-08-01T00:00:01+00:00 owner/repo pr #11: running 3 agents",
                    "2026-08-01T00:00:02+00:00 owner/repo pr #11: only 1/3 agents succeeded; "
                    "not posting review",
                    "2026-08-01T00:00:03+00:00 owner/repo pr #11: preserving run dir for "
                    "inspection: /w/runs/pr-11-%s-xyz" % current_head[:12],
                    "2026-08-02T00:00:00+00:00 owner/repo pr #12: cloning source into "
                    "/w/runs/pr-12-%s-abc/source" % ("c" * 12),
                    "2026-08-02T00:00:01+00:00 owner/repo pr #12: running 3 agents",
                    "2026-08-02T00:00:02+00:00 owner/repo pr #12: only 0/3 agents succeeded; "
                    "not posting review",
                    "2026-08-02T00:00:03+00:00 owner/repo pr #12: preserving run dir for "
                    "inspection: /w/runs/pr-12-%s-abc" % ("c" * 12),
                    "",
                ]
            ),
            encoding="utf-8",
        )

        heads = {11: current_head, 12: moved_head}
        asked: list = []

        class ResolverClient:
            def __init__(self, repo: str) -> None:
                self.repo = repo

            def get_pr(self, number):
                asked.append(number)
                return {"number": number, "state": "open", "head": {"sha": heads[int(number)]}}

        config = {
            "repositories": ["owner/repo"],
            "history_log_path": str(log_path),
        }
        lock = dx.ExecutorLock(self.tmp_path / "executor.lock")
        lock.acquire()
        self.addCleanup(lock.release)
        counts = dx.run_startup_imports(
            self.store,
            lock,
            {"repositories": {}},
            config=config,
            client_factory=ResolverClient,
            logger=lambda message: None,
        )

        self.assertEqual(counts["history_imported"], 2, counts)
        self.assertEqual(counts["history_head_resolved"], 1, counts)
        self.assertEqual(counts["history_head_prefix_only"], 1, counts)
        self.assertEqual(sorted(asked), [11, 12], "one bounded lookup per imported attempt")

        rows = {run["pr"]: run for run in self.store.list_runs()}
        self.assertEqual(rows[11]["head_sha"], current_head)
        self.assertEqual(len(rows[11]["head_sha"]), 40)
        self.assertEqual(rows[11]["status"], "failed")
        self.assertEqual(rows[12]["head_sha"], "c" * 12, "a moved head is never rewritten")
        self.assertNotEqual(rows[12]["head_sha"], moved_head)

        # The completed head is retryable; the prefix-only one is refused by the
        # same rule W3's retry gate uses (submitted head must equal the original).
        self.assertTrue(dx.retry_decision({"head_sha": current_head}, rows[11], None).allow)
        moved = dx.retry_decision({"head_sha": moved_head}, rows[12], None)
        self.assertFalse(moved.allow)
        self.assertEqual(moved.status, "stale")

    def _flaky_finish(self, failures: int):
        """Make ``finish_retry`` raise the first ``failures`` times, then work."""
        real_finish = self.store.finish_retry
        calls: list = []

        def flaky(request_id, status, run_id=None, error=None):
            calls.append(status)
            if len(calls) <= failures:
                raise sqlite3.OperationalError("database is locked")
            return real_finish(request_id, status, run_id=run_id, error=error)

        self.store.finish_retry = flaky  # type: ignore[assignment]
        return calls, real_finish

    def _drain_one(self, state=None):
        lock = dx.ExecutorLock(self.tmp_path / "executor.lock")
        lock.acquire()
        self.addCleanup(lock.release)
        reviewed_state = state if state is not None else {"reviewed": {}}

        def runner(client, pr, recorder=None, force=False):
            with _stub_agent(), _stub_clone():
                return self._review(recorder, state=reviewed_state, client=client)

        consumed = dx.drain_retry_requests(
            self.store,
            lock,
            ["owner/repo"],
            lambda repo: self.client,
            runner,
            lambda repo, pr: None,
            logger=lambda message: None,
        )
        return lock, consumed

    def test_transient_finalization_failure_still_records_the_succeeded_request(self) -> None:
        request = self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")
        self._flaky_finish(1)
        _lock, consumed = self._drain_one()
        self.assertEqual(consumed, 1)
        self.assertEqual(self.client.create_review_calls, 1, "the POST must not be repeated")
        self.assertEqual(self.store.retry_status(request["id"]), "succeeded")
        detail = self.store.get_retry(request["id"])
        assert detail is not None
        self.assertTrue(detail["run_id"])
        self.assertEqual(self.store.get_run(detail["run_id"])["status"], "succeeded")

    def test_request_left_running_by_a_dead_store_is_reconciled_later(self) -> None:
        request = self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")
        _calls, real_finish = self._flaky_finish(10_000)  # write path stays broken
        lock, consumed = self._drain_one()
        self.assertEqual(consumed, 1)
        self.assertEqual(self.client.create_review_calls, 1)
        # Truthful while the store cannot be written: not converged, not a lie.
        self.assertEqual(self.store.retry_status(request["id"]), "running")
        run_id = self.store.get_retry(request["id"])["run_id"]
        self.assertEqual(self.store.get_run(run_id)["status"], "succeeded")

        # The write path recovers; a later cycle reconciles from the durable run.
        self.store.finish_retry = real_finish  # type: ignore[assignment]
        self.assertEqual(dx.reconcile_running_requests(self.store, lock, logger=lambda m: None), 1)
        self.assertEqual(self.store.retry_status(request["id"]), "succeeded")
        self.assertEqual(self.client.create_review_calls, 1, "reconciliation never re-posts")
        self.assertEqual(self.store.get_retry(request["id"])["run_id"], run_id)
        # Idempotent: nothing left to reconcile.
        self.assertEqual(dx.reconcile_running_requests(self.store, lock, logger=lambda m: None), 0)

    def test_reconciliation_never_turns_an_unverified_post_into_success(self) -> None:
        request = self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")
        self.client.review_error = KeyboardInterrupt()
        _calls, real_finish = self._flaky_finish(10_000)
        lock = dx.ExecutorLock(self.tmp_path / "executor.lock")
        lock.acquire()
        self.addCleanup(lock.release)
        claimed = self.store.claim_retry()
        recorder = dx.DashboardRecorder(
            self.store, trigger="retry", request_id=claimed["id"], strict=True, logger=lambda m: None
        )
        with _stub_agent(), _stub_clone():
            with self.assertRaises(KeyboardInterrupt):
                self._review(recorder)
        run_id = recorder.run_id
        dx.recover_after_restart(self.store, lock, logger=lambda m: None)
        self.assertEqual(self.store.get_run(run_id)["status"], "interrupted")

        self.store.finish_retry = real_finish  # type: ignore[assignment]
        # recover_interrupted already failed the claimed request; reconciliation
        # must leave that terminal decision alone and never claim success.
        self.assertEqual(dx.reconcile_running_requests(self.store, lock, logger=lambda m: None), 0)
        self.assertEqual(self.store.retry_status(request["id"]), "failed")
        self.assertEqual(self.store.get_run(run_id)["status"], "interrupted")

    def test_reconciliation_finalizes_a_failed_run_as_a_failed_request(self) -> None:
        lock = dx.ExecutorLock(self.tmp_path / "executor.lock")
        lock.acquire()
        self.addCleanup(lock.release)
        request = self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")
        claimed = self.store.claim_retry()
        run = self.store.start_run(
            "owner/repo", 1, "abc1234567", trigger="retry", request_id=claimed["id"]
        )
        self.store.update_run(run["id"], "failed", stage="quorum", error="only 1/3 agents succeeded")

        self.assertEqual(dx.reconcile_running_requests(self.store, lock, logger=lambda m: None), 1)
        detail = self.store.get_retry(request["id"])
        assert detail is not None
        self.assertEqual(detail["status"], "failed")
        self.assertIn("1/3", detail["error"])

    def test_reconciliation_leaves_live_and_unlinked_requests_alone(self) -> None:
        lock = dx.ExecutorLock(self.tmp_path / "executor.lock")
        lock.acquire()
        self.addCleanup(lock.release)
        unlinked = self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")
        self.store.claim_retry()
        live = self.store.enqueue_retry("owner/repo", 2, "bbb2222222", "owner@company.example")
        claimed = self.store.claim_retry()
        self.store.start_run("owner/repo", 2, "bbb2222222", trigger="retry", request_id=claimed["id"])

        self.assertEqual(dx.reconcile_running_requests(self.store, lock, logger=lambda m: None), 0)
        self.assertEqual(self.store.retry_status(unlinked["id"]), "running")
        self.assertEqual(self.store.retry_status(live["id"]), "running")

    def test_reconciliation_requires_the_lock_and_survives_a_dead_store(self) -> None:
        unheld = dx.ExecutorLock(self.tmp_path / "other.lock")
        with self.assertRaises(RuntimeError):
            dx.reconcile_running_requests(self.store, unheld)
        lock = dx.ExecutorLock(self.tmp_path / "executor.lock")
        lock.acquire()
        self.addCleanup(lock.release)

        def boom(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        self.store.list_retries = boom  # type: ignore[assignment]
        logged: list = []
        self.assertEqual(dx.reconcile_running_requests(self.store, lock, logger=logged.append), 0)
        self.assertTrue(any("reconcil" in message for message in logged), logged)

    def test_retry_round_trip_through_the_real_store(self) -> None:
        """Depends on the agreed W1 additions (``start_run(request_id=...)``)."""
        state = {"reviewed": {}}
        request = self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")
        self.store.claim_retry()
        lock = dx.ExecutorLock(self.tmp_path / "executor.lock")
        lock.acquire()
        self.addCleanup(lock.release)

        def runner(client, pr, recorder=None, force=False):
            with _stub_agent(), _stub_clone():
                return self._review(recorder, state=state, client=client)

        # Re-enqueue path: the claimed request above is drained first.
        self.store.finish_retry(request["id"], "failed", error="reset for this test")
        fresh = self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")
        consumed = dx.drain_retry_requests(
            self.store,
            lock,
            ["owner/repo"],
            lambda repo: self.client,
            runner,
            lambda repo, pr: None,
            logger=lambda message: None,
        )
        self.assertEqual(consumed, 1)
        self.assertEqual(self.store.retry_status(fresh["id"]), "succeeded")
        retry_runs = [run for run in self.store.list_runs() if run["trigger"] == "retry"]
        self.assertEqual(len(retry_runs), 1)
        self.assertEqual(retry_runs[0]["status"], "succeeded")
        self.assertEqual(state["reviewed"]["1"]["head_sha"], "abc1234567")


class StoreContractTest(unittest.TestCase):
    """Narrow expectations on the W1 store surface that W2 calls.

    These are deliberately assertions about the store *contract*, not a second
    implementation of it: W2 cannot record legacy history or link a claimed
    request to its attempt without them.
    """

    def test_store_exposes_the_lifecycle_surface_w2_calls(self) -> None:
        for name in (
            "start_run",
            "update_run",
            "get_run",
            "claim_retry",
            "finish_retry",
            "events_after",
            "recover_interrupted",
            "get_setting",
        ):
            self.assertTrue(callable(getattr(DashboardStore, name, None)), name)

    def test_start_run_accepts_request_id_for_the_atomic_link(self) -> None:
        import inspect

        parameters = inspect.signature(DashboardStore.start_run).parameters
        self.assertIn("request_id", parameters)

    def test_import_run_accepts_the_backfill_call_w2_makes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DashboardStore(Path(tmp) / "dashboard" / "dashboard.sqlite3")
            store.import_run(
                dx.legacy_run_id("owner/repo", 2, "bbb2222222"),
                "owner/repo",
                2,
                "bbb2222222",
                status="skipped",
                trigger="baseline",
                created_at="",
                stage="",
                error="",
                total_agents=3,
            )
            run = store.get_run(dx.legacy_run_id("owner/repo", 2, "bbb2222222"))
            self.assertIsNotNone(run)
            assert run is not None
            self.assertEqual(run["status"], "skipped")
            self.assertEqual(run["trigger"], "baseline")




class RetryScopeExecutorTest(unittest.TestCase):
    """A "failed" retry hands the earlier attempt's kept results to the review; "all" hands nothing."""

    HEAD = "abc1234567"
    KEPT = [
        {"agent": "correctness", "summary": "ok", "findings": [], "llm": {"model": "grok-4.6"}},
        {"agent": "security", "summary": "ok", "findings": [{"path": "a.py", "line": 2, "title": "[P2] x"}]},
    ]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = FakeStore()
        self.lock = dx.ExecutorLock(Path(self._tmp.name) / "executor.lock")
        self.lock.acquire()
        self.addCleanup(self.lock.release)
        self.client = FakeClient()
        self.calls: list = []
        self.logged: list = []

    def _earlier(self, head: str = HEAD, kept=KEPT) -> str:
        run = self.store.start_run("owner/repo", 1, head)
        self.store.update_run(run["id"], "failed", stage="quorum", agent_results=list(kept))
        return run["id"]

    def _drain(self, **request) -> int:
        def runner(client, pr, recorder=None, force=False, **kwargs):
            self.calls.append(kwargs)
            recorder.begin(client.repo, pr["number"], pr["head"]["sha"])
            recorder.succeeded(review_url="https://github.invalid/r/1")
            return True

        self.store.enqueue_retry("owner/repo", 1, self.HEAD, "owner@company.example", **request)
        return dx.drain_retry_requests(
            self.store, self.lock, ["owner/repo"], lambda repo: self.client, runner, lambda repo, pr: None,
            logger=self.logged.append,
        )

    def test_a_failed_scope_passes_the_kept_results(self) -> None:
        earlier = self._earlier()
        self.assertEqual(self._drain(retry_of=earlier, options={"scope": "failed"}), 1)
        self.assertEqual(self.calls, [{"reuse_results": {
            "run_id": earlier,
            "head_sha": self.HEAD,
            "results": {"correctness": self.KEPT[0], "security": self.KEPT[1]},
        }}])
        self.assertIn("retry req-1: reusing the results of correctness, security from attempt run-1; "
                      "the other agents run again", self.logged)

    def test_an_all_scope_and_a_request_without_scope_pass_nothing(self) -> None:
        earlier = self._earlier()
        policy = {"model": "grok-4.6", "effort": "xhigh", "fallback_models": []}
        self.assertEqual(self._drain(retry_of=earlier, options={"scope": "all", "model_policy": policy}), 1)
        self.assertEqual(self.calls, [{"model_policy": policy}], "the pinned model still travels")
        self.assertIn("retry req-1: running every agent: the request asks for every agent", self.logged)
        self.calls.clear()
        self.assertEqual(self._drain(retry_of=earlier), 1, "queued before scopes existed")
        self.assertEqual(self.calls, [{}])

    def test_a_failed_scope_with_nothing_kept_runs_every_agent_and_says_why(self) -> None:
        earlier = self._earlier(kept=[])
        self.assertEqual(self._drain(retry_of=earlier, options={"scope": "failed"}), 1)
        self.assertEqual(self.calls, [{}])
        self.assertIn("retry req-1: running every agent: the earlier attempt kept no agent results", self.logged)

    def test_reuse_selection_refuses_another_head_and_an_unreadable_store(self) -> None:
        request = {"head_sha": self.HEAD, "options": {"scope": "failed"}}
        earlier = self.store.get_run(self._earlier(head="def4567890"))
        reuse, why = dx.reusable_results(self.store, request, earlier)
        self.assertIsNone(reuse)
        self.assertEqual(why, "the earlier attempt reviewed def4567890, not abc1234567")
        same = self.store.get_run(self._earlier())
        self.assertIsNone(dx.reusable_results(object(), request, same)[0], "a store without kept results")

        class Broken(FakeStore):
            def get_agent_results(self, run_id):
                raise RuntimeError("database is locked")

        reuse, why = dx.reusable_results(Broken(), request, same)
        self.assertIsNone(reuse)
        self.assertIn("database is locked", why)
        self.assertEqual(dx.reusable_results(self.store, request, None), (None, "there is no earlier attempt to reuse results from"))


class ModelPolicyExecutorTest(unittest.TestCase):
    """Executor plumbing for the model policy (.prd/model-policy/trace.md A/C)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = FakeStore()
        self.lock = dx.ExecutorLock(Path(self._tmp.name) / "executor.lock")
        self.lock.acquire()
        self.addCleanup(self.lock.release)
        self.client = FakeClient()
        self.calls: list = []

    def _drain(self) -> int:
        def runner(client, pr, recorder=None, force=False, **kwargs):
            self.calls.append(kwargs)
            recorder.begin(client.repo, pr["number"], pr["head"]["sha"])
            recorder.succeeded(review_url="https://github.invalid/r/1")
            return True

        return dx.drain_retry_requests(
            self.store, self.lock, ["owner/repo"], lambda repo: self.client, runner, lambda repo, pr: None,
            logger=lambda message: None,
        )

    def test_request_options_forward_the_model_policy_to_the_review(self) -> None:
        policy = {"model": "grok-4.6", "effort": "xhigh", "fallback_models": []}
        self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example", options={"model_policy": policy})
        self.assertEqual(self._drain(), 1)
        self.assertEqual(self.calls, [{"model_policy": policy}])

    def test_request_without_options_passes_no_policy_kwarg(self) -> None:
        self.store.enqueue_retry("owner/repo", 1, "abc1234567", "owner@company.example")
        self.assertEqual(self._drain(), 1)
        self.assertEqual(self.calls, [{}], "legacy review runners must keep working without the kwarg")

    def test_recorder_reads_the_dashboard_setting_and_never_raises(self) -> None:
        recorder = dx.DashboardRecorder(self.store, logger=lambda message: None)
        self.assertIsNone(recorder.setting("model_policy"))
        self.store.settings["model_policy"] = {"model": "grok-4.6", "effort": "xhigh", "fallback_models": []}
        self.assertEqual(recorder.setting("model_policy")["model"], "grok-4.6")
        self.store.fail_settings = True
        self.assertIsNone(recorder.setting("model_policy"))
        self.assertIsNone(dx.DashboardRecorder(None).setting("model_policy"))

    def test_review_pr_uses_the_dashboard_policy_over_the_config(self) -> None:
        """The stored policy wins over config and lands on the attempt + runner argv."""
        self.store.settings["model_policy"] = {"model": "gpt-5.6-sol[1m]", "effort": "max", "fallback_models": ["grok-4.6"]}
        recorder = dx.DashboardRecorder(self.store, logger=lambda message: None)
        seen: list = []

        def fake_run_agent(agent, config, config_dir, run_dir, repo, pr, files, diff_text, repository_rules="", artifact_root=None, policy=None):
            seen.append(policy)
            return {"agent": agent["id"], "summary": "ok", "findings": [], "llm": {"model": policy["model"], "effort": policy["effort"], "input_tokens": 3, "output_tokens": 1}}

        config = {
            "agents": [{"id": "a", "prompt_path": "x"}, {"id": "b", "prompt_path": "x"}],
            "agent_runner": {"command": ["true", svc.MODEL_POLICY_ARGS_TOKEN]},
            "model_policy": {"model": "gpt-6-astra[1m]", "effort": "xhigh", "fallback_models": ["grok-4.6"]},
            "min_successful_agents": 0,
            "max_review_comments": 30,
            "max_diff_chars": 180000,
            "work_dir": str(Path(self._tmp.name) / "work"),
            "progress_comment": {"enabled": True},
            "approval": {"enabled": False},
            "executive_summary": {"enabled": False},
        }
        pr = {"number": 3, "head": {"sha": "abc123456789"}, "base": {"sha": "b" * 12}, "title": "t", "body": "", "user": {"login": "x"}, "html_url": "https://github.invalid/pr/3"}
        with mock.patch.object(svc, "run_agent", fake_run_agent), mock.patch.object(
            svc, "clone_pr_source", lambda repo, pr_number, head_sha, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True)
        ), mock.patch.object(svc, "load_repository_rules", lambda *a, **k: ""):
            posted = svc.review_pr(self.client, config, Path(self._tmp.name) / "config" / "reviewer.json", pr, {"reviewed": {}}, recorder=recorder)
        self.assertTrue(posted)
        self.assertEqual({p["model"] for p in seen}, {"gpt-5.6-sol[1m]"})
        self.assertEqual({p["source"] for p in seen}, {"dashboard"})
        run = self.store.runs["run-1"]
        self.assertEqual(run["llm"]["policy"]["source"], "dashboard")
        self.assertEqual(run["llm"]["policy"]["model"], "gpt-5.6-sol[1m]")
        self.assertEqual(run["llm"]["totals"]["input_tokens"], 6)
        self.assertEqual(run["llm"]["totals"]["models_used"], ["gpt-5.6-sol[1m]"])
        self.assertEqual({row["model"] for row in run["agents"]}, {"gpt-5.6-sol[1m]"})
        self.assertIn("run_dir", run)


if __name__ == "__main__":
    unittest.main()
