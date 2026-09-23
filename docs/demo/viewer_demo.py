"""Record the viewer demo: Overview → Inspect (click a region, read the result) → Checks.

From the repository root, after `npm --prefix viewer run build`, with the project
environment active and Playwright installed (`pip install playwright`; it drives
the local Google Chrome):

    python docs/demo/viewer_demo.py            # English: docs/viewer-demo.gif and docs/viewer-demo.mp4
    python docs/demo/viewer_demo.py --lang it  # Italian: build/viewer-demo-it.gif and build/viewer-demo-it.mp4
    python docs/demo/viewer_demo.py --reuse    # re-encode the last recording, e.g. after changing the window

Frames come from Chrome's screencast (sharp, one per visual change) and are timed
and encoded with ffmpeg. A drawn cursor shows where the "user" points and clicks.
"""
from __future__ import annotations

import argparse
import base64
import math
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build"
VIEWPORT = {"width": 1280, "height": 800}
SCALE = 1.5  # 1920×1200 frames: sharp on a projector, scaled down for the README GIF

CURSOR = """
(() => {
  const cursor = document.createElement('div');
  cursor.id = 'demo-cursor';
  cursor.innerHTML = '<svg width="26" height="26" viewBox="0 0 26 26"><path d="M4 2 L4 21 L9 16.5 L12.5 24 L15.5 22.6 L12 15.3 L18.5 15.3 Z" fill="#16172a" stroke="#ffffff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
  Object.assign(cursor.style, {position: 'fixed', left: '0px', top: '0px', zIndex: 2147483647, pointerEvents: 'none', transform: 'translate(-3px,-2px)', transition: 'transform .12s ease'});
  const ring = document.createElement('div');
  Object.assign(ring.style, {position: 'fixed', width: '34px', height: '34px', marginLeft: '-17px', marginTop: '-17px', borderRadius: '50%', border: '2px solid #b4a0e5', opacity: 0, zIndex: 2147483646, pointerEvents: 'none', transition: 'opacity .35s ease, transform .35s ease'});
  const attach = () => { document.body.append(ring, cursor); };
  document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', attach) : attach();
  addEventListener('mousemove', e => { cursor.style.left = e.clientX + 'px'; cursor.style.top = e.clientY + 'px'; }, true);
  addEventListener('mousedown', e => {
    ring.style.left = e.clientX + 'px'; ring.style.top = e.clientY + 'px';
    ring.style.transition = 'none'; ring.style.opacity = 1; ring.style.transform = 'scale(.5)';
    requestAnimationFrame(() => { ring.style.transition = 'opacity .45s ease, transform .45s ease'; ring.style.opacity = 0; ring.style.transform = 'scale(1.4)'; });
    cursor.style.transform = 'translate(-3px,-2px) scale(.88)';
  }, true);
  addEventListener('mouseup', () => { cursor.style.transform = 'translate(-3px,-2px)'; }, true);
})();
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def preview_server():
    """Serve the built viewer, like GitHub Pages does."""
    port = free_port()
    process = subprocess.Popen(
        ["npx", "vite", "preview", "--port", str(port), "--strictPort", "--host", "127.0.0.1"],
        cwd=ROOT / "viewer", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        yield f"http://127.0.0.1:{port}/"
    finally:
        process.terminate()


class Screencast:
    """Collect Chrome screencast frames with their timestamps."""

    def __init__(self, page: Page) -> None:
        self.frames: list[tuple[float, bytes]] = []
        self.session = page.context.new_cdp_session(page)
        self.session.on("Page.screencastFrame", self._frame)

    def _frame(self, event: dict) -> None:
        self.frames.append((event["metadata"]["timestamp"], base64.b64decode(event["data"])))
        self.session.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})

    def start(self) -> None:
        # Without a maximum size Chrome sends CSS-pixel frames; ask for device pixels.
        self.session.send("Page.startScreencast", {
            "format": "png", "everyNthFrame": 1,
            "maxWidth": round(VIEWPORT["width"] * SCALE), "maxHeight": round(VIEWPORT["height"] * SCALE),
        })

    def stop(self) -> None:
        self.session.send("Page.stopScreencast")


class Director:
    """Human-paced mouse: eased moves, short pauses, visible clicks."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.x, self.y = 640.0, 420.0
        page.mouse.move(self.x, self.y)

    def pause(self, seconds: float) -> None:
        self.page.wait_for_timeout(int(seconds * 1000))

    def move_to(self, selector: str, dx: float = 0.5, dy: float = 0.5) -> None:
        box = self.page.locator(selector).first.bounding_box()
        assert box, f"not visible: {selector}"
        tx, ty = box["x"] + box["width"] * dx, box["y"] + box["height"] * dy
        steps = max(12, int(math.dist((self.x, self.y), (tx, ty)) / 18))
        for step in range(1, steps + 1):
            t = step / steps
            ease = t * t * (3 - 2 * t)
            self.page.mouse.move(self.x + (tx - self.x) * ease, self.y + (ty - self.y) * ease)
            self.page.wait_for_timeout(12)
        self.x, self.y = tx, ty

    def click(self, selector: str, dx: float = 0.5, dy: float = 0.5, after: float = 1.2) -> None:
        self.move_to(selector, dx, dy)
        self.pause(0.25)
        self.page.mouse.down()
        self.page.wait_for_timeout(90)
        self.page.mouse.up()
        self.pause(after)

    def scroll(self, pixels: int, steps: int = 24) -> None:
        for _ in range(steps):
            self.page.mouse.wheel(0, pixels / steps)
            self.page.wait_for_timeout(25)


def perform(page: Page, url: str) -> None:
    """The story: see the run, open a record at its source, check the bundle."""
    director = Director(page)
    director.pause(1.6)
    director.scroll(560)  # to the run log; the command types itself
    director.pause(5.5)
    director.scroll(-560)
    director.pause(0.6)
    director.click(".lede .primary", after=1.6)  # Inspect the document
    director.click('.bbox[data-type="title"]', after=1.4)
    director.click('.bbox[data-type="text"]', dy=0.3, after=1.8)
    director.click('.bbox[data-type="image"]', dx=0.3, dy=0.4, after=2.2)
    director.move_to(".inspector", dy=0.55)
    director.scroll(260, steps=16)
    director.pause(1.8)
    for _ in range(2):  # j walks the records, like a pager
        page.keyboard.press("j")
        director.pause(1.1)
    director.click(".tabs button:nth-child(3)", after=1.6)  # Checks
    director.click(".area summary", dx=0.2, after=2.4)
    director.move_to(".check-status .cells", dx=0.8)
    director.pause(1.6)


def save_frames(frames: list[tuple[float, bytes]], work: Path) -> None:
    """Write the frames and an ffmpeg concat list holding each until the next one."""
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    lines = []
    for index, (stamp, data) in enumerate(frames):
        path = work / f"{index:05d}.png"
        path.write_bytes(data)
        duration = (frames[index + 1][0] - stamp) if index + 1 < len(frames) else 2.0
        lines += [f"file '{path.name}'", f"duration {max(duration, 1 / 60):.4f}"]
    lines.append(f"file '{len(frames) - 1:05d}.png'")  # concat needs the last file repeated
    (work / "frames.txt").write_text("\n".join(lines) + "\n")


def browser_window(width: int, height: int, page: tuple[int, int, int], title: str) -> tuple[Image.Image, tuple[int, int]]:
    """A light macOS-style window with a soft shadow, drawn at the recording scale.

    Returns the background and where the page frames go.
    """
    scale = width / VIEWPORT["width"]  # draw the window at the frames' real scale
    bar, radius, margin = round(40 * scale), round(12 * scale), round(40 * scale)
    window_w, window_h = width, height + bar
    canvas = Image.new("RGBA", (window_w + 2 * margin, window_h + 2 * margin), page + (255,))
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (margin, margin + round(10 * scale), margin + window_w, margin + window_h + round(10 * scale)),
        radius, fill=(20, 20, 40, 60),
    )
    canvas = Image.alpha_composite(canvas, shadow.filter(ImageFilter.GaussianBlur(round(16 * scale))))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((margin, margin, margin + window_w, margin + window_h), radius, fill=(255, 255, 255), outline=(222, 222, 230))
    draw.rounded_rectangle((margin, margin, margin + window_w, margin + bar), radius, fill=(246, 246, 248))
    draw.rectangle((margin + 1, margin + bar - radius, margin + window_w - 1, margin + bar), fill=(246, 246, 248))
    draw.line((margin, margin + bar, margin + window_w, margin + bar), fill=(225, 225, 232), width=max(1, round(scale)))
    dot, gap = round(6 * scale), round(20 * scale)
    for index, colour in enumerate(((255, 95, 87), (254, 188, 46), (40, 200, 64))):
        cx, cy = margin + round(20 * scale) + index * gap, margin + bar // 2
        draw.ellipse((cx - dot, cy - dot, cx + dot, cy + dot), fill=colour)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/HelveticaNeue.ttc", round(13 * scale))
    except OSError:
        font = ImageFont.load_default()
    draw.text((margin + window_w / 2, margin + bar / 2), title, font=font, fill=(90, 90, 104), anchor="mm")
    return canvas.convert("RGB"), (margin, margin + bar)


def encode(work: Path, name: str, gif_width: int, page: tuple[int, int, int]) -> tuple[Path, Path]:
    """Put the page frames in the window, then encode MP4 and a lean GIF."""
    width, height = Image.open(work / "00000.png").size
    background, (x, y) = browser_window(width, height, page, "manual-ingestion · viewer")
    background.save(work / "window.png")
    mp4, gif = BUILD / f"{name}.mp4", BUILD / f"{name}.gif"
    sources = ["ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", "30", "-i", str(work / "window.png"),
               "-f", "concat", "-safe", "0", "-i", str(work / "frames.txt")]
    framed = f"[1]fps=30[page];[0][page]overlay={x}:{y}:shortest=1"
    subprocess.run([*sources, "-filter_complex", f"{framed},pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-movflags", "+faststart", str(mp4)], check=True)
    # Opaque background: the static window and shadow cost nothing once the
    # GIF only encodes the regions that change.
    subprocess.run([*sources, "-filter_complex",
                    f"{framed},fps=12,scale={gif_width}:-1:flags=lanczos,split[a][b];"
                    "[a]palettegen=max_colors=128:stats_mode=full[p];"
                    "[b][p]paletteuse=dither=none:diff_mode=rectangle", "-loop", "0", str(gif)], check=True)
    return mp4, gif


def record(lang: str, work: Path) -> None:
    if not (ROOT / "viewer/dist/index.html").exists():
        raise SystemExit("Build the viewer first: npm --prefix viewer run build")
    with preview_server() as base, sync_playwright() as playwright:
        # The flag makes headless screencast frames use device pixels (1920×1200).
        browser = playwright.chromium.launch(channel="chrome", args=[f"--force-device-scale-factor={SCALE}"])
        context = browser.new_context(viewport=VIEWPORT, device_scale_factor=SCALE, color_scheme="light")
        context.add_init_script(CURSOR)
        page = context.new_page()
        page.goto(f"{base}?lang={lang}#overview")
        page.wait_for_selector(".stages img")
        page.wait_for_timeout(600)
        cast = Screencast(page)
        cast.start()
        perform(page, base)
        cast.stop()
        browser.close()
    save_frames(cast.frames, work)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lang", choices=("en", "it"), default="en")
    parser.add_argument("--reuse", action="store_true", help="re-encode the last recording instead of recording again")
    args = parser.parse_args()
    BUILD.mkdir(exist_ok=True)
    name = "viewer-demo" if args.lang == "en" else "viewer-demo-it"
    work = BUILD / f"{name}-frames"
    if args.reuse:
        if not (work / "frames.txt").exists():
            raise SystemExit(f"No recording to reuse in {work}; run without --reuse first")
    else:
        record(args.lang, work)
    # English goes to the README on a light grey page; Italian is for white slides.
    page = (244, 244, 248) if args.lang == "en" else (255, 255, 255)
    mp4, gif = encode(work, name, gif_width=960 if args.lang == "en" else 1600, page=page)
    if args.lang == "en":
        mp4 = shutil.copy(mp4, ROOT / "docs/viewer-demo.mp4")
        gif = shutil.copy(gif, ROOT / "docs/viewer-demo.gif")
    for path in (Path(mp4), Path(gif)):
        print(f"{path.relative_to(ROOT)}  {path.stat().st_size / 1_000_000:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
