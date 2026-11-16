#!/bin/sh
# plyrix's container entrypoint.
#
# With arguments, it runs plyrix with them, so `docker compose run --rm plyrix doctor` works. With
# none, it is cron mode: one crontab line runs `plyrix run`, then `plyrix plex-refresh`, on
# CRON_SCHEDULE (default @weekly), under supercronic. The state database's lock keeps the two
# steps apart, and plex-refresh exits 0 when Plex isn't configured. ionice is best effort, as some
# filesystems ignore IO priority and some sandboxes refuse it.
#
# With DRAIN_SCHEDULE set, cron mode also runs `plyrix drain` on that schedule, to empty the queue
# the Lidarr shim fills. drain exits 0 with nothing queued, and while the weekly run holds the
# state database.
set -eu

if [ "$#" -gt 0 ]; then
  exec plyrix "$@"
fi

schedule=${CRON_SCHEDULE:-@weekly}
drain=${DRAIN_SCHEDULE:-}
crontab=${CRONTAB_FILE:-/tmp/plyrix.crontab}

# A line break in the schedule would add a cron line of its own.
newline='
'
carriage_return=$(printf '\r')
case "$schedule" in
  *"$newline"* | *"$carriage_return"*)
    echo "plyrix-entrypoint: CRON_SCHEDULE must be a single line" >&2
    exit 2
    ;;
esac
case "$drain" in
  *"$newline"* | *"$carriage_return"*)
    echo "plyrix-entrypoint: DRAIN_SCHEDULE must be a single line" >&2
    exit 2
    ;;
esac

# -t: where setting an IO priority is refused (a seccomp profile, gVisor), still run plyrix.
printf '%s ionice -t -c3 nice -n19 plyrix run; plyrix plex-refresh\n' "$schedule" > "$crontab"
if [ -n "$drain" ]; then
  printf '%s ionice -t -c3 nice -n19 plyrix drain\n' "$drain" >> "$crontab"
fi
supercronic -test "$crontab"
exec supercronic "$crontab"
