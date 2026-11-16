"""Config layers, one-line errors and the run path rule, mostly through the real console script."""

import ast
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError

from conftest import (
    CANARY,
    FIXTURES,
    RunCli,
    StubLrclib,
    StubPlex,
    parse_counts,
    snap,
    state_db_path,
)
from plyrix import config

DEAD_URL = "http://127.0.0.1:9/api"
README = Path(__file__).parent.parent / "README.md"
SRC = Path(__file__).parent.parent / "src"
# Prints what the CLI handed to the pipeline, so these tests read the settings themselves.
CHILD = """
import json, os, sys
from plyrix import cli, guard, pipeline

def report(settings, start):
    print(json.dumps({
        "settings": settings.model_dump(mode="json"),
        "start": os.fsdecode(start),
        "roots": [os.fsdecode(root) for root in guard._roots],
    }))
    return pipeline.Counts()

pipeline.run = report
cli.app(["run", *sys.argv[1:]])
"""


def copy_track(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return Path(shutil.copyfile(FIXTURES / "t.flac", directory / "t.flac"))


def write_toml(path: Path, **values: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{key} = {json.dumps(value)}\n" for key, value in values.items()))
    return path


def default_toml(tmp_path: Path) -> Path:
    """Return where run_cli's environment makes plyrix look for its config file."""
    return tmp_path / "config" / "plyrix" / "config.toml"


def sidecars(top: Path) -> list[Path]:
    return [path for path in top.rglob("*") if path.suffix in (".lrc", ".txt")]


def tree(top: Path) -> list[Path]:
    return sorted(top.rglob("*"))


def assert_refused(proc: "subprocess.CompletedProcess[bytes]", *names: str) -> None:
    """Assert exit 2 with one stderr line naming each setting, and nothing else printed."""
    assert proc.returncode == 2, proc.stderr
    lines = proc.stderr.splitlines()
    assert len(lines) == 1, proc.stderr
    assert b"Traceback" not in proc.stderr
    assert proc.stdout == b""
    for name in names:
        assert name.encode() in lines[0]


def ledger_paths(tmp_path: Path) -> list[bytes]:
    with sqlite3.connect(state_db_path(tmp_path)) as conn:
        return [row[0] for row in conn.execute("SELECT path FROM sidecars ORDER BY path")]


@pytest.fixture
def clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the caller's settings out of a test that builds Settings in this process."""
    for name in list(os.environ):
        if name.upper().startswith("PLYRIX_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


def child_process(
    tmp_path: Path, *args: str, env: dict[str, str] | None = None
) -> "subprocess.CompletedProcess[bytes]":
    """Run the real CLI with the pipeline replaced, so nothing can reach a server."""
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("PLYRIX_")}
    return subprocess.run(
        [sys.executable, "-c", CHILD, *args],
        env={
            **inherited,
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            **(env or {}),
        },
        capture_output=True,
        check=False,
        timeout=120,
    )


@pytest.fixture
def run_child(tmp_path: Path) -> Callable[..., dict[str, object]]:
    """Run the real CLI with the pipeline replaced, and return what the pipeline was given."""

    def run(*args: str, env: dict[str, str] | None = None) -> dict[str, object]:
        proc = child_process(tmp_path, *args, env=env)
        assert proc.returncode == 0, proc.stderr
        report: dict[str, object] = json.loads(proc.stdout.splitlines()[0])
        return report

    return run


# Required settings and defaults


def test_required_fields(tmp_path: Path, run_cli: RunCli) -> None:
    proc = run_cli(["run"])

    assert_refused(proc, "music_root", "lrclib_url")
    assert not state_db_path(tmp_path).exists()


def test_nothing_is_hardcoded(clean_env: Path) -> None:
    settings = config.Settings(music_root=clean_env / "lib", lrclib_url=DEAD_URL)

    assert settings.excludes == []
    assert settings.verbose is False
    assert settings.state_db == clean_env / "state" / "plyrix" / "state.db"
    assert config.Settings.model_fields["music_root"].is_required()
    assert config.Settings.model_fields["lrclib_url"].is_required()


def test_a_folder_named_like_the_prototype_exclude_is_walked(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib / "old_music")

    proc = run_cli(["run", lib], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").exists()


def test_help_lists_every_flag(run_cli: RunCli) -> None:
    proc = run_cli(["run", "--help"], env={"COLUMNS": "200"})

    assert proc.returncode == 0, proc.stderr
    for flag in (
        b"--config",
        b"--lrclib-url",
        b"--exclude",
        b"--state-db",
        b"--plain",
        b"--verbose",
    ):
        assert flag in proc.stdout


# Precedence


def test_precedence(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flag, env, secret, toml = (f"http://{name}.invalid/api" for name in "abcd")
    write_toml(default_toml(clean_env), music_root="/srv/music", lrclib_url=toml)
    secrets = clean_env / "secrets"
    secrets.mkdir()
    secret_file = secrets / "plyrix_lrclib_url"
    secret_file.write_text(secret + "\n")
    monkeypatch.setenv("PLYRIX_LRCLIB_URL", env)

    def url(**init: str) -> str:
        return str(config.Settings(_secrets_dir=secrets, **init).lrclib_url)  # type: ignore[arg-type]

    assert url(lrclib_url=flag) == flag
    assert url() == env
    monkeypatch.delenv("PLYRIX_LRCLIB_URL")
    assert url() == secret
    secret_file.unlink()
    assert url() == toml


@pytest.mark.parametrize("layer", ["flag", "env", "toml"])
def test_precedence_through_the_cli(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, layer: str
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    write_toml(
        default_toml(tmp_path), lrclib_url=stub_lrclib.base_url if layer == "toml" else DEAD_URL
    )
    args: list[str | Path] = ["run", lib]
    env = {}
    if layer == "flag":
        args.extend(["--lrclib-url", stub_lrclib.base_url])
        env["PLYRIX_LRCLIB_URL"] = DEAD_URL
    elif layer == "env":
        env["PLYRIX_LRCLIB_URL"] = stub_lrclib.base_url

    proc = run_cli(args, env=env)

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["written_lrc"] == 1
    assert track.with_suffix(".lrc").exists()


def test_flags_reach_the_settings(
    tmp_path: Path, run_child: Callable[..., dict[str, object]]
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    state = tmp_path / "elsewhere" / "state.db"

    report = run_child(
        str(lib),
        "--lrclib-url",
        DEAD_URL,
        "--exclude",
        "first/*",
        "--exclude",
        "second",
        "--state-db",
        str(state),
        "--verbose",
        env={"PLYRIX_EXCLUDES": '["from-env"]'},
    )

    settings = report["settings"]
    assert isinstance(settings, dict)
    assert settings["lrclib_url"] == DEAD_URL
    assert settings["excludes"] == ["first/*", "second"]
    assert settings["state_db"] == str(state)
    assert settings["verbose"] is True


def test_a_flag_left_unset_does_not_override_a_lower_layer(
    tmp_path: Path, run_child: Callable[..., dict[str, object]]
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()

    report = run_child(
        str(lib),
        env={
            "PLYRIX_LRCLIB_URL": DEAD_URL,
            "PLYRIX_EXCLUDES": '["from-env"]',
            "PLYRIX_VERBOSE": "true",
        },
    )

    settings = report["settings"]
    assert isinstance(settings, dict)
    assert settings["excludes"] == ["from-env"]
    assert settings["verbose"] is True


def test_config_flag_replaces_the_default_file(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    write_toml(default_toml(tmp_path), lrclib_url=DEAD_URL)
    named = write_toml(
        tmp_path / "named.toml", lrclib_url=stub_lrclib.base_url, music_root=str(lib)
    )

    proc = run_cli(["run", "--config", named])

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").exists()


def test_settings_stay_out_of_the_class_after_loading(tmp_path: Path, clean_env: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    named = write_toml(tmp_path / "named.toml", lrclib_url=DEAD_URL)

    config.Settings.load(lib, {}, named)

    # Built directly, since load sets the file again and so would hide one left on the class.
    with pytest.raises(ValidationError, match="lrclib_url"):
        config.Settings(music_root=lib)


# One-line errors


def test_a_missing_default_config_file_is_empty(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    assert not default_toml(tmp_path).exists()

    proc = run_cli(["run", lib], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("target", ["missing.toml", "folder"])
def test_a_named_config_file_that_is_not_there_exits_2(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, target: str
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    (tmp_path / "folder").mkdir()

    proc = run_cli(
        ["run", lib, "--config", tmp_path / target],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url},
    )

    assert_refused(proc)
    assert sidecars(lib) == []
    assert stub_lrclib.requests == []


@pytest.mark.parametrize("key", ["typo", 'we\\nird"key'])
def test_an_unknown_toml_key_exits_2_with_one_line(
    tmp_path: Path, run_cli: RunCli, key: str
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    toml = default_toml(tmp_path)
    toml.parent.mkdir(parents=True)
    quoted = key.replace('"', '\\"')
    toml.write_text(f'lrclib_url = "{DEAD_URL}"\n"{quoted}" = "{CANARY}"\n')

    proc = run_cli(["run", lib])

    assert_refused(proc)
    assert CANARY.encode() not in proc.stderr


@pytest.mark.parametrize(
    "content",
    [b"lrclib_url = \n", b'lrclib_url = "http://caf\xe9.invalid/api"\n'],
    ids=["bad-syntax", "not-utf8"],
)
def test_a_config_file_that_is_not_toml_exits_2(
    tmp_path: Path, run_cli: RunCli, content: bytes
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    toml = default_toml(tmp_path)
    toml.parent.mkdir(parents=True)
    toml.write_bytes(content)

    assert_refused(run_cli(["run", lib]))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_a_config_file_that_cannot_be_read_exits_2(tmp_path: Path, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    toml = write_toml(default_toml(tmp_path), lrclib_url=DEAD_URL)
    toml.chmod(0)

    assert_refused(run_cli(["run", lib]))


@pytest.mark.parametrize(
    ("args", "env", "toml"),
    [
        (["--lrclib-url", f"not a url {CANARY}"], {}, {}),
        ([], {"PLYRIX_LRCLIB_URL": f"not a url {CANARY}"}, {}),
        ([], {}, {"lrclib_url": f"not a url {CANARY}"}),
    ],
    ids=["flag", "env", "toml"],
)
def test_a_url_that_is_not_a_url_exits_2_without_echoing_it(
    tmp_path: Path,
    run_cli: RunCli,
    args: list[str],
    env: dict[str, str],
    toml: dict[str, str],
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    if toml:
        write_toml(default_toml(tmp_path), **toml)

    proc = run_cli(["run", lib, *args], env=env)

    assert_refused(proc, "lrclib_url")
    assert CANARY.encode() not in proc.stderr


@pytest.mark.parametrize(
    "value", [f"notjson {CANARY}", f'"{CANARY}"'], ids=["not-json", "not-list"]
)
def test_excludes_that_are_not_a_json_list_exit_2_with_one_line(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, value: str
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()

    proc = run_cli(
        ["run", lib],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "PLYRIX_EXCLUDES": value},
    )

    assert_refused(proc, "excludes")
    assert CANARY.encode() not in proc.stderr


def test_an_exclude_no_path_can_hold_exits_2_with_one_line(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    # JSON can spell a lone surrogate, which no filesystem encoding can turn into bytes.
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = run_cli(
        ["run", lib],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "PLYRIX_EXCLUDES": '["\\ud800"]'},
    )

    assert_refused(proc, "excludes")
    assert stub_lrclib.requests == []


CANNOT_MATCH = ["/abs/Pod", "/", "./Pod", ".", "../Pod", "..", "Pod/../x", "a/./b", "a//b", ""]


@pytest.mark.parametrize("pattern", CANNOT_MATCH)
@pytest.mark.parametrize("how", ["flag", "env", "toml"])
def test_an_exclude_that_could_never_match_exits_2_and_writes_nothing(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, pattern: str, how: str
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    args: list[str | Path] = ["run", lib]
    env = {"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    if how == "flag":
        args += ["--exclude", pattern]
    elif how == "env":
        env["PLYRIX_EXCLUDES"] = json.dumps([pattern])
    else:
        write_toml(default_toml(tmp_path), excludes=[pattern])

    proc = run_cli(args, env=env)

    assert_refused(proc, "excludes", "relative to the music root")
    assert sidecars(lib) == []
    assert stub_lrclib.requests == []


@pytest.mark.parametrize("pattern", ["Pod", "Pod/", "Pod/*", "*", "..hidden", "name..", "a.b/c"])
def test_an_exclude_relative_to_the_root_is_accepted(clean_env: Path, pattern: str) -> None:
    lib = clean_env / "lib"
    lib.mkdir()
    settings, _ = config.Settings.load(lib, {"lrclib_url": DEAD_URL, "excludes": [pattern]})
    assert settings.excludes == [pattern]


@pytest.mark.parametrize("suffix", ["?x=1", "?", "#part", "#", "/?x=1", "?x=1#part"])
def test_an_lrclib_url_with_a_query_or_fragment_exits_2(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, suffix: str
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = run_cli(["run", lib], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url + suffix})

    assert_refused(proc, "lrclib_url")
    assert stub_lrclib.requests == []


def test_a_verbose_value_that_is_not_a_boolean_exits_2(tmp_path: Path, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()

    proc = run_cli(["run", lib], env={"PLYRIX_LRCLIB_URL": DEAD_URL, "PLYRIX_VERBOSE": "perhaps"})

    assert_refused(proc, "verbose")


# An empty value would become Path("."), which is the working directory, and a run would then write
# sidecars there because a shell variable happened to be unset.
def test_an_empty_root_variable_counts_as_unset(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    track = copy_track(tmp_path / "here")

    proc = run_cli(
        ["run"],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "PLYRIX_MUSIC_ROOT": ""},
        cwd=track.parent,
    )

    assert_refused(proc, "music_root")
    assert sidecars(tmp_path) == []


def test_an_empty_root_variable_leaves_the_run_path_as_the_root(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)

    proc = run_cli(
        ["run", lib], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "PLYRIX_MUSIC_ROOT": ""}
    )

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").exists()


def test_an_empty_url_variable_falls_through_to_the_config_file(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    write_toml(default_toml(tmp_path), lrclib_url=stub_lrclib.base_url)

    proc = run_cli(["run", lib], env={"PLYRIX_LRCLIB_URL": ""})

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").exists()


def test_a_bad_configured_root_is_not_replaced_by_the_run_path(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    """The run path stands in only for a root that is missing, not for one written wrongly."""
    lib = tmp_path / "lib"
    copy_track(lib)
    write_toml(default_toml(tmp_path), lrclib_url=stub_lrclib.base_url, music_root="")

    proc = run_cli(["run", lib])

    assert_refused(proc, "music_root")
    assert sidecars(lib) == []
    assert stub_lrclib.requests == []


def test_an_empty_run_path_exits_2(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    track = copy_track(tmp_path / "here")

    proc = run_cli(["run", ""], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}, cwd=track.parent)

    assert_refused(proc)
    assert sidecars(tmp_path) == []


@pytest.mark.parametrize("key", ["music_root", "state_db"])
@pytest.mark.parametrize("value", ["", "a\u0000b"], ids=["empty", "nul"])
def test_a_toml_path_that_names_nothing_exits_2(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, key: str, value: str
) -> None:
    track = copy_track(tmp_path / "here")
    values = {"lrclib_url": stub_lrclib.base_url, "music_root": str(track.parent), key: value}
    write_toml(default_toml(tmp_path), **values)

    proc = run_cli(["run"], cwd=track.parent)

    assert_refused(proc, key)
    assert sidecars(tmp_path) == []


# Misspelled variables


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("PLYRIX_TYPO", b"PLYRIX_TYPO"),
        ("PLYRIX_EXCLUDE", b"PLYRIX_EXCLUDE"),
        ("plyrix_typo", b"plyrix_typo"),
        ("PLYRIX_LINE\nBREAK", b"PLYRIX_LINE\\nBREAK"),
    ],
)
def test_an_unknown_variable_warns_and_the_run_continues(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, name: str, shown: bytes
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)

    proc = run_cli(["run", lib], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, name: CANARY})

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").exists()
    lines = [line for line in proc.stderr.splitlines() if b"pre-release" not in line]
    assert len(lines) == 1, proc.stderr
    assert shown in lines[0]
    assert CANARY.encode() not in proc.stderr


@pytest.mark.parametrize(
    "name",
    [
        "PLYRIX_DENY_LIST",
        "PLYRIX_REAL_SAMPLES",
        "PLYRIX_GATE_RESULT",
        "PLYRIX_REQUIRE_GITLEAKS",
        "PLYRIX_LRCLIB_CONTRACT_URL",
        "PLYRIX_PLEX_CONTRACT_URL",
        "PLYRIX_PLEX_CONTRACT_SECTION",
        "PLYRIX_PLEX_CONTRACT_TOKEN",
        "PLYRIX_DOCKER_GATE",
        "PLYRIX_LIDARR_E2E",
        "PLYRIX_LIDARR_E2E_ALBUM",
        "PLYRIX_LRCLIB_UPDATE_GATE",
        "PLYRIX_EXCLUDES",
        "plyrix_excludes",
        "NOT_PLYRIX_TYPO",
    ],
)
def test_a_known_variable_gets_no_warning(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, name: str
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = run_cli(["run", lib], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, name: "[]"})

    assert proc.returncode == 0, proc.stderr
    assert [line for line in proc.stderr.splitlines() if b"pre-release" not in line] == []


def test_the_warning_comes_before_the_error_it_may_explain(tmp_path: Path, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()

    proc = run_cli(["run", lib], env={"PLYRIX_LRCLIB_URLL": DEAD_URL})

    assert proc.returncode == 2
    first, second = proc.stderr.splitlines()
    assert b"PLYRIX_LRCLIB_URLL" in first
    assert b"lrclib_url" in second


# The run path rule (D-23)


def test_run_path_becomes_root(tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib / "Album")

    proc = run_cli(["run", lib / "Album"], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").exists()
    assert ledger_paths(tmp_path) == [os.fsencode(lib / "Album" / "t.lrc")]


def test_run_path_narrows(tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli) -> None:
    lib = tmp_path / "lib"
    chosen, left_alone = copy_track(lib / "A"), copy_track(lib / "B")
    env = {"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "PLYRIX_MUSIC_ROOT": str(lib)}

    proc = run_cli(["run", lib / "A"], env=env)

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["written_lrc"] == 1
    assert chosen.with_suffix(".lrc").exists()
    assert not left_alone.with_suffix(".lrc").exists()
    assert len(stub_lrclib.requests) == 1
    # The row names the file itself, so a later run over the whole library sees it.
    assert ledger_paths(tmp_path) == [os.fsencode(lib / "A" / "t.lrc")]

    proc = run_cli(["run"], env=env)

    assert proc.returncode == 0, proc.stderr
    counts = parse_counts(proc.stdout)
    assert (counts["synced"], counts["skipped_unchanged"], counts["written_lrc"]) == (2, 1, 1)


def test_the_guard_root_stays_the_music_root_when_a_run_narrows(
    tmp_path: Path, run_child: Callable[..., dict[str, object]]
) -> None:
    lib = tmp_path / "lib"
    (lib / "A").mkdir(parents=True)

    report = run_child(
        str(lib / "A"),
        env={"PLYRIX_LRCLIB_URL": DEAD_URL, "PLYRIX_MUSIC_ROOT": str(lib)},
    )

    assert report["roots"] == [str(lib)]
    assert report["start"] == str(lib / "A")


def test_a_run_path_equal_to_the_root_is_allowed(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)

    proc = run_cli(
        ["run", f"{lib}/"],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "PLYRIX_MUSIC_ROOT": str(lib)},
    )

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").exists()


def _sibling(tmp_path: Path) -> Path:
    return tmp_path / "other"


def _shares_the_root_prefix(tmp_path: Path) -> Path:
    return tmp_path / "lib2"


def _through_dotdot(tmp_path: Path) -> Path:
    return tmp_path / "lib" / ".." / "other"


def _through_a_symlink_in_the_root(tmp_path: Path) -> Path:
    link = tmp_path / "lib" / "escape"
    link.symlink_to(tmp_path / "other")
    return link


def _parent_of_the_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.mark.parametrize(
    "outside",
    [
        _sibling,
        _shares_the_root_prefix,
        _through_dotdot,
        _through_a_symlink_in_the_root,
        _parent_of_the_root,
    ],
)
def test_run_path_outside_the_root_exits_2_and_writes_nothing(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, outside: Callable[[Path], Path]
) -> None:
    """Plan 01-04's gap: this path would be walked and written with the guard watching the root."""
    lib = tmp_path / "lib"
    tracks = [copy_track(lib), copy_track(tmp_path / "other"), copy_track(tmp_path / "lib2")]
    before = [snap(track) for track in tracks]
    path = outside(tmp_path)

    proc = run_cli(
        ["run", path],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "PLYRIX_MUSIC_ROOT": str(lib)},
    )

    assert_refused(proc, "music_root")
    assert os.fsencode(tmp_path) not in proc.stderr
    assert sidecars(tmp_path) == []
    assert [snap(track) for track in tracks] == before
    assert stub_lrclib.requests == []
    assert not state_db_path(tmp_path).exists()


def test_a_relative_run_path_outside_the_root_exits_2(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    copy_track(tmp_path / "other")

    proc = run_cli(
        ["run", "../other"],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "PLYRIX_MUSIC_ROOT": str(lib)},
        cwd=lib,
    )

    assert_refused(proc, "music_root")
    assert sidecars(tmp_path) == []


def test_the_same_outside_path_is_written_with_no_root_configured(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    """The control for the test above: that folder has lyrics to write, so the refusal did it."""
    track = copy_track(tmp_path / "other")

    proc = run_cli(["run", track.parent], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").exists()


def test_a_symlink_that_stays_in_the_root_is_allowed(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib / "A")
    (lib / "alias").symlink_to(lib / "A")

    proc = run_cli(
        ["run", lib / "alias"],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "PLYRIX_MUSIC_ROOT": str(lib)},
    )

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").exists()


def test_a_root_reached_through_a_symlink_still_contains_its_folders(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib / "A")
    alias = tmp_path / "alias"
    alias.symlink_to(lib)

    proc = run_cli(
        ["run", lib / "A"],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "PLYRIX_MUSIC_ROOT": str(alias)},
    )

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").exists()
    assert ledger_paths(tmp_path) == [os.fsencode(lib / "A" / "t.lrc")]


@pytest.mark.parametrize("target", ["missing", "A/t.flac"])
def test_a_run_path_in_the_root_that_is_not_a_folder_exits_2(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, target: str
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib / "A")

    proc = run_cli(
        ["run", lib / target],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "PLYRIX_MUSIC_ROOT": str(lib)},
    )

    assert_refused(proc)
    assert stub_lrclib.requests == []


# Relative paths. The guard trips on any relative write open, so none may reach the writer.


@pytest.mark.parametrize("path", ["./album", "album", "album/.", "../lib/album"])
def test_relative_run_path(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, path: str
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib / "album")

    proc = run_cli(["run", path], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}, cwd=lib)

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["written_lrc"] == 1
    assert track.with_suffix(".lrc").exists()


@pytest.mark.parametrize("how", ["env", "toml"])
def test_a_relative_root_from_a_variable_or_file_exits_2(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, how: str
) -> None:
    copy_track(tmp_path / "lib" / "album")
    env = {"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    if how == "env":
        env["PLYRIX_MUSIC_ROOT"] = "lib"
    else:
        write_toml(default_toml(tmp_path), music_root="lib")

    proc = run_cli(["run", "lib/album"], env=env, cwd=tmp_path)

    assert_refused(proc, "music_root", "absolute")
    assert stub_lrclib.requests == []
    assert sidecars(tmp_path / "lib") == []


@pytest.mark.parametrize("how", ["env", "toml"])
def test_a_relative_state_db_from_a_variable_or_file_exits_2_and_creates_nothing(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, how: str
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = {"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    if how == "env":
        env["PLYRIX_STATE_DB"] = "x/state.db"
    else:
        write_toml(default_toml(tmp_path), state_db="x/state.db")

    proc = run_cli(["run", lib], env=env, cwd=elsewhere)

    assert_refused(proc, "state_db", "absolute")
    assert list(elsewhere.iterdir()) == []
    assert stub_lrclib.requests == []


@pytest.mark.parametrize("how", ["env", "toml"])
def test_a_home_relative_path_from_a_variable_or_file_is_expanded(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, how: str
) -> None:
    home = tmp_path / "home"
    track = copy_track(home / "lib")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = {"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "HOME": str(home)}
    if how == "env":
        env |= {"PLYRIX_MUSIC_ROOT": "~/lib", "PLYRIX_STATE_DB": "~/state/x.db"}
    else:
        write_toml(default_toml(tmp_path), music_root="~/lib", state_db="~/state/x.db")

    proc = run_cli(["run"], env=env, cwd=elsewhere)

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").exists()
    assert (home / "state" / "x.db").exists()
    assert list(elsewhere.iterdir()) == []


def test_a_home_folder_that_does_not_exist_exits_2(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = run_cli(
        ["run", lib],
        env={
            "PLYRIX_LRCLIB_URL": stub_lrclib.base_url,
            "PLYRIX_STATE_DB": "~plyrix-no-such-user/state.db",
        },
        cwd=tmp_path,
    )

    assert_refused(proc, "state_db")
    assert stub_lrclib.requests == []


def test_a_relative_state_db_flag_means_the_working_directory(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    here = tmp_path / "here"
    here.mkdir()

    proc = run_cli(
        ["run", lib, "--state-db", "x.db"],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url},
        cwd=here,
    )

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").exists()
    assert (here / "x.db").exists()


def test_a_state_db_flag_with_a_tilde_means_the_home_folder_from_any_working_directory(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    """A shell doesn't expand ~ after an equals sign, so the flag reaches plyrix as typed."""
    lib = tmp_path / "lib"
    track = copy_track(lib)
    home = tmp_path / "home"
    home.mkdir()
    env = {"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "HOME": str(home)}
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()

    written = run_cli(["run", lib, "--state-db=~/x.db"], env=env, cwd=first)
    done = run_cli(["run", lib, "--state-db=~/x.db"], env=env, cwd=second)

    assert written.returncode == 0, written.stderr
    assert parse_counts(written.stdout)["written_lrc"] == 1
    assert parse_counts(done.stdout)["synced"] == 1
    assert parse_counts(done.stdout)["skipped_unchanged"] == 1
    assert track.with_suffix(".lrc").exists()
    assert (home / "x.db").exists()
    assert list(first.iterdir()) == []
    assert list(second.iterdir()) == []


def test_a_state_db_flag_with_an_unknown_home_folder_exits_2_and_creates_nothing(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    here = tmp_path / "here"
    here.mkdir()

    proc = run_cli(
        ["run", lib, "--state-db=~plyrix-no-such-user/x.db"],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url},
        cwd=here,
    )

    assert_refused(proc, "state_db")
    assert list(here.iterdir()) == []
    assert stub_lrclib.requests == []


def test_an_absolute_state_db_flag_can_still_name_a_folder_called_tilde(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    home = tmp_path / "home"
    home.mkdir()

    proc = run_cli(
        ["run", lib, f"--state-db={tmp_path}/~/x.db"],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "HOME": str(home)},
    )

    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "~" / "x.db").exists()
    assert list(home.iterdir()) == []


def test_a_relative_run_path_stands_in_as_the_root(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    track = copy_track(tmp_path / "lib" / "album")

    proc = run_cli(
        ["run", "lib/album"], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}, cwd=tmp_path
    )

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").exists()
    assert ledger_paths(tmp_path) == [os.fsencode(tmp_path / "lib" / "album" / "t.lrc")]


def test_dotdot_after_a_symlink_means_what_the_shell_means(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    """A lexical abspath would turn link/../album into the wrong folder, beside the link."""
    wanted = copy_track(tmp_path / "real" / "album")
    wrong = copy_track(tmp_path / "here" / "album")
    (tmp_path / "real" / "deep").mkdir()
    (tmp_path / "here" / "link").symlink_to(tmp_path / "real" / "deep")

    proc = run_cli(
        ["run", "link/../album"],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url},
        cwd=tmp_path / "here",
    )

    assert proc.returncode == 0, proc.stderr
    assert wanted.with_suffix(".lrc").exists()
    assert not wrong.with_suffix(".lrc").exists()


def test_load_returns_absolute_paths(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lib = clean_env / "lib"
    (lib / "album").mkdir(parents=True)
    monkeypatch.chdir(clean_env)
    monkeypatch.setenv("PLYRIX_LRCLIB_URL", DEAD_URL)

    settings, start = config.Settings.load(Path("lib/album"), {})

    assert settings.music_root == lib / "album"
    assert start == os.fsencode(lib / "album")


def test_a_root_of_slash_contains_everything(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every path is inside it, including the state database, so that is what gets refused."""
    lib = clean_env / "lib"
    lib.mkdir()
    monkeypatch.setenv("PLYRIX_LRCLIB_URL", DEAD_URL)
    monkeypatch.setenv("PLYRIX_MUSIC_ROOT", "/")

    with pytest.raises(config.ConfigError, match="state_db"):
        config.Settings.load(lib, {})


# The state database stays out of the library (D-04)


@pytest.mark.parametrize("how", ["flag", "env", "toml"])
def test_state_db_inside_the_root_exits_2_and_creates_nothing(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, how: str
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    before = tree(lib)
    state = str(lib / "state" / "state.db")
    args: list[str | Path] = ["run", lib]
    env = {"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    if how == "flag":
        args.extend(["--state-db", state])
    elif how == "env":
        env["PLYRIX_STATE_DB"] = state
    else:
        write_toml(default_toml(tmp_path), state_db=state)

    proc = run_cli(args, env=env)

    assert_refused(proc, "state_db")
    assert tree(lib) == before
    assert sidecars(lib) == []
    assert stub_lrclib.requests == []
    assert track.exists()


def test_state_db_in_a_part_of_the_root_the_run_does_not_walk_exits_2(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib / "A")
    (lib / "B").mkdir()

    proc = run_cli(
        ["run", lib / "A"],
        env={
            "PLYRIX_LRCLIB_URL": stub_lrclib.base_url,
            "PLYRIX_MUSIC_ROOT": str(lib),
            "PLYRIX_STATE_DB": str(lib / "B" / "state.db"),
        },
    )

    assert_refused(proc, "state_db")
    assert not (lib / "B" / "state.db").exists()


def test_state_db_reaching_the_root_through_a_symlink_exits_2(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    alias = tmp_path / "alias"
    alias.symlink_to(lib)

    proc = run_cli(
        ["run", lib],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "PLYRIX_STATE_DB": str(alias / "s.db")},
    )

    assert_refused(proc, "state_db")
    assert not (lib / "s.db").exists()


def test_a_relative_state_db_inside_the_root_exits_2(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = run_cli(
        ["run", "."],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "PLYRIX_STATE_DB": "state.db"},
        cwd=lib,
    )

    assert_refused(proc, "state_db")
    assert not (lib / "state.db").exists()


def test_the_default_state_db_inside_the_root_exits_2(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = run_cli(
        ["run", lib],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "XDG_STATE_HOME": str(lib / ".state")},
    )

    assert_refused(proc, "state_db")
    assert not (lib / ".state").exists()


def test_state_db_beside_the_root_with_a_shared_prefix_is_allowed(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib)
    state = tmp_path / "lib2" / "state.db"

    proc = run_cli(
        ["run", lib],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "PLYRIX_STATE_DB": str(state)},
    )

    assert proc.returncode == 0, proc.stderr
    assert track.with_suffix(".lrc").exists()
    assert state.exists()


# Non-UTF-8 paths. TOML can't hold one, so a variable or the run path has to carry it.


def test_a_non_utf8_root_keeps_its_bytes(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib_bytes = os.fsencode(tmp_path) + b"/caf\xe9"
    os.mkdir(lib_bytes)
    shutil.copyfile(os.fsencode(FIXTURES / "t.flac"), lib_bytes + b"/t.flac")

    proc = run_cli(
        ["run"],
        env={
            "PLYRIX_LRCLIB_URL": stub_lrclib.base_url,
            "PLYRIX_MUSIC_ROOT": os.fsdecode(lib_bytes),
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert os.path.exists(lib_bytes + b"/t.lrc")
    assert ledger_paths(tmp_path) == [lib_bytes + b"/t.lrc"]


def test_a_non_utf8_run_path_keeps_its_bytes(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib_bytes = os.fsencode(tmp_path) + b"/caf\xe9"
    os.mkdir(lib_bytes)
    shutil.copyfile(os.fsencode(FIXTURES / "t.flac"), lib_bytes + b"/t.flac")

    proc = run_cli(["run", lib_bytes], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert proc.returncode == 0, proc.stderr
    assert os.path.exists(lib_bytes + b"/t.lrc")


def test_load_keeps_a_non_utf8_root(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lib_bytes = os.fsencode(clean_env) + b"/caf\xe9"
    os.mkdir(lib_bytes)
    monkeypatch.setenv("PLYRIX_LRCLIB_URL", DEAD_URL)
    monkeypatch.setenv("PLYRIX_MUSIC_ROOT", os.fsdecode(lib_bytes))

    settings, start = config.Settings.load(None, {})

    assert os.fsencode(settings.music_root) == lib_bytes
    assert start == lib_bytes


# The README's run section, held to the settings so the two can't drift apart


def readme_env_names(text: str) -> set[str]:
    """Return the PLYRIX_* variable names a text mentions."""
    return set(re.findall(r"\bPLYRIX_[A-Z0-9_]+", text))


def names_that_are_not_settings(text: str) -> set[str]:
    return {
        name
        for name in readme_env_names(text)
        if name.removeprefix("PLYRIX_").lower() not in config.Settings.model_fields
    }


def test_readme_documents_run() -> None:
    readme = README.read_text(encoding="utf-8")

    assert "uv run plyrix run /srv/music" in readme
    assert {"PLYRIX_LRCLIB_URL", "PLYRIX_MUSIC_ROOT"} <= readme_env_names(readme)
    required = {
        f"PLYRIX_{name.upper()}"
        for name, field in config.Settings.model_fields.items()
        if field.is_required()
    }
    assert required <= readme_env_names(readme)


def test_readme_documents_the_plain_setting() -> None:
    readme = README.read_text(encoding="utf-8")

    assert "| `--plain` | `PLYRIX_PLAIN` | `plain` | `write` |" in readme


def test_readme_documents_the_dry_run_setting() -> None:
    readme = README.read_text(encoding="utf-8")

    assert "| `--dry-run` | `PLYRIX_DRY_RUN` | `dry_run` | off |" in readme


def test_readme_documents_the_method_setting_and_its_limit() -> None:
    readme = README.read_text(encoding="utf-8")

    assert "| `--sidecar-method` | `PLYRIX_SIDECAR_METHOD` | `sidecar_method` | `auto` |" in readme
    assert "a kill between those two steps leaves an empty `.lrc`" in readme


def test_readme_documents_the_rate_setting() -> None:
    readme = README.read_text(encoding="utf-8")

    assert "| `--max-rate` | `PLYRIX_MAX_RATE` | `max_rate` | no limit |" in readme


@pytest.mark.parametrize("value", [True, False])
def test_rate_a_switch_in_the_file_exits_2(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, value: bool
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    write_toml(default_toml(tmp_path), max_rate=value)

    proc = run_cli(["run", lib], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url})

    assert_refused(proc, "max_rate")
    assert proc.stderr.strip() == b"plyrix: invalid settings: max_rate (PLYRIX_MAX_RATE)"
    assert sidecars(lib) == []
    assert stub_lrclib.requests == []


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "abc", "1e-300", "true", "false"])
@pytest.mark.parametrize("source", ["flag", "variable", "file"])
def test_rate_invalid_settings_exit_2(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli, value: str, source: str
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    args: list[str | Path] = ["run", lib]
    env = {"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    if source == "flag":
        args += ["--max-rate", value]
    elif source == "variable":
        env["PLYRIX_MAX_RATE"] = value
    else:
        default_toml(tmp_path).parent.mkdir(parents=True)
        default_toml(tmp_path).write_text(f'max_rate = "{value}"\n')

    proc = run_cli(args, env=env)

    assert_refused(proc, "max_rate")
    assert proc.stderr.strip() == b"plyrix: invalid settings: max_rate (PLYRIX_MAX_RATE)"
    assert sidecars(lib) == []
    assert stub_lrclib.requests == []


def test_rate_is_off_unless_asked_for(clean_env: Path) -> None:
    lib = clean_env / "lib"
    lib.mkdir()
    settings, _ = config.Settings.load(lib, {"lrclib_url": DEAD_URL})
    assert settings.max_rate is None


def test_rate_flag_beats_variable_beats_file(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    write_toml(default_toml(tmp_path), max_rate=1.5)
    base = (str(lib), "--lrclib-url", DEAD_URL)

    def chosen(*args: str, env: dict[str, str] | None = None) -> float:
        proc = child_process(tmp_path, *base, *args, env=env)
        assert proc.returncode == 0, proc.stderr
        return float(json.loads(proc.stdout.splitlines()[0])["settings"]["max_rate"])

    assert chosen() == 1.5
    assert chosen(env={"PLYRIX_MAX_RATE": "2.5"}) == 2.5
    assert chosen("--max-rate", "3.5", env={"PLYRIX_MAX_RATE": "2.5"}) == 3.5


def test_rate_flag_reaches_a_real_run(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = run_cli(
        ["run", lib, "--max-rate", "1000"], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    )

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["written_lrc"] == 1


def test_dry_run_is_off_unless_asked_for(clean_env: Path) -> None:
    settings, _ = config.Settings.load(
        clean_env / "lib", {"lrclib_url": "http://localhost:3300/api"}
    )

    assert settings.dry_run is False


def test_the_dry_run_flag_beats_a_variable_saying_no(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = run_cli(
        ["run", lib, "--dry-run"],
        env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "PLYRIX_DRY_RUN": "0"},
    )

    assert proc.returncode == 0, proc.stderr
    assert sidecars(lib) == []


def test_a_dry_run_setting_that_is_not_a_yes_or_no_exits_2(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = run_cli(
        ["run", lib], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "PLYRIX_DRY_RUN": "maybe"}
    )

    assert proc.returncode == 2
    assert b"dry_run" in proc.stderr
    assert sidecars(lib) == []


@pytest.mark.parametrize("value", ["write", "skip"])
def test_plain_takes_write_or_skip(clean_env: Path, value: str) -> None:
    settings = config.Settings(
        music_root=clean_env,
        lrclib_url="http://localhost:3300/api",
        plain=value,
    )

    assert settings.plain == value


def test_plain_defaults_to_write(clean_env: Path) -> None:
    settings = config.Settings(
        music_root=clean_env,
        lrclib_url="http://localhost:3300/api",
    )

    assert settings.plain == "write"


@pytest.mark.parametrize("value", ["", "WRITE", "off", "true"])
def test_plain_refuses_anything_else(clean_env: Path, value: str) -> None:
    with pytest.raises(ValidationError):
        config.Settings(
            music_root=clean_env,
            lrclib_url="http://localhost:3300/api",
            plain=value,
        )


def test_plain_reads_the_config_file(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    track = copy_track(lib / "Album")
    write_toml(default_toml(tmp_path), plain="skip", lrclib_url=stub_lrclib.base_url)
    stub_lrclib.body = {
        "id": 7,
        "trackName": "Fixture Title",
        "artistName": "Fixture Artist",
        "albumName": "Fixture Album",
        "duration": 3.0,
        "instrumental": False,
        "plainLyrics": f"{CANARY} plain line 1",
        "syncedLyrics": None,
    }

    proc = run_cli(["run", lib])

    assert proc.returncode == 0, proc.stderr
    assert parse_counts(proc.stdout)["plain_skipped"] == 1
    assert not track.with_suffix(".txt").exists()


def test_readme_env_names_are_settings() -> None:
    readme = README.read_text(encoding="utf-8")

    assert readme_env_names(readme)
    assert names_that_are_not_settings(readme) == set()
    # The negative control: a name that isn't a setting has to be caught.
    sample = "Set PLYRIX_TYPO and PLYRIX_LRCLIB_URL."
    assert readme_env_names(sample) == {"PLYRIX_TYPO", "PLYRIX_LRCLIB_URL"}
    assert names_that_are_not_settings(sample) == {"PLYRIX_TYPO"}


# The dump id


@pytest.mark.parametrize(
    "value",
    ["x" * 256, "dump\nid", "dump\x7fid", "dump\x85id", "dump\udcffid"],
    ids=["too-long", "newline", "delete", "c1", "undecodable"],
)
def test_a_dump_id_a_ledger_or_a_log_line_cant_hold_is_refused(tmp_path: Path, value: str) -> None:
    with pytest.raises(config.ConfigError, match="dump_id") as caught:
        config.Settings.load(
            tmp_path, {"lrclib_url": "http://localhost:3300/api", "dump_id": value}
        )
    assert value not in caught.value.message


@pytest.mark.parametrize(
    "value", ["", "x" * 255, "lrclib-db-dump-20261101T000000Z.sqlite3.gz", "é"]
)
def test_a_usable_dump_id_is_kept(tmp_path: Path, value: str) -> None:
    settings, _ = config.Settings.load(
        tmp_path, {"lrclib_url": "http://localhost:3300/api", "dump_id": value}
    )
    assert settings.dump_id == value


def test_a_dump_id_flag_beats_the_variable_and_an_unset_flag_does_not_hide_it(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)
    env = {"PLYRIX_LRCLIB_URL": stub_lrclib.base_url, "PLYRIX_DUMP_ID": "from-variable"}

    assert run_cli(["run", lib], env=env).returncode == 0
    assert run_cli(["run", lib, "--dump-id", "from-flag"], env=env).returncode == 0

    conn = sqlite3.connect(state_db_path(tmp_path))
    rows = conn.execute("SELECT dump_id FROM runs ORDER BY id").fetchall()
    conn.close()
    assert rows == [("from-variable",), ("from-flag",)]


def test_an_undecodable_dump_id_flag_exits_2_with_one_line(
    tmp_path: Path, stub_lrclib: StubLrclib, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = run_cli(
        ["run", lib, "--dump-id", b"dump\xff"], env={"PLYRIX_LRCLIB_URL": stub_lrclib.base_url}
    )

    assert proc.returncode == 2
    assert b"dump_id" in proc.stderr
    assert b"Traceback" not in proc.stderr
    assert stub_lrclib.requests == []


# The public server, and URLs that only look like it. Each refused form runs with the pipeline
# replaced, so a check that fails here can't send anything to the real host.
@pytest.mark.parametrize(
    "url",
    [
        "https://lrclib.net/api",
        "https://LRCLIB.NET/api",
        "https://lrclib.net./api",
        "https://lrclib.net:443/api",
        "https://db-dumps.lrclib.net/",
        "http://www.lrclib.net:8080/api",
        "http://localhost@lrclib.net/api",
    ],
)
def test_public_lrclib_is_refused(tmp_path: Path, url: str) -> None:
    lib = tmp_path / "lib"
    copy_track(lib)

    proc = child_process(tmp_path, str(lib), env={"PLYRIX_LRCLIB_URL": url})

    assert_refused(proc, "lrclib_url")
    assert url.encode() not in proc.stderr
    assert not state_db_path(tmp_path).exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://lrclib.net@localhost/api",
        "http://notlrclib.net/api",
        "http://lrclib.net.evil.example/api",
        "http://localhost:3300/api",
        "http://[::1]:3300/api",
    ],
)
def test_other_hosts_are_not_refused(clean_env: Path, url: str) -> None:
    settings = config.Settings(music_root=clean_env / "lib", lrclib_url=url)

    assert str(settings.lrclib_url) == url


def urls_in(source: str) -> list[str]:
    """Return each str constant in source that is an http or https URL with a host.

    A scheme-only prefix has no host, so mounting an adapter passes. An f-string's literal part is
    a Constant too. A URL built at runtime is out of reach, and the redirect, proxy and socket
    tests cover that layer.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            parts = urlsplit(node.value)
            if parts.scheme in ("http", "https") and parts.hostname:
                found.append(node.value)
    return found


def test_src_holds_no_url() -> None:
    found = {}
    scanned = set()
    for path in sorted((SRC / "plyrix").rglob("*.py")):
        parts = path.relative_to(SRC).with_suffix("").parts
        scanned.add(".".join(parts))
        found[path.name] = urls_in(path.read_text(encoding="utf-8"))

    assert "plyrix.lrclib" in scanned
    assert {name: urls for name, urls in found.items() if urls} == {}


@pytest.mark.parametrize(
    "source",
    [
        'URL = "https://lrclib.net/api"',
        'URL = "http://localhost:3300"',
        'URL = "HTTP://LRCLIB.NET"',
        'URL = f"https://lrclib.net/{x}"',
    ],
)
def test_url_check_catches_a_url_with_a_host(source: str) -> None:
    assert len(urls_in(source)) == 1


@pytest.mark.parametrize(
    "source", ['session.mount("http://", adapter)', 'session.mount("https://", adapter)']
)
def test_url_check_allows_a_scheme_prefix(source: str) -> None:
    assert urls_in(source) == []


# Plex settings (D-03, D-03a, D-06, D-17)

PLEX_TOKEN = "plyrix-dummy-token-0123456789"


def plex_settings(clean_env: Path, **init: object) -> config.Settings:
    """Return settings built in this process, with a root and an LRCLIB URL and these values."""
    (clean_env / "lib").mkdir(exist_ok=True)
    return config.Settings(
        music_root=clean_env / "lib",
        lrclib_url="http://localhost:3300/api",
        **init,  # type: ignore[arg-type]
    )


def plex_refresh_env(tmp_path: Path, plex_url: str) -> dict[str, str]:
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    return {
        "PLYRIX_MUSIC_ROOT": str(lib),
        "PLYRIX_LRCLIB_URL": "http://localhost:3300/api",
        "PLYRIX_PLEX_URL": plex_url,
        "PLYRIX_PLEX_SECTION": "1",
        "PLYRIX_PLEX_TOKEN": PLEX_TOKEN,
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://plex.tv",
        "http://PLEX.TV.",
        "https://app.plex.tv/x",
        "http://user@plex.tv:32400",
        "https://Sub.App.Plex.Tv.:443",
    ],
)
def test_plex_url_on_plex_tv_exits_2_without_echoing_it(
    tmp_path: Path, run_cli: RunCli, url: str
) -> None:
    proc = run_cli(["plex-refresh"], env=plex_refresh_env(tmp_path, url))

    assert_refused(proc, "plex_url (PLYRIX_PLEX_URL)")
    assert b"not plex.tv" in proc.stderr
    assert url.encode() not in proc.stderr
    assert PLEX_TOKEN.encode() not in proc.stderr
    assert not state_db_path(tmp_path).exists()


@pytest.mark.parametrize("suffix", ["?x=1", "#part", "/?x=1"])
def test_plex_url_with_a_query_or_fragment_exits_2_and_sends_nothing(
    tmp_path: Path, stub_plex: StubPlex, run_cli: RunCli, suffix: str
) -> None:
    proc = run_cli(["plex-refresh"], env=plex_refresh_env(tmp_path, stub_plex.base_url + suffix))

    assert_refused(proc, "plex_url (PLYRIX_PLEX_URL)")
    assert stub_plex.requests == []


@pytest.mark.parametrize(
    "url",
    [
        "https://192-168-1-10.0123abcd.plex.direct:32400",
        "http://192.168.1.10:32400",
        "http://localhost:32400",
        "http://plex.tv.example.invalid:32400",
        "http://notplex.tv:32400",
    ],
)
def test_plex_url_on_your_own_server_is_allowed(clean_env: Path, url: str) -> None:
    settings = plex_settings(clean_env, plex_url=url)

    assert str(settings.plex_url).startswith(url)


def test_plex_token_has_no_flag(tmp_path: Path, run_cli: RunCli) -> None:
    proc = run_cli(["plex-refresh", "--plex-token", "x"])

    assert proc.returncode == 2
    assert b"No such option" in proc.stderr


def test_plex_token_from_a_systemd_credential(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = clean_env / "credentials"
    credentials.mkdir()
    (credentials / "plyrix_plex_token").write_text(PLEX_TOKEN + "\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))

    token = plex_settings(clean_env).plex_token

    assert token is not None
    assert token.get_secret_value() == PLEX_TOKEN


def test_plex_token_from_a_docker_secret(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = clean_env / "secrets"
    secrets.mkdir()
    (secrets / "plyrix_plex_token").write_text(PLEX_TOKEN + "\n")
    monkeypatch.setattr(config, "_DOCKER_SECRETS_DIR", str(secrets))

    token = plex_settings(clean_env).plex_token

    assert token is not None
    assert token.get_secret_value() == PLEX_TOKEN


def test_plex_token_variable_beats_the_secret_file(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = clean_env / "credentials"
    credentials.mkdir()
    (credentials / "plyrix_plex_token").write_text("from-the-file\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    monkeypatch.setenv("PLYRIX_PLEX_TOKEN", PLEX_TOKEN)

    token = plex_settings(clean_env).plex_token

    assert token is not None
    assert token.get_secret_value() == PLEX_TOKEN


def test_plex_secret_file_beats_the_config_file(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_toml(default_toml(clean_env), plex_token="from-the-config-file")
    credentials = clean_env / "credentials"
    credentials.mkdir()
    (credentials / "plyrix_plex_token").write_text(PLEX_TOKEN + "\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))

    token = plex_settings(clean_env).plex_token

    assert token is not None
    assert token.get_secret_value() == PLEX_TOKEN


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_an_unreadable_secret_file_names_its_setting_not_the_config_file(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (clean_env / "lib").mkdir()
    credentials = clean_env / "credentials"
    credentials.mkdir()
    secret = credentials / "plyrix_plex_token"
    secret.write_text(PLEX_TOKEN + "\n")
    secret.chmod(0)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    overrides = {"music_root": clean_env / "lib", "lrclib_url": "http://localhost:3300/api"}

    with pytest.raises(config.ConfigError) as caught:
        config.Settings.load(None, overrides)

    message = caught.value.message
    assert "plex_token (PLYRIX_PLEX_TOKEN)" in message
    assert "secret file" in message
    assert "config file" not in message and "JSON" not in message
    assert PLEX_TOKEN not in message
    assert str(credentials) not in message


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_run_with_an_unreadable_secret_file_exits_2_naming_it(
    tmp_path: Path, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    secret = credentials / "plyrix_plex_token"
    secret.write_text(PLEX_TOKEN + "\n")
    secret.chmod(0)
    env = {"PLYRIX_LRCLIB_URL": DEAD_URL, "CREDENTIALS_DIRECTORY": str(credentials)}

    proc = run_cli(["run", lib], env=env)

    assert_refused(proc, "plex_token (PLYRIX_PLEX_TOKEN)", "secret file")


def test_a_secret_file_that_is_not_utf8_names_its_setting(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (clean_env / "lib").mkdir()
    credentials = clean_env / "credentials"
    credentials.mkdir()
    (credentials / "plyrix_plex_token").write_bytes(b"\xff\xfe")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    overrides = {"music_root": clean_env / "lib", "lrclib_url": "http://localhost:3300/api"}

    with pytest.raises(config.ConfigError) as caught:
        config.Settings.load(None, overrides)

    message = caught.value.message
    assert "plex_token (PLYRIX_PLEX_TOKEN)" in message
    assert "secret file" in message and "UTF-8" in message
    assert "TOML" not in message and "JSON" not in message


@pytest.fixture
def unlistable_credentials(clean_env: Path) -> Iterator[Path]:
    """A credentials directory this user can enter but not list, as some secret managers make."""
    credentials = clean_env / "credentials"
    credentials.mkdir()
    (credentials / "plyrix_plex_token").write_text(PLEX_TOKEN + "\n")
    credentials.chmod(0o300)
    yield credentials
    credentials.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_a_secret_directory_that_cannot_be_listed_is_skipped(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, unlistable_credentials: Path
) -> None:
    (clean_env / "lib").mkdir()
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(unlistable_credentials))
    overrides = {"music_root": clean_env / "lib", "lrclib_url": "http://localhost:3300/api"}

    settings, _ = config.Settings.load(None, overrides)

    assert settings.plex_token is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_run_with_a_secret_directory_that_cannot_be_listed_is_not_a_config_error(
    tmp_path: Path, run_cli: RunCli
) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    credentials.chmod(0o300)
    env = {"PLYRIX_LRCLIB_URL": DEAD_URL, "CREDENTIALS_DIRECTORY": str(credentials)}
    try:
        proc = run_cli(["run", "--dry-run", lib], env=env)
    finally:
        credentials.chmod(0o700)

    # The config loaded, and the run got as far as walking the empty library.
    assert b"found no audio files" in proc.stderr, proc.stderr
    assert b"secret" not in proc.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_a_secret_directory_that_stops_listing_is_named_as_a_directory(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, unlistable_credentials: Path
) -> None:
    # It could pass the check and then lose its permissions before pydantic-settings lists it.
    (clean_env / "lib").mkdir()
    monkeypatch.setattr(config, "_secret_dirs", lambda: [str(unlistable_credentials)])
    overrides = {"music_root": clean_env / "lib", "lrclib_url": "http://localhost:3300/api"}

    with pytest.raises(config.ConfigError) as caught:
        config.Settings.load(None, overrides)

    message = caught.value.message
    assert "secrets directory" in message and "can't be listed" in message
    assert "music_root" not in message and "secret file" not in message
    assert str(unlistable_credentials) not in message


def test_a_secret_directory_that_is_not_there_gives_no_warning(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(clean_env / "not-there"))
    monkeypatch.setattr(config, "_DOCKER_SECRETS_DIR", str(clean_env / "not-there-either"))

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        settings = plex_settings(clean_env)

    assert settings.plex_token is None


@pytest.mark.parametrize("where", ["variable", "secret file"])
def test_an_empty_plex_token_counts_as_unset(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    if where == "variable":
        monkeypatch.setenv("PLYRIX_PLEX_TOKEN", "")
    else:
        credentials = clean_env / "credentials"
        credentials.mkdir()
        (credentials / "plyrix_plex_token").write_text("")
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))

    assert plex_settings(clean_env).plex_token is None


def test_plex_token_stays_out_of_repr_str_and_errors(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLYRIX_PLEX_TOKEN", PLEX_TOKEN)
    settings = plex_settings(clean_env, plex_url="http://localhost:32400")

    assert PLEX_TOKEN not in repr(settings)
    assert PLEX_TOKEN not in str(settings)
    monkeypatch.setenv("PLYRIX_MUSIC_ROOT", str(clean_env / "lib"))
    monkeypatch.setenv("PLYRIX_LRCLIB_URL", "http://localhost:3300/api")
    for bad in ({"plex_url": "https://plex.tv"}, {"plex_pace": "-1"}, {"plex_section": "0"}):
        with pytest.raises(config.ConfigError) as caught:
            config.Settings.load(None, bad)
        assert PLEX_TOKEN not in caught.value.message
        assert PLEX_TOKEN not in str(caught.value)


@pytest.mark.parametrize("value", [True, -1, -0.5, "nan", "inf"])
def test_plex_pace_that_is_not_a_wait_is_refused(clean_env: Path, value: object) -> None:
    with pytest.raises(ValidationError):
        plex_settings(clean_env, plex_pace=value)


def test_plex_pace_of_zero_is_allowed(clean_env: Path) -> None:
    assert plex_settings(clean_env, plex_pace=0).plex_pace == 0


def test_plex_pace_true_in_the_config_file_exits_2(tmp_path: Path, run_cli: RunCli) -> None:
    write_toml(default_toml(tmp_path), plex_pace=True)
    env = plex_refresh_env(tmp_path, "http://localhost:32400")

    proc = run_cli(["plex-refresh"], env=env)

    assert_refused(proc, "plex_pace (PLYRIX_PLEX_PACE)")


def test_plex_section_true_in_the_config_file_exits_2(tmp_path: Path, run_cli: RunCli) -> None:
    write_toml(default_toml(tmp_path), plex_section=True)
    env = plex_refresh_env(tmp_path, "http://localhost:32400")
    del env["PLYRIX_PLEX_SECTION"]

    proc = run_cli(["plex-refresh"], env=env)

    assert_refused(proc, "plex_section (PLYRIX_PLEX_SECTION)")


def test_plex_music_root_that_is_relative_exits_2(tmp_path: Path, run_cli: RunCli) -> None:
    env = plex_refresh_env(tmp_path, "http://localhost:32400")
    env["PLYRIX_PLEX_MUSIC_ROOT"] = "music"

    proc = run_cli(["plex-refresh"], env=env)

    assert_refused(proc, "plex_music_root (PLYRIX_PLEX_MUSIC_ROOT)")


def test_plex_music_root_defaults_to_the_resolved_music_root(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = clean_env / "real"
    real.mkdir()
    link = clean_env / "link"
    link.symlink_to(real)
    monkeypatch.setenv("PLYRIX_MUSIC_ROOT", str(link))
    monkeypatch.setenv("PLYRIX_LRCLIB_URL", "http://localhost:3300/api")

    settings, _ = config.Settings.load(None, {})

    assert settings.plex_music_root == Path(os.path.realpath(real))
    assert settings.plex_music_root == settings.music_root


def test_plex_music_root_that_is_set_is_kept_as_given(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (clean_env / "lib").mkdir()
    monkeypatch.setenv("PLYRIX_MUSIC_ROOT", str(clean_env / "lib"))
    monkeypatch.setenv("PLYRIX_LRCLIB_URL", "http://localhost:3300/api")
    monkeypatch.setenv("PLYRIX_PLEX_MUSIC_ROOT", "/plexview/../music")

    settings, _ = config.Settings.load(None, {})

    assert settings.plex_music_root == Path("/plexview/../music")


def readme_settings_table(text: str) -> str:
    """Return the README's Settings table, from its heading to the first line after the table."""
    section = text.split("### Settings\n", 1)[1]
    lines = section.lstrip("\n").splitlines()
    table = []
    for line in lines:
        if not line.startswith("|"):
            break
        table.append(line)
    return "\n".join(table)


def test_readme_documents_every_setting() -> None:
    table = readme_settings_table(README.read_text(encoding="utf-8"))

    for name in config.Settings.model_fields:
        assert f"`PLYRIX_{name.upper()}`" in table, name


def test_readme_documents_the_plex_settings() -> None:
    readme = README.read_text(encoding="utf-8")

    assert "| `--plex-url` | `PLYRIX_PLEX_URL` | `plex_url` | none |" in readme
    assert "| `--plex-section` | `PLYRIX_PLEX_SECTION` | `plex_section` | none |" in readme
    assert "| `--plex-pace` | `PLYRIX_PLEX_PACE` | `plex_pace` | `1` |" in readme
    assert "`plyrix_plex_token`" in readme
    assert "*.plex.direct" in readme
    assert "DNS-rebinding" in readme


def test_readme_states_the_precedence_with_secret_files() -> None:
    readme = README.read_text(encoding="utf-8")

    assert (
        "A flag beats a variable, a variable beats a secret file, and a secret file beats the "
        "config file."
    ) in readme


def test_child_processes_never_get_forced_colour() -> None:
    # conftest drops what makes Typer force Rich's terminal mode, as hosted CI sets GITHUB_ACTIONS.
    from conftest import COLOUR_FORCERS

    assert [name for name in COLOUR_FORCERS if name in os.environ] == []
    assert os.environ.get("NO_COLOR") == "1"
