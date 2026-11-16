"""The ledger: migrations, the single-run lock, BLOB paths, no lyric text, and exact hashes."""

import hashlib
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    FIXTURES,
    RECORD,
    RunCli,
    StubLrclib,
    leave_hot_journal,
    parse_counts,
    snap,
    state_db_path,
)
from plyrix import pipeline
from plyrix.config import Settings
from plyrix.domain import Kind, MissReason, Outcome, SyncedReason, TrackResult
from plyrix.state import db

HOLDER = """
import sys
from pathlib import Path
from plyrix.state import db

conn = db.connect(Path(sys.argv[1]))
print("ready", flush=True)
sys.stdin.read()
"""

StartHolder = Callable[[Path], "subprocess.Popen[bytes]"]


@pytest.fixture
def start_holder() -> Iterator[StartHolder]:
    """Return a function starting a child that holds the ledger until killed or told to stop."""
    children = []

    def start(path: Path) -> "subprocess.Popen[bytes]":
        child = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        children.append(child)
        assert child.stdout is not None
        assert child.stdout.readline() == b"ready\n"
        return child

    yield start
    for child in children:
        child.kill()
        child.communicate()


def rows_of(conn: sqlite3.Connection, sql: str) -> list[tuple[object, ...]]:
    return conn.execute(sql).fetchall()


def test_migrations_run_once_and_user_version_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.db"
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.SCHEMA)
    run_id = db.begin_run(conn, "test")
    db.add_intent(conn, b"/root/a.lrc", Kind.LRC, "abc", 3, run_id)
    conn.close()

    # A repeated CREATE TABLE would raise, so this shows the first migration did not run again.
    monkeypatch.setattr(db, "SCHEMA", [*db.SCHEMA, "CREATE TABLE later(id INTEGER PRIMARY KEY);"])
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.SCHEMA)
    assert rows_of(conn, "SELECT id FROM later") == []
    assert rows_of(conn, "SELECT path FROM sidecars") == [(b"/root/a.lrc",)]
    conn.close()

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.SCHEMA)
    conn.close()


def test_a_failed_migration_leaves_the_version_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.db"
    db.connect(path).close()
    monkeypatch.setattr(db, "SCHEMA", [*db.SCHEMA, "CREATE TABLE half(id); NOT VALID SQL;"])

    with pytest.raises(db.StateError) as caught:
        db.connect(path)

    assert caught.value.exit_code == 2
    monkeypatch.undo()
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.SCHEMA)
    assert rows_of(conn, "SELECT name FROM sqlite_master WHERE name = 'half'") == []
    conn.close()


def test_a_newer_user_version_raises_state_error(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version={len(db.SCHEMA) + 1}")
    conn.close()

    with pytest.raises(db.StateError) as caught:
        db.connect(path)

    assert caught.value.exit_code == 2
    assert "newer" in caught.value.message


def test_a_non_utf8_path_round_trips_as_a_blob(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    run_id = db.begin_run(conn, "test")
    path = b"/m\xfcsic/caf\xe9/\xff.lrc"

    db.add_intent(conn, path, Kind.LRC, "abc", 3, run_id)
    db.mark_published(conn, path)

    assert db.owned_row(conn, path) == ("abc", 3, "written", None)
    assert conn.execute("SELECT path, typeof(path) FROM sidecars").fetchone() == (path, "blob")
    conn.close()


def first_ledger(path: Path, rows: list[tuple[bytes, bytes, str, int]]) -> None:
    """Make a ledger exactly as the first release of the schema would have left it."""
    conn = sqlite3.connect(path)
    conn.executescript(f"BEGIN;{db.SCHEMA[0]};PRAGMA user_version=1;COMMIT;")
    conn.execute("INSERT INTO runs(id, started_at, version) VALUES(1, 'then', 'old')")
    conn.execute("INSERT INTO runs(id, started_at, version) VALUES(2, 'later', 'old')")
    conn.executemany(
        """
        INSERT INTO sidecars(root, rel_path, kind, status, sha256, size, run_id)
        VALUES(?, ?, 'lrc', 'published', ?, 3, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()


def test_an_old_ledger_keeps_ownership_under_the_new_key(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first_ledger(
        path,
        [
            (b"/m\xfcsic", b"caf\xe9/\xff.lrc", "aaa", 1),
            (b"/", b"t.lrc", "bbb", 1),
        ],
    )

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.SCHEMA)
    assert db.owned_row(conn, b"/m\xfcsic/caf\xe9/\xff.lrc") == ("aaa", 3, "written", None)
    assert db.owned_row(conn, b"/t.lrc") == ("bbb", 3, "written", None)
    assert {row[0] for row in rows_of(conn, "SELECT typeof(path) FROM sidecars")} == {"blob"}
    conn.close()


def test_two_old_rows_for_one_file_keep_the_newer_run(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first_ledger(
        path,
        [
            (b"/lib", b"A/t.lrc", "older", 1),
            (b"/lib/A", b"t.lrc", "newer", 2),
        ],
    )

    conn = db.connect(path)

    assert rows_of(conn, "SELECT path, sha256 FROM sidecars") == [(b"/lib/A/t.lrc", "newer")]
    conn.close()


def test_no_sidecars_column_stores_lyric_text(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    declared = {row[1]: row[2] for row in conn.execute("PRAGMA table_info(sidecars)")}
    conn.close()

    text_columns = {name for name, kind in declared.items() if kind == "TEXT"}
    # kind, status and origin are CHECK-limited words, sha256 is a digest, and dump_id names a dump.
    # Each replaces_ column holds what the same-named one held for the file being replaced.
    assert text_columns == {
        "kind",
        "status",
        "sha256",
        "dump_id",
        "origin",
        "replaces_sha256",
        "replaces_origin",
        "replaces_dump_id",
    }
    assert not any("lyric" in name or "text" in name for name in declared)


def test_a_second_connect_while_a_holder_lives_raises_75(
    tmp_path: Path, start_holder: StartHolder
) -> None:
    path = tmp_path / "state.db"
    start_holder(path)
    started = time.monotonic()

    with pytest.raises(db.StateError) as caught:
        db.connect(path)

    assert caught.value.exit_code == 75
    assert caught.value.message == "another plyrix run is using the state database"
    # It refuses within a moment, rather than waiting on the holder.
    assert time.monotonic() - started < 1.0


def test_the_lock_is_held_on_an_existing_ledger_too(
    tmp_path: Path, start_holder: StartHolder
) -> None:
    path = tmp_path / "state.db"
    db.connect(path).close()
    start_holder(path)

    with pytest.raises(db.StateError) as caught:
        db.connect(path)

    assert caught.value.exit_code == 75


def test_a_holder_that_exits_releases_the_lock(tmp_path: Path, start_holder: StartHolder) -> None:
    path = tmp_path / "state.db"
    holder = start_holder(path)

    holder.communicate()

    assert holder.returncode == 0
    db.connect(path).close()


def test_a_holder_killed_by_sigkill_releases_the_lock(
    tmp_path: Path, start_holder: StartHolder
) -> None:
    path = tmp_path / "state.db"
    holder = start_holder(path)
    with pytest.raises(db.StateError):
        db.connect(path)

    holder.send_signal(signal.SIGKILL)
    holder.communicate()

    assert holder.returncode == -signal.SIGKILL
    db.connect(path).close()


def test_racing_first_connects_leave_one_holder(tmp_path: Path) -> None:
    for attempt in range(8):
        path = tmp_path / str(attempt) / "state.db"
        children = [
            subprocess.Popen(
                [sys.executable, "-c", HOLDER, str(path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(3)
        ]
        lines = []
        for child in children:
            assert child.stdout is not None
            lines.append(child.stdout.readline())
        assert lines.count(b"ready\n") == 1, lines
        for child in children:
            child.kill()
            _, stderr = child.communicate()
            if child.returncode != -signal.SIGKILL:
                assert b"another plyrix run" in stderr


def test_a_lock_released_within_a_moment_is_taken(tmp_path: Path) -> None:
    # Racing first connects can each see another's brief lock. Refusing on that sight alone can
    # leave no holder at all, with every run wrongly told that another one is using the ledger.
    path = tmp_path / "state.db"
    held = db.connect(path)
    outcome: list[str] = []

    def connect_and_close() -> None:
        try:
            db.connect(path).close()
        except db.StateError as error:
            outcome.append(error.message)
        else:
            outcome.append("connected")

    waiter = threading.Thread(target=connect_and_close)
    waiter.start()
    time.sleep(0.05)
    held.close()
    waiter.join()

    assert outcome == ["connected"]


def test_a_run_while_a_holder_lives_exits_75_and_writes_nothing(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, start_holder: StartHolder
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    track = Path(shutil.copyfile(FIXTURES / "t.flac", lib / "t.flac"))
    before = snap(track)
    start_holder(state_db_path(tmp_path))
    before_listing = sorted(path.name for path in lib.iterdir())

    proc = run_cli(["run", lib], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 75
    lines = [line for line in proc.stderr.splitlines() if b"pre-release" not in line]
    assert lines == [b"another plyrix run is using the state database"]
    assert parse_counts(proc.stdout) == {}
    assert sorted(path.name for path in lib.iterdir()) == before_listing
    assert stub_lrclib.requests == []
    assert snap(track) == before


def test_the_run_after_a_killed_holder_finishes(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, start_holder: StartHolder
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copyfile(FIXTURES / "t.flac", lib / "t.flac")
    holder = start_holder(state_db_path(tmp_path))
    holder.send_signal(signal.SIGKILL)
    holder.communicate()

    proc = run_cli(["run", lib], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["written_lrc"] == 1


@pytest.mark.parametrize(
    "synced",
    [
        "[00:01.00] placeholder line 1\n",
        "\ufeff[00:01.00] placéholder ✓ 日本語\r\n[00:02.00] 𝄞 line 2\r\n\r\n",
    ],
    ids=["ascii", "multibyte-crlf-bom"],
)
def test_the_ledger_hash_and_size_match_the_bytes_on_disk(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch, synced: str
) -> None:
    (tmp_path / "lib").mkdir()
    track = Path(shutil.copyfile(FIXTURES / "t.flac", tmp_path / "lib" / "t.flac"))
    stub_lrclib.body = {**RECORD, "syncedLyrics": synced}
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    settings = Settings(
        music_root=tmp_path / "lib",
        lrclib_url=stub_lrclib.base_url,
        state_db=tmp_path / "state" / "state.db",
    )

    counts = pipeline.run(settings, os.fsencode(tmp_path / "lib"))

    assert counts.written_lrc == 1
    on_disk = track.with_suffix(".lrc").read_bytes()
    conn = db.connect(settings.state_db)
    rows = rows_of(conn, "SELECT status, sha256, size FROM sidecars")
    conn.close()
    assert rows == [("published", hashlib.sha256(on_disk).hexdigest(), len(on_disk))]


def test_a_version_4_ledger_migrates_with_defaults(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    old = sqlite3.connect(path)
    for index, migration in enumerate(db.SCHEMA[:4]):
        old.executescript(f"BEGIN;{migration};PRAGMA user_version={index + 1};COMMIT;")
    old.execute("INSERT INTO runs(id, started_at, version) VALUES(1, 'then', 'old')")
    old.execute(
        """
        INSERT INTO sidecars(path, kind, status, sha256, size, lrclib_id, run_id)
        VALUES(?, 'lrc', 'published', 'aaa', 3, 7, 1)
        """,
        (b"/lib/caf\xe9/t.lrc",),
    )
    old.execute("INSERT INTO temps(path) VALUES(?)", (b"/lib/.plyrix-00000000.part",))
    old.commit()
    old.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.SCHEMA)
    assert rows_of(
        conn,
        """
        SELECT path, kind, status, sha256, size, lrclib_id, run_id, claiming,
               dump_id, origin, mtime_ns, refresh_pending
        FROM sidecars
        """,
    ) == [
        (b"/lib/caf\xe9/t.lrc", "lrc", "published", "aaa", 3, 7, 1, 0, "", "written", None, 1),
    ]
    assert rows_of(conn, "SELECT id, version, dump_id, end_reason, counts_json FROM runs") == [
        (1, "old", "", None, None)
    ]
    assert rows_of(conn, "SELECT path FROM temps") == [(b"/lib/.plyrix-00000000.part",)]
    assert rows_of(conn, "SELECT count(*) FROM tracks") == [(0,)]
    conn.close()


def a_row(run_id: int, path: bytes = b"/lib/caf\xe9/t.flac", **changes: object) -> db.TrackRow:
    row = db.TrackRow(
        path, 9, 123, "hash", Outcome.NO_MATCH, MissReason.NOT_FOUND, "dump-a", 1, None, run_id
    )
    return row._replace(**changes)  # type: ignore[arg-type]


def test_a_result_round_trips_under_a_non_utf8_path(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    run_id = db.begin_run(conn, "test", "dump-a")
    path = b"/lib/caf\xe9/t.flac"
    assert db.track_result(conn, path) is None

    db.write_tracks(conn, [a_row(run_id)], [])

    found = db.track_result(conn, path)
    assert found is not None and found.first_seen is not None
    assert found == TrackResult(
        9,
        123,
        "hash",
        Outcome.NO_MATCH,
        MissReason.NOT_FOUND,
        "dump-a",
        1,
        None,
        first_seen=found.first_seen,
    )
    assert rows_of(conn, "SELECT typeof(path) FROM tracks") == [("blob",)]
    conn.close()


def test_a_result_with_no_reason_or_hash_reads_back_as_none(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    run_id = db.begin_run(conn, "test")

    row = a_row(run_id, outcome=Outcome.UNREADABLE, reason=None, tags_sha256=None, lrclib_id=7)
    db.write_tracks(conn, [row], [])

    found = db.track_result(conn, row.path)
    assert found is not None
    assert (found.outcome, found.reason, found.tags_sha256, found.lrclib_id) == (
        Outcome.UNREADABLE,
        None,
        None,
        7,
    )
    assert rows_of(conn, "SELECT reason FROM tracks") == [("",)]
    conn.close()


def test_a_newer_result_replaces_the_old_one(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    run_id = db.begin_run(conn, "test")

    db.write_tracks(conn, [a_row(run_id)], [])
    db.write_tracks(conn, [a_row(run_id, outcome=Outcome.SYNCED, reason=None, size=10)], [])

    assert rows_of(conn, "SELECT size, outcome, reason FROM tracks") == [(10, "synced", "")]
    conn.close()


def test_a_failed_batch_stores_nothing_and_leaves_the_ledger_usable(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    run_id = db.begin_run(conn, "test")
    db.add_intent(conn, b"/lib/t.lrc", Kind.LRC, "abc", 3, run_id)
    db.mark_published(conn, b"/lib/t.lrc")

    with pytest.raises(db.StateError):
        db.write_tracks(
            conn,
            [a_row(run_id), a_row(run_id + 1, path=b"/lib/no-such-run.flac")],
            [(5, b"/lib/t.lrc")],
        )

    assert rows_of(conn, "SELECT count(*) FROM tracks") == [(0,)]
    assert rows_of(conn, "SELECT mtime_ns FROM sidecars") == [(None,)]
    db.finish_run(conn, run_id)
    db.write_tracks(conn, [a_row(run_id)], [])
    assert rows_of(conn, "SELECT count(*) FROM tracks") == [(1,)]
    conn.close()


def test_a_batch_fills_published_mtimes_only(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    run_id = db.begin_run(conn, "test")
    db.add_intent(conn, b"/lib/a.lrc", Kind.LRC, "abc", 3, run_id)
    db.mark_published(conn, b"/lib/a.lrc")
    db.add_intent(conn, b"/lib/b.lrc", Kind.LRC, "abc", 3, run_id)

    db.write_tracks(conn, [], [(5, b"/lib/a.lrc"), (5, b"/lib/b.lrc")])

    assert rows_of(conn, "SELECT path, mtime_ns FROM sidecars ORDER BY path") == [
        (b"/lib/a.lrc", 5),
        (b"/lib/b.lrc", None),
    ]
    conn.close()


def test_a_rewrite_starts_the_row_over(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    run_id = db.begin_run(conn, "test", "dump-a")
    path = b"/lib/t.lrc"
    db.add_intent(conn, path, Kind.LRC, "abc", 3, run_id, 7, "dump-a")
    db.mark_published(conn, path, 111)
    assert rows_of(conn, "SELECT dump_id, origin, mtime_ns, refresh_pending FROM sidecars") == [
        ("dump-a", "written", 111, 1)
    ]
    conn.execute("UPDATE sidecars SET origin = 'proved', refresh_pending = 0")

    db.add_intent(conn, path, Kind.LRC, "def", 4, run_id, 8, "dump-b")

    assert rows_of(conn, "SELECT dump_id, origin, mtime_ns, refresh_pending FROM sidecars") == [
        ("dump-b", "written", None, 1)
    ]
    db.mark_published(conn, path)
    assert db.owned_row(conn, path) == ("def", 4, "written", None)
    conn.close()


def replacing(conn: db.Connection, path: bytes, run_id: int) -> None:
    """Leave a published row, kept on proof, with a replacement of it under way."""
    db.add_intent(conn, path, Kind.LRC, "abc", 3, run_id, 7, "dump-a")
    db.mark_published(conn, path, 111)
    conn.execute("UPDATE sidecars SET origin = 'proved', refresh_pending = 0")
    db.add_intent(conn, path, Kind.LRC, "def", 4, run_id, 8, "dump-b", True)


def test_a_replacement_remembers_what_it_replaces(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    run_id = db.begin_run(conn, "test", "dump-b")
    path = b"/lib/caf\xe9/t.lrc"

    replacing(conn, path, run_id)

    assert db.intent_rows(conn) == [(path, Kind.LRC, "def", False, "abc")]
    assert db.owned_row(conn, path) is None
    conn.close()


def test_restoring_a_replaced_row_gives_back_all_it_said(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    run_id = db.begin_run(conn, "test", "dump-b")
    path = b"/lib/t.lrc"
    replacing(conn, path, run_id)

    db.restore_replaced(conn, path)

    assert db.intent_rows(conn) == []
    assert db.owned_row(conn, path) == ("abc", 3, "proved", None)
    assert rows_of(conn, "SELECT lrclib_id, dump_id, refresh_pending, claiming FROM sidecars") == [
        (7, "dump-a", 1, 0)
    ]
    conn.close()


def test_finishing_a_replacement_forgets_what_it_replaced(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    run_id = db.begin_run(conn, "test", "dump-b")
    path = b"/lib/t.lrc"
    replacing(conn, path, run_id)

    db.mark_published(conn, path, 222)
    db.restore_replaced(conn, path)

    assert db.owned_row(conn, path) == ("def", 4, "written", 222)
    assert rows_of(conn, "SELECT replaces_sha256 FROM sidecars") == [(None,)]
    conn.close()


def test_a_write_that_replaces_nothing_has_nothing_to_restore(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    run_id = db.begin_run(conn, "test", "dump-b")
    path = b"/lib/t.lrc"
    db.add_intent(conn, path, Kind.LRC, "abc", 3, run_id)
    db.mark_published(conn, path, 111)

    # A published row whose file went is taken over, and its old hash says nothing any more.
    db.add_intent(conn, path, Kind.LRC, "def", 4, run_id)

    assert db.intent_rows(conn) == [(path, Kind.LRC, "def", False, None)]
    db.restore_replaced(conn, path)
    assert db.intent_rows(conn) == [(path, Kind.LRC, "def", False, None)]
    conn.close()


def test_a_version_5_ledger_migrates_with_nothing_being_replaced(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    old = sqlite3.connect(path)
    for index, migration in enumerate(db.SCHEMA[:5]):
        old.executescript(f"BEGIN;{migration};PRAGMA user_version={index + 1};COMMIT;")
    old.execute("INSERT INTO runs(id, started_at, version) VALUES(1, 'then', 'old')")
    old.execute(
        """
        INSERT INTO sidecars(path, kind, status, sha256, size, lrclib_id, run_id)
        VALUES(?, 'lrc', 'intent', 'aaa', 3, 7, 1)
        """,
        (b"/lib/t.lrc",),
    )
    old.commit()
    old.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.SCHEMA)
    assert db.intent_rows(conn) == [(b"/lib/t.lrc", Kind.LRC, "aaa", False, None)]
    conn.close()


def test_results_flush_in_batches_and_when_the_run_ends(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    for index in range(5):
        shutil.copyfile(FIXTURES / "t.flac", lib / f"{index}.flac")
    stub_lrclib.status, stub_lrclib.body = 404, {"name": "TrackNotFound"}
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(pipeline, "FLUSH_EVERY", 2)
    batches: list[int] = []
    real_write = db.write_tracks

    def write_and_note(conn: db.Connection, rows: list[db.TrackRow], fills: list[Any]) -> None:
        batches.append(len(rows))
        real_write(conn, rows, fills)

    monkeypatch.setattr(db, "write_tracks", write_and_note)
    settings = Settings(
        music_root=lib,
        lrclib_url=stub_lrclib.base_url,
        state_db=tmp_path / "state" / "state.db",
        dump_id="dump-a",
    )

    pipeline.run(settings, os.fsencode(lib))

    assert batches == [2, 2, 1]
    conn = db.connect(settings.state_db)
    assert rows_of(conn, "SELECT count(*) FROM tracks") == [(5,)]
    conn.close()


def adopted_row(conn: db.Connection, path: bytes) -> tuple[object, ...]:
    row: tuple[object, ...] = conn.execute(
        """
        SELECT kind, status, sha256, size, mtime_ns, origin, lrclib_id, dump_id, run_id,
               refresh_pending, claiming
        FROM sidecars WHERE path = ?
        """,
        (path,),
    ).fetchone()
    return row


def test_adopt_inserts_a_published_row_with_refresh_pending(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    run_id = db.begin_run(conn, "0")

    db.adopt(conn, b"/m/t.lrc", Kind.LRC, "a" * 64, 12, 34, "timed", None, "dump-a", run_id)

    assert adopted_row(conn, b"/m/t.lrc") == (
        "lrc",
        "published",
        "a" * 64,
        12,
        34,
        "timed",
        None,
        "dump-a",
        run_id,
        1,
        0,
    )
    assert db.owned_row(conn, b"/m/t.lrc") == db.OwnedRow("a" * 64, 12, "timed", 34)


def test_adopt_takes_over_a_row_already_there(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    run_id = db.begin_run(conn, "0")
    db.add_intent(conn, b"/m/t.txt", Kind.TXT, "b" * 64, 3, run_id)
    db.mark_claiming(conn, b"/m/t.txt")
    conn.execute("UPDATE sidecars SET refresh_pending = 0")

    db.adopt(conn, b"/m/t.txt", Kind.TXT, "c" * 64, 5, None, "proved", 7, "dump-b", run_id)

    assert adopted_row(conn, b"/m/t.txt") == (
        "txt",
        "published",
        "c" * 64,
        5,
        None,
        "proved",
        7,
        "dump-b",
        run_id,
        1,
        0,
    )


def test_adopt_refuses_an_origin_outside_the_three(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    run_id = db.begin_run(conn, "0")

    with pytest.raises(db.StateError):
        db.adopt(conn, b"/m/t.lrc", Kind.LRC, "a" * 64, 1, None, "guessed", None, "", run_id)


def test_set_origin_records_an_edited_sidecars_new_hash(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    run_id = db.begin_run(conn, "0")
    db.add_intent(conn, b"/m/t.lrc", Kind.LRC, "a" * 64, 1, run_id, 7, "dump-a")
    db.mark_published(conn, b"/m/t.lrc", 9)
    conn.execute("UPDATE sidecars SET refresh_pending = 0")

    db.set_origin(conn, b"/m/t.lrc", "timed", "d" * 64, 2, 10)

    assert adopted_row(conn, b"/m/t.lrc") == (
        "lrc",
        "published",
        "d" * 64,
        2,
        10,
        "timed",
        7,
        "dump-a",
        run_id,
        1,
        0,
    )


def test_set_origin_leaves_an_intent_alone(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    run_id = db.begin_run(conn, "0")
    db.add_intent(conn, b"/m/t.lrc", Kind.LRC, "a" * 64, 1, run_id)

    db.set_origin(conn, b"/m/t.lrc", "timed", "d" * 64, 2, 10)

    assert adopted_row(conn, b"/m/t.lrc")[1:3] == ("intent", "a" * 64)


def test_a_stale_reason_reads_back_as_itself(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    run_id = db.begin_run(conn, "0")
    row = db.TrackRow(
        b"/m/t.flac", 1, 2, None, Outcome.SYNCED, SyncedReason.LRC_STALE, "dump-a", 1, None, run_id
    )

    db.write_tracks(conn, [row], [])

    result = db.track_result(conn, b"/m/t.flac")
    assert result is not None
    assert result.reason is SyncedReason.LRC_STALE


SNAPSHOT_CHILD = """
import sys
from pathlib import Path
from plyrix.state import db

try:
    db.connect_snapshot(Path(sys.argv[1]))
except db.StateError as error:
    print(error.exit_code)
    print(error.message)
else:
    print("no error")
"""
SEED_RUN = "INSERT INTO runs(started_at, version) VALUES('then', 'old')"


def ledger_snaps(ledger: Path) -> dict[str, tuple[str, int, int, int, int, int, int]]:
    """Identify a ledger's folder and everything in it, so a new or changed file shows."""
    folder = ledger.parent
    return {"": snap(folder), **{path.name: snap(path) for path in folder.iterdir()}}


def seeded_ledger(path: Path) -> Path:
    """Make a ledger holding one run, with no connection left open."""
    conn = db.connect(path)
    conn.execute(SEED_RUN)
    conn.close()
    return path


def test_snapshot_leaves_the_ledger_unchanged(tmp_path: Path) -> None:
    ledger = seeded_ledger(tmp_path / "state" / "state.db")
    before = ledger_snaps(ledger)

    copy = db.connect_snapshot(ledger)
    assert copy.execute("SELECT version FROM runs").fetchall() == [("old",)]
    db.begin_run(copy, "new")
    copy.execute("DELETE FROM runs WHERE version = 'old'")
    assert copy.execute("SELECT version FROM runs").fetchall() == [("new",)]
    copy.close()

    assert ledger_snaps(ledger) == before
    reopened = sqlite3.connect(ledger)
    assert reopened.execute("SELECT version FROM runs").fetchall() == [("old",)]
    reopened.close()


def test_snapshot_of_a_missing_file_creates_nothing(tmp_path: Path) -> None:
    ledger = tmp_path / "state" / "deeper" / "state.db"

    copy = db.connect_snapshot(ledger)

    assert copy.execute("PRAGMA user_version").fetchone()[0] == len(db.SCHEMA)
    assert copy.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    copy.close()
    assert list(tmp_path.iterdir()) == []


def test_snapshot_copy_checks_foreign_keys(tmp_path: Path) -> None:
    copy = db.connect_snapshot(tmp_path / "state.db")

    with pytest.raises(sqlite3.IntegrityError):
        copy.execute(
            "INSERT INTO sidecars(path, kind, status, sha256, size, run_id) "
            "VALUES(x'2f', 'lrc', 'intent', 'x', 0, 99)"
        )
    copy.close()


def test_snapshot_migrates_only_the_copy(tmp_path: Path) -> None:
    ledger = tmp_path / "state.db"
    older = len(db.SCHEMA) - 1
    conn = sqlite3.connect(ledger, isolation_level=None)
    for index, migration in enumerate(db.SCHEMA[:older]):
        conn.executescript(f"BEGIN;{migration};PRAGMA user_version={index + 1};COMMIT;")
    conn.close()
    before = ledger_snaps(ledger)

    copy = db.connect_snapshot(ledger)

    assert copy.execute("PRAGMA user_version").fetchone()[0] == len(db.SCHEMA)
    copy.close()
    assert ledger_snaps(ledger) == before
    reopened = sqlite3.connect(ledger)
    assert reopened.execute("PRAGMA user_version").fetchone()[0] == older
    reopened.close()


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(os.fsdecode("Bj\u00f6rk".encode()), id="non_ascii"),
        pytest.param(os.fsdecode(b"not-utf8-\xff"), id="undecodable"),
        pytest.param("a ?b#c%20d", id="uri_characters"),
    ],
)
def test_snapshot_with_a_non_ascii_path(tmp_path: Path, name: str) -> None:
    ledger = seeded_ledger(tmp_path / name / "state.db")
    before = ledger_snaps(ledger)

    copy = db.connect_snapshot(ledger)

    assert copy.execute("SELECT version FROM runs").fetchall() == [("old",)]
    copy.close()
    assert ledger_snaps(ledger) == before


def test_snapshot_with_a_path_starting_with_two_slashes(tmp_path: Path) -> None:
    # In a URI, two slashes would start an authority, which SQLite refuses.
    ledger = seeded_ledger(tmp_path / "state.db")

    copy = db.connect_snapshot(Path(f"//{ledger}"))

    assert copy.execute("SELECT version FROM runs").fetchall() == [("old",)]
    copy.close()


def test_snapshot_with_a_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seeded_ledger(tmp_path / "state.db")
    monkeypatch.chdir(tmp_path)

    copy = db.connect_snapshot(Path("state.db"))

    assert copy.execute("SELECT version FROM runs").fetchall() == [("old",)]
    copy.close()


def test_snapshot_of_a_locked_ledger_raises_75(tmp_path: Path, start_holder: StartHolder) -> None:
    ledger = tmp_path / "state.db"
    start_holder(ledger)
    started = time.monotonic()

    # In a child with a timeout, as a backup with no read lock first hangs for ever.
    proc = subprocess.run(
        [sys.executable, "-c", SNAPSHOT_CHILD, ledger],
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert proc.stdout.decode().splitlines() == [
        "75",
        "another plyrix run is using the state database",
    ], proc.stderr
    assert time.monotonic() - started < 15


def test_snapshot_with_a_hot_journal_raises_2(tmp_path: Path) -> None:
    ledger = seeded_ledger(tmp_path / "state.db")
    journal = leave_hot_journal(ledger)
    before = ledger_snaps(ledger)

    with pytest.raises(db.StateError) as caught:
        db.connect_snapshot(ledger)

    assert caught.value.exit_code == 2
    assert caught.value.message.startswith("plyrix: ")
    assert "normal run" in caught.value.message
    assert journal.exists()
    assert ledger_snaps(ledger) == before


def test_a_normal_connect_rolls_back_what_a_snapshot_refused(tmp_path: Path) -> None:
    ledger = seeded_ledger(tmp_path / "state.db")
    leave_hot_journal(ledger)

    db.connect(ledger).close()

    copy = db.connect_snapshot(ledger)
    assert copy.execute("SELECT version FROM runs").fetchall() == [("old",)]
    copy.close()


def test_snapshot_of_a_newer_ledger_raises_2(tmp_path: Path) -> None:
    ledger = seeded_ledger(tmp_path / "state.db")
    conn = sqlite3.connect(ledger)
    conn.execute(f"PRAGMA user_version={len(db.SCHEMA) + 1}")
    conn.close()
    before = ledger_snaps(ledger)

    with pytest.raises(db.StateError) as caught:
        db.connect_snapshot(ledger)

    assert caught.value.exit_code == 2
    assert "newer" in caught.value.message
    assert ledger_snaps(ledger) == before


def test_snapshot_of_a_file_that_is_not_a_database_raises_2(tmp_path: Path) -> None:
    ledger = tmp_path / "state.db"
    ledger.write_bytes(b"this is not a database, and it is long enough to be read as one" * 8)
    before = ledger_snaps(ledger)

    with pytest.raises(db.StateError) as caught:
        db.connect_snapshot(ledger)

    assert caught.value.exit_code == 2
    assert "can't be used" in caught.value.message
    assert ledger_snaps(ledger) == before


def test_snapshot_of_a_path_under_a_file_raises_2(tmp_path: Path) -> None:
    # A real run couldn't use this path either, so the preview must not look like a fresh library.
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"")

    with pytest.raises(db.StateError) as caught:
        db.connect_snapshot(blocker / "state.db")

    assert caught.value.exit_code == 2


# Migration 7: the LRCLIB dumps an update has seen (Phase 6)


def test_a_version_6_ledger_migrates_to_7_with_unknown_track_keys(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    old = sqlite3.connect(path)
    for index, migration in enumerate(db.SCHEMA[:6]):
        old.executescript(f"BEGIN;{migration};PRAGMA user_version={index + 1};COMMIT;")
    old.execute("INSERT INTO runs(id, started_at, version) VALUES(1, 'then', 'old')")
    old.execute(
        """
        INSERT INTO tracks(path, size, mtime_ns, outcome, dump_id, matcher_version, run_id,
                           checked_at)
        VALUES(?, 1, 1, 'no_match', 'd', 1, 1, 'then')
        """,
        (b"/lib/a.flac",),
    )
    old.commit()
    old.close()

    conn = db.connect(path)

    assert len(db.SCHEMA) == 8
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    assert rows_of(
        conn, "SELECT first_seen_at, release_year, artist_key, title_key FROM tracks"
    ) == [(None, None, None, None)]
    assert rows_of(conn, "SELECT count(*) FROM lrclib_dumps") == [(0,)]
    assert rows_of(conn, "SELECT count(*) FROM lrclib_changes") == [(0,)]
    conn.close()


def test_a_version_7_ledger_migrates_to_8_with_every_row_reached(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    old = sqlite3.connect(path)
    for index, migration in enumerate(db.SCHEMA[:7]):
        old.executescript(f"BEGIN;{migration};PRAGMA user_version={index + 1};COMMIT;")
    old.execute("INSERT INTO runs(id, started_at, version) VALUES(1, 'then', 'old')")
    old.execute(
        """
        INSERT INTO tracks(path, size, mtime_ns, outcome, dump_id, matcher_version, run_id,
                           checked_at)
        VALUES(?, 1, 1, 'no_match', 'd', 1, 1, 'then')
        """,
        (b"/lib/a.flac",),
    )
    old.commit()
    old.close()

    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    assert rows_of(conn, "SELECT unseen_dump FROM tracks") == [(None,)]
    result = db.track_result(conn, b"/lib/a.flac")
    assert result is not None and not result.unseen
    conn.close()


def test_only_one_dump_can_be_live(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    insert = (
        "INSERT INTO lrclib_dumps(name, dump_time, status, recorded_at) "
        "VALUES(?, 'then', 'live', 'now')"
    )
    conn.execute(insert, ("lrclib-db-dump-20261030T182259Z.sqlite3.gz",))

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("lrclib-db-dump-20261201T000000Z.sqlite3.gz",))
    conn.close()


def test_record_live_makes_the_old_live_dump_previous(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    first, second = (
        "lrclib-db-dump-20261030T182259Z.sqlite3.gz",
        "lrclib-db-dump-20261201T000000Z.sqlite3.gz",
    )
    db.record_live(conn, first, "2026-10-30T18:22:59+00:00", "s1")
    assert db.live_dump(conn) is not None

    db.record_live(conn, second, "2026-12-01T00:00:00+00:00", "s2")

    live = db.live_dump(conn)
    assert live is not None and live.name == second and live.swapped_at == "s2"
    assert live.diff_state == "none"
    previous = db.dump_row(conn, first)
    assert previous is not None and previous.status == "previous"
    assert db.dump_row(conn, "lrclib-db-dump-20990101T000000Z.sqlite3.gz") is None
    # Recording the live dump again changes only its times.
    db.record_live(conn, second, "2026-12-01T00:00:00+00:00", "s3")
    assert rows_of(conn, "SELECT name, status FROM lrclib_dumps ORDER BY name") == [
        (first, "previous"),
        (second, "live"),
    ]
    conn.close()


# The retry rule's columns (06-06): first seen, the year and the lookup keys


def test_first_seen_is_set_once_and_keys_and_year_kept_unless_replaced(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    run_id = db.begin_run(conn, "test", "dump-a")
    row = a_row(run_id)._replace(release_year=2001, artist_key=11, title_key=22)
    db.write_tracks(conn, [row], [])
    first = rows_of(conn, "SELECT first_seen_at FROM tracks")[0][0]

    db.write_tracks(conn, [row._replace(release_year=None, artist_key=None, title_key=None)], [])
    kept = db.track_result(conn, row.path)
    db.write_tracks(conn, [row._replace(release_year=1999, artist_key=33, title_key=None)], [])
    replaced = db.track_result(conn, row.path)

    assert kept is not None and replaced is not None
    assert (kept.release_year, kept.artist_key, kept.title_key) == (2001, 11, 22)
    assert (replaced.release_year, replaced.artist_key, replaced.title_key) == (1999, 33, 22)
    assert rows_of(conn, "SELECT first_seen_at FROM tracks") == [(first,)]
    assert isinstance(first, str)
    assert replaced.first_seen == datetime.fromisoformat(first).timestamp()
    conn.close()


def test_a_migrated_row_reads_its_retry_columns_as_none(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    old = sqlite3.connect(path)
    for index, migration in enumerate(db.SCHEMA[:6]):
        old.executescript(f"BEGIN;{migration};PRAGMA user_version={index + 1};COMMIT;")
    old.execute("INSERT INTO runs(id, started_at, version) VALUES(1, 'then', 'old')")
    old.execute(
        "INSERT INTO tracks(path, size, mtime_ns, outcome, dump_id, matcher_version, run_id, "
        "checked_at) VALUES(?, 1, 1, 'no_match', 'd', 1, 1, 'then')",
        (b"/lib/a.flac",),
    )
    old.commit()
    old.close()
    conn = db.connect(path)

    found = db.track_result(conn, b"/lib/a.flac")

    assert found is not None
    retry = (found.release_year, found.artist_key, found.title_key, found.first_seen)
    assert retry == (None, None, None, None)
    conn.close()


def test_neither_key_query_scans_the_changes(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    try:
        for sql in db.KEY_CHANGED_QUERIES:
            plan = " ".join(
                str(row[-1]) for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", (1, "x"))
            )
            assert "SCAN c" not in plan and "SCAN lrclib_changes" not in plan, plan
            assert "USING INDEX lrclib_changes_" in plan or "USING COVERING INDEX" in plan, plan
    finally:
        conn.close()


def test_retry_bases_say_whether_every_later_diff_is_complete(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    try:
        times = ("2026-10-30", "2026-12-01", "2026-12-15")
        names = [f"lrclib-db-dump-{t.replace('-', '')}T000000Z.sqlite3.gz" for t in times]
        for name, when, state in zip(names, times, ("none", "complete", "unstable"), strict=True):
            stamp = f"{when}T00:00:00+00:00"
            db.stage_dump(conn, name, stamp, None, 8, 1, 1, None, None)
            db.finish_diff(conn, name, state)
            db.record_live(conn, name, stamp, None)

        bases = db.retry_bases(conn)
    finally:
        conn.close()

    assert [bases[name].chain_complete for name in names] == [False, False, True]
    assert bases[names[0]].year == 2026
    assert (
        bases[names[0]].dump_time == datetime.fromisoformat("2026-10-30T00:00:00+00:00").timestamp()
    )
