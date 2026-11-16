"""Name, public wording and static-analysis checks, all through the single test command."""

import ast
import inspect
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from plyrix import sidecars

ROOT = Path(__file__).resolve().parents[1]
_PROC_FDS = "/proc/self/fd"
# System paths src may name as a literal, each by exact match only.
_SYSTEM_PATHS = ("/run/secrets", "/proc/self/mountinfo")
_NO_CACHES = shutil.ignore_patterns("__pycache__")

# Built at run time so that no tracked file holds an old name, including this one.
OLD_NAME = ("sync" + "side").encode()
OLD_NAMES = (OLD_NAME, ("plip" + "sync").encode())

_CHANGE_VERB = r"(?:writ\w*|overwrit\w*|modif\w*|alter\w*|chang\w*|touch\w*|damag\w*)"
_UNSAFE_WORDING = re.compile(
    r"\bsandbox\w*\b"
    r"|\bimpossible\b[^.\n]*\baudio\b"
    r"|\baudio\b[^.\n]*\bimpossible\b"
    rf"|\b(?:cannot|can't|can\s+not|never|unable\s+to)\s+{_CHANGE_VERB}\s+(?:to\s+)?"
    r"(?:\w+\s+){0,2}?audio\b"
    r"|\baudio(?:\s+files?)?\s+(?:can(?:not|'t|\s+not|\s+never)\s+be|(?:is|are)\s+never)\s+"
    rf"{_CHANGE_VERB}",
    re.IGNORECASE,
)


def find_old_name(paths: list[bytes]) -> list[bytes]:
    """Return the paths holding an old name in their path or contents, in sorted byte order."""
    if not paths:
        raise ValueError("refusing to check an empty file list")
    offenders = []
    for path in paths:
        if any(name in path.lower() for name in OLD_NAMES):
            offenders.append(path)
        elif os.path.isfile(path):
            with open(path, "rb") as handle:
                contents = handle.read().lower()
            if any(name in contents for name in OLD_NAMES):
                offenders.append(path)
    return sorted(offenders)


def unsafe_wording(text: str) -> list[str]:
    """Return phrases that claim audio writes are impossible or call a safety layer a sandbox."""
    return [match.group(0) for match in _UNSAFE_WORDING.finditer(text)]


def test_name_pyproject() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["name"] == "plyrix"
    assert list(project["scripts"]) == ["plyrix"]
    assert project["scripts"]["plyrix"] == "plyrix.cli:app"
    # Listed rather than tested with is_dir, which would ignore case on some filesystems.
    assert "plyrix" in os.listdir(ROOT / "src")
    assert (ROOT / "src" / "plyrix").is_dir()


def test_name_absent_from_tracked_files(monkeypatch: pytest.MonkeyPatch) -> None:
    # Relative paths, so a parent folder of the checkout can never count as a match.
    monkeypatch.chdir(ROOT)
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True,
        check=True,
    )
    paths = [path for path in listed.stdout.split(b"\0") if path]
    assert paths, "git listed no files, so this check would pass without checking anything"
    assert find_old_name(paths) == []


def test_name_check_catches_planted_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    lower_name = OLD_NAME + b"-lower.txt"
    upper_name = OLD_NAME.upper() + b"-upper.txt"
    mixed_contents = b"a-mixed-contents.txt"
    binary_contents = b"z-binary-contents.bin"
    clean = b"clean.txt"

    for path, contents in (
        (lower_name, b"clean\n"),
        (upper_name, b"clean\n"),
        (mixed_contents, b"before " + OLD_NAME.capitalize() + b" after\n"),
        # Invalid UTF-8, so a check that decodes text would raise or miss this file.
        (binary_contents, b"\xff\xfe\x00" + OLD_NAME + b"\xff"),
        (clean, b"plyrix\n"),
    ):
        with open(path, "wb") as handle:
            handle.write(contents)

    found = find_old_name([binary_contents, clean, lower_name, mixed_contents, upper_name])

    # Byte order puts uppercase before lowercase, which a case-folded sort would not.
    assert found == [upper_name, mixed_contents, lower_name, binary_contents]


def test_name_check_refuses_empty_list() -> None:
    with pytest.raises(ValueError, match="empty file list"):
        find_old_name([])


def test_name_help() -> None:
    result = subprocess.run(
        [str(Path(sys.executable).parent / "plyrix"), "--help"],
        capture_output=True,
        text=True,
        env={**os.environ, "COLUMNS": "200"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "plyrix" in result.stdout
    assert "lyrics" in result.stdout


def test_public_wording() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for word in ("lyrics", "tested on ZFS on Linux only", "copied", ".lrc", ".txt"):
        assert word in readme, f"README.md lacks {word!r}"
    assert unsafe_wording(readme) == []

    approved = "The code that reads audio cannot write, and the only writer creates .lrc and .txt."
    assert unsafe_wording(approved) == []

    for claim in (
        "Writing audio is impossible.",
        "It is IMPOSSIBLE for plyrix to change your audio.",
        "plyrix cannot write audio files.",
        "plyrix will never touch your audio.",
        "Audio files can never be modified.",
        "The guard is a sandbox around the scan.",
        "Each run is sandboxed.",
    ):
        assert unsafe_wording(claim), f"not flagged: {claim}"


def _run_tool(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )


def test_ruff_clean() -> None:
    result = _run_tool("ruff", "check", "src", "tests")
    assert result.returncode == 0, result.stdout + result.stderr


def test_ruff_format_clean() -> None:
    result = _run_tool("ruff", "format", "--check", "src", "tests")
    assert result.returncode == 0, result.stdout + result.stderr


def test_mypy_strict() -> None:
    result = _run_tool("mypy", "src", "tests")
    assert result.returncode == 0, result.stdout + result.stderr


# The AST check. Ruff can't see instance methods or a bare open, and its per-file ignores turn the
# whole rule off for a file, so the per-module rules live here.
OS_WRITE = {
    "open",
    "fdopen",
    "link",
    "symlink",
    "rename",
    "renames",
    "replace",
    "remove",
    "unlink",
    "truncate",
    "ftruncate",
    "utime",
    "chmod",
    "fchmod",
    "lchmod",
    "chown",
    "fchown",
    "lchown",
    "mkdir",
    "makedirs",
    "rmdir",
    "removedirs",
    "setxattr",
    "removexattr",
    "mkfifo",
    "mknod",
    "write",
    "pwrite",
    "writev",
    "pwritev",
    "sendfile",
    "copy_file_range",
    "splice",
    "posix_fallocate",
}
# Starting a process is not a write that sidecars needs, so nothing is exempt from these.
OS_SPAWN = {
    "system",
    "popen",
    "fork",
    "forkpty",
    "posix_spawn",
    "posix_spawnp",
    "execl",
    "execle",
    "execlp",
    "execlpe",
    "execv",
    "execve",
    "execvp",
    "execvpe",
    "spawnl",
    "spawnle",
    "spawnlp",
    "spawnlpe",
    "spawnv",
    "spawnve",
    "spawnvp",
    "spawnvpe",
}
BANNED_MODULES = {
    "shutil",
    "tempfile",
    "subprocess",
    "mmap",
    "ctypes",
    "fileinput",
    "pty",
    "sqlite3",
}
TAG_WRITERS = {"mutagen", "mediafile", "beets", "beetsplug"}
PATH_WRITE_METHODS = {
    "write_text",
    "write_bytes",
    "touch",
    "unlink",
    "rename",
    "symlink_to",
    "hardlink_to",
    "chmod",
    "lchmod",
    "rmdir",
    "mkdir",
    "copy",
    "copy_into",
    "move",
    "move_into",
}
# Everything else public on Path, from Python 3.12 to 3.14. replace writes too, but it can't be told
# from str.replace, so the guard is what catches it. open has its own rule.
PATH_NOT_FLAGGED = frozenset(
    (
        "absolute anchor as_posix as_uri cwd drive exists expanduser from_uri full_match glob "
        "group home info is_absolute is_block_device is_char_device is_dir is_fifo is_file "
        "is_junction is_mount is_relative_to is_reserved is_socket is_symlink iterdir joinpath "
        "lstat match name open owner parent parents parser parts read_bytes read_text readlink "
        "relative_to replace resolve rglob root samefile stat stem suffix suffixes walk with_name "
        "with_segments with_stem with_suffix"
    ).split()
)
BANNED_CALLS = {"open", "exec", "eval", "__import__", "FileIO", "FileHandler"}
WRITER_CLASSES = {"FileIO", "FileHandler"}
TAGS_BANNED_ATTRIBUTES = {"other", "as_dict", "comment"}

_OS_WRITE_ALLOWED_IN = {"plyrix.sidecars"}
_MODULES_ALLOWED_IN = {"plyrix.state.db": {"sqlite3"}, "plyrix.state.lrclib_db": {"sqlite3"}}
_METHODS_ALLOWED_IN = {
    "plyrix.state.db": {"mkdir"},
    "plyrix.report": {"open", "unlink"},
    "plyrix.spool": {"open", "unlink", "mkdir"},
}
# A module whose os.open may take one named read-only flags constant, and nothing else. A test in
# that module's own tests checks the constant holds no write flag.
_OS_READ_OPEN_ALLOWED_IN = {"plyrix.spool": "_READ_FLAGS"}
_TAGS_MODULE = "plyrix.tags"
# The only standard library each module may import, so anything new fails until it is reviewed
# and listed here. That covers the many back doors a list of banned names can't keep up with,
# such as posix, _io, importlib, multiprocessing and logging. A module not listed may import none.
STDLIB_ALLOWED = {
    "plyrix.cli": {"dataclasses", "os", "pathlib", "typing"},
    "plyrix.config": {"collections", "os", "pathlib", "re", "sys", "tomllib", "typing"},
    "plyrix.dbupdate": {"dataclasses", "datetime", "os", "urllib"},
    "plyrix.doctor": {"dataclasses", "enum", "os", "pathlib", "re"},
    "plyrix.domain": {"collections", "dataclasses", "enum", "hashlib", "json"},
    "plyrix.dumps": {"dataclasses", "datetime", "json", "zlib"},
    "plyrix.guard": {"collections", "os", "re", "sys", "typing", "urllib"},
    "plyrix.hook": {"collections", "dataclasses", "enum", "os", "pathlib", "time"},
    "plyrix.lidarr": {"collections", "dataclasses", "os"},
    "plyrix.lrc": {"re"},
    "plyrix.lrclib": {"enum", "unicodedata"},
    "plyrix.matching": {"collections", "dataclasses", "hashlib", "math", "re", "unicodedata"},
    "plyrix.plex": {"collections", "dataclasses", "enum", "json", "os"},
    "plyrix.pipeline": {
        "collections",
        "dataclasses",
        "hashlib",
        "json",
        "math",
        "os",
        "sys",
        "time",
    },
    "plyrix.report": {"collections", "datetime", "json", "pathlib", "secrets"},
    "plyrix.refresh": {"collections", "dataclasses", "os", "time"},
    "plyrix.spool": {"collections", "datetime", "errno", "os", "pathlib", "secrets", "stat"},
    "plyrix.sidecars": {
        "enum",
        "errno",
        "hashlib",
        "os",
        "re",
        "secrets",
        "stat",
    },
    "plyrix.state.db": {"datetime", "os", "pathlib", "sqlite3", "typing", "urllib"},
    "plyrix.state.lrclib_db": {
        "collections",
        "datetime",
        "hashlib",
        "json",
        "os",
        "sqlite3",
        "typing",
        "urllib",
    },
    "plyrix.tags": {"errno", "io", "typing"},
    "plyrix.walker": {"collections", "dataclasses", "enum", "errno", "fnmatch", "os"},
}


def _import_problem(name: str, module: str) -> bool:
    top = name.split(".")[0]
    if top in TAG_WRITERS or (top == "tinytag" and module != _TAGS_MODULE):
        return True
    if top in BANNED_MODULES and top not in _MODULES_ALLOWED_IN.get(module, set()):
        return True
    return top in sys.stdlib_module_names and top not in STDLIB_ALLOWED.get(module, set())


def _from_import_problems(node: ast.ImportFrom, module: str) -> list[str]:
    source = node.module or ""
    found = []
    if node.level == 0 and _import_problem(source, module):
        found.append(f"from {source} import")
    for alias in node.names:
        writes = alias.name == "*" or alias.name in OS_WRITE
        if source == "os" and (
            alias.name in OS_SPAWN or (writes and module not in _OS_WRITE_ALLOWED_IN)
        ):
            found.append(f"from os import {alias.name}")
        if source in ("io", "builtins") and alias.name in ("*", "open"):
            found.append(f"from {source} import {alias.name}")
    return found


def _attribute_problems(node: ast.Attribute, module: str, os_names: set[str]) -> list[str]:
    base = node.value.id if isinstance(node.value, ast.Name) else None
    found = []
    if base in os_names:
        if node.attr in OS_SPAWN or (node.attr in OS_WRITE and module not in _OS_WRITE_ALLOWED_IN):
            found.append(f"os.{node.attr}")
        return found
    if node.attr in WRITER_CLASSES:
        found.append(f".{node.attr}")
    if node.attr in PATH_WRITE_METHODS and node.attr not in _METHODS_ALLOWED_IN.get(module, set()):
        found.append(f".{node.attr}")
    if node.attr == "open" and base != "TinyTag":
        if "open" not in _METHODS_ALLOWED_IN.get(module, set()):
            found.append(".open")
    if module == _TAGS_MODULE and node.attr in TAGS_BANNED_ATTRIBUTES:
        found.append(f".{node.attr}")
    return found


def _call_problems(node: ast.Call, module: str) -> list[str]:
    if not isinstance(node.func, ast.Name):
        return []
    if node.func.id in BANNED_CALLS:
        return [f"{node.func.id}()"]
    banned = OS_WRITE | OS_SPAWN | PATH_WRITE_METHODS | WRITER_CLASSES
    if module == _TAGS_MODULE:
        banned = banned | TAGS_BANNED_ATTRIBUTES
    if node.func.id in ("getattr", "setattr") and len(node.args) >= 2:
        name = node.args[1]
        if isinstance(name, ast.Constant) and name.value in banned:
            return [f'{node.func.id}("{name.value}")']
    return []


def _read_only_opens(tree: ast.AST, module: str, os_names: set[str]) -> set[int]:
    """Return the os.open attributes called with exactly the module's read-only flags constant."""
    flags = _OS_READ_OPEN_ALLOWED_IN.get(module)
    if flags is None:
        return set()
    allowed = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        func = node.func
        base = func.value.id if isinstance(func.value, ast.Name) else None
        if (
            base in os_names
            and func.attr == "open"
            and len(node.args) == 2
            and not node.keywords
            and isinstance(node.args[1], ast.Name)
            and node.args[1].id == flags
        ):
            allowed.add(id(func))
    return allowed


def find_violations(source: str, module: str) -> list[str]:
    """Return each way this check can see a module's source writing or spawning, by dotted name.

    Standard-library imports are an allow-list, and the names under it a deny-list. sidecars may
    use OS_WRITE, and state.db may use sqlite3 and .mkdir. Nothing else is exempt.

    A deny-list covers the names it holds and no others, so this sees exactly: os.* in OS_WRITE
    and OS_SPAWN, the methods in PATH_WRITE_METHODS, the calls in BANNED_CALLS and the imports in
    BANNED_MODULES. It doesn't see: p.replace("x") on a Path, which can't be told from str.replace
    and is left to the guard; a name Python adds to os later; a name reached by an aliased object
    or a computed getattr; or anything run from native code. A new public Path name fails
    test_every_public_path_name_is_classified until someone classifies it. Attribute names are
    matched on any object, so x.copy() on a dict is flagged as well, and dict(x) is the way round.
    """
    tree = ast.parse(source)
    os_names = {
        alias.asname or "os"
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "os" or (alias.name.startswith("os.") and alias.asname is None)
    }
    read_opens = _read_only_opens(tree, module, os_names)
    found: list[str] = []
    for node in ast.walk(tree):
        if id(node) in read_opens:
            continue
        if isinstance(node, ast.Import):
            found += [f"import {a.name}" for a in node.names if _import_problem(a.name, module)]
        elif isinstance(node, ast.ImportFrom):
            found += _from_import_problems(node, module)
        elif isinstance(node, ast.Attribute):
            found += _attribute_problems(node, module, os_names)
        elif isinstance(node, ast.Call):
            found += _call_problems(node, module)
    return found


def _src_modules() -> dict[str, str]:
    """Return each module under src/plyrix, by dotted name, with its source."""
    modules = {}
    for path in sorted((ROOT / "src" / "plyrix").rglob("*.py")):
        parts = path.relative_to(ROOT / "src").with_suffix("").parts
        name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        modules[name] = path.read_text(encoding="utf-8")
    return modules


_OS_WRITE_NAMES = (
    "open fdopen link symlink rename renames replace remove unlink truncate ftruncate utime chmod "
    "fchmod lchmod chown fchown lchown mkdir makedirs rmdir removedirs setxattr removexattr mkfifo "
    "mknod write pwrite writev pwritev sendfile copy_file_range splice posix_fallocate"
).split()
_OS_SPAWN_NAMES = (
    "system popen fork forkpty posix_spawn posix_spawnp execl execle execlp execlpe execv execve "
    "execvp execvpe spawnl spawnle spawnlp spawnlpe spawnv spawnve spawnvp spawnvpe"
).split()
_PATH_METHOD_NAMES = (
    "write_text write_bytes touch unlink rename symlink_to hardlink_to chmod lchmod rmdir mkdir "
    "copy copy_into move move_into"
).split()
_BANNED_MODULE_NAMES = "shutil tempfile subprocess mmap ctypes fileinput pty sqlite3".split()

_BAD_SAMPLES = [
    # The rows of the layer table in RESEARCH that the AST column catches.
    ("import os as o\no.replace('a', 'b')", "plyrix.walker"),
    ("from os import replace as r\nr('a', 'b')", "plyrix.walker"),
    ("open('a', 'wb')", "plyrix.walker"),
    ("Path('x').write_text('y')", "plyrix.walker"),
    ("Path('x').unlink()", "plyrix.walker"),
    ("p.touch()", "plyrix.walker"),
    ("import os\ngetattr(os, 'link')('a', 'b')", "plyrix.walker"),
    # Each remaining form, so a later edit can't quietly weaken one.
    ("exec('x')", "plyrix.walker"),
    ("eval('x')", "plyrix.walker"),
    ("__import__('shutil')", "plyrix.walker"),
    ("setattr(x, 'unlink', y)", "plyrix.walker"),
    ("Path('x').open('wb')", "plyrix.walker"),
    ("import io\nio.open('a')", "plyrix.walker"),
    ("import builtins\nbuiltins.open('a')", "plyrix.walker"),
    ("from io import open as o", "plyrix.walker"),
    ("from os import *", "plyrix.walker"),
    ("import os\nwriter = os.link", "plyrix.walker"),
    ("import os.path\nos.link('a', 'b')", "plyrix.walker"),
    ("from shutil import copyfile", "plyrix.walker"),
    ("import shutil.something", "plyrix.walker"),
    # Each module's exemption reaches only what it names.
    ("open('a', 'wb')", "plyrix.sidecars"),
    ("Path('x').unlink()", "plyrix.sidecars"),
    ("import shutil", "plyrix.sidecars"),
    ("import os\ngetattr(os, 'link')('a', 'b')", "plyrix.sidecars"),
    ("import os\nos.link('a', 'b')", "plyrix.state.db"),
    ("Path('x').unlink()", "plyrix.state.db"),
    ("Path('x').touch()", "plyrix.state.db"),
    ("import shutil", "plyrix.state.db"),
    ("import sqlite3", "plyrix.walker"),
    ("Path('x').mkdir()", "plyrix.walker"),
    ("import os\nos.link('a', 'b')", "plyrix.sidecars2"),
    ("import sqlite3", "plyrix.state"),
    # The guard and the read side have no exemption at all.
    ("import os\nos.replace('a', 'b')", "plyrix.guard"),
    ("import os\nos.replace('a', 'b')", "plyrix.tags"),
    # Modules that a list of banned names missed, which the allow-list fails by default.
    ("import io\nio.FileIO('a', 'w')", "plyrix.walker"),
    ("from io import FileIO", "plyrix.walker"),
    ("import _io\n_io.FileIO('a', 'w')", "plyrix.walker"),
    ("from _io import FileIO", "plyrix.walker"),
    ("import posix\nposix.remove('a')", "plyrix.walker"),
    ("from posix import remove", "plyrix.walker"),
    ("import importlib\nimportlib.import_module('shutil')", "plyrix.walker"),
    ("from importlib import import_module", "plyrix.walker"),
    ("import multiprocessing", "plyrix.walker"),
    ("import concurrent.futures", "plyrix.walker"),
    ("import logging\nlogging.FileHandler('a')", "plyrix.walker"),
    ("from logging import FileHandler", "plyrix.walker"),
    ("import pathlib", "plyrix.walker"),
    ("import hashlib", "plyrix.walker"),
    ("import os", "plyrix.not_listed"),
    # A class that opens a file for writing, whichever name reached it.
    ("x.FileIO('a', 'w')", "plyrix.walker"),
    ("FileIO('a', 'w')", "plyrix.walker"),
    ("x.FileHandler('a')", "plyrix.walker"),
    ("FileHandler('a')", "plyrix.walker"),
    ("getattr(x, 'FileIO')", "plyrix.walker"),
    # The writer may write, and may still never start a process.
    *((f"import os\nos.{name}('a')", "plyrix.sidecars") for name in _OS_SPAWN_NAMES),
    ("from os import fork", "plyrix.sidecars"),
    ("import subprocess", "plyrix.sidecars"),
    *((f"import os\nos.{name}('a')", "plyrix.pipeline") for name in _OS_WRITE_NAMES),
    *((f"import os\nos.{name}('a')", "plyrix.pipeline") for name in _OS_SPAWN_NAMES),
    *((f"p.{name}()", "plyrix.pipeline") for name in _PATH_METHOD_NAMES),
    ("Path('x').move('y')", "plyrix.cli"),
    ("Path('x').copy('y')", "plyrix.config"),
    ("Path('x').copy_into('y')", "plyrix.state.db"),
    ("Path('x').move_into('y')", "plyrix.state.db"),
    ("getattr(p, 'move')('y')", "plyrix.cli"),
    *((f"import {name}", "plyrix.pipeline") for name in _BANNED_MODULE_NAMES),
    *((f"import {name}", "plyrix.cli") for name in ("mutagen", "mediafile", "beets", "tinytag")),
    ("import beetsplug.lyrics", "plyrix.tags"),
    ("from mutagen.flac import FLAC", "plyrix.tags"),
]


@pytest.mark.parametrize(("source", "module"), _BAD_SAMPLES)
def test_ast_catches_bad_samples(source: str, module: str) -> None:
    assert find_violations(source, module), source


@pytest.mark.parametrize(
    "source",
    [
        "'abc'.replace('a', 'b')",
        "p.read_bytes()",
        "p.exists()",
        "import os as o\no.scandir('a')",
        "import os as o\no.path.join('a', 'b')",
        "import os\nos.fsencode('a')",
        "import fnmatch\nfnmatch.fnmatchcase('a', 'b')",
        "getattr(x, 'name')",
    ],
)
def test_ast_no_false_positives(source: str) -> None:
    assert find_violations(source, "plyrix.walker") == []


@pytest.mark.parametrize(
    ("source", "module"),
    [
        ("import os\nos.link('a', 'b')", "plyrix.sidecars"),
        ("from os import replace", "plyrix.sidecars"),
        ("import sqlite3", "plyrix.state.db"),
        ("Path('x').mkdir()", "plyrix.state.db"),
        ("Path('x').open('x')", "plyrix.report"),
        ("Path('x').unlink(missing_ok=True)", "plyrix.report"),
        ("Path('x').open('x')", "plyrix.spool"),
        ("Path('x').unlink(missing_ok=True)", "plyrix.spool"),
        ("Path('x').mkdir()", "plyrix.spool"),
        ("import os\nos.open('x', _READ_FLAGS)", "plyrix.spool"),
        ("from tinytag import TinyTag", "plyrix.tags"),
        ("TinyTag.open('a')", "plyrix.tags"),
        ("import hashlib\nhashlib.sha256(b'')", "plyrix.pipeline"),
        ("import os\nos.writev(1, [])", "plyrix.sidecars"),
    ],
)
def test_ast_allows_exactly_what_each_module_is_exempt_for(source: str, module: str) -> None:
    assert find_violations(source, module) == []


@pytest.mark.parametrize(
    "source",
    [
        "Path('x').write_text('y')",
        "Path('x').write_bytes(b'y')",
        "Path('x').rename('y')",
        "open('x', 'w')",
        "import os\nos.unlink('x')",
    ],
)
def test_ast_report_may_write_a_report_and_nothing_else(source: str) -> None:
    assert find_violations(source, "plyrix.report") != []


@pytest.mark.parametrize(
    "source",
    [
        "Path('x').write_text('y')",
        "Path('x').rename('y')",
        "import os\nos.unlink('x')",
        "import os\nos.open('x', os.O_WRONLY)",
        "import os\nos.open('x', _READ_FLAGS, 0o600)",
        "import os\nos.open('x', flags=_READ_FLAGS)",
        "import os\nos.fdopen(3, 'wb')",
    ],
)
def test_ast_spool_may_write_an_event_and_nothing_else(source: str) -> None:
    assert find_violations(source, "plyrix.spool") != []


def test_ast_only_report_and_spool_are_exempt_for_open() -> None:
    assert find_violations("Path('x').open('x')", "plyrix.pipeline") != []
    assert find_violations("Path('x').open('x')", "plyrix.cli") != []
    assert find_violations("import os\nos.open('x', _READ_FLAGS)", "plyrix.pipeline") != []


def test_every_public_path_name_is_classified() -> None:
    public = {name for name in dir(Path) if not name.startswith("_")}
    assert public - PATH_WRITE_METHODS - PATH_NOT_FLAGGED == set()
    assert not PATH_WRITE_METHODS & PATH_NOT_FLAGGED


def test_ast_src_clean() -> None:
    modules = _src_modules()
    assert {"plyrix.sidecars", "plyrix.tags", "plyrix.guard", "plyrix.state.db"} <= set(modules)
    found = {name: find_violations(source, name) for name, source in modules.items()}
    assert {name: problems for name, problems in found.items() if problems} == {}


def test_the_allow_list_names_only_what_each_module_imports() -> None:
    # An entry nothing imports would let a later import in unreviewed.
    used: dict[str, set[str]] = {}
    for name, source in _src_modules().items():
        tops = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                tops |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                tops.add((node.module or "").split(".")[0])
        stdlib = tops & sys.stdlib_module_names
        if stdlib:
            used[name] = stdlib
    assert used == STDLIB_ALLOWED


@pytest.mark.parametrize(
    "source",
    ["found.other", "found.as_dict()", "found.comment", "getattr(found, 'other')"],
)
def test_tags_cannot_reach_a_field_holding_embedded_lyrics(source: str) -> None:
    assert find_violations(source, "plyrix.tags")
    # Only tags.py holds a tinytag object, so the same word elsewhere is left alone.
    assert find_violations(source, "plyrix.walker") == []


def test_tags_imports_tinytag_and_nothing_else_does() -> None:
    importers = sorted(
        name
        for name, source in _src_modules().items()
        if any(
            (isinstance(node, ast.ImportFrom) and (node.module or "").startswith("tinytag"))
            or (
                isinstance(node, ast.Import)
                and any(alias.name.startswith("tinytag") for alias in node.names)
            )
            for node in ast.walk(ast.parse(source))
        )
    )
    assert importers == [_TAGS_MODULE]


def _ruff_codes(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--config",
            str(ROOT / "pyproject.toml"),
            "--output-format=concise",
            str(path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "source",
    [
        "import mutagen\n",
        "from mediafile import MediaFile\n",
        "import beets\n",
        "import beetsplug\n",
        "import tinytag\n",
        "import shutil\n",
        "import tempfile\n",
        "import subprocess\n",
        "import mmap\n",
        "import ctypes\n",
        "import fileinput\n",
        "import pty\n",
        "import builtins\n\nbuiltins.open('a')\n",
        "import io\n\nio.open('a')\n",
        *(
            f"import os as o\n\no.{name}\n"
            for name in (
                "open fdopen link symlink rename renames replace remove unlink truncate ftruncate "
                "utime chmod chown rmdir removedirs write pwrite mkdir makedirs fchmod lchmod "
                "fchown lchown setxattr removexattr mkfifo mknod splice"
            ).split()
        ),
        *(
            f"import os as o\n\no.{name}\n"
            for name in (
                "writev pwritev sendfile copy_file_range posix_fallocate fork forkpty posix_spawn "
                "posix_spawnp system popen execl execle execlp execlpe execv execve execvp execvpe "
                "spawnl spawnle spawnlp spawnlpe spawnv spawnve spawnvp spawnvpe"
            ).split()
        ),
        "from os import replace as r\n",
        "import io\n\nio.FileIO('a')\n",
        "from io import FileIO\n",
        "import _io\n",
        "import posix\n",
        "import importlib\n",
        "import multiprocessing\n",
        "import logging\n\nlogging.FileHandler('a')\n",
    ],
)
def test_ruff_catches_banned_import(tmp_path: Path, source: str) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(source, encoding="utf-8")
    result = _ruff_codes(sample)
    # An unused import also fails Ruff, so the code itself is what proves the ban.
    assert result.returncode != 0
    assert "TID251" in result.stdout, result.stdout


def test_ruff_holds_every_name_the_ast_check_bans() -> None:
    """Ruff is earlier feedback for the same names, so it mustn't drift from the check."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    banned = set(config["tool"]["ruff"]["lint"]["flake8-tidy-imports"]["banned-api"])
    wanted = {f"os.{name}" for name in OS_WRITE | OS_SPAWN} | (BANNED_MODULES - {"sqlite3"})
    assert wanted - banned == set()


def test_ruff_sample_with_no_banned_import_passes(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text("import json\n\njson.dumps({})\n", encoding="utf-8")
    result = _ruff_codes(sample)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    ("filename", "banned"),
    [
        ("src/plyrix/walker.py", True),
        ("src/plyrix/guard.py", True),
        ("src/plyrix/state/db.py", True),
        ("src/plyrix/pipeline.py", True),
        ("src/plyrix/tags.py", False),
        ("src/plyrix/sidecars.py", False),
        ("tests/test_x.py", False),
        ("scripts/x.py", False),
    ],
)
def test_ruff_ignores_the_ban_only_where_listed(filename: str, banned: bool) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--select",
            "TID251",
            "--output-format=concise",
            "--stdin-filename",
            filename,
            "-",
        ],
        input="import tinytag\n",
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ("TID251" in result.stdout) is banned, result.stdout


def _lint_imports(cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(Path(sys.executable).parent / "lint-imports"), "--no-cache"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def test_import_contracts() -> None:
    result = _lint_imports(ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "8 kept, 0 broken" in result.stdout, result.stdout


@pytest.mark.parametrize(
    ("module", "line", "contract"),
    [
        ("cli", "import mutagen", "no tag-writing library anywhere"),
        ("walker", "import plyrix.sidecars", "only pipeline may import sidecars"),
        ("guard", "import plyrix.sidecars", "only pipeline may import sidecars"),
        ("tags", "import plyrix.sidecars", "tags and sidecars never see each other"),
        ("sidecars", "import plyrix.tags", "tags and sidecars never see each other"),
        ("walker", "import tinytag", "tinytag only in tags"),
        ("guard", "import tinytag", "tinytag only in tags"),
        ("domain", "import os", "domain does no I/O"),
        ("matching", "import os", "domain does no I/O"),
        ("lrc", "import os", "domain does no I/O"),
        ("plex", "import plyrix.sidecars", "plex side never reaches the tag reader or the writer"),
        ("refresh", "import plyrix.tags", "plex side never reaches the tag reader or the writer"),
        ("plex", "import plyrix.state.db", "the plex client is HTTP only"),
        ("doctor", "import plyrix.walker", "plex side never reaches the tag reader or the writer"),
        ("doctor", "import plyrix.sidecars", "only pipeline may import sidecars"),
        ("doctor", "import tinytag", "tinytag only in tags"),
        ("dbupdate", "import plyrix.walker", "lrclib update side never reaches the library"),
        ("dumps", "import plyrix.pipeline", "lrclib update side never reaches the library"),
        ("dbupdate", "import plyrix.tags", "lrclib update side never reaches the library"),
        ("dumps", "import tinytag", "tinytag only in tags"),
        ("dbupdate", "import plyrix.sidecars", "only pipeline may import sidecars"),
    ],
)
def test_import_contracts_catch_a_planted_import(
    tmp_path: Path, module: str, line: str, contract: str
) -> None:
    shutil.copytree(ROOT / "src" / "plyrix", tmp_path / "plyrix", ignore=_NO_CACHES)
    shutil.copy(ROOT / "pyproject.toml", tmp_path)
    planted = tmp_path / "plyrix" / f"{module}.py"
    planted.write_text(f"{line}\n{planted.read_text(encoding='utf-8')}", encoding="utf-8")
    # lint-imports puts its working directory first on the path, so it reads this copy.
    result = _lint_imports(tmp_path, {"PYTHONPATH": str(tmp_path)})
    assert result.returncode != 0, result.stdout
    assert f"{contract} BROKEN" in result.stdout, result.stdout


def tag_writers_in_lock(text: str) -> list[str]:
    """Return the tag-writing packages a uv.lock's text pins, in sorted order."""
    packages = tomllib.loads(text).get("package", [])
    return sorted(package["name"] for package in packages if package["name"] in TAG_WRITERS)


def test_lock_has_no_tag_writer() -> None:
    text = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert tomllib.loads(text)["package"], "the lockfile lists no packages, so this checks nothing"
    assert tag_writers_in_lock(text) == []


@pytest.mark.parametrize("name", sorted(TAG_WRITERS))
def test_lock_check_finds_a_tag_writer_when_one_is_there(name: str) -> None:
    sample = f'[[package]]\nname = "tinytag"\n\n[[package]]\nname = "{name}"\nversion = "1.0"\n'
    assert tag_writers_in_lock(sample) == [name]


@pytest.mark.parametrize(
    ("function", "parameters"),
    [
        (sidecars.publish, ["track", "kind", "data", "log", "method"]),
        (sidecars.remove_owned, ["track", "kind", "expected_sha256"]),
        (sidecars.replace_owned, ["track", "kind", "data", "expected_sha256", "log"]),
    ],
)
def test_sidecars_api_takes_no_path(function: Callable[..., object], parameters: list[str]) -> None:
    assert list(inspect.signature(function).parameters) == parameters


def find_absolute_paths(source: str) -> list[str]:
    """Return each str or bytes constant that starts with a slash, apart from /proc/self/fd.

    The paths in _SYSTEM_PATHS are left out too, by exact match, so nothing under them is.

    A constant of exactly one slash is a separator, and a literal part of an f-string after a
    formatted value joins onto something else, as in f"{base}/get". Neither is an absolute path.
    """
    found: list[str] = []

    def check(value: object) -> None:
        text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
        if not isinstance(text, str) or not text.startswith("/") or text == "/":
            return
        if text == _PROC_FDS or text.startswith(_PROC_FDS + "/"):
            return
        if text in _SYSTEM_PATHS:
            return
        found.append(text)

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.JoinedStr):
            for index, part in enumerate(node.values):
                if isinstance(part, ast.Constant):
                    if index == 0:
                        check(part.value)
                elif isinstance(part, ast.FormattedValue):
                    # The format spec is formatting, so only the expression can hold a path.
                    visit(part.value)
            return
        if isinstance(node, ast.Constant):
            check(node.value)
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(ast.parse(source))
    return found


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ('"/srv/music"', True),
        ('b"/srv/music"', True),
        ('f"/srv/{x}"', True),
        ('"/get"', True),
        ('"//"', True),
        ('"/proc/self/fdx"', True),
        ('"/proc/self/environ"', True),
        ("f\"{g('/srv')}/x\"", True),
        ("x = ['/srv/music']", True),
        ("def f(root='/srv/music'): ...", True),
        ('f"{base}/get"', False),
        ('"/"', False),
        ('b"/"', False),
        ('f"/proc/self/fd/{p}"', False),
        ('"/proc/self/fd"', False),
        ('"/run/secrets"', False),
        ('"/run/secretsx"', True),
        ('"/run/secrets/x"', True),
        ('f"/run/secrets/{x}"', True),
        ('"/proc/self/mountinfo"', False),
        ('"/proc/self/mountinfox"', True),
        ('"/proc/self/mountinfo/x"', True),
        ('"/proc/self/mounts"', True),
        ('"http://host/api"', False),
        ('"v/a"', False),
        ('f"{x:/>4}"', False),
        ("1", False),
        ("None", False),
    ],
)
def test_absolute_paths_table(source: str, flagged: bool) -> None:
    assert bool(find_absolute_paths(source)) is flagged, source


def test_no_absolute_paths_in_src() -> None:
    modules = _src_modules()
    assert "plyrix.lrclib" in modules
    found = {name: find_absolute_paths(source) for name, source in modules.items()}
    assert {name: paths for name, paths in found.items() if paths} == {}


def test_guard_described_as_tripwire() -> None:
    docstring = ast.get_docstring(ast.parse(_src_modules()["plyrix.guard"]))
    assert docstring is not None
    assert "tripwire" in docstring
    assert unsafe_wording(docstring) == []
