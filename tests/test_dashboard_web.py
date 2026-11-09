"""Authenticated dashboard web API: verified OIDC, sessions, CSRF, read API/SSE/retry, usage.

W3 of ``.prd/admin-dashboard/loop.md``; HTTP behaviour is specified by
``.prd/admin-dashboard/api-contract.md``.

Two deliberate testing decisions:

* **The store is a documented contract, not a mock of our own code.** W1 owns
  ``dashboard_store.py`` and lands separately, so the web layer is exercised
  against ``FakeDashboardStore`` which implements the W1 contract from
  ``loop.md`` (dedup on enqueue, immutable past attempts, ascending event ids,
  bounded reads). ``StoreContractParityTest`` binds the fake to the real class
  as soon as that module exists, so the fake cannot drift into fiction.
* **OAuth is verified through real Authlib code where it matters.**
  ``RealAuthlibBoundaryTest`` drives the actual
  ``StarletteOAuth2App.authorize_access_token`` with only the HTTP token
  exchange stubbed, so the state/nonce validation under test is Authlib's own
  (observed behaviour: unknown state raises ``MismatchingStateError`` before any
  network call; a state entry without ``nonce`` yields a token with **no**
  ``userinfo``; a bogus ``id_token`` raises a JOSE error). The happy path uses
  ``FakeOAuthApp``, which reproduces that documented output shape
  (``token["userinfo"]`` = validated claims) without needing real Google keys.

None of the fixtures introduce a production bypass: everything test-only is
passed as a keyword argument to ``create_app`` by the test itself, never read
from the environment, the settings file or a request header.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import stat
import subprocess
import sys
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The review daemon stays stdlib-only and runs under /usr/bin/python3, so this
# module must import (and skip) there instead of breaking the suite. The web tier
# and its tests need the pinned dashboard venv (requirements-dashboard.txt).
DEPENDENCY_ERROR: str | None = None
try:
    import httpx
    from authlib.integrations.base_client.errors import OAuthError
    from authlib.integrations.starlette_client import OAuth
    from starlette.responses import RedirectResponse
    from starlette.testclient import TestClient

    from code_review_service import dashboard_web
except ModuleNotFoundError as exc:  # pragma: no cover - exercised on the daemon host
    # CI's dashboard venv sets this, so a missing dependency fails the import
    # instead of silently skipping the whole web tier. Same truthy values as
    # ROL_DASHBOARD_ALLOW_UNCONFIGURED in scripts/install-dashboard.py.
    if os.environ.get("ROL_REQUIRE_DASHBOARD_DEPS", "").strip() in ("1", "true", "yes"):
        raise
    DEPENDENCY_ERROR = (
        f"dashboard web dependencies missing ({exc.name}); "
        "install requirements-dashboard.txt into a dashboard venv"
    )
    httpx = None  # type: ignore[assignment]
    OAuthError = OAuth = RedirectResponse = TestClient = None  # type: ignore[assignment]
    dashboard_web = None  # type: ignore[assignment]

from code_review_service import dashboard_auth
from code_review_service import dashboard_execution as dx
from code_review_service import service as svc
from code_review_service.dashboard_store import PrBusyError, _require_superseded

requires_dashboard_deps = unittest.skipIf(
    DEPENDENCY_ERROR is not None, DEPENDENCY_ERROR or "dashboard dependencies present"
)

ADMIN_EMAIL = "owner@company.example"
PUBLIC_URL = "https://rol.company.example"
LOOPBACK_HOST = "127.0.0.1:8840"
MIN_TOKEN_LEN = 32
GOOGLE_SECRET = "google-client-secret-value"
SESSION_SECRET = "session-secret-value-0123456789abcdef"
LLMUX_KEY = "llmux-admin-key-value"
SERVICE_TOKEN = "portal-service-token-0123456789abcdef"

ADMIN_USERINFO = {
    "sub": "1234567890",
    "email": ADMIN_EMAIL,
    "email_verified": True,
    "name": "Z",
}
VIEWER_EMAIL = "dev@company.example"
VIEWER_USERINFO = {
    "sub": "555",
    "email": VIEWER_EMAIL,
    "email_verified": True,
    "hd": "company.example",
    "name": "Dev",
}

_SENTINEL = object()


# ---------------------------------------------------------------------------
# W1 DashboardStore contract stand-in
# ---------------------------------------------------------------------------


class FakeDashboardStore:
    """In-memory implementation of the W1 ``DashboardStore`` contract."""

    max_list_limit = 200
    max_event_limit = 500

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._run_order: list[str] = []
        self._requests: dict[str, dict[str, Any]] = {}
        self._request_order: list[str] = []
        self._events: list[dict[str, Any]] = []
        self._settings: dict[str, dict[str, Any]] = {}
        self._seq = 0
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    # -- helpers ---------------------------------------------------------
    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def _emit(self, kind: str, run_id: str | None, payload: dict[str, Any]) -> None:
        self._events.append(
            {
                "id": len(self._events) + 1,
                "kind": kind,
                "run_id": run_id,
                "payload": dict(payload),
                "created_at": "2026-09-11T00:00:00+00:00",
            }
        )

    # -- retry queue -----------------------------------------------------
    def enqueue_retry(
        self,
        repo: str,
        pr: int,
        head_sha: str,
        requested_by: str,
        retry_of: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("enqueue_retry", (repo, pr, head_sha, requested_by), {"retry_of": retry_of, "options": options}))
        if options is not None and not isinstance(options, dict):
            raise ValueError("options must be a dict")
        if not str(repo).strip() or int(pr) <= 0 or not str(head_sha).strip() or not str(requested_by).strip():
            raise ValueError("invalid retry request")
        # W1 contract: the busy check (any head) runs BEFORE the same-head
        # dedup below, so an identical-head duplicate rereview POST also gets
        # PrBusyError, not a silent 202 of the first request.
        if isinstance(options, dict) and options.get("kind") == "rereview":
            busy_request, busy_run = self.active_activity_for_pr(repo, pr)
            if busy_request is not None or busy_run is not None:
                raise PrBusyError("a review of this pull request is already queued or running")
        for request_id in self._request_order:
            existing = self._requests[request_id]
            if (
                existing["repo"] == repo
                and existing["pr"] == pr
                and existing["head_sha"] == head_sha
                and existing["status"] in {"queued", "running"}
            ):
                return dict(existing)
        request = {
            "id": self._next_id("req"),
            "status": "queued",
            "repo": repo,
            "options": options or None,
            "pr": pr,
            "head_sha": head_sha,
            "requested_by": requested_by,
            "retry_of": retry_of,
            "created_at": "2026-09-11T00:00:00+00:00",
        }
        self._requests[request["id"]] = request
        self._request_order.append(request["id"])
        self._emit(
            "retry_enqueued",
            None,
            {
                "request_id": request["id"],
                "repo": repo,
                "pr": pr,
                "scope": options.get("scope") if options else None,
                "kind": options.get("kind") if options else None,
            },
        )
        return dict(request)

    def claim_retry(self) -> dict[str, Any] | None:
        for request_id in self._request_order:
            request = self._requests[request_id]
            if request["status"] == "queued":
                request["status"] = "running"
                self._emit("retry_claimed", None, {"request_id": request_id})
                return dict(request)
        return None

    def finish_retry(
        self,
        request_id: str,
        status: str,
        run_id: str | None = None,
        error: str | None = None,
    ) -> None:
        request = self._requests.get(request_id)
        if request is None:
            raise ValueError("unknown retry request")
        if status not in {"succeeded", "failed", "stale"}:
            raise ValueError("invalid terminal status")
        request["status"] = status
        request["run_id"] = run_id
        request["error"] = error
        request["updated_at"] = "2026-09-11T00:00:01+00:00"
        self._emit(
            "retry_finished",
            run_id,
            {"request_id": request_id, "status": status, "error": error},
        )

    def retry_status(self, request_id: str) -> str | None:
        request = self._requests.get(request_id)
        return None if request is None else request["status"]

    def list_retries(
        self,
        status: str | None = None,
        repo: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self.calls.append(("list_retries", (), {"status": status, "repo": repo, "limit": limit, "offset": offset}))
        limit = max(1, min(int(limit) if limit else 1, self.max_list_limit))
        offset = max(0, int(offset))
        # The listing is the detail shape (adds run_id/error/updated_at to the
        # 8-key enqueue shape), so the queue view can show the linked attempt.
        rows = [
            {
                "run_id": None,
                "error": None,
                "updated_at": self._requests[request_id]["created_at"],
                **self._requests[request_id],
            }
            for request_id in reversed(self._request_order)
        ]
        if status:
            rows = [row for row in rows if row["status"] == status]
        if repo:
            rows = [row for row in rows if row["repo"] == repo]
        return rows[offset : offset + limit]

    # -- runs ------------------------------------------------------------
    # -- settings --------------------------------------------------------
    def get_setting(self, key: str) -> dict[str, Any] | None:
        self.calls.append(("get_setting", (key,), {}))
        return self._settings.get(key)

    def set_setting(self, key: str, value: dict[str, Any], updated_by: str) -> dict[str, Any]:
        self.calls.append(("set_setting", (key, value, updated_by), {}))
        if not isinstance(value, dict):
            raise ValueError("setting value must be a dict")
        row = {"key": key, "value": dict(value), "updated_at": "2026-09-17T00:00:00+00:00", "updated_by": updated_by}
        self._settings[key] = row
        self._emit("setting_updated", None, {"key": key, "value": dict(value), "updated_by": updated_by})
        return dict(row)

    def delete_setting(self, key: str, updated_by: str) -> bool:
        self.calls.append(("delete_setting", (key, updated_by), {}))
        existed = key in self._settings
        self._settings.pop(key, None)
        if existed:
            self._emit("setting_deleted", None, {"key": key, "updated_by": updated_by})
        return existed

    def merge_agent_setting(
        self, agent_id: str, patch: dict[str, Any], updated_by: str, normalize: Any, prepare: Any = None
    ) -> dict[str, Any] | None:
        """W1 contract: read-merge-write in one transaction (single-threaded here); ``prepare`` runs inside it."""
        if prepare is not None:
            patch = prepare(dict(patch), self._settings.get(f"agent:{agent_id}"))
        self.calls.append(("merge_agent_setting", (agent_id, dict(patch), updated_by), {}))
        key = f"agent:{agent_id}"
        stored = self._settings.get(key)
        try:
            current = normalize(stored["value"]) if stored else {}
        except ValueError:
            current = {}
        merged = normalize({**current, **patch})
        if merged:
            return self.set_setting(key, merged, updated_by)
        self.delete_setting(key, updated_by)
        return None

    def reconcile_agent_setting(self, agent_id: str, read_default: Any, reconcile: Any) -> tuple[dict[str, Any] | None, str | None]:
        """W1 contract: read the prompt file and apply it to the stored document in one transaction."""
        key = f"agent:{agent_id}"
        stored = self._settings.get(key)
        default_text = read_default() if stored is not None else None
        self.calls.append(("reconcile_agent_setting", (agent_id, default_text), {}))
        if stored is None:
            return None, None
        if not isinstance(default_text, str):
            return dict(stored), None
        try:
            value, action = reconcile(stored["value"], default_text, "2026-09-17T00:00:00+00:00", stored["updated_at"], stored["updated_by"])
        except ValueError:
            return dict(stored), None
        if action is None:
            return dict(stored), None
        if value:
            row = self.set_setting(key, value, stored["updated_by"])
            row["updated_at"] = self._settings[key]["updated_at"] = stored["updated_at"]
        else:
            self.delete_setting(key, stored["updated_by"])
            row = None
        if action == "superseded":
            self._emit("agent_prompt_superseded", None, {"key": key, "agent_id": agent_id})
        return row, action

    def get_agent_setting(self, agent_id: str) -> dict[str, Any] | None:
        """W1 contract: the agent's whole document as one row (the fake keeps it in one key)."""
        return self.get_setting(f"agent:{agent_id}")

    def delete_agent_setting(self, agent_id: str, updated_by: str, superseded_sha: str | None = None) -> bool:
        """W1 contract: Reset; a kept edit goes only when superseded_sha names it."""
        self.calls.append(("delete_agent_setting", (agent_id, updated_by, superseded_sha), {}))
        stored = self._settings.get(f"agent:{agent_id}")
        kept = stored["value"].get("superseded") if stored else None
        if isinstance(kept, dict):
            _require_superseded(kept, superseded_sha)
        return self.delete_setting(f"agent:{agent_id}", updated_by)

    def restore_superseded_prompt(
        self, agent_id: str, expected_sha: str, read_base: Any, updated_by: str, normalize: Any
    ) -> dict[str, Any] | None:
        """W1 contract: the kept edit becomes the prompt again, based on read_base() read inside; None when there is none."""
        base_sha = read_base()
        self.calls.append(("restore_superseded_prompt", (agent_id, base_sha, updated_by), {}))
        key = f"agent:{agent_id}"
        stored = self._settings.get(key)
        try:
            current = normalize(stored["value"]) if stored else {}
        except ValueError:
            current = {}
        kept = current.pop("superseded", None)
        if not kept:
            return None
        _require_superseded(kept, expected_sha)
        current["prompt"] = kept["prompt"]
        current["prompt_saved_at"], current["prompt_saved_by"] = "2026-09-17T00:00:00+00:00", updated_by
        current.pop("prompt_base_sha", None)
        if base_sha:
            current["prompt_base_sha"] = base_sha
        return self.set_setting(key, normalize(current), updated_by)

    def discard_superseded_prompt(
        self, agent_id: str, expected_sha: str, updated_by: str, normalize: Any
    ) -> tuple[bool, dict[str, Any] | None]:
        """W1 contract: drop the kept edit; (found, the row or None once empty)."""
        self.calls.append(("discard_superseded_prompt", (agent_id, updated_by), {}))
        key = f"agent:{agent_id}"
        stored = self._settings.get(key)
        try:
            current = normalize(stored["value"]) if stored else {}
        except ValueError:
            current = {}
        kept = current.pop("superseded", None)
        if kept is None:
            return False, (dict(stored) if stored else None)
        _require_superseded(kept, expected_sha)
        if current:
            return True, self.set_setting(key, current, updated_by)
        self.delete_setting(key, updated_by)
        return True, None

    def start_run(
        self,
        repo: str,
        pr: int,
        head_sha: str,
        title: str = "",
        url: str = "",
        trigger: str = "poll",
        retry_of: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        run = {
            "id": self._next_id("run"),
            # W1 links the attempt to the claimed request inside start_run; the
            # API deliberately does not project it (not in the contract's run
            # entry list), so it must be dropped, not forwarded.
            "request_id": request_id,
            # Execution identity shared with the retained log (W2b); like
            # request_id it is store-internal and not projected by the API.
            "run_dir": None,
            "repo": repo,
            "pr": pr,
            "head_sha": head_sha,
            "title": title,
            "url": url,
            "trigger": trigger,
            "status": "running",
            "stage": "",
            "created_at": "2026-09-11T00:00:00+00:00",
            "updated_at": "2026-09-11T00:00:00+00:00",
            "agents": [],
            "review_url": "",
            "error": "",
            "successful_agents": 0,
            "total_agents": 0,
            "llm": None,
            "retry_of": retry_of,
            # Ids of the agents whose results the attempt kept (the results stay in the store).
            "reusable_agents": [],
        }
        self._runs[run["id"]] = run
        self._run_order.append(run["id"])
        self._emit("run_started", run["id"], {"repo": repo, "pr": pr, "trigger": trigger})
        return dict(run)

    def update_run(self, run_id: str, status: str, **fields: Any) -> None:
        run = self._runs.get(run_id)
        if run is None:
            raise ValueError("unknown run")
        allowed = {"stage", "agents", "review_url", "error", "successful_agents", "total_agents"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"fields not allowed: {sorted(unknown)}")
        run["status"] = status
        run.update(fields)
        self._emit("run_updated", run_id, {"status": status})

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        self.calls.append(("get_run", (run_id,), {}))
        run = self._runs.get(run_id)
        return None if run is None else dict(run)

    def latest_attempt_for_head(self, repo: str, pr: int, head_sha: str) -> dict[str, Any] | None:
        """W1 contract: the single newest attempt for (repo, pr, head), unbounded."""
        self.calls.append(("latest_attempt_for_head", (repo, pr, head_sha), {}))
        matching = [
            self._runs[run_id]
            for run_id in self._run_order
            if self._runs[run_id]["repo"] == repo and self._runs[run_id]["pr"] == pr and self._runs[run_id]["head_sha"] == head_sha
        ]
        return dict(matching[-1]) if matching else None

    def active_activity_for_pr(self, repo: str, pr: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """W1 contract: ``(queued/running request, running run)`` for (repo, pr), any head, unbounded."""
        self.calls.append(("active_activity_for_pr", (repo, pr), {}))
        request = next(
            (
                dict(self._requests[request_id])
                for request_id in self._request_order
                if self._requests[request_id]["repo"] == repo
                and self._requests[request_id]["pr"] == pr
                and self._requests[request_id]["status"] in {"queued", "running"}
            ),
            None,
        )
        run = next(
            (
                dict(self._runs[run_id])
                for run_id in self._run_order
                if self._runs[run_id]["repo"] == repo and self._runs[run_id]["pr"] == pr and self._runs[run_id]["status"] == "running"
            ),
            None,
        )
        return request, run

    def list_runs(
        self,
        status: str | None = None,
        repo: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self.calls.append(("list_runs", (), {"status": status, "repo": repo, "limit": limit, "offset": offset}))
        limit = max(1, min(int(limit) if limit else 1, self.max_list_limit))
        offset = max(0, int(offset))
        rows = [dict(self._runs[run_id]) for run_id in reversed(self._run_order)]
        if status:
            rows = [row for row in rows if row["status"] == status]
        if repo:
            rows = [row for row in rows if row["repo"] == repo]
        return rows[offset : offset + limit]

    def run_status_counts(self, since: str) -> dict[str, dict[str, int]]:
        """One snapshot of every status: {status: {total, recent}}."""
        self.calls.append(("run_status_counts", (), {"since": since}))
        out: dict[str, dict[str, int]] = {}
        for run_id in self._run_order:
            row = self._runs[run_id]
            entry = out.setdefault(row["status"], {"total": 0, "recent": 0})
            entry["total"] += 1
            if str(row.get("updated_at") or "") >= str(since):
                entry["recent"] += 1
        return out

    # -- events ----------------------------------------------------------
    def latest_event_id(self) -> int:
        """Newest persisted event id (W1 addition): the live stream tail."""
        self.calls.append(("latest_event_id", (), {}))
        return self._events[-1]["id"] if self._events else 0

    def latest_event_at(self) -> str | None:
        """Timestamp of the newest event (W1 addition), or None when empty."""
        self.calls.append(("latest_event_at", (), {}))
        return self._events[-1]["created_at"] if self._events else None

    def events_after(self, cursor: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        self.calls.append(("events_after", (), {"cursor": cursor, "limit": limit}))
        cursor = max(0, int(cursor))
        limit = max(1, min(int(limit), self.max_event_limit))
        return [dict(event) for event in self._events if event["id"] > cursor][:limit]

    def recover_interrupted(self) -> None:  # pragma: no cover - executor-side only
        for run in self._runs.values():
            if run["status"] == "running":
                run["status"] = "interrupted"

    # -- test conveniences ----------------------------------------------
    def force_run(self, **fields: Any) -> dict[str, Any]:
        """Insert a run row directly, including fields the web layer must drop."""
        run = self.start_run(
            fields.pop("repo", "owner/repo"),
            fields.pop("pr", 7),
            fields.pop("head_sha", "a" * 40),
            title=fields.pop("title", "Add dashboard"),
            url=fields.pop("url", "https://github.invalid/owner/repo/pull/7"),
            trigger=fields.pop("trigger", "poll"),
            retry_of=fields.pop("retry_of", None),
        )
        stored = self._runs[run["id"]]
        stored.update(fields)
        return dict(stored)

    def force_event(self, kind: str, run_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        """Append an event exactly as the daemon would, including unsafe payload text."""
        self._emit(kind, run_id, payload)
        return dict(self._events[-1])

    def calls_named(self, name: str) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
        return [call for call in self.calls if call[0] == name]


class StoreWithoutRetryListing(FakeDashboardStore):
    """The store shape before W1 lands ``list_retries``."""

    list_retries = None  # type: ignore[assignment]


class StoreWithoutLatestEventId(FakeDashboardStore):
    """The store shape before W1 lands ``latest_event_id``."""

    latest_event_id = None  # type: ignore[assignment]


class StoreWithoutRetriggerCapability(FakeDashboardStore):
    """The store shape before ``active_activity_for_pr``/``latest_attempt_for_head``
    landed. Round 2 MUST-FIX: the retrigger routes must refuse with 503 on a
    store like this, never fall back to the old bounded repo-wide scans."""

    active_activity_for_pr = None  # type: ignore[assignment]
    latest_attempt_for_head = None  # type: ignore[assignment]


class StoreWithoutSummaryCounts(FakeDashboardStore):
    """The store shape before W1 lands the summary helpers."""

    run_status_counts = None  # type: ignore[assignment]
    latest_event_at = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# OAuth / GitHub / llmux fixtures
# ---------------------------------------------------------------------------


class FakeOAuthApp:
    """Reproduces Authlib's documented output shape for the OIDC code flow."""

    def __init__(self, token: dict[str, Any] | None = None) -> None:
        self.token = token if token is not None else {"access_token": "at", "userinfo": dict(ADMIN_USERINFO)}
        self.error: Exception | None = None
        self.authorize_kwargs: dict[str, Any] = {}
        self.userinfo_calls = 0

    async def authorize_redirect(self, request, redirect_uri=None, **kwargs):
        self.authorize_kwargs = dict(kwargs)
        state = f"state-{len(request.session) + 1}"
        request.session[f"_state_google_{state}"] = {
            "data": {"nonce": "nonce-value", "code_verifier": "verifier", "redirect_uri": redirect_uri},
            "exp": time.time() + 600,
        }
        return RedirectResponse(
            f"https://accounts.google.invalid/o/oauth2/v2/auth?state={state}&code_challenge_method=S256",
            status_code=302,
        )

    async def authorize_access_token(self, request, **kwargs):
        error = request.query_params.get("error")
        if error:
            raise OAuthError(error=error, description=request.query_params.get("error_description"))
        state = request.query_params.get("state") or ""
        key = f"_state_google_{state}"
        state_data = request.session.pop(key, None)
        if state_data is None:
            from authlib.integrations.base_client.errors import MismatchingStateError

            raise MismatchingStateError()
        if self.error is not None:
            raise self.error
        return dict(self.token)

    async def userinfo(self, **kwargs):  # pragma: no cover - must never be called
        self.userinfo_calls += 1
        return dict(ADMIN_USERINFO)


class FakeGitHubClient:
    def __init__(self, pr: dict[str, Any] | None = None, error: str | None = None) -> None:
        self.pr = pr
        self.error = error
        self.calls: list[int] = []

    def get_pr(self, number: int) -> dict[str, Any]:
        self.calls.append(number)
        if self.error is not None:
            raise svc.ServiceError(self.error)
        return dict(self.pr or {})


def _closure_value(app: Any, path: str, name: str) -> Any:
    """A variable the route's handler closes over (directly or through a helper)."""
    route = next(route for route in app.routes if getattr(route, "path", None) == path)
    seen: set = set()
    pending = [route.endpoint]
    while pending:
        fn = pending.pop()
        if id(fn) in seen or not hasattr(fn, "__code__"):
            continue
        seen.add(id(fn))
        for var, cell in zip(fn.__code__.co_freevars, fn.__closure__ or ()):
            value = cell.cell_contents
            if var == name:
                return value
            if callable(value):
                pending.append(value)
    raise AssertionError(f"{name} not reachable from {path}")


def open_pr(head_sha: str, number: int = 7) -> dict[str, Any]:
    return {
        "number": number,
        "state": "open",
        "title": "Add dashboard",
        "html_url": f"https://github.invalid/owner/repo/pull/{number}",
        "head": {"sha": head_sha},
    }


USAGE_PAYLOAD = {
    "accounts": [
        {
            "name": "team-a",
            "type": "oauth",
            "status": "healthy",
            "healthy": True,
            "paused": False,
            "in_flight": 2,
            "cooldown_until": None,
            "five_hour": {
                "utilization": 0.42,
                "resets_at": "2026-09-11T05:00:00Z",
                "resets_in_secs": 900,
                "fetched_at_ms": 1757548800000,
                "source": "upstream",
            },
            "seven_day": {
                "utilization": 0.11,
                "resets_at": "2026-09-18T00:00:00Z",
                "resets_in_secs": 600000,
                "fetched_at_ms": 1757548800000,
                "source": "upstream",
            },
            "totals": {"requests": 10, "input_tokens": 100, "output_tokens": 200},
            "session": {"requests": 3, "ok": 3, "errors": 0, "tokens_in": 30, "tokens_out": 60},
            # Fields the dashboard must never forward:
            "api_key": LLMUX_KEY,
            "oauth_refresh_token": "refresh-token-value",
            "recent_activity": [{"prompt": "secret prompt text"}],
            "logs": ["line one"],
        }
    ],
    "api_key": LLMUX_KEY,
    "prompts": ["do not leak"],
}


def usage_transport(
    handler=None,
    payload: dict[str, Any] | None = None,
    status_code: int = 200,
    requested: list[httpx.Request] | None = None,
):
    """httpx.MockTransport that records every request the server actually makes."""

    recorded = requested if requested is not None else []

    def default_handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(status_code, json=payload if payload is not None else USAGE_PAYLOAD)

    def recording_handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return handler(request)

    transport = httpx.MockTransport(recording_handler if handler else default_handler)
    transport.recorded = recorded  # type: ignore[attr-defined]
    return transport


# ---------------------------------------------------------------------------
# App harness
# ---------------------------------------------------------------------------


@requires_dashboard_deps
class AppTestCase(unittest.TestCase):
    """Base harness; the skip marker is inherited by every HTTP test below."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.service_root = self.tmp / "service"
        (self.service_root / "config").mkdir(parents=True)
        self.config_path = self.service_root / "config" / "reviewer.json"
        self.write_config(["owner/repo"])
        (self.service_root / "VERSION").write_text("deploy-main-test\n", encoding="utf-8")
        self.dashboard_dir = self.tmp / "dashboard"
        self.dashboard_dir.mkdir()
        self.store = FakeDashboardStore()
        self.oauth = FakeOAuthApp()
        self.github = FakeGitHubClient()
        self.github_repos: list[str] = []

    def write_config(self, repositories: list[str]) -> None:
        self.config_path.write_text(
            json.dumps(
                {
                    "repositories": repositories,
                    "agents": [{"id": "correctness"}, {"id": "security"}, {"id": "tests"}],
                    "gh_bin": "gh",
                }
            ),
            encoding="utf-8",
        )

    def settings(self, **overrides: Any) -> dict[str, Any]:
        settings = {
            "public_url": PUBLIC_URL,
            "google_client_id": "client-id.apps.googleusercontent.com",
            "google_client_secret": GOOGLE_SECRET,
            "session_secret": SESSION_SECRET,
            "llmux_api_key": LLMUX_KEY,
            "llmux_base_url": "http://127.0.0.1:3456",
            "config_path": str(self.config_path),
            "auth_db_path": str(self.service_root / "data" / "dashboard-auth.sqlite3"),
            "dashboard_dir": str(self.dashboard_dir),
            "sse_poll_seconds": 0.01,
            "sse_heartbeat_seconds": 0.05,
            "sse_recheck_seconds": 0.01,
        }
        settings.update(overrides)
        return settings

    def make_app(self, store: Any = _SENTINEL, **kwargs: Any):
        settings = kwargs.pop("settings", None) or self.settings()
        if store is _SENTINEL:
            store = self.store

        def github_factory(repo: str):
            self.github_repos.append(repo)
            return self.github

        kwargs.setdefault("oauth_client", self.oauth)
        kwargs.setdefault("github_client_factory", github_factory)
        kwargs.setdefault("usage_transport", usage_transport())
        return dashboard_web.create_app(settings, store, **kwargs)

    def make_client(self, **kwargs: Any) -> TestClient:
        app = self.make_app(**kwargs)
        client = TestClient(app, base_url=PUBLIC_URL)
        self.addCleanup(client.close)
        self.app = app
        return client

    # -- run fixtures ----------------------------------------------------
    def failed_run(self, head_sha: str = "a" * 40, **fields: Any) -> dict[str, Any]:
        run = self.store.force_run(head_sha=head_sha, **fields)
        self.store.update_run(run["id"], fields.get("status", "failed"), error="quorum not reached")
        return self.store.get_run(run["id"])  # type: ignore[return-value]


    # -- auth helpers ----------------------------------------------------
    def cookie(self, client: TestClient) -> str:
        return client.cookies[dashboard_auth.SESSION_COOKIE]


    def login(self, client: TestClient) -> dict[str, Any]:
        response = client.get("/auth/login", follow_redirects=False)
        self.assertEqual(response.status_code, 302, response.text)
        location = httpx.URL(response.headers["location"])
        state = location.params["state"]
        callback = client.get(f"/auth/callback?code=auth-code-value&state={state}", follow_redirects=False)
        self.assertEqual(callback.status_code, 302, callback.text)
        me = client.get("/api/me")
        self.assertEqual(me.status_code, 200, me.text)
        return me.json()

    def auth_headers(self, identity: dict[str, Any], **extra: str) -> dict[str, str]:
        headers = {
            "X-CSRF-Token": identity["csrf_token"],
            "Origin": PUBLIC_URL,
            "Content-Type": "application/json",
        }
        headers.update(extra)
        return headers


# ---------------------------------------------------------------------------
# 1. Unauthenticated access
# ---------------------------------------------------------------------------


class UnauthenticatedAccessTest(AppTestCase):
    def test_every_data_route_denies_anonymous_requests(self) -> None:
        client = self.make_client()
        self.store.force_run()
        for path in ("/api/me", "/api/runs", "/api/runs/run-0001", "/api/usage", "/api/events"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertIn("application/json", response.headers["content-type"])
                self.assertEqual(response.json()["error"], "authentication required")
                self.assertEqual(response.headers["cache-control"], "no-store")

    def test_anonymous_retry_is_denied_without_touching_the_store(self) -> None:
        client = self.make_client()
        run = self.store.force_run(status="failed")
        response = client.post(
            f"/api/runs/{run['id']}/retry",
            json={"head_sha": run["head_sha"]},
            headers={"Origin": PUBLIC_URL},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])
        self.assertEqual(self.github.calls, [])

    def test_login_shell_is_public_but_carries_no_run_data(self) -> None:
        client = self.make_client()
        self.store.force_run(repo="owner/repo", pr=4242, error="quorum not reached")
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        body = response.text
        self.assertNotIn("4242", body)
        self.assertNotIn("quorum not reached", body)
        self.assertNotIn("<script>", body.lower())
        self.assertIn("/auth/login", body)


# ---------------------------------------------------------------------------
# 2. Identity verification (unit)
# ---------------------------------------------------------------------------


class CsrfCompareTest(unittest.TestCase):
    def test_non_ascii_token_is_refused_not_raised(self) -> None:
        # RoL review on PR #12: compare_digest raises TypeError on a non-ASCII
        # str; a header can carry one, and that must be a refusal, not a 500.
        session = {"csrf_token": "abc123"}
        self.assertFalse(dashboard_auth.SessionStore.verify_csrf(session, "\xe9"))
        self.assertFalse(dashboard_auth.SessionStore.verify_csrf(session, "abc12\u00e9"))
        self.assertTrue(dashboard_auth.SessionStore.verify_csrf(session, "abc123"))
        self.assertFalse(dashboard_auth.SessionStore.verify_csrf({"csrf_token": "\u00e9"}, "\u00e9x"))


class VerifyIdentityTest(unittest.TestCase):
    def test_accepts_verified_admin(self) -> None:
        identity = dashboard_auth.verify_identity(dict(ADMIN_USERINFO))
        self.assertEqual(identity["email"], ADMIN_EMAIL)
        self.assertEqual(identity["sub"], "1234567890")
        self.assertEqual(identity["role"], "admin")

    def test_normalizes_case_and_surrounding_whitespace(self) -> None:
        identity = dashboard_auth.verify_identity(
            {"sub": "1", "email": " Owner@Company.EXAMPLE ", "email_verified": True}
        )
        self.assertEqual(identity["email"], ADMIN_EMAIL)
        self.assertEqual(identity["role"], "admin")

    def test_company_workspace_account_is_a_viewer(self) -> None:
        identity = dashboard_auth.verify_identity(dict(VIEWER_USERINFO))
        self.assertEqual(identity, {"email": VIEWER_EMAIL, "sub": "555", "role": "viewer"})
        self.assertEqual(dashboard_auth.verify_identity(dict(ADMIN_USERINFO))["role"], "admin")
        upper = dashboard_auth.verify_identity({**VIEWER_USERINFO, "email": " Dev@Company.EXAMPLE ", "hd": "Company.example"})
        self.assertEqual(upper["email"], VIEWER_EMAIL)

    def test_viewer_requires_the_hosted_domain_claim_and_an_exact_domain(self) -> None:
        cases = [
            {**VIEWER_USERINFO, "hd": None},                                   # consumer account with a company-looking address
            {**VIEWER_USERINFO, "hd": "example.com"},                          # another Workspace
            {**VIEWER_USERINFO, "email": "dev@company.example.evil.com", "hd": "company.example.evil.com"},
            {**VIEWER_USERINFO, "email": "dev@sub.company.example", "hd": "sub.company.example"},
            {**VIEWER_USERINFO, "email": "dev@company.exampl\u0435", "hd": "company.exampl\u0435"},
            {**VIEWER_USERINFO, "email": "@company.example"},
            {**VIEWER_USERINFO, "email_verified": "true"},
        ]
        for userinfo in cases:
            with self.subTest(userinfo=userinfo):
                with self.assertRaises(dashboard_auth.AuthError) as ctx:
                    dashboard_auth.verify_identity(userinfo)
                self.assertNotIn("acme", str(ctx.exception), "the rejected address never reaches the message")

    def test_rejects_lookalike_email(self) -> None:
        for email in ("owner@company.example.evil.com", "zz@company.example", "z@acme.iо"):
            with self.subTest(email=email):
                with self.assertRaises(dashboard_auth.AuthError):
                    dashboard_auth.verify_identity({"sub": "1", "email": email, "email_verified": True})

    def test_rejects_unverified_email(self) -> None:
        with self.assertRaises(dashboard_auth.AuthError):
            dashboard_auth.verify_identity(
                {"sub": "1", "email": ADMIN_EMAIL, "email_verified": False}
            )

    def test_rejects_truthy_non_boolean_email_verified(self) -> None:
        for value in ("true", 1, "True"):
            with self.subTest(value=value):
                with self.assertRaises(dashboard_auth.AuthError):
                    dashboard_auth.verify_identity(
                        {"sub": "1", "email": ADMIN_EMAIL, "email_verified": value}
                    )

    def test_rejects_missing_subject(self) -> None:
        for userinfo in ({"email": ADMIN_EMAIL, "email_verified": True}, {"sub": "  ", "email": ADMIN_EMAIL, "email_verified": True}):
            with self.subTest(userinfo=userinfo):
                with self.assertRaises(dashboard_auth.AuthError):
                    dashboard_auth.verify_identity(userinfo)

    def test_rejects_absent_userinfo(self) -> None:
        for userinfo in (None, {}, "owner@company.example"):
            with self.subTest(userinfo=userinfo):
                with self.assertRaises(dashboard_auth.AuthError):
                    dashboard_auth.verify_identity(userinfo)

    def test_error_message_does_not_echo_the_rejected_identity(self) -> None:
        try:
            dashboard_auth.verify_identity(
                {"sub": "1", "email": "attacker@example.com", "email_verified": True}
            )
        except dashboard_auth.AuthError as exc:
            self.assertNotIn("attacker@example.com", str(exc))
        else:  # pragma: no cover
            self.fail("expected AuthError")


GRANTED_EMAIL = "ops@example.com"
# A verified Workspace account outside VIEWER_DOMAINS: only a grant can let it
# in. ``hd`` matters: without it Google does not vouch for who holds the address.
GRANTED_USERINFO = {"sub": "777", "email": GRANTED_EMAIL, "email_verified": True, "hd": "example.com", "name": "Ops"}
GRANT_NEEDS_AUTHORITY = "an admin grant needs a Gmail address or a Google Workspace account"


class AdminGrantIdentityTest(unittest.TestCase):
    """Admin granted to an address on the dashboard: matched exactly, like the owner."""

    def test_a_granted_address_signs_in_as_admin(self) -> None:
        identity = dashboard_auth.verify_identity(dict(GRANTED_USERINFO), [GRANTED_EMAIL])
        # Stored as "granted", never "admin": a build without grants trusts an
        # "admin" row for 8 hours but deletes a role it does not know.
        self.assertEqual(identity, {"email": GRANTED_EMAIL, "sub": "777", "role": "granted"})
        # The owner stays the owner whether or not the list names it.
        self.assertEqual(dashboard_auth.verify_identity(dict(ADMIN_USERINFO), [ADMIN_EMAIL])["role"], "admin")
        # A company account on the list is admin, not viewer.
        self.assertEqual(dashboard_auth.verify_identity(dict(VIEWER_USERINFO), [VIEWER_EMAIL])["role"], "granted")

    def test_an_address_not_on_the_list_and_outside_the_viewer_domains_is_refused(self) -> None:
        for grants in ((), ["someone@example.com"], [ADMIN_EMAIL]):
            with self.subTest(grants=grants):
                with self.assertRaises(dashboard_auth.AuthError) as ctx:
                    dashboard_auth.verify_identity(dict(GRANTED_USERINFO), grants)
                self.assertNotIn(GRANTED_EMAIL, str(ctx.exception))

    def test_lookalikes_never_match_and_case_and_whitespace_normalize(self) -> None:
        for email in (
            "ops@examplе.com",       # cyrillic "е"
            "оps@example.com",       # cyrillic "о"
            "ops@example.com.evil.com",
            "xops@example.com",
            "ops@sub.example.com",
            "ops@example.co",
            "ops@example.com​",      # zero-width space survives strip()
            "ops+x@example.com",
        ):
            with self.subTest(email=email):
                with self.assertRaises(dashboard_auth.AuthError):
                    dashboard_auth.verify_identity({**GRANTED_USERINFO, "email": email}, [GRANTED_EMAIL])
        # A grant is one address, never a domain or a prefix.
        for grants in (["example.com"], ["@example.com"], ["ops@"], ["ops"], [""], [None, 7]):
            with self.subTest(grants=grants):
                with self.assertRaises(dashboard_auth.AuthError):
                    dashboard_auth.verify_identity(dict(GRANTED_USERINFO), grants)
        upper = dashboard_auth.verify_identity({**GRANTED_USERINFO, "email": "  OPS@Example.COM \t"}, [GRANTED_EMAIL])
        self.assertEqual((upper["email"], upper["role"]), (GRANTED_EMAIL, "granted"))

    def test_an_unverified_email_is_refused_even_when_granted(self) -> None:
        for value in (False, None, "true", 1):
            with self.subTest(email_verified=value):
                with self.assertRaises(dashboard_auth.AuthError):
                    dashboard_auth.verify_identity({**GRANTED_USERINFO, "email_verified": value}, [GRANTED_EMAIL])
        missing = {key: value for key, value in GRANTED_USERINFO.items() if key != "email_verified"}
        with self.assertRaises(dashboard_auth.AuthError):
            dashboard_auth.verify_identity(missing, [GRANTED_EMAIL])

    def test_normalize_admin_email(self) -> None:
        self.assertEqual(dashboard_auth.normalize_admin_email("  Ops@Example.COM "), GRANTED_EMAIL)
        longest = "a" * 242 + "@example.com"
        self.assertEqual(len(longest), 254)
        self.assertEqual(dashboard_auth.normalize_admin_email(longest), longest)
        for value in (
            None, 7, "", "   ", "ops", "ops@", "@example.com", "ops@@example.com", "o@ps@example.com",
            "o ps@example.com", "ops@exa\tmple.com", "ops\x00@example.com", "ops​@example.com",
            "ops@example.com‮", "ops @example.com", "a" * 243 + "@example.com",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as ctx:
                    dashboard_auth.normalize_admin_email(value)
                if isinstance(value, str) and "ops" in value:
                    self.assertNotIn("ops", str(ctx.exception), "the message never echoes the input")

    def test_only_well_formed_stored_entries_grant(self) -> None:
        value = {"added": [
            {"email": GRANTED_EMAIL, "added_by": ADMIN_EMAIL},
            {"email": "Upper@Example.com"},         # not in its stored form: ignored, not normalized
            {"email": " spaced@example.com"},
            {"email": ADMIN_EMAIL},                 # the owner is never a grant
            {"email": "two@@example.com"},
            {"email": 7},
            {"repo": "x/y"},
            "loose@example.com",
            None,
            {"email": GRANTED_EMAIL},               # a duplicate counts once
        ]}
        self.assertEqual(dashboard_auth.admin_emails_from_setting(value), [GRANTED_EMAIL])
        for broken in (None, {}, {"added": "ops@example.com"}, ["ops@example.com"], "x"):
            with self.subTest(value=broken):
                self.assertEqual(dashboard_auth.admin_emails_from_setting(broken), [])

    def test_a_grant_counts_only_for_an_address_google_is_authoritative_for(self) -> None:
        # Google vouches for who holds an address only for Gmail and for a
        # Workspace account (``hd``). For any other address ``email_verified``
        # can stay true after the mailbox changes hands, so the former holder
        # would still sign in as the granted admin.
        gmail = {"sub": "888", "email": "ops@gmail.com", "email_verified": True}
        self.assertEqual(dashboard_auth.verify_identity(gmail, ["ops@gmail.com"])["role"], "granted")
        self.assertEqual(dashboard_auth.verify_identity({**gmail, "email": " OPS@Gmail.COM "}, ["ops@gmail.com"])["role"], "granted")
        self.assertEqual(dashboard_auth.verify_identity(dict(GRANTED_USERINFO), [GRANTED_EMAIL])["role"], "granted")
        consumer = {key: value for key, value in GRANTED_USERINFO.items() if key != "hd"}
        company_consumer = {key: value for key, value in VIEWER_USERINFO.items() if key != "hd"}
        for userinfo in (
            consumer,
            company_consumer,
            {**consumer, "hd": ""},
            {**consumer, "hd": "   "},
            {**consumer, "hd": None},
            {**consumer, "hd": 7},
            {**consumer, "hd": True},
            {**consumer, "hd": ["example.com"]},
            {**gmail, "email": "ops@gmail.com.evil.com"},
            {**gmail, "email": "ops@sub.gmail.com"},
            {**gmail, "email": "ops@notgmail.com"},
            {**gmail, "email": "ops@googlemail.com"},
        ):
            email = userinfo["email"]
            with self.subTest(email=email, hd=userinfo.get("hd", "<absent>")):
                with self.assertRaises(dashboard_auth.AuthError) as ctx:
                    dashboard_auth.verify_identity(userinfo, [email])
                self.assertIn(GRANT_NEEDS_AUTHORITY, ctx.exception.safe_message)
                self.assertNotIn(email, ctx.exception.safe_message)
        # The owner and the viewer rule are unchanged.
        self.assertEqual(dashboard_auth.verify_identity(dict(ADMIN_USERINFO), [])["role"], "admin")
        self.assertEqual(dashboard_auth.verify_identity(dict(VIEWER_USERINFO), [])["role"], "viewer")

    def test_non_ascii_addresses_never_grant_and_never_sign_in_except_the_owner(self) -> None:
        kelvin = "\u212aate@example.com"     # KELVIN SIGN: str.lower() folds it to "kate@example.com"
        cyrillic = "d\u0435v@company.example"  # lists exactly like dev@company.example
        for value in (kelvin, cyrillic, "ops@exampl\u00e9.com", "\u00f6ps@example.com", " \u212aATE@EXAMPLE.COM "):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as ctx:
                    dashboard_auth.normalize_admin_email(value)
                self.assertIn("ASCII", str(ctx.exception))
                self.assertNotIn(value.strip().lower(), str(ctx.exception).lower())
        self.assertEqual(
            dashboard_auth.admin_emails_from_setting({"added": [{"email": cyrillic}, {"email": "kate@example.com"}]}),
            ["kate@example.com"],
        )
        kate = {"sub": "9", "email": "Kate@example.com", "email_verified": True, "hd": "example.com"}
        self.assertEqual(dashboard_auth.verify_identity(kate, ["kate@example.com"]),
                         {"email": "kate@example.com", "sub": "9", "role": "granted"})
        for userinfo, grants in (
            ({**kate, "email": kelvin}, ["kate@example.com"]),
            ({**kate, "email": " " + kelvin + " "}, ["kate@example.com"]),
            # The viewer path too: a folded viewer session could be promoted by a grant later.
            ({**VIEWER_USERINFO, "email": "\u212aate@company.example"}, []),
            ({**VIEWER_USERINFO, "email": "\u212aate@company.example"}, ["kate@company.example"]),
            ({**VIEWER_USERINFO, "email": cyrillic}, []),
        ):
            with self.subTest(email=userinfo["email"], grants=grants):
                with self.assertRaises(dashboard_auth.AuthError) as ctx:
                    dashboard_auth.verify_identity(userinfo, grants)
                self.assertNotIn(userinfo["email"].strip(), ctx.exception.safe_message)
        # The owner's path is unchanged.
        self.assertEqual(dashboard_auth.verify_identity({**ADMIN_USERINFO, "email": " Owner@Company.EXAMPLE "}, [])["role"], "admin")

    def test_resolve_session_re_derives_the_role_from_the_current_list(self) -> None:
        def session(email: str, role: str) -> dict:
            return {"email": email, "sub": "1", "role": role, "csrf_token": "c"}

        owner = dashboard_auth.resolve_session(session(ADMIN_EMAIL, "admin"), [])
        self.assertEqual((owner["role"], owner["owner"]), ("admin", True))
        granted = dashboard_auth.resolve_session(session(GRANTED_EMAIL, "granted"), [GRANTED_EMAIL])
        self.assertEqual((granted["role"], granted["owner"]), ("admin", False))
        upgraded = dashboard_auth.resolve_session(session(VIEWER_EMAIL, "viewer"), [VIEWER_EMAIL])
        self.assertEqual((upgraded["role"], upgraded["owner"]), ("admin", False))
        viewer = dashboard_auth.resolve_session(session(VIEWER_EMAIL, "viewer"), [])
        self.assertEqual((viewer["role"], viewer["owner"]), ("viewer", False))
        # Revoked: an admin-minted session ends; it is never downgraded to viewer.
        self.assertIsNone(dashboard_auth.resolve_session(session(GRANTED_EMAIL, "granted"), []))
        self.assertIsNone(dashboard_auth.resolve_session(session(GRANTED_EMAIL, "admin"), []))
        self.assertIsNone(dashboard_auth.resolve_session(session(VIEWER_EMAIL, "admin"), ["other@example.com"]))
        self.assertIsNone(dashboard_auth.resolve_session(session("owner@company.example.evil.com", "admin"), []))
        self.assertIsNone(dashboard_auth.resolve_session(session(VIEWER_EMAIL, "root"), []))


class GrantRemovalSessionPurgeTest(unittest.TestCase):
    """SessionStore.revoke_admin_sessions: the rows removing a grant deletes at once."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = dashboard_auth.SessionStore(Path(tmp.name) / "sessions.sqlite3")

    def test_it_deletes_that_address_admin_sessions_and_keeps_its_viewer_session(self) -> None:
        granted = [self.store.create(GRANTED_EMAIL, "777", role="granted") for _ in range(2)]
        legacy_admin = self.store.create(GRANTED_EMAIL, "777", role="admin")
        viewer = self.store.create(GRANTED_EMAIL, "777", role="viewer")
        other = self.store.create("other@example.com", "778", role="granted")
        near = self.store.create("xops@example.com", "779", role="granted")
        owner = self.store.create(ADMIN_EMAIL, "1", role="admin")

        self.assertEqual(self.store.revoke_admin_sessions(GRANTED_EMAIL), 3)
        for session in granted + [legacy_admin]:
            self.assertIsNone(self.store.get(session["id"]))
        self.assertEqual(self.store.get(viewer["id"])["role"], "viewer", "a viewer row is never touched")
        for session in (other, near, owner):
            self.assertIsNotNone(self.store.get(session["id"]), session["email"])
        self.assertEqual(self.store.revoke_admin_sessions(GRANTED_EMAIL), 0)

    def test_it_never_deletes_the_owner_sessions(self) -> None:
        owner = self.store.create(ADMIN_EMAIL, "1", role="admin")
        for email in (ADMIN_EMAIL, "Owner@Company.EXAMPLE"):
            with self.subTest(email=email):
                self.assertEqual(self.store.revoke_admin_sessions(email), 0)
        self.assertEqual(self.store.get(owner["id"])["role"], "admin")
        for value in ("", None, 7):
            with self.subTest(value=value):
                self.assertEqual(self.store.revoke_admin_sessions(value), 0)


# ---------------------------------------------------------------------------
# 3. Callback flow (fake OAuth transport)
# ---------------------------------------------------------------------------


class CallbackFlowTest(AppTestCase):
    def test_verified_admin_gets_an_opaque_session_cookie(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        self.assertEqual(identity["email"], ADMIN_EMAIL)
        self.assertEqual(identity["role"], "admin")
        self.assertTrue(identity["csrf_token"])

        cookie = client.cookies[dashboard_auth.SESSION_COOKIE]
        self.assertGreaterEqual(len(cookie), 32)
        self.assertNotIn(ADMIN_EMAIL, cookie)
        self.assertNotIn(".", cookie.strip("."))

    def test_session_cookie_flags(self) -> None:
        client = self.make_client()
        response = client.get("/auth/login", follow_redirects=False)
        state = httpx.URL(response.headers["location"]).params["state"]
        callback = client.get(f"/auth/callback?code=c&state={state}", follow_redirects=False)
        set_cookies = [value for value in callback.headers.get_list("set-cookie") if dashboard_auth.SESSION_COOKIE in value]
        self.assertEqual(len(set_cookies), 1, callback.headers.get_list("set-cookie"))
        header = set_cookies[0]
        self.assertIn("HttpOnly", header)
        self.assertIn("Secure", header)
        self.assertIn("SameSite=lax", header.replace("SameSite=Lax", "SameSite=lax"))
        self.assertIn("Path=/", header)

    def test_authorization_request_asks_for_pkce_openid_and_no_offline_access(self) -> None:
        # The factory's own Google registration (oauth_client=None) with only the
        # metadata fetch stubbed: the URL is what Authlib builds from
        # create_app's client_kwargs and the login route's arguments. The fake
        # client's redirect is a fixed string and could show neither.
        client = self.make_client(oauth_client=None)
        google = _closure_value(self.app, "/auth/login", "oauth_app")
        metadata = mock.AsyncMock(return_value=dict(GOOGLE_METADATA))
        with mock.patch.object(google, "load_server_metadata", metadata):
            response = client.get("/auth/login", follow_redirects=False)
        self.assertEqual(response.status_code, 302, response.text)
        location = response.headers["location"]
        params = httpx.URL(location).params
        self.assertIn("openid", params["scope"].split())
        self.assertEqual(params.get("code_challenge_method"), "S256")
        self.assertTrue(params.get("code_challenge"), "PKCE needs the challenge, not just its method")
        self.assertNotIn("access_type", params)
        self.assertNotIn("consent", params.get("prompt", "").split())
        self.assertNotIn("offline", location)

    def test_non_admin_email_is_denied_and_gets_no_session(self) -> None:
        self.oauth.token = {
            "access_token": "at",
            "userinfo": {"sub": "9", "email": "attacker@example.com", "email_verified": True},
        }
        client = self.make_client()
        response = client.get("/auth/login", follow_redirects=False)
        state = httpx.URL(response.headers["location"]).params["state"]
        callback = client.get(f"/auth/callback?code=c&state={state}", follow_redirects=False)
        self.assertEqual(callback.status_code, 403)
        self.assertNotIn(dashboard_auth.SESSION_COOKIE, client.cookies)
        self.assertEqual(client.get("/api/me").status_code, 401)
        self.assertNotIn("attacker@example.com", callback.text)

    def test_unverified_email_is_denied(self) -> None:
        self.oauth.token = {
            "access_token": "at",
            "userinfo": {"sub": "9", "email": ADMIN_EMAIL, "email_verified": False},
        }
        client = self.make_client()
        response = client.get("/auth/login", follow_redirects=False)
        state = httpx.URL(response.headers["location"]).params["state"]
        callback = client.get(f"/auth/callback?code=c&state={state}", follow_redirects=False)
        self.assertEqual(callback.status_code, 403)
        self.assertEqual(client.get("/api/me").status_code, 401)

    def test_token_without_validated_userinfo_is_denied_and_userinfo_endpoint_is_never_called(self) -> None:
        self.oauth.token = {"access_token": "at"}  # no id_token / nonce => no validated claims
        client = self.make_client()
        response = client.get("/auth/login", follow_redirects=False)
        state = httpx.URL(response.headers["location"]).params["state"]
        callback = client.get(f"/auth/callback?code=c&state={state}", follow_redirects=False)
        self.assertEqual(callback.status_code, 403)
        self.assertEqual(self.oauth.userinfo_calls, 0)
        self.assertEqual(client.get("/api/me").status_code, 401)

    def test_unknown_state_is_rejected(self) -> None:
        client = self.make_client()
        client.get("/auth/login", follow_redirects=False)
        callback = client.get("/auth/callback?code=c&state=forged", follow_redirects=False)
        self.assertEqual(callback.status_code, 403)
        self.assertEqual(client.get("/api/me").status_code, 401)

    def test_state_is_single_use_even_after_a_successful_login(self) -> None:
        client = self.make_client()
        response = client.get("/auth/login", follow_redirects=False)
        state = httpx.URL(response.headers["location"]).params["state"]
        url = f"/auth/callback?code=c&state={state}"
        self.assertEqual(client.get(url, follow_redirects=False).status_code, 302)
        replay = client.get(url, follow_redirects=False)
        self.assertEqual(replay.status_code, 403)

    def test_state_is_consumed_even_when_the_exchange_fails(self) -> None:
        self.oauth.error = OAuthError(error="invalid_grant", description="bad code")
        client = self.make_client()
        response = client.get("/auth/login", follow_redirects=False)
        state = httpx.URL(response.headers["location"]).params["state"]
        url = f"/auth/callback?code=c&state={state}"
        self.assertEqual(client.get(url, follow_redirects=False).status_code, 403)

        self.oauth.error = None
        self.assertEqual(client.get(url, follow_redirects=False).status_code, 403)

    def test_provider_error_response_is_sanitized(self) -> None:
        client = self.make_client()
        response = client.get("/auth/login", follow_redirects=False)
        state = httpx.URL(response.headers["location"]).params["state"]
        callback = client.get(
            f"/auth/callback?code=auth-code-value&state={state}&error=access_denied"
            "&error_description=internal+detail",
            follow_redirects=False,
        )
        self.assertEqual(callback.status_code, 403)
        self.assertNotIn("auth-code-value", callback.text)
        self.assertNotIn("internal detail", callback.text)
        self.assertNotIn(GOOGLE_SECRET, callback.text)

    def test_login_rotates_the_identity_session(self) -> None:
        client = self.make_client()
        self.login(client)
        first_cookie = client.cookies[dashboard_auth.SESSION_COOKIE]

        self.login(client)
        second_cookie = client.cookies[dashboard_auth.SESSION_COOKIE]
        self.assertNotEqual(first_cookie, second_cookie)

        stale = TestClient(self.app, base_url=PUBLIC_URL, cookies={dashboard_auth.SESSION_COOKIE: first_cookie})
        self.addCleanup(stale.close)
        self.assertEqual(stale.get("/api/me").status_code, 401)

    def test_a_second_browser_login_does_not_sign_the_first_one_out(self) -> None:
        """Two open pages are a requirement (realtime), not an anomaly."""
        first = self.make_client()
        self.login(first)

        second = TestClient(self.app, base_url=PUBLIC_URL)
        self.addCleanup(second.close)
        self.login(second)

        self.assertNotEqual(
            first.cookies[dashboard_auth.SESSION_COOKIE], second.cookies[dashboard_auth.SESSION_COOKIE]
        )
        self.assertEqual(first.get("/api/me").status_code, 200)
        self.assertEqual(second.get("/api/me").status_code, 200)

    def test_logout_in_one_browser_leaves_the_other_signed_in(self) -> None:
        first = self.make_client()
        identity = self.login(first)
        second = TestClient(self.app, base_url=PUBLIC_URL)
        self.addCleanup(second.close)
        self.login(second)

        self.assertEqual(first.post("/auth/logout", headers=self.auth_headers(identity)).status_code, 204)
        self.assertEqual(first.get("/api/me").status_code, 401)
        self.assertEqual(second.get("/api/me").status_code, 200)


# ---------------------------------------------------------------------------
# 4. Real Authlib validation boundary
# ---------------------------------------------------------------------------


GOOGLE_METADATA = {
    "issuer": "https://accounts.google.com",
    "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_endpoint": "https://oauth2.googleapis.com/token",
    "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
    "userinfo_endpoint": "https://openidconnect.googleapis.com/v1/userinfo",
}


class RealAuthlibBoundaryTest(AppTestCase):
    """Drive real Authlib state/nonce validation with only the token POST stubbed."""

    def build_oauth_app(self, token: dict[str, Any]):
        # Same server-side state cache the production factory installs, so the
        # one-time state record is exercised, not bypassed.
        self.state_cache = dashboard_auth.OAuthStateCache(Path(self.settings()["auth_db_path"]))
        registry = OAuth(cache=self.state_cache)
        template = registry.register(
            "google",
            client_id="client-id.apps.googleusercontent.com",
            client_secret=GOOGLE_SECRET,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
        )
        recorder = self

        class StubbedTokenApp(type(template)):
            async def load_server_metadata(self):
                return dict(GOOGLE_METADATA)

            async def fetch_access_token(self, redirect_uri=None, **kwargs):
                recorder.fetch_kwargs = dict(kwargs)
                return dict(token)

            async def userinfo(self, **kwargs):
                recorder.userinfo_calls += 1
                return dict(ADMIN_USERINFO)

        self.fetch_kwargs: dict[str, Any] = {}
        self.userinfo_calls = 0
        return StubbedTokenApp(
            template.framework,
            "google",
            client_id="client-id.apps.googleusercontent.com",
            client_secret=GOOGLE_SECRET,
            client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
        )

    def test_authorization_url_carries_pkce_s256_nonce_and_openid_scope(self) -> None:
        client = self.make_client(oauth_client=self.build_oauth_app({"access_token": "at"}))
        response = client.get("/auth/login", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        url = httpx.URL(response.headers["location"])
        self.assertEqual(url.params["code_challenge_method"], "S256")
        self.assertTrue(url.params["code_challenge"])
        self.assertTrue(url.params["nonce"])
        self.assertEqual(url.params["response_type"], "code")
        self.assertEqual(set(url.params["scope"].split()), {"openid", "email", "profile"})
        self.assertEqual(url.params.get("access_type"), None)
        self.assertEqual(
            url.params["redirect_uri"], f"{PUBLIC_URL}/auth/callback", "callback must be derived from public_url"
        )

    def test_forged_state_is_rejected_before_any_token_exchange(self) -> None:
        client = self.make_client(oauth_client=self.build_oauth_app({"access_token": "at"}))
        client.get("/auth/login", follow_redirects=False)
        callback = client.get("/auth/callback?code=c&state=forged", follow_redirects=False)
        self.assertEqual(callback.status_code, 403)
        self.assertEqual(self.fetch_kwargs, {}, "token endpoint must not be reached on state mismatch")
        self.assertEqual(client.get("/api/me").status_code, 401)

    def test_bogus_id_token_fails_signature_validation(self) -> None:
        client = self.make_client(
            oauth_client=self.build_oauth_app({"access_token": "at", "id_token": "not.a.real.jwt"})
        )
        response = client.get("/auth/login", follow_redirects=False)
        state = httpx.URL(response.headers["location"]).params["state"]
        callback = client.get(f"/auth/callback?code=c&state={state}", follow_redirects=False)
        self.assertEqual(callback.status_code, 403)
        self.assertEqual(client.get("/api/me").status_code, 401)
        self.assertNotIn("not.a.real.jwt", callback.text)

    def test_token_exchange_replays_the_stored_pkce_verifier(self) -> None:
        client = self.make_client(oauth_client=self.build_oauth_app({"access_token": "at"}))
        response = client.get("/auth/login", follow_redirects=False)
        state = httpx.URL(response.headers["location"]).params["state"]
        client.get(f"/auth/callback?code=auth-code-value&state={state}", follow_redirects=False)
        self.assertEqual(self.fetch_kwargs.get("code"), "auth-code-value")
        self.assertTrue(self.fetch_kwargs.get("code_verifier"))

    def test_state_record_is_stored_server_side_and_consumed_once(self) -> None:
        """A captured signed cookie is not enough: the server row is one-time."""
        client = self.make_client(oauth_client=self.build_oauth_app({"access_token": "at"}))
        cache = self.app.state.oauth_state_cache
        self.assertEqual(cache.count(), 0)

        response = client.get("/auth/login", follow_redirects=False)
        state = httpx.URL(response.headers["location"]).params["state"]
        self.assertEqual(cache.count(), 1, "state/nonce/PKCE must be recorded server-side")

        client.get(f"/auth/callback?code=c&state={state}", follow_redirects=False)
        self.assertEqual(cache.count(), 0, "the state record must be consumed by the callback")

        replay = client.get(f"/auth/callback?code=c&state={state}", follow_redirects=False)
        self.assertEqual(replay.status_code, 403)

    def test_unsigned_token_without_nonce_never_falls_back_to_the_userinfo_endpoint(self) -> None:
        client = self.make_client(oauth_client=self.build_oauth_app({"access_token": "at"}))
        response = client.get("/auth/login", follow_redirects=False)
        state = httpx.URL(response.headers["location"]).params["state"]
        callback = client.get(f"/auth/callback?code=c&state={state}", follow_redirects=False)
        self.assertEqual(callback.status_code, 403)
        self.assertEqual(self.userinfo_calls, 0)


# ---------------------------------------------------------------------------
# 5. Session lifecycle
# ---------------------------------------------------------------------------


class ViewerRoleTest(AppTestCase):
    """A verified company.example Workspace account reads; only the admin acts."""

    def viewer_login(self, client: TestClient) -> dict[str, Any]:
        self.oauth.token = {"access_token": "at", "userinfo": dict(VIEWER_USERINFO)}
        return self.login(client)

    def test_viewer_signs_in_with_the_viewer_role(self) -> None:
        client = self.make_client()
        identity = self.viewer_login(client)
        self.assertEqual((identity["email"], identity["role"]), (VIEWER_EMAIL, "viewer"))
        self.assertTrue(identity["csrf_token"])
        self.oauth.token = {"access_token": "at", "userinfo": dict(ADMIN_USERINFO)}
        admin_client = self.make_client()
        self.assertEqual(self.login(admin_client)["role"], "admin")

    def test_viewer_reads_runs_retries_models_policy_and_the_stream(self) -> None:
        client = self.make_client()
        identity = self.viewer_login(client)
        head = "a" * 40
        run = self.failed_run(head)
        self.assertEqual(client.get("/api/runs").status_code, 200)
        self.assertEqual(client.get(f"/api/runs/{run['id']}").status_code, 200)
        self.assertEqual(client.get("/api/retries").status_code, 200)
        self.assertEqual(client.get("/api/models").status_code, 200)
        self.assertEqual(client.get("/api/settings/model-policy").status_code, 200)
        state = collect_sse(self.app, "/api/events?after=0", self.cookie(client), minimum_events=0)
        self.assertEqual(state["status"], 200)
        self.assertEqual(client.get("/api/usage").status_code, 403, "account-level llmux usage stays with the admin")
        logout = client.post("/auth/logout", headers=self.auth_headers(identity))
        self.assertEqual(logout.status_code, 204)
        self.assertEqual(client.get("/api/me").status_code, 401)

    def test_viewer_cannot_retry_or_change_the_policy(self) -> None:
        client = self.make_client()
        identity = self.viewer_login(client)
        head = "a" * 40
        self.github.pr = open_pr(head)
        run = self.failed_run(head)
        retry = client.post(f"/api/runs/{run['id']}/retry", content=json.dumps({"head_sha": head}), headers=self.auth_headers(identity))
        self.assertEqual(retry.status_code, 403, retry.text)
        self.assertEqual(retry.json()["error"], "admin role required")
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])
        put = client.put("/api/settings/model-policy", content=json.dumps({"model": "grok-4.6"}), headers=self.auth_headers(identity))
        self.assertEqual(put.status_code, 403, put.text)
        delete = client.delete("/api/settings/model-policy", headers=self.auth_headers(identity))
        self.assertEqual(delete.status_code, 403, delete.text)
        self.assertEqual(self.store.calls_named("set_setting"), [])
        self.assertEqual(self.store.calls_named("delete_setting"), [])

    def test_session_role_survives_the_store_and_an_old_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.sqlite3"
            conn = sqlite3.connect(str(path))
            conn.executescript(
                "CREATE TABLE sessions (id_hash TEXT PRIMARY KEY, email TEXT NOT NULL, sub TEXT NOT NULL,"
                " csrf_token TEXT NOT NULL, created_at TEXT NOT NULL, expires_at REAL NOT NULL);"
            )
            conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                (dashboard_auth.hash_session_id("old-cookie"), ADMIN_EMAIL, "1", "csrf", "2026-09-17T00:00:00+00:00", time.time() + 3600),
            )
            conn.commit(); conn.close()
            store = dashboard_auth.SessionStore(path)
            self.assertEqual(store.get("old-cookie")["role"], "admin", "pre-role rows were all the admin's")
            viewer = store.create(VIEWER_EMAIL, "555", role="viewer")
            self.assertEqual(store.get(viewer["id"])["role"], "viewer")
            with self.assertRaises(ValueError):
                store.create(VIEWER_EMAIL, "555", role="root")
            def stored_rows(session: dict) -> int:
                # Read the table itself: a refused get() looks the same whether
                # the row was deleted or merely ignored.
                with closing(sqlite3.connect(str(path))) as conn:
                    return conn.execute(
                        "SELECT COUNT(*) FROM sessions WHERE id_hash = ?",
                        (dashboard_auth.hash_session_id(session["id"]),),
                    ).fetchone()[0]

            # A role this build does not know (newer build, then a rollback) must
            # not resolve upward: the row is dropped and the browser re-authenticates.
            conn = sqlite3.connect(str(path))
            conn.execute("UPDATE sessions SET role = 'root' WHERE id_hash = ?", (dashboard_auth.hash_session_id(viewer["id"]),))
            conn.commit(); conn.close()
            self.assertEqual(stored_rows(viewer), 1)
            self.assertIsNone(store.get(viewer["id"]))
            self.assertEqual(stored_rows(viewer), 0, "the unknown-role row is gone")

            # Rollback contract for grants: a session minted for a granted address
            # is stored as "granted". A build without grants knows only admin and
            # viewer, so the same database row is dropped there, never trusted.
            granted = store.create(GRANTED_EMAIL, "777", role=dashboard_auth.GRANTED_ROLE)
            self.assertEqual(store.get(granted["id"])["role"], "granted")
            with mock.patch.object(dashboard_auth, "ROLES", ("admin", "viewer")):
                self.assertIsNone(store.get(granted["id"]), "an older build drops the granted session")
            self.assertEqual(stored_rows(granted), 0, "and the row is deleted, not just ignored")
            self.assertIsNone(store.get(granted["id"]), "so the newer build cannot revive it either")


class SessionLifecycleTest(AppTestCase):
    def test_logout_requires_csrf_token(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        response = client.post("/auth/logout", headers={"Origin": PUBLIC_URL})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(client.get("/api/me").status_code, 200, "session must survive a rejected logout")

    def test_logout_requires_same_origin(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        response = client.post(
            "/auth/logout", headers=self.auth_headers(identity, Origin="https://evil.example")
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(client.get("/api/me").status_code, 200)

    def test_expired_session_is_denied(self) -> None:
        client = self.make_client()
        self.login(client)
        with mock.patch.object(
            dashboard_auth, "_now", return_value=time.time() + dashboard_auth.SESSION_TTL_SECONDS + 1
        ):
            self.assertEqual(client.get("/api/me").status_code, 401)
            self.assertEqual(client.get("/api/runs").status_code, 401)

    def test_absolute_expiry_is_eight_hours(self) -> None:
        self.assertEqual(dashboard_auth.SESSION_TTL_SECONDS, 8 * 60 * 60)

    def test_session_database_is_private(self) -> None:
        client = self.make_client()
        self.login(client)
        db_path = self.service_root / "data" / "dashboard-auth.sqlite3"
        self.assertTrue(db_path.exists())
        self.assertEqual(stat.S_IMODE(db_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(db_path.parent.stat().st_mode), 0o700)

    def test_session_store_persists_no_plaintext_cookie_value(self) -> None:
        client = self.make_client()
        self.login(client)
        cookie = client.cookies[dashboard_auth.SESSION_COOKIE]
        db_bytes = (self.service_root / "data" / "dashboard-auth.sqlite3").read_bytes()
        self.assertNotIn(cookie.encode("ascii"), db_bytes)

    def test_unknown_cookie_value_is_denied(self) -> None:
        client = self.make_client()
        self.login(client)
        client.cookies.set(dashboard_auth.SESSION_COOKIE, "forged-session-value", domain="rol.company.example")
        self.assertEqual(client.get("/api/me").status_code, 401)


# ---------------------------------------------------------------------------
# 6. Run read API
# ---------------------------------------------------------------------------


class RunsApiTest(AppTestCase):
    def test_projection_exposes_exactly_the_contract_fields(self) -> None:
        client = self.make_client()
        self.login(client)
        self.store.force_run(
            status="failed",
            stage="agents",
            error="quorum not reached",
            successful_agents=2,
            total_agents=3,
            agents=[{"id": "correctness", "status": "failed", "summary": "timeout"}],
            workspace="/Users/ci/work/review-of-legends/data/work/owner-repo-7",
            prompt="raw agent prompt text",
        )
        payload = client.get("/api/runs").json()
        self.assertEqual(list(payload), ["runs", "next_offset"])
        run = payload["runs"][0]
        self.assertEqual(
            set(run),
            {
                "id",
                "repo",
                "pr",
                "head_sha",
                "title",
                "url",
                "trigger",
                "status",
                "stage",
                "created_at",
                "updated_at",
                "agents",
                "review_url",
                "error",
                "successful_agents",
                "total_agents",
                "llm",
                "retry_of",
                "reusable_agents",
            },
        )
        body = json.dumps(payload)
        self.assertNotIn("workspace", body)
        self.assertNotIn("raw agent prompt text", body)
        self.assertNotIn("/Users/ci", body)

    def test_agent_entries_are_reduced_to_id_status_summary(self) -> None:
        client = self.make_client()
        self.login(client)
        self.store.force_run(
            agents=[
                {
                    "id": "security",
                    "status": "succeeded",
                    "summary": "looks fine",
                    "log_path": "/opt/review-of-legends/logs/security.log",
                    "prompt": "raw prompt",
                }
            ]
        )
        run = client.get("/api/runs").json()["runs"][0]
        self.assertEqual(run["agents"], [{"id": "security", "status": "succeeded", "summary": "looks fine"}])

    def test_text_fields_are_redacted_and_bounded(self) -> None:
        client = self.make_client()
        self.login(client)
        self.store.force_run(
            status="failed",
            error="clone failed in /Users/ci/work/owner-repo using token ghp_abcdefghijklmnopqrstuvwxyz0123456789 " + "x" * 5000,
        )
        run = client.get("/api/runs").json()["runs"][0]
        self.assertNotIn("/Users/ci", run["error"])
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz0123456789", run["error"])
        self.assertLessEqual(len(run["error"]), dashboard_web.MAX_TEXT_CHARS)

    def test_next_offset_reflects_a_full_page(self) -> None:
        client = self.make_client()
        self.login(client)
        for index in range(3):
            self.store.force_run(pr=index + 1)
        self.assertEqual(client.get("/api/runs?limit=2").json()["next_offset"], 2)
        self.assertIsNone(client.get("/api/runs?limit=5").json()["next_offset"])

    def test_limit_is_bounded_at_200(self) -> None:
        client = self.make_client()
        self.login(client)
        self.assertEqual(client.get("/api/runs?limit=1000").status_code, 200)
        limits = [call[2]["limit"] for call in self.store.calls_named("list_runs")]
        self.assertEqual(limits[-1], 200)

    def test_malformed_paging_and_filters_are_rejected(self) -> None:
        client = self.make_client()
        self.login(client)
        for query in ("limit=abc", "limit=-1", "offset=-1", "offset=abc", "status=cancelled", "repo=other/repo"):
            with self.subTest(query=query):
                response = client.get(f"/api/runs?{query}")
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("error", response.json())

    def test_baseline_skipped_status_is_an_accepted_filter(self) -> None:
        client = self.make_client()
        self.login(client)
        response = client.get("/api/runs?status=skipped")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.calls_named("list_runs")[-1][2]["status"], "skipped")

    def test_status_and_repo_filters_are_forwarded(self) -> None:
        client = self.make_client()
        self.login(client)
        client.get("/api/runs?status=failed&repo=owner/repo&limit=10&offset=5")
        call = self.store.calls_named("list_runs")[-1]
        self.assertEqual(
            call[2], {"status": "failed", "repo": "owner/repo", "limit": 10, "offset": 5}
        )

    def test_legacy_and_baseline_rows_keep_their_labels(self) -> None:
        client = self.make_client()
        self.login(client)
        self.store.force_run(trigger="legacy", status="succeeded")
        self.store.force_run(trigger="baseline", status="skipped")
        runs = client.get("/api/runs").json()["runs"]
        self.assertEqual({run["trigger"] for run in runs}, {"legacy", "baseline"})
        baseline = [run for run in runs if run["trigger"] == "baseline"][0]
        self.assertEqual(baseline["status"], "skipped", "baseline history must not be projected as success")

    def test_single_run_and_missing_run(self) -> None:
        client = self.make_client()
        self.login(client)
        run = self.store.force_run()
        response = client.get(f"/api/runs/{run['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["run"]["id"], run["id"])
        self.assertEqual(client.get("/api/runs/does-not-exist").status_code, 404)

    def test_store_unavailable_reports_503_not_empty_data(self) -> None:
        client = self.make_client(store=None)
        self.login(client)
        response = client.get("/api/runs")
        self.assertEqual(response.status_code, 503)
        self.assertIn("error", response.json())


class RetryQueueApiTest(AppTestCase):
    """GET /api/retries (api-contract.md:16) so queued dispatch survives a refresh."""

    def test_requires_authentication(self) -> None:
        client = self.make_client()
        self.assertEqual(client.get("/api/retries").status_code, 401)

    def test_comment_request_displays_github_requester_without_internal_options(self) -> None:
        client = self.make_client()
        self.login(client)
        self.store.enqueue_retry("owner/repo", 7, "a" * 40, "github:Alice_org", options={
            "kind": "rereview", "scope": "all", "source_key": "owner/repo#issue#42",
            "seen_at": "2026-10-15T01:00:00Z",
        })
        entry = client.get("/api/retries").json()["requests"][0]
        self.assertEqual(entry.get("requested_by"), "github:Alice_org")
        self.assertNotIn("source_key", entry)
        self.assertNotIn("options", entry)

    def test_projects_only_the_contract_fields(self) -> None:
        client = self.make_client()
        self.login(client)
        request = self.store.enqueue_retry("owner/repo", 7, "a" * 40, ADMIN_EMAIL, retry_of="run-0001")
        self.store._requests[request["id"]].update(
            {
                "run_id": "run-0002",
                "error": "clone failed in /Users/ci/work token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
                "updated_at": "2026-09-11T00:00:01+00:00",
                "requested_by": ADMIN_EMAIL,
            }
        )

        payload = client.get("/api/retries").json()
        self.assertEqual(list(payload), ["requests", "next_offset"])
        entry = payload["requests"][0]
        self.assertEqual(
            set(entry),
            {"id", "status", "repo", "pr", "head_sha", "retry_of", "created_at", "run_id", "error", "model_policy", "scope", "kind"},
        )
        self.assertEqual(entry["status"], "queued")
        self.assertEqual(entry["retry_of"], "run-0001")
        self.assertNotIn("/Users/ci", entry["error"])
        self.assertNotIn("ghp_", entry["error"])
        self.assertNotIn("updated_at", json.dumps(payload))

    def test_paging_bounds_and_filters(self) -> None:
        client = self.make_client()
        self.login(client)
        for pr in (1, 2, 3):
            self.store.enqueue_retry("owner/repo", pr, "a" * 40, ADMIN_EMAIL)

        self.assertEqual(client.get("/api/retries?limit=2").json()["next_offset"], 2)
        self.assertIsNone(client.get("/api/retries?limit=9").json()["next_offset"])
        client.get("/api/retries?limit=1000")
        self.assertEqual(self.store.calls_named("list_retries")[-1][2]["limit"], 200)
        for query in ("limit=abc", "offset=-1", "status=cancelled"):
            with self.subTest(query=query):
                self.assertEqual(client.get(f"/api/retries?{query}").status_code, 400)

    def test_queued_status_filter_is_forwarded(self) -> None:
        client = self.make_client()
        self.login(client)
        client.get("/api/retries?status=queued&limit=10&offset=0")
        self.assertEqual(
            self.store.calls_named("list_retries")[-1][2],
            {"status": "queued", "repo": None, "limit": 10, "offset": 0},
        )

    def test_store_without_retry_listing_reports_503(self) -> None:
        client = self.make_client(store=StoreWithoutRetryListing())
        self.login(client)
        response = client.get("/api/retries")
        self.assertEqual(response.status_code, 503)
        self.assertIn("error", response.json())


# ---------------------------------------------------------------------------
# 7. Retry dispatch
# ---------------------------------------------------------------------------


class RetryApiTest(AppTestCase):
    def failed_run(self, head_sha: str = "a" * 40, **fields: Any) -> dict[str, Any]:
        run = self.store.force_run(head_sha=head_sha, **fields)
        self.store.update_run(run["id"], fields.get("status", "failed"), error="quorum not reached")
        return self.store.get_run(run["id"])  # type: ignore[return-value]

    def test_retry_enqueues_with_server_authenticated_identity(self) -> None:
        head = "a" * 40
        self.github.pr = open_pr(head)
        client = self.make_client()
        identity = self.login(client)
        run = self.failed_run(head)

        response = client.post(
            f"/api/runs/{run['id']}/retry",
            content=json.dumps({"head_sha": head}),
            headers=self.auth_headers(identity),
        )
        self.assertEqual(response.status_code, 202, response.text)
        request = response.json()["request"]
        self.assertEqual(request["repo"], "owner/repo")
        self.assertEqual(request["pr"], 7)
        self.assertEqual(request["head_sha"], head)
        self.assertEqual(request["retry_of"], run["id"])
        self.assertEqual(request["status"], "queued")

        call = self.store.calls_named("enqueue_retry")[-1]
        self.assertEqual(call[1], ("owner/repo", 7, head, ADMIN_EMAIL))
        self.assertEqual(self.github_repos, ["owner/repo"])
        self.assertEqual(self.github.calls, [7])

    def test_duplicate_click_returns_the_same_request(self) -> None:
        head = "a" * 40
        self.github.pr = open_pr(head)
        client = self.make_client()
        identity = self.login(client)
        run = self.failed_run(head)
        body = json.dumps({"head_sha": head})

        first = client.post(f"/api/runs/{run['id']}/retry", content=body, headers=self.auth_headers(identity))
        second = client.post(f"/api/runs/{run['id']}/retry", content=body, headers=self.auth_headers(identity))
        self.assertEqual((first.status_code, second.status_code), (202, 202))
        self.assertEqual(second.json()["request"]["id"], first.json()["request"]["id"])

    def test_missing_or_wrong_csrf_token_is_rejected(self) -> None:
        head = "a" * 40
        self.github.pr = open_pr(head)
        client = self.make_client()
        identity = self.login(client)
        run = self.failed_run(head)
        body = json.dumps({"head_sha": head})

        no_csrf = client.post(
            f"/api/runs/{run['id']}/retry",
            content=body,
            headers={"Origin": PUBLIC_URL, "Content-Type": "application/json"},
        )
        wrong_csrf = client.post(
            f"/api/runs/{run['id']}/retry",
            content=body,
            headers=self.auth_headers(identity, **{"X-CSRF-Token": "wrong-token"}),
        )
        self.assertEqual((no_csrf.status_code, wrong_csrf.status_code), (403, 403))
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_foreign_or_missing_origin_is_rejected(self) -> None:
        head = "a" * 40
        self.github.pr = open_pr(head)
        client = self.make_client()
        identity = self.login(client)
        run = self.failed_run(head)
        body = json.dumps({"head_sha": head})

        foreign = client.post(
            f"/api/runs/{run['id']}/retry",
            content=body,
            headers=self.auth_headers(identity, Origin="https://evil.example"),
        )
        headers = self.auth_headers(identity)
        headers.pop("Origin")
        missing = client.post(f"/api/runs/{run['id']}/retry", content=body, headers=headers)
        self.assertEqual((foreign.status_code, missing.status_code), (403, 403))
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_wrong_content_type_and_malformed_body_are_rejected(self) -> None:
        head = "a" * 40
        self.github.pr = open_pr(head)
        client = self.make_client()
        identity = self.login(client)
        run = self.failed_run(head)

        wrong_type = client.post(
            f"/api/runs/{run['id']}/retry",
            content=json.dumps({"head_sha": head}),
            headers=self.auth_headers(identity, **{"Content-Type": "text/plain"}),
        )
        broken = client.post(
            f"/api/runs/{run['id']}/retry", content="{not json", headers=self.auth_headers(identity)
        )
        missing_field = client.post(
            f"/api/runs/{run['id']}/retry", content=json.dumps({}), headers=self.auth_headers(identity)
        )
        wrong_type_field = client.post(
            f"/api/runs/{run['id']}/retry",
            content=json.dumps({"head_sha": 12}),
            headers=self.auth_headers(identity),
        )
        self.assertEqual(
            [wrong_type.status_code, broken.status_code, missing_field.status_code, wrong_type_field.status_code],
            [400, 400, 400, 400],
        )
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_unknown_run_is_404(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        response = client.post(
            "/api/runs/does-not-exist/retry",
            content=json.dumps({"head_sha": "a" * 40}),
            headers=self.auth_headers(identity),
        )
        self.assertEqual(response.status_code, 404)

    def test_successful_run_is_not_retryable(self) -> None:
        head = "a" * 40
        self.github.pr = open_pr(head)
        client = self.make_client()
        identity = self.login(client)
        run = self.store.force_run(head_sha=head)
        self.store.update_run(run["id"], "succeeded", review_url="https://github.invalid/r/1")

        response = client.post(
            f"/api/runs/{run['id']}/retry",
            content=json.dumps({"head_sha": head}),
            headers=self.auth_headers(identity),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])
        self.assertEqual(self.github.calls, [], "no upstream call for a non-retryable run")

    def test_interrupted_and_stale_runs_are_retryable(self) -> None:
        head = "a" * 40
        self.github.pr = open_pr(head)
        client = self.make_client()
        identity = self.login(client)
        for status in ("interrupted", "stale"):
            with self.subTest(status=status):
                run = self.store.force_run(head_sha=head)
                self.store.update_run(run["id"], status)
                response = client.post(
                    f"/api/runs/{run['id']}/retry",
                    content=json.dumps({"head_sha": head}),
                    headers=self.auth_headers(identity),
                )
                self.assertEqual(response.status_code, 202, response.text)

    def test_submitted_head_must_match_the_failed_attempt(self) -> None:
        self.github.pr = open_pr("b" * 40)
        client = self.make_client()
        identity = self.login(client)
        run = self.failed_run("a" * 40)
        response = client.post(
            f"/api/runs/{run['id']}/retry",
            content=json.dumps({"head_sha": "b" * 40}),
            headers=self.auth_headers(identity),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_moved_head_is_rejected(self) -> None:
        head = "a" * 40
        self.github.pr = open_pr("c" * 40)  # PR advanced since the failure
        client = self.make_client()
        identity = self.login(client)
        run = self.failed_run(head)
        response = client.post(
            f"/api/runs/{run['id']}/retry",
            content=json.dumps({"head_sha": head}),
            headers=self.auth_headers(identity),
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("error", response.json())
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_closed_pr_is_rejected(self) -> None:
        head = "a" * 40
        pr = open_pr(head)
        pr["state"] = "closed"
        self.github.pr = pr
        client = self.make_client()
        identity = self.login(client)
        run = self.failed_run(head)
        response = client.post(
            f"/api/runs/{run['id']}/retry",
            content=json.dumps({"head_sha": head}),
            headers=self.auth_headers(identity),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_upstream_failure_is_a_sanitized_502(self) -> None:
        head = "a" * 40
        self.github.error = (
            "GitHub API GET https://api.github.com/repos/owner/repo/pulls/7 failed: 401 "
            '{"message":"Bad credentials"} token=ghp_abcdefghijklmnopqrstuvwxyz0123456789'
        )
        client = self.make_client()
        identity = self.login(client)
        run = self.failed_run(head)
        response = client.post(
            f"/api/runs/{run['id']}/retry",
            content=json.dumps({"head_sha": head}),
            headers=self.auth_headers(identity),
        )
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("ghp_", response.text)
        self.assertNotIn("Bad credentials", response.text)
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_client_cannot_choose_repository_or_smuggle_commands(self) -> None:
        head = "a" * 40
        self.github.pr = open_pr(head)
        client = self.make_client()
        identity = self.login(client)
        run = self.failed_run(head)
        response = client.post(
            f"/api/runs/{run['id']}/retry",
            content=json.dumps(
                {
                    "head_sha": head,
                    "repo": "attacker/repo",
                    "command": "rm -rf /",
                    "force": True,
                    "requested_by": "attacker@example.com",
                }
            ),
            headers=self.auth_headers(identity),
        )
        self.assertEqual(response.status_code, 202, response.text)
        call = self.store.calls_named("enqueue_retry")[-1]
        self.assertEqual(call[1], ("owner/repo", 7, head, ADMIN_EMAIL))
        self.assertEqual(self.github_repos, ["owner/repo"])

    def test_run_whose_review_post_outcome_is_unknown_is_refused(self) -> None:
        """A retry after a possible POST would duplicate the review (W2 rules)."""
        from code_review_service import dashboard_execution

        head = "a" * 40
        self.github.pr = open_pr(head)
        client = self.make_client()
        identity = self.login(client)

        for fields in (
            {"stage": dashboard_execution.POSTING_UNVERIFIED_STAGE},
            {"stage": dashboard_execution.POSTING_STAGE},
            {"review_url": "https://github.invalid/owner/repo/pull/7#pullrequestreview-1"},
        ):
            with self.subTest(fields=fields):
                self.store = FakeDashboardStore()
                client = self.make_client()
                identity = self.login(client)
                run = self.store.force_run(head_sha=head)
                self.store.update_run(run["id"], "failed", error="interrupted", **fields)

                response = client.post(
                    f"/api/runs/{run['id']}/retry",
                    content=json.dumps({"head_sha": head}),
                    headers=self.auth_headers(identity),
                )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self.store.calls_named("enqueue_retry"), [])
                self.assertEqual(self.github.calls, [], "no upstream call once the run is refused")

    def test_run_for_unconfigured_repository_is_refused(self) -> None:
        head = "a" * 40
        self.github.pr = open_pr(head)
        client = self.make_client()
        identity = self.login(client)
        run = self.store.force_run(repo="stranger/repo", head_sha=head)
        self.store.update_run(run["id"], "failed", error="boom")
        response = client.post(
            f"/api/runs/{run['id']}/retry",
            content=json.dumps({"head_sha": head}),
            headers=self.auth_headers(identity),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])
        self.assertEqual(self.github_repos, [])

    def test_default_github_client_uses_the_configured_gh_binary(self) -> None:
        """The production path (no injected factory) must honour config gh_bin."""
        head = "a" * 40
        self.config_path.write_text(
            json.dumps(
                {
                    "repositories": ["owner/repo"],
                    "agents": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                    "gh_bin": "/opt/homebrew/bin/gh",
                }
            ),
            encoding="utf-8",
        )
        client = self.make_client(github_client_factory=None)
        identity = self.login(client)
        run = self.failed_run(head)

        with mock.patch.object(dashboard_web.svc, "GitHubClient") as constructor:
            constructor.return_value = FakeGitHubClient(open_pr(head))
            response = client.post(
                f"/api/runs/{run['id']}/retry",
                content=json.dumps({"head_sha": head}),
                headers=self.auth_headers(identity),
            )
        self.assertEqual(response.status_code, 202, response.text)
        constructor.assert_called_once_with("owner/repo", gh_bin="/opt/homebrew/bin/gh")

    def test_configured_repositories_are_re_read_per_request(self) -> None:
        head = "a" * 40
        self.github.pr = open_pr(head)
        client = self.make_client()
        identity = self.login(client)
        run = self.failed_run(head)
        self.write_config(["other/repo"])  # config changed under the running server
        response = client.post(
            f"/api/runs/{run['id']}/retry",
            content=json.dumps({"head_sha": head}),
            headers=self.auth_headers(identity),
        )
        self.assertEqual(response.status_code, 409)


# ---------------------------------------------------------------------------
# 8. SSE
# ---------------------------------------------------------------------------


def collect_sse(
    app: Any,
    path: str,
    cookie: str,
    *,
    headers: dict[str, str] | None = None,
    minimum_events: int = 1,
    stop_on: tuple[str, ...] = ("auth-expired",),
    timeout: float = 5.0,
    on_block: Any = None,
) -> dict[str, Any]:
    """Drive the ASGI app directly, injecting ``http.disconnect`` once satisfied.

    An event stream never ends on its own, so a blocking HTTP client would wait
    for a body that has no last byte (and, on a single test portal, deadlock on
    any nested request). Driving the app with our own ``receive`` gives the two
    things the test actually needs: the real middleware/handler path, and a
    disconnect we control. ``anyio.fail_after`` bounds every case.
    """
    import anyio

    state: dict[str, Any] = {
        "status": None,
        "headers": {},
        "buffer": "",
        "blocks": [],
        "disconnect": False,
    }

    async def main() -> None:
        url = httpx.URL(PUBLIC_URL + path)
        raw_headers = [
            (b"host", url.netloc),
            (b"cookie", f"{dashboard_auth.SESSION_COOKIE}={cookie}".encode("ascii")),
            (b"accept", b"text/event-stream"),
        ]
        for key, value in (headers or {}).items():
            raw_headers.append((key.lower().encode("ascii"), value.encode("ascii")))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": url.path,
            "raw_path": url.path.encode("ascii"),
            "query_string": url.query,
            "root_path": "",
            "headers": raw_headers,
            "client": ("127.0.0.1", 50000),
            "server": (url.host, 443),
            "state": {},
        }

        async def receive() -> dict[str, Any]:
            if state["disconnect"]:
                return {"type": "http.disconnect"}
            await anyio.sleep(0.01)
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["headers"] = {
                    key.decode("latin-1").lower(): value.decode("latin-1")
                    for key, value in message["headers"]
                }
            elif message["type"] == "http.response.body":
                state["buffer"] += message.get("body", b"").decode("utf-8")
                while "\n\n" in state["buffer"]:
                    block, state["buffer"] = state["buffer"].split("\n\n", 1)
                    if not block:
                        continue
                    state["blocks"].append(block)
                    if on_block is not None:
                        on_block(block, state)
                updates = [block for block in state["blocks"] if "event: update" in block]
                terminal = any(
                    f"event: {name}" in block for name in stop_on for block in state["blocks"]
                )
                if len(updates) >= minimum_events or terminal:
                    state["disconnect"] = True

        with anyio.fail_after(timeout):
            await app(scope, receive, send)

    anyio.run(main)
    state["data_blocks"] = [block for block in state["blocks"] if "event: update" in block]
    state["frames"] = [
        line.split("event: ", 1)[1]
        for block in state["blocks"]
        for line in block.splitlines()
        if line.startswith("event: ")
    ]
    return state


class EventStreamTest(AppTestCase):
    def cookie(self, client: TestClient) -> str:
        return client.cookies[dashboard_auth.SESSION_COOKIE]

    def test_stream_headers_and_replay_from_last_event_id(self) -> None:
        client = self.make_client()
        self.login(client)
        run = self.store.force_run()
        self.store.update_run(run["id"], "failed", error="boom")
        all_events = self.store.events_after(0)
        self.assertGreaterEqual(len(all_events), 2)

        state = collect_sse(
            self.app,
            "/api/events",
            self.cookie(client),
            headers={"Last-Event-ID": str(all_events[0]["id"])},
            minimum_events=len(all_events) - 1,
        )
        self.assertEqual(state["status"], 200)
        self.assertIn("text/event-stream", state["headers"]["content-type"])
        self.assertEqual(state["headers"]["cache-control"], "no-store")
        self.assertEqual(state["headers"]["x-accel-buffering"], "no")

        self.assertEqual(len(state["data_blocks"]), len(all_events) - 1)
        first = state["data_blocks"][0]
        self.assertIn(f"id: {all_events[1]['id']}", first)
        self.assertIn("event: update", first)
        payload = json.loads(first.split("data: ", 1)[1])
        self.assertEqual(set(payload), {"id", "kind", "run_id", "payload", "created_at"})
        self.assertEqual(payload["id"], all_events[1]["id"])

        # The anchor frame precedes the replay and reports where it started.
        self.assertEqual(state["frames"][0], "ready")
        ready = state["blocks"][0]
        self.assertIn(f"id: {all_events[0]['id']}", ready)
        self.assertEqual(json.loads(ready.split("data: ", 1)[1]), {"cursor": all_events[0]["id"]})

    def test_fresh_connection_starts_at_the_tail_instead_of_replaying_history(self) -> None:
        client = self.make_client()
        self.login(client)
        run = self.store.force_run()
        self.store.update_run(run["id"], "failed", error="boom")
        backlog = self.store.events_after(0)
        self.assertGreaterEqual(len(backlog), 2)

        state = collect_sse(
            self.app,
            "/api/events",
            self.cookie(client),
            minimum_events=10**6,
            stop_on=("ready",),
            timeout=5.0,
        )
        self.assertEqual(state["frames"], ["ready"])
        self.assertEqual(state["data_blocks"], [], "an append-only backlog must not be replayed")
        ready = state["blocks"][0]
        self.assertEqual(json.loads(ready.split("data: ", 1)[1]), {"cursor": backlog[-1]["id"]})
        self.assertIn(f"id: {backlog[-1]['id']}", ready)
        self.assertTrue(self.store.calls_named("latest_event_id"), "the tail must come from one query")

    def test_explicit_after_zero_still_replays_everything(self) -> None:
        client = self.make_client()
        self.login(client)
        run = self.store.force_run()
        self.store.update_run(run["id"], "failed", error="boom")
        backlog = self.store.events_after(0)

        state = collect_sse(
            self.app, "/api/events?after=0", self.cookie(client), minimum_events=len(backlog)
        )
        self.assertEqual(len(state["data_blocks"]), len(backlog))
        self.assertEqual(json.loads(state["blocks"][0].split("data: ", 1)[1]), {"cursor": 0})

    def test_reconnect_resumes_exactly_after_the_anchor(self) -> None:
        """Native reconnect: the anchor id from one stream is the next cursor."""
        client = self.make_client()
        self.login(client)
        cookie = self.cookie(client)
        run = self.store.force_run()
        first = collect_sse(
            self.app, "/api/events", cookie, minimum_events=10**6, stop_on=("ready",)
        )
        anchor = json.loads(first["blocks"][0].split("data: ", 1)[1])["cursor"]

        self.store.update_run(run["id"], "failed", error="boom")  # happens while offline
        missed = [event for event in self.store.events_after(anchor)]
        self.assertTrue(missed)

        second = collect_sse(
            self.app,
            "/api/events",
            cookie,
            headers={"Last-Event-ID": str(anchor)},
            minimum_events=len(missed),
        )
        self.assertEqual(
            [json.loads(block.split("data: ", 1)[1])["id"] for block in second["data_blocks"]],
            [event["id"] for event in missed],
        )

    def test_tail_is_derived_without_latest_event_id_too(self) -> None:
        """Until W1 lands the query, the tail is found by bounded scanning."""
        store = StoreWithoutLatestEventId()
        client = self.make_client(store=store)
        self.login(client)
        run = store.force_run()
        store.update_run(run["id"], "failed", error="boom")
        backlog = store.events_after(0)

        state = collect_sse(
            self.app, "/api/events", self.cookie(client), minimum_events=10**6, stop_on=("ready",)
        )
        self.assertEqual(state["data_blocks"], [])
        self.assertEqual(
            json.loads(state["blocks"][0].split("data: ", 1)[1]), {"cursor": backlog[-1]["id"]}
        )

    def test_heartbeat_is_a_named_event_on_the_wire(self) -> None:
        """A bare ``: heartbeat`` comment never reaches addEventListener."""
        client = self.make_client()
        self.login(client)

        state = collect_sse(
            self.app,
            "/api/events",
            self.cookie(client),
            minimum_events=10**6,
            stop_on=("heartbeat",),
            timeout=5.0,
        )
        self.assertIn("heartbeat", state["frames"])
        heartbeat = [block for block in state["blocks"] if "event: heartbeat" in block][0]
        self.assertEqual(heartbeat.splitlines(), ["event: heartbeat", "data: {}"])
        self.assertFalse(
            any(block.startswith(":") for block in state["blocks"]), f"comment frame found: {state['blocks']}"
        )

    def test_after_cursor_query_is_honoured_on_first_connect(self) -> None:
        client = self.make_client()
        self.login(client)
        run = self.store.force_run()
        self.store.update_run(run["id"], "failed", error="boom")
        events = self.store.events_after(0)

        state = collect_sse(
            self.app, f"/api/events?after={events[-2]['id']}", self.cookie(client), minimum_events=1
        )
        self.assertIn(f"id: {events[-1]['id']}", state["data_blocks"][0])

    def test_invalid_cursor_is_rejected(self) -> None:
        client = self.make_client()
        self.login(client)
        for headers, query in (
            ({"Last-Event-ID": "abc"}, ""),
            ({"Last-Event-ID": "-1"}, ""),
            ({}, "?after=abc"),
            ({}, "?after=-5"),
        ):
            with self.subTest(headers=headers, query=query):
                response = client.get(f"/api/events{query}", headers=headers)
                self.assertEqual(response.status_code, 400)

    def test_batches_are_bounded(self) -> None:
        client = self.make_client()
        self.login(client)
        self.store.force_run()
        collect_sse(self.app, "/api/events?after=0", self.cookie(client), minimum_events=1)
        limits = [call[2]["limit"] for call in self.store.calls_named("events_after")]
        self.assertTrue(limits)
        self.assertEqual(max(limits), dashboard_web.MAX_EVENT_BATCH)

    def test_revoked_session_ends_the_stream_with_auth_expired(self) -> None:
        client = self.make_client()
        self.login(client)
        self.store.force_run()
        cookie = self.cookie(client)
        sessions = self.app.state.sessions

        def revoke_after_first_event(block: str, state: dict[str, Any]) -> None:
            # Same server-side revocation POST /auth/logout performs (covered in
            # CallbackFlowTest); here the point is the in-stream recheck.
            if "data:" in block and not state.get("revoked"):
                state["revoked"] = True
                sessions.revoke(cookie)

        state = collect_sse(
            self.app,
            "/api/events",
            cookie,
            minimum_events=10**6,
            timeout=10.0,
            on_block=revoke_after_first_event,
        )
        self.assertTrue(
            any("event: auth-expired" in block for block in state["blocks"]),
            f"expected auth-expired, got {state['blocks']}",
        )

    def test_expired_session_ends_the_stream_with_auth_expired(self) -> None:
        client = self.make_client()
        self.login(client)
        self.store.force_run()
        cookie = self.cookie(client)

        def expire_after_first_event(block: str, state: dict[str, Any]) -> None:
            if "data:" in block and not state.get("patched"):
                state["patched"] = True
                patch = mock.patch.object(
                    dashboard_auth, "_now", return_value=time.time() + dashboard_auth.SESSION_TTL_SECONDS + 1
                )
                patch.start()
                self.addCleanup(patch.stop)

        state = collect_sse(
            self.app, "/api/events", cookie, minimum_events=10**6, timeout=10.0, on_block=expire_after_first_event
        )
        self.assertTrue(any("event: auth-expired" in block for block in state["blocks"]))

    def test_unauthenticated_stream_is_denied(self) -> None:
        client = self.make_client()
        response = client.get("/api/events")
        self.assertEqual(response.status_code, 401)

    def test_event_payloads_are_projected_and_redacted(self) -> None:
        client = self.make_client()
        self.login(client)
        run = self.store.force_run()
        self.store.force_event(
            "run_updated",
            run["id"],
            {
                "status": "failed",
                "error": "clone failed in /Users/ci/work token ghp_abcdefghijklmnopqrstuvwxyz0123456789 " + "y" * 5000,
                "agents": [
                    {
                        "id": "security",
                        "status": "failed",
                        "summary": "boom",
                        "prompt": "raw agent prompt text",
                        "log_path": "/opt/review-of-legends/logs/security.log",
                    }
                ],
                "gh_token": "ghp_zyxwvutsrqponmlkjihgfedcba9876543210",
                "workspace": "/Users/ci/work/review-of-legends",
            },
        )
        state = collect_sse(
            self.app, "/api/events?after=0", client.cookies[dashboard_auth.SESSION_COOKIE], minimum_events=2
        )
        blocks = state["blocks"]
        raw = "\n".join(blocks)
        for leak in (
            "/Users/ci",
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "ghp_zyxwvutsrqponmlkjihgfedcba9876543210",
            "raw agent prompt text",
            "/opt/review-of-legends",
        ):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, raw)

        update = json.loads([block for block in blocks if '"kind": "run_updated"' in block][-1].split("data: ", 1)[1])
        self.assertEqual(update["payload"]["status"], "failed")
        self.assertEqual(
            update["payload"]["agents"], [{"id": "security", "status": "failed", "summary": "boom"}]
        )
        self.assertLessEqual(len(update["payload"]["error"]), dashboard_web.MAX_TEXT_CHARS)
        self.assertNotIn("gh_token", update["payload"])
        self.assertNotIn("workspace", update["payload"])


# ---------------------------------------------------------------------------
# 9. llmux usage proxy
# ---------------------------------------------------------------------------


class UsageApiTest(AppTestCase):
    def test_allowlisted_projection_and_no_secret_fields(self) -> None:
        transport = usage_transport()
        client = self.make_client(usage_transport=transport)
        self.login(client)
        response = client.get("/api/usage")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["available"])
        self.assertTrue(body["fetched_at"])
        account = body["accounts"][0]
        self.assertEqual(
            set(account),
            {
                "name",
                "type",
                "status",
                "healthy",
                "paused",
                "in_flight",
                "cooldown_until",
                "five_hour",
                "seven_day",
                "totals",
                "session",
            },
        )
        self.assertEqual(
            set(account["five_hour"]),
            # The two server-computed freshness labels are documented extensions:
            # upstream fetched_at_ms can be ~25 min old even on a fresh fetch.
            {"utilization", "resets_at", "resets_in_secs", "fetched_at_ms", "source", "age_secs", "stale"},
        )
        self.assertEqual(set(account["totals"]), {"requests", "input_tokens", "output_tokens"})
        self.assertEqual(set(account["session"]), {"requests", "ok", "errors", "tokens_in", "tokens_out"})

        raw = response.text
        for leak in (LLMUX_KEY, "refresh-token-value", "secret prompt text", "line one", "do not leak"):
            self.assertNotIn(leak, raw)

    def test_admin_key_is_sent_as_a_header_to_the_configured_loopback_url(self) -> None:
        transport = usage_transport()
        client = self.make_client(usage_transport=transport)
        self.login(client)
        client.get("/api/usage")
        request = transport.recorded[0]
        self.assertEqual(str(request.url), "http://127.0.0.1:3456/llmux/dashboard")
        self.assertEqual(request.method, "GET")
        header_values = " ".join(f"{key}: {value}" for key, value in request.headers.items())
        self.assertIn(LLMUX_KEY, header_values, "admin key must travel in a header")
        self.assertNotIn(LLMUX_KEY, str(request.url))

    def test_empty_auth_scheme_sends_the_bare_key(self) -> None:
        """The live proxy authenticates with ``x-api-key: <key>``, no scheme.

        An explicit empty ``llmux_auth_scheme`` must mean "no scheme", not
        "unset": a prefixed value is a malformed credential header.
        """
        transport = usage_transport()
        client = self.make_client(
            settings=self.settings(llmux_auth_header="x-api-key", llmux_auth_scheme=""),
            usage_transport=transport,
        )
        self.login(client)
        client.get("/api/usage")

        request = transport.recorded[0]
        self.assertEqual(request.headers["x-api-key"], LLMUX_KEY)
        self.assertNotIn("authorization", request.headers)

    def test_absent_auth_scheme_keeps_the_bearer_default(self) -> None:
        transport = usage_transport()
        settings = self.settings()
        settings.pop("llmux_auth_scheme", None)
        settings.pop("llmux_auth_header", None)
        client = self.make_client(settings=settings, usage_transport=transport)
        self.login(client)
        client.get("/api/usage")

        self.assertEqual(transport.recorded[0].headers["authorization"], f"Bearer {LLMUX_KEY}")

    def test_forbidden_upstream_is_unavailable_not_zero_usage(self) -> None:
        transport = usage_transport(status_code=403, payload={"error": "admin key required"})
        client = self.make_client(usage_transport=transport)
        self.login(client)
        body = client.get("/api/usage").json()
        self.assertFalse(body["available"])
        self.assertEqual(body["accounts"], [])
        self.assertTrue(body["error"])
        self.assertNotIn(LLMUX_KEY, json.dumps(body))

    def test_transport_error_is_unavailable_with_a_safe_message(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = self.make_client(usage_transport=usage_transport(handler=handler))
        self.login(client)
        body = client.get("/api/usage").json()
        self.assertFalse(body["available"])
        self.assertEqual(body["accounts"], [])
        self.assertNotIn("127.0.0.1", json.dumps(body))

    def test_redirects_are_not_followed(self) -> None:
        recorded: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "127.0.0.1":
                return httpx.Response(302, headers={"location": "https://evil.example/steal"})
            return httpx.Response(200, json={"accounts": []})  # pragma: no cover

        client = self.make_client(usage_transport=usage_transport(handler=handler, requested=recorded))
        self.login(client)
        body = client.get("/api/usage").json()
        self.assertFalse(body["available"])
        self.assertEqual([request.url.host for request in recorded], ["127.0.0.1"])

    def test_oversized_response_is_refused(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            blob = "x" * (dashboard_web.USAGE_MAX_BYTES + 1024)
            return httpx.Response(200, json={"accounts": [], "filler": blob})

        client = self.make_client(usage_transport=usage_transport(handler=handler))
        self.login(client)
        body = client.get("/api/usage").json()
        self.assertFalse(body["available"])
        self.assertTrue(body["error"])

    def test_result_is_cached_briefly_to_avoid_a_request_storm(self) -> None:
        transport = usage_transport()
        client = self.make_client(usage_transport=transport)
        self.login(client)
        for _ in range(4):
            self.assertTrue(client.get("/api/usage").json()["available"])
        self.assertEqual(len(transport.recorded), 1)

    def test_non_finite_and_wrongly_typed_values_are_dropped(self) -> None:
        payload = {
            "accounts": [
                {
                    "name": "team-a",
                    "healthy": "yes",
                    "in_flight": "many",
                    "five_hour": {"utilization": float("inf"), "resets_in_secs": "soon"},
                    "totals": {"requests": float("nan"), "input_tokens": 5, "output_tokens": None},
                }
            ]
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=json.dumps(payload), headers={"content-type": "application/json"})

        client = self.make_client(usage_transport=usage_transport(handler=handler))
        self.login(client)
        body = client.get("/api/usage").json()
        account = body["accounts"][0]
        self.assertEqual(account["name"], "team-a")
        self.assertNotIn("healthy", account)
        self.assertNotIn("in_flight", account)
        self.assertNotIn("utilization", account.get("five_hour", {}))
        self.assertNotIn("resets_in_secs", account.get("five_hour", {}))
        self.assertEqual(account.get("totals"), {"input_tokens": 5})

    def test_missing_accounts_key_is_unavailable(self) -> None:
        client = self.make_client(usage_transport=usage_transport(payload={"status": "ok"}))
        self.login(client)
        body = client.get("/api/usage").json()
        self.assertFalse(body["available"])

    def test_usage_requires_authentication(self) -> None:
        client = self.make_client()
        self.assertEqual(client.get("/api/usage").status_code, 401)

    def test_window_freshness_is_labelled_from_fetched_at_ms(self) -> None:
        """Observed live: a freshly fetched window can carry a 1481s-old fetched_at_ms."""
        stale_ms = int((time.time() - 1481) * 1000)
        fresh_ms = int(time.time() * 1000)
        payload = {
            "accounts": [
                {
                    "name": "team-a",
                    "five_hour": {"utilization": 0.4, "fetched_at_ms": stale_ms, "source": "upstream"},
                    "seven_day": {"utilization": 0.1, "fetched_at_ms": fresh_ms, "source": "upstream"},
                }
            ]
        }
        client = self.make_client(usage_transport=usage_transport(payload=payload))
        self.login(client)
        account = client.get("/api/usage").json()["accounts"][0]

        self.assertGreaterEqual(account["five_hour"]["age_secs"], 1400)
        self.assertTrue(account["five_hour"]["stale"])
        self.assertLess(account["seven_day"]["age_secs"], dashboard_web.USAGE_STALE_AFTER_SECONDS)
        self.assertFalse(account["seven_day"]["stale"])

    def test_cooldown_until_is_numeric_epoch_seconds(self) -> None:
        """llmux AccountDoc carries Option<u64> epoch seconds; the UI is numeric."""
        future = int(time.time()) + 900
        payload = {"accounts": [{"name": "team-a", "cooldown_until": future}]}
        client = self.make_client(usage_transport=usage_transport(payload=payload))
        self.login(client)
        account = client.get("/api/usage").json()["accounts"][0]
        self.assertEqual(account["cooldown_until"], future)
        self.assertIsInstance(account["cooldown_until"], int)

    def test_cooldown_until_null_is_preserved(self) -> None:
        payload = {"accounts": [{"name": "team-a", "cooldown_until": None}]}
        client = self.make_client(usage_transport=usage_transport(payload=payload))
        self.login(client)
        self.assertIsNone(client.get("/api/usage").json()["accounts"][0]["cooldown_until"])

    def test_cooldown_until_string_is_dropped(self) -> None:
        payload = {"accounts": [{"name": "team-a", "cooldown_until": "2026-09-11T05:00:00Z"}]}
        client = self.make_client(usage_transport=usage_transport(payload=payload))
        self.login(client)
        account = client.get("/api/usage").json()["accounts"][0]
        self.assertNotIn("cooldown_until", account, "a string must not reach a numeric renderer")

    def test_window_without_fetched_at_ms_is_labelled_unknown_freshness(self) -> None:
        payload = {"accounts": [{"name": "team-a", "five_hour": {"utilization": 0.4}}]}
        client = self.make_client(usage_transport=usage_transport(payload=payload))
        self.login(client)
        window = client.get("/api/usage").json()["accounts"][0]["five_hour"]
        self.assertIsNone(window["age_secs"])
        self.assertTrue(window["stale"], "unknown freshness must not be presented as fresh")


# ---------------------------------------------------------------------------
# 10. Static assets, headers, health
# ---------------------------------------------------------------------------


class StaticAndHeaderTest(AppTestCase):
    def test_healthz_reports_version_without_leaking_settings(self) -> None:
        client = self.make_client()
        response = client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body, {"status": "ok", "version": "deploy-main-test", "auth_configured": True})
        raw = response.text
        for leak in (LLMUX_KEY, GOOGLE_SECRET, SESSION_SECRET, str(self.config_path)):
            self.assertNotIn(leak, raw)

    def test_healthz_is_reachable_over_loopback_host(self) -> None:
        app = self.make_app()
        client = TestClient(app, base_url="http://127.0.0.1:8840")
        self.addCleanup(client.close)
        self.assertEqual(client.get("/healthz").status_code, 200)

    def test_foreign_host_header_is_rejected(self) -> None:
        app = self.make_app()
        client = TestClient(app, base_url="https://evil.example")
        self.addCleanup(client.close)
        self.assertEqual(client.get("/healthz").status_code, 400)

    def test_security_headers_on_shell_and_api(self) -> None:
        client = self.make_client()
        self.login(client)
        for path in ("/", "/api/me"):
            with self.subTest(path=path):
                response = client.get(path)
                csp = response.headers["content-security-policy"]
                self.assertIn("default-src 'self'", csp)
                self.assertIn("script-src 'self'", csp)
                self.assertIn("connect-src 'self'", csp)
                self.assertIn("frame-ancestors 'none'", csp)
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(response.headers["referrer-policy"], "no-referrer")
                self.assertNotIn("access-control-allow-origin", response.headers)

    def test_known_assets_are_served_and_traversal_is_refused(self) -> None:
        (self.dashboard_dir / "app.js").write_text("export const x = 1;\n", encoding="utf-8")
        (self.dashboard_dir / "assets").mkdir()
        (self.dashboard_dir / "assets" / "style.css").write_text("body{}\n", encoding="utf-8")
        # Present on disk, still not public: not part of the shipped UI surface.
        (self.dashboard_dir / "dev.mjs").write_text("export const dev = 1;\n", encoding="utf-8")
        (self.dashboard_dir / "app.js.map").write_text("{\"sources\":[\"app.ts\"]}\n", encoding="utf-8")
        secret = self.tmp / "outside.txt"
        secret.write_text("top secret\n", encoding="utf-8")
        os.symlink(secret, self.dashboard_dir / "escape.js")

        client = self.make_client()
        self.assertEqual(client.get("/app.js").status_code, 200)
        self.assertEqual(client.get("/assets/style.css").status_code, 200)
        for path in (
            "/dev.mjs",
            "/app.js.map",
            "/../outside.txt",
            "/..%2foutside.txt",
            "/assets/../../outside.txt",
            "/escape.js",
            "/app.js/../outside.txt",
            "/unknown.js",
            "/config/reviewer.json",
        ):
            with self.subTest(path=path):
                response = client.get(path, follow_redirects=False)
                self.assertIn(response.status_code, (403, 404), f"{path} -> {response.status_code}")
                self.assertNotIn("top secret", response.text)


# ---------------------------------------------------------------------------
# 11. Settings validation
# ---------------------------------------------------------------------------


class SettingsValidationTest(AppTestCase):
    def test_missing_non_secret_names_are_reported(self) -> None:
        settings = self.settings()
        for key in ("public_url", "llmux_base_url", "config_path"):
            settings.pop(key)
        with self.assertRaises(dashboard_web.SettingsError) as ctx:
            dashboard_web.validate_settings(settings)
        message = str(ctx.exception)
        for key in ("public_url", "llmux_base_url", "config_path"):
            self.assertIn(key, message)

    def test_missing_secret_names_and_values_are_not_reported(self) -> None:
        settings = self.settings()
        for key in ("google_client_secret", "session_secret", "llmux_api_key"):
            settings.pop(key)
        with self.assertRaises(dashboard_web.SettingsError) as ctx:
            dashboard_web.validate_settings(settings)
        message = str(ctx.exception)
        for key in ("google_client_secret", "session_secret", "llmux_api_key"):
            self.assertNotIn(key, message)
        self.assertIn("secret", message)

    def test_non_canonical_public_url_is_refused(self) -> None:
        for url in ("http://rol.company.example", "https://evil.example", "https://rol.company.example.evil.com"):
            with self.subTest(url=url):
                with self.assertRaises(dashboard_web.SettingsError):
                    dashboard_web.validate_settings(self.settings(public_url=url))

    def test_non_loopback_llmux_base_url_is_refused(self) -> None:
        for url in ("http://10.0.0.5:3456", "https://llmux.example.com", "http://evil.example"):
            with self.subTest(url=url):
                with self.assertRaises(dashboard_web.SettingsError):
                    dashboard_web.validate_settings(self.settings(llmux_base_url=url))

    def test_loopback_llmux_base_urls_are_accepted(self) -> None:
        for url in ("http://127.0.0.1:3456", "http://localhost:3456", "http://[::1]:3456"):
            with self.subTest(url=url):
                dashboard_web.validate_settings(self.settings(llmux_base_url=url))

    def test_settings_file_load_reports_the_path_only(self) -> None:
        missing = self.tmp / "settings.json"
        with self.assertRaises(dashboard_web.SettingsError) as ctx:
            dashboard_web.load_settings(missing)
        self.assertIn(str(missing), str(ctx.exception))

    def test_session_secret_is_never_generated_by_the_web_process(self) -> None:
        """W5's installer owns secret creation; the web process fails closed."""
        settings = self.settings()
        settings.pop("session_secret")
        with self.assertRaises(dashboard_web.SettingsError):
            dashboard_web.create_app(settings, self.store, oauth_client=self.oauth)
        for value in settings.values():
            self.assertNotEqual(value, "", "no setting may be silently defaulted to a blank secret")

    def test_short_session_secret_is_refused(self) -> None:
        with self.assertRaises(dashboard_web.SettingsError):
            dashboard_web.validate_settings(self.settings(session_secret="short"))

    def test_settings_file_must_not_be_world_readable(self) -> None:
        path = self.tmp / "dashboard-secrets.json"
        path.write_text(json.dumps(self.settings()), encoding="utf-8")
        path.chmod(0o644)
        with self.assertRaises(dashboard_web.SettingsError) as ctx:
            dashboard_web.load_settings(path)
        self.assertNotIn(SESSION_SECRET, str(ctx.exception))

        path.chmod(0o600)
        loaded = dashboard_web.load_settings(path)
        self.assertEqual(loaded["public_url"], PUBLIC_URL)

    def test_derived_paths_come_from_the_service_root(self) -> None:
        settings = self.settings()
        settings.pop("auth_db_path")
        settings.pop("dashboard_dir")
        resolved = dashboard_web.resolve_paths(settings)
        root = self.service_root.resolve()
        self.assertEqual(resolved["auth_db_path"], root / "data" / "dashboard-auth.sqlite3")
        self.assertEqual(resolved["store_path"], root / "data" / "dashboard" / "dashboard.sqlite3")
        self.assertEqual(resolved["dashboard_dir"], root / "dashboard")

    def test_server_is_bound_to_loopback_without_access_logs(self) -> None:
        """Access logs would record the callback query string, i.e. the auth code."""
        kwargs = dashboard_web.uvicorn_run_kwargs(self.settings())
        self.assertEqual(kwargs["host"], "127.0.0.1")
        self.assertEqual(kwargs["port"], 8840)
        self.assertFalse(kwargs["access_log"])
        self.assertNotIn(SESSION_SECRET, json.dumps(kwargs, default=str))

    def test_default_settings_path_matches_the_provisioned_location(self) -> None:
        self.assertEqual(
            dashboard_web.DEFAULT_SETTINGS_PATH,
            Path.home() / "review-of-legends/dashboard-secrets.json",
        )


# ---------------------------------------------------------------------------
# 12. Store contract parity (binds the fake to the real W1 module)
# ---------------------------------------------------------------------------


class StoreContractParityTest(unittest.TestCase):
    """The fake must stay signature-compatible with the real W1 store."""

    def test_real_store_exposes_the_methods_the_web_layer_calls(self) -> None:
        from code_review_service.dashboard_store import DashboardStore

        store_cls = DashboardStore
        import inspect

        for name in ("list_runs", "run_status_counts", "get_run", "enqueue_retry", "events_after",
                     "latest_event_id", "latest_event_at", "start_run", "get_setting", "set_setting", "delete_setting",
                     "merge_agent_setting", "reconcile_agent_setting", "restore_superseded_prompt", "discard_superseded_prompt",
                     "latest_attempt_for_head", "active_activity_for_pr"):
            with self.subTest(method=name):
                self.assertEqual(
                    list(inspect.signature(getattr(store_cls, name)).parameters),
                    list(inspect.signature(getattr(FakeDashboardStore, name)).parameters),
                    f"{name} signature drifted from the fake used in W3 tests",
                )
        self.assertEqual(store_cls.max_list_limit, FakeDashboardStore.max_list_limit)
        self.assertEqual(store_cls.max_event_limit, FakeDashboardStore.max_event_limit)

    def test_retry_listing_signature_matches_the_fake(self) -> None:
        from code_review_service.dashboard_store import DashboardStore

        store_cls = DashboardStore
        import inspect

        self.assertEqual(
            list(inspect.signature(store_cls.list_retries).parameters),
            list(inspect.signature(FakeDashboardStore.list_retries).parameters),
        )

    @requires_dashboard_deps  # asserts against dashboard_web's projection tuples
    def test_row_shapes_match_the_fake_for_every_shape_the_api_projects(self) -> None:
        """Compare real rows against fake rows, not against a hand-copied tuple."""
        from code_review_service.dashboard_store import DashboardStore

        store_cls = DashboardStore

        def populate(store: Any) -> dict[str, Any]:
            run = store.start_run("owner/repo", 7, "a" * 40, title="t", url="u")
            store.update_run(run["id"], "failed", error="boom", agents=[{"id": "a", "status": "failed"}])
            request = store.enqueue_retry("owner/repo", 7, "a" * 40, ADMIN_EMAIL, retry_of=run["id"])
            store.claim_retry()
            store.finish_retry(request["id"], "failed", run_id=run["id"], error="boom")
            return {
                "run": store.get_run(run["id"]),
                "runs": store.list_runs()[0],
                "enqueue": request,
                "event": store.events_after(0)[0],
                "latest_event_id": store.latest_event_id(),
            }

        with tempfile.TemporaryDirectory() as tmp:
            real = populate(store_cls(Path(tmp) / "dashboard" / "dashboard.sqlite3"))
            real_retry = store_cls(Path(tmp) / "dashboard" / "dashboard.sqlite3").list_retries()[0]
        fake_store = FakeDashboardStore()
        fake = populate(fake_store)
        fake_retry = fake_store.list_retries()[0]

        for shape in ("run", "runs", "enqueue", "event"):
            with self.subTest(shape=shape):
                self.assertEqual(set(real[shape]), set(fake[shape]))
        self.assertEqual(set(real_retry), set(fake_retry))
        self.assertIsInstance(real["latest_event_id"], int)
        self.assertEqual(real["latest_event_id"] > 0, fake["latest_event_id"] > 0)
        # Everything the API promises for /api/retries must exist in the real row.
        self.assertTrue(set(dashboard_web.RETRY_FIELDS) <= set(real_retry))
        self.assertTrue(set(dashboard_web.RUN_FIELDS) <= set(real["run"]))


class RealStoreIntegrationTest(AppTestCase):
    """Same HTTP assertions, driven through the real SQLite store."""

    def setUp(self) -> None:
        super().setUp()
        from code_review_service.dashboard_store import DashboardStore

        self.store = DashboardStore(self.service_root / "data" / "dashboard" / "dashboard.sqlite3")

    def test_runs_projection_over_real_rows(self) -> None:
        client = self.make_client()
        self.login(client)
        run = self.store.start_run("owner/repo", 7, "a" * 40, title="Add dashboard")
        self.store.update_run(
            run["id"],
            "failed",
            error="clone failed in /Users/ci/work/owner-repo",
            agents=[{"id": "security", "status": "failed", "summary": "boom", "prompt": "raw"}],
        )

        payload = client.get("/api/runs").json()
        entry = payload["runs"][0]
        self.assertEqual(entry["id"], run["id"])
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["agents"], [{"id": "security", "status": "failed", "summary": "boom"}])
        self.assertNotIn("/Users/ci", json.dumps(payload))

    def test_retry_round_trip_over_real_rows(self) -> None:
        head = "a" * 40
        self.github.pr = open_pr(head)
        client = self.make_client()
        identity = self.login(client)
        run = self.store.start_run("owner/repo", 7, head)
        self.store.update_run(run["id"], "failed", error="quorum not reached")

        first = client.post(
            f"/api/runs/{run['id']}/retry",
            content=json.dumps({"head_sha": head}),
            headers=self.auth_headers(identity),
        )
        self.assertEqual(first.status_code, 202, first.text)
        second = client.post(
            f"/api/runs/{run['id']}/retry",
            content=json.dumps({"head_sha": head}),
            headers=self.auth_headers(identity),
        )
        self.assertEqual(second.json()["request"]["id"], first.json()["request"]["id"])
        self.assertEqual(self.store.retry_status(first.json()["request"]["id"]), "queued")

    def test_kept_agent_results_reach_the_browser_as_ids_and_default_the_retry_scope(self) -> None:
        head = "a" * 40
        self.github.pr = open_pr(head)
        client = self.make_client()
        identity = self.login(client)
        run = self.store.start_run("owner/repo", 7, head)
        self.store.update_run(run["id"], "failed", stage="quorum", error="only 1/3 agents succeeded", agent_results=[
            {"agent": "correctness", "summary": "ok", "findings": [{"title": "kept finding text", "path": "a.py", "line": 1}]},
        ])

        payload = client.get(f"/api/runs/{run['id']}").json()["run"]
        self.assertEqual(payload["reusable_agents"], ["correctness"])
        stream = json.dumps(client.get("/api/runs").json()) + json.dumps(payload)
        self.assertNotIn("kept finding text", stream, "the results stay in the store")
        response = client.post(f"/api/runs/{run['id']}/retry", content=json.dumps({"head_sha": head}),
                               headers=self.auth_headers(identity))
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["request"]["scope"], "failed")

    def test_retry_queue_listing_over_real_rows(self) -> None:
        """/api/retries must carry run_id and error from the real store's list."""
        client = self.make_client()
        self.login(client)
        run = self.store.start_run("owner/repo", 7, "a" * 40)
        self.store.update_run(run["id"], "failed", error="quorum not reached")
        queued = self.store.enqueue_retry("owner/repo", 7, "a" * 40, ADMIN_EMAIL, retry_of=run["id"])
        self.store.claim_retry()
        attempt = self.store.start_run("owner/repo", 7, "a" * 40, trigger="retry", retry_of=run["id"])
        self.store.finish_retry(queued["id"], "failed", run_id=attempt["id"], error="agents timed out")

        payload = client.get("/api/retries").json()
        entry = [row for row in payload["requests"] if row["id"] == queued["id"]][0]
        self.assertEqual(
            set(entry),
            {"id", "status", "repo", "pr", "head_sha", "retry_of", "created_at", "run_id", "error", "model_policy", "scope", "kind"},
        )
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["retry_of"], run["id"])
        # These two are what the queue view needs and what the frozen
        # ``_request_dict`` shape does not yet provide (W1 aligning list_retries).
        self.assertEqual(entry["run_id"], attempt["id"])
        self.assertEqual(entry["error"], "agents timed out")

    def test_event_replay_over_real_rows(self) -> None:
        client = self.make_client()
        self.login(client)
        run = self.store.start_run("owner/repo", 7, "a" * 40)
        self.store.update_run(run["id"], "failed", error="quorum not reached")
        events = self.store.events_after(0)

        state = collect_sse(
            self.app,
            f"/api/events?after={events[0]['id']}",
            client.cookies[dashboard_auth.SESSION_COOKIE],
            minimum_events=1,
        )
        self.assertIn(f"id: {events[1]['id']}", state["data_blocks"][0])

    def test_fresh_stream_over_real_rows_starts_at_the_tail(self) -> None:
        client = self.make_client()
        self.login(client)
        run = self.store.start_run("owner/repo", 7, "a" * 40)
        self.store.update_run(run["id"], "failed", error="quorum not reached")
        backlog = self.store.events_after(0)

        state = collect_sse(
            self.app,
            "/api/events",
            client.cookies[dashboard_auth.SESSION_COOKIE],
            minimum_events=10**6,
            stop_on=("ready",),
        )
        self.assertEqual(state["data_blocks"], [])
        self.assertEqual(
            json.loads(state["blocks"][0].split("data: ", 1)[1]), {"cursor": backlog[-1]["id"]}
        )




# ---------------------------------------------------------------------------
# Model policy: catalog, settings, retry override (.prd/model-policy/trace.md B/C)
# ---------------------------------------------------------------------------

CATALOG_PAYLOAD = {
    "models": [
        {"id": "claude-opus-5[1m]", "aliases": ["opus"], "name": "Claude Opus 5 [1M]", "efforts": ["low", "high"], "max_context": 1000000, "group": "claude"},
        {"id": "gpt-6-astra[1m]", "aliases": ["astra"], "name": "GPT-6-Astra [1M]", "efforts": ["low", "xhigh", "ultra"], "max_context": 1000000, "group": "codex"},
        {"id": "grok-4.6", "aliases": [], "name": "Grok 4.6", "efforts": ["low", "xhigh"], "max_context": 500000, "group": "grok"},
        {"id": "", "name": "broken"},
        "not-an-object",
    ]
}


def catalog_transport(payload: Any = None, status_code: int = 200, requested: list | None = None):
    recorded = requested if requested is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        if request.url.path == "/llmux/models":
            return httpx.Response(status_code, json=payload if payload is not None else CATALOG_PAYLOAD)
        return httpx.Response(200, json=USAGE_PAYLOAD)

    transport = httpx.MockTransport(handler)
    transport.recorded = recorded  # type: ignore[attr-defined]
    return transport


class ModelCatalogTest(AppTestCase):
    def test_catalog_marks_claude_unusable_and_sends_no_key(self) -> None:
        transport = catalog_transport()
        client = self.make_client(usage_transport=transport)
        self.login(client)
        response = client.get("/api/models")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["available"])
        ids = [row["id"] for row in payload["models"]]
        self.assertEqual(ids, ["claude-opus-5[1m]", "gpt-6-astra[1m]", "grok-4.6"])
        claude = payload["models"][0]
        self.assertFalse(claude["usable"])
        self.assertIn("Claude models are not available", claude["reason"])
        self.assertEqual(claude["group_label"], "Anthropic Claude")
        grok = payload["models"][2]
        self.assertTrue(grok["usable"])
        self.assertIsNone(grok["reason"])
        self.assertEqual(grok["efforts"], ["low", "xhigh"])
        self.assertEqual(grok["max_context"], 500000)
        request = [r for r in transport.recorded if r.url.path == "/llmux/models"][0]
        self.assertNotIn("authorization", {k.lower() for k in request.headers})
        self.assertNotIn(LLMUX_KEY, str(request.headers))

    def test_catalog_is_cached_and_unavailability_is_explicit(self) -> None:
        transport = catalog_transport()
        client = self.make_client(usage_transport=transport)
        self.login(client)
        client.get("/api/models")
        client.get("/api/models")
        self.assertEqual(len([r for r in transport.recorded if r.url.path == "/llmux/models"]), 1)

        down = self.make_client(usage_transport=catalog_transport(status_code=503))
        self.login(down)
        payload = down.get("/api/models").json()
        self.assertFalse(payload["available"])
        self.assertEqual(payload["models"], [])
        self.assertIn("HTTP 503", payload["error"])

    def test_catalog_requires_the_admin_session(self) -> None:
        client = self.make_client(usage_transport=catalog_transport())
        self.assertEqual(client.get("/api/models").status_code, 401)


class ModelPolicySettingsTest(AppTestCase):
    POLICY = {"model": "gpt-6-astra[1m]", "effort": "xhigh", "fallback_models": ["grok-4.6"]}

    def test_get_reports_the_config_policy_until_the_dashboard_sets_one(self) -> None:
        self.config_path.write_text(json.dumps({"repositories": ["owner/repo"], "agents": [{"id": "correctness"}, {"id": "security"}, {"id": "tests"}], "gh_bin": "gh", "model_policy": self.POLICY}), encoding="utf-8")
        client = self.make_client()
        self.login(client)
        payload = client.get("/api/settings/model-policy").json()
        self.assertEqual(payload["source"], "config")
        self.assertEqual(payload["policy"], {**self.POLICY, "source": "config"})
        self.assertIsNone(payload["updated_by"])

    def test_put_validates_stores_and_reports_the_dashboard_policy(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        chosen = {"model": "grok-4.6", "effort": "XHIGH", "fallback_models": ["gpt-6-astra[1m]", "gpt-5.5"]}
        response = client.put("/api/settings/model-policy", content=json.dumps(chosen), headers=self.auth_headers(identity))
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["source"], "dashboard")
        self.assertEqual(payload["policy"], {"model": "grok-4.6", "effort": "xhigh", "fallback_models": ["gpt-6-astra[1m]", "gpt-5.5"], "source": "dashboard"})
        self.assertEqual(payload["updated_by"], ADMIN_EMAIL)
        self.assertTrue(payload["updated_at"])
        call = self.store.calls_named("set_setting")[-1]
        self.assertEqual(call[1][0], "model_policy")
        self.assertEqual(call[1][1], {"model": "grok-4.6", "effort": "xhigh", "fallback_models": ["gpt-6-astra[1m]", "gpt-5.5"]})
        self.assertEqual(call[1][2], ADMIN_EMAIL)
        self.assertEqual(client.get("/api/settings/model-policy").json()["source"], "dashboard")

        state = collect_sse(self.app, "/api/events?after=0", self.cookie(client), minimum_events=1)
        block = state["data_blocks"][-1]
        self.assertIn("setting_updated", block)
        event = json.loads(block.split("data: ", 1)[1])
        self.assertEqual(event["payload"]["key"], "model_policy")
        self.assertEqual(event["payload"]["value"]["model"], "grok-4.6")
        self.assertNotIn("updated_by", event["payload"], "the operator email is not part of the public event projection")

    def test_put_rejects_claude_and_malformed_policies_without_writing(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        for body in (
            {"model": "claude-opus-5[1m]"},
            {"model": "grok-4.6", "fallback_models": ["claude-sonnet-5"]},
            {"model": "grok-4.6", "effort": "ludicrous"},
            {"model": "grok-4.6", "fallback_models": ["grok-4.6"]},
            {"model": ""},
            [],
        ):
            with self.subTest(body=body):
                response = client.put("/api/settings/model-policy", content=json.dumps(body), headers=self.auth_headers(identity))
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.store.calls_named("set_setting"), [])
        self.assertIn("Claude models are not available", client.put(
            "/api/settings/model-policy", content=json.dumps({"model": "claude-opus-5[1m]"}), headers=self.auth_headers(identity)
        ).json()["error"])

    def test_put_and_delete_need_csrf_and_origin(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        headers = self.auth_headers(identity)
        no_csrf = {k: v for k, v in headers.items() if k != "X-CSRF-Token"}
        self.assertEqual(client.put("/api/settings/model-policy", content=json.dumps(self.POLICY), headers=no_csrf).status_code, 403)
        bad_origin = {**headers, "Origin": "https://evil.example"}
        self.assertEqual(client.put("/api/settings/model-policy", content=json.dumps(self.POLICY), headers=bad_origin).status_code, 403)
        self.assertEqual(client.delete("/api/settings/model-policy", headers=no_csrf).status_code, 403)
        self.assertEqual(self.store.calls_named("set_setting"), [])
        self.assertEqual(self.store.calls_named("delete_setting"), [])

    def test_delete_resets_to_the_config_policy(self) -> None:
        self.config_path.write_text(json.dumps({"repositories": ["owner/repo"], "agents": [{"id": "correctness"}, {"id": "security"}, {"id": "tests"}], "gh_bin": "gh", "model_policy": self.POLICY}), encoding="utf-8")
        client = self.make_client()
        identity = self.login(client)
        client.put("/api/settings/model-policy", content=json.dumps({"model": "grok-4.6"}), headers=self.auth_headers(identity))
        response = client.delete("/api/settings/model-policy", headers=self.auth_headers(identity))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["source"], "config")
        self.assertEqual(response.json()["policy"]["model"], "gpt-6-astra[1m]")
        self.assertEqual(self.store.calls_named("delete_setting")[-1][1], ("model_policy", ADMIN_EMAIL))

    def test_anonymous_settings_access_is_denied(self) -> None:
        client = self.make_client()
        self.assertEqual(client.get("/api/settings/model-policy").status_code, 401)
        self.assertEqual(client.put("/api/settings/model-policy", content="{}", headers={"Content-Type": "application/json"}).status_code, 401)
        self.assertEqual(client.get("/api/settings/agents").status_code, 401)
        self.assertEqual(client.put("/api/settings/agents/arch", content="{}", headers={"Content-Type": "application/json", "Origin": PUBLIC_URL}).status_code, 401)
        self.assertEqual(client.delete("/api/settings/agents/arch", headers={"Origin": PUBLIC_URL}).status_code, 401)


# ---------------------------------------------------------------------------
# Agent settings: prompt + required per reviewer agent
# ---------------------------------------------------------------------------


class _AgentFixture(AppTestCase):
    """Three readable prompt files, one missing, one disabled agent; no tests of its own."""

    # Token- and path-shaped words a sanitize_text pass would have redacted.
    ARCH_PROMPT = "You are the server architect.\nRead /Users/operator/pattern.db-access.md and token: abcdefgh12345678.\n"

    def setUp(self) -> None:
        super().setUp()
        agents_dir = self.service_root / "agents"
        agents_dir.mkdir()
        (agents_dir / "a.md").write_text("Prompt for a.\n", encoding="utf-8")
        (agents_dir / "b.md").write_text("Prompt for b.\n", encoding="utf-8")
        (agents_dir / "arch.md").write_text(self.ARCH_PROMPT, encoding="utf-8")
        self.config_path.write_text(json.dumps({
            "repositories": ["owner/repo", "owner/gamma"],
            "repository_agents": {"owner/gamma": {"arch": True}},
            "agents": [
                {"id": "a", "prompt_path": "../agents/a.md"},
                {"id": "b", "prompt_path": "../agents/b.md"},
                {"id": "c", "prompt_path": "../agents/missing.md"},
                {"id": "off", "enabled": False, "prompt_path": "../agents/a.md"},
                {"id": "arch", "opt_in": True, "required": True, "focus": "Server architecture", "prompt_path": "../agents/arch.md"},
            ],
            "gh_bin": "gh",
        }), encoding="utf-8")

    def viewer_login(self, client: TestClient) -> dict[str, Any]:
        self.oauth.token = {"access_token": "at", "userinfo": dict(VIEWER_USERINFO)}
        return self.login(client)

    def rows(self, client: TestClient) -> dict[str, dict[str, Any]]:
        response = client.get("/api/settings/agents")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["max_prompt_chars"], 48000)
        return {row["id"]: row for row in response.json()["agents"]}

    def put(self, client: TestClient, identity: dict[str, Any], agent_id: str, body: Any):
        return client.put(f"/api/settings/agents/{agent_id}", content=json.dumps(body), headers=self.auth_headers(identity))


class AgentSettingsTest(_AgentFixture):
    def test_viewer_reads_every_enabled_agent_with_its_prompt_and_required_flag(self) -> None:
        client = self.make_client()
        self.viewer_login(client)
        rows = self.rows(client)
        self.assertEqual(sorted(rows), ["a", "arch", "b", "c"], "a disabled agent is not listed")
        arch = rows["arch"]
        self.assertEqual(arch["prompt"], self.ARCH_PROMPT, "the prompt is shown verbatim, never redacted")
        self.assertEqual((arch["required"], arch["required_source"], arch["config_required"]), (True, "config", True))
        self.assertEqual((arch["prompt_source"], arch["updated_by"], arch["updated_at"]), ("file", None, None))
        self.assertEqual((arch["focus"], arch["repos"]), ("Server architecture", ["owner/gamma"]))
        self.assertEqual((rows["a"]["required"], rows["a"]["repos"]), (False, ["owner/repo", "owner/gamma"]))
        self.assertIsNone(rows["c"]["prompt"])
        self.assertEqual(rows["c"]["error"], "prompt file is unreadable")
        self.assertNotIn(str(self.service_root), json.dumps(rows))

    def test_viewer_cannot_change_or_reset_an_agent(self) -> None:
        client = self.make_client()
        identity = self.viewer_login(client)
        for agent_id in ("arch", "nope"):
            put = self.put(client, identity, agent_id, {"required": False})
            self.assertEqual((put.status_code, put.json()["error"]), (403, "admin role required"))
            delete = client.delete(f"/api/settings/agents/{agent_id}", headers=self.auth_headers(identity))
            self.assertEqual((delete.status_code, delete.json()["error"]), (403, "admin role required"))
        self.assertEqual(self.store.calls_named("set_setting"), [])
        self.assertEqual(self.store.calls_named("delete_setting"), [])

    def test_admin_put_round_trips_merges_and_null_clears(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        response = self.put(client, identity, "arch", {"prompt": "  New architect prompt.\n", "required": False})
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()
        self.assertEqual((row["prompt"], row["prompt_source"]), ("New architect prompt.", "dashboard"))
        self.assertEqual((row["required"], row["required_source"], row["config_required"]), (False, "dashboard", True))
        self.assertEqual(row["updated_by"], ADMIN_EMAIL)
        self.assertEqual(self.store.calls_named("set_setting")[-1][1], ("agent:arch", {
            "prompt": "New architect prompt.", "prompt_base_sha": svc.prompt_sha(self.ARCH_PROMPT), "required": False,
            "prompt_saved_at": mock.ANY, "prompt_saved_by": ADMIN_EMAIL,
        }, ADMIN_EMAIL))
        self.assertEqual(self.rows(client)["arch"], row, "PUT answers the same row GET lists")

        # Omitting a field keeps its override; null clears just that field.
        row = self.put(client, identity, "arch", {"prompt": None}).json()
        self.assertEqual((row["prompt"], row["prompt_source"]), (self.ARCH_PROMPT, "file"))
        self.assertEqual((row["required"], row["required_source"]), (False, "dashboard"))
        self.assertEqual(self.store.calls_named("set_setting")[-1][1][1], {"required": False})

        # A document left empty deletes the setting.
        row = self.put(client, identity, "arch", {"required": None}).json()
        self.assertEqual((row["required"], row["required_source"], row["updated_by"]), (True, "config", None))
        self.assertEqual(self.store.calls_named("delete_setting")[-1][1], ("agent:arch", ADMIN_EMAIL))
        self.assertIsNone(self.store.get_setting("agent:arch"))

    def test_admin_sets_and_clears_an_agent_model(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        arch = self.rows(client)["arch"]
        self.assertEqual((arch["model"], arch["model_source"]), (None, "default"))

        response = self.put(client, identity, "arch", {"model": " gpt-6-astra[1m] "})
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()
        self.assertEqual((row["model"], row["model_source"]), ("gpt-6-astra[1m]", "dashboard"))
        self.assertEqual((row["prompt_source"], row["required_source"]), ("file", "config"), "only the model is overridden")
        self.assertEqual(self.store.calls_named("set_setting")[-1][1], ("agent:arch", {"model": "gpt-6-astra[1m]"}, ADMIN_EMAIL))
        rows = self.rows(client)
        self.assertEqual(rows["arch"], row, "PUT answers the same row GET lists")
        self.assertEqual((rows["a"]["model"], rows["a"]["model_source"]), (None, "default"))

        row = self.put(client, identity, "arch", {"required": False}).json()
        self.assertEqual((row["model"], row["required"]), ("gpt-6-astra[1m]", False), "a field the body omits keeps its override")
        row = self.put(client, identity, "arch", {"model": None}).json()
        self.assertEqual((row["model"], row["model_source"], row["required_source"]), (None, "default", "dashboard"))
        self.assertEqual(self.store.calls_named("set_setting")[-1][1][1], {"required": False})

        self.put(client, identity, "arch", {"model": "gpt-6-astra[1m]"})
        reset = client.delete("/api/settings/agents/arch", headers=self.auth_headers(identity)).json()
        self.assertEqual((reset["model"], reset["model_source"], reset["required_source"]), (None, "default", "config"))

    def test_admin_put_refuses_a_claude_or_malformed_agent_model(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        for body, message in (
            ({"model": "claude-opus-4"}, "Claude models are not available to this service"),
            ({"model": ""}, "model must be a non-empty model id"),
            ({"model": "not a model"}, "model is not a valid model id"),
        ):
            with self.subTest(body=body):
                response = self.put(client, identity, "arch", body)
                self.assertEqual(response.status_code, 400, response.text[:200])
                self.assertIn(message, response.json()["error"])
        self.assertEqual(self.store.calls_named("set_setting"), [])

    def test_admin_put_refuses_an_agent_model_the_catalog_marks_unusable(self) -> None:
        payload = {"models": [
            {"id": "claude-opus-5[1m]", "aliases": ["opus"], "name": "Opus", "group": "claude", "efforts": []},
            {"id": "gpt-6-astra[1m]", "aliases": ["astra"], "name": "Astra", "group": "codex", "efforts": []},
        ]}
        client = self.make_client(usage_transport=catalog_transport(payload=payload))
        identity = self.login(client)
        response = self.put(client, identity, "arch", {"model": "opus"})
        self.assertEqual(response.status_code, 400, response.text[:200])
        self.assertIn("'opus'", response.json()["error"])
        self.assertEqual(self.store.calls_named("set_setting"), [])
        response = self.put(client, identity, "arch", {"model": "astra"})
        self.assertEqual(response.status_code, 200, response.text[:200])
        self.assertEqual(response.json()["model"], "astra")

    def test_admin_put_validates_fields_limits_and_the_agent(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        for body, message in (
            ({"prompt": "x", "focus": "y"}, "unknown fields: focus"),
            ({"prompt": "   "}, "non-empty string"),
            ({"prompt": 7}, "non-empty string"),
            ({"required": "yes"}, "true or false"),
            ({"prompt": "x" * 48001}, "at most 48000"),
            ([], "JSON object"),
        ):
            with self.subTest(body=str(body)[:40]):
                response = self.put(client, identity, "arch", body)
                self.assertEqual(response.status_code, 400, response.text[:200])
                self.assertIn(message, response.json()["error"])
        for agent_id in ("nope", "off", "Arch"):
            with self.subTest(agent_id=agent_id):
                self.assertEqual(self.put(client, identity, agent_id, {"required": True}).status_code, 404)
                self.assertEqual(client.delete(f"/api/settings/agents/{agent_id}", headers=self.auth_headers(identity)).status_code, 404)
        self.assertEqual(self.store.calls_named("set_setting"), [])
        no_csrf = {k: v for k, v in self.auth_headers(identity).items() if k != "X-CSRF-Token"}
        self.assertEqual(client.put("/api/settings/agents/arch", content=json.dumps({"required": True}), headers=no_csrf).status_code, 403)

    def test_a_full_size_prompt_passes_the_body_limit(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        # The put helper sends ASCII-only JSON: "가" is 6 bytes (\uac00), "😀" 12 (a
        # surrogate pair), so 48,000 emoji is ~576 KB for a prompt within the limit.
        for prompt in ("가" * 24000 + "x" * 24000, "😀" * 48000):
            with self.subTest(first=prompt[0]):
                response = self.put(client, identity, "arch", {"prompt": prompt})
                self.assertEqual(response.status_code, 200, response.text[:200])
                self.assertEqual(response.json()["prompt"], prompt)
        too_big = client.put("/api/settings/agents/arch", content=b'{"prompt": "' + b"x" * (700 * 1024) + b'"}', headers=self.auth_headers(identity))
        self.assertEqual(too_big.status_code, 413)

    def test_admin_delete_resets_to_the_file_and_config(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        self.put(client, identity, "b", {"prompt": "Override b", "required": True})
        response = client.delete("/api/settings/agents/b", headers=self.auth_headers(identity))
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()
        self.assertEqual((row["prompt"], row["prompt_source"], row["required"], row["required_source"]), ("Prompt for b.\n", "file", False, "config"))

    def test_a_store_event_for_an_agent_never_carries_the_prompt(self) -> None:
        from code_review_service.dashboard_store import DashboardStore

        self.store = DashboardStore(self.service_root / "data" / "dashboard" / "dashboard.sqlite3")
        client = self.make_client()
        identity = self.login(client)
        self.assertEqual(self.put(client, identity, "arch", {"prompt": "Secret architecture prompt", "required": False}).status_code, 200)
        state = collect_sse(self.app, "/api/events?after=0", self.cookie(client), minimum_events=2)
        events = [json.loads(block.split("data: ", 1)[1]) for block in state["data_blocks"] if "data: " in block]
        self.assertEqual({event["payload"]["key"] for event in events if event["kind"] == "setting_updated"}, {"agent:arch", "agent_meta:arch"})
        for block in state["data_blocks"]:
            self.assertNotIn("Secret architecture prompt", block)
        self.assertTrue(all(event["payload"].get("value") is None for event in events))

    def test_concurrent_partial_puts_on_different_fields_both_land(self) -> None:
        """Regression (PR #23 review): tab A saves the prompt while tab B saves required.

        Tab A is paused right after it reads the stored override, wherever the
        handler reads it: inside the store's merge transaction, or between a
        get_setting and a set_setting. Tab B saves during that pause. A
        read-merge-write in one transaction makes B wait and keeps both fields;
        a get-then-set handler lets A's write revert B's field.
        """
        import threading

        from code_review_service.dashboard_store import DashboardStore

        store = self.store = DashboardStore(self.service_root / "data" / "dashboard" / "dashboard.sqlite3")
        app = self.make_app()
        tabs = []
        for _ in range(2):
            tab = TestClient(app, base_url=PUBLIC_URL)
            self.addCleanup(tab.close)
            tabs.append((tab, self.login(tab)))
        a_has_read, b_done = threading.Event(), threading.Event()

        def pause_after_the_first_read() -> None:
            if not a_has_read.is_set():
                a_has_read.set()
                # Under the transaction B cannot finish while A holds it: give up after 1 s.
                b_done.wait(1.0)

        real_get, real_merge = store.get_setting, store.merge_agent_setting

        def get_setting(key: str) -> Any:
            row = real_get(key)
            if key == "agent:arch":
                pause_after_the_first_read()
            return row

        def merge_agent_setting(agent_id: str, patch: Any, updated_by: str, normalize: Any, prepare: Any = None) -> Any:
            def normalize_after_read(value: Any) -> Any:
                pause_after_the_first_read()  # the first call follows the SELECT, inside the transaction
                return normalize(value)

            return real_merge(agent_id, patch, updated_by, normalize_after_read, prepare)

        store.get_setting = get_setting  # type: ignore[method-assign]
        store.merge_agent_setting = merge_agent_setting  # type: ignore[method-assign]
        statuses: list[int] = []

        def save(index: int, body: dict[str, Any]) -> None:
            tab, identity = tabs[index]
            statuses.append(self.put(tab, identity, "arch", body).status_code)

        tab_a = threading.Thread(target=save, args=(0, {"prompt": "Prompt from tab A"}))
        tab_a.start()
        self.assertTrue(a_has_read.wait(5), "tab A never read the stored override")
        save(1, {"required": False})
        b_done.set()
        tab_a.join(10)
        self.assertEqual(statuses, [200, 200])
        self.assertEqual(real_get("agent:arch")["value"], {"prompt": "Prompt from tab A", "required": False})
        self.assertEqual(store.get_agent_setting("arch")["value"]["prompt_base_sha"], svc.prompt_sha(self.ARCH_PROMPT))


class AgentPromptSupersededTest(_AgentFixture):
    """A changed prompt file replaces the dashboard prompt once; GET shows the kept edit for compare, rollback or discard."""

    NEW_DEFAULT = "New default C.\n"

    def edit_then_change_file(self, client: TestClient, identity: dict[str, Any], agent_id: str = "arch", edit: str = "Edited B.") -> None:
        self.assertEqual(self.put(client, identity, agent_id, {"prompt": edit}).status_code, 200)
        (self.service_root / "agents" / f"{agent_id}.md").write_text(self.NEW_DEFAULT, encoding="utf-8")

    B_SHA = svc.prompt_sha("Edited B.")

    def restore(self, client: TestClient, identity: dict[str, Any], agent_id: str = "arch", sha: str | None = B_SHA):
        query = "" if sha is None else f"?sha={sha}"
        return client.post(f"/api/settings/agents/{agent_id}/superseded/restore{query}", headers=self.auth_headers(identity))

    def discard(self, client: TestClient, identity: dict[str, Any], agent_id: str = "arch", sha: str | None = B_SHA):
        query = "" if sha is None else f"?sha={sha}"
        return client.delete(f"/api/settings/agents/{agent_id}/superseded{query}", headers=self.auth_headers(identity))

    def test_get_shows_the_new_default_in_use_and_the_replaced_edit_once(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        self.edit_then_change_file(client, identity)
        self.assertEqual(self.store.get_setting("agent:arch")["value"]["prompt_base_sha"], svc.prompt_sha(self.ARCH_PROMPT))
        arch = self.rows(client)["arch"]
        self.assertEqual((arch["prompt"], arch["prompt_source"]), (self.NEW_DEFAULT, "file"))
        kept = arch["prompt_superseded"]
        self.assertEqual(set(kept), {"prompt", "sha", "saved_at", "saved_by", "superseded_at"})
        self.assertEqual(kept["sha"], self.B_SHA)
        self.assertEqual(arch["default_sha"], svc.prompt_sha(self.NEW_DEFAULT))
        self.assertEqual((kept["prompt"], kept["saved_by"]), ("Edited B.", ADMIN_EMAIL))
        self.assertTrue(kept["saved_at"] and kept["superseded_at"])
        self.assertIsNone(self.rows(client)["a"]["prompt_superseded"])
        stored = self.store.get_setting("agent:arch")["value"]
        self.assertNotIn("prompt", stored)
        self.assertEqual(stored["superseded"]["prompt"], "Edited B.")
        # Once: the next GET finds nothing more to replace, and the row is stable.
        self.assertEqual(self.rows(client)["arch"], arch)
        after_change = [call for call in self.store.calls_named("reconcile_agent_setting") if call[1][1] != self.ARCH_PROMPT]
        self.assertEqual(after_change, [("reconcile_agent_setting", ("arch", self.NEW_DEFAULT), {})],
                         "only an agent with a stored prompt is reconciled, and only while it has one")
        # A viewer sees the same compare, read-only.
        viewer = self.make_client()
        self.viewer_login(viewer)
        self.assertEqual(self.rows(viewer)["arch"]["prompt_superseded"], kept)

    def test_the_kept_edit_names_who_saved_the_prompt_not_a_later_field_save(self) -> None:
        from code_review_service.dashboard_store import DashboardStore

        self.store = DashboardStore(self.service_root / "data" / "dashboard" / "dashboard.sqlite3")
        client = self.make_client()
        identity = self.login(client)
        self.assertEqual(self.put(client, identity, "arch", {"prompt": "Edited B."}).status_code, 200)
        saved_at = self.store.get_setting("agent:arch")["updated_at"]
        # Another admin later changes only Required: the row's updated_at/by move, the prompt's do not.
        with mock.patch("code_review_service.dashboard_store._now", return_value="2099-01-01T00:00:00+00:00"):
            self.store.merge_agent_setting("arch", {"required": False}, "bob@company.example", svc.validate_agent_document)
        (self.service_root / "agents" / "arch.md").write_text(self.NEW_DEFAULT, encoding="utf-8")
        kept = self.rows(client)["arch"]["prompt_superseded"]
        self.assertEqual((kept["prompt"], kept["saved_by"], kept["saved_at"]), ("Edited B.", ADMIN_EMAIL, saved_at))

    def test_get_reconciles_through_the_real_store_and_the_event_carries_no_text(self) -> None:
        # The production store, imported directly: a missing or broken
        # implementation fails this test instead of silently skipping it.
        from code_review_service.dashboard_store import DashboardStore

        self.store = DashboardStore(self.service_root / "data" / "dashboard" / "dashboard.sqlite3")
        client = self.make_client()
        identity = self.login(client)
        self.edit_then_change_file(client, identity)
        arch = self.rows(client)["arch"]
        self.assertEqual((arch["prompt"], arch["prompt_superseded"]["prompt"]), (self.NEW_DEFAULT, "Edited B."))
        events = self.store.events_after(0, 50)
        self.assertEqual(events[-1]["kind"], "agent_prompt_superseded")
        self.assertEqual(events[-1]["payload"], {"key": "agent:arch", "agent_id": "arch"})
        self.assertNotIn("Edited B", json.dumps(events))
        self.assertEqual(self.rows(client)["arch"], arch)
        self.assertEqual(len(self.store.events_after(0, 50)), len(events), "nothing replaced twice")

    def test_admin_restores_the_edit_and_the_next_file_change_replaces_it_again(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        self.edit_then_change_file(client, identity)
        self.rows(client)
        response = self.restore(client, identity)
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()
        self.assertEqual((row["prompt"], row["prompt_source"], row["prompt_superseded"], row["updated_by"]), ("Edited B.", "dashboard", None, ADMIN_EMAIL))
        self.assertEqual(self.store.get_setting("agent:arch")["value"]["prompt_base_sha"], svc.prompt_sha(self.NEW_DEFAULT))
        self.assertEqual(self.rows(client)["arch"], row, "restore answers the same row GET lists")
        self.assertEqual(self.restore(client, identity).status_code, 404, "nothing left to restore")
        (self.service_root / "agents" / "arch.md").write_text("Default D.\n", encoding="utf-8")
        arch = self.rows(client)["arch"]
        self.assertEqual((arch["prompt"], arch["prompt_superseded"]["prompt"]), ("Default D.\n", "Edited B."))

    def test_admin_discards_the_edit(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        self.edit_then_change_file(client, identity)
        self.rows(client)
        response = self.discard(client, identity)
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()
        self.assertEqual((row["prompt"], row["prompt_source"], row["prompt_superseded"]), (self.NEW_DEFAULT, "file", None))
        self.assertIsNone(self.store.get_setting("agent:arch"), "the kept edit was the only field")
        self.assertEqual(self.discard(client, identity).status_code, 404)
        self.assertEqual(self.rows(client)["arch"], row)

    def test_get_still_answers_when_the_reconcile_write_fails(self) -> None:
        import sqlite3

        client = self.make_client()
        identity = self.login(client)
        self.edit_then_change_file(client, identity)
        with mock.patch.object(self.store, "reconcile_agent_setting", side_effect=sqlite3.OperationalError("database is locked")):
            arch = self.rows(client)["arch"]
        self.assertEqual((arch["prompt"], arch["prompt_source"], arch["prompt_superseded"]), ("Edited B.", "dashboard", None),
                         "the row as read: no replacement this time, and no error page")
        self.assertEqual(arch["default_sha"], svc.prompt_sha(self.ARCH_PROMPT), "B is still the edit of A, not of the new file")
        response = self.put(client, identity, "arch", {"prompt": "Edited B, more.", "expected_default_sha": arch["default_sha"]})
        self.assertEqual(response.status_code, 409, "a save from that row cannot skip the replacement")

    def test_a_stale_tab_cannot_discard_or_roll_back_a_newer_kept_edit(self) -> None:
        from code_review_service.dashboard_store import DashboardStore

        self.store = DashboardStore(self.service_root / "data" / "dashboard" / "dashboard.sqlite3")
        tab_a, tab_b = self.make_client(), self.make_client()
        identity_a, identity_b = self.login(tab_a), self.login(tab_b)
        self.edit_then_change_file(tab_a, identity_a)
        self.assertEqual(self.rows(tab_a)["arch"]["prompt_superseded"]["sha"], self.B_SHA, "tab A shows B1 and arms Discard B")
        # Tab B saves a newer edit and the default changes again: B2 is now the kept edit.
        self.assertEqual(self.put(tab_b, identity_b, "arch", {"prompt": "Edited B2."}).status_code, 200)
        (self.service_root / "agents" / "arch.md").write_text("Default D.\n", encoding="utf-8")
        self.assertEqual(self.rows(tab_b)["arch"]["prompt_superseded"]["prompt"], "Edited B2.")
        # Tab A confirms without refreshing: refused, and B2 survives.
        for act in (self.discard, self.restore):
            response = act(tab_a, identity_a)
            self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.get_agent_setting("arch")["value"]["superseded"]["prompt"], "Edited B2.")
        self.assertNotIn("prompt", self.store.get_agent_setting("arch")["value"])

    def test_a_save_written_against_an_older_default_is_refused(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        shown = self.rows(client)["arch"]
        self.assertEqual(shown["default_sha"], svc.prompt_sha(self.ARCH_PROMPT))
        # A deploy changes the default while the page is open.
        (self.service_root / "agents" / "arch.md").write_text(self.NEW_DEFAULT, encoding="utf-8")
        body = {"prompt": "Edited from A.", "expected_default_sha": shown["default_sha"]}
        response = self.put(client, identity, "arch", body)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIsNone(self.store.get_setting("agent:arch"), "nothing saved")
        # Written against the default now on disk, it saves; a malformed echo is a 400.
        body["expected_default_sha"] = svc.prompt_sha(self.NEW_DEFAULT)
        self.assertEqual(self.put(client, identity, "arch", body).status_code, 200)
        self.assertEqual(self.put(client, identity, "arch", {"prompt": "x", "expected_default_sha": "nothex"}).status_code, 400)

    def test_the_save_checks_its_default_inside_the_write_transaction(self) -> None:
        from code_review_service.dashboard_store import DashboardStore

        self.store = DashboardStore(self.service_root / "data" / "dashboard" / "dashboard.sqlite3")
        client = self.make_client()
        identity = self.login(client)
        shown = self.rows(client)["arch"]
        real_merge = self.store.merge_agent_setting

        def deploy_then_merge(*args, **kwargs):
            # The PUT waited behind another writer while a deploy shipped C.
            (self.service_root / "agents" / "arch.md").write_text(self.NEW_DEFAULT, encoding="utf-8")
            return real_merge(*args, **kwargs)

        with mock.patch.object(self.store, "merge_agent_setting", side_effect=deploy_then_merge):
            response = self.put(client, identity, "arch", {"prompt": "Edited from A.", "expected_default_sha": shown["default_sha"]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIsNone(self.store.get_agent_setting("arch"), "nothing saved")

    def test_the_save_reads_the_prompt_path_the_config_names_inside_the_write_transaction(self) -> None:
        from code_review_service.dashboard_store import DashboardStore

        self.store = DashboardStore(self.service_root / "data" / "dashboard" / "dashboard.sqlite3")
        client = self.make_client()
        identity = self.login(client)
        shown = self.rows(client)["arch"]
        (self.service_root / "agents" / "arch-c.md").write_text(self.NEW_DEFAULT, encoding="utf-8")
        real_merge = self.store.merge_agent_setting

        def move_prompt_path_then_merge(*args, **kwargs):
            # The PUT waited behind another writer while a deploy pointed arch at a new file.
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
            next(agent for agent in config["agents"] if agent["id"] == "arch")["prompt_path"] = "../agents/arch-c.md"
            self.config_path.write_text(json.dumps(config), encoding="utf-8")
            return real_merge(*args, **kwargs)

        with mock.patch.object(self.store, "merge_agent_setting", side_effect=move_prompt_path_then_merge):
            response = self.put(client, identity, "arch", {"prompt": "Edited from A.", "expected_default_sha": shown["default_sha"]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIsNone(self.store.get_agent_setting("arch"), "nothing saved")

    def test_rollback_reads_the_prompt_path_the_config_names_inside_the_write_transaction(self) -> None:
        from code_review_service.dashboard_store import DashboardStore

        self.store = DashboardStore(self.service_root / "data" / "dashboard" / "dashboard.sqlite3")
        client = self.make_client()
        identity = self.login(client)
        self.edit_then_change_file(client, identity)
        self.rows(client)
        (self.service_root / "agents" / "arch-d.md").write_text("Default D.\n", encoding="utf-8")
        real_restore = self.store.restore_superseded_prompt

        def move_prompt_path_then_restore(*args, **kwargs):
            # The Rollback waited behind another writer while a deploy pointed arch at a new file.
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
            next(agent for agent in config["agents"] if agent["id"] == "arch")["prompt_path"] = "../agents/arch-d.md"
            self.config_path.write_text(json.dumps(config), encoding="utf-8")
            return real_restore(*args, **kwargs)

        with mock.patch.object(self.store, "restore_superseded_prompt", side_effect=move_prompt_path_then_restore):
            self.assertEqual(self.restore(client, identity).status_code, 200)
        self.assertEqual(self.store.get_agent_setting("arch")["value"].get("prompt_base_sha"), svc.prompt_sha("Default D.\n"))
        self.assertEqual(self.rows(client)["arch"]["prompt"], "Edited B.", "B rolled back onto D is not replaced by D again")

    def test_a_config_that_cannot_be_read_inside_the_save_is_a_409_not_a_500(self) -> None:
        from code_review_service.dashboard_store import DashboardStore

        self.store = DashboardStore(self.service_root / "data" / "dashboard" / "dashboard.sqlite3")
        client = self.make_client()
        identity = self.login(client)
        shown = self.rows(client)["arch"]
        real_merge = self.store.merge_agent_setting

        def unreadable_config_then_merge(*args, **kwargs):
            with mock.patch.object(svc, "load_config", side_effect=PermissionError("config is being replaced")):
                return real_merge(*args, **kwargs)

        with mock.patch.object(self.store, "merge_agent_setting", side_effect=unreadable_config_then_merge):
            response = self.put(client, identity, "arch", {"prompt": "Edited from A.", "expected_default_sha": shown["default_sha"]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("cannot be read", response.json()["error"])
        self.assertIsNone(self.store.get_agent_setting("arch"), "nothing saved")

    def test_a_save_whose_default_cannot_be_read_is_refused_when_it_names_one(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        shown = self.rows(client)["arch"]
        (self.service_root / "agents" / "arch.md").unlink()
        response = self.put(client, identity, "arch", {"prompt": "Edited from A.", "expected_default_sha": shown["default_sha"]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("cannot be read", response.json()["error"])
        self.assertIsNone(self.store.get_setting("agent:arch"), "nothing saved")

    def test_a_row_describes_the_file_its_reconcile_decided_with(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        self.assertEqual(self.put(client, identity, "arch", {"prompt": "Edited B."}).status_code, 200)
        reads = iter([self.ARCH_PROMPT] + ["Default D.\n"] * 20)  # a deploy lands right after the reconcile read
        with mock.patch.object(svc, "read_agent_prompt_file", lambda agent, config_dir: next(reads) if agent["id"] == "arch" else "x"):
            arch = self.rows(client)["arch"]
        self.assertEqual((arch["prompt"], arch["default_sha"]), ("Edited B.", svc.prompt_sha(self.ARCH_PROMPT)),
                         "a save from this row must be checked against the file the edit was compared with")

    def test_a_field_save_answers_with_a_reconciled_row(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        self.edit_then_change_file(client, identity)  # no GET since the file changed
        row = self.put(client, identity, "arch", {"required": False}).json()
        self.assertEqual((row["prompt"], row["prompt_source"], row["prompt_superseded"]["prompt"], row["default_sha"]),
                         (self.NEW_DEFAULT, "file", "Edited B.", svc.prompt_sha(self.NEW_DEFAULT)),
                         "never the replaced prompt beside the new file's sha")

    def test_a_legacy_prompt_keeps_who_saved_it_when_the_first_save_is_another_field(self) -> None:
        from code_review_service.dashboard_store import DashboardStore

        self.store = DashboardStore(self.service_root / "data" / "dashboard" / "dashboard.sqlite3")
        with mock.patch("code_review_service.dashboard_store._now", return_value="2026-09-01T00:00:00+00:00"):
            self.store.set_setting("agent:arch", {"prompt": "Legacy B."}, "alice@company.example")  # before this release
        client = self.make_client()
        identity = self.login(client)
        # A tab left open across the deploy saves Required before any reconcile ran.
        with mock.patch.object(self.store, "reconcile_agent_setting", side_effect=RuntimeError("not yet")):
            self.assertEqual(self.put(client, identity, "arch", {"required": False}).status_code, 200)
        (self.service_root / "agents" / "arch.md").write_text(self.NEW_DEFAULT, encoding="utf-8")
        self.rows(client)  # adopts against the new file: nothing replaced yet
        (self.service_root / "agents" / "arch.md").write_text("Default D.\n", encoding="utf-8")
        kept = self.rows(client)["arch"]["prompt_superseded"]
        self.assertEqual((kept["prompt"], kept["saved_by"], kept["saved_at"]), ("Legacy B.", "alice@company.example", "2026-09-01T00:00:00+00:00"))

    def test_a_prompt_save_replaces_the_kept_edit_and_a_field_save_keeps_it(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        self.edit_then_change_file(client, identity)
        self.rows(client)
        row = self.put(client, identity, "arch", {"required": False}).json()
        self.assertEqual((row["required_source"], row["prompt_superseded"]["prompt"]), ("dashboard", "Edited B."))
        row = self.put(client, identity, "arch", {"prompt": "Edited E."}).json()
        self.assertEqual((row["prompt"], row["prompt_source"], row["prompt_superseded"]), ("Edited E.", "dashboard", None))
        self.assertEqual(self.store.get_setting("agent:arch")["value"], {
            "prompt": "Edited E.", "prompt_base_sha": svc.prompt_sha(self.NEW_DEFAULT), "required": False,
            "prompt_saved_at": mock.ANY, "prompt_saved_by": ADMIN_EMAIL,
        })
        self.assertEqual(self.rows(client)["arch"]["prompt_superseded"], None)
        # Reset to defaults drops everything, the kept edit included, but only
        # when the page names that kept edit: one it never showed is refused.
        (self.service_root / "agents" / "arch.md").write_text("Default D.\n", encoding="utf-8")
        self.assertEqual(self.rows(client)["arch"]["prompt_superseded"]["prompt"], "Edited E.")
        for query in ("", f"?superseded_sha={self.B_SHA}"):
            refused = client.delete(f"/api/settings/agents/arch{query}", headers=self.auth_headers(identity))
            self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(client.delete("/api/settings/agents/arch?superseded_sha=nothex", headers=self.auth_headers(identity)).status_code, 400)
        self.assertEqual(self.rows(client)["arch"]["prompt_superseded"]["prompt"], "Edited E.")
        reset = client.delete(f"/api/settings/agents/arch?superseded_sha={svc.prompt_sha('Edited E.')}", headers=self.auth_headers(identity)).json()
        self.assertEqual((reset["prompt"], reset["prompt_superseded"]), ("Default D.\n", None))
        self.assertIsNone(self.store.get_setting("agent:arch"))

    def test_viewer_cannot_restore_or_discard_and_the_guards_apply(self) -> None:
        admin = self.make_client()
        identity = self.login(admin)
        self.edit_then_change_file(admin, identity)
        self.rows(admin)
        viewer = self.make_client()
        viewer_identity = self.viewer_login(viewer)
        for response in (self.restore(viewer, viewer_identity), self.discard(viewer, viewer_identity)):
            self.assertEqual((response.status_code, response.json()["error"]), (403, "admin role required"))
        no_csrf = {k: v for k, v in self.auth_headers(identity).items() if k != "X-CSRF-Token"}
        self.assertEqual(admin.post("/api/settings/agents/arch/superseded/restore", headers=no_csrf).status_code, 403)
        self.assertEqual(admin.delete("/api/settings/agents/arch/superseded", headers=no_csrf).status_code, 403)
        for agent_id in ("nope", "off", "Arch"):
            with self.subTest(agent_id=agent_id):
                self.assertEqual(self.restore(admin, identity, agent_id).status_code, 404)
                self.assertEqual(self.discard(admin, identity, agent_id).status_code, 404)
        for agent_id in ("a", "c"):
            with self.subTest(agent_id=agent_id):
                self.assertEqual(self.restore(admin, identity, agent_id).status_code, 404, "no kept edit")
                self.assertEqual(self.discard(admin, identity, agent_id).status_code, 404, "no kept edit")
        for act in (self.restore, self.discard):
            self.assertEqual(act(admin, identity, "arch", sha=None).status_code, 400, "the kept edit on screen must be named")
            self.assertEqual(act(admin, identity, "arch", sha="xyz").status_code, 400)
        self.assertEqual(self.store.calls_named("restore_superseded_prompt"), [("restore_superseded_prompt", ("a", svc.prompt_sha("Prompt for a.\n"), ADMIN_EMAIL), {}),
                                                                              ("restore_superseded_prompt", ("c", None, ADMIN_EMAIL), {})])
        self.assertEqual(self.store.get_setting("agent:arch")["value"]["superseded"]["prompt"], "Edited B.")

    def test_put_body_refuses_the_server_managed_fields(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        kept = {"prompt": "x", "saved_at": None, "saved_by": None, "superseded_at": "t"}
        for field, value in (("prompt_base_sha", "a" * 64), ("superseded", kept), ("superseded", None)):
            with self.subTest(field=field):
                response = self.put(client, identity, "arch", {"prompt": "Edited B.", field: value})
                self.assertEqual(response.status_code, 400, response.text[:200])
                self.assertEqual(response.json()["error"], f"unknown fields: {field}")
        self.assertEqual(self.store.calls_named("merge_agent_setting"), [])

    def test_anonymous_access_is_denied(self) -> None:
        client = self.make_client()
        self.assertEqual(client.post("/api/settings/agents/arch/superseded/restore", headers={"Origin": PUBLIC_URL}).status_code, 401)
        self.assertEqual(client.delete("/api/settings/agents/arch/superseded", headers={"Origin": PUBLIC_URL}).status_code, 401)


# ---------------------------------------------------------------------------
# Review repositories added on the dashboard
# ---------------------------------------------------------------------------


class RepositorySettingsTest(AppTestCase):
    NOT_VISIBLE_ERROR = (
        "The review account cannot see acme-corp/dashboard on GitHub (gh: Not Found (HTTP 404)). "
        "Give it read access to the repository, then add it again."
    )

    def setUp(self) -> None:
        super().setUp()
        self.config_path.write_text(json.dumps({
            "repositories": ["acme-corp/Gamma", "acme-corp/review-of-legends"],
            "repository_agents": {"acme-corp/Gamma": {"arch": True}},
            "agents": [
                {"id": "a", "prompt_path": "../agents/a.md"},
                {"id": "b", "prompt_path": "../agents/b.md"},
                {"id": "c", "prompt_path": "../agents/c.md"},
                {"id": "arch", "opt_in": True, "prompt_path": "../agents/arch.md"},
            ],
            "gh_bin": "gh",
        }), encoding="utf-8")
        self.gh_calls: list[tuple[list[str], int]] = []

    def fake_gh(self, stdout: str = "", returncode: int = 0, stderr: str = "", raises: BaseException | None = None) -> None:
        def run(args, timeout, cwd=None, env=None):
            self.gh_calls.append((list(args), timeout))
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)

        patcher = mock.patch.object(svc, "run_command", run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def store_added(self, *repos: str) -> None:
        self.store._settings["repositories"] = {
            "key": "repositories",
            "value": {"added": [{"repo": repo, "added_by": ADMIN_EMAIL, "added_at": "2026-10-13T00:00:00+00:00"} for repo in repos]},
            "updated_at": "2026-10-13T00:00:00+00:00",
            "updated_by": ADMIN_EMAIL,
        }

    def viewer_login(self, client: TestClient) -> dict[str, Any]:
        self.oauth.token = {"access_token": "at", "userinfo": dict(VIEWER_USERINFO)}
        return self.login(client)

    def add(self, client: TestClient, identity: dict[str, Any], body: Any):
        return client.post("/api/settings/repositories", content=json.dumps(body), headers=self.auth_headers(identity))

    def remove(self, client: TestClient, identity: dict[str, Any], repo: str):
        return client.delete(f"/api/settings/repositories/{repo}", headers=self.auth_headers(identity))

    def test_viewer_reads_config_and_added_repositories_with_their_agents(self) -> None:
        self.store_added("acme-corp/MiniMoon")
        client = self.make_client()
        self.viewer_login(client)
        response = client.get("/api/settings/repositories")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["repositories"], [
            {"repo": "acme-corp/Gamma", "source": "config", "agents": ["a", "b", "c", "arch"], "added_by": None, "added_at": None, "problem": None},
            {"repo": "acme-corp/review-of-legends", "source": "config", "agents": ["a", "b", "c"], "added_by": None, "added_at": None, "problem": None},
            {"repo": "acme-corp/MiniMoon", "source": "dashboard", "agents": ["a", "b", "c"],
             "added_by": ADMIN_EMAIL, "added_at": "2026-10-13T00:00:00+00:00", "problem": None},
        ])
        self.assertNotIn("error", payload)
        self.assertEqual((payload["allowed_owners"], payload["max_added"]), (["acme-corp"], 50))
        self.assertEqual(payload["note"], dashboard_web.BASELINE_NOTE)
        self.assertIn("first added are not reviewed", payload["note"])
        # Without a store the config repositories are still listed.
        bare = self.make_client(store=None)
        self.viewer_login(bare)
        bare_payload = bare.get("/api/settings/repositories").json()
        self.assertEqual([row["repo"] for row in bare_payload["repositories"]],
                         ["acme-corp/Gamma", "acme-corp/review-of-legends"])
        self.assertIn("dashboard store is unavailable", bare_payload["error"])

    def test_viewer_and_anonymous_cannot_add_or_remove(self) -> None:
        self.fake_gh(stdout="acme-corp/MiniMoon\n")
        self.store_added("acme-corp/MiniMoon")
        anonymous = self.make_client()
        self.assertEqual(anonymous.get("/api/settings/repositories").status_code, 401)
        self.assertEqual(anonymous.post("/api/settings/repositories", content="{}", headers={"Content-Type": "application/json", "Origin": PUBLIC_URL}).status_code, 401)
        self.assertEqual(anonymous.delete("/api/settings/repositories/acme-corp/MiniMoon", headers={"Origin": PUBLIC_URL}).status_code, 401)
        client = self.make_client()
        identity = self.viewer_login(client)
        added = self.add(client, identity, {"repo": "acme-corp/MiniMoonBo"})
        self.assertEqual((added.status_code, added.json()["error"]), (403, "admin role required"))
        removed = self.remove(client, identity, "acme-corp/MiniMoon")
        self.assertEqual((removed.status_code, removed.json()["error"]), (403, "admin role required"))
        self.assertEqual(self.store.calls_named("set_setting"), [])
        self.assertEqual(self.store.calls_named("delete_setting"), [])
        self.assertEqual(self.gh_calls, [])

    def test_admin_adds_a_repository_under_the_name_github_reports(self) -> None:
        self.fake_gh(stdout="acme-corp/MiniMoon\n")
        client = self.make_client()
        identity = self.login(client)
        self.assertEqual(client.get("/api/runs?repo=acme-corp/MiniMoon").status_code, 400, "not reviewed yet")

        response = self.add(client, identity, {"repo": " acme-corp/minimoon "})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["added"], "acme-corp/MiniMoon")
        self.assertIn("first added are not reviewed", payload["note"])
        self.assertEqual(self.gh_calls, [(["gh", "api", "repos/acme-corp/minimoon", "--jq", ".full_name"], 20)])
        key, value, updated_by = self.store.calls_named("set_setting")[-1][1]
        self.assertEqual((key, updated_by), ("repositories", ADMIN_EMAIL))
        self.assertEqual([(entry["repo"], entry["added_by"]) for entry in value["added"]], [("acme-corp/MiniMoon", ADMIN_EMAIL)])
        self.assertRegex(value["added"][0]["added_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        row = payload["repositories"][-1]
        self.assertEqual((row["repo"], row["source"], row["agents"], row["added_by"]),
                         ("acme-corp/MiniMoon", "dashboard", ["a", "b", "c"], ADMIN_EMAIL))
        listed = client.get("/api/settings/repositories").json()
        self.assertEqual(listed, {key: item for key, item in payload.items() if key != "added"}, "POST answers the GET payload")

        # The queue filters now accept it, and the agents page lists it.
        self.assertEqual(client.get("/api/runs?repo=acme-corp/MiniMoon").status_code, 200)
        self.assertEqual(client.get("/api/retries?repo=acme-corp/MiniMoon").status_code, 200)
        agents = {row["id"]: row for row in client.get("/api/settings/agents").json()["agents"]}
        self.assertIn("acme-corp/MiniMoon", agents["a"]["repos"])
        self.assertNotIn("acme-corp/MiniMoon", agents["arch"]["repos"])

    def test_a_repository_the_review_account_cannot_see_is_refused_with_githubs_reason(self) -> None:
        self.fake_gh(returncode=1, stderr="gh: Not Found (HTTP 404)\n",
                     stdout='{"message":"Not Found","documentation_url":"https://docs.github.com/rest","status":"404"}')
        client = self.make_client()
        identity = self.login(client)
        response = self.add(client, identity, {"repo": "acme-corp/dashboard"})
        self.assertEqual((response.status_code, response.json()["error"]), (400, self.NOT_VISIBLE_ERROR))
        self.assertEqual(self.store.calls_named("set_setting"), [])

    def test_bad_requests_are_refused_before_github_is_asked(self) -> None:
        self.fake_gh(stdout="never/used\n")
        self.store_added("acme-corp/MiniMoon")
        client = self.make_client()
        identity = self.login(client)
        for body, message in (
            ({"repo": "acme-corp/gamma"}, "acme-corp/Gamma is already reviewed: it is in config/reviewer.json."),
            ({"repo": "ACME-CORP/minimoon"}, "acme-corp/MiniMoon is already reviewed: it was added on this dashboard."),
            ({"repo": "octocat/hello-world"}, "octocat is not an owner this service reviews (allowed: acme-corp)"),
            ({"repo": "MiniMoon"}, "a repository is written owner/name"),
            ({"repo": "acme-corp/.."}, "a repository is written owner/name"),
            ({"repo": 7}, "a repository is written owner/name"),
            ({}, "a repository is written owner/name"),
            ({"repo": "acme-corp/x", "added_by": "me"}, "unknown fields: added_by"),
            (["acme-corp/x"], "JSON object"),
        ):
            with self.subTest(body=body):
                response = self.add(client, identity, body)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn(message, response.json()["error"])
        self.assertEqual(self.gh_calls, [])
        self.assertEqual(self.store.calls_named("set_setting"), [])
        no_csrf = {k: v for k, v in self.auth_headers(identity).items() if k != "X-CSRF-Token"}
        self.assertEqual(client.post("/api/settings/repositories", content=json.dumps({"repo": "acme-corp/x"}), headers=no_csrf).status_code, 403)

    def test_githubs_canonical_name_is_checked_again(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        # A renamed repository redirects to one that is already reviewed.
        self.fake_gh(stdout="acme-corp/Gamma\n")
        response = self.add(client, identity, {"repo": "acme-corp/gamma-old"})
        self.assertEqual((response.status_code, response.json()["error"]),
                         (400, "acme-corp/Gamma is already reviewed: it is in config/reviewer.json."))
        # A transferred repository answers with an owner the service does not review.
        self.fake_gh(stdout="octocat/moved\n")
        response = self.add(client, identity, {"repo": "acme-corp/moved"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "GitHub reports acme-corp/moved as octocat/moved: "
                                                   "octocat is not an owner this service reviews (allowed: acme-corp)")
        self.assertEqual(self.store.calls_named("set_setting"), [])

    def test_the_fifty_repository_cap_is_a_plain_400(self) -> None:
        self.fake_gh(stdout="acme-corp/one-more\n")
        self.store_added(*[f"acme-corp/repo-{index}" for index in range(50)])
        client = self.make_client()
        identity = self.login(client)
        response = self.add(client, identity, {"repo": "acme-corp/one-more"})
        self.assertEqual((response.status_code, response.json()["error"]),
                         (400, "At most 50 repositories can be added on the dashboard; remove one first."))
        self.assertEqual(self.gh_calls, [])

    def test_a_missing_or_silent_github_cli_is_a_503(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        for raises, message in (
            (FileNotFoundError(2, "No such file or directory", "gh"), "The dashboard could not run the GitHub CLI to check acme-corp/MiniMoon; it was not added."),
            (subprocess.TimeoutExpired(["gh"], 20), "GitHub did not answer within 20 seconds while checking acme-corp/MiniMoon; try again."),
        ):
            with self.subTest(raises=type(raises).__name__):
                self.fake_gh(raises=raises)
                response = self.add(client, identity, {"repo": "acme-corp/MiniMoon"})
                self.assertEqual((response.status_code, response.json()["error"]), (503, message))
        self.fake_gh(stdout="\n")
        self.assertEqual(self.add(client, identity, {"repo": "acme-corp/MiniMoon"}).status_code, 503)
        self.assertEqual(self.store.calls_named("set_setting"), [])

    def test_admin_removes_only_dashboard_repositories(self) -> None:
        self.store_added("acme-corp/MiniMoon", "acme-corp/MiniMoonBo")
        client = self.make_client()
        identity = self.login(client)
        response = self.remove(client, identity, "acme-corp/minimoon")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["removed"], "acme-corp/MiniMoon")
        self.assertEqual([row["repo"] for row in payload["repositories"] if row["source"] == "dashboard"], ["acme-corp/MiniMoonBo"])
        self.assertEqual([entry["repo"] for entry in self.store.calls_named("set_setting")[-1][1][1]["added"]], ["acme-corp/MiniMoonBo"])

        # Removing the last one deletes the setting.
        self.assertEqual(self.remove(client, identity, "acme-corp/MiniMoonBo").status_code, 200)
        self.assertEqual(self.store.calls_named("delete_setting")[-1][1], ("repositories", ADMIN_EMAIL))
        self.assertIsNone(self.store.get_setting("repositories"))

        config_repo = self.remove(client, identity, "acme-corp/gamma")
        self.assertEqual((config_repo.status_code, config_repo.json()["error"]),
                         (409, "acme-corp/Gamma is configured in config/reviewer.json; remove it there."))
        unknown = self.remove(client, identity, "acme-corp/nope")
        self.assertEqual((unknown.status_code, unknown.json()["error"]), (404, "acme-corp/nope is not on the review list"))

    def test_a_retry_in_an_added_repository_is_queued(self) -> None:
        head = "a" * 40
        self.github.pr = open_pr(head)
        client = self.make_client()
        identity = self.login(client)
        run = self.failed_run(head, repo="acme-corp/MiniMoon")
        refused = client.post(f"/api/runs/{run['id']}/retry", content=json.dumps({"head_sha": head}), headers=self.auth_headers(identity))
        self.assertEqual((refused.status_code, refused.json()["error"]), (409, "run repository is not configured for review"))
        self.store_added("acme-corp/MiniMoon")
        response = client.post(f"/api/runs/{run['id']}/retry", content=json.dumps({"head_sha": head}), headers=self.auth_headers(identity))
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["request"]["repo"], "acme-corp/MiniMoon")
        # An attempt recorded under another spelling of the same repository is retryable too.
        other = self.failed_run("b" * 40, repo="acme-corp/minimoon", pr=8)
        self.github.pr = open_pr("b" * 40, number=8)
        again = client.post(f"/api/runs/{other['id']}/retry", content=json.dumps({"head_sha": "b" * 40}), headers=self.auth_headers(identity))
        self.assertEqual(again.status_code, 202, again.text)

    def test_a_repository_too_few_agents_would_review_is_refused_and_marked(self) -> None:
        # Every config repository switches the opt-in c on; an added one would get a and b only.
        self.config_path.write_text(json.dumps({
            "repositories": ["acme-corp/Gamma", "acme-corp/review-of-legends"],
            "repository_agents": {"acme-corp/Gamma": {"c": True}, "acme-corp/review-of-legends": {"c": True}},
            "agents": [{"id": "a", "prompt_path": "../agents/a.md"}, {"id": "b", "prompt_path": "../agents/b.md"},
                       {"id": "c", "opt_in": True, "prompt_path": "../agents/c.md"}],
            "gh_bin": "gh",
        }), encoding="utf-8")
        self.fake_gh(stdout="acme-corp/MiniMoon\n")
        client = self.make_client()
        identity = self.login(client)
        response = self.add(client, identity, {"repo": "acme-corp/MiniMoon"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"],
                         "acme-corp/MiniMoon cannot be added: at least 3 agents must review acme-corp/MiniMoon; "
                         "check opt_in and repository_agents. A repository added here gets the default agents only; "
                         "to switch agents on for it, add it to config/reviewer.json instead.")
        self.assertEqual(self.gh_calls, [])
        self.assertEqual(self.store.calls_named("set_setting"), [])

        # One added before the config changed is listed with the reason, and can still be removed.
        self.store_added("acme-corp/MiniMoon")
        row = client.get("/api/settings/repositories").json()["repositories"][-1]
        self.assertEqual((row["repo"], row["agents"]), ("acme-corp/MiniMoon", ["a", "b"]))
        self.assertEqual(row["problem"], "at least 3 agents must review acme-corp/MiniMoon; check opt_in and repository_agents")
        self.assertEqual(client.get("/api/runs?repo=acme-corp/MiniMoon").status_code, 400, "the daemon skips it")
        self.assertEqual(self.remove(client, identity, "acme-corp/MiniMoon").status_code, 200)

    def test_an_unreadable_saved_list_refuses_add_and_remove_without_writing(self) -> None:
        from code_review_service.dashboard_store import DashboardStore

        db_path = self.service_root / "data" / "dashboard" / "dashboard.sqlite3"
        self.store = DashboardStore(db_path)
        self.fake_gh(stdout="acme-corp/MiniMoonBo\n")
        client = self.make_client()
        identity = self.login(client)

        def raw_row() -> Any:
            conn = sqlite3.connect(str(db_path))
            try:
                return conn.execute("SELECT value, updated_at, updated_by FROM settings WHERE key = 'repositories'").fetchone()
            finally:
                conn.close()

        for raw in ('{"added": [{"repo": "acme-corp/MiniMoon"', '["acme-corp/MiniMoon"]', '{"added": "acme-corp/MiniMoon"}'):
            with self.subTest(raw=raw):
                conn = sqlite3.connect(str(db_path))
                try:
                    with conn:
                        conn.execute(
                            "INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('repositories', ?, 't', 'x')"
                            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (raw,),
                        )
                finally:
                    conn.close()
                events = self.store.latest_event_id()
                added = self.add(client, identity, {"repo": "acme-corp/MiniMoonBo"})
                self.assertEqual((added.status_code, added.json()["error"]), (409, dashboard_web.UNREADABLE_REPOSITORIES))
                removed = self.remove(client, identity, "acme-corp/MiniMoon")
                self.assertEqual((removed.status_code, removed.json()["error"]), (409, dashboard_web.UNREADABLE_REPOSITORIES))
                self.assertEqual(raw_row(), (raw, "t", "x"), "the row is left exactly as it was")
                self.assertEqual(self.store.latest_event_id(), events, "no event either")
                listed = client.get("/api/settings/repositories").json()
                self.assertEqual([row["source"] for row in listed["repositories"]], ["config", "config"])
                self.assertIn("saved repository list cannot be read", listed["error"])
        self.assertEqual(self.gh_calls, [], "refused before GitHub is asked")

    def test_a_store_that_cannot_be_read_refuses_changes_and_says_so(self) -> None:
        class UnreadableRepositories(FakeDashboardStore):
            def get_setting(self, key: str):
                if key == "repositories":
                    raise sqlite3.OperationalError("database is locked")
                return super().get_setting(key)

        self.store = UnreadableRepositories()
        self.fake_gh(stdout="acme-corp/MiniMoon\n")
        client = self.make_client()
        identity = self.login(client)
        added = self.add(client, identity, {"repo": "acme-corp/MiniMoon"})
        self.assertEqual((added.status_code, added.json()["error"]), (503, dashboard_web.STORE_READ_FAILED))
        removed = self.remove(client, identity, "acme-corp/MiniMoon")
        self.assertEqual((removed.status_code, removed.json()["error"]), (503, dashboard_web.STORE_READ_FAILED))
        self.assertEqual(self.store.calls_named("set_setting"), [])
        self.assertEqual(self.store.calls_named("delete_setting"), [])
        self.assertEqual(self.gh_calls, [])
        listed = client.get("/api/settings/repositories")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([row["source"] for row in listed.json()["repositories"]], ["config", "config"])
        self.assertIn("could not be read", listed.json()["error"])
        self.assertIn("not removed: do not add them again", listed.json()["error"])

    def test_changes_write_back_every_entry_they_did_not_name(self) -> None:
        foreign = {"repo": "octocat/kept-as-stored", "added_by": "someone", "extra": 1}
        minimoon = {"repo": "acme-corp/MiniMoon", "added_by": ADMIN_EMAIL, "added_at": "2026-10-13T00:00:00+00:00"}
        self.store._settings["repositories"] = {"key": "repositories", "value": {"added": [foreign, minimoon]},
                                                "updated_at": "t", "updated_by": ADMIN_EMAIL}
        self.fake_gh(stdout="acme-corp/MiniMoonBo\n")
        client = self.make_client()
        identity = self.login(client)
        self.assertEqual(self.add(client, identity, {"repo": "acme-corp/MiniMoonBo"}).status_code, 200)
        written = self.store.calls_named("set_setting")[-1][1][1]["added"]
        self.assertEqual(written[:2], [foreign, minimoon])
        self.assertEqual(written[2]["repo"], "acme-corp/MiniMoonBo")
        self.assertEqual(self.remove(client, identity, "acme-corp/minimoon").status_code, 200)
        self.assertEqual([entry["repo"] for entry in self.store.calls_named("set_setting")[-1][1][1]["added"]],
                         ["octocat/kept-as-stored", "acme-corp/MiniMoonBo"])

    def test_delete_without_csrf_is_refused(self) -> None:
        self.store_added("acme-corp/MiniMoon")
        client = self.make_client()
        identity = self.login(client)
        no_csrf = {k: v for k, v in self.auth_headers(identity).items() if k != "X-CSRF-Token"}
        response = client.delete("/api/settings/repositories/acme-corp/MiniMoon", headers=no_csrf)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.calls_named("set_setting"), [])
        self.assertEqual(self.store.calls_named("delete_setting"), [])

    def test_a_store_event_for_repositories_carries_no_identity(self) -> None:
        from code_review_service.dashboard_store import DashboardStore

        self.store = DashboardStore(self.service_root / "data" / "dashboard" / "dashboard.sqlite3")
        self.fake_gh(stdout="acme-corp/MiniMoon\n")
        client = self.make_client()
        identity = self.login(client)
        self.assertEqual(self.add(client, identity, {"repo": "acme-corp/MiniMoon"}).status_code, 200)
        self.assertEqual(self.store.get_setting("repositories")["value"]["added"][0]["repo"], "acme-corp/MiniMoon")
        state = collect_sse(self.app, "/api/events?after=0", self.cookie(client), minimum_events=1)
        block = state["data_blocks"][-1]
        self.assertIn("setting_updated", block)
        event = json.loads(block.split("data: ", 1)[1])
        self.assertEqual(event["payload"], {"key": "repositories"})
        self.assertNotIn(ADMIN_EMAIL, block)


# ---------------------------------------------------------------------------
# Admin granted to an address on the Settings page
# ---------------------------------------------------------------------------


class AdminGrantsTest(AppTestCase):
    OWNER_ONLY = "owner role required"
    ADDED_AT = r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$"

    def start(self, store: Any = _SENTINEL) -> tuple[TestClient, dict[str, Any]]:
        """One app for every browser in the test; the owner signs in first."""
        self.app = self.make_app(store=store)
        return self.client_for(ADMIN_USERINFO)

    def client_for(self, userinfo: dict[str, Any]) -> tuple[TestClient, dict[str, Any]]:
        client = TestClient(self.app, base_url=PUBLIC_URL)
        self.addCleanup(client.close)
        self.oauth.token = {"access_token": "at", "userinfo": dict(userinfo)}
        return client, self.login(client)

    def callback_status(self, userinfo: dict[str, Any]) -> int:
        """Sign-in status for ``userinfo`` (302 signed in, 403 refused)."""
        client = TestClient(self.app, base_url=PUBLIC_URL)
        self.addCleanup(client.close)
        self.oauth.token = {"access_token": "at", "userinfo": dict(userinfo)}
        state = httpx.URL(client.get("/auth/login", follow_redirects=False).headers["location"]).params["state"]
        return client.get(f"/auth/callback?code=c&state={state}", follow_redirects=False).status_code

    def sessions(self) -> Any:
        return dashboard_auth.SessionStore(Path(self.settings()["auth_db_path"]))

    def store_granted(self, *emails: str) -> None:
        self.store._settings["admins"] = {
            "key": "admins",
            "value": {"added": [{"email": email, "added_by": ADMIN_EMAIL, "added_at": "2026-10-14T00:00:00+00:00"} for email in emails]},
            "updated_at": "2026-10-14T00:00:00+00:00",
            "updated_by": ADMIN_EMAIL,
        }

    def add(self, client: TestClient, identity: dict[str, Any], body: Any, **headers: str):
        return client.post("/api/settings/admins", content=json.dumps(body), headers={**self.auth_headers(identity), **headers})

    def remove(self, client: TestClient, identity: dict[str, Any], email: str, **headers: str):
        return client.delete(f"/api/settings/admins/{email}", headers={**self.auth_headers(identity), **headers})

    def admin_writes(self) -> list:
        return [call for call in self.store.calls if call[0] in ("set_setting", "delete_setting") and call[1][0] == "admins"]

    def test_the_owner_adds_lists_and_removes_an_admin(self) -> None:
        owner, identity = self.start()
        self.assertEqual((identity["role"], identity["owner"]), ("admin", True))
        listed = owner.get("/api/settings/admins")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json(), {"owner": ADMIN_EMAIL, "admins": [], "can_edit": True, "max_added": 50})

        response = self.add(owner, identity, {"email": " Ops@Example.COM "})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["added"], GRANTED_EMAIL)
        [row] = payload["admins"]
        self.assertEqual((row["email"], row["added_by"]), (GRANTED_EMAIL, ADMIN_EMAIL))
        self.assertRegex(row["added_at"], self.ADDED_AT)
        key, value, updated_by = self.store.calls_named("set_setting")[-1][1]
        self.assertEqual((key, updated_by), ("admins", ADMIN_EMAIL))
        self.assertEqual(value, {"added": [row]})
        self.assertEqual(owner.get("/api/settings/admins").json(),
                         {key: item for key, item in payload.items() if key != "added"}, "POST answers the GET payload")

        self.assertEqual(self.add(owner, identity, {"email": VIEWER_EMAIL}).status_code, 200)
        removed = self.remove(owner, identity, GRANTED_EMAIL)
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(removed.json()["removed"], GRANTED_EMAIL)
        self.assertEqual([row["email"] for row in removed.json()["admins"]], [VIEWER_EMAIL])
        self.assertEqual([entry["email"] for entry in self.store.calls_named("set_setting")[-1][1][1]["added"]], [VIEWER_EMAIL])

        # Removing the last one deletes the setting.
        last = self.remove(owner, identity, VIEWER_EMAIL)
        self.assertEqual((last.status_code, last.json()["admins"]), (200, []))
        self.assertEqual(self.store.calls_named("delete_setting")[-1][1], ("admins", ADMIN_EMAIL))
        self.assertIsNone(self.store.get_setting("admins"))

    def test_an_added_admin_acts_as_admin_but_cannot_grant_or_revoke(self) -> None:
        self.start()
        self.store_granted(GRANTED_EMAIL, "other@example.com")
        ops, identity = self.client_for(GRANTED_USERINFO)
        self.assertEqual((identity["email"], identity["role"], identity["owner"]), (GRANTED_EMAIL, "admin", False))
        put = ops.put("/api/settings/model-policy", content=json.dumps({"model": "grok-4.6"}), headers=self.auth_headers(identity))
        self.assertEqual(put.status_code, 200, put.text)
        self.assertEqual(self.store.calls_named("set_setting")[-1][1][0::2], ("model_policy", GRANTED_EMAIL))
        self.assertEqual(ops.get("/api/usage").status_code, 200)

        for response in (
            self.add(ops, identity, {"email": "new@example.com"}),
            self.remove(ops, identity, "other@example.com"),
            self.remove(ops, identity, GRANTED_EMAIL),
        ):
            self.assertEqual((response.status_code, response.json()["error"]), (403, self.OWNER_ONLY))
        self.assertEqual(self.admin_writes(), [])
        listed = ops.get("/api/settings/admins").json()
        self.assertEqual(listed["can_edit"], False)
        self.assertEqual([row["email"] for row in listed["admins"]], [GRANTED_EMAIL, "other@example.com"])

    def test_a_revoked_admin_is_signed_out_on_its_next_request(self) -> None:
        owner, owner_identity = self.start()
        self.assertEqual(self.add(owner, owner_identity, {"email": GRANTED_EMAIL}).status_code, 200)
        ops, _ = self.client_for(GRANTED_USERINFO)
        cookie = self.cookie(ops)
        self.assertEqual(ops.get("/api/runs").status_code, 200)

        self.assertEqual(self.remove(owner, owner_identity, GRANTED_EMAIL).status_code, 200)
        self.assertEqual(ops.get("/api/runs").status_code, 401)
        self.assertIsNone(self.sessions().get(cookie), "the session row is deleted, not just refused")
        self.assertEqual(ops.get("/api/me").status_code, 401)
        self.assertEqual(self.callback_status(GRANTED_USERINFO), 403, "outside the viewer domains it cannot sign in again")
        self.assertEqual(owner.get("/api/me").status_code, 200)

        # A company account is not downgraded to viewer either: its admin
        # session never proved viewer eligibility. A new sign-in does.
        self.assertEqual(self.add(owner, owner_identity, {"email": VIEWER_EMAIL}).status_code, 200)
        dev, dev_identity = self.client_for(VIEWER_USERINFO)
        self.assertEqual(dev_identity["role"], "admin")
        self.assertEqual(self.remove(owner, owner_identity, VIEWER_EMAIL).status_code, 200)
        self.assertEqual(dev.get("/api/me").status_code, 401)
        _, again = self.client_for(VIEWER_USERINFO)
        self.assertEqual(again["role"], "viewer")

    def test_a_viewer_granted_admin_is_upgraded_without_signing_in_again(self) -> None:
        owner, owner_identity = self.start()
        viewer, identity = self.client_for(VIEWER_USERINFO)
        self.assertEqual(identity["role"], "viewer")
        self.assertEqual(viewer.get("/api/usage").status_code, 403)

        self.assertEqual(self.add(owner, owner_identity, {"email": VIEWER_EMAIL}).status_code, 200)
        me = viewer.get("/api/me").json()
        self.assertEqual((me["role"], me["owner"], me["csrf_token"]), ("admin", False, identity["csrf_token"]))
        self.assertEqual(viewer.get("/api/usage").status_code, 200)
        put = viewer.put("/api/settings/model-policy", content=json.dumps({"model": "grok-4.6"}), headers=self.auth_headers(identity))
        self.assertEqual(put.status_code, 200, put.text)

        # Revoked again: back to the viewer role its own sign-in proved.
        self.assertEqual(self.remove(owner, owner_identity, VIEWER_EMAIL).status_code, 200)
        me = viewer.get("/api/me")
        self.assertEqual((me.status_code, me.json()["role"]), (200, "viewer"))
        self.assertEqual(viewer.get("/api/usage").status_code, 403)

    def test_bad_requests_are_refused_without_writing(self) -> None:
        owner, identity = self.start()
        self.store_granted(GRANTED_EMAIL)
        owner_message = f"{ADMIN_EMAIL} is the owner and is always admin."
        for body, message in (
            ({"email": "ops"}, "exactly one @"),
            ({"email": "new@@example.com"}, "exactly one @"),
            ({"email": "@example.com"}, "exactly one @"),
            ({"email": "new@"}, "exactly one @"),
            ({"email": "new user@example.com"}, "spaces or invisible characters"),
            ({"email": "new\u200b@example.com"}, "spaces or invisible characters"),
            ({"email": "new\x07@example.com"}, "spaces or invisible characters"),
            ({"email": "a" * 243 + "@example.com"}, "at most 254 characters"),
            ({"email": 7}, "email address"),
            ({"email": "   "}, "email address"),
            ({}, "email address"),
            ({"email": "new@example.com", "role": "owner"}, "unknown fields: role"),
            (["new@example.com"], "JSON object"),
            ({"email": ADMIN_EMAIL}, owner_message),
            ({"email": " Owner@Company.EXAMPLE "}, owner_message),
            ({"email": "OPS@example.com"}, "ops@example.com is already an admin."),
        ):
            with self.subTest(body=body):
                response = self.add(owner, identity, body)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn(message, response.json()["error"])
        self.store_granted(*[f"user{index}@example.com" for index in range(50)])
        full = self.add(owner, identity, {"email": "one-more@example.com"})
        self.assertEqual((full.status_code, full.json()["error"]), (400, "At most 50 admins can be added; remove one first."))

        refused_owner = self.remove(owner, identity, ADMIN_EMAIL)
        self.assertEqual((refused_owner.status_code, refused_owner.json()["error"]),
                         (409, f"{ADMIN_EMAIL} is the owner and is always admin; it cannot be removed."))
        unknown = self.remove(owner, identity, "nobody@example.com")
        self.assertEqual((unknown.status_code, unknown.json()["error"]), (404, "nobody@example.com is not an added admin."))
        self.assertEqual(self.remove(owner, identity, "not-an-address").status_code, 400)
        self.assertEqual(self.remove(owner, identity, "").status_code, 400)
        self.assertEqual(self.admin_writes(), [])

    def test_changes_need_csrf_and_the_origin(self) -> None:
        owner, identity = self.start()
        self.store_granted(GRANTED_EMAIL)
        headers = self.auth_headers(identity)
        no_csrf = {key: value for key, value in headers.items() if key != "X-CSRF-Token"}
        body = json.dumps({"email": "new@example.com"})
        self.assertEqual(owner.post("/api/settings/admins", content=body, headers=no_csrf).status_code, 403)
        self.assertEqual(self.add(owner, identity, {"email": "new@example.com"}, Origin="https://evil.example").status_code, 403)
        self.assertEqual(owner.delete(f"/api/settings/admins/{GRANTED_EMAIL}", headers=no_csrf).status_code, 403)
        self.assertEqual(self.remove(owner, identity, GRANTED_EMAIL, Origin="https://evil.example").status_code, 403)
        self.assertEqual(self.admin_writes(), [])

    def test_viewers_read_the_list_and_anonymous_requests_are_refused(self) -> None:
        self.start()
        self.store_granted(GRANTED_EMAIL)
        anonymous = TestClient(self.app, base_url=PUBLIC_URL)
        self.addCleanup(anonymous.close)
        self.assertEqual(anonymous.get("/api/settings/admins").status_code, 401)
        self.assertEqual(anonymous.post("/api/settings/admins", content="{}", headers={"Content-Type": "application/json", "Origin": PUBLIC_URL}).status_code, 401)
        self.assertEqual(anonymous.delete(f"/api/settings/admins/{GRANTED_EMAIL}", headers={"Origin": PUBLIC_URL}).status_code, 401)

        viewer, identity = self.client_for(VIEWER_USERINFO)
        listed = viewer.get("/api/settings/admins")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json(), {
            "owner": ADMIN_EMAIL,
            "admins": [{"email": GRANTED_EMAIL, "added_by": ADMIN_EMAIL, "added_at": "2026-10-14T00:00:00+00:00"}],
            "can_edit": False,
            "max_added": 50,
        })
        added = self.add(viewer, identity, {"email": "new@example.com"})
        self.assertEqual((added.status_code, added.json()["error"]), (403, self.OWNER_ONLY))
        removed = self.remove(viewer, identity, GRANTED_EMAIL)
        self.assertEqual((removed.status_code, removed.json()["error"]), (403, self.OWNER_ONLY))
        self.assertEqual(self.admin_writes(), [])

    def test_a_store_that_cannot_be_read_grants_nothing_but_the_owner_keeps_working(self) -> None:
        class FlakyAdmins(FakeDashboardStore):
            broken = False

            def get_setting(self, key: str):
                if key == "admins" and self.broken:
                    raise sqlite3.OperationalError("database is locked")
                return super().get_setting(key)

        self.store = FlakyAdmins()
        owner, _ = self.start()
        self.store_granted(GRANTED_EMAIL)
        ops, ops_identity = self.client_for(GRANTED_USERINFO)
        viewer, _ = self.client_for(VIEWER_USERINFO)

        # The owner's requests and sign-in never read the grant list.
        self.store.calls.clear()
        self.assertEqual(owner.get("/api/runs").status_code, 200)
        self.assertEqual(self.callback_status(ADMIN_USERINFO), 302)
        self.assertEqual(self.store.calls_named("get_setting"), [])

        self.store.broken = True
        me = owner.get("/api/me")
        self.assertEqual((me.status_code, me.json()["role"], me.json()["owner"]), (200, "admin", True))
        self.assertEqual(viewer.get("/api/me").json()["role"], "viewer")
        ops_cookie = self.cookie(ops)
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(ops.get("/api/me").status_code, 401, "fail closed: no readable grant, no admin")
        # Visible to the operator, without any session data.
        self.assertIn("dashboard admins unreadable, only the owner is admin: OperationalError('database is locked')",
                      stderr.getvalue())
        self.assertNotIn(ops_cookie, stderr.getvalue())
        self.assertNotIn(ops_identity["csrf_token"], stderr.getvalue())
        self.assertEqual(self.callback_status(GRANTED_USERINFO), 403)
        owner_identity = owner.get("/api/me").json()
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            listed = owner.get("/api/settings/admins")
            responses = (self.add(owner, owner_identity, {"email": "new@example.com"}),
                         self.remove(owner, owner_identity, GRANTED_EMAIL))
        self.assertEqual((listed.status_code, listed.json()["admins"]), (200, []))
        self.assertIn("could not be read", listed.json()["error"])
        for response in responses:
            self.assertEqual((response.status_code, response.json()["error"]), (503, dashboard_web.STORE_READ_FAILED))
        # The owner's list, add and remove say why in the log too, not only in the answer.
        self.assertEqual(stderr.getvalue().count("dashboard admin list unreadable: OperationalError('database is locked')"), 3,
                         stderr.getvalue())
        self.assertEqual(self.admin_writes(), [])

        self.store.broken = False
        self.assertEqual(ops.get("/api/me").status_code, 401, "the ended session stays ended")
        self.assertEqual(self.callback_status(GRANTED_USERINFO), 302)

    def test_without_a_store_only_the_owner_is_admin(self) -> None:
        owner, identity = self.start(store=None)
        minted = self.sessions().create(GRANTED_EMAIL, "777", role="granted")
        ops = TestClient(self.app, base_url=PUBLIC_URL, cookies={dashboard_auth.SESSION_COOKIE: minted["id"]})
        self.addCleanup(ops.close)
        self.assertEqual(ops.get("/api/me").status_code, 401)
        self.assertEqual(owner.get("/api/me").json()["role"], "admin")
        listed = owner.get("/api/settings/admins").json()
        self.assertEqual((listed["admins"], listed["can_edit"]), ([], True))
        self.assertIn("store is unavailable", listed["error"])
        self.assertEqual(self.add(owner, identity, {"email": GRANTED_EMAIL}).status_code, 503)

    def test_malformed_stored_entries_grant_nothing_and_a_clean_grant_replaces_them(self) -> None:
        dev = {"email": VIEWER_EMAIL, "added_by": ADMIN_EMAIL, "added_at": "2026-10-14T00:00:00+00:00"}
        stored = [{"email": "OPS@example.com"}, {"email": " ops@example.com"}, "ops@example.com", {"email": ADMIN_EMAIL}, dev]
        self.store._settings["admins"] = {"key": "admins", "value": {"added": list(stored)}, "updated_at": "t", "updated_by": ADMIN_EMAIL}
        owner, identity = self.start()
        self.assertEqual([row["email"] for row in owner.get("/api/settings/admins").json()["admins"]], [VIEWER_EMAIL])
        self.assertEqual(self.callback_status(GRANTED_USERINFO), 403)

        self.assertEqual(self.add(owner, identity, {"email": GRANTED_EMAIL}).status_code, 200)
        written = self.store.calls_named("set_setting")[-1][1][1]["added"]
        self.assertEqual(written[:3], ["ops@example.com", {"email": ADMIN_EMAIL}, dev], "entries it did not name stay as stored")
        self.assertEqual(written[3]["email"], GRANTED_EMAIL)
        self.assertEqual(self.callback_status(GRANTED_USERINFO), 302)

    def test_unhashable_stored_emails_are_skipped_by_the_list_and_the_write_answer(self) -> None:
        dev = {"email": VIEWER_EMAIL, "added_by": ADMIN_EMAIL, "added_at": "2026-10-14T00:00:00+00:00"}
        stored = [{"email": []}, {"email": {}}, dev]
        self.store._settings["admins"] = {"key": "admins", "value": {"added": list(stored)}, "updated_at": "t", "updated_by": ADMIN_EMAIL}
        owner, identity = self.start()
        listed = owner.get("/api/settings/admins")
        self.assertEqual((listed.status_code, [row["email"] for row in listed.json()["admins"]]), (200, [VIEWER_EMAIL]))
        added = self.add(owner, identity, {"email": GRANTED_EMAIL})
        self.assertEqual(added.status_code, 200, added.text[:200])
        self.assertEqual([row["email"] for row in added.json()["admins"]], [VIEWER_EMAIL, GRANTED_EMAIL])

    def test_a_revoked_grant_ends_an_open_event_stream(self) -> None:
        self.start()
        self.store_granted(GRANTED_EMAIL)
        ops, _ = self.client_for(GRANTED_USERINFO)

        def revoke_after_first_frame(block: str, state: dict[str, Any]) -> None:
            if "data:" in block and not state.get("revoked"):
                state["revoked"] = True
                self.store._settings.pop("admins", None)

        state = collect_sse(
            self.app, "/api/events", self.cookie(ops), minimum_events=10**6, timeout=10.0, on_block=revoke_after_first_frame
        )
        self.assertTrue(any("event: auth-expired" in block for block in state["blocks"]), state["blocks"])

    def test_an_address_with_a_slash_is_removed_through_its_encoded_path(self) -> None:
        owner, identity = self.start()
        self.assertEqual(self.add(owner, identity, {"email": "a/b@example.com"}).status_code, 200)
        response = owner.delete("/api/settings/admins/a%2Fb%40example.com", headers=self.auth_headers(identity))
        self.assertEqual((response.status_code, response.json().get("removed")), (200, "a/b@example.com"), response.text)

    def test_a_granted_address_google_does_not_vouch_for_still_cannot_sign_in(self) -> None:
        # Reproduction (PR #34 review): a consumer Google account (no ``hd``)
        # claiming a company address. The viewer rule refuses it; a grant must too.
        consumer = {"sub": "999", "email": VIEWER_EMAIL, "email_verified": True, "name": "Not Dev"}
        owner, identity = self.start()
        self.assertEqual(self.callback_status(consumer), 403)
        self.assertEqual(self.add(owner, identity, {"email": VIEWER_EMAIL}).status_code, 200)
        client = TestClient(self.app, base_url=PUBLIC_URL)
        self.addCleanup(client.close)
        self.oauth.token = {"access_token": "at", "userinfo": dict(consumer)}
        state = httpx.URL(client.get("/auth/login", follow_redirects=False).headers["location"]).params["state"]
        refused = client.get(f"/auth/callback?code=c&state={state}", follow_redirects=False)
        self.assertEqual(refused.status_code, 403)
        self.assertIn(GRANT_NEEDS_AUTHORITY, refused.json()["error"])
        self.assertNotIn(VIEWER_EMAIL, refused.text)
        self.assertNotIn(dashboard_auth.SESSION_COOKIE, refused.headers.get("set-cookie", ""))
        # The Workspace account that owns the address signs in as admin; so does a Gmail grant.
        _, dev = self.client_for(VIEWER_USERINFO)
        self.assertEqual((dev["role"], dev["owner"]), ("admin", False))
        gmail = {"sub": "888", "email": "ops@gmail.com", "email_verified": True}
        self.assertEqual(self.callback_status(gmail), 403)
        self.assertEqual(self.add(owner, identity, {"email": "ops@gmail.com"}).status_code, 200)
        self.assertEqual(self.callback_status(gmail), 302)

    def test_a_granted_sign_in_is_stored_as_granted_so_an_older_build_drops_it(self) -> None:
        # The rollback contract where the role is written: /auth/callback.
        self.start()
        self.store_granted(GRANTED_EMAIL)
        ops, identity = self.client_for(GRANTED_USERINFO)
        self.assertEqual(identity["role"], "admin", "resolved per request")
        cookie = self.cookie(ops)
        self.assertEqual(self.sessions().get(cookie)["role"], "granted")
        with mock.patch.object(dashboard_auth, "ROLES", ("admin", "viewer")):
            self.assertIsNone(self.sessions().get(cookie), "a build without grants drops the row")
        self.assertEqual(ops.get("/api/me").status_code, 401)

    def test_an_add_and_a_removal_at_the_same_time_keep_both_changes(self) -> None:
        # One TestClient context is one event loop, as under uvicorn, so the
        # handlers really contend for admins_lock. The add pauses right after
        # it reads the list; the removal runs during the pause.
        self.app = self.make_app()
        self.store_granted(GRANTED_EMAIL)
        real_get = self.store.get_setting
        add_has_read, release = threading.Event(), threading.Event()

        def get_setting(key: str) -> Any:
            row = real_get(key)
            if key == "admins" and not add_has_read.is_set():
                add_has_read.set()
                release.wait(5)
            return row

        statuses: dict[str, int] = {}
        with TestClient(self.app, base_url=PUBLIC_URL) as shared:
            self.oauth.token = {"access_token": "at", "userinfo": dict(ADMIN_USERINFO)}
            identity = self.login(shared)
            self.store.get_setting = get_setting  # type: ignore[method-assign]

            def add() -> None:
                statuses["add"] = self.add(shared, identity, {"email": "new@example.com"}).status_code

            def remove() -> None:
                statuses["remove"] = self.remove(shared, identity, GRANTED_EMAIL).status_code

            adder = threading.Thread(target=add)
            adder.start()
            self.assertTrue(add_has_read.wait(5), "the add never read the list")
            remover = threading.Thread(target=remove)
            remover.start()
            remover.join(0.5)
            self.assertTrue(remover.is_alive(), "the removal waits for the add")
            release.set()
            adder.join(10)
            remover.join(10)
        self.assertEqual(statuses, {"add": 200, "remove": 200})
        self.assertEqual([entry["email"] for entry in real_get("admins")["value"]["added"]], ["new@example.com"],
                         "the add did not write the removed address back")

    def test_removing_a_grant_deletes_its_sessions_so_adding_it_back_revives_none(self) -> None:
        other = {"sub": "778", "email": "other@example.com", "email_verified": True, "hd": "example.com"}
        owner, owner_identity = self.start()
        for email in (GRANTED_EMAIL, "other@example.com"):
            self.assertEqual(self.add(owner, owner_identity, {"email": email}).status_code, 200)
        ops, _ = self.client_for(GRANTED_USERINFO)
        others, _ = self.client_for(other)
        ops_cookie, other_cookie = self.cookie(ops), self.cookie(others)

        # One of two (the list is rewritten), then the last one (the setting is deleted).
        for email, cookie in ((GRANTED_EMAIL, ops_cookie), ("other@example.com", other_cookie)):
            with self.subTest(email=email):
                self.assertEqual(self.remove(owner, owner_identity, email).status_code, 200)
                self.assertIsNone(self.sessions().get(cookie), "deleted by the removal, before any request of its own")
        self.assertIsNone(self.store.get_setting("admins"))

        for email in (GRANTED_EMAIL, "other@example.com"):
            self.assertEqual(self.add(owner, owner_identity, {"email": email}).status_code, 200)
        self.assertEqual(ops.get("/api/me").status_code, 401)
        self.assertEqual(others.get("/api/me").status_code, 401)
        self.assertEqual(owner.get("/api/me").json()["owner"], True)

    def test_removing_a_grant_keeps_the_viewer_session_of_that_address(self) -> None:
        owner, owner_identity = self.start()
        viewer, _ = self.client_for(VIEWER_USERINFO)
        self.assertEqual(self.add(owner, owner_identity, {"email": VIEWER_EMAIL}).status_code, 200)
        self.assertEqual(viewer.get("/api/me").json()["role"], "admin")
        granted, _ = self.client_for(VIEWER_USERINFO)
        granted_cookie = self.cookie(granted)
        self.assertEqual(self.sessions().get(granted_cookie)["role"], "granted")

        self.assertEqual(self.remove(owner, owner_identity, VIEWER_EMAIL).status_code, 200)
        self.assertIsNone(self.sessions().get(granted_cookie))
        self.assertEqual(self.sessions().get(self.cookie(viewer))["role"], "viewer")
        me = viewer.get("/api/me")
        self.assertEqual((me.status_code, me.json()["role"]), (200, "viewer"))

    def test_a_removal_whose_purge_fails_keeps_the_grant_so_it_can_be_retried(self) -> None:
        other = {"sub": "778", "email": "other@example.com", "email_verified": True, "hd": "example.com"}
        owner, owner_identity = self.start()
        failing = TestClient(self.app, base_url=PUBLIC_URL, raise_server_exceptions=False,
                             cookies={dashboard_auth.SESSION_COOKIE: self.cookie(owner)})
        self.addCleanup(failing.close)
        for email in (GRANTED_EMAIL, "other@example.com"):
            self.assertEqual(self.add(owner, owner_identity, {"email": email}).status_code, 200)
        ops, _ = self.client_for(GRANTED_USERINFO)
        others, _ = self.client_for(other)

        # One of two (the list is rewritten), then the last one (the setting is deleted).
        for email, client in ((GRANTED_EMAIL, ops), ("other@example.com", others)):
            with self.subTest(email=email):
                cookie = self.cookie(client)
                locked = sqlite3.OperationalError("database is locked")
                with mock.patch.object(dashboard_auth.SessionStore, "revoke_admin_sessions", side_effect=locked):
                    self.assertEqual(self.remove(failing, owner_identity, email).status_code, 500)
                self.assertIn(email, [row["email"] for row in owner.get("/api/settings/admins").json()["admins"]],
                              "nothing is removed while its sessions still exist")
                self.assertEqual(client.get("/api/me").json()["role"], "admin", "still granted, consistently")

                self.assertEqual(self.remove(owner, owner_identity, email).status_code, 200, "the retry is not a 404")
                self.assertIsNone(self.sessions().get(cookie))
        self.assertIsNone(self.store.get_setting("admins"))
        for email in (GRANTED_EMAIL, "other@example.com"):
            self.assertEqual(self.add(owner, owner_identity, {"email": email}).status_code, 200)
        self.assertEqual(ops.get("/api/me").status_code, 401)
        self.assertEqual(others.get("/api/me").status_code, 401)

    def test_a_grant_deletes_admin_sessions_its_address_was_left_with(self) -> None:
        # However the address lost its grant (a hand edit of the store, a process
        # stopped mid-removal), a row minted under the old grant must not come
        # back as admin with the new one. Its viewer session is kept.
        owner, owner_identity = self.start()
        viewer, _ = self.client_for(VIEWER_USERINFO)
        stranded = self.sessions().create(GRANTED_EMAIL, "777", role="granted")
        stranded_admin = self.sessions().create(VIEWER_EMAIL, "555", role="admin")
        for email in (GRANTED_EMAIL, VIEWER_EMAIL):
            self.assertEqual(self.add(owner, owner_identity, {"email": email}).status_code, 200)
        for minted in (stranded, stranded_admin):
            self.assertIsNone(self.sessions().get(minted["id"]))
            late = TestClient(self.app, base_url=PUBLIC_URL, cookies={dashboard_auth.SESSION_COOKIE: minted["id"]})
            self.addCleanup(late.close)
            self.assertEqual(late.get("/api/me").status_code, 401)
        self.assertEqual(viewer.get("/api/me").json()["role"], "admin", "the viewer session is upgraded, not deleted")
        signed_in, _ = self.client_for(GRANTED_USERINFO)
        self.assertEqual(signed_in.get("/api/me").json()["role"], "admin", "a sign-in under the new grant works")
        # Adding it again is refused before anything is deleted: a live grant keeps its sessions.
        self.assertEqual(self.add(owner, owner_identity, {"email": GRANTED_EMAIL}).status_code, 400)
        self.assertEqual(self.sessions().get(self.cookie(signed_in))["role"], "granted")

    def test_a_grant_whose_purge_fails_is_not_written(self) -> None:
        owner, owner_identity = self.start()
        failing = TestClient(self.app, base_url=PUBLIC_URL, raise_server_exceptions=False,
                             cookies={dashboard_auth.SESSION_COOKIE: self.cookie(owner)})
        self.addCleanup(failing.close)
        stranded = self.sessions().create(GRANTED_EMAIL, "777", role="granted")
        locked = sqlite3.OperationalError("database is locked")
        with mock.patch.object(dashboard_auth.SessionStore, "revoke_admin_sessions", side_effect=locked):
            self.assertEqual(self.add(failing, owner_identity, {"email": GRANTED_EMAIL}).status_code, 500)
        self.assertEqual(self.admin_writes(), [], "no grant is published while a stale admin row survives")
        late = TestClient(self.app, base_url=PUBLIC_URL, cookies={dashboard_auth.SESSION_COOKIE: stranded["id"]})
        self.addCleanup(late.close)
        self.assertEqual(late.get("/api/me").status_code, 401)

        self.assertEqual(self.add(owner, owner_identity, {"email": GRANTED_EMAIL}).status_code, 200, "the retry works")
        self.assertEqual(late.get("/api/me").status_code, 401)

    def test_a_sign_in_paused_across_a_removal_is_deleted_with_it(self) -> None:
        # The sign-in read the grant and is about to store its session when the
        # owner removes the grant. Holding admins_lock from the grant read to
        # SessionStore.create makes the removal wait, so its purge sees the row.
        owner, owner_identity = self.start()
        self.assertEqual(self.add(owner, owner_identity, {"email": GRANTED_EMAIL}).status_code, 200)
        owner_cookie = self.cookie(owner)
        real_create = dashboard_auth.SessionStore.create
        about_to_create, release = threading.Event(), threading.Event()

        def create(store: Any, email: str, *args: Any, **kwargs: Any) -> Any:
            if email == GRANTED_EMAIL and not about_to_create.is_set():
                about_to_create.set()
                release.wait(5)
            return real_create(store, email, *args, **kwargs)

        results: dict[str, Any] = {}
        with mock.patch.object(dashboard_auth.SessionStore, "create", create), \
                TestClient(self.app, base_url=PUBLIC_URL) as shared:
            self.oauth.token = {"access_token": "at", "userinfo": dict(GRANTED_USERINFO)}
            state = httpx.URL(shared.get("/auth/login", follow_redirects=False).headers["location"]).params["state"]

            def sign_in() -> None:
                results["callback"] = shared.get(f"/auth/callback?code=c&state={state}", follow_redirects=False)

            def removal() -> None:
                # Only the owner's cookie: the explicit header replaces the jar.
                headers = {**self.auth_headers(owner_identity), "Cookie": f"{dashboard_auth.SESSION_COOKIE}={owner_cookie}"}
                results["remove"] = shared.delete(f"/api/settings/admins/{GRANTED_EMAIL}", headers=headers)

            signer = threading.Thread(target=sign_in)
            signer.start()
            self.assertTrue(about_to_create.wait(5), "the sign-in never reached SessionStore.create")
            remover = threading.Thread(target=removal)
            remover.start()
            remover.join(0.5)
            self.assertTrue(remover.is_alive(), "the removal waits while the sign-in holds the grant list")
            release.set()
            signer.join(10)
            remover.join(10)
        self.assertEqual(results["callback"].status_code, 302, results["callback"].text)
        self.assertEqual(results["remove"].status_code, 200, results["remove"].text)
        cookie = results["callback"].cookies[dashboard_auth.SESSION_COOKIE]
        # Deleted by the removal itself: the re-grant below purges stale rows too, and must not be what passes this.
        self.assertIsNone(self.sessions().get(cookie), "the removal deleted the paused sign-in's session")

        self.assertEqual(self.add(owner, owner_identity, {"email": GRANTED_EMAIL}).status_code, 200)
        late = TestClient(self.app, base_url=PUBLIC_URL, cookies={dashboard_auth.SESSION_COOKIE: cookie})
        self.addCleanup(late.close)
        self.assertEqual(late.get("/api/me").status_code, 401, "the re-grant does not revive the paused sign-in")

    def test_a_254_character_address_round_trips_byte_for_byte(self) -> None:
        # Never through sanitize_text: its long-token rule would redact this address,
        # and the listed value is what the Remove button sends back.
        email = "b" + "a" * 241 + "@example.com"
        self.assertEqual(len(email), 254)
        owner, owner_identity = self.start()
        added = self.add(owner, owner_identity, {"email": email})
        self.assertEqual(added.status_code, 200, added.text[:200])
        self.assertEqual((added.json()["added"], [row["email"] for row in added.json()["admins"]]), (email, [email]))
        [listed] = owner.get("/api/settings/admins").json()["admins"]
        self.assertEqual(listed["email"], email)
        removed = self.remove(owner, owner_identity, listed["email"])
        self.assertEqual((removed.status_code, removed.json()["admins"]), (200, []))

    def test_a_long_grant_list_still_announces_its_key_to_every_tab(self) -> None:
        from code_review_service.dashboard_store import DashboardStore

        self.store = DashboardStore(self.service_root / "data" / "dashboard" / "dashboard.sqlite3")
        owner, owner_identity = self.start()
        viewer, _ = self.client_for(VIEWER_USERINFO)
        for index in range(33):
            email = "%02d" % index + "a" * 230 + "@example.com"
            self.assertEqual(self.add(owner, owner_identity, {"email": email}).status_code, 200)
        tail = self.store.events_after(0, 500)[-1]["id"]
        self.assertEqual(self.add(owner, owner_identity, {"email": VIEWER_EMAIL}).status_code, 200)
        projected = [dashboard_web.project_event(row) for row in self.store.events_after(tail, 50)]
        self.assertEqual([(event["kind"], event["payload"].get("key")) for event in projected],
                         [("setting_updated", "admins")], "the viewer's tab learns its role changed")
        self.assertEqual(viewer.get("/api/me").json()["role"], "admin")

    def test_grants_round_trip_through_the_real_store(self) -> None:
        from code_review_service.dashboard_store import DashboardStore

        db_path = self.service_root / "data" / "dashboard" / "dashboard.sqlite3"
        self.store = DashboardStore(db_path)
        owner, owner_identity = self.start()
        viewer, _ = self.client_for(VIEWER_USERINFO)
        self.assertEqual(self.add(owner, owner_identity, {"email": GRANTED_EMAIL}).status_code, 200)
        self.assertEqual(self.add(owner, owner_identity, {"email": VIEWER_EMAIL}).status_code, 200)
        self.assertEqual([entry["email"] for entry in self.store.get_setting("admins")["value"]["added"]], [GRANTED_EMAIL, VIEWER_EMAIL])
        ops, ops_identity = self.client_for(GRANTED_USERINFO)
        self.assertEqual(ops_identity["role"], "admin")
        self.assertEqual(viewer.get("/api/me").json()["role"], "admin")

        # The store event names the addresses; the browser sees only which setting changed.
        self.assertEqual(self.store.events_after(0, 50)[-1]["payload"],
                         {"key": "admins", "updated_by": ADMIN_EMAIL, "emails": [GRANTED_EMAIL, VIEWER_EMAIL]})
        state = collect_sse(self.app, "/api/events?after=0", self.cookie(viewer), minimum_events=2)
        for block in state["data_blocks"]:
            self.assertEqual(json.loads(block.split("data: ", 1)[1])["payload"], {"key": "admins"})
            for email in (ADMIN_EMAIL, GRANTED_EMAIL, VIEWER_EMAIL):
                self.assertNotIn(email, block)

        self.assertEqual(self.remove(owner, owner_identity, GRANTED_EMAIL).status_code, 200)
        self.assertEqual(ops.get("/api/me").status_code, 401)
        self.assertEqual(self.remove(owner, owner_identity, VIEWER_EMAIL).status_code, 200)
        self.assertEqual(viewer.get("/api/me").json()["role"], "viewer")
        self.assertIsNone(self.store.get_setting("admins"))

        # A row that is not an object with an 'added' list grants nothing and refuses changes.
        conn = sqlite3.connect(str(db_path))
        try:
            with conn:
                conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('admins', ?, 't', 'x')",
                             (json.dumps({"added": GRANTED_EMAIL}),))
        finally:
            conn.close()
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(self.callback_status(GRANTED_USERINFO), 403)
        self.assertIn("dashboard admins unreadable, only the owner is admin: the saved list has no 'added' list",
                      stderr.getvalue())
        listed = owner.get("/api/settings/admins").json()
        self.assertEqual(listed["admins"], [])
        self.assertIn("cannot be read", listed["error"])
        for response in (self.add(owner, owner_identity, {"email": "new@example.com"}), self.remove(owner, owner_identity, GRANTED_EMAIL)):
            self.assertEqual((response.status_code, response.json()["error"]), (409, dashboard_web.UNREADABLE_ADMINS))


class RetryModelOverrideTest(AppTestCase):
    def test_retry_with_a_chosen_model_queues_the_policy_as_options(self) -> None:
        head = "a" * 40
        self.github.pr = open_pr(head)
        client = self.make_client()
        identity = self.login(client)
        run = self.failed_run(head)
        body = {"head_sha": head, "model_policy": {"model": "grok-4.6", "effort": "xhigh", "fallback_models": ["gpt-5.5"]}}
        response = client.post(f"/api/runs/{run['id']}/retry", content=json.dumps(body), headers=self.auth_headers(identity))
        self.assertEqual(response.status_code, 202, response.text)
        request = response.json()["request"]
        self.assertEqual(request["model_policy"], {"model": "grok-4.6", "effort": "xhigh", "fallback_models": ["gpt-5.5"]})
        call = self.store.calls_named("enqueue_retry")[-1]
        self.assertEqual(call[2]["options"], {"model_policy": {"model": "grok-4.6", "effort": "xhigh", "fallback_models": ["gpt-5.5"]}, "scope": "all"})
        listed = client.get("/api/retries").json()["requests"][0]
        self.assertEqual(listed["model_policy"]["model"], "grok-4.6")

    def test_retry_without_a_model_keeps_the_service_policy(self) -> None:
        head = "a" * 40
        self.github.pr = open_pr(head)
        client = self.make_client()
        identity = self.login(client)
        run = self.failed_run(head)
        response = client.post(f"/api/runs/{run['id']}/retry", content=json.dumps({"head_sha": head}), headers=self.auth_headers(identity))
        self.assertEqual(response.status_code, 202, response.text)
        self.assertIsNone(response.json()["request"]["model_policy"])
        self.assertEqual(self.store.calls_named("enqueue_retry")[-1][2]["options"], {"scope": "all"}, "no model_policy key")

    def test_retry_with_a_claude_model_is_refused_before_anything_is_queued(self) -> None:
        head = "a" * 40
        self.github.pr = open_pr(head)
        client = self.make_client()
        identity = self.login(client)
        run = self.failed_run(head)
        body = {"head_sha": head, "model_policy": {"model": "claude-opus-5[1m]"}}
        response = client.post(f"/api/runs/{run['id']}/retry", content=json.dumps(body), headers=self.auth_headers(identity))
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("Claude models are not available", response.json()["error"])
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])
        self.assertEqual(self.github.calls, [], "the PR must not even be revalidated for an invalid policy")


class RetryScopeTest(AppTestCase):
    """A retry re-runs only the failed agents when the attempt kept the others' results."""

    def _retry(self, run: dict[str, Any], **body: Any):
        head = run["head_sha"]
        self.github.pr = open_pr(head)
        client = self.make_client()
        identity = self.login(client)
        return client.post(f"/api/runs/{run['id']}/retry", content=json.dumps({"head_sha": head, **body}),
                           headers=self.auth_headers(identity))

    def test_the_default_is_failed_agents_when_results_were_kept_and_all_otherwise(self) -> None:
        kept = self.failed_run(reusable_agents=["correctness", "security"])
        response = self._retry(kept)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["request"]["scope"], "failed")
        self.assertEqual(self.store.calls_named("enqueue_retry")[-1][2]["options"], {"scope": "failed"})

        nothing = self.failed_run(head_sha="b" * 40)
        response = self._retry(nothing)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["request"]["scope"], "all")
        self.assertEqual(self.store.calls_named("enqueue_retry")[-1][2]["options"], {"scope": "all"})

    def test_an_explicit_scope_is_honoured_next_to_the_model(self) -> None:
        kept = self.failed_run(reusable_agents=["correctness"])
        response = self._retry(kept, scope="all", model_policy={"model": "grok-4.6", "effort": "", "fallback_models": []})
        self.assertEqual(response.status_code, 202, response.text)
        options = self.store.calls_named("enqueue_retry")[-1][2]["options"]
        self.assertEqual(options, {"scope": "all", "model_policy": {"model": "grok-4.6", "effort": "", "fallback_models": []}})

    def test_an_invalid_scope_is_refused_before_anything_is_queued(self) -> None:
        kept = self.failed_run(reusable_agents=["correctness"])
        for bad in ("some", "", "FAILED", 1, True, ["failed"], {"scope": "all"}):
            with self.subTest(scope=bad):
                response = self._retry(kept, scope=bad)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"], "scope must be one of: failed, all")
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_failed_agents_only_is_refused_when_the_attempt_kept_nothing(self) -> None:
        response = self._retry(self.failed_run(), scope="failed")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("kept no agent results", response.json()["error"])
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_runs_and_events_carry_agent_ids_only(self) -> None:
        client = self.make_client()
        self.login(client)
        run = self.store.force_run(
            status="failed",
            reusable_agents=["correctness", "Bad Id", 7, "/Users/operator/x", "security"],
            # A store row never carries this, and the API must not forward it if one did.
            agent_results=[{"agent": "correctness", "findings": [{"body": "secret finding text"}]}],
            agents=[{"id": "correctness", "status": "done", "summary": "ok", "reused": True},
                    {"id": "security", "status": "failed", "summary": "timed out", "reused": "yes"}],
        )
        payload = client.get(f"/api/runs/{run['id']}").json()["run"]
        self.assertEqual(payload["reusable_agents"], ["correctness", "security"])
        self.assertNotIn("agent_results", payload)
        self.assertEqual(payload["agents"][0]["reused"], True)
        self.assertNotIn("reused", payload["agents"][1], "only a literal true marks a reused agent")
        listed = client.get("/api/runs").json()["runs"][0]
        self.assertEqual(listed["reusable_agents"], ["correctness", "security"])
        self.assertNotIn("secret finding text", json.dumps(payload) + json.dumps(listed))

        self.store.force_event("run_updated", run["id"], {
            "status": "failed", "reusable_agents": ["correctness", "../etc"], "agent_results": [{"agent": "x"}],
        })
        self.store.force_event("retry_enqueued", None, {"request_id": "req-1", "scope": "failed"})
        self.store.force_event("retry_enqueued", None, {"request_id": "req-2", "scope": "everything"})
        events = [dashboard_web.project_event(event) for event in self.store.events_after(0)[-3:]]
        self.assertEqual(events[0]["payload"], {"status": "failed", "reusable_agents": ["correctness"]})
        self.assertEqual(events[1]["payload"]["scope"], "failed")
        self.assertIsNone(events[2]["payload"]["scope"])

    def test_the_retry_listing_shows_the_scope(self) -> None:
        client = self.make_client()
        self.login(client)
        self.store.enqueue_retry("owner/repo", 7, "a" * 40, ADMIN_EMAIL, options={"scope": "failed"})
        self.store.enqueue_retry("owner/repo", 8, "b" * 40, ADMIN_EMAIL)
        rows = {row["pr"]: row for row in client.get("/api/retries").json()["requests"]}
        self.assertEqual((rows[7]["scope"], rows[8]["scope"]), ("failed", None))


class RetriggerApiTest(AppTestCase):
    """The Retrigger badge's page: any session reads it, an admin queues a rereview."""

    HEAD = "a" * 40
    PATH = "/api/retrigger/owner/repo/7"

    def viewer_login(self, client: TestClient) -> dict[str, Any]:
        self.oauth.token = {"access_token": "at", "userinfo": dict(VIEWER_USERINFO)}
        return self.login(client)

    def succeeded_run(self, head_sha: str = HEAD, **fields: Any) -> dict[str, Any]:
        run = self.store.force_run(head_sha=head_sha, **fields)
        self.store.update_run(run["id"], "succeeded", stage="posted", review_url="https://github.invalid/r/1")
        return self.store.get_run(run["id"])  # type: ignore[return-value]

    def post(self, client: TestClient, identity: dict[str, Any], body: Any = None, **headers: str) -> Any:
        if body is None:
            # The page sends the attempt it displayed: by default the store's latest.
            latest = self.store.latest_attempt_for_head("owner/repo", 7, self.HEAD) if callable(
                getattr(self.store, "latest_attempt_for_head", None)
            ) else None
            payload = {"repo": "owner/repo", "pr": 7, "head_sha": self.HEAD, "latest_run_id": latest["id"] if latest else None}
        else:
            payload = body
        if isinstance(payload, dict) and "repo" in payload and "observed_at" not in payload:
            # The page's GET snapshot time; by default the page was read just now.
            payload = {**payload, "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        return client.post("/api/retrigger", content=json.dumps(payload), headers=self.auth_headers(identity, **headers))

    # -- GET -------------------------------------------------------------
    def test_get_for_a_viewer_reports_the_head_and_the_latest_attempt_read_only(self) -> None:
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        self.viewer_login(client)
        older = self.succeeded_run()
        latest = self.succeeded_run()
        self.succeeded_run("b" * 40)  # another head of the same PR is not "the latest for this head"

        response = client.get(self.PATH)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["repo"], body["pr"], body["title"]), ("owner/repo", 7, "Add dashboard"))
        self.assertEqual((body["state"], body["draft"], body["head_sha"]), ("open", False, self.HEAD))
        self.assertEqual(body["latest_run"]["id"], latest["id"])
        self.assertNotEqual(body["latest_run"]["id"], older["id"])
        self.assertEqual(body["latest_run"]["status"], "succeeded")
        self.assertEqual(body["busy"], False)
        self.assertIsNone(body["active_request"])
        self.assertIsNone(body["running_run_id"])
        self.assertEqual(body["can_retrigger"], False)
        self.assertEqual(self.github.calls, [7])

    def test_get_reads_github_once_per_pr_within_the_cache_window(self) -> None:
        # The page re-reads on every stream event; each read used to be a
        # credentialed GitHub call (gh auth token + GET pulls/N) for any viewer.
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        self.viewer_login(client)
        for _ in range(3):
            self.assertEqual(client.get(self.PATH).status_code, 200)
        self.assertEqual(self.github.calls, [7])
        with mock.patch.object(dashboard_web, "RETRIGGER_PR_CACHE_SECONDS", 0.0):
            self.assertEqual(client.get(self.PATH).status_code, 200)
        self.assertEqual(self.github.calls, [7, 7])

    def test_get_retries_a_failed_read_once_the_window_has_passed(self) -> None:
        self.github.error = "boom"
        client = self.make_client()
        self.viewer_login(client)
        self.assertEqual(client.get(self.PATH).status_code, 502)
        self.github.error = None
        self.github.pr = open_pr(self.HEAD)
        self.assertEqual(client.get(self.PATH).status_code, 502, "the failure is remembered within the window")
        with mock.patch.object(dashboard_web, "RETRIGGER_PR_CACHE_SECONDS", 0.0):
            self.assertEqual(client.get(self.PATH).json()["head_sha"], self.HEAD)
        self.assertEqual(self.github.calls, [7, 7])

    def test_a_read_slower_than_the_cache_window_is_still_shared(self) -> None:
        # astra-zhuge (gate round 4) and the bot on c445e2d: freshness was
        # measured from the read's start, so a read that took longer than the
        # window was stale when stored and every waiter read GitHub again.
        # Also the cold-cache case: tabs refresh on the same stream event, and
        # must not each make a credentialed call.
        import threading

        class SlowGitHub(FakeGitHubClient):
            def get_pr(self, number: int) -> dict[str, Any]:
                time.sleep(0.4)
                return FakeGitHubClient.get_pr(self, number)

        self.github = SlowGitHub(open_pr(self.HEAD))
        client = self.make_client()
        self.viewer_login(client)
        codes: list = []
        with mock.patch.object(dashboard_web, "RETRIGGER_PR_CACHE_SECONDS", 0.3):
            threads = [threading.Thread(target=lambda: codes.append(client.get(self.PATH).status_code)) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        self.assertEqual(codes, [200] * 4)
        self.assertEqual(self.github.calls, [7], "the waiters shared the slow read")

    def test_concurrent_gets_share_one_failed_read(self) -> None:
        # astra-zhuge (gate round 3): waiters behind a failed in-flight read
        # must share its outcome, not retry GitHub one by one under the lock.
        import threading

        class SlowFailingGitHub(FakeGitHubClient):
            def get_pr(self, number: int) -> dict[str, Any]:
                time.sleep(0.3)
                return FakeGitHubClient.get_pr(self, number)

        self.github = SlowFailingGitHub(error="boom")
        client = self.make_client()
        self.viewer_login(client)
        codes: list = []
        threads = [threading.Thread(target=lambda: codes.append(client.get(self.PATH).status_code)) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(codes, [502] * 4)
        self.assertEqual(self.github.calls, [7])

    def test_post_reads_github_live_and_refreshes_what_get_shows(self) -> None:
        # A cached head must never decide a POST, and the live read a POST
        # makes is what the page shows after its "head moved" 409.
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        identity = self.login(client)
        self.assertEqual(client.get(self.PATH).json()["head_sha"], self.HEAD)
        self.github.pr = open_pr("b" * 40)
        response = self.post(client, identity)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.github.calls, [7, 7])
        self.assertEqual(client.get(self.PATH).json()["head_sha"], "b" * 40)
        self.assertEqual(self.github.calls, [7, 7])

    def test_get_for_an_admin_can_retrigger_and_sees_a_queued_request(self) -> None:
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        self.login(client)
        queued = self.store.enqueue_retry("owner/repo", 7, self.HEAD, ADMIN_EMAIL, options={"kind": "rereview", "scope": "all"})

        body = client.get(self.PATH).json()
        self.assertEqual(body["can_retrigger"], True)
        self.assertEqual(body["busy"], True)
        self.assertEqual(body["active_request"]["id"], queued["id"])
        self.assertEqual(body["active_request"]["kind"], "rereview")
        self.assertIsNone(body["latest_run"])

    def test_get_reports_a_running_attempt_of_the_pr_as_busy(self) -> None:
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        self.login(client)
        running = self.store.force_run(head_sha="c" * 40)  # another head, still this PR

        body = client.get(self.PATH).json()
        self.assertEqual(body["busy"], True)
        self.assertEqual(body["running_run_id"], running["id"])
        self.assertIsNone(body["active_request"])

    def test_get_refuses_an_unconfigured_repository_before_github(self) -> None:
        client = self.make_client()
        self.login(client)
        response = client.get("/api/retrigger/other/repo/7")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.github.calls, [])

    def test_get_rejects_a_bad_pull_request_number(self) -> None:
        client = self.make_client()
        self.login(client)
        for path in ("/api/retrigger/owner/repo/0", "/api/retrigger/owner/repo/abc", "/api/retrigger/owner/repo/-1"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 400)
        self.assertEqual(self.github.calls, [])

    def test_get_upstream_failure_is_a_sanitized_502(self) -> None:
        self.github.error = "GitHub API GET failed: 401 Bad credentials token=ghp_abcdefghijklmnopqrstuvwxyz0123456789"
        client = self.make_client()
        self.login(client)
        response = client.get(self.PATH)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("ghp_", response.text)
        self.assertNotIn("Bad credentials", response.text)

    def test_get_requires_a_session(self) -> None:
        client = self.make_client()
        self.assertEqual(client.get(self.PATH).status_code, 401)

    def test_get_and_post_refuse_503_on_a_store_missing_the_retrigger_capability(self) -> None:
        # Round 2 MUST-FIX: no fallback to the old bounded, repo-wide scans.
        self.github.pr = open_pr(self.HEAD)
        self.store = StoreWithoutRetriggerCapability()
        client = self.make_client()
        identity = self.login(client)
        get_response = client.get(self.PATH)
        self.assertEqual(get_response.status_code, 503, get_response.text)
        self.assertEqual(get_response.json()["error"], "retrigger is unavailable on this store")
        self.assertEqual(self.github.calls, [], "refused before any GitHub call")
        post_response = self.post(client, identity)
        self.assertEqual(post_response.status_code, 503, post_response.text)
        self.assertEqual(post_response.json()["error"], "retrigger is unavailable on this store")
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_a_store_missing_either_pr_scoped_query_is_503_before_github(self) -> None:
        # Both methods are required; a store with only one must not reach a
        # TypeError 500 after a GitHub call.
        for missing in ("active_activity_for_pr", "latest_attempt_for_head"):
            with self.subTest(missing=missing):
                self.github = FakeGitHubClient(open_pr(self.HEAD))
                self.store = type("PartialStore", (FakeDashboardStore,), {missing: None})()
                client = self.make_client()
                identity = self.login(client)
                self.assertEqual(client.get(self.PATH).status_code, 503)
                self.assertEqual(self.post(client, identity).status_code, 503)
                self.assertEqual(self.github.calls, [])

    def test_concurrent_gets_share_one_github_read(self) -> None:
        # Bot (red-team) on 13ff95b: tabs refresh on the same stream event; a
        # cold cache must not launch one credentialed GitHub call per viewer.
        import threading

        github = self.github

        class SlowGitHub(FakeGitHubClient):
            def get_pr(self, number: int) -> dict[str, Any]:
                time.sleep(0.3)
                return FakeGitHubClient.get_pr(self, number)

        self.github = SlowGitHub(open_pr(self.HEAD))
        client = self.make_client()
        self.viewer_login(client)
        codes: list = []
        threads = [threading.Thread(target=lambda: codes.append(client.get(self.PATH).status_code)) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(codes, [200] * 4)
        self.assertEqual(self.github.calls, [7], "one GitHub read for concurrent cold GETs")
        del github

    def test_a_slow_get_never_overwrites_the_head_a_later_post_read(self) -> None:
        # Bot (tests) on 446d5d1: a GET that started first but finished last
        # used to cache its older head over the one the POST had just read.
        import threading

        entered, gate = threading.Event(), threading.Event()

        class GatedGitHub(FakeGitHubClient):
            def get_pr(self, number: int) -> dict[str, Any]:
                self.calls.append(number)
                if len(self.calls) == 1:
                    entered.set()
                    gate.wait(5)
                    return open_pr("a" * 40)
                return open_pr("b" * 40)

        self.github = GatedGitHub()
        client = self.make_client()
        identity = self.login(client)
        seen: list = []
        slow = threading.Thread(target=lambda: seen.append(client.get(self.PATH).json()["head_sha"]))
        slow.start()
        self.assertTrue(entered.wait(5))
        moved = self.post(client, identity, {"repo": "owner/repo", "pr": 7, "head_sha": "a" * 40, "latest_run_id": None})
        self.assertEqual(moved.status_code, 409, moved.text)
        gate.set()
        slow.join(5)
        self.assertEqual(seen, ["b" * 40], "the slow GET answers with the newer read that landed meanwhile")
        self.assertEqual(client.get(self.PATH).json()["head_sha"], "b" * 40)
        self.assertEqual(self.github.calls, [7, 7])

    def test_a_slow_get_cannot_restore_an_older_head_after_the_newer_entry_expired(self) -> None:
        # Bot (correctness, red-team) on fa4ee50: pruning the POST's expired
        # entry also dropped the ordering it carried, so a GET that started
        # before the POST and finished after the entry expired cached its
        # older head as fresh.
        import threading

        entered, gate = threading.Event(), threading.Event()

        class GatedGitHub(FakeGitHubClient):
            def get_pr(self, number: int) -> dict[str, Any]:
                self.calls.append(number)
                if len(self.calls) == 1:
                    entered.set()
                    gate.wait(5)
                    return open_pr("a" * 40)
                return open_pr("b" * 40)

        self.github = GatedGitHub()
        client = self.make_client()
        identity = self.login(client)
        seen: list = []
        with mock.patch.object(dashboard_web, "RETRIGGER_PR_CACHE_SECONDS", 0.3):
            slow = threading.Thread(target=lambda: seen.append(client.get(self.PATH).json()["head_sha"]))
            slow.start()
            self.assertTrue(entered.wait(5))
            moved = self.post(client, identity, {"repo": "owner/repo", "pr": 7, "head_sha": "a" * 40, "latest_run_id": None})
            self.assertEqual(moved.status_code, 409, moved.text)
            time.sleep(0.5)  # the POST's entry expires while the slow GET is still out
            gate.set()
            slow.join(5)
            # Bot (maintainability) on 72f2df7: the superseded payload is not
            # returned either; the owner reads again and answers with B.
            self.assertEqual(seen, ["b" * 40])
            self.assertEqual(client.get(self.PATH).json()["head_sha"], "b" * 40)
        self.assertEqual(self.github.calls, [7, 7, 7], "one re-read, then the cache answers")

    def test_a_get_superseded_twice_answers_503_instead_of_reading_forever(self) -> None:
        # astra-zhuge (gate round 8): each re-read could be superseded again by
        # a later POST read whose entry expired before it finished; the loop had
        # no per-request bound. One re-read, then a retryable 503, never the
        # superseded payload.
        import threading

        entered = {1: threading.Event(), 3: threading.Event()}
        gates = {1: threading.Event(), 3: threading.Event()}

        class GatedGitHub(FakeGitHubClient):
            def get_pr(self, number: int) -> dict[str, Any]:
                self.calls.append(number)
                index = len(self.calls)
                if index in gates:
                    entered[index].set()
                    gates[index].wait(5)
                    return open_pr("a" * 40)
                return open_pr("b" * 40)

        self.github = GatedGitHub()
        client = self.make_client()
        identity = self.login(client)
        answers: list = []
        stale_post = {"repo": "owner/repo", "pr": 7, "head_sha": "a" * 40, "latest_run_id": None}
        with mock.patch.object(dashboard_web, "RETRIGGER_PR_CACHE_SECONDS", 0.3):
            slow = threading.Thread(target=lambda: answers.append(client.get(self.PATH)))
            slow.start()
            for index in (1, 3):
                self.assertTrue(entered[index].wait(5), index)
                self.assertEqual(self.post(client, identity, stale_post).status_code, 409)
                time.sleep(0.5)  # the POST's entry expires before the slow read returns
                gates[index].set()
            slow.join(5)
        self.assertEqual(len(self.github.calls), 4)
        self.assertEqual(answers[0].status_code, 503, answers[0].text)
        self.assertNotIn("a" * 40, answers[0].text)

    def test_a_failed_get_superseded_after_the_newer_entry_expired_reads_again(self) -> None:
        # Bot (maintainability) on 4192e67: the failure was correctly not
        # remembered, but the GET still answered 502 instead of reading again.
        import threading

        entered, gate = threading.Event(), threading.Event()

        class GatedFailingGitHub(FakeGitHubClient):
            def get_pr(self, number: int) -> dict[str, Any]:
                self.calls.append(number)
                if len(self.calls) == 1:
                    entered.set()
                    gate.wait(5)
                    raise svc.ServiceError("timed out")
                return open_pr("b" * 40)

        self.github = GatedFailingGitHub()
        client = self.make_client()
        identity = self.login(client)
        answers: list = []
        with mock.patch.object(dashboard_web, "RETRIGGER_PR_CACHE_SECONDS", 0.3):
            slow = threading.Thread(target=lambda: answers.append(client.get(self.PATH)))
            slow.start()
            self.assertTrue(entered.wait(5))
            stale_post = {"repo": "owner/repo", "pr": 7, "head_sha": "a" * 40, "latest_run_id": None}
            self.assertEqual(self.post(client, identity, stale_post).status_code, 409)
            time.sleep(0.5)  # the POST's entry expires before the slow read fails
            gate.set()
            slow.join(5)
        self.assertEqual(answers[0].status_code, 200, answers[0].text)
        self.assertEqual(answers[0].json()["head_sha"], "b" * 40)
        self.assertEqual(self.github.calls, [7, 7, 7])

    def test_a_read_started_late_by_a_busy_threadpool_keeps_its_place_in_order(self) -> None:
        # Bot (red-team) on 4192e67: the start was taken before waiting for a
        # worker, so a POST read delayed past RETRIGGER_PR_ORDER_SECONDS lost its
        # watermark at once and an older GET read replaced its newer head.
        import threading

        entered, gate = threading.Event(), threading.Event()

        class GatedGitHub(FakeGitHubClient):
            def get_pr(self, number: int) -> dict[str, Any]:
                self.calls.append(number)
                if len(self.calls) == 1:
                    entered.set()
                    gate.wait(5)
                    return open_pr("a" * 40)
                return open_pr("b" * 40)

        real_run_in_threadpool = dashboard_web.run_in_threadpool
        delayed = {"first": True}

        async def busy_pool(fn, *args, **kwargs):
            # The second offload (the POST's read) waits for a worker for a while.
            if delayed["first"]:
                delayed["first"] = False
            else:
                await dashboard_web.anyio.sleep(0.4)
            return await real_run_in_threadpool(fn, *args, **kwargs)

        self.github = GatedGitHub()
        client = self.make_client()
        identity = self.login(client)
        seen: list = []
        with mock.patch.object(dashboard_web, "RETRIGGER_PR_ORDER_SECONDS", 0.3), mock.patch.object(
            dashboard_web, "RETRIGGER_PR_CACHE_SECONDS", 5.0
        ), mock.patch.object(dashboard_web, "run_in_threadpool", busy_pool):
            slow = threading.Thread(target=lambda: seen.append(client.get(self.PATH).json()["head_sha"]))
            slow.start()
            self.assertTrue(entered.wait(5))
            stale_post = {"repo": "owner/repo", "pr": 7, "head_sha": "a" * 40, "latest_run_id": None}
            self.assertEqual(self.post(client, identity, stale_post).status_code, 409)
            gate.set()
            slow.join(5)
            self.assertEqual(seen, ["b" * 40], "the older GET read did not replace the POST's newer head")

    def test_a_slow_get_failing_after_a_later_post_read_does_not_mask_it(self) -> None:
        # fable-zhuge (gate round 4): a GET that started first and failed last
        # recorded a failure that outlived the POST's newer successful read.
        import threading

        entered, gate = threading.Event(), threading.Event()

        class GatedFailingGitHub(FakeGitHubClient):
            def get_pr(self, number: int) -> dict[str, Any]:
                self.calls.append(number)
                if len(self.calls) == 1:
                    entered.set()
                    gate.wait(5)
                    raise svc.ServiceError("timed out")
                return open_pr("b" * 40)

        self.github = GatedFailingGitHub()
        client = self.make_client()
        identity = self.login(client)
        codes: list = []
        with mock.patch.object(dashboard_web, "RETRIGGER_PR_CACHE_SECONDS", 0.6):
            slow = threading.Thread(target=lambda: codes.append(client.get(self.PATH).status_code))
            slow.start()
            self.assertTrue(entered.wait(5))
            moved = self.post(client, identity, {"repo": "owner/repo", "pr": 7, "head_sha": "a" * 40, "latest_run_id": None})
            self.assertEqual(moved.status_code, 409, moved.text)
            time.sleep(0.4)  # the slow read fails well after the POST's read landed
            gate.set()
            slow.join(5)
            # Bot (correctness) on 72f2df7: the failed owner answers with the
            # newer read the POST cached instead of a 502.
            self.assertEqual(codes, [200])
            time.sleep(0.3)  # the POST's entry has expired; a stale failure would still be "fresh"
            self.assertEqual(client.get(self.PATH).json()["head_sha"], "b" * 40)
        self.assertEqual(self.github.calls, [7, 7, 7])

    def test_a_failed_read_is_not_retried_within_the_window(self) -> None:
        # Bot (security) on 446d5d1: a nonexistent PR number used to cost one
        # credentialed GitHub call per GET.
        self.github.error = "Not Found"
        client = self.make_client()
        self.viewer_login(client)
        for _ in range(3):
            self.assertEqual(client.get(self.PATH).status_code, 502)
        self.assertEqual(self.github.calls, [7])

    def test_the_per_pr_read_locks_do_not_outlive_their_entries(self) -> None:
        # Bot (security) on deec39d: a lock per requested PR number used to stay
        # forever, so sweeping nonexistent PR numbers grew the table unbounded.
        self.github.error = "Not Found"
        client = self.make_client()
        self.viewer_login(client)
        for number in range(1, 6):
            self.assertEqual(client.get(f"/api/retrigger/owner/repo/{number}").status_code, 502)
        locks = _closure_value(self.app, "/api/retrigger/{owner}/{name}/{pr}", "retrigger_pr_locks")
        with mock.patch.object(dashboard_web, "RETRIGGER_PR_CACHE_SECONDS", 0.0):
            self.assertEqual(client.get("/api/retrigger/owner/repo/6").status_code, 502)
        self.assertLessEqual(len(locks), 1, sorted(locks))

    def test_retrigger_github_reads_run_in_a_few_workers_at_most(self) -> None:
        # Bot (maintainability) on 71c6cbb: the per-minute budget does not bound
        # how many slow reads of DIFFERENT PRs hold thread-pool workers at once.
        import threading

        active = {"now": 0, "max": 0}
        guard = threading.Lock()

        class SlowGitHub(FakeGitHubClient):
            def get_pr(self, number: int) -> dict[str, Any]:
                with guard:
                    active["now"] += 1
                    active["max"] = max(active["max"], active["now"])
                time.sleep(0.3)
                with guard:
                    active["now"] -= 1
                return FakeGitHubClient.get_pr(self, number)

        self.github = SlowGitHub(open_pr(self.HEAD))
        with mock.patch.object(dashboard_web, "RETRIGGER_PR_READ_SLOTS", 1):
            client = self.make_client()
            self.viewer_login(client)
            codes: list = []
            threads = [
                threading.Thread(target=lambda n=n: codes.append(client.get(f"/api/retrigger/owner/repo/{n}").status_code))
                for n in (7, 8, 9)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        self.assertEqual(sorted(codes), [200, 200, 200])
        self.assertEqual(active["max"], 1, "one GitHub read at a time with one slot")
        self.assertEqual(sorted(self.github.calls), [7, 8, 9])

    def test_the_budget_is_charged_when_a_queued_read_starts(self) -> None:
        # Bot (correctness, reliability, red-team, maintainability, tests) on
        # 348a0f6: the budget was charged before waiting for a read slot, so
        # queued reads outlived their charge and burst past the cap later.
        import threading

        entered, gate = threading.Event(), threading.Event()

        class StallingGitHub(FakeGitHubClient):
            def get_pr(self, number: int) -> dict[str, Any]:
                self.calls.append(number)
                if len(self.calls) == 1:
                    entered.set()
                    gate.wait(5)
                return open_pr(self.pr_head)

        self.github = StallingGitHub()
        self.github.pr_head = self.HEAD
        codes: dict = {}
        with mock.patch.object(dashboard_web, "RETRIGGER_PR_READ_SLOTS", 1), mock.patch.object(
            dashboard_web, "RETRIGGER_GITHUB_READS_PER_MINUTE", 2
        ), mock.patch.object(dashboard_web, "RETRIGGER_GITHUB_BUDGET_WINDOW_SECONDS", 0.5):
            client = self.make_client()
            self.viewer_login(client)

            def get(number: int) -> None:
                codes[number] = client.get(f"/api/retrigger/owner/repo/{number}").status_code

            threads = {n: threading.Thread(target=get, args=(n,)) for n in (7, 8, 9, 10)}
            threads[7].start()
            self.assertTrue(entered.wait(5))  # the only slot is stalled
            threads[8].start()
            time.sleep(0.6)  # charges taken at admission would have expired by now
            threads[9].start()
            threads[10].start()
            time.sleep(0.2)
            gate.set()
            for thread in threads.values():
                thread.join(10)
        self.assertEqual(sorted(codes.values()), [200, 200, 200, 429], codes)
        self.assertEqual(len(self.github.calls), 3, "at most the budget starts within one window")

    def test_the_budget_is_charged_when_the_worker_starts_the_read(self) -> None:
        # astra-zhuge (gate round 11): charged with the slot held but before the
        # shared thread pool ran the read, a read delayed there outlived its
        # charge; it now charges in the worker, right before the GitHub call.
        import threading

        real_run_in_threadpool = dashboard_web.run_in_threadpool
        delayed = {"left": 2}

        async def busy_pool(fn, *args, **kwargs):
            if getattr(fn, "__name__", "") == "_budgeted_read" and delayed["left"] > 0:
                delayed["left"] -= 1
                await dashboard_web.anyio.sleep(0.7)  # no worker free for a while
            return await real_run_in_threadpool(fn, *args, **kwargs)

        self.github.pr = open_pr(self.HEAD)
        codes: dict = {}
        with mock.patch.object(dashboard_web, "RETRIGGER_GITHUB_READS_PER_MINUTE", 2), mock.patch.object(
            dashboard_web, "RETRIGGER_GITHUB_BUDGET_WINDOW_SECONDS", 0.5
        ), mock.patch.object(dashboard_web, "run_in_threadpool", busy_pool):
            client = self.make_client()
            self.viewer_login(client)

            def get(number: int) -> None:
                codes[number] = client.get(f"/api/retrigger/owner/repo/{number}").status_code

            first = [threading.Thread(target=get, args=(n,)) for n in (7, 8)]
            for thread in first:
                thread.start()
            time.sleep(0.55)  # charges taken before the pool ran these would have expired
            later = [threading.Thread(target=get, args=(n,)) for n in (9, 10)]
            for thread in later:
                thread.start()
            for thread in first + later:
                thread.join(10)
        self.assertEqual(sorted(codes.values()), [200, 200, 429, 429], codes)
        self.assertEqual(len(self.github.calls), 2, "at most the budget starts within one window")

    def test_viewer_reads_across_prs_have_a_github_budget(self) -> None:
        # Varying the PR number bypasses the per-PR cache; a budget bounds it.
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        self.viewer_login(client)
        with mock.patch.object(dashboard_web, "RETRIGGER_GITHUB_READS_PER_MINUTE", 2):
            self.assertEqual(client.get("/api/retrigger/owner/repo/7").status_code, 200)
            self.assertEqual(client.get("/api/retrigger/owner/repo/8").status_code, 200)
            over = client.get("/api/retrigger/owner/repo/9")
            self.assertEqual(over.status_code, 429, over.text)
            self.assertEqual(client.get("/api/retrigger/owner/repo/7").status_code, 200, "a cached PR still answers")
        self.assertEqual(self.github.calls, [7, 8])

    # -- POST ------------------------------------------------------------
    def test_post_refuses_when_a_newer_attempt_than_the_displayed_one_exists(self) -> None:
        # Bot (correctness/red-team/reliability) on 13ff95b: retry_of must be the
        # attempt the page showed, not one that finished during admission.
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        identity = self.login(client)
        shown = self.succeeded_run()
        self.succeeded_run()  # a pool review of the head finished after the page was shown
        for displayed in (shown["id"], None):
            with self.subTest(displayed=displayed):
                body = {"repo": "owner/repo", "pr": 7, "head_sha": self.HEAD, "latest_run_id": displayed}
                response = self.post(client, identity, body)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertIn("reload", response.json()["error"])
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_post_requires_the_displayed_attempt(self) -> None:
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        identity = self.login(client)
        for bad in ({}, {"latest_run_id": 7}, {"latest_run_id": ""}):
            with self.subTest(bad=bad):
                body = {"repo": "owner/repo", "pr": 7, "head_sha": self.HEAD, **bad}
                self.assertEqual(self.post(client, identity, body).status_code, 400)

    def test_get_reports_when_it_read_the_snapshot(self) -> None:
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        identity = self.login(client)
        before = dt.datetime.now(dt.timezone.utc)
        response = client.get("/api/retrigger/owner/repo/7", headers=self.auth_headers(identity))
        after = dt.datetime.now(dt.timezone.utc)
        self.assertEqual(response.status_code, 200, response.text)
        observed = dt.datetime.fromisoformat(response.json()["observed_at"])
        self.assertIsNotNone(observed.tzinfo)
        self.assertTrue(before <= observed <= after, (before, observed, after))

    def test_post_queues_the_page_snapshot_time_as_the_late_check_cutoff(self) -> None:
        """Bot P2 on 64059fb (red-team, reliability): the cutoff is when the page read
        its snapshot, not when the request was queued after the live GitHub read."""
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        identity = self.login(client)
        shown = "2026-10-15T17:00:00+00:00"
        body = {"repo": "owner/repo", "pr": 7, "head_sha": self.HEAD, "latest_run_id": None, "observed_at": shown}
        response = self.post(client, identity, body)
        self.assertEqual(response.status_code, 202, response.text)
        options = self.store.calls_named("enqueue_retry")[0][2]["options"]
        self.assertEqual(options[dx.RETRY_SEEN_AT_OPTION], "2026-10-15T17:00:00.000000+00:00")

    def test_post_clamps_a_future_snapshot_time_to_now(self) -> None:
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        identity = self.login(client)
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat(timespec="seconds")
        body = {"repo": "owner/repo", "pr": 7, "head_sha": self.HEAD, "latest_run_id": None, "observed_at": future}
        before = dt.datetime.now(dt.timezone.utc)
        response = self.post(client, identity, body)
        after = dt.datetime.now(dt.timezone.utc)
        self.assertEqual(response.status_code, 202, response.text)
        cutoff = dt.datetime.fromisoformat(self.store.calls_named("enqueue_retry")[0][2]["options"][dx.RETRY_SEEN_AT_OPTION])
        self.assertTrue(before <= cutoff <= after, (before, cutoff, after))

    def test_post_keeps_the_snapshot_time_to_the_microsecond_and_reads_a_z_offset(self) -> None:
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        identity = self.login(client)
        base = {"repo": "owner/repo", "pr": 7, "head_sha": self.HEAD, "latest_run_id": None}
        response = self.post(client, identity, {**base, "observed_at": "2026-10-15T17:00:00.123456Z"})
        self.assertEqual(response.status_code, 202, response.text)
        options = self.store.calls_named("enqueue_retry")[0][2]["options"]
        self.assertEqual(options[dx.RETRY_SEEN_AT_OPTION], "2026-10-15T17:00:00.123456+00:00")

    def test_post_requires_the_snapshot_time(self) -> None:
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        identity = self.login(client)
        base = {"repo": "owner/repo", "pr": 7, "head_sha": self.HEAD, "latest_run_id": None}
        for bad in ({}, {"observed_at": None}, {"observed_at": ""}, {"observed_at": "yesterday"}, {"observed_at": "2026-10-15T17:00:00"}):
            with self.subTest(bad=bad):
                response = client.post(
                    "/api/retrigger", content=json.dumps({**base, **bad}), headers=self.auth_headers(identity)
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("observed_at", response.json()["error"])
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_post_as_a_viewer_is_403(self) -> None:
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        identity = self.viewer_login(client)
        response = self.post(client, identity)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"], "admin role required")
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_post_without_guards_is_refused(self) -> None:
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        identity = self.login(client)
        payload = json.dumps({"repo": "owner/repo", "pr": 7, "head_sha": self.HEAD})

        no_csrf = client.post("/api/retrigger", content=payload, headers={"Origin": PUBLIC_URL, "Content-Type": "application/json"})
        wrong_csrf = self.post(client, identity, **{"X-CSRF-Token": "wrong-token"})
        foreign = self.post(client, identity, Origin="https://evil.example")
        headers = self.auth_headers(identity)
        headers.pop("Origin")
        missing_origin = client.post("/api/retrigger", content=payload, headers=headers)
        wrong_type = self.post(client, identity, **{"Content-Type": "text/plain"})
        broken = client.post("/api/retrigger", content="{not json", headers=self.auth_headers(identity))
        self.assertEqual(
            [r.status_code for r in (no_csrf, wrong_csrf, foreign, missing_origin, wrong_type, broken)],
            [403, 403, 403, 403, 400, 400],
        )
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_post_validates_the_body(self) -> None:
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        identity = self.login(client)
        for body in (
            {},
            {"repo": "owner/repo", "pr": 7},
            {"repo": "owner/repo", "head_sha": self.HEAD},
            {"pr": 7, "head_sha": self.HEAD},
            {"repo": "owner/repo", "pr": "7", "head_sha": self.HEAD},
            {"repo": "owner/repo", "pr": 0, "head_sha": self.HEAD},
            {"repo": "owner", "pr": 7, "head_sha": self.HEAD},
            {"repo": "owner/repo", "pr": 7, "head_sha": " "},
            [],
        ):
            with self.subTest(body=body):
                self.assertEqual(self.post(client, identity, body=body).status_code, 400)
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_post_refuses_an_unconfigured_repository(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        response = self.post(client, identity, body={"repo": "other/repo", "pr": 7, "head_sha": self.HEAD, "latest_run_id": None})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_post_on_a_moved_closed_or_draft_pr_is_409(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        for pr in (open_pr("c" * 40), {**open_pr(self.HEAD), "state": "closed"}, {**open_pr(self.HEAD), "draft": True}):
            with self.subTest(pr=pr):
                self.github.pr = pr
                response = self.post(client, identity)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertIn("error", response.json())
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_post_while_a_request_is_queued_or_running_is_409(self) -> None:
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        identity = self.login(client)
        # A queued request for this PR, even for an older head, blocks.
        queued = self.store.enqueue_retry("owner/repo", 7, "9" * 40, ADMIN_EMAIL)
        response = self.post(client, identity)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("already queued or running", response.json()["error"])
        self.store.claim_retry()
        self.assertEqual(self.post(client, identity).status_code, 409, "a claimed (running) request blocks too")
        self.store.finish_retry(queued["id"], "failed", error="x")
        # Another PR's request does not block this one.
        self.store.enqueue_retry("owner/repo", 8, self.HEAD, ADMIN_EMAIL)
        self.assertEqual(self.post(client, identity).status_code, 202)
        accepted = self.store.calls_named("enqueue_retry")[-1]
        self.assertEqual(accepted[1][:3], ("owner/repo", 7, self.HEAD))
        self.assertEqual(len(self.store.calls_named("enqueue_retry")), 3, "the two fixtures plus the accepted rereview")

    def test_post_when_store_admission_races_after_precheck_is_409(self) -> None:
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        identity = self.login(client)
        busy_message = "a review of this pull request is already queued or running"
        # The empty fake passes the route's pre-check, but another admission
        # wins before enqueue_retry performs the store's transactional check.
        with mock.patch.object(self.store, "enqueue_retry", side_effect=PrBusyError(busy_message)) as enqueue:
            response = self.post(client, identity)

        self.assertEqual(self.store.calls_named("active_activity_for_pr"), [("active_activity_for_pr", ("owner/repo", 7), {})])
        enqueue.assert_called_once_with(
            "owner/repo", 7, self.HEAD, ADMIN_EMAIL,
            retry_of=None, options={"kind": "rereview", "scope": "all", "seen_at": mock.ANY},
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"], busy_message)
        self.assertEqual(self.store.list_retries(), [])

    def test_a_web_double_post_on_the_same_head_is_202_then_409(self) -> None:
        # Round 2: identical-head contention through the real route, not just
        # the store. The second POST must be refused even though it targets
        # the exact head the first POST's request already covers.
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        identity = self.login(client)
        first = self.post(client, identity)
        self.assertEqual(first.status_code, 202, first.text)
        second = self.post(client, identity)
        self.assertEqual(second.status_code, 409, second.text)
        self.assertIn("already queued or running", second.json()["error"])
        self.assertEqual(len(self.store.calls_named("enqueue_retry")), 1, "the second POST never reached enqueue_retry")

    def test_post_while_an_attempt_of_the_pr_is_running_is_409(self) -> None:
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        identity = self.login(client)
        self.store.force_run(head_sha="c" * 40)  # running, another head of the same PR
        response = self.post(client, identity)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("already queued or running", response.json()["error"])
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_post_with_an_unknown_posting_outcome_is_409(self) -> None:
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        identity = self.login(client)
        run = self.store.force_run(head_sha=self.HEAD)
        self.store.update_run(run["id"], "interrupted", stage=dx.POSTING_STAGE)
        response = self.post(client, identity)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("outcome is unknown", response.json()["error"])
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_post_queues_a_rereview_of_the_latest_attempt_for_the_head(self) -> None:
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        identity = self.login(client)
        self.succeeded_run()
        latest = self.succeeded_run()

        response = self.post(client, identity)
        self.assertEqual(response.status_code, 202, response.text)
        request = response.json()["request"]
        self.assertEqual((request["repo"], request["pr"], request["head_sha"]), ("owner/repo", 7, self.HEAD))
        self.assertEqual(request["retry_of"], latest["id"])
        self.assertEqual(request["status"], "queued")
        self.assertEqual(request["kind"], "rereview")
        self.assertEqual(request["scope"], "all")
        call = self.store.calls_named("enqueue_retry")[-1]
        self.assertEqual(call[1], ("owner/repo", 7, self.HEAD, ADMIN_EMAIL))
        self.assertEqual(
            call[2], {"retry_of": latest["id"], "options": {"kind": "rereview", "scope": "all", "seen_at": mock.ANY}}
        )
        self.assertEqual(self.github.calls, [7])
        # The queue listing and the page both show the kind.
        listed = {row["id"]: row for row in client.get("/api/retries").json()["requests"]}
        self.assertEqual(listed[request["id"]]["kind"], "rereview")
        self.assertEqual(client.get(self.PATH).json()["active_request"]["id"], request["id"])

    def test_post_without_an_earlier_attempt_queues_a_first_review(self) -> None:
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        identity = self.login(client)
        response = self.post(client, identity)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertIsNone(response.json()["request"]["retry_of"])

    def test_post_upstream_failure_is_a_sanitized_502(self) -> None:
        self.github.error = "GitHub API GET failed: 401 Bad credentials token=ghp_abcdefghijklmnopqrstuvwxyz0123456789"
        client = self.make_client()
        identity = self.login(client)
        response = self.post(client, identity)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("ghp_", response.text)
        self.assertNotIn("Bad credentials", response.text)
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_the_enqueued_event_carries_the_kind(self) -> None:
        # Reviewer: a real enqueue must emit the event, not a hand-built one
        # (``force_event`` is reserved for the bogus-payload edge case below).
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        identity = self.login(client)
        self.assertEqual(self.post(client, identity).status_code, 202)
        event = self.store.events_after(0)[-1]
        self.assertEqual(event["kind"], "retry_enqueued")
        self.assertEqual(event["payload"]["kind"], "rereview")
        projected = dashboard_web.project_event(event)
        self.assertEqual(projected["payload"]["kind"], "rereview")
        self.assertIsNone(
            dashboard_web.project_event({**event, "payload": {**event["payload"], "kind": "bogus"}})["payload"]["kind"]
        )

    def test_enqueuing_through_a_real_store_emits_the_kind_and_the_projection_carries_it(self) -> None:
        # Round 2: the test above uses FakeDashboardStore; this drives the
        # real DashboardStore end to end (SQLite row -> event -> projection).
        from code_review_service.dashboard_store import DashboardStore

        self.github.pr = open_pr(self.HEAD)
        self.store = DashboardStore(self.service_root / "data" / "dashboard" / "dashboard.sqlite3")
        client = self.make_client()
        identity = self.login(client)
        self.assertEqual(self.post(client, identity).status_code, 202)
        enqueued = [event for event in self.store.events_after(0) if event["kind"] == "retry_enqueued"]
        self.assertEqual(len(enqueued), 1, enqueued)
        self.assertEqual(enqueued[0]["payload"]["kind"], "rereview")
        self.assertEqual(dashboard_web.project_event(enqueued[0])["payload"]["kind"], "rereview")

    def test_post_requires_a_session(self) -> None:
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        response = client.post(
            "/api/retrigger",
            content=json.dumps({"repo": "owner/repo", "pr": 7, "head_sha": self.HEAD}),
            headers={"Origin": PUBLIC_URL, "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])
        self.assertEqual(self.github.calls, [])

    def test_get_for_a_viewer_projects_a_latest_run_seeded_with_sensitive_fields(self) -> None:
        """Reviewer: the GET route must stay projected even with hostile store rows."""
        self.github.pr = open_pr(self.HEAD)
        client = self.make_client()
        self.viewer_login(client)
        run = self.succeeded_run(
            llm={"prompt": "must not appear", "policy": {"model": "gpt-6-astra[1m]", "api_key": "leak"}},
            agent_results='[{"secret": "/Users/operator/private"}]',
            run_dir="/private/var/leak",
        )
        body = client.get(self.PATH).json()
        self.assertEqual(body["latest_run"]["id"], run["id"])
        rendered = json.dumps(body)
        for leaked in ("must not appear", "api_key", "leak", "/Users/operator/private", "run_dir"):
            self.assertNotIn(leaked, rendered, leaked)


class RunLlmProjectionTest(AppTestCase):
    def test_run_exposes_bounded_llm_record_and_agent_model_fields(self) -> None:
        client = self.make_client()
        self.login(client)
        run = self.store.force_run(
            status="succeeded",
            llm={
                "policy": {"model": "gpt-6-astra[1m]", "effort": "xhigh", "fallback_models": ["grok-4.6"], "source": "dashboard", "api_key": "leak"},
                "totals": {"input_tokens": 10, "output_tokens": 2, "agents_with_usage": 1, "models_used": ["grok-4.6"], "cost_usd": 1.5},
                "prompt": "must not appear",
            },
            agents=[{"id": "correctness", "status": "done", "summary": "ok", "model": "grok-4.6", "effort": "xhigh",
                     "input_tokens": 10, "output_tokens": 2, "elapsed_seconds": 4.5, "route": "gpt-6-astra[1m] ✗ quota → grok-4.6 ✓",
                     "secret": "/Users/operator/private"}],
        )
        payload = client.get(f"/api/runs/{run['id']}").json()["run"]
        self.assertEqual(payload["llm"]["policy"], {"model": "gpt-6-astra[1m]", "effort": "xhigh", "fallback_models": ["grok-4.6"], "source": "dashboard"})
        self.assertEqual(payload["llm"]["totals"], {"input_tokens": 10, "output_tokens": 2, "agents_with_usage": 1, "models_used": ["grok-4.6"]})
        self.assertNotIn("prompt", payload["llm"])
        agent = payload["agents"][0]
        self.assertEqual(agent["model"], "grok-4.6")
        self.assertEqual(agent["input_tokens"], 10)
        self.assertEqual(agent["elapsed_seconds"], 4.5)
        self.assertEqual(agent["route"], "gpt-6-astra[1m] ✗ quota → grok-4.6 ✓")
        self.assertNotIn("secret", agent)

    def test_run_without_llm_record_projects_null(self) -> None:
        client = self.make_client()
        self.login(client)
        run = self.store.force_run(status="failed")
        payload = client.get(f"/api/runs/{run['id']}").json()["run"]
        self.assertIsNone(payload["llm"])

    def test_agent_failure_detail_is_bounded_and_redacted(self) -> None:
        """Each hop's upstream text reaches the run view, minus paths and credential-shaped tokens."""
        client = self.make_client()
        self.login(client)
        run = self.store.force_run(
            status="failed",
            agents=[{"id": "security", "status": "failed", "summary": "agent failed with exit code 1", "attempt_log_omitted": 13,
                     "error": "llm_runner: all models exhausted:\nreading /Users/operator/work/prompt.md failed\n" + "budget gone " * 300,
                     "attempt_log": [
                         {"model": "gpt-6-astra[1m]", "attempt": 1, "outcome": "permanent_error", "http_status": 400, "elapsed_seconds": 0.4,
                          "error": "invalid_request_error: key sk-abcdefghijklmnop rejected for /Users/operator/private/x", "prompt": "never"},
                         "junk",
                         {"model": "grok-4.6", "attempt": True, "outcome": "succeeded"},
                         {"model": "grok-4.6", "attempt": 1, "outcome": "transient_error", "http_status": 503, "retry_after_seconds": 2,
                          "error": "HTTP 503: upstream down"},
                     ]}],
        )
        agent = client.get(f"/api/runs/{run['id']}").json()["run"]["agents"][0]
        first, second = agent["attempt_log"]
        self.assertEqual(agent["attempt_log_omitted"], 13)
        self.assertEqual(set(first), {"model", "attempt", "outcome", "http_status", "elapsed_seconds", "error"})
        self.assertEqual((first["model"], first["attempt"], first["outcome"], first["http_status"]), ("gpt-6-astra[1m]", 1, "permanent_error", 400))
        self.assertEqual(first["error"], "invalid_request_error: key [redacted] rejected for [path]")
        self.assertEqual((second["retry_after_seconds"], second["error"]), (2.0, "HTTP 503: upstream down"))
        self.assertLessEqual(len(agent["error"]), 2000)
        self.assertTrue(agent["error"].endswith("budget gone"), "the tail is kept")
        short = self.store.force_run(status="failed", agents=[{"id": "tests", "status": "failed", "summary": "agent timed out",
                                                                "error": "timed out reading /Users/operator/work/prompt.md\nghp_abcdefghijklmnopqrstuvwxyz"}])
        error = client.get(f"/api/runs/{short['id']}").json()["run"]["agents"][0]["error"]
        self.assertEqual(error, "timed out reading [path]\n[redacted]")


class ModelPolicyHardeningTest(AppTestCase):
    """Review findings: invalid config policy, unknown fields, catalog-backed refusal, unavailable cache."""

    def _config(self, policy: Any) -> None:
        self.config_path.write_text(json.dumps({
            "repositories": ["owner/repo"],
            "agents": [{"id": "correctness"}, {"id": "security"}, {"id": "tests"}],
            "gh_bin": "gh",
            "model_policy": policy,
        }), encoding="utf-8")

    def test_invalid_config_policy_still_renders_and_a_put_reports_success(self) -> None:
        self._config({"model": "gpt-6-astra[1m]", "effort": "maximum"})
        client = self.make_client(usage_transport=catalog_transport())
        identity = self.login(client)
        payload = client.get("/api/settings/model-policy").json()
        self.assertIsNone(payload["policy"])
        self.assertIn("config model policy unavailable", payload["error"])
        response = client.put("/api/settings/model-policy", content=json.dumps({"model": "grok-4.6"}), headers=self.auth_headers(identity))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["source"], "dashboard")
        self.assertEqual(response.json()["policy"]["model"], "grok-4.6")
        self.assertEqual(len(self.store.calls_named("set_setting")), 1)

    def test_put_with_an_unknown_field_is_a_400_not_a_silent_reset(self) -> None:
        client = self.make_client(usage_transport=catalog_transport())
        identity = self.login(client)
        response = client.put("/api/settings/model-policy", content=json.dumps({"model": "grok-4.6", "fallbacks": ["gpt-5.5"]}), headers=self.auth_headers(identity))
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("unknown fields", response.json()["error"])
        self.assertEqual(self.store.calls_named("set_setting"), [])

    def test_catalog_usability_verdict_refuses_ids_the_prefix_rule_misses(self) -> None:
        payload = {"models": [{"id": "sonnet-legacy", "name": "Anthropic leg", "group": "claude", "efforts": [], "max_context": 1}]}
        client = self.make_client(usage_transport=catalog_transport(payload=payload))
        identity = self.login(client)
        response = client.put("/api/settings/model-policy", content=json.dumps({"model": "sonnet-legacy"}), headers=self.auth_headers(identity))
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("Claude models are not available", response.json()["error"])
        self.assertEqual(self.store.calls_named("set_setting"), [])
        head = "a" * 40
        self.github.pr = open_pr(head)
        run = self.failed_run(head)
        retry = client.post(f"/api/runs/{run['id']}/retry", content=json.dumps({"head_sha": head, "model_policy": {"model": "sonnet-legacy"}}), headers=self.auth_headers(identity))
        self.assertEqual(retry.status_code, 400, retry.text)
        self.assertEqual(self.store.calls_named("enqueue_retry"), [])

    def test_aliases_are_judged_like_their_ids(self) -> None:
        """``opus`` is a catalog alias of a Claude id; the verdict and the PUT must see it."""
        payload = {"models": [
            {"id": "claude-opus-5[1m]", "aliases": ["opus"], "name": "Opus", "group": "claude", "efforts": []},
            {"id": "x-legacy", "aliases": ["claude-x"], "name": "Legacy leg", "group": "other", "efforts": []},
            {"id": "grok-4.6", "aliases": ["grok"], "name": "Grok", "group": "grok", "efforts": []},
        ]}
        client = self.make_client(usage_transport=catalog_transport(payload=payload))
        identity = self.login(client)
        models = {m["id"]: m for m in client.get("/api/models").json()["models"]}
        self.assertFalse(models["x-legacy"]["usable"])
        self.assertTrue(models["grok-4.6"]["usable"])
        for chosen in ("opus", "x-legacy", "claude-x"):
            response = client.put("/api/settings/model-policy", content=json.dumps({"model": chosen}), headers=self.auth_headers(identity))
            self.assertEqual(response.status_code, 400, (chosen, response.text))
        response = client.put("/api/settings/model-policy", content=json.dumps({"model": "grok"}), headers=self.auth_headers(identity))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.store.calls_named("set_setting")[-1][1][1]["model"], "grok")

    def test_catalog_marks_ids_the_validator_rejects_as_unusable(self) -> None:
        payload = {"models": [{"id": "vendor/slug:free", "name": "OR", "group": "openrouter", "efforts": []}, {"id": "or-free", "name": "OR", "group": "openrouter", "efforts": []}]}
        client = self.make_client(usage_transport=catalog_transport(payload=payload))
        self.login(client)
        models = {m["id"]: m for m in client.get("/api/models").json()["models"]}
        self.assertFalse(models["vendor/slug:free"]["usable"])
        self.assertIn("validator", models["vendor/slug:free"]["reason"])
        self.assertTrue(models["or-free"]["usable"])

    def test_rejected_aliases_are_not_offered_and_aliases_cannot_shadow_ids(self) -> None:
        payload = {"models": [
            {"id": "grok-4.6", "aliases": ["grok", "bad/alias"], "name": "Grok", "group": "grok", "efforts": []},
            {"id": "x-leg", "aliases": ["gpt-5.5"], "name": "Shadowing alias", "group": "other", "efforts": []},
            {"id": "gpt-5.5", "aliases": [], "name": "Real", "group": "claude", "efforts": []},
        ]}
        client = self.make_client(usage_transport=catalog_transport(payload=payload))
        identity = self.login(client)
        models = {m["id"]: m for m in client.get("/api/models").json()["models"]}
        self.assertEqual(models["grok-4.6"]["aliases"], ["grok"])
        response = client.put("/api/settings/model-policy", content=json.dumps({"model": "gpt-5.5"}), headers=self.auth_headers(identity))
        self.assertEqual(response.status_code, 400, "the id's own (unusable) verdict wins over another row's alias")

    def test_put_strips_a_client_supplied_source(self) -> None:
        client = self.make_client()
        identity = self.login(client)
        response = client.put("/api/settings/model-policy", content=json.dumps({"model": "grok-4.6", "source": "request"}), headers=self.auth_headers(identity))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("source", self.store.calls_named("set_setting")[-1][1][1])
        self.assertEqual(response.json()["source"], "dashboard")

    def test_catalog_redirects_are_not_followed(self) -> None:
        recorded: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/llmux/models":
                return httpx.Response(302, headers={"location": "https://evil.example/models"})
            return httpx.Response(200, json=USAGE_PAYLOAD)

        client = self.make_client(usage_transport=usage_transport(handler=handler, requested=recorded))
        self.login(client)
        body = client.get("/api/models").json()
        self.assertFalse(body["available"])
        self.assertEqual([request.url.host for request in recorded if request.url.path == "/llmux/models"], ["127.0.0.1"])

    def test_oversized_catalog_is_refused(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/llmux/models":
                return httpx.Response(200, json={"models": [], "filler": "x" * (dashboard_web.MODEL_CATALOG_MAX_BYTES + 1024)})
            return httpx.Response(200, json=USAGE_PAYLOAD)

        client = self.make_client(usage_transport=usage_transport(handler=handler))
        self.login(client)
        body = client.get("/api/models").json()
        self.assertFalse(body["available"])
        self.assertTrue(body["error"])

    def test_unavailable_catalog_is_cached_briefly(self) -> None:
        transport = catalog_transport(status_code=503)
        client = self.make_client(usage_transport=transport)
        self.login(client)
        for _ in range(3):
            self.assertFalse(client.get("/api/models").json()["available"])
        self.assertEqual(len([r for r in transport.recorded if r.url.path == "/llmux/models"]), 1)

    def test_get_policy_answers_from_config_without_a_store(self) -> None:
        self._config({"model": "gpt-6-astra[1m]", "effort": "xhigh", "fallback_models": ["grok-4.6"]})
        client = self.make_client(store=None)
        self.login(client)
        payload = client.get("/api/settings/model-policy").json()
        self.assertEqual(payload["source"], "config")
        self.assertEqual(payload["policy"]["model"], "gpt-6-astra[1m]")

    def test_catalog_ids_mangled_by_redaction_are_dropped(self) -> None:
        payload = {"models": [{"id": "A" * 60, "name": "long", "group": "codex", "efforts": []}, {"id": "grok-4.6", "name": "Grok", "group": "grok", "efforts": []}]}
        client = self.make_client(usage_transport=catalog_transport(payload=payload))
        self.login(client)
        self.assertEqual([m["id"] for m in client.get("/api/models").json()["models"]], ["grok-4.6"])

    def test_retry_enqueued_event_projects_the_policy(self) -> None:
        client = self.make_client()
        self.login(client)
        self.store.force_event("retry_enqueued", None, {"request_id": "req-1", "status": "queued", "repo": "owner/repo", "pr": 7, "head_sha": "a" * 40, "model_policy": {"model": "grok-4.6", "effort": "xhigh", "fallback_models": [], "api_key": "leak"}})
        state = collect_sse(self.app, "/api/events?after=0", self.cookie(client), minimum_events=1)
        event = json.loads(state["data_blocks"][-1].split("data: ", 1)[1])
        self.assertEqual(event["payload"]["model_policy"], {"model": "grok-4.6", "effort": "xhigh", "fallback_models": []})


# ---------------------------------------------------------------------------
# 16. Loopback service summary (the intranet portal's card)
# ---------------------------------------------------------------------------


def hours_ago(hours: float) -> str:
    """A store-shaped UTC timestamp ``hours`` in the past."""
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).isoformat(timespec="seconds")


class ServiceSummaryTest(AppTestCase):
    """``GET /api/summary``: loopback peer plus shared secret, and nothing else.

    The portal is a co-hosted service, not a browser, so the test peer address
    is the security boundary under test: ``TestClient(client=...)`` sets it, and
    the default ``("testclient", 50000)`` is deliberately *not* loopback.
    """

    def summary_client(self, peer: tuple[str, int] = ("127.0.0.1", 54321), **kwargs: Any) -> TestClient:
        settings = kwargs.pop("settings", None) or self.settings(rol_service_token=SERVICE_TOKEN)
        app = self.make_app(settings=settings, **kwargs)
        client = TestClient(app, base_url=PUBLIC_URL, client=peer)
        self.addCleanup(client.close)
        self.app = app
        return client

    def get_summary(self, client: TestClient, token: str | None = SERVICE_TOKEN):
        # The portal addresses the loopback bind directly, never the public
        # origin: ``Host`` is part of the boundary under test.
        headers = {"Host": LOOPBACK_HOST}
        if token is not None:
            headers["X-Service-Token"] = token
        return client.get("/api/summary", headers=headers)

    def executor_paths(self) -> dict[str, Path]:
        config = svc.load_config(self.config_path)
        state_path = self.service_root / "data" / "state.json"
        return {
            "lock": dx.executor_lock_path(config, state_path),
            "pause": dx.executor_pause_path(config, state_path),
        }

    def seed_runs(self) -> None:
        """Two live attempts and a terminal history straddling the 24h edge."""
        recent, old = hours_ago(1), hours_ago(30)
        self.store.force_run(pr=1)
        self.store.force_run(pr=2)
        self.store.force_run(pr=3, status="failed", updated_at=recent)
        self.store.force_run(pr=4, status="failed", updated_at=old)
        self.store.force_run(pr=5, status="succeeded", updated_at=recent)
        self.store.force_run(pr=6, status="interrupted", updated_at=recent)

    def test_route_is_absent_until_the_token_is_configured(self) -> None:
        client = self.summary_client(settings=self.settings())
        response = self.get_summary(client)
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json(), {"error": "not found"})

    def test_blank_token_setting_also_leaves_the_route_absent(self) -> None:
        client = self.summary_client(settings=self.settings(rol_service_token=""))
        self.assertEqual(self.get_summary(client, token="").status_code, 404)

    def test_whitespace_only_token_setting_leaves_the_route_absent(self) -> None:
        # Long enough to pass a naive length check, blank by any sane reading:
        # validation and activation must agree that this is "not configured".
        for blank in (" " * MIN_TOKEN_LEN, "\t" * MIN_TOKEN_LEN, " \n" * MIN_TOKEN_LEN):
            with self.subTest(repr(blank[:3])):
                dashboard_web.validate_settings(self.settings(rol_service_token=blank))
                client = self.summary_client(settings=self.settings(rol_service_token=blank))
                self.assertEqual(self.get_summary(client, token=blank).status_code, 404)
                self.assertEqual(self.get_summary(client, token="").status_code, 404)

    def test_short_token_is_refused_at_boot_without_printing_it(self) -> None:
        with self.assertRaises(dashboard_web.SettingsError) as ctx:
            dashboard_web.validate_settings(self.settings(rol_service_token="short-token-value"))
        self.assertNotIn("short-token-value", str(ctx.exception))

    def test_non_loopback_peer_is_refused_even_with_the_right_token(self) -> None:
        client = self.summary_client(peer=("10.9.9.9", 54321))
        response = self.get_summary(client)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json(), {"error": "loopback only"})
        self.assertEqual(self.store.calls_named("run_status_counts"), [])

    def test_public_host_header_is_refused_even_from_a_loopback_peer(self) -> None:
        # What a request relayed by the co-hosted reverse proxy looks like: the
        # peer is 127.0.0.1 (the proxy) but the Host is the public origin.
        client = self.summary_client(peer=("127.0.0.1", 54321))
        response = client.get("/api/summary", headers={"X-Service-Token": SERVICE_TOKEN, "Host": "rol.company.example"})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json(), {"error": "loopback only"})
        self.assertEqual(self.store.calls_named("run_status_counts"), [])

    def test_ipv6_loopback_peer_is_accepted(self) -> None:
        client = self.summary_client(peer=("::1", 54321))
        self.assertEqual(self.get_summary(client).status_code, 200)

    def test_missing_or_wrong_token_is_refused_even_for_a_signed_in_admin(self) -> None:
        client = self.summary_client()
        self.login(client)
        # Includes an equal-length token that differs in one character: the
        # comparison must be on the bytes, not on the length.
        for token in (None, "", "wrong-" + SERVICE_TOKEN, SERVICE_TOKEN[:-1], SERVICE_TOKEN + "x",
                      SERVICE_TOKEN[:-1] + "X"):
            with self.subTest(token=token):
                response = self.get_summary(client, token=token)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json(), {"error": "invalid service token"})

    def test_summary_counts_buckets_like_the_console_and_carries_no_session(self) -> None:
        self.seed_runs()
        client = self.summary_client()
        self.login(client)
        console_policy = client.get("/api/settings/model-policy")
        self.assertEqual(console_policy.status_code, 200, console_policy.text)

        response = self.get_summary(client)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(
            body["counts"],
            {"active": 2, "attention": 3, "succeeded_24h": 1, "failed_24h": 1, "history": 6},
        )
        # The card must not disagree with the Settings view it summarises.
        self.assertEqual(body["policy"], console_policy.json())
        self.assertEqual(body["version"], client.get("/healthz").json()["version"])
        self.assertEqual(body["executor"], {"state": "idle", "in_flight": False, "pid": None})
        self.assertEqual(body["last_event_at"], "2026-09-11T00:00:00+00:00")
        self.assertIsInstance(body["generated_at"], float)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("set-cookie", response.headers)
        raw = response.text
        for leak in (SERVICE_TOKEN, LLMUX_KEY, GOOGLE_SECRET, SESSION_SECRET, str(self.config_path)):
            self.assertNotIn(leak, raw)

    def test_empty_deployment_reports_zero_counts_and_no_last_event(self) -> None:
        client = self.summary_client()
        body = self.get_summary(client).json()
        self.assertEqual(
            body["counts"],
            {"active": 0, "attention": 0, "succeeded_24h": 0, "failed_24h": 0, "history": 0},
        )
        self.assertIsNone(body["last_event_at"])

    def test_a_held_executor_lock_is_reported_as_running(self) -> None:
        client = self.summary_client()
        lock = dx.ExecutorLock(self.executor_paths()["lock"])
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertEqual(
            self.get_summary(client).json()["executor"],
            {"state": "running", "in_flight": True, "pid": os.getpid()},
        )

    def test_pause_sentinel_is_reported_as_paused_from_the_filesystem(self) -> None:
        client = self.summary_client()
        pause_path = self.executor_paths()["pause"]
        pause_path.parent.mkdir(parents=True, exist_ok=True)
        pause_path.touch()
        # Paused with nothing held: intake is stopped and no attempt is in flight.
        self.assertEqual(
            self.get_summary(client).json()["executor"],
            {"state": "paused", "in_flight": False, "pid": None},
        )

    def test_unreadable_config_reports_unknown_not_idle(self) -> None:
        client = self.summary_client()
        self.config_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(
            self.get_summary(client).json()["executor"],
            {"state": "unknown", "in_flight": None, "pid": None},
        )

    def test_summary_needs_a_store(self) -> None:
        client = self.summary_client(store=None)
        response = self.get_summary(client)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), {"error": "dashboard store is unavailable"})

    def test_store_without_the_summary_helpers_answers_503(self) -> None:
        client = self.summary_client(store=StoreWithoutSummaryCounts())
        response = self.get_summary(client)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), {"error": "dashboard summary is unavailable"})

    def test_summary_is_read_only(self) -> None:
        client = self.summary_client()
        for method in (client.post, client.put, client.delete):
            with self.subTest(method=method.__name__):
                self.assertEqual(
                    method("/api/summary", headers={"X-Service-Token": SERVICE_TOKEN}).status_code, 405
                )


if __name__ == "__main__":
    unittest.main()
