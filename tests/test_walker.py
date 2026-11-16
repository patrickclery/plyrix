"""The walker: what counts as a track, what is left out, and what gets reported instead."""

import errno
import inspect
import os
import shutil
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from conftest import plant_deep_track
from plyrix import walker
from plyrix.domain import Track
from plyrix.pipeline import Counts
from plyrix.walker import Problem, Skip

PATH_MAX = os.pathconf("/", "PC_PATH_MAX")
NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")


def touch(root: Path, *names: str | bytes) -> None:
    for name in names:
        path = os.path.join(os.fsencode(root), os.fsencode(name))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as file:
            file.write(b"not really audio")


def walk(
    root: Path, excludes: Sequence[str] = (), start: Path | None = None
) -> tuple[list[bytes], list[tuple[bytes, Problem]]]:
    """Return the root-relative track paths and problems, in the order the walker gave them."""
    base = os.fsencode(root)
    tracks: list[bytes] = []
    problems: list[tuple[bytes, Problem]] = []
    for item in walker.walk(base, os.fsencode(start or root), excludes):
        if isinstance(item, Track):
            tracks.append(os.path.relpath(os.path.join(item.dir, item.name), base))
        else:
            problems.append((os.path.relpath(item.path, base), item.problem))
    return tracks, problems


def test_excludes_have_no_default() -> None:
    parameter = inspect.signature(walker.walk).parameters["excludes"]

    assert parameter.default is inspect.Parameter.empty


def test_no_excludes_leaves_out_nothing(tmp_path: Path) -> None:
    touch(tmp_path, "a/t.flac", "old_music/t.flac")

    assert walk(tmp_path) == ([b"a/t.flac", b"old_music/t.flac"], [])


@pytest.mark.parametrize(
    ("pattern", "kept"),
    [
        ("skipme", [b"keep/t.flac", b"keep/skipme/t.flac"]),
        ("skipme/", [b"keep/t.flac", b"keep/skipme/t.flac"]),
        ("skipme/*", [b"keep/t.flac", b"keep/skipme/t.flac"]),
        ("keep/skipme", [b"keep/t.flac", b"skipme/t.flac", b"skipme/deep/t.flac"]),
        ("*.flac", []),
        ("*/t.flac", []),
        (
            "SKIPME",
            [b"keep/t.flac", b"keep/skipme/t.flac", b"skipme/t.flac", b"skipme/deep/t.flac"],
        ),
        ("", [b"keep/t.flac", b"keep/skipme/t.flac", b"skipme/t.flac", b"skipme/deep/t.flac"]),
    ],
)
def test_excludes_match_the_path_from_the_root(
    tmp_path: Path, pattern: str, kept: list[bytes]
) -> None:
    touch(tmp_path, "keep/t.flac", "keep/skipme/t.flac", "skipme/t.flac", "skipme/deep/t.flac")

    assert walk(tmp_path, [pattern]) == (kept, [])


@NOT_ROOT
def test_an_excluded_folder_is_never_opened(tmp_path: Path) -> None:
    touch(tmp_path, "keep/t.flac", "skipme/t.flac")
    (tmp_path / "skipme").chmod(0)
    try:
        # Opening it would report a problem, since this folder can't be listed.
        assert walk(tmp_path, ["skipme"]) == ([b"keep/t.flac"], [])
    finally:
        (tmp_path / "skipme").chmod(0o755)


def test_excludes_stay_relative_to_the_root_on_a_narrowed_walk(tmp_path: Path) -> None:
    touch(tmp_path, "music/keep/t.flac", "music/skipme/t.flac")
    start = tmp_path / "music"

    assert walk(tmp_path, ["music/skipme"], start) == ([b"music/keep/t.flac"], [])
    assert walk(tmp_path, ["skipme"], start)[0] == [b"music/keep/t.flac", b"music/skipme/t.flac"]


@pytest.mark.parametrize("pattern", ["music", "music/skipme", "music/*", "*"])
def test_a_walk_starting_inside_an_excluded_folder_finds_nothing(
    tmp_path: Path, pattern: str
) -> None:
    touch(tmp_path, "music/skipme/deep/t.flac")

    assert walk(tmp_path, [pattern], tmp_path / "music/skipme/deep") == ([], [])


def test_an_exclude_matches_a_name_that_is_not_utf8(tmp_path: Path) -> None:
    touch(tmp_path, b"caf\xe9/t.flac", b"keep/t.flac")

    assert walk(tmp_path, [os.fsdecode(b"caf\xe9")]) == ([b"keep/t.flac"], [])


def test_an_exclude_matches_a_name_that_is_not_ascii(tmp_path: Path) -> None:
    touch(tmp_path, "Björk/t.flac", "日本/t.flac", "keep/t.flac")

    assert walk(tmp_path, ["Björk", "日本"]) == ([b"keep/t.flac"], [])


@pytest.mark.parametrize("pattern", ["café", os.fsdecode(b"caf\xe9")], ids=["utf8", "not-utf8"])
def test_an_exclude_holding_a_non_ascii_character_is_not_a_wildcard(
    tmp_path: Path, pattern: str
) -> None:
    touch(tmp_path, "cafe/t.flac", "cafes/t.flac")

    assert walk(tmp_path, [pattern]) == ([b"cafe/t.flac", b"cafes/t.flac"], [])


@pytest.mark.parametrize(
    ("name", "is_audio"),
    [
        ("t.flac", True),
        ("t.FLAC", True),
        ("t.Mp3", True),
        ("t.m4a", True),
        ("t.OGG", True),
        ("t.opus", True),
        (".hidden.flac", True),
        ("..flac", False),
        (".flac", False),
        ("flac", False),
        ("t.flac.bak", False),
        ("t.wav", False),
        ("t.lrc", False),
        (".plyrix-0123abcd.part", False),
    ],
)
def test_what_counts_as_audio(tmp_path: Path, name: str, is_audio: bool) -> None:
    touch(tmp_path, name)

    assert walk(tmp_path) == ([name.encode()] if is_audio else [], [])


def test_a_track_keeps_its_folder_name_and_stem(tmp_path: Path) -> None:
    touch(tmp_path, "U.S.A..FLAC")
    root = os.fsencode(tmp_path)

    assert list(walker.walk(root, root, [])) == [Track(root, b"U.S.A..FLAC", b"U.S.A.")]


def test_a_folder_named_like_audio_is_walked(tmp_path: Path) -> None:
    touch(tmp_path, "album.flac/t.mp3")

    assert walk(tmp_path) == ([b"album.flac/t.mp3"], [])


def test_tracks_come_folder_by_folder_in_sorted_order(tmp_path: Path) -> None:
    touch(tmp_path, "b.flac", "a.flac", "z/2.mp3", "z/1.mp3", "c/t.ogg")

    tracks, _ = walk(tmp_path)

    assert tracks == [b"a.flac", b"b.flac", b"c/t.ogg", b"z/1.mp3", b"z/2.mp3"]


def test_symlinks_are_counted_and_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    touch(outside, "t.flac", "album/t.flac")
    root = tmp_path / "lib"
    root.mkdir()
    (root / "file.flac").symlink_to(outside / "t.flac")
    (root / "folder").symlink_to(outside / "album")
    (root / "dangling.flac").symlink_to(outside / "missing.flac")
    (root / "cover.jpg").symlink_to(outside / "t.flac")

    assert walk(root) == (
        [],
        [
            (b"dangling.flac", Problem.SYMLINK),
            (b"file.flac", Problem.SYMLINK),
            (b"folder", Problem.SYMLINK),
        ],
    )


def test_a_symlink_loop_ends(tmp_path: Path) -> None:
    touch(tmp_path, "album/t.flac")
    (tmp_path / "album" / "again").symlink_to(tmp_path)
    (tmp_path / "self").symlink_to(tmp_path / "self")

    tracks, problems = walk(tmp_path)

    assert tracks == [b"album/t.flac"]
    assert (b"album/again", Problem.SYMLINK) in problems


def test_an_excluded_symlink_is_not_counted(tmp_path: Path) -> None:
    (tmp_path / "link.flac").symlink_to("nowhere")

    assert walk(tmp_path, ["link.flac"]) == ([], [])


def test_a_symlink_takes_no_part_in_a_stem_collision(tmp_path: Path) -> None:
    touch(tmp_path, "song.flac")
    (tmp_path / "song.mp3").symlink_to("song.flac")

    assert walk(tmp_path) == ([b"song.flac"], [(b"song.mp3", Problem.SYMLINK)])


def test_a_fifo_named_like_audio_is_unreadable(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe.flac")
    os.mkfifo(tmp_path / "pipe.txt")

    assert walk(tmp_path) == ([], [(b"pipe.flac", Problem.UNREADABLE)])


def test_stem_collision_gives_no_track_and_reports_both(tmp_path: Path) -> None:
    touch(tmp_path, "song.flac", "song.mp3", "other.flac")

    assert walk(tmp_path) == (
        [b"other.flac"],
        [(b"song.flac", Problem.STEM_COLLISION), (b"song.mp3", Problem.STEM_COLLISION)],
    )


def test_three_files_on_one_stem_report_three(tmp_path: Path) -> None:
    touch(tmp_path, "song.flac", "song.FLAC", "song.opus")

    tracks, problems = walk(tmp_path)

    assert tracks == []
    assert {problem for _, problem in problems} == {Problem.STEM_COLLISION}
    assert len(problems) == 3


def test_stems_only_collide_inside_one_folder(tmp_path: Path) -> None:
    touch(tmp_path, "a/song.flac", "b/song.mp3")

    assert walk(tmp_path) == ([b"a/song.flac", b"b/song.mp3"], [])


def test_stems_differing_in_case_do_not_collide(tmp_path: Path) -> None:
    touch(tmp_path, "Song.flac", "song.mp3")

    assert walk(tmp_path)[1] == []


def test_a_sidecar_or_other_file_is_not_a_collision(tmp_path: Path) -> None:
    touch(tmp_path, "song.flac", "song.lrc", "song.txt", "song.jpg")

    assert walk(tmp_path) == ([b"song.flac"], [])


def test_an_exclude_settles_a_stem_collision(tmp_path: Path) -> None:
    touch(tmp_path, "song.flac", "song.mp3")

    assert walk(tmp_path, ["*.mp3"]) == ([b"song.flac"], [])


@NOT_ROOT
def test_a_folder_that_cant_be_listed_has_its_own_problem_and_the_walk_goes_on(
    tmp_path: Path,
) -> None:
    touch(tmp_path, "a/t.flac", "locked/t.flac", "z/t.flac")
    (tmp_path / "locked").chmod(0)
    try:
        assert walk(tmp_path) == (
            [b"a/t.flac", b"z/t.flac"],
            [(b"locked", Problem.FOLDER_UNREADABLE)],
        )
    finally:
        (tmp_path / "locked").chmod(0o755)


@NOT_ROOT
def test_files_in_a_folder_listed_but_not_traversable_each_report_a_failed_read(
    tmp_path: Path,
) -> None:
    touch(tmp_path, "album/a.flac", "album/b.flac")
    (tmp_path / "album").chmod(0o444)
    try:
        assert walk(tmp_path) == (
            [],
            [(b"album/a.flac", Problem.READ_FAILED), (b"album/b.flac", Problem.READ_FAILED)],
        )
    finally:
        (tmp_path / "album").chmod(0o755)


def test_a_failed_listing_is_the_folder_problem_on_a_stale_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stale(_path: bytes) -> None:
        raise OSError(errno.ESTALE, os.strerror(errno.ESTALE))

    monkeypatch.setattr(os, "scandir", stale)

    assert walk(tmp_path) == ([], [(b".", Problem.FOLDER_UNREADABLE)])


def test_a_start_that_is_gone_is_unreadable(tmp_path: Path) -> None:
    assert walk(tmp_path, start=tmp_path / "gone") == ([], [(b"gone", Problem.UNREADABLE)])


def test_a_folder_past_path_max_is_too_long_and_the_walk_goes_on(tmp_path: Path) -> None:
    touch(tmp_path, "z/t.flac")
    base = tmp_path / "deep"
    base.mkdir()
    plant_deep_track(base, PATH_MAX + 900)
    try:
        tracks, problems = walk(tmp_path)
    finally:
        shutil.rmtree(base)

    assert tracks == [b"z/t.flac"]
    assert [problem for _, problem in problems] == [Problem.TOO_LONG]


def test_a_file_past_path_max_is_too_long(tmp_path: Path) -> None:
    base = tmp_path / "deep"
    base.mkdir()
    # The folder can be listed, and only the name inside it goes over.
    plant_deep_track(base, PATH_MAX - 4, b"a-long-enough-name.flac")
    try:
        tracks, problems = walk(tmp_path)
    finally:
        shutil.rmtree(base)

    assert tracks == []
    assert [problem for _, problem in problems] == [Problem.TOO_LONG]


class _BrokenEntry:
    """A listing entry whose stat fails, as one does when the file goes away after listing."""

    def __init__(self, entry: os.DirEntry[bytes], error: OSError) -> None:
        self.name = entry.name
        self.path = entry.path
        self._error = error

    def is_symlink(self) -> bool:
        return False

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        raise self._error

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        raise self._error

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        raise self._error


class _Listing:
    def __init__(self, entries: list[object]) -> None:
        self._entries = entries

    def __enter__(self) -> Iterator[object]:
        return iter(self._entries)

    def __exit__(self, *_exc: object) -> None:
        pass


@pytest.mark.parametrize(
    ("error", "problem"),
    [
        (FileNotFoundError(2, "gone"), Problem.UNREADABLE),
        (NotADirectoryError(20, "folder replaced by a file"), Problem.UNREADABLE),
        (PermissionError(13, "refused"), Problem.READ_FAILED),
        (OSError(5, "input/output error"), Problem.READ_FAILED),
        (OSError(36, "name too long"), Problem.TOO_LONG),
    ],
)
def test_an_entry_that_fails_is_counted_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError, problem: Problem
) -> None:
    touch(tmp_path, "a.flac", "b.flac", "c.flac")
    real_scandir = os.scandir

    def scandir(path: bytes) -> _Listing:
        with real_scandir(path) as listing:
            return _Listing([_BrokenEntry(e, error) if e.name == b"b.flac" else e for e in listing])

    monkeypatch.setattr(os, "scandir", scandir)

    assert walk(tmp_path) == ([b"a.flac", b"c.flac"], [(b"b.flac", problem)])


def test_a_listing_that_fails_part_way_is_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Failing:
        def __enter__(self) -> "Failing":
            return self

        def __exit__(self, *_exc: object) -> None:
            pass

        def __iter__(self) -> Iterator[object]:
            raise OSError(5, "input/output error")

    touch(tmp_path, "t.flac")
    monkeypatch.setattr(os, "scandir", lambda _path: Failing())

    assert walk(tmp_path) == ([], [(b".", Problem.FOLDER_UNREADABLE)])


def test_a_skip_names_its_problem_by_a_counts_field() -> None:
    """The pipeline counts a Skip through this name, so each must be a field of Counts."""
    assert {problem.value for problem in Problem} <= set(Counts.__dataclass_fields__)
    assert isinstance(Skip(b"x", Problem.SYMLINK).problem, Problem)


def test_on_folder_is_called_once_for_each_folder_listed(tmp_path: Path) -> None:
    for folder in ("with-audio", "no-audio", "skipped/inner"):
        (tmp_path / folder).mkdir(parents=True)
    (tmp_path / "with-audio" / "1.flac").write_bytes(b"")
    (tmp_path / "with-audio" / "2.flac").write_bytes(b"")
    root = os.fsencode(tmp_path)
    seen: list[bytes] = []

    list(walker.walk(root, root, ["skipped"], seen.append))

    assert seen == [root, root + b"/no-audio", root + b"/with-audio"]


def test_on_folder_is_not_called_for_a_folder_it_could_not_list(tmp_path: Path) -> None:
    missing = os.fsencode(tmp_path / "missing")
    seen: list[bytes] = []

    found = list(walker.walk(os.fsencode(tmp_path), missing, [], seen.append))

    assert found == [walker.Skip(missing, walker.Problem.UNREADABLE)]
    assert seen == []
