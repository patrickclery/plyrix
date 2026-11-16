"""GET-only client for a self-hosted LRCLIB server."""

import enum
import unicodedata

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from plyrix import __version__

TIMEOUT_SECONDS = 30
# LRCLIB names its errors in the JSON body (server/src/errors.rs). A 404 or 400 without its name
# came from something else, such as a wrong URL or a proxy, so it says nothing about the track.
_NOT_FOUND_ERROR = "TrackNotFound"
_REJECTED_ERROR = "ValidationError"
# The server's own limits on duration. Outside them it refuses the whole lookup.
_MIN_DURATION = 1
_MAX_DURATION = 3600
# Probes hold this and never anything from the library.
_PROBE_TEXT = "plyrix preflight"
_PROBE_DURATION = 100.0
# Ids have gaps in a real dump, so one alone can't say a database is empty.
_PROBE_IDS = (1, 2, 3, 100, 1000, 10000, 100000, 1000000)


class Status(enum.Enum):
    """Every outcome of a lookup. UNAVAILABLE says nothing about the track."""

    FOUND = enum.auto()
    NOT_FOUND = enum.auto()
    REJECTED = enum.auto()
    UNAVAILABLE = enum.auto()


class Preflight(enum.Enum):
    """Whether a server can answer for a library. Only READY makes a miss mean anything."""

    READY = enum.auto()
    UNAVAILABLE = enum.auto()
    INDEX_NOT_READY = enum.auto()
    NO_TRACKS = enum.auto()


def new_session() -> requests.Session:
    """Return a session that sends library metadata only to the configured URL.

    Ignoring the environment keeps a proxy setting from receiving the lookups. Only a 503 is asked
    again, as that is how LRCLIB says it is overloaded, and retry_after_max keeps a huge Retry-After
    from stalling a run. A dead server costs no retry, and a redirect is never followed.
    """
    session = requests.Session()
    session.trust_env = False
    retry = Retry(
        total=3,
        connect=0,
        read=0,
        other=0,
        redirect=0,
        status=3,
        status_forcelist=[503],
        allowed_methods=["GET"],
        backoff_factor=0.25,
        backoff_max=2,
        retry_after_max=5,
        respect_retry_after_header=True,
        raise_on_status=False,
        raise_on_redirect=False,
    )
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def get(
    session: requests.Session,
    base: str,
    artist: str,
    title: str,
    album: str | None,
    duration: float,
) -> tuple[Status, dict[str, object] | None]:
    """Look up one track. Anything but a clear answer from the server is UNAVAILABLE."""
    params: dict[str, str | int] = {
        "artist_name": _composed(artist),
        "track_name": _composed(title),
        "album_name": _composed(album or ""),
    }
    seconds = round(duration)
    if _MIN_DURATION <= seconds <= _MAX_DURATION:
        params["duration"] = seconds
    try:
        response = _send(session, f"{base.rstrip('/')}/get", params)
    except UnicodeEncodeError:
        return Status.REJECTED, None
    if response is None:
        return Status.UNAVAILABLE, None
    return _read_record(response)


def search(
    session: requests.Session,
    base: str,
    artist: str,
    title: str,
    album: str | None = None,
) -> tuple[Status, list[dict[str, object]] | None]:
    """Search for a track. Anything but a JSON list holding only rows is UNAVAILABLE."""
    params: dict[str, str | int] = {
        "artist_name": _composed(artist),
        "track_name": _composed(title),
    }
    if album:
        params["album_name"] = _composed(album)
    try:
        response = _send(session, f"{base.rstrip('/')}/search", params)
    except UnicodeEncodeError:
        return Status.REJECTED, None
    if response is None or response.status_code != 200:
        return Status.UNAVAILABLE, None
    try:
        body: object = response.json()
    except (ValueError, RecursionError):
        return Status.UNAVAILABLE, None
    if not isinstance(body, list):
        return Status.UNAVAILABLE, None
    rows = [item for item in body if isinstance(item, dict) and _is_row(item)]
    return (Status.FOUND, rows) if len(rows) == len(body) else (Status.UNAVAILABLE, None)


def get_by_id(
    session: requests.Session, base: str, track_id: int
) -> tuple[Status, dict[str, object] | None]:
    """Fetch one row by its id. Anything but the row or a clear TrackNotFound is UNAVAILABLE."""
    response = _send(session, f"{base.rstrip('/')}/get/{track_id}")
    if response is None:
        return Status.UNAVAILABLE, None
    status, record = _read_record(response)
    return (Status.UNAVAILABLE, None) if status is Status.REJECTED else (status, record)


def preflight(session: requests.Session, base: str) -> Preflight:
    """Say whether a server can answer for a library, with no library metadata in the probes.

    An empty database answers both lookups just as a healthy one does, so a row has to be seen too.
    Nothing here can tell a partly loaded database from a complete one.
    """
    status, _ = get(session, base, _PROBE_TEXT, _PROBE_TEXT, None, _PROBE_DURATION)
    if status not in (Status.FOUND, Status.NOT_FOUND):
        return Preflight.UNAVAILABLE
    status, _ = search(session, base, _PROBE_TEXT, _PROBE_TEXT)
    if status is not Status.FOUND:
        return Preflight.INDEX_NOT_READY
    failed = False
    for track_id in _PROBE_IDS:
        status, _ = get_by_id(session, base, track_id)
        if status is Status.FOUND:
            return Preflight.READY
        failed = failed or status is Status.UNAVAILABLE
    return Preflight.UNAVAILABLE if failed else Preflight.NO_TRACKS


def _composed(text: str) -> str:
    """Return text in NFC. The server doesn't normalise, so a decomposed tag would miss."""
    return unicodedata.normalize("NFC", text)


def _send(
    session: requests.Session, url: str, params: dict[str, str | int] | None = None
) -> requests.Response | None:
    """Send one GET, and return its response, or None if none came back.

    A redirect would send these params to another host, so it stays unfollowed and fails the
    callers' status checks. A lone surrogate in a param raises UnicodeEncodeError here.
    """
    try:
        return session.get(
            url,
            params=params,
            headers={"User-Agent": f"plyrix/{__version__}"},
            timeout=TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException:
        return None


def _is_row(body: dict[str, object]) -> bool:
    """Say whether a 200's object is an LRCLIB row, or came from something else, such as a proxy.

    The pinned server always sends an integer id and a title that may be null. A row with no title
    is still an answer about the track, and matching calls it doubtful.
    """
    title = body.get("trackName", False)
    return type(body.get("id")) is int and (title is None or isinstance(title, str))


def _read_record(response: requests.Response) -> tuple[Status, dict[str, object] | None]:
    """Read a response that should hold one row, or LRCLIB's own error naming why it doesn't."""
    if response.status_code not in (200, 400, 404):
        return Status.UNAVAILABLE, None
    try:
        body: object = response.json()
    except (ValueError, RecursionError):
        return Status.UNAVAILABLE, None
    if not isinstance(body, dict):
        return Status.UNAVAILABLE, None
    if response.status_code == 200:
        return (Status.FOUND, body) if _is_row(body) else (Status.UNAVAILABLE, None)
    if response.status_code == 404 and body.get("name") == _NOT_FOUND_ERROR:
        return Status.NOT_FOUND, None
    if response.status_code == 400 and body.get("name") == _REJECTED_ERROR:
        return Status.REJECTED, None
    return Status.UNAVAILABLE, None
