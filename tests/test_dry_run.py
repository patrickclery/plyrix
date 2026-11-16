"""RUN-01: a dry run does everything but the writing, proved through the real CLI."""

import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import (
    CANARY,
    FIXTURES,
    RECORD,
    RunCli,
    StubLrclib,
    copy_track,
    leaks,
    leave_hot_journal,
    non_notice_lines,
    nonzero,
    parse_counts,
    sidecars_in,
    snap,
    state_db_path,
)
from plyrix import pipeline, sidecars
from plyrix.domain import Kind
from plyrix.state import db

HOUR = 3600
LOCKED_WITHIN_S = 15
EXIT_TRIPPED = 70
EXIT_LOCKED = 75
UNKNOWN_ERROR = {"message": "Something bad happened", "name": "UnknownError", "statusCode": 500}
FOREIGN = b"notes a person wrote\n"
PLAIN_ONLY = {**RECORD, "syncedLyrics": None}
# albumartist_only.flac has no artist tag and runs 1 s, so its lookups can be told from t.flac's.
PLAIN_TRACK_ARTIST = "Fixture Album Artist"
PLAIN_TRACK_RECORD = {**PLAIN_ONLY, "artistName": PLAIN_TRACK_ARTIST, "duration": 1.0}
# A sidecar whose audio changed. The stamp at 4.0 s fits a 3 s track and not a 1 s one.
STALE_SYNCED = (
    f"[00:00.50] {CANARY} line 1\n[00:01.00] {CANARY} line 2\n"
    f"[00:01.50] {CANARY} line 3\n[00:04.00] {CANARY} line 4\n"
)
FITTING_SYNCED = (
    f"[00:00.10] {CANARY} new 1\n[00:00.30] {CANARY} new 2\n"
    f"[00:00.50] {CANARY} new 3\n[00:00.90] {CANARY} new 4\n"
)
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

HOLDER = """
import sys
from pathlib import Path
from plyrix.state import db

conn = db.connect(Path(sys.argv[1]))
print("ready", flush=True)
sys.stdin.read()
"""

# Stands in for a branch the pipeline missed: pipeline.run writes whatever the mode.
WRITES_ANYWAY = """
import os, sys

from plyrix import cli, pipeline, sidecars
from plyrix.domain import Kind, Track

library, *flags = sys.argv[1:]


def writes_anyway(settings, start):
    track = Track(os.path.join(os.fsencode(library), b"Album"), b"t.flac", b"t")
    sidecars.publish(track, Kind.LRC, b"[00:01.00] invented line\\n")
    return pipeline.Counts()


pipeline.run = writes_anyway
cli.app(["run", library, *flags])
"""


def dry_run(run_cli: RunCli, stub: StubLrclib, lib: Path, *flags: str, cwd: Path | None = None):  # type: ignore[no-untyped-def]
    return run_cli(
        ["run", lib, "--dry-run", *flags], env={"PLYRIX_LRCLIB_URL": stub.base_url}, cwd=cwd
    )


def real_run(run_cli: RunCli, stub: StubLrclib, lib: Path, *flags: str):  # type: ignore[no-untyped-def]
    return run_cli(["run", lib, *flags], env={"PLYRIX_LRCLIB_URL": stub.base_url})


def everything(*tops: Path) -> dict[str, tuple[str, int, int, int, int, int, int]]:
    """Identify every file and folder under these, so a listing, mtime or inode change shows."""
    found = {}
    for top in tops:
        for folder, _, files in os.walk(top):
            found[folder] = snap(Path(folder))
            for name in files:
                found[os.path.join(folder, name)] = snap(Path(folder) / name)
    return found


def answer_each_track(stub: StubLrclib) -> None:
    """Answer plain to the plain track's lookups, and synced to everything else's."""

    def answer() -> tuple[int, object]:
        wanted = stub.requests[-1].get("artist_name")
        return 200, PLAIN_TRACK_RECORD if wanted == PLAIN_TRACK_ARTIST else RECORD

    stub.next_response = answer  # type: ignore[method-assign]


def fill_library(lib: Path) -> None:
    """Add a track to write, a plain track, and a track beside a .txt that isn't plyrix's."""
    copy_track(lib, "A-write")
    plain = lib / "B-plain"
    plain.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "albumartist_only.flac", plain / "p.flac")
    copy_track(lib, "D-foreign").with_suffix(".txt").write_bytes(FOREIGN)


# synced for A, C and D, with C's own .txt upgraded, and plain for B.
WHOLE_LIBRARY_COUNTS = {
    "synced": 3,
    "plain": 1,
    "written_lrc": 3,
    "written_txt": 1,
    "upgraded": 1,
    "looked_up": 4,
}


def library_with_work_due(
    tmp_path: Path, stub: StubLrclib, run_cli: RunCli
) -> tuple[Path, Path, Path, Path]:
    """Leave a ledger, and a library with every kind of change a real run would make.

    Returns the library, the upgrade's .txt, a temp the ledger recorded and an old unrecorded temp.
    """
    lib = tmp_path / "lib"
    upgrade = copy_track(lib, "C-upgrade").with_suffix(".txt")
    stub.body = PLAIN_ONLY
    first = real_run(run_cli, stub, lib, "--dump-id", "dump-a")
    assert first.returncode == 0, first.stderr
    assert upgrade.exists()

    fill_library(lib)
    recorded_temp = lib / "A-write" / ".plyrix-aaaaaaaa.part"
    recorded_temp.write_bytes(b"lyric text a killed run left\n")
    conn = db.connect(state_db_path(tmp_path))
    db.add_temp(conn, os.fsencode(recorded_temp))
    conn.close()
    old_temp = lib / "B-plain" / ".plyrix-bbbbbbbb.part"
    old_temp.write_bytes(b"lyric text from long ago\n")
    two_hours_ago = time.time() - 2 * HOUR
    os.utime(old_temp, (two_hours_ago, two_hours_ago))

    answer_each_track(stub)
    return lib, upgrade, recorded_temp, old_temp


def test_dry_run_changes_nothing(tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli) -> None:
    lib, upgrade, recorded_temp, old_temp = library_with_work_due(tmp_path, stub_lrclib, run_cli)
    ledger = state_db_path(tmp_path)
    before = everything(lib, ledger.parent)

    proc = dry_run(run_cli, stub_lrclib, lib, "--dump-id", "dump-b")

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == WHOLE_LIBRARY_COUNTS
    after = everything(lib, ledger.parent)
    assert sorted(after) == sorted(before)
    assert after == before

    # The same library really was due all of it, so this proved something.
    real = real_run(run_cli, stub_lrclib, lib, "--dump-id", "dump-b")

    assert real.returncode == 0, real.stderr
    assert nonzero(real.stdout) == WHOLE_LIBRARY_COUNTS
    assert not upgrade.exists()
    assert not recorded_temp.exists()
    assert not old_temp.exists()
    assert (lib / "D-foreign" / "t.txt").read_bytes() == FOREIGN
    assert snap(ledger) != before[str(ledger)]


def test_dry_run_counts_what_a_real_run_would(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    fill_library(lib)
    answer_each_track(stub_lrclib)

    preview = dry_run(run_cli, stub_lrclib, lib, "--dump-id", "dump-a")
    real = real_run(run_cli, stub_lrclib, lib, "--dump-id", "dump-a")

    assert preview.returncode == real.returncode == 0, preview.stderr + real.stderr
    assert parse_counts(preview.stdout) == parse_counts(real.stdout)
    assert nonzero(preview.stdout) == {
        "synced": 2,
        "plain": 1,
        "written_lrc": 2,
        "written_txt": 1,
        "looked_up": 3,
    }


def test_dry_run_creates_no_state_folder(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = dry_run(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / "state").exists()
    assert sidecars_in(lib) == []


@pytest.mark.parametrize("ledger_exists", [False, True], ids=["fresh", "with_a_ledger"])
def test_dry_run_from_inside_the_library(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, ledger_exists: bool
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    if ledger_exists:
        db.connect(state_db_path(tmp_path)).close()

    proc = dry_run(run_cli, stub_lrclib, lib, cwd=track.parent)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert sidecars_in(lib) == []


def test_dry_run_with_a_locked_ledger_exits_75(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, state_db_path(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline() == b"ready\n"
        before = everything(lib)
        started = time.monotonic()

        proc = dry_run(run_cli, stub_lrclib, lib)

        assert proc.returncode == EXIT_LOCKED, proc.stderr
        assert time.monotonic() - started < LOCKED_WITHIN_S
        assert everything(lib) == before
    finally:
        holder.send_signal(signal.SIGKILL)
        holder.communicate()


def test_dry_run_with_a_hot_journal_exits_2(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    assert real_run(run_cli, stub_lrclib, lib).returncode == 0
    ledger = state_db_path(tmp_path)
    journal = leave_hot_journal(ledger)
    before = everything(lib, ledger.parent)

    proc = dry_run(run_cli, stub_lrclib, lib)

    assert proc.returncode == 2, proc.stderr
    assert b"normal run" in proc.stderr
    assert parse_counts(proc.stdout) == {}
    assert journal.exists()
    assert everything(lib, ledger.parent) == before

    # The way out it names works.
    assert real_run(run_cli, stub_lrclib, lib).returncode == 0
    assert dry_run(run_cli, stub_lrclib, lib).returncode == 0


def test_dry_run_still_stops_on_preflight(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    assert real_run(run_cli, stub_lrclib, lib).returncode == 0
    ledger = state_db_path(tmp_path)
    before = everything(lib, ledger.parent)
    stub_lrclib.preflight_search = (500, UNKNOWN_ERROR)

    proc = dry_run(run_cli, stub_lrclib, lib)

    assert proc.returncode == 1, proc.stderr
    assert everything(lib, ledger.parent) == before


@pytest.mark.parametrize(
    ("flags", "code"),
    [(["--dry-run"], EXIT_TRIPPED), ([], 0)],
    ids=["dry_run", "real_run"],
)
def test_dry_run_a_write_trips_the_guard(
    tmp_path: Path, stub_lrclib: StubLrclib, flags: list[str], code: int
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    before = everything(lib)
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("PLYRIX_")}

    proc = subprocess.run(
        [sys.executable, "-c", WRITES_ANYWAY, lib, *flags],
        env={
            **inherited,
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "PLYRIX_LRCLIB_URL": stub_lrclib.base_url,
        },
        capture_output=True,
        check=False,
        timeout=60,
    )

    assert proc.returncode == code, proc.stderr
    if flags:
        assert b"tripwire" in proc.stderr
        assert everything(lib) == before
    else:
        # The same write with the mode off goes through, so the mode is what stopped it.
        assert track.with_suffix(".lrc").exists()


def test_dry_run_prints_one_line_saying_nothing_changed(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = dry_run(run_cli, stub_lrclib, lib)
    real = real_run(run_cli, stub_lrclib, lib)

    lines = non_notice_lines(proc.stderr)
    assert len(lines) == 1
    assert lines[0].startswith(b"plyrix:")
    assert b"dry run" in lines[0]
    assert b"nothing" in lines[0]
    assert b"pre-release" in proc.stderr.splitlines()[0]
    assert all(re.fullmatch(rb"[a-z_]+=\d+", line) for line in proc.stdout.splitlines())
    assert parse_counts(proc.stdout)
    assert non_notice_lines(real.stderr) == []


@pytest.mark.parametrize("flags", [[], ["--verbose"]], ids=["counts", "verbose"])
def test_dry_run_holds_no_lyric_text(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, flags: list[str]
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = dry_run(run_cli, stub_lrclib, lib, *flags)

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["written_lrc"] == 1
    assert leaks(tmp_path, proc, lib) == []
    assert sidecars_in(lib) == []


@pytest.mark.parametrize("source", ["variable", "config_file"])
def test_dry_run_setting_behaves_like_the_flag(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, source: str
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    env = {"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    args: list[str | bytes | Path] = ["run", lib]
    if source == "variable":
        env["PLYRIX_DRY_RUN"] = "1"
    else:
        config_file = tmp_path / "plyrix.toml"
        config_file.write_text("dry_run = true\n", encoding="utf-8")
        args += ["--config", config_file]

    proc = run_cli(args, env=env)

    assert proc.returncode == 0, proc.stderr
    assert sidecars_in(lib) == []
    assert not (tmp_path / "state").exists()
    assert parse_counts(proc.stdout)["written_lrc"] == 1
    assert b"dry run" in proc.stderr


def test_a_real_run_after_a_dry_run_writes_as_usual(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    audio_before = snap(track)
    assert dry_run(run_cli, stub_lrclib, lib).returncode == 0

    proc = real_run(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert track.with_suffix(".lrc").exists()
    assert snap(track) == audio_before
    settled = real_run(run_cli, stub_lrclib, lib)
    assert nonzero(settled.stdout) == {"synced": 1, "skipped_unchanged": 1}


def test_dry_run_leaves_a_settled_library_alone(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    assert real_run(run_cli, stub_lrclib, lib, "--dump-id", "dump-a").returncode == 0
    ledger = state_db_path(tmp_path)
    before = everything(lib, ledger.parent)
    requests = len(stub_lrclib.requests)

    proc = dry_run(run_cli, stub_lrclib, lib, "--dump-id", "dump-a")

    assert nonzero(proc.stdout) == {"synced": 1, "skipped_unchanged": 1}
    assert len(stub_lrclib.requests) == requests
    assert everything(lib, ledger.parent) == before


def test_dry_run_counts_a_replace_without_making_it(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    stub_lrclib.body = {**RECORD, "syncedLyrics": STALE_SYNCED}
    assert real_run(run_cli, stub_lrclib, lib, "--dump-id", "dump-a").returncode == 0
    # A 1 s file with the same tags, as a re-rip would leave.
    shutil.copyfile(FIXTURES / "embedded_token.flac", track)
    stub_lrclib.body = {**RECORD, "id": 9, "duration": 1.0, "syncedLyrics": FITTING_SYNCED}
    ledger = state_db_path(tmp_path)
    before = everything(lib, ledger.parent)
    expected = {"synced": 1, "lrc_stale": 1, "written_lrc": 1, "looked_up": 1}

    proc = dry_run(run_cli, stub_lrclib, lib, "--dump-id", "dump-a")

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == expected
    assert everything(lib, ledger.parent) == before

    real = real_run(run_cli, stub_lrclib, lib, "--dump-id", "dump-a")

    assert nonzero(real.stdout) == expected
    assert track.with_suffix(".lrc").read_bytes() == sidecars.encode(FITTING_SYNCED)


def test_dry_run_adopts_only_in_its_copy(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    assert real_run(run_cli, stub_lrclib, lib).returncode == 0
    ledger = state_db_path(tmp_path)
    ledger.unlink()
    before = everything(lib, ledger.parent)
    requests = len(stub_lrclib.requests)

    proc = dry_run(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "adopted": 1}
    assert len(stub_lrclib.requests) == requests
    assert not ledger.exists()
    assert everything(lib, ledger.parent) == before


def killed_runs_work(tmp_path: Path, lib: Path) -> tuple[Path, Path, Path, Path]:
    """Leave what a kill leaves: a finished write, a claim and two temps, as a ledger records them.

    Returns the finished sidecar, the empty claim, the recorded temp and an old unrecorded temp.
    """
    finished = copy_track(lib, "finished").with_suffix(".lrc")
    finished.write_bytes(str(RECORD["syncedLyrics"]).encode())
    claim = copy_track(lib, "claimed").with_suffix(".lrc")
    claim.write_bytes(b"")
    recorded_temp = lib / "claimed" / ".plyrix-aaaaaaaa.part"
    recorded_temp.write_bytes(b"lyric text a killed run left\n")
    old_temp = lib / "finished" / ".plyrix-bbbbbbbb.part"
    old_temp.write_bytes(b"lyric text from long ago\n")
    two_hours_ago = time.time() - 2 * HOUR
    os.utime(old_temp, (two_hours_ago, two_hours_ago))

    conn = db.connect(state_db_path(tmp_path))
    run_id = db.begin_run(conn, "test")
    digest = hashlib.sha256(finished.read_bytes()).hexdigest()
    db.add_intent(conn, os.fsencode(finished), Kind.LRC, digest, finished.stat().st_size, run_id)
    db.add_intent(conn, os.fsencode(claim), Kind.LRC, "other", 1, run_id)
    db.mark_claiming(conn, os.fsencode(claim))
    db.add_temp(conn, os.fsencode(recorded_temp))
    conn.close()
    return finished, claim, recorded_temp, old_temp


def test_recover_with_remove_off_updates_only_the_copy(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    finished, claim, recorded_temp, _ = killed_runs_work(tmp_path, lib)
    ledger = state_db_path(tmp_path)
    before = everything(lib, ledger.parent)
    copy = db.connect_snapshot(ledger)

    pipeline.recover(copy, os.fsencode(lib), remove=False)

    assert everything(lib, ledger.parent) == before
    assert db.intent_rows(copy) == []
    assert db.temp_rows(copy) == []
    assert db.owned_row(copy, os.fsencode(finished)) is not None
    assert db.owned_row(copy, os.fsencode(claim)) is None
    copy.close()
    assert recorded_temp.exists()


def test_recover_with_remove_on_is_what_a_real_run_does(tmp_path: Path) -> None:
    # The same state as the test above, so each of those files really was at stake.
    lib = tmp_path / "lib"
    _, claim, recorded_temp, old_temp = killed_runs_work(tmp_path, lib)
    conn = db.connect(state_db_path(tmp_path))

    pipeline.recover(conn, os.fsencode(lib))

    conn.close()
    assert not claim.exists()
    assert not recorded_temp.exists()
    assert not old_temp.exists()


def test_dry_run_counts_a_killed_runs_empty_claim_as_blocked(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    # The accepted cost: only a real run can remove the claim and retry the track.
    lib = tmp_path / "lib"
    _, claim, _, _ = killed_runs_work(tmp_path, lib)
    ledger = state_db_path(tmp_path)
    before = everything(lib, ledger.parent)

    proc = dry_run(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["blocked_foreign"] == 1
    assert everything(lib, ledger.parent) == before

    real = real_run(run_cli, stub_lrclib, lib)

    assert parse_counts(real.stdout)["blocked_foreign"] == 0
    assert claim.read_bytes() != b""


def test_dry_run_counts_a_killed_upgrade_it_cant_finish(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    txt = track.with_suffix(".txt")
    stub_lrclib.body = PLAIN_ONLY
    assert real_run(run_cli, stub_lrclib, lib).returncode == 0
    # A kill just after the .lrc went in leaves both, and both rows.
    lrc = track.with_suffix(".lrc")
    data = sidecars.encode(str(RECORD["syncedLyrics"]))
    lrc.write_bytes(data)
    ledger = state_db_path(tmp_path)
    conn = db.connect(ledger)
    run_id = db.begin_run(conn, "test")
    db.add_intent(
        conn, os.fsencode(lrc), Kind.LRC, hashlib.sha256(data).hexdigest(), len(data), run_id
    )
    db.mark_published(conn, os.fsencode(lrc))
    conn.close()
    before = everything(lib, ledger.parent)

    proc = dry_run(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["upgraded"] == 1
    assert txt.exists()
    assert everything(lib, ledger.parent) == before
