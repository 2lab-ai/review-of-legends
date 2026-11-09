"""Agent runner that calls an Anthropic-Messages-compatible endpoint (llmux).

Invoked by the service as the ``agent_runner.command``. Reads the rendered
prompt from ``--prompt-file``, POSTs it to ``{base_url}/v1/messages``, and
writes the model's text output to ``--response-file`` for the service to parse.
With ``--meta-file`` it also writes a JSON record of *how* the answer was
produced: the ladder, the model that answered, the effort, token usage, every
attempt with its outcome, timings and the proxy's response ids. The service
publishes that record on the PR progress comment and the dashboard, so the
record must stay free of secrets: no API key, no prompt text, no paths. A failed
attempt keeps its HTTP status and the upstream's error text (or, for a reply
without JSON, the model's reply), clipped and path-scrubbed; only the dashboard
shows that text.

The request body carries nothing but the model, the token budget, the prompt,
and the optional effort: no vendor client-identity or account metadata.

Fail-closed by construction:
- ``--base-url`` must point at a proxy. Direct vendor endpoints
  (api.anthropic.com and friends) are rejected before any request is sent,
  so a misconfigured run can never silently bypass llmux.
- Model text is only ever accepted from a 2xx response.
- Any exhausted retry ladder (primary model, then each ``--fallback-model``)
  exits non-zero without writing the response file, which the service already
  records as an agent failure. The meta file is still written so the failure
  route is visible.

Error model:
- Quota exhausted (moved to the next model at once, no retry sleep): HTTP 429
  whose ``Retry-After`` is at least ``--quota-skip-seconds``, or whose message
  says the proxy's accounts are exhausted / rate-limited. llmux answers exactly
  this when every in-scope account has used up its usage window; waiting the
  retry-after out (hours) would burn the agent timeout for nothing.
- Transient (retried, then next model): other HTTP 408/429/5xx, unreachable
  endpoint, and 2xx/4xx bodies whose error type or message indicates a
  rate limit / overload / proxy or upstream fault.
- Permanent (skip straight to the next model): everything else, e.g.
  invalid-request errors or empty model output.

A wall-clock ``--total-budget`` bounds the whole ladder so N models x M
attempts can never exceed the service's per-agent timeout. While a fallback
model is still untried, an attempt is skipped (its time left to the fallback)
when its window would be shorter than ``--min-attempt-seconds`` or than the
model's last failed request ran, or when that request ran to the model's
``--model-ceiling``. This is a policy, not a proof: a retry with less time than
the request that just failed rarely does better, and one the proxy cuts at the
same point cannot, so the untried fallback gets the time instead.
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Direct vendor API hosts this runner must never talk to: the whole point is
# that account selection and token refresh live in the proxy.
DENIED_HOSTS = ("api.anthropic.com", "api.openai.com", "api.x.ai")

TRANSIENT_ERROR_TYPES = {
    "proxy_error",
    "overloaded_error",
    "api_error",
    "rate_limit_error",
}
TRANSIENT_MESSAGE_MARKERS = ("rate-limit", "rate limit", "overloaded", "temporarily")
# llmux's "every account used its usage window up" wording (forward.rs
# exhausted_response) plus the generic vocabulary other proxies use.
QUOTA_MESSAGE_MARKERS = (
    "eligible accounts are rate-limited",
    "accounts exhausted",
    "account(s) exhausted",
)
# Generic vocabulary: trusted only on a rate-limit error, so an api_error that
# merely mentions a quota keeps its retries.
QUOTA_RATE_LIMIT_MARKERS = ("usage limit", "quota")
# llmux's exhausted wording carries the window too: "...; retry in 387s."
RETRY_IN_PATTERN = re.compile(r"retry in (\d+(?:\.\d+)?)\s*s\b", re.IGNORECASE)
DEFAULT_QUOTA_SKIP_SECONDS = 60.0

# Response headers worth recording. Anything else (cookies, account hints)
# stays out of the meta record.
META_RESPONSE_HEADERS = (
    "request-id",
    "x-request-id",
    "retry-after",
    "x-llmux-compatibility",
    "x-llmux-compatibility-warnings",
    "x-llmux-omitted-fields",
    "x-llmux-token-count",
)
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
META_SCHEMA = 1
MAX_META_ERROR_CHARS = 2000


class RunnerError(Exception):
    # The HTTP status of the response that raised this, when there was one.
    http_status: int | None = None


class TransientError(RunnerError):
    """Retry the same model; ``retry_after`` paces the retry when the proxy said how long."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class QuotaExhaustedError(TransientError):
    """The proxy has no usage left for this model: try the next model now."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # The destination invariant (proxy-only) must hold for every hop, so a
    # redirect is refused outright instead of being followed and re-validated.
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def check_base_url(base_url: str) -> None:
    host = (urllib.parse.urlsplit(base_url).hostname or "").lower().rstrip(".")
    if not host:
        raise RunnerError(f"invalid --base-url: {base_url!r}")
    for denied in DENIED_HOSTS:
        if host == denied or host.endswith("." + denied):
            raise RunnerError(
                f"--base-url points at direct vendor endpoint {host}; this runner only talks to a proxy"
            )


def transient_status(code: int) -> bool:
    return code in (408, 429) or code >= 500


JSON_NUDGE = (
    "Your previous reply contained no JSON object. You have no tools and cannot open "
    "files: everything needed is in the first message. Reply now with exactly one JSON "
    "object matching the schema given there, and nothing else."
)


def has_json_object(text: str) -> bool:
    """The same test the service applies: fence-stripped, a JSON *object* parses.

    A brace pair inside prose (``if (!ok) { return; }`` in a model's plan) is
    not an object; declaring it one would skip the nudge exactly when it helps.
    """
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        # Whole text parses: the service accepts it only if it is an object.
        return isinstance(json.loads(candidate), dict)
    except json.JSONDecodeError:
        pass
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        return False
    try:
        return isinstance(json.loads(candidate[start : end + 1]), dict)
    except json.JSONDecodeError:
        return False


def build_payload(model: str, prompt: str, max_tokens: int, effort: str, messages: list | None = None) -> bytes:
    payload: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages or [{"role": "user", "content": prompt}],
    }
    if effort:
        # Passed through to the upstream when the proxy has no server-side
        # override configured (llmux codex.reasoning_effort bypass semantics).
        payload["output_config"] = {"effort": effort}
    return json.dumps(payload).encode("utf-8")


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a ``Retry-After`` header; HTTP-dates are not expected from the proxy."""
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


# An absolute filesystem path (three or more segments, not part of a URL);
# an API route such as ``/v1/messages`` keeps its diagnostics.
PATH_PATTERN = re.compile(r"(?<![:\w/.-])/(?:[\w.@-]+/){2,}[\w.@-]*")


def scrub_paths(text: str) -> str:
    """The meta record is published; an OSError text would otherwise name the work dir."""
    return PATH_PATTERN.sub("<path>", text)


def quota_message(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in QUOTA_MESSAGE_MARKERS)


def classify_error_body(raw: str, retry_after: float | None = None, quota_skip: float = 0.0) -> RunnerError:
    """Map an error payload (any HTTP status) to Quota/Transient/RunnerError."""
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return RunnerError(f"non-JSON error response: {raw[:500]}")
    error = body.get("error", {}) if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        error = {"message": str(error)}
    message = str(error.get("message", raw[:500]))
    etype = str(error.get("type", ""))
    lowered = message.lower()
    if retry_after is None:
        window = RETRY_IN_PATTERN.search(message)
        if window:
            retry_after = float(window.group(1))
    rate_limited = etype == "rate_limit_error"
    quota_wording = quota_message(message) or (rate_limited and any(marker in lowered for marker in QUOTA_RATE_LIMIT_MARKERS))
    if retry_after is not None and quota_skip > 0 and retry_after < quota_skip and (quota_wording or rate_limited):
        # A short window is a burst (six agents starting at once), not a used-up
        # usage window: retry this model after it, do not skip to the fallback.
        return TransientError(f"{etype}: {message}", retry_after=retry_after)
    # The exhausted-accounts wording is specific enough to trust under any
    # error type; a long Retry-After counts only on a rate-limit error.
    if quota_wording or (rate_limited and retry_after is not None and quota_skip > 0 and retry_after >= quota_skip):
        return QuotaExhaustedError(f"{etype}: {message}", retry_after=retry_after)
    if etype in TRANSIENT_ERROR_TYPES or any(marker in lowered for marker in TRANSIENT_MESSAGE_MARKERS):
        return TransientError(f"{etype}: {message}", retry_after=retry_after)
    return RunnerError(f"{etype}: {message}")


def extract_text(body: dict, quota_skip: float = 0.0) -> str:
    if body.get("type") == "error" or (body.get("error") and not body.get("content")):
        # A 200-status error envelope gets the same burst/quota rule as a 429.
        raise classify_error_body(json.dumps(body), quota_skip=quota_skip)
    blocks = body.get("content") or []
    if not isinstance(blocks, list):
        raise RunnerError(f"malformed content in response: {type(blocks).__name__}")
    chunks = [
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    if body.get("stop_reason") == "max_tokens":
        raise RunnerError("response truncated by max_tokens; refusing partial review JSON")
    text = "".join(chunks).strip()
    if not text:
        raise RunnerError("model returned no text content")
    return text


def _usage_record(body: dict) -> dict:
    usage = body.get("usage") if isinstance(body, dict) else None
    record: dict = {}
    if isinstance(usage, dict):
        for field in USAGE_FIELDS:
            value = usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                record[field] = value
    return record


def _header_record(headers) -> dict:
    record: dict = {}
    if headers is None:
        return record
    for name in META_RESPONSE_HEADERS:
        value = headers.get(name)
        if value is not None:
            record[name] = str(value)[:200]
    return record


def _with_status(error: RunnerError, http_status: int) -> RunnerError:
    error.http_status = http_status
    return error


def call_messages(
    base_url: str,
    model: str,
    prompt: str,
    max_tokens: int,
    effort: str,
    timeout: float,
    api_key: str,
    quota_skip: float = 0.0,
    messages: list | None = None,
) -> tuple[str, dict]:
    """POST once. Returns ``(text, response_meta)`` or raises a RunnerError."""
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/messages",
        data=build_payload(model, prompt, max_tokens, effort, messages=messages),
        headers={
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": api_key,
        },
        method="POST",
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            response_headers = _header_record(response.headers)
            http_status = int(getattr(response, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        # Model text is never accepted from a non-2xx response.
        raw = exc.read().decode("utf-8", errors="replace")
        if 300 <= exc.code < 400:
            raise _with_status(RunnerError(
                f"redirect refused (HTTP {exc.code} -> {exc.headers.get('location', '?')}): "
                "the proxy endpoint must answer directly"
            ), exc.code) from exc
        retry_after = parse_retry_after(exc.headers.get("retry-after"))
        if exc.code == 429:
            classified = classify_error_body(raw, retry_after=retry_after, quota_skip=quota_skip)
            if isinstance(classified, TransientError):
                # Quota (skip to the fallback) or a paced burst retry: both
                # carry the window the proxy announced.
                raise _with_status(classified, exc.code) from exc
            if retry_after is not None and quota_skip > 0 and retry_after >= quota_skip:
                # Non-JSON or unrecognised body: the header alone is decisive.
                raise _with_status(QuotaExhaustedError(
                    f"HTTP 429 with Retry-After {retry_after:.0f}s: {raw[:300]}", retry_after=retry_after
                ), exc.code) from exc
            raise _with_status(TransientError(f"HTTP 429: {raw[:500]}", retry_after=retry_after), exc.code) from exc
        if transient_status(exc.code):
            raise _with_status(TransientError(f"HTTP {exc.code}: {raw[:500]}"), exc.code) from exc
        raise _with_status(classify_error_body(raw), exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise TransientError(f"endpoint failed mid-response: {exc}") from exc
    try:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RunnerError(f"non-JSON response: {raw[:500]}") from exc
        if not isinstance(body, dict):
            raise RunnerError(f"non-object JSON response: {raw[:500]}")
        text = extract_text(body, quota_skip=quota_skip)
    except RunnerError as error:
        # A 2xx that still failed (error envelope, empty, truncated) says so.
        raise _with_status(error, http_status)
    meta = {
        "http_status": http_status,
        "response_id": str(body.get("id", ""))[:120] or None,
        "response_model": str(body.get("model", ""))[:120] or None,
        "stop_reason": str(body.get("stop_reason", ""))[:60] or None,
        "usage": _usage_record(body),
        "response_headers": response_headers,
    }
    return text, meta


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _clip_error(exc: BaseException) -> str:
    return scrub_paths(" ".join(str(exc).split()))[:MAX_META_ERROR_CHARS]


class RunMeta:
    """Everything about the run that is safe to publish on a PR."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.started = time.monotonic()
        self.record: dict = {
            "schema": META_SCHEMA,
            "runner": "llm_runner",
            "started_at": _now_iso(),
            "ladder": [args.model] + list(args.fallback_model),
            "effort": args.effort or None,
            "max_tokens": int(args.max_tokens),
            "quota_skip_seconds": float(args.quota_skip_seconds),
            "output_contract": getattr(args, "output_contract", "text"),
            "attempts": [],
            "outcome": "failed",
            "model": None,
            "response_model": None,
            "response_id": None,
            "stop_reason": None,
            "usage": {},
            "response_headers": {},
            "error": None,
        }

    def attempt(self, model: str, attempt: int, outcome: str, started: float, **fields) -> None:
        entry = {
            "model": model,
            "attempt": attempt,
            "outcome": outcome,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        entry.update({key: value for key, value in fields.items() if value is not None})
        self.record["attempts"].append(entry)

    def succeeded(self, model: str, response_meta: dict) -> None:
        self.record["outcome"] = "succeeded"
        self.record["model"] = model
        for key in ("response_model", "response_id", "stop_reason", "usage", "response_headers"):
            self.record[key] = response_meta.get(key)

    def failed(self, error: str) -> None:
        self.record["outcome"] = "failed"
        self.record["error"] = error[:2000]

    def finish(self) -> dict:
        self.record["finished_at"] = _now_iso()
        self.record["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        return self.record


def run_with_retries(args: argparse.Namespace, meta: RunMeta | None = None) -> str:
    check_base_url(args.base_url)
    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    meta = meta or RunMeta(args)
    deadline = time.monotonic() + args.total_budget
    models = [args.model] + list(args.fallback_model)
    errors: list[str] = []
    quota_skip = float(args.quota_skip_seconds)
    expect_json = getattr(args, "output_contract", "text") == "json"
    # Start of the HTTP request in flight: a transient error from the JSON
    # nudge must be timed from the nudge, not from the reply before it.
    request_started = [0.0]

    def deliver(model: str, attempt: int, text: str, response_meta: dict, effort: str, started: float, fallback_pending: bool, success_outcome: str = "succeeded") -> str | None:
        """Accept the answer, or nudge once when the contract is JSON and none came.

        A chat model given an agent-style prompt may answer with its plan
        ("I'll start by reading the files") and stop; one follow-up turn on the
        same model turns that into the object. Still no object → this model is
        done, the ladder moves on (None).
        """
        if not expect_json or has_json_object(text):
            meta.attempt(model, attempt, success_outcome, started, http_status=response_meta.get("http_status"))
            meta.succeeded(model, response_meta)
            return text
        meta.attempt(model, attempt, "no_json", started, http_status=response_meta.get("http_status"), error=_clip_error(RunnerError(text)))
        # The nudge is a second HTTP call: it gets what is left of the budget,
        # minus the reserve a fallback still needs, never a fresh attempt timeout.
        nudge_timeout = deadline - time.monotonic()
        if fallback_pending and args.fallback_reserve > 0:
            nudge_timeout -= args.fallback_reserve
        nudge_timeout = min(args.timeout, nudge_timeout)
        if nudge_timeout <= 1.0:
            errors.append(f"{model} attempt {attempt}: no JSON object in the reply and no budget left to nudge")
            return None
        if fallback_pending and nudge_timeout < args.min_attempt_seconds:
            # Same floor as an attempt: an untried fallback gets the time.
            errors.append(
                f"{model} attempt {attempt}: no JSON object in the reply; nudge skipped, its {nudge_timeout:.0f}s "
                f"window is shorter than the {args.min_attempt_seconds:g}s minimum attempt; the time goes to the fallback"
            )
            return None
        errors.append(f"{model} attempt {attempt}: no JSON object in the reply; nudging once")
        nudge_started = time.monotonic()
        request_started[0] = nudge_started
        history = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": text},
            {"role": "user", "content": JSON_NUDGE},
        ]
        try:
            text2, meta2 = call_messages(
                args.base_url, model, prompt, args.max_tokens, effort, nudge_timeout, args.api_key,
                quota_skip=quota_skip, messages=history,
            )
        except TransientError:
            # Quota (skip / wait out the window) and paced retries are the
            # outer handlers' business; they keep the window on record.
            raise
        except RunnerError as exc:
            errors.append(f"{model} nudge: {exc}")
            meta.attempt(model, attempt, "nudge_failed", nudge_started, http_status=exc.http_status, error=_clip_error(exc))
            return None
        merged = dict(meta2)
        usage = {}
        for key in set((response_meta.get("usage") or {}).keys()) | set((meta2.get("usage") or {}).keys()):
            usage[key] = int((response_meta.get("usage") or {}).get(key) or 0) + int((meta2.get("usage") or {}).get(key) or 0)
        merged["usage"] = usage
        if has_json_object(text2):
            meta.attempt(model, attempt, "succeeded_after_nudge", nudge_started, http_status=meta2.get("http_status"))
            meta.succeeded(model, merged)
            meta.record["nudged"] = True
            return text2
        errors.append(f"{model}: still no JSON object after the nudge")
        meta.attempt(model, attempt, "no_json_after_nudge", nudge_started, http_status=meta2.get("http_status"), error=_clip_error(RunnerError(text2)))
        meta.record["usage"] = usage
        return None

    def attempt_window(fallback_pending: bool) -> tuple[float, float]:
        """``(budget left, seconds the next attempt may take)`` right now."""
        remaining = deadline - time.monotonic()
        window = min(args.timeout, remaining)
        if fallback_pending and args.fallback_reserve > 0:
            # Never let an earlier model consume the whole budget while a
            # fallback model is still untried.
            window = min(window, max(1.0, remaining - args.fallback_reserve))
        return remaining, window

    def too_short(window: float, outlasted: float, ceiling: float | None) -> str | None:
        """Why ``window`` is not worth a request while a fallback is untried, else None."""
        if window < args.min_attempt_seconds:
            return f"its {window:.0f}s window is shorter than the {args.min_attempt_seconds:g}s minimum attempt"
        if ceiling is not None and outlasted >= ceiling:
            return f"its last attempt ran {outlasted:.0f}s, into the proxy's {ceiling:g}s cut for this model"
        if window < outlasted:
            return f"its {window:.0f}s window is shorter than the {outlasted:.0f}s its last attempt ran"
        return None

    for index, model in enumerate(models):
        fallback_pending = index < len(models) - 1
        # How long this model's last failed request ran. A retry given less
        # time than that rarely does better, so while a fallback is untried the
        # time goes to the fallback: a model cut at the proxy's ceiling (codex
        # ends every request at ~900s) must not be retried on what is left and
        # the next model starved to a 1s window.
        outlasted = 0.0
        ceiling = model_ceiling(args.model_ceiling, model)
        for attempt in range(1, args.attempts + 1):
            remaining, attempt_timeout = attempt_window(fallback_pending)
            if remaining <= 1:
                errors.append(f"total budget of {args.total_budget}s exhausted")
                raise RunnerError("all models exhausted:\n" + "\n".join(errors))
            why = too_short(attempt_timeout, outlasted, ceiling) if fallback_pending else None
            if why:
                errors.append(f"{model} attempt {attempt}: skipped, {why}; the time goes to the fallback")
                break
            started = time.monotonic()
            request_started[0] = started
            try:
                text, response_meta = call_messages(
                    args.base_url,
                    model,
                    prompt,
                    args.max_tokens,
                    args.effort,
                    attempt_timeout,
                    args.api_key,
                    quota_skip=quota_skip,
                )
                text = deliver(model, attempt, text, response_meta, args.effort, started, fallback_pending)
                if text is None:
                    break
                if errors:
                    print("llm_runner: succeeded on " + model + " after earlier failures:\n" + "\n".join(errors), file=sys.stderr)
                return text
            except QuotaExhaustedError as exc:
                # No usage left on this model: the proxy told us how long the
                # window lasts. With a fallback still untried, skip to it now.
                errors.append(f"{model} attempt {attempt}: quota exhausted: {exc}")
                meta.attempt(
                    model, attempt, "quota_exhausted", started,
                    http_status=exc.http_status, retry_after_seconds=exc.retry_after, error=_clip_error(exc),
                )
                window = exc.retry_after
                remaining = deadline - time.monotonic()
                if not fallback_pending and window is not None and attempt < args.attempts and window + 5.0 <= remaining:
                    # Last rung: nothing to skip to, and the window ends inside
                    # the budget, so waiting it out beats failing the agent now.
                    errors.append(f"{model}: waiting {window:.0f}s for the usage window")
                    time.sleep(window)
                    continue
                break
            except TransientError as exc:
                outlasted = time.monotonic() - request_started[0]
                errors.append(f"{model} attempt {attempt}: {exc}")
                paced = exc.retry_after
                meta.attempt(
                    model, attempt, "rate_limited" if paced is not None else "transient_error", started,
                    http_status=exc.http_status, retry_after_seconds=paced, error=_clip_error(exc),
                )
                if attempt < args.attempts and fallback_pending and too_short(attempt_window(True)[1], outlasted, ceiling):
                    # The retry would be skipped anyway: no backoff, the next
                    # pass records why and moves to the fallback.
                    continue
                if attempt < args.attempts:
                    delay = args.retry_delay * attempt
                    sleep_cap = deadline - time.monotonic()
                    if fallback_pending and args.fallback_reserve > 0:
                        sleep_cap -= args.fallback_reserve
                    pause = delay + random.uniform(0, delay)
                    if paced is not None and quota_skip > 0 and paced < quota_skip:
                        # Only a window the burst rule classified is waited out;
                        # a long one under a disabled skip keeps the plain backoff.
                        pause = max(pause, paced)
                    time.sleep(min(pause, max(0.0, sleep_cap)))
            except RunnerError as exc:
                if args.effort and "output_config" in str(exc):
                    errors.append(f"{model}: {exc} (retrying without output_config)")
                    meta.attempt(model, attempt, "effort_rejected", started, http_status=exc.http_status, error=_clip_error(exc))
                    retry_started = time.monotonic()
                    try:
                        text, response_meta = call_messages(
                            args.base_url, model, prompt, args.max_tokens, "",
                            attempt_timeout, args.api_key, quota_skip=quota_skip,
                        )
                        text = deliver(
                            model, attempt, text, response_meta, "", retry_started, fallback_pending,
                            success_outcome="succeeded_without_effort",
                        )
                        if text is None:
                            # A later rung may succeed with the requested effort;
                            # the dropped-effort marker belongs to a delivered answer only.
                            break
                        meta.record["effort_applied"] = None
                        return text
                    except (TransientError, RunnerError) as exc2:
                        errors.append(f"{model} no-effort retry: {exc2}")
                        meta.attempt(model, attempt, "failed_without_effort", retry_started, http_status=exc2.http_status, error=_clip_error(exc2))
                        break
                errors.append(f"{model}: {exc}")
                meta.attempt(model, attempt, "permanent_error", started, http_status=exc.http_status, error=_clip_error(exc))
                break
    raise RunnerError("all models exhausted:\n" + "\n".join(errors))


def ceiling_spec(value: str) -> tuple[str, float]:
    """``PREFIX=SECONDS`` for ``--model-ceiling``."""
    prefix, sep, seconds = value.partition("=")
    try:
        parsed = float(seconds)
    except ValueError:
        parsed = 0.0
    if not sep or not prefix or not parsed > 0:
        raise argparse.ArgumentTypeError(f"expected PREFIX=SECONDS with SECONDS > 0, got {value!r}")
    return prefix, parsed


def model_ceiling(specs: list[tuple[str, float]], model: str) -> float | None:
    """The cut for ``model``: the longest matching prefix wins."""
    matches = [(len(prefix), seconds) for prefix, seconds in specs if model.startswith(prefix)]
    return max(matches)[1] if matches else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="llmux Messages-API agent runner")
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--response-file", required=True)
    parser.add_argument(
        "--meta-file",
        default="",
        help="write a JSON record of the run (models tried, usage, timings) here; no secrets",
    )
    parser.add_argument("--base-url", default="http://localhost:3456")
    parser.add_argument("--model", required=True)
    parser.add_argument("--fallback-model", action="append", default=[])
    parser.add_argument("--effort", default="")
    parser.add_argument("--api-key", default="review-of-legends", help="x-api-key sent to the proxy")
    parser.add_argument("--api-key-file", help="read the proxy key from a private file instead of process arguments")
    parser.add_argument("--max-tokens", type=int, default=32000)
    parser.add_argument(
        "--output-contract",
        choices=("text", "markdown", "json"),
        default="text",
        help="json: a reply without a JSON object is nudged once on the same model before moving on",
    )
    parser.add_argument("--timeout", type=float, default=600, help="seconds per HTTP attempt")
    parser.add_argument("--attempts", type=int, default=2, help="attempts per model on transient errors")
    parser.add_argument("--retry-delay", type=float, default=5, help="base backoff seconds between attempts")
    parser.add_argument(
        "--fallback-reserve",
        type=float,
        default=300,
        help="wall-clock seconds kept in reserve for untried fallback models (0 disables)",
    )
    parser.add_argument(
        "--min-attempt-seconds",
        type=float,
        default=1.0,
        help=(
            "while a fallback model is still untried, a model whose attempt window (the budget "
            "left minus --fallback-reserve) would be shorter than this is skipped, its time left to the fallback"
        ),
    )
    parser.add_argument(
        "--model-ceiling",
        type=ceiling_spec,
        action="append",
        default=[],
        metavar="PREFIX=SECONDS",
        help=(
            "the proxy ends any request to a model whose id starts with PREFIX after about SECONDS "
            "(llmux's codex route: gpt-=900). While a fallback is untried, such a model is not retried "
            "after an attempt that ran that long: the retry would be cut at the same point"
        ),
    )
    parser.add_argument(
        "--total-budget",
        type=float,
        default=850,
        help="wall-clock seconds for the whole model ladder (keep below the service agent timeout)",
    )
    parser.add_argument(
        "--quota-skip-seconds",
        type=float,
        default=DEFAULT_QUOTA_SKIP_SECONDS,
        help=(
            "a 429 whose Retry-After is at least this many seconds is a used-up usage window: "
            "move to the next model at once instead of retrying (0 disables)"
        ),
    )
    return parser


def _write_private(path: str, text: str) -> None:
    # README guarantees run artifacts are written 0600; write_text would
    # follow the umask instead.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def write_meta(args: argparse.Namespace, meta: RunMeta) -> None:
    if not args.meta_file:
        return
    try:
        _write_private(args.meta_file, json.dumps(meta.finish(), ensure_ascii=False, sort_keys=True))
    except OSError as exc:
        print(f"llm_runner: could not write meta file: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    meta = RunMeta(args)
    try:
        if args.api_key_file:
            args.api_key = Path(args.api_key_file).read_text(encoding="utf-8").strip()
            if not args.api_key:
                raise RunnerError("proxy key file is empty")
        text = run_with_retries(args, meta)
        _write_private(args.response_file, text)
    except (RunnerError, OSError) as exc:
        meta.failed(scrub_paths(str(exc)))
        write_meta(args, meta)
        print(f"llm_runner: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - the route must be visible even for a bug
        meta.failed(scrub_paths(f"unexpected {type(exc).__name__}: {exc}"))
        write_meta(args, meta)
        raise
    write_meta(args, meta)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
