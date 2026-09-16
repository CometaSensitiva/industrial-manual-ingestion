from __future__ import annotations

import pytest

from manual_ingestion.models import (
    ElementType,
    ManualElement,
    PageSize,
    SourceReference,
    TraceMetadata,
)
from manual_ingestion.structure import (
    OCR_FALLBACK_STRATEGY,
    OCR_STRUCTURE_STRATEGY,
    OcrTocReconstructionError,
    reconstruct_toc_from_ocr_elements,
)


def test_printed_index_infers_offset_and_preserves_roman_relative_levels() -> None:
    elements = [
        _title("toc-title", 1, "INDEX", order=0),
        _text(
            "toc-rows",
            1,
            "\n".join(
                [
                    "I. GENERAL ................................ 1",
                    "1. INTRODUCTION .......................... 2",
                    "1.1 SCOPE ................................ 3",
                    "II. OPERATION ............................ 4",
                    "2. USE ................................... 5",
                ]
            ),
            order=1,
        ),
        _title("body-general", 3, "I. GENERAL"),
        _title("body-introduction", 4, "1. INTRODUCTION"),
        _title("body-scope", 5, "1.1 SCOPE"),
        _title("body-operation", 6, "II. OPERATION"),
        _title("body-use", 7, "2. USE"),
    ]

    result = reconstruct_toc_from_ocr_elements(elements, pages_total=7)

    assert [(entry.level, entry.title, entry.page) for entry in result.entries] == [
        (1, "I. GENERAL", 3),
        (2, "1. INTRODUCTION", 4),
        (3, "1.1 SCOPE", 5),
        (1, "II. OPERATION", 6),
        (2, "2. USE", 7),
    ]
    diagnostics = result.diagnostics
    assert diagnostics.strategy == OCR_STRUCTURE_STRATEGY
    assert diagnostics.mode == "printed_index"
    assert diagnostics.detected_toc_pages == (1,)
    assert diagnostics.inferred_offset == 2
    assert len({evidence.title for evidence in diagnostics.offset_evidence}) == 5
    assert [row.index for row in diagnostics.printed_rows] == list(range(5))
    assert [heading.index for heading in diagnostics.heading_candidates] == list(range(5))
    assert all(match.decision == "confirmed" for match in diagnostics.matches)


def test_markdown_table_rows_are_used_as_printed_index() -> None:
    elements = [
        _title("contents", 1, "CONTENTS", order=0),
        _table(
            "toc-table",
            1,
            """| Section | Page |
| --- | ---: |
| 1. INSTALLATION | 1 |
| 2. OPERATION | 2 |""",
            order=1,
        ),
        _title("installation", 2, "1. INSTALLATION"),
        _title("operation", 3, "2. OPERATION"),
    ]

    result = reconstruct_toc_from_ocr_elements(elements, pages_total=3)

    assert [(entry.title, entry.page) for entry in result.entries] == [
        ("1. INSTALLATION", 2),
        ("2. OPERATION", 3),
    ]
    assert result.diagnostics.inferred_offset == 1
    assert {row.source_kind for row in result.diagnostics.printed_rows} == {"table"}


def test_title_fallback_filters_repeated_headers_and_reports_lower_confidence() -> None:
    elements: list[ManualElement] = []
    for page in range(1, 5):
        elements.append(_title(f"header-{page}", page, "SERVICE MANUAL", order=0))
    elements.extend(
        [
            _title("roman-one", 1, "I. PREPARATION", order=1),
            _title("safety", 2, "1. SAFETY", order=1),
            _title("ppe", 3, "1.1 PPE", order=1),
            _title("roman-two", 4, "II. USE", order=1),
            _title("start", 4, "2. START", order=2),
        ]
    )

    result = reconstruct_toc_from_ocr_elements(elements, pages_total=4)

    assert [(entry.level, entry.title, entry.page) for entry in result.entries] == [
        (1, "I. PREPARATION", 1),
        (2, "1. SAFETY", 2),
        (3, "1.1 PPE", 3),
        (1, "II. USE", 4),
        (2, "2. START", 4),
    ]
    assert all(entry.confidence == 0.55 for entry in result.entries)
    diagnostics = result.diagnostics
    assert diagnostics.strategy == OCR_FALLBACK_STRATEGY
    assert diagnostics.mode == "title_fallback"
    assert diagnostics.overall_confidence == 0.55
    assert "lower-confidence" in diagnostics.warnings[0]
    repeated = [item for item in diagnostics.rejected_titles if item.reason == "repeated_header"]
    assert len(repeated) == 4
    assert all(candidate.title != "SERVICE MANUAL" for candidate in diagnostics.heading_candidates)


def test_title_fallback_rejects_visual_caption_roles() -> None:
    caption = _title("figure", 1, "Fig. 2")
    caption = caption.model_copy(
        update={"trace": caption.trace.model_copy(update={"role": "figure_title"})}
    )
    elements = [caption, _title("section", 1, "1. SAFETY", order=1)]

    result = reconstruct_toc_from_ocr_elements(elements, pages_total=1)

    assert [(entry.title, entry.page) for entry in result.entries] == [("1. SAFETY", 1)]
    assert any(
        rejected.element_id == "figure" and rejected.reason == "visual_caption"
        for rejected in result.diagnostics.rejected_titles
    )


def test_duplicate_titles_survive_and_out_of_range_row_is_not_clamped() -> None:
    elements = [
        _title("index", 1, "SOMMARIO", order=0),
        _text(
            "index-rows",
            1,
            "\n".join(
                [
                    "1. START ................................. 1",
                    "WARNINGS ................................. 2",
                    "WARNINGS ................................. 3",
                    "GHOST SECTION ............................ 999",
                    "2. END ................................... 4",
                ]
            ),
            order=1,
        ),
        _title("start", 3, "1. START"),
        _title("warnings-a", 4, "WARNINGS"),
        _title("warnings-b", 5, "WARNINGS"),
        _title("end", 6, "2. END"),
    ]

    result = reconstruct_toc_from_ocr_elements(elements, pages_total=6)

    assert [(entry.title, entry.page) for entry in result.entries] == [
        ("1. START", 3),
        ("WARNINGS", 4),
        ("WARNINGS", 5),
        ("2. END", 6),
    ]
    assert result.diagnostics.inferred_offset == 2
    unresolved = [match for match in result.diagnostics.matches if match.decision == "unresolved"]
    assert len(unresolved) == 1
    assert unresolved[0].page is None
    assert result.diagnostics.printed_rows[3].printed_page == 999
    assert result.diagnostics.printed_rows[3].resolved_page is None
    assert all(entry.page <= 6 for entry in result.entries)


def test_dot_leader_detection_without_keyword_is_deterministic() -> None:
    elements = [
        _text(
            "rows",
            1,
            "\n".join(
                [
                    "1. ALPHA ................................. 1",
                    "2. BETA .................................. 2",
                    "3. GAMMA ................................. 3",
                ]
            ),
        ),
        _title("alpha", 2, "1. ALPHA"),
        _title("beta", 3, "2. BETA"),
        _title("gamma", 4, "3. GAMMA"),
    ]

    first = reconstruct_toc_from_ocr_elements(elements, pages_total=4)
    second = reconstruct_toc_from_ocr_elements(list(reversed(elements)), pages_total=4)

    assert first.diagnostics.detected_toc_pages == (1,)
    assert first.entries == second.entries
    assert first.diagnostics.to_dict() == second.diagnostics.to_dict()


def test_printed_index_never_silently_falls_back_when_rows_are_unusable() -> None:
    elements = [
        _title("index", 1, "INDEX"),
        _title("body-title", 2, "1. START"),
    ]

    with pytest.raises(OcrTocReconstructionError, match="no usable TOC rows"):
        reconstruct_toc_from_ocr_elements(elements, pages_total=2)


def test_printed_index_fails_when_too_many_rows_remain_unresolved() -> None:
    elements = [
        _title("index", 1, "INDEX"),
        _text(
            "rows",
            1,
            "ALPHA ..... 1\nBETA ..... 2\nGAMMA ..... 3",
        ),
        _title("alpha", 2, "ALPHA"),
    ]

    with pytest.raises(OcrTocReconstructionError, match="below the configured trust threshold"):
        reconstruct_toc_from_ocr_elements(elements, pages_total=4)


def test_explicit_error_when_no_printed_index_or_structural_titles_exist() -> None:
    elements = [_text("body", 1, "ordinary OCR body text")]

    with pytest.raises(OcrTocReconstructionError, match="no usable structural title"):
        reconstruct_toc_from_ocr_elements(elements, pages_total=1)


def _title(
    identifier: str,
    page: int,
    text: str,
    *,
    order: int | None = None,
) -> ManualElement:
    return _element(identifier, ElementType.TITLE, page, text=text, order=order)


def _text(
    identifier: str,
    page: int,
    text: str,
    *,
    order: int | None = None,
) -> ManualElement:
    return _element(identifier, ElementType.TEXT, page, text=text, order=order)


def _table(
    identifier: str,
    page: int,
    markdown: str,
    *,
    order: int | None = None,
) -> ManualElement:
    return _element(identifier, ElementType.TABLE, page, table_markdown=markdown, order=order)


def _element(
    identifier: str,
    element_type: ElementType,
    page: int,
    *,
    text: str | None = None,
    table_markdown: str | None = None,
    order: int | None = None,
) -> ManualElement:
    return ManualElement(
        id=identifier,
        type=element_type,
        page=page,
        source=SourceReference(
            page=page,
            page_size=PageSize(width=595, height=842),
        ),
        text=text,
        table_markdown=table_markdown,
        trace=TraceMetadata(reading_order=order),
    )
