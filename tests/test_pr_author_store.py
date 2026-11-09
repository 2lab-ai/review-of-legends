"""PR authors' settings and feedback in the dashboard store (pr_authors, feedback).

The daemon writes them from ``@review-of-legends`` comments and the welcome
comment; the web tier only reads them. A database created before these tables
gains them on open, the feedback table keeps its newest MAX_FEEDBACK_ROWS rows,
and the event stream names logins and ids only, never a language or the text.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import dashboard_store as ds
from code_review_service.dashboard_store import DashboardStore


class PrAuthorStoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "dashboard" / "dashboard.sqlite3"
        self.store = DashboardStore(self.db_path)


class MigrationTest(PrAuthorStoreTestCase):
    def test_a_database_from_before_the_tables_gains_them_on_open(self) -> None:
        path = Path(self._tmp.name) / "old.sqlite3"
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE retry_requests (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
              repo TEXT NOT NULL, pr INTEGER NOT NULL, head_sha TEXT NOT NULL, requested_by TEXT NOT NULL,
              retry_of TEXT, status TEXT NOT NULL, run_id TEXT, error TEXT, options TEXT, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL);
            CREATE TABLE runs (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, repo TEXT NOT NULL,
              pr INTEGER NOT NULL, head_sha TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
              trigger_source TEXT NOT NULL DEFAULT 'poll', retry_of TEXT, request_id TEXT, run_dir TEXT, llm TEXT,
              status TEXT NOT NULL, stage TEXT, agents TEXT, review_url TEXT, error TEXT, successful_agents INTEGER,
              total_agents INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, run_id TEXT,
              payload TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL,
              updated_by TEXT NOT NULL);
            INSERT INTO settings VALUES ('model_policy', '{"model": "grok-4.6"}', 't', 'z');
            """
        )
        conn.commit()
        conn.close()
        store = DashboardStore(path)
        conn = sqlite3.connect(str(path))
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        author_columns = [row[1] for row in conn.execute("PRAGMA table_info(pr_authors)")]
        feedback_columns = [row[1] for row in conn.execute("PRAGMA table_info(feedback)")]
        conn.close()
        self.assertTrue({"pr_authors", "feedback"} <= tables)
        self.assertEqual(author_columns, ["login", "display_login", "lang", "lang_set_at", "lang_comment_url",
                                          "lang_source_at", "lang_source_key", "welcomed_at", "welcomed_pr", "updated_at"])
        self.assertEqual(feedback_columns, ["id", "repo", "pr", "login", "body", "comment_url", "created_at",
                                            "source_key"])
        self.assertEqual(store.get_setting("model_policy")["value"], {"model": "grok-4.6"}, "existing rows are kept")
        store.set_author_language("Alice", "ko")
        self.assertEqual(DashboardStore(path).get_pr_author("alice")["lang"], "ko", "opening it again changes nothing")


class AuthorTest(PrAuthorStoreTestCase):
    def test_logins_are_keyed_lowercase_and_shown_as_typed(self) -> None:
        self.assertIsNone(self.store.get_pr_author("Alice"))
        row = self.store.set_author_language("Alice", "ko", "https://github.com/o/r/pull/1#issuecomment-1")
        self.assertEqual((row["login"], row["display_login"], row["lang"]), ("alice", "Alice", "ko"))
        self.assertEqual(row["lang_comment_url"], "https://github.com/o/r/pull/1#issuecomment-1")
        self.assertTrue(row["lang_set_at"])
        self.assertIsNone(row["welcomed_at"])
        self.assertEqual(self.store.get_pr_author("ALICE")["lang"], "ko")

    def test_clearing_the_language_keeps_the_welcome(self) -> None:
        self.store.mark_author_welcomed("alice", "o/r#3")
        self.store.set_author_language("Alice", "ja")
        row = self.store.set_author_language("Alice", None)
        self.assertIsNone(row["lang"])
        self.assertEqual((row["welcomed_pr"], bool(row["welcomed_at"])), ("o/r#3", True))

    def test_an_older_command_never_overwrites_a_newer_setting(self) -> None:
        # source_at is when the command was written: the newest one stands,
        # whatever order the commands are recorded in. None writes regardless.
        newer = self.store.set_author_language("Alice", "en", "u2", source_at="2026-10-15T01:00:05Z")
        self.assertEqual((newer["lang"], newer["lang_source_at"]), ("en", "2026-10-15T01:00:05Z"))
        before = len(self.store.events_after(0))
        older = self.store.set_author_language("Alice", "ko", "u1", source_at="2026-10-15T01:00:00Z")
        self.assertEqual((older["lang"], older["lang_comment_url"]), ("en", "u2"), "the row that stands")
        cleared = self.store.set_author_language("Alice", None, "u0", source_at="2026-10-15T00:59:00Z")
        self.assertEqual(cleared["lang"], "en", "an older clear changes nothing either")
        self.assertEqual(len(self.store.events_after(0)), before, "and emits nothing")
        same = self.store.set_author_language("Alice", "ja", "u3", source_at="2026-10-15T01:00:05Z")
        self.assertEqual(same["lang"], "ja", "the same second: the later recorded wins")
        self.assertEqual(self.store.set_author_language("Alice", "ko")["lang"], "ko", "None: unconditional")

    def test_bad_input_is_refused(self) -> None:
        for login in ("", "dependabot[bot]", "-x", "a b", None, "x" * 101):
            with self.subTest(login=login), self.assertRaises(ValueError):
                self.store.set_author_language(login, "ko")  # type: ignore[arg-type]
        for lang in ("KO", "korean", "ko ko", "", 7):
            with self.subTest(lang=lang), self.assertRaises(ValueError):
                self.store.set_author_language("alice", lang)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.store.mark_author_welcomed("alice", "")
        self.assertEqual(self.store.events_after(0), [])

    def test_the_list_holds_authors_with_a_language_or_a_welcome_newest_first(self) -> None:
        self.store.set_author_language("Alice", "ko")
        self.store.mark_author_welcomed("Bob", "o/r#1")
        self.store.set_author_language("Carol", "en")
        self.store.set_author_language("Carol", None)  # nothing left: not listed
        logins = [row["login"] for row in self.store.list_pr_authors()]
        self.assertEqual(sorted(logins), ["alice", "bob"])
        self.assertEqual(len(self.store.list_pr_authors(limit=1)), 1)
        self.assertEqual(len(self.store.list_pr_authors(limit=10, offset=1)), 1)
        self.assertEqual(len(self.store.list_pr_authors(limit=10_000)), 2, "the limit is clamped, not refused")


class FeedbackTest(PrAuthorStoreTestCase):
    def test_feedback_is_bounded_and_listed_newest_first(self) -> None:
        first = self.store.add_feedback("o/r", 3, "Alice", "  too noisy  ", "https://github.com/o/r/pull/3#issuecomment-9")
        self.assertEqual((first["repo"], first["pr"], first["login"], first["body"]), ("o/r", 3, "Alice", "too noisy"))
        long = self.store.add_feedback("o/r", 4, "Bob", "x" * 9000)
        self.assertEqual(len(long["body"]), ds.MAX_FEEDBACK_CHARS)
        self.assertTrue(long["body"].endswith(ds.TRUNCATION_MARKER))
        self.assertEqual([row["id"] for row in self.store.list_feedback()], [long["id"], first["id"]])
        self.assertEqual([row["id"] for row in self.store.list_feedback(limit=1, before_id=long["id"])], [first["id"]])
        with self.assertRaises(ValueError):
            self.store.add_feedback("o/r", 0, "Alice", "x")

    def test_feedback_pages_by_row_id(self) -> None:
        ids = [self.store.add_feedback("o/r", 1, "Alice", "note %d" % index)["id"] for index in range(7)]
        listed = lambda **keys: [row["id"] for row in self.store.list_feedback(**keys)]  # noqa: E731
        self.assertEqual(listed(limit=3), ids[:-4:-1], "no key: the newest, newest first")
        self.assertEqual(listed(limit=3, before_id=ids[4]), [ids[3], ids[2], ids[1]], "below the key, newest first")
        self.assertEqual(listed(limit=3, after_id=ids[1]), [ids[2], ids[3], ids[4]], "right above the key, oldest first")
        self.assertEqual(listed(limit=10, before_id=ids[0]), [])
        self.assertEqual(listed(limit=10, after_id=ids[-1]), [])
        # A key names the same place however many rows arrive on top: no shift, no repeat.
        page = listed(limit=2, before_id=ids[5])
        self.store.add_feedback("o/r", 1, "Alice", "arrived while paging")
        self.assertEqual(listed(limit=2, before_id=page[-1]), [ids[2], ids[1]])
        # A key past every id stored is the top, not an error (SQLite holds 64-bit ids).
        self.assertEqual(len(listed(limit=200, before_id=2**80)), 8)
        self.assertEqual(listed(after_id=2**80), [])
        self.assertEqual(len(listed(limit=10_000)), 8, "the limit is clamped, not refused")
        for keys in ({"before_id": 0}, {"after_id": -1}, {"before_id": "3"}, {"after_id": True}, {"before_id": 1.5},
                     {"before_id": 5, "after_id": 2}):
            with self.subTest(keys=keys):
                with self.assertRaises(ValueError):
                    self.store.list_feedback(**keys)

    def test_one_comment_is_one_row_however_often_it_is_replayed(self) -> None:
        # The daemon records a command before it saves its cursor: a restart in
        # between replays the comment, and its source key keeps it one row.
        first = self.store.add_feedback("o/r", 3, "Alice", "too noisy", "https://github.com/o/r/pull/3#issuecomment-9",
                                        source_key="o/r#issue#9")
        again = self.store.add_feedback("o/r", 3, "Alice", "too noisy", "https://github.com/o/r/pull/3#issuecomment-9",
                                        source_key="o/r#issue#9")
        self.assertEqual(again, first)
        other_kind = self.store.add_feedback("o/r", 3, "Alice", "too noisy", source_key="o/r#review#9")
        self.assertNotEqual(other_kind["id"], first["id"], "a review comment with the same id is another comment")
        self.assertEqual(len(self.store.list_feedback()), 2)
        self.assertEqual([event["kind"] for event in self.store.events_after(0)], ["feedback_added", "feedback_added"],
                         "a replay announces nothing")
        # Without a key (a caller that has none) every call is a row, as before.
        self.store.add_feedback("o/r", 3, "Alice", "x")
        self.store.add_feedback("o/r", 3, "Alice", "x")
        self.assertEqual(len(self.store.list_feedback()), 4)

    def test_only_the_newest_rows_are_kept(self) -> None:
        with mock.patch.object(ds, "MAX_FEEDBACK_ROWS", 5):
            ids = [self.store.add_feedback("o/r", 1, "Alice", "note %d" % index)["id"] for index in range(7)]
        kept = [row["id"] for row in self.store.list_feedback(limit=200)]
        self.assertEqual(kept, list(reversed(ids[2:])))
        self.assertEqual(ds.MAX_FEEDBACK_ROWS, 2000)

    def test_the_real_cap(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        conn.executemany(
            "INSERT INTO feedback (repo, pr, login, body, comment_url, created_at) VALUES ('o/r', 1, 'a', ?, '', 't')",
            [("old %d" % index,) for index in range(ds.MAX_FEEDBACK_ROWS)],
        )
        conn.commit()
        conn.close()
        newest = self.store.add_feedback("o/r", 2, "Alice", "newest")
        conn = sqlite3.connect(str(self.db_path))
        count, lowest = conn.execute("SELECT COUNT(*), MIN(id) FROM feedback").fetchone()
        conn.close()
        self.assertEqual(count, ds.MAX_FEEDBACK_ROWS)
        self.assertEqual(lowest, 2, "the oldest row went")
        self.assertEqual(self.store.list_feedback(limit=1)[0]["id"], newest["id"])


class EventTest(PrAuthorStoreTestCase):
    def test_events_carry_logins_and_ids_never_text_or_language(self) -> None:
        self.store.set_author_language("Alice", "ko", "https://github.com/o/r/pull/1#issuecomment-1")
        self.store.mark_author_welcomed("Alice", "o/r#1")
        row = self.store.add_feedback("o/r", 1, "Alice", "SECRET-FEEDBACK-TEXT")
        events = self.store.events_after(0)
        self.assertEqual([event["kind"] for event in events], ["author_updated", "author_updated", "feedback_added"])
        self.assertEqual([event["payload"] for event in events],
                         [{"login": "alice"}, {"login": "alice"}, {"feedback_id": row["id"], "login": "alice"}])
        dumped = json.dumps(events)
        self.assertNotIn("SECRET-FEEDBACK-TEXT", dumped)
        self.assertNotIn('"ko"', dumped)


if __name__ == "__main__":
    unittest.main()
