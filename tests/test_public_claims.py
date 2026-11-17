"""Hold public text to the publishing rules, through the one checker publish-check also runs.

scripts/check_public_text.py is loaded from its file, so the tests, the gate, CI and the
external-repo checks share one rule set (D-03 to D-06, PUB-03).
"""

import importlib
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from test_lyric_leak import TIMESTAMPED_LINE

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_public_text.py"


def load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_public_text", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = load_checker()


def rules(text: str, html: bool = False) -> list[str]:
    return [problem.rule for problem in checker.check_text(text, html_mode=html)]


def test_the_lyric_pattern_is_the_leak_tests_pattern() -> None:
    assert checker.TIMESTAMPED_LINE.pattern == TIMESTAMPED_LINE.pattern


@pytest.mark.parametrize(
    "line", ["[00:12.34] some words", "  [1:02] words", "[03:04:567] words", "[12:00]"]
)
def test_a_timestamped_line_is_flagged(line: str) -> None:
    assert rules(line) == ["lyric-line"]


def test_a_placeholder_line_and_prose_brackets_pass() -> None:
    assert rules("[00:01.00] placeholder line 1") == []
    assert rules("see [the docs](x) and [12 tracks]") == []


@pytest.mark.parametrize(
    "line",
    ["a 49.3 GB download", "about 49 GB", "a 319.8 GB peak", "about 319.8GB", "a 246 MB image"],
)
def test_an_allowed_number_passes(line: str) -> None:
    assert rules(line) == []


@pytest.mark.parametrize("line", ["12,345 tracks", "42.4% right", "needs 999 GB", "a 7 TB disk"])
def test_an_unlisted_number_is_flagged(line: str) -> None:
    assert rules(line) == ["number"]


# 05-06 decision C: nothing measured on the maintainer's library is public. These are the
# figures the allow-list used to hold: coverage, adoption agreement, precision and recall.
LIBRARY_FIGURES = [
    "22,169",
    "14,416",
    "65.0%",
    "2,577",
    "3,084",
    "1,323",
    "11,669",
    "2,747",
    "99.0%",
    "95%",
    "97.5%",
    "94.2%",
    "98.9%",
    "96.5%",
    "10%",
    "72.7%",
]


@pytest.mark.parametrize("value", LIBRARY_FIGURES)
def test_no_library_figure_is_allowed(value: str) -> None:
    assert value not in checker.ALLOWED
    assert rules(f"plyrix got {value} on one library") == ["number"]


def test_numbers_inside_urls_and_plain_digits_are_not_counted() -> None:
    assert rules("https://example.invalid/a%20b?x=1,000 and 1000 tracks and 16K records") == []


def test_every_allowed_number_has_a_source() -> None:
    assert checker.ALLOWED
    for value, source in checker.ALLOWED.items():
        assert source.strip(), value
        if source.startswith("behaviour constant: "):
            continue
        assert "04-RESULTS.md" in source, value
        assert "evidence/04-0" in source, value


def test_every_behaviour_constant_names_a_real_code_constant() -> None:
    constants = [
        source.removeprefix("behaviour constant: ").split(",")[0]
        for source in checker.ALLOWED.values()
        if source.startswith("behaviour constant: ")
    ]
    assert constants
    for dotted in constants:
        module_name, _, attribute = dotted.rpartition(".")
        module = importlib.import_module(module_name)
        assert hasattr(module, attribute), dotted


def test_the_behaviour_constants_state_what_the_code_does() -> None:
    import plyrix.doctor
    import plyrix.dumps

    assert plyrix.dumps.PROBE_BYTES == 64 * 1024
    assert plyrix.doctor.INDEX_ALLOWANCE_BYTES == 2 * 10**9


def test_doctors_first_load_is_the_measured_peak_rounded_up() -> None:
    import plyrix.doctor

    assert plyrix.doctor.FIRST_LOAD_BYTES == 320 * 10**9


@pytest.mark.parametrize(
    "line",
    [
        "fetches from Genius",
        "like beets does",
        "LyricFind too",
        "via MUSIXMATCH",
        "Tubifarry users",
        "mediafile and mutagen",
        "turn on Find Lyrics",
    ],
)
def test_other_sources_and_tools_are_flagged(line: str) -> None:
    assert set(rules(line)) == {"name"}


def test_lrclib_and_lrcget_pass() -> None:
    assert rules("LRCLIB serves the lyrics; lrcget is the GUI option.") == []


@pytest.mark.parametrize(
    "line",
    [
        "No other tool does this.",
        "There is no existing tool for it.",
        "plyrix is the only tool that writes sidecars.",
        "Nothing else does this.",
    ],
)
def test_uniqueness_claims_are_flagged(line: str) -> None:
    assert "uniqueness" in rules(line)


def test_html_mode_strips_script_style_and_tags() -> None:
    page = (
        "<html><head><style>.x{width:42.4%}</style><script>var n='12,345';</script></head>"
        "<body><p>a 49.3 GB download</p><p>[00:12.34] words</p></body></html>"
    )
    assert rules(page, html=True) == ["lyric-line"]
    assert "number" in rules("<p>42.4% right</p>", html=True)


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_cli_reports_each_problem_and_exits_1(tmp_path: Path) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.md").write_text("clean\n42.4% right\n", encoding="utf-8")
    (folder / "b.mdx").write_text("ok\n", encoding="utf-8")
    result = run_cli(str(folder))
    assert result.returncode == 1
    assert result.stdout.strip().endswith("a.md:2: number: 42.4% in: 42.4% right")
    assert len(result.stdout.strip().splitlines()) == 1


def test_the_cli_exits_0_on_clean_files(tmp_path: Path) -> None:
    path = tmp_path / "clean.md"
    path.write_text("a 49.3 GB dump, measured 2026-11-11\n", encoding="utf-8")
    assert run_cli(str(path)).returncode == 0


def test_the_cli_refuses_a_bad_flag() -> None:
    assert run_cli("--bogus").returncode == 2


def test_a_missing_path_is_a_problem(tmp_path: Path) -> None:
    assert [p.rule for p in checker.check([tmp_path / "nope.md"])] == ["missing"]


def test_every_default_path_exists() -> None:
    for name in checker.DEFAULT_PATHS:
        assert (ROOT / name).exists(), name


def test_public_docs_pass() -> None:
    problems = checker.check(list(checker.DEFAULT_PATHS))
    assert problems == [], "\n".join(str(p) for p in problems)


def test_the_governance_docs_pass() -> None:
    names = [n for n in checker.DEFAULT_PATHS if n != "README.md"]
    problems = checker.check(names)
    assert problems == [], "\n".join(str(p) for p in problems)


# The README itself (PUB-01, PUB-03, PUB-06, PUB-11, D-12 to D-15, D-24)

README = ROOT / "README.md"
LEAD = (
    "Why rely on lyrics services that track your metadata? "
    "Download the entire LRCLIB database once and host it locally."
)
DISCLAIMER = "plyrix is an independent project, not affiliated with or endorsed by Plex, Inc."
SOURCE_NOTE = re.compile(r"measured \d{4}-\d{2}-\d{2}", re.IGNORECASE)


def readme() -> str:
    return README.read_text(encoding="utf-8")


def blocks(text: str) -> list[str]:
    """Paragraphs and tables, split on blank lines, with fenced code left out."""
    found: list[str] = []
    current: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if line.strip():
            current.append(line)
        elif current:
            found.append("\n".join(current))
            current = []
    if current:
        found.append("\n".join(current))
    return found


def section(heading: str) -> str:
    text = readme()
    start = text.index(f"\n## {heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def measured_values() -> set[str]:
    return {
        value
        for value, source in checker.ALLOWED.items()
        if not source.startswith("behaviour constant: ")
    }


def unsourced_blocks(text: str) -> list[str]:
    """Blocks holding a measured number with no dated source note in reach.

    A paragraph needs the note itself. A table may carry it, or have it on the line before or
    after it, as its title line or note line.
    """
    found = blocks(text)
    measured = measured_values()
    problems = []
    for index, block in enumerate(found):
        if not any(value in measured for value in checker.numbers(block)):
            continue
        reach = [block]
        if all(line.startswith("|") for line in block.splitlines()):
            reach += found[max(index - 1, 0) : index] + found[index + 1 : index + 2]
        if not any(SOURCE_NOTE.search(part) for part in reach):
            problems.append(block[:120])
    return problems


def test_the_readme_opens_with_the_lockup_and_the_lead() -> None:
    lines = readme().splitlines()
    assert lines[0] == "# plyrix"
    assert lines[1] == ""
    assert lines[2:7] == [
        "<picture>",
        '  <source media="(prefers-color-scheme: dark)" srcset="assets/brand/lockup-dark.svg">',
        '  <img src="assets/brand/lockup.svg" alt="plyrix">',
        "</picture>",
        "",
    ]
    assert lines[7] == LEAD
    assert "PLYRIX-TEXT-HEADER" not in readme()
    for name in ("lockup.svg", "lockup-dark.svg"):
        assert (ROOT / "assets" / "brand" / name).is_file()


def test_the_readme_states_its_scope_and_drops_the_banner() -> None:
    text = readme()
    assert "Tested on ZFS on Linux only" in blocks(text)[3]
    assert "pre-release" not in text.lower()
    assert "## Example: a retry pass" not in text


def test_the_readme_says_users_download_the_dump_and_holds_no_library_figures() -> None:
    text = readme()
    lead = " ".join(blocks(text)[3].split())
    assert "ships no lyrics and no data" in lead
    assert "you download LRCLIB's database dump yourself" in lead
    assert "https://lrclib.net/db-dumps" in lead
    assert "\n## Results\n" not in text
    for word in ["precision", "wilson", "labelled sample", "archive folder"]:
        assert word not in text.lower(), word


def test_every_measured_number_has_a_dated_source_note() -> None:
    assert unsourced_blocks(readme()) == []


def test_the_source_note_rule_control() -> None:
    text = "Peaks at 319.8 GB.\n\n| a | b |\n|---|---|\n| x | 49.3 GB |\n"
    assert len(unsourced_blocks(text)) == 2
    sourced = (
        "Peaks at 319.8 GB, measured 2026-11-11.\n\n"
        "| a |\n|---|\n| 49.3 GB |\n\nMeasured 2026-11-11."
    )
    assert unsourced_blocks(sourced) == []


def test_related_projects_names_exactly_lrclib_and_lrcget() -> None:
    links = re.findall(r"https?://[^\s)>\]]+", section("Related projects"))
    assert links == [
        "https://github.com/tranxuanthang/lrclib",
        "https://github.com/tranxuanthang/lrcget",
    ]


def test_credits_name_lrclib_its_licence_and_the_dump_stance() -> None:
    text = " ".join(section("Credits").split())
    assert "LRCLIB and its contributors" in text
    assert "MIT" in text
    assert "builds it from source" in text and "doesn't link it" in text
    assert "never bundles or mirrors the dump" in text


def test_lyrics_and_legal_gives_the_stance_and_the_routes() -> None:
    text = " ".join(section("Lyrics and legal").split())
    assert "ships no lyrics" in text
    assert "https://lrclib.net/db-dumps" in text
    assert "doesn't redistribute" in text
    assert "LRCLIB's terms" in text
    assert "GitHub issue" in text
    assert "https://github.com/patrickclery/plyrix/security/advisories/new" in text
    assert "takedown" not in text.lower()


def test_the_license_section_and_the_footer() -> None:
    text = section("License")
    assert "MIT" in text and "[LICENSE](LICENSE)" in text
    assert blocks(readme())[-1] == DISCLAIMER
    assert readme().count(DISCLAIMER) == 1


# The site's source is in the checker's scope (05-05)


def test_the_site_source_is_in_the_default_scope_and_passes() -> None:
    assert "site/src" in checker.DEFAULT_PATHS
    problems = checker.check(["site/src"])
    assert problems == [], "\n".join(str(p) for p in problems)


def test_a_planted_number_in_site_source_is_flagged(tmp_path: Path) -> None:
    page = tmp_path / "src" / "content" / "docs" / "planted.mdx"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntitle: x\n---\n\nMatches 42.4% more.\n", encoding="utf-8")
    assert [p.rule for p in checker.check([tmp_path / "src"])] == ["number"]


def test_the_generated_docs_pages_are_not_checked_twice() -> None:
    # They are the README split up, and the README is already in scope.
    files = checker.files_under(ROOT / "site" / "src", html_mode=False)
    assert not [f for f in files if "content/docs/docs" in f.as_posix()]
