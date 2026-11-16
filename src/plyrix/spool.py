"""A folder of Lidarr events the hook couldn't finish, one file per event, for a later drain.

It writes outside the library only. Config refuses a spool inside music_root, and the guard stops
anything that reaches there some other way. A file holds only the Lidarr_* values Lidarr passed,
which are paths and ids, never a tag or lyric text. It needs no lock, as the state database is
held by a run for hours, and each file is complete or absent.

A file is KEY=VALUE records, each ending in a NUL byte. The POSIX-sh shim writes the same format,
so a containerized Lidarr can queue an event with no Python.
"""

import errno
import os
import secrets
import stat
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

MAX_BYTES = 1024 * 1024
SUFFIX = ".event"
FAILED = "failed"
_RECORD_END = b"\0"
_EQUALS = b"="
_HIDDEN = "."
_MODE = 0o700
# Read-only, so a planted symlink isn't followed and a planted FIFO can't block the open. The
# boundary check lets spool's os.open take these flags and no others.
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class SpoolError(Exception):
    """A spool file that isn't in the format, or fields that can't be written in it."""


def now() -> float:
    """Return the time as a file's mtime states it, for comparing with one."""
    return datetime.now(UTC).timestamp()


def put(directory: Path, fields: Mapping[str, str]) -> Path:
    """Write the fields as one new file in directory, and return its path.

    The folder and any missing parent are made private, as the default sits beside a state
    database that a first run may not have made yet, and an album queued then must not be lost.
    The file is written under a dot-name a
    reader skips, opened exclusively so it can't follow a symlink, then renamed onto its name, so
    a reader sees all of it or none of it. Any failure removes the temp. Raises SpoolError for a
    field the format can't hold, and OSError.
    """
    data = _encode(fields)
    # pathlib applies mode only to the last folder of parents=True, so create them one by one.
    for folder in (*reversed(directory.parents), directory):
        folder.mkdir(mode=_MODE, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}-{secrets.token_hex(4)}{SUFFIX}"
    path = directory / name
    temp = directory / f"{_HIDDEN}{name}.tmp"
    # This raises before the temp is ours, so nothing above the try needs cleaning up.
    out = temp.open("xb")
    try:
        with out:
            out.write(data)
        temp.replace(path)
    except BaseException:
        _remove_quietly(temp)
        raise
    return path


def entries(directory: Path) -> list[Path]:
    """Return the event files in directory, oldest name first, skipping temps and symlinks."""
    try:
        with os.scandir(directory) as listing:
            names = [
                entry.name
                for entry in listing
                if not entry.name.startswith(_HIDDEN)
                and entry.name.endswith(SUFFIX)
                and entry.is_file(follow_symlinks=False)
            ]
    except FileNotFoundError:
        return []
    return [directory / name for name in sorted(names)]


def read(path: Path) -> dict[str, str]:
    """Return a file's fields, strictly. Raises SpoolError, or FileNotFoundError when it's gone.

    Lidarr's uid can write the spool, so the file can change after entries listed it. It is
    opened without following a symlink, must be a regular file, and is read only up to the cap,
    so a link to /dev/zero or a FIFO can't make the drain read without end or wait.
    """
    try:
        # Read-only flags; tests/test_boundaries.py allows this call with _READ_FLAGS and no other.
        descriptor = os.open(path, _READ_FLAGS)  # noqa: TID251
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise SpoolError("a spool file is a symlink") from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SpoolError("a spool file isn't a regular file")
        data = _read_at_most(descriptor, MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > MAX_BYTES:
        raise SpoolError("a spool file is larger than any event")
    records = data.split(_RECORD_END)
    if records[-1] != b"":
        raise SpoolError("a spool file doesn't end with a complete record")
    fields: dict[str, str] = {}
    for record in records[:-1]:
        key, equals, value = record.partition(_EQUALS)
        if not equals or not key:
            raise SpoolError("a spool file holds a record with no name")
        name = os.fsdecode(key)
        if name in fields:
            raise SpoolError("a spool file names a field twice")
        fields[name] = os.fsdecode(value)
    return fields


def remove(path: Path) -> None:
    """Remove a file a drain has finished with."""
    path.unlink(missing_ok=True)


def quarantine(path: Path) -> bool:
    """Move a file that can't be run into failed/, where a drain never looks.

    Returns False when the file is already gone, as another drain took it. Lidarr's uid can
    write the spool, so a failed/ that is a symlink, or anything but a folder, is refused with
    NotADirectoryError rather than followed.
    """
    failed = path.parent / FAILED
    failed.mkdir(mode=_MODE, exist_ok=True)
    if not stat.S_ISDIR(failed.lstat().st_mode):
        raise NotADirectoryError(errno.ENOTDIR, "failed/ in the spool isn't a plain folder")
    try:
        path.replace(failed / path.name)
    except FileNotFoundError:
        return False
    return True


def remove_older_than(directory: Path, cutoff: float) -> int:
    """Remove the event files last changed before cutoff, and return how many went."""
    removed = 0
    for path in entries(directory):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def _read_at_most(descriptor: int, limit: int) -> bytes:
    """Read until end of file or limit bytes, whichever comes first."""
    data = bytearray()
    while len(data) < limit:
        chunk = os.read(descriptor, limit - len(data))
        if not chunk:
            break
        data += chunk
    return bytes(data)


def _encode(fields: Mapping[str, str]) -> bytes:
    out = bytearray()
    for name, value in fields.items():
        try:
            key, data = os.fsencode(name), os.fsencode(value)
        except UnicodeEncodeError:
            raise SpoolError("a field holds text no file can") from None
        if not key or _EQUALS in key or _RECORD_END in key or _RECORD_END in data:
            raise SpoolError("a field can't be written as a spool record")
        out += key + _EQUALS + data + _RECORD_END
    return bytes(out)


def _remove_quietly(temp: Path) -> None:
    """Remove a temp, without letting a failure replace the error that got here."""
    try:
        temp.unlink(missing_ok=True)
    except OSError:
        pass
