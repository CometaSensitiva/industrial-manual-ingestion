"""Human-readable CLI presentation; bundle contracts remain unchanged."""
from __future__ import annotations

import argparse
import difflib
import io
import json
import os
import re
import shlex
import sys
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

from rich.console import Console
from rich.padding import Padding
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from . import __version__
from .cli_effects import SLOW, animate, typewrite

# Commands as shown on the home screen: name, argument shape, purpose.
COMMANDS = (
    ("start", "", "Guided run, step by step"),
    ("detect", "PDF", "Inspect a PDF"),
    ("ingest", "PDF --out DIR", "Create a bundle"),
    ("validate", "BUNDLE", "Check a bundle"),
    ("schema", "manual|run", "Print a JSON Schema"),
    ("help", "[COMMAND]", "Guided help for one command"),
)

VIEWER_URL = "https://CometaSensitiva.github.io/industrial-manual-ingestion/"

# The palette follows the portrait: lavender for identity, cobalt for actions.
ACCENT = "#b4a0e5"
BLUE = "#7f97ff"
# Explicit greys instead of the terminal's "dim", which can vanish on dark themes.
MUTED = "#9a9ab2"
LINE = "#6f6f88"
POSITIVE = "#86d4a0"
WARNING = "#f0c674"
NEGATIVE = "#ff8f8f"
TONE_STYLES = {"h": ACCENT, "j": BLUE, ".": LINE}
# Portrait and tone mask share one shape: h = hair, j = jacket, . = line work.
LOGO = "             ./\n            //      :/- ::::  .\n           ::    :///::::::::::=\n           .:.::/.   ::::::://::-::.\n         .||\\-:/  :::::::::--:::--==\\.\n          |\\|\\-:-|.-\\:::--:::-::--==\\\\\\\n     .  ..:/-----\\.::::--:-/--\\:--== .\n   ../\\///. /=-:::-\\:::::::::\\\\\\|==|\n  \\:|//-/|:/:=|.::::::::=-------=--\n   . /\\##|||--:::::::----==:------\n      |\\##\\\\\\==::.:::/|::---=-=++++_|\n         \\#\\\\**+-\\:-:\\/:=*********##|\n           \\\\###/-:::.:---**+****###\\\n             \\//:-=\\:::====*+**#**##|\\\n              //+***+=+//-+***********\\\n            _-*********|=***************\\\n            //-+******|-:+***************\\\n          :/./+*****+*\\-:+*+//_\\\\\\***#****|\n         _/ /+*******++***=|///\\\\\\|*##*##*#\n          .=*********#\\***||\\\\\\\\*|/#####*##\n         /+*******####****=\\/\\--//*########|\n.:--:::-=*******####********+=_==*#########|"
LOGO_TONES = "             ..\n            ..      h.. hhhh  .\n           ..    hhh.hhhhhhhhhhh\n           .....h.   hhhhhhhhhhhhhh.\n         ...hhhh  hhhhhhhhhhhhhhhhhhh.\n          .hhhhhh...hhhhhhhhhhhhhhhh...\n     .  ..hhhhh..h.hhhhhhhhhhhhhhhhh .\n   ........ hh.....hhhhhhhhhhhhhhhhh\n  ....h...hhhh........hhhhhh..hhhhh\n   . .h...hhhh..........h......hhh\n      ....hh.h........hh.......hjjjjj\n         .....jj.........jjjjjjjjjj..\n           .....j.........jjjjjjjjj..\n             ..............jjjjjjjj...\n              .jjjjjj......jjjjjjjjjjjj\n            .jjjjjjjjjj..jjjjjjjjjjjjjjjj\n            ...jjjjjjj...jjjjjjjjjjjjjjjjj\n          ...jjjjjjjjj....jjjj..jjjjjjjjjjj\n         .. jjjjjjjjjj..jjj........jjjjjjj.\n          .jjjjjjjjjjj.jjj............jjjj.\n         jjjjjjjjjjjjjjjjjj.......j....j....\n.......jjjjjjjjjj...jjjjjjjjjj...jj........."


def portrait() -> Text:
    """Colour the ASCII portrait from its tone mask, one run at a time."""
    text = Text()
    for row, tones in zip(LOGO.split("\n"), LOGO_TONES.split("\n")):
        start = 0
        for index in range(1, len(row) + 1):
            if index == len(row) or tones[index] != tones[start]:
                text.append(row[start:index], style=TONE_STYLES.get(tones[start]))
                start = index
        text.append("\n")
    text.rstrip()
    return text


class PresentationParser(argparse.ArgumentParser):
    """Present concise, task-oriented help while retaining argparse parsing."""

    def print_help(self, file=None):
        stream = file or sys.stdout
        command = self.prog.removeprefix("manual-ingestion").strip()
        if command:
            console = Console(file=stream, highlight=False, markup=False)
            self._print_command_help(console, command)
            return
        if file is None and animate(stream):
            terminal = Console(file=stream)
            buffer = io.StringIO()
            console = Console(
                file=buffer,
                force_terminal=True,
                color_system=terminal.color_system,
                width=terminal.width,
                highlight=False,
                markup=False,
            )
            self._print_root_help(console, typed=True)
            typewrite(buffer.getvalue(), stream)
            return
        self._print_root_help(Console(file=stream, highlight=False, markup=False))

    def _print_root_help(self, console: Console, *, typed: bool = False) -> None:
        mark = SLOW if typed else ""
        copy = Text()
        copy.append(f"{mark}INDUSTRIAL MANUAL INGESTION{mark}\n", style="bold")
        copy.append(f"v{__version__} · local PDF toolkit\n\n", style=MUTED)
        copy.append(f"{mark}PDFs into structured, traceable content.{mark}\n\n")
        for name, arguments, description in COMMANDS:
            copy.append(f"{name:10}", style=f"bold {ACCENT}")
            copy.append(f"{arguments:16}", style=MUTED)
            copy.append(description + "\n")
        copy.append("\n")
        copy.append_text(pipeline())
        console.print()
        if console.is_terminal and console.width >= 100:
            table = Table.grid(padding=(0, 4))
            table.add_column(width=44, no_wrap=True)
            table.add_column(vertical="middle")
            table.add_row(portrait(), copy)
            console.print(Padding(table, (0, 2)))
        else:
            console.print(Padding(copy, (0, 2)))
        console.print()
        self._section(console, "Start here")
        console.print("  New to the tool, or using your own PDF? One command asks four questions")
        console.print("  and does the rest.")
        console.print()
        self._command(console, "manual-ingestion start")
        console.print()
        self._section(console, "Or try each step yourself")
        console.print(
            "  Run these from the industrial-manual-ingestion project folder.",
            style=MUTED,
        )
        console.print()
        self._step(console, 1, "Inspect the included PDF")
        self._command(console, "manual-ingestion detect examples/synthetic-manual.pdf")
        self._step(console, 2, "Create a viewer-ready bundle without Qwen")
        self._command(
            console,
            "manual-ingestion ingest examples/synthetic-manual.pdf "
            "--out my-first-bundle --no-enrich --page-previews",
        )
        self._step(console, 3, "Check the new bundle")
        self._command(console, "manual-ingestion validate my-first-bundle")
        console.print()
        console.print("  The --out folder must not already exist.", style=MUTED)
        console.print("  Every option of one command:", style=MUTED)
        self._command(console, "manual-ingestion help ingest")
        console.print()
        self._section(console, "Flags for detect, ingest and validate")
        table = Table.grid(padding=(0, 2))
        table.add_column(width=23, style=ACCENT, no_wrap=True)
        table.add_column()
        table.add_row("--json", "Machine-readable output on stdout")
        table.add_row("--verbose", "Reasons, logs and every check")
        console.print(Padding(table, (0, 2)))
        console.print()

    def error(self, message):
        console = Console(file=sys.stderr, highlight=False, markup=False)
        console.print()
        title = Text("  ")
        title.append("[!!] ", style=f"bold {NEGATIVE}")
        title.append("Command not understood", style="bold")
        console.print(title)
        suggestion = self._error_suggestion(message)
        typo = re.search(r"argument command: invalid choice: '([^']*)'", message)
        if typo:
            message = f"'{typo.group(1)}' is not a manual-ingestion command."
        console.print(f"       {message}")
        if suggestion:
            console.print(f"  Try  {suggestion}", style=BLUE)
        console.print(f"  Help {self.prog} --help", style=MUTED)
        console.print()
        self.exit(2)

    def _print_command_help(self, console: Console, command: str) -> None:
        printers = {
            "detect": self._print_detect_help,
            "ingest": self._print_ingest_help,
            "validate": self._print_validate_help,
            "schema": self._print_schema_help,
            "start": self._print_start_help,
        }
        printer = printers.get(command)
        if printer is None:
            super().print_help(console.file)
            return
        printer(console)

    @staticmethod
    def _heading(console: Console, command: str, description: str) -> None:
        console.print()
        title = Text("  manual-ingestion ")
        title.append(command, style=f"bold {ACCENT}")
        console.print(title)
        console.print(f"  {description}", style=MUTED)
        console.print()

    @staticmethod
    def _section(console: Console, title: str) -> None:
        section(console, title)

    @staticmethod
    def _step(console: Console, number: int, description: str) -> None:
        line = Text("  ")
        line.append(f"{number:02d}  ", style=ACCENT)
        line.append(description)
        console.print(line)

    @staticmethod
    def _command(console: Console, value: str) -> None:
        # soft_wrap lets the terminal wrap long commands so copies stay intact.
        console.print(f"      {value}", style=BLUE, soft_wrap=True)

    @staticmethod
    def _options(
        console: Console,
        title: str,
        rows: list[tuple[str, str]],
    ) -> None:
        PresentationParser._section(console, title)
        table = Table.grid(padding=(0, 2))
        table.add_column(width=23, style=ACCENT, no_wrap=True)
        table.add_column(overflow="fold")
        for option, description in rows:
            table.add_row(option, description)
        console.print(Padding(table, (0, 2)))
        console.print()

    def _print_detect_help(self, console: Console) -> None:
        self._heading(console, "detect", "Inspect one PDF and show the selected route.")
        self._section(console, "Usage")
        self._command(console, "manual-ingestion detect PDF [--json] [--verbose]")
        console.print()
        self._options(
            console,
            "Inputs and options",
            [
                ("PDF", "Path to the source PDF. Drag the file into the terminal to insert it."),
                ("--json", "Print machine-readable output."),
                ("--verbose", "Show routing reasons and technical details."),
            ],
        )
        self._section(console, "Example")
        self._command(
            console,
            'manual-ingestion detect "/Users/name/Desktop/manual.pdf"',
        )
        console.print()

    def _print_ingest_help(self, console: Console) -> None:
        self._heading(console, "ingest", "Create one structured run bundle from a PDF.")
        self._section(console, "Usage")
        self._command(
            console,
            "manual-ingestion ingest PDF --out NEW_BUNDLE [options]",
        )
        console.print(
            "  PDF and NEW_BUNDLE may be absolute paths. The output folder must not exist yet.",
            style=MUTED,
        )
        console.print()
        self._options(
            console,
            "Common options",
            [
                ("--out NEW_BUNDLE", "Required. Folder that will contain the completed bundle."),
                ("--page-previews", "Include full-page PNG previews for the viewer."),
                ("--no-enrich", "Skip Qwen image descriptions; status remains experimental."),
                ("--pages 1-3,5", "Process only selected pages; status remains experimental."),
                ("--language CODE", "Document language metadata. Default: en."),
                ("--title TITLE", "Override the document title."),
                ("--json", "Print machine-readable output."),
                ("--verbose", "Show library logs and every warning."),
            ],
        )
        self._options(
            console,
            "Advanced options",
            [
                ("--run-id ID", "Choose the run identifier instead of generating one."),
                ("--ollama-url URL", "Use a different local Ollama endpoint."),
                ("--paddle-python PATH", "Python executable for isolated scanned-PDF OCR."),
                ("--paddle-cache DIR", "Cache directory for the scanned-PDF runtime."),
                ("--no-progress", "Disable the interactive progress display."),
                ("--write-failure-report", "Save a compact report if ingestion fails."),
            ],
        )
        self._section(console, "Fast example — no Qwen")
        self._command(
            console,
            "manual-ingestion ingest examples/synthetic-manual.pdf "
            "--out my-first-bundle --no-enrich --page-previews",
        )
        console.print()
        self._section(console, "Your PDF — with Qwen and viewer previews")
        self._command(
            console,
            'manual-ingestion ingest "/Users/name/Desktop/manual.pdf" '
            '--out "/Users/name/Desktop/manual-bundle" --page-previews',
        )
        console.print(
            "  Tip: type the command and a space, then drag the PDF into the terminal.",
            style=MUTED,
        )
        console.print(
            "  If you split a command across lines, \\ must be the final character on each continued line.",
            style=MUTED,
        )
        console.print()

    def _print_validate_help(self, console: Console) -> None:
        self._heading(console, "validate", "Check an existing bundle without changing it.")
        self._section(console, "Usage")
        self._command(console, "manual-ingestion validate BUNDLE [--json] [--verbose]")
        console.print()
        self._options(
            console,
            "Inputs and options",
            [
                ("BUNDLE", "Folder containing run.json and the bundle artifacts."),
                ("--json", "Print the complete validation report as JSON."),
                ("--verbose", "Show every individual software check."),
            ],
        )
        self._section(console, "Example")
        self._command(
            console,
            'manual-ingestion validate "/Users/name/Desktop/manual-bundle"',
        )
        console.print()

    def _print_start_help(self, console: Console) -> None:
        self._heading(console, "start", "Create a bundle by answering four short questions.")
        self._section(console, "Usage")
        self._command(console, "manual-ingestion start")
        console.print()
        self._options(
            console,
            "What it asks",
            [
                ("01 PDF", "Drag the file into the terminal; quotes and escapes are handled."),
                ("02 Output folder", "A new folder name is suggested for you."),
                ("03 Image descriptions", "Offered only when Ollama and the model are ready."),
                ("04 Page previews", "Recommended for the viewer."),
            ],
        )
        console.print(
            "  It shows the equivalent ingest command before starting, so you can reuse it.",
            style=MUTED,
        )
        console.print()

    def _print_schema_help(self, console: Console) -> None:
        self._heading(console, "schema", "Print a canonical JSON Schema.")
        self._section(console, "Choose one contract")
        self._command(console, "manual-ingestion schema manual")
        console.print("  Schema for manual.json", style=MUTED)
        self._command(console, "manual-ingestion schema run")
        console.print("  Schema for run.json", style=MUTED)
        console.print()
        console.print(
            "  This command accepts the word manual or run, not a file path.",
            style=MUTED,
        )
        console.print()

    def _error_suggestion(self, message: str) -> str | None:
        typo = re.search(r"invalid choice: '([^']*)'", message)
        if typo and not self.prog.endswith(" schema"):
            names = [name for name, _, _ in COMMANDS]
            close = difflib.get_close_matches(typo.group(1), names, n=1)
            return f"manual-ingestion {close[0]}" if close else "manual-ingestion help"
        if "required: --out" in message:
            return "manual-ingestion ingest PDF --out NEW_BUNDLE"
        if self.prog.endswith(" schema"):
            return "manual-ingestion schema manual"
        if "unrecognized arguments: -- out" in message:
            return "use --out without a space"
        if "required: source" in message.lower():
            return f"{self.prog} PDF"
        if "required: run_dir" in message.lower():
            return "manual-ingestion validate BUNDLE"
        return None


def pipeline() -> Text:
    """The product in one line, shared with the viewer footer."""
    text = Text()
    for index, stage in enumerate(("PDF", "Structure", "Bundle")):
        if index:
            text.append(" → ", style=LINE)
        text.append(stage, style=ACCENT)
    return text


def section(console: Console, title: str) -> None:
    rule = Rule(Text(title, style=f"bold {ACCENT}"), align="left", style=LINE, characters="─")
    console.print(Padding(rule, (0, 2)))


def _note(message, style=MUTED):
    note = Padding(Text(message, style=style), (0, 2))
    Console(highlight=False, markup=False).print(note)


def _summary(title, rows):
    console = Console(highlight=False, markup=False)
    console.print()
    section(console, title)
    table = Table.grid(padding=(0, 2))
    table.add_column(style=MUTED, no_wrap=True)
    table.add_column(overflow="fold")
    for index, (name, value) in enumerate(rows):
        style = BLUE if name in {"Bundle", "Output"} else ""
        cell = Text(str(value), style=style)
        if name in {"Result", "Status"}:
            token = {
                "passed": ("[ok] ", POSITIVE),
                "validated": ("[ok] ", POSITIVE),
                "experimental": ("[!]  ", WARNING),
            }.get(str(value), ("[!!] ", NEGATIVE))
            cell = Text.assemble(token, str(value))
        branch = "└─ " if index == len(rows) - 1 else "├─ "
        table.add_row(Text.assemble((branch, LINE), name), cell)
    console.print(Padding(table, (1, 2)))


def shell_path(path) -> str:
    """A path the user can paste back into the shell as a single argument."""
    return shlex.quote(str(display_path(path)))


def display_path(path) -> Path:
    """Prefer the short form relative to the current folder when there is one."""
    path = Path(path)
    try:
        return path.resolve().relative_to(Path.cwd().resolve())
    except (OSError, ValueError):
        return path


def bundle_name_for(source) -> Path:
    """Suggest ``<pdf-stem>-bundle`` in the current folder, never an existing one."""
    stem = Path(source).stem or "manual"
    candidate = Path(f"{stem}-bundle")
    counter = 2
    while candidate.exists():
        candidate = Path(f"{stem}-bundle-{counter}")
        counter += 1
    return candidate


def show_next(steps):
    """End human output with the obvious next commands, ready to copy."""
    console = Console(highlight=False, markup=False)
    console.print()
    section(console, "Next")
    for description, command in steps:
        line = Text("  › ", style=ACCENT)
        line.append(description, style=MUTED)
        console.print(line)
        console.print(f"    {command}", style=BLUE, soft_wrap=True)
    console.print()


def show_error(title, detail=None, hint=None):
    """One consistent error block on stderr: what happened and what to try."""
    console = Console(file=sys.stderr, highlight=False, markup=False)
    console.print()
    line = Text("  ")
    line.append("[!!] ", style=f"bold {NEGATIVE}")
    line.append(title, style="bold")
    console.print(line)
    if detail:
        console.print(f"       {detail}", style=MUTED)
    if hint:
        console.print(f"  Try  {hint}", style=BLUE)
    console.print()


def show_detection(detected, *, verbose=False, source=None):
    _summary("Document profile", [
        ("Profile", detected.profile.value.replace("_", " ")),
        ("Embedded outline", "yes" if detected.embedded_outline else "no"),
        ("Text layer", "usable" if detected.text_layer else "OCR required"),
        ("Sampled pages", ", ".join(map(str, detected.sampled_pages))),
    ])
    for reason in (detected.reasons if verbose else []):
        _note(reason)
    if verbose:
        print(json.dumps(detected.model_dump(mode="json"), indent=2))
        print("Profile confidence is a routing heuristic, not a measured probability.")
    if source is not None:
        show_next([
            ("Create a bundle from this PDF", f"manual-ingestion ingest {shell_path(source)} "
             f"--out {shell_path(bundle_name_for(source))} --page-previews"),
            ("Or let the guided run ask the questions", "manual-ingestion start"),
        ])


FAILED_CHECKS_SHOWN = 8


def show_validation(report, run_dir, *, verbose=False):
    failed = [check for check in report.checks if not check.passed]
    _summary("Bundle checks", [
        ("Result", report.status),
        ("Checks", f"{len(report.checks) - len(failed)}/{len(report.checks)} passed"),
        ("Bundle", run_dir),
    ])
    shown = report.checks if verbose else failed[:FAILED_CHECKS_SHOWN]
    if shown:
        table = Table.grid(padding=(0, 2))
        table.add_column(no_wrap=True)
        table.add_column(style="bold", no_wrap=True)
        table.add_column(style=MUTED, overflow="fold")
        for check in shown:
            token = ("[ok]", POSITIVE) if check.passed else ("[!!]", NEGATIVE)
            table.add_row(Text(*token), check.id, check.message)
        Console(highlight=False, markup=False).print(Padding(table, (0, 2, 1, 2)))
    if not verbose and len(failed) > FAILED_CHECKS_SHOWN:
        _note(f"{len(failed) - FAILED_CHECKS_SHOWN} more failed checks; add --verbose to see them all.")
    _note("Software checks do not certify the accuracy of generated descriptions.")
    if report.status == "passed":
        show_next([("Open it in the viewer: Open bundle › Choose local folder", VIEWER_URL)])
    else:
        show_next([("See every check", f"manual-ingestion validate {shell_path(run_dir)} --verbose")])


def show_outcome(outcome, *, verbose=False):
    manifest = outcome.manifest
    output_dir = Path(outcome.output_dir)
    manual = json.loads((output_dir / manifest.artifacts.manual).read_text())
    def elements(nodes):
        for node in nodes:
            if node['type'] == 'chapter':
                yield from elements(node['content'])
            else:
                yield node
    images = [e for e in elements(manual['content']) if e['type'] == 'image']
    page_previews = list(
        (output_dir / manifest.artifacts.assets / "pages").glob("page_*.png")
    )
    processed_pages = manual['metadata']['pages_processed']
    _summary("Bundle ready", [
        ("Status", manifest.status.value),
        ("Profile", manifest.pipeline.profile.value.replace("_", " ")),
        ("Pages", len(processed_pages)),
        ("Page previews", f"{len(page_previews)}/{len(processed_pages)}"),
        ("Images with descriptions", f"{sum(bool(e.get('caption_generated')) for e in images)}/{len(images)}"),
        ("Warnings", len(manifest.warnings)),
        ("Output", display_path(outcome.output_dir)),
    ])
    for warning in manifest.warnings if verbose else manifest.warnings[:3]:
        _note(f"[!]  {warning}", WARNING)
    if not verbose and len(manifest.warnings) > 3:
        _note("More warnings in run.json; use --verbose to display all.")
    _note("Included in bundle does not mean semantically verified.")
    show_next([
        ("Check the bundle", f"manual-ingestion validate {shell_path(outcome.output_dir)}"),
        ("Open it in the viewer: Open bundle › Choose local folder", VIEWER_URL),
    ])


@contextmanager
def operational_output(*, verbose=False):
    """Keep library prints off machine stdout and quiet download progress."""
    switches = {"HF_HUB_DISABLE_PROGRESS_BARS": "1", "TQDM_DISABLE": "1"}
    previous = {key: os.environ.get(key) for key in switches}
    if not verbose:
        os.environ.update(switches)
    try:
        with redirect_stdout(sys.stderr):
            yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
