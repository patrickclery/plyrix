"""Run a library through the steps. The only module that sees both sidecars and state."""

import hashlib
import json
import math
import os
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, replace

import requests

from plyrix import __version__, lrc, lrclib, matching, sidecars, tags, walker
from plyrix.config import Settings
from plyrix.domain import (
    MATCHER_VERSION,
    ORIGIN_PROVED,
    ORIGIN_TIMED,
    REPLACEABLE_ORIGINS,
    DumpBasis,
    Found,
    Kind,
    MissReason,
    Outcome,
    PlainReason,
    Reason,
    SidecarState,
    Step,
    SyncedReason,
    Track,
    TrackResult,
    TrackTags,
    affected_by_update,
    decide_existing,
    decide_found,
    lookup_artist,
    needs_lookup,
    release_year,
    show,
    stale_lookup_due,
    tags_fingerprint,
)
from plyrix.lrclib import Status
from plyrix.state import db

# Failed lookups with no successful search between them. A track settled by /api/get alone doesn't
# break the run of failures, or it would keep a run going through a whole index rebuild.
MAX_UNAVAILABLE_IN_A_ROW = 5
# A commit cost about 96 ms on the tested pool, so results are written in batches. A kill loses at
# most one batch, which costs only lookups.
FLUSH_EVERY = 200
# The most rows /api/search returns (SEARCH_RESULT_LIMIT on the pinned rev), with no paging.
_SEARCH_PAGE = 20
END_FINISHED = "finished"
END_LRCLIB_UNAVAILABLE = "lrclib_unavailable"
END_BUDGET = "budget"
_WRITTEN = "written"
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_NO_MATCH_PREFIX = "no_match_"
_PREFLIGHT_STOPS = {
    lrclib.Preflight.UNAVAILABLE: "LRCLIB is not answering at lrclib_url (PLYRIX_LRCLIB_URL)",
    lrclib.Preflight.INDEX_NOT_READY: "LRCLIB is unavailable or still rebuilding its search index",
    lrclib.Preflight.NO_TRACKS: "LRCLIB holds no tracks, so load a dump first",
}


@dataclass
class Counts:
    """What a run found and did. Each walked item adds one to one coverage or problem field.

    Actions say what this run did about a track, and reasons say why a no_match has no match, so
    the reasons add up to no_match. A re-run still reports coverage, with skipped_unchanged saying
    no work was done.
    """

    # Coverage
    synced: int = 0
    plain: int = 0
    plain_skipped: int = 0
    instrumental: int = 0
    no_match: int = 0
    blocked_foreign: int = 0
    untagged: int = 0
    # Problems
    unavailable: int = 0
    unreadable: int = 0
    read_failed: int = 0
    folder_unreadable: int = 0
    fs_no_atomic_publish: int = 0
    stem_collision: int = 0
    symlink: int = 0
    too_long: int = 0
    write_failed: int = 0
    # Actions
    written_lrc: int = 0
    written_txt: int = 0
    upgraded: int = 0
    adopted: int = 0
    lrc_stale: int = 0
    skipped_unchanged: int = 0
    looked_up: int = 0
    lrc_few_lines: int = 0
    lrc_low_coverage: int = 0
    # Reasons
    no_match_not_found: int = 0
    no_match_rejected: int = 0
    no_match_doubtful_artist: int = 0
    no_match_doubtful_title: int = 0
    no_match_doubtful_duration: int = 0
    no_match_doubtful_version: int = 0
    no_match_timing_rejected: int = 0

    def failed(self) -> int:
        """Return the tracks and folders whose work failed, which the next run tries again.

        A corrupt file counts as unreadable and isn't here, as it would then fail every run for
        good. A file that can't be read because of permissions or I/O can be fixed, so it is.
        """
        return (
            self.write_failed
            + self.fs_no_atomic_publish
            + self.unavailable
            + self.read_failed
            + self.folder_unreadable
        )


class NoAudioStop(Exception):
    """The library holds nothing to look at, which is how an unmounted one looks."""


class PreflightStop(Exception):
    """LRCLIB can't answer for a library right now, so the run ended before recording anything."""


class UnavailableStop(Exception):
    """LRCLIB stopped answering, so the run ended early with the counts so far."""

    def __init__(self, counts: Counts) -> None:
        super().__init__(
            f"plyrix: stopped after {MAX_UNAVAILABLE_IN_A_ROW} failed LRCLIB lookups with no "
            "successful search between them; those tracks were not recorded and the next run "
            "will try them again"
        )
        self.counts = counts


class BudgetStop(Exception):
    """The run's time ran out between tracks, so it ended early with the counts so far."""

    def __init__(self, counts: Counts) -> None:
        super().__init__(
            "plyrix: the time for this run ran out between tracks; the tracks done so far are "
            "recorded, and the rest wait for the next run"
        )
        self.counts = counts


class _Pacer:
    """Hold work to max_rate per second, with no burst after a pause. None means no limit."""

    def __init__(
        self,
        max_rate: float | None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = None if max_rate is None else 1 / max_rate
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        """Return when one interval has passed since the last slot, which this one then replaces."""
        if self._interval is None:
            return
        if self._last is not None:
            remaining = self._last + self._interval - self._clock()
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


@dataclass
class _Job:
    conn: db.Connection
    root: bytes
    run_id: int
    session: requests.Session
    base_url: str
    counts: Counts
    verbose: bool
    dump_id: str
    plain: str
    dry_run: bool
    pacer: _Pacer
    method: sidecars.Method
    unavailable_in_a_row: int = 0
    warned_no_dump_id: bool = False
    # The dumps LRCLIB served, for the retry rule after an update, and whether to retry all.
    bases: dict[str, DumpBasis] = field(default_factory=dict)
    full: bool = False
    pending: list[db.TrackRow] = field(default_factory=list)
    mtime_fills: list[tuple[int, bytes]] = field(default_factory=list)


@dataclass(frozen=True)
class _Audio:
    """An audio file as a result gets recorded against it."""

    path: bytes
    size: int
    mtime_ns: int
    tags_sha256: str | None
    # From tags read in this run, for the retry rule after a dump update. None keeps the stored.
    release_year: int | None = None
    artist_key: int | None = None
    title_key: int | None = None


def _with_tags(audio: _Audio, found: TrackTags) -> _Audio:
    """Return audio with the digest, year and lookup keys of tags just read."""
    artist_key, title_key = matching.lookup_keys(
        lookup_artist(found.artist, found.albumartist), found.title
    )
    return replace(
        audio,
        tags_sha256=tags_fingerprint(found),
        release_year=release_year(found.year),
        artist_key=artist_key,
        title_key=title_key,
    )


@dataclass(frozen=True)
class _ReadSidecar:
    """A sidecar's contents as they were read, for a decision. The text is never stored or shown."""

    text: str = field(repr=False)
    sha256: str
    size: int
    mtime_ns: int | None


@dataclass(frozen=True)
class _CheckedLrc:
    """A .lrc with no row to prove it, and the track whose duration it fits."""

    audio: _Audio
    sidecar: _ReadSidecar


def run(
    settings: Settings,
    *starts: bytes,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Counts:
    """Write a sidecar for each track under the starts that has lyrics, and return the counts.

    The starts are walked in turn in one run, with one ledger run recorded. A start equal to or
    inside another is dropped, so no track is walked twice. With a deadline, clock is read before
    each walked item, and once it reaches the deadline the run ends with BudgetStop. A track is
    never stopped part way, so the overrun is at most one track's work.

    A dry run does all of this but the writing. It reads tags and asks LRCLIB as usual, keeps its
    records in a memory copy of the ledger, and counts each write it would have made.

    The run's dump id is the dump recorded live, read once the ledger is open, else dump_id
    (D-07). It is kept in run_dump_id, so a report can name the same dump.
    """
    global run_dump_id
    run_dump_id = None
    if not starts:
        raise ValueError("pipeline.run needs at least one folder to start from")
    root = os.path.realpath(os.fsencode(settings.music_root))
    kept = _distinct_starts(starts)
    # One check for an empty, audio-free or wholly excluded library, stopping at the first audio
    # file. A Skip counts, so a folder that can't be listed stays a failed run. Only every start
    # holding nothing stops the run, so one empty folder among several costs nothing.
    has_audio = False
    for start in kept:
        if next(walker.walk(root, start, settings.excludes), None) is not None:
            has_audio = True
            break
    if not has_audio:
        raise NoAudioStop(
            "plyrix: found no audio files in the library to run on, so check that it is mounted "
            "and that excludes don't cover it"
        )
    session = lrclib.new_session()
    base_url = str(settings.lrclib_url)
    # Before the ledger opens, as a false miss under a dump id would never be looked up again.
    readiness = lrclib.preflight(session, base_url)
    if readiness is not lrclib.Preflight.READY:
        raise PreflightStop(f"plyrix: {_PREFLIGHT_STOPS[readiness]}; nothing was recorded")
    conn = (db.connect_snapshot if settings.dry_run else db.connect)(settings.state_db)
    try:
        dump_id = _run_dump_id(settings, conn)
        run_dump_id = dump_id
        run_id = db.begin_run(conn, __version__, dump_id)
        job = _Job(
            conn=conn,
            root=root,
            run_id=run_id,
            session=session,
            base_url=base_url,
            counts=Counts(),
            verbose=settings.verbose,
            dump_id=dump_id,
            plain=settings.plain,
            dry_run=settings.dry_run,
            pacer=_Pacer(settings.max_rate),
            method=sidecars.Method(settings.sidecar_method),
            bases=db.retry_bases(conn),
            full=settings.full,
        )
        on_folder = _leave_temps if settings.dry_run else _sweep_temps
        end_reason = None
        # What the walk reached, and what it reported, when it covers the whole library.
        walked: _Walked | None = None if settings.dry_run or root not in kept else _Walked()
        try:
            recover(conn, root, remove=not settings.dry_run)
            try:
                for start in kept:
                    for item in walker.walk(root, start, settings.excludes, on_folder):
                        if deadline is not None and clock() >= deadline:
                            end_reason = END_BUDGET
                            raise BudgetStop(job.counts)
                        if isinstance(item, walker.Skip):
                            if walked is not None:
                                walked.skipped.add(item.path)
                            _tally(job, item.problem.value, item.path)
                            continue
                        path = os.path.join(item.dir, item.name)
                        if walked is not None:
                            walked.reached.add(path)
                        _tally(job, _process(job, item), path)
                        if job.unavailable_in_a_row >= MAX_UNAVAILABLE_IN_A_ROW:
                            end_reason = END_LRCLIB_UNAVAILABLE
                            raise UnavailableStop(job.counts)
            finally:
                _warn_about_no_atomic_publish(job.counts)
            if walked is not None:
                _flush(job)
                _mark_unseen(job, walked)
            end_reason = END_FINISHED
            return job.counts
        finally:
            try:
                _flush(job)
            finally:
                db.finish_run(
                    conn, run_id, end_reason=end_reason, counts_json=json.dumps(asdict(job.counts))
                )
    finally:
        conn.close()


# The dump id the last run in this process recorded, or None when it stopped before its ledger
# opened. One run per process, so the CLI's report reads it rather than reading the ledger again.
run_dump_id: str | None = None


@dataclass
class _Walked:
    """The tracks a walk of the whole library reached, and each path it reported instead."""

    reached: set[bytes] = field(default_factory=set)
    skipped: set[bytes] = field(default_factory=set)


def _mark_unseen(job: _Job, walked: _Walked) -> None:
    """Mark the soft and stale rows this finished walk of the whole library didn't settle.

    A row this run judged under another dump was looked up or re-stamped, and so written. Any
    other row that names an older dump was not: its file was deleted, renamed or moved, excludes
    now cover it, it lies outside the current music root, or the walk reached it but stopped short
    of a write (a failed read, a lookup LRCLIB couldn't answer, a foreign sidecar). Without a mark
    its dump would keep every later change set from the prune for good, and a marked row counts as
    affected, so marking one costs at most one lookup. A row at or under a path the walk reported
    is left alone, as a folder that couldn't be listed may still hold it. A reached row that names
    this run's dump has its mark cleared. Only the newest dump the retry rule knows is named, so
    an older --dump-id can't move the prune back.
    """
    if not _is_newest(job, job.dump_id):
        return
    unseen: list[bytes] = []
    seen: list[bytes] = []
    for path, dump_id, run_id in db.pinning_rows(job.conn):
        if dump_id == job.dump_id:
            if path in walked.reached:
                seen.append(path)
        elif run_id != job.run_id and not (
            _within(path, job.root) and _under_any(path, walked.skipped, job.root)
        ):
            unseen.append(path)
    db.set_unseen(job.conn, unseen, seen, job.dump_id)


def _is_newest(job: _Job, dump_id: str) -> bool:
    """Whether dump_id is a dump the retry rule knows, and none it knows is later."""
    basis = job.bases.get(dump_id)
    return basis is not None and all(
        other.dump_time <= basis.dump_time for other in job.bases.values()
    )


def _under_any(path: bytes, folders: set[bytes], root: bytes) -> bool:
    """Whether path or a folder it is in, up to root, is one of folders."""
    while True:
        if path in folders:
            return True
        if path == root or not _within(path, root):
            return False
        parent = os.path.dirname(path)
        if parent == path:
            return False
        path = parent


# Set once a dump_id the live record overrides has been warned about, as a drain runs many times.
_warned_stale_dump_id = False


def _run_dump_id(settings: Settings, conn: db.Connection) -> str:
    """Return --dump-id when given, else the live dump's name, else dump_id, else "" (D-07).

    A --dump-id flag is an explicit override, so it wins. Otherwise the record wins, as
    lrclib-check reads it the same way: an update records the new dump live, and a dump_id left at
    the old name in the environment or config file must not pin runs to it, or no miss is looked
    up again. Such a dump_id gets one warning per process.
    """
    global _warned_stale_dump_id
    live = db.live_dump(conn)
    if settings.dump_id_from_flag and settings.dump_id:
        if live is not None and settings.dump_id != live.name:
            print(
                f"plyrix: using --dump-id {settings.dump_id} for this run, not {live.name}, the "
                f"dump recorded live; misses checked under {live.name} may be looked up again",
                file=sys.stderr,
            )
        return settings.dump_id
    if live is None:
        return settings.dump_id
    if settings.dump_id and settings.dump_id != live.name and not _warned_stale_dump_id:
        _warned_stale_dump_id = True
        print(
            f"plyrix: ignoring dump_id (PLYRIX_DUMP_ID or the config file), as it names another "
            f"dump than the one recorded live; using {live.name}. Remove the setting, or pass "
            f"--dump-id to override",
            file=sys.stderr,
        )
    return live.name


def _distinct_starts(starts: tuple[bytes, ...]) -> list[bytes]:
    """Return the resolved starts, sorted, without any that equals or is inside one kept."""
    kept: list[bytes] = []
    for start in sorted(os.path.realpath(start) for start in starts):
        if not any(_within(start, other) for other in kept):
            kept.append(start)
    return kept


def _within(path: bytes, folder: bytes) -> bool:
    """Whether path is folder or inside it, on a path-component boundary."""
    return path == folder or path.startswith(folder.rstrip(b"/") + b"/")


def recover(conn: db.Connection, root: bytes, remove: bool = True) -> None:
    """Resolve the intent rows a killed run left, before this run looks at any track.

    A matching hash means the write finished, so the row is finalised. A hash matching the file a
    replacement meant to take over means it never got there, so the row goes back to saying so,
    and the replacement can be tried again. An empty file on a row that recorded a claim is the
    fallback's claim, which is removed so the track is retried in this run. Anything else can't be
    proved plyrix's, so only the row goes and the file stays.

    A row outside this run's root stays, for a later run whose root covers it. So does a row whose
    file can't be read right now, and a row whose claim can't be removed right now, for a later
    run that can. With no row, that empty file would have no owner and block its track for good.

    The claim is recorded just before the file is made, as a record made after could be lost to a
    kill and strand plyrix's own claim. A failed step clears the record once no claim is left. So
    a file not plyrix's is taken for a claim only when the record outlives the claim, by a kill
    in that instant or by plyrix's own empty file going while the record stands, and another tool
    then makes an empty file at that exact name before the next run. Only a zero-byte file could
    be lost.

    A temp this ledger recorded was left by a run that has ended, as this run holds the ledger's
    lock. It holds lyric text and the walk may never come back to its folder, so it goes now,
    whatever its age. It stays recorded until it has gone. The hour's wait is for a temp with no
    record, which may belong to a run on another machine.

    A folder that is missing or empty says nothing about its files, as that is how an unmounted
    library looks: its root is an empty folder that nothing else can tell from a real one. So its
    rows and records stay until it holds something, or they would strand files that come back with
    the mount. Each folder is judged before anything is removed, as removing the last temp in a
    folder says nothing either. A folder really deleted leaves a row holding only a path, looked at
    on each run, which is a fair price for not losing ownership.

    With remove off, nothing in the library changes: the rows are settled the same way, for a
    connection that is only a copy. A claim then stays beside no row, so its track counts as blocked
    in that copy, where a real run would remove it and retry.
    """
    temps = db.temp_rows(conn)
    rows = db.intent_rows(conn)
    folders = {os.path.dirname(os.path.normpath(path)) for path in temps}
    folders |= {os.path.dirname(os.path.normpath(row.path)) for row in rows}
    can_tell = {folder for folder in folders if _inside(folder, root) and _holds_anything(folder)}
    for temp in temps:
        directory, name = os.path.split(os.path.normpath(temp))
        if directory in can_tell and (not remove or sidecars.remove_temp(directory, name)):
            db.drop_temp(conn, temp)
    for row in rows:
        directory, name = os.path.split(os.path.normpath(row.path))
        if directory not in can_tell:
            continue
        if remove:
            _sweep_temps(directory)
        track = _sidecar_track(directory, name, row.kind)
        try:
            digest = None if track is None else sidecars.read_sha256(track, row.kind)
        except OSError:
            continue
        if track is not None and digest == row.sha256:
            db.mark_published(conn, row.path)
            continue
        if track is not None and row.replaces_sha256 is not None and digest == row.replaces_sha256:
            db.restore_replaced(conn, row.path)
            continue
        if (
            remove
            and track is not None
            and row.claiming
            and digest == _EMPTY_SHA256
            and not sidecars.remove_owned(track, row.kind, _EMPTY_SHA256)
        ):
            continue
        db.delete_row(conn, row.path)


def _inside(directory: bytes, root: bytes) -> bool:
    return directory == root or directory.startswith(root.rstrip(b"/") + b"/")


def _holds_anything(directory: bytes) -> bool:
    """Say whether a folder can be listed now and has at least one entry."""
    try:
        with os.scandir(directory) as listing:
            return next(listing, None) is not None
    except OSError:
        return False


def _sweep_temps(directory: bytes) -> None:
    sidecars.sweep_temps(directory, time.time())


def _leave_temps(_directory: bytes) -> None:
    """Do what a dry run does with a folder's old temps: nothing."""


def _sidecar_track(directory: bytes, name: bytes, kind: Kind) -> Track | None:
    """Return a track naming a ledger row's sidecar, or None when the name isn't of that kind."""
    stem = name.removesuffix(kind.value)
    return None if not stem or stem == name else Track(directory, name, stem)


def _tally(job: _Job, count: str, path: bytes) -> None:
    """Count one walked item under its class, and name the path under --verbose.

    A no_match_ name is a reason, so it counts towards no_match as well.
    """
    _action(job, count)
    if count.startswith(_NO_MATCH_PREFIX):
        job.counts.no_match += 1
    if job.verbose:
        print(f"{count} {show(os.path.relpath(path, job.root))}")


def _action(job: _Job, name: str) -> None:
    """Add one to a field of Counts, and print nothing."""
    setattr(job.counts, name, getattr(job.counts, name) + 1)


def _count_for(outcome: Outcome, reason: Reason | None) -> str:
    """Name the count for a result, with its reason for a miss or a blocked .txt."""
    if reason is PlainReason.BLOCKED_FOREIGN:
        return "blocked_foreign"
    return f"{_NO_MATCH_PREFIX}{reason.value}" if isinstance(reason, MissReason) else outcome.value


def _process(job: _Job, track: Track) -> str:
    """Take one track as far as it goes, and return its coverage, problem or reason count."""
    existing = _existing_lrc_count(job, track)
    if existing is not None:
        return existing
    # A .txt never ends a track here, as it may not be lyrics, and a synced match beats it anyway.
    try:
        txt = _sidecar_state(job, track, Kind.TXT)
    except NotADirectoryError:
        return "unreadable"
    except OSError:
        return "read_failed"
    path = os.path.join(track.dir, track.name)
    audio_stat = _stat_audio(path)
    if isinstance(audio_stat, str):
        return audio_stat
    size, mtime_ns = audio_stat.st_size, audio_stat.st_mtime_ns

    settled = db.track_result(job.conn, path)
    audio = _Audio(path, size, mtime_ns, None)
    stat_unchanged = settled is not None and (settled.size, settled.mtime_ns) == (size, mtime_ns)
    found: TrackTags | None = None
    if settled is not None and stat_unchanged:
        tags_hash = settled.tags_sha256
    else:
        read = _read_tags(job, audio)
        if isinstance(read, str):
            return read
        found = read
        tags_hash = tags_fingerprint(found)
    affected = True
    if settled is not None and settled.tags_sha256 == tags_hash:
        affected = _affected(job, settled, audio_stat.st_ctime)
    if settled is not None and not needs_lookup(
        settled, tags_hash, job.dump_id, job.plain, txt=txt, affected=affected
    ):
        restamp = settled.is_soft and _restamps(job, settled)
        if not stat_unchanged or restamp:
            # What was found still stands. Only the stat moved, or the update since its dump
            # couldn't change it, which makes it this run's dump's answer too.
            _buffer(
                job,
                db.TrackRow(
                    path,
                    size,
                    mtime_ns,
                    settled.tags_sha256,
                    settled.outcome,
                    settled.reason,
                    job.dump_id if restamp else settled.dump_id,
                    settled.matcher_version,
                    settled.lrclib_id,
                    job.run_id,
                ),
            )
        _action(job, "skipped_unchanged")
        return _count_for(settled.outcome, settled.reason)
    if settled is not None and settled.is_soft and not job.dump_id:
        _warn_about_no_dump_id(job)
    if found is None:
        read = _read_tags(job, audio)
        if isinstance(read, str):
            return read
        found = read
    return _look_up(job, track, found, _with_tags(_Audio(path, size, mtime_ns, None), found), txt)


def _affected(job: _Job, settled: TrackResult, ctime: float | None) -> bool:
    """Say whether the dump update since a soft row's answer could change it (D-09).

    Only a soft row from another dump than this run's is asked about, and the diff is read only
    when no cheaper rule settles it.
    """
    if not (settled.is_soft or settled.reason is SyncedReason.LRC_STALE):
        return True
    if not job.dump_id or settled.dump_id == job.dump_id:
        return True
    basis = job.bases.get(settled.dump_id)
    if affected_by_update(settled, full=job.full, basis=basis, key_changed=False, ctime=ctime):
        return True
    return db.key_changed_since(job.conn, settled.dump_id, settled.artist_key, settled.title_key)


def _restamps(job: _Job, settled: TrackResult) -> bool:
    """Say whether an unaffected row should name this run's dump from now on.

    _affected found nothing in the update since the row's dump that could change it, so its answer
    is also this dump's: a year before the old dump's year is before this one's, a track seen
    before the old dump was seen before this one, and the diffs after this dump are a subset of
    those after the old one. Without that, a miss no diff touches would name its first dump for
    good, and the prune would keep every change set after it. Only a dump the retry rule knows
    is named, so the next run can still reason about the row.
    """
    if not job.dump_id or settled.dump_id == job.dump_id or job.dump_id not in job.bases:
        return False
    # An older --dump-id must not move a row back: the subset argument holds only going forward.
    old = job.bases.get(settled.dump_id)
    return old is None or job.bases[job.dump_id].dump_time >= old.dump_time


def _stat_audio(path: bytes) -> os.stat_result | str:
    """Return an audio file's stat, or the name of the count it ended in."""
    try:
        return os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "unreadable"
    except OSError:
        return "read_failed"


def _read_tags(job: _Job, audio: _Audio) -> TrackTags | str:
    """Return a file's tags, or the name of the count it ended in.

    A corrupt file gets a row, so an unchanged one isn't opened again. A file that can't be opened
    or has gone gets none, as that can be fixed without changing the file, such as by a remount.

    A tag read is the first work a track costs, and any lookup follows it, so a track waits here
    once, and a settled one that reads nothing doesn't wait at all.
    """
    job.pacer.wait()
    try:
        return tags.read(audio.path)
    except tags.Unreadable:
        _remember(job, audio, Outcome.UNREADABLE)
        return "unreadable"
    except tags.Gone:
        return "unreadable"
    except tags.AccessFailed:
        return "read_failed"


def _tags_of(path: bytes) -> TrackTags | str:
    """Return a file's tags, or the name of the count it ended in, and record nothing."""
    try:
        return tags.read(path)
    except (tags.Unreadable, tags.Gone):
        return "unreadable"
    except tags.AccessFailed:
        return "read_failed"


def _look_up(job: _Job, track: Track, found: TrackTags, audio: _Audio, txt: SidecarState) -> str:
    """Ask LRCLIB about one track, act on the answer, and return the count it ended in."""
    artist = lookup_artist(found.artist, found.albumartist)
    title = (found.title or "").strip()
    duration = found.duration
    if artist is None or not title or duration is None or not _is_usable_duration(duration):
        _remember(job, audio, Outcome.UNTAGGED)
        return "untagged"

    answered = _lookup(job, artist, title, found.album, duration)
    if answered is None:
        return "unavailable"
    verdict, rejected = answered
    found_kind = _found_in(verdict)
    proof = None
    if found_kind is Found.PLAIN and job.plain == "write":
        try:
            proof = _txt_proof(track, txt, verdict.plain)
        except OSError:
            return "read_failed"
    step, coverage = decide_found(found_kind, txt, job.plain, txt_matches=proof is not None)
    if verdict.synced is not None and verdict.synced.synced is not None:
        chosen = verdict.synced
        lyrics = verdict.synced.synced
        count = _publish(job, track, Kind.LRC, sidecars.encode(lyrics), chosen.id)
        if count != _WRITTEN:
            return count
        _record_written_lrc(job, audio, lyrics, duration, chosen.id)
        # Only now, so a failure at any step before this leaves the track its plain lyrics.
        if step is Step.PUBLISH_LRC_REMOVE_TXT:
            _remove_owned_txt(job, track)
        return coverage
    if verdict.plain:
        return _act_on_plain(job, track, audio, proof or verdict.plain[0], step, coverage)
    if verdict.instrumental is not None:
        _remember(job, audio, Outcome.INSTRUMENTAL, lrclib_id=verdict.instrumental.id)
        return coverage
    reason = MissReason.REJECTED if rejected else verdict.miss
    _remember(job, audio, Outcome.NO_MATCH, reason, verdict.miss_id)
    return _count_for(Outcome.NO_MATCH, reason)


def _record_written_lrc(
    job: _Job, audio: _Audio, lyrics: str, duration: float, lrclib_id: int | None
) -> None:
    _action(job, "written_lrc")
    for finding in lrc.quality(lyrics, duration):
        _action(job, f"lrc_{finding}")
    _remember(job, audio, Outcome.SYNCED, lrclib_id=lrclib_id)


def _found_in(verdict: matching.Verdict) -> Found:
    if verdict.synced is not None and verdict.synced.synced is not None:
        return Found.SYNCED
    if verdict.plain:
        return Found.PLAIN
    return Found.NOTHING if verdict.instrumental is None else Found.INSTRUMENTAL


def _txt_proof(
    track: Track, txt: SidecarState, candidates: tuple[matching.Candidate, ...]
) -> tuple[matching.Candidate, _ReadSidecar] | None:
    """Find the candidate whose plain text a .txt plyrix can't claim holds, and what it read.

    Both sides go through encode, so a newline or a BOM can't hide a match. Every candidate is
    tried, as a dump can hold several revisions. Raises OSError when the file can't be read now.
    """
    if txt not in (SidecarState.EDITED, SidecarState.FOREIGN):
        return None
    sidecar = _read_sidecar(track, Kind.TXT)
    if sidecar is None:
        return None
    held = sidecars.encode(sidecar.text)
    for candidate in candidates:
        if sidecars.encode(candidate.plain or "") == held:
            return candidate, sidecar
    return None


def _read_sidecar(track: Track, kind: Kind) -> _ReadSidecar | None:
    """Read a file plyrix can't claim, or return None when nothing there can prove anything.

    The hash is of the bytes that were judged. The stat comes first, as a file changing after it can
    then only cost another hash. Raises OSError when the file can't be read now.
    """
    seen = sidecars.file_stat(track, kind)
    text = sidecars.read_bounded(track, kind)
    if text is None:
        return None
    data = text.encode("utf-8")
    mtime_ns = seen[1] if seen is not None and seen[0] == len(data) else None
    return _ReadSidecar(text, hashlib.sha256(data).hexdigest(), len(data), mtime_ns)


def _act_on_plain(
    job: _Job,
    track: Track,
    audio: _Audio,
    answer: matching.Candidate | tuple[matching.Candidate, _ReadSidecar],
    step: Step,
    coverage: str,
) -> str:
    """Carry out what was decided for a plain result, and return the count it ended in.

    A blocked .txt is stored as a plain result with a reason, so the retry rule treats it like any
    other, and the track's next run comes to the same count without a lookup. A .txt found to hold
    a candidate's text becomes plyrix's own, with nothing written.
    """
    chosen, proved = answer if isinstance(answer, tuple) else (answer, None)
    if step is Step.ADOPT_TXT and proved is not None:
        db.adopt(
            job.conn,
            _sidecar_path(track, Kind.TXT),
            Kind.TXT,
            proved.sha256,
            proved.size,
            proved.mtime_ns,
            ORIGIN_PROVED,
            chosen.id,
            job.dump_id,
            job.run_id,
        )
        _action(job, "adopted")
    if step is Step.PUBLISH_TXT:
        # judge keeps only rows with text, so this always has some.
        count = _publish(job, track, Kind.TXT, sidecars.encode(chosen.plain or ""), chosen.id)
        if count != _WRITTEN:
            return count
        _action(job, "written_txt")
    if step is Step.BLOCK_TXT:
        _remember(job, audio, Outcome.PLAIN, PlainReason.BLOCKED_FOREIGN, chosen.id)
    elif coverage == Outcome.PLAIN_SKIPPED.value:
        _remember(job, audio, Outcome.PLAIN_SKIPPED, lrclib_id=chosen.id)
    else:
        _remember(job, audio, Outcome.PLAIN, lrclib_id=chosen.id)
    return coverage


def _lookup(
    job: _Job, artist: str, title: str, album: str | None, duration: float
) -> tuple[matching.Verdict, bool] | None:
    """Find the best row for a track, or return None when any step of asking failed.

    Every row found so far is judged together after each step, stopping at a valid synced one. A
    failed step throws away what the others found, as the search might have held a synced row, and
    a miss recorded now would stay settled under a set dump id.

    The flag says the server refused a step and no row was seen, which is a miss for that reason.

    Only a lookup that finished and reached a successful search says the server is well, so nothing
    else resets the count of failures.
    """
    wanted_album = (album or "").strip() or None
    records: list[dict[str, object]] = []
    rejected = searched = False
    verdict = matching.judge(records, artist, title, wanted_album, duration)
    for status, rows, is_search in _answers(job, artist, title, wanted_album, duration):
        if status is Status.UNAVAILABLE:
            job.unavailable_in_a_row += 1
            return None
        rejected = rejected or status is Status.REJECTED
        searched = searched or (is_search and status is Status.FOUND)
        records.extend(rows)
        verdict = matching.judge(records, artist, title, wanted_album, duration)
        if verdict.synced is not None:
            break
    if searched:
        job.unavailable_in_a_row = 0
    _action(job, "looked_up")
    return verdict, rejected and not records


def _answers(
    job: _Job, artist: str, title: str, album: str | None, duration: float
) -> Iterator[tuple[Status, list[dict[str, object]], bool]]:
    """Ask LRCLIB each step of the plan in turn, so a caller that stops also stops the requests.

    Each answer is a status, its rows, and whether it came from a search.
    """
    session, base = job.session, job.base_url
    for step_artist, step_title, step_album in matching.lookup_steps(artist, title, album):
        status, record = lrclib.get(session, base, step_artist, step_title, step_album, duration)
        yield status, [] if record is None else [record], False
    search_artist, search_title = matching.search_form(artist, title)
    status, rows = lrclib.search(session, base, search_artist, search_title)
    yield status, rows or [], True
    if album and rows is not None and len(rows) >= _SEARCH_PAGE:
        status, rows = lrclib.search(session, base, search_artist, search_title, album)
        yield status, rows or [], True


def _remember(
    job: _Job,
    audio: _Audio,
    outcome: Outcome,
    reason: Reason | None = None,
    lrclib_id: int | None = None,
) -> None:
    """Buffer a track's result. A failed lookup never gets here, as an error is never a miss."""
    _buffer(
        job,
        db.TrackRow(
            audio.path,
            audio.size,
            audio.mtime_ns,
            audio.tags_sha256,
            outcome,
            reason,
            job.dump_id,
            MATCHER_VERSION,
            lrclib_id,
            job.run_id,
            audio.release_year,
            audio.artist_key,
            audio.title_key,
        ),
    )


def _buffer(job: _Job, row: db.TrackRow) -> None:
    job.pending.append(row)
    _flush_if_full(job)


def _flush_if_full(job: _Job) -> None:
    if len(job.pending) + len(job.mtime_fills) >= FLUSH_EVERY:
        _flush(job)


def _flush(job: _Job) -> None:
    if job.pending or job.mtime_fills:
        db.write_tracks(job.conn, job.pending, job.mtime_fills)
        job.pending.clear()
        job.mtime_fills.clear()


def _existing_lrc_count(job: _Job, track: Track) -> str | None:
    """Name the count for a track whose .lrc name is taken, or None when nothing is there."""
    try:
        state = _sidecar_state(job, track, Kind.LRC)
    except NotADirectoryError:
        return "unreadable"
    except OSError:
        return "read_failed"
    if state is SidecarState.ABSENT:
        return None
    if state is SidecarState.OWNED:
        return _act_on_owned_lrc(job, track)
    return _act_on_unproved_lrc(job, track, state)


def _act_on_owned_lrc(job: _Job, track: Track) -> str:
    """Keep or recheck a .lrc the ledger holds, and return the count it ended in."""
    try:
        _finish_killed_upgrade(job, track)
    except OSError:
        return "read_failed"
    path = os.path.join(track.dir, track.name)
    result = db.track_result(job.conn, path)
    owned = db.owned_row(job.conn, _sidecar_path(track, Kind.LRC))
    audio_stat = _stat_audio(path)
    # A file that can't be looked at now says nothing about the .lrc, which stays as it is.
    seen = None if isinstance(audio_stat, str) else (audio_stat.st_size, audio_stat.st_mtime_ns)
    changed = result is not None and seen is not None and seen != (result.size, result.mtime_ns)
    ctime = None if isinstance(audio_stat, str) else audio_stat.st_ctime
    # A .lrc plyrix can't replace is never looked up again, so what the update holds is no matter.
    replaceable = owned is not None and owned.origin in REPLACEABLE_ORIGINS
    affected = result is None or not replaceable or _affected(job, result, ctime)
    due = owned is not None and stale_lookup_due(
        result, owned.origin, job.dump_id, affected=affected
    )
    step = decide_existing(SidecarState.OWNED, False, seen is not None and (changed or due))
    if step is Step.REVALIDATE and seen is not None and owned is not None:
        if due and not job.dump_id:
            _warn_about_no_dump_id(job)
        return _revalidate(job, track, owned, _Audio(path, seen[0], seen[1], None))
    _action(job, "skipped_unchanged")
    if result is not None and result.reason is SyncedReason.LRC_STALE:
        _action(job, "lrc_stale")
        # As for a miss: the update since its dump couldn't change it (_restamps). No rule reads
        # the dump of a .lrc plyrix can't replace, so it moves whatever the update holds, or it
        # would keep every change set after its first dump for good.
        if (
            seen is not None
            and not changed
            and (not affected or not replaceable)
            and _restamps(job, result)
        ):
            _buffer(
                job,
                db.TrackRow(
                    path,
                    seen[0],
                    seen[1],
                    result.tags_sha256,
                    result.outcome,
                    result.reason,
                    job.dump_id,
                    result.matcher_version,
                    result.lrclib_id,
                    job.run_id,
                ),
            )
    if seen is not None and (result is None or result.outcome is not Outcome.SYNCED):
        # A Phase 1 ledger or a kill's lost row then costs nothing to rebuild. The missing hash only
        # says the tags weren't read, and the sidecar's own row still says which dump gave the file.
        # A kill can leave the result from before the .lrc, which would say plain or no match.
        _remember(job, _Audio(path, seen[0], seen[1], None), Outcome.SYNCED)
    return Outcome.SYNCED.value


def _revalidate(job: _Job, track: Track, owned: db.OwnedRow, audio: _Audio) -> str:
    """Check a .lrc still suits its track, and return the count it ended in.

    Only the duration is checked, so a tag edit on a .lrc that still fits doesn't look it up again.
    A track that can't be judged now leaves the file and the row alone, so the next run asks again.
    """
    job.pacer.wait()
    found = _tags_of(audio.path)
    if isinstance(found, str):
        return found
    duration = found.duration
    if duration is None or not _is_usable_duration(duration):
        return Outcome.SYNCED.value
    audio = _with_tags(audio, found)
    try:
        text = sidecars.read_bounded(track, Kind.LRC)
    except OSError:
        return "read_failed"
    if text is not None and lrc.validate(text, duration) is None:
        _remember(job, audio, Outcome.SYNCED)
        return Outcome.SYNCED.value
    _action(job, "lrc_stale")
    if owned.origin in REPLACEABLE_ORIGINS:
        return _replace_stale(job, track, found, duration, audio, owned)
    return _keep_stale(job, audio)


def _keep_stale(job: _Job, audio: _Audio) -> str:
    """Keep a .lrc that no longer fits, reported until something can fix it."""
    _remember(job, audio, Outcome.SYNCED, SyncedReason.LRC_STALE)
    return Outcome.SYNCED.value


def _replace_stale(
    job: _Job, track: Track, found: TrackTags, duration: float, audio: _Audio, owned: db.OwnedRow
) -> str:
    """Look for a synced text that fits, and put it in place of plyrix's own .lrc if there is one.

    A failed lookup changes nothing, so the next run asks again.
    """
    artist = lookup_artist(found.artist, found.albumartist)
    title = (found.title or "").strip()
    if artist is None or not title:
        return _keep_stale(job, audio)
    answered = _lookup(job, artist, title, found.album, duration)
    if answered is None:
        return "unavailable"
    chosen = answered[0].synced
    if chosen is None or chosen.synced is None:
        return _keep_stale(job, audio)
    count = _replace(job, track, sidecars.encode(chosen.synced), chosen.id, owned)
    if count != _WRITTEN:
        return count
    _record_written_lrc(job, audio, chosen.synced, duration, chosen.id)
    return Outcome.SYNCED.value


def _act_on_unproved_lrc(job: _Job, track: Track, state: SidecarState) -> str:
    """Take a .lrc no row proves, or leave it and say so. Nothing in the library is written.

    With no row, the next run looks at a blocked file again, as it may have changed by then. An
    edited file's row stays until the file is valid, so restoring the bytes makes it owned again.
    """
    checked = None
    if state is not SidecarState.NOT_A_FILE:
        found = _check_lrc(job, track)
        if isinstance(found, str):
            return found
        checked = found
    step = decide_existing(state, checked is not None, False)
    if checked is None or step is Step.BLOCK_LRC:
        return "blocked_foreign"
    path = _sidecar_path(track, Kind.LRC)
    sidecar = checked.sidecar
    if step is Step.ADOPT_LRC:
        db.adopt(
            job.conn,
            path,
            Kind.LRC,
            sidecar.sha256,
            sidecar.size,
            sidecar.mtime_ns,
            ORIGIN_TIMED,
            None,
            job.dump_id,
            job.run_id,
        )
        _action(job, "adopted")
    else:
        db.set_origin(job.conn, path, ORIGIN_TIMED, sidecar.sha256, sidecar.size, sidecar.mtime_ns)
    _remember(job, checked.audio, Outcome.SYNCED)
    return Outcome.SYNCED.value


def _check_lrc(job: _Job, track: Track) -> _CheckedLrc | str | None:
    """Judge a .lrc against its track, returning None when it doesn't pass, or a problem's count.

    Timing is all a found .lrc can prove, and a file too large or undecodable proves nothing.
    """
    path = os.path.join(track.dir, track.name)
    audio_stat = _stat_audio(path)
    if isinstance(audio_stat, str):
        return audio_stat
    audio = _Audio(path, audio_stat.st_size, audio_stat.st_mtime_ns, None)
    found = _read_tags(job, audio)
    if isinstance(found, str):
        return found
    try:
        sidecar = _read_sidecar(track, Kind.LRC)
    except OSError:
        return "read_failed"
    duration = found.duration
    if (
        sidecar is None
        or duration is None
        or not _is_usable_duration(duration)
        or lrc.validate(sidecar.text, duration) is not None
    ):
        return None
    return _CheckedLrc(_with_tags(audio, found), sidecar)


def _finish_killed_upgrade(job: _Job, track: Track) -> None:
    """Finish an upgrade a kill interrupted, as its .lrc is published and nothing else will.

    Only a .txt plyrix wrote can be part of an upgrade, so a settled track costs one query and opens
    nothing. An adopted .txt stays beside the .lrc for good, so it isn't looked at, and nothing
    about it, such as its permissions or an edit, can fail the track. A row whose file has changed
    is an edit made since, which stays, as does the row.
    """
    path = _sidecar_path(track, Kind.TXT)
    owned = db.owned_row(job.conn, path)
    if owned is None or owned.origin not in REPLACEABLE_ORIGINS:
        return
    state = _sidecar_state(job, track, Kind.TXT)
    if state is SidecarState.OWNED:
        _remove_owned_txt(job, track)
    elif state is SidecarState.ABSENT:
        db.delete_row(job.conn, path)


def _remove_owned_txt(job: _Job, track: Track) -> None:
    """Remove a .txt plyrix wrote now a .lrc is published, and count the upgrade.

    The hash is checked at the moment of removal, as whatever said it was owned may be out of date.
    When it no longer matches, the file and its row both stay. A dry run counts it and leaves it.
    An adopted .txt stays beside the .lrc, as plyrix never wrote it.
    """
    path = _sidecar_path(track, Kind.TXT)
    owned = db.owned_row(job.conn, path)
    if (
        owned is not None
        and owned.origin in REPLACEABLE_ORIGINS
        and (job.dry_run or sidecars.remove_owned(track, Kind.TXT, owned.sha256))
    ):
        db.delete_row(job.conn, path)
        _action(job, "upgraded")


def _sidecar_state(job: _Job, track: Track, kind: Kind) -> SidecarState:
    """Say who a sidecar's name belongs to. Raises OSError when a file there can't be read now.

    A sidecar whose size and mtime equal its row counts as unchanged with no hash, so a re-run opens
    nothing. Only this says it's plyrix's, and whatever removes a sidecar checks its hash itself.
    A chmod moves neither, so permissions are asked about too, or a file no reader can open would
    count as covered. An I/O error on a file that still stats fine isn't seen until it is opened.
    The stat comes before the hash, so a file changing in between can only cost another hash.
    """
    path = _sidecar_path(track, kind)
    owned = db.owned_row(job.conn, path)
    seen = sidecars.file_stat(track, kind)
    if owned is not None and seen == (owned.size, owned.mtime_ns):
        if not sidecars.is_readable(track, kind):
            raise PermissionError("a sidecar can't be read")
        return SidecarState.OWNED
    digest = sidecars.read_sha256(track, kind)
    if digest is None:
        return SidecarState.ABSENT
    if not digest:
        return SidecarState.NOT_A_FILE
    if owned is None:
        return SidecarState.FOREIGN
    if owned.sha256 != digest:
        return SidecarState.EDITED
    if seen is not None:
        job.mtime_fills.append((seen[1], path))
        _flush_if_full(job)
    return SidecarState.OWNED


class _LedgerLog(sidecars.PublishLog):
    """Keep one sidecar's ledger row true as its publish goes on."""

    def __init__(self, conn: db.Connection, path: bytes) -> None:
        self._conn = conn
        self._path = path

    def temp_chosen(self, temp: bytes) -> None:
        db.add_temp(self._conn, temp)

    def temp_gone(self, temp: bytes) -> None:
        db.drop_temp(self._conn, temp)

    def claim_starting(self) -> None:
        db.mark_claiming(self._conn, self._path)

    def claim_gone(self) -> None:
        db.clear_claiming(self._conn, self._path)


def _publish(job: _Job, track: Track, kind: Kind, data: bytes, lrclib_id: int | None) -> str:
    """Publish a new sidecar, and return "written" or the problem's count."""
    return _write_sidecar(
        job,
        track,
        kind,
        data,
        lrclib_id,
        lambda log: sidecars.publish(track, kind, data, log, job.method),
    )


def _replace(
    job: _Job, track: Track, data: bytes, lrclib_id: int | None, owned: db.OwnedRow
) -> str:
    """Replace plyrix's own .lrc, and return "written" or the problem's count.

    The intent keeps the old hash too, so after a failure or a kill before the rename, recover can
    tell the old file is still there and give the row back, and the next run tries again. Any other
    file there stays untouched with no row. A refusal drops the row at once, as the file there is
    no longer one plyrix can prove its own.
    """
    return _write_sidecar(
        job,
        track,
        Kind.LRC,
        data,
        lrclib_id,
        lambda log: sidecars.replace_owned(track, Kind.LRC, data, owned.sha256, log),
        replacing=True,
    )


def _write_sidecar(
    job: _Job,
    track: Track,
    kind: Kind,
    data: bytes,
    lrclib_id: int | None,
    write: Callable[[sidecars.PublishLog], sidecars.Result],
    replacing: bool = False,
) -> str:
    """Carry out a write and keep its row true, and return "written" or the problem's count.

    A dry run says "written" and goes no further, so a publish or replace leaves the file and its
    row as they are. It can't foresee a rival or a filesystem refusing the real one.
    """
    if job.dry_run:
        return _WRITTEN
    conn = job.conn
    path = _sidecar_path(track, kind)
    digest = hashlib.sha256(data).hexdigest()
    db.add_intent(
        conn, path, kind, digest, len(data), job.run_id, lrclib_id, job.dump_id, replacing
    )
    try:
        result = write(_LedgerLog(conn, path))
    except OSError:
        # The sidecar may be complete by now, so the row stays for the next run's recover().
        return "write_failed"
    if result is sidecars.Result.WRITTEN:
        written = sidecars.file_stat(track, kind)
        # A different size means another writer got there, so its mtime says nothing about these.
        mtime_ns = written[1] if written is not None and written[0] == len(data) else None
        db.mark_published(conn, path, mtime_ns)
        return _WRITTEN
    if result is sidecars.Result.EXISTS:
        db.drop_intent(conn, path)
        return "blocked_foreign"
    # The rename was refused with the old file checked just before, so it is still plyrix's.
    db.restore_replaced(conn, path)
    db.drop_intent(conn, path)
    return "fs_no_atomic_publish"


def _warn_about_no_dump_id(job: _Job) -> None:
    if not job.warned_no_dump_id:
        job.warned_no_dump_id = True
        print(
            "plyrix: looking up earlier misses again, as no dump_id (PLYRIX_DUMP_ID) is set; set "
            "it to the loaded dump's filename to skip them until the dump or their tags change",
            file=sys.stderr,
        )


def _warn_about_no_atomic_publish(counts: Counts) -> None:
    if counts.fs_no_atomic_publish:
        print(
            f"plyrix: {counts.fs_no_atomic_publish} tracks got no sidecar, because this "
            "filesystem can't publish one atomically; nothing was overwritten",
            file=sys.stderr,
        )


def _is_usable_duration(duration: float) -> bool:
    return math.isfinite(duration) and duration > 0


def _sidecar_path(track: Track, kind: Kind) -> bytes:
    """Return a sidecar's ledger key. The walker only yields real folders, so this is real too."""
    return os.path.join(track.dir, track.stem + kind.value)
