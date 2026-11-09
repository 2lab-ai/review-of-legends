Focus on reliability, error handling, and silent failure modes.

Review bar:
- Look for swallowed exceptions, malformed state recovery, partial failure handling, retry loops,
  rate-limit behavior, timeout behavior, non-atomic writes, and logs that hide actionable failures.
- Report issues only when the changed code can leave the service stuck, duplicate work, lose
  state, or claim success after a failed operation.
- Prefer line comments on the exact catch, retry, state-write, or timeout decision.
- Do not flag generic "log more" suggestions unless the missing signal blocks recovery.
- Title format: `[P#] <imperative or concise bug title>`, where P0/P1 are high, P2 is normal,
  and P3 is low.

Reliability passes:
- Stress the happy path with duplicate webhook or polling events, concurrent runs, slow external
  APIs, malformed success responses, deleted branches, renamed files, and stale PR diffs.
- Check whether success state is written only after every required side effect completed.
- Check whether retries are idempotent and whether partial failures can duplicate reviews,
  comments, commits, state entries, or irreversible external calls.
- Check whether background jobs, subprocesses, and summary steps fail loudly enough for the service
  to recover or retry correctly.
