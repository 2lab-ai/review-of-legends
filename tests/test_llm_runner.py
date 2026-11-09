from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import llm_runner

# The shipped ladder (config/reviewer.json model_policy); pinned to the config
# by tests/test_model_ladder.py so transport tests exercise the real model ids.
PRIMARY_MODEL = "gpt-6-astra[1m]"
FALLBACK_MODEL = "grok-4.6"
EFFORT = "xhigh"


def _message_body(text: str) -> bytes:
    return json.dumps(
        {"type": "message", "content": [{"type": "text", "text": text}]}
    ).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    # Populated per-test: list of (status, body_bytes) consumed in order;
    # the last entry repeats once exhausted. Optional per-class extra
    # headers (e.g. a redirect Location) are attached to every response.
    script: list[tuple[int, bytes]] = []
    short_write: bool = False
    requests: list[dict] = []
    extra_headers: dict[str, str] = {}
    delays: list[float] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        payload = json.loads(self.rfile.read(length))
        payload["_api_key"] = self.headers.get("x-api-key")
        payload["_path"] = self.path
        type(self).requests.append(payload)
        if type(self).delays:
            time.sleep(type(self).delays.pop(0))
        status, body = self.script[0]
        if len(self.script) > 1:
            self.script.pop(0)
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body) + (16 if type(self).short_write else 0)))
        for name, value in self.extra_headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence test output
        pass


class _TargetHandler(_Handler):
    """Separate mock server standing in for a redirect target."""

    script: list[tuple[int, bytes]] = []
    short_write: bool = False
    requests: list[dict] = []
    extra_headers: dict[str, str] = {}

    def do_GET(self):  # noqa: N802 — count any contact, not just POSTs
        type(self).requests.append({"method": "GET"})
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()


class _RunnerCase(unittest.TestCase):
    """Mock proxy + argv harness shared by the runner test classes."""

    def setUp(self):
        _Handler.script = []
        _Handler.requests = []
        _Handler.extra_headers = {}
        _Handler.delays = []
        _Handler.short_write = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.tmp = tempfile.TemporaryDirectory()
        self.prompt_file = Path(self.tmp.name) / "prompt.md"
        self.response_file = Path(self.tmp.name) / "response.json"
        self.prompt_file.write_text("review this", encoding="utf-8")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _run(self, extra: list[str] | None = None) -> int:
        argv = [
            "--prompt-file", str(self.prompt_file),
            "--response-file", str(self.response_file),
            "--base-url", self.base_url,
            "--model", PRIMARY_MODEL,
            "--effort", EFFORT,
            "--fallback-model", FALLBACK_MODEL,
            "--attempts", "2",
            "--retry-delay", "0",
            "--timeout", "10",
        ]
        return llm_runner.main(argv + (extra or []))


class LlmRunnerTest(_RunnerCase):
    def test_success_writes_response_file(self):
        _Handler.script = [(200, _message_body('{"summary": "ok", "findings": []}'))]
        self.assertEqual(self._run(), 0)
        self.assertEqual(
            self.response_file.read_text(encoding="utf-8"),
            '{"summary": "ok", "findings": []}',
        )
        request = _Handler.requests[0]
        self.assertEqual(request["model"], PRIMARY_MODEL)
        self.assertEqual(request["output_config"], {"effort": EFFORT})
        self.assertEqual(request["messages"][0]["content"], "review this")

    def test_transient_error_falls_back_to_next_model(self):
        _Handler.script = [
            (500, b'{"type":"error","error":{"type":"api_error","message":"boom"}}'),
            (500, b'{"type":"error","error":{"type":"api_error","message":"boom"}}'),
            (200, _message_body('{"summary": "fallback"}')),
        ]
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.response_file.read_text(encoding="utf-8"), '{"summary": "fallback"}')
        models = [request["model"] for request in _Handler.requests]
        self.assertEqual(models, [PRIMARY_MODEL, PRIMARY_MODEL, FALLBACK_MODEL])

    def test_proxy_error_body_with_200_is_retried_then_fails_closed(self):
        _Handler.script = [
            (200, b'{"type":"error","error":{"type":"proxy_error","message":"upstream is temporarily rate-limiting"}}'),
        ]
        self.assertEqual(self._run(), 1)
        self.assertFalse(self.response_file.exists())
        # 2 attempts on the primary + 2 on the fallback, all transient
        self.assertEqual(len(_Handler.requests), 4)

    def test_unreachable_endpoint_fails_closed(self):
        self.server.shutdown()
        self.server.server_close()
        self.assertEqual(self._run(), 1)
        self.assertFalse(self.response_file.exists())

    def test_empty_content_is_permanent_failure_for_that_model(self):
        _Handler.script = [
            (200, _message_body("")),
            (200, _message_body('{"summary": "second model"}')),
        ]
        self.assertEqual(self._run(), 0)
        models = [request["model"] for request in _Handler.requests]
        self.assertEqual(models, [PRIMARY_MODEL, FALLBACK_MODEL])

    VENDOR_REJECTION = (
        "--base-url points at direct vendor endpoint api.anthropic.com; this runner only talks to a proxy"
    )

    def _run_against_vendor(self, base_url: str) -> None:
        """The guard refuses before any request: the opener is never called.

        A missing guard would POST to the real vendor host, so the opener is
        replaced by one that fails the test instead of leaving the machine.
        """
        meta_file = Path(self.tmp.name) / "meta.json"
        argv = [
            "--prompt-file", str(self.prompt_file),
            "--response-file", str(self.response_file),
            "--base-url", base_url,
            "--model", PRIMARY_MODEL,
            "--meta-file", str(meta_file),
        ]
        stderr = io.StringIO()
        with mock.patch.object(
            llm_runner._OPENER, "open", side_effect=AssertionError(f"request sent to {base_url}")
        ) as opener, contextlib.redirect_stderr(stderr):
            code = llm_runner.main(argv)
        self.assertEqual(code, 1)
        opener.assert_not_called()
        self.assertEqual(_Handler.requests, [])
        self.assertFalse(self.response_file.exists())
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        self.assertEqual(meta["attempts"], [])
        self.assertEqual(meta["error"], self.VENDOR_REJECTION)
        self.assertIn(self.VENDOR_REJECTION, stderr.getvalue())

    def test_direct_vendor_base_url_is_rejected_before_any_request(self):
        self._run_against_vendor("https://api.anthropic.com")

    def test_rate_limit_error_body_is_transient(self):
        _Handler.script = [
            (429, b'{"type":"error","error":{"type":"rate_limit_error","message":"Error"}}'),
            (200, b'{"type":"error","error":{"type":"rate_limit_error","message":"Error"}}'),
            (200, _message_body('{"summary": "after limits"}')),
        ]
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(_Handler.requests), 3)

    def test_unlisted_5xx_is_transient(self):
        _Handler.script = [
            (599, b"upstream exploded"),
            (200, _message_body('{"summary": "recovered"}')),
        ]
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(_Handler.requests), 2)

    def test_non_2xx_with_message_shaped_body_is_never_accepted(self):
        # A 400 whose body looks like a normal message must not produce output.
        _Handler.script = [
            (400, _message_body('{"summary": "smuggled"}')),
            (400, _message_body('{"summary": "smuggled"}')),
        ]
        self.assertEqual(self._run(), 1)
        self.assertFalse(self.response_file.exists())

    def test_total_budget_bounds_the_ladder(self):
        _Handler.script = [
            (503, b'{"type":"error","error":{"type":"overloaded_error","message":"busy"}}'),
        ]
        self.assertEqual(self._run(["--total-budget", "0.5"]), 1)
        self.assertLessEqual(len(_Handler.requests), 1)
        self.assertFalse(self.response_file.exists())

    def test_total_budget_stops_mid_ladder_with_deterministic_clock(self):
        _Handler.script = [
            (503, b'{"type":"error","error":{"type":"overloaded_error","message":"busy"}}'),
        ]
        # Each fake clock read advances 40s: with a 100s budget the ladder
        # affords the first attempts but exhausts before it can finish all
        # 2 models x 2 attempts.
        clock = {"now": 0.0}

        def fake_monotonic() -> float:
            clock["now"] += 40.0
            return clock["now"]

        from unittest import mock

        with mock.patch.object(llm_runner.time, "monotonic", fake_monotonic), mock.patch.object(
            llm_runner.time, "sleep", lambda *_: None
        ):
            code = self._run(["--total-budget", "100"])
        self.assertEqual(code, 1)
        self.assertLess(len(_Handler.requests), 4)
        self.assertFalse(self.response_file.exists())

    def test_redirect_is_refused_not_followed(self):
        # Stand up a real redirect target and prove it is never contacted.
        _TargetHandler.requests = []
        _TargetHandler.script = [(200, _message_body('{"summary": "smuggled via redirect"}'))]
        target = ThreadingHTTPServer(("127.0.0.1", 0), _TargetHandler)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()
        try:
            target_url = f"http://127.0.0.1:{target.server_address[1]}/v1/messages"
            _Handler.extra_headers = {"location": target_url}
            _Handler.script = [
                (302, b""),
                (302, b""),
            ]
            meta_file = Path(self.tmp.name) / "meta.json"
            self.assertEqual(self._run(["--meta-file", str(meta_file)]), 1)
            attempts = json.loads(meta_file.read_text(encoding="utf-8"))["attempts"]
            self.assertEqual([(a["outcome"], a["http_status"]) for a in attempts], [("permanent_error", 302)] * 2)
            # Redirect is a permanent refusal per model: one request per
            # model, no retries, and zero requests to the redirect target.
            self.assertEqual(len(_Handler.requests), 2)
            self.assertEqual(_TargetHandler.requests, [])
            self.assertFalse(self.response_file.exists())
        finally:
            target.shutdown()
            target.server_close()

    def test_trailing_dot_vendor_host_is_rejected(self):
        # The FQDN form names the same host; the rejection names it without the dot.
        self._run_against_vendor("https://api.anthropic.com./v1")

    def test_non_object_json_body_moves_to_fallback_without_crashing(self):
        _Handler.script = [
            (200, b'["valid json", "but not an object"]'),
            (200, _message_body("fallback ok")),
        ]
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.response_file.read_text(encoding="utf-8"), "fallback ok")
        self.assertEqual(_Handler.requests[1]["model"], FALLBACK_MODEL)

    def test_fallback_reserve_keeps_budget_for_untried_fallback(self):
        # Primary hangs past its reserved slice; the fallback model must still
        # get a live attempt instead of being starved by the primary.
        _Handler.delays = [3.0]
        _Handler.script = [(200, _message_body("fallback ok"))]
        rc = self._run([
            "--attempts", "1",
            "--total-budget", "5",
            "--fallback-reserve", "4",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(self.response_file.read_text(encoding="utf-8"), "fallback ok")
        self.assertEqual(len(_Handler.requests), 2)
        self.assertEqual(_Handler.requests[1]["model"], FALLBACK_MODEL)

    def test_string_error_payload_moves_to_fallback_without_crashing(self):
        _Handler.script = [
            (200, b'{"type": "error", "error": "nope"}'),
            (200, _message_body("fallback ok")),
        ]
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.response_file.read_text(encoding="utf-8"), "fallback ok")
        self.assertEqual(_Handler.requests[1]["model"], FALLBACK_MODEL)

    def test_null_content_moves_to_fallback_without_crashing(self):
        _Handler.script = [
            (200, b'{"content": null}'),
            (200, _message_body("fallback ok")),
        ]
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.response_file.read_text(encoding="utf-8"), "fallback ok")

    def test_response_file_is_written_0600_and_key_is_configurable(self):
        _Handler.script = [(200, _message_body("ok"))]
        self.assertEqual(self._run(["--api-key", "custom-key"]), 0)
        self.assertEqual(self.response_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(_Handler.requests[0]["_api_key"], "custom-key")

    def test_post_path_is_v1_messages(self):
        _Handler.script = [(200, _message_body("ok"))]
        self.assertEqual(self._run(), 0)
        self.assertEqual(_Handler.requests[0]["_path"], "/v1/messages")

    def test_incomplete_read_is_transient_and_retried(self):
        _Handler.short_write = True
        _Handler.script = [(200, _message_body("never delivered"))]
        rc = self._run(["--attempts", "1", "--total-budget", "30"])
        self.assertEqual(rc, 1)  # every response is truncated -> ladder exhausts, no crash
        self.assertFalse(self.response_file.exists())
        self.assertGreaterEqual(len(_Handler.requests), 2)  # fallback model was still tried

    def test_max_tokens_truncation_is_not_a_success(self):
        truncated = json.dumps({"content": [{"type": "text", "text": "{\"partial\":"}], "stop_reason": "max_tokens"}).encode()
        _Handler.script = [(200, truncated), (200, _message_body("fallback ok"))]
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.response_file.read_text(encoding="utf-8"), "fallback ok")

    def test_output_config_rejection_retries_same_model_without_effort(self):
        err = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": "output_config is not supported"}}).encode()
        _Handler.script = [(200, err), (200, _message_body("ok sans effort"))]
        self.assertEqual(self._run(), 0)
        self.assertEqual(self.response_file.read_text(encoding="utf-8"), "ok sans effort")
        self.assertEqual(_Handler.requests[1]["model"], PRIMARY_MODEL)
        self.assertNotIn("output_config", _Handler.requests[1])

    def test_six_concurrent_runners_all_succeed(self):
        _Handler.script = [(200, _message_body('{"summary": "ok", "findings": []}'))]
        results: list[int] = []
        missing_files: list[str] = []
        errors: list[BaseException] = []

        def one(i: int) -> None:
            try:
                response = Path(self.tmp.name) / f"response-{i}.json"
                code = llm_runner.main([
                    "--prompt-file", str(self.prompt_file),
                    "--response-file", str(response),
                    "--base-url", self.base_url,
                    "--model", PRIMARY_MODEL,
                    "--timeout", "10",
                ])
                results.append(code)
                if code == 0 and not response.is_file():
                    missing_files.append(str(response))
            except BaseException as exc:  # propagate to the main thread
                errors.append(exc)

        threads = [threading.Thread(target=one, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(missing_files, [])
        self.assertEqual(results, [0] * 6)
        self.assertEqual(len(_Handler.requests), 6)


class BuildPayloadTest(unittest.TestCase):
    """The ladder is Codex-only, so no request may carry vendor billing or
    client-identity metadata: the payload is exactly the model, the budget,
    the prompt, and (when asked for) the effort."""

    def test_configured_models_send_no_identity_metadata(self):
        for model in (PRIMARY_MODEL, FALLBACK_MODEL):
            with self.subTest(model=model):
                raw = llm_runner.build_payload(model, "review this", 32000, "max")
                payload = json.loads(raw)
                self.assertNotIn("system", payload)
                self.assertNotIn("metadata", payload)
                lowered = raw.decode("utf-8").lower()
                for marker in ("billing", "cc_version", "cc_entrypoint", "anthropic"):
                    self.assertNotIn(marker, lowered)

    def test_payload_is_model_budget_prompt_and_effort_only(self):
        payload = json.loads(
            llm_runner.build_payload(PRIMARY_MODEL, "review this", 32000, "max")
        )
        self.assertEqual(
            payload,
            {
                "model": PRIMARY_MODEL,
                "max_tokens": 32000,
                "messages": [{"role": "user", "content": "review this"}],
                "output_config": {"effort": "max"},
            },
        )

    def test_runner_holds_no_vendor_identity_code_path(self):
        # Not just absent from the payload: the constant and its
        # model-conditional branch are gone, so no future model id can
        # re-enable a vendor billing identity by name.
        self.assertFalse(hasattr(llm_runner, "CLAUDE_BILLING_IDENTITY"))
        source = Path(llm_runner.__file__).read_text(encoding="utf-8").lower()
        markers = [m for m in ("billing", "cc_version", "cc_entrypoint") if m in source]
        self.assertEqual(markers, [])

    def test_no_effort_drops_output_config_entirely(self):
        payload = json.loads(
            llm_runner.build_payload(FALLBACK_MODEL, "review this", 32000, "")
        )
        self.assertEqual(
            payload,
            {
                "model": FALLBACK_MODEL,
                "max_tokens": 32000,
                "messages": [{"role": "user", "content": "review this"}],
            },
        )




class QuotaAndMetaTest(_RunnerCase):
    """Quota-aware fallback and the published meta record (.prd/model-policy/trace.md A)."""

    QUOTA_BODY = (
        b'{"type":"error","error":{"type":"rate_limit_error",'
        b'"message":"All 2 eligible accounts are rate-limited right now; retry in 3600s."}}'
    )

    def _meta(self) -> dict:
        return json.loads((Path(self.tmp.name) / "meta.json").read_text(encoding="utf-8"))

    def _run_meta(self, extra: list[str] | None = None) -> int:
        return self._run(["--meta-file", str(Path(self.tmp.name) / "meta.json")] + (extra or []))

    def test_429_with_long_retry_after_skips_to_the_fallback_without_a_retry(self):
        _Handler.extra_headers = {"Retry-After": "3600"}
        _Handler.script = [(429, self.QUOTA_BODY), (200, _message_body('{"summary": "grok"}'))]
        self.assertEqual(self._run_meta(), 0)
        self.assertEqual([r["model"] for r in _Handler.requests], [PRIMARY_MODEL, FALLBACK_MODEL])
        meta = self._meta()
        self.assertEqual(meta["outcome"], "succeeded")
        self.assertEqual(meta["model"], FALLBACK_MODEL)
        self.assertEqual(meta["ladder"], [PRIMARY_MODEL, FALLBACK_MODEL])
        self.assertEqual(meta["effort"], EFFORT)
        self.assertEqual([a["outcome"] for a in meta["attempts"]], ["quota_exhausted", "succeeded"])
        self.assertEqual(meta["attempts"][0]["retry_after_seconds"], 3600.0)
        self.assertEqual(meta["attempts"][0]["http_status"], 429)

    def test_quota_wording_without_retry_after_also_skips(self):
        _Handler.script = [(429, self.QUOTA_BODY), (200, _message_body('{"summary": "grok"}'))]
        self.assertEqual(self._run_meta(), 0)
        self.assertEqual([r["model"] for r in _Handler.requests], [PRIMARY_MODEL, FALLBACK_MODEL])

    def test_short_retry_after_429_is_still_retried_on_the_same_model(self):
        _Handler.extra_headers = {"Retry-After": "2"}
        burst = b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}'
        _Handler.script = [(429, burst), (200, _message_body('{"summary": "ok"}'))]
        self.assertEqual(self._run_meta(), 0)
        self.assertEqual([r["model"] for r in _Handler.requests], [PRIMARY_MODEL, PRIMARY_MODEL])
        self.assertEqual([a["outcome"] for a in self._meta()["attempts"]], ["rate_limited", "succeeded"])

    def test_quota_skip_can_be_disabled(self):
        _Handler.extra_headers = {"Retry-After": "3600"}
        burst = b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}'
        _Handler.script = [(429, burst), (200, _message_body('{"summary": "ok"}'))]
        started = time.monotonic()
        self.assertEqual(self._run_meta(["--quota-skip-seconds", "0"]), 0)
        self.assertLess(time.monotonic() - started, 30, "a disabled skip must not pace on a long window")
        self.assertEqual([r["model"] for r in _Handler.requests], [PRIMARY_MODEL, PRIMARY_MODEL])

    def test_meta_records_usage_ids_and_headers_on_success(self):
        body = json.dumps(
            {
                "type": "message",
                "id": "msg_01",
                "model": "grok-4.6-upstream",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": '{"summary": "ok"}'}],
                "usage": {
                    "input_tokens": 1200,
                    "output_tokens": 340,
                    "cache_read_input_tokens": 1000,
                    "cache_creation_input_tokens": 0,
                    "service_tier": "not-an-int",
                },
            }
        ).encode("utf-8")
        _Handler.extra_headers = {"request-id": "req_9", "x-llmux-compatibility": "codex", "set-cookie": "no"}
        _Handler.script = [(200, body)]
        self.assertEqual(self._run_meta(), 0)
        meta = self._meta()
        self.assertEqual(meta["usage"], {
            "input_tokens": 1200,
            "output_tokens": 340,
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 0,
        })
        self.assertEqual(meta["response_id"], "msg_01")
        self.assertEqual(meta["response_model"], "grok-4.6-upstream")
        self.assertEqual(meta["stop_reason"], "end_turn")
        self.assertEqual(meta["response_headers"], {"request-id": "req_9", "x-llmux-compatibility": "codex"})
        self.assertEqual(meta["max_tokens"], 32000)
        self.assertIn("elapsed_seconds", meta)
        self.assertIn("started_at", meta)
        self.assertIn("finished_at", meta)
        raw = (Path(self.tmp.name) / "meta.json").read_text(encoding="utf-8")
        self.assertNotIn("review-of-legends", raw, "the api key must never reach the meta record")
        self.assertNotIn("review this", raw, "prompt text must never reach the meta record")
        self.assertNotIn(self.tmp.name, raw, "no filesystem path may reach the meta record")

    SHORT_BURST = (
        b'{"type":"error","error":{"type":"rate_limit_error",'
        b'"message":"All 3 eligible accounts are rate-limited right now; retry in 1s."}}'
    )

    def _single_model(self, extra: list[str]) -> int:
        return llm_runner.main([
            "--prompt-file", str(self.prompt_file),
            "--response-file", str(self.response_file),
            "--base-url", self.base_url,
            "--model", PRIMARY_MODEL,
            "--attempts", "2",
            "--retry-delay", "0",
            "--timeout", "10",
            "--meta-file", str(Path(self.tmp.name) / "meta.json"),
        ] + extra)

    def test_short_retry_after_is_a_burst_and_retries_the_same_model(self):
        """Six agents starting at once trip a seconds-long 429: pace, do not skip to the fallback."""
        _Handler.extra_headers = {"Retry-After": "1"}
        _Handler.script = [(429, self.SHORT_BURST), (200, _message_body("after the burst"))]
        started = time.monotonic()
        self.assertEqual(self._run_meta(), 0)
        self.assertGreaterEqual(time.monotonic() - started, 0.9, "the retry waits out the announced window")
        self.assertEqual([r["model"] for r in _Handler.requests], [PRIMARY_MODEL, PRIMARY_MODEL])
        meta = self._meta()
        self.assertEqual([a["outcome"] for a in meta["attempts"]], ["rate_limited", "succeeded"])
        self.assertEqual(meta["attempts"][0]["retry_after_seconds"], 1.0)

    def test_generic_429_wording_with_a_long_retry_after_moves_to_the_fallback_at_once(self):
        """The header alone decides: any 429 wording with Retry-After >= the skip threshold is quota."""
        _Handler.extra_headers = {"Retry-After": "3600"}
        body = b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}'
        _Handler.script = [(429, body), (200, _message_body("grok"))]
        self.assertEqual(self._run_meta(), 0)
        self.assertEqual([r["model"] for r in _Handler.requests], [PRIMARY_MODEL, FALLBACK_MODEL])
        self.assertEqual([a["outcome"] for a in self._meta()["attempts"]], ["quota_exhausted", "succeeded"])

    def test_non_json_429_with_a_long_retry_after_moves_to_the_fallback_at_once(self):
        _Handler.extra_headers = {"Retry-After": "3600"}
        _Handler.script = [(429, b"<html>Too Many Requests</html>"), (200, _message_body("grok"))]
        self.assertEqual(self._run_meta(), 0)
        self.assertEqual([r["model"] for r in _Handler.requests], [PRIMARY_MODEL, FALLBACK_MODEL])
        self.assertEqual(self._meta()["attempts"][0]["outcome"], "quota_exhausted")

    def test_non_json_429_with_a_short_retry_after_is_paced_on_the_same_model(self):
        _Handler.extra_headers = {"Retry-After": "1"}
        _Handler.script = [(429, b"<html>Too Many Requests</html>"), (200, _message_body("ok"))]
        self.assertEqual(self._run_meta(), 0)
        self.assertEqual([r["model"] for r in _Handler.requests], [PRIMARY_MODEL, PRIMARY_MODEL])
        self.assertEqual([a["outcome"] for a in self._meta()["attempts"]], ["rate_limited", "succeeded"])

    def test_failure_record_names_no_filesystem_path(self):
        missing = Path(self.tmp.name) / "does-not-exist" / "prompt.md"
        code = llm_runner.main([
            "--prompt-file", str(missing),
            "--response-file", str(self.response_file),
            "--base-url", self.base_url,
            "--model", PRIMARY_MODEL,
            "--meta-file", str(Path(self.tmp.name) / "meta.json"),
        ])
        self.assertNotEqual(code, 0)
        meta = self._meta()
        self.assertEqual(meta["outcome"], "failed")
        self.assertNotIn(self.tmp.name, json.dumps(meta))
        self.assertIn("No such file", meta["error"])
        self.assertEqual(llm_runner.scrub_paths("http://localhost:3456/v1/messages"), "http://localhost:3456/v1/messages")

    def test_burst_rule_applies_to_a_200_status_error_envelope(self):
        _Handler.script = [(200, self.SHORT_BURST), (200, _message_body("ok"))]
        self.assertEqual(self._run_meta(), 0)
        self.assertEqual([r["model"] for r in _Handler.requests], [PRIMARY_MODEL, PRIMARY_MODEL])
        self.assertEqual([a["outcome"] for a in self._meta()["attempts"]], ["rate_limited", "succeeded"])

    def test_attempt_errors_are_scrubbed_and_routes_are_kept(self):
        self.assertEqual(llm_runner.scrub_paths("404 for /v1/messages at /tmp/x/y/prompt.md"), "404 for /v1/messages at <path>")
        self.assertEqual(llm_runner.scrub_paths("open /var/folders/ab/cd/prompt.md"), "open <path>")

    PROSE = _message_body("I'll start by reading the changed files and surrounding code.")

    def test_json_contract_nudges_a_prose_reply_once_on_the_same_model(self):
        """grok answered the agent-style prompt with its plan and stopped (Pluto #508, 2026-09-17)."""
        def with_usage(text, n_in, n_out):
            return json.dumps({"type": "message", "content": [{"type": "text", "text": text}], "usage": {"input_tokens": n_in, "output_tokens": n_out}}).encode("utf-8")

        _Handler.script = [(200, with_usage("I'll start by reading the changed files and surrounding code.", 100, 10)), (200, with_usage('{"summary": "after nudge", "findings": []}', 200, 20))]
        self.assertEqual(self._run_meta(["--output-contract", "json"]), 0)
        self.assertEqual(self.response_file.read_text(encoding="utf-8"), '{"summary": "after nudge", "findings": []}')
        self.assertEqual([r["model"] for r in _Handler.requests], [PRIMARY_MODEL, PRIMARY_MODEL])
        turns = _Handler.requests[1]["messages"]
        self.assertEqual([t["role"] for t in turns], ["user", "assistant", "user"])
        self.assertIn("reading the changed files", turns[1]["content"])
        self.assertIn("no JSON object", turns[2]["content"])
        self.assertEqual(_Handler.requests[1].get("output_config"), {"effort": EFFORT}, "the nudge keeps the effort")
        meta = self._meta()
        self.assertEqual([a["outcome"] for a in meta["attempts"]], ["no_json", "succeeded_after_nudge"])
        self.assertTrue(meta["nudged"])
        self.assertEqual(meta["output_contract"], "json")
        self.assertEqual((meta["usage"]["input_tokens"], meta["usage"]["output_tokens"]), (300, 30), "both calls' usage is counted")

    def test_json_contract_moves_on_when_the_nudge_still_yields_prose(self):
        _Handler.script = [(200, self.PROSE), (200, self.PROSE), (200, _message_body('{"summary": "fallback"}'))]
        self.assertEqual(self._run_meta(["--output-contract", "json"]), 0)
        self.assertEqual([r["model"] for r in _Handler.requests], [PRIMARY_MODEL, PRIMARY_MODEL, FALLBACK_MODEL])
        self.assertEqual([a["outcome"] for a in self._meta()["attempts"]], ["no_json", "no_json_after_nudge", "succeeded"])

    def test_braces_inside_prose_do_not_pass_the_json_gate(self):
        """A plan that quotes code must still be nudged; a fenced object must not."""
        self.assertFalse(llm_runner.has_json_object("I'll read handler.py, e.g. the `if (!ok) { return; }` branch first."))
        self.assertTrue(llm_runner.has_json_object('```json\n{"summary": "ok", "findings": []}\n```'))
        self.assertTrue(llm_runner.has_json_object('Here it is: {"summary": "ok"} — done'))
        self.assertFalse(llm_runner.has_json_object('[{"summary": "not an object"}]'))
        _Handler.script = [(200, _message_body("Plan: check `if (!ok) { return; }` in handler.py")), (200, _message_body('{"summary": "ok"}'))]
        self.assertEqual(self._run_meta(["--output-contract", "json"]), 0)
        self.assertEqual([a["outcome"] for a in self._meta()["attempts"]], ["no_json", "succeeded_after_nudge"])

    def test_quota_during_the_nudge_keeps_its_window_and_moves_to_the_fallback(self):
        _Handler.extra_headers = {"Retry-After": "3600"}
        _Handler.script = [(200, self.PROSE), (429, self.QUOTA_BODY), (200, _message_body('{"summary": "grok"}'))]
        self.assertEqual(self._run_meta(["--output-contract", "json"]), 0)
        self.assertEqual([r["model"] for r in _Handler.requests], [PRIMARY_MODEL, PRIMARY_MODEL, FALLBACK_MODEL])
        meta = self._meta()
        self.assertEqual([a["outcome"] for a in meta["attempts"]], ["no_json", "quota_exhausted", "succeeded"])
        self.assertEqual(meta["attempts"][1]["retry_after_seconds"], 3600.0)

    def test_nudge_is_skipped_when_the_budget_is_gone(self):
        """No second call with a fresh attempt timeout: the fallback keeps its reserve."""
        _Handler.script = [(200, self.PROSE), (200, _message_body('{"summary": "grok"}'))]
        code = llm_runner.main([
            "--prompt-file", str(self.prompt_file), "--response-file", str(self.response_file), "--base-url", self.base_url,
            "--model", PRIMARY_MODEL, "--fallback-model", FALLBACK_MODEL, "--attempts", "1", "--retry-delay", "0",
            "--timeout", "10", "--total-budget", "3", "--fallback-reserve", "2.5",
            "--output-contract", "json", "--meta-file", str(Path(self.tmp.name) / "meta.json"),
        ])
        self.assertEqual(code, 0)
        self.assertEqual([r["model"] for r in _Handler.requests], [PRIMARY_MODEL, FALLBACK_MODEL], "no nudge call was made")
        self.assertEqual([a["outcome"] for a in self._meta()["attempts"]], ["no_json", "succeeded"])

    def test_dropped_effort_marker_is_not_left_behind_when_the_fallback_runs_with_effort(self):
        err = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": "output_config is not supported"}}).encode()
        _Handler.script = [(200, err), (200, self.PROSE), (200, self.PROSE), (200, _message_body('{"summary": "grok with effort"}'))]
        self.assertEqual(self._run_meta(["--output-contract", "json"]), 0)
        self.assertEqual([r["model"] for r in _Handler.requests], [PRIMARY_MODEL, PRIMARY_MODEL, PRIMARY_MODEL, FALLBACK_MODEL])
        self.assertEqual(_Handler.requests[3].get("output_config"), {"effort": EFFORT})
        meta = self._meta()
        self.assertNotIn("effort_applied", meta, "the fallback ran with the requested effort")
        self.assertEqual([a["outcome"] for a in meta["attempts"]], ["effort_rejected", "no_json", "no_json_after_nudge", "succeeded"])
        self.assertEqual(meta["model"], FALLBACK_MODEL)

    def test_markdown_and_text_contracts_never_nudge(self):
        for contract in ("markdown", "text"):
            with self.subTest(contract=contract):
                # A fresh harness per contract: tear the previous one down
                # first so no server or temp dir outlives its iteration.
                self.tearDown()
                self.setUp()
                _Handler.script = [(200, self.PROSE)]
                self.assertEqual(self._run_meta(["--output-contract", contract]), 0)
                self.assertEqual(len(_Handler.requests), 1)
                self.assertEqual([a["outcome"] for a in self._meta()["attempts"]], ["succeeded"])

    def test_window_in_the_message_counts_without_a_header(self):
        _Handler.script = [(429, self.SHORT_BURST), (200, _message_body("ok"))]
        self.assertEqual(self._single_model([]), 0)
        self.assertEqual([a["outcome"] for a in self._meta()["attempts"]], ["rate_limited", "succeeded"])

    def test_last_rung_waits_out_a_window_that_fits_the_budget(self):
        """With no fallback left and the window inside the budget, wait instead of failing the agent."""
        _Handler.extra_headers = {"Retry-After": "1"}
        _Handler.script = [(429, self.SHORT_BURST), (200, _message_body("after the window"))]
        self.assertEqual(self._single_model(["--quota-skip-seconds", "0.5"]), 0)
        meta = self._meta()
        self.assertEqual([a["outcome"] for a in meta["attempts"]], ["quota_exhausted", "succeeded"])
        self.assertEqual(self.response_file.read_text(encoding="utf-8"), "after the window")

    def test_last_rung_gives_up_on_a_window_beyond_the_budget(self):
        _Handler.extra_headers = {"Retry-After": "3600"}
        _Handler.script = [(429, self.QUOTA_BODY)]
        self.assertNotEqual(self._single_model([]), 0)
        self.assertEqual([a["outcome"] for a in self._meta()["attempts"]], ["quota_exhausted"])
        self.assertEqual(len(_Handler.requests), 1)

    def test_generic_quota_wording_on_an_api_error_keeps_its_retries(self):
        body = b'{"type":"error","error":{"type":"api_error","message":"upstream quota accounting hiccup"}}'
        _Handler.script = [(502, body), (200, _message_body("ok"))]
        self.assertEqual(self._run_meta(), 0)
        self.assertEqual([r["model"] for r in _Handler.requests], [PRIMARY_MODEL, PRIMARY_MODEL])
        self.assertEqual([a["outcome"] for a in self._meta()["attempts"]], ["transient_error", "succeeded"])

    def test_meta_records_the_dropped_effort_when_the_provider_rejects_it(self):
        err = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": "output_config is not supported"}}).encode()
        _Handler.script = [(200, err), (200, _message_body("ok sans effort"))]
        self.assertEqual(self._run_meta(), 0)
        meta = self._meta()
        self.assertEqual(meta["outcome"], "succeeded")
        self.assertEqual(meta["model"], PRIMARY_MODEL)
        self.assertEqual(meta["effort"], EFFORT, "the requested effort stays on record")
        self.assertIn("effort_applied", meta)
        self.assertIsNone(meta["effort_applied"], "the effort that actually ran is none")
        self.assertEqual([a["outcome"] for a in meta["attempts"]], ["effort_rejected", "succeeded_without_effort"])
        self.assertEqual({a["model"] for a in meta["attempts"]}, {PRIMARY_MODEL})

    def test_meta_records_a_failed_no_effort_retry_before_moving_on(self):
        err = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": "output_config is not supported"}}).encode()
        _Handler.script = [(200, err), (400, err), (200, _message_body("fallback"))]
        self.assertEqual(self._run_meta(), 0)
        meta = self._meta()
        self.assertEqual(meta["model"], FALLBACK_MODEL)
        self.assertEqual([a["outcome"] for a in meta["attempts"]][:2], ["effort_rejected", "failed_without_effort"])
        self.assertNotIn("effort_applied", meta, "the fallback ran with the requested effort")

    def test_meta_is_written_with_the_failure_route_when_every_model_fails(self):
        _Handler.extra_headers = {"Retry-After": "3600"}
        _Handler.script = [(429, self.QUOTA_BODY)]
        self.assertEqual(self._run_meta(), 1)
        self.assertFalse(self.response_file.exists())
        meta = self._meta()
        self.assertEqual(meta["outcome"], "failed")
        self.assertIsNone(meta["model"])
        self.assertEqual([a["model"] for a in meta["attempts"]], [PRIMARY_MODEL, FALLBACK_MODEL])
        self.assertTrue(all(a["outcome"] == "quota_exhausted" for a in meta["attempts"]))
        self.assertIn("all models exhausted", meta["error"])

    def test_rejected_request_records_its_status_and_the_upstream_detail(self):
        """The dashboard shows why a hop failed: llmux refused astra on a ChatGPT account (review-host, 2026-10-13)."""
        detail = 'codex upstream: {"detail":"The \'gpt-6-astra\' model is not supported when using Codex with a ChatGPT account."}'
        rejected = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": detail}}).encode()
        _Handler.script = [(400, rejected), (200, _message_body('{"summary": "grok"}'))]
        self.assertEqual(self._run_meta(), 0)
        first = self._meta()["attempts"][0]
        self.assertEqual((first["model"], first["outcome"], first["http_status"]), (PRIMARY_MODEL, "permanent_error", 400))
        self.assertEqual(first["error"], "invalid_request_error: " + detail)

    def test_every_failed_http_hop_carries_its_status(self):
        burst = b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}'
        _Handler.extra_headers = {"Retry-After": "1"}
        _Handler.script = [(429, burst), (503, b"upstream down"), (200, self.QUOTA_BODY), (200, _message_body("grok"))]
        self.assertEqual(self._run_meta(["--attempts", "3"]), 0)
        attempts = self._meta()["attempts"]
        self.assertEqual([(a["outcome"], a.get("http_status")) for a in attempts], [
            ("rate_limited", 429), ("transient_error", 503), ("quota_exhausted", 200), ("succeeded", 200),
        ], "a quota announced in a 200 envelope is not recorded as a 429")

    def test_a_reply_without_json_is_recorded_up_to_the_error_cap(self):
        # Each reply carries its own marker well past the old 160-char cut and runs past the cap.
        first = "I'll start by reading the changed files. " * 20 + "FIRST-REPLY-MARKER " + "then the tests. " * 120
        second = "Sure, first I need to open handler.py. " * 20 + "SECOND-REPLY-MARKER " + "then the routes. " * 120
        _Handler.script = [(200, _message_body(first)), (200, _message_body(second)), (200, _message_body('{"summary": "grok"}'))]
        self.assertEqual(self._run_meta(["--output-contract", "json"]), 0)
        no_json, after_nudge = self._meta()["attempts"][:2]
        self.assertEqual((no_json["outcome"], after_nudge["outcome"]), ("no_json", "no_json_after_nudge"))
        self.assertEqual(llm_runner.MAX_META_ERROR_CHARS, 2000)
        for hop, reply, marker in ((no_json, first, "FIRST-REPLY-MARKER"), (after_nudge, second, "SECOND-REPLY-MARKER")):
            self.assertEqual(hop["error"], " ".join(reply.split())[: llm_runner.MAX_META_ERROR_CHARS])
            self.assertGreater(hop["error"].index(marker), 160)
            self.assertEqual(hop["http_status"], 200)

    def test_meta_file_is_private(self):
        _Handler.script = [(200, _message_body('{"summary": "ok"}'))]
        self.assertEqual(self._run_meta(), 0)
        mode = (Path(self.tmp.name) / "meta.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


class LadderBudgetTest(unittest.TestCase):
    """The live ladder against a codex route that cuts every request at 900s.

    Replays the 2026-10-26 a production PR server-architect failure with
    the shipped budget: a fake clock, and a fake upstream where each model needs
    ``need`` seconds and the codex route answers HTTP 502 at the 900s cut.
    """

    CUT = 900.0
    CUT_502 = (
        'HTTP 502: {"error":{"message":"transient upstream error: codex upstream stream failed: '
        'error decoding response body","type":"proxy_error"},"type":"error"}'
    )
    LADDER = ["gpt-6-astra[1m]", "gpt-6.1-sol[1m]", "grok-4.7[1m]"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prompt_file = Path(self.tmp.name) / "prompt.md"
        self.prompt_file.write_text("review this", encoding="utf-8")
        self.now = 0.0
        self.calls: list[tuple[str, float]] = []

    def tearDown(self):
        self.tmp.cleanup()

    def _args(self, extra: list[str] | None = None):
        argv = [
            "--prompt-file", str(self.prompt_file),
            "--response-file", str(Path(self.tmp.name) / "response.json"),
            "--model", self.LADDER[0],
            "--fallback-model", self.LADDER[1],
            "--fallback-model", self.LADDER[2],
            "--effort", "max",
            "--output-contract", "json",
            # config/reviewer.json agent_runner
            "--timeout", "1500", "--total-budget", "1700", "--fallback-reserve", "300",
        ]
        return llm_runner.build_parser().parse_args(argv + (extra or []))

    def _run(self, need: dict, args, fail_fast: dict | None = None):
        """Run the ladder; ``fail_fast[model]`` = seconds after which that model's
        first attempt fails with an overload 502 regardless of its window."""
        fail_fast = dict(fail_fast or {})

        def fake_call(base_url, model, prompt, max_tokens, effort, timeout, api_key, quota_skip=0.0, messages=None):
            self.calls.append((model, round(timeout, 1)))
            if model in fail_fast:
                self.now += fail_fast.pop(model)
                raise llm_runner._with_status(llm_runner.TransientError(
                    'HTTP 502: {"error":{"message":"Our servers are currently overloaded.","type":"api_error"}}'
                ), 502)
            cut = self.CUT if model.startswith("gpt-") else float("inf")
            if need[model] <= min(timeout, cut):
                self.now += need[model]
                return '{"summary": "ok", "findings": []}', {"http_status": 200, "usage": {}}
            if cut <= timeout:
                self.now += cut + 1.6
                raise llm_runner._with_status(llm_runner.TransientError(self.CUT_502), 502)
            self.now += timeout
            raise llm_runner.TransientError("endpoint failed mid-response: timed out")

        def sleep(seconds):
            self.now += max(0.0, seconds)

        with mock.patch.object(llm_runner.time, "monotonic", lambda: self.now), \
                mock.patch.object(llm_runner.time, "sleep", sleep), \
                mock.patch.object(llm_runner.random, "uniform", lambda a, b: (a + b) / 2), \
                mock.patch.object(llm_runner, "call_messages", fake_call):
            try:
                return llm_runner.run_with_retries(args)
            except llm_runner.RunnerError as exc:
                return exc

    def test_a_cut_primary_hands_its_retry_time_to_the_next_model(self):
        # 2026-10-26: astra needs more than the cut, sol answers in 450s. The
        # shipped ladder retried astra with 490s and sent sol two 1s windows.
        result = self._run({"gpt-6-astra[1m]": 950.0, "gpt-6.1-sol[1m]": 450.0, "grok-4.7[1m]": 400.0},
                           self._args(["--min-attempt-seconds", "60"]))
        self.assertEqual(result, '{"summary": "ok", "findings": []}', f"calls: {self.calls}")
        self.assertEqual([m for m, _ in self.calls], [self.LADDER[0], self.LADDER[1]])
        self.assertGreaterEqual(self.calls[1][1], 490.0, "sol gets what astra's retry would have had")

    def test_no_model_is_sent_a_window_shorter_than_the_minimum(self):
        # Nobody answers: every request that goes out must still have had a
        # real window, and the last model keeps its reserve.
        result = self._run({"gpt-6-astra[1m]": 950.0, "gpt-6.1-sol[1m]": 950.0, "grok-4.7[1m]": 950.0},
                           self._args(["--min-attempt-seconds", "60"]))
        self.assertIsInstance(result, llm_runner.RunnerError)
        self.assertTrue(self.calls, "the ladder made no request")
        self.assertGreaterEqual(min(t for _, t in self.calls), 60.0, f"calls: {self.calls}")
        self.assertEqual(self.calls[-1][0], self.LADDER[2])
        self.assertGreaterEqual(self.calls[-1][1], 290.0, "grok keeps the fallback reserve")
        self.assertIn("skipped", str(result), "the skipped model is named in the failure")

    def test_a_starved_middle_model_is_skipped_so_the_last_one_gets_the_time(self):
        # astra fails fast once (500s < the 892s retry window, so the retry is
        # worth it), then the retry times out on the budget down to the
        # reserve: sol would get a 1s window.
        result = self._run({"gpt-6-astra[1m]": 950.0, "gpt-6.1-sol[1m]": 450.0, "grok-4.7[1m]": 250.0},
                           self._args(["--min-attempt-seconds", "60"]),
                           fail_fast={"gpt-6-astra[1m]": 500.0})
        self.assertEqual(result, '{"summary": "ok", "findings": []}', f"calls: {self.calls}")
        self.assertEqual([m for m, _ in self.calls], [self.LADDER[0], self.LADDER[0], self.LADDER[2]])

    def test_a_fast_failure_is_still_retried_on_the_same_model(self):
        # A production PR correctness, 2026-10-26: 502 overloaded after 436s, the
        # retry answered in 760s. A failure shorter than the retry window keeps
        # its retry.
        result = self._run({"gpt-6.1-sol[1m]": 760.0, "grok-4.7[1m]": 100.0},
                           llm_runner.build_parser().parse_args([
                               "--prompt-file", str(self.prompt_file), "--response-file", "x",
                               "--model", "gpt-6.1-sol[1m]", "--fallback-model", "grok-4.7[1m]",
                               "--timeout", "1500", "--total-budget", "1700", "--fallback-reserve", "300",
                               "--min-attempt-seconds", "60",
                           ]),
                           fail_fast={"gpt-6.1-sol[1m]": 436.0})
        self.assertEqual(result, '{"summary": "ok", "findings": []}', f"calls: {self.calls}")
        self.assertEqual([m for m, _ in self.calls], ["gpt-6.1-sol[1m]", "gpt-6.1-sol[1m]"])

    def test_the_last_model_still_retries_with_whatever_is_left(self):
        # Nothing to hand the time to: the last rung keeps the old behaviour.
        result = self._run({"grok-4.7[1m]": 20.0},
                           llm_runner.build_parser().parse_args([
                               "--prompt-file", str(self.prompt_file), "--response-file", "x",
                               "--model", "grok-4.7[1m]", "--timeout", "1500", "--total-budget", "300",
                               "--min-attempt-seconds", "60",
                           ]),
                           fail_fast={"grok-4.7[1m]": 260.0})
        self.assertEqual(result, '{"summary": "ok", "findings": []}', f"calls: {self.calls}")
        self.assertEqual(len(self.calls), 2)

    def _shipped_args(self):
        """The runner flags config/reviewer.json ships, placeholders filled with the ladder."""
        config = json.loads((ROOT / "config" / "reviewer.json").read_text(encoding="utf-8"))
        command = config["agent_runner"]["command"]
        extra = []
        for index, part in enumerate(command):
            if part in ("--timeout", "--total-budget", "--fallback-reserve", "--min-attempt-seconds", "--model-ceiling"):
                extra += [part, command[index + 1]]
        return self._args(extra), config

    def test_shipped_budget_lets_the_fallback_answer_after_a_cut_primary(self):
        # Measured 2026-10-26: gpt-6.1-sol answered the a production PR
        # server-architect prompt (458,868 bytes, effort max) in 791.1s through
        # llmux; astra was cut at the 900s ceiling twice on the same prompt.
        args, config = self._shipped_args()
        result = self._run({"gpt-6-astra[1m]": 950.0, "gpt-6.1-sol[1m]": 791.1, "grok-4.7[1m]": 400.0}, args)
        self.assertEqual(result, '{"summary": "ok", "findings": []}', f"calls: {self.calls}")
        self.assertEqual([m for m, _ in self.calls], [self.LADDER[0], self.LADDER[1]])
        self.assertLess(self.now, float(config["agent_timeout_seconds"]), "the service must not kill the agent first")

    def test_shipped_budget_fits_a_cut_primary_a_full_window_and_the_reserve(self):
        # A primary cut at the codex ceiling must still leave the next model a
        # full ceiling-long window and the last model its reserve; the whole
        # ladder must end before the service's agent timeout kills it.
        args, config = self._shipped_args()
        cut_attempt = self.CUT + 5.0
        self.assertGreaterEqual(args.total_budget, cut_attempt + self.CUT + args.fallback_reserve)
        self.assertLess(args.total_budget, float(config["agent_timeout_seconds"]))

    def test_without_the_ceiling_a_cut_primary_looks_worth_retrying(self):
        # Control for the shipped-budget replay: with room for a 998s retry
        # and no --model-ceiling, astra is retried (and cut at 900s again).
        no_ceiling = self._args(["--total-budget", "2200", "--min-attempt-seconds", "60"])
        self._run({"gpt-6-astra[1m]": 950.0, "gpt-6.1-sol[1m]": 791.1, "grok-4.7[1m]": 400.0}, no_ceiling)
        self.assertEqual([m for m, _ in self.calls].count(self.LADDER[0]), 2, f"calls: {self.calls}")

    def test_a_short_failure_on_a_ceiling_model_is_still_retried(self):
        # The ceiling only marks failures that ran to it: an overload 502
        # after 610s (a production PR astra, 2026-10-26) keeps its retry.
        args, _ = self._shipped_args()
        result = self._run({"gpt-6-astra[1m]": 384.0, "gpt-6.1-sol[1m]": 300.0, "grok-4.7[1m]": 100.0}, args,
                           fail_fast={"gpt-6-astra[1m]": 610.8})
        self.assertEqual(result, '{"summary": "ok", "findings": []}', f"calls: {self.calls}")
        self.assertEqual([m for m, _ in self.calls], [self.LADDER[0], self.LADDER[0]])

    def test_a_failed_nudge_is_timed_from_the_nudge_not_the_reply_before_it(self):
        # astra answers prose after 850s, the JSON nudge fails after 55s. No
        # request reached the 900s cut, so the retry must still run.
        script = [
            ("reply", 850.0, "I'll start by reading the files."),
            ("502", 55.0, None),
            ("reply", 100.0, '{"summary": "ok", "findings": []}'),
        ]

        def fake_call(base_url, model, prompt, max_tokens, effort, timeout, api_key, quota_skip=0.0, messages=None):
            self.calls.append((model, round(timeout, 1)))
            kind, seconds, text = script.pop(0)
            self.now += seconds
            if kind == "502":
                raise llm_runner._with_status(llm_runner.TransientError(self.CUT_502), 502)
            return text, {"http_status": 200, "usage": {}}

        args, _ = self._shipped_args()
        with mock.patch.object(llm_runner.time, "monotonic", lambda: self.now), \
                mock.patch.object(llm_runner.time, "sleep", lambda seconds: None), \
                mock.patch.object(llm_runner, "call_messages", fake_call):
            result = llm_runner.run_with_retries(args)
        self.assertEqual(result, '{"summary": "ok", "findings": []}', f"calls: {self.calls}")
        self.assertEqual([m for m, _ in self.calls], [self.LADDER[0]] * 3)

    def test_a_skipped_retry_does_not_back_off_first(self):
        # With a 200s base backoff, sleeping before a retry that is then
        # skipped would take up to 400s of the fallback's window (bot P2 on
        # PR #74). The fallback must start at once with the whole window.
        args, _ = self._shipped_args()
        args.retry_delay = 200.0
        result = self._run({"gpt-6-astra[1m]": 950.0, "gpt-6.1-sol[1m]": 791.1, "grok-4.7[1m]": 400.0}, args)
        self.assertEqual(result, '{"summary": "ok", "findings": []}', f"calls: {self.calls}")
        cut_at = self.CUT + 1.6
        expected = min(args.timeout, args.total_budget - cut_at - args.fallback_reserve)
        self.assertEqual(self.calls[1], (self.LADDER[1], round(expected, 1)), f"calls: {self.calls}")

    def test_a_nudge_under_the_minimum_window_is_skipped_for_the_fallback(self):
        # Bot P2 on PR #74: budget 400, reserve 300, minimum 60. The primary
        # answers prose after 80s; a nudge would get 20s. The fallback runs.
        script = [
            ("gpt-6-astra[1m]", 80.0, "I'll start by reading the files."),
            ("grok-4.7[1m]", 50.0, '{"summary": "ok", "findings": []}'),
        ]

        def fake_call(base_url, model, prompt, max_tokens, effort, timeout, api_key, quota_skip=0.0, messages=None):
            self.calls.append((model, round(timeout, 1)))
            expected_model, seconds, text = script.pop(0)
            self.assertEqual(model, expected_model, f"calls: {self.calls}")
            self.now += seconds
            return text, {"http_status": 200, "usage": {}}

        args = llm_runner.build_parser().parse_args([
            "--prompt-file", str(self.prompt_file), "--response-file", "x",
            "--model", "gpt-6-astra[1m]", "--fallback-model", "grok-4.7[1m]", "--output-contract", "json",
            "--timeout", "1500", "--total-budget", "400", "--fallback-reserve", "300", "--min-attempt-seconds", "60",
        ])
        with mock.patch.object(llm_runner.time, "monotonic", lambda: self.now), \
                mock.patch.object(llm_runner.time, "sleep", lambda seconds: None), \
                mock.patch.object(llm_runner, "call_messages", fake_call), \
                contextlib.redirect_stderr(io.StringIO()):
            result = llm_runner.run_with_retries(args)
        self.assertEqual(result, '{"summary": "ok", "findings": []}')
        self.assertEqual([m for m, _ in self.calls], ["gpt-6-astra[1m]", "grok-4.7[1m]"], "no 20s nudge was sent")

    def test_model_ceiling_spec_is_validated(self):
        parser = llm_runner.build_parser()
        base = ["--prompt-file", "p", "--response-file", "r", "--model", "m"]
        self.assertEqual(parser.parse_args(base + ["--model-ceiling", "gpt-=900"]).model_ceiling, [("gpt-", 900.0)])
        for bad in ("gpt-", "=900", "gpt-=0", "gpt-=x", "gpt-=-5"):
            with self.subTest(bad=bad), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(base + ["--model-ceiling", bad])
        specs = [("gpt-", 900.0), ("gpt-6-astra", 600.0)]
        self.assertEqual(llm_runner.model_ceiling(specs, "gpt-6-astra[1m]"), 600.0, "longest prefix wins")
        self.assertEqual(llm_runner.model_ceiling(specs, "gpt-6.1-sol[1m]"), 900.0)
        self.assertIsNone(llm_runner.model_ceiling(specs, "grok-4.7[1m]"))

    def test_every_runner_timeout_outlasts_the_ladder_budget(self):
        # The executive summary and the visual outline run the same runner
        # command; a service timeout under --total-budget kills the fallback
        # mid-answer (a review panel: both were left at 1800s).
        args, config = self._shipped_args()
        timeouts = {"agent_timeout_seconds": config["agent_timeout_seconds"]}
        for block in ("executive_summary", "visual_outline"):
            if "timeout_seconds" in config.get(block, {}):
                timeouts[f"{block}.timeout_seconds"] = config[block]["timeout_seconds"]
        for agent in config.get("agents", []):
            if "timeout_seconds" in agent and "runner" not in agent:
                timeouts[f"agents[{agent['id']}].timeout_seconds"] = agent["timeout_seconds"]
        for name, seconds in timeouts.items():
            with self.subTest(timeout=name):
                self.assertGreater(float(seconds), args.total_budget)

    def test_shipped_config_sets_a_minimum_attempt_window(self):
        command = json.loads((ROOT / "config" / "reviewer.json").read_text(encoding="utf-8"))["agent_runner"]["command"]
        self.assertIn("--min-attempt-seconds", command)
        self.assertGreaterEqual(float(command[command.index("--min-attempt-seconds") + 1]), 60.0)



if __name__ == "__main__":
    unittest.main()
