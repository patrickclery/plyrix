"""Timing checks for a timed lyric text. Nothing here touches the filesystem or the network."""

import re

# [0-9] and not \d, which also matches other scripts' digits. The bound keeps a hostile stamp from
# reaching a number int() or float() refuses, so it's only unparseable.
_ONE_STAMP = r"\[[0-9]{1,9}:[0-9]{1,2}(?:[.:][0-9]{1,3})?\]"
_STAMP = re.compile(r"\[([0-9]+):([0-9]+)(?:[.:]([0-9]+))?\]")
_TIMED_LINE = re.compile(rf"^\s*((?:{_ONE_STAMP})+)(.*)$")
_STARTS_LIKE_A_STAMP = re.compile(r"^\s*\[[0-9]")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f]")

_FEW_STAMPS = 4
_LOW_COVERAGE = 0.4


def validate(text: str, duration: float, tolerance: float = 2.0) -> str | None:
    """Return why a timed text can't fit this track, or None when it can.

    Each line is judged by its first stamp, as a line repeated later in a song lists its stamps in
    any order. One stamp alone is allowed, and that's a report-only finding of quality.
    """
    firsts: list[float] = []
    stamps: list[float] = []
    has_text = False
    for line in _lines(text):
        match = _TIMED_LINE.match(line)
        if match is None:
            if _STARTS_LIKE_A_STAMP.match(line):
                return "unparseable"
            continue
        values = _seconds(match.group(1))
        firsts.append(values[0])
        stamps.extend(values)
        has_text = has_text or bool(match.group(2).strip())
    if not stamps or not has_text:
        return "no_timed_line"
    if any(later < earlier for earlier, later in zip(firsts, firsts[1:], strict=False)):
        return "decreasing"
    if len(stamps) > 1 and len(set(stamps)) == 1:
        return "identical"
    if max(stamps) > duration + tolerance:
        return "beyond_end"
    return None


def quality(text: str, duration: float) -> frozenset[str]:
    """Return findings worth reporting about a timed text. None of them rejects anything."""
    stamps: list[float] = []
    for line in _lines(text):
        match = _TIMED_LINE.match(line)
        if match is not None:
            stamps.extend(_seconds(match.group(1)))
    findings = set()
    if len(stamps) < _FEW_STAMPS:
        findings.add("few_lines")
    if stamps and max(stamps) < _LOW_COVERAGE * duration:
        findings.add("low_coverage")
    return frozenset(findings)


def as_written(text: str) -> str:
    """Return text as a sidecar holds it: no BOM, only LF line endings, no stray control characters.

    Checks and the writer both use this, so what gets judged is what gets written.
    """
    normal = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_CHARACTERS.sub("", normal).strip()


def _lines(text: str) -> list[str]:
    return as_written(text).split("\n")


def _seconds(stamps: str) -> list[float]:
    return [
        int(minutes) * 60 + int(seconds) + (int(fraction) / 10 ** len(fraction) if fraction else 0)
        for minutes, seconds, fraction in _STAMP.findall(stamps)
    ]
