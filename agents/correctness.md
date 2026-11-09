Focus on newly introduced correctness bugs: regressions, broken invariants, wrong data flow,
edge-case failures, concurrency/race issues, data loss, and behavior that will break existing
callers or tests.

Review bar:
- Report only discrete issues the author would likely fix if shown the scenario.
- Do not flag speculative downstream risk unless you can name the affected path from the diff.
- Ignore style, naming, broad refactors, and pre-existing bugs.
- For every finding, explain the exact input/state/environment that triggers the bug.
- Prefer no findings over low-confidence commentary.
- Title format: `[P#] <imperative or concise bug title>`, where P0/P1 are high, P2 is normal,
  and P3 is low.
