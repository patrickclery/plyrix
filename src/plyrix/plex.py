"""HTTP client for your own Plex Media Server: read a section, list its tracks, refresh albums.

It follows the LRCLIB client's safety model. The token travels only in the X-Plex-Token header,
a redirect is never followed, and proxy settings are ignored, so the token can reach no host but
the configured one.
"""

import enum
import json
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from plyrix import __version__
from plyrix.domain import Kind

TIMEOUT_SECONDS = 30
# About 1.5 MB of JSON a page on a real section.
PAGE_SIZE = 1000
_CLIENT = "plyrix"
_TRACK_TYPE = "10"
_LYRIC_STREAM_TYPE = 4
# The provider Plex names for a sidecar beside the track. LyricFind's streams name their own.
LOCAL_PROVIDER = "com.plexapp.agents.localmedia"


class Reply(enum.Enum):
    """How the server answered. REFUSED is a 401 or 403, and MISSING a 404."""

    OK = enum.auto()
    UNAVAILABLE = enum.auto()
    REFUSED = enum.auto()
    MISSING = enum.auto()


@dataclass(frozen=True)
class PlexTrack:
    """One track Plex lists: its key, its album's key, and its files' paths as Plex reports them."""

    key: str
    album_key: str
    files: tuple[bytes, ...]


@dataclass(frozen=True)
class SectionInfo:
    """One library section: its key, its type ("artist" for music), its agent and its folders."""

    key: str
    type: str
    agent: str
    locations: tuple[bytes, ...]


@dataclass(frozen=True)
class LyricStream:
    """One lyric stream Plex lists for a track: its id, who provides it, and lrc or txt."""

    id: int
    provider: str
    codec: str


class Verdict(enum.Enum):
    """What Plex attached for a sidecar (D-10).

    LOCAL_TIMED and LOCAL_UNTIMED are a stream from the sidecar, whose body says it is or isn't
    timed. PROVIDER is lyrics from elsewhere only, and NONE no lyrics, or no such track.
    """

    LOCAL_TIMED = "local_timed"
    LOCAL_UNTIMED = "local_untimed"
    PROVIDER = "provider"
    NONE = "none"


def new_session(token: str | None) -> requests.Session:
    """Return a session that sends the token only to the configured server.

    Ignoring the environment keeps a proxy setting or a netrc file from seeing the requests. Only
    a 503 on a GET is asked again, and a redirect is never followed, as it would take the token
    with it.
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
    session.headers.update(
        {
            "User-Agent": f"{_CLIENT}/{__version__}",
            "Accept": "application/json",
            "X-Plex-Client-Identifier": _CLIENT,
            "X-Plex-Product": _CLIENT,
            "X-Plex-Version": __version__,
        }
    )
    if token:
        session.headers["X-Plex-Token"] = token
    return session


def list_tracks(
    session: requests.Session,
    base: str,
    section: int,
    *,
    page_size: int = PAGE_SIZE,
    pages: int | None = None,
) -> tuple[Reply, list[PlexTrack]]:
    """Return every track in a section, a page at a time, or how the server failed to list them.

    A track Plex describes in a way this can't read is left out, so its folder is never refreshed
    on a guess. With pages, only that many pages are read, which is a sample, not a listing.
    """
    url = f"{_root(base)}/library/sections/{section}/all"
    tracks: list[PlexTrack] = []
    offset = 0
    read = 0
    while True:
        response = _send(
            session,
            "GET",
            url,
            params={"type": _TRACK_TYPE},
            headers={
                "X-Plex-Container-Start": str(offset),
                "X-Plex-Container-Size": str(page_size),
            },
        )
        reply = _reply(response)
        if response is None or reply is not Reply.OK:
            return reply, []
        container = _container(response)
        if container is None:
            return Reply.UNAVAILABLE, []
        page = container.get("Metadata", [])
        if not isinstance(page, list):
            return Reply.UNAVAILABLE, []
        tracks.extend(track for item in page if (track := _track(item)) is not None)
        size, total = _count(container.get("size", len(page))), _count(container.get("totalSize"))
        if size is None or _count(container.get("offset", offset)) != offset:
            return Reply.UNAVAILABLE, []
        read += 1
        if pages is not None and read >= pages:
            return Reply.OK, tracks
        if total is None:
            # A response "might not be paginated at all", and then it holds every track, so more
            # than a page means the server didn't page. Exactly a page with no total could be a
            # truncated listing, which could hide a folder's second album, so only that fails.
            return (Reply.UNAVAILABLE, []) if size == page_size else (Reply.OK, tracks)
        if offset + size >= total:
            return Reply.OK, tracks
        # A short listing could hide a folder's second album, and clearing that folder would
        # strand it, so a page that ends the listing early fails the whole listing.
        if size == 0:
            return Reply.UNAVAILABLE, []
        offset += size


def section(
    session: requests.Session, base: str, section_id: int
) -> tuple[Reply, SectionInfo | None]:
    """Return a section's type and folders, or MISSING if the server doesn't list it.

    It reads the whole section list, as a section's own entry doesn't hold its folders, and a
    track listing can't stand in for the type check: a movie section answers it with no tracks.
    """
    response = _send(session, "GET", f"{_root(base)}/library/sections")
    reply = _reply(response)
    if response is None or reply is not Reply.OK:
        # A 404 here is the server, not the section.
        return (Reply.UNAVAILABLE if reply is Reply.MISSING else reply), None
    container = _container(response)
    directories = container.get("Directory", []) if container is not None else None
    if not isinstance(directories, list):
        return Reply.UNAVAILABLE, None
    for directory in directories:
        info = _section(directory)
        if info is not None and info.key == str(section_id):
            return Reply.OK, info
    return Reply.MISSING, None


def folder_albums(tracks: Iterable[PlexTrack]) -> dict[bytes, set[str]]:
    """Map each folder Plex holds files in to the keys of every album with a file there."""
    albums: dict[bytes, set[str]] = {}
    for track in tracks:
        for file in track.files:
            albums.setdefault(os.path.dirname(file), set()).add(track.album_key)
    return albums


def stem_albums(tracks: Iterable[PlexTrack]) -> dict[tuple[bytes, bytes], set[str]]:
    """Map each (folder, stem) Plex holds a file at to the keys of every album with one there.

    A sidecar is named for its track's stem, so this finds the albums a sidecar belongs to.
    """
    albums: dict[tuple[bytes, bytes], set[str]] = {}
    for track in tracks:
        for file in track.files:
            folder, name = os.path.split(file)
            albums.setdefault((folder, os.path.splitext(name)[0]), set()).add(track.album_key)
    return albums


def refresh(session: requests.Session, base: str, key: str) -> Reply:
    """Ask the server to refresh one album's metadata, which is what attaches new sidecars.

    Only a 2xx counts as accepted. A key that isn't a number is never sent, as it would change
    the request's path.
    """
    if not _is_key(key):
        return Reply.UNAVAILABLE
    response = _send(session, "PUT", f"{_root(base)}/library/metadata/{key}/refresh")
    reply = _reply(response)
    return reply if reply in (Reply.OK, Reply.REFUSED) else Reply.UNAVAILABLE


def lyric_streams(
    session: requests.Session, base: str, track_key: str
) -> tuple[Reply, list[LyricStream]]:
    """Return each lyric stream Plex lists for a track, or how the server failed to say.

    A track's own stream entry never says whether a local stream is timed, so this keeps only
    what picks the stream: its id, provider and codec. An entry this can't read is left out.
    """
    if not _is_key(track_key):
        return Reply.UNAVAILABLE, []
    response = _send(session, "GET", f"{_root(base)}/library/metadata/{track_key}")
    reply = _reply(response)
    if response is None or reply is not Reply.OK:
        return reply, []
    container = _container(response)
    metadata = container.get("Metadata") if container is not None else None
    if not isinstance(metadata, list):
        return Reply.UNAVAILABLE, []
    if not metadata or not isinstance(metadata[0], dict):
        return Reply.MISSING, []
    streams: list[LyricStream] = []
    for part in _parts(metadata[0]):
        entries = part.get("Stream", [])
        for entry in entries if isinstance(entries, list) else []:
            stream = _lyric_stream(entry)
            if stream is not None:
                streams.append(stream)
    return Reply.OK, streams


def stream_timed(session: requests.Session, base: str, stream_id: int) -> tuple[Reply, bool | None]:
    """Return how the server answered, and whether it shows a lyric stream as timed.

    The flag is None when the body can't say, and with any reply but OK, when the request
    failed, so a server that stopped answering is never taken for an untimed stream.

    The body is the lyrics themselves, with every line's text, and lyric text is copyrighted, so
    it must not leave this function: nothing of it is logged, kept, raised or returned but the
    timed flag, and only a real JSON boolean counts. The path is built from the integer id, never
    from the stream's own key, as a path from the server could point the request elsewhere.
    """
    if not isinstance(stream_id, int) or isinstance(stream_id, bool) or stream_id < 0:
        return Reply.UNAVAILABLE, None
    response = _send(session, "GET", f"{_root(base)}/library/streams/{stream_id}")
    reply = _reply(response)
    if response is None or reply is not Reply.OK:
        return reply, None
    container = _container(response)
    lyrics = container.get("Lyrics") if container is not None else None
    if not isinstance(lyrics, list) or not lyrics or not isinstance(lyrics[0], dict):
        return Reply.OK, None
    timed = lyrics[0].get("timed")
    return Reply.OK, (timed if isinstance(timed, bool) else None)


def local_stream(kind: Kind, streams: Iterable[LyricStream]) -> LyricStream | None:
    """Return the local stream to judge a sidecar on: one of its own kind, else any local one."""
    local = [stream for stream in streams if stream.provider == LOCAL_PROVIDER]
    codec = kind.value.removeprefix(b".").decode("ascii")
    return next((stream for stream in local if stream.codec == codec), local[0] if local else None)


def verdict(kind: Kind, streams: list[LyricStream], timed: bool | None) -> Verdict:
    """Judge what Plex attached for a sidecar, given the timed flag of its local stream's body.

    timed None means the body couldn't say, which counts as untimed, so it fails an .lrc.
    """
    if not streams:
        return Verdict.NONE
    if local_stream(kind, streams) is None:
        return Verdict.PROVIDER
    return Verdict.LOCAL_TIMED if timed is True else Verdict.LOCAL_UNTIMED


def passes(kind: Kind, verdict: Verdict) -> bool:
    """Say whether a verdict is what a sidecar of this kind should get: timed for an .lrc only."""
    expected = Verdict.LOCAL_TIMED if kind is Kind.LRC else Verdict.LOCAL_UNTIMED
    return verdict is expected


def track_index(tracks: Iterable[PlexTrack]) -> dict[tuple[bytes, bytes], str]:
    """Map each (folder, stem) Plex holds a file at to that track's key, the first if two share.

    A sidecar is named for its track's stem, so this finds the track a sidecar belongs to.
    """
    index: dict[tuple[bytes, bytes], str] = {}
    for track in tracks:
        for file in track.files:
            folder, name = os.path.split(file)
            index.setdefault((folder, os.path.splitext(name)[0]), track.key)
    return index


def _send(
    session: requests.Session,
    method: str,
    url: str,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> requests.Response | None:
    try:
        return session.request(
            method,
            url,
            params=params,
            headers=headers,
            timeout=TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException:
        return None


def _reply(response: requests.Response | None) -> Reply:
    if response is None:
        return Reply.UNAVAILABLE
    if 200 <= response.status_code < 300:
        return Reply.OK
    if response.status_code in (401, 403):
        return Reply.REFUSED
    if response.status_code == 404:
        return Reply.MISSING
    return Reply.UNAVAILABLE


def _container(response: requests.Response) -> dict[str, object] | None:
    """Return a body's MediaContainer, keeping any byte that isn't UTF-8 as it was.

    response.json() would turn such a byte into U+FFFD, and that folder would then never match.
    """
    try:
        body = json.loads(response.content.decode("utf-8", "surrogateescape"))
    except (ValueError, RecursionError):
        return None
    container = body.get("MediaContainer") if isinstance(body, dict) else None
    return container if isinstance(container, dict) else None


def _track(item: object) -> PlexTrack | None:
    if not isinstance(item, dict):
        return None
    key, album_key = _key(item.get("ratingKey")), _key(item.get("parentRatingKey"))
    if key is None or album_key is None:
        return None
    files = [file for part in _parts(item) if (file := _path(part.get("file"))) is not None]
    return PlexTrack(key=key, album_key=album_key, files=tuple(files))


def _parts(item: dict[str, object]) -> Iterator[dict[str, object]]:
    """Yield each Part of each Media of a track that is an object."""
    media = item.get("Media", [])
    for medium in media if isinstance(media, list) else []:
        parts = medium.get("Part", []) if isinstance(medium, dict) else []
        for part in parts if isinstance(parts, list) else []:
            if isinstance(part, dict):
                yield part


def _lyric_stream(entry: object) -> LyricStream | None:
    if not isinstance(entry, dict):
        return None
    kind, stream_id = entry.get("streamType"), entry.get("id")
    provider, codec = entry.get("provider"), entry.get("codec")
    if type(kind) is not int or kind != _LYRIC_STREAM_TYPE:
        return None
    if type(stream_id) is not int or stream_id < 0:
        return None
    if not (isinstance(provider, str) and isinstance(codec, str)):
        return None
    return LyricStream(id=stream_id, provider=provider, codec=codec)


def _section(item: object) -> SectionInfo | None:
    if not isinstance(item, dict):
        return None
    key, kind, agent = item.get("key"), item.get("type"), item.get("agent", "")
    if not (isinstance(key, str) and isinstance(kind, str) and isinstance(agent, str)):
        return None
    locations = item.get("Location", [])
    paths = [
        path
        for location in (locations if isinstance(locations, list) else [])
        if isinstance(location, dict) and (path := _path(location.get("path"))) is not None
    ]
    return SectionInfo(key=key, type=kind, agent=agent, locations=tuple(paths))


def _path(value: object) -> bytes | None:
    """Return a path from a body as the bytes it was sent as, or None if it isn't one."""
    if not isinstance(value, str):
        return None
    try:
        return value.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        return None


def _key(value: object) -> str | None:
    """Return a Plex key as text, which Plex sends as a string of digits."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    return value if isinstance(value, str) and _is_key(value) else None


def _is_key(key: str) -> bool:
    return key.isascii() and key.isdigit()


def _count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _root(base: str) -> str:
    return base.removesuffix("/")
