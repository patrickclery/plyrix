"""The lookup plan through the real pipeline: which requests a track costs, and what they settle."""

import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    CANARY,
    RECORD,
    RunCli,
    StubLrclib,
    copy_track,
    leaks,
    ledger_rows,
    non_notice_lines,
    nonzero,
    settings_for,
    sidecars_in,
    state_db_path,
    tags_with,
)
from plyrix import pipeline, tags
from plyrix.sidecars import encode

NOT_FOUND: tuple[int, object] = (404, {"name": "TrackNotFound"})
REJECTED: tuple[int, object] = (400, {"name": "ValidationError"})
SEARCH_DOWN: tuple[int, object] = (500, {"message": CANARY})
PLAIN_ONLY = {**RECORD, "syncedLyrics": None}
WRONG_ARTIST = {**RECORD, "id": 9, "artistName": "Another Artist"}
SIBLING_LYRICS = "".join(f"[00:0{n}.50] {CANARY} sibling line {n}\n" for n in range(4))
SIBLING = {**RECORD, "id": 8, "syncedLyrics": SIBLING_LYRICS}


def run_dump(run_cli: RunCli, stub: StubLrclib, lib: Path, dump_id: str = "dump-a"):  # type: ignore[no-untyped-def]
    return run_cli(["run", lib, "--dump-id", dump_id], env={"PLYRIX_LRCLIB_URL": stub.base_url})


@pytest.mark.parametrize("lookup", ["get", "search"])
def test_a_200_that_isnt_a_row_is_never_a_lasting_miss(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, lookup: str
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    if lookup == "get":
        stub_lrclib.body = {"error": "invented"}
    else:
        stub_lrclib.status, stub_lrclib.body = NOT_FOUND
        stub_lrclib.search_body = [{"error": "invented"}]

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert proc.returncode == 3, proc.stderr
    assert nonzero(proc.stdout) == {"unavailable": 1}
    assert track_rows(tmp_path) == []

    stub_lrclib.status, stub_lrclib.body = 200, RECORD
    stub_lrclib.search_body = []
    proc = run_dump(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout)["written_lrc"] == 1
    assert track.with_suffix(".lrc").exists()


def test_five_200s_that_arent_rows_stop_the_run(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    for index in range(7):
        copy_track(lib, name=f"{index}.flac")
    stub_lrclib.body = {"error": "invented"}

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert proc.returncode == 1, proc.stderr
    assert nonzero(proc.stdout) == {"unavailable": 5}
    assert track_rows(tmp_path) == []


def test_a_timing_failure_writing_would_reveal_writes_no_lrc(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    stub_lrclib.body = {
        **PLAIN_ONLY,
        "syncedLyrics": "[00:02.00] placeholder a\n\x01[00:01.00] placeholder b\n",
    }

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"plain": 1, "written_txt": 1, "looked_up": 1}
    assert not track.with_suffix(".lrc").exists()


def test_a_row_with_no_title_is_a_doubtful_title_and_not_a_failed_search(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    stub_lrclib.search_body = [{**RECORD, "id": 9, "trackName": None}]

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout)["no_match_doubtful_title"] == 1


def paths(stub: StubLrclib) -> list[str]:
    return [request["path"] for request in stub.requests]


def track_rows(tmp_path: Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(state_db_path(tmp_path))
    try:
        return conn.execute("SELECT outcome, reason, lrclib_id FROM tracks").fetchall()
    finally:
        conn.close()


def run_in_process(
    tmp_path: Path,
    stub: StubLrclib,
    monkeypatch: pytest.MonkeyPatch,
    **fields: object,
) -> pipeline.Counts:
    """Run over one track whose tags have these fields changed."""
    lib = tmp_path / "lib"
    copy_track(lib)
    monkeypatch.setattr(tags, "read", lambda _path: tags_with(**fields))
    return pipeline.run(settings_for(tmp_path, stub, monkeypatch), os.fsencode(lib))


def test_lookup_first_answer_settles_in_one_request(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert paths(stub_lrclib) == ["/api/get"]
    assert track.with_suffix(".lrc").read_bytes() == encode(str(RECORD["syncedLyrics"]))


def test_lookup_a_plain_row_cannot_hide_a_synced_sibling(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    stub_lrclib.body = PLAIN_ONLY
    stub_lrclib.search_body = [SIBLING]

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert track.with_suffix(".lrc").read_bytes() == encode(SIBLING_LYRICS)
    assert paths(stub_lrclib) == ["/api/get", "/api/get", "/api/search"]
    assert track_rows(tmp_path) == [("synced", "", 8)]
    assert leaks(tmp_path, proc, lib) == []


@pytest.mark.parametrize(
    "first",
    [{**RECORD, "instrumental": True, "syncedLyrics": None, "plainLyrics": None}, WRONG_ARTIST],
    ids=["instrumental", "wrong_row"],
)
def test_lookup_other_answers_cannot_hide_a_synced_sibling(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, first: dict[str, object]
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    stub_lrclib.body = first
    stub_lrclib.search_body = [SIBLING]

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").read_bytes() == encode(SIBLING_LYRICS)


def test_lookup_sends_each_step_its_own_form(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND

    counts = run_in_process(
        tmp_path,
        stub_lrclib,
        monkeypatch,
        artist="Fixture Artist feat. Guest",
        title="Fixture Title (Remastered 2011)",
    )

    assert counts.no_match_not_found == 1
    assert [
        (r["path"], r["artist_name"], r["track_name"], r.get("album_name"), r.get("duration"))
        for r in stub_lrclib.requests
    ] == [
        (
            "/api/get",
            "Fixture Artist feat. Guest",
            "Fixture Title (Remastered 2011)",
            "Fixture Album",
            "3",
        ),
        ("/api/get", "Fixture Artist feat. Guest", "Fixture Title (Remastered 2011)", None, "3"),
        ("/api/get", "Fixture Artist", "Fixture Title", None, "3"),
        ("/api/search", "Fixture Artist", "Fixture Title", None, None),
    ]


def test_lookup_stops_at_the_step_that_finds_a_synced_row(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_lrclib.queue = [NOT_FOUND, NOT_FOUND]

    counts = run_in_process(
        tmp_path, stub_lrclib, monkeypatch, title="Fixture Title (Remastered 2011)"
    )

    assert counts.written_lrc == 1
    assert paths(stub_lrclib) == ["/api/get"] * 3


@pytest.mark.parametrize("album", [None, "", "   "], ids=["none", "empty", "blank"])
def test_lookup_no_album_makes_one_get_and_no_second_search(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch, album: str | None
) -> None:
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    stub_lrclib.search_body = [WRONG_ARTIST] * 20

    counts = run_in_process(tmp_path, stub_lrclib, monkeypatch, album=album)

    assert counts.no_match_doubtful_artist == 1
    assert paths(stub_lrclib) == ["/api/get", "/api/search"]
    assert all("album_name" not in request for request in stub_lrclib.requests)


def test_lookup_ranks_search_results(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    stub_lrclib.search_body = [
        {**RECORD, "id": 1, "trackName": "Fixture Title (Live)"},
        {**RECORD, "id": 2, "duration": 60.0},
        SIBLING,
    ]

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").read_bytes() == encode(SIBLING_LYRICS)
    assert track_rows(tmp_path) == [("synced", "", 8)]


def test_lookup_ranks_every_answer_together(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    """A plain row from /get and one from /search are one choice, so the same album still wins."""
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.body = {**PLAIN_ONLY, "id": 20, "albumName": "Another Album"}
    stub_lrclib.search_body = [{**PLAIN_ONLY, "id": 30}]

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert track_rows(tmp_path) == [("plain", "", 30)]


@pytest.mark.parametrize(("rows", "searches"), [(20, 2), (19, 1)], ids=["full_page", "short_page"])
def test_lookup_a_full_page_narrows_by_album(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, rows: int, searches: int
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    stub_lrclib.search_queue = [(200, [WRONG_ARTIST] * rows), (200, [SIBLING])]

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert paths(stub_lrclib) == ["/api/get", "/api/get"] + ["/api/search"] * searches
    assert "album_name" not in stub_lrclib.requests[2]
    if searches == 2:
        assert stub_lrclib.requests[3]["album_name"] == "Fixture Album"
        assert track.with_suffix(".lrc").read_bytes() == encode(SIBLING_LYRICS)
    else:
        assert nonzero(proc.stdout) == {
            "no_match": 1,
            "no_match_doubtful_artist": 1,
            "looked_up": 1,
        }


def test_lookup_a_full_page_holding_a_synced_row_stops_there(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    stub_lrclib.search_body = [*[WRONG_ARTIST] * 19, SIBLING]

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert nonzero(proc.stdout) == {"synced": 1, "written_lrc": 1, "looked_up": 1}
    assert paths(stub_lrclib) == ["/api/get", "/api/get", "/api/search"]


def test_lookup_a_miss_makes_two_gets_and_one_search(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert paths(stub_lrclib) == ["/api/get", "/api/get", "/api/search"]
    assert nonzero(proc.stdout) == {"no_match": 1, "no_match_not_found": 1, "looked_up": 1}
    assert track_rows(tmp_path) == [("no_match", "not_found", None)]

    rerun = run_dump(run_cli, stub_lrclib, lib)

    assert len(stub_lrclib.requests) == 3
    assert nonzero(rerun.stdout) == {
        "no_match": 1,
        "no_match_not_found": 1,
        "skipped_unchanged": 1,
    }


def test_lookup_a_miss_records_the_row_that_gave_its_reason(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.body = WRONG_ARTIST
    stub_lrclib.search_body = [
        {**RECORD, "id": 41, "artistName": "Yet Another Artist"},
        {**RECORD, "id": 42, "trackName": "Fixture Title (Live)"},
    ]

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert nonzero(proc.stdout) == {"no_match": 1, "no_match_doubtful_version": 1, "looked_up": 1}
    assert track_rows(tmp_path) == [("no_match", "doubtful_version", 42)]
    assert sidecars_in(lib) == []


def test_lookup_a_rejected_step_continues_to_the_next(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    stub_lrclib.queue = [REJECTED, NOT_FOUND]
    stub_lrclib.search_body = [SIBLING]

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert paths(stub_lrclib) == ["/api/get", "/api/get", "/api/search"]
    assert track.with_suffix(".lrc").read_bytes() == encode(SIBLING_LYRICS)


def test_lookup_rejected_with_no_candidate_is_a_settled_miss(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.status, stub_lrclib.body = REJECTED

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc.stdout) == {"no_match": 1, "no_match_rejected": 1, "looked_up": 1}
    assert track_rows(tmp_path) == [("no_match", "rejected", None)]
    assert len(stub_lrclib.requests) == 3

    rerun = run_dump(run_cli, stub_lrclib, lib)

    assert len(stub_lrclib.requests) == 3
    assert nonzero(rerun.stdout) == {
        "no_match": 1,
        "no_match_rejected": 1,
        "skipped_unchanged": 1,
    }


def test_lookup_rejected_with_a_candidate_seen_names_the_candidate(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.queue = [REJECTED, NOT_FOUND]
    stub_lrclib.search_body = [WRONG_ARTIST]

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert nonzero(proc.stdout) == {"no_match": 1, "no_match_doubtful_artist": 1, "looked_up": 1}
    assert track_rows(tmp_path) == [("no_match", "doubtful_artist", 9)]


def test_lookup_a_tag_that_cant_be_sent_is_rejected_at_every_step(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = run_in_process(tmp_path, stub_lrclib, monkeypatch, title="Fixture \ud800 Title")

    assert counts.no_match_rejected == 1
    assert counts.unavailable == 0
    assert stub_lrclib.requests == []


@pytest.mark.parametrize(
    ("queue", "search_queue", "asked"),
    [
        ([(200, PLAIN_ONLY), (500, {"message": CANARY})], [], ["/api/get", "/api/get"]),
        ([(200, PLAIN_ONLY), (200, PLAIN_ONLY)], [(500, {"message": CANARY})], None),
        (
            [(200, PLAIN_ONLY), (200, PLAIN_ONLY)],
            [(200, [WRONG_ARTIST] * 20), (500, {"message": CANARY})],
            None,
        ),
    ],
    ids=["second_get", "search", "second_search"],
)
def test_lookup_any_failed_step_discards_what_it_had(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    run_cli: RunCli,
    queue: list[tuple[int, object]],
    search_queue: list[tuple[int, object]],
    asked: list[str] | None,
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.queue = list(queue)
    stub_lrclib.search_queue = list(search_queue)

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert proc.returncode == 3, proc.stderr
    assert nonzero(proc.stdout) == {"unavailable": 1}
    assert track_rows(tmp_path) == []
    assert sidecars_in(lib) == []
    if asked is not None:
        assert paths(stub_lrclib) == asked
    assert leaks(tmp_path, proc, lib) == []


def test_lookup_rebuilding_search_records_no_miss(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    for index in range(7):
        copy_track(lib, name=f"{index}.flac")
    stub_lrclib.body = PLAIN_ONLY
    stub_lrclib.search_status, stub_lrclib.search_body = SEARCH_DOWN

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert proc.returncode == 1, proc.stderr
    assert nonzero(proc.stdout) == {"unavailable": 5}
    assert len(track_rows(tmp_path)) == 0
    assert sidecars_in(lib) == []
    assert paths(stub_lrclib) == ["/api/get", "/api/get", "/api/search"] * 5
    lines = non_notice_lines(proc.stderr)
    assert len(lines) == 1
    assert b"not recorded" in lines[0]
    assert leaks(tmp_path, proc, lib) == []


def test_lookup_plain_text_with_a_failed_search_is_looked_up_again(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    stub_lrclib.body = PLAIN_ONLY
    stub_lrclib.search_status, stub_lrclib.search_body = SEARCH_DOWN

    first = run_dump(run_cli, stub_lrclib, lib)

    assert first.returncode == 3, first.stderr
    assert nonzero(first.stdout) == {"unavailable": 1}
    assert track_rows(tmp_path) == []

    stub_lrclib.search_status, stub_lrclib.search_body = 200, []
    second = run_dump(run_cli, stub_lrclib, lib)

    assert second.returncode == 0, second.stderr
    assert nonzero(second.stdout) == {"plain": 1, "written_txt": 1, "looked_up": 1}
    assert track_rows(tmp_path) == [("plain", "", 7)]
    assert len(stub_lrclib.requests) == 6


def test_lookup_get_only_tracks_do_not_reset_the_breaker(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    for index in range(10):
        copy_track(lib, name=f"{index}.flac")
    failing: list[tuple[int, object]] = [(200, PLAIN_ONLY), (200, PLAIN_ONLY)]
    stub_lrclib.queue = [*failing, (200, RECORD)] * 4 + failing
    stub_lrclib.search_status, stub_lrclib.search_body = SEARCH_DOWN

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert proc.returncode == 1, proc.stderr
    assert nonzero(proc.stdout) == {
        "synced": 4,
        "unavailable": 5,
        "written_lrc": 4,
        "looked_up": 4,
    }
    assert len(stub_lrclib.requests) == 5 * 3 + 4
    assert len(ledger_rows(tmp_path)) == 4


def test_lookup_skipped_tracks_do_not_reset_the_breaker(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    for index in (1, 3, 5, 7):
        copy_track(lib, name=f"{index}.flac")
    assert run_dump(run_cli, stub_lrclib, lib).returncode == 0
    for index in (0, 2, 4, 6, 8, 9):
        copy_track(lib, name=f"{index}.flac")
    stub_lrclib.body = PLAIN_ONLY
    stub_lrclib.search_status, stub_lrclib.search_body = SEARCH_DOWN

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert proc.returncode == 1, proc.stderr
    assert nonzero(proc.stdout) == {"synced": 4, "unavailable": 5, "skipped_unchanged": 4}


def test_lookup_a_good_search_resets_the_breaker(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    for index in range(9):
        copy_track(lib, name=f"{index}.flac")
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    stub_lrclib.search_queue = [*[SEARCH_DOWN] * 4, (200, []), *[SEARCH_DOWN] * 4]

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert proc.returncode == 3, proc.stderr
    assert nonzero(proc.stdout) == {
        "unavailable": 8,
        "no_match": 1,
        "no_match_not_found": 1,
        "looked_up": 1,
    }
    assert track_rows(tmp_path) == [("no_match", "not_found", None)]


def test_lookup_a_good_search_then_a_failed_one_does_not_reset_the_breaker(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    for index in range(7):
        copy_track(lib, name=f"{index}.flac")
    stub_lrclib.status, stub_lrclib.body = NOT_FOUND
    stub_lrclib.search_queue = [(200, [WRONG_ARTIST] * 20), SEARCH_DOWN] * 7

    proc = run_dump(run_cli, stub_lrclib, lib)

    assert proc.returncode == 1, proc.stderr
    assert nonzero(proc.stdout) == {"unavailable": 5}
    assert track_rows(tmp_path) == []
