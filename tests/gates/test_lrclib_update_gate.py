"""LRCLIB update gate: the real update script against a throwaway LRCLIB from the pinned image.

Skipped unless PLYRIX_LRCLIB_UPDATE_GATE=1, and never run in hosted CI. The gate runs its own
container from the locally built pinned image, with --pull never, under its own name, on its own
loopback port, over a data folder in pytest's tmp_path. It never builds or pulls an image, and it
never stops, starts or mounts anything of another container. Every dump is invented and served by
a local DumpServer, so no real listing or dump is fetched.

In order: the pinned server makes its database at the newest user_version the guard accepts; the
update script swaps dump B in and the real server answers for it; a dump C whose start fails once
is rolled back to B; a dump D too new for the pin is refused for one 64 KB read. The result file
holds booleans, counts, the image tag, timings and the date only.
"""

import datetime
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from conftest import copy_track, snap
from lrclib_fixture import (
    PROBE,
    DumpServer,
    gz_bytes,
    insert_tracks,
    replace_lyrics,
    running_dump_server,
)
from plyrix.state import lrclib_db

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "lrclib-update.sh"
PLYRIX = Path(sys.executable).parent / "plyrix"
IMAGE = f"plyrix-lrclib:{lrclib_db.LRCLIB_PIN[:7]}"
NAME = "plyrix-phase6-lrclib"
PORT = 13300
API = f"http://127.0.0.1:{PORT}/api"
LIVE = "lrclib"
# Docker's own names for a container started without --name: adjective_surname.
GENERATED_NAME = re.compile(r"^[a-z]+_[a-z]+$")
A = "lrclib-db-dump-20260601T000000Z.sqlite3.gz"
B = "lrclib-db-dump-20260701T000000Z.sqlite3.gz"
C = "lrclib-db-dump-20260801T000000Z.sqlite3.gz"
D = "lrclib-db-dump-20260901T000000Z.sqlite3.gz"
STOP = f"docker stop {NAME}"
START = f"docker start {NAME}"


def docker(*args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    if shutil.which("docker") is None:
        pytest.fail("PLYRIX_LRCLIB_UPDATE_GATE=1 but docker is not on PATH")
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False, timeout=timeout
    )


def containers() -> dict[str, str]:
    """Return each container's name with its ID."""
    proc = docker("ps", "-a", "--no-trunc", "--format", "{{.Names}}\t{{.ID}}")
    assert proc.returncode == 0, "docker ps failed"
    return dict(line.split("\t") for line in proc.stdout.splitlines())


def images() -> list[str]:
    proc = docker("images", "--no-trunc", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}")
    assert proc.returncode == 0, "docker images failed"
    return sorted(proc.stdout.splitlines())


def live_identity() -> str | None:
    """Return the live LRCLIB container's ID and start time, or None when there is none."""
    proc = docker("inspect", "--format", "{{.Id}} {{.State.StartedAt}}", LIVE)
    return proc.stdout.strip() if proc.returncode == 0 else None


def port_in_use() -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", PORT)) == 0


def get(path: str) -> int:
    """Return the HTTP status LRCLIB gives a GET of path, or 0 when it doesn't answer."""
    try:
        with urllib.request.urlopen(f"{API}{path}", timeout=30) as answer:  # noqa: S310
            return int(answer.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except OSError:
        return 0


def search_ready() -> bool:
    try:
        with urllib.request.urlopen(  # noqa: S310
            f"{API}/search?artist_name=plyrix+preflight&track_name=plyrix+preflight", timeout=30
        ) as answer:
            return bool(answer.status == 200 and answer.read(1) == b"[")
    except OSError:
        return False


def wait_for(check: Callable[[], bool], timeout: float = 120) -> bool:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if check():
            return True
        time.sleep(1)
    return False


@dataclass
class Gate:
    """The gate's container, data folder, dump server and state, and the result it builds."""

    tmp: Path
    server: DumpServer
    result: dict[str, object] = field(default_factory=dict)
    finished: bool = False
    before_containers: dict[str, str] = field(default_factory=dict)
    before_images: list[str] = field(default_factory=list)
    before_live: str | None = None
    new_db: Path | None = None
    b_ids: list[int] = field(default_factory=list)
    last_seconds: float = 0.0

    @property
    def data(self) -> Path:
        return self.tmp / "data"

    @property
    def db(self) -> Path:
        return self.data / "db.sqlite3"

    @property
    def lib(self) -> Path:
        return self.tmp / "lib"

    @property
    def state_db(self) -> Path:
        return self.tmp / "state" / "state.db"

    def env(self, **extra: str) -> dict[str, str]:
        inherited = {k: v for k, v in os.environ.items() if not k.startswith(("PLYRIX", "LRCLIB_"))}
        return {
            **inherited,
            "XDG_STATE_HOME": str(self.tmp / "xdg-state"),
            "XDG_CONFIG_HOME": str(self.tmp / "xdg-config"),
            "PLYRIX_MUSIC_ROOT": str(self.lib),
            "PLYRIX_LRCLIB_URL": API,
            "PLYRIX_STATE_DB": str(self.state_db),
            "LRCLIB_DATA": str(self.data),
            "PLYRIX": str(PLYRIX),
            "LRCLIB_STOP": STOP,
            "LRCLIB_START": START,
            "LRCLIB_URL": API,
            "LRCLIB_DUMP_LISTING": self.server.listing_url,
            "LRCLIB_DUMP_BASE": self.server.base_url,
            "LRCLIB_READY_TIMEOUT": "600",
            "LRCLIB_GET_TIMEOUT": "120",
            "LRCLIB_POLL_SECONDS": "1",
            "LRCLIB_RETRY_SECONDS": "0",
            **extra,
        }

    def plyrix(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(PLYRIX), *args], env=self.env(), capture_output=True, text=True, check=False,
            timeout=300,
        )  # fmt: skip

    def update(self, **extra: str) -> subprocess.CompletedProcess[str]:
        started = time.monotonic()
        proc = subprocess.run(
            [str(SCRIPT)], env=self.env(**extra), capture_output=True, text=True, check=False,
            timeout=1800,
        )  # fmt: skip
        self.last_seconds = round(time.monotonic() - started, 1)
        return proc

    def statuses(self) -> dict[str, str]:
        con = sqlite3.connect(self.state_db)
        try:
            return dict(con.execute("SELECT name, status FROM lrclib_dumps").fetchall())
        finally:
            con.close()

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
        return str(state), int(count)

    def library(self) -> dict[str, tuple[str, int, int, int, int, int, int]]:
        return {str(p.relative_to(self.lib)): snap(p) for p in sorted(self.lib.rglob("*"))}

    def finish(self) -> None:
        """Remove the gate's container and data, check the host, and write the result."""
        if self.finished:
            return
        self.finished = True
        docker("rm", "-f", NAME)
        shutil.rmtree(self.data, ignore_errors=True)
        after = containers()
        named = {n: i for n, i in self.before_containers.items() if not GENERATED_NAME.match(n)}
        self.result["named_containers_unchanged"] = all(
            after.get(name) == ident for name, ident in named.items()
        )
        self.result["gate_container_gone"] = NAME not in after
        self.result["images_unchanged"] = images() == self.before_images
        self.result["live_lrclib_untouched"] = live_identity() == self.before_live
        self.result["gate_data_removed"] = not self.data.exists()
        destination = os.environ.get("PLYRIX_GATE_RESULT")
        if destination:
            Path(destination).write_text(json.dumps(self.result, indent=2) + "\n", "utf-8")
        print(json.dumps(self.result))


@pytest.fixture(scope="module")
def lrclib(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Gate]:
    if docker("image", "inspect", "--format", "{{.Id}}", IMAGE).returncode != 0:
        pytest.fail(f"the local image {IMAGE} is missing, and the gate never builds or pulls")
    with ExitStack() as stack:
        server = stack.enter_context(running_dump_server())
        state = Gate(tmp_path_factory.mktemp("lrclib-update-gate"), server)
        state.before_containers = containers()
        state.before_images = images()
        state.before_live = live_identity()
        if NAME in state.before_containers:
            pytest.fail(f"a container named {NAME} already exists")
        if port_in_use():
            pytest.fail(f"127.0.0.1:{PORT} is already in use")
        (state.data / "tmp").mkdir(parents=True)
        copy_track(state.lib)
        state.result |= {"date": datetime.date.today().isoformat(), "image": IMAGE}
        try:
            started = time.monotonic()
            run = docker(
                "run", "-d", "--pull", "never", "--name", NAME,
                "--user", f"{os.getuid()}:{os.getgid()}", "-e", "TMPDIR=/data/tmp",
                "-v", f"{state.data}:/data", "-p", f"127.0.0.1:{PORT}:3300", IMAGE,
            )  # fmt: skip
            assert run.returncode == 0, "docker run failed"
            assert wait_for(search_ready), "the throwaway LRCLIB never answered search"
            state.result["first_start_seconds"] = round(time.monotonic() - started, 1)
            yield state
        finally:
            state.finish()


@pytest.mark.lrclib_update
@pytest.mark.skipif(
    os.environ.get("PLYRIX_LRCLIB_UPDATE_GATE") != "1",
    reason="set PLYRIX_LRCLIB_UPDATE_GATE=1 to run the update against a throwaway LRCLIB",
)
class TestTheUpdateAgainstThePinnedServer:
    def test_the_pinned_server_makes_the_newest_version_the_guard_accepts(
        self, lrclib: Gate
    ) -> None:
        assert docker("stop", NAME).returncode == 0
        conn = lrclib_db.open_read_only(lrclib.db)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

        lrclib.result["server_user_version_matches_guard"] = version == max(
            lrclib_db.LRCLIB_USER_VERSIONS
        )
        assert version == max(lrclib_db.LRCLIB_USER_VERSIONS)

    def test_the_update_swaps_in_b_against_the_real_server(self, lrclib: Gate) -> None:
        a_ids = insert_tracks(
            lrclib.db, [(f"Invented Artist {n}", f"Invented Track {n}") for n in range(30)]
        )
        old = lrclib.tmp / "old.sqlite3"
        shutil.copyfile(lrclib.db, old)
        new = lrclib.tmp / "new.sqlite3"
        shutil.copyfile(old, new)
        b_ids = insert_tracks(
            new, [(f"Invented Artist B{n}", f"Invented Track B{n}") for n in range(10)]
        )
        replace_lyrics(new, a_ids[:5])
        lrclib.new_db = new
        lrclib.server.offer(B, gz_bytes(new))
        assert docker("start", NAME).returncode == 0
        assert wait_for(search_ready), "LRCLIB didn't come back on dump A"
        recorded = lrclib.plyrix("lrclib-record", A, "--status", "live")
        assert recorded.returncode == 0, recorded.stderr
        library = lrclib.library()

        proc = lrclib.update()

        assert proc.returncode == 0, proc.stdout + proc.stderr
        lrclib.result["swap_seconds"] = lrclib.last_seconds
        b_only = get(f"/get/{b_ids[-1]}")
        state, changes = lrclib.diff_of(B)
        lrclib.result |= {
            "b_served": b_only == 200,
            "b_search_ready": search_ready(),
            "b_live_a_previous": lrclib.statuses() == {A: "previous", B: "live"},
            "b_diff_complete": state == "complete",
            "b_changes": changes,
            "b_changes_exact": changes == 15,
            "no_previous_left": not any(".previous" in p.name for p in lrclib.data.iterdir()),
            "library_unchanged": lrclib.library() == library,
        }
        for key in ("b_served", "b_search_ready", "b_live_a_previous", "b_diff_complete"):
            assert lrclib.result[key] is True, key
        assert changes == 15
        assert lrclib.result["no_previous_left"] is True
        assert lrclib.result["library_unchanged"] is True
        lrclib.b_ids = b_ids

    def test_a_failed_start_rolls_back_to_b(self, lrclib: Gate) -> None:
        assert lrclib.new_db is not None
        newer = lrclib.tmp / "c.sqlite3"
        shutil.copyfile(lrclib.new_db, newer)
        c_ids = insert_tracks(
            newer, [(f"Invented Artist C{n}", f"Invented Track C{n}") for n in range(5)]
        )
        lrclib.server.offer(C, gz_bytes(newer))
        once = lrclib.tmp / "started-once"
        wrapper = lrclib.tmp / "start-once"
        wrapper.write_text(
            f'#!/usr/bin/env bash\n{START} >/dev/null\nif [ ! -e "{once}" ]; then\n'
            f'  : >"{once}"\n  exit 1\nfi\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

        proc = lrclib.update(LRCLIB_START=str(wrapper))

        assert proc.returncode == 1, proc.stdout + proc.stderr
        lrclib.result["rollback_seconds"] = lrclib.last_seconds
        lrclib.result |= {
            "c_rolled_back": "rolled back" in proc.stderr,
            "c_not_served": get(f"/get/{c_ids[-1]}") == 404,
            "b_served_again": get(f"/get/{lrclib.b_ids[-1]}") == 200,
            "c_rolled_back_b_live": lrclib.statuses().get(C) == "rolled_back"
            and lrclib.statuses().get(B) == "live",
        }
        for key in ("c_rolled_back", "c_not_served", "b_served_again", "c_rolled_back_b_live"):
            assert lrclib.result[key] is True, key

    def test_a_dump_too_new_is_refused_before_download(self, lrclib: Gate) -> None:
        assert lrclib.new_db is not None
        conn_path = lrclib.tmp / "d.sqlite3"
        shutil.copyfile(lrclib.new_db, conn_path)
        con = sqlite3.connect(conn_path)
        con.execute("PRAGMA user_version=9")
        con.commit()
        con.close()
        lrclib.server.dumps.clear()
        lrclib.server.offer(D, gz_bytes(conn_path))
        lrclib.server.requests.clear()

        proc = lrclib.update()

        assert proc.returncode == 1, proc.stdout + proc.stderr
        fetched = lrclib.server.requests
        lrclib.result["d_refused_for_one_probe"] = fetched == [
            ("GET", "/listing", ""),
            ("GET", f"/{D}", PROBE),
        ]
        assert lrclib.result["d_refused_for_one_probe"] is True

    def test_the_teardown_left_the_host_as_it_was(self, lrclib: Gate) -> None:
        lrclib.finish()

        for key in (
            "named_containers_unchanged",
            "gate_container_gone",
            "images_unchanged",
            "live_lrclib_untouched",
            "gate_data_removed",
        ):
            assert lrclib.result[key] is True, key
        assert all(value for value in lrclib.result.values() if isinstance(value, bool))


# Static checks of the gate's own isolation, which run without Docker


def own_source() -> str:
    return Path(__file__).read_text(encoding="utf-8")


def test_the_gate_stops_and_starts_only_its_own_container() -> None:
    source = own_source()

    assert NAME.startswith("plyrix-phase6-")
    assert STOP == f"docker stop {NAME}" and START == f"docker start {NAME}"
    # Every docker stop or start the gate writes goes through those two constants.
    targets = re.findall(r"\"docker (?:stop|start) ([^\"]*)\"", source)
    assert targets and set(targets) == {"{NAME}"}
    assert f'"{LIVE}"' not in source.replace(f'LIVE = "{LIVE}"', "")


def test_the_gate_maps_only_its_own_port() -> None:
    source = own_source()

    assert PORT == 13300
    mappings = re.findall(r"127\.0\.0\.1:(\{PORT\}|\d+):3300", source)
    assert mappings and set(mappings) == {"{PORT}"}
    assert "3300" + ":3300" not in source


def test_the_gate_names_no_scratch_root() -> None:
    needle = "/ta" + "nk"

    assert needle not in own_source()


def test_the_gate_never_builds_or_pulls() -> None:
    source = own_source()

    assert '"--pull", "never"' in source
    for word in ("bui" + "ld", "pu" + "ll"):
        assert '"' + word + '"' not in source, word
