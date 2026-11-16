"""plex-refresh through the real console script, against a stub LRCLIB and a stub Plex server."""

import dataclasses
import hashlib
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import responses

from conftest import (
    RunCli,
    StubLrclib,
    StubPlex,
    cli_env,
    copy_track,
    parse_counts,
    plex_listing,
    plex_section_dir,
    plex_track,
    queue_sidecars,
    refresh_flags,
    run_lib,
    start_cli,
    state_db_path,
    tripwired_plex_refresh,
)
from plyrix import cli, plex, refresh
from plyrix.config import Settings
from plyrix.state import db

DUMMY_TOKEN = "plyrix-dummy-token-0123456789"
SECTION = "1"


def plex_env(lib: Path, lrclib: StubLrclib, stub: StubPlex) -> dict[str, str]:
    return {
        "PLYRIX_MUSIC_ROOT": str(lib),
        "PLYRIX_LRCLIB_URL": lrclib.base_url,
        "PLYRIX_PLEX_URL": stub.base_url,
        "PLYRIX_PLEX_SECTION": SECTION,
        "PLYRIX_PLEX_TOKEN": DUMMY_TOKEN,
        "PLYRIX_PLEX_PACE": "0",
    }


def pending_flags(tmp_path: Path) -> list[int]:
    with sqlite3.connect(state_db_path(tmp_path)) as conn:
        return [row[0] for row in conn.execute("SELECT refresh_pending FROM sidecars")]


def published_one(tmp_path: Path, run_cli: RunCli, lrclib: StubLrclib, stub: StubPlex) -> Path:
    """Run over one copied track, and have Plex list it under album 101. Return the library."""
    lib = tmp_path / "lib"
    track = copy_track(lib)
    proc = run_lib(run_cli, lrclib, lib)
    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").exists()
    assert pending_flags(tmp_path) == [1]
    real = os.fsencode(os.path.realpath(track))
    stub.location = os.path.realpath(lib)
    stub.tracks = [plex_track("201", "101", real)]
    return lib


def test_tracer_one_folder_refreshes_and_clears(
    tmp_path: Path, stub_lrclib: StubLrclib, stub_plex: StubPlex, run_cli: RunCli
) -> None:
    lib = published_one(tmp_path, run_cli, stub_lrclib, stub_plex)

    proc = run_cli(["plex-refresh"], env=plex_env(lib, stub_lrclib, stub_plex))

    assert proc.returncode == 0, proc.stderr
    listings = [r for r in stub_plex.requests if r["path"] == f"/library/sections/{SECTION}/all"]
    assert listings
    assert all(r["method"] == "GET" and r["query"] == {"type": "10"} for r in listings)
    for listing in listings:
        headers = listing["headers"]
        assert isinstance(headers, dict)
        assert "x-plex-container-start" in headers
        assert "x-plex-container-size" in headers
    puts = stub_plex.puts()
    assert len(puts) == 1
    assert puts[0]["path"] == "/library/metadata/101/refresh"
    headers = puts[0]["headers"]
    assert isinstance(headers, dict)
    assert headers["x-plex-token"] == DUMMY_TOKEN
    assert pending_flags(tmp_path) == [0]
    counts = parse_counts(proc.stdout)
    assert counts["refreshed"] == 1
    assert counts["cleared_folders"] == 1
    assert counts["cleared_sidecars"] == 1


def test_tracer_a_second_drain_sends_nothing(
    tmp_path: Path, stub_lrclib: StubLrclib, stub_plex: StubPlex, run_cli: RunCli
) -> None:
    lib = published_one(tmp_path, run_cli, stub_lrclib, stub_plex)
    env = plex_env(lib, stub_lrclib, stub_plex)
    first = run_cli(["plex-refresh"], env=env)
    assert first.returncode == 0, first.stderr

    second = run_cli(["plex-refresh"], env=env)

    assert second.returncode == 0, second.stderr
    assert len(stub_plex.puts()) == 1
    assert parse_counts(second.stdout)["refreshed"] == 0


def test_tracer_the_token_is_only_a_header(
    tmp_path: Path, stub_lrclib: StubLrclib, stub_plex: StubPlex, run_cli: RunCli
) -> None:
    lib = published_one(tmp_path, run_cli, stub_lrclib, stub_plex)

    proc = run_cli(["plex-refresh"], env=plex_env(lib, stub_lrclib, stub_plex))

    assert proc.returncode == 0, proc.stderr
    assert stub_plex.requests
    for request in stub_plex.requests:
        assert DUMMY_TOKEN not in str(request["path"])
        assert DUMMY_TOKEN not in str(request["query"])
    assert DUMMY_TOKEN.encode() not in proc.stdout
    assert DUMMY_TOKEN.encode() not in proc.stderr


def test_tracer_no_plex_url_is_nothing_to_refresh(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    env = {"PLYRIX_MUSIC_ROOT": str(lib), "PLYRIX_LRCLIB_URL": stub_lrclib.base_url}

    proc = run_cli(["plex-refresh"], env=env)

    assert proc.returncode == 0, proc.stderr
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert b"plex_url (PLYRIX_PLEX_URL)" in lines[0]
    assert not state_db_path(tmp_path).exists()


def test_the_real_command_resolves_only_the_stub(
    tmp_path: Path, stub_lrclib: StubLrclib, stub_plex: StubPlex, run_cli: RunCli
) -> None:
    lib = published_one(tmp_path, run_cli, stub_lrclib, stub_plex)

    proc, hosts = tripwired_plex_refresh(tmp_path, plex_env(lib, stub_lrclib, stub_plex))

    assert proc.returncode == 0, proc.stderr
    assert len(stub_plex.puts()) == 1
    assert set(hosts) == {"127.0.0.1"}


@pytest.mark.parametrize("where", ["variable", "secret file"])
def test_an_empty_plex_token_sends_no_token_header(
    tmp_path: Path, stub_lrclib: StubLrclib, stub_plex: StubPlex, run_cli: RunCli, where: str
) -> None:
    lib = published_one(tmp_path, run_cli, stub_lrclib, stub_plex)
    env = plex_env(lib, stub_lrclib, stub_plex)
    if where == "variable":
        env["PLYRIX_PLEX_TOKEN"] = ""
    else:
        del env["PLYRIX_PLEX_TOKEN"]
        credentials = tmp_path / "credentials"
        credentials.mkdir()
        (credentials / "plyrix_plex_token").write_text("")
        env["CREDENTIALS_DIRECTORY"] = str(credentials)

    proc = run_cli(["plex-refresh"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert stub_plex.requests
    for request in stub_plex.requests:
        headers = request["headers"]
        assert isinstance(headers, dict)
        assert "x-plex-token" not in headers


# The drain in this process, against responses, with a real ledger and a fake sleep.

BASE = "http://pms.test:32400"
SECTION_ID = 3
LISTING = f"{BASE}/library/sections/{SECTION_ID}/all"
DUMMY_LRCLIB = "http://127.0.0.1:9/api"


@pytest.fixture
def pms() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock() as mock:
        yield mock


class Library:
    """A library folder, a ledger beside it, and what Plex lists, all invented."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        self.root = tmp_path / "lib"
        self.root.mkdir()
        self.real = os.fsencode(os.path.realpath(self.root))
        self.ledger = tmp_path / "state" / "state.db"
        self.tracks: list[dict[str, object]] = []
        self.sleeps: list[float] = []

    def sidecar(self, folder: bytes, name: bytes = b"t.lrc") -> bytes:
        return self.real + b"/" + folder + b"/" + name

    def queue(self, *paths: bytes) -> None:
        queue_sidecars(self.ledger, list(paths))

    def plex_lists(
        self, album: str, folder: bytes, plex_root: bytes | None = None, name: bytes = b"t.flac"
    ) -> None:
        file = (plex_root or self.real) + b"/" + folder + b"/" + name
        self.tracks.append(plex_track(str(1000 + len(self.tracks)), album, file))

    def settings(self, **fields: object) -> Settings:
        values: dict[str, object] = {
            "music_root": self.root,
            "lrclib_url": DUMMY_LRCLIB,
            "state_db": self.ledger,
            "plex_url": BASE,
            "plex_section": SECTION_ID,
            "plex_pace": 0.25,
        }
        return Settings(**{**values, **fields})  # type: ignore[arg-type]

    def drain(
        self, pms: responses.RequestsMock, **kwargs: object
    ) -> tuple[plex.Reply, refresh.RefreshCounts]:
        pms.add(responses.GET, LISTING, body=plex_listing(self.tracks))
        settings = kwargs.pop("settings", None) or self.settings()
        conn = db.connect(self.ledger)
        try:
            return refresh.drain(
                conn,
                plex.new_session("plyrix-dummy-token"),
                settings,  # type: ignore[arg-type]
                sleep=self.sleeps.append,
                **kwargs,  # type: ignore[arg-type]
            )
        finally:
            conn.close()

    def pending(self) -> dict[bytes, int]:
        return refresh_flags(self.ledger)


def put_keys(pms: responses.RequestsMock) -> list[str]:
    keys = []
    for call in pms.calls:
        if call.request.method == "PUT":
            url = str(call.request.url)
            keys.append(url.removeprefix(f"{BASE}/library/metadata/").removesuffix("/refresh"))
    return keys


def ok_refreshes(pms: responses.RequestsMock, *albums: str, status: int = 200) -> None:
    for album in albums:
        pms.add(responses.PUT, f"{BASE}/library/metadata/{album}/refresh", status=status)


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Library:
    return Library(tmp_path, monkeypatch)


def test_multi_album_folder_is_cleared_once_both_albums_are_accepted(
    library: Library, pms: responses.RequestsMock
) -> None:
    a, b = library.sidecar(b"Split", b"a.lrc"), library.sidecar(b"Split", b"b.lrc")
    library.queue(a, b)
    library.plex_lists("11", b"Split")
    library.plex_lists("12", b"Split")
    ok_refreshes(pms, "11", "12")

    reply, counts = library.drain(pms)

    assert reply is plex.Reply.OK
    assert put_keys(pms) == ["11", "12"]
    assert library.pending() == {a: 0, b: 0}
    assert (counts.refreshed, counts.cleared_folders, counts.cleared_sidecars) == (2, 1, 2)
    assert counts.albums_to_refresh == 2


def test_multi_album_folder_with_one_failed_album_stays_pending(
    library: Library, pms: responses.RequestsMock
) -> None:
    path = library.sidecar(b"Split")
    library.queue(path)
    library.plex_lists("11", b"Split")
    library.plex_lists("12", b"Split")
    ok_refreshes(pms, "11")
    ok_refreshes(pms, "12", status=500)

    _, counts = library.drain(pms)

    assert put_keys(pms) == ["11", "12"]
    assert library.pending() == {path: 1}
    assert (counts.refreshed, counts.refresh_failed, counts.cleared_folders) == (1, 1, 0)


def test_an_album_spanning_two_folders_is_sent_once_and_clears_both(
    library: Library, pms: responses.RequestsMock
) -> None:
    x, y = library.sidecar(b"Disc 1"), library.sidecar(b"Disc 2")
    library.queue(x, y)
    library.plex_lists("21", b"Disc 1")
    library.plex_lists("21", b"Disc 2")
    ok_refreshes(pms, "21")

    _, counts = library.drain(pms)

    assert put_keys(pms) == ["21"]
    assert library.pending() == {x: 0, y: 0}
    assert (counts.refreshed, counts.cleared_folders, counts.albums_to_refresh) == (1, 2, 1)


def test_a_folder_plex_does_not_list_counts_not_in_plex_and_stays_pending(
    library: Library, pms: responses.RequestsMock
) -> None:
    listed, unlisted = library.sidecar(b"Listed"), library.sidecar(b"New")
    library.queue(listed, unlisted)
    library.plex_lists("31", b"Listed")
    ok_refreshes(pms, "31")

    reply, counts = library.drain(pms)

    assert reply is plex.Reply.OK
    assert library.pending() == {listed: 0, unlisted: 1}
    assert (counts.not_in_plex, counts.queued_folders, counts.cleared_folders) == (1, 2, 1)


def test_a_row_outside_root_counts_outside_root_and_stays_pending(
    library: Library, pms: responses.RequestsMock, tmp_path: Path
) -> None:
    elsewhere = os.fsencode(os.path.realpath(tmp_path)) + b"/elsewhere/t.lrc"
    # A sibling folder whose name only starts with the root's.
    sibling = library.real + b"-other/A/t.lrc"
    library.queue(elsewhere, sibling)

    _, counts = library.drain(pms)

    assert put_keys(pms) == []
    assert library.pending() == {elsewhere: 1, sibling: 1}
    assert counts.outside_root == 2


def test_plex_view_maps_the_music_root_to_plex_music_root(
    library: Library, pms: responses.RequestsMock
) -> None:
    path = library.sidecar(b"A")
    library.queue(path)
    library.plex_lists("41", b"A", plex_root=b"/plexview")
    ok_refreshes(pms, "41")

    _, counts = library.drain(pms, settings=library.settings(plex_music_root="/plexview"))

    assert put_keys(pms) == ["41"]
    assert library.pending() == {path: 0}
    assert counts.cleared_folders == 1


@pytest.mark.parametrize(
    ("folder", "root", "plex_root", "expected"),
    [
        (b"/srv/m/A", b"/srv/m", b"/plexview", b"/plexview/A"),
        (b"/srv/m/A/B", b"/srv/m/", b"/plexview/", b"/plexview/A/B"),
        (b"/srv/m", b"/srv/m", b"/plexview", b"/plexview"),
        (b"/srv/music/A", b"/srv/m", b"/plexview", None),
        (b"/A\xff", b"/", b"/data", b"/data/A\xff"),
        (b"/srv/m/A", b"/srv/m", b"/", b"/A"),
    ],
)
def test_plex_view_on_bytes(
    folder: bytes, root: bytes, plex_root: bytes, expected: bytes | None
) -> None:
    assert refresh.plex_view(folder, root, plex_root) == expected


def test_plex_view_sleeps_plex_pace_once_per_put(
    library: Library, pms: responses.RequestsMock
) -> None:
    library.queue(library.sidecar(b"A"), library.sidecar(b"B"))
    library.plex_lists("51", b"A")
    library.plex_lists("52", b"B")
    ok_refreshes(pms, "51")
    ok_refreshes(pms, "52", status=500)

    library.drain(pms)

    assert len(put_keys(pms)) == 2
    assert library.sleeps == [0.25, 0.25]


def test_limit_sends_at_most_that_many_refreshes(
    library: Library, pms: responses.RequestsMock
) -> None:
    paths = [library.sidecar(name) for name in (b"A", b"B", b"C")]
    library.queue(*paths)
    for album, name in (("61", b"A"), ("62", b"B"), ("63", b"C")):
        library.plex_lists(album, name)
    ok_refreshes(pms, "61", "62")

    _, counts = library.drain(pms, limit=2)

    assert put_keys(pms) == ["61", "62"]
    assert library.pending() == dict(zip(paths, [0, 0, 1], strict=True))
    assert (counts.cleared_folders, counts.left_by_limit, counts.albums_to_refresh) == (2, 1, 2)


def test_limit_leaves_a_sidecar_whose_albums_do_not_all_fit(
    library: Library, pms: responses.RequestsMock
) -> None:
    # Two of Plex's tracks share B/t, so its sidecar needs both albums, and only one fits.
    one, two = library.sidecar(b"A"), library.sidecar(b"B")
    library.queue(one, two)
    library.plex_lists("71", b"A")
    library.plex_lists("72", b"B")
    library.plex_lists("73", b"B")
    ok_refreshes(pms, "71")

    _, counts = library.drain(pms, limit=2)

    assert put_keys(pms) == ["71"]
    assert library.pending() == {one: 0, two: 1}
    assert (counts.refreshed, counts.left_by_limit, counts.albums_to_refresh) == (1, 1, 1)


def test_limit_drains_a_folder_holding_more_albums_than_the_limit(
    library: Library, pms: responses.RequestsMock
) -> None:
    # A flat folder where each track has its own album: each sidecar needs only its own.
    paths = [library.sidecar(b"Singles", stem + b".lrc") for stem in (b"a", b"b", b"c")]
    library.queue(*paths)
    for album, stem in (("1", b"a"), ("2", b"b"), ("3", b"c")):
        library.plex_lists(album, b"Singles", name=stem + b".flac")
    ok_refreshes(pms, "1", "2", "3")

    _, first = library.drain(pms, limit=2)

    assert put_keys(pms) == ["1", "2"]
    assert library.pending() == dict(zip(paths, [0, 0, 1], strict=True))
    assert (first.cleared_sidecars, first.cleared_folders, first.left_by_limit) == (2, 0, 1)

    _, second = library.drain(pms, limit=2)

    assert put_keys(pms) == ["1", "2", "3"]
    assert set(library.pending().values()) == {0}
    assert (second.cleared_sidecars, second.cleared_folders, second.left_by_limit) == (1, 1, 0)


def test_limit_skips_a_sidecar_too_big_for_it_and_reaches_later_folders(
    library: Library, pms: responses.RequestsMock
) -> None:
    # A's sidecar has no track of its own in Plex, so it needs all three of A's albums.
    big, later = library.sidecar(b"A", b"gone.lrc"), library.sidecar(b"B")
    library.queue(big, later)
    for album in ("1", "2", "3"):
        library.plex_lists(album, b"A")
    library.plex_lists("4", b"B")
    ok_refreshes(pms, "4")

    _, counts = library.drain(pms, limit=2)

    assert put_keys(pms) == ["4"]
    assert library.pending() == {big: 1, later: 0}
    assert (counts.left_by_limit, counts.cleared_folders) == (1, 1)


def test_a_sidecar_whose_own_album_was_accepted_clears_though_the_folder_has_another(
    library: Library, pms: responses.RequestsMock
) -> None:
    good, bad = library.sidecar(b"Split", b"a.lrc"), library.sidecar(b"Split", b"b.lrc")
    library.queue(good, bad)
    library.plex_lists("11", b"Split", name=b"a.flac")
    library.plex_lists("12", b"Split", name=b"b.flac")
    ok_refreshes(pms, "11")
    ok_refreshes(pms, "12", status=500)

    _, counts = library.drain(pms)

    assert library.pending() == {good: 0, bad: 1}
    assert (counts.cleared_sidecars, counts.cleared_folders, counts.refresh_failed) == (1, 0, 1)


def test_limit_does_not_charge_an_album_already_accepted(
    library: Library, pms: responses.RequestsMock
) -> None:
    paths = [library.sidecar(name) for name in (b"A", b"B", b"C")]
    library.queue(*paths)
    library.plex_lists("81", b"A")
    library.plex_lists("81", b"B")
    library.plex_lists("82", b"C")
    ok_refreshes(pms, "81")

    _, counts = library.drain(pms, limit=1)

    assert put_keys(pms) == ["81"]
    assert library.pending() == dict(zip(paths, [0, 0, 1], strict=True))
    assert (counts.cleared_folders, counts.left_by_limit) == (2, 1)


def test_limit_counts_a_failed_folder_as_failed_not_left(
    library: Library, pms: responses.RequestsMock
) -> None:
    library.queue(library.sidecar(b"A"), library.sidecar(b"B"))
    library.plex_lists("91", b"A")
    library.plex_lists("92", b"B")
    ok_refreshes(pms, "91", status=500)

    _, counts = library.drain(pms, limit=1)

    assert put_keys(pms) == ["91"]
    assert (counts.refresh_failed, counts.left_by_limit) == (1, 1)


def test_dry_run_sends_nothing_and_clears_nothing(
    library: Library, pms: responses.RequestsMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = [library.sidecar(name) for name in (b"A", b"B", b"C")]
    library.queue(*paths)
    library.plex_lists("101", b"A")
    library.plex_lists("101", b"B")
    library.plex_lists("102", b"C")
    library.plex_lists("103", b"C")
    cleared: list[object] = []
    monkeypatch.setattr(db, "clear_refresh", lambda *args: cleared.append(args))

    reply, counts = library.drain(pms, dry_run=True)

    assert reply is plex.Reply.OK
    assert put_keys(pms) == []
    assert cleared == []
    assert library.sleeps == []
    assert set(library.pending().values()) == {1}
    assert counts.albums_to_refresh == 3
    assert (counts.refreshed, counts.cleared_folders, counts.left_by_limit) == (0, 0, 0)


def test_dry_run_with_a_limit_counts_what_a_real_drain_would_send(
    library: Library, pms: responses.RequestsMock
) -> None:
    library.queue(*(library.sidecar(name) for name in (b"A", b"B", b"C")))
    for album, name in (("111", b"A"), ("112", b"B"), ("113", b"C")):
        library.plex_lists(album, name)

    _, counts = library.drain(pms, dry_run=True, limit=1)

    assert put_keys(pms) == []
    assert (counts.albums_to_refresh, counts.left_by_limit) == (1, 2)


def test_a_refused_refresh_stops_the_drain(library: Library, pms: responses.RequestsMock) -> None:
    paths = [library.sidecar(name) for name in (b"A", b"B", b"C")]
    library.queue(*paths)
    for album, name in (("121", b"A"), ("122", b"B"), ("123", b"C")):
        library.plex_lists(album, name)
    ok_refreshes(pms, "121")
    ok_refreshes(pms, "122", status=401)

    reply, counts = library.drain(pms)

    assert reply is plex.Reply.REFUSED
    assert put_keys(pms) == ["121", "122"]
    assert library.pending() == dict(zip(paths, [0, 1, 1], strict=True))
    assert (counts.refreshed, counts.refresh_failed, counts.cleared_folders) == (1, 1, 1)


def test_three_unanswered_refreshes_in_a_row_stop_the_drain(
    library: Library, pms: responses.RequestsMock
) -> None:
    names = (b"A", b"B", b"C", b"D", b"E")
    paths = [library.sidecar(name) for name in names]
    library.queue(*paths)
    for n, name in enumerate(names):
        library.plex_lists(str(131 + n), name)
    ok_refreshes(pms, "131")
    ok_refreshes(pms, "132", "133", "134", status=500)

    reply, counts = library.drain(pms)

    assert reply is plex.Reply.UNAVAILABLE
    assert put_keys(pms) == ["131", "132", "133", "134"]
    assert library.pending() == dict(zip(paths, [0, 1, 1, 1, 1], strict=True))
    assert (counts.refreshed, counts.refresh_failed, counts.cleared_sidecars) == (1, 3, 1)


def test_unanswered_refreshes_that_are_not_in_a_row_do_not_stop_the_drain(
    library: Library, pms: responses.RequestsMock
) -> None:
    names = (b"A", b"B", b"C", b"D", b"E")
    library.queue(*(library.sidecar(name) for name in names))
    for n, name in enumerate(names):
        library.plex_lists(str(141 + n), name)
    ok_refreshes(pms, "141", "142", "144", status=500)
    ok_refreshes(pms, "143", "145")

    reply, counts = library.drain(pms)

    assert reply is plex.Reply.OK
    assert len(put_keys(pms)) == 5
    assert (counts.refreshed, counts.refresh_failed) == (2, 3)


# plex-refresh through the real console script: interruption, the lock, exit codes, flags, report.

ALBUMS = (("1", b"A1"), ("2", b"A2"), ("3", b"A3"))


def queued(
    tmp_path: Path, stub: StubPlex, albums: tuple[tuple[str, bytes], ...] = ALBUMS
) -> tuple[Path, dict[str, str], dict[bytes, bytes]]:
    """Queue one sidecar per folder in an invented library, and have Plex list each folder.

    Returns the library, plex-refresh's environment, and each folder's sidecar path.
    """
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    real = os.fsencode(os.path.realpath(lib))
    paths = {folder: real + b"/" + folder + b"/t.lrc" for _, folder in albums}
    queue_sidecars(state_db_path(tmp_path), list(paths.values()))
    stub.location = os.fsdecode(real)
    stub.tracks = [
        plex_track(f"9{album}", album, real + b"/" + folder + b"/t.flac")
        for album, folder in albums
    ]
    env = {
        "PLYRIX_MUSIC_ROOT": str(lib),
        "PLYRIX_LRCLIB_URL": DUMMY_LRCLIB,
        "PLYRIX_PLEX_URL": stub.base_url,
        "PLYRIX_PLEX_SECTION": SECTION,
        "PLYRIX_PLEX_TOKEN": DUMMY_TOKEN,
        "PLYRIX_PLEX_PACE": "0",
    }
    return lib, env, paths


def flags_by_folder(tmp_path: Path, paths: dict[bytes, bytes]) -> dict[bytes, int]:
    flags = refresh_flags(state_db_path(tmp_path))
    return {folder: flags[path] for folder, path in paths.items()}


def test_a_drain_killed_mid_refresh_loses_nothing(
    tmp_path: Path, stub_plex: StubPlex, run_cli: RunCli
) -> None:
    _, env, paths = queued(tmp_path, stub_plex)
    stub_plex.hold_put = 3

    child = start_cli(tmp_path, ["plex-refresh"], env)
    try:
        assert stub_plex.put_arrived.wait(30), "the third refresh never arrived"
        child.send_signal(signal.SIGKILL)
        child.communicate(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
        stub_plex.release.set()

    assert child.returncode == -signal.SIGKILL
    assert stub_plex.put_keys() == ["1", "2", "3"]
    assert flags_by_folder(tmp_path, paths) == {b"A1": 0, b"A2": 0, b"A3": 1}

    stub_plex.requests.clear()
    stub_plex.hold_put = None
    proc = run_cli(["plex-refresh"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert stub_plex.put_keys() == ["3"]
    assert flags_by_folder(tmp_path, paths) == {b"A1": 0, b"A2": 0, b"A3": 0}


KILLED_RUN = """
import os, signal, sys

from plyrix import cli
from plyrix.state import db

real = db.mark_published
calls = 0


def publish_two_then_die(*args, **kwargs):
    global calls
    calls += 1
    if calls == 3:
        os.kill(os.getpid(), signal.SIGKILL)
    return real(*args, **kwargs)


db.mark_published = publish_two_then_die
cli.app(["run", sys.argv[1]])
"""


def test_a_killed_run_still_gets_its_refresh(
    tmp_path: Path, stub_lrclib: StubLrclib, stub_plex: StubPlex, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    tracks = {album: copy_track(lib, album=f"Album {album}") for album in ("1", "2", "3")}
    env = {
        "PLYRIX_LRCLIB_URL": stub_lrclib.base_url,
        "PLYRIX_PLEX_URL": stub_plex.base_url,
        "PLYRIX_PLEX_SECTION": SECTION,
        "PLYRIX_PLEX_TOKEN": DUMMY_TOKEN,
        "PLYRIX_PLEX_PACE": "0",
    }

    killed = subprocess.run(
        [sys.executable, "-c", KILLED_RUN, lib],
        env=cli_env(tmp_path, env),
        capture_output=True,
        check=False,
        timeout=60,
    )

    assert killed.returncode == -signal.SIGKILL, killed.stderr
    with sqlite3.connect(state_db_path(tmp_path)) as conn:
        rows = conn.execute("SELECT path, status, refresh_pending FROM sidecars").fetchall()
    published = {bytes(path) for path, status, _ in rows if status == "published"}
    assert len(published) == 2
    assert all(pending == 1 for _, status, pending in rows if status == "published")
    assert all(status in ("published", "intent") for _, status, _ in rows)
    real_lib = os.fsencode(os.path.realpath(lib))
    stub_plex.location = os.fsdecode(real_lib)
    album_of: dict[bytes, str] = {}
    for album, track in tracks.items():
        audio = os.fsencode(os.path.realpath(track))
        album_of[os.path.dirname(audio)] = album
        stub_plex.tracks.append(plex_track(f"9{album}", album, audio))
    expected = {album_of[os.path.dirname(path)] for path in published}

    env["PLYRIX_MUSIC_ROOT"] = str(lib)
    first = run_cli(["plex-refresh"], env=env)

    assert first.returncode == 0, first.stderr
    assert set(stub_plex.put_keys()) == expected
    assert len(stub_plex.put_keys()) == 2
    assert parse_counts(first.stdout)["cleared_folders"] == 2

    rerun = run_lib(run_cli, stub_lrclib, lib)
    assert rerun.returncode == 0, rerun.stderr
    stub_plex.requests.clear()
    second = run_cli(["plex-refresh"], env=env)

    assert second.returncode == 0, second.stderr
    assert stub_plex.put_keys() == sorted(set(tracks) - expected)
    assert set(refresh_flags(state_db_path(tmp_path)).values()) == {0}


def test_plex_refresh_waits_its_turn(tmp_path: Path, stub_plex: StubPlex, run_cli: RunCli) -> None:
    _, env, paths = queued(tmp_path, stub_plex)
    holder = db.connect(state_db_path(tmp_path))
    try:
        proc = run_cli(["plex-refresh"], env=env)
        dry = run_cli(["plex-refresh", "--dry-run"], env=env)
    finally:
        holder.close()

    assert proc.returncode == 75, proc.stderr
    assert dry.returncode == 75, dry.stderr
    assert stub_plex.requests == []
    assert set(flags_by_folder(tmp_path, paths).values()) == {1}


def test_run_never_refreshes_plex(
    tmp_path: Path, stub_lrclib: StubLrclib, stub_plex: StubPlex, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    plex_settings = {
        "PLYRIX_PLEX_URL": stub_plex.base_url,
        "PLYRIX_PLEX_SECTION": SECTION,
        "PLYRIX_PLEX_TOKEN": DUMMY_TOKEN,
        "PLYRIX_PLEX_PACE": "0",
    }

    proc = run_lib(run_cli, stub_lrclib, lib, **plex_settings)

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").exists()
    assert stub_plex.requests == []


def dead_url() -> str:
    """Return a local URL nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


EXIT_CASES = {
    "section not listed": (2, b"plex_section (PLYRIX_PLEX_SECTION)"),
    "section not music": (2, b"plex_section (PLYRIX_PLEX_SECTION)"),
    "sections 500": (1, b"nothing was refreshed"),
    "nothing listening": (1, b"nothing was refreshed"),
    "sections 401": (1, b"plex_token (PLYRIX_PLEX_TOKEN)"),
    "listing 401": (1, b"plex_token (PLYRIX_PLEX_TOKEN)"),
    "listing 404": (2, b"plex_section (PLYRIX_PLEX_SECTION)"),
    "listing 500": (1, b"nothing was refreshed"),
    "refresh 401": (3, b"plex_token (PLYRIX_PLEX_TOKEN)"),
    "refresh 500": (3, b"stay queued"),
}


@pytest.mark.parametrize("case", EXIT_CASES)
def test_plex_refresh_exit_codes(
    tmp_path: Path, stub_plex: StubPlex, run_cli: RunCli, case: str
) -> None:
    _, env, paths = queued(tmp_path, stub_plex)
    if case == "section not listed":
        stub_plex.sections = [plex_section_dir("2", "artist", stub_plex.location)]
    elif case == "section not music":
        stub_plex.sections = [plex_section_dir("1", "movie", stub_plex.location)]
    elif case == "nothing listening":
        env["PLYRIX_PLEX_URL"] = dead_url()
    elif case.startswith("sections"):
        stub_plex.sections_status = int(case.split()[1])
    elif case.startswith("listing"):
        stub_plex.listing_status = int(case.split()[1])
    else:
        stub_plex.refresh_status = int(case.split()[1])
    code, words = EXIT_CASES[case]

    proc = run_cli(["plex-refresh"], env=env)

    assert proc.returncode == code, proc.stderr
    assert words in proc.stderr
    assert len([line for line in proc.stderr.splitlines() if b"plex_token" in line]) <= 1
    assert DUMMY_TOKEN.encode() not in proc.stderr + proc.stdout
    assert set(flags_by_folder(tmp_path, paths).values()) == {1}
    if code != 3:
        assert stub_plex.puts() == []


def test_plex_refresh_a_folder_plex_does_not_list_still_exits_0(
    tmp_path: Path, stub_plex: StubPlex, run_cli: RunCli
) -> None:
    _, env, paths = queued(tmp_path, stub_plex)
    stub_plex.tracks = stub_plex.tracks[:2]

    proc = run_cli(["plex-refresh"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["not_in_plex"] == 1
    assert flags_by_folder(tmp_path, paths) == {b"A1": 0, b"A2": 0, b"A3": 1}


def test_plex_refresh_says_the_drain_stopped_when_plex_stops_answering(
    tmp_path: Path, stub_plex: StubPlex, run_cli: RunCli
) -> None:
    _, env, _ = queued(tmp_path, stub_plex)
    stub_plex.refresh_status = 500

    proc = run_cli(["plex-refresh"], env=env)

    assert proc.returncode == 3, proc.stderr
    assert b"stopped answering" in proc.stderr
    assert parse_counts(proc.stdout)["refresh_failed"] == 3


@pytest.mark.parametrize("value", ["0", "x", "-1"])
def test_plex_refresh_limit_must_be_a_positive_number(
    tmp_path: Path, stub_plex: StubPlex, run_cli: RunCli, value: str
) -> None:
    _, env, _ = queued(tmp_path, stub_plex)

    proc = run_cli(["plex-refresh", "--limit", value], env=env)

    assert proc.returncode == 2, proc.stderr
    assert stub_plex.requests == []


def test_plex_refresh_limit_sends_that_many(
    tmp_path: Path, stub_plex: StubPlex, run_cli: RunCli
) -> None:
    _, env, paths = queued(tmp_path, stub_plex)

    proc = run_cli(["plex-refresh", "--limit", "2"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert stub_plex.put_keys() == ["1", "2"]
    assert parse_counts(proc.stdout)["left_by_limit"] == 1
    assert flags_by_folder(tmp_path, paths) == {b"A1": 0, b"A2": 0, b"A3": 1}


@pytest.mark.parametrize("how", ["flag", "setting"])
def test_plex_refresh_dry_run_changes_nothing(
    tmp_path: Path, stub_plex: StubPlex, run_cli: RunCli, how: str
) -> None:
    _, env, _ = queued(tmp_path, stub_plex)
    ledger = state_db_path(tmp_path)
    before = hashlib.sha256(ledger.read_bytes()).hexdigest()
    args = ["plex-refresh"]
    if how == "flag":
        args.append("--dry-run")
    else:
        env["PLYRIX_DRY_RUN"] = "true"

    proc = run_cli(args, env=env)

    assert proc.returncode == 0, proc.stderr
    assert stub_plex.puts() == []
    assert stub_plex.requests
    assert cli.DRY_RUN.encode() in proc.stderr
    assert parse_counts(proc.stdout)["albums_to_refresh"] == 3
    assert hashlib.sha256(ledger.read_bytes()).hexdigest() == before
    assert not ledger.with_name(ledger.name + "-journal").exists()


def test_plex_refresh_says_how_many_albums_before_the_first(
    tmp_path: Path, stub_plex: StubPlex, run_cli: RunCli
) -> None:
    lib, env, _ = queued(tmp_path, stub_plex)
    env["PLYRIX_PLEX_PACE"] = "0.01"

    proc = run_cli(["plex-refresh"], env=env)

    assert proc.returncode == 0, proc.stderr
    lines = proc.stderr.splitlines()
    assert lines == [b"plyrix: asking Plex to refresh 3 albums, one every 0.01 seconds"]
    assert os.fsencode(lib) not in proc.stderr
    assert b"Fixture" not in proc.stderr


REPORT_KEYS = {
    "report_version",
    "plyrix_version",
    "command",
    "dry_run",
    "started_at",
    "finished_at",
    "end_reason",
    "counts",
}


@pytest.mark.parametrize(
    ("sections_status", "end_reason"), [(200, "finished"), (500, "plex_unavailable")]
)
def test_plex_refresh_report_holds_counts_and_run_facts_only(
    tmp_path: Path, stub_plex: StubPlex, run_cli: RunCli, sections_status: int, end_reason: str
) -> None:
    lib, env, _ = queued(tmp_path, stub_plex)
    stub_plex.sections_status = sections_status
    out = tmp_path / "out" / "report.json"
    out.parent.mkdir()

    proc = run_cli(["plex-refresh", "--report", str(out)], env=env)

    assert proc.returncode == (0 if sections_status == 200 else 1), proc.stderr
    data = out.read_bytes()
    report = json.loads(data)
    assert set(report) == REPORT_KEYS
    assert report["command"] == "plex-refresh"
    assert report["dry_run"] is False
    assert report["end_reason"] == end_reason
    assert set(report["counts"]) == {f.name for f in dataclasses.fields(refresh.RefreshCounts)}
    port = urlsplit(stub_plex.base_url).port
    assert port is not None
    assert str(port).encode() not in data
    assert b"127.0.0.1" not in data
    assert DUMMY_TOKEN.encode() not in data
    assert os.fsencode(os.path.realpath(lib)) not in data
    assert b"/lib" not in data


def test_plex_refresh_ignores_the_report_file_setting(
    tmp_path: Path, stub_plex: StubPlex, run_cli: RunCli
) -> None:
    _, env, _ = queued(tmp_path, stub_plex)
    out = tmp_path / "run-report.json"
    env["PLYRIX_REPORT_FILE"] = str(out)

    proc = run_cli(["plex-refresh"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert not out.exists()


def test_plex_refresh_report_refuses_a_path_in_the_library(
    tmp_path: Path, stub_plex: StubPlex, run_cli: RunCli
) -> None:
    lib, env, _ = queued(tmp_path, stub_plex)

    proc = run_cli(["plex-refresh", "--report", str(lib / "r.json")], env=env)

    assert proc.returncode == 2, proc.stderr
    assert stub_plex.requests == []


README = Path(__file__).parent.parent / "README.md"


def test_readme_plex_section_names_every_count_and_exit_code() -> None:
    readme = README.read_text(encoding="utf-8")
    section = readme.split("\n## Plex\n", 1)[1].split("\n## ", 1)[0]

    missing = {
        f.name for f in dataclasses.fields(refresh.RefreshCounts) if f"`{f.name}`" not in section
    }
    assert missing == set()
    codes = section.split("### Plex refresh exit codes", 1)[1].split("\n#", 1)[0]
    assert set(re.findall(r"^\| (\d+) \|", codes, re.MULTILINE)) == {"0", "1", "2", "3", "75"}


def test_under_drains_only_those_folders_on_a_component_boundary(
    library: Library, pms: responses.RequestsMock
) -> None:
    a, cd2 = library.sidecar(b"A"), library.sidecar(b"A/CD2")
    ab, b = library.sidecar(b"AB"), library.sidecar(b"B")
    library.queue(a, cd2, ab, b)
    for album, folder in (("11", b"A"), ("12", b"A/CD2"), ("13", b"AB"), ("14", b"B")):
        library.plex_lists(album, folder)
    ok_refreshes(pms, "11", "12")

    reply, counts = library.drain(pms, under=[library.real + b"/A"])

    assert reply is plex.Reply.OK
    assert put_keys(pms) == ["11", "12"]
    assert library.pending() == {a: 0, cd2: 0, ab: 1, b: 1}
    assert counts.queued_folders == 2


def test_the_deadline_stops_sending_and_leaves_the_rest(
    library: Library, pms: responses.RequestsMock
) -> None:
    paths = [library.sidecar(folder) for folder in (b"A", b"B", b"C")]
    library.queue(*paths)
    for album, folder in (("11", b"A"), ("12", b"B"), ("13", b"C")):
        library.plex_lists(album, folder)
    ok_refreshes(pms, "11")
    reads: list[int] = []

    def clock() -> float:
        # Time to spare for the listing and the first refresh, then none.
        reads.append(1)
        return 0.0 if len(reads) <= 2 else 5.0

    reply, counts = library.drain(pms, deadline=1.0, clock=clock)

    assert reply is plex.Reply.OK
    assert put_keys(pms) == ["11"]
    assert library.pending() == {paths[0]: 0, paths[1]: 1, paths[2]: 1}
    assert counts.left_by_limit == 2
    assert counts.cleared_folders == 1


def test_no_listing_once_the_deadline_has_passed(
    library: Library, pms: responses.RequestsMock
) -> None:
    paths = [library.sidecar(folder) for folder in (b"A", b"B")]
    library.queue(*paths)
    conn = db.connect(library.ledger)
    try:
        reply, counts = refresh.drain(
            conn,
            plex.new_session("plyrix-dummy-token"),
            library.settings(),
            sleep=library.sleeps.append,
            deadline=1.0,
            clock=lambda: 5.0,
        )
    finally:
        conn.close()

    assert reply is plex.Reply.OK
    assert len(pms.calls) == 0
    assert counts.left_by_limit == 2
    assert library.pending() == {paths[0]: 1, paths[1]: 1}
