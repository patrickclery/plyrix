"""A run scoped to chosen folders: the multi-start pipeline in process, and run --path."""

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from conftest import (
    RunCli,
    StubLrclib,
    copy_track,
    nonzero,
    settings_for,
    sidecars_in,
    state_db_path,
)
from plyrix import pipeline


def gets(stub: StubLrclib) -> list[dict[str, str]]:
    return [request for request in stub.requests if request["path"] == "/api/get"]


def test_two_starts_write_sidecars_in_those_folders_only(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "lib"
    a, b, c = (copy_track(lib, name) for name in ("A", "B", "C"))
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)

    counts = pipeline.run(settings, os.fsencode(lib / "A"), os.fsencode(lib / "B"))

    assert counts.written_lrc == 2
    assert a.with_suffix(".lrc").exists()
    assert b.with_suffix(".lrc").exists()
    assert sidecars_in(c.parent) == []


def test_a_start_inside_another_is_walked_once(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, "A/CD1")
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)

    counts = pipeline.run(settings, os.fsencode(lib / "A"), os.fsencode(lib / "A" / "CD1"))

    assert len(gets(stub_lrclib)) == 1
    assert counts.looked_up == 1


def test_a_sibling_sharing_a_prefix_is_not_taken_for_a_child(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "lib"
    a = copy_track(lib, "A")
    ax = copy_track(lib, "A-x")
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)

    counts = pipeline.run(settings, os.fsencode(lib / "A"), os.fsencode(lib / "A-x"))

    assert counts.written_lrc == 2
    assert a.with_suffix(".lrc").exists()
    assert ax.with_suffix(".lrc").exists()


def test_one_empty_start_among_others_costs_nothing(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "lib"
    a = copy_track(lib, "A")
    (lib / "E").mkdir()
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)

    counts = pipeline.run(settings, os.fsencode(lib / "A"), os.fsencode(lib / "E"))

    assert counts.written_lrc == 1
    assert a.with_suffix(".lrc").exists()


def test_every_start_empty_is_no_audio(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "lib"
    (lib / "E1").mkdir(parents=True)
    (lib / "E2").mkdir()
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)

    with pytest.raises(pipeline.NoAudioStop):
        pipeline.run(settings, os.fsencode(lib / "E1"), os.fsencode(lib / "E2"))


def test_no_start_is_a_programming_error(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "lib").mkdir()
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)

    with pytest.raises(ValueError):
        pipeline.run(settings)


def scoped_env(lib: Path, stub: StubLrclib) -> dict[str, str]:
    return {"PLYRIX_MUSIC_ROOT": str(lib), "PLYRIX_LRCLIB_URL": stub.base_url}


def test_run_path_limits_the_run_to_those_folders(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    a, b, c = (copy_track(lib, name) for name in ("A", "B", "C"))

    proc = run_cli(
        ["run", "--path", lib / "A", "--path", lib / "B"], env=scoped_env(lib, stub_lrclib)
    )

    assert proc.returncode == 0, proc.stderr
    assert a.with_suffix(".lrc").exists()
    assert b.with_suffix(".lrc").exists()
    assert sidecars_in(c.parent) == []
    assert nonzero(proc.stdout)["written_lrc"] == 2


def assert_refused_with(
    tmp_path: Path, proc: "subprocess.CompletedProcess[bytes]", stub: StubLrclib, needle: bytes
) -> None:
    assert proc.returncode == 2, proc.stderr
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert needle in lines[0]
    assert sidecars_in(tmp_path) == []
    assert not state_db_path(tmp_path).exists()
    assert stub.requests == []


def test_run_path_outside_the_root_is_refused(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, "A")
    copy_track(tmp_path / "elsewhere", "X")

    proc = run_cli(
        ["run", "--path", tmp_path / "elsewhere" / "X"], env=scoped_env(lib, stub_lrclib)
    )

    assert_refused_with(tmp_path, proc, stub_lrclib, b"music_root (PLYRIX_MUSIC_ROOT)")


def test_run_path_naming_a_file_is_refused(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")

    proc = run_cli(["run", "--path", track], env=scoped_env(lib, stub_lrclib))

    assert_refused_with(tmp_path, proc, stub_lrclib, b"is not a folder")


def test_run_path_with_a_positional_path_is_refused(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, "A")

    proc = run_cli(["run", lib, "--path", lib / "A"], env=scoped_env(lib, stub_lrclib))

    assert_refused_with(tmp_path, proc, stub_lrclib, b"not both")


def test_an_empty_run_path_is_refused(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, "A")

    proc = run_cli(["run", "--path", ""], env=scoped_env(lib, stub_lrclib))

    assert_refused_with(tmp_path, proc, stub_lrclib, b"music_root (PLYRIX_MUSIC_ROOT)")


def test_run_path_needs_a_configured_music_root(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, "A")

    proc = run_cli(["run", "--path", lib / "A"], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert_refused_with(tmp_path, proc, stub_lrclib, b"music_root (PLYRIX_MUSIC_ROOT)")


def test_the_hook_runs_every_disc_of_an_album_in_one_run(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    cd1 = copy_track(lib, "A/CD1")
    cd2 = copy_track(lib, "A/CD2")
    env = {
        **scoped_env(lib, stub_lrclib),
        "Lidarr_EventType": "AlbumDownload",
        "Lidarr_AddedTrackPaths": f"{cd1}|{cd2}",
        "Lidarr_Artist_Path": str(lib),
        "Lidarr_Album_Id": "12",
    }

    proc = run_cli(["lidarr-hook"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == b""
    assert cd1.with_suffix(".lrc").exists()
    assert cd2.with_suffix(".lrc").exists()
    with sqlite3.connect(state_db_path(tmp_path)) as conn:
        assert conn.execute("SELECT count(*) FROM runs").fetchone() == (1,)


def test_run_help_lists_path(run_cli: RunCli) -> None:
    proc = run_cli(["run", "--help"])

    assert proc.returncode == 0
    assert b"--path" in proc.stdout
