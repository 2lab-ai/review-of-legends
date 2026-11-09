"""Per-repository agent selection: opt-in agents and repository switches.

Shipped on 2026-10-07 for two new reviewers: ``simplify`` (every repository) and
``server-architect`` (opt-in, switched on for the backend servers: Gamma, and
MiniMoon since 2026-10-14).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from code_review_service import service as svc
from tests.shipped_config import load_shipped_config

ROOT = Path(__file__).resolve().parents[1]


def _agents(*ids: str, opt_in: tuple[str, ...] = ()) -> list[dict]:
    return [{"id": i, "prompt_path": "x", **({"opt_in": True} if i in opt_in else {})} for i in ids]


class AgentsForRepoTest(unittest.TestCase):
    def test_opt_in_agent_runs_nowhere_until_a_repository_switches_it_on(self) -> None:
        config = {"agents": _agents("a", "b", "c", "arch", opt_in=("arch",))}
        self.assertEqual([a["id"] for a in svc.agents_for_repo(config, "o/x")], ["a", "b", "c"])
        config["repository_agents"] = {"o/x": {"arch": True}}
        self.assertEqual([a["id"] for a in svc.agents_for_repo(config, "o/x")], ["a", "b", "c", "arch"])
        self.assertEqual([a["id"] for a in svc.agents_for_repo(config, "o/y")], ["a", "b", "c"])

    def test_a_default_agent_can_be_switched_off_for_one_repository(self) -> None:
        config = {"agents": _agents("a", "b", "c", "d"), "repository_agents": {"o/x": {"d": False}}}
        self.assertEqual([a["id"] for a in svc.agents_for_repo(config, "o/x")], ["a", "b", "c"])
        self.assertEqual([a["id"] for a in svc.agents_for_repo(config, "o/y")], ["a", "b", "c", "d"])

    def test_malformed_overrides_fall_back_to_defaults(self) -> None:
        config = {"agents": _agents("a", "b", "c"), "repository_agents": "nope"}
        self.assertEqual(len(svc.agents_for_repo(config, "o/x")), 3)


class ShippedConfigTest(unittest.TestCase):
    def test_public_template_has_no_organization_repositories_or_credentials(self) -> None:
        config = load_shipped_config()
        self.assertEqual(config["repositories"], ["example/project"])
        self.assertNotIn("github_app", config)
        self.assertNotIn("public_url", config["dashboard"])
        self.assertEqual(config["repository_agents"], {})
        ids = [a["id"] for a in svc.agents_for_repo(config, "example/project")]
        self.assertIn("simplify", ids)
        self.assertNotIn("server-architect", ids)
        self.assertEqual(svc.repository_quorum_problem(config, "example/project"), "")

    def test_both_prompts_carry_the_scope_carve_out_and_priority_rules(self) -> None:
        simplify = (ROOT / "agents" / "simplify.md").read_text(encoding="utf-8")
        architect = (ROOT / "agents" / "server-architect.md").read_text(encoding="utf-8")
        for raw in (simplify, architect):
            text = " ".join(raw.split())  # prompts are wrapped at ~100 columns
            # render_prompt appends a "Review standard" that defers to the agent's finding definition;
            # each prompt must state its definition and keep the diff-only rule.
            self.assertIn("defers to this definition", text)
            self.assertIn("diff-only rule still applies", text)
            self.assertIn("overall_correctness", text)
        self.assertIn("Never P0 or P1", simplify)
        self.assertIn("ponytail", simplify)
        for tag in ("delete:", "stdlib:", "native:", "reuse:", "yagni:", "shrink:"):
            self.assertIn(tag, simplify)
        for lens in ("db:", "net:", "capacity:"):
            self.assertIn(lens, architect)
        self.assertIn("pattern.db-access.md", architect)
        self.assertIn("P0 is not this agent's call", architect)
        self.assertIn("unknown, not absent", architect)
        self.assertIn("zero retries is a valid design", architect)
        self.assertIn("rolling deploy", architect)

    def test_rendered_prompt_keeps_agent_instructions_ahead_of_the_review_standard(self) -> None:
        agent_prompt = (ROOT / "agents" / "simplify.md").read_text(encoding="utf-8")
        rendered = svc.render_prompt("o/r", {"number": 1, "title": "t", "body": "", "base": {"ref": "main"}, "head": {"ref": "f"}}, [], "", agent_prompt)
        scope = rendered.index("Scope for this agent")
        standard = rendered.index("Review standard:")
        self.assertLess(scope, standard)
        self.assertIn("Lean already. Ship.", rendered)
        # The fixed standard must defer to the agent's definition, not demand bugs only.
        self.assertIn("The agent instructions above define what counts as a finding", rendered)
        self.assertNotIn("actionable bugs", rendered)


class LoadConfigValidationTest(unittest.TestCase):
    def _load(self, overrides) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviewer.json"
            path.write_text(json.dumps({
                "repositories": ["o/r", "o/s"],
                "agents": _agents("a", "b", "c", "arch", opt_in=("arch",)),
                "repository_agents": overrides,
            }), encoding="utf-8")
            return svc.load_config(path)

    def test_accepts_a_switch_for_a_known_repository_and_agent(self) -> None:
        config = self._load({"o/r": {"arch": True}})
        self.assertEqual([a["id"] for a in svc.agents_for_repo(config, "o/r")], ["a", "b", "c", "arch"])

    def test_rejects_an_unknown_repository(self) -> None:
        with self.assertRaises(svc.ServiceError) as caught:
            self._load({"o/typo": {"arch": True}})
        self.assertIn("o/typo", str(caught.exception))

    def test_rejects_an_unknown_agent_id(self) -> None:
        with self.assertRaises(svc.ServiceError) as caught:
            self._load({"o/r": {"architek": True}})
        self.assertIn("architek", str(caught.exception))

    def test_rejects_a_non_boolean_switch(self) -> None:
        with self.assertRaises(svc.ServiceError):
            self._load({"o/r": {"arch": "yes"}})

    def test_rejects_an_explicit_quorum_above_a_repository_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviewer.json"
            path.write_text(json.dumps({
                "repositories": ["o/r", "o/s"],
                "agents": _agents("a", "b", "c", "arch", opt_in=("arch",)),
                "repository_agents": {"o/r": {"arch": True}},
                "min_successful_agents": 4,
            }), encoding="utf-8")
            with self.assertRaises(svc.ServiceError) as caught:
                svc.load_config(path)
        self.assertIn("min_successful_agents=4 exceeds the 3 agents selected for o/s", str(caught.exception))

    def test_rejects_a_non_boolean_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviewer.json"
            path.write_text(json.dumps({
                "repositories": ["o/r"],
                "agents": [{"id": "a", "prompt_path": "x", "opt_in": "false"}, *_agents("b", "c", "d")],
            }), encoding="utf-8")
            with self.assertRaises(svc.ServiceError) as caught:
                svc.load_config(path)
        self.assertIn("opt_in", str(caught.exception))

    def test_a_switch_on_a_globally_disabled_agent_is_accepted_and_runs_nowhere(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviewer.json"
            path.write_text(json.dumps({
                "repositories": ["o/r"],
                "agents": [*_agents("a", "b", "c"), {"id": "arch", "prompt_path": "x", "opt_in": True, "enabled": False}],
                "repository_agents": {"o/r": {"arch": True}},
            }), encoding="utf-8")
            config = svc.load_config(path)
        self.assertEqual([a["id"] for a in svc.agents_for_repo(config, "o/r")], ["a", "b", "c"])

    def test_rejects_a_repository_left_below_the_quorum_floor(self) -> None:
        with self.assertRaises(svc.ServiceError) as caught:
            self._load({"o/r": {"a": False, "b": False}})
        self.assertIn("at least 3 agents must review o/r", str(caught.exception))


class ReviewPrUsesTheRepositorySelectionTest(unittest.TestCase):
    """review_pr dispatches, counts and records the per-repository list, never config["agents"]."""

    def _run(self, repo: str, *, failing: tuple[str, ...] = (), explicit_minimum: int | None = 0) -> tuple[list[str], dict, object]:
        from code_review_service import dashboard_execution as dx_mod
        from tests.test_dashboard_integration import FakeClient, FakeStore

        store = FakeStore()
        recorder = dx_mod.DashboardRecorder(store, logger=lambda message: None)
        config = {
            "agents": _agents("a", "b", "c", "arch", opt_in=("arch",)),
            "repository_agents": {"o/gamma": {"arch": True}},
            "agent_runner": {"command": ["r", "{model_policy_args}"]},
            "max_review_comments": 30, "max_diff_chars": 180000,
            "progress_comment": {"enabled": False}, "approval": {"enabled": False}, "executive_summary": {"enabled": False},
        }
        if explicit_minimum is not None:
            config["min_successful_agents"] = explicit_minimum
        dispatched: list[str] = []
        state: dict = {"reviewed": {}}
        with tempfile.TemporaryDirectory() as tmp:
            config["work_dir"] = str(Path(tmp) / "work")
            pr = {"number": 3, "head": {"sha": "abc123456789"}, "base": {"sha": "b" * 12}, "title": "t", "body": "", "user": {"login": "x"}, "html_url": "u"}

            def fake_run_agent(agent, *a, **k):
                dispatched.append(agent["id"])
                if agent["id"] in failing:
                    return {"agent": agent["id"], "summary": "boom", "findings": [], "error": "boom"}
                return {"agent": agent["id"], "summary": "ok", "findings": []}

            client = FakeClient(repo=repo)
            with mock.patch.object(svc, "run_agent", fake_run_agent), \
                 mock.patch.object(svc, "clone_pr_source", lambda repo, n, h, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True)), \
                 mock.patch.object(svc, "load_repository_rules", lambda *a, **k: ""):
                svc.review_pr(client, config, Path(tmp) / "config" / "reviewer.json", pr, state, recorder=recorder)
        return sorted(dispatched), state, (client, store)

    def test_the_opt_in_agent_runs_on_the_switched_on_repository_only(self) -> None:
        dispatched, state, _ = self._run("o/gamma")
        self.assertEqual(dispatched, ["a", "arch", "b", "c"])
        self.assertEqual(state["reviewed"]["3"]["agents"], ["a", "b", "c", "arch"])
        dispatched, state, _ = self._run("o/other")
        self.assertEqual(dispatched, ["a", "b", "c"])
        self.assertEqual(state["reviewed"]["3"]["agents"], ["a", "b", "c"])

    def test_policy_publication_is_judged_on_the_repository_selection(self) -> None:
        """An opt-in agent whose runner takes no model policy must not withhold the
        policy from repositories that never run it (codex thread on PR #21)."""
        token_argv = ["r", "{model_policy_args}"]
        rollback = ["codex", "exec"]
        config = {
            "agents": [*_agents("a", "b", "c"), {"id": "arch", "prompt_path": "x", "opt_in": True, "runner": {"command": rollback}}],
            "repository_agents": {"o/gamma": {"arch": True}},
            "agent_runner": {"command": token_argv},
            "executive_summary": {"enabled": False},
        }
        policy = {"model": "gpt-6-astra[1m]", "effort": "xhigh", "fallback_models": []}
        self.assertTrue(svc.policy_applies_everywhere(config, policy, svc.agents_for_repo(config, "o/other")))
        self.assertFalse(svc.policy_applies_everywhere(config, policy, svc.agents_for_repo(config, "o/gamma")))
        # Without a selection the whole list is judged, as before.
        self.assertFalse(svc.policy_applies_everywhere(config, policy))

    def test_three_selected_successes_approve_an_eligible_pr(self) -> None:
        """Approval needs every *selected* agent to succeed; the opted-out architect
        must not downgrade the review to COMMENT (daemon's tests-agent thread)."""
        from tests.test_progress_comment import _FakeClient

        config = {
            "agents": _agents("a", "b", "c", "arch", opt_in=("arch",)),
            "repository_agents": {"o/gamma": {"arch": True}},
            "agent_runner": {"command": ["r", "{model_policy_args}"]},
            "max_review_comments": 30, "max_diff_chars": 180000,
            "marker": svc.MARKER, "summary_marker": svc.SUMMARY_MARKER, "progress_marker": svc.PROGRESS_MARKER,
            "progress_comment": {"enabled": True}, "approval": {"enabled": True, "trusted_authors": ["trusted-author"]},
            "executive_summary": {"enabled": False},
        }
        client = _FakeClient()  # repo "owner/repo": the architect is not selected here
        with tempfile.TemporaryDirectory() as tmp:
            config["work_dir"] = str(Path(tmp) / "work")
            pr = {"number": 3, "head": {"sha": "abc123456789"}, "base": {"sha": "b" * 12}, "title": "t", "body": "", "user": {"login": "trusted-author"}}
            with mock.patch.object(svc, "run_agent", lambda agent, *a, **k: {"agent": agent["id"], "summary": "ok", "findings": []}), \
                 mock.patch.object(svc, "clone_pr_source", lambda repo, n, h, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True)), \
                 mock.patch.object(svc, "load_repository_rules", lambda *a, **k: ""):
                svc.review_pr(client, config, Path(tmp) / "config" / "reviewer.json", pr, {"reviewed": {}})
        self.assertEqual(client.review_events, ["APPROVE"])
        self.assertIn("3 / 3", client.progress_bodies[-1])

    def test_default_quorum_is_the_repository_selection_not_the_global_list(self) -> None:
        """Without an explicit minimum every selected agent must succeed: 3 of 3 on the
        repository without the opt-in agent posts; counting the global 4 would not."""
        _, state, (client, store) = self._run("o/other", explicit_minimum=None)
        self.assertEqual(client.create_review_calls, 1)
        self.assertIn("3", state["reviewed"])
        run = next(iter(store.runs.values()))
        self.assertEqual(run.get("total_agents"), 3)

    def test_a_failed_selected_agent_breaks_the_default_quorum_and_reports_the_selected_total(self) -> None:
        _, state, (client, store) = self._run("o/other", failing=("b",), explicit_minimum=None)
        self.assertEqual(client.create_review_calls, 0)
        self.assertNotIn("3", state["reviewed"])
        run = next(iter(store.runs.values()))
        self.assertEqual((run.get("successful_agents"), run.get("total_agents")), (2, 3))


if __name__ == "__main__":
    unittest.main()
