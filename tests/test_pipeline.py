"""Pipeline branches a fixture can't reach, in process. No guard is installed here."""

import errno
import hashlib
import math
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from conftest import FIXTURES, RECORD, StubLrclib, settings_for, tags_with
from plyrix import matching, pipeline, sidecars, tags
from plyrix.domain import Kind, Track, TrackTags
from plyrix.state import db

FOREIGN = b"not written by plyrix\n"


def copy_track(tmp_path: Path) -> Path:
    album = tmp_path / "lib" / "Album"
    album.mkdir(parents=True)
    return Path(shutil.copyfile(FIXTURES / "t.flac", album / "t.flac"))


@pytest.mark.parametrize(
    "fields",
    [
        {"artist": None},
        {"artist": "   "},
        {"artist": None, "albumartist": "Various Artists"},
        {"title": None},
        {"title": "   "},
        {"duration": None},
        {"duration": 0.0},
        {"duration": -1.0},
        {"duration": math.nan},
        {"duration": math.inf},
    ],
)
def test_a_missing_tag_counts_untagged_with_no_lookup(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    monkeypatch: pytest.MonkeyPatch,
    fields: dict[str, object],
) -> None:
    copy_track(tmp_path)
    monkeypatch.setattr(tags, "read", lambda _path: tags_with(**fields))
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)

    counts = pipeline.run(settings, str(tmp_path / "lib").encode())

    assert counts.untagged == 1
    assert stub_lrclib.requests == []


def test_album_artist_fallback_reaches_the_lookup(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy_track(tmp_path)
    monkeypatch.setattr(
        tags, "read", lambda _path: tags_with(artist=None, albumartist="Fixture Album Artist")
    )
    stub_lrclib.body = {**RECORD, "artistName": "Fixture Album Artist"}
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)

    counts = pipeline.run(settings, str(tmp_path / "lib").encode())

    assert counts.written_lrc == 1
    assert stub_lrclib.requests[0]["artist_name"] == "Fixture Album Artist"


def test_a_sidecar_appearing_after_the_check_is_left_alone(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = copy_track(tmp_path)
    sidecar = track.with_suffix(".lrc")
    real_publish = sidecars.publish

    def publish_after_a_rival(*args: Any) -> sidecars.Result:
        sidecar.write_bytes(FOREIGN)
        return real_publish(*args)

    monkeypatch.setattr(sidecars, "publish", publish_after_a_rival)
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)

    counts = pipeline.run(settings, str(tmp_path / "lib").encode())

    assert counts.blocked_foreign == 1
    assert counts.written_lrc == 0
    assert sidecar.read_bytes() == FOREIGN
    conn = db.connect(settings.state_db)
    assert conn.execute("SELECT COUNT(*) FROM sidecars").fetchone()[0] == 0


def test_a_filesystem_with_no_hard_links_leaves_no_row(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy_track(tmp_path)
    monkeypatch.setattr(sidecars, "publish", lambda *_args: sidecars.Result.UNSUPPORTED)
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)

    counts = pipeline.run(settings, str(tmp_path / "lib").encode())

    assert counts.fs_no_atomic_publish == 1
    conn = db.connect(settings.state_db)
    assert conn.execute("SELECT COUNT(*) FROM sidecars").fetchone()[0] == 0


def test_a_failed_temp_cleanup_still_counts_as_written_and_owned(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = copy_track(tmp_path).with_suffix(".lrc")
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    real_unlink = os.unlink

    def unlink(path: bytes) -> None:
        if path.endswith(b".part"):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        real_unlink(path)

    with monkeypatch.context() as faults:
        faults.setattr(os, "unlink", unlink)
        first = pipeline.run(settings, str(tmp_path / "lib").encode())

    assert first.written_lrc == 1
    assert first.write_failed == 0
    [temp] = sidecar.parent.glob(".plyrix-*.part")
    second = pipeline.run(settings, str(tmp_path / "lib").encode())
    assert second.synced == 1
    assert second.skipped_unchanged == 1
    assert second.blocked_foreign == 0
    assert sidecar.exists()
    assert not temp.exists()


def test_a_publish_that_fails_after_finishing_is_settled_by_the_next_run(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = copy_track(tmp_path).with_suffix(".lrc")
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    real_publish = sidecars.publish

    def publish_then_fail(*args: Any) -> sidecars.Result:
        real_publish(*args)
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    with monkeypatch.context() as faults:
        faults.setattr(sidecars, "publish", publish_then_fail)
        first = pipeline.run(settings, str(tmp_path / "lib").encode())

    assert first.write_failed == 1
    conn = db.connect(settings.state_db)
    assert conn.execute("SELECT status FROM sidecars").fetchall() == [("intent",)]
    conn.close()

    second = pipeline.run(settings, str(tmp_path / "lib").encode())

    assert second.synced == 1
    assert second.skipped_unchanged == 1
    assert second.blocked_foreign == 0
    assert sidecar.exists()


def test_a_publish_that_failed_with_nothing_written_is_retried_by_the_next_run(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = copy_track(tmp_path).with_suffix(".lrc")
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)

    def fail(*_args: Any) -> sidecars.Result:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    with monkeypatch.context() as faults:
        faults.setattr(sidecars, "publish", fail)
        first = pipeline.run(settings, str(tmp_path / "lib").encode())

    assert first.write_failed == 1
    second = pipeline.run(settings, str(tmp_path / "lib").encode())
    assert second.written_lrc == 1
    assert sidecar.exists()


def test_drop_intent_never_removes_a_published_row(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    run_id = db.begin_run(conn, "test")
    db.add_intent(conn, b"/root/a.lrc", Kind.LRC, "abc", 3, run_id)
    db.mark_published(conn, b"/root/a.lrc")

    db.drop_intent(conn, b"/root/a.lrc")

    assert db.owned_row(conn, b"/root/a.lrc") == ("abc", 3, "written", None)


def test_an_intent_row_is_not_ownership(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "state.db")
    run_id = db.begin_run(conn, "test")
    db.add_intent(conn, b"/root/a.lrc", Kind.LRC, "abc", 3, run_id)

    assert db.owned_row(conn, b"/root/a.lrc") is None


def test_read_sha256_hashes_a_regular_file(tmp_path: Path) -> None:
    (tmp_path / "t.lrc").write_bytes(FOREIGN)
    expected = hashlib.sha256(FOREIGN).hexdigest()
    assert sidecars.read_sha256(Track(os.fsencode(tmp_path), b"t.flac", b"t"), Kind.LRC) == expected


def test_read_sha256_with_nothing_there(tmp_path: Path) -> None:
    (tmp_path / "t.lrc").write_bytes(FOREIGN)
    assert sidecars.read_sha256(Track(os.fsencode(tmp_path), b"t.flac", b"t"), Kind.TXT) is None


@pytest.mark.parametrize(
    "plant",
    [
        lambda path: path.symlink_to("t.flac"),
        lambda path: path.symlink_to("nowhere"),
        lambda path: path.mkdir(),
        lambda path: os.mkfifo(path),
    ],
    ids=["symlink", "dangling-symlink", "directory", "fifo"],
)
def test_read_sha256_gives_an_empty_string_for_anything_it_cannot_hash(
    tmp_path: Path, plant: Callable[[Path], None]
) -> None:
    (tmp_path / "t.flac").write_bytes(FOREIGN)
    plant(tmp_path / "t.lrc")
    assert sidecars.read_sha256(Track(os.fsencode(tmp_path), b"t.flac", b"t"), Kind.LRC) == ""


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_read_sha256_raises_for_a_file_it_cannot_open(tmp_path: Path) -> None:
    sidecar = tmp_path / "t.lrc"
    sidecar.write_bytes(FOREIGN)
    sidecar.chmod(0)
    with pytest.raises(PermissionError):
        sidecars.read_sha256(Track(os.fsencode(tmp_path), b"t.flac", b"t"), Kind.LRC)


def test_read_sha256_raises_for_a_read_error_part_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "t.lrc").write_bytes(FOREIGN)

    def fail(*_args: object) -> bytes:
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(os, "read", fail)

    with pytest.raises(OSError, match="Input/output"):
        sidecars.read_sha256(Track(os.fsencode(tmp_path), b"t.flac", b"t"), Kind.LRC)


def test_a_failed_read_means_not_held_rather_than_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = Track(os.fsencode(tmp_path), b"t.flac", b"t")
    sidecar = tmp_path / "t.lrc"
    sidecar.write_bytes(FOREIGN)
    digest = hashlib.sha256(FOREIGN).hexdigest()

    def fail(*_args: object) -> bytes:
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    with monkeypatch.context() as faults:
        faults.setattr(os, "read", fail)
        assert sidecars.remove_owned(track, Kind.LRC, digest) is False

    assert sidecar.read_bytes() == FOREIGN


# The load limit, with a clock and a sleep the tests own.


class FakeTime:
    """A clock that a sleep moves on, keeping what it was asked to sleep."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def fake_pacer(monkeypatch: pytest.MonkeyPatch) -> FakeTime:
    """Make every run in this test pace itself on a FakeTime."""
    fake = FakeTime()

    class FakePacer(pipeline._Pacer):
        def __init__(self, max_rate: float | None) -> None:
            super().__init__(max_rate, clock=fake.clock, sleep=fake.sleep)

    monkeypatch.setattr(pipeline, "_Pacer", FakePacer)
    return fake


def copy_tracks(tmp_path: Path, count: int) -> list[Path]:
    album = tmp_path / "lib" / "Album"
    album.mkdir(parents=True)
    return [
        Path(shutil.copyfile(FIXTURES / "t.flac", album / f"{index}.flac"))
        for index in range(count)
    ]


def test_rate_spacing_with_an_injected_clock() -> None:
    fake = FakeTime()
    pacer = pipeline._Pacer(2.0, clock=fake.clock, sleep=fake.sleep)

    pacer.wait()
    assert fake.sleeps == []

    fake.now += 0.1
    pacer.wait()
    assert fake.sleeps == pytest.approx([0.4])

    pacer.wait()
    assert fake.sleeps == pytest.approx([0.4, 0.5])


def test_rate_a_long_pause_earns_no_burst() -> None:
    fake = FakeTime()
    pacer = pipeline._Pacer(2.0, clock=fake.clock, sleep=fake.sleep)
    pacer.wait()

    fake.now += 3600.0
    pacer.wait()
    assert fake.sleeps == []

    # An hour's pause buys only the slot it ended on, and the next waits a full interval.
    pacer.wait()
    assert fake.sleeps == pytest.approx([0.5])


def test_rate_no_limit_never_sleeps(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTime()
    pacer = pipeline._Pacer(None, clock=fake.clock, sleep=fake.sleep)
    for _ in range(3):
        pacer.wait()
    assert fake.sleeps == []

    fake = fake_pacer(monkeypatch)
    copy_tracks(tmp_path, 3)
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)

    counts = pipeline.run(settings, os.fsencode(tmp_path / "lib"))

    assert counts.written_lrc == 3
    assert fake.sleeps == []


def test_rate_settled_tracks_are_not_paced(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_pacer(monkeypatch)
    copy_tracks(tmp_path, 3)
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch).model_copy(update={"max_rate": 2.0})

    first = pipeline.run(settings, os.fsencode(tmp_path / "lib"))

    assert first.written_lrc == 3
    # Once per track after the first: a tag read and its lookup share one slot.
    assert fake.sleeps == pytest.approx([0.5, 0.5])

    fake.sleeps.clear()
    second = pipeline.run(settings, os.fsencode(tmp_path / "lib"))

    assert second.synced == second.skipped_unchanged == 3
    assert fake.sleeps == []


def test_rate_settled_misses_are_not_paced_until_they_are_due(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_pacer(monkeypatch)
    copy_tracks(tmp_path, 3)
    stub_lrclib.status, stub_lrclib.body = 404, {"name": "TrackNotFound"}
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    rated = settings.model_copy(update={"max_rate": 2.0, "dump_id": "dump-a"})

    assert pipeline.run(rated, os.fsencode(tmp_path / "lib")).no_match == 3
    fake.sleeps.clear()

    settled = pipeline.run(rated, os.fsencode(tmp_path / "lib"))
    assert settled.skipped_unchanged == 3
    assert fake.sleeps == []

    due = rated.model_copy(update={"dump_id": "dump-b"})
    assert pipeline.run(due, os.fsencode(tmp_path / "lib")).looked_up == 3
    assert len(fake.sleeps) == 2


def test_rate_a_track_whose_file_changed_is_paced(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_pacer(monkeypatch)
    tracks = copy_tracks(tmp_path, 2)
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch).model_copy(update={"max_rate": 2.0})
    pipeline.run(settings, os.fsencode(tmp_path / "lib"))
    fake.sleeps.clear()
    for track in tracks:
        os.utime(track, (1_000_000_000, 1_000_000_000))

    pipeline.run(settings, os.fsencode(tmp_path / "lib"))

    # Revalidating reads the tags of both, so the second one waits for its slot.
    assert fake.sleeps == pytest.approx([0.5])


# The retry rule's columns are written from the tags a run reads (06-06)


def retry_columns(settings: Any, path: Path) -> tuple[object, ...]:
    import sqlite3

    con = sqlite3.connect(settings.state_db)
    try:
        found = con.execute(
            "SELECT release_year, artist_key, title_key, first_seen_at FROM tracks WHERE path = ?",
            (os.fsencode(path),),
        ).fetchone()
    finally:
        con.close()
    assert found is not None
    return tuple(found)


def test_a_run_stores_the_year_keys_and_first_seen(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = copy_track(tmp_path)
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    monkeypatch.setattr(tags, "read", lambda path: tags_with(year="2026-01-01"))

    pipeline.run(settings, os.fsencode(tmp_path / "lib"))
    first = retry_columns(settings, track)
    reads: list[bytes] = []

    def counted(path: bytes) -> TrackTags:
        reads.append(path)
        return tags_with()

    monkeypatch.setattr(tags, "read", counted)
    pipeline.run(settings, os.fsencode(tmp_path / "lib"))

    keys = matching.lookup_keys("Fixture Artist", "Fixture Title")
    assert first[:3] == (2026, *keys)
    assert isinstance(first[3], str)
    # The second run read no tags, as the stat didn't move, and kept every value.
    assert reads == []
    assert retry_columns(settings, track) == first


def test_keys_come_from_the_album_artist_when_the_artist_is_empty(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = copy_track(tmp_path)
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    monkeypatch.setattr(tags, "read", lambda path: tags_with(artist="", albumartist="Fixture Band"))

    pipeline.run(settings, os.fsencode(tmp_path / "lib"))

    artist_key = retry_columns(settings, track)[1]
    assert artist_key == matching.lookup_keys("Fixture Band", "x")[0]
