from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from manual_ingestion.detection import PDFDetectionError, detect_pdf_capabilities
from manual_ingestion.models import DocumentProfile

DIGITAL_TEXT = (
    "This technical manual page contains operating instructions, safety notes, "
    "configuration details, and enough textual content for deterministic detection."
)


def _write_pdf(path: Path, page_texts: list[str], *, outline: bool = False) -> None:
    document = pymupdf.open()
    try:
        for text in page_texts:
            page = document.new_page()
            page.draw_rect((72, 72, 520, 760), color=(0, 0, 0))
            if text:
                page.insert_textbox((90, 90, 500, 700), text, fontsize=11)
        if outline:
            document.set_toc([[1, "Operating instructions", 1]])
        document.save(path)
    finally:
        document.close()


def test_detects_digital_pdf_with_embedded_outline(tmp_path: Path) -> None:
    source = tmp_path / "digital-outline.pdf"
    _write_pdf(source, [DIGITAL_TEXT, DIGITAL_TEXT], outline=True)

    detected = detect_pdf_capabilities(source)

    assert detected.profile is DocumentProfile.DIGITAL_OUTLINE
    assert detected.embedded_outline is True
    assert detected.outline_entries == 1
    assert detected.text_layer is True
    assert detected.sampled_pages == [1, 2]
    assert detected.pages_with_text == 2
    assert detected.median_text_characters >= 40
    assert detected.profile_confidence >= 0.9
    assert any("Selected digital_outline" in reason for reason in detected.reasons)


@pytest.mark.parametrize(
    ("outline", "expected_profile"),
    [
        (False, DocumentProfile.DIGITAL_RECONSTRUCTED),
        (True, DocumentProfile.DIGITAL_OUTLINE),
    ],
)
def test_six_of_seven_text_pages_is_robust_digital_coverage(
    tmp_path: Path,
    outline: bool,
    expected_profile: DocumentProfile,
) -> None:
    source = tmp_path / "digital.pdf"
    _write_pdf(
        source,
        [
            DIGITAL_TEXT,
            DIGITAL_TEXT,
            DIGITAL_TEXT,
            "",
            DIGITAL_TEXT,
            DIGITAL_TEXT,
            DIGITAL_TEXT,
        ],
        outline=outline,
    )

    detected = detect_pdf_capabilities(source)

    assert detected.profile is expected_profile
    assert detected.embedded_outline is outline
    assert detected.text_layer is True
    assert detected.sampled_pages == [1, 2, 3, 4, 5, 6, 7]
    assert detected.pages_with_text == 6
    assert detected.median_text_characters >= 40
    assert detected.profile_confidence >= 0.8
    assert any(
        "Robust text-layer coverage detected: 6/7 sampled pages (85.7%)" in reason
        and "minimum required: 6/7" in reason
        for reason in detected.reasons
    )


@pytest.mark.parametrize("outline", [False, True])
def test_two_of_seven_text_pages_routes_hybrid_pdf_to_scanned_ocr(
    tmp_path: Path,
    outline: bool,
) -> None:
    source = tmp_path / "hybrid.pdf"
    _write_pdf(
        source,
        [DIGITAL_TEXT, "", "", "", "", "", DIGITAL_TEXT],
        outline=outline,
    )

    detected = detect_pdf_capabilities(source)

    assert detected.profile is DocumentProfile.SCANNED_OCR
    assert detected.embedded_outline is outline
    assert detected.text_layer is False
    assert detected.sampled_pages == [1, 2, 3, 4, 5, 6, 7]
    assert detected.pages_with_text == 2
    assert detected.median_text_characters == 0
    assert any(
        "Insufficient text-layer coverage for a digital profile: "
        "2/7 sampled pages (28.6%)" in reason
        and "minimum required: 6/7" in reason
        for reason in detected.reasons
    )
    assert any(
        "Selected scanned_ocr fail closed" in reason
        and "below the required 6/7 digital coverage" in reason
        for reason in detected.reasons
    )
    if outline:
        assert any(
            "An embedded outline does not make the remaining pages safe" in reason
            for reason in detected.reasons
        )


@pytest.mark.parametrize(
    ("page_texts", "expected_profile", "expected_requirement"),
    [
        ([DIGITAL_TEXT], DocumentProfile.DIGITAL_RECONSTRUCTED, "1/1"),
        ([""], DocumentProfile.SCANNED_OCR, "1/1"),
        (
            [DIGITAL_TEXT, DIGITAL_TEXT],
            DocumentProfile.DIGITAL_RECONSTRUCTED,
            "2/2",
        ),
        ([DIGITAL_TEXT, ""], DocumentProfile.SCANNED_OCR, "2/2"),
        (
            [DIGITAL_TEXT, DIGITAL_TEXT, ""],
            DocumentProfile.SCANNED_OCR,
            "3/3",
        ),
        (
            [DIGITAL_TEXT, DIGITAL_TEXT, DIGITAL_TEXT, ""],
            DocumentProfile.DIGITAL_RECONSTRUCTED,
            "3/4",
        ),
    ],
)
def test_small_documents_apply_the_same_fail_closed_coverage_policy(
    tmp_path: Path,
    page_texts: list[str],
    expected_profile: DocumentProfile,
    expected_requirement: str,
) -> None:
    source = tmp_path / "small.pdf"
    _write_pdf(source, page_texts)

    detected = detect_pdf_capabilities(source)

    assert detected.profile is expected_profile
    assert any(
        f"minimum required: {expected_requirement}" in reason
        for reason in detected.reasons
    )
    if expected_profile is DocumentProfile.SCANNED_OCR:
        assert detected.text_layer is False
        assert any(
            "Insufficient text-layer coverage for a digital profile" in reason
            for reason in detected.reasons
        )
    else:
        assert detected.text_layer is True
        assert any(
            "Robust text-layer coverage detected" in reason
            for reason in detected.reasons
        )


def test_detects_scanned_pdf_without_text_layer(tmp_path: Path) -> None:
    source = tmp_path / "scanned.pdf"
    _write_pdf(source, ["", "", ""])

    detected = detect_pdf_capabilities(source)

    assert detected.profile is DocumentProfile.SCANNED_OCR
    assert detected.embedded_outline is False
    assert detected.text_layer is False
    assert detected.sampled_pages == [1, 2, 3]
    assert detected.pages_with_text == 0
    assert detected.median_text_characters == 0
    assert detected.profile_confidence >= 0.9
    assert any("Selected scanned_ocr" in reason for reason in detected.reasons)


def test_outline_without_usable_text_still_requires_ocr(tmp_path: Path) -> None:
    source = tmp_path / "outlined-scan.pdf"
    _write_pdf(source, ["", ""], outline=True)

    detected = detect_pdf_capabilities(source)

    assert detected.profile is DocumentProfile.SCANNED_OCR
    assert detected.embedded_outline is True
    assert detected.text_layer is False
    assert any("outline alone is insufficient" in reason for reason in detected.reasons)


def test_missing_pdf_has_actionable_error(tmp_path: Path) -> None:
    source = tmp_path / "missing.pdf"

    with pytest.raises(PDFDetectionError, match=r"PDF not found: .*missing\.pdf"):
        detect_pdf_capabilities(source)


def test_malformed_pdf_has_actionable_error(tmp_path: Path) -> None:
    source = tmp_path / "malformed.pdf"
    source.write_bytes(b"this is not a PDF")

    with pytest.raises(PDFDetectionError, match=r"Cannot open PDF '.*malformed\.pdf'"):
        detect_pdf_capabilities(source)
