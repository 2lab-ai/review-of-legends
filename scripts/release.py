#!/usr/bin/env python3
"""Release versions for deploy.yml: one git tag per deployed commit of main.

  prepare        in the repository it runs in: pick this commit's version tag,
                 write VERSION (the tag alone) and version.json (the commit,
                 build time and the commits since the previous release) into
                 --out, and print tag=, previous= and new_tag= for
                 $GITHUB_OUTPUT. The tag job creates the tag through the
                 GitHub API when new_tag=true.
  release-notes  print the GitHub Release body for a version.json.
  slack-message  print the #releases post for a version.json: one JSON
                 object with Slack's ``text`` and ``blocks``, in soma-work's
                 deploy layout. The announce job adds the channel and posts
                 it; nothing here reads a token or touches the network.

The rule: version tags are ``vMAJOR.MINOR[.PATCH]`` (a ``v1.1`` milestone tag
counts as 1.1.0) and every other tag is ignored. A commit that already carries
a version tag keeps the highest of them, so re-running a deploy keeps its
number and its dashboard venv. Any other commit gets the highest version tag in
the repository with the patch number plus one, or v1.0.0 when there is none.
The next minor or major starts from a tag such as v1.1.0 pushed by hand on the
commit to deploy.

The notes start at ``previous``: the highest published GitHub Release
(``--released-file``, one tag per line) that is a version tag of this
repository below this version. A bare tag never counts: a tag is pushed before
its deploy but released only after the deploy verified, so a version whose
deploy failed has a tag and no Release, and its commits stay in the next
release's notes. With no such Release ``previous`` is None and the notes are
the last 20 non-merge commits: a first release lists at most 20.

Stdlib-only and runnable by macOS ``/usr/bin/python3`` (3.9), like the other
scripts here: deploy.yml runs it there. VERSION names the dashboard venv
directory (install-dashboard.read_version), so it holds the tag and nothing
else.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

VERSION_TAG = re.compile(r"^v(\d+)\.(\d+)(?:\.(\d+))?$")
FIRST_VERSION = (1, 0, 0)
# Without a previous release there is no range to list, and the whole history
# of the first release would bury the commits that matter.
NOTES_WITHOUT_PREVIOUS = 20

# The name in the Slack post's header, beside soma-work's own posts.
PROJECT_NAME = "review-of-legends"
# Slack refuses a block whose text is longer than its limit (invalid_blocks),
# and with it the whole post: a header's plain_text and a section's text.
SLACK_HEADER_LIMIT = 150
SLACK_SECTION_LIMIT = 3000
# A tag where the post repeats it beside other text (the fallback text, the
# 버전 field, the changes heading). VERSION_TAG puts no bound on its digits;
# real tags are far shorter, and this keeps that text within Slack's limits.
SLACK_VERSION_LIMIT = 40

Version = Tuple[int, int, int]


class GitError(Exception):
    pass


def git(args: Sequence[str], cwd: Optional[Path] = None) -> str:
    try:
        proc = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True)
    except OSError as exc:
        raise GitError("cannot run git: %s" % exc) from exc
    if proc.returncode != 0:
        raise GitError("git %s failed (exit %d): %s" % (" ".join(args), proc.returncode, proc.stderr.strip()))
    return proc.stdout


def parse_version(tag: str) -> Optional[Version]:
    match = VERSION_TAG.match(tag)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def version_tags(tags: Sequence[str]) -> List[Tuple[Version, str]]:
    """(value, tag name) for every version tag, lowest first.

    The name is kept rather than re-spelled from the value: ``v1.1`` is the
    ref git log and ``gh release create --verify-tag`` must find, not v1.1.0.
    Ties on the value (v1.1 and v1.1.0) fall back to the name so the pick is
    deterministic.
    """
    parsed = []
    for tag in tags:
        value = parse_version(tag)
        if value is not None:
            parsed.append((value, tag))
    return sorted(parsed)


def choose_previous(
    known: Sequence[Tuple[Version, str]], value: Version, version: str, released: Sequence[str]
) -> Tuple[Optional[str], str]:
    """The tag the notes start from, and which rule picked it (for the job log).

    The highest of ``released`` (the published Releases) that names a version
    tag of this repository below ``version``; None when there is none.
    Equal does not count, so a re-run of a released version starts where its
    first run did.
    """
    tags = dict((name, tag_value) for tag_value, name in known)
    published = sorted(set(name for name in released if name))
    candidates = sorted((tags[name], name) for name in published if name in tags and tags[name] < value)
    ignored = [name for name in published if name not in tags]
    if candidates:
        previous = candidates[-1][1]
        rule = "previous is %s, the highest published release below %s" % (previous, version)
    else:
        previous = None
        rule = "no published release below %s; the notes list the last %d commits" % (
            version,
            NOTES_WITHOUT_PREVIOUS,
        )
    if ignored:
        rule += "; ignored published releases that are not version tags here: %s" % ", ".join(ignored)
    return previous, rule


def choose_version(
    all_tags: Sequence[str], head_tags: Sequence[str], released: Sequence[str] = ()
) -> Tuple[str, Optional[str], bool, str]:
    """(version, previous, new_tag, how previous was picked) for HEAD.

    ``released`` moves only where the notes start, never the version number.
    """
    known = version_tags(all_tags)
    at_head = version_tags(head_tags)
    if at_head:
        value, version = at_head[-1]
        new_tag = False
    elif known:
        major, minor, patch = known[-1][0]
        value = (major, minor, patch + 1)
        version = "v%d.%d.%d" % value
        new_tag = True
    else:
        value = FIRST_VERSION
        version = "v%d.%d.%d" % value
        new_tag = True
    previous, rule = choose_previous(known, value, version, released)
    return version, previous, new_tag, rule


def commit_notes(previous: Optional[str], cwd: Optional[Path] = None) -> List[Dict[str, str]]:
    """Non-merge commits since ``previous``, newest first: what this release adds."""
    args = ["log", "--no-merges", "--format=%h%x00%s"]
    if previous is None:
        args += ["-n", str(NOTES_WITHOUT_PREVIOUS), "HEAD"]
    else:
        # refs/tags/ and the trailing "--": a branch or a file named like the
        # tag must not change the range.
        args.append("refs/tags/%s..HEAD" % previous)
    args.append("--")
    notes = []
    for line in git(args, cwd).splitlines():
        sha, _, subject = line.partition("\x00")
        notes.append({"sha": sha, "subject": subject})
    return notes


def version_info(
    cwd: Optional[Path] = None,
    repository: Optional[str] = None,
    branch: Optional[str] = None,
    released: Sequence[str] = (),
    now: Optional[dt.datetime] = None,
) -> Tuple[Dict[str, object], str]:
    """Everything version.json records, asked of git before anything is
    written, and how ``previous`` was picked."""
    commit = git(["rev-parse", "--verify", "HEAD^{commit}"], cwd).strip()
    # From the epoch, not %cI: how %cI spells UTC depends on the git version
    # (the runner's Apple Git 2.39 writes +00:00), and it keeps the
    # committer's own offset. version.json says Z, like built_at.
    committed = int(git(["log", "-1", "--format=%ct", "HEAD"], cwd).strip())
    all_tags = git(["tag", "--list"], cwd).split()
    head_tags = git(["tag", "--points-at", "HEAD"], cwd).split()
    version, previous, new_tag, rule = choose_version(all_tags, head_tags, released)
    built_at = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    info = {
        "version": version,
        "previous": previous,
        "new_tag": new_tag,
        "commit": commit,
        "commit_short": commit[:7],
        "commit_time": dt.datetime.fromtimestamp(committed, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "built_at": built_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "branch": branch,
        "repository": repository,
        "notes": commit_notes(previous, cwd),
    }
    return info, rule


def render_release_notes(info: Dict[str, object]) -> str:
    """The GitHub Release body. Blank lines between the parts: in GitHub's
    markdown a line right after a list item continues that item."""
    previous = info.get("previous")
    if previous:
        header = "Changes since %s:" % previous
    else:
        header = "First release (at most the last %d commits):" % NOTES_WITHOUT_PREVIOUS
    lines = [header, ""]
    for note in info.get("notes") or []:
        lines.append("- %s %s" % (note["sha"], note["subject"]))
    repository = info.get("repository")
    if previous and repository:
        lines += ["", "Full diff: https://github.com/%s/compare/%s...%s" % (repository, previous, info["version"])]
    return "\n".join(lines).rstrip("\n") + "\n"


def mrkdwn_escape(text: str) -> str:
    """Text for Slack mrkdwn: a commit subject such as ``<!channel>`` would
    otherwise ping the channel or turn into a link, and ``&`` starts an
    entity."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_length(text: str) -> int:
    """Length in UTF-16 code units, where an emoji counts two: a text this
    measures within a limit is within it whether Slack counts those or code
    points."""
    return len(text.encode("utf-16-le")) // 2


def slack_cut(text: str, limit: int) -> str:
    """``text`` when it fits in ``limit`` (as slack_length counts), otherwise
    as much of it as fits followed by ``…``, never half a character."""
    if slack_length(text) <= limit:
        return text
    room = max(limit - slack_length("…"), 0)
    # A surrogate pair cut in two leaves half of it at the end, which the
    # decoder drops.
    return text.encode("utf-16-le")[: room * 2].decode("utf-16-le", "ignore") + "…"


def slack_date(iso: str) -> str:
    """A time Slack shows in the reader's own zone, with ``iso`` as the
    fallback text. Python 3.9's fromisoformat does not read the ``Z`` git
    and prepare write for UTC; an unreadable time is shown as it is."""
    try:
        moment = dt.datetime.fromisoformat(iso[:-1] + "+00:00" if iso.endswith("Z") else iso)
    except ValueError:
        return mrkdwn_escape(iso)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return "<!date^%d^{date_short_pretty} {time}|%s>" % (int(moment.timestamp()), mrkdwn_escape(iso))


def short_tag(tag: object) -> str:
    """A tag for mrkdwn beside other text, cut to SLACK_VERSION_LIMIT."""
    return mrkdwn_escape(slack_cut(str(tag), SLACK_VERSION_LIMIT))


def mrkdwn(text: str) -> Dict[str, object]:
    """A mrkdwn text object Slack shows as written: verbatim, so it does not
    turn a channel name into a link or a written @channel, @here or
    @everyone into a mention that notifies the channel. The ``<!date^…>``
    and ``<url|text>`` this module writes itself still work."""
    return {"type": "mrkdwn", "text": text, "verbatim": True}


def changes_text(info: Dict[str, object]) -> Optional[str]:
    """The post's 변경 사항 section, or None when there is no commit to list.

    The notes are newest first. When they do not fit in one section, the
    newest that fit are kept and the last line counts the rest, which the
    GitHub Release lists in full. The heading's tags are cut, so the heading
    alone always leaves room for that count.
    """
    notes = info.get("notes") or []
    if not notes:
        return None
    head = "*📋 변경 사항*"
    previous = info.get("previous")
    if previous:
        head += " _(%s → %s)_" % (short_tag(previous), short_tag(info["version"]))
    head += "\n\n"
    lines = ["• %s %s" % (mrkdwn_escape(note["sha"]), mrkdwn_escape(note["subject"])) for note in notes]
    text = head + "\n".join(lines)
    if slack_length(text) <= SLACK_SECTION_LIMIT:
        return text

    def rest(count: int) -> str:
        return "… 외 %d개 (릴리스 노트 참고)" % count

    kept: List[str] = []
    used = slack_length(head)
    for line in lines:
        # This line, its newline, and the count of what is left after it.
        if used + slack_length(line) + 1 + slack_length(rest(len(lines) - len(kept) - 1)) > SLACK_SECTION_LIMIT:
            break
        kept.append(line)
        used += slack_length(line) + 1
    return head + "\n".join(kept + [rest(len(lines) - len(kept))])


def slack_message(info: Dict[str, object]) -> Dict[str, object]:
    """The #releases post without its channel: soma-work's deploy post
    (notifyRelease in its packages/slack/src/release-notifier.ts) under this
    project's name. The announce job takes only ``text`` and ``blocks`` from
    it, so the channel and the other chat.postMessage fields are not chosen
    here. Tags carry their ``v`` already, so none is added."""
    tag = str(info["version"])
    previous = info.get("previous")
    commit = str(info.get("commit") or "")
    text = "🚀 *%s* 배포 완료" % short_tag(tag)
    if previous:
        text += "  _(%s → %s)_" % (short_tag(previous), short_tag(tag))
    # plain_text is shown as it is: nothing to escape. Only an odd tag makes
    # it long.
    header = slack_cut("🚀 %s %s 배포" % (PROJECT_NAME, tag), SLACK_HEADER_LIMIT)
    blocks: List[Dict[str, object]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header, "emoji": True}},
        {
            "type": "section",
            "fields": [
                mrkdwn("*버전*\n`%s`" % short_tag(tag)),
                mrkdwn("*브랜치*\n`%s`" % mrkdwn_escape(str(info.get("branch") or "-"))),
                mrkdwn("*커밋*\n`%s`" % mrkdwn_escape(commit[:8])),
                mrkdwn("*커밋 시간*\n%s" % slack_date(str(info.get("commit_time") or ""))),
            ],
        },
    ]
    changes = changes_text(info)
    if changes is not None:
        blocks += [{"type": "divider"}, {"type": "section", "text": mrkdwn(changes)}]
    footer = "빌드: %s | 커밋: `%s`" % (slack_date(str(info.get("built_at") or "")), mrkdwn_escape(commit[:12]))
    repository = info.get("repository")
    # A link to a cut tag would lead nowhere, and an uncut one would not fit.
    if repository and slack_cut(tag, SLACK_VERSION_LIMIT) == tag:
        footer += " | <https://github.com/%s/releases/tag/%s|릴리스 노트>" % (mrkdwn_escape(str(repository)), mrkdwn_escape(tag))
    blocks.append({"type": "context", "elements": [mrkdwn(footer)]})
    return {"text": text, "blocks": blocks}


def read_released(path: Optional[str]) -> List[str]:
    """Tags of the published Releases, one per line; no file means none."""
    if not path:
        return []
    return [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def cmd_prepare(args: argparse.Namespace) -> int:
    out = Path(args.out)
    try:
        released = read_released(args.released_file)
        info, rule = version_info(repository=args.repository, branch=args.branch, released=released)
        (out / "VERSION").write_text("%s\n" % info["version"], encoding="utf-8")
        (out / "version.json").write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except (GitError, OSError) as exc:
        # Nothing on stdout: the step reads it as the answer.
        print("release: %s" % exc, file=sys.stderr)
        return 1
    print(
        "release: %s (%s; previous %s; %d commits in the notes)"
        % (
            info["version"],
            "new tag" if info["new_tag"] else "already on HEAD, kept",
            info["previous"] or "none",
            len(info["notes"]),
        ),
        file=sys.stderr,
    )
    print("release: %s" % rule, file=sys.stderr)
    print("tag=%s" % info["version"])
    print("previous=%s" % (info["previous"] or ""))
    print("new_tag=%s" % ("true" if info["new_tag"] else "false"))
    return 0


def cmd_release_notes(args: argparse.Namespace) -> int:
    try:
        info = json.loads(Path(args.version_json).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("release: cannot read %s: %s" % (args.version_json, exc), file=sys.stderr)
        return 1
    sys.stdout.write(render_release_notes(info))
    return 0


def cmd_slack_message(args: argparse.Namespace) -> int:
    try:
        info = json.loads(Path(args.version_json).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("release: cannot read %s: %s" % (args.version_json, exc), file=sys.stderr)
        return 1
    tag = info.get("version") if isinstance(info, dict) else None
    if not isinstance(tag, str) or not tag:
        print("release: %s names no version; there is no announcement" % args.version_json, file=sys.stderr)
        return 1
    try:
        message = slack_message(info)
    except (AttributeError, KeyError, TypeError) as exc:
        # Nothing on stdout: the step's file must not hold half a message.
        print(
            "release: %s is not a version.json prepare wrote (%s: %s); there is no announcement"
            % (args.version_json, type(exc).__name__, exc),
            file=sys.stderr,
        )
        return 1
    # ASCII-escaped: the bytes do not depend on the locale the runner gives
    # stdout, and jq reads the same text back.
    sys.stdout.write(json.dumps(message) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Release versions for the Deploy workflow (deploy.yml)")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="pick HEAD's version tag; write VERSION and version.json")
    prepare.add_argument("--out", required=True, help="directory to write VERSION and version.json into")
    prepare.add_argument("--repository", help="OWNER/NAME, for the compare link in the release notes")
    prepare.add_argument("--branch", help="branch being deployed, recorded in version.json")
    prepare.add_argument(
        "--released-file",
        help="file with the tags of the published GitHub Releases, one per line (empty: none)",
    )
    prepare.set_defaults(func=cmd_prepare)
    notes = sub.add_parser("release-notes", help="print the GitHub Release body for a version.json")
    notes.add_argument("version_json", help="path to the version.json prepare wrote")
    notes.set_defaults(func=cmd_release_notes)
    slack = sub.add_parser(
        "slack-message", help="print the #releases Slack post (JSON with text and blocks) for a version.json"
    )
    slack.add_argument("version_json", help="path to the version.json prepare wrote")
    slack.set_defaults(func=cmd_slack_message)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
