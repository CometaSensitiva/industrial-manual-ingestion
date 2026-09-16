from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import manual_ingestion.failure_report as failure_report_module
from manual_ingestion.diagnostics import FailureReport
from manual_ingestion.failure_report import build_failure_report, write_failure_report


STARTED = datetime(2026, 7, 12, 19, 30, 0, tzinfo=UTC)
FAILED = STARTED + timedelta(minutes=2)


def _report(**overrides: object) -> FailureReport:
    values: dict[str, object] = {
        "run_id": "manual-run",
        "source_file": "manuale.pdf",
        "destination": "outputs/manuale",
        "started_at": STARTED,
        "failed_at": FAILED,
        "stage": "enrichment",
        "error_type": "RuntimeError",
        "error_message": "provider unavailable",
        "current": 3,
        "total": 12,
        "unit": "elements",
    }
    values.update(overrides)
    return FailureReport(**values)


def test_build_failure_report_keeps_only_bounded_operator_context() -> None:
    report = build_failure_report(
        run_id="manual-run",
        source="/private/input/Manuale operatore.pdf",
        destination="outputs/manuale",
        started_at=STARTED,
        failed_at=FAILED,
        error=RuntimeError("x" * 5000),
        stage="extraction",
        current=6,
        total=34,
        unit="pages",
    )

    payload = report.model_dump(mode="json")
    assert payload == {
        "schema_version": "1.1",
        "kind": "ingestion_failure",
        "run_id": "manual-run",
        "source_file": "Manuale operatore.pdf",
        "destination": "outputs/manuale",
        "started_at": "2026-07-12T19:30:00Z",
        "failed_at": "2026-07-12T19:32:00Z",
        "stage": "extraction",
        "error_type": "RuntimeError",
        "error_message": "x" * 4000,
        "current": 6,
        "total": 34,
        "unit": "pages",
        "bundle_published": False,
    }
    assert "traceback" not in payload
    assert "assets" not in payload
    assert "environment" not in payload
    assert "subprocess" not in payload


def test_build_failure_report_accepts_windows_source_and_naive_started_time() -> None:
    report = build_failure_report(
        run_id="manual-run",
        source=r"C:\manuals\manuale.pdf",
        destination="outputs/manuale",
        started_at=datetime(2026, 7, 12, 19, 30),
        failed_at=FAILED,
        error=KeyboardInterrupt(),
    )

    assert report.source_file == "manuale.pdf"
    assert report.started_at.tzinfo is UTC
    assert report.error_type == "KeyboardInterrupt"
    assert report.error_message == ""


def test_write_failure_report_is_atomic_sibling_and_not_a_bundle_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "runs" / "manuale"
    monkeypatch.setattr(failure_report_module.secrets, "token_hex", lambda _: "a1b2c3d4")

    path = write_failure_report(output_dir, _report(), now=FAILED)

    assert path.parent == output_dir.parent
    assert path.name == "manuale.failure-20260712T193200000000Z-a1b2c3d4.json"
    assert not output_dir.exists()
    assert FailureReport.model_validate_json(path.read_text(encoding="utf-8")) == _report()
    assert not list(path.parent.glob(".*.tmp-*"))


def test_writer_retries_a_name_collision_without_overwriting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "manuale"
    existing = tmp_path / "manuale.failure-20260712T193200000000Z-deadbeef.json"
    existing.write_text("do not replace", encoding="utf-8")
    suffixes = iter(("deadbeef", "cafebabe"))
    monkeypatch.setattr(failure_report_module.secrets, "token_hex", lambda _: next(suffixes))

    created = write_failure_report(output_dir, _report(), now=FAILED)

    assert created.name.endswith("-cafebabe.json")
    assert existing.read_text(encoding="utf-8") == "do not replace"


def test_writer_cleans_temporary_file_when_publication_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "manuale"

    def fail_link(source: Path, destination: Path) -> None:
        raise PermissionError("link denied")

    monkeypatch.setattr(os, "link", fail_link)

    with pytest.raises(PermissionError, match="link denied"):
        write_failure_report(output_dir, _report(), now=FAILED)

    assert list(tmp_path.iterdir()) == []


def test_writer_name_has_utc_microseconds_and_eight_hex_characters(tmp_path: Path) -> None:
    path = write_failure_report(tmp_path / "manuale", _report())

    assert re.fullmatch(
        r"manuale\.failure-\d{8}T\d{12}Z-[0-9a-f]{8}\.json",
        path.name,
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"bundle_published": True}, "Input should be False"),
        ({"source_file": "private/manual.pdf"}, "basename"),
        ({"error_message": "x" * 4001}, "at most 4000"),
        ({"current": 4, "total": 3}, "cannot exceed total"),
        ({"started_at": FAILED, "failed_at": STARTED}, "cannot precede"),
        ({"stage": " "}, "must be non-empty"),
    ],
)
def test_failure_report_rejects_incoherent_values(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _report(**overrides)
