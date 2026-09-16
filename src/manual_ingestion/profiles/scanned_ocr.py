"""Chosen V2 pipeline for scanned PDFs without a usable text layer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from manual_ingestion.adapters.paddle_adapter import PaddleOCRContentAdapter
from manual_ingestion.adapters.pymupdf_adapter import PyMuPDFOutlineAdapter
from manual_ingestion.models import Chapter, DocumentProfile, ManualDocument, ManualElement
from manual_ingestion.profiles.base import (
    ContentAdapter,
    ContentParseResult,
    OutlineParseResult,
    ProfileResult,
)
from manual_ingestion.profiles.digital_outline import DigitalOutlineProfile
from manual_ingestion.structure import (
    OcrReconstructionConfig,
    OcrReconstructionDiagnostics,
    reconstruct_toc_from_ocr_elements,
)

SCANNED_EMBEDDED_OUTLINE_STRATEGY = "embedded_outline_with_paddle_ocr"
PRINTED_TOC_EXCLUSION_REASON = "printed table-of-contents source page"


@dataclass(slots=True, kw_only=True)
class ScannedOCRResult(ProfileResult):
    diagnostics: OcrReconstructionDiagnostics | None
    runtime: dict[str, str] | None


@dataclass(slots=True)
class _StaticOutlineAdapter:
    result: OutlineParseResult
    name: str = "scanned_structure"

    def extract(self, pdf_path: Path) -> OutlineParseResult:
        return self.result


class _ScannedAssembler(DigitalOutlineProfile):
    profile = DocumentProfile.SCANNED_OCR

    def __init__(self, *, structure_strategy: str, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.structure_strategy = structure_strategy


class ScannedOCRProfile:
    """Parse every selected page with Paddle, then recover or reuse structure."""

    profile = DocumentProfile.SCANNED_OCR

    def __init__(
        self,
        *,
        content_adapter: ContentAdapter | None = None,
        structure_config: OcrReconstructionConfig | None = None,
    ) -> None:
        self.content_adapter = content_adapter or PaddleOCRContentAdapter()
        self.structure_config = structure_config

    def run(
        self,
        pdf_path: str | Path,
        *,
        run_id: str,
        assets_dir: str | Path,
        pages: list[int] | None = None,
        title: str | None = None,
        language: str = "en",
    ) -> ScannedOCRResult:
        if not run_id.strip():
            raise ValueError("run_id cannot be empty")
        source = Path(pdf_path)
        document_facts = PyMuPDFOutlineAdapter().extract(source)
        selected = (
            list(range(1, document_facts.pages_total + 1))
            if pages is None
            else _validate_selected_pages(pages, document_facts.pages_total)
        )
        parsed = self.content_adapter.parse(source, selected, Path(assets_dir))

        diagnostics: OcrReconstructionDiagnostics | None
        if document_facts.entries:
            toc = list(document_facts.entries)
            diagnostics = None
            structure_strategy = SCANNED_EMBEDDED_OUTLINE_STRATEGY
        else:
            reconstruction = reconstruct_toc_from_ocr_elements(
                parsed.elements,
                pages_total=document_facts.pages_total,
                config=self.structure_config,
            )
            toc = list(reconstruction.entries)
            diagnostics = reconstruction.diagnostics
            structure_strategy = diagnostics.strategy

        static_outline = OutlineParseResult(
            entries=toc,
            pages_total=document_facts.pages_total,
            document_title=document_facts.document_title,
        )
        assembled = _ScannedAssembler(
            structure_strategy=structure_strategy,
            content_adapter=_ParsedContentAdapter(parsed, name=self.content_adapter.name),
            outline_adapter=_StaticOutlineAdapter(static_outline),
        ).run(
            source,
            run_id=run_id,
            assets_dir=assets_dir,
            pages=selected,
            title=title,
            language=language,
        )

        warnings = list(assembled.warnings)
        manual = assembled.manual
        if diagnostics is not None and diagnostics.detected_toc_pages:
            manual, detached_count = _detach_printed_toc_elements(
                manual,
                set(diagnostics.detected_toc_pages),
            )
            if detached_count:
                warnings.append(
                    f"{detached_count} printed TOC element(s) were retained at document root "
                    "and excluded from RAG"
                )
        if pages is not None and len(selected) < document_facts.pages_total:
            warnings.append(
                "Scanned structure was derived from a partial page selection and may be incomplete."
            )
        if diagnostics is not None:
            warnings.extend(f"OCR structure: {warning}" for warning in diagnostics.warnings)
        runtime = getattr(self.content_adapter, "last_runtime", None)
        return ScannedOCRResult(
            manual=manual,
            toc=assembled.toc,
            warnings=warnings,
            diagnostics=diagnostics,
            runtime=dict(runtime) if isinstance(runtime, dict) else None,
        )


@dataclass(slots=True)
class _ParsedContentAdapter:
    """Replay an already parsed result through the shared hierarchy assembler."""

    result: ContentParseResult
    name: str

    def parse(
        self,
        pdf_path: Path,
        pages: list[int],
        assets_dir: Path,
    ) -> ContentParseResult:
        return self.result


def _validate_selected_pages(pages: list[int], pages_total: int) -> list[int]:
    if not pages:
        raise ValueError("pages cannot be empty")
    if len(pages) != len(set(pages)):
        raise ValueError("pages cannot contain duplicates")
    selected = sorted(pages)
    if selected[0] < 1 or selected[-1] > pages_total:
        raise ValueError(f"pages must be between 1 and {pages_total}")
    return selected


def _detach_printed_toc_elements(
    manual: ManualDocument,
    toc_pages: set[int],
) -> tuple[ManualDocument, int]:
    """Keep printed-index evidence, but never treat it as chapter/RAG content."""

    detached: list[ManualElement] = []

    def visit(items: list[Chapter | ManualElement]) -> list[Chapter | ManualElement]:
        retained: list[Chapter | ManualElement] = []
        for item in items:
            if isinstance(item, Chapter):
                retained.append(item.model_copy(update={"content": visit(item.content)}))
                continue
            if item.page not in toc_pages:
                retained.append(item)
                continue
            detached.append(
                item.model_copy(
                    update={
                        "trace": item.trace.model_copy(
                            update={
                                "parent_chapter_id": None,
                                "include_in_rag": False,
                                "exclusion_reason": PRINTED_TOC_EXCLUSION_REASON,
                            }
                        )
                    }
                )
            )
        return retained

    retained_root = visit(manual.content)
    retained_root.extend(detached)
    retained_root.sort(key=_root_content_sort_key)
    return manual.model_copy(update={"content": retained_root}), len(detached)


def _root_content_sort_key(item: Chapter | ManualElement) -> tuple[int, int, int]:
    if isinstance(item, Chapter):
        return (item.page, 0, 0)
    order = item.trace.reading_order if item.trace.reading_order is not None else 0
    return (item.page, 1, order)
