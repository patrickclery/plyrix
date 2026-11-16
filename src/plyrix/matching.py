"""Decide whether a candidate is really this track. Nothing here touches disk or network."""

import hashlib
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from plyrix import lrc
from plyrix._lay_folds import FOLDS, TABLE_SIZE
from plyrix.domain import MissReason, record_id

DURATION_TOLERANCE = 2.0

# The order and characters of LRCLIB's own preparation, at server/src/utils.rs on the pinned rev.
_TO_SPACE = str.maketrans(dict.fromkeys('`~!@#$%^&*()_|+-=?;:",.<>{}[]\\/\x00\n', " "))
_TO_REMOVE = str.maketrans(dict.fromkeys("'’"))
_WHITESPACE = re.compile(r"\s+")

_FEATURING = re.compile(r"[\s(\[](?:feat\.|ft\.|featuring\b).*$", re.IGNORECASE | re.DOTALL)
_BRACKETED = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")
_TRAILING_SEPARATOR = " - "
# Bounds the passes for a hostile title. Anything nested deeper stays in the title, so it's a miss.
_MAX_QUALIFIER_DEPTH = 3
_YEAR = re.compile(r"[0-9]{4}")

# Starting values, within D-05 to D-08. VALD-02 measures them, and raising MATCHER_VERSION
# re-evaluates settled misses after any change.
_LIVE_WORDS = frozenset({"live", "unplugged", "concert"})
_LIVE_PHRASES = ("ao vivo", "en vivo", "en directo")
_VERSION_WORDS = _LIVE_WORDS | {
    "acoustic",
    "stripped",
    "demo",
    "rehearsal",
    "remix",
    "rmx",
    "clean",
    "mix",
    "dub",
    "instrumental",
    "karaoke",
    "cover",
    "session",
    "sessions",
    "alternate",
    "alt",
    "take",
    "edit",
    "extended",
    "slowed",
    "reverb",
    "nightcore",
    "sped",
}
_NEUTRAL_WORDS = frozenset(
    {
        "remaster",
        "remastered",
        "mastered",
        "album",
        "version",
        "single",
        "original",
        "stereo",
        "mono",
        "explicit",
        "bonus",
        "track",
        "deluxe",
        "edition",
        "lp",
        "ep",
        "official",
        "audio",
        "video",
        "radio",
    }
)
# Whole words only, as Feathers and Prodigal are names. With and From aren't here, as a phrase
# starting with either can be the only thing telling two songs apart.
_CREDIT_WORDS = frozenset({"feat", "featuring", "ft", "prod", "produced"})


@dataclass(frozen=True)
class Candidate:
    """One LRCLIB row, with each field None unless it has the shape it should."""

    id: int | None
    artist: str | None
    title: str | None
    album: str | None
    duration: float | None
    synced: str | None
    plain: str | None
    instrumental: bool


@dataclass(frozen=True)
class Verdict:
    """What the candidates add up to. miss means something only when the other three are empty.

    miss_id is the row whose failure gave that reason, so it says what came closest. It chose
    nothing, and is None when no row answered.
    """

    synced: Candidate | None
    plain: tuple[Candidate, ...]
    instrumental: Candidate | None
    miss: MissReason
    miss_id: int | None


def prepare(text: str) -> str:
    """Fold text the way LRCLIB does, after NFC, which the server doesn't do itself."""
    nfc = unicodedata.normalize("NFC", text)
    folded = "".join(FOLDS.get(c, c.lower()) if ord(c) < TABLE_SIZE else c for c in nfc)
    spaced = folded.translate(_TO_SPACE).translate(_TO_REMOVE).lower()
    return _WHITESPACE.sub(" ", spaced).strip()


def primary_artist(name: str) -> str:
    """Cut a featured artist from a name. Only these words do, as Foo & Bar can be one act."""
    match = _FEATURING.search(name)
    return name if match is None else name[: match.start()].rstrip(" \t\n([")


def stripped_title(title: str) -> str:
    """Return a title without its neutral and version qualifiers."""
    return _split_title(title)[0]


def search_form(artist: str, title: str) -> tuple[str, str]:
    """Return the artist and title to search with, keeping the tag's text if nothing would be left.

    A search is a phrase query, so a qualifier the row doesn't hold would hide it.
    """
    return primary_artist(artist) or artist, stripped_title(title) or title


def lookup_keys(artist: str | None, title: str | None) -> tuple[int | None, int | None]:
    """Return 8-byte keys for an artist and a title, as a lookup would search for them.

    Both sides of an LRCLIB diff go through this one function: a library track's tags, and a
    changed LRCLIB row's names. So a key match means LRCLIB may now answer a lookup of that
    artist or title differently. A collision only costs an extra retry. A field that prepares to
    nothing has no key.
    """
    return (
        _key(primary_artist(artist) or artist if artist else None),
        _key(stripped_title(title) or title if title else None),
    )


def _key(text: str | None) -> int | None:
    if not text:
        return None
    prepared = prepare(text)
    if not prepared:
        return None
    digest = hashlib.sha256(prepared.encode("utf-8", "surrogatepass")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def lookup_steps(artist: str, title: str, album: str | None) -> list[tuple[str, str, str | None]]:
    """Return each /api/get to try in order, as artist, title and album, with no step repeated.

    The server takes the oldest row matching the album exactly, so each step asks more widely.
    """
    forms = [
        (artist, title, album or None),
        (artist, title, None),
        (*search_form(artist, title), None),
    ]
    return list(dict.fromkeys(forms))


def candidate_from(record: Mapping[str, object]) -> Candidate:
    """Read a row, trusting nothing about its fields, as polluted rows are known to exist."""
    return Candidate(
        id=record_id(record),
        artist=_text(record.get("artistName")),
        title=_text(record.get("trackName")),
        album=_text(record.get("albumName")),
        duration=_duration(record.get("duration")),
        synced=_text(record.get("syncedLyrics")),
        plain=_text(record.get("plainLyrics")),
        instrumental=record.get("instrumental") is True,
    )


def check(
    candidate: Candidate, artist: str, title: str, album: str | None, duration: float
) -> MissReason | None:
    """Return why a candidate isn't this track, or None when nothing says it isn't.

    The first failure names the reason. An empty key never matches, so a name of only punctuation
    can't pass as a match of another.
    """
    wanted_core, wanted_versions = _split_title(title)
    found_core, found_versions = _split_title(candidate.title or "")
    wanted_title, found_title = prepare(wanted_core), prepare(found_core)
    if not wanted_title or not found_title:
        return MissReason.DOUBTFUL_TITLE
    wanted_artist = prepare(primary_artist(artist))
    if not wanted_artist or wanted_artist != prepare(primary_artist(candidate.artist or "")):
        return MissReason.DOUBTFUL_ARTIST
    if wanted_title != found_title:
        return MissReason.DOUBTFUL_TITLE
    if _duration_gap(candidate, duration) > DURATION_TOLERANCE:
        return MissReason.DOUBTFUL_DURATION
    if wanted_versions != found_versions:
        return MissReason.DOUBTFUL_VERSION
    if album and candidate.album and _looks_live(album) != _looks_live(candidate.album):
        return MissReason.DOUBTFUL_VERSION
    return None


def judge(
    records: Iterable[Mapping[str, object]],
    artist: str,
    title: str,
    album: str | None,
    duration: float,
) -> Verdict:
    """Choose what to write from LRCLIB's rows. A row that fails a check counts for nothing.

    A synced row needs its timing to pass, and an instrumental one counts only when no accepted
    row has lyrics. Ranking is the same album, then the smallest duration gap, then the lowest id.
    """
    accepted: list[Candidate] = []
    misses: list[tuple[MissReason, int | None]] = []
    for record in records:
        candidate = candidate_from(record)
        reason = check(candidate, artist, title, album, duration)
        if reason is None:
            accepted.append(candidate)
        else:
            misses.append((reason, candidate.id))

    wanted_album = prepare(album or "")

    def rank(candidate: Candidate) -> tuple[bool, float, float]:
        same_album = bool(wanted_album) and prepare(candidate.album or "") == wanted_album
        lowest_id = math.inf if candidate.id is None else candidate.id
        return (not same_album, _duration_gap(candidate, duration), lowest_id)

    ranked = sorted(accepted, key=rank)
    timed = [(c, c.synced) for c in ranked if c.synced is not None]
    passing: list[Candidate] = []
    for candidate, text in timed:
        if lrc.validate(text, duration) is None:
            passing.append(candidate)
        else:
            misses.append((MissReason.TIMING_REJECTED, candidate.id))
    plain = tuple(c for c in ranked if c.plain is not None)
    instrumental = None if timed or plain else next((c for c in ranked if c.instrumental), None)
    # The first row asked wins a tie, and an accepted row with no text is the nearest thing to one.
    miss, miss_id = max(
        misses,
        key=lambda found: list(MissReason).index(found[0]),
        default=(MissReason.NOT_FOUND, ranked[0].id if ranked else None),
    )
    return Verdict(
        synced=passing[0] if passing else None,
        plain=plain,
        instrumental=instrumental,
        miss=miss,
        miss_id=miss_id,
    )


def _split_title(title: str) -> tuple[str, frozenset[str]]:
    """Return a title's core and the words of its version qualifiers.

    Qualifiers are only text in brackets or after a final " - ", so bare words such as Live and Let
    Die stay in the title.
    """
    versions: set[str] = set()

    def take_qualifier(match: re.Match[str]) -> str:
        words = _qualifier_words(match.group(1) or match.group(2) or "")
        if words is None:
            return match.group(0)
        versions.update(words)
        return " "

    core = title
    for _ in range(_MAX_QUALIFIER_DEPTH):
        core = _BRACKETED.sub(take_qualifier, core)
    for _ in range(_MAX_QUALIFIER_DEPTH):
        head, separator, tail = core.rstrip().rpartition(_TRAILING_SEPARATOR)
        words = _qualifier_words(tail) if separator else None
        if words is None:
            break
        versions.update(words)
        core = head
    return " ".join(core.split()), frozenset(versions)


def _qualifier_words(segment: str) -> frozenset[str] | None:
    """Return the words a qualifier adds to the version set, or None if it's part of the title.

    A year counts only beside a neutral word, as Song (1984) and Song (2012) can be two songs.
    """
    prepared = prepare(segment)
    words = prepared.split()
    if not words:
        return frozenset()
    if _VERSION_WORDS.intersection(words) or _holds_phrase(prepared, _LIVE_PHRASES):
        return frozenset(words)
    if any(word in _NEUTRAL_WORDS for word in words) and all(
        word in _NEUTRAL_WORDS or _YEAR.fullmatch(word) for word in words
    ):
        return frozenset()
    return frozenset() if words[0] in _CREDIT_WORDS else None


def _looks_live(album: str) -> bool:
    prepared = prepare(album)
    return bool(_LIVE_WORDS.intersection(prepared.split())) or _holds_phrase(
        prepared, _LIVE_PHRASES
    )


def _holds_phrase(prepared: str, phrases: tuple[str, ...]) -> bool:
    return any(f" {phrase} " in f" {prepared} " for phrase in phrases)


def _duration_gap(candidate: Candidate, duration: float) -> float:
    return math.inf if candidate.duration is None else abs(candidate.duration - duration)


def _text(value: object) -> str | None:
    """Return text with something to show, as control characters alone would make an empty file."""
    if not isinstance(value, str) or not any(c.isprintable() and not c.isspace() for c in value):
        return None
    return value


def _duration(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return None
    return seconds if math.isfinite(seconds) else None
