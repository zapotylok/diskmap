#!/usr/bin/env bash
# The hash of the icon's sources (app/diskmap.icon): names and contents, in a fixed order.
# app/icon.sh records it next to the compiled icon; test.sh compares the two.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
find diskmap.icon -type f ! -name .DS_Store | LC_ALL=C sort | while IFS= read -r f; do
  shasum -a 256 "$f"
done | shasum -a 256 | cut -d' ' -f1
