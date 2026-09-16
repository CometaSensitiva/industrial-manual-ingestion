"""Profile implementations selected after capability detection."""

from .base import (
    ContentAdapter,
    ContentParseResult,
    IngestionProfile,
    OutlineAdapter,
    OutlineParseResult,
    ProfileExecutionError,
    ProfileResult,
)

__all__ = [
    "ContentAdapter",
    "ContentParseResult",
    "IngestionProfile",
    "DigitalOutlineProfile",
    "DigitalReconstructedProfile",
    "DigitalReconstructedResult",
    "OutlineAdapter",
    "OutlineParseResult",
    "ProfileExecutionError",
    "ProfileResult",
    "ScannedOCRProfile",
    "ScannedOCRResult",
    "build_profile",
]


def __getattr__(name: str) -> object:
    """Load concrete profiles lazily so adapter protocols stay cycle-free."""

    if name == "build_profile":
        from .registry import build_profile

        return build_profile
    if name == "DigitalOutlineProfile":
        from .digital_outline import DigitalOutlineProfile

        return DigitalOutlineProfile
    if name in {"DigitalReconstructedProfile", "DigitalReconstructedResult"}:
        from .digital_reconstructed import (
            DigitalReconstructedProfile,
            DigitalReconstructedResult,
        )

        return {
            "DigitalReconstructedProfile": DigitalReconstructedProfile,
            "DigitalReconstructedResult": DigitalReconstructedResult,
        }[name]
    if name in {"ScannedOCRProfile", "ScannedOCRResult"}:
        from .scanned_ocr import ScannedOCRProfile, ScannedOCRResult

        return {
            "ScannedOCRProfile": ScannedOCRProfile,
            "ScannedOCRResult": ScannedOCRResult,
        }[name]
    raise AttributeError(name)
