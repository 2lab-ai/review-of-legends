#!/usr/bin/env python3
"""Install, verify and retire the admin dashboard web job alongside the daemon.

Why a second launchd job instead of a thread inside the review daemon: the
dashboard needs third-party packages (Starlette/Uvicorn/Authlib) while the
review daemon must stay stdlib-only under ``/usr/bin/python3``
(service.sh:105-113). Mixing them would either force the daemon onto a venv it
does not need or force the web app to vendor a server. So the daemon keeps
``/usr/bin/python3`` and the dashboard runs from its own immutable per-version
virtualenv created *outside* the rsynced tree -- ``rsync -a --delete`` in
.github/workflows/deploy.yml only preserves ``data/`` and ``logs/``, so a venv
inside the deployed directory would be deleted mid-flight on every deploy.

This script is stdlib-only and must stay runnable by macOS ``/usr/bin/python3``
(3.9): it is what *creates* the newer interpreter's venv, so it cannot depend on
it. No network access except ``pip install -r requirements-dashboard.txt``.

Subcommands (all take ``--root``, the deployed service tree):

  preflight  request a drain and wait for the executor to pause (never kills it)
  install    venv + settings defaults + launchd job, then kickstart
  verify     dashboard job running, /healthz ok, API closed to anonymous callers
  resume     clear the drain request so the daemon reviews again
  start / stop / uninstall  lifecycle used by service.sh

Provider credentials are never written or printed by this script: it merges only
the non-secret ``public_url``/``config_path`` keys into the external settings
file and leaves every operator-provisioned credential exactly as it found it.
The single exception is ``session_secret`` -- a server-owned random value with no
external party, generated once if absent and never overwritten (see
``merge_settings``).

A missing *Google* credential never makes this script skip the dashboard: the
job is installed and coupled to the deploy either way. It does make the deploy
fail, loudly and by name, because dashboard_web.main returns 2 on incomplete
settings (dashboard_web.py:1064-1069) and the job therefore cannot serve
``/healthz`` at all. Accept that state explicitly with ``--allow-unconfigured``
(or ``ROL_DASHBOARD_ALLOW_UNCONFIGURED=1``) if the dashboard must ship before
the Google client is provisioned.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import plistlib
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Deployment settings shared with service.sh: config/deploy.env under --root
#: (KEY=VALUE lines, optional) names the launchd labels and the dashboard's
#: access policy. A variable set (non-empty) in the environment wins over the file.
DEPLOY_ENV_NAME = "config/deploy.env"
DEPLOY_ENV_KEYS = (
    "ROL_SERVICE_LABEL",
    "ROL_DASHBOARD_LABEL",
    "ROL_DASHBOARD_OWNER",
    "ROL_DASHBOARD_VIEWER_DOMAINS",
    "ROL_DASHBOARD_PUBLIC_URL",
)
DEFAULT_DAEMON_LABEL = "ai.2lab.review-of-legends"
DAEMON_LABEL = os.environ.get("ROL_SERVICE_LABEL") or DEFAULT_DAEMON_LABEL
LABEL = os.environ.get("ROL_DASHBOARD_LABEL") or DAEMON_LABEL + ".dashboard"

#: The web app binds this address itself (api-contract.md "Bind fixed
#: loopback"); nothing here passes a host/port, we only probe the same address.
BIND_HOST = "127.0.0.1"
BIND_PORT = 8840
BASE_URL = "http://%s:%d" % (BIND_HOST, BIND_PORT)

MODULE = "code_review_service.dashboard_web"
REQUIREMENTS_NAME = "requirements-dashboard.txt"
CANONICAL_PUBLIC_URL = os.environ.get("ROL_DASHBOARD_PUBLIC_URL", "").rstrip("/")

# --------------------------------------------------------------------------
# Drain handshake, owned behaviourally by dashboard_execution (W2) and driven
# from here. The daemon holds the executor lock while any review runs and
# releases it when none does, so "the lock happens to be free" is not by itself
# a deploy window and elapsed time says nothing at all about liveness. The
# sequence is:
#
#   1. the deploy creates the pause sentinel (dashboard_execution
#      .executor_pause_path, ``data/executor.pause`` in the packaged layout).
#      It lives under data/, which ``rsync --delete`` preserves, so it survives
#      the tree swap and the reinstall;
#   2. the daemon's scheduler (code_review_service/scheduler.py) checks that
#      sentinel before it starts any review or claims any retry, both before
#      and right after taking the executor lock; a retry still waiting for the
#      same PR's review goes back to the queue instead of starting. ``once``
#      checks it between repositories and PRs. While it exists no new work
#      starts, so there is no release-then-reacquire window behind our back;
#   3. the deploy waits for the lock to be observably free
#      (dashboard_execution.probe_executor_lock) -- which happens only when
#      every review in flight has finished on its own -- and confirms it twice;
#   4. the last workflow step removes the sentinel again (``resume``), which it
#      does even when the deploy failed.
#
# Nothing here kills a review, and no attempt is ever declared dead: liveness
# comes from the flock, the holder PID and the daemon's launchd state only.
# Paths are asked of the *live* tree rather than hardcoded; these constants are
# only the fallback for a deployed tree that predates the contract -- and such a
# tree is deployed over ONLY when launchctl positively reports the daemon as not
# loaded, because an undrainable daemon plus an unknown state is exactly the
# "force interrupt" the user forbade.
EXECUTOR_LOCK_RELPATH = "data/executor.lock"
EXECUTOR_PAUSE_RELPATH = "data/executor.pause"

#: Only these keys are ever written by the installer. Everything else in the
#: settings file (google_client_id/secret, llmux_api_key, llmux_base_url, ...)
#: is operator-owned and preserved verbatim.
#:
#: ``store_path`` is deliberately NOT managed here: dashboard_web.resolve_paths
#: (dashboard_web.py:489-513) derives the attempt database from ``config_path``
#: via dashboard_execution.dashboard_db_path, which is the same value the daemon
#: writes. Pinning it from the installer would let the web tier read a different
#: file than the daemon writes the moment either default moves.
MANAGED_SETTING_KEYS = ("public_url", "config_path")
#: Server-owned secret with no external counterparty: it only signs this
#: deployment's own cookies, so generating it once is safe (and the alternative
#: is a dashboard that cannot hold a session at all). Never regenerated --
#: rewriting it would silently invalidate every live admin session.
GENERATED_SETTING_KEYS = ("session_secret",)
#: Credentials that belong to Google and can only come from the operator.
#: Missing ones are reported by name only (api-contract.md: only non-secret
#: setting *names* may appear in errors) and never invented.
AUTH_SETTING_KEYS = ("google_client_id", "google_client_secret")

#: Mirrors dashboard_web.PUBLIC_SETTINGS / SECRET_SETTINGS (dashboard_web.py:82-86).
#: Duplicated rather than imported: that module needs Starlette, this script must
#: run on stock /usr/bin/python3 before any venv exists. Only the non-secret
#: names are ever printed; secrets are reported as a count.
REQUIRED_PUBLIC_SETTINGS = ("public_url", "google_client_id", "llmux_base_url", "config_path")
REQUIRED_SECRET_SETTINGS = ("google_client_secret", "session_secret", "llmux_api_key")

#: Newest interpreter this deployment has been exercised against. Anything newer
#: is still accepted (with a warning) so a Homebrew bump cannot wedge deploys,
#: but it is not *preferred*, because wheels for the pinned dashboard
#: dependencies may not exist yet for a just-released CPython.
PREFERRED_MAX_MINOR = 13
MIN_PYTHON = (3, 10)
PYTHON_SEARCH_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/homebrew/opt/python@3.12/bin")

#: Per-version venvs: each one is the exact runtime a deploy was verified
#: against, so a rollback to it needs no network. They used to accumulate
#: forever (one per deploy, without bound).
#: Once the new job is confirmed running, the installer keeps the venv it just
#: installed, the one the job ran before this deploy (its plist, what launchd
#: had loaded, and the last confirmed-running record), and the newest
#: VENV_KEEP_PREVIOUS others, and removes the rest. When neither launchd nor the
#: record says what ran before, it removes none that deploy.
VENV_MARKER = ".rol-install-complete"
VENV_KEEP_PREVIOUS = 5
#: Under the venv root: the venv name the last confirmed-running install ran.
#: ``install --no-launchctl`` rewrites the plist without restarting the job,
#: so the plist alone does not say what launchd last ran.
LAST_RUNNING_FILE = ".last-running"

LAUNCHD_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

EXIT_BUSY = 3


# --------------------------------------------------------------------- helpers


def info(message: str) -> None:
    print("[dashboard] %s" % message, flush=True)


def warn(message: str) -> None:
    print("[dashboard][warn] %s" % message, flush=True)


def fail(message: str) -> "NoReturn":  # type: ignore[valid-type]
    print("[dashboard][error] %s" % message, file=sys.stderr, flush=True)
    raise SystemExit(1)


def run(cmd: Sequence[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """Thin ``subprocess.run`` wrapper so tests can patch one seam."""
    return subprocess.run(
        list(cmd),
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def home() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home()))


def allow_unconfigured(args: argparse.Namespace) -> bool:
    """Opt-out for a deploy that runs before the Google client exists.

    Reachable as an env var because the deploy calls ``install.sh`` (which
    forwards no arguments). It is never silent: every use is logged.
    """
    if getattr(args, "allow_unconfigured", False):
        return True
    if os.environ.get("ROL_DASHBOARD_ALLOW_UNCONFIGURED", "").strip() in ("1", "true", "yes"):
        warn("ROL_DASHBOARD_ALLOW_UNCONFIGURED is set: an unprovisioned dashboard will not fail the deploy")
        return True
    return False


def settings_path(args: argparse.Namespace) -> Path:
    explicit = getattr(args, "settings", None) or os.environ.get("ROL_DASHBOARD_SETTINGS")
    if explicit:
        return Path(explicit).expanduser()
    return home() / "review-of-legends" / "dashboard-secrets.json"


def venv_root(args: argparse.Namespace) -> Path:
    explicit = getattr(args, "venv_root", None) or os.environ.get("ROL_DASHBOARD_VENV_ROOT")
    if explicit:
        return Path(explicit).expanduser()
    return home() / "review-of-legends" / "venvs"


def launch_agents_dir() -> Path:
    return home() / "Library" / "LaunchAgents"


def plist_path() -> Path:
    return launch_agents_dir() / ("%s.plist" % LABEL)


def domain() -> str:
    return "gui/%d" % os.getuid()


def read_version(root: Path) -> str:
    """Version string used as the immutable venv directory name."""
    version_file = root / "VERSION"
    raw = ""
    if version_file.is_file():
        raw = version_file.read_text(encoding="utf-8").strip()
    if not raw:
        raw = "dev-local"
    # The value names a directory under $HOME; refuse anything that could climb
    # out of it instead of quietly sanitising a hostile string.
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", raw) or raw.startswith("."):
        fail("VERSION %r is not a safe directory name ([A-Za-z0-9._-], no leading dot)" % raw)
    return raw


# ---------------------------------------------------------------- interpreter


def python_candidates() -> List[Path]:
    names = ["python3.%d" % minor for minor in range(20, MIN_PYTHON[1] - 1, -1)]
    names.append("python3")
    found: List[Path] = []
    for directory in PYTHON_SEARCH_DIRS:
        for name in names:
            candidate = Path(directory) / name
            if candidate.exists() and candidate not in found:
                found.append(candidate)
    return found


def interpreter_version(executable: Path) -> Optional[Tuple[int, int]]:
    try:
        completed = run(
            [str(executable), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            check=True,
            capture=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (completed.stdout or "").strip().splitlines()
    if not text:
        return None
    match = re.fullmatch(r"(\d+)\.(\d+)", text[-1].strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)))


def select_python(explicit: Optional[str] = None) -> Path:
    """Newest supported Homebrew interpreter, preferring a verified minor.

    ``/usr/bin/python3`` is deliberately not a candidate: macOS ships 3.9 there
    and the dashboard runtime requires >=3.10 (api-contract.md).
    """
    if explicit:
        chosen = Path(explicit).expanduser()
        version = interpreter_version(chosen)
        if version is None:
            fail("--python %s is not executable / did not report a version" % chosen)
        if version < MIN_PYTHON:
            fail("--python %s is %d.%d; dashboard requires >=%d.%d" % (chosen, version[0], version[1], MIN_PYTHON[0], MIN_PYTHON[1]))
        return chosen

    supported: List[Tuple[Tuple[int, int], Path]] = []
    seen: set = set()
    for candidate in python_candidates():
        version = interpreter_version(candidate)
        if version is None or version < MIN_PYTHON or version in seen:
            continue
        seen.add(version)
        supported.append((version, candidate))
    if not supported:
        fail(
            "no Python >=%d.%d found in %s (install one: brew install python@3.12). "
            "/usr/bin/python3 is 3.9 and cannot run the dashboard."
            % (MIN_PYTHON[0], MIN_PYTHON[1], ", ".join(PYTHON_SEARCH_DIRS))
        )
    verified = [item for item in supported if item[0][1] <= PREFERRED_MAX_MINOR]
    if verified:
        version, executable = max(verified)
    else:
        version, executable = min(supported)
        warn(
            "only Python %d.%d found (newer than verified 3.%d); pinned wheels may be missing"
            % (version[0], version[1], PREFERRED_MAX_MINOR)
        )
    info("interpreter: %s (%d.%d)" % (executable, version[0], version[1]))
    return executable


# ----------------------------------------------------------------------- venv


def venv_python(venv_dir: Path) -> Path:
    return venv_dir / "bin" / "python"


def pip_install_args(requirements: Path) -> List[str]:
    """Arguments after ``<venv python> -m pip`` that install ``requirements``.

    The single definition of the deploy's pip flags: ensure_venv() runs them and
    the "Test dashboard web tier" step in deploy.yml asks this function for them,
    so CI installs exactly what a deploy installs. ``--require-hashes`` is added
    only when the file carries hashes, which pip would otherwise reject.
    """
    args = ["install"]
    if "--hash=" in requirements.read_text(encoding="utf-8"):
        args.append("--require-hashes")
    args += [
        "--no-input",
        "--disable-pip-version-check",
        "--upgrade-strategy",
        "only-if-needed",
        "-r",
        str(requirements),
    ]
    return args


def ensure_venv(root: Path, version: str, base_dir: Path, python: Path) -> Path:
    """Create (or reuse) the immutable venv for ``version`` and install deps."""
    requirements = root / REQUIREMENTS_NAME
    if not requirements.is_file():
        fail(
            "%s is missing from the deployed tree; the dashboard requirements and web module "
            "must ship in the same bundle as the daemon (deploy.yml tar list)" % requirements
        )

    base_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(str(base_dir), 0o700)
    venv_dir = base_dir / version
    marker = venv_dir / VENV_MARKER
    if marker.is_file() and venv_python(venv_dir).exists():
        info("reusing venv %s" % venv_dir)
        try:
            # A redeploy (a rollback) of an older version counts as recent for
            # prune_venvs, which keeps the newest by mtime.
            os.utime(str(venv_dir), None)
        except OSError:
            pass
        return venv_dir
    if venv_dir.exists():
        # A previous attempt died halfway; a half-populated venv is worse than
        # no venv because pip would "succeed" against a broken interpreter.
        info("discarding incomplete venv %s" % venv_dir)
        shutil.rmtree(str(venv_dir))

    info("creating venv %s" % venv_dir)
    run([str(python), "-m", "venv", str(venv_dir)])
    run([str(venv_python(venv_dir)), "-m", "pip"] + pip_install_args(requirements))
    marker.write_text("%s\n" % version, encoding="utf-8")
    return venv_dir


def plist_venv(destination: Path) -> Optional[Path]:
    """The venv the installed job runs (``<venv>/bin/python`` in its plist), if readable."""
    try:
        with destination.open("rb") as handle:
            spec = plistlib.load(handle)
        python = Path(str(spec["ProgramArguments"][0]))
    except Exception:  # noqa: BLE001 - no job yet, or a plist this installer did not write
        return None
    return python.parent.parent if python.parent.name == "bin" else None


def loaded_venv(label: str) -> Optional[Path]:
    """The venv of the job launchd has loaded now (``program = <venv>/bin/python`` in
    ``launchctl print``), or None when there is none or it cannot be read. Read
    before the plist is rewritten: ``install --no-launchctl`` changes the plist
    but not what launchd runs."""
    completed = run(["launchctl", "print", "%s/%s" % (domain(), label)], check=False, capture=True)
    if completed.returncode != 0:
        return None
    for line in (completed.stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("program = "):
            python = Path(stripped[len("program = "):].strip())
            return python.parent.parent if python.parent.name == "bin" else None
    return None


def last_running_venv(base_dir: Path) -> Optional[Path]:
    """The venv a confirmed-running install recorded, if the record is readable and sane."""
    try:
        name = (base_dir / LAST_RUNNING_FILE).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not name or "/" in name or name in (".", ".."):
        return None
    return base_dir / name


def record_running_venv(base_dir: Path, venv_dir: Path) -> None:
    """Best-effort: a failed record only makes the next prune keep less, never fails a deploy."""
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(base_dir), prefix=".last-running.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(venv_dir.name + "\n")
            os.replace(tmp_name, str(base_dir / LAST_RUNNING_FILE))
        except BaseException:
            os.unlink(tmp_name)
            raise
    except OSError as exc:
        warn("could not record the running venv in %s (%s)" % (base_dir, exc))


def prune_venvs(base_dir: Path, keep: Sequence[Path], keep_previous: int = VENV_KEEP_PREVIOUS) -> List[str]:
    """Remove per-version venvs under ``base_dir`` except ``keep`` and the newest
    ``keep_previous`` others; returns the names removed.

    Only venvs are candidates: real directories directly under ``base_dir``
    holding a ``pyvenv.cfg``. A symlink, a file, or a directory an operator put
    there is never touched, and ``shutil.rmtree`` unlinks symlinks inside a
    venv (``bin/python``) without following them. A venv that cannot be
    removed is logged and skipped: cleanup never fails a deploy.
    """
    keep_names = {Path(path).name for path in keep if path is not None}
    try:
        entries = [entry for entry in base_dir.iterdir()]
    except OSError as exc:
        warn("venv cleanup skipped: cannot list %s (%s)" % (base_dir, exc))
        return []
    candidates = []
    for entry in entries:
        try:
            if entry.name in keep_names or entry.is_symlink() or not entry.is_dir():
                continue
            marker = entry / "pyvenv.cfg"
            if marker.is_symlink() or not marker.is_file():
                continue
            candidates.append((entry.stat().st_mtime, entry))
        except OSError:
            continue
    candidates.sort(key=lambda item: item[0], reverse=True)
    removed: List[str] = []
    for _, entry in candidates[keep_previous:]:
        try:
            shutil.rmtree(str(entry))
        except OSError as exc:
            warn("venv cleanup: could not remove %s (%s); it stays" % (entry, exc))
            continue
        removed.append(entry.name)
    if removed:
        info("removed %d old venv(s): %s" % (len(removed), ", ".join(sorted(removed))))
    return removed


# ------------------------------------------------------------------- settings


def merge_settings(path: Path, managed: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Atomically merge non-secret defaults, preserving operator credentials.

    Returns ``(settings, missing_google_keys)``. ``public_url`` and
    ``config_path`` (``MANAGED_SETTING_KEYS``) are always refreshed:
    ``public_url`` to the canonical origin, the only one the dashboard accepts,
    with a warning when that replaces another value; ``config_path`` because it
    is deploy-derived. ``store_path`` is not managed: an operator's value is
    preserved like any other key.
    ``session_secret`` is generated once when absent -- it is server-owned, has
    no external counterparty, and is never regenerated or logged. Every other
    key, including all Google/llmux credentials, is copied through untouched and
    no value is ever printed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(path.parent), 0o700)
    except OSError:
        pass

    current: Dict[str, Any] = {}
    if path.is_file():
        raw = path.read_text(encoding="utf-8")
        if raw.strip():
            try:
                loaded = json.loads(raw)
            except ValueError as exc:
                # Never overwrite a file we cannot parse: it holds the only copy
                # of the operator's client secret.
                fail("%s is not valid JSON (%s); refusing to rewrite it" % (path, exc.__class__.__name__))
            if not isinstance(loaded, dict):
                fail("%s must contain a JSON object" % path)
            current = loaded

    merged = dict(current)
    for key in MANAGED_SETTING_KEYS:
        if key not in managed:
            continue
        # public_url is refreshed, not defaulted: dashboard_web.validate_settings
        # (dashboard_web.py:473-476) rejects anything but the canonical origin, so
        # leaving a stale value in place would only crash-loop the job.
        if key == "public_url" and current.get(key) not in (None, "", managed[key]):
            warn("public_url in %s replaced with the canonical origin %s" % (path, managed[key]))
        merged[key] = managed[key]

    generated: List[str] = []
    for key in GENERATED_SETTING_KEYS:
        existing = current.get(key)
        if isinstance(existing, str) and existing.strip():
            continue
        merged[key] = secrets.token_urlsafe(48)
        generated.append(key)

    if merged != current or not path.is_file():
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".dashboard-settings.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(merged, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, str(path))
        except BaseException:
            os.unlink(tmp_name)
            raise
        info("settings updated: %s (keys: %s)" % (path, ", ".join(sorted(merged))))
    else:
        info("settings already current: %s" % path)
    if generated:
        info("generated server-owned secret(s), value not logged: %s" % ", ".join(generated))
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass

    missing = [
        key
        for key in AUTH_SETTING_KEYS
        if not (isinstance(merged.get(key), str) and merged[key].strip())
    ]
    return merged, missing


def unconfigured_settings(path: Path) -> Tuple[List[str], int]:
    """What the web app is still missing before it can boot, names-only.

    ``dashboard_web.validate_settings`` exits 2 on any of these, so this is the
    difference between "the dashboard is broken" and "nobody has provisioned the
    Google/llmux credentials yet" -- a distinction the deploy log has to make.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return list(REQUIRED_PUBLIC_SETTINGS), len(REQUIRED_SECRET_SETTINGS)
    if not isinstance(payload, dict):
        return list(REQUIRED_PUBLIC_SETTINGS), len(REQUIRED_SECRET_SETTINGS)

    def blank(key: str) -> bool:
        value = payload.get(key)
        return not (isinstance(value, str) and value.strip())

    missing_public = [key for key in REQUIRED_PUBLIC_SETTINGS if blank(key)]
    missing_secret = sum(1 for key in REQUIRED_SECRET_SETTINGS if blank(key))
    return missing_public, missing_secret


def describe_unconfigured(path: Path) -> Optional[str]:
    missing_public, missing_secret = unconfigured_settings(path)
    if not missing_public and not missing_secret:
        return None
    parts = []
    if missing_public:
        parts.append("missing settings: %s" % ", ".join(missing_public))
    if missing_secret:
        parts.append("missing %d secret setting(s) (names withheld)" % missing_secret)
    return "%s in %s" % ("; ".join(parts), path)


# ---------------------------------------------------------------------- plist


def build_plist(root: Path, venv_dir: Path, settings_file: Path) -> Dict[str, Any]:
    log_dir = root / "logs"
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(venv_python(venv_dir)),
            "-m",
            MODULE,
            "--settings",
            str(settings_file),
        ],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "PATH": LAUNCHD_PATH,
            "PYTHONPATH": str(root),
            "PYTHONUNBUFFERED": "1",
            **{name: os.environ[name] for name in ("ROL_DASHBOARD_OWNER", "ROL_DASHBOARD_VIEWER_DOMAINS", "ROL_DASHBOARD_PUBLIC_URL") if name in os.environ},
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_dir / "dashboard.out.log"),
        "StandardErrorPath": str(log_dir / "dashboard.err.log"),
        "ThrottleInterval": 10,
    }


def write_plist(spec: Dict[str, Any], destination: Path) -> None:
    """``plistlib`` owns the XML escaping; never format plists with strings."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(destination.parent), prefix=".dashboard-plist.")
    try:
        with os.fdopen(fd, "wb") as handle:
            plistlib.dump(spec, handle)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, str(destination))
    except BaseException:
        os.unlink(tmp_name)
        raise


def launchctl_state(label: str) -> Optional[str]:
    completed = run(["launchctl", "print", "%s/%s" % (domain(), label)], check=False, capture=True)
    if completed.returncode != 0:
        return None
    for line in (completed.stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("state = "):
            return stripped[len("state = "):].strip()
    return ""


#: ``launchctl print`` wording when the job simply is not loaded. Anything else
#: that makes launchctl fail is an *unknown* state, never a stopped one.
NOT_LOADED_MARKERS = ("could not find service", "no such process", "no such file or directory")

DAEMON_RUNNING = "running"
DAEMON_STOPPED = "stopped"
DAEMON_UNKNOWN = "unknown"


def daemon_status() -> str:
    """Tri-state review-daemon status: running / stopped / unknown.

    "Stopped" is only returned when launchctl positively says the job is not
    loaded. A launchctl error, a missing state line, or any other state is
    ``unknown`` -- deploy decisions must never read "I could not tell" as "no
    executor is running".
    """
    completed = run(["launchctl", "print", "%s/%s" % (domain(), DAEMON_LABEL)], check=False, capture=True)
    output = (completed.stdout or "").lower()
    if completed.returncode != 0:
        if any(marker in output for marker in NOT_LOADED_MARKERS):
            return DAEMON_STOPPED
        return DAEMON_UNKNOWN
    for line in (completed.stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("state = "):
            state = stripped[len("state = "):].strip()
            return DAEMON_RUNNING if state == "running" else DAEMON_UNKNOWN
    return DAEMON_UNKNOWN


def bootstrap_job(destination: Path) -> None:
    run(["launchctl", "bootout", domain(), str(destination)], check=False, capture=True)
    run(["launchctl", "bootstrap", domain(), str(destination)])
    run(["launchctl", "kickstart", "-k", "%s/%s" % (domain(), LABEL)])


# ------------------------------------------------------------------ preflight


def executor_lock_busy(lock_file: Path) -> bool:
    """True when W2's executor holds the advisory lock (review in flight)."""
    if not lock_file.exists():
        return False
    try:
        import fcntl
    except ImportError:  # pragma: no cover - macOS/Linux always have fcntl
        return False
    try:
        handle = open(str(lock_file), "rb")
    except OSError:
        # Unreadable lock file: treat as busy rather than trampling a review.
        return True
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def lock_holder_pid(lock_file: Path) -> Optional[int]:
    """PID the executor wrote into its lock file, if any (diagnostics only)."""
    try:
        raw = lock_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    match = re.search(r"\d+", raw)
    return int(match.group(0)) if match else None


def executor_paths(root: Path) -> Dict[str, Any]:
    """Ask the *live* tree where its lock, pause sentinel and store live.

    The running daemon is the deployed tree, so its own
    ``dashboard_execution``/config are the only authority on these paths --
    duplicating the filenames here would silently drift the day either side
    changes a default. When the import fails the deployed tree predates the
    drain contract; that is reported, not papered over.
    """
    resolved: Dict[str, Any] = {
        "lock": root / EXECUTOR_LOCK_RELPATH,
        "pause": root / EXECUTOR_PAUSE_RELPATH,
        "store": None,
        "contract": False,
        "probe": None,
    }
    root_str = str(root)
    inserted = root_str not in sys.path
    if inserted:
        sys.path.insert(0, root_str)
    try:
        from code_review_service import dashboard_execution  # type: ignore
        from code_review_service import service as live_service  # type: ignore

        config_path = root / "config" / "reviewer.json"
        config = live_service.load_config(config_path)
        state_path = live_service.expand_path(
            config.get("state_path", "data/state.json"),
            live_service.service_root_for_config(config_path),
        )
        resolved["lock"] = dashboard_execution.executor_lock_path(config, state_path)
        resolved["pause"] = dashboard_execution.executor_pause_path(config, state_path)
        resolved["store"] = dashboard_execution.dashboard_db_path(config, state_path)
        resolved["probe"] = dashboard_execution.probe_executor_lock
        resolved["contract"] = True
    except Exception as exc:  # noqa: BLE001 - any import/config problem means "no contract"
        warn(
            "could not read the drain contract from %s (%s: %s); falling back to %s / %s"
            % (root, exc.__class__.__name__, exc, EXECUTOR_LOCK_RELPATH, EXECUTOR_PAUSE_RELPATH)
        )
    finally:
        if inserted:
            try:
                sys.path.remove(root_str)
            except ValueError:
                pass
    return resolved


def running_attempts(store_file: Path) -> List[str]:
    """Attempt rows still marked ``running`` (reported, never used to age out).

    Liveness comes from the executor lock and PIDs; this list is diagnostic, so
    that a crash leftover is *described* in the deploy log instead of silently
    deciding anything.
    """
    if not store_file.is_file():
        return []
    uri = "file:%s?mode=ro" % urllib.request.pathname2url(str(store_file))
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute("SELECT id FROM runs WHERE status = 'running'").fetchall()
    except sqlite3.Error:
        # Store missing/older schema: the lock remains the authoritative signal.
        return []
    finally:
        conn.close()
    return [str(row[0]) for row in rows]


def create_pause_sentinel(pause_file: Path) -> bool:
    """Create the executor pause sentinel; True when *this* call created it.

    ``dashboard_execution.stop_requested`` only tests for existence, so the body
    is purely for a human reading data/ later. The return value lets a refusing
    preflight clean up only what it introduced.
    """
    pause_file.parent.mkdir(parents=True, exist_ok=True)
    if pause_file.exists():
        return False
    fd, tmp_name = tempfile.mkstemp(dir=str(pause_file.parent), prefix=".executor-pause.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "requested_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "requested_by": "deploy preflight",
                    "pid": os.getpid(),
                },
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, str(pause_file))
    except BaseException:
        os.unlink(tmp_name)
        raise
    return True


def lock_is_free(paths: Dict[str, Any]) -> Tuple[bool, Optional[int]]:
    """(free, holder_pid). Uses the live tree's probe when it is available."""
    lock_file = Path(paths["lock"])
    probe = paths.get("probe")
    if probe is not None:
        try:
            holder = probe(lock_file)
        except Exception as exc:  # noqa: BLE001 - fall back rather than crash the deploy
            warn("probe_executor_lock failed (%s); using the local flock probe" % exc.__class__.__name__)
        else:
            return (holder is None, holder if holder and holder > 0 else None)
    busy = executor_lock_busy(lock_file)
    return (not busy, lock_holder_pid(lock_file) if busy else None)


def cmd_preflight(args: argparse.Namespace) -> int:
    """Pause the executor, then wait for the in-flight review to finish itself.

    The sentinel is created *first* and stays in place for the whole deploy, so
    the daemon cannot pick up new work between the check and the rsync. Nothing
    here signals or kills a review, and no attempt is judged by its age.

    The executor lock is probed for every daemon status, including "not loaded":
    ``code-review-service once`` holds the same lock without launchd.

    Exit codes: 0 = safe to replace the tree, EXIT_BUSY = refused (something
    holds the executor lock, or the deployed tree cannot be drained and the
    daemon is not positively stopped with a free lock).
    """
    root = Path(args.root).expanduser()
    paths = executor_paths(root)
    pause_file = Path(paths["pause"])
    lock_file = Path(paths["lock"])

    # Tri-state on purpose: "unknown" (launchctl error, or --no-launchctl test
    # mode) is never read as "no executor is running".
    status = DAEMON_UNKNOWN if args.no_launchctl else daemon_status()
    created_sentinel = create_pause_sentinel(pause_file)
    info("executor pause requested: %s (cleared by the 'resume' subcommand)" % pause_file)
    info("executor lock: %s | review daemon: %s" % (lock_file, status))

    if not paths["contract"]:
        # The deployed tree predates dashboard_execution, so it ignores the
        # sentinel: there is no way to drain it and no way to tell whether it is
        # mid-review. The single documented exception is a positively stopped
        # daemon whose executor lock is also free -- a warning is not permission
        # to interrupt a running review, and a held lock means something is
        # reviewing even when launchd says the daemon is not loaded.
        if status == DAEMON_STOPPED and lock_is_free(paths)[0]:
            info(
                "deployed tree has no drain contract, but the review daemon is not loaded and the "
                "executor lock is free; bootstrapping over a stopped legacy tree"
            )
            return 0
        if created_sentinel:
            # It means nothing to a legacy daemon; leave no confusing artifact.
            try:
                pause_file.unlink()
            except OSError:
                pass
        print(
            "[dashboard][error] the deployed tree does not implement the pause sentinel "
            "(no dashboard_execution), so a running review cannot be drained; review daemon is %s "
            "and the executor lock is %s. Refusing to replace the tree: stop the daemon "
            "deliberately first (`%s/service.sh stop` once its current review has finished) and let "
            "any manual `once` run finish, then re-run this deploy."
            % (status, "free" if lock_is_free(paths)[0] else "held", root),
            file=sys.stderr,
            flush=True,
        )
        return EXIT_BUSY

    # The lock is ALWAYS probed from here on, for every daemon status. launchd
    # is not the only executor: `code-review-service once` (service.py run_once)
    # takes the same lock from a plain shell, so "the launchd job is not loaded"
    # does not mean "nothing is reviewing". The sentinel is already in place, so
    # waiting is bounded either way.
    if status == DAEMON_STOPPED:
        info("review daemon is not loaded; still probing the executor lock (a manual `once` holds it too)")
    elif status == DAEMON_UNKNOWN:
        warn("review daemon state is unknown; waiting for the executor lock anyway")

    deadline = time.monotonic() + args.drain_timeout
    announced = 0.0
    stable = 0
    while True:
        free, holder = lock_is_free(paths)
        if free:
            # Confirmed twice: cheap insurance against observing the gap between
            # two lock acquisitions if a future cycle ever checks the sentinel
            # later than it does today.
            stable += 1
            if stable >= args.stable_checks:
                info("executor lock free and paused; safe to replace the tree")
                leftovers = running_attempts(Path(paths["store"])) if paths["store"] else []
                if leftovers:
                    warn(
                        "attempt rows still marked running while the executor is paused "
                        "(crash leftovers; recover_after_restart settles them): %s"
                        % ", ".join(leftovers[:5])
                    )
                return 0
        else:
            stable = 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if time.monotonic() - announced >= 30:
            announced = time.monotonic()
            info(
                "waiting for the in-flight review to finish (lock_held=%s%s, %ds left)"
                % (not free, " pid %d" % holder if holder else "", int(remaining))
            )
        time.sleep(min(args.poll_interval, max(remaining, 0.1)))

    _, holder = lock_is_free(paths)
    warn("executor still holds %s%s" % (lock_file, " (pid %d)" % holder if holder else ""))
    print(
        "[dashboard][error] a review is still in flight after waiting %ds. The deploy is refused "
        "instead of interrupting the run; re-run it once the review finishes."
        % args.drain_timeout,
        file=sys.stderr,
        flush=True,
    )
    return EXIT_BUSY


def cmd_resume(args: argparse.Namespace) -> int:
    """Remove the pause sentinel so the daemon reviews again.

    The lock file itself is never touched: it belongs to the executor.
    """
    root = Path(args.root).expanduser()
    pause_file = Path(executor_paths(root)["pause"])
    try:
        pause_file.unlink()
        info("cleared %s" % pause_file)
    except FileNotFoundError:
        info("no pause sentinel at %s" % pause_file)
    except OSError as exc:
        warn("could not remove %s (%s)" % (pause_file, exc.__class__.__name__))
        return 1
    info("review daemon may resume")
    return 0


# -------------------------------------------------------------------- install


def cmd_install(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    if not (root / "code_review_service").is_dir():
        fail("%s does not look like a deployed service tree" % root)

    version = read_version(root)
    python = select_python(args.python)
    venv_dir = ensure_venv(root, version, venv_root(args), python)

    settings_file = settings_path(args)
    _, missing_auth = merge_settings(
        settings_file,
        {
            "public_url": CANONICAL_PUBLIC_URL,
            "config_path": str(root / "config" / "reviewer.json"),
        },
    )

    (root / "logs").mkdir(parents=True, exist_ok=True)
    destination = plist_path()
    live_venv = plist_venv(destination)  # read before the new plist replaces it
    loaded = None if args.no_launchctl else loaded_venv(LABEL)
    write_plist(build_plist(root, venv_dir, settings_file), destination)
    info("launchd job written: %s" % destination)

    if args.no_launchctl:
        info("--no-launchctl: plist written, job not (re)started")
    else:
        bootstrap_job(destination)
        deadline = time.time() + args.timeout
        state = None
        while time.time() < deadline:
            state = launchctl_state(LABEL)
            if state == "running":
                break
            time.sleep(1)
        if state != "running":
            unconfigured = describe_unconfigured(settings_file)
            message = "%s is not running after %ds (state=%r); see %s" % (
                LABEL,
                args.timeout,
                state,
                root / "logs" / "dashboard.err.log",
            )
            if unconfigured and allow_unconfigured(args):
                # Explicit operator choice, logged: the job stays installed and
                # launchd brings it up as soon as the credentials land.
                warn("%s -- %s" % (message, unconfigured))
                warn("--allow-unconfigured: dashboard installed but DOWN pending credentials")
            elif unconfigured:
                fail("%s -- %s" % (message, unconfigured))
            else:
                fail(message)
        else:
            info("%s running" % LABEL)
            # Only now, with the new job confirmed running: a failed or unconfirmed
            # deploy keeps every venv, the one it may have to roll back to included.
            base = venv_root(args)
            recorded = last_running_venv(base)
            if loaded is None and recorded is None:
                # What the job ran before this deploy is unknown (launchd named no
                # program and no confirmed-running record exists yet): keep them all.
                info("venv cleanup skipped this deploy: the runtime the job ran before is unknown")
            else:
                prune_venvs(base, keep=[venv_dir, live_venv, loaded, recorded])
            record_running_venv(base, venv_dir)

    if missing_auth:
        # Loud, never silent: the dashboard is still installed and still coupled
        # to this deploy. Only the operator can provision a Google client.
        warn(
            "Google admin login is NOT configured: missing settings %s in %s. "
            "No login is possible until they are provisioned."
            % (", ".join(missing_auth), settings_file)
        )
    return 0


# --------------------------------------------------------------------- verify


def http_probe(url: str, timeout: float) -> Tuple[int, bytes]:
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    # No redirect following: a 3xx to an off-box location must be visible as a
    # failure, not chased by the verifier.
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(65536)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(65536)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def cmd_verify(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser()
    base = args.base_url.rstrip("/")
    failures: List[str] = []
    unconfigured = describe_unconfigured(settings_path(args))

    if not args.no_launchctl:
        state = launchctl_state(LABEL)
        if state != "running":
            failures.append("%s launchd state=%r (want 'running')" % (LABEL, state))

    health: Dict[str, Any] = {}
    deadline = time.time() + args.timeout
    last = "no response"
    while True:
        try:
            status, body = http_probe(base + "/healthz", timeout=5)
        except (urllib.error.URLError, OSError) as exc:
            last = "%s: %s" % (exc.__class__.__name__, exc)
        else:
            if status == 200:
                try:
                    parsed = json.loads(body.decode("utf-8"))
                except ValueError:
                    last = "/healthz returned non-JSON"
                else:
                    if isinstance(parsed, dict) and parsed.get("status") == "ok":
                        health = parsed
                        break
                    last = "/healthz body did not report status=ok"
            else:
                last = "/healthz HTTP %d" % status
        if time.time() >= deadline:
            failures.append("dashboard health check failed (%s) at %s/healthz" % (last, base))
            break
        time.sleep(1)

    if health:
        info("health: version=%s auth_configured=%s" % (health.get("version"), health.get("auth_configured")))
        deployed = read_version(root)
        if health.get("version") not in (None, deployed):
            failures.append(
                "dashboard reports version %r but the tree is %r (stale process?)"
                % (health.get("version"), deployed)
            )
        if args.require_auth and not health.get("auth_configured"):
            failures.append("auth_configured is false: Google admin login is not provisioned")
        elif not health.get("auth_configured"):
            warn("auth_configured is false: login fails closed until credentials are provisioned")

    # Anonymous callers must get 401 on identity *and* on every data route: a
    # 200 here would mean operational data leaks without a Google session.
    for path in ("/api/me", "/api/runs", "/api/runs?status=failed", "/api/usage", "/api/events"):
        try:
            status, body = http_probe(base + path, timeout=5)
        except (urllib.error.URLError, OSError) as exc:
            failures.append("unauthenticated probe of %s failed to connect (%s)" % (path, exc.__class__.__name__))
            continue
        if status != 401:
            failures.append("unauthenticated GET %s returned HTTP %d, expected 401" % (path, status))
        elif body and b'"runs"' in body:
            failures.append("unauthenticated GET %s returned run data in a 401 body" % path)
        else:
            info("unauthenticated GET %s -> 401" % path)

    if failures and unconfigured:
        if allow_unconfigured(args):
            warn("dashboard is not serving yet -- %s" % unconfigured)
            for failure in failures:
                warn(failure)
            warn("--allow-unconfigured: treating an unprovisioned dashboard as non-fatal")
            return 0
        failures.append(
            "the dashboard cannot boot until its settings are provisioned: %s "
            "(pass --allow-unconfigured to accept a dashboard-down deploy)" % unconfigured
        )

    if failures:
        for failure in failures:
            print("[dashboard][error] %s" % failure, file=sys.stderr, flush=True)
        return 1
    info("dashboard verified: job running, health ok, API closed to anonymous callers")
    return 0


# ------------------------------------------------------------------ lifecycle


def cmd_start(args: argparse.Namespace) -> int:
    destination = plist_path()
    if not destination.is_file():
        fail("%s not found; run install first" % destination)
    bootstrap_job(destination)
    info("%s started" % LABEL)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    destination = plist_path()
    run(["launchctl", "bootout", domain(), str(destination)], check=False, capture=True)
    info("%s stopped" % LABEL)
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    destination = plist_path()
    run(["launchctl", "bootout", domain(), str(destination)], check=False, capture=True)
    if destination.exists():
        destination.unlink()
    info("%s uninstalled" % LABEL)
    # Data, settings and venvs survive on purpose: the settings file holds the
    # only copy of the OAuth client secret and data/ holds review history.
    info("preserved: %s, %s, %s" % (settings_path(args), venv_root(args), Path(args.root) / "data"))
    return 0


# ----------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install the Review of Legends admin dashboard job")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]), help="deployed service tree")
    parser.add_argument("--settings", default=None, help="settings JSON path (default $HOME/review-of-legends/dashboard-secrets.json)")
    parser.add_argument("--venv-root", default=None, help="venv parent dir (default $HOME/review-of-legends/venvs)")
    sub = parser.add_subparsers(dest="command", required=True)

    preflight = sub.add_parser("preflight", help="pause the executor and wait for the review to finish")
    preflight.add_argument("--drain-timeout", type=int, default=2300, help="seconds to wait for the lock to free")
    preflight.add_argument("--poll-interval", type=float, default=2.0)
    preflight.add_argument("--stable-checks", type=int, default=2, help="consecutive free-lock observations required")
    preflight.add_argument("--no-launchctl", action="store_true", help="skip the daemon launchd state lookup")
    preflight.set_defaults(func=cmd_preflight)

    sub.add_parser("resume", help="clear the drain request (always run after a deploy)").set_defaults(
        func=cmd_resume
    )

    install = sub.add_parser("install", help="venv + settings + launchd job")
    install.add_argument("--python", default=None, help="interpreter used to create the venv")
    install.add_argument("--timeout", type=int, default=30)
    install.add_argument("--no-launchctl", action="store_true", help="write the plist without touching launchd")
    install.add_argument(
        "--allow-unconfigured",
        action="store_true",
        help="do not fail when the job cannot start only because credentials are unprovisioned",
    )
    install.set_defaults(func=cmd_install)

    verify = sub.add_parser("verify", help="job running, health ok, API closed")
    verify.add_argument("--base-url", default=BASE_URL)
    verify.add_argument("--timeout", type=int, default=30)
    verify.add_argument("--require-auth", action="store_true", help="also require auth_configured=true")
    verify.add_argument(
        "--allow-unconfigured",
        action="store_true",
        help="downgrade an unprovisioned (never-booted) dashboard to a warning",
    )
    verify.add_argument("--no-launchctl", action="store_true")
    verify.set_defaults(func=cmd_verify)

    sub.add_parser("start", help="bootstrap + kickstart the job").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="bootout the job").set_defaults(func=cmd_stop)
    sub.add_parser("uninstall", help="bootout + remove the plist (data preserved)").set_defaults(func=cmd_uninstall)
    return parser


def read_deploy_env(root: Path) -> Dict[str, str]:
    """The ROL_* settings in ``root/config/deploy.env``; {} when it is absent.

    Read as data, never executed: blank lines and ``#`` comments are skipped,
    every other line must be ``KEY=VALUE`` with a known key. The value is taken
    literally (no quotes, no expansion), as service.sh reads it.
    """
    path = root / DEPLOY_ENV_NAME
    if not path.is_file():
        return {}
    values: Dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Z_][A-Z0-9_]*)=(.*)", line)
        if not match:
            fail("%s:%d is not a KEY=VALUE line" % (path, number))
        key, value = match.group(1), match.group(2)
        if key not in DEPLOY_ENV_KEYS:
            fail("%s:%d: unknown key %s (known: %s)" % (path, number, key, ", ".join(DEPLOY_ENV_KEYS)))
        if key in values:
            fail("%s:%d: %s is set twice" % (path, number, key))
        values[key] = value
    return values


def apply_deploy_env(root: Path) -> None:
    """Load deploy.env into the environment and re-derive what depends on it."""
    global LABEL, DAEMON_LABEL, CANONICAL_PUBLIC_URL
    for key, value in read_deploy_env(root).items():
        # As service.sh: a variable set and non-empty wins; empty counts as unset.
        if not os.environ.get(key):
            os.environ[key] = value
    DAEMON_LABEL = os.environ.get("ROL_SERVICE_LABEL") or DEFAULT_DAEMON_LABEL
    LABEL = os.environ.get("ROL_DASHBOARD_LABEL") or DAEMON_LABEL + ".dashboard"
    CANONICAL_PUBLIC_URL = os.environ.get("ROL_DASHBOARD_PUBLIC_URL", "").rstrip("/")
    for label in (DAEMON_LABEL, LABEL):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", label):
            fail("launchd label %r may hold only letters, digits, '.', '_' and '-'" % label)
    if LABEL == DAEMON_LABEL:
        # One plist for both jobs: installing the dashboard would replace the daemon.
        fail("ROL_SERVICE_LABEL and ROL_DASHBOARD_LABEL must differ (both are %s)" % LABEL)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if getattr(args, "root", None):
        apply_deploy_env(Path(args.root))
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
