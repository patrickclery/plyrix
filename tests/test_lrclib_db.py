"""Read-only access to LRCLIB-shaped databases: what verify accepts and refuses, and that it writes
nothing beside the file. Every database is invented (tests/lrclib_fixture.py)."""

import os
import re
import shutil
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import CANARY, RunCli, StubLrclib, copy_track, snap, state_db_path
from lrclib_fixture import (
    add_tracks,
    corrupt_page,
    drop_index,
    lyric_pages,
    make_db,
    renumber,
    replace_lyrics,
    set_created,
)
from plyrix.state import lrclib_db


def test_verify_accepts_the_fixture_and_probes_its_highest_track(tmp_path: Path) -> None:
    path = make_db(tmp_path / "db.sqlite3", 30, first_id=100)

    verified = lrclib_db.verify(path)

    assert verified == lrclib_db.Verified(
        user_version=8,
        probe_id=129,
        newest_created_at="2026-01-01 00:00:00",
        created_after_name=False,
    )


def test_the_supported_versions_are_one_to_eight() -> None:
    assert lrclib_db.LRCLIB_USER_VERSIONS == range(1, 9)


@pytest.mark.parametrize("version", [1, 7])
def test_an_older_version_the_server_migrates_is_accepted(tmp_path: Path, version: int) -> None:
    path = make_db(tmp_path / "db.sqlite3", 3, user_version=version)

    assert lrclib_db.verify(path).user_version == version


def not_sqlite(path: Path) -> Path:
    path.write_bytes(b"this is not a database, " * 200)
    return path


def without(table: str) -> Callable[[Path], Path]:
    def build(path: Path) -> Path:
        make_db(path, 3)
        con = sqlite3.connect(path)
        con.execute(f"DROP TABLE {table}")
        con.commit()
        con.close()
        return path

    return build


def versioned(version: int) -> Callable[[Path], Path]:
    def build(path: Path) -> Path:
        return make_db(path, 3, user_version=version)

    return build


def empty(path: Path) -> Path:
    return make_db(path, 0)


def without_column(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tracks (id INTEGER PRIMARY KEY, name TEXT)")
    con.execute("CREATE TABLE lyrics (id INTEGER PRIMARY KEY, track_id INTEGER)")
    con.execute("INSERT INTO tracks(name) VALUES('Invented')")
    con.execute("PRAGMA user_version=8")
    con.commit()
    con.close()
    return path


@pytest.mark.parametrize(
    ("build", "phrase"),
    [
        (not_sqlite, "isn't an SQLite database"),
        (without("tracks"), "no tracks table"),
        (without("lyrics"), "no lyrics table"),
        (versioned(0), "user_version is 0"),
        (versioned(9), "user_version is 9"),
        (empty, "holds no tracks"),
        (without_column, "last_lyrics_id columns"),
    ],
)
def test_verify_refuses_what_the_pinned_server_cant_serve(
    tmp_path: Path, build: Callable[[Path], Path], phrase: str
) -> None:
    path = build(tmp_path / "db.sqlite3")

    with pytest.raises(lrclib_db.NotServable) as refused:
        lrclib_db.verify(path)

    assert phrase in refused.value.reason
    assert "Invented" not in refused.value.reason


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write in any folder")
def test_a_wal_database_in_a_read_only_folder_is_read_without_a_write(tmp_path: Path) -> None:
    folder = tmp_path / "ro"
    folder.mkdir()
    path = make_db(folder / "db.sqlite3", 5)
    # The header still says WAL, as a real dump's does.
    assert path.read_bytes()[18:20] == b"\x02\x02"
    before = {entry.name: snap(entry) for entry in folder.iterdir()}
    folder.chmod(0o555)
    try:
        verified = lrclib_db.verify(path)
        conn = lrclib_db.open_read_only(path)
        try:
            assert conn.execute("SELECT count(*) FROM tracks").fetchone() == (5,)
        finally:
            conn.close()
    finally:
        folder.chmod(0o755)

    assert verified.probe_id == 5
    assert {entry.name: snap(entry) for entry in folder.iterdir()} == before


def test_open_read_only_refuses_a_write(tmp_path: Path) -> None:
    path = make_db(tmp_path / "db.sqlite3", 2)
    before = snap(path)
    conn = lrclib_db.open_read_only(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM tracks")
    finally:
        conn.close()

    assert snap(path) == before


def test_verify_selects_no_lyric_column() -> None:
    source = Path(lrclib_db.__file__).read_text(encoding="utf-8")

    # Whole words, as has_synced_lyrics, a flag, is a column verify reads.
    assert re.search(r"\b(plain_lyrics|synced_lyrics|lyricsfile)\b", source) is None


# The full verify (06-03): every column the diff and the server use, the index, quick_check

TRACK_COLUMNS = [
    "id",
    "name",
    "name_lower",
    "artist_name",
    "artist_name_lower",
    "duration",
    "last_lyrics_id",
]
LYRIC_COLUMNS = ["id", "track_id", "created_at", "has_synced_lyrics", "instrumental"]


def custom_db(path: Path, tracks: list[str], lyrics: list[str]) -> Path:
    """Write a database with only these columns, one invented row, and the index if it can."""
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE tracks ({', '.join(tracks)})")
    con.execute(f"CREATE TABLE lyrics ({', '.join(lyrics)})")
    if "last_lyrics_id" in tracks:
        con.execute("CREATE INDEX idx_tracks_last_lyrics_id ON tracks (last_lyrics_id)")
    values = ", ".join("1" for _ in tracks)
    con.execute(f"INSERT INTO tracks VALUES ({values})")
    con.execute("PRAGMA user_version=8")
    con.commit()
    con.close()
    return path


def test_every_needed_column_present_is_accepted(tmp_path: Path) -> None:
    path = custom_db(tmp_path / "db.sqlite3", TRACK_COLUMNS, LYRIC_COLUMNS)

    assert lrclib_db.verify(path).probe_id == 1


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("tracks", "name_lower"),
        ("tracks", "artist_name_lower"),
        ("tracks", "duration"),
        ("lyrics", "created_at"),
        ("lyrics", "has_synced_lyrics"),
        ("lyrics", "instrumental"),
    ],
)
def test_a_missing_column_is_refused_by_name(tmp_path: Path, table: str, column: str) -> None:
    tracks = [c for c in TRACK_COLUMNS if (table, c) != ("tracks", column)]
    lyrics = [c for c in LYRIC_COLUMNS if (table, c) != ("lyrics", column)]
    path = custom_db(tmp_path / "db.sqlite3", tracks, lyrics)

    with pytest.raises(lrclib_db.NotServable) as refused:
        lrclib_db.verify(path)

    assert refused.value.reason == f"its {table} table has no {column} column"


def test_a_missing_index_is_refused(tmp_path: Path) -> None:
    path = drop_index(make_db(tmp_path / "db.sqlite3", 5))

    with pytest.raises(lrclib_db.NotServable) as refused:
        lrclib_db.verify(path)

    assert refused.value.reason == "it has no idx_tracks_last_lyrics_id index"


def test_a_damaged_page_fails_quick_check_unless_it_is_off(tmp_path: Path) -> None:
    path = make_db(tmp_path / "db.sqlite3", 400)
    pages = lyric_pages(path)
    assert len(pages) > 2
    corrupt_page(path, pages[len(pages) // 2])

    with pytest.raises(lrclib_db.NotServable) as refused:
        lrclib_db.verify(path)

    assert refused.value.reason == "it fails SQLite's quick_check"
    assert lrclib_db.verify(path, quick_check=False).probe_id == 400


def test_verify_never_selects_a_lyric_column_or_shows_its_text(tmp_path: Path) -> None:
    path = make_db(tmp_path / "db.sqlite3", 20)
    statements: list[str] = []

    verified = lrclib_db.verify(path, trace=statements.append)

    assert statements
    for statement in statements:
        assert re.search(r"\b(plain_lyrics|synced_lyrics|lyricsfile)\b", statement) is None
    assert CANARY not in repr(verified)


def test_a_refusal_holds_no_database_text(tmp_path: Path) -> None:
    path = make_db(tmp_path / "db.sqlite3", 400)
    corrupt_page(path, lyric_pages(path)[1])

    with pytest.raises(lrclib_db.NotServable) as refused:
        lrclib_db.verify(path)

    assert CANARY not in str(refused.value) and "Invented" not in str(refused.value)


def test_a_newest_record_long_after_the_name_sets_the_flag(tmp_path: Path) -> None:
    path = make_db(tmp_path / "db.sqlite3", 3)
    a_week_before = datetime(2025, 12, 25, tzinfo=UTC)
    the_same_day = datetime(2026, 1, 1, tzinfo=UTC)

    assert lrclib_db.verify(path, name_time=a_week_before).created_after_name
    assert not lrclib_db.verify(path, name_time=the_same_day).created_after_name
    assert not lrclib_db.verify(path).created_after_name


def test_a_created_at_that_cant_be_read_is_ignored(tmp_path: Path) -> None:
    path = make_db(tmp_path / "db.sqlite3", 3)
    con = sqlite3.connect(path)
    con.execute("UPDATE lyrics SET created_at = 'not a date'")
    con.commit()
    con.close()

    verified = lrclib_db.verify(path, name_time=datetime(2020, 1, 1, tzinfo=UTC))

    assert verified.newest_created_at == "not a date"
    assert not verified.created_after_name


# lrclib-verify and lrclib-record through the real console script


def cli_env_for(tmp_path: Path, stub: StubLrclib) -> dict[str, str]:
    lib = tmp_path / "lib"
    copy_track(lib)
    return {"PLYRIX_MUSIC_ROOT": str(lib), "PLYRIX_LRCLIB_URL": stub.base_url}


NAMED = "lrclib-db-dump-20251225T000000Z.sqlite3.gz"


def test_lrclib_verify_warns_when_records_postdate_the_name(
    run_cli: RunCli, tmp_path: Path, stub_lrclib: StubLrclib
) -> None:
    path = make_db(tmp_path / "staged.sqlite3", 4)

    proc = run_cli(
        ["lrclib-verify", str(path), "--name", NAMED], cli_env_for(tmp_path, stub_lrclib)
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == b"ok 4\n"
    assert proc.stderr.decode().splitlines() == [
        "plyrix: the dump's newest record is dated after its name, so plyrix uses the name's date"
    ]
    assert CANARY.encode() not in proc.stdout + proc.stderr


def test_lrclib_verify_can_skip_quick_check(
    run_cli: RunCli, tmp_path: Path, stub_lrclib: StubLrclib
) -> None:
    path = make_db(tmp_path / "staged.sqlite3", 400)
    corrupt_page(path, lyric_pages(path)[2])
    env = cli_env_for(tmp_path, stub_lrclib)

    refused = run_cli(["lrclib-verify", str(path)], env)
    skipped = run_cli(["lrclib-verify", str(path), "--no-quick-check"], env)

    assert refused.returncode == 1
    assert refused.stdout == b"refused\n"
    assert refused.stderr.decode().strip() == (
        "plyrix: the staged database can't be served: it fails SQLite's quick_check"
    )
    assert skipped.returncode == 0, skipped.stderr
    assert skipped.stdout == b"ok 400\n"


@pytest.mark.parametrize("args", [["--name", "db.sqlite3"], ["--name", ""]])
def test_lrclib_verify_refuses_a_name_that_isnt_a_dump(
    run_cli: RunCli, tmp_path: Path, stub_lrclib: StubLrclib, args: list[str]
) -> None:
    path = make_db(tmp_path / "staged.sqlite3", 2)

    proc = run_cli(["lrclib-verify", str(path), *args], cli_env_for(tmp_path, stub_lrclib))

    assert proc.returncode == 2


def test_lrclib_record_refused_and_never_the_live_dump(
    run_cli: RunCli, tmp_path: Path, stub_lrclib: StubLrclib
) -> None:
    env = cli_env_for(tmp_path, stub_lrclib)
    assert run_cli(["lrclib-record", NAMED, "--status", "live"], env).returncode == 0
    other = "lrclib-db-dump-20261201T000000Z.sqlite3.gz"

    refused = run_cli(["lrclib-record", other, "--status", "refused"], env)
    live = run_cli(["lrclib-record", NAMED, "--status", "refused"], env)
    bad = run_cli(["lrclib-record", other, "--status", "gone"], env)

    assert refused.returncode == 0, refused.stderr
    assert live.returncode == 2
    assert b"live dump" in live.stderr
    assert bad.returncode == 2
    con = sqlite3.connect(state_db_path(tmp_path))
    rows = con.execute("SELECT name, status FROM lrclib_dumps ORDER BY name").fetchall()
    con.close()
    assert rows == [(NAMED, "live"), (other, "refused")]


# The diff (06-05): two index range scans on the new database, and the bootstrap read


def old_and_new(tmp_path: Path) -> tuple[Path, Path, list[int], list[int]]:
    """Return an old database of 200 tracks and a new one with 50 more and 30 re-lyricked."""
    old = make_db(tmp_path / "old.sqlite3", 200)
    new = tmp_path / "new.sqlite3"
    shutil.copyfile(old, new)
    added = add_tracks(new, 50)
    relyricked = list(range(5, 155, 5))
    replace_lyrics(new, relyricked)
    return old, new, added, relyricked


def test_changed_finds_exactly_the_new_and_re_lyricked_tracks(tmp_path: Path) -> None:
    old, new, added, relyricked = old_and_new(tmp_path)
    marks = lrclib_db.watermarks(old)
    assert (marks.max_track_id, marks.max_lyrics_id) == (200, 200)

    found = [row for chunk in lrclib_db.changed(new, 200, 200) for row in chunk]

    ids = [row[0] for row in found]
    assert sorted(ids) == sorted(added + relyricked)
    assert len(ids) == len(set(ids)) == 80
    assert all(CANARY not in repr(row) for row in found)
    assert found[0][1:] == ("Invented Artist 5", "Invented Track 5")


def test_changed_keeps_to_its_batch_size(tmp_path: Path) -> None:
    _, new, _, _ = old_and_new(tmp_path)

    batches = list(lrclib_db.changed(new, 200, 200, batch=7))

    assert max(len(batch) for batch in batches) <= 7
    assert sum(len(batch) for batch in batches) == 80


def test_a_new_lyric_leaves_updated_at_alone(tmp_path: Path) -> None:
    old, new, _, relyricked = old_and_new(tmp_path)
    con_old, con_new = sqlite3.connect(old), sqlite3.connect(new)
    query = "SELECT updated_at, last_lyrics_id FROM tracks WHERE id = ?"
    before = con_old.execute(query, (relyricked[0],)).fetchone()
    after = con_new.execute(query, (relyricked[0],)).fetchone()
    con_old.close()
    con_new.close()

    # updated_at is the same, so it can't drive the diff; last_lyrics_id moved.
    assert before[0] == after[0]
    assert after[1] > before[1]


def test_neither_diff_query_scans_tracks(tmp_path: Path) -> None:
    _, new, _, _ = old_and_new(tmp_path)

    relyricked, added = lrclib_db.plans(new, 200, 200)

    assert "SCAN tracks" not in relyricked and "SCAN tracks" not in added
    assert "idx_tracks_last_lyrics_id" in relyricked
    assert "INTEGER PRIMARY KEY" in added


def test_watermarks_read_the_maxes_version_and_newest_created_at(tmp_path: Path) -> None:
    _, new, _, _ = old_and_new(tmp_path)

    marks = lrclib_db.watermarks(new)

    assert marks == lrclib_db.Watermarks(8, 250, 280, "2026-06-01 00:00:00")


def test_the_id_digest_is_stable_and_sensitive(tmp_path: Path) -> None:
    old, new, _, _ = old_and_new(tmp_path)
    digest = lrclib_db.id_digest(old, 200)

    assert digest is not None
    assert lrclib_db.id_digest(new, 200) == digest
    assert lrclib_db.id_digest(old, 0) is None
    assert lrclib_db.id_digest(old, None) is None

    changed_name = tmp_path / "changed.sqlite3"
    shutil.copyfile(new, changed_name)
    con = sqlite3.connect(changed_name)
    con.execute("UPDATE tracks SET name_lower = 'renamed' WHERE id = 200")
    con.commit()
    con.close()
    assert lrclib_db.id_digest(changed_name, 200) != digest

    renumber(new)
    assert lrclib_db.id_digest(new, 200) != digest


def test_bootstrap_marks_reads_the_live_database(tmp_path: Path) -> None:
    old = make_db(tmp_path / "old.sqlite3", 20)

    found = lrclib_db.bootstrap_marks(old, datetime(2026, 1, 5, tzinfo=UTC))

    assert found is not None
    marks, digest = found
    assert (marks.max_track_id, marks.max_lyrics_id) == (20, 20)
    assert digest == lrclib_db.id_digest(old, 20)


def test_bootstrap_marks_refuses_a_max_created_after_the_dump(tmp_path: Path) -> None:
    old = make_db(tmp_path / "old.sqlite3", 20)
    set_created(old, "tracks", 20, "2026-03-01 00:00:00")

    assert lrclib_db.bootstrap_marks(old, datetime(2026, 1, 5, tzinfo=UTC)) is None


def test_bootstrap_marks_refuses_a_created_at_that_cant_be_read(tmp_path: Path) -> None:
    old = make_db(tmp_path / "old.sqlite3", 20)
    set_created(old, "lyrics", 20, "not a date")

    assert lrclib_db.bootstrap_marks(old, datetime(2026, 1, 5, tzinfo=UTC)) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write in any folder")
def test_bootstrap_marks_in_a_read_only_folder_needs_two_agreeing_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "ro"
    folder.mkdir()
    old = make_db(folder / "db.sqlite3", 20)
    before = {entry.name: snap(entry) for entry in folder.iterdir()}
    folder.chmod(0o555)
    try:
        agreed = lrclib_db.bootstrap_marks(old, datetime(2026, 1, 5, tzinfo=UTC))
        reads = iter(
            [lrclib_db._read_marks_immutably(old), None, lrclib_db._read_marks_immutably(old)]
        )
        monkeypatch.setattr(lrclib_db, "_read_marks_immutably", lambda path: next(reads))
        torn = lrclib_db.bootstrap_marks(old, datetime(2026, 1, 5, tzinfo=UTC))
    finally:
        folder.chmod(0o755)

    assert agreed is not None and agreed[0].max_track_id == 20
    assert torn is None
    assert {entry.name: snap(entry) for entry in folder.iterdir()} == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write in any folder")
def test_bootstrap_marks_refuses_two_reads_that_disagree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "ro"
    folder.mkdir()
    old = make_db(folder / "db.sqlite3", 20)
    folder.chmod(0o555)
    try:
        first = lrclib_db._read_marks_immutably(old)
        assert first is not None
        other = (first[0]._replace(max_track_id=21), first[1], first[2])
        reads = iter([first, other])
        monkeypatch.setattr(lrclib_db, "_read_marks_immutably", lambda path: next(reads))
        found = lrclib_db.bootstrap_marks(old, datetime(2026, 1, 5, tzinfo=UTC))
    finally:
        folder.chmod(0o755)

    assert found is None
