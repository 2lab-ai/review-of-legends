"""Per-repository visual outline settings and the ui-diff stage.

Two things live here, both standard library only (the daemon runs on macOS's
stock Python 3.9):

* **Which repository gets what.** The visual outline and its UI diff are
  switched per repository: the dashboard's saved choice
  (``visual_outline_repositories``) wins over ``visual_outline.repositories``
  in the config, which wins over the config-wide ``visual_outline.enabled``.
  The UI diff is part of the outline: it runs only where the outline is on and
  ``visual_outline.ui_diff.command`` says how to start ui-diff.
* **Running ui-diff** (https://github.com/imuratalpay/ui-diff) on a review's
  clone: ``run`` captures the base and the head of the PR and diffs them,
  ``publish --no-comment`` pushes the images to the repository's own assets
  branch and writes the comment markdown, which becomes a section of the
  outline comment. ``run`` starts the PR's own code (its install scripts and dev
  server), so it gets an allowlisted environment without any credential; only
  ``publish``, which runs ui-diff's own code, gets the GitHub token.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

#: Dashboard store key of the operator's per-repository choices:
#: ``{"repositories": {"owner/name": {"enabled": bool, "ui_diff": bool}}}``.
#: Either flag may be absent (the config decides that one).
SETTING = "visual_outline_repositories"
FLAGS = ("enabled", "ui_diff")
MAX_STORED_REPOSITORIES = 200

#: The outline comment carries the UI diff between these two lines, so a new
#: head replaces the section and leaves the outline text alone.
SECTION_START = "<!-- review-of-legends:ui-diff:start -->"
SECTION_END = "<!-- review-of-legends:ui-diff:end -->"
#: A marker comment that carries only a UI diff (the outline agent produced
#: nothing) says so, so the next review still drafts the outline.
OUTLINE_PENDING = "<!-- review-of-legends:outline-pending -->"

#: ui-diff's own sticky-comment marker; dropped from the embedded section so a
#: person running ui-diff by hand never takes the bot's comment for theirs.
_UI_DIFF_MARKER = re.compile(r"<!--\s*ui-diff:sticky[^>]*-->\s*")
#: GitHub refuses a comment body over 65536 characters.
MAX_COMMENT_CHARS = 65000

DEFAULT_TIMEOUT_SECONDS = 1500
DEFAULT_PUBLISH_TIMEOUT_SECONDS = 300
DEFAULT_ASSETS_BRANCH = "ui-diff-assets"
DEFAULT_CACHE_DIR = "../data/ui-diff"
CONFIG_FILENAME = "ui-diff.config.json"
#: ui-diff exits 3 when git cannot read or push the repository (publish).
EXIT_GIT_ACCESS = 3
_BRANCH_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
_ENV_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
#: Names ``ui_diff.env`` may not set: they would undo the isolation below.
_RESERVED_ENV = frozenset(
    {"HOME", "PATH", "npm_config_cache", "PLAYWRIGHT_BROWSERS_PATH", "GH_TOKEN", "GITHUB_TOKEN"}
)
_RESERVED_ENV_PREFIXES = ("GIT_CONFIG", "GIT_ASKPASS", "SSH_")


# --------------------------------------------------------------- settings


def _valid_branch(name: Any) -> bool:
    """A branch name git accepts (git check-ref-format), in the plain subset used here."""
    if not isinstance(name, str) or not _BRANCH_PATTERN.fullmatch(name):
        return False
    if ".." in name or "//" in name or "@{" in name or name.endswith(("/", ".", ".lock")):
        return False
    return not any(part.startswith(".") or part.endswith(".lock") for part in name.split("/"))


def _repo_key(repo: str) -> str:
    return str(repo or "").strip().lower()


def _lookup(mapping: Any, repo: str) -> Optional[Dict[str, Any]]:
    """``mapping[repo]`` matched case-insensitively (GitHub names are), or None."""
    if not isinstance(mapping, dict):
        return None
    key = _repo_key(repo)
    for name, value in mapping.items():
        if _repo_key(name) == key and isinstance(value, dict):
            return value
    return None


def outline_block(config: Dict[str, Any]) -> Dict[str, Any]:
    block = config.get("visual_outline")
    return block if isinstance(block, dict) else {}


def ui_diff_block(config: Dict[str, Any]) -> Dict[str, Any]:
    block = outline_block(config).get("ui_diff")
    return block if isinstance(block, dict) else {}


def ui_diff_available(config: Dict[str, Any]) -> bool:
    """Whether this deployment can run ui-diff at all (a command is configured)."""
    command = ui_diff_block(config).get("command")
    return isinstance(command, list) and bool(command)


def stored_repositories(stored: Any) -> Dict[str, Dict[str, Any]]:
    """The dashboard's per-repository choices, malformed entries left out."""
    repositories = stored.get("repositories") if isinstance(stored, dict) else None
    if not isinstance(repositories, dict):
        return {}
    kept: Dict[str, Dict[str, Any]] = {}
    for name, value in repositories.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            continue
        flags = {flag: value[flag] for flag in FLAGS if isinstance(value.get(flag), bool)}
        if flags:
            kept[name] = flags
    return kept


def repository_settings(config: Dict[str, Any], repo: str, stored: Any = None) -> Dict[str, Any]:
    """What the outline stage does for ``repo``, and where each answer came from.

    ``enabled``: draft the outline. ``ui_diff``: add a UI diff (only where the
    outline is on and ui-diff is configured). ``*_source`` is ``dashboard``,
    ``config`` (``visual_outline.repositories``) or ``default`` (the config-wide
    ``visual_outline.enabled`` for the outline; off for the UI diff).
    """
    block = outline_block(config)
    configured = _lookup(block.get("repositories"), repo) or {}
    saved = _lookup(stored_repositories(stored), repo) or {}
    result: Dict[str, Any] = {}
    defaults = {"enabled": bool(block.get("enabled", False)), "ui_diff": False}
    for flag in FLAGS:
        if isinstance(saved.get(flag), bool):
            result[flag], result[flag + "_source"] = saved[flag], "dashboard"
        elif isinstance(configured.get(flag), bool):
            result[flag], result[flag + "_source"] = configured[flag], "config"
        else:
            result[flag], result[flag + "_source"] = defaults[flag], "default"
    available = ui_diff_available(config)
    result["ui_diff_available"] = available
    result["ui_diff_config"] = _lookup(ui_diff_block(config).get("configs"), repo) is not None
    # What actually runs: the choice, gated by the outline and the install.
    result["ui_diff_effective"] = bool(result["enabled"] and result["ui_diff"] and available)
    return result


def any_repository_enabled(config: Dict[str, Any]) -> bool:
    """True when the config turns the outline on anywhere (globally or for one repository)."""
    block = outline_block(config)
    if block.get("enabled", False):
        return True
    repositories = block.get("repositories")
    return isinstance(repositories, dict) and any(
        isinstance(value, dict) and value.get("enabled") is True for value in repositories.values()
    )


def validate(config: Dict[str, Any]) -> None:
    """ValueError for a malformed ``visual_outline.repositories`` or ``visual_outline.ui_diff``."""
    block = config.get("visual_outline")
    if block is None:
        return
    if not isinstance(block, dict):
        raise ValueError("config.visual_outline must be an object")
    repositories = block.get("repositories")
    if repositories is not None:
        if not isinstance(repositories, dict):
            raise ValueError("config.visual_outline.repositories must be an object keyed by owner/name")
        known = {_repo_key(name) for name in config.get("repositories") or []}
        for name, value in repositories.items():
            if name.startswith("_"):
                continue  # a "_comment" key
            if _repo_key(name) not in known:
                raise ValueError(f"config.visual_outline.repositories names a repository not in config.repositories: {name}")
            if not isinstance(value, dict):
                raise ValueError(f"config.visual_outline.repositories[{name}] must be an object")
            for key, flag in value.items():
                if key.startswith("_"):
                    continue
                if key not in FLAGS:
                    raise ValueError(f"config.visual_outline.repositories[{name}] has an unknown key: {key}")
                if not isinstance(flag, bool):
                    raise ValueError(f"config.visual_outline.repositories[{name}].{key} must be true or false")
    settings = block.get("ui_diff")
    if settings is None:
        return
    if not isinstance(settings, dict):
        raise ValueError("config.visual_outline.ui_diff must be an object")
    command = settings.get("command")
    if command is not None and (
        not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command)
    ):
        raise ValueError("config.visual_outline.ui_diff.command must be a non-empty list of strings")
    for key in ("timeout_seconds", "publish_timeout_seconds", "max_concurrent"):
        value = settings.get(key)
        if key in settings and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            raise ValueError(f"config.visual_outline.ui_diff.{key} must be a positive integer")
    for key in ("upload",):
        if key in settings and not isinstance(settings[key], bool):
            raise ValueError(f"config.visual_outline.ui_diff.{key} must be true or false")
    branch = settings.get("assets_branch")
    if branch is not None and not _valid_branch(branch):
        raise ValueError("config.visual_outline.ui_diff.assets_branch must be a plain branch name")
    cache_dir = settings.get("cache_dir")
    if cache_dir is not None and (not isinstance(cache_dir, str) or not cache_dir.strip()):
        raise ValueError("config.visual_outline.ui_diff.cache_dir must be a non-empty path")
    env = settings.get("env")
    if env is not None:
        if not isinstance(env, dict):
            raise ValueError("config.visual_outline.ui_diff.env must be an object of NAME -> string")
        for name, value in env.items():
            if not isinstance(name, str) or not _ENV_NAME_PATTERN.fullmatch(name) or not isinstance(value, str):
                raise ValueError(f"config.visual_outline.ui_diff.env[{name}] must be a NAME -> string pair")
            if name in _RESERVED_ENV or name.startswith(_RESERVED_ENV_PREFIXES):
                raise ValueError(f"config.visual_outline.ui_diff.env may not set {name}")
    sandbox = settings.get("sandbox")
    if sandbox is not None:
        if not isinstance(sandbox, dict):
            raise ValueError("config.visual_outline.ui_diff.sandbox must be an object")
        for key in sandbox:
            if key.startswith("_"):
                continue
            if key not in ("deny", "deny_write"):
                raise ValueError(f"config.visual_outline.ui_diff.sandbox has an unknown key: {key}")
            paths = sandbox[key]
            if not isinstance(paths, list) or not all(
                isinstance(path, str) and os.path.isabs(os.path.expanduser(path)) for path in paths
            ):
                raise ValueError(f"config.visual_outline.ui_diff.sandbox.{key} must be a list of absolute paths")
    configs = settings.get("configs")
    if configs is not None:
        if not isinstance(configs, dict):
            raise ValueError("config.visual_outline.ui_diff.configs must be an object keyed by owner/name")
        for name, value in configs.items():
            if name.startswith("_"):
                continue
            if not isinstance(value, dict):
                raise ValueError(f"config.visual_outline.ui_diff.configs[{name}] must be a ui-diff.config.json object")


def validate_stored(value: Any, known_repositories: List[str]) -> Dict[str, Any]:
    """The dashboard setting as it will be saved, or ValueError (web tier)."""
    if not isinstance(value, dict) or set(value) != {"repositories"} or not isinstance(value["repositories"], dict):
        raise ValueError('the setting must be {"repositories": {owner/name: {...}}}')
    known = {_repo_key(name): name for name in known_repositories}
    repositories: Dict[str, Dict[str, bool]] = {}
    for name, flags in value["repositories"].items():
        canonical = known.get(_repo_key(name))
        if canonical is None:
            raise ValueError(f"{name} is not on the review list")
        if not isinstance(flags, dict) or not flags or not set(flags) <= set(FLAGS):
            raise ValueError(f"{name}: only {', '.join(FLAGS)} may be set")
        if not all(isinstance(flag, bool) for flag in flags.values()):
            raise ValueError(f"{name}: each flag must be true or false")
        repositories[canonical] = dict(flags)
    if len(repositories) > MAX_STORED_REPOSITORIES:
        raise ValueError(f"at most {MAX_STORED_REPOSITORIES} repositories can carry a visual outline choice")
    return {"repositories": repositories}


# --------------------------------------------------------------- the comment section


def strip_section(body: str) -> str:
    """``body`` without its UI diff section (every one, should a hand edit have doubled it)."""
    text = str(body or "")
    while True:
        start = text.find(SECTION_START)
        if start < 0:
            return text
        end = text.find(SECTION_END, start)
        if end < 0:
            return text[:start].rstrip() + "\n"
        text = (text[:start].rstrip() + "\n" + text[end + len(SECTION_END):].lstrip("\n")).rstrip() + "\n"


def merge_section(body: str, section: str) -> str:
    """The outline comment with ``section`` as its only UI diff section (at the end)."""
    base = strip_section(body).rstrip()
    merged = f"{base}\n\n{section.strip()}\n"
    if len(merged) <= MAX_COMMENT_CHARS:
        return merged
    room = MAX_COMMENT_CHARS - len(base) - 400
    inner = section.strip()
    if room <= len(SECTION_START) + 200:
        # Not even a cut section fits with its markers: leave the outline alone.
        return base + "\n"
    cut = inner[:room]
    if not cut.endswith(SECTION_END):
        cut = cut.rstrip() + "\n\n<sub>UI diff truncated to fit GitHub's comment size limit.</sub>\n" + SECTION_END
    return f"{base}\n\n{cut}\n"


def build_section(markdown: str, notes: List[str], head_sha: str, elapsed: float) -> str:
    """The UI diff section: ui-diff's comment, the notes, one provenance line."""
    inner = _UI_DIFF_MARKER.sub("", str(markdown or "")).strip()
    lines = [SECTION_START, ""]
    if inner:
        lines += [inner, ""]
    for note in notes:
        lines.append(f"> {note}")
    if notes:
        lines.append("")
    minutes, seconds = divmod(int(round(elapsed)), 60)
    lines.append(
        f"<sub>— Review of Legends UI diff · PR head `{str(head_sha)[:7]}` · {minutes}m{seconds:02d}s</sub>"
    )
    lines.append(SECTION_END)
    return "\n".join(lines)


def failure_section(reason: str, head_sha: str, elapsed: float) -> str:
    return build_section("", [f"⚠️ UI diff could not run on this head: {reason}"], head_sha, elapsed)


def pending_outline_body(marker: str) -> str:
    """The marker comment's text when only the UI diff is ready (no outline yet)."""
    return "\n".join([
        marker,
        OUTLINE_PENDING,
        "",
        "_The visual outline could not be drafted for this pull request yet; the next review tries again._",
        "",
    ])


# --------------------------------------------------------------- running ui-diff


class Outcome(object):
    """What one ui-diff stage produced: the section to post (or None) and a log line."""

    def __init__(self, section: Optional[str], status: str, detail: str = "") -> None:
        self.section = section
        self.status = status  # posted-ready kinds: "ok", "text-only", "failed"; "skipped" posts nothing
        self.detail = detail


_SLOTS_LOCK = threading.Lock()
_SLOTS: List[threading.BoundedSemaphore] = []


def _acquire(slots: threading.BoundedSemaphore, wait: float, cancel: Optional[threading.Event]) -> bool:
    """Take a slot within ``wait`` seconds, giving up early on ``cancel``."""
    deadline = time.monotonic() + wait
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or (cancel is not None and cancel.is_set()):
            return False
        if slots.acquire(timeout=min(POLL_SECONDS, remaining)):
            return True


def _slots(limit: int) -> threading.BoundedSemaphore:
    """The one semaphore every UI diff takes: ui-diff runs two dev servers and a browser.

    Sized by the first ``max_concurrent`` it sees; a changed limit takes effect
    after a restart, so runs admitted under the old limit are never forgotten.
    """
    with _SLOTS_LOCK:
        if not _SLOTS:
            _SLOTS.append(threading.BoundedSemaphore(limit))
        return _SLOTS[0]


def cache_root(config: Dict[str, Any], config_dir: Path) -> Path:
    raw = str(ui_diff_block(config).get("cache_dir") or DEFAULT_CACHE_DIR)
    path = Path(os.path.expanduser(raw))
    return path if path.is_absolute() else (Path(config_dir) / path).resolve()


#: macOS's sandbox: the run step (the PR's code) runs under a profile built from
#: visual_outline.ui_diff.sandbox when that block is set.
SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def _sbpl(path: Any) -> str:
    """A path as an SBPL string literal, resolved the way the kernel sees it (/tmp is /private/tmp)."""
    real = os.path.realpath(os.path.expanduser(str(path)))
    return '"' + real.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_profile(config: Dict[str, Any], config_dir: Path) -> Optional[str]:
    """The sandbox profile for ``ui-diff run``, or None when no sandbox is configured.

    Everything is allowed except: reading or writing the ``deny`` paths, the
    GitHub App key's directory, the dashboard database and the review ledger;
    writing the service tree (the review work directory and the ui-diff caches
    excepted), the ui-diff install and the ``deny_write`` paths. Later rules
    win in a profile, so the two write exceptions come last.
    """
    settings = ui_diff_block(config)
    block = settings.get("sandbox")
    if not isinstance(block, dict):
        return None
    service_root = Path(config_dir).resolve().parent
    deny: List[Any] = list(block.get("deny") or [])
    app = config.get("github_app")
    if isinstance(app, dict) and app.get("private_key_path"):
        deny.append(Path(str(app["private_key_path"])).parent)
    deny.append(service_root / "data" / "dashboard")
    state = Path(os.path.expanduser(str(config.get("state_path") or "data/state.json")))
    deny.append(state if state.is_absolute() else service_root / state)
    deny_write: List[Any] = [service_root] + list(block.get("deny_write") or [])
    for arg in settings.get("command") or []:
        script = Path(str(arg))
        if script.suffix in (".mjs", ".js") and script.is_absolute() and len(script.parents) > 2:
            deny_write.append(script.parents[1])  # the skill directory: bin/ and dist/ beside it
    work = Path(os.path.expanduser(str(config.get("work_dir") or "data/work")))
    allow_write = [work if work.is_absolute() else service_root / work, cache_root(config, config_dir)]
    lines = ["(version 1)", "(allow default)"]
    lines += [f"(deny file-read* file-write* (subpath {_sbpl(path)}))" for path in deny]
    lines += [f"(deny file-write* (subpath {_sbpl(path)}))" for path in deny_write]
    lines += [f"(allow file-write* (subpath {_sbpl(path)}))" for path in allow_write]
    return "\n".join(lines) + "\n"


def run_environ(config: Dict[str, Any], config_dir: Path, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The environment ``ui-diff run`` gets: no credential, a private HOME and caches.

    The PR's install scripts and dev server run under it, so nothing from the
    daemon's own environment is passed through except what the tools need to
    start: PATH (with the configured node's directory first), the locale and the
    temp directory.
    """
    source = os.environ if base is None else base
    settings = ui_diff_block(config)
    root = cache_root(config, config_dir)
    for name in ("home", "npm-cache", "ms-playwright"):
        (root / name).mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(root / name), 0o700)
        except OSError:
            pass
    command = settings.get("command") or []
    path_entries: List[str] = []
    first = str(command[0]) if command else ""
    if os.path.isabs(first):
        path_entries.append(os.path.dirname(first))
    for entry in str(source.get("PATH") or "").split(os.pathsep) + ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]:
        if entry and entry not in path_entries:
            path_entries.append(entry)
    env = {
        "PATH": os.pathsep.join(path_entries),
        "HOME": str(root / "home"),
        "npm_config_cache": str(root / "npm-cache"),
        "PLAYWRIGHT_BROWSERS_PATH": str(root / "ms-playwright"),
        "LANG": source.get("LANG") or "en_US.UTF-8",
        "TMPDIR": source.get("TMPDIR") or "/tmp",
        "CI": "1",
        "NO_COLOR": "1",
        # The worktrees ui-diff makes must not pick up the host's git config
        # (url rewrites, credential helpers of other tenants).
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    for name, value in (settings.get("env") or {}).items():
        env[str(name)] = str(value)
    return env


def _tool(name: str, *candidates: str) -> str:
    """An absolute path for a system tool: launchd gives the daemon a short PATH."""
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which(name) or name


def _descendant_groups(root_pid: int) -> List[int]:
    """The process groups of every descendant of ``root_pid`` (ps), best effort.

    ui-diff starts installs and dev servers detached, each in its own group, so
    killing ui-diff's own group does not reach them.
    """
    try:
        listing = subprocess.run(
            [_tool("ps", "/bin/ps", "/usr/bin/ps"), "-axo", "pid=,ppid=,pgid="],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    children: Dict[int, List[Tuple[int, int]]] = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            continue
        pid, ppid, pgid = (int(part) for part in parts)
        children.setdefault(ppid, []).append((pid, pgid))
    groups: List[int] = []
    stack = [root_pid]
    seen = set()
    while stack:
        parent = stack.pop()
        for pid, pgid in children.get(parent, []):
            if pid in seen:
                continue
            seen.add(pid)
            stack.append(pid)
            if pgid > 1 and pgid not in groups:
                groups.append(pgid)
    return groups


def _groups_working_under(paths: List[str]) -> List[int]:
    """The process groups of this user's processes whose working directory is
    inside one of ``paths`` (lsof), best effort.

    A detached dev server that outlived ui-diff has been reparented, so the
    process tree no longer leads to it; its working directory (a worktree in
    the run's private temp directory, or the clone) still does.
    """
    roots = [os.path.realpath(path).rstrip(os.sep) + os.sep for path in paths if path]
    if not roots:
        return []
    try:
        listing = subprocess.run(
            [_tool("lsof", "/usr/sbin/lsof", "/usr/bin/lsof"), "-a", "-u", str(os.getuid()), "-d", "cwd", "-F", "pn"],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    groups: List[int] = []
    own = os.getpid()
    pid = 0
    for line in listing.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid and pid != own:
            cwd = os.path.realpath(line[1:]).rstrip(os.sep) + os.sep
            if any(cwd.startswith(root) for root in roots):
                try:
                    group = os.getpgid(pid)
                except OSError:
                    continue
                if group > 1 and group not in groups:
                    groups.append(group)
    return groups


def _kill_groups(groups: List[int], sig: int) -> None:
    for group in groups:
        try:
            os.killpg(group, sig)
        except OSError:
            pass


#: How often a running ui-diff is checked: its descendants recorded, a cancel noticed.
POLL_SECONDS = 2.0
#: How long ui-diff gets after SIGTERM to stop its own servers, before SIGKILL.
TERM_GRACE_SECONDS = 20.0


def _run_group(
    args: List[str],
    cwd: Path,
    env: Dict[str, str],
    timeout: int,
    cancel: Optional[threading.Event] = None,
    sweep: Optional[List[str]] = None,
) -> Tuple[Optional[int], str, str]:
    """Run ``args`` in its own session and leave nothing of it running.

    ui-diff starts installs and dev servers detached, each in its own process
    group, and once ui-diff exits they are reparented and can no longer be told
    apart. So while it runs, every descendant's group is recorded; when it ends —
    normally, past ``timeout`` or on ``cancel`` — those groups, its own group and
    the group of every process still working under ``sweep`` (a child that
    detached between two polls) get SIGKILL. A run that has to be stopped first
    gets SIGTERM (ui-diff then stops its own servers). Output goes to files, not
    pipes, so a child that inherits them cannot hold the call open.
    ``(exit, stdout, stderr)``, exit None when stopped.
    """
    if cancel is not None and cancel.is_set():
        return None, "", ""
    with tempfile.TemporaryFile() as out_file, tempfile.TemporaryFile() as err_file:
        proc = subprocess.Popen(
            args,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out_file,
            stderr=err_file,
            start_new_session=True,
        )
        groups: List[int] = []
        deadline = time.monotonic() + timeout
        stopped = False
        while True:
            try:
                proc.wait(timeout=POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                groups += [group for group in _descendant_groups(proc.pid) if group not in groups]
                if time.monotonic() >= deadline or (cancel is not None and cancel.is_set()):
                    stopped = True
                    break
        if stopped:
            groups += [group for group in _descendant_groups(proc.pid) if group not in groups]
            _kill_groups([proc.pid], signal.SIGTERM)
            try:
                proc.wait(timeout=TERM_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                groups += [group for group in _descendant_groups(proc.pid) if group not in groups]
                _kill_groups([proc.pid], signal.SIGKILL)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        # The sweep: anything ui-diff left behind (a dev server it did not stop),
        # found by the tree it had and by where it works.
        groups += [group for group in [proc.pid] + _groups_working_under(list(sweep or [])) if group not in groups]
        _kill_groups([group for group in groups if group != os.getpgid(0)], signal.SIGKILL)
        out = _tail(out_file)
        err = _tail(err_file)
    return (None if stopped else proc.returncode), out, err


#: The most of a step's stdout or stderr kept (its end, where the result and the error are).
MAX_OUTPUT_BYTES = 1024 * 1024


def _tail(handle: Any) -> str:
    size = handle.seek(0, os.SEEK_END)
    handle.seek(max(0, size - MAX_OUTPUT_BYTES))
    return handle.read().decode("utf-8", "replace")


def _last_json(text: str) -> Dict[str, Any]:
    """The JSON object ui-diff printed on stdout (the last one that parses), or {}."""
    stripped = str(text or "").strip()
    if not stripped:
        return {}
    try:
        value = json.loads(stripped)
        return value if isinstance(value, dict) else {}
    except ValueError:
        pass
    start = stripped.rfind("\n{")
    while start >= 0:
        try:
            value = json.loads(stripped[start + 1:])
            return value if isinstance(value, dict) else {}
        except ValueError:
            start = stripped.rfind("\n{", 0, start)
    return {}


def _one_line(text: str, limit: int = 300) -> str:
    line = " ".join(str(text or "").split())
    line = line.replace("`", "'")
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _failure_reason(
    code: Optional[int], stdout: str, stderr: str, timeout: int, cancel: Optional[threading.Event] = None
) -> str:
    if code is None:
        if cancel is not None and cancel.is_set():
            return "stopped: the review could not wait for it any longer"
        return f"timed out after {timeout} seconds"
    parsed = _last_json(stdout)
    message = parsed.get("error") or parsed.get("message")
    if isinstance(message, dict):
        message = message.get("message")
    if message:
        hint = parsed.get("hint")
        return _one_line(f"{message}" + (f" ({hint})" if isinstance(hint, str) and hint else ""))
    lines = [line for line in str(stderr or "").splitlines() if line.strip()]
    errors = [line for line in lines if "error" in line.lower()]
    return _one_line((errors or lines or [f"exit {code}"])[-1])


#: The largest repository ui-diff.config.json read.
MAX_CONFIG_BYTES = 256 * 1024


def config_file_for(
    config: Dict[str, Any], source_dir: Path, repo: str
) -> Tuple[Optional[Path], str, Dict[str, Any]]:
    """The ui-diff config this run uses, where it came from, and its contents.

    The repository's own ``ui-diff.config.json`` wins; otherwise the one in
    ``visual_outline.ui_diff.configs`` is written to the clone root (untracked),
    so its relative paths mean what they would in the repository. The contents
    are read here, before the PR's code runs, and are what publish gets later.
    """
    own = Path(source_dir) / CONFIG_FILENAME
    try:
        info = os.lstat(str(own))
    except OSError:
        info = None
    if info is not None:
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
            raise ValueError(f"the repository's {CONFIG_FILENAME} is not a regular file under {MAX_CONFIG_BYTES} bytes")
        settings = json.loads(own.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            raise ValueError(f"the repository's {CONFIG_FILENAME} is not a JSON object")
        return own, "repository", settings
    supplied = _lookup(ui_diff_block(config).get("configs"), repo)
    if supplied is None:
        return None, "", {}
    payload = {key: value for key, value in supplied.items() if not str(key).startswith("_")}
    own.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return own, "config", payload


def _harden_git(env: Dict[str, str]) -> Dict[str, str]:
    """``env`` with git hooks switched off, added to whatever GIT_CONFIG_* it carries."""
    hardened = dict(env)
    try:
        count = int(hardened.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        count = 0
    hardened[f"GIT_CONFIG_KEY_{count}"] = "core.hooksPath"
    hardened[f"GIT_CONFIG_VALUE_{count}"] = os.devnull
    hardened["GIT_CONFIG_COUNT"] = str(count + 1)
    return hardened


#: A file in .ui-diff larger than this is not copied for publishing (a screenshot never is).
MAX_PUBLISH_FILE_BYTES = 64 * 1024 * 1024
#: ... and the whole copy stays under these (a real run is a few dozen images).
MAX_PUBLISH_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_PUBLISH_FILES = 5000


def _publish_base(env: Dict[str, str], workspace: Path) -> Dict[str, str]:
    """The publish step's environment before the credential: a HOME of its own.

    The run step's HOME and caches were writable by the PR's code (an
    ``~/.npmrc`` planted there would reach npm if ui-diff rebuilt itself), so
    publish gets a fresh HOME inside its workspace and no npm user config.
    """
    home = workspace / ".home"
    home.mkdir(mode=0o700, exist_ok=True)
    base = {key: value for key, value in env.items() if key not in ("npm_config_cache", "PLAYWRIGHT_BROWSERS_PATH")}
    base.update({
        "HOME": str(home),
        "npm_config_userconfig": os.devnull,
        "npm_config_globalconfig": os.devnull,
        "npm_config_cache": str(home / "npm-cache"),
    })
    return base


def publish_workspace(out_dir: Path, settings: Dict[str, Any], env: Dict[str, str]) -> Path:
    """A fresh repository for ``ui-diff publish``, outside the clone the PR's code ran in.

    The PR's install scripts could write the clone's ``.git`` (hooks, config)
    and ``.ui-diff`` (symlinks); publish holds the token. So it runs in a new
    ``git init`` with a copy of ``.ui-diff`` that keeps regular files only (no
    symlink, FIFO or oversized file), and ``settings`` — the ui-diff config as
    read before the PR's code ran — without its ``publish`` block (the daemon
    passes remote, branch and link base itself; a repository cannot allow a
    public upload).
    """
    workspace = Path(tempfile.mkdtemp(prefix="rol-ui-diff-publish-"))
    try:
        init = subprocess.run(
            ["git", "init", "-q", str(workspace)], env=env, capture_output=True, text=True, timeout=60, check=False
        )
        if init.returncode != 0:
            raise OSError(f"git init failed: {init.stderr.strip()[:200]}")
        target = workspace / ".ui-diff"
        source = Path(out_dir)
        # os.walk follows a symlink given as its top, whatever followlinks says.
        root = os.lstat(str(source))
        if not stat.S_ISDIR(root.st_mode):
            raise ValueError(".ui-diff is not a plain directory")
        total = 0
        count = 0
        for directory, subdirs, files in os.walk(str(source), followlinks=False):
            relative = Path(directory).relative_to(source)
            (target / relative).mkdir(parents=True, exist_ok=True)
            subdirs[:] = [name for name in subdirs if not os.path.islink(os.path.join(directory, name))]
            for name in files:
                path = os.path.join(directory, name)
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PUBLISH_FILE_BYTES:
                    continue
                total += info.st_size
                count += 1
                if total > MAX_PUBLISH_TOTAL_BYTES or count > MAX_PUBLISH_FILES:
                    raise ValueError(
                        f".ui-diff holds more than {MAX_PUBLISH_FILES} files or "
                        f"{MAX_PUBLISH_TOTAL_BYTES // (1024 * 1024)} MiB"
                    )
                shutil.copyfile(path, str(target / relative / name), follow_symlinks=False)
        copied = {key: value for key, value in settings.items() if key != "publish"}
        (workspace / CONFIG_FILENAME).write_text(json.dumps(copied, indent=2) + "\n", encoding="utf-8")
    except BaseException:
        shutil.rmtree(str(workspace), ignore_errors=True)
        raise
    return workspace


def run_stage(
    config: Dict[str, Any],
    config_dir: Path,
    source_dir: Path,
    artifact_dir: Path,
    repo: str,
    pr: Dict[str, Any],
    publish_env: Callable[[Dict[str, str]], Dict[str, str]],
    log: Callable[[str], None],
    write_text: Callable[[Path, str], None],
    upload: bool = True,
    cancel: Optional[threading.Event] = None,
) -> Outcome:
    """Capture, diff and publish one PR head; never raises for an expected failure.

    ``publish_env`` adds the GitHub credential to an environment (only the
    publish step gets it). ``write_text`` writes a private artifact file.
    ``upload=False`` (a dry run) renders text-only and pushes nothing.
    ``cancel`` (set by ``Job.cancel``) stops whatever runs and ends the stage.
    """
    clock = time.monotonic
    started = clock()
    settings = ui_diff_block(config)
    number = int(pr.get("number") or 0)
    head_sha = str((pr.get("head") or {}).get("sha") or "")
    base_sha = str((pr.get("base") or {}).get("sha") or "")
    head_info = (pr.get("head") or {}).get("repo") or {}
    base_info = (pr.get("base") or {}).get("repo") or {}
    head_repo = head_info.get("full_name")
    # Only a head that GitHub places in this very repository: a fork's head, or
    # one whose fork was deleted (head.repo null), is someone else's code. The
    # ids decide when both are known (a renamed repository keeps its id).
    if head_info.get("id") and base_info.get("id"):
        same = head_info.get("id") == base_info.get("id")
    else:
        same = bool(head_repo) and _repo_key(head_repo) == _repo_key(repo)
    if not same:
        return Outcome(
            None, "skipped", f"the head is not in {repo} (head repository: {head_repo or 'deleted'}); ui-diff never runs it"
        )
    if not head_sha or not base_sha:
        return Outcome(None, "skipped", "the PR carries no base or head commit")
    try:
        config_path, config_source, config_settings = config_file_for(config, source_dir, repo)
    except (OSError, ValueError) as exc:
        reason = _one_line(f"could not read the ui-diff config: {exc}")
        return Outcome(failure_section(reason, head_sha, clock() - started), "failed", reason)
    if config_path is None:
        return Outcome(
            failure_section(
                f"no ui-diff config for {repo} (commit {CONFIG_FILENAME} to the repository, "
                "or add it to visual_outline.ui_diff.configs)",
                head_sha,
                clock() - started,
            ),
            "failed",
            "no ui-diff config",
        )
    command = [str(arg) for arg in settings.get("command") or []]
    out_dir = Path(source_dir) / ".ui-diff"
    env = run_environ(config, config_dir)
    profile = sandbox_profile(config, config_dir)
    if profile is not None and not os.access(SANDBOX_EXEC, os.X_OK):
        # Fail closed: a deployment that asked for the sandbox never runs the PR's code without it.
        reason = f"visual_outline.ui_diff.sandbox is set but {SANDBOX_EXEC} is not available on this host"
        return Outcome(failure_section(reason, head_sha, clock() - started), "failed", reason)
    run_prefix = [SANDBOX_EXEC, "-p", profile] if profile is not None else []
    # A private temp directory per run: ui-diff's worktrees land in it, so every
    # install and dev server this run started works under it and can be found.
    # mkdtemp + rmtree(ignore_errors), not TemporaryDirectory: on Python 3.9 its
    # cleanup raises on a file an install left read-only, which would turn a
    # finished UI diff into a failure.
    run_tmp = tempfile.mkdtemp(prefix="rol-ui-diff-run-")
    try:
        env["TMPDIR"] = run_tmp
        timeout = int(settings.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
        publish_timeout = int(settings.get("publish_timeout_seconds") or DEFAULT_PUBLISH_TIMEOUT_SECONDS)
        slots = _slots(int(settings.get("max_concurrent") or 1))
        log(f"{repo} pr #{number}: ui-diff waiting for a slot ({config_source} config)")
        # Bounded, so join_timeout always covers the wait plus this run: a review
        # never removes its clone under a UI diff that started late.
        if not _acquire(slots, _run_budget(settings), cancel):
            reason = "other UI diffs kept every slot busy for the whole wait; the next head tries again"
            return Outcome(failure_section(reason, head_sha, clock() - started), "failed", reason)
        try:
            log(f"{repo} pr #{number}: ui-diff capturing base {base_sha[:12]} and head {head_sha[:12]}")
            run_args = run_prefix + command + [
                "run", "--config", str(config_path), "--base", base_sha, "--head", head_sha, "--out", str(out_dir),
            ]
            code, stdout, stderr = _run_group(
                # Only the run's own temp directory: the reviewer agents work in the
                # clone at the same time, and must never be swept with it.
                run_args, Path(source_dir), env, timeout, cancel, sweep=[run_tmp]
            )
            write_text(artifact_dir / "ui-diff.run.stdout.txt", stdout)
            write_text(artifact_dir / "ui-diff.run.stderr.txt", stderr)
            if code != 0:
                reason = _failure_reason(code, stdout, stderr, timeout, cancel)
                return Outcome(failure_section(reason, head_sha, clock() - started), "failed", reason)
            notes: List[str] = []
            branch = str(settings.get("assets_branch") or DEFAULT_ASSETS_BRANCH)
            try:
                workspace = publish_workspace(out_dir, config_settings, env)
            except (OSError, ValueError) as exc:
                reason = _one_line(f"could not prepare the publish step: {exc}")
                return Outcome(failure_section(reason, head_sha, clock() - started), "failed", reason)
            published = workspace / ".ui-diff"
            publish_args = command + [
                "publish", "--config", str(workspace / CONFIG_FILENAME), "--out", str(published), "--pr", str(number),
                "--no-comment",
                # Pinned here so a repository's own config cannot point the upload
                # (and the credential) or the image links anywhere else.
                "--repo", repo, "--remote", f"https://github.com/{repo}.git", "--branch", branch,
                "--link-base", f"https://github.com/{repo}/raw/{branch}",
            ]
            try:
                status = "ok"
                if not upload:
                    status = "text-only"
                    notes.append("Dry run: screenshots were not uploaded.")
                elif settings.get("upload", True):
                    code, stdout, stderr = _run_group(
                        publish_args, workspace, _harden_git(publish_env(_publish_base(env, workspace))),
                        publish_timeout, cancel, sweep=[str(workspace)],
                    )
                    write_text(artifact_dir / "ui-diff.publish.stdout.txt", stdout)
                    write_text(artifact_dir / "ui-diff.publish.stderr.txt", stderr)
                    if code != 0:
                        reason = _failure_reason(code, stdout, stderr, publish_timeout, cancel)
                        if code == EXIT_GIT_ACCESS:
                            reason = (
                                f"the GitHub App could not push to the {branch} branch of {repo} "
                                f"(it needs the Contents: write permission; ui-diff said: {reason})"
                            )
                        notes.append(f"Screenshots were not uploaded: {reason}.")
                        status = "text-only"
                else:
                    status = "text-only"
                    notes.append("Screenshots are not uploaded on this deployment (visual_outline.ui_diff.upload is off).")
                if status == "text-only":
                    code, stdout, stderr = _run_group(
                        publish_args + ["--no-upload"], workspace, _harden_git(_publish_base(env, workspace)),
                        publish_timeout, cancel, sweep=[str(workspace)],
                    )
                    write_text(artifact_dir / "ui-diff.render.stderr.txt", stderr)
                    if code != 0:
                        reason = _failure_reason(code, stdout, stderr, publish_timeout, cancel)
                        return Outcome(failure_section(reason, head_sha, clock() - started), "failed", reason)
                try:
                    markdown = (published / "comment.md").read_text(encoding="utf-8")
                except OSError as exc:
                    reason = f"ui-diff wrote no comment ({exc.__class__.__name__})"
                    return Outcome(failure_section(reason, head_sha, clock() - started), "failed", reason)
                report = published / "report.json"
                if report.is_file():
                    try:
                        write_text(artifact_dir / "ui-diff.report.json", report.read_text(encoding="utf-8"))
                    except OSError:
                        pass
            finally:
                shutil.rmtree(str(workspace), ignore_errors=True)
        finally:
            slots.release()
        write_text(artifact_dir / "ui-diff.comment.md", markdown)
        section = build_section(markdown, notes, head_sha, clock() - started)
        return Outcome(section, status, "; ".join(notes))
    finally:
        shutil.rmtree(run_tmp, ignore_errors=True)
        if os.path.exists(run_tmp):
            log(f"{repo} pr #{number}: ui-diff left {run_tmp} behind (could not remove it); remove it by hand")


#: How long a cancelled stage may take to stop: a poll, ui-diff's SIGTERM grace, the SIGKILL wait.
CANCEL_WAIT_SECONDS = POLL_SECONDS + TERM_GRACE_SECONDS + 10 + 30


class Job(object):
    """``run_stage`` on a thread, so the reviewers do not wait for the browser.

    ``target`` gets the job's cancel event. ``result`` joins it; ``cancel``
    stops it (its processes included); whatever escapes the stage becomes a
    failure outcome.
    """

    def __init__(self, target: Callable[[threading.Event], Outcome], head_sha: str) -> None:
        self._target = target
        self._head_sha = head_sha
        self._started = time.monotonic()
        self._outcome: Optional[Outcome] = None
        self._cancel = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ui-diff", daemon=True)

    def start(self) -> "Job":
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            self._outcome = self._target(self._cancel)
        except Exception as exc:  # noqa: BLE001 - the stage is best effort
            reason = _one_line(f"{exc.__class__.__name__}: {exc}")
            self._outcome = Outcome(failure_section(reason, self._head_sha, time.monotonic() - self._started), "failed", reason)

    def result(self, timeout: float) -> Optional[Outcome]:
        """The outcome, or None when the thread is still running after ``timeout``."""
        self._thread.join(timeout)
        if self._thread.is_alive():
            return None
        return self._outcome

    def cancel(self, timeout: float = CANCEL_WAIT_SECONDS) -> bool:
        """Stop the stage and wait for it; True once the thread has ended."""
        self._cancel.set()
        self._thread.join(timeout)
        return not self._thread.is_alive()



def _run_budget(settings: Dict[str, Any]) -> float:
    """The longest one UI diff can take: the run and two publish steps, each with its kill grace."""
    timeout = int(settings.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    publish = int(settings.get("publish_timeout_seconds") or DEFAULT_PUBLISH_TIMEOUT_SECONDS)
    return float(timeout + 2 * publish + 3 * 30)


def join_timeout(config: Dict[str, Any]) -> float:
    """How long the review waits for its UI diff: the bounded slot wait plus its own run."""
    return 2 * _run_budget(ui_diff_block(config)) + 60
