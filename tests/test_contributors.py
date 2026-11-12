"""The contributor gate's pure rules (contributors.py): list parsing, the trust
decision, the pledge and verify commands, and the comments it writes."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from code_review_service import contributors as c

LIST = """\
# GitHub usernames that may submit implementation PRs, one per line.
alice
@Bob   # added by hand
release-bot[bot]
*@Example.com
*@sub.example.org
not a login!
*@
"""


class ParseListTest(unittest.TestCase):
    def test_logins_domains_and_invalid_lines(self) -> None:
        parsed = c.parse_list(LIST)
        self.assertEqual(frozenset({"alice", "bob", "release-bot[bot]"}), parsed.logins)
        self.assertEqual(frozenset({"example.com", "sub.example.org"}), parsed.domains)
        self.assertEqual(("not a login!", "*@"), parsed.invalid)

    def test_missing_file_is_empty(self) -> None:
        self.assertEqual(c.ContributorList(frozenset(), frozenset(), ()), c.parse_list(None))

    def test_herdr_format_is_accepted_as_is(self) -> None:
        herdr = "# GitHub usernames that may submit implementation PRs, one per line.\nogulcancelik\nakbash-bot\n"
        self.assertEqual(frozenset({"ogulcancelik", "akbash-bot"}), c.parse_list(herdr).logins)


class DecideTest(unittest.TestCase):
    approved = c.parse_list("alice\n*@example.com\n")
    empty = c.parse_list("")

    def decide(self, login, permission=None, identity=None, domains=(), user_id=0):
        return c.decide(
            login,
            permission=permission,
            approved=self.approved,
            trusted_domains=domains,
            identity=identity,
            user_id=user_id,
        )

    def test_write_permission_is_trusted(self) -> None:
        for permission in ("admin", "maintain", "write"):
            self.assertEqual(c.Decision(True, "%s permission" % permission), self.decide("mallory", permission))

    def test_read_or_triage_permission_is_not(self) -> None:
        for permission in ("read", "triage", "none", None):
            self.assertFalse(self.decide("mallory", permission).trusted)

    def test_listed_login_any_case(self) -> None:
        self.assertEqual(c.Decision(True, "approved list"), self.decide("ALICE"))

    def test_a_binding_belongs_to_the_account_not_the_login(self) -> None:
        identity = c.Identity("dave", "dave@example.com", 77)
        self.assertTrue(self.decide("dave", identity=identity, user_id=77).trusted)
        # The login was renamed away and someone else took it: no inherited trust.
        self.assertFalse(self.decide("dave", identity=identity, user_id=78).trusted)
        # Either side without an id falls back to the login.
        self.assertTrue(self.decide("dave", identity=c.Identity("dave", "dave@example.com"), user_id=78).trusted)

    def test_verified_email_at_a_listed_domain(self) -> None:
        decision = self.decide("dave", identity=c.Identity("Dave", "dave@Example.com"))
        self.assertEqual(c.Decision(True, "verified email at example.com"), decision)

    def test_verified_email_at_a_configured_domain(self) -> None:
        decision = self.decide("erin", identity=c.Identity("erin", "erin@company.example"), domains=["company.example"])
        self.assertTrue(decision.trusted)

    def test_identity_of_another_login_does_not_count(self) -> None:
        self.assertFalse(self.decide("mallory", identity=c.Identity("dave", "dave@example.com")).trusted)

    def test_email_at_an_untrusted_domain_does_not_count(self) -> None:
        self.assertFalse(self.decide("frank", identity=c.Identity("frank", "frank@other.example")).trusted)

    def test_subdomain_is_not_the_domain(self) -> None:
        self.assertFalse(self.decide("gina", identity=c.Identity("gina", "gina@evil.example.com")).trusted)

    def test_a_bot_is_decided_like_anyone_else_here(self) -> None:
        # Trusted bots are the gatekeeper's (contributor_gate.trusted_bots), checked before this.
        self.assertFalse(self.decide("dependabot[bot]").trusted)

    def test_login_note(self) -> None:
        text = "# header\nalice\n@Bob  # pledge signed in o/r#7\n"
        self.assertEqual("pledge signed in o/r#7", c.login_note(text, "bob"))
        self.assertEqual("", c.login_note(text, "ALICE"))
        self.assertIsNone(c.login_note(text, "carol"))

    def test_unknown_author(self) -> None:
        self.assertEqual(c.Decision(False, "not an approved contributor"), self.decide("mallory"))


class SettingsTest(unittest.TestCase):
    def test_defaults_are_off(self) -> None:
        settings = c.gate_settings({})
        self.assertFalse(settings["enabled"])
        self.assertEqual([], settings["trusted_email_domains"])
        self.assertTrue(settings["approve_fork_ci"])
        self.assertIsNone(settings["repositories"])

    def test_domains_are_normalized(self) -> None:
        settings = c.gate_settings({"contributor_gate": {"enabled": True, "trusted_email_domains": ["@Company.example", "*@b.example", "c.example"]}})
        self.assertEqual(["b.example", "c.example", "company.example"], settings["trusted_email_domains"])

    def test_bad_shapes_are_refused(self) -> None:
        for block in ({"enabled": "yes"}, {"trusted_email_domains": ["not a domain"]}, {"trusted_email_domains": 3},
                      {"trusted_email_domains": {"example.com": False}}, {"trusted_email_domains": "example.com"},
                      {"trusted_bots": "x"}, {"repositories": ["noslash"]},
                      {"approved_logins": ["not a login!"]}, {"approved_logins": "alice"}, []):
            with self.subTest(block=block), self.assertRaises(ValueError):
                c.gate_settings({"contributor_gate": block})

    def test_approved_logins_are_normalized(self) -> None:
        settings = c.gate_settings({"contributor_gate": {"approved_logins": ["@Alice", "release-bot[bot]"]}})
        self.assertEqual(["alice", "release-bot[bot]"], settings["approved_logins"])

    def test_gate_applies_to_listed_repositories_only(self) -> None:
        settings = c.gate_settings({"contributor_gate": {"enabled": True, "repositories": ["Acme-Corp/Public"]}})
        self.assertTrue(c.gate_applies(settings, "acme-corp/public"))
        self.assertFalse(c.gate_applies(settings, "acme-corp/private"))
        self.assertFalse(c.gate_applies(c.gate_settings({}), "acme-corp/public"))


class CommandsTest(unittest.TestCase):
    def test_pledge_sentence_tolerates_case_space_and_period(self) -> None:
        self.assertTrue(c.is_pledge("I have read the Contributor Pledge and I agree to it."))
        self.assertTrue(c.is_pledge("i have read the contributor pledge  and i agree to it"))
        self.assertFalse(c.is_pledge("I have read the Contributor Pledge"))
        self.assertFalse(c.is_pledge("I have read the Contributor Pledge and I agree to it, mostly."))

    def test_verify_code(self) -> None:
        code = c.new_verify_code()
        self.assertEqual(code, c.parse_verify("verify %s" % code))
        self.assertEqual(code, c.parse_verify("verify `%s`" % code.upper()))
        self.assertIsNone(c.parse_verify("verify"))
        self.assertIsNone(c.parse_verify("verify nope"))
        self.assertIsNone(c.parse_verify("verify %s extra" % code))

    def test_code_hash_is_stable_and_case_blind(self) -> None:
        self.assertEqual(c.code_hash("rol-ABC123"), c.code_hash(" rol-abc123 "))


class AddLoginTest(unittest.TestCase):
    def test_appends_and_keeps_the_rest(self) -> None:
        before = "# header\nalice\n*@example.com"
        after = c.add_login(before, "bob", "pledge signed in acme-corp/public#7")
        self.assertEqual(before + "\nbob  # pledge signed in acme-corp/public#7\n", after)
        self.assertIn("bob", c.parse_list(after).logins)

    def test_new_file_gets_a_header(self) -> None:
        after = c.add_login(None, "bob", "")
        self.assertTrue(after.startswith("# GitHub logins"))
        self.assertEqual(frozenset({"bob"}), c.parse_list(after).logins)

    def test_refuses_duplicates_and_non_logins(self) -> None:
        with self.assertRaises(ValueError):
            c.add_login("Bob\n", "bob", "")
        with self.assertRaises(ValueError):
            c.add_login("", "bob\nmallory", "")


class CommentsTest(unittest.TestCase):
    def test_gate_comment_offers_both_ways_in(self) -> None:
        body = c.gate_comment("mallory", "acme-corp/public", "main", domains=["example.com"], pledge=True, dashboard_url="https://reviews.example.com/")
        self.assertTrue(body.startswith(c.GATE_MARKER))
        self.assertIn("`@example.com`", body)
        self.assertIn("https://reviews.example.com/#contributor", body)
        self.assertIn("@review-of-legends " + c.PLEDGE_SENTENCE, body)
        self.assertIn("https://github.com/acme-corp/public/blob/main/.github/CONTRIBUTOR_PLEDGE.md", body)

    def test_without_pledge_or_dashboard_it_points_at_the_maintainers(self) -> None:
        body = c.gate_comment("mallory", "acme-corp/public", "main", domains=["example.com"], pledge=False)
        self.assertNotIn("verify", body)
        self.assertIn("A maintainer can add you", body)

    def test_korean(self) -> None:
        body = c.gate_comment("mallory", "acme-corp/public", "main", domains=[], pledge=True, lang="ko")
        self.assertIn("기여자 서약", body)


if __name__ == "__main__":
    unittest.main()
