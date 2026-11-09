"""``GET /api/authors`` and ``GET /api/feedback``: PR authors' settings and feedback, read-only.

Any signed-in account reads them (the admin and a viewer alike); nobody writes
them here, since authors change their settings by commenting on GitHub. Every
field is a bounded projection: feedback text is redacted like any other string
this tier sends, and only a github.com link survives as a link. Driven through
the real SQLite store with test_dashboard_web's harness.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service.dashboard_store import DashboardStore
from tests.test_dashboard_web import (
    ADMIN_USERINFO,
    VIEWER_USERINFO,
    AppTestCase,
    dashboard_web,
    requires_dashboard_deps,
)


@requires_dashboard_deps
class PrAuthorApiTest(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = DashboardStore(self.service_root / "data" / "dashboard" / "dashboard.sqlite3")

    def signed_in(self, userinfo: dict):
        client = self.make_client()
        self.oauth.token = {"access_token": "at", "userinfo": dict(userinfo)}
        identity = self.login(client)
        return client, identity

    def test_anonymous_requests_are_refused(self) -> None:
        client = self.make_client()
        for path in ("/api/authors", "/api/feedback"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"], "authentication required")

    def test_a_viewer_and_the_admin_read_the_same_projection(self) -> None:
        self.store.set_author_language("Alice", "ko", "https://github.com/owner/repo/pull/1#issuecomment-1")
        self.store.mark_author_welcomed("Bob", "owner/repo#2")
        self.store.add_feedback("owner/repo", 2, "Bob", "the summary is too long", "https://github.com/owner/repo/pull/2#issuecomment-5")
        answers = []
        for userinfo in (VIEWER_USERINFO, ADMIN_USERINFO):
            client, identity = self.signed_in(userinfo)
            authors = client.get("/api/authors")
            feedback = client.get("/api/feedback")
            self.assertEqual((authors.status_code, feedback.status_code), (200, 200))
            self.assertEqual(authors.headers["cache-control"], "no-store")
            answers.append((identity["role"], authors.json(), feedback.json()))
        self.assertEqual([answer[0] for answer in answers], ["viewer", "admin"])
        self.assertEqual(answers[0][1:], answers[1][1:])
        _, authors, feedback = answers[0]
        by_login = {row["login"]: row for row in authors["authors"]}
        self.assertEqual(set(by_login), {"Alice", "Bob"})
        self.assertEqual(set(by_login["Alice"]), set(dashboard_web.AUTHOR_FIELDS))
        self.assertEqual(by_login["Alice"]["lang"], "ko")
        self.assertIsNone(by_login["Alice"]["welcomed_at"])
        self.assertIsNone(by_login["Bob"]["lang"])
        self.assertTrue(by_login["Bob"]["welcomed_at"])
        self.assertIsNone(authors["next_offset"])
        [row] = feedback["feedback"]
        self.assertEqual(set(row), set(dashboard_web.FEEDBACK_FIELDS))
        self.assertEqual(
            {key: row[key] for key in ("login", "repo", "pr", "comment_url", "text")},
            {"login": "Bob", "repo": "owner/repo", "pr": 2,
             "comment_url": "https://github.com/owner/repo/pull/2#issuecomment-5", "text": "the summary is too long"},
        )
        self.assertNotIn("lang_comment_url", json.dumps(authors), "only the contract's fields leave")

    def test_nothing_here_writes(self) -> None:
        client, identity = self.signed_in(ADMIN_USERINFO)
        headers = self.auth_headers(identity)
        for method in ("post", "put", "delete"):
            for path in ("/api/authors", "/api/feedback"):
                with self.subTest(method=method, path=path):
                    response = getattr(client, method)(path, headers=headers)
                    self.assertEqual(response.status_code, 405, response.text)

    def test_feedback_pages_by_row_id_with_has_more_in_the_direction_asked(self) -> None:
        ids = [self.store.add_feedback("owner/repo", 1, "Alice", "note %d" % index)["id"] for index in range(5)]
        client, _ = self.signed_in(VIEWER_USERINFO)

        def page(query: str) -> tuple:
            body = client.get("/api/feedback?" + query).json()
            self.assertEqual(set(body), {"feedback", "has_more"})
            return [row["id"] for row in body["feedback"]], body["has_more"]

        self.assertEqual(page("limit=2"), ([ids[4], ids[3]], True), "the newest page, newest first")
        self.assertEqual(page("limit=2&before_id=%d" % ids[3]), ([ids[2], ids[1]], True))
        self.assertEqual(page("limit=2&before_id=%d" % ids[1]), ([ids[0]], False))
        # Exactly a page left: has_more is about rows past it, not a full page.
        self.assertEqual(page("limit=2&before_id=%d" % ids[2]), ([ids[1], ids[0]], False))
        # after_id: the rows right above the key, oldest first, so a refresh catches up without a gap.
        self.assertEqual(page("limit=2&after_id=%d" % ids[0]), ([ids[1], ids[2]], True))
        self.assertEqual(page("limit=2&after_id=%d" % ids[2]), ([ids[3], ids[4]], False))
        self.assertEqual(page("after_id=%d" % ids[4]), ([], False))
        # A row that arrives between two pages moves no key: the next older page is the same.
        self.store.add_feedback("owner/repo", 1, "Alice", "arrived while paging")
        self.assertEqual(page("limit=2&before_id=%d" % ids[3]), ([ids[2], ids[1]], True))
        # A key past every row is the top; the limit is clamped; offset is no longer read.
        self.assertEqual(len(page("before_id=%d" % 2**80)[0]), 6)
        self.assertEqual(page("after_id=%d" % 2**80), ([], False))
        self.assertEqual(len(page("limit=100000")[0]), 6)
        self.assertEqual(page("limit=2&offset=4")[0], page("limit=2")[0])

    def test_feedback_paging_keys_are_validated(self) -> None:
        self.store.add_feedback("owner/repo", 1, "Alice", "note")
        client, _ = self.signed_in(VIEWER_USERINFO)
        for query in ("limit=-1", "limit=x", "before_id=0", "before_id=-3", "before_id=x", "before_id=1.5",
                      "after_id=0", "after_id=-1", "after_id=abc", "before_id=5&after_id=2"):
            with self.subTest(query=query):
                response = client.get("/api/feedback?" + query)
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(client.get("/api/feedback?before_id=&after_id=").status_code, 200, "empty means absent")

    def test_text_is_redacted_and_bounded_and_only_github_links_survive(self) -> None:
        token = "ghp_" + "A" * 36
        self.store.add_feedback("owner/repo", 1, "Alice", "see /Users/operator/secret.txt and %s %s" % (token, "x" * 5000),
                                "javascript:alert(1)")
        self.store.add_feedback("owner/repo", 1, "Alice", "fine", "https://evil.example/pull/1")
        client, _ = self.signed_in(VIEWER_USERINFO)
        rows = client.get("/api/feedback").json()["feedback"]
        self.assertEqual([row["comment_url"] for row in rows], [None, None])
        text = rows[1]["text"]
        self.assertNotIn("/Users/operator", text)
        self.assertNotIn(token, text)
        self.assertLessEqual(len(text), 4000)

    def test_projections_drop_malformed_values(self) -> None:
        self.assertEqual(
            dashboard_web.project_author({"login": "x", "display_login": "X", "lang": "Korean!", "lang_set_at": None}),
            {"login": "X", "lang": None, "lang_set_at": None, "welcomed_at": None},
        )
        row = dashboard_web.project_feedback({"id": "7", "pr": True, "comment_url": "https://github.com/a b", "body": None})
        self.assertEqual((row["id"], row["pr"], row["comment_url"], row["text"]), (None, None, None, ""))

    def test_a_store_that_is_down_or_too_old_answers_503(self) -> None:
        client = self.make_client(store=None)
        self.oauth.token = {"access_token": "at", "userinfo": dict(VIEWER_USERINFO)}
        self.login(client)
        self.assertEqual(client.get("/api/authors").status_code, 503)
        old = self.make_client(store=object())
        self.login(old)
        self.assertEqual(old.get("/api/feedback").status_code, 503)

    def test_the_event_stream_names_the_login_and_the_id_only(self) -> None:
        self.store.set_author_language("Alice", "ko")
        row = self.store.add_feedback("owner/repo", 1, "Alice", "SECRET TEXT")
        events = [dashboard_web.project_event(event) for event in self.store.events_after(0)]
        self.assertEqual([event["payload"] for event in events],
                         [{"login": "alice"}, {"login": "alice", "feedback_id": row["id"]}])


if __name__ == "__main__":
    unittest.main()
