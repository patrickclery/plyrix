"""Write the plyrix brand SVGs from geometry constants. Usage: make_brand.py OUT_DIR

Called by make_brand.sh, never by tests. Stdlib only. Every bar edge sits on a whole grid unit,
so the small renders stay sharp. The wordmark is Fira Sans SemiBold, turned into one outline path
by rsvg-convert, so no font file ships. The output is byte-for-byte deterministic.
"""

import math
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

# D-04 palette, picked by Patrick.
TILE = "#115e59"
LINE = "#ffffff"
LINE_OPACITY = "0.9"
HIGHLIGHT = "#a3ff3c"

# A bar is (y, height, width); every bar starts at the same x, and the third is the highlight.
# All bars share one height and one gap (Patrick, 2026-11-15: equal heights, equal spacing).
Bar = tuple[int, int, int]

# Master on a 32-unit grid: the closest whole-unit snap of sketch L1 (see the brand notes).
MASTER_GRID = 32
MASTER_TILE_RX = "7"
MASTER_X = 6
MASTER_BARS: tuple[Bar, ...] = ((7, 3, 17), (12, 3, 13), (17, 3, 21), (22, 3, 15))

# 16 px variant on a 16-unit grid. Halving the master puts its 1-unit gap on half a pixel, which
# blends the second and third bars into one run at 16 px, so the small icon has its own snap.
SMALL_GRID = 16
SMALL_TILE_RX = "3.5"
SMALL_X = 3
SMALL_BARS: tuple[Bar, ...] = ((2, 2, 9), (5, 2, 7), (8, 2, 11), (11, 2, 8))


# Wordmark (D-03): lowercase, Fira Sans SemiBold 4.301 (SIL OFL 1.1), drawn as Pango shapes it.
WORDMARK = "plyrix"
FONT_FAMILY = "Fira Sans"
FONT_WEIGHT = 600
# fontconfig must resolve both queries to exactly this face, or it would silently substitute.
FONT_QUERIES = ("Fira Sans:style=SemiBold", "Fira Sans:weight=180")
FONT_EXPECTED = "Fira Sans|SemiBold|281870"
# Outlines are taken at a large size and scaled down, so Pango's glyph-position rounding stays
# far below the 0.01-unit precision of the output.
MEASURE_SIZE = 1000
# Cap height of the wordmark: the "l" ascender meets the top of bar 1 and the baseline the bottom
# of bar 4, so the word sits on the lyric lines.
WORDMARK_TOP = MASTER_BARS[0][0]
WORDMARK_BASELINE = MASTER_BARS[-1][0] + MASTER_BARS[-1][1]
# Clear space from the tile to the wordmark: the tile's corner radius plus one unit.
WORDMARK_GAP = int(MASTER_TILE_RX) + 1
WORDMARK_LIGHT = TILE
WORDMARK_DARK = LINE

_PATH_TOKEN = re.compile(r"[A-Za-z]|-?(?:\d+\.?\d*|\.\d+)(?:e-?\d+)?")


def _num(value: float) -> str:
    return f"{value:g}"


def _coord(value: float) -> str:
    """Round to 2 decimals and drop trailing zeros."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def mark_rects(grid: int, tile_rx: str, x: int, bars: tuple[Bar, ...]) -> list[str]:
    """Return the tile and four left-aligned rounded bars as rect elements."""
    lines = [f'<rect width="{grid}" height="{grid}" rx="{tile_rx}" fill="{TILE}"/>']
    for index, (y, height, width) in enumerate(bars):
        if index == 2:
            fill = f'fill="{HIGHLIGHT}"'
        else:
            fill = f'fill="{LINE}" fill-opacity="{LINE_OPACITY}"'
        lines.append(
            f'<rect x="{x}" y="{y}" width="{width}" height="{height}"'
            f' rx="{_num(height / 2)}" {fill}/>'
        )
    return lines


def _svg(width: int, height: int, body: list[str]) -> str:
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}"'
        f' width="{width}" height="{height}">',
        "<title>plyrix</title>",
        *body,
        "</svg>",
    ]
    return "\n".join(lines) + "\n"


def mark_svg(grid: int, tile_rx: str, x: int, bars: tuple[Bar, ...]) -> str:
    """Return the mark as SVG text: a tile and four left-aligned rounded bars."""
    return _svg(grid, grid, mark_rects(grid, tile_rx, x, bars))


def check_font() -> None:
    """Exit 2 unless fontconfig resolves Fira Sans SemiBold 4.301 for every query."""
    for query in FONT_QUERIES:
        found = subprocess.run(
            ["fc-match", "-f", "%{family[0]}|%{style[0]}|%{fontversion}", query],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        if found != FONT_EXPECTED:
            print(
                f"make_brand.py: Fira Sans SemiBold 4.301 is needed; fontconfig resolves"
                f" {query!r} to {found!r} and would substitute another font",
                file=sys.stderr,
            )
            sys.exit(2)


def outline(text: str) -> list[str | tuple[float, float]]:
    """Return the text as path tokens: absolute M/L/C/Z letters and (x, y) points.

    The text is set at MEASURE_SIZE with its baseline at y = MEASURE_SIZE.
    """
    size = MEASURE_SIZE
    source = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size * 8}" height="{size * 2}">'
        f'<text x="0" y="{size}" font-family="{FONT_FAMILY}" font-weight="{FONT_WEIGHT}"'
        f' font-size="{size}" fill="#000000">{text}</text></svg>'
    )
    with tempfile.TemporaryDirectory(prefix="plyrix-wordmark.") as tmp:
        src = Path(tmp) / "text.svg"
        src.write_text(source)
        result = subprocess.run(
            ["rsvg-convert", "-f", "svg", str(src)], capture_output=True, check=True
        )
    root = ET.fromstring(result.stdout)
    names = [element.tag.rsplit("}", 1)[-1] for element in root.iter()]
    if names != ["svg", "path"]:
        sys.exit(f"make_brand.py: rsvg-convert wrote {names}, not one svg and one path")
    d = root[0].get("d", "")
    raw = _PATH_TOKEN.findall(d)
    if "".join(raw) != re.sub(r"[\s,]", "", d):
        sys.exit("make_brand.py: the outline holds tokens other than commands and numbers")
    tokens: list[str | tuple[float, float]] = []
    numbers: list[float] = []
    for token in raw:
        if token.isalpha():
            if token not in {"M", "L", "C", "Z"}:
                sys.exit(f"make_brand.py: unexpected path command {token!r} in the outline")
            if numbers:
                sys.exit("make_brand.py: an odd count of coordinates in the outline")
            tokens.append(token)
        else:
            numbers.append(float(token))
            if len(numbers) == 2:
                tokens.append((numbers[0], numbers[1]))
                numbers = []
    if numbers:
        sys.exit("make_brand.py: an odd count of coordinates in the outline")
    # Cairo ends the path with a bare moveto to its current point, which draws nothing. Drop it,
    # so it cannot skew the measured extent.
    while len(tokens) >= 2 and tokens[-2] == "M":
        del tokens[-2:]
    return tokens


def wordmark() -> tuple[str, int]:
    """Return the placed wordmark's d attribute and the lockup width it needs."""
    tokens = outline(WORDMARK)
    points = [t for t in tokens if isinstance(t, tuple)]
    left = min(x for x, _ in points)
    top = min(y for _, y in points)
    scale = (WORDMARK_BASELINE - WORDMARK_TOP) / (MEASURE_SIZE - top)
    dx = MASTER_GRID + WORDMARK_GAP - left * scale
    dy = WORDMARK_BASELINE - MEASURE_SIZE * scale
    parts: list[str] = []
    placed: list[tuple[float, float]] = []
    for token in tokens:
        if isinstance(token, str):
            parts.append(token)
        else:
            x = round(token[0] * scale + dx, 2)
            y = round(token[1] * scale + dy, 2)
            placed.append((x, y))
            parts.append(f"{_coord(x)} {_coord(y)}")
    top_y = min(y for _, y in placed)
    bottom_y = max(y for _, y in placed)
    if abs(top_y - WORDMARK_TOP) > 0.25:
        sys.exit(f"make_brand.py: the wordmark top is {top_y}, not {WORDMARK_TOP}")
    if bottom_y > MASTER_GRID:
        sys.exit(f"make_brand.py: the descenders reach y {bottom_y}, below {MASTER_GRID}")
    width = math.ceil(max(x for x, _ in placed))
    return " ".join(parts), width


def lockup_svg(d: str, width: int, fill: str) -> str:
    """Return the mark with the outlined wordmark beside it."""
    body = mark_rects(MASTER_GRID, MASTER_TILE_RX, MASTER_X, MASTER_BARS)
    body.append(f'<path d="{d}" fill="{fill}"/>')
    return _svg(width, MASTER_GRID, body)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: make_brand.py OUT_DIR", file=sys.stderr)
        return 2
    out_dir = Path(argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    master = mark_svg(MASTER_GRID, MASTER_TILE_RX, MASTER_X, MASTER_BARS)
    small = mark_svg(SMALL_GRID, SMALL_TILE_RX, SMALL_X, SMALL_BARS)
    (out_dir / "mark.svg").write_bytes(master.encode())
    (out_dir / "mark-16.svg").write_bytes(small.encode())
    check_font()
    d, width = wordmark()
    (out_dir / "lockup.svg").write_bytes(lockup_svg(d, width, WORDMARK_LIGHT).encode())
    (out_dir / "lockup-dark.svg").write_bytes(lockup_svg(d, width, WORDMARK_DARK).encode())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
