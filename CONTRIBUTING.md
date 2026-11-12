# Contributing

## Who can open a pull request that gets reviewed

This repository runs its own contributor gate: pull requests are reviewed, and their CI runs, only for approved contributors listed in [`.github/APPROVED_CONTRIBUTORS`](.github/APPROVED_CONTRIBUTORS) (and maintainers with write access). If you are not on the list, open your pull request anyway. The bot comments on it with the ways in, including signing the [contributor pledge](.github/CONTRIBUTOR_PLEDGE.md) with one comment. A maintainer accepts you by merging the pull request the bot then opens.

## Working on the code

Use Python 3.10+ for the CLI. The core daemon has no third-party dependencies and must keep running on Python 3.9. The dashboard uses `requirements-dashboard.txt` and a separate virtual environment.

Read [AGENTS.md](AGENTS.md) first: it holds the architecture rules, conventions and release runbook. Create a feature branch and keep changes focused. Add regression tests for changed behavior and run `make check` (unit suite plus the dashboard's Node tests). For web-tier tests install the pinned dashboard requirements; tests skipped for missing dependencies are not web-tier validation.

Document user-visible changes in their owning doc ([rules/documents.md](rules/documents.md)). Do not commit real credentials, local settings, production repository lists, or captured PR content. Use `example/project`, `acme-corp/*` and `owner@company.example` in fixtures. Keep PR read paths and review-posting paths distinct; tests should not post to real PRs.

Describe the behavior changed, why, and the validation performed in pull requests. Avoid changing unrelated formatting. Report security issues through the private process in SECURITY.md.

This project is MIT licensed; see LICENSE. Contributions are accepted under the same license.

