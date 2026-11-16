#!/usr/bin/env python3
"""Regenerate src/plyrix/_lay_folds.py. Usage: python3 -I gen_lay_folds.py <path-to-data_bmp.rs>.

Tests never call this: the output is committed, and tests pin its size and sample values. The
input is src/data_bmp.rs from the secular 1.0.1 crate, read as data, and nothing from the crate is
run. The table folds text the way the pinned LRCLIB server does, so plyrix can compare names as it
does.
"""

import re
import sys
from pathlib import Path

TABLE_SIZE = 65371
_SURROGATES = range(0xD800, 0xE000)
_ENTRY = re.compile(r"^\s*'\\u\{([0-9a-f]+)\}',\s*(?://.*)?$")
_OUTPUT = Path(__file__).resolve().parent.parent / "src" / "plyrix" / "_lay_folds.py"

_HEADER = '''\
"""Characters LRCLIB's name folding treats differently from str.lower(). Generated, don't edit.

Data from the secular crate, version 1.0.1, https://static.crates.io/crates/secular/secular-1.0.1.crate
whose Cargo.toml declares license = "MIT". Written by scripts/gen_lay_folds.py from its data_bmp.rs.
"""

'''


def table_entries(source: str) -> list[int]:
    """Return the code point each index of LAY_CHARS maps to, in order."""
    body = source[source.index("LAY_CHARS") :]
    body = body[body.index("\n") + 1 : body.index("\n];")]
    entries = []
    for line in body.split("\n"):
        match = _ENTRY.match(line)
        if match is None:
            raise SystemExit(f"unexpected line in LAY_CHARS: {line!r}")
        entries.append(int(match.group(1), 16))
    return entries


def escaped(text: str) -> str:
    """Return a string literal's body, so the file stays ASCII and ruff format leaves it alone."""
    return "".join(
        char if char.isascii() and char.isalnum() else f"\\u{ord(char):04x}" for char in text
    )


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    entries = table_entries(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if len(entries) != TABLE_SIZE:
        raise SystemExit(f"expected {TABLE_SIZE} entries in LAY_CHARS, found {len(entries)}")
    folds = {
        chr(index): chr(value)
        for index, value in enumerate(entries)
        if index not in _SURROGATES and chr(value) != chr(index).lower()
    }
    lines = [f'    "{escaped(key)}": "{escaped(value)}",' for key, value in folds.items()]
    _OUTPUT.write_text(
        _HEADER
        + f"TABLE_SIZE = {TABLE_SIZE}\n\n"
        + "FOLDS: dict[str, str] = {\n"
        + "\n".join(lines)
        + "\n}\n",
        encoding="ascii",
    )
    print(f"wrote {len(folds)} folds to {_OUTPUT}")


if __name__ == "__main__":
    main()
