"""Runs, the Lidarr hook and drain use --dump-id, else the dump recorded live, else dump_id (D-07).

Each test records a dump with the real `plyrix lrclib-record`, then runs plyrix against a stub
LRCLIB and reads the ledger back. Every dump name and tag is invented.
"""

import json
import sqlite3
from pathlib import Path

from conftest import (
    RECORD,
    RunCli,
    StubLrclib,
    copy_track,
    lidarr_env,
    parse_counts,
    snap,
    state_db_path,
)
from plyrix import spool

NAME = "lrclib-db-dump-20261030T182259Z.sqlite3.gz"
NAME2 = "lrclib-db-dump-20261201T000000Z.sqlite3.gz"
OTHER = "lrclib-db-dump-20260901T000000Z.sqlite3.gz"
NOT_FOUND = (404, {"name": "TrackNotFound"})


def env_for(lib: Path, stub: StubLrclib, **extra: str) -> dict[str, str]:
    return {"PLYRIX_MUSIC_ROOT": str(lib), "PLYRIX_LRCLIB_URL": stub.base_url, **extra}


def record(run_cli: RunCli, env: dict[str, str], name: str) -> None:
    proc = run_cli(["lrclib-record", name, "--status", "live"], env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.decode().strip() == f"plyrix: {name} is recorded as LRCLIB's live dump"


def ledger(tmp_path: Path, sql: str) -> list[tuple[object, ...]]:
    con = sqlite3.connect(state_db_path(tmp_path))
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def run_dump_ids(tmp_path: Path) -> list[object]:
    return [row[0] for row in ledger(tmp_path, "SELECT dump_id FROM runs ORDER BY id")]


def test_a_run_uses_the_live_dump(tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, "A")
    copy_track(lib, "B")
    env = env_for(lib, stub_lrclib)
    record(run_cli, env, NAME)
    report = tmp_path / "report.json"

    proc = run_cli(["run", "--report", str(report)], env)

    assert proc.returncode == 0, proc.stderr
    assert run_dump_ids(tmp_path) == [NAME]
    assert ledger(tmp_path, "SELECT DISTINCT dump_id FROM tracks") == [(NAME,)]
    assert json.loads(report.read_text(encoding="utf-8"))["dump_id"] == NAME
    assert b"dump_id" not in proc.stderr


def test_the_live_dump_wins_over_dump_id(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    env = env_for(lib, stub_lrclib)
    record(run_cli, env, NAME)
    report = tmp_path / "report.json"

    proc = run_cli(["run", "--report", str(report)], {**env, "PLYRIX_DUMP_ID": OTHER})

    assert proc.returncode == 0, proc.stderr
    assert run_dump_ids(tmp_path) == [NAME]
    assert ledger(tmp_path, "SELECT DISTINCT dump_id FROM tracks") == [(NAME,)]
    assert json.loads(report.read_text(encoding="utf-8"))["dump_id"] == NAME
    assert proc.stderr.count(b"ignoring dump_id") == 1
    assert NAME.encode() in proc.stderr


def test_a_dump_id_in_the_config_file_is_ignored_with_a_warning(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    env = env_for(lib, stub_lrclib)
    record(run_cli, env, NAME)
    toml = tmp_path / "plyrix.toml"
    toml.write_text(f'dump_id = "{OTHER}"\n', encoding="utf-8")

    proc = run_cli(["run", "--config", str(toml)], env)

    assert proc.returncode == 0, proc.stderr
    assert run_dump_ids(tmp_path) == [NAME]
    assert proc.stderr.count(b"ignoring dump_id") == 1


def test_a_dump_id_naming_the_live_dump_gets_no_warning(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    env = env_for(lib, stub_lrclib)
    record(run_cli, env, NAME)

    proc = run_cli(["run"], {**env, "PLYRIX_DUMP_ID": NAME})

    assert proc.returncode == 0, proc.stderr
    assert run_dump_ids(tmp_path) == [NAME]
    assert b"dump_id" not in proc.stderr


def test_the_dump_id_flag_overrides_the_live_dump(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    env = env_for(lib, stub_lrclib)
    record(run_cli, env, NAME)
    report = tmp_path / "report.json"

    proc = run_cli(
        ["run", "--dump-id", OTHER, "--report", str(report)], {**env, "PLYRIX_DUMP_ID": NAME2}
    )

    assert proc.returncode == 0, proc.stderr
    assert run_dump_ids(tmp_path) == [OTHER]
    assert ledger(tmp_path, "SELECT DISTINCT dump_id FROM tracks") == [(OTHER,)]
    assert json.loads(report.read_text(encoding="utf-8"))["dump_id"] == OTHER
    assert b"ignoring dump_id" not in proc.stderr
    assert proc.stderr.count(f"using --dump-id {OTHER} for this run".encode()) == 1
    # The override is for that run only: the record is unchanged, and the next run uses it.
    again = run_cli(["run"], env)
    assert again.returncode == 0, again.stderr
    assert run_dump_ids(tmp_path) == [OTHER, NAME]


def test_drain_warns_once_about_a_stale_dump_id(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    first = copy_track(lib / "one", "A")
    second = copy_track(lib / "two", "B")
    env = env_for(lib, stub_lrclib)
    record(run_cli, env, NAME)
    folder = state_db_path(tmp_path).parent / "lidarr-spool"
    spool.put(folder, lidarr_env(added=[first], artist=lib / "one"))
    spool.put(folder, lidarr_env(added=[second], artist=lib / "two"))

    proc = run_cli(["drain"], {**env, "PLYRIX_DUMP_ID": OTHER})

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["drained"] == 2
    assert run_dump_ids(tmp_path) == [NAME, NAME]
    assert proc.stderr.count(b"ignoring dump_id") == 1


def test_dump_id_stands_in_until_a_dump_is_recorded(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    env = env_for(lib, stub_lrclib)

    proc = run_cli(["run"], {**env, "PLYRIX_DUMP_ID": OTHER})

    assert proc.returncode == 0, proc.stderr
    assert run_dump_ids(tmp_path) == [OTHER]


def test_a_stale_dump_id_does_not_pin_runs_after_an_update(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    # Setup's advice was followed, so dump_id names the first dump. An update then records a
    # newer dump live, and the next run must look the miss up again under that one.
    lib = tmp_path / "lib"
    copy_track(lib, "A")
    env = {**env_for(lib, stub_lrclib), "PLYRIX_DUMP_ID": NAME}
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    first = run_cli(["run"], env)
    assert first.returncode == 0, first.stderr
    one_miss = len(stub_lrclib.requests)
    assert one_miss > 0
    record(run_cli, env, NAME2)
    stub_lrclib.requests.clear()

    again = run_cli(["run"], env)

    assert again.returncode == 0, again.stderr
    assert len(stub_lrclib.requests) == one_miss
    assert run_dump_ids(tmp_path) == [NAME, NAME2]
    assert ledger(tmp_path, "SELECT outcome, dump_id FROM tracks") == [("no_match", NAME2)]


def test_a_new_live_dump_retries_a_miss_and_not_a_synced_track(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, "A")
    env = env_for(lib, stub_lrclib)
    record(run_cli, env, NAME)
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    first = run_cli(["run"], env)
    assert first.returncode == 0, first.stderr
    assert parse_counts(first.stdout)["no_match"] == 1
    one_miss = len(stub_lrclib.requests)
    assert one_miss > 0
    # A second track, found and synced under the same dump. The miss isn't asked again.
    copy_track(lib, "B")
    stub_lrclib.status, stub_lrclib.body = 200, RECORD
    stub_lrclib.requests.clear()
    second = run_cli(["run"], env)
    assert second.returncode == 0, second.stderr
    assert parse_counts(second.stdout)["synced"] == 1
    record(run_cli, env, NAME2)
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    stub_lrclib.requests.clear()

    third = run_cli(["run"], env)

    assert third.returncode == 0, third.stderr
    # Exactly one track's lookup: the miss, under the new dump. The synced track is settled.
    assert len(stub_lrclib.requests) == one_miss
    assert run_dump_ids(tmp_path)[-1] == NAME2
    rows = ledger(tmp_path, "SELECT outcome, dump_id FROM tracks ORDER BY path")
    assert rows == [("no_match", NAME2), ("synced", NAME)]


def test_a_dry_run_reads_the_live_dump_and_changes_no_file(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    env = env_for(lib, stub_lrclib)
    record(run_cli, env, NAME)
    folder = state_db_path(tmp_path).parent
    before = {path.name: snap(path) for path in folder.iterdir()}
    report = tmp_path / "report.json"

    proc = run_cli(["run", "--dry-run", "--report", str(report)], env)

    assert proc.returncode == 0, proc.stderr
    assert {path.name: snap(path) for path in folder.iterdir()} == before
    assert json.loads(report.read_text(encoding="utf-8"))["dump_id"] == NAME


def test_the_lidarr_hook_records_the_live_dump(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    env = env_for(lib, stub_lrclib)
    record(run_cli, env, NAME)

    proc = run_cli(["lidarr-hook"], {**env, **lidarr_env(added=[track], artist=lib)})

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").exists()
    assert run_dump_ids(tmp_path) == [NAME]


def test_drain_records_the_live_dump(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib, "A")
    env = env_for(lib, stub_lrclib)
    record(run_cli, env, NAME)
    spool.put(
        state_db_path(tmp_path).parent / "lidarr-spool", lidarr_env(added=[track], artist=lib)
    )

    proc = run_cli(["drain"], env)

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["drained"] == 1
    assert run_dump_ids(tmp_path) == [NAME]


def test_with_no_record_and_no_dump_id_a_run_is_as_before(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    env = env_for(lib, stub_lrclib)
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    first = run_cli(["run"], env)
    assert first.returncode == 0, first.stderr

    again = run_cli(["run"], env)

    assert again.returncode == 0, again.stderr
    assert run_dump_ids(tmp_path) == ["", ""]
    assert b"no dump_id" in again.stderr
    assert ledger(tmp_path, "SELECT count(*) FROM lrclib_dumps") == [(0,)]
