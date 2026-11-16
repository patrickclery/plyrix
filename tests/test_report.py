"""The JSON report and the exit codes, through the real console script. All text is invented."""

import dataclasses
import errno
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from conftest import (
    CANARY,
    RECORD,
    RunCli,
    StubLrclib,
    copy_track,
    leaks,
    non_notice_lines,
    nonzero,
    parse_counts,
    run_lib,
    snap,
    state_db_path,
)
from plyrix import __version__, cli, guard, pipeline, report

README = Path(__file__).parent.parent / "README.md"
ALL_COUNTS = {field.name for field in dataclasses.fields(pipeline.Counts)}
REPORT_KEYS = {
    "report_version",
    "plyrix_version",
    "dry_run",
    "dump_id",
    "started_at",
    "finished_at",
    "end_reason",
    "counts",
}
SERVER_ERROR = (500, {"message": "invented"})


def read_report(path: Path) -> dict[str, object]:
    found: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    assert set(found) == REPORT_KEYS
    return found


def old_temp_of(path: Path) -> Path:
    """Return the fixed temp name a report used to have, which anything could be waiting at."""
    return path.with_name(path.name + ".tmp")


def temps_of(path: Path) -> list[Path]:
    return list(path.parent.glob(f".{path.name}.*.tmp"))


def tree_of(top: Path) -> dict[Path, tuple[str, int, int, int, int, int, int]]:
    return {path: snap(path) for path in [top, *top.rglob("*")]}


def runs(tmp_path: Path) -> list[tuple[str | None, str | None]]:
    with sqlite3.connect(state_db_path(tmp_path)) as conn:
        rows = conn.execute("SELECT end_reason, counts_json FROM runs ORDER BY id").fetchall()
    conn.close()
    return rows


def test_report_holds_counts_and_no_lyrics(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    path = tmp_path / "report.json"

    proc = run_cli(
        ["run", lib, "--report", path, "--dump-id", "dump-a"],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url},
    )

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").read_bytes().count(CANARY.encode()) == 4
    assert path.is_file()
    found = read_report(path)
    assert found["report_version"] == 1
    assert found["plyrix_version"] == __version__
    assert found["dry_run"] is False
    assert found["dump_id"] == "dump-a"
    assert found["end_reason"] == "finished"
    assert found["counts"] == parse_counts(proc.stdout)
    assert set(parse_counts(proc.stdout)) == ALL_COUNTS
    started = datetime.fromisoformat(str(found["started_at"]))
    assert started <= datetime.fromisoformat(str(found["finished_at"]))
    assert started.tzinfo is not None

    text = path.read_text(encoding="utf-8")
    for held_by_the_library in (
        CANARY,
        str(lib),
        str(tmp_path),
        "t.flac",
        str(RECORD["trackName"]),
        str(RECORD["artistName"]),
        str(RECORD["albumName"]),
    ):
        assert held_by_the_library not in text
    assert leaks(tmp_path, proc, lib) == []


@pytest.mark.parametrize("source", ["variable", "file"])
def test_report_comes_from_each_source(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, source: str
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    path = tmp_path / "report.json"
    env = {"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    if source == "variable":
        env["PLYRIX_REPORT_FILE"] = str(path)
    else:
        toml = tmp_path / "config" / "plyrix" / "config.toml"
        toml.parent.mkdir(parents=True)
        toml.write_text(f'report_file = "{path}"\n', encoding="utf-8")

    proc = run_cli(["run", lib], env=env)

    assert proc.returncode == 0, proc.stderr
    assert read_report(path)["end_reason"] == "finished"


def test_no_report_unless_asked_for(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert list(tmp_path.glob("*.json")) == []


def test_report_on_a_stopped_run_at_preflight(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    path = tmp_path / "report.json"
    stub_lrclib.preflight_get = SERVER_ERROR

    proc = run_cli(["run", lib, "--report", path], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 1, proc.stderr
    assert proc.stdout == b""
    assert len(non_notice_lines(proc.stderr)) == 1
    assert not state_db_path(tmp_path).exists()
    found = read_report(path)
    assert found["end_reason"] == "lrclib_unavailable"
    assert found["counts"] == dict.fromkeys(ALL_COUNTS, 0)


def test_report_on_a_stopped_run_after_failed_lookups(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    for index in range(7):
        copy_track(lib, name=f"{index}.flac")
    path = tmp_path / "report.json"
    stub_lrclib.status, stub_lrclib.body = SERVER_ERROR

    proc = run_cli(["run", lib, "--report", path], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 1, proc.stderr
    assert nonzero(proc.stdout) == {"unavailable": 5}
    found = read_report(path)
    assert found["end_reason"] == "lrclib_unavailable"
    assert found["counts"] == parse_counts(proc.stdout)
    [(end_reason, counts_json)] = runs(tmp_path)
    assert end_reason == "lrclib_unavailable"
    assert json.loads(str(counts_json)) == parse_counts(proc.stdout)


def test_report_on_a_dry_run(tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    path = tmp_path / "report.json"
    before = tree_of(lib)

    proc = run_cli(
        ["run", lib, "--dry-run", "--report", path],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url},
    )

    assert proc.returncode == 0, proc.stderr
    found = read_report(path)
    assert found["dry_run"] is True
    assert found["counts"] == parse_counts(proc.stdout)
    assert parse_counts(proc.stdout)["written_lrc"] == 1
    assert tree_of(lib) == before
    assert not state_db_path(tmp_path).parent.exists()


@pytest.mark.parametrize("source", ["flag", "variable", "file", "folder_link"])
def test_report_inside_the_library_is_refused(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, source: str
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    inside = lib / "report.json"
    args: list[str | Path] = ["run", lib]
    env = {"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    if source == "flag":
        args += ["--report", inside]
    elif source == "variable":
        env["PLYRIX_REPORT_FILE"] = str(inside)
    elif source == "file":
        toml = tmp_path / "config" / "plyrix" / "config.toml"
        toml.parent.mkdir(parents=True)
        toml.write_text(f'report_file = "{inside}"\n', encoding="utf-8")
    else:
        (tmp_path / "looks_outside").symlink_to(lib)
        args += ["--report", tmp_path / "looks_outside" / "report.json"]
    before = tree_of(lib)

    proc = run_cli(args, env=env)

    assert proc.returncode == 2, proc.stderr
    assert proc.stdout == b""
    assert proc.stderr.splitlines() == [
        b"plyrix: report_file (PLYRIX_REPORT_FILE) must be outside the library"
    ]
    assert tree_of(lib) == before
    assert stub_lrclib.requests == []
    assert stub_lrclib.probes == []


@pytest.mark.parametrize("source", ["variable", "file"])
def test_report_a_path_from_a_variable_must_be_absolute(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, source: str
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    env = {"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    if source == "variable":
        env["PLYRIX_REPORT_FILE"] = "report.json"
    else:
        toml = tmp_path / "config" / "plyrix" / "config.toml"
        toml.parent.mkdir(parents=True)
        toml.write_text('report_file = "report.json"\n', encoding="utf-8")

    proc = run_cli(["run", lib], env=env, cwd=tmp_path)

    assert proc.returncode == 2, proc.stderr
    assert proc.stderr.splitlines() == [
        b"plyrix: report_file (PLYRIX_REPORT_FILE) must be an absolute path"
    ]
    assert not (tmp_path / "report.json").exists()


def test_report_a_relative_flag_means_the_working_directory(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    proc = run_cli(
        ["run", lib, "--report", "report.json"],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url},
        cwd=elsewhere,
    )

    assert proc.returncode == 0, proc.stderr
    assert read_report(elsewhere / "report.json")["end_reason"] == "finished"


def test_report_a_home_folder_is_expanded(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = run_cli(
        ["run", lib],
        env={
            "PLYRIX_LRCLIB_URL": stub_lrclib.base_url,
            "PLYRIX_REPORT_FILE": "~/report.json",
            "HOME": str(tmp_path),
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert read_report(tmp_path / "report.json")["end_reason"] == "finished"


def test_report_an_empty_flag_is_refused(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = run_cli(["run", lib, "--report", ""], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 2, proc.stderr
    assert proc.stderr.splitlines() == [
        b"plyrix: invalid settings: report_file (PLYRIX_REPORT_FILE)"
    ]


def test_report_cant_be_the_state_database(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    ledger = tmp_path / "state.db"

    proc = run_cli(
        ["run", lib, "--report", ledger, "--state-db", ledger],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url},
    )

    assert proc.returncode == 2, proc.stderr
    assert proc.stderr.splitlines() == [
        b"plyrix: report_file (PLYRIX_REPORT_FILE) can't be the state database"
    ]
    assert not ledger.exists()


def test_report_leaves_a_ledger_at_its_old_temp_name_alone(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    path = tmp_path / "report.json"
    ledger = old_temp_of(path)

    proc = run_cli(
        ["run", lib, "--report", path, "--state-db", ledger],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url},
    )

    assert proc.returncode == 0, proc.stderr
    assert read_report(path)["end_reason"] == "finished"
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("SELECT count(*) FROM runs").fetchone() == (1,)
    conn.close()


def test_report_that_cant_be_written(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)

    proc = run_cli(
        ["run", lib, "--report", tmp_path / "missing" / "report.json"],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url},
    )

    assert proc.returncode == 3, proc.stderr
    assert parse_counts(proc.stdout)["written_lrc"] == 1
    assert track.with_suffix(".lrc").exists()
    lines = non_notice_lines(proc.stderr)
    assert lines == [b"plyrix: report_file (PLYRIX_REPORT_FILE) could not be written"]
    assert not (tmp_path / "missing").exists()


def test_report_at_a_folder_leaves_no_temp(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    folder = tmp_path / "taken"
    folder.mkdir()

    proc = run_cli(
        ["run", lib, "--report", folder], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    )

    assert proc.returncode == 3, proc.stderr
    assert non_notice_lines(proc.stderr) == [
        b"plyrix: report_file (PLYRIX_REPORT_FILE) could not be written"
    ]
    assert temps_of(folder) == []
    assert list(folder.iterdir()) == []


def test_report_that_cant_be_written_keeps_a_stopped_runs_exit_code(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.preflight_get = SERVER_ERROR

    proc = run_cli(
        ["run", lib, "--report", tmp_path / "missing" / "report.json"],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url},
    )

    assert proc.returncode == 1, proc.stderr
    lines = non_notice_lines(proc.stderr)
    assert len(lines) == 2
    assert b"report_file" in lines[0]
    assert b"LRCLIB" in lines[1]


def test_report_a_link_at_the_old_temp_name_into_the_library_is_not_followed(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    audio_before = snap(track)
    path = tmp_path / "report.json"
    old_temp_of(path).symlink_to(track)

    proc = run_cli(["run", lib, "--report", path], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 0, proc.stderr
    assert snap(track) == audio_before
    assert old_temp_of(path).is_symlink()
    assert read_report(path)["end_reason"] == "finished"


@pytest.mark.parametrize("dry_run", [False, True], ids=["run", "dry_run"])
def test_report_a_link_at_the_old_temp_name_out_of_the_library_is_not_followed(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, dry_run: bool
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    path = tmp_path / "report.json"
    outside = tmp_path / "someone_elses.conf"
    outside.write_text("not plyrix's\n", encoding="utf-8")
    old_temp_of(path).symlink_to(outside)
    args: list[str | Path] = ["run", lib, "--report", path]

    proc = run_cli(
        [*args, "--dry-run"] if dry_run else args, env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    )

    assert proc.returncode == 0, proc.stderr
    assert outside.read_text(encoding="utf-8") == "not plyrix's\n"
    assert old_temp_of(path).is_symlink()
    assert read_report(path)["end_reason"] == "finished"


def test_report_a_link_at_the_old_temp_name_to_a_users_txt_is_not_followed(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    users = track.with_name("notes.txt")
    users.write_text("the user's own text\n", encoding="utf-8")
    path = tmp_path / "report.json"
    old_temp_of(path).symlink_to(users)

    proc = run_cli(["run", lib, "--report", path], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 0, proc.stderr
    assert users.read_text(encoding="utf-8") == "the user's own text\n"
    assert read_report(path)["end_reason"] == "finished"


def test_report_another_programs_file_at_the_old_temp_name_stays(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    path = tmp_path / "report.json"
    theirs = old_temp_of(path)
    theirs.write_text("another program's data\n", encoding="utf-8")

    proc = run_cli(["run", lib, "--report", path], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 0, proc.stderr
    assert theirs.read_text(encoding="utf-8") == "another program's data\n"
    assert read_report(path)["end_reason"] == "finished"


@pytest.mark.parametrize("waiting", ["link", "file"])
def test_report_never_reuses_what_is_at_its_random_temp_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waiting: str
) -> None:
    path = tmp_path / "report.json"
    outside = tmp_path / "someone_elses.conf"
    outside.write_text("not plyrix's\n", encoding="utf-8")
    monkeypatch.setattr(secrets, "token_hex", lambda _size: "00000000")
    temp = tmp_path / ".report.json.00000000.tmp"
    if waiting == "link":
        temp.symlink_to(outside)
    else:
        temp.write_text("not plyrix's\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        report.write(path, {"report_version": 1})

    assert outside.read_text(encoding="utf-8") == "not plyrix's\n"
    assert temp.is_symlink() is (waiting == "link")
    assert temp.read_text(encoding="utf-8") == "not plyrix's\n"
    assert not path.exists()


def test_report_a_folder_that_reaches_the_library_trips_the_guard(tmp_path: Path) -> None:
    """Config refuses this at load, so only a folder swapped afterwards can reach the guard."""
    lib = tmp_path / "lib"
    track = copy_track(lib)
    audio_before = snap(track)
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from plyrix import guard, report\n"
        "guard.install([sys.argv[1]])\n"
        "try:\n"
        "    report.write(Path(sys.argv[1]) / 'report.json', {'report_version': 1})\n"
        "except guard.GuardViolation:\n"
        "    sys.exit(70)\n"
    )

    proc = subprocess.run(
        [sys.executable, "-c", code, str(lib)], capture_output=True, check=False, timeout=60
    )

    assert proc.returncode == 70, proc.stderr
    assert snap(track) == audio_before
    assert not (lib / "report.json").exists()
    assert temps_of(lib / "report.json") == []


def test_report_replaces_atomically(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    path = tmp_path / "report.json"
    path.write_text("an older report\n", encoding="utf-8")
    older = path.stat().st_ino

    proc = run_cli(["run", lib, "--report", path], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 0, proc.stderr
    assert read_report(path)["end_reason"] == "finished"
    # A new file took the name, rather than the old one being rewritten in place.
    assert path.stat().st_ino != older
    assert temps_of(path) == []


def test_report_a_failed_replace_leaves_the_older_report_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "report.json"
    path.write_text("an older report\n", encoding="utf-8")

    def fail(_self: Path, _target: Path) -> Path:
        raise OSError("failed")

    monkeypatch.setattr(Path, "replace", fail)

    with pytest.raises(OSError, match="failed"):
        report.write(path, {"report_version": 1})

    assert path.read_text(encoding="utf-8") == "an older report\n"
    assert temps_of(path) == []


def test_report_a_rename_refused_by_a_bind_mount_leaves_the_folder_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "report.json"
    path.write_text("an older report\n", encoding="utf-8")

    def busy(_self: Path, _target: Path) -> Path:
        raise OSError(errno.EBUSY, "busy")

    monkeypatch.setattr(Path, "replace", busy)

    with pytest.raises(OSError, match="busy"):
        report.write(path, {"report_version": 1})

    assert list(tmp_path.iterdir()) == [path]


def test_report_a_failed_write_leaves_no_part_of_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "report.json"

    def full_disk(*_args: object, **_kwargs: object) -> str:
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(json, "dumps", full_disk)

    with pytest.raises(OSError, match="no space"):
        report.write(path, {"report_version": 1})

    assert list(tmp_path.iterdir()) == []


def test_report_cleanup_that_fails_keeps_the_first_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "report.json"

    def tripped(_self: Path, _target: Path) -> Path:
        raise guard.GuardViolation("invented")

    def cant_unlink(_self: Path, missing_ok: bool = False) -> None:
        raise OSError(errno.EIO, "no")

    monkeypatch.setattr(Path, "replace", tripped)
    monkeypatch.setattr(Path, "unlink", cant_unlink)

    with pytest.raises(guard.GuardViolation):
        report.write(path, {"report_version": 1})


def test_a_finished_run_stores_its_counts(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    [(end_reason, counts_json)] = runs(tmp_path)
    assert end_reason == "finished"
    assert json.loads(str(counts_json)) == parse_counts(proc.stdout)
    assert CANARY not in str(counts_json)


# Exit codes: each comes from a real run. 70 and 75 have theirs in test_guard.py and test_db.py.


def clean_run(_lib: Path, _stub: StubLrclib, _env: dict[str, str]) -> None:
    """Change nothing, as the library and the server are already well."""


def lrclib_down(_lib: Path, stub: StubLrclib, _env: dict[str, str]) -> None:
    stub.preflight_get = SERVER_ERROR


def bad_setting(_lib: Path, _stub: StubLrclib, env: dict[str, str]) -> None:
    env["PLYRIX_SIDECAR_METHOD"] = "invented"


def empty_library(lib: Path, _stub: StubLrclib, _env: dict[str, str]) -> None:
    for track in lib.rglob("*.flac"):
        track.unlink()


def unreadable_track(lib: Path, _stub: StubLrclib, _env: dict[str, str]) -> None:
    for track in lib.rglob("*.flac"):
        track.chmod(0)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
@pytest.mark.parametrize(
    ("arrange", "code"),
    [
        (clean_run, 0),
        (lrclib_down, 1),
        (bad_setting, 2),
        (empty_library, 2),
        (unreadable_track, 3),
    ],
    ids=["clean", "lrclib_down", "bad_setting", "empty_library", "read_failed"],
)
def test_exit_code_each_from_a_real_run(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    arrange: Callable[[Path, StubLrclib, dict[str, str]], None],
    code: int,
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    env = {"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    arrange(lib, stub_lrclib, env)

    try:
        proc = run_cli(["run", lib], env=env)
    finally:
        if track.exists():
            track.chmod(0o644)

    assert proc.returncode == code, proc.stderr
    assert b"Traceback" not in proc.stderr
    assert CANARY.encode() not in proc.stdout + proc.stderr


CROSSES_A_DEVICE = """
import errno, os, sys
from plyrix import cli


def crosses_a_device(*_args, **_kwargs):
    raise OSError(errno.EXDEV, "cross-device")


os.link = crosses_a_device
cli.app(["run", sys.argv[1]])
"""


def test_exit_code_a_filesystem_that_cant_publish_is_a_failed_run(
    tmp_path: Path, stub_lrclib: StubLrclib
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("PLYRIX_")}

    proc = subprocess.run(
        [sys.executable, "-c", CROSSES_A_DEVICE, str(lib)],
        env={
            **inherited,
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "PLYRIX_LRCLIB_URL": stub_lrclib.base_url,
        },
        capture_output=True,
        check=False,
        timeout=120,
    )

    assert proc.returncode == 3, proc.stderr
    assert nonzero(proc.stdout) == {"fs_no_atomic_publish": 1, "looked_up": 1}
    assert not track.with_suffix(".lrc").exists()
    assert b"the next run will try them again" in proc.stderr


def test_exit_code_3_names_each_count_that_causes_it() -> None:
    readme = README.read_text(encoding="utf-8")
    row = next(line for line in readme.splitlines() if line.startswith("| 3 |"))

    for name in ("write_failed", "fs_no_atomic_publish", "unavailable", "read_failed"):
        assert f"`{name}`" in row


def test_exit_codes_match_the_readme() -> None:
    readme = README.read_text(encoding="utf-8")
    section = readme.split("### Exit codes", 1)[1].split("\n#", 1)[0]

    assert set(re.findall(r"^\| (\d+) \|", section, re.MULTILINE)) == {
        "0",
        "1",
        "2",
        "3",
        "70",
        "75",
    }
    assert (cli.EXIT_UNFINISHED, cli.EXIT_TRIPPED) == (3, 70)


def test_readme_documents_the_report_setting() -> None:
    readme = README.read_text(encoding="utf-8")

    assert "| `--report` | `PLYRIX_REPORT_FILE` | `report_file` | none |" in readme


def test_readme_names_every_count() -> None:
    readme = README.read_text(encoding="utf-8")
    section = readme.split("### Reading the counts", 1)[1].split("\n### ", 1)[0]

    missing = {name for name in ALL_COUNTS if f"`{name}`" not in section}
    assert missing == set()
    assert "no_match_doubtful_version" in section
