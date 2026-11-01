#!/usr/bin/env bash
# Build diskmap.app (A′, K14): the page of `diskmap ui` in a native window (app/diskmap.swift).
#
# Two kinds of bundle, the same window:
# - a build for this Mac (D53): the bundle carries diskmap's own code (core/, cli/, ui/,
#   compiled here) and runs it with the Python it was built with — PYTHON, else python3 on
#   PATH; it does not carry an interpreter;
# - --command DISKMAP (D54): the bundle carries no code and runs that `diskmap` command —
#   the Homebrew formula's, by its opt/ path, so a copy of the app runs whatever is installed.
# Signed ad hoc either way: no Developer ID, not notarized. The icon is the compiled one in
# app/icon/ (app/icon.sh): actool is not in the Command Line Tools Homebrew builds with.
#
#   app/build.sh                               → app/build/diskmap.app
#   PYTHON=/path/python3 app/build.sh
#   OUT=/prefix app/build.sh --command /opt/homebrew/opt/diskmap/bin/diskmap
#
# Nothing here touches a disk: it compiles, copies and signs files under OUT.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${OUT:-$ROOT/app/build}"  # test.sh builds into a temporary folder, the formula into its prefix
APP="$OUT/diskmap.app"
MIN_MACOS=26.0  # the only macOS diskmap has been run on (CLAUDE.md §5)

COMMAND=
case "${1:-}" in
  --command)
    COMMAND="${2:-}"
    [[ "$COMMAND" == /* ]] || { echo "build: --command takes the absolute path of diskmap" >&2; exit 2; } ;;
  "") ;;
  *) echo "usage: app/build.sh [--command /path/to/diskmap]" >&2; exit 2 ;;
esac

PYTHON="${PYTHON:-$(command -v python3 || true)}"
[[ -n "$PYTHON" && -x "$PYTHON" ]] || { echo "build: no python3 — set PYTHON" >&2; exit 1; }
[[ "$PYTHON" == /* ]] || { echo "build: PYTHON must be an absolute path: $PYTHON" >&2; exit 1; }
command -v swiftc >/dev/null || { echo "build: no swiftc — install Xcode or its command line tools" >&2; exit 1; }
# The interpreter itself, not a python3 link that moves to the next minor version: the bytecode
# below is for this one, and with another `sudo … apply` would write new bytecode into the
# bundle as root. Gone with its version, the app says so instead (D53).
PYTHON=$("$PYTHON" -c 'import sys; print(sys.executable)')

VERSION=$(cd "$ROOT" && "$PYTHON" -B -c 'from core.version import VERSION; print(VERSION)')
if [[ -n "$COMMAND" ]]; then
  echo "diskmap $VERSION, the window over $COMMAND"
else
  echo "diskmap $VERSION, Python $("$PYTHON" -c 'import sys; print(sys.version.split()[0])') at $PYTHON"
fi

rm -rf "$APP"
CONTENTS="$APP/Contents"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"

swiftc -O -target "arm64-apple-macos$MIN_MACOS" -o "$CONTENTS/MacOS/diskmap" "$ROOT/app/diskmap.swift"

cp "$ROOT/LICENSE" "$ROOT/app/icon/Assets.car" "$ROOT/app/icon/diskmap.icns" "$CONTENTS/Resources/"
if [[ -z "$COMMAND" ]]; then
  CODE="$CONTENTS/Resources/diskmap"  # the server's working folder: `python -m cli` finds diskmap here
  mkdir -p "$CODE"
  for pkg in core cli ui; do
    rsync -a --exclude '__pycache__' --exclude '*.pyc' "$ROOT/$pkg" "$CODE/"
  done
  # Bytecode now, so nothing is written into the bundle later — least of all by root, when
  # Terminal runs `sudo python -m cli apply` in this folder (ui/terminal.py, ROOT).
  "$PYTHON" -m compileall -q "$CODE"
  # The interpreter can run this code at all: the stdlib it needs is there.
  (cd "$CODE" && "$PYTHON" -B -E -s -c 'import cli.main, ui.server') \
    || { echo "build: $PYTHON cannot import diskmap" >&2; exit 1; }
fi

"$PYTHON" -B - "$ROOT/app/Info.plist" "$CONTENTS/Info.plist" "$VERSION" "$PYTHON" "$COMMAND" <<'EOF'
import plistlib, sys
template, out, version, python, command = sys.argv[1:]
with open(template, "rb") as f:
    info = plistlib.load(f)
info["CFBundleShortVersionString"] = info["CFBundleVersion"] = version  # D52: one string
if command:
    info["DiskmapCommand"] = command
else:
    info["DiskmapPython"] = python
with open(out, "wb") as f:
    plistlib.dump(info, f)
EOF
plutil -lint -s "$CONTENTS/Info.plist"

codesign --force --sign - --timestamp=none "$APP"
codesign --verify --strict "$APP"
echo "built $APP"
