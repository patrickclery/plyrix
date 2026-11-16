"""SAFE-02: the runtime tripwire, proved in child processes against the real code.

An audit hook can't be removed, so no test here installs the guard in the pytest process (D-28).
Each case starts a fresh interpreter, installs the guard on a library under tmp_path and attempts
one operation. The parent reads the exit status and compares the library with a snapshot.
"""

import os
import shutil
import stat
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import FIXTURES, RunCli, StubLrclib, parse_counts, snap

TRIPPED = 42
EXIT_GUARD_TRIPPED = 70
NOTICE_WORD = "pre-release"
SIDECAR_BYTES = b"[00:01.00] invented line\n"

CHILD_START = """
import errno, hashlib, os, shutil, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path

from plyrix import guard

mode, root, outside = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
audio = root / "t.flac"
temp = root / ".plyrix-0123abcd.part"
owned = root / "owned.lrc"
album = root / "album"
"""

# An op fails the child with a traceback unless the guard raises it, so a typo is never a pass.
CHILD_GUARDED = """
if mode == "guarded":
    guard.install([root])
elif mode == "read_only":
    guard.install([root], read_only=True)
try:
{action}
except guard.GuardViolation:
    sys.exit(42 if guard.tripped else 1)
if guard.tripped:
    sys.exit(1)
"""


@dataclass(frozen=True)
class Op:
    """One attempted operation. It runs after setup, which runs before the guard is installed."""

    id: str
    action: str
    setup: str = ""
    # Whether running it with no guard is expected to change the library.
    changes: bool = True
    # A sidecar or temp name may be removed, so this op can take those before it trips on audio.
    may_remove_sidecars: bool = False


READ_WRITE_FD = "fd = os.open(audio, os.O_RDWR)"
ROOT_FD = "dfd = os.open(root, os.O_RDONLY)"
# Evaluated in the child, where audio is defined. Unguarded, these really do change the file.
APPEND_TO_AUDIO = """f"printf x >> '{audio}'\""""

BLOCKED = [
    Op("open_w", 'open(audio, "wb")'),
    Op("open_a", 'with open(audio, "ab") as f: f.write(b"x")'),
    Op("open_r_plus", 'with open(audio, "r+b") as f: f.write(b"x")'),
    Op("open_x", 'open(root / "new.flac", "xb")'),
    Op("os_open_write_flag", 'os.write(os.open(audio, os.O_WRONLY), b"x")'),
    Op("os_open_rdwr_flag", 'os.write(os.open(audio, os.O_RDWR), b"x")'),
    Op("os_open_create_flag", 'os.open(root / "new.flac", os.O_WRONLY | os.O_CREAT)'),
    Op(
        "os_open_create_with_read_only_flag", 'os.open(root / "new.flac", os.O_RDONLY | os.O_CREAT)'
    ),
    Op(
        "os_open_append_with_read_only_flag",
        "os.open(audio, os.O_RDONLY | os.O_APPEND)",
        changes=False,
    ),
    Op("os_open_trunc_flag", "os.open(audio, os.O_RDONLY | os.O_TRUNC)"),
    Op("os_open_append_flag", 'os.write(os.open(audio, os.O_WRONLY | os.O_APPEND), b"x")'),
    Op(
        "os_open_relative_dir_fd",
        'os.write(os.open("t.flac", os.O_WRONLY, dir_fd=dfd), b"x")',
        setup=ROOT_FD,
    ),
    Op(
        "os_open_relative_path_object_dir_fd",
        'os.write(os.open(Path("t.flac"), os.O_WRONLY, dir_fd=dfd), b"x")',
        setup=ROOT_FD,
    ),
    Op("open_relative_to_cwd", 'open("t.flac", "wb")', setup="os.chdir(root)"),
    # FileIO is the one call whose event carries a Path, so this row needs the isinstance test.
    Op(
        "fileio_relative_path_object",
        'import io; io.FileIO(Path("new.flac"), "w")',
        setup="os.chdir(outside)",
        changes=False,
    ),
    Op("fdopen", 'with os.fdopen(fd, "wb") as f: f.write(b"x")', setup=READ_WRITE_FD),
    Op("path_write_text", 'audio.write_text("x")'),
    Op("path_write_bytes", 'audio.write_bytes(b"x")'),
    Op("path_open", 'audio.open("wb")'),
    Op("mkstemp", "tempfile.mkstemp(dir=root)"),
    Op("named_temporary_file", "tempfile.NamedTemporaryFile(dir=root, delete=False)"),
    Op("os_link_from_audio", 'os.link(audio, root / "x.lrc")'),
    Op("path_hardlink_to_from_audio", '(root / "x.lrc").hardlink_to(audio)'),
    Op("os_link_audio_to_temp_name", "os.link(audio, root / '.plyrix-aaaaaaaa.part')"),
    Op("os_link_temp_to_audio_name", 'os.link(temp, root / "x.flac")'),
    Op("os_link_temp_to_temp_name", "os.link(temp, root / '.plyrix-aaaaaaaa.part')"),
    Op("os_rename_temp_to_temp_name", "os.rename(temp, root / '.plyrix-aaaaaaaa.part')"),
    Op("os_link_from_a_sidecar", 'os.link(owned, root / "x.lrc")'),
    Op("os_symlink", 'os.symlink(audio, root / "x.lrc")'),
    Op("path_symlink_to", '(root / "x.lrc").symlink_to(audio)'),
    Op("os_rename_onto_audio", "os.rename(temp, audio)"),
    Op("os_replace_onto_audio", "os.replace(temp, audio)"),
    Op("path_rename_onto_audio", "temp.rename(audio)"),
    Op("path_replace_onto_audio", "temp.replace(audio)"),
    Op("os_rename_audio_away", 'os.rename(audio, root / "x.lrc")'),
    Op("os_rename_sidecar_onto_sidecar", 'os.rename(owned, root / "x.txt")'),
    Op("os_remove", "os.remove(audio)"),
    Op("os_unlink", "os.unlink(audio)"),
    Op("path_unlink", "audio.unlink()"),
    Op("os_truncate", "os.truncate(audio, 0)"),
    Op("os_ftruncate", "os.ftruncate(fd, 0)", setup=READ_WRITE_FD),
    Op("os_utime", "os.utime(audio, (1, 1))"),
    Op("path_touch", "audio.touch()"),
    Op("os_chmod", "os.chmod(audio, 0o600)"),
    Op("os_fchmod", "os.fchmod(fd, 0o600)", setup=READ_WRITE_FD),
    Op("path_chmod", "audio.chmod(0o600)"),
    Op("os_chown", "os.chown(audio, os.getuid(), os.getgid())"),
    Op("os_setxattr", 'os.setxattr(audio, "user.plyrix", b"x")', changes=False),
    Op("os_removexattr", 'os.removexattr(audio, "user.plyrix")', changes=False),
    Op("os_mkdir", 'os.mkdir(root / "new")'),
    Op("path_mkdir", '(root / "new").mkdir()'),
    Op("os_makedirs", 'os.makedirs(root / "new" / "deeper")'),
    Op("os_rmdir", "os.rmdir(album)"),
    Op("shutil_copyfile_onto_audio", 'shutil.copyfile(outside / "other.flac", audio)'),
    Op("shutil_copyfile_into_root", 'shutil.copyfile(audio, root / "copy.flac")'),
    Op("shutil_copy2_into_root", 'shutil.copy2(audio, root / "copy.flac")'),
    Op("shutil_move", 'shutil.move(audio, outside / "moved.flac")'),
    Op("shutil_rmtree", "shutil.rmtree(root)", may_remove_sidecars=True),
    Op("sqlite3_connect_in_root", 'sqlite3.connect(root / "x.db").execute("CREATE TABLE t(x)")'),
    Op("subprocess_run", f"subprocess.run(['/bin/sh', '-c', {APPEND_TO_AUDIO}], check=True)"),
    Op("os_system", f"os.system({APPEND_TO_AUDIO})"),
    Op("os_fork", "os.fork()", changes=False),
    Op("os_execv", 'os.execv("/bin/true", ["true"])', changes=False),
    Op("os_posix_spawn", 'os.posix_spawn("/bin/true", ["true"], {})', changes=False),
    Op("ctypes_load", "import ctypes", changes=False),
]

ALLOWED = [
    Op("read_only_open", 'assert open(audio, "rb").read(4) == b"fLaC"', changes=False),
    Op("read_only_os_open", "os.close(os.open(audio, os.O_RDONLY))", changes=False),
    Op("directory_listing", 'assert "t.flac" in os.listdir(root)', changes=False),
    Op("raw_temp_create", "os.close(os.open(root / '.plyrix-ffffffff.part', NEW_FILE))"),
    Op("raw_link_temp_to_lrc", 'os.link(temp, root / "t.lrc")'),
    Op("raw_link_temp_to_txt", 'os.link(temp, root / "t.txt")'),
    Op("raw_unlink_temp", "os.unlink(temp)"),
    Op("raw_replace_temp_onto_lrc", "os.replace(temp, owned)"),
    Op("raw_rename_temp_onto_txt", 'os.rename(temp, root / "t.txt")'),
    Op("raw_remove_lrc", "os.remove(owned)"),
    Op("write_mode_open_of_a_sidecar", 'open(root / "new.lrc", "xb")'),
    Op(
        "publish_lrc",
        """
assert sidecars.publish(TRACK, Kind.LRC, b"x\\n") is sidecars.Result.WRITTEN
assert (root / "t.lrc").read_bytes() == b"x\\n"
""",
    ),
    Op(
        "publish_txt",
        """
assert sidecars.publish(TRACK, Kind.TXT, b"x\\n") is sidecars.Result.WRITTEN
assert (root / "t.txt").read_bytes() == b"x\\n"
""",
    ),
    Op(
        "publish_refused_by_an_existing_name",
        """
result = sidecars.publish(OWNED_TRACK, Kind.LRC, b"x\\n")
assert result is sidecars.Result.EXISTS
""",
        changes=False,
    ),
    Op(
        "publish_through_claim_then_replace",
        """
def no_hard_links(*_args, **_kwargs):
    raise OSError(errno.EPERM, "no hard links")

os.link = no_hard_links
assert sidecars.publish(TRACK, Kind.LRC, b"x\\n") is sidecars.Result.WRITTEN
assert (root / "t.lrc").read_bytes() == b"x\\n"
""",
    ),
    Op(
        "remove_owned",
        """
assert sidecars.remove_owned(OWNED_TRACK, Kind.LRC, OWNED_SHA256)
assert not owned.exists()
""",
    ),
    Op(
        "replace_owned",
        """
result = sidecars.replace_owned(OWNED_TRACK, Kind.LRC, b"x\\n", OWNED_SHA256)
assert result is sidecars.Result.WRITTEN
assert owned.read_bytes() == b"x\\n"
""",
    ),
    Op(
        "sweep_temps",
        """
assert sidecars.sweep_temps(os.fsencode(root), time.time()) == 1
assert not temp.exists()
""",
        setup="os.utime(temp, (1, 1))",
    ),
    Op(
        "tag_read",
        """
found = tags.read(os.fsencode(audio))
assert found.duration
assert TinyTag.get(audio).duration
""",
        setup="from tinytag import TinyTag",
        changes=False,
    ),
    Op(
        "ledger_outside_the_root",
        """
conn = db.connect(outside / "nested" / "state.db")
db.begin_run(conn, "0")
conn.close()
assert (outside / "nested" / "state.db").exists()
""",
        changes=False,
    ),
    Op("sqlite_in_memory", 'sqlite3.connect(":memory:").execute("SELECT 1")', changes=False),
    Op("write_outside_the_root", 'open(outside / "x.flac", "wb")', changes=False),
]

ALLOWED_PRELUDE = """
from plyrix import sidecars, tags
from plyrix.domain import Kind, Track
from plyrix.state import db

NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL
TRACK = Track(os.fsencode(root), b"t.flac", b"t")
OWNED_TRACK = Track(os.fsencode(root), b"owned.flac", b"owned")
OWNED_SHA256 = hashlib.sha256(owned.read_bytes()).hexdigest()
"""


def child_script(op: Op, prelude: str = "") -> str:
    # setup and prelude run before the guard goes on, as the real CLI's imports do.
    setup = textwrap.dedent(op.setup)
    action = textwrap.indent(textwrap.dedent(op.action).strip("\n"), "    ")
    return CHILD_START + prelude + setup + "\n" + CHILD_GUARDED.format(action=action)


def run_child(
    script: str, mode: str, root: Path, outside: Path, env: dict[str, str] | None = None
) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(
        [sys.executable, "-c", script, mode, root, outside],
        capture_output=True,
        check=False,
        timeout=60,
        env={**os.environ, **(env or {})},
    )


def tree(root: Path) -> dict[str, object]:
    """Identify everything under root, so any entry or folder that changes shows."""
    if not root.exists():
        return {}
    found: dict[str, object] = {".": _entry_stat(root)}
    for base, dirs, files in os.walk(root):
        for name in [*dirs, *files]:
            path = Path(base) / name
            is_regular = stat.S_ISREG(path.lstat().st_mode)
            found[str(path.relative_to(root))] = snap(path) if is_regular else _entry_stat(path)
    return found


def _entry_stat(path: Path) -> tuple[int, int, int, int]:
    st = path.lstat()
    return (st.st_mode, st.st_ino, st.st_mtime_ns, st.st_ctime_ns)


@pytest.fixture
def lib(tmp_path: Path) -> Path:
    root = tmp_path / "lib"
    (root / "album").mkdir(parents=True)
    shutil.copyfile(FIXTURES / "t.flac", root / "t.flac")
    (root / ".plyrix-0123abcd.part").write_bytes(SIDECAR_BYTES)
    (root / "owned.lrc").write_bytes(SIDECAR_BYTES)
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    directory = tmp_path / "outside"
    directory.mkdir()
    shutil.copyfile(FIXTURES / "t.flac", directory / "other.flac")
    return directory


def named(ops: list[Op]) -> list[object]:
    return [pytest.param(op, id=op.id) for op in ops]


def test_the_blocked_table_covers_every_listed_kind_of_write() -> None:
    assert len(BLOCKED) >= 26
    assert len({op.id for op in BLOCKED}) == len(BLOCKED)


@pytest.mark.parametrize("op", named(BLOCKED))
def test_a_write_style_operation_trips_and_changes_nothing(
    lib: Path, outside: Path, op: Op
) -> None:
    before = tree(lib)
    audio_before = snap(lib / "t.flac")
    proc = run_child(child_script(op), "guarded", lib, outside)
    assert proc.returncode == TRIPPED, proc.stderr
    assert snap(lib / "t.flac") == audio_before
    if not op.may_remove_sidecars:
        assert tree(lib) == before


@pytest.mark.parametrize("op", named([op for op in BLOCKED if op.changes]))
def test_with_no_guard_the_same_operation_does_change_the_library(
    lib: Path, outside: Path, op: Op
) -> None:
    # Proves each row reaches what it claims to, so a row can't pass by doing nothing.
    before = tree(lib)
    proc = run_child(child_script(op), "unguarded", lib, outside)
    assert proc.returncode == 0, proc.stderr
    assert tree(lib) != before


@pytest.mark.parametrize("op", named(ALLOWED))
def test_an_allowed_flow_passes_and_leaves_audio_alone(lib: Path, outside: Path, op: Op) -> None:
    audio_before = snap(lib / "t.flac")
    before = tree(lib)
    proc = run_child(child_script(op, ALLOWED_PRELUDE), "guarded", lib, outside)
    assert proc.returncode == 0, proc.stderr
    assert snap(lib / "t.flac") == audio_before
    if op.changes:
        assert tree(lib) != before


def test_requests_get_passes_through_the_guard(
    lib: Path, outside: Path, stub_lrclib: StubLrclib
) -> None:
    op = Op(
        "requests_get",
        """
assert requests.get(os.environ["STUB_URL"] + "/get", timeout=30).status_code == 200
session = lrclib.new_session()
status, record = lrclib.get(session, os.environ["STUB_URL"], "a", "t", None, 3.0)
assert status is lrclib.Status.FOUND and record
""",
        setup="import requests\nfrom plyrix import lrclib",
    )
    env = {"STUB_URL": stub_lrclib.base_url}
    proc = run_child(child_script(op), "guarded", lib, outside, env=env)
    assert proc.returncode == 0, proc.stderr
    assert len(stub_lrclib.requests) == 2


NAME_ATTEMPT = """
descriptor = os.open(root / sys.argv[4], os.O_WRONLY | os.O_CREAT | os.O_EXCL)
os.close(descriptor)
"""


def _try_name(lib: Path, outside: Path, name: str) -> "subprocess.CompletedProcess[bytes]":
    script = CHILD_START + CHILD_GUARDED.format(
        action=textwrap.indent(NAME_ATTEMPT.strip("\n"), "    ")
    )
    return subprocess.run(
        [sys.executable, "-c", script, "guarded", lib, outside, name],
        capture_output=True,
        check=False,
        timeout=60,
    )


@pytest.mark.parametrize(
    "name",
    [
        ".plyrix-01234567.part",
        ".plyrix-ffffffff.part",
        "x.lrc",
        "x.txt",
        "x.flac.lrc",
    ],
)
def test_exact_allowed_names_pass(lib: Path, outside: Path, name: str) -> None:
    proc = _try_name(lib, outside, name)
    assert proc.returncode == 0, proc.stderr
    assert (lib / name).exists()


@pytest.mark.parametrize(
    "name",
    [
        ".plyrix-0123abc.part",  # 7 hex
        ".plyrix-0123abcde.part",  # 9 hex
        ".plyrix-0123ABCD.part",  # uppercase hex
        ".plyrix-0123abcg.part",  # not hex
        "plyrix-0123abcd.part",  # not hidden
        ".plyrix-0123abcd.partx",
        ".plyrix-0123abcd.part\n",
        "x.lrcx",
        "x.lr",
        "x.LRC",
        "x.TXT",
        "x.txtx",
        "x.flac",
        "x.lrc.flac",
        "lrc",
    ],
)
def test_near_miss_names_trip(lib: Path, outside: Path, name: str) -> None:
    before = tree(lib)
    proc = _try_name(lib, outside, name)
    assert proc.returncode == TRIPPED, proc.stderr
    assert tree(lib) == before


@pytest.mark.parametrize(
    ("link_name", "action"),
    [
        ("t.lrc", 'open(root / "t.lrc", "wb")'),
        ("t.lrc", 'os.open(root / "t.lrc", os.O_WRONLY)'),
        ("t.txt", 'open(root / "t.txt", "wb")'),
        (".plyrix-aaaaaaaa.part", "open(root / '.plyrix-aaaaaaaa.part', 'wb')"),
    ],
)
def test_a_sidecar_name_that_is_a_symlink_to_audio_trips(
    lib: Path, outside: Path, link_name: str, action: str
) -> None:
    (lib / link_name).symlink_to("t.flac")
    before = tree(lib)
    proc = run_child(child_script(Op("symlink", action)), "guarded", lib, outside)
    assert proc.returncode == TRIPPED, proc.stderr
    assert tree(lib) == before


@pytest.mark.parametrize(
    "action",
    [
        'open(root / "elsewhere.flac", "wb")',
        'os.remove(root / "elsewhere.flac")',
        'os.rename(root / "elsewhere.flac", root / "x.lrc")',
    ],
)
def test_a_symlink_with_an_audio_name_trips_even_when_it_points_outside(
    lib: Path, outside: Path, action: str
) -> None:
    # Its real path is outside the root, so only the name in its real parent shows it is in scope.
    (lib / "elsewhere.flac").symlink_to(outside / "other.flac")
    before = tree(lib)
    other_before = snap(outside / "other.flac")
    proc = run_child(child_script(Op("elsewhere", action)), "guarded", lib, outside)
    assert proc.returncode == TRIPPED, proc.stderr
    assert tree(lib) == before
    assert snap(outside / "other.flac") == other_before


def test_a_path_reaching_audio_through_a_symlinked_folder_trips(lib: Path, outside: Path) -> None:
    (outside / "alias").symlink_to(lib)
    before = tree(lib)
    op = Op("alias", 'open(outside / "alias" / "t.flac", "wb")')
    proc = run_child(child_script(op), "guarded", lib, outside)
    assert proc.returncode == TRIPPED, proc.stderr
    assert tree(lib) == before


def test_a_root_given_through_a_symlink_is_still_guarded(lib: Path, outside: Path) -> None:
    alias = outside / "alias"
    alias.symlink_to(lib)
    before = tree(lib)
    # root is the alias here, while the write goes to the real path.
    op = Op("root_alias", f'open("{lib / "t.flac"}", "wb")')
    proc = run_child(child_script(op), "guarded", alias, outside)
    assert proc.returncode == TRIPPED, proc.stderr
    assert tree(lib) == before


def test_a_library_at_the_filesystem_root_is_still_guarded(lib: Path, outside: Path) -> None:
    before = tree(lib)
    script = (
        CHILD_START
        + 'guard.install(["/"])\n'
        + "try:\n"
        + '    open(audio, "wb")\n'
        + "except guard.GuardViolation:\n"
        + "    sys.exit(42)\n"
    )
    proc = run_child(script, "guarded", lib, outside)
    assert proc.returncode == TRIPPED, proc.stderr
    assert tree(lib) == before


def test_a_sibling_folder_sharing_the_roots_prefix_is_not_inside_it(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    sibling = tmp_path / "library"
    root.mkdir()
    sibling.mkdir()
    op = Op("sibling", 'open(outside / "x.flac", "wb")')
    proc = run_child(child_script(op), "guarded", root, sibling)
    assert proc.returncode == 0, proc.stderr
    assert (sibling / "x.flac").exists()


def test_a_swallowed_violation_stays_on_the_sticky_list(lib: Path, outside: Path) -> None:
    before = tree(lib)
    script = (
        CHILD_START
        + "guard.install([root])\n"
        + "try:\n"
        + '    open(audio, "wb")\n'
        + "except BaseException:\n"
        + "    pass\n"
        + 'sys.exit(42 if guard.tripped == ["open"] else 1)\n'
    )
    proc = run_child(script, "guarded", lib, outside)
    assert proc.returncode == TRIPPED, proc.stderr
    assert tree(lib) == before


def test_except_exception_cannot_swallow_a_violation(lib: Path, outside: Path) -> None:
    before = tree(lib)
    op = Op(
        "except_exception",
        """
try:
    open(audio, "wb")
except Exception:
    pass
""",
    )
    proc = run_child(child_script(op), "guarded", lib, outside)
    assert proc.returncode == TRIPPED, proc.stderr
    assert tree(lib) == before


@pytest.mark.parametrize("db_name", ["state.db", "nested/state.db"])
def test_the_ledger_inside_the_root_aborts_and_creates_nothing(
    lib: Path, outside: Path, db_name: str
) -> None:
    # Called directly, so this backstop never depends on config refusing the same input first.
    before = tree(lib)
    op = Op("backstop", f'db.connect(root / "{db_name}")')
    script = child_script(op, "from plyrix.state import db\n")
    proc = run_child(script, "guarded", lib, outside)
    assert proc.returncode == TRIPPED, proc.stderr
    assert tree(lib) == before
    assert not (lib / db_name).exists()


CLI_CHILD = """
import os, sys

from plyrix import cli, pipeline
from plyrix.state import db

mode, library = sys.argv[1:]
audio = os.path.join(library, "t.flac")


def attempt_write(settings, start):
    if mode == "raises":
        open(audio, "wb")
    try:
        open(audio, "wb")
    except BaseException:
        pass
    if mode == "swallows_then_lrclib_stops":
        raise pipeline.UnavailableStop(pipeline.Counts())
    if mode == "swallows_then_ledger_fails":
        raise db.StateError("plyrix: the state database can't be used (Test)")
    return pipeline.Counts()


pipeline.run = attempt_write
cli.app(["run", library])
"""


def run_cli_child(
    mode: str, lib: Path, tmp_path: Path, stub: StubLrclib
) -> "subprocess.CompletedProcess[bytes]":
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("PLYRIX_")}
    return subprocess.run(
        [sys.executable, "-c", CLI_CHILD, mode, lib],
        env={
            **inherited,
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "PLYRIX_LRCLIB_URL": stub.base_url,
        },
        capture_output=True,
        check=False,
        timeout=60,
    )


@pytest.mark.parametrize(
    "mode",
    ["raises", "swallows", "swallows_then_lrclib_stops", "swallows_then_ledger_fails"],
)
def test_the_cli_exits_70_with_one_line_when_a_run_trips_the_guard(
    tmp_path: Path, lib: Path, stub_lrclib: StubLrclib, mode: str
) -> None:
    before = tree(lib)
    proc = run_cli_child(mode, lib, tmp_path, stub_lrclib)
    assert proc.returncode == EXIT_GUARD_TRIPPED, proc.stderr
    assert tree(lib) == before
    lines = [line for line in proc.stderr.decode().splitlines() if NOTICE_WORD not in line and line]
    assert len(lines) == 1, proc.stderr
    assert "Traceback" not in lines[0]
    assert "tripwire" in lines[0]
    assert str(lib) not in lines[0]


def test_a_real_run_passes_through_the_guard_the_cli_installs(
    tmp_path: Path, lib: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    audio_before = snap(lib / "t.flac")
    proc = run_cli(["run", lib], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})
    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["written_lrc"] == 1
    assert (lib / "t.lrc").is_file()
    assert snap(lib / "t.flac") == audio_before


def test_a_real_run_with_no_hard_links_passes_through_the_guard(
    tmp_path: Path, lib: Path, stub_lrclib: StubLrclib
) -> None:
    script = """
import errno, os, sys

from plyrix import cli


def no_hard_links(*_args, **_kwargs):
    raise OSError(errno.EPERM, "no hard links")


os.link = no_hard_links
cli.app(["run", sys.argv[1]])
"""
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("PLYRIX_")}
    audio_before = snap(lib / "t.flac")
    proc = subprocess.run(
        [sys.executable, "-c", script, lib],
        env={
            **inherited,
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "PLYRIX_LRCLIB_URL": stub_lrclib.base_url,
        },
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["written_lrc"] == 1
    assert (lib / "t.lrc").is_file()
    assert snap(lib / "t.flac") == audio_before


def test_importing_the_cli_installs_nothing(lib: Path, outside: Path) -> None:
    script = (
        CHILD_START
        + "from plyrix import cli\n"
        + "subprocess.run([sys.executable, '-c', 'pass'], check=True)\n"
    )
    proc = run_child(script, "unguarded", lib, outside)
    assert proc.returncode == 0, proc.stderr


def test_pytest_itself_has_no_guard_installed() -> None:
    # A guard in this process would trip every spawn below, whatever its roots were.
    from plyrix import guard

    assert guard._roots == ()
    assert guard.tripped == []
    assert subprocess.run([sys.executable, "-c", "pass"], check=False).returncode == 0


KNOWN_GAP = """
import os, sys

from plyrix import guard

root = sys.argv[1]
call = {"mkfifo": os.mkfifo, "mknod": os.mknod}[sys.argv[2]]
guard.install([root])
events = []
sys.addaudithook(lambda event, _args: events.append(event))
events.clear()
call(os.path.join(root, "new.flac"))
assert events == [], events
try:
    call(os.path.join(root, "t.flac"))
except FileExistsError:
    pass
else:
    raise SystemExit("it replaced an existing file")
assert not guard.tripped
"""


@pytest.mark.parametrize("call", ["mkfifo", "mknod"])
def test_known_gap_mkfifo_and_mknod_raise_no_audit_event(lib: Path, call: str) -> None:
    # The guard can't see these, so it can't stop them making a new name. They refuse an existing
    # name, so audio survives. If a later Python adds an event, this fails and the gap has closed.
    audio_before = snap(lib / "t.flac")
    proc = subprocess.run(
        [sys.executable, "-c", KNOWN_GAP, lib, call],
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert snap(lib / "t.flac") == audio_before
    assert (lib / "new.flac").exists()


def test_known_gap_a_hard_link_to_audio_is_not_seen(lib: Path, outside: Path) -> None:
    # A name can't show it shares an inode with audio, so the guard allows this write. The writer
    # never opens an existing name for writing, which test_sidecars.py proves.
    audio = lib / "t.flac"
    os.link(audio, lib / "t.lrc")
    op = Op("hard_link", 'open(root / "t.lrc", "wb")')
    proc = run_child(child_script(op), "guarded", lib, outside)
    assert proc.returncode == 0, proc.stderr
    assert audio.stat().st_size == 0


def test_known_gap_sqlite_attach_reaches_the_root_without_the_connect_event_showing_it(
    lib: Path, outside: Path
) -> None:
    # Only state/db.py may import sqlite3, and it never attaches, so the AST check is the layer
    # holding this.
    action = """
conn = sqlite3.connect(":memory:")
conn.execute(f"ATTACH DATABASE '{root}/x.db' AS other")
conn.execute("CREATE TABLE other.t(x)")
"""
    proc = run_child(child_script(Op("sqlite_gap", action)), "guarded", lib, outside)
    assert proc.returncode == 0, proc.stderr
    assert (lib / "x.db").exists()


CREATE_TABLE = '.execute("CREATE TABLE t(x)")'


@pytest.mark.parametrize(
    "op",
    named(
        [
            Op("absolute", f'sqlite3.connect(f"file:{{root}}/x.db", uri=True){CREATE_TABLE}'),
            Op(
                "empty_authority",
                f'sqlite3.connect(f"file://{{root}}/x.db", uri=True){CREATE_TABLE}',
            ),
            Op(
                "localhost",
                f'sqlite3.connect(f"file://localhost{{root}}/x.db", uri=True){CREATE_TABLE}',
            ),
            Op(
                "with_a_query",
                f'sqlite3.connect(f"file:{{root}}/x.db?mode=rwc", uri=True){CREATE_TABLE}',
            ),
            # %78 is x, so the real name only shows once the URI is decoded.
            Op(
                "percent_encoded",
                f'sqlite3.connect(f"file:{{root}}/%78.db", uri=True){CREATE_TABLE}',
            ),
            Op(
                "relative",
                f'sqlite3.connect("file:x.db", uri=True){CREATE_TABLE}',
                setup="os.chdir(root)",
            ),
        ]
    ),
)
def test_a_sqlite_uri_inside_the_library_trips(lib: Path, outside: Path, op: Op) -> None:
    before = tree(lib)

    proc = run_child(child_script(op), "guarded", lib, outside)

    assert proc.returncode == TRIPPED, proc.stderr
    assert tree(lib) == before


@pytest.mark.parametrize(
    "op",
    named(
        [
            Op("uri_from_outside", 'sqlite3.connect(f"file:{outside}/x.db", uri=True).close()'),
            # A URI's text read as a relative path would put it inside the library.
            Op(
                "uri_from_inside",
                'sqlite3.connect(f"file:{outside}/x.db", uri=True).close()',
                setup="os.chdir(root)",
            ),
            Op(
                "read_only_uri_from_inside",
                """
sqlite3.connect(outside / "ro.db").close()
sqlite3.connect(f"file:{outside}/ro.db?mode=ro", uri=True).close()
""",
                setup="os.chdir(root)",
            ),
            Op("memory_from_inside", 'sqlite3.connect(":memory:").close()', setup="os.chdir(root)"),
        ]
    ),
)
def test_a_database_outside_the_library_passes_from_any_folder(
    lib: Path, outside: Path, op: Op
) -> None:
    before = tree(lib)

    proc = run_child(child_script(op), "guarded", lib, outside)

    assert proc.returncode == 0, proc.stderr
    assert tree(lib) == before


# Every write a publish, a replace or a removal makes. Each name is one the guard otherwise allows.
READ_ONLY_REFUSES = [
    Op("sidecar_name", 'open(root / "new.lrc", "xb")'),
    Op("plain_sidecar_name", 'open(root / "new.txt", "xb")'),
    Op("temp_name", 'open(root / ".plyrix-89abcdef.part", "xb")'),
    Op("link", 'os.link(temp, root / "new.lrc")'),
    Op("replace", "os.replace(temp, owned)"),
    Op("rename", 'os.rename(temp, root / "new.lrc")'),
    Op("remove_sidecar", "os.remove(owned)"),
    Op("remove_temp", "os.remove(temp)"),
]


@pytest.mark.parametrize("op", named(READ_ONLY_REFUSES))
def test_read_only_refuses_every_write_under_the_root(lib: Path, outside: Path, op: Op) -> None:
    before = tree(lib)

    proc = run_child(child_script(op), "read_only", lib, outside)

    assert proc.returncode == TRIPPED, proc.stderr
    assert tree(lib) == before


@pytest.mark.parametrize("op", named(READ_ONLY_REFUSES))
def test_read_only_off_allows_each_of_those_writes(lib: Path, outside: Path, op: Op) -> None:
    proc = run_child(child_script(op), "guarded", lib, outside)

    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize(
    "op",
    named(
        [
            Op("read_audio", "audio.read_bytes()"),
            Op("read_sidecar", "owned.read_bytes()"),
            Op("list_folder", "os.listdir(root)"),
            Op("write_outside", 'open(outside / "x.lrc", "xb").close()'),
            Op(
                "replace_outside",
                """
open(outside / "a.part", "xb").close()
os.replace(outside / "a.part", outside / "x.lrc")
""",
            ),
            Op("remove_outside", 'os.remove(outside / "other.flac")'),
        ]
    ),
)
def test_read_only_allows_a_read_and_a_write_outside(lib: Path, outside: Path, op: Op) -> None:
    before = tree(lib)

    proc = run_child(child_script(op), "read_only", lib, outside)

    assert proc.returncode == 0, proc.stderr
    assert tree(lib) == before
