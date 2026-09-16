"""Build and atomically publish compact ingestion failure reports."""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from .diagnostics import FailureReport

MAX_FAILURE_MESSAGE_CHARACTERS = 4000
_MAX_NAME_ATTEMPTS = 100


def build_failure_report(
    *,
    run_id: str,
    source: str | Path,
    destination: str | Path,
    started_at: datetime,
    error: BaseException,
    failed_at: datetime | None = None,
    stage: str | None = None,
    current: int | None = None,
    total: int | None = None,
    unit: str | None = None,
) -> FailureReport:
    """Return the privacy-bounded report contract for one failed invocation."""

    return FailureReport(
        run_id=run_id,
        source_file=_source_basename(source),
        destination=str(destination),
        started_at=_as_utc(started_at),
        failed_at=_as_utc(failed_at or _utc_now()),
        stage=stage,
        error_type=type(error).__name__,
        error_message=str(error)[:MAX_FAILURE_MESSAGE_CHARACTERS],
        current=current,
        total=total,
        unit=unit,
    )


def write_failure_report(
    output_dir: str | Path,
    report: FailureReport,
    *,
    now: datetime | None = None,
) -> Path:
    """Atomically write ``report`` beside ``output_dir`` without overwriting.

    The completed temporary file is published with an exclusive hard link.
    Linking on the same filesystem is atomic and, unlike a regular POSIX
    rename, fails rather than replacing an existing destination.
    """

    output_path = Path(output_dir).expanduser()
    if not output_path.name:
        raise ValueError("output_dir must have a final path component")

    parent = output_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    instant = _as_utc(now or report.failed_at)
    timestamp = instant.strftime("%Y%m%dT%H%M%S%fZ")
    serialized = json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
    ) + "\n"

    for _ in range(_MAX_NAME_ATTEMPTS):
        suffix = secrets.token_hex(4)
        target = parent / f"{output_path.name}.failure-{timestamp}-{suffix}.json"
        temporary = parent / f".{target.name}.tmp-{uuid4().hex}"
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                continue
            return target
        finally:
            temporary.unlink(missing_ok=True)

    raise FileExistsError(
        f"could not allocate a unique failure-report name beside {output_path}"
    )


def _source_basename(source: str | Path) -> str:
    value = str(source)
    posix_name = PurePosixPath(value).name
    windows_name = PureWindowsPath(value).name
    if "\\" in value:
        return windows_name
    return posix_name


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)
