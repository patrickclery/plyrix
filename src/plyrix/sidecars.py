"""The only module that writes in a music library.

It takes a track and a sidecar kind, never a path, so a caller can't aim it at an audio file.
"""

import enum
import errno
import hashlib
import os
import re
import secrets
import stat

from plyrix.domain import Kind, Track
from plyrix.lrc import as_written

# os.link failed because this filesystem has no usable hard links, so claim the name instead.
# EXDEV is not here: os.replace would raise it too, so it ends the attempt.
_UNSUPPORTED = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EMLINK})
_READ_SIZE = 1 << 16
_TEMP_NAME = re.compile(rb"\.plyrix-[0-9a-f]{8}\.part")
_TEMP_MAX_AGE_SECONDS = 3600
_READ_LIMIT = 2 * 1024 * 1024
_NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


class Result(enum.Enum):
    """What happened to a publish."""

    WRITTEN = enum.auto()
    EXISTS = enum.auto()
    UNSUPPORTED = enum.auto()


class Method(enum.Enum):
    """How publish puts a sidecar at its name."""

    AUTO = "auto"
    PLAIN = "plain"


class PublishLog:
    """What publish tells its caller as it goes, so a record kept elsewhere stays true.

    The defaults record nothing. Any of these may raise, which ends the publish.
    """

    def temp_chosen(self, temp: bytes) -> None:
        """A temp holding the contents is about to be made at this absolute path."""

    def temp_gone(self, temp: bytes) -> None:
        """That temp is not there any more. If this isn't said, the temp is still there."""

    def claim_starting(self) -> None:
        """An empty file is about to hold the name. This is the only proof it is plyrix's."""

    def claim_gone(self) -> None:
        """This writer's claim is not at the name: it was never made, was removed or was taken."""


def encode(text: str) -> bytes:
    """Return sidecar bytes: UTF-8 with no BOM, LF line endings and no stray control characters."""
    return (as_written(text) + "\n").encode("utf-8", "replace")


_NO_LOG = PublishLog()


def publish(
    track: Track,
    kind: Kind,
    data: bytes,
    log: PublishLog = _NO_LOG,
    method: Method = Method.AUTO,
) -> Result:
    """Create the sidecar with complete contents, and never replace anything already there.

    AUTO uses a hard link, and claims the name only on a filesystem that has none. PLAIN always
    claims it, which leaves an empty sidecar if a kill lands between the claim and the rename.

    log hears about the temp, and about the claim, before each is made and once it is known not to
    be there. An OSError raised after claim_starting with no claim_gone means the claim may be
    there.
    """
    dest = _destination(track, kind)
    temp = _new_temp_path(track.dir)
    log.temp_chosen(temp)
    leftover: bytes | None = temp
    try:
        _write_temp(temp, data)
        result = None if method is Method.PLAIN else _link(temp, dest)
        if result is None:
            result = _claim_then_replace(temp, dest, log)
            if result is Result.WRITTEN:
                leftover = None
        if result is Result.WRITTEN:
            _fsync_directory(track.dir)
        return result
    finally:
        if leftover is None or _discard_temp(leftover):
            log.temp_gone(temp)


def remove_owned(track: Track, kind: Kind, expected_sha256: str) -> bool:
    """Remove a sidecar only when its hash matches the ledger's, and say whether it was removed."""
    if not _holds(track, kind, expected_sha256):
        return False
    try:
        os.unlink(_destination(track, kind))
    except OSError:
        return False
    return True


def replace_owned(
    track: Track, kind: Kind, data: bytes, expected_sha256: str, log: PublishLog = _NO_LOG
) -> Result:
    """Replace a sidecar only when its hash matches the ledger's. EXISTS means it was refused.

    A rival can still edit the file between the last check and os.replace. That is accepted,
    because only a file the ledger proves plyrix's own is ever at risk.

    log hears about the temp before it is made and once it is known not to be there, as publish's
    does, so a kill can't leave lyric text to wait for the hourly sweep.
    """
    if not _holds(track, kind, expected_sha256):
        return Result.EXISTS
    temp = _new_temp_path(track.dir)
    log.temp_chosen(temp)
    leftover: bytes | None = temp
    try:
        _write_temp(temp, data)
        if not _holds(track, kind, expected_sha256):
            return Result.EXISTS
        try:
            os.replace(temp, _destination(track, kind))
        except OSError as error:
            if error.errno == errno.EXDEV:
                return Result.UNSUPPORTED
            raise
        leftover = None
        _fsync_directory(track.dir)
        return Result.WRITTEN
    finally:
        if leftover is None or _discard_temp(leftover):
            log.temp_gone(temp)


def sweep_temps(dir: bytes, now: float) -> int:
    """Remove this writer's temp files older than an hour, and return how many were removed.

    A young temp may belong to a run on another machine, so it stays. A failure leaves the file
    for a later sweep, because a stray temp is hidden and harmless. A temp a dead run recorded in
    this ledger goes sooner, through remove_temp.
    """
    removed = 0
    try:
        with os.scandir(dir) as listing:
            names = [
                entry.name
                for entry in listing
                if _TEMP_NAME.fullmatch(entry.name)
                and entry.is_file(follow_symlinks=False)
                and now - entry.stat(follow_symlinks=False).st_mtime > _TEMP_MAX_AGE_SECONDS
            ]
    except OSError:
        return 0
    for name in names:
        try:
            os.unlink(os.path.join(dir, name))
        except OSError:
            continue
        removed += 1
    return removed


def remove_temp(directory: bytes, name: bytes) -> bool:
    """Remove a temp a ledger recorded, whatever its age, and say whether it can be forgotten.

    A name not of this writer's form, or anything but a regular file, is not its temp, so that
    stays and can be forgotten. False means the temp is still there, for a later attempt.
    """
    if not _TEMP_NAME.fullmatch(name):
        return True
    path = os.path.join(directory, name)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return True
        os.unlink(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def read_sha256(track: Track, kind: Kind) -> str | None:
    """Hash what is at a sidecar's name, without following a symlink or waiting on a FIFO.

    Returns None when nothing is there, and an empty string when something that is not a regular
    file is there, which never matches a ledger row. Raises OSError when a regular file can't be
    read now, since that says nothing about who wrote it.
    """
    dest = _destination(track, kind)
    try:
        if not stat.S_ISREG(os.lstat(dest).st_mode):
            return ""
        descriptor = os.open(dest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return ""
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, _READ_SIZE):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def read_bounded(track: Track, kind: Kind, limit: int = _READ_LIMIT) -> str | None:
    """Return the text at a sidecar's name, opened as read_sha256 does, reading limit + 1 at most.

    Returns None when there is no regular file there, when it is over the limit, and when it isn't
    UTF-8, as none of those can be proved anything. Raises OSError when a regular file can't be read
    now. The text only ever lives in the returned value, and a caller must not log or store it.
    """
    dest = _destination(track, kind)
    try:
        if not stat.S_ISREG(os.lstat(dest).st_mode):
            return None
        descriptor = os.open(dest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    chunks = []
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        remaining = limit + 1
        while remaining and (chunk := os.read(descriptor, min(_READ_SIZE, remaining))):
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    if remaining == 0:
        return None
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        return None


def file_stat(track: Track, kind: Kind) -> tuple[int, int] | None:
    """Return the size and mtime_ns of a regular file at a sidecar's name, opening nothing.

    None means anything else, including a name that can't be looked at now, which read_sha256 can
    then tell apart. Equal numbers only say a file looks unchanged, so nothing destructive may
    rely on them, and each destructive step here checks the hash itself.
    """
    try:
        found = os.lstat(_destination(track, kind))
    except OSError:
        return None
    return (found.st_size, found.st_mtime_ns) if stat.S_ISREG(found.st_mode) else None


def is_readable(track: Track, kind: Kind) -> bool:
    """Say whether permissions let this process read a sidecar now, opening nothing.

    Only permissions can show here, so an I/O error on a file that still stats fine stays unseen.
    """
    return os.access(_destination(track, kind), os.R_OK)


def _holds(track: Track, kind: Kind, expected_sha256: str) -> bool:
    try:
        digest = read_sha256(track, kind)
    except OSError:
        return False
    return bool(digest) and digest == expected_sha256


def _new_temp_path(directory: bytes) -> bytes:
    return os.path.join(directory, b".plyrix-" + secrets.token_hex(4).encode() + b".part")


def _write_temp(temp: bytes, data: bytes) -> None:
    """Write complete, flushed contents to a new hidden file. The caller removes it on failure."""
    descriptor = os.open(temp, _NEW_FILE, 0o666)
    try:
        remaining = memoryview(data)
        while remaining:
            remaining = remaining[os.write(descriptor, remaining) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _link(temp: bytes, dest: bytes) -> Result | None:
    """Give the temp its sidecar name, or return None when this filesystem has no hard links."""
    try:
        os.link(temp, dest, follow_symlinks=False)
    except FileExistsError:
        return Result.EXISTS
    except OSError as error:
        if error.errno == errno.EXDEV:
            return Result.UNSUPPORTED
        if error.errno not in _UNSUPPORTED:
            raise
        return None
    return Result.WRITTEN


def _claim_then_replace(temp: bytes, dest: bytes, log: PublishLog) -> Result:
    """Publish with no hard link: hold the name with an empty file, then rename over the claim.

    A kill or a failure between those two steps leaves an empty sidecar, which the next run
    removes because its ledger row records the claim. log.claim_gone keeps that record true when
    a step fails with no claim left, so no later empty file is taken for one. O_EXCL and O_NOFOLLOW
    mean this never opens anything already at the name.
    """
    log.claim_starting()
    try:
        descriptor = os.open(dest, _NEW_FILE, 0o666)
    except OSError as error:
        log.claim_gone()
        if isinstance(error, FileExistsError):
            return Result.EXISTS
        raise
    try:
        claim = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if not _is_claim(dest, claim):
        log.claim_gone()
        return Result.EXISTS
    try:
        os.replace(temp, dest)
    except OSError as error:
        if _is_claim(dest, claim):
            _unlink_if_present(dest)
        log.claim_gone()
        if error.errno == errno.EXDEV:
            return Result.UNSUPPORTED
        raise
    return Result.WRITTEN


def _is_claim(dest: bytes, claim: os.stat_result) -> bool:
    """Say whether the name still holds the empty file this writer created.

    Only a missing name means the claim is gone. Any other error raises, as the claim may be there.
    """
    try:
        current = os.lstat(dest)
    except FileNotFoundError:
        return False
    return (current.st_dev, current.st_ino, current.st_size) == (claim.st_dev, claim.st_ino, 0)


def _unlink_if_present(path: bytes) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _discard_temp(path: bytes | None) -> bool:
    """Remove a temp, without letting a failure replace the result of what came before it.

    Say whether it is gone. A later sweep removes one that is left, and it is hidden until then.
    """
    if path is None:
        return True
    try:
        os.unlink(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _destination(track: Track, kind: Kind) -> bytes:
    return os.path.join(track.dir, track.stem + kind.value)


def _fsync_directory(directory: bytes) -> None:
    """Ask for the new name to reach disk. Some filesystems refuse to fsync a directory.

    The sidecar is complete either way. If a crash then loses the name, the next run finds
    nothing there and writes it again, so a failure here must not fail the publish.
    """
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
