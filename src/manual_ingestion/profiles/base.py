"""Shared profile boundaries and profile-level results."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from manual_ingestion.models import (
    DocumentProfile,
    ManualDocument,
    ManualElement,
    TocEntry,
)


class ProfileExecutionError(RuntimeError):
    """Raised when a selected profile cannot produce a trustworthy result."""


@dataclass(slots=True)
class ContentParseResult:
    """Canonical, parser-independent page content."""

    elements: list[ManualElement]
    pages_processed: list[int]
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class OutlineParseResult:
    """Embedded PDF outline plus document facts needed by a profile."""

    entries: list[TocEntry]
    pages_total: int
    document_title: str | None = None


@dataclass(slots=True)
class ProfileResult:
    """Artifacts produced before run-bundle serialization."""

    manual: ManualDocument
    toc: list[TocEntry]
    warnings: list[str] = field(default_factory=list)


class IngestionProfile(Protocol):
    """Common execution boundary for every detected manual profile."""

    profile: DocumentProfile

    def run(
        self,
        pdf_path: str | Path,
        *,
        run_id: str,
        assets_dir: str | Path,
        pages: list[int] | None = None,
        title: str | None = None,
        language: str = "en",
    ) -> ProfileResult:
        """Convert the selected PDF pages into canonical V2 artifacts."""


class ContentAdapter(Protocol):
    """Boundary implemented by Docling, PaddleOCR-VL, or a test fake."""

    name: str

    def parse(
        self,
        pdf_path: Path,
        pages: list[int],
        assets_dir: Path,
    ) -> ContentParseResult:
        """Parse selected physical PDF pages into canonical elements."""


class OutlineAdapter(Protocol):
    """Boundary for extracting an authoritative embedded outline."""

    name: str

    def extract(self, pdf_path: Path) -> OutlineParseResult:
        """Read the embedded outline and stable document metadata."""
