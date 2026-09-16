"""Exact mapping from detected capabilities to the three V2 profiles."""

from __future__ import annotations

from manual_ingestion.adapters.paddle_adapter import (
    PaddleOCRContentAdapter,
    PaddleRuntimeConfig,
)
from manual_ingestion.models import DetectedCapabilities, DocumentProfile
from manual_ingestion.progress import ProgressCallback

from .base import IngestionProfile
from .digital_outline import DigitalOutlineProfile
from .digital_reconstructed import DigitalReconstructedProfile
from .scanned_ocr import ScannedOCRProfile


def build_profile(
    selection: DocumentProfile | DetectedCapabilities,
    *,
    paddle_runtime: PaddleRuntimeConfig | None = None,
    progress: ProgressCallback | None = None,
) -> IngestionProfile:
    """Build the one chosen implementation for an already detected profile.

    ``DetectedCapabilities`` is accepted directly so callers do not need to
    duplicate detection-to-profile plumbing. Plain strings are deliberately
    rejected: routing must be based on the validated enum and must never fall
    through to a default parser.

    The optional Paddle runtime is applied only to ``scanned_ocr``. This lets a
    caller select the isolated external interpreter explicitly without mutating
    process-wide environment variables.
    """

    if isinstance(selection, DetectedCapabilities):
        profile = selection.profile
    elif isinstance(selection, DocumentProfile):
        profile = selection
    else:
        raise TypeError(
            "selection must be a DocumentProfile or DetectedCapabilities instance"
        )

    if profile is DocumentProfile.DIGITAL_OUTLINE:
        return DigitalOutlineProfile()
    if profile is DocumentProfile.DIGITAL_RECONSTRUCTED:
        return DigitalReconstructedProfile()
    if profile is DocumentProfile.SCANNED_OCR:
        return ScannedOCRProfile(
            content_adapter=PaddleOCRContentAdapter(
                runtime=paddle_runtime,
                progress=progress,
            )
        )

    # Defensive guard for future enum additions: choosing a new profile must be
    # an explicit architecture decision, never an accidental fallback.
    raise ValueError(f"Unsupported document profile: {profile!r}")
