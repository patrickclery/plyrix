"""Prove scripts/lrclib-setup.sh on a synthetic mini database, served over file:// and local stubs.

No real dump is fetched and no container is started: doctor, LRCLIB's stop and start, and curl
are fakes or shims that log what they were asked to do. Every row in the mini database is invented.
"""

import gzip
import os
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import StubLrclib, snap

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lrclib-setup.sh"
NAME = "lrclib-db-dump-20261030T182259Z.sqlite3.gz"
OLD_DB = b"the old database, which a failed run must leave alone"
REAL_CURL = shutil.which("curl")
REAL_GZIP = shutil.which("gzip")

pytestmark = pytest.mark.skipif(REAL_CURL is None, reason="curl is not on PATH")


def mini_database(path: Path) -> bytes:
    """Write an SQLite file of 20,000 invented rows and return its bytes."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tracks (id INTEGER PRIMARY KEY, name TEXT, artist_name TEXT)")
    con.executemany(
        "INSERT INTO tracks (name, artist_name) VALUES (?, ?)",
        ((f"Invented Track {n}", f"Invented Artist {n % 97}") for n in range(20_000)),
    )
    con.commit()
    con.close()
    return path.read_bytes()


def random_database(path: Path, megabytes: int) -> bytes:
    """Write an SQLite file holding about this many megabytes of seeded random bytes."""
    rng = random.Random(20261111)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE blobs (id INTEGER PRIMARY KEY, data BLOB)")
    con.executemany(
        "INSERT INTO blobs (data) VALUES (?)",
        ((rng.randbytes(65536),) for _ in range(megabytes * 16)),
    )
    con.commit()
    con.close()
    return path.read_bytes()


# The curl shim fakes one failed download when CURL_FAKE_EXIT is set: it writes the first
# CURL_FAKE_BYTES of CURL_FAKE_SOURCE to the .part and exits with that code, as curl does when a
# transfer breaks part way. Every later call, and every call that isn't a download, is real curl.
# A call that throws its body away is the script's resume probe, which sends the same Range as a
# resume, so the shim marks it with a header for the fault server to tell the two apart.
CURL_SHIM = """\
printf "%s\\n" "$*" >>"$CURL_LOG"
case " $* " in
  *" -o /dev/null "*) exec "{real}" -H "X-Fault-Probe: 1" "$@" ;;
esac
case " $* " in
  *".part "*)
    if [ -n "${CURL_FAKE_EXIT:-}" ] && [ ! -e "$CURL_LOG.faked" ]; then
      : >"$CURL_LOG.faked"
      out=
      prev=
      for arg in "$@"; do [ "$prev" = -o ] && out=$arg; prev=$arg; done
      head -c "$CURL_FAKE_BYTES" "$CURL_FAKE_SOURCE" >"$out"
      echo "curl: ($CURL_FAKE_EXIT) injected failure" >&2
      exit "$CURL_FAKE_EXIT"
    fi
    ;;
esac
exec "{real}" "$@"
"""


# The gzip shim fakes `gzip -t` on the .part exiting with GZIP_FAKE_EXIT, as a missing gzip (127),
# one killed by a signal (128 and up) or a warning (2) would. Every other call is real gzip.
GZIP_SHIM = """\
if [ -n "${GZIP_FAKE_EXIT:-}" ] && [ "$1" = -t ] && [ "${2%.part}" != "$2" ]; then
  exit "$GZIP_FAKE_EXIT"
fi
exec "{real}" "$@"
"""


class Setup:
    """A data folder holding an old LRCLIB install, a served dump, fakes and a curl shim."""

    def __init__(self, tmp_path: Path, stub: StubLrclib) -> None:
        self.tmp = tmp_path
        self.data = tmp_path / "data"
        self.data.mkdir()
        self.served = tmp_path / "served"
        self.served.mkdir()
        self.db_bytes = mini_database(tmp_path / "mini.sqlite3")
        self.gz_bytes = gzip.compress(self.db_bytes)
        self.serve(self.gz_bytes)
        (self.data / "db.sqlite3").write_bytes(OLD_DB)
        index = self.data / "db.sqlite3.tantivy"
        index.mkdir()
        (index / "meta.json").write_bytes(b"{}")
        (self.data / "db.sqlite3-wal").write_bytes(b"old wal")
        (self.data / "db.sqlite3-shm").write_bytes(b"old shm")
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.log = tmp_path / "events.log"
        self.curl_log = tmp_path / "curl.log"
        self.doctor = self.fake(
            "doctor", 'printf "doctor %s\\n" "$*" >>"$EVENTS"\nexit "${DOCTOR_EXIT:-0}"'
        )
        db = self.data / "db.sqlite3"
        # Setup unpacks to db.sqlite3.staged since it moved onto the shared swap (06-04), so the
        # part= field says whether that file was there.
        record = (
            'printf "%s %s %s part=%s\\n" "$(date +%s%N)" "{event}" '
            f'"$(sha256sum "{db}" | cut -d" " -f1)" '
            f'"$([ -e "{db}.staged" ] && echo yes || echo no)" >>"$EVENTS"'
        )
        self.stop = self.fake("stop", record.replace("{event}", "stop"))
        self.start = self.fake("start", record.replace("{event}", "start"))
        self.fake("curl", CURL_SHIM.replace("{real}", str(REAL_CURL)))
        self.env = {
            **{k: v for k, v in os.environ.items() if not k.startswith(("PLYRIX", "LRCLIB_"))},
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "EVENTS": str(self.log),
            "CURL_LOG": str(self.curl_log),
            "LRCLIB_DATA": str(self.data),
            "PLYRIX": str(self.doctor),
            "LRCLIB_DUMP_BASE": self.served.as_uri(),
            "LRCLIB_STOP": str(self.stop),
            "LRCLIB_START": str(self.start),
            "LRCLIB_URL": stub.base_url,
            "LRCLIB_POLL_SECONDS": "0.05",
            "LRCLIB_READY_TIMEOUT": "30",
            "LRCLIB_RETRY_SECONDS": "0",
        }

    def fake(self, name: str, body: str) -> Path:
        path = self.bin / name
        path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def serve(self, payload: bytes) -> None:
        (self.served / NAME).write_bytes(payload)

    def use_big_dump(self, megabytes: int = 3) -> None:
        """Serve a dump of a few megabytes that gzip can't shrink, so a transfer has a middle."""
        (self.tmp / "mini.sqlite3").unlink()
        self.db_bytes = random_database(self.tmp / "mini.sqlite3", megabytes)
        self.gz_bytes = gzip.compress(self.db_bytes, compresslevel=1)
        self.serve(self.gz_bytes)

    def part(self) -> Path:
        return self.data / "dl" / f"{NAME}.part"

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

    def downloads(self) -> list[str]:
        """Return the curl calls that wrote the download, not a header or ready probe."""
        if not self.curl_log.exists():
            return []
        lines = self.curl_log.read_text(encoding="utf-8").splitlines()
        return [line for line in lines if f"{NAME}.part" in line]

    def old_install(self) -> dict[str, tuple[str, int, int, int, int, int, int]]:
        paths = ["db.sqlite3", "db.sqlite3.tantivy", "db.sqlite3.tantivy/meta.json"]
        paths += ["db.sqlite3-wal", "db.sqlite3-shm"]
        return {name: snap(self.data / name) for name in paths}


@pytest.fixture
def setup(tmp_path: Path, stub_lrclib: StubLrclib) -> Setup:
    return Setup(tmp_path, stub_lrclib)


def every_file(folder: Path) -> list[Path]:
    # The lock setup shares with lrclib-update.sh is made on the first run and never removed, as a
    # lock on a removed file guards nothing (06-04), so it isn't a change to the install.
    return sorted(path for path in folder.rglob("*") if path.name != ".plyrix-update.lock")


# Arguments and the data folder


@pytest.mark.parametrize(
    "args",
    [
        [],
        [NAME, NAME],
        ["../etc/passwd"],
        [f"{NAME}/x"],
        [f"x/{NAME}"],
        ["lrclib-db-dump-2026.sqlite3.gz"],
    ],
)
def test_a_bad_argument_exits_2_with_one_usage_line(setup: Setup, args: list[str]) -> None:
    before = every_file(setup.data)

    proc = setup.run(*args)

    assert proc.returncode == 2, proc.stderr
    assert len(proc.stderr.splitlines()) == 1
    assert proc.stderr.startswith("usage:")
    assert every_file(setup.data) == before
    assert setup.events() == []
    assert not setup.curl_log.exists()


def test_lrclib_data_unset_exits_2(setup: Setup) -> None:
    env = {k: v for k, v in setup.env.items() if k != "LRCLIB_DATA"}

    proc = subprocess.run(
        [str(SCRIPT), NAME], env=env, capture_output=True, text=True, check=False, timeout=60
    )

    assert proc.returncode == 2
    assert "LRCLIB_DATA" in proc.stderr
    assert setup.events() == []


def test_lrclib_data_not_a_folder_exits_2(setup: Setup) -> None:
    proc = setup.run(NAME, LRCLIB_DATA=str(setup.tmp / "missing"))

    assert proc.returncode == 2
    assert "LRCLIB_DATA" in proc.stderr
    assert not (setup.tmp / "missing").exists()
    assert setup.events() == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write in any folder")
def test_lrclib_data_not_writable_exits_2(setup: Setup) -> None:
    before = every_file(setup.data)
    setup.data.chmod(0o555)
    try:
        proc = setup.run(NAME)
    finally:
        setup.data.chmod(0o755)

    assert proc.returncode == 2
    assert "LRCLIB_DATA" in proc.stderr
    assert every_file(setup.data) == before
    assert setup.events() == []


# Doctor first


def test_a_failing_doctor_stops_the_script_before_any_download(setup: Setup) -> None:
    before = every_file(setup.data)

    proc = setup.run(NAME, DOCTOR_EXIT="1")

    assert proc.returncode == 1
    assert setup.events() == [f"doctor doctor --lrclib-data {setup.data} --dump {NAME}"]
    assert setup.downloads() == []
    assert every_file(setup.data) == before


def test_doctor_exit_2_is_passed_on(setup: Setup) -> None:
    proc = setup.run(NAME, DOCTOR_EXIT="2")

    assert proc.returncode == 2
    assert setup.downloads() == []


def test_plyrix_is_split_into_words_for_a_container_command(setup: Setup) -> None:
    proc = setup.run(NAME, PLYRIX=f"{setup.doctor} extra-arg", DOCTOR_EXIT="1")

    assert proc.returncode == 1
    assert setup.events() == [f"doctor extra-arg doctor --lrclib-data {setup.data} --dump {NAME}"]


# A whole run


def test_a_good_dump_is_swapped_in_and_lrclib_waited_for(
    setup: Setup, stub_lrclib: StubLrclib
) -> None:
    old_sha = snap(setup.data / "db.sqlite3")[0]
    stub_lrclib.preflight_search_queue = [(500, {}), (500, {})]

    proc = setup.run(NAME)

    assert proc.returncode == 0, proc.stderr
    assert (setup.data / "db.sqlite3").read_bytes() == setup.db_bytes
    for gone in ("db.sqlite3.tantivy", "db.sqlite3-wal", "db.sqlite3-shm"):
        assert not (setup.data / gone).exists()
    assert [path for path in every_file(setup.data) if path.name.endswith(".part")] == []
    leftovers = [p for p in every_file(setup.data) if ".staged" in p.name or ".previous" in p.name]
    assert leftovers == []
    assert (setup.data / "dl" / NAME).read_bytes() == setup.gz_bytes
    assert (setup.data / "tmp").is_dir()

    events = setup.events()
    assert events[0] == f"doctor doctor --lrclib-data {setup.data} --dump {NAME}"
    # The doctor fake stands in for plyrix, so setup's live record logs a line too (06-04).
    assert events[-1] == f"doctor lrclib-record {NAME} --status live"
    stop, start = (line.split() for line in events[1:-1])
    assert stop[1:] == ["stop", old_sha, "part=yes"]
    assert start[1:] == ["start", snap(setup.data / "db.sqlite3")[0], "part=no"]
    assert int(stop[0]) <= int(start[0])

    assert [probe["path"] for probe in stub_lrclib.probes] == ["/api/search"] * 3
    last = proc.stdout.splitlines()[-2:]
    assert NAME in last[0] and "dump_id" in last[0]
    assert f"rm {setup.data / 'dl' / NAME}" in last[1]
    assert len(setup.downloads()) == 1


# Resuming


def test_a_half_written_download_resumes_byte_identically(setup: Setup) -> None:
    (setup.data / "dl").mkdir()
    half = setup.gz_bytes[: len(setup.gz_bytes) // 2]
    (setup.data / "dl" / f"{NAME}.part").write_bytes(half)

    proc = setup.run(NAME)

    assert proc.returncode == 0, proc.stderr
    assert (setup.data / "dl" / NAME).read_bytes() == setup.gz_bytes
    assert (setup.data / "db.sqlite3").read_bytes() == setup.db_bytes
    assert len(setup.downloads()) == 1
    assert "-C -" in setup.downloads()[0]


def test_a_part_already_complete_skips_curl(setup: Setup) -> None:
    (setup.data / "dl").mkdir()
    (setup.data / "dl" / f"{NAME}.part").write_bytes(setup.gz_bytes)

    proc = setup.run(NAME)

    assert proc.returncode == 0, proc.stderr
    assert setup.downloads() == []
    assert (setup.data / "dl" / NAME).read_bytes() == setup.gz_bytes
    assert (setup.data / "db.sqlite3").read_bytes() == setup.db_bytes


def test_a_download_already_finished_is_used_without_curl(setup: Setup) -> None:
    (setup.data / "dl").mkdir()
    (setup.data / "dl" / NAME).write_bytes(setup.gz_bytes)
    (setup.served / NAME).unlink()

    proc = setup.run(NAME)

    assert proc.returncode == 0, proc.stderr
    assert setup.downloads() == []
    assert (setup.data / "db.sqlite3").read_bytes() == setup.db_bytes


@pytest.fixture
def no_range_server(setup: Setup) -> Iterator[str]:
    """Serve the dump over HTTP, answering every request with the whole file and no Range."""
    payload = setup.gz_bytes

    class Handler(BaseHTTPRequestHandler):
        def headers_only(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()

        def do_HEAD(self) -> None:
            self.headers_only()

        def do_GET(self) -> None:
            self.headers_only()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_a_server_that_cant_resume_keeps_the_partial_and_says_how_to_start_again(
    setup: Setup, no_range_server: str
) -> None:
    (setup.data / "dl").mkdir()
    half = setup.gz_bytes[: len(setup.gz_bytes) // 2]
    setup.part().write_bytes(half)
    before = setup.old_install()

    proc = setup.run(NAME, LRCLIB_DUMP_BASE=no_range_server)

    assert proc.returncode == 1, proc.stderr
    assert setup.part().read_bytes() == half
    last = proc.stderr.strip().splitlines()[-1]
    assert "can't resume" in last and "kept" in last and f"rm {setup.part()}" in last
    assert len(setup.downloads()) == 1
    assert setup.old_install() == before

    # Deleting it as the message says downloads from zero, which needs no range support.
    setup.part().unlink()
    again = setup.run(NAME, LRCLIB_DUMP_BASE=no_range_server)

    assert again.returncode == 0, again.stderr
    assert (setup.data / "dl" / NAME).read_bytes() == setup.gz_bytes
    assert (setup.data / "db.sqlite3").read_bytes() == setup.db_bytes


@dataclass
class Served:
    """One GET the fault server answered: what it was, the offset asked for, status, bytes sent."""

    kind: str
    start: int
    status: int
    sent: int


class FaultServer:
    """Serve the dump over HTTP with Range support, following a script of faults.

    HEAD always answers 200 with Accept-Ranges and the size, or head_length when that is set. A
    GET the curl shim marks is the script's resume probe, answered with headers only: 206 from
    the asked offset, or 200 when probe is "whole". With closed_only, any open-ended Range (as
    curl -C - sends) gets 200 with the whole file, and only a bounded one gets 206, as some
    proxies do. Every other GET is a download and takes the next step of plan, then "ok" once
    plan is empty:
      ("ok",)            serve it: 206 from the asked offset, or 200 for the whole file
      ("drop", n)        the same headers, then n bytes and a closed connection (curl exit 18)
      ("whole",)         ignore the Range and send 200 with the whole file (curl exit 33)
      ("status", code)   answer with that status and no body
    """

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.plan: list[tuple[str, int] | tuple[str]] = []
        self.probe = "honest"
        self.head_length: str | None = None
        self.closed_only = False
        self.log: list[Served] = []
        self.lock = threading.Lock()

    def downloads(self) -> list[Served]:
        return [entry for entry in self.log if entry.kind == "download"]

    def probes(self) -> list[Served]:
        return [entry for entry in self.log if entry.kind == "probe"]


@pytest.fixture
def fault_server(setup: Setup) -> Iterator[tuple[FaultServer, str]]:
    setup.use_big_dump()
    faults = FaultServer(setup.gz_bytes)
    payload = faults.payload
    asked = re.compile(r"bytes=(\d+)-(\d*)")

    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", faults.head_length or str(len(payload)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

        def send(self, entry: Served, start: int, limit: int | None = None) -> None:
            body = payload[start:]
            self.send_response(entry.status)
            if entry.status == 206:
                self.send_header(
                    "Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}"
                )
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            chunk = body if limit is None else body[:limit]
            # Counted before any byte leaves, so a test that reads the log once the script has
            # exited never sees a count this thread hasn't written yet.
            entry.sent = len(chunk)
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except OSError:
                entry.sent = 0
                return
            if limit is not None:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True

        def logged(self, kind: str, start: int, status: int) -> Served:
            """Log a request before answering it, so the log keeps the order requests came in.

            Logged after its answer, a request on a thread descheduled once the socket had shut
            could land after the retry that the shutdown set off."""
            entry = Served(kind, start, status, 0)
            faults.log.append(entry)
            return entry

        def do_GET(self) -> None:
            found = asked.fullmatch(self.headers.get("Range", ""))
            start = int(found.group(1)) if found else 0
            kind = "probe" if self.headers.get("X-Fault-Probe") else "download"
            limit: int | None = 0
            bodiless = False
            with faults.lock:
                if found and faults.closed_only:
                    status = 206 if found.group(2) else 200
                elif kind == "probe":
                    status = 200 if faults.probe == "whole" or not found else 206
                else:
                    step = faults.plan.pop(0) if faults.plan else ("ok",)
                    if step[0] == "status":
                        status, bodiless = step[1], True  # type: ignore[misc]
                    elif step[0] == "whole" and found:
                        status, limit = 200, None
                    else:
                        status = 206 if found else 200
                        limit = step[1] if step[0] == "drop" else None  # type: ignore[misc]
                entry = self.logged(kind, start, status)
            if bodiless:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send(entry, start if status == 206 else 0, limit)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield faults, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def kept_bytes(faults: FaultServer) -> int:
    """Bytes the download kept: every body served from the offset asked for."""
    return sum(
        entry.sent
        for entry in faults.downloads()
        if entry.status == 206 or (entry.status == 200 and entry.start == 0)
    )


def test_a_download_that_keeps_breaking_resumes_to_a_byte_identical_file(
    setup: Setup, fault_server: tuple[FaultServer, str]
) -> None:
    faults, url = fault_server
    fifth = len(setup.gz_bytes) // 5
    faults.plan = [("drop", fifth), ("status", 503), ("drop", fifth), ("whole",), ("drop", fifth)]

    proc = setup.run(NAME, LRCLIB_DUMP_BASE=url)

    assert proc.returncode == 0, proc.stderr
    assert (setup.data / "dl" / NAME).read_bytes() == setup.gz_bytes
    assert (setup.data / "db.sqlite3").read_bytes() == setup.db_bytes
    starts = [entry.start for entry in faults.downloads()]
    # Never from zero again: each retry asks for the bytes after those already on disk.
    assert starts == [0, fifth, fifth, 2 * fifth, 2 * fifth, 3 * fifth]
    assert kept_bytes(faults) == len(setup.gz_bytes)
    # The one exit 33 was checked with a fresh range request, which the server honoured.
    assert [(entry.start, entry.status) for entry in faults.probes()] == [(2 * fifth, 206)]
    assert "curl exit 33" in proc.stderr
    assert len(setup.downloads()) == 6


@pytest.mark.parametrize("code", [92, 56, 28, 18, 16])
def test_a_transient_curl_failure_is_retried_from_where_it_stopped(
    setup: Setup, fault_server: tuple[FaultServer, str], code: int
) -> None:
    faults, url = fault_server
    third = len(setup.gz_bytes) // 3

    proc = setup.run(
        NAME,
        LRCLIB_DUMP_BASE=url,
        CURL_FAKE_EXIT=str(code),
        CURL_FAKE_BYTES=str(third),
        CURL_FAKE_SOURCE=str(setup.served / NAME),
    )

    assert proc.returncode == 0, proc.stderr
    assert (setup.data / "dl" / NAME).read_bytes() == setup.gz_bytes
    assert f"curl exit {code}" in proc.stderr
    assert [entry.start for entry in faults.downloads()] == [third]
    assert len(setup.downloads()) == 2


def test_retries_stop_after_the_limit_and_keep_the_partial(
    setup: Setup, fault_server: tuple[FaultServer, str]
) -> None:
    faults, url = fault_server
    (setup.data / "dl").mkdir()
    half = setup.gz_bytes[: len(setup.gz_bytes) // 2]
    setup.part().write_bytes(half)
    faults.plan = [("status", 503)] * 10
    before = setup.old_install()

    proc = setup.run(NAME, LRCLIB_DUMP_BASE=url, LRCLIB_DOWNLOAD_RETRIES="3")

    assert proc.returncode == 1, proc.stderr
    assert [entry.status for entry in faults.downloads()] == [503, 503, 503]
    assert setup.part().read_bytes() == half
    last = proc.stderr.strip().splitlines()[-1]
    assert "3 times in a row" in last and "kept" in last and "run again" in last
    assert setup.old_install() == before

    # The rerun the message names resumes the same partial.
    faults.plan = []
    again = setup.run(NAME, LRCLIB_DUMP_BASE=url)

    assert again.returncode == 0, again.stderr
    assert (setup.data / "dl" / NAME).read_bytes() == setup.gz_bytes
    assert faults.downloads()[-1].start == len(half)


def test_a_missing_dump_is_not_retried(setup: Setup, fault_server: tuple[FaultServer, str]) -> None:
    faults, url = fault_server
    faults.plan = [("status", 404)]

    proc = setup.run(NAME, LRCLIB_DUMP_BASE=url)

    assert proc.returncode == 1
    assert len(faults.downloads()) == 1
    assert "HTTP 404" in proc.stderr


def test_one_exit_33_never_deletes_the_partial(
    setup: Setup, fault_server: tuple[FaultServer, str]
) -> None:
    faults, url = fault_server
    (setup.data / "dl").mkdir()
    half = setup.gz_bytes[: len(setup.gz_bytes) // 2]
    setup.part().write_bytes(half)
    faults.plan = [("whole",)]

    proc = setup.run(NAME, LRCLIB_DUMP_BASE=url)

    assert proc.returncode == 0, proc.stderr
    assert [entry.start for entry in faults.downloads()] == [len(half), len(half)]
    assert [entry.status for entry in faults.downloads()] == [200, 206]
    assert kept_bytes(faults) == len(setup.gz_bytes) - len(half)
    assert (setup.data / "dl" / NAME).read_bytes() == setup.gz_bytes


@pytest.mark.parametrize("head_length", ["0", "123, 123", "0000"])
@pytest.mark.parametrize("partial", [True, False])
def test_a_head_size_that_isnt_a_positive_number_is_no_size(
    setup: Setup, fault_server: tuple[FaultServer, str], head_length: str, partial: bool
) -> None:
    """A HEAD saying 0, or something that isn't a number, must not get the partial deleted."""
    faults, url = fault_server
    faults.head_length = head_length
    (setup.data / "dl").mkdir()
    half = setup.gz_bytes[: len(setup.gz_bytes) // 2]
    if partial:
        setup.part().write_bytes(half)

    proc = setup.run(NAME, LRCLIB_DUMP_BASE=url)

    assert proc.returncode == 0, proc.stderr
    assert (setup.data / "dl" / NAME).read_bytes() == setup.gz_bytes
    assert [entry.start for entry in faults.downloads()] == [len(half) if partial else 0]


def test_the_resume_probe_asks_for_the_same_open_range_as_a_resume(
    setup: Setup, fault_server: tuple[FaultServer, str]
) -> None:
    """A proxy that honours only a bounded range must be found out, not retried for ever."""
    faults, url = fault_server
    faults.closed_only = True
    (setup.data / "dl").mkdir()
    half = setup.gz_bytes[: len(setup.gz_bytes) // 2]
    setup.part().write_bytes(half)

    proc = setup.run(NAME, LRCLIB_DUMP_BASE=url, LRCLIB_DOWNLOAD_RETRIES="3")

    assert proc.returncode == 1, proc.stderr
    assert setup.part().read_bytes() == half
    last = proc.stderr.strip().splitlines()[-1]
    assert "can't resume" in last and f"rm {setup.part()}" in last
    assert [(entry.start, entry.status) for entry in faults.probes()] == [(len(half), 200)]


def test_retries_that_run_out_on_exit_33_also_name_rm(
    setup: Setup, fault_server: tuple[FaultServer, str]
) -> None:
    faults, url = fault_server
    (setup.data / "dl").mkdir()
    half = setup.gz_bytes[: len(setup.gz_bytes) // 2]
    setup.part().write_bytes(half)
    faults.plan = [("whole",)] * 10

    proc = setup.run(NAME, LRCLIB_DUMP_BASE=url, LRCLIB_DOWNLOAD_RETRIES="3")

    assert proc.returncode == 1, proc.stderr
    assert setup.part().read_bytes() == half
    last = proc.stderr.strip().splitlines()[-1]
    assert "3 times in a row" in last and "run again" in last and f"rm {setup.part()}" in last
    assert [entry.status for entry in faults.probes()] == [206, 206, 206]


def test_a_partial_larger_than_the_dump_is_kept_and_named(setup: Setup) -> None:
    (setup.data / "dl").mkdir()
    setup.part().write_bytes(setup.gz_bytes + b"extra")
    before = setup.old_install()

    proc = setup.run(NAME)

    assert proc.returncode == 1
    assert setup.part().read_bytes() == setup.gz_bytes + b"extra"
    assert str(setup.part()) in proc.stderr
    assert setup.downloads() == []
    assert setup.old_install() == before


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("LRCLIB_DOWNLOAD_RETRIES", "0"),
        ("LRCLIB_DOWNLOAD_RETRIES", "x"),
        ("LRCLIB_RETRY_SECONDS", "-1"),
        # bash arithmetic reads a leading 0 as octal: 08 is an error, 010 is 8.
        ("LRCLIB_RETRY_SECONDS", "08"),
        ("LRCLIB_RETRY_SECONDS", "010"),
        ("LRCLIB_RETRY_SECONDS", "1" * 20),
    ],
)
def test_a_bad_retry_setting_exits_2(setup: Setup, variable: str, value: str) -> None:
    proc = setup.run(NAME, **{variable: value})

    assert proc.returncode == 2
    assert variable in proc.stderr
    assert setup.downloads() == []


# A bad download never replaces a good database


def flipped(payload: bytes) -> bytes:
    middle = len(payload) // 2
    return payload[:middle] + bytes([payload[middle] ^ 0xFF]) + payload[middle + 1 :]


@pytest.mark.parametrize("damage", ["truncated", "flipped byte", "not sqlite"])
def test_a_bad_dump_exits_1_and_leaves_the_old_install(setup: Setup, damage: str) -> None:
    if damage == "truncated":
        setup.serve(setup.gz_bytes[: len(setup.gz_bytes) * 3 // 5])
    elif damage == "flipped byte":
        setup.serve(flipped(setup.gz_bytes))
    else:
        setup.serve(gzip.compress(b"not a database\n" * 2000))
    before = setup.old_install()

    proc = setup.run(NAME)

    assert proc.returncode == 1, proc.stderr
    assert setup.old_install() == before
    assert not (setup.data / "db.sqlite3.part").exists()
    assert [line for line in setup.events() if not line.startswith("doctor ")] == []
    if damage != "not sqlite":
        assert not (setup.data / "dl" / NAME).exists()
        assert not (setup.data / "dl" / f"{NAME}.part").exists()


@pytest.mark.parametrize("code", [2, 126, 127, 137])
def test_a_full_size_download_is_kept_when_gzip_fails_for_another_reason(
    setup: Setup, code: int
) -> None:
    """Only exit 1 is gzip's verdict of corruption. A missing or killed gzip, or a warning, keeps
    an hour's download."""
    setup.fake("gzip", GZIP_SHIM.replace("{real}", str(REAL_GZIP)))
    before = setup.old_install()

    proc = setup.run(NAME, GZIP_FAKE_EXIT=str(code))

    assert proc.returncode == 1, proc.stderr
    assert setup.part().read_bytes() == setup.gz_bytes
    last = proc.stderr.strip().splitlines()[-1]
    assert f"exit {code}" in last and "kept" in last and f"rm {setup.part()}" in last
    assert setup.old_install() == before


def test_a_gzip_warning_says_a_rerun_wont_fix_it(setup: Setup) -> None:
    """Exit 2 is gzip's warning about the file, such as trailing garbage, so it comes back on
    every run: the message names rm, and doesn't say gzip couldn't check it."""
    setup.fake("gzip", GZIP_SHIM.replace("{real}", str(REAL_GZIP)))

    proc = setup.run(NAME, GZIP_FAKE_EXIT="2")

    assert proc.returncode == 1, proc.stderr
    assert setup.part().read_bytes() == setup.gz_bytes
    last = proc.stderr.strip().splitlines()[-1]
    assert "warns" in last and f"rm {setup.part()}" in last
    assert "couldn't check" not in last and "run again" not in last.split("rm ")[0]


# Waiting for LRCLIB


def test_a_search_index_never_ready_exits_1_naming_it(
    setup: Setup, stub_lrclib: StubLrclib
) -> None:
    stub_lrclib.preflight_search = (500, {})

    proc = setup.run(NAME, LRCLIB_READY_TIMEOUT="2", LRCLIB_POLL_SECONDS="0.2")

    assert proc.returncode == 1
    assert "search index" in proc.stderr
    assert len(stub_lrclib.probes) >= 2


# The file itself


def test_the_script_parses_and_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)
    assert subprocess.run(["bash", "-n", str(SCRIPT)], check=False).returncode == 0


# The default stop and start: docker compose, with only LRCLIB_DATA set (a docker shim on PATH)

DOCKER_SHIM = """\
printf "docker %s MUSIC_ROOT=%s STATE_DIR=%s\\n" "$*" "${MUSIC_ROOT:-}" "${STATE_DIR:-}" >>"$EVENTS"
# Like compose, refuse any command while a required variable is unset.
if [ -z "${MUSIC_ROOT:-}" ] || [ -z "${STATE_DIR:-}" ] || [ -z "${LRCLIB_DATA:-}" ]; then
  echo "required variable MUSIC_ROOT is missing a value" >&2
  exit 1
fi
# DOCKER_FAIL_ON names one subcommand, such as build or up, that fails.
case " $* " in
  *" ${DOCKER_FAIL_ON:-none} "*) echo "docker: $DOCKER_FAIL_ON failed" >&2; exit 1 ;;
esac
exit "${DOCKER_EXIT:-0}"
"""


def default_compose_env(setup: Setup) -> dict[str, str]:
    setup.fake("docker", DOCKER_SHIM)
    env = {k: v for k, v in setup.env.items() if k not in ("LRCLIB_STOP", "LRCLIB_START")}
    return {k: v for k, v in env.items() if k not in ("MUSIC_ROOT", "STATE_DIR")}


def run_with(env: dict[str, str], **extra: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [str(SCRIPT), NAME],
        env={**env, **extra},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_the_default_compose_commands_work_with_only_lrclib_data(setup: Setup) -> None:
    proc = run_with(default_compose_env(setup))

    assert proc.returncode == 0, proc.stderr
    assert (setup.data / "db.sqlite3").read_bytes() == setup.db_bytes
    compose = f"compose -f {ROOT / 'compose.yaml'}"
    docker = [line for line in setup.events() if line.startswith("docker ")]
    assert [line.split(" MUSIC_ROOT=")[0] for line in docker] == [
        f"docker {compose} config -q lrclib",
        f"docker {compose} build lrclib",
        f"docker {compose} stop lrclib",
        f"docker {compose} up -d --no-build lrclib",
    ]
    assert setup.events()[0].startswith("docker ")
    # The image is built before the download, so the start after the swap needs no network.
    events = [line.split()[0] for line in setup.events()]
    assert events.index("doctor") < events.index("docker", 2)


def test_an_lrclib_image_that_wont_build_stops_the_script_before_any_download(
    setup: Setup,
) -> None:
    before = setup.old_install()

    proc = run_with(default_compose_env(setup), DOCKER_FAIL_ON="build")

    assert proc.returncode == 1
    assert "lrclib-setup:" in proc.stderr and "build" in proc.stderr
    assert "nothing was changed" in proc.stderr
    assert not any(" stop " in line for line in setup.events())
    assert setup.downloads() == []
    assert setup.old_install() == before


def printed_start(stderr: str) -> str:
    """Return the start command a failed start prints, as a user would copy it."""
    last = stderr.strip().splitlines()[-1]
    return last.split("start it with: ", 1)[1].rsplit(", then run doctor", 1)[0]


def without_compose_vars(env: dict[str, str]) -> dict[str, str]:
    """Return env as a user's shell would be after the script: none of compose's variables set."""
    return {k: v for k, v in env.items() if k not in ("LRCLIB_DATA", "MUSIC_ROOT", "STATE_DIR")}


def test_lrclib_that_wont_start_after_the_swap_says_how_to_start_it(setup: Setup) -> None:
    env = default_compose_env(setup)
    proc = run_with(env, DOCKER_FAIL_ON="up")

    # Setup has an old database here, so since the shared swap (06-04) a failed start puts it
    # back. The first-install case, with nothing to put back, is the next test.
    assert proc.returncode == 1
    last = proc.stderr.strip().splitlines()[-1]
    assert last.startswith(f"lrclib-setup: the load of {NAME} was rolled back after start failed")
    assert "previous database is back in place" in last and "LRCLIB didn't start" in last
    assert (setup.data / "db.sqlite3").read_bytes() == OLD_DB
    shown = printed_start(proc.stderr)
    assert f"LRCLIB_DATA={setup.data}" in shown
    assert "MUSIC_ROOT=/nonexistent" in shown and "STATE_DIR=/nonexistent" in shown

    # Pasted into a shell that has none of compose's variables, it runs the same start.
    again = subprocess.run(
        ["bash", "-c", shown],
        env=without_compose_vars(env),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert again.returncode == 0, again.stderr
    compose = f"compose -f {ROOT / 'compose.yaml'}"
    assert setup.events()[-1] == (
        f"docker {compose} up -d --no-build lrclib MUSIC_ROOT=/nonexistent STATE_DIR=/nonexistent"
    )


def test_a_failed_start_prints_a_command_that_keeps_set_variables(setup: Setup) -> None:
    proc = run_with(
        default_compose_env(setup),
        DOCKER_FAIL_ON="up",
        MUSIC_ROOT="/srv/m",
        STATE_DIR="/srv/s",
        LRCLIB_USER="1234:1234",
        LRCLIB_PORT="3399",
    )

    assert proc.returncode == 1
    shown = printed_start(proc.stderr)
    for word in ("MUSIC_ROOT=/srv/m", "STATE_DIR=/srv/s", "LRCLIB_USER=1234:1234"):
        assert word in shown
    assert "LRCLIB_PORT=3399" in shown


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not on PATH")
def test_real_compose_accepts_the_printed_start_from_a_path_with_a_space(
    setup: Setup, tmp_path: Path
) -> None:
    checkout = tmp_path / "a checkout"
    (checkout / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, checkout / "scripts" / SCRIPT.name)
    # The script sources its shared library from beside it, so a checkout holds that too.
    shutil.copytree(ROOT / "scripts" / "lib", checkout / "scripts" / "lib")
    shutil.copy2(ROOT / "compose.yaml", checkout / "compose.yaml")
    env = default_compose_env(setup)
    proc = subprocess.run(
        [str(checkout / "scripts" / SCRIPT.name), NAME],
        env={**env, "DOCKER_FAIL_ON": "up"},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 1, proc.stderr
    shown = printed_start(proc.stderr)
    suffix = " up -d --no-build lrclib"
    assert shown.rstrip().endswith(suffix)
    check = shown.rstrip()[: -len(suffix)] + " config -q lrclib"
    real = without_compose_vars(dict(os.environ))

    result = subprocess.run(
        ["bash", "-c", check],
        env=real,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr


def first_install(setup: Setup) -> None:
    """Remove the old install, so setup loads into an empty LRCLIB_DATA."""
    shutil.rmtree(setup.data / "db.sqlite3.tantivy")
    for name in ("db.sqlite3", "db.sqlite3-wal", "db.sqlite3-shm"):
        (setup.data / name).unlink()


def test_a_first_install_that_wont_start_keeps_the_new_database_and_says_how_to_start_it(
    setup: Setup,
) -> None:
    first_install(setup)
    env = default_compose_env(setup)

    proc = run_with(env, DOCKER_FAIL_ON="up")

    assert proc.returncode == 1
    last = proc.stderr.strip().splitlines()[-1]
    assert last.startswith("lrclib-setup: LRCLIB didn't start, but the new database is in place")
    assert (setup.data / "db.sqlite3").read_bytes() == setup.db_bytes
    assert printed_start(proc.stderr).endswith(" up -d --no-build lrclib")
    assert not (setup.data / ".plyrix-update.journal").exists()


def test_a_custom_start_that_fails_is_named_in_the_message(setup: Setup) -> None:
    # A first install, so the new database stays in place with nothing to roll back to (06-04).
    first_install(setup)
    failing = setup.fake("bad-start", "exit 7")

    proc = setup.run(NAME, LRCLIB_START=str(failing))

    assert proc.returncode == 1
    last = proc.stderr.strip().splitlines()[-1]
    assert last.startswith("lrclib-setup: LRCLIB didn't start")
    assert str(failing) in last
    assert (setup.data / "db.sqlite3").read_bytes() == setup.db_bytes


def test_lrclib_that_wont_stop_leaves_the_old_install(setup: Setup) -> None:
    before = setup.old_install()
    failing = setup.fake("bad-stop", "exit 1")

    proc = setup.run(NAME, LRCLIB_STOP=str(failing))

    assert proc.returncode == 1
    assert "lrclib-setup: LRCLIB didn't stop" in proc.stderr
    assert setup.old_install() == before
    assert not (setup.data / "db.sqlite3.part").exists()


def test_the_default_compose_commands_keep_a_set_music_root(setup: Setup) -> None:
    proc = run_with(default_compose_env(setup), MUSIC_ROOT="/srv/m", STATE_DIR="/srv/s")

    assert proc.returncode == 0, proc.stderr
    docker = [line for line in setup.events() if line.startswith("docker ")]
    assert all(line.endswith(" MUSIC_ROOT=/srv/m STATE_DIR=/srv/s") for line in docker)


def test_compose_that_cant_read_its_file_stops_the_script_before_any_download(
    setup: Setup,
) -> None:
    before = setup.old_install()

    proc = run_with(default_compose_env(setup), DOCKER_EXIT="1")

    assert proc.returncode == 2
    assert "compose" in proc.stderr and "LRCLIB_STOP" in proc.stderr
    assert [line.split()[0] for line in setup.events()] == ["docker"]
    assert setup.downloads() == []
    assert not (setup.data / "dl").exists()
    assert setup.old_install() == before


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not on PATH")
def test_real_compose_reads_the_file_with_the_scripts_placeholders(
    setup: Setup, tmp_path: Path
) -> None:
    run_with(default_compose_env(setup))
    line = next(line for line in setup.events() if " config -q " in line)
    placeholders = dict(word.split("=", 1) for word in line.split()[-2:])
    env = {k: v for k, v in os.environ.items() if k not in ("MUSIC_ROOT", "STATE_DIR")}
    env |= {"LRCLIB_DATA": str(tmp_path), **placeholders}

    proc = subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "compose.yaml"), "config", "-q", "lrclib"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr


# The lock, recovery and live record setup shares with lrclib-update.sh (06-04)


def test_setup_waits_for_an_update_holding_the_lock(setup: Setup) -> None:
    lock = setup.data / ".plyrix-update.lock"
    holder = subprocess.Popen(
        ["flock", str(lock), "-c", "echo held; sleep 60"], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline() == "held\n"
        before = setup.old_install()

        proc = setup.run(NAME)
    finally:
        holder.kill()
        holder.wait()

    assert proc.returncode == 75
    assert "another update of LRCLIB_DATA is running" in proc.stderr
    assert setup.events() == []
    assert setup.downloads() == []
    assert setup.old_install() == before


def test_setup_recovers_a_swap_a_crash_left_before_downloading(setup: Setup) -> None:
    old = {
        name: (setup.data / name).read_bytes()
        for name in ("db.sqlite3", "db.sqlite3-wal", "db.sqlite3-shm")
    }
    for name in ("db.sqlite3", "db.sqlite3-wal", "db.sqlite3-shm", "db.sqlite3.tantivy"):
        (setup.data / name).rename(setup.data / name.replace("db.sqlite3", "db.sqlite3.previous"))
    (setup.data / "db.sqlite3").write_bytes(b"a half-started new database")
    (setup.data / ".plyrix-update.journal").write_text(f"starting {NAME}\n", encoding="utf-8")
    seen = setup.tmp / "at-doctor"
    doctor = setup.fake(
        "doctor-look",
        f'[ "$1" != doctor ] || cp "{setup.data}/db.sqlite3" "{seen}"\n'
        'printf "doctor %s\\n" "$*" >>"$EVENTS"',
    )

    proc = setup.run(NAME, PLYRIX=str(doctor), DOCKER_FAIL_ON="none")

    assert proc.returncode == 0, proc.stderr
    # The old database was back before doctor, and so before any download.
    assert seen.read_bytes() == old["db.sqlite3"]
    assert "put back" in proc.stdout
    assert (setup.data / "db.sqlite3").read_bytes() == setup.db_bytes


def test_setup_clears_a_first_load_a_crash_left_with_no_database_and_loads(
    setup: Setup,
) -> None:
    first_install(setup)
    (setup.data / ".plyrix-update.journal").write_text(f"moving_old {NAME}\n", encoding="utf-8")

    proc = setup.run(NAME)

    assert proc.returncode == 0, proc.stderr
    assert "before any database was in place, so it is loaded again" in proc.stderr
    assert (setup.data / "db.sqlite3").read_bytes() == setup.db_bytes
    assert not (setup.data / ".plyrix-update.journal").exists()


def first_load_in_place(setup: Setup) -> None:
    """Leave what a first load killed at starting leaves: its database, a journal, no previous."""
    first_install(setup)
    (setup.data / "db.sqlite3").write_bytes(setup.db_bytes)
    (setup.data / ".plyrix-update.journal").write_text(f"starting {NAME}\n", encoding="utf-8")


def doctor_short_of_space(setup: Setup) -> Path:
    """Return a PLYRIX whose doctor fails, as on a host sized for one load, and whose rest works."""
    return setup.fake(
        "doctor-short",
        'printf "doctor %s\\n" "$*" >>"$EVENTS"\n[ "$1" != doctor ] || exit 1',
    )


def test_setup_waits_for_a_first_load_a_crash_left_in_place_and_doesnt_load_it_again(
    setup: Setup,
) -> None:
    first_load_in_place(setup)
    # Doctor would fail for space with the recovered database there.
    proc = setup.run(NAME, PLYRIX=str(doctor_short_of_space(setup)))

    assert proc.returncode == 0, proc.stderr
    assert "isn't loaded again" in proc.stdout
    assert f"LRCLIB is ready with {NAME}" in proc.stdout
    assert "once its search index is ready, record it" not in proc.stderr
    assert setup.downloads() == []
    assert not any(line.startswith("doctor doctor") for line in setup.events())
    assert any(" start " in f" {line} " for line in setup.events())
    assert f"doctor lrclib-record {NAME} --status live" in setup.events()
    assert (setup.data / "db.sqlite3").read_bytes() == setup.db_bytes
    assert not (setup.data / "db.sqlite3.staged").exists()
    assert not (setup.data / ".plyrix-update.journal").exists()


def test_a_first_load_in_place_whose_index_isnt_ready_keeps_its_journal(
    setup: Setup, stub_lrclib: StubLrclib
) -> None:
    first_load_in_place(setup)
    stub_lrclib.preflight_search = (500, {})

    proc = setup.run(NAME, LRCLIB_READY_TIMEOUT="1", LRCLIB_POLL_SECONDS="0.2")

    assert proc.returncode == 1
    assert "search index" in proc.stderr
    assert setup.downloads() == []
    assert (setup.data / "db.sqlite3").read_bytes() == setup.db_bytes
    # The next run finds the same first load and waits again, rather than loading it.
    journal = (setup.data / ".plyrix-update.journal").read_text(encoding="utf-8")
    assert journal == f"starting {NAME}\n"


def test_setup_for_a_dump_left_swapped_records_it_and_stops(setup: Setup) -> None:
    for name in ("db.sqlite3", "db.sqlite3-wal", "db.sqlite3-shm", "db.sqlite3.tantivy"):
        (setup.data / name).rename(setup.data / name.replace("db.sqlite3", "db.sqlite3.previous"))
    (setup.data / "db.sqlite3").write_bytes(setup.db_bytes)
    (setup.data / ".plyrix-update.journal").write_text(f"swapped {NAME}\n", encoding="utf-8")

    proc = setup.run(NAME, PLYRIX=str(doctor_short_of_space(setup)))

    assert proc.returncode == 0, proc.stderr
    assert f"LRCLIB is ready with {NAME}" in proc.stdout
    assert setup.downloads() == []
    assert not any(line.startswith("doctor doctor") for line in setup.events())
    assert (setup.data / "db.sqlite3").read_bytes() == setup.db_bytes


def recording_plyrix(setup: Setup, state_db: Path, fail_record: bool = False) -> Path:
    """Return a PLYRIX that sends doctor to the fake and the rest to the real plyrix."""
    real = Path(sys.executable).parent / "plyrix"
    lib = setup.tmp / "lib"
    lib.mkdir(exist_ok=True)
    record = "exit 1" if fail_record else f'exec "{real}" "$@"'
    return setup.fake(
        "plyrix-routed",
        f'if [ "$1" = doctor ]; then exec "{setup.doctor}" "$@"; fi\n'
        f'if [ "$1" = lrclib-record ]; then {record}; fi\n'
        f'exec "{real}" "$@"',
    )


def test_a_configured_plyrix_records_the_loaded_dump_live(
    setup: Setup, stub_lrclib: StubLrclib
) -> None:
    state_db = setup.tmp / "state" / "state.db"
    plyrix = recording_plyrix(setup, state_db)

    proc = setup.run(
        NAME,
        PLYRIX=str(plyrix),
        PLYRIX_MUSIC_ROOT=str(setup.tmp / "lib"),
        PLYRIX_LRCLIB_URL=stub_lrclib.base_url,
        PLYRIX_STATE_DB=str(state_db),
        XDG_CONFIG_HOME=str(setup.tmp / "xdg"),
    )

    assert proc.returncode == 0, proc.stderr
    con = sqlite3.connect(state_db)
    rows = con.execute("SELECT name, status FROM lrclib_dumps").fetchall()
    con.close()
    assert rows == [(NAME, "live")]
    second_to_last = proc.stdout.splitlines()[-2]
    assert second_to_last == (
        f"lrclib-setup: plyrix recorded {NAME} as LRCLIB's live dump, so dump_id needs no setting"
    )
    assert NAME in second_to_last and "dump_id" in second_to_last


def test_a_plyrix_that_cant_record_gets_todays_dump_id_line(setup: Setup) -> None:
    plyrix = recording_plyrix(setup, setup.tmp / "state.db", fail_record=True)

    proc = setup.run(NAME, PLYRIX=str(plyrix))

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[-2] == (
        f"Once plyrix is configured, run: plyrix lrclib-record {NAME} --status live. "
        f"Until then, set dump_id to {NAME} (PLYRIX_DUMP_ID, or dump_id in the config file)."
    )
