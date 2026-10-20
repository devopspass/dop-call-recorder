#!/bin/bash
# Builds and ad-hoc signs build/DOPCallRecorder.app: ONE bundle for recorder + Accessibility helper + menu-bar app (one macOS permission identity).
# Stable bundle id => TCC grants persist across rebuilds only while the code hash is unchanged; ad-hoc rebuilds reset them.
set -euo pipefail
cd "$(dirname "$0")"
OUT="${OUT_DIR:-build}"   # brew sets OUT_DIR=<libexec>/apps
APP="$OUT/DOPCallRecorder.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/menubar"
swiftc -O -swift-version 5 -target arm64-apple-macosx14.2 Sources/*.swift -o "$APP/Contents/MacOS/dop-call-recorder"
cp ../icons/MenuAppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
cp ../icons/menubar8/*.png "$APP/Contents/Resources/menubar/"
cp Info.plist "$APP/Contents/Info.plist"
[ -n "${OUT_DIR:-}" ] || (cd .. && pwd) > "$APP/Contents/Resources/dop_cr_root"  # dev only: repo root with .venv/bin/dop-recorder; brew passes DOP_RECORDER_BIN instead
codesign --force --sign - --identifier com.devopspass.call-recorder --entitlements entitlements.plist "$APP"
codesign --verify --verbose=2 "$APP"
echo "built $APP"
