"""Unsynchronisation across ID3 versions: v2.2 is undone like v2.3, v2.4's header flag is left
to tinytag, and both keep the file bytes unchanged."""

import os
from pathlib import Path

import pytest
from tinytag import TinyTag

from conftest import FIXTURES
from plyrix import tags

VALUES = {
    b"TT2": "Fixture Title",
    b"TP1": "Fixture Artist",
    b"TP2": "Fixture Album Artist",
    b"TAL": "Fixture Album",
}


def synchsafe(size: int) -> bytes:
    return bytes((size >> shift) & 0x7F for shift in (21, 14, 7, 0))


def v22_frame(frame_id: bytes, text: str) -> bytes:
    data = b"\x01" + text.encode("utf-16")  # UTF-16 with a BOM, FF FE on little-endian
    return frame_id + len(data).to_bytes(3, "big") + data


def unsynchronise(data: bytes) -> bytes:
    out = bytearray()
    for i, byte in enumerate(data):
        out.append(byte)
        nxt = data[i + 1] if i + 1 < len(data) else 0
        if byte == 0xFF and (nxt >= 0xE0 or nxt == 0x00 or i + 1 == len(data)):
            out.append(0x00)
    return bytes(out)


def audio_of(name: str) -> bytes:
    whole = (FIXTURES / name).read_bytes()
    size = sum(b << s for b, s in zip(whole[6:10], (21, 14, 7, 0), strict=True))
    return whole[10 + size :]


def v22_unsynced(name: str) -> bytes:
    body = unsynchronise(b"".join(v22_frame(k, v) for k, v in VALUES.items()))
    assert b"\xff\x00" in body
    return b"ID3\x02\x00\x80" + synchsafe(len(body)) + body + audio_of(name)


@pytest.mark.parametrize("name", ["id3v23_unsync.mp3", "id3v23_unsync.flac"])
def test_an_unsynchronised_id3v22_tag_reads_as_written(tmp_path: Path, name: str) -> None:
    copy = tmp_path / name
    copy.write_bytes(v22_unsynced(name))
    before = copy.read_bytes()

    found = tags.read(os.fsencode(copy))

    assert found.title == "Fixture Title"
    assert found.artist == "Fixture Artist"
    assert found.albumartist == "Fixture Album Artist"
    assert found.album == "Fixture Album"
    assert found.duration == pytest.approx(3.0, abs=0.25)
    assert copy.read_bytes() == before


def test_tinytag_alone_garbles_the_unsynchronised_id3v22_tag(tmp_path: Path) -> None:
    copy = tmp_path / "v22.mp3"
    copy.write_bytes(v22_unsynced("id3v23_unsync.mp3"))

    assert TinyTag.get(copy).title != "Fixture Title"


def test_a_v24_header_flag_is_left_to_tinytag(tmp_path: Path) -> None:
    """plyrix rewrites only v2.2 and v2.3; a flagged v2.4 tag reads as tinytag reads it."""
    whole = (FIXTURES / "id3v23_unsync.mp3").read_bytes()
    copy = tmp_path / "v24.mp3"
    copy.write_bytes(whole[:3] + b"\x04" + whole[4:])

    assert tags.read(os.fsencode(copy)).title == TinyTag.get(copy).title
