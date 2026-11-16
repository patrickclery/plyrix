"""Timing checks: only structural failures reject a .lrc, and coverage findings only report."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from plyrix.lrc import quality, validate
from plyrix.sidecars import encode

DURATION = 180.0
# A control character hides a line from a check of the raw text, and writing the file removes it.
HIDDEN_DECREASING = "[00:10.00] a\n\x01[00:05.00] b\n"
HIDDEN_BEYOND_THE_END = "[00:10.00] a\n\x01[99:00.00] b\n"
HIDDEN_BAD_STAMP = "[00:10.00] a\n\x01[00:] b\n"
ONLY_HIDDEN_LINES = "\x01[00:01.00] a\n\x01[00:02.00] b\n"
TIMED_TEXT = st.lists(
    st.one_of(
        st.text(alphabet="[]:.0123 a﻿\r\t\x00\x01\x08\x0b\x1b\x1f", max_size=12),
        st.tuples(
            st.sampled_from(["", "\x01", "﻿", " "]),
            st.integers(min_value=0, max_value=99),
            st.integers(min_value=0, max_value=59),
        ).map(lambda line: f"{line[0]}[{line[1]:02}:{line[2]:02}.00] a"),
    ),
    max_size=6,
).map("\n".join)


@pytest.mark.parametrize(
    ("text", "why"),
    [
        (HIDDEN_DECREASING, "decreasing"),
        (HIDDEN_BEYOND_THE_END, "beyond_end"),
        (HIDDEN_BAD_STAMP, "unparseable"),
    ],
    ids=["decreasing", "beyond_end", "unparseable"],
)
def test_a_line_only_writing_reveals_is_judged(text: str, why: str) -> None:
    assert validate(text, DURATION) == why


def test_lines_only_writing_reveals_count_as_timed() -> None:
    assert validate(ONLY_HIDDEN_LINES, DURATION) is None
    assert "few_lines" in quality(ONLY_HIDDEN_LINES, DURATION)
    assert "low_coverage" in quality(ONLY_HIDDEN_LINES, DURATION)


@given(TIMED_TEXT)
def test_a_text_is_judged_the_same_as_the_bytes_written_for_it(text: str) -> None:
    written = encode(text).decode("utf-8")

    assert validate(text, DURATION) == validate(written, DURATION)
    assert quality(text, DURATION) == quality(written, DURATION)


@pytest.mark.parametrize(
    "text",
    [
        "[0:01] placeholder line 1\n[0:02] placeholder line 2",
        "[00:01] placeholder line 1\n[00:02] placeholder line 2",
        "[00:01.5] placeholder line 1\n[00:02.5] placeholder line 2",
        "[00:01.50] placeholder line 1\n[00:02.50] placeholder line 2",
        "[00:01.500] placeholder line 1\n[00:02.500] placeholder line 2",
        "[00:01:50] placeholder line 1\n[00:02:50] placeholder line 2",
        "[00:01.00] placeholder line 1\r\n[00:02.00] placeholder line 2\r\n",
        "[00:01.00] placeholder line 1\r[00:02.00] placeholder line 2\r",
        "  [00:01.00] placeholder line 1\n\t[00:02.00] placeholder line 2",
        "[ar:Fixture Artist]\n[00:01.00] placeholder line 1\nan untimed line\n[00:02.00] line 2",
        "﻿[00:01.00] placeholder line 1\n[00:02.00] placeholder line 2",
    ],
)
def test_valid_timed_text_passes(text: str) -> None:
    assert validate(text, DURATION) is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n  ",
        "placeholder line 1\nplaceholder line 2\n",
        "[ar:Fixture Artist]\n[ti:Fixture Title]\n",
        "[00:01.00]\n[00:02.00]  \n",
        "x [00:01.00] placeholder line 1",
        "[٠٠:٠١.٠٠] placeholder line 1\n[٠٠:٠٢.٠٠] placeholder line 2",
    ],
    ids=["empty", "blank", "untimed", "id_tags", "no_text", "mid_line", "arabic_indic"],
)
def test_no_timed_line(text: str) -> None:
    assert validate(text, DURATION) == "no_timed_line"


def test_a_timed_line_with_no_text_is_fine_when_another_has_some() -> None:
    assert validate("[00:01.00]\n[00:02.00] placeholder line\n", DURATION) is None


@pytest.mark.parametrize(
    "line",
    [
        "[1] placeholder",
        "[12:xx] placeholder",
        "[00:001] placeholder",
        "[00:01.0000] placeholder",
        "[00:01:02:03] placeholder",
        "[00:01",
        "[" + "9" * 5000 + ":00] placeholder",
        "[" + "9" * 400 + ":00] placeholder",
    ],
    ids=[
        "number",
        "letters",
        "three_second_digits",
        "four_fraction_digits",
        "too_many_parts",
        "unclosed",
        "digits_past_int_limit",
        "digits_past_float_limit",
    ],
)
def test_unparseable(line: str) -> None:
    assert validate(f"[00:01.00] placeholder line\n{line}\n", DURATION) == "unparseable"


def test_decreasing() -> None:
    text = "[00:02.00] placeholder line 1\n[00:01.00] placeholder line 2\n"
    assert validate(text, DURATION) == "decreasing"


def test_a_line_out_of_order_inside_itself_is_not_decreasing() -> None:
    text = "[00:30.00][00:10.00] placeholder line 1\n[00:40.00] placeholder line 2\n"
    assert validate(text, DURATION) is None


def test_lines_with_equal_first_stamps_are_not_decreasing() -> None:
    text = "[00:01.00] placeholder 1\n[00:01.00] placeholder 2\n[00:02.00] placeholder 3\n"
    assert validate(text, DURATION) is None


@pytest.mark.parametrize(
    "text",
    [
        "[00:01.00] placeholder line 1\n[00:01.00] placeholder line 2\n",
        "[00:01.00][00:01.00] placeholder line\n",
        "[00:01.5] placeholder line 1\n[00:01.50] placeholder line 2\n",
    ],
)
def test_identical(text: str) -> None:
    assert validate(text, DURATION) == "identical"


@pytest.mark.parametrize("line", ["[00:01.00] placeholder", "[01:00.00] placeholder"])
def test_a_single_stamp_is_allowed(line: str) -> None:
    assert validate(line, DURATION) is None


@pytest.mark.parametrize(
    ("stamp", "duration", "expected"),
    [
        ("[03:02.00]", 180.0, None),
        ("[03:02.01]", 180.0, "beyond_end"),
        ("[03:02.001]", 180.0, "beyond_end"),
        ("[03:03]", 180.0, "beyond_end"),
    ],
)
def test_beyond_end_uses_the_largest_stamp(stamp: str, duration: float, expected: str) -> None:
    text = f"[00:01.00] placeholder line 1\n{stamp} placeholder line 2\n"
    assert validate(text, duration) == expected


def test_beyond_end_sees_a_later_stamp_on_a_multi_stamp_line() -> None:
    text = "[00:01.00][10:00.00] placeholder line 1\n[00:02.00] placeholder line 2\n"
    assert validate(text, DURATION) == "beyond_end"


def test_the_tolerance_can_be_set() -> None:
    text = "[00:01.00] placeholder line 1\n[03:05.00] placeholder line 2\n"
    assert validate(text, DURATION, tolerance=5.0) is None
    assert validate(text, DURATION, tolerance=4.9) == "beyond_end"


def test_a_fraction_is_its_digits_over_a_power_of_ten() -> None:
    # .5 is half a second, so this ends at 182.5 and not at 182.05.
    text = "[00:01.00] placeholder line 1\n[03:02.5] placeholder line 2\n"
    assert validate(text, DURATION) == "beyond_end"
    assert validate("[00:01.5] placeholder\n[00:01.05] placeholder\n", DURATION) == "decreasing"


def _lines(count: int, last: float) -> str:
    step = last / count
    stamps = [step * (index + 1) for index in range(count)]
    return "\n".join(f"[{int(s // 60):02d}:{s % 60:05.2f}] placeholder" for s in stamps)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_lines(4, 100.0), frozenset()),
        (_lines(3, 100.0), frozenset({"few_lines"})),
        (_lines(1, 100.0), frozenset({"few_lines"})),
        (_lines(4, 71.0), frozenset({"low_coverage"})),
        (_lines(4, 72.0), frozenset()),
        (_lines(2, 10.0), frozenset({"few_lines", "low_coverage"})),
        ("placeholder line", frozenset({"few_lines"})),
        ("", frozenset({"few_lines"})),
    ],
)
def test_quality(text: str, expected: frozenset[str]) -> None:
    assert quality(text, DURATION) == expected


@pytest.mark.parametrize(
    "text",
    [_lines(1, 100.0), _lines(2, 100.0), _lines(4, 10.0), _lines(2, 10.0)],
)
def test_findings_do_not_change_the_answer(text: str) -> None:
    assert quality(text, DURATION)
    assert validate(text, DURATION) is None


@given(
    st.one_of(st.text(), st.text(alphabet="[]0123456789:.\n\r ")),
    st.floats(min_value=0.0, max_value=1e6),
)
def test_validate_and_quality_never_raise(text: str, duration: float) -> None:
    assert validate(text, duration) in {
        None,
        "no_timed_line",
        "unparseable",
        "decreasing",
        "identical",
        "beyond_end",
    }
    assert quality(text, duration) <= {"few_lines", "low_coverage"}


@given(st.lists(st.integers(min_value=0, max_value=17_999), min_size=2, max_size=40, unique=True))
def test_increasing_stamps_within_the_duration_pass(centiseconds: list[int]) -> None:
    ordered = sorted(centiseconds)
    text = "\n".join(
        f"[{c // 6000:02d}:{c // 100 % 60:02d}.{c % 100:02d}] placeholder" for c in ordered
    )
    assert validate(text, ordered[-1] / 100) is None
