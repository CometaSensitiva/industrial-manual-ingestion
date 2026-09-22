"""Terminal-only reveal effects for the home screen; never used for pipes or JSON."""
from __future__ import annotations

import os
import random
import re
import select
import sys
import time
from contextlib import contextmanager

ANIMATION_OFF = "MANUAL_INGESTION_NO_ANIMATION"
# Zero-width marker around text that should be typed at human speed. Rich
# measures it as zero cells and the typewriter never writes it.
SLOW = "⁠"
_TOKENS = re.compile(r"(\x1b\[[0-9;?]*[A-Za-z])|(\n)|([ \t]+)|(.)", re.S)


def animate(stream) -> bool:
    """Only animate interactively: never for pipes, CI, dumb terminals or opt-out."""
    try:
        interactive = stream.isatty()
    except (AttributeError, OSError):
        return False
    return (
        interactive
        and not os.environ.get(ANIMATION_OFF)
        and not os.environ.get("CI")
        and os.environ.get("TERM") != "dumb"
    )


@contextmanager
def _keypress():
    """Yield ``wait(seconds) -> bool``: sleep, returning True if a key was pressed."""
    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            raise OSError
        saved = termios.tcgetattr(fd)
    except (ImportError, OSError, ValueError, AttributeError):
        yield lambda seconds: bool(time.sleep(max(0.0, seconds)))
        return

    def wait(seconds: float) -> bool:
        ready, _, _ = select.select([fd], [], [], max(0.0, seconds))
        if ready:
            os.read(fd, 64)  # Consume the key so it never reaches the shell.
            return True
        return False

    tty.setcbreak(fd)
    try:
        yield wait
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


class _Rhythm:
    """Human-like delays: uneven keystrokes, pauses after punctuation and lines."""

    def __init__(self, seed: int | None = None) -> None:
        self._random = random.Random(seed)

    def keystroke(self, glyph: str) -> float:
        delay = self._random.uniform(0.014, 0.042)
        if glyph in ".,:;!?·":
            delay += self._random.uniform(0.08, 0.16)
        elif self._random.random() < 0.035:
            delay += self._random.uniform(0.08, 0.2)  # a brief hesitation
        return delay

    def word(self) -> float:
        return self._random.uniform(0.012, 0.04)

    def line(self, typed: bool) -> float:
        return self._random.uniform(0.12, 0.22) if typed else self._random.uniform(0.004, 0.018)


def typewrite(rendered: str, stream, *, seed: int | None = None) -> None:
    """Reveal pre-rendered ANSI output: lines print quickly, marked text is typed.

    Any key (or Ctrl-C) prints the remainder at once. The real cursor stays
    visible so it follows the typing.
    """
    tokens = [match.group(0) for match in _TOKENS.finditer(rendered)]
    rhythm = _Rhythm(seed)
    slow = typed_line = skipped = False
    index = 0

    def rest() -> str:
        return "".join(tokens[index + 1:]).replace(SLOW, "")

    with _keypress() as wait:
        try:
            for index, token in enumerate(tokens):
                if token == SLOW:
                    slow = not slow
                    continue
                stream.write(token)
                delay = 0.0
                if token == "\n":
                    delay = rhythm.line(typed_line)
                    typed_line = False
                elif slow and token.startswith("\x1b"):
                    continue
                elif slow and token.isspace():
                    delay = rhythm.word()
                elif slow:
                    delay = rhythm.keystroke(token)
                    typed_line = True
                if delay:
                    stream.flush()
                    if wait(delay):
                        skipped = True
                        break
        except KeyboardInterrupt:
            skipped = True
        if skipped:
            stream.write(rest())
        stream.write("\x1b[0m")
        stream.flush()
