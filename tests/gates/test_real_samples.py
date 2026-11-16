"""D-14 gate: plyrix.tags against ffprobe on real samples copied to a scratch area.

Skipped unless PLYRIX_REAL_SAMPLES names that copy. Output and the result file hold counts only,
never a tag value or a file name, so nothing here can identify a sample.
"""

import json
import os
import shutil
import struct
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from tinytag import TinyTag

from plyrix import tags

AUDIO_SUFFIXES = {b".flac", b".mp3", b".m4a", b".opus", b".ogg"}
REQUIRED = ("flac", "mp3", "m4a-aac", "m4a-alac", "opus", "ogg")
CLASS_BY_CODEC = {
    "flac": "flac",
    "mp3": "mp3",
    "aac": "m4a-aac",
    "alac": "m4a-alac",
    "opus": "opus",
    "vorbis": "ogg",
}
# Field in the result, attribute on TrackTags, and the keys ffprobe may report it under.
TAG_FIELDS = (
    ("artist", "artist", ("artist",)),
    ("title", "title", ("title",)),
    ("album", "album", ("album",)),
    ("album_artist", "albumartist", ("album_artist", "albumartist", "album artist")),
)
DURATION_TOLERANCE_S = 2.0
# Marks where _keep_repeats joined a repeated key. No tag text can hold it, unlike "/" or ";".
MULTI_VALUE_SEPARATOR = "\x00"
# What ffprobe itself puts between the values of a multi-value FLAC, Ogg, Opus or M4A tag.
FFPROBE_JOINER = ";"
VBR_MARKERS = (b"Xing", b"Info", b"VBRI")
VBR_SEARCH_BYTES = 4096
FFPROBE_TIMEOUT_S = 600


def _empty_class() -> dict[str, Any]:
    fields = [field for field, _, _ in TAG_FIELDS] + ["duration"]
    return {
        "files": 0,
        "mismatches": dict.fromkeys(fields, 0),
        "exceptions": 0,
        "largest_duration_gap": 0.0,
    }


def _audio_files(root: str) -> Iterator[bytes]:
    for folder, dirs, names in os.walk(os.fsencode(root)):
        dirs.sort()
        for name in sorted(names):
            if os.path.splitext(name)[1].lower() in AUDIO_SUFFIXES:
                yield os.path.join(folder, name)


def _keep_repeats(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Join a repeated JSON key, as ffprobe prints one for each value of a multi-value ID3 frame."""
    joined: dict[str, Any] = {}
    for key, value in pairs:
        joined[key] = f"{joined[key]}{MULTI_VALUE_SEPARATOR}{value}" if key in joined else value
    return joined


def _ffprobe(path: bytes, *options: str) -> str:
    done = subprocess.run(
        ["ffprobe", "-v", "quiet", *options, path],
        capture_output=True,
        check=True,
        timeout=FFPROBE_TIMEOUT_S,
    )
    return done.stdout.decode("utf-8", "surrogateescape")


def _probe(path: bytes) -> tuple[str, dict[str, str], float | None] | None:
    """Return the class, the tags with lower-case keys, and the container duration."""
    report = json.loads(
        _ffprobe(path, "-print_format", "json", "-show_format", "-show_streams"),
        object_pairs_hook=_keep_repeats,
    )
    audio = next((s for s in report.get("streams", []) if s.get("codec_type") == "audio"), None)
    if audio is None or audio.get("codec_name") not in CLASS_BY_CODEC:
        return None
    container = report.get("format", {})
    found: dict[str, str] = {}
    # Opus and Ogg hold their tags on the stream, and the other formats on the container.
    for source in (container.get("tags", {}), audio.get("tags", {})):
        for key, value in source.items():
            found.setdefault(key.strip().lower(), str(value))
    duration = container.get("duration", audio.get("duration"))
    return CLASS_BY_CODEC[audio["codec_name"]], found, float(duration) if duration else None


def _has_vbr_marker(path: bytes) -> bool:
    """Look for a VBR header in the bytes after any ID3v2 tag."""
    with open(path, "rb") as handle:
        header = handle.read(10)
        start = 0
        if len(header) == 10 and header.startswith(b"ID3"):
            size = 0
            for byte in header[6:10]:
                size = (size << 7) | (byte & 0x7F)
            start = 10 + size + (10 if header[5] & 0x10 else 0)
        handle.seek(start)
        window = handle.read(VBR_SEARCH_BYTES)
    return any(marker in window for marker in VBR_MARKERS)


def _packet_duration(path: bytes) -> float:
    """Sum the packets, as ffprobe's container duration only estimates a file with no VBR header."""
    rows = _ffprobe(
        path, "-select_streams", "a:0", "-show_entries", "packet=duration_time", "-of", "csv=p=0"
    )
    total = 0.0
    for row in rows.splitlines():
        cell = row.strip().strip(",")
        if cell and cell != "N/A":
            total += float(cell)
    return total


def _other_values(path: bytes) -> dict[str, list[str]]:
    """Return the values after the first that tinytag found, by TrackTags attribute.

    plyrix.tags copies only the first, so this reads tinytag directly. It is the one way to tell a
    real second value from a ";" in the text. Only these fields are read, and none is recorded.
    """
    other = TinyTag.get(path).other
    return {attribute: list(other.get(attribute, [])) for _, attribute, _ in TAG_FIELDS}


def _same(value: str | None, reference: str | None, others: Sequence[str] = ()) -> bool:
    """Say whether tinytag's values, the first and the others, are what ffprobe reported.

    ffprobe reports a multi-value tag in one of three ways: its first value only (ID3v2.4), its
    values joined with a ";" of its own (FLAC, Ogg, Opus and M4A), or a repeated JSON key. All the
    values have to agree when it shows them. A ";" or "/" that tinytag has no value for has to be
    in tinytag's text, so a value cut off at one fails.
    """
    found = (value or "").strip()
    expected = (reference or "").strip()
    if found == expected:
        return True
    values = [found, *(other.strip() for other in others)]
    if not found:
        return False
    if MULTI_VALUE_SEPARATOR in expected:
        return [part.strip() for part in expected.split(MULTI_VALUE_SEPARATOR)] == values
    return len(values) > 1 and FFPROBE_JOINER.join(values) == expected


def _measure(path: bytes, result: dict[str, Any]) -> None:
    try:
        probed = _probe(path)
    except (subprocess.SubprocessError, OSError, ValueError):
        probed = None
    if probed is None:
        result["unclassified"] += 1
        return
    name, reference, duration = probed
    counts = result["classes"][name]
    counts["files"] += 1
    no_vbr_marker = False
    try:
        if name == "mp3":
            no_vbr_marker = not _has_vbr_marker(path)
            if no_vbr_marker:
                duration = _packet_duration(path)
                result["mp3_no_vbr_header"]["files"] += 1
        found = tags.read(path)
        others = _other_values(path)
    except Exception:  # Any failure at all is the count the gate exists to report.
        counts["exceptions"] += 1
        return
    for field, attribute, keys in TAG_FIELDS:
        expected = next((reference[key] for key in keys if key in reference), None)
        if not _same(getattr(found, attribute), expected, others[attribute]):
            counts["mismatches"][field] += 1
    if found.duration is None or duration is None:
        counts["mismatches"]["duration"] += 1
        return
    gap = abs(found.duration - duration)
    if gap > DURATION_TOLERANCE_S:
        counts["mismatches"]["duration"] += 1
    counts["largest_duration_gap"] = max(counts["largest_duration_gap"], round(gap, 3))
    if no_vbr_marker:
        group = result["mp3_no_vbr_header"]
        group["largest_duration_gap"] = max(group["largest_duration_gap"], round(gap, 3))


def _gate_result(root: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "fail",
        "classes": {name: _empty_class() for name in REQUIRED},
        "missing_classes": [],
        "unclassified": 0,
        "mp3_no_vbr_header": {"files": 0, "largest_duration_gap": 0.0},
    }
    for path in _audio_files(root):
        _measure(path, result)
    classes = result["classes"]
    result["missing_classes"] = [name for name in REQUIRED if classes[name]["files"] == 0]
    clean = all(
        counts["exceptions"] == 0 and not any(counts["mismatches"].values())
        for counts in classes.values()
    )
    if clean and not result["missing_classes"] and result["unclassified"] == 0:
        result["status"] = "pass"
    return result


@pytest.mark.parametrize(
    ("value", "reference", "others", "same"),
    [
        ("A", "A", [], True),
        (" A ", "A", [], True),
        (None, "", [], True),
        (None, None, [], True),
        ("A/B", "A/B", [], True),
        ("A;B", "A;B", [], True),
        # ffprobe joined a real multi-value tag itself, on FLAC, Ogg, Opus and M4A.
        ("A", "A;B", ["B"], True),
        ("A", "A;B;C", ["B", "C"], True),
        (" A ", "A;B", [" B "], True),
        # ffprobe shows only the first value of an ID3v2.4 tag.
        ("A", "A", ["B"], True),
        # A repeated JSON key.
        ("A", "A\x00B", ["B"], True),
        # Cut off at a separator: tinytag has no value that could have followed it.
        ("A", "A;B", [], False),
        ("A", "A/B", [], False),
        ("A", "A / B", [], False),
        ("A", "A\x00B", [], False),
        # A second value that isn't the one ffprobe has, or one too few or too many.
        ("A", "A;B", ["C"], False),
        ("A", "A;B;C", ["B"], False),
        ("A", "A;B", ["B", "C"], False),
        ("A", "A\x00B", ["C"], False),
        ("A", "A\x00B\x00C", ["B"], False),
        # tinytag's values in another order, or joined differently, are not the same tag.
        ("B", "A;B", ["A"], False),
        ("A", "A/B", ["B"], False),
        ("B", "A\x00B", [], False),
        (None, "A", [], False),
        ("A", "", [], False),
        ("", "A\x00B", [], False),
        (None, "A;B", ["B"], False),
    ],
)
def test_same_accepts_only_equal_values(
    value: str | None, reference: str | None, others: list[str], same: bool
) -> None:
    assert _same(value, reference, others) is same


def test_keep_repeats_joins_a_repeated_key_with_the_private_separator() -> None:
    report = json.loads(
        '{"artist": "A", "artist": "B/C", "title": "T;U"}', object_pairs_hook=_keep_repeats
    )
    assert report == {"artist": "A\x00B/C", "title": "T;U"}
    assert _same("A", report["artist"], ["B/C"]) is True
    assert _same("T", report["title"]) is False


def _flac_with_comments(tmp_path: Path, comments: list[str]) -> bytes:
    """Copy the synthetic fixture with its Vorbis comments replaced, and return the copy's path."""
    source = (Path(__file__).parents[1] / "fixtures" / "t.flac").read_bytes()
    blocks: list[bytes] = []
    position = 4
    while True:
        header = source[position]
        size = int.from_bytes(source[position + 1 : position + 4], "big")
        body = source[position + 4 : position + 4 + size]
        position += 4 + size
        if header & 0x7F == 4:
            vendor = b"fixture"
            encoded = [comment.encode() for comment in comments]
            body = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(encoded))
            body += b"".join(struct.pack("<I", len(item)) + item for item in encoded)
        blocks.append(bytes([header]) + len(body).to_bytes(3, "big") + body)
        if header & 0x80:
            break
    path = tmp_path / "multi.flac"
    path.write_bytes(source[:4] + b"".join(blocks) + source[position:])
    return os.fsencode(path)


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe is not on PATH")
@pytest.mark.parametrize(
    ("artists", "same"),
    [
        (["First", "Second"], True),
        (["First", "Second", "Third"], True),
        (["First;Second"], True),
        (["First"], True),
    ],
    ids=["two-values", "three-values", "literal-semicolon", "one-value"],
)
def test_the_gate_compares_a_real_file_with_ffprobe(
    tmp_path: Path, artists: list[str], same: bool
) -> None:
    path = _flac_with_comments(tmp_path, [f"ARTIST={artist}" for artist in artists])

    probed = _probe(path)

    assert probed is not None
    found = tags.read(path).artist
    assert _same(found, probed[1]["artist"], _other_values(path)["artist"]) is same


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe is not on PATH")
def test_the_gate_fails_a_multi_value_tag_when_the_reader_loses_a_value(tmp_path: Path) -> None:
    path = _flac_with_comments(tmp_path, ["ARTIST=First", "ARTIST=Second"])

    probed = _probe(path)

    assert probed is not None
    assert _same("First", probed[1]["artist"], []) is False
    assert _same("Second", probed[1]["artist"], ["First"]) is False


@pytest.mark.real_samples
def test_tag_reader_against_ffprobe() -> None:
    root = os.environ.get("PLYRIX_REAL_SAMPLES")
    if not root:
        pytest.skip("set PLYRIX_REAL_SAMPLES to a scratch copy of real samples")
    result = _gate_result(root)
    destination = os.environ.get("PLYRIX_GATE_RESULT")
    if destination:
        Path(destination).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    for name, counts in result["classes"].items():
        print(
            f"{name}: files={counts['files']} mismatches={sum(counts['mismatches'].values())}"
            f" exceptions={counts['exceptions']} largest_gap={counts['largest_duration_gap']}"
        )
    print(f"mp3_no_vbr_header: {result['mp3_no_vbr_header']}")
    print(f"unclassified: {result['unclassified']}")
    status = result["status"]
    assert status == "pass"
