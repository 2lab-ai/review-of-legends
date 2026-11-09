"""Model policy resolution, runner argv rendering and PR metadata (.prd/model-policy/trace.md).

Contract tests derived from the trace: the policy object is validated once by
``validate_model_policy`` and reused by every tier; precedence is request >
dashboard > config > legacy argv; the runner's meta record is projected onto
agent rows and rendered on the progress comment and the review body.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import dashboard_execution as dx
from code_review_service import service as svc

POLICY = {"model": "gpt-6-astra[1m]", "effort": "xhigh", "fallback_models": ["grok-4.6"]}


class ValidateModelPolicyTest(unittest.TestCase):
    def test_accepts_the_shipped_ladder_and_normalises_case(self) -> None:
        policy = svc.validate_model_policy({"model": " gpt-6-astra[1m] ", "effort": "XHIGH", "fallback_models": ["grok-4.6"]})
        self.assertEqual(policy, POLICY)

    def test_effort_may_be_empty_or_missing_meaning_proxy_default(self) -> None:
        self.assertEqual(svc.validate_model_policy({"model": "grok-4.6"})["effort"], "")
        self.assertEqual(svc.validate_model_policy({"model": "grok-4.6", "effort": None, "fallback_models": None})["fallback_models"], [])

    def test_rejects_claude_anywhere_in_the_ladder_with_the_reason(self) -> None:
        for candidate in (
            {"model": "claude-opus-5"},
            {"model": "Claude-fable-5-1[1m]"},
            {"model": "grok-4.6", "fallback_models": ["claude-sonnet-5"]},
        ):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError) as caught:
                svc.validate_model_policy(candidate)
            self.assertIn("Claude models are not available", str(caught.exception))

    def test_rejects_shape_defects(self) -> None:
        bad = [
            "gpt-6-astra",
            {"model": ""},
            {"model": "gpt 6"},
            {"model": "a" * 81},
            {"model": "grok-4.6", "effort": "ludicrous"},
            {"model": "grok-4.6", "fallback_models": "grok-4.5"},
            {"model": "grok-4.6", "fallback_models": ["grok-4.6"]},
            {"model": "grok-4.6", "fallback_models": ["grok-4.5", "grok-4.5"]},
            {"model": "grok-4.6", "fallback_models": [f"m{i}" for i in range(9)]},
            {"model": "grok-4.6", "api_key": "x"},
        ]
        for candidate in bad:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                svc.validate_model_policy(candidate)

    def test_config_comment_keys_are_ignored(self) -> None:
        policy = svc.validate_model_policy({"_comment": "why", **POLICY})
        self.assertNotIn("_comment", policy)


class ResolveModelPolicyTest(unittest.TestCase):
    def test_precedence_request_over_dashboard_over_config(self) -> None:
        config = {"model_policy": POLICY}
        dashboard = {"model": "gpt-5.6-sol[1m]", "effort": "max", "fallback_models": []}
        request = {"model": "grok-4.6", "effort": "high", "fallback_models": ["gpt-5.5"]}
        self.assertEqual(svc.resolve_model_policy(config, stored=dashboard, override=request)["source"], "request")
        self.assertEqual(svc.resolve_model_policy(config, stored=dashboard, override=request)["model"], "grok-4.6")
        resolved = svc.resolve_model_policy(config, stored=dashboard)
        self.assertEqual((resolved["source"], resolved["model"], resolved["effort"]), ("dashboard", "gpt-5.6-sol[1m]", "max"))
        resolved = svc.resolve_model_policy(config)
        self.assertEqual((resolved["source"], resolved["model"]), ("config", "gpt-6-astra[1m]"))

    def test_invalid_override_falls_through_instead_of_running(self) -> None:
        config = {"model_policy": POLICY}
        resolved = svc.resolve_model_policy(config, stored={"model": "claude-opus-5"}, override={"model": ""})
        self.assertEqual(resolved["source"], "config")

    def test_legacy_argv_config_is_recovered_as_source_command(self) -> None:
        config = {"agent_runner": {"command": ["python3", "r.py", "--model", "gpt-6-astra[1m]", "--effort", "max", "--fallback-model", "gpt-5.6-sol[1m]"]}}
        resolved = svc.resolve_model_policy(config)
        self.assertEqual(resolved, {"model": "gpt-6-astra[1m]", "effort": "max", "fallback_models": ["gpt-5.6-sol[1m]"], "source": "command"})

    def test_no_policy_anywhere_uses_the_built_in_default(self) -> None:
        resolved = svc.resolve_model_policy({"agent_runner": {"command": ["true"]}})
        self.assertEqual(resolved["source"], "default")
        self.assertEqual(resolved["model"], svc.DEFAULT_MODEL_POLICY["model"])


class DescribeModelPolicyTest(unittest.TestCase):
    def test_agent_models_follow_the_ladder_sorted_by_agent(self) -> None:
        text = svc.describe_model_policy({**POLICY, "source": "dashboard",
                                          "agent_models": {"server-architect": "gpt-6-astra[1m]", "a": "grok-4.6"}})
        self.assertTrue(text.endswith(" · source: dashboard · agent models: a → `grok-4.6`, server-architect → `gpt-6-astra[1m]`"))

    def test_no_agent_models_adds_nothing(self) -> None:
        self.assertNotIn("agent models", svc.describe_model_policy({**POLICY, "agent_models": {}}))


class RenderRunnerCommandTest(unittest.TestCase):
    VALUES = {"prompt_file": "p.md", "response_file": "r.json", "meta_file": "m.json"}

    def test_token_expands_to_the_ladder_in_order(self) -> None:
        argv = svc.render_runner_command(["r", "--prompt-file", "{prompt_file}", "{model_policy_args}", "--x"], self.VALUES, {**POLICY, "source": "config"})
        self.assertEqual(argv, ["r", "--prompt-file", "p.md", "--model", "gpt-6-astra[1m]", "--effort", "xhigh", "--fallback-model", "grok-4.6", "--x"])

    def test_empty_effort_omits_the_effort_argument(self) -> None:
        argv = svc.render_runner_command(["{model_policy_args}"], self.VALUES, {"model": "grok-4.6", "effort": "", "fallback_models": []})
        self.assertEqual(argv, ["--model", "grok-4.6"])

    def test_legacy_argv_keeps_its_literals_for_a_config_policy(self) -> None:
        legacy = ["r", "--model", "gpt-6-astra[1m]", "--effort", "max", "--fallback-model", "gpt-5.6-sol[1m]"]
        argv = svc.render_runner_command(legacy, self.VALUES, {"model": "gpt-6-astra[1m]", "effort": "max", "fallback_models": ["gpt-5.6-sol[1m]"], "source": "command"})
        self.assertEqual(argv, legacy)

    def test_legacy_argv_is_overridden_by_an_operator_policy(self) -> None:
        legacy = ["r", "--model", "gpt-6-astra[1m]", "--effort", "max", "--fallback-model", "gpt-5.6-sol[1m]", "--timeout", "9"]
        argv = svc.render_runner_command(legacy, self.VALUES, {"model": "grok-4.6", "effort": "xhigh", "fallback_models": [], "source": "dashboard"})
        self.assertEqual(argv, ["r", "--timeout", "9", "--model", "grok-4.6", "--effort", "xhigh"])

    def test_legacy_argv_takes_an_agent_model_over_its_own_literals(self) -> None:
        legacy = ["r", "--model", "gpt-6-astra[1m]", "--effort", "max", "--fallback-model", "gpt-5.6-sol[1m]"]
        command_policy = {"model": "gpt-6-astra[1m]", "effort": "max", "fallback_models": ["gpt-5.6-sol[1m]"], "source": "command"}
        argv = svc.render_runner_command(legacy, self.VALUES, svc.agent_model_ladder(command_policy, "grok-4.6"))
        self.assertEqual(argv, ["r", "--model", "grok-4.6", "--effort", "max",
                                "--fallback-model", "gpt-6-astra[1m]", "--fallback-model", "gpt-5.6-sol[1m]"])


class MetaProjectionTest(unittest.TestCase):
    META = {
        "schema": 1,
        "ladder": ["gpt-6-astra[1m]", "grok-4.6"],
        "effort": "xhigh",
        "max_tokens": 32000,
        "outcome": "succeeded",
        "model": "grok-4.6",
        "response_model": "grok-4.6",
        "response_id": "msg_1",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 300, "cache_creation_input_tokens": 0},
        "elapsed_seconds": 41.25,
        "attempts": [
            {"model": "gpt-6-astra[1m]", "attempt": 1, "outcome": "quota_exhausted", "retry_after_seconds": 3600.0, "http_status": 429},
            {"model": "grok-4.6", "attempt": 1, "outcome": "succeeded", "http_status": 200},
        ],
    }

    def test_meta_projects_onto_bounded_agent_fields(self) -> None:
        fields = svc.llm_fields_from_meta(self.META)
        self.assertEqual(fields["model"], "grok-4.6")
        self.assertEqual(fields["effort"], "xhigh")
        self.assertEqual((fields["input_tokens"], fields["output_tokens"], fields["cache_read_tokens"], fields["cache_write_tokens"]), (1000, 200, 300, 0))
        self.assertEqual(fields["attempts"], 2)
        self.assertEqual(fields["route"], "gpt-6-astra[1m] ✗ quota (retry-after 3600s) → grok-4.6 ✓")
        self.assertEqual(fields["elapsed_seconds"], 41.25)
        paced = svc.route_from_meta({"attempts": [{"model": "grok-4.6", "outcome": "rate_limited", "retry_after_seconds": 9}, {"model": "grok-4.6", "outcome": "succeeded"}]})
        nudged = svc.route_from_meta({"attempts": [{"model": "grok-4.6", "outcome": "no_json"}, {"model": "grok-4.6", "outcome": "succeeded_after_nudge"}]})
        self.assertEqual(nudged, "grok-4.6 ✗ no JSON (nudged) → grok-4.6 ✓ (after nudge)")
        self.assertEqual(paced, "grok-4.6 ✗ rate-limited (retry-after 9s) → grok-4.6 ✓")
        self.assertEqual(fields["ladder"], ["gpt-6-astra[1m]", "grok-4.6"])
        self.assertEqual(svc.llm_fields_from_meta(None), {})

    def test_an_overridden_agents_whole_ladder_is_reported(self) -> None:
        """agent_model_ladder can put MAX_FALLBACK_MODELS + 1 fallbacks behind an agent's model."""
        policy = {"model": "m0", "effort": "", "fallback_models": [f"m{i}" for i in range(1, svc.MAX_FALLBACK_MODELS + 1)], "source": "config"}
        agent = svc.agent_model_ladder(policy, "astra")
        rungs = [agent["model"], *agent["fallback_models"]]
        self.assertEqual(len(rungs), svc.MAX_FALLBACK_MODELS + 2)
        self.assertEqual(svc.llm_fields_from_meta({"ladder": rungs})["ladder"], rungs, "the policy's last fallback is still reported")
        self.assertEqual(svc.llm_fields_from_meta({"ladder": [*rungs, "extra"]})["ladder"], rungs, "a longer ladder is still cut")

    def test_dropped_effort_is_published_as_no_effort(self) -> None:
        """The comment must not claim ``xhigh`` for a run the provider ran without it."""
        meta = dict(self.META, model="gpt-6-astra[1m]", effort_applied=None, attempts=[
            {"model": "gpt-6-astra[1m]", "attempt": 1, "outcome": "effort_rejected", "http_status": 400},
            {"model": "gpt-6-astra[1m]", "attempt": 1, "outcome": "succeeded_without_effort", "http_status": 200},
        ])
        fields = svc.llm_fields_from_meta(meta)
        self.assertEqual(fields["effort"], "")
        self.assertEqual(fields["route"], "gpt-6-astra[1m] effort rejected → gpt-6-astra[1m] ✓ (effort dropped)")
        self.assertEqual(svc.format_model_cell(fields), "gpt-6-astra[1m] · 2 attempts", "no effort in the cell, the attempts stay")

    def test_agent_rows_keep_the_llm_scalars_and_drop_junk(self) -> None:
        rows = dx.normalize_agent_rows({"correctness": {"status": "done", "summary": "ok", "model": "grok-4.6", "effort": "xhigh",
                                                          "input_tokens": 5, "output_tokens": -1, "elapsed_seconds": float("nan"),
                                                          "route": "x" * 500, "ladder": ["not", "a", "scalar"]}})
        row = rows[0]
        self.assertEqual(row["model"], "grok-4.6")
        self.assertEqual(row["input_tokens"], 5)
        self.assertNotIn("output_tokens", row)
        self.assertNotIn("elapsed_seconds", row)
        self.assertNotIn("ladder", row)
        self.assertLessEqual(len(row["route"]), 240)

    def test_totals_sum_agents_and_list_models_in_order(self) -> None:
        totals = svc.llm_totals([
            {"model": "grok-4.6", "input_tokens": 10, "output_tokens": 1},
            {"model": "gpt-6-astra[1m]", "input_tokens": 5, "output_tokens": 2, "cache_read_tokens": 4},
            {"model": "grok-4.6", "input_tokens": 1, "output_tokens": 1},
            {"status": "failed"},
        ])
        self.assertEqual(totals["input_tokens"], 16)
        self.assertEqual(totals["output_tokens"], 4)
        self.assertEqual(totals["cache_read_tokens"], 4)
        self.assertEqual(totals["agents_with_usage"], 3)
        self.assertEqual(totals["models_used"], ["grok-4.6", "gpt-6-astra[1m]"])

    def test_a_reused_agent_names_its_model_but_its_tokens_stay_with_the_attempt_that_spent_them(self) -> None:
        totals = svc.llm_totals([
            {"model": "grok-4.6", "input_tokens": 10, "output_tokens": 1},
            {"model": "gpt-6-astra[1m]", "input_tokens": 500, "output_tokens": 50, "reused": True},
        ])
        self.assertEqual((totals["input_tokens"], totals["output_tokens"], totals["agents_with_usage"]), (10, 1, 1))
        self.assertEqual(totals["models_used"], ["grok-4.6", "gpt-6-astra[1m]"])


class ProgressCommentMetadataTest(unittest.TestCase):
    def _entries(self):
        entries = svc.make_progress_entries([{"id": "correctness", "prompt_path": "x"}, {"id": "security", "prompt_path": "x"}])
        entries["correctness"].update({"status": "done", "summary": "P2 x 1", **svc.llm_fields_from_meta(MetaProjectionTest.META)})
        entries["security"].update({"status": "failed", "summary": "agent failed", "attempts": 2,
                                    "route": "gpt-6-astra[1m] ✗ quota (retry-after 3600s) → grok-4.6 ✗ transient", "outcome": "failed"})
        return entries

    def test_progress_comment_shows_policy_models_tokens_and_details(self) -> None:
        started = dt.datetime(2026, 9, 17, 1, 0, tzinfo=dt.timezone.utc)
        body = svc.build_progress_comment_body(
            {"progress_marker": svc.PROGRESS_MARKER}, {"number": 7}, "abcdef1234567890", self._entries(),
            "Reviewer agents running", started, policy={**POLICY, "source": "dashboard"},
            summary_meta={"model": "grok-4.6", "effort": "xhigh", "input_tokens": 100, "output_tokens": 20},
        )
        self.assertIn("**Model policy:** `gpt-6-astra[1m]` (effort xhigh) → `grok-4.6` · source: dashboard", body)
        self.assertIn("| 1 | correctness | Correctness / regressions | ✅ Done | grok-4.6 · xhigh · 2 attempts | in 1,000 / out 200 / cache read 300 | P2 x 1 |", body)
        self.assertIn("| 2 | security | Security / trust boundaries | ❌ Failed | — | — | agent failed |", body)
        self.assertIn("**Tokens so far:** in 1,100 / out 220 / cache read 300 (2 agent(s) reported usage) · **Models used:** `grok-4.6`", body)
        self.assertIn("<details>", body)
        self.assertIn("<summary>Model and usage metadata</summary>", body)
        self.assertIn("- **correctness**: model `grok-4.6`; effort xhigh; tokens in 1,000 / out 200 / cache read 300; max_tokens 32,000; stop end_turn; elapsed 41.2s; response id `msg_1`; route gpt-6-astra[1m] ✗ quota (retry-after 3600s) → grok-4.6 ✓", body)
        self.assertIn("- **security**: route gpt-6-astra[1m] ✗ quota (retry-after 3600s) → grok-4.6 ✗ transient; outcome failed", body)
        self.assertIn("- **executive-summary**: model `grok-4.6`; effort xhigh; tokens in 100 / out 20", body)

    def test_review_body_lists_models_and_tokens(self) -> None:
        results = [
            {"agent": "correctness", "summary": "ok", "findings": [], "llm": svc.llm_fields_from_meta(MetaProjectionTest.META)},
            {"agent": "security", "summary": "ok", "findings": [], "llm": {}},
        ]
        body = svc.build_review_body({}, {"number": 7, "head": {"sha": "abc"}}, results, [], 0, version="v-test", policy={**POLICY, "source": "config"})
        self.assertIn("Model policy: `gpt-6-astra[1m]` (effort xhigh) → `grok-4.6` · source: config", body)
        self.assertIn("Models used: `grok-4.6`", body)
        self.assertIn("Tokens (reviewer agents only): in 1,000 / out 200 / cache read 300 (1/2 runner invocations reported usage)", body)
        summary_meta = {"model": "grok-4.6", "input_tokens": 50, "output_tokens": 5, "outcome": "succeeded"}
        with_summary = svc.build_review_body({}, {"number": 7, "head": {"sha": "abc"}}, results, [], 0, version="v-test", policy={**POLICY, "source": "config"}, summary_meta=summary_meta)
        self.assertIn("Tokens (reviewer agents + executive summary): in 1,050 / out 205 / cache read 300 (2/3 runner invocations reported usage)", with_summary)

    def test_review_body_is_built_after_the_summary_runner_reports(self) -> None:
        """The posted review's header must include the summary runner's tokens (codex thread)."""
        from code_review_service import dashboard_execution as dx_mod
        from tests.test_dashboard_integration import FakeClient, FakeStore

        store = FakeStore()
        recorder = dx_mod.DashboardRecorder(store, logger=lambda message: None)
        config = {
            "agents": [{"id": "a", "prompt_path": "x"}],
            "agent_runner": {"command": ["r", "{model_policy_args}"]},
            "model_policy": POLICY,
            "min_successful_agents": 0, "max_review_comments": 30, "max_diff_chars": 180000,
            "progress_comment": {"enabled": False}, "approval": {"enabled": False}, "executive_summary": {"enabled": True},
        }
        posted: list = []

        def fake_summary(config, config_dir, source_dir, repo, pr, files, results, findings, artifact_root=None, policy=None, meta_sink=None):
            if meta_sink is not None:
                meta_sink.update({"model": "grok-4.6", "input_tokens": 70, "output_tokens": 7, "outcome": "succeeded"})
            return "summary text"

        with tempfile.TemporaryDirectory() as tmp:
            config["work_dir"] = str(Path(tmp) / "work")
            pr = {"number": 3, "head": {"sha": "abc123456789"}, "base": {"sha": "b" * 12}, "title": "t", "body": "", "user": {"login": "x"}, "html_url": "u"}
            with mock.patch.object(svc, "run_agent", lambda agent, *a, **k: {"agent": agent["id"], "summary": "ok", "findings": [], "llm": {"model": "grok-4.6", "input_tokens": 30, "output_tokens": 3, "outcome": "succeeded"}}), \
                 mock.patch.object(svc, "run_executive_summary_agent", fake_summary), \
                 mock.patch.object(svc, "build_executive_summary_body", lambda *a, **k: "es"), \
                 mock.patch.object(svc, "clone_pr_source", lambda repo, n, h, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True)), \
                 mock.patch.object(svc, "load_repository_rules", lambda *a, **k: ""), \
                 mock.patch.object(svc, "build_review_body", lambda *a, **k: posted.append(k.get("summary_meta")) or "body"):
                svc.review_pr(FakeClient(), config, Path(tmp) / "config" / "reviewer.json", pr, {"reviewed": {}}, recorder=recorder)
        self.assertEqual(posted, [{"model": "grok-4.6", "input_tokens": 70, "output_tokens": 7, "outcome": "succeeded"}])

    def test_metadata_block_is_absent_without_runner_metadata(self) -> None:
        body = svc.build_review_body({}, {"number": 7, "head": {"sha": "abc"}}, [{"agent": "a", "summary": "s", "findings": []}], [], 0, version="v")
        self.assertNotIn("Models used", body)
        self.assertNotIn("Model policy", body)


class RunAgentMetaTest(unittest.TestCase):
    """run_agent reads the runner's meta file and attaches it to the result."""

    def _config(self, work: Path, command: list[str]) -> dict:
        return {"agents": [], "agent_runner": {"command": command}, "work_dir": str(work), "agent_timeout_seconds": 30}

    def test_meta_file_lands_on_the_result_for_success_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agents").mkdir()
            (root / "agents" / "correctness.md").write_text("prompt", encoding="utf-8")
            run_dir = root / "run"
            run_dir.mkdir()
            writer = root / "fake_runner.py"
            writer.write_text(
                "import json, sys\n"
                "args = sys.argv[1:]\n"
                "meta = args[args.index('--meta-file') + 1]\n"
                "resp = args[args.index('--response-file') + 1]\n"
                "models = [args[i + 1] for i, a in enumerate(args) if a in ('--model', '--fallback-model')]\n"
                "json.dump({'model': models[-1], 'effort': args[args.index('--effort') + 1], 'ladder': models, 'outcome': 'succeeded',\n"
                "           'usage': {'input_tokens': 7, 'output_tokens': 3}, 'attempts': [{'model': models[-1], 'attempt': 1, 'outcome': 'succeeded'}]},\n"
                "          open(meta, 'w'))\n"
                "if models[-1] == 'fail-me':\n"
                "    sys.exit(3)\n"
                "open(resp, 'w').write(json.dumps({'summary': 'ok', 'findings': []}))\n",
                encoding="utf-8",
            )
            command = [sys.executable, str(writer), "--response-file", "{response_file}", "--meta-file", "{meta_file}", "{model_policy_args}"]
            agent = {"id": "correctness", "prompt_path": str(root / "agents" / "correctness.md")}
            pr = {"number": 1, "title": "t", "body": "", "head": {"sha": "abc"}}
            with mock.patch.object(svc, "build_agent_environ", lambda runner, artifact_dir, config_dir: dict(PATH="/usr/bin:/bin")):
                result = svc.run_agent(agent, self._config(root, command), root, run_dir, "o/r", pr, [], "", policy={"model": "grok-4.6", "effort": "xhigh", "fallback_models": []})
                self.assertNotIn("error", result)
                self.assertEqual(result["llm"]["model"], "grok-4.6")
                self.assertEqual(result["llm"]["input_tokens"], 7)
                self.assertEqual(result["llm"]["route"], "grok-4.6 ✓")

                failed = svc.run_agent(agent, self._config(root, command), root, run_dir, "o/r", pr, [], "", policy={"model": "fail-me", "effort": "low", "fallback_models": []})
                self.assertIn("error", failed)
                self.assertEqual(failed["llm"]["model"], "fail-me")
                self.assertEqual(failed["llm"]["effort"], "low")

    def test_timeout_error_names_the_timeout_and_never_the_argv(self) -> None:
        """str(TimeoutExpired) is the whole command line; the dashboard row would publish it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agents").mkdir()
            (root / "agents" / "correctness.md").write_text("prompt", encoding="utf-8")
            run_dir = root / "run"
            run_dir.mkdir()
            sleeper = root / "slow_runner.py"
            sleeper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            command = [sys.executable, str(sleeper), "--api-key", "argv-secret-value", "--prompt-file", "{prompt_file}", "{model_policy_args}"]
            config = dict(self._config(root, command), agent_timeout_seconds=1)
            agent = {"id": "correctness", "prompt_path": str(root / "agents" / "correctness.md")}
            pr = {"number": 1, "title": "t", "body": "", "head": {"sha": "abc"}}
            with mock.patch.object(svc, "build_agent_environ", lambda runner, artifact_dir, config_dir: dict(PATH="/usr/bin:/bin")):
                result = svc.run_agent(agent, config, root, run_dir, "o/r", pr, [], "", policy={"model": "grok-4.6", "effort": "xhigh", "fallback_models": []})
        self.assertEqual((result["summary"], result["error"]), ("agent timed out after 1s", "agent timed out after 1s"))
        row = dx.normalize_agent_rows({"correctness": {"status": "failed", "summary": result["summary"], "error": svc.agent_failure_text(result)}})[0]
        self.assertEqual(row["error"], "agent timed out after 1s")
        for leaked in ("argv-secret-value", "--prompt-file", "slow_runner.py", tmp):
            self.assertNotIn(leaked, json.dumps(result))


class FailureDetailTest(unittest.TestCase):
    """Why an agent or a model hop failed reaches the dashboard row, never the PR (2026-10-13)."""

    REJECTED = (
        'invalid_request_error: codex upstream: {"detail":"The \'gpt-6-astra\' model is not supported '
        'when using Codex with a ChatGPT account."}'
    )
    META = {
        "ladder": ["gpt-6-astra[1m]", "grok-4.6"],
        "effort": "xhigh",
        "outcome": "succeeded",
        "model": "grok-4.6",
        "elapsed_seconds": 350.1,
        "attempts": [
            {"model": "gpt-6-astra[1m]", "attempt": 1, "outcome": "permanent_error", "http_status": 400, "elapsed_seconds": 0.41, "error": REJECTED},
            {"model": "grok-4.6", "attempt": 1, "outcome": "succeeded", "http_status": 200, "elapsed_seconds": 349.7},
        ],
    }
    EXHAUSTED = "llm_runner: all models exhausted:\ngpt-6-astra[1m]: " + REJECTED + "\ngrok-4.6 attempt 1: HTTP 503: upstream down"

    def test_meta_projects_a_bounded_attempt_log(self) -> None:
        fields = svc.llm_fields_from_meta(self.META)
        self.assertEqual(fields["route"], "gpt-6-astra[1m] ✗ permanent error → grok-4.6 ✓")
        self.assertEqual(fields["attempt_log"], [
            {"model": "gpt-6-astra[1m]", "attempt": 1, "outcome": "permanent_error", "http_status": 400, "elapsed_seconds": 0.41, "error": self.REJECTED},
            {"model": "grok-4.6", "attempt": 1, "outcome": "succeeded", "http_status": 200, "elapsed_seconds": 349.7},
        ])
        junk = [None, "hop", {"model": "m"}, {"model": "m", "attempt": True, "outcome": "x"}, {"model": " ", "attempt": 1, "outcome": "x"}]
        hops = [
            {"model": "m" * 100, "attempt": n, "outcome": "transient_error", "http_status": 503,
             "retry_after_seconds": float("nan"), "elapsed_seconds": -1, "error": "e" * 3000, "prompt": "never"}
            for n in range(1, 16)
        ]
        capped = svc.llm_fields_from_meta({"attempts": junk + hops})
        log = capped["attempt_log"]
        self.assertEqual([hop["attempt"] for hop in log], list(range(4, 16)), "the latest 12 hops, oldest first")
        self.assertEqual(capped["attempt_log_omitted"], 3)
        self.assertNotIn("attempt_log_omitted", fields)
        self.assertEqual(set(log[0]), {"model", "attempt", "outcome", "http_status", "error"})
        self.assertEqual((len(log[0]["model"]), len(log[0]["error"])), (80, 2000))
        self.assertNotIn("attempt_log", svc.llm_fields_from_meta({"attempts": junk}))

    def test_rows_keep_the_failure_detail_bounded_and_readable(self) -> None:
        rows = dx.normalize_agent_rows({"security": {
            "status": "failed", "summary": "agent failed with exit code 1",
            "error": "first   line\n\n  second line\n" + "z" * 2500,
            "attempt_log": ["junk", {"model": "grok-4.6", "attempt": 1, "outcome": "no_json", "http_status": 200,
                                     "retry_after_seconds": float("inf"), "error": "e" * 2500, "prompt": "never"}],
        }})
        row = rows[0]
        self.assertEqual(len(row["error"]), 2000)
        self.assertTrue(row["error"].startswith("...") and row["error"].endswith("z"), "the tail says why the runner gave up")
        self.assertEqual(dx.normalize_agent_rows({"a": {"error": "first   line\n\n  second line"}})[0]["error"], "first line\nsecond line")
        self.assertEqual(row["attempt_log"], [{"model": "grok-4.6", "attempt": 1, "outcome": "no_json", "http_status": 200, "error": "e" * 1997 + "..."}])

    @staticmethod
    def _max_llm() -> dict:
        """The largest llm record a run_updated event carries: 8 fallbacks of 80-char ids, all used."""
        ids = ["m%d-" % n + "x" * 77 for n in range(9)]
        policy = {"model": ids[0], "effort": "xhigh", "fallback_models": ids[1:], "source": "dashboard"}
        return svc.llm_record(policy, [{"model": model, "input_tokens": 10 ** 12, "output_tokens": 10 ** 12,
                                         "cache_read_tokens": 10 ** 12, "cache_write_tokens": 10 ** 12} for model in ids])

    def _record(self, entries: dict, llm: dict, error: str | None = None) -> tuple:
        """Write the rows the way review_pr does, through the recorder into the real store."""
        from code_review_service.dashboard_store import DashboardStore

        with tempfile.TemporaryDirectory() as tmp:
            store = DashboardStore(Path(tmp) / "dashboard.sqlite3")
            recorder = dx.DashboardRecorder(store, logger=lambda message: None)
            recorder.begin("o/r", 1, "abc1234")
            if error is None:
                recorder.agents(entries, stage="agents", llm=llm)
            else:
                recorder.failed(error, stage="posting-unverified", agents=dx.normalize_agent_rows(entries),
                                successful_agents=0, total_agents=len(entries), llm=llm)
            stored = store.get_run(recorder.run_id)["agents"]
            payload = store.events_after(0, limit=500)[-1]["payload"]
        return stored, payload, len(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def test_eight_agents_at_the_hop_cap_keep_a_failed_hop_each(self) -> None:
        """Hop metadata alone can pass the budget: hops then shrink to the newest, counted, never to none."""
        from code_review_service.dashboard_store import DashboardStore

        hops = [
            {"model": "gpt-6-astra[1m]" if n < 8 else "grok-4.6", "attempt": n, "outcome": "transient_error", "http_status": 503,
             "retry_after_seconds": 30.0, "elapsed_seconds": 1500.25, "error": "HTTP 503: " + "upstream down " * 150}
            for n in range(1, 15)
        ]
        fields = svc.llm_fields_from_meta({"outcome": "failed", "attempts": hops})
        self.assertEqual((len(fields["attempt_log"]), fields["attempt_log_omitted"]), (12, 2))
        entries = {f"agent-{n}": {"status": "failed", "summary": "agent failed with exit code 1", "error": self.EXHAUSTED, **fields} for n in range(8)}
        stored, payload, chars = self._record(entries, self._max_llm(), error="only 0/8 agents succeeded; minimum is 3")
        self.assertNotIn("truncated", payload)
        self.assertEqual(payload["status"], "failed")
        self.assertLessEqual(chars, DashboardStore.max_json_chars)
        self.assertEqual(dx.normalize_agent_rows(stored), stored, "the web tier's re-projection must not shrink stored rows")
        for rows in (stored, payload["agents"]):
            self.assertIsInstance(rows, list, "neither the column nor the event became a truncation marker")
            self.assertEqual(len(rows), 8)
            for row in rows:
                self.assertGreaterEqual(len(row["attempt_log"]), 1)
                self.assertTrue(all(hop["outcome"] == "transient_error" and hop["http_status"] == 503 for hop in row["attempt_log"]))
                self.assertEqual(row["attempt_log"][-1]["attempt"], 14, "the newest failed hop is the one kept")
                self.assertEqual(row["attempt_log_omitted"] + len(row["attempt_log"]), 14, "every hop is shown or counted")

    def test_a_max_size_event_payload_still_fits_with_the_agents(self) -> None:
        """The rows get what the event's other fields leave, measured at write time, not a guessed headroom."""
        from code_review_service.dashboard_store import DashboardStore

        error = '"' * 600  # _clip keeps MAX_ERROR_CHARS of them; JSON escaping doubles each one
        entries = {
            f"agent-{n}": {"status": "failed", "summary": "agent failed with exit code 1", "model": "grok-4.6", "route": "r" * 120,
                           "error": "e" * 2000,
                           "attempt_log": [{"model": "gpt-6-astra[1m]", "attempt": 1, "outcome": "permanent_error", "http_status": 400, "error": "t " * 1000}]}
            for n in range(8)
        }
        llm = self._max_llm()
        unfitted = len(json.dumps(dx.normalize_agent_rows(entries, budget=10 ** 9), ensure_ascii=False, sort_keys=True))
        stored, payload, chars = self._record(entries, llm, error=error)
        self.assertNotIn("truncated", payload)
        self.assertEqual((payload["status"], payload["error"], payload["llm"]), ("failed", dx._clip(error), llm))
        self.assertEqual(payload["agents"], stored)
        self.assertLessEqual(chars, DashboardStore.max_json_chars)
        others = chars - len(json.dumps(stored, ensure_ascii=False, sort_keys=True))
        self.assertGreater(others, 2000, "this payload is larger than the old fixed headroom")
        self.assertGreater(unfitted + others, DashboardStore.max_json_chars, "the agents alone would have overflowed it")
        self.assertTrue(all(row["attempt_log"][0]["http_status"] == 400 for row in stored))

    def test_base_metadata_gives_way_when_even_one_hop_per_agent_does_not_fit(self) -> None:
        """Every base field at its accepted limit plus the largest event: the rows still fit, with a failed hop each."""
        from code_review_service.dashboard_store import DashboardStore

        big = 10 ** 12
        entries = {
            f"agent-{n}": {
                "status": "failed", "summary": "s" * 200, "model": "m" * 80, "response_model": "r" * 80, "effort": "e" * 16,
                "route": "r" * 240, "outcome": "o" * 32, "input_tokens": big, "output_tokens": big, "cache_read_tokens": big,
                "cache_write_tokens": big, "attempts": 14, "elapsed_seconds": 1699.999, "attempt_log_omitted": 2,
                "attempt_log": [{"model": "a" * 80, "attempt": hop, "outcome": "transient_error", "http_status": 503, "error": "x" * 500}
                                for hop in range(3, 15)],
            }
            for n in range(8)
        }
        stored, payload, chars = self._record(entries, self._max_llm(), error='"' * 600)
        self.assertNotIn("truncated", payload)
        self.assertLessEqual(chars, DashboardStore.max_json_chars)
        self.assertIsInstance(stored, list, "the stored agents value is not a truncation marker")
        self.assertEqual([row["id"] for row in stored], [f"agent-{n}" for n in range(8)])
        for row in stored:
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["attempt_log"], [{"model": "a" * 80, "attempt": 14, "outcome": "transient_error", "http_status": 503}])
            self.assertEqual(row["attempt_log_omitted"], 13)
        self.assertEqual(dx.normalize_agent_rows(stored), stored)

    def test_succeeded_hops_give_way_before_any_text(self) -> None:
        entries = {f"agent-{n}": {"status": "done", "summary": "ok", **svc.llm_fields_from_meta(self.META)} for n in range(8)}
        full = len(json.dumps(dx.normalize_agent_rows(entries, budget=10 ** 9), ensure_ascii=False, sort_keys=True))
        for row in dx.normalize_agent_rows(entries, budget=full - 1):
            self.assertEqual(row["attempt_log"], [
                {"model": "gpt-6-astra[1m]", "attempt": 1, "outcome": "permanent_error", "http_status": 400, "elapsed_seconds": 0.41, "error": self.REJECTED},
            ])
            self.assertNotIn("attempt_log_omitted", row, "a succeeded hop is not a hidden failure")

    def test_eight_agents_that_fell_back_keep_the_whole_upstream_text(self) -> None:
        """The reported case: every agent's astra hop refused (HTTP 400), grok answered."""
        meta = dict(self.META, response_model="grok-4.6", response_id="resp_" + "0" * 30, stop_reason="end_turn", max_tokens=100000,
                    usage={"input_tokens": 123456, "output_tokens": 12345, "cache_read_input_tokens": 100000, "cache_creation_input_tokens": 0})
        ids = ["correctness", "security", "maintainability", "tests", "reliability", "red-team", "simplify", "server-architect"]
        entries = svc.make_progress_entries([{"id": agent_id, "prompt_path": "x"} for agent_id in ids])
        for agent_id in ids:
            entries[agent_id].update({"status": "done", "summary": "P1 x 1, P2 x 3, P3 x 2", **svc.llm_fields_from_meta(meta)})
        stored, payload, _ = self._record(entries, svc.llm_record({**POLICY, "source": "config"}, entries.values()))
        for rows in (stored, payload["agents"]):
            for row in rows:
                self.assertEqual(row["attempt_log"][0], {"model": "gpt-6-astra[1m]", "attempt": 1, "outcome": "permanent_error",
                                                         "http_status": 400, "elapsed_seconds": 0.41, "error": self.REJECTED})

    def test_review_pr_puts_the_failure_on_the_dashboard_row_and_not_on_the_pr(self) -> None:
        from tests.test_dashboard_integration import FakeClient, FakeStore

        class RecordingClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.bodies: list = []

            def upsert_issue_comment(self, number, marker, body):
                self.bodies.append(body)
                return {"id": 998}

            def create_review(self, number, commit_id, body, comments, event="COMMENT"):
                self.bodies.append(body)
                return super().create_review(number, commit_id, body, comments, event)

        def fake_run_agent(agent, *args, **kwargs):
            if agent["id"] == "security":
                llm = svc.llm_fields_from_meta(dict(self.META, outcome="failed", model=None))
                return {"agent": "security", "summary": "agent failed with exit code 1", "findings": [], "error": self.EXHAUSTED, "llm": llm}
            return {"agent": agent["id"], "summary": "ok", "findings": [], "llm": svc.llm_fields_from_meta(self.META)}

        store = FakeStore()
        recorder = dx.DashboardRecorder(store, logger=lambda message: None)
        client = RecordingClient()
        config = {
            "agents": [{"id": "correctness", "prompt_path": "x"}, {"id": "security", "prompt_path": "x"}],
            "agent_runner": {"command": ["r", "{model_policy_args}"]},
            "model_policy": POLICY,
            "min_successful_agents": 0, "max_review_comments": 30, "max_diff_chars": 180000,
            "progress_comment": {"enabled": True}, "approval": {"enabled": False}, "executive_summary": {"enabled": False},
        }
        with tempfile.TemporaryDirectory() as tmp:
            config["work_dir"] = str(Path(tmp) / "work")
            pr = {"number": 3, "head": {"sha": "abc123456789"}, "base": {"sha": "b" * 12}, "title": "t", "body": "", "user": {"login": "x"}, "html_url": "u"}
            with mock.patch.object(svc, "run_agent", fake_run_agent), \
                 mock.patch.object(svc, "clone_pr_source", lambda repo, n, h, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True)), \
                 mock.patch.object(svc, "load_repository_rules", lambda *a, **k: ""):
                svc.review_pr(client, config, Path(tmp) / "config" / "reviewer.json", pr, {"reviewed": {}}, recorder=recorder)
        rows = {row["id"]: row for row in store.runs[recorder.run_id]["agents"]}
        self.assertEqual(rows["security"]["status"], "failed")
        self.assertEqual(rows["security"]["error"], self.EXHAUSTED)
        self.assertEqual(rows["security"]["attempt_log"][0]["error"], self.REJECTED)
        self.assertEqual(rows["correctness"]["attempt_log"][0]["http_status"], 400)
        self.assertNotIn("error", rows["correctness"])
        self.assertTrue(any("Model and usage metadata" in body for body in client.bodies), "the progress comment was posted")
        for body in client.bodies:
            self.assertNotIn("not supported when using Codex", body)
            self.assertNotIn("upstream down", body)

    def test_pr_metadata_builders_never_carry_the_failure_text(self) -> None:
        llm = svc.llm_fields_from_meta(self.META)
        entries = svc.make_progress_entries([{"id": "correctness", "prompt_path": "x"}])
        entries["correctness"].update({"status": "failed", "summary": "agent failed", "error": self.EXHAUSTED, **llm})
        details = "\n".join(svc.build_llm_details(entries.values(), POLICY, summary_meta=llm, visual_meta=llm))
        self.assertIn("route gpt-6-astra[1m] ✗ permanent error → grok-4.6 ✓", details)
        body = svc.build_progress_comment_body(
            {"progress_marker": svc.PROGRESS_MARKER}, {"number": 7}, "abcdef1234567890", entries,
            "Reviewer agents running", dt.datetime(2026, 10, 13, tzinfo=dt.timezone.utc), policy=POLICY, summary_meta=llm,
        )
        for text in (details, body):
            self.assertNotIn("not supported when using Codex", text)
            self.assertNotIn("upstream down", text)


class PolicyPublicationTest(unittest.TestCase):
    """What is published as the policy must be what actually ran (review finding)."""

    ROLLBACK_ARGV = ["codex", "exec", "-m", "gpt-5.5", "--cd", "{run_dir}", "-o", "{response_file}"]

    def test_config_policy_replaces_legacy_model_flags(self) -> None:
        legacy = ["r", "--model", "gpt-6-astra[1m]", "--effort", "max", "--fallback-model", "gpt-5.6-sol[1m]"]
        argv = svc.render_runner_command(legacy, {}, {**POLICY, "source": "config"})
        self.assertEqual(argv, ["r", "--model", "gpt-6-astra[1m]", "--effort", "xhigh", "--fallback-model", "grok-4.6"])

    def test_policy_does_not_apply_to_an_argv_without_model_flags(self) -> None:
        self.assertFalse(svc.policy_applies(self.ROLLBACK_ARGV, {**POLICY, "source": "config"}))
        self.assertTrue(svc.policy_applies(["r", "{model_policy_args}"], {**POLICY, "source": "config"}))
        self.assertTrue(svc.policy_applies(["r", "--model", "x"], {**POLICY, "source": "dashboard"}))
        self.assertFalse(svc.policy_applies(["r", "{model_policy_args}"], None))

    def test_publication_is_judged_on_every_argv_that_runs(self) -> None:
        """A per-agent rollback runner withholds the policy even when agent_runner takes it."""
        token_argv = ["r", "{model_policy_args}"]
        policy = {**POLICY, "source": "config"}
        config = {"agents": [{"id": "a"}, {"id": "b", "runner": {"command": self.ROLLBACK_ARGV}}], "agent_runner": {"command": token_argv}}
        self.assertEqual(svc.runner_commands(config), [token_argv, self.ROLLBACK_ARGV])
        self.assertFalse(svc.policy_applies_everywhere(config, policy))
        config["agents"][1].pop("runner")
        self.assertTrue(svc.policy_applies_everywhere(config, policy))
        config["executive_summary"] = {"enabled": True, "runner": {"command": self.ROLLBACK_ARGV}}
        self.assertFalse(svc.policy_applies_everywhere(config, policy))
        config["executive_summary"]["enabled"] = False
        self.assertTrue(svc.policy_applies_everywhere(config, policy))
        # A per-agent runner dict without a command is taken whole, as run_agent does.
        config["agents"][1]["runner"] = {"env_allowlist": ["PATH"]}
        self.assertEqual(svc.runner_commands(config)[1], None)
        self.assertFalse(svc.policy_applies_everywhere(config, policy))

    def test_token_without_a_policy_fails_closed_instead_of_dropping_model(self) -> None:
        with self.assertRaises(svc.ServiceError):
            svc.render_runner_command(["r", "{model_policy_args}"], {}, None)

    def test_mixed_runners_still_run_the_ladder_but_publish_nothing(self) -> None:
        """The token-bearing agent must receive the ladder even when the policy is withheld."""
        from code_review_service import dashboard_execution as dx_mod
        from tests.test_dashboard_integration import FakeClient, FakeStore

        store = FakeStore()
        recorder = dx_mod.DashboardRecorder(store, logger=lambda message: None)
        seen: list = []
        config = {
            "agents": [{"id": "a", "prompt_path": "x"}, {"id": "b", "prompt_path": "x", "runner": {"command": self.ROLLBACK_ARGV}}],
            "agent_runner": {"command": ["r", "{model_policy_args}"]},
            "model_policy": POLICY,
            "min_successful_agents": 0, "max_review_comments": 30, "max_diff_chars": 180000,
            "progress_comment": {"enabled": True}, "approval": {"enabled": False}, "executive_summary": {"enabled": False},
        }
        with tempfile.TemporaryDirectory() as tmp:
            config["work_dir"] = str(Path(tmp) / "work")
            pr = {"number": 3, "head": {"sha": "abc123456789"}, "base": {"sha": "b" * 12}, "title": "t", "body": "", "user": {"login": "x"}, "html_url": "u"}

            def fake_run_agent(agent, *a, **k):
                seen.append((agent["id"], k.get("policy")))
                return {"agent": agent["id"], "summary": "ok", "findings": []}

            with mock.patch.object(svc, "run_agent", fake_run_agent), \
                 mock.patch.object(svc, "clone_pr_source", lambda repo, n, h, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True)), \
                 mock.patch.object(svc, "load_repository_rules", lambda *a, **k: ""):
                svc.review_pr(FakeClient(), config, Path(tmp) / "config" / "reviewer.json", pr, {"reviewed": {}}, recorder=recorder)
        self.assertNotIn("policy", store.runs["run-1"]["llm"])
        self.assertEqual({agent for agent, _ in seen}, {"a", "b"})
        self.assertTrue(all(p and p["model"] == POLICY["model"] for _, p in seen), seen)

    def test_invalid_legacy_argv_is_never_published_as_the_policy(self) -> None:
        config = {"agent_runner": {"command": ["r", "--model", "claude-opus-5", "--effort", "max"]}}
        policy = svc.resolve_model_policy(config)
        self.assertEqual(policy["source"], "default")
        self.assertFalse(policy["model"].startswith("claude"))

    def test_load_config_rejects_a_malformed_policy_at_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviewer.json"
            path.write_text(json.dumps({
                "repositories": ["o/r"],
                "agents": [{"id": "a", "prompt_path": "x"}, {"id": "b", "prompt_path": "x"}, {"id": "c", "prompt_path": "x"}],
                "model_policy": {"model": "gpt-6-astra[1m]", "effort": "maximum"},
            }), encoding="utf-8")
            with self.assertRaises(svc.ServiceError) as caught:
                svc.load_config(path)
            self.assertIn("model_policy", str(caught.exception))

    def test_resolve_never_raises_on_a_bad_config_policy(self) -> None:
        policy = svc.resolve_model_policy({"model_policy": {"model": "gpt-6-astra[1m]", "effort": "maximum"}})
        self.assertEqual(policy["source"], "default")

    def test_review_with_rollback_argv_publishes_no_policy(self) -> None:
        """A run through the Codex CLI rollback command records llm.policy absent."""
        from code_review_service import dashboard_execution as dx_mod
        from tests.test_dashboard_integration import FakeClient, FakeStore

        store = FakeStore()
        recorder = dx_mod.DashboardRecorder(store, logger=lambda message: None)
        config = {
            "agents": [{"id": "a", "prompt_path": "x"}, {"id": "b", "prompt_path": "x"}],
            "agent_runner": {"command": self.ROLLBACK_ARGV},
            "model_policy": POLICY,
            "min_successful_agents": 0, "max_review_comments": 30, "max_diff_chars": 180000,
            "progress_comment": {"enabled": True}, "approval": {"enabled": False}, "executive_summary": {"enabled": False},
        }
        with tempfile.TemporaryDirectory() as tmp:
            config["work_dir"] = str(Path(tmp) / "work")
            pr = {"number": 3, "head": {"sha": "abc123456789"}, "base": {"sha": "b" * 12}, "title": "t", "body": "", "user": {"login": "x"}, "html_url": "u"}
            with mock.patch.object(svc, "run_agent", lambda agent, *a, **k: {"agent": agent["id"], "summary": "ok", "findings": []}), \
                 mock.patch.object(svc, "clone_pr_source", lambda repo, n, h, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True)), \
                 mock.patch.object(svc, "load_repository_rules", lambda *a, **k: ""):
                svc.review_pr(FakeClient(), config, Path(tmp) / "config" / "reviewer.json", pr, {"reviewed": {}}, recorder=recorder)
        self.assertNotIn("policy", store.runs["run-1"]["llm"])


if __name__ == "__main__":
    unittest.main()
