"""Human-facing installation and first-run flow. Core runtime remains stdlib-only."""
from __future__ import annotations

import argparse
import getpass
import html
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import service, llm_runner

ROOT = Path(os.environ.get("ROL_ASSETS", Path(__file__).resolve().parents[1]))
REPOSITORY = "https://github.com/2lab-ai/review-of-legends"



def homebrew_formula() -> str:
    """The Homebrew formula this rol came from: the wrapper names it (stable or preview)."""
    name = os.environ.get("ROL_HOMEBREW_FORMULA", "review-of-legends")
    return name if name in ("review-of-legends", "review-of-legends-preview") else "review-of-legends"

def home() -> Path:
    return Path(os.environ.get("ROL_HOME", Path.home() / ".config/review-of-legends")).expanduser().resolve()


def private_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".rol-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(value)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_json(path: Path, value: dict) -> None:
    private_write(path, json.dumps(value, indent=2) + "\n")


def run(args: list[str], capture: bool = False) -> str:
    result = subprocess.run(args, text=True, capture_output=capture)
    if result.returncode:
        # Captured output may contain OAuth credentials. Never echo it.
        raise service.ServiceError(f"{args[0]} {args[1]} failed (exit {result.returncode}); retry that step.")
    return result.stdout if capture else ""


def ask(label: str, default: str = "") -> str:
    value = input(f"{label}{' [' + default + ']' if default else ''}: ").strip()
    return value or default


def yes(label: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = ask(label, hint)
        if answer == hint:
            return default
        if answer.lower() in ("y", "yes"):
            return True
        if answer.lower() in ("n", "no"):
            return False
        print("Enter yes or no.")


def api(path: str, token: str = "", method: str = "GET"):
    request = urllib.request.Request("https://api.github.com" + path, method=method,
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with llm_runner._OPENER.open(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise service.ServiceError(f"GitHub setup request failed (HTTP {exc.code}). Check App permissions and retry.") from None
    except OSError:
        raise service.ServiceError("GitHub setup request failed. Check the connection and retry.") from None


def manifest(callback: str, name: str = "Review of Legends") -> dict:
    return {"name": name, "url": REPOSITORY, "redirect_url": callback,
            "hook_attributes": {"url": "https://example.com/unused", "active": False},
            "public": False, "default_events": [],
            "default_permissions": {"contents": "read", "metadata": "read",
                                    "pull_requests": "write", "issues": "write", "statuses": "write"}}


def create_app(directory: Path) -> dict:
    state = secrets.token_urlsafe(32)
    route = "/" + secrets.token_urlsafe(24)
    result = {}
    org = ask("App owner organization (blank for your personal account)")
    if org and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", org):
        raise service.ServiceError("Invalid GitHub organization name.")
    name = ask("GitHub App name", "Review of Legends-" + secrets.token_hex(4))
    if not name or len(name) > 34 or any(ord(char) < 32 for char in name):
        raise service.ServiceError("Use an App name with 1–34 printable characters.")
    target = f"https://github.com/organizations/{org}/settings/apps/new" if org else "https://github.com/settings/apps/new"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # callback contains a one-time code

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            status, body = 200, ""
            if parsed.path == route:
                data = html.escape(json.dumps(manifest(origin + "/callback", name)), quote=True)
                body = (f'<form method="post" action="{target}?state={state}">'
                        f'<input type="hidden" name="manifest" value="{data}">'
                        '<button>Create Review of Legends GitHub App</button></form>')
            elif parsed.path == "/callback" and secrets.compare_digest(query.get("state", [""])[0], state) and query.get("code"):
                result["code"] = query["code"][0]
                body = "App created. Return to the terminal to install it and choose repositories."
            else:
                status, body = 400, "Invalid setup callback. Return to the terminal."
            payload = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 1
        origin = f"http://127.0.0.1:{server.server_port}"
        url = origin + route
        print("Open to create your App:", url)
        webbrowser.open(url)
        deadline = time.monotonic() + 600
        while "code" not in result and time.monotonic() < deadline:
            server.handle_request()
        if "code" not in result:
            raise service.ServiceError("App creation timed out after 10 minutes; run rol setup again.")
    data = api("/app-manifests/" + urllib.parse.quote(result["code"], safe="") + "/conversions", method="POST")
    key = directory / ("github-app-" + secrets.token_hex(8) + ".pem")
    private_write(key, data["pem"])
    app = {"app_id": data["id"], "private_key_path": str(key), "bot_login": data["slug"] + "[bot]"}
    save_json(directory / "app-registration.json", app)  # resume after interrupted installation
    return app


def configure_app(directory: Path, existing: dict) -> dict:
    registration = directory / "app-registration.json"
    app = existing.get("github_app")
    if not app and registration.is_file():
        app = json.loads(registration.read_text())
    if app and yes("Reuse saved GitHub App"):
        app = dict(app)
    elif yes("Create a GitHub App in the browser"):
        app = create_app(directory)
    else:
        key = Path(ask("Existing App private key file")).expanduser().resolve()
        app = {"app_id": int(ask("App ID")), "private_key_path": str(directory / ("github-app-" + secrets.token_hex(8) + ".pem")),
               "bot_login": ask("App bot login (slug[bot])")}
        private_write(Path(app["private_key_path"]), key.read_text())
        save_json(registration, app)
    slug = app["bot_login"].removesuffix("[bot]")
    url = f"https://github.com/apps/{urllib.parse.quote(slug, safe='')}/installations/new"
    print("Install the App on the repositories you want to review:", url)
    webbrowser.open(url)
    ask("Press Enter after installing the App")
    jwt = service.github_app_jwt(int(app["app_id"]), app["private_key_path"])
    installations = []
    page = 1
    while True:
        batch = api(f"/app/installations?per_page=100&page={page}", jwt)
        installations.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    if not installations:
        raise service.ServiceError("No App installations found. Install it, then rerun rol setup; the key is saved.")
    for i, item in enumerate(installations, 1):
        print(f"  {i}. {item['account']['login']}")
    index = int(ask("Installation number", "1")) - 1
    if index < 0 or index >= len(installations):
        raise service.ServiceError("Invalid installation number.")
    app["installation_id"] = installations[index]["id"]
    save_json(registration, app)
    return service.validate_github_app(app, directory)


def choose_repos(app: dict) -> list[str]:
    token = service.github_app_token(app)
    available = []
    page = 1
    while True:
        batch = api(f"/installation/repositories?per_page=100&page={page}", token)["repositories"]
        available.extend(item["full_name"] for item in batch)
        if len(batch) < 100:
            break
        page += 1
    if not available:
        raise service.ServiceError("The App has no accessible repositories. Update its installation and retry.")
    for i, repo in enumerate(available, 1):
        print(f"  {i}. {repo}")
    selection = ask("Repository numbers, separated by commas (or all)")
    if selection.lower() == "all":
        selected = available
    else:
        indices = [int(part.strip()) - 1 for part in selection.split(",")]
        if any(i < 0 or i >= len(available) for i in indices):
            raise service.ServiceError("Invalid repository number.")
        selected = list(dict.fromkeys(available[i] for i in indices))
    for repo in selected:
        run(["gh", "api", "repos/" + repo], capture=True)  # gh identity must also clone
    return selected


def parse_llmux_env(raw: str) -> dict:
    values = {}
    for line in raw.splitlines():
        words = shlex.split(line)
        if len(words) == 2 and words[0] == "export" and "=" in words[1]:
            key, value = words[1].split("=", 1)
            if key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"):
                values[key] = value
    return values


def configure_llmux(directory: Path) -> tuple[str, Path]:
    new_install = not shutil.which("llmux")
    if new_install:
        if not shutil.which("brew"):
            raise service.ServiceError("Install Homebrew or llmux first: https://github.com/2lab-ai/llmux#install")
        if not yes("Install llmux using brew install 2lab-ai/tap/llmux"):
            raise service.ServiceError("llmux is required; install it and rerun rol setup.")
        run(["brew", "install", "2lab-ai/tap/llmux"])
    if yes("Connect a ChatGPT subscription in llmux", new_install):
        run(["llmux", "login", "--codex"])
    env = parse_llmux_env(run(["llmux", "env"], capture=True))
    endpoint = ask("llmux endpoint", env.get("ANTHROPIC_BASE_URL", "http://localhost:3456")).rstrip("/")
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise service.ServiceError("Use an http(s) llmux URL without credentials, query, or fragment.")
    if parsed.hostname not in ("localhost", "127.0.0.1", "::1") and parsed.scheme != "https":
        raise service.ServiceError("Use HTTPS for a remote llmux endpoint.")
    llm_runner.check_base_url(endpoint)
    key = env.get("ANTHROPIC_API_KEY", "") if endpoint == env.get("ANTHROPIC_BASE_URL", "").rstrip("/") else ""
    if not key:
        key = getpass.getpass("llmux client key (blank for an unkeyed local daemon): ").strip() or "review-of-legends"
    key_file = directory / ("llmux-key-" + secrets.token_hex(8))
    private_write(key_file, key)
    def probe():
        with llm_runner._OPENER.open(urllib.request.Request(endpoint + "/v1/models", headers={"x-api-key": key}), timeout=10) as response:
            return json.load(response)
    try:
        probe()
    except urllib.error.HTTPError:
        raise service.ServiceError("llmux refused the client key or model endpoint. Check llmux env and retry.") from None
    except OSError:
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if not local or not yes("Start the local llmux server now"):
            raise service.ServiceError("Start llmux (llmux server --no-tui), then rerun setup.") from None
        log = directory / "llmux.log"
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as output:
            process = subprocess.Popen(["llmux", "server", "--no-tui", "--port", str(parsed.port or 3456)],
                stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
        ready = False
        for _ in range(30):
            if process.poll() is not None:
                break
            try:
                probe()
                ready = True
                break
            except (OSError, ValueError):
                time.sleep(1)
        if not ready:
            if process.poll() is None:
                process.terminate()
            raise service.ServiceError(f"llmux did not become ready. Inspect {log} and rerun setup.")
    return endpoint, key_file


def configuration(directory: Path, repos: list[str], app: dict, endpoint: str, key_file: Path, existing: dict) -> dict:
    config = existing or json.loads((ROOT / "config/reviewer.json").read_text())
    config = json.loads(json.dumps(config))
    config.update(repositories=repos, github_app=app)
    config["repository_agents"] = {r: v for r, v in config.get("repository_agents", {}).items() if r in repos}
    config["state_path"] = str(directory / "data/state.json")
    config["work_dir"] = str(directory / "data/work")
    config["sweep_legacy_work_dir"] = False
    if not existing:
        # The review history store (stdlib SQLite under data/): author
        # commands, language preferences and rereviews read it. It is not the
        # web dashboard, which is a separate deployment.
        config["dashboard"] = {"enabled": True}
        config["model_policy"]["fallback_models"] = []
    for agent in config["agents"]:
        if not existing:
            agent["prompt_path"] = str(ROOT / "agents" / (agent["id"] + ".md"))
    for name, prompt in (("executive_summary", "executive-summary"), ("visual_outline", "visual-outline")):
        if not existing:
            config[name]["prompt_path"] = str(ROOT / "agents" / (prompt + ".md"))
    # Keep runner tuning while updating the interpreter, code location and endpoint.
    command = config["agent_runner"]["command"]
    command[0:2] = [os.environ.get("ROL_PYTHON", sys.executable), str(ROOT / "code_review_service/llm_runner.py")]
    for flag, value in (("--base-url", endpoint), ("--api-key-file", str(key_file))):
        if flag in command:
            command[command.index(flag) + 1] = value
        else:
            command.extend([flag, value])
    if "--api-key" in command:
        offset = command.index("--api-key")
        del command[offset:offset + 2]
    return config


def setup() -> int:
    if not sys.stdin.isatty():
        raise service.ServiceError("rol setup needs an interactive terminal.")
    directory = home()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "reviewer.json"
    existing = json.loads(path.read_text()) if path.exists() else {}
    if existing and not yes("Update existing setup (a backup will be saved)"):
        return 0
    for command in ("git", "gh", "openssl"):
        if not shutil.which(command):
            raise service.ServiceError(f"Missing {command}; install it first (brew install git gh openssl@3).")
    if subprocess.run(["gh", "auth", "status"], capture_output=True).returncode:
        run(["gh", "auth", "login", "--hostname", "github.com", "--web", "--git-protocol", "https"])
    endpoint, key_file = configure_llmux(directory)
    app = configure_app(directory, existing)
    repos = choose_repos(app)
    config = configuration(directory, repos, app, endpoint, key_file, existing)
    config["model_policy"]["model"] = ask("Review model", config["model_policy"]["model"])
    config["review_existing_open_prs_on_first_run"] = yes("Review existing open PRs on first start", False)
    # Validate the complete candidate before replacing an existing configuration.
    candidate = directory / ".reviewer-candidate.json"
    save_json(candidate, config)
    try:
        service.load_config(candidate)
    finally:
        candidate.unlink(missing_ok=True)
    if path.exists():
        private_write(directory / ("reviewer.backup-" + str(time.time_ns()) + ".json"), path.read_text())
    save_json(path, config)
    print("Saved configuration:", path)
    if service.main(["--config", str(path), "doctor"]):
        raise service.ServiceError("Setup saved, but doctor failed. Fix the reported problem and run rol doctor.")
    print("Setup verified. Starting the bot posts reviews to the selected repositories.")
    if yes("Start reviewing now"):
        if shutil.which("brew") and os.environ.get("ROL_HOMEBREW") == "1" and not os.environ.get("ROL_HOME"):
            run(["brew", "services", "start", homebrew_formula()])
        else:
            return service.main(["--config", str(path), "daemon"])
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="rol", description="Review of Legends — install once, review continuously")
    parser.add_argument("--config", default=str(home() / "reviewer.json"))
    parser.add_argument("command", choices=["setup", "doctor", "daemon", "once", "version", "start", "stop", "status"])
    args, rest = parser.parse_known_args(argv)
    try:
        if args.command == "setup":
            if rest or args.config != str(home() / "reviewer.json"):
                parser.error("setup uses ROL_HOME; no other options are supported")
            return setup()
        if args.command in ("start", "stop", "status"):
            if rest or args.config != str(home() / "reviewer.json"):
                parser.error("Homebrew services use the default configuration")
            if os.environ.get("ROL_HOME"):
                raise service.ServiceError("Use rol daemon for a custom ROL_HOME; brew services uses the default home.")
            if not shutil.which("brew"):
                raise service.ServiceError("Service management needs Homebrew; use rol daemon in a supervisor otherwise.")
            run(["brew", "services", "info" if args.command == "status" else args.command, homebrew_formula()])
            return 0
        return service.main(["--config", args.config, args.command] + rest)
    except (service.ServiceError, llm_runner.RunnerError, ValueError, OSError) as exc:
        print(f"rol: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nSetup interrupted. Saved App registration can be reused by rol setup.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
