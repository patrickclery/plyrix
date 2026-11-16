"""SAFE-05: lyric text reaches a sidecar and nothing else. All lyric-shaped data is invented."""

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from tinytag import TinyTag

from conftest import (
    AUDIO_SUFFIXES,
    CANARY,
    FIXTURES,
    LOCAL_LYRICS,
    LYRICFIND,
    RECORD,
    RunCli,
    StubLrclib,
    StubPlex,
    leaks,
    parse_counts,
    per_format_lib,
    plex_lyric_stream,
    plex_metadata,
    plex_section_dir,
    plex_stream_body,
    plex_track,
    queue_sidecars,
    state_db_path,
)
from plyrix import tags

ROOT = Path(__file__).parent.parent
EMBEDDED_TOKEN = "PLYRIX-EMBEDDED-TOKEN-7F3A"
OK_PROC = subprocess.CompletedProcess[bytes](args=[], returncode=0, stdout=b"", stderr=b"")

TIMESTAMPED_LINE = re.compile(r"^\s*\[\d{1,2}:\d{2}([.:]\d{1,3})?\]")
PLACEHOLDER_LINE = re.compile(r"placeholder line \d+|PLYRIX-CANARY-")
BANNED_SUFFIXES = (".lrc", ".sqlite", ".sqlite3", ".db")
ALLOWED_TAG_VALUES = {
    "artist": {None, "Fixture Artist"},
    "albumartist": {None, "Fixture Album Artist", "Various Artists"},
    "title": {None, "Fixture Title"},
    "album": {None, "Fixture Album"},
}
ALLOWED_CATCH_ALL = {
    "encoder": {"Lavc libopus", "Lavc libvorbis"},
    "lyrics": {EMBEDDED_TOKEN},
    "uslt": {EMBEDDED_TOKEN},
}

# Fails inside the writer, with the lyric text in a local and nowhere in the message. Nothing in
# plyrix hides this: the point is that a plain traceback prints no locals.
UNCAUGHT_CHILD = """
import sys
from plyrix import cli, sidecars

lyric_canary = sys.argv[2]

def publish(track, kind, data, log, method):
    lyric_text = data.decode()
    if lyric_canary not in lyric_text:
        raise SystemExit("the local does not hold the canary, so this proves nothing")
    raise RuntimeError("the writer failed")

sidecars.publish = publish
if sys.argv[3] == "control":
    cli.app.pretty_exceptions_enable = True
    cli.app.pretty_exceptions_show_locals = True
cli.app(["run", sys.argv[1]])
"""


# Dies with the lyric text in a temp file and no sidecar yet.
KILLED_BEFORE_LINK_CHILD = """
import os, signal, sys
from plyrix import cli

def kill(*_args, **_kwargs):
    os.kill(os.getpid(), signal.SIGKILL)

os.link = kill
cli.app(["run", sys.argv[1]])
"""


def needles(token: str) -> list[bytes]:
    return [token.encode(), token.encode("utf-16-le"), token.encode("utf-16-be")]


def scan(
    tmp_path: Path,
    proc: "subprocess.CompletedProcess[bytes]",
    token: str = CANARY,
    skip_suffixes: tuple[str, ...] = (".lrc", ".txt"),
) -> list[str]:
    """Return every place token reached, as UTF-8 or UTF-16, in the output and any file's bytes.

    Files under the library with a skipped suffix are left out. Failing on an empty walk or a
    missing ledger keeps this from passing because it looked at nothing.
    """
    wanted = needles(token)
    hits = [
        label
        for label, blob in (("stdout", proc.stdout), ("stderr", proc.stderr))
        if any(needle in blob for needle in wanted)
    ]
    scanned: list[Path] = []
    for base, _, files in os.walk(tmp_path):
        for file in files:
            path = Path(base) / file
            if path.suffix in skip_suffixes and path.is_relative_to(tmp_path / "lib"):
                continue
            scanned.append(path)
            blob = path.read_bytes()
            if any(needle in blob for needle in wanted):
                hits.append(str(path))
    assert scanned, "the scan looked at no files"
    assert state_db_path(tmp_path) in scanned, "the scan did not cover the state database"
    return hits


def run(
    run_cli: RunCli, stub: StubLrclib, lib: Path, *flags: str
) -> "subprocess.CompletedProcess[bytes]":
    return run_cli(["run", lib, *flags], env={"PLYRIX_LRCLIB_URL": stub.base_url})


def nonzero(proc: "subprocess.CompletedProcess[bytes]") -> dict[str, int]:
    return {key: value for key, value in parse_counts(proc.stdout).items() if value}


def one_track(tmp_path: Path) -> Path:
    lib = tmp_path / "lib"
    (lib / "Album").mkdir(parents=True)
    shutil.copyfile(FIXTURES / "t.flac", lib / "Album" / "t.flac")
    return lib


def ledger_with_a_run(tmp_path: Path) -> Path:
    """Make the file the scan requires, so a control starts from a clean, complete tree."""
    state = state_db_path(tmp_path)
    state.parent.mkdir(parents=True)
    with sqlite3.connect(state) as conn:
        conn.execute("CREATE TABLE planted (value)")
    return state


@pytest.mark.parametrize("flags", [(), ("--verbose",)], ids=["default", "verbose"])
def test_clean_run_all_formats(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, flags: tuple[str, ...]
) -> None:
    lib = tmp_path / "lib"
    tracks = per_format_lib(lib, "t")

    proc = run(run_cli, stub_lrclib, lib, *flags)

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["written_lrc"] == 5
    assert len(stub_lrclib.requests) == 5
    # The canary did travel the whole way, so its absence everywhere else means something.
    for track in tracks:
        assert CANARY.encode() in track.with_suffix(".lrc").read_bytes()
    assert scan(tmp_path, proc) == []


def test_negative_control(tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli) -> None:
    lib = one_track(tmp_path)
    proc = run(run_cli, stub_lrclib, lib)
    assert scan(tmp_path, proc) == []

    with sqlite3.connect(state_db_path(tmp_path)) as conn:
        conn.execute("CREATE TABLE planted (value)")
        conn.execute("INSERT INTO planted VALUES (?)", (CANARY,))

    assert scan(tmp_path, proc) == [str(state_db_path(tmp_path))]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "utf-16-be"])
def test_negative_control_finds_every_encoding_in_any_state_file(
    tmp_path: Path, encoding: str
) -> None:
    state = ledger_with_a_run(tmp_path)
    planted = state.parent / "run.log"
    planted.write_bytes(b"\xff\x00binary " + CANARY.encode(encoding))

    assert scan(tmp_path, OK_PROC) == [str(planted)]


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_negative_control_finds_output(tmp_path: Path, stream: str) -> None:
    ledger_with_a_run(tmp_path)
    output = {"stdout": b"", "stderr": b"", stream: CANARY.encode()}
    proc = subprocess.CompletedProcess[bytes](args=[], returncode=0, **output)

    assert scan(tmp_path, proc) == [stream]


def test_negative_control_finds_a_sidecar_outside_the_library(tmp_path: Path) -> None:
    ledger_with_a_run(tmp_path)
    outside = tmp_path / "elsewhere" / "t.lrc"
    outside.parent.mkdir()
    outside.write_bytes(CANARY.encode())
    inside = tmp_path / "lib" / "t.lrc"
    inside.parent.mkdir()
    inside.write_bytes(CANARY.encode())

    assert scan(tmp_path, OK_PROC) == [str(outside)]


def test_negative_control_an_empty_tree_fails_the_scan(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="no files"):
        scan(tmp_path, OK_PROC)


def test_negative_control_a_tree_with_no_ledger_fails_the_scan(tmp_path: Path) -> None:
    (tmp_path / "other").write_bytes(b"x")

    with pytest.raises(AssertionError, match="state database"):
        scan(tmp_path, OK_PROC)


@pytest.mark.parametrize(
    "body", [{"message": CANARY}, f"plain text {CANARY}".encode()], ids=["json", "text"]
)
def test_server_error_body(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, body: object
) -> None:
    lib = one_track(tmp_path)
    stub_lrclib.status = 500
    stub_lrclib.body = body

    proc = run(run_cli, stub_lrclib, lib)

    assert proc.returncode == 3, proc.stderr
    assert nonzero(proc) == {"unavailable": 1}
    assert len(stub_lrclib.requests) == 1
    assert scan(tmp_path, proc) == []


def test_server_error_body_when_the_run_stops_early(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    (lib / "Album").mkdir(parents=True)
    for index in range(6):
        shutil.copyfile(FIXTURES / "t.flac", lib / "Album" / f"{index}.flac")
    stub_lrclib.status = 503
    stub_lrclib.body = {"message": CANARY}

    proc = run(run_cli, stub_lrclib, lib)

    assert proc.returncode == 1, proc.stderr
    assert nonzero(proc) == {"unavailable": 5}
    assert scan(tmp_path, proc) == []


UNAVAILABLE = {"unavailable": 1}
PLAIN = {"plain": 1, "written_txt": 1, "looked_up": 1}
NOT_FOUND = {"no_match": 1, "no_match_not_found": 1, "looked_up": 1}
SYNCED = {"synced": 1, "written_lrc": 1, "looked_up": 1}
TIMING_REJECTED = {"no_match": 1, "no_match_timing_rejected": 1, "looked_up": 1}
DOUBTFUL_ARTIST = {"no_match": 1, "no_match_doubtful_artist": 1, "looked_up": 1}
DOUBTFUL_TITLE = {"no_match": 1, "no_match_doubtful_title": 1, "looked_up": 1}
BEYOND_THE_END = f"[00:01.00] {CANARY}\n[59:00.00] {CANARY}"


@pytest.mark.parametrize(
    ("body", "counts"),
    [
        (f"not json {CANARY}".encode(), UNAVAILABLE),
        (f'{{"syncedLyrics": "{CANARY}'.encode(), UNAVAILABLE),
        ([CANARY], UNAVAILABLE),
        (CANARY, UNAVAILABLE),
        ({**RECORD, "syncedLyrics": CANARY}, PLAIN),
        ({**RECORD, "syncedLyrics": [f"[00:01.00] {CANARY}"]}, PLAIN),
        ({**RECORD, "syncedLyrics": {"line": f"[00:01.00] {CANARY}"}}, PLAIN),
        ({**RECORD, "syncedLyrics": 7}, PLAIN),
        ({**RECORD, "syncedLyrics": None, "plainLyrics": [CANARY]}, NOT_FOUND),
        ({**RECORD, "syncedLyrics": None, "plainLyrics": {"text": CANARY}}, NOT_FOUND),
        ({**RECORD, "syncedLyrics": None, "lyricsfile": [CANARY]}, PLAIN),
        ({**RECORD, "plainLyrics": [CANARY], "lyricsfile": 7}, SYNCED),
        ({**RECORD, "id": CANARY}, UNAVAILABLE),
        ({"message": CANARY}, UNAVAILABLE),
        ({**RECORD, "syncedLyrics": BEYOND_THE_END}, PLAIN),
        ({**RECORD, "syncedLyrics": BEYOND_THE_END, "plainLyrics": None}, TIMING_REJECTED),
        ({**RECORD, "artistName": CANARY}, DOUBTFUL_ARTIST),
        ({**RECORD, "trackName": CANARY}, DOUBTFUL_TITLE),
    ],
)
def test_malformed_responses(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, body: object, counts: dict[str, int]
) -> None:
    lib = one_track(tmp_path)
    stub_lrclib.body = body

    proc = run(run_cli, stub_lrclib, lib, "--verbose")

    assert proc.returncode == (3 if counts is UNAVAILABLE else 0), proc.stderr
    assert nonzero(proc) == counts
    assert b"Traceback" not in proc.stderr
    assert scan(tmp_path, proc) == []


@pytest.mark.parametrize("change", ["track-gone", "nothing", "folder-excluded"])
def test_a_killed_runs_temp_does_not_outlive_the_next_run(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, change: str
) -> None:
    lib = one_track(tmp_path)
    killed = subprocess.run(
        [sys.executable, "-c", KILLED_BEFORE_LINK_CHILD, lib],
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("PLYRIX_")},
            "PLYRIX_LRCLIB_URL": stub_lrclib.base_url,
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
        },
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    [temp] = (lib / "Album").glob(".plyrix-*.part")
    # The canary really is in the temp, so its absence afterwards means something.
    assert scan(tmp_path, OK_PROC) == [str(temp)]
    if change == "track-gone":
        (lib / "Album" / "t.flac").unlink()
    # A library with no audio stops before the ledger opens, so this run needs something to look at.
    shutil.copyfile(FIXTURES / "notags.flac", lib / "u.flac")

    flags = ("--exclude", "Album") if change == "folder-excluded" else ()
    proc = run(run_cli, stub_lrclib, lib, *flags)

    assert not temp.exists()
    assert scan(tmp_path, proc) == []


def run_uncaught_child(
    tmp_path: Path, stub: StubLrclib, lib: Path, mode: str
) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(
        [sys.executable, "-c", UNCAUGHT_CHILD, lib, CANARY, mode],
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("PLYRIX_")},
            "PLYRIX_LRCLIB_URL": stub.base_url,
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "COLUMNS": "200",
        },
        capture_output=True,
        check=False,
        timeout=120,
    )


def test_uncaught_exception(tmp_path: Path, stub_lrclib: StubLrclib) -> None:
    lib = one_track(tmp_path)

    proc = run_uncaught_child(tmp_path, stub_lrclib, lib, "real")

    assert proc.returncode == 1
    assert b"RuntimeError: the writer failed" in proc.stderr
    assert scan(tmp_path, proc) == []


def test_uncaught_exception_control(tmp_path: Path, stub_lrclib: StubLrclib) -> None:
    """With locals turned on, the same failure prints the canary, so the test above can fail."""
    lib = one_track(tmp_path)

    proc = run_uncaught_child(tmp_path, stub_lrclib, lib, "control")

    assert proc.returncode == 1
    assert b"RuntimeError" in proc.stderr
    assert "stderr" in scan(tmp_path, proc)


@pytest.mark.parametrize("flags", [(), ("--verbose",)], ids=["default", "verbose"])
def test_embedded_token(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, flags: tuple[str, ...]
) -> None:
    lib = tmp_path / "lib"
    copies = per_format_lib(lib, "embedded_token")
    for copy in copies:
        assert EMBEDDED_TOKEN in str(TinyTag.get(copy).other), copy.name

    proc = run(run_cli, stub_lrclib, lib, *flags)

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["written_lrc"] == 5
    assert len(stub_lrclib.requests) == 5
    assert EMBEDDED_TOKEN not in json.dumps(stub_lrclib.requests)
    # The audio copies hold the token by construction, and nothing else may.
    assert scan(tmp_path, proc, EMBEDDED_TOKEN, skip_suffixes=AUDIO_SUFFIXES) == []


@pytest.mark.parametrize("synced", [None, "", "   \n\t "], ids=["null", "empty", "whitespace"])
def test_empty_synced_fields_write_no_lrc(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, synced: str | None
) -> None:
    lib = one_track(tmp_path)
    stub_lrclib.body = {**RECORD, "syncedLyrics": synced}

    proc = run(run_cli, stub_lrclib, lib)

    assert proc.returncode == 0, proc.stderr
    assert nonzero(proc) == PLAIN
    assert sorted(path.name for path in (lib / "Album").iterdir()) == ["t.flac", "t.txt"]
    assert scan(tmp_path, proc) == []


def tracked_files() -> list[Path]:
    """Return tracked files and new unignored ones, so a new file is checked before its commit."""
    listing = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    paths = [ROOT / os.fsdecode(name) for name in listing.split(b"\0") if name]
    return [path for path in paths if path.is_file()]


def banned_lyric_files(paths: list[Path]) -> list[Path]:
    return [
        path
        for path in paths
        if path.suffix.lower() in BANNED_SUFFIXES
        or "cassette" in str(path.relative_to(ROOT)).lower()
    ]


def unplaceholder_lines(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if TIMESTAMPED_LINE.match(line) and not PLACEHOLDER_LINE.search(line)
    ]


def test_the_listing_covers_the_repo_and_leaves_out_private_files() -> None:
    names = {str(path.relative_to(ROOT)) for path in tracked_files()}

    assert {
        "README.md",
        "tests/test_lyric_leak.py",
        "tests/fixtures/t.flac",
        "CONTRIBUTING.md",
        ".github/workflows/ci.yml",
        "site/astro.config.mjs",
        "site/src/content/docs/index.mdx",
    } <= names
    assert not {name for name in names if name.startswith((".planning/", "reference/"))}
    # The site's installs, build output and README-generated pages stay out.
    generated = ("site/node_modules/", "site/dist/", "site/.astro/", "site/src/content/docs/docs/")
    assert not {name for name in names if name.startswith(generated)}


# Images: no screenshot, no Plex or Plexamp screen, nothing that could show lyrics (D-03, D-23).
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico"}
ALLOWED_IMAGES = {
    "site/public/favicon.svg",
    # Phase 04.1-03's brand files, allowed ahead of time so either phase can land first.
    "assets/brand/mark.svg",
    "assets/brand/mark-16.svg",
    "assets/brand/lockup.svg",
    "assets/brand/lockup-dark.svg",
    "assets/brand/mark-16.png",
    "assets/brand/mark-32.png",
    "assets/brand/mark-180.png",
    "assets/brand/mark-512.png",
    "assets/brand/favicon.ico",
    "assets/brand/README.md",
}


def unlisted_images(names: list[str]) -> list[str]:
    return [
        name
        for name in names
        if Path(name).suffix.lower() in IMAGE_SUFFIXES and name not in ALLOWED_IMAGES
    ]


def test_tracked_images_are_allow_listed() -> None:
    names = [str(path.relative_to(ROOT)) for path in tracked_files()]
    assert unlisted_images(names) == []


def test_tracked_images_control() -> None:
    assert unlisted_images(["docs/screenshot.png", "site/public/favicon.svg"]) == [
        "docs/screenshot.png"
    ]


def test_tracked_svgs_hold_no_text() -> None:
    for path in tracked_files():
        if path.suffix.lower() == ".svg":
            assert "<text" not in path.read_text(encoding="utf-8"), path.relative_to(ROOT)


def test_no_tracked_lyric_files() -> None:
    assert banned_lyric_files(tracked_files()) == []


@pytest.mark.parametrize(
    "name", ["a/t.lrc", "a/T.LRC", "state.db", "x.sqlite3", "x.sqlite", "tests/cassettes/get.yaml"]
)
def test_no_tracked_lyric_files_control(name: str) -> None:
    assert banned_lyric_files([ROOT / name]) == [ROOT / name]


def test_no_tracked_lyric_files_control_passes_ordinary_files() -> None:
    assert banned_lyric_files([ROOT / "README.md", ROOT / "tests/fixtures/t.flac"]) == []


def test_timestamped_lines_are_placeholders() -> None:
    checked = 0
    for path in tracked_files():
        blob = path.read_bytes()
        if b"\0" in blob:
            continue
        checked += 1
        problems = unplaceholder_lines(blob.decode("utf-8", "replace"))
        assert problems == [], path.relative_to(ROOT)
    assert checked > 0


@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        ("[00:01.00] invented words standing in for a lyric", True),
        ("  [1:02] invented words", True),
        ("[00:01:50] invented words", True),
        ("[00:01.00] placeholder line 3", False),
        (f"[00:01.00] {CANARY} line 1", False),
        ("[ar:Fixture Artist]", False),
        ("ordinary text with [00:01.00] inside it", False),
    ],
)
def test_timestamped_lines_control(line: str, flagged: bool) -> None:
    assert bool(unplaceholder_lines(f"before\n{line}\nafter\n")) is flagged


def test_fixture_metadata_allowlist() -> None:
    fixtures = [path for path in FIXTURES.iterdir() if path.name != "SHA256SUMS"]
    assert len(fixtures) == 28

    for path in fixtures:
        # Named fields as plyrix reads them: tinytag alone garbles the unsynchronised fixtures.
        named = tags.read(os.fsencode(path))
        for field, allowed in ALLOWED_TAG_VALUES.items():
            assert getattr(named, field) in allowed, (path.name, field)
        found = TinyTag.get(path)
        assert found.comment is None, path.name
        catch_all = dict(found.other)
        assert set(catch_all) <= set(ALLOWED_CATCH_ALL), path.name
        for key, values in catch_all.items():
            assert set(values) <= ALLOWED_CATCH_ALL[key], (path.name, key)


def test_plex_check_leaks_no_lyric_text(
    tmp_path: Path, stub_plex: StubPlex, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    real = os.fsencode(os.path.realpath(lib))
    sidecars = {f"A{n}".encode(): real + f"/A{n}/t.lrc".encode() for n in range(3)}
    queue_sidecars(state_db_path(tmp_path), list(sidecars.values()), refreshed=True)
    section = plex_section_dir("1", "artist", real)
    stub_plex.sections = [{**section, "title": CANARY}]
    for n, path in enumerate(sidecars.values()):
        key = str(700 + n)
        track = plex_track(key, str(600 + n), path.removesuffix(b".lrc") + b".flac")
        stub_plex.tracks.append({**track, "title": CANARY, "parentTitle": CANARY})
        stream = {**plex_lyric_stream(int(key), LOCAL_LYRICS, "lrc"), "displayTitle": CANARY}
        found = {**plex_lyric_stream(n, LYRICFIND, "lrc", timed="1"), "displayTitle": CANARY}
        stub_plex.metadata[key] = plex_metadata(key, [found, stream], title=CANARY)
        # The last stream's body says it isn't timed, so a failing verdict is covered too.
        stub_plex.streams[key] = plex_stream_body(n < 2, CANARY)
    out = tmp_path / "out" / "check.json"
    out.parent.mkdir()
    env = {
        "PLYRIX_MUSIC_ROOT": str(lib),
        "PLYRIX_LRCLIB_URL": "http://127.0.0.1:9/api",
        "PLYRIX_PLEX_URL": stub_plex.base_url,
        "PLYRIX_PLEX_SECTION": "1",
    }

    proc = run_cli(["plex-check", "--verbose", "--report", str(out)], env=env)

    assert proc.returncode == 3, proc.stderr
    assert parse_counts(proc.stdout)["local_timed"] == 2
    # The canary did travel: every body holding it was served.
    served = {str(r["path"]) for r in stub_plex.requests}
    assert {f"/library/streams/{700 + n}" for n in range(3)} <= served
    assert out.exists()
    assert leaks(tmp_path, proc, lib) == []


NAME_TOKEN = "PLYRIX-NAME-TOKEN-91C2"


def test_the_diff_stores_no_lyric_name_or_title_text(
    tmp_path: Path, run_cli: RunCli, stub_lrclib: StubLrclib
) -> None:
    from lrclib_fixture import add_tracks, make_db, replace_lyrics

    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copyfile(FIXTURES / "t.flac", lib / "t.flac")
    # The LRCLIB databases sit outside the scanned tree: they are the source, not a leak.
    source = tmp_path.parent / f"{tmp_path.name}-lrclib"
    source.mkdir()
    old = make_db(source / "old.sqlite3", 30, token=NAME_TOKEN)
    new = source / "new.sqlite3"
    shutil.copyfile(old, new)
    add_tracks(new, 10, token=NAME_TOKEN)
    replace_lyrics(new, [3, 6, 9])
    env = {"PLYRIX_MUSIC_ROOT": str(lib), "PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    first, second = (
        "lrclib-db-dump-20261030T182259Z.sqlite3.gz",
        "lrclib-db-dump-20261201T000000Z.sqlite3.gz",
    )

    assert run_cli(["lrclib-record", first, "--status", "live"], env).returncode == 0
    diffed = run_cli(["lrclib-diff", second, "--staged", new, "--previous-db", old], env)
    recorded = run_cli(["lrclib-record", second, "--status", "live"], env)

    assert diffed.returncode == 0, diffed.stderr
    assert diffed.stdout == b"diff complete 13\n"
    assert recorded.returncode == 0, recorded.stderr
    for proc in (diffed, recorded):
        assert scan(tmp_path, proc) == []
        assert scan(tmp_path, proc, token=NAME_TOKEN) == []
