#!/usr/bin/env bash
# Only the disposable GitHub runner may install into its default Homebrew prefix.
set -euo pipefail
if [[ "${GITHUB_ACTIONS:-}" != true ]]; then
  echo 'Run this installation check on a disposable GitHub Actions runner.' >&2
  exit 2
fi
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ARTIFACTS="$RUNNER_TEMP/rol-distribution"
python3 "$ROOT/scripts/package-release.py" 0.0.0 --out "$ARTIFACTS"
TAP_DIR="$(brew --repository)/Library/Taps/rol-ci/homebrew-tap"
mkdir -p "$TAP_DIR/Formula"
git init -q "$TAP_DIR"
python3 - "$ARTIFACTS" "$TAP_DIR" <<'PY'
from pathlib import Path
import sys
artifacts, tap = map(Path, sys.argv[1:])
formula = (artifacts/'review-of-legends.rb').read_text()
url = 'https://github.com/2lab-ai/review-of-legends/releases/download/v0.0.0/review-of-legends-0.0.0.tar.gz'
assert url in formula
formula = formula.replace(url, (artifacts/'review-of-legends-0.0.0.tar.gz').as_uri())
(tap/'Formula/review-of-legends.rb').write_text(formula)
PY
brew install --build-from-source rol-ci/tap/review-of-legends
brew test rol-ci/tap/review-of-legends
rol --help
[[ "$(rol version)" == v0.0.0 ]]
ROL_ACCEPTANCE_RUNTIME="$(brew --prefix review-of-legends)/libexec" \
  python3 -m unittest tests.test_setup_acceptance
# A real service runs paused on this disposable runner, without credentials.
python3 "$ROOT/scripts/test-homebrew-lifecycle.py"
