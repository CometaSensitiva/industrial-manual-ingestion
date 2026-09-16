"""Internal contracts for deterministic digital-PDF TOC reconstruction."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from manual_ingestion.models import TocEntry


STRUCTURE_STRATEGY = "printed_toc_body_heading_fusion_v1"


class TocReconstructionError(ValueError):
    """Raised when a digital PDF cannot produce a usable reconstructed TOC."""


@dataclass(frozen=True)
class ReconstructionConfig:
    """Generic, persisted thresholds for the single Case 2 strategy."""

    max_depth: int = 6
    toc_search_min_pages: int = 80
    toc_search_fraction: float = 0.25
    toc_search_tail_pages: int = 40
    toc_keywords: tuple[str, ...] = ("table of contents", "contents", "indice", "sommario")
    min_terminal_refs_without_keyword: int = 6
    min_toc_page_score: float = 8.0
    header_footer_fraction: float = 0.18
    min_namespace_evidence: int = 3
    body_font_sample_pages: int = 25
    heading_score_threshold: float = 0.48
    match_threshold: float = 0.72
    weak_match_threshold: float = 0.55
    heading_page_window: int = 3
    heading_page_override_distance: int = 2

    def __post_init__(self) -> None:
        if self.max_depth < 1:
            raise ValueError("max_depth must be at least 1")
        if not 0 < self.toc_search_fraction <= 1:
            raise ValueError("toc_search_fraction must be in (0, 1]")
        if not 0 < self.header_footer_fraction < 0.5:
            raise ValueError("header_footer_fraction must be in (0, 0.5)")
        if self.min_namespace_evidence < 1:
            raise ValueError("min_namespace_evidence must be at least 1")
        if not 0 <= self.weak_match_threshold <= self.match_threshold <= 1:
            raise ValueError("match thresholds must satisfy 0 <= weak <= strong <= 1")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["toc_keywords"] = list(self.toc_keywords)
        return value


@dataclass(frozen=True)
class PageLine:
    text: str
    bbox: tuple[float, float, float, float] | None
    font_size: float | None
    font_name: str | None


@dataclass(frozen=True)
class PageSignals:
    number: int
    width: float
    height: float
    lines: tuple[PageLine, ...]


@dataclass(frozen=True)
class PageLabelSegment:
    start_page: int
    end_page: int
    prefix: str
    first_number: int
    style: str


@dataclass(frozen=True)
class DocumentSignals:
    page_count: int
    outline_entries: int
    pages: tuple[PageSignals, ...]
    page_labels: tuple[PageLabelSegment, ...] = ()

    def page(self, page_number: int) -> PageSignals:
        if page_number < 1 or page_number > self.page_count:
            raise IndexError(f"page {page_number} is outside 1..{self.page_count}")
        return self.pages[page_number - 1]


@dataclass(frozen=True)
class PageNumberNamespace:
    prefix: str
    pdf_start: int
    printed_start: int
    evidence_count: int
    source: str

    @property
    def offset(self) -> int:
        return self.pdf_start - self.printed_start


@dataclass
class PageNumberMap:
    page_count: int
    direct_refs: dict[str, int] = field(default_factory=dict)
    namespaces: dict[str, PageNumberNamespace] = field(default_factory=dict)

    def resolve(self, page_ref: str) -> int | None:
        from .page_map import page_ref_key, parse_page_ref

        parsed = parse_page_ref(page_ref)
        if parsed is None:
            return None
        prefix, printed_page = parsed
        key = page_ref_key(prefix, printed_page)
        resolved = self.direct_refs.get(key)
        if resolved is None:
            namespace = self.namespaces.get(prefix)
            if namespace is None:
                return None
            resolved = printed_page + namespace.offset
        return resolved if 1 <= resolved <= self.page_count else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "direct_ref_count": len(self.direct_refs),
            "namespaces": {
                prefix or "body": {
                    "prefix": namespace.prefix,
                    "pdf_start": namespace.pdf_start,
                    "printed_start": namespace.printed_start,
                    "evidence_count": namespace.evidence_count,
                    "source": namespace.source,
                    "offset": namespace.offset,
                }
                for prefix, namespace in sorted(self.namespaces.items())
            },
        }


@dataclass(frozen=True)
class PageRefObservation:
    pdf_page: int
    prefix: str
    printed_page: int
    confidence: float
    raw_text: str


@dataclass(frozen=True)
class PrintedTocRow:
    index: int
    level: int
    title: str
    page_ref: str
    printed_page: int | None
    resolved_page: int | None
    source_page: int
    raw_text: str
    confidence: float
    signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class HeadingCandidate:
    index: int
    title: str
    page: int
    bbox: tuple[float, float, float, float] | None
    font_size: float | None
    font_name: str | None
    confidence: float
    raw_text: str
    signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class FusionMatch:
    row_index: int
    heading_index: int | None
    score: float
    decision: str
    page: int | None
    reason: str


@dataclass(frozen=True)
class ReconstructionDiagnostics:
    strategy: str
    config: dict[str, Any]
    detected_toc_pages: tuple[int, ...]
    ignored_repeated_lines: tuple[str, ...]
    page_number_map: dict[str, Any]
    printed_rows: tuple[PrintedTocRow, ...]
    heading_candidates: tuple[HeadingCandidate, ...]
    matches: tuple[FusionMatch, ...]
    warnings: tuple[str, ...]
    entry_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "config": self.config,
            "detected_toc_pages": list(self.detected_toc_pages),
            "ignored_repeated_lines": list(self.ignored_repeated_lines),
            "page_number_map": self.page_number_map,
            "printed_rows": [asdict(row) for row in self.printed_rows],
            "heading_candidates": [asdict(candidate) for candidate in self.heading_candidates],
            "matches": [asdict(match) for match in self.matches],
            "warnings": list(self.warnings),
            "entry_count": self.entry_count,
        }


@dataclass(frozen=True)
class TocReconstructionResult:
    entries: tuple[TocEntry, ...]
    diagnostics: ReconstructionDiagnostics
