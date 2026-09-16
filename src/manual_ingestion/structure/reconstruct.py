"""Case 2 TOC reconstruction from digital PDF text and layout signals."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import replace
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median

from manual_ingestion.models import TocEntry

from .models import (
    STRUCTURE_STRATEGY,
    DocumentSignals,
    FusionMatch,
    HeadingCandidate,
    PageLine,
    PageNumberMap,
    PageNumberNamespace,
    PrintedTocRow,
    ReconstructionConfig,
    ReconstructionDiagnostics,
    TocReconstructionError,
    TocReconstructionResult,
)
from .page_map import infer_page_number_map, is_header_or_footer, parse_page_ref
from .pdf_source import load_pdf_signals

MAX_BODY_HEADING_CHARACTERS = 180
MAX_PRINTED_TOC_TITLE_CHARACTERS = 400


def reconstruct_toc(
    pdf_path: str | Path,
    config: ReconstructionConfig | None = None,
) -> TocReconstructionResult:
    """Reconstruct a TOC for a digital PDF whose embedded outline is absent."""

    return reconstruct_toc_from_signals(load_pdf_signals(pdf_path), config=config)


def reconstruct_toc_from_signals(
    document: DocumentSignals,
    config: ReconstructionConfig | None = None,
) -> TocReconstructionResult:
    """Pure reconstruction entry point used by the PDF adapter and deterministic tests."""

    cfg = config or ReconstructionConfig()
    if document.outline_entries:
        raise TocReconstructionError(
            "digital_reconstructed received a PDF with an embedded outline; use digital_outline instead"
        )

    page_map = infer_page_number_map(document, cfg)
    toc_pages = detect_printed_toc_pages(document, cfg)
    if not toc_pages:
        raise TocReconstructionError("No printed table-of-contents pages could be detected")

    repeated_furniture = _detect_repeated_toc_furniture(document, toc_pages, cfg)
    rows = parse_printed_toc_pages(
        document,
        toc_pages,
        page_map,
        cfg,
        ignored_lines=repeated_furniture,
    )
    if not rows:
        raise TocReconstructionError(
            "Printed table-of-contents pages were detected, but no usable index rows could be parsed"
        )

    body_start_page = _infer_body_start_page(page_map.namespaces.get(""), toc_pages)
    headings = detect_body_headings(
        document,
        body_start_page=body_start_page,
        skip_pages=set(toc_pages),
        config=cfg,
    )
    entries, matches, warnings = fuse_toc_candidates(
        rows=rows,
        headings=headings,
        page_count=document.page_count,
        config=cfg,
    )
    if not entries:
        raise TocReconstructionError("TOC candidates were found, but none resolved to a valid PDF page")

    diagnostics = ReconstructionDiagnostics(
        strategy=STRUCTURE_STRATEGY,
        config=cfg.to_dict(),
        detected_toc_pages=tuple(toc_pages),
        ignored_repeated_lines=tuple(sorted(repeated_furniture)),
        page_number_map=page_map.to_dict(),
        printed_rows=tuple(rows),
        heading_candidates=tuple(headings),
        matches=tuple(matches),
        warnings=tuple(warnings),
        entry_count=len(entries),
    )
    return TocReconstructionResult(entries=tuple(entries), diagnostics=diagnostics)


def detect_printed_toc_pages(
    document: DocumentSignals,
    config: ReconstructionConfig,
) -> list[int]:
    """Find the highest-scoring contiguous printed-index block."""

    head_end = min(
        document.page_count,
        max(
            config.toc_search_min_pages,
            math.ceil(document.page_count * config.toc_search_fraction),
        ),
    )
    tail_start = max(1, document.page_count - config.toc_search_tail_pages + 1)
    pages_to_search = sorted(
        set(range(1, head_end + 1))
        | set(range(tail_start, document.page_count + 1))
    )

    scored_pages: list[tuple[int, float]] = []
    for page_number in pages_to_search:
        texts = [line.text for line in document.page(page_number).lines]
        first_text = " ".join(texts[:12]).casefold()
        has_keyword = any(keyword.casefold() in first_text for keyword in config.toc_keywords)
        terminal_refs = sum(_line_has_terminal_page_ref(text) for text in texts)
        dot_leaders = sum(bool(re.search(r"\.{3,}", text)) for text in texts)
        outline_markers = sum(_starts_with_outline_marker(text) for text in texts)

        if not has_keyword and terminal_refs < config.min_terminal_refs_without_keyword:
            continue
        score = (8.0 if has_keyword else 0.0) + terminal_refs
        score += min(dot_leaders, 10) * 0.7
        score += min(outline_markers, 12) * 0.2
        if score >= config.min_toc_page_score:
            scored_pages.append((page_number, score))

    if not scored_pages:
        return []

    groups: list[list[tuple[int, float]]] = []
    current: list[tuple[int, float]] = []
    for page_number, score in scored_pages:
        if not current or page_number == current[-1][0] + 1:
            current.append((page_number, score))
        else:
            groups.append(current)
            current = [(page_number, score)]
    if current:
        groups.append(current)

    best = max(
        groups,
        key=lambda group: (
            sum(score for _, score in group),
            len(group),
            -group[0][0],
        ),
    )
    return [page_number for page_number, _ in best]


def parse_printed_toc_pages(
    document: DocumentSignals,
    toc_pages: list[int],
    page_map: PageNumberMap,
    config: ReconstructionConfig,
    *,
    ignored_lines: set[str] | None = None,
) -> list[PrintedTocRow]:
    ignored_lines = ignored_lines or set()
    rows: list[PrintedTocRow] = []
    for page_number in toc_pages:
        lines = [
            line.text
            for line in document.page(page_number).lines
            if normalize_title(line.text) not in ignored_lines
        ]
        rows.extend(
            _parse_printed_toc_page_lines(
                lines,
                source_page=page_number,
                page_map=page_map,
                config=config,
            )
        )

    adjusted = _apply_roman_section_depth(rows, config.max_depth)
    deduped: list[PrintedTocRow] = []
    seen: set[tuple[str, str]] = set()
    for row in adjusted:
        key = (normalize_title(row.title), row.page_ref.casefold())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return [replace(row, index=index) for index, row in enumerate(deduped)]


def detect_body_headings(
    document: DocumentSignals,
    *,
    body_start_page: int,
    skip_pages: set[int],
    config: ReconstructionConfig,
) -> list[HeadingCandidate]:
    body_size = _estimate_body_font_size(document, body_start_page, skip_pages, config)
    candidates: list[HeadingCandidate] = []
    seen: set[tuple[int, str]] = set()

    for page_number in range(max(body_start_page, 1), document.page_count + 1):
        page = document.page(page_number)
        if page_number in skip_pages:
            continue
        for line in page.lines:
            if _is_footer(line, page.height, config.header_footer_fraction):
                continue
            candidate = _heading_candidate_from_line(
                line,
                page_number=page_number,
                page_width=page.width,
                body_size=body_size,
                config=config,
            )
            if candidate is None:
                continue
            key = (page_number, normalize_title(candidate.title))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)

    title_frequency = Counter(normalize_title(candidate.title) for candidate in candidates)
    body_page_count = max(
        1,
        sum(
            page_number not in skip_pages
            for page_number in range(max(body_start_page, 1), document.page_count + 1)
        ),
    )
    repeated_threshold = max(2, body_page_count // 3)
    filtered = [
        candidate
        for candidate in candidates
        if not (
            title_frequency[normalize_title(candidate.title)] > repeated_threshold
            and not _starts_with_outline_marker(candidate.title)
        )
    ]
    return [replace(candidate, index=index) for index, candidate in enumerate(filtered)]


def fuse_toc_candidates(
    *,
    rows: list[PrintedTocRow],
    headings: list[HeadingCandidate],
    page_count: int,
    config: ReconstructionConfig,
) -> tuple[list[TocEntry], list[FusionMatch], list[str]]:
    matches: list[FusionMatch] = []
    warnings: list[str] = []

    for row in rows:
        heading, score = _best_heading_match(row, headings, config)
        if heading is not None and score >= config.match_threshold:
            page = _page_from_row_and_heading(row, heading, config)
            decision = "matched"
            reason = "body_heading_confirms_printed_toc_row"
        elif heading is not None and score >= config.weak_match_threshold:
            page = row.resolved_page if row.resolved_page is not None else heading.page
            decision = "weak_match"
            reason = "printed_toc_row_kept_with_weak_body_confirmation"
        elif row.resolved_page is not None:
            page = row.resolved_page
            decision = "unmatched"
            reason = "printed_toc_row_kept_from_page_mapping"
        else:
            page = None
            decision = "unresolved"
            reason = "no_valid_page_mapping_or_body_heading"
            warnings.append(f"Unresolved TOC row {row.index}: {row.title}")

        if page is not None and not 1 <= page <= page_count:
            warnings.append(f"Rejected out-of-range TOC row {row.index}: {row.title} -> {page}")
            page = None
            decision = "unresolved"
            reason = "resolved_page_out_of_range"

        matches.append(
            FusionMatch(
                row_index=row.index,
                heading_index=heading.index if heading is not None else None,
                score=round(score, 4),
                decision=decision,
                page=page,
                reason=reason,
            )
        )

    entries: list[TocEntry] = []
    seen: set[tuple[int, str, int]] = set()
    rows_by_index = {row.index: row for row in rows}
    for match in matches:
        if match.page is None:
            continue
        row = rows_by_index[match.row_index]
        key = (row.level, normalize_title(row.title), match.page)
        if key in seen:
            continue
        seen.add(key)
        entries.append(TocEntry(level=row.level, title=row.title, page=match.page))
    return entries, matches, warnings


def normalize_title(title: str) -> str:
    text = title.casefold()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w\s]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_printed_toc_page_lines(
    lines: list[str],
    *,
    source_page: int,
    page_map: PageNumberMap,
    config: ReconstructionConfig,
) -> list[PrintedTocRow]:
    clean_lines = [_normalize_space(line) for line in lines if _normalize_space(line)]
    rows: list[PrintedTocRow] = []
    index = 0

    while index < len(clean_lines):
        line = clean_lines[index]
        if _is_printed_toc_furniture(line, config.toc_keywords):
            index += 1
            continue

        direct = _parse_printed_toc_line(
            line,
            source_page=source_page,
            page_map=page_map,
            max_depth=config.max_depth,
        )
        if direct is not None:
            rows.append(direct)
            index += 1
            continue

        if _is_numbering_token(line):
            parts = [line]
            cursor = index + 1
            parsed_numbered = False
            while cursor < min(len(clean_lines), index + 5):
                candidate = clean_lines[cursor]
                if _is_printed_toc_furniture(candidate, config.toc_keywords):
                    cursor += 1
                    continue
                if cursor > index + 1 and (_is_numbering_token(candidate) or _is_roman_section_title(candidate)):
                    break
                parts.append(candidate)
                combined = " ".join(parts)
                parsed = _parse_printed_toc_line(
                    combined,
                    source_page=source_page,
                    page_map=page_map,
                    max_depth=config.max_depth,
                )
                cursor += 1
                if parsed is not None:
                    rows.append(replace(parsed, signals=("split_numbering_token",)))
                    index = cursor
                    parsed_numbered = True
                    break
            if parsed_numbered:
                continue
            index += 1
            continue

        if _is_roman_section_title(line):
            rows.append(
                PrintedTocRow(
                    index=-1,
                    level=1,
                    title=line,
                    page_ref="",
                    printed_page=None,
                    resolved_page=None,
                    source_page=source_page,
                    raw_text=line,
                    confidence=0.76,
                    signals=("roman_section_title",),
                )
            )
            index += 1
            continue

        if _looks_like_title(line):
            parts = [line]
            parsed_wrapped: PrintedTocRow | None = None
            consumed = index + 1
            for cursor in range(index + 1, min(len(clean_lines), index + 4)):
                candidate = clean_lines[cursor]
                if _is_printed_toc_furniture(candidate, config.toc_keywords):
                    break
                parts.append(candidate)
                parsed_wrapped = _parse_printed_toc_line(
                    " ".join(parts),
                    source_page=source_page,
                    page_map=page_map,
                    max_depth=config.max_depth,
                )
                consumed = cursor + 1
                if parsed_wrapped is not None:
                    break
            if parsed_wrapped is not None:
                rows.append(replace(parsed_wrapped, signals=("wrapped_printed_row",)))
                index = consumed
                continue

        index += 1
    return rows


def _parse_printed_toc_line(
    text: str,
    *,
    source_page: int,
    page_map: PageNumberMap,
    max_depth: int,
) -> PrintedTocRow | None:
    raw_text = _normalize_space(text)
    match = re.match(
        r"^(?P<title>.+?)(?:\.{2,}|\s{2,}|\s+)(?P<page_ref>(?:[A-Za-z]{1,12}-?)?\d{1,6})\s*$",
        raw_text,
    )
    if match is None:
        return None

    title = _clean_title(match.group("title"))
    if not _looks_like_toc_title(title):
        return None
    page_ref = match.group("page_ref")
    parsed_ref = parse_page_ref(page_ref)
    if parsed_ref is None:
        return None

    _, printed_page = parsed_ref
    resolved_page = page_map.resolve(page_ref)
    confidence = 0.72
    if resolved_page is not None:
        confidence += 0.12
    if _starts_with_outline_marker(title):
        confidence += 0.12
    if re.search(r"\.{3,}", raw_text):
        confidence += 0.04

    return PrintedTocRow(
        index=-1,
        level=min(max(1, _infer_level(title)), max_depth),
        title=title,
        page_ref=page_ref,
        printed_page=printed_page,
        resolved_page=resolved_page,
        source_page=source_page,
        raw_text=raw_text,
        confidence=min(confidence, 1.0),
        signals=("printed_toc_line",),
    )


def _heading_candidate_from_line(
    line: PageLine,
    *,
    page_number: int,
    page_width: float,
    body_size: float,
    config: ReconstructionConfig,
) -> HeadingCandidate | None:
    text = _clean_title(line.text)
    if not _looks_like_title(text):
        return None

    font_name = (line.font_name or "").casefold()
    numbered = bool(re.match(r"^\d+(?:[.\-_]\d+)*\b", text))
    roman = _is_roman_section_title(text)
    bold = any(marker in font_name for marker in ("bold", "black", "heavy"))
    larger_than_body = line.font_size is not None and line.font_size >= body_size + 0.8
    upper_short = text.isupper() and len(text) <= 100

    score = 0.0
    signals: list[str] = []
    if numbered or roman:
        score += 0.42
        signals.append("numbered_or_roman")
    if larger_than_body:
        score += 0.32
        signals.append("larger_than_body")
    if bold:
        score += 0.2
        signals.append("bold")
    if upper_short:
        score += 0.12
        signals.append("upper_short")
    if line.bbox is not None and line.bbox[0] <= page_width * 0.30:
        score += 0.05
        signals.append("left_aligned")
    if score < config.heading_score_threshold:
        return None

    return HeadingCandidate(
        index=-1,
        title=text,
        page=page_number,
        bbox=line.bbox,
        font_size=line.font_size,
        font_name=line.font_name,
        confidence=min(score, 1.0),
        raw_text=line.text,
        signals=tuple(signals),
    )


def _estimate_body_font_size(
    document: DocumentSignals,
    body_start_page: int,
    skip_pages: set[int],
    config: ReconstructionConfig,
) -> float:
    sizes: list[float] = []
    end_page = min(document.page_count, body_start_page + config.body_font_sample_pages - 1)
    for page_number in range(max(1, body_start_page), end_page + 1):
        if page_number in skip_pages:
            continue
        page = document.page(page_number)
        for line in page.lines:
            if _is_footer(line, page.height, config.header_footer_fraction):
                continue
            if line.font_size is not None and len(line.text) > 35:
                sizes.append(round(line.font_size, 1))
    return median(sizes) if sizes else 10.0


def _best_heading_match(
    row: PrintedTocRow,
    headings: list[HeadingCandidate],
    config: ReconstructionConfig,
) -> tuple[HeadingCandidate | None, float]:
    if not headings:
        return None, 0.0
    if row.resolved_page is not None:
        nearby = [
            heading
            for heading in headings
            if abs(heading.page - row.resolved_page) <= config.heading_page_window
        ]
        search_space = nearby or headings
    else:
        search_space = headings

    best: HeadingCandidate | None = None
    best_score = 0.0
    for heading in search_space:
        title_score = _similarity(row.title, heading.title)
        if row.resolved_page is None:
            page_score = 0.0
        else:
            distance = abs(heading.page - row.resolved_page)
            page_score = 1.0 if distance == 0 else max(0.0, 1.0 - distance / 4)
        score = title_score * 0.55
        score += page_score * 0.20
        score += _numbering_score(row.title, heading.title) * 0.15
        score += heading.confidence * 0.10
        if normalize_title(row.title) == normalize_title(heading.title):
            score += 0.08
        score = min(score, 1.0)
        if score > best_score or (
            score == best_score and best is not None and heading.index < best.index
        ):
            best = heading
            best_score = score
    return best, best_score


def _page_from_row_and_heading(
    row: PrintedTocRow,
    heading: HeadingCandidate,
    config: ReconstructionConfig,
) -> int:
    if row.resolved_page is None:
        return heading.page
    if abs(row.resolved_page - heading.page) <= config.heading_page_override_distance:
        return heading.page
    return row.resolved_page


def _infer_body_start_page(
    namespace: PageNumberNamespace | None,
    toc_pages: list[int],
) -> int:
    if namespace is not None:
        return namespace.pdf_start
    return max(toc_pages) + 1


def _apply_roman_section_depth(
    rows: list[PrintedTocRow],
    max_depth: int,
) -> list[PrintedTocRow]:
    adjusted: list[PrintedTocRow] = []
    inside_roman_section = False
    for row in rows:
        if _is_roman_section_title(row.title):
            inside_roman_section = True
            adjusted.append(row)
        elif inside_roman_section and re.match(r"^\d+(?:[.\-_]\d+)*\b", row.title):
            adjusted.append(
                replace(
                    row,
                    level=min(row.level + 1, max_depth),
                    signals=(*row.signals, "inside_roman_section"),
                )
            )
        else:
            adjusted.append(row)
    return adjusted


def _line_has_terminal_page_ref(text: str) -> bool:
    return bool(
        re.search(
            r"(?:\.{2,}|\s{2,}|\s+)(?:[A-Za-z]{1,12}-?)?\d{1,6}\s*$",
            text,
        )
    )


def _detect_repeated_toc_furniture(
    document: DocumentSignals,
    toc_pages: list[int],
    config: ReconstructionConfig,
) -> set[str]:
    occurrences: dict[str, set[int]] = {}
    for page_number in toc_pages:
        page = document.page(page_number)
        for line in page.lines:
            if not is_header_or_footer(line, page.height, config.header_footer_fraction):
                continue
            normalized = normalize_title(line.text)
            if normalized:
                occurrences.setdefault(normalized, set()).add(page_number)
    minimum_pages = max(2, math.ceil(len(toc_pages) * 0.30))
    return {
        normalized
        for normalized, pages in occurrences.items()
        if len(pages) >= minimum_pages
    }


def _is_footer(line: PageLine, page_height: float, fraction: float) -> bool:
    return line.bbox is not None and line.bbox[3] >= page_height * (1 - fraction)


def _numbering_score(left: str, right: str) -> float:
    left_marker = _leading_marker(left)
    right_marker = _leading_marker(right)
    if left_marker and right_marker and left_marker == right_marker:
        return 1.0
    if left_marker and right_marker and (
        left_marker.startswith(right_marker) or right_marker.startswith(left_marker)
    ):
        return 0.6
    return 0.0


def _leading_marker(title: str) -> str | None:
    match = re.match(
        r"^\s*((?:\d+(?:[.\-_]\d+)*)|(?:[IVXLCDM]+\.))(?=\s|$)",
        title,
    )
    return match.group(1) if match else None


def _infer_level(title: str) -> int:
    if _is_roman_section_title(title):
        return 1
    match = re.match(r"^(\d+(?:[.\-_]\d+)*|[A-Z](?:\.\d+)+)\b", title)
    return len(re.split(r"[.\-_]", match.group(1))) if match else 1


def _is_printed_toc_furniture(text: str, keywords: tuple[str, ...]) -> bool:
    return text.strip().casefold() in {keyword.casefold() for keyword in keywords}


def _is_numbering_token(text: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:[.\-_]\d+)*|[A-Z](?:\.\d+)*", text))


def _is_roman_section_title(text: str) -> bool:
    return bool(re.match(r"^[IVXLCDM]+\.(?:\s+\S.*)?$", text))


def _starts_with_outline_marker(title: str) -> bool:
    return bool(
        re.match(
            r"^(?:\d+(?:[.\-_]\d+)*\b|[A-Z](?:\.\d+)+\b|[IVXLCDM]+\.(?=\s|$))",
            title,
        )
    )


def _clean_title(text: str) -> str:
    return re.sub(r"\.{2,}$", "", _normalize_space(text)).strip()


def _looks_like_title(text: str) -> bool:
    if len(text) < 3 or len(text) > MAX_BODY_HEADING_CHARACTERS:
        return False
    if re.fullmatch(r"[\d.\-_]+", text):
        return False
    return sum(character.isalpha() for character in text) >= 2


def _looks_like_toc_title(text: str) -> bool:
    """Allow long index rows without broadening body-heading detection."""

    if len(text) < 3 or len(text) > MAX_PRINTED_TOC_TITLE_CHARACTERS:
        return False
    if re.fullmatch(r"[\d.\-_]+", text):
        return False
    return sum(character.isalpha() for character in text) >= 2


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_title(left), normalize_title(right)).ratio()
