# shellcheck shell=bash
# The download, unpack, compose and wait steps of an LRCLIB dump load, sourced by both
# scripts/lrclib-setup.sh and scripts/lrclib-update.sh, so the first install and every update
# take one code path (D-03). Each message takes its prefix from the tag dump_init is given.
#
# Functions:
#   dump_init TAG          check LRCLIB_DATA and the settings, and set the compose, plyrix, stop
#                          and start commands (plyrix_cmd, stop_cmd, start_cmd, start_shown)
#   dump_fail MSG [CODE]   print "TAG: MSG" on stderr and exit CODE (default 1)
#   dump_build_lrclib      build LRCLIB's image, unless LRCLIB_START is set
#   dump_download NAME     download DIR/dl/NAME, resuming, and check it; sets gz
#   dump_unpack GZ DEST    decompress GZ to DEST and check the SQLite header; DEST goes on failure
#   dump_wait_search       poll LRCLIB's search until its index is ready
#   dump_wait_get ID       poll LRCLIB's /api/get/ID until it answers; returns 1 on timeout
#   dump_lock              take the lock updates and setup share, or exit 75
#   dump_step STEP         write the swap's journal, `STEP NAME`, before the step runs
#   dump_swap NAME [ID]    swap DIR/db.sqlite3.staged in, journaled, rolling back on any failure
#   dump_rollback NAME STEP  put the previous database set back, start LRCLIB on it, and exit 1
#   dump_cleanup [--keep-download]  after the live record, remove the previous set and download
#   dump_recover           finish or undo a swap a crash left part way, before anything else
#   dump_drop_kept         remove a previous set kept by LRCLIB_KEEP_PREVIOUS_DAYS
#   plyrix_wait CMD...     run a plyrix command again while it exits 75, up to LRCLIB_STATE_WAIT
#
# The settings it reads are listed in lrclib-setup.sh's header.

dump_fail() {
  echo "$dump_tag: $1" >&2
  exit "${2:-1}"
}

dump_init() {
  dump_tag=$1
  [ -n "${LRCLIB_DATA:-}" ] || dump_fail "set LRCLIB_DATA to LRCLIB's data directory" 2
  [ -d "$LRCLIB_DATA" ] || dump_fail "LRCLIB_DATA is not a folder" 2
  [ -w "$LRCLIB_DATA" ] || dump_fail "LRCLIB_DATA can't be written by this user" 2
  LRCLIB_DATA=$(cd "$LRCLIB_DATA" && pwd -P)
  export LRCLIB_DATA

  timeout=${LRCLIB_READY_TIMEOUT:-43200}
  poll=${LRCLIB_POLL_SECONDS:-30}
  [[ $timeout =~ ^[0-9]+$ ]] || dump_fail "LRCLIB_READY_TIMEOUT must be a whole number of seconds" 2
  [[ $poll =~ ^[0-9]+(\.[0-9]+)?$ ]] || dump_fail "LRCLIB_POLL_SECONDS must be a number of seconds" 2
  retries=${LRCLIB_DOWNLOAD_RETRIES:-10}
  retry_wait=${LRCLIB_RETRY_SECONDS:-5}
  [[ $retries =~ ^[0-9]+$ ]] && [ "$retries" -gt 0 ] ||
    dump_fail "LRCLIB_DOWNLOAD_RETRIES must be a whole number above 0" 2
  # No leading 0, which bash arithmetic reads as octal, and no more digits than a wait needs.
  [[ $retry_wait =~ ^(0|[1-9][0-9]{0,5})$ ]] ||
    dump_fail "LRCLIB_RETRY_SECONDS must be a whole number of seconds, from 0 to 999999, with no leading 0" 2

  get_timeout=${LRCLIB_GET_TIMEOUT:-600}
  [[ $get_timeout =~ ^[0-9]+$ ]] || dump_fail "LRCLIB_GET_TIMEOUT must be a whole number of seconds" 2
  keep_download=${LRCLIB_KEEP_DOWNLOAD:-0}
  [[ $keep_download =~ ^[01]$ ]] || dump_fail "LRCLIB_KEEP_DOWNLOAD must be 0 or 1" 2
  state_wait=${LRCLIB_STATE_WAIT:-43200}
  [[ $state_wait =~ ^[0-9]+$ ]] || dump_fail "LRCLIB_STATE_WAIT must be a whole number of seconds" 2
  keep_days=${LRCLIB_KEEP_PREVIOUS_DAYS:-0}
  [[ $keep_days =~ ^(0|[1-9][0-9]{0,4})$ ]] ||
    dump_fail "LRCLIB_KEEP_PREVIOUS_DAYS must be a whole number of days, with no leading 0" 2

  command -v gzip >/dev/null || dump_fail "gzip is needed to check and unpack the dump" 2

  # The checkout is the one holding the script that was run, whose compose.yaml is beside scripts/.
  repo=$(cd "$(dirname "${BASH_SOURCE[${#BASH_SOURCE[@]} - 1]}")/.." && pwd -P)
  # Compose checks every service's variables for any command, and only plyrix's service uses
  # MUSIC_ROOT and STATE_DIR, so a placeholder stands in for each one that isn't set. The lrclib
  # commands below never create plyrix's container or its mounts.
  # LRCLIB_DATA, and LRCLIB_USER and LRCLIB_PORT when set, are named too, so the start command
  # printed if LRCLIB won't start runs the same start when pasted into a shell without them.
  compose_env=("LRCLIB_DATA=$LRCLIB_DATA" "MUSIC_ROOT=${MUSIC_ROOT:-/nonexistent}"
    "STATE_DIR=${STATE_DIR:-/nonexistent}")
  [ -z "${LRCLIB_USER:-}" ] || compose_env+=("LRCLIB_USER=$LRCLIB_USER")
  [ -z "${LRCLIB_PORT:-}" ] || compose_env+=("LRCLIB_PORT=$LRCLIB_PORT")
  compose=(env "${compose_env[@]}" docker compose -f "$repo/compose.yaml")
  read -ra plyrix_cmd <<<"${PLYRIX:-plyrix}"
  if [ -n "${LRCLIB_STOP:-}" ]; then read -ra stop_cmd <<<"$LRCLIB_STOP"; else stop_cmd=("${compose[@]}" stop lrclib); fi
  # compose.yaml's pull_policy: build rebuilds LRCLIB from GitHub on every up, so the default start
  # skips that, and the image is built before anything is changed instead.
  if [ -n "${LRCLIB_START:-}" ]; then
    read -ra start_cmd <<<"$LRCLIB_START"
    start_shown=$LRCLIB_START
  else
    start_cmd=("${compose[@]}" up -d --no-build lrclib)
    # Quoted for a shell, so a checkout path with a space survives a copy and paste.
    start_shown=$(printf '%q ' "${start_cmd[@]}")
    start_shown=${start_shown% }
  fi

  # The swap runs compose only after hours of work, so check now that it can read the file.
  if [ -z "${LRCLIB_STOP:-}" ] || [ -z "${LRCLIB_START:-}" ]; then
    "${compose[@]}" config -q lrclib >/dev/null ||
      dump_fail "docker compose can't read $repo/compose.yaml; fix that, or set LRCLIB_STOP and LRCLIB_START" 2
  fi
}

# Build LRCLIB's image now, from its pinned commit on GitHub, so that starting it after the swap
# can't fail on the network with the new database in place. A build that is up to date is quick.
dump_build_lrclib() {
  if [ -z "${LRCLIB_START:-}" ]; then
    "${compose[@]}" build lrclib ||
      dump_fail "LRCLIB's image didn't build, and nothing was changed; fix that and run again"
  fi
}

_dump_size_of() {
  if [ -f "$1" ]; then wc -c <"$1" | tr -d ' '; else echo 0; fi
}

# The dump's size from a HEAD whose last answer is 200 (or file://, which has no status line).
# Anything but a positive whole number, such as 0 or "123, 123", prints nothing: no size is known.
_dump_remote_size() {
  local n
  n=$(curl -sIL --max-time 60 "$url" 2>/dev/null | tr -d '\r' |
    awk '/^HTTP\// { code = $2; n = "" } tolower($1) == "content-length:" { n = $2 }
      END { if (code == "" || code == 200) print n }') || true
  if [[ $n =~ ^[0-9]{1,18}$ ]] && ((10#$n > 0)); then echo "$((10#$n))"; fi
}

# The status of a fresh request from offset $1 to the end, the same open range curl -C - sends, as
# some proxies honour only a bounded one: 206 means the server can resume. --max-filesize stops
# the body, and curl still prints the status.
_dump_range_probe() {
  curl -sL -o /dev/null --max-time 60 --max-filesize 65536 -r "$1-" -w '%{http_code}' "$url" \
    2>/dev/null || true
}

# Whether curl's exit $1, with HTTP status $2, is worth another attempt from the same offset.
_dump_retryable() {
  case $1 in
    6 | 7 | 16 | 18 | 28 | 35 | 52 | 55 | 56 | 92) return 0 ;;
    22) [[ $2 =~ ^(408|429|5[0-9][0-9])$ ]] ;;
    *) return 1 ;;
  esac
}

# Download DIR/dl/NAME.part from LRCLIB_DUMP_BASE, check it with gzip -t, and rename it to
# DIR/dl/NAME, which is reused when it is already there and whole. Sets dl, gz, part and url.
dump_download() {
  local name=$1 length have fails attempts rc code now reason probe next pause i grc
  dl=$LRCLIB_DATA/dl
  mkdir -p "$dl" "$LRCLIB_DATA/tmp"
  gz=$dl/$name
  part=$gz.part
  url=${LRCLIB_DUMP_BASE:-https://db-dumps.lrclib.net}/$name

  if [ -f "$gz" ] && gzip -t "$gz" 2>/dev/null; then
    echo "$dump_tag: using the download already in $gz"
    return 0
  fi
  length=$(_dump_remote_size)
  have=$(_dump_size_of "$part")
  if [ -n "$length" ] && [ "$have" -gt "$length" ]; then
    dump_fail "$part holds $have bytes, more than the server's $length, so it isn't this dump; delete it with: rm $part, and run again"
  fi
  if [ -n "$length" ] && [ "$have" -gt 0 ] && [ "$have" -eq "$length" ]; then
    echo "$dump_tag: $part is already complete"
  fi
  fails=0
  attempts=0
  while [ -z "$length" ] || [ "$have" -lt "$length" ]; do
    attempts=$((attempts + 1))
    rc=0
    code=$(curl -fL -C - --connect-timeout 30 --speed-limit 1024 --speed-time 120 \
      -w '%{http_code}' -o "$part" "$url") || rc=$?
    now=$(_dump_size_of "$part")
    if [ "$rc" -eq 0 ]; then
      [ -n "$length" ] || length=$(_dump_remote_size)
      if [ -z "$length" ] || [ "$now" -ge "$length" ]; then
        have=$now
        break
      fi
      reason="the server ended the transfer at $now of $length bytes"
    elif [ "$rc" -eq 33 ]; then
      # One refused resume can be a hiccup; only a fresh range request shows the server can't.
      probe=$(_dump_range_probe "$now")
      if [ "$probe" = 200 ]; then
        dump_fail "the server answered a range request with the whole file, so it can't resume; $part ($now bytes) is kept. To download from zero, delete it with: rm $part, and run again"
      fi
      reason="curl exit 33, but a range request then got HTTP ${probe:-no answer}"
    elif _dump_retryable "$rc" "$code"; then
      reason="curl exit $rc${code:+, HTTP $code}"
    else
      dump_fail "the download failed (curl exit $rc${code:+, HTTP $code}); $part ($now bytes) is kept, so running again resumes it"
    fi
    if [ "$now" -gt "$have" ]; then fails=0; else fails=$((fails + 1)); fi
    have=$now
    if [ "$fails" -ge "$retries" ] || [ "$attempts" -ge 1000 ]; then
      next="so run again to resume it"
      # Refused resumes that probes call hiccups may yet be a server that can't resume.
      [ "$rc" -ne 33 ] || next="$next, or, if the server never resumes it, delete it with: rm $part, and run again"
      dump_fail "the download failed $fails times in a row, last with $reason; $part ($have bytes) is kept, $next"
    fi
    pause=$retry_wait
    for ((i = 1; i < fails && pause < 300; i++)); do pause=$((pause * 2)); done
    ((pause <= 300)) || pause=300
    echo "$dump_tag: $reason, with $have bytes on disk; resuming in ${pause}s" >&2
    sleep "$pause"
  done
  if [ -n "$length" ] && [ "$have" -ne "$length" ]; then
    dump_fail "the download holds $have bytes but the server's file has $length; $part is kept. Delete it with: rm $part, and run again"
  fi
  # gzip exits 1 on corrupt data. Any other failure (2 a warning, 126 or 127 no gzip, 128 and up a
  # signal) isn't a verdict of corruption, so only exit 1 at the server's full size deletes it.
  # gzip also exits 1 on a read error, so a failing disk can still delete a full-size download.
  grc=0
  gzip -t "$part" || grc=$?
  if [ "$grc" -ne 0 ]; then
    if [ -z "$length" ]; then
      dump_fail "$part fails its check, and the server gave no size to tell a cut-off download from a corrupt one; it is kept. Run again to resume it, or delete it with: rm $part"
    fi
    # A warning, such as trailing garbage, is about the file, so a rerun gets it again, and
    # unpacking it would stop the script.
    if [ "$grc" -eq 2 ]; then
      dump_fail "gzip -t warns about $part (exit 2), so it can't be used as it is, and it is kept; delete it with: rm $part, and run again to fetch it"
    fi
    if [ "$grc" -ne 1 ]; then
      dump_fail "gzip -t couldn't check $part (exit $grc), so it is kept; fix that and run again, or delete it with: rm $part"
    fi
    rm -f "$part"
    dump_fail "the download is the server's full size but corrupt, so it was deleted; run again to fetch it"
  fi
  mv "$part" "$gz"
}

# Decompress GZ to DEST beside the old database, which stays in service, and check the SQLite
# header. DEST is removed if the script ends before dump_swap journals its first step, as the
# verify and the diff after this can run for a long time and nothing else would remove it.
dump_unpack() {
  local source=$1
  _dump_unpacking=$2
  trap 'rm -f "$_dump_unpacking"' EXIT
  gzip -dc "$source" >"$_dump_unpacking"
  if [ "$(head -c 15 "$_dump_unpacking" | tr -d '\0')" != "SQLite format 3" ]; then
    dump_fail "$(basename "$source") doesn't hold an SQLite database; delete $source and check the name"
  fi
}

# Poll LRCLIB's search until its index answers, failing after LRCLIB_READY_TIMEOUT seconds.
dump_wait_search() {
  _dump_ready_search ||
    dump_fail "LRCLIB's search index wasn't ready after $timeout seconds; check LRCLIB's logs and run doctor"
}

# Poll LRCLIB's search until its index answers. Returns 1 after LRCLIB_READY_TIMEOUT seconds.
_dump_ready_search() {
  local probe answer code body started
  probe="$(_dump_api)/search?artist_name=plyrix+preflight&track_name=plyrix+preflight"
  echo "$dump_tag: waiting for LRCLIB to build its search index, which can take hours"
  started=$SECONDS
  while :; do
    answer=$(curl -s --max-time 60 -w '\n%{http_code}' "$probe" 2>/dev/null) || answer=
    code=${answer##*$'\n'}
    body=${answer%$'\n'*}
    if [ "$code" = 200 ] && [ "${body:0:1}" = "[" ]; then
      return 0
    fi
    if ((SECONDS - started >= timeout)); then
      return 1
    fi
    sleep "$poll"
  done
}

# Poll LRCLIB's /api/get/ID, the route for one track by id (router.rs), until it answers with
# that track. Returns 1 after LRCLIB_GET_TIMEOUT seconds. This answers before the search index is
# built, so it shows the server started on the new database.
dump_wait_get() {
  local probe answer code body started
  probe="$(_dump_api)/get/$1"
  started=$SECONDS
  while :; do
    answer=$(curl -s --max-time 60 -w '\n%{http_code}' "$probe" 2>/dev/null) || answer=
    code=${answer##*$'\n'}
    body=${answer%$'\n'*}
    if [ "$code" = 200 ] && [ "${body:0:1}" = "{" ]; then
      return 0
    fi
    if ((SECONDS - started >= get_timeout)); then
      return 1
    fi
    sleep "$poll"
  done
}

_dump_api() {
  echo "${LRCLIB_URL:-http://127.0.0.1:${LRCLIB_PORT:-3300}/api}"
}

# Take the lock every update and setup run shares, on fd 9, exiting 75 when another run holds it.
# The lock lives beside what it guards, and is never removed: a lock on a removed file guards
# nothing. The kernel lets it go when this process ends, however it ends.
dump_lock() {
  command -v flock >/dev/null || dump_fail "flock (util-linux) is needed to keep two updates apart" 2
  exec 9>>"$LRCLIB_DATA/.plyrix-update.lock"
  if ! flock -n 9; then
    echo "$dump_tag: another update of LRCLIB_DATA is running, so nothing was done" >&2
    exit 75
  fi
}

# Write the swap's journal, one whole line `STEP NAME`, before the step runs, so a run after a
# crash knows what was under way. A temp file and a rename keep the line whole.
dump_step() {
  local journal=$LRCLIB_DATA/.plyrix-update.journal
  printf '%s %s\n' "$1" "$dump_name" >"$journal.tmp"
  mv -T "$journal.tmp" "$journal"
}

# Swap DIR/db.sqlite3.staged in for NAME, journaled. The old database, its -wal and -shm and its
# search index are renamed aside, never deleted, so any failure can put them back, and the new
# database starts with no index, as one built for the old database would keep stale entries.
# Returns 0 at the step swapped. A failed stop changes nothing. A failed start, /api/get/PROBE_ID
# wait (when a probe id is given) or search wait rolls back through dump_rollback, which exits.
dump_swap() {
  dump_name=$1
  local probe_id=${2:-} db=$LRCLIB_DATA/db.sqlite3 suffix
  local staged=$db.staged
  [ ! -e "$db.previous.kept" ] || _dump_drop_previous
  if [ -e "$db.previous" ]; then
    rm -f "$staged"
    dump_fail "$db.previous is left from an earlier update, so nothing changed; once you're done with it, delete it and its -wal, -shm and .tantivy, then run again"
  fi
  # From here the journal guards the staged database, and recovery removes it.
  trap - EXIT
  dump_step stopping
  if ! "${stop_cmd[@]}"; then
    rm -f "$staged" "$LRCLIB_DATA/.plyrix-update.journal"
    dump_fail "LRCLIB didn't stop, so the old database is still in place and nothing changed"
  fi
  dump_step moving_old
  # Never delete a WAL here: a killed LRCLIB leaves committed data in it.
  for suffix in "" -wal -shm; do
    [ ! -e "$db$suffix" ] || mv -T "$db$suffix" "$db.previous$suffix"
  done
  [ ! -e "$db.tantivy" ] || mv -T "$db.tantivy" "$db.previous.tantivy"
  dump_step moving_new
  mv -T "$staged" "$db"
  dump_step starting
  "${start_cmd[@]}" || dump_rollback "$dump_name" start
  if [ -n "$probe_id" ]; then
    dump_wait_get "$probe_id" || dump_rollback "$dump_name" "the wait for /api/get"
  fi
  _dump_ready_search || dump_rollback "$dump_name" "the wait for the search index"
  dump_step swapped
}

# Put the previous database set back after STEP failed, start LRCLIB on it and exit 1. With no
# previous database, as on a first install, there is nothing to put back: the new database stays
# in place, and the message says how to start LRCLIB.
dump_rollback() {
  dump_name=$1
  local step=$2 db=$LRCLIB_DATA/db.sqlite3 suffix
  if [ ! -e "$db.previous" ]; then
    rm -f "$LRCLIB_DATA/.plyrix-update.journal"
    if [ "$step" = start ]; then
      dump_fail "LRCLIB didn't start, but the new database is in place; fix that, start it with: $start_shown, then run doctor"
    fi
    dump_fail "LRCLIB's search index wasn't ready after $timeout seconds; check LRCLIB's logs and run doctor"
  fi
  "${stop_cmd[@]}" || true
  _dump_restore
  if ! "${start_cmd[@]}"; then
    dump_fail "${dump_what:-the update to} $dump_name was rolled back after $step failed, and the previous database is back in place, but LRCLIB didn't start; start it with: $start_shown"
  fi
  if ! _dump_ready_search; then
    dump_fail "${dump_what:-the update to} $dump_name was rolled back after $step failed, and the previous database is back in place, but LRCLIB's search index didn't answer after $timeout seconds; check LRCLIB's logs"
  fi
  dump_step rolled_back
  plyrix_wait "${plyrix_cmd[@]}" lrclib-record "$dump_name" --status rolled-back >/dev/null ||
    echo "$dump_tag: plyrix couldn't record $dump_name as rolled back" >&2
  dump_fail "${dump_what:-the update to} $dump_name was rolled back after $step failed, so LRCLIB serves the previous database; its download is kept; ${dump_retry_hint:-run again to try it again}"
}

# Remove the new database's files and rename the previous set back. LRCLIB must be stopped.
# With --old-only, as after a crash before the new database was moved in, every file at its own
# name is still the old one's, so nothing is removed.
_dump_restore() {
  local db=$LRCLIB_DATA/db.sqlite3 suffix
  if [ "${1:-}" != --old-only ]; then
    rm -f "$db" "$db-wal" "$db-shm"
    rm -rf "$db.tantivy"
  fi
  for suffix in "" -wal -shm; do
    [ ! -e "$db.previous$suffix" ] || mv -T "$db.previous$suffix" "$db$suffix"
  done
  [ ! -e "$db.previous.tantivy" ] || mv -T "$db.previous.tantivy" "$db.tantivy"
}

# Remove the previous database set. LRCLIB serves the new dump by now.
_dump_drop_previous() {
  local db=$LRCLIB_DATA/db.sqlite3
  rm -rf "$db.previous.tantivy"
  rm -f "$db.previous" "$db.previous-wal" "$db.previous-shm" "$db.previous.kept"
}

# After the live record: remove the previous set, or keep it with a marker when
# LRCLIB_KEEP_PREVIOUS_DAYS is set, and, unless LRCLIB_KEEP_DOWNLOAD=1 or --keep-download is
# given, the download, then the finished journal.
dump_cleanup() {
  if [ "$keep_days" -eq 0 ]; then
    _dump_drop_previous
  elif [ -e "$LRCLIB_DATA/db.sqlite3.previous" ]; then
    touch "$LRCLIB_DATA/db.sqlite3.previous.kept"
  fi
  if [ "${1:-}" != --keep-download ] && [ "$keep_download" -eq 0 ]; then
    rm -f "$LRCLIB_DATA/dl/$dump_name"
  fi
  rm -f "$LRCLIB_DATA/.plyrix-update.journal"
}

# Finish or undo a swap a crash or a reboot left part way, before anything else runs. The journal
# names the step that was under way when the run ended:
#   stopping                       nothing was moved: LRCLIB is started again
#   moving_old, moving_new, starting  a rename may be half done: the previous set is put back,
#                                  LRCLIB is started on it, and the dump is recorded rolled back
#   swapped                        LRCLIB serves the new dump: the live record is finished
#   live, rolled_back              finished: the journal is removed
# LRCLIB is never started with no db.sqlite3, which would make it create an empty database: a
# first load that ended before its database was in place only loses its journal. A first load
# whose new database is in place, with no previous one, keeps it and starts LRCLIB on it, and sets
# dump_recovered_first to its name. A swap finished at swapped sets dump_recovered_live. Then a
# kept previous set past LRCLIB_KEEP_PREVIOUS_DAYS is removed.
dump_recover() {
  local journal=$LRCLIB_DATA/.plyrix-update.journal db=$LRCLIB_DATA/db.sqlite3
  local line step name extra
  if [ -e "$journal" ]; then
    line=$(head -c 512 "$journal")
    read -r step name extra <<<"$line" || true
    if [ -n "${extra:-}" ] || [ "$(wc -l <"$journal")" -ne 1 ] ||
      ! [[ ${name:-} =~ ^lrclib-db-dump-[0-9]{8}T[0-9]{6}Z\.sqlite3\.gz$ ]]; then
      dump_fail "$journal holds a line this script doesn't know, so nothing was touched; check LRCLIB_DATA by hand"
    fi
    dump_name=$name
    case $step in
      live | rolled_back) rm -f "$journal" ;;
      stopping | moving_old | moving_new | starting)
        if [ ! -e "$db" ] && [ ! -e "$db.previous" ]; then
          # A first load ended before its database was in place, so there is nothing to put
          # back or start. The journal goes, or every later run, setup included, stops here.
          rm -f "$db.staged" "$journal"
          if [ "$dump_tag" = lrclib-setup ]; then
            echo "$dump_tag: the last load of $name stopped at $step before any database was in place, so it is loaded again" >&2
          else
            dump_fail "the last load of $name stopped at $step before any database was in place, so LRCLIB was not started; load a dump with lrclib-setup.sh"
          fi
        else
          _dump_recover_unfinished "$step"
        fi
        ;;
      swapped)
        echo "$dump_tag: the last update swapped in $name but didn't record it, so it is recorded now"
        plyrix_wait "${plyrix_cmd[@]}" lrclib-record "$name" --status live ||
          dump_fail "LRCLIB serves $name, but plyrix couldn't record it; run: ${PLYRIX:-plyrix} lrclib-record $name --status live"
        dump_step live
        dump_cleanup
        dump_recovered_live=$name
        ;;
      *) dump_fail "$journal holds a line this script doesn't know, so nothing was touched; check LRCLIB_DATA by hand" ;;
    esac
  elif [ -e "$db.staged" ]; then
    # With no journal, no swap began, so a staged database is left from a run killed between
    # unpack and swap (SIGKILL skips the exit trap). It would hold the space the next one needs.
    echo "$dump_tag: removing db.sqlite3.staged, left by a run that ended before its swap"
    rm -f "$db.staged"
  fi
  _dump_expire_kept
}

_dump_recover_unfinished() {
  local step=$1 db=$LRCLIB_DATA/db.sqlite3 journal=$LRCLIB_DATA/.plyrix-update.journal
  rm -f "$db.staged"
  if [ "$step" = stopping ]; then
    echo "$dump_tag: the last update to $dump_name stopped before it moved anything, so LRCLIB is started again"
    "${start_cmd[@]}" || dump_fail "LRCLIB didn't start; start it with: $start_shown"
    rm -f "$journal"
    return 0
  fi
  if [ "$step" != moving_old ] && [ ! -e "$db.previous" ] && [ -e "$db" ]; then
    # A first load: the new database is in place and there is no previous one to go back to.
    # LRCLIB serves it once started, so it isn't recorded rolled back. It isn't recorded live
    # either, as its search index was never seen ready.
    echo "$dump_tag: the last load of $dump_name stopped at $step with no previous database, so the new one stays in place and LRCLIB is started on it"
    "${stop_cmd[@]}" || true
    "${start_cmd[@]}" || dump_fail "the new database is in place, but LRCLIB didn't start; start it with: $start_shown"
    rm -f "$journal"
    # Setup reads this, so a re-run for the same dump waits for it rather than loading it again.
    dump_recovered_first=$dump_name
    if [ "$dump_tag" != lrclib-setup ]; then
      echo "$dump_tag: once its search index is ready, record it with: ${PLYRIX:-plyrix} lrclib-record $dump_name --status live" >&2
    fi
    return 0
  fi
  echo "$dump_tag: the last update to $dump_name stopped at $step, so the previous database is put back"
  "${stop_cmd[@]}" || true
  if [ "$step" = moving_old ]; then
    # Nothing new is in place yet: each old file is either still at its name or renamed aside.
    _dump_restore --old-only
  elif [ -e "$db.previous" ]; then
    _dump_restore
  fi
  "${start_cmd[@]}" || dump_fail "the previous database is back in place, but LRCLIB didn't start; start it with: $start_shown"
  dump_step rolled_back
  plyrix_wait "${plyrix_cmd[@]}" lrclib-record "$dump_name" --status rolled-back >/dev/null ||
    echo "$dump_tag: plyrix couldn't record $dump_name as rolled back" >&2
}

# Remove a kept previous set once its marker is LRCLIB_KEEP_PREVIOUS_DAYS days old.
_dump_expire_kept() {
  local marker=$LRCLIB_DATA/db.sqlite3.previous.kept age
  [ -e "$marker" ] || return 0
  age=$(($(date +%s) - $(stat -c %Y "$marker")))
  if ((age >= keep_days * 86400)); then
    _dump_drop_previous
    echo "$dump_tag: the previous database was kept $keep_days days, so it was deleted"
  fi
}

# Remove a kept previous set now, as a newer dump is about to be loaded, and the live database is
# then what a rollback goes back to.
dump_drop_kept() {
  if [ -e "$LRCLIB_DATA/db.sqlite3.previous.kept" ]; then
    _dump_drop_previous
    echo "$dump_tag: the kept previous database was deleted, as a newer dump is being loaded"
  fi
}

# Run a plyrix command, and while it exits 75, as another plyrix run holds the state database,
# wait LRCLIB_POLL_SECONDS and run it again, for up to LRCLIB_STATE_WAIT seconds. Returns its
# exit code, or 75 once the wait is over.
plyrix_wait() {
  local rc started=$SECONDS
  while :; do
    rc=0
    "$@" || rc=$?
    [ "$rc" -eq 75 ] || return "$rc"
    ((SECONDS - started < state_wait)) || return 75
    sleep "$poll"
  done
}
