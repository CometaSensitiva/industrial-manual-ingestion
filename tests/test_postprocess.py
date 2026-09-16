from __future__ import annotations

import math

import pytest

from manual_ingestion.models import (
    BoundingBox,
    CaptionProvenance,
    Chapter,
    DocumentProfile,
    ElementType,
    ManualDocument,
    ManualElement,
    ManualMetadata,
    PageSize,
    SourceReference,
    TraceMetadata,
)
from manual_ingestion.postprocess import (
    SMALL_VISUAL_EXCLUSION_REASON,
    VISUAL_CROP_FILTER_STRATEGY,
    VisualCropDecisionStatus,
    filter_small_visual_crops,
)


def test_filter_excludes_only_visuals_strictly_below_threshold() -> None:
    manual = _manual(
        _visual("small", ElementType.IMAGE, width=10, height=10),
        _visual("boundary", ElementType.TABLE, width=20, height=10),
        _visual("large", ElementType.IMAGE, width=50, height=50),
        _text(),
    )

    filtered, report = filter_small_visual_crops(manual)

    by_id = _elements_by_id(filtered)
    assert by_id["small"].trace.include_in_rag is False
    assert by_id["small"].trace.exclusion_reason == SMALL_VISUAL_EXCLUSION_REASON
    assert by_id["boundary"].trace.include_in_rag is True
    assert by_id["large"].trace.include_in_rag is True
    assert by_id["text"].trace.include_in_rag is True
    assert [(decision.element_id, decision.status) for decision in report.decisions] == [
        ("small", VisualCropDecisionStatus.EXCLUDED),
        ("boundary", VisualCropDecisionStatus.KEPT),
        ("large", VisualCropDecisionStatus.KEPT),
    ]
    assert report.decisions[0].area_fraction == pytest.approx(0.01)
    assert report.strategy == VISUAL_CROP_FILTER_STRATEGY
    assert report.decisions[1].area_fraction == pytest.approx(0.02)
    assert (report.excluded, report.kept, report.preserved, report.unassessed) == (
        1,
        2,
        0,
        0,
    )


def test_filter_is_non_mutating_and_preserves_caption_and_provenance() -> None:
    visual = _visual("small", ElementType.IMAGE, width=10, height=10)
    visual.caption_original = "Pannello"
    visual.caption_generated = "Descrizione tecnica"
    visual.caption_provenance = CaptionProvenance(
        provider="provider",
        model="model",
        prompt_version="v1",
        input_sha256="a" * 64,
    )
    manual = _manual(visual)
    original_dump = manual.model_dump()

    filtered, _ = filter_small_visual_crops(manual)
    filtered_visual = _elements_by_id(filtered)["small"]

    assert manual.model_dump() == original_dump
    assert filtered is not manual
    assert filtered_visual is not visual
    assert filtered_visual.caption_original == visual.caption_original
    assert filtered_visual.caption_generated == visual.caption_generated
    assert filtered_visual.caption_provenance == visual.caption_provenance


@pytest.mark.parametrize("missing", ["bbox", "page_size"])
def test_filter_leaves_visual_unassessed_when_geometry_is_missing(missing: str) -> None:
    visual = _visual("visual", ElementType.TABLE, width=10, height=10)
    update = {missing: None}
    visual.source = visual.source.model_copy(update=update)
    manual = _manual(visual)

    filtered, report = filter_small_visual_crops(manual)

    assert _elements_by_id(filtered)["visual"].trace.include_in_rag is True
    assert report.decisions[0].status is VisualCropDecisionStatus.UNASSESSED
    assert report.decisions[0].area_fraction is None
    assert report.unassessed == 1


def test_filter_preserves_a_pre_existing_exclusion_and_reason() -> None:
    visual = _visual("excluded", ElementType.IMAGE, width=5, height=5)
    visual.trace = TraceMetadata(
        include_in_rag=False,
        exclusion_reason="printed table-of-contents source page",
    )
    manual = _manual(visual)

    filtered, report = filter_small_visual_crops(manual)
    filtered_visual = _elements_by_id(filtered)["excluded"]

    assert filtered_visual.trace.include_in_rag is False
    assert filtered_visual.trace.exclusion_reason == "printed table-of-contents source page"
    assert report.decisions[0].status is VisualCropDecisionStatus.PRESERVED
    assert report.preserved == 1


def test_filter_is_idempotent_for_output_and_report() -> None:
    once, first_report = filter_small_visual_crops(
        _manual(_visual("small", ElementType.IMAGE, width=10, height=10))
    )

    twice, second_report = filter_small_visual_crops(once)

    assert twice == once
    assert second_report == first_report


@pytest.mark.parametrize("threshold", [0, 1, -0.1, 1.1, math.nan, math.inf, True])
def test_filter_rejects_invalid_threshold(threshold: float) -> None:
    with pytest.raises(ValueError, match="0 < threshold < 1"):
        filter_small_visual_crops(_manual(_visual("visual", ElementType.IMAGE)), threshold=threshold)


def _manual(*elements: ManualElement) -> ManualDocument:
    return ManualDocument(
        id="manual-1",
        title="Manuale",
        source_file="manual.pdf",
        metadata=ManualMetadata(
            pages_total=1,
            pages_processed=[1],
            profile=DocumentProfile.DIGITAL_OUTLINE,
            parser="test",
            structure_strategy="test",
            toc_available=True,
        ),
        content=[
            Chapter(
                id="chapter-1",
                title="Capitolo",
                level=1,
                page=1,
                content=list(elements),
            )
        ],
    )


def _visual(
    identifier: str,
    element_type: ElementType,
    *,
    width: float = 50,
    height: float = 50,
) -> ManualElement:
    source = SourceReference(
        page=1,
        page_size=PageSize(width=100, height=100),
        bbox=BoundingBox(x0=0, y0=0, x1=width, y1=height),
    )
    if element_type is ElementType.IMAGE:
        return ManualElement(
            id=identifier,
            type=element_type,
            page=1,
            source=source,
            image_path=f"assets/images/{identifier}.png",
        )
    return ManualElement(
        id=identifier,
        type=element_type,
        page=1,
        source=source,
        table_image_path=f"assets/tables/{identifier}.png",
    )


def _text() -> ManualElement:
    return ManualElement(
        id="text",
        type=ElementType.TEXT,
        page=1,
        source=SourceReference(page=1),
        text="Testo",
    )


def _elements_by_id(manual: ManualDocument) -> dict[str, ManualElement]:
    chapter = manual.content[0]
    assert isinstance(chapter, Chapter)
    return {
        element.id: element
        for element in chapter.content
        if isinstance(element, ManualElement)
    }
