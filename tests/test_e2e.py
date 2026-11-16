"""End-to-end proofs through the real console script, in a subprocess, against a stub server."""

import errno
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    AUDIO_SUFFIXES,
    CANARY,
    FIXTURES,
    PROBE_TEXT,
    RECORD,
    RunCli,
    StubLrclib,
    audited_run,
    copy_track,
    leaks,
    ledger_rows,
    non_notice_lines,
    nonzero,
    parse_counts,
    per_format_lib,
    plant_deep_track,
    run_lib,
    settings_for,
    sidecars_in,
    snap,
    state_db_path,
    tags_with,
)
from plyrix import pipeline, sidecars, tags, walker
from plyrix.domain import MATCHER_VERSION
from plyrix.sidecars import encode

PATH_MAX = os.pathconf("/", "PC_PATH_MAX")


def published_paths(tmp_path: Path) -> list[bytes]:
    return [row[3] for row in ledger_rows(tmp_path) if row[0] == "published"]


def query(tmp_path: Path, sql: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(state_db_path(tmp_path))
    try:
        rows = conn.execute(sql).fetchall()
        conn.commit()
    finally:
        conn.close()
    return rows


def test_tracer_run_writes_lrc(tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    before = snap(track)

    proc = run_cli(["run", lib], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 0, proc.stderr
    sidecar = track.with_suffix(".lrc")
    expected = encode(str(RECORD["syncedLyrics"]))
    assert sidecar.read_bytes() == expected
    assert sorted(path.name for path in track.parent.iterdir()) == ["t.flac", "t.lrc"]
    assert parse_counts(proc.stdout)["written_lrc"] == 1
    assert b"pre-release" in proc.stderr
    assert b"copied" in proc.stderr
    assert ledger_rows(tmp_path) == [
        ("published", hashlib.sha256(expected).hexdigest(), len(expected), os.fsencode(sidecar))
    ]
    assert len(stub_lrclib.requests) == 1
    request = stub_lrclib.requests[0]
    assert request["path"] == "/api/get"
    assert request["artist_name"] == "Fixture Artist"
    assert request["track_name"] == "Fixture Title"
    assert request["album_name"] == "Fixture Album"
    assert request["duration"] == "3"
    assert snap(track) == before
    assert leaks(tmp_path, proc, lib) == []


def run_dump(run_cli: RunCli, stub: StubLrclib, lib: Path, dump_id: str):  # type: ignore[no-untyped-def]
    return run_cli(["run", lib, "--dump-id", dump_id], env={"PLYRIX_LRCLIB_URL": stub.base_url})


NOT_FOUND: tuple[int, object] = (404, {"name": "TrackNotFound"})
INSTRUMENTAL = {**RECORD, "instrumental": True, "syncedLyrics": None, "plainLyrics": None}


def test_tracer_a_miss_remembers_its_dump(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND

    first = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert first.returncode == 0, first.stderr
    assert nonzero(first.stdout) == {"no_match": 1, "looked_up": 1, "no_match_not_found": 1}
    assert len(stub_lrclib.requests) == 3
    assert query(tmp_path, "SELECT path, outcome, reason, dump_id FROM tracks") == [
        (os.fsencode(track), "no_match", "not_found", "dump-a")
    ]
    assert query(tmp_path, "SELECT dump_id FROM runs") == [("dump-a",)]
    before = snap(track)

    second = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert second.returncode == 0, second.stderr
    assert nonzero(second.stdout) == {
        "no_match": 1,
        "no_match_not_found": 1,
        "skipped_unchanged": 1,
    }
    assert len(stub_lrclib.requests) == 3
    assert snap(track) == before

    third = run_dump(run_cli, stub_lrclib, lib, "dump-b")

    assert third.returncode == 0, third.stderr
    assert nonzero(third.stdout) == {"no_match": 1, "looked_up": 1, "no_match_not_found": 1}
    assert len(stub_lrclib.requests) == 6
    assert query(tmp_path, "SELECT dump_id FROM tracks") == [("dump-b",)]
    assert query(tmp_path, "SELECT dump_id FROM runs ORDER BY id") == [
        ("dump-a",),
        ("dump-a",),
        ("dump-b",),
    ]


def test_tracer_a_synced_result_records_its_dump(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)

    proc = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").exists()
    assert query(tmp_path, "SELECT outcome, reason, dump_id, lrclib_id FROM tracks") == [
        ("synced", "", "dump-a", 7)
    ]
    assert query(tmp_path, "SELECT dump_id, origin, refresh_pending FROM sidecars") == [
        ("dump-a", "written", 1)
    ]
    assert leaks(tmp_path, proc, lib) == []


def test_tracer_no_dump_id_warns_once_and_retries(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, name="a.flac")
    copy_track(lib, name="b.flac")
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND

    first = run_lib(run_cli, stub_lrclib, lib)

    assert first.returncode == 0, first.stderr
    assert non_notice_lines(first.stderr) == []
    assert len(stub_lrclib.requests) == 6

    second = run_lib(run_cli, stub_lrclib, lib)

    assert second.returncode == 0, second.stderr
    assert nonzero(second.stdout) == {"no_match": 2, "looked_up": 2, "no_match_not_found": 2}
    assert len(stub_lrclib.requests) == 12
    lines = non_notice_lines(second.stderr)
    assert len(lines) == 1
    assert b"PLYRIX_DUMP_ID" in lines[0]


def test_tracer_a_failed_lookup_records_no_result(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.status, stub_lrclib.body = 503, {}

    proc = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert proc.returncode == 3, proc.stderr
    assert query(tmp_path, "SELECT count(*) FROM tracks") == [(0,)]


def test_tracer_a_failed_retry_leaves_the_old_result_due(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    run_dump(run_cli, stub_lrclib, lib, "dump-a")
    stub_lrclib.status, stub_lrclib.body = 503, {}

    assert run_dump(run_cli, stub_lrclib, lib, "dump-b").returncode == 3
    assert query(tmp_path, "SELECT dump_id FROM tracks") == [("dump-a",)]

    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    assert nonzero(run_dump(run_cli, stub_lrclib, lib, "dump-b").stdout) == {
        "no_match": 1,
        "looked_up": 1,
        "no_match_not_found": 1,
    }
    assert query(tmp_path, "SELECT dump_id FROM tracks") == [("dump-b",)]


@pytest.mark.parametrize(
    ("body", "outcome", "reason", "counts"),
    [
        (INSTRUMENTAL, "instrumental", "", {"instrumental": 1}),
        ({**RECORD, "syncedLyrics": None}, "plain", "", {"plain": 1, "written_txt": 1}),
        (
            {**RECORD, "syncedLyrics": None, "plainLyrics": "  \n "},
            "no_match",
            "not_found",
            {"no_match": 1, "no_match_not_found": 1},
        ),
    ],
    ids=["instrumental", "plain", "blank"],
)
def test_tracer_a_record_with_no_synced_text_says_what_it_was(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    body: dict[str, object],
    outcome: str,
    reason: str,
    counts: dict[str, int],
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.body = body

    proc = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {**counts, "looked_up": 1}
    assert query(tmp_path, "SELECT outcome, reason, dump_id, lrclib_id FROM tracks") == [
        (outcome, reason, "dump-a", 7)
    ]
    assert leaks(tmp_path, proc, lib) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores folder permissions")
def test_tracer_a_refused_publish_records_no_result(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    # The folder takes no new file, so the publish fails after a lookup that answered.
    track.parent.chmod(0o555)
    try:
        proc = run_dump(run_cli, stub_lrclib, lib, "dump-a")
    finally:
        track.parent.chmod(0o755)

    assert proc.returncode == 3, proc.stderr
    assert nonzero(proc.stdout) == {"write_failed": 1, "looked_up": 1}
    assert query(tmp_path, "SELECT count(*) FROM tracks") == [(0,)]


COVERAGE_COUNTS = (
    "synced",
    "plain",
    "plain_skipped",
    "instrumental",
    "no_match",
    "blocked_foreign",
    "untagged",
)
PROBLEM_COUNTS = (
    "unavailable",
    "unreadable",
    "read_failed",
    "folder_unreadable",
    "fs_no_atomic_publish",
    "stem_collision",
    "symlink",
    "too_long",
    "write_failed",
)
ACTION_COUNTS = (
    "written_lrc",
    "written_txt",
    "upgraded",
    "adopted",
    "lrc_stale",
    "skipped_unchanged",
    "looked_up",
    "lrc_few_lines",
    "lrc_low_coverage",
)
REASON_COUNTS = (
    "no_match_not_found",
    "no_match_rejected",
    "no_match_doubtful_artist",
    "no_match_doubtful_title",
    "no_match_doubtful_duration",
    "no_match_doubtful_version",
    "no_match_timing_rejected",
)
ALL_COUNTS = (*COVERAGE_COUNTS, *PROBLEM_COUNTS, *ACTION_COUNTS, *REASON_COUNTS)
FOREIGN = b"not written by plyrix\n"


@pytest.mark.parametrize(
    ("status", "name", "field"),
    [
        (404, "TrackNotFound", "no_match_not_found"),
        (400, "ValidationError", "no_match_rejected"),
    ],
)
def test_an_answer_with_no_record_writes_nothing(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, status: int, name: str, field: str
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    before = snap(track)
    stub_lrclib.status = status
    stub_lrclib.body = {"name": name, "message": CANARY}

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"no_match": 1, field: 1, "looked_up": 1}
    assert ledger_rows(tmp_path) == []
    assert sidecars_in(lib) == []
    assert snap(track) == before
    assert leaks(tmp_path, proc, lib) == []


@pytest.mark.parametrize(
    "synced",
    [
        None,
        "",
        "   \n  ",
        "placeholder line 1\nplaceholder line 2\n",
        "[ar:Fixture Artist]\n",
        7,
        ["[00:01.00] placeholder line 1"],
    ],
)
def test_unusable_synced_lyrics_write_no_lrc(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, synced: object
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    before = snap(track)
    stub_lrclib.body = {**RECORD, "syncedLyrics": synced}

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"plain": 1, "written_txt": 1, "looked_up": 1}
    assert published_paths(tmp_path) == [os.fsencode(track.with_suffix(".txt"))]
    assert sidecars_in(lib) == [track.with_suffix(".txt")]
    assert snap(track) == before
    assert leaks(tmp_path, proc, lib) == []


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (500, {"message": CANARY}),
        (503, {"message": CANARY}),
        (200, f"not json {CANARY}".encode()),
        (200, [RECORD]),
        (302, {"message": CANARY}),
    ],
)
def test_an_outage_stops_the_run_and_records_nothing(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, status: int, body: object
) -> None:
    lib = tmp_path / "lib"
    tracks = [copy_track(lib, name=f"{index}.flac") for index in range(7)]
    before = [snap(track) for track in tracks]
    stub_lrclib.status = status
    stub_lrclib.body = body

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 1, proc.stderr
    assert nonzero(proc.stdout) == {"unavailable": 5}
    # A 503 is asked again three times, as that is how LRCLIB says it is overloaded.
    assert len(stub_lrclib.requests) == (20 if status == 503 else 5)
    lines = non_notice_lines(proc.stderr)
    assert len(lines) == 1
    assert b"LRCLIB" in lines[0]
    assert b"Traceback" not in proc.stderr
    assert ledger_rows(tmp_path) == []
    assert sidecars_in(lib) == []
    assert [snap(track) for track in tracks] == before
    assert leaks(tmp_path, proc, lib) == []


def test_only_failures_in_a_row_stop_the_run(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    for index in range(9):
        copy_track(lib, name=f"{index}.flac")
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    failures: list[tuple[int, object]] = [(500, {})] * 4
    stub_lrclib.search_queue = [*failures, (200, [RECORD]), *failures]

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 3, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "unavailable": 8, "written_lrc": 1, "looked_up": 1}
    assert len(stub_lrclib.requests) == 9 * 3
    assert len(ledger_rows(tmp_path)) == 1


def test_a_url_missing_its_api_path_is_not_a_clean_run_of_misses(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    for name in ("1.flac", "2.flac", "3.flac", "4.flac", "5.flac", "6.flac"):
        copy_track(lib, name=name)

    proc = run_cli(
        ["run", lib], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url.removesuffix("/api")}
    )

    assert_preflight_stop(proc, tmp_path, b"not answering")
    assert stub_lrclib.requests == []


WATCHED_RUN = """
import json, sys

from plyrix import cli

connects = []


def record_connects(event, args):
    if event == "socket.connect":
        connects.append(args[1])


log, *argv = sys.argv[1:]
sys.addaudithook(record_connects)
try:
    cli.app(argv)
finally:
    with open(log, "w", encoding="utf-8") as file:
        json.dump(connects, file)
"""


def test_no_other_host_is_contacted(tmp_path: Path, stub_lrclib: StubLrclib) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    log = tmp_path / "connects.json"
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("PLYRIX_")}

    proc = subprocess.run(
        [sys.executable, "-c", WATCHED_RUN, log, "run", lib],
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

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"no_match": 1, "looked_up": 1, "no_match_not_found": 1}
    connects = json.loads(log.read_text(encoding="utf-8"))
    # The audit event has to be seen, or an empty list would prove nothing.
    assert connects
    host, port = stub_lrclib.base_url.removeprefix("http://").removesuffix("/api").split(":")
    assert {tuple(address) for address in connects} == {(host, int(port))}


UNKNOWN_ERROR = {"message": "Something bad happened", "name": "UnknownError", "statusCode": 500}


def assert_preflight_stop(
    proc: "subprocess.CompletedProcess[bytes]", tmp_path: Path, says: bytes
) -> None:
    """Assert exit 1 with one line and no counts, and that the run opened no ledger."""
    assert proc.returncode == 1, proc.stderr
    assert proc.stdout == b""
    lines = non_notice_lines(proc.stderr)
    assert len(lines) == 1, proc.stderr
    assert lines[0].startswith(b"plyrix: ")
    assert says in lines[0]
    assert b"nothing was recorded" in lines[0]
    assert not state_db_path(tmp_path).exists()


@pytest.mark.parametrize(
    ("field", "answer", "says"),
    [
        ("preflight_get", (500, UNKNOWN_ERROR), b"not answering"),
        ("preflight_search", (500, UNKNOWN_ERROR), b"still rebuilding its search index"),
        ("preflight_id", (404, {"name": "TrackNotFound"}), b"holds no tracks"),
    ],
    ids=["get_down", "search_500", "no_tracks"],
)
def test_preflight_stops_with_nothing_recorded(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    field: str,
    answer: tuple[int, object],
    says: bytes,
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    before = snap(track)
    setattr(stub_lrclib, field, answer)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert_preflight_stop(proc, tmp_path, says)
    assert sidecars_in(lib) == []
    assert snap(track) == before
    assert stub_lrclib.requests == []


def test_preflight_leaves_an_existing_ledger_alone(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    assert run_lib(run_cli, stub_lrclib, lib).returncode == 0
    before = snap(state_db_path(tmp_path))
    stub_lrclib.preflight_search = (500, UNKNOWN_ERROR)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 1, proc.stderr
    assert snap(state_db_path(tmp_path)) == before


def test_preflight_probes_hold_no_library_metadata(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    # A probe's body holds lyrics on a real server, and it has to be thrown away.
    stub_lrclib.preflight_id = (200, RECORD)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    by_text = [probe for probe in stub_lrclib.probes if "track_name" in probe]
    assert [probe["path"] for probe in by_text] == ["/api/get", "/api/search"]
    for probe in by_text:
        assert (probe["track_name"], probe["artist_name"]) == (PROBE_TEXT, PROBE_TEXT)
    assert b"Fixture" not in json.dumps(stub_lrclib.probes).encode()
    assert leaks(tmp_path, proc, lib) == []
    # The stub keeps probes apart, so the track's own lookup is the only request.
    assert [request["track_name"] for request in stub_lrclib.requests] == ["Fixture Title"] * 3


def test_a_run_where_a_write_or_lookup_failed_says_so_on_one_line_after_the_counts(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.status = 503

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 3
    assert parse_counts(proc.stdout) == {**dict.fromkeys(ALL_COUNTS, 0), "unavailable": 1}
    assert proc.stderr.splitlines()[-1] == (
        b"plyrix: tracks where a write, a file read or an LRCLIB lookup failed, and folders that "
        b"could not be listed: 1; the next run will try them again"
    )


@pytest.mark.parametrize(
    ("body", "status", "counts"),
    [
        (RECORD, 200, {"synced": 1, "written_lrc": 1}),
        ({"name": "TrackNotFound"}, 404, {"no_match": 1, "no_match_not_found": 1}),
        ({"name": "ValidationError"}, 400, {"no_match": 1, "no_match_rejected": 1}),
        ({**RECORD, "syncedLyrics": None}, 200, {"plain": 1, "written_txt": 1}),
    ],
    ids=["synced", "not-found", "rejected", "plain"],
)
def test_a_track_with_an_answer_is_not_a_failed_run(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    body: dict[str, object],
    status: int,
    counts: dict[str, int],
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.status, stub_lrclib.body = status, body

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {**counts, "looked_up": 1}


def test_a_refused_connection_is_unavailable(tmp_path: Path, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]

    proc = run_cli(["run", lib], env={"PLYRIX_LRCLIB_URL": f"http://127.0.0.1:{closed_port}/api"})

    assert_preflight_stop(proc, tmp_path, b"not answering")


def test_lookups_never_go_through_a_proxy(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    dead_proxy = "http://127.0.0.1:9"

    proc = run_lib(
        run_cli,
        stub_lrclib,
        lib,
        HTTP_PROXY=dead_proxy,
        http_proxy=dead_proxy,
        ALL_PROXY=dead_proxy,
        all_proxy=dead_proxy,
        NO_PROXY="",
        no_proxy="",
    )

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert len(stub_lrclib.requests) == 1


def test_a_file_with_no_tags_counts_untagged_and_the_run_continues(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    good = copy_track(lib, name="z.flac")
    (good.parent / "blank.mp3").write_bytes(b"\0" * 4096)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "untagged": 1, "written_lrc": 1, "looked_up": 1}
    assert len(stub_lrclib.requests) == 1


def test_an_unreadable_file_counts_unreadable_and_the_run_continues(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    good = copy_track(lib, name="z.flac")
    broken = good.parent / "broken.flac"
    broken.write_bytes(b"this is not a flac stream")
    before = snap(broken)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "unreadable": 1, "written_lrc": 1, "looked_up": 1}
    assert b"Traceback" not in proc.stderr
    assert snap(broken) == before


def test_a_second_run_skips_an_owned_track_with_no_lookup(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    assert nonzero(run_lib(run_cli, stub_lrclib, lib).stdout) == {
        "synced": 1,
        "written_lrc": 1,
        "looked_up": 1,
    }
    sidecar = track.with_suffix(".lrc")
    audio_before, sidecar_before = snap(track), snap(sidecar)
    rows_before = ledger_rows(tmp_path)
    # An outage now would show at once if the second run asked LRCLIB anything.
    stub_lrclib.status = 503

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "skipped_unchanged": 1}
    assert len(stub_lrclib.requests) == 1
    assert snap(track) == audio_before
    assert snap(sidecar) == sidecar_before
    assert ledger_rows(tmp_path) == rows_before


def test_a_foreign_lrc_stays_byte_identical(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    sidecar = track.with_suffix(".lrc")
    sidecar.write_bytes(FOREIGN)
    before = snap(sidecar), snap(track)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"blocked_foreign": 1}
    assert (snap(sidecar), snap(track)) == before
    assert ledger_rows(tmp_path) == []
    assert stub_lrclib.requests == []
    assert len(sidecars_in(lib)) == 1


def test_a_foreign_txt_stays_byte_identical_with_the_lrc_beside_it(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    sidecar = track.with_suffix(".txt")
    sidecar.write_bytes(FOREIGN)
    before = snap(sidecar), snap(track)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert (snap(sidecar), snap(track)) == before
    assert published_paths(tmp_path) == [os.fsencode(track.with_suffix(".lrc"))]
    assert sorted(sidecars_in(lib)) == [track.with_suffix(".lrc"), sidecar]


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
def test_anything_else_at_the_sidecar_name_is_left_alone(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, plant: Callable[[Path], None]
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    sidecar = track.with_suffix(".lrc")
    plant(sidecar)
    audio_before = snap(track)
    entry_before = os.lstat(sidecar)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"blocked_foreign": 1}
    assert os.lstat(sidecar).st_ino == entry_before.st_ino
    assert os.lstat(sidecar).st_mtime_ns == entry_before.st_mtime_ns
    assert snap(track) == audio_before
    assert ledger_rows(tmp_path) == []
    assert stub_lrclib.requests == []


def test_an_edited_owned_sidecar_stays_byte_identical(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    run_lib(run_cli, stub_lrclib, lib)
    rows_before = ledger_rows(tmp_path)
    sidecar = track.with_suffix(".lrc")
    sidecar.write_bytes(FOREIGN)
    before = snap(sidecar)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"blocked_foreign": 1}
    assert snap(sidecar) == before
    assert len(stub_lrclib.requests) == 1
    assert ledger_rows(tmp_path) == rows_before


def test_a_symlink_to_identical_bytes_is_not_an_owned_sidecar(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    run_lib(run_cli, stub_lrclib, lib)
    sidecar = track.with_suffix(".lrc")
    sidecar.rename(track.with_name("moved.bak"))
    sidecar.symlink_to("moved.bak")

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"blocked_foreign": 1}
    assert sidecar.is_symlink()


def test_a_deleted_owned_sidecar_is_written_again(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    run_lib(run_cli, stub_lrclib, lib)
    rows_before = ledger_rows(tmp_path)
    track.with_suffix(".lrc").unlink()

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert track.with_suffix(".lrc").exists()
    assert ledger_rows(tmp_path) == rows_before


def test_ownership_belongs_to_one_library_root(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    track = copy_track(first)
    run_lib(run_cli, stub_lrclib, first)
    copied = copy_track(second).with_suffix(".lrc")
    shutil.copyfile(track.with_suffix(".lrc"), copied)
    before = snap(copied)

    proc = run_lib(run_cli, stub_lrclib, second)

    # The copy passes on its timing alone, so it is kept but never counts as plyrix's own.
    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "adopted": 1}
    assert snap(copied) == before
    assert query(tmp_path, "SELECT path, origin FROM sidecars ORDER BY path") == [
        (os.fsencode(track.with_suffix(".lrc")), "written"),
        (os.fsencode(copied), "timed"),
    ]


def test_ownership_of_an_untimed_file_belongs_to_one_library_root(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    stub_lrclib.body = {**RECORD, "syncedLyrics": None}
    track = copy_track(first)
    run_lib(run_cli, stub_lrclib, first)
    copied = copy_track(second).with_suffix(".lrc")
    shutil.copyfile(track.with_suffix(".txt"), copied)
    before = snap(copied)

    proc = run_lib(run_cli, stub_lrclib, second)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"blocked_foreign": 1}
    assert snap(copied) == before
    assert [row[3] for row in ledger_rows(tmp_path)] == [os.fsencode(track.with_suffix(".txt"))]


@pytest.mark.parametrize("first_album", [True, False], ids=["album-then-lib", "lib-then-album"])
def test_a_sidecar_stays_plyrix_whichever_folder_each_run_names(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, first_album: bool
) -> None:
    lib = tmp_path / "lib"
    sidecar = copy_track(lib).with_suffix(".lrc")
    order = (lib / "Album", lib) if first_album else (lib, lib / "Album")

    first = run_lib(run_cli, stub_lrclib, order[0])
    assert nonzero(first.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    before = snap(sidecar)
    second = run_lib(run_cli, stub_lrclib, order[1])

    assert second.returncode == 0, second.stderr
    assert nonzero(second.stdout) == {"synced": 1, "skipped_unchanged": 1}
    assert snap(sidecar) == before
    assert len(ledger_rows(tmp_path)) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores folder permissions")
def test_a_folder_that_refuses_writes_is_counted_and_the_run_continues(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    locked = copy_track(lib, album="A")
    good = copy_track(lib, album="B")
    locked.parent.chmod(0o555)
    try:
        proc = run_lib(run_cli, stub_lrclib, lib)
        assert sorted(path.name for path in locked.parent.iterdir()) == ["t.flac"]
    finally:
        locked.parent.chmod(0o755)

    assert proc.returncode == 3, proc.stderr
    assert nonzero(proc.stdout) == {
        "synced": 1,
        "write_failed": 1,
        "written_lrc": 1,
        "looked_up": 2,
    }
    assert b"Traceback" not in proc.stderr
    assert good.with_suffix(".lrc").exists()
    assert published_paths(tmp_path) == [os.fsencode(lib / "B" / "t.lrc")]

    # The row for the failed write stays, so the next run settles it and tries that track again.
    retried = run_lib(run_cli, stub_lrclib, lib)
    assert nonzero(retried.stdout) == {
        "synced": 2,
        "written_lrc": 1,
        "skipped_unchanged": 1,
        "looked_up": 1,
    }
    assert locked.with_suffix(".lrc").exists()
    assert len(ledger_rows(tmp_path)) == 2


@pytest.mark.parametrize(
    ("value", "stored"),
    [(7, 7), (-1, None), (2**70, None)],
)
def test_a_record_id_is_stored_only_when_the_ledger_can_hold_it(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, value: object, stored: int | None
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.body = {**RECORD, "id": value}

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    with sqlite3.connect(state_db_path(tmp_path)) as conn:
        assert conn.execute("SELECT lrclib_id FROM sidecars").fetchone()[0] == stored


@pytest.mark.parametrize("value", ["7", True, None], ids=repr)
def test_a_record_whose_id_isnt_an_integer_is_unavailable(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, value: object
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    stub_lrclib.body = {**RECORD, "id": value}

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 3, proc.stderr
    assert nonzero(proc.stdout) == {"unavailable": 1}
    assert not track.with_suffix(".lrc").exists()


def test_text_python_cannot_encode_does_not_stop_the_run(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    synced = "".join(f"[00:0{n}.00] placeholder \ud800 line {n}\n" for n in range(1, 5))
    stub_lrclib.body = {**RECORD, "syncedLyrics": synced}

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}


def test_output_holds_counts_and_nothing_from_the_library(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert list(parse_counts(proc.stdout)) == list(ALL_COUNTS)
    assert len(proc.stdout.splitlines()) == len(ALL_COUNTS)
    assert non_notice_lines(proc.stderr) == []
    for private in (b"Fixture", b"t.flac", str(lib).encode()):
        assert private not in proc.stdout + proc.stderr


def test_the_ledger_has_no_lyric_column(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    run_lib(run_cli, stub_lrclib, lib)

    def columns(table: str) -> set[str]:
        return {row[1] for row in query(tmp_path, f"PRAGMA table_info({table})")}

    assert columns("sidecars") == {
        "path",
        "kind",
        "status",
        "sha256",
        "size",
        "lrclib_id",
        "run_id",
        "claiming",
        "dump_id",
        "origin",
        "mtime_ns",
        "refresh_pending",
        # What a replacement took the place of: a digest, a size, an origin and two ids.
        "replaces_sha256",
        "replaces_size",
        "replaces_origin",
        "replaces_lrclib_id",
        "replaces_dump_id",
    }
    assert columns("tracks") == {
        "path",
        "size",
        "mtime_ns",
        "tags_sha256",
        "outcome",
        "reason",
        "dump_id",
        "matcher_version",
        "lrclib_id",
        "run_id",
        "checked_at",
        # For the targeted retry after an LRCLIB update: a time, a year and two 8-byte keys.
        "first_seen_at",
        "release_year",
        "artist_key",
        "title_key",
        # The name of the dump of the last full walk that didn't reach the track.
        "unseen_dump",
    }
    # The dumps an update has seen, and what each diff found: names, times, ids and keys.
    assert columns("lrclib_dumps") == {
        "name",
        "dump_time",
        "status",
        "size",
        "user_version",
        "max_track_id",
        "max_lyrics_id",
        "newest_created_at",
        "id_digest",
        "diff_from",
        "diff_state",
        "recorded_at",
        "swapped_at",
    }
    assert columns("lrclib_changes") == {"dump", "lrclib_id", "artist_key", "title_key"}
    assert columns("runs") == {
        "id",
        "started_at",
        "finished_at",
        "version",
        "dump_id",
        "end_reason",
        "counts_json",
    }


def test_a_new_state_folder_is_private(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    run_lib(run_cli, stub_lrclib, lib)

    for folder in (tmp_path / "state", state_db_path(tmp_path).parent):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def plant_nothing(lib: Path) -> None:
    lib.mkdir()


def plant_only_other_files(lib: Path) -> None:
    (lib / "lost+found").mkdir(parents=True)
    (lib / "notes.txt").write_bytes(b"not audio")


@pytest.mark.parametrize(
    ("plant", "args"),
    [
        (plant_nothing, []),
        (plant_only_other_files, []),
        (copy_track, ["--exclude", "Album"]),
    ],
    ids=["empty", "no_audio", "wholly_excluded"],
)
def test_root_with_nothing_to_look_at_exits_2(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    plant: Callable[[Path], object],
    args: list[str],
) -> None:
    lib = tmp_path / "lib"
    plant(lib)
    before = sorted(lib.rglob("*"))

    proc = run_cli(["run", lib, *args], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 2, proc.stderr
    assert proc.stdout == b""
    lines = non_notice_lines(proc.stderr)
    assert len(lines) == 1, proc.stderr
    assert lines[0].startswith(b"plyrix: ")
    assert b"no audio files" in lines[0]
    assert not state_db_path(tmp_path).exists()
    assert stub_lrclib.requests == []
    assert stub_lrclib.probes == []
    assert sorted(lib.rglob("*")) == before


def test_an_empty_library_is_not_a_clean_run(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 2, proc.stderr
    assert parse_counts(proc.stdout) == {}
    assert stub_lrclib.requests == []


def test_no_root_and_no_url_exit_2_naming_both(run_cli: RunCli) -> None:
    proc = run_cli(["run"])

    assert proc.returncode == 2
    lines = proc.stderr.splitlines()
    assert len(lines) == 1
    assert b"music_root" in lines[0]
    assert b"lrclib_url" in lines[0]
    assert b"Traceback" not in proc.stderr
    assert proc.stdout == b""


def test_a_path_with_no_url_exits_2_naming_the_url(tmp_path: Path, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()

    proc = run_cli(["run", lib])

    assert proc.returncode == 2
    lines = proc.stderr.splitlines()
    assert len(lines) == 1
    assert b"lrclib_url" in lines[0]
    assert b"music_root" not in lines[0]


def test_a_bad_url_exits_2_without_echoing_it(tmp_path: Path, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()

    proc = run_cli(["run", lib], env={"PLYRIX_LRCLIB_URL": f"not a url {CANARY}"})

    assert proc.returncode == 2
    assert len(proc.stderr.splitlines()) == 1
    assert b"lrclib_url" in proc.stderr
    assert CANARY.encode() not in proc.stderr


@pytest.mark.parametrize("target", ["missing", "lib/Album/t.flac"])
def test_a_path_that_is_not_a_folder_exits_2(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, target: str
) -> None:
    copy_track(tmp_path / "lib")

    proc = run_lib_at(run_cli, stub_lrclib, tmp_path / target)

    assert proc.returncode == 2
    assert len(proc.stderr.splitlines()) == 1
    assert b"Traceback" not in proc.stderr
    assert stub_lrclib.requests == []
    assert not state_db_path(tmp_path).exists()


def run_lib_at(run_cli: RunCli, stub: StubLrclib, path: Path):  # type: ignore[no-untyped-def]
    return run_cli(["run", path], env={"PLYRIX_LRCLIB_URL": stub.base_url})


def test_an_unusable_state_path_exits_2(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"a plain file, so no folder can go here")

    proc = run_lib(run_cli, stub_lrclib, lib, PLYRIX_STATE_DB=str(blocker / "state.db"))

    assert proc.returncode == 2
    assert len(non_notice_lines(proc.stderr)) == 1
    assert b"state database" in proc.stderr
    assert b"Traceback" not in proc.stderr
    assert sidecars_in(lib) == []


def test_a_corrupt_state_file_exits_2(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    state = state_db_path(tmp_path)
    state.parent.mkdir(parents=True)
    state.write_bytes(b"this is not a database, and it is long enough to be read as a header" * 8)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 2
    assert b"Traceback" not in proc.stderr
    assert sidecars_in(lib) == []


def test_a_newer_ledger_exits_2(tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    state = state_db_path(tmp_path)
    state.parent.mkdir(parents=True)
    with sqlite3.connect(state) as conn:
        conn.execute("PRAGMA user_version=99")

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 2
    assert b"newer" in proc.stderr
    assert sidecars_in(lib) == []
    assert stub_lrclib.requests == []


def test_album_artist_reaches_lrclib_on_all_five_formats(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    per_format_lib(lib, "albumartist_only")
    stub_lrclib.body = {**RECORD, "artistName": "Fixture Album Artist"}

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 5, "written_lrc": 5, "looked_up": 5}
    assert [request["artist_name"] for request in stub_lrclib.requests] == [
        "Fixture Album Artist"
    ] * len(AUDIO_SUFFIXES)


def test_various_artists_makes_no_request_on_all_five_formats(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    per_format_lib(lib, "various_artists")

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"untagged": 5}
    assert stub_lrclib.requests == []
    assert sidecars_in(lib) == []


@pytest.mark.parametrize("how", ["flag", "env", "narrowed-flag", "narrowed-env"])
def test_paths_configured_exclude_prunes(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, how: str
) -> None:
    lib = tmp_path / "lib"
    narrowed = how.startswith("narrowed")
    top = lib / "music" if narrowed else lib
    keep, skip = copy_track(top, album="keep"), copy_track(top, album="skipme")
    before = snap(skip)
    pattern = "music/skipme" if narrowed else "skipme"
    env = {"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    args: list[str | Path] = ["run", top]
    if narrowed:
        env["PLYRIX_MUSIC_ROOT"] = str(lib)
    if how.endswith("flag"):
        args += ["--exclude", pattern]
    else:
        env["PLYRIX_EXCLUDES"] = json.dumps([pattern])

    proc = run_cli(args, env=env)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert len(stub_lrclib.requests) == 1
    assert keep.with_suffix(".lrc").exists()
    assert sorted(path.name for path in skip.parent.iterdir()) == ["t.flac"]
    assert snap(skip) == before


def test_paths_exclude_from_the_config_file(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    keep, skip = copy_track(lib, album="keep"), copy_track(lib, album="skipme")
    config = tmp_path / "plyrix.toml"
    config.write_text('excludes = ["skipme/*"]\n', encoding="utf-8")

    proc = run_cli(
        ["run", lib, "--config", config], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    )

    assert proc.returncode == 0, proc.stderr
    assert keep.with_suffix(".lrc").exists()
    assert not skip.with_suffix(".lrc").exists()


def test_paths_exclude_on_a_name_that_is_not_utf8(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, album="keep")
    folder = os.path.join(os.fsencode(lib), b"caf\xe9")
    os.mkdir(folder)
    shutil.copyfile(FIXTURES / "t.flac", os.path.join(folder, b"t.flac"))

    proc = run_cli(
        ["run", lib, "--exclude", b"caf\xe9"], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    )

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert os.listdir(folder) == [b"t.flac"]


@pytest.mark.parametrize("how", ["flag", "env"])
def test_paths_exclude_on_a_utf8_name_that_is_not_ascii(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, how: str
) -> None:
    lib = tmp_path / "lib"
    keep, skip = copy_track(lib, album="keep"), copy_track(lib, album="Björk")
    env = {"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    args: list[str | Path] = ["run", lib]
    if how == "flag":
        args += ["--exclude", "Björk"]
    else:
        env["PLYRIX_EXCLUDES"] = json.dumps(["Björk"], ensure_ascii=False)

    proc = run_cli(args, env=env)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert keep.with_suffix(".lrc").exists()
    assert not skip.with_suffix(".lrc").exists()


def test_paths_a_run_inside_an_excluded_folder_writes_nothing(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    skip = copy_track(lib, album="skipme")

    proc = run_cli(
        ["run", skip.parent, "--exclude", "skipme"],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "PLYRIX_MUSIC_ROOT": str(lib)},
    )

    assert proc.returncode == 2, proc.stderr
    assert nonzero(proc.stdout) == {}
    assert sidecars_in(lib) == []
    assert stub_lrclib.requests == []


def copy_to_bytes_name(folder: bytes, name: bytes) -> bytes:
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    shutil.copyfile(FIXTURES / "t.flac", path)
    return path


def test_paths_non_utf8_names_are_written_and_found_again(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    folder = os.path.join(os.fsencode(lib), b"caf\xe9")
    copy_to_bytes_name(folder, b"\xff\xfe-t.flac")

    first = run_lib(run_cli, stub_lrclib, lib)
    second = run_lib(run_cli, stub_lrclib, lib)

    assert first.returncode == 0, first.stderr
    assert nonzero(first.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert b"\xff\xfe-t.lrc" in os.listdir(folder)
    assert [row[3] for row in ledger_rows(tmp_path)] == [os.path.join(folder, b"\xff\xfe-t.lrc")]
    assert nonzero(second.stdout) == {"synced": 1, "skipped_unchanged": 1}
    assert len(stub_lrclib.requests) == 1


def test_paths_a_250_byte_stem_is_written(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    stem = b"s" * 250
    folder = os.path.join(os.fsencode(lib), b"Album")
    copy_to_bytes_name(folder, stem + b".flac")

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert stem + b".lrc" in os.listdir(folder)
    assert sorted(len(name) for name in os.listdir(folder)) == [254, 255]


ESCAPE_NAME = b"[bold]\x1b[31mt.flac"


def test_paths_a_terminal_escape_never_reaches_output_by_default(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_to_bytes_name(os.fsencode(lib), ESCAPE_NAME)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    output = proc.stdout + proc.stderr
    assert b"\x1b" not in output
    assert b"bold" not in output


def test_paths_verbose_prints_a_terminal_escape_harmlessly(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_to_bytes_name(os.fsencode(lib), ESCAPE_NAME)

    proc = run_cli(["run", lib, "--verbose"], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 0, proc.stderr
    assert b"\x1b" not in proc.stdout + proc.stderr
    assert b"synced [bold]\\x1b[31mt.flac\n" in proc.stdout


def test_paths_verbose_names_each_track_and_its_outcome(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    album = copy_track(lib).parent
    (album / "broken.flac").write_bytes(b"this is not a flac stream")
    (album / "song.flac").write_bytes(b"x")
    (album / "song.mp3").write_bytes(b"x")
    (album / "link.flac").symlink_to("t.flac")
    (album / "skipped.flac").write_bytes(b"x")

    proc = run_cli(
        ["run", lib, "--verbose", "--exclude", "Album/skipped.flac"],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url},
    )

    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert sorted(line for line in lines if b"/" in line) == [
        b"stem_collision Album/song.flac",
        b"stem_collision Album/song.mp3",
        b"symlink Album/link.flac",
        b"synced Album/t.flac",
        b"unreadable Album/broken.flac",
    ]
    assert parse_counts(proc.stdout) == {
        **dict.fromkeys(ALL_COUNTS, 0),
        "stem_collision": 2,
        "symlink": 1,
        "unreadable": 1,
        "synced": 1,
        "written_lrc": 1,
        "looked_up": 1,
    }


def test_paths_a_name_cannot_forge_a_count_line(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_to_bytes_name(os.fsencode(lib), b"x\nwritten=99\n.flac")

    proc = run_cli(["run", lib, "--verbose"], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["written_lrc"] == 1


def test_paths_verbose_on_a_strict_stdout(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_to_bytes_name(os.fsencode(lib), b"caf\xe9-\xff.flac")

    proc = run_cli(
        ["run", lib, "--verbose"],
        env={
            "PLYRIX_LRCLIB_URL": stub_lrclib.base_url,
            "PYTHONIOENCODING": "ascii:strict",
            "PYTHONUTF8": "0",
            "LC_ALL": "C",
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert b"Traceback" not in proc.stderr
    assert parse_counts(proc.stdout)["written_lrc"] == 1


def test_paths_past_path_max_are_counted_and_the_run_goes_on(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    good = copy_track(lib, album="z")
    base = lib / "deep"
    base.mkdir()
    plant_deep_track(base, PATH_MAX + 900)
    try:
        proc = run_cli(["run", lib, "--verbose"], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})
    finally:
        shutil.rmtree(base)

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["too_long"] == 1
    assert parse_counts(proc.stdout)["written_lrc"] == 1
    assert b"Traceback" not in proc.stderr
    assert good.with_suffix(".lrc").exists()


def test_paths_with_no_room_for_a_temp_name_count_as_a_failed_write(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    good = copy_track(lib, album="z")
    base = lib / "deep"
    base.mkdir()
    # t.flac fits under PATH_MAX here, and the 21-byte temp name doesn't.
    plant_deep_track(base, PATH_MAX - 12)
    try:
        proc = run_lib(run_cli, stub_lrclib, lib)
    finally:
        shutil.rmtree(base)

    assert proc.returncode == 3, proc.stderr
    assert nonzero(proc.stdout) == {
        "synced": 1,
        "write_failed": 1,
        "written_lrc": 1,
        "looked_up": 2,
    }
    assert good.with_suffix(".lrc").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_paths_a_file_that_cant_be_opened_is_a_failed_run_and_the_run_goes_on(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    good = copy_track(lib, name="a.flac")
    locked = good.with_name("z.flac")
    shutil.copyfile(good, locked)
    locked.chmod(0)
    try:
        proc = run_cli(["run", lib, "--verbose"], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})
    finally:
        locked.chmod(0o644)

    assert proc.returncode == 3, proc.stderr
    assert parse_counts(proc.stdout)["read_failed"] == 1
    assert parse_counts(proc.stdout)["written_lrc"] == 1
    assert parse_counts(proc.stdout)["unreadable"] == 0
    assert b"read_failed Album/z.flac" in proc.stdout.splitlines()
    assert not locked.with_suffix(".lrc").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
@pytest.mark.parametrize(
    ("body", "suffix", "settled"),
    [
        (RECORD, ".lrc", {"synced": 1, "skipped_unchanged": 1}),
        ({**RECORD, "syncedLyrics": None}, ".txt", {"plain": 1, "skipped_unchanged": 1}),
    ],
    ids=["lrc", "txt"],
)
def test_a_settled_sidecar_that_cant_be_read_is_a_failed_run(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    body: dict[str, object],
    suffix: str,
    settled: dict[str, int],
) -> None:
    lib = tmp_path / "lib"
    sidecar = copy_track(lib).with_suffix(suffix)
    stub_lrclib.body = body
    args: list[str | Path] = ["run", lib, "--dump-id", "dump-a"]
    env = {"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    assert run_cli(args, env=env).returncode == 0
    before = sidecar.read_bytes()

    sidecar.chmod(0)
    try:
        proc = run_cli(args, env=env)
    finally:
        sidecar.chmod(0o644)

    assert proc.returncode == 3, proc.stderr
    assert nonzero(proc.stdout) == {"read_failed": 1}
    assert sidecar.read_bytes() == before

    healed = run_cli(args, env=env)

    assert healed.returncode == 0, healed.stderr
    assert nonzero(healed.stdout) == settled


def lock_every_file(album: Path) -> Callable[[], None]:
    files = list(album.iterdir())
    for path in files:
        path.chmod(0)

    def undo() -> None:
        for path in files:
            path.chmod(0o644)

    return undo


def lock_traversal(album: Path) -> Callable[[], None]:
    """Leave the album listable but its files out of reach, which a run sees as a failed stat."""
    album.chmod(0o444)
    return lambda: album.chmod(0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
@pytest.mark.parametrize("lock", [lock_every_file, lock_traversal], ids=["files", "traversal"])
def test_a_run_that_can_list_the_music_but_read_none_of_it_fails(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    lock: Callable[[Path], Callable[[], None]],
) -> None:
    """A timer or cron job only sees the exit code, and here it would see success with no work."""
    lib = tmp_path / "lib"
    first = copy_track(lib, name="a.flac")
    copy_track(lib, name="b.flac")
    unlock = lock(first.parent)
    try:
        proc = run_lib(run_cli, stub_lrclib, lib)
    finally:
        unlock()

    assert proc.returncode == 3, proc.stderr
    assert nonzero(proc.stdout) == {"read_failed": 2}
    assert b"a file read" in proc.stderr
    assert stub_lrclib.requests == []

    # Fixing the permissions is all it takes.
    proc = run_lib(run_cli, stub_lrclib, lib)
    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 2, "written_lrc": 2, "looked_up": 2}


def test_paths_an_unreadable_folder_is_counted(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    locked = copy_track(lib, album="a").parent
    copy_track(lib, album="z")
    locked.chmod(0)
    try:
        proc = run_lib(run_cli, stub_lrclib, lib)
    finally:
        locked.chmod(0o755)

    assert proc.returncode == 3, proc.stderr
    assert nonzero(proc.stdout) == {
        "synced": 1,
        "folder_unreadable": 1,
        "written_lrc": 1,
        "looked_up": 1,
    }


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores folder permissions")
def test_a_library_root_that_cant_be_listed_is_a_failed_run(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    lib.chmod(0)
    try:
        proc = run_lib(run_cli, stub_lrclib, lib)
    finally:
        lib.chmod(0o755)

    assert proc.returncode == 3, proc.stderr
    assert nonzero(proc.stdout) == {"folder_unreadable": 1}
    assert b"folders that could not be listed" in proc.stderr


def remove_during_next_lookup(stub: StubLrclib, target: Path) -> None:
    """Delete target while the run waits on LRCLIB, which is after the walker listed its folder."""
    real_next_response = stub.next_response

    def remove_then_answer() -> tuple[int, object]:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
        return real_next_response()

    stub.next_response = remove_then_answer  # type: ignore[method-assign]


def test_paths_a_vanishing_file_is_counted(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    first = copy_track(lib, name="a.flac")
    remove_during_next_lookup(stub_lrclib, Path(shutil.copy(first, first.with_name("z.flac"))))

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "unreadable": 1, "written_lrc": 1, "looked_up": 1}


def test_paths_a_vanishing_folder_is_counted(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, album="a")
    remove_during_next_lookup(stub_lrclib, copy_track(lib, album="z").parent)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "unreadable": 1, "written_lrc": 1, "looked_up": 1}


def test_paths_a_folder_vanishing_under_its_own_track_is_a_failed_write(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    remove_during_next_lookup(stub_lrclib, copy_track(lib).parent)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 3, proc.stderr
    assert nonzero(proc.stdout) == {"write_failed": 1, "looked_up": 1}
    assert published_paths(tmp_path) == []

    # A missing folder says nothing about its files, so the row stays until the folder is back.
    assert nonzero(run_lib(run_cli, stub_lrclib, lib).stdout) == {}
    assert [row[0] for row in ledger_rows(tmp_path)] == ["intent"]

    # With the folder back and nothing at that name, the row goes and the track is written.
    del stub_lrclib.next_response
    copy_track(lib)
    assert nonzero(run_lib(run_cli, stub_lrclib, lib).stdout) == {
        "synced": 1,
        "written_lrc": 1,
        "looked_up": 1,
    }
    assert [row[0] for row in ledger_rows(tmp_path)] == ["published"]


def test_paths_a_folder_replaced_by_a_file_is_not_a_failed_read(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    first = copy_track(lib, name="a.flac")
    copy_track(lib, name="b.flac")
    album = first.parent
    real_next_response = stub_lrclib.next_response

    def replace_folder_then_answer() -> tuple[int, object]:
        if album.is_dir():
            shutil.rmtree(album)
            album.write_bytes(b"")
        return real_next_response()

    stub_lrclib.next_response = replace_folder_then_answer  # type: ignore[method-assign]

    proc = run_lib(run_cli, stub_lrclib, lib)

    # The first track was mid-publish, and the second has nothing left to read.
    assert nonzero(proc.stdout) == {"unreadable": 1, "write_failed": 1, "looked_up": 1}


def test_paths_symlinks_are_counted_and_not_followed(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    outside = copy_track(tmp_path / "outside")
    lib.mkdir()
    (lib / "file.flac").symlink_to(outside)
    (lib / "folder").symlink_to(outside.parent)
    (lib / "dangling.flac").symlink_to("missing.flac")

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"symlink": 3}
    assert stub_lrclib.requests == []
    assert sorted(path.name for path in outside.parent.iterdir()) == ["t.flac"]


def test_paths_a_stem_collision_is_counted_with_no_lookup(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    flac = copy_track(lib, name="song.flac")
    shutil.copyfile(FIXTURES / "t.mp3", flac.with_suffix(".mp3"))

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"stem_collision": 2}
    assert stub_lrclib.requests == []
    assert sidecars_in(lib) == []


def test_paths_never_appear_without_verbose(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    album = copy_track(lib, album="PrivateAlbum").parent
    (album / "broken.flac").write_bytes(b"this is not a flac stream")
    (album / "link.flac").symlink_to("t.flac")
    (album / "song.flac").write_bytes(b"x")
    (album / "song.mp3").write_bytes(b"x")

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert len(proc.stdout.splitlines()) == len(ALL_COUNTS)
    assert b"PrivateAlbum" not in proc.stdout + proc.stderr


# Re-runs. A child records every file it opens, as a stat opens nothing, so these prove a settled
# track costs no audio read. The hook goes in after the imports, so it only sees the run.


def test_rerun_opens_no_audio_and_no_sidecar(tmp_path: Path, stub_lrclib: StubLrclib) -> None:
    lib = tmp_path / "lib"
    tracks = [
        copy_track(lib, name="a-synced.flac"),
        copy_track(lib, name="b-miss.flac"),
        copy_track(lib, name="c-instrumental.flac"),
    ]
    untagged = tracks[0].with_name("d-untagged.flac")
    shutil.copyfile(FIXTURES / "notags.flac", untagged)
    stub_lrclib.queue = [
        (200, RECORD),
        NOT_FOUND,
        NOT_FOUND,
        (200, INSTRUMENTAL),
        (200, INSTRUMENTAL),
    ]

    first, opened = audited_run(tmp_path, stub_lrclib, lib)

    assert first.returncode == 0, first.stderr
    assert nonzero(first.stdout) == {
        "synced": 1,
        "instrumental": 1,
        "no_match": 1,
        "untagged": 1,
        "written_lrc": 1,
        "looked_up": 3,
        "no_match_not_found": 1,
    }
    # This shows the hook can see an audio file being opened, which the next run must not do.
    assert sum(path.endswith(".flac") for path in opened) == 4
    before = [snap(track) for track in (*tracks, untagged)]
    requests = len(stub_lrclib.requests)
    stub_lrclib.status, stub_lrclib.body = 503, {}

    second, opened = audited_run(tmp_path, stub_lrclib, lib)

    assert second.returncode == 0, second.stderr
    assert nonzero(second.stdout) == {
        "synced": 1,
        "instrumental": 1,
        "no_match": 1,
        "untagged": 1,
        "skipped_unchanged": 4,
        "no_match_not_found": 1,
    }
    assert opened == []
    assert len(stub_lrclib.requests) == requests
    assert [snap(track) for track in (*tracks, untagged)] == before


def test_rerun_a_touch_with_equal_tags_stays_settled(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    run_dump(run_cli, stub_lrclib, lib, "dump-a")
    hash_before = query(tmp_path, "SELECT tags_sha256 FROM tracks")
    later = track.stat().st_mtime_ns + 10**9
    os.utime(track, ns=(later, later))

    proc, opened = audited_run(tmp_path, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"no_match": 1, "no_match_not_found": 1, "skipped_unchanged": 1}
    assert len(stub_lrclib.requests) == 3
    assert opened == [os.fsdecode(track)]
    assert query(tmp_path, "SELECT mtime_ns, outcome, reason, dump_id FROM tracks") == [
        (later, "no_match", "not_found", "dump-a")
    ]
    assert query(tmp_path, "SELECT tags_sha256 FROM tracks") == hash_before

    _, opened = audited_run(tmp_path, stub_lrclib, lib)

    assert opened == []


def test_rerun_a_touch_keeps_the_dump_that_gave_an_untagged_result(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    track = Path(shutil.copyfile(FIXTURES / "notags.flac", lib / "t.flac"))
    run_dump(run_cli, stub_lrclib, lib, "dump-a")
    later = track.stat().st_mtime_ns + 10**9
    os.utime(track, ns=(later, later))

    proc = run_dump(run_cli, stub_lrclib, lib, "dump-b")

    assert nonzero(proc.stdout) == {"untagged": 1, "skipped_unchanged": 1}
    assert query(tmp_path, "SELECT mtime_ns, outcome, dump_id FROM tracks") == [
        (later, "untagged", "dump-a")
    ]


def test_rerun_changed_tags_look_up_again(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    run_dump(run_cli, stub_lrclib, lib, "dump-a")
    hash_before = query(tmp_path, "SELECT tags_sha256 FROM tracks")
    shutil.copyfile(FIXTURES / "albumartist_only.flac", track)

    proc = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert nonzero(proc.stdout) == {"no_match": 1, "looked_up": 1, "no_match_not_found": 1}
    assert len(stub_lrclib.requests) == 6
    assert query(tmp_path, "SELECT tags_sha256 FROM tracks") != hash_before


def test_rerun_a_raised_matcher_version_looks_up_again(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    run_dump(run_cli, stub_lrclib, lib, "dump-a")
    query(tmp_path, "UPDATE tracks SET matcher_version = 0")

    proc = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert nonzero(proc.stdout) == {"no_match": 1, "looked_up": 1, "no_match_not_found": 1}
    assert len(stub_lrclib.requests) == 6
    assert query(tmp_path, "SELECT matcher_version FROM tracks") == [(MATCHER_VERSION,)]


def plant_untagged(path: Path) -> None:
    shutil.copyfile(FIXTURES / "notags.flac", path)


def plant_corrupt(path: Path) -> None:
    path.write_bytes(b"this is not a flac stream")


@pytest.mark.parametrize(
    ("plant", "count", "outcome"),
    [(plant_untagged, "untagged", "untagged"), (plant_corrupt, "unreadable", "unreadable")],
    ids=["untagged", "corrupt"],
)
def test_rerun_a_file_that_cant_be_looked_up_is_not_opened_again(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    plant: Callable[[Path], None],
    count: str,
    outcome: str,
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    track = lib / "t.flac"
    plant(track)

    first, opened = audited_run(tmp_path, stub_lrclib, lib)

    assert nonzero(first.stdout) == {count: 1}
    assert opened == [os.fsdecode(track)]
    assert query(tmp_path, "SELECT outcome, size, mtime_ns FROM tracks") == [
        (outcome, track.stat().st_size, track.stat().st_mtime_ns)
    ]

    second, opened = audited_run(tmp_path, stub_lrclib, lib)

    assert second.returncode == 0, second.stderr
    assert nonzero(second.stdout) == {count: 1, "skipped_unchanged": 1}
    assert opened == []
    assert stub_lrclib.requests == []

    # With good contents, the file is read again and gets its lookup.
    shutil.copyfile(FIXTURES / "t.flac", track)
    third, _ = audited_run(tmp_path, stub_lrclib, lib)

    assert nonzero(third.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert query(tmp_path, "SELECT outcome FROM tracks") == [("synced",)]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_rerun_a_file_that_cant_be_opened_is_not_settled(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    track.chmod(0)
    try:
        proc = run_dump(run_cli, stub_lrclib, lib, "dump-a")
    finally:
        track.chmod(0o644)

    assert proc.returncode == 3, proc.stderr
    assert nonzero(proc.stdout) == {"read_failed": 1}
    assert query(tmp_path, "SELECT count(*) FROM tracks") == [(0,)]

    assert nonzero(run_dump(run_cli, stub_lrclib, lib, "dump-a").stdout) == {
        "synced": 1,
        "written_lrc": 1,
        "looked_up": 1,
    }


def test_rerun_a_file_gone_during_the_tag_read_is_not_settled(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mount that drops in that gap and comes back must not leave a healthy track unreadable."""
    lib = tmp_path / "lib"
    track = copy_track(lib)
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    settings = settings.model_copy(update={"dump_id": "dump-a"})
    start = os.fsencode(lib)

    def gone(_path: bytes) -> None:
        raise tags.Gone("FileNotFoundError")

    with monkeypatch.context() as faults:
        faults.setattr(tags, "read", gone)
        counts = pipeline.run(settings, start)

    assert (counts.unreadable, counts.read_failed, counts.written_lrc) == (1, 0, 0)
    assert query_db(settings.state_db, "SELECT count(*) FROM tracks") == [(0,)]

    later = pipeline.run(settings, start)

    assert (later.unreadable, later.written_lrc, later.synced) == (0, 1, 1)
    assert track.with_suffix(".lrc").exists()


def test_rerun_a_corrupt_file_is_still_settled_as_unreadable(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    settings = settings.model_copy(update={"dump_id": "dump-a"})

    def corrupt(_path: bytes) -> None:
        raise tags.Unreadable("TinyTagException")

    with monkeypatch.context() as faults:
        faults.setattr(tags, "read", corrupt)
        counts = pipeline.run(settings, os.fsencode(lib))

    assert counts.unreadable == 1
    assert query_db(settings.state_db, "SELECT outcome FROM tracks") == [("unreadable",)]


def test_rerun_a_sidecar_records_its_stat_when_published(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    sidecar = copy_track(lib).with_suffix(".lrc")

    run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert query(tmp_path, "SELECT size, mtime_ns FROM sidecars") == [
        (sidecar.stat().st_size, sidecar.stat().st_mtime_ns)
    ]


def forget_the_mtime(tmp_path: Path, _sidecar: Path) -> None:
    query(tmp_path, "UPDATE sidecars SET mtime_ns = NULL")


def touch_the_sidecar(_tmp_path: Path, sidecar: Path) -> None:
    later = sidecar.stat().st_mtime_ns + 10**9
    os.utime(sidecar, ns=(later, later))


@pytest.mark.parametrize(
    "disturb", [forget_the_mtime, touch_the_sidecar], ids=["null-mtime", "touched"]
)
def test_rerun_a_sidecar_whose_stat_cant_be_trusted_is_hashed_once(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    disturb: Callable[[Path, Path], None],
) -> None:
    lib = tmp_path / "lib"
    sidecar = copy_track(lib).with_suffix(".lrc")
    run_dump(run_cli, stub_lrclib, lib, "dump-a")
    disturb(tmp_path, sidecar)

    proc, opened = audited_run(tmp_path, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "skipped_unchanged": 1}
    assert opened == [os.fsdecode(sidecar)]
    assert query(tmp_path, "SELECT mtime_ns FROM sidecars") == [(sidecar.stat().st_mtime_ns,)]

    _, opened = audited_run(tmp_path, stub_lrclib, lib)

    assert opened == []


def test_rerun_an_edit_of_the_same_size_is_still_noticed(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    sidecar = copy_track(lib).with_suffix(".lrc")
    run_dump(run_cli, stub_lrclib, lib, "dump-a")
    edited = b"x" * sidecar.stat().st_size
    sidecar.write_bytes(edited)

    proc = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert nonzero(proc.stdout) == {"blocked_foreign": 1}
    assert sidecar.read_bytes() == edited


def test_rerun_an_owned_sidecar_with_no_result_gets_one_without_a_read(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    run_dump(run_cli, stub_lrclib, lib, "dump-a")
    query(tmp_path, "DELETE FROM tracks")

    proc, opened = audited_run(tmp_path, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "skipped_unchanged": 1}
    assert opened == []
    assert len(stub_lrclib.requests) == 1
    assert query(tmp_path, "SELECT size, mtime_ns, tags_sha256, outcome, dump_id FROM tracks") == [
        (track.stat().st_size, track.stat().st_mtime_ns, None, "synced", "dump-a")
    ]

    # With no hash to compare, a lost sidecar still gets its lookup, and the hash fills in.
    track.with_suffix(".lrc").unlink()
    again = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert nonzero(again.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert query(tmp_path, "SELECT tags_sha256 IS NOT NULL FROM tracks") == [(1,)]


def test_rerun_a_folder_gone_for_a_run_keeps_its_results(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    album = copy_track(lib).parent
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    run_dump(run_cli, stub_lrclib, lib, "dump-a")
    away = tmp_path / "away"
    album.rename(away)

    assert nonzero(run_dump(run_cli, stub_lrclib, lib, "dump-a").stdout) == {}

    away.rename(album)
    assert query(tmp_path, "SELECT count(*) FROM tracks") == [(1,)]
    assert nonzero(run_dump(run_cli, stub_lrclib, lib, "dump-a").stdout) == {
        "no_match": 1,
        "no_match_not_found": 1,
        "skipped_unchanged": 1,
    }
    assert len(stub_lrclib.requests) == 3


def test_counts_every_walked_item_lands_in_one_class(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    album = copy_track(lib).parent
    shutil.copyfile(FIXTURES / "notags.flac", album / "untagged.flac")
    (album / "zero.flac").write_bytes(bytes(4096))
    (album / "song.flac").write_bytes(b"x")
    (album / "song.mp3").write_bytes(b"x")
    (album / "link.flac").symlink_to("t.flac")
    root = os.fsencode(lib)
    walked = list(walker.walk(root, root, []))
    assert len(walked) == 6

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    counts = parse_counts(proc.stdout)
    assert sum(counts.get(name, 0) for name in (*COVERAGE_COUNTS, *PROBLEM_COUNTS)) == len(walked)
    assert counts.get("synced") == 1
    assert counts.get("stem_collision") == 2
    assert counts.get("symlink") == 1


def test_counts_reasons_sum_to_no_match(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    for index in range(3):
        copy_track(lib, name=f"{index}.flac")
    stub_lrclib.queue = [*[NOT_FOUND] * 4, *[(400, {"name": "ValidationError"})] * 2]

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {
        "no_match": 3,
        "no_match_not_found": 2,
        "no_match_rejected": 1,
        "looked_up": 3,
    }
    counts = parse_counts(proc.stdout)
    assert sum(counts[name] for name in REASON_COUNTS) == counts["no_match"]


def test_counts_a_second_run_still_reports_what_the_library_holds(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    first = run_lib(run_cli, stub_lrclib, lib)
    second = run_lib(run_cli, stub_lrclib, lib)

    assert nonzero(first.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert nonzero(second.stdout) == {"synced": 1, "skipped_unchanged": 1}


@pytest.mark.parametrize(
    ("answer", "coverage"),
    [
        (NOT_FOUND, {"no_match": 1, "no_match_not_found": 1}),
        ((400, {"name": "ValidationError"}), {"no_match": 1, "no_match_rejected": 1}),
        ((200, INSTRUMENTAL), {"instrumental": 1}),
    ],
    ids=["not-found", "rejected", "instrumental"],
)
def test_counts_a_stored_result_keeps_its_outcome_and_reason_on_a_skipped_run(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    answer: tuple[int, object],
    coverage: dict[str, int],
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.status, stub_lrclib.body = answer

    first = run_dump(run_cli, stub_lrclib, lib, "dump-a")
    second = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert nonzero(first.stdout) == {**coverage, "looked_up": 1}
    assert nonzero(second.stdout) == {**coverage, "skipped_unchanged": 1}
    assert len(stub_lrclib.requests) == 3


def test_counts_a_stored_untagged_or_corrupt_file_keeps_its_class_on_a_skipped_run(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    album = lib / "Album"
    album.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "notags.flac", album / "untagged.flac")
    (album / "broken.flac").write_bytes(b"this is not a flac stream")

    first = run_dump(run_cli, stub_lrclib, lib, "dump-a")
    second = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert nonzero(first.stdout) == {"untagged": 1, "unreadable": 1}
    assert nonzero(second.stdout) == {"untagged": 1, "unreadable": 1, "skipped_unchanged": 2}


def test_counts_stdout_is_one_line_per_field_in_order(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert [line.split(b"=")[0].decode() for line in proc.stdout.splitlines()] == list(ALL_COUNTS)
    assert list(parse_counts(proc.stdout)) == list(ALL_COUNTS)


def test_counts_verbose_names_a_field_a_reason_and_the_path(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, name="hit.flac")
    copy_track(lib, name="miss.flac")
    stub_lrclib.queue = [(200, RECORD), NOT_FOUND, NOT_FOUND]

    def path_lines(proc: "subprocess.CompletedProcess[bytes]") -> list[bytes]:
        return sorted(line for line in proc.stdout.splitlines() if b"/" in line)

    first = run_cli(
        ["run", lib, "--verbose", "--dump-id", "dump-a"],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url},
    )
    second = run_cli(
        ["run", lib, "--verbose", "--dump-id", "dump-a"],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url},
    )

    expected = [b"no_match_not_found Album/miss.flac", b"synced Album/hit.flac"]
    assert path_lines(first) == expected
    assert path_lines(second) == expected


def test_verified_a_matching_record_writes_lrc(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    tracks = per_format_lib(lib, "t")
    before = [snap(track) for track in tracks]

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 5, "written_lrc": 5, "looked_up": 5}
    expected = encode(str(RECORD["syncedLyrics"]))
    assert [track.with_suffix(".lrc").read_bytes() for track in tracks] == [expected] * 5
    assert [snap(track) for track in tracks] == before
    assert leaks(tmp_path, proc, lib) == []


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"artistName": "Other Artist"}, "doubtful_artist"),
        ({"trackName": "Other Title"}, "doubtful_title"),
        ({"duration": 30.0}, "doubtful_duration"),
        ({"trackName": "Fixture Title (Live)"}, "doubtful_version"),
        ({"albumName": "Fixture Album Live"}, "doubtful_version"),
    ],
    ids=["artist", "title", "duration", "version", "album_version"],
)
def test_verified_doubtful_records_write_nothing(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    fields: dict[str, object],
    reason: str,
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    before = snap(track)
    stub_lrclib.body = {**RECORD, **fields}

    proc = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"no_match": 1, f"no_match_{reason}": 1, "looked_up": 1}
    assert sidecars_in(lib) == []
    assert ledger_rows(tmp_path) == []
    assert query(tmp_path, "SELECT outcome, reason FROM tracks") == [("no_match", reason)]
    assert snap(track) == before
    assert leaks(tmp_path, proc, lib) == []


BEYOND_THE_END = f"[00:01.00] {CANARY} line 1\n[01:00.00] {CANARY} line 2\n"


@pytest.mark.parametrize(
    ("plain", "counts", "row"),
    [
        (f"{CANARY} plain line 1", {"plain": 1, "written_txt": 1}, ("plain", "", 7)),
        (
            None,
            {"no_match": 1, "no_match_timing_rejected": 1},
            ("no_match", "timing_rejected", 7),
        ),
    ],
    ids=["with_plain_text", "without"],
)
def test_verified_bad_timing_falls_back(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    plain: str | None,
    counts: dict[str, int],
    row: tuple[str, str, int],
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    before = snap(track)
    stub_lrclib.body = {**RECORD, "syncedLyrics": BEYOND_THE_END, "plainLyrics": plain}

    proc = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {**counts, "looked_up": 1}
    assert not track.with_suffix(".lrc").exists()
    assert query(tmp_path, "SELECT outcome, reason, lrclib_id FROM tracks") == [row]
    assert snap(track) == before
    assert leaks(tmp_path, proc, lib) == []


def test_verified_instrumental_counts_only_with_no_lyrics(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib, "Silent")
    stub_lrclib.body = INSTRUMENTAL
    proc = run_dump(run_cli, stub_lrclib, lib, "dump-a")
    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"instrumental": 1, "looked_up": 1}
    assert sidecars_in(lib) == []

    track = copy_track(lib, "Sung")
    stub_lrclib.body = {**RECORD, "instrumental": True}
    proc = run_dump(run_cli, stub_lrclib, lib, "dump-a")
    assert proc.returncode == 0, proc.stderr
    assert sidecars_in(lib) == [track.with_suffix(".lrc")]
    assert track.with_suffix(".lrc").read_bytes() == encode(str(RECORD["syncedLyrics"]))
    assert parse_counts(proc.stdout)["written_lrc"] == 1


@pytest.mark.parametrize(
    ("artist", "written"),
    [("Fixture Album Artist", True), ("Fixture Artist", False)],
    ids=["its_album_artist", "another_artist"],
)
def test_verified_album_artist_is_compared(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, artist: str, written: bool
) -> None:
    lib = tmp_path / "lib"
    (lib / "Album").mkdir(parents=True)
    track = Path(shutil.copyfile(FIXTURES / "albumartist_only.flac", lib / "Album" / "t.flac"))
    stub_lrclib.body = {**RECORD, "artistName": artist}

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").exists() is written
    assert stub_lrclib.requests[0]["artist_name"] == "Fixture Album Artist"
    expected = (
        {"synced": 1, "written_lrc": 1}
        if written
        else {
            "no_match": 1,
            "no_match_doubtful_artist": 1,
        }
    )
    assert nonzero(proc.stdout) == {**expected, "looked_up": 1}


@pytest.mark.parametrize(
    ("synced", "finding"),
    [
        (f"[00:01.00] {CANARY} line 1\n[00:02.00] {CANARY} line 2\n", "lrc_few_lines"),
        (
            "".join(f"[00:00.{tenths}0] {CANARY} line {tenths}\n" for tenths in range(1, 6)),
            "lrc_low_coverage",
        ),
    ],
    ids=["few_lines", "low_coverage"],
)
def test_verified_a_report_only_finding_still_writes(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, synced: str, finding: str
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    stub_lrclib.body = {**RECORD, "syncedLyrics": synced}

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1, finding: 1}
    assert track.with_suffix(".lrc").read_bytes() == encode(synced)


# Plain lyrics. A .txt holds them, as a .lrc with no timestamps attaches in Plex and shows nothing.

PLAIN_ONLY = {**RECORD, "syncedLyrics": None}
PLAIN_BYTES = encode(str(RECORD["plainLyrics"]))
# A plain answer settles nothing, so the track costs its two gets and its search.
PLAIN_LOOKUP_REQUESTS = 3


def run_plain(run_cli: RunCli, stub: StubLrclib, lib: Path, setting: str, dump_id: str):  # type: ignore[no-untyped-def]
    return run_cli(
        ["run", lib, "--plain", setting, "--dump-id", dump_id],
        env={"PLYRIX_LRCLIB_URL": stub.base_url},
    )


def test_plain_only_writes_txt_never_lrc(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    before = snap(track)
    stub_lrclib.body = PLAIN_ONLY

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"plain": 1, "written_txt": 1, "looked_up": 1}
    sidecar = track.with_suffix(".txt")
    assert sidecar.read_bytes() == PLAIN_BYTES
    assert not track.with_suffix(".lrc").exists()
    digest = hashlib.sha256(PLAIN_BYTES).hexdigest()
    assert ledger_rows(tmp_path) == [("published", digest, len(PLAIN_BYTES), os.fsencode(sidecar))]
    assert query(tmp_path, "SELECT kind FROM sidecars") == [("txt",)]
    assert snap(track) == before
    assert leaks(tmp_path, proc, lib) == []


def test_plain_bad_timing_falls_back_to_txt(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    stub_lrclib.body = {**RECORD, "syncedLyrics": BEYOND_THE_END}

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"plain": 1, "written_txt": 1, "looked_up": 1}
    assert track.with_suffix(".txt").read_bytes() == PLAIN_BYTES
    assert not track.with_suffix(".lrc").exists()
    assert leaks(tmp_path, proc, lib) == []


def test_plain_skip_writes_nothing(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    stub_lrclib.body = PLAIN_ONLY

    skipped = run_plain(run_cli, stub_lrclib, lib, "skip", "dump-a")

    assert skipped.returncode == 0, skipped.stderr
    assert nonzero(skipped.stdout) == {"plain_skipped": 1, "looked_up": 1}
    assert sidecars_in(lib) == []
    assert ledger_rows(tmp_path) == []
    assert query(tmp_path, "SELECT outcome, reason FROM tracks") == [("plain_skipped", "")]
    requests = len(stub_lrclib.requests)

    again = run_plain(run_cli, stub_lrclib, lib, "skip", "dump-a")

    assert nonzero(again.stdout) == {"plain_skipped": 1, "skipped_unchanged": 1}
    assert len(stub_lrclib.requests) == requests

    written = run_plain(run_cli, stub_lrclib, lib, "write", "dump-a")

    assert written.returncode == 0, written.stderr
    assert nonzero(written.stdout) == {"plain": 1, "written_txt": 1, "looked_up": 1}
    assert len(stub_lrclib.requests) == requests + PLAIN_LOOKUP_REQUESTS
    assert track.with_suffix(".txt").read_bytes() == PLAIN_BYTES


def test_plain_a_deleted_txt_comes_back(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    stub_lrclib.body = PLAIN_ONLY
    run_dump(run_cli, stub_lrclib, lib, "dump-a")
    sidecar = track.with_suffix(".txt")
    sidecar.unlink()

    proc = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"plain": 1, "written_txt": 1, "looked_up": 1}
    assert sidecar.read_bytes() == PLAIN_BYTES
    assert len(ledger_rows(tmp_path)) == 1


def test_plain_settled_under_a_dump_id_makes_no_lookup(
    tmp_path: Path, stub_lrclib: StubLrclib
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    sidecar = track.with_suffix(".txt")
    stub_lrclib.body = PLAIN_ONLY

    first, opened = audited_run(tmp_path, stub_lrclib, lib)

    assert first.returncode == 0, first.stderr
    assert nonzero(first.stdout) == {"plain": 1, "written_txt": 1, "looked_up": 1}
    # This shows the hook can see an audio file being opened, which the next run must not do.
    assert sum(path.endswith(".flac") for path in opened) >= 1
    before = snap(track), snap(sidecar)
    requests = len(stub_lrclib.requests)

    second, opened = audited_run(tmp_path, stub_lrclib, lib)

    assert second.returncode == 0, second.stderr
    assert nonzero(second.stdout) == {"plain": 1, "skipped_unchanged": 1}
    assert opened == []
    assert len(stub_lrclib.requests) == requests
    assert (snap(track), snap(sidecar)) == before

    other, _ = audited_run(tmp_path, stub_lrclib, lib, dump_id="dump-b")

    assert other.returncode == 0, other.stderr
    assert nonzero(other.stdout) == {"plain": 1, "looked_up": 1}
    assert len(stub_lrclib.requests) == requests + PLAIN_LOOKUP_REQUESTS
    assert (snap(track), snap(sidecar)) == before


def test_plain_an_owned_txt_with_no_dump_id_is_looked_up_and_kept(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    stub_lrclib.body = PLAIN_ONLY
    run_lib(run_cli, stub_lrclib, lib)
    before = snap(track.with_suffix(".txt"))
    rows = ledger_rows(tmp_path)

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"plain": 1, "looked_up": 1}
    assert snap(track.with_suffix(".txt")) == before
    assert ledger_rows(tmp_path) == rows
    assert b"no dump_id" in proc.stderr


@pytest.mark.parametrize(
    ("args", "env"),
    [(["--plain", "sometimes"], {}), ([], {"PLYRIX_PLAIN": "sometimes"})],
    ids=["flag", "variable"],
)
def test_plain_invalid_setting_exits_2(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    args: list[str],
    env: dict[str, str],
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = run_cli(["run", lib, *args], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, **env})

    assert proc.returncode == 2, proc.stderr
    lines = proc.stderr.splitlines()
    assert len(lines) == 1
    assert b"PLYRIX_PLAIN" in lines[0]
    assert stub_lrclib.requests == []
    assert sidecars_in(lib) == []


@pytest.mark.parametrize(
    "text", ["\x01\x02", "\ufeff", " \x07\n"], ids=["control", "bom", "spaced_control"]
)
def test_plain_text_with_nothing_visible_writes_nothing(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, text: str
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.body = {**PLAIN_ONLY, "plainLyrics": text}

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"no_match": 1, "no_match_not_found": 1, "looked_up": 1}
    assert sidecars_in(lib) == []


# Upgrades. A .txt plyrix wrote goes once a synced version exists, only after the .lrc is marked.

LRC_BYTES = encode(str(RECORD["syncedLyrics"]))


def test_upgrade_replaces_own_txt(tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    audio_before = snap(track)
    txt, lrc = track.with_suffix(".txt"), track.with_suffix(".lrc")
    stub_lrclib.body = PLAIN_ONLY
    run_dump(run_cli, stub_lrclib, lib, "dump-a")
    assert txt.read_bytes() == PLAIN_BYTES
    stub_lrclib.body = RECORD
    requests = len(stub_lrclib.requests)

    same_dump = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert nonzero(same_dump.stdout) == {"plain": 1, "skipped_unchanged": 1}
    assert len(stub_lrclib.requests) == requests
    assert txt.exists()

    new_dump = run_dump(run_cli, stub_lrclib, lib, "dump-b")

    assert new_dump.returncode == 0, new_dump.stderr
    assert nonzero(new_dump.stdout) == {
        "synced": 1,
        "written_lrc": 1,
        "upgraded": 1,
        "looked_up": 1,
    }
    assert lrc.read_bytes() == LRC_BYTES
    assert not txt.exists()
    assert published_paths(tmp_path) == [os.fsencode(lrc)]
    assert query(tmp_path, "SELECT outcome, reason, dump_id FROM tracks") == [
        ("synced", "", "dump-b")
    ]
    assert snap(track) == audio_before
    requests = len(stub_lrclib.requests)

    settled = run_dump(run_cli, stub_lrclib, lib, "dump-b")

    assert nonzero(settled.stdout) == {"synced": 1, "skipped_unchanged": 1}
    assert len(stub_lrclib.requests) == requests


def test_upgrade_with_no_dump_id(tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    stub_lrclib.body = PLAIN_ONLY
    run_lib(run_cli, stub_lrclib, lib)
    stub_lrclib.body = RECORD

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "written_lrc": 1, "upgraded": 1, "looked_up": 1}
    assert sidecars_in(lib) == [track.with_suffix(".lrc")]
    assert published_paths(tmp_path) == [os.fsencode(track.with_suffix(".lrc"))]


def test_upgrade_leaves_an_edited_txt_alone(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    txt = track.with_suffix(".txt")
    stub_lrclib.body = PLAIN_ONLY
    run_lib(run_cli, stub_lrclib, lib)
    txt.write_bytes(FOREIGN)
    before = snap(txt)
    stub_lrclib.body = RECORD

    proc = run_lib(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert snap(txt) == before
    assert track.with_suffix(".lrc").read_bytes() == LRC_BYTES

    again = run_lib(run_cli, stub_lrclib, lib)

    assert nonzero(again.stdout) == {"synced": 1, "skipped_unchanged": 1}
    assert snap(txt) == before
    assert len(published_paths(tmp_path)) == 2


def test_upgrade_leaves_a_foreign_txt_alone(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    txt = track.with_suffix(".txt")
    txt.write_bytes(FOREIGN)
    before = snap(txt)
    stub_lrclib.body = PLAIN_ONLY
    run_dump(run_cli, stub_lrclib, lib, "dump-a")
    stub_lrclib.body = RECORD

    proc = run_dump(run_cli, stub_lrclib, lib, "dump-b")

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert snap(txt) == before
    assert published_paths(tmp_path) == [os.fsencode(track.with_suffix(".lrc"))]


def _no_hard_links_at_all(*_args: Any, **_kwargs: Any) -> None:
    raise OSError(errno.EXDEV, "cross-device")


def _a_publish_that_is_refused(*_args: Any, **_kwargs: Any) -> sidecars.Result:
    return sidecars.Result.EXISTS


def _a_publish_that_fails(*_args: Any, **_kwargs: Any) -> sidecars.Result:
    raise OSError(errno.EIO, "failed")


@pytest.mark.parametrize(
    ("target", "name", "broken", "field"),
    [
        (os, "link", _no_hard_links_at_all, "fs_no_atomic_publish"),
        (sidecars, "publish", _a_publish_that_is_refused, "blocked_foreign"),
        (sidecars, "publish", _a_publish_that_fails, "write_failed"),
    ],
    ids=["exdev", "refused", "failed"],
)
def test_upgrade_failed_publish_keeps_txt(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    monkeypatch: pytest.MonkeyPatch,
    target: object,
    name: str,
    broken: Callable[..., Any],
    field: str,
) -> None:
    track = copy_track(tmp_path / "lib")
    txt = track.with_suffix(".txt")
    stub_lrclib.body = PLAIN_ONLY
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    start = os.fsencode(tmp_path / "lib")
    pipeline.run(settings, start)
    before = snap(txt)
    stub_lrclib.body = RECORD

    with monkeypatch.context() as patch:
        patch.setattr(target, name, broken)
        counts = pipeline.run(settings, start)

    assert getattr(counts, field) == 1
    assert (counts.written_lrc, counts.upgraded) == (0, 0)
    assert snap(txt) == before
    assert not track.with_suffix(".lrc").exists()
    conn = sqlite3.connect(settings.state_db)
    try:
        rows = conn.execute("SELECT status FROM sidecars WHERE kind = 'txt'").fetchall()
    finally:
        conn.close()
    assert rows == [("published",)]

    finished = pipeline.run(settings, start)

    assert (finished.written_lrc, finished.upgraded) == (1, 1)
    assert not txt.exists()


def test_upgrade_a_txt_edited_after_the_check_stays(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = copy_track(tmp_path / "lib")
    txt = track.with_suffix(".txt")
    stub_lrclib.body = PLAIN_ONLY
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    start = os.fsencode(tmp_path / "lib")
    pipeline.run(settings, start)
    stub_lrclib.body = RECORD
    real_publish = sidecars.publish

    def publish_after_an_edit(*args: Any) -> sidecars.Result:
        txt.write_bytes(FOREIGN)
        return real_publish(*args)

    monkeypatch.setattr(sidecars, "publish", publish_after_an_edit)

    counts = pipeline.run(settings, start)

    assert (counts.written_lrc, counts.upgraded) == (1, 0)
    assert txt.read_bytes() == FOREIGN
    assert track.with_suffix(".lrc").read_bytes() == LRC_BYTES


# A sidecar whose audio changed. The .lrc has a stamp at 4.0 s, which fits a 3 s track within the
# tolerance and is beyond the end of a 1 s one, and embedded_token.flac has t.flac's fields at 1 s.
STALE_SYNCED = (
    f"[00:00.50] {CANARY} line 1\n[00:01.00] {CANARY} line 2\n"
    f"[00:01.50] {CANARY} line 3\n[00:04.00] {CANARY} line 4\n"
)
STALE_RECORD = {**RECORD, "syncedLyrics": STALE_SYNCED}
FITTING_SYNCED = (
    f"[00:00.10] {CANARY} new 1\n[00:00.30] {CANARY} new 2\n"
    f"[00:00.50] {CANARY} new 3\n[00:00.90] {CANARY} new 4\n"
)
FITTING_RECORD = {**RECORD, "id": 9, "duration": 1.0, "syncedLyrics": FITTING_SYNCED}
STALE_BYTES = encode(STALE_SYNCED)
FITTING_BYTES = encode(FITTING_SYNCED)


def shorten(track: Path) -> None:
    """Swap the audio for a 1 s file with the same tags, as a re-rip or a re-encode would."""
    shutil.copyfile(FIXTURES / "embedded_token.flac", track)


def stale_lrc(
    tmp_path: Path, stub: StubLrclib, run_cli: RunCli, dump_id: str = "dump-a"
) -> tuple[Path, Path, Path]:
    """Leave plyrix's own .lrc beside a track it no longer fits."""
    lib = tmp_path / "lib"
    track = copy_track(lib)
    stub.body = STALE_RECORD
    assert nonzero(run_dump(run_cli, stub, lib, dump_id).stdout) == {
        "synced": 1,
        "written_lrc": 1,
        "looked_up": 1,
    }
    shorten(track)
    return lib, track, track.with_suffix(".lrc")


def query_db(path: Path, sql: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def stale_rows(tmp_path: Path) -> list[tuple[Any, ...]]:
    return query(tmp_path, "SELECT outcome, reason, dump_id FROM tracks")


def test_stale_passing_revalidation_stays_done(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    run_dump(run_cli, stub_lrclib, lib, "dump-a")
    sidecar = track.with_suffix(".lrc")
    later = track.stat().st_mtime_ns + 10**9
    os.utime(track, ns=(later, later))
    before = snap(sidecar), snap(sidecar.parent)
    requests = len(stub_lrclib.requests)

    proc, opened = audited_run(tmp_path, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1}
    assert os.fsdecode(track) in opened
    assert len(stub_lrclib.requests) == requests
    assert (snap(sidecar), snap(sidecar.parent)) == before
    assert query(tmp_path, "SELECT mtime_ns, outcome, reason FROM tracks") == [
        (later, "synced", "")
    ]

    settled, opened = audited_run(tmp_path, stub_lrclib, lib)

    assert nonzero(settled.stdout) == {"synced": 1, "skipped_unchanged": 1}
    assert opened == []


def test_stale_owned_lrc_is_replaced(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib, track, sidecar = stale_lrc(tmp_path, stub_lrclib, run_cli)
    audio_before = snap(track)
    stub_lrclib.body = FITTING_RECORD

    proc = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {
        "synced": 1,
        "lrc_stale": 1,
        "written_lrc": 1,
        "looked_up": 1,
    }
    assert sidecar.read_bytes() == FITTING_BYTES
    assert snap(track) == audio_before
    assert sorted(path.name for path in sidecar.parent.iterdir()) == ["t.flac", "t.lrc"]
    assert ledger_rows(tmp_path) == [
        (
            "published",
            hashlib.sha256(FITTING_BYTES).hexdigest(),
            len(FITTING_BYTES),
            os.fsencode(sidecar),
        )
    ]
    assert query(tmp_path, "SELECT origin, lrclib_id, refresh_pending FROM sidecars") == [
        ("written", 9, 1)
    ]
    assert stale_rows(tmp_path) == [("synced", "", "dump-a")]
    assert query(tmp_path, "SELECT path FROM temps") == []
    assert leaks(tmp_path, proc, lib) == []
    requests = len(stub_lrclib.requests)

    settled = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert nonzero(settled.stdout) == {"synced": 1, "skipped_unchanged": 1}
    assert len(stub_lrclib.requests) == requests


def test_stale_a_timed_lrc_is_never_replaced(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    sidecar = track.with_suffix(".lrc")
    sidecar.write_bytes(STALE_BYTES)
    assert nonzero(run_dump(run_cli, stub_lrclib, lib, "dump-a").stdout) == {
        "synced": 1,
        "adopted": 1,
    }
    before = snap(sidecar)
    shorten(track)
    stub_lrclib.body = FITTING_RECORD

    proc = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "lrc_stale": 1}
    assert stub_lrclib.requests == []
    assert snap(sidecar) == before
    assert stale_rows(tmp_path) == [("synced", "lrc_stale", "dump-a")]
    assert query(tmp_path, "SELECT origin FROM sidecars") == [("timed",)]

    new_dump, opened = audited_run(tmp_path, stub_lrclib, lib, "dump-b")

    assert nonzero(new_dump.stdout) == {"synced": 1, "lrc_stale": 1, "skipped_unchanged": 1}
    assert opened == []
    assert stub_lrclib.requests == []
    assert snap(sidecar) == before

    # An audio change is the one thing that looks at it again, and that still asks nobody.
    shutil.copyfile(FIXTURES / "t.flac", track)

    fits_again = run_dump(run_cli, stub_lrclib, lib, "dump-b")

    assert nonzero(fits_again.stdout) == {"synced": 1}
    assert stub_lrclib.requests == []
    assert stale_rows(tmp_path) == [("synced", "", "dump-b")]
    assert snap(sidecar) == before


def test_stale_with_no_replacement_is_kept_and_reported(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib, _, sidecar = stale_lrc(tmp_path, stub_lrclib, run_cli)
    before = snap(sidecar)
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND

    proc = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "lrc_stale": 1, "looked_up": 1}
    assert snap(sidecar) == before
    assert stale_rows(tmp_path) == [("synced", "lrc_stale", "dump-a")]
    assert query(tmp_path, "SELECT status, origin FROM sidecars") == [("published", "written")]
    requests = len(stub_lrclib.requests)

    for _ in range(2):
        later, opened = audited_run(tmp_path, stub_lrclib, lib)

        assert nonzero(later.stdout) == {"synced": 1, "lrc_stale": 1, "skipped_unchanged": 1}
        assert opened == []
        assert len(stub_lrclib.requests) == requests
        assert snap(sidecar) == before


def test_stale_a_plain_answer_is_no_replacement(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib, track, sidecar = stale_lrc(tmp_path, stub_lrclib, run_cli)
    before = snap(sidecar)
    stub_lrclib.body = {**PLAIN_ONLY, "duration": 1.0}

    proc = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert nonzero(proc.stdout) == {"synced": 1, "lrc_stale": 1, "looked_up": 1}
    assert snap(sidecar) == before
    assert not track.with_suffix(".txt").exists()


def test_stale_a_new_dump_can_fix_it(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib, _, sidecar = stale_lrc(tmp_path, stub_lrclib, run_cli)
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    run_dump(run_cli, stub_lrclib, lib, "dump-a")
    stub_lrclib.status, stub_lrclib.body = 200, FITTING_RECORD

    proc = run_dump(run_cli, stub_lrclib, lib, "dump-b")

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {
        "synced": 1,
        "lrc_stale": 1,
        "written_lrc": 1,
        "looked_up": 1,
    }
    assert sidecar.read_bytes() == FITTING_BYTES
    assert stale_rows(tmp_path) == [("synced", "", "dump-b")]


def test_stale_a_new_dump_with_no_fit_is_asked_once(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib, _, sidecar = stale_lrc(tmp_path, stub_lrclib, run_cli)
    before = snap(sidecar)
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    run_dump(run_cli, stub_lrclib, lib, "dump-a")
    requests = len(stub_lrclib.requests)

    asked = run_dump(run_cli, stub_lrclib, lib, "dump-b")

    assert nonzero(asked.stdout) == {"synced": 1, "lrc_stale": 1, "looked_up": 1}
    assert len(stub_lrclib.requests) > requests
    assert stale_rows(tmp_path) == [("synced", "lrc_stale", "dump-b")]
    assert snap(sidecar) == before
    requests = len(stub_lrclib.requests)

    settled = run_dump(run_cli, stub_lrclib, lib, "dump-b")

    assert nonzero(settled.stdout) == {"synced": 1, "lrc_stale": 1, "skipped_unchanged": 1}
    assert len(stub_lrclib.requests) == requests


def test_stale_with_no_dump_id_is_checked_each_run(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib, _, sidecar = stale_lrc(tmp_path, stub_lrclib, run_cli, dump_id="")
    before = snap(sidecar)
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND

    for _ in range(2):
        requests = len(stub_lrclib.requests)

        proc = run_lib(run_cli, stub_lrclib, lib)

        assert nonzero(proc.stdout) == {"synced": 1, "lrc_stale": 1, "looked_up": 1}
        assert len(stub_lrclib.requests) > requests
        assert snap(sidecar) == before
    warnings = [line for line in non_notice_lines(proc.stderr) if b"dump_id" in line]
    assert len(warnings) == 1


def test_stale_an_edited_lrc_is_not_replaced(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = copy_track(tmp_path / "lib")
    sidecar = track.with_suffix(".lrc")
    stub_lrclib.body = STALE_RECORD
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    start = os.fsencode(tmp_path / "lib")
    pipeline.run(settings, start)
    shorten(track)
    edited = b"[00:09.00] someone's own late line\n"
    sidecar.write_bytes(edited)
    before = snap(sidecar)
    requests = len(stub_lrclib.requests)
    stub_lrclib.body = FITTING_RECORD

    def never(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("replace_owned was called")

    monkeypatch.setattr(sidecars, "replace_owned", never)

    counts = pipeline.run(settings, start)

    assert (counts.blocked_foreign, counts.lrc_stale, counts.written_lrc) == (1, 0, 0)
    assert snap(sidecar) == before
    assert len(stub_lrclib.requests) == requests


def test_stale_replace_refused_means_refused(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib, _, sidecar = stale_lrc(tmp_path, stub_lrclib, run_cli)
    rival = b"a rival's own words, written during the lookup\n"
    stub_lrclib.body = FITTING_RECORD
    answer = stub_lrclib.next_response

    def rival_edits_then_answers() -> tuple[int, object]:
        sidecar.write_bytes(rival)
        return answer()

    stub_lrclib.next_response = rival_edits_then_answers  # type: ignore[method-assign]

    proc = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"blocked_foreign": 1, "lrc_stale": 1, "looked_up": 1}
    assert sidecar.read_bytes() == rival
    assert ledger_rows(tmp_path) == []
    assert query(tmp_path, "SELECT path FROM temps") == []
    assert [path.name for path in sidecar.parent.glob(".plyrix-*")] == []
    stub_lrclib.next_response = answer  # type: ignore[method-assign]

    later = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert nonzero(later.stdout) == {"blocked_foreign": 1}
    assert sidecar.read_bytes() == rival


def test_stale_unavailable_keeps_everything(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib, _, sidecar = stale_lrc(tmp_path, stub_lrclib, run_cli)
    before = snap(sidecar)
    rows = query(tmp_path, "SELECT * FROM tracks"), ledger_rows(tmp_path)
    stub_lrclib.status, stub_lrclib.body = 503, {}

    proc = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert nonzero(proc.stdout) == {"unavailable": 1, "lrc_stale": 1}
    assert snap(sidecar) == before
    assert (query(tmp_path, "SELECT * FROM tracks"), ledger_rows(tmp_path)) == rows
    stub_lrclib.status, stub_lrclib.body = 200, FITTING_RECORD

    fixed = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert nonzero(fixed.stdout) == {
        "synced": 1,
        "lrc_stale": 1,
        "written_lrc": 1,
        "looked_up": 1,
    }
    assert sidecar.read_bytes() == FITTING_BYTES


def test_stale_check_a_tag_change_on_a_valid_lrc_is_not_rematched(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = copy_track(tmp_path / "lib")
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    start = os.fsencode(tmp_path / "lib")
    pipeline.run(settings, start)
    sidecar = track.with_suffix(".lrc")
    before = snap(sidecar)
    hash_before = query_db(settings.state_db, "SELECT tags_sha256 FROM tracks")
    requests = len(stub_lrclib.requests)
    later = track.stat().st_mtime_ns + 10**9
    os.utime(track, ns=(later, later))
    monkeypatch.setattr(tags, "read", lambda _path: tags_with(title="A Corrected Title"))

    counts = pipeline.run(settings, start)

    assert (counts.synced, counts.lrc_stale, counts.looked_up) == (1, 0, 0)
    assert snap(sidecar) == before
    assert len(stub_lrclib.requests) == requests
    assert query_db(settings.state_db, "SELECT tags_sha256 FROM tracks") != hash_before


def test_stale_check_with_no_usable_duration_leaves_the_file_and_the_row(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = copy_track(tmp_path / "lib")
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    start = os.fsencode(tmp_path / "lib")
    pipeline.run(settings, start)
    sidecar = track.with_suffix(".lrc")
    before = snap(sidecar)
    rows = query_db(settings.state_db, "SELECT mtime_ns, tags_sha256, reason FROM tracks")
    requests = len(stub_lrclib.requests)
    later = track.stat().st_mtime_ns + 10**9
    os.utime(track, ns=(later, later))
    monkeypatch.setattr(tags, "read", lambda _path: tags_with(duration=None))

    counts = pipeline.run(settings, start)

    assert (counts.synced, counts.lrc_stale, counts.looked_up) == (1, 0, 0)
    assert snap(sidecar) == before
    assert len(stub_lrclib.requests) == requests
    assert query_db(settings.state_db, "SELECT mtime_ns, tags_sha256, reason FROM tracks") == rows


def test_stale_check_of_a_corrupted_track_keeps_its_lrc_and_keeps_saying_so(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    run_dump(run_cli, stub_lrclib, lib, "dump-a")
    sidecar = track.with_suffix(".lrc")
    before = snap(sidecar)
    rows = query(tmp_path, "SELECT outcome, reason, size FROM tracks")
    track.write_bytes(b"this is not a flac stream")

    for _ in range(2):
        proc = run_dump(run_cli, stub_lrclib, lib, "dump-a")

        assert nonzero(proc.stdout) == {"unreadable": 1}
        assert snap(sidecar) == before
        assert query(tmp_path, "SELECT outcome, reason, size FROM tracks") == rows


def _a_replace_with_no_atomic_rename(*_args: Any, **_kwargs: Any) -> None:
    raise OSError(errno.EXDEV, "cross-device")


def _a_replace_that_is_refused(*_args: Any, **_kwargs: Any) -> sidecars.Result:
    return sidecars.Result.EXISTS


def _a_replace_that_fails(*_args: Any, **_kwargs: Any) -> sidecars.Result:
    raise OSError(errno.ENOSPC, "no space")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_stale_a_failed_replace_keeps_ownership_through_the_cli(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib, _, sidecar = stale_lrc(tmp_path, stub_lrclib, run_cli)
    before = snap(sidecar)
    stub_lrclib.body = FITTING_RECORD
    # Without write permission the temp can't be made, as on a read-only folder or a full disk.
    sidecar.parent.chmod(0o555)
    try:
        failed = run_dump(run_cli, stub_lrclib, lib, "dump-a")
    finally:
        sidecar.parent.chmod(0o755)

    assert failed.returncode == 3, failed.stderr
    assert parse_counts(failed.stdout)["write_failed"] == 1
    assert b"the next run will try them again" in failed.stderr
    assert snap(sidecar) == before

    retried = run_dump(run_cli, stub_lrclib, lib, "dump-a")

    assert retried.returncode == 0, retried.stderr
    assert nonzero(retried.stdout) == {
        "synced": 1,
        "written_lrc": 1,
        "lrc_stale": 1,
        "looked_up": 1,
    }
    assert sidecar.read_bytes() == encode(FITTING_SYNCED)
    assert query(tmp_path, "SELECT status, origin FROM sidecars") == [("published", "written")]


@pytest.mark.parametrize(
    ("target", "name", "broken", "field", "rows_left", "retried"),
    [
        (
            os,
            "replace",
            _a_replace_with_no_atomic_rename,
            "fs_no_atomic_publish",
            "published",
            True,
        ),
        (sidecars, "replace_owned", _a_replace_that_is_refused, "blocked_foreign", None, False),
        (sidecars, "replace_owned", _a_replace_that_fails, "write_failed", "intent", True),
    ],
    ids=["exdev", "refused", "failed"],
)
def test_stale_a_replace_that_cant_go_ahead_keeps_the_file(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    monkeypatch: pytest.MonkeyPatch,
    target: object,
    name: str,
    broken: Callable[..., Any],
    field: str,
    rows_left: str | None,
    retried: bool,
) -> None:
    track = copy_track(tmp_path / "lib")
    sidecar = track.with_suffix(".lrc")
    stub_lrclib.body = STALE_RECORD
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    start = os.fsencode(tmp_path / "lib")
    pipeline.run(settings, start)
    shorten(track)
    before = snap(sidecar)
    stub_lrclib.body = FITTING_RECORD

    with monkeypatch.context() as patch:
        patch.setattr(target, name, broken)
        counts = pipeline.run(settings, start)

    assert getattr(counts, field) == 1
    assert (counts.lrc_stale, counts.written_lrc, counts.synced) == (1, 0, 0)
    assert snap(sidecar) == before
    left = [] if rows_left is None else [(rows_left,)]
    assert query_db(settings.state_db, "SELECT status FROM sidecars") == left
    assert [path.name for path in sidecar.parent.glob(".plyrix-*")] == []

    later = pipeline.run(settings, start)

    if retried:
        # The file was never reached, so it is still plyrix's, and the replacement goes ahead.
        assert (later.blocked_foreign, later.written_lrc, later.synced) == (0, 1, 1)
        assert sidecar.read_bytes() == encode(FITTING_SYNCED)
        assert query_db(settings.state_db, "SELECT status FROM sidecars") == [("published",)]
    else:
        # A refusal says the file isn't plyrix's any more, so it stays, and so does no row.
        assert later.blocked_foreign == 1
        assert snap(sidecar) == before
        assert query_db(settings.state_db, "SELECT status FROM sidecars") == []
