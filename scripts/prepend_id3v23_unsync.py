#!/usr/bin/env python3
"""Write a copy of an audio file with an unsynchronised ID3v2.3 tag in front. Fixtures only.

Usage: python3 -I prepend_id3v23_unsync.py <input> <output>

make_fixtures.sh calls this on a tone it has just made with ffmpeg. The tag holds the fixture
tags as UTF-16 text frames, so each frame's byte order mark FF FE is followed by a byte that
unsynchronisation must escape. Some taggers write FLAC files this way, and tinytag
2.3.2 reads such a tag without undoing the escapes. Standard library only: no tag library is
needed, and plyrix never depends on one. The input is read and never changed.
"""

import sys
from pathlib import Path

FRAMES = (
    (b"TIT2", "Fixture Title"),
    (b"TPE1", "Fixture Artist"),
    (b"TPE2", "Fixture Album Artist"),
    (b"TALB", "Fixture Album"),
)
PADDING = 64
UNSYNCHRONISATION = 0x80


def text_frame(frame_id: bytes, text: str) -> bytes:
    """An ID3v2.3 text frame in encoding 1, UTF-16 with a little-endian byte order mark."""
    body = b"\x01\xff\xfe" + text.encode("utf-16-le")
    return frame_id + len(body).to_bytes(4, "big") + b"\x00\x00" + body


def unsynchronise(data: bytes) -> bytes:
    """ID3v2.3 section 5: put 00 after every FF followed by 111xxxxx or 00, and after a final FF."""
    out = bytearray()
    for index, byte in enumerate(data):
        out.append(byte)
        following = data[index + 1] if index + 1 < len(data) else None
        if byte == 0xFF and (following is None or following >= 0xE0 or following == 0x00):
            out.append(0x00)
    return bytes(out)


def synchsafe(size: int) -> bytes:
    return bytes((size >> shift) & 0x7F for shift in (21, 14, 7, 0))


def tag() -> bytes:
    frames = b"".join(text_frame(frame_id, text) for frame_id, text in FRAMES)
    body = unsynchronise(frames) + bytes(PADDING)
    return b"ID3\x03\x00" + bytes([UNSYNCHRONISATION]) + synchsafe(len(body)) + body


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit("usage: prepend_id3v23_unsync.py <input> <output>")
    source, target = Path(sys.argv[1]), Path(sys.argv[2])
    target.write_bytes(tag() + source.read_bytes())


if __name__ == "__main__":
    main()
