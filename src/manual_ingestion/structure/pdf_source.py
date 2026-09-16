"""PyMuPDF adapter that extracts generic structural signals from a digital PDF."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

import pymupdf

from .models import (
    DocumentSignals,
    PageLabelSegment,
    PageLine,
    PageSignals,
    TocReconstructionError,
)


def load_pdf_signals(pdf_path: str | Path) -> DocumentSignals:
    """Load all text-layout signals once so reconstruction remains deterministic."""

    path = Path(pdf_path).expanduser()
    if not path.exists():
        raise TocReconstructionError(f"PDF not found: {path}")
    if not path.is_file():
        raise TocReconstructionError(f"PDF path is not a file: {path}")

    try:
        document = pymupdf.open(path)
    except (OSError, RuntimeError, ValueError, pymupdf.FileDataError) as exc:
        raise TocReconstructionError(f"Cannot open PDF '{path}': {exc}") from exc

    try:
        if not document.is_pdf:
            raise TocReconstructionError(f"Source is not a PDF document: {path}")
        if document.needs_pass:
            raise TocReconstructionError(
                f"PDF is password-protected and cannot be reconstructed without credentials: {path}"
            )
        if document.page_count < 1:
            raise TocReconstructionError(f"PDF contains no pages: {path}")

        try:
            outline_entries = len(document.get_toc(simple=True))
        except (RuntimeError, ValueError) as exc:
            raise TocReconstructionError(f"Cannot inspect embedded outline in '{path}': {exc}") from exc

        page_labels = _extract_page_label_segments(document)
        pages = tuple(_extract_page(document, page_index) for page_index in range(document.page_count))
        return DocumentSignals(
            page_count=document.page_count,
            outline_entries=outline_entries,
            pages=pages,
            page_labels=page_labels,
        )
    finally:
        document.close()


def _extract_page(document: pymupdf.Document, page_index: int) -> PageSignals:
    page = document.load_page(page_index)
    try:
        # Preserve the PDF content-stream order. Printed indexes often use multiple
        # columns; geometric sorting can interleave their numbering and title lines.
        raw = page.get_text("dict")
    except (RuntimeError, ValueError) as exc:
        raise TocReconstructionError(f"Cannot extract text layout from PDF page {page_index + 1}: {exc}") from exc

    lines: list[PageLine] = []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for raw_line in block.get("lines", []):
            spans = [span for span in raw_line.get("spans", []) if str(span.get("text", "")).strip()]
            text = _normalize_text(" ".join(str(span.get("text", "")) for span in spans))
            if not text:
                continue
            sizes = [float(span["size"]) for span in spans if span.get("size")]
            fonts = [str(span["font"]) for span in spans if span.get("font")]
            bbox = _bbox_tuple(raw_line.get("bbox"))
            lines.append(
                PageLine(
                    text=text,
                    bbox=bbox,
                    font_size=max(sizes) if sizes else None,
                    font_name=fonts[0] if fonts else None,
                )
            )

    return PageSignals(
        number=page_index + 1,
        width=float(page.rect.width),
        height=float(page.rect.height),
        lines=tuple(lines),
    )


def _extract_page_label_segments(document: pymupdf.Document) -> tuple[PageLabelSegment, ...]:
    try:
        raw_labels = document.get_page_labels()
    except (RuntimeError, ValueError):
        return ()
    if not raw_labels:
        return ()

    labels = sorted(raw_labels, key=lambda value: int(value.get("startpage", 0)))
    segments: list[PageLabelSegment] = []
    for index, label in enumerate(labels):
        start_index = int(label.get("startpage", 0))
        next_start = (
            int(labels[index + 1].get("startpage", document.page_count))
            if index + 1 < len(labels)
            else document.page_count
        )
        if start_index < 0 or start_index >= document.page_count or next_start <= start_index:
            continue
        segments.append(
            PageLabelSegment(
                start_page=start_index + 1,
                end_page=min(next_start, document.page_count),
                prefix=str(label.get("prefix", "") or ""),
                first_number=max(1, int(label.get("firstpagenum", 1) or 1)),
                style=str(label.get("style", "D") or "D"),
            )
        )
    return tuple(segments)


def _bbox_tuple(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    return tuple(float(coordinate) for coordinate in value)  # type: ignore[return-value]


def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", normalized).strip()
