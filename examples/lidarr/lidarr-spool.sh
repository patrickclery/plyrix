#!/bin/sh
# Hands one Lidarr event to plyrix through a folder both containers mount, for a Lidarr container
# that has no Python and no docker CLI. It never blocks or fails an import: plyrix's container runs
# `plyrix drain` on its own schedule (DRAIN_SCHEDULE) and does the work there.
#
# Set this file as the Path of Lidarr's Custom Script connection, ticked for On Release Import and
# On Upgrade. The folder is PLYRIX_LIDARR_SPOOL in Lidarr's environment, /plyrix-spool by default.
# Lidarr's user must be able to write it, and plyrix's user to read and remove files in it.
#
# Lidarr 3.1 on Linux sets its variables in lowercase (lidarr_eventtype), though its source spells
# them Lidarr_EventType, so both spellings are read. Each event becomes one file of KEY=VALUE
# records under the mixed-case names, each ending in a NUL byte, holding Lidarr's own values. It
# is written under a dot-name, which plyrix skips, then renamed, so plyrix sees all of it or none
# of it. Lidarr's Test button exits 1 when the folder can't be written, so it shows there.

spool=${PLYRIX_LIDARR_SPOOL:-/plyrix-spool}

case "${Lidarr_EventType:-${lidarr_eventtype:-}}" in
  Test)
    if [ -d "$spool" ] && [ -w "$spool" ] && [ -x "$spool" ]; then
      exit 0
    fi
    echo "plyrix: the spool folder in PLYRIX_LIDARR_SPOOL is not a folder Lidarr can write" >&2
    exit 1
    ;;
  AlbumDownload) ;;
  *) exit 0 ;;
esac

# The same name plyrix gives a file: a UTC stamp, a dash and 8 hex characters.
random=$(od -An -N4 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')
case "$random" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
  *) random=$(printf '%08d' "$$" | cut -c1-8) ;;
esac
name="$(date -u +%Y%m%dT%H%M%SZ)-$random"
tmp="$spool/.$name.tmp"

write_event() {
  for key in Lidarr_EventType Lidarr_AddedTrackPaths Lidarr_DeletedPaths Lidarr_Artist_Path \
    Lidarr_Album_Id; do
    lower=$(printf '%s' "$key" | tr 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' 'abcdefghijklmnopqrstuvwxyz')
    eval "value=\${$key:-\${$lower-}}"
    printf '%s=%s\0' "$key" "$value" || return 1
  done
}

if (write_event > "$tmp") 2>/dev/null && mv "$tmp" "$spool/$name.event" 2>/dev/null; then
  exit 0
fi
rm -f "$tmp" 2>/dev/null
echo "plyrix: this album couldn't be queued in PLYRIX_LIDARR_SPOOL, so the next scheduled run covers it" >&2
exit 0
