from __future__ import annotations

from datetime import UTC, datetime
import pytest

from manual_ingestion.diagnostics import EnrichmentDiagnostic, StructureDiagnostic
from manual_ingestion.models import (
    CaptionProvenance,
    DetectedCapabilities,
    DocumentProfile,
    ManualDocument,
    ManualElement,
    ManualMetadata,
    PipelineSelection,
    RunManifest,
    RunStatus,
    SourceDocument,
    SourceReference,
)
from manual_ingestion.profiles.scanned_ocr import SCANNED_EMBEDDED_OUTLINE_STRATEGY
from manual_ingestion.promotion import (
    build_promotion_assessment,
    caption_identity_errors,
    failed_promotion_gate_ids,
    warning_requires_experimental,
)
from manual_ingestion.providers.ollama import (
    ACCEPTED_OLLAMA_MODEL,
    ACCEPTED_OLLAMA_MODEL_DIGEST,
    ACCEPTED_OLLAMA_OUTPUT_PARAMS,
    OLLAMA_RUNTIME_BY_PROMPT_VERSION,
)
from manual_ingestion.structure.ocr_reconstruct import (
    OCR_FALLBACK_STRATEGY,
    OCR_STRUCTURE_STRATEGY,
)


def test_benign_profile_warning_does_not_demote_accepted_digital_setup() -> None:
    manifest, manual, enrichment = _contracts(
        DocumentProfile.DIGITAL_OUTLINE,
        parser="docling",
        structure_strategy="embedded_outline",
    )

    assessment = build_promotion_assessment(
        manifest=manifest,
        manual=manual,
        enrichment=enrichment,
        structure=StructureDiagnostic(
            strategy="embedded_outline",
            toc_available=True,
            details_available=False,
        ),
        profile_warnings=[
            "outline anchor normalized match: chapter 'Safety' on page 1 "
            "matched 1 TITLE block(s) via normalized_title_similarity "
            "(score=0.975)",
            "outline container boundary delegated: chapter 'Operations' on "
            "page 1 has no standalone TITLE block; anchored child chapters "
            "define the same-page boundaries",
        ],
    )

    assert assessment.eligible_for_validated is True
    assert failed_promotion_gate_ids(assessment) == []


@pytest.mark.parametrize(
    ("prompt_version", "runtime_version", "accepted"),
    [
        ("technical-caption-v2", "0.31.2", True),
        ("technical-caption-v3", "0.34.0", True),
        ("technical-caption-v4", "0.34.2", True),
        ("technical-caption-v4", "0.35.0", True),
        ("technical-caption-v2", "0.34.0", True),
        ("technical-caption-v999", "0.34.0", False),
        ("technical-caption-v4", "nightly", False),
    ],
)
def test_accepted_prompt_is_required_and_any_released_runtime_is_recorded(
    prompt_version, runtime_version, accepted,
) -> None:
    manifest, manual, enrichment = _contracts(
        DocumentProfile.DIGITAL_OUTLINE,
        parser="docling", structure_strategy="embedded_outline",
    )
    raw_enrichment = manifest.pipeline.config["enrichment"]
    raw_enrichment["prompt_version"] = prompt_version
    raw_enrichment["preflight"]["version"] = runtime_version
    enrichment.provider_preflight["version"] = runtime_version
    element = _captioned_image("image-1", "ollama", ACCEPTED_OLLAMA_MODEL)
    element.caption_provenance.prompt_version = prompt_version
    element.caption_provenance.runtime_version = runtime_version

    errors = caption_identity_errors(manifest, [element])
    assert (not errors) is accepted
    assessment = build_promotion_assessment(
        manifest=manifest, manual=manual, enrichment=enrichment,
        structure=StructureDiagnostic(
            strategy="embedded_outline", toc_available=True, details_available=False,
        ),
        profile_warnings=[],
    )
    gate = next(gate for gate in assessment.gates if gate.id == "enrichment.configured")
    assert gate.passed is accepted


def test_supported_prompt_versions_cannot_be_mixed_inside_one_bundle() -> None:
    manifest, _, _ = _contracts(
        DocumentProfile.DIGITAL_OUTLINE,
        parser="docling", structure_strategy="embedded_outline",
    )
    element = _captioned_image("image-1", "ollama", ACCEPTED_OLLAMA_MODEL)
    element.caption_provenance.prompt_version = "technical-caption-v3"
    assert any(
        "prompt_version must match pipeline" in error
        for error in caption_identity_errors(manifest, [element])
    )


def test_scanned_fallback_and_incomplete_resolution_remain_experimental() -> None:
    manifest, manual, enrichment = _contracts(
        DocumentProfile.SCANNED_OCR,
        parser="paddleocr_vl",
        structure_strategy=OCR_FALLBACK_STRATEGY,
    )
    fallback = build_promotion_assessment(
        manifest=manifest,
        manual=manual,
        enrichment=enrichment,
        structure=StructureDiagnostic(
            strategy=OCR_FALLBACK_STRATEGY,
            toc_available=True,
            details_available=True,
            details={"strategy": OCR_FALLBACK_STRATEGY, "mode": "title_fallback"},
        ),
        profile_warnings=[],
        scanned_acceptance_evidence_id="accepted-evidence",
    )
    assert "structure.accepted" in failed_promotion_gate_ids(fallback)

    manifest_raw = manifest.model_dump(mode="json")
    manifest_raw["pipeline"]["structure_strategy"] = OCR_STRUCTURE_STRATEGY
    incomplete_manifest = RunManifest.model_validate(manifest_raw)
    manual_raw = manual.model_dump(mode="json")
    manual_raw["metadata"]["structure_strategy"] = OCR_STRUCTURE_STRATEGY
    incomplete_manual = ManualDocument.model_validate(manual_raw)
    incomplete = build_promotion_assessment(
        manifest=incomplete_manifest,
        manual=incomplete_manual,
        enrichment=enrichment,
        structure=StructureDiagnostic(
            strategy=OCR_STRUCTURE_STRATEGY,
            toc_available=True,
            details_available=True,
            details={
                "strategy": OCR_STRUCTURE_STRATEGY,
                "mode": "printed_index",
                "resolved_ratio": 0.9,
            },
        ),
        profile_warnings=[],
        scanned_acceptance_evidence_id="accepted-evidence",
    )
    assert "structure.accepted" in failed_promotion_gate_ids(incomplete)


def test_scanned_acceptance_evidence_and_fixed_parser_are_required() -> None:
    manifest, manual, enrichment = _contracts(
        DocumentProfile.SCANNED_OCR,
        parser="paddleocr_vl",
        structure_strategy=SCANNED_EMBEDDED_OUTLINE_STRATEGY,
    )
    no_evidence = build_promotion_assessment(
        manifest=manifest,
        manual=manual,
        enrichment=enrichment,
        structure=StructureDiagnostic(
            strategy=SCANNED_EMBEDDED_OUTLINE_STRATEGY,
            toc_available=True,
            details_available=False,
        ),
        profile_warnings=[],
        scanned_acceptance_evidence_id=None,
    )
    assert "profile.accepted" in failed_promotion_gate_ids(no_evidence)

    digital_manifest, digital_manual, digital_enrichment = _contracts(
        DocumentProfile.DIGITAL_OUTLINE,
        parser="arbitrary-parser",
        structure_strategy="embedded_outline",
    )
    wrong_parser = build_promotion_assessment(
        manifest=digital_manifest,
        manual=digital_manual,
        enrichment=digital_enrichment,
        structure=StructureDiagnostic(
            strategy="embedded_outline",
            toc_available=True,
            details_available=False,
        ),
        profile_warnings=[],
    )
    assert "profile.accepted" in failed_promotion_gate_ids(wrong_parser)


def test_multiple_caption_provider_identities_are_rejected() -> None:
    manifest, _, _ = _contracts(
        DocumentProfile.DIGITAL_OUTLINE,
        parser="docling",
        structure_strategy="embedded_outline",
    )
    elements = [
        _captioned_image("image-1", "ollama", ACCEPTED_OLLAMA_MODEL),
        _captioned_image("image-2", "other-provider", "other-model"),
    ]

    errors = caption_identity_errors(manifest, elements)

    assert any("multiple provider/model identities" in error for error in errors)


def test_table_fallback_and_unavailable_warnings_are_consequential() -> None:
    assert warning_requires_experimental(
        "Table 'table-1' serialization used fallback for a retrievable table."
    )
    assert warning_requires_experimental(
        "Table 'table-2' serialization is unavailable for a retrievable table."
    )


def test_retrievable_table_serialization_warning_prevents_promotion() -> None:
    manifest, manual, enrichment = _contracts(
        DocumentProfile.DIGITAL_OUTLINE,
        parser="docling",
        structure_strategy="embedded_outline",
    )

    assessment = build_promotion_assessment(
        manifest=manifest,
        manual=manual,
        enrichment=enrichment,
        structure=StructureDiagnostic(
            strategy="embedded_outline",
            toc_available=True,
            details_available=False,
        ),
        profile_warnings=[
            "Table 'table-1' serialization is unavailable for a retrievable table."
        ],
    )

    assert assessment.eligible_for_validated is False
    assert "warnings.nonconsequential" in failed_promotion_gate_ids(assessment)


def _contracts(
    profile: DocumentProfile,
    *,
    parser: str,
    structure_strategy: str,
) -> tuple[RunManifest, ManualDocument, EnrichmentDiagnostic]:
    embedded = profile is DocumentProfile.DIGITAL_OUTLINE or (
        profile is DocumentProfile.SCANNED_OCR
        and structure_strategy == SCANNED_EMBEDDED_OUTLINE_STRATEGY
    )
    text_layer = profile is not DocumentProfile.SCANNED_OCR
    detected = DetectedCapabilities(
        profile=profile,
        embedded_outline=embedded,
        outline_entries=1 if embedded else 0,
        text_layer=text_layer,
        sampled_pages=[1],
        pages_with_text=1 if text_layer else 0,
        median_text_characters=100 if text_layer else 0,
        profile_confidence=1,
    )
    preflight = {
        "version": OLLAMA_RUNTIME_BY_PROMPT_VERSION["technical-caption-v2"],
        "model": ACCEPTED_OLLAMA_MODEL,
        "model_digest": ACCEPTED_OLLAMA_MODEL_DIGEST,
    }
    provider_config = {
        "model": ACCEPTED_OLLAMA_MODEL,
        **ACCEPTED_OLLAMA_OUTPUT_PARAMS,
    }
    now = datetime.now(UTC).isoformat()
    manifest = RunManifest(
        run_id="run-1",
        status=RunStatus.COMPLETED,
        source=SourceDocument(
            file="manual.pdf",
            sha256="a" * 64,
            size_bytes=1,
            pages_total=2,
            detected=detected,
        ),
        pipeline=PipelineSelection(
            profile=profile,
            parser=parser,
            structure_strategy=structure_strategy,
            enrichment_provider="ollama",
            enrichment_model=ACCEPTED_OLLAMA_MODEL,
            config={
                "enrichment": {
                    "enabled": True,
                    "prompt_version": "technical-caption-v2",
                    "max_caption_characters": 1200,
                    "provider_config": provider_config,
                    "preflight": preflight,
                }
            },
        ),
        started_at=now,
        completed_at=now,
    )
    manual = ManualDocument(
        id="run-1",
        title="Manual",
        source_file="manual.pdf",
        metadata=ManualMetadata(
            pages_total=2,
            pages_processed=[1, 2],
            profile=profile,
            parser=parser,
            structure_strategy=structure_strategy,
            toc_available=True,
        ),
    )
    enrichment = EnrichmentDiagnostic(
        provider_configured=True,
        provider_preflight=preflight,
        enriched=0,
        skipped=0,
        failed=0,
        review_required=0,
        items=[],
    )
    return manifest, manual, enrichment


def _captioned_image(
    element_id: str,
    provider: str,
    model: str,
) -> ManualElement:
    return ManualElement(
        id=element_id,
        type="image",
        page=1,
        source=SourceReference(page=1),
        image_path=f"assets/images/{element_id}.png",
        caption_generated="caption",
        caption_provenance=CaptionProvenance(
            provider=provider,
            model=model,
            prompt_version="technical-caption-v2",
            params=dict(ACCEPTED_OLLAMA_OUTPUT_PARAMS),
            input_sha256="b" * 64,
            runtime_version=OLLAMA_RUNTIME_BY_PROMPT_VERSION["technical-caption-v2"],
        ),
    )
