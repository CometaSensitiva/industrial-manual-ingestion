"""Render the viewer's icons and share image from the CLI portrait.

Run from the repository root with the project environment active:

    python viewer/scripts/brand_assets.py

Every asset is drawn from ``manual_ingestion.cli_display.LOGO`` and its tone
mask, so the terminal, the viewer mark and the icons stay one identity.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from manual_ingestion.cli_display import LOGO, LOGO_TONES

OUT = Path(__file__).resolve().parents[1] / "public"
NIGHT = (22, 23, 42)
TONES = {"h": (212, 198, 251), "j": (149, 169, 255), ".": (138, 138, 163)}
DENSITY = {".": 0.3, ":": 0.4, "-": 0.45, "_": 0.45, "=": 0.6, "+": 0.65, "/": 0.7, "\\": 0.7, "|": 0.7, "*": 0.8, "#": 1.0}
ROWS = LOGO.split("\n")
TONE_ROWS = LOGO_TONES.split("\n")
COLS = max(map(len, ROWS))


def cells():
    """One cell per glyph; opacity is quantized so SVG runs can be merged."""
    for y, row in enumerate(ROWS):
        for x, glyph in enumerate(row):
            if glyph != " ":
                opacity = round(DENSITY.get(glyph, 0.6) * 4) / 4
                yield x, y, TONE_ROWS[y][x] if x < len(TONE_ROWS[y]) else ".", opacity


def svg() -> str:
    """Merge horizontal runs of equal tone and opacity to keep the file small."""
    grid = {(x, y): (tone, opacity) for x, y, tone, opacity in cells()}
    rects: list[str] = []
    for y, row in enumerate(ROWS):
        x = 0
        while x < len(row):
            key = grid.get((x, y))
            if key is None:
                x += 1
                continue
            start = x
            while grid.get((x, y)) == key:
                x += 1
            tone, opacity = key
            fill = "#%02x%02x%02x" % TONES.get(tone, TONES["."])
            rects.append(f'<rect x="{start}" y="{y * 2}" width="{x - start}" height="2" fill="{fill}" opacity="{opacity:g}"/>')
    night = "#%02x%02x%02x" % NIGHT
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="-4 -3 52 50">'
        f'<rect x="-4" y="-3" width="52" height="50" rx="10" fill="{night}"/>{"".join(rects)}</svg>\n'
    )


def draw_portrait(image: Image.Image, left: float, top: float, cell_w: float) -> None:
    draw = ImageDraw.Draw(image)
    cell_h = cell_w * 2
    for x, y, tone, opacity in cells():
        colour = tuple(round(NIGHT[i] + (TONES.get(tone, TONES["."])[i] - NIGHT[i]) * opacity) for i in range(3))
        x0, y0 = left + x * cell_w, top + y * cell_h
        draw.rectangle((x0, y0, x0 + cell_w - 0.01, y0 + cell_h - 0.01), fill=colour)


def icon(size: int, radius_ratio: float = 0.22) -> Image.Image:
    scale = 4
    big = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    ImageDraw.Draw(big).rounded_rectangle((0, 0, size * scale - 1, size * scale - 1), round(size * scale * radius_ratio), fill=NIGHT)
    cell = size * scale * 0.84 / COLS
    height = len(ROWS) * cell * 2
    draw_portrait(big, (size * scale - COLS * cell) / 2, (size * scale - height) / 2, cell)
    return big.resize((size, size), Image.LANCZOS)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for path, index in (("/System/Library/Fonts/Menlo.ttc", 1 if bold else 0), ("DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf", 0)):
        try:
            return ImageFont.truetype(path, size, index=index)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_ascii(image: Image.Image, left: float, top: float, size: int) -> None:
    """Large enough to read: the portrait as real coloured glyphs, like the CLI."""
    draw = ImageDraw.Draw(image)
    glyphs = font(size, bold=True)
    advance = glyphs.getlength("M")
    line = size * 1.2
    for y, row in enumerate(ROWS):
        for x, glyph in enumerate(row):
            if glyph != " ":
                tone = TONE_ROWS[y][x] if x < len(TONE_ROWS[y]) else "."
                draw.text((left + x * advance, top + y * line), glyph, font=glyphs, fill=TONES.get(tone, TONES["."]))


def share_image() -> Image.Image:
    width, height = 1200, 630
    image = Image.new("RGB", (width, height), NIGHT)
    size = 17
    draw_ascii(image, 70, (height - len(ROWS) * size * 1.2) / 2, size)
    draw = ImageDraw.Draw(image)
    x = 560
    draw.text((x, 200), "manual-ingestion", font=font(52, bold=True), fill=TONES["h"])
    draw.text((x, 282), "PDFs into structured,", font=font(30), fill=(226, 226, 236))
    draw.text((x, 324), "traceable content.", font=font(30), fill=(226, 226, 236))
    draw.text((x, 398), "PDF → Structure → Bundle", font=font(24), fill=TONES["j"])
    draw.text((x, 452), "local · open source · CLI + viewer", font=font(20), fill=(111, 111, 136))
    return image


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    (OUT / "favicon.svg").write_text(svg(), encoding="utf-8")
    icon(32, 0.2).save(OUT / "favicon-32.png", optimize=True)
    icon(180, 0).save(OUT / "apple-touch-icon.png", optimize=True)
    share_image().save(OUT / "og.png", optimize=True)
    for name in ("favicon.svg", "favicon-32.png", "apple-touch-icon.png", "og.png"):
        print(f"{name:22}{(OUT / name).stat().st_size:>8} bytes")
