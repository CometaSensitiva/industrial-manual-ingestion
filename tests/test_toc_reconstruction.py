from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from manual_ingestion.structure import (
    STRUCTURE_STRATEGY,
    TocReconstructionError,
    reconstruct_toc,
)


PAGE_WIDTH = 595
PAGE_HEIGHT = 842
BODY_TEXT = "Ordinary technical body text long enough to establish the normal document font size."


def test_reconstructs_printed_index_with_footer_page_offset(tmp_path: Path) -> None:
    source = tmp_path / "digital-no-outline.pdf"
    document = _new_document(5)
    _insert(document, 1, "Copertina", 50, 120, 16)
    _insert(document, 2, "INDICE", 50, 80, 16)
    _insert(document, 2, "1. INTRODUZIONE ................................ 1", 70, 130, 11)
    _insert(document, 2, "1.1 SCOPO ...................................... 2", 70, 155, 11)
    _insert(document, 2, "2. UTILIZZO .................................... 3", 70, 180, 11)
    _body_page(document, 3, "1. INTRODUZIONE", printed_page=1, heading_size=18)
    _body_page(document, 4, "1.1 SCOPO", printed_page=2, heading_size=16)
    _body_page(document, 5, "2. UTILIZZO", printed_page=3, heading_size=18)
    _save(document, source)

    result = reconstruct_toc(source)

    assert _entries(result) == [
        {"level": 1, "title": "1. INTRODUZIONE", "page": 3},
        {"level": 2, "title": "1.1 SCOPO", "page": 4},
        {"level": 1, "title": "2. UTILIZZO", "page": 5},
    ]
    assert result.diagnostics.strategy == STRUCTURE_STRATEGY
    assert result.diagnostics.detected_toc_pages == (2,)
    assert result.diagnostics.page_number_map["namespaces"]["body"]["offset"] == 2
    assert all(match.decision == "matched" for match in result.diagnostics.matches)


def test_handles_multipage_index_wrapped_rows_and_roman_hierarchy(tmp_path: Path) -> None:
    source = tmp_path / "multipage-index.pdf"
    document = _new_document(6)
    _insert(document, 1, "Cover", 50, 120, 16)

    _insert(document, 2, "INDICE", 50, 70, 16)
    _insert(document, 2, "I. PRIMA PARTE", 70, 110, 11)
    _insert(document, 2, "1", 70, 140, 11)
    _insert(document, 2, "INTRODUZIONE ................................. 1", 100, 165, 11)
    _insert(document, 2, "1.1 SCOPO MOLTO", 70, 190, 11)
    _insert(document, 2, "DELLA PROCEDURA ............................... 2", 100, 215, 11)

    _insert(document, 3, "INDICE", 50, 70, 16)
    _insert(document, 3, "2. UTILIZZO .................................... 3", 70, 120, 11)

    _insert(document, 4, "I. PRIMA PARTE", 50, 95, 18)
    _insert(document, 4, "1 INTRODUZIONE", 50, 130, 16)
    _insert(document, 4, BODY_TEXT, 50, 180, 9)
    _footer(document, 4, 1)
    _body_page(document, 5, "1.1 SCOPO MOLTO DELLA PROCEDURA", printed_page=2, heading_size=16)
    _body_page(document, 6, "2. UTILIZZO", printed_page=3, heading_size=18)
    _save(document, source)

    result = reconstruct_toc(source)

    assert result.diagnostics.detected_toc_pages == (2, 3)
    assert _entries(result) == [
        {"level": 1, "title": "I. PRIMA PARTE", "page": 4},
        {"level": 2, "title": "1 INTRODUZIONE", "page": 4},
        {"level": 3, "title": "1.1 SCOPO MOLTO DELLA PROCEDURA", "page": 5},
        {"level": 2, "title": "2. UTILIZZO", "page": 6},
    ]
    signals = [signal for row in result.diagnostics.printed_rows for signal in row.signals]
    assert "split_numbering_token" in signals
    assert "wrapped_printed_row" in signals
    assert "inside_roman_section" in signals


def test_preserves_duplicate_titles_and_rejects_unresolved_out_of_range_rows(
    tmp_path: Path,
) -> None:
    source = tmp_path / "duplicates-and-noise.pdf"
    document = _new_document(5)
    _insert(document, 2, "CONTENTS", 50, 80, 16)
    _insert(document, 2, "AVVERTENZE ................................. 1", 70, 130, 11)
    _insert(document, 2, "AVVERTENZE ................................. 2", 70, 155, 11)
    _insert(document, 2, "FUORI RANGE ................................ 9999", 70, 180, 11)

    for page_number in (3, 4, 5):
        _insert(document, page_number, "MANUALE TECNICO", 50, 28, 12)
        _insert(document, page_number, BODY_TEXT, 50, 175, 9)
        _footer(document, page_number, page_number - 2)
    _insert(document, 3, "AVVERTENZE", 50, 100, 18)
    _insert(document, 4, "AVVERTENZE", 50, 100, 18)
    _save(document, source)

    result = reconstruct_toc(source)

    assert _entries(result) == [
        {"level": 1, "title": "AVVERTENZE", "page": 3},
        {"level": 1, "title": "AVVERTENZE", "page": 4},
    ]
    assert all(entry.page != 1 for entry in result.entries)
    unresolved = [match for match in result.diagnostics.matches if match.decision == "unresolved"]
    assert len(unresolved) == 1
    assert unresolved[0].page is None
    assert result.diagnostics.warnings == ("Unresolved TOC row 2: FUORI RANGE",)
    assert all(candidate.title != "MANUALE TECNICO" for candidate in result.diagnostics.heading_candidates)


def test_uses_native_pdf_page_labels_when_available(tmp_path: Path) -> None:
    source = tmp_path / "page-labels.pdf"
    document = _new_document(3)
    _insert(document, 1, "TABLE OF CONTENTS", 50, 80, 16)
    _insert(document, 1, "1 START ...................................... 1", 70, 130, 11)
    _body_page(document, 2, "1 START", printed_page=None, heading_size=18)
    _insert(document, 3, BODY_TEXT, 50, 175, 9)
    document.set_page_labels(
        [
            {"startpage": 0, "prefix": "cover-", "style": "D", "firstpagenum": 1},
            {"startpage": 1, "prefix": "", "style": "D", "firstpagenum": 1},
        ]
    )
    _save(document, source)

    result = reconstruct_toc(source)

    assert _entries(result) == [{"level": 1, "title": "1 START", "page": 2}]
    assert result.diagnostics.page_number_map["namespaces"]["body"] == {
        "prefix": "",
        "pdf_start": 2,
        "printed_start": 1,
        "evidence_count": 2,
        "source": "pdf_page_labels",
        "offset": 1,
    }


def test_preserves_numbered_toc_titles_longer_than_body_heading_limit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "long-wrapped-index-title.pdf"
    document = _new_document(3)
    first = "Lavorazione conica con specifica della forma della sommità e della base"
    second = "del pezzo con superficie programmata e superficie opposta definite"
    third = "indipendentemente per la preparazione tecnica estesa"
    expected_title = f"2.12.9 {first} {second} {third}"
    assert len(expected_title) > 180

    _insert(document, 2, "INDICE", 50, 70, 16)
    _insert(document, 2, "2.12.9", 70, 115, 11)
    _insert(document, 2, first, 100, 140, 11)
    _insert(document, 2, second, 100, 165, 11)
    _insert(document, 2, f"{third} ........................ 1", 100, 190, 11)
    _body_page(document, 3, "2.12.9 Lavorazione conica", printed_page=None, heading_size=16)
    document.set_page_labels(
        [
            {"startpage": 0, "prefix": "cover-", "style": "D", "firstpagenum": 1},
            {"startpage": 2, "prefix": "", "style": "D", "firstpagenum": 1},
        ]
    )
    _save(document, source)

    result = reconstruct_toc(source)

    assert _entries(result) == [
        {"level": 3, "title": expected_title, "page": 3}
    ]
    assert result.diagnostics.printed_rows[0].signals == (
        "split_numbering_token",
    )


def test_raises_explicit_error_when_no_printed_index_exists(tmp_path: Path) -> None:
    source = tmp_path / "no-index.pdf"
    document = _new_document(3)
    for page_number in range(1, 4):
        _body_page(document, page_number, f"{page_number} SECTION", printed_page=None, heading_size=16)
    _save(document, source)

    with pytest.raises(TocReconstructionError, match="No printed table-of-contents pages"):
        reconstruct_toc(source)


def test_rejects_embedded_outline_in_case2_engine(tmp_path: Path) -> None:
    source = tmp_path / "has-outline.pdf"
    document = _new_document(2)
    _insert(document, 1, "CONTENTS", 50, 80, 16)
    _insert(document, 1, "SECTION ...................................... 2", 70, 130, 11)
    document.set_toc([[1, "SECTION", 2]])
    _save(document, source)

    with pytest.raises(TocReconstructionError, match="use digital_outline"):
        reconstruct_toc(source)


def test_reconstruction_is_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "deterministic.pdf"
    document = _new_document(4)
    _insert(document, 1, "SOMMARIO", 50, 80, 16)
    _insert(document, 1, "1 ALPHA ....................................... 1", 70, 130, 11)
    _insert(document, 1, "2 BETA ........................................ 2", 70, 155, 11)
    _body_page(document, 3, "1 ALPHA", printed_page=1, heading_size=18)
    _body_page(document, 4, "2 BETA", printed_page=2, heading_size=18)
    _save(document, source)

    first = reconstruct_toc(source)
    second = reconstruct_toc(source)

    assert _entries(first) == _entries(second)
    assert first.diagnostics.to_dict() == second.diagnostics.to_dict()
    assert [candidate.index for candidate in first.diagnostics.heading_candidates] == list(
        range(len(first.diagnostics.heading_candidates))
    )


def _new_document(page_count: int) -> pymupdf.Document:
    document = pymupdf.open()
    for _ in range(page_count):
        document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    return document


def _insert(
    document: pymupdf.Document,
    page_number: int,
    text: str,
    x: float,
    y: float,
    size: float,
) -> None:
    document.load_page(page_number - 1).insert_text((x, y), text, fontsize=size)


def _footer(document: pymupdf.Document, page_number: int, printed_page: int) -> None:
    _insert(document, page_number, f"- {printed_page} -", 285, 820, 9)


def _body_page(
    document: pymupdf.Document,
    page_number: int,
    heading: str,
    *,
    printed_page: int | None,
    heading_size: float,
) -> None:
    _insert(document, page_number, heading, 50, 100, heading_size)
    _insert(document, page_number, BODY_TEXT, 50, 160, 9)
    if printed_page is not None:
        _footer(document, page_number, printed_page)


def _save(document: pymupdf.Document, path: Path) -> None:
    try:
        document.save(path)
    finally:
        document.close()


def _entries(result) -> list[dict[str, object]]:
    return [entry.model_dump(exclude_none=True) for entry in result.entries]
