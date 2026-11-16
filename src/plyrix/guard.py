"""A runtime tripwire: abort any write-style operation under a library root.

It catches a mistake in plyrix's own Python code or in a Python dependency, at the moment it
happens. It is one layer of several. Native code and deliberately malicious code can bypass an
audit hook, as Python's documentation says, so this does not contain anything.

Known gaps: os.mkfifo and os.mknod raise no audit event, a name can't show that it is a hard link
to audio, and SQLite's ATTACH reaches a path the connect event doesn't name. tests/test_guard.py
states all three.
"""

import os
import re
import sys
from collections.abc import Iterable
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit


class GuardViolation(BaseException):
    """BaseException, so an `except Exception` around a tag read can't swallow it."""


_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
# Must match the temp name and suffixes that sidecars.py creates.
_TEMP = re.compile(rb"\.plyrix-[0-9a-f]{8}\.part")
_SIDECAR = (b".lrc", b".txt")
# Event name, then the index of its dir_fd argument, or None when it has none.
_PATH_EVENTS: dict[str, int | None] = {
    "os.truncate": None,
    "os.utime": 3,
    "os.chmod": 2,
    "os.chown": 3,
    "os.setxattr": None,
    "os.removexattr": None,
    "os.mkdir": 2,
    "os.rmdir": 1,
    "os.remove": 1,
}
_SPAWN = frozenset(
    {
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "os.fork",
        "os.forkpty",
        "pty.spawn",
        "ctypes.dlopen",
    }
)

_MEMORY = b":memory:"
_URI_PREFIX = b"file:"

_roots: tuple[bytes, ...] = ()
_read_only = False
# Sticky, so the CLI still fails a run when something swallowed the exception.
tripped: list[str] = []


def _resolve(path: Any, dir_fd: int | None = -1) -> list[bytes]:
    """Return the name in its real parent, for rename and remove, and the full real path."""
    if isinstance(path, int):
        return [os.fsencode(os.readlink(f"/proc/self/fd/{path}"))]
    name = os.fsencode(path)
    if dir_fd not in (-1, None) and not name.startswith(b"/"):
        name = os.path.join(os.fsencode(os.readlink(f"/proc/self/fd/{dir_fd}")), name)
    parent, base = os.path.split(os.path.abspath(name))
    return [os.path.join(os.path.realpath(parent), base), os.path.realpath(name)]


def _under(path: bytes) -> bool:
    return any(path == root or path.startswith(root.rstrip(b"/") + b"/") for root in _roots)


def _trip(event: str, why: str) -> None:
    tripped.append(event)
    raise GuardViolation(f"{event}: {why}")


def _allowed_name(paths: list[bytes], *, temp: bool = True, sidecar: bool = True) -> bool:
    for path in paths:
        if not _under(path):
            continue
        if _read_only:
            return False
        name = os.path.basename(path)
        is_temp = temp and _TEMP.fullmatch(name) is not None
        is_sidecar = sidecar and name.endswith(_SIDECAR)
        if not (is_temp or is_sidecar):
            return False
    return True


def _database_path(database: Any) -> bytes:
    """Return the path a database argument reaches, as a URI's own text isn't one.

    The event doesn't say whether uri=True was passed, so a plain path named file:x reads as a URI.
    """
    name = os.fsencode(database)
    if not name.startswith(_URI_PREFIX):
        return name
    return unquote_to_bytes(os.fsencode(urlsplit(os.fsdecode(name)).path))


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if event == "open":
        path, _mode, flags = args
        if path is None or not flags & _WRITE_FLAGS:
            return
        # The event leaves out dir_fd, so a relative path can't be resolved. Trip on all of them.
        if not isinstance(path, int) and not os.fsencode(path).startswith(b"/"):
            _trip(event, "relative write-mode open; plyrix writes only absolute paths")
        if not _allowed_name(_resolve(path)):
            _trip(event, "write-mode open")
    elif event in ("os.link", "os.rename"):
        source, destination, source_fd, destination_fd = args
        if not _allowed_name(_resolve(source, source_fd), sidecar=False) or not _allowed_name(
            _resolve(destination, destination_fd), temp=False
        ):
            _trip(event, "only a temp name may become a sidecar name")
    elif event == "os.symlink":
        if any(_under(path) for path in _resolve(args[1], args[2])):
            _trip(event, "symlink inside the library")
    elif event in _PATH_EVENTS:
        index = _PATH_EVENTS[event]
        paths = _resolve(args[0], -1 if index is None else args[index])
        if event == "os.remove":
            if not _allowed_name(paths):
                _trip(event, "only a temp or sidecar name may be removed")
        elif any(_under(path) for path in paths):
            _trip(event, "metadata or folder change inside the library")
    elif event == "sqlite3.connect":
        database = _database_path(args[0])
        if database != _MEMORY and any(_under(path) for path in _resolve(database)):
            _trip(event, "database inside the library")
    elif event in _SPAWN:
        _trip(event, "plyrix never spawns a process or loads native code at run time")


def install(
    roots: Iterable[str | bytes | os.PathLike[str] | os.PathLike[bytes]],
    read_only: bool = False,
) -> None:
    """Start the tripwire. Python has no way to remove it, so call this once per process.

    With read_only, no name under a root is allowed, sidecars and temps included.
    """
    global _read_only, _roots
    _read_only = read_only
    _roots = tuple(os.path.realpath(os.fsencode(root)) for root in roots)
    sys.addaudithook(_hook)
