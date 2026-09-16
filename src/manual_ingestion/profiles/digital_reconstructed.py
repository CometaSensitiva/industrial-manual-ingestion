"""Chosen V2 pipeline for digital PDFs without an embedded outline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from manual_ingestion.adapters.docling_adapter import DoclingContentAdapter
from manual_ingestion.adapters.pymupdf_adapter import PyMuPDFOutlineAdapter
from manual_ingestion.models import DocumentProfile
from manual_ingestion.profiles.base import (
    ContentAdapter,
    OutlineParseResult,
    ProfileResult,
)
from manual_ingestion.profiles.digital_outline import DigitalOutlineProfile
from manual_ingestion.structure import (
    STRUCTURE_STRATEGY,
    ReconstructionConfig,
    ReconstructionDiagnostics,
    reconstruct_toc,
)


@dataclass(slots=True, kw_only=True)
class DigitalReconstructedResult(ProfileResult):
    """Profile result retaining the evidence behind the reconstructed TOC."""

    diagnostics: ReconstructionDiagnostics


@dataclass(slots=True)
class _StaticOutlineAdapter:
    """Expose one reconstructed outline through the shared hierarchy assembler."""

    result: OutlineParseResult
    name: str = "reconstructed_toc"

    def extract(self, pdf_path: Path) -> OutlineParseResult:
        return self.result


class _DigitalReconstructedAssembler(DigitalOutlineProfile):
    """Reuse the canonical hierarchy and trace assembly with Case 2 metadata."""

    profile = DocumentProfile.DIGITAL_RECONSTRUCTED
    structure_strategy = STRUCTURE_STRATEGY


class DigitalReconstructedProfile:
    """Fuse printed TOC and body headings, then parse page content with Docling."""

    profile = DocumentProfile.DIGITAL_RECONSTRUCTED
    structure_strategy = STRUCTURE_STRATEGY

    def __init__(
        self,
        *,
        content_adapter: ContentAdapter | None = None,
        config: ReconstructionConfig | None = None,
    ) -> None:
        self.content_adapter = content_adapter or DoclingContentAdapter()
        self.config = config

    def run(
        self,
        pdf_path: str | Path,
        *,
        run_id: str,
        assets_dir: str | Path,
        pages: list[int] | None = None,
        title: str | None = None,
        language: str = "en",
    ) -> DigitalReconstructedResult:
        source_path = Path(pdf_path)

        # This call owns the profile mismatch check: an embedded outline raises
        # TocReconstructionError before content parsing or generic assembly.
        reconstruction = reconstruct_toc(source_path, self.config)

        document_facts = PyMuPDFOutlineAdapter().extract(source_path)
        reconstructed_outline = OutlineParseResult(
            entries=list(reconstruction.entries),
            pages_total=document_facts.pages_total,
            document_title=document_facts.document_title,
        )
        assembler = _DigitalReconstructedAssembler(
            content_adapter=self.content_adapter,
            outline_adapter=_StaticOutlineAdapter(reconstructed_outline),
        )
        assembled = assembler.run(
            source_path,
            run_id=run_id,
            assets_dir=assets_dir,
            pages=pages,
            title=title,
            language=language,
        )

        reconstruction_warnings = [
            f"TOC reconstruction: {warning}" for warning in reconstruction.diagnostics.warnings
        ]
        return DigitalReconstructedResult(
            manual=assembled.manual,
            toc=assembled.toc,
            warnings=[*assembled.warnings, *reconstruction_warnings],
            diagnostics=reconstruction.diagnostics,
        )
