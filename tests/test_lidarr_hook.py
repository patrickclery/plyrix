"""The Lidarr hook, through the real console script, with a fake Lidarr environment.

Lidarr sets mixed-case names, which Linux reads case-sensitively, so every test spells them exactly.
"""

import hashlib
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import (
    RunCli,
    StubLrclib,
    StubPlex,
    copy_track,
    leaks,
    lidarr_env,
    nonzero,
    plex_track,
    queue_sidecars,
    refresh_flags,
    settings_for,
    sidecars_in,
    state_db_path,
)
from plyrix import config, hook, lidarr, pipeline, spool
from plyrix.config import Settings
from plyrix.state import db


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hook_env(lib: Path, stub: StubLrclib, added: str) -> dict[str, str]:
    return {
        "PLYRIX_MUSIC_ROOT": str(lib),
        "PLYRIX_LRCLIB_URL": stub.base_url,
        "Lidarr_EventType": "AlbumDownload",
        "Lidarr_AddedTrackPaths": added,
        "Lidarr_Artist_Path": str(lib),
        "Lidarr_Album_Id": "12",
    }


def test_an_import_writes_the_album_sidecar_and_nothing_else(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    a = copy_track(lib, "A")
    b = copy_track(lib, "B")
    before = {a: sha(a), b: sha(b)}

    proc = run_cli(["lidarr-hook"], env=hook_env(lib, stub_lrclib, str(a)))

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == b""
    assert a.with_suffix(".lrc").exists()
    assert sidecars_in(lib / "B") == []
    assert nonzero(proc.stdout)["written_lrc"] == 1
    assert {path: sha(path) for path in before} == before


def assert_refused(
    tmp_path: Path, stub: StubLrclib, proc: "subprocess.CompletedProcess[bytes]"
) -> None:
    assert proc.returncode == 0, proc.stderr
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert b"music_root (PLYRIX_MUSIC_ROOT)" in lines[0]
    assert os.fsencode(tmp_path) not in lines[0]
    assert b"/" not in lines[0]
    assert sidecars_in(tmp_path) == []
    assert not state_db_path(tmp_path).exists()
    assert stub.requests == []
    assert stub.probes == []


def test_a_folder_outside_music_root_is_refused(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, "A")
    outside = copy_track(tmp_path / "elsewhere", "X")

    proc = run_cli(["lidarr-hook"], env=hook_env(lib, stub_lrclib, str(outside)))

    assert_refused(tmp_path, stub_lrclib, proc)


def test_a_dotdot_path_is_resolved_before_the_check(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, "A")
    copy_track(tmp_path / "elsewhere", ".")
    sneaky = f"{lib}/A/../../elsewhere/t.flac"

    proc = run_cli(["lidarr-hook"], env=hook_env(lib, stub_lrclib, sneaky))

    assert_refused(tmp_path, stub_lrclib, proc)


def test_the_lowercase_names_lidarr_really_sets_are_read(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    # Lidarr collects its variables in a .NET StringDictionary, which lowercases them.
    lib = tmp_path / "lib"
    a = copy_track(lib, "A")
    copy_track(lib, "B")
    env = {
        "PLYRIX_MUSIC_ROOT": str(lib),
        "PLYRIX_LRCLIB_URL": stub_lrclib.base_url,
        **lidarr_env(added=[a], artist=lib, lowercase=True),
    }

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == b""
    assert a.with_suffix(".lrc").exists()
    assert sidecars_in(lib / "B") == []


def test_no_event_type_in_either_spelling_is_not_a_lidarr_event(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    env = {
        "PLYRIX_MUSIC_ROOT": str(lib),
        "PLYRIX_LRCLIB_URL": stub_lrclib.base_url,
        "LIDARR_EVENTTYPE": "AlbumDownload",
        "lidarr_addedtrackpaths": str(track),
    }

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 2
    lines = proc.stderr.splitlines()
    assert len(lines) == 1
    assert b"Lidarr_EventType" in lines[0]
    assert sidecars_in(lib) == []
    assert not state_db_path(tmp_path).exists()
    assert stub_lrclib.requests == []


def test_fields_reads_either_spelling_under_the_mixed_case_names() -> None:
    lower = {"lidarr_eventtype": "AlbumDownload", "lidarr_album_id": "3", "lidarr_deletedpaths": ""}
    both = {"Lidarr_Album_Id": "", "lidarr_album_id": "4", "Lidarr_EventType": "Test"}

    assert lidarr.fields(lower) == {
        "Lidarr_EventType": "AlbumDownload",
        "Lidarr_DeletedPaths": "",
        "Lidarr_Album_Id": "3",
    }
    assert lidarr.fields(both) == {"Lidarr_EventType": "Test", "Lidarr_Album_Id": "4"}
    assert lidarr.fields({"LIDARR_EVENTTYPE": "Test"}) == {}


def test_a_lowercase_import_is_queued_under_the_mixed_case_names(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    holder = hold_the_ledger(tmp_path)
    try:
        proc = run_cli(
            ["lidarr-hook"],
            env={**setup_env(lib, stub_lrclib), **lidarr_env(added=[track], lowercase=True)},
        )
    finally:
        holder.close()

    assert proc.returncode == 0, proc.stderr
    (file,) = queued(tmp_path)
    assert spool.read(file) == lidarr_env(added=[track])


def test_parse_needs_an_event_type() -> None:
    assert lidarr.parse({}) is None
    assert lidarr.parse({"Lidarr_EventType": ""}) is None
    assert lidarr.parse({"lidarr_eventtype": ""}) is None
    assert lidarr.parse({"LIDARR_EVENTTYPE": "AlbumDownload"}) is None


def test_parse_splits_the_track_paths_and_drops_empty_items() -> None:
    event = lidarr.parse(
        {
            "Lidarr_EventType": "AlbumDownload",
            "Lidarr_AddedTrackPaths": "/m/A/1.flac||/m/A/2.flac|",
            "Lidarr_Artist_Path": "/m",
            "Lidarr_Album_Id": "3",
        }
    )

    assert event is not None
    assert event.type == lidarr.ALBUM_DOWNLOAD
    assert event.added == ("/m/A/1.flac", "/m/A/2.flac")
    assert event.deleted == ()
    assert event.artist_path == "/m"
    assert event.album_id == "3"


def test_an_upgrade_is_an_album_download_with_deleted_paths() -> None:
    base = {"Lidarr_EventType": "AlbumDownload", "Lidarr_AddedTrackPaths": "/m/A/1.flac"}
    plain = lidarr.parse(base)
    upgrade = lidarr.parse({**base, "Lidarr_DeletedPaths": "/m/A/old.mp3"})
    empty = lidarr.parse({**base, "Lidarr_DeletedPaths": ""})

    assert plain is not None and not plain.is_upgrade
    assert upgrade is not None and upgrade.is_upgrade
    assert upgrade.deleted == ("/m/A/old.mp3",)
    assert empty is not None and not empty.is_upgrade


def test_album_folders_are_the_sorted_distinct_parents() -> None:
    event = lidarr.parse(
        {
            "Lidarr_EventType": "AlbumDownload",
            "Lidarr_AddedTrackPaths": "/m/B/1.flac|/m/A/CD2/1.flac|/m/A/CD1/2.flac|/m/A/CD1/1.flac",
        }
    )

    assert event is not None
    assert lidarr.album_folders(event, (), lambda _folder: True) == [
        "/m/A/CD1",
        "/m/A/CD2",
        "/m/B",
    ]


def test_keys_are_the_exact_mixed_case_names() -> None:
    assert lidarr.KEYS == (
        "Lidarr_EventType",
        "Lidarr_AddedTrackPaths",
        "Lidarr_DeletedPaths",
        "Lidarr_Artist_Path",
        "Lidarr_Album_Id",
    )


def test_contain_refuses_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    lib = tmp_path / "lib"
    (lib / "A").mkdir(parents=True)
    (tmp_path / "out").mkdir()
    (tmp_path / "link").symlink_to(lib / "A")
    settings = Settings(
        music_root=lib, lrclib_url="http://127.0.0.1:1/api", state_db=tmp_path / "s.db"
    )

    inside, refused = config.contain(
        settings,
        [str(lib / "A"), str(tmp_path / "out"), "", "a\x00b", str(tmp_path / "link"), b"/"],
    )

    assert inside == [os.path.realpath(os.fsencode(lib / "A"))]
    assert refused == [os.fsencode(tmp_path / "out"), b"", b"a\x00b", b"/"]


def tree_under(path: Path) -> list[str]:
    return sorted(str(entry) for entry in path.rglob("*"))


def setup_env(lib: Path, stub: StubLrclib) -> dict[str, str]:
    return {"PLYRIX_MUSIC_ROOT": str(lib), "PLYRIX_LRCLIB_URL": stub.base_url}


def test_event_with_a_working_setup(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, "A")
    env = {
        **setup_env(lib, stub_lrclib),
        **lidarr_env("Test"),
        "Lidarr_InstanceName": "Lidarr",
        "Lidarr_ApplicationUrl": "",
    }
    before = tree_under(tmp_path)

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == b""
    assert len(proc.stdout.splitlines()) == 1
    assert not state_db_path(tmp_path).exists()
    assert stub_lrclib.requests == []
    assert stub_lrclib.probes == []
    assert tree_under(tmp_path) == before


@pytest.mark.parametrize(
    ("case", "named"),
    [
        ("no_root", b"music_root (PLYRIX_MUSIC_ROOT)"),
        ("missing_root", b"music_root (PLYRIX_MUSIC_ROOT)"),
        ("map_outside", b"lidarr_path_map (PLYRIX_LIDARR_PATH_MAP)"),
        ("public_lrclib", b"lrclib_url (PLYRIX_LRCLIB_URL)"),
    ],
)
def test_event_with_a_broken_setup(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, case: str, named: bytes
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    env = {**setup_env(lib, stub_lrclib), **lidarr_env("Test")}
    if case == "no_root":
        del env["PLYRIX_MUSIC_ROOT"]
    elif case == "missing_root":
        env["PLYRIX_MUSIC_ROOT"] = str(tmp_path / "gone")
    elif case == "map_outside":
        env["PLYRIX_LIDARR_PATH_MAP"] = f'["/music={tmp_path / "elsewhere"}"]'
    else:
        env["PLYRIX_LRCLIB_URL"] = "https://lrclib.net/api"

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 1, proc.stderr
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert named in lines[0]
    assert not state_db_path(tmp_path).exists()


IGNORED = [
    "Grab",
    "Rename",
    "TrackRetag",
    "ArtistAdd",
    "ArtistDeleted",
    "AlbumDeleted",
    "HealthIssue",
    "HealthRestored",
    "ApplicationUpdate",
    "SomethingNew",
]


@pytest.mark.parametrize("event_type", IGNORED)
def test_ignored_events(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, event_type: str
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    env = {
        **setup_env(lib, stub_lrclib),
        **lidarr_env(event_type, added=[track], artist=lib),
        "Lidarr_TrackFile_Paths": str(track),
    }

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == b""
    assert proc.stderr == b""
    assert not state_db_path(tmp_path).exists()
    assert stub_lrclib.requests == []
    assert stub_lrclib.probes == []
    assert sidecars_in(lib) == []


def test_an_upgrade_runs_like_an_import(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    env = {
        **setup_env(lib, stub_lrclib),
        **lidarr_env(added=[track], deleted=[lib / "A" / "old.mp3"], artist=lib, album_id="9"),
    }

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == b""
    assert track.with_suffix(".lrc").exists()
    first = proc.stdout.splitlines()[0]
    assert b"upgrade" in first
    assert b"9" in first


def test_an_album_id_that_is_not_a_number_is_left_out(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    env = {**setup_env(lib, stub_lrclib), **lidarr_env(added=[track], album_id="x\x1by")}

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == b"plyrix: Lidarr import of an album"


def test_a_mapped_import(tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    env = {
        **setup_env(lib, stub_lrclib),
        **lidarr_env(added=["/music/A/t.flac"], artist="/music"),
        "PLYRIX_LIDARR_PATH_MAP": f'["/music={lib}"]',
    }

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == b""
    assert track.with_suffix(".lrc").exists()


def test_no_track_paths_falls_back_to_the_artist_folder(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    x = copy_track(lib, "Artist/X")
    y = copy_track(lib, "Artist/Y")
    other = copy_track(lib, "Other/Z")
    env = {**setup_env(lib, stub_lrclib), **lidarr_env(artist=lib / "Artist")}

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert x.with_suffix(".lrc").exists()
    assert y.with_suffix(".lrc").exists()
    assert sidecars_in(other.parent) == []


def test_exit_is_zero_when_a_setting_is_missing(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    env = {"PLYRIX_MUSIC_ROOT": str(lib), **lidarr_env(added=[track], artist=lib)}

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 0
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert b"lrclib_url (PLYRIX_LRCLIB_URL)" in lines[0]


def test_exit_is_zero_when_the_album_holds_no_audio(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    (lib / "A").mkdir(parents=True)
    (lib / "A" / "cover.jpg").write_bytes(b"not audio")
    env = {**setup_env(lib, stub_lrclib), **lidarr_env(added=[lib / "A" / "t.flac"], artist=lib)}

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 0
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert b"no audio" in lines[0]


def test_exit_is_zero_when_a_sidecar_write_fails(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    env = {**setup_env(lib, stub_lrclib), **lidarr_env(added=[track], artist=lib)}
    track.parent.chmod(0o555)
    try:
        proc = run_cli(["lidarr-hook"], env=env)
    finally:
        track.parent.chmod(0o755)

    assert proc.returncode == 0
    assert nonzero(proc.stdout)["write_failed"] == 1
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert b"next run" in lines[0]


HOOK_CHILD = """
import os, sys

from plyrix import cli, pipeline

audio = sys.argv[1]


def attempt_write(settings, *starts, **options):
    try:
        open(audio, "r+b")
    except BaseException:
        pass
    return pipeline.Counts()


pipeline.run = attempt_write
cli.app(["lidarr-hook"])
"""


def test_a_tripwire_stop_still_exits_zero(tmp_path: Path, stub_lrclib: StubLrclib) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    before = sha(track)
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("PLYRIX_")}

    proc = subprocess.run(
        [sys.executable, "-c", HOOK_CHILD, track],
        env={
            **inherited,
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            **setup_env(lib, stub_lrclib),
            **lidarr_env(added=[track], artist=lib),
        },
        capture_output=True,
        check=False,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert b"tripwire" in proc.stderr
    assert b"Traceback" not in proc.stderr
    assert sha(track) == before


def last_run_end(ledger: Path) -> str | None:
    with sqlite3.connect(ledger) as conn:
        row = conn.execute("SELECT end_reason FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return None if row is None else str(row[0])


def test_a_deadline_already_past_stops_before_any_track(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, "A")
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)

    with pytest.raises(pipeline.BudgetStop) as caught:
        pipeline.run(settings, os.fsencode(lib), deadline=0.0, clock=lambda: 1.0)

    assert caught.value.counts.looked_up == 0
    assert last_run_end(settings.state_db) == "budget"


def test_the_budget_is_checked_between_tracks(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "lib"
    first = copy_track(lib, "A", "a.flac")
    copy_track(lib, "A", "b.flac")
    copy_track(lib, "A", "c.flac")
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    calls: list[int] = []

    def clock() -> float:
        calls.append(1)
        return 0.0 if len(calls) == 1 else 2.0

    with pytest.raises(pipeline.BudgetStop) as caught:
        pipeline.run(settings, os.fsencode(lib), deadline=1.0, clock=clock)

    assert caught.value.counts.looked_up == 1
    assert first.with_suffix(".lrc").exists()
    with sqlite3.connect(settings.state_db) as conn:
        assert conn.execute("SELECT status FROM sidecars").fetchall() == [("published",)]
    assert last_run_end(settings.state_db) == "budget"


def test_no_deadline_changes_nothing(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, "A", "a.flac")
    copy_track(lib, "A", "b.flac")
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)

    counts = pipeline.run(settings, os.fsencode(lib), deadline=None, clock=lambda: 1e18)

    assert counts.written_lrc == 2
    assert last_run_end(settings.state_db) == "finished"


def spool_dir(tmp_path: Path) -> Path:
    return state_db_path(tmp_path).parent / "lidarr-spool"


def queued(tmp_path: Path) -> list[Path]:
    return spool.entries(spool_dir(tmp_path))


def hold_the_ledger(tmp_path: Path) -> db.Connection:
    state_db_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    return db.connect(state_db_path(tmp_path))


def test_a_locked_ledger_queues_the_album(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    fields = lidarr_env(added=[track], artist=lib, album_id="4")
    holder = hold_the_ledger(tmp_path)
    try:
        proc = run_cli(["lidarr-hook"], env={**setup_env(lib, stub_lrclib), **fields})
    finally:
        holder.close()

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == b""
    assert b"another plyrix run is using the state database" in proc.stdout
    assert b"queued" in proc.stdout
    files = queued(tmp_path)
    assert len(files) == 1
    assert spool.read(files[0]) == fields
    assert sidecars_in(lib) == []


def test_a_queued_event_keeps_lidarr_paths(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, "A")
    fields = lidarr_env(added=["/music/A/t.flac"], artist="/music")
    env = {**setup_env(lib, stub_lrclib), **fields, "PLYRIX_LIDARR_PATH_MAP": f'["/music={lib}"]'}
    holder = hold_the_ledger(tmp_path)
    try:
        proc = run_cli(["lidarr-hook"], env=env)
    finally:
        holder.close()

    assert proc.returncode == 0, proc.stderr
    (file,) = queued(tmp_path)
    assert spool.read(file)["Lidarr_AddedTrackPaths"] == "/music/A/t.flac"


def closed_url() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/api"


def test_lrclib_down_queues_the_album(tmp_path: Path, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    env = {
        "PLYRIX_MUSIC_ROOT": str(lib),
        "PLYRIX_LRCLIB_URL": closed_url(),
        **lidarr_env(added=[track], artist=lib),
    }

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert len(queued(tmp_path)) == 1
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert b"LRCLIB is unavailable" in lines[0]
    assert b"queued" in lines[0]


def test_lrclib_failing_mid_album_queues_it(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    tracks = [copy_track(lib, "A", f"{n}.flac") for n in range(6)]
    stub_lrclib.status, stub_lrclib.body = 503, {}
    env = {**setup_env(lib, stub_lrclib), **lidarr_env(added=tracks, artist=lib)}

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert len(queued(tmp_path)) == 1
    assert nonzero(proc.stdout)["unavailable"] == 5
    assert b"queued" in proc.stderr


def test_the_budget_queues_the_album(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    env = {
        **setup_env(lib, stub_lrclib),
        **lidarr_env(added=[track], artist=lib),
        "PLYRIX_LIDARR_BUDGET": "0.000001",
    }

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == b""
    assert len(queued(tmp_path)) == 1
    assert b"lidarr_budget (PLYRIX_LIDARR_BUDGET)" in proc.stdout


STATE_CHILD = """
from plyrix import cli, pipeline
from plyrix.state import db


def unusable(settings, *starts, **options):
    raise db.StateError("plyrix: the state database can't be written", 2)


pipeline.run = unusable
cli.app(["lidarr-hook"])
"""


def test_a_state_error_queues_the_album(tmp_path: Path, stub_lrclib: StubLrclib) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    fields = lidarr_env(added=[track], artist=lib)
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("PLYRIX_")}

    proc = subprocess.run(
        [sys.executable, "-c", STATE_CHILD],
        env={
            **inherited,
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            **setup_env(lib, stub_lrclib),
            **fields,
        },
        capture_output=True,
        check=False,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    lines = proc.stderr.splitlines()
    assert len(lines) == 2, proc.stderr
    assert b"can't be written" in lines[0]
    assert b"queued for the next drain" in lines[1]
    (file,) = queued(tmp_path)
    assert spool.read(file) == fields


def test_no_folder_found_is_not_silent(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "lib").mkdir()
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    event = lidarr.Event(lidarr.ALBUM_DOWNLOAD, added=("/nope/a/t.flac",), artist_path="/nope/a")

    result = hook.handle(settings, event)

    assert result.outcome is hook.Outcome.REFUSED
    assert result.refused == 0
    assert result.reason == hook.NO_FOLDER
    assert result.message is not None


def test_a_wrong_path_map_queues_the_album_and_says_so(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, "A")
    fields = lidarr_env(added=["/music/A/t.flac"], artist="/music")
    wrong = f'["/music={lib}/Missing"]'
    env = {**setup_env(lib, stub_lrclib), **fields, "PLYRIX_LIDARR_PATH_MAP": wrong}

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 0, proc.stderr
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert b"lidarr_path_map (PLYRIX_LIDARR_PATH_MAP)" in lines[0]
    assert b"queued" in lines[0]
    (file,) = queued(tmp_path)
    assert spool.read(file) == fields
    assert sidecars_in(lib) == []
    assert stub_lrclib.requests == []


def test_a_dry_run_never_queues(tmp_path: Path, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    env = {
        "PLYRIX_MUSIC_ROOT": str(lib),
        "PLYRIX_LRCLIB_URL": closed_url(),
        "PLYRIX_DRY_RUN": "true",
        **lidarr_env(added=[track], artist=lib),
    }

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert not spool_dir(tmp_path).exists()
    assert b"dry run" in proc.stderr


def test_an_unwritable_spool_loses_nothing_silently(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    locked = tmp_path / "ro"
    locked.mkdir(mode=0o500)
    env = {
        **setup_env(lib, stub_lrclib),
        **lidarr_env(added=[track], artist=lib),
        "PLYRIX_LIDARR_SPOOL": str(locked / "spool"),
    }
    holder = hold_the_ledger(tmp_path)
    try:
        proc = run_cli(["lidarr-hook"], env=env)
    finally:
        holder.close()
        locked.chmod(0o700)

    assert proc.returncode == 0
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert b"couldn't be queued" in lines[0]
    assert b"next scheduled run" in lines[0]
    assert not (locked / "spool").exists()


def test_event_with_a_spool_that_cannot_be_written(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    folder = tmp_path / "spool"
    folder.mkdir(mode=0o500)
    env = {
        **setup_env(lib, stub_lrclib),
        **lidarr_env("Test"),
        "PLYRIX_LIDARR_SPOOL": str(folder),
    }
    try:
        proc = run_cli(["lidarr-hook"], env=env)
    finally:
        folder.chmod(0o700)

    assert proc.returncode == 1
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert b"lidarr_spool (PLYRIX_LIDARR_SPOOL)" in lines[0]


def test_event_with_a_spool_not_made_yet_creates_nothing(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    env = {**setup_env(lib, stub_lrclib), **lidarr_env("Test")}

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert not spool_dir(tmp_path).exists()


def test_nothing_leaks_from_a_hook(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    a = copy_track(lib, "A")
    b = copy_track(lib, "B")
    done = run_cli(["lidarr-hook"], env={**setup_env(lib, stub_lrclib), **lidarr_env(added=[a])})
    holder = hold_the_ledger(tmp_path)
    try:
        queued_proc = run_cli(
            ["lidarr-hook"], env={**setup_env(lib, stub_lrclib), **lidarr_env(added=[b])}
        )
    finally:
        holder.close()

    assert done.returncode == 0 and queued_proc.returncode == 0
    assert len(queued(tmp_path)) == 1
    assert leaks(tmp_path, done, lib) == []
    assert leaks(tmp_path, queued_proc, lib) == []


PLEX_TOKEN = "plyrix-dummy-token-0123456789"


def plex_vars(stub: StubPlex) -> dict[str, str]:
    return {
        "PLYRIX_PLEX_URL": stub.base_url,
        "PLYRIX_PLEX_SECTION": "1",
        "PLYRIX_PLEX_TOKEN": PLEX_TOKEN,
        "PLYRIX_PLEX_PACE": "0",
    }


def real(path: Path) -> bytes:
    return os.fsencode(os.path.realpath(path))


def test_the_hook_refreshes_only_its_album(
    tmp_path: Path, stub_lrclib: StubLrclib, stub_plex: StubPlex, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    a = copy_track(lib, "A")
    b = copy_track(lib, "B")
    earlier = real(b.with_suffix(".lrc"))
    queue_sidecars(state_db_path(tmp_path), [earlier])
    stub_plex.tracks = [plex_track("1", "11", real(a)), plex_track("2", "22", real(b))]
    env = {**setup_env(lib, stub_lrclib), **plex_vars(stub_plex), **lidarr_env(added=[a])}

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == b""
    assert stub_plex.put_keys() == ["11"]
    flags = refresh_flags(state_db_path(tmp_path))
    assert flags[real(a.with_suffix(".lrc"))] == 0
    assert flags[earlier] == 1
    assert nonzero(proc.stdout)["refreshed"] == 1


def test_without_plex_the_sidecar_stays_queued(
    tmp_path: Path, stub_lrclib: StubLrclib, stub_plex: StubPlex, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    a = copy_track(lib, "A")

    proc = run_cli(["lidarr-hook"], env={**setup_env(lib, stub_lrclib), **lidarr_env(added=[a])})

    assert proc.returncode == 0, proc.stderr
    assert stub_plex.requests == []
    assert refresh_flags(state_db_path(tmp_path)) == {real(a.with_suffix(".lrc")): 1}


def test_a_new_album_plex_has_not_scanned(
    tmp_path: Path, stub_lrclib: StubLrclib, stub_plex: StubPlex, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    a = copy_track(lib, "A")
    env = {**setup_env(lib, stub_lrclib), **plex_vars(stub_plex), **lidarr_env(added=[a])}

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == b""
    assert nonzero(proc.stdout)["not_in_plex"] == 1
    assert stub_plex.put_keys() == []
    assert refresh_flags(state_db_path(tmp_path)) == {real(a.with_suffix(".lrc")): 1}


def test_plex_down_after_the_album(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    a = copy_track(lib, "A")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        **setup_env(lib, stub_lrclib),
        "PLYRIX_PLEX_URL": f"http://127.0.0.1:{port}",
        "PLYRIX_PLEX_SECTION": "1",
        "PLYRIX_PLEX_TOKEN": PLEX_TOKEN,
        **lidarr_env(added=[a]),
    }

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 0
    assert a.with_suffix(".lrc").exists()
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert b"Plex didn't answer" in lines[0]
    assert b"plex-refresh" in lines[0]
    assert refresh_flags(state_db_path(tmp_path)) == {real(a.with_suffix(".lrc")): 1}


def test_no_refresh_once_the_budget_is_spent(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    stub_plex: StubPlex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib = tmp_path / "lib"
    a = copy_track(lib, "A")
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch).model_copy(
        update={"plex_url": stub_plex.base_url, "plex_section": 1}
    )
    monkeypatch.setattr(pipeline, "run", lambda *_a, **_k: pipeline.Counts(written_lrc=1))
    event = lidarr.parse(lidarr_env(added=[a]))
    assert event is not None

    result = hook.handle(settings, event, deadline=time.monotonic() - 1)

    assert result.outcome is hook.Outcome.DONE
    assert result.plex_reply is None
    assert stub_plex.requests == []


def test_drain_refreshes_what_it_drains(
    tmp_path: Path, stub_lrclib: StubLrclib, stub_plex: StubPlex, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    a = copy_track(lib, "A")
    spool.put(spool_dir(tmp_path), lidarr_env(added=[a]))
    stub_plex.tracks = [plex_track("1", "11", real(a))]

    proc = run_cli(["drain"], env={**setup_env(lib, stub_lrclib), **plex_vars(stub_plex)})

    assert proc.returncode == 0, proc.stderr
    assert stub_plex.put_keys() == ["11"]
    assert nonzero(proc.stdout)["refreshed"] == 1


def test_a_deleted_lrc_comes_back_on_the_next_hook(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    before = sha(track)
    env = {**setup_env(lib, stub_lrclib), **lidarr_env(added=[track], artist=lib)}

    first = run_cli(["lidarr-hook"], env=env)
    assert first.returncode == 0, first.stderr
    assert track.with_suffix(".lrc").exists()
    # As LyricExtractor does on import.
    track.with_suffix(".lrc").unlink()

    second = run_cli(["lidarr-hook"], env=env)

    assert second.returncode == 0, second.stderr
    assert second.stderr == b""
    assert track.with_suffix(".lrc").exists()
    assert nonzero(second.stdout)["written_lrc"] == 1
    assert sha(track) == before
