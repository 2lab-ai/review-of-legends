"""Review repositories added on the dashboard (store key ``repositories``).

The daemon reviews config.repositories plus the dashboard-added ones, read
every cycle. An added entry must be a well-formed ``owner/name`` whose owner the
config already reviews; config wins a case-insensitive duplicate; a store that
cannot be read means "config only", never a stopped cycle. A newly added
repository's first cycle records a baseline instead of reviewing the pull
requests already open, and an operator retry for it is accepted.
"""

from __future__ import annotations

import argparse
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
from tests.test_dashboard_integration import FakeStore

CONFIG_PATH = ROOT / "config" / "reviewer.json"
OWNERS = ["acme-corp"]


def _added(*repos: str) -> dict:
    return {"added": [{"repo": repo, "added_by": "owner@company.example", "added_at": "2026-10-13T00:00:00+00:00"} for repo in repos]}


class RepositoryNameTest(unittest.TestCase):
    def test_accepts_owner_name_and_compares_the_owner_case_insensitively(self) -> None:
        self.assertEqual(svc.validate_repository_name("acme-corp/MiniMoon", OWNERS), "acme-corp/MiniMoon")
        self.assertEqual(svc.validate_repository_name("  Acme-CORP/.github\n", OWNERS), "Acme-CORP/.github")
        self.assertEqual(svc.validate_repository_name("acme-corp/a_b.c-d", OWNERS), "acme-corp/a_b.c-d")

    def test_rejects_malformed_names_including_dot_segments(self) -> None:
        for bad in (None, 7, "", "MiniMoon", "acme-corp/", "/MiniMoon", "a/b/c", "-owner/x", "acme-corp/a b",
                    "acme-corp/.", "acme-corp/..", "acme-corp/x\ny", "o" * 40 + "/x", "acme-corp/" + "x" * 101):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as caught:
                svc.validate_repository_name(bad, OWNERS)
            self.assertIn("owner/name", str(caught.exception))

    def test_rejects_an_owner_the_config_does_not_review(self) -> None:
        with self.assertRaises(ValueError) as caught:
            svc.validate_repository_name("octocat/hello-world", OWNERS)
        self.assertEqual(str(caught.exception), "octocat is not an owner this service reviews (allowed: acme-corp)")

    def test_owners_come_from_the_config_repositories(self) -> None:
        config = {"repositories": ["acme-corp/Gamma", "Acme-Corp/Pluto", "2lab-ai/llmux"]}
        self.assertEqual(svc.repository_owners(config), ["acme-corp", "2lab-ai"])


class MergeRepositoriesTest(unittest.TestCase):
    CONFIG = {
        "repositories": ["acme-corp/Gamma", "acme-corp/review-of-legends"],
        "agents": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
    }

    def test_config_first_then_added_with_config_winning_a_duplicate(self) -> None:
        stored = _added("acme-corp/MiniMoon", "acme-corp/gamma", "ACME-CORP/minimoon", "acme-corp/PlutoBo")
        rows = svc.merge_repositories(self.CONFIG, stored)
        self.assertEqual(
            [(row["repo"], row["source"]) for row in rows],
            [
                ("acme-corp/Gamma", "config"),
                ("acme-corp/review-of-legends", "config"),
                ("acme-corp/MiniMoon", "dashboard"),
                ("acme-corp/PlutoBo", "dashboard"),
            ],
        )
        self.assertEqual((rows[2]["added_by"], rows[2]["added_at"]), ("owner@company.example", "2026-10-13T00:00:00+00:00"))
        self.assertEqual((rows[0]["added_by"], rows[0]["added_at"]), (None, None))

    def test_malformed_entries_are_logged_and_ignored(self) -> None:
        stored = {"added": ["acme-corp/Raw", {"repo": "octocat/x"}, {"repo": "acme-corp/.."}, {"repo": "acme-corp/Ok", "added_by": 3}]}
        with mock.patch.object(svc, "log") as logged:
            rows = svc.merge_repositories(self.CONFIG, stored)
        self.assertEqual([row["repo"] for row in rows][2:], ["acme-corp/Ok"])
        self.assertIsNone(rows[2]["added_by"], "a non-string added_by is dropped, the repository kept")
        messages = [call.args[0] for call in logged.call_args_list]
        self.assertEqual(len(messages), 3, messages)
        self.assertTrue(all(message.startswith("WARNING ignoring a dashboard repository entry") for message in messages))

    def test_a_setting_without_an_added_list_means_config_only(self) -> None:
        for stored in ({}, {"added": "acme-corp/MiniMoon"}, ["acme-corp/MiniMoon"]):
            with self.subTest(stored=stored), mock.patch.object(svc, "log") as logged:
                self.assertEqual(len(svc.merge_repositories(self.CONFIG, stored)), 2)
                self.assertIn("has no 'added' list", logged.call_args.args[0])
        self.assertEqual(len(svc.merge_repositories(self.CONFIG, None)), 2)

    def test_review_repositories_reads_the_store_best_effort(self) -> None:
        store = FakeStore()
        self.assertEqual(svc.review_repositories(self.CONFIG, None), self.CONFIG["repositories"])
        self.assertEqual(svc.review_repositories(self.CONFIG, store), self.CONFIG["repositories"])
        store.settings["repositories"] = _added("acme-corp/MiniMoon")
        self.assertEqual(svc.review_repositories(self.CONFIG, store), self.CONFIG["repositories"] + ["acme-corp/MiniMoon"])
        store.fail_settings = True
        with mock.patch.object(svc, "log") as logged:
            self.assertEqual(svc.review_repositories(self.CONFIG, store), self.CONFIG["repositories"])
        self.assertIn("reviewing the config repositories only", logged.call_args.args[0])


class AddedRepositoryQuorumTest(unittest.TestCase):
    """An added repository gets the default agents only, so it must pass the same
    quorum check as a config repository: at load time the config cannot see it."""

    def _load(self, tmp: Path, agents: list, switches: dict, **extra) -> dict:
        repositories = ["acme-corp/Gamma", "acme-corp/Pluto"]
        payload = {
            "repositories": repositories,
            "agents": [{"prompt_path": "x", **agent} for agent in agents],
            "repository_agents": {repo: dict(switches) for repo in repositories},
            **extra,
        }
        path = tmp / "reviewer.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return svc.load_config(path)

    def test_default_agents_below_the_floor_mark_and_skip_an_added_repository(self) -> None:
        # Passes load_config: every config repository switches the opt-in c on.
        with tempfile.TemporaryDirectory() as tmp:
            config = self._load(Path(tmp), [{"id": "a"}, {"id": "b"}, {"id": "c", "opt_in": True}], {"c": True})
        store = FakeStore()
        store.settings["repositories"] = _added("acme-corp/MiniMoon")
        row = svc.merge_repositories(config, store.settings["repositories"])[-1]
        self.assertEqual((row["repo"], row["source"]), ("acme-corp/MiniMoon", "dashboard"))
        self.assertEqual(row["problem"], "at least 3 agents must review acme-corp/MiniMoon; check opt_in and repository_agents")
        with mock.patch.object(svc, "log") as logged:
            self.assertEqual(svc.review_repositories(config, store), ["acme-corp/Gamma", "acme-corp/Pluto"])
        self.assertEqual(logged.call_args.args[0], "WARNING not reviewing dashboard repository acme-corp/MiniMoon: "
                                                   "at least 3 agents must review acme-corp/MiniMoon; check opt_in and repository_agents")

    def test_an_explicit_minimum_above_the_default_set_is_the_same_problem(self) -> None:
        agents = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d", "opt_in": True}]
        with tempfile.TemporaryDirectory() as tmp:
            config = self._load(Path(tmp), agents, {"d": True}, min_successful_agents=4)
        self.assertEqual(svc.repository_quorum_problem(config, "acme-corp/MiniMoon"),
                         "min_successful_agents=4 exceeds the 3 agents selected for acme-corp/MiniMoon")
        self.assertEqual(svc.repository_quorum_problem(config, "acme-corp/Gamma"), "")
        with mock.patch.object(svc, "log"):
            self.assertNotIn("acme-corp/MiniMoon", svc.review_repositories(config, _StaticStore(_added("acme-corp/MiniMoon"))))

    def test_the_config_check_keeps_its_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(svc.ServiceError) as caught:
            self._load(Path(tmp), [{"id": "a"}, {"id": "b"}, {"id": "c", "opt_in": True}], {})
        self.assertRegex(str(caught.exception),
                         r"^at least 3 agents must review acme-corp/(Gamma|Pluto); check opt_in and repository_agents$")


class _StaticStore:
    def __init__(self, value: dict) -> None:
        self.value = value

    def get_setting(self, key: str) -> dict:
        return {"key": key, "value": self.value, "updated_at": "t", "updated_by": "z"}


class CycleWithAddedRepositoryTest(unittest.TestCase):
    """``_run_once_locked`` polls, baselines and retries a dashboard-added repository."""

    HEAD = "abc123456789"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state_path = self.tmp / "state.json"
        # owner/repo was reviewed before; owner/added has never been seen.
        self.state_path.write_text(json.dumps({"repositories": {"owner/repo": {"reviewed": {}, "baseline_initialized": True}}}), encoding="utf-8")
        self.store = FakeStore()
        self.store.settings["repositories"] = _added("owner/added")

    @staticmethod
    def _pr(number: int, head: str) -> dict:
        return {"number": number, "state": "open", "head": {"sha": head}, "title": "t", "html_url": "u", "body": ""}

    def _run_once(self, open_prs: dict, *, store=None, repo=None, dry_run=False, lookup=None, overrides=None):
        calls: list = []
        polled: list = []
        lookup = lookup or {}
        config = {
            "repositories": ["owner/repo"],
            "agents": [{"id": i, "prompt_path": "x"} for i in ("a", "b", "c")],
            "agent_runner": {"command": ["true"]},
            "work_dir": str(self.tmp / "work"),
            "state_path": str(self.state_path),
            "review_existing_open_prs_on_first_run": False,
            **(overrides or {}),
        }

        class Client:
            def __init__(self, repository, gh_bin="gh"):
                self.repo = repository
                polled.append(repository)

            def list_open_prs(self):
                return [dict(pr) for pr in open_prs.get(self.repo, [])]

            def get_pr(self, number):
                return dict(lookup[(self.repo, number)])

        def fake_review_pr(client, config, config_path, pr, state, **kwargs):
            recorder = kwargs.get("recorder")
            calls.append((client.repo, int(pr["number"]), "retry" if recorder is not None and recorder.strict else "poll"))
            return False

        with mock.patch.object(svc, "GitHubClient", Client), \
             mock.patch.object(svc, "load_config", return_value=config), \
             mock.patch.object(svc, "expand_path", side_effect=lambda v, base=None: Path(str(v))), \
             mock.patch.object(dx, "open_store", return_value=self.store if store is None else store), \
             mock.patch.object(svc, "review_pr", fake_review_pr):
            svc.run_once(argparse.Namespace(config=str(CONFIG_PATH), repo=repo, pr=None, dry_run=dry_run, force=False))
        return calls, polled

    def _saved(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))["repositories"]

    def test_first_cycle_baselines_the_added_repository_and_the_next_one_reviews_it(self) -> None:
        open_prs = {"owner/repo": [self._pr(1, "r" * 12)], "owner/added": [self._pr(5, self.HEAD), self._pr(6, "f" * 12)]}
        calls, polled = self._run_once(open_prs)
        self.assertEqual(polled, ["owner/repo", "owner/added"])
        self.assertEqual(calls, [("owner/repo", 1, "poll")], "pull requests open when the repository was added are not reviewed")
        bucket = self._saved()["owner/added"]
        self.assertTrue(bucket["baseline_initialized"])
        self.assertEqual(sorted(bucket["reviewed"]), ["5", "6"])
        self.assertTrue(bucket["reviewed"]["5"]["initial_baseline_without_review"])
        self.assertEqual(bucket["reviewed"]["5"]["head_sha"], self.HEAD)

        calls, _ = self._run_once(open_prs)
        self.assertEqual(calls, [("owner/repo", 1, "poll"), ("owner/added", 5, "poll"), ("owner/added", 6, "poll")])

    def test_an_added_repository_too_few_agents_would_review_is_not_polled(self) -> None:
        overrides = {
            "agents": [{"id": "a", "prompt_path": "x"}, {"id": "b", "prompt_path": "x"}, {"id": "c", "opt_in": True, "prompt_path": "x"}],
            "repository_agents": {"owner/repo": {"c": True}},
        }
        calls, polled = self._run_once({"owner/repo": [self._pr(1, "r" * 12)], "owner/added": [self._pr(5, self.HEAD)]}, overrides=overrides)
        self.assertEqual(polled, ["owner/repo"])
        self.assertEqual(calls, [("owner/repo", 1, "poll")])
        self.assertNotIn("owner/added", self._saved())

    def test_an_added_repository_under_a_new_spelling_resumes_its_bucket(self) -> None:
        # Reviewed as owner/added, removed, then added back as GitHub now spells it.
        old_entry = {"head_sha": self.HEAD, "reviewed_at": "2026-10-01T00:00:00+00:00", "line_comments": 0}
        self.state_path.write_text(json.dumps({"repositories": {
            "owner/repo": {"reviewed": {}, "baseline_initialized": True},
            "owner/added": {"reviewed": {"5": old_entry}, "baseline_initialized": True},
        }}), encoding="utf-8")
        self.store.settings["repositories"] = _added("owner/Added")
        # #5 was pushed and #6 opened while it was off the list.
        open_prs = {"owner/Added": [self._pr(5, "n" * 12), self._pr(6, "f" * 12)]}
        calls, polled = self._run_once(open_prs)
        self.assertEqual(polled, ["owner/repo", "owner/Added"])
        self.assertEqual(calls, [("owner/Added", 5, "poll"), ("owner/Added", 6, "poll")],
                         "resumed from the old ledger: reviewed, never baselined unreviewed")

        # The bucket moves to the polled spelling (review_pr then saves it under that key).
        state = {"repositories": {"owner/added": {"reviewed": {"5": old_entry}, "baseline_initialized": True}}}
        bucket = svc.repo_state(state, "owner/Added")
        self.assertEqual(list(state["repositories"]), ["owner/Added"])
        self.assertEqual(bucket["reviewed"], {"5": old_entry})
        # An exact bucket wins; nothing moves.
        both = {"repositories": {"owner/added": {"reviewed": {}}, "owner/Added": {"reviewed": {"6": old_entry}}}}
        self.assertEqual(svc.repo_state(both, "owner/Added")["reviewed"], {"6": old_entry})
        self.assertEqual(sorted(both["repositories"]), ["owner/Added", "owner/added"])

    def test_repo_flag_and_retries_match_the_listed_spelling_case_insensitively(self) -> None:
        _, polled = self._run_once({"owner/added": [self._pr(5, self.HEAD)]}, repo="OWNER/ADDED")
        self.assertEqual(polled, ["owner/added"])
        # An attempt recorded under another spelling is retried under the polled one.
        previous = self.store.start_run("OWNER/Added", 5, self.HEAD, trigger="poll")
        self.store.update_run(previous["id"], "failed", stage="quorum", error="quorum not reached")
        request = self.store.enqueue_retry("OWNER/Added", 5, self.HEAD, "owner@company.example", retry_of=previous["id"])
        calls, _ = self._run_once({}, lookup={("owner/added", 5): self._pr(5, self.HEAD)})
        self.assertEqual(calls, [("owner/added", 5, "retry")])
        self.assertNotIn("not configured", str(self.store.requests[request["id"]].get("error") or ""))

    def test_an_unreadable_store_polls_the_config_repositories_only(self) -> None:
        self.store.fail_settings = True
        calls, polled = self._run_once({"owner/repo": [self._pr(1, "r" * 12)], "owner/added": [self._pr(5, self.HEAD)]})
        self.assertEqual(polled, ["owner/repo"])
        self.assertEqual(calls, [("owner/repo", 1, "poll")])
        self.assertNotIn("owner/added", self._saved(), "a store outage creates no state for it")

    def test_repo_flag_accepts_an_added_repository_but_a_dry_run_reads_config_only(self) -> None:
        calls, polled = self._run_once({"owner/added": [self._pr(5, self.HEAD)]}, repo="owner/added")
        self.assertEqual(polled, ["owner/added"])
        with self.assertRaises(svc.ServiceError) as caught:
            self._run_once({}, repo="owner/added", dry_run=True)
        self.assertIn("unknown configured repository: owner/added", str(caught.exception))

    def test_an_operator_retry_for_an_added_repository_is_reviewed(self) -> None:
        previous = self.store.start_run("owner/added", 5, self.HEAD, trigger="poll")
        self.store.update_run(previous["id"], "failed", stage="quorum", error="quorum not reached")
        request = self.store.enqueue_retry("owner/added", 5, self.HEAD, "owner@company.example", retry_of=previous["id"])
        lookup = {("owner/added", 5): self._pr(5, self.HEAD)}
        calls, _ = self._run_once({}, lookup=lookup)
        self.assertEqual(calls, [("owner/added", 5, "retry")])
        self.assertNotIn("not configured", str(self.store.requests[request["id"]].get("error") or ""))

        # Control: the same request is refused once the repository is removed again.
        del self.store.settings["repositories"]
        again = self.store.enqueue_retry("owner/added", 6, "f" * 12, "owner@company.example")
        calls, _ = self._run_once({}, lookup=lookup)
        self.assertEqual(calls, [])
        self.assertEqual(self.store.retry_status(again["id"]), "failed")
        self.assertIn("repository owner/added is not configured", self.store.requests[again["id"]]["error"])


if __name__ == "__main__":
    unittest.main()
