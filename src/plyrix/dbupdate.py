"""The read-only and state-only half of an LRCLIB update: check, verify and record.

The host script scripts/lrclib-update.sh does everything that renames, deletes or starts
anything (D-13), and runs these through the CLI. Like hook.py, this decides no exit code: each
function returns a typed result, or raises db.StateError, and the CLI maps that to output.
"""

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from plyrix import config, dumps, matching
from plyrix.doctor import DUMP_NAME
from plyrix.state import db, lrclib_db

NONE = "none"
UPDATE = "update"
REFUSED = "refused"
UNKNOWN = "unknown"
LIVE = "live"
STATUS_REFUSED = "refused"
STATUS_ROLLED_BACK = "rolled-back"
STATUSES = (LIVE, STATUS_REFUSED, STATUS_ROLLED_BACK)
# How the ledger stores each status the CLI takes.
_STORED = {LIVE: "live", STATUS_REFUSED: "refused", STATUS_ROLLED_BACK: "rolled_back"}
# Statuses a newer listed dump can have that keep a check from trying it again by itself.
_NOT_RETRIED = {"refused": "refused", "rolled_back": "rolled back"}
NotServable = lrclib_db.NotServable


COMPLETE = "complete"
UNSTABLE = "unstable"
NO_DIFF = "none"


@dataclass(frozen=True)
class DiffResult:
    """A diff's state, how many changed LRCLIB tracks it recorded, and the dump it diffed from."""

    state: str
    changed: int
    previous: str | None


@dataclass(frozen=True)
class CheckResult:
    """A check's verdict, and the newer dump's name and size when there is one.

    verdict is NONE, UPDATE, REFUSED or UNKNOWN. message is a line to print on stderr, holding no
    response text, or None. An update also carries the decompressed size and the user_version
    from the dump's header.
    """

    verdict: str
    name: str | None = None
    size: int | None = None
    message: str | None = None
    unpacked: int | None = None
    user_version: int | None = None


def usable_listing_url(url: str) -> bool:
    """Say whether a listing URL is http or https with a host."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def live_name(settings: config.Settings) -> str | None:
    """Return the dump LRCLIB serves: the one recorded live, else dump_id if it names a dump.

    The ledger is read through a memory copy, so nothing is created or locked.
    """
    conn = db.connect_snapshot(settings.state_db)
    try:
        return _live_name(settings, conn)
    finally:
        conn.close()


def _live_name(settings: config.Settings, conn: db.Connection) -> str | None:
    live = db.live_dump(conn)
    if live is not None:
        return live.name
    if DUMP_NAME.fullmatch(settings.dump_id):
        return settings.dump_id
    return None


def check(
    settings: config.Settings, listing_url: str, base_url: str, retry: bool = False
) -> CheckResult:
    """Say whether the listing names a dump newer than the live one that LRCLIB can serve.

    Newer means a later time in the name: neither the upload time nor the listing's order counts.
    With no live dump known, the verdict is UNKNOWN, as a dump LRCLIB already holds must not be
    downloaded again by guesswork. A newer dump's first 64 KB are read to check its SQLite
    version against the pinned LRCLIB's before anything else is fetched (D-06, D-15).
    """
    conn = db.connect_snapshot(settings.state_db)
    try:
        return _check(settings, conn, listing_url, base_url, retry)
    finally:
        conn.close()


def _check(
    settings: config.Settings, conn: db.Connection, listing_url: str, base_url: str, retry: bool
) -> CheckResult:
    live = _live_name(settings, conn)
    if live is None:
        return CheckResult(UNKNOWN)
    session = dumps.new_session()
    found = dumps.read_listing(session, listing_url)
    if found.newest is None:
        return CheckResult(NONE, message=f"plyrix: {found.problem}, so there is no update")
    name = found.newest.key
    if dumps.dump_time(name) <= dumps.dump_time(live):
        message = None
        if found.truncated:
            message = "plyrix: the listing was cut short and names no newer dump"
        return CheckResult(NONE, message=message)
    seen = db.dump_row(conn, name)
    if seen is not None and seen.status in _NOT_RETRIED and not retry:
        how = _NOT_RETRIED[seen.status]
        return CheckResult(
            REFUSED,
            name,
            message=f"plyrix: {name} was {how} on {seen.recorded_at[:10]}, so it isn't tried "
            "again by itself; LRCLIB_UPDATE_RETRY=1 tries it again",
        )
    header = dumps.probe_header(session, f"{base_url.rstrip('/')}/{name}")
    if header == dumps.NOT_SQLITE:
        return CheckResult(
            REFUSED,
            name,
            message=f"plyrix: {name} doesn't hold an SQLite database, so it was refused; "
            "nothing was downloaded",
        )
    if isinstance(header, str):
        return CheckResult(
            NONE,
            message=f"plyrix: the newest dump's header couldn't be read ({header}), so no "
            "update was tried",
        )
    versions = lrclib_db.LRCLIB_USER_VERSIONS
    if header.user_version not in versions:
        return CheckResult(
            REFUSED,
            name,
            message=f"plyrix: {name} needs a newer LRCLIB than the pinned build: its "
            f"user_version is {header.user_version}, and the build at "
            f"{lrclib_db.LRCLIB_PIN[:7]} serves {versions.start} to {versions.stop - 1}, so "
            "nothing was downloaded; move the LRCLIB pin in compose.yaml to update",
        )
    return CheckResult(
        UPDATE, name, found.newest.size, unpacked=header.unpacked, user_version=header.user_version
    )


def verify(
    path: str | os.PathLike[str], name: str | None = None, quick_check: bool = True
) -> lrclib_db.Verified:
    """Check a staged database read-only, against the time in its name when given.

    Raises lrclib_db.NotServable.
    """
    name_time = None if name is None else dumps.dump_time(name)
    return lrclib_db.verify(path, name_time=name_time, quick_check=quick_check)


def diff(
    settings: config.Settings,
    name: str,
    staged: str | os.PathLike[str],
    previous_db: str | os.PathLike[str] | None = None,
) -> DiffResult:
    """Record which LRCLIB tracks are new or have new lyrics in the staged dump (D-08, D-16).

    The previous dump is the live one, else dump_id when it names another dump, which is then
    recorded live. Its watermarks say where the diff starts. When it has none, as after setup,
    they are read from previous_db, the database LRCLIB serves, and trusted only when that read
    is safe and no higher than the staged maxes. The staged dump's own watermarks are stored for
    the next update. Changed tracks are kept as ids and keys, and the diff stays on the staged row
    until the live record promotes it. Raises lrclib_db.NotServable for a staged file that can't
    be read, and db.StateError.
    """
    marks = lrclib_db.watermarks(staged)
    digest = lrclib_db.id_digest(staged, marks.max_track_id)
    conn = db.connect(settings.state_db)
    try:
        previous = _previous_dump(settings, conn, name)
        if previous is not None and not _has_marks(previous) and previous_db is not None:
            _bootstrap(conn, previous, previous_db, marks)
            previous = db.dump_row(conn, previous.name)
        db.drop_staged(conn)
        db.stage_dump(
            conn,
            name,
            dumps.dump_time(name).isoformat(),
            None if previous is None else previous.name,
            marks.user_version,
            marks.max_track_id,
            marks.max_lyrics_id,
            marks.newest_created_at,
            digest,
        )
        if (
            previous is None
            or previous.max_track_id is None
            or previous.max_lyrics_id is None
            or previous.id_digest is None
        ):
            db.finish_diff(conn, name, NO_DIFF)
            return DiffResult(NO_DIFF, 0, None if previous is None else previous.name)
        stable = (
            (marks.max_track_id or 0) >= previous.max_track_id
            and (marks.max_lyrics_id or 0) >= previous.max_lyrics_id
            and lrclib_db.id_digest(staged, previous.max_track_id) == previous.id_digest
        )
        if not stable:
            db.finish_diff(conn, name, UNSTABLE)
            return DiffResult(UNSTABLE, 0, previous.name)
        count = 0
        for batch in lrclib_db.changed(staged, previous.max_track_id, previous.max_lyrics_id):
            rows = [
                (track_id, *matching.lookup_keys(artist, title))
                for track_id, artist, title in batch
            ]
            db.add_changes(conn, name, rows)
            count += len(rows)
        db.finish_diff(conn, name, COMPLETE)
        return DiffResult(COMPLETE, count, previous.name)
    finally:
        conn.close()


def _previous_dump(settings: config.Settings, conn: db.Connection, name: str) -> db.DumpRow | None:
    live = db.live_dump(conn)
    if live is None and DUMP_NAME.fullmatch(settings.dump_id) and settings.dump_id != name:
        db.record_live(conn, settings.dump_id, dumps.dump_time(settings.dump_id).isoformat(), None)
        live = db.live_dump(conn)
    if live is None or live.name == name:
        return None
    return live


def _has_marks(row: db.DumpRow) -> bool:
    return (
        row.max_track_id is not None and row.max_lyrics_id is not None and row.id_digest is not None
    )


def _bootstrap(
    conn: db.Connection,
    previous: db.DumpRow,
    previous_db: str | os.PathLike[str],
    staged: lrclib_db.Watermarks,
) -> None:
    """Store the previous dump's watermarks read from the database LRCLIB serves, if trusted."""
    found = lrclib_db.bootstrap_marks(previous_db, dumps.dump_time(previous.name))
    if found is None:
        return
    marks, digest = found
    if marks.max_track_id is None or marks.max_lyrics_id is None:
        return
    if staged.max_track_id is None or staged.max_lyrics_id is None:
        return
    # Maxes above the staged dump's can't be the previous dump's: a torn or local read.
    if marks.max_track_id > staged.max_track_id or marks.max_lyrics_id > staged.max_lyrics_id:
        return
    db.set_watermarks(
        conn,
        previous.name,
        marks.user_version,
        marks.max_track_id,
        marks.max_lyrics_id,
        marks.newest_created_at,
        digest,
    )


def record(settings: config.Settings, name: str, status: str) -> str:
    """Record a dump's status in the ledger, and return the line to print."""
    if status not in STATUSES:
        raise ValueError("not a status a dump can be recorded with")
    dump_time = dumps.dump_time(name).isoformat()
    conn = db.connect(settings.state_db)
    try:
        if status == LIVE:
            db.record_live(conn, name, dump_time, _now())
        else:
            db.mark_status(conn, name, _STORED[status], dump_time)
    finally:
        conn.close()
    if status == LIVE:
        return f"plyrix: {name} is recorded as LRCLIB's live dump"
    how = _NOT_RETRIED[_STORED[status]]
    return f"plyrix: {name} is recorded as {how}, so it isn't tried again by itself"


def _now() -> str:
    return datetime.now(UTC).isoformat()
