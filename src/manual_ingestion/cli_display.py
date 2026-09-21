"""Human-readable CLI presentation; bundle contracts remain unchanged."""
from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

from rich.console import Console
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from . import __version__

ACCENT = "#b4a0e5"
BLUE = "#97bafa"
LOGO = '         ..\n       :-.\n      -+.   .::..:-:.   .\n     :+. .-*#*####%%#+=-:.\n    .*-.=*##%%#%%#****#+:.\n    -%+*###%@%%##*#*++*#+=--.\n    +#*+*%@%##%%##*++***++==-:\n   .=**+*+*%###*+++*+++**====-.\n..=*#**++=+#%%#*+++++++*++=-..\n++#*#%*=***+**##******+++==:\n**=*%#*=*****+**+=+++++==+=.\n-= =#**+**+***+++===++++++:\n.: .++=+**#*+*++#++++++==-..\n..  .=-:-++*+++*#+---:::.....\n     .. .:+*****+=:::::::..\n         -****#*++=::::....\n       .+#+:=***--+=::.......\n       +*=::::-:-+=::..:.:::...'


class PresentationParser(argparse.ArgumentParser):
    """Present concise, task-oriented help while retaining argparse parsing."""

    def print_help(self, file=None):
        console = Console(file=file or sys.stdout, highlight=False, markup=False)
        command = self.prog.removeprefix("manual-ingestion").strip()
        if command:
            self._print_command_help(console, command)
            return

        copy = Text()
        copy.append("INDUSTRIAL\nMANUAL INGESTION\n", style="bold")
        copy.append(f"v{__version__}\n\n", style="dim")
        copy.append("PDFs into structured, traceable content.\n\n")
        for name, description in (
            ("detect", "Inspect a PDF"),
            ("ingest", "Create a bundle"),
            ("validate", "Check a bundle"),
            ("schema", "Print a JSON Schema"),
        ):
            copy.append(f"{name:10}", style=f"bold {ACCENT}")
            copy.append(description + "\n")
        copy.append("\nStart with the examples below.\n", style="dim")
        copy.append("Every command starts with manual-ingestion.", style="dim")
        console.print()
        if console.is_terminal and console.width >= 90:
            table = Table.grid(padding=(0, 4))
            table.add_column(width=33)
            table.add_column()
            table.add_row(Text(LOGO, style=ACCENT), copy)
            console.print(Padding(table, (0, 2)))
        else:
            console.print(Padding(copy, (0, 2)))
        console.print()
        console.print("  Quick tour", style="bold")
        console.print(
            "  Run these from the industrial-manual-ingestion project folder.",
            style="dim",
        )
        console.print("  1  Inspect the included PDF", style="dim")
        console.print("  manual-ingestion detect examples/synthetic-manual.pdf", style=BLUE)
        console.print("  2  Create a viewer-ready bundle without Qwen", style="dim")
        console.print(
            "  manual-ingestion ingest examples/synthetic-manual.pdf "
            "--out my-first-bundle --no-enrich --page-previews",
            style=BLUE,
        )
        console.print("  3  Check the new bundle", style="dim")
        console.print("  manual-ingestion validate my-first-bundle", style=BLUE)
        console.print(
            "  The --out folder must not already exist.",
            style="dim",
        )
        console.print()
        console.print("  Use your own PDF", style="bold")
        console.print(
            "  Open the guided ingest help, then drag your PDF into the terminal",
            style="dim",
        )
        console.print("  when you need its full path.", style="dim")
        console.print("  manual-ingestion ingest --help", style=BLUE)
        console.print()
        console.print(
            "  Machine output: --json   Diagnostics: --verbose   "
            "Main help: manual-ingestion --help",
            style="dim",
        )
        console.print()

    def error(self, message):
        console = Console(file=sys.stderr, highlight=False, markup=False)
        console.print()
        console.print("  Command not understood", style="bold red")
        console.print(f"  {message}")
        suggestion = self._error_suggestion(message)
        if suggestion:
            console.print(f"  Try: {suggestion}", style=BLUE)
        console.print(f"  Help: {self.prog} --help", style="dim")
        console.print()
        self.exit(2)

    def _print_command_help(self, console: Console, command: str) -> None:
        printers = {
            "detect": self._print_detect_help,
            "ingest": self._print_ingest_help,
            "validate": self._print_validate_help,
            "schema": self._print_schema_help,
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
        console.print(f"  {description}", style="dim")
        console.print()

    @staticmethod
    def _section(console: Console, title: str) -> None:
        console.print(f"  {title}", style="bold")

    @staticmethod
    def _command(console: Console, value: str) -> None:
        console.print(f"  {value}", style=BLUE)

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
            style="dim",
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
            style="dim",
        )
        console.print(
            "  If you split a command across lines, \\ must be the final character on each continued line.",
            style="dim",
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

    def _print_schema_help(self, console: Console) -> None:
        self._heading(console, "schema", "Print a canonical JSON Schema.")
        self._section(console, "Choose one contract")
        self._command(console, "manual-ingestion schema manual")
        console.print("  Schema for manual.json", style="dim")
        self._command(console, "manual-ingestion schema run")
        console.print("  Schema for run.json", style="dim")
        console.print()
        console.print(
            "  This command accepts the word manual or run, not a file path.",
            style="dim",
        )
        console.print()

    def _error_suggestion(self, message: str) -> str | None:
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


def _note(message, style="dim"):
    note = Padding(Text(message, style=style), (0, 2))
    Console(highlight=False, markup=False).print(note)


def _summary(title, rows):
    console = Console(highlight=False, markup=False)
    console.print()
    console.print(Text("  " + title, style=f"bold {ACCENT}"))
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim")
    table.add_column(overflow="fold")
    for name, value in rows:
        style = BLUE if name in {"Bundle", "Output"} else ""
        if name == "Result":
            style = "green" if str(value) == "passed" else "red"
        table.add_row(Text(str(name)), Text(str(value), style=style))
    console.print(Padding(table, (1, 2)))


def show_detection(detected, *, verbose=False):
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


def show_validation(report, run_dir, *, verbose=False):
    failed = [check for check in report.checks if not check.passed]
    _summary("Bundle checks", [
        ("Result", report.status),
        ("Checks", f"{len(report.checks) - len(failed)}/{len(report.checks)} passed"),
        ("Bundle", run_dir),
    ])
    for check in report.checks if verbose else failed:
        print(f"  {'OK' if check.passed else 'FAIL'}  {check.id}: {check.message}")
    _note("Software checks do not certify the accuracy of generated descriptions.")


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
        ("Output", outcome.output_dir),
    ])
    for warning in manifest.warnings if verbose else manifest.warnings[:3]:
        _note(f"Warning: {warning}", "yellow")
    if not verbose and len(manifest.warnings) > 3:
        print("  More warnings in run.json; use --verbose to display all.")
    _note("Included in bundle does not mean semantically verified.")


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
