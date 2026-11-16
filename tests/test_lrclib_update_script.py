"""Prove scripts/lrclib-update.sh end to end on invented LRCLIB-shaped databases.

The listing and the dumps come from a local DumpServer, and LRCLIB's stop and start are fakes that
log what they were asked to do. plyrix is the real console script, so its check, verify and record
run for real against a state database under tmp_path. No real listing or dump is ever fetched, and
no container is stopped or started.
"""

import gzip
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import StubLrclib, copy_track, snap
from lrclib_fixture import (  # noqa: F401
    PROBE,
    DumpServer,
    corrupt_page,
    drop_index,
    dump_server,
    gz_bytes,
    listed,
    listing,
    lyric_pages,
    make_db,
)
from plyrix.state import db, lrclib_db

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lrclib-update.sh"
PLYRIX = Path(sys.executable).parent / "plyrix"
OLD = "lrclib-db-dump-20261030T182259Z.sqlite3.gz"
NEW = "lrclib-db-dump-20261201T000000Z.sqlite3.gz"
OLDER = "lrclib-db-dump-20260901T000000Z.sqlite3.gz"

pytestmark = pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not on PATH")


class Update:
    """LRCLIB's data folder on an old dump, a newer dump served locally, fakes, and plyrix."""

    def __init__(self, tmp_path: Path, stub: StubLrclib, server: DumpServer) -> None:
        self.tmp = tmp_path
        self.server = server
        self.data = tmp_path / "data"
        self.data.mkdir()
        self.db = self.data / "db.sqlite3"
        make_db(self.db, 50)
        (self.data / "db.sqlite3-wal").write_bytes(b"old wal")
        (self.data / "db.sqlite3-shm").write_bytes(b"old shm")
        index = self.data / "db.sqlite3.tantivy"
        index.mkdir()
        (index / "meta.json").write_bytes(b"{}")
        self.new_db = make_db(tmp_path / "new.sqlite3", 80)
        server.offer(NEW, gz_bytes(self.new_db))
        self.lib = tmp_path / "lib"
        copy_track(self.lib)
        self.state_db = tmp_path / "state" / "state.db"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.log = tmp_path / "events.log"
        record = (
            'printf "%s staged=%s\\n" "{event}" '
            f'"$([ -e "{self.db}.staged" ] && echo yes || echo no)" >>"$EVENTS"'
        )
        self.stop = self.fake("stop", record.replace("{event}", "stop"))
        self.start = self.fake("start", record.replace("{event}", "start"))
        self.env = {
            **{k: v for k, v in os.environ.items() if not k.startswith(("PLYRIX", "LRCLIB_"))},
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
            "EVENTS": str(self.log),
            "LRCLIB_DATA": str(self.data),
            "PLYRIX": str(PLYRIX),
            "LRCLIB_DUMP_LISTING": server.listing_url,
            "LRCLIB_DUMP_BASE": server.base_url,
            "LRCLIB_STOP": str(self.stop),
            "LRCLIB_START": str(self.start),
            "LRCLIB_URL": stub.base_url,
            "LRCLIB_POLL_SECONDS": "0.05",
            "LRCLIB_READY_TIMEOUT": "30",
            "LRCLIB_RETRY_SECONDS": "0",
            "PLYRIX_MUSIC_ROOT": str(self.lib),
            "PLYRIX_LRCLIB_URL": stub.base_url,
            "PLYRIX_STATE_DB": str(self.state_db),
            "PLYRIX_DUMP_ID": OLD,
        }

    def fake(self, name: str, body: str) -> Path:
        path = self.bin / name
        path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def shim(self, subcommand: str, reply: str, code: int = 0) -> Path:
        """Return a PLYRIX answering one subcommand with reply, and running the real one else."""
        return self.fake(
            f"plyrix-{subcommand}",
            f'if [ "$1" = {subcommand} ]; then\n  printf "%s\\n" "{reply}"\n  exit {code}\nfi\n'
            f'exec "{PLYRIX}" "$@"',
        )

    def run(self, *args: str, **env: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            [str(SCRIPT), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    def events(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def data_snaps(self) -> dict[str, tuple[str, int, int, int, int, int, int]]:
        return {
            str(path.relative_to(self.data)): snap(path)
            for path in sorted(self.data.rglob("*"))
            if path.name != ".plyrix-update.lock"
        }

    def lib_snaps(self) -> dict[str, tuple[str, int, int, int, int, int, int]]:
        return {str(path.relative_to(self.lib)): snap(path) for path in sorted(self.lib.rglob("*"))}

    def remember_old(self) -> None:
        """Record the old dump live with its watermarks, as an earlier update would have.

        The diff then needs no bootstrap read of the live database, which, as SQLite's normal
        reader, maintains its -shm, so a test can hold every old file to its snap.
        """
        conn = db.connect(self.state_db)
        try:
            db.record_live(conn, OLD, "2026-10-30T18:22:59+00:00", None)
            marks = lrclib_db.watermarks(self.db)
            digest = lrclib_db.id_digest(self.db, marks.max_track_id)
            db.set_watermarks(conn, OLD, *marks, digest)
        finally:
            conn.close()

    def diff_of(self, name: str) -> tuple[str, int]:
        con = sqlite3.connect(self.state_db)
        try:
            state = con.execute(
                "SELECT diff_state FROM lrclib_dumps WHERE name = ?", (name,)
            ).fetchone()[0]
            count = con.execute(
                "SELECT count(*) FROM lrclib_changes WHERE dump = ?", (name,)
            ).fetchone()[0]
        finally:
            con.close()
        return state, count

    def dump_statuses(self) -> list[tuple[str, str]]:
        con = sqlite3.connect(self.state_db)
        try:
            return con.execute("SELECT name, status FROM lrclib_dumps ORDER BY name").fetchall()
        finally:
            con.close()


@pytest.fixture
def update(tmp_path: Path, stub_lrclib: StubLrclib, dump_server: DumpServer) -> Update:  # noqa: F811
    return Update(tmp_path, stub_lrclib, dump_server)


def test_a_newer_dump_is_loaded_and_recorded_live(update: Update) -> None:
    library = update.lib_snaps()

    proc = update.run()

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == f"lrclib-update: LRCLIB is ready with {NEW}"
    assert update.db.read_bytes() == update.new_db.read_bytes()
    leftovers = [
        p.name for p in update.data.iterdir() if ".staged" in p.name or ".previous" in p.name
    ]
    assert leftovers == []
    assert update.events() == ["stop staged=yes", "start staged=no"]
    # dump_id named the old dump, so the diff recorded it live first, read its watermarks from
    # the database LRCLIB served, and found the 30 new tracks.
    assert update.dump_statuses() == [(OLD, "previous"), (NEW, "live")]
    assert update.diff_of(NEW) == ("complete", 30)
    assert update.lib_snaps() == library
    # The download is deleted once LRCLIB serves it.
    assert not (update.data / "dl" / NEW).exists()


@pytest.mark.parametrize("listed_name", [OLD, OLDER])
def test_no_newer_dump_downloads_nothing(update: Update, listed_name: str) -> None:
    update.server.listing_body = listing(listed(listed_name, 1234))
    before = update.data_snaps()

    proc = update.run()

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["lrclib-update: no newer dump; nothing changed"]
    assert update.server.requests == [("GET", "/listing", "")]
    assert update.data_snaps() == before
    assert not update.state_db.exists()
    assert update.events() == []


def test_a_dump_plyrix_refuses_is_never_swapped_in(update: Update) -> None:
    bad = update.tmp / "bad.sqlite3"
    con = sqlite3.connect(bad)
    con.execute("CREATE TABLE tracks (id INTEGER PRIMARY KEY, last_lyrics_id INTEGER)")
    con.execute("INSERT INTO tracks(last_lyrics_id) VALUES(1)")
    con.execute("PRAGMA user_version=8")
    con.commit()
    con.close()
    update.server.offer(NEW, gz_bytes(bad))
    before = {
        name: snap(update.data / name)
        for name in ("db.sqlite3", "db.sqlite3-wal", "db.sqlite3-shm", "db.sqlite3.tantivy")
    }

    proc = update.run()

    assert proc.returncode == 1
    refused = [
        line
        for line in proc.stderr.splitlines()
        if line.startswith("plyrix: the staged database can't be served")
    ]
    assert len(refused) == 1, proc.stderr
    assert "tracks table" in refused[0]
    assert update.events() == []
    after = {name: snap(update.data / name) for name in before}
    assert after == before
    assert not (update.data / "db.sqlite3.staged").exists()


def test_the_script_parses_and_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)
    for script in (SCRIPT, ROOT / "scripts" / "lib" / "lrclib-dump.sh"):
        assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0


def test_an_argument_prints_usage_and_exits_2(update: Update) -> None:
    proc = update.run("x")

    assert proc.returncode == 2
    assert proc.stderr.startswith("usage:")
    assert update.server.requests == []


# The check stays safe when anything is off


@pytest.mark.parametrize(("status", "body"), [(500, b"oops"), (200, b"<html>a page</html>")])
def test_a_broken_listing_is_no_update(update: Update, status: int, body: bytes) -> None:
    update.server.listing_status = status
    update.server.listing_body = body
    before = update.data_snaps()

    proc = update.run()

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["lrclib-update: no newer dump; nothing changed"]
    assert len(proc.stderr.splitlines()) == 1
    assert update.server.dump_requests() == []
    assert update.data_snaps() == before
    assert update.events() == []


def test_a_second_update_waits_for_the_first(update: Update) -> None:
    lock = update.data / ".plyrix-update.lock"
    holder = subprocess.Popen(
        ["flock", str(lock), "-c", "echo held; sleep 60"], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline() == "held\n"
        before = update.data_snaps()

        proc = update.run()
    finally:
        holder.kill()
        holder.wait()

    assert proc.returncode == 75
    assert proc.stderr.splitlines() == [
        "lrclib-update: another update of LRCLIB_DATA is running, so nothing was done"
    ]
    assert update.server.requests == []
    assert update.data_snaps() == before
    assert lock.exists()


def test_a_busy_state_database_waits_before_anything_is_fetched(update: Update) -> None:
    conn = db.connect(update.state_db)
    try:
        before = update.data_snaps()

        proc = update.run()
    finally:
        conn.close()

    assert proc.returncode == 75, proc.stderr
    assert "another plyrix run is using the state database" in proc.stderr.splitlines()[-1]
    assert update.server.requests == []
    assert update.data_snaps() == before


def test_no_live_dump_known_exits_2_naming_dump_id(update: Update) -> None:
    env = {k: v for k, v in update.env.items() if k != "PLYRIX_DUMP_ID"}

    proc = subprocess.run(
        [str(SCRIPT)], env=env, capture_output=True, text=True, check=False, timeout=120
    )

    assert proc.returncode == 2
    assert "dump_id (PLYRIX_DUMP_ID)" in proc.stderr
    assert update.server.requests == []


@pytest.mark.parametrize(
    "line",
    [
        "update ../x 5",
        f"update ../{NEW} 5",
        f"update {NEW} 5 extra",
        f"update {NEW} -5",
        f"upgrade {NEW} 5",
        "update",
    ],
)
def test_a_line_lrclib_check_shouldnt_print_is_never_used(update: Update, line: str) -> None:
    shim = update.shim("lrclib-check", line)
    before = update.data_snaps()
    outside = sorted(p.name for p in update.tmp.iterdir())

    proc = update.run(PLYRIX=str(shim))

    assert proc.returncode == 1
    assert "can't use" in proc.stderr
    assert update.server.requests == []
    assert update.data_snaps() == before
    assert sorted(p.name for p in update.tmp.iterdir()) == outside
    assert update.events() == []


# The version guard, for one 64 KB read


def check_line(update: Update) -> str:
    """Return what lrclib-check prints for the update's listing, through the real plyrix."""
    proc = subprocess.run(
        [
            str(PLYRIX),
            "lrclib-check",
            "--listing",
            update.server.listing_url,
            "--base",
            update.server.base_url,
        ],
        env=update.env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return proc.stdout.strip()


def test_the_check_names_the_exact_size_the_staged_file_will_have(update: Update) -> None:
    line = check_line(update)
    update.server.requests.clear()

    proc = update.run()

    assert proc.returncode == 0, proc.stderr
    word, name, size, unpacked, version = line.split()
    assert (word, name, version) == ("update", NEW, "8")
    assert int(size) == len(update.server.dumps[NEW])
    assert int(unpacked) == update.db.stat().st_size == update.new_db.stat().st_size


@pytest.mark.parametrize("version", [9, 0])
def test_a_dump_too_new_for_the_pin_is_refused_for_one_64_kb_read(
    update: Update, version: int
) -> None:
    update.server.offer(NEW, gz_bytes(make_db(update.tmp / "v.sqlite3", 3, user_version=version)))
    before = update.data_snaps()

    proc = update.run()

    assert proc.returncode == 1
    assert f"user_version is {version}" in proc.stderr and "e1d863a" in proc.stderr
    assert proc.stderr.splitlines()[-1] == (
        "lrclib-update: the newest dump was refused, so nothing changed"
    )
    assert update.server.requests == [("GET", "/listing", ""), ("GET", f"/{NEW}", PROBE)]
    assert update.data_snaps() == before
    assert update.events() == []


def test_a_dump_that_isnt_sqlite_is_refused_for_one_64_kb_read(update: Update) -> None:
    update.server.offer(NEW, gzip.compress(b"not a database " * 1000))
    before = update.data_snaps()

    proc = update.run()

    assert proc.returncode == 1
    assert "doesn't hold an SQLite database" in proc.stderr
    assert update.server.downloads() == []
    assert update.data_snaps() == before


def test_a_version_the_server_migrates_is_loaded(update: Update) -> None:
    older = make_db(update.tmp / "v7.sqlite3", 6, user_version=7)
    update.server.offer(NEW, gz_bytes(older))

    proc = update.run()

    assert proc.returncode == 0, proc.stderr
    assert update.db.read_bytes() == older.read_bytes()


def test_a_header_probe_that_fails_is_no_update(update: Update) -> None:
    update.server.dump_status = 503
    before = update.data_snaps()

    proc = update.run()

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["lrclib-update: no newer dump; nothing changed"]
    assert len(proc.stderr.splitlines()) == 1 and "HTTP 503" in proc.stderr
    assert update.server.downloads() == []
    assert update.data_snaps() == before


# The full verify gates the swap, and a refused dump waits for the user


def serve_without_index(update: Update) -> None:
    update.server.offer(NEW, gz_bytes(drop_index(make_db(update.tmp / "noidx.sqlite3", 20))))


def test_a_refused_staged_database_never_stops_lrclib(update: Update) -> None:
    serve_without_index(update)
    names = ("db.sqlite3", "db.sqlite3-wal", "db.sqlite3-shm", "db.sqlite3.tantivy")
    before = {name: snap(update.data / name) for name in names}

    proc = update.run()

    assert proc.returncode == 1
    assert "no idx_tracks_last_lyrics_id index" in proc.stderr
    assert update.events() == []
    assert {name: snap(update.data / name) for name in names} == before
    assert not (update.data / "db.sqlite3.staged").exists()
    assert update.dump_statuses() == [(NEW, "refused")]
    kept = update.data / "dl" / NEW
    assert kept.exists()
    assert proc.stdout.strip().endswith(f"once you're done with it: rm {kept}")


def test_a_refused_dump_isnt_tried_again_until_asked(update: Update) -> None:
    serve_without_index(update)
    assert update.run().returncode == 1
    update.server.requests.clear()

    again = update.run()

    assert again.returncode == 1
    assert "isn't tried again by itself" in again.stderr
    assert update.server.requests == [("GET", "/listing", "")]

    update.server.requests.clear()
    retried = update.run(LRCLIB_UPDATE_RETRY="1")

    assert retried.returncode == 1
    # The kept download is reused: only the listing and the 64 KB probe were fetched.
    assert update.server.downloads() == []
    assert "no idx_tracks_last_lyrics_id index" in retried.stderr
    assert not (update.data / "db.sqlite3.staged").exists()


def test_a_verify_that_fails_without_refusing_records_nothing(update: Update) -> None:
    # Exit 1 with no "refused" line, as from a crash or a container that didn't start.
    shim = update.shim("lrclib-verify", "Traceback (most recent call last):", 1)
    before = {name: snap(update.data / name) for name in OLD_SET}

    proc = update.run(PLYRIX=str(shim))

    assert proc.returncode == 1
    assert "lrclib-verify failed" in proc.stderr.splitlines()[-1]
    assert update.events() == []
    assert {name: snap(update.data / name) for name in OLD_SET} == before
    assert not (update.data / "db.sqlite3.staged").exists()
    # Nothing was recorded, so the ledger may not exist yet.
    assert not update.state_db.exists() or (NEW, "refused") not in update.dump_statuses()


def test_quick_check_can_be_turned_off(update: Update) -> None:
    damaged = make_db(update.tmp / "damaged.sqlite3", 400)
    corrupt_page(damaged, lyric_pages(damaged)[3])
    update.server.offer(NEW, gz_bytes(damaged))

    refused = update.run()
    assert refused.returncode == 1
    assert "quick_check" in refused.stderr

    loaded = update.run(LRCLIB_QUICK_CHECK="0", LRCLIB_UPDATE_RETRY="1")

    assert loaded.returncode == 0, loaded.stderr
    assert update.db.read_bytes() == damaged.read_bytes()


@pytest.mark.parametrize("variable", ["LRCLIB_QUICK_CHECK", "LRCLIB_UPDATE_RETRY"])
def test_a_bad_switch_exits_2_before_any_request(update: Update, variable: str) -> None:
    proc = update.run(**{variable: "x"})

    assert proc.returncode == 2
    assert variable in proc.stderr
    assert update.server.requests == []


# Doctor checks the room before the download


@pytest.mark.parametrize("code", [1, 2])
def test_no_room_stops_the_update_before_the_download(update: Update, code: int) -> None:
    shim = update.shim("doctor", "problem lrclib_data: not enough space", code)
    before = update.data_snaps()

    proc = update.run(PLYRIX=str(shim))

    assert proc.returncode == code
    assert proc.stderr.splitlines()[-1] == "lrclib-update: doctor found a problem with LRCLIB_DATA"
    assert update.server.downloads() == []
    assert update.data_snaps() == before
    assert update.events() == []


# The journaled swap puts the old database back when any step fails (06-04)

OLD_SET = ("db.sqlite3", "db.sqlite3-wal", "db.sqlite3-shm", "db.sqlite3.tantivy")


def old_set(update: Update) -> dict[str, tuple[str, int, int, int, int, int, int]]:
    return {name: snap(update.data / name) for name in OLD_SET}


def old_bytes(update: Update) -> dict[str, bytes]:
    return {
        name: (update.data / name).read_bytes()
        for name in ("db.sqlite3", "db.sqlite3-wal", "db.sqlite3-shm")
    } | {"meta.json": (update.data / "db.sqlite3.tantivy" / "meta.json").read_bytes()}


def assert_rolled_back(update: Update, proc: "subprocess.CompletedProcess[str]", step: str) -> None:
    assert proc.returncode == 1
    last = proc.stderr.strip().splitlines()[-1]
    assert last.startswith(f"lrclib-update: the update to {NEW} was rolled back after {step}")
    assert "LRCLIB serves the previous database" in last
    assert [p.name for p in update.data.iterdir() if ".previous" in p.name] == []
    assert not (update.data / "db.sqlite3.staged").exists()
    assert update.dump_statuses() == [(OLD, "live"), (NEW, "rolled_back")]
    assert update.diff_of(NEW) == ("none", 0)
    journal = update.data / ".plyrix-update.journal"
    assert journal.read_text(encoding="utf-8") == f"rolled_back {NEW}\n"
    assert (update.data / "dl" / NEW).exists()


def test_a_failed_stop_changes_nothing(update: Update) -> None:
    update.remember_old()
    before = old_set(update)
    failing = update.fake("bad-stop", 'echo stop >>"$EVENTS"; exit 1')

    proc = update.run(LRCLIB_STOP=str(failing))

    assert proc.returncode == 1
    assert "nothing changed" in proc.stderr.strip().splitlines()[-1]
    assert update.events() == ["stop"]
    assert old_set(update) == before
    assert not (update.data / "db.sqlite3.staged").exists()
    assert not (update.data / ".plyrix-update.journal").exists()


def test_a_failed_start_puts_the_old_database_back(update: Update) -> None:
    update.remember_old()
    before = old_bytes(update)
    once = update.tmp / "started-once"
    start = update.fake(
        "start-once",
        f'echo start >>"$EVENTS"\nif [ ! -e "{once}" ]; then : >"{once}"; exit 1; fi',
    )
    stop = update.fake("stop-log", 'echo stop >>"$EVENTS"')

    proc = update.run(LRCLIB_START=str(start), LRCLIB_STOP=str(stop))

    assert update.events() == ["stop", "start", "stop", "start"]
    assert_rolled_back(update, proc, "start")
    assert old_bytes(update) == before


def test_a_get_that_never_answers_puts_the_old_database_back(
    update: Update, stub_lrclib: StubLrclib
) -> None:
    update.remember_old()
    before = old_bytes(update)
    stub_lrclib.preflight_id = (404, {"name": "TrackNotFound"})

    proc = update.run(LRCLIB_GET_TIMEOUT="1")

    assert_rolled_back(update, proc, "the wait for /api/get")
    assert old_bytes(update) == before
    assert update.events() == [
        "stop staged=yes",
        "start staged=no",
        "stop staged=no",
        "start staged=no",
    ]


def test_a_search_index_never_ready_puts_the_old_database_back(
    update: Update, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    update.remember_old()
    before = old_bytes(update)
    restored = update.tmp / "restored"
    count = update.tmp / "starts"
    # The old database's index answers once it is back, which the second start marks.
    start = update.fake(
        "start-count",
        f'echo x >>"{count}"\n[ "$(wc -l <"{count}")" -lt 2 ] || : >"{restored}"',
    )
    monkeypatch.setattr(
        stub_lrclib,
        "next_probe_search",
        lambda: (200, []) if restored.exists() else (500, {}),
    )

    proc = update.run(LRCLIB_START=str(start), LRCLIB_READY_TIMEOUT="1")

    assert_rolled_back(update, proc, "the wait for the search index")
    assert old_bytes(update) == before


def test_the_new_database_starts_with_no_index_and_the_old_wal_aside(update: Update) -> None:
    update.remember_old()
    seen = update.tmp / "seen"
    data = update.data
    old = f'"{data}/db.sqlite3.previous-wal" "{data}/db.sqlite3.previous-shm"'
    start = update.fake(
        "start-look",
        f'[ -e "{seen}" ] || {{ cat {old} >"{seen}"; '
        f'[ -e "{data}/db.sqlite3.tantivy" ] && echo index >>"{seen}"; }}; true',
    )

    proc = update.run(LRCLIB_START=str(start))

    assert proc.returncode == 0, proc.stderr
    assert seen.read_bytes() == b"old walold shm"


def test_a_success_probes_get_before_search_and_cleans_up(
    update: Update, stub_lrclib: StubLrclib
) -> None:
    proc = update.run()

    assert proc.returncode == 0, proc.stderr
    paths = [probe["path"] for probe in stub_lrclib.probes]
    assert paths[0] == "/api/get/80"
    assert paths.index("/api/get/80") < paths.index("/api/search")
    assert [p.name for p in update.data.iterdir() if ".previous" in p.name] == []
    assert not (update.data / "dl" / NEW).exists()
    assert not (update.data / ".plyrix-update.journal").exists()


def test_the_download_can_be_kept(update: Update) -> None:
    proc = update.run(LRCLIB_KEEP_DOWNLOAD="1")

    assert proc.returncode == 0, proc.stderr
    assert (update.data / "dl" / NEW).exists()


def test_a_rolled_back_dump_isnt_tried_again_until_asked(update: Update) -> None:
    update.remember_old()
    once = update.tmp / "started-once"
    failing = update.fake("start-once", f'if [ ! -e "{once}" ]; then : >"{once}"; exit 1; fi')
    assert update.run(LRCLIB_START=str(failing)).returncode == 1
    update.server.requests.clear()

    again = update.run()

    assert again.returncode == 1
    assert "rolled back on" in again.stderr
    assert update.server.requests == [("GET", "/listing", "")]

    update.server.requests.clear()
    retried = update.run(LRCLIB_UPDATE_RETRY="1")

    assert retried.returncode == 0, retried.stderr
    assert update.server.downloads() == []
    assert update.dump_statuses() == [(OLD, "previous"), (NEW, "live")]
    assert update.diff_of(NEW) == ("complete", 30)


def test_a_previous_database_left_behind_stops_the_swap(update: Update) -> None:
    (update.data / "db.sqlite3.previous").write_bytes(b"left from before")

    proc = update.run()

    assert proc.returncode == 1
    assert "db.sqlite3.previous" in proc.stderr.strip().splitlines()[-1]
    assert update.events() == []
    assert (update.data / "db.sqlite3.previous").read_bytes() == b"left from before"


# The diff runs between verify and the swap (06-05)


def logging_plyrix(update: Update, diff_body: str = "  :") -> Path:
    """Return a PLYRIX that logs each subcommand and its niceness, then runs the real one."""
    return update.fake(
        "plyrix-log",
        'echo "plyrix $1 nice=$(nice)" >>"$EVENTS"\n'
        f'if [ "$1" = lrclib-diff ]; then\n{diff_body}\nfi\n'
        f'exec "{PLYRIX}" "$@"',
    )


def test_the_diff_runs_idle_after_verify_and_before_stop(update: Update) -> None:
    update.remember_old()

    proc = update.run(PLYRIX=str(logging_plyrix(update)))

    assert proc.returncode == 0, proc.stderr
    events = update.events()
    kinds = [line.split()[1] if line.startswith("plyrix ") else line.split()[0] for line in events]
    assert kinds.index("lrclib-verify") < kinds.index("lrclib-diff") < kinds.index("stop")
    nice = {
        line.split()[1]: int(line.split("=")[1]) for line in events if line.startswith("plyrix ")
    }
    # nice -n19 adds 19 to the niceness the script started with, up to 19. A host renicer such as
    # ananicy can lower it afterwards, so the idle steps must only be nicer than the check.
    assert nice["lrclib-diff"] > nice["lrclib-check"] or nice["lrclib-check"] == 19
    assert nice["lrclib-verify"] > nice["lrclib-check"] or nice["lrclib-check"] == 19
    assert "lrclib-update: diff complete 30" in proc.stdout
    assert update.diff_of(NEW) == ("complete", 30)


def test_a_diff_that_fails_swaps_nothing(update: Update) -> None:
    update.remember_old()
    names = ("db.sqlite3", "db.sqlite3-wal", "db.sqlite3-shm", "db.sqlite3.tantivy")
    before = {name: snap(update.data / name) for name in names}

    proc = update.run(PLYRIX=str(logging_plyrix(update, "  exit 1")))

    assert proc.returncode == 1
    assert "nothing was swapped" in proc.stderr.strip().splitlines()[-1]
    assert [line for line in update.events() if not line.startswith("plyrix")] == []
    assert not (update.data / "db.sqlite3.staged").exists()
    assert {name: snap(update.data / name) for name in names} == before


def test_a_busy_ledger_at_the_diff_is_waited_for(update: Update) -> None:
    update.remember_old()
    count = update.tmp / "diff-tries"
    busy = (
        f'  echo x >>"{count}"\n'
        f'  if [ "$(wc -l <"{count}")" -le 2 ]; then echo busy >&2; exit 75; fi'
    )

    proc = update.run(
        PLYRIX=str(logging_plyrix(update, busy)),
        LRCLIB_STATE_WAIT="10",
        LRCLIB_POLL_SECONDS="0.05",
    )

    assert proc.returncode == 0, proc.stderr
    assert count.read_text(encoding="utf-8").count("x") == 3
    assert update.dump_statuses() == [(OLD, "previous"), (NEW, "live")]


def test_a_ledger_busy_past_the_wait_gives_75(update: Update) -> None:
    update.remember_old()

    proc = update.run(
        PLYRIX=str(logging_plyrix(update, "  exit 75")),
        LRCLIB_STATE_WAIT="0",
    )

    assert proc.returncode == 75
    assert [line for line in update.events() if not line.startswith("plyrix")] == []


def test_the_first_update_after_setup_bootstraps_or_retries_everything(update: Update) -> None:
    # The old database's newest track was published locally after its dump's date, so its
    # maxes can't be the dump's, and the diff can't be trusted: state none, retry everything.
    con = sqlite3.connect(update.db)
    con.execute("UPDATE tracks SET created_at = '2026-11-20 00:00:00' WHERE id = 50")
    con.commit()
    con.close()

    proc = update.run()

    assert proc.returncode == 0, proc.stderr
    assert "lrclib-update: diff none 0" in proc.stdout
    assert update.diff_of(NEW) == ("none", 0)


def test_a_bad_state_wait_exits_2(update: Update) -> None:
    proc = update.run(LRCLIB_STATE_WAIT="soon")

    assert proc.returncode == 2
    assert "LRCLIB_STATE_WAIT" in proc.stderr
