"""Required reviewer agents, dashboard agent overrides, and the poll loop's retry limit.

A required agent (``agents[].required``, or a dashboard ``agent:<id>`` override)
must finish: when it errors the attempt ends like a quorum failure — nothing is
posted. Either failure is counted per head in ``state.json`` so the poll loop
stops re-running every agent on a commit that keeps failing; an operator retry
from the dashboard still runs.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
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
from tests.shipped_config import app_key_stand_in, load_shipped_config
from tests.test_dashboard_integration import FakeStore
from tests.test_progress_comment import _FakeClient

CONFIG_PATH = ROOT / "config" / "reviewer.json"
HEAD = "abc123456789"
BASE = "b" * 12  # the base SHA every harness PR is on unless a test moves it
UTC = dt.timezone.utc


def _agents(*ids: str, required: tuple[str, ...] = ()) -> list[dict]:
    return [{"id": i, "prompt_path": "x", **({"required": True} if i in required else {})} for i in ids]


def _config(work_dir: Path, *, required: tuple[str, ...] = ("arch",), minimum: int = 2) -> dict:
    return {
        "agents": _agents("a", "b", "arch", required=required),
        "agent_runner": {"command": ["true"]},
        "min_successful_agents": minimum,
        "max_review_comments": 30,
        "max_diff_chars": 180000,
        "work_dir": str(work_dir),
        "marker": svc.MARKER,
        "summary_marker": svc.SUMMARY_MARKER,
        "progress_marker": svc.PROGRESS_MARKER,
        "progress_comment": {"enabled": True},
        "approval": {"enabled": False},
        "executive_summary": {"enabled": False},
    }


def _write_config(tmp: Path, payload: dict) -> Path:
    path = tmp / "reviewer.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class RequiredConfigTest(unittest.TestCase):
    def _base(self) -> dict:
        return {"repositories": ["o/r"], "agents": _agents("a", "b", "c")}

    def test_rejects_a_non_boolean_required(self) -> None:
        for value in ("true", 1, None):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                payload = self._base()
                payload["agents"][0]["required"] = value
                with self.assertRaises(svc.ServiceError) as caught:
                    svc.load_config(_write_config(Path(tmp), payload))
                self.assertIn("required must be true or false", str(caught.exception))

    def test_rejects_non_positive_retry_limits(self) -> None:
        for key in ("auto_retry_attempts", "auto_retry_backoff_seconds"):
            for value in (0, -1, "3", True, 1.5, None):  # null used to pass and crash the poll loop
                with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as tmp:
                    payload = self._base()
                    payload[key] = value
                    with self.assertRaises(svc.ServiceError) as caught:
                        svc.load_config(_write_config(Path(tmp), payload))
                    self.assertIn(f"config.{key} must be a positive integer", str(caught.exception))

    def test_rejects_an_agent_id_the_settings_key_cannot_hold(self) -> None:
        # Such an id used to vanish from the Settings page and never take an override.
        for bad in ("server_architect", "Review", "-lead", "", 7):
            with self.subTest(agent_id=bad), tempfile.TemporaryDirectory() as tmp:
                payload = self._base()
                payload["agents"][0]["id"] = bad
                with self.assertRaises(svc.ServiceError) as caught:
                    svc.load_config(_write_config(Path(tmp), payload))
                self.assertIn(f"config.agents id {bad!r} is invalid", str(caught.exception))
        # A disabled agent is declared too, so it is checked as well.
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._base()
            payload["agents"].append({"name": "no-id", "prompt_path": "x", "enabled": False})
            with self.assertRaises(svc.ServiceError):
                svc.load_config(_write_config(Path(tmp), payload))

    def test_shipped_config_requires_the_server_architect_on_the_backend_servers_only(self) -> None:
        config = load_shipped_config()
        architect = next(agent for agent in config["agents"] if agent["id"] == "server-architect")
        self.assertIs(architect["required"], True)
        others = [agent["id"] for agent in config["agents"] if agent.get("required")]
        self.assertEqual(others, ["server-architect"])
        backends = {"acme-corp/Gamma", "acme-corp/MiniMoon"}
        for repo in config["repositories"]:
            selected = [agent["id"] for agent in svc.agents_for_repo(config, repo)]
            self.assertEqual("server-architect" in selected, repo in backends, repo)
        self.assertEqual(svc.auto_retry_limits(config), (3, 600))

    def test_doctor_marks_required_agents_per_repository(self) -> None:
        config = load_shipped_config()
        config["repository_agents"] = {"example/project": {"server-architect": True}}
        out = io.StringIO()
        with mock.patch.object(svc, "load_config", return_value=config), mock.patch.object(svc.shutil, "which", return_value=None), contextlib.redirect_stdout(out):
            svc.run_doctor(argparse.Namespace(config=str(CONFIG_PATH)))
        lines = out.getvalue().splitlines()
        line = next(line for line in lines if line.startswith("  example/project: "))
        self.assertTrue(line.endswith(", server-architect (required)"), line)
        self.assertEqual(sum("(required)" in line for line in lines), 1, lines)


class AgentOverrideTest(unittest.TestCase):
    def test_validate_normalises_and_names_the_defect(self) -> None:
        self.assertEqual(svc.validate_agent_override({"prompt": "  hi\n", "required": False}), {"prompt": "hi", "required": False})
        self.assertEqual(svc.validate_agent_override({"prompt": None, "required": None}), {})
        for bad, message in (
            ([], "must be an object"),
            ({"prompt": "x", "focus": "y"}, "unknown fields: focus"),
            ({"prompt": "   "}, "non-empty string"),
            ({"prompt": 3}, "non-empty string"),
            ({"prompt": "x" * (svc.MAX_AGENT_PROMPT_CHARS + 1)}, "at most 48000"),
            ({"required": "yes"}, "true or false"),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as caught:
                svc.validate_agent_override(bad)
            self.assertIn(message, str(caught.exception))

    def test_a_model_override_passes_the_policy_model_id_rules(self) -> None:
        self.assertEqual(svc.validate_agent_override({"model": "gpt-6-astra[1m]"}), {"model": "gpt-6-astra[1m]"})
        self.assertEqual(svc.validate_agent_override({"model": "  grok-4.7[1m]\n"}), {"model": "grok-4.7[1m]"}, "stored stripped")
        self.assertEqual(svc.validate_agent_override({"model": None, "required": True}), {"required": True}, "null = not overridden")
        for bad, message in (
            ({"model": ""}, "model must be a non-empty model id"),
            ({"model": "   "}, "model must be a non-empty model id"),
            ({"model": 7}, "model must be a non-empty model id"),
            ({"model": "gpt 6"}, "model is not a valid model id"),
            ({"model": "x" * 81}, "model is not a valid model id"),
            ({"model": "claude-opus-4"}, f"model 'claude-opus-4': {svc.UNUSABLE_MODEL_REASON}"),
            ({"model": " Claude-Sonnet "}, f"model 'Claude-Sonnet': {svc.UNUSABLE_MODEL_REASON}"),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as caught:
                svc.validate_agent_override(bad)
            self.assertEqual(str(caught.exception), message)
        # The policy keeps its own messages with the shared helper.
        with self.assertRaises(ValueError) as caught:
            svc.validate_model_policy({"model": "gpt-6", "fallback_models": ["claude-x"]})
        self.assertEqual(str(caught.exception), f"fallback_models[0] 'claude-x': {svc.UNUSABLE_MODEL_REASON}")

    def test_resolution_prefers_the_dashboard_and_ignores_a_malformed_override(self) -> None:
        store = FakeStore()
        store.settings["agent:a"] = {"required": True, "prompt": "dashboard prompt"}
        store.settings["agent:arch"] = {"required": "no"}  # malformed: config wins
        store.settings["agent:b"] = {"model": "gpt-6-astra[1m]"}
        recorder = dx.DashboardRecorder(store, logger=lambda message: None)
        resolved = svc.resolve_agent_settings(_agents("a", "b", "arch", required=("arch",)), recorder)
        self.assertEqual(resolved["a"], {"required": True, "prompt": "dashboard prompt", "model": None})
        self.assertEqual(resolved["b"], {"required": False, "prompt": None, "model": "gpt-6-astra[1m]"})
        self.assertEqual(resolved["arch"], {"required": True, "prompt": None, "model": None})

    def test_a_stored_claude_model_is_ignored_like_any_malformed_override(self) -> None:
        store = FakeStore()
        store.settings["agent:arch"] = {"model": "claude-opus-4", "required": False}
        recorder = dx.DashboardRecorder(store, logger=lambda message: None)
        with mock.patch.object(svc, "log") as logged:
            resolved = svc.resolve_agent_settings(_agents("arch", required=("arch",)), recorder)
        self.assertEqual(resolved["arch"], {"required": True, "prompt": None, "model": None})
        self.assertIn("ignoring the dashboard setting for agent arch", logged.call_args[0][0])


class PromptDefaultReconcileTest(unittest.TestCase):
    """A changed prompt file replaces a dashboard edit once (svc.reconcile_agent_document)."""

    A, B, C = "Prompt A.\n", "Prompt B (edited).", "Prompt C (new default).\n"
    NOW = "2026-10-15T12:00:00+00:00"
    SAVED_AT, SAVED_BY = "2026-10-14T00:00:00+00:00", "owner@company.example"

    def reconcile(self, value: dict, default_text: str) -> tuple:
        return svc.reconcile_agent_document(value, default_text, self.NOW, saved_at=self.SAVED_AT, saved_by=self.SAVED_BY)

    def test_an_unchanged_default_is_a_no_op(self) -> None:
        value = {"prompt": self.B, "prompt_base_sha": svc.prompt_sha(self.A), "required": True}
        self.assertEqual(self.reconcile(value, self.A), (value, None))
        # The base is of the stripped text: surrounding whitespace is not a change.
        self.assertEqual(self.reconcile(value, "\n\n" + self.A + "\n"), (value, None))
        self.assertEqual(svc.prompt_sha("  x \n"), svc.prompt_sha("x"))

    def test_a_changed_default_replaces_the_edit_once_and_keeps_the_other_fields(self) -> None:
        base = svc.prompt_sha(self.A)
        value = {"prompt": self.B, "prompt_base_sha": base, "required": True, "model": "gpt-6-astra[1m]"}
        document, action = self.reconcile(value, self.C)
        self.assertEqual(action, "superseded")
        self.assertEqual(document, {
            "required": True,
            "model": "gpt-6-astra[1m]",
            "superseded": {
                "prompt": self.B,
                "saved_at": self.SAVED_AT, "saved_by": self.SAVED_BY, "superseded_at": self.NOW,
            },
        })
        # Once: with no prompt left, neither this default nor the next replaces anything.
        self.assertEqual(self.reconcile(document, self.C), (document, None))
        self.assertEqual(self.reconcile(document, "Prompt D.\n"), (document, None))

    def test_an_edit_equal_to_the_new_default_is_dropped_silently(self) -> None:
        value = {"prompt": self.C.strip(), "prompt_base_sha": svc.prompt_sha(self.A), "required": False}
        self.assertEqual(self.reconcile(value, self.C), ({"required": False}, "cleared"))

    def test_an_edit_equal_to_the_new_default_drops_an_older_superseded_too(self) -> None:
        # Not reachable through the API (PUT and restore always clear `superseded`
        # first), but reconcile is pure and must stay total: nothing is left to
        # compare once the admin's edit equals the new default, so a leftover
        # `superseded` from an earlier round does not survive either.
        older = {"prompt": "Prompt B0.",
                 "saved_at": None, "saved_by": None, "superseded_at": "2026-10-01T00:00:00+00:00"}
        value = {"prompt": self.C.strip(), "prompt_base_sha": svc.prompt_sha(self.A), "required": False, "superseded": older}
        self.assertEqual(self.reconcile(value, self.C), ({"required": False}, "cleared"))

    def test_a_legacy_edit_without_a_base_is_adopted_not_replaced(self) -> None:
        value = {"prompt": self.B, "model": "grok-4.7[1m]"}
        document, action = self.reconcile(value, self.C)
        self.assertEqual(action, "adopted")
        # Who saved the legacy prompt, and when, is pinned now (the row as it stands),
        # so a later Required- or model-only save cannot take over its provenance.
        self.assertEqual(document, {"prompt": self.B, "model": "grok-4.7[1m]", "prompt_base_sha": svc.prompt_sha(self.C),
                                    "prompt_saved_at": self.SAVED_AT, "prompt_saved_by": self.SAVED_BY})
        self.assertEqual(self.reconcile(document, self.C), (document, None))

    def test_a_newer_edit_replaces_the_kept_one(self) -> None:
        older = {"prompt": "Prompt B0.",
                 "saved_at": None, "saved_by": None, "superseded_at": "2026-10-01T00:00:00+00:00"}
        value = {"prompt": "Prompt B1.", "prompt_base_sha": svc.prompt_sha(self.A), "superseded": older}
        document, action = self.reconcile(value, self.C)
        self.assertEqual(action, "superseded")
        self.assertEqual(document["superseded"]["prompt"], "Prompt B1.")
        self.assertNotIn("prompt", document)
        self.assertNotIn("prompt_base_sha", document)

    def test_a_document_without_a_prompt_is_never_touched(self) -> None:
        self.assertEqual(self.reconcile({"required": True}, self.C), ({"required": True}, None))
        self.assertEqual(self.reconcile({}, self.C), ({}, None))
        with self.assertRaises(ValueError):
            self.reconcile({"prompt": "x", "required": "garbage"}, self.C)

    def test_the_stored_document_accepts_the_server_fields_the_put_body_refuses(self) -> None:
        base = svc.prompt_sha(self.A)
        superseded = {"prompt": " B ",
                      "saved_at": self.SAVED_AT, "saved_by": self.SAVED_BY, "superseded_at": self.NOW}
        document = svc.validate_agent_document({"prompt": " B ", "prompt_base_sha": base, "superseded": superseded, "required": True})
        self.assertEqual(document["prompt"], "B")
        self.assertEqual(document["prompt_base_sha"], base)
        self.assertEqual(document["superseded"], dict(superseded, prompt="B"))
        self.assertEqual(svc.validate_agent_document({"prompt_base_sha": base, "required": True}), {"required": True},
                         "a base without a prompt means nothing and is dropped")
        self.assertEqual(svc.validate_agent_document({"prompt": "x", "prompt_base_sha": None, "superseded": None}), {"prompt": "x"})
        for bad in (
            {"prompt": "x", "prompt_base_sha": "nope"},
            {"prompt": "x", "prompt_base_sha": 7},
            {"superseded": "x"},
            {"superseded": {"prompt": "x"}},
            {"superseded": dict(superseded, prompt="  ")},
            {"superseded": dict(superseded, superseded_at="  ")},
            {"superseded": dict(superseded, extra=1)},
            {"prompt": "x", "focus": "y"},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                svc.validate_agent_document(bad)
        # The PUT body never carries the server-managed fields.
        for field in ("prompt_base_sha", "superseded"):
            with self.subTest(field=field), self.assertRaises(ValueError) as caught:
                svc.validate_agent_override({"prompt": "x", field: base})
            self.assertIn("unknown fields: " + field, str(caught.exception))


class PromptDefaultDaemonTest(unittest.TestCase):
    """resolve_agent_settings reconciles each stored prompt with its file at attempt start."""

    def setUp(self) -> None:
        from code_review_service.dashboard_store import DashboardStore

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config_dir = self.tmp / "config"
        self.config_dir.mkdir()
        self.prompt_file = self.tmp / "agents" / "a.md"
        self.prompt_file.parent.mkdir()
        self.prompt_file.write_text("Prompt A.\n", encoding="utf-8")
        self.store = DashboardStore(self.tmp / "dashboard" / "dashboard.sqlite3")
        self.agents = [{"id": "a", "prompt_path": "../agents/a.md"}, {"id": "b", "prompt_path": "../agents/missing.md"}]
        self.logged: list[str] = []
        self.recorder = dx.DashboardRecorder(self.store, logger=self.logged.append)

    def resolve(self) -> dict:
        with mock.patch.object(svc, "log", self.logged.append):
            return svc.resolve_agent_settings(self.agents, self.recorder, config_dir=self.config_dir)

    def test_a_changed_file_replaces_the_edit_once_and_a_rollback_restores_it(self) -> None:
        self.store.merge_agent_setting("a", {"prompt": "Prompt B.", "required": True}, "owner@company.example", svc.validate_agent_document)
        # Attempt 1: the file is what the edit was made against; the legacy edit is adopted and runs.
        self.assertEqual(self.resolve()["a"], {"required": True, "prompt": "Prompt B.", "model": None})
        self.assertEqual(self.store.get_agent_setting("a")["value"]["prompt_base_sha"], svc.prompt_sha("Prompt A.\n"))
        # The file changes: attempt 2 runs the file and keeps the edit for compare.
        self.prompt_file.write_text("Prompt C.\n", encoding="utf-8")
        self.assertEqual(self.resolve()["a"], {"required": True, "prompt": None, "model": None})
        value = self.store.get_agent_setting("a")["value"]
        self.assertNotIn("prompt", value)
        self.assertEqual(value["required"], True)
        self.assertEqual((value["superseded"]["prompt"], value["superseded"]["saved_by"]), ("Prompt B.", "owner@company.example"))
        self.assertTrue(any("agent a" in line and "replaced" in line for line in self.logged), self.logged)
        events = self.store.events_after(0, 50)
        self.assertEqual(events[-1]["kind"], "agent_prompt_superseded")
        self.assertEqual(events[-1]["payload"], {"key": "agent:a", "agent_id": "a"})
        self.assertNotIn("Prompt B", json.dumps(events))
        # Attempt 3: nothing more to replace.
        self.assertIsNone(self.resolve()["a"]["prompt"])
        self.assertEqual(len(self.store.events_after(0, 50)), len(events))
        # Rollback: B runs again until the file changes once more.
        self.store.restore_superseded_prompt("a", svc.prompt_sha("Prompt B."), lambda: svc.prompt_sha("Prompt C.\n"), "owner@company.example", svc.validate_agent_document)
        self.assertEqual(self.resolve()["a"]["prompt"], "Prompt B.")
        self.assertEqual(self.resolve()["a"]["prompt"], "Prompt B.")
        self.prompt_file.write_text("Prompt D.\n", encoding="utf-8")
        self.assertIsNone(self.resolve()["a"]["prompt"])
        self.assertEqual(self.store.get_agent_setting("a")["value"]["superseded"]["prompt"], "Prompt B.")

    def test_the_file_is_read_inside_the_reconcile_transaction(self) -> None:
        # A deploy and a save that land between the attempt's start and its store
        # write: the save was made against the new file, so nothing is replaced.
        by = "owner@company.example"
        self.store.merge_agent_setting("a", {"prompt": "Prompt B.", "prompt_base_sha": svc.prompt_sha("Prompt A.\n")}, by, svc.validate_agent_document)
        real = self.store.reconcile_agent_setting

        def deploy_then_save(*args, **kwargs):
            self.prompt_file.write_text("Prompt C.\n", encoding="utf-8")
            self.store.merge_agent_setting(
                "a", {"prompt": "Prompt E.", "prompt_base_sha": svc.prompt_sha("Prompt C.\n"), "superseded": None}, by, svc.validate_agent_document
            )
            return real(*args, **kwargs)

        with mock.patch.object(self.store, "reconcile_agent_setting", side_effect=deploy_then_save):
            self.assertEqual(self.resolve()["a"]["prompt"], "Prompt E.")
        value = self.store.get_agent_setting("a")["value"]
        self.assertEqual((value["prompt"], value.get("superseded")), ("Prompt E.", None))

    def test_a_config_change_during_the_attempt_leaves_the_reconcile_to_the_next_one(self) -> None:
        # A deploy moves prompt_path after this attempt loaded its config, and an
        # admin saves an edit against the new file before the reconcile runs:
        # the attempt's old file must not replace that edit.
        config_path = self.config_dir / "reviewer.json"

        def write_config(prompt_path: str) -> None:
            config_path.write_text(json.dumps({"repositories": ["owner/repo"], "agents": [
                {"id": "a", "prompt_path": prompt_path}, {"id": "b", "prompt_path": "../agents/missing.md"},
                {"id": "c", "prompt_path": "../agents/c.md"}]}), encoding="utf-8")

        write_config("../agents/a.md")
        svc.load_config(config_path)  # valid: the check below compares prompt paths, it does not fail to load
        (self.prompt_file.parent / "a2.md").write_text("Prompt D.\n", encoding="utf-8")
        by = "owner@company.example"
        self.store.merge_agent_setting("a", {"prompt": "Prompt B.", "prompt_base_sha": svc.prompt_sha("Prompt A.\n")}, by, svc.validate_agent_document)
        real = self.store.reconcile_agent_setting

        def deploy_then_save(*args, **kwargs):
            write_config("../agents/a2.md")
            self.store.merge_agent_setting(
                "a", {"prompt": "Prompt E.", "prompt_base_sha": svc.prompt_sha("Prompt D.\n"), "superseded": None}, by, svc.validate_agent_document
            )
            return real(*args, **kwargs)

        with mock.patch.object(self.store, "reconcile_agent_setting", side_effect=deploy_then_save), \
                mock.patch.object(svc, "log", self.logged.append):
            resolved = svc.resolve_agent_settings(self.agents, self.recorder, config_dir=self.config_dir, config_path=config_path)
        self.assertEqual(resolved["a"]["prompt"], "Prompt E.")
        value = self.store.get_agent_setting("a")["value"]
        self.assertEqual((value["prompt"], value.get("superseded")), ("Prompt E.", None))
        self.assertTrue(any("agent a:" in line and "config changed" in line for line in self.logged), self.logged)
        # The next attempt loads the moved config: the reconcile runs again, and a change of the new file replaces E once.
        svc.load_config(config_path)
        (self.prompt_file.parent / "a2.md").write_text("Prompt F.\n", encoding="utf-8")
        moved = [{"id": "a", "prompt_path": "../agents/a2.md"}, {"id": "b", "prompt_path": "../agents/missing.md"}]
        with mock.patch.object(svc, "log", self.logged.append):
            resolved = svc.resolve_agent_settings(moved, self.recorder, config_dir=self.config_dir, config_path=config_path)
        self.assertIsNone(resolved["a"]["prompt"])
        self.assertEqual(self.store.get_agent_setting("a")["value"]["superseded"]["prompt"], "Prompt E.")

    def test_an_unreadable_file_under_an_edit_is_logged(self) -> None:
        self.store.merge_agent_setting("b", {"prompt": "Prompt B."}, "owner@company.example", svc.validate_agent_document)
        self.assertEqual(self.resolve()["b"]["prompt"], "Prompt B.")
        self.assertTrue(any(line.startswith("WARNING agent b:") and "cannot be read" in line for line in self.logged), self.logged)
        self.assertFalse(any(line.startswith("WARNING agent a:") for line in self.logged), "a readable file is not reported")

    def test_an_unreadable_file_or_a_failing_store_keeps_the_edit(self) -> None:
        self.store.merge_agent_setting("b", {"prompt": "Prompt B."}, "owner@company.example", svc.validate_agent_document)
        self.assertEqual(self.resolve()["b"]["prompt"], "Prompt B.", "no file to compare with: today's behaviour")
        self.assertNotIn("prompt_base_sha", self.store.get_agent_setting("b")["value"])
        self.store.merge_agent_setting("a", {"prompt": "Prompt B."}, "owner@company.example", svc.validate_agent_document)
        self.prompt_file.write_text("Prompt C.\n", encoding="utf-8")
        with mock.patch.object(self.store, "reconcile_agent_setting", side_effect=RuntimeError("locked")):
            self.assertEqual(self.resolve()["a"]["prompt"], "Prompt B.", "a store that cannot be written: no replacement this time")
        self.assertTrue(any("WARNING" in line and "agent:a" in line for line in self.logged), self.logged)
        # The call shape without a config dir (and a dry run's null recorder) never reconciles.
        self.assertEqual(svc.resolve_agent_settings(self.agents, self.recorder)["a"]["prompt"], "Prompt B.")
        self.assertIsNone(svc.resolve_agent_settings(self.agents, dx.DashboardRecorder(None), config_dir=self.config_dir)["a"]["prompt"])
        self.assertEqual(self.store.get_setting("agent:a")["value"]["prompt"], "Prompt B.")


class AgentModelLadderTest(unittest.TestCase):
    LADDER = {"model": "gpt-6.1-sol[1m]", "effort": "max", "fallback_models": ["grok-4.7[1m]"], "source": "dashboard"}

    def test_no_override_or_no_ladder_returns_the_ladder_itself(self) -> None:
        ladder = dict(self.LADDER)
        self.assertIs(svc.agent_model_ladder(ladder, None), ladder)
        self.assertIsNone(svc.agent_model_ladder(None, "gpt-6-astra[1m]"))
        self.assertEqual(svc.agent_model_ladder({}, "gpt-6-astra[1m]"), {})

    def test_a_retry_request_model_beats_the_agent_override(self) -> None:
        ladder = {**self.LADDER, "source": "request"}
        self.assertIs(svc.agent_model_ladder(ladder, "gpt-6-astra[1m]"), ladder)

    def test_the_override_leads_and_the_default_ladder_follows(self) -> None:
        # Deep copies: a shallow one would share fallback_models with LADDER and
        # hide an in-place change to the attempt's ladder.
        ladder = json.loads(json.dumps(self.LADDER))
        agent = svc.agent_model_ladder(ladder, "gpt-6-astra[1m]")
        self.assertEqual(agent, {
            "model": "gpt-6-astra[1m]",
            "effort": "max",
            "fallback_models": ["gpt-6.1-sol[1m]", "grok-4.7[1m]"],
            "source": "dashboard",
        })
        self.assertEqual(ladder, {"model": "gpt-6.1-sol[1m]", "effort": "max", "fallback_models": ["grok-4.7[1m]"], "source": "dashboard"},
                         "the attempt's ladder is shared and never changed")
        argv = svc.render_runner_command(["run", svc.MODEL_POLICY_ARGS_TOKEN], {}, agent)
        self.assertEqual(argv, ["run", "--model", "gpt-6-astra[1m]", "--effort", "max",
                                "--fallback-model", "gpt-6.1-sol[1m]", "--fallback-model", "grok-4.7[1m]"])

    def test_the_override_is_not_repeated_and_no_policy_fallback_is_cut(self) -> None:
        deduped = svc.agent_model_ladder(dict(self.LADDER), "grok-4.7[1m]")
        self.assertEqual((deduped["model"], deduped["fallback_models"]), ("grok-4.7[1m]", ["gpt-6.1-sol[1m]"]))
        self.assertEqual(svc.agent_model_ladder(dict(self.LADDER), "gpt-6.1-sol[1m]")["fallback_models"], ["grok-4.7[1m]"])
        full = {"model": "m0", "effort": "", "fallback_models": [f"m{i}" for i in range(1, svc.MAX_FALLBACK_MODELS + 1)], "source": "config"}
        whole = svc.agent_model_ladder(full, "astra")
        self.assertEqual(whole["fallback_models"], [f"m{i}" for i in range(svc.MAX_FALLBACK_MODELS + 1)],
                         "the policy's last fallback still follows an overridden agent")

    def test_run_agent_never_reports_an_empty_failure_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "agent.md").write_text("prompt", encoding="utf-8")
            agent = {"id": "arch", "prompt_path": "agent.md"}
            config = {"agent_runner": {"command": ["true"]}}
            pr = {"number": 1, "title": "t", "body": "", "head": {"sha": HEAD}}

            def run(label, returncode, stdout, response=None):
                run_dir = tmp_path / label
                run_dir.mkdir()

                def runner(*args, **kwargs):
                    # The runner writes the response file; stdout stays empty.
                    if response is not None:
                        (run_dir / svc.REVIEW_ARTIFACT_DIR / "arch.response.json").write_text(response, encoding="utf-8")
                    return mock.Mock(returncode=returncode, stdout=stdout, stderr="")

                with mock.patch.object(svc, "run_command", side_effect=runner):
                    return svc.run_agent(agent, config, tmp_path, run_dir, "o/r", pr, [], "")

            exit_code = run("exit", 1, "")
            self.assertEqual(exit_code["error"], "agent failed with exit code 1")
            invalid = run("invalid", 0, "", response="not json at all")
            self.assertEqual(invalid["error"], "agent returned invalid JSON: no JSON object found")
            echoed_null = run("null", 0, '{"summary": "ok", "findings": [], "error": null}')
            self.assertFalse(svc.agent_failed(echoed_null), "a model echoing a null error has not failed")
            echoed = run("echo", 0, '{"summary": "x", "findings": [], "error": "cannot review"}')
            self.assertTrue(svc.agent_failed(echoed), "a model's own non-empty error still fails it, as before")

    def test_run_agent_renders_the_override_prompt_and_falls_back_to_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "agent.md").write_text("FILE PROMPT TEXT", encoding="utf-8")
            agent = {"id": "arch", "prompt_path": "agent.md"}
            config = {"agent_runner": {"command": ["true"]}}
            pr = {"number": 1, "title": "t", "body": "", "head": {"sha": HEAD}}
            done = mock.Mock(returncode=0, stdout='{"summary": "ok", "findings": []}', stderr="")
            rendered = {}
            for label, kwargs in (("override", {"prompt_text": "DASHBOARD PROMPT TEXT"}), ("file", {})):
                run_dir = tmp_path / label
                run_dir.mkdir()
                with mock.patch.object(svc, "run_command", return_value=done):
                    result = svc.run_agent(agent, config, tmp_path, run_dir, "o/r", pr, [], "", **kwargs)
                self.assertNotIn("error", result)
                rendered[label] = (run_dir / svc.REVIEW_ARTIFACT_DIR / "arch.prompt.md").read_text(encoding="utf-8")
        self.assertIn("DASHBOARD PROMPT TEXT", rendered["override"])
        self.assertNotIn("FILE PROMPT TEXT", rendered["override"])
        self.assertIn("FILE PROMPT TEXT", rendered["file"])


class _RecordingClient(_FakeClient):
    """Also keeps what each review POST carried."""

    def __init__(self) -> None:
        super().__init__()
        self.reviews: list = []

    def create_review(self, number, commit_id, body, comments, event="COMMENT"):
        self.reviews.append({"body": body, "comments": comments})
        return super().create_review(number, commit_id, body, comments, event)


class _ReviewPrHarness(unittest.TestCase):
    """One review_pr call against fake agents, a fake store and a fake GitHub."""

    def _run(self, *, failing=(), required=("arch",), settings=None, minimum=2, state=None, head=HEAD, error_text="timeout",
             reuse_results=None, model_policy=None, base=BASE, fresh_llm=None, files=None, language=None):
        store = FakeStore()
        store.settings.update(settings or {})
        if language is not None:
            # The PR author's own setting (@review-of-legends lang); already welcomed,
            # so the review posts no welcome comment.
            store.get_pr_author = lambda login: {"login": login, "lang": language, "welcomed_at": "2026-10-01T00:00:00+00:00"}
        recorder = dx.DashboardRecorder(store, logger=lambda message: None)
        client = _RecordingClient()
        if files is not None:
            client.list_files = lambda number: files
        state = state if state is not None else {"reviewed": {}}
        calls: dict = {}
        policies = self.policies = {}  # agent id -> the ladder run_agent was given
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = _config(tmp_path / "work", required=required, minimum=minimum)
            # The daemon reconciles a dashboard prompt only while the config on
            # disk still names the agent's prompt file (resolve_agent_settings).
            (tmp_path / "config").mkdir()
            config_path = _write_config(tmp_path / "config", {**config, "repositories": ["owner/repo"]})
            state_path = tmp_path / "state.json"
            pr = {"number": 3, "head": {"sha": head}, "base": {"sha": base}, "title": "t", "body": "", "user": {"login": "x"}}

            def fake_run_agent(agent, *args, **kwargs):
                calls[agent["id"]] = kwargs.get("prompt_text")
                policies[agent["id"]] = kwargs.get("policy")
                if agent["id"] in failing:
                    return {"agent": agent["id"], "summary": "agent timed out after 1800s", "findings": [], "error": error_text}
                result = {"agent": agent["id"], "summary": "ok", "findings": []}
                if fresh_llm is not None:
                    result["llm"] = dict(fresh_llm)
                return result

            with mock.patch.object(svc, "run_agent", fake_run_agent), \
                 mock.patch.object(svc, "clone_pr_source", lambda repo, n, h, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True)), \
                 mock.patch.object(svc, "load_repository_rules", lambda *a, **k: ""):
                posted = svc.review_pr(client, config, config_path, pr, state,
                                       state_path=state_path, recorder=recorder,
                                       reuse_results=reuse_results, model_policy=model_policy)
            saved = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
        run = next(iter(store.runs.values()))
        return posted, client, run, state, saved, calls


class ReviewPrRequiredTest(_ReviewPrHarness):
    """review_pr ends the attempt when a required agent errors, before the quorum check."""

    def test_a_failed_required_agent_ends_the_attempt_without_posting(self) -> None:
        posted, client, run, state, saved, _ = self._run(failing=("arch",))
        self.assertFalse(posted)
        self.assertEqual(client.review_calls, 0)
        self.assertEqual((run["status"], run["stage"]), ("failed", "required"))
        self.assertIn("required agent(s) failed: arch — agent timed out after 1800s", run["error"])
        self.assertEqual((run["successful_agents"], run["total_agents"]), (2, 3))
        note = client.progress_bodies[-1]
        self.assertIn("**Phase:** Review not posted", note)
        self.assertIn("Required agent arch failed: agent timed out after 1800s.", note)
        self.assertIn("The service tries again automatically, not before", note)
        self.assertIn("| arch (required) |", note)
        entry = state["failed"]["3"]
        self.assertEqual((entry["head_sha"], entry["attempts"], entry["stage"]), (HEAD, 1, "required"))
        self.assertEqual(saved["failed"]["3"], entry, "the failure count is flushed like a posted review")
        self.assertNotIn("3", state["reviewed"])

    def test_an_empty_error_still_counts_as_a_failure(self) -> None:
        """An ``"error": ""`` result used to pass as successful (truthiness)."""
        posted, client, run, _, _, _ = self._run(failing=("a", "b"), required=(), error_text="")
        self.assertFalse(posted)
        self.assertEqual(client.review_calls, 0)
        self.assertEqual((run["stage"], run["successful_agents"], run["total_agents"]), ("quorum", 1, 3))
        posted, client, run, _, _, _ = self._run(failing=("arch",), error_text="")
        self.assertFalse(posted)
        self.assertEqual((run["stage"], run["successful_agents"]), ("required", 2))
        self.assertIn("| arch (required) |", client.progress_bodies[-1])
        self.assertIn("❌ Failed", client.progress_bodies[-1])

    def test_a_failed_optional_agent_with_the_quorum_met_still_posts(self) -> None:
        posted, client, run, state, _, _ = self._run(failing=("b",))
        self.assertTrue(posted)
        self.assertEqual(client.review_calls, 1)
        self.assertEqual(run["status"], "succeeded")
        self.assertIn("3", state["reviewed"])

    def test_a_dashboard_override_flips_required_both_ways(self) -> None:
        posted, client, _, _, _, _ = self._run(failing=("arch",), settings={"agent:arch": {"required": False}})
        self.assertTrue(posted, "required switched off on the dashboard")
        self.assertEqual(client.review_calls, 1)
        self.assertNotIn("(required)", client.progress_bodies[-1])

        posted, client, run, _, _, _ = self._run(failing=("b",), settings={"agent:b": {"required": True}})
        self.assertFalse(posted, "required switched on on the dashboard")
        self.assertEqual(client.review_calls, 0)
        self.assertEqual(run["stage"], "required")
        self.assertIn("Required agent b failed", client.progress_bodies[-1])

    def test_quorum_failure_is_counted_and_attempts_restart_on_a_new_head(self) -> None:
        state = {"reviewed": {}}
        self._run(failing=("a", "b"), required=(), state=state)
        self._run(failing=("a", "b"), required=(), state=state)
        self.assertEqual((state["failed"]["3"]["attempts"], state["failed"]["3"]["stage"]), (2, "quorum"))
        self._run(failing=("a", "b"), required=(), state=state, head="def456789012")
        self.assertEqual((state["failed"]["3"]["head_sha"], state["failed"]["3"]["attempts"]), ("def456789012", 1))

    def test_the_last_automatic_attempt_says_retries_stopped(self) -> None:
        state = {"reviewed": {}, "failed": {"3": {"head_sha": HEAD, "attempts": 2, "last_failed_at": "2026-10-13T00:00:00+00:00", "stage": "required"}}}
        _, client, _, _, _, _ = self._run(failing=("arch",), state=state)
        self.assertEqual(state["failed"]["3"]["attempts"], 3)
        self.assertIn("Automatic retries stopped after 3 failed attempts on this commit; an admin can retry from the dashboard.",
                      client.progress_bodies[-1])

    def test_a_posted_review_clears_the_failure_count(self) -> None:
        state = {"reviewed": {}, "failed": {"3": {"head_sha": HEAD, "attempts": 2, "last_failed_at": "2026-10-13T00:00:00+00:00", "stage": "quorum"}}}
        posted, _, _, state, saved, _ = self._run(state=state)
        self.assertTrue(posted)
        self.assertNotIn("3", state["failed"])
        self.assertNotIn("3", saved["failed"])

    def test_only_a_dashboard_prompt_is_passed_to_run_agent(self) -> None:
        _, _, _, _, _, calls = self._run(settings={"agent:a": {"prompt": "dashboard prompt for a"}})
        self.assertEqual(calls, {"a": "dashboard prompt for a", "b": None, "arch": None})

    def test_an_attempt_replaces_a_stored_prompt_whose_file_changed(self) -> None:
        # review_pr must hand resolve_agent_settings the config dir: without it
        # no attempt ever applies a changed default, and only the Agents page would.
        stale = {"prompt": "Edited B", "prompt_base_sha": "0" * 64}
        with mock.patch.object(svc, "read_agent_prompt_file", lambda agent, config_dir: "Default C\n"):
            _, _, _, _, _, calls = self._run(settings={"agent:a": stale})
        self.assertEqual(calls, {"a": None, "b": None, "arch": None}, "the changed prompt file runs, not the edit it replaced")

    def test_an_attempt_whose_config_moved_on_leaves_the_reconcile_to_the_next(self) -> None:
        # review_pr must hand resolve_agent_settings the config path: a deploy that
        # moved prompt_path since this attempt loaded its config skips the reconcile.
        stale = {"prompt": "Edited B", "prompt_base_sha": "0" * 64}
        moved = {"repositories": ["owner/repo"], "agents": [{"id": "a", "prompt_path": "../agents/moved.md"}]}
        with mock.patch.object(svc, "read_agent_prompt_file", lambda agent, config_dir: "Default C\n"), \
                mock.patch.object(svc, "load_config", lambda path: moved):
            _, _, _, _, _, calls = self._run(settings={"agent:a": stale})
        self.assertEqual(calls["a"], "Edited B", "the edit runs; the next attempt compares it with the moved file")

    def test_a_dashboard_model_runs_only_its_agent_on_the_override_ladder(self) -> None:
        policy = {"model": "gpt-6.1-sol[1m]", "effort": "max", "fallback_models": ["grok-4.7[1m]"]}
        # The harness argv ("true") takes no model policy, so nothing would be
        # published; pretend it does to see the policy line the PR gets.
        with mock.patch.object(svc, "policy_applies_everywhere", lambda *args, **kwargs: True):
            posted, client, run, _, _, _ = self._run(settings={
                dx.MODEL_POLICY_SETTING: policy,
                "agent:arch": {"model": "gpt-6-astra[1m]"},
            })
        self.assertTrue(posted)
        self.assertIn("**Model policy:** `gpt-6.1-sol[1m]` (effort max) → `grok-4.7[1m]` · source: dashboard"
                      " · agent models: arch → `gpt-6-astra[1m]`", client.progress_bodies[-1])
        self.assertEqual(self.policies["arch"], {
            "model": "gpt-6-astra[1m]",
            "effort": "max",
            "fallback_models": ["gpt-6.1-sol[1m]", "grok-4.7[1m]"],
            "source": "dashboard",
        })
        default = {**policy, "source": "dashboard", "agent_models": {"arch": "gpt-6-astra[1m]"}}
        self.assertEqual(self.policies["a"], default)
        self.assertIs(self.policies["a"], self.policies["b"], "every other agent shares the attempt's ladder")

    def test_a_retry_request_model_beats_a_dashboard_model(self) -> None:
        # Publish the policy line (the harness argv takes none) so it can be checked.
        with mock.patch.object(svc, "policy_applies_everywhere", lambda *args, **kwargs: True):
            posted, client, _, _, _, _ = self._run(settings={"agent:arch": {"model": "gpt-6-astra[1m]"}},
                                                   model_policy={"model": "grok-4.7[1m]", "effort": "high", "fallback_models": []})
        self.assertTrue(posted)
        self.assertEqual({agent_id: ladder["model"] for agent_id, ladder in self.policies.items()},
                         {"a": "grok-4.7[1m]", "b": "grok-4.7[1m]", "arch": "grok-4.7[1m]"})
        self.assertEqual(self.policies["arch"]["source"], "request")
        self.assertIn("**Model policy:** `grok-4.7[1m]` (effort high) · no fallback · source: request\n", client.progress_bodies[-1])
        self.assertNotIn("agent models", "\n".join(client.progress_bodies))


#: The diff every harness review reads (the fake client's PR files) unless a test changes it.
DIFF = svc.review_diff_fingerprint(_FakeClient().list_files(3))


def _kept(agent_id: str) -> dict:
    """A success an earlier attempt kept: findings, a verdict and the models it ran on."""
    return {
        "agent": agent_id,
        "summary": f"kept summary of {agent_id}",
        "overall_correctness": "patch is correct",
        "findings": [{"path": "a.py", "line": 2, "priority": 2, "title": f"[P2] kept finding of {agent_id}", "body": "b"}],
        "llm": {"model": "gpt-6-astra[1m]", "effort": "max", "input_tokens": 7, "output_tokens": 3},
        "reviewed_base_sha": BASE,
        "reviewed_diff_sha": DIFF,
        "reviewed_language": "",
    }


def _reuse(*agent_ids: str, head: str = HEAD) -> dict:
    return {"run_id": "run-earlier", "head_sha": head, "results": {agent_id: _kept(agent_id) for agent_id in agent_ids}}


class ReviewPrReuseTest(_ReviewPrHarness):
    """A dashboard retry of a failed attempt runs only the agents that attempt kept no result for."""

    def test_only_the_agents_without_a_kept_result_run_and_the_quorum_counts_the_reused(self) -> None:
        posted, client, run, _, _, calls = self._run(required=(), minimum=3, reuse_results=_reuse("a", "b"))
        self.assertTrue(posted)
        self.assertEqual(calls, {"arch": None}, "only the agent without a kept result runs")
        self.assertEqual((run["status"], run["successful_agents"], run["total_agents"]), ("succeeded", 3, 3))
        body = client.reviews[0]["body"]
        self.assertIn("- **a**: kept summary of a", body)
        self.assertIn("(a): [P2] kept finding of a", body, "reused findings are posted like fresh ones")
        self.assertIn("- **b**: patch is correct", body)
        rows = {row["id"]: row for row in run["agents"]}
        self.assertEqual({agent_id: row.get("reused") for agent_id, row in rows.items()}, {"a": True, "b": True, "arch": None})
        self.assertEqual((rows["a"]["status"], rows["a"]["model"]), ("done", "gpt-6-astra[1m]"), "the reused row keeps its models")
        progress = client.progress_bodies[-1]
        self.assertIn("Reused from the previous attempt (not run again): a, b.", progress)
        self.assertIn("| 1 | a | ", progress, "the table keeps its format")

    def test_a_required_agent_that_failed_runs_again_and_its_success_posts(self) -> None:
        posted, client, failed_run, _, _, calls = self._run(failing=("arch",))
        self.assertFalse(posted)
        self.assertEqual(failed_run["stage"], "required")
        kept = failed_run["agent_results"]
        stamp = {"reviewed_base_sha": BASE, "reviewed_diff_sha": DIFF, "reviewed_language": ""}
        self.assertEqual(kept, [{"agent": "a", "summary": "ok", "findings": [], **stamp},
                                {"agent": "b", "summary": "ok", "findings": [], **stamp}],
                         "what run_agent returned for each success, with the base, diff and language it was reviewed in")
        reuse = {"run_id": failed_run["id"], "head_sha": HEAD, "results": {item["agent"]: item for item in kept}}
        posted, client, run, state, _, calls = self._run(reuse_results=reuse)
        self.assertTrue(posted)
        self.assertEqual(calls, {"arch": None})
        self.assertEqual(client.review_calls, 1)
        self.assertEqual((run["status"], run["successful_agents"]), ("succeeded", 3))
        self.assertIn("3", state["reviewed"])

    def test_a_retry_that_fails_again_keeps_the_reused_results_too(self) -> None:
        posted, _, run, _, _, calls = self._run(failing=("arch",), reuse_results=_reuse("a", "b"))
        self.assertFalse(posted)
        self.assertEqual(calls, {"arch": None})
        self.assertEqual([item["agent"] for item in run["agent_results"]], ["a", "b"])
        self.assertEqual(run["agent_results"][0], {**_kept("a"), "reused": True}, "same base, so the next retry reuses it again")

    def test_reuse_is_ignored_on_a_different_head(self) -> None:
        with mock.patch.object(svc, "log") as logged:
            posted, client, run, _, _, calls = self._run(reuse_results=_reuse("a", "b", head="def456789012"))
        self.assertTrue(posted)
        self.assertEqual(set(calls), {"a", "b", "arch"}, "every agent runs")
        self.assertNotIn("Reused", client.progress_bodies[-1])
        self.assertFalse(any(row.get("reused") for row in run["agents"]))
        lines = [call.args[0] for call in logged.call_args_list]
        self.assertTrue(any("not reusing earlier agent results (they are for def456789012, not abc123456789)" in line for line in lines), lines)

    def test_only_successes_of_agents_this_repository_still_runs_are_reused(self) -> None:
        reuse = _reuse("a", "gone")
        reuse["results"]["b"] = {**_kept("b"), "summary": "timed out", "error": "timeout"}  # on the right base: only its failure bars it
        reuse["results"]["arch"] = {**_kept("arch"), "agent": "someone-else"}
        with mock.patch.object(svc, "log") as logged:
            posted, _, _, _, _, calls = self._run(reuse_results=reuse)
        self.assertTrue(posted)
        self.assertEqual(set(calls), {"b", "arch"})
        lines = [call.args[0] for call in logged.call_args_list]
        self.assertIn("owner/repo pr #3: reusing results of a from attempt run-earlier; running b, arch", lines)

    def test_a_pinned_retry_model_runs_only_the_rerun_agents(self) -> None:
        pinned = {"model": "grok-4.7[1m]", "effort": "high", "fallback_models": []}
        posted, _, run, _, _, _ = self._run(reuse_results=_reuse("a"), model_policy=pinned)
        self.assertTrue(posted)
        self.assertEqual({agent_id: ladder["model"] for agent_id, ladder in self.policies.items()},
                         {"b": "grok-4.7[1m]", "arch": "grok-4.7[1m]"})
        reused = next(row for row in run["agents"] if row["id"] == "a")
        self.assertEqual((reused["model"], reused["effort"]), ("gpt-6-astra[1m]", "max"))

    def test_every_agent_reused_runs_none_and_still_posts(self) -> None:
        posted, client, run, _, _, calls = self._run(reuse_results=_reuse("a", "b", "arch"))
        self.assertTrue(posted)
        self.assertEqual(calls, {})
        self.assertEqual(client.review_calls, 1)
        self.assertEqual(run["successful_agents"], 3)

    def test_an_agent_that_succeeds_on_a_failed_retry_is_kept_for_the_next_one(self) -> None:
        """Retry 1 reuses a, b now succeeds, arch fails again; retry 2 then runs arch alone."""
        posted, _, second, _, _, calls = self._run(failing=("arch",), reuse_results=_reuse("a"))
        self.assertFalse(posted)
        self.assertEqual(set(calls), {"b", "arch"})
        self.assertEqual([item["agent"] for item in second["agent_results"]], ["a", "b"], "the reused and the new success")
        reuse = {"run_id": second["id"], "head_sha": HEAD,
                 "results": {item["agent"]: item for item in second["agent_results"]}}
        posted, _, third, _, _, calls = self._run(reuse_results=reuse)
        self.assertTrue(posted)
        self.assertEqual(calls, {"arch": None}, "only the agent that never succeeded runs")
        self.assertEqual((third["status"], third["successful_agents"]), ("succeeded", 3))

    def test_reuse_is_ignored_when_the_diff_changed_on_the_same_head_and_base(self) -> None:
        """The base took in some of the PR's commits: same head, same base.sha, a smaller diff."""
        # Only the patch text differs: same path, status and counts, as when the base
        # took in the commit that used to add the earlier hunk.
        changed = [{**item, "patch": "@@ -1,1 +1,2 @@\n context\n+added later\n"} for item in _FakeClient().list_files(3)]
        self.assertEqual([{k: v for k, v in item.items() if k != "patch"} for item in changed],
                         [{k: v for k, v in item.items() if k != "patch"} for item in _FakeClient().list_files(3)])
        with mock.patch.object(svc, "log") as logged:
            posted, _, run, _, _, calls = self._run(reuse_results=_reuse("a", "b"), files=changed)
        self.assertTrue(posted)
        self.assertEqual(set(calls), {"a", "b", "arch"}, "every agent reviews the diff it has not seen")
        self.assertFalse(any(row.get("reused") for row in run["agents"]))
        lines = [call.args[0] for call in logged.call_args_list]
        self.assertTrue(any("they were reviewed against another base or diff" in line for line in lines), lines)

    def test_reuse_is_ignored_when_the_pr_moved_to_another_base(self) -> None:
        """Same head, other base (a retargeted PR): the diff and the rules differ, so every agent runs."""
        _, _, failed_run, _, _, _ = self._run(failing=("arch",))
        reuse = {"run_id": failed_run["id"], "head_sha": HEAD,
                 "results": {item["agent"]: item for item in failed_run["agent_results"]}}
        with mock.patch.object(svc, "log") as logged:
            posted, client, run, _, _, calls = self._run(reuse_results=reuse, base="c" * 12)
        self.assertTrue(posted)
        self.assertEqual(set(calls), {"a", "b", "arch"}, "every agent runs on the new base")
        self.assertFalse(any(row.get("reused") for row in run["agents"]))
        self.assertNotIn("Reused", client.progress_bodies[-1])
        lines = [call.args[0] for call in logged.call_args_list]
        self.assertTrue(any("not reusing earlier agent results (they were reviewed against another base or diff, not cccccccccccc)" in line
                            for line in lines), lines)

    def test_a_kept_result_without_its_base_or_diff_is_not_reused(self) -> None:
        for key in ("reviewed_base_sha", "reviewed_diff_sha"):
            reuse = _reuse("a", "b")
            for result in reuse["results"].values():
                del result[key]
            posted, _, _, _, _, calls = self._run(reuse_results=reuse)
            self.assertTrue(posted)
            self.assertEqual(set(calls), {"a", "b", "arch"}, key)

    def test_reuse_needs_a_known_base(self) -> None:
        # Kept on the same unknown base: the stamps match, so only the unknown-base guard refuses them.
        reuse = _reuse("a", "b")
        for result in reuse["results"].values():
            result["reviewed_base_sha"] = ""
        posted, _, _, _, _, calls = self._run(reuse_results=reuse, base="")
        self.assertTrue(posted)
        self.assertEqual(set(calls), {"a", "b", "arch"}, "an unknown base cannot match the kept one")

    def test_reuse_is_ignored_when_the_review_language_changed(self) -> None:
        """Same head, base and diff, but the author set ``ko`` since: the kept results are in the default language."""
        with mock.patch.object(svc, "log") as logged:
            posted, client, run, _, _, calls = self._run(reuse_results=_reuse("a", "b"), language="ko")
        self.assertTrue(posted)
        self.assertEqual(set(calls), {"a", "b", "arch"}, "every agent writes its review in the new language")
        self.assertFalse(any(row.get("reused") for row in run["agents"]))
        self.assertNotIn("Reused", client.progress_bodies[-1])
        lines = [call.args[0] for call in logged.call_args_list]
        self.assertTrue(any("not reusing earlier agent results (they were written in the default language, not ko)" in line
                            for line in lines), lines)

    def test_a_result_kept_in_the_review_language_is_reused(self) -> None:
        reuse = _reuse("a", "b")
        for result in reuse["results"].values():
            result["reviewed_language"] = "ko"
        posted, _, _, _, _, calls = self._run(reuse_results=reuse, language="ko")
        self.assertTrue(posted)
        self.assertEqual(calls, {"arch": None})
        # And back: an author who cleared the language gets every agent again.
        with mock.patch.object(svc, "log") as logged:
            posted, _, _, _, _, calls = self._run(reuse_results=reuse)
        self.assertEqual(set(calls), {"a", "b", "arch"})
        lines = [call.args[0] for call in logged.call_args_list]
        self.assertTrue(any("(they were written in ko, not the default language)" in line for line in lines), lines)

    def test_a_result_kept_before_languages_were_stamped_counts_as_the_default_language(self) -> None:
        reuse = _reuse("a", "b")
        for result in reuse["results"].values():
            del result["reviewed_language"]
        posted, _, _, _, _, calls = self._run(reuse_results=reuse)
        self.assertTrue(posted)
        self.assertEqual(calls, {"arch": None}, "no language then, none now")
        posted, _, _, _, _, calls = self._run(reuse_results=reuse, language="ko")
        self.assertTrue(posted)
        self.assertEqual(set(calls), {"a", "b", "arch"}, "no language then, ko now")
        # The positional signature is unchanged: no language argument means none is set.
        agents = _agents("a", "b")
        self.assertEqual(set(svc.reused_agent_results(reuse, agents, HEAD, BASE, DIFF)[0]), {"a", "b"})

    def test_a_failed_attempt_keeps_the_language_and_a_retry_after_the_author_changed_it_runs_every_agent(self) -> None:
        _, _, failed_run, _, _, _ = self._run(failing=("arch",))
        self.assertEqual({item["agent"]: item.get("reviewed_language") for item in failed_run["agent_results"]},
                         {"a": "", "b": ""}, "no language set: the default language")
        reuse = {"run_id": failed_run["id"], "head_sha": HEAD,
                 "results": {item["agent"]: item for item in failed_run["agent_results"]}}
        # The author comments "@review-of-legends lang ko"; the retry fails again on arch.
        posted, client, second, _, _, calls = self._run(failing=("arch",), reuse_results=reuse, language="ko")
        self.assertFalse(posted)
        self.assertEqual(set(calls), {"a", "b", "arch"}, "nothing written in the default language is reused")
        self.assertNotIn("Reused", client.progress_bodies[-1])
        self.assertEqual({item["agent"]: item.get("reviewed_language") for item in second["agent_results"]},
                         {"a": "ko", "b": "ko"})
        # A retry in the same language reuses what the ko attempt kept.
        reuse = {"run_id": second["id"], "head_sha": HEAD,
                 "results": {item["agent"]: item for item in second["agent_results"]}}
        posted, _, third, _, _, calls = self._run(reuse_results=reuse, language="ko")
        self.assertTrue(posted)
        self.assertEqual(calls, {"arch": None})
        self.assertEqual((third["status"], third["successful_agents"]), ("succeeded", 3))

    def test_the_posted_review_counts_only_the_tokens_this_attempt_spent(self) -> None:
        """Reused results were counted by the attempt that ran them; the header names their models only."""
        reuse = _reuse("a", "b")
        del reuse["results"]["b"]["llm"]  # a reused result without usage is still not an invocation of this attempt
        fresh = {"model": "grok-4.7[1m]", "effort": "max", "input_tokens": 100, "output_tokens": 50}
        posted, client, _, _, _, calls = self._run(reuse_results=reuse, fresh_llm=fresh)
        self.assertTrue(posted)
        self.assertEqual(set(calls), {"arch"})
        body = client.reviews[0]["body"]
        self.assertIn("Tokens (reviewer agents only): in 100 / out 50 (1/1 runner invocations reported usage)", body)
        self.assertIn("Models used: `gpt-6-astra[1m]`, `grok-4.7[1m]`", body)
        self.assertIn("Agents: 3", body, "reused agents still count as agents of the review")


class AutoRetryGateTest(unittest.TestCase):
    CONFIG = {"auto_retry_attempts": 3, "auto_retry_backoff_seconds": 600}
    FAILED_AT = dt.datetime(2026, 10, 13, 12, 0, tzinfo=UTC)

    def _entry(self, attempts: int, head: str = HEAD) -> dict:
        return {"head_sha": head, "attempts": attempts, "last_failed_at": self.FAILED_AT.isoformat(), "stage": "required"}

    def test_backoff_doubles_and_the_cap_stops_automatic_retries(self) -> None:
        reason = svc.auto_retry_skip_reason
        self.assertIn("not before 2026-10-13 12:10 UTC", reason(self.CONFIG, self._entry(1), HEAD, self.FAILED_AT + dt.timedelta(minutes=9)))
        self.assertEqual(reason(self.CONFIG, self._entry(1), HEAD, self.FAILED_AT + dt.timedelta(minutes=10)), "")
        self.assertIn("not before 2026-10-13 12:20 UTC", reason(self.CONFIG, self._entry(2), HEAD, self.FAILED_AT + dt.timedelta(minutes=19)))
        self.assertEqual(reason(self.CONFIG, self._entry(2), HEAD, self.FAILED_AT + dt.timedelta(minutes=20)), "")
        self.assertIn("automatic retries stopped", reason(self.CONFIG, self._entry(3), HEAD, self.FAILED_AT + dt.timedelta(days=30)))
        self.assertEqual(reason(self.CONFIG, self._entry(3, head="other"), HEAD, self.FAILED_AT), "", "a new head is reviewed")
        self.assertEqual(reason(self.CONFIG, None, HEAD, self.FAILED_AT), "")
        # A huge attempt cap must not overflow the backoff arithmetic in the poll loop.
        self.assertIn("not before 9999-12-31", reason({"auto_retry_attempts": 500}, self._entry(400), HEAD, self.FAILED_AT))


class PollLoopRetryLimitTest(unittest.TestCase):
    """The limit lives in the poll loop only: the operator retry path still reviews."""

    def _run_once(self, failed: dict, prs: list, *, store=None, force: bool = False, raises=None, reviewed=None) -> list:
        calls: list = []
        self.saved_state: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = {
                "repositories": ["owner/repo"],
                "agents": _agents("a", "b", "c"),
                "agent_runner": {"command": ["true"]},
                "work_dir": str(tmp_path / "work"),
                "state_path": str(tmp_path / "state.json"),
                "review_existing_open_prs_on_first_run": True,
                "auto_retry_attempts": 3,
                "auto_retry_backoff_seconds": 600,
            }
            (tmp_path / "state.json").write_text(json.dumps({"repositories": {"owner/repo": {"reviewed": reviewed or {}, "failed": failed}}}), encoding="utf-8")

            class Client:
                def __init__(self, repo, gh_bin="gh"):
                    self.repo = repo

                def list_open_prs(self):
                    return [dict(pr) for pr in prs]

                def get_pr(self, number):
                    return next(dict(pr) for pr in prs if pr["number"] == number)

            def fake_review_pr(client, config, config_path, pr, state, **kwargs):
                recorder = kwargs.get("recorder")
                calls.append((int(pr["number"]), "retry" if recorder is not None and recorder.strict else "poll"))
                if raises is not None:
                    raise raises
                return False

            with mock.patch.object(svc, "GitHubClient", Client), \
                 mock.patch.object(svc, "load_config", return_value=config), \
                 mock.patch.object(svc, "expand_path", side_effect=lambda v, base=None: Path(str(v))), \
                 mock.patch.object(dx, "open_store", return_value=store), \
                 mock.patch.object(svc, "review_pr", fake_review_pr):
                svc.run_once(argparse.Namespace(config=str(CONFIG_PATH), repo=None, pr=None, dry_run=False, force=force))
            self.saved_state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        return calls

    @staticmethod
    def _pr(number: int, head: str) -> dict:
        return {"number": number, "state": "open", "head": {"sha": head}, "title": "t", "html_url": "u", "body": ""}

    def test_backoff_and_cap_skip_the_head_while_a_new_head_is_reviewed(self) -> None:
        now = dt.datetime.now(UTC)
        failed = {
            "1": {"head_sha": "a" * 12, "attempts": 1, "last_failed_at": now.isoformat(), "stage": "quorum"},
            "2": {"head_sha": "b" * 12, "attempts": 3, "last_failed_at": (now - dt.timedelta(days=9)).isoformat(), "stage": "required"},
            "3": {"head_sha": "old" * 4, "attempts": 3, "last_failed_at": now.isoformat(), "stage": "required"},
            "4": {"head_sha": "d" * 12, "attempts": 1, "last_failed_at": (now - dt.timedelta(hours=1)).isoformat(), "stage": "quorum"},
        }
        prs = [self._pr(1, "a" * 12), self._pr(2, "b" * 12), self._pr(3, "c" * 12), self._pr(4, "d" * 12)]
        self.assertEqual(self._run_once(failed, prs), [(3, "poll"), (4, "poll")])
        self.assertEqual(self._run_once(failed, prs, force=True), [(1, "poll"), (2, "poll"), (3, "poll"), (4, "poll")])

    def test_a_review_that_raises_counts_toward_the_limit(self) -> None:
        """Clone timeouts, diff fetches and GitHub API errors raise out of review_pr;
        before PR #23's review round they were re-run every cycle forever."""
        prs = [self._pr(5, "e" * 12)]
        for error in (svc.ServiceError("clone timed out"), RuntimeError("boom")):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(self._run_once({}, prs, raises=error), [(5, "poll")])
                entry = self.saved_state["repositories"]["owner/repo"]["failed"]["5"]
                self.assertEqual((entry["head_sha"], entry["attempts"], entry["stage"]), ("e" * 12, 1, "error"))
                # The next cycle is inside the backoff: no review at all.
                self.assertEqual(self._run_once({"5": entry}, prs, raises=error), [])
                capped = {**entry, "attempts": 2, "last_failed_at": (dt.datetime.now(UTC) - dt.timedelta(hours=1)).isoformat()}
                self._run_once({"5": capped}, prs, raises=error)
                self.assertEqual(self.saved_state["repositories"]["owner/repo"]["failed"]["5"]["attempts"], 3)
                self.assertEqual(self._run_once(self.saved_state["repositories"]["owner/repo"]["failed"], prs, raises=error), [])

    def test_a_raise_on_a_head_already_in_the_ledger_is_not_counted(self) -> None:
        reviewed = {"5": {"head_sha": "e" * 12, "post_outcome_unknown": True}}
        self._run_once({}, [self._pr(5, "e" * 12)], raises=svc.ServiceError("POST timed out"), reviewed=reviewed)
        self.assertEqual(self.saved_state["repositories"]["owner/repo"]["failed"], {})

    def test_an_operator_retry_reviews_a_capped_head_the_poll_loop_skips(self) -> None:
        store = FakeStore()
        head = "b" * 12
        previous = store.start_run("owner/repo", 2, head, trigger="poll")
        store.update_run(previous["id"], "failed", stage="required", error="required agent(s) failed: arch")
        store.enqueue_retry("owner/repo", 2, head, "owner@company.example", retry_of=previous["id"])
        failed = {"2": {"head_sha": head, "attempts": 3, "last_failed_at": dt.datetime.now(UTC).isoformat(), "stage": "required"}}
        self.assertEqual(self._run_once(failed, [self._pr(2, head)], store=store), [(2, "retry")])


if __name__ == "__main__":
    unittest.main()
