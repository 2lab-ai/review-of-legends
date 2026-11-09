"""Visual outline stage tests.

The outline is a once-per-PR orientation comment posted *before* the reviewer
agents run. The posted comment carrying ``visual_marker`` is the whole ledger:
present means never run again, absent means the next review may try. Nothing in
this stage may block or unwind the review itself.
"""

from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import service as svc


VISUAL_MARKER = svc.VISUAL_MARKER
OUTLINE = "## 왜 이 변경인가\n한 문장.\n\n## 리뷰어가 알아야 할 것\n- 없음."
POLICY = {"model": "gpt-6-astra[1m]", "effort": "xhigh", "fallback_models": ["grok-4.6"]}


class _FakeClient:
    """Fake GitHub client that records the order of every write it receives."""

    def __init__(self, repo: str = "owner/repo", comments: list[dict] | None = None) -> None:
        self.repo = repo
        self.comments = comments if comments is not None else []
        self.calls: list[tuple] = []
        self.list_comments_calls = 0
        self.list_should_fail = False
        self.upsert_should_fail_markers: set[str] = set()

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

    def list_issue_comments(self, number):
        self.list_comments_calls += 1
        self.calls.append(("list_issue_comments", number))
        if self.list_should_fail:
            raise svc.ServiceError("listing boom")
        return list(self.comments)

    def upsert_issue_comment(self, number, marker, body):
        self.calls.append(("upsert_issue_comment", marker, body))
        if marker in self.upsert_should_fail_markers:
            raise svc.ServiceError("upsert boom")
        return {"id": 998}

    def create_review(self, number, commit_id, body, comments, event="COMMENT"):
        self.calls.append(("create_review", event))
        return {"id": 999}

    def markers(self) -> list[str]:
        return [call[1] for call in self.calls if call[0] == "upsert_issue_comment"]


def _make_config(work_dir: Path, visual: dict | None = None) -> dict:
    config = {
        "agents": [{"id": "fake", "enabled": True, "prompt_path": "../agents/correctness.md"}],
        "agent_runner": {"command": ["true"]},
        "min_successful_agents": 0,
        "max_review_comments": 30,
        "max_diff_chars": 180000,
        "work_dir": str(work_dir),
        "marker": svc.MARKER,
        "summary_marker": svc.SUMMARY_MARKER,
        "visual_marker": VISUAL_MARKER,
        # Keep the call log to the writes under test.
        "progress_comment": {"enabled": False},
        "executive_summary": {"enabled": False},
    }
    if visual is not None:
        config["visual_outline"] = visual
    return config


def _visual_block() -> dict:
    return {"enabled": True, "prompt_path": "../agents/visual-outline.md"}


class _ReviewHarness:
    """review_pr with the clone and the reviewer agents stubbed out.

    ``outline`` is what the stubbed outline agent returns; an exception
    instance is raised instead, which is how the escaping-error paths (a
    missing prompt file, a runner that will not launch) reach ``review_pr``.
    ``patch_outline=False`` leaves the real ``run_visual_outline_agent`` in
    place.
    """

    def __init__(self, test: unittest.TestCase, outline=OUTLINE, patch_outline: bool = True):
        self._test = test
        self._outline = outline
        self._patch_outline = patch_outline
        self.outline_calls = 0

    def _run_visual(self, *args, **kwargs):
        self.outline_calls += 1
        if isinstance(self._outline, BaseException):
            raise self._outline
        meta_sink = kwargs.get("meta_sink")
        if meta_sink is not None and self._outline is not None:
            meta_sink.update({"model": "gpt-6-astra[1m]", "input_tokens": 11, "output_tokens": 22})
        return self._outline

    def run(self, client, config, pr, state, state_path=None, dry_run=False, **review_kwargs):
        def _run_agent(*args, **kwargs):
            # Ordered against the comment writes: the outline must be up before
            # any reviewer agent starts, not merely before the review is posted.
            client.calls.append(("run_agent",))
            return {"agent": "fake", "summary": "ok", "findings": []}

        outline_patch = (
            mock.patch.object(svc, "run_visual_outline_agent", self._run_visual)
            if self._patch_outline
            else contextlib.nullcontext()
        )
        with mock.patch.object(svc, "run_agent", _run_agent), outline_patch, mock.patch.object(
            svc,
            "clone_pr_source",
            lambda repo, pr_number, head_sha, run_dir, **kw: run_dir.mkdir(parents=True, exist_ok=True),
        ):
            return svc.review_pr(
                client,
                config,
                ROOT / "config" / "reviewer.json",
                pr,
                state,
                state_path=state_path,
                dry_run=dry_run,
                **review_kwargs,
            )


class VisualOutlineStageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.pr = {"number": 7, "head": {"sha": "abc1234567"}, "title": "t", "body": ""}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_existing_marker_comment_skips_the_agent(self) -> None:
        client = _FakeClient(comments=[{"id": 1, "body": f"{VISUAL_MARKER}\nold outline"}])
        config = _make_config(self.tmp_path / "work", _visual_block())
        harness = _ReviewHarness(self)

        self.assertTrue(harness.run(client, config, self.pr, {"reviewed": {}}))

        self.assertEqual(harness.outline_calls, 0)
        self.assertNotIn(VISUAL_MARKER, client.markers())
        self.assertIn(("create_review", "COMMENT"), client.calls)

    def test_outline_is_posted_before_the_review(self) -> None:
        client = _FakeClient()
        config = _make_config(self.tmp_path / "work", _visual_block())
        harness = _ReviewHarness(self)

        self.assertTrue(harness.run(client, config, self.pr, {"reviewed": {}}))

        kinds = [call[0] for call in client.calls]
        upsert_index = kinds.index("upsert_issue_comment")
        self.assertLess(upsert_index, kinds.index("run_agent"), client.calls)
        self.assertLess(upsert_index, kinds.index("create_review"), client.calls)
        marker, body = client.calls[upsert_index][1], client.calls[upsert_index][2]
        self.assertEqual(marker, VISUAL_MARKER)
        self.assertTrue(body.startswith(VISUAL_MARKER))
        self.assertIn("## 왜 이 변경인가", body)
        self.assertIn("Review of Legends visual outline", body)
        self.assertIn("gpt-6-astra[1m]", body)
        self.assertIn("PR head: `abc1234`", body)

    def test_agent_failure_posts_no_comment_and_the_review_still_runs(self) -> None:
        client = _FakeClient()
        config = _make_config(self.tmp_path / "work", _visual_block())
        harness = _ReviewHarness(self, outline=None)

        self.assertTrue(harness.run(client, config, self.pr, {"reviewed": {}}))

        self.assertEqual(harness.outline_calls, 1)
        self.assertNotIn(VISUAL_MARKER, client.markers())
        self.assertIn(("create_review", "COMMENT"), client.calls)

    def test_upsert_failure_does_not_block_the_review(self) -> None:
        client = _FakeClient()
        client.upsert_should_fail_markers = {VISUAL_MARKER}
        config = _make_config(self.tmp_path / "work", _visual_block())
        harness = _ReviewHarness(self)

        self.assertTrue(harness.run(client, config, self.pr, {"reviewed": {}}))
        self.assertIn(("create_review", "COMMENT"), client.calls)

    def test_dry_run_prints_the_outline_and_posts_nothing(self) -> None:
        client = _FakeClient()
        config = _make_config(self.tmp_path / "work", _visual_block())
        harness = _ReviewHarness(self)

        with mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
            self.assertTrue(harness.run(client, config, self.pr, {"reviewed": {}}, dry_run=True))
        printed = out.getvalue()

        self.assertIn("VISUAL OUTLINE", printed)
        self.assertIn("## 왜 이 변경인가", printed)
        self.assertNotIn("upsert_issue_comment", [call[0] for call in client.calls])
        self.assertNotIn("create_review", [call[0] for call in client.calls])

    def test_absent_config_block_never_lists_comments(self) -> None:
        client = _FakeClient()
        config = _make_config(self.tmp_path / "work")
        harness = _ReviewHarness(self)

        self.assertTrue(harness.run(client, config, self.pr, {"reviewed": {}}))

        self.assertEqual(client.list_comments_calls, 0)
        self.assertEqual(harness.outline_calls, 0)
        self.assertIn(("create_review", "COMMENT"), client.calls)

    def test_listing_failure_skips_the_stage(self) -> None:
        client = _FakeClient()
        client.list_should_fail = True
        config = _make_config(self.tmp_path / "work", _visual_block())
        harness = _ReviewHarness(self)

        self.assertTrue(harness.run(client, config, self.pr, {"reviewed": {}}))

        self.assertEqual(harness.outline_calls, 0)
        self.assertNotIn(VISUAL_MARKER, client.markers())
        self.assertIn(("create_review", "COMMENT"), client.calls)


class VisualOutlineFailureIsolationTest(unittest.TestCase):
    """Nothing the stage can raise may cost the review that follows it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.pr = {"number": 9, "head": {"sha": "abc1234567"}, "title": "t", "body": ""}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _assert_review_survived(self, client, state, state_path: Path) -> None:
        kinds = [call[0] for call in client.calls]
        self.assertIn("run_agent", kinds)
        self.assertIn(("create_review", "COMMENT"), client.calls)
        self.assertNotIn(VISUAL_MARKER, client.markers())
        self.assertEqual(state["reviewed"]["9"]["head_sha"], "abc1234567")
        on_disk = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["reviewed"]["9"]["head_sha"], "abc1234567")

    def _run_with_raising_agent(self, exc: BaseException) -> None:
        client = _FakeClient()
        config = _make_config(self.tmp_path / "work", _visual_block())
        state = {"reviewed": {}}
        state_path = self.tmp_path / "state.json"

        self.assertTrue(
            _ReviewHarness(self, outline=exc).run(client, config, self.pr, state, state_path=state_path)
        )
        self._assert_review_survived(client, state, state_path)

    def test_unexpected_agent_exception_does_not_abort_the_review(self) -> None:
        self._run_with_raising_agent(RuntimeError("runner exploded"))

    def test_service_error_from_the_agent_does_not_abort_the_review(self) -> None:
        self._run_with_raising_agent(svc.ServiceError("runner argv expects {model_policy_args}"))

    def test_missing_prompt_file_does_not_abort_the_review(self) -> None:
        """The real agent function runs: read_text raises FileNotFoundError."""
        client = _FakeClient()
        config = _make_config(self.tmp_path / "work", _visual_block())
        config["visual_outline"]["prompt_path"] = str(self.tmp_path / "no-such-prompt.md")
        state = {"reviewed": {}}
        state_path = self.tmp_path / "state.json"

        harness = _ReviewHarness(self, patch_outline=False)
        self.assertTrue(harness.run(client, config, self.pr, state, state_path=state_path))
        self._assert_review_survived(client, state, state_path)


class VisualOutlinePolicyTest(unittest.TestCase):
    """The outline runner is one more argv the published policy must cover."""

    ROLLBACK_ARGV = ["codex", "exec", "-m", "gpt-5.5", "--cd", "{run_dir}", "-o", "{response_file}"]

    def test_runner_commands_include_the_outline_runner(self) -> None:
        token_argv = ["r", "{model_policy_args}"]
        policy = {**POLICY, "source": "config"}
        config = {
            "agents": [{"id": "a"}],
            "agent_runner": {"command": token_argv},
            "visual_outline": {"enabled": True},
        }
        self.assertEqual(svc.runner_commands(config), [token_argv, token_argv])
        self.assertTrue(svc.policy_applies_everywhere(config, policy))

        config["visual_outline"]["runner"] = {"command": self.ROLLBACK_ARGV}
        self.assertEqual(svc.runner_commands(config), [token_argv, self.ROLLBACK_ARGV])
        self.assertFalse(svc.policy_applies_everywhere(config, policy))

        config["visual_outline"]["enabled"] = False
        self.assertEqual(svc.runner_commands(config), [token_argv])
        self.assertTrue(svc.policy_applies_everywhere(config, policy))

    def test_absent_block_is_disabled(self) -> None:
        self.assertFalse(svc.visual_outline_enabled({}))
        self.assertFalse(svc.visual_outline_enabled({"visual_outline": {}}))
        self.assertTrue(svc.visual_outline_enabled({"visual_outline": {"enabled": True}}))


WRITER_SCRIPT = """
import json, sys
response = sys.argv[1]
with open(response, "w", encoding="utf-8") as handle:
    handle.write("```markdown\\n## 왜 이 변경인가\\n한 문장.\\n\\n```diff\\n-a\\n+b\\n```\\n```\\n")
with open(response + ".argv.json", "w", encoding="utf-8") as handle:
    json.dump(sys.argv, handle)
"""

FAILING_SCRIPT = """
import sys
sys.exit(1)
"""


class VisualOutlineRunnerTest(unittest.TestCase):
    def _config(self, command: list) -> dict:
        return {
            "visual_outline": {
                "enabled": True,
                "prompt_path": "../agents/visual-outline.md",
                "timeout_seconds": 120,
            },
            "agent_runner": {"command": command, "isolate_home": False},
        }

    def test_runner_output_is_unfenced_and_carries_the_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            command = ["python3", "-c", WRITER_SCRIPT, "{response_file}", "{model_policy_args}"]
            pr = {"number": 3, "head": {"sha": "0123456789"}, "title": "t", "body": ""}

            outline = svc.run_visual_outline_agent(
                self._config(command),
                ROOT / "config",
                run_dir,
                "owner/repo",
                pr,
                [],
                "diff",
                artifact_root=run_dir,
                policy={**POLICY, "source": "config"},
            )

            self.assertIsNotNone(outline)
            self.assertTrue(outline.startswith("## 왜 이 변경인가"))
            self.assertIn("```diff", outline)
            self.assertFalse(outline.startswith("```markdown"))
            artifact_dir = svc.review_artifact_dir(run_dir, run_dir)
            argv = json.loads((artifact_dir / "visual-outline.response.md.argv.json").read_text(encoding="utf-8"))
            self.assertIn("--model", argv)
            self.assertIn("gpt-6-astra[1m]", argv)
            self.assertIn("--fallback-model", argv)
            self.assertIn("grok-4.6", argv)
            self.assertTrue((artifact_dir / "visual-outline.prompt.md").exists())

    def test_non_zero_exit_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            command = ["python3", "-c", FAILING_SCRIPT, "{response_file}", "{model_policy_args}"]
            pr = {"number": 3, "head": {"sha": "0123456789"}, "title": "t", "body": ""}

            outline = svc.run_visual_outline_agent(
                self._config(command),
                ROOT / "config",
                run_dir,
                "owner/repo",
                pr,
                [],
                "diff",
                artifact_root=run_dir,
                policy={**POLICY, "source": "config"},
            )
            self.assertIsNone(outline)


class StripMarkdownFenceTest(unittest.TestCase):
    def test_only_a_whole_wrapper_fence_is_removed(self) -> None:
        inner = "## 제목\n\n```diff\n-a\n+b\n```\n\n끝."
        self.assertEqual(svc.strip_markdown_fence(f"```markdown\n{inner}\n```"), inner)
        self.assertEqual(svc.strip_markdown_fence(f"```\n{inner}\n```"), inner)
        # Not a wrapper: the outline itself opens with a view.
        starts_with_view = "```diff\n-a\n+b\n```"
        self.assertEqual(svc.strip_markdown_fence(starts_with_view), starts_with_view)
        self.assertEqual(svc.strip_markdown_fence(f"  {inner}  "), inner)

    def test_two_sibling_blocks_are_not_a_wrapper(self) -> None:
        """A real ```markdown block followed by a ```diff block only looks wrapped."""
        siblings = "```markdown\nprose\n```\n```diff\n-a\n+b\n```"
        self.assertEqual(svc.strip_markdown_fence(siblings), siblings)

    def test_wrapper_with_several_tagged_views_is_unwrapped(self) -> None:
        inner = "## 개요\n\n```diff\n-a\n+b\n```\n\n```text\ntree\n```"
        self.assertEqual(svc.strip_markdown_fence(f"```markdown\n{inner}\n```"), inner)

    def test_four_backtick_wrapper_is_unwrapped(self) -> None:
        inner = "## 개요\n\n```diff\n-a\n+b\n```"
        self.assertEqual(svc.strip_markdown_fence(f"````markdown\n{inner}\n````"), inner)
        self.assertEqual(svc.strip_markdown_fence(f"````\n{inner}\n````"), inner)

    def test_mismatched_fence_lengths_are_left_alone(self) -> None:
        mismatched = "```markdown\n## 개요\n한 문장.\n````"
        self.assertEqual(svc.strip_markdown_fence(mismatched), mismatched)
        other_way = "````markdown\n## 개요\n한 문장.\n```"
        self.assertEqual(svc.strip_markdown_fence(other_way), other_way)


class RenderVisualOutlinePromptTest(unittest.TestCase):
    def test_prompt_carries_metadata_and_diff_but_no_review_schema(self) -> None:
        pr = {
            "number": 12,
            "title": "add the thing",
            "body": "why the thing",
            "base": {"ref": "main"},
            "head": {"ref": "feat/thing", "sha": "abc1234567"},
        }
        files = [{"filename": "a.py", "status": "modified", "additions": 3, "deletions": 1}]
        prompt = svc.render_visual_outline_prompt(
            "owner/repo", pr, files, "@@ -1 +1 @@\n-a\n+b\n", "OUTLINE INSTRUCTIONS"
        )

        self.assertIn("add the thing", prompt)
        self.assertIn("why the thing", prompt)
        self.assertIn("a.py (modified, +3, -1)", prompt)
        self.assertIn("OUTLINE INSTRUCTIONS", prompt)
        self.assertIn("+b", prompt)
        self.assertNotIn('"findings"', prompt)
        self.assertNotIn("overall_correctness", prompt)


MERMAID_OUTLINE = (
    "## 왜 이 변경인가\n한 문장.\n\n"
    "## 다이어그램\n\n```mermaid\n%%{init: {'theme': 'neutral'}}%%\nflowchart TD\n"
    '  A["요청 수신"] --> B{"헤드가 같은가?"}\n  B -->|예| C["리뷰 재실행"]\n  B -->|아니오| D["409 거부"]\n```\n\n'
    "## 리뷰어가 알아야 할 것\n- 없음.\n\n"
    "## 변경 개요\n\n```diff\n-a\n+b\n```\n한 줄 설명."
)


class VisualOutlinePromptFileTest(unittest.TestCase):
    """The shipped instructions ask for the Greptile-style diagram section."""

    def setUp(self) -> None:
        self.instructions = (ROOT / "agents" / "visual-outline.md").read_text(encoding="utf-8")
        pr = {"number": 1, "title": "t", "body": "", "base": {"ref": "main"}, "head": {"ref": "f", "sha": "abc"}}
        self.prompt = svc.render_visual_outline_prompt("owner/repo", pr, [], "", self.instructions)

    def test_the_four_sections_are_required_in_order(self) -> None:
        self.assertIn("exactly these four", self.prompt)
        self.assertNotIn("exactly these three", self.prompt)
        headings = ["## 왜 이 변경인가", "## 다이어그램", "## 리뷰어가 알아야 할 것", "## 변경 개요"]
        positions = [self.prompt.index(heading) for heading in headings]
        self.assertEqual(positions, sorted(positions), headings)

    def test_the_diagram_rules_are_present(self) -> None:
        diagram = self.prompt[self.prompt.index("## 다이어그램"):self.prompt.index("## 리뷰어가 알아야 할 것")]
        self.assertIn("```mermaid", diagram)
        # The normative sentence, not just the example fences, requires it.
        self.assertIn("Exactly one ```mermaid block, starting with", diagram)
        self.assertIn("%%{init: {'theme': 'neutral'}}%%", diagram)
        self.assertIn("flowchart TD", diagram)
        self.assertIn("sequenceDiagram", diagram)
        self.assertIn("4-12 nodes", diagram)
        self.assertIn("-->|예|", diagram)
        self.assertIn("-->|아니오|", diagram)
        self.assertIn("double quotes", diagram)
        self.assertIn("No HTML, no backticks", diagram)
        self.assertIn("ASCII", diagram)
        self.assertIn("One diagram only", diagram)
        self.assertIn("- 없음.", diagram)
        self.assertIn("never a list of\nfiles", diagram)

    def test_the_rules_are_split_by_diagram_type_with_an_example_each(self) -> None:
        diagram = self.prompt[self.prompt.index("## 다이어그램"):self.prompt.index("## 리뷰어가 알아야 할 것")]
        # flowchart example: decisions and labelled edges, quoted labels.
        flow_example = diagram[diagram.index("flowchart TD"):]
        self.assertIn('A["', flow_example)
        self.assertIn("-->|예|", flow_example)
        # sequenceDiagram gets its own rules and its own tiny example, not the
        # flowchart's `{...}`/`-->|예|` syntax.
        self.assertIn('participant A as "', diagram)
        self.assertIn("A->>B:", diagram)
        self.assertIn("no `;`", diagram)
        self.assertIn("no `#`", diagram)
        seq_example = diagram[diagram.index("participant A as"):]
        self.assertIn("->>", seq_example)

    def test_the_overview_no_longer_invites_a_second_diagram(self) -> None:
        overview = self.prompt[self.prompt.index("## 변경 개요"):]
        self.assertNotIn("sequenceDiagram", overview, "the sequence diagram moved to 다이어그램")
        self.assertIn("do not repeat one here", overview)


class MermaidOutlineSurvivesTest(unittest.TestCase):
    """A mermaid block is a tagged fence like ```diff: the unwrap and the comment keep it byte for byte."""

    def test_the_block_survives_fence_stripping_bare_and_wrapped(self) -> None:
        self.assertEqual(svc.strip_markdown_fence(MERMAID_OUTLINE), MERMAID_OUTLINE)
        self.assertEqual(svc.strip_markdown_fence(f"```markdown\n{MERMAID_OUTLINE}\n```"), MERMAID_OUTLINE)
        self.assertEqual(svc.strip_markdown_fence(f"```\n{MERMAID_OUTLINE}\n```"), MERMAID_OUTLINE)

    def test_the_block_survives_the_comment_body(self) -> None:
        body = svc.build_visual_outline_body({}, {"head": {"sha": "abc1234567"}}, MERMAID_OUTLINE, "v-test")
        self.assertIn(MERMAID_OUTLINE, body)
        self.assertTrue(body.startswith(VISUAL_MARKER))
        self.assertEqual(body.count("```mermaid"), 1)


class ProgressCommentVisualRowTest(unittest.TestCase):
    def test_visual_outline_row_and_tokens_appear(self) -> None:
        entries = {
            "fake": {
                "agent": "fake",
                "focus": "Correctness",
                "status": "done",
                "summary": "ok",
                "model": "gpt-6-astra[1m]",
                "input_tokens": 100,
                "output_tokens": 10,
            }
        }
        body = svc.build_progress_comment_body(
            {},
            {"number": 4},
            "abc1234567",
            entries,
            "Reviewer agents running",
            __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            visual_meta={"model": "grok-4.6", "input_tokens": 5, "output_tokens": 7},
        )

        self.assertIn("visual-outline", body)
        self.assertIn("`grok-4.6`", body)
        self.assertIn("in 105", body)
        self.assertIn("out 17", body)
        self.assertIn("2 agent(s) reported usage", body)


if __name__ == "__main__":
    unittest.main()
