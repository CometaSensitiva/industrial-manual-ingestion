"""Shared deterministic post-processing for canonical manual documents."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from .models import Chapter, ElementType, ManualDocument, ManualElement

DEFAULT_MIN_VISUAL_AREA_FRACTION = 0.02
SMALL_VISUAL_EXCLUSION_REASON = "visual crop below page-area threshold"
VISUAL_CROP_FILTER_STRATEGY = "bbox_area_fraction_v1"
VISUAL_CROP_KEPT_REASON = "crop area meets page-area threshold"
VISUAL_CROP_UNASSESSED_REASON = "missing bbox or page_size"


class VisualCropDecisionStatus(StrEnum):
    """Possible outcomes for one visual element."""

    EXCLUDED = "excluded"
    KEPT = "kept"
    PRESERVED = "preserved"
    UNASSESSED = "unassessed"


@dataclass(frozen=True)
class VisualCropDecision:
    """Traceable crop-filter decision for one image or table."""

    element_id: str
    element_type: ElementType
    status: VisualCropDecisionStatus
    area_fraction: float | None
    reason: str


@dataclass(frozen=True)
class VisualCropFilterReport:
    """Typed summary of every visual element considered by the filter."""

    strategy: str
    threshold: float
    decisions: tuple[VisualCropDecision, ...]

    @property
    def excluded(self) -> int:
        return sum(
            decision.status is VisualCropDecisionStatus.EXCLUDED
            for decision in self.decisions
        )

    @property
    def kept(self) -> int:
        return sum(
            decision.status is VisualCropDecisionStatus.KEPT
            for decision in self.decisions
        )

    @property
    def preserved(self) -> int:
        return sum(
            decision.status is VisualCropDecisionStatus.PRESERVED
            for decision in self.decisions
        )

    @property
    def unassessed(self) -> int:
        return sum(
            decision.status is VisualCropDecisionStatus.UNASSESSED
            for decision in self.decisions
        )


def filter_small_visual_crops(
    manual: ManualDocument,
    *,
    threshold: float = DEFAULT_MIN_VISUAL_AREA_FRACTION,
) -> tuple[ManualDocument, VisualCropFilterReport]:
    """Return a copy with undersized image/table crops excluded from retrieval.

    The decision uses ``bbox area / page area`` and excludes only ratios strictly
    below ``threshold``. Elements remain in the document, and existing exclusions
    are never overwritten.
    """

    _validate_threshold(threshold)
    filtered = manual.model_copy(deep=True)
    decisions: list[VisualCropDecision] = []

    for element in _walk_elements(filtered.content):
        if element.type not in {ElementType.IMAGE, ElementType.TABLE}:
            continue

        area_fraction = _area_fraction(element)

        if not element.trace.include_in_rag:
            current_reason = element.trace.exclusion_reason or "pre-existing exclusion"
            status = (
                VisualCropDecisionStatus.EXCLUDED
                if current_reason == SMALL_VISUAL_EXCLUSION_REASON
                and area_fraction is not None
                and area_fraction < threshold
                else VisualCropDecisionStatus.PRESERVED
            )
            decisions.append(
                VisualCropDecision(
                    element_id=element.id,
                    element_type=element.type,
                    status=status,
                    area_fraction=area_fraction,
                    reason=current_reason,
                )
            )
            continue

        if area_fraction is None:
            decisions.append(
                VisualCropDecision(
                    element_id=element.id,
                    element_type=element.type,
                    status=VisualCropDecisionStatus.UNASSESSED,
                    area_fraction=None,
                    reason=VISUAL_CROP_UNASSESSED_REASON,
                )
            )
            continue

        if area_fraction < threshold:
            element.trace.include_in_rag = False
            element.trace.exclusion_reason = SMALL_VISUAL_EXCLUSION_REASON
            status = VisualCropDecisionStatus.EXCLUDED
            reason = SMALL_VISUAL_EXCLUSION_REASON
        else:
            status = VisualCropDecisionStatus.KEPT
            reason = VISUAL_CROP_KEPT_REASON

        decisions.append(
            VisualCropDecision(
                element_id=element.id,
                element_type=element.type,
                status=status,
                area_fraction=area_fraction,
                reason=reason,
            )
        )

    return filtered, VisualCropFilterReport(
        strategy=VISUAL_CROP_FILTER_STRATEGY,
        threshold=threshold,
        decisions=tuple(decisions),
    )


def _validate_threshold(value: float) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or not 0 < value < 1:
        raise ValueError("threshold must satisfy 0 < threshold < 1")


def _area_fraction(element: ManualElement) -> float | None:
    bbox = element.source.bbox
    page_size = element.source.page_size
    if bbox is None or page_size is None:
        return None
    crop_area = (bbox.x1 - bbox.x0) * (bbox.y1 - bbox.y0)
    return crop_area / (page_size.width * page_size.height)


def _walk_elements(items: list[Chapter | ManualElement]) -> list[ManualElement]:
    elements: list[ManualElement] = []
    for item in items:
        if isinstance(item, Chapter):
            elements.extend(_walk_elements(item.content))
        else:
            elements.append(item)
    return elements
