"""The watcher reads PR comments for ``@review-of-legends`` commands (ReviewScheduler._poll_mentions).

GitHub never notifies the App's bot of a mention, so after each listing the
watcher reads the repository's PR comments updated since its cursor. A quiet
repository must cost the same conditional URL every tick (a 304), the cursor
starts at the first sight of a repository (history is not answered, save a
comment in the margin a cursor without GitHub's clock starts early by), each
comment is answered once, bots and plain issues are ignored, at most
MAX_COMMANDS_PER_TICK commands are answered per tick, a rate-limit refusal ends
the watch, and nothing is read while the dashboard store is down.

A command is recorded in the store, then the cursor saved, then acknowledged:
a cursor that cannot be saved means no acknowledgement and the comment applied
again next tick (the store keeps that to one row), and a crash after the save
loses the acknowledgement rather than doubling it.

The GitHub fake and the scheduler harness are test_scheduler's; the store is
the real SQLite one.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import mentions
from code_review_service import scheduler as sched_mod
from code_review_service import service as svc
from code_review_service.dashboard_store import DashboardStore
from tests.test_scheduler import REPO, SchedulerTestCase, pr

NOW = "2026-10-13T10:10:06Z"  # SchedulerTestCase's clock, OPENED_TS + 15
LATER = "2026-10-13T10:11:00Z"
# The welcome comment's examples, as an author might paste them.
EXAMPLES = "\n".join(
    line for line in mentions.welcome_body("Alice", "en").splitlines() if line.startswith("@review-of-legends")
)


class Crash(BaseException):
    """The daemon dying mid-tick: nothing in the watcher catches it."""


def rate_limited(method: str = "POST") -> svc.ServiceError:
    error = svc.ServiceError(
        'GitHub API %s failed: 403 {"message": "You have exceeded a secondary rate limit."}' % method
    )
    error.status_code = 403  # type: ignore[attr-defined]
    return error


def tied(cursor: Dict[str, Any]) -> List[int]:
    """The ids the cursor read in its own second (at_since holds [id, revision])."""
    return sorted(entry[0] for entry in cursor["at_since"])


def comment(
    comment_id: int,
    body: str,
    login: str = "Alice",
    number: int = 7,
    updated: str = LATER,
    created: str = "",
    kind: str = mentions.ISSUE,
    user_type: str = "User",
) -> Dict[str, Any]:
    item = {
        "id": comment_id,
        "body": body,
        "user": {"login": login, "type": user_type},
        "created_at": created or updated,
        "updated_at": updated,
        "node_id": "C_%d" % comment_id,
    }
    if kind == mentions.REVIEW:
        item["html_url"] = "https://github.com/%s/pull/%d#discussion_r%d" % (REPO, number, comment_id)
        item["pull_request_url"] = "https://api.github.com/repos/%s/pulls/%d" % (REPO, number)
    else:
        item["html_url"] = "https://github.com/%s/pull/%d#issuecomment-%d" % (REPO, number, comment_id)
        item["issue_url"] = "https://api.github.com/repos/%s/issues/%d" % (REPO, number)
    return item


class MentionPollerTest(SchedulerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = DashboardStore(self.tmp / "dashboard" / "dashboard.sqlite3")
        # GitHub's clock agrees with the daemon's unless a test says otherwise.
        self.listing.server_time = self.now

    def scheduler(self, **kwargs: Any):
        kwargs.setdefault("store", self.store)
        scheduler = self.make(**kwargs)
        scheduler.control()  # startup: lock, state, sweep
        self.listing.prs.setdefault(REPO, [])
        return scheduler

    def add(self, *items: Dict[str, Any], kind: str = mentions.ISSUE, repo: str = REPO) -> None:
        self.listing.comments.setdefault((repo, kind), []).extend(items)

    @staticmethod
    def use_watch_clients(scheduler: Any, factory: Any) -> None:
        scheduler._watch_client_factory = factory
        scheduler._watch_clients.clear()

    def reads(self, kind: str = mentions.ISSUE) -> List[str]:
        return [since for repo, read_kind, since in self.listing.comment_reads if read_kind == kind]

    def writes(self, name: str) -> List[tuple]:
        return [write for write in self.listing.comment_writes if write[0] == name]

    def prepare_author_review(self):
        scheduler = self.scheduler()
        scheduler.tick()
        live = pr(7, "a" * 40)
        live["user"] = {"login": "alice", "type": "User"}
        self.listing.live[(REPO, 7)] = live
        self.store.import_run("reviewed-head", REPO, 7, "a" * 40)
        self.store.set_author_language("Alice", "en")
        return scheduler

    def test_author_review_queues_the_live_head_with_comment_timestamp(self) -> None:
        scheduler = self.prepare_author_review()
        self.add(comment(1, "@review-of-legends review"))
        scheduler._poll_mentions(REPO, 10)
        requests = self.store.list_retries()
        self.assertEqual(len(requests), 1)
        queued = requests[0]
        self.assertEqual(queued["head_sha"], "a" * 40)
        self.assertEqual(queued["retry_of"], "reviewed-head")
        self.assertEqual(queued["requested_by"], "github:Alice")
        self.assertEqual(queued["options"]["kind"], "rereview")
        self.assertEqual(queued["options"]["scope"], "all")
        self.assertEqual(queued["options"]["seen_at"], LATER)
        self.assertIn("aaaaaaaaaaaa", self.writes("comment")[0][-1])
        self.assertIn("queued", self.writes("comment")[0][-1])

    def test_author_review_second_comment_on_same_head_is_refused(self) -> None:
        scheduler = self.prepare_author_review()
        self.add(comment(1, "@review-of-legends review"))
        scheduler._poll_mentions(REPO, 10)
        first = self.store.claim_retry()
        if first:
            self.store.finish_retry(first["id"], "succeeded")
        self.add(comment(2, "@review-of-legends review"))
        scheduler._poll_mentions(REPO, 10)
        self.assertIn("push a commit", self.writes("comment")[-1][-1])
        self.assertIn("maintainer", self.writes("comment")[-1][-1])
        self.assertEqual(len(self.store.list_retries()), 1)

    def test_author_review_github_failure_leaves_comment_unread(self) -> None:
        scheduler = self.prepare_author_review()
        self.add(comment(1, "@review-of-legends review"))
        for error in (svc.ServiceError("GitHub unavailable"), ValueError("unreadable response")):
            self.listing.get_error = error
            scheduler._poll_mentions(REPO, 10)
            self.assertEqual(self.listing.comment_writes, [])
            self.assertEqual(self.store.list_retries(), [])
            self.assertEqual(self.saved()["mentions"]["issue"]["since"], NOW)
        self.listing.get_error = None
        scheduler._poll_mentions(REPO, 10)
        self.assertEqual(len(self.store.list_retries()), 1)
        self.assertEqual(len(self.writes("react-issue")), 1)

    def test_author_review_read_rate_limit_stops_mention_phase(self) -> None:
        scheduler = self.prepare_author_review()
        self.add(comment(1, "@review-of-legends review"))
        self.listing.get_error = rate_limited("GET")
        answered, refused = scheduler._poll_mentions(REPO, 10)
        self.assertEqual((answered, refused), (0, True))
        self.assertEqual(self.listing.comment_writes, [])
        self.assertEqual(self.saved()["mentions"]["issue"]["since"], NOW)

    def test_author_review_cursor_save_failure_replays_one_queue_row(self) -> None:
        scheduler = self.prepare_author_review()
        self.add(comment(1, "@review-of-legends review"))
        with mock.patch.object(svc, "save_state", side_effect=OSError("disk full")):
            scheduler._poll_mentions(REPO, 10)
        self.assertEqual(len(self.store.list_retries()), 1)
        self.assertEqual(self.listing.comment_writes, [])
        scheduler._poll_mentions(REPO, 10)
        self.assertEqual(len(self.store.list_retries()), 1)
        self.assertIn("queued", self.writes("comment")[0][-1])
        self.assertEqual(len(self.writes("react-issue")), 1)

    def test_author_reviews_consume_the_normal_command_budget(self) -> None:
        scheduler = self.prepare_author_review()
        self.add(*(comment(i, "@review-of-legends review") for i in range(1, 13)))
        scheduler._poll_all_mentions([REPO], sched_mod.COMMAND_POSTS)
        self.assertEqual(len(self.writes("react-issue")), 1)
        scheduler._poll_all_mentions([REPO], 100)
        self.assertEqual(len(self.writes("react-issue")), 11)
        scheduler._poll_all_mentions([REPO], 100)
        self.assertEqual(len(self.writes("react-issue")), 12)
        self.assertEqual(len(self.store.list_retries()), 1)

    def test_the_cursors_start_now_and_history_is_never_answered(self) -> None:
        # Created before the cursor started, edited after: it comes back through ?since=, unanswered.
        self.add(comment(1, "@review-of-legends lang ko", created="2026-10-01T00:00:00Z", updated=LATER))
        scheduler = self.scheduler()
        scheduler.tick()
        self.assertEqual(self.listing.comment_reads, [(REPO, "issue", NOW), (REPO, "review", NOW)])
        cursors = self.saved()["mentions"]
        self.assertEqual(cursors["review"], {"since": NOW, "floor": NOW, "at_since": [], "seen": []})
        self.assertEqual((cursors["issue"]["since"], tied(cursors["issue"]), cursors["issue"]["seen"]),
                         (LATER, [1], []), "read through, never answered")
        self.assertEqual(self.listing.comment_writes, [])
        self.assertIsNone(self.store.get_pr_author("alice"))

    def test_the_cursors_start_on_githubs_clock_not_the_daemons(self) -> None:
        # The daemon's clock runs an hour ahead: a cursor started on it would
        # take the next hour of comments for history and never answer them.
        github_now = self.now - 3600
        self.listing.server_time = github_now
        scheduler = self.scheduler()
        scheduler.tick()
        started = mentions.github_time(github_now)
        self.assertEqual(self.saved()["mentions"]["issue"]["floor"], started)
        self.assertEqual(self.reads(), [started])
        soon = mentions.github_time(github_now + 60)
        self.add(comment(5, "@review-of-legends lang ko", created=soon, updated=soon))
        scheduler.tick()
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "ko")
        self.assertEqual(len(self.writes("react-issue")), 1)

    def test_without_githubs_clock_the_cursors_start_a_margin_early(self) -> None:
        self.listing.server_time = None
        scheduler = self.scheduler()
        scheduler.tick()
        started = mentions.github_time(self.now - sched_mod.CURSOR_START_MARGIN_SECONDS)
        self.assertGreaterEqual(sched_mod.CURSOR_START_MARGIN_SECONDS, 120)
        self.assertEqual(self.saved()["mentions"]["review"],
                         {"since": started, "floor": started, "at_since": [], "seen": []})
        # GitHub's clock runs a minute behind: a comment it stamps before the
        # daemon's "now" is still answered, once (the seen ids dedupe).
        behind = mentions.github_time(self.now - 60)
        self.add(comment(5, "@review-of-legends lang ko", created=behind, updated=behind))
        scheduler.tick()
        scheduler.tick()
        self.assertEqual(len(self.writes("react-issue")), 1)

    def test_the_cursors_start_at_the_pr_listings_time_not_a_later_response(self) -> None:
        # The status POSTs after the listing move the client's clock on; a
        # command written meanwhile is new, not history.
        listed_at = self.now - 30
        self.listing.server_time = listed_at
        scheduler = self.scheduler(state={"repositories": {REPO: {
            "reviewed": {"1": {"head_sha": "1" * 40}}, "baseline_initialized": True}}})
        self.listing.prs[REPO] = [pr(1, "1" * 40)]
        real = self.listing.watch_client

        def later_clock(repo: str, cache: Dict[str, Any]) -> Any:
            client = real(repo, cache)
            post = client.create_commit_status

            def status(*args: Any, **kwargs: Any) -> Any:
                self.listing.server_time = listed_at + 30
                return post(*args, **kwargs)

            client.create_commit_status = status
            return client

        self.use_watch_clients(scheduler, later_clock)
        scheduler.tick()
        self.assertEqual(len(self.listing.statuses), 1)
        self.assertEqual(self.saved()["mentions"]["issue"]["floor"], mentions.github_time(listed_at))

    def test_a_comment_someone_else_edited_is_never_answered(self) -> None:
        # GitHub keeps the comment's author when a collaborator edits it: the
        # edit history (GraphQL) decides, not the timestamps (a submitted
        # review moves its comments' updated_at without any edit).
        scheduler = self.scheduler()
        scheduler.tick()
        self.listing.edits["C_5"] = {"edited": True, "editors": ["Alice", "mallory"], "complete": True}
        self.add(comment(5, "@review-of-legends lang en", login="Alice", created="2026-10-13T10:10:30Z"),
                 comment(6, "@review-of-legends lang ko", login="Bob", created="2026-10-13T10:10:31Z",
                         updated="2026-10-13T10:12:00Z"))
        self.listing.edits["C_6"] = {"edited": True, "editors": ["bob"], "complete": True}  # his own edit
        scheduler.tick()
        self.assertIsNone(self.store.get_pr_author("alice"), "an edit by someone else")
        self.assertEqual(self.store.get_pr_author("bob")["lang"], "ko", "the author's own edit")
        self.assertEqual([write[2] for write in self.writes("react-issue")], [6])
        self.assertTrue(any("passed over: edited by someone else" in line for line in self.logged))
        self.assertEqual(self.saved()["mentions"]["issue"]["seen"], [5, 6], "neither is read as a command again")

    def test_an_edit_history_that_cannot_be_read_holds_the_command_for_the_next_watch(self) -> None:
        scheduler = self.scheduler()
        scheduler.tick()
        self.add(comment(5, "@review-of-legends lang en", updated="2026-10-13T10:11:00Z"),
                 comment(6, "@review-of-legends lang ko", login="Bob", updated="2026-10-13T10:12:00Z"))
        self.listing.edits_error = svc.ServiceError("GitHub API POST https://api.github.com/graphql failed: 502")
        scheduler.tick()
        self.assertEqual(self.listing.comment_writes, [])
        self.assertEqual(self.saved()["mentions"]["issue"]["since"], NOW, "not past the command")
        self.listing.edits_error = None
        scheduler.tick()
        self.assertEqual([write[2] for write in self.writes("react-issue")], [5, 6])

    def test_a_cursor_start_whose_save_failed_keeps_its_time(self) -> None:
        # The first save fails; by the next watch GitHub's clock has moved on.
        # A command written in between is new, not history.
        first = self.now
        self.listing.server_time = first
        scheduler = self.scheduler()
        with mock.patch.object(svc, "save_state", side_effect=OSError("No space left on device")):
            scheduler.tick()
        self.assertNotIn("mentions", self.saved())
        scheduler.state = json.loads(self.state_path.read_text(encoding="utf-8"))  # an idle reload drops memory
        self.listing.server_time = first + 120
        self.add(comment(5, "@review-of-legends lang ko", updated=mentions.github_time(first + 60)))
        scheduler.tick()
        self.assertEqual(self.saved()["mentions"]["issue"]["floor"], mentions.github_time(first))
        scheduler.tick()
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "ko")

    def test_a_status_post_refused_for_the_rate_limit_backs_the_watch_off(self) -> None:
        # The content-creation limit is the token's: the watch slows down, lists
        # nothing more that tick, and answers no command until the cooldown ends.
        other = "owner/other"
        clock = {"now": 1000.0}
        scheduler = self.scheduler(
            state={"repositories": {
                REPO: {"reviewed": {"1": {"head_sha": "1" * 40}}, "baseline_initialized": True},
                other: {"reviewed": {}, "baseline_initialized": True}}},
            repositories=[REPO, other], monotonic=lambda: clock["now"])
        self.listing.prs[other] = []
        scheduler.tick()
        self.listing.prs[REPO] = [pr(1, "1" * 40)]
        self.add(comment(5, "@review-of-legends lang ko"))
        self.listing.listed.clear()
        self.listing.comment_reads.clear()
        self.listing.status_error = rate_limited()
        self.assertEqual(scheduler.tick(), scheduler.slow_interval)
        self.assertEqual(self.listing.listed, [REPO], "nothing more listed that tick")
        self.listing.status_error = None
        clock["now"] += scheduler.slow_interval / 2
        scheduler.tick()
        self.assertEqual(self.listing.comment_reads, [], "still cooling down: no command answered")
        clock["now"] += scheduler.slow_interval
        scheduler.tick()
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "ko")

    def test_a_cursor_start_kept_waiting_by_a_busy_lock_keeps_its_time(self) -> None:
        first = self.now
        self.listing.server_time = first
        scheduler = self.scheduler()
        with mock.patch.object(scheduler, "_lease_locked", return_value=False):
            scheduler.tick()  # a once holds the executor lock
        self.assertNotIn("mentions", self.saved())
        self.listing.server_time = first + 120
        self.add(comment(5, "@review-of-legends lang ko", updated=mentions.github_time(first + 60)))
        scheduler.tick()
        self.assertEqual(self.saved()["mentions"]["issue"]["floor"], mentions.github_time(first))
        scheduler.tick()
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "ko")

    def test_a_comment_that_changed_after_it_was_listed_waits_for_the_next_listing(self) -> None:
        # The edit history is only trusted for the text it describes.
        scheduler = self.scheduler()
        scheduler.tick()
        self.add(comment(5, "@review-of-legends lang ko"))
        self.listing.edits["C_5"] = {"body": "@review-of-legends lang en", "updated_at": "2026-10-13T10:11:30Z"}
        scheduler.tick()
        self.assertEqual(self.listing.comment_writes, [])
        self.assertEqual(self.saved()["mentions"]["issue"]["since"], NOW, "not past it")
        self.assertTrue(any("changed while it was read" in line for line in self.logged))
        del self.listing.edits["C_5"]
        scheduler.tick()
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "ko")

    def test_a_copy_that_never_matches_its_listing_is_passed_over_not_waited_for_forever(self) -> None:
        # A deleted author's comment: REST says "ghost", GraphQL has no author.
        scheduler = self.scheduler()
        scheduler.tick()
        self.add(comment(5, "@review-of-legends lang ko", login="ghost", updated="2026-10-13T10:11:00Z"),
                 comment(6, "@review-of-legends lang en", login="Bob", updated="2026-10-13T10:12:00Z"))
        self.listing.edits["C_5"] = {"author": None}
        for _ in range(sched_mod.MAX_SNAPSHOT_MISMATCHES - 1):
            scheduler.tick()
        self.assertEqual(self.listing.comment_writes, [], "held while it may be an edit in flight")
        scheduler.tick()
        self.assertIsNone(self.store.get_pr_author("ghost"))
        self.assertEqual([write[2] for write in self.writes("react-issue")], [6], "the rest no longer waits")
        self.assertTrue(any("passed over: GitHub's copy differed" in line for line in self.logged))

    def test_an_author_still_editing_is_waited_for_not_passed_over(self) -> None:
        # Each watch lists a new revision (the author edits again) and GitHub's
        # copy is newer still: the count restarts with every listed revision.
        scheduler = self.scheduler()
        scheduler.tick()
        self.add(comment(5, "@review-of-legends lang ko", updated="2026-10-13T10:11:00Z"))
        listed = self.listing.comments[(REPO, "issue")][0]
        for second in range(sched_mod.MAX_SNAPSHOT_MISMATCHES + 1):
            listed["updated_at"] = "2026-10-13T10:11:%02dZ" % second
            listed["body"] = "@review-of-legends lang ko" + " " * second
            self.listing.edits["C_5"] = {"updated_at": "2026-10-13T10:12:00Z", "editors": ["Alice"], "edited": True}
            scheduler.tick()
        self.assertEqual(self.listing.comment_writes, [], "still being edited: waiting")
        del self.listing.edits["C_5"]
        scheduler.tick()
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "ko", "answered once it is stable")

    def test_a_first_listing_fixes_the_cursor_time_even_when_a_rate_limit_skips_the_mentions(self) -> None:
        first = self.now
        self.listing.server_time = first
        scheduler = self.scheduler(state={"repositories": {REPO: {
            "reviewed": {"1": {"head_sha": "1" * 40}}, "baseline_initialized": True}}})
        self.listing.prs[REPO] = [pr(1, "1" * 40)]
        self.listing.status_error = rate_limited()
        scheduler.tick()  # listed, then the status POST is refused: no mention phase
        self.assertNotIn("mentions", self.saved())
        self.listing.status_error = None
        scheduler._mentions_resume_at = 0.0  # the cooldown is over
        self.listing.server_time = first + 300
        self.add(comment(5, "@review-of-legends lang ko", updated=mentions.github_time(first + 60)))
        scheduler.tick()
        self.assertEqual(self.saved()["mentions"]["issue"]["floor"], mentions.github_time(first))
        scheduler.tick()
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "ko")

    def test_a_comment_edited_into_a_command_within_the_second_it_was_read_is_answered(self) -> None:
        # Second-precision times: the edit keeps the updated_at the first read
        # saw. The cursor remembers what it read in that second, not just the id.
        scheduler = self.scheduler()
        scheduler.tick()
        self.add(comment(5, "hello", updated=LATER))
        scheduler.tick()
        first = self.listing.comments[(REPO, "issue")][0]
        first["body"] = "@review-of-legends lang ko"  # same second, same updated_at
        self.listing.edits["C_5"] = {"edited": True, "editors": ["Alice"]}
        scheduler.tick()
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "ko")
        scheduler.tick()
        self.assertEqual(len(self.writes("react-issue")), 1, "answered once")

    def test_repositories_take_turns_by_who_went_first_not_by_watch(self) -> None:
        # A watch that skips the mention phase is no turn, and a repository
        # whose listing fails every other watch still gets its turns.
        scheduler, other = self._two_repositories()
        order: List[str] = []
        with mock.patch.object(scheduler, "_poll_mentions", side_effect=lambda repo, budget: (order.append(repo), (0, False))[1]):
            phases = [[REPO], [REPO, other], [REPO], [REPO, other], [REPO], [REPO, other]]
            firsts = []
            for listed in phases:
                scheduler._ticks += 2
                before = len(order)
                scheduler._poll_all_mentions(listed, 20)
                firsts.append(order[before])
        self.assertEqual([first for first, listed in zip(firsts, phases) if len(listed) == 2], [other, other, other],
                         "the other repository goes first whenever it is listed after the first one went first")

    def test_a_failed_first_cursor_save_is_retried_while_the_repository_is_quiet(self) -> None:
        # The unsaved cursors are not left in memory: the next watch starts them
        # again (same time) and saves, so a restart cannot start them later.
        first = self.now
        self.listing.server_time = first
        scheduler = self.scheduler()
        with mock.patch.object(svc, "save_state", side_effect=OSError("No space left on device")):
            scheduler.tick()
        self.assertNotIn("mentions", svc.repo_state(scheduler.state, REPO), "nothing unsaved kept")
        self.listing.server_time = first + 120
        scheduler.tick()
        self.assertEqual(self.saved()["mentions"]["issue"]["floor"], mentions.github_time(first))

    def test_a_rate_limit_on_the_bots_own_login_ends_the_mention_work(self) -> None:
        # The same token answers the reaction and the reply: nothing is applied
        # and saved only to have its acknowledgement refused.
        scheduler = self.scheduler()
        scheduler.tick()
        self.add(comment(5, "@review-of-legends lang ko"))
        real = self.listing.watch_client

        def refusing(repo: str, cache: Dict[str, Any]) -> Any:
            client = real(repo, cache)

            def bot_login() -> str:
                raise rate_limited("GET")

            client.bot_login = bot_login
            return client

        self.use_watch_clients(scheduler, refusing)
        self.assertEqual(scheduler.tick(), scheduler.slow_interval)
        self.assertIsNone(self.store.get_pr_author("alice"))
        self.assertEqual(self.saved()["mentions"]["issue"]["since"], NOW, "the command waits")
        self.use_watch_clients(scheduler, real)
        scheduler.tick()
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "ko")

    def test_a_quiet_repository_asks_the_same_url_and_takes_no_lock(self) -> None:
        scheduler = self.scheduler()
        scheduler.tick()
        before = len(self.lock.events)
        scheduler.tick()
        scheduler.tick()
        self.assertEqual(self.reads(), [NOW, NOW, NOW])
        self.assertEqual(self.reads(mentions.REVIEW), [NOW, NOW, NOW])
        self.assertEqual(self.lock.events[before:], [], "a quiet watch takes no lock")

    def test_a_command_is_answered_once_and_the_cursor_moves_to_it(self) -> None:
        scheduler = self.scheduler()
        scheduler.tick()
        self.add(comment(5, "@review-of-legends lang ko"))
        scheduler.tick()
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "ko")
        self.assertEqual(self.writes("react-issue"), [("react-issue", REPO, 5, "+1")])
        [reply] = self.writes("comment")
        self.assertEqual(reply[:3], ("comment", REPO, 7))
        self.assertIn("@Alice", reply[3])
        self.assertEqual(self.saved()["mentions"]["issue"]["since"], LATER)
        # ?since= is inclusive, so the answered comment comes back: never answered twice.
        scheduler.tick()
        self.assertEqual(self.reads()[-1], LATER)
        # An edit brings it back with a later updated_at: still never answered again.
        self.listing.comments[(REPO, "issue")][0]["updated_at"] = "2026-10-13T10:20:00Z"
        self.listing.comments[(REPO, "issue")][0]["body"] = "@review-of-legends lang en"
        scheduler.tick()
        self.assertEqual(len(self.listing.comment_writes), 2)
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "ko")
        self.assertEqual(self.logged.count("owner/repo pr #7: @review-of-legends lang from Alice"), 1)

    def test_review_comments_get_a_reaction_and_a_thread_reply(self) -> None:
        scheduler = self.scheduler()
        scheduler.tick()
        self.add(comment(8, "@review-of-legends this finding is noise", number=3, kind=mentions.REVIEW), kind=mentions.REVIEW)
        scheduler.tick()
        self.assertEqual(self.writes("react-review"), [("react-review", REPO, 8, "+1")])
        [reply] = self.writes("reply")
        self.assertEqual(reply[:4], ("reply", REPO, 3, 8))
        [feedback] = self.store.list_feedback()
        self.assertEqual((feedback["pr"], feedback["body"]), (3, "this finding is noise"))

    def test_a_reply_in_a_thread_goes_under_its_first_comment(self) -> None:
        # GitHub takes no reply to a reply: the bot answers under the thread's root.
        scheduler = self.scheduler()
        scheduler.tick()
        answer = comment(9, "@review-of-legends lang ko", number=3, kind=mentions.REVIEW)
        answer["in_reply_to_id"] = 8
        self.add(answer, kind=mentions.REVIEW)
        scheduler.tick()
        self.assertEqual(self.writes("react-review"), [("react-review", REPO, 9, "+1")], "the reaction is on the comment")
        [reply] = self.writes("reply")
        self.assertEqual(reply[:4], ("reply", REPO, 3, 8))

    def test_a_command_inside_code_is_never_answered(self) -> None:
        scheduler = self.scheduler()
        scheduler.tick()
        bodies = [
            "Like this:\n`\n@review-of-legends lang ko\n`\nright?",
            "Type `` @review-of-legends lang ko `` to switch.",
            "The examples: `" + EXAMPLES + "`",
            "````\n```\n@review-of-legends lang ko\n```\n````",
            "~~~\n@review-of-legends lang ko\n~~~",
        ]
        self.add(*[comment(10 + index, body) for index, body in enumerate(bodies)])
        scheduler.tick()
        self.assertEqual(self.listing.comment_writes, [], "no reaction and no reply")
        self.assertEqual((self.store.list_pr_authors(), self.store.list_feedback()), ([], []), "nothing recorded")
        cursor = self.saved()["mentions"]["issue"]
        self.assertEqual((cursor["since"], tied(cursor)), (LATER, [10, 11, 12, 13, 14]),
                         "read through: not commands")
        # A command is a comment of its own, starting with the mention.
        self.add(comment(20, "`\n@review-of-legends lang ko\n`\n@review-of-legends lang en",
                         updated="2026-10-13T10:12:00Z"),
                 comment(21, "@review-of-legends lang en", updated="2026-10-13T10:12:01Z"))
        scheduler.tick()
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "en")
        self.assertEqual(self.writes("react-issue"), [("react-issue", REPO, 21, "+1")])

    def test_the_bot_bot_accounts_and_plain_issues_are_skipped(self) -> None:
        scheduler = self.scheduler()
        scheduler.tick()
        plain = comment(4, "@review-of-legends lang ko")
        plain["html_url"] = "https://github.com/%s/issues/7#issuecomment-4" % REPO
        self.add(
            comment(1, "@review-of-legends lang ko", login="review-of-legends[bot]", user_type="Bot"),
            comment(2, "@review-of-legends lang ko", login="dependabot[bot]", user_type="Bot"),
            comment(3, "@review-of-legends lang ko", login="ci-helper", user_type="Bot"),
            plain,
        )
        scheduler.tick()
        self.assertEqual(self.listing.comment_writes, [])
        self.assertEqual(self.store.list_pr_authors(), [])
        cursor = self.saved()["mentions"]["issue"]
        self.assertEqual((cursor["since"], tied(cursor)), (LATER, [1, 2, 3, 4]))
        scheduler.tick()
        before = len(self.lock.events)
        scheduler.tick()
        self.assertEqual(self.lock.events[before:], [], "read once: the next watches are quiet")

    def test_at_most_ten_commands_a_tick_and_the_rest_wait_their_turn(self) -> None:
        scheduler = self.scheduler()
        scheduler.tick()
        times = ["2026-10-13T10:11:%02dZ" % second for second in range(11)]
        self.add(*[comment(100 + index, "@review-of-legends feedback %d" % index, updated=at) for index, at in enumerate(times)])
        scheduler.tick()
        self.assertEqual(len(self.writes("react-issue")), mentions.MAX_COMMANDS_PER_TICK)
        self.assertEqual(len(self.writes("comment")), mentions.MAX_COMMANDS_PER_TICK)
        self.assertEqual(self.saved()["mentions"]["issue"]["since"], times[9], "not past the one left waiting")
        scheduler.tick()
        self.assertEqual(self.reads()[-1], times[9])
        self.assertEqual([write[2] for write in self.writes("react-issue")], list(range(100, 111)))
        self.assertEqual(len(self.store.list_feedback()), 11)

    def test_the_cap_spans_repositories(self) -> None:
        other = "owner/other"
        state = {"repositories": {
            REPO: {"reviewed": {}, "baseline_initialized": True},
            other: {"reviewed": {}, "baseline_initialized": True},
        }}
        scheduler = self.scheduler(state=state, repositories=[REPO, other])
        self.listing.prs[other] = []
        scheduler.tick()
        self.add(*[comment(index, "@review-of-legends hi %d" % index) for index in range(1, 7)])
        self.add(*[comment(index, "@review-of-legends hi %d" % index) for index in range(11, 17)], repo=other)
        scheduler.tick()
        self.assertEqual(len(self.writes("react-issue")), 10)
        scheduler.tick()
        self.assertEqual(len(self.writes("react-issue")), 12)

    def test_a_rate_limit_refusal_ends_the_watch_and_slows_it(self) -> None:
        other = "owner/other"
        state = {"repositories": {
            REPO: {"reviewed": {}, "baseline_initialized": True},
            other: {"reviewed": {}, "baseline_initialized": True},
        }}
        scheduler = self.scheduler(state=state, repositories=[REPO, other])
        self.listing.prs[other] = []
        scheduler.tick()
        scheduler._mention_turns = [REPO, other]  # this repository's turn to go first
        self.listing.comment_reads.clear()
        self.listing.listed.clear()
        error = svc.ServiceError("GitHub API GET failed: 403 secondary rate limit")
        error.status_code = 403  # type: ignore[attr-defined]
        self.listing.comment_error = error
        self.assertEqual(scheduler.tick(), scheduler.slow_interval)
        self.assertEqual(self.listing.comment_reads, [(REPO, "issue", NOW)], "nothing more read this tick")
        self.assertEqual(self.listing.listed, [REPO, other], "the PR listings came first")
        self.assertEqual(sum("reading issue comments" in line for line in self.logged), 1)
        # Recovered: back to every watch interval, and said once.
        self.listing.comment_error = None
        self.listing.remaining = 4000
        self.assertEqual(scheduler.tick(), scheduler.watch_interval)
        self.assertIn("owner/repo: reading issue comments works again", self.logged)

    def test_another_listing_failure_skips_that_listing_only(self) -> None:
        scheduler = self.scheduler()
        scheduler.tick()
        error = svc.ServiceError("GitHub API GET failed: 404")
        error.status_code = 404  # type: ignore[attr-defined]
        self.listing.comment_error = error
        self.assertEqual(scheduler.tick(), scheduler.watch_interval)
        self.assertEqual(self.reads(), [NOW, NOW])
        self.assertEqual(self.reads(mentions.REVIEW), [NOW, NOW], "the other listing is still read")

    def test_nothing_is_read_while_the_store_is_down(self) -> None:
        self.add(comment(1, "@review-of-legends lang ko"))
        scheduler = self.scheduler(store=None)
        scheduler.tick()
        scheduler.tick()
        self.assertEqual(self.listing.comment_reads, [])
        self.assertNotIn("mentions", self.saved(), "the cursors stay put")
        self.assertEqual(self.listing.comment_writes, [])

    def test_a_store_write_that_fails_is_tried_again_next_tick_with_nothing_posted(self) -> None:
        scheduler = self.scheduler()
        scheduler.tick()
        self.add(comment(1, "@review-of-legends lang ko", updated="2026-10-13T10:11:00Z"),
                 comment(2, "@review-of-legends lang en", login="Bob", updated="2026-10-13T10:12:00Z"))
        real = self.store.set_author_language
        calls: List[str] = []

        def broken(login: str, lang: Any, comment_url: str = "", **kwargs: Any) -> Any:
            calls.append(login)
            raise RuntimeError("database is locked")

        self.store.set_author_language = broken  # type: ignore[assignment]
        scheduler.tick()
        self.assertEqual(calls, ["Alice"], "nothing after the failed one is answered first")
        self.assertEqual(self.listing.comment_writes, [])
        self.assertEqual(self.saved()["mentions"]["issue"]["since"], NOW)
        self.store.set_author_language = real  # type: ignore[assignment]
        scheduler.tick()
        self.assertEqual((self.store.get_pr_author("alice")["lang"], self.store.get_pr_author("bob")["lang"]), ("ko", "en"))
        self.assertEqual([write[2] for write in self.writes("react-issue")], [1, 2])

    def test_a_cursor_that_cannot_be_saved_sends_no_acknowledgement_and_is_answered_next_tick(self) -> None:
        scheduler = self.scheduler()
        scheduler.tick()
        self.add(comment(5, "@review-of-legends the summary is too long", updated="2026-10-13T10:11:00Z"),
                 comment(6, "@review-of-legends lang en", login="Bob", updated="2026-10-13T10:12:00Z"))
        with mock.patch.object(svc, "save_state", side_effect=OSError("No space left on device")):
            scheduler.tick()
        self.assertEqual(self.listing.comment_writes, [], "no reaction and no reply for an unsaved cursor")
        self.assertEqual(len(self.store.list_feedback()), 1, "recorded in the store")
        self.assertIsNone(self.store.get_pr_author("bob"), "nothing after it is answered first")
        self.assertEqual(self.saved()["mentions"]["issue"]["since"], NOW, "unhandled on disk")
        self.assertTrue(any("saving the comment cursor failed" in line for line in self.logged))
        # The disk is back: applied again (the same row) and acknowledged once.
        scheduler.tick()
        self.assertEqual([(write[0], write[2]) for write in self.listing.comment_writes],
                         [("react-issue", 5), ("comment", 7), ("react-issue", 6), ("comment", 7)])
        self.assertEqual(len(self.store.list_feedback()), 1, "the replay is the same feedback row")
        self.assertEqual(self.store.get_pr_author("bob")["lang"], "en")
        cursor = self.saved()["mentions"]["issue"]
        self.assertEqual((cursor["since"], tied(cursor), cursor["seen"]), ("2026-10-13T10:12:00Z", [6], [5, 6]))
        scheduler.tick()
        self.assertEqual(len(self.listing.comment_writes), 4, "acknowledged exactly once")

    def test_a_restart_between_the_store_and_the_cursor_save_records_one_row(self) -> None:
        scheduler = self.scheduler()
        scheduler.tick()
        self.add(comment(5, "@review-of-legends the summary is too long"))
        with mock.patch.object(svc, "save_state", side_effect=Crash()), self.assertRaises(Crash):
            scheduler.tick()
        self.assertEqual(len(self.store.list_feedback()), 1)
        self.assertEqual(self.listing.comment_writes, [])
        restarted = self.make(store=self.store, state=json.loads(self.state_path.read_text(encoding="utf-8")))
        restarted.control()
        restarted.tick()
        self.assertEqual(len(self.store.list_feedback()), 1, "the replayed comment is the same row")
        self.assertEqual([write[0] for write in self.listing.comment_writes], ["react-issue", "comment"])

    def test_a_crash_after_the_save_loses_the_acknowledgement_and_never_doubles_it(self) -> None:
        # At most once: the cursor is saved before the reaction and the reply,
        # so a daemon that dies in between never sends them, now or later.
        scheduler = self.scheduler()
        scheduler.tick()
        self.add(comment(5, "@review-of-legends lang ko"))
        with mock.patch.object(mentions, "acknowledge", side_effect=Crash()), self.assertRaises(Crash):
            scheduler.tick()
        self.assertEqual(tied(self.saved()["mentions"]["issue"]), [5], "saved before the acknowledgement")
        restarted = self.make(store=self.store, state=json.loads(self.state_path.read_text(encoding="utf-8")))
        restarted.control()
        restarted.tick()
        self.assertEqual(self.listing.comment_writes, [])
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "ko")

    def _two_repositories_with_commands(self):
        other = "owner/other"
        state = {"repositories": {
            REPO: {"reviewed": {}, "baseline_initialized": True},
            other: {"reviewed": {}, "baseline_initialized": True},
        }}
        scheduler = self.scheduler(state=state, repositories=[REPO, other])
        self.listing.prs[other] = []
        scheduler.tick()
        self.add(comment(5, "@review-of-legends lang ko", updated="2026-10-13T10:11:00Z"),
                 comment(6, "@review-of-legends lang en", login="Bob", updated="2026-10-13T10:12:00Z"))
        self.add(comment(7, "@review-of-legends hello"), repo=other)
        scheduler._mention_turns = [REPO, other]  # this repository's turn to go first
        self.listing.listed.clear()
        self.listing.comment_reads.clear()
        return scheduler

    def _assert_the_refused_watch_stopped(self, scheduler) -> None:
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "ko", "applied and saved before the refusal")
        self.assertEqual(tied(self.saved()["mentions"]["issue"]), [5])
        self.assertIsNone(self.store.get_pr_author("bob"), "the rest of the repository waits")
        self.assertEqual({read[0] for read in self.listing.comment_reads}, {REPO}, "the next repository waits too")
        self.assertEqual(self.store.list_feedback(), [])
        # Recovered: the rest is answered, and the refused comment is not applied or acknowledged again.
        self.listing.react_error = self.listing.reply_error = None
        self.listing.remaining = 4000
        self.assertEqual(scheduler.tick(), scheduler.watch_interval)
        # The next watch starts with the other repository: the repositories take turns.
        self.assertEqual([write[2] for write in self.writes("react-issue")], [5, 7, 6])
        self.assertEqual(self.store.get_pr_author("bob")["lang"], "en")
        self.assertEqual(len(self.store.list_feedback()), 1)
        self.assertEqual([event["payload"]["login"] for event in self.store.events_after(0)
                          if event["kind"] == "author_updated"], ["alice", "bob"], "alice's command applied once")

    def test_a_reaction_refused_for_a_rate_limit_ends_the_mention_work_and_slows_the_watch(self) -> None:
        scheduler = self._two_repositories_with_commands()
        self.listing.react_error = rate_limited()
        self.assertEqual(scheduler.tick(), scheduler.slow_interval)
        self.assertEqual(self.listing.comment_writes, [("react-issue", REPO, 5, "+1")], "no reply after it")
        self.assertTrue(any("rate limit" in line and "@review-of-legends" in line for line in self.logged))
        self._assert_the_refused_watch_stopped(scheduler)

    def test_a_reply_refused_for_a_rate_limit_ends_the_mention_work_and_slows_the_watch(self) -> None:
        scheduler = self._two_repositories_with_commands()
        self.listing.reply_error = rate_limited()
        self.assertEqual(scheduler.tick(), scheduler.slow_interval)
        self.assertEqual([(write[0], write[2]) for write in self.listing.comment_writes],
                         [("react-issue", 5), ("comment", 7)])
        self._assert_the_refused_watch_stopped(scheduler)

    def test_a_failed_acknowledgement_that_is_not_a_rate_limit_is_logged_and_the_watch_goes_on(self) -> None:
        scheduler = self.scheduler()
        scheduler.tick()
        self.add(comment(5, "@review-of-legends lang ko", updated="2026-10-13T10:11:00Z"),
                 comment(6, "@review-of-legends lang en", login="Bob", updated="2026-10-13T10:12:00Z"))
        error = svc.ServiceError("GitHub API POST failed: 403 Resource not accessible by integration")
        error.status_code = 403  # type: ignore[attr-defined]
        self.listing.react_error = error
        self.assertEqual(scheduler.tick(), scheduler.watch_interval)
        self.assertEqual([(write[0], write[2]) for write in self.listing.comment_writes],
                         [("react-issue", 5), ("comment", 7), ("react-issue", 6), ("comment", 7)])

    def test_a_state_reload_between_ticks_keeps_the_cursor(self) -> None:
        scheduler = self.scheduler()
        scheduler.tick()
        self.add(comment(5, "@review-of-legends lang ko"))
        scheduler.tick()
        # A restart: a new scheduler reads state.json as the first one left it.
        reloaded = self.make(store=self.store, state=json.loads(self.state_path.read_text(encoding="utf-8")))
        reloaded.control()
        reloaded.tick()
        self.assertEqual(len(self.writes("react-issue")), 1, "the saved ledger remembers the comment")
        self.assertEqual(tied(json.loads(self.state_path.read_text(encoding="utf-8"))["repositories"][REPO]["mentions"]["issue"]), [5])

    # ------------------------------------------- progress, order and fairness

    def _two_repositories(self, other_state: Any = None):
        other = "owner/other"
        state = {"repositories": {
            REPO: {"reviewed": {}, "baseline_initialized": True},
            other: other_state or {"reviewed": {}, "baseline_initialized": True},
        }}
        scheduler = self.scheduler(state=state, repositories=[REPO, other])
        self.listing.prs[other] = []
        return scheduler, other

    def test_a_full_page_of_edits_to_comments_read_before_never_blocks_a_newer_command(self) -> None:
        # The bot edits its own progress comments for hours after posting them,
        # so one page (100 rows, oldest update first) can hold nothing but
        # comments read before. The cursor moves through them all the same.
        self.listing.comment_page = 100
        scheduler = self.scheduler()
        scheduler.tick()
        self.add(*[comment(index, "progress %d" % index, login="review-of-legends[bot]", user_type="Bot")
                   for index in range(1, 101)])
        scheduler.tick()
        for index, item in enumerate(self.listing.comments[(REPO, "issue")]):
            item["updated_at"] = "2026-10-13T10:%02d:%02dZ" % (12 + index // 60, index % 60)
        self.add(comment(500, "@review-of-legends lang ko", updated="2026-10-13T10:20:00Z"))
        scheduler.tick()
        scheduler.tick()
        self.assertEqual(self.writes("react-issue"), [("react-issue", REPO, 500, "+1")])
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "ko")
        self.assertEqual(self.saved()["mentions"]["issue"]["since"], "2026-10-13T10:20:00Z")

    def test_a_command_is_never_answered_again_however_many_comments_follow_it(self) -> None:
        # The ids of answered commands are kept for good: a bounded window of
        # every comment read would let an edit replay an old command.
        scheduler = self.scheduler()
        scheduler.tick()
        self.add(comment(5, "@review-of-legends lang ko", updated="2026-10-13T10:11:00Z"))
        scheduler.tick()
        self.add(*[comment(10 + index, "chatter %d" % index, login="review-of-legends[bot]", user_type="Bot",
                           updated="2026-10-13T10:%02d:%02dZ" % (12 + index // 60, index % 60))
                   for index in range(1001)])
        scheduler.tick()
        self.store.set_author_language("Alice", "en")  # her newer command, answered meanwhile
        first = self.listing.comments[(REPO, "issue")][0]
        first["updated_at"] = "2026-10-13T11:30:00Z"  # an edit brings the old command back
        scheduler.tick()
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "en", "the old command is not applied again")
        self.assertEqual(len(self.writes("react-issue")), 1, "nor acknowledged again")

    def test_an_older_language_command_never_overwrites_a_newer_one_from_another_repository(self) -> None:
        # The setting is the author's, across repositories: a repository whose
        # listing was late cannot bring back an older choice.
        scheduler, other = self._two_repositories()
        scheduler.tick()
        self.add(comment(9, "@review-of-legends lang en", updated="2026-10-13T10:11:05Z"))
        self.add(comment(4, "@review-of-legends lang ko", updated="2026-10-13T10:11:00Z"), repo=other)
        real = self.listing.watch_client

        def other_fails(repo: str, cache: Dict[str, Any]) -> Any:
            client = real(repo, cache)
            if repo == other:
                def broken(since: str) -> List[Dict[str, Any]]:
                    raise svc.ServiceError("GitHub API GET failed: 502")
                client.list_repo_issue_comments = broken
            return client

        self.use_watch_clients(scheduler, other_fails)
        scheduler.tick()
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "en")
        self.assertEqual(len(self.writes("comment")), 1, "the other repository's listing failed")
        self.use_watch_clients(scheduler, real)
        scheduler.tick()
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "en", "the older command does not win")
        [_, late] = self.writes("comment")
        self.assertEqual(late[1], other)
        self.assertIn("en", late[3].split("\n")[-1], "the reply names the language that stands")

    def test_language_commands_apply_in_time_order_across_both_kinds_of_comment(self) -> None:
        scheduler = self.scheduler()
        scheduler.tick()
        self.add(comment(8, "@review-of-legends lang ko", number=3, kind=mentions.REVIEW,
                         updated="2026-10-13T10:11:00Z"), kind=mentions.REVIEW)
        self.add(comment(9, "@review-of-legends lang en", updated="2026-10-13T10:11:05Z"))
        scheduler.tick()
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "en", "the newer command wins")
        self.assertEqual([write[0] for write in self.listing.comment_writes],
                         ["react-review", "reply", "react-issue", "comment"], "answered oldest first")
        self.add(comment(10, "@review-of-legends lang ja", updated="2026-10-13T10:12:00Z"))
        self.add(comment(11, "@review-of-legends lang zh", number=3, kind=mentions.REVIEW,
                         updated="2026-10-13T10:12:05Z"), kind=mentions.REVIEW)
        scheduler.tick()
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "zh")

    def test_a_failed_listing_holds_back_the_repositorys_other_listing_too(self) -> None:
        # Answering only the listing that worked could apply a newer command
        # before an older one the failed listing holds.
        scheduler = self.scheduler()
        scheduler.tick()
        self.add(comment(8, "@review-of-legends lang ko", number=3, kind=mentions.REVIEW,
                         updated="2026-10-13T10:11:00Z"), kind=mentions.REVIEW)
        self.add(comment(9, "@review-of-legends lang en", updated="2026-10-13T10:11:05Z"))
        real = self.listing.watch_client

        def review_listing_fails(repo: str, cache: Dict[str, Any]) -> Any:
            client = real(repo, cache)

            def broken(since: str) -> List[Dict[str, Any]]:
                raise svc.ServiceError("GitHub API GET failed: 502")

            client.list_repo_review_comments = broken
            return client

        self.use_watch_clients(scheduler, review_listing_fails)
        scheduler.tick()
        self.assertEqual(self.listing.comment_writes, [], "nothing answered while one listing fails")
        self.use_watch_clients(scheduler, real)
        scheduler.tick()
        self.assertEqual(self.store.get_pr_author("alice")["lang"], "en")

    def test_a_busy_repository_cannot_starve_the_next_one(self) -> None:
        scheduler, other = self._two_repositories()
        scheduler.tick()
        self.add(*[comment(100 + index, "@review-of-legends spam %d" % index, updated="2026-10-13T10:11:%02dZ" % index)
                   for index in range(40)])
        self.add(comment(7, "@review-of-legends lang ko", login="Bob"), repo=other)
        scheduler.tick()
        scheduler.tick()
        self.assertEqual((self.store.get_pr_author("bob") or {}).get("lang"), "ko", "answered within two watches")

    def test_a_new_repositorys_cursors_start_even_when_the_budget_is_spent(self) -> None:
        # The other repository is seen for the first time on a watch whose
        # command budget the first one spends: its cursors start all the same.
        started = {"since": NOW, "floor": NOW, "at_since": [], "seen": []}
        scheduler = self.scheduler(
            state={"repositories": {
                REPO: {"reviewed": {}, "baseline_initialized": True,
                       "mentions": {"issue": dict(started), "review": dict(started)}},
                "owner/other": {"reviewed": {}, "baseline_initialized": True},
            }},
            repositories=["owner/other", REPO],
        )
        scheduler._mention_turns = [REPO, "owner/other"]  # the busy repository goes first
        self.listing.prs["owner/other"] = []
        self.add(*[comment(100 + index, "@review-of-legends spam %d" % index, updated="2026-10-13T10:11:%02dZ" % index)
                   for index in range(mentions.MAX_COMMANDS_PER_TICK)])
        scheduler.tick()
        self.assertEqual(len(self.writes("react-issue")), mentions.MAX_COMMANDS_PER_TICK)
        ledger = json.loads(self.state_path.read_text(encoding="utf-8"))["repositories"]["owner/other"]
        self.assertEqual(sorted(ledger.get("mentions") or {}), ["issue", "review"])

    def test_mention_posts_share_the_watchs_write_budget_with_reviewed_statuses(self) -> None:
        # A reaction and a reply are two content-creating POSTs, like a status:
        # one budget per watch, of which a command always keeps one turn.
        reviewed = {str(number): {"head_sha": "%040d" % number} for number in range(1, 23)}
        scheduler = self.scheduler(state={"repositories": {REPO: {"reviewed": reviewed, "baseline_initialized": True}}})
        scheduler.tick()
        self.listing.prs[REPO] = [pr(number, "%040d" % number) for number in range(1, 23)]
        self.add(*[comment(100 + index, "@review-of-legends hi %d" % index, updated="2026-10-13T10:11:%02dZ" % index)
                   for index in range(5)])
        scheduler.tick()
        self.assertEqual(len(self.listing.statuses),
                         sched_mod.MAX_REVIEWED_STATUS_POSTS_PER_TICK - sched_mod.COMMAND_POSTS,
                         "one command's POSTs are reserved before the statuses")
        self.assertEqual(len(self.writes("react-issue")), 1, "20 POSTs: 18 statuses, one command's reaction and reply")
        scheduler.tick()
        self.assertEqual(len(self.listing.statuses), 22)
        self.assertEqual(len(self.writes("react-issue")), 5)

    def test_a_listing_refused_for_the_rate_limit_reads_no_comments_that_watch(self) -> None:
        scheduler, other = self._two_repositories()
        scheduler.tick()
        self.listing.comment_reads.clear()
        self.add(comment(5, "@review-of-legends lang ko"))
        real = self.listing.watch_client

        def other_refused(repo: str, cache: Dict[str, Any]) -> Any:
            client = real(repo, cache)
            if repo == other:
                def refused() -> List[Dict[str, Any]]:
                    client.rate_limit_remaining = 0
                    raise rate_limited("GET")
                client.list_open_prs = refused
            return client

        self.use_watch_clients(scheduler, other_refused)
        self.assertEqual(scheduler.tick(), scheduler.slow_interval)
        self.assertEqual(self.listing.comment_reads, [], "the token is spent: no comment listing either")
        self.assertIsNone(self.store.get_pr_author("alice"))

if __name__ == "__main__":
    unittest.main()
