"""Where a finding lands on the diff: by the code it quotes, not by the line number it guesses.

Agents copy the code a finding is about into ``existing_code``. Matching that
text against the new side of each hunk gives the line without trusting the
model to count: a model that miscounts by one put a P1 on an unchanged blank
line, where it got no comment. The line number the agent wrote stays the
fallback.

Order (``DiffAnchors.place``): the quoted code in the finding's own file; when
it is nowhere in that file, the one other file where it appears (two or more
files: no guess); otherwise nothing, and the caller tries the line number.

Standard library only: the daemon runs on macOS's stock Python 3.9.
"""

from __future__ import annotations

import dataclasses
import posixpath
import re
from typing import Any, Iterable

CODE = "code"  # the quoted code, in the finding's own file
MOVED = "moved"  # the quoted code, found in exactly one other file of the diff
LINE = "line"  # the line number the agent wrote
# The quote names unchanged code: no comment, and no falling back to the agent's line,
# which could be an unrelated added line. Its line is the match's first line, for the record.
UNCHANGED = "unchanged"

# A quote longer than this is not a pointer to code any more; it is matched on its first lines.
MAX_QUOTED_LINES = 40
_HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_MARKER = re.compile(r"^[+-] ?")


@dataclasses.dataclass(frozen=True)
class DiffLine:
    line: int  # new-side line number
    text: str  # whitespace-normalized, never empty
    added: bool


@dataclasses.dataclass(frozen=True)
class Placement:
    path: str
    line: int
    how: str  # CODE, MOVED or LINE


def _norm(text: str) -> str:
    return " ".join(text.split())


def quoted_lines(snippet: Any) -> list:
    """The non-blank lines of ``existing_code``, whitespace-normalized; [] when there is none."""
    if not isinstance(snippet, str):
        return []
    lines = [_norm(line) for line in snippet.split("\n")]
    return [line for line in lines if line][:MAX_QUOTED_LINES]


def _without_markers(snippet: str) -> list:
    """The quote again with a leading diff marker taken off each line, for a quote copied with them."""
    return quoted_lines("\n".join(_MARKER.sub("", line.strip()) for line in snippet.split("\n")))


class DiffAnchors:
    """The new side of every hunk in a PR's file patches, blank lines left out."""

    def __init__(self, files: Iterable[dict[str, Any]]) -> None:
        self.hunks: dict[str, list] = {}
        # (path, quote) -> matching runs: a review places each finding in several passes
        # (inline, blockers, unplaced, counts, raised keys), and each would search the diff again.
        self._found: dict[tuple, list] = {}
        for file_info in files:
            path = file_info.get("filename")
            patch = file_info.get("patch")
            if path and patch:
                self.hunks[str(path)] = _new_side_hunks(str(patch))

    def own_files(self, path: str) -> list:
        """The diff files ``path`` may name: itself, or every file it ends with (an absolute path).

        More than one means the finding's own file is ambiguous (``/x/src/a.py``
        ends with both ``src/a.py`` and ``a.py``); none means it is not in the diff.
        """
        if path in self.hunks:
            return [path]
        if not path:
            return []
        # "./src/a.py" and "src//a.py" name src/a.py.
        path = posixpath.normpath(path)
        if path in self.hunks:
            return [path]
        return [name for name in self.hunks if path.endswith("/" + name)]

    def find(self, path: str, wanted: list) -> list:
        """Every run of consecutive new-side lines of ``path`` (one hunk) equal to ``wanted``."""
        found = []
        size = len(wanted)
        for hunk in self.hunks.get(path, []):
            for start in range(len(hunk) - size + 1):
                if all(hunk[start + offset].text == wanted[offset] for offset in range(size)):
                    found.append(hunk[start:start + size])
        return found

    def _find_quote(self, path: str, snippet: str) -> list:
        """The quote's runs in ``path``: as written first, then with leading diff markers taken off.

        As written first, so code that starts with an operator (a YAML ``- item``,
        a ``-1``) matches itself before a marker-stripped reading can match
        something else.
        """
        key = (path, snippet)
        if key not in self._found:
            found = self.find(path, quoted_lines(snippet))
            if not found:
                bare = _without_markers(snippet)
                if bare and bare != quoted_lines(snippet):
                    found = self.find(path, bare)
            self._found[key] = found
        return self._found[key]

    def place(self, path: str, line: int | None, snippet: Any) -> Placement | None:
        """Where the quoted code sits on an added line; UNCHANGED when it names unchanged code;
        None when the quote decides nothing (the caller falls back to ``line``).

        In the finding's own file a quote that matches more than once takes the
        match nearest ``line``, changed or not: when that match has no added
        line the finding is about unchanged code and nothing is placed. A quote
        found in its own file is not looked for elsewhere, and neither is one
        whose own file is ambiguous (own_files): moving it could pick the file
        the agent did not mean.

        Elsewhere, files count, not matches: the quote must sit on added lines
        in exactly one other file, and within that file the same choice applies
        (the nearest match, changed or not; none with added lines, no place).
        """
        if not quoted_lines(snippet):
            return None
        owns = self.own_files(path)
        if len(owns) > 1:
            return None
        own = owns[0] if owns else None
        if own is not None:
            found = self._find_quote(own, snippet)
            if found:
                chosen = _pick(found, line)
                if chosen is None:
                    return None
                if not any(item.added for item in chosen):
                    return Placement(own, chosen[0].line, UNCHANGED)
                return Placement(own, _anchor_line(chosen, line), CODE)
        elsewhere: dict = {}
        for name in self.hunks:
            if name == own:
                continue
            runs = self._find_quote(name, snippet)
            if any(item.added for run in runs for item in run):
                elsewhere[name] = runs
        if len(elsewhere) != 1:
            return None
        name, runs = next(iter(elsewhere.items()))
        chosen = _pick(runs, line)
        if chosen is None:
            return None
        if not any(item.added for item in chosen):
            return Placement(name, chosen[0].line, UNCHANGED)
        return Placement(name, _anchor_line(chosen, line), MOVED)

    def quoted_in(self, snippet: Any) -> frozenset:
        """Every diff file where the quote appears at all, on added lines or not."""
        if not quoted_lines(snippet):
            return frozenset()
        return frozenset(name for name in self.hunks if self._find_quote(name, snippet))


def _new_side_hunks(patch: str) -> list:
    hunks: list = []
    current: list | None = None
    new_line = 0
    # split, not splitlines: a U+2028 inside a diff line must not shift the count.
    for raw in patch.split("\n"):
        hunk = _HUNK.match(raw)
        if hunk:
            current = []
            hunks.append(current)
            new_line = int(hunk.group(1))
            continue
        if current is None or raw.startswith("-") or raw.startswith("\\"):
            continue
        text = _norm(raw[1:])
        if text:
            current.append(DiffLine(new_line, text, raw.startswith("+")))
        new_line += 1
    return hunks


def _pick(runs: list, line: int | None) -> list | None:
    if len(runs) == 1:
        return runs[0]
    if not runs or line is None:
        return None

    def distance(run: list) -> int:
        return max(0, run[0].line - line, line - run[-1].line)

    ranked = sorted(runs, key=distance)
    return ranked[0] if distance(ranked[0]) < distance(ranked[1]) else None


def _anchor_line(run: list, line: int | None) -> int:
    """The agent's line when it is an added line of the run, else the run's last added line."""
    added = [item.line for item in run if item.added]
    return line if line in added else added[-1]
