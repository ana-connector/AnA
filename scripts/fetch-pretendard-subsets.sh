#!/usr/bin/env bash
# Re-downloads Pretendard Variable's dynamic subset into the web app's public
# directory. Run after bumping VERSION; the CSS in
# apps/web/src/app/pretendard.css names the same files and ranges.
#
# The single-file build is 2.06 MB and the browser needed all of it to draw
# one screen of Korean text. These 92 files carry one `unicode-range` each.
set -euo pipefail

VERSION="${PRETENDARD_VERSION:-1.3.9}"
SUBSETS="${PRETENDARD_SUBSETS:-92}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/apps/web/public/fonts/pretendard"
BASE="https://unpkg.com/pretendard@${VERSION}/dist/web/variable/woff2-dynamic-subset"

mkdir -p "$OUT"
for index in $(seq 0 $((SUBSETS - 1))); do
  name="PretendardVariable.subset.${index}.woff2"
  curl -fsSL -m 90 "$BASE/$name" -o "$OUT/$name"
  # A CDN that answers with an error page writes a "font" the browser cannot
  # parse, and the page then silently falls back to a system font.
  case "$(file -b "$OUT/$name")" in
    "Web Open Font Format"*) ;;
    *) echo "not a woff2: $name" >&2; exit 1 ;;
  esac
done
echo "fetched $SUBSETS subsets of Pretendard $VERSION into $OUT"
