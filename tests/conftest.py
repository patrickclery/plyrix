"""Shared test helpers: stub LRCLIB and Plex servers, the real CLI, an audio snapshot, a canary."""

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from plyrix.config import Settings
from plyrix.domain import Kind, TrackTags
from plyrix.state import db

FIXTURES = Path(__file__).parent / "fixtures"
AUDIO_SUFFIXES = (".flac", ".mp3", ".m4a", ".ogg", ".opus")

# Typer forces Rich's terminal mode when GITHUB_ACTIONS, FORCE_COLOR or PY_COLORS is set, and the
# styled help splits flag names with escape codes. Every child process inherits os.environ, so the
# suite drops them once and asks for no colour, giving hosted CI the same plain output as a pipe.
COLOUR_FORCERS = ("GITHUB_ACTIONS", "FORCE_COLOR", "PY_COLORS")


@pytest.fixture(autouse=True, scope="session")
def plain_child_output() -> Iterator[None]:
    with pytest.MonkeyPatch.context() as patch:
        for name in COLOUR_FORCERS:
            patch.delenv(name, raising=False)
        patch.setenv("NO_COLOR", "1")
        yield


# All invented text. A unique canary per session shows where lyric text ends up.
CANARY = "PLYRIX-CANARY-" + uuid.uuid4().hex
RECORD = {
    "id": 7,
    "name": "x",
    "trackName": "Fixture Title",
    "artistName": "Fixture Artist",
    "albumName": "Fixture Album",
    "duration": 3.0,
    "instrumental": False,
    "hasWordSync": False,
    "plainLyrics": f"{CANARY} plain line 1",
    "syncedLyrics": (
        f"[00:00.50] {CANARY} line 1\n[00:01.00] {CANARY} line 2\n"
        f"[00:01.50] {CANARY} line 3\n[00:02.00] {CANARY} line 4\n"
    ),
    "lyricsfile": CANARY,
}

# What answers a preflight probe by id: a row with no lyric text, as a probe's body is discarded.
PROBED_RECORD = {"id": 1, "trackName": "x", "artistName": "x", "instrumental": True}
PROBE_TEXT = "plyrix preflight"
_ID_PATH = re.compile(r"/api/get/\d+")

RunCli = Callable[..., "subprocess.CompletedProcess[bytes]"]


class StubLrclib:
    """A healthy server: lookups come from their own fields, and preflight's probes from theirs.

    /api/get serves status and body, or queue while it holds (status, body) pairs. /api/search does
    the same with search_status, search_body and search_queue. A body may be a JSON-serialisable
    value, or bytes to send as they are. Anything else gets 404 with an empty object.

    A probe holds the text preflight sends, or asks for a track by id. Probes are answered from
    preflight_get, preflight_search and preflight_id, so a test that breaks a lookup still gets past
    preflight, and they are kept in probes. A search probe takes preflight_search_queue's pairs
    first. Every other request is kept in requests. Each is its query params plus a "path" key.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.status = 200
        self.body: object = RECORD
        self.queue: list[tuple[int, object]] = []
        self.search_status = 200
        self.search_body: object = []
        self.search_queue: list[tuple[int, object]] = []
        self.preflight_get: tuple[int, object] = (404, {"name": "TrackNotFound"})
        self.preflight_search: tuple[int, object] = (200, [])
        self.preflight_search_queue: list[tuple[int, object]] = []
        self.preflight_id: tuple[int, object] = (200, PROBED_RECORD)
        self.requests: list[dict[str, str]] = []
        self.probes: list[dict[str, str]] = []

    def next_response(self) -> tuple[int, object]:
        return self.queue.pop(0) if self.queue else (self.status, self.body)

    def next_search_response(self) -> tuple[int, object]:
        if self.search_queue:
            return self.search_queue.pop(0)
        return self.search_status, self.search_body

    def next_probe_search(self) -> tuple[int, object]:
        if self.preflight_search_queue:
            return self.preflight_search_queue.pop(0)
        return self.preflight_search

    def answer(self, path: str, params: dict[str, str]) -> tuple[int, bytes]:
        """Keep one request, and return its status and body."""
        is_probe = params.get("track_name") == PROBE_TEXT or _ID_PATH.fullmatch(path) is not None
        (self.probes if is_probe else self.requests).append({"path": path, **params})
        answer: tuple[int, object] = (404, {})
        if path == "/api/get":
            answer = self.preflight_get if is_probe else self.next_response()
        elif path == "/api/search":
            answer = self.next_probe_search() if is_probe else self.next_search_response()
        elif _ID_PATH.fullmatch(path):
            answer = self.preflight_id
        status, body = answer
        return status, body if isinstance(body, bytes) else json.dumps(body).encode()


@pytest.fixture
def stub_lrclib() -> Iterator[StubLrclib]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlsplit(self.path)
            status, body = stub.answer(url.path, dict(parse_qsl(url.query)))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    stub = StubLrclib(f"http://127.0.0.1:{server.server_address[1]}/api")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


_PLEX_ACCOUNT_HOST = "plex.tv"


def is_plex_tv(host: object) -> bool:
    """Say whether a host is plex.tv or a subdomain, in any case and with a trailing dot."""
    name = host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)
    name = name.rstrip(".").lower()
    return name == _PLEX_ACCOUNT_HOST or name.endswith("." + _PLEX_ACCOUNT_HOST)


@pytest.fixture(autouse=True, scope="session")
def no_plex_tv() -> Iterator[None]:
    """Stop any test resolving plex.tv, as a token sent there has broken a server record."""
    real = socket.getaddrinfo

    def refuse_plex_tv(host: object, *args: object, **kwargs: object) -> object:
        if is_plex_tv(host):
            raise AssertionError("a test tried to reach plex.tv")
        return real(host, *args, **kwargs)  # type: ignore[arg-type]

    socket.getaddrinfo = refuse_plex_tv  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.getaddrinfo = real


_REFRESH_PATH = re.compile(r"/library/metadata/(\d+)/refresh")
_METADATA_PATH = re.compile(r"/library/metadata/(\d+)")
_STREAM_PATH = re.compile(r"/library/streams/(\d+)")
_SECTION_ALL_PATH = re.compile(r"/library/sections/(\d+)/all")


def plex_track(key: str, album_key: str, file: bytes) -> dict[str, object]:
    """Return one track as a Plex listing holds it, with an invented title and its file's path."""
    return {
        "ratingKey": key,
        "parentRatingKey": album_key,
        "title": "Fixture Title",
        "Media": [{"Part": [{"file": file.decode("utf-8", "surrogateescape")}]}],
    }


LOCAL_LYRICS = "com.plexapp.agents.localmedia"
LYRICFIND = "com.plexapp.agents.lyricfind"


def plex_lyric_stream(
    id: int, provider: str, codec: str, timed: str | None = None
) -> dict[str, object]:
    """Return one lyric stream as a track's Part lists it. LyricFind's carries timed as a string."""
    stream: dict[str, object] = {
        "id": id,
        "key": f"/library/streams/{id}",
        "streamType": 4,
        "provider": provider,
        "codec": codec,
        "format": codec,
        "displayTitle": "Fixture Lyrics",
    }
    if timed is not None:
        stream["timed"] = timed
    return stream


def plex_stream_body(timed: object, text: str) -> bytes:
    """Return GET /library/streams/{id}'s body, with invented text in Line[].Span[].text."""
    lyrics = {
        "provider": LOCAL_LYRICS,
        "timed": timed,
        "minLines": "1",
        "author": text,
        "by": text,
        "title": text,
        "Line": [
            {"startOffset": 500 * n, "endOffset": 500 * n + 400, "Span": [{"text": text}]}
            for n in range(1, 4)
        ],
    }
    return _plex_body({"size": 1, "Lyrics": [lyrics]})


def plex_metadata(key: str, streams: Sequence[object], title: str = "Fixture Title") -> bytes:
    """Return GET /library/metadata/{key}'s body: one track whose one Part holds these streams."""
    audio = {"id": 1, "streamType": 2, "codec": "flac", "displayTitle": title}
    track = {
        "ratingKey": key,
        "title": title,
        "parentTitle": title,
        "grandparentTitle": title,
        "Media": [{"Part": [{"file": "", "Stream": [audio, *streams]}]}],
    }
    return _plex_body({"size": 1, "Metadata": [track]})


def plex_section_dir(
    key: str = "1", kind: str = "artist", location: bytes | str = ""
) -> dict[str, object]:
    """Return one section as GET /library/sections lists it, with an invented title."""
    path = location if isinstance(location, str) else location.decode("utf-8", "surrogateescape")
    return {
        "key": key,
        "type": kind,
        "agent": "tv.plex.agents.music" if kind == "artist" else "tv.plex.agents.movie",
        "title": "Fixture Library",
        "Location": [{"id": 1, "path": path}],
    }


def plex_listing(tracks: Sequence[dict[str, object]], **container: object) -> bytes:
    """Return a whole track listing as one page's body, which may hold non-UTF-8 bytes."""
    page = {"size": len(tracks), "totalSize": len(tracks), "offset": 0, "Metadata": list(tracks)}
    return _plex_body({**page, **container})


def queue_sidecars(
    ledger: Path, paths: Sequence[bytes], kind: Kind = Kind.LRC, refreshed: bool = False
) -> None:
    """Record each path as a published sidecar whose album Plex hasn't been asked to refresh yet.

    With refreshed, Plex has accepted that refresh. No file is written; plex-refresh and
    plex-check read only the ledger.
    """
    # Only a test's own ledger, so it needn't wait for each commit to reach the disk.
    if not ledger.exists():
        ledger.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fresh = sqlite3.connect(ledger)
        try:
            fresh.execute("PRAGMA synchronous=OFF")
            db._migrate(fresh)
        finally:
            fresh.close()
    conn = db.connect(ledger)
    conn.execute("PRAGMA synchronous=OFF")
    try:
        run_id = db.begin_run(conn, "test")
        for path in paths:
            db.add_intent(conn, path, kind, "0" * 64, 1, run_id)
            db.mark_published(conn, path)
        if refreshed:
            db.clear_refresh(conn, list(paths))
    finally:
        conn.close()


def refresh_flags(ledger: Path) -> dict[bytes, int]:
    """Return each sidecar's refresh_pending flag by path."""
    with sqlite3.connect(ledger) as conn:
        rows = conn.execute("SELECT path, refresh_pending FROM sidecars").fetchall()
    return {bytes(path): flag for path, flag in rows}


class StubPlex:
    """A local Plex Media Server: a section list, a paged track listing and album refreshes.

    GET /library/sections answers sections_status, with sections, or by default one music
    section with key "1" whose Location is location. GET /library/sections/{id}/all answers
    listing_status, slicing tracks by the paging headers. PUT /library/metadata/{key}/refresh
    answers refresh_statuses for that key if set, else from refresh_queue while it holds
    statuses, then refresh_status. Anything else gets 404. Every request is kept in requests as
    its method, path, query params and lower-cased headers. Bodies are encoded with
    surrogateescape, so a track's file can hold raw non-UTF-8 bytes.

    GET /library/metadata/{key} answers metadata[key] and GET /library/streams/{id} answers
    streams[id], each a raw body, with 404 for a key or id not there. A streams_status other
    than 200 answers every stream request with that status.

    With hold_put set to n, the nth PUT sets put_arrived and waits for release before it is
    answered, so a test can kill the client while Plex holds that request. The fixture sets
    release on teardown.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.sections: list[dict[str, object]] | None = None
        self.location = ""
        self.tracks: list[dict[str, object]] = []
        self.sections_status = 200
        self.listing_status = 200
        self.refresh_status = 200
        self.refresh_queue: list[int] = []
        self.refresh_statuses: dict[str, int] = {}
        self.metadata: dict[str, bytes] = {}
        self.streams: dict[str, bytes] = {}
        self.streams_status = 200
        self.requests: list[dict[str, object]] = []
        self.hold_put: int | None = None
        self.put_arrived = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def put_keys(self) -> list[str]:
        """Return the album key of each refresh asked for, in order."""
        keys = []
        for request in self.puts():
            match = _REFRESH_PATH.fullmatch(str(request["path"]))
            assert match
            keys.append(match.group(1))
        return keys

    def puts(self) -> list[dict[str, object]]:
        return [request for request in self.requests if request["method"] == "PUT"]

    def answer(
        self, method: str, path: str, query: dict[str, str], headers: dict[str, str]
    ) -> tuple[int, bytes]:
        """Keep one request, and return its status and body."""
        with self._lock:
            self.requests.append(
                {"method": method, "path": path, "query": query, "headers": headers}
            )
            held = method == "PUT" and self.hold_put == len(self.puts())
        if method == "PUT" and (match := _REFRESH_PATH.fullmatch(path)):
            if held:
                self.put_arrived.set()
                self.release.wait(60)
            if match.group(1) in self.refresh_statuses:
                return self.refresh_statuses[match.group(1)], b""
            status = self.refresh_queue.pop(0) if self.refresh_queue else self.refresh_status
            return status, b""
        if method == "GET" and path == "/library/sections":
            if self.sections_status != 200:
                return self.sections_status, b""
            sections = self.sections
            if sections is None:
                sections = [plex_section_dir("1", "artist", self.location)]
            return 200, _plex_body({"size": len(sections), "Directory": sections})
        if method == "GET" and _SECTION_ALL_PATH.fullmatch(path):
            if self.listing_status != 200:
                return self.listing_status, b""
            start = int(headers.get("x-plex-container-start", "0"))
            size = int(headers.get("x-plex-container-size", str(len(self.tracks))))
            page = self.tracks[start : start + size]
            container = {
                "size": len(page),
                "totalSize": len(self.tracks),
                "offset": start,
                "Metadata": page,
            }
            return 200, _plex_body(container)
        if method == "GET" and (match := _METADATA_PATH.fullmatch(path)):
            body = self.metadata.get(match.group(1))
            return (404, b"") if body is None else (200, body)
        if method == "GET" and (match := _STREAM_PATH.fullmatch(path)):
            if self.streams_status != 200:
                return self.streams_status, b""
            body = self.streams.get(match.group(1))
            return (404, b"") if body is None else (200, body)
        return 404, b""


def _plex_body(container: dict[str, object]) -> bytes:
    body = {"MediaContainer": container}
    return json.dumps(body, ensure_ascii=False).encode("utf-8", "surrogateescape")


@pytest.fixture
def stub_plex() -> Iterator[StubPlex]:
    class Handler(BaseHTTPRequestHandler):
        def _answer(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            url = urlsplit(self.path)
            headers = {name.lower(): value for name, value in self.headers.items()}
            status, body = stub.answer(method, url.path, dict(parse_qsl(url.query)), headers)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._answer("GET")

        def do_PUT(self) -> None:
            self._answer("PUT")

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    stub = StubPlex(f"http://127.0.0.1:{server.server_address[1]}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield stub
    finally:
        # A held PUT must not keep its handler waiting through shutdown.
        stub.release.set()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def run_cli(tmp_path: Path) -> RunCli:
    """Return a function running the real console script, with state and config under tmp_path."""

    def run(
        args: Sequence[str | bytes | Path],
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> "subprocess.CompletedProcess[bytes]":
        inherited = {k: v for k, v in os.environ.items() if not k.startswith("PLYRIX_")}
        return subprocess.run(
            [Path(sys.executable).parent / "plyrix", *args],
            env={
                **inherited,
                "XDG_STATE_HOME": str(tmp_path / "state"),
                "XDG_CONFIG_HOME": str(tmp_path / "config"),
                **(env or {}),
            },
            cwd=cwd,
            capture_output=True,
            check=False,
            timeout=120,
        )

    return run


def cli_env(tmp_path: Path, env: dict[str, str]) -> dict[str, str]:
    """Return run_cli's environment: no inherited PLYRIX_ variable, state and config in tmp_path."""
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("PLYRIX_")}
    return {
        **inherited,
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        **env,
    }


def start_cli(
    tmp_path: Path, args: Sequence[str], env: dict[str, str]
) -> "subprocess.Popen[bytes]":
    """Start the real console script with run_cli's environment, without waiting for it."""
    return subprocess.Popen(
        [Path(sys.executable).parent / "plyrix", *args],
        env=cli_env(tmp_path, env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def state_db_path(tmp_path: Path) -> Path:
    """Return where run_cli's default environment puts the ledger."""
    return tmp_path / "state" / "plyrix" / "state.db"


def snap(path: Path) -> tuple[str, int, int, int, int, int, int]:
    """Identify a file or folder so that any change shows, even a rename-over of identical bytes."""
    st = path.stat()
    contents = b"" if path.is_dir() else path.read_bytes()
    return (
        hashlib.sha256(contents).hexdigest(),
        st.st_size,
        st.st_mtime_ns,
        st.st_ctime_ns,
        st.st_ino,
        st.st_nlink,
        st.st_mode,
    )


def leaks(tmp_path: Path, proc: "subprocess.CompletedProcess[bytes]", lib: Path) -> list[str]:
    """Return every place the canary reached, apart from sidecars in the library."""
    needle = CANARY.encode()
    found = [
        label
        for label, blob in (("stdout", proc.stdout), ("stderr", proc.stderr))
        if needle in blob
    ]
    for base, _, files in os.walk(tmp_path):
        for file in files:
            path = Path(base) / file
            if path.suffix in (".lrc", ".txt") and path.is_relative_to(lib):
                continue
            if needle in path.read_bytes():
                found.append(str(path))
    return found


def parse_counts(stdout: bytes) -> dict[str, int]:
    """Return the key=value count lines of a run's stdout."""
    matches = re.findall(rb"^([a-z_]+)=(\d+)$", stdout, re.MULTILINE)
    return {key.decode(): int(value) for key, value in matches}


def lidarr_env(
    event: str = "AlbumDownload",
    added: Sequence[str | Path] = (),
    deleted: Sequence[str | Path] = (),
    artist: str | Path = "",
    album_id: str = "1",
    lowercase: bool = False,
) -> dict[str, str]:
    """Return the variables Lidarr's Custom Script sets, under the mixed-case names of its source.

    With lowercase, the names are as a real Lidarr on Linux sets them, as it collects them in a
    .NET StringDictionary. Lidarr leaves out the track path lists when they would be empty, so this
    does too.
    """
    env = {"Lidarr_EventType": event}
    if added:
        env["Lidarr_AddedTrackPaths"] = "|".join(str(path) for path in added)
    if deleted:
        env["Lidarr_DeletedPaths"] = "|".join(str(path) for path in deleted)
    if event != "Test":
        env["Lidarr_Artist_Path"] = str(artist)
        env["Lidarr_Album_Id"] = album_id
    if lowercase:
        return {name.lower(): value for name, value in env.items()}
    return env


def copy_track(lib: Path, album: str = "Album", name: str = "t.flac") -> Path:
    """Copy t.flac to lib/album/name, and return the copy."""
    directory = lib / album
    directory.mkdir(parents=True, exist_ok=True)
    return Path(shutil.copyfile(FIXTURES / "t.flac", directory / name))


def ledger_rows(tmp_path: Path) -> list[tuple[str, str, int, bytes]]:
    """Return what the ledger holds about each sidecar."""
    with sqlite3.connect(state_db_path(tmp_path)) as conn:
        return conn.execute("SELECT status, sha256, size, path FROM sidecars").fetchall()


def run_lib(run_cli: RunCli, stub: StubLrclib, lib: Path, **env: str):  # type: ignore[no-untyped-def]
    """Run the real console script over lib, against the stub."""
    return run_cli(["run", lib], env={"PLYRIX_LRCLIB_URL": stub.base_url, **env})


def settings_for(tmp_path: Path, stub: StubLrclib, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Return settings for a run in this process, over tmp_path/lib and against the stub."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return Settings(
        music_root=tmp_path / "lib",
        lrclib_url=stub.base_url,
        state_db=tmp_path / "state" / "state.db",
    )


def tags_with(**fields: object) -> TrackTags:
    """Return the tags t.flac holds, with these fields changed, for patching over tags.read."""
    values: dict[str, object] = {
        "artist": "Fixture Artist",
        "albumartist": None,
        "title": "Fixture Title",
        "album": "Fixture Album",
        "duration": 3.0,
    }
    return TrackTags(**{**values, **fields})  # type: ignore[arg-type]


def non_notice_lines(stderr: bytes) -> list[bytes]:
    """Return stderr without the pre-release notice."""
    return [line for line in stderr.splitlines() if b"pre-release" not in line]


def sidecars_in(lib: Path) -> list[Path]:
    """Return every lyric sidecar under lib."""
    return [path for path in lib.rglob("*") if path.suffix in (".lrc", ".txt")]


def nonzero(stdout: bytes) -> dict[str, int]:
    """Return the counts of a run's stdout that aren't zero."""
    return {key: value for key, value in parse_counts(stdout).items() if value}


AUDITED_RUN = """
import json, os, sys

from plyrix import cli

opened = []


def record_opens(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        opened.append(os.fsdecode(args[0]))


log, *argv = sys.argv[1:]
sys.addaudithook(record_opens)
try:
    cli.app(argv)
finally:
    with open(log, "w", encoding="utf-8") as file:
        json.dump(opened, file)
"""
LIBRARY_FILES = (".flac", ".mp3", ".lrc", ".txt")


def audited_run(
    tmp_path: Path, stub: StubLrclib, lib: Path, dump_id: str = "dump-a"
) -> tuple["subprocess.CompletedProcess[bytes]", list[str]]:
    """Run the real command in a child, and return it with the library files it opened."""
    log = tmp_path / "opened.json"
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("PLYRIX_")}
    proc = subprocess.run(
        [sys.executable, "-c", AUDITED_RUN, log, "run", lib, "--dump-id", dump_id],
        env={
            **inherited,
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "PLYRIX_LRCLIB_URL": stub.base_url,
        },
        capture_output=True,
        check=False,
        timeout=120,
    )
    opened = json.loads(log.read_text(encoding="utf-8"))
    return proc, [path for path in opened if path.endswith(LIBRARY_FILES)]


TRIPWIRED_PLEX_REFRESH = """
import json, socket, sys

from plyrix import cli

hosts = []
real = socket.getaddrinfo


def record(host, *args, **kwargs):
    name = host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)
    hosts.append(name)
    name = name.rstrip(".").lower()
    if name == "plex.tv" or name.endswith(".plex.tv"):
        raise AssertionError("plyrix tried to reach plex.tv")
    return real(host, *args, **kwargs)


log, *argv = sys.argv[1:]
socket.getaddrinfo = record
try:
    cli.app(argv)
finally:
    with open(log, "w", encoding="utf-8") as file:
        json.dump(hosts, file)
"""


def tripwired_plex_refresh(
    tmp_path: Path, env: dict[str, str], *args: str
) -> tuple["subprocess.CompletedProcess[bytes]", list[str]]:
    """Run the real plex-refresh in a child, and return it with every host it resolved."""
    log = tmp_path / "hosts.json"
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("PLYRIX_")}
    proc = subprocess.run(
        [sys.executable, "-c", TRIPWIRED_PLEX_REFRESH, log, "plex-refresh", *args],
        env={
            **inherited,
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            **env,
        },
        capture_output=True,
        check=False,
        timeout=120,
    )
    hosts: list[str] = json.loads(log.read_text(encoding="utf-8"))
    return proc, hosts


KILLED_WRITER = """
import os, signal, sqlite3, sys

conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("PRAGMA cache_size=1")
conn.execute("BEGIN IMMEDIATE")
for _ in range(2000):
    conn.execute("INSERT INTO runs(started_at, version) VALUES(?, ?)", ("x" * 200, "x"))
os.kill(os.getpid(), signal.SIGKILL)
"""


def leave_hot_journal(ledger: Path) -> Path:
    """Kill a writer partway through a transaction that reached the disk, and return its journal.

    It opens the ledger without exclusive mode, so nothing but the journal says it was mid-write.
    """
    proc = subprocess.run(
        [sys.executable, "-c", KILLED_WRITER, ledger], check=False, capture_output=True, timeout=60
    )
    assert proc.returncode == -signal.SIGKILL, proc.stderr
    journal = ledger.with_name(ledger.name + "-journal")
    # A journal starting with a zero byte isn't hot, and the test using this would pass by accident.
    assert journal.read_bytes()[:1] not in (b"", b"\0")
    return journal


def per_format_lib(lib: Path, stem: str) -> list[Path]:
    """Copy STEM in each format to its own directory under lib, and return the copies.

    Five files sharing a stem in one folder would count as one sidecar, so they never share one.
    """
    copies = []
    for suffix in AUDIO_SUFFIXES:
        directory = lib / suffix.removeprefix(".")
        directory.mkdir(parents=True, exist_ok=True)
        copies.append(Path(shutil.copy(FIXTURES / f"{stem}{suffix}", directory)))
    return copies


def plant_deep_track(base: Path, folder_length: int, name: bytes = b"t.flac") -> bytes:
    """Copy t.flac into a new folder whose absolute path is folder_length bytes, and return it.

    Built through dir_fd, the only way to reach past PATH_MAX. shutil.rmtree(base) removes it.
    """
    audio = (FIXTURES / "t.flac").read_bytes()
    path = os.fsencode(base)
    folder = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        while (remaining := folder_length - len(path)) > 0:
            # Leave room for at least one byte of the next name and its slash.
            size = remaining - 1 if remaining <= 201 else min(200, remaining - 3)
            component = b"d" * size
            os.mkdir(component, dir_fd=folder)
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY, dir_fd=folder)
            os.close(folder)
            folder = child
            path = path + b"/" + component
        file = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, dir_fd=folder)
        try:
            os.write(file, audio)
        finally:
            os.close(file)
    finally:
        os.close(folder)
    return path
