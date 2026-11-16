"""The Lidarr spool: one complete file per event or none, read back strictly."""

import os
import re
import stat
from pathlib import Path
from typing import Any

import pytest

from conftest import lidarr_env
from plyrix import spool
from plyrix.config import ConfigError, Settings

NAME = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}\.event$")


def fields() -> dict[str, str]:
    return {
        "Lidarr_EventType": "AlbumDownload",
        "Lidarr_AddedTrackPaths": "/m/A\n1|x=y.flac|/m/A/2.flac",
        "Lidarr_DeletedPaths": "",
        "Lidarr_Artist_Path": "/m/A=B",
        "Lidarr_Album_Id": "12",
    }


def test_put_then_read_round_trips(tmp_path: Path) -> None:
    folder = tmp_path / "spool"

    path = spool.put(folder, fields())

    assert NAME.fullmatch(path.name)
    assert spool.read(path) == fields()
    assert [entry.name for entry in folder.iterdir()] == [path.name]


def test_a_non_utf8_path_survives(tmp_path: Path) -> None:
    odd = os.fsdecode(b"/m/A\xff/t.flac")

    path = spool.put(tmp_path / "spool", {**lidarr_env(added=[odd])})

    assert spool.read(path)["Lidarr_AddedTrackPaths"] == odd


def test_put_makes_a_private_folder(tmp_path: Path) -> None:
    folder = tmp_path / "spool"

    spool.put(folder, fields())

    assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_put_makes_missing_parents_private(tmp_path: Path) -> None:
    folder = tmp_path / "state" / "plyrix" / "spool"

    spool.put(folder, fields())

    for made in (tmp_path / "state", tmp_path / "state" / "plyrix", folder):
        assert stat.S_IMODE(made.stat().st_mode) == 0o700


def test_a_failed_write_leaves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "spool"
    folder.mkdir()
    real_open = Path.open

    def failing_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        handle = real_open(self, *args, **kwargs)

        class Failing:
            def __enter__(self) -> "Failing":
                return self

            def __exit__(self, *exc: object) -> None:
                handle.close()

            def write(self, _data: bytes) -> int:
                raise OSError(28, "No space left on device")

        return Failing()

    monkeypatch.setattr(Path, "open", failing_open)

    with pytest.raises(OSError):
        spool.put(folder, fields())

    assert list(folder.iterdir()) == []


def test_entries_lists_only_event_files_in_name_order(tmp_path: Path) -> None:
    folder = tmp_path / "spool"
    b = spool.put(folder, fields())
    a = folder / "00000000T000000Z-00000000.event"
    a.write_bytes(b"")
    (folder / ".x.event.tmp").write_bytes(b"")
    (folder / "x.txt").write_bytes(b"")
    (folder / "z.event").symlink_to(b)
    (folder / "failed").mkdir()
    (folder / "failed" / "y.event").write_bytes(b"")

    assert spool.entries(folder) == [a, b]


def test_entries_of_a_missing_folder_is_empty(tmp_path: Path) -> None:
    assert spool.entries(tmp_path / "nothing") == []


@pytest.mark.parametrize(
    "data",
    [
        b"Lidarr_EventType=AlbumDownload",
        b"Lidarr_EventType\0",
        b"=AlbumDownload\0",
        b"Lidarr_EventType=Test\0Lidarr_EventType=Test\0",
    ],
)
def test_read_refuses_a_bad_file(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "x.event"
    path.write_bytes(data)

    with pytest.raises(spool.SpoolError):
        spool.read(path)


def test_read_refuses_a_file_too_large(tmp_path: Path) -> None:
    path = tmp_path / "x.event"
    path.write_bytes(b"K=" + b"v" * spool.MAX_BYTES + b"\0")

    with pytest.raises(spool.SpoolError):
        spool.read(path)


def test_read_opens_read_only() -> None:
    writes = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    assert spool._READ_FLAGS & writes == 0
    assert spool._READ_FLAGS & os.O_NOFOLLOW


def test_read_refuses_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.write_bytes(b"Lidarr_EventType=AlbumDownload\0")
    path = tmp_path / "x.event"
    path.symlink_to(real)

    with pytest.raises(spool.SpoolError):
        spool.read(path)


def test_read_refuses_a_symlink_to_an_endless_device(tmp_path: Path) -> None:
    # Its st_size is 0, so a size check before the read would pass it.
    path = tmp_path / "x.event"
    path.symlink_to("/dev/zero")

    with pytest.raises(spool.SpoolError):
        spool.read(path)


def test_read_refuses_a_fifo_without_waiting(tmp_path: Path) -> None:
    path = tmp_path / "x.event"
    os.mkfifo(path)

    with pytest.raises(spool.SpoolError):
        spool.read(path)


def test_read_stops_at_the_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "x.event"
    path.write_bytes(b"K=" + b"v" * spool.MAX_BYTES + b"\0")
    asked: list[int] = []
    real_read = os.read

    def counting_read(descriptor: int, size: int) -> bytes:
        asked.append(size)
        return real_read(descriptor, size)

    monkeypatch.setattr(os, "read", counting_read)

    with pytest.raises(spool.SpoolError):
        spool.read(path)
    assert sum(asked) <= spool.MAX_BYTES + 1


def test_read_of_a_file_gone_says_so(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        spool.read(tmp_path / "gone.event")


@pytest.mark.parametrize(
    "bad",
    [{"A=B": "x"}, {"A\0": "x"}, {"A": "x\0y"}, {"": "x"}],
)
def test_put_refuses_what_the_format_cannot_hold(tmp_path: Path, bad: dict[str, str]) -> None:
    with pytest.raises(spool.SpoolError):
        spool.put(tmp_path / "spool", bad)

    assert spool.entries(tmp_path / "spool") == []


def test_quarantine_moves_into_failed(tmp_path: Path) -> None:
    folder = tmp_path / "spool"
    path = spool.put(folder, fields())
    (folder / "failed").mkdir(mode=0o700)
    (folder / "failed" / path.name).write_bytes(b"older")

    spool.quarantine(path)

    assert not path.exists()
    assert spool.read(folder / "failed" / path.name) == fields()
    assert spool.entries(folder) == []


def test_quarantine_makes_a_private_failed_folder(tmp_path: Path) -> None:
    path = spool.put(tmp_path / "spool", fields())

    spool.quarantine(path)

    assert stat.S_IMODE((tmp_path / "spool" / "failed").stat().st_mode) == 0o700


def test_quarantine_refuses_a_symlinked_failed_folder(tmp_path: Path) -> None:
    folder = tmp_path / "spool"
    path = spool.put(folder, fields())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (folder / "failed").symlink_to(elsewhere)

    with pytest.raises(NotADirectoryError):
        spool.quarantine(path)

    assert path.exists()
    assert list(elsewhere.iterdir()) == []


def test_quarantine_of_a_file_another_drain_took(tmp_path: Path) -> None:
    path = spool.put(tmp_path / "spool", fields())
    path.unlink()

    assert spool.quarantine(path) is False
    assert spool.quarantine(spool.put(tmp_path / "spool", fields())) is True


def test_remove_older_than(tmp_path: Path) -> None:
    folder = tmp_path / "spool"
    old = spool.put(folder, fields())
    new = spool.put(folder, fields())
    cutoff = spool.now()
    os.utime(old, (cutoff - 3600, cutoff - 3600))
    os.utime(new, (cutoff + 3600, cutoff + 3600))

    assert spool.remove_older_than(folder, cutoff) == 1
    assert spool.entries(folder) == [new]
    assert spool.remove_older_than(tmp_path / "missing", cutoff) == 0


def load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **environ: str) -> Settings:
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("PLYRIX_MUSIC_ROOT", str(lib))
    monkeypatch.setenv("PLYRIX_LRCLIB_URL", "http://127.0.0.1:1/api")
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    settings, _ = Settings.load(None, {})
    return settings


def test_the_spool_defaults_to_beside_the_state_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load(tmp_path, monkeypatch)

    assert settings.lidarr_spool == settings.state_db.parent / "lidarr-spool"
    assert settings.lidarr_spool is not None and settings.lidarr_spool.is_absolute()


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("{lib}/spool", "must be outside the library"),
        ("relative/spool", "must be an absolute path"),
        ("{tmp}/state/s.db", "can't be the state database"),
        ("{tmp}/state", "or a folder holding it"),
        ("{tmp}/state/", "or a folder holding it"),
        ("{tmp}", "or a folder holding it"),
        ("{tmp}/link", "or a folder holding it"),
    ],
)
def test_a_bad_spool_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, why: str
) -> None:
    spool_value = value.format(lib=tmp_path / "lib", tmp=tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "state")

    with pytest.raises(ConfigError) as caught:
        load(
            tmp_path,
            monkeypatch,
            PLYRIX_LIDARR_SPOOL=spool_value,
            PLYRIX_STATE_DB=str(tmp_path / "state" / "s.db"),
        )

    assert "lidarr_spool (PLYRIX_LIDARR_SPOOL)" in caught.value.message
    assert why in caught.value.message
    assert str(tmp_path) not in caught.value.message


def test_a_spool_inside_the_state_folder_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load(
        tmp_path,
        monkeypatch,
        PLYRIX_LIDARR_SPOOL=str(tmp_path / "state" / "spool"),
        PLYRIX_STATE_DB=str(tmp_path / "state" / "s.db"),
    )

    assert settings.lidarr_spool == tmp_path / "state" / "spool"


def test_the_budget_defaults_to_a_minute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert load(tmp_path, monkeypatch).lidarr_budget == 60


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_a_bad_budget_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    with pytest.raises(ConfigError) as caught:
        load(tmp_path, monkeypatch, PLYRIX_LIDARR_BUDGET=value)

    assert "lidarr_budget (PLYRIX_LIDARR_BUDGET)" in caught.value.message


def test_a_toml_switch_is_not_a_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    toml = tmp_path / "config" / "plyrix" / "config.toml"
    toml.parent.mkdir(parents=True)
    toml.write_text("lidarr_budget = true\n")

    with pytest.raises(ConfigError) as caught:
        load(tmp_path, monkeypatch)

    assert "lidarr_budget (PLYRIX_LIDARR_BUDGET)" in caught.value.message
