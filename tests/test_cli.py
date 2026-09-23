from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from manual_ingestion import cli
from manual_ingestion.models import (
    DetectedCapabilities,
    DocumentProfile,
    RunStatus,
    ValidationCheck,
    ValidationReport,
)
from manual_ingestion.providers.ollama import OllamaCaptionProvider
from manual_ingestion.progress import ProgressEvent, ProgressStage, ProgressState


def _detected() -> DetectedCapabilities:
    return DetectedCapabilities(
        profile=DocumentProfile.DIGITAL_RECONSTRUCTED,
        embedded_outline=False,
        outline_entries=0,
        text_layer=True,
        sampled_pages=[1, 3],
        pages_with_text=2,
        median_text_characters=420.0,
        profile_confidence=0.98,
        reasons=["usable text layer"],
    )


def _validation(*, passed: bool) -> ValidationReport:
    return ValidationReport(
        run_id="manual-run",
        status="passed" if passed else "failed",
        checks=[
            ValidationCheck(
                id="bundle.contract",
                passed=passed,
                message="valid" if passed else "manual.json is missing",
            )
        ],
        metrics={"element_count": 12},
    )


def _outcome(output_dir: Path, *, run_id: str = "manual-run") -> SimpleNamespace:
    return SimpleNamespace(
        output_dir=output_dir,
        manifest=SimpleNamespace(
            run_id=run_id,
            status=RunStatus.VALIDATED,
            pipeline=SimpleNamespace(profile=DocumentProfile.DIGITAL_RECONSTRUCTED),
        ),
        validation=SimpleNamespace(status="passed"),
    )


def _ingest_option_names() -> set[str]:
    parser = cli.build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return {
        option
        for action in subparsers.choices["ingest"]._actions
        for option in action.option_strings
    }


def test_ingest_cli_exposes_no_technology_choice_flags() -> None:
    options = _ingest_option_names()

    assert options == {
        "-h",
        "--help",
        "--out",
        "--run-id",
        "--pages",
        "--title",
        "--language",
        "--ollama-url",
        "--paddle-python",
        "--paddle-cache",
        "--page-previews",
        "--no-enrich",
        "--no-progress",
        "--write-failure-report",
        "--json",
        "--verbose",
    }
    assert not {"--profile", "--engine", "--strategy", "--model"} & options


@pytest.mark.parametrize(
    "value",
    [
        "",
        "0",
        "01",
        "1,0",
        "3-1",
        "1,1",
        "1-3,3",
        "1-3,2-4",
        "1,,2",
        "1,",
        ",1",
        "1 2",
        "1 - 2",
        "a",
        "1-2-3",
        " 1-3",
    ],
)
def test_page_parser_rejects_invalid_or_ambiguous_selections(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_pages(value)


def test_page_parser_expands_ordered_ranges() -> None:
    assert cli.parse_pages("1-3,5,8-10") == [1, 2, 3, 5, 8, 9, 10]


def test_default_run_id_uses_safe_stem_and_utc_timestamp() -> None:
    local_time = datetime(2026, 7, 11, 12, 2, 3, tzinfo=timezone_plus_two())

    assert cli.default_run_id("Manuale: Impianto v2.pdf", now=local_time) == (
        "manuale-impianto-v2-20260711T100203Z"
    )
    assert cli.default_run_id("!!.pdf", now=local_time) == (
        "manual-20260711T100203Z"
    )


def timezone_plus_two():
    return timezone(timedelta(hours=2))


def test_detect_prints_the_canonical_detection_json(monkeypatch, capsys) -> None:
    calls: list[Path] = []

    def fake_detect(source: Path) -> DetectedCapabilities:
        calls.append(source)
        return _detected()

    monkeypatch.setattr(cli, "detect_pdf_capabilities", fake_detect)

    assert cli.main(["detect", "--json", "manual.pdf"]) == 0

    assert calls == [Path("manual.pdf")]
    assert json.loads(capsys.readouterr().out) == _detected().model_dump(mode="json")


def test_ingest_forwards_user_inputs_and_builds_the_fixed_runtime_contract(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    output = tmp_path / "final-bundle"
    captured: dict[str, object] = {}

    def fake_ingest(source_pdf, target_dir, **kwargs):
        captured.update(
            {
                "source_pdf": source_pdf,
                "target_dir": target_dir,
                **kwargs,
            }
        )
        return _outcome(output, run_id="chosen-run")

    monkeypatch.setattr(cli, "ingest_manual", fake_ingest)

    exit_code = cli.main(
        [
            "ingest",
            "--json",
            str(source),
            "--out",
            str(output),
            "--run-id",
            "chosen-run",
            "--pages",
            "1-3,5",
            "--title",
            "Manuale scelto",
            "--language",
            "en",
            "--ollama-url",
            "http://localhost:11434/api/",
            "--paddle-python",
            "/opt/paddle/bin/python",
            "--paddle-cache",
            "/var/cache/paddlex",
            "--page-previews",
        ]
    )

    assert exit_code == 0
    assert captured["source_pdf"] == source
    assert captured["target_dir"] == output
    assert captured["run_id"] == "chosen-run"
    assert captured["pages"] == [1, 2, 3, 5]
    assert captured["title"] == "Manuale scelto"
    assert captured["language"] == "en"
    assert captured["page_previews"] is True
    assert callable(captured["progress"])

    provider = captured["provider"]
    assert isinstance(provider, OllamaCaptionProvider)
    assert provider.config.model == "qwen3.5:4b"
    assert provider.base_url == "http://localhost:11434"

    paddle_runtime = captured["paddle_runtime"]
    assert paddle_runtime.python_executable == "/opt/paddle/bin/python"
    assert paddle_runtime.cache_dir == Path("/var/cache/paddlex")

    assert json.loads(capsys.readouterr().out) == {
        "output_dir": str(output),
        "run_id": "chosen-run",
        "status": "validated",
        "profile": "digital_reconstructed",
        "validation": "passed",
    }


def test_ingest_uses_fixed_default_provider_and_no_paddle_override(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    output = tmp_path / "bundle"

    def fake_ingest(source_pdf, target_dir, **kwargs):
        captured.update(kwargs)
        return _outcome(output, run_id="generated-run")

    monkeypatch.setattr(cli, "ingest_manual", fake_ingest)
    monkeypatch.setattr(cli, "default_run_id", lambda source: "generated-run")

    assert cli.main(["ingest", "--json", "manual.pdf", "--out", str(output)]) == 0

    provider = captured["provider"]
    assert isinstance(provider, OllamaCaptionProvider)
    assert provider.config.model == "qwen3.5:4b"
    assert provider.base_url == "http://127.0.0.1:11434"
    assert captured["paddle_runtime"] is None
    assert captured["run_id"] == "generated-run"
    assert captured["pages"] is None
    assert captured["title"] is None
    assert captured["language"] == "en"
    assert captured["page_previews"] is False
    assert callable(captured["progress"])
    assert json.loads(capsys.readouterr().out)["run_id"] == "generated-run"


def test_no_enrich_is_diagnostic_and_passes_no_provider(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    output = tmp_path / "bundle"

    def fake_ingest(source_pdf, target_dir, **kwargs):
        captured.update(kwargs)
        return _outcome(output)

    monkeypatch.setattr(cli, "ingest_manual", fake_ingest)

    assert (
        cli.main(
            [
                "ingest",
            "--json",
                "--json",
                "manual.pdf",
                "--out",
                str(output),
                "--run-id",
                "manual-run",
                "--no-enrich",
            ]
        )
        == 0
    )

    assert captured["provider"] is None
    ingest_help = cli.build_parser()._subparsers._group_actions[0].choices[
        "ingest"
    ].format_help()
    assert "Diagnostic only" in ingest_help
    assert "explicit page subset" in ingest_help
    assert ingest_help.count("remains experimental") == 2
    capsys.readouterr()


@pytest.mark.parametrize(("passed", "expected_exit"), [(True, 0), (False, 1)])
def test_validate_prints_report_and_uses_status_exit_code(
    monkeypatch,
    capsys,
    passed: bool,
    expected_exit: int,
) -> None:
    report = _validation(passed=passed)
    calls: list[Path] = []

    def fake_validate(run_dir: Path) -> ValidationReport:
        calls.append(run_dir)
        return report

    monkeypatch.setattr(cli, "validate_run_bundle", fake_validate)

    assert cli.main(["validate", "--json", "bundle"]) == expected_exit
    assert calls == [Path("bundle")]
    assert json.loads(capsys.readouterr().out) == report.model_dump(mode="json")


@pytest.mark.parametrize("contract", ["manual", "run"])
def test_schema_command_prints_requested_contract(contract: str, capsys) -> None:
    assert cli.main(["schema", contract]) == 0

    schema = json.loads(capsys.readouterr().out)
    assert schema["title"] == (
        "ManualDocument" if contract == "manual" else "RunManifest"
    )


def test_expected_runtime_error_is_actionable_without_traceback(
    monkeypatch,
    capsys,
) -> None:
    def fail(_source: Path):
        raise ValueError("PDF not found: missing.pdf")

    monkeypatch.setattr(cli, "detect_pdf_capabilities", fail)

    assert cli.main(["detect", "--json", "missing.pdf"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "manual-ingestion: error: PDF not found: missing.pdf\n"
    assert "Traceback" not in captured.err


def test_ingest_failure_report_is_opt_in_and_records_last_progress(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    output = tmp_path / "bundle"

    def fail_ingest(source_pdf, target_dir, **kwargs):
        kwargs["progress"](
            ProgressEvent(
                ProgressStage.ENRICHMENT,
                ProgressState.UPDATED,
                current=3,
                total=7,
                unit="elements",
            )
        )
        kwargs["progress"](
            ProgressEvent(
                ProgressStage.ENRICHMENT,
                ProgressState.FAILED,
                detail="provider unavailable",
            )
        )
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(cli, "ingest_manual", fail_ingest)

    assert cli.main(
        [
            "ingest",
            "--json",
            "manual.pdf",
            "--out",
            str(output),
            "--run-id",
            "run-1",
            "--no-progress",
            "--write-failure-report",
        ]
    ) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "manual-ingestion: error: provider unavailable" in captured.err
    reports = list(tmp_path.glob("bundle.failure-*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["stage"] == "enrichment"
    assert report["current"] == 3
    assert report["total"] == 7
    assert report["bundle_published"] is False


def test_ingest_failure_without_flag_writes_no_report(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        cli,
        "ingest_manual",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("failed")),
    )

    assert cli.main(
        ["ingest", "manual.pdf", "--out", str(tmp_path / "bundle"), "--no-progress"]
    ) == 1

    assert not list(tmp_path.glob("*.failure-*.json"))
    assert capsys.readouterr().out == ""


def test_unexpected_ingest_exception_still_writes_the_opt_in_report(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    output = tmp_path / "bundle"
    monkeypatch.setattr(
        cli,
        "ingest_manual",
        lambda *args, **kwargs: (_ for _ in ()).throw(TypeError("bad adapter result")),
    )

    assert cli.main(
        [
            "ingest",
            "--json",
            "manual.pdf",
            "--out",
            str(output),
            "--no-progress",
            "--write-failure-report",
        ]
    ) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "manual-ingestion: error: bad adapter result" in captured.err
    reports = list(tmp_path.glob("bundle.failure-*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["error_type"] == "TypeError"
    assert report["bundle_published"] is False


def test_ingest_keyboard_interrupt_returns_130_and_can_write_report(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    def interrupt(*args, **kwargs):
        kwargs["progress"](
            ProgressEvent(ProgressStage.EXTRACTION, ProgressState.STARTED)
        )
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "ingest_manual", interrupt)
    output = tmp_path / "bundle"

    assert cli.main(
        [
            "ingest",
            "--json",
            "manual.pdf",
            "--out",
            str(output),
            "--no-progress",
            "--write-failure-report",
        ]
    ) == 130

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "manual-ingestion: interrupted" in captured.err
    assert len(list(tmp_path.glob("bundle.failure-*.json"))) == 1


def test_argparse_rejects_hidden_technology_flag_without_traceback(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(
            [
                "ingest",
            "--json",
                "--json",
                "manual.pdf",
                "--out",
                "bundle",
                "--profile",
                "scanned_ocr",
            ]
        )

    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "unrecognized arguments: --profile scanned_ocr" in captured.err
    assert "Traceback" not in captured.err


def test_detect_default_is_readable_and_does_not_present_a_probability(monkeypatch, capsys):
    monkeypatch.setattr(cli, "detect_pdf_capabilities", lambda _: _detected())
    assert cli.main(["detect", "manual.pdf"]) == 0
    output = capsys.readouterr().out
    assert "Document profile" in output
    assert "digital reconstructed" in output
    assert "98%" not in output
    assert "profile_confidence" not in output


def test_root_help_uses_complete_copyable_examples(capsys):
    cli.build_parser().print_help()
    output = capsys.readouterr().out
    flattened = " ".join(output.split())
    assert "manual-ingestion detect examples/synthetic-manual.pdf" in flattened
    assert "--out my-first-bundle --no-enrich --page-previews" in flattened
    assert "manual-ingestion validate my-first-bundle" in flattened
    assert "manual-ingestion start" in output
    assert "manual-ingestion help ingest" in output
    assert "<command>" not in output
    assert "manual-ingestion detect manual.pdf" not in output


def test_ingest_help_is_task_oriented_and_explains_multiline_commands(capsys):
    parser = cli.build_parser()
    ingest = parser._subparsers._group_actions[0].choices["ingest"]

    ingest.print_help()

    output = capsys.readouterr().out
    flattened = " ".join(output.split())
    assert "manual-ingestion ingest PDF --out NEW_BUNDLE" in flattened
    assert "--page-previews" in output
    assert "The output folder must not exist yet" in flattened
    assert "must be the final character" in flattened


def test_missing_output_shows_a_copyable_ingest_command(capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["ingest", "manual.pdf"])

    assert error.value.code == 2
    output = capsys.readouterr().err
    assert "Command not understood" in output
    assert "manual-ingestion ingest PDF --out NEW_BUNDLE" in output


def test_validation_default_shows_failed_checks(monkeypatch, capsys, tmp_path):
    (tmp_path / "run.json").write_text("{}")
    monkeypatch.setattr(cli, "validate_run_bundle", lambda _: _validation(passed=False))
    assert cli.main(["validate", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "manual.json is missing" in output
    assert "Software checks do not certify" in output


def test_human_ingest_summary_reads_published_content(monkeypatch, capsys, tmp_path):
    outcome = _outcome(tmp_path)
    outcome.manifest.artifacts = SimpleNamespace(
        manual="manual.json",
        assets="assets",
    )
    outcome.manifest.warnings = []
    (tmp_path / "manual.json").write_text(json.dumps({"metadata": {"pages_processed": [1]}, "content": [{"type":"image", "caption_generated":"Description"}]}))
    monkeypatch.setattr(cli, "ingest_manual", lambda *args, **kwargs: outcome)
    assert cli.main(["ingest", "source.pdf", "--out", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "Bundle ready" in output
    assert "Page previews" in output
    assert "0/1" in output
    assert "1/1" in output
    assert "Included in bundle" in output


def test_library_stdout_cannot_corrupt_json(monkeypatch, capsys, tmp_path):
    def fake(*args, **kwargs):
        print("library diagnostic")
        return _outcome(tmp_path)
    monkeypatch.setattr(cli, "ingest_manual", fake)
    assert cli.main(["ingest", "source.pdf", "--out", str(tmp_path), "--json"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == "validated"
    assert "library diagnostic" in output.err


def test_cli_and_viewer_share_one_portrait():
    from manual_ingestion import cli_display

    brand = (Path(__file__).parents[1] / "viewer/src/brand.ts").read_text()
    assert f"BRAND_ASCII = {json.dumps(cli_display.LOGO)};" in brand
    assert f"BRAND_TONES = {json.dumps(cli_display.LOGO_TONES)};" in brand
    rows = cli_display.LOGO.split("\n")
    tones = cli_display.LOGO_TONES.split("\n")
    assert [len(row) for row in rows] == [len(row) for row in tones]


def test_typewriter_reveals_the_exact_help_without_markers(monkeypatch):
    import io

    from manual_ingestion import cli_effects

    monkeypatch.setattr(cli_effects.time, "sleep", lambda _: None)
    marker = cli_effects.SLOW
    rendered = f"\x1b[1m{marker}INDUSTRIAL{marker}\x1b[0m manual\n  ├─ ok\n"
    stream = io.StringIO()
    cli_effects.typewrite(rendered, stream, seed=1)
    assert stream.getvalue() == rendered.replace(marker, "") + "\x1b[0m"


def test_help_is_never_animated_outside_an_interactive_terminal(monkeypatch):
    import io

    from manual_ingestion import cli_effects

    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv(cli_effects.ANIMATION_OFF, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert cli_effects.animate(Tty())
    assert not cli_effects.animate(io.StringIO())
    monkeypatch.setenv(cli_effects.ANIMATION_OFF, "1")
    assert not cli_effects.animate(Tty())


def test_static_help_never_contains_typing_markers(capsys):
    from manual_ingestion import cli_effects

    cli.build_parser().print_help()
    assert cli_effects.SLOW not in capsys.readouterr().out


def test_missing_bundle_folder_is_explained_not_checked(capsys, tmp_path):
    assert cli.main(["validate", str(tmp_path / "nope")]) == 1
    captured = capsys.readouterr()
    assert "Bundle folder not found" in captured.err
    assert "run.json" not in captured.out


def test_bare_invocation_opens_the_home_screen(capsys):
    assert cli.main([]) == 0
    output = capsys.readouterr().out
    assert "INDUSTRIAL MANUAL INGESTION" in output
    for command in ("detect", "ingest", "validate", "schema", "help"):
        assert command in output


def test_help_command_routes_to_guided_help(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["help", "validate"])
    assert exit_info.value.code == 0
    assert "Check an existing bundle" in capsys.readouterr().out


def test_mistyped_command_suggests_the_closest_one(capsys):
    with pytest.raises(SystemExit):
        cli.main(["ingets", "manual.pdf"])
    error = capsys.readouterr().err
    assert "'ingets' is not a manual-ingestion command." in error
    assert "Try  manual-ingestion ingest" in error


@pytest.mark.parametrize(
    ("message", "hint"),
    [
        ("caption provider preflight failed: Cannot reach local Ollama at http://127.0.0.1:11434: refused", "ollama serve"),
        ("caption provider preflight failed: The exact configured Ollama model tag 'qwen3.5:4b' is not installed", "ollama pull qwen3.5:4b"),
    ],
)
def test_ollama_failures_get_a_specific_hint(message, hint):
    assert hint in cli._hint(message, argparse.Namespace())


def test_next_hints_can_be_hidden(monkeypatch, capsys):
    monkeypatch.setattr(cli, "detect_pdf_capabilities", lambda _: _detected())
    assert cli.main(["detect", "manual.pdf"]) == 0
    assert "Next" in capsys.readouterr().out
    monkeypatch.setenv("MANUAL_INGESTION_NO_HINTS", "1")
    assert cli.main(["detect", "manual.pdf"]) == 0
    output = capsys.readouterr().out
    assert "Next" not in output
    assert "Document profile" in output
