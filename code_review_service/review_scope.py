"""Which changed files of a pull request the reviewer agents read: the review scope.

Generated files, documentation and vendored dependencies are left out of the
diff by default; an operator brings a category back per repository
(``exclude_categories``) or one path at a time (``include``). Secret paths —
credential files such as ``.env`` or ``id_rsa`` — are left out of every prompt
the service builds (the diff, and the repository rules), and no setting brings
them back. A runner that reads the checkout itself (the Codex rollback runner)
is outside that guarantee. ``files_not_read`` then names every file the
agents did not read in full and why, so a review never looks complete when it
was not.

The setting lives in this service's config, never in the reviewed repository,
so a pull request cannot widen or narrow its own review. A file a pattern
leaves out is still listed by name on the review, so moving code into a
``*.min.js`` or a ``docs/*.md`` does not hide it from the people reading it.

Standard library only: the daemon runs on macOS's stock Python 3.9.
"""

from __future__ import annotations

import dataclasses
import posixpath
import re
from typing import Any, Iterable

# Why a file is not in the diff the agents read. The first five are review
# scope decisions; the last three are what the diff itself could not carry.
SECRET = "secret"
CONFIGURED = "configured"
GENERATED = "generated"
DOCUMENTATION = "documentation"
VENDORED = "vendored"
BUDGET = "budget"
NO_PATCH = "no-patch"
BINARY = "binary"
CUT_PATCH = "cut-patch"

CATEGORIES = (GENERATED, DOCUMENTATION, VENDORED)
DEFAULT_EXCLUDE_CATEGORIES = CATEGORIES
# Reasons that make a review partial: a file it chose to review, it did not read.
PARTIAL_REASONS = frozenset({BUDGET, NO_PATCH, CUT_PATCH})

REASON_TEXT = {
    SECRET: "secret path, left out of every prompt",
    CONFIGURED: "excluded by review_scope.exclude",
    GENERATED: "generated file",
    DOCUMENTATION: "documentation",
    VENDORED: "vendored dependency",
    BUDGET: "past the diff budget (max_diff_chars)",
    NO_PATCH: "GitHub sent no patch (the diff is too large)",
    BINARY: "no text diff (binary file)",
    CUT_PATCH: "GitHub's patch is shorter than the change (cut by GitHub)",
}

SECRET_PATTERNS = (
    "**/.ssh/**",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    ".netrc",
    "_netrc",
    ".npmrc",
    ".pypirc",
    ".dockercfg",
    "**/.docker/config.json",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
)
# .env and .env.<anything> are secret, except the templates projects commit on purpose.
ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template", ".env.dist"})

GENERATED_PATTERNS = (
    # Dependency lock files: written by the package manager.
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lock",
    "bun.lockb",
    "Cargo.lock",
    "poetry.lock",
    "Pipfile.lock",
    "uv.lock",
    "pdm.lock",
    "composer.lock",
    "Gemfile.lock",
    "go.sum",
    "Package.resolved",
    "Podfile.lock",
    "pubspec.lock",
    "mix.lock",
    "flake.lock",
    "gradle.lockfile",
    "packages.lock.json",
    ".terraform.lock.hcl",
    # Minified bundles and source maps.
    "*.min.js",
    "*.min.mjs",
    "*.min.css",
    "*.js.map",
    "*.mjs.map",
    "*.css.map",
    # Code generators' conventional names.
    "*.pb.go",
    "*.pb.cc",
    "*.pb.h",
    "*.pb.swift",
    "*_pb2.py",
    "*_pb2.pyi",
    "*_pb2_grpc.py",
    "*.gen.go",
    "*_generated.go",
    "zz_generated*.go",
    "*.generated.*",
    "*.g.dart",
    "*.freezed.dart",
    "*.designer.cs",
    # Test snapshots and committed build output.
    "**/__snapshots__/**",
    "*.snap",
    "**/dist/**",
    "**/.next/**",
    "**/.nuxt/**",
    "**/.svelte-kit/**",
)
DOCUMENTATION_PATTERNS = (
    "*.md",
    "*.mdx",
    "*.markdown",
    "*.rst",
    "*.adoc",
    "*.asciidoc",
    "*.rdoc",
    "*.textile",
    "**/docs/**/*.txt",
    "**/doc/**/*.txt",
    # Project notices, with no extension or .txt (a bare HISTORY* would take history.py).
    "{LICENSE,LICENCE,COPYING,NOTICE,AUTHORS,CONTRIBUTORS,CHANGELOG,CHANGES,HISTORY}",
    "{LICENSE,LICENCE,COPYING,NOTICE,AUTHORS,CONTRIBUTORS,CHANGELOG,CHANGES,HISTORY}.txt",
)
VENDORED_PATTERNS = (
    "**/vendor/**",
    "**/node_modules/**",
    "**/bower_components/**",
    "**/.yarn/{cache,releases,unplugged,sdks}/**",
    ".pnp.cjs",
    ".pnp.loader.mjs",
    "**/Pods/**",
    "**/Carthage/**",
)

# The two notices generators write in a comment at the top of a file: Go's
# "Code generated ... DO NOT EDIT." and "@generated" (Meta, Buck, others). Only a
# comment line counts, not code that mentions the words, and only its first
# GENERATED_HEADER_CHARS characters are read: a PR-supplied line can be megabytes
# long, and the bounded gap keeps the match linear.
GENERATED_HEADER = re.compile(
    r"\s*(?://|#|/\*|\*|--|<!--|;|%)\s*(?:Code generated\b[^\n]{0,200}?\bDO NOT EDIT\b|@generated\b)"
)
GENERATED_HEADER_LINES = 5
GENERATED_HEADER_CHARS = 300
_HUNK = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def glob_regex(pattern: str) -> "re.Pattern[str]":
    """A compiled, case-insensitive regex for one path glob.

    ``**`` spans directories, ``*`` and ``?`` stay inside one, ``{a,b}`` is a
    choice and ``[...]`` a character class. A pattern without a ``/`` matches
    the file name at any depth, like a .gitignore line; a leading ``/``
    anchors it at the repository root.
    """
    text = pattern.strip()
    anchored = text.startswith("/")
    text = text.lstrip("/")
    prefix = "" if anchored or "/" in text else "(?:.*/)?"
    # DOTALL: a Git path may hold a line feed, and a secret pattern must still match it.
    return re.compile(prefix + _translate(text), re.IGNORECASE | re.DOTALL)


def _translate(text: str) -> str:
    """The regex for a glob body; each ``{a,b}`` choice is a glob in its own right."""
    out = []
    i = 0
    while i < len(text):
        char = text[i]
        if text.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif text.startswith("/**", i) and i + 3 == len(text):
            out.append("(?:/.*)?")
            i += 3
        elif text.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        elif char == "{":
            end = text.find("}", i)
            if end < 0:
                out.append(re.escape(char))
                i += 1
                continue
            choices = text[i + 1:end].split(",")
            out.append("(?:" + "|".join(_translate(choice) for choice in choices) + ")")
            i = end + 1
        elif char == "[":
            end = text.find("]", i + 1)
            if end < 0:
                out.append(re.escape(char))
                i += 1
                continue
            body = text[i + 1:end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = end + 1
        else:
            out.append(re.escape(char))
            i += 1
    return "".join(out)


def _compile(patterns: Iterable[str]) -> tuple:
    return tuple(glob_regex(pattern) for pattern in patterns)


_SECRET = _compile(SECRET_PATTERNS)
_CATEGORY_PATTERNS = {
    GENERATED: _compile(GENERATED_PATTERNS),
    DOCUMENTATION: _compile(DOCUMENTATION_PATTERNS),
    VENDORED: _compile(VENDORED_PATTERNS),
}


def _matches(path: str, patterns: Iterable["re.Pattern[str]"]) -> bool:
    return any(pattern.fullmatch(path) for pattern in patterns)


def is_secret_path(path: str) -> bool:
    """Whether ``path`` names a credential file that must never reach a model (by name only)."""
    if not path:
        return False
    base = posixpath.basename(path).lower()
    if base not in ENV_TEMPLATES and (base == ".env" or base.startswith(".env.")):
        return True
    return _matches(path, _SECRET)


def has_generated_header(patch: str, removed: bool = False, first_lines: int = GENERATED_HEADER_LINES) -> bool:
    """Whether the patch shows a generator's header in the file's first ``first_lines`` lines.

    Only lines the patch carries count, so a header outside every hunk is not
    seen; the path patterns cover the rest. The new side is read (added and
    context lines); for a ``removed`` file, whose every line is deleted, the
    old side is.
    """
    line = None
    # split, not splitlines: a U+2028 inside a diff line must not shift the count.
    for raw in patch.split("\n"):
        hunk = _HUNK.match(raw)
        if hunk:
            line = int(hunk.group(1 if removed else 2))
            continue
        # Past the top of the file, the rest of the hunk is too: wait for the next one.
        if line is None or line > first_lines or raw.startswith("\\") or raw.startswith("+" if removed else "-"):
            continue
        if GENERATED_HEADER.match(raw[1:1 + GENERATED_HEADER_CHARS]):
            return True
        line += 1
    return False


@dataclasses.dataclass(frozen=True)
class ScopeSettings:
    """One repository's review scope, resolved from config (settings_for)."""

    exclude_categories: frozenset = frozenset(DEFAULT_EXCLUDE_CATEGORIES)
    include: tuple = ()  # compiled globs reviewed even when a category leaves them out
    exclude: tuple = ()  # compiled globs left out on top of the categories


@dataclasses.dataclass(frozen=True)
class Scope:
    """The files the agents read and the ones left out, each in GitHub's order."""

    reviewed: tuple  # GitHub file dicts
    excluded: tuple  # (path, reason)


def file_reason(file_info: dict[str, Any], settings: ScopeSettings) -> str | None:
    """Why the review leaves this file out, or None when the agents read it.

    Order: a secret path always; then the operator's ``exclude``; then
    ``include``, which beats every category; then the categories.
    """
    path = str(file_info.get("filename") or "")
    previous = str(file_info.get("previous_filename") or "")
    # A file renamed away from a secret name still carries that content in its patch.
    if is_secret_path(path) or is_secret_path(previous):
        return SECRET
    if _matches(path, settings.exclude):
        return CONFIGURED
    if _matches(path, settings.include):
        return None
    for category in CATEGORIES:
        if category not in settings.exclude_categories:
            continue
        if _matches(path, _CATEGORY_PATTERNS[category]):
            return category
        removed = file_info.get("status") == "removed"
        if category == GENERATED and has_generated_header(str(file_info.get("patch") or ""), removed=removed):
            return GENERATED
    return None


def select_files(files: Iterable[dict[str, Any]], settings: ScopeSettings) -> Scope:
    reviewed = []
    excluded = []
    for file_info in files:
        reason = file_reason(file_info, settings)
        if reason is None:
            reviewed.append(file_info)
        else:
            excluded.append((str(file_info.get("filename") or "<unknown>"), reason))
    return Scope(tuple(reviewed), tuple(excluded))


def files_not_read(scope: Scope, shown: Iterable[str]) -> list:
    """Every changed file the agents did not read in full: ``[(path, reason)]``.

    ``shown`` is the set of files whose whole patch was in the agents' diff
    (diff_and_shown_files). A reviewed file outside it was cut by the diff
    budget, or GitHub sent no patch: for a binary file (nothing added or
    deleted) that is ``binary``, otherwise the diff was too large. A file in
    ``shown`` whose patch has fewer added and deleted lines than GitHub counts
    for it was cut by GitHub (``cut-patch``). A rename with no change has
    nothing to read and is not listed.

    The files that make the review partial come first, so a list cut for
    length still names them.
    """
    seen = frozenset(shown)
    missing = list(scope.excluded)
    for file_info in scope.reviewed:
        path = str(file_info.get("filename") or "<unknown>")
        if path in seen:
            if _patch_lines(str(file_info.get("patch") or "")) < _changed(file_info):
                missing.append((path, CUT_PATCH))
            continue
        if file_info.get("patch"):
            missing.append((path, BUDGET))
            continue
        if _changed(file_info):
            missing.append((path, NO_PATCH))
        elif file_info.get("status") != "renamed":
            missing.append((path, BINARY))
    return sorted(missing, key=lambda item: item[1] not in PARTIAL_REASONS)


def _changed(file_info: dict[str, Any]) -> int:
    """The added plus deleted lines GitHub counts for a file."""
    try:
        return int(file_info.get("additions") or 0) + int(file_info.get("deletions") or 0)
    except (TypeError, ValueError):
        return 0


def _patch_lines(patch: str) -> int:
    """The added and deleted lines a patch carries (inside hunks; ``+++x`` there is an added line)."""
    count = 0
    in_hunk = False
    for raw in patch.split("\n"):
        if raw.startswith("@@"):
            in_hunk = True
        elif in_hunk and raw[:1] in ("+", "-"):
            count += 1
    return count


def is_partial(not_read: Iterable[tuple]) -> bool:
    """Whether the agents skipped a file this review chose to read (cut, or no patch)."""
    return any(reason in PARTIAL_REASONS for _, reason in not_read)


def _string_list(value: Any, where: str) -> list:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{where} must be a list of non-empty strings")
    return [item.strip() for item in value]


def _glob_list(value: Any, where: str) -> list:
    """A list of globs that compile: a bad one fails at startup, not in every review."""
    patterns = _string_list(value, where)
    for pattern in patterns:
        try:
            glob_regex(pattern)
        except re.error as exc:
            raise ValueError(f"{where}: {pattern!r} is not a valid glob ({exc})") from None
    return patterns


def _categories(value: Any, where: str) -> list:
    names = _string_list(value, where)
    unknown = sorted(set(names) - set(CATEGORIES))
    if unknown:
        raise ValueError(f"{where} names unknown categories {unknown}; use {list(CATEGORIES)}")
    return names


def _block(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be an object")
    block: dict[str, Any] = {}
    if "exclude_categories" in raw:
        block["exclude_categories"] = _categories(raw["exclude_categories"], f"{where}.exclude_categories")
    for key in ("include", "exclude"):
        if key in raw:
            block[key] = _glob_list(raw[key], f"{where}.{key}")
    return block


def validate(config: dict[str, Any]) -> None:
    """Raise ValueError when ``config.review_scope`` has a bad shape. Absent means the defaults."""
    raw = config.get("review_scope")
    if raw is None:
        return
    _block(raw, "config.review_scope")
    per_repo = raw.get("repositories", {})
    if not isinstance(per_repo, dict):
        raise ValueError("config.review_scope.repositories must be an object keyed by owner/name")
    for repo, block in per_repo.items():
        if not isinstance(repo, str) or not _REPOSITORY.fullmatch(repo):
            raise ValueError(f"config.review_scope.repositories key {repo!r} is not owner/name")
        _block(block, f"config.review_scope.repositories[{repo}]")


def settings_for(config: dict[str, Any], repo: str) -> ScopeSettings:
    """The review scope of ``repo``: the global block, each key the repository sets replacing it.

    Repository keys compare case-insensitively, as GitHub names do. The config
    was validated at load time (validate).
    """
    raw = config.get("review_scope") or {}
    merged = _block(raw, "config.review_scope")
    for name, block in (raw.get("repositories") or {}).items():
        if name.lower() == str(repo).lower():
            merged.update(_block(block, f"config.review_scope.repositories[{name}]"))
    return ScopeSettings(
        exclude_categories=frozenset(merged.get("exclude_categories", DEFAULT_EXCLUDE_CATEGORIES)),
        include=_compile(merged.get("include", ())),
        exclude=_compile(merged.get("exclude", ())),
    )
