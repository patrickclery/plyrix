"""Image gate: the locally built plyrix:local image, checked from outside.

Skipped unless PLYRIX_DOCKER_GATE=1, and never run in hosted CI. Building writes to Docker's own
storage, so the build is a step a person approves (plan 03-07), and this gate then runs against the
result: plyrix runs, as uid 1000, with its tools on PATH, cron mode refuses a schedule of more than
one line, and the image holds no dump, no lyrics and none of the working tree's private files.
"""

import os
import shutil
import subprocess

import pytest

IMAGE = "plyrix:local"

pytestmark = [
    pytest.mark.docker_image,
    pytest.mark.skipif(
        os.environ.get("PLYRIX_DOCKER_GATE") != "1",
        reason="set PLYRIX_DOCKER_GATE=1 after building plyrix:local",
    ),
]


def docker(*args: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("docker") is None:
        pytest.fail("PLYRIX_DOCKER_GATE=1 but docker is not on PATH")
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False, timeout=600
    )


def test_help_runs() -> None:
    proc = docker("run", "--rm", IMAGE, "--help")

    assert proc.returncode == 0, proc.stderr
    assert "plyrix" in proc.stdout


def test_the_image_runs_as_1000() -> None:
    uid = docker("run", "--rm", "--entrypoint", "id", IMAGE, "-u")
    user = docker("image", "inspect", "--format", "{{.Config.User}}", IMAGE)

    assert uid.returncode == 0, uid.stderr
    assert uid.stdout.strip() == "1000"
    assert user.returncode == 0, user.stderr
    assert user.stdout.strip() == "1000:1000"


def test_its_tools_are_on_path() -> None:
    tools = ("tini", "supercronic", "ionice", "plyrix")
    script = " && ".join(f"command -v {tool}" for tool in tools)
    proc = docker("run", "--rm", "--entrypoint", "sh", IMAGE, "-c", script)

    assert proc.returncode == 0, proc.stderr
    assert len(proc.stdout.splitlines()) == len(tools)


def test_cron_mode_refuses_a_schedule_of_more_than_one_line() -> None:
    proc = docker("run", "--rm", "-e", "CRON_SCHEDULE=@weekly\n* * * * * id", IMAGE)

    assert proc.returncode == 2, proc.stderr
    assert "CRON_SCHEDULE" in proc.stderr


def test_the_image_holds_no_dump_lyrics_or_private_file() -> None:
    proc = docker(
        "run", "--rm", "--entrypoint", "find", IMAGE, "/", "-xdev",
        "(",
        "-name", "*.sqlite3*", "-o", "-name", "lrclib-db-dump*", "-o", "-name", "*.lrc",
        "-o", "-name", "PROJECT-BRIEF.md", "-o", "-name", "CLAUDE.local.md",
        "-o", "(", "-type", "d", "(", "-name", ".planning", "-o", "-name", ".claude", ")", ")",
        ")", "-print",
    )  # fmt: skip

    # find exits 1 when it can't read a folder as uid 1000; the listing is what counts.
    assert proc.returncode in (0, 1), proc.stderr
    assert proc.stdout == ""
