from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from manual_ingestion.models import (
    Chapter,
    DocumentProfile,
    ManualElement,
    PageSize,
    SourceReference,
)
from manual_ingestion.profiles.base import ContentParseResult, ProfileResult
from manual_ingestion.profiles.digital_reconstructed import (
    DigitalReconstructedProfile,
    DigitalReconstructedResult,
)
from manual_ingestion.structure import STRUCTURE_STRATEGY, TocReconstructionError


PAGE_WIDTH = 595
PAGE_HEIGHT = 842
BODY_TEXT = "Ordinary technical body text long enough to establish the normal document font size."


class _FakeDoclingAdapter:
    name = "docling"

    def __init__(self) -> None:
        self.called = False

    def parse(self, pdf_path: Path, pages: list[int], assets_dir: Path) -> ContentParseResult:
        self.called = True
        elements = [
            _element("text-introduction", 3, "Introduction body"),
            _element("text-installation", 4, "Installation body"),
            _element("text-operation", 5, "Operation body"),
        ]
        return ContentParseResult(
            elements=[element for element in elements if element.page in pages],
            pages_processed=list(pages),
            warnings=["fake Docling warning"],
        )


def test_digital_reconstructed_uses_fused_toc_for_hierarchy_and_trace(tmp_path: Path) -> None:
    source = _reconstructable_pdf(tmp_path / "manual-no-outline.pdf")
    content_adapter = _FakeDoclingAdapter()
    profile = DigitalReconstructedProfile(content_adapter=content_adapter)

    result = profile.run(
        source,
        run_id="run-case-2",
        assets_dir=tmp_path / "run" / "assets",
    )

    assert isinstance(result, DigitalReconstructedResult)
    assert isinstance(result, ProfileResult)
    assert content_adapter.called is True
    assert result.manual.title == "Digital Service Manual"
    assert result.manual.metadata.profile is DocumentProfile.DIGITAL_RECONSTRUCTED
    assert result.manual.metadata.parser == "docling"
    assert result.manual.metadata.structure_strategy == STRUCTURE_STRATEGY
    assert result.manual.metadata.pages_processed == [1, 2, 3, 4, 5]
    assert result.manual.metadata.toc_available is True
    assert result.warnings == ["fake Docling warning"]

    assert [(entry.level, entry.title, entry.page) for entry in result.toc] == [
        (1, "1. INTRODUZIONE", 3),
        (2, "1.1 INSTALLAZIONE", 4),
        (1, "2. UTILIZZO", 5),
    ]
    introduction = result.manual.content[0]
    operation = result.manual.content[1]
    assert isinstance(introduction, Chapter)
    assert isinstance(operation, Chapter)

    introduction_body = introduction.content[0]
    installation = introduction.content[1]
    assert isinstance(introduction_body, ManualElement)
    assert isinstance(installation, Chapter)
    installation_body = installation.content[0]
    operation_body = operation.content[0]
    assert isinstance(installation_body, ManualElement)
    assert isinstance(operation_body, ManualElement)
    assert introduction_body.trace.parent_chapter_id == introduction.id
    assert installation_body.trace.parent_chapter_id == installation.id
    assert operation_body.trace.parent_chapter_id == operation.id
    assert [
        introduction_body.trace.reading_order,
        installation_body.trace.reading_order,
        operation_body.trace.reading_order,
    ] == [0, 1, 2]

    assert result.diagnostics.strategy == STRUCTURE_STRATEGY
    assert result.diagnostics.detected_toc_pages == (2,)
    assert result.diagnostics.entry_count == len(result.toc)
    assert result.diagnostics.to_dict()["strategy"] == STRUCTURE_STRATEGY


def test_digital_reconstructed_rejects_embedded_outline_before_content_parse(
    tmp_path: Path,
) -> None:
    source = tmp_path / "has-outline.pdf"
    document = _new_document(2)
    _insert(document, 1, "CONTENTS", 50, 80, 16)
    _insert(document, 1, "SECTION ...................................... 2", 70, 130, 11)
    document.set_toc([[1, "SECTION", 2]])
    _save(document, source)
    content_adapter = _FakeDoclingAdapter()

    with pytest.raises(TocReconstructionError, match="use digital_outline"):
        DigitalReconstructedProfile(content_adapter=content_adapter).run(
            source,
            run_id="wrong-profile",
            assets_dir=tmp_path / "assets",
        )

    assert content_adapter.called is False


def test_digital_reconstructed_reports_missing_printed_toc_explicitly(tmp_path: Path) -> None:
    source = tmp_path / "no-printed-index.pdf"
    document = _new_document(3)
    for page_number in range(1, 4):
        _insert(document, page_number, f"{page_number} SECTION", 50, 100, 16)
        _insert(document, page_number, BODY_TEXT, 50, 160, 9)
    _save(document, source)
    content_adapter = _FakeDoclingAdapter()

    with pytest.raises(TocReconstructionError, match="No printed table-of-contents pages"):
        DigitalReconstructedProfile(content_adapter=content_adapter).run(
            source,
            run_id="missing-toc",
            assets_dir=tmp_path / "assets",
        )

    assert content_adapter.called is False


def _reconstructable_pdf(path: Path) -> Path:
    document = _new_document(5)
    document.set_metadata({"title": "Digital Service Manual"})
    _insert(document, 1, "Copertina", 50, 120, 16)
    _insert(document, 2, "INDICE", 50, 80, 16)
    _insert(document, 2, "1. INTRODUZIONE ................................ 1", 70, 130, 11)
    _insert(document, 2, "1.1 INSTALLAZIONE .............................. 2", 70, 155, 11)
    _insert(document, 2, "2. UTILIZZO .................................... 3", 70, 180, 11)
    _body_page(document, 3, "1. INTRODUZIONE", printed_page=1, heading_size=18)
    _body_page(document, 4, "1.1 INSTALLAZIONE", printed_page=2, heading_size=16)
    _body_page(document, 5, "2. UTILIZZO", printed_page=3, heading_size=18)
    _save(document, path)
    return path


def _element(identifier: str, page: int, text: str) -> ManualElement:
    return ManualElement(
        id=identifier,
        type="text",
        page=page,
        source=SourceReference(
            page=page,
            page_size=PageSize(width=PAGE_WIDTH, height=PAGE_HEIGHT),
        ),
        text=text,
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


def _body_page(
    document: pymupdf.Document,
    page_number: int,
    heading: str,
    *,
    printed_page: int,
    heading_size: float,
) -> None:
    _insert(document, page_number, heading, 50, 100, heading_size)
    _insert(document, page_number, BODY_TEXT, 50, 160, 9)
    _insert(document, page_number, f"- {printed_page} -", 285, 820, 9)


def _save(document: pymupdf.Document, path: Path) -> None:
    try:
        document.save(path)
    finally:
        document.close()
