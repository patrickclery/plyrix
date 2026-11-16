"""The scripts Lidarr's Custom Script connection calls, run under sh with fake binaries."""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from conftest import lidarr_env
from plyrix import lidarr, spool

SHIMS = Path(__file__).resolve().parent.parent / "examples" / "lidarr"
SPOOL_SHIM = SHIMS / "lidarr-spool.sh"
HOOK_SHIM = SHIMS / "plyrix-hook.sh"
COMPOSE_SHIM = SHIMS / "plyrix-hook-compose.sh"
ALL = [SPOOL_SHIM, HOOK_SHIM, COMPOSE_SHIM]
NAME = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}\.event$")
FAKE = """#!/bin/sh
for arg in "$@"; do printf '%s\\n' "$arg" >> "$LOG_DIR/args"; done
printf '%s\\n' "${Lidarr_EventType-}" > "$LOG_DIR/event"
exit 7
"""


def run_shim(
    shim: Path, tmp_path: Path, env: dict[str, str]
) -> "subprocess.CompletedProcess[bytes]":
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("plyrix", "docker"):
        (bin_dir / name).write_text(FAKE, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    return subprocess.run(
        ["sh", str(shim)],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "LOG_DIR": str(tmp_path), **env},
        capture_output=True,
        check=False,
        timeout=30,
    )


def spool_env(folder: Path, event: dict[str, str]) -> dict[str, str]:
    return {"PLYRIX_LIDARR_SPOOL": str(folder), **event}


@pytest.mark.parametrize("lowercase", [True, False], ids=["as_lidarr_sets_them", "mixed_case"])
def test_test_with_a_writable_spool(tmp_path: Path, lowercase: bool) -> None:
    folder = tmp_path / "spool"
    folder.mkdir()

    proc = run_shim(
        SPOOL_SHIM, tmp_path, spool_env(folder, lidarr_env("Test", lowercase=lowercase))
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == b""
    assert list(folder.iterdir()) == []


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        pytest.param(
            "read_only",
            marks=pytest.mark.skipif(os.geteuid() == 0, reason="root can write in any folder"),
        ),
    ],
)
def test_test_with_a_spool_lidarr_cannot_write(tmp_path: Path, case: str) -> None:
    folder = tmp_path / "spool"
    if case == "read_only":
        folder.mkdir(mode=0o500)
    try:
        proc = run_shim(SPOOL_SHIM, tmp_path, spool_env(folder, lidarr_env("Test", lowercase=True)))
    finally:
        if folder.exists():
            folder.chmod(0o700)

    assert proc.returncode == 1
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert b"PLYRIX_LIDARR_SPOOL" in lines[0]


@pytest.mark.parametrize("lowercase", [True, False], ids=["as_lidarr_sets_them", "mixed_case"])
def test_an_import_becomes_one_file_plyrix_reads(tmp_path: Path, lowercase: bool) -> None:
    folder = tmp_path / "spool"
    folder.mkdir()
    added = ["/m/A/1 one.flac", "/m/A/2\nnewline.flac"]
    event = lidarr_env(
        added=added, deleted=["/m/A/old.mp3"], artist="/m", album_id="12", lowercase=lowercase
    )

    proc = run_shim(SPOOL_SHIM, tmp_path, spool_env(folder, event))

    assert proc.returncode == 0, proc.stderr
    names = [path.name for path in folder.iterdir()]
    assert len(names) == 1, names
    assert NAME.fullmatch(names[0])
    (path,) = spool.entries(folder)
    fields = spool.read(path)
    assert set(fields) == set(lidarr.KEYS)
    parsed = lidarr.parse(fields)
    assert parsed is not None
    assert parsed.added == tuple(added)
    assert parsed.deleted == ("/m/A/old.mp3",)
    assert parsed.is_upgrade
    assert parsed.album_id == "12"


def test_an_import_with_no_deleted_paths_is_not_an_upgrade(tmp_path: Path) -> None:
    folder = tmp_path / "spool"
    folder.mkdir()

    proc = run_shim(SPOOL_SHIM, tmp_path, spool_env(folder, lidarr_env(added=["/m/A/1.flac"])))

    assert proc.returncode == 0, proc.stderr
    (path,) = spool.entries(folder)
    fields = spool.read(path)
    assert fields["Lidarr_DeletedPaths"] == ""
    parsed = lidarr.parse(fields)
    assert parsed is not None and not parsed.is_upgrade


def test_other_events_write_nothing(tmp_path: Path) -> None:
    folder = tmp_path / "spool"
    folder.mkdir()

    proc = run_shim(SPOOL_SHIM, tmp_path, spool_env(folder, lidarr_env("Grab")))

    assert proc.returncode == 0
    assert proc.stderr == b""
    assert list(folder.iterdir()) == []


def test_a_failed_write_still_exits_zero(tmp_path: Path) -> None:
    folder = tmp_path / "spool"
    folder.write_bytes(b"")
    before = sorted(tmp_path.iterdir())

    proc = run_shim(SPOOL_SHIM, tmp_path, spool_env(folder, lidarr_env(added=["/m/A/1.flac"])))

    assert proc.returncode == 0
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert b"PLYRIX_LIDARR_SPOOL" in lines[0]
    assert sorted(path for path in tmp_path.iterdir() if path.name != "bin") == [
        path for path in before if path.name != "bin"
    ]


def test_two_imports_give_two_files(tmp_path: Path) -> None:
    folder = tmp_path / "spool"
    folder.mkdir()
    event = spool_env(folder, lidarr_env(added=["/m/A/1.flac"]))

    run_shim(SPOOL_SHIM, tmp_path, event)
    run_shim(SPOOL_SHIM, tmp_path, event)

    assert len(spool.entries(folder)) == 2
    assert not [path for path in folder.iterdir() if path.name.startswith(".")]


def logged(tmp_path: Path, name: str) -> list[str]:
    path = tmp_path / name
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_the_direct_shim_runs_the_hook(tmp_path: Path) -> None:
    proc = run_shim(HOOK_SHIM, tmp_path, lidarr_env(added=["/m/A/1.flac"]))

    assert proc.returncode == 0
    assert logged(tmp_path, "args") == ["lidarr-hook"]
    assert logged(tmp_path, "event") == ["AlbumDownload"]


def test_the_compose_shim_runs_the_service(tmp_path: Path) -> None:
    proc = run_shim(COMPOSE_SHIM, tmp_path, lidarr_env(added=["/m/A/1.flac"]))

    assert proc.returncode == 0
    names = [arg for key in (*lidarr.KEYS, *lidarr.LOWERCASE_KEYS) for arg in ("-e", key)]
    assert logged(tmp_path, "args") == [
        "compose",
        "run",
        "--pull",
        "never",
        "--rm",
        "--no-deps",
        "-T",
        *names,
        "plyrix",
        "lidarr-hook",
    ]


HOOK_SHIMS = [HOOK_SHIM, COMPOSE_SHIM]
MISSING = 127


def run_broken(
    shim: Path, tmp_path: Path, env: dict[str, str], status: int = MISSING
) -> "subprocess.CompletedProcess[bytes]":
    """Run a hook shim whose plyrix or docker exits with status, as a missing one does."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("plyrix", "docker"):
        (bin_dir / name).write_text(f"#!/bin/sh\nexit {status}\n", encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    return subprocess.run(
        ["sh", str(shim)],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", **env},
        capture_output=True,
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize("status", [MISSING, 1, 125], ids=["missing", "failed", "docker_error"])
@pytest.mark.parametrize("shim", HOOK_SHIMS, ids=lambda path: path.name)
def test_a_hook_shim_that_cannot_run_plyrix_queues_the_import(
    tmp_path: Path, shim: Path, status: int
) -> None:
    folder = tmp_path / "spool"
    folder.mkdir()
    added = ["/m/A/1 one.flac", "/m/A/2\nnewline.flac"]
    event = lidarr_env(added=added, artist="/m", album_id="12", lowercase=True)

    proc = run_broken(shim, tmp_path, spool_env(folder, event), status)

    assert proc.returncode == 0
    assert len(proc.stderr.splitlines()) == 1, proc.stderr
    assert b"queued" in proc.stderr
    (path,) = spool.entries(folder)
    assert not [p for p in folder.iterdir() if p.name.startswith(".")]
    parsed = lidarr.parse(spool.read(path))
    assert parsed is not None
    assert parsed.type == lidarr.ALBUM_DOWNLOAD
    assert parsed.added == tuple(added)
    assert parsed.album_id == "12"


@pytest.mark.parametrize("case", ["unset", "missing", "a_file"])
@pytest.mark.parametrize("shim", HOOK_SHIMS, ids=lambda path: path.name)
def test_a_hook_shim_with_no_spool_still_exits_zero(tmp_path: Path, shim: Path, case: str) -> None:
    folder = tmp_path / "spool"
    if case == "a_file":
        folder.write_bytes(b"")
    env = lidarr_env(added=["/m/A/1.flac"])
    if case != "unset":
        env = spool_env(folder, env)

    proc = run_broken(shim, tmp_path, env)

    assert proc.returncode == 0
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert b"PLYRIX_LIDARR_SPOOL" in lines[0]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        ["bin", *(["spool"] if case == "a_file" else [])]
    )


@pytest.mark.parametrize("status", [MISSING, 1], ids=["missing", "broken_setup"])
@pytest.mark.parametrize("shim", HOOK_SHIMS, ids=lambda path: path.name)
def test_a_hook_shim_still_fails_lidarrs_test(tmp_path: Path, shim: Path, status: int) -> None:
    folder = tmp_path / "spool"
    folder.mkdir()

    proc = run_broken(shim, tmp_path, spool_env(folder, lidarr_env("Test")), status)

    assert proc.returncode == 1
    assert list(folder.iterdir()) == []
    if status == MISSING:
        assert len(proc.stderr.splitlines()) == 1, proc.stderr


@pytest.mark.parametrize("shim", HOOK_SHIMS, ids=lambda path: path.name)
def test_a_hook_shim_queues_nothing_for_other_events(tmp_path: Path, shim: Path) -> None:
    folder = tmp_path / "spool"
    folder.mkdir()

    proc = run_broken(shim, tmp_path, spool_env(folder, lidarr_env("Grab")))

    assert proc.returncode == 0
    assert list(folder.iterdir()) == []


@pytest.mark.parametrize("shim", HOOK_SHIMS, ids=lambda path: path.name)
def test_a_hook_shim_passes_a_working_runs_exit_through(tmp_path: Path, shim: Path) -> None:
    folder = tmp_path / "spool"
    folder.mkdir()

    proc = run_broken(shim, tmp_path, spool_env(folder, lidarr_env("Test")), 0)

    assert proc.returncode == 0
    assert proc.stderr == b""
    assert list(folder.iterdir()) == []


def _function(shim: Path, name: str) -> str:
    text = shim.read_text(encoding="utf-8")
    start = text.index(f"{name}() {{")
    return text[start : text.index("\n}\n", start)]


@pytest.mark.parametrize("shim", HOOK_SHIMS, ids=lambda path: path.name)
def test_the_hook_shims_write_events_as_the_spool_shim_does(shim: Path) -> None:
    assert _function(shim, "write_event") == _function(SPOOL_SHIM, "write_event")


@pytest.mark.parametrize("shim", ALL, ids=lambda path: path.name)
def test_each_shim_is_an_executable_posix_script(shim: Path) -> None:
    proc = subprocess.run(["sh", "-n", str(shim)], capture_output=True, check=False)

    assert proc.returncode == 0, proc.stderr
    assert shim.read_text(encoding="utf-8").startswith("#!/bin/sh\n")
    assert shim.stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize("shim", ALL, ids=lambda path: path.name)
def test_no_shim_needs_the_docker_socket(shim: Path) -> None:
    assert "docker.sock" not in shim.read_text(encoding="utf-8")


@pytest.mark.parametrize("shim", ALL, ids=lambda path: path.name)
def test_no_shim_holds_a_bashism(shim: Path) -> None:
    code = "\n".join(
        line
        for line in shim.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )

    for bashism in ("[[", "local ", "$'", "declare", "function ", "=(", "${!", "<<<"):
        assert bashism not in code, bashism


def test_the_scripts_are_tracked_as_executable() -> None:
    proc = subprocess.run(
        ["git", "ls-files", "-s", "examples/lidarr"],
        cwd=SHIMS.parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    modes = {line.split()[3]: line.split()[0] for line in proc.stdout.splitlines()}
    if not modes:
        pytest.skip("examples/lidarr is not tracked yet")
    for shim in ALL:
        assert modes[f"examples/lidarr/{shim.name}"] == "100755"
    assert os.access(SPOOL_SHIM, os.X_OK)
