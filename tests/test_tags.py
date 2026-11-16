"""tags.read on all five formats: correct values, and embedded lyrics never leave the reader."""

import dataclasses
import errno
import os
import re
from pathlib import Path
from typing import IO

import pytest
from tinytag import TinyTag

from conftest import AUDIO_SUFFIXES, FIXTURES
from plyrix import tags
from plyrix.domain import TrackTags

TOKEN = "PLYRIX-EMBEDDED-TOKEN-7F3A"
FULL_TAG_FIXTURES = [f"t{suffix}" for suffix in AUDIO_SUFFIXES] + ["t_id3v24.mp3"]


def read(name: str) -> TrackTags:
    return tags.read(os.fsencode(FIXTURES / name))


@pytest.mark.parametrize("name", FULL_TAG_FIXTURES)
def test_full_tags(name: str) -> None:
    found = read(name)

    assert found.artist == "Fixture Artist"
    assert found.albumartist == "Fixture Album Artist"
    assert found.title == "Fixture Title"
    assert found.album == "Fixture Album"
    # ffprobe says 3.0 s. tinytag gives 3.168 s for the MP3, one 144 ms frame at this rate.
    assert found.duration == pytest.approx(3.0, abs=0.25)


@pytest.mark.parametrize("suffix", AUDIO_SUFFIXES)
def test_no_tags_is_all_none_with_no_exception(suffix: str) -> None:
    found = read(f"notags{suffix}")

    assert (found.artist, found.albumartist, found.title, found.album) == (None, None, None, None)
    assert found.duration == pytest.approx(1.0, abs=0.25)


@pytest.mark.parametrize("suffix", AUDIO_SUFFIXES)
def test_album_artist_only(suffix: str) -> None:
    found = read(f"albumartist_only{suffix}")

    assert found.artist is None
    assert found.albumartist == "Fixture Album Artist"


@pytest.mark.parametrize("suffix", AUDIO_SUFFIXES)
def test_embedded_token_stays_in_the_file(suffix: str) -> None:
    found = read(f"embedded_token{suffix}")

    assert found.artist == "Fixture Artist"
    assert TOKEN not in repr(found)
    assert TOKEN not in str(dataclasses.asdict(found))


@pytest.mark.parametrize("suffix", AUDIO_SUFFIXES)
def test_embedded_token_in_catch_all(suffix: str) -> None:
    """The control for the test above: it shows the token is there to leak, and on which files."""
    assert TOKEN in str(TinyTag.get(FIXTURES / f"embedded_token{suffix}").other)
    assert TOKEN not in str(TinyTag.get(FIXTURES / f"t{suffix}").other)


def test_track_tags_has_named_fields_only() -> None:
    fields = {field.name for field in dataclasses.fields(TrackTags)}

    # year is for the retry rule after a dump update (06-06), never a lyric field.
    assert fields == {"artist", "albumartist", "title", "album", "duration", "year"}


def test_read_copies_the_year(monkeypatch: pytest.MonkeyPatch) -> None:
    class Found:
        artist = albumartist = title = album = None
        duration = 1.0
        year = "2003-04-05"

    monkeypatch.setattr(tags._Reader, "get", lambda path: Found())

    assert tags.read(b"/x.flac").year == "2003-04-05"


@pytest.mark.parametrize("suffix", AUDIO_SUFFIXES)
def test_the_fixtures_carry_no_year(suffix: str) -> None:
    assert tags.read(bytes(FIXTURES / f"t{suffix}")).year is None


# tinytag 2.3.2 reads a partial result from most lengths, so each of these is one that fails.
@pytest.mark.parametrize(
    ("suffix", "length"),
    [(".flac", 10), (".mp3", 20), (".m4a", 4), (".ogg", 40), (".opus", 4)],
)
def test_a_truncated_file_is_unreadable_and_says_only_a_type_name(
    tmp_path: Path, suffix: str, length: int
) -> None:
    name = f"{TOKEN}{suffix}"
    (tmp_path / name).write_bytes((FIXTURES / f"t{suffix}").read_bytes()[:length])

    with pytest.raises(tags.Unreadable) as raised:
        tags.read(os.fsencode(tmp_path / name))

    assert re.fullmatch(r"[A-Za-z]+", str(raised.value))
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


@pytest.mark.parametrize("suffix", AUDIO_SUFFIXES)
def test_a_file_cut_off_anywhere_reads_or_is_unreadable(tmp_path: Path, suffix: str) -> None:
    whole = (FIXTURES / f"t{suffix}").read_bytes()
    copy = tmp_path / f"t{suffix}"

    for length in range(0, min(len(whole), 1200), 7):
        copy.write_bytes(whole[:length])
        try:
            tags.read(os.fsencode(copy))
        except tags.Unreadable:
            pass


def test_a_missing_file_is_gone_and_says_only_a_type_name(tmp_path: Path) -> None:
    with pytest.raises(tags.Gone) as raised:
        tags.read(os.fsencode(tmp_path / f"{TOKEN}.flac"))

    assert str(raised.value) == "FileNotFoundError"


def test_a_folder_replaced_by_a_file_is_gone(tmp_path: Path) -> None:
    (tmp_path / "album").write_bytes(b"")

    with pytest.raises(tags.Gone) as raised:
        tags.read(os.fsencode(tmp_path / "album" / f"{TOKEN}.flac"))

    assert str(raised.value) == "NotADirectoryError"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_a_file_that_cant_be_opened_is_an_access_failure_and_says_only_a_type_name(
    tmp_path: Path,
) -> None:
    locked = tmp_path / f"{TOKEN}.flac"
    locked.write_bytes((FIXTURES / "t.flac").read_bytes())
    locked.chmod(0)

    try:
        with pytest.raises(tags.AccessFailed) as raised:
            tags.read(os.fsencode(locked))
    finally:
        locked.chmod(0o644)

    assert str(raised.value) == "PermissionError"
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


class _FailingFile:
    """A real open file whose reads all fail, as on a bad disk or a stale network mount."""

    def __init__(self, inner: IO[bytes], code: int) -> None:
        self._inner = inner
        self._code = code

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def read(self, *_args: object) -> bytes:
        raise OSError(self._code, os.strerror(self._code))


def reads_fail_with(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    """Fail tinytag's own reads, so its parser hits the error and wraps it as it really does."""

    def failing_open(path: bytes, mode: str) -> _FailingFile:
        return _FailingFile(open(path, mode), code)  # noqa: SIM115

    monkeypatch.setattr("tinytag.tinytag.open", failing_open, raising=False)


@pytest.mark.parametrize("suffix", AUDIO_SUFFIXES)
@pytest.mark.parametrize("code", [errno.EIO, errno.ESTALE], ids=["eio", "estale"])
def test_an_io_error_while_parsing_is_an_access_failure_not_a_corrupt_file(
    monkeypatch: pytest.MonkeyPatch, suffix: str, code: int
) -> None:
    reads_fail_with(monkeypatch, code)

    with pytest.raises(tags.AccessFailed) as raised:
        read(f"t{suffix}")

    assert str(raised.value) == "OSError"


def test_a_seek_before_the_start_of_a_corrupt_file_stays_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corrupt sizes can do this. Calling it an access failure would fail every run for good."""
    reads_fail_with(monkeypatch, errno.EINVAL)

    with pytest.raises(tags.Unreadable):
        read("t.flac")


def test_a_path_that_is_not_utf8_reads(tmp_path: Path) -> None:
    path = os.path.join(os.fsencode(tmp_path), b"caf\xe9-\xff.flac")
    with open(path, "wb") as copy:
        copy.write((FIXTURES / "t.flac").read_bytes())

    assert tags.read(path).title == "Fixture Title"


# Some taggers put an ID3v2.3 tag in front of a FLAC and set its unsynchronisation flag, which
# escapes every FF that could look like an MPEG sync with an extra 00. tinytag 2.3.2 never undoes
# that, so the byte order mark of the first text frame reads as text and every later frame is lost.
UNSYNC_FIXTURES = ["id3v23_unsync.flac", "id3v23_unsync.mp3"]


@pytest.mark.parametrize("name", UNSYNC_FIXTURES)
def test_an_unsynchronised_id3v23_tag_reads_as_written(name: str) -> None:
    before = (FIXTURES / name).read_bytes()

    found = read(name)

    assert found.artist == "Fixture Artist"
    assert found.albumartist == "Fixture Album Artist"
    assert found.title == "Fixture Title"
    assert found.album == "Fixture Album"
    assert found.duration == pytest.approx(3.0, abs=0.25)
    assert (FIXTURES / name).read_bytes() == before


@pytest.mark.parametrize("name", UNSYNC_FIXTURES)
def test_tinytag_alone_garbles_the_unsynchronised_tag(name: str) -> None:
    """The control: the defect plyrix works round is in these files. If a tinytag upgrade fixes
    it, this fails, and plyrix's own handling can go."""
    found = TinyTag.get(FIXTURES / name)

    assert found.title != "Fixture Title"
    assert found.artist is None


def test_a_tag_without_the_flag_is_left_to_tinytag(tmp_path: Path) -> None:
    """An FF 00 pair in a tag without the flag is data, and stays as it is."""
    whole = (FIXTURES / "id3v23_unsync.flac").read_bytes()
    copy = tmp_path / "plain.flac"
    copy.write_bytes(whole[:5] + b"\x00" + whole[6:])

    assert tags.read(os.fsencode(copy)).title == TinyTag.get(copy).title != "Fixture Title"


@pytest.mark.parametrize("name", UNSYNC_FIXTURES)
def test_an_unsynchronised_tag_cut_off_anywhere_reads_or_is_unreadable(
    tmp_path: Path, name: str
) -> None:
    whole = (FIXTURES / name).read_bytes()
    copy = tmp_path / name

    for length in range(0, min(len(whole), 400)):
        copy.write_bytes(whole[:length])
        try:
            tags.read(os.fsencode(copy))
        except tags.Unreadable:
            pass


# tinytag 2.3.2 reads an ID3v2.3 extended header's size as v2.4's synchsafe size that counts
# itself, so it skips 6 bytes where v2.3 has 10 or 14, and every frame after it is lost. plyrix
# drops the extended header, which holds no tag values, before tinytag parses the frames.
# Some taggers write the v2.4 form, whose size counts itself, into a v2.3 tag. Its usual sizes,
# 6 and 10, are v2.3's too, and tinytag reads that form right, so plyrix must not strip 4 too many.
_PADDING = (64).to_bytes(4, "big")
EXTENDED_HEADERS = {
    # ID3v2.3 section 3.2: a size that doesn't count its own 4 bytes, flags, padding size, CRC.
    "v23": (6).to_bytes(4, "big") + b"\x00\x00" + _PADDING,
    "v23-crc": (10).to_bytes(4, "big") + b"\x80\x00" + _PADDING + b"\x12\x34\x56\x78",
    # ID3v2.4 section 3.2: a size that counts itself, a flag count, then the flags.
    "self-6": (6).to_bytes(4, "big") + b"\x01\x00",
    "self-10": (10).to_bytes(4, "big") + b"\x01\x00\x00\x00\x00\x00",
}
V23_FORMS = ["v23", "v23-crc"]
SELF_COUNTING_FORMS = ["self-6", "self-10"]


def with_extended_header(name: str, form: str, unsynchronised: bool) -> bytes:
    """A fixture's tag, rebuilt as v2.3 with an extended header, and its audio unchanged."""
    whole = (FIXTURES / name).read_bytes()
    size = sum(byte << shift for byte, shift in zip(whole[6:10], (21, 14, 7, 0), strict=True))
    stored, audio = whole[10 : 10 + size], whole[10 + size :]
    frames = stored if unsynchronised else stored.replace(b"\xff\x00", b"\xff")
    body = EXTENDED_HEADERS[form] + frames
    flags = 0x40 | (0x80 if unsynchronised else 0)
    synchsafe = bytes((len(body) >> shift) & 0x7F for shift in (21, 14, 7, 0))
    return b"ID3\x03\x00" + bytes([flags]) + synchsafe + body + audio


def with_v23_extended_header(name: str, crc: bool, unsynchronised: bool) -> bytes:
    return with_extended_header(name, "v23-crc" if crc else "v23", unsynchronised)


def extended_cases(forms: list[str], unsyncs: tuple[bool, ...] = (False, True)) -> list[object]:
    return [
        pytest.param(name, form, unsync, id=f"{name}-{form}-{'unsync' if unsync else 'plain'}")
        for name in UNSYNC_FIXTURES
        for form in forms
        for unsync in unsyncs
    ]


@pytest.mark.parametrize(
    ("name", "form", "unsync"), extended_cases(V23_FORMS + SELF_COUNTING_FORMS)
)
def test_an_extended_header_in_a_v23_tag_keeps_the_frames_after_it(
    tmp_path: Path, name: str, form: str, unsync: bool
) -> None:
    copy = tmp_path / name
    copy.write_bytes(with_extended_header(name, form, unsync))
    before = copy.read_bytes()

    found = tags.read(os.fsencode(copy))

    assert found.artist == "Fixture Artist"
    assert found.albumartist == "Fixture Album Artist"
    assert found.title == "Fixture Title"
    assert found.album == "Fixture Album"
    assert found.duration == pytest.approx(3.0, abs=0.25)
    assert copy.read_bytes() == before


@pytest.mark.parametrize(("name", "form", "unsync"), extended_cases(V23_FORMS))
def test_tinytag_alone_loses_the_frames_after_a_v23_extended_header(
    tmp_path: Path, name: str, form: str, unsync: bool
) -> None:
    """The control. If a tinytag upgrade reads these, this fails, and the workaround can go."""
    copy = tmp_path / name
    copy.write_bytes(with_extended_header(name, form, unsync))

    assert TinyTag.get(copy).artist is None


@pytest.mark.parametrize(
    ("name", "form", "unsync"), extended_cases(SELF_COUNTING_FORMS, unsyncs=(False,))
)
def test_tinytag_alone_reads_a_self_counting_extended_header(
    tmp_path: Path, name: str, form: str, unsync: bool
) -> None:
    """The control for the form plyrix must leave readable: tinytag already skips it right."""
    copy = tmp_path / name
    copy.write_bytes(with_extended_header(name, form, unsync))

    assert TinyTag.get(copy).artist == "Fixture Artist"


@pytest.mark.parametrize("name", UNSYNC_FIXTURES)
def test_an_extended_header_that_lands_on_no_frame_is_left_to_tinytag(
    tmp_path: Path, name: str
) -> None:
    """Neither reading of the size finds a frame ID or padding, so the tag reaches tinytag as is."""
    built = with_extended_header(name, "v23", unsynchronised=False)
    # The first frame ID, 10 bytes into the body, in lower case; 6 bytes in is the padding size.
    copy = tmp_path / name
    copy.write_bytes(built[:20] + built[20:24].lower() + built[24:])

    found = tags.read(os.fsencode(copy))

    assert found.artist == TinyTag.get(copy).artist
    assert found.title == TinyTag.get(copy).title


@pytest.mark.parametrize("declared", [0, 7, 0xFFFFFFFF])
def test_a_v23_extended_header_of_a_size_v23_cant_have_reads_or_is_unreadable(
    tmp_path: Path, declared: int
) -> None:
    built = with_v23_extended_header("id3v23_unsync.flac", crc=False, unsynchronised=False)
    copy = tmp_path / "bad.flac"
    copy.write_bytes(built[:10] + declared.to_bytes(4, "big") + built[14:])

    try:
        tags.read(os.fsencode(copy))
    except tags.Unreadable:
        pass


@pytest.mark.parametrize("unsync", [False, True])
def test_a_v23_extended_header_cut_off_anywhere_reads_or_is_unreadable(
    tmp_path: Path, unsync: bool
) -> None:
    whole = with_v23_extended_header("id3v23_unsync.flac", crc=True, unsynchronised=unsync)
    copy = tmp_path / "cut.flac"

    for length in range(0, min(len(whole), 400)):
        copy.write_bytes(whole[:length])
        try:
            tags.read(os.fsencode(copy))
        except tags.Unreadable:
            pass
