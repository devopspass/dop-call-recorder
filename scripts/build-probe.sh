#!/bin/bash
# Compiles the call-detector audio probe. Output: $OUT_DIR (brew: <libexec>/bin) or scripts/.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${OUT_DIR:-scripts}"; mkdir -p "$OUT"
swiftc -O -target arm64-apple-macosx14.2 detector/audioprobe.swift -o "$OUT/audioprobe"
echo "built $OUT/audioprobe"
