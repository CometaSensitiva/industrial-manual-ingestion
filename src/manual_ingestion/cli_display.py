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
    """Style root help without changing argparse's parsing or error behavior."""

    def print_help(self, file=None):
        if self.prog != "manual-ingestion":
            return super().print_help(file)
        console = Console(file=file or sys.stdout, highlight=False, markup=False)
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
        console.print("  Try the included example", style="bold")
        console.print("  Run these from the industrial-manual-ingestion project folder.", style="dim")
        console.print("  Inspect the sample PDF:", style="dim")
        console.print("  manual-ingestion detect examples/synthetic-manual.pdf", style=BLUE)
        console.print("  Check the existing sample bundle:", style="dim")
        console.print("  manual-ingestion validate examples/synthetic-bundle", style=BLUE)
        console.print()
        console.print("  Use your own PDF", style="bold")
        console.print("  Type manual-ingestion detect followed by a space, then drag your PDF", style="dim")
        console.print("  into the terminal to insert its path. Press Enter.", style="dim")
        console.print()
        console.print("  Learn how to create a bundle:", style="dim")
        console.print("  manual-ingestion ingest --help", style=BLUE)
        console.print("  Options for inspecting a PDF:", style="dim")
        console.print("  manual-ingestion detect --help", style=BLUE)
        console.print()
        console.print("  Add --json or --verbose to detect, ingest or validate commands.", style="dim")
        console.print("  Show this help: manual-ingestion --help", style="dim")
        console.print()


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
    manual = json.loads((Path(outcome.output_dir) / manifest.artifacts.manual).read_text())
    def elements(nodes):
        for node in nodes:
            if node['type'] == 'chapter':
                yield from elements(node['content'])
            else:
                yield node
    images = [e for e in elements(manual['content']) if e['type'] == 'image']
    _summary("Bundle ready", [
        ("Status", manifest.status.value),
        ("Profile", manifest.pipeline.profile.value.replace("_", " ")),
        ("Pages", len(manual['metadata']['pages_processed'])),
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
