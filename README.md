# Review of Legends

**A self-hosted code review bot that brings several specialist reviewers to every pull request.**

Connect your own llmux accounts, choose GitHub repositories, and let the bot post actionable inline findings, a change outline, and an executive summary. Reviews run on your machine; the configured model provider receives the review prompts and diff.

## What it does today

- **Seven review perspectives:** correctness, security, maintainability, tests, reliability, red-team, and simplification. Enable the optional server architect per repository.
- **Reviews that follow the conversation:** merge findings on the same line, avoid repeating open findings, respect earlier answers, and tally feedback. Resolving outdated threads requires optional Contents write permission.
- **Visible progress:** a live PR comment reports each agent's progress; a visual outline explains the change and a separate summary synthesizes the results.
- **A contributor gate for public repositories:** review, and approve fork CI for, only approved contributors listed in the repository's own `.github/APPROVED_CONTRIBUTORS`; company accounts get in by verifying their email domain, anyone else by signing a contributor pledge that a maintainer accepts with one merge.
- **Continuous review without a webhook server:** poll open PRs, skip drafts, review new commits, cap concurrency, and retry failed attempts with backoff.
- **Your models and rules:** use llmux's ChatGPT/Codex accounts, configure fallback models, tune prompts, and incorporate repository rules from the PR's base revision.
- **Controlled merge checks:** minimum reviewer quorum and optional required reviewers. Automatic approval is off by default. The `reviewed/<branch>` status records review history, not proof that the latest head passed.
- **Optional operator dashboard:** queue, history, retries, model policy, prompts, repository settings, and account usage. Its web/OAuth deployment is separate from the CLI quick start.

## Install and set up

Install from the 2lab-ai Homebrew tap (published with each stable release):

```sh
brew tap 2lab-ai/tap
brew install review-of-legends
rol setup
```

Or use the fully qualified formula, following [llmux's installation pattern](https://github.com/2lab-ai/llmux#install):

```sh
brew install 2lab-ai/tap/review-of-legends
rol setup
```

For [xbrew](https://github.com/2lab-ai/xbrew) users, the included bundle installs llmux and RoL together after the taps are published. From a checkout:

```sh
# If Homebrew is absent: xbrew install brew, then apply its printed PATH instructions.
xbrew bundle packaging/xbrew/software.yaml
rol setup
```

The bundle registers and trusts both taps before installation. An optional macOS local recipe for `xbrew install review-of-legends` is included; see [xbrew distribution](docs/distribution.md#xbrew).

Until publication, run from a checkout with Python 3.10+, Git, GitHub CLI, and OpenSSL installed:

```sh
git clone https://github.com/2lab-ai/review-of-legends.git
cd review-of-legends
./bin/rol setup
```

`rol setup` walks through:

1. GitHub CLI authentication (needed to clone PR source).
2. Reusing llmux via `llmux env`, or installing `2lab-ai/tap/llmux`; optionally connecting your ChatGPT subscription with `llmux login --codex`. A stopped local server can be started by setup.
3. Naming and creating your own GitHub App through GitHub's browser manifest flow, automatically saving its key, or importing an existing App. Setup suggests a unique name; you approve the installation on GitHub.
4. Choosing an installation and the repositories to monitor, selecting a model, and deciding whether to process existing PRs on first start.
5. Saving private configuration, running diagnostics, and offering to start the review bot. A Homebrew installation starts a background service; a source checkout runs in the foreground.

Re-run setup to change the configuration. Existing configuration is backed up; an interrupted App installation resumes from the saved registration. Keep the terminal open during browser authorization. Setup uses no hosted callback service.

**Before starting:** the bot posts to the selected repositories and uses your connected model account. By default, existing PRs form a baseline; subsequent PRs and commits are reviewed. Choose backlog review in setup to include existing PRs immediately.

## Everyday commands

```sh
rol doctor                        # check configuration, GitHub credentials, and proxy reachability
rol start                         # Homebrew background service
rol status
rol stop
rol daemon                        # foreground, also usable with your own supervisor
rol once --repo owner/repo --pr 42 --dry-run
rol once --repo owner/repo --pr 42
```

Configuration and credentials live in `~/.config/review-of-legends/`; state and review workspaces live in its `data/` directory. Credentials are written with mode `0600`. They are not stored in the Homebrew Cellar or repository. `ROL_HOME` selects another directory for foreground use. `rol --config /path/reviewer.json daemon` runs an existing deployment configuration.

Homebrew upgrades retain settings and state. `brew services restart review-of-legends` loads an upgrade. llmux is managed independently (`llmux update`, `llmux restart`). Setup's local llmux process survives the terminal but is not registered for reboot; use llmux's own service management for unattended restarts.

## Configuration and boundaries

- The default model is `gpt-6-astra[1m]`; select a model your llmux account can serve. A reachable model catalog is not a guarantee of available subscription quota.
- The default App needs Contents read, Pull requests write, Issues write (for reactions to comment commands), and Commit statuses write. No webhook endpoint is required. It cannot resolve threads automatically without Contents write.
- One setup selects one GitHub App installation. For multiple organizations, use separate `ROL_HOME` directories and foreground supervisors.
- llmux keeps upstream subscription credentials. RoL stores only its downstream client key; it does not copy ChatGPT login tokens. Remote llmux endpoints must use HTTPS in setup.
- Dashboard installation is not part of `rol setup`; see [operations](docs/operations.md#admin-dashboard) and [deployment settings](docs/operations.md#deployment-settings).

## Documentation and development

- [Operations reference](docs/operations.md): scheduling, finding lifecycle, agents, merge checks, dashboard, and self-hosted deployment.
- [Distribution](docs/distribution.md): release channels, Homebrew formula, checksums, and xbrew.
- [AGENTS.md](AGENTS.md): architecture rules, conventions, and the release runbook.
- [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

```sh
make check
```

The review daemon is Python standard-library only. The optional dashboard dependencies are pinned in `requirements-dashboard.txt`.

## License

MIT. See [LICENSE](LICENSE) and [third-party notices](THIRD_PARTY_NOTICES.md).
