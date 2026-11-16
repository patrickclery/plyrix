"""Pure core types and decisions. Nothing here touches the filesystem or the network."""

import enum
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

# Raising this re-evaluates settled misses, so a better matcher can reach them.
MATCHER_VERSION = 2

AUDIO_SUFFIXES = frozenset({b".flac", b".mp3", b".m4a", b".ogg", b".opus"})

# Only "Various Artists" was seen in testing. The rest are common spellings of the same idea.
COMPILATION_VALUES = frozenset(
    {
        "various artists",
        "various",
        "va",
        "v.a.",
        "v/a",
        "varios artistas",
        "verschiedene interpreten",
        "artistes divers",
    }
)

# Where a sidecar's row came from. Only the first says plyrix wrote those bytes, so only that one
# may ever be replaced or removed (D-10). A proved .txt holds LRCLIB's text, but the user may have
# put it there. A timed .lrc proves timing and not authorship, as a hand-corrected file looks just
# the same.
ORIGIN_WRITTEN = "written"
ORIGIN_PROVED = "proved"
ORIGIN_TIMED = "timed"
REPLACEABLE_ORIGINS = frozenset({ORIGIN_WRITTEN})

_MAX_STORABLE_ID = 2**63


class Kind(enum.Enum):
    """The two sidecar kinds, which are the only suffixes plyrix ever writes."""

    LRC = b".lrc"
    TXT = b".txt"


class Outcome(enum.Enum):
    """How a track's last lookup, or its attempt at one, ended. These are the ledger's words."""

    SYNCED = "synced"
    PLAIN = "plain"
    PLAIN_SKIPPED = "plain_skipped"
    INSTRUMENTAL = "instrumental"
    NO_MATCH = "no_match"
    UNTAGGED = "untagged"
    UNREADABLE = "unreadable"


class MissReason(enum.Enum):
    """Why a track has no match, from furthest from one to closest."""

    NOT_FOUND = "not_found"
    REJECTED = "rejected"
    DOUBTFUL_ARTIST = "doubtful_artist"
    DOUBTFUL_TITLE = "doubtful_title"
    DOUBTFUL_DURATION = "doubtful_duration"
    DOUBTFUL_VERSION = "doubtful_version"
    TIMING_REJECTED = "timing_rejected"


class PlainReason(enum.Enum):
    """Why a track with plain lyrics has no .txt plyrix wrote. Stored as a reason, like a miss's."""

    BLOCKED_FOREIGN = "blocked_foreign"


class SyncedReason(enum.Enum):
    """Why a track with a .lrc needs a word said about it. Stored as a reason, like a miss's."""

    LRC_STALE = "lrc_stale"


Reason = MissReason | PlainReason | SyncedReason


class SidecarState(enum.Enum):
    """Who a sidecar's name belongs to."""

    ABSENT = enum.auto()
    OWNED = enum.auto()
    EDITED = enum.auto()
    FOREIGN = enum.auto()
    NOT_A_FILE = enum.auto()


class Found(enum.Enum):
    """What a finished lookup found for a track."""

    SYNCED = enum.auto()
    PLAIN = enum.auto()
    INSTRUMENTAL = enum.auto()
    NOTHING = enum.auto()


class Step(enum.Enum):
    """What a run does about a finished lookup."""

    PUBLISH_LRC = enum.auto()
    PUBLISH_LRC_REMOVE_TXT = enum.auto()
    PUBLISH_TXT = enum.auto()
    KEEP_TXT = enum.auto()
    ADOPT_TXT = enum.auto()
    BLOCK_TXT = enum.auto()
    KEEP_LRC = enum.auto()
    ADOPT_LRC = enum.auto()
    BLOCK_LRC = enum.auto()
    MARK_TIMED = enum.auto()
    REVALIDATE = enum.auto()
    NOTHING = enum.auto()


# LRCLIB can answer these differently with another dump, so none of them is final.
_SOFT_OUTCOMES = frozenset(
    {Outcome.PLAIN, Outcome.PLAIN_SKIPPED, Outcome.INSTRUMENTAL, Outcome.NO_MATCH}
)


@dataclass(frozen=True)
class Track:
    """An audio file. A sidecar's destination derives only from these three values."""

    dir: bytes
    name: bytes
    stem: bytes

    def __post_init__(self) -> None:
        if b"/" in self.name or b"\0" in self.name or self.name in (b".", b".."):
            raise ValueError("a track name must be one plain directory entry")
        # A stem that is not part of the name could point a sidecar at another directory.
        if not self.stem or not self.name.startswith(self.stem):
            raise ValueError("a track stem must be the start of its name")


@dataclass(frozen=True)
class TrackTags:
    """The named tag fields plyrix uses. Embedded lyrics have no place here.

    year is used only by the retry rule after a dump update, and is kept out of tags_fingerprint
    on purpose: adding it there would change every stored digest and look every track up again.
    """

    artist: str | None
    albumartist: str | None
    title: str | None
    album: str | None
    duration: float | None
    year: str | None = None


@dataclass(frozen=True)
class TrackResult:
    """What the ledger remembers of a track. Its tags are only ever a digest."""

    size: int
    mtime_ns: int
    tags_sha256: str | None
    outcome: Outcome
    reason: Reason | None
    dump_id: str
    matcher_version: int
    lrclib_id: int | None
    # For the retry rule after a dump update (D-09): a year, two 8-byte keys and when the track
    # was first seen, as epoch seconds. None means unknown, which counts as affected.
    release_year: int | None = None
    artist_key: int | None = None
    title_key: int | None = None
    first_seen: float | None = None
    # A finished walk of the whole library didn't reach the track, so its dump's later change sets
    # may have been pruned.
    unseen: bool = False

    @property
    def is_soft(self) -> bool:
        """Say whether another dump could change this result, so a retry can be worth it."""
        return self.outcome in _SOFT_OUTCOMES


def release_year(year: str | None) -> int | None:
    """Return a tag's year as a number when its first four characters are one from 1000 to 2999."""
    head = (year or "")[:4]
    if len(head) != 4 or not head.isascii() or not head.isdigit():
        return None
    value = int(head)
    return value if 1000 <= value <= 2999 else None


def tags_fingerprint(found: TrackTags) -> str:
    """Return a digest of the tags a lookup depends on, so the ledger never holds their text.

    The duration is rounded to the whole second a lookup sends. A tag edit can shift the estimate
    for some files by a fraction, and that asks LRCLIB nothing new.
    """
    duration = None if found.duration is None else round(found.duration, 0)
    fields = [found.artist, found.albumartist, found.title, found.album, duration]
    return hashlib.sha256(json.dumps(fields).encode("ascii")).hexdigest()


@dataclass(frozen=True)
class DumpBasis:
    """What the ledger knows of a dump a row's answer came from.

    chain_complete says every later dump LRCLIB served came with a complete diff. dump_time is
    the time in its name as epoch seconds, and year that time's year.
    """

    chain_complete: bool
    dump_time: float
    year: int


def affected_by_update(
    result: TrackResult,
    *,
    full: bool,
    basis: DumpBasis | None,
    key_changed: bool,
    ctime: float | None,
) -> bool:
    """Say whether a dump update could change a soft result from an earlier dump (D-09).

    In order, cheapest first, each from D-09 and research Pattern 5:
    - full asks everything again, as before the targeted retry;
    - a row a finished walk didn't reach may have lost the change sets after its dump to the prune;
    - an unknown dump, or one with an unstable or missing diff after it, can't be reasoned about;
    - a NULL key, as on a row from before the keys were stored, can't be matched to a diff;
    - the diff holds the row's artist or title key, so LRCLIB may answer it differently now;
    - the track was released in or after its dump's year, so LRCLIB may have it now;
    - the track was first seen after its dump, or, when that is unknown, its file changed after.
    """
    if full or result.unseen or basis is None or not basis.chain_complete:
        return True
    if result.artist_key is None or result.title_key is None:
        return True
    if key_changed:
        return True
    if result.release_year is not None and result.release_year >= basis.year:
        return True
    seen = result.first_seen if result.first_seen is not None else ctime
    return seen is not None and seen > basis.dump_time


def needs_lookup(
    result: TrackResult | None,
    tags_hash: str | None,
    dump_id: str,
    plain: str,
    *,
    txt: SidecarState,
    matcher_version: int = MATCHER_VERSION,
    affected: bool = True,
) -> bool:
    """Say whether LRCLIB should be asked about a track again.

    A caller whose stat of the audio is unchanged passes the row's own hash, so no tags are read.
    With no dump id, nothing says the dump is the same, so every soft result is due. txt says who
    the track's .txt name belongs to now, which a plain result depends on. affected says whether
    the dump update since a soft result's own dump could change it (affected_by_update).
    """
    if result is None or result.matcher_version != matcher_version:
        return True
    if result.tags_sha256 != tags_hash:
        return True
    if plain == "write" and _plain_result_is_out_of_date(result, txt):
        return True
    if result.is_soft:
        return not dump_id or (result.dump_id != dump_id and affected)
    return result.outcome not in (Outcome.UNTAGGED, Outcome.UNREADABLE)


def _plain_result_is_out_of_date(result: TrackResult, txt: SidecarState) -> bool:
    """Say whether a result about a .txt no longer says what is at its name.

    A plain result is true only of a .txt still plyrix's, or a deleted one would never come back and
    a replaced one would still count as covered. A block holds until something else has the name.
    """
    if result.outcome is Outcome.PLAIN_SKIPPED:
        return True
    if result.outcome is not Outcome.PLAIN:
        return False
    if result.reason is PlainReason.BLOCKED_FOREIGN:
        return txt in (SidecarState.ABSENT, SidecarState.OWNED)
    return txt is not SidecarState.OWNED


def decide_found(
    found: Found, txt: SidecarState, plain: str, txt_matches: bool
) -> tuple[Step, str]:
    """Return what to do about a finished lookup, and the coverage count the track falls under.

    txt_matches says the text already at the .txt's name equals what was found, which is the only
    way a .txt plyrix didn't write becomes its own.
    """
    if found is Found.SYNCED:
        step = Step.PUBLISH_LRC_REMOVE_TXT if txt is SidecarState.OWNED else Step.PUBLISH_LRC
        return step, "synced"
    if found is Found.INSTRUMENTAL:
        return Step.NOTHING, "instrumental"
    if found is Found.NOTHING:
        return Step.NOTHING, "no_match"
    if plain == "skip":
        return Step.NOTHING, "plain_skipped"
    if txt is SidecarState.ABSENT:
        return Step.PUBLISH_TXT, "plain"
    if txt is SidecarState.OWNED:
        return Step.KEEP_TXT, "plain"
    if txt_matches and txt in (SidecarState.EDITED, SidecarState.FOREIGN):
        return Step.ADOPT_TXT, "plain"
    return Step.BLOCK_TXT, "blocked_foreign"


def decide_existing(lrc: SidecarState, lrc_valid: bool, recheck: bool) -> Step:
    """Return what to do about whatever is at a track's .lrc name, before any lookup.

    lrc_valid says a file there passes timing against the track, and only matters for one with no
    row to prove it. recheck says a row's .lrc may no longer suit its track.
    """
    if lrc is SidecarState.ABSENT:
        return Step.NOTHING
    if lrc is SidecarState.OWNED:
        return Step.REVALIDATE if recheck else Step.KEEP_LRC
    if lrc is SidecarState.NOT_A_FILE or not lrc_valid:
        return Step.BLOCK_LRC
    return Step.MARK_TIMED if lrc is SidecarState.EDITED else Step.ADOPT_LRC


def stale_lookup_due(
    result: TrackResult | None, origin: str, dump_id: str, *, affected: bool = True
) -> bool:
    """Say whether a kept stale .lrc is worth checking again, so a later dump can fix it.

    This is the rule needs_lookup gives a miss. A .lrc that can't be replaced has nothing to gain.
    """
    return (
        result is not None
        and result.reason is SyncedReason.LRC_STALE
        and origin in REPLACEABLE_ORIGINS
        and (not dump_id or (result.dump_id != dump_id and affected))
    )


def lookup_artist(artist: str | None, album_artist: str | None) -> str | None:
    """Return the artist to search for, falling back to a non-compilation album artist."""
    track_artist = (artist or "").strip()
    if track_artist:
        return track_artist
    fallback = (album_artist or "").strip()
    return fallback if fallback and fallback.casefold() not in COMPILATION_VALUES else None


def show(path: bytes) -> str:
    """Return a path as printable ASCII, so a name can't drive a terminal or fail on output."""
    return path.decode("utf-8", "backslashreplace").encode("unicode_escape").decode("ascii")


def record_id(record: Mapping[str, object]) -> int | None:
    """Return a record's id when it is an integer the ledger can store, else None."""
    value = record.get("id")
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < _MAX_STORABLE_ID:
        return value
    return None
