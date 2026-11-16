"""One row per line of the lookup half of the decision table."""

import itertools

import pytest

from plyrix.domain import (
    Found,
    MissReason,
    Outcome,
    SidecarState,
    Step,
    SyncedReason,
    TrackResult,
    decide_existing,
    decide_found,
    stale_lookup_due,
)

OTHER_THAN_OWNED = [state for state in SidecarState if state is not SidecarState.OWNED]
EDITED_OR_FOREIGN = [SidecarState.EDITED, SidecarState.FOREIGN]
NEITHER_MATCHING_NOR_OURS = [SidecarState.EDITED, SidecarState.FOREIGN, SidecarState.NOT_A_FILE]

ROWS = [
    (Found.SYNCED, SidecarState.OWNED, "write", False, Step.PUBLISH_LRC_REMOVE_TXT, "synced"),
    (Found.SYNCED, SidecarState.OWNED, "skip", False, Step.PUBLISH_LRC_REMOVE_TXT, "synced"),
    *[(Found.SYNCED, txt, "write", False, Step.PUBLISH_LRC, "synced") for txt in OTHER_THAN_OWNED],
    *[(Found.SYNCED, txt, "skip", False, Step.PUBLISH_LRC, "synced") for txt in OTHER_THAN_OWNED],
    *[(Found.SYNCED, txt, "write", True, Step.PUBLISH_LRC, "synced") for txt in EDITED_OR_FOREIGN],
    *[(Found.PLAIN, txt, "skip", False, Step.NOTHING, "plain_skipped") for txt in SidecarState],
    *[(Found.PLAIN, txt, "skip", True, Step.NOTHING, "plain_skipped") for txt in SidecarState],
    (Found.PLAIN, SidecarState.ABSENT, "write", False, Step.PUBLISH_TXT, "plain"),
    (Found.PLAIN, SidecarState.OWNED, "write", False, Step.KEEP_TXT, "plain"),
    (Found.PLAIN, SidecarState.OWNED, "write", True, Step.KEEP_TXT, "plain"),
    *[(Found.PLAIN, txt, "write", True, Step.ADOPT_TXT, "plain") for txt in EDITED_OR_FOREIGN],
    *[
        (Found.PLAIN, txt, "write", False, Step.BLOCK_TXT, "blocked_foreign")
        for txt in NEITHER_MATCHING_NOR_OURS
    ],
    (Found.PLAIN, SidecarState.NOT_A_FILE, "write", True, Step.BLOCK_TXT, "blocked_foreign"),
    *[
        (Found.INSTRUMENTAL, txt, "write", False, Step.NOTHING, "instrumental")
        for txt in SidecarState
    ],
    *[
        (Found.INSTRUMENTAL, txt, "skip", False, Step.NOTHING, "instrumental")
        for txt in SidecarState
    ],
    *[(Found.NOTHING, txt, "write", False, Step.NOTHING, "no_match") for txt in SidecarState],
    *[(Found.NOTHING, txt, "skip", False, Step.NOTHING, "no_match") for txt in SidecarState],
]


@pytest.mark.parametrize(("found", "txt", "plain", "matches", "step", "coverage"), ROWS)
def test_decide_found(
    found: Found, txt: SidecarState, plain: str, matches: bool, step: Step, coverage: str
) -> None:
    assert decide_found(found, txt, plain, matches) == (step, coverage)


@pytest.mark.parametrize(
    ("found", "txt", "plain", "matches"),
    list(itertools.product(Found, SidecarState, ("write", "skip"), (True, False))),
)
def test_decide_found_has_an_answer_for_every_combination(
    found: Found, txt: SidecarState, plain: str, matches: bool
) -> None:
    step, coverage = decide_found(found, txt, plain, matches)

    assert isinstance(step, Step)
    assert coverage in {"synced", "plain", "plain_skipped", "blocked_foreign"} | {
        "instrumental",
        "no_match",
    }


def test_only_a_proved_txt_is_ever_removed_or_adopted() -> None:
    for found, txt, plain, matches in itertools.product(
        Found, SidecarState, ("write", "skip"), (True, False)
    ):
        step, _ = decide_found(found, txt, plain, matches)
        if step is Step.PUBLISH_LRC_REMOVE_TXT:
            assert txt is SidecarState.OWNED
        if step is Step.ADOPT_TXT:
            assert matches and txt in EDITED_OR_FOREIGN
        if step is Step.PUBLISH_TXT:
            assert txt is SidecarState.ABSENT


EXISTING_ROWS = [
    (SidecarState.ABSENT, False, False, Step.NOTHING),
    (SidecarState.ABSENT, True, True, Step.NOTHING),
    (SidecarState.OWNED, False, False, Step.KEEP_LRC),
    (SidecarState.OWNED, True, False, Step.KEEP_LRC),
    (SidecarState.OWNED, False, True, Step.REVALIDATE),
    (SidecarState.OWNED, True, True, Step.REVALIDATE),
    (SidecarState.EDITED, True, False, Step.MARK_TIMED),
    (SidecarState.EDITED, True, True, Step.MARK_TIMED),
    (SidecarState.EDITED, False, False, Step.BLOCK_LRC),
    (SidecarState.EDITED, False, True, Step.BLOCK_LRC),
    (SidecarState.FOREIGN, True, False, Step.ADOPT_LRC),
    (SidecarState.FOREIGN, True, True, Step.ADOPT_LRC),
    (SidecarState.FOREIGN, False, False, Step.BLOCK_LRC),
    (SidecarState.FOREIGN, False, True, Step.BLOCK_LRC),
    (SidecarState.NOT_A_FILE, False, False, Step.BLOCK_LRC),
    (SidecarState.NOT_A_FILE, True, True, Step.BLOCK_LRC),
]


@pytest.mark.parametrize(("lrc", "valid", "recheck", "step"), EXISTING_ROWS)
def test_decide_existing(lrc: SidecarState, valid: bool, recheck: bool, step: Step) -> None:
    assert decide_existing(lrc, valid, recheck) is step


def test_decide_existing_covers_every_state() -> None:
    assert {row[0] for row in EXISTING_ROWS} == set(SidecarState)


def stored(reason: MissReason | SyncedReason | None, dump_id: str) -> TrackResult:
    return TrackResult(1, 2, None, Outcome.SYNCED, reason, dump_id, 1, None)


STALE = SyncedReason.LRC_STALE


@pytest.mark.parametrize(
    ("result", "origin", "dump_id", "due"),
    [
        (stored(STALE, "dump-a"), "written", "dump-b", True),
        (stored(STALE, "dump-a"), "proved", "dump-b", False),
        (stored(STALE, "dump-a"), "written", "", True),
        (stored(STALE, ""), "written", "", True),
        (stored(STALE, ""), "written", "dump-a", True),
        (stored(STALE, "dump-a"), "written", "dump-a", False),
        (stored(STALE, "dump-a"), "proved", "dump-a", False),
        (stored(STALE, "dump-a"), "timed", "dump-b", False),
        (stored(STALE, "dump-a"), "timed", "", False),
        (stored(STALE, "dump-a"), "timed", "dump-a", False),
        (stored(None, "dump-a"), "written", "dump-b", False),
        (stored(None, "dump-a"), "written", "", False),
        (stored(MissReason.NOT_FOUND, "dump-a"), "written", "dump-b", False),
        (None, "written", "dump-b", False),
    ],
)
def test_stale_lookup_due(result: TrackResult | None, origin: str, dump_id: str, due: bool) -> None:
    assert stale_lookup_due(result, origin, dump_id) is due


def test_a_stale_lrc_from_an_old_dump_waits_for_affected() -> None:
    row = stored(STALE, "dump-a")

    assert stale_lookup_due(row, "written", "dump-b", affected=False) is False
    assert stale_lookup_due(row, "written", "dump-b", affected=True) is True
    # With no dump id nothing says the dump is the same, whatever the diff says.
    assert stale_lookup_due(row, "written", "", affected=False) is True
