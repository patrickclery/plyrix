"""Drain the ledger's refresh queue into album refreshes on Plex, and check what Plex attached."""

import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import requests

from plyrix import plex
from plyrix.config import Settings
from plyrix.domain import Kind
from plyrix.state import db

# Refreshes in a row Plex fails or doesn't answer before a drain stops. Each can take
# plex.TIMEOUT_SECONDS.
STOP_AFTER_UNANSWERED = 3


@dataclass
class RefreshCounts:
    """What one drain found and did. A folder counts once, and a sidecar once."""

    queued_sidecars: int = 0
    queued_folders: int = 0
    albums_to_refresh: int = 0
    refreshed: int = 0
    refresh_failed: int = 0
    cleared_folders: int = 0
    cleared_sidecars: int = 0
    not_in_plex: int = 0
    outside_root: int = 0
    left_by_limit: int = 0


def drain(
    conn: db.Connection,
    session: requests.Session,
    settings: Settings,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    announce: Callable[[int], None] | None = None,
    under: Sequence[bytes] | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[plex.Reply, RefreshCounts]:
    """Ask Plex to refresh each album holding a queued sidecar, and clear what it accepted.

    Each album is sent at most once, one per request with plex_pace after it, as Plex doesn't
    take a batch (D-04). A sidecar needs its own track's album, or every album in its folder if
    Plex lists no track of its name there, and its row is cleared as soon as all it needs was
    accepted, so an interrupted drain loses nothing (D-05). A folder Plex doesn't list, or one
    outside the music root, stays queued. With limit, at most that many refreshes are sent: a
    sidecar whose albums don't all fit in what is left is skipped and stays queued, and the
    next one is tried, so a big folder never starves the ones after it. With dry_run, nothing is
    sent or cleared, and the counts say what a real drain would send. announce gets the number
    of albums to refresh before the first is sent. The drain stops, leaving the rest queued, at
    the first refresh Plex refuses, which points at the token, and after STOP_AFTER_UNANSWERED
    unanswered in a row, so a dead server can't hold the ledger's lock for hours. Returns the
    listing's reply if it failed, else REFUSED or UNAVAILABLE if the drain stopped, else OK.

    With under, only sidecars in those folders or below them are drained, and the rest stay
    queued without being counted. With deadline, clock is read before the listing and before
    each refresh, and once it reaches the deadline nothing more is sent: each folder not cleared
    counts left_by_limit, and the reply is still OK, as running out of time says nothing about
    Plex.
    """
    counts = RefreshCounts()
    by_folder: dict[bytes, list[bytes]] = {}
    for path in db.pending_refresh(conn):
        folder = os.path.dirname(path)
        if under is None or any(_within(folder, scope) for scope in under):
            by_folder.setdefault(folder, []).append(path)
    counts.queued_sidecars = sum(len(paths) for paths in by_folder.values())
    counts.queued_folders = len(by_folder)
    if not by_folder:
        return plex.Reply.OK, counts
    if settings.plex_url is None or settings.plex_section is None:
        return plex.Reply.UNAVAILABLE, counts
    if deadline is not None and clock() >= deadline:
        # The listing pages through the whole section, so it waits for time to spare as well.
        counts.left_by_limit = len(by_folder)
        return plex.Reply.OK, counts
    base = str(settings.plex_url)
    reply, tracks = plex.list_tracks(session, base, settings.plex_section)
    if reply is not plex.Reply.OK:
        return reply, counts
    planned = _plan(by_folder, tracks, settings, counts)
    # Every album sent counts against the limit, accepted or not, in the order a drain sends
    # them, so this is exactly what a real drain sends.
    order: dict[str, None] = {}
    skipped: set[bytes] = set()
    for folder, needs in planned:
        for _, keys in needs:
            new = sorted(keys - order.keys())
            if limit is not None and len(order) + len(new) > limit:
                skipped.add(folder)
                continue
            order.update(dict.fromkeys(new))
    counts.albums_to_refresh = len(order)
    if dry_run:
        counts.left_by_limit = len(skipped)
        return plex.Reply.OK, counts

    if order and announce is not None:
        announce(len(order))
    accepted: dict[str, bool] = {}
    stopped: plex.Reply | None = None
    unanswered = 0
    timed_out = False
    for index, (folder, needs) in enumerate(planned):
        done: list[bytes] = []
        for path, keys in needs:
            for key in sorted(keys & order.keys()):
                if key in accepted:
                    continue
                if deadline is not None and clock() >= deadline:
                    timed_out = True
                    break
                answer = plex.refresh(session, base, key)
                accepted[key] = answer is plex.Reply.OK
                if accepted[key]:
                    counts.refreshed += 1
                else:
                    counts.refresh_failed += 1
                unanswered = unanswered + 1 if answer is plex.Reply.UNAVAILABLE else 0
                if answer is plex.Reply.REFUSED or unanswered >= STOP_AFTER_UNANSWERED:
                    stopped = answer
                    break
                sleep(settings.plex_pace)
            if stopped is not None or timed_out:
                break
            if all(accepted.get(key, False) for key in keys):
                done.append(path)
        if done:
            db.clear_refresh(conn, done)
            counts.cleared_sidecars += len(done)
        if stopped is not None:
            break
        if timed_out:
            # This folder, unless it was cleared before time ran out, and every one after it.
            cleared = len(done) == len(by_folder[folder])
            counts.cleared_folders += cleared
            counts.left_by_limit += len(planned) - index - 1 + (not cleared)
            break
        if len(done) == len(by_folder[folder]):
            counts.cleared_folders += 1
        elif folder in skipped and all(
            accepted.get(key, True) for _, keys in needs for key in keys
        ):
            counts.left_by_limit += 1
    return stopped or plex.Reply.OK, counts


def _within(folder: bytes, scope: bytes) -> bool:
    """Whether folder is scope or inside it, on a path-component boundary, in bytes."""
    return folder == scope or folder.startswith(scope.rstrip(b"/") + b"/")


def _plan(
    by_folder: dict[bytes, list[bytes]],
    tracks: list[plex.PlexTrack],
    settings: Settings,
    counts: RefreshCounts,
) -> list[tuple[bytes, list[tuple[bytes, set[str]]]]]:
    """Return each queued folder Plex lists, in path order, with what each sidecar in it needs.

    A sidecar needs the album of each track Plex lists at its folder and stem. If Plex lists
    none there, it needs every album with a file in the folder, so it is never cleared on a
    guess.
    """
    albums = plex.folder_albums(tracks)
    by_stem = plex.stem_albums(tracks)
    root = os.path.realpath(os.fsencode(settings.music_root))
    plex_root = os.fsencode(settings.plex_music_root or settings.music_root)
    planned: list[tuple[bytes, list[tuple[bytes, set[str]]]]] = []
    for folder in sorted(by_folder):
        view = plex_view(folder, root, plex_root)
        if view is None:
            counts.outside_root += 1
            continue
        keys = albums.get(view)
        if not keys:
            counts.not_in_plex += 1
            continue
        needs = []
        for path in by_folder[folder]:
            stem = os.path.splitext(os.path.basename(path))[0]
            needs.append((path, by_stem.get((view, stem)) or keys))
        planned.append((folder, needs))
    return planned


def plex_view(folder: bytes, root: bytes, plex_root: bytes) -> bytes | None:
    """Return a ledger folder as Plex sees it, or None if it isn't inside the music root.

    Bytes throughout, as a path that isn't UTF-8 must still match exactly.
    """
    if folder == root:
        return plex_root
    prefix = root.rstrip(b"/") + b"/"
    if not folder.startswith(prefix):
        return None
    return plex_root.rstrip(b"/") + b"/" + folder[len(prefix) :]


@dataclass
class CheckCounts:
    """What one plex-check found. Each sidecar checked counts once under one verdict.

    not_in_plex and unreadable are within the verdicts: a sidecar Plex doesn't list is also
    none, and one whose stream body couldn't say is also local_untimed.
    """

    checked: int = 0
    passed: int = 0
    failed: int = 0
    local_timed: int = 0
    local_untimed: int = 0
    provider: int = 0
    none: int = 0
    not_in_plex: int = 0
    unreadable: int = 0


def check(
    session: requests.Session, settings: Settings, targets: list[tuple[bytes, Kind]]
) -> tuple[plex.Reply, CheckCounts, list[tuple[bytes, plex.Verdict]]]:
    """Ask Plex what it attached for each sidecar, and judge it (D-10). Nothing is changed.

    The section's tracks are listed once, and each sidecar is found by its folder, as Plex sees
    it, and its stem. Only the chosen local stream's body is read, for its timed flag. Returns
    the reply that stopped the check if Plex failed, with what was checked before that.
    """
    counts = CheckCounts()
    verdicts: list[tuple[bytes, plex.Verdict]] = []
    if not targets:
        return plex.Reply.OK, counts, verdicts
    if settings.plex_url is None or settings.plex_section is None:
        return plex.Reply.UNAVAILABLE, counts, verdicts
    base = str(settings.plex_url)
    reply, tracks = plex.list_tracks(session, base, settings.plex_section)
    if reply is not plex.Reply.OK:
        return reply, counts, verdicts
    index = plex.track_index(tracks)
    root = os.path.realpath(os.fsencode(settings.music_root))
    plex_root = os.fsencode(settings.plex_music_root or settings.music_root)
    for path, kind in targets:
        folder, name = os.path.split(path)
        view = plex_view(folder, root, plex_root)
        key = None if view is None else index.get((view, name.removesuffix(kind.value)))
        streams: list[plex.LyricStream] = []
        if key is None:
            counts.not_in_plex += 1
        else:
            reply, streams = plex.lyric_streams(session, base, key)
            if reply is plex.Reply.MISSING:
                counts.not_in_plex += 1
            elif reply is not plex.Reply.OK:
                return reply, counts, verdicts
        timed = None
        chosen = plex.local_stream(kind, streams)
        if chosen is not None:
            reply, timed = plex.stream_timed(session, base, chosen.id)
            # A stream gone since it was listed can't say, like an unreadable body. Any other
            # failure is Plex's, not a verdict on the sidecar.
            if reply not in (plex.Reply.OK, plex.Reply.MISSING):
                return reply, counts, verdicts
            counts.unreadable += timed is None
        verdict = plex.verdict(kind, streams, timed)
        setattr(counts, verdict.value, getattr(counts, verdict.value) + 1)
        counts.checked += 1
        if plex.passes(kind, verdict):
            counts.passed += 1
        else:
            counts.failed += 1
        verdicts.append((path, verdict))
    return plex.Reply.OK, counts, verdicts
