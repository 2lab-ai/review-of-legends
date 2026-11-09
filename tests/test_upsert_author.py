"""``upsert_issue_comment`` must only PATCH bot-authored marker comments.

If another PR participant happens to include the marker text in their comment,
the daemon must not rewrite their comment — it must instead append a new bot
comment.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service.service import GitHubClient


class UpsertAuthorGuardTest(unittest.TestCase):
    def _make_client(self) -> GitHubClient:
        client = GitHubClient.__new__(GitHubClient)
        client.repo = "owner/repo"
        client.gh_bin = "gh"
        client.api_version = "2022-11-28"
        client.token = "ghs_test"
        client._bot_login = "bot-login"  # injected to avoid network lookup
        return client

    def test_updates_existing_bot_comment(self) -> None:
        client = self._make_client()
        marker = "<!-- review-of-legends-executive-summary -->"
        existing = [
            {
                "id": 11,
                "body": f"{marker}\nold",
                "user": {"login": "bot-login"},
            }
        ]
        with mock.patch.object(
            client, "list_issue_comments", return_value=existing
        ), mock.patch.object(
            client, "update_issue_comment", return_value={"id": 11}
        ) as update_mock, mock.patch.object(
            client, "create_issue_comment", return_value={"id": 22}
        ) as create_mock:
            client.upsert_issue_comment(1, marker, "new body")
        update_mock.assert_called_once_with(11, "new body")
        create_mock.assert_not_called()

    def test_does_not_patch_non_bot_marker_comment(self) -> None:
        client = self._make_client()
        marker = "<!-- review-of-legends-executive-summary -->"
        existing = [
            {
                "id": 11,
                "body": f"{marker}\nspoof",
                "user": {"login": "attacker"},
            }
        ]
        with mock.patch.object(
            client, "list_issue_comments", return_value=existing
        ), mock.patch.object(
            client, "update_issue_comment"
        ) as update_mock, mock.patch.object(
            client, "create_issue_comment", return_value={"id": 22}
        ) as create_mock:
            client.upsert_issue_comment(1, marker, "new body")
        update_mock.assert_not_called()
        create_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
