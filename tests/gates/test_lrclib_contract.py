"""Contract gate: the answers plyrix relies on, from a live LRCLIB at the pinned revision.

Skipped unless PLYRIX_LRCLIB_CONTRACT_URL names that server. Every other test talks to a stub, so
this is what would notice the real server moving away from it. It only reads, and it asks with
invented text. Rows are read inside _check, which hands back field names, type names and counts, so
no assertion or printed line can hold a lyric, artist or title from a response.
"""

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import NoneType
from urllib.parse import urlsplit

import pytest

from conftest import CANARY, RECORD, StubLrclib
from plyrix import config, lrclib
from plyrix.lrclib import Preflight, Status
from plyrix.matching import candidate_from

# What candidate_from reads, and the types it can use. Exact types, as a JSON true is an int to
# isinstance. A polluted row can still miss one of these, which is why candidate_from trusts none.
FIELDS: dict[str, tuple[type, ...]] = {
    "id": (int,),
    "artistName": (str,),
    "trackName": (str,),
    "albumName": (str,),
    "duration": (int, float),
    "syncedLyrics": (str, NoneType),
    "plainLyrics": (str, NoneType),
    "instrumental": (bool,),
}


@dataclass(frozen=True)
class _Result:
    """Everything the gate keeps from a server: statuses, counts, field names and type names."""

    preflight: Preflight
    get_status: Status
    search_status: Status
    search_rows: int
    ids_found: int
    ids_failed: int
    problems: tuple[str, ...]


def _field_problems(record: Mapping[str, object]) -> list[str]:
    """Name each field that isn't as candidate_from needs it, and never what it holds."""
    problems = []
    for name, types in FIELDS.items():
        if name not in record:
            problems.append(f"{name}: missing")
        elif type(record[name]) not in types:
            problems.append(f"{name}: {type(record[name]).__name__}")
    return problems


def _check(url: str) -> _Result:
    """Ask a server what plyrix asks, through the code that ships."""
    session = lrclib.new_session()
    text = lrclib._PROBE_TEXT
    get_status, _ = lrclib.get(session, url, text, text, None, lrclib._PROBE_DURATION)
    search_status, rows = lrclib.search(session, url, text, text)
    problems = [problem for row in rows or [] for problem in _field_problems(row)]
    ids_found = ids_failed = 0
    for track_id in lrclib._PROBE_IDS:
        status, record = lrclib.get_by_id(session, url, track_id)
        if record is not None:
            ids_found += 1
            problems += _field_problems(record)
        ids_failed += status is Status.UNAVAILABLE
    return _Result(
        lrclib.preflight(session, url),
        get_status,
        search_status,
        -1 if rows is None else len(rows),
        ids_found,
        ids_failed,
        tuple(dict.fromkeys(problems)),
    )


def _is_public(url: str) -> bool:
    host = (urlsplit(url).hostname or "").rstrip(".").lower()
    public = config._PUBLIC_LRCLIB_HOST
    return host == public or host.endswith(f".{public}")


@pytest.mark.lrclib_contract
def test_lrclib_contract() -> None:
    url = os.environ.get("PLYRIX_LRCLIB_CONTRACT_URL")
    if not url:
        pytest.skip("set PLYRIX_LRCLIB_CONTRACT_URL to a self-hosted LRCLIB at the pinned revision")
    if _is_public(url):
        pytest.fail("PLYRIX_LRCLIB_CONTRACT_URL must be a self-hosted LRCLIB")
    result = _check(url)
    print(
        f"search_rows={result.search_rows} ids_found={result.ids_found}"
        f" ids_failed={result.ids_failed} problems={len(result.problems)}"
    )
    assert result.preflight is Preflight.READY
    assert result.get_status is Status.NOT_FOUND
    assert result.search_status is Status.FOUND
    assert result.ids_found >= 1
    assert result.problems == ()


class _ReadsSeen(Mapping[str, object]):
    """Holds nothing, and keeps the name of each field asked for."""

    def __init__(self) -> None:
        self.seen: set[str] = set()

    def __getitem__(self, key: str) -> object:
        self.seen.add(key)
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0


def test_the_fields_checked_are_the_fields_candidate_from_reads() -> None:
    record = _ReadsSeen()

    candidate_from(record)

    assert record.seen == set(FIELDS)


def test_the_stubs_row_holds_to_the_contract() -> None:
    assert _field_problems(RECORD) == []


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("id", True, "id: bool"),
        ("duration", True, "duration: bool"),
        ("duration", "3", "duration: str"),
        ("artistName", None, "artistName: NoneType"),
        ("instrumental", 0, "instrumental: int"),
        ("syncedLyrics", 1, "syncedLyrics: int"),
    ],
)
def test_a_field_of_the_wrong_type_is_named(field: str, value: object, expected: str) -> None:
    assert _field_problems({**RECORD, field: value}) == [expected]


@pytest.mark.parametrize("field", ["syncedLyrics", "plainLyrics"])
def test_a_lyric_field_may_be_null(field: str) -> None:
    assert _field_problems({**RECORD, field: None}) == []


def test_a_missing_field_is_named() -> None:
    record = {name: value for name, value in RECORD.items() if name != "albumName"}

    assert _field_problems(record) == ["albumName: missing"]


def test_a_server_answering_as_plyrix_expects_passes(stub_lrclib: StubLrclib) -> None:
    stub_lrclib.preflight_id = (200, RECORD)

    assert _check(stub_lrclib.base_url) == _Result(
        Preflight.READY,
        Status.NOT_FOUND,
        Status.FOUND,
        0,
        len(lrclib._PROBE_IDS),
        0,
        (),
    )


def test_a_row_that_moved_is_reported_once_with_no_text(stub_lrclib: StubLrclib) -> None:
    moved = {name: value for name, value in RECORD.items() if name != "duration"}
    stub_lrclib.preflight_id = (200, {**moved, "artistName": [CANARY]})

    result = _check(stub_lrclib.base_url)

    assert result.problems == ("artistName: list", "duration: missing")
    assert CANARY not in repr(result)


def test_a_title_that_moved_isnt_a_row_at_all(stub_lrclib: StubLrclib) -> None:
    stub_lrclib.preflight_id = (200, {**RECORD, "trackName": [CANARY]})

    result = _check(stub_lrclib.base_url)

    assert result.preflight is Preflight.UNAVAILABLE
    assert result.ids_failed == len(lrclib._PROBE_IDS)
    assert CANARY not in repr(result)


def test_a_search_row_is_checked_too(stub_lrclib: StubLrclib) -> None:
    stub_lrclib.preflight_id = (200, RECORD)
    stub_lrclib.preflight_search = (200, [{**RECORD, "artistName": None}])

    result = _check(stub_lrclib.base_url)

    assert (result.search_rows, result.problems) == (1, ("artistName: NoneType",))


def test_a_search_that_fails_has_no_rows_to_count(stub_lrclib: StubLrclib) -> None:
    stub_lrclib.preflight_search = (500, {})

    result = _check(stub_lrclib.base_url)

    assert (result.preflight, result.search_status) == (
        Preflight.INDEX_NOT_READY,
        Status.UNAVAILABLE,
    )
    assert result.search_rows == -1


def test_an_id_that_fails_is_counted(stub_lrclib: StubLrclib) -> None:
    stub_lrclib.preflight_id = (500, {})

    result = _check(stub_lrclib.base_url)

    assert (result.ids_found, result.ids_failed) == (0, len(lrclib._PROBE_IDS))


def test_the_gate_passes_and_prints_counts_only(
    stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stub_lrclib.preflight_id = (200, RECORD)
    monkeypatch.setenv("PLYRIX_LRCLIB_CONTRACT_URL", stub_lrclib.base_url)

    test_lrclib_contract()

    out = capsys.readouterr().out
    assert out == f"search_rows=0 ids_found={len(lrclib._PROBE_IDS)} ids_failed=0 problems=0\n"


@pytest.mark.parametrize(
    ("attribute", "answer"),
    [
        ("preflight_get", (500, {})),
        ("preflight_get", (200, RECORD)),
        ("preflight_search", (500, {})),
        ("preflight_search", (200, {})),
        ("preflight_id", (404, {"name": "TrackNotFound"})),
        ("preflight_id", (200, {**RECORD, "trackName": [CANARY]})),
    ],
)
def test_the_gate_fails_on_each_drift_and_shows_no_text(
    stub_lrclib: StubLrclib,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    attribute: str,
    answer: tuple[int, object],
) -> None:
    setattr(stub_lrclib, attribute, answer)
    if attribute != "preflight_id":
        stub_lrclib.preflight_id = (200, RECORD)
    monkeypatch.setenv("PLYRIX_LRCLIB_CONTRACT_URL", stub_lrclib.base_url)

    with pytest.raises(AssertionError) as failure:
        test_lrclib_contract()

    assert CANARY not in str(failure.value)
    assert CANARY not in capsys.readouterr().out


def test_the_gate_skips_with_no_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLYRIX_LRCLIB_CONTRACT_URL", raising=False)

    with pytest.raises(pytest.skip.Exception, match="PLYRIX_LRCLIB_CONTRACT_URL"):
        test_lrclib_contract()


@pytest.mark.parametrize(
    "url",
    [
        "https://lrclib.net/api",
        "http://LRCLIB.NET/api",
        "https://lrclib.net./api",
        "https://www.lrclib.net/api",
        "https://user@lrclib.net:443/api",
    ],
)
def test_the_gate_refuses_the_public_server(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLYRIX_LRCLIB_CONTRACT_URL", url)
    monkeypatch.setattr(lrclib, "new_session", lambda: pytest.fail("the gate made a session"))

    with pytest.raises(pytest.fail.Exception, match="self-hosted"):
        test_lrclib_contract()


@pytest.mark.parametrize(
    "url", ["http://localhost:3300/api", "http://notlrclib.net/api", "http://lrclib.net.test/api"]
)
def test_the_gate_allows_a_host_that_isnt_the_public_server(url: str) -> None:
    assert not _is_public(url)
