"""LRCLIB-shaped databases with invented rows, and a local server for a dump listing and dumps.

The schema is the pinned server's (e1d863a, migrations 01 to 08), cut to what plyrix reads or a
test needs: tracks, lyrics, the trigger that moves tracks.last_lyrics_id, and both indexes. Every
row is invented, and every lyric holds CANARY, so a leak test can prove none of it escapes.
"""

import gzip
import io
import json
import re
import sqlite3
import struct
import threading
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import CANARY

SCHEMA = """
CREATE TABLE tracks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT,
  name_lower TEXT,
  artist_name TEXT,
  artist_name_lower TEXT,
  album_name TEXT,
  album_name_lower TEXT,
  duration FLOAT,
  last_lyrics_id INTEGER,
  created_at DATETIME,
  updated_at DATETIME,
  UNIQUE(name_lower, artist_name_lower, album_name_lower, duration)
);
CREATE TABLE lyrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plain_lyrics TEXT,
  synced_lyrics TEXT,
  track_id INTEGER,
  has_plain_lyrics BOOLEAN,
  has_synced_lyrics BOOLEAN,
  instrumental BOOLEAN,
  source TEXT,
  created_at DATETIME,
  updated_at DATETIME,
  lyricsfile TEXT,
  has_lyricsfile BOOLEAN,
  has_word_sync BOOLEAN
);
CREATE TRIGGER set_tracks_last_lyrics_id AFTER INSERT ON lyrics BEGIN
  UPDATE tracks SET last_lyrics_id = NEW.id WHERE tracks.id = NEW.track_id;
END;
CREATE INDEX idx_tracks_last_lyrics_id ON tracks (last_lyrics_id);
CREATE INDEX idx_lyrics_created_at ON lyrics (created_at);
"""
CREATED = "2026-01-01 00:00:00"


def make_db(
    path: Path, tracks: int, *, first_id: int = 1, user_version: int = 8, token: str = ""
) -> Path:
    """Write an LRCLIB-shaped database of invented tracks, each with one lyric row, in WAL mode.

    Track ids run from first_id. Closing the last connection checkpoints the WAL, so only the
    file is left, still marked WAL in its header, as a real dump is.
    """
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        if first_id > 1:
            con.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES('tracks', ?)", (first_id - 1,)
            )
        for n in range(first_id, first_id + tracks):
            add_track(con, n, token)
        con.execute(f"PRAGMA user_version={user_version}")
        con.commit()
    finally:
        con.close()
    return path


def add_track(con: sqlite3.Connection, n: int, token: str = "", created: str = CREATED) -> int:
    """Insert invented track n and one lyric for it, and return the track's id.

    token, when given, goes into the artist and track names, so a leak test can look for it.
    """
    artist, title = f"Invented Artist {n}{token}", f"Invented Track {n}{token}"
    cursor = con.execute(
        "INSERT INTO tracks(name, name_lower, artist_name, artist_name_lower, album_name, "
        "album_name_lower, duration, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            title,
            title.lower(),
            artist,
            artist.lower(),
            "Invented Album",
            "invented album",
            180.0,
            created,
            created,
        ),
    )
    track_id = cursor.lastrowid
    assert track_id is not None
    add_lyric(con, track_id, created)
    return track_id


def add_lyric(con: sqlite3.Connection, track_id: int, created: str = CREATED) -> None:
    """Insert a lyric row holding CANARY, which the trigger makes the track's latest."""
    con.execute(
        "INSERT INTO lyrics(plain_lyrics, synced_lyrics, track_id, has_plain_lyrics, "
        "has_synced_lyrics, instrumental, source, created_at, updated_at, has_lyricsfile, "
        "has_word_sync) VALUES(?, ?, ?, 1, 1, 0, 'invented', ?, ?, 0, 0)",
        (f"{CANARY} plain", f"[00:01.00] {CANARY}", track_id, created, created),
    )


FTEXT, FHCRC, FEXTRA, FNAME, FCOMMENT = 0x01, 0x02, 0x04, 0x08, 0x10


def gz_with_header(payload: bytes, flags: int) -> bytes:
    """Return payload gzipped by hand, with exactly these optional header fields set."""
    head = b"\x1f\x8b\x08" + bytes([flags]) + struct.pack("<I", 0) + b"\x00\xff"
    if flags & FEXTRA:
        extra = b"AB\x04\x00data"
        head += struct.pack("<H", len(extra)) + extra
    if flags & FNAME:
        head += b"db.sqlite3\x00"
    if flags & FCOMMENT:
        head += b"an invented comment\x00"
    if flags & FHCRC:
        head += struct.pack("<H", zlib.crc32(head) & 0xFFFF)
    deflate = zlib.compressobj(9, zlib.DEFLATED, -15)
    body = deflate.compress(payload) + deflate.flush()
    trailer = struct.pack("<II", zlib.crc32(payload) & 0xFFFFFFFF, len(payload) & 0xFFFFFFFF)
    return head + body + trailer


def add_tracks(path: Path, n: int, token: str = "", created: str = CREATED) -> list[int]:
    """Add n invented tracks after the highest id, each with a lyric, and return their ids."""
    con = sqlite3.connect(path)
    try:
        top = con.execute("SELECT coalesce(max(id), 0) FROM tracks").fetchone()[0]
        ids = [add_track(con, top + 1 + i, token, created) for i in range(n)]
        con.commit()
    finally:
        con.close()
    return ids


def add_named_track(path: Path, artist: str, title: str) -> int:
    """Add one invented track with these names, and a lyric, after the highest id."""
    con = sqlite3.connect(path)
    try:
        cursor = con.execute(
            "INSERT INTO tracks(name, name_lower, artist_name, artist_name_lower, album_name, "
            "album_name_lower, duration, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, 'Invented Album', 'invented album', 3.0, ?, ?)",
            (title, title.lower(), artist, artist.lower(), CREATED, CREATED),
        )
        track_id = cursor.lastrowid
        assert track_id is not None
        add_lyric(con, track_id)
        con.commit()
    finally:
        con.close()
    return track_id


def insert_tracks(path: Path, rows: list[tuple[str, str]], created: str = CREATED) -> list[int]:
    """Insert invented tracks, each with a lyric, into a database in the pinned server's schema.

    The server's own schema has FTS triggers and NOT NULL flags with defaults this fills or
    leaves to them, so the rows are the server's kind. Returns the new track ids.
    """
    con = sqlite3.connect(path)
    try:
        ids = []
        for artist, title in rows:
            cursor = con.execute(
                "INSERT INTO tracks(name, name_lower, artist_name, artist_name_lower, album_name, "
                "album_name_lower, duration, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, 'Invented Album', 'invented album', 180.0, ?, ?)",
                (title, title.lower(), artist, artist.lower(), created, created),
            )
            track_id = cursor.lastrowid
            assert track_id is not None
            con.execute(
                "INSERT INTO lyrics(plain_lyrics, synced_lyrics, track_id, has_plain_lyrics, "
                "has_synced_lyrics, instrumental, source, created_at, updated_at) "
                "VALUES('invented line', '[00:01.00] invented line', ?, 1, 1, 0, 'invented', ?, ?)",
                (track_id, created, created),
            )
            ids.append(track_id)
        con.commit()
    finally:
        con.close()
    return ids


def replace_lyrics(path: Path, ids: list[int]) -> None:
    """Give each track a new lyric row, as LRCLIB does: the trigger moves last_lyrics_id, and the
    track's updated_at is left alone."""
    con = sqlite3.connect(path)
    try:
        for track_id in ids:
            add_lyric(con, track_id, "2026-06-01 00:00:00")
        con.commit()
    finally:
        con.close()


def renumber(path: Path, by: int = 1_000_000) -> None:
    """Move every track to a new id, as a dump rebuilt from scratch might."""
    con = sqlite3.connect(path)
    try:
        con.execute("UPDATE tracks SET id = id + ?", (by,))
        con.execute("UPDATE lyrics SET track_id = track_id + ?", (by,))
        con.commit()
    finally:
        con.close()


def set_created(path: Path, table: str, row_id: int, created: str) -> None:
    """Set one row's created_at."""
    con = sqlite3.connect(path)
    try:
        con.execute(f"UPDATE {table} SET created_at = ? WHERE id = ?", (created, row_id))
        con.commit()
    finally:
        con.close()


def drop_index(path: Path, index: str = "idx_tracks_last_lyrics_id") -> Path:
    """Drop one of the fixture's indexes."""
    con = sqlite3.connect(path)
    try:
        con.execute(f"DROP INDEX {index}")
        con.commit()
    finally:
        con.close()
    return path


def lyric_pages(path: Path, page_size: int = 4096) -> list[int]:
    """Return the 1-based numbers of the pages that hold a lyric's text (CANARY)."""
    data = path.read_bytes()
    needle = CANARY.encode()
    return [
        n + 1
        for n in range(len(data) // page_size)
        if needle in data[n * page_size : (n + 1) * page_size]
    ]


def corrupt_page(path: Path, page: int, page_size: int = 4096) -> Path:
    """Write garbage over one page, as a bad disk or a cut-off copy would leave it."""
    data = bytearray(path.read_bytes())
    start = (page - 1) * page_size
    data[start : start + page_size] = b"\xa5" * page_size
    path.write_bytes(bytes(data))
    return path


def gz_bytes(path: Path) -> bytes:
    """Return a file gzipped with FNAME db.sqlite3, as the real dump carries."""
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="db.sqlite3", mode="wb", fileobj=buffer, mtime=0) as out:
        out.write(path.read_bytes())
    return buffer.getvalue()


def listed(key: str, size: int, uploaded: str = "2026-12-01T00:30:00.000Z") -> dict[str, object]:
    """Return one object as the R2 listing holds it."""
    return {
        "storageClass": "Standard",
        "uploaded": uploaded,
        "checksums": {},
        "httpEtag": '"0123456789abcdef0123456789abcdef-2"',
        "etag": "0123456789abcdef0123456789abcdef-2",
        "size": size,
        "version": "0123456789abcdef0123456789abcdef",
        "key": key,
    }


def listing(*objects: dict[str, object], truncated: bool = False) -> bytes:
    """Return the listing's body for these objects, in the R2 shape."""
    body = {"objects": list(objects), "truncated": truncated, "delimitedPrefixes": []}
    return json.dumps(body).encode()


@dataclass
class DumpServer:
    """Serve GET /listing and HEAD or GET of each dump by name, logging every request.

    listing_status and listing_body answer /listing. dumps maps a name to its bytes, served with
    206 for a Range. requests holds (method, path, Range header) in the order they came.
    """

    base_url: str = ""
    listing_status: int = 200
    listing_body: bytes = b'{"objects":[],"truncated":false,"delimitedPrefixes":[]}'
    listing_headers: dict[str, str] = field(default_factory=dict)
    dumps: dict[str, bytes] = field(default_factory=dict)
    # A status that answers every request for a dump instead of its bytes, such as 503.
    dump_status: int | None = None
    requests: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def listing_url(self) -> str:
        return f"{self.base_url}/listing"

    def offer(self, name: str, payload: bytes) -> None:
        """Serve a dump and list it as the only object."""
        self.dumps[name] = payload
        self.listing_body = listing(listed(name, len(payload)))

    def downloads(self) -> list[tuple[str, str, str]]:
        """Return each request for a dump that wasn't the 64 KB header probe."""
        return [entry for entry in self.dump_requests() if entry != ("GET", entry[1], PROBE)]

    def dump_requests(self) -> list[tuple[str, str, str]]:
        return [entry for entry in self.requests if entry[1] != "/listing"]


_RANGE = re.compile(r"bytes=(\d+)-(\d*)")
PROBE = "bytes=0-65535"


@pytest.fixture
def dump_server() -> Iterator[DumpServer]:
    with running_dump_server() as served:
        yield served


@contextmanager
def running_dump_server() -> Iterator[DumpServer]:
    """Run a DumpServer on a free loopback port for the length of the block."""
    served = DumpServer()

    class Handler(BaseHTTPRequestHandler):
        def answer(self, method: str) -> None:
            path = self.path.split("?", 1)[0]
            served.requests.append((method, path, self.headers.get("Range", "")))
            if path == "/listing":
                body = served.listing_body
                self.send_response(served.listing_status)
                for name, value in served.listing_headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if method == "GET":
                    self.wfile.write(body)
                return
            if served.dump_status is not None:
                self.send_response(served.dump_status)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = served.dumps.get(path.lstrip("/"))
            if payload is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            asked = _RANGE.fullmatch(self.headers.get("Range", ""))
            start = int(asked.group(1)) if asked else 0
            end = int(asked.group(2)) if asked and asked.group(2) else len(payload) - 1
            end = min(end, len(payload) - 1)
            body = payload[start : end + 1]
            self.send_response(206 if asked else 200)
            if asked:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header(
                "Content-Length", str(len(body) if method == "GET" or asked else len(payload))
            )
            self.end_headers()
            if method == "GET":
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

        def do_GET(self) -> None:
            self.answer("GET")

        def do_HEAD(self) -> None:
            self.answer("HEAD")

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    served.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield served
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
