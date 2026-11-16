"""Hold the README's `## Keeping LRCLIB current` section to the update scripts it documents."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
SCRIPTS = (ROOT / "scripts" / "lrclib-update.sh", ROOT / "scripts" / "lib" / "lrclib-dump.sh")
HEADING = "## Keeping LRCLIB current"


def section() -> str:
    readme = README.read_text(encoding="utf-8")
    start = readme.index(HEADING)
    return readme[start : readme.index("\n## ", start + len(HEADING))]


def table(text: str, heading: str) -> list[list[str]]:
    """Return the rows of the first table after heading, without its header and rule."""
    after = text[text.index(heading) :]
    rows = []
    for line in after.splitlines()[1:]:
        if line.startswith("|"):
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
        elif rows:
            break
    return rows[2:]


def test_the_section_sits_between_run_and_plex() -> None:
    readme = README.read_text(encoding="utf-8")

    assert readme.count(HEADING) == 1
    assert readme.index("## Run") < readme.index(HEADING) < readme.index("## Plex")


def test_the_section_holds_the_facts_a_self_hoster_needs() -> None:
    text = section()

    for fact in (
        "scripts/lrclib-update.sh",
        "plyrix-lrclib-update.timer",
        "@daily",
        "user_version",
        "1 to 8",
        "LRCLIB_UPDATE_RETRY=1",
        "rolled back",
        "--full",
        "plex-refresh --limit",
        "LRCLIB_FOLLOW_UP=1",
        "LRCLIB_PLEX_LIMIT",
        "index",
        "checksum",
        "starts and stops nothing",
        "Never touches the music library",
    ):
        assert fact in text, fact


def script_variables() -> set[str]:
    found: set[str] = set()
    for script in SCRIPTS:
        found |= set(re.findall(r"\$\{(LRCLIB_[A-Z_]+)", script.read_text(encoding="utf-8")))
    return found


def test_the_variable_table_is_exactly_what_the_scripts_read() -> None:
    rows = table(section(), "### Update variables")
    named = {row[0].strip("`") for row in rows}

    assert script_variables() - {"LRCLIB_DATA"} <= named
    assert {name for name in named if name.startswith("LRCLIB_")} == script_variables()
    assert script_variables() >= {"LRCLIB_FOLLOW_UP", "LRCLIB_PLEX_LIMIT", "LRCLIB_STATE_WAIT"}


def test_the_exit_codes_are_the_scripts() -> None:
    rows = table(section(), "### Update exit codes")

    assert {int(row[0]) for row in rows} == {0, 1, 2, 3, 75}


def test_the_section_names_no_private_path() -> None:
    text = section()

    for private in ("/tank", "/home", "tailnet"):
        assert private not in text


def test_every_unit_the_section_names_exists() -> None:
    names = set(re.findall(r"plyrix-lrclib-update\.(?:service|timer)", section()))

    assert names == {"plyrix-lrclib-update.service", "plyrix-lrclib-update.timer"}
    for name in names:
        assert (ROOT / "examples" / "systemd" / name).is_file()


def test_the_install_section_points_at_the_update() -> None:
    readme = README.read_text(encoding="utf-8")
    install = readme[readme.index("## Install") : readme.index("## Run")]

    assert "records the loaded dump" in install
    assert "Keeping LRCLIB current" in install
