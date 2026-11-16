"""plyrix doctor: mount lookup, disk, library, LRCLIB and Plex checks. Every mount is invented."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import RunCli, StubLrclib, StubPlex, cli_env, plex_section_dir, snap
from plyrix import doctor, lrclib, plex
from plyrix.doctor import DOWNLOAD_BYTES, FIRST_LOAD_BYTES, Level

GB = 10**9
# Invented mounts only, under /srv. Field 5 is the mount point, and the fstype follows " - ".
MOUNTINFO = (
    "22 1 0:21 / / rw,relatime shared:1 - btrfs /dev/fixture1 rw\n"
    "30 22 0:30 / /srv rw,relatime shared:2 - ext4 /dev/fixture2 rw\n"
    "31 30 0:31 / /srv/data rw,relatime shared:3 - zfs fixture/data rw,xattr\n"
    "32 30 0:32 / /srv/my\\040music rw,relatime - xfs /dev/fixture3 rw\n"
    "33 30 0:33 / /srv/tab\\011and\\012nl\\134bs rw - tmpfs tmpfs rw\n"
    "this line is not a mount at all\n"
    "34 30 0:34 / /srv/short\n"
    "35 30 0:35 / /srv/nofs rw - \n"
)


def test_mount_of_picks_the_longest_prefix() -> None:
    assert doctor.mount_of(b"/srv/data/x", MOUNTINFO) == (b"/srv/data", "zfs")
    assert doctor.mount_of(b"/srv/data", MOUNTINFO) == (b"/srv/data", "zfs")
    assert doctor.mount_of(b"/srv/other/x", MOUNTINFO) == (b"/srv", "ext4")
    assert doctor.mount_of(b"/opt/x", MOUNTINFO) == (b"/", "btrfs")


def test_mount_of_is_a_path_prefix_not_a_string_prefix() -> None:
    assert doctor.mount_of(b"/srvx/y", MOUNTINFO) == (b"/", "btrfs")
    assert doctor.mount_of(b"/srv/datax/y", MOUNTINFO) == (b"/srv", "ext4")


def test_mount_of_decodes_octal_escapes() -> None:
    assert doctor.mount_of(b"/srv/my music/a", MOUNTINFO) == (b"/srv/my music", "xfs")
    assert doctor.mount_of(b"/srv/tab\tand\nnl\\bs/x", MOUNTINFO) == (
        b"/srv/tab\tand\nnl\\bs",
        "tmpfs",
    )
    # The escaped form itself is not the mount point.
    assert doctor.mount_of(b"/srv/my\\040music/a", MOUNTINFO) == (b"/srv", "ext4")


def test_mount_of_skips_malformed_lines() -> None:
    assert doctor.mount_of(b"/srv/short/x", MOUNTINFO) == (b"/srv", "ext4")
    assert doctor.mount_of(b"/srv/nofs/x", MOUNTINFO) == (b"/srv", "ext4")
    assert doctor.mount_of(b"/srv/x", "garbage\n\n - \n1 2 3 - x\n") is None
    assert doctor.mount_of(b"/srv/x", "") is None


def test_mount_of_looks_up_a_missing_path_at_its_nearest_existing_ancestor(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    text = f"22 1 0:21 / / rw - btrfs /dev/fixture rw\n40 22 0:40 / {real} rw - zfs fixture rw\n"
    missing = os.fsencode(tmp_path / "link" / "not" / "yet" / "state.db")
    assert doctor.mount_of(missing, text) == (os.fsencode(real), "zfs")


def test_read_mountinfo_takes_its_path(tmp_path: Path) -> None:
    fixture = tmp_path / "mountinfo"
    fixture.write_bytes(b"22 1 0:21 / /srv/caf\xe9 rw - ext4 /dev/fixture rw\n")
    text = doctor.read_mountinfo(fixture)
    assert doctor.mount_of(b"/srv/caf\xe9/x", text) == (b"/srv/caf\xe9", "ext4")
    assert doctor.read_mountinfo(tmp_path / "absent") == ""


@pytest.mark.parametrize("fstype", ["btrfs", "ext4", "xfs", "zfs"])
def test_a_local_filesystem_passes(fstype: str) -> None:
    text = f"22 1 0:21 / /srv rw - {fstype} /dev/fixture rw\n"
    check = doctor.filesystem_check("state_db", b"/srv/state.db", text)
    assert (check.name, check.level, check.exit_code) == ("state_db", Level.OK, 0)
    assert fstype in check.message


def test_tmpfs_passes_with_a_reboot_warning() -> None:
    text = "22 1 0:21 / /srv rw - tmpfs tmpfs rw\n"
    check = doctor.filesystem_check("state_db", b"/srv/state.db", text)
    assert (check.level, check.exit_code) == (Level.WARN, 0)
    assert "reboot" in check.message


def test_overlay_warns_that_a_container_layer_is_lost() -> None:
    # A state database outside every mount sits in the container's own writable layer.
    text = "22 1 0:21 / / rw - overlay overlay rw\n"
    check = doctor.filesystem_check("state_db", b"/home/plyrix/state.db", text)
    assert (check.level, check.exit_code) == (Level.WARN, 0)
    assert "overlay" in check.message
    assert "volume" in check.message


@pytest.mark.parametrize(
    "fstype",
    [
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "smbfs",
        "ceph",
        "glusterfs",
        "9p",
        "afs",
        "lustre",
        "fuse.sshfs",
        "fuse.rclone",
    ],
)
def test_a_network_filesystem_warns(fstype: str) -> None:
    text = f"22 1 0:21 / /srv rw - {fstype} fixture:/share rw\n"
    check = doctor.filesystem_check("state_db", b"/srv/state.db", text)
    assert (check.level, check.exit_code) == (Level.WARN, 0)
    assert fstype in check.message


def test_any_other_fuse_filesystem_warns_that_locking_cant_be_confirmed() -> None:
    text = "22 1 0:21 / /srv rw - fuse.mergerfs pool rw\n"
    check = doctor.filesystem_check("state_db", b"/srv/state.db", text)
    assert (check.level, check.exit_code) == (Level.WARN, 0)
    assert "locking" in check.message


def test_an_unidentified_filesystem_warns() -> None:
    check = doctor.filesystem_check("state_db", b"/srv/state.db", "")
    assert (check.level, check.exit_code) == (Level.WARN, 0)
    assert "couldn't be identified" in check.message


def test_filesystem_messages_name_the_setting_not_the_path() -> None:
    text = "22 1 0:21 / /srv rw - nfs4 fixture:/share rw\n"
    check = doctor.filesystem_check("state_db", b"/srv/fixture-state.db", text)
    assert "PLYRIX_STATE_DB" in check.message
    assert "/srv" not in check.message


def _statvfs(free: int) -> object:
    return SimpleNamespace(f_bavail=free, f_frsize=1)


def _free(monkeypatch: pytest.MonkeyPatch, free: int) -> None:
    def fake(path: object) -> object:
        return _statvfs(free)

    monkeypatch.setattr(os, "statvfs", fake)


DUMP = "lrclib-db-dump-20261030T182259Z.sqlite3.gz"


def test_first_load_bytes_is_the_measured_peak_rounded_up() -> None:
    # 319.8 GB apparent at the peak of one measured first load, rounded up.
    assert FIRST_LOAD_BYTES == 320 * GB


def test_enough_space_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _free(monkeypatch, FIRST_LOAD_BYTES)
    check = doctor.space_check(tmp_path)
    assert (check.level, check.exit_code) == (Level.OK, 0)
    assert "about 320 GB" in check.message
    assert "measured" in check.message
    assert "arithmetic" not in check.message
    assert "129.6 GB" in check.message


def test_space_one_byte_short_of_the_first_load_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda path: SimpleNamespace(f_bavail=FIRST_LOAD_BYTES - 1, f_frsize=1),
    )
    check = doctor.space_check(tmp_path)
    assert (check.level, check.exit_code) == (Level.FAIL, 1)
    assert "about 320 GB" in check.message


@pytest.mark.parametrize(
    "name",
    [
        "lrclib-db-dump-20261030T182259Z.sqlite3.gz",
        "lrclib-db-dump-20261030T182259Z.sqlite3.gz.part",
    ],
)
def test_a_dump_already_downloaded_counts_against_the_need(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    (tmp_path / "dl").mkdir()
    with (tmp_path / "dl" / name).open("wb") as dump:
        dump.truncate(49 * GB)
    _free(monkeypatch, FIRST_LOAD_BYTES - DOWNLOAD_BYTES)
    check = doctor.space_check(tmp_path, DUMP)
    assert (check.level, check.exit_code) == (Level.OK, 0)
    assert "already downloaded" in check.message


@pytest.mark.parametrize("dump", [None, "lrclib-db-dump-20261201T000000Z.sqlite3.gz"])
def test_only_the_dump_about_to_be_loaded_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dump: str | None
) -> None:
    # An older dump kept in dl doesn't make room for the new one.
    (tmp_path / "dl").mkdir()
    with (tmp_path / "dl" / DUMP).open("wb") as old:
        old.truncate(49 * GB)
    _free(monkeypatch, FIRST_LOAD_BYTES - DOWNLOAD_BYTES)
    check = doctor.space_check(tmp_path, dump)
    assert (check.level, check.exit_code) == (Level.FAIL, 1)
    assert "already downloaded" not in check.message


def test_a_download_counts_for_no_more_than_the_dump_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "dl").mkdir()
    with (tmp_path / "dl" / f"{DUMP}.part").open("wb") as part:
        part.truncate(60 * GB)
    _free(monkeypatch, FIRST_LOAD_BYTES - DOWNLOAD_BYTES - 1)
    assert doctor.space_check(tmp_path, DUMP).level is Level.FAIL


def test_a_file_that_isnt_a_dump_doesnt_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "dl").mkdir()
    with (tmp_path / "dl" / "other.gz").open("wb") as other:
        other.truncate(49 * GB)
    _free(monkeypatch, FIRST_LOAD_BYTES - DOWNLOAD_BYTES)
    assert doctor.space_check(tmp_path).level is Level.FAIL


def test_music_root_missing_fails_with_exit_2(tmp_path: Path) -> None:
    check = doctor.music_root_check(tmp_path / "absent")
    assert (check.name, check.level, check.exit_code) == ("music_root", Level.FAIL, 2)
    assert "PLYRIX_MUSIC_ROOT" in check.message


def test_music_root_not_writable_fails_with_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "access", lambda path, mode: False)
    check = doctor.music_root_check(tmp_path)
    assert (check.level, check.exit_code) == (Level.FAIL, 2)
    assert "written" in check.message


def test_music_root_writable_passes(tmp_path: Path) -> None:
    check = doctor.music_root_check(tmp_path)
    assert (check.level, check.exit_code) == (Level.OK, 0)


@pytest.mark.parametrize(
    ("result", "level", "code", "words"),
    [
        (lrclib.Preflight.READY, Level.OK, 0, "ready"),
        (lrclib.Preflight.INDEX_NOT_READY, Level.FAIL, 1, "still building its search index"),
        (lrclib.Preflight.NO_TRACKS, Level.FAIL, 1, "empty database"),
        (lrclib.Preflight.UNAVAILABLE, Level.FAIL, 1, "down or not reachable"),
    ],
)
def test_lrclib_results(result: lrclib.Preflight, level: Level, code: int, words: str) -> None:
    check = doctor.lrclib_check(result)
    assert (check.name, check.level, check.exit_code) == ("lrclib", level, code)
    assert words in check.message
    assert "PLYRIX_LRCLIB_URL" in check.message


def _music(*locations: bytes, kind: str = "artist") -> plex.SectionInfo:
    return plex.SectionInfo("1", kind, "tv.plex.agents.music", tuple(locations))


def test_plex_not_configured_is_one_ok_line() -> None:
    checks = doctor.plex_checks(None, None, Path("/srv/music"))
    assert [(c.name, c.level, c.exit_code) for c in checks] == [("plex", Level.OK, 0)]
    assert "isn't configured" in checks[0].message


@pytest.mark.parametrize("location", [b"/srv/music", b"/srv/music/", b"/srv", b"/srv/music/Rock"])
def test_a_music_section_whose_folder_matches_passes(location: bytes) -> None:
    checks = doctor.plex_checks(plex.Reply.OK, _music(b"/other", location), Path("/srv/music"))
    assert [(c.level, c.exit_code) for c in checks] == [(Level.OK, 0), (Level.OK, 0)]
    assert "server answered; token not checked" in checks[0].message


@pytest.mark.parametrize("location", [b"/srv/musicx", b"/srv/other", b"/data/music"])
def test_a_folder_that_doesnt_match_warns_of_not_in_plex(location: bytes) -> None:
    checks = doctor.plex_checks(plex.Reply.OK, _music(location), Path("/srv/music"))
    assert checks[0].level is Level.OK
    assert (checks[1].name, checks[1].level, checks[1].exit_code) == (
        "plex_music_root",
        Level.WARN,
        0,
    )
    assert "not_in_plex" in checks[1].message


def test_a_section_with_no_folders_warns() -> None:
    checks = doctor.plex_checks(plex.Reply.OK, _music(), Path("/srv/music"))
    assert checks[1].level is Level.WARN


@pytest.mark.parametrize("kind", ["movie", "show", "photo"])
def test_a_section_that_isnt_music_fails_with_exit_2(kind: str) -> None:
    checks = doctor.plex_checks(plex.Reply.OK, _music(b"/srv/music", kind=kind), Path("/srv/music"))
    assert [(c.level, c.exit_code) for c in checks] == [(Level.FAIL, 2)]
    assert "PLYRIX_PLEX_SECTION" in checks[0].message


def test_a_missing_section_fails_with_exit_2() -> None:
    checks = doctor.plex_checks(plex.Reply.MISSING, None, Path("/srv/music"))
    assert [(c.level, c.exit_code) for c in checks] == [(Level.FAIL, 2)]
    assert "PLYRIX_PLEX_SECTION" in checks[0].message


@pytest.mark.parametrize("reply", [plex.Reply.UNAVAILABLE, plex.Reply.REFUSED])
def test_an_unreachable_or_refusing_plex_warns(reply: plex.Reply) -> None:
    checks = doctor.plex_checks(reply, None, Path("/srv/music"))
    assert [(c.level, c.exit_code) for c in checks] == [(Level.WARN, 0)]


def test_a_refused_plex_names_the_token_setting() -> None:
    (check,) = doctor.plex_checks(plex.Reply.REFUSED, None, Path("/srv/music"))
    assert "PLYRIX_PLEX_TOKEN" in check.message


@pytest.mark.parametrize(
    ("codes", "worst"),
    [((), 0), ((0, 0), 0), ((0, 1), 1), ((1, 2, 0), 2), ((2, 1), 2)],
)
def test_the_worst_code_wins(codes: tuple[int, ...], worst: int) -> None:
    checks = [doctor.Check("x", Level.FAIL if code else Level.OK, "m", code) for code in codes]
    assert doctor.worst(checks) == worst


def test_data_dir_that_doesnt_exist_fails_with_exit_2(tmp_path: Path) -> None:
    checks = doctor.lrclib_data_checks(tmp_path / "absent", "")
    assert [(c.level, c.exit_code) for c in checks] == [(Level.FAIL, 2)]


def test_data_dir_checks_writability_filesystem_and_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _free(monkeypatch, FIRST_LOAD_BYTES)
    text = f"22 1 0:21 / / rw - btrfs /dev/fixture rw\n40 22 0:40 / {tmp_path} rw - zfs f rw\n"
    checks = doctor.lrclib_data_checks(tmp_path, text)
    assert [c.level for c in checks] == [Level.OK, Level.OK, Level.OK]
    assert "zfs" in checks[1].message
    assert "about 320 GB" in checks[2].message


def test_data_dir_that_cant_be_written_from_here_only_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _free(monkeypatch, FIRST_LOAD_BYTES)
    monkeypatch.setattr(os, "access", lambda path, mode: False)
    checks = doctor.lrclib_data_checks(tmp_path, "")
    assert checks[0].level is Level.WARN
    assert "can't be written from here" in checks[0].message
    assert doctor.worst(checks) == 0


# plyrix doctor through the real console script, against stub servers. All of it invented.

DUMMY_TOKEN = "plyrix-dummy-token-doctor-0123456789"
DEAD_LRCLIB = "http://127.0.0.1:9/api"
DEAD_PLEX = "http://127.0.0.1:9"
README = Path(__file__).parent.parent / "README.md"
_LINE = re.compile(r"^(ok|warning|problem) ([a-z_]+): (.+)$")


def lines(stdout: bytes) -> list[tuple[str, str, str]]:
    """Return each doctor line as its level, check name and message. Every line must parse."""
    parsed = []
    for line in stdout.decode().splitlines():
        match = _LINE.fullmatch(line)
        assert match, line
        parsed.append((match.group(1), match.group(2), match.group(3)))
    return parsed


def levels(stdout: bytes) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for level, name, _ in lines(stdout):
        found.setdefault(name, []).append(level)
    return found


def healthy_env(tmp_path: Path, stub: StubLrclib, **extra: str) -> dict[str, str]:
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    return {"PLYRIX_MUSIC_ROOT": str(lib), "PLYRIX_LRCLIB_URL": stub.base_url, **extra}


def plex_env(tmp_path: Path, stub: StubLrclib, plex_stub: StubPlex) -> dict[str, str]:
    env = healthy_env(tmp_path, stub)
    plex_stub.location = env["PLYRIX_MUSIC_ROOT"]
    return {
        **env,
        "PLYRIX_PLEX_URL": plex_stub.base_url,
        "PLYRIX_PLEX_SECTION": "1",
        "PLYRIX_PLEX_TOKEN": DUMMY_TOKEN,
    }


def host_state_level(tmp_path: Path) -> str:
    """Return the level doctor should give a state database under tmp_path on this host."""
    state = os.fsencode(tmp_path / "state" / "plyrix" / "state.db")
    return doctor.filesystem_check("state_db", state, doctor.read_mountinfo()).level.value


def test_doctor_passes_a_healthy_setup(
    run_cli: RunCli, tmp_path: Path, stub_lrclib: StubLrclib
) -> None:
    proc = run_cli(["doctor"], env=healthy_env(tmp_path, stub_lrclib))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [name for _, name, _ in lines(proc.stdout)] == [
        "config",
        "music_root",
        "state_db",
        "lrclib",
        "plex",
    ]
    found = levels(proc.stdout)
    assert found["config"] == found["music_root"] == found["lrclib"] == found["plex"] == ["ok"]
    assert found["state_db"] == [host_state_level(tmp_path)]
    assert b"pre-release" not in proc.stderr
    assert stub_lrclib.probes, "doctor never asked LRCLIB"


def test_lrclib_down_exits_1(run_cli: RunCli, tmp_path: Path, stub_lrclib: StubLrclib) -> None:
    env = healthy_env(tmp_path, stub_lrclib, PLYRIX_LRCLIB_URL=DEAD_LRCLIB)
    proc = run_cli(["doctor"], env=env)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert levels(proc.stdout)["lrclib"] == ["problem"]
    assert b"down or not reachable" in proc.stdout


def test_lrclib_still_indexing_exits_1(
    run_cli: RunCli, tmp_path: Path, stub_lrclib: StubLrclib
) -> None:
    stub_lrclib.preflight_search = (500, {})
    proc = run_cli(["doctor"], env=healthy_env(tmp_path, stub_lrclib))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert b"search index" in proc.stdout


def test_no_lrclib_url_exits_2_with_the_config_message(run_cli: RunCli, tmp_path: Path) -> None:
    (tmp_path / "lib").mkdir()
    proc = run_cli(["doctor"], env={"PLYRIX_MUSIC_ROOT": str(tmp_path / "lib")})
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert lines(proc.stdout) == [
        ("problem", "config", "missing required settings: lrclib_url (PLYRIX_LRCLIB_URL)")
    ]


def test_a_missing_music_root_exits_2(
    run_cli: RunCli, tmp_path: Path, stub_lrclib: StubLrclib
) -> None:
    env = healthy_env(tmp_path, stub_lrclib, PLYRIX_MUSIC_ROOT=str(tmp_path / "absent"))
    proc = run_cli(["doctor"], env=env)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert levels(proc.stdout)["music_root"] == ["problem"]
    assert levels(proc.stdout)["lrclib"] == ["ok"]


def test_a_bad_section_beats_lrclib_down(
    run_cli: RunCli, tmp_path: Path, stub_lrclib: StubLrclib, stub_plex: StubPlex
) -> None:
    env = {**plex_env(tmp_path, stub_lrclib, stub_plex), "PLYRIX_LRCLIB_URL": DEAD_LRCLIB}
    stub_plex.sections = [plex_section_dir("1", "movie", env["PLYRIX_MUSIC_ROOT"])]
    proc = run_cli(["doctor"], env=env)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert levels(proc.stdout)["lrclib"] == ["problem"]
    assert levels(proc.stdout)["plex"] == ["problem"]


def test_a_music_section_passes_with_its_token_not_checked(
    run_cli: RunCli, tmp_path: Path, stub_lrclib: StubLrclib, stub_plex: StubPlex
) -> None:
    proc = run_cli(["doctor"], env=plex_env(tmp_path, stub_lrclib, stub_plex))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert b"token not checked" in proc.stdout
    assert levels(proc.stdout)["plex_music_root"] == ["ok"]
    assert [r["method"] for r in stub_plex.requests] == ["GET"]


def test_a_movie_section_exits_2(
    run_cli: RunCli, tmp_path: Path, stub_lrclib: StubLrclib, stub_plex: StubPlex
) -> None:
    env = plex_env(tmp_path, stub_lrclib, stub_plex)
    stub_plex.sections = [plex_section_dir("1", "movie", env["PLYRIX_MUSIC_ROOT"])]
    proc = run_cli(["doctor"], env=env)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert b"isn't a music library" in proc.stdout


def test_a_section_folder_that_doesnt_match_warns(
    run_cli: RunCli, tmp_path: Path, stub_lrclib: StubLrclib, stub_plex: StubPlex
) -> None:
    env = plex_env(tmp_path, stub_lrclib, stub_plex)
    stub_plex.location = "/srv/elsewhere"
    proc = run_cli(["doctor"], env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert levels(proc.stdout)["plex_music_root"] == ["warning"]
    assert b"not_in_plex" in proc.stdout


def test_a_dead_plex_warns_and_still_exits_0(
    run_cli: RunCli, tmp_path: Path, stub_lrclib: StubLrclib, stub_plex: StubPlex
) -> None:
    env = {**plex_env(tmp_path, stub_lrclib, stub_plex), "PLYRIX_PLEX_URL": DEAD_PLEX}
    proc = run_cli(["doctor"], env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert levels(proc.stdout)["plex"] == ["warning"]


def test_doctor_never_prints_the_token_or_the_plex_address(
    run_cli: RunCli, tmp_path: Path, stub_lrclib: StubLrclib, stub_plex: StubPlex
) -> None:
    port = stub_plex.base_url.rsplit(":", 1)[1].encode()
    for kind in ("artist", "movie"):
        env = plex_env(tmp_path, stub_lrclib, stub_plex)
        stub_plex.sections = [plex_section_dir("1", kind, env["PLYRIX_MUSIC_ROOT"])]
        proc = run_cli(["doctor"], env=env)
        for output in (proc.stdout, proc.stderr):
            assert DUMMY_TOKEN.encode() not in output
            assert port not in output
            assert b"127.0.0.1" not in output


def test_lrclib_data_needs_no_settings(run_cli: RunCli, tmp_path: Path) -> None:
    free = os.statvfs(tmp_path)
    if free.f_bavail * free.f_frsize < FIRST_LOAD_BYTES:
        pytest.skip("tmp_path has less free space than a first load; the patched child covers it")
    proc = run_cli(["doctor", "--lrclib-data", tmp_path])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert set(levels(proc.stdout)) == {"lrclib_data"}
    assert b"about 320 GB" in proc.stdout
    assert b"measured" in proc.stdout


PATCHED_SPACE = """
import os, sys
from types import SimpleNamespace

from plyrix import cli

free, *argv = sys.argv[1:]
os.statvfs = lambda path: SimpleNamespace(f_bavail=int(free), f_frsize=1)
cli.app(argv)
"""


def patched_doctor(
    tmp_path: Path, free: int, *args: str | Path
) -> "subprocess.CompletedProcess[bytes]":
    """Run doctor in a child whose os.statvfs reports this much free space."""
    return subprocess.run(
        [sys.executable, "-c", PATCHED_SPACE, str(free), "doctor", *map(str, args)],
        env=cli_env(tmp_path, {}),
        capture_output=True,
        check=False,
        timeout=120,
    )


def test_lrclib_data_short_of_space_exits_1(tmp_path: Path) -> None:
    proc = patched_doctor(tmp_path, FIRST_LOAD_BYTES - 1, "--lrclib-data", tmp_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert levels(proc.stdout)["lrclib_data"][-1] == "problem"
    assert b"about 320 GB" in proc.stdout


def test_lrclib_data_with_room_exits_0(tmp_path: Path) -> None:
    proc = patched_doctor(tmp_path, FIRST_LOAD_BYTES, "--lrclib-data", tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_lrclib_data_counts_the_named_dump(tmp_path: Path) -> None:
    (tmp_path / "dl").mkdir()
    with (tmp_path / "dl" / DUMP).open("wb") as dump:
        dump.truncate(49 * GB)
    free = FIRST_LOAD_BYTES - DOWNLOAD_BYTES
    proc = patched_doctor(tmp_path, free, "--lrclib-data", tmp_path, "--dump", DUMP)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert b"already downloaded" in proc.stdout


@pytest.mark.parametrize("dump", ["../etc/passwd", f"x/{DUMP}", "lrclib-db-dump-2026.sqlite3.gz"])
def test_a_bad_dump_name_exits_2(run_cli: RunCli, tmp_path: Path, dump: str) -> None:
    proc = run_cli(["doctor", "--lrclib-data", tmp_path, "--dump", dump])
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert b"--dump" in proc.stdout + proc.stderr


def test_dump_without_lrclib_data_exits_2(run_cli: RunCli) -> None:
    proc = run_cli(["doctor", "--dump", DUMP])
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert b"--lrclib-data" in proc.stdout + proc.stderr


def test_lrclib_data_that_doesnt_exist_exits_2(run_cli: RunCli, tmp_path: Path) -> None:
    proc = run_cli(["doctor", "--lrclib-data", tmp_path / "absent"])
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert levels(proc.stdout) == {"lrclib_data": ["problem"]}


def test_lrclib_data_empty_exits_2(run_cli: RunCli) -> None:
    proc = run_cli(["doctor", "--lrclib-data", ""])
    assert proc.returncode == 2, proc.stdout + proc.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a read-only folder")
def test_lrclib_data_read_only_only_warns(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o555)
    try:
        proc = patched_doctor(tmp_path, FIRST_LOAD_BYTES, "--lrclib-data", data)
    finally:
        data.chmod(0o755)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert b"warning lrclib_data: " in proc.stdout
    assert b"can't be written from here" in proc.stdout


AUDITED_DOCTOR = """
import json, os, sys

from plyrix import cli

events = []


def record(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        events.append([event, os.fsdecode(args[0]), args[2]])
    elif event.startswith(("os.", "shutil.")):
        paths = [os.fsdecode(a) for a in args if isinstance(a, (str, bytes))]
        events.append([event, paths, 0])


log, *argv = sys.argv[1:]
sys.addaudithook(record)
try:
    cli.app(argv)
finally:
    with open(log, "w", encoding="utf-8") as file:
        json.dump(events, file)
"""
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_CHANGES = {"os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.chmod", "os.utime"}


def test_no_write(tmp_path: Path, stub_lrclib: StubLrclib, stub_plex: StubPlex) -> None:
    env = plex_env(tmp_path, stub_lrclib, stub_plex)
    lib = Path(env["PLYRIX_MUSIC_ROOT"])
    (lib / "Album").mkdir()
    (lib / "Album" / "t.flac").write_bytes(b"fLaC invented")
    (lib / "Album" / "t.lrc").write_bytes(b"[00:00.50] invented\n")
    state = tmp_path / "state"
    state.mkdir()
    config_file = state / "config.toml"
    config_file.write_text('plain = "write"\n', encoding="utf-8")
    before = {path: snap(path) for path in [lib, *lib.rglob("*")]}
    log = tmp_path / "events.json"
    proc = subprocess.run(
        [sys.executable, "-c", AUDITED_DOCTOR, log, "doctor", "--config", config_file],
        env=cli_env(tmp_path, env),
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    events = json.loads(log.read_text(encoding="utf-8"))
    watched = (str(tmp_path) + "/", str(lib))

    def watched_path(path: str) -> bool:
        return path != str(log) and (path.startswith(watched[0]) or path == watched[1])

    writes = [e for e in events if e[0] == "open" and e[2] & _WRITE_FLAGS and watched_path(e[1])]
    changes = [e for e in events if e[0] in _CHANGES and any(map(watched_path, e[1]))]
    assert writes == []
    assert changes == []
    # The hook can see opens: the config file in the state folder was read.
    reads = [e for e in events if e[0] == "open" and not e[2] & _WRITE_FLAGS]
    assert any(e[1] == str(config_file) for e in reads), reads
    assert {path: snap(path) for path in [lib, *lib.rglob("*")]} == before
    assert not (state / "plyrix").exists()


def test_readme_doctor_section_names_exactly_its_exit_codes() -> None:
    readme = README.read_text(encoding="utf-8")
    section = readme.split("### Doctor\n", 1)[1].split("\n## ", 1)[0]
    assert "plyrix doctor" in section
    assert "token not checked" in section
    assert "--lrclib-data" in section
    assert "about 320 GB" in section
    assert "measured 2026-11-11" in section
    codes = section.split("### Doctor exit codes", 1)[1].split("\n#", 1)[0]
    assert set(re.findall(r"^\| (\d+) \|", codes, re.MULTILINE)) == {"0", "1", "2"}


def test_every_320_gb_in_the_readme_is_labelled_measured() -> None:
    paragraphs = [p for p in README.read_text(encoding="utf-8").split("\n\n") if "320 GB" in p]
    assert paragraphs
    for paragraph in paragraphs:
        assert "measured" in paragraph, paragraph
        assert "arithmetic" not in paragraph, paragraph


# The exact room an update needs (Phase 6, D-05)

SIZE = 49 * GB
UNPACKED = 269 * GB


def test_an_update_needs_exactly_the_download_the_database_and_the_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    need = SIZE + UNPACKED + doctor.INDEX_ALLOWANCE_BYTES
    _free(monkeypatch, need)
    fits = doctor.space_check(tmp_path, DUMP, SIZE, UNPACKED)
    _free(monkeypatch, need - 1)
    short = doctor.space_check(tmp_path, DUMP, SIZE, UNPACKED)

    assert (fits.level, fits.exit_code) == (Level.OK, 0)
    assert (short.level, short.exit_code) == (Level.FAIL, 1)
    for message in (fits.message, short.message):
        assert "free; this update needs 320.0 GB" in message
        assert "download left" in message and "decompressed database" in message
        assert "2 GB for the search index" in message and "1.4 GB" in message
        assert str(tmp_path) not in message


def test_a_partial_download_lowers_the_need_by_its_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "dl").mkdir()
    with (tmp_path / "dl" / f"{DUMP}.part").open("wb") as part:
        part.truncate(10 * GB)
    need = SIZE - 10 * GB + UNPACKED + doctor.INDEX_ALLOWANCE_BYTES
    _free(monkeypatch, need)
    assert doctor.space_check(tmp_path, DUMP, SIZE, UNPACKED).level is Level.OK
    _free(monkeypatch, need - 1)
    assert doctor.space_check(tmp_path, DUMP, SIZE, UNPACKED).level is Level.FAIL


def test_a_download_counts_for_no_more_than_the_listed_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Capped at the listed size, not the first load's 49 GB.
    (tmp_path / "dl").mkdir()
    with (tmp_path / "dl" / f"{DUMP}.part").open("wb") as part:
        part.truncate(5 * GB)
    _free(monkeypatch, UNPACKED + doctor.INDEX_ALLOWANCE_BYTES)
    assert doctor.space_check(tmp_path, DUMP, 3 * GB, UNPACKED).level is Level.OK
    _free(monkeypatch, UNPACKED + doctor.INDEX_ALLOWANCE_BYTES - 1)
    assert doctor.space_check(tmp_path, DUMP, 3 * GB, UNPACKED).level is Level.FAIL


def test_the_index_allowance_is_2_gb() -> None:
    assert doctor.INDEX_ALLOWANCE_BYTES == 2 * GB


def test_doctor_takes_the_update_sizes(run_cli: RunCli, tmp_path: Path) -> None:
    proc = run_cli(
        [
            "doctor",
            "--lrclib-data",
            str(tmp_path),
            "--dump",
            DUMP,
            "--size",
            "100",
            "--unpacked",
            "4096",
        ]
    )

    assert proc.returncode == 0, proc.stdout
    assert b"this update needs" in proc.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["--dump", DUMP, "--size", "100"],
        ["--dump", DUMP, "--unpacked", "100"],
        ["--size", "100", "--unpacked", "100"],
        ["--dump", DUMP, "--size", "0", "--unpacked", "100"],
        ["--dump", DUMP, "--size", "100", "--unpacked", "-1"],
    ],
)
def test_doctor_refuses_a_partial_set_of_update_sizes(
    run_cli: RunCli, tmp_path: Path, args: list[str]
) -> None:
    proc = run_cli(["doctor", "--lrclib-data", str(tmp_path), *args])

    assert proc.returncode == 2
    lines = (proc.stdout + proc.stderr).decode().strip().splitlines()
    assert len(lines) >= 1
