"""PyMuPDF adapter for authoritative embedded PDF outlines."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from manual_ingestion.models import TocEntry
from manual_ingestion.profiles.base import OutlineParseResult


class OutlineExtractionError(RuntimeError):
    """Raised when a PDF cannot be inspected safely."""


class PyMuPDFOutlineAdapter:
    """Extract the native PDF outline without reconstructing or guessing it."""

    name = "pymupdf"

    def extract(self, pdf_path: Path) -> OutlineParseResult:
        path = Path(pdf_path)
        if not path.is_file():
            raise FileNotFoundError(f"PDF not found: {path}")

        try:
            with pymupdf.open(path) as document:
                if not document.is_pdf:
                    raise OutlineExtractionError(f"Source is not a PDF document: {path}")
                if document.needs_pass:
                    raise OutlineExtractionError(f"PDF is password protected: {path}")
                pages_total = document.page_count
                if pages_total < 1:
                    raise OutlineExtractionError(f"PDF has no pages: {path}")
                raw_outline = document.get_toc(simple=True)
                raw_title = document.metadata.get("title") if document.metadata else None
        except OutlineExtractionError:
            raise
        except Exception as exc:
            raise OutlineExtractionError(f"Cannot read PDF outline from {path}: {exc}") from exc

        entries: list[TocEntry] = []
        for index, raw_entry in enumerate(raw_outline, start=1):
            if len(raw_entry) < 3:
                raise OutlineExtractionError(f"Outline entry {index} is malformed")
            level, raw_entry_title, page = raw_entry[:3]
            title = str(raw_entry_title).strip()
            try:
                level_number = int(level)
                page_number = int(page)
            except (TypeError, ValueError) as exc:
                raise OutlineExtractionError(
                    f"Outline entry {index} has non-numeric level or page"
                ) from exc
            if not title:
                raise OutlineExtractionError(f"Outline entry {index} has an empty title")
            if level_number < 1:
                raise OutlineExtractionError(
                    f"Outline entry {index} has invalid level {level_number}"
                )
            if page_number < 1 or page_number > pages_total:
                raise OutlineExtractionError(
                    f"Outline entry {index} points outside the document (page {page_number})"
                )
            entries.append(
                TocEntry(
                    level=level_number,
                    title=title,
                    page=page_number,
                    confidence=1.0,
                )
            )

        document_title = str(raw_title).strip() if raw_title else None
        return OutlineParseResult(
            entries=entries,
            pages_total=pages_total,
            document_title=document_title or None,
        )
