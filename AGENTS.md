# Review of Legends — agent and contributor guide (SSOT)

**This file is the single source of truth for contributor and agent rules.**
`CLAUDE.md` only loads it; do not add rule bodies there.

Documentation ownership after feature work: [`rules/documents.md`](rules/documents.md) (binding).

## What this is

A self-hosted GitHub pull request review bot. The daemon (`code_review_service/`) polls
repositories, runs several reviewer agents through an llmux endpoint, and posts one review
per head as a GitHub App. The optional dashboard (`code_review_service/dashboard_*`,
`dashboard/`) is a separate launchd job with its own virtualenv.

## Architecture rules

- **The daemon is Python standard library only** and must run on macOS's stock
  `/usr/bin/python3` (3.9). No 3.10+ syntax in `code_review_service/` outside the CLI
  and dashboard tiers, and no third-party imports there. CI runs the suite on 3.9.
- **The dashboard tier** (Starlette/Uvicorn/Authlib) lives in its own venv from
  `requirements-dashboard.txt`; every line there is pinned.
- **No environment in code.** Host names, company domains, owner addresses, repository
  lists, App ids, launchd labels and Slack channels are configuration:
  `config/reviewer.json` and `config/deploy.env` (see `docs/operations.md`
  "Deployment settings"). Fixtures use `acme-corp/*`, `owner@company.example` and
  `example.com`.
- **Secrets never enter the tree.** App keys and dashboard OAuth secrets live outside it,
  mode `0600`. Agent subprocesses get an allowlisted environment only.
- **Review state is idempotent.** `data/state.json` is the posted-review ledger; a deploy
  never replaces `data/` or `logs/`, and a running review is drained, never killed.

## Conventions

- Conventional commits, lowercase subject (`fix: …`, `feat: …`, `docs: …`, `ci: …`).
- `make check` must pass before every commit: compile on the local `python3`, the unit
  suite, and the dashboard's Node tests. CI runs the same commands on macOS's stock Python 3.9 and on
  3.13, macOS and Linux.
- Feature branches only; `main` is protected and every push to it publishes a preview.
- **User-visible changes update their owning doc in the same PR** —
  [`rules/documents.md`](rules/documents.md). Otherwise write `docs: N/A — <why>` in the PR.
- Tests never post to real PRs and never reach the real `launchd`: stub `launchctl` on
  `PATH` when a test runs `service.sh`.
- Never commit names of private systems. The Sanitize workflow scans full history with the
  maintainers' secret pattern list and gitleaks; a hit fails the PR.

## Release

Two channels, as [llmux](https://github.com/2lab-ai/llmux):

- **Preview** — every push to `main` publishes the prerelease
  `preview-<YYYY-MM-DD-HHMM>-<sha12>` (archive, `SHA256SUMS`, formula
  `review-of-legends-preview`). See `.github/workflows/preview.yml`.
- **Stable** — push a tag `vMAJOR.MINOR.PATCH` on a commit of `main`:
  `.github/workflows/release.yml` tests it, publishes the archive, `SHA256SUMS` and the
  `review-of-legends` formula, with generated notes. Bump the patch by default; a minor or
  major bump is a deliberate choice stated in the PR that prepares it.

```sh
git checkout main && git pull
git tag v1.1.1 && git push origin v1.1.1
```

- **Tap** — with repository variable `ROL_TAP_REPOSITORY` and secret `TAP_DISPATCH_TOKEN`
  set, both workflows push their formula to the tap; once opted in a failed push fails the
  release. Users install with `brew install 2lab-ai/tap/review-of-legends`.
- **Deployments pin stable releases.** A deployment repository pins one stable tag by full
  commit id, overlays its own `config/reviewer.json` and `config/deploy.env`, and deploys
  that (see `docs/operations.md` "Deployment"). Preview builds are for opt-in testers, never
  production.

## Load-bearing facts

- `scripts/package-release.py` archives tracked files only; `VERSION` in the archive is the
  release tag, and the dashboard venv directory is named after it.
- `service.sh` and `scripts/install-dashboard.py` both read `config/deploy.env` as data
  (`KEY=VALUE`, known `ROL_*` keys, each once); a non-empty environment variable wins over
  the file. `./service.sh
  labels` prints the launchd labels a tree installs.
- `./service.sh install` refuses when another LaunchAgent runs from the same tree: two
  daemons on one tree post every review twice.
- The dashboard refuses to start without `ROL_DASHBOARD_OWNER` and an HTTPS
  `ROL_DASHBOARD_PUBLIC_URL`.
