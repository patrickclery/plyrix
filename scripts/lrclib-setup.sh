#!/usr/bin/env bash
# Load an official LRCLIB dump into LRCLIB's data directory and wait until LRCLIB answers searches.
# Usage: LRCLIB_DATA=DIR lrclib-setup.sh lrclib-db-dump-YYYYMMDDTHHMMSSZ.sqlite3.gz
#
# Steps, in order. Anything that fails stops the script before the old database is touched:
#   1. Check the dump name, that LRCLIB_DATA is a folder this user can write, and, unless
#      LRCLIB_STOP and LRCLIB_START are both set, that docker compose can read compose.yaml.
#   2. Run `$PLYRIX doctor --lrclib-data DIR --dump NAME`, which checks the folder and its free
#      space, counting only NAME's download in DIR/dl, and stop with its exit code if it fails.
#   3. Download DIR/dl/NAME.part from the official host with curl. A transfer that breaks part way
#      (a dropped connection, an HTTP/2 stream error, a timeout, a 5xx) is retried with backoff,
#      resuming from the bytes already on disk, until LRCLIB_DOWNLOAD_RETRIES attempts in a row add
#      nothing (or after 1000 attempts in all). A partial download is never deleted: if it can't be finished, it is kept, so the
#      next run resumes it. Only a fresh range request from the same offset answered with the whole
#      file shows the server can't resume; the script then stops and says how to start again. The finished
#      size must equal the server's HEAD size. A finished DIR/dl/NAME is reused.
#   4. Check the download with `gzip -t`, then rename it to DIR/dl/NAME. The dump has no published
#      checksum, so the gzip CRC is the only integrity check there is.
#   5. Decompress to DIR/db.sqlite3.staged while a running LRCLIB keeps serving the old database,
#      and check the SQLite header.
#   6. The swap lrclib-update.sh uses, journaled in DIR/.plyrix-update.journal: stop LRCLIB, rename
#      the old database, its WAL and SHM and its search index to db.sqlite3.previous*, rename the
#      staged file to db.sqlite3, and start LRCLIB. With the default start, LRCLIB's image is built
#      from GitHub after step 2, so the start after the swap needs no network.
#   7. Poll LRCLIB's search until its index is ready. If the start or the wait fails on a reload,
#      the old database set is put back and LRCLIB started on it; a first install has nothing to
#      put back, so its new database stays in place. Then record the dump live in plyrix when
#      plyrix is configured, or print the dump_id to configure, delete the previous set, and say
#      how to delete the download, which is kept.
# The run takes the lock lrclib-update.sh takes (exit 75 when it is held), and first finishes or
# undoes a swap that a crash left part way. When that recovery finds this same dump already in
# place, from a first load cut short or a swap left unrecorded, it isn't loaded again: setup goes
# straight to step 7, or is done.
#
# Environment:
#   LRCLIB_DATA           LRCLIB's data directory (required); compose mounts it at /data
#   PLYRIX                the plyrix command (default: plyrix), split into words. In a container:
#                         PLYRIX="docker compose -f CHECKOUT/compose.yaml run --rm --no-deps plyrix"
#                         with CHECKOUT the checkout's absolute path. --no-deps stops compose
#                         waiting for an LRCLIB that can't be healthy before its first load. The
#                         plyrix service mounts LRCLIB_DATA at the same path, read-only, so doctor
#                         measures the same folder.
#   LRCLIB_DUMP_BASE      where dumps are fetched (default: https://db-dumps.lrclib.net); tests
#                         point it at file:// or a local stub
#   LRCLIB_STOP           command that stops LRCLIB (default: docker compose stop lrclib)
#   LRCLIB_START          command that starts LRCLIB (default: docker compose up -d --no-build
#                         lrclib, after `docker compose build lrclib` before the download)
#   MUSIC_ROOT, STATE_DIR passed to compose if set; else a placeholder, as only plyrix's service
#                         uses them
#   LRCLIB_URL            LRCLIB's API (default: http://127.0.0.1:${LRCLIB_PORT:-3300}/api)
#   LRCLIB_DOWNLOAD_RETRIES  download attempts in a row that add no bytes before giving up
#                         (default: 10); one that adds bytes starts the count again
#   LRCLIB_RETRY_SECONDS  first wait between download attempts, doubled after each attempt that
#                         adds nothing, up to 300 (default: 5)
#   LRCLIB_READY_TIMEOUT  seconds to wait for the search index (default: 43200, as it can take hours)
#   LRCLIB_GET_TIMEOUT    seconds lrclib-update.sh waits for /api/get after a swap (default: 600)
#   LRCLIB_POLL_SECONDS   seconds between polls (default: 30)
#
# Exit codes: 0 ready, 1 a download, check or wait failed, 2 a bad argument or folder, or doctor's,
# 75 an update or another setup holds the lock.
set -euo pipefail

usage() {
  echo "usage: LRCLIB_DATA=DIR lrclib-setup.sh lrclib-db-dump-YYYYMMDDTHHMMSSZ.sqlite3.gz" >&2
  exit 2
}

[ "$#" -eq 1 ] || usage
name=$1
[[ $name =~ ^lrclib-db-dump-[0-9]{8}T[0-9]{6}Z\.sqlite3\.gz$ ]] || usage

# The download, unpack, compose and wait steps are shared with lrclib-update.sh (D-03).
. "$(dirname "${BASH_SOURCE[0]}")/lib/lrclib-dump.sh"

# 1. the dump name, LRCLIB_DATA, the settings and compose, then the lock lrclib-update.sh takes
# too, and the recovery of a swap a crash left part way
dump_init lrclib-setup
dump_lock
dump_what="the load of"
dump_retry_hint="running lrclib-setup.sh again tries it again"
dump_recover
db=$LRCLIB_DATA/db.sqlite3
gz=$LRCLIB_DATA/dl/$name

if [ "${dump_recovered_live:-}" = "$name" ]; then
  # Recovery found this dump swapped in and recorded it live, so nothing is left to do.
  echo "lrclib-setup: LRCLIB is ready with $name"
  exit 0
elif [ "${dump_recovered_first:-}" = "$name" ]; then
  # A first load of this dump that a crash cut short is in place, and recovery started LRCLIB on
  # it. Loading it again would redo hours of work, and doctor would count the database it already
  # holds against the space a load needs, so only the wait for its search index is left. The
  # journal is kept at starting until then, so a wait that fails is recovered the same way.
  dump_name=$name
  dump_step starting
  echo "lrclib-setup: $name is already in place from the last load, so it isn't loaded again"
  dump_wait_search
else
  # 2. doctor, before anything is made or fetched
  rc=0
  "${plyrix_cmd[@]}" doctor --lrclib-data "$LRCLIB_DATA" --dump "$name" || rc=$?
  [ "$rc" -eq 0 ] || dump_fail "doctor found a problem with LRCLIB_DATA" "$rc"

  dump_build_lrclib

  # 3 and 4. download and check
  dump_download "$name"

  # 5. decompress beside the old database, which stays in service
  dump_unpack "$gz" "$db.staged"

  # 6 and 7. the journaled swap, which waits for the search index and rolls back to an old
  # database on any failure. A first install has no old database, so it keeps the new one in
  # place.
  dump_swap "$name"
fi

echo "lrclib-setup: LRCLIB is ready with $name"
# plyrix may not be configured yet on a first install, so this only has to work when it is.
if plyrix_wait "${plyrix_cmd[@]}" lrclib-record "$name" --status live >/dev/null 2>&1; then
  recorded="lrclib-setup: plyrix recorded $name as LRCLIB's live dump, so dump_id needs no setting"
else
  recorded="Once plyrix is configured, run: plyrix lrclib-record $name --status live. Until then, set dump_id to $name (PLYRIX_DUMP_ID, or dump_id in the config file)."
fi
dump_step live
# Setup always keeps the download, and says how to delete it.
dump_cleanup --keep-download
echo "$recorded"
echo "The download is kept; once you're done with it: rm $gz"
