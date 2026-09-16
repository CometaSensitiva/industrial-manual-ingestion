"""Human-readable CLI presentation; bundle contracts remain unchanged."""
from __future__ import annotations
import json
import os
import sys
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from rich.console import Console
from rich.table import Table


def _summary(title, rows):
    console = Console(highlight=False)
    console.print(title, style="bold")
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim")
    table.add_column(overflow="fold")
    for name, value in rows:
        table.add_row(str(name), str(value))
    console.print(table)


def show_detection(detected, *, verbose=False):
    _summary("Document profile", [
        ("Profile", detected.profile.value.replace("_", " ")),
        ("Embedded outline", "yes" if detected.embedded_outline else "no"),
        ("Text layer", "usable" if detected.text_layer else "OCR required"),
        ("Sampled pages", ", ".join(map(str, detected.sampled_pages))),
    ])
    for reason in (detected.reasons if verbose else detected.reasons[-1:]):
        print(f"  {reason}")
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
    print("Software checks do not certify the accuracy of generated descriptions.")


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
        print(f"  Warning: {warning}")
    if not verbose and len(manifest.warnings) > 3:
        print("  More warnings in run.json; use --verbose to display all.")
    print("Included in bundle does not mean semantically verified.")


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
