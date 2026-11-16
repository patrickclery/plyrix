"""BRAND-01 and BRAND-02: the brand files are the right size, safe to serve and clear of Plex.

Stdlib only. The files are built by scripts/make_brand.sh, which these tests never call.
"""

import colorsys
import re
import struct
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "assets" / "brand"
SVG_NS = "{http://www.w3.org/2000/svg}"

PNG_SIZES = {"mark-16.png": 16, "mark-32.png": 32, "mark-180.png": 180, "mark-512.png": 512}
EXPECTED_FILES = {
    "mark.svg",
    "mark-16.svg",
    "mark-16.png",
    "mark-32.png",
    "mark-180.png",
    "mark-512.png",
    "favicon.ico",
    "lockup.svg",
    "lockup-dark.svg",
    "README.md",
}

# D-04 palette.
TILE = "#115e59"
LINE = "#ffffff"
LINE_OPACITY = 0.9
HIGHLIGHT = "#a3ff3c"
PALETTE = {TILE, LINE, HIGHLIGHT}
# Plex's gold, its widely quoted orange, its black and its white (research table; never fetched).
PLEX_HEXES = {"#ebaf00", "#e5a00d", "#191919", "#fffbf8"}
DARK_PAGE = "#121212"
LIGHT_PAGE = "#ffffff"
HEX6 = re.compile(r"#[0-9a-f]{6}")

ALLOWED_ATTRIBUTES = {
    "svg": {"xmlns", "viewBox", "width", "height"},
    "title": set(),
    "rect": {"x", "y", "width", "height", "rx", "fill", "fill-opacity"},
    "path": {"d", "fill", "fill-opacity"},
}
BANNED_ANYWHERE = re.compile(r"\bon[a-z]+\s*=|\bstyle\b|\bhref\b|\bclass\b", re.IGNORECASE)


def png_size(data: bytes) -> tuple[int, int]:
    """Return a PNG's width and height from its IHDR chunk."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    assert data[12:16] == b"IHDR", "IHDR is not the first chunk"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def png_colour_type(data: bytes) -> int:
    """Return the IHDR colour type: 2 is RGB, 6 is RGBA."""
    png_size(data)
    return data[25]


def ico_sizes(data: bytes) -> list[int]:
    """Return the width of each ICO entry; a width byte of 0 means 256."""
    _reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert kind == 1, f"ICONDIR type is {kind}, not 1 (icon)"
    return [data[6 + 16 * i] or 256 for i in range(count)]


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def markup_problems(text: str) -> list[str]:
    """Return every way an SVG strays from the allow-list; empty means it is safe to serve."""
    problems = [f"banned token {m.group(0)!r}" for m in BANNED_ANYWHERE.finditer(text)]
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return [*problems, f"not well-formed: {exc}"]
    if root.tag != f"{SVG_NS}svg":
        problems.append(f"root is {root.tag}, not an SVG-namespace svg")
    for element in root.iter():
        if not element.tag.startswith(SVG_NS):
            problems.append(f"element outside the SVG namespace: {element.tag}")
        name = _local(element.tag)
        allowed = ALLOWED_ATTRIBUTES.get(name)
        if allowed is None:
            problems.append(f"element {name!r} is not allowed")
            continue
        for attribute in element.attrib:
            if attribute not in allowed:
                problems.append(f"attribute {attribute!r} is not allowed on {name!r}")
        if name == "title" and element.text != "plyrix":
            problems.append(f"title is {element.text!r}, not 'plyrix'")
        elif name != "title" and (element.text or "").strip():
            problems.append(f"element {name!r} holds text")
    titles = [e for e in root.iter() if _local(e.tag) == "title"]
    if len(titles) != 1:
        problems.append(f"expected one title, found {len(titles)}")
    return problems


def svg_files() -> list[Path]:
    return sorted(BRAND.glob("*.svg"))


@pytest.mark.parametrize(("name", "size"), sorted(PNG_SIZES.items()))
def test_png_has_its_exact_size(name: str, size: int) -> None:
    assert png_size((BRAND / name).read_bytes()) == (size, size)


def test_svgs_use_only_allowed_markup() -> None:
    files = svg_files()
    assert files, f"no SVG in {BRAND}"
    for path in files:
        assert markup_problems(path.read_text()) == [], path.name


GOOD = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">'
    '<title>plyrix</title><rect width="32" height="32" rx="7" fill="#115e59"/>{}</svg>'
)


def test_the_markup_control_passes() -> None:
    assert markup_problems(GOOD.format("")) == []


@pytest.mark.parametrize(
    "planted",
    [
        '<text x="1" y="9">la la</text>',
        "<script>alert(1)</script>",
        '<rect width="1" height="1" onload="alert(1)"/>',
        '<image href="https://example.com/x.png" width="1" height="1"/>',
        '<rect width="1" height="1" style="fill:red"/>',
        "<foreignObject width='1' height='1'><p>hi</p></foreignObject>",
    ],
)
def test_planted_markup_is_reported(planted: str) -> None:
    assert markup_problems(GOOD.format(planted)) != []


def test_touch_icon_is_opaque() -> None:
    assert png_colour_type((BRAND / "mark-180.png").read_bytes()) == 2


@pytest.mark.parametrize("name", ["mark-16.png", "mark-32.png", "mark-512.png"])
def test_other_pngs_keep_alpha(name: str) -> None:
    assert png_colour_type((BRAND / name).read_bytes()) == 6


def test_favicon_holds_16_and_32() -> None:
    sizes = ico_sizes((BRAND / "favicon.ico").read_bytes())
    assert len(sizes) == 2
    assert set(sizes) == {16, 32}


def test_brand_folder_holds_exactly_the_listed_files() -> None:
    assert {p.name for p in BRAND.iterdir()} == EXPECTED_FILES


# Colour --------------------------------------------------------------------------------------


def rgb(hex6: str) -> tuple[float, float, float]:
    """Return a #rrggbb colour as three channels in 0..1."""
    assert HEX6.fullmatch(hex6), f"{hex6!r} is not lowercase #rrggbb"
    r, g, b = (int(hex6[i : i + 2], 16) / 255 for i in (1, 3, 5))
    return r, g, b


def over(top: str, opacity: float, bottom: str) -> tuple[float, float, float]:
    """Composite top at the given opacity over an opaque bottom colour."""
    t, b = rgb(top), rgb(bottom)
    r, g, bl = (opacity * x + (1 - opacity) * y for x, y in zip(t, b, strict=True))
    return r, g, bl


def luminance(colour: tuple[float, float, float]) -> float:
    """WCAG 2.x relative luminance."""

    def linear(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(c) for c in colour)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def plex_like(hex6: str) -> list[str]:
    """Return every reason a colour is too close to Plex's look (D-05); empty means clear."""
    reasons = []
    hue, light, sat = colorsys.rgb_to_hls(*rgb(hex6))
    if 20 <= hue * 360 <= 65 and sat >= 0.35:
        reasons.append("amber or gold")
    if sat < 0.15 and light < 0.35:
        reasons.append("black or dark grey")
    if hex6 in PLEX_HEXES:
        reasons.append("a Plex brand colour")
    return reasons


def palette_problems(text: str) -> list[str]:
    """Return every fill that strays from the D-04 palette; empty means it is all palette."""
    problems = []
    root = ET.fromstring(text)
    for element in root.iter():
        fill = element.get("fill")
        opacity = element.get("fill-opacity")
        if fill is not None and fill not in PALETTE:
            problems.append(f"fill {fill!r} is not in the palette")
        if opacity is not None and (fill != LINE or float(opacity) != LINE_OPACITY):
            problems.append(f"fill-opacity {opacity!r} on {fill!r}")
        # The lyric lines are white at 0.9; the dark lockup's wordmark is solid white.
        if fill == LINE and opacity is None and _local(element.tag) != "path":
            problems.append("white without its 0.9 opacity")
    return problems


def svg_colours(text: str) -> set[str]:
    return {e.get("fill", "") for e in ET.fromstring(text).iter()} - {""}


def test_svg_colours_are_the_palette() -> None:
    for path in svg_files():
        assert palette_problems(path.read_text()) == [], path.name


@pytest.mark.parametrize(
    "fill",
    [
        'fill="red"',
        'fill="rgb(17,94,89)"',
        'fill="#EBAF00"',
        'fill="#ffffff" fill-opacity="0.5"',
        'fill="#ffffff"',
    ],
)
def test_off_palette_fills_are_reported(fill: str) -> None:
    assert palette_problems(GOOD.format(f'<rect width="1" height="1" {fill}/>')) != []


def test_no_plex_like_colour() -> None:
    for path in svg_files():
        for colour in svg_colours(path.read_text()):
            assert plex_like(colour) == [], f"{path.name}: {colour}"


@pytest.mark.parametrize("colour", sorted(PLEX_HEXES - {"#fffbf8"}))
def test_plex_colours_are_flagged(colour: str) -> None:
    assert plex_like(colour) != []


@pytest.mark.parametrize("colour", sorted(PALETTE))
def test_palette_is_clear_of_plex(colour: str) -> None:
    assert plex_like(colour) == []


def test_contrast() -> None:
    tile = rgb(TILE)
    white_on_tile = over(LINE, LINE_OPACITY, TILE)
    white_on_dark = over(LINE, LINE_OPACITY, DARK_PAGE)
    assert contrast(white_on_tile, tile) >= 3
    assert contrast(rgb(HIGHLIGHT), tile) >= 3
    assert contrast(tile, rgb(LIGHT_PAGE)) >= 3
    assert contrast(white_on_dark, rgb(DARK_PAGE)) >= 3
    assert contrast(rgb(HIGHLIGHT), rgb(DARK_PAGE)) >= 3
    # The tile on a dark page is 2.47:1. It is not gated: it is Patrick's locked palette (D-04).


# Form ----------------------------------------------------------------------------------------


def _int(value: str | None) -> int | None:
    if value is None or not re.fullmatch(r"\d+", value):
        return None
    return int(value)


def is_unimodal(widths: list[int]) -> bool:
    """True if the widths strictly rise then strictly fall, which reads as a pointer."""
    peak = widths.index(max(widths))
    if peak in (0, len(widths) - 1):
        return False
    rising = all(a < b for a, b in zip(widths[:peak], widths[1 : peak + 1], strict=True))
    falling = all(a > b for a, b in zip(widths[peak:-1], widths[peak + 1 :], strict=True))
    return rising and falling


def bar_problems(text: str) -> list[str]:
    """Return every way a mark strays from four left-aligned bars on a tile; empty if none."""
    root = ET.fromstring(text)
    problems = []
    names = [_local(e.tag) for e in root.iter()]
    if set(names) - {"svg", "title", "rect"}:
        problems.append(f"elements other than svg, title and rect: {sorted(set(names))}")
    grid = root.get("width")
    rects = [e for e in root.iter() if _local(e.tag) == "rect"]
    if not rects:
        return [*problems, "no rect"]
    tile, bars = rects[0], rects[1:]
    if (tile.get("width"), tile.get("height"), tile.get("fill")) != (grid, grid, TILE):
        problems.append("the first rect is not a full-size tile")
    if tile.get("x", "0") != "0" or tile.get("y", "0") != "0":
        problems.append("the tile does not start at the origin")
    if len(bars) != 4:
        return [*problems, f"{len(bars)} bars, not 4"]
    geometry = []
    for bar in bars:
        values = [_int(bar.get(k)) for k in ("x", "y", "width", "height")]
        x, y, w, h = values
        if x is None or y is None or w is None or h is None:
            problems.append(f"bar with a non-integer edge: {bar.attrib}")
            continue
        if w <= h:
            problems.append(f"bar at y {y} is not wider than tall")
        geometry.append((x, y, w, h, bar.get("fill")))
    if len(geometry) != 4:
        return problems
    if len({g[0] for g in geometry}) != 1:
        problems.append("the bars do not share a left edge")
    geometry.sort(key=lambda g: g[1])
    for (_, y1, _, h1, _), (_, y2, _, _, _) in zip(geometry, geometry[1:], strict=False):
        if y2 - (y1 + h1) < 1:
            problems.append(f"bars at y {y1} and {y2} are less than 1 unit apart")
    lime = [i for i, g in enumerate(geometry) if g[4] == HIGHLIGHT]
    if lime != [2]:
        problems.append(f"the lime bar is not exactly the third: {lime}")
    if len({g[3] for g in geometry}) != 1:
        problems.append("the bars are not all the same height")
    if (
        len(
            {
                y2 - (y1 + h1)
                for (_, y1, _, h1, _), (_, y2, _, _, _) in zip(geometry, geometry[1:], strict=False)
            }
        )
        != 1
    ):
        problems.append("the gaps between bars are not all equal")
    if is_unimodal([g[2] for g in geometry]):
        problems.append("the bar ends imply a pointer")
    return problems


def mark_files() -> list[Path]:
    return [p for p in (BRAND / "mark.svg", BRAND / "mark-16.svg") if p.exists()]


def test_mark_is_four_left_aligned_bars() -> None:
    files = mark_files()
    assert BRAND / "mark.svg" in files
    for path in files:
        assert bar_problems(path.read_text()) == [], path.name


def _bars(widths: tuple[int, int, int, int], extra: str = "") -> str:
    rows = []
    for i, (y, w) in enumerate(zip((7, 12, 17, 22), widths, strict=True)):
        h = 3
        fill = f'fill="{HIGHLIGHT}"' if i == 2 else f'fill="{LINE}" fill-opacity="0.9"'
        rows.append(f'<rect x="6" y="{y}" width="{w}" height="{h}" rx="1" {fill}/>')
    return GOOD.format("".join(rows) + extra)


def test_the_bar_control_passes() -> None:
    assert bar_problems(_bars((17, 13, 21, 15))) == []


def test_a_pointer_envelope_is_reported() -> None:
    assert bar_problems(_bars((10, 14, 18, 14))) != []


def test_unequal_heights_or_gaps_are_reported() -> None:
    thick = _bars((17, 13, 21, 15)).replace(
        'y="17" width="21" height="3"', 'y="17" width="21" height="4"'
    )
    assert "the bars are not all the same height" in bar_problems(thick)
    uneven = _bars((17, 13, 21, 15)).replace('y="17"', 'y="16"')
    assert "the gaps between bars are not all equal" in bar_problems(uneven)


def test_a_path_is_reported() -> None:
    assert bar_problems(_bars((17, 13, 21, 15), '<path d="M0 0L4 2L0 4z" fill="#a3ff3c"/>')) != []


# Small-size legibility (D-08) ----------------------------------------------------------------

Pixel = tuple[int, int, int, int]
INK = 1.25
SOLID = 3.0


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_png(data: bytes) -> list[list[Pixel]]:
    """Decode an 8-bit, non-interlaced RGB or RGBA PNG into rows of RGBA pixels."""
    width, height = png_size(data)
    depth, colour, _, _, interlace = data[24:29]
    assert depth == 8 and colour in (2, 6) and interlace == 0, "unsupported PNG"
    bpp = 4 if colour == 6 else 3
    idat = b""
    pos = 8
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        chunk_type = data[pos + 4 : pos + 8]
        if chunk_type == b"IDAT":
            idat += data[pos + 8 : pos + 8 + length]
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * bpp
    rows: list[list[Pixel]] = []
    previous = bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind = raw[start]
        line = bytearray(raw[start + 1 : start + 1 + stride])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = previous[i]
            c = previous[i - bpp] if i >= bpp else 0
            if kind == 1:
                line[i] = (line[i] + a) & 0xFF
            elif kind == 2:
                line[i] = (line[i] + b) & 0xFF
            elif kind == 3:
                line[i] = (line[i] + (a + b) // 2) & 0xFF
            elif kind == 4:
                line[i] = (line[i] + _paeth(a, b, c)) & 0xFF
            else:
                assert kind == 0, f"unknown filter {kind}"
        row: list[Pixel] = []
        for x in range(width):
            px = line[x * bpp : x * bpp + bpp]
            row.append((px[0], px[1], px[2], px[3] if bpp == 4 else 255))
        rows.append(row)
        previous = line
    return rows


def bar_runs(column: list[Pixel], tile: str = TILE) -> list[tuple[int, int, bool]]:
    """Return the ink runs down a column as (first row, last row, holds a solid row)."""
    base = rgb(tile)
    runs: list[tuple[int, int, bool]] = []
    for y, (r, g, b, a) in enumerate(column):
        alpha = a / 255
        seen = tuple(
            alpha * c / 255 + (1 - alpha) * t for c, t in zip((r, g, b), base, strict=True)
        )
        ratio = contrast((seen[0], seen[1], seen[2]), base)
        if ratio < INK:
            continue
        if runs and runs[-1][1] == y - 1:
            first, _, solid = runs[-1]
            runs[-1] = (first, y, solid or ratio >= SOLID)
        else:
            runs.append((y, y, ratio >= SOLID))
    return runs


def _encode_png(rows: list[list[Pixel]], filters: list[int]) -> bytes:
    """Build an RGBA PNG with the given filter type on each row (test helper)."""
    width, bpp = len(rows[0]), 4
    raw = b""
    previous = bytes(width * bpp)
    for row, kind in zip(rows, filters, strict=True):
        line = bytes(c for px in row for c in px)
        out = bytearray()
        for i, value in enumerate(line):
            a = line[i - bpp] if i >= bpp else 0
            b = previous[i]
            c = previous[i - bpp] if i >= bpp else 0
            predictor = (0, a, b, (a + b) // 2, _paeth(a, b, c))[kind]
            out.append((value - predictor) & 0xFF)
        raw += bytes([kind]) + bytes(out)
        previous = line

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body)
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, len(rows), 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def test_the_decoder_undoes_every_filter() -> None:
    rows = [
        [
            ((x * 70 + y * 31) % 256, (x * 13 + y * 90) % 256, (200 - x * y) % 256, 255 - y * 9)
            for x in range(3)
        ]
        for y in range(5)
    ]
    assert decode_png(_encode_png(rows, [0, 1, 2, 3, 4])) == rows


@pytest.mark.parametrize("size", [16, 32])
def test_small_icons_resolve_four_bars(size: int) -> None:
    rows = decode_png((BRAND / f"mark-{size}.png").read_bytes())
    column = [row[size // 4] for row in rows]
    runs = bar_runs(column)
    assert len(runs) == 4, runs
    assert all(solid for _, _, solid in runs), runs


# Column x = 4 of sketch L1 rendered at 16 px by rsvg-convert: rows 3 to 12 blend into one run.
_T: Pixel = (17, 94, 89, 255)
L1_AT_16_COLUMN: list[Pixel] = [
    _T,
    _T,
    _T,
    (74, 133, 129, 255),
    (232, 239, 239, 255),
    (60, 123, 119, 255),
    (232, 239, 239, 255),
    (117, 162, 159, 255),
    (125, 212, 68, 255),
    (163, 255, 60, 255),
    (60, 123, 119, 255),
    (232, 239, 239, 255),
    (74, 133, 129, 255),
    _T,
    _T,
    _T,
]


def test_a_merged_column_is_caught() -> None:
    assert len(bar_runs(L1_AT_16_COLUMN)) < 4


# Lockups (D-03) ------------------------------------------------------------------------------

LOCKUP_FILLS = {"lockup.svg": TILE, "lockup-dark.svg": LINE}
LOCKUP_PAGES = {"lockup.svg": LIGHT_PAGE, "lockup-dark.svg": DARK_PAGE}
# The wordmark keeps at least this many units of clear space right of the tile.
WORDMARK_CLEARANCE = 4
PATH_CHARACTERS = re.compile(r"[MmZzLlHhVvCcSsQqTtAa0-9., -]+")
PATH_TOKEN = re.compile(r"[A-Za-z]|-?(?:\d+\.?\d*|\.\d+)")


def rects(text: str) -> list[dict[str, str]]:
    return [dict(e.attrib) for e in ET.fromstring(text).iter() if _local(e.tag) == "rect"]


def wordmark_problems(text: str, fill: str) -> list[str]:
    """Return every way a lockup's wordmark strays from one outlined path; empty means none."""
    root = ET.fromstring(text)
    problems: list[str] = []
    box = (root.get("viewBox") or "").split()
    if len(box) != 4 or box[:2] != ["0", "0"] or box[2:] != [root.get("width"), "32"]:
        return [f"viewBox {box} does not match a 32-high lockup of its own width"]
    width, height = float(box[2]), float(box[3])
    tile = rects(text)[0] if rects(text) else {}
    tile_right = float(tile.get("width", "0"))
    paths = [e for e in root.iter() if _local(e.tag) == "path"]
    if len(paths) != 1:
        return [*problems, f"{len(paths)} paths, not 1"]
    path = paths[0]
    if path.get("fill") != fill:
        problems.append(f"wordmark fill {path.get('fill')!r}, not {fill!r}")
    if path.get("fill-opacity") is not None:
        problems.append("the wordmark has a fill-opacity")
    d = path.get("d", "")
    if not PATH_CHARACTERS.fullmatch(d):
        return [*problems, "d holds characters other than path commands and numbers"]
    tokens = PATH_TOKEN.findall(d)
    if "".join(tokens) != re.sub(r"[\s,]", "", d):
        return [*problems, "d does not split cleanly into commands and numbers"]
    letters = {t for t in tokens if t.isalpha()}
    if letters - {"M", "L", "C", "Z"}:
        return [*problems, f"commands other than absolute M, L, C and Z: {sorted(letters)}"]
    numbers = [float(t) for t in tokens if not t.isalpha()]
    if len(numbers) % 2:
        return [*problems, "an odd count of coordinates"]
    xs, ys = numbers[0::2], numbers[1::2]
    if min(xs) < tile_right + WORDMARK_CLEARANCE:
        problems.append(f"the wordmark reaches x {min(xs)}, too close to the tile")
    if max(xs) > width:
        problems.append(f"the wordmark reaches x {max(xs)}, outside the viewBox")
    if min(ys) < 0 or max(ys) > height:
        problems.append(f"the wordmark spans y {min(ys)} to {max(ys)}, outside the viewBox")
    return problems


def lockup_files() -> list[Path]:
    return [BRAND / name for name in sorted(LOCKUP_FILLS)]


def test_lockups_reuse_the_mark() -> None:
    mark = rects((BRAND / "mark.svg").read_text())
    for path in lockup_files():
        assert rects(path.read_text()) == mark, path.name


def test_lockup_wordmark() -> None:
    for path in lockup_files():
        assert wordmark_problems(path.read_text(), LOCKUP_FILLS[path.name]) == [], path.name


def test_lockup_wordmark_contrast() -> None:
    for path in lockup_files():
        paths = [e for e in ET.fromstring(path.read_text()).iter() if _local(e.tag) == "path"]
        fill = paths[0].get("fill", "")
        ratio = contrast(rgb(fill), rgb(LOCKUP_PAGES[path.name]))
        assert ratio >= 4.5, f"{path.name}: {ratio:.2f}"


LOCKUP = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 32" width="80" height="32">'
    '<title>plyrix</title><rect width="32" height="32" rx="7" fill="#115e59"/>{}</svg>'
)
GOOD_WORDMARK = '<path d="M 40 7 L 79.5 7 C 60 20, 50 30, 40.25 31.9 Z" fill="#115e59"/>'


def test_the_wordmark_control_passes() -> None:
    assert wordmark_problems(LOCKUP.format(GOOD_WORDMARK), TILE) == []


@pytest.mark.parametrize(
    "planted",
    [
        GOOD_WORDMARK.replace("M 40 7", "M 34 7"),
        GOOD_WORDMARK.replace("79.5", "81"),
        GOOD_WORDMARK.replace("31.9", "32.5"),
        GOOD_WORDMARK.replace("L 79.5 7", "l 9.5 0"),
        GOOD_WORDMARK.replace("Z", "Z;"),
        GOOD_WORDMARK.replace('fill="#115e59"', 'fill="#115e59" fill-opacity="0.9"'),
        GOOD_WORDMARK.replace('fill="#115e59"', 'fill="#ffffff"'),
        GOOD_WORDMARK + GOOD_WORDMARK,
    ],
)
def test_planted_wordmarks_are_reported(planted: str) -> None:
    assert wordmark_problems(LOCKUP.format(planted), TILE) != []


def test_a_solid_white_wordmark_is_palette() -> None:
    assert palette_problems(LOCKUP.format(GOOD_WORDMARK.replace("#115e59", "#ffffff"))) == []


# The brand note for Phase 5 (D-06) -----------------------------------------------------------

NON_AFFILIATION = "plyrix is an independent project, not affiliated with or endorsed by Plex, Inc."


def readme() -> str:
    return (BRAND / "README.md").read_text()


def test_readme_states_non_affiliation() -> None:
    assert NON_AFFILIATION in readme()


def test_readme_credits_the_font() -> None:
    text = readme()
    assert "Fira Sans SemiBold" in text
    assert "4.301" in text
    assert "SIL Open Font License 1.1" in text


def test_readme_lists_the_palette() -> None:
    text = readme()
    for colour in (TILE, LINE, HIGHLIGHT):
        assert colour in text, colour


def test_readme_names_every_file() -> None:
    text = readme()
    for name in sorted(EXPECTED_FILES - {"README.md"}):
        assert f"`{name}`" in text, name
