"""Talking to the bot: ``@review-of-legends`` commands in PR comments, the PR
author's review language, and the one-time welcome comment.

Nothing notifies the service of a mention: no GitHub user is called
review-of-legends, and GitHub does not deliver mentions to an App's bot. The
daemon reads each review repository's new PR comments every watch instead
(``ReviewScheduler._poll_mentions``) and answers the comments whose first line
starts with the mention (``COMMAND_LINE``), and that no one but their author
ever edited (``edited_by_others``), with the rules here:

- ``lang <code>`` / ``lang off`` / ``lang``: the commenter's own review language
  (keyed by GitHub login, lowercased), shown, set or cleared;
- ``settings`` (or ``help``, ``config``, or nothing at all): the commenter's
  settings and the command list;
- ``review`` / ``rereview`` / ``retrigger``: an author-requested fresh review;
- anything else: feedback for the maintainers.

Both kinds of comment are answered in one order, oldest update first
(``merged``), and the store keeps the newest language command
(``Request.updated``), so an older command never undoes a newer one.

``review_pr`` reads the PR author's language and appends ``language_instruction``
to the reviewer, summary and outline prompts, and posts ``welcome_body`` once per
author. Replies and the welcome are written in Korean or English.

A leaf: it imports nothing from this package but contributors.py (another
leaf), so service.py, scheduler.py and the store can all import it. It talks to nothing either; GitHub clients and the
store are passed in. Python 3.9, stdlib only (the daemon runs on /usr/bin/python3).
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import re
import urllib.parse
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from . import contributors

BOT_NAME = "review-of-legends"
WELCOME_MARKER = "<!-- review-of-legends-welcome -->"
# On every reply (its first line, so a reply never starts with the mention either).
REPLY_MARKER = "<!-- review-of-legends-mention-reply -->"

# The two places a PR comment lives: the timeline (an issue comment on the PR)
# and a review thread (a line comment).
ISSUE = "issue"
REVIEW = "review"
COMMENT_KINDS = (ISSUE, REVIEW)

# The most commands one watch answers, across repositories: each costs a
# reaction and a reply POST, which count toward GitHub's secondary limit on
# content-creating requests. The rest wait for the next watch.
MAX_COMMANDS_PER_TICK = 10
MAX_FEEDBACK_CHARS = 4000
MAX_QUOTED_CHARS = 200

REPLY_LANGUAGES = ("ko", "en")
DEFAULT_REPLY_LANGUAGE = "ko"

# A language code: 2-3 letters, optionally "-" and a 2-8 character region or
# script (zh-tw, pt-br, sr-latn). Matched lowercase.
LANGUAGE_CODE_PATTERN = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?")
LANGUAGE_ALIASES = {
    "korean": "ko",
    "한국어": "ko",
    "kr": "ko",
    "english": "en",
    "영어": "en",
    "japanese": "ja",
    "일본어": "ja",
    "chinese": "zh",
    "중국어": "zh",
}
LANGUAGE_CLEAR_WORDS = ("off", "default", "reset")
SETTINGS_WORDS = ("settings", "help", "config")
REVIEW_WORDS = ("review", "rereview", "retrigger")
LANGUAGE_NAMES = {
    "ko": "Korean",
    "en": "English",
    "ja": "Japanese",
    "zh": "Chinese",
    "zh-tw": "Traditional Chinese",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "vi": "Vietnamese",
    "th": "Thai",
    "id": "Indonesian",
}

# A command: the comment's first non-blank line starts with the mention, as a
# whole token, with or without the "[bot]" GitHub shows, indented at most 3
# spaces. CommonMark makes such a line paragraph text (or a heading or table
# header) whatever follows it: never code, a quote, a list or HTML. So no
# Markdown is parsed: an example in code, a quote or the middle of a comment
# is never a command, and a command is always a comment of its own.
COMMAND_LINE = re.compile(r"^ {0,3}@review-of-legends(?:\[bot\])?(?![\w-])", re.IGNORECASE)
LINE_ENDING = re.compile(r"\r\n|\r|\n")

# Command kinds.
# REVIEW ("review") is also the review-comment endpoint kind above; Request.kind
# and Request.command.kind are separate namespaces.
LANG = "lang"
LANG_CLEAR = "lang-clear"
LANG_SHOW = "lang-show"
LANG_INVALID = "lang-invalid"
SETTINGS = "settings"
FEEDBACK = "feedback"
# The contributor gate (gate.py): signing the pledge, binding a verified email.
PLEDGE = "pledge"
VERIFY = "verify"
VERIFY_INVALID = "verify-invalid"
CONTRIBUTOR_KINDS = (PLEDGE, VERIFY, VERIFY_INVALID)

Logger = Callable[[str], None]


class Command(NamedTuple):
    kind: str
    # LANG: the code to store; LANG_INVALID: what was given; FEEDBACK: the text.
    value: str = ""


class Request(NamedTuple):
    """One comment that addressed the bot, with everything an answer needs."""

    kind: str  # ISSUE or REVIEW
    comment_id: int
    number: int  # the pull request
    login: str  # the commenter, as GitHub spells it
    url: str  # the comment's html_url
    command: Command
    line: str  # the text after the mention on its line, as typed
    # REVIEW: the comment a reply goes under, its thread's first one (GitHub
    # takes no reply to a reply); 0 means the comment itself.
    reply_to: int = 0
    # The comment's updated_at: when what it says was written. Language
    # commands are applied in this order (DashboardStore.set_author_language).
    updated: str = ""
    # GitHub's GraphQL id of the comment: where its edit history is read (edited_by_others).
    node_id: str = ""
    # The commenter's GitHub user id (a pledge signature records it).
    user_id: int = 0


# ------------------------------------------------------------------ language


def normalize_language(value: Any) -> Optional[str]:
    """The code to store for a ``lang`` value (lowercase), or None when it is not one."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    alias = LANGUAGE_ALIASES.get(text)
    if alias:
        return alias
    return text if LANGUAGE_CODE_PATTERN.fullmatch(text) else None


def stored_language(value: Any) -> Optional[str]:
    """A language read back from the store, or None when it is absent or malformed."""
    if isinstance(value, str) and LANGUAGE_CODE_PATTERN.fullmatch(value):
        return value
    return None


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code, code)


def language_instruction(code: str) -> str:
    """The block review_pr appends to every prompt when the PR author set a language."""
    return (
        "Output language (set by the pull request author):\n"
        "Write every human-readable sentence (summaries, finding titles and bodies, verdict sentences, "
        "outline text) in %s (%s). Keep code, identifiers, file paths, commands and JSON keys unchanged, "
        "and every fixed value the output format names exactly as written, in English: "
        '"patch is correct" or "patch is incorrect", severity (high, medium, low), priority and the scores. '
        "This overrides any language the instructions above ask for." % (language_name(code), code)
    )


def default_reply_language(config: Dict[str, Any]) -> str:
    block = config.get("mentions")
    value = block.get("default_reply_language") if isinstance(block, dict) else None
    return value if value in REPLY_LANGUAGES else DEFAULT_REPLY_LANGUAGE


def reply_language(lang: Optional[str], config: Dict[str, Any]) -> str:
    """Replies: the commenter's language if ko or en, English for any other, else the default."""
    if not lang:
        return default_reply_language(config)
    return lang if lang in REPLY_LANGUAGES else "en"


def welcome_language(lang: Optional[str], config: Dict[str, Any]) -> str:
    """The welcome: the author's language if ko or en, otherwise the default reply language."""
    return lang if lang in REPLY_LANGUAGES else default_reply_language(config)


# --------------------------------------------------------------------- parse


def find_mention(body: Any) -> Optional[Tuple[str, str]]:
    """``(command, feedback text)`` when the comment starts with the mention (COMMAND_LINE), else None.

    The command is the rest of that first line as typed, trimmed; the
    feedback text runs from there to the end of the comment.
    """
    if not isinstance(body, str):
        return None
    # CommonMark's line endings only (str.splitlines also splits at U+2028 and
    # the like), and its blank lines: spaces and tabs only.
    lines = LINE_ENDING.split(body)
    for index, line in enumerate(lines):
        if not line.strip(" \t"):
            continue
        match = COMMAND_LINE.match(line)
        if match is None:
            return None
        command = line[match.end():].strip().lstrip(":,").strip()
        feedback = "\n".join([command] + lines[index + 1:]).strip()
        return command, feedback
    return None


def parse_command(command: str, feedback: str = "") -> Command:
    """What a mention asks for. Nothing after the mention reads as ``settings``."""
    words = command.split()
    if not words:
        return Command(SETTINGS)
    head = words[0].lower().rstrip(":")
    if contributors.is_pledge(command):
        return Command(PLEDGE)
    if head == "verify":
        code = contributors.parse_verify(command)
        return Command(VERIFY, code) if code else Command(VERIFY_INVALID, " ".join(words[1:]))
    if head in REVIEW_WORDS:
        return Command(REVIEW)
    if head == "lang":
        rest = words[1:]
        if not rest:
            return Command(LANG_SHOW)
        value = " ".join(rest)
        if len(rest) == 1 and rest[0].lower() in LANGUAGE_CLEAR_WORDS:
            return Command(LANG_CLEAR)
        code = normalize_language(value) if len(rest) == 1 else None
        return Command(LANG, code) if code else Command(LANG_INVALID, value)
    if len(words) == 1 and head.rstrip("?!.") in SETTINGS_WORDS:
        return Command(SETTINGS)
    text = (feedback or command).strip()
    return Command(FEEDBACK, text[:MAX_FEEDBACK_CHARS])


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _pr_number(comment: Dict[str, Any], kind: str) -> Optional[int]:
    """The pull request a comment belongs to; None for an issue comment on a plain issue."""
    if kind == REVIEW:
        url = str(comment.get("pull_request_url") or "")
        return _int(url.rstrip("/").rsplit("/", 1)[-1]) if "/pulls/" in url else None
    # github.com/<owner>/<name>/pull/<n>#issuecomment-<id>: the route, not any "/pull/" (a repository may be named pull).
    path = urllib.parse.urlparse(str(comment.get("html_url") or "")).path.strip("/").split("/")
    if len(path) < 4 or path[2] != "pull":
        return None
    return _int(path[3])


def is_bot_account(user: Any, bot_login: str = "") -> bool:
    """The service itself, or any account GitHub marks as a bot."""
    if not isinstance(user, dict):
        return True
    login = str(user.get("login") or "")
    if not login:
        return True
    if bot_login and login.lower() == bot_login.lower():
        return True
    return str(user.get("type") or "") == "Bot" or login.lower().endswith("[bot]")


def request_from(comment: Any, kind: str, bot_login: str) -> Optional[Request]:
    """The command a newly seen comment carries, or None when it is not one to answer.

    Whether someone else edited it is asked of GitHub before it is answered
    (``edited_by_others``): the REST payload names only the comment's author.
    """
    if not isinstance(comment, dict):
        return None
    comment_id = _int(comment.get("id"))
    user = comment.get("user")
    if comment_id is None or is_bot_account(user, bot_login):
        return None
    body = comment.get("body")
    if not isinstance(body, str):
        return None
    number = _pr_number(comment, kind)
    if number is None:
        return None
    found = find_mention(body)
    if found is None:
        return None
    line, feedback = found
    return Request(
        kind=kind,
        comment_id=comment_id,
        number=number,
        login=str(user["login"]),
        url=str(comment.get("html_url") or ""),
        command=parse_command(line, feedback),
        line=line,
        reply_to=(_int(comment.get("in_reply_to_id")) or comment_id) if kind == REVIEW else comment_id,
        updated=str(comment.get("updated_at") or comment.get("created_at") or ""),
        node_id=str(comment.get("node_id") or ""),
        user_id=_int(user.get("id")) or 0,
    )


def source_key(repo: str, request: Request) -> str:
    """The comment a command came from, as the store keys its feedback: ``owner/name#kind#id``.

    The two comment kinds number their ids separately, so the kind is part of it.
    """
    return "%s#%s#%d" % (repo.lower(), request.kind, request.comment_id)


# -------------------------------------------------------------------- cursor
# Per repository and comment kind, in the ledger bucket (state.json):
# ``mentions[kind] = {"since", "floor", "at_since", "seen"}``. ``since`` is the
# newest updated_at read, the listing's ?since=; GitHub's ?since= is inclusive,
# so the comments of that very second come back every time, and ``at_since``
# holds ``[id, revision]`` of those already read (``revision``: a hash of the
# body, since an edit within that second keeps its updated_at). A quiet
# repository asks the same URL and is answered 304. ``floor`` is when the cursor started: a comment created
# before it is history and is never answered. ``seen`` holds the id of every
# command answered or passed over, for good: an edit brings a comment back,
# and a command is never answered twice. Commands are rare; the comments read
# for nothing are not kept.


def github_time(timestamp: float) -> str:
    """``timestamp`` in GitHub's own shape, so cursor times compare as text."""
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_cursor(bucket: Dict[str, Any], kind: str) -> Optional[Dict[str, Any]]:
    """The cursor for ``kind``, or None before the first one; the caller holds STATE_LOCK."""
    cursors = bucket.get("mentions")
    cursor = cursors.get(kind) if isinstance(cursors, dict) else None
    if isinstance(cursor, dict) and isinstance(cursor.get("since"), str) and cursor["since"]:
        for key in ("at_since", "seen"):
            if not isinstance(cursor.get(key), list):
                cursor[key] = []
        if not isinstance(cursor.get("floor"), str):
            cursor["floor"] = cursor["since"]
        return cursor
    return None


def start_cursor(bucket: Dict[str, Any], kind: str, now: str) -> Dict[str, Any]:
    """A cursor that starts ``now``; the caller holds STATE_LOCK and saves."""
    cursors = bucket.get("mentions")
    if not isinstance(cursors, dict):
        cursors = bucket["mentions"] = {}
    cursor = {"since": now, "floor": now, "at_since": [], "seen": []}
    cursors[kind] = cursor
    return cursor


def _updated(comment: Dict[str, Any]) -> str:
    return str(comment.get("updated_at") or "")


def _revision(comment: Dict[str, Any]) -> str:
    """What a comment says, as a short hash: an edit within one second changes it, not updated_at."""
    return hashlib.sha256(str(comment.get("body") or "").encode("utf-8")).hexdigest()[:16]


def _tied(cursor: Dict[str, Any]) -> Dict[int, str]:
    """``at_since`` as {id: revision}; an entry without one (a bare id) matches no revision."""
    tied: Dict[int, str] = {}
    for entry in cursor.get("at_since") or []:
        if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], int):
            tied[entry[0]] = str(entry[1])
        elif isinstance(entry, int):
            tied[entry] = ""
    return tied


def pending(cursor: Dict[str, Any], comments: List[Any]) -> List[Dict[str, Any]]:
    """The listed comments not read at their current updated_at, oldest update first.

    Those are what the cursor moves through: a comment updated after
    ``since``, or of ``since``'s own second and not read in it as it is now.
    """
    since = str(cursor.get("since") or "")
    tied = _tied(cursor)
    fresh = []
    for comment in comments:
        if not isinstance(comment, dict) or _int(comment.get("id")) is None:
            continue
        updated = _updated(comment)
        if updated > since or (updated == since and tied.get(_int(comment.get("id"))) != _revision(comment)):
            fresh.append(comment)
    fresh.sort(key=lambda comment: (_updated(comment), _int(comment.get("id")) or 0))
    return fresh


def merged(listings: Dict[str, Tuple[Dict[str, Any], List[Any]]]) -> List[Tuple[str, Dict[str, Any]]]:
    """``(kind, comment)`` for both kinds' pending comments, in one order: oldest update first.

    ``listings`` maps a kind to its cursor and its listing. A listing cut short
    (a ``truncated`` attribute, service.CommentListing) holds every comment up
    to its last one only: past that time the other kind waits, or a newer
    command of it could be applied before an older one not listed yet.
    """
    horizon = ""
    for _, listing in listings.values():
        if getattr(listing, "truncated", False) and listing:
            last = max(_updated(comment) for comment in listing if isinstance(comment, dict))
            horizon = min(horizon, last) if horizon else last
    order = {kind: index for index, kind in enumerate(COMMENT_KINDS)}
    items = []
    for kind, (cursor, listing) in listings.items():
        cut = horizon and not getattr(listing, "truncated", False)
        for comment in pending(cursor, listing):
            if horizon and (_updated(comment) > horizon or (cut and _updated(comment) == horizon)):
                continue
            items.append((kind, comment))
    items.sort(key=lambda item: (_updated(item[1]), order.get(item[0], len(order)), _int(item[1].get("id")) or 0))
    return items


def is_answered(cursor: Dict[str, Any], comment: Dict[str, Any]) -> bool:
    return _int(comment.get("id")) in set(cursor.get("seen") or [])


def is_history(cursor: Dict[str, Any], comment: Dict[str, Any]) -> bool:
    created = str(comment.get("created_at") or "")
    return bool(created) and created < str(cursor.get("floor") or "")


def advance(cursor: Dict[str, Any], comment: Dict[str, Any]) -> None:
    """Move the cursor through ``comment``, read; the caller holds STATE_LOCK."""
    comment_id = _int(comment.get("id"))
    updated = _updated(comment)
    if updated > str(cursor.get("since") or ""):
        cursor["since"] = updated
        cursor["at_since"] = []
    if updated == cursor.get("since") and comment_id is not None:
        tied = _tied(cursor)
        tied[comment_id] = _revision(comment)
        cursor["at_since"] = [[key, value] for key, value in tied.items()]


def mark_answered(cursor: Dict[str, Any], comment: Dict[str, Any]) -> None:
    """Never answer ``comment`` again, and move the cursor through it; the caller holds STATE_LOCK."""
    comment_id = _int(comment.get("id"))
    seen = cursor.setdefault("seen", [])
    if comment_id is not None and comment_id not in seen:
        seen.append(comment_id)
    advance(cursor, comment)


def snapshot(cursor: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy({key: cursor.get(key) for key in ("since", "at_since", "seen")})


def restore(cursor: Dict[str, Any], saved: Dict[str, Any]) -> None:
    cursor.update(copy.deepcopy(saved))


def same_comment(snapshot: Any, comment: Dict[str, Any]) -> bool:
    """Whether GitHub's current copy (GitHubClient.comment_edits) is the comment as listed:
    the same body, author and updated_at. Only then do its edit history and
    the command parsed from the listing describe the same text."""
    if not isinstance(snapshot, dict):
        return False
    author = snapshot.get("author")
    login = str((comment.get("user") or {}).get("login") or "")
    return (
        snapshot.get("body") == comment.get("body")
        and snapshot.get("updated_at") == comment.get("updated_at")
        and isinstance(author, str) and author.lower() == login.lower()
    )


def edited_by_others(edits: Any, login: str) -> bool:
    """Whether anyone but ``login`` ever edited the comment (GitHubClient.comment_edits).

    GitHub keeps a comment's author when a collaborator edits it, so such a
    comment could speak for someone else: it is never a command. Every edit
    counts, not only the last, and an edit history GitHub cut short, an
    unknown editor or an unreadable answer counts as someone else.
    """
    if not isinstance(edits, dict):
        return True
    if not edits.get("edited"):
        return False
    editors = edits.get("editors")
    if not edits.get("complete") or not isinstance(editors, list):
        return True
    return any(not isinstance(editor, str) or editor.lower() != login.lower() for editor in editors)


# ------------------------------------------------------------------ commands


def apply_command(
    store: Any, repo: str, request: Request, config: Dict[str, Any],
    review: Optional[Callable[[Request], Tuple[str, str]]] = None,
    contributor: Optional[Callable[[Request], str]] = None,
) -> str:
    """Do what the command asks in the store and return the reply body.

    A store error propagates: the caller leaves the comment unread, so the next
    watch tries it again, and nothing has been posted for it yet. Applying one
    comment twice changes nothing (the caller does when it could not save its
    cursor): a language is set to the same value again, and feedback is keyed
    by the comment (``source_key``). The injected review handler uses a durable
    receipt for the same key, committed with its queue row. Its read failures
    also propagate, so GitHub outages leave a review command unread.
    A language command older than the one
    that set the author's language (``Request.updated``, from any repository;
    the comment's ``source_key`` breaks a tie within one second) changes
    nothing, and the reply says which language stands.
    """
    command = request.command
    if command.kind in CONTRIBUTOR_KINDS:
        # The contributor gate (gate.Gatekeeper.handle_command) answers these itself.
        if contributor is None:
            raise RuntimeError("contributor command handler is unavailable")
        return contributor(request)
    if command.kind in (LANG, LANG_CLEAR):
        value = command.value if command.kind == LANG else None
        row = store.set_author_language(
            request.login, value, request.url,
            source_at=request.updated or None, source_key=source_key(repo, request),
        )
        lang = stored_language(row.get("lang")) if isinstance(row, dict) else value
        if isinstance(row, dict) and row.get("lang_comment_url") != (request.url or None):
            # A newer command of theirs stands (the store keeps the newest): say so.
            return reply_body(request, lang, reply_language(lang, config), superseded=True)
    else:
        row = store.get_pr_author(request.login)
        lang = stored_language(row.get("lang")) if isinstance(row, dict) else None
        if command.kind == REVIEW:
            if review is None:
                raise RuntimeError("review command handler is unavailable")
            outcome, head = review(request)
            language = reply_language(lang, config)
            message = _REVIEW_REPLIES[language][outcome]
            if outcome in {"queued", "answered"}:
                message = message % _code(head[:12])
            return _reply(request, message)
        if command.kind == FEEDBACK:
            store.add_feedback(
                repo, request.number, request.login, command.value, request.url,
                source_key=source_key(repo, request),
            )
    return reply_body(request, lang, reply_language(lang, config))


def _never(exc: Exception) -> bool:
    return False


def acknowledge(
    client: Any,
    request: Request,
    body: str,
    log: Logger,
    repo: str,
    refused: Callable[[Exception], bool] = _never,
) -> None:
    """A thumbs-up on the comment and a reply: each tried once, a failure logged, never retried.

    A failure ``refused`` calls a rate limit is raised at once, unlogged, with
    nothing more sent (no reply after a refused reaction): the caller stops
    answering for this watch and backs off.
    """
    label = "%s pr #%d" % (repo, request.number)
    try:
        if request.kind == REVIEW:
            client.react_to_review_comment(request.comment_id, "+1")
        else:
            client.react_to_issue_comment(request.comment_id, "+1")
    except Exception as exc:  # noqa: BLE001 - an acknowledgement never stops the watcher, but a rate limit does
        if refused(exc):
            raise
        log("%s: WARNING reacting to comment %d failed: %s" % (label, request.comment_id, exc))
    try:
        if request.kind == REVIEW:
            client.reply_to_review_comment(request.number, request.reply_to or request.comment_id, body)
        else:
            client.create_issue_comment(request.number, body)
    except Exception as exc:  # noqa: BLE001 - see above
        if refused(exc):
            raise
        log("%s: WARNING replying to comment %d failed: %s" % (label, request.comment_id, exc))


# ----------------------------------------------------------------- templates


def _code(text: str) -> str:
    """User text as inline code: backticks dropped and length bounded, so it can
    neither break out of the span nor @-mention anyone through the bot."""
    cleaned = " ".join(str(text).replace("`", "'").split())
    if len(cleaned) > MAX_QUOTED_CHARS:
        cleaned = cleaned[: MAX_QUOTED_CHARS - 3] + "..."
    return "`%s`" % (cleaned or " ")


def _described(code: str) -> str:
    return "`%s` (%s)" % (code, language_name(code))


COMMAND_LIST = {
    "ko": [
        "- `@review-of-legends review`: 이 PR의 현재 커밋을 다시 리뷰 (작성자만, 커밋당 1회).",
        "- `@review-of-legends lang <코드>`: 내 PR 리뷰를 그 언어로 받습니다. 예: `ko`, `en`, `ja`, `zh`, `zh-tw` "
        "(또는 korean, english, japanese, chinese, 한국어, 영어, 일본어, 중국어).",
        "- `@review-of-legends lang off`: 언어 설정을 지우고 서비스 기본값으로 돌아갑니다. "
        "`@review-of-legends lang`은 지금 설정을 보여줍니다.",
        "- `@review-of-legends settings`: 내 설정과 이 명령 목록을 보여줍니다.",
        "- `@review-of-legends <아무 내용>`: 메인테이너에게 피드백을 남깁니다.",
    ],
    "en": [
        "- `@review-of-legends review`: a fresh review of this PR's current commit (author only, once per commit).",
        "- `@review-of-legends lang <code>`: reviews of your pull requests in that language, e.g. `ko`, `en`, "
        "`ja`, `zh`, `zh-tw` (or korean, english, japanese, chinese).",
        "- `@review-of-legends lang off`: clears it; reviews go back to the service default. "
        "`@review-of-legends lang` shows the current one.",
        "- `@review-of-legends settings`: your settings and this list.",
        "- `@review-of-legends <any text>`: feedback to the maintainers.",
    ],
}

_REVIEW_REPLIES = {
    "ko": {
        "author": "댓글로 재리뷰를 요청할 수 있는 사람은 PR 작성자뿐입니다. 메인테이너는 리뷰의 Retrigger 배지를 이용해 주세요.",
        "closed": "닫혔거나 병합된 PR은 다시 리뷰할 수 없습니다.",
        "draft": "드래프트 PR은 다시 리뷰할 수 없습니다.",
        "busy": "이 PR의 리뷰가 이미 대기 중이거나 실행 중입니다.",
        "unknown": "이 커밋에는 아직 완료된 리뷰가 없습니다. 이 커밋의 리뷰가 진행될 예정이니 기다려 주세요.",
        "posting": "이전 GitHub 리뷰 게시 결과를 확인할 수 없습니다. 메인테이너가 게시 여부를 확인한 뒤 다시 요청해 주세요.",
        "limit": "이 커밋의 댓글 재리뷰는 이미 한 번 요청했습니다. 새 커밋을 푸시하거나 메인테이너에게 요청해 주세요.",
        "queued": "%s 커밋의 새로운 리뷰를 대기열에 등록했습니다.",
        "answered": "%s 커밋의 리뷰가 이 댓글과 같은 시각이나 그 뒤에 끝나, 이 요청은 이미 처리되었습니다.",
    },
    "en": {
        "author": "Only the PR author can request a fresh review by comment. Maintainers can use the Retrigger badge on the review.",
        "closed": "This PR is closed or merged and cannot be reviewed again.",
        "draft": "This PR is a draft and cannot be reviewed again.",
        "busy": "A review of this PR is already queued or running.",
        "unknown": "This head has no completed review yet. A review of this head is on its way; please wait.",
        "posting": "The previous GitHub review posting outcome is unknown. Ask a maintainer to reconcile it before requesting another review.",
        "limit": "A comment-requested review has already been used for this head. Please push a commit or ask a maintainer.",
        "queued": "A fresh review of %s is queued.",
        "answered": "A review of %s that completed at or after your comment already answers this request.",
    },
}

_REPLIES = {
    "ko": {
        LANG: "설정했습니다. 앞으로 내 PR의 리뷰는 %s로 작성됩니다. 되돌리려면 `@review-of-legends lang off`.",
        LANG_CLEAR: "언어 설정을 지웠습니다. 앞으로 내 PR의 리뷰는 서비스 기본 언어로 작성됩니다.",
        "current": "지금 리뷰 언어: %s.",
        "unset": "리뷰 언어를 설정하지 않았습니다. 리뷰는 서비스 기본 언어로 작성됩니다.",
        LANG_INVALID: (
            "%s는 받을 수 있는 언어 코드가 아니어서 아무것도 바꾸지 않았습니다. 2-3자 언어 코드에 선택적으로 "
            "`-`와 지역·문자 코드(2-8자)를 붙여 쓰세요. 예: `ko`, `en`, `ja`, `zh`, `zh-tw`. "
            "korean, english, japanese, chinese, 한국어, 영어, 일본어, 중국어도 됩니다. 지우려면 `lang off`."
        ),
        "superseded": "더 최근에 남긴 언어 명령이 이미 적용되어 있어 바꾸지 않았습니다.",
        SETTINGS: "내 설정:",
        "commands": "명령 (PR 댓글이나 리뷰 댓글의 첫 줄에, 댓글 하나에 하나씩):",
        FEEDBACK: "피드백을 기록했습니다. 메인테이너가 대시보드에서 확인합니다. 감사합니다.",
    },
    "en": {
        LANG: "Done: reviews of your pull requests are now written in %s. `@review-of-legends lang off` undoes it.",
        LANG_CLEAR: "Done: your language setting is cleared. Reviews of your pull requests use the service default again.",
        "current": "Your review language: %s.",
        "unset": "You have not set a review language; reviews use the service default.",
        LANG_INVALID: (
            "%s is not a language code I accept, so nothing changed. Use 2-3 letters, optionally followed by `-` "
            "and a 2-8 character region or script, e.g. `ko`, `en`, `ja`, `zh`, `zh-tw`; korean, english, "
            "japanese and chinese work too. `lang off` clears it."
        ),
        "superseded": "Nothing changed: a newer language command of yours already applies.",
        SETTINGS: "Your settings:",
        "commands": "Commands (the first line of any PR comment or review comment, one per comment):",
        FEEDBACK: "Thanks: your feedback is recorded for the maintainers, who read it on the dashboard.",
    },
}


def _language_line(lang: Optional[str], texts: Dict[str, str]) -> str:
    return texts["current"] % _described(lang) if lang else texts["unset"]


def reply_body(request: Request, lang: Optional[str], language: str, superseded: bool = False) -> str:
    """The reply to one command: @-mentions the commenter and quotes what they wrote."""
    texts = _REPLIES[language if language in _REPLIES else "en"]
    kind = request.command.kind
    if superseded:
        message = texts["superseded"] + " " + _language_line(lang, texts)
    elif kind == LANG:
        message = texts[LANG] % _described(request.command.value)
    elif kind == LANG_CLEAR:
        message = texts[LANG_CLEAR]
    elif kind == LANG_SHOW:
        message = _language_line(lang, texts)
    elif kind == LANG_INVALID:
        message = texts[LANG_INVALID] % _code(request.command.value)
    elif kind == SETTINGS:
        message = "\n".join(
            [texts[SETTINGS], "- " + _language_line(lang, texts), "", texts["commands"]]
            + COMMAND_LIST[language if language in COMMAND_LIST else "en"]
        )
    else:
        message = texts[FEEDBACK]
    return _reply(request, message)


def _reply(request: Request, message: str) -> str:
    quoted = "@%s %s" % (BOT_NAME, request.line) if request.line else "@" + BOT_NAME
    return "\n".join([REPLY_MARKER, "@%s" % request.login, "", "> %s" % _code(quoted), "", message])


_WELCOME = {
    "ko": [
        "안녕하세요 @{login}, Review of Legends 리뷰 봇입니다. 하는 일과 설정할 수 있는 것을 한 번만 안내드립니다.",
        "",
        "**하는 일**",
        "- PR이 열리거나 리뷰 준비(ready for review)가 되면 약 20초 안에 리뷰를 시작합니다. 드래프트는 건너뜁니다. "
        "새 커밋을 올리면 그 커밋을 다시 리뷰합니다.",
        "- `review-of-legends/reviewed/<branch>` 체크: 이 PR에 리뷰가 한 번이라도 올라갔다는 뜻입니다(그 뒤의 "
        "커밋에도 바로 성공으로 붙습니다).",
        "- `review-of-legends/required/<branch>` 체크: 필수 리뷰어가 있는 저장소에서, 이 커밋의 필수 리뷰어가 통과했는지 "
        "(성공), 문제를 찾았는지(실패), 끝내지 못했는지(오류)를 보여줍니다.",
        "",
        "**설정과 피드백** (PR 댓글이나 리뷰 댓글의 첫 줄에, 댓글 하나에 하나씩)",
        "```",
        "@review-of-legends lang ko",
        "@review-of-legends lang en",
        "@review-of-legends lang off",
        "@review-of-legends settings",
        "@review-of-legends review",
        "@review-of-legends 리뷰에 대한 의견",
        "```",
        "- `lang <코드>`: 내 PR의 리뷰를 그 언어로 받습니다 (`ko`, `en`, `ja`, `zh`, `zh-tw` 등). `lang off`로 지웁니다.",
        "- `settings`: 내 설정과 명령 목록을 답글로 보여줍니다.",
        "- `review`: 이 PR의 현재 커밋을 다시 리뷰 (작성자만, 커밋당 1회). 메인테이너는 리뷰의 Retrigger 배지를 이용하세요.",
        "- 그 밖의 내용은 메인테이너에게 피드백으로 전달됩니다.",
        "",
        "봇이 GitHub 앱이라 `@review-of-legends`가 자동완성되지 않습니다. 그대로 입력하면 됩니다. 봇이 약 20초 안에 댓글을 "
        "읽고 👍와 답글로 확인해 드립니다.",
    ],
    "en": [
        "Hi @{login}, this is the Review of Legends review bot. Here is, once, what it does and what you can set.",
        "",
        "**What it does**",
        "- A review starts within about 20 seconds of a pull request opening or becoming ready for review; "
        "drafts are skipped. Every new push is reviewed again.",
        "- The `review-of-legends/reviewed/<branch>` check means a review has been posted on this pull request "
        "(later pushes get it at once).",
        "- The `review-of-legends/required/<branch>` check, in repositories with required reviewers, says whether "
        "they passed this commit (success), found a problem (failure) or did not finish (error).",
        "",
        "**Settings and feedback** (the first line of any PR comment or review comment, one per comment)",
        "```",
        "@review-of-legends lang ko",
        "@review-of-legends lang en",
        "@review-of-legends lang off",
        "@review-of-legends settings",
        "@review-of-legends review",
        "@review-of-legends your feedback about the reviews",
        "```",
        "- `lang <code>`: reviews of your pull requests in that language (`ko`, `en`, `ja`, `zh`, `zh-tw`, ...); "
        "`lang off` clears it.",
        "- `settings`: replies with your settings and the command list.",
        "- `review`: a fresh review of this PR's current commit (author only, once per commit). Maintainers can use the Retrigger badge on the review.",
        "- Anything else is sent to the maintainers as feedback.",
        "",
        "GitHub does not autocomplete `@review-of-legends` because the bot is an app; typing it is enough. "
        "The bot reads new comments within about 20 seconds and confirms with a 👍 and a reply.",
    ],
}


def welcome_body(login: str, language: str) -> str:
    lines = _WELCOME[language if language in _WELCOME else "en"]
    return "\n".join([WELCOME_MARKER] + [line.replace("{login}", login) for line in lines])


def pr_author(pr: Any) -> Tuple[str, bool]:
    """``(login, is a bot)`` of a pull request's author; ``("", True)`` when unknown."""
    user = pr.get("user") if isinstance(pr, dict) else None
    if not isinstance(user, dict) or not str(user.get("login") or ""):
        return "", True
    return str(user["login"]), is_bot_account(user)
