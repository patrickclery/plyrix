# plyrix brand files

The plyrix mark is four lyric lines on a teal tile, with the third line highlighted as the line
now playing. These files are for the README, the project site and other places that link to
plyrix.

## Files

- `mark.svg`: the master mark on a 32-unit grid. It also works as an SVG favicon.
- `mark-16.svg`: the 16 px source. At 16 px the master blurs two lines into one, so the small
  icon has its own whole-pixel geometry.
- `lockup.svg`: the mark with the wordmark, for light backgrounds.
- `lockup-dark.svg`: the mark with a white wordmark, for dark backgrounds.
- `mark-16.png` and `mark-32.png`: favicons.
- `mark-180.png`: the apple-touch icon. It is an opaque square, because iOS rounds the corners
  itself and would show transparent corners as black.
- `mark-512.png`: the large icon.
- `favicon.ico`: holds the 16 and 32 px icons.

## Palette

| Use | Colour |
|---|---|
| Tile | `#115e59` |
| Lyric lines | `#ffffff` at 90% opacity |
| Highlighted line | `#a3ff3c` |

The highlight marks the current synced line. No artwork shows lyric text.

## Wordmark

The wordmark is lowercase "plyrix" in Fira Sans SemiBold, version 4.301, converted to outlines,
so no font file ships. The font's name table states: "Digitized data copyright 2012-2018, The
Mozilla Foundation and Telefonica S.A., bBox Type GmbH and Carrois Corporate GbR, with Reserved
Font Name "Fira"" and "Licensed under the Open Font License, version 1.1 or later". Fira Sans is
licensed under the SIL Open Font License 1.1 (https://openfontlicense.org).

## Non-affiliation

Use this sentence wherever the logo appears beside Plex:

plyrix is an independent project, not affiliated with or endorsed by Plex, Inc.

Plex is a trademark of Plex, Inc.

## Regenerating

```sh
scripts/make_brand.sh [--check] [output-dir]
```

The script writes the SVGs from constants, then renders the PNGs and `favicon.ico`. `--check`
rebuilds into a temporary folder and fails if any generated file differs. It needs rsvg-convert
(librsvg), ImageMagick 7, uv and Fira Sans SemiBold 4.301 installed through fontconfig, and it
stops if fontconfig would substitute another font. Tests never call it, and it adds no runtime
dependency.
