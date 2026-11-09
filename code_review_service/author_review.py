"""Admission of PR-author comment rereviews; the runner owns execution checks.

Kept outside mentions.py so its parser/templates remain a stdlib-only leaf.
"""

from typing import Any, Callable, Dict, Optional, Tuple

from . import dashboard_execution as execution
from . import mentions
from .dashboard_store import (
    AuthorReviewLimitError, PrBusyError, RETRY_KIND_OPTION, RETRY_KIND_REREVIEW,
    RETRY_SCOPE_ALL, RETRY_SCOPE_OPTION, RETRY_SEEN_AT_OPTION, RETRY_SOURCE_KEY_OPTION,
)


def apply(
    store: Any, repo: str, request: mentions.Request, client: Any,
    ledger_lookup: Callable[[str, int], Optional[Dict[str, Any]]],
) -> Tuple[str, str]:
    """Return a localized reply key and head; unreadable checks propagate.

    One live GET verifies ownership and the current PR/head. Wrap read errors
    (including ValueError) so the poller never marks an unreadable command as
    permanently invalid input. No acknowledgement is sent until admission and
    the comment cursor have both been saved.
    """
    try:
        pr = client.get_pr(request.number)
        author, _ = mentions.pr_author(pr)
        if not author:
            raise RuntimeError("GitHub returned no PR author")
    except ValueError as exc:
        raise RuntimeError("could not read the live PR for a review command") from exc
    if author.lower() != request.login.lower():
        return "author", ""
    source = mentions.source_key(repo, request)
    previous = store.comment_review_request(source)
    if previous is not None:
        return "queued", previous["head_sha"]
    if pr.get("state") != "open" or pr.get("merged"):
        return "closed", ""
    if pr.get("draft"):
        return "draft", ""
    # Refuse busy PRs before further admission work; enqueue still guards races atomically.
    active, running = store.active_activity_for_pr(repo, request.number)
    if active is not None or running is not None:
        return "busy", ""
    head = str((pr.get("head") or {}).get("sha") or "")
    if not head:
        raise RuntimeError("GitHub returned no PR head")
    latest = store.latest_attempt_for_head(repo, request.number, head)
    entry = ledger_lookup(repo, request.number)
    same_entry = entry if entry and entry.get("head_sha") == head else None
    if execution.posting_outcome_unknown(latest) or (same_entry and same_entry.get("post_outcome_unknown")):
        return "posting", head
    reviewed = same_entry and not same_entry.get("initial_baseline_without_review")
    completed = store.latest_completed_review(repo, request.number, head)
    if not reviewed and completed is None:
        return "unknown", head
    # Never substitute admission time: that could allow a review posted after
    # the comment but before this watch to be posted again by the late check.
    if not request.updated:
        raise RuntimeError("review comment has no timestamp")
    finished_at = str(completed.get("updated_at") or "") if completed else ""
    if finished_at:
        try:
            answered = execution.review_finished_at_or_after(
                finished_at, request.updated, whole_seconds="." not in finished_at and "," not in finished_at,
            )
        except (ValueError, TypeError) as exc:
            raise RuntimeError("could not compare review completion with comment timestamp") from exc
        if answered:
            return "answered", head
    # The poll's recorder is best effort, so a posted review can lack its
    # completed run; the late check would refuse this request as stale.
    try:
        answered = execution.ledger_reviewed_after(same_entry, head, request.updated)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("could not compare ledger review time with comment timestamp") from exc
    if answered:
        return "answered", head
    try:
        queued = store.enqueue_retry(
            repo, request.number, head, "github:" + request.login,
            retry_of=str(latest["id"]) if latest else None,
            options={
                RETRY_KIND_OPTION: RETRY_KIND_REREVIEW,
                RETRY_SCOPE_OPTION: RETRY_SCOPE_ALL,
                RETRY_SEEN_AT_OPTION: request.updated,
                RETRY_SOURCE_KEY_OPTION: source,
            },
        )
    except PrBusyError:
        return "busy", head
    except AuthorReviewLimitError:
        return "limit", head
    return "queued", queued["head_sha"]
