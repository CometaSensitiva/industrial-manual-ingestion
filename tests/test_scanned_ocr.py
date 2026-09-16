from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from manual_ingestion.models import (
    Chapter,
    DocumentProfile,
    ElementType,
    ManualElement,
    PageSize,
    SourceReference,
    TraceMetadata,
)
from manual_ingestion.profiles.base import ContentParseResult
from manual_ingestion.profiles.scanned_ocr import (
    SCANNED_EMBEDDED_OUTLINE_STRATEGY,
    ScannedOCRProfile,
)
from manual_ingestion.structure import OCR_STRUCTURE_STRATEGY


class FakePaddleAdapter:
    name = "paddleocr_vl"
    last_runtime = {
        "python": "3.12",
        "paddleocr": "3.7.0",
        "paddlepaddle": "3.2.1",
        "paddlex": "3.7.2",
    }

    def __init__(self, elements: list[ManualElement]) -> None:
        self.elements = elements
        self.requested_pages: list[int] | None = None

    def parse(self, pdf_path: Path, pages: list[int], assets_dir: Path) -> ContentParseResult:
        self.requested_pages = pages
        return ContentParseResult(
            elements=[element for element in self.elements if element.page in pages],
            pages_processed=list(pages),
            warnings=["fake Paddle warning"],
        )


def test_scanned_profile_reconstructs_printed_index_and_attaches_trace(tmp_path: Path) -> None:
    source = _scanned_pdf(tmp_path / "scan.pdf", pages=4, title="Scanned Manual")
    paddle = FakePaddleAdapter(
        [
            _title("index", 1, "INDICE", 0),
            _text(
                "toc-rows",
                1,
                "1. START ........................ 1\n2. STOP ......................... 2",
                1,
            ),
            _title("start", 3, "1. START", 2),
            _text("start-body", 3, "Start instructions", 3),
            _title("stop", 4, "2. STOP", 4),
            _text("stop-body", 4, "Stop instructions", 5),
        ]
    )

    result = ScannedOCRProfile(content_adapter=paddle).run(
        source,
        run_id="scan-run",
        assets_dir=tmp_path / "run" / "assets",
    )

    assert paddle.requested_pages == [1, 2, 3, 4]
    assert result.manual.metadata.profile is DocumentProfile.SCANNED_OCR
    assert result.manual.metadata.parser == "paddleocr_vl"
    assert result.manual.metadata.structure_strategy == OCR_STRUCTURE_STRATEGY
    assert [(entry.title, entry.page) for entry in result.toc] == [
        ("1. START", 3),
        ("2. STOP", 4),
    ]
    assert result.diagnostics is not None
    assert result.diagnostics.inferred_offset == 2
    assert result.runtime == paddle.last_runtime
    assert result.warnings[0] == "fake Paddle warning"
    assert any("precede the first outline" in warning for warning in result.warnings)
    start_chapter = next(
        item
        for item in result.manual.content
        if isinstance(item, Chapter) and item.title == "1. START"
    )
    assert start_chapter.title == "1. START"
    assert any(
        isinstance(item, ManualElement)
        and item.id == "start-body"
        and item.trace.parent_chapter_id == start_chapter.id
        for item in start_chapter.content
    )
    toc_elements = [
        item
        for item in result.manual.content
        if isinstance(item, ManualElement) and item.page == 1
    ]
    assert {item.id for item in toc_elements} == {"index", "toc-rows"}
    assert all(item.trace.parent_chapter_id is None for item in toc_elements)
    assert all(item.trace.include_in_rag is False for item in toc_elements)
    assert all(
        item.trace.exclusion_reason == "printed table-of-contents source page"
        for item in toc_elements
    )


def test_scanned_profile_reuses_embedded_outline_when_text_layer_is_absent(
    tmp_path: Path,
) -> None:
    source = _scanned_pdf(
        tmp_path / "outlined-scan.pdf",
        pages=2,
        outline=[[1, "Safety", 1], [1, "Operation", 2]],
    )
    paddle = FakePaddleAdapter(
        [
            _text("safety", 1, "Safety body", 0),
            _text("operation", 2, "Operation body", 1),
        ]
    )

    result = ScannedOCRProfile(content_adapter=paddle).run(
        source,
        run_id="outlined-scan",
        assets_dir=tmp_path / "run" / "assets",
    )

    assert result.diagnostics is None
    assert result.manual.metadata.structure_strategy == SCANNED_EMBEDDED_OUTLINE_STRATEGY
    assert [(entry.title, entry.page) for entry in result.toc] == [
        ("Safety", 1),
        ("Operation", 2),
    ]


def test_printed_index_between_body_chapters_is_detached_from_previous_chapter(
    tmp_path: Path,
) -> None:
    source = _scanned_pdf(tmp_path / "scan.pdf", pages=3)
    paddle = FakePaddleAdapter(
        [
            _title("alpha", 1, "ALPHA", 0),
            _text("alpha-body", 1, "Alpha instructions", 1),
            _title("index", 2, "INDEX", 2),
            _text("toc-rows", 2, "ALPHA ..... 1\nBETA ..... 3", 3),
            _title("beta", 3, "BETA", 4),
            _text("beta-body", 3, "Beta instructions", 5),
        ]
    )

    result = ScannedOCRProfile(content_adapter=paddle).run(
        source,
        run_id="middle-index",
        assets_dir=tmp_path / "run" / "assets",
    )

    alpha = next(
        item
        for item in result.manual.content
        if isinstance(item, Chapter) and item.title == "ALPHA"
    )
    assert all(
        not isinstance(item, ManualElement) or item.page != 2
        for item in alpha.content
    )
    root_toc = [
        item
        for item in result.manual.content
        if isinstance(item, ManualElement) and item.page == 2
    ]
    assert {item.id for item in root_toc} == {"index", "toc-rows"}
    assert all(item.trace.include_in_rag is False for item in root_toc)


def test_partial_scanned_profile_is_explicitly_warned(tmp_path: Path) -> None:
    source = _scanned_pdf(tmp_path / "scan.pdf", pages=3)
    paddle = FakePaddleAdapter([_title("only", 1, "1. SAFETY", 0)])

    result = ScannedOCRProfile(content_adapter=paddle).run(
        source,
        run_id="partial",
        assets_dir=tmp_path / "run" / "assets",
        pages=[1],
    )

    assert any("partial page selection" in warning for warning in result.warnings)
    assert result.diagnostics is not None
    assert result.diagnostics.mode == "title_fallback"


def test_scanned_profile_rejects_empty_run_id_before_ocr(tmp_path: Path) -> None:
    source = _scanned_pdf(tmp_path / "scan.pdf", pages=1)
    paddle = FakePaddleAdapter([_title("only", 1, "SAFETY", 0)])

    with pytest.raises(ValueError, match="run_id cannot be empty"):
        ScannedOCRProfile(content_adapter=paddle).run(
            source,
            run_id="  ",
            assets_dir=tmp_path / "run" / "assets",
        )

    assert paddle.requested_pages is None


def _scanned_pdf(
    path: Path,
    *,
    pages: int,
    title: str | None = None,
    outline: list[list[object]] | None = None,
) -> Path:
    document = pymupdf.open()
    for _ in range(pages):
        page = document.new_page(width=300, height=400)
        page.draw_rect((20, 20, 280, 380), color=(0, 0, 0))
    if title:
        document.set_metadata({"title": title})
    if outline:
        document.set_toc(outline)
    document.save(path)
    document.close()
    return path


def _title(identifier: str, page: int, text: str, order: int) -> ManualElement:
    return _element(identifier, ElementType.TITLE, page, text, order)


def _text(identifier: str, page: int, text: str, order: int) -> ManualElement:
    return _element(identifier, ElementType.TEXT, page, text, order)


def _element(
    identifier: str,
    element_type: ElementType,
    page: int,
    text: str,
    order: int,
) -> ManualElement:
    return ManualElement(
        id=identifier,
        type=element_type,
        page=page,
        source=SourceReference(
            page=page,
            page_size=PageSize(width=300, height=400),
        ),
        text=text,
        trace=TraceMetadata(reading_order=order),
    )
