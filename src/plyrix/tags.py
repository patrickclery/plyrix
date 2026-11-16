"""Read tags with tinytag, the only module that imports it. Named fields only.

tinytag's catch-all field and dict export expose embedded lyrics, so neither is used here.

tinytag 2.3.2 ignores an ID3v2.2 or v2.3 tag's unsynchronisation flag, so a FLAC or MP3 with
such a tag reads a garbled title and loses the frames after it. It also skips too little of a
v2.3 extended header, losing every frame. _ID3Unsynced undoes the one and drops the other,
working only on the bytes tinytag's own read-only handle returns: plyrix opens nothing itself here.
"""

import errno
import io
from typing import BinaryIO

from tinytag import TinyTag, TinyTagException

# Private to tinytag, which is pinned. tests/test_tags.py proves both overrides on fixtures, and
# fails if a tinytag release fixes the defect, so this can go then.
from tinytag.tinytag import _ID3

from plyrix.domain import TrackTags

_UNSYNCHRONISED = 0x80
# v2.4 flags unsynchronisation on each frame instead, with sizes taken after it; not handled.
_WHOLE_TAG_VERSIONS = (2, 3)
# In v2.2 this bit means compression, and tinytag reads v2.4's extended header correctly.
_EXTENDED = 0x40
# ID3v2.3 section 3.2: 6 bytes, or 10 with a CRC, after a 4-byte size that doesn't count itself.
# Some taggers write v2.4's form instead, whose size counts itself, usually with these sizes too.
_V23_EXTENDED_SIZES = (6, 10)
_PADDING = b"\x00" * 4


def _synchsafe(size: int) -> bytes:
    return bytes((size >> shift) & 0x7F for shift in (21, 14, 7, 0))


def _starts_frames(chunk: bytes) -> bool:
    """Whether chunk is a v2.3 frame ID, four of A-Z and 0-9, or the start of padding."""
    return len(chunk) == 4 and (
        chunk == _PADDING or all(0x30 <= b <= 0x39 or 0x41 <= b <= 0x5A for b in chunk)
    )


def _without_v23_extended_header(body: bytes) -> bytes | None:
    """The tag body after its extended header, or None if neither reading of its size lands on a
    frame or padding."""
    size = int.from_bytes(body[:4], "big")
    if size not in _V23_EXTENDED_SIZES:
        return None
    # The v2.3 form first, then the v2.4 form, which tinytag reads right but plyrix would overshoot.
    for skip in (4 + size, size):
        if _starts_frames(body[skip : skip + 4]):
            return body[skip:]
    return None


class _ID3Unsynced(_ID3):
    """tinytag's ID3 parser, undoing a v2.2 or v2.3 tag's unsynchronisation and dropping a v2.3
    extended header before reading it."""

    def _parse_id3v2(self, fh: BinaryIO) -> int:
        start = fh.tell()
        header = fh.read(self._ID3V2_HEADER_SIZE)
        id3 = len(header) == self._ID3V2_HEADER_SIZE and header.startswith(b"ID3")
        unsynchronised = (
            id3 and header[3] in _WHOLE_TAG_VERSIONS and bool(header[5] & _UNSYNCHRONISED)
        )
        extended = id3 and header[3] == 3 and bool(header[5] & _EXTENDED)
        fh.seek(start)
        if not (unsynchronised or extended):
            return super()._parse_id3v2(fh)
        size = self._unsynchsafe(header, 6)
        stored = fh.read(self._ID3V2_HEADER_SIZE + size)[self._ID3V2_HEADER_SIZE :]
        # ID3v2.3 section 5: a reader drops the 00 after every FF, extended header included.
        body = stored.replace(b"\xff\x00", b"\xff") if unsynchronised else stored
        flags = header[5] & ~_UNSYNCHRONISED
        if extended:
            frames = _without_v23_extended_header(body)
            # A header plyrix can't place is left in place, for tinytag to read as it always has.
            if frames is not None:
                body, flags = frames, flags & ~_EXTENDED
        plain = header[:5] + bytes([flags]) + _synchsafe(len(body)) + body
        super()._parse_id3v2(io.BytesIO(plain))
        # The caller finds the audio after the tag as stored, so give it the stored size.
        fh.seek(start + self._ID3V2_HEADER_SIZE + size)
        return size


class _Reader(TinyTag):
    """TinyTag.get, choosing _ID3Unsynced where tinytag would choose its own ID3 parser."""

    @classmethod
    def _get_parser_class(
        cls, filename: str | None = None, filehandle: BinaryIO | None = None
    ) -> type[TinyTag]:
        # Called on TinyTag itself, which picks by name and magic bytes; on a subclass it would
        # return the subclass for every file.
        parser = TinyTag._get_parser_class(filename, filehandle)
        return _ID3Unsynced if parser is _ID3 else parser


class Unreadable(Exception):
    """A file was read and couldn't be parsed. Holds a type name, and never a tag value."""


class Gone(Exception):
    """A file has gone since the walk listed it, which says nothing of it. Holds a type name."""


class AccessFailed(Exception):
    """Permissions or I/O stopped a file being read, which can be fixed. Holds a type name."""


def read(path: bytes) -> TrackTags:
    """Copy the named tag fields of the file at path."""
    try:
        found = _Reader.get(path)
    except TinyTagException as error:
        # tinytag wraps what its parser raises, so a failed read while parsing only shows here.
        # EINVAL is a seek before the start of the file, sent there by a corrupt file's sizes.
        cause = error.__cause__
        if isinstance(cause, OSError) and cause.errno != errno.EINVAL:
            raise AccessFailed(type(cause).__name__) from None
        raise Unreadable(type(error).__name__) from None
    except (FileNotFoundError, NotADirectoryError) as error:
        raise Gone(type(error).__name__) from None
    except OSError as error:
        raise AccessFailed(type(error).__name__) from None
    return TrackTags(
        artist=found.artist,
        albumartist=found.albumartist,
        title=found.title,
        album=found.album,
        duration=found.duration,
        year=found.year,
    )
