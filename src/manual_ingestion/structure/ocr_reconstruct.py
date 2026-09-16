"""Deterministic TOC reconstruction from canonical scanned-OCR elements."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from difflib import SequenceMatcher
from typing import Any, Literal

from manual_ingestion.models import ElementType, ManualElement, TocEntry

from .models import TocReconstructionError


OCR_STRUCTURE_STRATEGY = "ocr_printed_toc_title_fusion_v1"
OCR_FALLBACK_STRATEGY = "ocr_structural_titles_fallback_v1"

_TOC_KEYWORDS = ("index", "indice", "contents", "table of contents", "sommario")
_PAGE_REFERENCE = r"(?:[A-Za-z]+-?)?\d{1,4}"
_DOT_ROW = re.compile(
    rf"^\s*(?P<title>.+?)\s*(?:\.{{2,}}|…{{2,}})\s*(?P<page_ref>{_PAGE_REFERENCE})\s*$"
)
_PAGE_REFERENCE_ONLY = re.compile(rf"^{_PAGE_REFERENCE}$")
_TABLE_SEPARATOR = re.compile(r"^:?-{2,}:?$")


class OcrTocReconstructionError(TocReconstructionError):
    """Raised when canonical OCR elements cannot yield usable structure."""


@dataclass(frozen=True)
class OcrReconstructionConfig:
    """Persisted generic thresholds for the single scanned-manual strategy."""

    toc_following_page_window: int = 10
    min_rows_without_keyword: int = 3
    min_offset_evidence: int = 2
    title_match_threshold: float = 0.72
    repeated_header_min_pages: int = 3
    repeated_header_fraction: float = 0.5
    printed_confidence: float = 0.9
    fallback_confidence: float = 0.55
    min_resolved_ratio: float = 0.8

    def __post_init__(self) -> None:
        if self.toc_following_page_window < 0:
            raise ValueError("toc_following_page_window must be non-negative")
        if self.min_rows_without_keyword < 1:
            raise ValueError("min_rows_without_keyword must be positive")
        if self.min_offset_evidence < 2:
            raise ValueError("min_offset_evidence must be at least two")
        if self.repeated_header_min_pages < 2:
            raise ValueError("repeated_header_min_pages must be at least two")
        for name, value in (
            ("title_match_threshold", self.title_match_threshold),
            ("repeated_header_fraction", self.repeated_header_fraction),
            ("printed_confidence", self.printed_confidence),
            ("fallback_confidence", self.fallback_confidence),
            ("min_resolved_ratio", self.min_resolved_ratio),
        ):
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.fallback_confidence >= self.printed_confidence:
            raise ValueError("fallback_confidence must be lower than printed_confidence")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OcrPrintedRow:
    index: int
    level: int
    title: str
    page_ref: str
    printed_page: int
    source_page: int
    source_element_id: str
    source_kind: Literal["text", "table"]
    raw_text: str
    resolved_page: int | None = None
    resolution: str | None = None


@dataclass(frozen=True)
class OcrHeadingCandidate:
    index: int
    source_index: int
    element_id: str
    title: str
    normalized_title: str
    page: int
    confidence: float
    source_heading_level: int | None


@dataclass(frozen=True)
class OcrRejectedTitle:
    source_index: int
    element_id: str
    title: str
    page: int
    reason: str


@dataclass(frozen=True)
class OcrOffsetEvidence:
    row_index: int
    heading_index: int
    title: str
    printed_page: int
    pdf_page: int
    offset: int


@dataclass(frozen=True)
class OcrFusionMatch:
    row_index: int
    heading_index: int | None
    score: float
    decision: Literal["confirmed", "offset_only", "heading_only", "unresolved"]
    page: int | None
    confidence: float
    reason: str


@dataclass(frozen=True)
class OcrReconstructionDiagnostics:
    strategy: str
    mode: Literal["printed_index", "title_fallback"]
    overall_confidence: float
    config: dict[str, Any]
    detected_toc_pages: tuple[int, ...]
    printed_rows: tuple[OcrPrintedRow, ...]
    heading_candidates: tuple[OcrHeadingCandidate, ...]
    rejected_titles: tuple[OcrRejectedTitle, ...]
    inferred_offset: int | None
    offset_evidence: tuple[OcrOffsetEvidence, ...]
    matches: tuple[OcrFusionMatch, ...]
    warnings: tuple[str, ...]
    entry_count: int
    resolved_ratio: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "mode": self.mode,
            "overall_confidence": self.overall_confidence,
            "config": self.config,
            "detected_toc_pages": list(self.detected_toc_pages),
            "printed_rows": [asdict(row) for row in self.printed_rows],
            "heading_candidates": [asdict(candidate) for candidate in self.heading_candidates],
            "rejected_titles": [asdict(candidate) for candidate in self.rejected_titles],
            "inferred_offset": self.inferred_offset,
            "offset_evidence": [asdict(evidence) for evidence in self.offset_evidence],
            "matches": [asdict(match) for match in self.matches],
            "warnings": list(self.warnings),
            "entry_count": self.entry_count,
            "resolved_ratio": self.resolved_ratio,
        }


@dataclass(frozen=True)
class OcrTocReconstructionResult:
    entries: tuple[TocEntry, ...]
    diagnostics: OcrReconstructionDiagnostics


@dataclass(frozen=True)
class _RawPrintedRow:
    title: str
    page_ref: str
    printed_page: int
    source_page: int
    source_element_id: str
    source_kind: Literal["text", "table"]
    raw_text: str


def reconstruct_toc_from_ocr_elements(
    elements: list[ManualElement],
    *,
    pages_total: int,
    config: OcrReconstructionConfig | None = None,
) -> OcrTocReconstructionResult:
    """Recover document structure without depending on raw OCR-engine output."""

    if pages_total < 1:
        raise ValueError("pages_total must be positive")
    cfg = config or OcrReconstructionConfig()
    ordered = _ordered_elements(elements, pages_total)
    toc_pages = _detect_printed_toc_pages(ordered, cfg)
    rows = _printed_rows(ordered, toc_pages)
    headings, rejected = _heading_candidates(
        ordered,
        excluded_pages=set(toc_pages),
        pages_total=pages_total,
        config=cfg,
    )

    if toc_pages:
        if not rows:
            raise OcrTocReconstructionError(
                f"Printed index detected on pages {toc_pages}, but no usable TOC rows were parsed"
            )
        adjusted_rows, inferred_offset, offset_evidence = _apply_exact_title_offset(
            rows,
            headings,
            pages_total=pages_total,
            min_evidence=cfg.min_offset_evidence,
        )
        entries, matches, warnings = _fuse_rows(
            adjusted_rows,
            headings,
            pages_total=pages_total,
            config=cfg,
        )
        if not entries:
            raise OcrTocReconstructionError(
                "Printed index rows were parsed, but none resolved to a valid PDF page"
            )
        resolved_ratio = len(entries) / len(adjusted_rows)
        if resolved_ratio < cfg.min_resolved_ratio:
            raise OcrTocReconstructionError(
                "Printed index resolution is below the configured trust threshold: "
                f"{len(entries)}/{len(adjusted_rows)} rows "
                f"({resolved_ratio:.1%}) < {cfg.min_resolved_ratio:.1%}"
            )
        diagnostics = OcrReconstructionDiagnostics(
            strategy=OCR_STRUCTURE_STRATEGY,
            mode="printed_index",
            overall_confidence=round(cfg.printed_confidence * resolved_ratio, 4),
            config=cfg.to_dict(),
            detected_toc_pages=tuple(toc_pages),
            printed_rows=tuple(adjusted_rows),
            heading_candidates=tuple(headings),
            rejected_titles=tuple(rejected),
            inferred_offset=inferred_offset,
            offset_evidence=tuple(offset_evidence),
            matches=tuple(matches),
            warnings=tuple(warnings),
            entry_count=len(entries),
            resolved_ratio=resolved_ratio,
        )
        return OcrTocReconstructionResult(entries=tuple(entries), diagnostics=diagnostics)

    if not headings:
        raise OcrTocReconstructionError(
            "No printed index and no usable structural title elements were found"
        )
    fallback_entries = _fallback_entries(headings, cfg.fallback_confidence)
    if not fallback_entries:
        raise OcrTocReconstructionError("Structural title fallback produced no usable entries")
    fallback_warning = (
        "No printed index detected; structure uses lower-confidence OCR title-element fallback."
    )
    diagnostics = OcrReconstructionDiagnostics(
        strategy=OCR_FALLBACK_STRATEGY,
        mode="title_fallback",
        overall_confidence=cfg.fallback_confidence,
        config=cfg.to_dict(),
        detected_toc_pages=(),
        printed_rows=(),
        heading_candidates=tuple(headings),
        rejected_titles=tuple(rejected),
        inferred_offset=None,
        offset_evidence=(),
        matches=(),
        warnings=(fallback_warning,),
        entry_count=len(fallback_entries),
        resolved_ratio=None,
    )
    return OcrTocReconstructionResult(entries=tuple(fallback_entries), diagnostics=diagnostics)


def _ordered_elements(elements: list[ManualElement], pages_total: int) -> list[ManualElement]:
    for element in elements:
        if element.page > pages_total:
            raise OcrTocReconstructionError(
                f"Element {element.id!r} points outside the document: page {element.page} > {pages_total}"
            )

    def key(element: ManualElement) -> tuple[int, int, float, str]:
        reading_order = element.trace.reading_order
        bbox_y = element.source.bbox.y0 if element.source.bbox is not None else math.inf
        return (
            element.page,
            reading_order if reading_order is not None else 1_000_000_000,
            bbox_y,
            element.id,
        )

    return sorted(elements, key=key)


def _detect_printed_toc_pages(
    elements: list[ManualElement],
    config: OcrReconstructionConfig,
) -> list[int]:
    keyword_pages: set[int] = set()
    row_counts: Counter[int] = Counter()
    for element in elements:
        text = _element_text(element)
        if text and _is_toc_furniture(text):
            keyword_pages.add(element.page)
        row_counts[element.page] += len(_raw_rows_from_element(element))

    if keyword_pages:
        first_keyword = min(keyword_pages)
        last_following = first_keyword + config.toc_following_page_window
        detected = set(keyword_pages)
        detected.update(
            page
            for page, count in row_counts.items()
            if count > 0 and first_keyword <= page <= last_following
        )
        return sorted(detected)
    return sorted(
        page
        for page, count in row_counts.items()
        if count >= config.min_rows_without_keyword
    )


def _printed_rows(
    elements: list[ManualElement],
    toc_pages: list[int],
) -> list[OcrPrintedRow]:
    if not toc_pages:
        return []
    toc_page_set = set(toc_pages)
    raw_rows: list[_RawPrintedRow] = []
    for element in elements:
        if element.page in toc_page_set:
            raw_rows.extend(_raw_rows_from_element(element))

    deduped: list[_RawPrintedRow] = []
    seen: set[tuple[str, str]] = set()
    for row in raw_rows:
        key = (normalize_ocr_toc_title(row.title), row.page_ref.casefold())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    levels = _relative_levels([row.title for row in deduped])
    return [
        OcrPrintedRow(
            index=index,
            level=levels[index],
            title=row.title,
            page_ref=row.page_ref,
            printed_page=row.printed_page,
            source_page=row.source_page,
            source_element_id=row.source_element_id,
            source_kind=row.source_kind,
            raw_text=row.raw_text,
        )
        for index, row in enumerate(deduped)
    ]


def _raw_rows_from_element(element: ManualElement) -> list[_RawPrintedRow]:
    if element.type is ElementType.TABLE:
        if not element.table_markdown:
            return []
        return _rows_from_markdown_table(element.table_markdown, element)
    if element.type not in {ElementType.TEXT, ElementType.TITLE} or not element.text:
        return []

    rows: list[_RawPrintedRow] = []
    for raw_line in element.text.splitlines():
        match = _DOT_ROW.match(_normalize_space(raw_line))
        if match is None:
            continue
        parsed = _raw_row(
            title=match.group("title"),
            page_ref=match.group("page_ref"),
            raw_text=raw_line,
            element=element,
            source_kind="text",
        )
        if parsed is not None:
            rows.append(parsed)
    return rows


def _rows_from_markdown_table(
    markdown: str,
    element: ManualElement,
) -> list[_RawPrintedRow]:
    rows: list[_RawPrintedRow] = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if "|" not in line:
            continue
        cells = [_normalize_space(cell) for cell in line.strip("|").split("|")]
        cells = [cell for cell in cells if cell]
        if len(cells) < 2 or all(_TABLE_SEPARATOR.fullmatch(cell) for cell in cells):
            continue
        page_ref = cells[-1]
        if _PAGE_REFERENCE_ONLY.fullmatch(page_ref) is None:
            continue
        title = " ".join(cells[:-1])
        parsed = _raw_row(
            title=title,
            page_ref=page_ref,
            raw_text=raw_line,
            element=element,
            source_kind="table",
        )
        if parsed is not None:
            rows.append(parsed)
    return rows


def _raw_row(
    *,
    title: str,
    page_ref: str,
    raw_text: str,
    element: ManualElement,
    source_kind: Literal["text", "table"],
) -> _RawPrintedRow | None:
    clean_title = _clean_title(title)
    if not clean_title or _is_toc_furniture(clean_title) or len(clean_title) > 180:
        return None
    printed_match = re.search(r"\d{1,4}$", page_ref)
    if printed_match is None:
        return None
    return _RawPrintedRow(
        title=clean_title,
        page_ref=page_ref,
        printed_page=int(printed_match.group()),
        source_page=element.page,
        source_element_id=element.id,
        source_kind=source_kind,
        raw_text=_normalize_space(raw_text),
    )


def _heading_candidates(
    elements: list[ManualElement],
    *,
    excluded_pages: set[int],
    pages_total: int,
    config: OcrReconstructionConfig,
) -> tuple[list[OcrHeadingCandidate], list[OcrRejectedTitle]]:
    title_sources: list[tuple[int, ManualElement, str, str]] = []
    rejected: list[OcrRejectedTitle] = []
    for source_index, element in enumerate(elements):
        if element.type is not ElementType.TITLE or element.page in excluded_pages:
            continue
        title = _clean_title(element.text or "")
        normalized = normalize_ocr_toc_title(title)
        if (element.trace.role or "").casefold() in {
            "figure_title",
            "figure_caption",
            "image_caption",
            "table_title",
            "table_caption",
        }:
            rejected.append(
                OcrRejectedTitle(source_index, element.id, title, element.page, "visual_caption")
            )
            continue
        if not title or _is_toc_furniture(title):
            rejected.append(
                OcrRejectedTitle(source_index, element.id, title, element.page, "furniture_or_empty")
            )
            continue
        if not _looks_structural_title(title):
            rejected.append(
                OcrRejectedTitle(source_index, element.id, title, element.page, "not_structural")
            )
            continue
        title_sources.append((source_index, element, title, normalized))

    pages_by_title: dict[str, set[int]] = defaultdict(set)
    for _, element, _, normalized in title_sources:
        pages_by_title[normalized].add(element.page)
    title_pages = {element.page for _, element, _, _ in title_sources}
    body_pages = max(1, min(pages_total - len(excluded_pages), len(title_pages)))
    repeated_threshold = max(
        config.repeated_header_min_pages,
        math.ceil(body_pages * config.repeated_header_fraction),
    )

    accepted_sources: list[tuple[int, ManualElement, str, str]] = []
    seen: set[tuple[int, str]] = set()
    for source_index, element, title, normalized in title_sources:
        if len(pages_by_title[normalized]) >= repeated_threshold and not _starts_with_outline_marker(title):
            rejected.append(
                OcrRejectedTitle(source_index, element.id, title, element.page, "repeated_header")
            )
            continue
        key = (element.page, normalized)
        if key in seen:
            rejected.append(
                OcrRejectedTitle(source_index, element.id, title, element.page, "duplicate_on_page")
            )
            continue
        seen.add(key)
        accepted_sources.append((source_index, element, title, normalized))

    candidates = [
        OcrHeadingCandidate(
            index=index,
            source_index=source_index,
            element_id=element.id,
            title=title,
            normalized_title=normalized,
            page=element.page,
            confidence=0.75,
            source_heading_level=element.trace.heading_level,
        )
        for index, (source_index, element, title, normalized) in enumerate(accepted_sources)
    ]
    return candidates, sorted(rejected, key=lambda item: (item.source_index, item.reason))


def _apply_exact_title_offset(
    rows: list[OcrPrintedRow],
    headings: list[OcrHeadingCandidate],
    *,
    pages_total: int,
    min_evidence: int,
) -> tuple[list[OcrPrintedRow], int | None, list[OcrOffsetEvidence]]:
    rows_by_title: dict[str, list[OcrPrintedRow]] = defaultdict(list)
    headings_by_title: dict[str, list[OcrHeadingCandidate]] = defaultdict(list)
    for row in rows:
        rows_by_title[normalize_ocr_toc_title(row.title)].append(row)
    for heading in headings:
        headings_by_title[heading.normalized_title].append(heading)

    evidence: list[OcrOffsetEvidence] = []
    for normalized in sorted(set(rows_by_title) & set(headings_by_title)):
        for row in rows_by_title[normalized]:
            for heading in headings_by_title[normalized]:
                evidence.append(
                    OcrOffsetEvidence(
                        row_index=row.index,
                        heading_index=heading.index,
                        title=row.title,
                        printed_page=row.printed_page,
                        pdf_page=heading.page,
                        offset=heading.page - row.printed_page,
                    )
                )

    title_support: dict[int, set[str]] = defaultdict(set)
    for item in evidence:
        title_support[item.offset].add(normalize_ocr_toc_title(item.title))
    qualified = [
        (offset, len(titles))
        for offset, titles in title_support.items()
        if len(titles) >= min_evidence
    ]
    if not qualified:
        return rows, None, []
    inferred_offset = min(qualified, key=lambda item: (-item[1], abs(item[0]), item[0]))[0]
    selected_evidence = sorted(
        (item for item in evidence if item.offset == inferred_offset),
        key=lambda item: (item.row_index, item.heading_index),
    )

    adjusted: list[OcrPrintedRow] = []
    for row in rows:
        resolved = row.printed_page + inferred_offset
        if 1 <= resolved <= pages_total:
            adjusted.append(
                replace(
                    row,
                    resolved_page=resolved,
                    resolution="exact_title_offset",
                )
            )
        else:
            adjusted.append(row)
    return adjusted, inferred_offset, selected_evidence


def _fuse_rows(
    rows: list[OcrPrintedRow],
    headings: list[OcrHeadingCandidate],
    *,
    pages_total: int,
    config: OcrReconstructionConfig,
) -> tuple[list[TocEntry], list[OcrFusionMatch], list[str]]:
    matches: list[OcrFusionMatch] = []
    warnings: list[str] = []
    entries: list[TocEntry] = []
    seen: set[tuple[int, str, int]] = set()

    for row in rows:
        heading, score = _best_heading(row, headings)
        page: int | None
        confidence: float
        if heading is not None and score >= config.title_match_threshold:
            page = heading.page
            confidence = 0.96 if row.resolved_page == heading.page else 0.82
            decision: Literal["confirmed", "offset_only", "heading_only", "unresolved"] = (
                "confirmed" if row.resolved_page == heading.page else "heading_only"
            )
            reason = (
                "title_match_confirms_inferred_offset"
                if decision == "confirmed"
                else "title_match_resolves_row"
            )
        elif row.resolved_page is not None:
            page = row.resolved_page
            confidence = 0.84
            decision = "offset_only"
            reason = "consensus_offset_resolves_printed_page"
        else:
            page = None
            confidence = 0.0
            decision = "unresolved"
            reason = "no_consensus_offset_or_matching_body_title"

        if page is not None and not 1 <= page <= pages_total:
            page = None
            confidence = 0.0
            decision = "unresolved"
            reason = "resolved_page_out_of_range"
        matches.append(
            OcrFusionMatch(
                row_index=row.index,
                heading_index=heading.index if heading is not None else None,
                score=round(score, 4),
                decision=decision,
                page=page,
                confidence=confidence,
                reason=reason,
            )
        )
        if page is None:
            warnings.append(
                f"Unresolved OCR TOC row {row.index}: {row.title} -> {row.page_ref}"
            )
            continue
        key = (row.level, normalize_ocr_toc_title(row.title), page)
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            TocEntry(
                level=row.level,
                title=row.title,
                page=page,
                confidence=confidence,
            )
        )
    return entries, matches, warnings


def _best_heading(
    row: OcrPrintedRow,
    headings: list[OcrHeadingCandidate],
) -> tuple[OcrHeadingCandidate | None, float]:
    if not headings:
        return None, 0.0
    row_normalized = normalize_ocr_toc_title(row.title)
    scored: list[tuple[float, int, int, OcrHeadingCandidate]] = []
    for heading in headings:
        score = SequenceMatcher(None, row_normalized, heading.normalized_title).ratio()
        if _leading_marker(row.title) and _leading_marker(row.title) == _leading_marker(heading.title):
            score = min(1.0, score + 0.08)
        distance = abs(heading.page - row.resolved_page) if row.resolved_page is not None else 0
        scored.append((score, -distance, -heading.index, heading))
    best = max(scored, key=lambda item: (item[0], item[1], item[2]))
    return best[3], best[0]


def _fallback_entries(
    headings: list[OcrHeadingCandidate],
    confidence: float,
) -> list[TocEntry]:
    levels = _relative_levels([heading.title for heading in headings])
    entries: list[TocEntry] = []
    seen: set[tuple[int, str, int]] = set()
    for index, heading in enumerate(headings):
        key = (levels[index], heading.normalized_title, heading.page)
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            TocEntry(
                level=levels[index],
                title=heading.title,
                page=heading.page,
                confidence=confidence,
            )
        )
    return entries


def _relative_levels(titles: list[str]) -> list[int]:
    levels: list[int] = []
    inside_roman_section = False
    for title in titles:
        if _is_roman_section(title):
            inside_roman_section = True
            levels.append(1)
            continue
        level = _infer_level(title)
        if inside_roman_section and _starts_with_numeric_marker(title):
            level += 1
        levels.append(level)
    return levels


def _infer_level(title: str) -> int:
    numeric = re.match(r"^\s*(\d+(?:[.\-_]\d+)*)", title)
    if numeric:
        return len(re.split(r"[.\-_]", numeric.group(1)))
    alpha = re.match(r"^\s*([A-Z](?:\.\d+)+)", title)
    if alpha:
        return len(alpha.group(1).split("."))
    return 1


def _element_text(element: ManualElement) -> str:
    if element.type is ElementType.TABLE:
        return element.table_markdown or ""
    return element.text or ""


def _is_toc_furniture(text: str) -> bool:
    clean = _normalize_space(text).casefold().strip(":")
    return clean in _TOC_KEYWORDS


def _looks_structural_title(title: str) -> bool:
    if len(title) < 2 or len(title) > 180:
        return False
    if re.fullmatch(r"[\d.\-_()\[\]\s]+", title):
        return False
    if _starts_with_outline_marker(title):
        return True
    words = title.split()
    if len(words) > 14:
        return False
    if re.search(r"[.!?]\s+\S", title):
        return False
    alpha = [character for character in title if character.isalpha()]
    if not alpha:
        return False
    uppercase_ratio = sum(character.isupper() for character in alpha) / len(alpha)
    return uppercase_ratio >= 0.55 or len(words) <= 10


def _starts_with_outline_marker(title: str) -> bool:
    return _starts_with_numeric_marker(title) or _is_roman_section(title) or bool(
        re.match(r"^\s*[A-Z](?:\.\d+)+\b", title)
    )


def _starts_with_numeric_marker(title: str) -> bool:
    return bool(re.match(r"^\s*\d+(?:[.\-_]\d+)*\b", title))


def _is_roman_section(title: str) -> bool:
    return bool(re.match(r"^\s*[IVXLCDM]+\.(?:\s+\S|$)", title, re.IGNORECASE))


def _leading_marker(title: str) -> str | None:
    match = re.match(
        r"^\s*(\d+(?:[.\-_]\d+)*|[IVXLCDM]+\.)(?=\s|$)",
        title,
        re.IGNORECASE,
    )
    return match.group(1).casefold().rstrip(".") if match else None


def _clean_title(text: str) -> str:
    clean = _normalize_space(text)
    return re.sub(r"(?:\.{2,}|…{2,})\s*$", "", clean).strip()


def normalize_ocr_toc_title(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = re.sub(r"[^\w\s]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _normalize_space(text: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", unicodedata.normalize("NFKC", text)).strip()
