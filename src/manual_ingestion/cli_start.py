"""Guided run: a few plain questions, the equivalent command, then a normal ingest."""
from __future__ import annotations

import json
import shlex
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.text import Text

from .cli_display import (
    ACCENT,
    BLUE,
    MUTED,
    NEGATIVE,
    POSITIVE,
    WARNING,
    bundle_name_for,
    display_path,
    operational_output,
    section,
    shell_path,
    show_error,
)
from .detection import detect_pdf_capabilities

OLLAMA_URL = "http://127.0.0.1:11434"


def clean_path(raw: str) -> Path:
    """Accept what a drag-and-drop or a copy leaves behind: quotes, escapes, ~."""
    value = raw.strip()
    try:
        parts = shlex.split(value)
    except ValueError:
        parts = []
    if len(parts) == 1:
        value = parts[0]
    return Path(value).expanduser()


def ollama_status(model: str, base_url: str = OLLAMA_URL) -> tuple[bool, str]:
    """Return (ready, explanation) without ever raising or waiting long."""
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=0.8) as response:
            names = {entry.get("name") for entry in json.load(response).get("models", [])}
    except Exception:
        return False, "Ollama is not running. Start it with: ollama serve"
    if model not in names:
        return False, f"Ollama is running, but {model} is missing. Get it with: ollama pull {model}"
    return True, f"Ollama is running with {model}."


class _Ask(Prompt):
    prompt_suffix = " "


class _YesNo(Confirm):
    prompt_suffix = " "


class _Guide:
    def __init__(self, console: Console) -> None:
        self.console = console

    def step(self, number: int, question: str, help_text: str) -> None:
        line = Text("  ")
        line.append(f"{number:02d}  ", style=ACCENT)
        line.append(question, style="bold")
        self.console.print()
        self.console.print(line)
        self.console.print(f"      {help_text}", style=MUTED)

    def result(self, ok: bool, message: str) -> None:
        line = Text("      ")
        line.append("[ok] " if ok else "[!!] ", style=POSITIVE if ok else NEGATIVE)
        line.append(message, style=MUTED if ok else "")
        self.console.print(line, soft_wrap=True)

    def ask(self, default: str | None = None) -> str:
        prompt = Text("    › ", style=ACCENT)
        if default is None:
            return _Ask.ask(prompt, console=self.console)
        return _Ask.ask(prompt, console=self.console, default=default)

    def confirm(self, question: str, default: bool) -> bool:
        prompt = Text("    › ", style=ACCENT)
        prompt.append(question)
        return _YesNo.ask(prompt, console=self.console, default=default)


def _choose_pdf(guide: _Guide) -> Path:
    guide.step(1, "Which PDF?", "Drag the file into this window, then press Enter.")
    while True:
        path = clean_path(guide.ask())
        if not str(path).strip() or str(path) == ".":
            guide.result(False, "Nothing entered yet. Drag a PDF here, or type its path.")
        elif not path.is_file():
            guide.result(False, f"No file at {path}. Check the path and try again.")
        elif path.suffix.lower() != ".pdf":
            guide.result(False, f"{path.name} is not a PDF. Choose a .pdf file.")
        else:
            break
    with guide.console.status("Reading the PDF…", spinner="line", spinner_style=ACCENT):
        with operational_output():
            detected = detect_pdf_capabilities(path)
    text_layer = "text layer found" if detected.text_layer else "scanned pages (OCR, experimental)"
    guide.result(True, f"{path.name} · {detected.profile.value.replace('_', ' ')} · {text_layer}")
    return path


def _choose_output(guide: _Guide, pdf: Path) -> Path:
    guide.step(2, "Where should the bundle go?", "Press Enter to accept the suggested folder name.")
    default = str(bundle_name_for(pdf))
    while True:
        out = clean_path(guide.ask(default))
        if out.exists():
            guide.result(False, f"{out} already exists. Choose a new name.")
        elif not out.parent.exists():
            guide.result(False, f"The folder {out.parent} does not exist.")
        else:
            guide.result(True, f"The bundle will be written to {display_path(out)}")
            return out


def run_guided(
    parse: Callable[[list[str]], object],
    ingest: Callable[[object], int],
    *,
    model: str,
) -> int:
    """Ask, confirm, then hand a regular ``ingest`` command to the normal path."""
    console = Console(highlight=False, markup=False)
    if not sys.stdin.isatty() or not console.is_terminal:
        show_error(
            "The guided run needs an interactive terminal",
            hint="manual-ingestion ingest PDF --out NEW_BUNDLE --page-previews",
        )
        return 2

    guide = _Guide(console)
    try:
        console.print()
        section(console, "Guided run")
        console.print("  Four short questions, then the tool does the rest.", style=MUTED)
        console.print("  Press Ctrl-C at any time to cancel. Nothing is written before you confirm.", style=MUTED)

        pdf = _choose_pdf(guide)
        out = _choose_output(guide, pdf)

        ready, explanation = ollama_status(model)
        guide.step(3, "Describe images with the local model?", explanation)
        if not ready:
            console.print("      Without descriptions the bundle is marked experimental.", style=MUTED)
        enrich = guide.confirm("Describe images", default=ready)

        guide.step(4, "Add page previews for the viewer?", "Recommended: the viewer shows each original page.")
        previews = guide.confirm("Add page previews", default=True)

        argv = ["ingest", str(pdf), "--out", str(out)]
        if previews:
            argv.append("--page-previews")
        if not enrich:
            argv.append("--no-enrich")

        console.print()
        section(console, "Ready")
        console.print("  This is the command it will run. Next time you can paste it directly:", style=MUTED)
        console.print()
        command = " ".join(shell_path(part) for part in ["manual-ingestion", *argv])
        console.print(f"      {command}", style=BLUE, soft_wrap=True)
        console.print()
        if not guide.confirm("Start now", default=True):
            console.print("\n  Nothing was written.", style=MUTED)
            return 0
    except (KeyboardInterrupt, EOFError):
        console.print("\n\n  Cancelled. Nothing was written.", style=WARNING)
        return 130

    console.print()
    return ingest(parse(argv))
