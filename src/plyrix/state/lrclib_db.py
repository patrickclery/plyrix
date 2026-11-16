"""Read-only access to LRCLIB's own databases: a staged dump, or the old one after LRCLIB stops.

Every open is a `mode=ro&immutable=1` URI. immutable tells SQLite the file can't change, so it
takes no lock and makes no -wal, -shm or -journal beside it. That is only correct for a file
nothing writes, which holds for a staged dump before LRCLIB starts on it and for the old database
once LRCLIB has stopped. It is also the only open that works in the plyrix container, which mounts
LRCLIB's data read-only, as a WAL database opened with mode=ro alone wants to create its -shm.

Nothing here selects a lyric column, and no reason given back holds text from the database.
"""

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import NamedTuple
from urllib.parse import quote

# The LRCLIB build compose.yaml pins. tests/test_packaging.py holds the two equal.
LRCLIB_PIN = "e1d863afc430392c8b2f97bea72139292d498432"
# The user_version values the pinned server serves. It migrates with rusqlite_migration, which
# counts the 8 migrations at the pin in PRAGMA user_version, applies any missing ones on start,
# and refuses to start at all past the last one (DatabaseTooFarAhead). 0 would make it try to
# create its tables over a full database.
LRCLIB_USER_VERSIONS = range(1, 9)
# Each table's columns plyrix's diff and the pinned server's queries rely on.
_COLUMNS = {
    "tracks": (
        "id",
        "name",
        "name_lower",
        "artist_name",
        "artist_name_lower",
        "duration",
        "last_lyrics_id",
    ),
    "lyrics": ("id", "track_id", "created_at", "has_synced_lyrics", "instrumental"),
}
_INDEX = "idx_tracks_last_lyrics_id"
# How far past the time in a dump's name its newest record may be before that is worth a line.
_CREATED_SLACK = timedelta(days=1)


class NotServable(Exception):
    """The database can't be served by the pinned LRCLIB, for reason, which holds no data."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Verified(NamedTuple):
    """What a database that passed verify holds.

    Its version, a track id to probe the server with, its newest lyric's created_at as SQLite
    holds it, and whether that is more than a day after the time in the dump's name.
    """

    user_version: int
    probe_id: int
    newest_created_at: str | None = None
    created_after_name: bool = False


def open_read_only(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open an LRCLIB database through an immutable read-only URI.

    realpath leaves no leading // for a URI to take for an authority.
    """
    uri = f"file:{quote(os.path.realpath(os.fsencode(path)))}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True, isolation_level=None)


def verify(
    path: str | os.PathLike[str],
    *,
    name_time: datetime | None = None,
    quick_check: bool = True,
    trace: Callable[[str], None] | None = None,
) -> Verified:
    """Check that the pinned LRCLIB can serve a database, or raise NotServable saying why.

    The checks run cheapest first: the header and its version, the tables and columns, the index
    the diff reads, a track to probe, then PRAGMA quick_check, which reads every page. name_time
    is the time in the dump's name, for the created_at cross-check (D-07). trace, for tests, sees
    every statement run.
    """
    try:
        conn = open_read_only(path)
    except sqlite3.Error:
        raise NotServable("it can't be opened as an SQLite database") from None
    if trace is not None:
        conn.set_trace_callback(trace)
    try:
        return _verify(conn, name_time, quick_check)
    except sqlite3.DatabaseError:
        raise NotServable("it isn't an SQLite database, or it is damaged") from None
    finally:
        conn.close()


def _verify(conn: sqlite3.Connection, name_time: datetime | None, quick_check: bool) -> Verified:
    # The first read checks the header, and a file that isn't SQLite fails it.
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version not in LRCLIB_USER_VERSIONS:
        raise NotServable(
            f"its user_version is {version}, and the pinned LRCLIB serves only "
            f"{LRCLIB_USER_VERSIONS.start} to {LRCLIB_USER_VERSIONS.stop - 1}"
        )
    for table, wanted in _COLUMNS.items():
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not columns:
            raise NotServable(f"it has no {table} table")
        missing = [column for column in wanted if column not in columns]
        if missing:
            noun = "column" if len(missing) == 1 else "columns"
            raise NotServable(f"its {table} table has no {', '.join(missing)} {noun}")
    try:
        conn.execute(
            "SELECT 1 FROM tracks INDEXED BY idx_tracks_last_lyrics_id "
            "WHERE last_lyrics_id > 0 LIMIT 1"
        ).fetchall()
    except sqlite3.OperationalError:
        raise NotServable(f"it has no {_INDEX} index") from None
    probe = conn.execute("SELECT max(id) FROM tracks").fetchone()[0]
    if probe is None:
        raise NotServable("it holds no tracks")
    newest = conn.execute("SELECT max(created_at) FROM lyrics").fetchone()[0]
    newest_created_at = None if newest is None else str(newest)
    if quick_check:
        try:
            result = conn.execute("PRAGMA quick_check").fetchall()
        except sqlite3.DatabaseError:
            result = []
        if result != [("ok",)]:
            raise NotServable("it fails SQLite's quick_check")
    after = name_time is not None and _later_than(newest_created_at, name_time + _CREATED_SLACK)
    return Verified(version, int(probe), newest_created_at, after)


def _later_than(created_at: str | None, limit: datetime) -> bool:
    """Say whether a created_at, read leniently, is after limit. One that can't be read isn't."""
    if created_at is None:
        return False
    try:
        when = datetime.fromisoformat(created_at.strip().replace(" ", "T", 1))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when > limit


# What a dump holds at its highest ids, so the next update can tell what is new without a second
# copy of this database.
class Watermarks(NamedTuple):
    user_version: int
    max_track_id: int | None
    max_lyrics_id: int | None
    newest_created_at: str | None


_DIGEST_SAMPLES = 64
# The diff's two queries. Two, not one: a UNION of them planned a full scan of tracks.
_RELYRICKED = (
    "SELECT id, artist_name, name FROM tracks INDEXED BY idx_tracks_last_lyrics_id "
    "WHERE last_lyrics_id > ? AND id <= ?"
)
_NEW_TRACKS = "SELECT id, artist_name, name FROM tracks WHERE id > ?"


def watermarks(path: str | os.PathLike[str]) -> Watermarks:
    """Return a database's version, highest track and lyric ids and newest lyric's created_at."""
    conn = _open_checked(path)
    try:
        return _watermarks(conn)
    except sqlite3.Error:
        raise NotServable("it can't be read as an LRCLIB database") from None
    finally:
        conn.close()


def _watermarks(conn: sqlite3.Connection) -> Watermarks:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    track = conn.execute("SELECT max(id) FROM tracks").fetchone()[0]
    lyric = conn.execute("SELECT max(id) FROM lyrics").fetchone()[0]
    newest = conn.execute("SELECT max(created_at) FROM lyrics").fetchone()[0]
    return Watermarks(
        version,
        None if track is None else int(track),
        None if lyric is None else int(lyric),
        None if newest is None else str(newest),
    )


def id_digest(path: str | os.PathLike[str], upto: int | None) -> str | None:
    """Return a digest of 64 tracks spread evenly over ids 1 to upto, or None for no ids.

    Another database gives the same digest for the same upto only if those rows are unchanged,
    which is how a later diff knows LRCLIB kept its ids (D-16).
    """
    if not upto or upto < 1:
        return None
    conn = _open_checked(path)
    try:
        return _id_digest(conn, upto)
    except sqlite3.Error:
        raise NotServable("it can't be read as an LRCLIB database") from None
    finally:
        conn.close()


def _id_digest(conn: sqlite3.Connection, upto: int) -> str:
    sampled = []
    for i in range(1, _DIGEST_SAMPLES + 1):
        target = max(1, i * upto // _DIGEST_SAMPLES)
        row = conn.execute(
            "SELECT id, name_lower, artist_name_lower, duration FROM tracks "
            "WHERE id >= ? AND id <= ? ORDER BY id LIMIT 1",
            (target, upto),
        ).fetchone()
        sampled.append(None if row is None else list(row))
    return hashlib.sha256(json.dumps(sampled).encode()).hexdigest()


def changed(
    path: str | os.PathLike[str], after_track_id: int, after_lyrics_id: int, batch: int = 10_000
) -> Iterator[list[tuple[int, str | None, str | None]]]:
    """Yield, in batches, every track that is new or has new lyrics since the given watermarks.

    Each is (id, artist_name, name). Old tracks with new lyrics come from the last_lyrics_id
    index, and new tracks from the primary key, so each query is an index range scan on this
    database alone, and the two never overlap. updated_at can't drive this: a new lyric row moves
    last_lyrics_id through a trigger and leaves the track's updated_at alone.
    """
    conn = _open_checked(path)
    try:
        for sql, params in (
            (_RELYRICKED, (after_lyrics_id, after_track_id)),
            (_NEW_TRACKS, (after_track_id,)),
        ):
            cursor = conn.execute(sql, params)
            while rows := cursor.fetchmany(batch):
                yield [(int(row[0]), _text(row[1]), _text(row[2])) for row in rows]
    except sqlite3.Error:
        raise NotServable("it can't be read as an LRCLIB database") from None
    finally:
        conn.close()


def plans(path: str | os.PathLike[str], after_track_id: int, after_lyrics_id: int) -> list[str]:
    """Return the query plan details of the diff's two queries, for tests."""
    conn = _open_checked(path)
    try:
        details = []
        for sql, params in (
            (_RELYRICKED, (after_lyrics_id, after_track_id)),
            (_NEW_TRACKS, (after_track_id,)),
        ):
            rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
            details.append("; ".join(str(row[-1]) for row in rows))
        return details
    finally:
        conn.close()


def bootstrap_marks(
    path: str | os.PathLike[str], dump_time: datetime
) -> tuple[Watermarks, str] | None:
    """Return the watermarks and id digest of the database LRCLIB is serving, or None.

    This is the only place plyrix opens a database another process may be writing: the live
    db.sqlite3, read only when the dump it holds has no stored watermarks, as after setup. A torn
    read could give max ids that are too high, and a diff from those would miss changes, so:
    SQLite's normal reader (mode=ro, without immutable, which honours the WAL and LRCLIB's locks
    and writes no database page) is tried first. Where that can't open, as in the container's
    read-only mount, two immutable reads must agree. Either way the rows at the max ids must
    exist and have been created no later than a day after dump_time, as rows newer than the dump
    can't be its own. Anything else gives None, which means the diff retries everything.
    """
    found = _read_marks_safely(path)
    if found is None:
        first, second = _read_marks_immutably(path), _read_marks_immutably(path)
        if first is None or first != second:
            return None
        found = first
    marks, digest, created = found
    limit = dump_time + _CREATED_SLACK
    for when in created:
        if when is None or not _parses(when) or _later_than(when, limit):
            return None
    return marks, digest


_Read = tuple[Watermarks, str, tuple[str | None, str | None]]


def _read_marks_safely(path: str | os.PathLike[str]) -> _Read | None:
    uri = f"file:{quote(os.path.realpath(os.fsencode(path)))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)
    except sqlite3.Error:
        return None
    try:
        conn.execute("BEGIN")
        found = _read_marks(conn)
        conn.execute("COMMIT")
        return found
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _read_marks_immutably(path: str | os.PathLike[str]) -> _Read | None:
    try:
        conn = open_read_only(path)
    except sqlite3.Error:
        return None
    try:
        return _read_marks(conn)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _read_marks(conn: sqlite3.Connection) -> _Read | None:
    marks = _watermarks(conn)
    if marks.max_track_id is None or marks.max_lyrics_id is None:
        return None
    track = conn.execute(
        "SELECT created_at FROM tracks WHERE id = ?", (marks.max_track_id,)
    ).fetchone()
    lyric = conn.execute(
        "SELECT created_at FROM lyrics WHERE id = ?", (marks.max_lyrics_id,)
    ).fetchone()
    if track is None or lyric is None:
        return None
    digest = _id_digest(conn, marks.max_track_id)
    return marks, digest, (_text(track[0]), _text(lyric[0]))


def _open_checked(path: str | os.PathLike[str]) -> sqlite3.Connection:
    try:
        return open_read_only(path)
    except sqlite3.Error:
        raise NotServable("it can't be opened as an SQLite database") from None


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _parses(created_at: str) -> bool:
    try:
        datetime.fromisoformat(created_at.strip().replace(" ", "T", 1))
    except ValueError:
        return False
    return True
