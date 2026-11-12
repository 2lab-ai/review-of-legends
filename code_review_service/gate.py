"""The contributor gate at work: decisions with GitHub reads, the gate comment,
fork CI approval, and the pledge and verify commands (rules in contributors.py).

The scheduler asks ``Gatekeeper.decide`` for each pull request that would be
queued; an untrusted one is not queued and gets the gate comment instead. A
decision is cached per (repository, login): an untrusted one for
``UNTRUSTED_TTL`` seconds, so a list change, a verification or a pledge merge is
picked up within that, and a trusted one for ``TRUSTED_TTL``. A verify or
pledge command clears the login's entries at once.

GitHub clients and the store are passed in; every GitHub error propagates to
the caller, which leaves the pull request for the next watch (fail closed: an
author whose trust cannot be read is not reviewed this time). Python 3.9.
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Any, Callable, Dict, Optional, Set, Tuple

from . import contributors as c
from . import mentions

TRUSTED_TTL = 600.0
UNTRUSTED_TTL = 120.0
LISTS_TTL = 60.0
# Fork CI approval is refused (no Actions write): try that repository again after this.
NO_PERMISSION_BACKOFF = 3600.0
# How long after the daemon first sees a fork head it keeps approving that
# head's waiting runs (workflows start at different times; a reviewed head
# still needs its CI).
CI_APPROVAL_WINDOW = 1800.0
PLEDGE_BRANCH_PREFIX = "review-of-legends/contributor-"

Logger = Callable[[str], None]


def _status(exc: Exception) -> Optional[int]:
    return getattr(exc, "status_code", None)


def default_branch(pr: Dict[str, Any]) -> str:
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
    return str(repo.get("default_branch") or base.get("ref") or "main")


def is_fork(repo: str, pr: Dict[str, Any]) -> bool:
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else None
    if head_repo is None:
        return True  # a deleted fork
    return str(head_repo.get("full_name") or "").lower() != repo.lower()


class Gatekeeper:
    def __init__(self, clock: Callable[[], float] = time.monotonic, log: Logger = print) -> None:
        self._clock = clock
        self._log = log
        self._lock = threading.Lock()
        # (repo, login) -> (decision, expires)
        self._decisions: Dict[Tuple[str, str, int], Tuple[c.Decision, float]] = {}
        # (repo, branch) -> (approved list, pledge blob sha or "", expires)
        self._lists: Dict[Tuple[str, str], Tuple[c.ContributorList, str, float]] = {}
        # (repo, number) -> hash of the gate comment body already on the PR
        self._commented: Dict[Tuple[str, int], str] = {}
        # (repo, number) whose comment was already checked once trusted
        self._approved_checked: Set[Tuple[str, int]] = set()
        # (repo, number) of an approved author whose approved note waits for the write budget
        self._approved_deferred: Set[Tuple[str, int]] = set()
        # repo -> when fork CI approval may be tried again after a refusal
        self._ci_backoff: Dict[str, float] = {}
        # (repo, list blob sha) whose ignored lines were already logged
        self._warned_lists: Set[Tuple[str, str]] = set()

    # ------------------------------------------------------------- decisions

    def applies(self, config: Dict[str, Any], repo: str) -> bool:
        try:
            return c.gate_applies(c.gate_settings(config), repo)
        except ValueError:
            return False  # load_config refuses it; never gate on a config it rejected

    def lists(self, client: Any, repo: str, branch: str) -> Tuple[c.ContributorList, str]:
        """The approved list and the pledge file's blob sha ("" when there is none) on ``branch``."""
        key = (repo.lower(), branch)
        now = self._clock()
        with self._lock:
            cached = self._lists.get(key)
        if cached is not None and cached[2] > now:
            return cached[0], cached[1]
        approved = client.get_file(c.APPROVED_FILE, branch)
        pledge = client.get_file(c.PLEDGE_FILE, branch)
        parsed = c.parse_list(approved[0] if approved else None)
        if parsed.invalid and approved and (repo.lower(), approved[1]) not in self._warned_lists:
            self._warned_lists.add((repo.lower(), approved[1]))
            self._log("%s: WARNING %s on %s: ignored lines that are neither a login nor *@domain: %s"
                      % (repo, c.APPROVED_FILE, branch, ", ".join(repr(line) for line in parsed.invalid[:5])))
        pledge_sha = pledge[1] if pledge else ""
        with self._lock:
            self._lists[key] = (parsed, pledge_sha, now + LISTS_TTL)
        return parsed, pledge_sha

    def decide(self, client: Any, config: Dict[str, Any], repo: str, pr: Dict[str, Any], store: Any) -> c.Decision:
        login, _ = mentions.pr_author(pr)
        if not login:
            return c.Decision(False, "no author")
        user = pr.get("user") if isinstance(pr.get("user"), dict) else {}
        user_id = user.get("id") if isinstance(user.get("id"), int) and not isinstance(user.get("id"), bool) else 0
        # The account id too: a login reassigned within the TTL is decided afresh.
        key = (repo.lower(), login.lower(), user_id)
        now = self._clock()
        with self._lock:
            cached = self._decisions.get(key)
        if cached is not None and cached[1] > now:
            return cached[0]
        decision = self._decide(client, config, repo, pr, login, store)
        ttl = TRUSTED_TTL if decision.trusted else UNTRUSTED_TTL
        with self._lock:
            self._decisions[key] = (decision, now + ttl)
        return decision

    def _decide(
        self, client: Any, config: Dict[str, Any], repo: str, pr: Dict[str, Any], login: str, store: Any
    ) -> c.Decision:
        settings = c.gate_settings(config)
        bot = str(client.bot_login() or "") if hasattr(client, "bot_login") else ""
        if bot and login.lower() == bot.lower():
            return c.Decision(True, "this bot")
        if login.lower() in settings["trusted_bots"]:
            return c.Decision(True, "trusted bot")
        if login.lower() in settings["approved_logins"]:
            return c.Decision(True, "approved by the deployment")
        approved, _ = self.lists(client, repo, default_branch(pr))
        if login.lower() in approved.logins:
            return c.Decision(True, "approved list")
        user = pr.get("user") if isinstance(pr.get("user"), dict) else {}
        user_id = user.get("id") if isinstance(user.get("id"), int) and not isinstance(user.get("id"), bool) else 0
        identity = None
        row = store.contributor_identity(login) if store is not None else None
        if isinstance(row, dict) and row.get("email"):
            identity = c.Identity(str(row.get("login") or login), str(row["email"]), int(row.get("user_id") or 0))
        if identity is not None:
            decision = c.decide(
                login, permission=None, approved=approved, trusted_domains=settings["trusted_email_domains"],
                identity=identity, trusted_bots=(), user_id=user_id,
            )
            if decision.trusted:
                return decision
        permission = client.collaborator_permission(login)
        return c.decide(
            login, permission=permission, approved=approved, trusted_domains=settings["trusted_email_domains"],
            identity=identity, trusted_bots=(), user_id=user_id,
        )

    def forget(self, login: str) -> None:
        """A login's trust may have changed (verified, signed): decide again next time."""
        lowered = login.lower()
        with self._lock:
            for key in [k for k in self._decisions if k[1] == lowered]:
                del self._decisions[key]

    def forget_lists(self, repo: str) -> None:
        """Read the repository's lists afresh next time; cached decisions stay (they expire on their own)."""
        with self._lock:
            for key in [k for k in self._lists if k[0] == repo.lower()]:
                del self._lists[key]

    # --------------------------------------------------------------- comments

    def _own_gate_comment(self, client: Any, number: int) -> Optional[Dict[str, Any]]:
        bot = str(client.bot_login() or "")
        for comment in client.list_issue_comments(number):
            body = str(comment.get("body") or "")
            author = str((comment.get("user") or {}).get("login") or "")
            if body.startswith(c.GATE_MARKER) and bot and author.lower() == bot.lower():
                return comment
        return None

    def post_gate_comment(
        self, client: Any, config: Dict[str, Any], repo: str, pr: Dict[str, Any], store: Any
    ) -> bool:
        """Put the gate comment on an unapproved author's PR; True when a comment was written."""
        number = int(pr["number"])
        login, _ = mentions.pr_author(pr)
        settings = c.gate_settings(config)
        branch = default_branch(pr)
        approved, pledge_sha = self.lists(client, repo, branch)
        dashboard = config.get("dashboard") if isinstance(config.get("dashboard"), dict) else {}
        lang = None
        row = store.get_pr_author(login) if store is not None and login else None
        if isinstance(row, dict):
            lang = mentions.stored_language(row.get("lang"))
        body = c.gate_comment(
            login, repo, branch,
            domains=set(approved.domains) | set(settings["trusted_email_domains"]),
            pledge=bool(pledge_sha) and store is not None,
            dashboard_url=str(dashboard.get("public_url") or "") if store is not None else "",
            lang=mentions.reply_language(lang, config),
        )
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        key = (repo.lower(), number)
        with self._lock:
            if self._commented.get(key) == digest:
                return False
        existing = self._own_gate_comment(client, number)
        wrote = False
        if existing is None:
            client.create_issue_comment(number, body)
            wrote = True
        elif str(existing.get("body") or "") != body:
            client.update_issue_comment(int(existing["id"]), body)
            wrote = True
        with self._lock:
            self._commented[key] = digest
            self._approved_checked.discard(key)
        return wrote

    def mark_approved(self, client: Any, config: Dict[str, Any], repo: str, pr: Dict[str, Any], decision: c.Decision) -> bool:
        """Once per PR and process: turn a gate comment left from before into the approved note."""
        number = int(pr["number"])
        key = (repo.lower(), number)
        with self._lock:
            if key in self._approved_checked:
                return False
        existing = self._own_gate_comment(client, number)
        wrote = False
        if existing is not None:
            login, _ = mentions.pr_author(pr)
            body = c.approved_comment(login, decision.reason, mentions.default_reply_language(config))
            if str(existing.get("body") or "") != body:
                client.update_issue_comment(int(existing["id"]), body)
                wrote = True
        with self._lock:
            self._approved_checked.add(key)
            self._commented.pop(key, None)
            self._approved_deferred.discard(key)
        return wrote

    def defer_approved(self, repo: str, number: int) -> None:
        """The approved note of this PR waits for a later tick's write budget."""
        with self._lock:
            self._approved_deferred.add((repo.lower(), int(number)))

    def unsettled(self, repo: str, open_numbers: Set[int]) -> Set[int]:
        """Open PRs of ``repo`` whose gate comment may still need the approved note.

        Those this process commented on or deferred, not yet checked once
        trusted. Closed PRs are dropped from the bookkeeping here.
        """
        lowered = repo.lower()
        with self._lock:
            for key in [k for k in self._commented if k[0] == lowered and k[1] not in open_numbers]:
                del self._commented[key]
            self._approved_deferred = {k for k in self._approved_deferred if k[0] != lowered or k[1] in open_numbers}
            keys = set(k for k in self._commented if k[0] == lowered) | set(k for k in self._approved_deferred if k[0] == lowered)
            return {k[1] for k in keys if k not in self._approved_checked}

    # ---------------------------------------------------------------- fork CI

    def ci_blocked(self, repo: str) -> bool:
        """Fork CI approval is backing off after a refusal (no Actions write)."""
        return self.ci_backoff_end(repo) > self._clock()

    def ci_backoff_end(self, repo: str) -> float:
        """When the last refusal's backoff ends (or ended); 0.0 when there never was one."""
        with self._lock:
            return self._ci_backoff.get(repo.lower(), 0.0)

    def approve_fork_runs(self, client: Any, config: Dict[str, Any], repo: str, pr: Dict[str, Any]) -> int:
        """Approve this PR's waiting fork workflow runs; the number approved.

        The caller has checked that every open PR sharing this fork, branch and
        commit has an approved author (a run cannot be told apart between
        them). A run that names its pull requests must name this one.
        """
        settings = c.gate_settings(config)
        if not settings["approve_fork_ci"] or not is_fork(repo, pr):
            return 0
        now = self._clock()
        if self.ci_blocked(repo):
            return 0
        head_info = pr.get("head") if isinstance(pr.get("head"), dict) else {}
        head = str(head_info.get("sha") or "")
        head_repo = head_info.get("repo") if isinstance(head_info.get("repo"), dict) else {}
        source = str(head_repo.get("full_name") or "").lower()
        branch = str(head_info.get("ref") or "")
        if not head or not source:
            return 0  # a deleted fork: nothing that is provably this PR's
        approved = 0
        try:
            runs = client.list_waiting_runs(head)
        except Exception as exc:  # noqa: BLE001 - a missing permission must not stop reviews
            if _status(exc) in (403, 404):
                self._refused(repo, now, exc)
                return 0
            raise
        for run in runs:
            # Only this PR's runs: same commit, same fork and branch. Another
            # PR (an unapproved author's) can point at the very same commit.
            run_repo = run.get("head_repository") if isinstance(run.get("head_repository"), dict) else {}
            if str(run_repo.get("full_name") or "").lower() != source or str(run.get("head_branch") or "") != branch:
                continue
            numbers = {item.get("number") for item in run.get("pull_requests") or [] if isinstance(item, dict)}
            if numbers and int(pr["number"]) not in numbers:
                continue
            try:
                client.approve_run(int(run["id"]))
            except Exception as exc:  # noqa: BLE001 - see below
                status = _status(exc)
                if status == 404:
                    # Listed a moment ago: the run was deleted or re-run since, not a missing permission.
                    self._log("%s pr #%d: fork CI run %s is gone; skipped" % (repo, int(pr["number"]), run.get("id")))
                    continue
                if status == 403:
                    self._refused(repo, now, exc)
                    break
                raise
            approved += 1
        if approved:
            self._log("%s pr #%d: approved %d waiting fork CI run(s) of %s" % (repo, int(pr["number"]), approved, head[:12]))
        return approved

    def _refused(self, repo: str, now: float, exc: Exception) -> None:
        with self._lock:
            self._ci_backoff[repo.lower()] = now + NO_PERMISSION_BACKOFF
        self._log("%s: WARNING cannot approve fork CI runs (the App needs Actions: read and write): %s" % (repo, exc))

    # --------------------------------------------------------------- commands

    def handle_command(
        self, client: Any, store: Any, config: Dict[str, Any], repo: str, request: mentions.Request
    ) -> str:
        """Answer a PLEDGE or VERIFY command; returns the reply body. GitHub/store errors propagate."""
        row = store.get_pr_author(request.login)
        lang = mentions.stored_language(row.get("lang")) if isinstance(row, dict) else None
        language = mentions.reply_language(lang, config)
        texts = _TEXTS[language]
        if not self.applies(config, repo):
            return mentions._reply(request, texts["off"])
        kind = request.command.kind
        if kind == mentions.VERIFY_INVALID:
            return mentions._reply(request, texts["verify_usage"])
        if kind == mentions.VERIFY:
            return self._verify(client, store, config, repo, request, texts)
        return self._pledge(client, store, config, repo, request, texts)

    def _verify(self, client: Any, store: Any, config: Dict[str, Any], repo: str, request: mentions.Request, texts: Dict[str, str]) -> str:
        email = store.consume_contributor_code(
            c.code_hash(request.command.value), request.login, request.url, user_id=request.user_id,
        )
        if not email:
            return mentions._reply(request, texts["verify_unknown"])
        self.forget(request.login)
        domain = c.email_domain(email) or "?"
        pr = client.get_pr(request.number)
        approved, _ = self.lists(client, repo, default_branch(pr))
        trusted = set(approved.domains) | set(c.gate_settings(config)["trusted_email_domains"])
        message = texts["verified_ok" if domain in trusted else "verified_untrusted"] % {"domain": domain}
        return mentions._reply(request, message)

    def _pledge(self, client: Any, store: Any, config: Dict[str, Any], repo: str, request: mentions.Request, texts: Dict[str, str]) -> str:
        pr = client.get_pr(request.number)
        author, _ = mentions.pr_author(pr)
        if author.lower() != request.login.lower():
            return mentions._reply(request, texts["pledge_author"])
        branch = default_branch(pr)
        self.forget_lists(repo)
        self.forget(request.login)
        approved, pledge_sha = self.lists(client, repo, branch)
        if not pledge_sha:
            return mentions._reply(request, texts["pledge_none"])
        if request.login.lower() in approved.logins:
            return mentions._reply(request, texts["pledge_listed"])
        signature = store.record_pledge(
            repo, request.number, request.login, request.user_id, request.url, pledge_sha,
            source_key=mentions.source_key(repo, request),
        )
        if signature.get("pull_url"):
            return mentions._reply(request, texts["pledge_pr"] % {"url": signature["pull_url"]})
        if not c.gate_settings(config)["open_pledge_pull_request"]:
            return mentions._reply(request, texts["pledge_recorded_manual"] % {"login": request.login})
        try:
            url = self._open_pledge_pull(client, repo, branch, request, signature)
        except ValueError:
            # add_login refused: the default branch lists the signer already (listed since the read above).
            self.forget_lists(repo)
            return mentions._reply(request, texts["pledge_listed"])
        except Exception as exc:  # noqa: BLE001 - recorded; a maintainer can add the line by hand
            if _status(exc) in (403, 404, 422):
                self._log("%s pr #%d: WARNING could not open the pledge pull request for %s (the App needs Contents: read and write): %s"
                          % (repo, request.number, request.login, exc))
                return mentions._reply(request, texts["pledge_recorded_manual"] % {"login": request.login})
            raise
        store.set_pledge_pull(int(signature["id"]), url)
        return mentions._reply(request, texts["pledge_pr"] % {"url": url})

    def _open_pledge_pull(self, client: Any, repo: str, base: str, request: mentions.Request, signature: Dict[str, Any]) -> str:
        """Open (or refresh) the pull request that adds the signer to the approved list.

        Its one commit is always built on the default branch as it is now: a
        branch left from a declined pull request, or an open one that a merged
        sibling pledge has since put in conflict, is pointed at a new commit
        rather than reset, so an open pull request never shows an empty diff.
        """
        branch = PLEDGE_BRANCH_PREFIX + request.login.lower()
        base_sha = client.branch_sha(base)
        current = client.get_file(c.APPROVED_FILE, base_sha)
        message = "chore: approve contributor @%s (pledge signed)" % request.login
        wanted = c.add_login(
            current[0] if current else None, request.login, "pledge signed in %s#%d" % (repo, request.number),
        )
        existing = client.open_pull_for_branch(branch)
        if existing is not None:
            on_branch = client.get_file(c.APPROVED_FILE, branch)
            if on_branch is None or on_branch[0] != wanted:
                client.update_branch(branch, client.commit_file(base_sha, c.APPROVED_FILE, wanted, message))
            return str(existing.get("html_url") or "")
        commit = client.commit_file(base_sha, c.APPROVED_FILE, wanted, message)
        try:
            client.create_branch(branch, commit)
        except Exception as exc:  # noqa: BLE001 - 422: the branch is left from a declined pull request
            if _status(exc) != 422:
                raise
            client.update_branch(branch, commit)
        body = c.pledge_pull_request_body(
            request.login, int(request.user_id or 0), repo, request.number, request.url,
            str(signature["signed_at"]), str(signature.get("pledge_sha") or ""),
        )
        pull = client.create_pull(branch, base, message, body)
        return str(pull.get("html_url") or "")


_TEXTS: Dict[str, Dict[str, str]] = {
    "en": {
        "off": "This repository has no contributor gate; nothing to do.",
        "verify_usage": "Usage: `@review-of-legends verify <code>`, with the code the dashboard's Contributor page shows.",
        "verify_unknown": "That code is unknown, already used or expired (codes live one hour). Get a new one on the dashboard's Contributor page.",
        "verified_ok": "Verified: your GitHub account is bound to your email at `%(domain)s`. Your pull requests here are now reviewed.",
        "verified_untrusted": "Verified your email at `%(domain)s`, but that domain is not trusted in this repository; a maintainer must add you, or you can sign the contributor pledge.",
        "pledge_author": "Only the pull request's author can sign the pledge here.",
        "pledge_none": "This repository has no contributor pledge (`.github/CONTRIBUTOR_PLEDGE.md`); a maintainer must add you to the list.",
        "pledge_listed": "You are already an approved contributor here.",
        "pledge_pr": "Signature recorded. A maintainer accepts it by merging %(url)s; this pull request is reviewed after that.",
        "pledge_recorded_manual": "Signature recorded. A maintainer accepts it by adding `%(login)s` to `.github/APPROVED_CONTRIBUTORS`; this pull request is reviewed after that.",
    },
    "ko": {
        "off": "이 저장소에는 기여자 게이트가 없습니다. 처리할 것이 없습니다.",
        "verify_usage": "사용법: `@review-of-legends verify <코드>` (대시보드 Contributor 화면의 코드).",
        "verify_unknown": "알 수 없거나 이미 쓰였거나 만료된 코드입니다(유효 1시간). 대시보드 Contributor 화면에서 새로 받으세요.",
        "verified_ok": "인증 완료: GitHub 계정이 `%(domain)s` 이메일에 연결됐습니다. 이제 이 저장소의 PR이 리뷰됩니다.",
        "verified_untrusted": "`%(domain)s` 이메일은 인증됐지만 이 저장소에서 신뢰하는 도메인이 아닙니다. 메인테이너가 추가하거나 기여자 서약에 서명해 주세요.",
        "pledge_author": "서약은 이 PR의 작성자만 서명할 수 있습니다.",
        "pledge_none": "이 저장소에는 기여자 서약(`.github/CONTRIBUTOR_PLEDGE.md`)이 없습니다. 메인테이너가 명단에 추가해야 합니다.",
        "pledge_listed": "이미 승인된 기여자입니다.",
        "pledge_pr": "서명을 기록했습니다. 메인테이너가 %(url)s 를 머지하면 승인되고, 그 뒤 이 PR이 리뷰됩니다.",
        "pledge_recorded_manual": "서명을 기록했습니다. 메인테이너가 `.github/APPROVED_CONTRIBUTORS`에 `%(login)s`를 추가하면 승인되고, 그 뒤 이 PR이 리뷰됩니다.",
    },
}
