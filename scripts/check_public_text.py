#!/usr/bin/env python3
"""Check public text against the publishing rules (Phase 5 D-03 to D-06, PUB-03).

One rule set for every public text: the README and governance files, the project site's source
and built pages, release notes, and the portfolio and profile text. A problem is one of:

  lyric-line  a line shaped like a timestamped lyric line, such as `[00:12.34] ...`
  number      a percentage, a comma-grouped count, or a KB, MB, GB or TB size not in ALLOWED
  name        an online lyrics source or another tool, by name; only LRCLIB and lrcget are named
  uniqueness  a claim that no other tool does this

Usage: check_public_text.py [--html] [PATH ...]
With no PATH it checks DEFAULT_PATHS. A folder is checked file by file. With --html, script and
style elements and tags are stripped first, so built site pages can be checked.
Exit 0 when clean, 1 with one `path:line: rule: excerpt` line per problem, 2 on a usage error.

Stdlib only, so tests, publish-check, CI and the external-repo gate share it as is.
"""

from __future__ import annotations

import html
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The same pattern as tests/test_lyric_leak.py's TIMESTAMPED_LINE; a test holds them equal.
TIMESTAMPED_LINE = re.compile(r"^\s*\[\d{1,2}:\d{2}([.:]\d{1,3})?\]")
# Synthetic placeholder lines, which tests/test_lyric_leak.py also accepts.
PLACEHOLDER_LINE = re.compile(r"placeholder line \d+|PLYRIX-CANARY-")

PERCENT = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s?%")
GROUPED = re.compile(r"(?<![\w.,])\d{1,3}(?:,\d{3})+(?![\w,]|\.\d)")
SIZE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s?(?:KB|MB|GB|TB)\b")
URL = re.compile(r"https?://\S+")

# Named only in this rule list, never in prose.
NAMES = re.compile(
    r"\b(genius|lyricfind|musixmatch|beets|tubifarry|mediafile|mutagen)\b|find lyrics",
    re.IGNORECASE,
)
UNIQUENESS = re.compile(
    r"\b(no other tool|no existing tool|the only tool|nothing else does|"
    r"no other project|the only project|first tool to|only tool that)\b",
    re.IGNORECASE,
)

QUOTABLE = "04-RESULTS.md Quotable for Phase 5"
FIRST_LOAD = f"{QUOTABLE}, first load, evidence/04-04-dump-load.md"
IMAGES = f"{QUOTABLE}, docs gap #8, evidence/04-08-clean-install.md"

# Every number public text may hold, normalised, with its source. Only facts about LRCLIB's dump,
# the disk it needs and plyrix's own images are measured numbers here; nothing measured on one
# person's library is public (05-06 decision C: no track counts, coverage, precision or sample
# sizes). A behaviour constant names the code constant it states.
ALLOWED: dict[str, str] = {
    # First load. "about 49 GB" is 49.3 GB rounded, quoted with "about".
    "49.3 GB": FIRST_LOAD,
    "49 GB": f"{FIRST_LOAD}, the 49.3 GB download rounded, always with 'about'",
    "319.8 GB": FIRST_LOAD,
    "129.6 GB": FIRST_LOAD,
    "80 GB": FIRST_LOAD,
    # Image sizes, two measures.
    "246 MB": IMAGES,
    "44 MB": IMAGES,
    "89.1 MB": IMAGES,
    "12.5 MB": IMAGES,
    # Behaviour constants.
    "64 KB": "behaviour constant: plyrix.dumps.PROBE_BYTES, the dump header probe",
    "320 GB": "behaviour constant: plyrix.doctor.FIRST_LOAD_BYTES, the 319.8 GB peak rounded up",
    "2 GB": "behaviour constant: plyrix.doctor.INDEX_ALLOWANCE_BYTES, the search index allowance",
}

DEFAULT_PATHS = [
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "SUPPORT.md",
    "CODE_OF_CONDUCT.md",
    "RELEASING.md",
    ".github/ISSUE_TEMPLATE/bug.yml",
    ".github/ISSUE_TEMPLATE/feature.yml",
    ".github/ISSUE_TEMPLATE/question.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/pull_request_template.md",
    "site/src",
]

# The docs pages generated from the README at build time, which the README already covers.
GENERATED = ("site/src/content/docs/docs/",)

TEXT_SUFFIXES = {
    ".md",
    ".mdx",
    ".astro",
    ".yml",
    ".yaml",
    ".txt",
    ".html",
    ".ts",
    ".mjs",
    ".js",
    ".css",
    ".json",
    ".svg",
}

SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
TAG = re.compile(r"<[^>]+>")
BLOCK_END = re.compile(r"</?(p|div|li|tr|h[1-6]|br|section|header|footer|pre|table)\b[^>]*>", re.I)


@dataclass(frozen=True)
class Problem:
    path: str
    line: int
    rule: str
    excerpt: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.excerpt}"


def normalise(number: str) -> str:
    """One space between a size and its unit, none before a percent sign."""
    number = re.sub(r"\s+", " ", number.strip())
    number = re.sub(r"\s%$", "%", number)
    return re.sub(r"(\d)(KB|MB|GB|TB)$", r"\1 \2", number)


def numbers(line: str) -> list[str]:
    text = URL.sub(" ", line)
    found = [m.group(0) for m in PERCENT.finditer(text)]
    found += [m.group(0) for m in GROUPED.finditer(text)]
    found += [m.group(0) for m in SIZE.finditer(text)]
    return [normalise(n) for n in found]


def html_to_text(text: str) -> str:
    text = SCRIPT_OR_STYLE.sub(" ", text)
    text = BLOCK_END.sub("\n", text)
    text = TAG.sub(" ", text)
    return html.unescape(text)


def check_text(text: str, path: str = "<text>", html_mode: bool = False) -> list[Problem]:
    if html_mode:
        text = html_to_text(text)
    problems = []
    for number, line in enumerate(text.splitlines(), start=1):
        excerpt = line.strip()[:120]
        if TIMESTAMPED_LINE.match(line) and not PLACEHOLDER_LINE.search(line):
            problems.append(Problem(path, number, "lyric-line", excerpt))
        for value in numbers(line):
            if value not in ALLOWED:
                problems.append(Problem(path, number, "number", f"{value} in: {excerpt}"))
        for match in NAMES.finditer(line):
            problems.append(Problem(path, number, "name", f"{match.group(0)} in: {excerpt}"))
        for match in UNIQUENESS.finditer(line):
            problems.append(Problem(path, number, "uniqueness", f"{match.group(0)} in: {excerpt}"))
    return problems


def files_under(path: Path, html_mode: bool) -> list[Path]:
    if path.is_file():
        return [path]
    suffixes = {".html"} if html_mode else TEXT_SUFFIXES
    return sorted(
        p
        for p in path.rglob("*")
        if p.is_file()
        and p.suffix.lower() in suffixes
        and "node_modules" not in p.parts
        and not display(p).startswith(GENERATED)
    )


def display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def check(paths: list[str | Path], html: bool = False) -> list[Problem]:
    problems = []
    for name in paths:
        path = Path(name)
        if not path.is_absolute() and not path.exists():
            path = ROOT / path
        if not path.exists():
            problems.append(Problem(str(name), 0, "missing", "no such file or folder"))
            continue
        for file in files_under(path, html):
            blob = file.read_bytes()
            if b"\0" in blob:
                continue
            text = blob.decode("utf-8", "replace")
            problems += check_text(text, display(file), html_mode=html)
    return problems


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    html_mode = False
    if args and args[0] == "--html":
        html_mode = True
        args = args[1:]
    if any(arg.startswith("-") for arg in args):
        print("usage: check_public_text.py [--html] [PATH ...]", file=sys.stderr)
        return 2
    paths: list[str | Path] = list(args) if args else list(DEFAULT_PATHS)
    problems = check(paths, html=html_mode)
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
