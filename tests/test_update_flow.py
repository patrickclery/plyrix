"""A whole update cycle through the real update script: .txt files under dump A, an update to B
recorded with its diff, then the follow-up's upgrade to .lrc and its capped, paced Plex refresh.

The refreshes go through plyrix's existing pending-refresh ledger and plex_pace, with
`plex-refresh --limit` as the cap per update (D-10). Every database and tag is invented, LRCLIB's
stop and start are fakes, and Plex is the local stub.
"""

import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from conftest import RECORD, StubLrclib, StubPlex, copy_track, plex_track, snap
from lrclib_fixture import DumpServer, add_named_track, dump_server, gz_bytes  # noqa: F401
from test_lrclib_update_script import NEW, OLD, PLYRIX, Update

PLAIN_ONLY = {**RECORD, "syncedLyrics": None}


class Flow:
    """The update harness with three albums, Plex configured, and dump A live with watermarks."""

    def __init__(self, update: Update, lrclib: StubLrclib, plex: StubPlex) -> None:
        self.update = update
        self.lrclib = lrclib
        self.plex = plex
        self.tracks = [update.lib / "Album" / "t.flac"]
        self.tracks += [copy_track(update.lib, album) for album in ("Album 2", "Album 3")]
        # B adds an LRCLIB track by the fixture tags' artist, so the diff names every track here.
        new = update.tmp / "flow-new.sqlite3"
        shutil.copyfile(update.db, new)
        add_named_track(new, "Fixture Artist", "Fixture Title")
        update.new_db = new
        update.server.offer(NEW, gz_bytes(new))
        del update.env["PLYRIX_DUMP_ID"]
        update.env |= {
            "PLYRIX_PLEX_URL": plex.base_url,
            "PLYRIX_PLEX_SECTION": "1",
            "PLYRIX_PLEX_TOKEN": "plyrix-dummy-token-0123456789",
            "PLYRIX_PLEX_PACE": "0",
        }
        self.plyrix("lrclib-diff", OLD, "--staged", str(update.db))
        self.plyrix("lrclib-record", OLD, "--status", "live")
        plex.location = os.path.realpath(update.lib)
        plex.tracks = [
            plex_track(str(201 + i), str(101 + i), os.fsencode(os.path.realpath(track)))
            for i, track in enumerate(self.tracks)
        ]
        self.put_times: list[float] = []
        answer = plex.answer

        def timed(method: str, path: str, query: dict[str, str], headers: dict[str, str]):  # type: ignore[no-untyped-def]
            if method == "PUT":
                self.put_times.append(time.monotonic())
            return answer(method, path, query, headers)

        plex.answer = timed  # type: ignore[method-assign]

    def plyrix(self, *args: str, **env: str) -> "subprocess.CompletedProcess[str]":
        proc = subprocess.run(
            [str(PLYRIX), *args],
            env={**self.update.env, **env},
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        return proc

    def under_a(self) -> None:
        """Write a .txt for each track under A, refresh their albums, and age the rows."""
        self.lrclib.body = PLAIN_ONLY
        self.plyrix("run")
        assert all(track.with_suffix(".txt").exists() for track in self.tracks)
        self.plyrix("plex-refresh")
        assert len(self.plex.puts()) == 3
        con = sqlite3.connect(self.update.state_db)
        con.execute("UPDATE tracks SET first_seen_at = '2026-01-01T00:00:00+00:00'")
        con.commit()
        con.close()
        self.lrclib.body = RECORD
        self.plex.requests.clear()
        self.put_times.clear()

    def pending(self) -> int:
        con = sqlite3.connect(self.update.state_db)
        try:
            return int(
                con.execute("SELECT count(*) FROM sidecars WHERE refresh_pending = 1").fetchone()[0]
            )
        finally:
            con.close()


@pytest.fixture
def flow(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    stub_plex: StubPlex,
    dump_server: DumpServer,  # noqa: F811
) -> Flow:
    return Flow(Update(tmp_path, stub_lrclib, dump_server), stub_lrclib, stub_plex)


def test_an_update_upgrades_txt_and_the_follow_up_refresh_is_capped_and_paced(flow: Flow) -> None:
    flow.under_a()

    proc = flow.update.run(LRCLIB_FOLLOW_UP="1", LRCLIB_PLEX_LIMIT="2", PLYRIX_PLEX_PACE="0.2")

    assert proc.returncode == 0, proc.stderr
    assert flow.update.dump_statuses() == [(OLD, "previous"), (NEW, "live")]
    assert flow.update.diff_of(NEW) == ("complete", 1)
    for track in flow.tracks:
        assert track.with_suffix(".lrc").exists()
        assert not track.with_suffix(".txt").exists()
    assert len(flow.plex.puts()) == 2
    assert flow.put_times[1] - flow.put_times[0] >= 0.19
    assert flow.pending() == 1

    flow.plyrix("plex-refresh")

    assert len(flow.plex.puts()) == 3
    assert flow.pending() == 0


def test_the_follow_up_is_off_by_default(flow: Flow) -> None:
    flow.under_a()
    library = {str(path): snap(path) for path in sorted(flow.update.lib.rglob("*"))}

    proc = flow.update.run()

    assert proc.returncode == 0, proc.stderr
    assert flow.update.dump_statuses() == [(OLD, "previous"), (NEW, "live")]
    assert {str(path): snap(path) for path in sorted(flow.update.lib.rglob("*"))} == library
    assert flow.plex.requests == []
    assert all(track.with_suffix(".txt").exists() for track in flow.tracks)


def logging_plyrix(update: Update) -> Path:
    return update.fake("plyrix-log", f'echo "plyrix $1" >>"$EVENTS"\nexec "{PLYRIX}" "$@"')


@pytest.mark.parametrize("case", ["no update", "refused", "rolled back"])
def test_no_follow_up_without_a_new_live_dump(flow: Flow, case: str) -> None:
    update = flow.update
    env = {"PLYRIX": str(logging_plyrix(update)), "LRCLIB_FOLLOW_UP": "1"}
    if case == "no update":
        update.server.offer(OLD, b"")
    elif case == "refused":
        from lrclib_fixture import drop_index, make_db

        update.server.offer(NEW, gz_bytes(drop_index(make_db(update.tmp / "x.sqlite3", 5))))
    else:
        once = update.tmp / "started-once"
        env["LRCLIB_START"] = str(
            update.fake("start-once", f'if [ ! -e "{once}" ]; then : >"{once}"; exit 1; fi')
        )

    proc = update.run(**env)

    assert proc.returncode == (0 if case == "no update" else 1), proc.stderr
    calls = [line.split()[1] for line in update.events() if line.startswith("plyrix ")]
    assert "run" not in calls and "plex-refresh" not in calls
    assert flow.plex.requests == []


def test_a_failed_follow_up_keeps_the_dump_live_and_still_refreshes(flow: Flow) -> None:
    update = flow.update
    shim = update.shim("run", "a run that failed", 3)
    logged = update.fake("plyrix-log-shim", f'echo "plyrix $1" >>"$EVENTS"\nexec "{shim}" "$@"')

    proc = update.run(PLYRIX=str(logged), LRCLIB_FOLLOW_UP="1")

    assert proc.returncode == 3
    last = proc.stderr.strip().splitlines()[-1]
    assert last.startswith(f"lrclib-update: {NEW} is live, but the follow-up run or Plex refresh")
    assert update.dump_statuses() == [(OLD, "previous"), (NEW, "live")]
    # A scan that didn't finish every track still wrote sidecars, so the refresh runs.
    calls = [line.split()[1] for line in update.events() if line.startswith("plyrix ")]
    assert calls.index("run") < calls.index("plex-refresh")


def test_a_failed_follow_up_refresh_exits_3(flow: Flow) -> None:
    shim = flow.update.shim("plex-refresh", "a refresh that failed", 1)

    proc = flow.update.run(PLYRIX=str(shim), LRCLIB_FOLLOW_UP="1")

    assert proc.returncode == 3
    assert "(exit 1)" in proc.stderr.strip().splitlines()[-1]


@pytest.mark.parametrize(
    ("variable", "value"),
    [("LRCLIB_FOLLOW_UP", "2"), ("LRCLIB_PLEX_LIMIT", "0"), ("LRCLIB_PLEX_LIMIT", "x")],
)
def test_a_bad_follow_up_setting_exits_2(flow: Flow, variable: str, value: str) -> None:
    flow.update.server.requests.clear()

    proc = flow.update.run(**{variable: value})

    assert proc.returncode == 2
    assert variable in proc.stderr
    assert flow.update.server.requests == []
