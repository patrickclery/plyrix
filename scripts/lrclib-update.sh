#!/usr/bin/env bash
# Keep a local LRCLIB on the newest official dump. Run it from a host timer or cron, daily.
# Usage: LRCLIB_DATA=DIR lrclib-update.sh
#
# This runs on the host, as the user who runs LRCLIB, because it stops and starts LRCLIB and
# renames its files, and plyrix never starts a process or renames anything outside the library's
# sidecars. No container gets the Docker socket. plyrix does only the read-only and state-only
# steps: the check, the verify and the record. The music library is never opened.
#
# Steps, in order. Nothing before step 7 touches the database LRCLIB serves:
#   1. Check LRCLIB_DATA and the settings, as lrclib-setup.sh does, and take an exclusive lock on
#      DIR/.plyrix-update.lock, so two updates never overlap. Then finish or undo a swap that a
#      crash left part way, from DIR/.plyrix-update.journal, and remove a kept previous database
#      once LRCLIB_KEEP_PREVIOUS_DAYS have passed.
#   2. Run `$PLYRIX lrclib-check --listing URL --base URL`, one small GET of the dump listing. When
#      it names no dump newer than the live one, or the listing fails or changes shape, print one
#      line and exit 0 with nothing downloaded or changed. For a newer dump it reads the first
#      64 KB, and a dump whose SQLite version the pinned LRCLIB can't serve is refused (exit 1)
#      before anything else is fetched.
#   3. Run `$PLYRIX doctor` with the dump's name, size and decompressed size, which needs room
#      for what is left of the download, the decompressed database and its search index, and stop
#      with doctor's exit code if it doesn't fit. Then build LRCLIB's image (unless LRCLIB_START
#      is set) and download the newer dump into DIR/dl, with the same resume, retries and checks
#      as lrclib-setup.sh.
#   4. Decompress it to DIR/db.sqlite3.staged, beside the live database, on the same filesystem.
#   5. Run `$PLYRIX lrclib-verify DIR/db.sqlite3.staged --name NAME` at idle IO priority. It opens
#      the file read-only and refuses one the pinned LRCLIB can't serve: the tables and columns,
#      the index, and PRAGMA quick_check. A refused file is deleted, the dump is recorded refused,
#      its download is kept, and LRCLIB is left alone.
#   6. Run `$PLYRIX lrclib-diff NAME --staged ... --previous-db DIR/db.sqlite3` at idle IO
#      priority. It records, as ids and 8-byte keys in plyrix's state database, the LRCLIB tracks
#      that are new or have new lyrics, for the next scan to retry. A failure stops the update.
#   7. Swap, journaled in DIR/.plyrix-update.journal: stop LRCLIB, rename db.sqlite3, its -wal
#      and -shm and its .tantivy index to db.sqlite3.previous*, rename the staged file to
#      db.sqlite3, start LRCLIB, wait for /api/get on a track of the new dump, then for its search
#      index. If the start or either wait fails, the previous set is renamed back, LRCLIB is
#      started on it, the dump is recorded rolled back, and the script exits 1.
#   8. Run `$PLYRIX lrclib-record NAME --status live`, so later runs use the new dump's id and
#      its diff. Every plyrix call after the check waits while another plyrix run holds the
#      state database, up to LRCLIB_STATE_WAIT seconds.
#   9. Delete the previous database and, unless LRCLIB_KEEP_DOWNLOAD=1, the download.
#  10. With LRCLIB_FOLLOW_UP=1, and only after a new dump went live: run `$PLYRIX run`, the scan
#      that looks up only what the update could change, then `$PLYRIX plex-refresh --limit
#      LRCLIB_PLEX_LIMIT`. That scan is plyrix's ordinary guarded run; the update's own steps
#      never open the music library.
#
# Environment (and every variable lrclib-setup.sh lists, which mean the same here):
#   LRCLIB_DATA          LRCLIB's data directory (required)
#   PLYRIX               the plyrix command (default: plyrix), configured with music_root,
#                        lrclib_url and state_db as for a run
#   LRCLIB_DUMP_LISTING  the dump listing (default: the one LRCLIB's db-dumps page reads)
#   LRCLIB_DUMP_BASE     where dumps are fetched (default: https://db-dumps.lrclib.net)
#   LRCLIB_STOP, LRCLIB_START  commands that stop and start LRCLIB (default: docker compose)
#   LRCLIB_QUICK_CHECK   1 runs SQLite's quick_check on the staged database (default), 0 skips it
#   LRCLIB_UPDATE_RETRY  1 tries a dump that was refused or rolled back again (default: 0, never)
#   LRCLIB_GET_TIMEOUT   seconds to wait for /api/get after the swap (default: 600)
#   LRCLIB_KEEP_DOWNLOAD 1 keeps the download once the new dump is live (default: 0)
#   LRCLIB_KEEP_PREVIOUS_DAYS  days to keep the previous database after a swap (default: 0)
#   LRCLIB_STATE_WAIT    seconds to wait for a busy state database after the check (default: 43200)
#   LRCLIB_FOLLOW_UP     1 runs the scan and a capped Plex refresh after a new dump (default: 0)
#   LRCLIB_PLEX_LIMIT    most album refreshes the follow-up sends (default: 100)
#
# Exit codes: 0 updated or no update, 1 a download, check, swap or wait failed, or a dump was
# refused or rolled back, 2 a bad argument, folder or setting, 3 the new dump is live but the
# follow-up didn't finish, 75 another update or plyrix run was busy, so the next run tries again.
set -euo pipefail

if [ "$#" -ne 0 ]; then
  echo "usage: LRCLIB_DATA=DIR lrclib-update.sh" >&2
  exit 2
fi

# shellcheck source=scripts/lib/lrclib-dump.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/lrclib-dump.sh"

# 1. settings and the lock
dump_init lrclib-update
quick_check=${LRCLIB_QUICK_CHECK:-1}
[[ $quick_check =~ ^[01]$ ]] || dump_fail "LRCLIB_QUICK_CHECK must be 0 or 1" 2
retry=${LRCLIB_UPDATE_RETRY:-0}
[[ $retry =~ ^[01]$ ]] || dump_fail "LRCLIB_UPDATE_RETRY must be 0 or 1" 2
follow=${LRCLIB_FOLLOW_UP:-0}
[[ $follow =~ ^[01]$ ]] || dump_fail "LRCLIB_FOLLOW_UP must be 0 or 1" 2
plex_limit=${LRCLIB_PLEX_LIMIT:-100}
[[ $plex_limit =~ ^[1-9][0-9]{0,5}$ ]] ||
  dump_fail "LRCLIB_PLEX_LIMIT must be a whole number of albums from 1, with no leading 0" 2
# The checks that read a whole database run at idle IO priority where the host has ionice.
if command -v ionice >/dev/null; then idle=(ionice -t -c3 nice -n19); else idle=(nice -n19); fi
dump_lock
dump_retry_hint="LRCLIB_UPDATE_RETRY=1 tries it again"

# With LRCLIB_FOLLOW_UP=1, once a new dump is live: plyrix's ordinary scan, which looks up only
# what the update could change, then a Plex refresh capped at LRCLIB_PLEX_LIMIT albums and paced
# by plex_pace, through the pending-refresh ledger (D-10), run even when the scan didn't finish.
# Albums over the cap wait for the next plex-refresh. Off by default, so the update's own steps never open the music library.
follow_up() {
  [ "$follow" -eq 1 ] || return 0
  local rc=0 refresh_rc=0
  plyrix_wait "${plyrix_cmd[@]}" run || rc=$?
  # The refresh runs whatever the scan's result: a run that couldn't finish every track, which is
  # usual in a large library, still wrote sidecars that need it, as plyrix.service does.
  plyrix_wait "${plyrix_cmd[@]}" plex-refresh --limit "$plex_limit" || refresh_rc=$?
  [ "$rc" -ne 0 ] || rc=$refresh_rc
  if [ "$rc" -ne 0 ]; then
    echo "lrclib-update: $1 is live, but the follow-up run or Plex refresh didn't finish (exit $rc); the next scheduled run does it" >&2
    exit 3
  fi
}

dump_recover
# A swap a crash left at swapped is live now, so its follow-up is due.
if [ -n "${dump_recovered_live:-}" ]; then follow_up "$dump_recovered_live"; fi

listing=${LRCLIB_DUMP_LISTING:-https://lrclib-db-dumps.bu3nnyut4y9jfkdg.workers.dev} # gitleaks:allow
base=${LRCLIB_DUMP_BASE:-https://db-dumps.lrclib.net}
db=$LRCLIB_DATA/db.sqlite3

# 2. check
rc=0
check_args=(--listing "$listing" --base "$base")
[ "$retry" -eq 0 ] || check_args+=(--retry)
verdict=$("${plyrix_cmd[@]}" lrclib-check "${check_args[@]}") || rc=$?
case $rc in
  0) ;;
  1) [[ $verdict == "refused "* ]] || dump_fail "plyrix lrclib-check failed, so nothing was changed"
    dump_fail "the newest dump was refused, so nothing changed" ;;
  75) dump_fail "another plyrix run is using the state database, so nothing was done; the next check tries again" 75 ;;
  *) dump_fail "plyrix lrclib-check failed, so nothing was changed" "$rc" ;;
esac
if [ "$verdict" = none ]; then
  echo "lrclib-update: no newer dump; nothing changed"
  exit 0
fi
read -r word name size unpacked version extra <<<"$verdict" || true
# The name becomes a URL and a path, so it is checked here again, whatever printed it.
if [ "$word" != update ] || [ -n "${extra:-}" ] ||
  ! [[ ${name:-} =~ ^lrclib-db-dump-[0-9]{8}T[0-9]{6}Z\.sqlite3\.gz$ ]] ||
  ! [[ ${size:-} =~ ^[0-9]{1,13}$ ]] || ! [[ ${unpacked:-} =~ ^[0-9]{1,15}$ ]] ||
  ! [[ ${version:-} =~ ^[0-9]{1,3}$ ]]; then
  dump_fail "plyrix lrclib-check printed a line this script can't use, so nothing was changed"
fi
echo "lrclib-update: $name is newer than the live dump, so it will be loaded"

# 3. room for it, before anything is fetched. A kept previous database goes first, as the live
# one is now what a rollback would go back to.
dump_drop_kept
rc=0
"${plyrix_cmd[@]}" doctor --lrclib-data "$LRCLIB_DATA" --dump "$name" --size "$size" \
  --unpacked "$unpacked" || rc=$?
[ "$rc" -eq 0 ] || dump_fail "doctor found a problem with LRCLIB_DATA" "$rc"

# build and download
dump_build_lrclib
dump_download "$name"

# 4. unpack beside the live database, which stays in service
staged=$db.staged
dump_unpack "$gz" "$staged"

# 5. verify, read-only, before LRCLIB is touched
verify_args=("$staged" --name "$name")
[ "$quick_check" -eq 1 ] || verify_args+=(--no-quick-check)
rc=0
verified=$("${idle[@]}" "${plyrix_cmd[@]}" lrclib-verify "${verify_args[@]}") || rc=$?
# Only a refusal says so on stdout. Any other exit 1, a crash or a container that didn't start,
# isn't about the dump, so it isn't recorded, and the next run tries the dump again.
if [ "$rc" -eq 1 ] && [ "$verified" = refused ]; then
  rm -f "$staged"
  plyrix_wait "${plyrix_cmd[@]}" lrclib-record "$name" --status refused >/dev/null ||
    echo "lrclib-update: plyrix couldn't record $name as refused" >&2
  echo "lrclib-update: $name was refused, so nothing changed; LRCLIB_UPDATE_RETRY=1 tries it again; its download is kept; once you're done with it: rm $gz"
  exit 1
fi
if [ "$rc" -ne 0 ]; then
  rm -f "$staged"
  dump_fail "plyrix lrclib-verify failed, so $name was not loaded and LRCLIB was left alone" "$rc"
fi
if ! [[ $verified =~ ^ok\ [0-9]+$ ]]; then
  rm -f "$staged"
  dump_fail "plyrix lrclib-verify printed a line this script can't use, so nothing was changed"
fi
probe_id=${verified#ok }

# 6. diff the staged database against the live one's watermarks, before LRCLIB is touched. The
# diff writes only ids and keys to plyrix's state database, and waits for another plyrix run.
rc=0
diffed=$(plyrix_wait "${idle[@]}" "${plyrix_cmd[@]}" lrclib-diff "$name" --staged "$staged" \
  --previous-db "$db") || rc=$?
if [ "$rc" -ne 0 ] || ! [[ $diffed =~ ^diff\ (complete|unstable|none)\ [0-9]+$ ]]; then
  rm -f "$staged"
  [ "$rc" -ne 0 ] || rc=1
  dump_fail "plyrix lrclib-diff failed, so nothing was swapped and LRCLIB was left alone" "$rc"
fi
echo "lrclib-update: $diffed"

# 7. swap, journaled, rolling back on any failure
dump_swap "$name" "$probe_id"

# 8. record. A failure leaves the journal at swapped, and the next run records it. The record
# makes the diff active.
if ! plyrix_wait "${plyrix_cmd[@]}" lrclib-record "$name" --status live; then
  dump_fail "LRCLIB serves $name, but plyrix couldn't record it; run: ${PLYRIX:-plyrix} lrclib-record $name --status live"
fi
dump_step live

# 9. clean up
dump_cleanup
echo "lrclib-update: LRCLIB is ready with $name"

# 10. the follow-up, when LRCLIB_FOLLOW_UP=1
follow_up "$name"
