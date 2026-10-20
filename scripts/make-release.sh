#!/bin/bash
# Builds dist/dop-call-recorder-<version>.tar.gz (source tarball the Homebrew formula builds from) and prints its sha256.
set -euo pipefail
cd "$(dirname "$0")/.."
VER="$(python3 -c 'import re;print(re.search(r"__version__ = \"(.+?)\"", open("dop_call_recorder/__init__.py").read()).group(1))')"
NAME="dop-call-recorder-$VER"
STAGE="$(mktemp -d)/$NAME"; mkdir -p "$STAGE" dist
rsync -a --exclude .venv --exclude .bench --exclude build --exclude dist --exclude __pycache__ --exclude '*.egg-info' --exclude .pytest_cache \
      --exclude .DS_Store --exclude scripts/audioprobe --exclude Formula --exclude .git --exclude .github --exclude vocabulary.yaml ./ "$STAGE/"
cp vocabulary.example.yaml "$STAGE/vocabulary.yaml"
tar -czf "dist/$NAME.tar.gz" -C "$(dirname "$STAGE")" "$NAME"
echo "dist/$NAME.tar.gz"; shasum -a 256 "dist/$NAME.tar.gz"
