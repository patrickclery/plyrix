"""Candidate checks: the local name check folds as LRCLIB does, and doubt means a named miss."""

import math
import re
import unicodedata

import pytest
from hypothesis import given
from hypothesis import strategies as st

from plyrix import matching
from plyrix._lay_folds import FOLDS, TABLE_SIZE
from plyrix.domain import MATCHER_VERSION, MissReason
from plyrix.matching import (
    Candidate,
    candidate_from,
    check,
    judge,
    lookup_steps,
    prepare,
    primary_artist,
    search_form,
    stripped_title,
)

ARTIST = "Fixture Artist"
TITLE = "Fixture Title"
ALBUM = "Fixture Album"
DURATION = 100.0
SYNCED = "[00:01.00] placeholder line 1\n[00:02.00] placeholder line 2\n"
PLAIN = "placeholder line 1\n"
BEYOND_END = "[00:01.00] placeholder line 1\n[59:00.00] placeholder line 2\n"


def record(**fields: object) -> dict[str, object]:
    return {
        "id": 7,
        "trackName": TITLE,
        "artistName": ARTIST,
        "albumName": ALBUM,
        "duration": DURATION,
        "instrumental": False,
        "plainLyrics": PLAIN,
        "syncedLyrics": SYNCED,
        **fields,
    }


def judged(*records: dict[str, object], album: str | None = ALBUM):  # type: ignore[no-untyped-def]
    return judge(records, ARTIST, TITLE, album, DURATION)


def test_fold_table_is_the_pinned_one() -> None:
    assert TABLE_SIZE == 65371
    assert len(FOLDS) == 797
    assert FOLDS["ß"] == "s"
    assert FOLDS["ø"] == "o"
    assert FOLDS["ł"] == "l"
    assert FOLDS["İ"] == "i"
    for kept in ("й", "ά", "–"):
        assert kept not in FOLDS


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Björk", "bjork"),
        (unicodedata.normalize("NFD", "Björk"), "bjork"),
        ("AC/DC", "ac dc"),
        ("Don't Stop", "dont stop"),
        ("Don’t Stop", "dont stop"),
        ("R.E.M.", "r e m"),
        ("Straße", "strase"),
        ("Røyksopp", "royksopp"),
        ("Łukasz", "lukasz"),
        ("MÖTLEY CRÜE", "motley crue"),
        ("İstanbul", "istanbul"),
        ("Ёлка", "ёлка"),
        ("Song – Mix", "song – mix"),
        ("???", ""),
        ("  \t\n ", ""),
        ("", ""),
    ],
)
def test_prepare(text: str, expected: str) -> None:
    assert prepare(text) == expected


REPLACED = set("`~!@#$%^&*()_|+-=?;:\",.<>{}[]\\/\x00\n'’")


@given(st.text())
def test_prepare_always_gives_a_clean_key(text: str) -> None:
    key = prepare(text)

    assert "  " not in key
    assert key == key.strip()
    assert not re.search("[A-Z]", key)
    assert not REPLACED & set(key)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Foo feat. Bar", "Foo"),
        ("Foo Feat. Bar", "Foo"),
        ("Foo ft. Bar", "Foo"),
        ("Foo featuring Bar", "Foo"),
        ("Foo (ft. Bar)", "Foo"),
        ("Foo [feat. Bar]", "Foo"),
        ("Foo & Bar", "Foo & Bar"),
        ("Foo, Bar", "Foo, Bar"),
        ("Foo with Bar", "Foo with Bar"),
        ("Softcell", "Softcell"),
        ("Feather", "Feather"),
    ],
)
def test_primary_artist(name: str, expected: str) -> None:
    assert primary_artist(name) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Song (Live)", "Song"),
        ("Song (Remastered 2011)", "Song"),
        ("Song - Remastered 2011", "Song"),
        ("Song - Radio Edit", "Song"),
        ("Song [Live]", "Song"),
        ("Song (feat. Other)", "Song"),
        ("Song (2011)", "Song (2011)"),
        ("Song - 2011", "Song - 2011"),
        ("Song (Live) (Remastered)", "Song"),
        ("Song (ft. Other)", "Song"),
        ("Song (Featuring Other)", "Song"),
        ("Song (prod. Other)", "Song"),
        ("Song (Produced by Other)", "Song"),
        ("Song (with Other)", "Song (with Other)"),
        ("Song (From Afar)", "Song (From Afar)"),
        ("Song - From Afar", "Song - From Afar"),
        ("Stay (Feathers)", "Stay (Feathers)"),
        ("Stay (Prodigal Son)", "Stay (Prodigal Son)"),
        ("Stay (Production)", "Stay (Production)"),
        ("(Don't Fear) The Reaper", "(Don't Fear) The Reaper"),
        ("Live and Let Die", "Live and Let Die"),
        ("Demo Girl", "Demo Girl"),
        ("Song (Part 2)", "Song (Part 2)"),
        ("One - Two", "One - Two"),
        ("Song (Live", "Song (Live"),
    ],
)
def test_stripped_title(title: str, expected: str) -> None:
    assert stripped_title(title) == expected


def candidate(**fields: object) -> Candidate:
    return candidate_from(record(**fields))


@pytest.mark.parametrize(
    ("fields", "track", "expected"),
    [
        ({"trackName": "Song (Remastered 2011)"}, {"title": "Song"}, None),
        ({"trackName": "Song"}, {"title": "Song (Remastered 2011)"}, None),
        ({"trackName": "Song (Album Version)"}, {"title": "Song - Single"}, None),
        ({"trackName": "Song (Live)"}, {"title": "Song"}, MissReason.DOUBTFUL_VERSION),
        ({"trackName": "Song"}, {"title": "Song (Live)"}, MissReason.DOUBTFUL_VERSION),
        ({"trackName": "Song [Live]"}, {"title": "Song (Live)"}, None),
        ({"trackName": "Song - Live"}, {"title": "Song (Live)"}, None),
        ({"trackName": "Song - Radio Edit"}, {"title": "Song"}, MissReason.DOUBTFUL_VERSION),
        ({"trackName": "Song (Remix)"}, {"title": "Song (Live)"}, MissReason.DOUBTFUL_VERSION),
        ({"trackName": "Song (Ao Vivo)"}, {"title": "Song"}, MissReason.DOUBTFUL_VERSION),
        (
            {"trackName": "Song (Live in One)"},
            {"title": "Song (Live)"},
            MissReason.DOUBTFUL_VERSION,
        ),
        ({"trackName": "Song"}, {"title": "Song (feat. Other)"}, None),
        ({"trackName": "Song (with Other)"}, {"title": "Song"}, MissReason.DOUBTFUL_TITLE),
        ({"trackName": "Song (with Other)"}, {"title": "Song (with Other)"}, None),
        ({"trackName": "Home (With You)"}, {"title": "Home"}, MissReason.DOUBTFUL_TITLE),
        (
            {"trackName": "Home (From The Inside)"},
            {"title": "Home (With You)"},
            MissReason.DOUBTFUL_TITLE,
        ),
        ({"trackName": "Stay (Feathers)"}, {"title": "Stay"}, MissReason.DOUBTFUL_TITLE),
        ({"trackName": "Stay (Prodigal Son)"}, {"title": "Stay"}, MissReason.DOUBTFUL_TITLE),
        ({"trackName": "Song - From Afar"}, {"title": "Song"}, MissReason.DOUBTFUL_TITLE),
        ({"trackName": "Song (2011)"}, {"title": "Song"}, MissReason.DOUBTFUL_TITLE),
        ({"trackName": "Song (2012)"}, {"title": "Song (1984)"}, MissReason.DOUBTFUL_TITLE),
        ({"trackName": "Song (2011)"}, {"title": "Song (2011)"}, None),
        ({"trackName": "Song (Remastered 2011)"}, {"title": "Song (Remastered 1999)"}, None),
        ({"trackName": "Song (Clean)"}, {"title": "Song"}, MissReason.DOUBTFUL_VERSION),
        ({"trackName": "Song (Clean)"}, {"title": "Song (Explicit)"}, MissReason.DOUBTFUL_VERSION),
        ({"trackName": "Song (Clean)"}, {"title": "Song (Clean)"}, None),
        ({"trackName": "Song (Explicit)"}, {"title": "Song"}, None),
        ({"artistName": "Other"}, {}, MissReason.DOUBTFUL_ARTIST),
        ({"artistName": "fixture artist feat. Other"}, {}, None),
        ({"artistName": "FIXTURE  ARTIST"}, {}, None),
        ({"artistName": "Fixture Artist & Other"}, {}, MissReason.DOUBTFUL_ARTIST),
        ({"trackName": "Other"}, {}, MissReason.DOUBTFUL_TITLE),
        ({"trackName": "(Other) Fixture Title"}, {}, MissReason.DOUBTFUL_TITLE),
        ({"duration": 102.0}, {}, None),
        ({"duration": 98.0}, {}, None),
        ({"duration": 102.01}, {}, MissReason.DOUBTFUL_DURATION),
        ({"duration": 97.99}, {}, MissReason.DOUBTFUL_DURATION),
        ({"albumName": "Fixture Album (Live)"}, {}, MissReason.DOUBTFUL_VERSION),
        ({"albumName": "Live at the Fixture"}, {}, MissReason.DOUBTFUL_VERSION),
        ({"albumName": "Fixture MTV Unplugged"}, {}, MissReason.DOUBTFUL_VERSION),
        ({"albumName": "Fixture Ao Vivo"}, {}, MissReason.DOUBTFUL_VERSION),
        ({"albumName": "Fixture Live"}, {"album": "Fixture Live"}, None),
        ({"albumName": "Alive"}, {}, None),
        ({"albumName": "Other Album"}, {}, None),
        ({"albumName": "Fixture Album (Live)"}, {"album": None}, None),
        ({"albumName": None}, {"album": "Fixture Live"}, None),
        ({"trackName": "???"}, {"title": "???"}, MissReason.DOUBTFUL_TITLE),
        ({"artistName": "???"}, {"artist": "???"}, MissReason.DOUBTFUL_ARTIST),
    ],
)
def test_check(
    fields: dict[str, object], track: dict[str, object], expected: MissReason | None
) -> None:
    wanted = {"artist": ARTIST, "title": TITLE, "album": ALBUM, "duration": DURATION, **track}

    assert check(candidate(**fields), **wanted) is expected  # type: ignore[arg-type]


def test_the_stricter_titles_look_settled_misses_up_again() -> None:
    assert MATCHER_VERSION >= 2


def test_the_first_failure_names_the_reason() -> None:
    wrong = candidate(artistName="Other", trackName="Another", duration=1.0)
    assert check(wrong, ARTIST, TITLE, ALBUM, DURATION) is MissReason.DOUBTFUL_ARTIST

    wrong = candidate(trackName="Another", duration=1.0)
    assert check(wrong, ARTIST, TITLE, ALBUM, DURATION) is MissReason.DOUBTFUL_TITLE

    wrong = candidate(trackName=f"{TITLE} (Live)", duration=1.0)
    assert check(wrong, ARTIST, TITLE, ALBUM, DURATION) is MissReason.DOUBTFUL_DURATION


@pytest.mark.parametrize(
    "duration",
    [None, "100", True, False, math.nan, math.inf, -math.inf, 10**400, [100.0], {}],
    ids=repr,
)
def test_a_hostile_duration_is_doubtful(duration: object) -> None:
    assert candidate(duration=duration).duration is None
    assert check(candidate(duration=duration), ARTIST, TITLE, ALBUM, DURATION) is (
        MissReason.DOUBTFUL_DURATION
    )


def test_an_integer_duration_counts() -> None:
    assert check(candidate(duration=100), ARTIST, TITLE, ALBUM, DURATION) is None


@pytest.mark.parametrize("name", [None, 7, True, [ARTIST], {"name": ARTIST}, "", "  "])
def test_a_hostile_name_is_doubtful(name: object) -> None:
    artist = candidate(artistName=name)
    title = candidate(trackName=name)

    assert check(artist, ARTIST, TITLE, ALBUM, DURATION) is MissReason.DOUBTFUL_ARTIST
    assert check(title, ARTIST, TITLE, ALBUM, DURATION) is MissReason.DOUBTFUL_TITLE


def test_a_record_with_no_fields_is_a_miss_and_nothing_raises() -> None:
    empty = candidate_from({})

    assert empty == Candidate(None, None, None, None, None, None, None, False)
    assert check(empty, ARTIST, TITLE, ALBUM, DURATION) is not None
    assert judged({}).miss is MissReason.DOUBTFUL_TITLE


@pytest.mark.parametrize(
    ("fields", "attribute"),
    [
        ({"syncedLyrics": 7}, "synced"),
        ({"syncedLyrics": [SYNCED]}, "synced"),
        ({"syncedLyrics": "  \n "}, "synced"),
        ({"plainLyrics": {"text": PLAIN}}, "plain"),
        ({"plainLyrics": ""}, "plain"),
        ({"albumName": 7}, "album"),
        ({"id": "7"}, "id"),
        ({"id": True}, "id"),
        ({"id": 2**63}, "id"),
    ],
)
def test_candidate_from_drops_a_field_of_the_wrong_shape(
    fields: dict[str, object], attribute: str
) -> None:
    assert getattr(candidate(**fields), attribute) is None


@pytest.mark.parametrize("value", [1, "true", [True], None])
def test_only_true_is_instrumental(value: object) -> None:
    assert candidate(instrumental=value).instrumental is False


def test_a_valid_synced_candidate_is_chosen() -> None:
    verdict = judged(record())

    assert verdict.synced is not None
    assert verdict.synced.id == 7


def test_the_same_album_wins_over_a_closer_duration() -> None:
    verdict = judged(
        record(id=1, albumName="Other Album", duration=DURATION),
        record(id=2, albumName=ALBUM, duration=DURATION + 1),
    )

    assert verdict.synced is not None
    assert verdict.synced.id == 2


def test_the_smallest_duration_gap_wins_next() -> None:
    verdict = judged(record(id=1, duration=DURATION + 2), record(id=2, duration=DURATION + 1))

    assert verdict.synced is not None
    assert verdict.synced.id == 2


def test_the_lowest_id_wins_last() -> None:
    verdict = judged(record(id=9), record(id=3), record(id=None))

    assert verdict.synced is not None
    assert verdict.synced.id == 3


def test_a_valid_synced_candidate_beats_a_better_ranked_one_that_fails_timing() -> None:
    verdict = judged(
        record(id=1, syncedLyrics=BEYOND_END),
        record(id=2, albumName="Other", duration=DURATION + 1),
    )

    assert verdict.synced is not None
    assert verdict.synced.id == 2


def test_failing_timing_with_plain_text_gives_plain_and_no_synced() -> None:
    verdict = judged(record(syncedLyrics=BEYOND_END))

    assert verdict.synced is None
    assert [item.id for item in verdict.plain] == [7]
    assert verdict.instrumental is None


def test_failing_timing_with_no_plain_text_names_timing() -> None:
    verdict = judged(record(syncedLyrics=BEYOND_END, plainLyrics=None))

    assert (verdict.synced, verdict.plain, verdict.instrumental) == (None, (), None)
    assert verdict.miss is MissReason.TIMING_REJECTED


def test_plain_candidates_are_ranked() -> None:
    verdict = judged(
        record(id=1, syncedLyrics=None, albumName="Other"),
        record(id=2, syncedLyrics=None, duration=DURATION + 1),
        record(id=3, syncedLyrics=None),
    )

    assert [item.id for item in verdict.plain] == [3, 2, 1]


def test_instrumental_with_no_lyrics_counts() -> None:
    verdict = judged(record(instrumental=True, syncedLyrics=None, plainLyrics=None))

    assert verdict.instrumental is not None
    assert (verdict.synced, verdict.plain) == (None, ())


@pytest.mark.parametrize(
    "other",
    [
        {"id": 2, "syncedLyrics": None},
        {"id": 2, "plainLyrics": None},
        {"id": 2, "syncedLyrics": BEYOND_END, "plainLyrics": None},
    ],
    ids=["plain", "synced", "synced_failing_timing"],
)
def test_instrumental_counts_only_when_no_accepted_candidate_has_lyrics(
    other: dict[str, object],
) -> None:
    verdict = judged(
        record(id=1, instrumental=True, syncedLyrics=None, plainLyrics=None), record(**other)
    )

    assert verdict.instrumental is None


def test_a_doubtful_candidates_lyrics_dont_stop_an_instrumental_answer() -> None:
    verdict = judged(
        record(id=1, instrumental=True, syncedLyrics=None, plainLyrics=None),
        record(id=2, artistName="Other"),
    )

    assert verdict.instrumental is not None
    assert verdict.instrumental.id == 1


def test_no_candidates_is_not_found() -> None:
    assert judge([], ARTIST, TITLE, ALBUM, DURATION).miss is MissReason.NOT_FOUND


def test_an_accepted_candidate_with_no_text_is_not_found() -> None:
    assert judged(record(syncedLyrics=None, plainLyrics=None)).miss is MissReason.NOT_FOUND


def test_the_miss_is_the_closest_reason_seen() -> None:
    verdict = judged(
        record(artistName="Other"),
        record(trackName=f"{TITLE} (Live)"),
        record(duration=1.0),
        record(trackName="Other"),
    )

    assert verdict.synced is None
    assert verdict.miss is MissReason.DOUBTFUL_VERSION


def test_a_timing_failure_is_the_closest_reason() -> None:
    verdict = judged(
        record(trackName=f"{TITLE} (Live)"), record(syncedLyrics=BEYOND_END, plainLyrics=None)
    )

    assert verdict.miss is MissReason.TIMING_REJECTED


def test_a_miss_names_the_row_that_gave_its_reason() -> None:
    verdict = judged(
        record(id=1, artistName="Other"),
        record(id=2, trackName=f"{TITLE} (Live)"),
        record(id=3, trackName=f"{TITLE} (Live)"),
        record(id=4, duration=1.0),
    )

    assert verdict.miss is MissReason.DOUBTFUL_VERSION
    assert verdict.miss_id == 2


def test_a_timing_miss_names_the_row_whose_timing_failed() -> None:
    verdict = judged(
        record(id=1, trackName=f"{TITLE} (Live)"),
        record(id=2, syncedLyrics=BEYOND_END, plainLyrics=None),
    )

    assert verdict.miss_id == 2


def test_an_accepted_row_with_no_text_is_the_one_a_miss_names() -> None:
    assert judged(record(id=5, syncedLyrics=None, plainLyrics=None)).miss_id == 5


def test_a_miss_with_no_rows_names_none() -> None:
    assert judged().miss_id is None


@pytest.mark.parametrize(
    ("artist", "title", "album", "expected"),
    [
        ("A", "T", "Al", [("A", "T", "Al"), ("A", "T", None)]),
        ("A", "T", None, [("A", "T", None)]),
        ("A", "T", "", [("A", "T", None)]),
        (
            "A feat. B",
            "T",
            "Al",
            [("A feat. B", "T", "Al"), ("A feat. B", "T", None), ("A", "T", None)],
        ),
        (
            "A",
            "T (Remastered 2011)",
            "Al",
            [
                ("A", "T (Remastered 2011)", "Al"),
                ("A", "T (Remastered 2011)", None),
                ("A", "T", None),
            ],
        ),
        ("A feat. B", "T (Live)", None, [("A feat. B", "T (Live)", None), ("A", "T", None)]),
        # Nothing would be left to send, so the tag's own text stays and nothing repeats.
        ("(feat. B)", "T", None, [("(feat. B)", "T", None)]),
        ("A", "(Live)", None, [("A", "(Live)", None)]),
    ],
)
def test_lookup_steps(
    artist: str, title: str, album: str | None, expected: list[tuple[str, str, str | None]]
) -> None:
    assert lookup_steps(artist, title, album) == expected


@pytest.mark.parametrize(
    ("artist", "title", "expected"),
    [
        ("A", "T", ("A", "T")),
        ("A ft. B", "T - Remastered 2011", ("A", "T")),
        ("(feat. B)", "(Live)", ("(feat. B)", "(Live)")),
    ],
)
def test_search_form(artist: str, title: str, expected: tuple[str, str]) -> None:
    assert search_form(artist, title) == expected


names = st.one_of(
    st.none(), st.integers(), st.floats(), st.booleans(), st.text(), st.lists(st.text())
)


@given(
    st.fixed_dictionaries(
        {},
        optional={
            key: names
            for key in (
                "id",
                "trackName",
                "artistName",
                "albumName",
                "duration",
                "instrumental",
                "plainLyrics",
                "syncedLyrics",
            )
        },
    ),
    st.text(),
    st.text(),
    st.one_of(st.none(), st.text()),
)
def test_judge_never_raises(
    answer: dict[str, object], artist: str, title: str, album: str | None
) -> None:
    verdict = judge([answer], artist, title, album, DURATION)

    assert isinstance(verdict.miss, MissReason)


# The 8-byte keys both sides of an LRCLIB diff share (06-05)


def test_a_featured_artist_keys_as_the_primary_one() -> None:
    assert (
        matching.lookup_keys("Artist feat. Guest", "Song")[0]
        == matching.lookup_keys("Artist", "Song")[0]
    )


def test_a_title_qualifier_keys_as_the_bare_title() -> None:
    assert (
        matching.lookup_keys("Artist", "Song (Live)")[1]
        == matching.lookup_keys("Artist", "Song")[1]
    )


def test_case_and_folds_give_equal_keys() -> None:
    assert matching.lookup_keys("ARTIST", "Song!") == matching.lookup_keys("artist", "song")
    assert matching.lookup_keys("Café", "x")[0] == matching.lookup_keys("cafe", "x")[0]


@pytest.mark.parametrize("empty", [None, "", "!!!"])
def test_a_field_that_prepares_to_nothing_has_no_key(empty: str | None) -> None:
    artist, title = matching.lookup_keys(empty, empty)

    assert artist is None and title is None


@given(st.text(), st.text())
def test_lookup_keys_never_raise_and_fit_in_64_bits(artist: str, title: str) -> None:
    for key in matching.lookup_keys(artist, title):
        assert key is None or -(2**63) <= key < 2**63
