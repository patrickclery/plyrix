"""The ledger: which sidecars plyrix wrote, and their hashes. It holds no lyric text."""

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

from plyrix.domain import (
    DumpBasis,
    Kind,
    MissReason,
    Outcome,
    PlainReason,
    Reason,
    SyncedReason,
    TrackResult,
)

Connection = sqlite3.Connection

LOCK_WAIT_S = 0.5
_REASONS = {reason.value: reason for reason in (*MissReason, *PlainReason, *SyncedReason)}

# Append-only. Each entry is one migration, and PRAGMA user_version counts those applied.
SCHEMA = [
    """
CREATE TABLE runs(
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  version TEXT NOT NULL);
CREATE TABLE sidecars(
  root BLOB NOT NULL,
  rel_path BLOB NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('lrc','txt')),
  status TEXT NOT NULL CHECK(status IN ('intent','published')),
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL,
  lrclib_id INTEGER,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  PRIMARY KEY(root, rel_path));
""",
    """
ALTER TABLE sidecars ADD COLUMN claiming INTEGER NOT NULL DEFAULT 0 CHECK(claiming IN (0, 1));
""",
    # A row's identity is the sidecar's absolute real path, so it can't depend on which folder a
    # run was pointed at. If two old rows name one file, the newer run's row is the one kept.
    """
CREATE TABLE sidecars_by_path(
  path BLOB PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('lrc','txt')),
  status TEXT NOT NULL CHECK(status IN ('intent','published')),
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL,
  lrclib_id INTEGER,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  claiming INTEGER NOT NULL DEFAULT 0 CHECK(claiming IN (0, 1)));
INSERT OR IGNORE INTO sidecars_by_path
  SELECT
    CAST(CASE WHEN root = x'2f' THEN x'2f' || rel_path ELSE root || x'2f' || rel_path END AS BLOB),
    kind, status, sha256, size, lrclib_id, run_id, claiming
  FROM sidecars ORDER BY run_id DESC;
DROP TABLE sidecars;
ALTER TABLE sidecars_by_path RENAME TO sidecars;
""",
    # Each temp is recorded before it is made and forgotten once it is gone. It has its own table,
    # as it can outlive the sidecar's row, and a temp holds lyric text that must not be left behind.
    """
CREATE TABLE temps(path BLOB PRIMARY KEY);
""",
    # Each track's result, and the dump it is current against, as this can't be added later.
    # Reviewed against the database update requirements: dump_id compares by equality, so a rollback
    # looks like a change (DBUP-01); tracks.outcome with sidecars.origin and sha256 tell what to
    # retry and what may be upgraded (DBUP-02); refresh_pending is the durable record of what to
    # refresh (DBUP-03, D-18), and its default covers rows from before this migration; lrclib_id
    # says which record gave a result, for records removed upstream later (Phase 999.1). Tags are
    # stored only as a digest, and no row is ever pruned for a missing folder, as that is how an
    # unmount looks.
    """
ALTER TABLE runs ADD COLUMN dump_id TEXT NOT NULL DEFAULT '';
ALTER TABLE runs ADD COLUMN end_reason TEXT;
ALTER TABLE runs ADD COLUMN counts_json TEXT;
ALTER TABLE sidecars ADD COLUMN dump_id TEXT NOT NULL DEFAULT '';
ALTER TABLE sidecars ADD COLUMN origin TEXT NOT NULL DEFAULT 'written'
  CHECK(origin IN ('written','proved','timed'));
ALTER TABLE sidecars ADD COLUMN mtime_ns INTEGER;
ALTER TABLE sidecars ADD COLUMN refresh_pending INTEGER NOT NULL DEFAULT 1
  CHECK(refresh_pending IN (0,1));
CREATE TABLE tracks(
  path BLOB PRIMARY KEY,
  size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  tags_sha256 TEXT,
  outcome TEXT NOT NULL CHECK(outcome IN
    ('synced','plain','plain_skipped','instrumental','no_match','untagged','unreadable')),
  reason TEXT NOT NULL DEFAULT '',
  dump_id TEXT NOT NULL,
  matcher_version INTEGER NOT NULL,
  lrclib_id INTEGER,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  checked_at TEXT NOT NULL);
""",
    # What a replacement's intent took the place of, so a failure or a kill before the rename can
    # put the row back when the old file is still there. replaces_sha256 being set is what says a
    # replacement is under way, and the others mean nothing without it.
    """
ALTER TABLE sidecars ADD COLUMN replaces_sha256 TEXT;
ALTER TABLE sidecars ADD COLUMN replaces_size INTEGER;
ALTER TABLE sidecars ADD COLUMN replaces_origin TEXT;
ALTER TABLE sidecars ADD COLUMN replaces_lrclib_id INTEGER;
ALTER TABLE sidecars ADD COLUMN replaces_dump_id TEXT;
""",
    # The LRCLIB dumps an update has seen, and what each diff found, for the targeted retry
    # (DBUP-04, DBUP-05). It holds ids, 8-byte keys and the names of dumps, never lyric or tag
    # text. Every new tracks column is NULL on an old row, which means "unknown, so retry".
    """
ALTER TABLE tracks ADD COLUMN first_seen_at TEXT;
ALTER TABLE tracks ADD COLUMN release_year INTEGER;
ALTER TABLE tracks ADD COLUMN artist_key INTEGER;
ALTER TABLE tracks ADD COLUMN title_key INTEGER;
CREATE TABLE lrclib_dumps(
  name TEXT PRIMARY KEY,
  dump_time TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('staged','live','previous','refused','rolled_back')),
  size INTEGER,
  user_version INTEGER,
  max_track_id INTEGER,
  max_lyrics_id INTEGER,
  newest_created_at TEXT,
  id_digest TEXT,
  diff_from TEXT,
  diff_state TEXT NOT NULL DEFAULT 'none' CHECK(diff_state IN ('complete','unstable','none')),
  recorded_at TEXT NOT NULL,
  swapped_at TEXT);
CREATE UNIQUE INDEX lrclib_dumps_one_live ON lrclib_dumps(status) WHERE status = 'live';
CREATE TABLE lrclib_changes(
  dump TEXT NOT NULL REFERENCES lrclib_dumps(name) ON DELETE CASCADE,
  lrclib_id INTEGER NOT NULL,
  artist_key INTEGER,
  title_key INTEGER,
  PRIMARY KEY(dump, lrclib_id)) WITHOUT ROWID;
CREATE INDEX lrclib_changes_artist ON lrclib_changes(artist_key);
CREATE INDEX lrclib_changes_title ON lrclib_changes(title_key);
""",
    # The dump of the last finished walk of the whole library that didn't reach a soft or stale
    # row, as for a deleted, renamed or excluded file. NULL means the walk reached it. The prune
    # reads this dump in place of the row's own, so a row no run reaches can't keep old change
    # sets for good, and a row that has it set counts as affected by any update when it is back.
    """
ALTER TABLE tracks ADD COLUMN unseen_dump TEXT;
""",
]


class StateError(Exception):
    """The ledger can't be used, which stops the run with a message and an exit code."""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


class OwnedRow(NamedTuple):
    """A sidecar plyrix published."""

    sha256: str
    size: int
    origin: str
    mtime_ns: int | None


class TrackRow(NamedTuple):
    """One track's result, ready to store. An audio file's absolute real path is its identity."""

    path: bytes
    size: int
    mtime_ns: int
    tags_sha256: str | None
    outcome: Outcome
    reason: Reason | None
    dump_id: str
    matcher_version: int
    lrclib_id: int | None
    run_id: int
    # From the tags read in this run. None keeps what the row already holds.
    release_year: int | None = None
    artist_key: int | None = None
    title_key: int | None = None


class IntentRow(NamedTuple):
    """A sidecar a run meant to write, with no record yet that it did."""

    path: bytes
    kind: Kind
    sha256: str
    claiming: bool
    replaces_sha256: str | None


class DumpRow(NamedTuple):
    """One LRCLIB dump an update has seen, with every lrclib_dumps column, in table order."""

    name: str
    dump_time: str
    status: str
    size: int | None
    user_version: int | None
    max_track_id: int | None
    max_lyrics_id: int | None
    newest_created_at: str | None
    id_digest: str | None
    diff_from: str | None
    diff_state: str
    recorded_at: str
    swapped_at: str | None


def connect(path: Path) -> Connection:
    """Open the ledger, creating it and any missing parent folders, and apply migrations.

    The connection holds SQLite's exclusive lock until it closes or the process dies, so a second
    run can't interleave on this ledger. The lock comes first, so two first runs can't both try
    to apply the same migration. It waits LOCK_WAIT_S for that lock, since racing first runs can
    each see another's brief lock, and refusing on sight could leave no run holding it.
    """
    conn = None
    try:
        # pathlib applies mode only to the last folder of parents=True, so create them one by one.
        for parent in (*reversed(path.parent.parents), path.parent):
            parent.mkdir(mode=0o700, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None, timeout=LOCK_WAIT_S)
        conn.execute("PRAGMA foreign_keys=ON")
        # The mode comes after the lock: in this mode a run that fails to take the lock keeps its
        # shared lock until it closes, and racing runs can then block each other for the whole wait.
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("PRAGMA locking_mode=EXCLUSIVE")
        conn.execute("COMMIT")
        _migrate(conn)
    except StateError:
        _close_quietly(conn)
        raise
    except (sqlite3.Error, OSError) as error:
        _close_quietly(conn)
        raise _state_error(error) from None
    return conn


def connect_snapshot(path: Path) -> Connection:
    """Copy the ledger into memory with no write to its file, and apply migrations to the copy.

    A missing file gives an empty copy, and nothing is created. Changes to the copy go when it
    closes. A read-only connection can't roll back a killed writer's journal, so that stops with
    exit 2 and leaves the journal for a normal run.
    """
    conn = None
    try:
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON")
        if _ledger_exists(path):
            _copy_ledger(path, conn)
        _migrate(conn)
    except StateError:
        _close_quietly(conn)
        raise
    except (sqlite3.Error, OSError) as error:
        _close_quietly(conn)
        raise _state_error(error) from None
    return conn


def _ledger_exists(path: Path) -> bool:
    """Say whether there is a ledger to copy. Raises OSError when that can't be found out."""
    try:
        os.stat(path)
    except FileNotFoundError:
        return False
    return True


def _copy_ledger(path: Path, memory: Connection) -> None:
    """Copy a ledger's file into a connection.

    The read lock comes first, as backup waits for ever on a source another run has locked, where
    this read gives up after LOCK_WAIT_S. realpath leaves no leading // for a URI to take for an
    authority.
    """
    uri = f"file:{quote(os.path.realpath(os.fsencode(path)))}?mode=ro"
    source = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=LOCK_WAIT_S)
    try:
        source.execute("BEGIN")
        source.execute("SELECT count(*) FROM sqlite_master").fetchone()
        source.backup(memory)
        source.execute("COMMIT")
    except sqlite3.Error as error:
        if error.sqlite_errorname == "SQLITE_READONLY_ROLLBACK":
            raise StateError(
                "plyrix: a killed run left the state database mid-write, so do a normal run first"
            ) from None
        raise
    finally:
        source.close()


def _migrate(conn: Connection) -> None:
    """Apply the migrations a connection's database hasn't had yet."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > len(SCHEMA):
        raise StateError("plyrix: the state database is newer than this plyrix; upgrade plyrix")
    for index in range(version, len(SCHEMA)):
        conn.executescript(f"BEGIN;{SCHEMA[index]};PRAGMA user_version={index + 1};COMMIT;")


def _state_error(error: sqlite3.Error | OSError) -> StateError:
    """Say what a failure to open the ledger means for the user."""
    if isinstance(error, sqlite3.OperationalError) and "database is locked" in str(error):
        return StateError("another plyrix run is using the state database", exit_code=75)
    return StateError(_unusable(error))


def begin_run(conn: Connection, version: str, dump_id: str = "") -> int:
    """Record a new run and the dump it looks things up in, and return its id."""
    cursor = _execute(
        conn,
        "INSERT INTO runs(started_at, version, dump_id) VALUES(?, ?, ?)",
        (_now(), version, dump_id),
    )
    if cursor.lastrowid is None:
        raise StateError("plyrix: the state database did not record this run")
    return cursor.lastrowid


def finish_run(
    conn: Connection, run_id: int, end_reason: str | None = None, counts_json: str | None = None
) -> None:
    """Record when and why a run ended, and what it counted. A crash leaves no reason."""
    _execute(
        conn,
        "UPDATE runs SET finished_at = ?, end_reason = ?, counts_json = ? WHERE id = ?",
        (_now(), end_reason, counts_json, run_id),
    )


def add_intent(
    conn: Connection,
    path: bytes,
    kind: Kind,
    sha256: str,
    size: int,
    run_id: int,
    lrclib_id: int | None = None,
    dump_id: str = "",
    replacing: bool = False,
) -> None:
    """Commit what is about to be written, before it is written.

    An existing row means this write takes over from the file behind it, which has gone or is being
    upgraded or replaced. The row starts over, as a rewrite must not inherit an old origin, a
    cleared refresh or the old file's mtime. With replacing, it also keeps what it said of the file
    still there, for restore_replaced.
    """
    _execute(
        conn,
        """
        INSERT INTO sidecars(path, kind, status, sha256, size, lrclib_id, run_id, dump_id)
        VALUES(?, ?, 'intent', ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
          replaces_sha256 = CASE WHEN ? THEN sidecars.sha256 END,
          replaces_size = sidecars.size,
          replaces_origin = sidecars.origin,
          replaces_lrclib_id = sidecars.lrclib_id,
          replaces_dump_id = sidecars.dump_id,
          kind = excluded.kind,
          status = 'intent',
          claiming = 0,
          sha256 = excluded.sha256,
          size = excluded.size,
          lrclib_id = excluded.lrclib_id,
          run_id = excluded.run_id,
          dump_id = excluded.dump_id,
          origin = 'written',
          mtime_ns = NULL,
          refresh_pending = 1
        """,
        (path, kind.name.lower(), sha256, size, lrclib_id, run_id, dump_id, replacing),
    )


def restore_replaced(conn: Connection, path: bytes) -> None:
    """Put a replacement's row back, as the file it would have replaced is still there.

    The mtime isn't kept, so the next look at the file costs one hash. The refresh stays pending,
    which errs towards an extra one. A row that isn't replacing anything is left alone.
    """
    _execute(
        conn,
        """
        UPDATE sidecars SET
          status = 'published',
          claiming = 0,
          sha256 = replaces_sha256,
          size = replaces_size,
          origin = replaces_origin,
          lrclib_id = replaces_lrclib_id,
          dump_id = replaces_dump_id,
          mtime_ns = NULL,
          replaces_sha256 = NULL
        WHERE path = ? AND status = 'intent' AND replaces_sha256 IS NOT NULL
        """,
        (path,),
    )


def mark_claiming(conn: Connection, path: bytes) -> None:
    """Record that an empty file is about to hold this sidecar's name, before it is created."""
    _execute(conn, "UPDATE sidecars SET claiming = 1 WHERE path = ? AND status = 'intent'", (path,))


def clear_claiming(conn: Connection, path: bytes) -> None:
    """Record that plyrix's empty file is not at this sidecar's name, so none there is its own."""
    _execute(conn, "UPDATE sidecars SET claiming = 0 WHERE path = ?", (path,))


def mark_published(conn: Connection, path: bytes, mtime_ns: int | None = None) -> None:
    """Record that the intended sidecar now exists, and its mtime when known."""
    _execute(
        conn,
        """
        UPDATE sidecars SET status = 'published', claiming = 0, mtime_ns = ?, replaces_sha256 = NULL
        WHERE path = ?
        """,
        (mtime_ns, path),
    )


def drop_intent(conn: Connection, path: bytes) -> None:
    """Forget an intent that was not carried out. A published row is left alone."""
    _execute(conn, "DELETE FROM sidecars WHERE path = ? AND status = 'intent'", (path,))


def adopt(
    conn: Connection,
    path: bytes,
    kind: Kind,
    sha256: str,
    size: int,
    mtime_ns: int | None,
    origin: str,
    lrclib_id: int | None,
    dump_id: str,
    run_id: int,
) -> None:
    """Record a file already in the library as published, on what its content proves.

    Nothing in the library is written. The refresh stays pending, which errs towards an extra one.
    """
    _execute(
        conn,
        """
        INSERT INTO sidecars(path, kind, status, sha256, size, mtime_ns, origin, lrclib_id, dump_id,
                             run_id, refresh_pending)
        VALUES(?, ?, 'published', ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(path) DO UPDATE SET
          kind = excluded.kind,
          status = 'published',
          claiming = 0,
          sha256 = excluded.sha256,
          size = excluded.size,
          mtime_ns = excluded.mtime_ns,
          origin = excluded.origin,
          lrclib_id = excluded.lrclib_id,
          dump_id = excluded.dump_id,
          run_id = excluded.run_id,
          refresh_pending = 1,
          replaces_sha256 = NULL
        """,
        (path, kind.name.lower(), sha256, size, mtime_ns, origin, lrclib_id, dump_id, run_id),
    )


def set_origin(
    conn: Connection, path: bytes, origin: str, sha256: str, size: int, mtime_ns: int | None
) -> None:
    """Record what a published sidecar now holds after an edit, and on what it is kept.

    The file changed, so its refresh is pending again.
    """
    _execute(
        conn,
        """
        UPDATE sidecars SET origin = ?, sha256 = ?, size = ?, mtime_ns = ?, refresh_pending = 1
        WHERE path = ? AND status = 'published'
        """,
        (origin, sha256, size, mtime_ns, path),
    )


def owned_row(conn: Connection, path: bytes) -> OwnedRow | None:
    """Return the published row for a sidecar, or None when plyrix can't claim it."""
    row = _execute(
        conn,
        "SELECT sha256, size, origin, mtime_ns FROM sidecars "
        "WHERE path = ? AND status = 'published'",
        (path,),
    ).fetchone()
    return None if row is None else OwnedRow(row[0], row[1], row[2], row[3])


def intent_rows(conn: Connection) -> list[IntentRow]:
    """Return the intents a killed run left, wherever they are."""
    rows = _execute(
        conn,
        "SELECT path, kind, sha256, claiming, replaces_sha256 FROM sidecars "
        "WHERE status = 'intent'",
        (),
    ).fetchall()
    return [IntentRow(row[0], Kind[row[1].upper()], row[2], bool(row[3]), row[4]) for row in rows]


def delete_row(conn: Connection, path: bytes) -> None:
    """Forget a sidecar, whatever its status. The file itself is not touched."""
    _execute(conn, "DELETE FROM sidecars WHERE path = ?", (path,))


def add_temp(conn: Connection, path: bytes) -> None:
    """Commit the absolute path of a temp that is about to be made, before it is made."""
    _execute(conn, "INSERT OR IGNORE INTO temps(path) VALUES(?)", (path,))


def drop_temp(conn: Connection, path: bytes) -> None:
    """Forget a temp that is gone."""
    _execute(conn, "DELETE FROM temps WHERE path = ?", (path,))


def temp_rows(conn: Connection) -> list[bytes]:
    """Return the temps still recorded, wherever they are.

    A run holds this ledger's lock, so any found as a run starts was left by one that has ended.
    """
    return [row[0] for row in _execute(conn, "SELECT path FROM temps", ()).fetchall()]


def track_result(conn: Connection, path: bytes) -> TrackResult | None:
    """Return what the ledger remembers of an audio file, or None when it has no row."""
    row = _execute(
        conn,
        """
        SELECT size, mtime_ns, tags_sha256, outcome, reason, dump_id, matcher_version, lrclib_id,
               release_year, artist_key, title_key, first_seen_at, unseen_dump
        FROM tracks WHERE path = ?
        """,
        (path,),
    ).fetchone()
    if row is None:
        return None
    reason = _REASONS[row[4]] if row[4] else None
    return TrackResult(
        row[0],
        row[1],
        row[2],
        Outcome(row[3]),
        reason,
        row[5],
        row[6],
        row[7],
        release_year=row[8],
        artist_key=row[9],
        title_key=row[10],
        first_seen=_epoch(row[11]),
        unseen=row[12] is not None,
    )


def _epoch(stamp: str | None) -> float | None:
    """Return an ISO time as epoch seconds, or None when there is none or it can't be read."""
    if stamp is None:
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.timestamp()


def write_tracks(
    conn: Connection, rows: list[TrackRow], mtime_fills: list[tuple[int, bytes]]
) -> None:
    """Store a batch of results and sidecar mtimes, as a commit per row is slow on a slow disk.

    Each fill is an mtime_ns and a sidecar's path, for a file whose hash just matched its row.
    """
    now = _now()
    values = [
        (
            row.path,
            row.size,
            row.mtime_ns,
            row.tags_sha256,
            row.outcome.value,
            "" if row.reason is None else row.reason.value,
            row.dump_id,
            row.matcher_version,
            row.lrclib_id,
            row.run_id,
            now,
            now,
            row.release_year,
            row.artist_key,
            row.title_key,
        )
        for row in rows
    ]
    try:
        conn.execute("BEGIN")
        # first_seen_at is set on insert only. A row built without reading tags carries no year
        # or keys, so it keeps the ones stored. A written row loses its unseen mark only when it
        # now names the mark's dump or a later one: the change sets up to the mark may be pruned,
        # so an answer from an older dump, as a stat-only rewrite or an older --dump-id leaves,
        # still needs the mark to count as affected. An unknown dump keeps the mark, which is safe.
        conn.executemany(
            """
            INSERT INTO tracks(path, size, mtime_ns, tags_sha256, outcome, reason, dump_id,
                               matcher_version, lrclib_id, run_id, checked_at, first_seen_at,
                               release_year, artist_key, title_key)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
              release_year = COALESCE(excluded.release_year, tracks.release_year),
              artist_key = COALESCE(excluded.artist_key, tracks.artist_key),
              title_key = COALESCE(excluded.title_key, tracks.title_key),
              size = excluded.size,
              mtime_ns = excluded.mtime_ns,
              tags_sha256 = excluded.tags_sha256,
              outcome = excluded.outcome,
              reason = excluded.reason,
              dump_id = excluded.dump_id,
              matcher_version = excluded.matcher_version,
              lrclib_id = excluded.lrclib_id,
              run_id = excluded.run_id,
              checked_at = excluded.checked_at,
              unseen_dump = CASE
                WHEN excluded.dump_id = tracks.unseen_dump
                  OR (SELECT dump_time FROM lrclib_dumps WHERE name = excluded.dump_id)
                     >= (SELECT dump_time FROM lrclib_dumps WHERE name = tracks.unseen_dump)
                THEN NULL ELSE tracks.unseen_dump END
            """,
            values,
        )
        conn.executemany(
            "UPDATE sidecars SET mtime_ns = ? WHERE path = ? AND status = 'published'",
            mtime_fills,
        )
        conn.execute("COMMIT")
    except sqlite3.Error as error:
        _rollback_quietly(conn)
        raise StateError(_unusable(error)) from None


def pending_refresh(conn: Connection) -> list[bytes]:
    """Return the path of each published sidecar whose album Plex hasn't been asked to refresh.

    An intent row has no proven file yet, so it waits for its run to publish it.
    """
    rows = _execute(
        conn,
        """
        SELECT path FROM sidecars WHERE status = 'published' AND refresh_pending = 1
        ORDER BY path
        """,
        (),
    ).fetchall()
    return [bytes(row[0]) for row in rows]


def clear_refresh(conn: Connection, paths: list[bytes]) -> None:
    """Record that Plex accepted a refresh for these sidecars, by exact path, all or none.

    Never by prefix: paths are bytes, and a prefix would also match the folder's subfolders.
    """
    try:
        conn.execute("BEGIN")
        conn.executemany(
            "UPDATE sidecars SET refresh_pending = 0 WHERE path = ? AND status = 'published'",
            [(path,) for path in paths],
        )
        conn.execute("COMMIT")
    except sqlite3.Error as error:
        _rollback_quietly(conn)
        raise StateError(_unusable(error)) from None


def sample_refreshed(conn: Connection, n: int) -> list[tuple[bytes, Kind]]:
    """Return up to n published sidecars, chosen at random, whose album refresh Plex accepted."""
    rows = _execute(
        conn,
        """
        SELECT path, kind FROM sidecars WHERE status = 'published' AND refresh_pending = 0
        ORDER BY random() LIMIT ?
        """,
        (n,),
    ).fetchall()
    return [(bytes(row[0]), Kind[row[1].upper()]) for row in rows]


def published_under(conn: Connection, path: bytes) -> list[tuple[bytes, Kind]]:
    """Return the published sidecar at path and every one in a folder under it, in path order.

    A byte range rather than LIKE, as paths are bytes and LIKE would need escaping: every path
    under the folder sorts after folder + "/" and before folder + "0", the byte after "/".
    """
    folder = path.rstrip(b"/")
    rows = _execute(
        conn,
        """
        SELECT path, kind FROM sidecars
        WHERE status = 'published' AND (path = ? OR (path > ? AND path < ?))
        ORDER BY path
        """,
        (path, folder + b"/", folder + b"0"),
    ).fetchall()
    return [(bytes(row[0]), Kind[row[1].upper()]) for row in rows]


def live_dump(conn: Connection) -> DumpRow | None:
    """Return the dump recorded as the one LRCLIB serves, or None when none is."""
    row = _execute(conn, "SELECT * FROM lrclib_dumps WHERE status = 'live'", ()).fetchone()
    return None if row is None else DumpRow(*row)


def dump_row(conn: Connection, name: str) -> DumpRow | None:
    """Return what the ledger holds of one dump, or None."""
    row = _execute(conn, "SELECT * FROM lrclib_dumps WHERE name = ?", (name,)).fetchone()
    return None if row is None else DumpRow(*row)


def record_live(conn: Connection, name: str, dump_time: str, swapped_at: str | None) -> None:
    """Record a dump as the one LRCLIB serves, and the one served before it as previous.

    A staged row of that name is promoted with its diff, which only now becomes active, and
    change sets no track row can need any more are pruned.
    """
    try:
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE lrclib_dumps SET status = 'previous' WHERE status = 'live' AND name != ?",
            (name,),
        )
        conn.execute(
            """
            INSERT INTO lrclib_dumps(name, dump_time, status, recorded_at, swapped_at)
            VALUES(?, ?, 'live', ?, ?)
            ON CONFLICT(name) DO UPDATE SET
              status = 'live',
              recorded_at = excluded.recorded_at,
              swapped_at = excluded.swapped_at
            """,
            (name, dump_time, _now(), swapped_at),
        )
        conn.execute(_PRUNE)
        conn.execute("COMMIT")
    except sqlite3.Error as error:
        _rollback_quietly(conn)
        raise StateError(_unusable(error)) from None


# A track row the targeted retry can ask about, a soft outcome or a stale .lrc, needs the change
# sets of dumps after its own dump_id. So every change set of a dump at or before the earliest
# live or previous dump such a row names is no use to any row. A synced row keeps the dump_id of
# its first check for good and is never asked about, so it holds nothing back. An unseen row
# counts as affected when it is back, so it needs no change set, and names its unseen_dump here.
_PRUNE = """
DELETE FROM lrclib_changes WHERE dump IN (
  SELECT name FROM lrclib_dumps WHERE dump_time <= (
    SELECT min(seen.dump_time) FROM lrclib_dumps AS seen
    WHERE seen.status IN ('live', 'previous') AND seen.name IN (
      SELECT COALESCE(unseen_dump, dump_id) FROM tracks
      WHERE outcome IN ('plain', 'plain_skipped', 'instrumental', 'no_match')
         OR reason = 'lrc_stale')))
"""


# The soft and stale rows, as the prune reads them: path, dump_id and the run that wrote it.
_PINNING = """
SELECT path, dump_id, run_id FROM tracks
WHERE outcome IN ('plain', 'plain_skipped', 'instrumental', 'no_match') OR reason = 'lrc_stale'
"""


def pinning_rows(conn: Connection) -> list[tuple[bytes, str, int | None]]:
    """Return the path, dump_id and run_id of each soft or stale row, which the prune reads."""
    rows = _execute(conn, _PINNING, ()).fetchall()
    return [(bytes(path), str(dump), run) for path, dump, run in rows]


def set_unseen(conn: Connection, unseen: list[bytes], seen: list[bytes], dump_id: str) -> None:
    """Mark the unseen rows with dump_id, and clear the seen rows that already name it, all or none.

    Each unseen row names dump_id from now on in the prune. A seen row's mark is cleared only when
    the row itself names dump_id, so no change set it needs can have been pruned while it was
    marked. Any other seen row keeps its mark until a write clears it.
    """
    try:
        conn.execute("BEGIN")
        conn.executemany(
            "UPDATE tracks SET unseen_dump = ? WHERE path = ?",
            [(dump_id, path) for path in unseen],
        )
        conn.executemany(
            "UPDATE tracks SET unseen_dump = NULL "
            "WHERE path = ? AND unseen_dump IS NOT NULL AND dump_id = ?",
            [(path, dump_id) for path in seen],
        )
        conn.execute("COMMIT")
    except sqlite3.Error as error:
        _rollback_quietly(conn)
        raise StateError(_unusable(error)) from None


def prune_changes(conn: Connection) -> None:
    """Delete the change sets no track row can need. With no track naming a dump, none goes."""
    _execute(conn, _PRUNE, ())


def set_watermarks(
    conn: Connection,
    name: str,
    user_version: int | None,
    max_track_id: int | None,
    max_lyrics_id: int | None,
    newest_created_at: str | None,
    id_digest: str | None,
) -> None:
    """Store what a dump holds at its highest ids, which the next diff starts from."""
    _execute(
        conn,
        """
        UPDATE lrclib_dumps SET user_version = ?, max_track_id = ?, max_lyrics_id = ?,
          newest_created_at = ?, id_digest = ?
        WHERE name = ?
        """,
        (user_version, max_track_id, max_lyrics_id, newest_created_at, id_digest, name),
    )


def drop_staged(conn: Connection) -> None:
    """Forget a dump a crashed update left staged, and its changes, which go by cascade."""
    _execute(conn, "DELETE FROM lrclib_dumps WHERE status = 'staged'", ())


def stage_dump(
    conn: Connection,
    name: str,
    dump_time: str,
    diff_from: str | None,
    user_version: int | None,
    max_track_id: int | None,
    max_lyrics_id: int | None,
    newest_created_at: str | None,
    id_digest: str | None,
) -> None:
    """Record a dump about to be swapped in, with diff state none until its diff finishes.

    A row of that name left refused or rolled back is taken over, with its changes dropped.
    """
    drop_changes(conn, name)
    _execute(
        conn,
        """
        INSERT INTO lrclib_dumps(name, dump_time, status, user_version, max_track_id,
          max_lyrics_id, newest_created_at, id_digest, diff_from, diff_state, recorded_at)
        VALUES(?, ?, 'staged', ?, ?, ?, ?, ?, ?, 'none', ?)
        ON CONFLICT(name) DO UPDATE SET
          status = 'staged',
          user_version = excluded.user_version,
          max_track_id = excluded.max_track_id,
          max_lyrics_id = excluded.max_lyrics_id,
          newest_created_at = excluded.newest_created_at,
          id_digest = excluded.id_digest,
          diff_from = excluded.diff_from,
          diff_state = 'none',
          recorded_at = excluded.recorded_at,
          swapped_at = NULL
        """,
        (
            name,
            dump_time,
            user_version,
            max_track_id,
            max_lyrics_id,
            newest_created_at,
            id_digest,
            diff_from,
            _now(),
        ),
    )


def add_changes(
    conn: Connection, dump: str, rows: list[tuple[int, int | None, int | None]]
) -> None:
    """Store one batch of changed LRCLIB ids and their keys in one transaction."""
    try:
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT OR REPLACE INTO lrclib_changes(dump, lrclib_id, artist_key, title_key) "
            "VALUES(?, ?, ?, ?)",
            [(dump, *row) for row in rows],
        )
        conn.execute("COMMIT")
    except sqlite3.Error as error:
        _rollback_quietly(conn)
        raise StateError(_unusable(error)) from None


def finish_diff(conn: Connection, name: str, state: str) -> None:
    """Record how far a dump's diff can be trusted. It is set last, so a crash leaves none."""
    _execute(conn, "UPDATE lrclib_dumps SET diff_state = ? WHERE name = ?", (state, name))


def drop_changes(conn: Connection, dump: str) -> None:
    """Forget a dump's change set."""
    _execute(conn, "DELETE FROM lrclib_changes WHERE dump = ?", (dump,))


def retry_bases(conn: Connection) -> dict[str, DumpBasis]:
    """Map each dump LRCLIB served, live or previous, to what the retry rule needs of it."""
    rows = _execute(
        conn,
        "SELECT name, dump_time, diff_state FROM lrclib_dumps "
        "WHERE status IN ('live', 'previous') ORDER BY dump_time",
        (),
    ).fetchall()
    bases = {}
    for index, (name, stamp, _state) in enumerate(rows):
        when = _epoch(stamp)
        if when is None:
            continue
        complete = all(later[2] == "complete" for later in rows[index + 1 :])
        bases[name] = DumpBasis(complete, when, datetime.fromtimestamp(when, UTC).year)
    return bases


# One per key: a change in any dump LRCLIB served after the row's own dump. INDEXED BY, so each is
# a search of the key's index and never a scan of the changes.
_KEY_CHANGED = """
SELECT EXISTS(
  SELECT 1 FROM lrclib_changes AS c INDEXED BY {index}
  JOIN lrclib_dumps AS d ON d.name = c.dump
  WHERE c.{column} = ? AND d.status IN ('live', 'previous')
    AND d.dump_time > (SELECT dump_time FROM lrclib_dumps WHERE name = ?))
"""
KEY_CHANGED_QUERIES = (
    _KEY_CHANGED.format(index="lrclib_changes_artist", column="artist_key"),
    _KEY_CHANGED.format(index="lrclib_changes_title", column="title_key"),
)


def key_changed_since(
    conn: Connection, dump_name: str, artist_key: int | None, title_key: int | None
) -> bool:
    """Say whether a later served dump's diff holds this artist or title key."""
    for sql, key in zip(KEY_CHANGED_QUERIES, (artist_key, title_key), strict=True):
        if key is not None and _execute(conn, sql, (key, dump_name)).fetchone()[0]:
            return True
    return False


def mark_status(conn: Connection, name: str, status: str, dump_time: str) -> None:
    """Record a dump LRCLIB doesn't serve as refused or rolled_back, so no check retries it alone.

    The live dump can't be marked this way, which raises StateError with exit code 2.
    """
    live = live_dump(conn)
    if live is not None and live.name == name:
        raise StateError(
            f"plyrix: {name} is LRCLIB's live dump, so it can't be recorded as {status}"
        )
    drop_changes(conn, name)
    _execute(
        conn,
        """
        INSERT INTO lrclib_dumps(name, dump_time, status, recorded_at) VALUES(?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
          status = excluded.status, recorded_at = excluded.recorded_at, diff_state = 'none'
        """,
        (name, dump_time, status, _now()),
    )


def _execute(conn: Connection, sql: str, params: tuple[object, ...]) -> sqlite3.Cursor:
    try:
        return conn.execute(sql, params)
    except sqlite3.Error as error:
        raise StateError(_unusable(error)) from None


def _rollback_quietly(conn: Connection) -> None:
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _close_quietly(conn: Connection | None) -> None:
    if conn is not None:
        conn.close()


def _unusable(error: Exception) -> str:
    return f"plyrix: the state database can't be used ({type(error).__name__})"


def _now() -> str:
    return datetime.now(UTC).isoformat()
