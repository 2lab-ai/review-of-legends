"""Posting as a GitHub App: config, JWT, installation-token cache, client, doctor.

With ``github_app`` configured every GitHub API call goes out with an
installation token the daemon mints itself (openssl-signed JWT, stdlib HTTP);
without it the host's ``gh auth token`` path is unchanged.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import service as svc
from tests.shipped_config import SHIPPED_CONFIG_PATH, load_shipped_config

OPENSSL = shutil.which("openssl")
BOT = "review-of-legends[bot]"
JWT_SENTINEL = "JWT-SENTINEL-eyJhbGciOiJSUzI1NiJ9.e30.c2ln"
TOKEN_SENTINEL = "ghs_INSTALLATION_TOKEN_SENTINEL"


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _app(key_path: str = "/nonexistent/app.pem", app_id: int = 11, installation_id: int = 22) -> dict:
    return {
        "app_id": app_id,
        "installation_id": installation_id,
        "private_key_path": key_path,
        "bot_login": BOT,
    }


class _FakeResponse(io.BytesIO):
    """A urlopen response whose body is ``payload`` as JSON."""

    def __init__(self, payload: object, headers: dict | None = None) -> None:
        super().__init__(json.dumps(payload).encode("utf-8"))
        self.headers = headers or {}


class _RawResponse(io.BytesIO):
    """A 200 whose body is the given bytes, or whose read() raises."""

    def __init__(self, raw: bytes = b"", failure: BaseException | None = None) -> None:
        super().__init__(raw)
        self.headers: dict = {}
        self._failure = failure

    def read(self, *args: int) -> bytes:
        if self._failure is not None:
            raise self._failure
        return super().read(*args)


class _Clock:
    """Stands in for the service module's ``time``: ``time()`` returns ``now``."""

    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now

    def __getattr__(self, name: str) -> object:
        return getattr(time, name)


def _http_error(url: str, code: int, payload: object) -> urllib.error.HTTPError:
    body = io.BytesIO(json.dumps(payload).encode("utf-8"))
    return urllib.error.HTTPError(url, code, "Unauthorized", {}, body)


class _TokenCacheIsolation(unittest.TestCase):
    def setUp(self) -> None:
        svc._github_app_tokens.clear()
        self.addCleanup(svc._github_app_tokens.clear)


@unittest.skipUnless(OPENSSL, "openssl is not installed")
class GitHubAppJwtTest(_TokenCacheIsolation):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._tmp.name)
        cls.key = cls.dir / "app.pem"
        cls.pub = cls.dir / "app.pub"
        subprocess.run(
            [OPENSSL, "genrsa", "-out", str(cls.key), "2048"], capture_output=True, check=True, timeout=60
        )
        subprocess.run(
            [OPENSSL, "rsa", "-in", str(cls.key), "-pubout", "-out", str(cls.pub)],
            capture_output=True,
            check=True,
            timeout=60,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_jwt_is_rs256_with_github_claims_and_a_valid_signature(self) -> None:
        now = 1_700_000_000
        jwt = svc.github_app_jwt(4242, str(self.key), now=now)
        parts = jwt.split(".")
        self.assertEqual(len(parts), 3)
        for part in parts:
            self.assertNotIn("=", part)
        self.assertEqual(json.loads(_b64url_decode(parts[0])), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(
            json.loads(_b64url_decode(parts[1])), {"iat": now - 60, "exp": now + 540, "iss": "4242"}
        )
        signature = self.dir / "signature.bin"
        signature.write_bytes(_b64url_decode(parts[2]))
        verify = subprocess.run(
            [OPENSSL, "dgst", "-sha256", "-verify", str(self.pub), "-signature", str(signature)],
            input=f"{parts[0]}.{parts[1]}".encode("ascii"),
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertIn(b"Verified OK", verify.stdout)

    def test_unusable_key_names_the_path_but_not_its_contents(self) -> None:
        bad = self.dir / "bad.pem"
        bad.write_text("-----BEGIN RSA PRIVATE KEY-----\nKEYMATERIALSENTINEL\n-----END RSA PRIVATE KEY-----\n")
        with self.assertRaises(svc.ServiceError) as caught:
            svc.github_app_jwt(1, str(bad))
        message = str(caught.exception)
        self.assertIn(str(bad), message)
        self.assertNotIn("KEYMATERIALSENTINEL", message)
        self.assertIs(caught.exception.request_sent, False)

    def test_rejected_exchange_never_echoes_the_jwt(self) -> None:
        sent: dict[str, str] = {}

        def urlopen(request, timeout=None):
            sent["authorization"] = request.get_header("Authorization")
            raise _http_error(request.full_url, 401, {"message": "A JSON web token could not be decoded"})

        with mock.patch.object(svc.urllib.request, "urlopen", urlopen):
            with self.assertRaises(svc.ServiceError) as caught:
                svc.github_app_token(_app(str(self.key)))
        jwt = sent["authorization"].split(" ", 1)[1]
        self.assertEqual(jwt.count("."), 2)
        message = str(caught.exception)
        self.assertNotIn(jwt, message)
        self.assertNotIn(jwt.split(".")[2], message)
        self.assertIn("401 A JSON web token could not be decoded", message)
        self.assertIsNone(caught.exception.__cause__)


class GitHubAppTokenTest(_TokenCacheIsolation):
    def setUp(self) -> None:
        super().setUp()
        jwt = mock.patch.object(svc, "github_app_jwt", return_value=JWT_SENTINEL)
        jwt.start()
        self.addCleanup(jwt.stop)
        self.requests: list = []

    def _urlopen(self, token: str = TOKEN_SENTINEL, expires_at: str = "2099-01-01T00:00:00Z"):
        def urlopen(request, timeout=None):
            self.requests.append(request)
            return _FakeResponse({"token": token, "expires_at": expires_at, "permissions": {}})

        return urlopen

    def test_exchange_posts_the_jwt_to_the_installation_endpoint(self) -> None:
        with mock.patch.object(svc.urllib.request, "urlopen", self._urlopen()):
            token, expires = svc.exchange_github_app_token(_app(), "2022-11-28")
        self.assertEqual(token, TOKEN_SENTINEL)
        self.assertEqual(expires, 4070908800.0)  # 2099-01-01T00:00:00Z
        (request,) = self.requests
        self.assertEqual(request.full_url, "https://api.github.com/app/installations/22/access_tokens")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {JWT_SENTINEL}")
        self.assertEqual(request.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(request.get_header("X-github-api-version"), "2022-11-28")

    def test_token_is_cached_per_app_and_installation(self) -> None:
        with mock.patch.object(svc.urllib.request, "urlopen", self._urlopen()):
            self.assertEqual(svc.github_app_token(_app()), TOKEN_SENTINEL)
            self.assertEqual(svc.github_app_token(_app()), TOKEN_SENTINEL)
            svc.github_app_token(_app(installation_id=23))
        self.assertEqual(len(self.requests), 2)

    def test_token_is_minted_again_when_under_five_minutes_remain(self) -> None:
        svc._github_app_tokens[(11, 22)] = ("ghs_old", time.time() + 200)
        with mock.patch.object(svc.urllib.request, "urlopen", self._urlopen(token="ghs_new")):
            self.assertEqual(svc.github_app_token(_app()), "ghs_new")
        self.assertEqual(len(self.requests), 1)

    def test_token_with_time_left_is_reused(self) -> None:
        svc._github_app_tokens[(11, 22)] = ("ghs_live", time.time() + 3000)
        with mock.patch.object(svc.urllib.request, "urlopen", self._urlopen()):
            self.assertEqual(svc.github_app_token(_app()), "ghs_live")
        self.assertEqual(self.requests, [])

    def test_the_same_client_mints_again_before_its_token_expires(self) -> None:
        # A cycle outlives one installation token: the client must not keep the
        # token it first used.
        clock = _Clock(1_900_000_000.0)
        tokens = iter(["ghs_first", "ghs_second"])
        sent: list[str] = []

        def urlopen(request, timeout=None):
            if request.full_url.endswith("/access_tokens"):
                self.requests.append(request)
                expires = dt.datetime.fromtimestamp(clock.now + 3600, dt.timezone.utc)
                return _FakeResponse({"token": next(tokens), "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")})
            sent.append(request.get_header("Authorization"))
            return _FakeResponse({"number": 7})

        with mock.patch.object(svc.GitHubClient, "_load_token", side_effect=AssertionError("gh must not be asked")):
            client = svc.GitHubClient("owner/repo", github_app=_app())
        with mock.patch.object(svc, "time", clock), mock.patch.object(svc.urllib.request, "urlopen", urlopen):
            client.request("GET", "repos/owner/repo/pulls/7")
            clock.now += 3600 - 301  # 301 s left: still above the 300 s margin
            client.request("GET", "repos/owner/repo/pulls/7")
            clock.now += 2  # 299 s left: this request must mint again
            client.request("GET", "repos/owner/repo/pulls/7")
        self.assertEqual(sent, ["Bearer ghs_first", "Bearer ghs_first", "Bearer ghs_second"])
        self.assertEqual(len(self.requests), 2)

    def test_concurrent_callers_share_one_mint(self) -> None:
        mints: list[int] = []

        def exchange(app, api_version):
            mints.append(1)
            time.sleep(0.05)
            return TOKEN_SENTINEL, time.time() + 3600

        results: list[str] = []
        with mock.patch.object(svc, "exchange_github_app_token", exchange):
            threads = [
                threading.Thread(target=lambda: results.append(svc.github_app_token(_app()))) for _ in range(8)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len(mints), 1)
        self.assertEqual(results, [TOKEN_SENTINEL] * 8)

    def test_http_error_carries_status_and_github_message_only(self) -> None:
        def urlopen(request, timeout=None):
            raise _http_error(
                request.full_url, 404, {"message": "Not Found", "echo": JWT_SENTINEL, "documentation_url": "x"}
            )

        with mock.patch.object(svc.urllib.request, "urlopen", urlopen):
            with self.assertRaises(svc.ServiceError) as caught:
                svc.github_app_token(_app())
        message = str(caught.exception)
        # The whole message: GitHub's `message` field and nothing else from the body.
        self.assertEqual(message, "GitHub App token for installation 22 failed: 404 Not Found")
        self.assertNotIn(JWT_SENTINEL, message)
        self.assertIs(caught.exception.request_sent, False)
        self.assertFalse(hasattr(caught.exception, "status_code"))
        self.assertFalse(hasattr(caught.exception, "transport_error"))

    def test_transport_error_is_reported_without_the_jwt(self) -> None:
        def urlopen(request, timeout=None):
            raise urllib.error.URLError("timed out")

        with mock.patch.object(svc.urllib.request, "urlopen", urlopen):
            with self.assertRaises(svc.ServiceError) as caught:
                svc.github_app_token(_app())
        self.assertIn("failed: timed out", str(caught.exception))
        self.assertNotIn(JWT_SENTINEL, str(caught.exception))
        # A failed mint sent nothing; it is not a transport failure of the caller's request.
        self.assertFalse(hasattr(caught.exception, "transport_error"))

    def test_response_without_a_token_does_not_echo_the_body(self) -> None:
        def urlopen(request, timeout=None):
            return _FakeResponse({"tok": TOKEN_SENTINEL, "expires_at": "2099-01-01T00:00:00Z"})

        with mock.patch.object(svc.urllib.request, "urlopen", urlopen):
            with self.assertRaises(svc.ServiceError) as caught:
                svc.github_app_token(_app())
        self.assertNotIn(TOKEN_SENTINEL, str(caught.exception))
        self.assertEqual(svc._github_app_tokens, {})


class GitHubAppSignerFailureTest(unittest.TestCase):
    def test_missing_or_hung_openssl_is_a_clean_not_sent_error(self) -> None:
        for failure in (FileNotFoundError(2, "No such file"), subprocess.TimeoutExpired(["openssl"], 30)):
            with self.subTest(type(failure).__name__), mock.patch.object(
                svc.subprocess, "run", side_effect=failure
            ), self.assertRaises(svc.ServiceError) as caught:
                svc.github_app_jwt(1, "/keys/app.pem")
            self.assertIn("openssl", str(caught.exception))
            self.assertIs(caught.exception.request_sent, False)


class GitHubAppClientTest(_TokenCacheIsolation):
    def setUp(self) -> None:
        super().setUp()
        # A live cached token: the client must use it without minting.
        svc._github_app_tokens[(11, 22)] = (TOKEN_SENTINEL, time.time() + 3600)

    def _client(self) -> svc.GitHubClient:
        with mock.patch.object(svc.GitHubClient, "_load_token", side_effect=AssertionError("gh must not be asked")):
            return svc.GitHubClient("owner/repo", github_app=_app())

    def test_requests_carry_the_installation_token(self) -> None:
        client = self._client()
        sent: list = []

        def urlopen(request, timeout=None):
            sent.append(request)
            return _FakeResponse({"number": 7})

        with mock.patch.object(svc.urllib.request, "urlopen", urlopen):
            self.assertEqual(client.get_pr(7), {"number": 7})
        self.assertEqual(sent[0].get_header("Authorization"), f"Bearer {TOKEN_SENTINEL}")
        self.assertEqual(sent[0].full_url, "https://api.github.com/repos/owner/repo/pulls/7")

    def test_bot_login_is_the_configured_login_without_an_http_call(self) -> None:
        client = self._client()
        with mock.patch.object(svc.urllib.request, "urlopen", side_effect=AssertionError("no HTTP")):
            self.assertEqual(client.bot_login(), BOT)

    def test_upsert_updates_the_apps_marker_comment_not_the_previous_identitys(self) -> None:
        client = self._client()
        marker = svc.PROGRESS_MARKER
        comments = [
            {"id": 1, "body": f"{marker}\nold progress", "user": {"login": "devacme"}},
            {"id": 2, "body": f"{marker}\nlive progress", "user": {"login": BOT}},
        ]
        with mock.patch.object(client, "list_issue_comments", return_value=comments), mock.patch.object(
            client, "update_issue_comment", return_value={"id": 2}
        ) as update, mock.patch.object(client, "create_issue_comment") as create:
            client.upsert_issue_comment(5, marker, "new body")
        update.assert_called_once_with(2, "new body")
        create.assert_not_called()

    def test_upsert_creates_a_new_comment_when_only_the_previous_identity_has_one(self) -> None:
        client = self._client()
        marker = svc.SUMMARY_MARKER
        comments = [{"id": 1, "body": f"{marker}\nold summary", "user": {"login": "devacme"}}]
        with mock.patch.object(client, "list_issue_comments", return_value=comments), mock.patch.object(
            client, "update_issue_comment"
        ) as update, mock.patch.object(client, "create_issue_comment", return_value={"id": 9}) as create:
            client.upsert_issue_comment(5, marker, "new body")
        update.assert_not_called()
        create.assert_called_once_with(5, "new body")

    def test_mint_failure_while_posting_is_not_an_unknown_posting_outcome(self) -> None:
        svc._github_app_tokens.clear()
        client = self._client()

        def exchange(app, api_version):
            raise svc._not_sent_error("GitHub App token for installation 22 failed: 401 Bad credentials")

        with mock.patch.object(svc, "exchange_github_app_token", exchange), mock.patch.object(
            svc.urllib.request, "urlopen", side_effect=AssertionError("the review POST must not be sent")
        ):
            with self.assertRaises(svc.ServiceError) as caught:
                client.create_review(5, "a" * 40, "body", [])
        self.assertFalse(svc.post_outcome_is_uncertain(caught.exception))

    def test_without_an_app_the_gh_constructor_call_is_unchanged(self) -> None:
        with mock.patch.object(svc, "GitHubClient") as constructor:
            svc.make_github_client("owner/repo", {"gh_bin": "gh-custom"})
        constructor.assert_called_once_with("owner/repo", gh_bin="gh-custom")

    def test_with_an_app_the_factory_builds_an_app_client(self) -> None:
        with mock.patch.object(svc.GitHubClient, "_load_token", side_effect=AssertionError("gh must not be asked")):
            client = svc.make_github_client("owner/repo", {"github_app": _app()})
        self.assertEqual(client.bot_login(), BOT)


class GitHubAppMintFailureWhilePostingTest(_TokenCacheIsolation):
    """Whatever breaks while minting, the review POST is never sent and the
    failure is never read as an unknown posting outcome."""

    TOKEN_URL = "https://api.github.com/app/installations/22/access_tokens"

    def _post_review(self, token_response: io.BytesIO, token_fetched: bool = True) -> svc.ServiceError:
        with mock.patch.object(svc.GitHubClient, "_load_token", side_effect=AssertionError("gh must not be asked")):
            client = svc.GitHubClient("owner/repo", github_app=_app())
        urls: list[str] = []

        def urlopen(request, timeout=None):
            urls.append(request.full_url)
            if request.full_url == self.TOKEN_URL:
                return token_response
            raise AssertionError("the review POST must not be sent")

        with mock.patch.object(svc, "github_app_jwt", return_value=JWT_SENTINEL), mock.patch.object(
            svc.urllib.request, "urlopen", urlopen
        ):
            with self.assertRaises(svc.ServiceError) as caught:
                client.create_review(5, "a" * 40, "body", [])
        self.assertEqual(urls, [self.TOKEN_URL] if token_fetched else [])
        self.assertIs(caught.exception.request_sent, False)
        self.assertFalse(svc.post_outcome_is_uncertain(caught.exception))
        self.assertEqual(svc._github_app_tokens, {})
        return caught.exception

    # Each failure is named by the exchange itself; the catch-all in
    # github_app_token ("could not be minted: <type>") would also be not-sent,
    # so the exact message is what proves the exchange handled it.
    NO_TOKEN_AND_EXPIRY = "GitHub App token for installation 22: GitHub returned no token and expiry"

    def test_truncated_token_response(self) -> None:
        partial = b'{"token": "' + TOKEN_SENTINEL[:12].encode("ascii")
        error = self._post_review(_RawResponse(failure=http.client.IncompleteRead(partial, 80)))
        self.assertEqual(str(error), "GitHub App token for installation 22 failed: IncompleteRead")
        self.assertNotIn(TOKEN_SENTINEL[:12], str(error))

    def test_token_response_that_is_not_utf8(self) -> None:
        error = self._post_review(_RawResponse(b'{"token": "\xff\xfe", "expires_at": "2099-01-01T00:00:00Z"}'))
        self.assertEqual(str(error), self.NO_TOKEN_AND_EXPIRY)

    def test_token_response_that_is_not_json(self) -> None:
        error = self._post_review(_RawResponse(b"<html>unicorn</html>"))
        self.assertEqual(str(error), self.NO_TOKEN_AND_EXPIRY)

    def test_read_timeout_on_the_token_response(self) -> None:
        error = self._post_review(_RawResponse(failure=TimeoutError("timed out")))
        self.assertEqual(str(error), "GitHub App token for installation 22 failed: TimeoutError")

    def test_unexpected_exception_inside_acquisition_is_normalized(self) -> None:
        with mock.patch.object(svc, "exchange_github_app_token", side_effect=RuntimeError(TOKEN_SENTINEL)):
            error = self._post_review(_FakeResponse({}), token_fetched=False)
        self.assertNotIn(TOKEN_SENTINEL, str(error))
        self.assertIn("RuntimeError", str(error))

    def test_token_that_cannot_go_in_a_header_is_refused_and_not_cached(self) -> None:
        for bad in ("ghs_abc\n", "ghs_abc ", "ghs abc", "ghs_\u00e9t\u00e9", "ghs_a\r\nX-Injected: 1", "x" * 513):
            with self.subTest(repr(bad)):
                svc._github_app_tokens.clear()
                error = self._post_review(_FakeResponse({"token": bad, "expires_at": "2099-01-01T00:00:00Z"}))
                self.assertNotIn(bad.strip(), str(error))

    def test_unparseable_expiry_is_refused(self) -> None:
        self._post_review(_FakeResponse({"token": TOKEN_SENTINEL, "expires_at": "in an hour"}))


class RequestBuildFailureTest(unittest.TestCase):
    """Anything that breaks while building the request, before urlopen, is a
    not-sent failure; http.client would otherwise raise a bare ValueError from
    inside urlopen (with the header value in its message)."""

    def _gh_client(self, token: str) -> svc.GitHubClient:
        client = svc.GitHubClient.__new__(svc.GitHubClient)
        client.repo = "owner/repo"
        client.gh_bin = "gh"
        client.api_version = "2022-11-28"
        client.token = token
        client._bot_login = "bot-login"
        return client

    def _assert_not_sent(self, call) -> svc.ServiceError:
        with mock.patch.object(svc.urllib.request, "urlopen", side_effect=AssertionError("must not be sent")):
            with self.assertRaises(svc.ServiceError) as caught:
                call()
        self.assertIs(caught.exception.request_sent, False)
        self.assertFalse(svc.post_outcome_is_uncertain(caught.exception))
        return caught.exception

    def test_header_invalid_token_is_not_sent_and_not_echoed(self) -> None:
        client = self._gh_client("ghp_SECRETVALUE\r\nX-Injected: 1")
        error = self._assert_not_sent(lambda: client.create_review(5, "a" * 40, "body", []))
        self.assertNotIn("ghp_SECRETVALUE", str(error))

    def test_unserializable_body_is_not_sent(self) -> None:
        client = self._gh_client("ghp_fine")
        self._assert_not_sent(lambda: client.request("POST", "repos/owner/repo/issues/5/comments", {"body": object()}))


class GitHubAppRevokedTokenTest(_TokenCacheIsolation):
    API_URL = "https://api.github.com/repos/owner/repo/pulls/7"

    def test_401_drops_the_cached_token_without_retrying(self) -> None:
        tokens = iter(["ghs_revoked", "ghs_fresh"])
        api_calls: list[str] = []
        answers = iter(["401", "200"])

        def urlopen(request, timeout=None):
            if request.full_url.endswith("/access_tokens"):
                return _FakeResponse({"token": next(tokens), "expires_at": "2099-01-01T00:00:00Z"})
            api_calls.append(request.get_header("Authorization"))
            if next(answers) == "401":
                raise _http_error(request.full_url, 401, {"message": "Bad credentials"})
            return _FakeResponse({"number": 7})

        with mock.patch.object(svc.GitHubClient, "_load_token", side_effect=AssertionError("gh must not be asked")):
            client = svc.GitHubClient("owner/repo", github_app=_app())
        with mock.patch.object(svc, "github_app_jwt", return_value=JWT_SENTINEL), mock.patch.object(
            svc.urllib.request, "urlopen", urlopen
        ):
            with self.assertRaises(svc.ServiceError) as caught:
                client.get_pr(7)
            self.assertEqual(caught.exception.status_code, 401)
            self.assertEqual(api_calls, ["Bearer ghs_revoked"])  # no automatic retry
            self.assertEqual(svc._github_app_tokens, {})
            self.assertEqual(client.get_pr(7), {"number": 7})
        self.assertEqual(api_calls, ["Bearer ghs_revoked", "Bearer ghs_fresh"])

    def test_401_keeps_a_token_another_request_already_replaced(self) -> None:
        svc._github_app_tokens[(11, 22)] = ("ghs_newer", time.time() + 3600)
        svc.drop_github_app_token(_app(), "ghs_revoked")
        self.assertEqual(svc._github_app_tokens[(11, 22)][0], "ghs_newer")


class GitHubAppErrorRedactionTest(_TokenCacheIsolation):
    PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAsecretbody\n-----END RSA PRIVATE KEY-----"
    SECRETS = (
        TOKEN_SENTINEL,
        "ghp_PERSONALTOKENSENTINEL123",
        "github_pat_11FINEGRAINED_SENTINEL",
        "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiIxMSJ9.c2lnbmF0dXJl",
        "MIIEowIBAAKCAQEAsecretbody",
    )

    def _mint_error(self, urlopen) -> str:
        with mock.patch.object(svc, "github_app_jwt", return_value="header.payload.signature"), mock.patch.object(
            svc.urllib.request, "urlopen", urlopen
        ), self.assertRaises(svc.ServiceError) as caught:
            svc.github_app_token(_app())
        return str(caught.exception)

    def test_secrets_reflected_in_githubs_message_are_redacted(self) -> None:
        reflected = "Bad credentials: {} / {} / {} / Bearer {} / {}".format(
            TOKEN_SENTINEL, "ghp_PERSONALTOKENSENTINEL123", "github_pat_11FINEGRAINED_SENTINEL",
            "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiIxMSJ9.c2lnbmF0dXJl", self.PEM,
        )

        def urlopen(request, timeout=None):
            raise _http_error(request.full_url, 401, {"message": reflected})

        message = self._mint_error(urlopen)
        for secret in self.SECRETS:
            self.assertNotIn(secret, message)
        self.assertIn("401 Bad credentials: [redacted]", message)

    def test_githubs_message_is_bounded(self) -> None:
        def urlopen(request, timeout=None):
            raise _http_error(request.full_url, 422, {"message": "x" * 5000})

        message = self._mint_error(urlopen)
        self.assertLessEqual(len(message.split("failed: 422 ", 1)[1]), 200)

    def test_transport_reason_is_redacted_and_bounded(self) -> None:
        def urlopen(request, timeout=None):
            raise urllib.error.URLError(f"proxy refused Bearer {TOKEN_SENTINEL} " + "y" * 5000)

        message = self._mint_error(urlopen)
        self.assertNotIn(TOKEN_SENTINEL, message)
        self.assertLessEqual(len(message.split("failed: ", 1)[1]), 200)

    def test_openssl_stderr_is_redacted(self) -> None:
        stderr = ("unable to load key: " + self.PEM.replace("\n", " ")).encode("utf-8")
        failed = subprocess.CompletedProcess(["openssl"], 1, stdout=b"", stderr=stderr)
        with mock.patch.object(svc.subprocess, "run", return_value=failed), self.assertRaises(
            svc.ServiceError
        ) as caught:
            svc.github_app_jwt(11, "/keys/app.pem")
        self.assertNotIn("MIIEowIBAAKCAQEAsecretbody", str(caught.exception))
        self.assertIn("/keys/app.pem", str(caught.exception))


class SelfApprovalRetryGuardTest(unittest.TestCase):
    """review_pr re-posts as COMMENT only after a definite 422, so a second
    POST of the same review never follows an attempt that may have landed."""

    TEXT = (
        "GitHub API POST https://api.github.com/repos/o/r/pulls/422/reviews failed: %s "
        "Can not approve your own pull request"
    )

    def _error(self, code: int, **attrs: object) -> svc.ServiceError:
        error = svc.ServiceError(self.TEXT % code)
        for name, value in attrs.items():
            setattr(error, name, value)
        return error

    def test_definite_422_is_a_self_approval_rejection(self) -> None:
        self.assertTrue(svc.is_self_approval_rejection(self._error(422, status_code=422)))
        self.assertTrue(svc.is_self_approval_rejection(self._error(422)))  # message-only double

    def test_ambiguous_or_unsent_attempts_are_not(self) -> None:
        self.assertFalse(svc.is_self_approval_rejection(self._error(502, status_code=502)))
        self.assertFalse(svc.is_self_approval_rejection(self._error(422, transport_error=True)))
        self.assertFalse(svc.is_self_approval_rejection(self._error(422, request_sent=False)))


def _write_config(directory: Path, github_app: object = None, include_app: bool = True) -> Path:
    config = {
        "repositories": ["owner/repo"],
        "agents": [{"id": name, "prompt_path": f"../agents/{name}.md"} for name in ("a", "b", "c")],
        "agent_runner": {"command": ["python3", "runner.py"]},
    }
    if include_app:
        config["github_app"] = github_app
    path = directory / "reviewer.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


class GitHubAppConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "keys").mkdir()
        self.key = self.dir / "keys" / "app.pem"
        self.key.write_text("-----BEGIN RSA PRIVATE KEY-----\nKEYMATERIALSENTINEL\n", encoding="utf-8")

    def _load(self, github_app: object) -> dict:
        return svc.load_config(_write_config(self.dir, github_app))

    def test_absent_block_keeps_gh_mode(self) -> None:
        config = svc.load_config(_write_config(self.dir, include_app=False))
        self.assertNotIn("github_app", config)

    def test_relative_key_path_resolves_against_the_config_directory(self) -> None:
        config = self._load({**_app(), "private_key_path": "keys/app.pem"})
        self.assertEqual(config["github_app"], {**_app(), "private_key_path": str(self.key.resolve())})

    def test_bad_shapes_are_refused_at_load(self) -> None:
        cases = {
            "not an object": "github_app",
            "app_id zero": {**_app(str(self.key)), "app_id": 0},
            "app_id string": {**_app(str(self.key)), "app_id": "11"},
            "app_id bool": {**_app(str(self.key)), "app_id": True},
            "installation missing": {k: v for k, v in _app(str(self.key)).items() if k != "installation_id"},
            "key path empty": {**_app(str(self.key)), "private_key_path": " "},
            "login without [bot]": {**_app(str(self.key)), "bot_login": "review-of-legends"},
            "login only [bot]": {**_app(str(self.key)), "bot_login": "[bot]"},
        }
        for label, block in cases.items():
            with self.subTest(label), self.assertRaises(svc.ServiceError) as caught:
                self._load(block)
            self.assertIn("config.github_app", str(caught.exception))

    def test_missing_key_file_names_the_path(self) -> None:
        missing = self.dir / "keys" / "absent.pem"
        with self.assertRaises(svc.ServiceError) as caught:
            self._load(_app(str(missing)))
        self.assertIn(str(missing.resolve()), str(caught.exception))

    def test_key_directory_is_refused_without_reading_anything(self) -> None:
        keys = (self.dir / "keys").resolve()
        opened: list[str] = []

        def refusing(real_open):
            # Fails the test on any open of the key directory or a file in it;
            # everything else (the config file itself) opens normally.
            def guarded(file, *args, **kwargs):
                if isinstance(file, (str, bytes, os.PathLike)):
                    target = Path(os.fsdecode(file)).resolve()
                    if target == keys or keys in target.parents:
                        opened.append(str(target))
                        raise AssertionError(f"{target} was opened")
                return real_open(file, *args, **kwargs)

            return guarded

        with mock.patch("builtins.open", refusing(open)), mock.patch("io.open", refusing(io.open)), mock.patch(
            "os.open", refusing(os.open)
        ), self.assertRaises(svc.ServiceError) as caught:
            self._load(_app(str(self.dir / "keys")))
        self.assertEqual(opened, [])
        message = str(caught.exception)
        self.assertEqual(message, f"config.github_app.private_key_path not found or not a file: {keys}")
        self.assertNotIn("KEYMATERIALSENTINEL", message)


class GitHubAppDoctorTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.key = self.dir / "app.pem"
        self.key.write_text("unused: the provider is faked", encoding="utf-8")

    def _doctor(self, config_path: Path, token_side_effect, host_login: bool = True) -> tuple[int, str]:
        if host_login:
            gh = subprocess.CompletedProcess(["gh"], 0, stdout="gh-token\n", stderr="")
        else:
            gh = subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="You are not logged into any GitHub hosts.")
        output = io.StringIO()
        with mock.patch.object(svc.shutil, "which", return_value="/usr/bin/true"), mock.patch.object(
            svc, "run_command", return_value=gh
        ), mock.patch.object(svc, "github_app_token", side_effect=token_side_effect), contextlib.redirect_stdout(
            output
        ):
            code = svc.run_doctor(argparse.Namespace(config=str(config_path)))
        return code, output.getvalue()

    def test_reports_the_app_and_a_minted_token_without_printing_it(self) -> None:
        code, output = self._doctor(_write_config(self.dir, _app(str(self.key))), lambda app: TOKEN_SENTINEL)
        self.assertEqual(code, 0)
        self.assertIn(f"github app: id 11, installation 22, posts as {BOT}", output)
        self.assertIn("github app token: ok", output)
        self.assertNotIn(TOKEN_SENTINEL, output)

    def test_mint_failure_fails_the_doctor(self) -> None:
        failure = svc._not_sent_error("GitHub App token for installation 22 failed: 401 Bad credentials")
        code, output = self._doctor(_write_config(self.dir, _app(str(self.key))), failure)
        self.assertEqual(code, 2)
        self.assertIn(
            "github app token: FAILED (GitHub App token for installation 22 failed: 401 Bad credentials)", output
        )

    def test_with_an_app_no_host_gh_login_is_needed(self) -> None:
        # The App clones and posts, so a host without a gh login (review-host since
        # 2026-11-06) is healthy; deploy.yml's Verify step runs this doctor.
        code, output = self._doctor(_write_config(self.dir, _app(str(self.key))), lambda app: TOKEN_SENTINEL, host_login=False)
        self.assertEqual(code, 0, output)
        self.assertIn("gh auth token: not available (not needed: the GitHub App clones and posts)", output)

    def test_without_an_app_a_missing_host_gh_login_fails(self) -> None:
        code, output = self._doctor(
            _write_config(self.dir, include_app=False), AssertionError("must not mint without github_app"), host_login=False
        )
        self.assertEqual(code, 2)
        self.assertIn("gh auth token: not available", output)

    def test_without_an_app_nothing_is_minted(self) -> None:
        code, output = self._doctor(
            _write_config(self.dir, include_app=False), AssertionError("must not mint without github_app")
        )
        self.assertEqual(code, 0)
        self.assertNotIn("github app", output)


class ShippedGitHubAppConfigTest(unittest.TestCase):
    """Each installation provisions its own App; the public template has none."""

    def test_public_template_does_not_grant_a_production_app_identity(self) -> None:
        config = load_shipped_config()
        self.assertNotIn("github_app", config)


if __name__ == "__main__":
    unittest.main()
