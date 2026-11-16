"""The official dump listing: which LRCLIB dump is newest, read defensively.

The listing is an undocumented JSON list of an R2 bucket, the one LRCLIB's own db-dumps page
reads, so nothing in it is trusted. A key becomes a dump's name only when it fullmatches
DUMP_NAME, and anything off, from an HTTP error to a changed shape, means no update (D-02). No URL
is built here: the script names the listing, and builds a dump's URL only from a checked name.
"""

import json
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from plyrix import __version__
from plyrix.doctor import DUMP_NAME

TIMEOUT_SECONDS = 30
MAX_LISTING_BYTES = 1_000_000
# How much of a dump is read before it is accepted: enough for the gzip header and the 100-byte
# SQLite header inside it, which give the version and the exact decompressed size.
PROBE_BYTES = 65_536
_SQLITE_MAGIC = b"SQLite format 3\x00"
_GZIP_MAGIC = b"\x1f\x8b\x08"
_FHCRC, _FEXTRA, _FNAME, _FCOMMENT = 0x02, 0x04, 0x08, 0x10
# What probe_header returns for bytes that hold no SQLite header, which the check refuses.
NOT_SQLITE = "no SQLite header"
# What probe_header returns for a header whose database size SQLite marks out of date, so the
# decompressed size, and the room the update needs, aren't known.
STALE_SIZE = "a database size SQLite marks out of date"
# Below a terabyte. The 2026-10-30 dump is 49 GB.
MAX_DUMP_SIZE = 10**12
_TIME_FORMAT = "%Y%m%dT%H%M%SZ"
# Where the timestamp sits in a name that fullmatches DUMP_NAME.
_STAMP = slice(len("lrclib-db-dump-"), len("lrclib-db-dump-") + 16)


@dataclass(frozen=True)
class Listed:
    """One dump the listing names, checked: its name, size, and the fields it may lack."""

    key: str
    size: int
    etag: str | None
    uploaded: str | None


@dataclass(frozen=True)
class Header:
    """What a dump's SQLite header says: its version, page size and page count.

    size_valid is SQLite's own rule for the in-header database size: it holds only when the
    change counter at offset 24 equals the version-valid-for number at offset 92. Otherwise
    page_count may be stale, and unpacked too small.
    """

    user_version: int
    page_size: int
    page_count: int
    size_valid: bool = True

    @property
    def unpacked(self) -> int:
        """The decompressed database's exact size in bytes."""
        return self.page_size * self.page_count


@dataclass(frozen=True)
class Listing:
    """What a read of the listing found: the newest dump, or a problem phrase, and truncation."""

    newest: Listed | None
    problem: str | None
    truncated: bool


def new_session() -> requests.Session:
    """Return a session for the listing.

    Unlike the LRCLIB client, this one honours proxy variables: it sends nothing from the
    library, and curl in the update script, which fetches the dump, honours the same ones. A
    redirect is never followed, and a 502, 503 or 504 is asked twice more.
    """
    session = requests.Session()
    session.headers["User-Agent"] = f"plyrix/{__version__}"
    retry = Retry(
        total=2,
        connect=2,
        read=0,
        other=0,
        redirect=0,
        status=2,
        status_forcelist=[502, 503, 504],
        allowed_methods=["GET"],
        backoff_factor=1,
        backoff_max=4,
        raise_on_status=False,
        raise_on_redirect=False,
    )
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def dump_time(name: str) -> datetime:
    """Return the UTC time in a dump's name, which must fullmatch DUMP_NAME."""
    if not DUMP_NAME.fullmatch(name):
        raise ValueError("not a dump's name")
    return datetime.strptime(name[_STAMP], _TIME_FORMAT).replace(tzinfo=UTC)


def newest(body: object) -> Listed | None:
    """Return the newest well-formed dump in a listing's body, or None when there is none."""
    if not isinstance(body, dict):
        return None
    objects = body.get("objects")
    if not isinstance(objects, list):
        return None
    found = [listed for obj in objects if (listed := _listed(obj)) is not None]
    return max(found, key=lambda listed: listed.key[_STAMP], default=None)


def _listed(obj: object) -> Listed | None:
    if not isinstance(obj, dict):
        return None
    key, size = obj.get("key"), obj.get("size")
    if not isinstance(key, str) or not DUMP_NAME.fullmatch(key):
        return None
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size < MAX_DUMP_SIZE:
        return None
    try:
        dump_time(key)
    except ValueError:
        return None
    etag, uploaded = obj.get("etag"), obj.get("uploaded")
    return Listed(
        key,
        size,
        etag if isinstance(etag, str) else None,
        uploaded if isinstance(uploaded, str) else None,
    )


def read_listing(session: requests.Session, url: str) -> Listing:
    """Fetch and read the listing. Any failure is a Listing with no newest and a problem phrase.

    The phrase holds no response text. At most MAX_LISTING_BYTES are read.
    """
    try:
        with session.get(
            url, stream=True, allow_redirects=False, timeout=TIMEOUT_SECONDS
        ) as response:
            if 300 <= response.status_code < 400:
                return Listing(None, "the listing answered with a redirect", False)
            if response.status_code != 200:
                return Listing(None, f"the listing answered HTTP {response.status_code}", False)
            body = response.raw.read(MAX_LISTING_BYTES + 1, decode_content=True)
    except (requests.RequestException, urllib3.exceptions.HTTPError, OSError):
        return Listing(None, "the listing didn't answer", False)
    if len(body) > MAX_LISTING_BYTES:
        return Listing(None, "the listing is more than 1 MB", False)
    try:
        parsed: object = json.loads(body)
    except (ValueError, RecursionError):
        return Listing(None, "the listing isn't JSON", False)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("objects"), list):
        return Listing(None, "the listing has an unexpected shape", False)
    truncated = parsed.get("truncated") is True
    found = newest(parsed)
    if found is None:
        problem = (
            "the listing was cut short and names no dump"
            if truncated
            else "the listing names no dump"
        )
        return Listing(None, problem, truncated)
    return Listing(found, None, truncated)


def sqlite_header(gz: bytes) -> Header | None:
    """Read the SQLite header at the start of a gzip stream, or None if it isn't one.

    Every offset is checked, so a short or hostile header gives None and never raises.
    """
    if len(gz) < 18 or gz[:3] != _GZIP_MAGIC:
        return None
    flags, i = gz[3], 10
    if flags & _FEXTRA:
        i += 2 + int.from_bytes(gz[i : i + 2], "little")
    for bit in (_FNAME, _FCOMMENT):
        if flags & bit:
            end = gz.find(b"\0", i)
            if end < 0:
                return None
            i = end + 1
    if flags & _FHCRC:
        i += 2
    if i >= len(gz):
        return None
    try:
        head = zlib.decompressobj(-15).decompress(gz[i:], 100)
    except zlib.error:
        return None
    if len(head) < 100 or head[:16] != _SQLITE_MAGIC:
        return None
    size = int.from_bytes(head[16:18], "big")
    page_size = 65_536 if size == 1 else size
    # SQLite's page size is a power of two from 512 to 65,536.
    if page_size < 512 or page_size & (page_size - 1):
        return None
    page_count = int.from_bytes(head[28:32], "big")
    if page_count == 0:
        return None
    return Header(
        int.from_bytes(head[60:64], "big", signed=True),
        page_size,
        page_count,
        size_valid=head[24:28] == head[92:96],
    )


def probe_header(session: requests.Session, url: str) -> Header | str:
    """Read a dump's first PROBE_BYTES with one range request, and parse its SQLite header.

    At most PROBE_BYTES are read, even from a server that ignores the range and answers 200 with
    the whole file. Returns the Header, or a problem phrase holding no response text. A stream
    that holds bytes but no SQLite header gives a Header-less problem the caller refuses.
    """
    try:
        with session.get(
            url,
            headers={"Range": f"bytes=0-{PROBE_BYTES - 1}"},
            stream=True,
            allow_redirects=False,
            timeout=TIMEOUT_SECONDS,
        ) as response:
            if 300 <= response.status_code < 400:
                return "a redirect"
            if response.status_code not in (200, 206):
                return f"HTTP {response.status_code}"
            data = response.raw.read(PROBE_BYTES, decode_content=False)
    except (requests.RequestException, urllib3.exceptions.HTTPError, OSError):
        return "no answer"
    found = sqlite_header(data[:PROBE_BYTES])
    if found is None:
        return NOT_SQLITE
    # A stale size would let the space check pass, and the unpack fill the disk LRCLIB serves from.
    return found if found.size_valid else STALE_SIZE
