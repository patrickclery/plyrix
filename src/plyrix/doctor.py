"""Checks before a run: the library, the state database's disk, LRCLIB, Plex and LRCLIB's data.

Nothing here writes or starts a process. Writability is os.access, the disk comes from the
kernel's mount table, read as text, and free space from os.statvfs. Messages name settings and
never their values, so a line can be pasted into an issue.
"""

import enum
import os
import re
from dataclasses import dataclass
from pathlib import Path

from plyrix import config, lrclib, plex

MOUNTINFO = "/proc/self/mountinfo"
# Measured on one install on 2026-11-11: a first load peaked at 319.8 GB apparent, with the
# 49.3 GB download kept until LRCLIB was ready and its search index built, rounded up here. On
# ZFS with lz4 the same peak took 129.6 GB on disk. DOWNLOAD_BYTES caps what a download already
# in dl counts for.
DOWNLOAD_BYTES = 49 * 10**9
FIRST_LOAD_BYTES = 320 * 10**9
FIRST_LOAD = (
    "about 320 GB (the peak measured on one install in 2026-11, with the download kept until "
    "LRCLIB is ready; a compressing filesystem such as ZFS with lz4 used 129.6 GB)"
)
# Room for the search index LRCLIB builds after an update. Arithmetic, over the 1.4 GB index
# measured on one install, not a measurement of this one.
INDEX_ALLOWANCE_BYTES = 2 * 10**9
UPDATE_NEED = (
    "the download left, plus the decompressed database from its header, plus 2 GB for the "
    "search index (measured at 1.4 GB on one install)"
)
# The setup script takes the same names, so a name can't reach outside dl.
DUMP_NAME = re.compile(r"lrclib-db-dump-[0-9]{8}T[0-9]{6}Z\.sqlite3\.gz")
_OCTAL = re.compile(rb"\\([0-7]{3})")
_LOCAL = frozenset({"btrfs", "ext2", "ext3", "ext4", "xfs", "zfs", "f2fs", "bcachefs", "jfs"})
_SHARED = frozenset(
    {"nfs", "nfs4", "cifs", "smb3", "smbfs", "ceph", "glusterfs", "9p", "afs", "lustre"}
)
_SECTION = "plex_section (PLYRIX_PLEX_SECTION)"


class Level(enum.Enum):
    """How a check came out. Only FAIL changes the exit code."""

    OK = "ok"
    WARN = "warning"
    FAIL = "problem"


@dataclass(frozen=True)
class Check:
    """One line of doctor's output, and the exit code it asks for: 0 unless it failed."""

    name: str
    level: Level
    message: str
    exit_code: int = 0


def worst(checks: list[Check]) -> int:
    """Return the exit code the checks add up to, where 2 beats 1 and 1 beats 0."""
    return max((check.exit_code for check in checks), default=0)


def read_mountinfo(path: str | os.PathLike[str] = MOUNTINFO) -> str:
    """Return the mount table's text, or nothing if it can't be read. Bytes survive as escapes."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return ""


def mount_of(path: bytes, mountinfo: str) -> tuple[bytes, str] | None:
    """Return the mount point a path is on and its filesystem type, or None if none matches.

    The path is resolved at its nearest existing ancestor, so a state database that doesn't exist
    yet is found where it will be. Device numbers aren't compared, as btrfs subvolumes report
    anonymous ones. A line this can't read is skipped.
    """
    real = _resolved(path)
    best: tuple[bytes, str] | None = None
    for line in mountinfo.splitlines():
        left, separator, right = line.partition(" - ")
        fields, types = left.split(), right.split()
        if not separator or len(fields) < 5 or not types:
            continue
        point = _OCTAL.sub(lambda m: bytes([int(m.group(1), 8)]), os.fsencode(fields[4]))
        if _inside(real, point) and (best is None or len(point) > len(best[0])):
            best = (point, types[0])
    return best


def filesystem_check(name: str, path: bytes, mountinfo: str, what: str | None = None) -> Check:
    """Say whether a path is on a local disk, where SQLite's locking can be trusted.

    what names the path in the message, and is the setting called name by default.
    """
    setting = what or _setting(name)
    found = mount_of(path, mountinfo)
    if found is None:
        return Check(name, Level.WARN, f"the filesystem under {setting} couldn't be identified")
    fstype = found[1]
    if fstype == "tmpfs":
        return Check(name, Level.WARN, f"{setting} is on tmpfs, so it won't survive a reboot")
    if fstype == "overlay":
        return Check(
            name,
            Level.WARN,
            f"{setting} is on overlay, a container layer that is lost when the container is "
            "removed; mount a volume",
        )
    if fstype in _SHARED:
        return Check(
            name,
            Level.WARN,
            f"{setting} is on {fstype}, a network filesystem, where SQLite's locking can fail",
        )
    if fstype.startswith("fuse"):
        return Check(
            name,
            Level.WARN,
            f"{setting} is on {fstype}, a FUSE filesystem, so its locking can't be confirmed",
        )
    if fstype in _LOCAL:
        return Check(name, Level.OK, f"{setting} is on a local {fstype} filesystem")
    return Check(
        name, Level.WARN, f"{setting} is on {fstype}, which plyrix doesn't know to be local"
    )


def space_check(
    directory: Path,
    dump: str | None = None,
    size: int | None = None,
    unpacked: int | None = None,
) -> Check:
    """Say whether a dump load fits, less what of the dump named dump is already in dl.

    Only that dump counts, whole or partial, as an older one kept in dl doesn't make room for
    it. With no name, nothing counts. With size and unpacked, from the listing and the dump's
    header, the need is exact (D-05): what is left of the download, plus the decompressed
    database, plus INDEX_ALLOWANCE_BYTES. The old database stays where it is through a swap and
    its index is renamed, not copied, so neither needs new room. Without them, it is the first
    load's arithmetic.
    """
    stats = os.statvfs(directory)
    free = stats.f_bavail * stats.f_frsize
    if size is not None and unpacked is not None:
        downloaded = min(_downloaded(directory / "dl", dump), size) if dump is not None else 0
        need = max(size - downloaded, 0) + unpacked + INDEX_ALLOWANCE_BYTES
        message = f"{_gb(free)} free; this update needs {_gb(need)}: {UPDATE_NEED}"
        if free < need:
            return Check("lrclib_data", Level.FAIL, f"not enough space: {message}", 1)
        return Check("lrclib_data", Level.OK, message)
    downloaded = _downloaded(directory / "dl", dump) if dump is not None else 0
    need = max(FIRST_LOAD_BYTES - downloaded, 0)
    already = f", less {_gb(downloaded)} already downloaded" if downloaded else ""
    message = f"{_gb(free)} free; a first dump load needs {FIRST_LOAD}{already}"
    if free < need:
        return Check("lrclib_data", Level.FAIL, f"not enough space: {message}", 1)
    return Check("lrclib_data", Level.OK, message)


def lrclib_data_checks(
    directory: Path,
    mountinfo: str,
    dump: str | None = None,
    size: int | None = None,
    unpacked: int | None = None,
) -> list[Check]:
    """Check LRCLIB's data folder before a dump is downloaded into it, changing nothing.

    Writability only warns, as a read-only view of the folder, such as a container's, can't speak
    for the user the setup script runs as, which checks it for itself.
    """
    if not os.path.isdir(directory):
        return [Check("lrclib_data", Level.FAIL, "the folder given to --lrclib-data is missing", 2)]
    if os.access(directory, os.W_OK):
        writable = Check(
            "lrclib_data", Level.OK, "the folder given to --lrclib-data exists and can be written"
        )
    else:
        writable = Check(
            "lrclib_data",
            Level.WARN,
            "the folder can't be written from here; the setup script checks it as its own user",
        )
    filesystem = filesystem_check("lrclib_data", os.fsencode(directory), mountinfo, "the folder")
    return [writable, filesystem, space_check(directory, dump, size, unpacked)]


def music_root_check(root: Path) -> Check:
    """Say whether the music root is a folder this user can create sidecars in."""
    setting = _setting("music_root")
    if not os.path.isdir(root):
        return Check("music_root", Level.FAIL, f"{setting} is not a folder", 2)
    if not os.access(root, os.W_OK):
        return Check("music_root", Level.FAIL, f"{setting} can't be written by this user", 2)
    return Check("music_root", Level.OK, f"{setting} is a folder this user can write in")


def lrclib_check(result: lrclib.Preflight) -> Check:
    """Map LRCLIB's preflight to a line. Anything but READY would stop a run with exit 1."""
    setting = _setting("lrclib_url")
    if result is lrclib.Preflight.READY:
        return Check("lrclib", Level.OK, f"LRCLIB at {setting} is ready and holds tracks")
    why = {
        lrclib.Preflight.INDEX_NOT_READY: "is still building its search index",
        lrclib.Preflight.NO_TRACKS: "answers, but has an empty database",
        lrclib.Preflight.UNAVAILABLE: "is down or not reachable",
    }[result]
    return Check("lrclib", Level.FAIL, f"LRCLIB at {setting} {why}", 1)


def plex_checks(
    reply: plex.Reply | None, info: plex.SectionInfo | None, plex_music_root: Path
) -> list[Check]:
    """Map what Plex said about the section to lines. reply is None when Plex isn't configured.

    A server that answers a GET may still refuse the token on a refresh, so the token is never
    called good here. Plex is optional for a run, so a server that doesn't answer only warns.
    """
    if reply is None:
        return [Check("plex", Level.OK, f"Plex isn't configured ({_setting('plex_url')} unset)")]
    if reply is plex.Reply.REFUSED:
        return [
            Check("plex", Level.WARN, f"Plex refused the request; check {_setting('plex_token')}")
        ]
    if reply is plex.Reply.UNAVAILABLE:
        return [Check("plex", Level.WARN, f"Plex didn't answer at {_setting('plex_url')}")]
    if reply is plex.Reply.MISSING or info is None:
        return [Check("plex", Level.FAIL, f"{_SECTION} isn't a section on this Plex server", 2)]
    if info.type != "artist":
        return [Check("plex", Level.FAIL, f"{_SECTION} isn't a music library", 2)]
    answered = Check(
        "plex", Level.OK, f"server answered; token not checked; {_SECTION} is a music library"
    )
    root = os.path.normpath(os.fsencode(plex_music_root))
    setting = _setting("plex_music_root")
    for location in info.locations:
        folder = os.path.normpath(location)
        if _inside(root, folder) or _inside(folder, root):
            return [answered, Check("plex_music_root", Level.OK, f"{setting} matches a folder")]
    return [
        answered,
        Check(
            "plex_music_root",
            Level.WARN,
            f"{setting} matches none of the section's folders, so every folder would count "
            "not_in_plex",
        ),
    ]


def checks_for(settings: config.Settings) -> list[Check]:
    """Run every check after the config's, in order: library, state database, LRCLIB, Plex."""
    checks = [
        music_root_check(settings.music_root),
        filesystem_check("state_db", os.fsencode(settings.state_db), read_mountinfo()),
        lrclib_check(lrclib.preflight(lrclib.new_session(), str(settings.lrclib_url))),
    ]
    if settings.plex_url is None:
        return checks + plex_checks(None, None, settings.music_root)
    if settings.plex_section is None:
        return [*checks, Check("plex", Level.FAIL, f"missing required settings: {_SECTION}", 2)]
    token = settings.plex_token.get_secret_value() if settings.plex_token else None
    reply, info = plex.section(
        plex.new_session(token), str(settings.plex_url), settings.plex_section
    )
    root = settings.plex_music_root or settings.music_root
    return checks + plex_checks(reply, info, root)


def _resolved(path: bytes) -> bytes:
    """Return a path's real form, resolved at its nearest existing ancestor."""
    absolute = os.path.abspath(path)
    head, tail = absolute, b""
    while not os.path.exists(head):
        parent, name = os.path.split(head)
        if parent == head:
            break
        head, tail = parent, os.path.join(name, tail) if tail else name
    real = os.path.realpath(head)
    return os.path.join(real, tail) if tail else real


def _inside(path: bytes, root: bytes) -> bool:
    return root == b"/" or path == root or path.startswith(root.rstrip(b"/") + b"/")


def _downloaded(folder: Path, dump: str) -> int:
    """Return how much of a dump is in folder, whole or partial, up to a dump's size."""
    sizes = [
        found.stat().st_size
        for found in (folder / dump, folder / f"{dump}.part")
        if found.is_file()
    ]
    return min(max(sizes, default=0), DOWNLOAD_BYTES)


def _gb(size: int) -> str:
    return f"{size / 10**9:.1f} GB"


def _setting(field: str) -> str:
    return f"{field} (PLYRIX_{field.upper()})"
