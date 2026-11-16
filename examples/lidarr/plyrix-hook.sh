#!/bin/sh
# Runs plyrix on the album Lidarr just imported, for plyrix installed where Lidarr runs: inside
# Lidarr's container, or on the host beside a Lidarr installed there.
#
# Lidarr's Custom Script connection takes no arguments, so this file is the connection's Path,
# ticked for On Release Import and On Upgrade. If plyrix isn't on Lidarr's PATH, give its full
# path here; `uv tool install plyrix` puts it in the installing user's ~/.local/bin. Settings come
# from PLYRIX_ variables or plyrix's config file in Lidarr's environment.
#
# plyrix itself exits 0 for every import. If it can't run at all, as when it isn't installed, this
# queues the import in PLYRIX_LIDARR_SPOOL, in the format lidarr-spool.sh writes, for the next
# `plyrix drain`, and still exits 0, so Lidarr's import never fails. Set PLYRIX_LIDARR_SPOOL in
# Lidarr's environment for that. Lidarr's Test button still exits 1 when plyrix can't run.

plyrix=plyrix

event=${Lidarr_EventType:-${lidarr_eventtype:-}}

# The same as write_event in lidarr-spool.sh: Lidarr's five values under the mixed-case names,
# each record ending in a NUL byte, written under a dot-name, then renamed.
queue_event() {
  spool=${PLYRIX_LIDARR_SPOOL:-}
  if [ -z "$spool" ] || [ ! -d "$spool" ]; then
    return 1
  fi
  random=$(od -An -N4 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')
  case "$random" in
    [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
    *) random=$(printf '%08d' "$$" | cut -c1-8) ;;
  esac
  name="$(date -u +%Y%m%dT%H%M%SZ)-$random"
  tmp="$spool/.$name.tmp"
  if (write_event > "$tmp") 2>/dev/null && mv "$tmp" "$spool/$name.event" 2>/dev/null; then
    return 0
  fi
  rm -f "$tmp" 2>/dev/null
  return 1
}

write_event() {
  for key in Lidarr_EventType Lidarr_AddedTrackPaths Lidarr_DeletedPaths Lidarr_Artist_Path \
    Lidarr_Album_Id; do
    lower=$(printf '%s' "$key" | tr 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' 'abcdefghijklmnopqrstuvwxyz')
    eval "value=\${$key:-\${$lower-}}"
    printf '%s=%s\0' "$key" "$value" || return 1
  done
}

"$plyrix" lidarr-hook
status=$?
if [ "$status" -eq 0 ] || [ -z "$event" ]; then
  exit "$status"
fi
case "$event" in
  Test)
    if [ "$status" -ne 1 ]; then
      echo "plyrix: plyrix lidarr-hook couldn't run (exit $status), so check that plyrix is on Lidarr's PATH" >&2
    fi
    exit 1
    ;;
  AlbumDownload) ;;
  *) exit 0 ;;
esac
if queue_event; then
  echo "plyrix: plyrix lidarr-hook couldn't run (exit $status), so this album is queued in PLYRIX_LIDARR_SPOOL for the next drain" >&2
else
  echo "plyrix: plyrix lidarr-hook couldn't run (exit $status), and this album couldn't be queued in PLYRIX_LIDARR_SPOOL, so the next scheduled run covers it" >&2
fi
exit 0
