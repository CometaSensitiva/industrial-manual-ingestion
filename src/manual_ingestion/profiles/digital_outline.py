"""Chosen V2 pipeline for digital PDFs with an embedded outline."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from manual_ingestion.adapters.docling_adapter import DoclingContentAdapter
from manual_ingestion.adapters.pymupdf_adapter import PyMuPDFOutlineAdapter
from manual_ingestion.models import (
    Chapter,
    DocumentProfile,
    ElementType,
    ManualDocument,
    ManualElement,
    ManualMetadata,
    TocEntry,
)
from manual_ingestion.profiles.base import (
    ContentAdapter,
    OutlineAdapter,
    ProfileExecutionError,
    ProfileResult,
)


@dataclass(slots=True)
class _ChapterWindow:
    chapter: Chapter
    page_end: int
    order: int


@dataclass(slots=True)
class _ChapterAnchors:
    """Resolved reading-order boundaries for outline entries sharing a page."""

    by_chapter_id: dict[str, int]
    pages: set[int]
    warnings: list[str]


@dataclass(frozen=True, slots=True)
class _ApproximateAnchor:
    elements: tuple[ManualElement, ...]
    score: float
    strategy: str


class DigitalOutlineProfile:
    """Use native PDF bookmarks for structure and Docling for page content."""

    profile = DocumentProfile.DIGITAL_OUTLINE
    structure_strategy = "embedded_outline"

    def __init__(
        self,
        *,
        content_adapter: ContentAdapter | None = None,
        outline_adapter: OutlineAdapter | None = None,
    ) -> None:
        self.content_adapter = content_adapter or DoclingContentAdapter()
        self.outline_adapter = outline_adapter or PyMuPDFOutlineAdapter()

    def run(
        self,
        pdf_path: str | Path,
        *,
        run_id: str,
        assets_dir: str | Path,
        pages: list[int] | None = None,
        title: str | None = None,
        language: str = "en",
    ) -> ProfileResult:
        source_path = Path(pdf_path)
        if not run_id.strip():
            raise ValueError("run_id cannot be empty")

        outline = self.outline_adapter.extract(source_path)
        if outline.pages_total < 1:
            raise ProfileExecutionError("outline adapter reported a document without pages")
        if not outline.entries:
            raise ProfileExecutionError(
                "digital_outline requires a non-empty embedded PDF outline; run detection "
                "and select digital_reconstructed when the outline is absent"
            )
        selected_pages = _select_pages(pages, outline.pages_total)
        _validate_toc(outline.entries, outline.pages_total)

        parsed = self.content_adapter.parse(source_path, selected_pages, Path(assets_dir))
        if sorted(parsed.pages_processed) != selected_pages:
            raise ProfileExecutionError(
                "content adapter did not confirm every requested page: "
                f"requested={selected_pages}, processed={sorted(parsed.pages_processed)}"
            )
        _validate_elements(parsed.elements, selected_pages, outline.pages_total)

        roots, windows = _build_chapters(outline.entries, outline.pages_total)
        ordered_elements = [
            element.model_copy(
                update={
                    "trace": element.trace.model_copy(
                        update={
                            "reading_order": (
                                element.trace.reading_order
                                if element.trace.reading_order is not None
                                else reading_order
                            )
                        }
                    )
                }
            )
            for reading_order, element in enumerate(parsed.elements)
        ]
        anchors = _resolve_same_page_anchors(
            windows,
            ordered_elements,
            selected_pages=selected_pages,
        )
        unassigned: list[ManualElement] = []
        for element in ordered_elements:
            destination = _chapter_for_element(windows, element, anchors)
            parent_id = destination.chapter.id if destination else None
            normalized = element.model_copy(
                update={
                    "trace": element.trace.model_copy(
                        update={"parent_chapter_id": parent_id}
                    )
                }
            )
            if destination is None:
                unassigned.append(normalized)
            else:
                destination.chapter.content.append(normalized)

        for root in roots:
            _sort_chapter_content(root, anchors.by_chapter_id)
        document_content: list[Chapter | ManualElement] = [*roots, *unassigned]
        _sort_content_preserving_chapter_order(
            document_content,
            anchors.by_chapter_id,
        )

        resolved_title = _resolve_title(title, outline.document_title, source_path)
        manual = ManualDocument(
            id=run_id.strip(),
            title=resolved_title,
            source_file=source_path.name,
            language=language,
            metadata=ManualMetadata(
                pages_total=outline.pages_total,
                pages_processed=selected_pages,
                profile=self.profile,
                parser=self.content_adapter.name,
                structure_strategy=self.structure_strategy,
                toc_available=True,
            ),
            content=document_content,
        )
        warnings = [*parsed.warnings, *anchors.warnings]
        if unassigned:
            warnings.append(
                f"{len(unassigned)} element(s) precede the first outline destination and remain at document root"
            )
        return ProfileResult(manual=manual, toc=list(outline.entries), warnings=warnings)


def _select_pages(pages: list[int] | None, pages_total: int) -> list[int]:
    if pages is None:
        return list(range(1, pages_total + 1))
    if not pages:
        raise ValueError("pages cannot be empty")
    if len(pages) != len(set(pages)):
        raise ValueError("pages cannot contain duplicates")
    selected = sorted(pages)
    if selected[0] < 1 or selected[-1] > pages_total:
        raise ValueError(f"pages must be between 1 and {pages_total}")
    return selected


def _validate_toc(entries: list[TocEntry], pages_total: int) -> None:
    for entry in entries:
        if entry.page > pages_total:
            raise ProfileExecutionError(
                f"outline entry {entry.title!r} points outside the document (page {entry.page})"
            )


def _validate_elements(
    elements: list[ManualElement],
    selected_pages: list[int],
    pages_total: int,
) -> None:
    selected = set(selected_pages)
    identifiers: set[str] = set()
    for element in elements:
        if element.id in identifiers:
            raise ProfileExecutionError(f"content adapter returned duplicate element id: {element.id}")
        identifiers.add(element.id)
        if element.page > pages_total or element.page not in selected:
            raise ProfileExecutionError(
                f"element {element.id!r} belongs to unrequested or invalid page {element.page}"
            )


def _build_chapters(
    entries: list[TocEntry],
    pages_total: int,
) -> tuple[list[Chapter], list[_ChapterWindow]]:
    roots: list[Chapter] = []
    stack: list[_ChapterWindow] = []
    windows: list[_ChapterWindow] = []

    for index, entry in enumerate(entries):
        page_end = pages_total
        for following in entries[index + 1 :]:
            if following.level <= entry.level:
                page_end = max(entry.page, following.page - 1)
                break
        chapter = Chapter(
            id=_chapter_id(index, entry.title),
            title=entry.title,
            level=entry.level,
            page=entry.page,
        )
        window = _ChapterWindow(chapter=chapter, page_end=min(page_end, pages_total), order=index)

        while stack and stack[-1].chapter.level >= chapter.level:
            stack.pop()
        if stack:
            stack[-1].chapter.content.append(chapter)
        else:
            roots.append(chapter)
        stack.append(window)
        windows.append(window)

    return roots, windows


def _chapter_for_page(windows: list[_ChapterWindow], page: int) -> _ChapterWindow | None:
    candidates = [window for window in windows if window.chapter.page <= page <= window.page_end]
    if not candidates:
        return None
    return max(candidates, key=lambda window: (window.chapter.level, window.order))


def _resolve_same_page_anchors(
    windows: list[_ChapterWindow],
    elements: list[ManualElement],
    *,
    selected_pages: list[int],
) -> _ChapterAnchors:
    """Match colliding outline entries to unique canonical title elements.

    Page ranges are sufficient when outline entries start on distinct pages.  If
    two or more entries start on one page, however, a page number cannot locate
    the boundary between them.  Exact canonical title matches provide that
    boundary without relying on fuzzy guesses.
    """

    start_counts = Counter(window.chapter.page for window in windows)
    selected_page_set = set(selected_pages)
    anchor_pages = {
        page
        for page, count in start_counts.items()
        if count > 1 and page in selected_page_set
    }
    if not anchor_pages:
        return _ChapterAnchors(by_chapter_id={}, pages=set(), warnings=[])

    titles_by_page: dict[int, dict[str, list[ManualElement]]] = defaultdict(
        lambda: defaultdict(list)
    )
    ordered_titles_by_page: dict[int, list[ManualElement]] = defaultdict(list)
    for element in elements:
        if element.page not in anchor_pages or element.type is not ElementType.TITLE:
            continue
        titles_by_page[element.page][_canonical_title(element.text or "")].append(element)
        ordered_titles_by_page[element.page].append(element)
    for page_titles in ordered_titles_by_page.values():
        page_titles.sort(
            key=lambda element: (
                element.trace.reading_order
                if element.trace.reading_order is not None
                else -1
            )
        )

    entries_by_page_and_title: Counter[tuple[int, str]] = Counter(
        (window.chapter.page, _canonical_title(window.chapter.title))
        for window in windows
        if window.chapter.page in anchor_pages
    )
    anchors: dict[str, int] = {}
    warnings: list[str] = []
    used_title_ids: set[str] = set()
    unresolved: list[_ChapterWindow] = []

    for window in windows:
        page = window.chapter.page
        if page not in anchor_pages:
            continue
        canonical = _canonical_title(window.chapter.title)
        matches = titles_by_page[page].get(canonical, [])
        repeated_entry = entries_by_page_and_title[(page, canonical)] > 1

        if not matches:
            unresolved.append(window)
            continue
        if repeated_entry or len(matches) > 1:
            warnings.append(
                "outline anchor ambiguous: "
                f"chapter {window.chapter.title!r} on page {page} maps to "
                f"{len(matches)} TITLE element(s) across "
                f"{entries_by_page_and_title[(page, canonical)]} outline entry/entries; "
                "no reading-order boundary was inferred"
            )
            continue

        matched = matches[0]
        reading_order = matched.trace.reading_order
        if reading_order is None:  # Defensive: the assembler assigns every order above.
            warnings.append(
                "outline anchor unmatched: "
                f"chapter {window.chapter.title!r} on page {page} has no reading order; "
                "same-page content remains assigned conservatively"
            )
            continue
        anchors[window.chapter.id] = reading_order
        used_title_ids.add(matched.id)

    for window in unresolved:
        page = window.chapter.page
        approximate = _approximate_title_anchor(
            window.chapter.title,
            ordered_titles_by_page.get(page, []),
            used_title_ids=used_title_ids,
        )
        if approximate is None:
            anchored_children = [
                item
                for item in window.chapter.content
                if isinstance(item, Chapter)
                and item.page == page
                and item.id in anchors
            ]
            if anchored_children:
                warnings.append(
                    "outline container boundary delegated: "
                    f"chapter {window.chapter.title!r} on page {page} has no "
                    "standalone TITLE block; anchored child chapters define the "
                    "same-page boundaries"
                )
                continue
            warnings.append(
                "outline anchor unmatched: "
                f"chapter {window.chapter.title!r} on page {page} has no unique "
                "high-confidence TITLE match; same-page content remains assigned "
                "conservatively"
            )
            continue
        reading_order = approximate.elements[0].trace.reading_order
        if reading_order is None:  # Defensive: candidates without an order are filtered.
            warnings.append(
                "outline anchor unmatched: "
                f"chapter {window.chapter.title!r} on page {page} has no reading order; "
                "same-page content remains assigned conservatively"
            )
            continue
        anchors[window.chapter.id] = reading_order
        used_title_ids.update(element.id for element in approximate.elements)
        warnings.append(
            "outline anchor normalized match: "
            f"chapter {window.chapter.title!r} on page {page} matched "
            f"{len(approximate.elements)} TITLE block(s) via {approximate.strategy} "
            f"(score={approximate.score:.3f})"
        )

    windows_by_anchor: dict[tuple[int, int], list[_ChapterWindow]] = defaultdict(list)
    for window in windows:
        anchor = anchors.get(window.chapter.id)
        if anchor is not None:
            windows_by_anchor[(window.chapter.page, anchor)].append(window)
    for (page, reading_order), colliding in windows_by_anchor.items():
        if len(colliding) < 2:
            continue
        titles = ", ".join(repr(window.chapter.title) for window in colliding)
        warnings.append(
            "outline anchor ambiguous: "
            f"chapters {titles} on page {page} share TITLE reading order {reading_order}; "
            "no boundary was inferred for those chapters"
        )
        for window in colliding:
            anchors.pop(window.chapter.id, None)

    resolved_by_page: dict[int, list[tuple[int, int, _ChapterWindow]]] = defaultdict(list)
    for window in windows:
        anchor = anchors.get(window.chapter.id)
        if anchor is not None:
            resolved_by_page[window.chapter.page].append((window.order, anchor, window))
    for page, resolved in resolved_by_page.items():
        outline_order = [anchor for _, anchor, _ in sorted(resolved)]
        if outline_order != sorted(outline_order):
            titles = ", ".join(repr(window.chapter.title) for _, _, window in sorted(resolved))
            warnings.append(
                "outline anchors out of order: "
                f"page {page} TITLE reading order conflicts with outline order for {titles}; "
                "resolved title positions are used as the physical-page authority"
            )

    return _ChapterAnchors(by_chapter_id=anchors, pages=anchor_pages, warnings=warnings)


def _approximate_title_anchor(
    outline_title: str,
    page_titles: list[ManualElement],
    *,
    used_title_ids: set[str],
) -> _ApproximateAnchor | None:
    """Resolve only unique, high-confidence parser variations.

    Docling can omit the leading chapter number, expose private-use glyphs, or
    split a long heading across two TITLE blocks.  Exact matching alone loses
    otherwise obvious same-page boundaries.  Candidate spans are limited to
    one or two consecutive TITLE blocks and competing physical start positions
    must be clearly worse, keeping the fallback deterministic and fail-closed.
    A unique identical numeric section prefix is also authoritative when only
    the trailing fragment of a very long title survives on the page.
    """

    candidates: list[_ApproximateAnchor] = []
    expected = _canonical_title(outline_title)
    for start in range(len(page_titles)):
        for length in (1, 2):
            span = page_titles[start : start + length]
            if len(span) != length:
                continue
            if any(element.id in used_title_ids for element in span):
                continue
            if any(element.trace.reading_order is None for element in span):
                continue
            # Do not stitch across a TITLE block already consumed by an exact
            # match; filtering above also makes every accepted span one-to-one.
            text = " ".join(element.text or "" for element in span)
            score = SequenceMatcher(
                None,
                expected,
                _canonical_title(text),
            ).ratio()
            candidates.append(
                _ApproximateAnchor(
                    elements=tuple(span),
                    score=score,
                    strategy="normalized_title_similarity",
                )
            )

    # Multiple span lengths can share the same physical boundary.  They are
    # alternatives for one anchor, not competing evidence.
    best_by_start: dict[int, _ApproximateAnchor] = {}
    for candidate in candidates:
        reading_order = candidate.elements[0].trace.reading_order
        if reading_order is None:
            continue
        current = best_by_start.get(reading_order)
        if current is None or candidate.score > current.score:
            best_by_start[reading_order] = candidate
    ranked = sorted(best_by_start.values(), key=lambda item: item.score, reverse=True)
    if ranked:
        best = ranked[0]
        runner_up_score = ranked[1].score if len(ranked) > 1 else 0.0
        if best.score >= 0.90 and best.score - runner_up_score >= 0.04:
            return best

    expected_number = _section_number(outline_title)
    if expected_number is None:
        return None
    numbered = [
        element
        for element in page_titles
        if element.id not in used_title_ids
        if _section_number(element.text or "") == expected_number
        and element.trace.reading_order is not None
    ]
    if len(numbered) != 1:
        return None
    element = numbered[0]
    score = SequenceMatcher(
        None,
        expected,
        _canonical_title(element.text or ""),
    ).ratio()
    return _ApproximateAnchor(
        elements=(element,),
        score=score,
        strategy="unique_section_number",
    )


def _section_number(title: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", title).strip()
    match = re.match(r"^(\d+(?:\.\d+)*)\b", normalized)
    return match.group(1) if match else None


def _chapter_for_element(
    windows: list[_ChapterWindow],
    element: ManualElement,
    anchors: _ChapterAnchors,
) -> _ChapterWindow | None:
    if element.page not in anchors.pages:
        return _chapter_for_page(windows, element.page)

    reading_order = element.trace.reading_order
    activated = [
        window
        for window in windows
        if window.chapter.page == element.page
        and (anchor := anchors.by_chapter_id.get(window.chapter.id)) is not None
        and reading_order is not None
        and anchor <= reading_order
    ]
    if activated:
        return max(
            activated,
            key=lambda window: (anchors.by_chapter_id[window.chapter.id], window.order),
        )

    # Before the first trustworthy title boundary, retain only the chapter that
    # was already active on a previous page.  Assigning the element to a new,
    # unresolved same-page entry would claim precision the source does not give.
    prior_candidates = [
        window
        for window in windows
        if window.chapter.page < element.page <= window.page_end
    ]
    if not prior_candidates:
        return None
    return max(prior_candidates, key=lambda window: (window.chapter.level, window.order))


def _chapter_id(index: int, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:48]
    suffix = f"-{slug}" if slug else ""
    return f"chapter-{index + 1:04d}{suffix}"


def _sort_chapter_content(chapter: Chapter, anchors: dict[str, int]) -> None:
    for item in chapter.content:
        if isinstance(item, Chapter):
            _sort_chapter_content(item, anchors)
    _sort_content_preserving_chapter_order(chapter.content, anchors)


def _sort_content_preserving_chapter_order(
    items: list[Chapter | ManualElement],
    anchors: dict[str, int],
) -> None:
    """Interleave elements without changing authoritative outline order.

    Exact title anchors locate same-page chapter boundaries.  Real parsers can
    still omit one title or expose title blocks in an order that conflicts with
    the embedded outline.  Sorting directly on those raw anchors could then
    swap sibling chapters, making ``manual.json`` contradict ``toc.json``.

    Clamp each sibling chapter's effective anchor monotonically per page.  This
    retains useful element interleaving while keeping the embedded outline as
    the structural authority.  Equal keys are safe because Python's sort is
    stable and chapters enter this function in outline order.
    """

    effective_anchors: dict[str, int] = {}
    previous_by_page: dict[int, int] = {}
    for item in items:
        if not isinstance(item, Chapter):
            continue
        previous = previous_by_page.get(item.page)
        raw = anchors.get(item.id)
        if raw is None:
            effective = previous if previous is not None else -1
        else:
            effective = raw if previous is None else max(previous, raw)
        effective_anchors[item.id] = effective
        previous_by_page[item.page] = effective

    items.sort(
        key=lambda item: _content_sort_key(
            item,
            anchors,
            effective_anchors=effective_anchors,
        )
    )


def _content_sort_key(
    item: Chapter | ManualElement,
    anchors: dict[str, int],
    *,
    effective_anchors: dict[str, int] | None = None,
) -> tuple[int, int, int, int]:
    if isinstance(item, Chapter):
        anchor = (
            effective_anchors.get(item.id)
            if effective_anchors is not None
            else anchors.get(item.id)
        )
        if anchor is None:
            return (item.page, 0, 0, 0)
        return (item.page, 1, anchor, 0)
    reading_order = item.trace.reading_order if item.trace.reading_order is not None else 0
    return (item.page, 1, reading_order, 1)


def _canonical_title(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).casefold()
    normalized = re.sub(r"[^\w\s]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _resolve_title(explicit: str | None, metadata_title: str | None, source_path: Path) -> str:
    for candidate in (explicit, metadata_title, source_path.stem):
        if candidate and candidate.strip():
            return candidate.strip()
    raise ProfileExecutionError("manual title could not be derived")
