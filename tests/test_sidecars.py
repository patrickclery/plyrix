"""The writer's case table: never replace a file plyrix can't prove it wrote.

Faults are injected on the os module in this process, for sidecars only, since sidecars.py installs
no guard. All lyric-shaped data is invented placeholder text.
"""

import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from conftest import (
    FIXTURES,
    RECORD,
    RunCli,
    StubLrclib,
    non_notice_lines,
    parse_counts,
    sidecars_in,
    snap,
)
from plyrix import pipeline, sidecars, walker
from plyrix.config import Settings
from plyrix.domain import Kind, Track
from plyrix.sidecars import Result
from plyrix.state import db

PAYLOAD = b"[00:01.00] placeholder line 1\n"
FOREIGN = b"not written by plyrix\n"
NEW_PAYLOAD = b"[00:02.00] placeholder line 2\n"
HOUR = 3600.0
# Saved here, so a test can act as a rival after the faults below replace these.
REAL_LINK = os.link
REAL_REPLACE = os.replace


def track_in(directory: Path, stem: bytes = b"t", suffix: bytes = b".flac") -> Track:
    return Track(os.fsencode(directory), stem + suffix, stem)


def raising(code: int) -> Callable[..., None]:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError(code, os.strerror(code))

    return fail


def temps_in(directory: Path) -> list[bytes]:
    return [name for name in os.listdir(os.fsencode(directory)) if name.endswith(b".part")]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(params=["normal", "fallback"])
def link_path(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run a test on the hard-link path, and on the path for a filesystem without hard links."""
    if request.param == "fallback":
        monkeypatch.setattr(os, "link", raising(errno.EPERM))
    return str(request.param)


@pytest.fixture
def umask_022() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


# --- publish: what is already there stays


def test_publish_refuses_foreign(tmp_path: Path, link_path: str) -> None:
    sidecar = tmp_path / "t.lrc"
    sidecar.write_bytes(FOREIGN)
    sidecar.chmod(0o444)
    before = snap(sidecar)

    assert sidecars.publish(track_in(tmp_path), Kind.LRC, PAYLOAD) is Result.EXISTS

    assert snap(sidecar) == before
    assert temps_in(tmp_path) == []


def test_publish_refuses_a_dangling_symlink(tmp_path: Path, link_path: str) -> None:
    sidecar = tmp_path / "t.lrc"
    sidecar.symlink_to("nowhere")

    assert sidecars.publish(track_in(tmp_path), Kind.LRC, PAYLOAD) is Result.EXISTS

    assert sidecar.is_symlink()
    assert os.readlink(sidecar) == "nowhere"
    assert not (tmp_path / "nowhere").exists()
    assert temps_in(tmp_path) == []


def test_publish_refuses_a_symlink_to_audio(tmp_path: Path, link_path: str) -> None:
    audio = tmp_path / "t.flac"
    audio.write_bytes(FOREIGN)
    (tmp_path / "t.lrc").symlink_to("t.flac")
    before = snap(audio)

    assert sidecars.publish(track_in(tmp_path), Kind.LRC, PAYLOAD) is Result.EXISTS

    assert snap(audio) == before


def test_publish_refuses_a_hard_link_to_audio(tmp_path: Path, link_path: str) -> None:
    audio = tmp_path / "t.flac"
    audio.write_bytes(FOREIGN)
    REAL_LINK(audio, tmp_path / "t.lrc")
    before = snap(audio)

    assert sidecars.publish(track_in(tmp_path), Kind.LRC, PAYLOAD) is Result.EXISTS

    assert snap(audio) == before
    assert temps_in(tmp_path) == []


# --- publish: what it creates


def test_publish_mode_links_and_temps(tmp_path: Path, link_path: str, umask_022: None) -> None:
    assert sidecars.publish(track_in(tmp_path), Kind.LRC, PAYLOAD) is Result.WRITTEN

    status = os.lstat(tmp_path / "t.lrc")
    assert stat.S_IMODE(status.st_mode) == 0o644
    assert status.st_nlink == 1
    assert (tmp_path / "t.lrc").read_bytes() == PAYLOAD
    assert temps_in(tmp_path) == []


@pytest.mark.parametrize("suffix", [b".flac", b".opus"])
def test_publish_long_stem(tmp_path: Path, link_path: str, suffix: bytes) -> None:
    stem = b"s" * 250
    os.close(os.open(os.path.join(os.fsencode(tmp_path), stem + suffix), os.O_CREAT | os.O_WRONLY))

    assert sidecars.publish(track_in(tmp_path, stem, suffix), Kind.LRC, PAYLOAD) is Result.WRITTEN

    with open(os.path.join(os.fsencode(tmp_path), stem + b".lrc"), "rb") as handle:
        assert handle.read() == PAYLOAD
    assert temps_in(tmp_path) == []


def test_publish_keeps_a_non_utf8_stem(tmp_path: Path, link_path: str) -> None:
    stem = b"caf\xe9-\xff"

    assert sidecars.publish(track_in(tmp_path, stem), Kind.LRC, PAYLOAD) is Result.WRITTEN

    assert os.listdir(os.fsencode(tmp_path)) == [stem + b".lrc"]


def test_a_stem_ending_in_a_dot_is_kept(tmp_path: Path) -> None:
    (tmp_path / "U.S.A..flac").write_bytes(FOREIGN)
    root = os.fsencode(tmp_path)

    [track] = walker.walk(root, root, [])

    assert isinstance(track, Track)
    assert track.stem == b"U.S.A."
    assert sidecars.publish(track, Kind.LRC, PAYLOAD) is Result.WRITTEN
    assert (tmp_path / "U.S.A..lrc").read_bytes() == PAYLOAD


def test_txt_kind(tmp_path: Path, link_path: str, umask_022: None) -> None:
    track = track_in(tmp_path)

    assert sidecars.publish(track, Kind.TXT, PAYLOAD) is Result.WRITTEN
    assert sidecars.publish(track, Kind.TXT, NEW_PAYLOAD) is Result.EXISTS

    sidecar = tmp_path / "t.txt"
    assert sidecar.read_bytes() == PAYLOAD
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o644
    assert sorted(path.name for path in tmp_path.iterdir()) == ["t.txt"]


# --- publish: filesystems and faults


@pytest.mark.parametrize(
    "code",
    [errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EMLINK],
    ids=["EPERM", "ENOTSUP", "EOPNOTSUPP", "ENOSYS", "EMLINK"],
)
def test_publish_falls_back_without_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    monkeypatch.setattr(os, "link", raising(code))

    assert sidecars.publish(track_in(tmp_path), Kind.LRC, PAYLOAD) is Result.WRITTEN

    assert (tmp_path / "t.lrc").read_bytes() == PAYLOAD
    assert temps_in(tmp_path) == []


def test_publish_records_the_claim_before_making_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "link", raising(errno.EPERM))
    seen: list[bool] = []

    class Log(sidecars.PublishLog):
        def claim_starting(self) -> None:
            seen.append((tmp_path / "t.lrc").exists())

    assert sidecars.publish(track_in(tmp_path), Kind.LRC, PAYLOAD, Log()) is Result.WRITTEN

    assert seen == [False]


def test_publish_records_no_claim_on_the_hard_link_path(tmp_path: Path) -> None:
    seen: list[bool] = []

    class Log(sidecars.PublishLog):
        def claim_starting(self) -> None:
            seen.append(True)

    assert sidecars.publish(track_in(tmp_path), Kind.LRC, PAYLOAD, Log()) is Result.WRITTEN

    assert seen == []


def test_publish_exdev_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "link", raising(errno.EXDEV))

    assert sidecars.publish(track_in(tmp_path), Kind.LRC, PAYLOAD) is Result.UNSUPPORTED

    assert list(tmp_path.iterdir()) == []


def test_publish_exdev_from_replace_removes_the_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "link", raising(errno.EPERM))
    monkeypatch.setattr(os, "replace", raising(errno.EXDEV))

    assert sidecars.publish(track_in(tmp_path), Kind.LRC, PAYLOAD) is Result.UNSUPPORTED

    assert list(tmp_path.iterdir()) == []


def test_publish_other_replace_errors_raise_and_clean_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "link", raising(errno.EPERM))
    monkeypatch.setattr(os, "replace", raising(errno.EIO))

    with pytest.raises(OSError, match="Input/output"):
        sidecars.publish(track_in(tmp_path), Kind.LRC, PAYLOAD)

    assert list(tmp_path.iterdir()) == []


def test_publish_other_link_errors_raise_and_clean_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "link", raising(errno.EIO))

    with pytest.raises(OSError, match="Input/output"):
        sidecars.publish(track_in(tmp_path), Kind.LRC, PAYLOAD)

    assert list(tmp_path.iterdir()) == []


def failing_temp_unlink(code: int) -> Callable[[bytes], None]:
    """Return an os.unlink that fails on a temp name and works on anything else."""
    real_unlink = os.unlink

    def unlink(path: bytes) -> None:
        if path.endswith(b".part"):
            raise OSError(code, os.strerror(code))
        real_unlink(path)

    return unlink


@pytest.mark.parametrize("code", [errno.EIO, errno.EROFS, errno.EACCES])
def test_a_failed_temp_cleanup_does_not_replace_a_finished_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    monkeypatch.setattr(os, "unlink", failing_temp_unlink(code))

    assert sidecars.publish(track_in(tmp_path), Kind.LRC, PAYLOAD) is Result.WRITTEN

    assert (tmp_path / "t.lrc").read_bytes() == PAYLOAD


def test_a_failed_temp_cleanup_does_not_replace_an_exists_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "t.lrc").write_bytes(FOREIGN)
    monkeypatch.setattr(os, "unlink", failing_temp_unlink(errno.EIO))

    assert sidecars.publish(track_in(tmp_path), Kind.LRC, PAYLOAD) is Result.EXISTS


def test_a_failed_temp_cleanup_does_not_replace_a_refused_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "t.lrc").write_bytes(PAYLOAD)
    monkeypatch.setattr(os, "unlink", failing_temp_unlink(errno.EIO))
    # The file changes after the first check, so the temp already written has to go.
    answers = iter([True, False])
    monkeypatch.setattr(sidecars, "_holds", lambda *_args: next(answers))

    assert sidecars.replace_owned(track_in(tmp_path), Kind.LRC, NEW_PAYLOAD, sha256(PAYLOAD)) is (
        Result.EXISTS
    )


def test_a_failed_temp_cleanup_does_not_replace_the_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "fsync", raising(errno.ENOSPC))
    monkeypatch.setattr(os, "unlink", failing_temp_unlink(errno.EIO))

    with pytest.raises(OSError, match="No space left"):
        sidecars.publish(track_in(tmp_path), Kind.LRC, PAYLOAD)


def test_publish_a_failed_data_fsync_raises_and_clean_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "fsync", raising(errno.EIO))

    with pytest.raises(OSError, match="Input/output"):
        sidecars.publish(track_in(tmp_path), Kind.LRC, PAYLOAD)

    assert list(tmp_path.iterdir()) == []


def _rival_rewrites_the_claim(sidecar: Path) -> None:
    sidecar.write_bytes(FOREIGN)


def _rival_replaces_the_claim(sidecar: Path) -> None:
    rival = sidecar.with_name("rival")
    rival.write_bytes(FOREIGN)
    REAL_REPLACE(rival, sidecar)


@pytest.mark.parametrize("rival", [_rival_rewrites_the_claim, _rival_replaces_the_claim])
def test_a_rival_taking_the_claim_keeps_its_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rival: Callable[[Path], None]
) -> None:
    sidecar = tmp_path / "t.lrc"
    monkeypatch.setattr(os, "link", raising(errno.EPERM))
    real_lstat = os.lstat

    def lstat_after_rival(path: bytes) -> os.stat_result:
        rival(sidecar)
        return real_lstat(path)

    monkeypatch.setattr(os, "lstat", lstat_after_rival)

    result = sidecars.publish(track_in(tmp_path), Kind.LRC, PAYLOAD)

    monkeypatch.undo()
    assert result is Result.EXISTS
    assert sidecar.read_bytes() == FOREIGN
    assert temps_in(tmp_path) == []


def test_a_failed_replace_never_removes_a_rival(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = tmp_path / "t.lrc"
    monkeypatch.setattr(os, "link", raising(errno.EPERM))

    def rival_then_fail(*_args: object) -> None:
        _rival_replaces_the_claim(sidecar)
        raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))

    monkeypatch.setattr(os, "replace", rival_then_fail)

    assert sidecars.publish(track_in(tmp_path), Kind.LRC, PAYLOAD) is Result.UNSUPPORTED

    monkeypatch.undo()
    assert sidecar.read_bytes() == FOREIGN


def test_a_directory_that_cannot_fsync_still_counts_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = os.fsync

    def fsync_files_only(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fsync_files_only)

    assert sidecars.publish(track_in(tmp_path), Kind.LRC, PAYLOAD) is Result.WRITTEN

    assert (tmp_path / "t.lrc").read_bytes() == PAYLOAD


# --- publish: a real race between two processes

RACER = """
import errno, os, sys
from plyrix import sidecars
from plyrix.domain import Kind, Track

directory, fill, path = sys.argv[1:]
if path == "fallback":
    def no_hard_links(*_args, **_kwargs):
        raise OSError(errno.EPERM, "no hard links")
    os.link = no_hard_links
payload = fill.encode() * 500_000
print("ready", flush=True)
sys.stdin.readline()
result = sidecars.publish(Track(os.fsencode(directory), b"t.flac", b"t"), Kind.LRC, payload)
print(result.name, flush=True)
"""


@pytest.mark.parametrize("path", ["normal", "fallback"])
def test_publish_race_one_winner(tmp_path: Path, path: str) -> None:
    for attempt in range(5):
        directory = tmp_path / str(attempt)
        directory.mkdir()
        racers = {
            fill: subprocess.Popen(
                [sys.executable, "-c", RACER, str(directory), fill, path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
            for fill in ("a", "b")
        }
        for racer in racers.values():
            assert racer.stdout is not None
            assert racer.stdout.readline() == b"ready\n"
        for racer in racers.values():
            assert racer.stdin is not None
            racer.stdin.write(b"go\n")
            racer.stdin.flush()
        results = {
            fill: racer.communicate(timeout=60)[0].decode().strip()
            for fill, racer in racers.items()
        }

        assert sorted(results.values()) == ["EXISTS", "WRITTEN"]
        winner = next(fill for fill, result in results.items() if result == "WRITTEN")
        assert (directory / "t.lrc").read_bytes() == winner.encode() * 500_000
        assert sorted(item.name for item in directory.iterdir()) == ["t.lrc"]


# --- encode

ANY_TEXT = st.text(
    st.one_of(
        st.characters(exclude_categories=()),
        st.sampled_from(["\ufeff", "\r", "\n", "\t", "\x00", "\x1b", "\x1f", "\x0b", "\x0c"]),
    )
)


@given(ANY_TEXT)
def test_encode_is_clean_utf8(text: str) -> None:
    decoded = sidecars.encode(text).decode("utf-8")

    assert "\ufeff" not in decoded
    assert "\r" not in decoded
    assert all(char in "\n\t" or ord(char) >= 0x20 for char in decoded)


def test_encode_normalises_a_small_example() -> None:
    raw = "\ufeff  [00:01.00] one\x1b\r\n[00:02.00]\ttwo\rthree\n\n"
    assert sidecars.encode(raw) == b"[00:01.00] one\n[00:02.00]\ttwo\nthree\n"


# --- remove_owned and replace_owned: only a file the ledger proves ours


def test_remove_owned_removes_on_a_match(tmp_path: Path) -> None:
    sidecar = tmp_path / "t.lrc"
    sidecar.write_bytes(PAYLOAD)

    assert sidecars.remove_owned(track_in(tmp_path), Kind.LRC, sha256(PAYLOAD)) is True

    assert not sidecar.exists()


def test_remove_owned_refuses_foreign_on_a_hash_mismatch(tmp_path: Path) -> None:
    sidecar = tmp_path / "t.lrc"
    sidecar.write_bytes(FOREIGN)
    before = snap(sidecar)

    assert sidecars.remove_owned(track_in(tmp_path), Kind.LRC, sha256(PAYLOAD)) is False

    assert snap(sidecar) == before


def test_remove_owned_does_not_follow_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "moved.bak"
    target.write_bytes(PAYLOAD)
    sidecar = tmp_path / "t.lrc"
    sidecar.symlink_to("moved.bak")
    before = snap(target)

    assert sidecars.remove_owned(track_in(tmp_path), Kind.LRC, sha256(PAYLOAD)) is False

    assert sidecar.is_symlink()
    assert snap(target) == before


def test_remove_owned_with_nothing_there(tmp_path: Path) -> None:
    assert sidecars.remove_owned(track_in(tmp_path), Kind.LRC, sha256(PAYLOAD)) is False


def test_remove_owned_never_matches_something_unhashable(tmp_path: Path) -> None:
    (tmp_path / "t.lrc").symlink_to("nowhere")
    (tmp_path / "t.txt").mkdir()

    # read_sha256 gives an empty string for these, which must not count as a match.
    assert sidecars.remove_owned(track_in(tmp_path), Kind.LRC, "") is False
    assert sidecars.remove_owned(track_in(tmp_path), Kind.TXT, "") is False
    assert sidecars.remove_owned(track_in(tmp_path), Kind.LRC, sha256(PAYLOAD)) is False

    assert (tmp_path / "t.lrc").is_symlink()
    assert (tmp_path / "t.txt").is_dir()


def test_replace_owned_replaces_on_a_match(tmp_path: Path, umask_022: None) -> None:
    sidecar = tmp_path / "t.lrc"
    sidecar.write_bytes(PAYLOAD)

    result = sidecars.replace_owned(track_in(tmp_path), Kind.LRC, NEW_PAYLOAD, sha256(PAYLOAD))

    assert result is Result.WRITTEN
    assert sidecar.read_bytes() == NEW_PAYLOAD
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o644
    assert temps_in(tmp_path) == []


def test_replace_owned_writes_txt(tmp_path: Path) -> None:
    (tmp_path / "t.txt").write_bytes(PAYLOAD)

    result = sidecars.replace_owned(track_in(tmp_path), Kind.TXT, NEW_PAYLOAD, sha256(PAYLOAD))

    assert result is Result.WRITTEN
    assert (tmp_path / "t.txt").read_bytes() == NEW_PAYLOAD


def test_replace_owned_refuses_foreign_on_a_hash_mismatch(tmp_path: Path) -> None:
    sidecar = tmp_path / "t.lrc"
    sidecar.write_bytes(FOREIGN)
    before = snap(sidecar)
    folder_before = tmp_path.stat().st_mtime_ns

    result = sidecars.replace_owned(track_in(tmp_path), Kind.LRC, NEW_PAYLOAD, sha256(PAYLOAD))

    assert result is Result.EXISTS
    assert snap(sidecar) == before
    # An unchanged folder time shows a refusal created no temp at all.
    assert tmp_path.stat().st_mtime_ns == folder_before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["t.lrc"]


def test_replace_owned_does_not_follow_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "moved.bak"
    target.write_bytes(PAYLOAD)
    sidecar = tmp_path / "t.lrc"
    sidecar.symlink_to("moved.bak")
    before = snap(target)

    result = sidecars.replace_owned(track_in(tmp_path), Kind.LRC, NEW_PAYLOAD, sha256(PAYLOAD))

    assert result is Result.EXISTS
    assert sidecar.is_symlink()
    assert snap(target) == before


def test_replace_owned_with_nothing_there_creates_nothing(tmp_path: Path) -> None:
    result = sidecars.replace_owned(track_in(tmp_path), Kind.LRC, NEW_PAYLOAD, sha256(PAYLOAD))

    assert result is Result.EXISTS
    assert list(tmp_path.iterdir()) == []


def test_replace_owned_exdev_leaves_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = tmp_path / "t.lrc"
    sidecar.write_bytes(PAYLOAD)
    before = snap(sidecar)
    monkeypatch.setattr(os, "replace", raising(errno.EXDEV))

    result = sidecars.replace_owned(track_in(tmp_path), Kind.LRC, NEW_PAYLOAD, sha256(PAYLOAD))

    monkeypatch.undo()
    assert result is Result.UNSUPPORTED
    assert snap(sidecar) == before
    assert temps_in(tmp_path) == []


def test_replace_owned_refuses_a_file_edited_during_the_temp_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = tmp_path / "t.lrc"
    sidecar.write_bytes(PAYLOAD)
    real_fsync = os.fsync

    def fsync_after_an_edit(descriptor: int) -> None:
        sidecar.write_bytes(FOREIGN)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fsync_after_an_edit)

    result = sidecars.replace_owned(track_in(tmp_path), Kind.LRC, NEW_PAYLOAD, sha256(PAYLOAD))

    monkeypatch.undo()
    assert result is Result.EXISTS
    assert sidecar.read_bytes() == FOREIGN
    assert temps_in(tmp_path) == []


# --- sweep_temps


def make_aged(directory: Path, name: str, age: float, now: float) -> Path:
    path = directory / name
    path.write_bytes(PAYLOAD)
    os.utime(path, (now - age, now - age))
    return path


def test_sweep_removes_only_old_exact_temps(tmp_path: Path) -> None:
    now = time.time()
    old = make_aged(tmp_path, ".plyrix-0123abcd.part", HOUR + 60, now)
    stay = [
        make_aged(tmp_path, ".plyrix-89abcdef.part", HOUR - 60, now),
        make_aged(tmp_path, ".plyrix-0123abc.part", HOUR * 2, now),
        make_aged(tmp_path, ".plyrix-0123abcde.part", HOUR * 2, now),
        make_aged(tmp_path, ".plyrix-0123ABCD.part", HOUR * 2, now),
        make_aged(tmp_path, ".plyrix-0123abcd.part.bak", HOUR * 2, now),
        make_aged(tmp_path, "x.plyrix-0123abcd.part", HOUR * 2, now),
        make_aged(tmp_path, "t.flac", HOUR * 2, now),
        make_aged(tmp_path, "t.lrc", HOUR * 2, now),
    ]

    assert sidecars.sweep_temps(os.fsencode(tmp_path), now) == 1

    assert not old.exists()
    assert all(path.exists() for path in stay)


def test_sweep_keeps_a_temp_exactly_one_hour_old(tmp_path: Path) -> None:
    now = time.time()
    temp = make_aged(tmp_path, ".plyrix-0123abcd.part", HOUR, now)

    assert sidecars.sweep_temps(os.fsencode(tmp_path), now) == 0

    assert temp.exists()


def test_sweep_leaves_anything_but_a_regular_file(tmp_path: Path) -> None:
    now = time.time()
    link = tmp_path / ".plyrix-0123abcd.part"
    link.symlink_to("target")
    os.utime(link, (now - HOUR * 2, now - HOUR * 2), follow_symlinks=False)
    target = make_aged(tmp_path, "target", HOUR * 2, now)
    (tmp_path / ".plyrix-89abcdef.part").mkdir()
    os.utime(tmp_path / ".plyrix-89abcdef.part", (now - HOUR * 2, now - HOUR * 2))

    assert sidecars.sweep_temps(os.fsencode(tmp_path), now) == 0

    assert link.is_symlink()
    assert target.exists()
    assert (tmp_path / ".plyrix-89abcdef.part").is_dir()


def test_sweep_of_a_missing_folder_finds_nothing(tmp_path: Path) -> None:
    assert sidecars.sweep_temps(os.fsencode(tmp_path / "gone"), time.time()) == 0


# --- through the pipeline


def settings_for(tmp_path: Path, stub: StubLrclib, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return Settings(
        music_root=tmp_path / "lib",
        lrclib_url=stub.base_url,
        state_db=tmp_path / "state" / "state.db",
    )


def copy_tracks(tmp_path: Path, count: int) -> list[Path]:
    album = tmp_path / "lib" / "Album"
    album.mkdir(parents=True)
    tracks = []
    for index in range(count):
        track = album / f"{index}.flac"
        track.write_bytes((FIXTURES / "t.flac").read_bytes())
        tracks.append(track)
    return tracks


def test_exdev_counts_each_track_and_warns_once(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tracks = copy_tracks(tmp_path, 3)
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    monkeypatch.setattr(os, "link", raising(errno.EXDEV))

    counts = pipeline.run(settings, os.fsencode(tmp_path / "lib"))

    assert counts.fs_no_atomic_publish == 3
    assert counts.written_lrc == 0
    assert sorted(path.name for path in tracks[0].parent.iterdir()) == [
        track.name for track in tracks
    ]
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    assert "3" in lines[0]
    assert str(tmp_path) not in lines[0]
    assert ".flac" not in lines[0]
    conn = db.connect(settings.state_db)
    assert conn.execute("SELECT COUNT(*) FROM sidecars").fetchone()[0] == 0
    conn.close()


def test_no_warning_when_nothing_hit_exdev(
    tmp_path: Path,
    stub_lrclib: StubLrclib,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    copy_tracks(tmp_path, 1)
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)

    counts = pipeline.run(settings, os.fsencode(tmp_path / "lib"))

    assert counts.written_lrc == 1
    assert capsys.readouterr().err == ""


def test_a_foreign_file_appearing_before_publish_stays_on_the_fallback_path(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    [track] = copy_tracks(tmp_path, 1)
    sidecar = track.with_suffix(".lrc")
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch)
    monkeypatch.setattr(os, "link", raising(errno.EPERM))
    real_publish = sidecars.publish

    def publish_after_a_rival(*args: Any) -> Result:
        sidecar.write_bytes(FOREIGN)
        return real_publish(*args)

    monkeypatch.setattr(sidecars, "publish", publish_after_a_rival)

    counts = pipeline.run(settings, os.fsencode(tmp_path / "lib"))

    assert counts.blocked_foreign == 1
    assert counts.written_lrc == 0
    assert sidecar.read_bytes() == FOREIGN
    conn = db.connect(settings.state_db)
    assert conn.execute("SELECT COUNT(*) FROM sidecars").fetchone()[0] == 0
    conn.close()


def test_file_stat_gives_a_regular_files_size_and_mtime(tmp_path: Path) -> None:
    track = Track(os.fsencode(tmp_path), b"t.flac", b"t")
    sidecar = tmp_path / "t.lrc"
    sidecar.write_bytes(b"abc")

    assert sidecars.file_stat(track, Kind.LRC) == (3, sidecar.stat().st_mtime_ns)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_is_readable_follows_permissions(tmp_path: Path) -> None:
    track = Track(os.fsencode(tmp_path), b"t.flac", b"t")
    sidecar = tmp_path / "t.lrc"
    sidecar.write_bytes(b"abc")
    assert sidecars.is_readable(track, Kind.LRC) is True

    sidecar.chmod(0)
    try:
        assert sidecars.is_readable(track, Kind.LRC) is False
    finally:
        sidecar.chmod(0o644)


def test_file_stat_is_none_for_anything_else(tmp_path: Path) -> None:
    track = Track(os.fsencode(tmp_path), b"t.flac", b"t")
    assert sidecars.file_stat(track, Kind.LRC) is None
    (tmp_path / "target").write_bytes(b"abc")
    (tmp_path / "t.lrc").symlink_to("target")
    assert sidecars.file_stat(track, Kind.LRC) is None
    (tmp_path / "t.txt").mkdir()
    assert sidecars.file_stat(track, Kind.TXT) is None
    gone = Track(os.fsencode(tmp_path / "t.lrc" / "x"), b"t.flac", b"t")
    assert sidecars.file_stat(gone, Kind.LRC) is None


# --- read_bounded: a foreign file's text, without trusting its size or kind


def test_read_bounded_returns_the_text(tmp_path: Path) -> None:
    (tmp_path / "t.lrc").write_bytes("[00:01.00] caf\u00e9\r\n".encode())

    assert sidecars.read_bounded(track_in(tmp_path), Kind.LRC) == "[00:01.00] caf\u00e9\r\n"


def test_read_bounded_says_none_when_nothing_is_there(tmp_path: Path) -> None:
    assert sidecars.read_bounded(track_in(tmp_path), Kind.LRC) is None


def test_read_bounded_allows_a_file_of_exactly_the_limit(tmp_path: Path) -> None:
    (tmp_path / "t.txt").write_bytes(b"x" * 10)

    assert sidecars.read_bounded(track_in(tmp_path), Kind.TXT, limit=10) == "x" * 10


def test_read_bounded_refuses_a_larger_file_without_reading_it_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "t.lrc").write_bytes(b"x" * (3 * 1024 * 1024))
    real_read = os.read
    asked: list[int] = []

    def counting_read(descriptor: int, size: int) -> bytes:
        asked.append(size)
        return real_read(descriptor, size)

    monkeypatch.setattr(os, "read", counting_read)

    assert sidecars.read_bounded(track_in(tmp_path), Kind.LRC) is None
    assert sum(asked) <= 2 * 1024 * 1024 + 1


def test_read_bounded_refuses_undecodable_bytes(tmp_path: Path) -> None:
    (tmp_path / "t.lrc").write_bytes(b"[00:01.00] \xff\xfe\n")

    assert sidecars.read_bounded(track_in(tmp_path), Kind.LRC) is None


def test_read_bounded_refuses_a_symlink(tmp_path: Path) -> None:
    (tmp_path / "other").write_bytes(PAYLOAD)
    (tmp_path / "t.lrc").symlink_to(tmp_path / "other")

    assert sidecars.read_bounded(track_in(tmp_path), Kind.LRC) is None


def test_read_bounded_refuses_a_fifo_without_waiting_on_it(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "t.lrc")

    assert sidecars.read_bounded(track_in(tmp_path), Kind.LRC) is None


def test_read_bounded_refuses_a_directory(tmp_path: Path) -> None:
    (tmp_path / "t.lrc").mkdir()

    assert sidecars.read_bounded(track_in(tmp_path), Kind.LRC) is None


def test_read_bounded_refuses_a_swap_to_a_fifo_after_the_lstat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.mkfifo(tmp_path / "t.lrc")
    regular = os.lstat(os.fsencode(tmp_path))
    real_lstat = os.lstat

    def looks_regular(path: bytes) -> os.stat_result:
        found = real_lstat(path)
        fields = list(found)
        fields[0] = stat.S_IFREG | stat.S_IMODE(regular.st_mode)
        return os.stat_result(fields)

    monkeypatch.setattr(os, "lstat", looks_regular)

    assert sidecars.read_bounded(track_in(tmp_path), Kind.LRC) is None


def test_read_bounded_raises_on_a_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "t.lrc").write_bytes(PAYLOAD)
    monkeypatch.setattr(os, "read", raising(errno.EIO))

    with pytest.raises(OSError, match="Input/output"):
        sidecars.read_bounded(track_in(tmp_path), Kind.LRC)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_read_bounded_raises_when_a_file_cant_be_opened(tmp_path: Path) -> None:
    sidecar = tmp_path / "t.lrc"
    sidecar.write_bytes(PAYLOAD)
    sidecar.chmod(0)
    try:
        with pytest.raises(OSError):
            sidecars.read_bounded(track_in(tmp_path), Kind.LRC)
    finally:
        sidecar.chmod(0o644)


class TempLog(sidecars.PublishLog):
    """Keep what a writer says about its temp, with whether the temp was there each time."""

    def __init__(self) -> None:
        self.events: list[tuple[str, bytes, bool]] = []

    def temp_chosen(self, temp: bytes) -> None:
        self.events.append(("chosen", temp, os.path.exists(temp)))

    def temp_gone(self, temp: bytes) -> None:
        self.events.append(("gone", temp, os.path.exists(temp)))


def test_replace_owned_records_its_temp(tmp_path: Path) -> None:
    (tmp_path / "t.lrc").write_bytes(PAYLOAD)
    log = TempLog()

    result = sidecars.replace_owned(track_in(tmp_path), Kind.LRC, NEW_PAYLOAD, sha256(PAYLOAD), log)

    assert result is Result.WRITTEN
    [(first, temp, there_at_first), (second, same, there_after)] = log.events
    assert (first, there_at_first, second, there_after) == ("chosen", False, "gone", False)
    assert same == temp
    assert os.path.dirname(temp) == os.fsencode(tmp_path)
    assert temps_in(tmp_path) == []


def test_replace_owned_tells_its_log_about_a_temp_it_removed_on_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = tmp_path / "t.lrc"
    sidecar.write_bytes(PAYLOAD)
    real_fsync = os.fsync
    log = TempLog()

    def fsync_after_an_edit(descriptor: int) -> None:
        sidecar.write_bytes(FOREIGN)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fsync_after_an_edit)

    result = sidecars.replace_owned(track_in(tmp_path), Kind.LRC, NEW_PAYLOAD, sha256(PAYLOAD), log)

    monkeypatch.undo()
    assert result is Result.EXISTS
    assert [event[0] for event in log.events] == ["chosen", "gone"]
    assert sidecar.read_bytes() == FOREIGN


def test_replace_owned_says_nothing_of_a_temp_it_never_chose(tmp_path: Path) -> None:
    (tmp_path / "t.lrc").write_bytes(FOREIGN)
    log = TempLog()

    result = sidecars.replace_owned(track_in(tmp_path), Kind.LRC, NEW_PAYLOAD, sha256(PAYLOAD), log)

    assert result is Result.EXISTS
    assert log.events == []


def test_replace_owned_keeps_a_temp_it_couldnt_remove_on_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "t.lrc").write_bytes(PAYLOAD)
    log = TempLog()
    monkeypatch.setattr(os, "replace", raising(errno.EIO))
    monkeypatch.setattr(os, "unlink", raising(errno.EACCES))

    with pytest.raises(OSError, match="Input/output"):
        sidecars.replace_owned(track_in(tmp_path), Kind.LRC, NEW_PAYLOAD, sha256(PAYLOAD), log)

    monkeypatch.undo()
    assert [event[0] for event in log.events] == ["chosen"]
    assert len(temps_in(tmp_path)) == 1


# How sidecars are created. The method only chooses between the hard link and the claim.

COUNTED_RUN = """
import json, sys

from plyrix import cli

links = []


def count_links(event, _args):
    if event == "os.link":
        links.append(1)


log, *argv = sys.argv[1:]
sys.addaudithook(count_links)
try:
    cli.app(argv)
finally:
    with open(log, "w", encoding="utf-8") as file:
        json.dump(len(links), file)
"""


def counted_run(
    tmp_path: Path, stub: StubLrclib, lib: Path, *args: str, **env: str
) -> tuple["subprocess.CompletedProcess[bytes]", int]:
    """Run the real command in a child, and return it with how many os.link events it raised."""
    log = tmp_path / "links.json"
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("PLYRIX_")}
    proc = subprocess.run(
        [sys.executable, "-c", COUNTED_RUN, log, "run", lib, *args],
        env={
            **inherited,
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "PLYRIX_LRCLIB_URL": stub.base_url,
            **env,
        },
        capture_output=True,
        check=False,
        timeout=120,
    )
    links = json.loads(log.read_text(encoding="utf-8"))
    assert isinstance(links, int)
    return proc, links


def test_method_plain_makes_no_link(tmp_path: Path, stub_lrclib: StubLrclib) -> None:
    [track] = copy_tracks(tmp_path, 1)
    lib = tmp_path / "lib"
    before = snap(track)

    proc, links = counted_run(tmp_path, stub_lrclib, lib, PLYRIX_SIDECAR_METHOD="plain")

    assert proc.returncode == 0, proc.stderr
    assert links == 0
    assert parse_counts(proc.stdout)["written_lrc"] == 1
    expected = sidecars.encode(str(RECORD["syncedLyrics"]))
    assert track.with_suffix(".lrc").read_bytes() == expected
    assert snap(track) == before
    assert temps_in(track.parent) == []


def test_method_plain_flag_makes_no_link(tmp_path: Path, stub_lrclib: StubLrclib) -> None:
    [track] = copy_tracks(tmp_path, 1)

    proc, links = counted_run(tmp_path, stub_lrclib, tmp_path / "lib", "--sidecar-method", "plain")

    assert proc.returncode == 0, proc.stderr
    assert links == 0
    assert track.with_suffix(".lrc").exists()


@pytest.mark.parametrize("env", [{}, {"PLYRIX_SIDECAR_METHOD": "auto"}], ids=["default", "auto"])
def test_method_auto_links(tmp_path: Path, stub_lrclib: StubLrclib, env: dict[str, str]) -> None:
    [track] = copy_tracks(tmp_path, 1)

    proc, links = counted_run(tmp_path, stub_lrclib, tmp_path / "lib", **env)

    assert proc.returncode == 0, proc.stderr
    assert links >= 1
    assert track.with_suffix(".lrc").exists()


def test_method_plain_never_replaces_what_is_there(tmp_path: Path, stub_lrclib: StubLrclib) -> None:
    [track] = copy_tracks(tmp_path, 1)
    sidecar = track.with_suffix(".lrc")
    sidecar.write_bytes(FOREIGN)

    proc, links = counted_run(
        tmp_path, stub_lrclib, tmp_path / "lib", PLYRIX_SIDECAR_METHOD="plain"
    )

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["blocked_foreign"] == 1
    assert links == 0
    assert sidecar.read_bytes() == FOREIGN


def test_method_plain_never_replaces_a_rival_that_got_there_first(
    tmp_path: Path, stub_lrclib: StubLrclib, monkeypatch: pytest.MonkeyPatch
) -> None:
    [track] = copy_tracks(tmp_path, 1)
    sidecar = track.with_suffix(".lrc")
    settings = settings_for(tmp_path, stub_lrclib, monkeypatch).model_copy(
        update={"sidecar_method": "plain"}
    )
    monkeypatch.setattr(os, "link", raising(errno.EIO))
    real_publish = sidecars.publish

    def publish_after_a_rival(*args: Any) -> Result:
        sidecar.write_bytes(FOREIGN)
        return real_publish(*args)

    monkeypatch.setattr(sidecars, "publish", publish_after_a_rival)

    counts = pipeline.run(settings, os.fsencode(tmp_path / "lib"))

    assert counts.blocked_foreign == 1
    assert counts.written_lrc == 0
    assert counts.write_failed == 0
    assert sidecar.read_bytes() == FOREIGN
    assert temps_in(track.parent) == []


def test_method_plain_txt(tmp_path: Path, stub_lrclib: StubLrclib) -> None:
    [track] = copy_tracks(tmp_path, 1)
    stub_lrclib.body = {**RECORD, "syncedLyrics": None}

    proc, links = counted_run(
        tmp_path, stub_lrclib, tmp_path / "lib", PLYRIX_SIDECAR_METHOD="plain"
    )

    assert proc.returncode == 0, proc.stderr
    assert links == 0
    assert parse_counts(proc.stdout)["written_txt"] == 1
    assert track.with_suffix(".txt").read_bytes() == sidecars.encode(str(RECORD["plainLyrics"]))
    assert not track.with_suffix(".lrc").exists()


def test_method_plain_tells_the_log_about_its_claim_as_the_fallback_does(tmp_path: Path) -> None:
    events: list[str] = []

    class Log(sidecars.PublishLog):
        def temp_chosen(self, temp: bytes) -> None:
            events.append("temp_chosen")

        def temp_gone(self, temp: bytes) -> None:
            events.append("temp_gone")

        def claim_starting(self) -> None:
            events.append("claim_starting")

        def claim_gone(self) -> None:
            events.append("claim_gone")

    track = track_in(tmp_path)

    assert (
        sidecars.publish(track, Kind.LRC, PAYLOAD, Log(), sidecars.Method.PLAIN) is Result.WRITTEN
    )
    assert events == ["temp_chosen", "claim_starting", "temp_gone"]
    assert (tmp_path / "t.lrc").read_bytes() == PAYLOAD
    assert temps_in(tmp_path) == []

    events.clear()
    assert (
        sidecars.publish(track, Kind.LRC, NEW_PAYLOAD, Log(), sidecars.Method.PLAIN)
        is Result.EXISTS
    )
    assert events == ["temp_chosen", "claim_starting", "claim_gone", "temp_gone"]
    assert (tmp_path / "t.lrc").read_bytes() == PAYLOAD
    assert temps_in(tmp_path) == []


@pytest.mark.parametrize("source", ["flag", "variable"])
def test_method_invalid_setting_exits_2(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, source: str
) -> None:
    copy_tracks(tmp_path, 1)
    lib = tmp_path / "lib"
    args: list[str | Path] = ["run", lib]
    env = {"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    if source == "flag":
        args += ["--sidecar-method", "copy"]
    else:
        env["PLYRIX_SIDECAR_METHOD"] = "copy"

    proc = run_cli(args, env=env)

    assert proc.returncode == 2, proc.stderr
    assert non_notice_lines(proc.stderr) == [
        b"plyrix: invalid settings: sidecar_method (PLYRIX_SIDECAR_METHOD)"
    ]
    assert sidecars_in(lib) == []
    assert stub_lrclib.requests == []
