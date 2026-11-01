#!/usr/bin/env bash
# Compile app/diskmap.icon (Icon Composer, D51) into app/icon/: Assets.car (macOS 26, Liquid
# Glass) and diskmap.icns (older macOS, Finder fallbacks). Needs Xcode: actool is not in the
# Command Line Tools, which is all Homebrew has — so the compiled icon is kept in the repo,
# and app/build.sh only copies it (D54). source.sha256 is the hash of the sources it was
# compiled from; test.sh fails when they changed and this was not run again.
#
#   app/icon.sh

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/app/diskmap.icon"
OUT="$ROOT/app/icon"
MIN_MACOS=26.0

xcrun --find actool >/dev/null 2>&1 || { echo "icon: no actool — install Xcode" >&2; exit 1; }
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
xcrun actool "$SRC" --compile "$TMP" \
  --app-icon diskmap --platform macosx --target-device mac \
  --minimum-deployment-target "$MIN_MACOS" --output-partial-info-plist "$TMP/icon.plist" \
  --output-format human-readable-text --notices --warnings --errors >/dev/null
mkdir -p "$OUT"
cp "$TMP/Assets.car" "$TMP/diskmap.icns" "$OUT/"
"$ROOT/app/icon_hash.sh" >"$OUT/source.sha256"
echo "compiled $SRC → $OUT"
