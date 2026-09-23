"""Record a CLI demo with VHS and encode it with a window frame.

From the repository root, with the project environment active:

    python docs/demo/render.py           # README demo: docs/cli-demo.gif and docs/cli-demo.mp4
    python docs/demo/render.py --slide   # presentation demo: build/cli-demo-slide.mp4

VHS (https://github.com/charmbracelet/vhs) records the terminal frames described in
a tape; this script adds the window and encodes the video with ffmpeg, so the result
does not depend on VHS's own encoder.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build"
FRAMES = BUILD / "vhs-frames"
FPS = 30
BACKGROUND = (22, 23, 42)  # terminal theme background (#16172a)
BAR = (30, 31, 52)


@dataclass(frozen=True)
class Frame:
    """Window proportions; the slide profile is scaled up to stay sharp when projected."""

    pad: int
    bar: int
    radius: int
    dot: int
    title: int


README_FRAME = Frame(pad=28, bar=40, radius=14, dot=6, title=13)
SLIDE_FRAME = Frame(pad=32, bar=60, radius=18, dot=9, title=22)


def run(*command: str) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def window(width: int, height: int, frame: Frame, margin: int = 0, radius: int | None = None) -> Image.Image:
    """Window with title bar on a transparent canvas; a margin adds a soft shadow."""
    radius = frame.radius if radius is None else radius
    window_w, window_h = width + 2 * frame.pad, height + frame.bar + 2 * frame.pad
    canvas = Image.new("RGBA", (window_w + 2 * margin, window_h + 2 * margin), (0, 0, 0, 0))
    if margin:
        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (margin, margin + 8, margin + window_w, margin + window_h + 8), radius, fill=(10, 10, 20, 90)
        )
        canvas = Image.alpha_composite(canvas, shadow.filter(ImageFilter.GaussianBlur(14)))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((margin, margin, margin + window_w, margin + window_h), radius, fill=BACKGROUND)
    draw.rounded_rectangle((margin, margin, margin + window_w, margin + frame.bar), radius, fill=BAR)
    draw.rectangle((margin, margin + frame.bar - frame.radius, margin + window_w, margin + frame.bar), fill=BAR)
    for index, colour in enumerate(((255, 95, 87), (254, 188, 46), (40, 200, 64))):
        cx, cy = margin + frame.pad * 0.8 + index * frame.dot * 3.3, margin + frame.bar / 2
        draw.ellipse((cx - frame.dot, cy - frame.dot, cx + frame.dot, cy + frame.dot), fill=colour)
    try:
        title_font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", frame.title)
    except OSError:
        title_font = ImageFont.load_default()
    draw.text((margin + window_w / 2, margin + frame.bar / 2), "manual-ingestion", font=title_font, fill=(111, 111, 136), anchor="mm")
    return canvas


def flatten(image: Image.Image, page: tuple[int, int, int], path: Path) -> None:
    background = Image.new("RGBA", image.size, page + (255,))
    Image.alpha_composite(background, image).convert("RGB").save(path)


def record(tape: Path) -> tuple[int, int]:
    shutil.rmtree(FRAMES, ignore_errors=True)
    run("vhs", str(tape.relative_to(ROOT)))
    first = FRAMES / "frame-text-00001.png"
    if not first.exists():
        raise SystemExit(f"VHS produced no frames in {FRAMES}")
    return Image.open(first).size


def inputs(background: Path) -> list[str]:
    return [
        "-loop", "1", "-framerate", str(FPS), "-i", str(background),
        "-framerate", str(FPS), "-i", str(FRAMES / "frame-text-%05d.png"),
        "-framerate", str(FPS), "-i", str(FRAMES / "frame-cursor-%05d.png"),
    ]


def compose(x: int, y: int) -> str:
    return f"[1][2]overlay[term];[0][term]overlay={x}:{y}:shortest=1"


def mp4(background: Path, offset: tuple[int, int], out: Path) -> None:
    run("ffmpeg", "-v", "error", "-y", *inputs(background), "-filter_complex",
        f"{compose(*offset)},pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p",
        "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-movflags", "+faststart", str(out))


def readme() -> list[Path]:
    width, height = record(ROOT / "docs/demo/cli.tape")
    frame, margin = README_FRAME, 36
    shadowed, square = BUILD / "vhs-mp4.png", BUILD / "vhs-gif.png"
    flatten(window(width, height, frame, margin=margin), (244, 244, 248), shadowed)
    # Any transparency stops ffmpeg from encoding only the changed regions of each
    # GIF frame, which makes the file around fifty times larger: opaque and square.
    window(width, height, frame, radius=0).convert("RGB").save(square)
    mp4(shadowed, (margin + frame.pad, margin + frame.bar + frame.pad), ROOT / "docs/cli-demo.mp4")
    run("ffmpeg", "-v", "error", "-y", *inputs(square), "-filter_complex",
        f"{compose(frame.pad, frame.bar + frame.pad)},fps=12,scale=960:-1:flags=lanczos,split[a][b];"
        "[a]palettegen=max_colors=64:stats_mode=full[p];"
        "[b][p]paletteuse=dither=none:diff_mode=rectangle",
        str(ROOT / "docs/cli-demo.gif"))
    return [ROOT / "docs/cli-demo.mp4", ROOT / "docs/cli-demo.gif"]


def slide() -> list[Path]:
    """Large type for an audience; the window sits on white, like a slide."""
    width, height = record(ROOT / "docs/demo/slide.tape")
    frame, background = SLIDE_FRAME, BUILD / "vhs-slide.png"
    flatten(window(width, height, frame), (255, 255, 255), background)
    out = BUILD / "cli-demo-slide.mp4"
    mp4(background, (frame.pad, frame.bar + frame.pad), out)
    return [out]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--slide", action="store_true", help="render the large-type presentation demo")
    args = parser.parse_args()
    for tool in ("vhs", "ffmpeg"):
        if shutil.which(tool) is None:
            print(f"{tool} is required: brew install {tool}", file=sys.stderr)
            return 1
    BUILD.mkdir(exist_ok=True)
    for path in slide() if args.slide else readme():
        print(f"{path.relative_to(ROOT)}  {path.stat().st_size / 1_000_000:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
