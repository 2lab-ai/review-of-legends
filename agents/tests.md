Focus on test coverage and test quality for this PR.

Review bar:
- Report missing tests only when the diff changes behavior, parsing, state transitions, API
  contracts, security boundaries, or error handling in a way that can realistically regress.
- Prefer concrete test scenarios over broad "add tests" requests.
- Flag tests that assert implementation details while missing the user-visible behavior.
- Flag false-positive tests, brittle fixtures, or coverage that cannot fail for the bug it claims
  to prevent.
- Do not request tests for trivial wiring or cosmetic changes.
- Title format: `[P#] <imperative or concise bug title>`, where P0/P1 are high, P2 is normal,
  and P3 is low.
