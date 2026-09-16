from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from manual_ingestion.models import (
    Chapter,
    DocumentProfile,
    ElementType,
    ManualElement,
    PageSize,
    SourceReference,
)
from manual_ingestion.profiles.base import ContentParseResult, ProfileExecutionError
from manual_ingestion.profiles.digital_outline import DigitalOutlineProfile


def _outlined_pdf(path: Path, *, with_outline: bool = True) -> Path:
    document = fitz.open()
    for page_number in range(1, 4):
        page = document.new_page(width=200, height=100)
        page.insert_text((15, 50), f"Page {page_number}")
    document.set_metadata({"title": "Hydraulic Pump Manual"})
    document.save(path)
    document.close()
    if with_outline:
        with fitz.open(path) as document:
            document.set_toc(
                [
                    [1, "Introduction", 1],
                    [2, "Installation", 2],
                    [1, "Operation", 3],
                ]
            )
            document.saveIncr()
    return path


def _element(
    identifier: str,
    page: int,
    text: str,
    *,
    element_type: ElementType = ElementType.TEXT,
) -> ManualElement:
    return ManualElement(
        id=identifier,
        type=element_type,
        page=page,
        source=SourceReference(
            page=page,
            page_size=PageSize(width=200, height=100),
        ),
        text=text,
    )


class _FakeContentAdapter:
    name = "fake-docling"

    def parse(self, pdf_path: Path, pages: list[int], assets_dir: Path) -> ContentParseResult:
        return ContentParseResult(
            elements=[
                _element("text-1", 1, "Introduction body"),
                _element("text-2", 2, "Installation body"),
                _element("text-3", 3, "Operation body"),
            ],
            pages_processed=list(pages),
            warnings=["fake warning"],
        )


class _StaticContentAdapter:
    name = "static-docling"

    def __init__(self, elements: list[ManualElement]) -> None:
        self.elements = elements

    def parse(self, pdf_path: Path, pages: list[int], assets_dir: Path) -> ContentParseResult:
        return ContentParseResult(
            elements=list(self.elements),
            pages_processed=list(pages),
        )


def _same_page_outlined_pdf(path: Path) -> Path:
    document = fitz.open()
    for page_number in range(1, 3):
        page = document.new_page(width=200, height=100)
        page.insert_text((15, 50), f"Page {page_number}")
    document.set_metadata({"title": "Same-page Sections"})
    document.set_toc(
        [
            [1, "Chapter One", 1],
            [2, "First Section", 1],
            [2, "Second Section", 1],
            [1, "Chapter Two", 2],
        ]
    )
    document.save(path)
    document.close()
    return path


def _same_page_root_outline_pdf(path: Path) -> Path:
    document = fitz.open()
    page = document.new_page(width=200, height=100)
    page.insert_text((15, 50), "Two root chapters")
    document.set_toc(
        [
            [1, "Chapter One", 1],
            [1, "Chapter Two", 1],
        ]
    )
    document.save(path)
    document.close()
    return path


def test_digital_outline_builds_authoritative_hierarchy_and_trace(tmp_path: Path) -> None:
    path = _outlined_pdf(tmp_path / "manual.pdf")
    profile = DigitalOutlineProfile(content_adapter=_FakeContentAdapter())

    result = profile.run(path, run_id="run-case-1", assets_dir=tmp_path / "run" / "assets")

    manual = result.manual
    assert manual.id == "run-case-1"
    assert manual.title == "Hydraulic Pump Manual"
    assert manual.source_file == "manual.pdf"
    assert manual.metadata.profile is DocumentProfile.DIGITAL_OUTLINE
    assert manual.metadata.parser == "fake-docling"
    assert manual.metadata.structure_strategy == "embedded_outline"
    assert manual.metadata.pages_processed == [1, 2, 3]
    assert manual.metadata.toc_available is True
    assert result.warnings == ["fake warning"]

    introduction = manual.content[0]
    operation = manual.content[1]
    assert isinstance(introduction, Chapter)
    assert isinstance(operation, Chapter)
    assert (introduction.title, introduction.level, introduction.page) == ("Introduction", 1, 1)
    assert (operation.title, operation.level, operation.page) == ("Operation", 1, 3)

    intro_body = introduction.content[0]
    installation = introduction.content[1]
    assert isinstance(intro_body, ManualElement)
    assert isinstance(installation, Chapter)
    installation_body = installation.content[0]
    operation_body = operation.content[0]
    assert isinstance(installation_body, ManualElement)
    assert isinstance(operation_body, ManualElement)
    assert intro_body.trace.parent_chapter_id == introduction.id
    assert installation_body.trace.parent_chapter_id == installation.id
    assert operation_body.trace.parent_chapter_id == operation.id
    assert [intro_body.trace.reading_order, installation_body.trace.reading_order, operation_body.trace.reading_order] == [
        0,
        1,
        2,
    ]
    assert [(entry.level, entry.title, entry.page) for entry in result.toc] == [
        (1, "Introduction", 1),
        (2, "Installation", 2),
        (1, "Operation", 3),
    ]


def test_digital_outline_refuses_pdf_without_embedded_outline(tmp_path: Path) -> None:
    path = _outlined_pdf(tmp_path / "manual.pdf", with_outline=False)
    profile = DigitalOutlineProfile(content_adapter=_FakeContentAdapter())

    with pytest.raises(ProfileExecutionError, match="digital_reconstructed"):
        profile.run(path, run_id="run-case-2", assets_dir=tmp_path / "assets")


def test_digital_outline_uses_title_anchors_to_split_same_page_sections(
    tmp_path: Path,
) -> None:
    path = _same_page_outlined_pdf(tmp_path / "manual.pdf")
    adapter = _StaticContentAdapter(
        [
            _element(
                "title-chapter",
                1,
                "CHAPTER ONE",
                element_type=ElementType.TITLE,
            ),
            _element("chapter-intro", 1, "Intro before the first subsection."),
            _element(
                "title-first",
                1,
                "First section",
                element_type=ElementType.TITLE,
            ),
            _element("first-body", 1, "First body."),
            _element(
                "title-second",
                1,
                "Second Section",
                element_type=ElementType.TITLE,
            ),
            _element("second-body", 1, "Second body."),
            _element("chapter-two-body", 2, "Distinct-page behavior."),
        ]
    )

    result = DigitalOutlineProfile(content_adapter=adapter).run(
        path,
        run_id="run-same-page",
        assets_dir=tmp_path / "assets",
    )

    chapter_one, chapter_two = result.manual.content
    assert isinstance(chapter_one, Chapter)
    assert isinstance(chapter_two, Chapter)
    assert [
        item.id if isinstance(item, ManualElement) else item.title
        for item in chapter_one.content
    ] == ["title-chapter", "chapter-intro", "First Section", "Second Section"]

    first_section = chapter_one.content[2]
    second_section = chapter_one.content[3]
    assert isinstance(first_section, Chapter)
    assert isinstance(second_section, Chapter)
    assert [item.id for item in first_section.content if isinstance(item, ManualElement)] == [
        "title-first",
        "first-body",
    ]
    assert [item.id for item in second_section.content if isinstance(item, ManualElement)] == [
        "title-second",
        "second-body",
    ]
    assert all(
        item.trace.parent_chapter_id == first_section.id
        for item in first_section.content
        if isinstance(item, ManualElement)
    )
    assert all(
        item.trace.parent_chapter_id == second_section.id
        for item in second_section.content
        if isinstance(item, ManualElement)
    )
    assert [
        item.id for item in chapter_two.content if isinstance(item, ManualElement)
    ] == ["chapter-two-body"]
    assert result.warnings == []


def test_digital_outline_warns_when_same_page_title_anchors_are_not_unique(
    tmp_path: Path,
) -> None:
    path = _same_page_outlined_pdf(tmp_path / "manual.pdf")
    adapter = _StaticContentAdapter(
        [
            _element(
                "title-chapter-a",
                1,
                "Chapter One",
                element_type=ElementType.TITLE,
            ),
            _element(
                "title-chapter-b",
                1,
                "CHAPTER ONE",
                element_type=ElementType.TITLE,
            ),
            _element(
                "title-first",
                1,
                "First Section",
                element_type=ElementType.TITLE,
            ),
            _element("unresolved-body", 1, "No heading for the second section."),
            _element("chapter-two-body", 2, "Distinct-page behavior."),
        ]
    )

    result = DigitalOutlineProfile(content_adapter=adapter).run(
        path,
        run_id="run-ambiguous-anchor",
        assets_dir=tmp_path / "assets",
    )

    assert any(
        "outline anchor ambiguous" in warning and "Chapter One" in warning
        for warning in result.warnings
    )
    assert any(
        "outline anchor unmatched" in warning and "Second Section" in warning
        for warning in result.warnings
    )


def test_digital_outline_never_reorders_outline_siblings_from_partial_anchors(
    tmp_path: Path,
) -> None:
    path = _same_page_root_outline_pdf(tmp_path / "manual.pdf")
    adapter = _StaticContentAdapter(
        [
            _element(
                "title-one",
                1,
                "Chapter One",
                element_type=ElementType.TITLE,
            ),
            _element("body", 1, "Body after the only resolved title."),
        ]
    )

    result = DigitalOutlineProfile(content_adapter=adapter).run(
        path,
        run_id="run-partial-root-anchors",
        assets_dir=tmp_path / "assets",
    )

    chapters = [item for item in result.manual.content if isinstance(item, Chapter)]
    assert [chapter.title for chapter in chapters] == ["Chapter One", "Chapter Two"]
    assert [entry.title for entry in result.toc] == ["Chapter One", "Chapter Two"]
    assert any(
        "outline anchor unmatched" in warning and "Chapter Two" in warning
        for warning in result.warnings
    )


def test_digital_outline_resolves_unique_normalized_and_split_title_anchors(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manual.pdf"
    document = fitz.open()
    document.new_page(width=200, height=100)
    document.set_toc(
        [
            [1, "1 Safety controls", 1],
            [1, "2 Long operation heading", 1],
        ]
    )
    document.save(path)
    document.close()
    adapter = _StaticContentAdapter(
        [
            _element(
                "title-safety",
                1,
                "Safety controls",
                element_type=ElementType.TITLE,
            ),
            _element("safety-body", 1, "Safety body."),
            _element(
                "title-operation-a",
                1,
                "2 Long operation",
                element_type=ElementType.TITLE,
            ),
            _element(
                "title-operation-b",
                1,
                "heading",
                element_type=ElementType.TITLE,
            ),
            _element("operation-body", 1, "Operation body."),
        ]
    )

    result = DigitalOutlineProfile(content_adapter=adapter).run(
        path,
        run_id="run-normalized-anchors",
        assets_dir=tmp_path / "assets",
    )

    first, second = result.manual.content
    assert isinstance(first, Chapter)
    assert isinstance(second, Chapter)
    assert [item.id for item in first.content if isinstance(item, ManualElement)] == [
        "title-safety",
        "safety-body",
    ]
    assert [item.id for item in second.content if isinstance(item, ManualElement)] == [
        "title-operation-a",
        "title-operation-b",
        "operation-body",
    ]
    normalized = [
        warning
        for warning in result.warnings
        if warning.startswith("outline anchor normalized match:")
    ]
    assert len(normalized) == 2
    assert not any("outline anchor unmatched" in warning for warning in result.warnings)


def test_digital_outline_accepts_container_boundary_defined_by_anchored_child(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manual.pdf"
    document = fitz.open()
    document.new_page(width=200, height=100)
    document.new_page(width=200, height=100)
    document.set_toc(
        [
            [1, "Container", 1],
            [2, "1.1 Anchored child", 1],
            [1, "Next chapter", 2],
        ]
    )
    document.save(path)
    document.close()
    adapter = _StaticContentAdapter(
        [
            _element(
                "title-child",
                1,
                "1.1 Anchored child",
                element_type=ElementType.TITLE,
            ),
            _element("child-body", 1, "Child body."),
            _element("next-body", 2, "Next body."),
        ]
    )

    result = DigitalOutlineProfile(content_adapter=adapter).run(
        path,
        run_id="run-container-anchor",
        assets_dir=tmp_path / "assets",
    )

    assert any(
        warning.startswith("outline container boundary delegated:")
        for warning in result.warnings
    )
    assert not any("outline anchor unmatched" in warning for warning in result.warnings)
    container = result.manual.content[0]
    assert isinstance(container, Chapter)
    child = next(item for item in container.content if isinstance(item, Chapter))
    assert [item.id for item in child.content if isinstance(item, ManualElement)] == [
        "title-child",
        "child-body",
    ]


def test_partial_run_ignores_same_page_anchor_checks_outside_selected_pages(
    tmp_path: Path,
) -> None:
    path = _same_page_outlined_pdf(tmp_path / "manual.pdf")
    adapter = _StaticContentAdapter(
        [_element("chapter-two-body", 2, "Only the selected page is parsed.")]
    )

    result = DigitalOutlineProfile(content_adapter=adapter).run(
        path,
        run_id="run-page-two-only",
        assets_dir=tmp_path / "assets",
        pages=[2],
    )

    assert not any("outline anchor" in warning for warning in result.warnings)
    assert result.manual.metadata.pages_processed == [2]
