#!/bin/bash
# Local-only test flow (no publishing): build tarball, render the formula with file:// url + sha256, put it in a local tap.
#   scripts/tap-install-local.sh            -> tap devopspass/tap + formula ready: `brew install --build-from-source devopspass/tap/dop-call-recorder`
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="$(scripts/make-release.sh)"; TAR="$PWD/$(echo "$OUT" | head -1)"; SHA="$(echo "$OUT" | tail -1 | cut -d' ' -f1)"
brew tap devopspass/tap >/dev/null 2>&1 || brew tap-new --no-git devopspass/tap
DEST="$(brew --repository devopspass/tap)/Formula"; mkdir -p "$DEST"
sed -e "s|@URL@|file://$TAR|" -e "s|@SHA256@|$SHA|" Formula/dop-call-recorder.rb > "$DEST/dop-call-recorder.rb"
echo "formula: $DEST/dop-call-recorder.rb"
