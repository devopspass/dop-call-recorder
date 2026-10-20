#!/bin/bash
# Regenerates requirements.lock from requirements.in (needs `uv`). mlx-whisper (0.4.3) is appended by hand: see header notes in the lock.
set -euo pipefail
cd "$(dirname "$0")/.."
uv pip compile requirements.in --universal --generate-hashes --python-version 3.12 -o requirements.lock.new
echo "review requirements.lock.new, re-append the mlx-whisper block from requirements.lock, then mv it over requirements.lock"
