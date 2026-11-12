"""Who may have a pull request reviewed: the contributor gate.

A public repository takes pull requests from anyone. With the gate on
(``contributor_gate.enabled``), the daemon reviews a pull request, and approves
its waiting fork CI runs, only when the author is an approved contributor:

- a repository collaborator with write, maintain or admin permission;
- a login listed in ``.github/APPROVED_CONTRIBUTORS`` on the default branch
  (one per line, ``#`` comments; the format herdr uses);
- a login whose verified email is at a trusted domain: a ``*@example.com`` line
  in ``APPROVED_CONTRIBUTORS`` or ``contributor_gate.trusted_email_domains``.
  The email is proven on the dashboard (Google sign-in); the code it issues
  names the GitHub login it is for, and only that login's
  ``@review-of-legends verify <code>`` comment binds it, to the account's
  immutable user id. A commit email is never evidence, since anyone can set one;
- a configured bot (``contributor_gate.trusted_bots``) or a login the
  deployment approves everywhere (``contributor_gate.approved_logins``).

Everyone else gets one gate comment that explains the two ways in: domain
verification, and signing ``.github/CONTRIBUTOR_PLEDGE.md`` by commenting
``PLEDGE_SENTENCE``. A signature makes the daemon open a pull request that adds
the login to ``APPROVED_CONTRIBUTORS``; merging it is the maintainer's
acceptance.

A leaf like mentions.py: no imports from this package, no I/O. GitHub calls and
the store are passed in. Python 3.9, stdlib only.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

APPROVED_FILE = ".github/APPROVED_CONTRIBUTORS"
PLEDGE_FILE = ".github/CONTRIBUTOR_PLEDGE.md"
GATE_MARKER = "<!-- review-of-legends-contributor-gate -->"
WRITE_PERMISSIONS = ("admin", "maintain", "write")
DEFAULT_TRUSTED_BOTS = ("dependabot[bot]", "renovate[bot]", "github-actions[bot]")

# The sentence a contributor comments to sign the pledge. Compared after
# normalize_sentence: case, runs of whitespace and a closing period do not matter.
PLEDGE_SENTENCE = "I have read the Contributor Pledge and I agree to it."
VERIFY_CODE_PREFIX = "rol-"
VERIFY_CODE_TTL_SECONDS = 3600

# A GitHub login: letters, digits and single hyphens, at most 39 characters;
# a GitHub App's bot account adds "[bot]" (zhuge-bot[bot]).
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}(?:\[bot\])?")
# A domain line: *@example.com (an IDN must be written in punycode).
DOMAIN_LINE = re.compile(r"\*@((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})")
VERIFY_CODE = re.compile(r"rol-[a-z0-9]{6,32}")


class ContributorList(NamedTuple):
    """One parsed list file: lowercased logins and email domains."""

    logins: frozenset
    domains: frozenset
    # Lines that are neither: reported by doctor, never trusted.
    invalid: Tuple[str, ...] = ()


class Decision(NamedTuple):
    trusted: bool
    # Why, for logs, the gate comment and the dashboard: "write permission",
    # "approved list", "maintainer", "verified email at example.com",
    # "trusted bot", or for an untrusted author "not an approved contributor".
    reason: str


class Identity(NamedTuple):
    """A GitHub account bound to an email proven on the dashboard."""

    login: str
    email: str
    # GitHub's immutable user id: a renamed or reassigned login never inherits the binding.
    user_id: int = 0


def parse_list(text: Optional[str]) -> ContributorList:
    """Parse an APPROVED_CONTRIBUTORS file. A missing file is an empty list."""
    logins: Set[str] = set()
    domains: Set[str] = set()
    invalid: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        lowered = line.lower()
        domain = DOMAIN_LINE.fullmatch(lowered)
        if domain:
            domains.add(domain.group(1))
        elif LOGIN.fullmatch(line.lstrip("@")):
            logins.add(line.lstrip("@").lower())
        else:
            invalid.append(line)
    return ContributorList(frozenset(logins), frozenset(domains), tuple(invalid))


def normalize_domain(value: Any) -> Optional[str]:
    """``example.com`` from ``example.com``, ``@example.com`` or ``*@example.com``; None if not a domain."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    if not candidate.startswith("*@"):
        candidate = "*@" + candidate.lstrip("@")
    match = DOMAIN_LINE.fullmatch(candidate)
    return match.group(1) if match else None


def email_domain(email: Any) -> Optional[str]:
    if not isinstance(email, str) or email.count("@") != 1:
        return None
    return normalize_domain(email.rsplit("@", 1)[1])


def gate_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    """The validated ``contributor_gate`` block with defaults. Raises ValueError on a bad shape."""
    raw = config.get("contributor_gate")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("config.contributor_gate must be an object")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("config.contributor_gate.enabled must be true or false")
    domains: List[str] = []
    listed = raw.get("trusted_email_domains", [])
    if not isinstance(listed, list):
        raise ValueError("config.contributor_gate.trusted_email_domains must be a list of domains")
    for value in listed:
        domain = normalize_domain(value)
        if domain is None:
            raise ValueError("config.contributor_gate.trusted_email_domains: %r is not a domain" % (value,))
        domains.append(domain)
    bots = raw.get("trusted_bots", list(DEFAULT_TRUSTED_BOTS))
    if not isinstance(bots, list) or not all(isinstance(b, str) and b.strip() for b in bots):
        raise ValueError("config.contributor_gate.trusted_bots must be a list of logins")
    approve_ci = raw.get("approve_fork_ci", True)
    if not isinstance(approve_ci, bool):
        raise ValueError("config.contributor_gate.approve_fork_ci must be true or false")
    pledge_pr = raw.get("open_pledge_pull_request", True)
    if not isinstance(pledge_pr, bool):
        raise ValueError("config.contributor_gate.open_pledge_pull_request must be true or false")
    logins = raw.get("approved_logins", []) or []
    if not isinstance(logins, list) or not all(isinstance(x, str) and LOGIN.fullmatch(x.strip().lstrip("@")) for x in logins):
        raise ValueError("config.contributor_gate.approved_logins must be a list of GitHub logins")
    repos = raw.get("repositories")
    if repos is not None and (not isinstance(repos, list) or not all(isinstance(r, str) and "/" in r for r in repos)):
        raise ValueError("config.contributor_gate.repositories must be a list of owner/name")
    return {
        "enabled": enabled,
        "trusted_email_domains": sorted(set(domains)),
        "trusted_bots": sorted({b.strip().lower() for b in bots}),
        # Logins the deployment approves in every gated repository (beside each
        # repository's own APPROVED_CONTRIBUTORS): for private repositories
        # that carry no list file.
        "approved_logins": sorted({x.strip().lstrip("@").lower() for x in logins}),
        "approve_fork_ci": approve_ci,
        "open_pledge_pull_request": pledge_pr,
        # None: every review repository; a list: only these (lowercased).
        "repositories": None if repos is None else sorted({r.strip().lower() for r in repos}),
    }


def gate_applies(settings: Dict[str, Any], repo: str) -> bool:
    if not settings.get("enabled"):
        return False
    only = settings.get("repositories")
    return only is None or repo.lower() in only


def decide(
    login: str,
    *,
    permission: Optional[str],
    approved: ContributorList,
    trusted_domains: Iterable[str],
    identity: Optional[Identity],
    user_id: int = 0,
) -> Decision:
    """Whether ``login`` is an approved contributor, and why.

    ``permission`` is GitHub's base collaborator permission for the login
    (admin, write, read or none; None when unknown). ``identity`` is the
    dashboard-proven email bound to the account, if any; it counts only for the
    account it was bound to (``user_id``, when both sides know it).
    """
    lowered = (login or "").lower()
    if not lowered:
        return Decision(False, "no author")
    if permission in WRITE_PERMISSIONS:
        return Decision(True, "%s permission" % permission)
    if lowered in approved.logins:
        return Decision(True, "approved list")
    domains = set(approved.domains) | {d.lower() for d in trusted_domains}
    same_account = identity is not None and (
        identity.user_id and user_id and identity.user_id == user_id
        or not (identity.user_id and user_id) and identity.login.lower() == lowered
    )
    if identity is not None and same_account:
        domain = email_domain(identity.email)
        if domain and domain in domains:
            return Decision(True, "verified email at %s" % domain)
    return Decision(False, "not an approved contributor")


# -------------------------------------------------------------- pledge / verify


def normalize_sentence(text: str) -> str:
    return " ".join(text.lower().split()).rstrip(".!").strip()


def is_pledge(text: str) -> bool:
    """Whether a mention's command text is the pledge sentence."""
    return normalize_sentence(text) == normalize_sentence(PLEDGE_SENTENCE)


def new_verify_code() -> str:
    """A one-time code the dashboard shows a signed-in contributor."""
    return VERIFY_CODE_PREFIX + secrets.token_hex(8)


def code_hash(code: str) -> str:
    """Codes are stored hashed: a leaked store does not hand out bindings."""
    return hashlib.sha256(code.strip().lower().encode("utf-8")).hexdigest()


def parse_verify(command: str) -> Optional[str]:
    """The code in ``verify <code>``, or None."""
    words = command.split()
    if len(words) != 2 or words[0].lower().rstrip(":") != "verify":
        return None
    code = words[1].strip().strip("`").lower()
    return code if VERIFY_CODE.fullmatch(code) else None


def add_login(text: Optional[str], login: str, comment: str) -> str:
    """APPROVED_CONTRIBUTORS with ``login`` appended (with a trailing ``# comment``).

    The file's other lines are kept byte for byte; a missing file starts with a
    header. Raises ValueError when the login is already listed or not a login.
    """
    if not LOGIN.fullmatch(login):
        raise ValueError("not a GitHub login: %r" % login)
    current = text or (
        "# GitHub logins whose pull requests are reviewed, one per line.\n"
        "# A line *@example.com approves anyone who verified an email at that domain.\n"
    )
    if login.lower() in parse_list(current).logins:
        raise ValueError("%s is already listed" % login)
    note = " ".join(comment.split())
    line = "%s  # %s" % (login, note) if note else login
    if current and not current.endswith("\n"):
        current += "\n"
    return current + line + "\n"


def login_note(text: Optional[str], login: str) -> Optional[str]:
    """The ``# comment`` on ``login``'s line ("" when it has none); None when the login is not listed."""
    for raw in (text or "").splitlines():
        entry, _, comment = raw.partition("#")
        if entry.strip().lstrip("@").lower() == login.lower():
            return " ".join(comment.split())
    return None


# ------------------------------------------------------------------ comments


def gate_comment(
    login: str,
    repo: str,
    default_branch: str,
    *,
    domains: Iterable[str],
    pledge: bool,
    dashboard_url: str = "",
    lang: str = "en",
) -> str:
    """The one comment an unapproved author's pull request gets (GATE_MARKER, upserted)."""
    domain_list = ", ".join("`@%s`" % d for d in sorted(domains))
    blob = "https://github.com/%s/blob/%s/" % (repo, default_branch)
    verify = "@review-of-legends verify <code>"
    if lang == "ko":
        lines = [
            GATE_MARKER,
            "@%s 님, 이 저장소는 **승인된 기여자**의 PR만 리뷰하고 CI를 실행합니다." % login,
            "승인된 기여자 명단: [`%s`](%s%s)." % (APPROVED_FILE, blob, APPROVED_FILE),
            "",
            "참여 방법:",
        ]
        if domain_list and dashboard_url:
            lines.append(
                "- **회사 이메일 인증** (%s): [대시보드](%s/#contributor)에 Google로 로그인해 GitHub 아이디 `%s`용 코드를 받은 뒤, "
                "이 PR에 `@review-of-legends verify <코드>` 를 댓글로 남기세요." % (domain_list, dashboard_url.rstrip("/"), login)
            )
        if pledge:
            lines.append(
                "- **기여자 서약**: [`%s`](%s%s)를 읽고, 이 PR에 아래 한 줄을 댓글로 남기세요. "
                "서명이 기록되고 명단 추가 PR이 열리며, 메인테이너가 머지하면 승인됩니다." % (PLEDGE_FILE, blob, PLEDGE_FILE)
            )
            lines += ["", "```", "@review-of-legends " + PLEDGE_SENTENCE, "```"]
        if not (pledge or (domain_list and dashboard_url)):
            lines.append("- 메인테이너가 명단에 추가하면 이 PR은 자동으로 리뷰됩니다.")
        return "\n".join(lines) + "\n"
    lines = [
        GATE_MARKER,
        "Hi @%s, this repository reviews pull requests, and runs their CI, only for **approved contributors**." % login,
        "The list is [`%s`](%s%s)." % (APPROVED_FILE, blob, APPROVED_FILE),
        "",
        "Ways in:",
    ]
    if domain_list and dashboard_url:
        lines.append(
            "- **Verify your company email** (%s): sign in to the [dashboard](%s/#contributor) with Google, "
            "get a code for the GitHub login `%s`, and comment `%s` on this pull request." % (domain_list, dashboard_url.rstrip("/"), login, verify)
        )
    if pledge:
        lines.append(
            "- **Sign the contributor pledge**: read [`%s`](%s%s), then comment the line below on this pull request. "
            "Your signature is recorded and a pull request adding you to the list is opened; "
            "a maintainer accepts it by merging." % (PLEDGE_FILE, blob, PLEDGE_FILE)
        )
        lines += ["", "```", "@review-of-legends " + PLEDGE_SENTENCE, "```"]
    if not (pledge or (domain_list and dashboard_url)):
        lines.append("- A maintainer can add you to the list; this pull request is then reviewed automatically.")
    return "\n".join(lines) + "\n"


def approved_comment(login: str, reason: str, lang: str = "en") -> str:
    """What the gate comment becomes once the author is approved."""
    if lang == "ko":
        return "%s\n@%s 님은 승인된 기여자입니다 (%s). 이 PR을 리뷰합니다.\n" % (GATE_MARKER, login, reason)
    return "%s\n@%s is an approved contributor (%s). This pull request will be reviewed.\n" % (GATE_MARKER, login, reason)


def pledge_pull_request_body(
    login: str, user_id: int, repo: str, number: int, comment_url: str, signed_at: str, pledge_sha: str
) -> str:
    return "\n".join(
        [
            "@%s signed the [contributor pledge](%s) on %s#%d." % (login, PLEDGE_FILE, repo, number),
            "",
            "- Login: `%s` (GitHub user id %d)" % (login, user_id),
            "- Signature: %s" % comment_url,
            "- Signed at: %s" % signed_at,
            "- Pledge version: `%s` (blob SHA of `%s` on the default branch)" % (pledge_sha, PLEDGE_FILE),
            "",
            "Merging this pull request approves @%s as a contributor: their pull requests are then reviewed and their fork CI runs approved." % login,
            "Close it to decline.",
        ]
    ) + "\n"
