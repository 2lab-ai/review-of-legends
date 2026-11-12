"""Authenticated dashboard web API (W3 of .prd/admin-dashboard/loop.md).

Shape of the tier, and why:

- **It cannot run a review.** There is no thread, subprocess or executor here.
  The only mutation is ``enqueue_retry``, a row the daemon later claims under its
  own OS lock (W2). A compromised browser session can therefore queue work, not
  execute it, and cannot choose a repository, a command or a requester.
- **Every read is a projection, never a passthrough.** Store rows and llmux
  telemetry are copied field-by-field onto the keys in ``api-contract.md``, with
  text bounded and redacted. Adding a column upstream cannot leak a filesystem
  path, a prompt or a credential into a browser.
- **Authorization is rechecked per request**, including inside the SSE loop, so
  logout, expiry and a revoked admin grant take effect on an already-open
  stream; the role itself is re-derived from the owner's grants (dashboard_auth.
  resolve_session) rather than trusted from sign-in. The one non-session
  route, ``GET /api/summary``, is read-only, exists only when an optional shared
  secret is configured, and answers only a loopback peer that presents it.
- **Secrets are inputs, never outputs.** Settings come from a protected external
  JSON file the installer (W5) owns; this process never generates a secret, never
  logs one, and reports missing secret settings without naming them.

CLI: ``python -m code_review_service.dashboard_web --settings PATH`` binds
127.0.0.1:8840 with uvicorn's access log off (an access log of ``/auth/callback``
would persist the authorization code).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hmac
import ipaddress
import json
import math
import os
import re
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Mapping

import anyio
import httpx
from authlib.common.errors import AuthlibBaseError
from authlib.integrations.starlette_client import OAuth
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import contributors
from . import dashboard_auth
from . import mentions
from . import service as svc
from .dashboard_store import (
    ADMINS_SETTING,
    AGENT_ID_PATTERN,
    MAX_ADDED_ADMINS,
    MAX_ADDED_REPOSITORIES,
    MAX_ROW_ID,
    REPOSITORIES_SETTING,
    SupersededConflict,
)
from .dashboard_store import RETRY_SCOPE_ALL, RETRY_SCOPE_FAILED, RETRY_SCOPE_OPTION, RETRY_SCOPES
from .dashboard_store import RETRY_KIND_OPTION, RETRY_KIND_REREVIEW, RETRY_SEEN_AT_OPTION, PrBusyError
from .dashboard_store import LOGIN_PATTERN

try:  # Authlib 1.8 parses id_tokens through joserfc, whose errors are not AuthlibBaseError.
    from joserfc.errors import JoseError as _JoseError

    OAUTH_VALIDATION_ERRORS: Tuple[type, ...] = (AuthlibBaseError, _JoseError)
except ModuleNotFoundError:  # pragma: no cover - joserfc ships with Authlib 1.8
    OAUTH_VALIDATION_ERRORS = (AuthlibBaseError,)

try:  # W2 owns the "did the review POST maybe land?" rule; never duplicate it here.
    from . import dashboard_execution
except ImportError:  # pragma: no cover - W2 landed before this module
    dashboard_execution = None  # type: ignore[assignment]


# --------------------------------------------------------------------- settings

DEFAULT_SETTINGS_PATH = Path.home() / "review-of-legends/dashboard-secrets.json"
CANONICAL_PUBLIC_URL = os.environ.get("ROL_DASHBOARD_PUBLIC_URL", "").rstrip("/")
BIND_HOST = "127.0.0.1"
BIND_PORT = 8840
CALLBACK_PATH = "/auth/callback"

# Settings whose *names* may appear in an error message.
PUBLIC_SETTINGS = ("public_url", "google_client_id", "llmux_base_url", "config_path")
# Settings whose names are withheld: naming them tells an unauthenticated reader
# exactly which credential the deployment is missing.
SECRET_SETTINGS = ("google_client_secret", "session_secret", "llmux_api_key")
MIN_SESSION_SECRET_CHARS = 32
# Optional shared secret for the loopback-only summary endpoint. It is not in
# SECRET_SETTINGS on purpose: a deployment that never provisions it must keep
# booting, and then /api/summary simply does not exist.
SERVICE_TOKEN_SETTING = "rol_service_token"
SERVICE_TOKEN_HEADER = "X-Service-Token"
MIN_SERVICE_TOKEN_CHARS = 32

GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"
GOOGLE_SCOPE = "openid email profile"


class SettingsError(RuntimeError):
    """A settings file/value problem, phrased so it is safe to print or log."""


# ------------------------------------------------------------------ projections

RUN_FIELDS = (
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
    "retry_of",
    "llm",
    # Ids of the agents whose results a failed attempt kept: what a "failed
    # agents" retry reuses. The results themselves never reach the browser.
    "reusable_agents",
)
RUN_TEXT_FIELDS = ("title", "stage", "error")
RUN_INT_FIELDS = ("pr", "successful_agents", "total_agents")

# The store's own statuses (dashboard_store.RUN_STATUSES) plus ``skipped``, which
# labels imported baseline history that was never reviewed. Accepting it as a
# filter keeps "not reviewed" browsable without ever counting it as success.
RUN_STATUSES = frozenset({"running", "succeeded", "failed", "interrupted", "stale", "skipped"})
RETRYABLE_RUN_STATUSES = frozenset({"failed", "interrupted", "stale"})
# The console's own buckets (dashboard/runs.js:11-37): a summary shown elsewhere
# must count what the queue page shows, not a second opinion. ``history`` is the
# remainder, so a status added upstream lands there in both places.
ACTIVE_RUN_STATUSES = ("running",)
ATTENTION_RUN_STATUSES = ("failed", "interrupted", "stale")
SUMMARY_WINDOW_SECONDS = 24 * 60 * 60

RETRY_FIELDS = ("id", "status", "repo", "pr", "head_sha", "retry_of", "created_at", "run_id", "error", "options")
RETRY_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "stale"})

# Model metadata carried on agent rows (dashboard_execution.AGENT_*_FIELDS).
AGENT_TEXT_EXTRA = (("model", 80), ("effort", 16), ("response_model", 80), ("route", 240), ("outcome", 32))
AGENT_INT_EXTRA = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "attempts")
AGENT_FLOAT_EXTRA = ("elapsed_seconds",)
# Failure detail on agent rows (dashboard_execution.ATTEMPT_LOG_* / MAX_AGENT_*).
AGENT_ATTEMPT_LOG_TEXT = (("model", 80), ("outcome", 32))
AGENT_ATTEMPT_LOG_INT = ("attempt", "http_status")
AGENT_ATTEMPT_LOG_FLOAT = ("retry_after_seconds", "elapsed_seconds")
MAX_AGENT_ATTEMPT_LOG = 12
MAX_AGENT_FAILURE_CHARS = 2000
LLM_TOTAL_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "agents_with_usage")
MODEL_POLICY_SETTING = "model_policy"
# An agent PUT carries a whole prompt (svc.MAX_AGENT_PROMPT_CHARS code points);
# ASCII-only JSON writes a non-BMP character as a surrogate pair of \uXXXX
# escapes, 12 bytes, so this route alone lifts the 64 KiB app cap that far.
AGENT_SETTING_MAX_BODY_BYTES = 12 * svc.MAX_AGENT_PROMPT_CHARS + 32 * 1024
# Adding a repository first asks GitHub, through the daemon's own gh login,
# whether the review account can read it.
REPOSITORY_PROBE_TIMEOUT_SECONDS = 20
UNREADABLE_REPOSITORIES = (
    "The saved repository list cannot be read (it is not an object with an 'added' list), so nothing was "
    "changed. Repair or delete the 'repositories' row in the dashboard store, then try again."
)
STORE_READ_FAILED = "The dashboard store could not be read, so nothing was changed; try again."
UNREADABLE_ADMINS = (
    "The saved admin list cannot be read (it is not an object with an 'added' list), so nothing was "
    "changed. Repair or delete the 'admins' row in the dashboard store, then try again."
)
# A grant changes who holds admin power, so only the owner makes one: an added
# admin who could grant could also keep admin alive after the owner revoked it.
OWNER_REQUIRED = "owner role required"
BASELINE_NOTE = (
    "Pull requests already open when a repository is first added are not reviewed; their next push is. "
    "A repository added back resumes from its earlier record."
)
BASELINE_OFF_NOTE = (
    "Pull requests already open when a repository is first added are reviewed on the next cycle "
    "(review_existing_open_prs_on_first_run is on). A repository added back resumes from its earlier record."
)
MODEL_CATALOG_PATH = "/llmux/models"
MODEL_CATALOG_CACHE_SECONDS = 60.0
#: How long the Retrigger page reuses its GitHub read of one PR. The page
#: re-reads on every stream event and any session can open it; without this,
#: each re-read is a credentialed GitHub call. A POST always reads live and
#: refreshes this, so a cached head never decides a rereview.
RETRIGGER_PR_CACHE_SECONDS = 15.0
#: GitHub reads the Retrigger page's GET may make per rolling minute, across
#: every PR (a failed read is remembered for RETRIGGER_PR_CACHE_SECONDS too).
#: Any session can open the page, so neither a nonexistent PR number nor a
#: sweep of PR numbers may spend the GitHub quota the reviews need. The admin
#: POST reads live and is not counted.
RETRIGGER_GITHUB_READS_PER_MINUTE = 30
#: The rolling window RETRIGGER_GITHUB_READS_PER_MINUTE counts over.
RETRIGGER_GITHUB_BUDGET_WINDOW_SECONDS = 60.0
#: How long the start time of the newest successful read of a PR is kept after its
#: payload expired: longer than any GitHub read can take (token 30 s + request 60 s),
#: so a read that started earlier can never install an older head.
RETRIGGER_PR_ORDER_SECONDS = 300.0
#: Re-reads one Retrigger GET makes when its read was superseded by a later one
#: whose payload expired meanwhile; past it the GET answers a retryable 503.
RETRIGGER_PR_MAX_REREADS = 1
#: GitHub reads the Retrigger page's GET may have in flight at once, across PRs.
#: Each read holds a thread-pool worker for up to the GitHub timeouts, and the
#: per-minute budget alone would let distinct PRs occupy every worker.
RETRIGGER_PR_READ_SLOTS = 4
#: How often a GET waiting for another GET's read of the same PR checks back.
RETRIGGER_PR_WAIT_POLL_SECONDS = 0.05
MODEL_CATALOG_MAX_BYTES = 512 * 1024
MODEL_CATALOG_MAX_MODELS = 200
MODEL_GROUP_LABELS = {"codex": "OpenAI Codex", "grok": "xAI Grok", "openrouter": "OpenRouter", "claude": "Anthropic Claude"}
EVENT_PAYLOAD_FIELDS = (
    "status",
    "stage",
    "repo",
    "pr",
    "head_sha",
    "trigger",
    "title",
    "url",
    "retry_of",
    "request_id",
    "review_url",
    "error",
    "successful_agents",
    "total_agents",
    "agents",
    "llm",
    "key",
    "value",
    "model_policy",
    "scope",
    "kind",
    "reusable_agents",
    # author_updated / feedback_added (PR authors' own settings): who, never what.
    "login",
    "feedback_id",
)
MODEL_CATALOG_UNAVAILABLE_CACHE_SECONDS = 10.0

MAX_LIST_LIMIT = 200
DEFAULT_LIST_LIMIT = 50
# PR authors' settings and feedback (``@review-of-legends`` comments, mentions.py):
# read-only here, written by the daemon. A comment link is passed on only when it is a github.com page.
AUTHOR_FIELDS = ("login", "lang", "lang_set_at", "welcomed_at")
FEEDBACK_FIELDS = ("id", "login", "repo", "pr", "comment_url", "text", "created_at")
GITHUB_LINK_PREFIX = "https://github.com/"
MAX_EVENT_BATCH = 500
MAX_TEXT_CHARS = 2000
MAX_AGENT_ROWS = 32
MAX_AGENT_SUMMARY_CHARS = 200
TRUNCATION_MARKER = "...[truncated]"

USAGE_PATH = "/llmux/dashboard"
USAGE_TIMEOUT_SECONDS = 5.0
USAGE_MAX_BYTES = 2 * 1024 * 1024
USAGE_CACHE_SECONDS = 5.0
# llmux reports the age of its own upstream snapshot; a "fresh" HTTP fetch has
# been observed carrying a 1481s-old ``fetched_at_ms``, so freshness is labelled
# rather than assumed.
USAGE_STALE_AFTER_SECONDS = 300
USAGE_ACCOUNT_FIELDS = ("name", "type", "status", "healthy", "paused", "in_flight", "cooldown_until")
# Each allowlisted field also has an expected kind: a proxy that reports
# ``healthy: "yes"`` must not reach the UI as a truthy string.
USAGE_ACCOUNT_FIELD_KINDS = {
    "name": "text",
    "type": "text",
    "status": "text",
    "healthy": "bool",
    "paused": "bool",
    "in_flight": "number",
    # llmux AccountDoc carries Option<u64> epoch seconds here, and the UI renders
    # it numerically: a string must be dropped, not forwarded as a label.
    "cooldown_until": "nullable_number",
}
USAGE_WINDOW_FIELDS = ("utilization", "resets_at", "resets_in_secs", "fetched_at_ms", "source")
USAGE_WINDOWS = ("five_hour", "seven_day")
USAGE_TOTALS_FIELDS = ("requests", "input_tokens", "output_tokens")
USAGE_SESSION_FIELDS = ("requests", "ok", "errors", "tokens_in", "tokens_out")
USAGE_MAX_ACCOUNTS = 100
USAGE_MAX_TEXT_CHARS = 200

SSE_POLL_SECONDS = 0.5
SSE_HEARTBEAT_SECONDS = 15.0
SSE_RECHECK_SECONDS = 5.0
# Wire frames: named events so the browser can addEventListener on them.
HEARTBEAT_FRAME = "event: heartbeat\ndata: {}\n\n"
AUTH_EXPIRED_FRAME = "event: auth-expired\ndata: {}\n\n"
# Bounded fallback for the stream tail until W1 lands ``latest_event_id()``.
MAX_TAIL_SCAN_BATCHES = 200

ASSET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ASSET_SUFFIXES = frozenset(
    # Deliberately no .mjs and no .map: the shipped UI does not need them, and a
    # source map hands a reader the unbundled sources.
    {".html", ".css", ".js", ".svg", ".png", ".jpg", ".webp", ".ico", ".webmanifest", ".txt"}
)
MAX_ASSET_DEPTH = 2

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)

LOGIN_SHELL_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review of Legends</title>
</head>
<body>
<main>
<h1>Review of Legends</h1>
<p>Operational data requires an authenticated admin session.</p>
<p><a href="/auth/login" rel="nofollow">Sign in with Google</a></p>
</main>
</body>
</html>
"""

# Redaction patterns applied to every string that leaves this tier.
_PATH_PATTERN = re.compile(
    r"(?:file://)?/(?:Users|home|opt|private|var|tmp|etc|Volumes|root|srv)/[^\s'\"<>)\]]*"
)
_TOKEN_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{8,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{8,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\b(?:bearer|token|api[_-]?key|secret|password)\b\s*[:=]?\s*[A-Za-z0-9._\-]{8,}"),
    re.compile(r"ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"[A-Za-z0-9_\-]{48,}"),
)
_REDACTED = "[redacted]"


def sanitize_text(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    """Bound a string and strip filesystem paths and credential-shaped tokens."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = _PATH_PATTERN.sub("[path]", text)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    if len(text) > limit:
        text = text[: max(0, limit - len(TRUNCATION_MARKER))] + TRUNCATION_MARKER
    return text


def _optional_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    return None


def project_agents(entries: Any) -> List[Dict[str, Any]]:
    """Bounded ``{id,status,summary}`` rows plus model scalars, redacted. Never the raw agent blob."""
    if dashboard_execution is not None:
        rows = dashboard_execution.normalize_agent_rows(entries)
    else:  # pragma: no cover - W2 landed before this module
        rows = []
        candidates = entries if isinstance(entries, (list, tuple)) else []
        for entry in candidates[:MAX_AGENT_ROWS]:
            if isinstance(entry, dict):
                rows.append(
                    {
                        "id": str(entry.get("id", "")),
                        "status": str(entry.get("status", "") or ""),
                        "summary": str(entry.get("summary", "") or ""),
                    }
                )
    projected: List[Dict[str, Any]] = []
    for row in rows[:MAX_AGENT_ROWS]:
        item: Dict[str, Any] = {
            "id": sanitize_text(row.get("id", ""), 64),
            "status": sanitize_text(row.get("status", ""), 32),
            "summary": sanitize_text(row.get("summary", ""), MAX_AGENT_SUMMARY_CHARS),
        }
        for name, limit in AGENT_TEXT_EXTRA:
            value = row.get(name)
            if isinstance(value, str) and value:
                item[name] = sanitize_text(value, limit)
        for name in AGENT_INT_EXTRA:
            value = _optional_int(row.get(name))
            if value is not None and value >= 0:
                item[name] = value
        for name in AGENT_FLOAT_EXTRA:
            value = _finite_number(row.get(name))
            if value is not None and value >= 0:
                item[name] = round(float(value), 3)
        attempt_log = _project_attempt_log(row.get("attempt_log"))
        if attempt_log:
            item["attempt_log"] = attempt_log
        omitted = _optional_int(row.get("attempt_log_omitted"))
        if omitted is not None and omitted > 0:
            # Failed hops the row had no room for; the run view says so.
            item["attempt_log_omitted"] = omitted
        error = row.get("error")
        if isinstance(error, str) and error:
            item["error"] = sanitize_text(error, MAX_AGENT_FAILURE_CHARS)
        if row.get("reused") is True:
            # Not run in this attempt: the result came from the attempt it retried.
            item["reused"] = True
        projected.append(item)
    return projected


def project_agent_ids(value: Any) -> List[str]:
    """A bounded list of agent ids (the agents a retry can reuse); anything else is dropped."""
    if not isinstance(value, list):
        return []
    return [
        item for item in value[:MAX_AGENT_ROWS] if isinstance(item, str) and AGENT_ID_PATTERN.fullmatch(item)
    ]


def project_retry_scope(value: Any) -> Optional[str]:
    """``failed`` or ``all``; anything else (a request queued before scopes existed) is None."""
    return value if isinstance(value, str) and value in RETRY_SCOPES else None


def project_retry_kind(value: Any) -> Optional[str]:
    """``rereview`` (the Retrigger badge); a plain retry, or anything unknown, is None."""
    return value if isinstance(value, str) and value == RETRY_KIND_REREVIEW else None


def _project_attempt_log(value: Any) -> List[Dict[str, Any]]:
    """Each model hop's outcome, status and upstream text: redacted like every other string."""
    hops: List[Dict[str, Any]] = []
    if not isinstance(value, list):
        return hops
    for hop in value[-MAX_AGENT_ATTEMPT_LOG:]:
        if not isinstance(hop, dict):
            continue
        item: Dict[str, Any] = {}
        for name, limit in AGENT_ATTEMPT_LOG_TEXT:
            text = hop.get(name)
            if isinstance(text, str) and text:
                item[name] = sanitize_text(text, limit)
        for name in AGENT_ATTEMPT_LOG_INT:
            number = _optional_int(hop.get(name))
            if number is not None and number >= 0:
                item[name] = number
        for name in AGENT_ATTEMPT_LOG_FLOAT:
            number = _finite_number(hop.get(name))
            if number is not None and number >= 0:
                item[name] = round(float(number), 3)
        if not all(name in item for name in ("model", "attempt", "outcome")):
            continue
        error = hop.get("error")
        if isinstance(error, str) and error:
            item["error"] = sanitize_text(error, MAX_AGENT_FAILURE_CHARS)
        hops.append(item)
    return hops


def project_model_policy(candidate: Any) -> Optional[Dict[str, Any]]:
    """``{model, effort, fallback_models, source}`` with every value bounded, or None."""
    if not isinstance(candidate, dict):
        return None
    model = candidate.get("model")
    if not isinstance(model, str) or not model.strip():
        return None
    fallbacks = candidate.get("fallback_models")
    projected: Dict[str, Any] = {
        "model": sanitize_text(model, 80),
        "effort": sanitize_text(candidate.get("effort") or "", 16),
        "fallback_models": [
            sanitize_text(item, 80) for item in (fallbacks if isinstance(fallbacks, list) else [])[:16] if isinstance(item, str)
        ],
    }
    source = candidate.get("source")
    if isinstance(source, str) and source:
        projected["source"] = sanitize_text(source, 16)
    return projected


def project_llm(value: Any) -> Optional[Dict[str, Any]]:
    """The attempt's model record: policy + token totals + models used, bounded."""
    if not isinstance(value, dict):
        return None
    projected: Dict[str, Any] = {}
    policy = project_model_policy(value.get("policy"))
    if policy is not None:
        projected["policy"] = policy
    totals = value.get("totals")
    if isinstance(totals, dict):
        clean: Dict[str, Any] = {}
        for name in LLM_TOTAL_FIELDS:
            number = _optional_int(totals.get(name))
            if number is not None and number >= 0:
                clean[name] = number
        models = totals.get("models_used")
        clean["models_used"] = [
            sanitize_text(item, 80) for item in (models if isinstance(models, list) else [])[:16] if isinstance(item, str)
        ]
        projected["totals"] = clean
    return projected or None


def project_run(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Copy a store row onto the contract keys; unknown columns are dropped."""
    source = row or {}
    projected: Dict[str, Any] = {}
    for field in RUN_FIELDS:
        value = source.get(field)
        if field == "agents":
            projected[field] = project_agents(value)
        elif field == "llm":
            projected[field] = project_llm(value)
        elif field == "reusable_agents":
            projected[field] = project_agent_ids(value)
        elif field in RUN_TEXT_FIELDS:
            projected[field] = sanitize_text(value)
        elif field in RUN_INT_FIELDS:
            projected[field] = _optional_int(value)
        elif field in ("id", "repo", "head_sha", "trigger", "status", "retry_of", "review_url", "url"):
            projected[field] = None if value is None else sanitize_text(value, 512)
        else:
            projected[field] = None if value is None else sanitize_text(value, 64)
    return projected


def project_retry_request(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Retry-queue projection; public GitHub requesters are shown, admin emails stay private."""
    source = row or {}
    projected: Dict[str, Any] = {}
    for field in RETRY_FIELDS:
        if field == "options":
            # The stored options blob is exposed as the validated policy and scope only.
            options = source.get("options")
            candidate = options.get("model_policy") if isinstance(options, dict) else None
            projected["model_policy"] = project_model_policy(candidate)
            projected["scope"] = project_retry_scope(options.get(RETRY_SCOPE_OPTION) if isinstance(options, dict) else None)
            projected["kind"] = project_retry_kind(options.get(RETRY_KIND_OPTION) if isinstance(options, dict) else None)
            continue
        value = source.get(field)
        if field == "pr":
            projected[field] = _optional_int(value)
        elif field == "error":
            projected[field] = None if value is None else sanitize_text(value)
        else:
            projected[field] = None if value is None else sanitize_text(value, 512)
    requester = source.get("requested_by")
    if isinstance(requester, str) and requester.startswith("github:") and LOGIN_PATTERN.fullmatch(requester[7:]):
        projected["requested_by"] = requester
    return projected


def project_event(row: Dict[str, Any]) -> Dict[str, Any]:
    """Project an event, including its payload: raw payloads carry error text."""
    payload = row.get("payload")
    projected_payload: Dict[str, Any] = {}
    if isinstance(payload, dict):
        for field in EVENT_PAYLOAD_FIELDS:
            if field not in payload:
                continue
            value = payload[field]
            if field == "agents":
                projected_payload[field] = project_agents(value)
            elif field == "llm":
                projected_payload[field] = project_llm(value)
            elif field == "value":
                projected_payload[field] = project_model_policy(value) if payload.get("key") == MODEL_POLICY_SETTING else None
            elif field == "model_policy":
                projected_payload[field] = project_model_policy(value)
            elif field == "scope":
                projected_payload[field] = project_retry_scope(value)
            elif field == "kind":
                projected_payload[field] = project_retry_kind(value)
            elif field == "reusable_agents":
                projected_payload[field] = project_agent_ids(value)
            elif field in ("pr", "successful_agents", "total_agents", "feedback_id"):
                projected_payload[field] = _optional_int(value)
            else:
                projected_payload[field] = None if value is None else sanitize_text(value)
    return {
        "id": _optional_int(row.get("id")),
        "kind": sanitize_text(row.get("kind"), 64),
        "run_id": None if row.get("run_id") is None else sanitize_text(row.get("run_id"), 128),
        "payload": projected_payload,
        "created_at": sanitize_text(row.get("created_at"), 64),
    }


def _github_link(value: Any) -> Optional[str]:
    """A github.com page link as stored, or None: nothing else becomes a link in the browser."""
    if not isinstance(value, str) or not value.startswith(GITHUB_LINK_PREFIX) or len(value) > 512:
        return None
    return value if not any(char.isspace() or char in "\"'<>" for char in value) else None


def project_author(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A PR author's settings: the login as GitHub spells it, the language, and when."""
    source = row or {}
    login = source.get("display_login") or source.get("login")
    return {
        "login": sanitize_text(login, 100) or None,
        "lang": mentions.stored_language(source.get("lang")),
        "lang_set_at": None if source.get("lang_set_at") is None else sanitize_text(source.get("lang_set_at"), 64),
        "welcomed_at": None if source.get("welcomed_at") is None else sanitize_text(source.get("welcomed_at"), 64),
    }


def project_feedback(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """One piece of feedback; the text is bounded and redacted like any other string here."""
    source = row or {}
    return {
        "id": _optional_int(source.get("id")),
        "login": sanitize_text(source.get("login"), 100) or None,
        "repo": sanitize_text(source.get("repo"), 140) or None,
        "pr": _optional_int(source.get("pr")),
        "comment_url": _github_link(source.get("comment_url")),
        "text": sanitize_text(source.get("body"), mentions.MAX_FEEDBACK_CHARS),
        "created_at": sanitize_text(source.get("created_at"), 64) or None,
    }


def project_model_catalog(payload: Any) -> Optional[List[Dict[str, Any]]]:
    """Allowlisted catalog rows with a usability verdict; None on an unexpected shape.

    A Claude row stays in the list (so an operator sees why it cannot be
    chosen) but is marked ``usable: false`` with the daemon's own reason.
    """
    rows = payload.get("models") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return None
    projected: List[Dict[str, Any]] = []
    for row in rows[:MODEL_CATALOG_MAX_MODELS]:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        raw_id = model_id.strip()
        model_id = sanitize_text(raw_id, 80)
        if model_id != raw_id:
            # An id the redactor rewrote could never be selected and validated
            # as itself; offering the mangled text would only mislead.
            continue
        group = sanitize_text(row.get("group") or "", 32)
        efforts = row.get("efforts")
        aliases = row.get("aliases")
        alias_ids = [
            sanitize_text(item, 40) for item in (aliases if isinstance(aliases, list) else [])[:8] if isinstance(item, str)
        ]
        # An alias the write gate would reject must not be offered either.
        alias_ids = [alias for alias in alias_ids if svc.MODEL_ID_PATTERN.match(alias)]
        # An alias (``opus``) resolves on the proxy to the same leg as its id, so
        # the prefix rule must see every name the row answers to, not just the id.
        usable, reason = True, ""
        for name in [model_id] + alias_ids:
            usable, reason = svc.model_usable(name)
            if not usable:
                break
        if usable and group.lower() == "claude":
            usable, reason = False, svc.UNUSABLE_MODEL_REASON
        if usable and not svc.MODEL_ID_PATTERN.match(model_id):
            # The write gate runs this pattern; offering an id it rejects would
            # only produce a 400 the operator cannot act on.
            usable, reason = False, "model id contains characters the policy validator does not accept"
        projected.append(
            {
                "id": model_id,
                "name": sanitize_text(row.get("name") or model_id, 120),
                "group": group,
                "group_label": MODEL_GROUP_LABELS.get(group.lower(), group or "other"),
                "efforts": [
                    sanitize_text(item, 16) for item in (efforts if isinstance(efforts, list) else [])[:12] if isinstance(item, str)
                ],
                "aliases": alias_ids,
                "max_context": _optional_int(row.get("max_context")),
                "usable": usable,
                "reason": reason or None,
            }
        )
    return projected


def _finite_number(value: Any) -> Optional[float | int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


_USAGE_DROP = object()


def _usage_scalar(value: Any) -> Any:
    """Allow only bounded strings, finite numbers, booleans and null."""
    if value is None or isinstance(value, bool):
        return value
    number = _finite_number(value)
    if number is not None:
        return number
    if isinstance(value, str):
        return value[:USAGE_MAX_TEXT_CHARS]
    return None


def _usage_typed(kind: str, value: Any) -> Any:
    """Return the value when it matches the declared kind, else ``_USAGE_DROP``."""
    if kind == "bool":
        return value if isinstance(value, bool) else _USAGE_DROP
    if kind == "number":
        number = _finite_number(value)
        return _USAGE_DROP if number is None else number
    if kind == "nullable_number":
        if value is None:
            return None
        number = _finite_number(value)
        return _USAGE_DROP if number is None else number
    if isinstance(value, str):
        return value[:USAGE_MAX_TEXT_CHARS]
    return _USAGE_DROP


def _usage_window(raw: Any, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    window: Dict[str, Any] = {}
    for field in USAGE_WINDOW_FIELDS:
        if field not in raw:
            continue
        if field in ("utilization", "resets_in_secs", "fetched_at_ms"):
            value = _finite_number(raw[field])
        else:
            value = _usage_scalar(raw[field])
        if value is None:
            continue
        window[field] = value
    fetched_at_ms = window.get("fetched_at_ms")
    reference = time.time() if now is None else now
    if isinstance(fetched_at_ms, (int, float)):
        age = int(max(0.0, reference - float(fetched_at_ms) / 1000.0))
        window["age_secs"] = age
        window["stale"] = age > USAGE_STALE_AFTER_SECONDS
    else:
        # Unknown freshness is labelled stale: the UI must not imply "just now".
        window["age_secs"] = None
        window["stale"] = True
    return window


def _usage_group(raw: Any, fields: Iterable[str]) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    group: Dict[str, Any] = {}
    for field in fields:
        if field not in raw:
            continue
        value = _finite_number(raw[field])
        if value is None:
            continue
        group[field] = value
    return group


def project_usage_accounts(payload: Any, now: Optional[float] = None) -> Optional[List[Dict[str, Any]]]:
    """Allowlisted per-account projection, or ``None`` when the shape is wrong."""
    if not isinstance(payload, dict):
        return None
    raw_accounts = payload.get("accounts")
    if not isinstance(raw_accounts, list):
        return None
    accounts: List[Dict[str, Any]] = []
    for raw in raw_accounts[:USAGE_MAX_ACCOUNTS]:
        if not isinstance(raw, dict):
            continue
        account: Dict[str, Any] = {}
        for field in USAGE_ACCOUNT_FIELDS:
            if field not in raw:
                continue
            value = _usage_typed(USAGE_ACCOUNT_FIELD_KINDS[field], raw[field])
            if value is _USAGE_DROP:
                continue
            account[field] = value
        for window_name in USAGE_WINDOWS:
            window = _usage_window(raw.get(window_name), now)
            if window is not None:
                account[window_name] = window
        totals = _usage_group(raw.get("totals"), USAGE_TOTALS_FIELDS)
        if totals is not None:
            account["totals"] = totals
        session = _usage_group(raw.get("session"), USAGE_SESSION_FIELDS)
        if session is not None:
            account["session"] = session
        if account:
            accounts.append(account)
    return accounts


# ------------------------------------------------------------- settings loading


def load_settings(path: Path) -> Dict[str, Any]:
    """Read the protected settings JSON. Refuses group/world-readable files."""
    path = Path(path)
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise SettingsError(f"dashboard settings file not readable: {path} ({exc.strerror})") from exc
    if stat.S_IMODE(mode) & 0o077:
        raise SettingsError(
            f"dashboard settings file must not be group/world readable: {path} "
            f"(found {stat.S_IMODE(mode):04o}, expected 0600)"
        )
    try:
        with path.open("r", encoding="utf-8") as handle:
            settings = json.load(handle)
    except json.JSONDecodeError as exc:
        raise SettingsError(f"dashboard settings file is not valid JSON: {path} (line {exc.lineno})") from exc
    except OSError as exc:
        raise SettingsError(f"dashboard settings file not readable: {path} ({exc.strerror})") from exc
    if not isinstance(settings, dict):
        raise SettingsError(f"dashboard settings file must contain a JSON object: {path}")
    validate_settings(settings)
    return settings


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def validate_settings(settings: Dict[str, Any]) -> None:
    """Fail closed on missing/unsafe settings without printing secret names."""
    missing_public = [name for name in PUBLIC_SETTINGS if not str(settings.get(name) or "").strip()]
    missing_secret = [name for name in SECRET_SETTINGS if not str(settings.get(name) or "").strip()]
    if missing_public or missing_secret:
        parts = []
        if missing_public:
            parts.append("missing settings: " + ", ".join(missing_public))
        if missing_secret:
            parts.append(f"missing {len(missing_secret)} secret setting(s) (names withheld)")
        raise SettingsError(
            "; ".join(parts) + ". The deployment installer provisions the dashboard settings file."
        )

    if not CANONICAL_PUBLIC_URL or not dashboard_auth.ADMIN_EMAIL:
        raise SettingsError("Configure ROL_DASHBOARD_PUBLIC_URL and ROL_DASHBOARD_OWNER before starting the dashboard")
    origin = urllib.parse.urlsplit(CANONICAL_PUBLIC_URL)
    if origin.scheme != "https" or not origin.hostname or origin.username or origin.password or origin.path or origin.query or origin.fragment:
        raise SettingsError("ROL_DASHBOARD_PUBLIC_URL must be an HTTPS origin")
    public_url = str(settings["public_url"]).rstrip("/")
    if public_url != CANONICAL_PUBLIC_URL:
        raise SettingsError(f"public_url must be exactly {CANONICAL_PUBLIC_URL}")

    llmux_base_url = str(settings["llmux_base_url"])
    parsed = urllib.parse.urlsplit(llmux_base_url)
    if parsed.scheme not in ("http", "https") or not _is_loopback(parsed.hostname or ""):
        raise SettingsError("llmux_base_url must be a loopback URL (the admin key never leaves the host)")

    if len(str(settings["session_secret"])) < MIN_SESSION_SECRET_CHARS:
        # Never generate one here: W5's installer owns secret material.
        raise SettingsError(
            f"session_secret must be at least {MIN_SESSION_SECRET_CHARS} characters; "
            "the installer provisions it"
        )

    # Absent or blank is a supported deployment: the summary route stays off.
    # Present means it is a credential, so it is held to a credential's length -
    # a three-character "token" on a loopback port is not a second factor.
    # ``service_token_bytes`` is the single predicate for "configured", so
    # validation and activation can never disagree about a blank value.
    service_token_bytes(settings)


def service_token_bytes(settings: Mapping[str, Any]) -> bytes:
    """The configured summary token as bytes, or ``b""`` when the route is off.

    Blank (absent, ``None``, empty or whitespace-only) means "not configured".
    Anything else must be a string of at least ``MIN_SERVICE_TOKEN_CHARS``
    characters and is used byte-for-byte (never stripped), so a legitimate
    token is compared exactly as the operator wrote it.
    """
    raw = settings.get(SERVICE_TOKEN_SETTING)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return b""
    if not isinstance(raw, str) or len(raw) < MIN_SERVICE_TOKEN_CHARS:
        raise SettingsError(
            "the optional loopback summary token must be a string of at least "
            f"{MIN_SERVICE_TOKEN_CHARS} characters (value withheld); "
            "remove the setting to leave the summary endpoint absent"
        )
    return raw.encode("utf-8")


def resolve_paths(settings: Dict[str, Any]) -> Dict[str, Path]:
    """Derive the auth DB, dashboard store and static asset paths."""
    config_path = Path(str(settings["config_path"])).expanduser()
    service_root = svc.service_root_for_config(config_path)
    data_dir = service_root / "data"

    store_path = settings.get("store_path")
    if store_path:
        resolved_store = Path(str(store_path)).expanduser()
    else:
        resolved_store = None
        if dashboard_execution is not None:
            with contextlib.suppress(Exception):
                config = svc.load_config(config_path)
                state_path = Path(str(config.get("state_path") or "data/state.json"))
                if not state_path.is_absolute():
                    state_path = service_root / state_path
                resolved_store = dashboard_execution.dashboard_db_path(config, state_path)
        if resolved_store is None:
            resolved_store = data_dir / "dashboard" / "dashboard.sqlite3"

    return {
        "service_root": service_root,
        "config_path": config_path,
        "auth_db_path": Path(str(settings.get("auth_db_path") or data_dir / "dashboard-auth.sqlite3")).expanduser(),
        "store_path": resolved_store,
        "dashboard_dir": Path(str(settings.get("dashboard_dir") or service_root / "dashboard")).expanduser(),
    }


def uvicorn_run_kwargs(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Loopback bind, no access log (it would persist the OAuth code)."""
    return {
        "host": BIND_HOST,
        "port": int(settings.get("port") or BIND_PORT),
        "access_log": False,
        "log_level": "warning",
        "server_header": False,
        "date_header": True,
        "proxy_headers": False,
        "forwarded_allow_ips": None,
    }


# ------------------------------------------------------------------- HTTP tier


def _json(payload: Any, status_code: int = 200, headers: Optional[Dict[str, str]] = None) -> JSONResponse:
    merged = {"Cache-Control": "no-store"}
    if headers:
        merged.update(headers)
    return JSONResponse(payload, status_code=status_code, headers=merged)


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def _parse_bounded_int(raw: Optional[str], default: int, maximum: int, name: str) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise _bad_request(f"{name} must be an integer") from None
    if value < 0:
        raise _bad_request(f"{name} must not be negative")
    return min(value, maximum)


def _parse_row_id(raw: Optional[str], name: str) -> Optional[int]:
    """A paging key: absent (None) or a positive integer, capped at the largest row id."""
    if raw is None or raw == "":
        return None
    value = _parse_bounded_int(raw, 0, MAX_ROW_ID, name)
    if value < 1:
        raise _bad_request(f"{name} must be a positive integer")
    return value


def stream_tail_cursor(store: Any) -> int:
    """Id of the newest persisted event, i.e. where a fresh stream starts.

    The event log is append-only and never pruned, so a fresh connection that
    started at 0 would replay the whole history before showing anything live.
    ``latest_event_id()`` (W1) answers this in one query; until it exists the
    fallback walks bounded batches instead of streaming the backlog to a browser.
    """
    latest = getattr(store, "latest_event_id", None)
    if callable(latest):
        return max(0, int(latest() or 0))
    cursor = 0
    for _ in range(MAX_TAIL_SCAN_BATCHES):
        batch = store.events_after(cursor, MAX_EVENT_BATCH)
        if not batch:
            break
        cursor = max(cursor, int(batch[-1]["id"]))
    return cursor


def _parse_cursor(raw: Optional[str], name: str) -> int:
    if raw is None or raw == "":
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise _bad_request(f"{name} must be a nonnegative integer") from None
    if value < 0:
        raise _bad_request(f"{name} must be a nonnegative integer")
    return value


def create_app(
    settings: Dict[str, Any],
    store: Any = None,
    *,
    oauth_client: Any = None,
    github_client_factory: Optional[Callable[[str], Any]] = None,
    usage_transport: Any = None,
) -> Starlette:
    """Build the dashboard app.

    ``oauth_client``, ``github_client_factory`` and ``usage_transport`` exist for
    tests only: they are Python arguments, so no request, environment variable or
    settings key can supply them, and production ``main()`` never passes them.
    """
    validate_settings(settings)
    paths = resolve_paths(settings)
    public_url = str(settings["public_url"]).rstrip("/")
    redirect_uri = public_url + CALLBACK_PATH
    canonical_host = urllib.parse.urlsplit(public_url).hostname or ""
    llmux_url = str(settings["llmux_base_url"]).rstrip("/") + USAGE_PATH
    llmux_key = str(settings["llmux_api_key"])
    auth_header_name = str(settings.get("llmux_auth_header") or "Authorization")
    # Absent means "use the default scheme"; an explicit "" means "no scheme at
    # all" (llmux authenticates with a bare x-api-key value, and a prefix there
    # is a malformed credential). `or` would collapse those two cases.
    _raw_auth_scheme = settings.get("llmux_auth_scheme")
    auth_header_scheme = (str(_raw_auth_scheme) if _raw_auth_scheme is not None else "Bearer").strip()

    # Read once at boot and kept as bytes: the comparison below never has to
    # re-encode the secret, and a settings value that is not a usable token is
    # already the empty string here (validate_settings rejected a short one).
    configured_service_token = service_token_bytes(settings)

    sessions = dashboard_auth.SessionStore(paths["auth_db_path"])
    poll_seconds = float(settings.get("sse_poll_seconds") or SSE_POLL_SECONDS)
    heartbeat_seconds = float(settings.get("sse_heartbeat_seconds") or SSE_HEARTBEAT_SECONDS)
    recheck_seconds = float(settings.get("sse_recheck_seconds") or SSE_RECHECK_SECONDS)

    state_cache = dashboard_auth.OAuthStateCache(paths["auth_db_path"])
    if oauth_client is None:
        # The cache makes the state/nonce/PKCE record server-side and one-time:
        # Authlib then requires both the signed cookie marker and this row.
        registry = OAuth(cache=state_cache)
        oauth_app = registry.register(
            "google",
            client_id=str(settings["google_client_id"]),
            client_secret=str(settings["google_client_secret"]),
            server_metadata_url=GOOGLE_METADATA_URL,
            # PKCE S256 plus openid scope; Authlib then adds state and nonce and
            # validates issuer/signature/audience/exp/nonce on the id_token.
            client_kwargs={"scope": GOOGLE_SCOPE, "code_challenge_method": "S256"},
        )
    else:
        oauth_app = oauth_client

    usage_cache: Dict[str, Any] = {"expires_at": 0.0, "payload": None}
    usage_lock = anyio.Lock()
    # Serialises the owner's grant changes with each other and with a non-owner
    # sign-in (from its grant read to its stored session), so a removal's purge
    # sees every session minted under the grant it removes. One process, one
    # event loop (uvicorn.run(app)), so an anyio lock is enough.
    admins_lock = anyio.Lock()

    # ----------------------------------------------------------- auth helpers
    def _granted_admin_emails() -> List[str]:
        """The owner's admin grants as stored now; [] whenever they cannot be read.

        Fail closed: a missing store, a store error or an unreadable row grants
        nothing, so added admins lose admin (and their admin sessions end) until
        the list can be read again. The owner never depends on this read.
        """
        if store is None:
            return []
        try:
            row = store.get_setting(ADMINS_SETTING)
        except Exception as exc:  # noqa: BLE001 - any store failure grants nothing
            # Added admins are signed out by this; say why, without session data.
            print(f"dashboard admins unreadable, only the owner is admin: {exc!r}", file=sys.stderr)
            return []
        value = row.get("value") if isinstance(row, dict) else None
        if row is not None and not (isinstance(value, dict) and isinstance(value.get("added"), list)):
            print("dashboard admins unreadable, only the owner is admin: the saved list has no 'added' list", file=sys.stderr)
            return []
        return dashboard_auth.admin_emails_from_setting(value)

    def _live_session(session_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """The session with its role re-derived for this request, or None.

        A session minted as admin whose grant was revoked is deleted here, so
        the revocation takes effect on the next request (and the next SSE
        recheck) instead of when the cookie expires.
        """
        session = sessions.get(session_id)
        if session is None:
            return None
        # The owner is resolved without reading the store: a slow or broken
        # store must not lock the owner out of the page that repairs it.
        grants = [] if dashboard_auth.is_owner(session.get("email")) else _granted_admin_emails()
        live = dashboard_auth.resolve_session(session, grants)
        if live is None and session_id:
            sessions.revoke(session_id)
        return live

    def _session_for(request: Request) -> Optional[Dict[str, Any]]:
        return _live_session(request.cookies.get(dashboard_auth.SESSION_COOKIE))

    async def require_session(request: Request) -> Dict[str, Any]:
        """Any signed-in account: reads are open to viewers and admins alike."""
        session = await run_in_threadpool(_session_for, request)
        if session is None:
            raise HTTPException(status_code=401, detail="authentication required")
        return session

    async def require_admin(request: Request) -> Dict[str, Any]:
        """Mutations and account-level usage: admins only (403 for a viewer)."""
        session = await require_session(request)
        if session.get("role") != dashboard_auth.ADMIN_ROLE:
            raise HTTPException(status_code=403, detail="admin role required")
        return session

    async def require_owner(request: Request) -> Dict[str, Any]:
        """Granting or revoking admin: the owner only (403 for an added admin too)."""
        session = await require_session(request)
        if session.get("owner") is not True:
            raise HTTPException(status_code=403, detail=OWNER_REQUIRED)
        return session

    def require_mutation_guards(request: Request, session: Dict[str, Any]) -> None:
        origin = request.headers.get("origin")
        if not origin or origin.rstrip("/") != public_url:
            raise HTTPException(status_code=403, detail="request origin is not allowed")
        if not dashboard_auth.SessionStore.verify_csrf(session, request.headers.get(dashboard_auth.CSRF_HEADER)):
            raise HTTPException(status_code=403, detail="invalid CSRF token")

    def require_store() -> Any:
        if store is None:
            raise HTTPException(status_code=503, detail="dashboard store is unavailable")
        return store

    # ----------------------------------------------------------------- routes
    async def login(request: Request) -> Response:
        return await oauth_app.authorize_redirect(request, redirect_uri)

    async def callback(request: Request) -> Response:
        try:
            token = await oauth_app.authorize_access_token(request)
        except OAUTH_VALIDATION_ERRORS:
            # Provider/JOSE text may contain the code or token internals.
            return _json({"error": "google sign-in could not be verified"}, status_code=403)
        # Rotate only this browser's own session; another admin tab stays signed in.
        previous = request.cookies.get(dashboard_auth.SESSION_COOKIE)

        async def sign_in(userinfo: Any, grants: List[str]) -> Dict[str, Any]:
            identity = dashboard_auth.verify_identity(userinfo, grants)
            return await run_in_threadpool(
                sessions.create, identity["email"], identity["sub"], previous, role=identity["role"]
            )

        try:
            userinfo = token.get("userinfo") if isinstance(token, dict) else None
            claimed = str(userinfo.get("email") or "").strip().lower() if isinstance(userinfo, Mapping) else ""
            if dashboard_auth.is_owner(claimed):
                # The owner's sign-in skips the grant read and the lock, like its
                # requests do; verify_identity still checks every claim.
                session = await sign_in(userinfo, [])
            else:
                # From the grant read to the stored row: a removal that landed in
                # between would purge nothing, and a later re-grant would revive
                # the session this sign-in stores.
                async with admins_lock:
                    session = await sign_in(userinfo, await run_in_threadpool(_granted_admin_emails))
        except dashboard_auth.AuthError as exc:
            return _json({"error": exc.safe_message}, status_code=403)
        finally:
            # Provider tokens are not needed past this point and are never stored.
            if isinstance(token, dict):
                token.clear()

        response = Response(status_code=302, headers={"Location": "/", "Cache-Control": "no-store"})
        response.set_cookie(
            dashboard_auth.SESSION_COOKIE,
            session["id"],
            max_age=dashboard_auth.SESSION_TTL_SECONDS,
            path="/",
            httponly=True,
            secure=True,
            samesite="lax",
        )
        return response

    async def logout(request: Request) -> Response:
        session = await require_session(request)
        require_mutation_guards(request, session)
        await run_in_threadpool(sessions.revoke, request.cookies.get(dashboard_auth.SESSION_COOKIE) or "")
        response = Response(status_code=204, headers={"Cache-Control": "no-store"})
        response.delete_cookie(
            dashboard_auth.SESSION_COOKIE, path="/", httponly=True, secure=True, samesite="lax"
        )
        return response

    async def me(request: Request) -> Response:
        session = await require_session(request)
        return _json(
            {
                "email": session["email"],
                # The role in effect now, which a grant or revocation can change
                # after sign-in; ``owner`` alone may change the admin list.
                "role": session["role"],
                "owner": session["owner"],
                "csrf_token": session["csrf_token"],
            }
        )

    async def current_config() -> Dict[str, Any]:
        """Re-read the service config per request: it can change under the server."""
        return await run_in_threadpool(svc.load_config, paths["config_path"])

    async def configured_repositories() -> List[str]:
        """What the daemon polls: config.repositories plus the dashboard-added ones."""
        config = await current_config()
        return await run_in_threadpool(svc.review_repositories, config, store)

    async def list_runs(request: Request) -> Response:
        await require_session(request)
        active_store = require_store()
        status = request.query_params.get("status") or None
        repo = request.query_params.get("repo") or None
        limit = _parse_bounded_int(request.query_params.get("limit"), DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT, "limit")
        offset = _parse_bounded_int(request.query_params.get("offset"), 0, 10**9, "offset")
        if status is not None and status not in RUN_STATUSES:
            raise _bad_request("status is not a known run status")
        if repo is not None and repo not in await configured_repositories():
            raise _bad_request("repo is not a configured repository")

        rows = await run_in_threadpool(
            lambda: active_store.list_runs(status=status, repo=repo, limit=limit, offset=offset)
        )
        return _json(
            {
                "runs": [project_run(row) for row in rows],
                "next_offset": offset + limit if len(rows) == limit else None,
            }
        )

    async def get_run(request: Request) -> Response:
        await require_session(request)
        active_store = require_store()
        run_id = request.path_params["run_id"]
        row = await run_in_threadpool(active_store.get_run, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        return _json({"run": project_run(row)})

    async def list_retries(request: Request) -> Response:
        await require_session(request)
        active_store = require_store()
        lister = getattr(active_store, "list_retries", None)
        if not callable(lister):
            raise HTTPException(status_code=503, detail="retry queue listing is unavailable")
        status = request.query_params.get("status") or None
        repo = request.query_params.get("repo") or None
        limit = _parse_bounded_int(request.query_params.get("limit"), DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT, "limit")
        offset = _parse_bounded_int(request.query_params.get("offset"), 0, 10**9, "offset")
        if status is not None and status not in RETRY_STATUSES:
            raise _bad_request("status is not a known retry status")
        if repo is not None and repo not in await configured_repositories():
            raise _bad_request("repo is not a configured repository")

        rows = await run_in_threadpool(
            lambda: lister(status=status, repo=repo, limit=limit, offset=offset)
        )
        return _json(
            {
                "requests": [project_retry_request(row) for row in rows],
                "next_offset": offset + limit if len(rows) == limit else None,
            }
        )

    # -------------------------------------------- PR authors and their feedback
    # Read-only for every session: authors change their settings by commenting
    # on GitHub (mentions.py), and the daemon writes them.
    async def list_authors(request: Request) -> Response:
        await require_session(request)
        active_store = require_store()
        lister = getattr(active_store, "list_pr_authors", None)
        if not callable(lister):
            raise HTTPException(status_code=503, detail="PR author listing is unavailable")
        limit = max(1, _parse_bounded_int(request.query_params.get("limit"), MAX_LIST_LIMIT, MAX_LIST_LIMIT, "limit"))
        offset = _parse_bounded_int(request.query_params.get("offset"), 0, 10**9, "offset")
        rows = await run_in_threadpool(lambda: lister(limit=limit, offset=offset))
        return _json(
            {
                "authors": [project_author(row) for row in rows],
                "next_offset": offset + limit if len(rows) == limit else None,
            }
        )

    async def list_feedback(request: Request) -> Response:
        await require_session(request)
        active_store = require_store()
        lister = getattr(active_store, "list_feedback", None)
        if not callable(lister):
            raise HTTPException(status_code=503, detail="feedback listing is unavailable")
        limit = max(1, _parse_bounded_int(request.query_params.get("limit"), DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT, "limit"))
        before_id = _parse_row_id(request.query_params.get("before_id"), "before_id")
        after_id = _parse_row_id(request.query_params.get("after_id"), "after_id")
        if before_id is not None and after_id is not None:
            raise _bad_request("give before_id or after_id, not both")
        # Paged by row id, not offset: a key names the same place however much
        # feedback arrives between two pages. ``after_id`` answers oldest first,
        # so its last row is the edge in either direction; ``has_more`` asks the
        # store for one row past it. ``offset`` is not read: no offset-paged UI
        # ever shipped, so there is no older client to page for.
        key = "after_id" if after_id is not None else "before_id"

        def page() -> Tuple[List[Dict[str, Any]], bool]:
            rows = lister(limit=limit, before_id=before_id, after_id=after_id)
            more = len(rows) == limit and bool(lister(limit=1, **{key: rows[-1]["id"]}))
            return rows, more

        rows, has_more = await run_in_threadpool(page)
        return _json({"feedback": [project_feedback(row) for row in rows], "has_more": has_more})

    async def retry_run(request: Request) -> Response:
        session = await require_admin(request)
        require_mutation_guards(request, session)
        active_store = require_store()

        content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise _bad_request("content-type must be application/json")
        raw = await request.body()
        try:
            body = json.loads(raw or b"")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise _bad_request("body must be a JSON object") from None
        if not isinstance(body, dict):
            raise _bad_request("body must be a JSON object")
        head_sha = body.get("head_sha")
        if not isinstance(head_sha, str) or not head_sha.strip():
            raise _bad_request("head_sha is required")
        head_sha = head_sha.strip()
        # Which agents run again: only the ones the failed attempt kept no result
        # for ("failed"), or every agent ("all"). Defaulted from the run below.
        scope = body.get("scope")
        if scope is not None and (not isinstance(scope, str) or scope not in RETRY_SCOPES):
            raise _bad_request(f"scope must be one of: {', '.join(RETRY_SCOPES)}")
        # An operator-chosen ladder for this retry only (trace.md scenario C).
        # Validated by the daemon's own rules before anything is queued.
        options: Dict[str, Any] = {}
        if body.get("model_policy") is not None:
            try:
                chosen = svc.validate_model_policy(body.get("model_policy"))
            except ValueError as exc:
                raise _bad_request(f"model_policy: {exc}")
            chosen.pop("source", None)
            await _refuse_unusable(chosen)
            options["model_policy"] = chosen

        run_id = request.path_params["run_id"]
        run = await run_in_threadpool(active_store.get_run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if str(run.get("status")) not in RETRYABLE_RUN_STATUSES:
            raise HTTPException(status_code=409, detail="only failed, interrupted or stale runs can be retried")
        if str(run.get("head_sha")) != head_sha:
            raise HTTPException(status_code=409, detail="head has moved since the failed attempt")
        # A terminal attempt never changes, so "nothing kept" here is final, not a race.
        kept = project_agent_ids(run.get("reusable_agents"))
        if scope is None:
            scope = RETRY_SCOPE_FAILED if kept else RETRY_SCOPE_ALL
        elif scope == RETRY_SCOPE_FAILED and not kept:
            raise HTTPException(
                status_code=409,
                detail="this attempt kept no agent results to reuse; retry with scope all",
            )
        options[RETRY_SCOPE_OPTION] = scope
        if dashboard_execution is not None and dashboard_execution.posting_outcome_unknown(run):
            # The review POST may already have landed; retrying would duplicate it.
            raise HTTPException(
                status_code=409,
                detail="the previous attempt's GitHub review outcome is unknown; reconcile before retrying",
            )

        config = await current_config()
        repo = str(run.get("repo") or "")
        listed = await run_in_threadpool(svc.review_repositories, config, active_store)
        if repo.lower() not in {name.lower() for name in listed}:
            raise HTTPException(status_code=409, detail="run repository is not configured for review")
        pr_number = _optional_int(run.get("pr"))
        if pr_number is None or pr_number <= 0:
            raise HTTPException(status_code=409, detail="run has no pull request number")

        gh_bin = str(config.get("gh_bin") or "gh")
        factory = github_client_factory or (lambda name: svc.GitHubClient(name, gh_bin=gh_bin))
        try:
            pr = await run_in_threadpool(lambda: factory(repo).get_pr(pr_number))
        except svc.ServiceError:
            # Upstream text can carry tokens and URLs; report the class only.
            raise HTTPException(status_code=502, detail="could not revalidate the pull request on GitHub") from None
        if not isinstance(pr, dict) or not pr:
            raise HTTPException(status_code=502, detail="could not revalidate the pull request on GitHub")
        if str(pr.get("state") or "") != "open":
            raise HTTPException(status_code=409, detail="pull request is not open")
        if pr.get("draft"):
            raise HTTPException(status_code=409, detail="pull request is a draft")
        if str((pr.get("head") or {}).get("sha") or "") != head_sha:
            raise HTTPException(status_code=409, detail="head has moved since the failed attempt")

        try:
            queued = await run_in_threadpool(
                lambda: active_store.enqueue_retry(
                    repo, pr_number, head_sha, session["email"], retry_of=run_id, options=options
                )
            )
        except ValueError:
            raise _bad_request("retry request was rejected by the store") from None
        return _json({"request": project_retry_request(queued)}, status_code=202)

    # --------------------------------------------------------------- retrigger
    # The Retrigger badge on a PR comment links #retrigger/{owner}/{name}/{pr}.
    # The page reads the PR's current head and its latest attempt (any session)
    # and lets an admin queue a rereview of that head: every agent, a second
    # review posted on purpose, the visual outline refreshed.
    def _pr_number_param(raw: Any) -> int:
        try:
            number = int(str(raw))
        except (TypeError, ValueError):
            raise _bad_request("pr must be a positive integer") from None
        if number <= 0:
            raise _bad_request("pr must be a positive integer")
        return number

    async def _configured_spelling(config: Dict[str, Any], repo: str, active_store: Any) -> str:
        """The configured spelling of ``repo`` (GitHub names are case-insensitive), else 409.

        Checked before any GitHub call: the name is caller-controlled here,
        unlike the retry route, where it comes from the attempt row.
        """
        listed = await run_in_threadpool(svc.review_repositories, config, active_store)
        match = next((name for name in listed if name.lower() == repo.lower()), None)
        if match is None:
            raise HTTPException(status_code=409, detail="repository is not configured for review")
        return match

    # (repo, pr) -> (monotonic time the read STARTED, time it COMPLETED, PR
    # payload): successful reads only. The start orders competing reads (a slow
    # GET finishing after the POST's live read must not restore an older head);
    # the completion measures freshness (a read slower than the window is still
    # fresh for the waiters behind it).
    retrigger_pr_cache: Dict[Tuple[str, int], Tuple[float, float, Dict[str, Any]]] = {}
    # (repo, pr) -> monotonic time of the last failed GET read: answered 502
    # without GitHub for RETRIGGER_PR_CACHE_SECONDS, which also makes waiters
    # behind a failed in-flight read share its outcome instead of retrying.
    retrigger_pr_failures: Dict[Tuple[str, int], float] = {}
    # (repo, pr) -> start time of the newest successful read stored: the ordering
    # watermark, kept for RETRIGGER_PR_ORDER_SECONDS even after the payload expires.
    retrigger_pr_newest: Dict[Tuple[str, int], float] = {}
    # Start times of the GET's GitHub reads in the last minute (the budget).
    retrigger_pr_reads: List[float] = []
    # One in-flight GitHub read per PR for the page's GET: tabs refresh on the
    # same stream event, so concurrent cold GETs wait for one read instead of
    # each making one. Held inside the worker thread that does the read.
    retrigger_pr_locks: Dict[Tuple[str, int], threading.Lock] = {}
    retrigger_pr_guard = threading.Lock()
    retrigger_pr_read_slots = threading.BoundedSemaphore(max(1, int(RETRIGGER_PR_READ_SLOTS)))

    def _pr_client_factory(config: Dict[str, Any]) -> Callable[[str], Any]:
        gh_bin = str(config.get("gh_bin") or "gh")
        return github_client_factory or (lambda name: svc.GitHubClient(name, gh_bin=gh_bin))

    def _fresh_pull_request(key: Tuple[str, int]) -> Optional[Dict[str, Any]]:
        cached = retrigger_pr_cache.get(key)
        if cached is not None and time.monotonic() - cached[1] < RETRIGGER_PR_CACHE_SECONDS:
            return dict(cached[2])
        return None

    def _prune_pull_request_reads(now: float) -> None:
        """Drop expired cache and failure entries, and every idle lock not backing one
        (call with retrigger_pr_guard held). Keeps all three bounded by recent use."""
        for stale in [k for k, (_, done_at, _) in retrigger_pr_cache.items() if now - done_at >= RETRIGGER_PR_CACHE_SECONDS]:
            del retrigger_pr_cache[stale]
        for stale in [k for k, failed_at in retrigger_pr_failures.items() if now - failed_at >= RETRIGGER_PR_CACHE_SECONDS]:
            del retrigger_pr_failures[stale]
        for stale in [k for k, started in retrigger_pr_newest.items() if now - started >= RETRIGGER_PR_ORDER_SECONDS]:
            del retrigger_pr_newest[stale]
        for idle in [k for k, lock in retrigger_pr_locks.items() if not lock.locked()]:
            if idle not in retrigger_pr_cache and idle not in retrigger_pr_failures:
                del retrigger_pr_locks[idle]

    def _remember_pull_request(key: Tuple[str, int], pr: Any, started_at: float) -> bool:
        """Cache ``pr`` unless a later-started read already landed; True when stored."""
        if not isinstance(pr, dict) or not pr:
            return False
        with retrigger_pr_guard:
            now = time.monotonic()
            _prune_pull_request_reads(now)
            newest = retrigger_pr_newest.get(key)
            if newest is not None and newest > started_at:
                return False  # a later read already landed, even if its payload expired since
            retrigger_pr_cache[key] = (started_at, now, pr)
            retrigger_pr_newest[key] = started_at
            retrigger_pr_failures.pop(key, None)
            return True

    def _remember_failed_read(key: Tuple[str, int], started_at: float) -> bool:
        """Remember a failed read unless a later-started read already succeeded; True when remembered."""
        with retrigger_pr_guard:
            newest = retrigger_pr_newest.get(key)
            if newest is not None and newest > started_at:
                return False  # a later read (the POST's) already succeeded
            retrigger_pr_failures[key] = time.monotonic()
            return True

    def _timed_read(factory: Callable[[str], Any], repo: str, pr_number: int) -> Tuple[float, Any]:
        """One GitHub read, in a worker thread: (when it actually started, PR or None).

        The start is taken here, not when the read was queued for a worker, so
        a read delayed by a busy thread pool still orders after the ones that
        really started before it.
        """
        started_at = time.monotonic()
        try:
            return started_at, factory(repo).get_pr(pr_number)
        except svc.ServiceError:
            return started_at, None

    def _budgeted_read(factory: Callable[[str], Any], repo: str, pr_number: int) -> Tuple[float, Any]:
        """The GET's read, in the worker: charge the minute budget right before the
        GitHub call (atomically), so neither the slot wait nor the shared thread
        pool's queue can let a read outlive its charge. The POST does not use this."""
        with retrigger_pr_guard:
            now = time.monotonic()
            window = RETRIGGER_GITHUB_BUDGET_WINDOW_SECONDS
            retrigger_pr_reads[:] = [at for at in retrigger_pr_reads if now - at < window]
            if len(retrigger_pr_reads) >= RETRIGGER_GITHUB_READS_PER_MINUTE:
                raise HTTPException(
                    status_code=429, detail="too many GitHub reads from the Retrigger page; try again in a minute"
                )
            retrigger_pr_reads.append(now)
        return _timed_read(factory, repo, pr_number)

    def _checked_pull_request(pr: Any) -> Dict[str, Any]:
        if not isinstance(pr, dict) or not pr:
            raise HTTPException(status_code=502, detail="could not read the pull request on GitHub")
        return dict(pr)

    async def _live_pull_request(config: Dict[str, Any], repo: str, pr_number: int) -> Dict[str, Any]:
        """A live GitHub read (the POST): never cached, and it refreshes what the GET shows."""
        factory = _pr_client_factory(config)
        # Upstream text can carry tokens and URLs; a failure reports the class only.
        started_at, pr = await run_in_threadpool(_timed_read, factory, repo, pr_number)
        checked = _checked_pull_request(pr)
        _remember_pull_request((repo.lower(), pr_number), checked, started_at)
        return checked

    async def _cached_pull_request(config: Dict[str, Any], repo: str, pr_number: int) -> Dict[str, Any]:
        """The page's read: per PR at most once per RETRIGGER_PR_CACHE_SECONDS (success or
        failure), and at most RETRIGGER_GITHUB_READS_PER_MINUTE GitHub reads overall.

        Concurrent GETs of one PR share one read: the first takes the PR's lock;
        the others wait on the event loop (not in a worker thread) and then use
        its outcome, a cached payload or a remembered failure. A read that a
        later one superseded never answers: the newer cached read does, or,
        when that has expired too, one more read.
        """
        key = (repo.lower(), pr_number)
        unreadable = "could not read the pull request on GitHub"
        rereads = 0
        while True:
            hit = _fresh_pull_request(key)
            if hit is not None:
                return hit
            with retrigger_pr_guard:
                now = time.monotonic()
                _prune_pull_request_reads(now)
                failed_at = retrigger_pr_failures.get(key)
                if failed_at is not None and now - failed_at < RETRIGGER_PR_CACHE_SECONDS:
                    raise HTTPException(status_code=502, detail=unreadable)
                lock = retrigger_pr_locks.setdefault(key, threading.Lock())
                owned = lock.acquire(blocking=False)
            if not owned:
                await anyio.sleep(RETRIGGER_PR_WAIT_POLL_SECONDS)
                continue
            try:
                factory = _pr_client_factory(config)
                # A worker slot for this feature: waits on the event loop, never in a worker.
                while not retrigger_pr_read_slots.acquire(blocking=False):
                    await anyio.sleep(RETRIGGER_PR_WAIT_POLL_SECONDS)
                try:
                    # The minute budget is charged in the worker, right before
                    # the GitHub call (_budgeted_read).
                    started_at, pr = await run_in_threadpool(_budgeted_read, factory, repo, pr_number)
                finally:
                    retrigger_pr_read_slots.release()
                if not isinstance(pr, dict) or not pr:
                    remembered = _remember_failed_read(key, started_at)
                    newer = _fresh_pull_request(key)
                    if newer is not None:
                        return newer  # a later read (the POST's) succeeded meanwhile
                    if remembered:
                        # Upstream text can carry tokens and URLs; report the class only.
                        raise HTTPException(status_code=502, detail=unreadable)
                else:
                    stored = _remember_pull_request(key, pr, started_at)
                    newer = _fresh_pull_request(key)
                    if newer is not None:
                        return newer
                    if stored:
                        return dict(pr)
                # Superseded (a later read already succeeded, and its payload has
                # expired since): never answer with the older result, success or
                # failure; read again (a new start), a bounded number of times.
                if rereads >= RETRIGGER_PR_MAX_REREADS:
                    raise HTTPException(
                        status_code=503, detail="the pull request changed while it was being read; reload the page"
                    )
                rereads += 1
            finally:
                lock.release()

    def _require_retrigger_capability(active_store: Any) -> None:
        """Both PR-scoped queries the retrigger routes depend on, or 503.

        Round 2 MUST-FIX: a store without ``active_activity_for_pr`` /
        ``latest_attempt_for_head`` used to fall back to bounded, repo-wide
        scans (paging ``list_retries``/``list_runs`` and filtering by PR in
        Python, and taking ``rows[-1]`` of a bounded ``attempts_for`` window) -
        exactly the staleness and pagination bugs those two methods exist to
        fix. A store that lacks them is unavailable for this feature, not
        quietly degraded back to the bug.
        """
        if not callable(getattr(active_store, "active_activity_for_pr", None)) or not callable(
            getattr(active_store, "latest_attempt_for_head", None)
        ):
            raise HTTPException(status_code=503, detail="retrigger is unavailable on this store")

    def _latest_attempt_for_head(active_store: Any, repo: str, pr_number: int, head_sha: str) -> Optional[Dict[str, Any]]:
        """The newest attempt for (repo, pr, head), or None."""
        if not head_sha:
            return None
        return active_store.latest_attempt_for_head(repo, pr_number, head_sha)

    def _utc_now_iso() -> str:
        # To the microsecond: the daemon orders a ledger review against it.
        return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")

    def _seen_at(value: Any) -> str:
        """The page's snapshot time (``observed_at`` from its GET), clamped to now:
        a future value would switch the daemon's ledger check off."""
        if not isinstance(value, str) or not value.strip():
            raise _bad_request("observed_at is required (the observed_at of the page's GET); reload the page")
        try:
            parsed = dt.datetime.fromisoformat(value.strip())
        except ValueError:
            parsed = None
        if parsed is None or parsed.tzinfo is None:
            raise _bad_request("observed_at must be an ISO 8601 time with an offset")
        now = dt.datetime.now(dt.timezone.utc)
        return min(parsed.astimezone(dt.timezone.utc), now).isoformat(timespec="microseconds")

    async def retrigger_info(request: Request) -> Response:
        session = await require_session(request)
        active_store = require_store()
        _require_retrigger_capability(active_store)
        pr_number = _pr_number_param(request.path_params["pr"])
        config = await current_config()
        repo = await _configured_spelling(
            config, f"{request.path_params['owner']}/{request.path_params['name']}", active_store
        )
        pr = await _cached_pull_request(config, repo, pr_number)
        head_sha = str((pr.get("head") or {}).get("sha") or "")
        # Taken before the store reads: a review recorded after it is one this
        # page could not show (the POST sends it back as the rereview's cutoff).
        observed_at = _utc_now_iso()
        latest = await run_in_threadpool(_latest_attempt_for_head, active_store, repo, pr_number, head_sha)
        active_request, running = await run_in_threadpool(active_store.active_activity_for_pr, repo, pr_number)
        return _json(
            {
                "repo": repo,
                "pr": pr_number,
                "title": sanitize_text(pr.get("title"), 300),
                "url": sanitize_text(pr.get("html_url"), 512),
                "state": sanitize_text(pr.get("state"), 32),
                "draft": bool(pr.get("draft")),
                "head_sha": sanitize_text(head_sha, 64),
                "latest_run": project_run(latest) if latest else None,
                "active_request": project_retry_request(active_request) if active_request else None,
                "running_run_id": sanitize_text(running.get("id"), 128) if running else None,
                "busy": bool(active_request or running),
                "can_retrigger": session.get("role") == dashboard_auth.ADMIN_ROLE,
                "observed_at": observed_at,
            }
        )

    async def retrigger(request: Request) -> Response:
        session = await require_admin(request)
        require_mutation_guards(request, session)
        active_store = require_store()
        _require_retrigger_capability(active_store)
        body = await _read_json_object(request)
        repo = body.get("repo")
        if not isinstance(repo, str) or repo.count("/") != 1 or not all(repo.split("/")):
            raise _bad_request("repo must be owner/name")
        pr_number = _optional_int(body.get("pr"))
        if pr_number is None or pr_number <= 0:
            raise _bad_request("pr must be a positive integer")
        head_sha = body.get("head_sha")
        if not isinstance(head_sha, str) or not head_sha.strip():
            raise _bad_request("head_sha is required")
        head_sha = head_sha.strip()
        # The attempt the page displayed for this head (None: it showed none).
        # The rereview is made against it; a newer one means the page is stale.
        if "latest_run_id" not in body:
            raise _bad_request("latest_run_id is required (null when the page showed no attempt)")
        displayed_run_id = body.get("latest_run_id")
        if displayed_run_id is not None and not (isinstance(displayed_run_id, str) and displayed_run_id.strip()):
            raise _bad_request("latest_run_id must be an attempt id or null")
        # When the page read that snapshot: the daemon's late check refuses a
        # review the ledger records after it (one this page could not show).
        seen_at = _seen_at(body.get("observed_at"))

        config = await current_config()
        repo = await _configured_spelling(config, repo, active_store)
        # One review of a PR at a time, whatever its head: a queued request or
        # a running attempt is refused before GitHub is asked anything.
        active_request, running = await run_in_threadpool(active_store.active_activity_for_pr, repo, pr_number)
        if active_request is not None or running is not None:
            raise HTTPException(status_code=409, detail="a review of this pull request is already queued or running")

        pr = await _live_pull_request(config, repo, pr_number)
        if str(pr.get("state") or "") != "open":
            raise HTTPException(status_code=409, detail="pull request is not open")
        if pr.get("draft"):
            raise HTTPException(status_code=409, detail="pull request is a draft")
        if str((pr.get("head") or {}).get("sha") or "") != head_sha:
            raise HTTPException(status_code=409, detail="head has moved since the page was shown; reload it")
        latest = await run_in_threadpool(_latest_attempt_for_head, active_store, repo, pr_number, head_sha)
        if (str(latest["id"]) if latest else None) != displayed_run_id:
            # An attempt of this head finished since the page was shown (a pool
            # review that ran meanwhile): that is the fresh review; never rebase
            # the request onto it, or the late check could not tell it apart.
            raise HTTPException(
                status_code=409, detail="a newer attempt of this head exists since the page was shown; reload it"
            )
        if dashboard_execution is not None and dashboard_execution.posting_outcome_unknown(latest):
            # The review POST may already have landed; a rereview would stack on it.
            raise HTTPException(
                status_code=409,
                detail="the previous attempt's GitHub review outcome is unknown; reconcile before retrying",
            )

        options: Dict[str, Any] = {
            RETRY_KIND_OPTION: RETRY_KIND_REREVIEW,
            RETRY_SCOPE_OPTION: RETRY_SCOPE_ALL,
            RETRY_SEEN_AT_OPTION: seen_at,
        }
        try:
            queued = await run_in_threadpool(
                lambda: active_store.enqueue_retry(
                    repo,
                    pr_number,
                    head_sha,
                    session["email"],
                    retry_of=str(latest["id"]) if latest else None,
                    options=options,
                )
            )
        except PrBusyError as exc:
            # The store's own admission check, inside its write transaction:
            # the closer of a race the pre-check above (active_activity_for_pr)
            # cannot fully cover between two overlapping POSTs.
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except ValueError:
            raise _bad_request("retrigger request was rejected by the store") from None
        return _json({"request": project_retry_request(queued)}, status_code=202)

    # ------------------------------------------------------------ model policy
    async def _read_json_object(request: Request) -> Dict[str, Any]:
        content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise _bad_request("content-type must be application/json")
        raw = await request.body()
        try:
            body = json.loads(raw or b"")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise _bad_request("body must be a JSON object") from None
        if not isinstance(body, dict):
            raise _bad_request("body must be a JSON object")
        return body

    async def _config_policy_or_error() -> tuple:
        """The config's policy, or (None, reason) when the config cannot say."""
        try:
            config = await current_config()
            return svc.config_model_policy(config), None
        except (svc.ServiceError, ValueError) as exc:
            return None, sanitize_text(f"config model policy unavailable: {exc}", 300)

    async def _policy_payload(active_store: Any) -> Dict[str, Any]:
        config_policy, config_error = await _config_policy_or_error()
        config_projected = project_model_policy(config_policy) if config_policy else None
        stored = None
        if active_store is not None:
            stored = await run_in_threadpool(active_store.get_setting, MODEL_POLICY_SETTING)
        if isinstance(stored, dict) and isinstance(stored.get("value"), dict):
            try:
                policy = svc.validate_model_policy(stored["value"])
            except ValueError:
                policy = None
            if policy is not None:
                policy["source"] = "dashboard"
                payload = {
                    "policy": project_model_policy(policy),
                    "source": "dashboard",
                    "updated_at": sanitize_text(stored.get("updated_at"), 64) or None,
                    "updated_by": sanitize_text(stored.get("updated_by"), 254) or None,
                    "config_policy": config_projected,
                }
                if config_error:
                    payload["error"] = config_error
                return payload
        payload = {
            "policy": config_projected,
            "source": (config_policy.get("source") if config_policy else None) or "config",
            "updated_at": None,
            "updated_by": None,
            "config_policy": config_projected,
        }
        if config_error:
            payload["error"] = config_error
        return payload

    async def _refuse_unusable(policy: Dict[str, Any]) -> None:
        """Cross-check the chosen ids against the catalog's own usability verdict.

        The id prefix rule lives in the daemon; the catalog additionally knows the
        provider group, so an Anthropic-leg id that does not start with ``claude``
        is still refused here rather than stored and failing every review.
        """
        catalog = await _cached_catalog()
        if not catalog.get("available"):
            return
        rows = catalog.get("models") or []
        # Aliases resolve to the row's leg on the proxy; a verdict keyed on the
        # id alone would let ``opus`` through as an unknown, usable id. Ids are
        # filled first so an alias can never shadow another row's id.
        verdicts: Dict[str, Dict[str, Any]] = {row["id"]: row for row in rows}
        for row in rows:
            for name in row.get("aliases") or []:
                verdicts.setdefault(name, row)
        for model_id in [policy["model"]] + list(policy.get("fallback_models") or []):
            row = verdicts.get(model_id)
            if row is not None and not row.get("usable"):
                raise _bad_request(f"{model_id!r}: {row.get('reason') or 'not usable'}")

    async def get_model_policy(request: Request) -> Response:
        await require_session(request)
        # The config answer needs no store; a store outage must not hide it.
        return _json(await _policy_payload(store))

    async def put_model_policy(request: Request) -> Response:
        session = await require_admin(request)
        require_mutation_guards(request, session)
        active_store = require_store()
        body = await _read_json_object(request)
        try:
            # The validator's own unknown-field check runs on the raw body, so a
            # misspelt key is a 400, not a silently reset ladder.
            policy = svc.validate_model_policy(body)
        except ValueError as exc:
            raise _bad_request(str(exc))
        policy.pop("source", None)
        await _refuse_unusable(policy)
        try:
            await run_in_threadpool(active_store.set_setting, MODEL_POLICY_SETTING, policy, session["email"])
        except ValueError:
            raise _bad_request("model policy was rejected by the store") from None
        return _json(await _policy_payload(active_store))

    async def delete_model_policy(request: Request) -> Response:
        session = await require_admin(request)
        require_mutation_guards(request, session)
        active_store = require_store()
        await run_in_threadpool(active_store.delete_setting, MODEL_POLICY_SETTING, session["email"])
        return _json(await _policy_payload(active_store))

    # ---------------------------------------------------------- agent settings
    # Per-agent prompt, required flag and model (store key ``agent:<id>``). Every
    # session reads them; only the admin writes. The prompt is operator content
    # that is shown and saved back verbatim, so it is bounded but never passed
    # through sanitize_text: redacting a path or token-shaped word would corrupt
    # the prompt on the next save.
    def _agent_rows(
        config: Dict[str, Any], stored: Dict[str, Any], repositories: List[str], file_texts: Dict[str, Optional[str]]
    ) -> List[Dict[str, Any]]:
        config_dir = paths["config_path"].parent
        selected = {
            repo: {str(agent.get("id")) for agent in svc.agents_for_repo(config, repo)}
            for repo in repositories
        }
        rows = []
        for agent_id, agent in _enabled_agents(config).items():
            row_stored = stored.get(agent_id)
            config_required = agent.get("required") is True
            # The text a reconcile just decided with (file_texts), so the row's
            # default_sha and its prompt describe the same file; else read now.
            if agent_id in file_texts:
                file_text = file_texts[agent_id]
            else:
                file_text = svc.read_agent_prompt_file(agent, config_dir)
            override: Dict[str, Any] = {}
            error = None
            if isinstance(row_stored, dict):
                try:
                    override = svc.validate_agent_document(row_stored.get("value"))
                except ValueError:
                    # The daemon ignores it too (svc.resolve_agent_settings).
                    error = "the saved dashboard setting is invalid and ignored"
            # The edit a changed prompt file replaced (svc.reconcile_agent_document):
            # shown beside the file prompt now in use, for compare, rollback or discard.
            kept = override.get("superseded")
            row: Dict[str, Any] = {
                "id": agent_id,
                "focus": sanitize_text(svc.agent_focus(agent), 120),
                "repos": [repo for repo, ids in selected.items() if agent_id in ids],
                "required": override.get("required", config_required),
                "required_source": "dashboard" if "required" in override else "config",
                "config_required": config_required,
                "prompt": override.get("prompt"),
                "prompt_source": "dashboard" if "prompt" in override else "file",
                # The prompt file a save from this page is written against: the
                # client echoes it (expected_default_sha), so a default that
                # changed under an open editor is refused, not silently adopted.
                # A dashboard prompt was written against its base: a reconcile
                # that could not run this time (a busy store) must not present
                # it as an edit of a newer file.
                "default_sha": override["prompt_base_sha"] if override.get("prompt_base_sha") and "prompt" in override
                else None if file_text is None else svc.prompt_sha(file_text),
                "prompt_superseded": None if not kept else {
                    "prompt": kept["prompt"],
                    # Names this kept edit for Discard B / Rollback to B / Reset.
                    "sha": svc.prompt_sha(kept["prompt"]),
                    "saved_at": sanitize_text(kept.get("saved_at"), 64) or None,
                    "saved_by": sanitize_text(kept.get("saved_by"), 254) or None,
                    "superseded_at": sanitize_text(kept.get("superseded_at"), 64) or None,
                },
                # None: the agent runs the model policy (svc.agent_model_ladder).
                "model": override.get("model"),
                "model_source": "dashboard" if "model" in override else "default",
                "updated_at": None,
                "updated_by": None,
            }
            if isinstance(row_stored, dict):
                row["updated_at"] = sanitize_text(row_stored.get("updated_at"), 64) or None
                row["updated_by"] = sanitize_text(row_stored.get("updated_by"), 254) or None
            if "prompt" not in override:
                text = file_text
                if text is None:
                    error = "prompt file is unreadable"
                if text is not None and len(text) > svc.MAX_AGENT_PROMPT_CHARS:
                    text = text[: svc.MAX_AGENT_PROMPT_CHARS]
                    error = f"prompt file exceeds {svc.MAX_AGENT_PROMPT_CHARS} characters; showing the first {svc.MAX_AGENT_PROMPT_CHARS}"
                row["prompt"] = text
            if error:
                row["error"] = error
            rows.append(row)
        return rows

    class _DefaultChanged(Exception):
        """A prompt save whose expected_default_sha is not the default current at commit (409)."""

    def _current_prompt_text(agent_id: str) -> Optional[str]:
        """The agent's prompt file text under the config as it is now, for a caller
        inside a write transaction: a deploy can move ``prompt_path`` while a request
        waits for the lock, so the request's own config may name the old file. None
        when the config, the agent or the file cannot be read."""
        try:
            agent = _enabled_agents(svc.load_config(paths["config_path"])).get(agent_id)
        except Exception:  # noqa: BLE001 - a config that cannot be read now is "cannot be read", never a 500
            return None
        return svc.read_agent_prompt_file(agent, paths["config_path"].parent) if agent else None

    def _current_prompt_file_sha(agent_id: str) -> Optional[str]:
        """The base a prompt saved or rolled back now is written against (_current_prompt_text)."""
        text = _current_prompt_text(agent_id)
        return None if text is None else svc.prompt_sha(text)

    def _reconciled_agent_settings(
        config: Dict[str, Any], active_store: Any, only: Optional[str] = None
    ) -> Tuple[Dict[str, Any], Dict[str, Optional[str]]]:
        """Every enabled agent's stored row, after a prompt file that changed since the
        dashboard edit replaced the edit (once, the same store transaction the daemon
        uses), so the [!] shows before the next review; and the file text each
        reconcile read. Only an agent with a stored prompt is reconciled; a file or
        store problem means no replacement this time. ``only``: just that agent
        (the row a mutation answers with)."""
        agents = [agent_id for agent_id in _enabled_agents(config) if only is None or agent_id == only]
        stored = _stored_agent_settings(active_store, agents)
        seen: Dict[str, Optional[str]] = {}

        def reader(agent_id: str) -> Optional[str]:
            # The file the config names at the write, like a save or a Rollback.
            seen[agent_id] = _current_prompt_text(agent_id)
            return seen[agent_id]

        for agent_id in agents:
            row = stored.get(agent_id)
            value = row.get("value") if isinstance(row, dict) else None
            if not isinstance(value, dict) or not isinstance(value.get("prompt"), str):
                continue
            try:
                # The file is read inside the store's write transaction, never before it.
                stored[agent_id], _action = active_store.reconcile_agent_setting(
                    agent_id, lambda agent_id=agent_id: reader(agent_id), svc.reconcile_agent_document
                )
            except Exception as exc:  # noqa: BLE001 - the row as read still renders
                seen.pop(agent_id, None)
                print(f"dashboard agent {agent_id}: prompt not reconciled with its file: {exc!r}", file=sys.stderr)
        return stored, seen

    def _enabled_agents(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """The configured (enabled) agents an override can address, by id."""
        return {
            str(agent["id"]): agent
            for agent in config.get("agents") or []
            if isinstance(agent, dict) and isinstance(agent.get("id"), str) and AGENT_ID_PATTERN.fullmatch(agent["id"])
        }

    def _stored_agent_settings(active_store: Any, agent_ids: Iterable[str]) -> Dict[str, Any]:
        if active_store is None:
            return {}
        return {agent_id: active_store.get_agent_setting(agent_id) for agent_id in agent_ids}

    async def _agent_row(config: Dict[str, Any], agent_id: str, active_store: Any) -> Dict[str, Any]:
        def build() -> Dict[str, Any]:
            # Reconciled like a GET row: a mutation's answer never pairs a prompt
            # the file has replaced with the new file's default_sha.
            stored, file_texts = _reconciled_agent_settings(config, active_store, only=agent_id)
            rows = _agent_rows(config, stored, svc.review_repositories(config, active_store), file_texts)
            return next(row for row in rows if row["id"] == agent_id)

        return await run_in_threadpool(build)

    async def _configured_agent(request: Request) -> Tuple[Dict[str, Any], str]:
        """The current config and the addressed agent id; 404 unless it is an enabled configured agent."""
        try:
            config = await current_config()
        except svc.ServiceError:
            raise HTTPException(status_code=503, detail="the service config is unavailable") from None
        agent_id = request.path_params["agent_id"]
        if agent_id not in _enabled_agents(config):
            raise HTTPException(status_code=404, detail="agent not found")
        return config, agent_id

    async def get_agent_settings(request: Request) -> Response:
        await require_session(request)
        try:
            config = await current_config()
        except svc.ServiceError as exc:
            return _json({
                "agents": [],
                "max_prompt_chars": svc.MAX_AGENT_PROMPT_CHARS,
                "error": sanitize_text(f"service config unavailable: {exc}", 300),
            })

        def build() -> List[Dict[str, Any]]:
            # The config answer needs no store; a store outage shows the defaults.
            stored, file_texts = _reconciled_agent_settings(config, store)
            return _agent_rows(config, stored, svc.review_repositories(config, store), file_texts)

        rows = await run_in_threadpool(build)
        return _json({"agents": rows, "max_prompt_chars": svc.MAX_AGENT_PROMPT_CHARS})

    async def put_agent_setting(request: Request) -> Response:
        session = await require_admin(request)
        require_mutation_guards(request, session)
        active_store = require_store()
        config, agent_id = await _configured_agent(request)
        body = await _read_json_object(request)
        # The prompt file the client's text was written against (its row's
        # default_sha): a precondition, never stored as given.
        expected_default = body.pop("expected_default_sha", None)
        if expected_default is not None and not (isinstance(expected_default, str) and svc.SHA256_HEX_PATTERN.fullmatch(expected_default)):
            raise _bad_request("expected_default_sha must be a sha256 hex digest")
        unknown = sorted(str(key) for key in body if key not in svc.AGENT_OVERRIDE_FIELDS)
        if unknown:
            raise _bad_request("unknown fields: " + ", ".join(unknown))
        try:
            override = svc.validate_agent_override(body)
        except ValueError as exc:
            raise _bad_request(str(exc))
        if override.get("model"):
            # Same catalog verdict as the model policy: an alias of an unusable
            # leg (``opus``) passes the id rules but would fail every attempt.
            await _refuse_unusable({"model": override["model"]})
        # A merge inside one store transaction: a field the body omits keeps its
        # override (even if another tab saves it concurrently); null clears it,
        # and a document left empty is deleted. A saved prompt records the file
        # it was written against (its base) and who saved it when, and replaces
        # the edit a changed file kept; the server-managed fields come from
        # here, never from the body.
        def prepare(patch: Dict[str, Any], stored: Optional[Dict[str, Any]]) -> Dict[str, Any]:
            # Runs inside the store's write transaction: the config and file read
            # and the expected_default_sha check see the default current at commit.
            if "prompt" not in patch:
                value = stored["value"] if stored else {}
                if isinstance(value.get("prompt"), str) and not value.get("prompt_saved_at"):
                    # A prompt saved before save stamps existed: who saved it, and
                    # when, is the row as it stands; pin that before this save of
                    # another field moves the row's stamps.
                    patch["prompt_saved_at"], patch["prompt_saved_by"] = stored["updated_at"], stored["updated_by"]
                return patch
            saving = patch["prompt"] is not None
            base = _current_prompt_file_sha(agent_id) if saving else None
            if saving and expected_default is not None and expected_default != base:
                raise _DefaultChanged("the default prompt cannot be read right now; nothing was saved, try again" if base is None
                                      else "the default prompt changed while you were editing; reload to compare it with your text")
            patch["prompt_base_sha"] = base
            patch["prompt_saved_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds") if saving else None
            patch["prompt_saved_by"] = session["email"] if saving else None
            if saving:
                patch["superseded"] = None
            return patch

        try:
            await run_in_threadpool(
                active_store.merge_agent_setting, agent_id, dict(body), session["email"], svc.validate_agent_document, prepare
            )
        except _DefaultChanged as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except ValueError as exc:
            raise _bad_request(sanitize_text(f"agent setting was rejected: {exc}", 300)) from None
        return _json(await _agent_row(config, agent_id, active_store))

    async def delete_agent_setting(request: Request) -> Response:
        session = await require_admin(request)
        require_mutation_guards(request, session)
        active_store = require_store()
        config, agent_id = await _configured_agent(request)
        # A kept edit goes only when the page names it (superseded_sha): one it
        # never showed is refused, like a stale Discard B.
        superseded_sha = _optional_sha_param(request, "superseded_sha")
        try:
            await run_in_threadpool(active_store.delete_agent_setting, agent_id, session["email"], superseded_sha)
        except SupersededConflict:
            raise HTTPException(status_code=409, detail="this agent has a replaced edit this page did not show; reload first") from None
        return _json(await _agent_row(config, agent_id, active_store))

    def _optional_sha_param(request: Request, name: str) -> Optional[str]:
        value = request.query_params.get(name)
        if value is None:
            return None
        if not svc.SHA256_HEX_PATTERN.fullmatch(value):
            raise _bad_request(f"{name} must be a sha256 hex digest")
        return value

    def _required_sha_param(request: Request, name: str) -> str:
        value = _optional_sha_param(request, name)
        if value is None:
            raise _bad_request(f"{name} is required: the sha of the replaced edit on screen")
        return value

    # The edit a changed prompt file replaced (``prompt_superseded`` on the row):
    # the admin rolls back to it or discards it, naming it by its ``sha`` so a
    # tab that has not seen a newer kept edit cannot act on it (409). Both
    # answer the updated row.
    async def restore_agent_superseded(request: Request) -> Response:
        session = await require_admin(request)
        require_mutation_guards(request, session)
        active_store = require_store()
        config, agent_id = await _configured_agent(request)
        expected = _required_sha_param(request, "sha")
        try:
            # The base is read inside the restore's write transaction.
            row = await run_in_threadpool(
                active_store.restore_superseded_prompt, agent_id, expected, lambda: _current_prompt_file_sha(agent_id), session["email"],
                svc.validate_agent_document,
            )
        except SupersededConflict:
            raise HTTPException(status_code=409, detail="the replaced edit changed since this page loaded it; reload to see it") from None
        except ValueError as exc:
            raise _bad_request(sanitize_text(f"agent setting was rejected: {exc}", 300)) from None
        if row is None:
            raise HTTPException(status_code=404, detail="this agent has no replaced prompt to roll back to")
        return _json(await _agent_row(config, agent_id, active_store))

    async def discard_agent_superseded(request: Request) -> Response:
        session = await require_admin(request)
        require_mutation_guards(request, session)
        active_store = require_store()
        config, agent_id = await _configured_agent(request)
        expected = _required_sha_param(request, "sha")
        try:
            found, _row = await run_in_threadpool(
                active_store.discard_superseded_prompt, agent_id, expected, session["email"], svc.validate_agent_document
            )
        except SupersededConflict:
            raise HTTPException(status_code=409, detail="the replaced edit changed since this page loaded it; reload to see it") from None
        if not found:
            raise HTTPException(status_code=404, detail="this agent has no replaced prompt to discard")
        return _json(await _agent_row(config, agent_id, active_store))

    # ---------------------------------------------------- review repositories
    # Repositories the admin adds here join config.repositories on the daemon's
    # next cycle (svc.review_repositories). Every session reads the list; only
    # the admin adds or removes, and only dashboard-added rows: a config
    # repository is changed in git. Adds and removes are serialised so two
    # tabs cannot drop each other's change.
    repositories_lock = anyio.Lock()

    def _repository_payload(config: Dict[str, Any], stored: Any) -> Dict[str, Any]:
        rows = [
            {
                # Validated owner/name, never passed through sanitize_text: its
                # long-token rule would redact a legitimately long name.
                "repo": row["repo"],
                "source": row["source"],
                "agents": [str(agent.get("id")) for agent in svc.agents_for_repo(config, row["repo"])],
                "added_by": sanitize_text(row["added_by"], 254) or None,
                "added_at": sanitize_text(row["added_at"], 64) or None,
                # Why the daemon skips it (too few agents for the quorum), if it does.
                "problem": sanitize_text(row["problem"], 300) or None,
            }
            for row in svc.merge_repositories(config, stored)
        ]
        return {
            "repositories": rows,
            "allowed_owners": svc.repository_owners(config),
            "max_added": MAX_ADDED_REPOSITORIES,
            "note": BASELINE_OFF_NOTE if config.get("review_existing_open_prs_on_first_run") else BASELINE_NOTE,
        }

    def _stored_added_entries(active_store: Any) -> List[Any]:
        """The stored ``added`` list exactly as saved, [] when there is no setting.

        Strict, for a change: a store error fails the request, and so does a row
        that is present but unreadable (not JSON, not an object, no ``added``
        list). Read as empty, the write that follows would replace every added
        repository with the new one.
        """
        try:
            row = active_store.get_setting(REPOSITORIES_SETTING)
        except Exception:  # noqa: BLE001 - any store failure refuses the change
            raise HTTPException(status_code=503, detail=STORE_READ_FAILED) from None
        if row is None:
            return []
        value = row.get("value") if isinstance(row, dict) else None
        added = value.get("added") if isinstance(value, dict) else None
        if not isinstance(added, list):
            raise HTTPException(status_code=409, detail=UNREADABLE_REPOSITORIES)
        return list(added)

    def _entry_names(entry: Any, repo: str) -> bool:
        """Whether a stored entry is ``repo`` (case-insensitive, as on GitHub)."""
        name = entry.get("repo") if isinstance(entry, dict) else None
        return isinstance(name, str) and name.strip().lower() == repo.lower()

    def _check_addable(config: Dict[str, Any], entries: List[Any], repo: str) -> None:
        """400 when ``repo`` is already on the list, the list is full, or too few agents would review it."""
        for row in svc.merge_repositories(config, {"added": entries}):
            if row["repo"].lower() == repo.lower():
                where = "it is in config/reviewer.json" if row["source"] == "config" else "it was added on this dashboard"
                raise _bad_request(f"{row['repo']} is already reviewed: {where}.")
        # Counted as stored: the store refuses a 51st entry, readable or not.
        if len(entries) >= MAX_ADDED_REPOSITORIES:
            raise _bad_request(
                f"At most {MAX_ADDED_REPOSITORIES} repositories can be added on the dashboard; remove one first."
            )
        problem = svc.repository_quorum_problem(config, repo)
        if problem:
            raise _bad_request(
                f"{repo} cannot be added: {problem}. A repository added here gets the default agents only; "
                "to switch agents on for it, add it to config/reviewer.json instead."
            )

    async def _repository_config() -> Dict[str, Any]:
        try:
            return await current_config()
        except svc.ServiceError:
            raise HTTPException(status_code=503, detail="the service config is unavailable") from None

    async def _github_full_name(config: Dict[str, Any], repo: str) -> str:
        """The repository's name as GitHub spells it, read with the daemon's own
        gh binary and login: "added" means "the daemon can read it"."""
        command = [str(config.get("gh_bin") or "gh"), "api", f"repos/{repo}", "--jq", ".full_name"]
        try:
            result = await run_in_threadpool(svc.run_command, command, REPOSITORY_PROBE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise HTTPException(
                status_code=503,
                detail=f"GitHub did not answer within {REPOSITORY_PROBE_TIMEOUT_SECONDS} seconds while checking {repo}; try again.",
            ) from None
        except OSError:
            raise HTTPException(
                status_code=503,
                detail=f"The dashboard could not run the GitHub CLI to check {repo}; it was not added.",
            ) from None
        if result.returncode != 0:
            # gh puts its verdict ("gh: Not Found (HTTP 404)") on stderr and the
            # raw response body on stdout.
            lines = [line.strip() for line in f"{result.stderr or ''}\n{result.stdout or ''}".splitlines() if line.strip()]
            reason = sanitize_text(lines[0], 200) if lines else f"gh exited with status {result.returncode}"
            raise _bad_request(
                f"The review account cannot see {repo} on GitHub ({reason}). "
                "Give it read access to the repository, then add it again."
            )
        lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        if not lines:
            raise HTTPException(status_code=503, detail=f"GitHub returned no repository name for {repo}; try again.")
        return lines[0]

    async def get_repository_settings(request: Request) -> Response:
        await require_session(request)
        try:
            config = await current_config()
        except svc.ServiceError as exc:
            return _json({
                "repositories": [],
                "allowed_owners": [],
                "max_added": MAX_ADDED_REPOSITORIES,
                "note": BASELINE_NOTE,
                "error": sanitize_text(f"service config unavailable: {exc}", 300),
            })
        # The config answer needs no store. When the saved list cannot be read
        # the config rows are still listed, with an error, so a store outage is
        # never mistaken for "the added repositories were removed".
        error = None
        stored = None
        if store is None:
            error = "The dashboard store is unavailable; only config/reviewer.json repositories are shown."
        else:
            try:
                row = await run_in_threadpool(store.get_setting, REPOSITORIES_SETTING)
            except Exception:  # noqa: BLE001 - shown as an error, never raised
                row = None
                error = (
                    "The dashboard store could not be read; only config/reviewer.json repositories are shown. "
                    "Repositories added here are not removed: do not add them again."
                )
            if isinstance(row, dict):
                stored = row.get("value")
                if not isinstance(stored, dict) or not isinstance(stored.get("added"), list):
                    error = "The saved repository list cannot be read; only config/reviewer.json repositories are shown and reviewed."
                    stored = None
        payload = await run_in_threadpool(_repository_payload, config, stored)
        if error:
            payload["error"] = error
        return _json(payload)

    async def add_repository(request: Request) -> Response:
        session = await require_admin(request)
        require_mutation_guards(request, session)
        active_store = require_store()
        config = await _repository_config()
        body = await _read_json_object(request)
        unknown = sorted(str(key) for key in body if key != "repo")
        if unknown:
            raise _bad_request("unknown fields: " + ", ".join(unknown))
        owners = svc.repository_owners(config)
        try:
            requested = svc.validate_repository_name(body.get("repo"), owners)
        except ValueError as exc:
            raise _bad_request(str(exc))
        # A duplicate, a full list or too few agents is refused before GitHub is asked anything.
        _check_addable(config, await run_in_threadpool(_stored_added_entries, active_store), requested)
        full_name = await _github_full_name(config, requested)
        try:
            # A transferred repository answers with its new owner.
            canonical = svc.validate_repository_name(full_name, owners)
        except ValueError as exc:
            raise _bad_request(sanitize_text(f"GitHub reports {requested} as {full_name}: {exc}", 300)) from None
        async with repositories_lock:
            # Entries are written back exactly as stored: a change never drops one it did not name.
            entries = await run_in_threadpool(_stored_added_entries, active_store)
            _check_addable(config, entries, canonical)
            value = {"added": entries + [{
                "repo": canonical,
                "added_by": session["email"],
                "added_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            }]}
            try:
                await run_in_threadpool(active_store.set_setting, REPOSITORIES_SETTING, value, session["email"])
            except ValueError as exc:
                raise _bad_request(sanitize_text(f"the repository list was rejected by the store: {exc}", 300)) from None
        payload = await run_in_threadpool(_repository_payload, config, value)
        payload["added"] = canonical
        return _json(payload)

    async def remove_repository(request: Request) -> Response:
        session = await require_admin(request)
        require_mutation_guards(request, session)
        active_store = require_store()
        config = await _repository_config()
        target = f"{request.path_params['owner']}/{request.path_params['name']}"
        async with repositories_lock:
            entries = await run_in_threadpool(_stored_added_entries, active_store)
            rows = svc.merge_repositories(config, {"added": entries})
            match = next((row for row in rows if row["repo"].lower() == target.lower()), None)
            if match is None:
                raise HTTPException(status_code=404, detail=sanitize_text(f"{target} is not on the review list", 300))
            if match["source"] == "config":
                raise HTTPException(
                    status_code=409, detail=f"{match['repo']} is configured in config/reviewer.json; remove it there."
                )
            kept = [entry for entry in entries if not _entry_names(entry, match["repo"])]
            value = {"added": kept} if kept else None
            if value is not None:
                await run_in_threadpool(active_store.set_setting, REPOSITORIES_SETTING, value, session["email"])
            else:
                await run_in_threadpool(active_store.delete_setting, REPOSITORIES_SETTING, session["email"])
        payload = await run_in_threadpool(_repository_payload, config, value)
        payload["removed"] = match["repo"]
        return _json(payload)

    # ------------------------------------------------------------ admin grants
    # Addresses the owner granted admin (store key ``admins``). Every session
    # reads the list; only the owner adds or removes, and require_session
    # re-derives every role from it per request. Changes hold admins_lock (see
    # the auth helpers) so two tabs cannot drop each other's change.

    def _admin_rows(value: Any) -> List[Dict[str, Any]]:
        """The stored entries that grant admin (malformed ones grant nothing and are not listed)."""
        granted = set(dashboard_auth.admin_emails_from_setting(value))
        rows = []  # type: List[Dict[str, Any]]
        for entry in value.get("added") if isinstance(value, dict) and isinstance(value.get("added"), list) else []:
            email = entry.get("email") if isinstance(entry, dict) else None
            # A hand-edited entry can hold any JSON (a list is unhashable).
            if not isinstance(email, str) or email not in granted:
                continue
            granted.discard(email)
            rows.append({
                # Validated and normalized, never passed through sanitize_text:
                # its long-token rule would redact a legitimately long address.
                "email": email,
                "added_by": sanitize_text(entry.get("added_by"), 254) or None,
                "added_at": sanitize_text(entry.get("added_at"), 64) or None,
            })
        return rows

    def _admins_payload(session: Dict[str, Any], value: Any) -> Dict[str, Any]:
        return {
            "owner": dashboard_auth.ADMIN_EMAIL,
            "admins": _admin_rows(value),
            "can_edit": session.get("owner") is True,
            "max_added": MAX_ADDED_ADMINS,
        }

    def _stored_admin_entries(active_store: Any) -> List[Any]:
        """The stored ``added`` list exactly as saved, [] when there is no setting.

        Strict, like _stored_added_entries: read as empty, the write that
        follows would revoke every other grant.
        """
        try:
            row = active_store.get_setting(ADMINS_SETTING)
        except Exception as exc:  # noqa: BLE001 - any store failure refuses the change
            print(f"dashboard admin list unreadable: {exc!r}", file=sys.stderr)
            raise HTTPException(status_code=503, detail=STORE_READ_FAILED) from None
        if row is None:
            return []
        value = row.get("value") if isinstance(row, dict) else None
        added = value.get("added") if isinstance(value, dict) else None
        if not isinstance(added, list):
            raise HTTPException(status_code=409, detail=UNREADABLE_ADMINS)
        return list(added)

    def _admin_entry_names(entry: Any, email: str) -> bool:
        """Whether a stored entry names ``email``; loose on purpose, so a removal also drops a hand-edited spelling."""
        stored = entry.get("email") if isinstance(entry, dict) else None
        return isinstance(stored, str) and stored.strip().lower() == email

    def _requested_admin_email(raw: Any) -> str:
        try:
            return dashboard_auth.normalize_admin_email(raw)
        except ValueError as exc:
            raise _bad_request(str(exc)) from None

    async def get_admin_settings(request: Request) -> Response:
        session = await require_session(request)
        # The owner is admin whatever the store says; an unreadable list is
        # shown as an error, never as "nobody else is admin".
        if store is None:
            payload = _admins_payload(session, None)
            payload["error"] = "The dashboard store is unavailable; only the owner is admin until it is back."
            return _json(payload)
        try:
            row = await run_in_threadpool(store.get_setting, ADMINS_SETTING)
        except Exception as exc:  # noqa: BLE001 - shown as an error, never raised
            print(f"dashboard admin list unreadable: {exc!r}", file=sys.stderr)
            payload = _admins_payload(session, None)
            payload["error"] = "The dashboard store could not be read; only the owner is admin until it can."
            return _json(payload)
        value = row.get("value") if isinstance(row, dict) else None
        payload = _admins_payload(session, value)
        if row is not None and not (isinstance(value, dict) and isinstance(value.get("added"), list)):
            payload["error"] = "The saved admin list cannot be read; only the owner is admin until it is repaired."
        return _json(payload)

    async def contributor_code(request: Request) -> Response:
        """A one-time code binding a GitHub login to this session's verified email (contributor gate).

        Any signed-in account may ask: the sign-in already proved the email and
        its Workspace domain. The body names the GitHub login the code is for
        (``{"github_login": "..."}``); only that login's ``@review-of-legends
        verify <code>`` comment consumes it. The code is returned once and
        stored hashed.
        """
        session = await require_session(request)
        require_mutation_guards(request, session)
        active_store = require_store()
        body = await _read_json_object(request)
        unknown = sorted(str(key) for key in body if key != "github_login")
        if unknown:
            raise _bad_request("unknown fields: " + ", ".join(unknown))
        github_login = str(body.get("github_login") or "").strip().lstrip("@")
        if not LOGIN_PATTERN.fullmatch(github_login) or not contributors.LOGIN.fullmatch(github_login) or github_login.endswith("[bot]"):
            raise _bad_request("github_login must be your GitHub login")
        email = str(session.get("email") or "").strip().lower()
        if not email or "@" not in email:
            raise HTTPException(status_code=403, detail="this session has no verified email")
        code = contributors.new_verify_code()
        expires_at = await run_in_threadpool(
            active_store.create_contributor_code, email, contributors.code_hash(code), github_login,
            contributors.VERIFY_CODE_TTL_SECONDS,
        )
        return _json({"code": code, "expires_at": expires_at, "github_login": github_login})

    async def add_admin(request: Request) -> Response:
        session = await require_owner(request)
        require_mutation_guards(request, session)
        active_store = require_store()
        body = await _read_json_object(request)
        unknown = sorted(str(key) for key in body if key != "email")
        if unknown:
            raise _bad_request("unknown fields: " + ", ".join(unknown))
        email = _requested_admin_email(body.get("email"))
        if dashboard_auth.is_owner(email):
            raise _bad_request(f"{email} is the owner and is always admin.")
        async with admins_lock:
            # Entries are written back exactly as stored: a change never drops one it did not name.
            entries = await run_in_threadpool(_stored_admin_entries, active_store)
            if email in dashboard_auth.admin_emails_from_setting({"added": entries}):
                raise _bad_request(f"{email} is already an admin.")
            # A hand-edited spelling of the same address grants nothing; the
            # clean entry replaces it instead of sitting next to it.
            entries = [entry for entry in entries if not _admin_entry_names(entry, email)]
            # Counted as stored: the store refuses a 51st entry, readable or not.
            if len(entries) >= MAX_ADDED_ADMINS:
                raise _bad_request(f"At most {MAX_ADDED_ADMINS} admins can be added; remove one first.")
            # The address is not granted now, so an admin row it still has was
            # minted under an earlier grant (removed by a hand edit, or by a
            # removal that stopped half way): it must not come back as admin.
            await run_in_threadpool(sessions.revoke_admin_sessions, email)
            value = {"added": entries + [{
                "email": email,
                "added_by": session["email"],
                "added_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            }]}
            try:
                await run_in_threadpool(active_store.set_setting, ADMINS_SETTING, value, session["email"])
            except ValueError as exc:
                raise _bad_request(sanitize_text(f"the admin list was rejected by the store: {exc}", 300)) from None
        payload = _admins_payload(session, value)
        payload["added"] = email
        return _json(payload)

    async def remove_admin(request: Request) -> Response:
        session = await require_owner(request)
        require_mutation_guards(request, session)
        active_store = require_store()
        email = _requested_admin_email(request.path_params.get("email"))
        if dashboard_auth.is_owner(email):
            raise HTTPException(status_code=409, detail=f"{email} is the owner and is always admin; it cannot be removed.")
        async with admins_lock:
            entries = await run_in_threadpool(_stored_admin_entries, active_store)
            kept = [entry for entry in entries if not _admin_entry_names(entry, email)]
            if len(kept) == len(entries):
                raise HTTPException(status_code=404, detail=f"{email} is not an added admin.")
            # Now, not on their next request: an idle session would otherwise
            # come back as admin when the address is granted again. Before the
            # list is written, so a failed purge removes nothing and the removal
            # can be retried. Inside the lock, every sign-in that read the old
            # grant has stored its row, and none can store one until the list
            # is written.
            await run_in_threadpool(sessions.revoke_admin_sessions, email)
            value = {"added": kept} if kept else None
            if value is not None:
                try:
                    await run_in_threadpool(active_store.set_setting, ADMINS_SETTING, value, session["email"])
                except ValueError as exc:
                    raise _bad_request(sanitize_text(f"the admin list was rejected by the store: {exc}", 300)) from None
            else:
                await run_in_threadpool(active_store.delete_setting, ADMINS_SETTING, session["email"])
        payload = _admins_payload(session, value)
        payload["removed"] = email
        return _json(payload)

    catalog_cache: Dict[str, Any] = {"expires_at": 0.0, "payload": None}
    catalog_lock = anyio.Lock()
    catalog_url = str(settings["llmux_base_url"]).rstrip("/") + MODEL_CATALOG_PATH

    async def _fetch_catalog() -> Dict[str, Any]:
        fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        unavailable = {"available": False, "fetched_at": fetched_at, "models": []}
        try:
            async with httpx.AsyncClient(
                timeout=USAGE_TIMEOUT_SECONDS, follow_redirects=False, transport=usage_transport
            ) as client:
                # The catalog is llmux's unauthenticated data plane: no key is sent.
                async with client.stream("GET", catalog_url, headers={"Accept": "application/json"}) as response:
                    if response.status_code >= 300:
                        return {**unavailable, "error": f"llmux model catalog is unavailable (HTTP {response.status_code})"}
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MODEL_CATALOG_MAX_BYTES:
                            return {**unavailable, "error": "llmux model catalog response exceeded the size limit"}
        except (httpx.HTTPError, ValueError):
            return {**unavailable, "error": "llmux model catalog could not be reached"}
        try:
            payload = json.loads(bytes(body))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {**unavailable, "error": "llmux model catalog returned an unreadable response"}
        models = project_model_catalog(payload)
        if models is None:
            return {**unavailable, "error": "llmux model catalog returned an unexpected shape"}
        return {"available": True, "fetched_at": fetched_at, "models": models}

    async def _cached_catalog() -> Dict[str, Any]:
        async with catalog_lock:
            now = time.monotonic()
            if catalog_cache["payload"] is not None and now < catalog_cache["expires_at"]:
                return catalog_cache["payload"]
            payload = await _fetch_catalog()
            catalog_cache["payload"] = payload
            # An unavailable catalog is cached too (briefly), so a hung llmux
            # costs one timeout per window instead of one per request.
            catalog_cache["expires_at"] = now + (
                MODEL_CATALOG_CACHE_SECONDS if payload.get("available") else MODEL_CATALOG_UNAVAILABLE_CACHE_SECONDS
            )
            return payload

    async def models(request: Request) -> Response:
        await require_session(request)
        return _json(await _cached_catalog())

    async def events(request: Request) -> Response:
        session = await require_session(request)
        active_store = require_store()
        # An explicit cursor (native reconnect header, or ?after= including 0)
        # means "replay exactly from there"; its absence means "start live".
        header_cursor = request.headers.get("last-event-id")
        after_param = request.query_params.get("after")
        if header_cursor not in (None, ""):
            cursor = _parse_cursor(header_cursor, "Last-Event-ID")
        elif after_param not in (None, ""):
            cursor = _parse_cursor(after_param, "after")
        else:
            cursor = await run_in_threadpool(stream_tail_cursor, active_store)
        session_id = request.cookies.get(dashboard_auth.SESSION_COOKIE) or ""

        async def stream():
            position = cursor
            last_heartbeat = time.monotonic()
            last_recheck = time.monotonic()
            # Anchor first: a client that connects during a quiet period still
            # learns the cursor its next reconnect should resume from.
            yield f"id: {position}\nevent: ready\ndata: {json.dumps({'cursor': position})}\n\n"
            while True:
                if await request.is_disconnected():
                    return
                now = time.monotonic()
                if now - last_recheck >= recheck_seconds:
                    last_recheck = now
                    # The same per-request check as require_session: a revoked
                    # admin grant ends an open stream too.
                    live = await run_in_threadpool(_live_session, session_id)
                    if live is None:
                        yield AUTH_EXPIRED_FRAME
                        return
                batch = await run_in_threadpool(
                    lambda: active_store.events_after(position, MAX_EVENT_BATCH)
                )
                for row in batch:
                    event = project_event(row)
                    position = event["id"] if event["id"] is not None else position
                    yield f"id: {event['id']}\nevent: update\ndata: {json.dumps(event)}\n\n"
                    last_heartbeat = time.monotonic()
                if not batch:
                    if time.monotonic() - last_heartbeat >= heartbeat_seconds:
                        last_heartbeat = time.monotonic()
                        # A named event, not an SSE comment: the browser client
                        # subscribes by name (dashboard/stream.js), and a comment
                        # line never reaches an addEventListener handler.
                        yield HEARTBEAT_FRAME
                    await anyio.sleep(poll_seconds)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    async def _fetch_usage() -> Dict[str, Any]:
        fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        headers = {
            auth_header_name: f"{auth_header_scheme} {llmux_key}".strip() if auth_header_scheme else llmux_key,
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=USAGE_TIMEOUT_SECONDS,
                follow_redirects=False,
                transport=usage_transport,
            ) as client:
                async with client.stream("GET", llmux_url, headers=headers) as response:
                    if response.status_code >= 300:
                        return {
                            "available": False,
                            "fetched_at": fetched_at,
                            "accounts": [],
                            "error": f"llmux telemetry is unavailable (HTTP {response.status_code})",
                        }
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > USAGE_MAX_BYTES:
                            return {
                                "available": False,
                                "fetched_at": fetched_at,
                                "accounts": [],
                                "error": "llmux telemetry response exceeded the size limit",
                            }
        except (httpx.HTTPError, ValueError):
            return {
                "available": False,
                "fetched_at": fetched_at,
                "accounts": [],
                "error": "llmux telemetry could not be reached",
            }

        try:
            payload = json.loads(bytes(body))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        accounts = project_usage_accounts(payload)
        if accounts is None:
            return {
                "available": False,
                "fetched_at": fetched_at,
                "accounts": [],
                "error": "llmux telemetry response was not in the expected shape",
            }
        return {"available": True, "fetched_at": fetched_at, "accounts": accounts}

    async def usage(request: Request) -> Response:
        await require_admin(request)
        async with usage_lock:
            now = time.monotonic()
            if usage_cache["payload"] is not None and now < usage_cache["expires_at"]:
                return _json(usage_cache["payload"])
            payload = await _fetch_usage()
            usage_cache["payload"] = payload
            usage_cache["expires_at"] = now + USAGE_CACHE_SECONDS
        return _json(payload)

    # --------------------------------------------------------- service summary
    def require_service_token(request: Request) -> None:
        """Loopback peer plus a shared secret; no session, cookie or CSRF.

        The caller is a co-hosted service, not a browser, so there is nothing to
        sign in and nothing an attacker's page could ride: a cross-site request
        cannot set this header, and a request from off-host cannot reach the
        loopback branch at all. Order matters - an unconfigured deployment is
        indistinguishable from one without the route, and the peer is judged
        before the token so a remote guess never gets an oracle on the secret.
        """
        if not configured_service_token:
            raise HTTPException(status_code=404, detail="not found")
        client = request.client
        host = client.host if client is not None else ""
        if not host or not _is_loopback(host):
            raise HTTPException(status_code=403, detail="loopback only")
        # The reverse proxy for the public origin runs on this same host, so a
        # proxied request also arrives from a loopback peer. It keeps the
        # public ``Host`` header, though; a genuine co-hosted caller addresses
        # the loopback bind directly, so the requested host must be loopback too.
        requested_host = urllib.parse.urlsplit("//" + (request.headers.get("host") or "")).hostname or ""
        if not requested_host or not _is_loopback(requested_host):
            raise HTTPException(status_code=403, detail="loopback only")
        presented = request.headers.get(SERVICE_TOKEN_HEADER) or ""
        if not hmac.compare_digest(presented.encode("utf-8"), configured_service_token):
            raise HTTPException(status_code=403, detail="invalid service token")

    def summary_counts(active_store: Any, cutoff: str) -> Dict[str, int]:
        """Bucket sizes from ONE store snapshot, never by paging rows up here.

        A single ``run_status_counts`` read means the buckets partition the same
        rows: an attempt cannot be counted as running and as failed because it
        transitioned between two queries.
        """
        by_status = active_store.run_status_counts(cutoff)
        total = sum(entry["total"] for entry in by_status.values())
        active = sum(by_status.get(status, {}).get("total", 0) for status in ACTIVE_RUN_STATUSES)
        attention = sum(by_status.get(status, {}).get("total", 0) for status in ATTENTION_RUN_STATUSES)
        return {
            "active": active,
            "attention": attention,
            "succeeded_24h": by_status.get("succeeded", {}).get("recent", 0),
            "failed_24h": by_status.get("failed", {}).get("recent", 0),
            # The console's History view is the UNFILTERED query
            # (dashboard/runs.js bucketStatuses -> [null], bucketAccepts -> true
            # for every run), so its count is every attempt, including the ones
            # the other two buckets also show.
            "history": total,
        }

    def executor_snapshot() -> Dict[str, Any]:
        """Lock/pause sentinels, read exactly the way a deploy drain reads them.

        Nothing here is inferred from the store: a row can say ``running`` long
        after the process that wrote it died, so the answer to "is an attempt
        in flight" is the OS lock. An unreadable config reports ``unknown``
        rather than a guessed ``idle``, which would read as "nothing running".
        """
        unknown: Dict[str, Any] = {"state": "unknown", "in_flight": None, "pid": None}
        if dashboard_execution is None:
            return unknown
        try:
            config = svc.load_config(paths["config_path"])
            state_path = Path(str(config.get("state_path") or "data/state.json"))
            if not state_path.is_absolute():
                state_path = paths["service_root"] / state_path
            lock_path = dashboard_execution.executor_lock_path(config, state_path)
            pause_path = dashboard_execution.executor_pause_path(config, state_path)
        except (svc.ServiceError, OSError, TypeError, ValueError):
            return unknown
        pid = dashboard_execution.probe_executor_lock(lock_path)
        # ``-1`` is the lock's "held, but the holder pid is unreadable" marker:
        # held is real, the number is not, so it is not reported as a pid.
        holder = pid if isinstance(pid, int) and pid > 0 else None
        if dashboard_execution.stop_requested(pause_path):
            # Paused is about intake. The attempt the executor already holds is
            # still running, and the lock is what knows that.
            return {"state": "paused", "in_flight": pid is not None, "pid": holder}
        if pid is None:
            return {"state": "idle", "in_flight": False, "pid": None}
        return {"state": "running", "in_flight": True, "pid": holder}

    async def service_summary(request: Request) -> Response:
        require_service_token(request)
        active_store = require_store()
        if not callable(getattr(active_store, "run_status_counts", None)) or not callable(
            getattr(active_store, "latest_event_at", None)
        ):
            raise HTTPException(status_code=503, detail="dashboard summary is unavailable")
        cutoff = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=SUMMARY_WINDOW_SECONDS)
        ).isoformat(timespec="seconds")
        version = await run_in_threadpool(svc.current_version, paths["service_root"])
        counts = await run_in_threadpool(lambda: summary_counts(active_store, cutoff))
        last_event_at = await run_in_threadpool(active_store.latest_event_at)
        executor = await run_in_threadpool(executor_snapshot)
        # The same projection the Settings view's "in effect now" line is built
        # from, so the portal card cannot disagree with the console.
        policy = await _policy_payload(active_store)
        return _json(
            {
                "version": version,
                "executor": executor,
                "counts": counts,
                "policy": policy,
                "last_event_at": last_event_at,
                "generated_at": time.time(),
            }
        )

    async def healthz(request: Request) -> Response:
        version = await run_in_threadpool(svc.current_version, paths["service_root"])
        return _json(
            {
                "status": "ok",
                "version": version,
                "auth_configured": bool(str(settings.get("google_client_id") or "").strip()),
            }
        )

    def _resolve_asset(relative: str) -> Optional[Path]:
        root = paths["dashboard_dir"]
        parts = [part for part in relative.split("/") if part not in ("", ".")]
        if not parts or len(parts) > MAX_ASSET_DEPTH:
            return None
        for part in parts:
            if part == ".." or not ASSET_NAME_PATTERN.match(part):
                return None
        candidate = root.joinpath(*parts)
        if candidate.suffix.lower() not in ASSET_SUFFIXES:
            return None
        try:
            resolved = candidate.resolve(strict=True)
            root_resolved = root.resolve(strict=True)
        except OSError:
            return None
        # A symlink inside dashboard/ must not reach outside it.
        if not resolved.is_relative_to(root_resolved) or not resolved.is_file():
            return None
        return resolved

    async def shell(request: Request) -> Response:
        index = _resolve_asset("index.html")
        if index is not None:
            return FileResponse(
                index, headers={"Cache-Control": "no-store"}, media_type="text/html; charset=utf-8"
            )
        return HTMLResponse(LOGIN_SHELL_HTML, headers={"Cache-Control": "no-store"})

    async def asset(request: Request) -> Response:
        resolved = _resolve_asset(request.path_params.get("asset_path", ""))
        if resolved is None:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(resolved, headers={"Cache-Control": "no-store"})

    async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
        if exc.status_code == 405:
            return _json({"error": "method not allowed"}, status_code=405)
        detail = exc.detail if isinstance(exc.detail, str) else "request failed"
        return _json({"error": detail}, status_code=exc.status_code)

    class SecurityHeadersMiddleware:
        """CSP and friends on every response, including static assets."""

        def __init__(self, app) -> None:
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return

            async def send_with_headers(message):
                if message["type"] == "http.response.start":
                    headers = message.setdefault("headers", [])
                    existing = {key.decode("latin-1").lower() for key, _ in headers}
                    additions = {
                        "content-security-policy": CONTENT_SECURITY_POLICY,
                        "x-content-type-options": "nosniff",
                        "referrer-policy": "no-referrer",
                        "x-frame-options": "DENY",
                        "cross-origin-opener-policy": "same-origin",
                        "permissions-policy": "geolocation=(), camera=(), microphone=()",
                    }
                    for name, value in additions.items():
                        if name not in existing:
                            headers.append((name.encode("latin-1"), value.encode("latin-1")))
                await send(message)

            await self.app(scope, receive, send_with_headers)

    allowed_hosts = [canonical_host, BIND_HOST, "localhost", f"{BIND_HOST}:{BIND_PORT}", f"localhost:{BIND_PORT}"]
    middleware = [
        Middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts, www_redirect=False),
        Middleware(SecurityHeadersMiddleware),
        Middleware(
            SessionMiddleware,
            secret_key=str(settings["session_secret"]),
            session_cookie=dashboard_auth.OAUTH_STATE_COOKIE,
            max_age=dashboard_auth.OAUTH_STATE_TTL_SECONDS,
            same_site="lax",
            https_only=True,
            path="/auth",
        ),
    ]

    routes = [
        Route("/", shell, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/auth/login", login, methods=["GET"]),
        Route(CALLBACK_PATH, callback, methods=["GET"]),
        Route("/auth/logout", logout, methods=["POST"]),
        Route("/api/me", me, methods=["GET"]),
        Route("/api/runs", list_runs, methods=["GET"]),
        Route("/api/runs/{run_id}", get_run, methods=["GET"]),
        Route("/api/runs/{run_id}/retry", retry_run, methods=["POST"]),
        Route("/api/retries", list_retries, methods=["GET"]),
        Route("/api/retrigger/{owner}/{name}/{pr}", retrigger_info, methods=["GET"]),
        Route("/api/retrigger", retrigger, methods=["POST"]),
        Route("/api/models", models, methods=["GET"]),
        Route("/api/settings/model-policy", get_model_policy, methods=["GET"]),
        Route("/api/settings/model-policy", put_model_policy, methods=["PUT"]),
        Route("/api/settings/model-policy", delete_model_policy, methods=["DELETE"]),
        Route("/api/settings/agents", get_agent_settings, methods=["GET"]),
        Route("/api/settings/agents/{agent_id}", put_agent_setting, methods=["PUT"], max_body_size=AGENT_SETTING_MAX_BODY_BYTES),
        Route("/api/settings/agents/{agent_id}", delete_agent_setting, methods=["DELETE"]),
        Route("/api/settings/agents/{agent_id}/superseded", discard_agent_superseded, methods=["DELETE"]),
        Route("/api/settings/agents/{agent_id}/superseded/restore", restore_agent_superseded, methods=["POST"]),
        Route("/api/settings/repositories", get_repository_settings, methods=["GET"]),
        Route("/api/settings/repositories", add_repository, methods=["POST"]),
        Route("/api/settings/repositories/{owner}/{name}", remove_repository, methods=["DELETE"]),
        Route("/api/settings/admins", get_admin_settings, methods=["GET"]),
        Route("/api/settings/admins", add_admin, methods=["POST"]),
        # ``path``: the server decodes %2F before routing, and an address's local
        # part may legally carry a "/".
        Route("/api/settings/admins/{email:path}", remove_admin, methods=["DELETE"]),
        Route("/api/contributor/code", contributor_code, methods=["POST"]),
        Route("/api/authors", list_authors, methods=["GET"]),
        Route("/api/feedback", list_feedback, methods=["GET"]),
        Route("/api/events", events, methods=["GET"]),
        Route("/api/usage", usage, methods=["GET"]),
        Route("/api/summary", service_summary, methods=["GET"]),
        Route("/{asset_path:path}", asset, methods=["GET"]),
    ]

    app = Starlette(
        debug=False,
        routes=routes,
        middleware=middleware,
        exception_handlers={HTTPException: http_exception_handler},
        max_body_size=64 * 1024,
    )
    app.state.sessions = sessions
    app.state.oauth_state_cache = state_cache
    return app


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Review of Legends admin dashboard web API")
    parser.add_argument("--settings", default=str(DEFAULT_SETTINGS_PATH), help="path to the settings JSON file")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(Path(args.settings))
    except SettingsError as exc:
        print(f"dashboard settings error: {exc}", file=sys.stderr)
        return 2

    paths = resolve_paths(settings)
    store = None
    try:
        from .dashboard_store import DashboardStore

        store = DashboardStore(paths["store_path"])
    except Exception as exc:  # noqa: BLE001 - the web tier must still serve /healthz
        print(f"dashboard store unavailable at {paths['store_path']}: {exc!r}", file=sys.stderr)

    import uvicorn

    uvicorn.run(create_app(settings, store), **uvicorn_run_kwargs(settings))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
