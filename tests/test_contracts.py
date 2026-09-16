from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from manual_ingestion.models import (
    ArtifactPaths,
    BoundingBox,
    CaptionProvenance,
    DetectedCapabilities,
    DocumentProfile,
    ManualDocument,
    ManualElement,
    ManualMetadata,
    PageSize,
    PipelineSelection,
    RunManifest,
    RunStatus,
    SourceDocument,
    SourceReference,
    TableSerialization,
    TableSerializationStatus,
    TableSerializedRow,
    TocEntry,
    TraceMetadata,
    ValidationCheck,
    ValidationReport,
)

SHA256 = "a" * 64


def capabilities() -> DetectedCapabilities:
    detected = DetectedCapabilities(
        profile=DocumentProfile.DIGITAL_OUTLINE,
        embedded_outline=True,
        outline_entries=3,
        text_layer=True,
        sampled_pages=[1],
        pages_with_text=1,
        median_text_characters=100,
        profile_confidence=1,
        reasons=["embedded outline found"],
    )
    return detected


def test_manual_contract_rejects_page_mismatch() -> None:
    with pytest.raises(ValidationError, match="source.page"):
        ManualElement(
            id="text-1",
            type="text",
            page=2,
            source=SourceReference(page=1),
            text="hello",
        )


def test_source_contract_rejects_bbox_outside_page() -> None:
    with pytest.raises(ValidationError, match="inside page_size"):
        SourceReference(
            page=1,
            page_size=PageSize(width=100, height=100),
            bbox=BoundingBox(x0=0, y0=0, x1=120, y1=50),
        )


def test_run_contract_accepts_matching_detected_profile() -> None:
    now = datetime.now(UTC).isoformat()
    run = RunManifest(
        run_id="run-1",
        status=RunStatus.COMPLETED,
        source=SourceDocument(file="manual.pdf", sha256=SHA256, size_bytes=1, pages_total=1, detected=capabilities()),
        pipeline=PipelineSelection(
            profile=DocumentProfile.DIGITAL_OUTLINE,
            parser="docling",
            structure_strategy="embedded_outline",
        ),
        artifacts=ArtifactPaths(),
        started_at=now,
        completed_at=now,
    )
    assert run.pipeline.profile is DocumentProfile.DIGITAL_OUTLINE


@pytest.mark.parametrize(
    "overrides",
    [
        {"enrichment_provider": "ollama"},
        {"enrichment_model": "qwen3.5:4b"},
    ],
)
def test_pipeline_enrichment_provider_and_model_are_an_atomic_identity(
    overrides: dict[str, str],
) -> None:
    with pytest.raises(ValidationError, match="must be declared together"):
        PipelineSelection(
            profile=DocumentProfile.DIGITAL_OUTLINE,
            parser="docling",
            structure_strategy="embedded_outline",
            **overrides,
        )


@pytest.mark.parametrize("field", ["provider", "model", "prompt_version"])
def test_caption_provenance_identity_fields_reject_blank_values(field: str) -> None:
    values = {
        "provider": "ollama",
        "model": "qwen3.5:4b",
        "prompt_version": "technical-caption-v2",
        "input_sha256": SHA256,
    }
    values[field] = " "
    with pytest.raises(ValidationError, match="canonical non-empty strings"):
        CaptionProvenance(**values)


def test_manual_schema_version_is_v1_1() -> None:
    manual = ManualDocument(
        id="run-1",
        title="Manual",
        source_file="manual.pdf",
        metadata=ManualMetadata(
            pages_total=1,
            pages_processed=[1],
            profile=DocumentProfile.DIGITAL_OUTLINE,
            parser="docling",
            structure_strategy="embedded_outline",
            toc_available=True,
        ),
    )
    assert manual.schema_version == "1.1"


def test_non_table_elements_reject_table_serialization() -> None:
    with pytest.raises(ValidationError, match="only valid for table elements"):
        ManualElement(
            id="text-1",
            type="text",
            page=1,
            source=SourceReference(page=1),
            text="hello",
            table_serialization=TableSerialization(
                status=TableSerializationStatus.UNAVAILABLE,
            ),
        )


def test_table_serialization_contract_requires_status_coherent_rows() -> None:
    with pytest.raises(ValidationError, match="structured.*requires rows"):
        TableSerialization(status=TableSerializationStatus.STRUCTURED)

    with pytest.raises(ValidationError, match="exactly one row"):
        TableSerialization(
            status=TableSerializationStatus.FALLBACK,
            rows=[
                TableSerializedRow(id="row-1", row_index=1, serialized_text="one"),
                TableSerializedRow(id="row-2", row_index=2, serialized_text="two"),
            ],
        )


def test_table_serialization_contract_requires_consecutive_row_indexes() -> None:
    with pytest.raises(ValidationError, match="consecutive and one-based"):
        TableSerialization(
            status=TableSerializationStatus.STRUCTURED,
            rows=[TableSerializedRow(id="row-2", row_index=2, serialized_text="two")],
        )


def test_toc_levels_are_one_based() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        TocEntry(level=0, title="Synthetic root", page=1)


def test_detected_capabilities_reject_incoherent_profile_evidence() -> None:
    with pytest.raises(ValidationError, match="requires text and no embedded outline"):
        DetectedCapabilities(
            profile=DocumentProfile.DIGITAL_RECONSTRUCTED,
            embedded_outline=True,
            outline_entries=1,
            text_layer=True,
            sampled_pages=[1],
            pages_with_text=1,
            median_text_characters=100,
            profile_confidence=1,
        )


def test_excluded_elements_require_a_reason() -> None:
    with pytest.raises(ValidationError, match="requires an exclusion_reason"):
        TraceMetadata(include_in_rag=False)


def test_heading_level_is_positive_when_present() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        TraceMetadata(heading_level=0)


@pytest.mark.parametrize(
    "run_id",
    ["", " ", ".", "..", "../run", "run/child", "run\\child", "run\nchild"],
)
def test_run_id_must_be_a_safe_nonempty_component(run_id: str) -> None:
    with pytest.raises(ValidationError, match="run_id"):
        ValidationReport(
            run_id=run_id,
            status="passed",
            checks=[ValidationCheck(id="contract", passed=True, message="ok")],
        )


def test_artifact_paths_include_a_safe_diagnostics_directory() -> None:
    artifacts = ArtifactPaths(diagnostics="reports/diagnostics")

    assert artifacts.diagnostics == "reports/diagnostics"


@pytest.mark.parametrize(
    "diagnostics",
    ["", " ", ".", "../diagnostics", "\\diagnostics", "C:\\diagnostics"],
)
def test_artifact_paths_reject_unsafe_diagnostics(diagnostics: str) -> None:
    with pytest.raises(ValidationError, match="artifact paths"):
        ArtifactPaths(diagnostics=diagnostics)


@pytest.mark.parametrize(
    "overrides",
    [
        {"toc": "manual.json"},
        {"manual": "assets/manual.json"},
        {"diagnostics": "assets/diagnostics"},
        {"manual": "run.json"},
    ],
)
def test_artifact_paths_reject_exact_nested_and_reserved_collisions(
    overrides: dict[str, str],
) -> None:
    with pytest.raises(ValidationError, match="artifact paths collide"):
        ArtifactPaths(**overrides)


def test_experimental_manifest_requires_validation_artifact() -> None:
    now = datetime.now(UTC).isoformat()
    with pytest.raises(ValidationError, match="experimental run requires artifacts.validation"):
        RunManifest(
            run_id="experimental-run",
            status=RunStatus.EXPERIMENTAL,
            source=SourceDocument(
                file="manual.pdf",
                sha256=SHA256,
                size_bytes=1,
                pages_total=1,
                detected=capabilities(),
            ),
            pipeline=PipelineSelection(
                profile=DocumentProfile.DIGITAL_OUTLINE,
                parser="docling",
                structure_strategy="embedded_outline",
            ),
            started_at=now,
            completed_at=now,
            warnings=["bounded evidence only"],
        )


def test_successful_finished_manifest_cannot_declare_errors() -> None:
    now = datetime.now(UTC).isoformat()
    with pytest.raises(ValidationError, match="cannot declare errors"):
        RunManifest(
            run_id="run-1",
            status=RunStatus.VALIDATED,
            source=SourceDocument(
                file="manual.pdf",
                sha256=SHA256,
                size_bytes=1,
                pages_total=1,
                detected=capabilities(),
            ),
            pipeline=PipelineSelection(
                profile=DocumentProfile.DIGITAL_OUTLINE,
                parser="docling",
                structure_strategy="embedded_outline",
            ),
            artifacts=ArtifactPaths(validation="validation.json"),
            started_at=now,
            completed_at=now,
            errors=["catastrophic parser error"],
        )
