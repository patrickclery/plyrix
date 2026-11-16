"""Prove plyrix lrclib-diff records what a staged dump gained, as ids and keys, and that the live
record activates it, a rollback drops it, and pruning keeps only what a track can still need.

Every database is invented (tests/lrclib_fixture.py), and the CLI is the real console script.
"""

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import RunCli, StubLrclib, copy_track, state_db_path
from lrclib_fixture import add_tracks, make_db, renumber, replace_lyrics, set_created
from plyrix.domain import Outcome
from plyrix.state import db, lrclib_db

A = "lrclib-db-dump-20261030T182259Z.sqlite3.gz"
B = "lrclib-db-dump-20261201T000000Z.sqlite3.gz"
C = "lrclib-db-dump-20261215T000000Z.sqlite3.gz"


class Ledger:
    """A configured plyrix and two LRCLIB-shaped databases: the old one A, the new one B."""

    def __init__(self, tmp_path: Path, run_cli: RunCli, stub: StubLrclib) -> None:
        self.tmp = tmp_path
        self.run_cli = run_cli
        lib = tmp_path / "lib"
        copy_track(lib)
        self.env = {"PLYRIX_MUSIC_ROOT": str(lib), "PLYRIX_LRCLIB_URL": stub.base_url}
        self.old = make_db(tmp_path / "old.sqlite3", 200)
        self.new = tmp_path / "new.sqlite3"
        shutil.copyfile(self.old, self.new)
        add_tracks(self.new, 50)
        replace_lyrics(self.new, list(range(5, 155, 5)))

    def cli(self, *args: str, **env: str) -> "tuple[int, str, str]":
        proc = self.run_cli(list(args), {**self.env, **env})
        return proc.returncode, proc.stdout.decode(), proc.stderr.decode()

    def record(self, name: str, status: str = "live") -> None:
        code, _, err = self.cli("lrclib-record", name, "--status", status)
        assert code == 0, err

    def remember(self, name: str, path: Path) -> None:
        """Store a dump's watermarks, as an earlier update's diff would have."""
        conn = db.connect(state_db_path(self.tmp))
        try:
            marks = lrclib_db.watermarks(path)
            db.set_watermarks(conn, name, *marks, lrclib_db.id_digest(path, marks.max_track_id))
        finally:
            conn.close()

    def rows(self, sql: str) -> list[tuple[object, ...]]:
        con = sqlite3.connect(state_db_path(self.tmp))
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()

    def dumps(self) -> list[tuple[object, ...]]:
        return self.rows(
            "SELECT name, status, diff_state, diff_from FROM lrclib_dumps ORDER BY name"
        )

    def changes(self, name: str) -> int:
        found = self.rows(f"SELECT count(*) FROM lrclib_changes WHERE dump = '{name}'")
        value = found[0][0]
        assert isinstance(value, int)
        return value


@pytest.fixture
def ledger(tmp_path: Path, run_cli: RunCli, stub_lrclib: StubLrclib) -> Ledger:
    return Ledger(tmp_path, run_cli, stub_lrclib)


def test_a_diff_from_a_dump_with_watermarks_is_complete(ledger: Ledger) -> None:
    ledger.record(A)
    ledger.remember(A, ledger.old)

    code, out, err = ledger.cli("lrclib-diff", B, "--staged", str(ledger.new))

    assert code == 0, err
    assert out == "diff complete 80\n"
    assert ledger.dumps() == [(A, "live", "none", None), (B, "staged", "complete", A)]
    row = ledger.rows(
        "SELECT user_version, max_track_id, max_lyrics_id, newest_created_at, id_digest "
        f"FROM lrclib_dumps WHERE name = '{B}'"
    )[0]
    assert row[:4] == (8, 250, 280, "2026-06-01 00:00:00")
    assert row[4] == lrclib_db.id_digest(ledger.new, 250)
    kinds = ledger.rows(
        "SELECT DISTINCT typeof(lrclib_id), typeof(artist_key), typeof(title_key) "
        "FROM lrclib_changes"
    )
    assert kinds == [("integer", "integer", "integer")]
    assert ledger.changes(B) == 80


def test_the_live_record_activates_the_diff(ledger: Ledger) -> None:
    ledger.record(A)
    ledger.remember(A, ledger.old)
    ledger.cli("lrclib-diff", B, "--staged", str(ledger.new))

    ledger.record(B)

    assert ledger.dumps() == [(A, "previous", "none", None), (B, "live", "complete", A)]
    assert ledger.changes(B) == 80


def test_a_dump_recorded_by_setup_is_bootstrapped_from_the_live_database(ledger: Ledger) -> None:
    ledger.record(A)

    code, out, err = ledger.cli(
        "lrclib-diff", B, "--staged", str(ledger.new), "--previous-db", str(ledger.old)
    )

    assert code == 0, err
    assert out == "diff complete 80\n"
    marks = ledger.rows(f"SELECT max_track_id, max_lyrics_id FROM lrclib_dumps WHERE name = '{A}'")
    assert marks == [(200, 200)]


@pytest.mark.parametrize("previous", ["none", "not sqlite", "missing"])
def test_without_a_trusted_bootstrap_the_diff_is_none(ledger: Ledger, previous: str) -> None:
    ledger.record(A)
    args = ["lrclib-diff", B, "--staged", str(ledger.new)]
    if previous == "not sqlite":
        bogus = ledger.tmp / "bogus.sqlite3"
        bogus.write_bytes(b"not a database " * 100)
        args += ["--previous-db", str(bogus)]
    elif previous == "missing":
        args += ["--previous-db", str(ledger.tmp / "absent.sqlite3")]

    code, out, err = ledger.cli(*args)

    assert code == 0, err
    assert out == "diff none 0\n"
    assert ledger.changes(B) == 0
    marks = ledger.rows(f"SELECT max_track_id FROM lrclib_dumps WHERE name = '{A}'")
    assert marks == [(None,)]


def test_dump_id_stands_in_for_a_live_record(ledger: Ledger) -> None:
    code, out, err = ledger.cli(
        "lrclib-diff",
        B,
        "--staged",
        str(ledger.new),
        "--previous-db",
        str(ledger.old),
        PLYRIX_DUMP_ID=A,
    )

    assert code == 0, err
    assert out == "diff complete 80\n"
    assert ledger.dumps() == [(A, "live", "none", None), (B, "staged", "complete", A)]


def test_a_max_created_after_the_previous_dump_is_rejected(ledger: Ledger) -> None:
    ledger.record(A)
    set_created(ledger.old, "tracks", 200, "2026-11-20 00:00:00")

    code, out, err = ledger.cli(
        "lrclib-diff", B, "--staged", str(ledger.new), "--previous-db", str(ledger.old)
    )

    assert code == 0, err
    assert out == "diff none 0\n"
    assert ledger.rows(f"SELECT max_track_id FROM lrclib_dumps WHERE name = '{A}'") == [(None,)]


def test_maxes_above_the_staged_ones_are_rejected(ledger: Ledger) -> None:
    ledger.record(A)
    bigger = make_db(ledger.tmp / "bigger.sqlite3", 300)

    code, out, err = ledger.cli(
        "lrclib-diff", B, "--staged", str(ledger.new), "--previous-db", str(bigger)
    )

    assert code == 0, err
    assert out == "diff none 0\n"


def test_no_previous_dump_at_all_is_none(ledger: Ledger) -> None:
    code, out, err = ledger.cli("lrclib-diff", B, "--staged", str(ledger.new))

    assert code == 0, err
    assert out == "diff none 0\n"
    assert ledger.dumps() == [(B, "staged", "none", None)]


def test_renumbered_ids_make_the_diff_unstable(ledger: Ledger) -> None:
    ledger.record(A)
    ledger.remember(A, ledger.old)
    renumber(ledger.new)

    code, out, err = ledger.cli("lrclib-diff", B, "--staged", str(ledger.new))

    assert code == 0, err
    assert out == "diff unstable 0\n"
    assert ledger.changes(B) == 0


def test_a_staged_row_left_by_a_crash_is_replaced(ledger: Ledger) -> None:
    ledger.record(A)
    ledger.remember(A, ledger.old)
    conn = db.connect(state_db_path(ledger.tmp))
    db.stage_dump(conn, C, "2026-12-15T00:00:00+00:00", A, 8, 1, 1, None, None)
    db.add_changes(conn, C, [(1, 2, 3), (4, 5, 6)])
    conn.close()

    code, out, err = ledger.cli("lrclib-diff", B, "--staged", str(ledger.new))

    assert code == 0, err
    assert [row[0] for row in ledger.dumps()] == [A, B]
    assert ledger.changes(C) == 0
    assert ledger.changes(B) == 80


def test_a_rollback_drops_the_diff_and_keeps_the_old_dump_live(ledger: Ledger) -> None:
    ledger.record(A)
    ledger.remember(A, ledger.old)
    ledger.cli("lrclib-diff", B, "--staged", str(ledger.new))

    ledger.record(B, "rolled-back")

    assert ledger.dumps() == [(A, "live", "none", None), (B, "rolled_back", "none", A)]
    assert ledger.changes(B) == 0


def test_pruning_keeps_only_change_sets_a_track_can_need(ledger: Ledger) -> None:
    conn = db.connect(state_db_path(ledger.tmp))
    try:
        for name, when in ((A, "2026-10-30"), (B, "2026-12-01"), (C, "2026-12-15")):
            db.stage_dump(conn, name, f"{when}T00:00:00+00:00", None, 8, 1, 1, None, None)
            db.add_changes(conn, name, [(1, 2, 3)])
            db.finish_diff(conn, name, "complete")
            db.record_live(conn, name, f"{when}T00:00:00+00:00", None)
            if name == A:
                # No track names a dump yet, so nothing may go.
                held = "SELECT count(*) FROM lrclib_changes WHERE dump = ?"
                assert conn.execute(held, (A,)).fetchone() == (1,)
        run_id = db.begin_run(conn, "test", B)
        conn.execute(
            "INSERT INTO tracks(path, size, mtime_ns, outcome, dump_id, matcher_version, run_id, "
            "checked_at) VALUES(?, 1, 1, 'no_match', ?, 1, ?, 'now')",
            (b"/lib/a.flac", B, run_id),
        )
        db.prune_changes(conn)
    finally:
        conn.close()

    assert (ledger.changes(A), ledger.changes(B), ledger.changes(C)) == (0, 0, 1)


def test_an_unseen_row_holds_change_sets_back_from_its_unseen_dump_only(ledger: Ledger) -> None:
    conn = db.connect(state_db_path(ledger.tmp))
    try:
        for name, when in ((A, "2026-10-30"), (B, "2026-12-01"), (C, "2026-12-15")):
            db.stage_dump(conn, name, f"{when}T00:00:00+00:00", None, 8, 1, 1, None, None)
            db.add_changes(conn, name, [(1, 2, 3)])
            db.finish_diff(conn, name, "complete")
            db.record_live(conn, name, f"{when}T00:00:00+00:00", None)
        run_id = db.begin_run(conn, "test", A)
        conn.execute(
            "INSERT INTO tracks(path, size, mtime_ns, outcome, dump_id, matcher_version, run_id, "
            "checked_at) VALUES(?, 1, 1, 'no_match', ?, 1, ?, 'now')",
            (b"/lib/gone.flac", A, run_id),
        )
        db.prune_changes(conn)
        held = "SELECT dump FROM lrclib_changes ORDER BY dump"
        assert conn.execute(held).fetchall() == [(B,), (C,)]
        assert db.pinning_rows(conn) == [(b"/lib/gone.flac", A, run_id)]

        db.set_unseen(conn, [b"/lib/gone.flac"], [], B)
        db.prune_changes(conn)
        result = db.track_result(conn, b"/lib/gone.flac")
    finally:
        conn.close()

    assert (ledger.changes(A), ledger.changes(B), ledger.changes(C)) == (0, 0, 1)
    assert result is not None and result.unseen and result.dump_id == A


def test_writing_a_row_or_reaching_it_clears_unseen(ledger: Ledger) -> None:
    conn = db.connect(state_db_path(ledger.tmp))
    try:
        run_id = db.begin_run(conn, "test", A)
        rows = [
            db.TrackRow(path, 1, 1, None, Outcome.NO_MATCH, None, A, 1, None, run_id)
            for path in (b"/lib/a.flac", b"/lib/b.flac")
        ]
        db.write_tracks(conn, rows, [])
        db.set_unseen(conn, [b"/lib/a.flac", b"/lib/b.flac"], [], A)
        marked = [db.track_result(conn, row.path) for row in rows]
        assert [result is not None and result.unseen for result in marked] == [True, True]
        db.write_tracks(conn, rows[:1], [])
        db.set_unseen(conn, [], [b"/lib/b.flac"], A)
        found = [db.track_result(conn, row.path) for row in rows]
    finally:
        conn.close()

    assert [result is not None and result.unseen for result in found] == [False, False]


def test_a_synced_row_on_the_first_dump_holds_no_change_set_back(ledger: Ledger) -> None:
    d = "lrclib-db-dump-20261231T000000Z.sqlite3.gz"
    conn = db.connect(state_db_path(ledger.tmp))
    try:
        db.stage_dump(conn, A, "2026-10-30T00:00:00+00:00", None, 8, 1, 1, None, None)
        db.record_live(conn, A, "2026-10-30T00:00:00+00:00", None)
        run_id = db.begin_run(conn, "test", A)
        insert = (
            "INSERT INTO tracks(path, size, mtime_ns, outcome, reason, dump_id, matcher_version, "
            "run_id, checked_at) VALUES(?, 1, 1, ?, ?, ?, 1, ?, 'now')"
        )
        # A synced row keeps A for good. The miss was asked again under C.
        conn.execute(insert, (b"/lib/a.flac", "synced", "", A, run_id))
        conn.execute(insert, (b"/lib/b.flac", "no_match", "not_found", A, run_id))
        for name, when in ((B, "2026-12-01"), (C, "2026-12-15"), (d, "2026-12-31")):
            db.stage_dump(conn, name, f"{when}T00:00:00+00:00", None, 8, 1, 1, None, None)
            db.add_changes(conn, name, [(1, 2, 3)])
            db.finish_diff(conn, name, "complete")
            db.record_live(conn, name, f"{when}T00:00:00+00:00", None)
            if name == C:
                conn.execute("UPDATE tracks SET dump_id = ? WHERE outcome = 'no_match'", (C,))
    finally:
        conn.close()

    assert (ledger.changes(B), ledger.changes(C), ledger.changes(d)) == (0, 0, 1)


def test_a_bad_name_exits_2_and_a_busy_ledger_75(ledger: Ledger) -> None:
    bad, _, _ = ledger.cli("lrclib-diff", "db.sqlite3", "--staged", str(ledger.new))
    missing, _, _ = ledger.cli("lrclib-diff", B, "--staged", str(ledger.tmp / "absent"))
    path = state_db_path(ledger.tmp)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(path)
    try:
        busy, _, err = ledger.cli("lrclib-diff", B, "--staged", str(ledger.new))
    finally:
        conn.close()

    assert (bad, missing, busy) == (2, 2, 75)
    assert "another plyrix run is using the state database" in err


def test_a_staged_file_that_isnt_sqlite_exits_1(ledger: Ledger) -> None:
    bogus = ledger.tmp / "staged.sqlite3"
    bogus.write_bytes(b"not a database " * 100)

    code, _, err = ledger.cli("lrclib-diff", B, "--staged", str(bogus))

    assert code == 1
    assert err.startswith("plyrix: the staged database can't be diffed")
