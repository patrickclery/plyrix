#!/usr/bin/env bash
# Regenerate the brand files in assets/brand. Usage: make_brand.sh [--check] [output-dir]
#
# Dev tool only: tests never call this, and nothing here is a runtime dependency. The SVGs are
# written from constants by make_brand.py (the lockup wordmark is outlined from Fira Sans SemiBold
# 4.301 through fontconfig and rsvg-convert), then rasterised with rsvg-convert and packed into
# favicon.ico with ImageMagick. --check rebuilds into a temp dir and compares every generated
# file byte for byte with output-dir; hand-written files such as README.md are not compared.
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd)

check=0
if [ "${1:-}" = "--check" ]; then
  check=1
  shift
fi

for tool in rsvg-convert fc-match magick uv; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "make_brand.sh: $tool is required but was not found" >&2
    exit 2
  fi
done

readonly TILE="#115e59"

# build <dir>: write every generated file into <dir>.
build() {
  local dir=$1
  mkdir -p "$dir"
  uv run --no-project -q python -I "$repo_root/scripts/make_brand.py" "$dir"
  # The 16 px icon comes from its own 16-unit snap; halving the master merges two bars.
  rsvg-convert -w 16 -h 16 "$dir/mark-16.svg" -o "$dir/mark-16.png"
  rsvg-convert -w 32 -h 32 "$dir/mark.svg" -o "$dir/mark-32.png"
  rsvg-convert -w 512 -h 512 "$dir/mark.svg" -o "$dir/mark-512.png"
  # iOS masks the corners of a touch icon itself and shows transparency as black, so this one
  # is a full-bleed opaque tile.
  rsvg-convert -w 180 -h 180 -b "$TILE" "$dir/mark.svg" -o "$dir/mark-180.png"
  # Explicit PNGs keep the hand-snapped small pixels; never let magick resize.
  magick "$dir/mark-16.png" "$dir/mark-32.png" "$dir/favicon.ico"
}

out_dir=${1:-"$repo_root/assets/brand"}

if [ "$check" -eq 0 ]; then
  build "$out_dir"
  exit 0
fi

tmp=$(mktemp -d "${TMPDIR:-/tmp}/plyrix-brand.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
build "$tmp"
status=0
for f in "$tmp"/*; do
  name=$(basename "$f")
  if [ ! -e "$out_dir/$name" ]; then
    echo "make_brand.sh: missing $name" >&2
    status=1
  elif ! cmp -s "$f" "$out_dir/$name"; then
    echo "make_brand.sh: $name differs from a fresh build" >&2
    status=1
  fi
done
if [ "$status" -eq 0 ]; then
  echo "make_brand.sh: every generated file matches"
fi
exit "$status"
