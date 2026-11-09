from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import service as svc
from tests.test_llm_runner import EFFORT, FALLBACK_MODEL, PRIMARY_MODEL

CONFIG_PATH = ROOT / "config" / "reviewer.json"


class ModelLadderTest(unittest.TestCase):
    """The shipped ladder is gpt-6-astra[1m] at effort xhigh, then grok-4.6 at the
    same effort when astra has no usage left (SSOT 2026-09-17). Both ids are
    canonical in the llmux catalog; no Claude model may appear, because this
    service has no Anthropic account leg through llmux."""

    def setUp(self) -> None:
        self.config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        self.command = self.config["agent_runner"]["command"]

    def test_config_policy_is_astra_xhigh_then_grok(self) -> None:
        policy = svc.config_model_policy(self.config)
        self.assertEqual(policy["model"], "gpt-6-astra[1m]")
        self.assertEqual(policy["effort"], "xhigh")
        self.assertEqual(policy["fallback_models"], ["grok-4.6"])
        self.assertEqual(policy["source"], "config")

    def test_runner_argv_takes_the_ladder_from_the_policy_not_from_literals(self) -> None:
        self.assertIn(svc.MODEL_POLICY_ARGS_TOKEN, self.command)
        for literal in ("--model", "--effort", "--fallback-model"):
            self.assertNotIn(literal, self.command)
        self.assertIn("--meta-file", self.command)
        self.assertEqual(self.command[self.command.index("--meta-file") + 1], "{meta_file}")

    def test_rendered_argv_carries_the_shipped_ladder(self) -> None:
        values = {"prompt_file": "p", "response_file": "r", "meta_file": "m", "service_root": "/srv"}
        argv = svc.render_runner_command(self.command, values, svc.config_model_policy(self.config))
        self.assertEqual(argv[argv.index("--model") + 1], PRIMARY_MODEL)
        self.assertEqual(argv[argv.index("--effort") + 1], EFFORT)
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--fallback-model"], [FALLBACK_MODEL])
        self.assertEqual(argv[argv.index("--meta-file") + 1], "m")

    def test_runner_argv_declares_the_output_contract_per_invocation(self) -> None:
        """Reviewer agents expect JSON (nudge on prose); the summary is markdown."""
        self.assertEqual(self.command[self.command.index("--output-contract") + 1], "{output_contract}")
        values = {"prompt_file": "p", "response_file": "r", "meta_file": "m", "service_root": "/srv", "output_contract": "json"}
        argv = svc.render_runner_command(self.command, values, svc.config_model_policy(self.config))
        self.assertEqual(argv[argv.index("--output-contract") + 1], "json")

    def test_output_budget_covers_xhigh_reasoning(self) -> None:
        """llmux maps max_tokens to max_output_tokens, which includes reasoning; at
        xhigh grok-4.6 exhausted 32,000 on Pluto #508 (three agents truncated)."""
        self.assertGreaterEqual(int(self.command[self.command.index("--max-tokens") + 1]), 100000)

    def test_prompt_closes_with_the_no_tools_json_only_instruction(self) -> None:
        """grok answered the agent-style prompt with a plan and stopped (Pluto #508)."""
        prompt = svc.render_prompt("o/r", {"number": 1, "title": "t", "body": "", "base": {"ref": "main"}, "head": {"ref": "x", "sha": "abc"}, "user": {"login": "u"}}, [], "diff --git a b", "focus")
        tail = prompt[-400:]
        self.assertIn("You have no tools and cannot open files", tail)
        self.assertIn("nothing else: no preamble, no plan, no markdown", tail)
        self.assertLess(prompt.rfind("diff --git a b"), prompt.rfind("End of input."), "the instruction follows the diff")

    def test_default_runner_targets_the_local_llmux_proxy(self) -> None:
        self.assertIn("--base-url", self.command)
        self.assertEqual(self.command[self.command.index("--base-url") + 1], "http://localhost:3456")

    def test_no_claude_model_anywhere_in_the_config(self) -> None:
        """Every runner argv and ladder in the shipped config, comments excluded."""

        def strip_comments(value):
            if isinstance(value, dict):
                return {k: strip_comments(v) for k, v in value.items() if not str(k).startswith("_")}
            if isinstance(value, list):
                return [strip_comments(v) for v in value]
            return value

        raw = json.dumps(strip_comments(self.config)).lower()
        for marker in ("claude", "anthropic"):
            self.assertNotIn(marker, raw)

    def test_executive_summary_reuses_the_default_runner(self) -> None:
        # No separate summary runner: the summary agent falls back to
        # config.agent_runner, so one ladder change covers every agent.
        self.assertNotIn("runner", self.config["executive_summary"])


if __name__ == "__main__":
    unittest.main()
