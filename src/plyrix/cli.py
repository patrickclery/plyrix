"""Console entry point for plyrix."""

import dataclasses
import os
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from plyrix import (
    __version__,
    config,
    dbupdate,
    doctor,
    guard,
    hook,
    lidarr,
    pipeline,
    plex,
    refresh,
    report,
    spool,
)
from plyrix.domain import Kind, show
from plyrix.state import db

HELP = "plyrix: time-synced lyrics sidecars for a self-hosted music library."
NOTICE = "plyrix is pre-release; matching is unverified; run it only on a copied library."
DRY_RUN = "plyrix: this is a dry run, so nothing will be changed."
TRIPPED = "plyrix: the write tripwire stopped this run: "
UNEXPECTED = (
    "plyrix: the hook stopped on an unexpected error, so the next scheduled run will cover "
    "this album: "
)
UNEXPECTED_QUEUED = (
    "plyrix: the hook stopped on an unexpected error, so this album is queued for the next drain: "
)
UNEXPECTED_BACKLOG = (
    "plyrix: the hook stopped on an unexpected error while running albums queued earlier, "
    "which stay queued: "
)
NO_PLEX = (
    "plyrix: Plex is not configured: plex_url (PLYRIX_PLEX_URL) is not set, so nothing to refresh"
)
NOT_MUSIC = "plyrix: plex_section (PLYRIX_PLEX_SECTION) is not a music library on this Plex server"
PLEX_DOWN = "plyrix: Plex didn't answer at plex_url (PLYRIX_PLEX_URL), so nothing was refreshed"
PLEX_REFUSED = (
    "plyrix: Plex refused the token in plex_token (PLYRIX_PLEX_TOKEN), so nothing was refreshed"
)
REFRESH_REFUSED = "plyrix: Plex refused an album refresh, so check plex_token (PLYRIX_PLEX_TOKEN)"
REFRESH_STOPPED = (
    "plyrix: Plex stopped answering album refreshes at plex_url (PLYRIX_PLEX_URL), "
    "so this drain stopped early"
)
NO_PLEX_CHECK = (
    "plyrix: Plex is not configured: plex_url (PLYRIX_PLEX_URL) is not set, so nothing to check"
)
CHECK_DOWN = "plyrix: Plex didn't answer at plex_url (PLYRIX_PLEX_URL), so the check is incomplete"
CHECK_REFUSED = (
    "plyrix: Plex refused the token in plex_token (PLYRIX_PLEX_TOKEN), so the check is incomplete"
)
NOTHING_REFRESHED = (
    "plyrix: no sidecar has had its album refreshed yet, so run plyrix plex-refresh first"
)
NOTHING_PUBLISHED = "plyrix: no published sidecar is at the path to check"
END_PLEX_UNAVAILABLE = "plex_unavailable"
EXIT_UNFINISHED = 3
EXIT_TRIPPED = 70
# plyrix drain only: an error nothing expected, kept apart from 1, which means LRCLIB is down.
EXIT_UNEXPECTED = 4
REPORT_VERSION = 1
HOOK_TEST_OK = "plyrix: the Lidarr connection works: the settings load and music_root is a folder"
_QUEUE_REASONS = {
    hook.LOCKED: "another plyrix run is using the state database",
    hook.BUDGET: "the album took longer than lidarr_budget (PLYRIX_LIDARR_BUDGET)",
    hook.LRCLIB_UNAVAILABLE: "LRCLIB is unavailable at lrclib_url (PLYRIX_LRCLIB_URL)",
    hook.NO_FOLDER: hook.NO_FOLDER_FOUND,
    hook.STATE: "the state database can't be used",
}
NOT_A_LIDARR_EVENT = (
    "plyrix: lidarr-hook runs from Lidarr's Custom Script connection, "
    "and Lidarr_EventType (lidarr_eventtype) is not set"
)

app = typer.Typer(pretty_exceptions_enable=False, add_completion=False, help=HELP)


@app.callback()
def main() -> None:
    """Keep later commands as named subcommands."""


@app.command()
def run(
    # A str, because a Path would turn an empty argument into the working directory.
    path: Annotated[
        str | None,
        typer.Argument(
            metavar="[PATH]",
            help="Library folder: the music root if none is configured, else a folder inside it.",
        ),
    ] = None,
    config_file: Annotated[
        Path | None, typer.Option("--config", help="TOML config file to read.")
    ] = None,
    lrclib_url: Annotated[
        str | None, typer.Option("--lrclib-url", help="Base URL of your local LRCLIB.")
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option("--exclude", help="Path pattern to leave out, relative to the music root."),
    ] = None,
    state_db: Annotated[
        Path | None, typer.Option("--state-db", help="State database, outside the library.")
    ] = None,
    dump_id: Annotated[
        str | None,
        typer.Option(
            "--dump-id",
            help="Filename of the LRCLIB dump LRCLIB serves. Overrides the dump recorded live.",
        ),
    ] = None,
    plain: Annotated[
        str | None, typer.Option("--plain", help="Plain-only lyrics: write a .txt, or skip them.")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", help="Also print paths.")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Change nothing, and print what a run would count.")
    ] = False,
    full: Annotated[
        bool,
        typer.Option(
            "--full",
            help="After a dump update, look up every earlier miss and .txt again, whatever "
            "the diff says.",
        ),
    ] = False,
    # A str, so a value that isn't a number gets the one line every other bad setting gets.
    max_rate: Annotated[
        str | None,
        typer.Option("--max-rate", help="Most tracks per second to read or look up. No limit."),
    ] = None,
    sidecar_method: Annotated[
        str | None,
        typer.Option("--sidecar-method", help="How to create a sidecar: auto, or plain."),
    ] = None,
    # A str, because a Path would turn an empty argument into the working directory.
    report_file: Annotated[
        str | None,
        typer.Option("--report", help="JSON file of counts and run facts, outside the library."),
    ] = None,
    # A str, because a Path would turn an empty argument into the working directory.
    scope: Annotated[
        list[str] | None,
        typer.Option(
            "--path",
            metavar="DIR",
            help="Limit the run to this folder inside the music root. Repeatable.",
        ),
    ] = None,
) -> None:
    """Write .lrc and .txt lyrics sidecars beside the tracks in a copied library, then exit."""
    config.warn_unknown_env(os.environ)
    # Only flags the user set, so an unset flag can't hide a variable or the config file.
    flags = {
        "lrclib_url": lrclib_url,
        "excludes": exclude,
        "state_db": state_db,
        "dump_id": dump_id,
        "plain": plain,
        "max_rate": max_rate,
        "sidecar_method": sidecar_method,
        "report_file": report_file,
    }
    overrides: dict[str, object] = {k: v for k, v in flags.items() if v is not None}
    if verbose:
        overrides["verbose"] = True
    if dry_run:
        overrides["dry_run"] = True
    if full:
        overrides["full"] = True
    if path is not None and scope:
        _stop("plyrix: give the folders to run on as PATH or with --path, not both", 2)
    try:
        settings, start = config.Settings.load(path, overrides, config_file)
    except config.ConfigError as error:
        _stop(error.message, 2)
    starts = _scoped_starts(settings, scope) if scope else [start]
    for folder in starts:
        if not os.path.isdir(folder):
            _stop("plyrix: the library to run on is not a folder", 2)
    guard.install([settings.music_root], read_only=settings.dry_run)
    typer.echo(NOTICE, err=True)
    if settings.dry_run:
        typer.echo(DRY_RUN, err=True)
    started_at = report.now()
    # A finished run over the whole library covers every album queued before it started.
    covers_all = not scope and not settings.dry_run and start == os.fsencode(settings.music_root)
    cutoff = spool.now()

    try:
        counts = pipeline.run(settings, *starts)
    except guard.GuardViolation as violation:
        _stop(f"{TRIPPED}{violation}", EXIT_TRIPPED)
    except pipeline.NoAudioStop as stop:
        _stop_if_tripped()
        _stop(str(stop), 2)
    except pipeline.PreflightStop as stop:
        _stop_if_tripped()
        _report(settings, started_at, pipeline.END_LRCLIB_UNAVAILABLE, pipeline.Counts())
        _stop(str(stop), 1)
    except pipeline.UnavailableStop as stop:
        _print_counts(stop.counts)
        _stop_if_tripped()
        _report(settings, started_at, pipeline.END_LRCLIB_UNAVAILABLE, stop.counts)
        _stop(str(stop), 1)
    except db.StateError as error:
        _stop_if_tripped()
        _stop(error.message, error.exit_code)
    _print_counts(counts)
    _stop_if_tripped()
    if covers_all:
        _clear_covered(settings, cutoff)
    reported = _report(settings, started_at, pipeline.END_FINISHED, counts)
    # A timer or cron job only sees the exit code, so work that failed has to show there. A corrupt
    # file counts as unreadable and doesn't, as it would then fail every run for good. A file that
    # can't be read because of permissions or I/O can be fixed, so it does.
    failed = counts.failed()
    if failed:
        _stop(
            "plyrix: tracks where a write, a file read or an LRCLIB lookup failed, and folders "
            f"that could not be listed: {failed}; the next run will try them again",
            EXIT_UNFINISHED,
        )
    if not reported:
        raise typer.Exit(EXIT_UNFINISHED)


def _clear_covered(settings: config.Settings, cutoff: float) -> None:
    """Remove queued Lidarr albums older than this run, which has just covered them."""
    try:
        covered = spool.remove_older_than(hook.spool_folder(settings), cutoff)
    except OSError:
        # It costs only a drain that finds nothing to do.
        return
    if covered:
        typer.echo(f"plyrix: this run covered {covered} queued Lidarr albums")


def _scoped_starts(settings: config.Settings, scope: list[str]) -> list[bytes]:
    """Return the --path folders resolved, stopping the run if any is outside music_root."""
    inside, refused = config.contain(settings, scope)
    if refused:
        _stop("plyrix: the library path to run on must be inside music_root (PLYRIX_MUSIC_ROOT)", 2)
    return inside


@app.command("lidarr-hook")
def lidarr_hook(
    config_file: Annotated[
        Path | None, typer.Option("--config", help="TOML config file to read.")
    ] = None,
) -> None:
    """Run plyrix on the album Lidarr just imported. Made for Lidarr's Custom Script connection.

    Lidarr logs every stderr line as an error and waits for this to finish, so a run that works
    prints only on stdout, and every outcome of an import exits 0. Only the Test button's event
    can fail, so that Lidarr shows a setup that doesn't work.
    """
    progress = _HookProgress()
    try:
        _lidarr_hook(config_file, progress)
    except typer.Exit:
        raise
    except Exception as error:
        # A backstop: an error nothing expected must still not fail Lidarr's import (D-04). A
        # Test event may fail, so Lidarr shows it.
        event = lidarr.parse(os.environ)
        if event is None or event.type == lidarr.TEST:
            raise
        name = type(error).__name__
        if progress.backlog:
            # The hook's own album was finished with; the queued file that crashed stays queued.
            _hook_exit(f"{UNEXPECTED_BACKLOG}{name}")
        if progress.settings is not None and not progress.settled:
            if _backstop_queue(progress.settings):
                _hook_exit(f"{UNEXPECTED_QUEUED}{name}")
        _hook_exit(f"{UNEXPECTED}{name}")


@dataclasses.dataclass
class _HookProgress:
    """How far the hook got, so the backstop can say what an unexpected error interrupted.

    settled is set once nothing more is owed for the hook's own album: it ran, or was queued.
    """

    settings: config.Settings | None = None
    settled: bool = False
    backlog: bool = False


def _backstop_queue(settings: config.Settings) -> bool:
    """Queue the hook's album after an unexpected error, and say whether that worked.

    It must not fail itself, as it runs in the backstop. A dry run queues nothing.
    """
    if settings.dry_run:
        return False
    try:
        spool.put(hook.spool_folder(settings), lidarr.fields(os.environ))
    except Exception:
        return False
    return True


def _lidarr_hook(config_file: Path | None, progress: _HookProgress) -> None:
    config.warn_unknown_env(os.environ)
    event = lidarr.parse(os.environ)
    if event is None:
        _stop(NOT_A_LIDARR_EVENT, 2)
    if event.type == lidarr.TEST:
        _hook_test(config_file)
    if event.type != lidarr.ALBUM_DOWNLOAD:
        # Rename and the rest are left to the next scheduled run.
        _hook_exit()
    try:
        settings, _ = config.Settings.load(None, {}, config_file)
    except config.ConfigError as error:
        _hook_exit(error.message)
    progress.settings = settings
    budget_end = hook.deadline(settings)
    guard.install([settings.music_root], read_only=settings.dry_run)
    # stdout, as Lidarr logs stderr as an error.
    typer.echo(_hook_heading(event))
    typer.echo(NOTICE)
    try:
        result = hook.handle(settings, event, deadline=budget_end)
    except guard.GuardViolation as violation:
        _hook_exit(f"{TRIPPED}{violation}")
    # From here an unexpected error re-runs the album at worst, or, on a queue, may lose it.
    progress.settled = result.outcome in (hook.Outcome.DONE, hook.Outcome.EMPTY) or (
        result.outcome is hook.Outcome.REFUSED and result.reason != hook.NO_FOLDER
    )
    if result.refused:
        typer.echo(_refused_line(result.refused), err=True)
    if result.counts is not None:
        _print_nonzero(result.counts)
    if result.outcome is hook.Outcome.RETRY or result.reason == hook.NO_FOLDER:
        _queue(settings, result.reason)
    if result.outcome is hook.Outcome.FAILED:
        # A ledger problem, such as a full disk, may pass, so the album waits as a drain's would.
        if result.message:
            typer.echo(result.message, err=True)
        _queue(settings, hook.STATE)
    if result.outcome is hook.Outcome.EMPTY:
        _hook_exit(result.message)
    if result.outcome is hook.Outcome.DONE and result.counts is not None:
        failed = result.counts.failed()
        if failed:
            typer.echo(
                "plyrix: tracks where a write, a file read or an LRCLIB lookup failed, and "
                f"folders that could not be listed: {failed}; the next run will try them again",
                err=True,
            )
    if result.outcome is hook.Outcome.DONE:
        _report_plex(result.refreshed, result.plex_reply, result.plex_left)
        progress.backlog = True
        _drain_backlog(settings, budget_end)
    _hook_exit()


def _report_plex(
    counts: refresh.RefreshCounts | None, reply: plex.Reply | None, left: bool
) -> None:
    """Print the hook's Plex refresh: counts on stdout, and one stderr line if Plex failed."""
    if counts is not None:
        for name, value in dataclasses.asdict(counts).items():
            if value:
                typer.echo(f"{name}={value}")
    if left:
        typer.echo(
            "plyrix: the state database is busy, so the Plex refresh is left for plex-refresh"
        )
    if reply is plex.Reply.REFUSED:
        typer.echo(
            "plyrix: Plex refused the token in plex_token (PLYRIX_PLEX_TOKEN), so the album "
            "stays queued for plex-refresh",
            err=True,
        )
    elif reply is not None and reply is not plex.Reply.OK:
        typer.echo(
            "plyrix: Plex didn't answer at plex_url (PLYRIX_PLEX_URL), so the album stays "
            "queued for plex-refresh",
            err=True,
        )


def _drain_backlog(settings: config.Settings, budget_end: float) -> None:
    """Run albums queued earlier, within what is left of this hook's budget."""
    try:
        backlog = hook.drain_spool(settings, deadline=budget_end)
    except guard.GuardViolation as violation:
        _hook_exit(f"{TRIPPED}{violation}")
    if backlog.queued:
        _print_drain(backlog)
        _report_plex(backlog.refreshed, backlog.plex_reply, backlog.plex_left)
        if backlog.quarantined:
            typer.echo(_quarantined_line(backlog.quarantined), err=True)
    if backlog.stopped in (hook.STATE, hook.SPOOL, hook.ROOT) and backlog.message:
        typer.echo(backlog.message, err=True)


def _print_drain(counts: hook.DrainCounts) -> None:
    for name in ("queued", "drained", "left", "quarantined", "failed_tracks"):
        typer.echo(f"{name}={getattr(counts, name)}")


def _quarantined_line(count: int) -> str:
    return (
        f"plyrix: queued Lidarr albums that can't be run, moved to failed/ in lidarr_spool "
        f"(PLYRIX_LIDARR_SPOOL): {count}"
    )


def _queue(settings: config.Settings, reason: str | None) -> NoReturn:
    """Put Lidarr's own fields in the spool for the next drain, say why, and exit 0.

    They are queued as Lidarr gave them, under the mixed-case names, so the drain applies
    whatever lidarr_path_map is then. A
    lock or the budget is a normal day, so its line goes on stdout. A dead LRCLIB is worth an
    error line in Lidarr's log.
    """
    why = _QUEUE_REASONS.get(reason or "", _QUEUE_REASONS[hook.LRCLIB_UNAVAILABLE])
    to_stderr = reason not in (hook.LOCKED, hook.BUDGET)
    if settings.dry_run:
        typer.echo(f"plyrix: {why}, and this is a dry run, so it was not queued", err=to_stderr)
        _hook_exit()
    try:
        spool.put(hook.spool_folder(settings), lidarr.fields(os.environ))
    except (OSError, spool.SpoolError):
        _hook_exit(
            f"plyrix: {why}, and the album couldn't be queued in lidarr_spool "
            "(PLYRIX_LIDARR_SPOOL), so the next scheduled run will cover it"
        )
    typer.echo(f"plyrix: {why}, so this album is queued for the next drain", err=to_stderr)
    _hook_exit()


def _hook_test(config_file: Path | None) -> NoReturn:
    """Check the setup for Lidarr's Test button, touching no ledger, LRCLIB or library file."""
    try:
        settings, _ = config.Settings.load(None, {}, config_file)
    except config.ConfigError as error:
        _stop(error.message, 1)
    problem = hook.check_setup(settings)
    if problem is not None:
        _stop(problem, 1)
    typer.echo(HOOK_TEST_OK)
    raise typer.Exit(0)


def _hook_heading(event: lidarr.Event) -> str:
    """Return the hook's first line, with Lidarr's album id only when it is a plain number."""
    kind = "upgrade" if event.is_upgrade else "import"
    album_id = event.album_id
    if album_id.isascii() and album_id.isdigit():
        return f"plyrix: Lidarr {kind} of album {album_id}"
    return f"plyrix: Lidarr {kind} of an album"


def _refused_line(count: int) -> str:
    if count == 1:
        return (
            "plyrix: 1 folder Lidarr imported is outside music_root (PLYRIX_MUSIC_ROOT), "
            "so it was left alone"
        )
    return (
        f"plyrix: {count} folders Lidarr imported are outside music_root (PLYRIX_MUSIC_ROOT), "
        "so they were left alone"
    )


def _print_nonzero(counts: pipeline.Counts) -> None:
    for name, value in dataclasses.asdict(counts).items():
        if value:
            typer.echo(f"{name}={value}")


def _hook_exit(message: str | None = None) -> NoReturn:
    """End the hook with exit 0, as a failure must never fail Lidarr's import (D-04).

    The message, and a tripwire stop something swallowed, go on stderr, which Lidarr logs as an
    error.
    """
    if message is not None:
        typer.echo(message, err=True)
    if guard.tripped and not (message or "").startswith(TRIPPED):
        typer.echo(f"{TRIPPED}{guard.tripped[0]}", err=True)
    raise typer.Exit(0)


@app.command("drain")
def drain(
    config_file: Annotated[
        Path | None, typer.Option("--config", help="TOML config file to read.")
    ] = None,
) -> None:
    """Run the albums the Lidarr hook queued, removing each once its run has finished."""
    config.warn_unknown_env(os.environ)
    try:
        settings, _ = config.Settings.load(None, {}, config_file)
    except config.ConfigError as error:
        _stop(error.message, 2)
    try:
        queued = spool.entries(hook.spool_folder(settings))
    except OSError:
        _stop(hook.SPOOL_UNREADABLE, 2)
    if not queued:
        raise typer.Exit(0)
    guard.install([settings.music_root], read_only=settings.dry_run)
    typer.echo(NOTICE, err=True)
    if settings.dry_run:
        typer.echo(DRY_RUN, err=True)
    try:
        counts = hook.drain_spool(settings)
    except guard.GuardViolation as violation:
        _stop(f"{TRIPPED}{violation}", EXIT_TRIPPED)
    except Exception as error:
        # Not Python's exit 1, which a scheduler would read as LRCLIB being down.
        _stop_if_tripped()
        _stop(
            "plyrix: the drain stopped on an unexpected error, so the albums not yet run stay "
            f"queued: {type(error).__name__}",
            EXIT_UNEXPECTED,
        )
    _print_drain(counts)
    _report_plex(counts.refreshed, counts.plex_reply, counts.plex_left)
    _stop_if_tripped()
    if counts.stopped == hook.LOCKED:
        # A drain is a frequent scheduled job, and the weekly run holding the lock is normal.
        typer.echo(
            "plyrix: another plyrix run is using the state database, so "
            f"{counts.left} queued albums wait for the next drain"
        )
    elif counts.stopped == hook.LRCLIB_UNAVAILABLE:
        _stop(counts.message or _QUEUE_REASONS[hook.LRCLIB_UNAVAILABLE], 1)
    elif counts.stopped in (hook.STATE, hook.SPOOL, hook.ROOT):
        _stop(counts.message or hook.SPOOL_UNCHANGEABLE, 2)
    if counts.quarantined:
        _stop(_quarantined_line(counts.quarantined), EXIT_UNFINISHED)
    if counts.failed_tracks:
        _stop(
            "plyrix: tracks where a write, a file read or an LRCLIB lookup failed in queued "
            f"albums: {counts.failed_tracks}; the next run will try them again",
            EXIT_UNFINISHED,
        )


NO_LIVE_DUMP = (
    "plyrix: no live dump is recorded and dump_id (PLYRIX_DUMP_ID) is not set, so plyrix can't "
    "tell whether a listed dump is newer; set dump_id to the dump LRCLIB holds, or run plyrix "
    "lrclib-record NAME --status live once"
)


def _update_settings(config_file: Path | None) -> config.Settings:
    """Load the settings for an lrclib-* command and start the tripwire over the library."""
    config.warn_unknown_env(os.environ)
    try:
        settings, _ = config.Settings.load(None, {}, config_file)
    except config.ConfigError as error:
        _stop(error.message, 2)
    # These read LRCLIB's files and write only the state database, never the library.
    guard.install([settings.music_root], read_only=True)
    return settings


@app.command("lrclib-check")
def lrclib_check(
    listing: Annotated[
        str, typer.Option("--listing", metavar="URL", help="URL of the official dump listing.")
    ],
    base: Annotated[
        str, typer.Option("--base", metavar="URL", help="URL the dumps are fetched from.")
    ],
    retry: Annotated[
        bool, typer.Option("--retry", help="Try a dump that was refused or rolled back again.")
    ] = False,
    config_file: Annotated[
        Path | None, typer.Option("--config", help="TOML config file to read.")
    ] = None,
) -> None:
    """Say whether the dump listing names a newer dump LRCLIB can serve. Downloads nothing."""
    if not dbupdate.usable_listing_url(listing):
        _stop("plyrix: --listing must be an http or https URL with a host", 2)
    if not dbupdate.usable_listing_url(base):
        _stop("plyrix: --base must be an http or https URL with a host", 2)
    settings = _update_settings(config_file)
    try:
        result = dbupdate.check(settings, listing, base, retry)
    except db.StateError as error:
        _stop(error.message, error.exit_code)
    _stop_if_tripped()
    if result.verdict == dbupdate.UNKNOWN:
        _stop(NO_LIVE_DUMP, 2)
    if result.verdict == dbupdate.REFUSED:
        typer.echo(f"refused {result.name}")
        _stop(result.message or "plyrix: the newest dump was refused", 1)
    if result.verdict == dbupdate.UPDATE:
        typer.echo(f"update {result.name} {result.size} {result.unpacked} {result.user_version}")
        raise typer.Exit(0)
    if result.message:
        typer.echo(result.message, err=True)
    typer.echo("none")
    raise typer.Exit(0)


@app.command("lrclib-verify")
def lrclib_verify(
    # A str, because a Path would turn an empty argument into the working directory.
    path: Annotated[str, typer.Argument(metavar="PATH", help="The staged LRCLIB database.")],
    name: Annotated[
        str | None,
        typer.Option("--name", metavar="NAME", help="The dump's name, for its date."),
    ] = None,
    no_quick_check: Annotated[
        bool,
        typer.Option("--no-quick-check", help="Skip PRAGMA quick_check, which reads every page."),
    ] = False,
    config_file: Annotated[
        Path | None, typer.Option("--config", help="TOML config file to read.")
    ] = None,
) -> None:
    """Check, read-only, that the pinned LRCLIB can serve a staged database."""
    if not path or not os.path.isfile(path):
        _stop("plyrix: the staged database to verify is not a file", 2)
    if name is not None and not doctor.DUMP_NAME.fullmatch(name):
        _stop("plyrix: --name isn't a dump's name", 2)
    _update_settings(config_file)
    try:
        verified = dbupdate.verify(os.path.abspath(path), name, quick_check=not no_quick_check)
    except dbupdate.NotServable as refused:
        _stop_if_tripped()
        # The protocol line tells a refusal from any other exit 1, such as a crash, which
        # lrclib-update.sh must not record as a refusal for good.
        typer.echo("refused")
        _stop(f"plyrix: the staged database can't be served: {refused.reason}", 1)
    _stop_if_tripped()
    if verified.created_after_name:
        typer.echo(
            "plyrix: the dump's newest record is dated after its name, so plyrix uses the "
            "name's date",
            err=True,
        )
    typer.echo(f"ok {verified.probe_id}")


@app.command("lrclib-diff")
def lrclib_diff(
    name: Annotated[str, typer.Argument(metavar="NAME", help="The staged dump's file name.")],
    # Strs, because a Path would turn an empty argument into the working directory.
    staged: Annotated[
        str, typer.Option("--staged", metavar="PATH", help="The staged LRCLIB database.")
    ],
    previous_db: Annotated[
        str | None,
        typer.Option(
            "--previous-db",
            metavar="PATH",
            help="The database LRCLIB serves, read only when the live dump has no watermarks.",
        ),
    ] = None,
    config_file: Annotated[
        Path | None, typer.Option("--config", help="TOML config file to read.")
    ] = None,
) -> None:
    """Record which LRCLIB tracks are new or have new lyrics in a staged dump. Ids and keys only."""
    if not doctor.DUMP_NAME.fullmatch(name):
        _stop("plyrix: NAME isn't a dump's name", 2)
    if not staged or not os.path.isfile(staged):
        _stop("plyrix: the staged database to diff is not a file", 2)
    settings = _update_settings(config_file)
    previous = os.path.abspath(previous_db) if previous_db else None
    try:
        result = dbupdate.diff(settings, name, os.path.abspath(staged), previous)
    except dbupdate.NotServable as refused:
        _stop_if_tripped()
        _stop(f"plyrix: the staged database can't be diffed: {refused.reason}", 1)
    except db.StateError as error:
        _stop_if_tripped()
        _stop(error.message, error.exit_code)
    _stop_if_tripped()
    typer.echo(f"diff {result.state} {result.changed}")


@app.command("lrclib-record")
def lrclib_record(
    name: Annotated[str, typer.Argument(metavar="NAME", help="The dump's file name.")],
    status: Annotated[
        str,
        typer.Option(
            "--status", help="What the dump now is to LRCLIB: live, refused or rolled-back."
        ),
    ],
    config_file: Annotated[
        Path | None, typer.Option("--config", help="TOML config file to read.")
    ] = None,
) -> None:
    """Record in the state database which dump LRCLIB serves."""
    if not doctor.DUMP_NAME.fullmatch(name):
        _stop("plyrix: NAME isn't a dump's name", 2)
    if status not in dbupdate.STATUSES:
        _stop(f"plyrix: --status must be one of: {', '.join(dbupdate.STATUSES)}", 2)
    settings = _update_settings(config_file)
    try:
        line = dbupdate.record(settings, name, status)
    except db.StateError as error:
        _stop_if_tripped()
        _stop(error.message, error.exit_code)
    _stop_if_tripped()
    typer.echo(line)


@app.command("plex-refresh")
def plex_refresh(
    config_file: Annotated[
        Path | None, typer.Option("--config", help="TOML config file to read.")
    ] = None,
    state_db: Annotated[
        Path | None, typer.Option("--state-db", help="State database, outside the library.")
    ] = None,
    plex_url: Annotated[
        str | None, typer.Option("--plex-url", help="URL of your own Plex Media Server.")
    ] = None,
    # Strs, so a value that isn't a number gets the one line every other bad setting gets.
    plex_section: Annotated[
        str | None, typer.Option("--plex-section", help="Key of the Plex music library section.")
    ] = None,
    plex_music_root: Annotated[
        str | None,
        typer.Option("--plex-music-root", help="The music root as Plex sees it."),
    ] = None,
    plex_pace: Annotated[
        str | None,
        typer.Option("--plex-pace", help="Seconds to wait after each album refresh."),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=1, help="Most album refreshes to send. No limit."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Send nothing, and print what would be refreshed.")
    ] = False,
    # A str, because a Path would turn an empty argument into the working directory.
    report_file: Annotated[
        str | None,
        typer.Option("--report", help="JSON file of counts and run facts, outside the library."),
    ] = None,
) -> None:
    """Ask your local Plex Media Server to refresh each album that gained or changed a sidecar."""
    config.warn_unknown_env(os.environ)
    # Only flags the user set, so an unset flag can't hide a variable or the config file.
    flags = {
        "state_db": state_db,
        "plex_url": plex_url,
        "plex_section": plex_section,
        "plex_music_root": plex_music_root,
        "plex_pace": plex_pace,
        "report_file": report_file,
    }
    overrides: dict[str, object] = {k: v for k, v in flags.items() if v is not None}
    if dry_run:
        overrides["dry_run"] = True
    try:
        settings, _ = config.Settings.load(None, overrides, config_file)
    except config.ConfigError as error:
        _stop(error.message, 2)
    if settings.plex_url is None:
        # The step is optional, and a scheduler runs it after every run.
        _stop(NO_PLEX, 0)
    if settings.plex_section is None:
        _stop("plyrix: missing required settings: plex_section (PLYRIX_PLEX_SECTION)", 2)
    # report_file is run's setting, so a refresh chained after a run can't replace its report.
    report_path = settings.report_file if report_file is not None else None
    guard.install([settings.music_root], read_only=True)
    if settings.dry_run:
        typer.echo(DRY_RUN, err=True)
    started_at = report.now()
    if not os.path.exists(settings.state_db):
        # No ledger means nothing is queued, and this creates none.
        _finish_refresh(settings, report_path, started_at, plex.Reply.OK, refresh.RefreshCounts())
    token = settings.plex_token.get_secret_value() if settings.plex_token else None
    session = plex.new_session(token)
    base = str(settings.plex_url)
    try:
        # A dry run takes no lock and writes nothing, so it can't stop a run.
        conn = (db.connect_snapshot if settings.dry_run else db.connect)(settings.state_db)
    except db.StateError as error:
        _stop(error.message, error.exit_code)
    try:
        # Nothing queued sends Plex nothing, so a scheduled no-op stays quiet.
        if db.pending_refresh(conn):
            reply, info = plex.section(session, base, settings.plex_section)
            if reply is plex.Reply.OK and info is not None and info.type != "artist":
                reply = plex.Reply.MISSING
            if reply is not plex.Reply.OK:
                _finish_refresh(settings, report_path, started_at, reply, None)

        def announce(albums: int) -> None:
            noun = "album" if albums == 1 else "albums"
            typer.echo(
                f"plyrix: asking Plex to refresh {albums} {noun}, "
                f"one every {settings.plex_pace:g} seconds",
                err=True,
            )

        reply, counts = refresh.drain(
            conn, session, settings, limit=limit, dry_run=settings.dry_run, announce=announce
        )
    except db.StateError as error:
        _stop(error.message, error.exit_code)
    finally:
        conn.close()
    _finish_refresh(settings, report_path, started_at, reply, counts)


@app.command("plex-check")
def plex_check(
    # A str, because a Path would turn an empty argument into the working directory.
    path: Annotated[
        str | None,
        typer.Argument(
            metavar="[PATH]",
            help="A track, sidecar or folder in the music root to check, instead of a sample.",
        ),
    ] = None,
    sample: Annotated[
        int, typer.Option("--sample", min=1, help="How many refreshed sidecars to sample.")
    ] = 10,
    config_file: Annotated[
        Path | None, typer.Option("--config", help="TOML config file to read.")
    ] = None,
    state_db: Annotated[
        Path | None, typer.Option("--state-db", help="State database, outside the library.")
    ] = None,
    plex_url: Annotated[
        str | None, typer.Option("--plex-url", help="URL of your own Plex Media Server.")
    ] = None,
    # A str, so a value that isn't a number gets the one line every other bad setting gets.
    plex_section: Annotated[
        str | None, typer.Option("--plex-section", help="Key of the Plex music library section.")
    ] = None,
    plex_music_root: Annotated[
        str | None,
        typer.Option("--plex-music-root", help="The music root as Plex sees it."),
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", help="Also print each sidecar and its verdict.")
    ] = False,
    # A str, because a Path would turn an empty argument into the working directory.
    report_file: Annotated[
        str | None,
        typer.Option("--report", help="JSON file of counts and run facts, outside the library."),
    ] = None,
) -> None:
    """Ask your local Plex Media Server whether it attached each sidecar's lyrics as expected."""
    config.warn_unknown_env(os.environ)
    flags = {
        "state_db": state_db,
        "plex_url": plex_url,
        "plex_section": plex_section,
        "plex_music_root": plex_music_root,
        "report_file": report_file,
    }
    overrides: dict[str, object] = {k: v for k, v in flags.items() if v is not None}
    try:
        settings, start = config.Settings.load(path, overrides, config_file)
    except config.ConfigError as error:
        _stop(error.message, 2)
    if settings.plex_url is None:
        # Unlike plex-refresh, this was asked for, so it can't quietly pass.
        _stop(NO_PLEX_CHECK, 2)
    if settings.plex_section is None:
        _stop("plyrix: missing required settings: plex_section (PLYRIX_PLEX_SECTION)", 2)
    report_path = settings.report_file if report_file is not None else None
    guard.install([settings.music_root], read_only=True)
    started_at = report.now()
    try:
        # A snapshot takes no lock and writes nothing, so a check changes nothing (D-12).
        conn = db.connect_snapshot(settings.state_db)
    except db.StateError as error:
        _stop(error.message, error.exit_code)
    try:
        targets = db.sample_refreshed(conn, sample) if path is None else _named(conn, start)
    except db.StateError as error:
        _stop(error.message, error.exit_code)
    finally:
        conn.close()
    if path is not None and not targets:
        _stop(NOTHING_PUBLISHED, 2)
    if not targets:
        typer.echo(NOTHING_REFRESHED, err=True)
        _finish_check(settings, report_path, started_at, plex.Reply.OK, refresh.CheckCounts(), [])
    token = settings.plex_token.get_secret_value() if settings.plex_token else None
    session = plex.new_session(token)
    reply, info = plex.section(session, str(settings.plex_url), settings.plex_section)
    if reply is plex.Reply.OK and info is not None and info.type != "artist":
        reply = plex.Reply.MISSING
    if reply is not plex.Reply.OK:
        _finish_check(settings, report_path, started_at, reply, None, [])
    reply, counts, verdicts = refresh.check(session, settings, targets)
    shown = verdicts if verbose else []
    _finish_check(settings, report_path, started_at, reply, counts, shown)


@app.command("doctor")
def doctor_command(
    config_file: Annotated[
        Path | None, typer.Option("--config", help="TOML config file to read.")
    ] = None,
    lrclib_url: Annotated[
        str | None, typer.Option("--lrclib-url", help="Base URL of your local LRCLIB.")
    ] = None,
    state_db: Annotated[
        Path | None, typer.Option("--state-db", help="State database, outside the library.")
    ] = None,
    plex_url: Annotated[
        str | None, typer.Option("--plex-url", help="URL of your own Plex Media Server.")
    ] = None,
    # A str, so a value that isn't a number gets the one line every other bad setting gets.
    plex_section: Annotated[
        str | None, typer.Option("--plex-section", help="Key of the Plex music library section.")
    ] = None,
    plex_music_root: Annotated[
        str | None,
        typer.Option("--plex-music-root", help="The music root as Plex sees it."),
    ] = None,
    # A str, because a Path would turn an empty argument into the working directory.
    lrclib_data: Annotated[
        str | None,
        typer.Option(
            "--lrclib-data",
            metavar="DIR",
            help="Only check that a first LRCLIB dump load fits in DIR. Needs no settings.",
        ),
    ] = None,
    dump: Annotated[
        str | None,
        typer.Option(
            "--dump",
            metavar="NAME",
            help="With --lrclib-data, the dump to be loaded, whose download in DIR/dl counts.",
        ),
    ] = None,
    size: Annotated[
        int | None,
        typer.Option(
            "--size", metavar="BYTES", min=1, help="With --dump, the dump's download size."
        ),
    ] = None,
    unpacked: Annotated[
        int | None,
        typer.Option(
            "--unpacked",
            metavar="BYTES",
            min=1,
            help="With --dump and --size, the decompressed database's size from its header.",
        ),
    ] = None,
) -> None:
    """Check the config, library, state database disk, LRCLIB and Plex before a run."""
    if (size is None) != (unpacked is None):
        _finish_doctor(
            [doctor.Check("lrclib_data", doctor.Level.FAIL, "--size needs --unpacked", 2)]
        )
    if size is not None and dump is None:
        _finish_doctor([doctor.Check("lrclib_data", doctor.Level.FAIL, "--size needs --dump", 2)])
    if dump is not None and lrclib_data is None:
        _finish_doctor(
            [doctor.Check("lrclib_data", doctor.Level.FAIL, "--dump needs --lrclib-data", 2)]
        )
    if lrclib_data is not None:
        # The setup script runs this before LRCLIB exists, and maybe before plyrix is configured.
        if not lrclib_data:
            _finish_doctor(
                [doctor.Check("lrclib_data", doctor.Level.FAIL, "--lrclib-data is empty", 2)]
            )
        if dump is not None and not doctor.DUMP_NAME.fullmatch(dump):
            _finish_doctor(
                [doctor.Check("lrclib_data", doctor.Level.FAIL, "--dump isn't a dump's name", 2)]
            )
        directory = Path(os.path.abspath(lrclib_data))
        guard.install([directory], read_only=True)
        _finish_doctor(
            doctor.lrclib_data_checks(directory, doctor.read_mountinfo(), dump, size, unpacked)
        )
    config.warn_unknown_env(os.environ)
    flags = {
        "lrclib_url": lrclib_url,
        "state_db": state_db,
        "plex_url": plex_url,
        "plex_section": plex_section,
        "plex_music_root": plex_music_root,
    }
    overrides: dict[str, object] = {k: v for k, v in flags.items() if v is not None}
    try:
        settings, _ = config.Settings.load(None, overrides, config_file)
    except config.ConfigError as error:
        # Nothing else can be checked without settings.
        message = error.message.removeprefix("plyrix: ")
        _finish_doctor([doctor.Check("config", doctor.Level.FAIL, message, 2)])
    guard.install([settings.music_root], read_only=True)
    loaded = doctor.Check("config", doctor.Level.OK, "the settings load")
    _finish_doctor([loaded, *doctor.checks_for(settings)])


def _finish_doctor(checks: list[doctor.Check]) -> NoReturn:
    """Print one line per check, and exit with the worst code: 2 beats 1, and 1 beats 0."""
    for check in checks:
        typer.echo(f"{check.level.value} {check.name}: {check.message}")
    _stop_if_tripped()
    raise typer.Exit(doctor.worst(checks))


def _named(conn: db.Connection, start: bytes) -> list[tuple[bytes, Kind]]:
    """Return the published sidecars a PATH names: a folder's, a sidecar itself, or a track's."""
    stem, suffix = os.path.splitext(start)
    if os.path.isdir(start) or suffix in (Kind.LRC.value, Kind.TXT.value):
        return db.published_under(conn, start)
    return [
        row
        for kind in (Kind.LRC, Kind.TXT)
        for row in db.published_under(conn, stem + kind.value)
        if row[0] == stem + kind.value
    ]


def _finish_check(
    settings: config.Settings,
    report_path: Path | None,
    started_at: str,
    reply: plex.Reply,
    counts: refresh.CheckCounts | None,
    verdicts: list[tuple[bytes, plex.Verdict]],
) -> NoReturn:
    """Print the verdicts and counts, write the report, and exit with this outcome's code.

    Nothing Plex returned is printed but a verdict. counts is None when Plex failed before any
    sidecar was checked. A failed check is reported and nothing is queued again (D-11).
    """
    if reply is plex.Reply.MISSING:
        _stop(NOT_MUSIC, 2)
    root = os.fsencode(settings.music_root)
    for path, verdict in verdicts:
        typer.echo(f"{show(os.path.relpath(path, root))} {verdict.value}")
    if counts is not None:
        for name, value in dataclasses.asdict(counts).items():
            typer.echo(f"{name}={value}")
    unavailable = reply is not plex.Reply.OK
    payload = {
        "report_version": REPORT_VERSION,
        "plyrix_version": __version__,
        "command": "plex-check",
        "dry_run": False,
        "started_at": started_at,
        "finished_at": report.now(),
        "end_reason": END_PLEX_UNAVAILABLE if unavailable else pipeline.END_FINISHED,
        "counts": dataclasses.asdict(counts or refresh.CheckCounts()),
    }
    reported = report_path is None or _write_report(report_path, payload)
    if unavailable:
        _stop(CHECK_REFUSED if reply is plex.Reply.REFUSED else CHECK_DOWN, 1)
    if counts is not None and counts.failed:
        _stop(
            f"plyrix: sidecars Plex didn't attach as expected: {counts.failed}; nothing was "
            "queued again, and Plex may still be refreshing in the background",
            EXIT_UNFINISHED,
        )
    raise typer.Exit(0 if reported else EXIT_UNFINISHED)


def _finish_refresh(
    settings: config.Settings,
    report_path: Path | None,
    started_at: str,
    reply: plex.Reply,
    counts: refresh.RefreshCounts | None,
) -> NoReturn:
    """Print the counts, write the report, and exit with the code this outcome gets.

    counts is None when Plex failed before the drain, and the report then holds zero counts. A
    section that isn't there or isn't music is a wrong setting, which writes no report.
    """
    failed_listing = reply is not plex.Reply.OK and (counts is None or not counts.refresh_failed)
    if failed_listing and reply is plex.Reply.MISSING:
        _stop(NOT_MUSIC, 2)
    if counts is not None:
        _print_refresh_counts(counts)
    end_reason = END_PLEX_UNAVAILABLE if failed_listing else pipeline.END_FINISHED
    payload = {
        "report_version": REPORT_VERSION,
        "plyrix_version": __version__,
        "command": "plex-refresh",
        "dry_run": settings.dry_run,
        "started_at": started_at,
        "finished_at": report.now(),
        "end_reason": end_reason,
        "counts": dataclasses.asdict(counts or refresh.RefreshCounts()),
    }
    reported = report_path is None or _write_report(report_path, payload)
    if failed_listing:
        _stop(PLEX_REFUSED if reply is plex.Reply.REFUSED else PLEX_DOWN, 1)
    if counts is not None and counts.refresh_failed:
        if reply is plex.Reply.REFUSED:
            typer.echo(REFRESH_REFUSED, err=True)
        elif reply is plex.Reply.UNAVAILABLE:
            typer.echo(REFRESH_STOPPED, err=True)
        _stop(
            f"plyrix: album refreshes Plex didn't accept: {counts.refresh_failed}; "
            "their sidecars stay queued for the next plex-refresh",
            EXIT_UNFINISHED,
        )
    raise typer.Exit(0 if reported else EXIT_UNFINISHED)


def _print_refresh_counts(counts: refresh.RefreshCounts) -> None:
    for name, value in dataclasses.asdict(counts).items():
        typer.echo(f"{name}={value}")


def _print_counts(counts: pipeline.Counts) -> None:
    for name, value in dataclasses.asdict(counts).items():
        typer.echo(f"{name}={value}")


def _report(
    settings: config.Settings, started_at: str, end_reason: str, counts: pipeline.Counts
) -> bool:
    """Write the report if one was asked for, and return False if it couldn't be written.

    The counts are on stdout by then, so that costs one line, and the exit code says the rest.
    """
    if settings.report_file is None:
        return True
    # The run read the dump recorded live with its ledger open, so the report names that (D-07).
    dump_id = settings.dump_id if pipeline.run_dump_id is None else pipeline.run_dump_id
    payload = {
        "report_version": REPORT_VERSION,
        "plyrix_version": __version__,
        "dry_run": settings.dry_run,
        "dump_id": dump_id,
        "started_at": started_at,
        "finished_at": report.now(),
        "end_reason": end_reason,
        "counts": dataclasses.asdict(counts),
    }
    return _write_report(settings.report_file, payload)


def _write_report(path: Path, payload: dict[str, object]) -> bool:
    """Write a report, and return False if it couldn't be written."""
    try:
        report.write(path, payload)
    except guard.GuardViolation as violation:
        _stop(f"{TRIPPED}{violation}", EXIT_TRIPPED)
    except OSError:
        typer.echo("plyrix: report_file (PLYRIX_REPORT_FILE) could not be written", err=True)
        return False
    return True


def _stop_if_tripped() -> None:
    """Fail the run on a violation that something swallowed, or that another error replaced."""
    if guard.tripped:
        _stop(f"{TRIPPED}{guard.tripped[0]}", EXIT_TRIPPED)


def _stop(message: str, exit_code: int) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(exit_code)
