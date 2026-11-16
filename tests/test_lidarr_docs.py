"""The README's Lidarr section holds the facts a user needs to set the hook up, and no others."""

import re
from dataclasses import fields
from pathlib import Path

from plyrix import hook, lidarr

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
REQUIRED = [
    "On Release Import",
    "On Upgrade",
    "Test",
    "qBittorrent",
    "lidarr-spool.sh",
    "plyrix-hook.sh",
    "plyrix-hook-compose.sh",
    "compose.lidarr.yaml",
    "--pull never",
    "--no-deps",
    "PLYRIX_LIDARR_PATH_MAP",
    "plyrix drain",
    "DRAIN_SCHEDULE",
    "lidarr_budget",
    "LyricExtractor",
    "3.1.0.4875",
    "2026-11-10",
    "Docker socket",
    "Import Extra Files",
    "failed/*.event",
]
# The names Lidarr's Test event sets beside Lidarr_EventType.
TEST_ONLY = {"Lidarr_InstanceName", "Lidarr_ApplicationUrl"}


def readme() -> str:
    return README.read_text(encoding="utf-8")


def lidarr_section() -> str:
    text = readme()
    start = text.index("\n## Lidarr\n")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def exit_codes(heading: str) -> set[int]:
    section = lidarr_section()
    table = section[section.index(f"\n### {heading}\n") :]
    following = table.find("\n### ", 1)
    if following != -1:
        table = table[:following]
    return {int(code) for code in re.findall(r"^\| (\d+) \|", table, re.MULTILINE)}


def test_the_lidarr_section_sits_after_plex() -> None:
    text = readme()

    assert text.count("\n## Lidarr\n") == 1
    assert text.index("\n## Plex\n") < text.index("\n## Lidarr\n")
    assert text.index("\n## Lidarr\n") < text.index("\n## Related projects\n")


def test_the_lidarr_section_holds_every_required_fact() -> None:
    section = lidarr_section()

    missing = [fact for fact in REQUIRED if fact not in section]
    assert missing == []


def test_the_lidarr_section_names_only_shims_that_exist() -> None:
    named = set(re.findall(r"examples/lidarr/[\w.-]+", lidarr_section()))

    assert named
    for path in named:
        assert (ROOT / path).is_file(), path


def test_the_lidarr_section_spells_lidarr_names_exactly() -> None:
    section = lidarr_section()
    names = set(re.findall(r"\bLidarr_[A-Za-z_]+", section))
    lowercase = set(re.findall(r"`(lidarr_[a-z_]+)`", section)) - {"lidarr_budget", "lidarr_spool"}
    lowercase -= {"lidarr_path_map"}

    assert names
    assert names <= set(lidarr.KEYS) | TEST_ONLY
    # Lidarr on Linux really sets the names in lowercase, which the section says.
    assert lowercase
    assert lowercase <= set(lidarr.LOWERCASE_KEYS)


def test_lidarr_hook_exit_codes() -> None:
    assert exit_codes("Lidarr hook exit codes") == {0, 1, 2}


def test_drain_exit_codes() -> None:
    assert exit_codes("Drain exit codes") == {0, 1, 2, 3, 4, 70}


def test_docs_name_every_drain_count() -> None:
    section = lidarr_section()

    for field in fields(hook.DrainCounts):
        if field.compare:
            assert f"`{field.name}`" in section, field.name


def test_the_host_route_says_how_the_compose_service_drains() -> None:
    section = lidarr_section()
    route = section[section.index("**Lidarr on the host.**") : section.index("**plyrix inside")]

    assert "*/5 * * * * plyrix drain" in route
    assert "DRAIN_SCHEDULE" in route
