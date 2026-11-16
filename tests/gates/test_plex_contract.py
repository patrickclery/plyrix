"""Contract gate: the answers plyrix relies on, from a live local Plex Media Server.

Skipped unless PLYRIX_PLEX_CONTRACT_URL and PLYRIX_PLEX_CONTRACT_SECTION name that server and its
music section; PLYRIX_PLEX_CONTRACT_TOKEN is optional. Every other test talks to a stub, so this is
what would notice a real server moving away from it. It sends GETs only, through the client that
ships: the section list, one listing page of one track, that track's metadata, and one lyric
stream's body. A session hook reads each response's field types in place and keeps names, type
names and counts only, so no assertion or printed line can hold a title, a path or a lyric.
"""

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import pytest
import requests
import responses

from conftest import (
    CANARY,
    LOCAL_LYRICS,
    LYRICFIND,
    plex_lyric_stream,
    plex_metadata,
    plex_section_dir,
    plex_stream_body,
    plex_track,
)
from plyrix import config, plex

# The fields plyrix reads, and the exact type each must have. Exact, as a JSON true is an int to
# isinstance.
FIELDS: dict[str, type] = {
    "totalSize": int,
    "parentRatingKey": str,
    "Part.file": str,
    "Stream.streamType": int,
    "Stream.provider": str,
    "Stream.codec": str,
    "Lyrics.timed": bool,
}
_LISTING_FIELDS = ("totalSize", "parentRatingKey", "Part.file")


@dataclass
class _Seen:
    """What the hook found in each response: field type names, and nothing a field held."""

    types: dict[str, set[str]] = field(default_factory=dict)
    methods: set[str] = field(default_factory=set)
    requests: int = 0

    def saw(self, name: str, value: object, present: bool = True) -> None:
        self.types.setdefault(name, set()).add(type(value).__name__ if present else "missing")


@dataclass(frozen=True)
class _Result:
    """Everything the gate keeps from a server: replies, counts, methods and field problems."""

    section: plex.Reply
    music: bool
    listing: plex.Reply
    tracks: int
    metadata: plex.Reply | None
    lyric_streams: int
    local_streams: int
    timed_read: bool
    requests: int
    methods: frozenset[str]
    problems: tuple[str, ...]

    def line(self) -> str:
        counts = {
            "tracks": self.tracks,
            "lyric_streams": self.lyric_streams,
            "local_streams": self.local_streams,
            "timed_read": int(self.timed_read),
            "requests": self.requests,
            "problems": len(self.problems),
        }
        return "plex_contract: " + " ".join(f"{k}={v}" for k, v in counts.items())


def _first(container: dict[str, Any], key: str) -> dict[str, Any] | None:
    items = container.get(key)
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return None


def _parts(track: dict[str, Any]) -> list[dict[str, Any]]:
    media = track.get("Media")
    parts = []
    for medium in media if isinstance(media, list) else []:
        found = medium.get("Part") if isinstance(medium, dict) else None
        parts += [part for part in found or [] if isinstance(part, dict)]
    return parts


def _inspect(seen: _Seen, path: str, container: dict[str, Any]) -> None:
    """Record the type of each field plyrix reads in one response, and keep none of its values."""
    if path.endswith("/all"):
        seen.saw("totalSize", container.get("totalSize"), "totalSize" in container)
        track = _first(container, "Metadata")
        if track is not None:
            seen.saw("parentRatingKey", track.get("parentRatingKey"), "parentRatingKey" in track)
            for part in _parts(track):
                seen.saw("Part.file", part.get("file"), "file" in part)
    elif path.startswith("/library/metadata/"):
        track = _first(container, "Metadata")
        for part in _parts(track or {}):
            streams = part.get("Stream")
            for stream in streams if isinstance(streams, list) else []:
                if not isinstance(stream, dict):
                    continue
                kind = stream.get("streamType")
                seen.saw("Stream.streamType", kind, "streamType" in stream)
                if kind in (4, "4"):
                    for name in ("provider", "codec"):
                        seen.saw(f"Stream.{name}", stream.get(name), name in stream)
    elif path.startswith("/library/streams/"):
        lyrics = _first(container, "Lyrics")
        if lyrics is not None:
            seen.saw("Lyrics.timed", lyrics.get("timed"), "timed" in lyrics)


def _watch(session: requests.Session, seen: _Seen) -> None:
    """Record every request's method, and inspect every response's body in place."""
    send = session.send

    def recording_send(request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        seen.methods.add(str(request.method))
        seen.requests += 1
        return send(request, **kwargs)

    def inspect(response: requests.Response, *args: object, **kwargs: object) -> None:
        try:
            body = json.loads(response.content.decode("utf-8", "surrogateescape"))
        except (ValueError, RecursionError):
            return
        container = body.get("MediaContainer") if isinstance(body, dict) else None
        if isinstance(container, dict):
            _inspect(seen, urlsplit(response.url).path, container)

    session.send = recording_send  # type: ignore[method-assign]
    session.hooks["response"].append(inspect)


def _problems(seen: _Seen) -> list[str]:
    """Name each field seen with a type plyrix can't use, and never what it held."""
    problems = []
    for name, expected in FIELDS.items():
        for found in sorted(seen.types.get(name, set()) - {expected.__name__}):
            problems.append(f"{name}: {found}")
    return problems


def _check(url: str, section: int, token: str | None) -> _Result:
    """Ask a server what plyrix asks, through the code that ships, and with GETs only."""
    session = plex.new_session(token)
    seen = _Seen()
    _watch(session, seen)
    section_reply, info = plex.section(session, url, section)
    listing, tracks = plex.list_tracks(session, url, section, page_size=1, pages=1)
    metadata = None
    streams: list[plex.LyricStream] = []
    local: list[plex.LyricStream] = []
    timed = None
    if tracks:
        metadata, streams = plex.lyric_streams(session, url, tracks[0].key)
        local = [stream for stream in streams if stream.provider == plex.LOCAL_PROVIDER]
        if local:
            _, timed = plex.stream_timed(session, url, local[0].id)
    problems = _problems(seen)
    if tracks:
        problems += [f"{name}: missing" for name in _LISTING_FIELDS if name not in seen.types]
    if local and timed is None:
        problems.append("Lyrics.timed: unreadable")
    return _Result(
        section=section_reply,
        music=info is not None and info.type == "artist",
        listing=listing,
        tracks=len(tracks),
        metadata=metadata,
        lyric_streams=len(streams),
        local_streams=len(local),
        timed_read=timed is not None,
        requests=seen.requests,
        methods=frozenset(seen.methods),
        problems=tuple(dict.fromkeys(problems)),
    )


def _is_plex_tv(url: str) -> bool:
    host = (urlsplit(url).hostname or "").rstrip(".").lower()
    account = config._PLEX_ACCOUNT_HOST
    return host == account or host.endswith(f".{account}")


@pytest.mark.plex_contract
def test_plex_contract() -> None:
    url = os.environ.get("PLYRIX_PLEX_CONTRACT_URL")
    section = os.environ.get("PLYRIX_PLEX_CONTRACT_SECTION")
    if not url or not section:
        pytest.skip(
            "set PLYRIX_PLEX_CONTRACT_URL and PLYRIX_PLEX_CONTRACT_SECTION to a local Plex "
            "Media Server and its music section"
        )
    if _is_plex_tv(url):
        pytest.fail("PLYRIX_PLEX_CONTRACT_URL must be your own Plex Media Server, not plex.tv")
    if not (section.isascii() and section.isdigit()):
        pytest.fail("PLYRIX_PLEX_CONTRACT_SECTION must be a section key, a number")
    result = _check(url, int(section), os.environ.get("PLYRIX_PLEX_CONTRACT_TOKEN") or None)
    print(result.line())
    assert result.methods <= {"GET"}, "the gate sent a request that wasn't a GET"
    assert result.problems == (), "fields: " + ", ".join(result.problems)
    assert result.section is plex.Reply.OK, "the section list"
    assert result.music, "the section isn't a music library"
    assert result.listing is plex.Reply.OK, "the track listing"
    assert result.tracks == 1, "the first listing page"
    assert result.metadata is plex.Reply.OK, "the track's metadata"


# Meta-tests: the gate against responses, with CANARY in every title, path and lyric line.

BASE = "http://pms.test:32400"
SECTION = 4
SECTIONS = f"{BASE}/library/sections"
LISTING = f"{SECTIONS}/{SECTION}/all"
TRACK_KEY = "77"
METADATA = f"{BASE}/library/metadata/{TRACK_KEY}"
STREAM = f"{BASE}/library/streams/{TRACK_KEY}"
CANARY_FILE = f"/m/{CANARY}/01 {CANARY}.flac".encode()


def _section_body() -> bytes:
    section = {**plex_section_dir(str(SECTION), "artist", f"/m/{CANARY}"), "title": CANARY}
    return json.dumps({"MediaContainer": {"size": 1, "Directory": [section]}}).encode()


def _listing_body(total: object = 2500, **track: object) -> bytes:
    item = {**plex_track(TRACK_KEY, "7", CANARY_FILE), "title": CANARY, **track}
    page = {"size": 1, "totalSize": total, "offset": 0, "Metadata": [item]}
    return json.dumps({"MediaContainer": page}).encode()


def _streams(**local: object) -> list[dict[str, object]]:
    return [
        {**plex_lyric_stream(9, LYRICFIND, "lrc", timed="1"), "displayTitle": CANARY},
        {**plex_lyric_stream(int(TRACK_KEY), LOCAL_LYRICS, "lrc"), "displayTitle": CANARY, **local},
    ]


def _serve(
    pms: responses.RequestsMock,
    *,
    listing: bytes | None = None,
    metadata: bytes | None = None,
    stream: bytes | None = None,
) -> None:
    pms.add(responses.GET, SECTIONS, body=_section_body())
    pms.add(responses.GET, LISTING, body=listing or _listing_body())
    pms.add(responses.GET, METADATA, body=metadata or plex_metadata(TRACK_KEY, _streams(), CANARY))
    pms.add(responses.GET, STREAM, body=stream or plex_stream_body(True, CANARY))


@pytest.fixture
def pms(monkeypatch: pytest.MonkeyPatch) -> Iterator[responses.RequestsMock]:
    monkeypatch.setenv("PLYRIX_PLEX_CONTRACT_URL", BASE)
    monkeypatch.setenv("PLYRIX_PLEX_CONTRACT_SECTION", str(SECTION))
    monkeypatch.delenv("PLYRIX_PLEX_CONTRACT_TOKEN", raising=False)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


def test_a_server_answering_as_plyrix_expects_passes(
    pms: responses.RequestsMock, capsys: pytest.CaptureFixture[str]
) -> None:
    _serve(pms)

    test_plex_contract()

    out = capsys.readouterr().out
    assert out == (
        "plex_contract: tracks=1 lyric_streams=2 local_streams=1 timed_read=1 requests=4 "
        "problems=0\n"
    )
    assert CANARY not in out


def test_the_gate_sends_only_gets_and_reads_one_page(pms: responses.RequestsMock) -> None:
    _serve(pms)

    result = _check(BASE, SECTION, None)

    assert result.methods == {"GET"}
    assert {call.request.method for call in pms.calls} == {"GET"}
    listings = [call for call in pms.calls if str(call.request.url).startswith(LISTING)]
    assert len(listings) == 1
    assert listings[0].request.headers["X-Plex-Container-Size"] == "1"
    assert CANARY not in repr(result)


def test_the_gate_never_reads_a_lyricfind_body(pms: responses.RequestsMock) -> None:
    _serve(pms)

    _check(BASE, SECTION, None)

    assert all("/library/streams/9" not in str(call.request.url) for call in pms.calls)


DRIFTS = {
    "totalSize": {"listing": _listing_body(total="2500")},
    "parentRatingKey": {"listing": _listing_body(parentRatingKey=7)},
    "Part.file": {"listing": _listing_body(Media=[{"Part": [{"file": [CANARY]}]}])},
    "Stream.streamType": {"metadata": plex_metadata(TRACK_KEY, _streams(streamType="4"), CANARY)},
    "Stream.provider": {"metadata": plex_metadata(TRACK_KEY, _streams(provider=None), CANARY)},
    "Stream.codec": {"metadata": plex_metadata(TRACK_KEY, _streams(codec=4), CANARY)},
    "Lyrics.timed": {"stream": plex_stream_body("1", CANARY)},
}


@pytest.mark.parametrize("name", DRIFTS)
def test_the_gate_fails_on_each_drift_naming_only_the_field(
    pms: responses.RequestsMock, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    _serve(pms, **DRIFTS[name])

    with pytest.raises(AssertionError) as failure:
        test_plex_contract()

    message = str(failure.value)
    assert name in message
    assert CANARY not in message
    assert CANARY not in capsys.readouterr().out


def test_the_fields_checked_are_the_fields_named() -> None:
    assert set(DRIFTS) == set(FIELDS)


@pytest.mark.parametrize(("value", "expected"), [(None, "NoneType"), ("1", "str"), (1, "int")])
def test_a_field_of_the_wrong_type_is_named_by_type(value: object, expected: str) -> None:
    seen = _Seen()
    seen.saw("Lyrics.timed", value)
    seen.saw("Lyrics.timed", True)

    assert _problems(seen) == [f"Lyrics.timed: {expected}"]


def test_a_missing_field_is_named() -> None:
    seen = _Seen()
    seen.saw("Stream.codec", None, present=False)

    assert _problems(seen) == ["Stream.codec: missing"]


@pytest.mark.parametrize("status", [401, 500])
def test_the_gate_fails_when_the_server_does(
    pms: responses.RequestsMock, capsys: pytest.CaptureFixture[str], status: int
) -> None:
    pms.add(responses.GET, SECTIONS, status=status)
    pms.add(responses.GET, LISTING, status=status)

    with pytest.raises(AssertionError) as failure:
        test_plex_contract()

    assert CANARY not in str(failure.value) + capsys.readouterr().out


def test_the_gate_skips_with_no_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLYRIX_PLEX_CONTRACT_URL", raising=False)
    monkeypatch.setenv("PLYRIX_PLEX_CONTRACT_SECTION", "1")

    with pytest.raises(pytest.skip.Exception, match="PLYRIX_PLEX_CONTRACT_URL"):
        test_plex_contract()


def test_the_gate_skips_with_no_section(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLYRIX_PLEX_CONTRACT_URL", BASE)
    monkeypatch.delenv("PLYRIX_PLEX_CONTRACT_SECTION", raising=False)

    with pytest.raises(pytest.skip.Exception, match="PLYRIX_PLEX_CONTRACT_SECTION"):
        test_plex_contract()


@pytest.mark.parametrize(
    "url",
    [
        "https://plex.tv",
        "http://PLEX.TV:32400",
        "https://plex.tv.",
        "https://app.plex.tv",
        "https://user@plex.tv:443",
    ],
)
def test_the_gate_refuses_plex_tv_before_any_session(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLYRIX_PLEX_CONTRACT_URL", url)
    monkeypatch.setenv("PLYRIX_PLEX_CONTRACT_SECTION", "1")
    monkeypatch.setattr(plex, "new_session", lambda token: pytest.fail("the gate made a session"))

    with pytest.raises(pytest.fail.Exception, match="not plex.tv"):
        test_plex_contract()


@pytest.mark.parametrize(
    "url", ["http://localhost:32400", "http://notplex.tv", "https://1-2-3-4.x.plex.direct:32400"]
)
def test_the_gate_allows_a_host_that_isnt_plex_tv(url: str) -> None:
    assert not _is_plex_tv(url)


@pytest.mark.parametrize("section", ["x", "-1", "٤"])
def test_the_gate_refuses_a_section_that_isnt_a_key(
    section: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLYRIX_PLEX_CONTRACT_URL", BASE)
    monkeypatch.setenv("PLYRIX_PLEX_CONTRACT_SECTION", section)
    monkeypatch.setattr(plex, "new_session", lambda token: pytest.fail("the gate made a session"))

    with pytest.raises(pytest.fail.Exception, match="PLYRIX_PLEX_CONTRACT_SECTION"):
        test_plex_contract()
