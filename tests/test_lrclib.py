"""The LRCLIB client's status map and request shape, in process with responses."""

import re
from collections.abc import Iterator
from urllib.parse import parse_qsl, urlsplit

import pytest
import requests
import responses
from requests.adapters import HTTPAdapter

from conftest import PROBE_TEXT, StubLrclib
from plyrix import __version__, lrclib
from plyrix.lrclib import Preflight, Status

BASE = "http://lrclib.test/api"
URL = f"{BASE}/get"
SEARCH_URL = f"{BASE}/search"
ID_URL = re.compile(re.escape(URL) + r"/\d+")
PROBE_IDS = (1, 2, 3, 100, 1000, 10000, 100000, 1000000)
LONE_SURROGATE = "x\ud800"


@pytest.fixture
def mock() -> Iterator[responses.RequestsMock]:
    with responses.RequestsMock() as requests_mock:
        yield requests_mock


def lookup(duration: float = 3.0, album: str | None = "Album", base: str = BASE):  # type: ignore[no-untyped-def]
    return lrclib.get(lrclib.new_session(), base, "Artist", "Title", album, duration)


def query_of(request: requests.PreparedRequest) -> str:
    assert isinstance(request.url, str)
    return urlsplit(request.url).query


def sent_timeout(request: requests.PreparedRequest) -> object:
    """Return the timeout sent, which responses keeps on the request where no stub can see it."""
    return vars(request)["req_kwargs"]["timeout"]


def sent_params(mock: responses.RequestsMock) -> dict[str, str]:
    return dict(parse_qsl(query_of(mock.calls[0].request), keep_blank_values=True))


A_ROW = {"id": 7, "trackName": "Title"}
# What a proxy or a wrong service might answer with a 200. None says anything about a track.
NOT_ROWS = [
    {},
    {"error": "invented"},
    {"id": 7},
    {"trackName": "Title"},
    {"id": "7", "trackName": "Title"},
    {"id": True, "trackName": "Title"},
    {"id": None, "trackName": "Title"},
    {"id": 7, "trackName": 7},
    {"id": 7, "trackName": ["Title"]},
]


def test_found_returns_the_record(mock: responses.RequestsMock) -> None:
    mock.get(URL, json=A_ROW)
    assert lookup() == (Status.FOUND, A_ROW)


def test_a_row_may_have_no_title_as_the_server_allows_that(mock: responses.RequestsMock) -> None:
    untitled = {"id": 7, "trackName": None}
    mock.get(URL, json=untitled)
    assert lookup() == (Status.FOUND, untitled)


@pytest.mark.parametrize("body", NOT_ROWS, ids=repr)
def test_a_200_that_isnt_a_row_is_unavailable(mock: responses.RequestsMock, body: object) -> None:
    mock.get(URL, json=body)
    assert lookup() == (Status.UNAVAILABLE, None)


# The bodies the pinned server sends, from server/src/errors.rs and checked against a live one.
TRACK_NOT_FOUND = {
    "message": "Failed to find specified track",
    "name": "TrackNotFound",
    "statusCode": 404,
}
VALIDATION_ERROR = {
    "message": "duration: must be between 1 and 3600",
    "name": "ValidationError",
    "statusCode": 400,
}


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (404, TRACK_NOT_FOUND, Status.NOT_FOUND),
        (400, VALIDATION_ERROR, Status.REJECTED),
        (500, {"name": "UnknownError"}, Status.UNAVAILABLE),
        (503, {"name": "Whatever"}, Status.UNAVAILABLE),
    ],
)
def test_status_map(
    mock: responses.RequestsMock, status: int, body: dict[str, object], expected: Status
) -> None:
    mock.get(URL, status=status, json=body)
    assert lookup() == (expected, None)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (404, b""),
        (404, b"<html>404 Not Found</html>"),
        (404, b"[]"),
        (404, b'{"name": "Whatever"}'),
        (404, b'{"name": ["TrackNotFound"]}'),
        (404, b'{"message": "Failed to find specified track"}'),
        (404, b'{"name": "ValidationError"}'),
        (404, b"[" * 200_000),
        (400, b"Failed to deserialize query string"),
        (400, b'{"name": "Whatever"}'),
        (400, b'{"name": "TrackNotFound"}'),
    ],
)
def test_an_error_not_from_lrclib_says_nothing_about_the_track(
    mock: responses.RequestsMock, status: int, body: bytes
) -> None:
    mock.get(URL, status=status, body=body)
    assert lookup() == (Status.UNAVAILABLE, None)


@pytest.mark.parametrize(
    "failure",
    [requests.Timeout(), requests.ConnectionError(), requests.exceptions.ChunkedEncodingError()],
)
def test_transport_failures_are_unavailable(
    mock: responses.RequestsMock, failure: Exception
) -> None:
    mock.get(URL, body=failure)
    assert lookup() == (Status.UNAVAILABLE, None)


@pytest.mark.parametrize(
    "body", [b"not json", b"", b"[1, 2]", b'"text"', b"null", b"7", b"[" * 200_000]
)
def test_a_200_without_a_json_object_is_unavailable(
    mock: responses.RequestsMock, body: bytes
) -> None:
    mock.get(URL, body=body)
    assert lookup() == (Status.UNAVAILABLE, None)


def test_a_redirect_is_never_followed(mock: responses.RequestsMock) -> None:
    mock.get(URL, status=302, headers={"Location": "http://elsewhere.test/get"})
    assert lookup() == (Status.UNAVAILABLE, None)
    # Library metadata in the query must not reach the second host.
    assert len(mock.calls) == 1


def test_session_ignores_proxy_settings() -> None:
    assert lrclib.new_session().trust_env is False


@pytest.mark.parametrize(
    ("duration", "sent"), [(2.5, "2"), (3.5, "4"), (3.0, "3"), (3.49, "3"), (3.51, "4")]
)
def test_duration_uses_round(mock: responses.RequestsMock, duration: float, sent: str) -> None:
    mock.get(URL, status=404)
    lookup(duration=duration)
    assert sent_params(mock)["duration"] == sent


def test_request_shape(mock: responses.RequestsMock) -> None:
    mock.get(URL, status=404)
    lookup()
    assert sent_params(mock) == {
        "artist_name": "Artist",
        "track_name": "Title",
        "album_name": "Album",
        "duration": "3",
    }
    request = mock.calls[0].request
    assert request.method == "GET"
    assert request.headers["User-Agent"] == f"plyrix/{__version__}"
    assert sent_timeout(request) == 30


def test_a_missing_album_is_sent_empty(mock: responses.RequestsMock) -> None:
    mock.get(URL, status=404)
    lookup(album=None)
    assert sent_params(mock)["album_name"] == ""


def test_a_trailing_slash_on_the_base_is_harmless(mock: responses.RequestsMock) -> None:
    mock.get(URL, status=404, json=TRACK_NOT_FOUND)
    assert lookup(base=BASE + "/") == (Status.NOT_FOUND, None)


@pytest.mark.parametrize(
    ("duration", "sent"),
    [(1.0, "1"), (3600.0, "3600"), (0.4, None), (3600.6, None), (5000.0, None)],
)
def test_a_duration_lrclib_would_refuse_is_left_out(
    mock: responses.RequestsMock, duration: float, sent: str | None
) -> None:
    mock.get(URL, status=404)
    lookup(duration=duration)
    assert sent_params(mock).get("duration") == sent


@pytest.mark.parametrize(
    ("artist", "title", "album"),
    [
        (LONE_SURROGATE, "Title", "Album"),
        ("Artist", LONE_SURROGATE, "Album"),
        ("Artist", "Title", LONE_SURROGATE),
    ],
)
def test_text_that_cant_be_sent_is_rejected(
    mock: responses.RequestsMock, artist: str, title: str, album: str
) -> None:
    session = lrclib.new_session()
    assert lrclib.get(session, BASE, artist, title, album, 3.0) == (Status.REJECTED, None)
    assert lrclib.search(session, BASE, artist, title, album) == (Status.REJECTED, None)
    assert len(mock.calls) == 0


def test_a_lookup_sends_composed_text(mock: responses.RequestsMock) -> None:
    mock.get(URL, status=404)
    lrclib.get(lrclib.new_session(), BASE, "Cafe\u0301", "Ole\u0301", "Ni\u00f1o\u0301", 3.0)
    sent = sent_params(mock)
    assert (sent["artist_name"], sent["track_name"]) == ("Caf\u00e9", "Ol\u00e9")
    assert sent["album_name"] == "Ni\u00f1\u00f3"


def test_a_search_sends_composed_text(mock: responses.RequestsMock) -> None:
    mock.get(SEARCH_URL, json=[])
    lrclib.search(lrclib.new_session(), BASE, "Cafe\u0301", "Ole\u0301", "Ni\u00f1o\u0301")
    assert sent_params(mock) == {
        "artist_name": "Caf\u00e9",
        "track_name": "Ol\u00e9",
        "album_name": "Ni\u00f1\u00f3",
    }


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_the_retry_policy(scheme: str) -> None:
    adapter = lrclib.new_session().get_adapter(f"{scheme}://lrclib.test")
    assert isinstance(adapter, HTTPAdapter)
    retry = adapter.max_retries
    assert (retry.status, retry.connect, retry.read, retry.other, retry.redirect) == (3, 0, 0, 0, 0)
    assert retry.status_forcelist == [503]
    assert retry.parse_retry_after("3600") == 5


def test_an_overloaded_server_is_asked_again(stub_lrclib: StubLrclib) -> None:
    stub_lrclib.queue = [(503, {}), (200, A_ROW)]
    answer = lrclib.get(lrclib.new_session(), stub_lrclib.base_url, "Artist", "Title", None, 3.0)
    assert answer == (Status.FOUND, A_ROW)
    assert len(stub_lrclib.requests) == 2


def test_a_server_that_stays_overloaded_is_unavailable(stub_lrclib: StubLrclib) -> None:
    stub_lrclib.status, stub_lrclib.body = 503, {}
    answer = lrclib.get(lrclib.new_session(), stub_lrclib.base_url, "Artist", "Title", None, 3.0)
    assert answer == (Status.UNAVAILABLE, None)
    assert len(stub_lrclib.requests) == 4


def test_search_returns_the_rows(mock: responses.RequestsMock) -> None:
    rows = [A_ROW, {"id": 8, "trackName": None}]
    mock.get(SEARCH_URL, json=rows)
    answer = lrclib.search(lrclib.new_session(), BASE, "Artist", "Title")
    assert answer == (Status.FOUND, rows)


@pytest.mark.parametrize("item", [*NOT_ROWS, 7, "text", None, [1]], ids=repr)
def test_search_holding_anything_but_rows_is_unavailable(
    mock: responses.RequestsMock, item: object
) -> None:
    mock.get(SEARCH_URL, json=[A_ROW, item])
    answer = lrclib.search(lrclib.new_session(), BASE, "Artist", "Title")
    assert answer == (Status.UNAVAILABLE, None)


def test_search_request_shape(mock: responses.RequestsMock) -> None:
    mock.get(SEARCH_URL, json=[])
    lrclib.search(lrclib.new_session(), BASE + "/", "Artist", "Title")
    assert sent_params(mock) == {"artist_name": "Artist", "track_name": "Title"}
    request = mock.calls[0].request
    assert request.headers["User-Agent"] == f"plyrix/{__version__}"
    assert sent_timeout(request) == 30


def test_search_narrows_by_album_when_given_one(mock: responses.RequestsMock) -> None:
    mock.get(SEARCH_URL, json=[])
    lrclib.search(lrclib.new_session(), BASE, "Artist", "Title", "Album")
    assert sent_params(mock)["album_name"] == "Album"


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, b"{}"),
        (200, b'"text"'),
        (200, b"null"),
        (200, b"not json"),
        (200, b"[" * 200_000),
        (500, b"[]"),
        (503, b"[]"),
        (404, b"[]"),
        (404, b'{"name": "TrackNotFound"}'),
    ],
)
def test_search_anything_but_a_200_list_is_unavailable(
    mock: responses.RequestsMock, status: int, body: bytes
) -> None:
    mock.get(SEARCH_URL, status=status, body=body)
    assert lrclib.search(lrclib.new_session(), BASE, "Artist", "Title") == (
        Status.UNAVAILABLE,
        None,
    )


def test_search_never_follows_a_redirect(mock: responses.RequestsMock) -> None:
    mock.get(SEARCH_URL, status=302, headers={"Location": "http://elsewhere.test/search"})
    assert lrclib.search(lrclib.new_session(), BASE, "Artist", "Title") == (
        Status.UNAVAILABLE,
        None,
    )
    assert len(mock.calls) == 1


def test_search_transport_failure_is_unavailable(mock: responses.RequestsMock) -> None:
    mock.get(SEARCH_URL, body=requests.ConnectionError())
    assert lrclib.search(lrclib.new_session(), BASE, "Artist", "Title") == (
        Status.UNAVAILABLE,
        None,
    )


def test_get_by_id_returns_the_record(mock: responses.RequestsMock) -> None:
    mock.get(f"{URL}/7", json=A_ROW)
    assert lrclib.get_by_id(lrclib.new_session(), BASE, 7) == (Status.FOUND, A_ROW)


@pytest.mark.parametrize("body", NOT_ROWS, ids=repr)
def test_get_by_id_a_200_that_isnt_a_row_is_unavailable(
    mock: responses.RequestsMock, body: object
) -> None:
    mock.get(f"{URL}/7", json=body)
    assert lrclib.get_by_id(lrclib.new_session(), BASE, 7) == (Status.UNAVAILABLE, None)


def test_get_by_id_says_when_there_is_no_such_track(mock: responses.RequestsMock) -> None:
    mock.get(f"{URL}/7", status=404, json=TRACK_NOT_FOUND)
    assert lrclib.get_by_id(lrclib.new_session(), BASE, 7) == (Status.NOT_FOUND, None)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, b"[]"),
        (200, b"not json"),
        (404, b"{}"),
        (404, b"[]"),
        (400, b'{"name": "ValidationError"}'),
        (500, b"{}"),
        (503, b"{}"),
        (302, b"{}"),
    ],
)
def test_get_by_id_anything_else_is_unavailable(
    mock: responses.RequestsMock, status: int, body: bytes
) -> None:
    mock.get(f"{URL}/7", status=status, body=body)
    assert lrclib.get_by_id(lrclib.new_session(), BASE, 7) == (Status.UNAVAILABLE, None)


@pytest.fixture
def server() -> Iterator[responses.RequestsMock]:
    """A mock that doesn't mind an answer going unused, as preflight stops at the first failure."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as requests_mock:
        yield requests_mock


Answer = tuple[int, object]
NO_TRACK: Answer = (404, TRACK_NOT_FOUND)
ROW: Answer = (200, {"id": 1, "trackName": "Title"})
NOT_A_ROW: Answer = (200, {"error": "invented"})


def serve(
    server: responses.RequestsMock,
    get: Answer = NO_TRACK,
    search: Answer = (200, []),
    by_id: Answer = ROW,
) -> None:
    server.get(URL, status=get[0], json=get[1])
    server.get(SEARCH_URL, status=search[0], json=search[1])
    server.get(ID_URL, status=by_id[0], json=by_id[1])


def test_preflight_a_healthy_server_is_ready(server: responses.RequestsMock) -> None:
    serve(server)
    assert lrclib.preflight(lrclib.new_session(), BASE) is Preflight.READY


def test_preflight_a_row_answering_its_own_probe_is_ready(server: responses.RequestsMock) -> None:
    serve(server, get=ROW)
    assert lrclib.preflight(lrclib.new_session(), BASE) is Preflight.READY


@pytest.mark.parametrize(
    "get",
    [
        (500, {"name": "UnknownError"}),
        (503, {}),
        (404, {}),
        (400, {"name": "ValidationError"}),
        (200, []),
        NOT_A_ROW,
        (302, {}),
    ],
)
def test_preflight_a_failing_get_is_unavailable(
    server: responses.RequestsMock, get: Answer
) -> None:
    serve(server, get=get)
    assert lrclib.preflight(lrclib.new_session(), BASE) is Preflight.UNAVAILABLE


def test_preflight_a_refused_connection_is_unavailable(server: responses.RequestsMock) -> None:
    server.get(URL, body=requests.ConnectionError())
    assert lrclib.preflight(lrclib.new_session(), BASE) is Preflight.UNAVAILABLE


@pytest.mark.parametrize(
    "search",
    [(500, {"name": "UnknownError"}), (503, {}), (200, {}), (404, {}), (200, [{"error": "x"}])],
)
def test_preflight_a_failing_search_is_index_not_ready(
    server: responses.RequestsMock, search: Answer
) -> None:
    serve(server, search=search)
    assert lrclib.preflight(lrclib.new_session(), BASE) is Preflight.INDEX_NOT_READY


def test_preflight_a_200_that_isnt_a_row_doesnt_prove_a_database(
    server: responses.RequestsMock,
) -> None:
    serve(server, by_id=NOT_A_ROW)
    assert lrclib.preflight(lrclib.new_session(), BASE) is Preflight.UNAVAILABLE


def test_preflight_no_id_answering_is_no_tracks(server: responses.RequestsMock) -> None:
    serve(server, by_id=NO_TRACK)
    assert lrclib.preflight(lrclib.new_session(), BASE) is Preflight.NO_TRACKS
    asked = [urlsplit(call.request.url).path for call in server.calls]
    assert asked[2:] == [f"/api/get/{track_id}" for track_id in PROBE_IDS]


def test_preflight_stops_asking_at_the_first_row(server: responses.RequestsMock) -> None:
    serve(server)
    lrclib.preflight(lrclib.new_session(), BASE)
    assert len(server.calls) == 3


@pytest.mark.parametrize("track_id", PROBE_IDS)
def test_preflight_any_id_answering_is_ready(server: responses.RequestsMock, track_id: int) -> None:
    server.get(f"{URL}/{track_id}", json={"id": track_id, "trackName": "Title"})
    serve(server, by_id=NO_TRACK)
    assert lrclib.preflight(lrclib.new_session(), BASE) is Preflight.READY


def test_preflight_an_id_failing_is_not_called_empty(server: responses.RequestsMock) -> None:
    server.get(f"{URL}/100", status=500, json={})
    serve(server, by_id=NO_TRACK)
    assert lrclib.preflight(lrclib.new_session(), BASE) is Preflight.UNAVAILABLE


def test_preflight_an_id_failing_doesnt_hide_one_that_answers(
    server: responses.RequestsMock,
) -> None:
    server.get(f"{URL}/1", status=500, json={})
    server.get(f"{URL}/2", json={"id": 2, "trackName": "Title"})
    serve(server, by_id=NO_TRACK)
    assert lrclib.preflight(lrclib.new_session(), BASE) is Preflight.READY


def test_preflight_sends_only_its_own_text(server: responses.RequestsMock) -> None:
    serve(server)
    lrclib.preflight(lrclib.new_session(), BASE)
    for call in server.calls[:2]:
        params = dict(parse_qsl(query_of(call.request)))
        assert (params["artist_name"], params["track_name"]) == (PROBE_TEXT, PROBE_TEXT)
