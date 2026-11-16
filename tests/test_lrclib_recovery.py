"""Prove lrclib-update.sh recovers from a crash at every step of its journaled swap.

Each test builds by hand the files a kill at one step leaves in LRCLIB's data folder, with the
journal naming that step, and a listing that names nothing newer, so the run ends once it has
recovered. Every database is invented, and LRCLIB's stop and start are logging fakes.
"""

import os
import shutil
import time
from pathlib import Path

import pytest

from conftest import StubLrclib, snap
from lrclib_fixture import DumpServer, dump_server, gz_bytes, listed, listing  # noqa: F401
from test_lrclib_update_script import NEW, OLD, PLYRIX, Update

JOURNAL = ".plyrix-update.journal"
OLD_NAMES = ("db.sqlite3", "db.sqlite3-wal", "db.sqlite3-shm")


@pytest.fixture
def update(tmp_path: Path, stub_lrclib: StubLrclib, dump_server: DumpServer) -> Update:  # noqa: F811
    made = Update(tmp_path, stub_lrclib, dump_server)
    # Nothing newer is listed, so each run ends once it has recovered.
    dump_server.listing_body = listing(listed(OLD, 1234))
    return made


def old_contents(update: Update) -> dict[str, bytes]:
    found = {name: (update.data / name).read_bytes() for name in OLD_NAMES}
    found["index"] = (update.data / "db.sqlite3.tantivy" / "meta.json").read_bytes()
    return found


def journal(update: Update, line: str) -> None:
    (update.data / JOURNAL).write_text(line, encoding="utf-8")


def aside(update: Update, *names: str) -> None:
    """Rename old files to their .previous names, as moving_old does one at a time."""
    for name in names:
        moved = name.replace("db.sqlite3", "db.sqlite3.previous", 1)
        (update.data / name).rename(update.data / moved)


def new_database_in_place(update: Update) -> None:
    (update.data / "db.sqlite3").write_bytes(update.new_db.read_bytes())
    (update.data / "db.sqlite3-wal").write_bytes(b"new wal")
    index = update.data / "db.sqlite3.tantivy"
    index.mkdir()
    (index / "meta.json").write_bytes(b"new index")


def previous_names(update: Update) -> list[str]:
    return sorted(p.name for p in update.data.iterdir() if ".previous" in p.name)


def test_a_crash_while_stopping_starts_lrclib_again(update: Update) -> None:
    before = {name: snap(update.data / name) for name in OLD_NAMES}
    (update.data / "db.sqlite3.staged").write_bytes(update.new_db.read_bytes())
    journal(update, f"stopping {NEW}\n")

    proc = update.run()

    assert proc.returncode == 0, proc.stderr
    assert update.events() == ["start staged=no"]
    assert not (update.data / "db.sqlite3.staged").exists()
    assert not (update.data / JOURNAL).exists()
    assert {name: snap(update.data / name) for name in OLD_NAMES} == before


def test_a_crash_while_moving_the_old_set_puts_it_back(update: Update) -> None:
    before = old_contents(update)
    aside(update, "db.sqlite3")
    journal(update, f"moving_old {NEW}\n")

    proc = update.run()

    assert proc.returncode == 0, proc.stderr
    assert old_contents(update) == before
    assert previous_names(update) == []
    assert [e for e in update.events() if e.startswith("start")] == ["start staged=no"]
    assert update.dump_statuses() == [(NEW, "rolled_back")]
    assert "put back" in proc.stdout


def test_a_crash_while_moving_the_new_one_in_puts_the_old_set_back(update: Update) -> None:
    before = old_contents(update)
    aside(update, *OLD_NAMES, "db.sqlite3.tantivy")
    (update.data / "db.sqlite3.staged").write_bytes(update.new_db.read_bytes())
    journal(update, f"moving_new {NEW}\n")

    proc = update.run()

    assert proc.returncode == 0, proc.stderr
    assert old_contents(update) == before
    assert previous_names(update) == []
    assert not (update.data / "db.sqlite3.staged").exists()
    assert [e for e in update.events() if e.startswith("start")] == ["start staged=no"]
    assert update.dump_statuses() == [(NEW, "rolled_back")]


def test_a_crash_while_starting_removes_the_new_set_and_restores_the_old(update: Update) -> None:
    before = old_contents(update)
    aside(update, *OLD_NAMES, "db.sqlite3.tantivy")
    new_database_in_place(update)
    journal(update, f"starting {NEW}\n")

    proc = update.run()

    assert proc.returncode == 0, proc.stderr
    assert old_contents(update) == before
    assert previous_names(update) == []
    assert update.dump_statuses() == [(NEW, "rolled_back")]
    assert (update.data / JOURNAL).read_text(encoding="utf-8") == f"rolled_back {NEW}\n"


def test_a_crash_after_the_swap_finishes_the_live_record(update: Update) -> None:
    aside(update, *OLD_NAMES, "db.sqlite3.tantivy")
    new_database_in_place(update)
    journal(update, f"swapped {NEW}\n")
    update.server.listing_body = listing(listed(NEW, 1234))

    proc = update.run()

    assert proc.returncode == 0, proc.stderr
    assert update.events() == []
    assert update.dump_statuses() == [(NEW, "live")]
    assert previous_names(update) == []
    assert not (update.data / JOURNAL).exists()
    assert (update.data / "db.sqlite3").read_bytes() == update.new_db.read_bytes()


def test_a_staged_database_left_before_any_swap_is_removed(update: Update) -> None:
    # A run killed during its verify or diff leaves the staged file and no journal.
    before = {name: snap(update.data / name) for name in OLD_NAMES}
    (update.data / "db.sqlite3.staged").write_bytes(update.new_db.read_bytes())

    proc = update.run()

    assert proc.returncode == 0, proc.stderr
    assert not (update.data / "db.sqlite3.staged").exists()
    assert "removing db.sqlite3.staged" in proc.stdout
    assert update.events() == []
    assert {name: snap(update.data / name) for name in OLD_NAMES} == before


@pytest.mark.parametrize("step", ["live", "rolled_back"])
def test_a_finished_journal_is_removed_and_nothing_else(update: Update, step: str) -> None:
    before = update.data_snaps()
    journal(update, f"{step} {NEW}\n")

    proc = update.run()

    assert proc.returncode == 0, proc.stderr
    assert not (update.data / JOURNAL).exists()
    assert update.data_snaps() == before
    assert update.events() == []


@pytest.mark.parametrize("step", ["stopping", "moving_old", "moving_new", "starting"])
def test_lrclib_is_never_started_with_no_database(update: Update, step: str) -> None:
    for name in OLD_NAMES:
        (update.data / name).unlink()
    journal(update, f"{step} {NEW}\n")
    (update.data / "db.sqlite3.staged").write_bytes(update.new_db.read_bytes())
    before = sorted(
        p.name
        for p in update.data.rglob("*")
        if p.name not in (".plyrix-update.lock", JOURNAL, "db.sqlite3.staged")
    )

    proc = update.run()

    assert proc.returncode == 1
    last = proc.stderr.strip().splitlines()[-1]
    assert "before any database was in place" in last and "lrclib-setup.sh" in last
    assert update.events() == []
    # The journal goes, so the next run, setup's included, doesn't stop here again.
    assert (
        sorted(p.name for p in update.data.rglob("*") if p.name != ".plyrix-update.lock") == before
    )
    assert update.server.requests == []

    again = update.run()

    assert "before any database was in place" not in again.stderr
    assert update.events() == []


@pytest.mark.parametrize("step", ["moving_new", "starting"])
def test_a_first_load_with_its_database_in_place_is_started_and_not_rolled_back(
    update: Update, step: str
) -> None:
    shutil.rmtree(update.data / "db.sqlite3.tantivy")
    for name in OLD_NAMES:
        (update.data / name).unlink()
    new_database_in_place(update)
    journal(update, f"{step} {NEW}\n")

    proc = update.run()

    assert [e for e in update.events() if e.startswith("start")] == ["start staged=no"]
    assert (update.data / "db.sqlite3").read_bytes() == update.new_db.read_bytes()
    assert previous_names(update) == []
    assert not (update.data / JOURNAL).exists()
    # Nothing records it rolled back, so the ledger may not even exist yet.
    assert not update.state_db.exists() or (NEW, "rolled_back") not in update.dump_statuses()
    assert "put back" not in proc.stdout
    assert f"lrclib-record {NEW} --status live" in proc.stderr


@pytest.mark.parametrize(
    "line",
    ["bogus\n", f"exploding {NEW}\n", "starting ../x\n", f"starting {NEW} extra\n", ""],
)
def test_a_journal_line_the_script_doesnt_know_touches_nothing(update: Update, line: str) -> None:
    journal(update, line)
    before = update.data_snaps()

    proc = update.run()

    assert proc.returncode == 1
    assert JOURNAL in proc.stderr.strip().splitlines()[-1]
    assert update.data_snaps() == before
    assert update.events() == []


# Keeping the previous database for LRCLIB_KEEP_PREVIOUS_DAYS


def kept(update: Update) -> list[str]:
    return previous_names(update)


def test_keep_days_keeps_the_previous_set_with_a_marker(update: Update) -> None:
    update.server.offer(NEW, gz_bytes(update.new_db))

    proc = update.run(LRCLIB_KEEP_PREVIOUS_DAYS="1")

    assert proc.returncode == 0, proc.stderr
    assert kept(update) == [
        "db.sqlite3.previous",
        "db.sqlite3.previous-shm",
        "db.sqlite3.previous-wal",
        "db.sqlite3.previous.kept",
        "db.sqlite3.previous.tantivy",
    ]


def test_a_kept_set_goes_once_its_days_have_passed(update: Update) -> None:
    update.server.offer(NEW, gz_bytes(update.new_db))
    assert update.run(LRCLIB_KEEP_PREVIOUS_DAYS="1").returncode == 0
    marker = update.data / "db.sqlite3.previous.kept"
    two_days_ago = time.time() - 2 * 86400
    os.utime(marker, (two_days_ago, two_days_ago))

    proc = update.run(LRCLIB_KEEP_PREVIOUS_DAYS="1")

    assert proc.returncode == 0, proc.stderr
    assert kept(update) == []
    assert "kept 1 days, so it was deleted" in proc.stdout


def test_a_kept_set_younger_than_its_days_stays(update: Update) -> None:
    update.server.offer(NEW, gz_bytes(update.new_db))
    assert update.run(LRCLIB_KEEP_PREVIOUS_DAYS="2").returncode == 0

    proc = update.run(LRCLIB_KEEP_PREVIOUS_DAYS="2")

    assert proc.returncode == 0, proc.stderr
    assert "db.sqlite3.previous.kept" in kept(update)


def test_a_newer_dump_deletes_a_kept_set_before_doctor_counts_the_room(update: Update) -> None:
    update.server.offer(NEW, gz_bytes(update.new_db))
    assert update.run(LRCLIB_KEEP_PREVIOUS_DAYS="5").returncode == 0
    newer = "lrclib-db-dump-20261215T000000Z.sqlite3.gz"
    update.server.offer(newer, gz_bytes(update.new_db))
    seen = update.tmp / "at-doctor"
    shim = update.fake(
        "plyrix-doctor-look",
        f'if [ "$1" = doctor ]; then ls "{update.data}" >"{seen}"; fi\nexec "{PLYRIX}" "$@"',
    )

    proc = update.run(PLYRIX=str(shim), LRCLIB_KEEP_PREVIOUS_DAYS="5")

    assert proc.returncode == 0, proc.stderr
    assert "previous" not in seen.read_text(encoding="utf-8")


def test_a_bad_keep_days_exits_2(update: Update) -> None:
    proc = update.run(LRCLIB_KEEP_PREVIOUS_DAYS="x")

    assert proc.returncode == 2
    assert "LRCLIB_KEEP_PREVIOUS_DAYS" in proc.stderr
    assert update.server.requests == []
