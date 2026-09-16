"""Document-structure recovery for canonical ingestion profiles."""

from .models import (
    STRUCTURE_STRATEGY,
    ReconstructionConfig,
    ReconstructionDiagnostics,
    TocReconstructionError,
    TocReconstructionResult,
)
from .reconstruct import reconstruct_toc, reconstruct_toc_from_signals
from .ocr_reconstruct import (
    OCR_FALLBACK_STRATEGY,
    OCR_STRUCTURE_STRATEGY,
    OcrReconstructionConfig,
    OcrReconstructionDiagnostics,
    OcrTocReconstructionError,
    OcrTocReconstructionResult,
    normalize_ocr_toc_title,
    reconstruct_toc_from_ocr_elements,
)

__all__ = [
    "STRUCTURE_STRATEGY",
    "OCR_FALLBACK_STRATEGY",
    "OCR_STRUCTURE_STRATEGY",
    "OcrReconstructionConfig",
    "OcrReconstructionDiagnostics",
    "OcrTocReconstructionError",
    "OcrTocReconstructionResult",
    "ReconstructionConfig",
    "ReconstructionDiagnostics",
    "TocReconstructionError",
    "TocReconstructionResult",
    "reconstruct_toc",
    "reconstruct_toc_from_signals",
    "reconstruct_toc_from_ocr_elements",
    "normalize_ocr_toc_title",
]
