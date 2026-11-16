"""List the audio files in a library. This module never opens a file."""

import enum
import errno
import fnmatch
import os
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from plyrix.domain import AUDIO_SUFFIXES, Track


class Problem(enum.Enum):
    """Why something was reported instead of walked. Each value is a field of pipeline.Counts."""

    STEM_COLLISION = "stem_collision"
    SYMLINK = "symlink"
    TOO_LONG = "too_long"
    UNREADABLE = "unreadable"
    READ_FAILED = "read_failed"
    FOLDER_UNREADABLE = "folder_unreadable"


@dataclass(frozen=True)
class Skip:
    """Something in the library that gets no sidecar, and the reason."""

    path: bytes
    problem: Problem


def walk(
    root: bytes,
    start: bytes,
    excludes: Sequence[str],
    on_folder: Callable[[bytes], None] = lambda _folder: None,
) -> Iterator[Track | Skip]:
    """Yield each audio file under start, in sorted order, without following symlinks.

    start is the walk's top, and root is the library it belongs to. Excludes match the path from
    root, whatever start is, and a matching folder is never opened. An error costs only the entry
    it happened on, so one bad folder or file never stops a run. on_folder is called once for each
    folder listed, whether or not it holds any audio.
    """
    patterns = [os.fsencode(exclude).rstrip(b"/") for exclude in excludes]
    if _inside_an_excluded_folder(root, start, patterns):
        return
    pending = [start]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as listing:
                entries = sorted(listing, key=lambda entry: entry.name)
        except OSError as error:
            yield Skip(directory, _folder_problem(error))
            continue
        on_folder(directory)
        subdirs: list[bytes] = []
        audio: list[tuple[bytes, bytes]] = []
        for entry in entries:
            if _excluded(root, entry.path, patterns):
                continue
            stem, suffix = os.path.splitext(entry.name)
            is_audio = suffix.lower() in AUDIO_SUFFIXES
            try:
                if entry.is_symlink():
                    if is_audio or entry.is_dir():
                        yield Skip(entry.path, Problem.SYMLINK)
                elif entry.is_dir(follow_symlinks=False):
                    subdirs.append(entry.path)
                elif is_audio:
                    if entry.is_file(follow_symlinks=False):
                        # Listing doesn't touch the file. This shows a path past PATH_MAX here.
                        entry.stat(follow_symlinks=False)
                        audio.append((entry.name, stem))
                    else:
                        yield Skip(entry.path, Problem.UNREADABLE)
            except OSError as error:
                yield Skip(entry.path, _problem(error))
        stems = Counter(stem for _, stem in audio)
        for name, stem in audio:
            if stems[stem] > 1:
                yield Skip(os.path.join(directory, name), Problem.STEM_COLLISION)
            else:
                yield Track(directory, name, stem)
        pending.extend(reversed(subdirs))


def _problem(error: OSError) -> Problem:
    """Name why a file couldn't be looked at. One gone since the listing leaves nothing to do."""
    if error.errno in (errno.ENOENT, errno.ENOTDIR):
        return Problem.UNREADABLE
    return Problem.TOO_LONG if error.errno == errno.ENAMETOOLONG else Problem.READ_FAILED


def _folder_problem(error: OSError) -> Problem:
    """Name why a folder wasn't listed. One that has gone since it was seen holds nothing to do."""
    if error.errno in (errno.ENOENT, errno.ENOTDIR):
        return Problem.UNREADABLE
    return Problem.TOO_LONG if error.errno == errno.ENAMETOOLONG else Problem.FOLDER_UNREADABLE


def _excluded(root: bytes, path: bytes, patterns: Sequence[bytes]) -> bool:
    if not patterns:
        return False
    relative = os.path.relpath(path, root)
    return any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns)


def _inside_an_excluded_folder(root: bytes, start: bytes, patterns: Sequence[bytes]) -> bool:
    """Say whether start or a folder above it, up to root, is excluded."""
    path = start
    while path != root and path != b"/":
        if _excluded(root, path, patterns):
            return True
        path = os.path.dirname(path)
    return False
