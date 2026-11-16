#!/usr/bin/env bash
# Regenerate every committed audio fixture and SHA256SUMS. Usage: make_fixtures.sh [output-dir]
#
# Tests never call this or ffmpeg: the output is committed, so the safety proof can't be skipped
# when ffmpeg is missing. All tones and tags are synthetic, and no real lyrics are involved.
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd)
out_dir=${1:-"$repo_root/tests/fixtures"}
mkdir -p "$out_dir"

readonly EMBEDDED_TOKEN="PLYRIX-EMBEDDED-TOKEN-7F3A"
readonly FULL_TAGS=(
  -metadata "artist=Fixture Artist"
  -metadata "album_artist=Fixture Album Artist"
  -metadata "title=Fixture Title"
  -metadata "album=Fixture Album"
)
readonly ALBUM_ARTIST_ONLY_TAGS=(
  -metadata "album_artist=Fixture Album Artist"
  -metadata "title=Fixture Title"
  -metadata "album=Fixture Album"
)
readonly VARIOUS_ARTISTS_TAGS=(
  -metadata "album_artist=Various Artists"
  -metadata "title=Fixture Title"
  -metadata "album=Fixture Album"
)

# gen <file> <sample-rate> <seconds> <tag args...> -- <codec args...>
gen() {
  local file=$1 rate=$2 seconds=$3
  shift 3
  local tags=()
  while [ "$1" != "--" ]; do
    tags+=("$1")
    shift
  done
  shift
  ffmpeg -hide_banner -loglevel error -y -f lavfi \
    -i "sine=frequency=440:sample_rate=$rate:duration=$seconds" -ac 1 "$@" \
    -map_metadata -1 -fflags +bitexact -flags:a +bitexact \
    ${tags[@]+"${tags[@]}"} "$out_dir/$file"
}

# all_formats <stem> <seconds> <tag args...>
all_formats() {
  local stem=$1 seconds=$2
  shift 2
  gen "$stem.flac" 8000 "$seconds" "$@" -- -c:a flac -compression_level 8
  gen "$stem.mp3" 8000 "$seconds" "$@" -- -c:a libmp3lame -b:a 8k -id3v2_version 3
  gen "$stem.m4a" 8000 "$seconds" "$@" -- -c:a aac -b:a 16k -movflags +faststart
  gen "$stem.ogg" 8000 "$seconds" "$@" -- -c:a libvorbis -q:a 0
  gen "$stem.opus" 16000 "$seconds" "$@" -- -c:a libopus -b:a 8k
}

all_formats t 3 "${FULL_TAGS[@]}"
gen t_id3v24.mp3 8000 3 "${FULL_TAGS[@]}" -- -c:a libmp3lame -b:a 8k -id3v2_version 4

all_formats albumartist_only 1 "${ALBUM_ARTIST_ONLY_TAGS[@]}"
all_formats various_artists 1 "${VARIOUS_ARTISTS_TAGS[@]}"
all_formats notags 1
all_formats embedded_token 1 "${FULL_TAGS[@]}" -metadata "lyrics=$EMBEDDED_TOKEN"

# A FLAC and an MP3 with no tags of their own behind an unsynchronised ID3v2.3 tag, as some taggers
# write them. The tag is built by a standard-library script, as ffmpeg can't write one.
for suffix in flac mp3; do
  if [ "$suffix" = flac ]; then codec=(-c:a flac -compression_level 8); else
    codec=(-c:a libmp3lame -b:a 8k -write_id3v2 0)
  fi
  gen ".unsync-base.$suffix" 8000 3 -- "${codec[@]}"
  python3 -I "$repo_root/scripts/prepend_id3v23_unsync.py" "$out_dir/.unsync-base.$suffix" \
    "$out_dir/id3v23_unsync.$suffix"
  rm "$out_dir/.unsync-base.$suffix"
done

cd "$out_dir"
find . -maxdepth 1 -type f ! -name SHA256SUMS -printf '%f\0' \
  | LC_ALL=C sort -z \
  | xargs -0 sha256sum >SHA256SUMS
