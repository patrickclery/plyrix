"""plex-check through the real console script, against a stub Plex server. All of it invented."""

import dataclasses
import hashlib
import json
import os
import re
import time
from pathlib import Path

import pytest

from conftest import (
    LOCAL_LYRICS,
    LYRICFIND,
    RunCli,
    StubPlex,
    parse_counts,
    plex_lyric_stream,
    plex_metadata,
    plex_section_dir,
    plex_stream_body,
    plex_track,
    queue_sidecars,
    refresh_flags,
    state_db_path,
)
from plyrix import plex, refresh
from plyrix.domain import Kind, show
from plyrix.state import db

DUMMY_TOKEN = "plyrix-dummy-token-0123456789"
DUMMY_LRCLIB = "http://127.0.0.1:9/api"
SECTION = "1"
INVENTED_LINE = "invented line"
REPORT_KEYS = {
    "report_version",
    "plyrix_version",
    "command",
    "dry_run",
    "started_at",
    "finished_at",
    "end_reason",
    "counts",
}


class Checked:
    """A library whose sidecars sit only in the ledger, and a Plex that lists a track for each.

    Every track's local stream is timed for an .lrc and untimed for a .txt, so each check passes
    until a test changes what Plex answers.
    """

    def __init__(self, tmp_path: Path, stub: StubPlex) -> None:
        self.tmp_path = tmp_path
        self.stub = stub
        self.lib = tmp_path / "lib"
        self.lib.mkdir(exist_ok=True)
        self.real = os.fsencode(os.path.realpath(self.lib))
        self.ledger = state_db_path(tmp_path)
        self.keys: dict[bytes, str] = {}
        stub.location = os.fsdecode(self.real)
        self.env = {
            "PLYRIX_MUSIC_ROOT": str(self.lib),
            "PLYRIX_LRCLIB_URL": DUMMY_LRCLIB,
            "PLYRIX_PLEX_URL": stub.base_url,
            "PLYRIX_PLEX_SECTION": SECTION,
            "PLYRIX_PLEX_TOKEN": DUMMY_TOKEN,
        }

    def add(self, rel: bytes, kind: Kind = Kind.LRC, refreshed: bool = True) -> bytes:
        """Publish the sidecar at rel, have Plex list its track, and return the sidecar's path."""
        path = self.real + b"/" + rel
        queue_sidecars(self.ledger, [path], kind, refreshed)
        key = str(500 + len(self.keys))
        self.keys[rel] = key
        track = path.removesuffix(kind.value) + b".flac"
        self.stub.tracks.append(plex_track(key, str(100 + len(self.keys)), track))
        codec = kind.value.removeprefix(b".").decode()
        self.answer(rel, [plex_lyric_stream(int(key), LOCAL_LYRICS, codec)])
        self.stub.streams[key] = plex_stream_body(kind is Kind.LRC, INVENTED_LINE)
        return path

    def answer(self, rel: bytes, streams: list[dict[str, object]]) -> None:
        key = self.keys[rel]
        self.stub.metadata[key] = plex_metadata(key, streams)

    def body(self, rel: bytes, body: bytes) -> None:
        self.stub.streams[self.keys[rel]] = body

    def metadata_keys(self) -> list[str]:
        prefix = "/library/metadata/"
        return [
            str(r["path"]).removeprefix(prefix)
            for r in self.stub.requests
            if str(r["path"]).startswith(prefix)
        ]

    def sha(self) -> str:
        return hashlib.sha256(self.ledger.read_bytes()).hexdigest()


def folders(n: int, start: int = 0) -> list[bytes]:
    return [f"R{i:02}/t.lrc".encode() for i in range(start, start + n)]


@pytest.fixture
def lib(tmp_path: Path, stub_plex: StubPlex) -> Checked:
    return Checked(tmp_path, stub_plex)


def test_check_samples_ten_refreshed_sidecars_by_default(lib: Checked, run_cli: RunCli) -> None:
    for rel in folders(12):
        lib.add(rel)
    pending = []
    for rel in folders(3, 12):
        lib.add(rel, refreshed=False)
        pending.append(lib.keys[rel])

    proc = run_cli(["plex-check"], env=lib.env)

    assert proc.returncode == 0, proc.stderr
    counts = parse_counts(proc.stdout)
    assert (counts["checked"], counts["passed"], counts["failed"]) == (10, 10, 0)
    assert counts["local_timed"] == 10
    asked = lib.metadata_keys()
    assert len(asked) == 10
    assert set(asked).isdisjoint(pending)
    assert all(r["method"] == "GET" for r in lib.stub.requests)


def test_check_sample_flag_sets_how_many(lib: Checked, run_cli: RunCli) -> None:
    for rel in folders(5):
        lib.add(rel)

    proc = run_cli(["plex-check", "--sample", "3"], env=lib.env)

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["checked"] == 3
    assert len(lib.metadata_keys()) == 3


@pytest.mark.parametrize("value", ["0", "-1", "x"])
def test_check_sample_must_be_a_positive_number(lib: Checked, run_cli: RunCli, value: str) -> None:
    lib.add(b"A/t.lrc")

    proc = run_cli(["plex-check", "--sample", value], env=lib.env)

    assert proc.returncode == 2, proc.stderr
    assert lib.stub.requests == []


def test_check_a_lyricfind_stream_alone_fails_and_changes_nothing(
    lib: Checked, run_cli: RunCli
) -> None:
    paths = [lib.add(rel) for rel in folders(4)]
    lib.answer(b"R02/t.lrc", [plex_lyric_stream(9, LYRICFIND, "lrc", timed="1")])
    lib.stub.streams["9"] = plex_stream_body(True, INVENTED_LINE)
    before = lib.sha()

    proc = run_cli(["plex-check"], env=lib.env)

    assert proc.returncode == 3, proc.stderr
    counts = parse_counts(proc.stdout)
    assert (counts["checked"], counts["passed"], counts["failed"]) == (4, 3, 1)
    assert counts["provider"] == 1
    assert lib.sha() == before
    assert refresh_flags(lib.ledger) == {path: 0 for path in paths}
    assert not lib.ledger.with_name(lib.ledger.name + "-journal").exists()
    # Only the local stream's body is read, never LyricFind's.
    assert all(r["path"] != "/library/streams/9" for r in lib.stub.requests)


def test_check_a_local_untimed_stream_fails_an_lrc(lib: Checked, run_cli: RunCli) -> None:
    lib.add(b"A/t.lrc")
    lib.body(b"A/t.lrc", plex_stream_body(False, INVENTED_LINE))

    proc = run_cli(["plex-check"], env=lib.env)

    assert proc.returncode == 3, proc.stderr
    counts = parse_counts(proc.stdout)
    assert (counts["local_untimed"], counts["failed"], counts["unreadable"]) == (1, 1, 0)


def test_check_no_lyric_stream_is_none(lib: Checked, run_cli: RunCli) -> None:
    lib.add(b"A/t.lrc")
    lib.answer(b"A/t.lrc", [])

    proc = run_cli(["plex-check"], env=lib.env)

    assert proc.returncode == 3, proc.stderr
    counts = parse_counts(proc.stdout)
    assert (counts["none"], counts["not_in_plex"], counts["failed"]) == (1, 0, 1)


def test_check_a_sidecar_plex_does_not_list_is_none_and_not_in_plex(
    lib: Checked, run_cli: RunCli
) -> None:
    lib.add(b"A/t.lrc")
    lib.add(b"B/t.lrc")
    lib.stub.tracks = lib.stub.tracks[:1]

    proc = run_cli(["plex-check"], env=lib.env)

    assert proc.returncode == 3, proc.stderr
    counts = parse_counts(proc.stdout)
    assert (counts["checked"], counts["none"], counts["not_in_plex"]) == (2, 1, 1)
    assert lib.metadata_keys() == [lib.keys[b"A/t.lrc"]]


def test_check_a_track_plex_no_longer_has_is_not_in_plex(lib: Checked, run_cli: RunCli) -> None:
    lib.add(b"A/t.lrc")
    del lib.stub.metadata[lib.keys[b"A/t.lrc"]]

    proc = run_cli(["plex-check"], env=lib.env)

    assert proc.returncode == 3, proc.stderr
    counts = parse_counts(proc.stdout)
    assert (counts["none"], counts["not_in_plex"]) == (1, 1)


@pytest.mark.parametrize("body", [b"", b"not json", plex_stream_body("1", INVENTED_LINE)])
def test_check_an_unreadable_body_counts_untimed_and_unreadable(
    lib: Checked, run_cli: RunCli, body: bytes
) -> None:
    lib.add(b"A/t.lrc")
    lib.body(b"A/t.lrc", body)

    proc = run_cli(["plex-check"], env=lib.env)

    assert proc.returncode == 3, proc.stderr
    counts = parse_counts(proc.stdout)
    assert (counts["local_untimed"], counts["unreadable"], counts["failed"]) == (1, 1, 1)


@pytest.mark.parametrize(("kind", "rel"), [(Kind.LRC, b"A/t.lrc"), (Kind.TXT, b"A/t.txt")])
@pytest.mark.parametrize("status", [500, 401])
def test_check_a_stream_plex_fails_to_serve_is_no_verdict(
    lib: Checked, run_cli: RunCli, kind: Kind, rel: bytes, status: int
) -> None:
    # A .lrc must not count as untimed, nor a .txt pass, on a request Plex didn't answer.
    lib.add(rel, kind)
    lib.stub.streams_status = status

    proc = run_cli(["plex-check"], env=lib.env)

    assert proc.returncode == 1, proc.stderr
    assert b"plex_" in proc.stderr
    assert b"passed=1" not in proc.stdout
    assert b"local_untimed=1" not in proc.stdout


def test_check_a_stream_plex_no_longer_has_counts_unreadable(lib: Checked, run_cli: RunCli) -> None:
    lib.add(b"A/t.lrc")
    lib.stub.streams.clear()

    proc = run_cli(["plex-check"], env=lib.env)

    assert proc.returncode == 3, proc.stderr
    counts = parse_counts(proc.stdout)
    assert (counts["local_untimed"], counts["unreadable"], counts["failed"]) == (1, 1, 1)


def test_check_a_txt_passes_as_local_untimed(lib: Checked, run_cli: RunCli) -> None:
    lib.add(b"A/t.txt", Kind.TXT)

    proc = run_cli(["plex-check"], env=lib.env)

    assert proc.returncode == 0, proc.stderr
    counts = parse_counts(proc.stdout)
    assert (counts["local_untimed"], counts["passed"]) == (1, 1)


def test_check_an_lrc_beside_a_local_txt_is_judged_on_the_lrc_stream(
    lib: Checked, run_cli: RunCli
) -> None:
    lib.add(b"A/t.lrc")
    key = lib.keys[b"A/t.lrc"]
    lib.answer(
        b"A/t.lrc",
        [
            plex_lyric_stream(8, LOCAL_LYRICS, "txt"),
            plex_lyric_stream(int(key), LOCAL_LYRICS, "lrc"),
        ],
    )
    lib.stub.streams["8"] = plex_stream_body(False, INVENTED_LINE)

    proc = run_cli(["plex-check"], env=lib.env)

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["local_timed"] == 1
    assert all(r["path"] != "/library/streams/8" for r in lib.stub.requests)


def named_library(lib: Checked) -> None:
    lib.add(b"A/t.lrc", refreshed=False)
    lib.add(b"A/u.txt", Kind.TXT, refreshed=False)
    lib.add(b"A/CD2/v.lrc")
    lib.add(b"AB/t.lrc")
    lib.add(b"B/t.lrc")
    (lib.lib / "A" / "CD2").mkdir(parents=True)
    (lib.lib / "Empty").mkdir()


@pytest.mark.parametrize(
    ("target", "checked"),
    [
        ("A", 3),
        ("A/", 3),
        ("A/CD2", 1),
        ("A/t.flac", 1),
        ("A/u.mp3", 1),
        ("A/t.lrc", 1),
        ("A/u.txt", 1),
        (".", 5),
    ],
)
def test_check_a_named_path_checks_what_is_published_there(
    lib: Checked, run_cli: RunCli, target: str, checked: int
) -> None:
    named_library(lib)

    proc = run_cli(["plex-check", lib.lib / target], env=lib.env)

    assert proc.returncode == 0, proc.stderr
    counts = parse_counts(proc.stdout)
    assert (counts["checked"], counts["passed"]) == (checked, checked)


def test_check_a_named_folder_ignores_whether_a_refresh_is_pending(
    lib: Checked, run_cli: RunCli
) -> None:
    named_library(lib)

    proc = run_cli(["plex-check", lib.lib / "A", "--verbose"], env=lib.env)

    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if b"=" not in line]
    assert lines == [
        b"A/CD2/v.lrc local_timed",
        b"A/t.lrc local_timed",
        b"A/u.txt local_untimed",
    ]


def test_check_a_path_outside_the_music_root_exits_2(
    lib: Checked, run_cli: RunCli, tmp_path: Path
) -> None:
    named_library(lib)
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    proc = run_cli(["plex-check", outside], env=lib.env)

    assert proc.returncode == 2, proc.stderr
    assert b"music_root" in proc.stderr
    assert lib.stub.requests == []


@pytest.mark.parametrize("target", ["Empty", "Missing", "A/none.flac"])
def test_check_a_path_with_nothing_published_exits_2_with_one_line(
    lib: Checked, run_cli: RunCli, target: str
) -> None:
    named_library(lib)

    proc = run_cli(["plex-check", lib.lib / target], env=lib.env)

    assert proc.returncode == 2, proc.stderr
    assert len(proc.stderr.splitlines()) == 1
    assert lib.stub.requests == []


def test_check_with_nothing_refreshed_yet_says_to_refresh_first(
    lib: Checked, run_cli: RunCli
) -> None:
    lib.add(b"A/t.lrc", refreshed=False)

    proc = run_cli(["plex-check"], env=lib.env)

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["checked"] == 0
    lines = proc.stderr.splitlines()
    assert len(lines) == 1
    assert b"plex-refresh" in lines[0]
    assert lib.stub.requests == []


def test_check_with_no_ledger_creates_none(lib: Checked, run_cli: RunCli) -> None:
    proc = run_cli(["plex-check"], env=lib.env)

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["checked"] == 0
    assert not lib.ledger.exists()


def test_check_waits_its_turn_without_hanging(lib: Checked, run_cli: RunCli) -> None:
    lib.add(b"A/t.lrc")
    holder = db.connect(lib.ledger)
    try:
        started = time.monotonic()
        proc = run_cli(["plex-check"], env=lib.env)
        took = time.monotonic() - started
    finally:
        holder.close()

    assert proc.returncode == 75, proc.stderr
    assert took < 15
    assert lib.stub.requests == []


@pytest.mark.parametrize("args", [(), ("--verbose",), ("--sample", "1")])
def test_check_leaves_the_ledger_bytes_alone(
    lib: Checked, run_cli: RunCli, args: tuple[str, ...]
) -> None:
    lib.add(b"A/t.lrc")
    lib.add(b"B/t.lrc")
    lib.answer(b"B/t.lrc", [])
    before = lib.sha()

    proc = run_cli(["plex-check", *args], env=lib.env)

    assert proc.returncode in (0, 3), proc.stderr
    assert lib.sha() == before


def test_check_with_no_plex_url_exits_2_naming_it(lib: Checked, run_cli: RunCli) -> None:
    lib.add(b"A/t.lrc")
    del lib.env["PLYRIX_PLEX_URL"]

    proc = run_cli(["plex-check"], env=lib.env)

    assert proc.returncode == 2, proc.stderr
    assert b"plex_url (PLYRIX_PLEX_URL)" in proc.stderr


def test_check_with_no_plex_section_exits_2_naming_it(lib: Checked, run_cli: RunCli) -> None:
    lib.add(b"A/t.lrc")
    del lib.env["PLYRIX_PLEX_SECTION"]

    proc = run_cli(["plex-check"], env=lib.env)

    assert proc.returncode == 2, proc.stderr
    assert b"plex_section (PLYRIX_PLEX_SECTION)" in proc.stderr


EXIT_CASES = {
    "sections 500": (1, b"plex_url (PLYRIX_PLEX_URL)"),
    "sections 401": (1, b"plex_token (PLYRIX_PLEX_TOKEN)"),
    "section not music": (2, b"plex_section (PLYRIX_PLEX_SECTION)"),
    "section not listed": (2, b"plex_section (PLYRIX_PLEX_SECTION)"),
    "listing 500": (1, b"plex_url (PLYRIX_PLEX_URL)"),
    "listing 404": (2, b"plex_section (PLYRIX_PLEX_SECTION)"),
    "metadata unreadable": (1, b"plex_url (PLYRIX_PLEX_URL)"),
}


@pytest.mark.parametrize("case", EXIT_CASES)
def test_check_exit_codes_when_plex_fails(lib: Checked, run_cli: RunCli, case: str) -> None:
    lib.add(b"A/t.lrc")
    if case == "section not music":
        lib.stub.sections = [plex_section_dir(SECTION, "movie", lib.stub.location)]
    elif case == "section not listed":
        lib.stub.sections = [plex_section_dir("2", "artist", lib.stub.location)]
    elif case.startswith("sections"):
        lib.stub.sections_status = int(case.split()[1])
    elif case.startswith("listing"):
        lib.stub.listing_status = int(case.split()[1])
    else:
        lib.stub.metadata.clear()
        lib.stub.tracks[0]["ratingKey"] = "9999"
        lib.stub.metadata["9999"] = b'{"MediaContainer": '
    code, words = EXIT_CASES[case]

    proc = run_cli(["plex-check"], env=lib.env)

    assert proc.returncode == code, proc.stderr
    assert words in proc.stderr
    assert DUMMY_TOKEN.encode() not in proc.stdout + proc.stderr


def test_check_verbose_prints_escaped_paths_and_verdicts_only(
    lib: Checked, run_cli: RunCli
) -> None:
    lib.add(b"Caf\xe9/t.lrc")
    lib.add(b"B/t.lrc")
    lib.answer(b"B/t.lrc", [plex_lyric_stream(9, LYRICFIND, "lrc", timed="1")])

    proc = run_cli(["plex-check", "--verbose"], env=lib.env)

    assert proc.returncode == 3, proc.stderr
    lines = sorted(line for line in proc.stdout.splitlines() if b"=" not in line)
    assert lines == [b"B/t.lrc provider", show(b"Caf\xe9/t.lrc").encode() + b" local_timed"]
    assert b"Fixture" not in proc.stdout + proc.stderr
    assert lib.real not in proc.stdout


@pytest.mark.parametrize("failing", [False, True])
def test_check_report_holds_counts_and_run_facts_only(
    lib: Checked, run_cli: RunCli, tmp_path: Path, failing: bool
) -> None:
    lib.add(b"A/t.lrc")
    if failing:
        lib.answer(b"A/t.lrc", [])
    out = tmp_path / "out" / "check.json"
    out.parent.mkdir()

    proc = run_cli(["plex-check", "--report", str(out)], env=lib.env)

    assert proc.returncode == (3 if failing else 0), proc.stderr
    data = out.read_bytes()
    report = json.loads(data)
    assert set(report) == REPORT_KEYS
    assert report["command"] == "plex-check"
    assert report["end_reason"] == "finished"
    assert set(report["counts"]) == {f.name for f in dataclasses.fields(refresh.CheckCounts)}
    assert report["counts"]["checked"] == 1
    assert b"127.0.0.1" not in data
    assert DUMMY_TOKEN.encode() not in data
    assert lib.real not in data


def test_check_report_after_plex_failed_says_plex_unavailable(
    lib: Checked, run_cli: RunCli, tmp_path: Path
) -> None:
    lib.add(b"A/t.lrc")
    lib.stub.sections_status = 500
    out = tmp_path / "check.json"

    proc = run_cli(["plex-check", "--report", str(out)], env=lib.env)

    assert proc.returncode == 1, proc.stderr
    assert json.loads(out.read_bytes())["end_reason"] == "plex_unavailable"


def test_check_report_refuses_a_path_in_the_library(lib: Checked, run_cli: RunCli) -> None:
    lib.add(b"A/t.lrc")

    proc = run_cli(["plex-check", "--report", str(lib.lib / "r.json")], env=lib.env)

    assert proc.returncode == 2, proc.stderr
    assert lib.stub.requests == []


README = Path(__file__).parent.parent / "README.md"


def test_readme_plex_check_names_every_count_and_exit_code() -> None:
    readme = README.read_text(encoding="utf-8")
    section = readme.split("\n## Plex\n", 1)[1].split("\n## ", 1)[0]
    check = section.split("### Checking that lyrics attached", 1)[1]

    missing = {
        f.name for f in dataclasses.fields(refresh.CheckCounts) if f"`{f.name}`" not in check
    }
    assert missing == set()
    verdicts = {f"`{verdict.value}`" for verdict in plex.Verdict}
    assert {v for v in verdicts if v not in check} == set()
    codes = check.split("### Plex check exit codes", 1)[1].split("\n#", 1)[0]
    assert set(re.findall(r"^\| (\d+) \|", codes, re.MULTILINE)) == {"0", "1", "2", "3", "75"}
