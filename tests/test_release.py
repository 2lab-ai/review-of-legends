"""scripts/release.py: which version tag a deploy gets and what its release says.

Every test builds a throwaway git repository in a temp dir; nothing reads this
checkout's tags or touches the network.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.local_ports import refused_port

RELEASE = ROOT / "scripts" / "release.py"
INSTALLER = ROOT / "scripts" / "install-dashboard.py"


def load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load_script("release", RELEASE)

# The developer's or the runner's own git config (commit and tag signing,
# hooks, log.showSignature) must not reach the throwaway repositories.
HERMETIC_GIT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


class GitRepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "repo"
        self.repo.mkdir()
        self.out = Path(tmp.name) / "out"
        self.out.mkdir()
        self.released = Path(tmp.name) / "released.txt"
        env = mock.patch.dict(os.environ, HERMETIC_GIT)
        env.start()
        self.addCleanup(env.stop)
        self.clock = 0
        self.git("init", "-q")

    def git(self, *args: str, env=None) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t"] + list(args),
            cwd=str(self.repo),
            env=env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit(self, subject: str) -> str:
        """An empty commit one second after the last, so git log's order is fixed."""
        self.clock += 1
        stamp = "2026-10-20T00:%02d:%02d+00:00" % divmod(self.clock, 60)
        env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        self.git("commit", "-q", "--allow-empty", "-m", subject, env=env)
        return self.git("rev-parse", "--short", "HEAD")

    def merge(self, branch: str, subject: str) -> None:
        self.clock += 1
        stamp = "2026-10-20T00:%02d:%02d+00:00" % divmod(self.clock, 60)
        env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        self.git("merge", "-q", "--no-ff", "-m", subject, branch, env=env)

    def info(self, **kwargs):
        return release.version_info(cwd=self.repo, **kwargs)[0]

    def run_script(self, *args: str, cwd=None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(RELEASE)] + list(args),
            cwd=str(cwd or self.repo),
            capture_output=True,
            text=True,
        )

    def released_file(self, *tags: str) -> str:
        """The step's --released-file: one published Release tag per line."""
        self.released.write_text("".join(tag + "\n" for tag in tags), encoding="utf-8")
        return str(self.released)

    def prepare(self, *extra: str, cwd=None) -> subprocess.CompletedProcess:
        return self.run_script(
            "prepare",
            "--out",
            str(self.out),
            "--repository",
            "2lab-ai/review-of-legends",
            "--branch",
            "main",
            *extra,
            cwd=cwd,
        )


class VersionRuleTest(GitRepoTestCase):
    def test_no_version_tag_starts_at_v1_0_0(self) -> None:
        self.commit("first")
        info = self.info()
        self.assertEqual("v1.0.0", info["version"])
        self.assertIsNone(info["previous"])
        self.assertTrue(info["new_tag"])

    def test_bump_adds_one_to_the_patch_and_lists_only_commits_since_the_release(self) -> None:
        self.commit("older")
        self.git("tag", "v1.0.3")
        self.commit("already released")
        self.git("tag", "v1.0.4")
        after_one = self.commit("after one")
        self.git("checkout", "-q", "-b", "side")
        on_side = self.commit("on side")
        self.git("checkout", "-q", "-")
        after_two = self.commit("after two")
        self.merge("side", "Merge branch side")
        info = self.info(released=["v1.0.4", "v1.0.3"])
        self.assertEqual(("v1.0.5", "v1.0.4", True), (info["version"], info["previous"], info["new_tag"]))
        # Newest first; the merge commit itself is left out, the commit it
        # brought in is not, and nothing at or before v1.0.4 is listed.
        self.assertEqual(
            [
                {"sha": after_two, "subject": "after two"},
                {"sha": on_side, "subject": "on side"},
                {"sha": after_one, "subject": "after one"},
            ],
            info["notes"],
        )

    def test_versions_compare_as_numbers_not_as_text(self) -> None:
        self.commit("nine")
        self.git("tag", "v1.0.9")
        self.commit("ten")
        self.git("tag", "v1.0.10")
        self.commit("next")
        info = self.info(released=["v1.0.9", "v1.0.10"])
        # Sorted as text, v1.0.9 is the highest: the next tag would be
        # v1.0.10, which already names another commit, and the notes would
        # start one release too early.
        self.assertEqual(("v1.0.11", "v1.0.10"), (info["version"], info["previous"]))

    def test_milestone_tag_counts_as_patch_zero(self) -> None:
        self.commit("last of 1.0")
        self.git("tag", "v1.0.9")
        self.commit("milestone")
        # Annotated, as a hand-pushed milestone tag may be.
        self.git("tag", "-a", "v1.1", "-m", "1.1")
        after = self.commit("after the milestone")
        info = self.info(released=["v1.1", "v1.0.9"])
        self.assertEqual(("v1.1.1", "v1.1", True), (info["version"], info["previous"], info["new_tag"]))
        self.assertEqual([{"sha": after, "subject": "after the milestone"}], info["notes"])

    def test_tagged_head_keeps_its_highest_version_and_tags_nothing(self) -> None:
        self.commit("six")
        self.git("tag", "v1.0.6")
        seven = self.commit("seven")
        self.git("tag", "v1.0.7")
        # A lower version tag on the same commit does not win.
        self.git("tag", "v1.0.2")
        info = self.info(released=["v1.0.6"])
        self.assertEqual(("v1.0.7", "v1.0.6", False), (info["version"], info["previous"], info["new_tag"]))
        self.assertEqual([{"sha": seven, "subject": "seven"}], info["notes"])

    def test_tagged_head_keeps_a_milestone_tags_own_name(self) -> None:
        # gh release create --verify-tag and git log need the ref that exists:
        # v1.1, not v1.1.0.
        self.commit("1.0")
        self.git("tag", "v1.0.4")
        self.commit("milestone")
        self.git("tag", "v1.1")
        info = self.info(released=["v1.0.4"])
        self.assertEqual(("v1.1", "v1.0.4", False), (info["version"], info["previous"], info["new_tag"]))

    def test_non_version_tags_are_ignored(self) -> None:
        self.commit("first")
        for tag in ("deploy-x", "v1.0.0-rc1", "1.0.0", "v1", "release-v2.0.0", "v2.0.0.1"):
            self.git("tag", tag)
        info = self.info()
        self.assertEqual(("v1.0.0", True), (info["version"], info["new_tag"]))

        self.git("tag", "v1.0.0")
        self.commit("second")
        self.git("tag", "v9.9.9-rc1")
        info = self.info()
        self.assertEqual(("v1.0.1", True), (info["version"], info["new_tag"]))

    def test_first_release_lists_the_last_twenty_commits(self) -> None:
        subjects = ["commit %d" % index for index in range(1, 23)]
        for subject in subjects:
            self.commit(subject)
        notes = self.info()["notes"]
        self.assertEqual(list(reversed(subjects))[:20], [note["subject"] for note in notes])


class PublishedBaselineTest(GitRepoTestCase):
    """previous is the highest published Release below the version, never a bare tag.

    A tag is pushed before its deploy and released only after the deploy
    verified, so a tag whose deploy failed has no Release, and its commits
    must reach the next release's notes.
    """

    def released_failed_new(self):
        """v1.0.1 released; v1.0.2 tagged but its deploy failed; HEAD new."""
        self.commit("released")
        self.git("tag", "v1.0.1")
        failed = self.commit("deploy failed")
        self.git("tag", "v1.0.2")
        new = self.commit("new")
        return failed, new

    def test_a_failed_first_deploy_leaves_no_baseline(self) -> None:
        # (i) v1.0.0 was tagged and its deploy failed; nothing is released.
        first = self.commit("first")
        failed = self.commit("failed first deploy")
        self.git("tag", "v1.0.0")
        new = self.commit("next")
        info, rule = release.version_info(cwd=self.repo, released=[])
        self.assertEqual(("v1.0.1", None, True), (info["version"], info["previous"], info["new_tag"]))
        self.assertEqual(
            [
                {"sha": new, "subject": "next"},
                {"sha": failed, "subject": "failed first deploy"},
                {"sha": first, "subject": "first"},
            ],
            info["notes"],
        )
        self.assertEqual("no published release below v1.0.1; the notes list the last 20 commits", rule)

    def test_an_unreleased_tag_is_not_a_baseline(self) -> None:
        failed, new = self.released_failed_new()
        info, rule = release.version_info(cwd=self.repo, released=["v1.0.1"])
        # The number still counts on from the highest tag.
        self.assertEqual(("v1.0.3", "v1.0.1", True), (info["version"], info["previous"], info["new_tag"]))
        self.assertEqual([{"sha": new, "subject": "new"}, {"sha": failed, "subject": "deploy failed"}], info["notes"])
        self.assertEqual("previous is v1.0.1, the highest published release below v1.0.3", rule)

    def test_a_rerun_of_a_released_version_keeps_its_first_runs_notes(self) -> None:
        # (ii) released v1.0.1, failed v1.0.2, released v1.0.3; v1.0.3 runs again.
        self.released_failed_new()
        first_run, _ = release.version_info(cwd=self.repo, released=["v1.0.1"])
        self.assertEqual(("v1.0.3", "v1.0.1", True), (first_run["version"], first_run["previous"], first_run["new_tag"]))
        self.git("tag", "v1.0.3")
        rerun, _ = release.version_info(cwd=self.repo, released=["v1.0.3", "v1.0.1"])
        # Not v1.0.3 (itself: an empty range) and not v1.0.2 (never released).
        self.assertEqual(("v1.0.3", "v1.0.1", False), (rerun["version"], rerun["previous"], rerun["new_tag"]))
        self.assertEqual(first_run["notes"], rerun["notes"])

    def test_the_highest_published_release_below_wins(self) -> None:
        self.released_failed_new()
        info, _ = release.version_info(cwd=self.repo, released=["v1.0.1", "v1.0.2"])
        self.assertEqual(("v1.0.3", "v1.0.2"), (info["version"], info["previous"]))

    def test_published_names_that_are_not_version_tags_here_are_ignored(self) -> None:
        # (iii) v1.0.0 is a well-formed version below v1.0.3 that names no tag
        # here: taking it would make git log fail on refs/tags/v1.0.0.
        self.released_failed_new()
        self.git("tag", "deploy-x", "HEAD~2")
        junk = ["deploy-x", "v1.0.0", "v9.9.9", "v1.0.1-rc1"]
        info, rule = release.version_info(cwd=self.repo, released=junk + ["v1.0.1"])
        self.assertEqual(("v1.0.3", "v1.0.1"), (info["version"], info["previous"]))
        self.assertEqual(
            "previous is v1.0.1, the highest published release below v1.0.3; "
            "ignored published releases that are not version tags here: deploy-x, v1.0.0, v1.0.1-rc1, v9.9.9",
            rule,
        )
        info, _ = release.version_info(cwd=self.repo, released=junk)
        self.assertIsNone(info["previous"])
        self.assertEqual(["new", "deploy failed", "released"], [note["subject"] for note in info["notes"]])


class PrepareTest(GitRepoTestCase):
    def test_prepare_writes_a_version_file_the_installer_accepts(self) -> None:
        self.commit("released")
        self.git("tag", "v1.0.4")
        after = self.commit("after")
        before = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        result = self.prepare("--released-file", self.released_file("v1.0.4"))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("v1.0.5\n", (self.out / "VERSION").read_text(encoding="utf-8"))
        installer = load_script("install_dashboard", INSTALLER)
        self.assertEqual("v1.0.5", installer.read_version(self.out), "VERSION names the dashboard venv")

        info = json.loads((self.out / "version.json").read_text(encoding="utf-8"))
        commit = self.git("rev-parse", "HEAD")
        built_at = info.pop("built_at")
        self.assertEqual(
            {
                "version": "v1.0.5",
                "previous": "v1.0.4",
                "new_tag": True,
                "commit": commit,
                "commit_short": commit[:7],
                "commit_time": "2026-10-20T00:00:02Z",
                "branch": "main",
                "repository": "2lab-ai/review-of-legends",
                "notes": [{"sha": after, "subject": "after"}],
            },
            info,
        )
        self.assertRegex(built_at, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        built = dt.datetime.strptime(built_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        self.assertLessEqual(before, built)
        self.assertLess(built - before, dt.timedelta(minutes=1))

    def test_commit_time_is_utc_with_z_whatever_the_committer_offset(self) -> None:
        # %cI would give +09:00 here, and its UTC spelling depends on the git
        # version (+00:00 from the runner's Apple Git 2.39).
        self.clock += 1
        stamp = "2026-10-20T09:00:%02d+09:00" % self.clock
        env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        self.git("commit", "-q", "--allow-empty", "-m", "in Seoul", env=env)
        self.assertEqual("2026-10-20T00:00:01Z", self.info()["commit_time"])

    def test_prepare_prints_exactly_the_three_output_lines(self) -> None:
        self.commit("first")
        cases = []
        cases.append(("no tag yet", self.prepare(), "tag=v1.0.0\nprevious=\nnew_tag=true\n"))
        self.git("tag", "v1.0.0")
        self.commit("second")
        released = self.released_file("v1.0.0")
        cases.append(("bump", self.prepare("--released-file", released), "tag=v1.0.1\nprevious=v1.0.0\nnew_tag=true\n"))
        self.git("tag", "v1.0.1")
        cases.append(
            (
                "re-run of a tagged commit",
                self.prepare("--released-file", released),
                "tag=v1.0.1\nprevious=v1.0.0\nnew_tag=false\n",
            )
        )
        for label, result, stdout in cases:
            with self.subTest(label):
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(stdout, result.stdout)
                # Diagnostics go to the job log, not into $GITHUB_OUTPUT.
                self.assertIn("release: v1.0.", result.stderr)

    def test_prepare_reads_the_released_file_and_names_the_rule_on_stderr(self) -> None:
        self.commit("released")
        self.git("tag", "v1.0.1")
        self.commit("deploy failed")
        self.git("tag", "v1.0.2")
        self.commit("new")
        # gh prints one tag per line; blank lines and edge spaces carry nothing.
        self.released.write_text("\n v1.0.1 \n\nnot-a-version\n", encoding="utf-8")
        result = self.prepare("--released-file", str(self.released))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("tag=v1.0.3\nprevious=v1.0.1\nnew_tag=true\n", result.stdout)
        self.assertIn(
            "release: previous is v1.0.1, the highest published release below v1.0.3; "
            "ignored published releases that are not version tags here: not-a-version\n",
            result.stderr,
        )
        info = json.loads((self.out / "version.json").read_text(encoding="utf-8"))
        self.assertEqual(["new", "deploy failed"], [note["subject"] for note in info["notes"]])

        # An empty file: nothing is released yet.
        result = self.prepare("--released-file", self.released_file())
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("tag=v1.0.3\nprevious=\nnew_tag=true\n", result.stdout)
        self.assertIn("release: no published release below v1.0.3; the notes list the last 20 commits\n", result.stderr)

    def test_a_missing_released_file_exits_nonzero_with_nothing_on_stdout(self) -> None:
        self.commit("first")
        result = self.prepare("--released-file", str(self.released))
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual([], sorted(path.name for path in self.out.iterdir()), "nothing half-written")

    def test_a_git_failure_exits_nonzero_with_nothing_on_stdout(self) -> None:
        # A repository with no commit: HEAD does not resolve.
        result = self.prepare()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn("release: git", result.stderr)
        self.assertEqual([], sorted(path.name for path in self.out.iterdir()), "nothing half-written")

    def test_an_unwritable_out_directory_exits_nonzero_with_nothing_on_stdout(self) -> None:
        self.commit("first")
        self.out.rmdir()
        result = self.prepare()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("", result.stdout)


class ReleaseNotesTest(GitRepoTestCase):
    def test_release_notes_since_the_previous_release(self) -> None:
        self.commit("released")
        self.git("tag", "v1.0.4")
        one = self.commit("ci: one")
        two = self.commit("fix: two (#66)")
        self.assertEqual(0, self.prepare("--released-file", self.released_file("v1.0.4")).returncode)
        result = self.run_script("release-notes", str(self.out / "version.json"))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            "Changes since v1.0.4:\n"
            "\n"
            "- %s fix: two (#66)\n"
            "- %s ci: one\n"
            "\n"
            "Full diff: https://github.com/2lab-ai/review-of-legends/compare/v1.0.4...v1.0.5\n" % (two, one),
            result.stdout,
        )

    def test_first_release_notes_say_the_list_is_capped_and_have_no_compare_link(self) -> None:
        first = self.commit("first")
        self.assertEqual(0, self.prepare().returncode)
        result = self.run_script("release-notes", str(self.out / "version.json"))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("First release (at most the last 20 commits):\n\n- %s first\n" % first, result.stdout)

    def test_no_compare_link_without_a_repository(self) -> None:
        info = {"version": "v1.0.5", "previous": "v1.0.4", "repository": None, "notes": [{"sha": "abc1234", "subject": "x"}]}
        self.assertEqual("Changes since v1.0.4:\n\n- abc1234 x\n", release.render_release_notes(info))

    def test_unreadable_version_json_fails(self) -> None:
        result = self.run_script("release-notes", str(self.out / "missing.json"))
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("", result.stdout)


# Two notes, newest first; a +09:00 commit time and a Z build time, as git
# and prepare may write them.
VERSION_INFO = {
    "version": "v1.0.5",
    "previous": "v1.0.4",
    "new_tag": True,
    "commit": "0123456789abcdef0123456789abcdef01234567",
    "commit_short": "0123456",
    "commit_time": "2026-10-20T09:00:02+09:00",
    "built_at": "2026-10-20T00:05:00Z",
    "branch": "main",
    "repository": "2lab-ai/review-of-legends",
    "notes": [{"sha": "abc1234", "subject": "fix: two (#66)"}, {"sha": "def5678", "subject": "ci: one"}],
}
LIMIT = 3000


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class SlackMessageTest(GitRepoTestCase):
    """``release.py slack-message``: the #releases post the announce job sends."""

    def setUp(self) -> None:
        super().setUp()
        self.version_json = self.out / "version.json"

    def slack_message(self, info=None, env=None) -> subprocess.CompletedProcess:
        """The script as "Version this deploy" runs it; ``info`` None reads
        the version.json already in place."""
        if info is not None:
            self.version_json.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(RELEASE), "slack-message", str(self.version_json)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def rendered(self, info=None) -> dict:
        result = self.slack_message(info)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def section_text(self, message: dict) -> str:
        sections = [block for block in message["blocks"] if block["type"] == "section" and "text" in block]
        self.assertEqual(1, len(sections))
        return sections[0]["text"]["text"]

    def test_renders_the_release_in_soma_works_layout(self) -> None:
        # 2026-10-20T09:00:02+09:00 and 2026-10-20T00:05:00Z as Unix times.
        self.assertEqual(
            {
                "text": "🚀 *v1.0.5* 배포 완료  _(v1.0.4 → v1.0.5)_",
                "blocks": [
                    {"type": "header", "text": {"type": "plain_text", "text": "🚀 review-of-legends v1.0.5 배포", "emoji": True}},
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": "*버전*\n`v1.0.5`", "verbatim": True},
                            {"type": "mrkdwn", "text": "*브랜치*\n`main`", "verbatim": True},
                            {"type": "mrkdwn", "text": "*커밋*\n`01234567`", "verbatim": True},
                            {
                                "type": "mrkdwn",
                                "text": "*커밋 시간*\n<!date^1792454402^{date_short_pretty} {time}|2026-10-20T09:00:02+09:00>",
                                "verbatim": True,
                            },
                        ],
                    },
                    {"type": "divider"},
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "*📋 변경 사항* _(v1.0.4 → v1.0.5)_\n\n• abc1234 fix: two (#66)\n• def5678 ci: one",
                            "verbatim": True,
                        },
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "빌드: <!date^1792454700^{date_short_pretty} {time}|2026-10-20T00:05:00Z>"
                                " | 커밋: `0123456789ab`"
                                " | <https://github.com/2lab-ai/review-of-legends/releases/tag/v1.0.5|릴리스 노트>",
                                "verbatim": True,
                            }
                        ],
                    },
                ],
            },
            self.rendered(VERSION_INFO),
        )

    def test_renders_the_version_json_prepare_wrote(self) -> None:
        self.commit("released")
        self.git("tag", "v1.0.4")
        after = self.commit("after")
        self.assertEqual(0, self.prepare("--released-file", self.released_file("v1.0.4")).returncode)
        message = self.rendered()
        commit = self.git("rev-parse", "HEAD")
        self.assertEqual("*커밋*\n`%s`" % commit[:8], message["blocks"][1]["fields"][2]["text"])
        # git's Z, read by 3.9's fromisoformat.
        self.assertEqual(
            "*커밋 시간*\n<!date^1792454402^{date_short_pretty} {time}|2026-10-20T00:00:02Z>",
            message["blocks"][1]["fields"][3]["text"],
        )
        self.assertEqual("*📋 변경 사항* _(v1.0.4 → v1.0.5)_\n\n• %s after" % after, self.section_text(message))

    def test_output_does_not_depend_on_the_stdout_locale(self) -> None:
        # Korean and emoji are escaped, so an ASCII stdout writes the same bytes.
        info = dict(VERSION_INFO, notes=[{"sha": "abc1234", "subject": "fix: 한글 🐛"}])
        utf8 = self.slack_message(info)
        ascii_only = self.slack_message(info, env=dict(os.environ, PYTHONIOENCODING="ascii"))
        for result in (utf8, ascii_only):
            self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(utf8.stdout, ascii_only.stdout)
        self.assertIn("• abc1234 fix: 한글 🐛", self.section_text(json.loads(utf8.stdout)))

    def test_a_first_release_has_no_range(self) -> None:
        message = self.rendered(dict(VERSION_INFO, previous=None))
        self.assertEqual("🚀 *v1.0.5* 배포 완료", message["text"])
        self.assertEqual("*📋 변경 사항*\n\n• abc1234 fix: two (#66)\n• def5678 ci: one", self.section_text(message))
        self.assertNotIn("→", json.dumps(message, ensure_ascii=False))

    def test_without_commits_there_is_no_changes_section(self) -> None:
        # notifyRelease drops the divider and the section when there are no notes.
        message = self.rendered(dict(VERSION_INFO, notes=[]))
        self.assertEqual(["header", "section", "context"], [block["type"] for block in message["blocks"]])

    def test_without_a_repository_there_is_no_release_link(self) -> None:
        footer = self.rendered(dict(VERSION_INFO, repository=None))["blocks"][-1]["elements"][0]["text"]
        self.assertTrue(footer.endswith(" | 커밋: `0123456789ab`"), footer)

    def test_subjects_are_escaped_for_mrkdwn(self) -> None:
        # Unescaped, <!channel> pings everyone and <url|x> becomes a link.
        info = dict(VERSION_INFO, notes=[{"sha": "abc1234", "subject": "fix: <!channel> a & b <http://x|y> c > d"}])
        self.assertEqual(
            "*📋 변경 사항* _(v1.0.4 → v1.0.5)_\n\n• abc1234 fix: &lt;!channel&gt; a &amp; b &lt;http://x|y&gt; c &gt; d",
            self.section_text(self.rendered(info)),
        )

    def test_a_long_commit_list_keeps_the_newest_and_counts_the_rest(self) -> None:
        # Escaped "&" (5 units) and an emoji (2 UTF-16 units) count as sent.
        total = 300
        notes = [
            {"sha": "%07x" % number, "subject": "fix: 🐛 change & number %d" % number} for number in range(total, 0, -1)
        ]
        result = self.slack_message(dict(VERSION_INFO, notes=notes))
        self.assertEqual(0, result.returncode, result.stderr)
        # The announce job refuses an artifact over 64 KiB; the caps here are
        # in UTF-16 units, so check the bytes the job will measure.
        self.assertLess(len(result.stdout.encode("utf-8")), 64 * 1024)
        text = self.section_text(json.loads(result.stdout))
        self.assertLessEqual(utf16_length(text), LIMIT)
        head, _, listing = text.partition("\n\n")
        self.assertEqual("*📋 변경 사항* _(v1.0.4 → v1.0.5)_", head)
        lines = listing.split("\n")
        kept, last = lines[:-1], lines[-1]
        self.assertGreater(len(kept), 0)
        expected = ["• %s %s" % (note["sha"], note["subject"].replace("&", "&amp;")) for note in notes]
        self.assertEqual(expected[: len(kept)], kept, "the newest commits, in order")
        self.assertEqual("… 외 %d개 (릴리스 노트 참고)" % (total - len(kept)), last)
        # As many as fit: one more would not.
        one_more = "\n".join([head, ""] + kept + [expected[len(kept)], "… 외 %d개 (릴리스 노트 참고)" % (total - len(kept) - 1)])
        self.assertGreater(utf16_length(one_more), LIMIT)

    def test_a_list_that_fits_is_not_cut(self) -> None:
        # Exactly the limit, made of the same 2-note listing padded in a subject.
        base = "*📋 변경 사항* _(v1.0.4 → v1.0.5)_\n\n• abc1234 \n• def5678 ci: one"
        subject = "x" * (LIMIT - utf16_length(base))
        info = dict(VERSION_INFO, notes=[{"sha": "abc1234", "subject": subject}, VERSION_INFO["notes"][1]])
        text = self.section_text(self.rendered(info))
        self.assertEqual(LIMIT, utf16_length(text))
        self.assertNotIn("릴리스 노트 참고", text)

    def test_a_header_longer_than_150_is_cut(self) -> None:
        # Slack refuses a header whose plain_text is over 150; only an odd tag
        # gets there. Two of the units are the rocket's.
        around_the_tag = utf16_length("🚀 review-of-legends  배포")
        for length in (150, 151):
            with self.subTest(length=length):
                tag = "v1.0.5-" + "x" * (length - around_the_tag - len("v1.0.5-"))
                full = "🚀 review-of-legends %s 배포" % tag
                self.assertEqual(length, utf16_length(full))
                message = self.rendered(dict(VERSION_INFO, version=tag))
                header = message["blocks"][0]["text"]["text"]
                if length == 150:
                    self.assertEqual(full, header)
                else:
                    # 149 units (the rocket and 147 more characters), then "…".
                    self.assertEqual(full[:148] + "…", header)
                    self.assertEqual(150, utf16_length(header))
                # The fallback text and the 버전 field cut the tag at 40.
                self.assertIn("*%s…*" % tag[:39], message["text"])
                self.assertEqual("*버전*\n`%s…`" % tag[:39], message["blocks"][1]["fields"][0]["text"])

    def test_tags_of_any_length_keep_every_block_within_slacks_limits(self) -> None:
        # VERSION_TAG bounds a tag's shape, not its digits: the changes
        # heading alone could outgrow the section, and the count of the rest
        # would no longer fit.
        previous = "v1.0." + "4" * 3000
        version = "v1.0." + "5" * 3000
        notes = [{"sha": "%07x" % number, "subject": "fix: change %d" % number} for number in range(300, 0, -1)]
        for label, info in (
            ("long previous", dict(VERSION_INFO, previous=previous, notes=notes)),
            ("long previous and version", dict(VERSION_INFO, previous=previous, version=version, notes=notes)),
        ):
            with self.subTest(label):
                message = self.rendered(info)
                section = self.section_text(message)
                self.assertLessEqual(release.slack_length(section), release.SLACK_SECTION_LIMIT)
                head, _, listing = section.partition("\n\n")
                self.assertLessEqual(utf16_length(head), 120)
                kept = listing.split("\n")
                self.assertGreater(len(kept), 1, "commits still fit beside the heading")
                self.assertRegex(kept[-1], r"^… 외 \d+개 \(릴리스 노트 참고\)$")
                self.assertEqual(300, len(kept) - 1 + int(re.search(r"외 (\d+)개", kept[-1]).group(1)))
                # Slack's own limits: header 150, section and context text
                # 3000, a field 2000.
                self.assertLessEqual(utf16_length(message["blocks"][0]["text"]["text"]), 150)
                for field in message["blocks"][1]["fields"]:
                    self.assertLessEqual(utf16_length(field["text"]), 100)
                self.assertLessEqual(utf16_length(message["blocks"][-1]["elements"][0]["text"]), 3000)
                self.assertLessEqual(utf16_length(message["text"]), 200)
        # A link to the Release only for a tag shown whole.
        footer = self.rendered(dict(VERSION_INFO, version=version))["blocks"][-1]["elements"][0]["text"]
        self.assertNotIn("releases/tag", footer)

    def test_bad_input_exits_nonzero_with_nothing_on_stdout(self) -> None:
        cases = [
            ("missing file", None, "release: cannot read"),
            ("not JSON", "{not json", "release: cannot read"),
            ("not an object", "[]", "names no version"),
            ("no version", json.dumps({"notes": []}), "names no version"),
            ("empty version", json.dumps(dict(VERSION_INFO, version="")), "names no version"),
            ("a note without a sha", json.dumps(dict(VERSION_INFO, notes=[{"subject": "x"}])), "is not a version.json prepare wrote"),
            ("notes that are not a list of notes", json.dumps(dict(VERSION_INFO, notes="abc")), "is not a version.json prepare wrote"),
        ]
        for label, content, reason in cases:
            with self.subTest(label):
                if self.version_json.exists():
                    self.version_json.unlink()
                if content is not None:
                    self.version_json.write_text(content, encoding="utf-8")
                result = self.slack_message()
                self.assertNotEqual(0, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertIn(reason, result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_rendering_needs_no_network_tool_token_or_proxy(self) -> None:
        # The bot token lives in the announce job, which runs no repository
        # code: rendering on review-host must work with no curl or gh it could call,
        # every proxy pointing at a port that refuses connections, and a
        # token in the environment that changes nothing.
        expected = self.slack_message(VERSION_INFO)
        self.assertEqual(0, expected.returncode, expected.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sentinel = tmp_path / "a tool ran"
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            for tool in ("curl", "gh", "wget", "nc"):
                (bin_dir / tool).write_text("#!/bin/sh\n/usr/bin/touch '%s'\nexit 1\n" % sentinel, encoding="utf-8")
                (bin_dir / tool).chmod(0o755)
            proxy = "http://127.0.0.1:%d" % refused_port(self)
            env = dict(
                (key, value) for key, value in os.environ.items() if "proxy" not in key.lower() and not key.startswith("SLACK_")
            )
            env.update(
                {
                    "PATH": str(bin_dir),
                    "http_proxy": proxy,
                    "https_proxy": proxy,
                    "HTTP_PROXY": proxy,
                    "HTTPS_PROXY": proxy,
                    "ALL_PROXY": proxy,
                    "SLACK_BOT_TOKEN": "xoxb-not-a-token",
                }
            )
            result = self.slack_message(env=env)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(expected.stdout, result.stdout)
            self.assertEqual("", result.stderr)
            self.assertFalse(sentinel.exists(), "no network tool was run")


class SlackRenderingInProcessTest(unittest.TestCase):
    """The rendering helpers and ``main(["slack-message", ...])`` called in
    this process, for the branches the subprocess runs above do not reach."""

    def test_slack_date_shows_an_unreadable_time_as_it_is_escaped(self) -> None:
        self.assertEqual("soon &lt;&amp;&gt;", release.slack_date("soon <&>"))

    def test_slack_date_reads_a_time_without_an_offset_as_utc(self) -> None:
        # In a zone that is not UTC, so a naive time read as local time would
        # show here as a different epoch.
        try:
            with mock.patch.dict(os.environ, {"TZ": "Asia/Seoul"}):
                time.tzset()
                naive = release.slack_date("2026-10-20T00:05:00")
                utc = release.slack_date("2026-10-20T00:05:00Z")
        finally:
            time.tzset()
        self.assertEqual("<!date^1792454700^{date_short_pretty} {time}|2026-10-20T00:05:00>", naive)
        self.assertEqual(utc.split("|")[0], naive.split("|")[0])

    def test_slack_cut_keeps_what_fits_and_never_half_a_character(self) -> None:
        # 🚀 is two UTF-16 units. "ab🚀" fits in 4 as it is; in "ab🚀c" the
        # "…" leaves 3 units, and the rocket does not fit in the last one.
        self.assertEqual("ab🚀", release.slack_cut("ab🚀", 4))
        self.assertEqual("ab…", release.slack_cut("ab🚀c", 4))
        self.assertEqual("a🚀…", release.slack_cut("a🚀bc", 4))

    def test_no_text_in_the_post_can_mention_the_channel(self) -> None:
        # Escaping stops <!channel>; verbatim stops Slack's own reading of a
        # written @channel, @here, @everyone or #channel in every mrkdwn text.
        subject = "release @channel @here @everyone <!channel> #general"
        message = release.slack_message(dict(VERSION_INFO, notes=[{"sha": "abc1234", "subject": subject}]))
        texts = []

        def walk(node) -> None:
            if isinstance(node, dict):
                if node.get("type") == "mrkdwn":
                    texts.append(node)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(message["blocks"])
        # The four fields, the changes section and the footer.
        self.assertEqual(6, len(texts))
        for text in texts:
            self.assertIs(True, text.get("verbatim"), text)
        self.assertIn(
            "• abc1234 release @channel @here @everyone &lt;!channel&gt; #general", message["blocks"][3]["text"]["text"]
        )

    def test_changes_text_is_none_without_notes(self) -> None:
        for notes in (None, []):
            self.assertIsNone(release.changes_text({"version": "v1.0.5", "previous": "v1.0.4", "notes": notes}))

    def test_changes_text_keeps_the_newest_lines_and_counts_the_rest(self) -> None:
        notes = [{"sha": "%07x" % number, "subject": "x" * 100} for number in range(60, 0, -1)]
        text = release.changes_text({"version": "v1.0.5", "previous": "v1.0.4", "notes": notes})
        self.assertLessEqual(release.slack_length(text), release.SLACK_SECTION_LIMIT)
        lines = text.split("\n")
        kept = lines[2:-1]
        self.assertGreater(len(kept), 0)
        self.assertEqual(["• %07x %s" % (number, "x" * 100) for number in range(60, 60 - len(kept), -1)], kept)
        self.assertEqual("… 외 %d개 (릴리스 노트 참고)" % (60 - len(kept)), lines[-1])

    def slack_message_main(self, content):
        """main(["slack-message", path]) on ``content`` (None: no file):
        (exit code, stdout, stderr)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "version.json"
        if content is not None:
            path.write_text(content, encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = release.main(["slack-message", str(path)])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_main_slack_message_prints_one_json_object(self) -> None:
        code, stdout, stderr = self.slack_message_main(json.dumps(VERSION_INFO))
        self.assertEqual((0, ""), (code, stderr))
        self.assertEqual(1, stdout.count("\n"))
        self.assertEqual(release.slack_message(VERSION_INFO), json.loads(stdout))

    def test_main_slack_message_fails_bad_input_with_nothing_on_stdout(self) -> None:
        for label, content in (
            ("missing file", None),
            ("not JSON", "{not json"),
            ("no version", "[]"),
            ("a note without a sha", json.dumps(dict(VERSION_INFO, notes=[{"subject": "x"}]))),
        ):
            with self.subTest(label):
                code, stdout, stderr = self.slack_message_main(content)
                self.assertNotEqual(0, code)
                self.assertEqual("", stdout)
                self.assertTrue(stderr.startswith("release: "), stderr)


if __name__ == "__main__":
    unittest.main()
