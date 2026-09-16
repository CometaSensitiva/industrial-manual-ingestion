"""Deterministic PDF capability detection for V2 ingestion profiles."""

from __future__ import annotations

import math
import statistics
from pathlib import Path

import pymupdf

from .models import DetectedCapabilities, DocumentProfile

MAX_SAMPLED_PAGES = 7
MIN_USABLE_TEXT_CHARACTERS = 40
MIN_TEXT_PAGE_COVERAGE = 0.75


class PDFDetectionError(ValueError):
    """Raised when a source cannot be inspected as a usable PDF."""


def detect_pdf_capabilities(pdf_path: str | Path) -> DetectedCapabilities:
    """Inspect a PDF and select its canonical ingestion profile.

    Text evidence is collected from a deterministic, evenly distributed page
    sample. A text layer is considered usable only when enough sampled pages
    contain meaningful text. Digital profiles require robust coverage across
    the sample; isolated text-bearing pages route to ``scanned_ocr`` so a
    no-OCR digital parser cannot silently leave most pages unparsed.
    """

    path = Path(pdf_path).expanduser()
    if not path.exists():
        raise PDFDetectionError(f"PDF not found: {path}")
    if not path.is_file():
        raise PDFDetectionError(f"PDF path is not a file: {path}")

    try:
        document = pymupdf.open(path)
    except (OSError, RuntimeError, ValueError, pymupdf.FileDataError) as exc:
        raise PDFDetectionError(f"Cannot open PDF '{path}': {exc}") from exc

    try:
        if not document.is_pdf:
            raise PDFDetectionError(f"Source is not a PDF document: {path}")
        if document.needs_pass:
            raise PDFDetectionError(
                f"PDF is password-protected and cannot be inspected without credentials: {path}"
            )
        if document.page_count < 1:
            raise PDFDetectionError(f"PDF contains no pages: {path}")

        outline_entries = _count_outline_entries(document, path)
        sampled_pages = _sample_page_numbers(document.page_count)
        text_character_counts = [
            _page_text_character_count(document, page_number, path)
            for page_number in sampled_pages
        ]
    finally:
        document.close()

    pages_with_text = sum(
        count >= MIN_USABLE_TEXT_CHARACTERS for count in text_character_counts
    )
    sampled_page_count = len(sampled_pages)
    required_text_pages = max(
        1,
        math.ceil(sampled_page_count * MIN_TEXT_PAGE_COVERAGE),
    )
    text_layer = pages_with_text >= required_text_pages
    embedded_outline = outline_entries > 0
    median_text_characters = float(statistics.median(text_character_counts))
    text_coverage = pages_with_text / sampled_page_count

    profile, confidence, selection_reason = _select_profile(
        embedded_outline=embedded_outline,
        text_layer=text_layer,
        pages_with_text=pages_with_text,
        sampled_pages=sampled_page_count,
        required_text_pages=required_text_pages,
        nonempty_pages=sum(count > 0 for count in text_character_counts),
    )

    reasons = [
        (
            f"Embedded PDF outline detected ({outline_entries} entries)."
            if embedded_outline
            else "No embedded PDF outline detected."
        ),
        (
            "Robust text-layer coverage detected: "
            f"{pages_with_text}/{sampled_page_count} sampled pages "
            f"({text_coverage:.1%}) contain at least "
            f"{MIN_USABLE_TEXT_CHARACTERS} non-whitespace characters "
            f"(minimum required: {required_text_pages}/{sampled_page_count} under the "
            f"{MIN_TEXT_PAGE_COVERAGE:.0%} digital-coverage policy)."
            if text_layer
            else "Insufficient text-layer coverage for a digital profile: "
            f"{pages_with_text}/{sampled_page_count} sampled pages "
            f"({text_coverage:.1%}) contain at least "
            f"{MIN_USABLE_TEXT_CHARACTERS} non-whitespace characters "
            f"(minimum required: {required_text_pages}/{sampled_page_count} under the "
            f"{MIN_TEXT_PAGE_COVERAGE:.0%} digital-coverage policy)."
        ),
        f"Median sampled text count: {median_text_characters:.1f} non-whitespace characters.",
        selection_reason,
    ]

    return DetectedCapabilities(
        profile=profile,
        embedded_outline=embedded_outline,
        outline_entries=outline_entries,
        text_layer=text_layer,
        sampled_pages=sampled_pages,
        pages_with_text=pages_with_text,
        median_text_characters=median_text_characters,
        profile_confidence=confidence,
        reasons=reasons,
    )


def _sample_page_numbers(page_count: int) -> list[int]:
    sample_count = min(page_count, MAX_SAMPLED_PAGES)
    if sample_count == page_count:
        return list(range(1, page_count + 1))
    if sample_count == 1:
        return [1]

    # Integer interpolation includes both endpoints and is stable across runs.
    return [
        1 + (sample_index * (page_count - 1)) // (sample_count - 1)
        for sample_index in range(sample_count)
    ]


def _count_outline_entries(document: pymupdf.Document, path: Path) -> int:
    try:
        return len(document.get_toc(simple=True))
    except (RuntimeError, ValueError) as exc:
        raise PDFDetectionError(f"Cannot inspect the embedded outline in '{path}': {exc}") from exc


def _page_text_character_count(
    document: pymupdf.Document,
    page_number: int,
    path: Path,
) -> int:
    try:
        text = document.load_page(page_number - 1).get_text("text")
    except (RuntimeError, ValueError) as exc:
        raise PDFDetectionError(
            f"Cannot inspect text on page {page_number} of '{path}': {exc}"
        ) from exc
    return sum(not character.isspace() for character in text)


def _select_profile(
    *,
    embedded_outline: bool,
    text_layer: bool,
    pages_with_text: int,
    sampled_pages: int,
    required_text_pages: int,
    nonempty_pages: int,
) -> tuple[DocumentProfile, float, str]:
    text_coverage = pages_with_text / sampled_pages

    if embedded_outline and text_layer:
        confidence = min(1.0, 0.92 + 0.08 * text_coverage)
        return (
            DocumentProfile.DIGITAL_OUTLINE,
            round(confidence, 3),
            "Selected digital_outline because both an embedded outline and a usable text layer are available.",
        )

    if text_layer:
        confidence = min(0.98, 0.78 + 0.20 * text_coverage)
        return (
            DocumentProfile.DIGITAL_RECONSTRUCTED,
            round(confidence, 3),
            "Selected digital_reconstructed because usable text is available but the embedded outline is absent.",
        )

    nonempty_coverage = nonempty_pages / sampled_pages
    confidence = 0.97 - 0.25 * nonempty_coverage
    if embedded_outline:
        confidence -= 0.12
    if pages_with_text:
        outline_clause = (
            " An embedded outline does not make the remaining pages safe for a "
            "no-OCR digital parser."
            if embedded_outline
            else ""
        )
        reason = (
            "Selected scanned_ocr fail closed because usable text appears on only "
            f"{pages_with_text}/{sampled_pages} sampled pages, below the required "
            f"{required_text_pages}/{sampled_pages} digital coverage."
            f"{outline_clause}"
        )
    elif embedded_outline:
        reason = (
            "Selected scanned_ocr because the outline alone is insufficient: "
            "the sampled pages do not contain a usable text layer."
        )
    else:
        reason = "Selected scanned_ocr because neither an embedded outline nor a usable text layer is available."
    return DocumentProfile.SCANNED_OCR, round(max(0.5, confidence), 3), reason
