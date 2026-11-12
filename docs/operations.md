# Operations reference

Local macOS launchd service that watches configured GitHub repositories for open pull requests, runs
configurable review agents, and posts one GitHub PR review containing a summary plus line comments.

## How it works

- Authentication comes from `gh auth token`, unless a GitHub App is configured: then reviews and
  comments are posted as the App's bot (see "Posting as a GitHub App").
- PR data and review posting use the GitHub REST API.
- Repositories are configured in `config/reviewer.json` under `repositories`; an admin can add
  more from the dashboard's Settings page (see *Repositories added on the dashboard* below).
- Each review creates a fresh unique directory under `work_dir/runs`, clones the full PR source
  into it, checks out the PR head, and runs the review agents from that clone.
- All Markdown files under the cloned repository's `./rules/**/*.md` path are read and injected
  into every review agent prompt.
- Review comments are posted through the pull request review endpoint with `path`, `line`, and
  `side: RIGHT` on added lines. Findings on one line share a comment, a finding the bot already has
  an open thread for is not posted again, and outdated bot threads are resolved (see *Finding
  lifecycle*).
- Reviews are submitted as `APPROVE` only when **all** of the following hold; otherwise the service
  posts a non-approving `COMMENT` review:
  - `approval.enabled` is `true` in `config/reviewer.json` (default: `false`; opt-in because the
    LLM agents that decide P0/P1 see attacker-influenced PR titles, bodies, and diffs).
  - The PR author appears in the `approval.trusted_authors` allowlist (also in
    `config/reviewer.json`). An empty or missing list disables approvals entirely.
  - All configured agents returned a valid response (no quorum failure).
  - No agent emitted a P0/P1 finding, checked against the **raw** agent results — a P0/P1 that
    fails to map onto an added diff line still blocks approval.
  - The bot identity is not the PR author (GitHub blocks self-approval anyway; the service falls
    back to `COMMENT` on the 422).
- Duplicate reviews are avoided by storing the last reviewed head SHA in `data/state.json` under
  the installed service directory.
- On first daemon start, existing open PRs are recorded as a baseline by default. New PRs and new
  commits after that baseline are reviewed automatically. Set
  `review_existing_open_prs_on_first_run` to `true` to process the current backlog.
- A PR's review starts within one `watch_interval_seconds` (20 s) of it being opened or marked
  ready for review, unless `max_concurrent_reviews` reviews are already running (see
  *Scheduling*). Draft PRs are not reviewed until they are marked ready, unless `review_drafts` is
  `true` (also for `once` without `--pr`; an explicit `--pr N` reviews a draft); a queued review
  whose PR goes back to draft, closes or merges is dropped. A first-run baseline leaves drafts out,
  so marking one ready starts its first review.
- Reviews are not posted unless `min_successful_agents` agents return valid JSON, and every
  **required** agent finished (see *Configure agents*).
- A head whose review was not posted (quorum or a required agent failed, or the review raised: a
  clone timeout, a diff fetch or GitHub API error — stage `error`) is retried by the poll
  loop at most `auto_retry_attempts` times in total (default 3), waiting
  `auto_retry_backoff_seconds * 2^(failures - 1)` after each failure (default 600, so 10 then
  20 minutes); when set, both must be positive integers (`null` fails at startup). The count lives in `state.json` under the repository's `failed` map, restarts at 1
  on a new push, and is cleared when a review is posted. Once it is used up the daemon logs a
  skip line (once per head) instead of re-running every agent; the PR progress comment says whether
  another automatic attempt will run (and not before when, UTC) or that retries stopped. An admin
  retry from the dashboard and `once --force` always run; `once --pr N` without `--force` honours
  the limit.
- An attempt that ends without a review because of the quorum or a required agent keeps the
  results of the agents that succeeded, so the dashboard's **Retry failed agents** re-runs only the
  others (see *Admin dashboard*). The poll loop's automatic retries still run every agent.
- Findings use the same review bar as the Soma review workflow: newly introduced, discrete,
  actionable issues only, with P0-P3 priority tags and optional overall correctness verdicts.
- A separate executive summary agent synthesizes the completed review results into a Greptile-style
  PR timeline comment and updates the existing summary comment on subsequent reviews.
- Before the reviewer agents are dispatched, one agent writes a visual outline of the change (why,
  what a reviewer must know, and diff-shaped views of the implementation) and posts it as a PR
  comment — once per PR, never blocking the review. See below.
- Each posted PR review includes the running service version from `VERSION`. In deployed builds,
  that version is the release tag (for example `v1.0.3`) written by CI (see *Versions*).
- While the review agents run, a live progress comment is posted on the PR and updated as each
  agent finishes, showing per-agent focus, status, and a short result summary. Disable it with
  `progress_comment.enabled` set to `false` in `config/reviewer.json`.
- The default agent set includes correctness, security, maintainability, tests, reliability, and
  red-team review. The security and red-team agents borrow from gstack-style security review
  checklists: trust boundaries, LLM/tool-output handling, supply-chain risk, silent failure paths,
  and adversarial integration breaks.
- The launchd service runs `python3 -m code_review_service --config ./config/reviewer.json daemon`.

### Finding lifecycle

A pull request is reviewed again on every push, so the review decides what its inline comments
add to the threads already on the PR. Four switches in `config/reviewer.json` under
`finding_lifecycle`, each `true` unless set to `false` (an absent block or key is on):

- **`merge_same_line`** — findings that land on the same `path:line` in one review become one
  inline comment. Its header is the worst priority's title; below it each finding is listed as
  `agent · priority · title` with its body. The review body's Findings list stays one line per
  finding, and its `Line comments: X/Y` counts the findings carried inline. One comment carries at
  most 40 findings (a busier line gets one comment per 40, each counted by `max_review_comments`),
  and a comment that would pass 60,000 characters (GitHub refuses a review with a comment over
  65,536) has each finding body cut to an equal share, marked as cut; every header and agent line
  stays, and a cut never stops inside code or raw HTML (it goes back to before the line that starts
  the code, or before the first `<`-tag), so no partial suggestion is left to apply. Only plain
  bodies share a comment: a finding whose body has fenced code (a line starting with ```` ``` ```` or
  `~~~`, also inside any mix of quote and list markers, and after a bare carriage return) or anything that may be raw HTML (`<`
  followed by a letter, `/`, `!` or `?`, anywhere, code spans included: an unclosed `<details>`,
  `<pre>` or `<!--` wraps what follows), or whose title has such raw HTML, gets an inline comment
  of its own. Every inline comment ends with a metadata
  line GitHub does not show (`<!-- review-of-legends-finding <base64 JSON> -->`: format version `v`,
  the review's instructions key `instr`, the agents and each finding's title key), also with every
  switch off; later reviews read a thread's agents and titles only from that line, never from the
  finding bodies an agent wrote. The instructions key is a short hash of what sets the words the
  agents write: the review language (`@review-of-legends lang`), the repository rules and every
  agent's prompt (dashboard override or prompt file, read once before the agents start; they run
  on the same texts, so a file edited during the review changes neither); `""` when a prompt
  cannot be read, and in a
  comment carrying a finding a dashboard retry reused from an earlier attempt (written under that
  attempt's instructions). A line of
  another version, or with a field that is not a list of non-empty strings, names nothing. A comment
  posted before that line existed is read from its header and closing
  `_Agent: …; confidence: …_` line (for G2 and the feedback tally only).
- **`skip_open_duplicates`** — before posting, the service lists the PR's review threads
  (GraphQL `reviewThreads`, 100 per page). A new finding is not posted inline on a line where the
  bot's own earlier thread is still open (not resolved, not outdated, same current line, on the
  file's current name when GitHub reports it renamed), unless
  the new finding is of a higher priority (smaller P number). The review body lists what was held
  back under **Not posted again: the line has an open earlier thread**, with a link to the thread
  (at most 20 entries, then `…and N more`). A bot thread is *answered* when a person replied: a
  comment after the first that no GitHub App wrote (GraphQL author type `Bot`: this bot, Codex,
  CodeRabbit, …) by the PR's author (the same login, letter case aside, whatever its
  association) or by an `OWNER`, `MEMBER`, `COLLABORATOR` or `CONTRIBUTOR` (the comment's
  `authorAssociation`; a first-time contributor's or anyone else's reply is no answer), with every
  comment of the thread listed (the listing reads the first 100). `CONTRIBUTOR` counts because,
  read with the App's installation token, org members who have committed to the repository come
  back as `CONTRIBUTOR` (a production PR, 2026-10-21: 191 replies read `CONTRIBUTOR` with the App
  token and `MEMBER` with a user token); a member who never committed there reads as `NONE` or
  `FIRST_TIME_CONTRIBUTOR` and answers only as the PR author. Counting `CONTRIBUTOR` assumes
  private repositories: on a public one it also admits outside people who once had a commit
  merged (every repository in `config/reviewer.json` is private, 2026-10-21). A P2/P3 (or a
  priority-less medium/low) is not posted inline either when an answered thread that names its
  agent is in the same file (by its current name) and either on the same current line (and
  resolved) or, when the thread's comment is that agent's alone, under a title that shares with
  the new one at least two words and at least half of the shorter title's words, resolved or
  not, outdated or not. Words are in lower case, without the priority and lens prefixes such as
  `net:`, with a backticked span or an identifier joined by `_` or `.` (`retry_key`,
  `ISportSummary.GetLeague`) as one word, without one-letter tokens (a Korean particle split off
  next to an identifier), and without small words like "the", "in", "with" or the words the
  agents' titles are written around ("add", "missing", "test", "cover", "remove", "use",
  "existing", "helper", …; under a Korean review language 테스트, 테스트를, 추가, 추가하세요,
  검증, 확인, 누락, 없음, 대한, 사용하지, 않는, 헬퍼, 제거, 삭제, …, so "빈 리그 케이스 테스트
  추가" and "이름 변경 경로 테스트 추가" share no word). A title under another of simplify's
  remedy tags (`delete:`, `stdlib:`, `native:`, `reuse:`, `yagni:`, `shrink:`) never
  matches; any other lens (server-architect's `db:`, `net:`, `capacity:`, which file
  locking under both `db:` and `net:`), or none, compares by its words. A merged
  comment of several agents does not say whose each title is, so only the same-line rule reads
  it, and so does a thread whose latest answer starts by saying it was fixed (수정했, 추가했,
  반영했, 고쳤, 바꿨, 삭제했, 제거했, 옮겼, 적용했; "fixed", "added", "done", "addressed",
  "updated", "removed", "changed", "applied"): the findings to keep off are those declined with
  a reason, and a new gap in a fix reads like the fixed one (a production PR: "Cover a multi-batch
  delete-only reconciliation" after 「추가했습니다(MySQL). …」 on "Exercise reconciliation across
  the 200-league batch boundary"). A marker later in the answer ("이 부분은 수정했지만
  유지합니다") does not count. Markdown before the first word is skipped: one heading or list
  marker (`#`, `-`, `*`, `1.`), then emphasis or code markers (`**`, `__`, `*`, `_`, a
  backtick), so `**Added** a MySQL test.`, `__수정했습니다__` and `- 수정했습니다` read as fixed;
  a quote (`> 수정했습니다 …` repeats someone else's words) and a strikethrough
  (`~~수정했습니다~~ 유지합니다` takes the word back) do not. A production PR is why: answered "Materialize the league in the locking query" (4 words)
  came back on other lines once the thread was outdated as "Return the league entity from the
  locking read" (2 of the 4 shared) and "Load league entities in the locking query" (3 of 4);
  "Preserve manual league fields in team-driven saves" (1 shared) does not match and is
  posted. This matches the agent, the file and the line or the words, not the finding: each
  such finding is listed in the review body under
  **Not posted again: answered on an earlier thread** with a link
  to the newest such thread and its text quoted (cut to 600 characters, and before any fenced
  code or raw HTML). A P0/P1 is always posted. A thread's current line is GitHub's mapping onto the
  PR's head of that moment, so threads count (here and for resolving) only when GitHub gave them
  for the head and base this review read; after a push during the review every finding is posted.
  With this switch on and a GitHub App posting, the service also lists the threads before the
  agents start (when any agent runs) and puts the bot's answered findings, open or resolved, into
  every agent's prompt: the findings on current code first (threads GitHub does not mark outdated
  and still maps onto a line), then the outdated ones, newest first in each, one per finding (the
  same agents on one line, or under one title once outdated), each with its latest answer on one
  line cut to 400 characters, at most 80 entries and 32,000 characters for the whole block. The
  findings on current code are the ones an agent can raise again, so they are kept by cutting
  their answers shorter before any is dropped: when they do not all fit, every one of their
  answers is cut to 200, then 100 characters, then left out (the title line alone), the longest
  at which they all fit; when not even their title lines all fit, as many title lines as fit are
  kept, newest first. The outdated ones take the room left, answers cut to 400; one that does not
  fit is skipped and counted. An entry without its answer still counts as answered. The log line
  says how many of each the agents got and where the answers on current code were cut (`… N/N on
  current code, n/M outdated, replies cut at 200`). The prompt tells the agents that a P2/P3 a
  reply declined with a reason is settled and comes back only as a P0/P1 that names what the
  reason does not answer; that a P0/P1 comes back while the diff still shows the defect unless
  the reply shows it is not one ("kept as is", "intended" or "a later follow-up" do not settle
  it); and that a finding a reply says was fixed comes back only if the diff still shows it. A
  failed listing runs the agents without the block (logged as a warning), never costs the review.
- **`resolve_outdated_threads`** — after the review POST is confirmed, the service resolves the
  bot's own threads that are not resolved, that GitHub marks outdated (the commented code changed
  in a later commit), that nobody but the bot commented on, whose comment ends with the metadata
  line of this format and this review's instructions key, and whose title this review does not raise again
  in the same file (a renamed file's old or current name counts as the same file; a thread on a
  name that is both, as with `a.py -> b.py` and `b.py -> c.py`, or a renamed-away name the PR has
  as a file again, is left to people and not used by
  G2, and a finding raised under such a name keeps the threads of both files open). Titles compare
  exactly once the priority prefix is stripped, spacing collapsed and case folded; the words after
  the prefix are cut at 160 characters on every run, so the same title always gives the same key. A
  thread without that line (every comment posted before this feature) or written under other
  instructions (the language, the repository rules or a prompt changed between reviews) is left to
  people: its title cannot show the finding was not raised again; the log counts these threads. A finding an agent
  places on a line the diff did not add still counts as raised (it gets no inline comment, but it
  keeps the thread open). A thread is also kept open when an agent it names did not run to the end in
  this review (a quorum can post without it) or was reused from an earlier attempt by a dashboard
  retry (it read that attempt's diff), or unless its file's whole patch reached the agents: a file
  cut at `max_diff_chars`, omitted by GitHub for a large file, or not in the PR's file list under
  its current name (GitHub's reported renames are followed; a reverted or further-moved file is
  not re-checked) keeps its threads open. Paths compare exactly (`a.py` and `src/a.py` are two
  files) once `.`, `..` and doubled slashes are folded; an absolute path inside the review's clone becomes the repository path, and any other
  longer path counts as raised for every PR file it ends in (a wrong pick could resolve a thread the
  finding keeps open). The review body names
  the threads first under
  **Resolving earlier threads** (at most 20 entries, then `…and N more`). Right before its own
  resolve, each thread is read again (GraphQL `node`, with the PR's current head and base) and
  resolved only if it still qualifies (someone may have answered while the review was being
  posted) and the PR is still on the head and base this review read; once either moved the rest
  stay open for the next review. A thread that cannot be read again stays open. The final progress
  note says `resolved X/Y earlier threads` (plus how many were skipped or left for a newer head or
  base). A failed resolve is logged and the rest go on, except GitHub's *Resource not accessible
  by integration*: the rest would get the same answer, so they are not tried and the note says so.
  With a GitHub App, threads are resolved only when the App's installation token carries
  **Contents: write**: GitHub refused `resolveReviewThread` to the App with Contents read
  (live check, PR #42, 2026-10-15). With Contents read, or before the permissions of a token are
  known, the same threads are listed under **Looks fixed, for people to resolve** (at most 20 entries, then `…and N more`) with the reason,
  and no resolve is tried. The token's permissions come with each token GitHub mints (hourly), so a
  granted Contents write takes effect at the next token without a deploy.
- **`feedback_tally`** — with a GitHub App (the only identity whose threads are claimed), the last
  line each inline comment shows is *React with 👍 or 👎 to rate this finding*.
  The same thread listing reads the thumbs reactions on the first comment of every bot thread of
  the PR (open, outdated or resolved), and the next review body counts them per agent under
  **Feedback on earlier findings** (`👍 U · 👎 D on N earlier findings`, then one line per agent;
  a merged comment credits every agent it lists). Other reactions are ignored. Nothing is stored
  outside GitHub yet: the tally is per pull request.

The three sections take only what the rest of the review body leaves under 60,000 characters:
entries that do not fit are counted in `…and N more`, and a section whose heading does not fit is
left out, so the sections never make GitHub refuse a review that would have posted.

Only threads whose first comment is a bot finding (author = the App bot: GraphQL actor type `Bot`
and the bot's login, which has no `[bot]` suffix there; and a `**P2 - …**` / `**HIGH - …**`
header) are considered: threads opened by people or other bots, and bot threads anyone else
answered (a person holding the bare login included), are never touched. Without a GitHub App the
service posts as a person, nothing tells which of that person's comments it wrote, so the lifecycle
claims no thread at all and reviews post as before. Nothing asks a model
whether a finding was fixed, no reply is posted on a thread, and a resolved thread is never
reopened. If listing or reading the threads fails, the review is posted as before (no repeat check, nothing
resolved) and the progress comment says so; a review POST that fails or whose outcome is unknown
resolves nothing. Inline comment titles also lose the priority an agent wrote into them
(`[P2] …`, `P2 - …`, `P2: …`; a separator followed by a space, so `P1 -> P2 …` stays a title), so
the header names the priority once; a finding with no numeric priority keeps its title as written,
since its header shows the severity (`**LOW - P0: …**`).

### Scheduling

The daemon (`code_review_service/scheduler.py`) does not walk the repositories one PR at a time:

- **Watching.** Every `watch_interval_seconds` (default 20) it re-reads `config/reviewer.json` (a
  file that does not load or validate is logged once and the last good config stays in effect;
  new caps and intervals apply to reviews that start afterwards) and lists each review
  repository's open PRs — the config's and the dashboard-added ones. The listing is a conditional
  request (`If-None-Match` with the last `ETag`), so an unchanged list is answered `304` and costs
  no GitHub rate limit. When fewer than 300 API calls are left (`X-RateLimit-Remaining`, also
  read from a refused listing), or a listing is refused for a rate limit (a `429`, or a `403`
  that says so: a secondary limit, which leaves quota), it lists every `poll_interval_seconds`
  instead until the limit recovers, and logs both changes once. A refused listing also ends that
  watch: every repository uses the same token.
- **Queue.** A head is queued when it is not a draft, not already in the ledger, not in flight or
  already queued (a newer head replaces the queued one), and not held back by the automatic retry
  limit. A PR with no real review yet (new, just out of draft, or only in the first-run baseline)
  is a *first* review; a new head on a reviewed PR is an *update*. First reviews go before
  updates; within each, the head seen earliest goes first, whatever repository it is in (heads
  seen in the same watch: the PR opened first). Each repository's listing is queued as it
  arrives, so at the hard cap a repository listed earlier in a watch can take a slot before an
  older PR of a repository listed later. A queued first review becomes an update if a `once`
  reviewed an earlier head of that PR meanwhile; a queued head that is already reviewed is dropped.
- **Starting.** A first review starts at once, on its own thread, unless `max_concurrent_reviews`
  reviews (default 8; operator retries included) are already running — the hard safety cap.
  Only then does it wait, with a `WARNING … waits` line once per PR, and it starts the moment a
  review ends. Updates run at most `max_concurrent_update_reviews` (default 2) at a time, so they
  can never fill the cap. The latency claim is therefore: a non-draft PR's review starts within
  one watch interval of it being opened or marked ready, unless the hard cap is reached.
- **Re-read before it starts.** The PR is read from GitHub once more just before its review: a PR
  closed, merged or back to draft meanwhile is not reviewed, and a head that moved is checked
  against the ledger and the retry limit and reviewed instead. A failed read skips it until the
  next watch. A pause or stop that arrives during the read still keeps the review from starting:
  nothing is recorded for it, it goes back in the queue, and it starts once the executor resumes.
- **Load.** Each review runs up to `max_parallel_agents` agents (6), so up to
  `max_concurrent_reviews × max_parallel_agents` (48 with the shipped values) LLM calls can be in
  flight through llmux at once, plus one clone per review on disk.
- **Retries.** Operator retries from the dashboard run on their own thread and count toward the
  hard cap. A PR is never reviewed by the retry thread and a review slot at the same time. A
  claimed retry that has not begun its attempt goes back to the queue (not lost, not failed) if the
  executor is paused or stopping meanwhile (also while the PR is being re-read) or if the PR
  changed, and runs (or is closed as stale) on its next claim. If the store cannot take that
  write, the request is returned to the queue before the next claim; if the daemon stops first,
  restart recovery closes it as interrupted.
- **Log.** Every start is one line, e.g. `dispatch acme-corp/Gamma pr #4769 1a2b3c4d5e6f
  (first) 14s after opened`: measured from the PR's `created_at`, from when the daemon saw it leave
  draft (`ready`), or from when it saw the head (`seen`, updates, baselined and re-tried heads).
- **Executor lock.** Held while anything runs (a review, a retry, a baseline or ledger write, the
  sweep or prune) and released when nothing does, so `once` still works between reviews and the
  dashboard's executor state means what it did. `state.json` is re-read whenever the lock is
  taken again, because a `once` may have written it meanwhile; if it cannot be read, nothing
  starts until it can (one WARNING per distinct error). The lock, pause and state paths are read
  once, at start.
- **Recovery.** A dashboard store that cannot be opened at start is opened again every
  `poll_interval_seconds`; once open, attempts are recorded and operator retries run. A dispatch
  or retry thread that stops is started again on the next watch, with one `ERROR` line.
- **Reviewed status.** After each listing the watcher sets `review-of-legends/reviewed/<base>`
  to `success` on every open, non-draft PR that has had a review, on a base and head it was not
  set on yet (see *Commit statuses and merge rulesets*), at most 20 per watch across
  repositories. After a POST that fails it sets none in that repository for
  `poll_interval_seconds` (180 s), with one `WARNING` until a batch in that repository ends
  without a refusal.
- **Mentions.** After each listing, while the dashboard store is open, the watcher reads the
  repository's PR comments for `@review-of-legends` commands (see *Talking to the bot*): two more
  conditional requests per repository, answered `304` while nothing is new.

`once` keeps the old serial cycle (`--repo`, `--pr`, `--force`, `--dry-run` unchanged). It sets
the statuses of the reviews it runs, but not the watcher's reviewed status on other heads.

### Commit statuses and merge rulesets

The service sets two commit statuses on PR heads, both ending in the PR's base branch. Merging
is gated by requiring them in each repository's ruleset, pinned to the GitHub App "Review of
Legends" (your App, the identity the daemon posts as; see *Posting as a GitHub App*).

| Context | Where | States |
|---|---|---|
| `review-of-legends/reviewed/<base branch>` | every repository | `success` only |
| `review-of-legends/required/<base branch>` | repositories with a required agent | `pending`, then `success`, `failure` or `error` |

**`reviewed/<base>` is success once a Review of Legends review has been posted on this PR at
least once (any head; the context names the base the PR targets now). A run that posts nothing
does not count. Push again, or retry from the dashboard.** The service sets nothing on a PR without a posted review, so a ruleset that requires
the context keeps that PR from merging (unless its head commit and base are those of a reviewed
PR; see below).

- `reviewed_status` (default `true`) turns it on; `false` turns it off. A dry run sets nothing.
- A review sets it when the review is posted ("reviewed (<head>)"). A review POST whose outcome is
  unknown (a timeout or 5xx after sending) sets it too ("reviewed (<head>); the posted review
  could not be confirmed"): every agent ran and the POST most likely landed, and the ledger
  already counts that head as reviewed, so no retry may post it again (a Retrigger re-run is
  refused on such a head too, until the POST is reconciled against GitHub).
- No run sets `pending`, `error` or `failure` on it. A run that fails leaves whatever status the
  head already has, so it can never overwrite another PR's success on a shared head or leave a
  stuck error.
- The watcher sets it on each new head of a reviewed PR within one watch of the push ("reviewed
  at least once (last review <sha>); this head's own review runs separately"), without waiting
  for that head's own review, so a reviewed PR stays mergeable on every head. A review counts for
  the base the PR targets now: when a reviewed PR is retargeted, the watcher sets the new base's
  context on its next watch. The same pass sets it once on PRs reviewed before the status
  existed, at most 20 per watch, so the first watches after the deploy fill it in.
- GitHub keys a status by commit and context, so the context names the base: a PR on another base
  that shares a reviewed PR's head commit gets nothing from it. A PR with the same head commit
  **and** the same base as a reviewed PR does show its success; merging it produces exactly the
  result the reviewed PR may already merge. A base name with characters other than letters,
  digits, `.`, `_`, `/` and `-`, or longer than 100 characters, has those characters replaced by
  `-`, is shortened, and ends in `~` plus 32 hex characters (128 bits) of the name's SHA-256
  (`rel+1` gives `rel-1~c40776d5bc6cb59fe0d432ad5e832c96`, apart from `rel-1`; git allows no `~` in
  a branch name, so no branch is named that, and 128 bits leave no searchable collision);
  `reviewed/` and `required/` use the same suffix.
- A status call that fails is logged; from a review it is also noted on the progress comment, and
  the watcher sets it on a later watch. After a POST of its own fails, the watcher sets none in
  that repository for `poll_interval_seconds` (180 s), so a lasting refusal cannot spend the shared
  App token every watch. A failure never fails or delays a review.
- The base and head it was last set on (`<base>@<head>`) are kept per PR in `state.json` under the
  repository's `reviewed_status` map, apart from the `reviewed` ledger entries a review rewrites.
  Entries of PRs no longer open are dropped.

`review-of-legends/required/<base branch>` is described under *Configure agents*: `pending` while
the attempt runs, then `success`, `failure` (a required agent's verdict) or `error` (not posted),
on each reviewed head.

Recommended rulesets (the operator sets them after the deploy), source "Review of Legends":

- **Every repository:** require `review-of-legends/reviewed/<branch>` for each protected branch,
  for example `review-of-legends/reviewed/develop` for a repository that merges into `develop`
  and `review-of-legends/reviewed/main` elsewhere.
- **Repositories with a required agent** (for example backend servers with `server-architect`
  switched on): also require `review-of-legends/required/<branch>`.
- A PR that was already open when its repository was first watched is in the baseline, unreviewed,
  so it has no reviewed status until a review of it is posted: its next push starts its first
  review, or an admin retries it from the dashboard.

## Configure agents

Edit `config/reviewer.json`:

- Add, remove, or disable entries in `agents`. Seven agents ship on by default (correctness,
  security, maintainability, tests, reliability, red-team, simplify); `server-architect` ships
  with `"opt_in": true`, so it runs nowhere until a repository switches it on. Every agent
  (enabled or not) needs an `id` of 1-64 lowercase letters, digits or `-`, starting with a letter
  or digit; any other id fails at startup, because it could not take a dashboard override.
- Switch agents per repository in `repository_agents`, keyed by `owner/name`:
  `{"acme-corp/api": {"server-architect": true}}` turns the opt-in architect on for
  `acme-corp/api`, and `{"owner/name": {"red-team": false}}` turns a default agent off for one
  repository. The switch lives here, not in the reviewed repository, so a PR author cannot
  toggle reviewers on their own PR. Unknown repository or agent ids, non-boolean values, a
  repository left with fewer than 3 agents, and a `min_successful_agents` above a repository's
  selected count fail at startup (`doctor` prints the per-repository list). A switch on an agent
  that is globally `"enabled": false` is accepted and the agent runs nowhere. The model-policy
  metadata on a PR is judged on the runners that repository actually executes.
- Mark an agent `"required": true` (default `false`) when the review must not pass without it.
  Required covers two failures:
  - **Run**: the agent does not finish — timeout, every model in the ladder exhausted, or invalid
    JSON. The attempt ends like a quorum failure: no review is posted, the attempt is recorded
    failed with stage `required`, and the PR progress comment names the agent and why.
  - **Verdict**: the agent finishes with a P0/P1 finding (or a priority-less high severity). The
    service derives every agent's verdict from its findings: "patch is incorrect" exactly when it
    has a P0/P1, "patch is correct" otherwise, whatever the agent wrote in `overall_correctness`.
    A "patch is incorrect" without a P0/P1 is rewritten to "patch is correct" (and a P0/P1 under
    "patch is correct" to "patch is incorrect"), and the review body's correctness verdicts show
    what the agent wrote, e.g. *patch is correct (no P0/P1 finding; the agent wrote "patch is
    incorrect")*. On a failed verdict the review **is** posted as usual (never as `APPROVE`), with
    a line such as *Required review failed: server-architect reported a P1 finding* on the review
    and the progress comment.

  On a repository where at least one selected agent is required (config or dashboard), the
  service sets the commit status `review-of-legends/required/<base branch>` (e.g.
  `review-of-legends/required/main`) on the PR head: `pending` as soon
  as the attempt starts, `success` when the review is posted and every required agent passed,
  `failure` when a required agent failed its verdict, `error` when the review was not posted
  (a required agent did not finish, the quorum failed, or anything raised — the diff fetch, the
  clone, posting), so a re-run of a head never keeps an earlier result. The base branch is part
  of the context because two open PRs can share a head commit with different bases (different
  diffs, different verdicts) and GitHub keys a status by commit and context. To block merging,
  make the context for that branch a required status check in its branch protection (for
  `main`: `review-of-legends/required/main`). The status links the
  dashboard attempt page when `dashboard.public_url` is set (shipped:
  `https://rol.company.example`), else the PR. A status call that fails is logged and
  noted on the progress comment; it never fails or delays the review. Repositories without a
  required agent get no required status, and a dry run makes none. Every repository also gets
  `review-of-legends/reviewed/<base branch>` (see *Commit statuses and merge rulesets*).
  `server-architect` ships required (switch it on for backend server repositories). `doctor`
  marks required agents in the per-repository list, and the PR progress table labels them
  `(required)`.
- Point `prompt_path` at any Markdown instruction file.
- Override `agent_runner.command` globally, or add a per-agent `runner` object.
- Available placeholders in runner args: `{prompt_file}`, `{work_dir}`, `{run_dir}`,
  `{artifact_dir}`, `{service_root}`, `{agent_id}`, `{repo}`, `{pr_number}`, and `{head_sha}`.

The default runner is `code_review_service/llm_runner.py`, a stdlib-only script that POSTs
the rendered prompt to an Anthropic-Messages-compatible proxy (llmux) and writes the model's
text output to `{response_file}`:

```sh
python3 {service_root}/code_review_service/llm_runner.py \
  --prompt-file {prompt_file} --response-file {response_file} --meta-file {meta_file} \
  --base-url http://localhost:3456 \
  {model_policy_args}   # expands to: --model 'gpt-6-astra[1m]' --effort xhigh --fallback-model grok-4.6
```

### Model policy

Which models review, at what effort, and in what fallback order is one object,
`model_policy` in `config/reviewer.json`:

```json
"model_policy": { "model": "gpt-6-astra[1m]", "effort": "xhigh", "fallback_models": ["grok-4.6"] }
```

The runner walks the ladder in order. A model whose usage window is used up
(llmux answers `429 rate_limit_error` with a long `Retry-After`) is skipped at once,
without the transient-error retry sleep, so the next model starts within seconds.
Transient errors (502 `proxy_error`, 5xx, timeouts) are retried per model first.

The ladder shares one wall-clock budget (`--total-budget`, 2200s shipped, below
`agent_timeout_seconds` 2300s), and while a fallback model is still untried it keeps
`--fallback-reserve` (300s) for it. While a fallback is untried a retry is skipped, its time
left to the fallback, when it would get less than `--min-attempt-seconds` (60s), less time
than the request that just failed ran, or when that request ran into the model's
`--model-ceiling` (a policy: the untried fallback is the better use of the time). llmux's codex route ends every request at about 900s (no codex reply
through llmux has taken longer than 900.3s), so the shipped `--model-ceiling gpt-=900`
marks such a cut as final: a retry would be cut at the same point. The budget fits one cut
primary, a full 900s window for the next model and the reserve (observed in production:
astra was cut twice at 900s, then `gpt-6.1-sol` got two 1s windows; it answers the same
prompt in 791s).

The policy is resolved per attempt with this precedence:

1. the model chosen on the dashboard's retry panel for that one retry (`source: request`),
2. the policy saved on the dashboard **Settings** page (`source: dashboard`, stored in
   `data/dashboard/dashboard.sqlite3`, survives restarts and deploys),
3. `model_policy` in the config (`source: config`), or the legacy `--model` argv if the
   config predates the block (`source: command`).

Claude models can never be selected: this service has no Anthropic account leg through
llmux, so the daemon, the settings API and the retry API all refuse `claude*` ids with
that reason, and the Settings page lists them greyed out. The catalog the page offers
is llmux's own (`GET /llmux/models`), so a newly deployed model appears without a release.

### Review metadata on the pull request

Every `llm_runner` invocation carrying `--meta-file` (the shipped argv does; the Codex
CLI rollback argv does not) writes `<agent>.meta.json` next to its response: the ladder, the
model that answered, the effort, token usage (input/output/cache), each attempt with
its outcome (`quota_exhausted`, `transient_error`, `succeeded`, …), the proxy's response
id and stop reason, and timings. The service publishes that record where the operator
looks:

- the **progress comment** on the PR shows the policy in effect, a Model/effort and a
  Tokens column per agent, running totals, and a collapsible "Model and usage metadata"
  block with every field above (including the fallback route, e.g.
  `gpt-6-astra[1m] ✗ quota (retry-after 3600s) → grok-4.6 ✓`);
- the **posted review** header lists the policy, the models used and the token totals;
- the **dashboard** attempt detail shows the same per agent and per attempt.

The record never contains the API key or the prompt; filesystem paths are scrubbed from
its free-text `error` fields, and what reaches the PR is the bounded projection in
`service.llm_fields_from_meta` (model, effort, outcome, tokens, route), never the free text.

On the dashboard run view, an agent row whose route has a failed hop (or an agent that
failed outright) carries a collapsed **Why** block: for every failed hop the model, attempt,
a plain cause (out of quota, rate-limited burst, transient timeout/5xx, rejected by the
upstream, reply without JSON, …), the HTTP status, the retry-after window, and the response
text the proxy or model sent back (up to 2,000 characters; paths and credential-shaped tokens
redacted), plus the agent's own failure output. When many agents fail at once the text is
shortened, and then older failed hops are counted ("N earlier hops not shown") rather than
shown, so the rows still fit the store; at the last step each agent keeps only its newest
failed hop's model, attempt, outcome and HTTP status, and the row's own metadata (route,
summary, models, tokens) gives way before that hop does.
That text is dashboard-only: it never appears on the PR.

**Runtime dependency:** a running llmux daemon on the service host (`localhost:3456` by
default). llmux owns account selection and token refresh across its Claude/Codex/Grok
account pools, so the service no longer reads or copies any credential file. The runner is
fail-closed: if the proxy is unreachable, every retry is exhausted, or the response has no
text, it exits non-zero and the service records an agent failure (quorum still applies) —
it can never silently fall through to a direct upstream endpoint.

Model fallback runs in order: the primary `--model` first, then each `--fallback-model`
(repeatable), both taken from the model policy above. The shipped ladder is
`gpt-6-astra[1m]` at effort `xhigh`, then `grok-4.6` at the same effort, and the same
`agent_runner` block serves the review agents and the executive summary, so changing the
policy in one place changes it for every agent. Requests carry no vendor client-identity or
billing metadata: the body is the model, the token budget, the prompt, and the effort.

**Rollback:** the previous Codex CLI runner is preserved in
`config/reviewer.json` under `agent_runner_codex_rollback`; copy its `command` back into
`agent_runner.command` to revert (the Codex auth-copy code path is still in the service).

### Visual outline comment

Before any reviewer agent runs, one agent drafts a humanlayer `visual-pr`-style outline of the
change and posts it as a PR timeline comment: **왜 이 변경인가** (one sentence), **다이어그램** (one
Mermaid diagram of the changed behaviour, below), **리뷰어가 알아야 할 것** (1-3 bullets a reviewer
would be hurt by not knowing), and **변경 개요** — the smallest set of "show me" views that explain
the implementation (pseudocode, call/control/data flow, contract or schema changes, a shallow
annotated file tree, a component tree), `diff`-shaped when an existing shape changed. It orients a
reader; it never carries findings or a verdict, which is the reviewers' job.

- **Diagram.** The 다이어그램 section is one ```mermaid block, the way Greptile's summary draws one:
  `%%{init: {'theme': 'neutral'}}%%` then either `flowchart TD` or `sequenceDiagram` — each with its
  own syntax the prompt pins separately, never mixed:
  - `flowchart TD` (the default, unless the change is mainly an interaction over time): 4-12 nodes,
    decisions as `{...}` with `-->|예|` / `-->|아니오|` edges, every node label in double quotes, no
    HTML or backticks in a label, ASCII node ids.
  - `sequenceDiagram` (only for a change that is mainly an interaction between components over
    time): participants declared first (`participant A as "..."`), each step `A->>B: ...` (no
    quotes needed in a message), no `;` and no `#` anywhere in the diagram, ASCII node ids as above
    — never `{...}` decision nodes or `-->|...|` edges, which belong to `flowchart TD` only.

  Either way: only behaviour visible in the diff, one diagram only. A diff with no flow to draw
  (docs, config, a rename) gets `- 없음.` instead. The block passes through the markdown unwrap and
  the comment body untouched.
- **Once per PR.** The comment carrying `visual_marker` is the whole ledger: if any comment on the
  PR already contains that marker, the stage is skipped without spending an agent run. New commits
  therefore do **not** regenerate the outline. A failed attempt posts no comment, so the next review
  of that PR tries again. If listing the PR comments fails, the stage is skipped rather than risking
  a duplicate. The one exception is a **Retrigger** re-run (see *Admin dashboard*): it drafts the
  outline again and edits the service's own outline comment in place, which is how a PR reviewed
  before the diagram existed gets one.
- **Same runner and model policy as the reviewers**, so the primary → fallback ladder above applies
  unchanged, and the outline's model/tokens appear in the progress comment's metadata block and in
  the attempt's LLM record. It runs with `--output-contract markdown` (not JSON: the outline's
  nested code fences do not survive a JSON string), and a wrapping ```markdown fence, if the model
  adds one, is stripped.
- **Never blocks the review.** A timeout, a non-zero runner exit, empty output, or a failed comment
  POST is logged and the review continues; the outline plays no part in quorum or approval.
- **Config** (`config/reviewer.json`): `visual_marker`, and the `visual_outline` block with
  `enabled`, `prompt_path` (default `../agents/visual-outline.md`), optional `runner`, and
  `timeout_seconds` (falls back to `agent_timeout_seconds`). Set `visual_outline.enabled` to `false`
  — or remove the block entirely, which is the default when it is absent — to disable the stage.
- **Artifacts** land next to the agents' own, in the run's `.review-of-legends` artifact directory
  (mode `0600`): `visual-outline.prompt.md`, `.response.md`, `.meta.json`, `.stdout.txt`,
  `.stderr.txt`.

### Agent isolation

PR titles, bodies, and diffs are attacker-controlled. To make prompt-injection less useful:

- The service runs each agent with a tight environment: only
  `agent_runner.env_allowlist` (default: `PATH`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TERM`) is
  inherited. Tokens like `OPENAI_API_KEY`, `GH_TOKEN`, `GITHUB_TOKEN`, and AWS credentials
  are **not** passed through and would have to be allow-listed explicitly.
- `HOME` and `CODEX_HOME` are redirected to private subdirectories of the PR's run dir.
  With the default llmux runner no credential file is copied at all (the proxy holds the
  accounts); when the Codex rollback runner is active, only the operator's
  `~/.codex/auth.json` is copied (not symlinked) into the isolated `CODEX_HOME`. Set
  `agent_runner.isolate_home` to `false` to opt out.
- The default llmux runner makes exactly one kind of side effect: HTTP POSTs to the
  configured `--base-url`. It executes nothing from the PR checkout and reads nothing but
  `{prompt_file}`, so the agent process is read-only toward the repository by construction.
  (The Codex rollback runner instead relies on `--sandbox read-only`,
  `--ignore-user-config`, and `--ignore-rules`.) Repository `./rules/**/*.md` files are
  still used because the service reads them from the clone and injects them into the
  prompt explicitly.
- Review prompts, agent responses, and stdout/stderr captures are written with mode `0600`.

These mitigations narrow the blast radius but are not a full sandbox. For repositories that
accept untrusted PRs, run the service under a dedicated low-privilege macOS account whose
Codex credential is scoped to a separate billing/budget envelope.

### State recovery

`state.json` is the daemon's idempotency ledger. If it is corrupt at startup the daemon
moves it to `state.json.corrupt-<UTC-timestamp>` and exits with an error rather than
silently re-baselining open PRs. To recover, inspect the backup, either restore it from a
known-good copy or delete the active state file to force a fresh baseline cycle.

Each agent must return JSON:

```json
{
  "summary": "short summary",
  "findings": [
    {
      "path": "relative/file",
      "line": 10,
      "severity": "high",
      "priority": 1,
      "title": "short title",
      "body": "actionable review comment",
      "confidence": 0.85
    }
  ],
  "overall_correctness": "patch is correct",
  "overall_explanation": "No blocking correctness issues were found.",
  "overall_confidence_score": 0.8
}
```

The parser also accepts Soma/Codex-style findings with `confidence_score` and
`code_location.absolute_file_path` plus `line_range.start`.

## Disk: what a review leaves behind

Each review clones the PR into `work_dir/runs/<run>/source` (hundreds of MB for a large repo).
That clone is deleted when the review ends — posted, quorum failure, or any exception on any
stage — leaving only the small `.review-of-legends/` artifacts (prompts, responses, meta) for
inspection. While no review is in flight — when the daemon starts, when it resumes from a pause,
and at the start of every `once` — `work_dir/runs` is swept: any
leftover clone (from a crash, a kill, or a deploy mid-review) is removed, and run directories
older than `run_dir_retention_days` (default 14; `0` disables) are deleted whole. While reviews
run, the daemon only prunes: at most once an hour it deletes whole run directories past the
retention age, never strips a clone, and never touches a run directory of a PR in flight. The
same hourly prune deletes, whatever their age, the run directories of a PR that a successful
listing of its repository stopped showing (merged or closed), every head of it: only those whose
`.review-of-legends/repository` names that repository (run directory names carry just the number
and head, which two repositories can share). A run directory from before that file existed, or of
a PR that closed while the daemon was down, waits for its retention age. A clone that fails is removed at once and the log says so. With
`sweep_legacy_work_dir: true` (set in the deployed config) the sweep also covers the pre-service-local default
`~/.local/share/review-of-legends/runs`, which earlier deployments wrote to; it is an explicit migration switch because
another configuration may still own that directory. Set
`cleanup_run_dir` to `false` only for a debugging session; it is the one way to keep a clone, and it also
stops the sweep from stripping clones and the prune from deleting closed PRs' run directories
(retention pruning still applies). A failed `state.json` write removes its temp file. A run dir the sweep cannot
read or delete is logged and skipped; it never stops the rest of the sweep.

## Posting as a GitHub App

By default the daemon talks to GitHub with the host's `gh` login. With a `github_app` block in
`config/reviewer.json`, every GitHub API call it makes (PR reads, the review and its line comments,
the progress, visual-outline and executive-summary comments, the `review-of-legends/reviewed/<base>`
and `review-of-legends/required/<base>` commit statuses) goes out as the App's installation, so it shows up as the bot (for example
`review-of-legends[bot]`) instead of a person's account:

```json
"github_app": {
  "app_id": 123456,
  "installation_id": 7890123,
  "private_key_path": "~/.config/review-of-legends/github-app.pem",
  "bot_login": "review-of-legends[bot]"
}
```

- Repository permissions the App needs: Contents read, Metadata read, Pull requests read and
  write, Issues read and write, Commit statuses read and write. Contents read and write is needed
  only for the bot to resolve its own fixed threads (`resolve_outdated_threads`) and to open the
  contributor-pledge pull request (*Contributor gate*); with Contents read it lists threads for
  people instead and asks a maintainer to add a pledged login. Actions read and write lets the
  contributor gate approve an approved author's waiting fork CI runs (`rol setup` asks for it). Contents write also lets the App's key push to every repository
  the App is installed on, so weigh that before granting it. Install it on every configured
  repository. No webhook: the daemon watches the PR lists and the PR comments (see *Scheduling* and
  *Talking to the bot*).
- Keep the private key (the `.pem` GitHub generates) outside the repository and the deployed
  tree, readable only by the service user (`chmod 600`). `private_key_path` may use `~`; a relative
  path is resolved against the config file's directory. A missing key file stops the service at
  config load with the path (never the key) in the error, and every process that loads the config
  (daemon, dashboard, the deploy's `doctor`) checks it, so put the key on the host before the
  config change deploys.
- `bot_login` is the App's login: its slug followed by `[bot]`. An installation token cannot ask
  GitHub who it is, so this value is how the service finds its own progress and summary comments.
- The daemon signs a short-lived JWT with `openssl` (the macOS system one is enough) and exchanges
  it for an installation token, which lives one hour; the token is cached and replaced five minutes
  before it expires. Tokens and JWTs are never logged or put in errors.
- Cloning the PR source still uses the host's `gh` login, so that account keeps needing read access.
  The dashboard's retry check, which only reads the PR, also stays on the `gh` login.
- `code-review-service doctor` prints `github app: id …, installation …, posts as …` and mints a
  token (`github app token: ok`, or `FAILED (reason)` with a non-zero exit, which fails deploy
  verification).
- Comments made earlier by the previous identity are not edited or removed. On an open PR the next
  review posts a new progress and summary comment as the bot and updates those from then on; the
  visual outline is once per PR, so a PR that already has one keeps the old one (a Retrigger re-run
  posts a fresh outline as the bot, since the old identity's comment is not the bot's to edit).

## Talking to the bot

PR authors can set their own options and send feedback by writing `@review-of-legends` in a pull
request comment, either a timeline comment or a review (line) comment.

**Why GitHub does not notify the bot.** No GitHub user is called `review-of-legends` (the App posts
as `review-of-legends[bot]`), so the mention links to nobody, GitHub does not offer it in
autocomplete, and GitHub does not deliver mentions to an App's bot. The App also subscribes to no
webhook events. So the daemon reads the comments itself: after the PR listings (every
`watch_interval_seconds`, 20 s) it asks GitHub for each review repository's PR comments updated
since its cursor (`GET /repos/{repo}/issues/comments` and `/pulls/comments`, `sort=updated`,
oldest first, conditional, so a quiet repository costs two `304`s). `since` is inclusive, so the
listing follows the next page while a page holds only the cursor's own second: any number of
comments updated in one second is read through. Such a listing is read twice and used only when
the second read starts with every comment of the first, in order (offset pages shift if one is
edited in between). Typing the mention is enough.
In normal operation (no backlog of commands, no rate limiting, the dashboard store available) the
answer comes within a watch; otherwise the command waits, in order, for a later one (see *Limits*).

**Commands** (a comment that starts with the mention; the command is the rest of that first line;
case does not matter, `[bot]` may follow the name):

| Command | What it does |
|---|---|
| `@review-of-legends lang <code>` | Reviews of **your** pull requests are written in that language. A code is 2-3 letters, optionally `-` and a 2-8 character region or script (`ko`, `en`, `ja`, `zh`, `zh-tw`, `pt-br`); `korean`/`한국어`/`kr`, `english`/`영어`, `japanese`/`일본어` and `chinese`/`중국어` work too. Stored lowercase, per GitHub login. |
| `@review-of-legends lang off` | Clears it (`lang default` and `lang reset` too); reviews go back to the service default. |
| `@review-of-legends lang` | Replies with your current language. An invalid code replies with the accepted forms and changes nothing. |
| `@review-of-legends settings` | Replies with your settings and this list (`help`, `config`, or the mention alone, too). |
| `@review-of-legends review` | A fresh review of this PR's current commit, **PR author only, once per head**. `rereview` and `retrigger` are aliases; the first word is case-insensitive. |
| `@review-of-legends verify <code>` | Contributor gate: binds your GitHub login to the email you signed in to the dashboard with (the code is on its **Contributor** page, `#contributor`). See *Contributor gate*. |
| `@review-of-legends I have read the Contributor Pledge and I agree to it.` | Contributor gate: signs `.github/CONTRIBUTOR_PLEDGE.md` (PR author only); the bot opens the pull request that adds you to `.github/APPROVED_CONTRIBUTORS`. See *Contributor gate*. |
| `@review-of-legends <anything else>` | Feedback to the maintainers: the text from the mention to the end of the comment (up to 4,000 characters), with the repository, PR and comment link. |

- **Author rereviews.** The bot reads the live PR and compares the commenter's GitHub login
  with its author's, case-insensitively. Closed, merged or draft PRs, queued or running reviews,
  heads without a completed review, and unknown review-posting outcomes are refused with a reason.
  An accepted comment queues all reviewers for the live head, attributed as `github:<login>`
  in the dashboard. Each head permits one comment-requested rereview, even if that request later
  fails; push a commit or ask a maintainer after using it. Maintainers with dashboard admin access
  can use the **Retrigger** badge on the review without this per-head limit. The comment's
  `updated_at` (or `created_at`) is saved so execution refuses if another review was recorded
  after the author asked. A review of the head recorded as completed at or after the comment —
  in the dashboard or the review ledger — already answers it: the reply says so, and nothing is
  queued or counted against the head's one request.
  A failed GitHub read leaves the comment unread for the next watch.
- **What counts.** A comment whose first non-blank line starts with the mention (indented at most
  3 spaces), on a pull request (not a plain issue), by a person (never the bot itself or any bot
  account): one command per comment. CommonMark makes that first line plain text whatever follows,
  so no Markdown is parsed: a mention in code, a quote, a list or the middle of a comment is never
  a command, and quoting the welcome comment or pasting an example never triggers anything. A
  comment anyone but its author ever edited is never a command: GitHub keeps a comment's author
  when a collaborator edits it, so before answering, the daemon reads the comment from GraphQL
  (body, author, `updatedAt` and `userContentEdits`, one query per command). If that copy is not
  the comment as listed, the command waits for the next listing; if another account ever edited
  it, it is passed over; the author's own edits are fine. A command is answered once, when first read; editing
  it later does nothing. Comments written before the daemon first watched a repository are not
  answered: the cursor starts at GitHub's clock (the `Date` of the PR listing; a start whose save
  failed keeps that time). One exception: without that clock the cursor starts 120 s
  before the daemon's (so a daemon clock that runs ahead takes no new comment for history), and a
  command written in those 120 s before the first watch is answered, once.
- **Acknowledgement.** Every command gets a 👍 reaction and a short reply: a new PR comment that
  @-mentions the commenter and quotes the command, or, for a review comment, a reply in its
  thread (under the thread's first comment: GitHub takes no reply to a reply). Replies are in
  Korean or English: the commenter's language when it is `ko` or `en`, English for any other
  language, and `mentions.default_reply_language` (default `ko`) when none is set. A reaction or
  reply that fails is logged and not retried.
- **Order, and what a failure costs.** A command is recorded in the store, then the cursor is
  saved in `state.json`, then the reaction and the reply go out. When the cursor cannot be saved
  nothing is posted and the comment is applied again on the next watch, which changes nothing
  twice (a language is set to the same value; feedback is one row per comment; a rereview returns
  the request already queued for that comment). A daemon that
  stops between the save and the reply loses that reaction and reply: an acknowledgement is sent
  at most once, never twice.
- **One order.** Both kinds of comment are answered in one order, oldest `updated_at` first, so
  a `lang` in a review thread and a newer one in the timeline end with the newer. A repository's
  commands wait while either of its listings fails. Across repositories the store keeps the
  newest: a language command older than the one the setting came from (its comment's
  `updated_at`; within one second, the comment's identity decides, so a replay never undoes a later
  command) changes nothing, and its reply names the language that stands.
- **Limits.** The cursor of a repository seen for the first time starts at GitHub's clock as
  of that watch's PR listing (kept while the executor lock is busy or the save fails). A command
  costs two content POSTs (the reaction and the reply), like a reviewed status. A `review`
  command also costs one live PR GET, tracked by the client's read limit rather than
  the content-POST budget. Per watch,
  statuses get 18 of the 20 POSTs, and commands what is left, at most 10 and always at least one; the rest wait for the next watch, in order. The repositories take turns
  going first, one turn per mention phase run. A watch whose PR listing was refused for the rate limit reads no comments; a
  reviewed-status POST refused for it also ends the watch, slows the next ones, and holds every
  command until `poll_interval_seconds` have passed. A
  rate-limit refusal (`429`, or a `403` that says so) of a comment listing, a live PR read, a reaction or a reply
  ends that watch's mention work and slows the next watches, as for the PR listing; a command
  whose acknowledgement was refused stays answered (it is not applied again). While the dashboard
  store is unavailable no comment is read and the cursors stay where they are, so nothing is
  lost; a command the store refuses to record is read again on the next watch, and nothing is
  posted for it until it is.
- **Where it is kept.** The cursors live in `state.json` per repository and comment kind: the
  newest `updated_at` read, the ids read in that very second with a hash of what each said then
  (`since` is inclusive, and an edit within that second keeps its `updated_at`), and the id of
  every command answered or passed over, for good (commands are rare; the comments read for
  nothing are not kept). The settings and feedback live in the dashboard store (`pr_authors`, `feedback`, one row per
  comment; the newest 2,000 feedback rows are kept).
  Author rereviews have a durable `author_review_requests` receipt keyed by the comment's
  `source_key`, with a unique repository/PR/head constraint. The receipt and retry queue row
  are inserted in the same transaction: concurrent requests cannot spend the allowance twice,
  and replay after a cursor-save failure or restart returns the original request. Receipts
  survive completion, and badge requests do not consume them.

**The language effect.** At the start of each review the PR author's language is read from the
store. When one is set, the visual outline, every reviewer agent and the executive summary are
asked to write every human-readable sentence (summaries, finding titles and bodies, verdicts,
outline text) in it, keeping code, identifiers, paths, commands, JSON keys and the format's fixed
values (`patch is correct` / `patch is incorrect`, severity, priority, scores) unchanged; the
posted review header gains `Language: <code> (set by @<login>)`. Headings the service itself
writes stay as they are. A store that cannot be read means the default language (logged), never
a skipped review.

**The welcome comment.** The first time the service reviews a pull request (first review or an
update) by an author who has not been welcomed, it posts one comment (marker
`<!-- review-of-legends-welcome -->`) explaining what the bot does, the `reviewed` and `required`
checks, and every command above, in the author's language when it is `ko` or `en`, otherwise
the default reply language. It is recorded only after the post succeeds, so a failed post is
tried again on the author's next reviewed pull request; a welcome the service already posted on
that pull request (one with the marker, written by the service's own account; a pasted copy
does not count) is recorded instead of posted again. One that went up but could not be recorded
is recorded on the author's next reviewed pull request, not posted again, and one whose POST
answer was lost (a timeout) is looked for on its pull request first (10 minutes after the attempt,
so GitHub has settled) and posted again only if it is not there (both until the daemon restarts; while 200 wait to be recorded, no new welcome is
posted). Bots get none, and nothing is posted while
the store cannot be read (it could not be recorded). The feature is new to everyone, so
**existing authors get it too, once, on their next reviewed pull request.**

**Where feedback shows up.** The dashboard's Settings page lists, read-only for every signed-in
account, the PR authors with a language or a welcome (up to 20 pages of 200; a note says when
there are more) and the feedback, newest first, each linked to its comment (`GET /api/authors`,
`GET /api/feedback`). Feedback is paged by row id, not by offset: `before_id=N` returns the rows
below `N`, newest first, and `after_id=N` the rows right above it, oldest first, each up to
`limit` (default 50, at most 200) with `has_more` for that direction. Ids only grow, so feedback
that arrives while pages load never shifts a page: "Show older feedback" asks below the oldest
row shown, and a live refresh (an event, a resync, Reload) asks above the newest one and follows
`has_more`, so new feedback joins the list without dropping, skipping or repeating a row. A
refresh that cannot catch up within 10 pages starts again from the newest page and says so on
the page; "Show older feedback" reads back from there. Nothing there changes
them: authors change their own settings by commenting on GitHub. The event stream announces a
change with the login and row id only, never the text.

**Config** (`config/reviewer.json`): `mentions.default_reply_language`, `"ko"` (default) or
`"en"`: the language of replies and of the welcome comment for someone who has not set one.
Any other value fails at config load.

## Contributor gate

On a public repository anyone can open a pull request. With `contributor_gate.enabled`, the daemon reviews a pull request, and approves its waiting fork CI runs, only when its author is an **approved contributor**. The list lives in the repository itself, in the format [herdr](https://github.com/herdrdev/herdr) uses.

```json
"contributor_gate": {
  "enabled": true,
  "trusted_email_domains": ["example.com"],
  "trusted_bots": ["dependabot[bot]", "renovate[bot]", "github-actions[bot]"],
  "approved_logins": [],
  "approve_fork_ci": true,
  "open_pledge_pull_request": true
}
```

Add `"repositories": ["owner/name"]` to gate only those repositories. Without it, the gate covers every review repository; an empty list gates none, and `doctor` says so.

**Who is approved.** The author of a pull request is approved when any of these holds:

- They have write or admin permission on the repository. This is GitHub's base permission, so maintain and a custom role built on write count too.
- Their login is listed in `.github/APPROVED_CONTRIBUTORS` on the default branch. The format is one login per line; `#` starts a comment.
- They proved an email at a trusted domain. A trusted domain is either a `*@example.com` line in that file or an entry in `trusted_email_domains`.
- They are one of `trusted_bots`, or one of `approved_logins`. `approved_logins` approves a login in every gated repository, for private repositories that carry no list file. Put the bot accounts of your own GitHub Apps that open pull requests in `trusted_bots` (`name[bot]`); a bot is never a collaborator, so without a line it is gated.

Unlike herdr, there is no `.github/MAINTAINERS`: write access already approves.

**What an unapproved author sees.**

- The pull request is not reviewed. Its fork CI stays waiting for approval.
- It gets one comment (marker `review-of-legends-contributor-gate`) that lists the ways in.
- The daemon checks the pull request again on every watch. Reading the list costs a conditional request (a `304` when nothing changed).
- When the author becomes approved, the comment says so and the review starts. At most three gate comments are written per watch; one that has to wait is written on a later watch, also after the head was reviewed.
- `@review-of-legends review` from an unapproved author is refused with the same pointer.

**Domain verification.** A commit email is never evidence: anyone can put any address in a commit.

1. The contributor signs in to the dashboard with Google. The sign-in rules are those of *Admin dashboard*: a verified email, and a Workspace `hd` claim that matches a viewer domain.
2. On the **Contributor** page they enter their GitHub login and get a one-time code **for that login**. A code lives 1 hour and is stored hashed. It works only in a comment by that login, so a code read off a public comment is worthless to anyone else.
3. They comment `@review-of-legends verify <code>` on their pull request.
4. The daemon binds the account to the signed-in email, and replies. The binding is to the login and to GitHub's immutable user id, so a renamed or reassigned login does not inherit it.

From then on, any pull request by that login passes the gate when the email's domain is trusted. Removing the domain line removes the trust; the binding alone approves nothing.

**Contributor pledge.**

1. The repository carries `.github/CONTRIBUTOR_PLEDGE.md`.
2. The author comments `@review-of-legends I have read the Contributor Pledge and I agree to it.` on their pull request. Case, extra spaces and the final period do not matter.
3. The daemon records the signature in the history store: login, user id, comment link, time, and the pledge file's blob SHA.
4. It opens a pull request that adds the login to `APPROVED_CONTRIBUTORS`, with the record in its body. Its one commit is built on the default branch as it is at that moment.
5. Merging that pull request is the maintainer's acceptance. Closing it declines.

When two contributors sign before either pull request is merged, both add a line at the end of the list, so merging one puts the other in conflict. Signing again (the same comment, once more) rebuilds the open pull request on the current default branch. A branch left from a declined pull request is rebuilt the same way on the next signature.

Opening the pull request needs Contents write. Without it, the reply asks a maintainer to add the line, and the signature is still recorded.

**Fork CI.** Set the repository's Actions setting *Approval for running fork pull request workflows from contributors* to **Require approval for all external contributors**. For an approved author, the daemon then approves the pull request's waiting runs (`POST /repos/{repo}/actions/runs/{id}/approve`): only runs of its head commit from its own fork and branch. Another pull request at the same commit is not covered. It tries each head on every watch for 30 minutes, counted from when it first sees the author approved, independently of the review: workflows start at different times, and a pledge accepted hours later still gets its CI approved. While approval backs off after a refusal, that time does not count, and a watch whose Actions call fails (an outage) starts the 30 minutes again. A run deleted between listing and approval is skipped. Pull requests that share a fork, branch and commit (into different base branches, say) are approved together, only when every one of their authors is approved: GitHub cannot tell their runs apart. Drafts count among them: a draft gets no CI approval of its own, but an unapproved author's draft on the same fork, branch and commit keeps the shared runs waiting. This needs the App's **Actions: read and write** permission. Without it the runs wait for a person, and `doctor` says so.

## Commands

```sh
./bin/code-review-service doctor
./bin/code-review-service once --dry-run
./bin/code-review-service once --repo 2lab-ai/review-of-legends --dry-run
./bin/code-review-service once --pr 123 --force
./service status
./service logs all 100
./service logs follow
./service dashboard verify
./install.sh
./uninstall.sh
```

## Admin dashboard

The admin dashboard (Google-authenticated run queue, retry dispatch, live event stream, llmux
usage) is part of this repository and part of every deployment.

**Roles.** The address in `ROL_DASHBOARD_OWNER` (see *Deployment settings*) is the owner and always admin: retries, the model policy, review agent
prompts and required flags, and per-account llmux usage. The owner can grant the same admin rights to
other addresses on the Settings page (see *Admins granted on the dashboard* below). Any other Google
account whose verified email
domain **and** Workspace `hd` claim are one of `ROL_DASHBOARD_VIEWER_DOMAINS` signs in as a **viewer**: the queue,
attempt detail, the live stream, the policy in effect and the review agents' prompts are readable; every mutation and `/api/usage` answer 403. Every other account
is refused at sign-in. It runs as a **second launchd
job** (label `ROL_DASHBOARD_LABEL`, default `ai.2lab.review-of-legends.dashboard`), because the web tier needs
Starlette/Uvicorn/Authlib while the review daemon stays stdlib-only on `/usr/bin/python3`:

- Runtime: a per-version virtualenv at `$HOME/review-of-legends/venvs/<VERSION>`, built
  from the newest Homebrew Python >= 3.10 found on the host and `requirements-dashboard.txt`
  (pinned; the workflow refuses unpinned lines). A re-run of an already tagged commit deploys the
  same version and so reuses its venv. The venv lives **outside** the deployed tree
  because `rsync --delete` would otherwise wipe it mid-deploy. Old version venvs are rollback
  runtimes: once the new job is confirmed running, the installer keeps the venv it just
  installed, the one the job ran before this deploy (its plist, the program launchd had loaded, and
  the venv the last confirmed-running install recorded in `venvs/.last-running`), and the newest 5
  others; when neither launchd nor that record says what ran before, it removes none that deploy
  (`VENV_KEEP_PREVIOUS`), and removes the rest (without this they pile up, one per deploy). A failed or unconfirmed deploy removes none; a venv that cannot be removed is
  logged and never fails the deploy; only directories holding a `pyvenv.cfg` are candidates (symlinks,
  files and other directories under the venv root are never touched), and a reused venv counts as recent.
- Bind: `127.0.0.1:8840` only; public access is `ROL_DASHBOARD_PUBLIC_URL` through
  a reverse proxy you run. Nothing binds a public interface.
- Settings: `$HOME/review-of-legends/dashboard-secrets.json`, mode `0600`, **outside** the rsynced
  tree and never committed. The installer only writes the non-secret `public_url` (always the
  canonical origin, which the web tier requires) and `config_path`, plus a one-time random
  `session_secret` that signs this deployment's own cookies. Google and llmux credentials are
  operator-provisioned; the installer never invents, prints, or rewrites them.
- Until a Google client and llmux admin key are provisioned the web app cannot boot, so the
  deploy fails with the missing **non-secret setting names** (secret names are withheld, values
  never appear). To ship the dashboard before that provisioning, run
  `./service.sh install --allow-unconfigured` or set `ROL_DASHBOARD_ALLOW_UNCONFIGURED=1` — both
  log the choice on every run.

The dashboard's **Settings** page also shows, read-only, PR authors' own settings and their
feedback (see *Talking to the bot*). It edits the model policy (default model, effort, ordered
fallbacks) from the llmux catalog; the attempt page's retry panel can pin a model for a
single retry. Both go through `/api/settings/model-policy` and `/api/runs/{id}/retry`
with the same validation the daemon applies (see *Model policy* above).

**Retry** (admin; the attempt page, `POST /api/runs/{id}/retry` with body `{"head_sha", "model_policy"?,
"scope"?}`) queues a new attempt of a failed, interrupted or stale one on the same head:

- An attempt that ended because too few agents succeeded or a required agent failed keeps the
  results of the agents that succeeded (in the dashboard store: at most 32 results, 32,000
  characters each and 128,000 in total; a result over that is not kept and its agent runs again).
  Its panel offers **Retry failed agents (N)**, N being the agents without a kept result, and
  **Retry all agents**, with a line naming the agents whose results are reused.
- A reused result is used as it is: its findings, verdict, summary and the models it ran on count
  toward the quorum, the required check, the executive summary and the posted review exactly like
  a fresh one. The new attempt's agent rows and the PR progress comment mark those agents
  "reused from the previous attempt", and their tokens stay with the attempt that spent them: the
  progress comment and the posted review count only the tokens of the agents that ran. A model
  pinned for the retry applies to the agents that run.
  A result is kept per agent id, head, base, diff and review language: a prompt or model changed on
  the Agents page since the failed attempt does not invalidate it. To apply such a change to every agent, use **Retry
  all agents**.
- An attempt that kept nothing (interrupted, stale, failed before its agents finished) offers the
  whole review only.
- `scope` is `failed` or `all`. It defaults to `failed` when the attempt kept results and to `all`
  otherwise; `failed` on an attempt that kept nothing answers 409. A request already queued for
  the head is reused with the model and scope it was created with.
- The daemon reuses a kept result only on the head, the base commit and the diff it was reviewed
  against, and only in the review language it was written in: the PR's `base.sha` (repository rules
  load from it), a fingerprint of the PR files the agents read, and the PR author's language (see
  *Talking to the bot*; none set is a language of its own). A PR moved to another base, or whose base
  branch took in some of its commits, keeps its head but gets a different diff, so every agent runs
  again; new base commits that leave the PR's diff unchanged do not cost the reuse. An author who
  sets or clears a language with `@review-of-legends lang` after the failed attempt gets every
  agent on the next retry, so no reused result is in the old language; a result kept before the
  language was recorded counts as written with none set. It reuses only for an agent the repository still
  runs; every other agent runs, and the log names the reused agents or says why none was reused. Only the agents' ids reach the browser (`reusable_agents`); the results stay in
  the store.

**Retrigger** (admin; the badge on the posted review and on the executive summary, the way
Greptile's summary carries one). With `dashboard.public_url` set, both comment headings carry a
right-aligned **Retrigger** badge linking `{public_url}/#retrigger/{owner}/{repo}/{pr}` — keyed by
repository and PR number, never by an attempt id, so the link on an old comment still opens the
page for the PR's current head. Without `public_url` there is no badge, as there is no status link.
The page (`GET /api/retrigger/{owner}/{repo}/{pr}`, any signed-in account) shows the PR's current
head, the latest attempt of that head, and whether a review of the PR is already queued or
running; signing in from the badge lands back on it. The page's GitHub read of the PR is reused
for 15 seconds (concurrent page loads share one read, and a failed read is remembered as long), the
page's GitHub reads are capped at 30 a minute and 4 at once across all PRs (answering 429 beyond the
minute cap), and the
page only refreshes while it is open, so watching it does not spend the
GitHub quota once per review event; the POST always reads GitHub live. Only dashboard admins get the **Re-run
review** button (`POST /api/retrigger` with body `{"repo", "pr", "head_sha", "latest_run_id", "observed_at"}`: the
head and the latest attempt of that head the page showed, `null` when it showed none, and the GET's
`observed_at`, when the server read that snapshot — an ISO 8601 time with an offset, a later one counting as
now); everyone else reads the same card with "Only dashboard admins can re-run a review."

- A re-run is a fresh review of the current head with **every** agent (nothing is reused): it posts
  a new review, a new executive summary and the commit statuses again, even though the head may
  already carry a review — a second review on the same head is intended. The attempt's trigger is
  `retrigger`. The visual outline is drafted again and the bot's existing outline comment is edited
  in place (a PR with no outline gets one).
- It is refused (409) when the repository is not configured for review, the pull request is not
  open or is a draft, the current head differs from the one the page showed (reload the page), an attempt of the head
  newer than the one the page showed exists (a review ran meanwhile; reload the page), a
  review of the pull request is already queued or running (any head), or the latest attempt of
  the head stopped where the review POST may have landed (reconcile first, as for a retry). GitHub
  errors answer 502 without upstream text. The daemon re-checks the same things before it runs and
  additionally refuses a head whose ledger entry records an unverified POST, and a head that an
  attempt newer than the one the page showed already reviewed while the request waited (a pool
  review that took the PR first), or that the ledger records as reviewed after the page's snapshot
  (`observed_at`, compared to the microsecond with the ledger's `reviewed_at_exact`, an equal time
  counting as after; an older entry with only whole-second `reviewed_at` refuses within the snapshot's
  second; this is the same pool review when its attempt row could not be written): one click never posts
  two reviews. A refusal at that point ends
  the request with its reason as the error, which the page shows.
- The request is a retry-queue request with `kind: rereview` and `scope: all` (`GET /api/retries`
  and the event stream show the kind), so the queue view and the attempt lineage follow it like a
  retry; one request per repo/PR/head is active at a time.
- **Config:** `dashboard.retrigger_badge` = `{"light": url, "dark": url}` replaces the badge images
  (GitHub proxies comment images, so they must be reachable from the internet; the defaults are
  public shields.io badges). Each given URL must be `https://` or the config fails to load, like
  `public_url`.

The **Agents** page (between Accounts and Settings) lists every enabled **review agent**: its
focus, the repositories it runs on, whether it is required, its model, and its prompt. Any
signed-in account can read them; only an admin can edit a prompt (at most 48,000 characters),
switch *required* on or off, pick a model for that one agent, or reset the agent to its prompt
file, config value and the default model (`GET /api/settings/agents`, `PUT`/`DELETE
/api/settings/agents/{id}`, body fields `prompt`, `required`, `model`; `null` clears a field).
An agent's model replaces only the first rung of the ladder: the model policy's model and its
fallbacks follow it as fallbacks, so an agent whose model fails still reviews on the models
everyone else runs. A retry that pins a model for the attempt wins over every agent's model;
the visual outline and the executive summary always run the model policy. An override is stored in `data/dashboard/dashboard.sqlite3` under
the key `agent:<id>` — never truncated: an oversized value is refused — and is read once at the
start of each review attempt, so attempts already running keep what they started with. A
malformed override is logged and ignored (the config and the prompt file apply). The event
stream announces a change without the prompt text. The fields the server manages (which prompt
file an edit was written against, who saved it and when, and a replaced edit) live beside it under
`agent_meta:<id>`, so `agent:<id>` keeps the shape the previous release reads: rolling back to it
keeps every prompt, required and model override. The sidecar records which overrides it belongs
to; once the previous release saves or resets `agent:<id>`, it no longer applies and is ignored, so
a replaced edit kept at that point is dropped rather than shown against the wrong prompt.

A saved prompt also records which prompt file it was written against (`prompt_base_sha`, set by
the server, never by the PUT body). When that file changes — a deploy ships a new default — the
edit is replaced by the new default **once**: the daemon does it at the start of the next attempt
and the dashboard does it when the Agents page loads, both through the same store transaction,
so the file runs and the edit moves aside as `prompt_superseded` (an edit that already equals the
new file is simply dropped). The card then shows a **[!]**, with an accessible label. The Agents
nav entry shows one too, once the Agents page has loaded its data in this tab (until then the
browser has not read the agents). Clicking it (or the prompt area) opens a compare,
built lazily so the diff is not computed at page render: the new default on the left (the editable
textarea, in use) and the replaced edit, verbatim, on the right, with its lines not in the new
default marked (a marked line really is missing from the new default: a moved line is not marked,
and a line the edit has twice and the default once is marked once; copying the right pane yields
the replaced edit exactly — a line only in the new default is never shown there). Three ways out,
bound to the exact replaced edit on screen by its sha (`prompt_superseded.sha`; a refresh that
brings a different one disarms a pending confirmation): **Discard B** (confirmed
inline; `DELETE /api/settings/agents/{id}/superseded?sha=<sha of B>`) — this is how to keep the new
default as it is; **Rollback to B** (`POST /api/settings/agents/{id}/superseded/restore?sha=<sha of
B>`, the edit runs again until the file changes once more); or editing the left pane and saving,
which only lands when the text differs from the new default (saving it unchanged is Discard B's
job, not a third way to say the same thing). Discard B and Rollback to B need the `sha` (400
without it), answer 404 when there is no replaced edit and 409 when the server's replaced edit is
another one. Then nothing changes: the card shows the error and reads the agent again, so the edit
the server holds now appears. Both keep unsaved edits in the card: Discard B changes no field and
Rollback to B only the prompt. While a replaced edit is kept, **Reset to defaults** deletes it too
(with every other dashboard setting of the agent), so it asks first, inline, and sends
`?superseded_sha=<sha of B>`; without the matching sha the server answers 409 and deletes nothing.
With no replaced edit, Reset acts at once and the parameter is ignored. Only the last direct edit
is kept. A prompt saved before this tracking existed has no base: it is adopted against the
current file on first sight, never replaced by the deploy that brings the feature.

Each agent row also carries `default_sha`: the sha256 of its prompt file now (surrounding whitespace
ignored; `null` when the file is unreadable), or, for a dashboard prompt, of the file that prompt was
written against (so a page read whose reconcile could not run, say on a busy store, never presents
the edit as one of a newer file). An edit remembers the `default_sha` it started from,
and a Save that sends a prompt sends it back as `expected_default_sha`. The server compares it
inside the save's write transaction: if the file changed since, or cannot be read right now, it
answers 409 and saves nothing; the card keeps the edit and shows the error. The check is opt-in: a
client that sends no `expected_default_sha` (a script, say) saves against whatever file is current.
A save, a Rollback and a page read all take the prompt file from the config as it is at their
write, so a deploy that moves `prompt_path` while a request waits is seen too. A review attempt
reconciles only while the config on disk still names the prompt file the attempt loaded; after
such a move it runs the dashboard prompt and leaves the reconcile to the next attempt (logged).
When a review attempt cannot read the prompt file of an agent that has a dashboard prompt, the
daemon logs `WARNING agent <id>: the prompt file cannot be read, ...` and runs the dashboard
prompt; nothing is replaced until the file can be read again. If a page
read finds that the default changed under an unsaved prompt edit, the editor shows the new default
and the unsaved text is kept below it, read-only, with **Put back in editor** (the text then
applies to the new default) and **Drop**. If that editor had focus, the read releases it, so the
set-aside text and both buttons show at once and the next keystroke cannot land in the new default.

#### Repositories added on the dashboard

The same page lists every repository the service reviews — the ones in `config/reviewer.json` and
the ones added on the dashboard — with the agents that review each (`GET
/api/settings/repositories`, any signed-in account). Only an admin can add or remove one:

- **Add** (`POST /api/settings/repositories`, body `{"repo": "owner/name"}`). The name must be
  `owner/name` (owner: letters, digits, `-`, at most 39; name: letters, digits, `.`, `-`, `_`, at
  most 100; `.` and `..` are refused), its owner must already own a config repository (today
  `acme-corp`), it must not already be on the list (compared case-insensitively), at most
  50 repositories can be added this way, and the agents it would get must pass the same check as a
  config repository (at least 3, and not fewer than an explicit `min_successful_agents`). The
  dashboard then runs `gh api repos/<owner>/<name> --jq .full_name` with the config's `gh_bin` —
  the login the daemon clones with, and polls with unless a GitHub App is configured — and refuses
  the repository (400, with gh's reason, e.g. `HTTP 404`) when that account cannot see it; a
  missing `gh` or no answer within 20 seconds is a 503 and nothing is stored. With a GitHub App,
  also install the App on the repository: the add check does not test the App. The name is
  stored as GitHub spells it.
- **Baseline.** The daemon re-reads the list on every watch. A repository's first watch records every
  pull request already open as seen without reviewing it (unless
  `review_existing_open_prs_on_first_run` is on); their next push is reviewed, as is every pull
  request opened later. Removing a repository keeps its entry in `state.json`, so
  adding it back resumes from that ledger instead of taking a new baseline: pull requests pushed
  while it was removed are reviewed on the next watch. The entry is matched case-insensitively,
  so a repository added back under another spelling (GitHub changed its case) resumes too; one
  renamed to a different name on GitHub is a new repository and takes a new baseline. `once
  --repo` matches case-insensitively as well.
- **Remove** (`DELETE /api/settings/repositories/{owner}/{name}`) takes a dashboard-added
  repository off the list from the next watch; a review already running finishes, and a retry
  queued for it afterwards is refused as not configured. A repository from `config/reviewer.json`
  answers 409: remove it there, through git.
- An added repository gets the default agent set: opt-in agents stay off and
  `repository_agents` switches cannot name it (they are config-only, validated against
  `config.repositories`). To switch agents for it, move it into `config/reviewer.json`; the config
  entry then wins and the dashboard entry is ignored. If the config later changes so that an added
  repository would get too few agents, the daemon skips it (logged) and the Settings page marks it
  "Not reviewed" with the reason until the config is fixed or the repository is removed.
- The list is stored in `data/dashboard/dashboard.sqlite3` under the key `repositories`; an add or
  remove writes back every stored entry it did not name, as stored. When the daemon cannot read
  the store, or the saved list is unreadable, it reviews the config repositories only (logged) and
  the Settings page says so. An add or remove then changes nothing: an unreadable saved list is a
  409 (repair or delete the row first), a store that cannot be read a 503. `doctor` and
  `once --dry-run` read the config only (a dry run opens no store), so `once --dry-run --repo` with
  a dashboard-added repository answers "unknown configured repository"; `once --repo` without
  `--dry-run` accepts it.

#### Admins granted on the dashboard

The Settings page also lists who is admin: the owner first ("Owner — always admin"), then every
address the owner added, with who added it and when (`GET /api/settings/admins`, any signed-in
account; `can_edit` is true only for the owner).

- **Owner-only edits.** Only the owner (`ROL_DASHBOARD_OWNER`) can add (`POST /api/settings/admins`, body
  `{"email": "name@example.com"}`) or remove (`DELETE /api/settings/admins/{email}`) an address;
  an added admin has every other admin right but gets 403 here, so a grant cannot be passed on or
  kept alive after the owner revokes it. Both need the CSRF token and the dashboard origin. The
  address is stripped and lowercased and must be at most 254 characters with exactly one `@`, text
  on both sides, no whitespace or invisible characters, and ASCII only (400 otherwise: a Cyrillic
  letter would look identical to a Latin one, and lowercasing folds the Kelvin sign into `k`); the
  owner's own address and an address already on the list are refused (400), at most 50 addresses
  can be added, and removing the owner answers 409.
- **Exact match only, and only where Google vouches for the holder.** A grant is one address,
  matched byte for byte against the email Google verified (`email_verified` must be `true`), never
  a domain or a prefix. It counts only for a `@gmail.com` address or a Google Workspace account
  (the ID token carries a nonempty `hd`): for any other address Google is not authoritative for who
  holds it now, and `email_verified` can stay `true` after the mailbox changes hands, so that
  sign-in is refused (403, "an admin grant needs a Gmail address or a Google Workspace account")
  even when the address is listed. The account does not need to be on a viewer Workspace
  domain. Any account other than the owner whose Google email is not ASCII is refused at sign-in.
- **Effect on the next request.** Every request re-derives the role from the current list, so a grant
  applies on the person's next request: a viewer who is already signed in becomes admin without
  signing in again (the page picks it up from the event stream).
- **Removal signs the person out.** Removing an address deletes the sessions that signed in through
  its grant at once, so adding the address back later revives none of them; each one gets 401 on its
  next request (including an open event stream). Such a session is not downgraded to viewer, because
  its sign-in never proved viewer eligibility. Signing in again then gives whatever the address is
  still entitled to (viewer for an account on a viewer Workspace domain, nothing otherwise). A viewer
  who was granted admin and then removed keeps its viewer session and simply returns to viewer. A
  sign-in under that grant that is still in progress when the address is removed is finished first
  and then deleted with the others. The sessions are deleted before the list is written: if that
  fails, nothing is removed (500) and the removal can be tried again. Adding an address also first
  deletes any admin session it was left with (after a hand edit of the store, say), so a grant never
  revives an old one.
- The list is stored in `data/dashboard/dashboard.sqlite3` under the key `admins`. When the store
  cannot be read, or the saved list is unreadable, nobody but the owner is admin (added admins are
  signed out, and a failed read or an unreadable list logs `dashboard admins unreadable, only the owner is admin: ...` to
  `logs/dashboard.err.log`) and the Settings page says so; an add or remove then changes nothing (503,
  or 409 for an unreadable list). When the owner's list, add or remove cannot read the store, it logs
  `dashboard admin list unreadable: ...` there too. The event stream announces a change by key only (`admins`); the store's own event
  record carries only the addresses and the account that changed the list (only the key when the
  addresses would not fit in an event, so every open tab still re-reads its role).

### Summary endpoint for a co-hosted portal

`GET /api/summary` answers one JSON object (version, executor state, queue bucket counts, the
model policy in effect, newest event timestamp) for an intranet portal running on the same host.
It is **not** part of the browser API: no session, no cookie, no CSRF.

- **Guard.** The request must come from a loopback peer (`127.0.0.1`/`::1`) **and** carry
  `X-Service-Token` matching the settings key `rol_service_token` (constant-time compare). A
  non-loopback peer is 403, a missing or wrong token is 403.
- **Optional by design.** Without `rol_service_token` in `dashboard-secrets.json` the route
  answers 404 as though it did not exist, so an existing deployment is unchanged by this feature.
  When the key is present it must be a string of at least 32 characters or the web app refuses to
  boot (the value is never printed). Provision it like the other secrets: operator-set, in the
  `0600` settings file, never committed.
- Counts use the queue page's own buckets (`active` = running, `attention` = failed / interrupted
  / stale, `history` = every attempt, the console's unfiltered History query); `succeeded_24h` / `failed_24h` count attempts whose `updated_at`
  — the completion time of a terminal attempt — falls within the last 24 hours.

```sh
curl -sS -H "X-Service-Token: $ROL_SERVICE_TOKEN" http://127.0.0.1:8840/api/summary
```

Lifecycle is coupled to the daemon: `./service.sh install` installs both jobs (and fails if the
dashboard cannot be installed), `stop`/`uninstall` shut both down, and `uninstall` preserves
`data/` (review state and attempt history) plus the external settings file. Dashboard-specific
operations go through `./service.sh dashboard <preflight|install|verify|start|stop|uninstall|resume>`.

## Deployment

The service runs from a plain directory tree on a macOS host: the review daemon and the dashboard
are two launchd jobs started from that tree. Homebrew users can run `rol` instead (see
`docs/distribution.md`); this section is for a self-hosted tree, for example
`/opt/review-of-legends/main`.

```sh
git clone https://github.com/2lab-ai/review-of-legends.git /opt/review-of-legends/main
cd /opt/review-of-legends/main
cp config/deploy.env.example config/deploy.env   # then edit it
$EDITOR config/reviewer.json                     # repositories, GitHub App, dashboard URL
./install.sh
```

### Deployment settings

Everything that names a host, a company or a person is configuration, never code:

- `config/reviewer.json`: repositories to review, the GitHub App (`app_id`, `installation_id`,
  `private_key_path`), the dashboard public URL, the model policy and per-repository agents.
- `config/deploy.env` (optional, `KEY=VALUE` lines, each key once, read as data and never
  executed; a variable set and non-empty in the environment wins):

  | Key | Default | Used by |
  | --- | --- | --- |
  | `ROL_SERVICE_LABEL` | `ai.2lab.review-of-legends` | daemon launchd job |
  | `ROL_DASHBOARD_LABEL` | `<ROL_SERVICE_LABEL>.dashboard` | dashboard launchd job |
  | `ROL_DASHBOARD_OWNER` | none: the dashboard refuses to start | the one owner address, always admin |
  | `ROL_DASHBOARD_VIEWER_DOMAINS` | none: no viewers | comma-separated Google Workspace domains that sign in read-only |
  | `ROL_DASHBOARD_PUBLIC_URL` | none: the dashboard refuses to start | the HTTPS origin the reverse proxy serves |

- Secrets stay outside the tree with mode `0600`: the App private key (`private_key_path`) and
  the dashboard settings file `$HOME/review-of-legends/dashboard-secrets.json`.

`./install.sh` refuses to install when another LaunchAgent already runs from the same tree (two
daemons on one tree would post every review twice): uninstall the other job first.

A deployment repository can automate the steps: pin a released commit of this repository, copy its
own `config/reviewer.json` and `config/deploy.env` over the tree, run the test suite with the
host's `/usr/bin/python3`, drain (below), sync the tree with `data/` and `logs/` excluded, and run
`./install.sh`. The maintainers deploy their own instance that way from a private repository.

### Versions

Releases follow llmux's two channels (see `AGENTS.md`):

- **Preview:** every push to `main` publishes a GitHub prerelease `preview-<UTC minute>-<sha12>`
  with a source archive and `SHA256SUMS`.
- **Stable:** the Release workflow tags the next `vMAJOR.MINOR.PATCH` (`scripts/release.py` holds
  the rule) and publishes the archive, `SHA256SUMS` and the Homebrew formula.

A deployed tree carries `VERSION` (the tag) and `version.json` (the commit, the build time and the
commits since the previous release). To see which version is running: `/healthz`, the dashboard
footer, `bin/code-review-service version` in the tree, or the `Version:` line at the top of every
posted PR review.

### Draining a review before the tree is replaced

Replacing the tree while a review is running would lose the attempt, so the deploy asks the daemon
to pause instead of killing it, **before** the rsync:

1. the deploy creates the pause sentinel `data/executor.pause` (kept by the `data/` rsync
   exclude, so it survives the tree swap and the reinstall);
2. the daemon checks that sentinel before it starts a review or claims a retry, and again right
   after it takes the executor lock — so while it exists no new review, PR or retry is picked up.
   What is already running is never killed; when the last review in flight finishes the daemon
   releases the lock and waits, checking the sentinel every 5 seconds;
3. the deploy continues only once the executor lock (`data/executor.lock`) is observably free on
   two consecutive checks, which happens when the in-flight reviews finish on their own. Otherwise
   the deploy is refused (exit 3) with "re-run after it finishes". No attempt is ever declared dead
   because of its age, and the sentinel/lock paths are read from the deployed tree's own
   `dashboard_execution` + `config/reviewer.json` rather than hardcoded here;
4. the final deploy step always removes the sentinel (`./service.sh dashboard resume`) so a
   failed deploy cannot leave the daemon paused. The lock file itself is never touched.

The daemon's launchd status is read as three values, and only a positive "not loaded" counts as
stopped: a launchctl error is **unknown**, and unknown is drained exactly like running. The executor
lock is probed for **every** status, including "not loaded", because `code-review-service once`
(`run_once`) takes the same lock from a plain shell without launchd — a stopped launchd job is never
taken as proof that nothing is reviewing.

### Verification

The same deploy that installs the code verifies both halves: `code-review-service doctor` plus
`launchctl state = running` for the daemon, and for the dashboard `launchctl state = running`,
`/healthz` reporting the deployed `VERSION`, and `401` on `/api/me`, `/api/runs`, `/api/usage`
and `/api/events` without a session (a run payload in an anonymous response fails the deploy).

The launchd plists (`ROL_SERVICE_LABEL`, `ROL_DASHBOARD_LABEL`) live under `$HOME`, but runtime
state is local to the deployed tree: `data/state.json`, `data/work`, and `logs/`. A deploy must
exclude `data/` and `logs/` when it replaces the tree, so code changes never drop review state or logs.
The tree also includes `service.sh` and a `service` wrapper, so from the tree you can run `./service status`, `./service start`, `./service stop`,
`./service restart`, and `./service logs follow`.

## Notes

The current GitHub CLI account must be authenticated and authorized for every configured
repository: it clones PR sources, and it posts unless a GitHub App is configured. If
`gh auth status` reports an invalid token, run `gh auth login -h github.com` before expecting the
service to fetch PRs or post comments.

## Portable dashboard deployment

The original deployment examples above describe a historical installation. New installations have no built-in dashboard owner or viewer domain. Set the following deployment environment before provisioning/starting the optional dashboard:

```sh
export ROL_DASHBOARD_OWNER='owner@example.com'
export ROL_DASHBOARD_VIEWER_DOMAINS='example.com'
export ROL_DASHBOARD_PUBLIC_URL='https://reviews.example.com'
```

The public URL must be an HTTPS origin and must match `public_url` in the protected dashboard settings. The legacy installer copies these policy variables into its LaunchAgent. Keep the same values on subsequent installs; do not reuse the internal identities in the historical examples. An empty viewer-domain list disables domain-wide viewer access. Google OIDC validation still applies; setting an owner never bypasses verified sign-in.

Provision the existing Google client ID/secret, llmux admin key, session secret, and HTTPS reverse proxy using the dashboard procedure above. Enable `dashboard.enabled` in the reviewer configuration to record dashboard data, and set its `dashboard.public_url` to your public URL for commit-status links. The CLI setup keeps the dashboard disabled until this separate configuration is complete.
