"""Transient progress events shared by ingestion callers and renderers.

Progress events deliberately live outside the canonical bundle contracts: they
describe an in-flight operation and must never affect manifests, fingerprints,
or published artifacts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias


class ProgressStage(StrEnum):
    """Stable stages emitted by the ingestion pipeline."""

    PREPARATION = "preparation"
    DETECTION = "detection"
    EXTRACTION = "extraction"
    CROP_FILTER = "crop_filter"
    ENRICHMENT = "enrichment"
    TABLE_SERIALIZATION = "table_serialization"
    VALIDATION = "validation"
    PUBLICATION = "publication"


class ProgressState(StrEnum):
    """Lifecycle states for one progress stage."""

    STARTED = "started"
    UPDATED = "updated"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One immutable, non-persistent progress notification."""

    stage: ProgressStage
    state: ProgressState
    current: int | None = None
    total: int | None = None
    unit: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", ProgressStage(self.stage))
        object.__setattr__(self, "state", ProgressState(self.state))

        for field_name in ("current", "total"):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int)
            ):
                raise TypeError(f"{field_name} must be an integer or None")
            if value is not None and value < 0:
                raise ValueError(f"{field_name} cannot be negative")

        if (
            self.current is not None
            and self.total is not None
            and self.current > self.total
        ):
            raise ValueError("current cannot exceed total")

        for field_name in ("unit", "detail"):
            value = getattr(self, field_name)
            if value is not None and (not value or value != value.strip()):
                raise ValueError(f"{field_name} must be a canonical non-empty string")


ProgressCallback: TypeAlias = Callable[[ProgressEvent], None]


def emit_progress(callback: ProgressCallback | None, event: ProgressEvent) -> None:
    """Best-effort delivery that cannot change the ingestion outcome.

    Progress is an observational, transient channel.  A broken renderer or
    integration callback must therefore not abort an otherwise valid run or
    turn an already published bundle into an apparent failure.
    """

    if callback is not None:
        try:
            callback(event)
        except Exception:
            pass


__all__ = [
    "ProgressCallback",
    "ProgressEvent",
    "ProgressStage",
    "ProgressState",
    "emit_progress",
]
