"""SAFE-04: a real SIGKILL at each point of a publish, then recovery through the real CLI.

Each crash runs in a child process, which replaces an os function or a ledger call with a kill and
then runs the real command, so production code carries no test hook. Output checks use byte sizes
and hashes only.
"""

import errno
import hashlib
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import FIXTURES, RECORD, RunCli, StubLrclib, parse_counts, snap, state_db_path
from plyrix import pipeline, sidecars
from plyrix.config import Settings
from plyrix.domain import Kind
from plyrix.state import db

CHILD = """
import errno, os, signal, sys

from plyrix import cli, sidecars
from plyrix.state import db


def kill(*_args, **_kwargs):
    os.kill(os.getpid(), signal.SIGKILL)


def no_hard_links(*_args, **_kwargs):
    raise OSError(errno.EPERM, "no hard links")


point, library = sys.argv[1:]
if point in ("during_write", "during_replace_write"):
    real_write = os.write

    def write_half_then_die(descriptor, data):
        real_write(descriptor, bytes(data[: len(data) // 2]))
        kill()

    os.write = write_half_then_die
elif point == "before_fsync":
    os.fsync = kill
elif point == "before_link":
    os.link = kill
elif point == "after_link_before_unlink":
    os.unlink = kill
elif point == "fallback_after_claim":
    os.link = no_hard_links
    os.replace = kill
elif point == "method_plain_after_claim":
    os.replace = kill
elif point == "fallback_before_claim_record":
    os.link = no_hard_links
    db.mark_claiming = kill
elif point == "before_claim_record_cleared":
    def replace_fails(*_args, **_kwargs):
        raise OSError(errno.EIO, "failed")

    os.link = no_hard_links
    os.replace = replace_fails
    db.clear_claiming = kill
elif point == "after_intent_before_publish":
    sidecars.publish = kill
elif point == "before_published_commit":
    db.mark_published = kill
elif point == "second_track_before_link":
    real_link = os.link
    links = []

    def link_once_then_die(*args, **kwargs):
        if links:
            kill()
        links.append(1)
        real_link(*args, **kwargs)

    os.link = link_once_then_die
elif point == "before_temp_record":
    db.add_temp = kill
elif point == "before_temp_forgotten":
    db.drop_temp = kill
elif point == "during_recovery":
    db.delete_row = kill
elif point == "before_tracks_flush":
    db.write_tracks = kill
elif point == "upgrade_before_txt_removed":
    sidecars.remove_owned = kill
elif point == "upgrade_before_row_dropped":
    db.delete_row = kill
elif point == "before_replace_owned":
    sidecars.replace_owned = kill
else:
    raise SystemExit("unknown crash point")
cli.app(["run", library])
"""

EXPECTED = sidecars.encode(str(RECORD["syncedLyrics"]))
FOREIGN = b"not written by plyrix\n"
TWO_HOURS = 7200
REAL_REPLACE = os.replace


def copy_track(lib: Path, name: str = "t.flac") -> Path:
    lib.mkdir(parents=True, exist_ok=True)
    return Path(shutil.copyfile(FIXTURES / "t.flac", lib / name))


def temps_in(directory: Path) -> list[Path]:
    return sorted(directory.glob(".plyrix-*.part"))


def crash_at(
    point: str,
    lib: Path,
    tmp_path: Path,
    stub: StubLrclib,
    dump_id: str = "",
    env: dict[str, str] | None = None,
) -> None:
    """Run the real command in a child until it kills itself, and require that it did."""
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("PLYRIX_")}
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, point, lib],
        env={
            **inherited,
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "PLYRIX_LRCLIB_URL": stub.base_url,
            "PLYRIX_DUMP_ID": dump_id,
            **(env or {}),
        },
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == -signal.SIGKILL, proc.stderr


def next_run(run_cli: RunCli, stub: StubLrclib, lib: Path, exit_code: int = 0) -> dict[str, int]:
    proc = run_cli(["run", lib], env={"PLYRIX_LRCLIB_URL": stub.base_url})
    assert proc.returncode == exit_code, proc.stderr
    return {key: value for key, value in parse_counts(proc.stdout).items() if value}


def ledger(tmp_path: Path) -> list[tuple[bytes, str, str, int]]:
    with sqlite3.connect(state_db_path(tmp_path)) as conn:
        rows = conn.execute("SELECT path, status, sha256, size FROM sidecars").fetchall()
    conn.close()
    return rows


def temp_rows(tmp_path: Path) -> list[bytes]:
    with sqlite3.connect(state_db_path(tmp_path)) as conn:
        rows = conn.execute("SELECT path FROM temps").fetchall()
    conn.close()
    return [row[0] for row in rows]


def untagged_track_elsewhere(lib: Path) -> None:
    """Give the library an audio file outside Album that writes nothing and asks LRCLIB nothing.

    A library with no audio stops before the ledger opens, so these runs need something to look at.
    """
    (lib / "Elsewhere").mkdir()
    shutil.copyfile(FIXTURES / "notags.flac", lib / "Elsewhere" / "u.flac")


@contextmanager
def unmounted(lib: Path) -> Iterator[None]:
    """Move the library away and leave an empty folder at its path, as an absent mount does."""
    away = lib.with_name(f"{lib.name}.away")
    lib.rename(away)
    lib.mkdir()
    try:
        yield
    finally:
        lib.rmdir()
        away.rename(lib)


def age(path: Path, seconds: int) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then), follow_symlinks=False)


@dataclass(frozen=True)
class CrashPoint:
    """What a kill at one point leaves on disk, and what the next run must do about it."""

    name: str
    sidecar_after_kill: bytes | None
    temps_after_kill: int
    next_counts: dict[str, int]


CRASH_POINTS = [
    CrashPoint("before_temp_record", None, 0, {"synced": 1, "written_lrc": 1, "looked_up": 1}),
    CrashPoint("during_write", None, 1, {"synced": 1, "written_lrc": 1, "looked_up": 1}),
    CrashPoint("before_fsync", None, 1, {"synced": 1, "written_lrc": 1, "looked_up": 1}),
    CrashPoint("before_link", None, 1, {"synced": 1, "written_lrc": 1, "looked_up": 1}),
    CrashPoint(
        "after_intent_before_publish", None, 0, {"synced": 1, "written_lrc": 1, "looked_up": 1}
    ),
    CrashPoint("after_link_before_unlink", EXPECTED, 1, {"synced": 1, "skipped_unchanged": 1}),
    CrashPoint("before_temp_forgotten", EXPECTED, 0, {"synced": 1, "skipped_unchanged": 1}),
    CrashPoint(
        "fallback_before_claim_record", None, 1, {"synced": 1, "written_lrc": 1, "looked_up": 1}
    ),
    CrashPoint("fallback_after_claim", b"", 1, {"synced": 1, "written_lrc": 1, "looked_up": 1}),
    CrashPoint(
        "before_claim_record_cleared", None, 1, {"synced": 1, "written_lrc": 1, "looked_up": 1}
    ),
    CrashPoint("before_published_commit", EXPECTED, 0, {"synced": 1, "skipped_unchanged": 1}),
]


@pytest.mark.parametrize("point", CRASH_POINTS, ids=lambda point: point.name)
def test_sigkill_leaves_no_partial_sidecar_and_the_next_run_recovers(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, point: CrashPoint
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    sidecar = track.with_suffix(".lrc")
    audio_before = snap(track)

    crash_at(point.name, lib, tmp_path, stub_lrclib)

    assert snap(track) == audio_before
    on_disk = sidecar.read_bytes() if sidecar.exists() else None
    assert on_disk == point.sidecar_after_kill
    temps = temps_in(lib)
    assert len(temps) == point.temps_after_kill
    assert [row[1] for row in ledger(tmp_path)] == ["intent"]
    requests_after_kill = len(stub_lrclib.requests)

    counts = next_run(run_cli, stub_lrclib, lib)

    assert counts == point.next_counts
    assert snap(track) == audio_before
    assert sidecar.read_bytes() == EXPECTED
    digest = hashlib.sha256(EXPECTED).hexdigest()
    assert ledger(tmp_path) == [(os.fsencode(sidecar), "published", digest, len(EXPECTED))]
    # A finalised track is never asked for again, and a retried one is asked once.
    asked_again = 1 if "written_lrc" in counts else 0
    assert len(stub_lrclib.requests) == requests_after_kill + asked_again
    # The ledger names a dead run's temp, so it goes however young it is.
    assert temps_in(lib) == []
    assert temp_rows(tmp_path) == []


@pytest.mark.parametrize("next_method", ["plain", "auto"])
def test_method_plain_after_claim_leaves_an_empty_file_the_next_run_replaces(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, next_method: str
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    sidecar = track.with_suffix(".lrc")
    audio_before = snap(track)

    crash_at(
        "method_plain_after_claim",
        lib,
        tmp_path,
        stub_lrclib,
        env={"PLYRIX_SIDECAR_METHOD": "plain"},
    )

    # This is the limit the README states: nothing but the kill's next run clears it.
    assert sidecar.read_bytes() == b""
    assert [row[1] for row in ledger(tmp_path)] == ["intent"]

    proc = run_cli(
        ["run", lib],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "PLYRIX_SIDECAR_METHOD": next_method},
    )

    assert proc.returncode == 0, proc.stderr
    counts = {key: value for key, value in parse_counts(proc.stdout).items() if value}
    assert counts == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert sidecar.read_bytes() == EXPECTED
    assert snap(track) == audio_before
    assert temps_in(lib) == []
    assert temp_rows(tmp_path) == []


PLAIN_EXPECTED = sidecars.encode(str(RECORD["plainLyrics"]))


def track_outcomes(tmp_path: Path) -> list[str]:
    with sqlite3.connect(state_db_path(tmp_path)) as conn:
        rows = conn.execute("SELECT outcome FROM tracks").fetchall()
    conn.close()
    return [row[0] for row in rows]


def plain_run_then_synced(
    lib: Path, tmp_path: Path, stub: StubLrclib, run_cli: RunCli
) -> tuple[Path, Path, Path]:
    """Leave a track with plyrix's own .txt, with a synced row now on offer."""
    track = copy_track(lib)
    stub.body = {**RECORD, "syncedLyrics": None}
    assert next_run(run_cli, stub, lib) == {"plain": 1, "written_txt": 1, "looked_up": 1}
    stub.body = RECORD
    return track, track.with_suffix(".txt"), track.with_suffix(".lrc")


def test_a_kill_before_an_upgrades_txt_goes_is_finished_by_the_next_run(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track, txt, lrc = plain_run_then_synced(lib, tmp_path, stub_lrclib, run_cli)
    audio_before = snap(track)

    crash_at("upgrade_before_txt_removed", lib, tmp_path, stub_lrclib)

    assert lrc.read_bytes() == EXPECTED
    assert txt.read_bytes() == PLAIN_EXPECTED
    assert [row[1] for row in ledger(tmp_path)] == ["published", "published"]
    requests = len(stub_lrclib.requests)

    counts = next_run(run_cli, stub_lrclib, lib)

    assert counts == {"synced": 1, "skipped_unchanged": 1, "upgraded": 1}
    assert len(stub_lrclib.requests) == requests
    assert sorted(path.name for path in lib.iterdir()) == ["t.flac", "t.lrc"]
    digest = hashlib.sha256(EXPECTED).hexdigest()
    assert ledger(tmp_path) == [(os.fsencode(lrc), "published", digest, len(EXPECTED))]
    assert track_outcomes(tmp_path) == ["synced"]
    assert snap(track) == audio_before


def test_a_kill_before_an_upgrades_row_goes_is_finished_by_the_next_run(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track, txt, lrc = plain_run_then_synced(lib, tmp_path, stub_lrclib, run_cli)
    audio_before = snap(track)

    crash_at("upgrade_before_row_dropped", lib, tmp_path, stub_lrclib)

    assert lrc.read_bytes() == EXPECTED
    assert not txt.exists()
    assert len(ledger(tmp_path)) == 2
    requests = len(stub_lrclib.requests)

    counts = next_run(run_cli, stub_lrclib, lib)

    assert counts == {"synced": 1, "skipped_unchanged": 1}
    assert len(stub_lrclib.requests) == requests
    assert sorted(path.name for path in lib.iterdir()) == ["t.flac", "t.lrc"]
    digest = hashlib.sha256(EXPECTED).hexdigest()
    assert ledger(tmp_path) == [(os.fsencode(lrc), "published", digest, len(EXPECTED))]
    assert track_outcomes(tmp_path) == ["synced"]
    assert snap(track) == audio_before


def test_a_txt_edited_after_a_killed_upgrade_stays(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    _, txt, lrc = plain_run_then_synced(lib, tmp_path, stub_lrclib, run_cli)
    crash_at("upgrade_before_txt_removed", lib, tmp_path, stub_lrclib)
    txt.write_bytes(FOREIGN)
    before = snap(txt)

    counts = next_run(run_cli, stub_lrclib, lib)

    assert counts == {"synced": 1, "skipped_unchanged": 1}
    assert snap(txt) == before
    assert lrc.read_bytes() == EXPECTED


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_a_killed_upgrades_txt_that_cant_be_read_waits_for_a_run_that_can(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    _, txt, _ = plain_run_then_synced(lib, tmp_path, stub_lrclib, run_cli)
    crash_at("upgrade_before_txt_removed", lib, tmp_path, stub_lrclib)
    # A new mtime forces a hash, as an equal stat would not open the file.
    age(txt, TWO_HOURS)
    txt.chmod(0)
    try:
        counts = next_run(run_cli, stub_lrclib, lib, exit_code=3)
    finally:
        txt.chmod(0o644)

    assert counts == {"read_failed": 1}
    assert txt.read_bytes() == PLAIN_EXPECTED

    assert next_run(run_cli, stub_lrclib, lib) == {
        "synced": 1,
        "skipped_unchanged": 1,
        "upgraded": 1,
    }
    assert not txt.exists()


def test_a_kill_before_the_rows_flush_costs_only_lookups(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    synced, miss = copy_track(lib, "a.flac"), copy_track(lib, "b.flac")
    audio_before = snap(synced), snap(miss)
    not_found = (404, {"name": "TrackNotFound"})
    stub_lrclib.queue = [(200, RECORD), not_found, not_found]
    env = {"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "PLYRIX_DUMP_ID": "dump-a"}

    def tracks() -> list[tuple[bytes, str]]:
        with sqlite3.connect(state_db_path(tmp_path)) as conn:
            rows = conn.execute("SELECT path, outcome FROM tracks ORDER BY path").fetchall()
        conn.close()
        return rows

    crash_at("before_tracks_flush", lib, tmp_path, stub_lrclib, dump_id="dump-a")

    assert tracks() == []
    assert synced.with_suffix(".lrc").read_bytes() == EXPECTED
    assert not miss.with_suffix(".lrc").exists()
    assert [row[1] for row in ledger(tmp_path)] == ["published"]
    assert len(stub_lrclib.requests) == 4

    # The miss has lost its row, so it is asked about again even though the dump is the same.
    stub_lrclib.queue = [not_found, not_found]
    proc = run_cli(["run", lib], env=env)

    assert proc.returncode == 0, proc.stderr
    assert {k: v for k, v in parse_counts(proc.stdout).items() if v} == {
        "synced": 1,
        "no_match": 1,
        "skipped_unchanged": 1,
        "looked_up": 1,
        "no_match_not_found": 1,
    }
    assert len(stub_lrclib.requests) == 7
    assert tracks() == [
        (os.fsencode(synced), "synced"),
        (os.fsencode(miss), "no_match"),
    ]
    assert synced.with_suffix(".lrc").read_bytes() == EXPECTED
    assert not miss.with_suffix(".lrc").exists()
    assert len(ledger(tmp_path)) == 1
    assert (snap(synced), snap(miss)) == audio_before

    # That cost one lookup, and now both are settled.
    proc = run_cli(["run", lib], env=env)

    assert {k: v for k, v in parse_counts(proc.stdout).items() if v} == {
        "synced": 1,
        "no_match": 1,
        "skipped_unchanged": 2,
        "no_match_not_found": 1,
    }
    assert len(stub_lrclib.requests) == 7


def test_a_kill_after_link_has_a_complete_sidecar_sharing_the_temps_inode(
    tmp_path: Path, stub_lrclib: StubLrclib
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)

    crash_at("after_link_before_unlink", lib, tmp_path, stub_lrclib)

    sidecar = track.with_suffix(".lrc")
    [temp] = temps_in(lib)
    assert sidecar.stat().st_ino == temp.stat().st_ino
    assert sidecar.stat().st_size == len(EXPECTED)


def test_a_kill_during_write_leaves_a_partial_temp_and_nothing_visible(
    tmp_path: Path, stub_lrclib: StubLrclib
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)

    crash_at("during_write", lib, tmp_path, stub_lrclib)

    [temp] = temps_in(lib)
    assert 0 < temp.stat().st_size < len(EXPECTED)
    assert sorted(path.name for path in lib.iterdir()) == sorted([track.name, temp.name])


def test_a_kill_midway_keeps_the_finished_tracks(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    first, second = copy_track(lib, "a.flac"), copy_track(lib, "b.flac")
    before = snap(first), snap(second)

    crash_at("second_track_before_link", lib, tmp_path, stub_lrclib)

    assert first.with_suffix(".lrc").read_bytes() == EXPECTED
    assert not second.with_suffix(".lrc").exists()

    assert next_run(run_cli, stub_lrclib, lib) == {
        "synced": 2,
        "written_lrc": 1,
        "skipped_unchanged": 1,
        "looked_up": 1,
    }

    assert second.with_suffix(".lrc").read_bytes() == EXPECTED
    assert (snap(first), snap(second)) == before


def test_a_kill_during_recovery_is_recovered_too(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    before = snap(track)
    crash_at("fallback_after_claim", lib, tmp_path, stub_lrclib)

    crash_at("during_recovery", lib, tmp_path, stub_lrclib)

    assert not track.with_suffix(".lrc").exists()
    assert next_run(run_cli, stub_lrclib, lib) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert track.with_suffix(".lrc").read_bytes() == EXPECTED
    assert snap(track) == before


def test_a_kill_recovering_a_recorded_temp_is_recovered_too(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    crash_at("before_link", lib, tmp_path, stub_lrclib)

    crash_at("before_temp_forgotten", lib, tmp_path, stub_lrclib)

    assert temps_in(lib) == []
    assert len(temp_rows(tmp_path)) == 1
    assert next_run(run_cli, stub_lrclib, lib) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert temp_rows(tmp_path) == []
    assert track.with_suffix(".lrc").read_bytes() == EXPECTED


def test_a_zero_byte_sidecar_with_no_intent_row_stays(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    sidecar = track.with_suffix(".lrc")
    sidecar.write_bytes(b"")
    before = snap(sidecar), snap(track)

    assert next_run(run_cli, stub_lrclib, lib) == {"blocked_foreign": 1}

    assert (snap(sidecar), snap(track)) == before
    assert ledger(tmp_path) == []


@pytest.mark.parametrize(
    "point", ["before_link", "after_intent_before_publish", "fallback_before_claim_record"]
)
def test_an_empty_file_made_by_another_tool_after_a_kill_with_no_claim_stays(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, point: str
) -> None:
    """SAFE-03: the row's existence proves nothing about who made an empty file at that name."""
    lib = tmp_path / "lib"
    track = copy_track(lib)
    sidecar = track.with_suffix(".lrc")
    crash_at(point, lib, tmp_path, stub_lrclib)
    assert [row[1] for row in ledger(tmp_path)] == ["intent"]
    sidecar.write_bytes(b"")
    before = snap(sidecar), snap(track)

    assert next_run(run_cli, stub_lrclib, lib) == {"blocked_foreign": 1}

    assert (snap(sidecar), snap(track)) == before
    assert ledger(tmp_path) == []


def test_an_intent_row_for_a_file_with_another_hash_goes_and_the_file_stays(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    sidecar = track.with_suffix(".lrc")
    crash_at("before_published_commit", lib, tmp_path, stub_lrclib)
    sidecar.write_bytes(FOREIGN)
    before = snap(sidecar)

    assert next_run(run_cli, stub_lrclib, lib) == {"blocked_foreign": 1}

    assert snap(sidecar) == before
    assert ledger(tmp_path) == []


def test_an_intent_row_for_a_symlink_goes_and_the_symlink_stays(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    sidecar = track.with_suffix(".lrc")
    crash_at("before_published_commit", lib, tmp_path, stub_lrclib)
    # The target holds the intended bytes, so following the link would look like a match.
    sidecar.rename(lib / "moved.bak")
    sidecar.symlink_to("moved.bak")

    assert next_run(run_cli, stub_lrclib, lib) == {"blocked_foreign": 1}

    assert sidecar.is_symlink()
    assert (lib / "moved.bak").read_bytes() == EXPECTED
    assert ledger(tmp_path) == []


def test_an_intent_row_for_a_missing_file_goes_and_the_track_is_retried(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    crash_at("before_published_commit", lib, tmp_path, stub_lrclib)
    track.with_suffix(".lrc").unlink()

    assert next_run(run_cli, stub_lrclib, lib) == {"synced": 1, "written_lrc": 1, "looked_up": 1}

    assert track.with_suffix(".lrc").read_bytes() == EXPECTED


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_a_sidecar_unreadable_for_one_run_keeps_its_proof_of_ownership(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    sidecar = copy_track(lib).with_suffix(".lrc")
    crash_at("before_published_commit", lib, tmp_path, stub_lrclib)
    sidecar.chmod(0)

    try:
        assert next_run(run_cli, stub_lrclib, lib, exit_code=3) == {"read_failed": 1}
        assert [row[1] for row in ledger(tmp_path)] == ["intent"]
    finally:
        sidecar.chmod(0o644)

    assert next_run(run_cli, stub_lrclib, lib) == {"synced": 1, "skipped_unchanged": 1}
    assert [row[1] for row in ledger(tmp_path)] == ["published"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_an_owned_sidecar_unreadable_for_one_run_is_not_called_foreign(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    sidecar = copy_track(lib).with_suffix(".lrc")
    assert next_run(run_cli, stub_lrclib, lib) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    # A sidecar whose stat equals its row is never opened, so this one needs a hash.
    later = sidecar.stat().st_mtime_ns + 10**9
    os.utime(sidecar, ns=(later, later))
    sidecar.chmod(0)

    try:
        assert next_run(run_cli, stub_lrclib, lib, exit_code=3) == {"read_failed": 1}
    finally:
        sidecar.chmod(0o644)

    assert next_run(run_cli, stub_lrclib, lib) == {"synced": 1, "skipped_unchanged": 1}


def test_an_owned_sidecar_with_an_equal_stat_still_shows_a_permissions_problem(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    sidecar = copy_track(lib).with_suffix(".lrc")
    assert next_run(run_cli, stub_lrclib, lib) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    sidecar.chmod(0)

    try:
        assert next_run(run_cli, stub_lrclib, lib, exit_code=3) == {"read_failed": 1}
    finally:
        sidecar.chmod(0o644)


def test_an_intent_left_by_a_run_on_an_album_is_recovered_by_a_run_on_the_library(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib / "Album")
    crash_at("before_published_commit", lib / "Album", tmp_path, stub_lrclib)
    assert [row[1] for row in ledger(tmp_path)] == ["intent"]

    assert next_run(run_cli, stub_lrclib, lib) == {"synced": 1, "skipped_unchanged": 1}

    assert track.with_suffix(".lrc").read_bytes() == EXPECTED
    assert [row[1] for row in ledger(tmp_path)] == ["published"]


def test_an_intent_outside_the_runs_folder_waits_for_a_run_that_covers_it(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib / "A")
    other = copy_track(lib / "B")
    crash_at("before_published_commit", lib / "A", tmp_path, stub_lrclib)

    assert next_run(run_cli, stub_lrclib, other.parent) == {
        "synced": 1,
        "written_lrc": 1,
        "looked_up": 1,
    }
    assert sorted(row[1] for row in ledger(tmp_path)) == ["intent", "published"]

    assert next_run(run_cli, stub_lrclib, lib) == {"synced": 2, "skipped_unchanged": 2}
    assert [row[1] for row in ledger(tmp_path)] == ["published", "published"]


def test_an_old_temp_is_swept_on_the_next_run(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    crash_at("before_link", lib, tmp_path, stub_lrclib)
    [temp] = temps_in(lib)
    age(temp, TWO_HOURS)

    assert next_run(run_cli, stub_lrclib, lib) == {"synced": 1, "written_lrc": 1, "looked_up": 1}

    assert temps_in(lib) == []
    assert track.with_suffix(".lrc").read_bytes() == EXPECTED


def test_a_young_temp_with_no_record_stays_on_the_next_run(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    """It may belong to a run on another machine, using another ledger."""
    lib = tmp_path / "lib"
    copy_track(lib)
    temp = lib / ".plyrix-0123abcd.part"
    temp.write_bytes(b"partial")

    next_run(run_cli, stub_lrclib, lib)

    assert temps_in(lib) == [temp]


@pytest.mark.parametrize("point", ["during_write", "before_link", "after_link_before_unlink"])
def test_a_young_temp_in_a_folder_excluded_after_the_kill_goes_through_its_record(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, point: str
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib / "Album")
    crash_at(point, lib, tmp_path, stub_lrclib)
    assert len(temps_in(lib / "Album")) == 1
    untagged_track_elsewhere(lib)

    proc = run_cli(
        ["run", lib, "--exclude", "Album"], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    )

    assert proc.returncode == 0, proc.stderr
    assert temps_in(lib / "Album") == []
    assert temp_rows(tmp_path) == []


def test_a_recorded_temp_outside_the_runs_root_waits_for_a_run_that_covers_it(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib / "A")
    other = copy_track(lib / "B")
    crash_at("before_link", lib / "A", tmp_path, stub_lrclib)
    [temp] = temps_in(lib / "A")

    next_run(run_cli, stub_lrclib, other.parent)

    assert temp.exists()
    assert temp_rows(tmp_path) == [os.fsencode(temp)]

    next_run(run_cli, stub_lrclib, lib)

    assert not temp.exists()
    assert temp_rows(tmp_path) == []


def test_a_recorded_temp_that_cannot_be_removed_stays_recorded_until_it_can(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = os.fsencode(tmp_path)
    temp = tmp_path / ".plyrix-0123abcd.part"
    temp.write_bytes(b"partial")
    conn = db.connect(tmp_path / "state.db")
    db.add_temp(conn, os.fsencode(temp))

    with monkeypatch.context() as faults:
        faults.setattr(os, "unlink", raising(errno.EACCES))
        pipeline.recover(conn, root)

    assert temp.exists()
    assert db.temp_rows(conn) == [os.fsencode(temp)]

    pipeline.recover(conn, root)

    assert not temp.exists()
    assert db.temp_rows(conn) == []
    conn.close()


@pytest.mark.parametrize(
    "name",
    [b"t.flac", b"t.lrc", b".plyrix-0123abcd.part.bak", b".plyrix-0123ABCD.part"],
    ids=["audio", "sidecar", "longer-name", "upper-case"],
)
def test_a_recorded_name_that_is_not_a_temp_is_left_and_forgotten(
    tmp_path: Path, name: bytes
) -> None:
    root = os.fsencode(tmp_path)
    planted = root + b"/" + name
    with open(planted, "wb") as handle:
        handle.write(b"keep")
    conn = db.connect(tmp_path / "state.db")
    db.add_temp(conn, planted)

    pipeline.recover(conn, root)

    assert os.path.exists(planted)
    assert db.temp_rows(conn) == []
    conn.close()


@pytest.mark.parametrize("plant", ["symlink", "directory"])
def test_something_at_a_recorded_temp_that_is_not_a_regular_file_is_left_and_forgotten(
    tmp_path: Path, plant: str
) -> None:
    root = os.fsencode(tmp_path)
    temp = tmp_path / ".plyrix-0123abcd.part"
    if plant == "symlink":
        (tmp_path / "t.flac").write_bytes(b"keep")
        temp.symlink_to("t.flac")
    else:
        temp.mkdir()
    conn = db.connect(tmp_path / "state.db")
    db.add_temp(conn, os.fsencode(temp))

    pipeline.recover(conn, root)

    assert os.path.lexists(temp)
    assert db.temp_rows(conn) == []
    conn.close()


def test_a_recorded_temp_stays_recorded_until_its_folder_can_say_it_is_gone(
    tmp_path: Path,
) -> None:
    root = os.fsencode(tmp_path)
    temp = root + b"/gone/.plyrix-0123abcd.part"
    conn = db.connect(tmp_path / "state.db")
    db.add_temp(conn, temp)

    pipeline.recover(conn, root)
    assert db.temp_rows(conn) == [temp]

    (tmp_path / "gone").mkdir()
    pipeline.recover(conn, root)
    assert db.temp_rows(conn) == [temp]

    (tmp_path / "gone" / "t.flac").write_bytes(b"keep")
    pipeline.recover(conn, root)
    assert db.temp_rows(conn) == []
    conn.close()


def test_a_temp_under_a_folder_replaced_by_a_file_is_not_called_gone(tmp_path: Path) -> None:
    root = os.fsencode(tmp_path)
    (tmp_path / "album").write_bytes(b"")

    assert sidecars.remove_temp(root + b"/album", b".plyrix-0123abcd.part") is False

    conn = db.connect(tmp_path / "state.db")
    db.add_temp(conn, root + b"/album/.plyrix-0123abcd.part")
    intent(conn, root + b"/album/t.lrc", Kind.LRC, "aaa")

    pipeline.recover(conn, root)

    assert len(db.temp_rows(conn)) == 1
    assert len(db.intent_rows(conn)) == 1
    conn.close()


def test_sweeping_a_temp_that_shares_the_sidecars_inode_keeps_the_sidecar(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    sidecar = track.with_suffix(".lrc")
    crash_at("after_link_before_unlink", lib, tmp_path, stub_lrclib)
    [temp] = temps_in(lib)
    age(temp, TWO_HOURS)

    assert next_run(run_cli, stub_lrclib, lib) == {"synced": 1, "skipped_unchanged": 1}

    assert temps_in(lib) == []
    assert sidecar.read_bytes() == EXPECTED
    assert sidecar.stat().st_nlink == 1


def test_a_temp_in_each_visited_folder_is_swept(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    old_temps = []
    for album in ("A", "B"):
        copy_track(lib / album)
        temp = lib / album / ".plyrix-0123abcd.part"
        temp.write_bytes(b"partial")
        age(temp, TWO_HOURS)
        old_temps.append(temp)

    next_run(run_cli, stub_lrclib, lib)

    assert not any(temp.exists() for temp in old_temps)


def test_an_old_temp_in_a_folder_whose_track_has_gone_is_swept(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib / "Album")
    crash_at("before_link", lib, tmp_path, stub_lrclib)
    [temp] = temps_in(lib / "Album")
    age(temp, TWO_HOURS)
    track.unlink()
    untagged_track_elsewhere(lib)

    assert next_run(run_cli, stub_lrclib, lib) == {"untagged": 1}

    assert temps_in(lib / "Album") == []
    assert ledger(tmp_path) == []


def test_an_old_temp_in_a_folder_excluded_after_the_kill_is_swept_through_its_row(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib / "Album")
    crash_at("before_link", lib, tmp_path, stub_lrclib)
    [temp] = temps_in(lib / "Album")
    age(temp, TWO_HOURS)
    untagged_track_elsewhere(lib)

    proc = run_cli(
        ["run", lib, "--exclude", "Album"], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    )

    assert proc.returncode == 0, proc.stderr
    assert temps_in(lib / "Album") == []
    assert ledger(tmp_path) == []


def test_a_young_temp_in_a_folder_with_a_row_stays(tmp_path: Path) -> None:
    root = os.fsencode(tmp_path)
    temp = tmp_path / ".plyrix-0123abcd.part"
    temp.write_bytes(b"partial")
    conn = db.connect(tmp_path / "state.db")
    intent(conn, root + b"/t.lrc", Kind.LRC, "aaa")

    pipeline.recover(conn, root)

    assert temp.exists()
    conn.close()


def test_recover_sweeps_no_folder_outside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    temp = outside / ".plyrix-0123abcd.part"
    temp.write_bytes(b"partial")
    age(temp, TWO_HOURS)
    conn = db.connect(tmp_path / "state.db")
    intent(conn, os.fsencode(outside / "t.lrc"), Kind.LRC, "aaa")

    pipeline.recover(conn, os.fsencode(root))

    assert temp.exists()
    conn.close()


@pytest.mark.parametrize("name", ["no-audio", "only-skips"])
def test_an_old_temp_in_a_folder_with_no_track_to_process_is_swept(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, name: str
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib / "keep")
    folder = lib / name
    folder.mkdir()
    if name == "only-skips":
        # One stem twice in a folder, which the walker reports as a Skip and never as a Track.
        for suffix in (".flac", ".mp3"):
            shutil.copyfile(FIXTURES / "t.flac", folder / f"t{suffix}")
    temp = folder / ".plyrix-0123abcd.part"
    temp.write_bytes(b"partial")
    age(temp, TWO_HOURS)

    next_run(run_cli, stub_lrclib, lib)

    assert not temp.exists()


def test_the_sweep_runs_once_per_folder(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "lib"
    for album in ("A", "B"):
        copy_track(lib / album, "1.flac")
        copy_track(lib / album, "2.flac")
    swept: list[bytes] = []
    monkeypatch.setattr(sidecars, "sweep_temps", lambda dir, _now: swept.append(dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    settings = Settings(
        music_root=lib,
        lrclib_url=stub_lrclib.base_url,
        state_db=tmp_path / "state" / "state.db",
    )

    pipeline.run(settings, os.fsencode(lib))

    assert swept == [os.fsencode(lib), os.fsencode(lib / "A"), os.fsencode(lib / "B")]


# --- recover and its ledger calls, in process


def intent(conn: db.Connection, path: bytes, kind: Kind, sha256: str) -> None:
    db.add_intent(conn, path, kind, sha256, 3, db.begin_run(conn, "test"))


def claim(conn: db.Connection, path: bytes, kind: Kind, sha256: str) -> None:
    intent(conn, path, kind, sha256)
    db.mark_claiming(conn, path)


def test_intent_rows_lists_every_intent_and_no_published_row(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    intent(conn, b"/root/a\xff.lrc", Kind.LRC, "aaa")
    intent(conn, b"/root/b.txt", Kind.TXT, "bbb")
    intent(conn, b"/root/done.lrc", Kind.LRC, "ccc")
    db.mark_published(conn, b"/root/done.lrc")
    intent(conn, b"/other/c.lrc", Kind.LRC, "ddd")

    rows = db.intent_rows(conn)

    assert sorted(rows) == [
        (b"/other/c.lrc", Kind.LRC, "ddd", False, None),
        (b"/root/a\xff.lrc", Kind.LRC, "aaa", False, None),
        (b"/root/b.txt", Kind.TXT, "bbb", False, None),
    ]
    conn.close()


def test_delete_row_removes_one_row(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    intent(conn, b"/root/a.lrc", Kind.LRC, "aaa")
    intent(conn, b"/root/b.lrc", Kind.LRC, "bbb")
    intent(conn, b"/other/a.lrc", Kind.LRC, "ccc")

    db.delete_row(conn, b"/root/a.lrc")

    assert sorted(db.intent_rows(conn)) == [
        (b"/other/a.lrc", Kind.LRC, "ccc", False, None),
        (b"/root/b.lrc", Kind.LRC, "bbb", False, None),
    ]
    conn.close()


def test_recover_removes_a_recorded_claim_for_a_txt_too(tmp_path: Path) -> None:
    root = os.fsencode(tmp_path)
    (tmp_path / "t.txt").write_bytes(b"")
    conn = db.connect(tmp_path / "state.db")
    claim(conn, root + b"/t.txt", Kind.TXT, hashlib.sha256(FOREIGN).hexdigest())

    pipeline.recover(conn, root)

    assert not (tmp_path / "t.txt").exists()
    assert db.intent_rows(conn) == []
    conn.close()


@pytest.mark.parametrize("suffix, kind", [(".lrc", Kind.LRC), (".txt", Kind.TXT)])
def test_recover_leaves_an_empty_file_when_the_row_recorded_no_claim(
    tmp_path: Path, suffix: str, kind: Kind
) -> None:
    root = os.fsencode(tmp_path)
    empty = tmp_path / f"t{suffix}"
    empty.write_bytes(b"")
    conn = db.connect(tmp_path / "state.db")
    intent(conn, os.fsencode(empty), kind, hashlib.sha256(FOREIGN).hexdigest())

    pipeline.recover(conn, root)

    assert empty.exists()
    assert db.intent_rows(conn) == []
    conn.close()


def test_recover_leaves_a_non_empty_file_even_when_the_row_recorded_a_claim(
    tmp_path: Path,
) -> None:
    root = os.fsencode(tmp_path)
    sidecar = tmp_path / "t.lrc"
    sidecar.write_bytes(FOREIGN)
    conn = db.connect(tmp_path / "state.db")
    claim(conn, root + b"/t.lrc", Kind.LRC, "not this file's hash")

    pipeline.recover(conn, root)

    assert sidecar.read_bytes() == FOREIGN
    assert db.intent_rows(conn) == []
    conn.close()


def test_a_new_intent_clears_an_earlier_claim(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    claim(conn, b"/root/a.lrc", Kind.LRC, "aaa")

    intent(conn, b"/root/a.lrc", Kind.LRC, "bbb")

    assert db.intent_rows(conn) == [(b"/root/a.lrc", Kind.LRC, "bbb", False, None)]
    conn.close()


def test_a_published_row_cannot_be_marked_as_a_claim(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    intent(conn, b"/root/a.lrc", Kind.LRC, "aaa")
    db.mark_published(conn, b"/root/a.lrc")

    db.mark_claiming(conn, b"/root/a.lrc")

    assert conn.execute("SELECT claiming FROM sidecars").fetchall() == [(0,)]
    conn.close()


def test_recover_finalises_a_match_in_a_subfolder_with_a_non_utf8_name(tmp_path: Path) -> None:
    root = os.fsencode(tmp_path)
    album = root + b"/caf\xe9"
    os.mkdir(album)
    with open(album + b"/\xff.lrc", "wb") as handle:
        handle.write(FOREIGN)
    conn = db.connect(tmp_path / "state.db")
    path = album + b"/\xff.lrc"
    intent(conn, path, Kind.LRC, hashlib.sha256(FOREIGN).hexdigest())

    pipeline.recover(conn, root)

    assert db.owned_row(conn, path) is not None
    conn.close()


@pytest.mark.parametrize(
    "row_path",
    [b"outside/t.lrc", b"lib/../outside/t.lrc", b"lib/x/../../outside/t.lrc", b"lib2/t.lrc"],
    ids=["beside", "parent", "parent-after-folder", "name-starts-like-the-root"],
)
def test_recover_never_acts_outside_the_root_and_keeps_the_row(
    tmp_path: Path, row_path: bytes
) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "lib2").mkdir()
    full = os.fsencode(tmp_path) + b"/" + row_path
    planted = Path(os.path.normpath(os.fsdecode(full)))
    planted.write_bytes(b"")
    conn = db.connect(tmp_path / "state.db")
    claim(conn, full, Kind.LRC, "aaa")

    pipeline.recover(conn, os.fsencode(root))

    assert planted.exists()
    assert db.intent_rows(conn) == [(full, Kind.LRC, "aaa", True, None)]
    conn.close()


def test_recover_leaves_published_rows_alone(tmp_path: Path) -> None:
    root = os.fsencode(tmp_path)
    conn = db.connect(tmp_path / "state.db")
    intent(conn, root + b"/gone.lrc", Kind.LRC, "aaa")
    db.mark_published(conn, root + b"/gone.lrc")

    pipeline.recover(conn, root)

    assert db.owned_row(conn, root + b"/gone.lrc") == ("aaa", 3, "written", None)
    conn.close()


def test_recover_keeps_the_row_until_an_empty_file_can_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = os.fsencode(tmp_path)
    (tmp_path / "t.lrc").write_bytes(b"")
    conn = db.connect(tmp_path / "state.db")
    digest = hashlib.sha256(FOREIGN).hexdigest()
    claim(conn, root + b"/t.lrc", Kind.LRC, digest)

    def refuse(_path: bytes) -> None:
        raise PermissionError

    with monkeypatch.context() as faults:
        faults.setattr(os, "unlink", refuse)
        pipeline.recover(conn, root)

    assert (tmp_path / "t.lrc").exists()
    assert db.intent_rows(conn) == [(root + b"/t.lrc", Kind.LRC, digest, True, None)]

    pipeline.recover(conn, root)

    assert not (tmp_path / "t.lrc").exists()
    assert db.intent_rows(conn) == []
    conn.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_a_claim_that_cannot_be_removed_for_one_run_does_not_block_the_track_for_good(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    sidecar = copy_track(lib).with_suffix(".lrc")
    crash_at("fallback_after_claim", lib, tmp_path, stub_lrclib)
    lib.chmod(0o555)

    try:
        assert next_run(run_cli, stub_lrclib, lib) == {"blocked_foreign": 1}
        assert sidecar.read_bytes() == b""
        assert claiming_flags(tmp_path) == [1]
    finally:
        lib.chmod(0o755)

    assert next_run(run_cli, stub_lrclib, lib) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert sidecar.read_bytes() == EXPECTED
    assert claiming_flags(tmp_path) == [0]


# --- a failed write on the no-hard-links path, then what the next run finds
#
# Each fault fails one step of that path, in process and with no kill. The next run is the real
# command, so these walk the claim, write-failure and recovery paths together.


def raising(code: int) -> Callable[..., None]:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError(code, os.strerror(code))

    return fail


def at_claim_open(faults: pytest.MonkeyPatch, action: Callable[[], None]) -> None:
    """Run action as the claim is opened: creating a name that isn't a temp."""
    real_open = os.open

    def opened(path: bytes, flags: int, *args: int, **kwargs: int) -> int:
        if flags & os.O_CREAT and os.fsencode(path).endswith(b".lrc"):
            action()
        return real_open(path, flags, *args, **kwargs)

    faults.setattr(os, "open", opened)


def rival_takes_the_name(sidecar: Path) -> None:
    rival = sidecar.with_name("rival")
    rival.write_bytes(b"")
    REAL_REPLACE(rival, sidecar)


def claim_open_fails(code: int) -> Callable[[pytest.MonkeyPatch, Path], None]:
    def install(faults: pytest.MonkeyPatch, _sidecar: Path) -> None:
        at_claim_open(faults, lambda: raising(code)())

    return install


def replace_fails(faults: pytest.MonkeyPatch, _sidecar: Path) -> None:
    faults.setattr(os, "replace", raising(errno.EIO))


def replace_crosses_a_device(faults: pytest.MonkeyPatch, _sidecar: Path) -> None:
    faults.setattr(os, "replace", raising(errno.EXDEV))


def replace_and_removing_the_claim_fail(faults: pytest.MonkeyPatch, _sidecar: Path) -> None:
    faults.setattr(os, "replace", raising(errno.EIO))
    real_unlink = os.unlink

    def unlink(path: bytes) -> None:
        if path.endswith(b".lrc"):
            raising(errno.EACCES)()
        real_unlink(path)

    faults.setattr(os, "unlink", unlink)


def fstat_of_the_claim_fails(faults: pytest.MonkeyPatch, _sidecar: Path) -> None:
    armed: list[bool] = []
    real_fstat = os.fstat

    def fstat(descriptor: int) -> os.stat_result:
        if armed:
            armed.clear()
            raising(errno.EIO)()
        return real_fstat(descriptor)

    faults.setattr(os, "fstat", fstat)
    at_claim_open(faults, lambda: armed.append(True))


def lstat_of_the_claim_fails(call: int) -> Callable[[pytest.MonkeyPatch, Path], None]:
    """Fail the call'th look at the name while the claim is there. A kept row has to follow."""

    def install(faults: pytest.MonkeyPatch, sidecar: Path) -> None:
        calls: list[int] = []
        real_lstat = os.lstat
        name = os.fsencode(sidecar)

        def lstat(path: bytes) -> os.stat_result:
            found = real_lstat(path)
            if path == name:
                calls.append(1)
                if len(calls) == call:
                    raising(errno.EIO)()
            return found

        faults.setattr(os, "lstat", lstat)
        if call > 1:
            faults.setattr(os, "replace", raising(errno.EIO))

    return install


def rival_before_the_claim_open(faults: pytest.MonkeyPatch, sidecar: Path) -> None:
    at_claim_open(faults, lambda: rival_takes_the_name(sidecar))


def rival_then_replace_fails(faults: pytest.MonkeyPatch, sidecar: Path) -> None:
    def replace(*_args: object) -> None:
        rival_takes_the_name(sidecar)
        raising(errno.EIO)()

    faults.setattr(os, "replace", replace)


@dataclass(frozen=True)
class FailedStep:
    """A fault on the no-hard-links path, and the true record it has to leave."""

    name: str
    install: Callable[[pytest.MonkeyPatch, Path], None]
    count: str
    left_at_the_name: bytes | None
    # The row's claiming flag, or None when no row should stay.
    claiming: int | None


FAILED_STEPS = [
    FailedStep("claim_open_enospc", claim_open_fails(errno.ENOSPC), "write_failed", None, 0),
    FailedStep("claim_open_edquot", claim_open_fails(errno.EDQUOT), "write_failed", None, 0),
    FailedStep("claim_open_eio", claim_open_fails(errno.EIO), "write_failed", None, 0),
    FailedStep("replace_eio", replace_fails, "write_failed", None, 0),
    FailedStep("replace_exdev", replace_crosses_a_device, "fs_no_atomic_publish", None, None),
    FailedStep("replace_and_unlink", replace_and_removing_the_claim_fail, "write_failed", b"", 1),
    FailedStep("fstat", fstat_of_the_claim_fails, "write_failed", b"", 1),
    FailedStep("lstat_after_claim", lstat_of_the_claim_fails(1), "write_failed", b"", 1),
    FailedStep("lstat_after_failed_replace", lstat_of_the_claim_fails(2), "write_failed", b"", 1),
    FailedStep("rival_before_open", rival_before_the_claim_open, "blocked_foreign", b"", None),
    FailedStep("rival_then_replace", rival_then_replace_fails, "write_failed", b"", 0),
]
STEP_IDS = [step.name for step in FAILED_STEPS]
# The steps that leave plyrix's own claim, or nothing, so the next run can write the sidecar.
RETRIED_STEPS = [step for step in FAILED_STEPS if not step.name.startswith("rival")]
NOTHING_LEFT_STEPS = [step for step in FAILED_STEPS if step.left_at_the_name is None]
CLAIM_LEFT_STEPS = [step for step in RETRIED_STEPS if step.left_at_the_name is not None]


def fail_a_step(
    step: FailedStep, tmp_path: Path, stub: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, pipeline.Counts]:
    """Run once with hard links refused and step's fault on, and return the sidecar and counts."""
    lib = tmp_path / "lib"
    sidecar = copy_track(lib / "Album").with_suffix(".lrc")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    settings = Settings(
        music_root=lib,
        lrclib_url=stub.base_url,
        state_db=state_db_path(tmp_path),
    )
    with monkeypatch.context() as faults:
        faults.setattr(os, "link", raising(errno.EPERM))
        step.install(faults, sidecar)
        counts = pipeline.run(settings, os.fsencode(lib))
    return sidecar, counts


def claiming_flags(tmp_path: Path) -> list[int]:
    with sqlite3.connect(state_db_path(tmp_path)) as conn:
        rows = conn.execute("SELECT claiming FROM sidecars").fetchall()
    conn.close()
    return [row[0] for row in rows]


def temp_removal_fails(faults: pytest.MonkeyPatch, _sidecar: Path) -> None:
    real_unlink = os.unlink

    def unlink(path: bytes) -> None:
        if path.endswith(b".part"):
            raising(errno.EIO)()
        real_unlink(path)

    faults.setattr(os, "unlink", unlink)


@pytest.mark.parametrize("step", FAILED_STEPS, ids=STEP_IDS)
def test_a_failed_step_leaves_a_true_record_of_the_claim(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch, step: FailedStep
) -> None:
    sidecar, counts = fail_a_step(step, tmp_path, stub_lrclib, monkeypatch)

    assert getattr(counts, step.count) == 1
    on_disk = sidecar.read_bytes() if sidecar.exists() else None
    assert on_disk == step.left_at_the_name
    assert claiming_flags(tmp_path) == ([] if step.claiming is None else [step.claiming])
    assert temps_in(sidecar.parent) == []
    assert temp_rows(tmp_path) == []


@pytest.mark.parametrize("step", FAILED_STEPS, ids=STEP_IDS)
def test_a_temp_left_by_a_failed_step_is_recorded_and_goes_from_an_excluded_folder(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    step: FailedStep,
) -> None:
    """A step that fails, a temp that won't go and a folder excluded next, all at once."""

    def install(faults: pytest.MonkeyPatch, sidecar: Path) -> None:
        temp_removal_fails(faults, sidecar)
        step.install(faults, sidecar)

    sidecar, _ = fail_a_step(
        FailedStep(step.name, install, step.count, step.left_at_the_name, step.claiming),
        tmp_path,
        stub_lrclib,
        monkeypatch,
    )
    [temp] = temps_in(sidecar.parent)
    assert temp_rows(tmp_path) == [os.fsencode(temp)]
    assert claiming_flags(tmp_path) == ([] if step.claiming is None else [step.claiming])
    untagged_track_elsewhere(tmp_path / "lib")

    proc = run_cli(
        ["run", tmp_path / "lib", "--exclude", "Album"],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url},
    )

    assert proc.returncode == 0, proc.stderr
    assert temps_in(sidecar.parent) == []
    assert temp_rows(tmp_path) == []
    # The claim is settled as well, and a rival's file is not.
    assert sidecar.exists() is step.name.startswith("rival")
    assert ledger(tmp_path) == []


@pytest.mark.parametrize("step", NOTHING_LEFT_STEPS, ids=lambda step: step.name)
def test_after_a_failed_step_with_no_claim_left_another_tools_empty_file_stays(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    step: FailedStep,
) -> None:
    """SAFE-03: a failed write must not leave a record that makes the next run delete this."""
    sidecar, _ = fail_a_step(step, tmp_path, stub_lrclib, monkeypatch)
    sidecar.write_bytes(b"")
    before = snap(sidecar)

    assert next_run(run_cli, stub_lrclib, sidecar.parent) == {"blocked_foreign": 1}

    assert snap(sidecar) == before
    assert ledger(tmp_path) == []


@pytest.mark.parametrize("step", RETRIED_STEPS, ids=lambda step: step.name)
def test_the_next_run_writes_the_sidecar_after_a_failed_step(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    step: FailedStep,
) -> None:
    sidecar, _ = fail_a_step(step, tmp_path, stub_lrclib, monkeypatch)

    assert next_run(run_cli, stub_lrclib, sidecar.parent) == {
        "synced": 1,
        "written_lrc": 1,
        "looked_up": 1,
    }

    assert sidecar.read_bytes() == EXPECTED
    assert [row[1] for row in ledger(tmp_path)] == ["published"]
    assert claiming_flags(tmp_path) == [0]


@pytest.mark.parametrize(
    "step",
    [step for step in RETRIED_STEPS if step.claiming is not None],
    ids=lambda step: step.name,
)
def test_a_kill_recovering_from_a_failed_step_is_recovered_too(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    step: FailedStep,
) -> None:
    sidecar, _ = fail_a_step(step, tmp_path, stub_lrclib, monkeypatch)

    crash_at("during_recovery", sidecar.parent, tmp_path, stub_lrclib)

    assert next_run(run_cli, stub_lrclib, sidecar.parent) == {
        "synced": 1,
        "written_lrc": 1,
        "looked_up": 1,
    }
    assert sidecar.read_bytes() == EXPECTED


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
@pytest.mark.parametrize("step", CLAIM_LEFT_STEPS, ids=lambda step: step.name)
def test_a_claim_left_by_a_failed_step_survives_a_run_that_cannot_remove_it(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    step: FailedStep,
) -> None:
    sidecar, _ = fail_a_step(step, tmp_path, stub_lrclib, monkeypatch)
    lib = sidecar.parent
    lib.chmod(0o555)

    try:
        assert next_run(run_cli, stub_lrclib, lib) == {"blocked_foreign": 1}
        assert claiming_flags(tmp_path) == [1]
    finally:
        lib.chmod(0o755)

    assert next_run(run_cli, stub_lrclib, lib) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert sidecar.read_bytes() == EXPECTED


@pytest.mark.parametrize(
    "step",
    [step for step in FAILED_STEPS if step.name.startswith("rival")],
    ids=lambda step: step.name,
)
def test_a_rival_at_the_name_keeps_its_empty_file_after_a_failed_step(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    step: FailedStep,
) -> None:
    sidecar, _ = fail_a_step(step, tmp_path, stub_lrclib, monkeypatch)
    before = snap(sidecar)

    assert next_run(run_cli, stub_lrclib, sidecar.parent) == {"blocked_foreign": 1}

    assert snap(sidecar) == before
    assert ledger(tmp_path) == []


# --- a run while the library is unmounted, then the same files back
#
# A missing folder and an empty root are all an absent mount shows, so each of these has to come
# through with the ledger unchanged, and the run after it has to finish what the kill left.

# Where the track sits decides what is empty while unmounted: its folder is missing, or it is the
# root itself, which stays as an empty folder.
LAYOUTS = ["Album", "."]
LAYOUT_IDS = ["in-album", "in-root"]


def assert_unmounted_run_forgets_nothing(
    run_cli: RunCli, stub: StubLrclib, lib: Path, tmp_path: Path
) -> None:
    rows, records, claims = ledger(tmp_path), temp_rows(tmp_path), claiming_flags(tmp_path)
    with unmounted(lib):
        # The run stops before it opens the ledger.
        assert next_run(run_cli, stub, lib, exit_code=2) == {}
        assert ledger(tmp_path) == rows
        assert temp_rows(tmp_path) == records
        assert claiming_flags(tmp_path) == claims
        # Recovery has to hold too, as one folder can be absent while the rest of a library runs.
        conn = db.connect(state_db_path(tmp_path))
        pipeline.recover(conn, os.path.realpath(os.fsencode(lib)))
        conn.close()
        assert ledger(tmp_path) == rows
        assert temp_rows(tmp_path) == records
        assert claiming_flags(tmp_path) == claims


@pytest.mark.parametrize("layout", LAYOUTS, ids=LAYOUT_IDS)
@pytest.mark.parametrize("point", CRASH_POINTS, ids=lambda point: point.name)
def test_a_kill_then_an_unmounted_run_still_recovers_when_the_library_is_back(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, point: CrashPoint, layout: str
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib / layout)
    sidecar = track.with_suffix(".lrc")
    audio_before = snap(track)
    crash_at(point.name, lib, tmp_path, stub_lrclib)

    assert_unmounted_run_forgets_nothing(run_cli, stub_lrclib, lib, tmp_path)

    assert snap(track) == audio_before
    assert next_run(run_cli, stub_lrclib, lib) == point.next_counts
    assert sidecar.read_bytes() == EXPECTED
    assert [row[1] for row in ledger(tmp_path)] == ["published"]
    assert claiming_flags(tmp_path) == [0]
    assert temps_in(track.parent) == []
    assert temp_rows(tmp_path) == []
    assert snap(track) == audio_before


@pytest.mark.parametrize("point", ["during_write", "before_link", "after_link_before_unlink"])
def test_after_an_unmounted_run_a_temp_in_an_excluded_folder_still_goes_through_its_record(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, point: str
) -> None:
    """With no record only the hour's wait could remove this, and an excluded folder never does."""
    lib = tmp_path / "lib"
    copy_track(lib / "Album")
    crash_at(point, lib, tmp_path, stub_lrclib)
    assert len(temps_in(lib / "Album")) == 1

    assert_unmounted_run_forgets_nothing(run_cli, stub_lrclib, lib, tmp_path)
    untagged_track_elsewhere(lib)

    proc = run_cli(
        ["run", lib, "--exclude", "Album"], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    )
    assert proc.returncode == 0, proc.stderr
    assert temps_in(lib / "Album") == []
    assert temp_rows(tmp_path) == []


@pytest.mark.parametrize("layout", LAYOUTS, ids=LAYOUT_IDS)
@pytest.mark.parametrize(
    ("point", "kill_in_recovery"),
    [("fallback_after_claim", "during_recovery"), ("before_link", "before_temp_forgotten")],
    ids=["claim", "temp"],
)
def test_a_kill_recovering_after_an_unmounted_run_is_recovered_too(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    point: str,
    kill_in_recovery: str,
    layout: str,
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib / layout)
    crash_at(point, lib, tmp_path, stub_lrclib)
    assert_unmounted_run_forgets_nothing(run_cli, stub_lrclib, lib, tmp_path)

    crash_at(kill_in_recovery, lib, tmp_path, stub_lrclib)

    assert next_run(run_cli, stub_lrclib, lib) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert track.with_suffix(".lrc").read_bytes() == EXPECTED
    assert temps_in(track.parent) == []
    assert temp_rows(tmp_path) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_a_claim_through_an_unmounted_run_then_a_read_only_mount_is_still_plyrixs(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    sidecar = copy_track(lib / "Album").with_suffix(".lrc")
    crash_at("fallback_after_claim", lib, tmp_path, stub_lrclib)
    assert_unmounted_run_forgets_nothing(run_cli, stub_lrclib, lib, tmp_path)
    sidecar.parent.chmod(0o555)

    try:
        assert next_run(run_cli, stub_lrclib, lib) == {"blocked_foreign": 1}
        assert sidecar.read_bytes() == b""
        assert claiming_flags(tmp_path) == [1]
    finally:
        sidecar.parent.chmod(0o755)

    assert next_run(run_cli, stub_lrclib, lib) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert sidecar.read_bytes() == EXPECTED


@pytest.mark.parametrize("step", RETRIED_STEPS, ids=lambda step: step.name)
def test_a_failed_step_then_an_unmounted_run_still_writes_when_the_library_is_back(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    step: FailedStep,
) -> None:
    sidecar, _ = fail_a_step(step, tmp_path, stub_lrclib, monkeypatch)
    lib = tmp_path / "lib"

    assert_unmounted_run_forgets_nothing(run_cli, stub_lrclib, lib, tmp_path)

    assert next_run(run_cli, stub_lrclib, lib) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert sidecar.read_bytes() == EXPECTED
    assert claiming_flags(tmp_path) == [0]


@pytest.mark.parametrize("step", FAILED_STEPS, ids=STEP_IDS)
def test_a_failed_step_with_a_temp_left_then_an_unmounted_run_settles_from_an_excluded_folder(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    step: FailedStep,
) -> None:
    """The failed write, the temp that won't go, the absent mount and the exclude, all at once."""

    def install(faults: pytest.MonkeyPatch, sidecar: Path) -> None:
        temp_removal_fails(faults, sidecar)
        step.install(faults, sidecar)

    sidecar, _ = fail_a_step(
        FailedStep(step.name, install, step.count, step.left_at_the_name, step.claiming),
        tmp_path,
        stub_lrclib,
        monkeypatch,
    )
    lib = tmp_path / "lib"
    assert len(temps_in(sidecar.parent)) == 1

    assert_unmounted_run_forgets_nothing(run_cli, stub_lrclib, lib, tmp_path)
    untagged_track_elsewhere(lib)

    proc = run_cli(
        ["run", lib, "--exclude", "Album"], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    )
    assert proc.returncode == 0, proc.stderr
    assert temps_in(sidecar.parent) == []
    assert temp_rows(tmp_path) == []
    assert sidecar.exists() is step.name.startswith("rival")
    assert ledger(tmp_path) == []


def test_one_absent_folder_keeps_its_row_while_the_rest_of_the_library_runs(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    sidecar = copy_track(lib / "A").with_suffix(".lrc")
    crash_at("before_published_commit", lib / "A", tmp_path, stub_lrclib)
    copy_track(lib / "B")
    (lib / "A").rename(tmp_path / "A.away")

    assert next_run(run_cli, stub_lrclib, lib) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert sorted(row[1] for row in ledger(tmp_path)) == ["intent", "published"]

    (tmp_path / "A.away").rename(lib / "A")

    assert next_run(run_cli, stub_lrclib, lib) == {"synced": 2, "skipped_unchanged": 2}
    assert sidecar.read_bytes() == EXPECTED


def test_a_row_kept_for_a_deleted_folder_does_not_stop_a_new_track_there(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    sidecar = copy_track(lib / "Album").with_suffix(".lrc")
    crash_at("fallback_after_claim", lib, tmp_path, stub_lrclib)
    shutil.rmtree(lib / "Album")
    untagged_track_elsewhere(lib)

    assert next_run(run_cli, stub_lrclib, lib) == {"untagged": 1}
    assert claiming_flags(tmp_path) == [1]

    copy_track(lib / "Album")

    assert next_run(run_cli, stub_lrclib, lib) == {
        "synced": 1,
        "written_lrc": 1,
        "looked_up": 1,
        "untagged": 1,
        "skipped_unchanged": 1,
    }
    assert sidecar.read_bytes() == EXPECTED
    assert temp_rows(tmp_path) == []


# A kill while replacing a .lrc that no longer fits. The .lrc has a stamp at 4.0 s, which suits a
# 3 s track and is beyond the end of a 1 s one, and embedded_token.flac has t.flac's fields at 1 s.
STALE_SYNCED = "[00:00.50] a\n[00:01.00] b\n[00:01.50] c\n[00:04.00] d\n"
STALE_BYTES = sidecars.encode(STALE_SYNCED)
FITTING = {
    **RECORD,
    "duration": 1.0,
    "syncedLyrics": "[00:00.10] a\n[00:00.30] b\n[00:00.50] c\n[00:00.90] d\n",
}


def stale_lrc_with_a_fit_on_offer(
    lib: Path, stub: StubLrclib, run_cli: RunCli
) -> tuple[Path, Path, str]:
    """Leave plyrix's own .lrc beside a track it no longer fits, and a record that does fit."""
    track = copy_track(lib)
    stub.body = {**RECORD, "syncedLyrics": STALE_SYNCED}
    assert next_run(run_cli, stub, lib) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    shutil.copyfile(FIXTURES / "embedded_token.flac", track)
    stub.body = FITTING
    new_hash = hashlib.sha256(sidecars.encode(str(FITTING["syncedLyrics"]))).hexdigest()
    return track, track.with_suffix(".lrc"), new_hash


REPLACED = {"synced": 1, "written_lrc": 1, "lrc_stale": 1, "looked_up": 1}


def test_a_kill_before_replace_owned_keeps_the_file_owned_and_the_next_run_replaces_it(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track, lrc, new_hash = stale_lrc_with_a_fit_on_offer(lib, stub_lrclib, run_cli)
    audio_before = snap(track)

    crash_at("before_replace_owned", lib, tmp_path, stub_lrclib)

    assert lrc.read_bytes() == STALE_BYTES
    assert [row[1:3] for row in ledger(tmp_path)] == [("intent", new_hash)]

    counts = next_run(run_cli, stub_lrclib, lib)

    assert counts == REPLACED
    assert lrc.read_bytes() == sidecars.encode(str(FITTING["syncedLyrics"]))
    assert snap(track) == audio_before
    assert [row[1:3] for row in ledger(tmp_path)] == [("published", new_hash)]
    assert temps_in(lib) == []


def test_a_kill_before_replace_owned_then_an_edit_makes_the_file_foreign(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track, lrc, _ = stale_lrc_with_a_fit_on_offer(lib, stub_lrclib, run_cli)
    audio_before = snap(track)
    crash_at("before_replace_owned", lib, tmp_path, stub_lrclib)
    lrc.write_bytes(FOREIGN)
    before = snap(lrc)
    requests = len(stub_lrclib.requests)

    counts = next_run(run_cli, stub_lrclib, lib)

    assert counts == {"blocked_foreign": 1}
    assert snap(lrc) == before
    assert snap(track) == audio_before
    assert ledger(tmp_path) == []
    assert len(stub_lrclib.requests) == requests
    assert temps_in(lib) == []


def test_a_kill_before_replace_owned_keeps_the_file_if_it_fits_after_all(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track, lrc, _ = stale_lrc_with_a_fit_on_offer(lib, stub_lrclib, run_cli)
    crash_at("before_replace_owned", lib, tmp_path, stub_lrclib)
    # The original audio comes back, so the file fits again.
    shutil.copyfile(FIXTURES / "t.flac", track)
    before = snap(lrc)

    counts = next_run(run_cli, stub_lrclib, lib)

    assert counts == {"synced": 1}
    assert snap(lrc) == before
    digest = hashlib.sha256(STALE_BYTES).hexdigest()
    assert ledger(tmp_path) == [(os.fsencode(lrc), "published", digest, len(STALE_BYTES))]


def test_a_kill_during_replace_write_leaves_the_lrc_and_the_next_run_removes_the_temp(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track, lrc, new_hash = stale_lrc_with_a_fit_on_offer(lib, stub_lrclib, run_cli)
    audio_before = snap(track)

    crash_at("during_replace_write", lib, tmp_path, stub_lrclib)

    assert lrc.read_bytes() == STALE_BYTES
    temps = temps_in(lib)
    assert len(temps) == 1
    assert temp_rows(tmp_path) == [os.fsencode(temps[0])]

    counts = next_run(run_cli, stub_lrclib, lib)

    assert counts == REPLACED
    assert lrc.read_bytes() == sidecars.encode(str(FITTING["syncedLyrics"]))
    assert [row[1:3] for row in ledger(tmp_path)] == [("published", new_hash)]
    assert snap(track) == audio_before
    assert temps_in(lib) == []
    assert temp_rows(tmp_path) == []


def replacing_intent(conn: db.Connection, sidecar: Path) -> None:
    """Leave the ledger as a kill between the intent and the replace would: old, then new hash."""
    run_id = db.begin_run(conn, "test", "dump-a")
    old = hashlib.sha256(STALE_BYTES).hexdigest()
    db.add_intent(conn, os.fsencode(sidecar), Kind.LRC, old, len(STALE_BYTES), run_id, 7, "dump-a")
    db.mark_published(conn, os.fsencode(sidecar), 1)
    db.add_intent(conn, os.fsencode(sidecar), Kind.LRC, "new", 3, run_id, 8, "dump-b", True)


@pytest.mark.parametrize("remove", [True, False], ids=["run", "dry_run"])
def test_recover_gives_a_row_back_when_the_old_file_is_still_there(
    tmp_path: Path, remove: bool
) -> None:
    sidecar = tmp_path / "a.lrc"
    sidecar.write_bytes(STALE_BYTES)
    before = snap(sidecar)
    conn = db.connect(tmp_path.parent / f"{tmp_path.name}.db")
    replacing_intent(conn, sidecar)

    pipeline.recover(conn, os.fsencode(tmp_path), remove=remove)

    old = hashlib.sha256(STALE_BYTES).hexdigest()
    assert db.intent_rows(conn) == []
    assert db.owned_row(conn, os.fsencode(sidecar)) == (old, len(STALE_BYTES), "written", None)
    assert snap(sidecar) == before
    conn.close()


def test_recover_finishes_a_replacement_when_the_new_file_is_there(tmp_path: Path) -> None:
    sidecar = tmp_path / "a.lrc"
    sidecar.write_bytes(b"new")
    new = hashlib.sha256(b"new").hexdigest()
    conn = db.connect(tmp_path.parent / f"{tmp_path.name}.db")
    replacing_intent(conn, sidecar)
    conn.execute("UPDATE sidecars SET sha256 = ?", (new,))

    pipeline.recover(conn, os.fsencode(tmp_path))

    assert db.owned_row(conn, os.fsencode(sidecar)) == (new, 3, "written", None)
    assert db.intent_rows(conn) == []
    conn.close()


@pytest.mark.parametrize("left", ["edited", "gone"])
def test_recover_drops_a_replacing_row_when_the_old_file_is_not_there(
    tmp_path: Path, left: str
) -> None:
    sidecar = tmp_path / "a.lrc"
    keeps_the_folder_telling = tmp_path / "other"
    keeps_the_folder_telling.write_bytes(b"x")
    if left == "edited":
        sidecar.write_bytes(FOREIGN)
    conn = db.connect(tmp_path.parent / f"{tmp_path.name}.db")
    replacing_intent(conn, sidecar)

    pipeline.recover(conn, os.fsencode(tmp_path))

    assert db.owned_row(conn, os.fsencode(sidecar)) is None
    assert db.intent_rows(conn) == []
    assert (sidecar.read_bytes() == FOREIGN) if left == "edited" else not sidecar.exists()
    conn.close()
