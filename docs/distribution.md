# Distribution

Reference: [2lab-ai/llmux](https://github.com/2lab-ai/llmux), inspected at `main` on 2026-10-29. Its stable/preview release workflows publish checksums and distribute via `2lab-ai/homebrew-tap`. The RoL core is standard-library Python, so a checksummed source archive and Python dependency suffice; there is no Rust/native binary build to duplicate.

The requested xbrew reference is the separate [2lab-ai/xbrew](https://github.com/2lab-ai/xbrew) repository. Its manifest parser, platform documentation, and macOS formula recipe implementation were inspected on 2026-10-29.

## Release channels

Releases follow llmux's two channels; the runbook is in [AGENTS.md](../AGENTS.md#release).

- **Preview:** every push to `main` runs `.github/workflows/preview.yml`. It builds
  `review-of-legends-YYYY.MM.DD.HHMM.tar.gz`, `SHA256SUMS` and the `review-of-legends-preview`
  formula, and publishes them as the prerelease `preview-<YYYY-MM-DD-HHMM>-<sha12>`. The
  newest 15 previews are kept.
- **Stable:** pushing a tag `vMAJOR.MINOR.PATCH` on a commit of `main` runs
  `.github/workflows/release.yml`: tests, then `review-of-legends-X.Y.Z.tar.gz`, `SHA256SUMS`
  and the `review-of-legends` formula on the Release, with generated notes.

To build the same artifacts locally (nothing is published):

```sh
python3 scripts/package-release.py 1.1.0 --out dist
cat dist/SHA256SUMS
```

The builder exports only tracked runtime inputs and writes `VERSION` (the release tag) into the
archive. The formula references that immutable release asset, never a moving branch.

### Tap

The formulae live in [2lab-ai/homebrew-tap](https://github.com/2lab-ai/homebrew-tap), next to
llmux's. With the repository variable `ROL_TAP_REPOSITORY` and the secret `TAP_DISPATCH_TOKEN`
(contents write on the tap only) set, both workflows push their formula to the tap; once opted in,
a release whose tap push fails is a failed release. Without them, copy the formula from the
Release into `Formula/` by hand.

After `brew tap 2lab-ai/tap`, users run `brew install review-of-legends` (or
`review-of-legends-preview`; the two conflict because both install `rol`). Verify a new formula on
a clean macOS host with `brew install --build-from-source 2lab-ai/tap/review-of-legends`,
`brew test review-of-legends` and `brew audit --strict review-of-legends`.

The formula uses `opt_libexec` for assets, so saved prompt and runner paths survive upgrades.
Settings and keys stay under the user's home directory. The formula installs only runtime code,
prompts and public defaults. The optional dashboard is a separate deployment.

## Self-hosted deployments

A deployment that runs from a tree rather than Homebrew (see
[operations](operations.md#deployment)) pins a stable release by its full commit id and overlays
its own `config/reviewer.json` and `config/deploy.env`. The shipped `config/reviewer.json` is a
generic template: never deploy it over a live configuration.

## xbrew

`packaging/xbrew/software.yaml` follows xbrew's `trust:` / `xbrew:` manifest format. It registers and trusts `2lab-ai/tap`, then installs `llmux` and `review-of-legends`. Homebrew must already be on PATH because tap registration happens before package installation. If needed, run `xbrew install brew` first and follow its PATH instructions.

After RoL's public tap/release are published, from a checkout:

```sh
xbrew bundle packaging/xbrew/software.yaml
rol setup
```

For a macOS single-package alias, install the supplied local recipe after reviewing it:

```sh
mkdir -p ~/.xbrew/recipes
cp packaging/xbrew/review-of-legends.toml ~/.xbrew/recipes/
xbrew install review-of-legends
rol setup
```

The recipe uses xbrew's native `macos.formula` backend, as its existing somawork recipe does. That backend handles the fully qualified tap formula and tracks the Homebrew installation for uninstall. The local recipe is not an upstream xbrew registration. No change was made to 2lab-ai/xbrew or its recipe registry.

On Linux, use the bundle's Homebrew fallback after installing Homebrew; no Arch/AUR/apt/dnf package or native-binary recipe is claimed here. Clean-host xbrew installation still requires acceptance testing alongside the actual published RoL formula. The recipe installs RoL; `rol setup` can install llmux if absent, while the bundle includes both up front.

## Installation evidence

`Public CI / installation` builds a version `0.0.0` test archive, rewrites only the generated formula URL to a local file, installs it with the actual Homebrew engine on a disposable macOS runner, runs `brew test`, and exercises the installed runtime's setup against isolated fake upstream services. It does not publish the archive or post a live review. The lifecycle check runs the real Homebrew service with a pause sentinel, no App credentials and a disabled GitHub executable, then checks stop/restart, upgrade from 0.0.0 to 0.0.1, removal of the old keg, and preservation of private settings and prompt/runner paths. A second check builds the pinned xbrew revision and verifies bundle ordering, adoption on rerun, and uninstall routing against a fake package manager.

A local attempt using a copied Homebrew in `/tmp` was refused by Homebrew's inherited-sandbox write check. The guard was not disabled. Local extracted-archive tests remain useful but are not represented as a successful `brew install`; the hosted installation job supplies that separate evidence.
