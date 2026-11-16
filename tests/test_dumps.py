"""The dump listing read defensively, in process with responses, and lrclib-check through the CLI.

Every listing is invented in the R2 shape LRCLIB's db-dumps page reads. No real listing is fetched.
"""

import gzip
import io
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import requests
import responses
from hypothesis import given
from hypothesis import strategies as st

from conftest import CANARY, RunCli, StubLrclib, copy_track, state_db_path
from lrclib_fixture import (  # noqa: F401
    FCOMMENT,
    FEXTRA,
    FHCRC,
    FNAME,
    FTEXT,
    PROBE,
    DumpServer,
    dump_server,
    gz_bytes,
    gz_with_header,
    listed,
    listing,
    make_db,
)
from plyrix import dumps

if TYPE_CHECKING:
    from _typeshed import WriteableBuffer

URL = "http://listing.test/listing"
OLD = "lrclib-db-dump-20261030T182259Z.sqlite3.gz"
NEW = "lrclib-db-dump-20261201T000000Z.sqlite3.gz"
NEWEST = "lrclib-db-dump-20261215T120000Z.sqlite3.gz"
# The listing as it read on 2026-11-15 (06-RESEARCH.md).
R2 = {
    "objects": [
        {
            "storageClass": "Standard",
            "uploaded": "2026-10-30T18:52:01.641Z",
            "checksums": {},
            "httpEtag": '"64768155181f5eba7412f7b69145c803-2937"',
            "etag": "64768155181f5eba7412f7b69145c803-2937",
            "size": 49261576112,
            "version": "7e5e71f2f374b93d6ec18b22a5c17443",
            "key": OLD,
        }
    ],
    "truncated": False,
    "delimitedPrefixes": [],
}


@pytest.fixture
def mock() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock() as requests_mock:
        yield requests_mock


def read(mock: responses.RequestsMock, body: object, status: int = 200) -> dumps.Listing:
    payload = body if isinstance(body, bytes) else json.dumps(body).encode()
    mock.add(responses.GET, URL, body=payload, status=status)
    return dumps.read_listing(dumps.new_session(), URL)


def test_the_r2_listing_gives_its_dump(mock: responses.RequestsMock) -> None:
    found = read(mock, R2)

    assert found.problem is None
    assert found.newest == dumps.Listed(
        OLD, 49261576112, "64768155181f5eba7412f7b69145c803-2937", "2026-10-30T18:52:01.641Z"
    )


def test_the_latest_key_wins_whatever_the_order(mock: responses.RequestsMock) -> None:
    body = json.loads(listing(listed(NEW, 5), listed(NEWEST, 6), listed(OLD, 7)))

    found = read(mock, body)

    assert found.newest is not None and found.newest.key == NEWEST


def test_a_good_object_beside_bad_ones_is_still_found(mock: responses.RequestsMock) -> None:
    body = {"objects": [1, {"key": "x", "size": 3}, {"key": NEW}, listed(NEW, 9)]}

    found = read(mock, body)

    assert found.newest is not None and found.newest.key == NEW and found.newest.size == 9


def test_missing_optional_fields_read_as_none(mock: responses.RequestsMock) -> None:
    found = read(mock, {"objects": [{"key": NEW, "size": 9, "etag": 4}]})

    assert found.newest == dumps.Listed(NEW, 9, None, None)


@pytest.mark.parametrize(
    "body",
    [
        {"objects": []},
        {"objects": "not a list"},
        {"no": "objects"},
        [listed(NEW, 5)],
        {"objects": [listed(f"../{NEW}", 5)]},
        {"objects": [listed(f"{NEW}\n", 5)]},
        {"objects": [listed("lrclib-db-dump-2026120T000000Z.sqlite3.gz", 5)]},
        {"objects": [listed("lrclib-db-dump-20261399T000000Z.sqlite3.gz", 5)]},
        {"objects": [listed(f"dl/{NEW}", 5)]},
        {"objects": [listed(NEW, True)]},
        {"objects": [listed(NEW, "49261576112")]},  # type: ignore[arg-type]
        {"objects": [listed(NEW, 0)]},
        {"objects": [listed(NEW, -5)]},
        {"objects": [listed(NEW, 10**12)]},
        {"objects": [listed(NEW, 5.0)]},  # type: ignore[arg-type]
    ],
)
def test_a_listing_with_no_usable_dump_is_no_update(
    mock: responses.RequestsMock, body: object
) -> None:
    found = read(mock, body)

    assert found.newest is None
    assert found.problem


@pytest.mark.parametrize(
    ("status", "body", "phrase"),
    [
        (404, b"<html>not found</html>", "HTTP 404"),
        (200, b"<html>a page</html>", "isn't JSON"),
        (200, b"", "isn't JSON"),
        (302, b"", "redirect"),
        (500, CANARY.encode(), "HTTP 500"),
    ],
)
def test_a_listing_that_fails_is_no_update(
    mock: responses.RequestsMock, status: int, body: bytes, phrase: str
) -> None:
    found = read(mock, body, status)

    assert found.newest is None
    assert found.problem is not None and phrase in found.problem
    assert CANARY not in found.problem


def test_a_redirect_is_not_followed(mock: responses.RequestsMock) -> None:
    mock.add(responses.GET, URL, status=302, headers={"Location": "http://elsewhere.test/x"})

    found = dumps.read_listing(dumps.new_session(), URL)

    assert found.newest is None
    assert len(mock.calls) == 1


def test_a_503_is_asked_again_then_is_no_update(
    mock: responses.RequestsMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("urllib3.util.retry.Retry.sleep", lambda *args, **kwargs: None)
    for _ in range(3):
        mock.add(responses.GET, URL, status=503)

    found = dumps.read_listing(dumps.new_session(), URL)

    assert found.newest is None
    assert found.problem == "the listing answered HTTP 503"
    assert len(mock.calls) == 3


def test_a_listing_over_one_megabyte_is_no_update(mock: responses.RequestsMock) -> None:
    body = b" " * (dumps.MAX_LISTING_BYTES + 1)

    found = read(mock, body)

    assert found.newest is None
    assert found.problem == "the listing is more than 1 MB"


def test_a_listing_of_exactly_one_megabyte_is_read(mock: responses.RequestsMock) -> None:
    text = json.dumps({"objects": [listed(NEW, 9)]}).encode()
    body = text + b" " * (dumps.MAX_LISTING_BYTES - len(text))

    found = read(mock, body)

    assert found.newest is not None and found.newest.key == NEW


def test_a_truncated_listing_with_no_dump_says_so(mock: responses.RequestsMock) -> None:
    found = read(mock, {"objects": [], "truncated": True})

    assert found.newest is None
    assert found.truncated
    assert found.problem is not None and "cut short" in found.problem


def test_a_dump_time_comes_from_its_name() -> None:
    assert dumps.dump_time(OLD) == datetime(2026, 10, 30, 18, 22, 59, tzinfo=UTC)
    assert dumps.dump_time(OLD).isoformat() == "2026-10-30T18:22:59+00:00"
    with pytest.raises(ValueError):
        dumps.dump_time("db.sqlite3")


def test_newest_reads_only_dicts() -> None:
    assert dumps.newest(None) is None
    assert dumps.newest("objects") is None
    assert dumps.newest({"objects": [listed(NEW, 5)]}) == dumps.Listed(
        NEW, 5, "0123456789abcdef0123456789abcdef-2", "2026-12-01T00:30:00.000Z"
    )


# lrclib-check, through the real console script and a local listing


def check_env(tmp_path: Path, stub: StubLrclib, **extra: str) -> dict[str, str]:
    lib = tmp_path / "lib"
    copy_track(lib)
    return {"PLYRIX_MUSIC_ROOT": str(lib), "PLYRIX_LRCLIB_URL": stub.base_url, **extra}


def check_args(server: DumpServer, *extra: str) -> list[str]:
    return ["lrclib-check", "--listing", server.listing_url, "--base", server.base_url, *extra]


def offer_db(server: DumpServer, tmp_path: Path, name: str, user_version: int = 8) -> Path:
    path = make_db(tmp_path / f"served-{name}.sqlite3", 4, user_version=user_version)
    server.offer(name, gz_bytes(path))
    return path


def test_lrclib_check_prints_an_update_with_the_headers_size_and_version(
    run_cli: RunCli,
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    dump_server: DumpServer,  # noqa: F811
) -> None:
    served = offer_db(dump_server, tmp_path, NEW)
    size = len(dump_server.dumps[NEW])

    proc = run_cli(check_args(dump_server), check_env(tmp_path, stub_lrclib, PLYRIX_DUMP_ID=OLD))

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.decode().splitlines() == [f"update {NEW} {size} {served.stat().st_size} 8"]
    assert dump_server.requests == [("GET", "/listing", ""), ("GET", f"/{NEW}", PROBE)]
    assert stub_lrclib.requests == [] and stub_lrclib.probes == []
    assert not state_db_path(tmp_path).exists()


@pytest.mark.parametrize("listed_name", [OLD, "lrclib-db-dump-20260101T000000Z.sqlite3.gz"])
def test_lrclib_check_prints_none_for_a_dump_no_newer(
    run_cli: RunCli,
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    dump_server: DumpServer,  # noqa: F811
    listed_name: str,
) -> None:
    dump_server.listing_body = listing(listed(listed_name, 1234))

    proc = run_cli(check_args(dump_server), check_env(tmp_path, stub_lrclib, PLYRIX_DUMP_ID=OLD))

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.decode().splitlines() == ["none"]
    assert proc.stderr == b""
    assert dump_server.requests == [("GET", "/listing", "")]


@pytest.mark.parametrize("version", [0, 9, 1000])
def test_a_dump_too_new_or_unversioned_is_refused_before_download(
    run_cli: RunCli,
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    dump_server: DumpServer,  # noqa: F811
    version: int,
) -> None:
    offer_db(dump_server, tmp_path, NEW, user_version=version)

    proc = run_cli(check_args(dump_server), check_env(tmp_path, stub_lrclib, PLYRIX_DUMP_ID=OLD))

    assert proc.returncode == 1
    assert proc.stdout.decode().splitlines() == [f"refused {NEW}"]
    line = proc.stderr.decode().strip()
    assert len(line.splitlines()) == 1
    assert f"user_version is {version}" in line and "1 to 8" in line and "e1d863a" in line
    assert dump_server.requests == [("GET", "/listing", ""), ("GET", f"/{NEW}", PROBE)]


def test_a_dump_that_isnt_sqlite_is_refused_before_download(
    run_cli: RunCli,
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    dump_server: DumpServer,  # noqa: F811
) -> None:
    dump_server.offer(NEW, gzip.compress(b"not a database " * 100))

    proc = run_cli(check_args(dump_server), check_env(tmp_path, stub_lrclib, PLYRIX_DUMP_ID=OLD))

    assert proc.returncode == 1
    assert proc.stdout.decode().splitlines() == [f"refused {NEW}"]
    assert b"doesn't hold an SQLite database" in proc.stderr


def test_a_version_the_server_migrates_is_an_update(
    run_cli: RunCli,
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    dump_server: DumpServer,  # noqa: F811
) -> None:
    offer_db(dump_server, tmp_path, NEW, user_version=7)

    proc = run_cli(check_args(dump_server), check_env(tmp_path, stub_lrclib, PLYRIX_DUMP_ID=OLD))

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.decode().split()[0] == "update"
    assert proc.stdout.decode().split()[-1] == "7"


def test_a_probe_that_fails_is_no_update(
    run_cli: RunCli,
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    dump_server: DumpServer,  # noqa: F811
) -> None:
    offer_db(dump_server, tmp_path, NEW)
    dump_server.dump_status = 404

    proc = run_cli(check_args(dump_server), check_env(tmp_path, stub_lrclib, PLYRIX_DUMP_ID=OLD))

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == b"none\n"
    assert proc.stderr.decode().splitlines() == [
        "plyrix: the newest dump's header couldn't be read (HTTP 404), so no update was tried"
    ]
    assert dump_server.downloads() == []


@pytest.mark.parametrize(
    "args",
    [
        ["--listing", "ftp://x/listing", "--base", "http://x"],
        ["--listing", "", "--base", "http://x"],
        ["--listing", "http://", "--base", "http://x"],
        ["--listing", "listing", "--base", "http://x"],
        ["--listing", "http://x/listing", "--base", "file:///x"],
        ["--listing", "http://x/listing", "--base", ""],
    ],
)
def test_lrclib_check_refuses_a_url_that_isnt_http(
    run_cli: RunCli, tmp_path: Path, stub_lrclib: StubLrclib, args: list[str]
) -> None:
    proc = run_cli(["lrclib-check", *args], check_env(tmp_path, stub_lrclib, PLYRIX_DUMP_ID=OLD))

    assert proc.returncode == 2
    assert b"--listing" in proc.stderr or b"--base" in proc.stderr


def test_lrclib_check_with_no_live_dump_known_exits_2(
    run_cli: RunCli,
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    dump_server: DumpServer,  # noqa: F811
) -> None:
    proc = run_cli(check_args(dump_server), check_env(tmp_path, stub_lrclib))

    assert proc.returncode == 2
    lines = proc.stderr.decode().splitlines()
    assert len(lines) == 1
    assert "dump_id (PLYRIX_DUMP_ID)" in lines[0] and "lrclib-record" in lines[0]
    assert dump_server.requests == []
    assert not state_db_path(tmp_path).exists()


def test_a_listing_that_errors_is_no_update_and_leaks_no_response_text(
    run_cli: RunCli,
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    dump_server: DumpServer,  # noqa: F811
) -> None:
    dump_server.listing_status = 500
    dump_server.listing_body = f"<html>{CANARY}</html>".encode()

    proc = run_cli(check_args(dump_server), check_env(tmp_path, stub_lrclib, PLYRIX_DUMP_ID=OLD))

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == b"none\n"
    assert len(proc.stderr.decode().splitlines()) == 1
    assert b"HTTP 500" in proc.stderr
    assert CANARY.encode() not in proc.stdout + proc.stderr


# The SQLite header inside a dump's first 64 KB


def sqlite_file(tmp_path: Path) -> bytes:
    return make_db(tmp_path / "h.sqlite3", 3).read_bytes()


def test_the_fixture_gz_gives_its_version_and_exact_size(tmp_path: Path) -> None:
    path = make_db(tmp_path / "h.sqlite3", 40)

    header = dumps.sqlite_header(gz_bytes(path)[: dumps.PROBE_BYTES])

    assert header is not None
    assert header.user_version == 8
    assert header.page_size == 4096
    assert header.unpacked == path.stat().st_size


@pytest.mark.parametrize(
    "flags",
    [0, FTEXT, FEXTRA, FNAME, FCOMMENT, FHCRC, FEXTRA | FNAME | FCOMMENT | FHCRC | FTEXT],
)
def test_every_optional_gzip_header_field_is_skipped(tmp_path: Path, flags: int) -> None:
    payload = sqlite_file(tmp_path)
    gz = gz_with_header(payload, flags)
    assert gzip.decompress(gz) == payload

    header = dumps.sqlite_header(gz)

    assert header is not None and header.user_version == 8
    assert header.unpacked == len(payload)


def test_a_page_size_field_of_1_means_65536(tmp_path: Path) -> None:
    payload = bytearray(sqlite_file(tmp_path))
    payload[16:18] = (1).to_bytes(2, "big")

    header = dumps.sqlite_header(gz_with_header(bytes(payload), FNAME))

    assert header is not None and header.page_size == 65_536


def test_a_header_whose_counters_differ_says_its_size_isnt_valid(tmp_path: Path) -> None:
    payload = bytearray(sqlite_file(tmp_path))
    fresh = dumps.sqlite_header(gz_with_header(bytes(payload), 0))
    assert fresh is not None and fresh.size_valid
    # SQLite trusts the in-header size only while offset 24 equals offset 92.
    payload[92:96] = (int.from_bytes(payload[24:28], "big") + 1).to_bytes(4, "big")

    header = dumps.sqlite_header(gz_with_header(bytes(payload), 0))

    assert header is not None and not header.size_valid


@pytest.mark.parametrize(
    "gz",
    [
        b"",
        b"\x1f\x8b\x08",
        b"not gzip at all, not even close",
        gzip.compress(b"plain text that isn't SQLite " * 10),
        gzip.compress(b"SQLite format 3\x00" + b"\x00" * 20),
        b"\x1f\x8b\x08\x08" + b"\x00" * 6 + b"db.sqlite3",
        b"\x1f\x8b\x08\x04" + b"\x00" * 6 + b"\xff\xff",
        b"\x1f\x8b\x08\x00" + b"\x00" * 6 + b"\xff" * 30,
    ],
)
def test_bytes_that_arent_a_gzipped_sqlite_header_give_none(gz: bytes) -> None:
    assert dumps.sqlite_header(gz) is None


@given(st.binary(max_size=400))
def test_any_bytes_give_a_header_or_none(data: bytes) -> None:
    for gz in (data, b"\x1f\x8b\x08" + data):
        assert dumps.sqlite_header(gz) is None or isinstance(dumps.sqlite_header(gz), dumps.Header)


class CountingStream(io.BufferedReader):
    """A response body that counts every byte the client reads from it."""

    def __init__(self, payload: bytes) -> None:
        super().__init__(io.BytesIO(payload))
        self.taken = 0

    def read(self, size: int | None = -1) -> bytes:
        data = super().read(size)
        self.taken += len(data)
        return data

    def read1(self, size: int = -1) -> bytes:
        data = super().read1(size)
        self.taken += len(data)
        return data

    def readinto(self, buffer: "WriteableBuffer") -> int:
        count = super().readinto(buffer)
        self.taken += count
        return count


DUMP_URL = "http://dumps.test/" + NEW


def test_probe_header_parses_a_206(mock: responses.RequestsMock, tmp_path: Path) -> None:
    gz = gz_bytes(make_db(tmp_path / "h.sqlite3", 3))
    mock.add(responses.GET, DUMP_URL, body=gz[: dumps.PROBE_BYTES], status=206)

    header = dumps.probe_header(dumps.new_session(), DUMP_URL)

    assert isinstance(header, dumps.Header) and header.user_version == 8
    assert mock.calls[0].request.headers["Range"] == "bytes=0-65535"


def test_a_200_with_the_whole_file_is_read_for_64_kb_only(
    mock: responses.RequestsMock, tmp_path: Path
) -> None:
    gz = gz_bytes(make_db(tmp_path / "h.sqlite3", 3))
    stream = CountingStream(gz + os.urandom(3 * 1024 * 1024))
    mock.add(responses.GET, DUMP_URL, body=stream, status=200, auto_calculate_content_length=False)

    header = dumps.probe_header(dumps.new_session(), DUMP_URL)

    assert isinstance(header, dumps.Header)
    assert 0 < stream.taken <= dumps.PROBE_BYTES


@pytest.mark.parametrize("status", [404, 503, 302])
def test_a_probe_that_isnt_answered_gives_a_problem(
    mock: responses.RequestsMock, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setattr("urllib3.util.retry.Retry.sleep", lambda *args, **kwargs: None)
    for _ in range(3 if status == 503 else 1):
        mock.add(responses.GET, DUMP_URL, status=status, headers={"Location": "http://x.test/"})

    found = dumps.probe_header(dumps.new_session(), DUMP_URL)

    assert isinstance(found, str) and found != dumps.NOT_SQLITE


def test_a_probe_that_cant_connect_gives_a_problem(mock: responses.RequestsMock) -> None:
    mock.add(responses.GET, DUMP_URL, body=requests.ConnectionError("refused"))

    assert dumps.probe_header(dumps.new_session(), DUMP_URL) == "no answer"


def stale_size_gz(tmp_path: Path) -> bytes:
    payload = bytearray(make_db(tmp_path / "stale.sqlite3", 3).read_bytes())
    payload[92:96] = (int.from_bytes(payload[24:28], "big") + 1).to_bytes(4, "big")
    return gzip.compress(bytes(payload))


def test_a_probe_of_a_stale_size_says_so(mock: responses.RequestsMock, tmp_path: Path) -> None:
    mock.add(responses.GET, DUMP_URL, body=stale_size_gz(tmp_path)[: dumps.PROBE_BYTES], status=206)

    assert dumps.probe_header(dumps.new_session(), DUMP_URL) == dumps.STALE_SIZE


def test_lrclib_check_tries_no_update_when_the_size_is_stale(
    run_cli: RunCli,
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    dump_server: DumpServer,  # noqa: F811
) -> None:
    dump_server.offer(NEW, stale_size_gz(tmp_path))

    proc = run_cli(check_args(dump_server), check_env(tmp_path, stub_lrclib, PLYRIX_DUMP_ID=OLD))

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == b"none\n"
    assert dumps.STALE_SIZE in proc.stderr.decode()
    assert dump_server.downloads() == []


def test_a_probe_of_bytes_that_arent_sqlite_says_so(mock: responses.RequestsMock) -> None:
    mock.add(responses.GET, DUMP_URL, body=gzip.compress(b"x" * 500), status=206)

    assert dumps.probe_header(dumps.new_session(), DUMP_URL) == dumps.NOT_SQLITE
