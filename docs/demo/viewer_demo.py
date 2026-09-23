"""Record the viewer demo: Overview → Inspect (click a region, read the result) → Checks.

From the repository root, after `npm --prefix viewer run build`, with the project
environment active and Playwright installed (`pip install playwright`; it drives
the local Google Chrome):

    python docs/demo/viewer_demo.py            # English: docs/viewer-demo.gif and docs/viewer-demo.mp4
    python docs/demo/viewer_demo.py --lang it  # Italian: build/viewer-demo-it.gif and build/viewer-demo-it.mp4

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
        self.session.send("Page.startScreencast", {"format": "png", "everyNthFrame": 1})

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


def encode(frames: list[tuple[float, bytes]], name: str, gif_width: int) -> tuple[Path, Path]:
    """Hold each frame until the next one, then encode MP4 and a lean GIF."""
    work = BUILD / f"{name}-frames"
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
    mp4, gif = BUILD / f"{name}.mp4", BUILD / f"{name}.gif"
    concat = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(work / "frames.txt")]
    subprocess.run([*concat, "-vf", "fps=30,pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-movflags", "+faststart", str(mp4)], check=True)
    subprocess.run([*concat, "-filter_complex",
                    f"fps=12,scale={gif_width}:-1:flags=lanczos,split[a][b];"
                    "[a]palettegen=max_colors=128:stats_mode=full[p];"
                    "[b][p]paletteuse=dither=none:diff_mode=rectangle", "-loop", "0", str(gif)], check=True)
    return mp4, gif


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lang", choices=("en", "it"), default="en")
    args = parser.parse_args()
    if not (ROOT / "viewer/dist/index.html").exists():
        raise SystemExit("Build the viewer first: npm --prefix viewer run build")
    BUILD.mkdir(exist_ok=True)
    with preview_server() as base, sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome")
        context = browser.new_context(viewport=VIEWPORT, device_scale_factor=SCALE, color_scheme="light")
        context.add_init_script(CURSOR)
        page = context.new_page()
        page.goto(f"{base}?lang={args.lang}#overview")
        page.wait_for_selector(".stages img")
        page.wait_for_timeout(600)
        cast = Screencast(page)
        cast.start()
        perform(page, base)
        cast.stop()
        browser.close()
    name = "viewer-demo" if args.lang == "en" else "viewer-demo-it"
    mp4, gif = encode(cast.frames, name, gif_width=960 if args.lang == "en" else 1600)
    if args.lang == "en":
        mp4 = shutil.copy(mp4, ROOT / "docs/viewer-demo.mp4")
        gif = shutil.copy(gif, ROOT / "docs/viewer-demo.gif")
    for path in (Path(mp4), Path(gif)):
        print(f"{path.relative_to(ROOT)}  {path.stat().st_size / 1_000_000:.1f} MB  ({len(cast.frames)} frames)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
