from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from manual_ingestion.diagnostics import (
    CropFilterDiagnostic,
    DetectionDiagnostic,
    EnrichmentDiagnostic,
    RuntimeDiagnostic,
    StructureDiagnostic,
)
from manual_ingestion.models import (
    ArtifactPaths,
    Chapter,
    DetectedCapabilities,
    DocumentProfile,
    ElementType,
    ManualDocument,
    ManualElement,
    ManualMetadata,
    PipelineSelection,
    RunManifest,
    RunStatus,
    SourceDocument,
    SourceReference,
    TocEntry,
    TraceMetadata,
)
from manual_ingestion.run_bundle import (
    RunBundleExistsError,
    RunBundleWorkspace,
    write_run_bundle,
)
from manual_ingestion.promotion import build_promotion_assessment
from manual_ingestion.validation import build_validation_report, validate_run_bundle


def _bundle_contracts(
    *,
    artifacts: ArtifactPaths | None = None,
) -> tuple[RunManifest, ManualDocument, list[TocEntry]]:
    detected = DetectedCapabilities(
        profile=DocumentProfile.DIGITAL_OUTLINE,
        embedded_outline=True,
        outline_entries=1,
        text_layer=True,
        sampled_pages=[1],
        pages_with_text=1,
        median_text_characters=42,
        profile_confidence=1,
        reasons=["embedded outline found"],
    )
    now = datetime.now(UTC).isoformat()
    manifest = RunManifest(
        run_id="run-1",
        status=RunStatus.COMPLETED,
        source=SourceDocument(
            file="manual.pdf",
            sha256="b" * 64,
            size_bytes=10,
            pages_total=1,
            detected=detected,
        ),
        pipeline=PipelineSelection(
            profile=DocumentProfile.DIGITAL_OUTLINE,
            parser="docling",
            structure_strategy="embedded_outline",
            enrichment_provider="ollama",
            enrichment_model="qwen3.5:4b",
            config={
                "routing": {
                    "mode": "automatic",
                    "strategy": "pdf_capability_detection_v1",
                    "detected_profile": "digital_outline",
                    "profile_confidence": 1,
                },
                "pages": {"selection": "full_document", "processed": [1]},
                "parser": {
                    "name": "docling",
                    "runtime": {
                        "manual_ingestion": "1.0.0",
                        "python": "3.12.13",
                        "pydantic": "2.13.4",
                        "pymupdf": "1.28.0",
                        "pillow": "11.3.0",
                        "docling": "2.112.0",
                    },
                    "config": None,
                },
                "structure": {"strategy": "embedded_outline", "config": None},
                "postprocess": {
                    "visual_crop_filter": {
                        "strategy": "bbox_area_fraction_v1",
                        "threshold": 0.02,
                    }
                },
                "enrichment": {
                    "enabled": True,
                    "prompt_version": "technical-caption-v2",
                    "max_caption_characters": 1200,
                    "provider_config": {
                        "model": "qwen3.5:4b",
                        "temperature": 0.0,
                        "num_predict": 512,
                        "repeat_penalty": 1.15,
                        "num_ctx": 4096,
                        "thinking": False,
                    },
                    "preflight": {
                        "model": "qwen3.5:4b",
                        "version": "0.31.2",
                        "model_digest": "2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd",
                    },
                }
            },
        ),
        artifacts=artifacts or ArtifactPaths(),
        started_at=now,
        completed_at=now,
    )
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
        content=[
            Chapter(
                id="chapter-1",
                title="One",
                level=1,
                page=1,
                content=[
                    ManualElement(
                        id="text-1",
                        type=ElementType.TEXT,
                        page=1,
                        source=SourceReference(page=1),
                        text="Canonical page content",
                        trace=TraceMetadata(parent_chapter_id="chapter-1"),
                    )
                ],
            )
        ],
    )
    return manifest, manual, [TocEntry(level=1, title="One", page=1)]


def test_write_run_bundle_is_complete(tmp_path) -> None:
    detected = DetectedCapabilities(
        profile=DocumentProfile.DIGITAL_OUTLINE,
        embedded_outline=True,
        outline_entries=1,
        text_layer=True,
        sampled_pages=[1],
        pages_with_text=1,
        median_text_characters=42,
        profile_confidence=1,
        reasons=["embedded outline found"],
    )
    now = datetime.now(UTC).isoformat()
    manifest = RunManifest(
        run_id="run-1",
        status=RunStatus.COMPLETED,
        source=SourceDocument(
            file="manual.pdf",
            sha256="b" * 64,
            size_bytes=10,
            pages_total=1,
            detected=detected,
        ),
        pipeline=PipelineSelection(
            profile=DocumentProfile.DIGITAL_OUTLINE,
            parser="docling",
            structure_strategy="embedded_outline",
        ),
        started_at=now,
        completed_at=now,
    )
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

    output = write_run_bundle(tmp_path / "run-1", manifest=manifest, manual=manual, toc=[TocEntry(level=1, title="One", page=1)])

    assert json.loads((output / "run.json").read_text())["run_id"] == "run-1"
    assert json.loads((output / "manual.json").read_text())["schema_version"] == "1.1"
    assert json.loads((output / "toc.json").read_text())[0]["title"] == "One"
    assert (output / "assets" / "pages").is_dir()


def test_validated_bundle_requires_validation(tmp_path) -> None:
    detected = DetectedCapabilities(
        profile=DocumentProfile.DIGITAL_OUTLINE,
        embedded_outline=True,
        outline_entries=1,
        text_layer=True,
        sampled_pages=[1],
        pages_with_text=1,
        median_text_characters=42,
        profile_confidence=1,
    )
    now = datetime.now(UTC).isoformat()
    manifest = RunManifest(
        run_id="run-1",
        status=RunStatus.VALIDATED,
        source=SourceDocument(file="manual.pdf", sha256="b" * 64, size_bytes=10, pages_total=1, detected=detected),
        pipeline=PipelineSelection(profile=DocumentProfile.DIGITAL_OUTLINE, parser="docling", structure_strategy="embedded_outline"),
        artifacts=ArtifactPaths(validation="validation.json"),
        started_at=now,
        completed_at=now,
    )
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

    with pytest.raises(ValueError, match="requires validation"):
        write_run_bundle(tmp_path / "run-1", manifest=manifest, manual=manual, toc=[])


def test_workspace_preserves_profile_assets_and_diagnostics_without_copying(tmp_path) -> None:
    manifest, manual, toc = _bundle_contracts()
    output_dir = tmp_path / "run-1"

    with RunBundleWorkspace(output_dir, artifacts=manifest.artifacts) as workspace:
        staging = workspace.staging_dir
        assert staging.parent == output_dir.parent
        assert staging.name.startswith(".run-1.staging-")
        image = workspace.assets_dir / "images" / "figure.png"
        diagnostic = workspace.diagnostics_dir / "parser.json"
        image.write_bytes(b"image")
        diagnostic.write_text('{"parser": "docling"}', encoding="utf-8")
        staged_image_inode = image.stat().st_ino

        workspace.stage_bundle(manifest=manifest, manual=manual, toc=toc)
        assert not output_dir.exists()
        published = workspace.publish()

    assert published == output_dir
    assert (published / "assets" / "images" / "figure.png").read_bytes() == b"image"
    assert (published / "assets" / "images" / "figure.png").stat().st_ino == staged_image_inode
    assert (published / "diagnostics" / "parser.json").is_file()
    assert not staging.exists()


@pytest.mark.parametrize("raised", [RuntimeError("stop"), KeyboardInterrupt()])
def test_workspace_cleans_staging_on_exception_and_interrupt(tmp_path, raised) -> None:
    output_dir = tmp_path / "run-1"
    staging = None

    with pytest.raises(type(raised)):
        with RunBundleWorkspace(output_dir) as workspace:
            staging = workspace.staging_dir
            (workspace.diagnostics_dir / "partial.txt").write_text("partial", encoding="utf-8")
            raise raised

    assert staging is not None
    assert not staging.exists()
    assert not output_dir.exists()


def test_workspace_never_overwrites_a_target_created_before_publish(tmp_path) -> None:
    manifest, manual, toc = _bundle_contracts()
    output_dir = tmp_path / "run-1"

    with RunBundleWorkspace(output_dir) as workspace:
        workspace.stage_bundle(manifest=manifest, manual=manual, toc=toc)
        staging = workspace.staging_dir
        output_dir.mkdir()
        marker = output_dir / "owner.txt"
        marker.write_text("preexisting", encoding="utf-8")

        with pytest.raises(RunBundleExistsError):
            workspace.publish()

    assert marker.read_text(encoding="utf-8") == "preexisting"
    assert not staging.exists()


def test_backward_compatible_writer_can_publish_a_populated_workspace(tmp_path) -> None:
    manifest, manual, toc = _bundle_contracts()
    output_dir = tmp_path / "run-1"

    with RunBundleWorkspace(output_dir) as workspace:
        diagnostic = workspace.diagnostics_dir / "profile.log"
        diagnostic.write_text("profile result", encoding="utf-8")
        published = write_run_bundle(
            output_dir,
            manifest=manifest,
            manual=manual,
            toc=toc,
            workspace=workspace,
        )

    assert (published / "diagnostics" / "profile.log").read_text(encoding="utf-8") == "profile result"


@pytest.mark.parametrize("final_status", [RunStatus.VALIDATED, RunStatus.EXPERIMENTAL])
def test_workspace_supports_final_state_validation_before_publish(
    tmp_path,
    final_status: RunStatus,
) -> None:
    draft_manifest, manual, toc = _bundle_contracts()
    output_dir = tmp_path / "run-1"

    with RunBundleWorkspace(output_dir) as workspace:
        workspace.stage_bundle(manifest=draft_manifest, manual=manual, toc=toc)
        draft_report = validate_run_bundle(workspace.root)
        assert draft_report.status == "passed"

        final_raw = draft_manifest.model_dump(mode="json")
        final_raw["status"] = final_status.value
        final_raw["artifacts"]["validation"] = "validation.json"
        profile_warnings = (
            ["bounded OCR evidence"]
            if final_status is RunStatus.EXPERIMENTAL
            else []
        )
        final_raw["warnings"] = profile_warnings
        final_manifest = RunManifest.model_validate(final_raw)
        enrichment = EnrichmentDiagnostic(
            provider_configured=True,
            provider_preflight={
                "model": "qwen3.5:4b",
                "version": "0.31.2",
                "model_digest": "2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd",
            },
            enriched=0,
            skipped=0,
            failed=0,
            review_required=0,
            items=[],
        )
        structure = StructureDiagnostic(
            strategy="embedded_outline",
            toc_available=True,
            details_available=False,
        )
        detection = DetectionDiagnostic(capabilities=final_manifest.source.detected)
        crop_filter = CropFilterDiagnostic(
            strategy="bbox_area_fraction_v1",
            threshold=0.02,
            excluded=0,
            kept=0,
            preserved=0,
            unassessed=0,
            decisions=[],
        )
        runtime = RuntimeDiagnostic(
            profile_runtime=final_manifest.pipeline.config["parser"]["runtime"],
            provider_runtime=enrichment.provider_preflight,
            paddle_runtime_config=None,
        )
        promotion = build_promotion_assessment(
            manifest=final_manifest,
            manual=manual,
            enrichment=enrichment,
            structure=structure,
            profile_warnings=profile_warnings,
        )
        for name, diagnostic in (
            ("detection.json", detection),
            ("crop_filter.json", crop_filter),
            ("enrichment.json", enrichment),
            ("structure.json", structure),
            ("promotion.json", promotion),
            ("runtime.json", runtime),
        ):
            (workspace.diagnostics_dir / name).write_text(
                diagnostic.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
        workspace.stage_bundle(
            manifest=final_manifest,
            manual=manual,
            toc=toc,
            validation=draft_report,
            replace=True,
        )

        canonical_report = build_validation_report(workspace.root)
        assert canonical_report.status == "passed"
        workspace.stage_bundle(
            manifest=final_manifest,
            manual=manual,
            toc=toc,
            validation=canonical_report,
            replace=True,
        )
        final_report = validate_run_bundle(workspace.root)
        assert final_report.status == "passed"
        assert final_report == canonical_report
        published = workspace.publish()

    assert published == output_dir
    published_report = validate_run_bundle(published)
    assert published_report.status == "passed"
    assert published_report == final_report
