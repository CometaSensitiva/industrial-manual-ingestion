"""Public command-line interface for the fixed V2 ingestion path."""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .adapters.paddle_adapter import PaddleRuntimeConfig
from .cli_display import (
    PresentationParser,
    bundle_name_for,
    operational_output,
    shell_path,
    show_detection,
    show_error,
    show_outcome,
    show_validation,
)
from .cli_progress import RichProgressRenderer
from .cli_start import run_guided
from .detection import detect_pdf_capabilities
from .failure_report import build_failure_report, write_failure_report
from .models import ManualDocument, RunManifest
from .orchestrator import ingest_manual
from .progress import ProgressEvent
from .providers.ollama import OllamaCaptionProvider, OllamaConfig
from .validation import validate_run_bundle

OLLAMA_MODEL = "qwen3.5:4b"
DEFAULT_LANGUAGE = "en"
_PAGE_TOKEN = re.compile(r"[1-9]\d*(?:-[1-9]\d*)?")
_RUN_ID_UNSAFE = re.compile(r"[^\w]+", flags=re.UNICODE)


def parse_pages(value: str) -> list[int]:
    """Parse a strict one-based page list such as ``1-3,5``."""

    if not value or value != value.strip():
        raise argparse.ArgumentTypeError(
            "pages must use positive one-based numbers, for example 1-3,5"
        )

    pages: list[int] = []
    seen: set[int] = set()
    for token in value.split(","):
        if _PAGE_TOKEN.fullmatch(token) is None:
            raise argparse.ArgumentTypeError(
                f"invalid page token {token!r}; use positive numbers or ranges such as 1-3,5"
            )

        if "-" in token:
            first_text, last_text = token.split("-", maxsplit=1)
            first, last = int(first_text), int(last_text)
            if last < first:
                raise argparse.ArgumentTypeError(
                    f"descending page range {token!r} is not allowed"
                )
            expanded = range(first, last + 1)
        else:
            expanded = (int(token),)

        for page in expanded:
            if page in seen:
                raise argparse.ArgumentTypeError(
                    f"page {page} is selected more than once"
                )
            seen.add(page)
            pages.append(page)

    return pages


def build_parser() -> argparse.ArgumentParser:
    parser = PresentationParser(
        prog="manual-ingestion",
        description="Automatically ingest and validate an industrial manual PDF.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Guided run: answer a few questions")
    start.set_defaults(json=False, verbose=False)

    schema = subparsers.add_parser("schema", help="Print a canonical JSON Schema")
    schema.add_argument("contract", choices=["manual", "run"])

    detect = subparsers.add_parser(
        "detect",
        help="Inspect a PDF and report the automatically selected profile",
    )
    detect.add_argument("source", type=Path, metavar="SOURCE")

    ingest = subparsers.add_parser(
        "ingest",
        help="Create one final run bundle using automatic routing",
    )
    ingest.add_argument("source", type=Path, metavar="SOURCE")
    ingest.add_argument(
        "--out",
        type=Path,
        required=True,
        metavar="RUN_DIR",
        help="Final bundle directory (it must not already exist)",
    )
    ingest.add_argument("--run-id", metavar="ID")
    ingest.add_argument(
        "--pages",
        type=parse_pages,
        metavar="1-3,5",
        help=(
            "Diagnostic only: process an explicit page subset; "
            "the bundle remains experimental"
        ),
    )
    ingest.add_argument("--title")
    ingest.add_argument("--language", default=DEFAULT_LANGUAGE)
    ingest.add_argument("--ollama-url", metavar="URL")
    ingest.add_argument("--paddle-python", metavar="PATH")
    ingest.add_argument("--paddle-cache", type=Path, metavar="DIR")
    ingest.add_argument(
        "--page-previews",
        action="store_true",
        help="Render full-page PNG previews for the bundle viewer",
    )
    ingest.add_argument(
        "--no-enrich",
        action="store_true",
        help="Diagnostic only: skip visual captioning; the bundle remains experimental",
    )
    ingest.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable the interactive progress display",
    )
    ingest.add_argument(
        "--write-failure-report",
        action="store_true",
        help="Write a compact failure report beside RUN_DIR if ingestion fails",
    )

    validate = subparsers.add_parser(
        "validate",
        help="Validate an existing run bundle without modifying it",
    )
    validate.add_argument("run_dir", type=Path, metavar="RUN_DIR")
    for command in (detect, ingest, validate):
        command.add_argument("--json", action="store_true", help="Print machine-readable JSON")
        command.add_argument("--verbose", action="store_true", help="Show technical details and library logs")
    return parser


def _utc_now() -> datetime:
    return datetime.now(UTC)


def default_run_id(source: str | Path, *, now: datetime | None = None) -> str:
    """Return a portable run id derived from the source stem and UTC time."""

    source_stem = Path(source).stem.strip().casefold()
    safe_stem = _RUN_ID_UNSAFE.sub("-", source_stem).strip("-_") or "manual"
    instant = now or _utc_now()
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    timestamp = instant.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{safe_stem}-{timestamp}"


def _paddle_runtime(args: argparse.Namespace) -> PaddleRuntimeConfig | None:
    if args.paddle_python is None and args.paddle_cache is None:
        return None
    values: dict[str, object] = {}
    if args.paddle_python is not None:
        values["python_executable"] = args.paddle_python
    if args.paddle_cache is not None:
        values["cache_dir"] = args.paddle_cache
    return PaddleRuntimeConfig(**values)


def _provider(args: argparse.Namespace) -> OllamaCaptionProvider | None:
    if args.no_enrich:
        return None
    config_values: dict[str, object] = {"model": OLLAMA_MODEL}
    if args.ollama_url is not None:
        config_values["base_url"] = args.ollama_url
    return OllamaCaptionProvider(OllamaConfig(**config_values))


def _print_json(value: object, *, indent: int | None = None) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=indent))


@dataclass(slots=True)
class _ProgressTracker:
    """Retain the latest stage and truthful counter without producing output."""

    last_event: ProgressEvent | None = None
    current: int | None = None
    total: int | None = None
    unit: str | None = None

    def __call__(self, event: ProgressEvent) -> None:
        if self.last_event is None or event.stage != self.last_event.stage:
            self.current = None
            self.total = None
            self.unit = None
        if event.current is not None:
            self.current = event.current
        if event.total is not None:
            self.total = event.total
        if event.unit is not None:
            self.unit = event.unit
        self.last_event = event


@dataclass(frozen=True, slots=True)
class _IngestCommandFailure(Exception):
    original: BaseException
    report_path: Path | None = None
    report_error: BaseException | None = None


def _write_requested_failure_report(
    args: argparse.Namespace,
    *,
    run_id: str,
    started_at: datetime,
    error: BaseException,
    tracker: _ProgressTracker,
) -> tuple[Path | None, BaseException | None]:
    if not args.write_failure_report:
        return None, None
    event = tracker.last_event
    try:
        report = build_failure_report(
            run_id=run_id,
            source=args.source,
            destination=args.out,
            started_at=started_at,
            error=error,
            stage=event.stage.value if event is not None else None,
            current=tracker.current,
            total=tracker.total,
            unit=tracker.unit,
        )
        return write_failure_report(args.out, report), None
    except Exception as report_error:
        return None, report_error


def _run_ingest_command(args: argparse.Namespace) -> int:
    run_id = args.run_id or default_run_id(args.source)
    started_at = _utc_now()
    tracker = _ProgressTracker()
    renderer = RichProgressRenderer(enabled=not args.no_progress and sys.stderr.isatty())

    def progress(event: ProgressEvent) -> None:
        tracker(event)
        renderer(event)

    try:
        with operational_output(verbose=args.verbose), renderer:
            outcome = ingest_manual(
                args.source,
                args.out,
                run_id=run_id,
                provider=_provider(args),
                pages=args.pages,
                title=args.title,
                language=args.language,
                paddle_runtime=_paddle_runtime(args),
                page_previews=args.page_previews,
                progress=progress,
            )
    except KeyboardInterrupt as error:
        report_path, report_error = _write_requested_failure_report(
            args,
            run_id=run_id,
            started_at=started_at,
            error=error,
            tracker=tracker,
        )
        print("manual-ingestion: interrupted", file=sys.stderr)
        if report_path is not None:
            print(f"manual-ingestion: failure report: {report_path}", file=sys.stderr)
        if report_error is not None:
            print(
                f"manual-ingestion: warning: failure report could not be written: {report_error}",
                file=sys.stderr,
            )
        return 130
    except Exception as error:
        report_path, report_error = _write_requested_failure_report(
            args,
            run_id=run_id,
            started_at=started_at,
            error=error,
            tracker=tracker,
        )
        raise _IngestCommandFailure(error, report_path, report_error) from error

    result = {
            "output_dir": str(outcome.output_dir),
            "run_id": outcome.manifest.run_id,
            "status": outcome.manifest.status.value,
            "profile": outcome.manifest.pipeline.profile.value,
            "validation": outcome.validation.status,
        }
    if args.json:
        _print_json(result)
    else:
        show_outcome(outcome, verbose=args.verbose)
    return 0


def _hint(message: str, args: argparse.Namespace) -> str | None:
    """Turn the most common failures into one concrete next action."""
    if message.startswith("PDF not found"):
        return "check the path, or type the command, a space, then drag the PDF into the terminal"
    if message.startswith("Source is not a PDF"):
        return "choose a .pdf file"
    if message.startswith("run bundle already exists"):
        source = getattr(args, "source", None) or Path("manual.pdf")
        return f"choose a new folder: --out {shell_path(bundle_name_for(source))}"
    lowered = message.lower()
    if "cannot reach local ollama" in lowered:
        return "start Ollama (open the app or run: ollama serve), or add --no-enrich"
    if "is not installed" in lowered and "ollama" in lowered:
        return f"ollama pull {OLLAMA_MODEL}, or add --no-enrich"
    if "different digest" in lowered:
        return f"re-download the accepted model: ollama pull {OLLAMA_MODEL}"
    if "ollama" in lowered or "provider" in lowered:
        return "add --no-enrich to skip image descriptions, or --verbose for details"
    return None


def _report_error(args: argparse.Namespace, message: str) -> None:
    if getattr(args, "json", False):
        print(f"manual-ingestion: error: {message}", file=sys.stderr)
    else:
        show_error(message, hint=_hint(message, args))


def _run(args: argparse.Namespace) -> int:
    if args.command == "start":
        return run_guided(
            lambda argv: build_parser().parse_args(argv),
            _run_ingest_command,
            model=OLLAMA_MODEL,
        )

    if args.command == "schema":
        model = ManualDocument if args.contract == "manual" else RunManifest
        _print_json(model.model_json_schema(), indent=2)
        return 0

    if args.command == "detect":
        detected = detect_pdf_capabilities(args.source)
        if args.json:
            _print_json(detected.model_dump(mode="json"))
        else:
            show_detection(detected, verbose=args.verbose, source=args.source)
        return 0

    if args.command == "validate":
        if not args.json and not (args.run_dir / "run.json").is_file():
            show_error(
                "This folder is not a bundle" if args.run_dir.is_dir() else "Bundle folder not found",
                f"{args.run_dir} has no run.json" if args.run_dir.is_dir() else str(args.run_dir),
                "use the folder you passed to --out when running ingest",
            )
            return 1
        report = validate_run_bundle(args.run_dir)
        if args.json:
            _print_json(report.model_dump(mode="json"))
        else:
            show_validation(report, args.run_dir, verbose=args.verbose)
        return 0 if report.status == "passed" else 1

    if args.command == "ingest":
        return _run_ingest_command(args)

    raise RuntimeError(f"unsupported command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code without operational tracebacks."""

    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        # A bare invocation opens the home screen instead of a usage error.
        parser.print_help()
        return 0
    if argv[0] == "help":
        argv = [*argv[1:2], "--help"]
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except _IngestCommandFailure as exc:
        if args.verbose:
            traceback.print_exception(exc.original, file=sys.stderr)
        message = str(exc.original).strip() or exc.original.__class__.__name__
        _report_error(args, message)
        if exc.report_path is not None:
            print(
                f"manual-ingestion: failure report: {exc.report_path}",
                file=sys.stderr,
            )
        if exc.report_error is not None:
            print(
                "manual-ingestion: warning: failure report could not be written: "
                f"{exc.report_error}",
                file=sys.stderr,
            )
        return 1
    except (OSError, RuntimeError, ValueError) as exc:
        if getattr(args, "verbose", False):
            traceback.print_exc(file=sys.stderr)
        message = str(exc).strip() or exc.__class__.__name__
        _report_error(args, message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
