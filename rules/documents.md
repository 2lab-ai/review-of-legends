# Documentation ownership (`rules/documents.md`)

Binding for contributors and agents. Loaded via [`AGENTS.md`](../AGENTS.md).

## Rule

**Every user-visible change updates its owning doc in the same PR.** If nothing
user-facing changed, write a one-line reason in the PR body: `docs: N/A — <why>`.
Skipping both means the change is incomplete.

## Owning-doc map

| Change surface | Owning doc |
| --- | --- |
| Product story, install, quick start, what ships | [`README.md`](../README.md) |
| Scheduling, finding lifecycle, agents, merge checks, dashboard, deployment settings | [`docs/operations.md`](../docs/operations.md) |
| Release channels, formula, archive, xbrew | [`docs/distribution.md`](../docs/distribution.md) |
| `config/reviewer.json` keys | the `_comment` keys in [`config/reviewer.json`](../config/reviewer.json) and `docs/operations.md` |
| `config/deploy.env` keys | [`config/deploy.env.example`](../config/deploy.env.example) and `docs/operations.md` "Deployment settings" |
| Contributor and agent rules, release runbook | [`AGENTS.md`](../AGENTS.md) |

Prefer the narrowest doc a new user would open to understand the change.
