"""The pure core: track names can't aim a sidecar elsewhere, and retries follow one rule."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from plyrix import domain
from plyrix.domain import (
    COMPILATION_VALUES,
    MATCHER_VERSION,
    DumpBasis,
    Kind,
    MissReason,
    Outcome,
    PlainReason,
    SidecarState,
    Track,
    TrackResult,
    TrackTags,
    affected_by_update,
    lookup_artist,
    needs_lookup,
    record_id,
    release_year,
    tags_fingerprint,
)
from plyrix.sidecars import encode


@pytest.mark.parametrize(
    ("name", "stem"),
    [
        (b"a/b.flac", b"a/b"),
        (b"../b.flac", b"../b"),
        (b"a\0b.flac", b"a\0b"),
        (b".", b"."),
        (b"..", b".."),
        (b"", b""),
        (b"t.flac", b""),
        (b"t.flac", b"../t"),
        (b"t.flac", b"other"),
    ],
)
def test_track_rejects_unsafe_names(name: bytes, stem: bytes) -> None:
    with pytest.raises(ValueError):
        Track(b"dir", name, stem)


@pytest.mark.parametrize(
    ("name", "stem"),
    [(b"t.flac", b"t"), (b"U.S.A..flac", b"U.S.A."), (b"caf\xe9-\xff.flac", b"caf\xe9-\xff")],
)
def test_track_accepts_real_names(name: bytes, stem: bytes) -> None:
    assert Track(b"dir", name, stem).stem == stem


def test_kinds_are_exact() -> None:
    assert {kind.value for kind in Kind} == {b".lrc", b".txt"}


@pytest.mark.parametrize(
    ("artist", "album_artist", "expected"),
    [
        ("Band", "Other", "Band"),
        ("  Band ", None, "Band"),
        (None, "Other", "Other"),
        ("  ", " Other ", "Other"),
        (None, "Various Artists", None),
        (None, "VARIOUS ARTISTS", None),
        (None, " various artists ", None),
        (None, None, None),
        ("", "", None),
    ],
)
def test_lookup_artist(artist: str | None, album_artist: str | None, expected: str | None) -> None:
    assert lookup_artist(artist, album_artist) == expected


@pytest.mark.parametrize("value", sorted(COMPILATION_VALUES))
def test_lookup_artist_skips_every_compilation_value(value: str) -> None:
    assert lookup_artist(None, value) is None
    assert lookup_artist("  ", value.upper()) is None


def test_lookup_artist_keeps_a_compilation_value_named_as_the_artist() -> None:
    # D-15 applies to the fallback. A track tagged this way says who it thinks the artist is.
    assert lookup_artist("Various Artists", "Other") == "Various Artists"


@given(st.none() | st.text(), st.none() | st.text())
def test_lookup_artist_gives_nothing_or_a_clean_name(
    artist: str | None, album_artist: str | None
) -> None:
    result = lookup_artist(artist, album_artist)

    if result is None:
        assert not (artist or "").strip()
    else:
        assert result == result.strip()
        assert result
        if not (artist or "").strip():
            assert result.casefold() not in COMPILATION_VALUES


def test_show_escapes_a_terminal_escape() -> None:
    assert domain.show(b"[bold]\x1b[31mt.flac") == "[bold]\\x1b[31mt.flac"


def test_show_escapes_a_line_break() -> None:
    assert "\n" not in domain.show(b"written=1\nwritten=99")


def test_show_keeps_a_plain_path_readable() -> None:
    assert domain.show(b"Album/01 - t.flac") == "Album/01 - t.flac"


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        (b"caf\xe9-\xff.flac", "caf\\\\xe9-\\\\xff.flac"),
        (b"\xed\xa0\x80.flac", "\\\\xed\\\\xa0\\\\x80.flac"),
        ("café.flac".encode(), "caf\\xe9.flac"),
    ],
)
def test_show_takes_any_bytes_and_passes_none_through_raw(name: bytes, shown: str) -> None:
    assert domain.show(name) == shown


@given(st.binary())
def test_show_output_is_always_printable_ascii(path: bytes) -> None:
    shown = domain.show(path)

    assert shown.isascii()
    assert shown.isprintable()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (7, 7),
        (0, 0),
        (-1, None),
        (2**63, None),
        ("7", None),
        (True, None),
        (7.0, None),
        (None, None),
    ],
)
def test_record_id_takes_only_a_storable_integer(value: object, expected: int | None) -> None:
    assert record_id({"id": value}) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[00:01.00] x\n", b"[00:01.00] x\n"),
        ("[00:01.00] x", b"[00:01.00] x\n"),
        ("\ufeff[00:01.00] x", b"[00:01.00] x\n"),
        ("[00:01.00] x\r\n[00:02.00] y\r\n", b"[00:01.00] x\n[00:02.00] y\n"),
        ("[00:01.00] \x1b[31mx\t\x00y", b"[00:01.00] [31mx\ty\n"),
        ("[00:01.00] café", "[00:01.00] café\n".encode()),
        ("[00:01.00] \ud800", b"[00:01.00] ?\n"),
    ],
)
def test_encode(text: str, expected: bytes) -> None:
    assert encode(text) == expected


def settled(
    outcome: Outcome,
    dump_id: str = "dump-a",
    tags_sha256: str | None = "hash",
    matcher_version: int = MATCHER_VERSION,
    reason: PlainReason | None = None,
) -> TrackResult:
    return TrackResult(3, 5, tags_sha256, outcome, reason, dump_id, matcher_version, None)


SOFT_NOT_PLAIN = [Outcome.INSTRUMENTAL, Outcome.NO_MATCH]
SOFT = [Outcome.PLAIN, Outcome.PLAIN_SKIPPED, *SOFT_NOT_PLAIN]
FINAL = [Outcome.UNTAGGED, Outcome.UNREADABLE]
OWNED, ABSENT = SidecarState.OWNED, SidecarState.ABSENT
SOMEONE_ELSES = [SidecarState.EDITED, SidecarState.FOREIGN, SidecarState.NOT_A_FILE]
BLOCKED = settled(Outcome.PLAIN, reason=PlainReason.BLOCKED_FOREIGN)


@pytest.mark.parametrize(
    ("result", "tags_hash", "dump_id", "plain", "txt", "due"),
    [
        # A track with no result is always asked about.
        (None, "hash", "dump-a", "write", ABSENT, True),
        # Tags that changed, whatever the outcome.
        *[(settled(o), "other", "dump-a", "write", OWNED, True) for o in [*SOFT, *FINAL]],
        (settled(Outcome.SYNCED), "other", "dump-a", "write", ABSENT, True),
        # An old matcher, whatever the outcome.
        *[
            (
                settled(o, matcher_version=MATCHER_VERSION - 1),
                "hash",
                "dump-a",
                "write",
                OWNED,
                True,
            )
            for o in [*SOFT, *FINAL, Outcome.SYNCED]
        ],
        # Soft results follow the dump, and no id means nothing says it is the same one.
        *[(settled(o), "hash", "dump-a", "skip", OWNED, False) for o in SOFT],
        *[(settled(o), "hash", "dump-b", "skip", OWNED, True) for o in SOFT],
        *[(settled(o), "hash", "", "skip", OWNED, True) for o in SOFT],
        *[(settled(o, dump_id=""), "hash", "dump-a", "skip", OWNED, True) for o in SOFT],
        *[(settled(o, dump_id=""), "hash", "", "skip", OWNED, True) for o in SOFT],
        # The plain setting only reaches plain results.
        *[(settled(o), "hash", "dump-a", "write", ABSENT, False) for o in SOFT_NOT_PLAIN],
        (settled(Outcome.PLAIN_SKIPPED), "hash", "dump-a", "write", ABSENT, True),
        (settled(Outcome.PLAIN), "hash", "dump-a", "write", OWNED, False),
        # A .txt that has gone comes back, unless plain text is switched off.
        (settled(Outcome.PLAIN), "hash", "dump-a", "write", ABSENT, True),
        (settled(Outcome.PLAIN), "hash", "dump-a", "skip", ABSENT, False),
        # A plain result is only true of a .txt still plyrix's, so anything else is looked at again.
        *[(settled(Outcome.PLAIN), "hash", "dump-a", "write", t, True) for t in SOMEONE_ELSES],
        # A block holds while something else has the name, until the dump changes.
        *[(BLOCKED, "hash", "dump-a", "write", t, False) for t in SOMEONE_ELSES],
        *[(BLOCKED, "hash", "dump-b", "write", t, True) for t in SOMEONE_ELSES],
        (BLOCKED, "hash", "dump-a", "write", ABSENT, True),
        (BLOCKED, "hash", "dump-a", "write", OWNED, True),
        # A file that can't be looked up stays settled while it is the same, under any dump.
        *[(settled(o), "hash", "dump-b", "write", ABSENT, False) for o in FINAL],
        *[(settled(o), "hash", "", "write", ABSENT, False) for o in FINAL],
        (settled(Outcome.UNREADABLE, tags_sha256=None), None, "dump-a", "write", ABSENT, False),
        # Reached only when its sidecar has gone, which is worth a lookup.
        (settled(Outcome.SYNCED), "hash", "dump-a", "write", ABSENT, True),
        (settled(Outcome.SYNCED, tags_sha256=None), None, "dump-a", "write", ABSENT, True),
    ],
)
def test_needs_lookup(
    result: TrackResult | None,
    tags_hash: str | None,
    dump_id: str,
    plain: str,
    txt: SidecarState,
    due: bool,
) -> None:
    assert needs_lookup(result, tags_hash, dump_id, plain, txt=txt) is due


def test_needs_lookup_takes_the_matcher_version_it_is_told() -> None:
    result = settled(Outcome.NO_MATCH)
    args = (result, "hash", "dump-a", "write")

    assert needs_lookup(*args, txt=ABSENT, matcher_version=MATCHER_VERSION) is False
    assert needs_lookup(*args, txt=ABSENT, matcher_version=MATCHER_VERSION + 1) is True


def test_outcomes_are_the_ledgers_words() -> None:
    assert {outcome.value for outcome in Outcome} == {
        "synced",
        "plain",
        "plain_skipped",
        "instrumental",
        "no_match",
        "untagged",
        "unreadable",
    }


def test_miss_reasons_run_from_furthest_to_closest() -> None:
    assert [reason.value for reason in MissReason] == [
        "not_found",
        "rejected",
        "doubtful_artist",
        "doubtful_title",
        "doubtful_duration",
        "doubtful_version",
        "timing_rejected",
    ]


TAGS = TrackTags("artist", "album artist", "title", "album", 180.0)


@pytest.mark.parametrize(
    "changed",
    [
        TrackTags("other", "album artist", "title", "album", 180.0),
        TrackTags("artist", "other", "title", "album", 180.0),
        TrackTags("artist", "album artist", "other", "album", 180.0),
        TrackTags("artist", "album artist", "title", "other", 180.0),
        TrackTags("artist", "album artist", "title", "album", 200.0),
        TrackTags("artist", "album artist", "title", "album", None),
        TrackTags(None, "album artist", "title", "album", 180.0),
    ],
)
def test_tags_fingerprint_changes_with_any_field(changed: TrackTags) -> None:
    assert tags_fingerprint(changed) != tags_fingerprint(TAGS)


def test_tags_fingerprint_is_stable_and_ignores_a_fraction_of_a_second() -> None:
    copy = TrackTags("artist", "album artist", "title", "album", 180.2)
    assert tags_fingerprint(copy) == tags_fingerprint(TAGS)


def test_tags_fingerprint_does_not_let_a_value_stand_for_two_fields() -> None:
    one = TrackTags("a", "b", "c", "d", None)
    other = TrackTags("a b", None, "c", "d", None)
    assert tags_fingerprint(one) != tags_fingerprint(other)


@given(
    st.none() | st.text(),
    st.none() | st.text(),
    st.none() | st.text(),
    st.none() | st.text(),
    st.none() | st.floats(),
)
def test_tags_fingerprint_takes_anything_tags_can_hold_and_holds_no_text(
    artist: str | None,
    albumartist: str | None,
    title: str | None,
    album: str | None,
    duration: float | None,
) -> None:
    digest = tags_fingerprint(TrackTags(artist, albumartist, title, album, duration))

    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


# The tag year, kept out of the fingerprint (06-06)


@pytest.mark.parametrize(
    ("year", "expected"),
    [
        ("2026", 2026),
        ("2026-05-01", 2026),
        ("1999abc", 1999),
        ("1000", 1000),
        ("2999", 2999),
        ("999", None),
        ("0999", None),
        ("3000", None),
        ("", None),
        (None, None),
        ("abcd", None),
        ("２０２６", None),
    ],
)
def test_release_year(year: str | None, expected: int | None) -> None:
    assert release_year(year) == expected


def test_the_year_is_not_in_the_fingerprint() -> None:
    plain = TrackTags("A", None, "T", "Al", 3.0)

    assert tags_fingerprint(TrackTags("A", None, "T", "Al", 3.0, "2001")) == tags_fingerprint(plain)
    assert plain.year is None


# The targeted retry after a dump update (06-06, D-09)

BASIS = DumpBasis(chain_complete=True, dump_time=1_000_000.0, year=2026)


def soft(**fields: object) -> TrackResult:
    values: dict[str, object] = {
        "release_year": 2001,
        "artist_key": 1,
        "title_key": 2,
        "first_seen": 999_999.0,
    }
    return TrackResult(
        1,
        1,
        "h",
        Outcome.NO_MATCH,
        MissReason.NOT_FOUND,
        "a",
        MATCHER_VERSION,
        None,
        **{**values, **fields},  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("result", "full", "basis", "key_changed", "ctime", "expected"),
    [
        (soft(), True, BASIS, False, None, True),
        (soft(), False, None, False, None, True),
        (soft(), False, DumpBasis(False, 1_000_000.0, 2026), False, None, True),
        (soft(artist_key=None), False, BASIS, False, None, True),
        (soft(title_key=None), False, BASIS, False, None, True),
        (soft(), False, BASIS, True, None, True),
        (soft(release_year=2026), False, BASIS, False, None, True),
        (soft(release_year=2027), False, BASIS, False, None, True),
        (soft(first_seen=1_000_001.0), False, BASIS, False, None, True),
        (soft(first_seen=None), False, BASIS, False, 1_000_001.0, True),
        (soft(first_seen=None), False, BASIS, False, 999_999.0, False),
        (soft(first_seen=None), False, BASIS, False, None, False),
        (soft(unseen=True), False, BASIS, False, None, True),
        (soft(release_year=None), False, BASIS, False, None, False),
        (soft(), False, BASIS, False, 2_000_000.0, False),
        (soft(), False, BASIS, False, None, False),
    ],
)
def test_affected_by_update(
    result: TrackResult,
    full: bool,
    basis: DumpBasis | None,
    key_changed: bool,
    ctime: float | None,
    expected: bool,
) -> None:
    found = affected_by_update(result, full=full, basis=basis, key_changed=key_changed, ctime=ctime)

    assert found is expected


def test_a_soft_row_from_an_old_dump_waits_for_affected() -> None:
    row = soft()
    args = (row, row.tags_sha256, "b", "write")

    assert needs_lookup(*args, txt=SidecarState.ABSENT, affected=False) is False
    assert needs_lookup(*args, txt=SidecarState.ABSENT, affected=True) is True
    assert needs_lookup(*args, txt=SidecarState.ABSENT) is True


@pytest.mark.parametrize("affected", [True, False])
def test_affected_changes_nothing_for_the_same_dump_or_no_dump(affected: bool) -> None:
    row = soft()

    same = needs_lookup(row, "h", "a", "write", txt=SidecarState.ABSENT, affected=affected)
    none = needs_lookup(row, "h", "", "write", txt=SidecarState.ABSENT, affected=affected)

    assert same is False
    assert none is True


@pytest.mark.parametrize("outcome", [Outcome.SYNCED, Outcome.UNTAGGED, Outcome.UNREADABLE])
def test_affected_changes_nothing_for_a_row_that_isnt_soft(outcome: Outcome) -> None:
    row = TrackResult(1, 1, "h", outcome, None, "a", MATCHER_VERSION, None)

    def due(affected: bool) -> bool:
        return needs_lookup(row, "h", "b", "write", txt=SidecarState.ABSENT, affected=affected)

    assert due(True) is due(False)


@given(
    st.sampled_from(list(Outcome)),
    st.sampled_from(["a", "b", ""]),
    st.sampled_from(["write", "skip"]),
    st.sampled_from(list(SidecarState)),
    st.booleans(),
)
def test_not_affected_never_asks_more(
    outcome: Outcome, dump_id: str, plain: str, txt: SidecarState, same_hash: bool
) -> None:
    row = TrackResult(1, 1, "h", outcome, None, "a", MATCHER_VERSION, None)
    tags_hash = "h" if same_hash else "other"

    narrowed = needs_lookup(row, tags_hash, dump_id, plain, txt=txt, affected=False)
    wide = needs_lookup(row, tags_hash, dump_id, plain, txt=txt, affected=True)

    assert not narrowed or wide
