"""Handle one Lidarr event: the folders it names, checked against the library, then the run.

The hook and the spool drain share this, so an album queued for later gets exactly the run it
would have had. cli is still the only module that turns a result into output and an exit code.
"""

import enum
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from plyrix import config, lidarr, pipeline, plex, refresh, spool
from plyrix.config import Settings
from plyrix.state import db

ROOT_NOT_A_FOLDER = "plyrix: music_root (PLYRIX_MUSIC_ROOT) is not a folder"
SPOOL_NOT_WRITABLE = (
    "plyrix: lidarr_spool (PLYRIX_LIDARR_SPOOL) can't be written, so a queued album would be lost"
)
LOCKED = "locked"
LRCLIB_UNAVAILABLE = "lrclib_unavailable"
BUDGET = "budget"
STATE = "state"
SPOOL = "spool"
NO_FOLDER = "no_folder"
NO_FOLDER_FOUND = (
    "none of the album's folders exist where plyrix sees the library, "
    "as lidarr_path_map (PLYRIX_LIDARR_PATH_MAP) may be wrong"
)
SPOOL_UNCHANGEABLE = (
    "plyrix: a file in lidarr_spool (PLYRIX_LIDARR_SPOOL) can't be removed or moved by this user"
)
SPOOL_UNREADABLE = "plyrix: lidarr_spool (PLYRIX_LIDARR_SPOOL) can't be listed by this user"
ROOT = "root"
ROOT_UNAVAILABLE = (
    "plyrix: music_root (PLYRIX_MUSIC_ROOT) is missing or empty, so check that it is mounted; "
    "the queued albums wait for the next drain"
)
_LOCKED_EXIT = 75


class Outcome(enum.Enum):
    """How handling an event ended."""

    DONE = "done"
    REFUSED = "refused"
    FAILED = "failed"
    # Worth trying again later: the album goes in the spool.
    RETRY = "retry"
    # The folders hold no audio, so there is nothing to run.
    EMPTY = "empty"


# The outcomes after which a queued file is finished with: removed, or set aside.
_FINISHED = (Outcome.DONE, Outcome.REFUSED, Outcome.EMPTY)


@dataclass(frozen=True)
class Result:
    """What handling an event did. Folders are those run, resolved; refused is a count."""

    outcome: Outcome
    counts: pipeline.Counts | None = None
    folders: tuple[bytes, ...] = ()
    refused: int = 0
    message: str | None = None
    # For RETRY: LOCKED, LRCLIB_UNAVAILABLE or BUDGET. For REFUSED: NO_FOLDER when no folder the
    # event names exists, which the hook queues, so a drain after the path map is fixed runs it.
    reason: str | None = None
    # The Plex refresh of the folders run, after DONE with Plex configured. A reply of None with
    # plex_left set means the ledger couldn't be opened for it, so it waits for plex-refresh.
    plex_reply: plex.Reply | None = None
    refreshed: refresh.RefreshCounts | None = None
    plex_left: bool = False


def handle(settings: Settings, event: lidarr.Event, *, deadline: float | None = None) -> Result:
    """Run the event's album folders that are inside music_root, in this process.

    A tripwire stop is a BaseException, so it passes through to the caller untouched.
    """
    folders = lidarr.album_folders(event, lidarr.parse_path_map(settings.lidarr_path_map))
    inside, refused = config.contain(settings, folders)
    if not inside:
        if not refused:
            # Nothing Lidarr named exists here: a wrong or missing path map, or no paths at all.
            return Result(Outcome.REFUSED, message=f"plyrix: {NO_FOLDER_FOUND}", reason=NO_FOLDER)
        return Result(Outcome.REFUSED, refused=len(refused))
    ran, skipped = tuple(inside), len(refused)
    try:
        counts = pipeline.run(settings, *inside, deadline=deadline)
    except pipeline.NoAudioStop as stop:
        return Result(Outcome.EMPTY, None, ran, skipped, str(stop))
    except pipeline.PreflightStop as stop:
        return Result(Outcome.RETRY, None, ran, skipped, str(stop), LRCLIB_UNAVAILABLE)
    except pipeline.UnavailableStop as stop:
        return Result(Outcome.RETRY, stop.counts, ran, skipped, str(stop), LRCLIB_UNAVAILABLE)
    except pipeline.BudgetStop as stop:
        return Result(Outcome.RETRY, stop.counts, ran, skipped, str(stop), BUDGET)
    except db.StateError as error:
        if error.exit_code == _LOCKED_EXIT:
            return Result(Outcome.RETRY, None, ran, skipped, error.message, LOCKED)
        return Result(Outcome.FAILED, None, ran, skipped, error.message)
    reply, refreshed, left = _refresh_plex(settings, ran, deadline)
    return Result(
        Outcome.DONE, counts, ran, skipped, plex_reply=reply, refreshed=refreshed, plex_left=left
    )


def _refresh_plex(
    settings: Settings, folders: tuple[bytes, ...], deadline: float | None
) -> tuple[plex.Reply | None, refresh.RefreshCounts | None, bool]:
    """Ask Plex to refresh only the albums in these folders, paced, within the deadline.

    Without Plex settings, or in a dry run, nothing is sent, and the new sidecars wait in the
    pending-refresh queue as every published sidecar does. Nothing here writes to stderr.
    """
    if settings.plex_url is None or settings.plex_section is None or settings.dry_run:
        return None, None, False
    if deadline is not None and time.monotonic() >= deadline:
        return None, None, False
    token = settings.plex_token.get_secret_value() if settings.plex_token else None
    try:
        conn = db.connect(settings.state_db)
    except db.StateError:
        return None, None, True
    try:
        reply, counts = refresh.drain(
            conn, plex.new_session(token), settings, under=folders, deadline=deadline
        )
    except db.StateError:
        return None, None, True
    finally:
        conn.close()
    return reply, counts, False


@dataclass
class DrainCounts:
    """What a drain of the spool did. stopped is why it ended early, with its message."""

    queued: int = 0
    drained: int = 0
    left: int = 0
    quarantined: int = 0
    failed_tracks: int = 0
    stopped: str | None = field(default=None, compare=False)
    message: str | None = field(default=None, compare=False)
    # The Plex refreshes of the drained albums, summed, and the first reply that wasn't OK.
    refreshed: refresh.RefreshCounts | None = field(default=None, compare=False)
    plex_reply: plex.Reply | None = field(default=None, compare=False)
    plex_left: bool = field(default=False, compare=False)


def drain_spool(
    settings: Settings,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> DrainCounts:
    """Run each queued album, oldest first, removing a file only once its run has finished.

    An empty spool returns at once, before anything opens the ledger or asks LRCLIB. A file that
    can't be run, because it isn't an import, is corrupt or unreadable, or names nothing inside the
    library, is moved to failed/ so it can't block the queue, and a user can move it back. A lock,
    an LRCLIB outage, the budget, an unusable ledger or a spool that can't be listed stops the
    drain and leaves the rest. A music_root that is missing or empty, as an unmounted one is,
    stops it before the first file, so a remount can't empty the queue into failed/. A dry run
    changes nothing in the spool.
    """
    folder = spool_folder(settings)
    try:
        paths = spool.entries(folder)
    except OSError:
        return DrainCounts(stopped=SPOOL, message=SPOOL_UNREADABLE)
    counts = DrainCounts(queued=len(paths))
    if paths and not _root_ready(settings):
        # An unmounted library looks like albums whose folders are gone; keep them all for later.
        counts.stopped, counts.message = ROOT, ROOT_UNAVAILABLE
        counts.left = counts.queued
        return counts
    for path in paths:
        if deadline is not None and clock() >= deadline:
            counts.stopped = BUDGET
            break
        try:
            event = _queued_event(path)
        except FileNotFoundError:
            # Another drain took it.
            counts.queued -= 1
            continue
        except OSError:
            # Unreadable, such as written by another uid: set aside, so it can't block the queue.
            event = None
        result = None if event is None else handle(settings, event, deadline=deadline)
        if result is not None and result.outcome not in _FINISHED:
            counts.stopped = result.reason if result.outcome is Outcome.RETRY else STATE
            counts.message = result.message
            break
        done = result is not None and result.outcome is Outcome.DONE
        # Only the spool's own operations: an error from the run itself isn't the spool's.
        try:
            if not done:
                _set_aside(settings, path, counts)
            elif not settings.dry_run:
                spool.remove(path)
        except OSError:
            counts.stopped, counts.message = SPOOL, SPOOL_UNCHANGEABLE
            break
        if done and result is not None:
            counts.drained += 1
            counts.failed_tracks += result.counts.failed() if result.counts else 0
            _add_refresh(counts, result)
    counts.left = counts.queued - counts.drained - counts.quarantined
    return counts


def _root_ready(settings: Settings) -> bool:
    """Whether music_root is a folder holding anything, as an unmounted one is missing or empty."""
    try:
        with os.scandir(settings.music_root) as listing:
            return next(listing, None) is not None
    except OSError:
        return False


def _add_refresh(counts: DrainCounts, result: Result) -> None:
    counts.plex_left = counts.plex_left or result.plex_left
    if result.plex_reply is not None and result.plex_reply is not plex.Reply.OK:
        counts.plex_reply = counts.plex_reply or result.plex_reply
    if result.refreshed is None:
        return
    if counts.refreshed is None:
        counts.refreshed = refresh.RefreshCounts()
    for name, value in vars(result.refreshed).items():
        setattr(counts.refreshed, name, getattr(counts.refreshed, name) + value)


def _queued_event(path: Path) -> lidarr.Event | None:
    """Return a queued import, or None for a file that isn't one. A missing file raises."""
    try:
        fields = spool.read(path)
    except spool.SpoolError:
        return None
    event = lidarr.parse(fields)
    if event is None or event.type != lidarr.ALBUM_DOWNLOAD:
        return None
    return event


def _set_aside(settings: Settings, path: Path, counts: DrainCounts) -> None:
    if settings.dry_run or spool.quarantine(path):
        counts.quarantined += 1
    else:
        # Another drain took it.
        counts.queued -= 1


def deadline(settings: Settings) -> float:
    """Return when the hook's time budget runs out, on the clock pipeline.run reads."""
    return time.monotonic() + settings.lidarr_budget


def spool_folder(settings: Settings) -> Path:
    """Return the spool folder, which load resolves, and settings made directly may leave out."""
    return settings.lidarr_spool or settings.state_db.parent / "lidarr-spool"


def check_setup(settings: Settings) -> str | None:
    """Return the one problem a Test event should show, or None when the setup works.

    It creates nothing. A spool folder not made yet is fine when the nearest folder above it that
    exists can be written, as the first queued album makes it.
    """
    if not os.path.isdir(settings.music_root):
        return ROOT_NOT_A_FOLDER
    folder = Path(os.path.abspath(spool_folder(settings)))
    while not os.path.lexists(folder) and folder != folder.parent:
        folder = folder.parent
    usable = os.path.isdir(folder) and os.access(folder, os.W_OK | os.X_OK)
    return None if usable else SPOOL_NOT_WRITABLE
