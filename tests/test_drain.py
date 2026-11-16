"""Emptying the Lidarr spool: plyrix drain, the hook's backlog, and a run that covers the queue."""

import hashlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import (
    RunCli,
    StubLrclib,
    copy_track,
    lidarr_env,
    non_notice_lines,
    parse_counts,
    settings_for,
    sidecars_in,
    state_db_path,
)
from plyrix import hook, spool
from plyrix.state import db

NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def spool_dir(tmp_path: Path) -> Path:
    return state_db_path(tmp_path).parent / "lidarr-spool"


def env_for(lib: Path, stub: StubLrclib, **extra: str) -> dict[str, str]:
    return {"PLYRIX_MUSIC_ROOT": str(lib), "PLYRIX_LRCLIB_URL": stub.base_url, **extra}


def queue(tmp_path: Path, *tracks: Path | str, artist: Path | str = "") -> Path:
    return spool.put(spool_dir(tmp_path), lidarr_env(added=list(tracks), artist=artist))


def hold_the_ledger(tmp_path: Path) -> db.Connection:
    state_db_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    return db.connect(state_db_path(tmp_path))


def closed_url() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/api"


@pytest.mark.parametrize("made", [False, True])
def test_an_empty_spool_is_quiet(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, made: bool
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, "A")
    if made:
        spool_dir(tmp_path).mkdir(parents=True)

    proc = run_cli(["drain"], env=env_for(lib, stub_lrclib))

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == b""
    assert proc.stderr == b""
    assert not state_db_path(tmp_path).exists()
    assert stub_lrclib.requests == []
    assert stub_lrclib.probes == []


def test_drain_runs_each_queued_album(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    a = copy_track(lib, "A")
    b = copy_track(lib, "B")
    before = {a: sha(a), b: sha(b)}
    queue(tmp_path, a, artist=lib)
    queue(tmp_path, b, artist=lib)

    proc = run_cli(["drain"], env=env_for(lib, stub_lrclib))

    assert proc.returncode == 0, proc.stderr
    assert a.with_suffix(".lrc").exists()
    assert b.with_suffix(".lrc").exists()
    assert spool.entries(spool_dir(tmp_path)) == []
    assert parse_counts(proc.stdout)["drained"] == 2
    assert {path: sha(path) for path in before} == before


def test_drain_applies_the_current_path_map(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    queue(tmp_path, "/music/A/t.flac", artist="/music")

    proc = run_cli(
        ["drain"], env=env_for(lib, stub_lrclib, PLYRIX_LIDARR_PATH_MAP=f'["/music={lib}"]')
    )

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").exists()
    assert spool.entries(spool_dir(tmp_path)) == []


def test_an_album_set_aside_before_the_map_was_fixed_runs_once_moved_back(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    queue(tmp_path, "/music/A/t.flac", artist="/music")

    first = run_cli(["drain"], env=env_for(lib, stub_lrclib))

    assert first.returncode == 3, first.stderr
    failed = spool_dir(tmp_path) / "failed"
    (moved,) = failed.iterdir()
    # The README's requeue: move the file from failed/ back into the spool.
    moved.replace(spool_dir(tmp_path) / moved.name)
    fixed = env_for(lib, stub_lrclib, PLYRIX_LIDARR_PATH_MAP=f'["/music={lib}"]')

    second = run_cli(["drain"], env=fixed)

    assert second.returncode == 0, second.stderr
    assert parse_counts(second.stdout)["drained"] == 1
    assert track.with_suffix(".lrc").exists()
    assert spool.entries(spool_dir(tmp_path)) == []
    assert list(failed.iterdir()) == []


def test_drain_waits_for_the_lock(tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    a = copy_track(lib, "A")
    b = copy_track(lib, "B")
    files = [queue(tmp_path, a), queue(tmp_path, b)]
    holder = hold_the_ledger(tmp_path)
    try:
        proc = run_cli(["drain"], env=env_for(lib, stub_lrclib))
    finally:
        holder.close()

    assert proc.returncode == 0, proc.stderr
    assert all(path.exists() for path in files)
    assert sidecars_in(lib) == []
    assert b"wait for the next drain" in proc.stdout
    assert parse_counts(proc.stdout)["left"] == 2


def test_drain_on_lrclib_down(tmp_path: Path, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    file = queue(tmp_path, track)

    proc = run_cli(
        ["drain"], env={"PLYRIX_MUSIC_ROOT": str(lib), "PLYRIX_LRCLIB_URL": closed_url()}
    )

    assert proc.returncode == 1
    assert file.exists()
    lines = [line for line in proc.stderr.splitlines() if b"pre-release" not in line]
    assert len(lines) == 1, proc.stderr
    assert b"LRCLIB" in lines[0]


def test_drain_quarantines_what_it_cannot_run(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    good = copy_track(lib, "Good")
    (lib / "Empty").mkdir()
    outside = copy_track(tmp_path / "elsewhere", "X")
    folder = spool_dir(tmp_path)
    folder.mkdir(parents=True)
    garbage = folder / "00000000T000000Z-00000001.event"
    garbage.write_bytes(b"\xff\xfe not a record")
    grab = spool.put(folder, lidarr_env("Grab", added=[good]))
    away = spool.put(folder, lidarr_env(added=[outside]))
    empty = spool.put(folder, lidarr_env(added=[lib / "Empty" / "t.flac"]))
    valid = spool.put(folder, lidarr_env(added=[good]))

    proc = run_cli(["drain"], env=env_for(lib, stub_lrclib))

    assert proc.returncode == 3, proc.stderr
    for path in (garbage, grab, away, empty):
        assert not path.exists()
        assert (folder / "failed" / path.name).exists()
    assert not valid.exists()
    assert good.with_suffix(".lrc").exists()
    counts = parse_counts(proc.stdout)
    assert counts["quarantined"] == 4
    assert counts["drained"] == 1
    assert sidecars_in(tmp_path / "elsewhere") == []


def test_an_entry_another_drain_took_is_skipped(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy_track(tmp_path / "lib", "A")
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    gone = tmp_path / "state" / "lidarr-spool" / "gone.event"
    monkeypatch.setattr(spool, "entries", lambda _folder: [gone])

    counts = hook.drain_spool(settings)

    assert counts.drained == 0
    assert counts.quarantined == 0
    assert counts.left == 0


def test_the_backlog_stops_at_the_budget(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    folder = hook.spool_folder(settings)
    files = [spool.put(folder, lidarr_env(added=[track])) for _ in range(2)]

    counts = hook.drain_spool(settings, deadline=time.monotonic() - 1)

    assert counts.stopped == hook.BUDGET
    assert counts.left == 2
    assert all(path.exists() for path in files)
    assert sidecars_in(lib) == []


def test_the_hook_drains_the_backlog_after_its_own_album(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    a = copy_track(lib, "A")
    c = copy_track(lib, "C")
    queue(tmp_path, c)

    proc = run_cli(["lidarr-hook"], env={**env_for(lib, stub_lrclib), **lidarr_env(added=[a])})

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == b""
    assert a.with_suffix(".lrc").exists()
    assert c.with_suffix(".lrc").exists()
    assert spool.entries(spool_dir(tmp_path)) == []
    assert parse_counts(proc.stdout)["drained"] == 1


def test_a_queued_hook_does_not_drain(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    a = copy_track(lib, "A")
    c = copy_track(lib, "C")
    earlier = queue(tmp_path, c)
    before = earlier.read_bytes()
    holder = hold_the_ledger(tmp_path)
    try:
        proc = run_cli(["lidarr-hook"], env={**env_for(lib, stub_lrclib), **lidarr_env(added=[a])})
    finally:
        holder.close()

    assert proc.returncode == 0, proc.stderr
    files = spool.entries(spool_dir(tmp_path))
    assert len(files) == 2
    assert earlier in files
    assert earlier.read_bytes() == before
    assert sidecars_in(lib) == []


def aged(path: Path, seconds: float) -> Path:
    when = time.time() + seconds
    os.utime(path, (when, when))
    return path


def test_a_whole_library_run_clears_what_it_covered(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    a = copy_track(lib, "A")
    old = aged(queue(tmp_path, a), -3600)
    new = aged(queue(tmp_path, a), 3600)

    scoped = run_cli(["run", "--path", lib / "A"], env=env_for(lib, stub_lrclib))
    dry = run_cli(["run", "--dry-run"], env=env_for(lib, stub_lrclib))

    assert scoped.returncode == 0, scoped.stderr
    assert dry.returncode == 0, dry.stderr
    assert old.exists()

    whole = run_cli(["run"], env=env_for(lib, stub_lrclib))

    assert whole.returncode == 0, whole.stderr
    assert not old.exists()
    assert new.exists()
    assert b"this run covered 1 queued Lidarr albums" in whole.stdout


def test_a_dry_run_drain_changes_nothing(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    a = copy_track(lib, "A")
    file = queue(tmp_path, a)
    bad = spool.put(spool_dir(tmp_path), lidarr_env("Grab"))

    proc = run_cli(["drain"], env=env_for(lib, stub_lrclib, PLYRIX_DRY_RUN="true"))

    assert proc.returncode in (0, 3), proc.stderr
    assert file.exists()
    assert bad.exists()
    assert not (spool_dir(tmp_path) / "failed").exists()
    assert sidecars_in(lib) == []


@NOT_ROOT
def test_an_unreadable_file_is_set_aside_not_raised(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    folder = hook.spool_folder(settings)
    unreadable = spool.put(folder, lidarr_env(added=[track]))
    valid = spool.put(folder, lidarr_env(added=[track]))
    unreadable.chmod(0)

    counts = hook.drain_spool(settings)

    assert counts.stopped is None
    assert counts.quarantined == 1
    assert counts.drained == 1
    assert (folder / "failed" / unreadable.name).exists()
    assert not valid.exists()


def test_a_spool_that_cannot_be_listed_stops_the_drain(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "lib").mkdir()
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)

    def refuse(_folder: Path) -> list[Path]:
        raise PermissionError

    monkeypatch.setattr(spool, "entries", refuse)

    counts = hook.drain_spool(settings)

    assert counts.stopped == hook.SPOOL
    assert counts.message == hook.SPOOL_UNREADABLE


@NOT_ROOT
def test_drain_on_a_spool_that_cannot_be_listed(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    queue(tmp_path, track)
    spool_dir(tmp_path).chmod(0)
    try:
        proc = run_cli(["drain"], env=env_for(lib, stub_lrclib))
    finally:
        spool_dir(tmp_path).chmod(0o700)

    assert proc.returncode == 2
    assert b"Traceback" not in proc.stderr
    assert b"can't be listed" in proc.stderr


HOOK_CHILD = """
import sys

from plyrix import cli, hook


def crash(*_args, **_options):
    raise RuntimeError("nothing expected this")


setattr(hook, sys.argv[1], crash)
cli.app(["lidarr-hook"])
"""


DRAIN_CHILD = """
from plyrix import cli, hook


def crash(*_args, **_options):
    raise RuntimeError("nothing expected this")


hook.handle = crash
cli.app(["drain"])
"""


def test_an_unexpected_error_in_a_drain_has_its_own_exit_code(
    tmp_path: Path, stub_lrclib: StubLrclib
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    file = queue(tmp_path, track)
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("PLYRIX_")}

    proc = subprocess.run(
        [sys.executable, "-c", DRAIN_CHILD],
        env={
            **inherited,
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            **env_for(lib, stub_lrclib),
        },
        capture_output=True,
        check=False,
        timeout=60,
    )

    assert proc.returncode == 4, proc.stderr
    assert b"Traceback" not in proc.stderr
    lines = non_notice_lines(proc.stderr)
    assert len(lines) == 1, proc.stderr
    assert b"unexpected error" in lines[0]
    assert b"RuntimeError" in lines[0]
    assert file.exists()


def run_crashing_hook(
    tmp_path: Path, stub: StubLrclib, crashing: str, track: Path, **extra: str
) -> "subprocess.CompletedProcess[bytes]":
    """Run the hook in a child whose hook.<crashing> raises an error nothing expects."""
    lib = tmp_path / "lib"
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("PLYRIX_")}
    return subprocess.run(
        [sys.executable, "-c", HOOK_CHILD, crashing],
        env={
            **inherited,
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            **env_for(lib, stub, **extra),
            **lidarr_env(added=[track], artist=lib),
        },
        capture_output=True,
        check=False,
        timeout=60,
    )


def test_an_unexpected_error_in_the_hook_still_exits_zero(
    tmp_path: Path, stub_lrclib: StubLrclib
) -> None:
    track = copy_track(tmp_path / "lib", "A")

    proc = run_crashing_hook(tmp_path, stub_lrclib, "drain_spool", track)

    assert proc.returncode == 0, proc.stderr
    assert b"Traceback" not in proc.stderr
    assert b"unexpected error while running albums queued earlier" in proc.stderr
    assert track.with_suffix(".lrc").exists()
    # The hook's own album ran, so it isn't queued again.
    assert spool.entries(spool_dir(tmp_path)) == []


def test_an_unexpected_error_before_the_run_queues_the_album(
    tmp_path: Path, stub_lrclib: StubLrclib
) -> None:
    track = copy_track(tmp_path / "lib", "A")

    proc = run_crashing_hook(tmp_path, stub_lrclib, "handle", track)

    assert proc.returncode == 0, proc.stderr
    assert b"Traceback" not in proc.stderr
    lines = non_notice_lines(proc.stderr)
    assert len(lines) == 1, proc.stderr
    assert b"unexpected error, so this album is queued for the next drain" in lines[0]
    assert b"RuntimeError" in lines[0]
    (path,) = spool.entries(spool_dir(tmp_path))
    assert spool.read(path)["Lidarr_AddedTrackPaths"] == str(track)
    assert not track.with_suffix(".lrc").exists()


def test_an_unexpected_error_with_no_usable_spool_says_so(
    tmp_path: Path, stub_lrclib: StubLrclib
) -> None:
    track = copy_track(tmp_path / "lib", "A")
    blocked = tmp_path / "blocked"
    blocked.write_bytes(b"")

    proc = run_crashing_hook(
        tmp_path, stub_lrclib, "handle", track, PLYRIX_LIDARR_SPOOL=str(blocked / "spool")
    )

    assert proc.returncode == 0, proc.stderr
    assert b"Traceback" not in proc.stderr
    assert b"the next scheduled run will cover this album" in proc.stderr


def test_an_unexpected_error_in_a_dry_run_queues_nothing(
    tmp_path: Path, stub_lrclib: StubLrclib
) -> None:
    track = copy_track(tmp_path / "lib", "A")

    proc = run_crashing_hook(tmp_path, stub_lrclib, "handle", track, PLYRIX_DRY_RUN="true")

    assert proc.returncode == 0, proc.stderr
    assert b"the next scheduled run will cover this album" in proc.stderr
    assert spool.entries(spool_dir(tmp_path)) == []


def test_an_unmounted_library_keeps_the_whole_queue(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    files = [queue(tmp_path, lib / "A" / "t.flac"), queue(tmp_path, lib / "B" / "t.flac")]

    proc = run_cli(["drain"], env=env_for(lib, stub_lrclib))

    assert proc.returncode == 2
    assert all(path.exists() for path in files)
    assert not (spool_dir(tmp_path) / "failed").exists()
    assert b"is missing or empty" in proc.stderr
    assert parse_counts(proc.stdout)["left"] == 2
    assert stub_lrclib.probes == []


def test_a_missing_music_root_stops_before_the_first_file(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    file = spool.put(hook.spool_folder(settings), lidarr_env(added=["/gone/A/t.flac"]))

    counts = hook.drain_spool(settings)

    assert counts.stopped == hook.ROOT
    assert counts.quarantined == 0
    assert counts.left == 1
    assert file.exists()


def test_a_file_another_drain_set_aside_first_is_skipped(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy_track(tmp_path / "lib", "A")
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    file = spool.put(hook.spool_folder(settings), lidarr_env(added=["/elsewhere/t.flac"]))

    def raced(*_args: object, **_options: object) -> hook.Result:
        file.unlink()
        return hook.Result(hook.Outcome.REFUSED, refused=1)

    monkeypatch.setattr(hook, "handle", raced)

    counts = hook.drain_spool(settings)

    assert counts.stopped is None
    assert counts.quarantined == 0
    assert counts.left == 0


def test_a_symlinked_failed_folder_stops_the_drain(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy_track(tmp_path / "lib", "A")
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    folder = hook.spool_folder(settings)
    file = spool.put(folder, {"Lidarr_EventType": "Grab"})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (folder / "failed").symlink_to(elsewhere)

    counts = hook.drain_spool(settings)

    assert counts.stopped == hook.SPOOL
    assert counts.quarantined == 0
    assert file.exists()
    assert list(elsewhere.iterdir()) == []


def test_an_error_from_the_run_is_not_blamed_on_the_spool(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = copy_track(tmp_path / "lib", "A")
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    file = spool.put(hook.spool_folder(settings), lidarr_env(added=[track]))

    def broken(*_args: object, **_options: object) -> hook.Result:
        raise ConnectionError("not the spool")

    monkeypatch.setattr(hook, "handle", broken)

    with pytest.raises(ConnectionError):
        hook.drain_spool(settings)
    assert file.exists()
