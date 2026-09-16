from __future__ import annotations

import pytest

from manual_ingestion.adapters.paddle_adapter import (
    PaddleOCRContentAdapter,
    PaddleRuntimeConfig,
)
from manual_ingestion.models import DetectedCapabilities, DocumentProfile
from manual_ingestion.profiles import build_profile
from manual_ingestion.profiles.digital_outline import DigitalOutlineProfile
from manual_ingestion.profiles.digital_reconstructed import DigitalReconstructedProfile
from manual_ingestion.profiles.scanned_ocr import ScannedOCRProfile


@pytest.mark.parametrize(
    ("profile", "expected_type"),
    [
        (DocumentProfile.DIGITAL_OUTLINE, DigitalOutlineProfile),
        (DocumentProfile.DIGITAL_RECONSTRUCTED, DigitalReconstructedProfile),
        (DocumentProfile.SCANNED_OCR, ScannedOCRProfile),
    ],
)
def test_build_profile_routes_every_detected_profile_exactly(
    profile: DocumentProfile,
    expected_type: type[object],
) -> None:
    built = build_profile(_capabilities(profile))

    assert type(built) is expected_type
    assert built.profile is profile


def test_build_profile_accepts_the_validated_profile_enum_directly() -> None:
    built = build_profile(DocumentProfile.DIGITAL_RECONSTRUCTED)

    assert type(built) is DigitalReconstructedProfile


def test_build_profile_passes_explicit_runtime_only_to_scanned_adapter() -> None:
    runtime = PaddleRuntimeConfig(python_executable="/opt/paddle/bin/python")

    built = build_profile(DocumentProfile.SCANNED_OCR, paddle_runtime=runtime)

    assert type(built) is ScannedOCRProfile
    assert isinstance(built.content_adapter, PaddleOCRContentAdapter)
    assert built.content_adapter.runtime is runtime


def test_build_profile_rejects_unvalidated_strings_without_fallback() -> None:
    with pytest.raises(TypeError, match="DocumentProfile or DetectedCapabilities"):
        build_profile("scanned_ocr")  # type: ignore[arg-type]


def _capabilities(profile: DocumentProfile) -> DetectedCapabilities:
    embedded_outline = profile is DocumentProfile.DIGITAL_OUTLINE
    text_layer = profile is not DocumentProfile.SCANNED_OCR
    return DetectedCapabilities(
        profile=profile,
        embedded_outline=embedded_outline,
        outline_entries=10 if embedded_outline else 0,
        text_layer=text_layer,
        sampled_pages=[1, 2],
        pages_with_text=2 if text_layer else 0,
        median_text_characters=120.0 if text_layer else 0.0,
        profile_confidence=0.98,
        reasons=["test fixture"],
    )
