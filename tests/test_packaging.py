"""Hold compose, the build context, the package, the image definition and the install docs to the
repo's files, without building an image."""

import importlib.metadata
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml  # type: ignore[import-untyped]  # PyYAML ships no stubs
from packaging.markers import Marker

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.yaml"
README = ROOT / "README.md"
DOCKERIGNORE = ROOT / ".dockerignore"
SYSTEMD = ROOT / "examples" / "systemd"
DOCKERFILE = ROOT / "Dockerfile"
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"
LOCK = ROOT / "uv.lock"
LICENSE = ROOT / "LICENSE"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
PIN = "e1d863afc430392c8b2f97bea72139292d498432"
UPSTREAM = "https://github.com/tranxuanthang/lrclib.git"
FULL_SHA = re.compile(r"\b[0-9a-f]{40}\b")
# The listing Worker is the one LRCLIB's own db-dumps page reads (scripts/lrclib-update.sh).
DUMP_HOSTS = {"db-dumps.lrclib.net", "lrclib.net", "lrclib-db-dumps.bu3nnyut4y9jfkdg.workers.dev"}
DUMP_SUFFIXES = {".gz", ".sqlite", ".sqlite3", ".db"}
URL = re.compile(r"https?://[^\s\"'`<>()\[\]{}$|]+")


def compose() -> dict[str, Any]:
    loaded = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def lrclib() -> dict[str, Any]:
    service = compose()["services"]["lrclib"]
    assert isinstance(service, dict)
    return service


# compose.yaml's lrclib service (D-13)


def test_lrclib_is_built_from_upstream_at_the_full_pinned_sha() -> None:
    build = lrclib()["build"]

    assert build["context"] == f"{UPSTREAM}#{PIN}"
    assert build.get("dockerfile", "Dockerfile") == "Dockerfile"


def test_the_update_side_pins_the_same_lrclib_build() -> None:
    from plyrix.state import lrclib_db

    assert lrclib_db.LRCLIB_PIN == PIN


def test_the_update_scripts_default_listing_is_an_allowed_dump_host() -> None:
    text = (ROOT / "scripts" / "lrclib-update.sh").read_text(encoding="utf-8")
    found = re.search(r"LRCLIB_DUMP_LISTING:-(https://[^}]+)\}", text)

    assert found is not None
    assert urlsplit(found.group(1)).hostname in DUMP_HOSTS


def test_lrclib_is_never_pulled_from_a_registry() -> None:
    service = lrclib()

    assert service["pull_policy"] == "build"
    image = service["image"]
    assert PIN[:7] in image
    assert "/" not in image and "." not in image.split(":")[0]


def test_lrclib_runs_as_1000_by_default() -> None:
    assert lrclib()["user"] == "${LRCLIB_USER:-1000:1000}"


def test_lrclib_is_published_on_loopback_only() -> None:
    ports = lrclib()["ports"]

    assert len(ports) == 1
    assert ports[0].startswith("127.0.0.1:")


def test_lrclib_keeps_temp_files_in_its_data_folder() -> None:
    assert lrclib()["environment"]["TMPDIR"] == "/data/tmp"


def test_lrclib_data_is_a_required_bind_mount_at_data() -> None:
    volumes = lrclib()["volumes"]

    assert len(volumes) == 1
    assert volumes[0]["type"] == "bind"
    assert volumes[0]["source"].startswith("${LRCLIB_DATA:?")
    assert volumes[0]["target"] == "/data"


def test_lrclib_health_is_its_search_answering_the_probe() -> None:
    service = lrclib()
    command = " ".join(service["healthcheck"]["test"])

    assert "/api/search" in command
    assert "artist_name=plyrix+preflight&track_name=plyrix+preflight" in command
    assert service["restart"]


def test_compose_variables_are_not_plyrix_settings() -> None:
    names = re.findall(r"\$\{([A-Za-z0-9_]+)", COMPOSE.read_text(encoding="utf-8"))

    assert "LRCLIB_DATA" in names
    assert [name for name in names if name.startswith("PLYRIX_")] == []


def test_the_readme_and_compose_quote_the_same_full_sha() -> None:
    in_compose = set(FULL_SHA.findall(COMPOSE.read_text(encoding="utf-8")))
    in_readme = set(FULL_SHA.findall(README.read_text(encoding="utf-8")))

    assert in_compose == {PIN}
    assert in_readme == in_compose


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not on PATH")
def test_docker_compose_parses_the_file(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "LRCLIB_DATA": str(tmp_path / "lrclib"),
        "MUSIC_ROOT": str(tmp_path / "music"),
        "STATE_DIR": str(tmp_path / "state"),
    }
    proc = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config", "--format", "json"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    service = json.loads(proc.stdout)["services"]["lrclib"]
    assert service["user"] == "1000:1000"
    assert [port["host_ip"] for port in service["ports"]] == ["127.0.0.1"]
    assert service["volumes"][0]["source"] == str(tmp_path / "lrclib")
    plyrix = json.loads(proc.stdout)["services"]["plyrix"]
    mounts = {(v["source"], v["target"]): v.get("read_only", False) for v in plyrix["volumes"]}
    assert mounts[(str(tmp_path / "music"), str(tmp_path / "music"))] is False
    assert mounts[(str(tmp_path / "lrclib"), str(tmp_path / "lrclib"))] is True
    assert mounts[(str(tmp_path / "state"), "/state")] is False
    assert "PLYRIX_PLEX_TOKEN" not in plyrix["environment"]
    assert [secret["source"] for secret in plyrix["secrets"]] == ["plyrix_plex_token"]


def test_docker_compose_refuses_a_missing_lrclib_data(tmp_path: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker is not on PATH")
    env = {k: v for k, v in os.environ.items() if k != "LRCLIB_DATA"}
    env |= {"MUSIC_ROOT": str(tmp_path / "music"), "STATE_DIR": str(tmp_path / "state")}
    proc = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert proc.returncode != 0
    assert "LRCLIB_DATA" in proc.stderr


# The README's install section (INST-04)


def install_lead() -> str:
    """Return README's ## Install section up to its first code block."""
    readme = README.read_text(encoding="utf-8")
    assert "## Install" in readme and "## Run" in readme
    assert readme.index("## Install") < readme.index("## Run")
    section = readme[readme.index("## Install") :]
    return section[: section.index("```")]


def test_install_section_states_the_disk_cost_first() -> None:
    lead = install_lead()
    pieces = re.split(r"(?<=[.!?])\s+|\n|\|", lead)

    # The first-load figures as measured in Phase 4 (04-04), never the earlier arithmetic.
    for figure in ("49.3 GB", "319.8 GB", "129.6 GB", "About 80 GB"):
        assert figure in lead, figure
    for gone in ("269 GB", "318 GB", "124 GB", "arithmetic"):
        assert gone not in lead, gone
    assert [piece for piece in pieces if "Measured 2026-11-11" in piece]
    assert "Docker build space" in lead
    assert "### Disk space" in lead


def test_install_disk_cost_gives_docker_build_space_as_measured() -> None:
    rows = [line for line in install_lead().splitlines() if "Docker build space" in line]

    assert len(rows) == 1
    row = rows[0]
    assert "not yet measured" not in row
    assert "Measured 2026-11-11" in row
    # Two measures of the same images (04-08 docs gap #8).
    for figure in ("246 MB", "44 MB", "89.1 MB", "12.5 MB", "docker images", "Docker 29"):
        assert figure in row, figure
    for gone in ("2.8 GB", "1.35 GB"):
        assert gone not in row, gone
    assert "build cache" in row
    assert "/tank" not in row


def test_install_credits_lrclib_and_names_the_setup_steps() -> None:
    readme = README.read_text(encoding="utf-8")
    section = readme[readme.index("## Install") : readme.index("## Run")]

    assert "https://github.com/tranxuanthang/lrclib" in section
    assert "https://lrclib.net/db-dumps" in section
    assert "scripts/lrclib-setup.sh" in section
    assert "plyrix doctor --lrclib-data" in section
    assert "docker compose build lrclib" in section
    assert "dump_id" in section


# Nothing tracked bundles or mirrors the dump (INST-06)


def tracked_files() -> list[Path]:
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    paths = [ROOT / os.fsdecode(name) for name in listing.split(b"\0") if name]
    return [path for path in paths if path.is_file()]


def dump_like(paths: list[Path]) -> list[Path]:
    return [
        path
        for path in paths
        if path.suffix.lower() in DUMP_SUFFIXES or "lrclib-db-dump-" in path.name
    ]


def foreign_dump_urls(text: str) -> list[str]:
    """Return URLs naming a dump on any host but lrclib.net's own."""
    return [
        url
        for url in URL.findall(text)
        if "db-dump" in url and urlsplit(url).hostname not in DUMP_HOSTS
    ]


def test_no_tracked_file_bundles_a_dump() -> None:
    files = tracked_files()
    assert ROOT / "compose.yaml" in files

    assert dump_like(files) == []
    offenders = {
        str(path.relative_to(ROOT)): urls
        for path in files
        if (urls := foreign_dump_urls(path.read_bytes().decode("utf-8", "replace")))
    }
    assert offenders == {}


@pytest.mark.parametrize(
    "name", ["x.gz", "data/db.sqlite3", "a.SQLITE", "state.db", "lrclib-db-dump-1.txt"]
)
def test_dump_like_control(name: str) -> None:
    assert dump_like([ROOT / name]) == [ROOT / name]


def test_dump_like_control_passes_ordinary_files() -> None:
    assert dump_like([ROOT / "README.md", ROOT / "tests/fixtures/t.flac"]) == []


def test_foreign_dump_urls_control() -> None:
    mirror = "https://" + "mirror.example" + "/db-dumps/x.sqlite3.gz"
    official = "https://db-dumps.lrclib.net/x.sqlite3.gz and https://lrclib.net/db-dumps."
    shell = "${LRCLIB_DUMP_BASE:-https://db-dumps.lrclib.net}/$name"

    assert foreign_dump_urls(f"see {mirror}") == [mirror]
    assert foreign_dump_urls(official) == []
    assert foreign_dump_urls(shell) == []


# The build context (INST-06, Pitfall 1)


def dockerignore_rules() -> list[str]:
    lines = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    """Translate one .dockerignore pattern: `**` spans any number of folders, `*` and `?` one."""
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def _parsed(rules: list[str]) -> list[tuple[bool, re.Pattern[str]]]:
    parsed = []
    for rule in rules:
        keep = rule.startswith("!")
        pattern = rule[1:] if keep else rule
        parsed.append((keep, _pattern_regex(pattern.strip("/"))))
    return parsed


def excluded(relpath: str, rules: list[str]) -> bool:
    """Say whether a context path is left out, modelling Docker's rules for simple patterns only.

    Rules apply in order and the last one matching wins. A rule matches a path if it matches the
    path or any folder above it, so a matched folder covers everything inside. `!` re-includes.
    Docker's own matcher has more cases than this; plan 03-07's image gate checks the built image.
    """
    parts = relpath.split("/")
    prefixes = ["/".join(parts[: n + 1]) for n in range(len(parts))]
    result = False
    for keep, regex in _parsed(rules):
        if any(regex.match(prefix) for prefix in prefixes):
            result = not keep
    return result


def build_context(root: Path, rules: list[str]) -> list[str]:
    """Return the files a build context at root would hold, as sorted relative POSIX paths."""
    reincluders = [rule[1:].strip("/") for rule in rules if rule.startswith("!")]
    kept = []
    for folder, dirs, files in os.walk(root):
        rel = Path(folder).relative_to(root).as_posix()
        base = "" if rel == "." else rel + "/"
        for name in list(dirs):
            path = base + name
            # A left-out folder no `!` rule could reach into is skipped whole, as Docker does.
            if excluded(path, rules) and not any(
                "**" in pattern or pattern.split("/")[0] == path.split("/")[0]
                for pattern in reincluders
            ):
                dirs.remove(name)
        kept += [base + name for name in files if not excluded(base + name, rules)]
    return sorted(kept)


def test_dockerignore_is_an_allow_list() -> None:
    assert dockerignore_rules() == [
        "*",
        "!src/",
        "!pyproject.toml",
        "!uv.lock",
        "!README.md",
        "!LICENSE",
        "!docker/",
        "**/__pycache__",
        "**/*.pyc",
    ]


def test_build_context_holds_only_allowed_files(tmp_path: Path) -> None:
    planted = [
        "src/plyrix/cli.py",
        "src/plyrix/__pycache__/x.pyc",
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "LICENSE",
        "docker/entrypoint.sh",
        "reference/x",
        ".planning/x",
        ".claude/x",
        "PROJECT-BRIEF.md",
        "CLAUDE.local.md",
        "secrets/plyrix_plex_token",
        "tests/conftest.py",
        ".venv/x",
        "dist/x",
    ]
    for name in planted:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x", encoding="utf-8")
    rules = dockerignore_rules()

    assert build_context(tmp_path, rules) == sorted(
        [
            "src/plyrix/cli.py",
            "pyproject.toml",
            "uv.lock",
            "README.md",
            "LICENSE",
            "docker/entrypoint.sh",
        ]
    )

    real = build_context(ROOT, rules)
    assert "src/plyrix/cli.py" in real
    assert "LICENSE" in real
    assert [p for p in real if p.split("/")[0] not in {"src", "docker"} and "/" in p] == []
    assert [p for p in real if p.startswith(("reference/", ".planning/", ".claude/"))] == []
    assert not {"PROJECT-BRIEF.md", "CLAUDE.local.md"} & set(real)


def test_the_matcher_control() -> None:
    rules = ["*", "!src/", "**/__pycache__"]

    assert excluded("x", rules)
    assert not excluded("src/a/b.py", rules)
    assert excluded("src/a/__pycache__/b.pyc", rules)
    assert excluded("__pycache__", ["**/__pycache__"])
    assert excluded("srcx/a", ["*", "!src/"])


# The package (INST-02, INST-06)

DIST_FORBIDDEN = re.compile(r"(^|/)(tests|reference|\.planning|__pycache__)(/|$)")
DIST_FORBIDDEN_SUFFIXES = (".gz", ".sqlite3", ".db", ".lrc", ".txt")
# The wheel's metadata, held to an exact list, so entry_points.txt is the only .txt allowed.
INFO_FILES = sorted(["METADATA", "RECORD", "WHEEL", "entry_points.txt", "licenses/LICENSE"])


def _uv() -> str:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    return uv


def _uv_env() -> dict[str, str]:
    """Pass UV_CACHE_DIR and TMPDIR through, and drop the outer run's venv so uv picks its own."""
    return {
        k: v for k, v in os.environ.items() if k not in {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"}
    }


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("dist")
    proc = subprocess.run(
        [_uv(), "build", "--out-dir", str(out)],
        cwd=ROOT,
        env=_uv_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr
    return out


def _forbidden(names: list[str]) -> list[str]:
    return [n for n in names if DIST_FORBIDDEN.search(n) or n.endswith(DIST_FORBIDDEN_SUFFIXES)]


def test_wheel_and_sdist_hold_only_plyrix(dist: Path) -> None:
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    assert [w.name for w in wheels] == [f"plyrix-{VERSION}-py3-none-any.whl"]
    assert [s.name for s in sdists] == [f"plyrix-{VERSION}.tar.gz"]

    with zipfile.ZipFile(wheels[0]) as wheel:
        # Folder entries carry no content, so only files count.
        wheel_names = [n for n in wheel.namelist() if not n.endswith("/")]
    with tarfile.open(sdists[0]) as sdist:
        sdist_names = [m.name for m in sdist.getmembers() if m.isfile()]

    sources = sorted(
        p.relative_to(ROOT / "src").as_posix() for p in (ROOT / "src" / "plyrix").rglob("*.py")
    )
    info_dir = f"plyrix-{VERSION}.dist-info/"
    in_wheel = sorted(n for n in wheel_names if not n.startswith(info_dir))
    assert in_wheel == sources
    info = sorted(n for n in wheel_names if n.startswith(info_dir))
    assert info == [f"{info_dir}{n}" for n in INFO_FILES]

    top = {n.split("/")[0] for n in sdist_names}
    assert top == {f"plyrix-{VERSION}"}
    # uv_build rewrites pyproject.toml in the sdist and keeps the original as pyproject.toml.orig.
    in_sdist = sorted(n.split("/", 1)[1] for n in sdist_names)
    assert in_sdist == sorted(
        ["PKG-INFO", "pyproject.toml", "pyproject.toml.orig", "README.md", "LICENSE"]
        + [f"src/{s}" for s in sources]
    )

    assert _forbidden(in_wheel) == []
    assert _forbidden(sdist_names) == []


def test_the_wheel_installs_and_runs(dist: Path, tmp_path: Path) -> None:
    """Install the wheel with uv.lock's runtime versions into a fresh venv and run it.

    This install checks no hashes, as `uv export --no-hashes` drops them; the image build's
    `uv sync --locked` is the path that verifies them.
    """
    uv = _uv()
    env = _uv_env()
    reqs = tmp_path / "requirements.txt"
    venv = tmp_path / "venv"

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*args], cwd=ROOT, env=env, capture_output=True, text=True, check=False, timeout=600
        )

    exported = run(
        uv,
        "export",
        "--frozen",
        "--no-dev",
        "--no-emit-project",
        "--no-hashes",
        "--output-file",
        str(reqs),
    )
    assert exported.returncode == 0, exported.stderr
    made = run(uv, "venv", "--python", sys.executable, str(venv))
    assert made.returncode == 0, made.stderr
    wheel = next(dist.glob("*.whl"))
    installed = run(
        uv, "pip", "install", "--python", str(venv / "bin" / "python"), "-r", str(reqs), str(wheel)
    )
    assert installed.returncode == 0, installed.stderr

    helped = run(str(venv / "bin" / "plyrix"), "--help")
    assert helped.returncode == 0, helped.stderr
    assert "plyrix" in helped.stdout


# Example systemd units (D-02)


def unit_lines(name: str) -> list[str]:
    text = (SYSTEMD / name).read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip() and line[0] not in "#;"]


def test_systemd_examples_chain_the_refresh() -> None:
    service = unit_lines("plyrix.service")
    timer = unit_lines("plyrix.timer")

    assert "Type=oneshot" in service
    starts = [line for line in service if line.startswith("ExecStart=")]
    assert len(starts) == 1 and starts[0].endswith("plyrix run")
    posts = [line for line in service if line.startswith("ExecStopPost=")]
    assert len(posts) == 1 and posts[0].endswith("plyrix plex-refresh")
    assert "IOSchedulingClass=idle" in service
    assert "Nice=19" in service
    assert [line for line in timer if line.startswith("OnCalendar=")]
    assert "Persistent=true" in timer


def test_the_update_service_runs_the_host_script() -> None:
    service = unit_lines("plyrix-lrclib-update.service")

    assert "Type=oneshot" in service
    starts = [line for line in service if line.startswith("ExecStart=")]
    assert len(starts) == 1 and starts[0].endswith("scripts/lrclib-update.sh")
    for line in (
        "IOSchedulingClass=idle",
        "Nice=19",
        "TimeoutStartSec=infinity",
        "SuccessExitStatus=75",
        "Environment=LRCLIB_FOLLOW_UP=1",
        "Environment=LRCLIB_PLEX_LIMIT=100",
    ):
        assert line in service, line
    data = [line for line in service if line.startswith("Environment=LRCLIB_DATA=")]
    assert len(data) == 1 and data[0].split("=", 2)[2].startswith("/srv/")


def test_the_update_timer_is_daily() -> None:
    timer = unit_lines("plyrix-lrclib-update.timer")

    for line in (
        "OnCalendar=daily",
        "RandomizedDelaySec=1h",
        "Persistent=true",
        "WantedBy=timers.target",
    ):
        assert line in timer, line


@pytest.mark.parametrize("name", ["plyrix-lrclib-update.service", "plyrix-lrclib-update.timer"])
def test_no_update_unit_names_dockers_socket(name: str) -> None:
    assert "docker.sock" not in (SYSTEMD / name).read_text(encoding="utf-8")


def test_readme_package_install() -> None:
    readme = README.read_text(encoding="utf-8")
    section = readme[readme.index("### Package") : readme.index("## Run")]

    assert "uv tool install plyrix\n" in section
    assert "`pipx install plyrix`" in section
    assert section.index("uv tool install plyrix\n") < section.index("uv build")
    assert "uv build" in section
    assert f"uv tool install dist/plyrix-{VERSION}-py3-none-any.whl" in section
    assert f"pipx install dist/plyrix-{VERSION}-py3-none-any.whl" in section
    assert "~/.config/systemd/user/" in section
    assert "systemctl --user enable --now plyrix.timer" in section
    assert "LoadCredential=plyrix_plex_token:" in section
    assert "chmod 600" in section


# The image definition (INST-01), read without building

SUPERCRONIC_URL = (
    "https://github.com/aptible/supercronic/releases/download/v0.2.51/supercronic-linux-$TARGETARCH"
)
SUPERCRONIC_SHA256 = {
    "amd64": "cc232108374e8e2b537045f2d5eaa1229f619db314c3ba6a14d38b35dc0aa123",
    "arm64": "4ae22576a78223d8ccdf5f3b3660a5cbb5288416bf0ca96afc4a36d195932b94",
}
DIGEST_REF = re.compile(r"^[a-z0-9./_-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")
# ionice -t runs plyrix even where setting an IO priority is refused.
CRON_LINE = "ionice -t -c3 nice -n19 plyrix run; plyrix plex-refresh"


def instructions() -> list[tuple[str, str]]:
    """Return the Dockerfile's instructions as (KEYWORD, arguments), continuations joined."""
    joined = re.sub(r"\\\n", " ", DOCKERFILE.read_text(encoding="utf-8"))
    out = []
    for line in joined.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            word, _, rest = stripped.partition(" ")
            out.append((word.upper(), rest.strip()))
    return out


def stages() -> dict[str, list[tuple[str, str]]]:
    """Split the instructions by FROM, keyed by stage name, the last stage as "runtime"."""
    found: list[tuple[str, list[tuple[str, str]]]] = []
    for word, rest in instructions():
        if word == "FROM":
            parts = rest.split()
            name = parts[-1] if len(parts) >= 3 and parts[-2].upper() == "AS" else ""
            found.append((name, []))
        found[-1][1].append((word, rest))
    named = {name: body for name, body in found[:-1]}
    named["runtime"] = found[-1][1]
    return named


def env_of(stage: list[tuple[str, str]]) -> dict[str, str]:
    values = {}
    for word, rest in stage:
        if word == "ENV":
            for pair in shlex.split(rest):
                key, _, value = pair.partition("=")
                values[key] = value
    return values


def test_every_base_image_is_pinned_by_digest() -> None:
    froms = [rest.split()[0] for word, rest in instructions() if word == "FROM"]

    assert len(froms) >= 2
    for ref in froms:
        assert DIGEST_REF.match(ref), ref
        assert ":latest" not in ref
    assert DOCKERFILE.read_text(encoding="utf-8").count("@sha256:") >= 4


def test_supercronic_is_pinned_by_sha256() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    runs = " ".join(rest for word, rest in instructions() if word == "RUN")

    assert f'"{SUPERCRONIC_URL}"' in runs
    for digest in SUPERCRONIC_SHA256.values():
        assert text.count(digest) == 1
    assert "sha256sum -c" in runs
    assert re.search(r"\*\)[^;]*;\s*exit 1", runs), "an unknown architecture must fail the build"


def test_the_image_runs_rootless() -> None:
    steps = instructions()
    users = [rest for word, rest in steps if word == "USER"]
    entrypoints = [rest for word, rest in steps if word == "ENTRYPOINT"]

    assert users[-1] == "1000:1000"
    assert len(entrypoints) == 1
    assert json.loads(entrypoints[0]) == ["/usr/bin/tini", "--", "plyrix-entrypoint"]
    assert [word for word, _ in steps if word == "CMD"] == []
    for path in (DOCKERFILE, ENTRYPOINT, COMPOSE):
        assert not re.search(r"PUID|PGID", path.read_text(encoding="utf-8")), path
    assert [word for word, _ in steps if word == "ADD"] == []
    assert not re.search(r"(?im)^#\s*syntax\s*=", DOCKERFILE.read_text(encoding="utf-8"))


def test_copies_name_only_allow_listed_paths() -> None:
    rules = dockerignore_rules()
    copies = [rest.split() for word, rest in instructions() if word == "COPY"]
    from_context = [args for args in copies if not any(a.startswith("--from=") for a in args)]

    assert from_context
    for args in from_context:
        sources = [a for a in args if not a.startswith("--")][:-1]
        assert sources
        for source in sources:
            assert source not in {".", "./", "*"}, args
            assert not excluded(source.rstrip("/"), rules), source


def test_the_build_stage_copies_the_licence() -> None:
    build = stages()["build"]
    assert ("COPY", "pyproject.toml uv.lock README.md LICENSE ./") in build


def test_the_venv_lives_where_it_runs() -> None:
    named = stages()
    build = named["build"]
    runtime = named["runtime"]

    assert env_of(build)["UV_PROJECT_ENVIRONMENT"] == "/opt/plyrix"
    runs = [rest for word, rest in build if word == "RUN"]
    assert [r for r in runs if "uv sync --locked --no-dev --no-editable" in r]
    env = env_of(runtime)
    assert env["PATH"].split(":")[0] == "/opt/plyrix/bin"
    assert env["XDG_CONFIG_HOME"] == "/config"
    assert env["XDG_STATE_HOME"] == "/state"
    assert env["CRON_SCHEDULE"] == "@weekly"
    assert ("COPY", "--from=build /opt/plyrix /opt/plyrix") in runtime


# The entrypoint (D-02, research D7), run with fakes for plyrix and supercronic

FAKE_PLYRIX = """#!/bin/sh
for arg in "$@"; do printf '%s\\n' "$arg" >> "$LOG_DIR/plyrix"; done
exit 3
"""
FAKE_SUPERCRONIC = """#!/bin/sh
echo "$*" >> "$LOG_DIR/supercronic"
if [ "$1" = -test ] && [ -n "${FAIL_TEST:-}" ]; then exit 4; fi
exit 0
"""


def run_entrypoint(
    tmp_path: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("plyrix", FAKE_PLYRIX), ("supercronic", FAKE_SUPERCRONIC)):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    full = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "LOG_DIR": str(tmp_path),
        "CRONTAB_FILE": str(tmp_path / "crontab"),
        **(env or {}),
    }
    return subprocess.run(
        ["sh", str(ENTRYPOINT), *args],
        env=full,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def logged(tmp_path: Path, name: str) -> list[str]:
    path = tmp_path / name
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_entrypoint_with_arguments_runs_plyrix(tmp_path: Path) -> None:
    proc = run_entrypoint(tmp_path, "run", "--dry-run")

    assert proc.returncode == 3
    assert logged(tmp_path, "plyrix") == ["run", "--dry-run"]
    assert logged(tmp_path, "supercronic") == []
    assert not (tmp_path / "crontab").exists()


def test_entrypoint_with_none_starts_cron_mode(tmp_path: Path) -> None:
    proc = run_entrypoint(tmp_path)
    crontab = tmp_path / "crontab"

    assert proc.returncode == 0, proc.stderr
    assert crontab.read_text(encoding="utf-8") == f"@weekly {CRON_LINE}\n"
    assert logged(tmp_path, "supercronic") == [f"-test {crontab}", str(crontab)]
    assert logged(tmp_path, "plyrix") == []


def test_entrypoint_takes_the_schedule(tmp_path: Path) -> None:
    proc = run_entrypoint(tmp_path, env={"CRON_SCHEDULE": "0 3 * * 1"})

    assert proc.returncode == 0, proc.stderr
    lines = (tmp_path / "crontab").read_text(encoding="utf-8").splitlines()
    assert lines == [f"0 3 * * 1 {CRON_LINE}"]


@pytest.mark.parametrize("schedule", ["@weekly\n* * * * * id", "@weekly\r* * * * * id", "\n"])
def test_entrypoint_refuses_a_schedule_of_more_than_one_line(tmp_path: Path, schedule: str) -> None:
    proc = run_entrypoint(tmp_path, env={"CRON_SCHEDULE": schedule})

    assert proc.returncode == 2
    assert len(proc.stderr.splitlines()) == 1, proc.stderr
    assert "CRON_SCHEDULE" in proc.stderr
    assert not (tmp_path / "crontab").exists()
    assert logged(tmp_path, "supercronic") == []


def test_entrypoint_stops_when_supercronic_rejects_the_file(tmp_path: Path) -> None:
    proc = run_entrypoint(tmp_path, env={"FAIL_TEST": "1"})
    crontab = tmp_path / "crontab"

    assert proc.returncode != 0
    assert logged(tmp_path, "supercronic") == [f"-test {crontab}"]


def test_entrypoint_is_valid_posix_sh() -> None:
    proc = subprocess.run(
        ["sh", "-n", str(ENTRYPOINT)], capture_output=True, text=True, check=False
    )

    assert proc.returncode == 0, proc.stderr
    assert ENTRYPOINT.read_text(encoding="utf-8").startswith("#!/bin/sh\n")


# compose.yaml's plyrix service (D-03, D-07, D-14, D-15, D-16)


def plyrix_service() -> dict[str, Any]:
    service = compose()["services"]["plyrix"]
    assert isinstance(service, dict)
    return service


def volume_parts(spec: str) -> list[str]:
    """Split a short-syntax volume on the colons outside ${...}."""
    return re.findall(r"(?:\$\{[^}]*\}|[^:$]|\$(?!\{))+", spec)


def plyrix_volumes() -> list[list[str]]:
    return [volume_parts(spec) for spec in plyrix_service()["volumes"]]


def test_volume_parts_control() -> None:
    assert volume_parts("${A:?set A}:/state") == ["${A:?set A}", "/state"]
    assert volume_parts("${A}:${A}:ro") == ["${A}", "${A}", "ro"]
    assert volume_parts("./config:/config:ro") == ["./config", "/config", "ro"]


def test_plyrix_is_built_locally() -> None:
    service = plyrix_service()

    assert service["build"] == "."
    assert service["image"] == "plyrix:local"
    assert service["pull_policy"] == "build"
    assert service["user"] == "${CONTAINER_USER:-1000:1000}"


def test_compose_mounts_the_music_root_once_writable_at_its_own_path() -> None:
    music = [v for v in plyrix_volumes() if "MUSIC_ROOT" in v[0] or "MUSIC_ROOT" in v[1]]

    assert len(music) == 1
    assert music[0][0] == music[0][1] == "${MUSIC_ROOT}"
    assert len(music[0]) == 2, "the library must be writable, as sidecars sit beside tracks"


def test_compose_mounts_state_and_config() -> None:
    by_target = {v[1]: v for v in plyrix_volumes()}

    assert by_target["/state"][0].startswith("${STATE_DIR:?")
    assert len(by_target["/state"]) == 2
    assert by_target["/config"] == ["./config", "/config", "ro"]
    assert len(plyrix_volumes()) == 4


def test_lrclib_data_is_visible_to_doctor() -> None:
    lrclib_data = [v for v in plyrix_volumes() if v[0] == "${LRCLIB_DATA}"]

    assert lrclib_data == [["${LRCLIB_DATA}", "${LRCLIB_DATA}", "ro"]]


def test_plyrix_reaches_lrclib_and_takes_the_token_as_a_secret() -> None:
    service = plyrix_service()
    env = service["environment"]

    assert env["PLYRIX_LRCLIB_URL"] == "http://lrclib:3300/api"
    assert env["PLYRIX_MUSIC_ROOT"].startswith("${MUSIC_ROOT:?")
    assert env["CRON_SCHEDULE"] == "${CRON_SCHEDULE:-@weekly}"
    # So the compose route of the Lidarr hook can drain its queue in cron mode.
    assert env["DRAIN_SCHEDULE"] == "${DRAIN_SCHEDULE:-}"
    assert not [name for name in env if "TOKEN" in name.upper()]
    assert service["secrets"] == ["plyrix_plex_token"]
    secret = compose()["secrets"]["plyrix_plex_token"]
    assert secret == {"file": "${PLEX_TOKEN_FILE:-./secrets/plyrix_plex_token}"}


def test_plyrix_waits_for_a_healthy_lrclib() -> None:
    service = plyrix_service()

    assert service["depends_on"] == {"lrclib": {"condition": "service_healthy"}}
    assert service["restart"] == "unless-stopped"


def test_secrets_folder_is_gitignored() -> None:
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "/secrets/" in lines


def test_readme_container_install() -> None:
    readme = README.read_text(encoding="utf-8")
    section = readme[readme.index("### Container") : readme.index("## Run")]
    flat = " ".join(section.split())

    for needed in (
        "docker compose build",
        "docker compose run --rm plyrix doctor",
        "docker compose run --rm plyrix run",
        "docker compose up -d",
        "CRON_SCHEDULE",
        "--no-deps",
        "MUSIC_ROOT",
        "STATE_DIR",
        "PLEX_TOKEN_FILE",
        "CONTAINER_USER",
        "config/plyrix/config.toml",
    ):
        assert needed in section, needed
    assert "readable by uid 1000" in flat
    assert f"ghcr.io/patrickclery/plyrix:{VERSION}" in flat
    assert "`linux/amd64` and `linux/arm64`" in flat
    assert "docker compose -f compose.yaml -f examples/compose.release.yaml" in flat
    assert "built locally" in flat and "published nowhere" in flat
    assert "writable" in flat


# The optional drain line, and the override for a containerized Lidarr (Phase 7 D-20)

OVERRIDE = ROOT / "examples" / "lidarr" / "compose.lidarr.yaml"
SPOOL_SHIM = ROOT / "examples" / "lidarr" / "lidarr-spool.sh"
DRAIN_LINE = "ionice -t -c3 nice -n19 plyrix drain"


def test_entrypoint_adds_a_drain_line_on_drain_schedule(tmp_path: Path) -> None:
    proc = run_entrypoint(tmp_path, env={"DRAIN_SCHEDULE": "*/5 * * * *"})

    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "crontab").read_text(encoding="utf-8").splitlines() == [
        f"@weekly {CRON_LINE}",
        f"*/5 * * * * {DRAIN_LINE}",
    ]


@pytest.mark.parametrize("env", [{}, {"DRAIN_SCHEDULE": ""}])
def test_entrypoint_without_a_drain_schedule_has_only_the_weekly_line(
    tmp_path: Path, env: dict[str, str]
) -> None:
    proc = run_entrypoint(tmp_path, env=env)

    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "crontab").read_text(encoding="utf-8") == f"@weekly {CRON_LINE}\n"


@pytest.mark.parametrize("schedule", ["*/5 * * * *\n* * * * * id", "*/5 * * * *\r* * * * * id"])
def test_entrypoint_refuses_a_drain_schedule_of_more_than_one_line(
    tmp_path: Path, schedule: str
) -> None:
    proc = run_entrypoint(tmp_path, env={"DRAIN_SCHEDULE": schedule})

    assert proc.returncode == 2
    assert len(proc.stderr.splitlines()) == 1, proc.stderr
    assert "DRAIN_SCHEDULE" in proc.stderr
    assert not (tmp_path / "crontab").exists()


def override() -> dict[str, Any]:
    loaded = yaml.safe_load(OVERRIDE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_override_shares_only_the_spool() -> None:
    loaded = override()

    assert list(loaded) == ["services"]
    assert list(loaded["services"]) == ["plyrix"]
    service = loaded["services"]["plyrix"]
    (volume,) = [volume_parts(spec) for spec in service["volumes"]]
    assert volume[1] == "/plyrix-spool"
    assert volume[0].startswith("${LIDARR_SPOOL:?")
    environment = service["environment"]
    assert environment["PLYRIX_LIDARR_SPOOL"] == "/plyrix-spool"
    assert environment["PLYRIX_LIDARR_PATH_MAP"] == '["${LIDARR_MUSIC:-/music}=${MUSIC_ROOT}"]'
    default = re.fullmatch(r"\$\{DRAIN_SCHEDULE:-(.+)\}", environment["DRAIN_SCHEDULE"])
    assert default is not None
    assert len(default.group(1).split()) == 5


def test_the_override_takes_no_plyrix_variable_and_no_socket() -> None:
    text = OVERRIDE.read_text(encoding="utf-8")
    names = re.findall(r"\$\{([A-Za-z0-9_]+)", text)

    assert [name for name in names if name.startswith("PLYRIX_")] == []
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    for line in code:
        assert "docker.sock" not in line
        assert "PUID" not in line and "PGID" not in line


def test_the_override_and_the_shim_agree_on_the_spool() -> None:
    assert "${PLYRIX_LIDARR_SPOOL:-/plyrix-spool}" in SPOOL_SHIM.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not on PATH")
def test_docker_compose_merges_the_override(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "LRCLIB_DATA": str(tmp_path / "lrclib"),
        "MUSIC_ROOT": str(tmp_path / "music"),
        "STATE_DIR": str(tmp_path / "state"),
        "LIDARR_SPOOL": str(tmp_path / "spool"),
    }
    proc = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE),
            "-f",
            str(OVERRIDE),
            "config",
            "--format",
            "json",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    plyrix = json.loads(proc.stdout)["services"]["plyrix"]
    assert len(plyrix["volumes"]) == 5
    path_map = json.loads(plyrix["environment"]["PLYRIX_LIDARR_PATH_MAP"])
    assert path_map == [f"/music={tmp_path / 'music'}"]
    assert plyrix["environment"]["DRAIN_SCHEDULE"] == "*/5 * * * *"


# The licence (D-13): MIT, carried into every artefact, and a runtime closure that fits it

PERMISSIVE = {"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0"}
# Exact (package, licence) pairs outside PERMISSIVE. None can be dropped and none is GPL; Patrick
# accepts or changes them at C2. A change of licence in any of them fails the test.
LICENCE_EXCEPTIONS = {
    ("certifi", "MPL-2.0"): "requests' CA bundle; MPL-2.0 is file-level copyleft, and an "
    "unmodified dependency doesn't extend it to plyrix",
    ("shellingham", "ISC"): "typer's shell detection; ISC is permissive, equivalent to MIT",
    ("typing-extensions", "PSF-2.0"): "pydantic's typing backports; PSF-2.0 is permissive",
}
TAG_WRITERS = {"mutagen", "mediafile", "beets"}
GPL_FAMILY = re.compile(r"\b(A|L)?GPL", re.IGNORECASE)
# Free-text License fields and classifier names, mapped to SPDX ids.
LICENCE_NAMES = {
    "MIT License": "MIT",
    "Apache Software License": "Apache-2.0",
    "Apache License 2.0": "Apache-2.0",
    "ISC License": "ISC",
    "ISC License (ISCL)": "ISC",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "BSD 3-Clause License": "BSD-3-Clause",
    "BSD 2-Clause License": "BSD-2-Clause",
}


def runtime_closure(lock: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Walk plyrix's runtime dependencies breadth first, never its dev group.

    Returns the packages that apply on this platform, and the entries skipped by their marker.
    """
    packages = {package["name"]: package for package in lock["package"]}
    queue = [(dep, "plyrix") for dep in packages["plyrix"].get("dependencies", [])]
    names: list[str] = []
    skipped: list[str] = []
    while queue:
        dep, parent = queue.pop(0)
        name = dep["name"]
        marker = dep.get("marker")
        if marker and not Marker(marker).evaluate():
            skipped.append(f"{name} (from {parent}: {marker})")
            continue
        if name in names:
            continue
        names.append(name)
        package = packages[name]
        children = list(package.get("dependencies", []))
        for extra in dep.get("extra", []):
            children += package.get("optional-dependencies", {}).get(extra, [])
        queue += [(child, name) for child in children]
    return names, skipped


def licence_from(expression: str | None, field: str | None, classifiers: list[str]) -> str | None:
    """License-Expression, else a one-line License field, else the License classifiers."""
    if expression and expression.strip():
        return expression.strip()
    if field and field.strip() and "\n" not in field.strip():
        return LICENCE_NAMES.get(field.strip(), field.strip())
    names = sorted(
        {
            LICENCE_NAMES.get(c.rsplit(" :: ", 1)[-1], c.rsplit(" :: ", 1)[-1])
            for c in classifiers
            if c.startswith("License :: ")
        }
    )
    return " OR ".join(names) if names else None


def installed_licence(name: str) -> str | None:
    meta = importlib.metadata.metadata(name)
    return licence_from(
        meta.get("License-Expression"), meta.get("License"), meta.get_all("Classifier") or []
    )


def licence_problem(name: str, licence: str | None) -> str | None:
    if name in TAG_WRITERS:
        return f"{name} is a tag writer and must never reach runtime"
    if licence is None:
        return f"{name} has no licence metadata"
    if GPL_FAMILY.search(licence):
        return f"{name} is {licence}, a GPL family licence"
    if (name, licence) in LICENCE_EXCEPTIONS:
        return None
    options = [part.strip(" ()") for part in re.split(r"\s+OR\s+", licence)]
    if any(option in PERMISSIVE for option in options):
        return None
    return f"{name} is {licence}, outside MIT, BSD and Apache-2.0 and not a named exception"


def closure_problems(
    lock: dict[str, Any], licence_of: Any = installed_licence
) -> tuple[list[str], list[str], list[str]]:
    names, skipped = runtime_closure(lock)
    problems = [p for n in names if (p := licence_problem(n, licence_of(n))) is not None]
    return names, skipped, problems


def test_runtime_licences_fit_mit() -> None:
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    names, skipped, problems = closure_problems(lock)

    assert problems == [], f"skipped on this platform: {skipped}"
    assert {"tinytag", "requests", "typer", "pydantic-settings"} <= set(names)
    assert not {"pytest", "mypy", "ruff", "responses", "pyyaml"} & set(names)
    # Every exception is still needed, so a stale one can't hide a later change.
    used = {(n, installed_licence(n)) for n in names}
    assert set(LICENCE_EXCEPTIONS) <= used


def planted_lock(runtime: list[str], dev: list[str] | None = None) -> dict[str, Any]:
    deps = ", ".join(f'{{ name = "{n}" }}' for n in runtime)
    dev_deps = ", ".join(f'{{ name = "{n}" }}' for n in dev or [])
    text = f"""
[[package]]
name = "plyrix"
version = "0.1.0"
source = {{ editable = "." }}
dependencies = [{deps}]

[package.dev-dependencies]
dev = [{dev_deps}]
"""
    for name in [*runtime, *(dev or [])]:
        text += f'\n[[package]]\nname = "{name}"\nversion = "1.0"\n'
    return tomllib.loads(text)


def planted_licences(licences: dict[str, str | None]) -> Any:
    return lambda name: licences[name]


@pytest.mark.parametrize(
    ("name", "licence"),
    [
        ("mutagen", "MIT"),
        ("mediafile", "MIT"),
        ("beets", "MIT"),
        ("gpl-thing", "GPL-2.0-or-later"),
        ("lgpl-thing", "LGPL-3.0"),
        ("agpl-thing", "AGPL-3.0-only"),
        ("other-mpl", "MPL-2.0"),
        ("bare", None),
        ("certifi", "MIT OR GPL-3.0"),
        ("shellingham", "MPL-2.0"),
    ],
)
def test_licence_closure_control_fails(name: str, licence: str | None) -> None:
    lock = planted_lock(["tinytag", name])
    _, _, problems = closure_problems(lock, planted_licences({"tinytag": "MIT", name: licence}))
    assert len(problems) == 1
    assert problems[0].startswith(name)


def test_licence_closure_control_passes_a_dev_only_gpl_package() -> None:
    lock = planted_lock(["tinytag"], dev=["gpl-thing"])
    names, _, problems = closure_problems(
        lock, planted_licences({"tinytag": "MIT", "gpl-thing": "GPL-3.0-only"})
    )
    assert names == ["tinytag"]
    assert problems == []


def test_licence_closure_control_skips_a_marker_that_does_not_apply() -> None:
    lock = tomllib.loads(
        """
[[package]]
name = "plyrix"
version = "0.1.0"
dependencies = [{ name = "winonly", marker = "sys_platform == 'nonesuch'" }]

[[package]]
name = "winonly"
version = "1.0"
"""
    )
    names, skipped, problems = closure_problems(lock, planted_licences({}))
    assert names == []
    assert skipped == ["winonly (from plyrix: sys_platform == 'nonesuch')"]
    assert problems == []


@pytest.mark.parametrize(
    ("expression", "field", "classifiers", "expected"),
    [
        ("MIT", None, [], "MIT"),
        (None, "ISC License", [], "ISC"),
        (None, "MPL-2.0", [], "MPL-2.0"),
        (None, None, ["License :: OSI Approved :: MIT License"], "MIT"),
        (None, None, ["License :: OSI Approved :: Apache Software License"], "Apache-2.0"),
        (None, "line one\nline two", ["License :: OSI Approved :: MIT License"], "MIT"),
        (None, None, ["Topic :: Multimedia"], None),
    ],
)
def test_licence_from_reads_each_metadata_shape(
    expression: str | None, field: str | None, classifiers: list[str], expected: str | None
) -> None:
    assert licence_from(expression, field, classifiers) == expected


def test_the_licence_is_mit_and_named_in_pyproject() -> None:
    text = LICENSE.read_text(encoding="utf-8")
    assert text.startswith("MIT License\n")
    assert "Copyright (c) 2026 Patrick Clery" in text
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    # PEP 639: with a licence expression there is no License classifier.
    assert [c for c in project.get("classifiers", []) if c.startswith("License ::")] == []
