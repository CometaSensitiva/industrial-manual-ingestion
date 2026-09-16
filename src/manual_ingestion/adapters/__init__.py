"""External parser and PDF metadata adapters."""

from .docling_adapter import (
    DoclingContentAdapter,
    DoclingConversionError,
    DoclingUnavailableError,
)
from .pymupdf_adapter import OutlineExtractionError, PyMuPDFOutlineAdapter
from .paddle_adapter import (
    PaddleAdapterError,
    PaddleOCRConfig,
    PaddleOCRContentAdapter,
    PaddleRuntimeConfig,
    SubprocessPaddleWorker,
)

__all__ = [
    "DoclingContentAdapter",
    "DoclingConversionError",
    "DoclingUnavailableError",
    "OutlineExtractionError",
    "PaddleAdapterError",
    "PaddleOCRConfig",
    "PaddleOCRContentAdapter",
    "PaddleRuntimeConfig",
    "PyMuPDFOutlineAdapter",
    "SubprocessPaddleWorker",
]
