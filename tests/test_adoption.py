"""What plyrix does about a sidecar it can't prove it wrote, including after losing its ledger."""

import hashlib
import os
import sqlite3
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    CANARY,
    RECORD,
    RunCli,
    StubLrclib,
    audited_run,
    copy_track,
    leaks,
    ledger_rows,
    nonzero,
    parse_counts,
    run_lib,
    snap,
    state_db_path,
)
from plyrix.sidecars import encode

FOREIGN = b"not written by plyrix\n"
PLAIN_ONLY = {**RECORD, "syncedLyrics": None}
INSTRUMENTAL = {**RECORD, "instrumental": True, "syncedLyrics": None, "plainLyrics": None}
NOT_FOUND: tuple[int, object] = (404, {"name": "TrackNotFound"})
SYNCED_BYTES = encode(str(RECORD["syncedLyrics"]))
PLAIN_BYTES = encode(str(RECORD["plainLyrics"]))
OTHER_TIMED = f"[00:00.70] {CANARY} someone's own line\n[00:01.20] {CANARY} and another\n".encode()
TWO_MIB = 2 * 1024 * 1024


def query(tmp_path: Path, sql: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(state_db_path(tmp_path))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def sidecar_rows(tmp_path: Path) -> list[tuple[Any, ...]]:
    return query(
        tmp_path,
        "SELECT status, origin, refresh_pending, lrclib_id, sha256, size "
        "FROM sidecars ORDER BY path",
    )


def lose_ledger(tmp_path: Path) -> None:
    state_db_path(tmp_path).unlink()


def test_foreign_txt_does_not_block_a_synced_match(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    txt = track.with_suffix(".txt")
    txt.write_bytes(FOREIGN)
    before = snap(txt), snap(track)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert track.with_suffix(".lrc").read_bytes() == encode(str(RECORD["syncedLyrics"]))
    assert (snap(txt), snap(track)) == before
    assert [os.path.basename(row[3]) for row in ledger_rows(tmp_path)] == [b"t.lrc"]
    assert leaks(tmp_path, proc, lib) == []

    second = run_lib(run_cli, stub_lrclib, lib)

    assert nonzero(second.stdout) == {"synced": 1, "skipped_unchanged": 1}
    assert (snap(txt), snap(track)) == before


def test_foreign_txt_with_a_plain_match_stays(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    txt = track.with_suffix(".txt")
    txt.write_bytes(FOREIGN)
    before = snap(txt), snap(track)
    stub_lrclib.body = PLAIN_ONLY
    dump = {"PLYRIX_DUMP_ID": "dump-a"}

    proc = run_lib(run_cli, stub_lrclib, lib, **dump)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"blocked_foreign": 1, "looked_up": 1}
    assert (snap(txt), snap(track)) == before
    assert ledger_rows(tmp_path) == []
    assert query(tmp_path, "SELECT outcome, reason, dump_id FROM tracks") == [
        ("plain", "blocked_foreign", "dump-a")
    ]
    assert leaks(tmp_path, proc, lib) == []
    requests = len(stub_lrclib.requests)

    second = run_lib(run_cli, stub_lrclib, lib, **dump)

    assert second.returncode == 0, second.stderr
    assert nonzero(second.stdout) == {"blocked_foreign": 1, "skipped_unchanged": 1}
    assert len(stub_lrclib.requests) == requests
    assert (snap(txt), snap(track)) == before


def test_a_foreign_txt_that_goes_away_lets_the_plain_text_land(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    txt = track.with_suffix(".txt")
    txt.write_bytes(FOREIGN)
    stub_lrclib.body = PLAIN_ONLY
    dump = {"PLYRIX_DUMP_ID": "dump-a"}
    run_lib(run_cli, stub_lrclib, lib, **dump)
    txt.unlink()

    proc = run_lib(run_cli, stub_lrclib, lib, **dump)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"plain": 1, "written_txt": 1, "looked_up": 1}
    assert txt.read_bytes() == encode(str(RECORD["plainLyrics"]))
    assert query(tmp_path, "SELECT outcome, reason FROM tracks") == [("plain", "")]


def swap_for_other_text(txt: Path) -> None:
    txt.write_bytes(FOREIGN)


def swap_for_a_folder(txt: Path) -> None:
    txt.unlink()
    txt.mkdir()


@pytest.mark.parametrize("swap", [swap_for_other_text, swap_for_a_folder], ids=["text", "folder"])
def test_a_settled_plain_track_whose_txt_was_swapped_is_not_called_plain(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, swap: Callable[[Path], None]
) -> None:
    lib = tmp_path / "lib"
    txt = copy_track(lib).with_suffix(".txt")
    stub_lrclib.body = PLAIN_ONLY
    dump = {"PLYRIX_DUMP_ID": "dump-a"}
    first = run_lib(run_cli, stub_lrclib, lib, **dump)
    assert nonzero(first.stdout) == {"plain": 1, "written_txt": 1, "looked_up": 1}
    swap(txt)
    before = snap(txt)

    proc = run_lib(run_cli, stub_lrclib, lib, **dump)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"blocked_foreign": 1, "looked_up": 1}
    assert snap(txt) == before
    requests = len(stub_lrclib.requests)

    settled = run_lib(run_cli, stub_lrclib, lib, **dump)

    assert nonzero(settled.stdout) == {"blocked_foreign": 1, "skipped_unchanged": 1}
    assert len(stub_lrclib.requests) == requests
    assert snap(txt) == before


def test_a_blocked_txt_put_back_as_plyrix_wrote_it_is_plain_again(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    txt = copy_track(lib).with_suffix(".txt")
    stub_lrclib.body = PLAIN_ONLY
    dump = {"PLYRIX_DUMP_ID": "dump-a"}
    run_lib(run_cli, stub_lrclib, lib, **dump)
    txt.write_bytes(FOREIGN)
    run_lib(run_cli, stub_lrclib, lib, **dump)
    txt.write_bytes(PLAIN_BYTES)

    proc = run_lib(run_cli, stub_lrclib, lib, **dump)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"plain": 1, "looked_up": 1}
    assert query(tmp_path, "SELECT outcome, reason FROM tracks") == [("plain", "")]

    settled = run_lib(run_cli, stub_lrclib, lib, **dump)

    assert nonzero(settled.stdout) == {"plain": 1, "skipped_unchanged": 1}


def test_an_edited_txt_with_a_plain_match_stays(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    txt = track.with_suffix(".txt")
    stub_lrclib.body = PLAIN_ONLY
    run_lib(run_cli, stub_lrclib, lib)
    rows_before = ledger_rows(tmp_path)
    txt.write_bytes(FOREIGN)
    before = snap(txt)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"blocked_foreign": 1, "looked_up": 1}
    assert snap(txt) == before
    assert ledger_rows(tmp_path) == rows_before


def _plant_dangling_symlink(path: Path) -> None:
    path.symlink_to("nowhere")


def _plant_symlink_to_audio(path: Path) -> None:
    path.symlink_to("t.flac")


def _plant_directory(path: Path) -> None:
    path.mkdir()


def _plant_fifo(path: Path) -> None:
    os.mkfifo(path)


@pytest.mark.parametrize(
    "plant",
    [_plant_dangling_symlink, _plant_symlink_to_audio, _plant_directory, _plant_fifo],
)
@pytest.mark.parametrize(
    ("body", "counts", "lrc_written"),
    [
        (PLAIN_ONLY, {"blocked_foreign": 1, "looked_up": 1}, False),
        (RECORD, {"synced": 1, "written_lrc": 1, "looked_up": 1}, True),
    ],
    ids=["plain", "synced"],
)
def test_anything_else_at_the_txt_name_is_left_alone(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    plant: Callable[[Path], None],
    body: dict[str, object],
    counts: dict[str, int],
    lrc_written: bool,
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    txt = track.with_suffix(".txt")
    plant(txt)
    audio_before = snap(track)
    entry_before = os.lstat(txt)
    stub_lrclib.body = body

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == counts
    assert os.lstat(txt).st_ino == entry_before.st_ino
    assert os.lstat(txt).st_mtime_ns == entry_before.st_mtime_ns
    assert stat.S_IFMT(os.lstat(txt).st_mode) == stat.S_IFMT(entry_before.st_mode)
    assert track.with_suffix(".lrc").exists() is lrc_written
    assert snap(track) == audio_before


@pytest.mark.parametrize(
    ("answer", "counts", "row"),
    [
        ((200, INSTRUMENTAL), {"instrumental": 1}, ("instrumental", "")),
        (NOT_FOUND, {"no_match": 1, "no_match_not_found": 1}, ("no_match", "not_found")),
    ],
    ids=["instrumental", "no_match"],
)
def test_a_track_with_no_lyrics_writes_nothing_beside_a_foreign_txt(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    answer: tuple[int, object],
    counts: dict[str, int],
    row: tuple[str, str],
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    txt = track.with_suffix(".txt")
    txt.write_bytes(FOREIGN)
    before = snap(txt)
    stub_lrclib.status, stub_lrclib.body = answer

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {**counts, "looked_up": 1}
    assert snap(txt) == before
    assert not track.with_suffix(".lrc").exists()
    assert ledger_rows(tmp_path) == []
    assert query(tmp_path, "SELECT outcome, reason FROM tracks") == [row]


@pytest.mark.parametrize(
    ("answer", "counts", "row"),
    [
        ((200, INSTRUMENTAL), {"instrumental": 1}, ("instrumental", "")),
        (NOT_FOUND, {"no_match": 1, "no_match_not_found": 1}, ("no_match", "not_found")),
    ],
    ids=["instrumental", "no_match"],
)
def test_a_track_with_no_lyrics_keeps_its_own_txt(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    answer: tuple[int, object],
    counts: dict[str, int],
    row: tuple[str, str],
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    txt = track.with_suffix(".txt")
    stub_lrclib.body = PLAIN_ONLY
    run_lib(run_cli, stub_lrclib, lib)
    before = snap(txt)
    stub_lrclib.status, stub_lrclib.body = answer

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {**counts, "looked_up": 1}
    assert snap(txt) == before
    assert query(tmp_path, "SELECT outcome, reason FROM tracks") == [row]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_a_foreign_txt_that_cant_be_read_counts_read_failed_with_no_row(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    txt = track.with_suffix(".txt")
    txt.write_bytes(FOREIGN)
    txt.chmod(0)
    try:
        proc = run_lib(run_cli, stub_lrclib, lib)
    finally:
        txt.chmod(0o644)

    assert proc.returncode == 3, proc.stderr
    assert nonzero(proc.stdout) == {"read_failed": 1}
    assert stub_lrclib.requests == []
    assert query(tmp_path, "SELECT count(*) FROM tracks") == [(0,)]
    assert txt.read_bytes() == FOREIGN


def everything_in(lib: Path) -> dict[Path, tuple[str, int, int, int, int, int, int]]:
    """Identify every file and folder under lib, and lib itself."""
    return {path: snap(path) for path in (lib, *lib.rglob("*"))}


def test_adoption_deleting_the_ledger_changes_no_file(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, album="Synced")
    copy_track(lib, album="Plain")
    copy_track(lib, album="Foreign").with_suffix(".txt").write_bytes(FOREIGN)
    run_lib(run_cli, stub_lrclib, lib / "Synced")
    stub_lrclib.body = PLAIN_ONLY
    run_lib(run_cli, stub_lrclib, lib)
    before = everything_in(lib)
    lose_ledger(tmp_path)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {
        "synced": 1,
        "plain": 1,
        "blocked_foreign": 1,
        "adopted": 2,
        "looked_up": 2,
    }
    assert everything_in(lib) == before
    assert sorted(row[3] for row in ledger_rows(tmp_path)) == [
        os.fsencode(lib / "Plain" / "t.txt"),
        os.fsencode(lib / "Synced" / "t.lrc"),
    ]
    assert [row[0] for row in ledger_rows(tmp_path)] == ["published", "published"]
    assert leaks(tmp_path, proc, lib) == []

    again = run_lib(run_cli, stub_lrclib, lib)

    assert again.returncode == 0, again.stderr
    assert nonzero(again.stdout) == {
        "synced": 1,
        "plain": 1,
        "blocked_foreign": 1,
        "skipped_unchanged": 1,
        "looked_up": 2,
    }
    assert everything_in(lib) == before


def test_adoption_valid_lrc_is_adopted_with_no_lookup(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    run_lib(run_cli, stub_lrclib, lib)
    lose_ledger(tmp_path)
    requests = len(stub_lrclib.requests)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "adopted": 1}
    assert len(stub_lrclib.requests) == requests
    assert sidecar_rows(tmp_path) == [
        (
            "published",
            "timed",
            1,
            None,
            hashlib.sha256(SYNCED_BYTES).hexdigest(),
            len(SYNCED_BYTES),
        )
    ]
    assert query(tmp_path, "SELECT outcome, reason FROM tracks") == [("synced", "")]
    assert leaks(tmp_path, proc, lib) == []


def test_adoption_of_someone_elses_timed_lrc_prints_and_stores_none_of_it(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    sidecar = copy_track(lib).with_suffix(".lrc")
    sidecar.write_bytes(OTHER_TIMED)
    before = snap(sidecar)

    proc = run_cli(["run", lib, "--verbose"], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "adopted": 1}
    assert snap(sidecar) == before
    assert stub_lrclib.requests == []
    assert leaks(tmp_path, proc, lib) == []


def test_adoption_is_stable(tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    run_lib(run_cli, stub_lrclib, lib, PLYRIX_DUMP_ID="dump-a")
    lose_ledger(tmp_path)

    first, opened = audited_run(tmp_path, stub_lrclib, lib)

    assert nonzero(first.stdout) == {"synced": 1, "adopted": 1}
    assert os.fsdecode(track) in opened
    requests = len(stub_lrclib.requests)

    second, opened = audited_run(tmp_path, stub_lrclib, lib)

    assert second.returncode == 0, second.stderr
    assert nonzero(second.stdout) == {"synced": 1, "skipped_unchanged": 1}
    assert opened == []
    assert len(stub_lrclib.requests) == requests


def test_adoption_txt_equal_to_the_record_is_proved(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    txt = track.with_suffix(".txt")
    stub_lrclib.body = PLAIN_ONLY
    run_lib(run_cli, stub_lrclib, lib)
    before = snap(txt)
    lose_ledger(tmp_path)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"plain": 1, "adopted": 1, "looked_up": 1}
    assert snap(txt) == before
    assert [row[:4] for row in sidecar_rows(tmp_path)] == [("published", "proved", 1, 7)]
    assert query(tmp_path, "SELECT outcome, reason FROM tracks") == [("plain", "")]
    assert leaks(tmp_path, proc, lib) == []


@pytest.mark.parametrize("dump_changes", [False, True], ids=["no_dump_id", "new_dump"])
def test_adoption_a_txt_plyrix_only_adopted_is_never_removed(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, dump_changes: bool
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    txt = track.with_suffix(".txt")
    txt.write_bytes(PLAIN_BYTES)
    before = snap(txt)
    stub_lrclib.body = PLAIN_ONLY
    first_dump = {"PLYRIX_DUMP_ID": "dump-a"} if dump_changes else {}
    later_dump = {"PLYRIX_DUMP_ID": "dump-b"} if dump_changes else {}

    adopted = run_lib(run_cli, stub_lrclib, lib, **first_dump)

    assert nonzero(adopted.stdout) == {"plain": 1, "adopted": 1, "looked_up": 1}
    assert [row[:2] for row in sidecar_rows(tmp_path)] == [("published", "proved")]

    stub_lrclib.body = RECORD
    synced = run_lib(run_cli, stub_lrclib, lib, **later_dump)

    assert synced.returncode == 0, synced.stderr
    assert nonzero(synced.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert track.with_suffix(".lrc").read_bytes() == SYNCED_BYTES
    assert snap(txt) == before

    again = run_lib(run_cli, stub_lrclib, lib, **later_dump)

    assert nonzero(again.stdout) == {"synced": 1, "skipped_unchanged": 1}
    assert snap(txt) == before
    assert sorted(row[:2] for row in sidecar_rows(tmp_path)) == [
        ("published", "proved"),
        ("published", "written"),
    ]


def adopted_txt_beside_a_synced_lrc(
    tmp_path: Path, stub: StubLrclib, run_cli: RunCli
) -> tuple[Path, Path]:
    """Adopt a .txt under one dump, then get a .lrc beside it under another."""
    lib = tmp_path / "lib"
    txt = copy_track(lib).with_suffix(".txt")
    txt.write_bytes(PLAIN_BYTES)
    stub.body = PLAIN_ONLY
    adopted = run_lib(run_cli, stub, lib, PLYRIX_DUMP_ID="dump-a")
    assert nonzero(adopted.stdout) == {"plain": 1, "adopted": 1, "looked_up": 1}
    stub.body = RECORD
    synced = run_lib(run_cli, stub, lib, PLYRIX_DUMP_ID="dump-b")
    assert nonzero(synced.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    return lib, txt


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_adoption_an_unreadable_adopted_txt_beside_a_synced_lrc_doesnt_fail_the_track(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib, txt = adopted_txt_beside_a_synced_lrc(tmp_path, stub_lrclib, run_cli)
    before = txt.read_bytes()
    txt.chmod(0)
    try:
        proc = run_lib(run_cli, stub_lrclib, lib, PLYRIX_DUMP_ID="dump-b")
    finally:
        txt.chmod(0o644)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "skipped_unchanged": 1}
    assert txt.read_bytes() == before


def test_adoption_an_edited_adopted_txt_beside_a_synced_lrc_is_not_opened(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib, txt = adopted_txt_beside_a_synced_lrc(tmp_path, stub_lrclib, run_cli)
    txt.write_bytes(FOREIGN)
    before = snap(txt)

    for _ in range(2):
        proc, opened = audited_run(tmp_path, stub_lrclib, lib, dump_id="dump-b")

        assert proc.returncode == 0, proc.stderr
        assert nonzero(proc.stdout) == {"synced": 1, "skipped_unchanged": 1}
        assert opened == []
    assert snap(txt) == before


def test_upgrade_a_txt_plyrix_wrote_is_still_removed(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    txt = track.with_suffix(".txt")
    stub_lrclib.body = PLAIN_ONLY
    written = run_lib(run_cli, stub_lrclib, lib)
    assert nonzero(written.stdout) == {"plain": 1, "written_txt": 1, "looked_up": 1}

    stub_lrclib.body = RECORD
    upgraded = run_lib(run_cli, stub_lrclib, lib)

    assert nonzero(upgraded.stdout) == {
        "synced": 1,
        "written_lrc": 1,
        "upgraded": 1,
        "looked_up": 1,
    }
    assert not txt.exists()
    assert track.with_suffix(".lrc").read_bytes() == SYNCED_BYTES


@pytest.mark.parametrize(
    "form",
    [
        str(RECORD["plainLyrics"]).encode(),
        b"\n" + str(RECORD["plainLyrics"]).encode() + b"\r\n\r\n",
    ],
    ids=["no-newline", "crlf-and-blank-lines"],
)
def test_adoption_txt_with_a_trailing_newline_difference_is_proved(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, form: bytes
) -> None:
    lib = tmp_path / "lib"
    txt = copy_track(lib).with_suffix(".txt")
    txt.write_bytes(form)
    before = snap(txt)
    stub_lrclib.body = PLAIN_ONLY

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert nonzero(proc.stdout) == {"plain": 1, "adopted": 1, "looked_up": 1}
    assert snap(txt) == before
    # The row holds the file's own bytes, not the encoded form they were compared in.
    assert sidecar_rows(tmp_path)[0][4:] == (hashlib.sha256(form).hexdigest(), len(form))

    again = run_lib(run_cli, stub_lrclib, lib)

    assert nonzero(again.stdout) == {"plain": 1, "looked_up": 1}
    assert snap(txt) == before


def test_adoption_txt_is_compared_with_every_revision(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    txt = copy_track(lib).with_suffix(".txt")
    revision = f"{CANARY} an older revision of the words"
    txt.write_bytes(encode(revision))
    stub_lrclib.body = PLAIN_ONLY
    stub_lrclib.search_body = [{**PLAIN_ONLY, "id": 8, "plainLyrics": revision}]

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert nonzero(proc.stdout) == {"plain": 1, "adopted": 1, "looked_up": 1}
    assert [row[:4] for row in sidecar_rows(tmp_path)] == [("published", "proved", 1, 8)]
    assert query(tmp_path, "SELECT lrclib_id FROM tracks") == [(8,)]


def test_adoption_a_different_txt_stays_foreign(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    txt = copy_track(lib).with_suffix(".txt")
    txt.write_bytes(PLAIN_BYTES + b"one more line\n")
    before = snap(txt)
    stub_lrclib.body = PLAIN_ONLY

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert nonzero(proc.stdout) == {"blocked_foreign": 1, "looked_up": 1}
    assert snap(txt) == before
    assert ledger_rows(tmp_path) == []


def test_adoption_an_edited_txt_equal_to_the_record_is_proved(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    txt = copy_track(lib).with_suffix(".txt")
    stub_lrclib.body = PLAIN_ONLY
    run_lib(run_cli, stub_lrclib, lib)
    edited = PLAIN_BYTES.rstrip(b"\n")
    txt.write_bytes(edited)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert nonzero(proc.stdout) == {"plain": 1, "adopted": 1, "looked_up": 1}
    assert txt.read_bytes() == edited
    assert sidecar_rows(tmp_path) == [
        ("published", "proved", 1, 7, hashlib.sha256(edited).hexdigest(), len(edited))
    ]


@pytest.mark.parametrize("contents", [b"", b"no timing here\n", b"[00:99.00\n"], ids=repr)
def test_adoption_empty_or_invalid_lrc_is_left(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, contents: bytes
) -> None:
    lib = tmp_path / "lib"
    sidecar = copy_track(lib).with_suffix(".lrc")
    sidecar.write_bytes(contents)
    before = snap(sidecar)

    for _ in range(2):
        proc = run_lib(run_cli, stub_lrclib, lib)

        assert proc.returncode == 0, proc.stderr
        assert nonzero(proc.stdout) == {"blocked_foreign": 1}
        assert snap(sidecar) == before
        assert ledger_rows(tmp_path) == []
    assert stub_lrclib.requests == []


def test_adoption_a_timing_that_cant_fit_the_track_is_left(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    sidecar = copy_track(lib).with_suffix(".lrc")
    sidecar.write_bytes(b"[09:00.00] a stamp far beyond the end\n")

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert nonzero(proc.stdout) == {"blocked_foreign": 1}
    assert ledger_rows(tmp_path) == []


def test_adoption_an_edited_owned_lrc_becomes_timed(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    sidecar = copy_track(lib).with_suffix(".lrc")
    run_lib(run_cli, stub_lrclib, lib, PLYRIX_DUMP_ID="dump-a")
    query_conn = sqlite3.connect(state_db_path(tmp_path))
    query_conn.execute("UPDATE sidecars SET refresh_pending = 0")
    query_conn.commit()
    query_conn.close()
    sidecar.write_bytes(OTHER_TIMED)
    requests = len(stub_lrclib.requests)

    proc = run_lib(run_cli, stub_lrclib, lib, PLYRIX_DUMP_ID="dump-a")

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1}
    assert sidecar.read_bytes() == OTHER_TIMED
    assert len(stub_lrclib.requests) == requests
    assert sidecar_rows(tmp_path) == [
        (
            "published",
            "timed",
            1,
            7,
            hashlib.sha256(OTHER_TIMED).hexdigest(),
            len(OTHER_TIMED),
        )
    ]

    settled = run_lib(run_cli, stub_lrclib, lib, PLYRIX_DUMP_ID="dump-a")

    assert nonzero(settled.stdout) == {"synced": 1, "skipped_unchanged": 1}

    # Edited again into something with no timing: it stays, and the row says what was last proved.
    rows = sidecar_rows(tmp_path)
    sidecar.write_bytes(b"not timed any more\n")

    blocked = run_lib(run_cli, stub_lrclib, lib, PLYRIX_DUMP_ID="dump-a")

    assert nonzero(blocked.stdout) == {"blocked_foreign": 1}
    assert sidecar.read_bytes() == b"not timed any more\n"
    assert sidecar_rows(tmp_path) == rows


def _oversized_lrc() -> bytes:
    line = b"[00:00.50] a valid timed line\n"
    return line * (TWO_MIB // len(line) + 1)


@pytest.mark.parametrize(
    ("suffix", "contents"),
    [
        (".lrc", _oversized_lrc()),
        (".lrc", b"\xff\xfe[00:00.50] not utf-8\n"),
        (".txt", PLAIN_BYTES + b" " * TWO_MIB + b"x\n"),
        (".txt", b"\xff" + PLAIN_BYTES),
    ],
    ids=["lrc-oversized", "lrc-undecodable", "txt-oversized", "txt-undecodable"],
)
def test_adoption_oversized_or_undecodable_is_foreign(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, suffix: str, contents: bytes
) -> None:
    lib = tmp_path / "lib"
    sidecar = copy_track(lib).with_suffix(suffix)
    sidecar.write_bytes(contents)
    before = snap(sidecar)
    stub_lrclib.body = PLAIN_ONLY

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["blocked_foreign"] == 1
    assert parse_counts(proc.stdout)["adopted"] == 0
    assert snap(sidecar) == before
    assert ledger_rows(tmp_path) == []


def test_adoption_a_timed_row_is_never_removed(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    """A .txt row can't get origin timed today, so this holds the rule if one ever does."""
    lib = tmp_path / "lib"
    track = copy_track(lib)
    txt = track.with_suffix(".txt")
    stub_lrclib.body = PLAIN_ONLY
    run_lib(run_cli, stub_lrclib, lib)
    conn = sqlite3.connect(state_db_path(tmp_path))
    conn.execute("UPDATE sidecars SET origin = 'timed'")
    conn.commit()
    conn.close()
    before = snap(txt)
    stub_lrclib.body = RECORD

    proc = run_lib(run_cli, stub_lrclib, lib)
    again = run_lib(run_cli, stub_lrclib, lib)

    assert nonzero(proc.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert nonzero(again.stdout) == {"synced": 1, "skipped_unchanged": 1}
    assert snap(txt) == before


def test_adoption_beside_a_corrupt_track_counts_unreadable(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    track.write_bytes(b"this is not a flac stream")
    sidecar = track.with_suffix(".lrc")
    sidecar.write_bytes(OTHER_TIMED)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert nonzero(proc.stdout) == {"unreadable": 1}
    assert sidecar.read_bytes() == OTHER_TIMED
    assert ledger_rows(tmp_path) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_adoption_a_lrc_that_cant_be_read_counts_read_failed(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    sidecar = copy_track(lib).with_suffix(".lrc")
    sidecar.write_bytes(OTHER_TIMED)
    sidecar.chmod(0)
    try:
        proc = run_lib(run_cli, stub_lrclib, lib)
    finally:
        sidecar.chmod(0o644)

    assert proc.returncode == 3, proc.stderr
    assert nonzero(proc.stdout) == {"read_failed": 1}
    assert ledger_rows(tmp_path) == []
