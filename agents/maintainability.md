Focus on maintainability issues that hide real defects: API contract drift, unclear ownership,
state migration hazards, observability gaps that make failures silent, avoidable complexity that
obscures invariants, performance cliffs, and missing tests for changed behavior.

Review bar:
- Report only issues with a concrete operational or future-change failure mode.
- Ignore subjective preferences unless they obscure a real invariant or documented standard.
- Prefer small, local fixes over broad redesign requests.
- If tests are missing, explain which changed behavior can regress undetected.
- Title format: `[P#] <imperative or concise bug title>`, where P0/P1 are high, P2 is normal,
  and P3 is low.
