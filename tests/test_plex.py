"""The Plex client in this process, against responses: its safety rules, and the tripwire."""

import json
import logging
import socket
from collections.abc import Iterator, Sequence

import pytest
import requests
import responses
from responses import matchers

from conftest import (
    CANARY,
    LOCAL_LYRICS,
    LYRICFIND,
    plex_lyric_stream,
    plex_metadata,
    plex_stream_body,
)
from plyrix import plex
from plyrix.domain import Kind

BASE = "http://pms.test:32400"
TOKEN = "plyrix-dummy-token-0123456789"
REFRESH = f"{BASE}/library/metadata/42/refresh"
SECTION = 7
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy")


@pytest.fixture
def pms() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock() as mock:
        yield mock


def test_plex_refresh_sends_the_token_in_a_header_and_an_empty_query(
    pms: responses.RequestsMock,
) -> None:
    pms.add(
        responses.PUT,
        REFRESH,
        status=200,
        match=[
            matchers.header_matcher({"X-Plex-Token": TOKEN}),
            matchers.query_param_matcher({}),
        ],
    )

    assert plex.refresh(plex.new_session(TOKEN), BASE, "42") is plex.Reply.OK
    assert len(pms.calls) == 1
    assert TOKEN not in str(pms.calls[0].request.url)


def test_plex_redirect_to_plex_tv_is_never_followed(pms: responses.RequestsMock) -> None:
    pms.add(responses.PUT, REFRESH, status=302, headers={"Location": "https://plex.tv/x"})

    assert plex.refresh(plex.new_session(TOKEN), BASE, "42") is not plex.Reply.OK
    assert len(pms.calls) == 1


def test_plex_redirect_on_a_listing_is_never_followed(pms: responses.RequestsMock) -> None:
    pms.add(
        responses.GET,
        f"{BASE}/library/sections/{SECTION}/all",
        status=301,
        headers={"Location": "https://plex.tv/x"},
    )

    reply, tracks = plex.list_tracks(plex.new_session(TOKEN), BASE, SECTION)

    assert reply is not plex.Reply.OK
    assert tracks == []
    assert len(pms.calls) == 1


def test_plex_proxy_variables_are_ignored(
    pms: responses.RequestsMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in PROXY_VARIABLES:
        monkeypatch.setenv(name, "http://proxy.invalid:3128")
    pms.add(responses.PUT, REFRESH, status=200)
    session = plex.new_session(TOKEN)

    assert session.trust_env is False
    assert plex.refresh(session, BASE, "42") is plex.Reply.OK
    assert pms.calls[0].request.url == REFRESH


@pytest.mark.parametrize("token", [None, ""])
def test_plex_no_token_sends_no_token_header(
    pms: responses.RequestsMock, token: str | None
) -> None:
    pms.add(responses.PUT, REFRESH, status=200)

    assert plex.refresh(plex.new_session(token), BASE, "42") is plex.Reply.OK
    assert "X-Plex-Token" not in pms.calls[0].request.headers


def test_plex_session_identifies_itself_and_asks_for_json(pms: responses.RequestsMock) -> None:
    pms.add(responses.PUT, REFRESH, status=200)

    plex.refresh(plex.new_session(TOKEN), f"{BASE}/", "42")

    headers = pms.calls[0].request.headers
    assert headers["Accept"] == "application/json"
    assert headers["X-Plex-Client-Identifier"] == "plyrix"
    assert headers["X-Plex-Product"] == "plyrix"
    assert pms.calls[0].request.url == REFRESH


@pytest.mark.parametrize("key", ["", "4/2", "42?x=1", "../1", "٤٢", "-1"])
def test_plex_refresh_sends_nothing_for_a_key_that_is_not_a_number(
    pms: responses.RequestsMock, key: str
) -> None:
    assert plex.refresh(plex.new_session(TOKEN), BASE, key) is not plex.Reply.OK
    assert len(pms.calls) == 0


def test_plex_token_is_not_in_the_session_repr() -> None:
    assert TOKEN not in repr(plex.new_session(TOKEN))


# The suite-wide tripwire from conftest: no test can resolve plex.tv.


@pytest.fixture
def no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.usefixtures("no_proxy")
def test_plex_tripwire_stops_requests_reaching_plex_tv() -> None:
    with pytest.raises(AssertionError, match="plex.tv"):
        requests.get("https://app.plex.tv/", timeout=5)


@pytest.mark.parametrize("host", ["plex.tv", "PLEX.TV.", "app.plex.tv", b"plex.tv", b"x.Plex.Tv."])
def test_plex_tripwire_refuses_every_spelling(host: str | bytes) -> None:
    with pytest.raises(AssertionError, match="plex.tv"):
        socket.getaddrinfo(host, 443)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_plex_tripwire_lets_other_hosts_resolve(host: str) -> None:
    assert socket.getaddrinfo(host, 80)


# Listing: paging, bytes, odd shapes and the status map.

LISTING = f"{BASE}/library/sections/{SECTION}/all"
SECTIONS = f"{BASE}/library/sections"


def track_item(key: int, album: int, file: str) -> dict[str, object]:
    return {
        "ratingKey": str(key),
        "parentRatingKey": str(album),
        "title": "Fixture Title",
        "Media": [{"Part": [{"file": file}]}],
    }


def listing_body(items: Sequence[object], **container: object) -> bytes:
    page = {"size": len(items), "totalSize": len(items), "offset": 0, "Metadata": list(items)}
    return json.dumps({"MediaContainer": {**page, **container}}).encode()


def test_plex_listing_pages_a_large_section(pms: responses.RequestsMock) -> None:
    items = [track_item(i, 1 + i // 10, f"/m/Album {i // 10}/{i}.flac") for i in range(2500)]

    def page(request: requests.PreparedRequest) -> tuple[int, dict[str, str], bytes]:
        start = int(request.headers["X-Plex-Container-Start"])
        size = int(request.headers["X-Plex-Container-Size"])
        chunk = items[start : start + size]
        return 200, {}, listing_body(chunk, totalSize=len(items), offset=start)

    pms.add_callback(responses.GET, LISTING, callback=page)

    reply, tracks = plex.list_tracks(plex.new_session(TOKEN), BASE, SECTION)

    assert reply is plex.Reply.OK
    assert len(tracks) == 2500
    starts = [call.request.headers["X-Plex-Container-Start"] for call in pms.calls]
    assert starts == ["0", "1000", "2000"]


def test_plex_listing_from_a_server_that_ignores_paging_takes_one_request(
    pms: responses.RequestsMock,
) -> None:
    items: list[object] = [track_item(i, 1, f"/m/A/{i}.flac") for i in range(1500)]
    pms.add(responses.GET, LISTING, body=listing_body(items))

    reply, tracks = plex.list_tracks(plex.new_session(TOKEN), BASE, SECTION)

    assert reply is plex.Reply.OK
    assert len(tracks) == 1500
    assert len(pms.calls) == 1


def untotalled_body(items: Sequence[object]) -> bytes:
    page = {"size": len(items), "offset": 0, "Metadata": list(items)}
    return json.dumps({"MediaContainer": page}).encode()


def test_plex_listing_without_a_total_fails_on_a_full_page(pms: responses.RequestsMock) -> None:
    # A full page could be a truncated listing, which could hide a folder's second album.
    items: list[object] = [track_item(i, 1, f"/m/A/{i}.flac") for i in range(4)]
    pms.add(responses.GET, LISTING, body=untotalled_body(items))

    reply, tracks = plex.list_tracks(plex.new_session(TOKEN), BASE, SECTION, page_size=4)

    assert reply is plex.Reply.UNAVAILABLE
    assert tracks == []
    assert len(pms.calls) == 1


def test_plex_listing_without_a_total_takes_a_short_page_as_complete(
    pms: responses.RequestsMock,
) -> None:
    items: list[object] = [track_item(i, 1, f"/m/A/{i}.flac") for i in range(3)]
    pms.add(responses.GET, LISTING, body=untotalled_body(items))

    reply, tracks = plex.list_tracks(plex.new_session(TOKEN), BASE, SECTION, page_size=4)

    assert reply is plex.Reply.OK
    assert len(tracks) == 3


def test_plex_listing_without_a_total_takes_more_than_a_page_as_complete(
    pms: responses.RequestsMock,
) -> None:
    # A server that ignores X-Plex-Container-Size returns more than asked for: the whole listing.
    items: list[object] = [track_item(i, 1, f"/m/A/{i}.flac") for i in range(1500)]
    pms.add(responses.GET, LISTING, body=untotalled_body(items))

    reply, tracks = plex.list_tracks(plex.new_session(TOKEN), BASE, SECTION)

    assert reply is plex.Reply.OK
    assert len(tracks) == 1500
    assert len(pms.calls) == 1


def test_plex_listing_without_a_total_still_samples_a_page(pms: responses.RequestsMock) -> None:
    items: list[object] = [track_item(i, 1, f"/m/A/{i}.flac") for i in range(4)]
    pms.add(responses.GET, LISTING, body=untotalled_body(items))

    reply, tracks = plex.list_tracks(plex.new_session(TOKEN), BASE, SECTION, page_size=4, pages=1)

    assert reply is plex.Reply.OK
    assert len(tracks) == 4


def test_plex_listing_stops_on_an_empty_page_before_the_total(
    pms: responses.RequestsMock,
) -> None:
    pms.add(responses.GET, LISTING, body=listing_body([], totalSize=5))

    reply, tracks = plex.list_tracks(plex.new_session(TOKEN), BASE, SECTION)

    # A short listing could hide a folder's second album, and clearing it would strand that album.
    assert reply is plex.Reply.UNAVAILABLE
    assert tracks == []
    assert len(pms.calls) == 1


def test_plex_listing_stops_on_an_offset_that_does_not_advance(
    pms: responses.RequestsMock,
) -> None:
    items: list[object] = [track_item(i, 1, f"/m/A/{i}.flac") for i in range(1000)]
    # Every page claims to start at 0, whatever was asked for.
    pms.add(responses.GET, LISTING, body=listing_body(items, totalSize=2500, offset=0))

    reply, tracks = plex.list_tracks(plex.new_session(TOKEN), BASE, SECTION)

    assert reply is plex.Reply.UNAVAILABLE
    assert tracks == []
    assert len(pms.calls) == 2


def test_plex_listing_keeps_raw_non_utf8_bytes_in_a_path(pms: responses.RequestsMock) -> None:
    body = (
        b'{"MediaContainer": {"size": 1, "totalSize": 1, "Metadata": [{"ratingKey": "5", '
        b'"parentRatingKey": "6", "Media": [{"Part": [{"file": "/m/A\xff/t\xfe.flac"}]}]}]}}'
    )
    pms.add(responses.GET, LISTING, body=body)

    reply, tracks = plex.list_tracks(plex.new_session(TOKEN), BASE, SECTION)

    assert reply is plex.Reply.OK
    assert tracks[0].files == (b"/m/A\xff/t\xfe.flac",)
    assert plex.folder_albums(tracks) == {b"/m/A\xff": {"6"}}


def test_plex_listing_never_normalises_a_decomposed_name(pms: responses.RequestsMock) -> None:
    decomposed = "/m/Béla/t.flac"
    pms.add(responses.GET, LISTING, body=listing_body([track_item(1, 2, decomposed)]))

    _, tracks = plex.list_tracks(plex.new_session(TOKEN), BASE, SECTION)

    assert tracks[0].files == (decomposed.encode(),)
    assert b"e\xcc\x81" in tracks[0].files[0]


def test_plex_listing_skips_items_it_cannot_read(pms: responses.RequestsMock) -> None:
    good = track_item(1, 2, "/m/A/t.flac")
    odd: list[object] = [
        "not an item",
        {"ratingKey": "3", "Media": [{"Part": [{"file": "/m/B/t.flac"}]}]},
        {**good, "ratingKey": "4", "Media": [{"Part": [{"file": 5}]}]},
        {**good, "ratingKey": "5", "Media": {"Part": []}},
        {**good, "ratingKey": "6", "parentRatingKey": "7a"},
        {**good, "ratingKey": "٨", "parentRatingKey": "9"},
        {**good, "ratingKey": "10", "parentRatingKey": True},
    ]
    pms.add(responses.GET, LISTING, body=listing_body([good, *odd]))

    reply, tracks = plex.list_tracks(plex.new_session(TOKEN), BASE, SECTION)

    assert reply is plex.Reply.OK
    assert plex.folder_albums(tracks) == {b"/m/A": {"2"}}


@pytest.mark.parametrize(
    "body", [b"not json", b"[]", b'{"other": {}}', b'{"MediaContainer": []}', b"\xff\xfe"]
)
def test_plex_listing_without_a_container_is_unavailable(
    pms: responses.RequestsMock, body: bytes
) -> None:
    pms.add(responses.GET, LISTING, body=body)

    assert plex.list_tracks(plex.new_session(TOKEN), BASE, SECTION) == (plex.Reply.UNAVAILABLE, [])


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (404, plex.Reply.MISSING),
        (401, plex.Reply.REFUSED),
        (403, plex.Reply.REFUSED),
        (500, plex.Reply.UNAVAILABLE),
        (requests.Timeout(), plex.Reply.UNAVAILABLE),
        (requests.ConnectionError(), plex.Reply.UNAVAILABLE),
    ],
)
def test_plex_listing_status_map(
    pms: responses.RequestsMock, answer: int | Exception, expected: plex.Reply
) -> None:
    if isinstance(answer, int):
        pms.add(responses.GET, LISTING, status=answer)
    else:
        pms.add(responses.GET, LISTING, body=answer)

    assert plex.list_tracks(plex.new_session(TOKEN), BASE, SECTION) == (expected, [])


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (200, plex.Reply.OK),
        (204, plex.Reply.OK),
        (401, plex.Reply.REFUSED),
        (403, plex.Reply.REFUSED),
        (302, plex.Reply.UNAVAILABLE),
        (404, plex.Reply.UNAVAILABLE),
        (500, plex.Reply.UNAVAILABLE),
        (503, plex.Reply.UNAVAILABLE),
        (requests.Timeout(), plex.Reply.UNAVAILABLE),
    ],
)
def test_plex_refresh_status_map_and_never_a_retried_put(
    pms: responses.RequestsMock, answer: int | Exception, expected: plex.Reply
) -> None:
    if isinstance(answer, int):
        pms.add(responses.PUT, REFRESH, status=answer)
    else:
        pms.add(responses.PUT, REFRESH, body=answer)

    assert plex.refresh(plex.new_session(TOKEN), BASE, "42") is expected
    assert len(pms.calls) == 1


# section: the library's type and locations, from the full list.


def section_dir(key: str, kind: str, *paths: str) -> dict[str, object]:
    return {
        "key": key,
        "type": kind,
        "agent": "tv.plex.agents.music",
        "title": "Fixture Library",
        "Location": [{"id": i, "path": path} for i, path in enumerate(paths)],
    }


def sections_body(*directories: object) -> bytes:
    return json.dumps(
        {"MediaContainer": {"size": len(directories), "Directory": list(directories)}}
    ).encode()


def test_plex_section_reads_a_music_library(pms: responses.RequestsMock) -> None:
    pms.add(
        responses.GET,
        SECTIONS,
        body=sections_body(
            section_dir("2", "movie", "/v"), section_dir("1", "artist", "/m/a", "/m/b")
        ),
    )

    reply, info = plex.section(plex.new_session(TOKEN), BASE, 1)

    assert reply is plex.Reply.OK
    assert info == plex.SectionInfo(
        key="1", type="artist", agent="tv.plex.agents.music", locations=(b"/m/a", b"/m/b")
    )
    assert pms.calls[0].request.headers["X-Plex-Token"] == TOKEN


def test_plex_section_keeps_raw_bytes_in_a_location(pms: responses.RequestsMock) -> None:
    body = (
        b'{"MediaContainer": {"Directory": [{"key": "1", "type": "artist", "agent": "a", '
        b'"Location": [{"path": "/m\xff"}, {"path": 7}, "x"]}]}}'
    )
    pms.add(responses.GET, SECTIONS, body=body)

    reply, info = plex.section(plex.new_session(TOKEN), BASE, 1)

    assert reply is plex.Reply.OK
    assert info is not None
    assert info.locations == (b"/m\xff",)


def test_plex_section_not_listed_is_missing(pms: responses.RequestsMock) -> None:
    pms.add(responses.GET, SECTIONS, body=sections_body(section_dir("2", "artist", "/m")))

    assert plex.section(plex.new_session(TOKEN), BASE, 1) == (plex.Reply.MISSING, None)


def test_plex_section_of_another_type_says_so(pms: responses.RequestsMock) -> None:
    pms.add(responses.GET, SECTIONS, body=sections_body(section_dir("1", "movie", "/v")))

    reply, info = plex.section(plex.new_session(TOKEN), BASE, 1)

    assert reply is plex.Reply.OK
    assert info is not None
    assert info.type == "movie"


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (401, plex.Reply.REFUSED),
        (403, plex.Reply.REFUSED),
        (500, plex.Reply.UNAVAILABLE),
        (302, plex.Reply.UNAVAILABLE),
        (requests.ConnectionError(), plex.Reply.UNAVAILABLE),
        (b"not json", plex.Reply.UNAVAILABLE),
        (b'{"MediaContainer": {"Directory": {}}}', plex.Reply.UNAVAILABLE),
    ],
)
def test_plex_section_failures(
    pms: responses.RequestsMock, answer: int | bytes | Exception, expected: plex.Reply
) -> None:
    if isinstance(answer, int):
        pms.add(responses.GET, SECTIONS, status=answer)
    else:
        pms.add(responses.GET, SECTIONS, body=answer)

    assert plex.section(plex.new_session(TOKEN), BASE, 1) == (expected, None)


def test_plex_section_skips_a_directory_it_cannot_read(pms: responses.RequestsMock) -> None:
    pms.add(
        responses.GET,
        SECTIONS,
        body=sections_body("x", {"key": 1, "type": "artist"}, section_dir("1", "artist", "/m")),
    )

    reply, info = plex.section(plex.new_session(TOKEN), BASE, 1)

    assert reply is plex.Reply.OK
    assert info is not None
    assert info.locations == (b"/m",)


# Lyric streams: provider and codec from a track, timing from a stream body, and the verdict.

TRACK = f"{BASE}/library/metadata/77"


def stream_url(stream_id: int) -> str:
    return f"{BASE}/library/streams/{stream_id}"


def test_plex_lyric_streams_reads_local_and_lyricfind(pms: responses.RequestsMock) -> None:
    pms.add(
        responses.GET,
        TRACK,
        body=plex_metadata(
            "77",
            [
                plex_lyric_stream(11, LOCAL_LYRICS, "lrc"),
                plex_lyric_stream(12, LYRICFIND, "lrc", timed="1"),
            ],
        ),
    )

    reply, streams = plex.lyric_streams(plex.new_session(TOKEN), BASE, "77")

    assert reply is plex.Reply.OK
    assert streams == [
        plex.LyricStream(11, LOCAL_LYRICS, "lrc"),
        plex.LyricStream(12, LYRICFIND, "lrc"),
    ]
    assert plex.LOCAL_PROVIDER == LOCAL_LYRICS


def test_plex_lyric_streams_skips_streams_it_cannot_read(pms: responses.RequestsMock) -> None:
    good = plex_lyric_stream(20, LOCAL_LYRICS, "txt")
    odd = [
        {**plex_lyric_stream(21, LOCAL_LYRICS, "lrc"), "streamType": "4"},
        {**plex_lyric_stream(22, LOCAL_LYRICS, "lrc"), "id": "22"},
        {**plex_lyric_stream(23, LOCAL_LYRICS, "lrc"), "id": True},
        {k: v for k, v in plex_lyric_stream(24, LOCAL_LYRICS, "lrc").items() if k != "provider"},
        {**plex_lyric_stream(25, LOCAL_LYRICS, "lrc"), "codec": None},
        {**plex_lyric_stream(26, LOCAL_LYRICS, "lrc"), "streamType": 3},
        "not a stream",
    ]
    pms.add(responses.GET, TRACK, body=plex_metadata("77", [*odd, good]))

    reply, streams = plex.lyric_streams(plex.new_session(TOKEN), BASE, "77")

    assert reply is plex.Reply.OK
    assert streams == [plex.LyricStream(20, LOCAL_LYRICS, "txt")]


@pytest.mark.parametrize(
    ("status", "expected"),
    [(404, plex.Reply.MISSING), (401, plex.Reply.REFUSED), (500, plex.Reply.UNAVAILABLE)],
)
def test_plex_lyric_streams_status_map(
    pms: responses.RequestsMock, status: int, expected: plex.Reply
) -> None:
    pms.add(responses.GET, TRACK, status=status)

    assert plex.lyric_streams(plex.new_session(TOKEN), BASE, "77") == (expected, [])


@pytest.mark.parametrize("key", ["", "7/7", "77?x=1", "../1", "٤٢"])
def test_plex_lyric_streams_sends_nothing_for_a_key_that_is_not_a_number(
    pms: responses.RequestsMock, key: str
) -> None:
    reply, streams = plex.lyric_streams(plex.new_session(TOKEN), BASE, key)

    assert (reply, streams) == (plex.Reply.UNAVAILABLE, [])
    assert len(pms.calls) == 0


@pytest.mark.parametrize("timed", [True, False])
def test_plex_stream_timed_reads_the_body_flag(pms: responses.RequestsMock, timed: bool) -> None:
    pms.add(responses.GET, stream_url(11), body=plex_stream_body(timed, CANARY))

    assert plex.stream_timed(plex.new_session(TOKEN), BASE, 11) == (plex.Reply.OK, timed)
    assert pms.calls[0].request.url == stream_url(11)


@pytest.mark.parametrize(
    "answer",
    [
        b"",
        b"not json",
        b'{"MediaContainer": {"size": 0}}',
        b'{"MediaContainer": {"Lyrics": []}}',
        b'{"MediaContainer": {"Lyrics": ["x"]}}',
        plex_stream_body("1", CANARY),
        plex_stream_body(1, CANARY),
        plex_stream_body(None, CANARY),
    ],
    ids=[
        "empty",
        "not json",
        "no lyrics",
        "empty lyrics",
        "lyrics not an object",
        "timed string",
        "timed int",
        "timed null",
    ],
)
def test_plex_stream_timed_is_none_when_the_body_cannot_say(
    pms: responses.RequestsMock, answer: bytes
) -> None:
    pms.add(responses.GET, stream_url(11), body=answer)

    assert plex.stream_timed(plex.new_session(TOKEN), BASE, 11) == (plex.Reply.OK, None)


@pytest.mark.parametrize(
    ("answer", "reply"),
    [
        (500, plex.Reply.UNAVAILABLE),
        (401, plex.Reply.REFUSED),
        (404, plex.Reply.MISSING),
        (requests.ConnectionError(), plex.Reply.UNAVAILABLE),
    ],
    ids=["500", "401", "404", "connection error"],
)
def test_plex_stream_timed_says_how_a_failed_request_failed(
    pms: responses.RequestsMock, answer: int | Exception, reply: plex.Reply
) -> None:
    if isinstance(answer, int):
        pms.add(responses.GET, stream_url(11), status=answer)
    else:
        pms.add(responses.GET, stream_url(11), body=answer)

    assert plex.stream_timed(plex.new_session(TOKEN), BASE, 11) == (reply, None)


@pytest.mark.parametrize("stream_id", [-1, True])
def test_plex_stream_timed_sends_nothing_for_an_id_that_is_not_a_count(
    pms: responses.RequestsMock, stream_id: int
) -> None:
    assert plex.stream_timed(plex.new_session(TOKEN), BASE, stream_id) == (
        plex.Reply.UNAVAILABLE,
        None,
    )
    assert len(pms.calls) == 0


@pytest.mark.parametrize("timed", [True, False, "1"])
def test_plex_stream_timed_lets_no_lyric_text_out(
    pms: responses.RequestsMock,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    timed: object,
) -> None:
    caplog.set_level(logging.DEBUG)
    pms.add(responses.GET, stream_url(11), body=plex_stream_body(timed, CANARY))

    reply, result = plex.stream_timed(plex.new_session(TOKEN), BASE, 11)

    assert reply is plex.Reply.OK
    assert result is None or isinstance(result, bool)
    assert CANARY not in repr(result)
    assert CANARY not in caplog.text
    captured = capsys.readouterr()
    assert CANARY not in captured.out
    assert CANARY not in captured.err


LOCAL_LRC = plex.LyricStream(1, LOCAL_LYRICS, "lrc")
LOCAL_TXT = plex.LyricStream(2, LOCAL_LYRICS, "txt")
FOUND_LRC = plex.LyricStream(3, LYRICFIND, "lrc")
FOUND_TXT = plex.LyricStream(4, LYRICFIND, "txt")
V = plex.Verdict

VERDICTS = [
    # (streams, timed, verdict, passes for .lrc, passes for .txt)
    ([LOCAL_LRC], True, V.LOCAL_TIMED, True, False),
    ([LOCAL_LRC], False, V.LOCAL_UNTIMED, False, True),
    ([LOCAL_LRC], None, V.LOCAL_UNTIMED, False, True),
    ([LOCAL_TXT], False, V.LOCAL_UNTIMED, False, True),
    ([FOUND_LRC], None, V.PROVIDER, False, False),
    ([FOUND_LRC, FOUND_TXT], True, V.PROVIDER, False, False),
    ([], None, V.NONE, False, False),
    ([FOUND_LRC, LOCAL_LRC], True, V.LOCAL_TIMED, True, False),
]


@pytest.mark.parametrize(("streams", "timed", "expected", "lrc", "txt"), VERDICTS)
def test_plex_verdict_and_passes(
    streams: list[plex.LyricStream],
    timed: bool | None,
    expected: plex.Verdict,
    lrc: bool,
    txt: bool,
) -> None:
    for kind, passing in ((Kind.LRC, lrc), (Kind.TXT, txt)):
        verdict = plex.verdict(kind, streams, timed)
        assert verdict is expected
        assert plex.passes(kind, verdict) is passing


def test_plex_verdict_table_covers_every_verdict() -> None:
    assert {row[2] for row in VERDICTS} == set(plex.Verdict)


@pytest.mark.parametrize(
    ("kind", "streams", "chosen"),
    [
        (Kind.LRC, [LOCAL_TXT, LOCAL_LRC], LOCAL_LRC),
        (Kind.TXT, [LOCAL_LRC, LOCAL_TXT], LOCAL_TXT),
        (Kind.LRC, [FOUND_LRC, LOCAL_LRC], LOCAL_LRC),
        (Kind.LRC, [FOUND_LRC, LOCAL_TXT], LOCAL_TXT),
        (Kind.LRC, [FOUND_LRC], None),
        (Kind.TXT, [], None),
    ],
)
def test_plex_verdict_judges_the_local_stream_of_the_sidecars_kind(
    kind: Kind, streams: list[plex.LyricStream], chosen: plex.LyricStream | None
) -> None:
    assert plex.local_stream(kind, streams) == chosen


def test_plex_verdict_an_lrc_judged_on_its_own_stream_beside_a_local_txt() -> None:
    # The body read is the lrc stream's, so its timed flag decides.
    assert plex.verdict(Kind.LRC, [LOCAL_TXT, LOCAL_LRC], True) is V.LOCAL_TIMED
    assert plex.verdict(Kind.TXT, [LOCAL_TXT, LOCAL_LRC], False) is V.LOCAL_UNTIMED


def test_plex_track_index_maps_folder_and_stem_to_the_track() -> None:
    tracks = [
        plex.PlexTrack("201", "101", (b"/m/lib/A/01 Song.flac",)),
        plex.PlexTrack("202", "101", (b"/m/lib/A/02 Other.v2.mp3",)),
        plex.PlexTrack("203", "102", (b"/m/lib/B/\xff raw.flac",)),
    ]

    assert plex.track_index(tracks) == {
        (b"/m/lib/A", b"01 Song"): "201",
        (b"/m/lib/A", b"02 Other.v2"): "202",
        (b"/m/lib/B", b"\xff raw"): "203",
    }


def test_plex_track_index_keeps_the_first_of_two_tracks_sharing_a_stem() -> None:
    tracks = [
        plex.PlexTrack("201", "101", (b"/m/A/01.flac",)),
        plex.PlexTrack("202", "101", (b"/m/A/01.mp3",)),
    ]

    assert plex.track_index(tracks) == {(b"/m/A", b"01"): "201"}
