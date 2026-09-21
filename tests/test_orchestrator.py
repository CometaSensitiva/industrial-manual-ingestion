from __future__ import annotations

import json
import inspect
import os
from pathlib import Path

import pymupdf
import pytest

import manual_ingestion.validation as validation_module
import manual_ingestion.orchestrator as orchestrator_module
from manual_ingestion.enrichment import CaptionRequest, CaptionResponse
from manual_ingestion.models import (
    BoundingBox,
    Chapter,
    DetectedCapabilities,
    DocumentProfile,
    ElementType,
    ManualDocument,
    ManualElement,
    ManualMetadata,
    PageSize,
    SourceReference,
    TocEntry,
    TraceMetadata,
    ValidationCheck,
    ValidationReport,
)
from manual_ingestion.orchestrator import (
    IngestionError,
    _ingest_manual as ingest_manual,
    ingest_manual as public_ingest_manual,
)
from manual_ingestion.profiles.base import ProfileResult
from manual_ingestion.profiles.scanned_ocr import ScannedOCRResult
from manual_ingestion.progress import ProgressEvent, ProgressStage, ProgressState
from manual_ingestion.providers.ollama import (
    ACCEPTED_OLLAMA_MODEL,
    ACCEPTED_OLLAMA_MODEL_DIGEST,
    ACCEPTED_OLLAMA_OUTPUT_PARAMS,
    ACCEPTED_OLLAMA_VERSION,
)
from manual_ingestion.run_bundle import RunBundleExistsError
from manual_ingestion.structure import reconstruct_toc_from_ocr_elements
from manual_ingestion.validation import build_validation_report, validate_run_bundle


VALID_CAPTION = """Tipo tecnico: componente/assemblaggio
Oggetto: gruppo meccanico
Elementi visibili: supporto, vite
Relazioni/funzione: la vite fissa il supporto
Valori/avvertenze: non applicabile
Rilevanza RAG: come si fissa il supporto?
Incertezze: nessuna evidente"""


def test_public_ingestion_api_does_not_expose_routing_or_validator_overrides() -> None:
    parameters = inspect.signature(public_ingest_manual).parameters
    assert "progress" in parameters
    assert "page_previews" in parameters
    assert "detector" not in parameters
    assert "profile_builder" not in parameters
    assert "validator" not in parameters


class FakeProvider:
    name = "ollama"

    def __init__(
        self,
        *,
        fail_preflight: bool = False,
        fail_caption: bool = False,
        caption_text: str = VALID_CAPTION,
    ) -> None:
        self.fail_preflight = fail_preflight
        self.fail_caption = fail_caption
        self.caption_text = caption_text
        self.events: list[str] = []

    def preflight(self) -> dict[str, str]:
        self.events.append("provider.preflight")
        if self.fail_preflight:
            raise RuntimeError("model unavailable")
        return {
            "version": ACCEPTED_OLLAMA_VERSION,
            "model": ACCEPTED_OLLAMA_MODEL,
            "model_digest": ACCEPTED_OLLAMA_MODEL_DIGEST,
        }

    def public_config(self) -> dict[str, object]:
        return {
            "model": ACCEPTED_OLLAMA_MODEL,
            "base_url": "http://127.0.0.1:11434",
            "timeout_seconds": 180,
            **ACCEPTED_OLLAMA_OUTPUT_PARAMS,
        }

    def caption(self, request: CaptionRequest) -> CaptionResponse:
        self.events.append(f"provider.caption:{request.element_id}")
        if self.fail_caption:
            raise RuntimeError("caption failure")
        return CaptionResponse(
            text=self.caption_text,
            provider="ollama",
            model=ACCEPTED_OLLAMA_MODEL,
            params=dict(ACCEPTED_OLLAMA_OUTPUT_PARAMS),
            runtime_version=ACCEPTED_OLLAMA_VERSION,
            done_reason="stop",
        )


class FakeDigitalResult(ProfileResult):
    # This fixture simulates Docling; tests must not require its heavyweight runtime.
    runtime = {"docling": validation_module.ACCEPTED_DOCLING_VERSION}


class FakeProfile:
    def __init__(
        self,
        profile: DocumentProfile,
        *,
        events: list[str] | None = None,
        mutate_source: bool = False,
        stealth_mutation: bool = False,
        write_paddle_paths: bool = False,
        warnings: list[str] | None = None,
        table_markdown: str | None = None,
    ) -> None:
        self.profile = profile
        self.events = events if events is not None else []
        self.mutate_source = mutate_source
        self.stealth_mutation = stealth_mutation
        self.write_paddle_paths = write_paddle_paths
        self.warnings = warnings or []
        self.table_markdown = table_markdown
        self.requested_pages: list[int] | None = None

    def run(
        self,
        pdf_path: str | Path,
        *,
        run_id: str,
        assets_dir: str | Path,
        pages: list[int] | None = None,
        title: str | None = None,
        language: str = "it",
    ) -> ProfileResult:
        self.events.append("profile.run")
        source = Path(pdf_path)
        assets = Path(assets_dir)
        with pymupdf.open(source) as document:
            pages_total = document.page_count
        selected = list(range(1, pages_total + 1)) if pages is None else list(pages)
        self.requested_pages = selected
        image_path = assets / "images" / "figure.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(b"fake-image")
        if self.write_paddle_paths:
            diagnostic = assets.parent / "diagnostics" / "paddle" / "worker_request.json"
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            diagnostic.write_text(
                json.dumps(
                    {
                        "inside": str(assets / "pages" / "page_0001.png"),
                        "outside": "/external/runtime/model",
                    }
                ),
                encoding="utf-8",
            )

        chapter_content = [
            ManualElement(
                id="image-1",
                type=ElementType.IMAGE,
                page=selected[0],
                source=SourceReference(
                    page=selected[0],
                    page_size=PageSize(width=100, height=100),
                    bbox=BoundingBox(x0=10, y0=10, x1=70, y1=70),
                ),
                image_path="assets/images/figure.png",
                trace=TraceMetadata(parent_chapter_id="chapter-1"),
            ),
            *[
                ManualElement(
                    id=f"text-{page}",
                    type=ElementType.TEXT,
                    page=page,
                    source=SourceReference(page=page),
                    text=f"Content from page {page}",
                    trace=TraceMetadata(parent_chapter_id="chapter-1"),
                )
                for page in selected[1:]
            ],
        ]
        if self.table_markdown is not None:
            chapter_content.append(
                ManualElement(
                    id="table-1",
                    type=ElementType.TABLE,
                    page=selected[0],
                    source=SourceReference(page=selected[0]),
                    table_markdown=self.table_markdown,
                    trace=TraceMetadata(parent_chapter_id="chapter-1"),
                )
            )
        scanned_reconstruction = None
        if self.profile is DocumentProfile.SCANNED_OCR:
            chapter_content = [
                ManualElement(
                    id="contents-title",
                    type=ElementType.TITLE,
                    page=1,
                    source=SourceReference(page=1),
                    text="CONTENTS",
                    trace=TraceMetadata(
                        reading_order=0,
                        heading_level=1,
                        parent_chapter_id="chapter-1",
                    ),
                ),
                ManualElement(
                    id="contents-row",
                    type=ElementType.TEXT,
                    page=1,
                    source=SourceReference(page=1),
                    text="Safety ................ 1",
                    trace=TraceMetadata(
                        reading_order=1,
                        parent_chapter_id="chapter-1",
                    ),
                ),
                *chapter_content,
                ManualElement(
                    id="safety-title",
                    type=ElementType.TITLE,
                    page=2,
                    source=SourceReference(page=2),
                    text="Safety",
                    trace=TraceMetadata(
                        reading_order=2,
                        heading_level=1,
                        parent_chapter_id="chapter-1",
                    ),
                ),
            ]
            scanned_reconstruction = reconstruct_toc_from_ocr_elements(
                chapter_content,
                pages_total=pages_total,
            )
        chapter = Chapter(
            id="chapter-1",
            title="Safety",
            level=1,
            page=(2 if self.profile is DocumentProfile.SCANNED_OCR else 1),
            content=chapter_content,
        )
        manual = ManualDocument(
            id=run_id,
            title=title.strip() if title else source.stem,
            source_file=source.name,
            language=language,
            metadata=ManualMetadata(
                pages_total=pages_total,
                pages_processed=selected,
                profile=self.profile,
                parser=("paddleocr_vl" if self.profile is DocumentProfile.SCANNED_OCR else "docling"),
                structure_strategy=(
                    "ocr_printed_toc_title_fusion_v1"
                    if self.profile is DocumentProfile.SCANNED_OCR
                    else "embedded_outline"
                ),
                toc_available=True,
            ),
            content=[chapter],
        )
        if self.mutate_source:
            if self.stealth_mutation:
                stat = source.stat()
                payload = bytearray(source.read_bytes())
                payload[max(1, len(payload) // 2)] ^= 1
                source.write_bytes(payload)
                os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            else:
                source.write_bytes(source.read_bytes() + b"changed")
        result_args = {
            "manual": manual,
            "toc": (
                list(scanned_reconstruction.entries)
                if scanned_reconstruction is not None
                else [TocEntry(level=1, title="Safety", page=1, confidence=1)]
            ),
            "warnings": list(self.warnings),
        }
        if self.profile is DocumentProfile.SCANNED_OCR:
            return ScannedOCRResult(
                **result_args,
                diagnostics=scanned_reconstruction.diagnostics,
                runtime={
                    "python": "3.12.13",
                    "paddleocr": "3.7.0",
                    "paddlepaddle": "3.2.1",
                    "paddlex": "3.7.2",
                },
            )
        return FakeDigitalResult(
            **result_args,
        )


def test_full_digital_ingestion_publishes_validated_atomic_bundle(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=2)
    target = tmp_path / "runs" / "run-1"
    events: list[str] = []
    provider = FakeProvider()
    provider.events = events
    profile = FakeProfile(DocumentProfile.DIGITAL_OUTLINE, events=events)
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)

    outcome = ingest_manual(
        source,
        target,
        run_id="run-1",
        provider=provider,
        detector=lambda path: detected,
        profile_builder=_builder(profile, detected),
    )

    assert outcome.output_dir == target.resolve()
    assert outcome.manifest.status.value == "validated"
    assert outcome.validation.status == "passed"
    assert validate_run_bundle(target) == outcome.validation
    assert events[0:2] == ["provider.preflight", "profile.run"]
    assert (target / "assets" / "images" / "figure.png").is_file()
    assert {
        "detection.json",
        "crop_filter.json",
        "enrichment.json",
        "structure.json",
        "runtime.json",
    } <= {path.name for path in (target / "diagnostics").iterdir()}
    run = json.loads((target / "run.json").read_text(encoding="utf-8"))
    assert run["source"]["detected"]["profile"] == "digital_outline"
    assert run["pipeline"]["enrichment_provider"] == "ollama"
    assert run["pipeline"]["enrichment_model"] == ACCEPTED_OLLAMA_MODEL
    assert run["pipeline"]["config"]["routing"]["mode"] == "automatic"
    structure = json.loads(
        (target / "diagnostics" / "structure.json").read_text(encoding="utf-8")
    )
    assert structure["toc_available"] is True
    assert structure["details_available"] is False
    manual = json.loads((target / "manual.json").read_text(encoding="utf-8"))
    assert manual["content"][0]["content"][0]["caption_generated"] == VALID_CAPTION


def test_requested_page_previews_are_rendered_inside_the_bundle(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=2)
    target = tmp_path / "run"
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)

    ingest_manual(
        source,
        target,
        run_id="run-1",
        provider=FakeProvider(),
        page_previews=True,
        detector=lambda path: detected,
        profile_builder=_builder(
            FakeProfile(DocumentProfile.DIGITAL_OUTLINE),
            detected,
        ),
    )

    previews = sorted((target / "assets" / "pages").glob("page_*.png"))
    assert [path.name for path in previews] == ["page_0001.png", "page_0002.png"]
    preview = pymupdf.Pixmap(previews[0])
    assert preview.width == 450
    assert preview.height == 600


def test_progress_reports_the_complete_successful_pipeline_in_order(
    tmp_path: Path,
) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=1)
    target = tmp_path / "run"
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)
    progress: list[ProgressEvent] = []

    ingest_manual(
        source,
        target,
        run_id="run-1",
        provider=FakeProvider(),
        progress=progress.append,
        detector=lambda path: detected,
        profile_builder=_builder(
            FakeProfile(DocumentProfile.DIGITAL_OUTLINE),
            detected,
        ),
    )

    lifecycle = [
        (event.stage, event.state)
        for event in progress
        if event.state is not ProgressState.UPDATED
    ]
    assert lifecycle == [
        (stage, state)
        for stage in ProgressStage
        for state in (ProgressState.STARTED, ProgressState.COMPLETED)
    ]
    enrichment_updates = [
        event
        for event in progress
        if event.stage is ProgressStage.ENRICHMENT
        and event.state is ProgressState.UPDATED
    ]
    assert [(event.current, event.total, event.unit) for event in enrichment_updates] == [
        (1, 1, "elements")
    ]
    extraction_start = next(
        event
        for event in progress
        if event.stage is ProgressStage.EXTRACTION
        and event.state is ProgressState.STARTED
    )
    assert extraction_start.current is None
    assert extraction_start.total is None


def test_caption_failure_completes_pipeline_as_experimental_bundle(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=1)
    target = tmp_path / "run"
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)
    progress: list[ProgressEvent] = []

    outcome = ingest_manual(
        source,
        target,
        run_id="run-1",
        provider=FakeProvider(fail_caption=True),
        progress=progress.append,
        detector=lambda path: detected,
        profile_builder=_builder(
            FakeProfile(DocumentProfile.DIGITAL_OUTLINE),
            detected,
        ),
    )

    assert outcome.manifest.status.value == "experimental"
    assert progress[-1].stage is ProgressStage.PUBLICATION
    assert progress[-1].state is ProgressState.COMPLETED
    manual = json.loads((target / "manual.json").read_text(encoding="utf-8"))
    image = manual["content"][0]["content"][0]
    assert image["caption_generated"] is None
    assert image["trace"]["include_in_rag"] is False
    assert image["trace"]["exclusion_reason"] == "caption unavailable after enrichment failure"


def test_no_provider_enrichment_stage_is_explicitly_skipped(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=1)
    target = tmp_path / "run"
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)
    progress: list[ProgressEvent] = []

    ingest_manual(
        source,
        target,
        run_id="run-1",
        provider=None,
        progress=progress.append,
        detector=lambda path: detected,
        profile_builder=_builder(
            FakeProfile(DocumentProfile.DIGITAL_OUTLINE),
            detected,
        ),
    )

    completed = next(
        event
        for event in progress
        if event.stage is ProgressStage.ENRICHMENT
        and event.state is ProgressState.COMPLETED
    )
    assert completed.current == completed.total == 0
    assert completed.unit == "elements"
    assert completed.detail == "skipped: no caption provider configured"


def test_table_serialization_is_persisted_and_declared_by_the_pipeline(
    tmp_path: Path,
) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=1)
    target = tmp_path / "run"
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)
    profile = FakeProfile(
        DocumentProfile.DIGITAL_OUTLINE,
        table_markdown="| Codice | Azione |\n|---|---|\n| E1 | Reset |",
    )

    outcome = ingest_manual(
        source,
        target,
        run_id="run-1",
        provider=FakeProvider(),
        detector=lambda path: detected,
        profile_builder=_builder(profile, detected),
    )

    assert outcome.manifest.status.value == "validated"
    assert outcome.manifest.pipeline.config["table_serialization"] == {
        "strategy": "header_value_rows_v1"
    }
    manual = json.loads((target / "manual.json").read_text(encoding="utf-8"))
    table = manual["content"][0]["content"][-1]
    assert table["table_markdown"] == profile.table_markdown
    assert table["caption_generated"] is None
    assert table["table_serialization"]["status"] == "structured"
    assert "Codice: E1" in table["table_serialization"]["rows"][0][
        "serialized_text"
    ]


def test_table_serialization_strategy_is_declared_even_without_tables(
    tmp_path: Path,
) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=1)
    target = tmp_path / "run"
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)

    outcome = ingest_manual(
        source,
        target,
        run_id="run-1",
        provider=FakeProvider(),
        detector=lambda path: detected,
        profile_builder=_builder(
            FakeProfile(DocumentProfile.DIGITAL_OUTLINE),
            detected,
        ),
    )

    assert outcome.manifest.pipeline.config["table_serialization"] == {
        "strategy": "header_value_rows_v1"
    }


def test_interrupt_from_completion_callback_cannot_hide_a_published_bundle(
    tmp_path: Path,
) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=1)
    target = tmp_path / "run"
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)

    def interrupt_after_publish(event: ProgressEvent) -> None:
        if (
            event.stage is ProgressStage.PUBLICATION
            and event.state is ProgressState.COMPLETED
        ):
            raise KeyboardInterrupt

    outcome = ingest_manual(
        source,
        target,
        run_id="run-1",
        provider=FakeProvider(),
        progress=interrupt_after_publish,
        detector=lambda path: detected,
        profile_builder=_builder(
            FakeProfile(DocumentProfile.DIGITAL_OUTLINE),
            detected,
        ),
    )

    assert outcome.output_dir == target.resolve()
    assert target.is_dir()
    assert not list(tmp_path.glob(".run.staging-*"))


def test_retrievable_table_fallback_forces_experimental_status(
    tmp_path: Path,
) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=1)
    target = tmp_path / "run"
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)
    profile = FakeProfile(
        DocumentProfile.DIGITAL_OUTLINE,
        table_markdown="Codice E1 Azione Reset",
    )

    outcome = ingest_manual(
        source,
        target,
        run_id="run-1",
        provider=FakeProvider(),
        detector=lambda path: detected,
        profile_builder=_builder(profile, detected),
    )

    assert outcome.manifest.status.value == "experimental"
    assert any(
        "Table serialization fallback" in warning
        for warning in outcome.manifest.warnings
    )


@pytest.mark.parametrize(
    ("provider", "expected_reason"),
    [
        (None, "no caption provider"),
        (FakeProvider(), "partial run"),
    ],
)
def test_diagnostic_or_partial_run_is_explicitly_experimental(
    tmp_path: Path,
    provider: FakeProvider | None,
    expected_reason: str,
) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=2)
    target = tmp_path / "run"
    profile = FakeProfile(DocumentProfile.DIGITAL_OUTLINE)
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)

    outcome = ingest_manual(
        source,
        target,
        run_id="experimental-run",
        provider=provider,
        pages=None if provider is None else [1],
        detector=lambda path: detected,
        profile_builder=_builder(profile, detected),
    )

    assert outcome.manifest.status.value == "experimental"
    assert outcome.validation.status == "passed"
    assert any(expected_reason in warning.casefold() for warning in outcome.manifest.warnings)
    if provider is None:
        manual = json.loads((target / "manual.json").read_text(encoding="utf-8"))
        trace = manual["content"][0]["content"][0]["trace"]
        assert trace["include_in_rag"] is False
        assert trace["exclusion_reason"] == "caption provider unavailable in diagnostic run"


def test_scanned_profile_remains_experimental_before_acceptance_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        orchestrator_module,
        "SCANNED_OCR_ACCEPTANCE_EVIDENCE_ID",
        None,
    )
    source = _pdf(tmp_path / "scan.pdf", pages=2)
    target = tmp_path / "scan-run"
    detected = _detected(DocumentProfile.SCANNED_OCR)
    profile = FakeProfile(
        DocumentProfile.SCANNED_OCR,
        write_paddle_paths=True,
    )

    outcome = ingest_manual(
        source,
        target,
        run_id="scan-run",
        provider=FakeProvider(),
        detector=lambda path: detected,
        profile_builder=_builder(profile, detected),
    )

    assert outcome.manifest.status.value == "experimental"
    assert any("acceptance gate" in warning for warning in outcome.manifest.warnings)
    diagnostic = json.loads(
        (target / "diagnostics" / "paddle" / "worker_request.json").read_text(
            encoding="utf-8"
        )
    )
    assert diagnostic["inside"] == "assets/pages/page_0001.png"
    assert diagnostic["outside"] == "/external/runtime/model"
    serialized_manifest = (target / "run.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in serialized_manifest


def test_scanned_profile_is_promoted_by_recorded_acceptance_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator_module, "SCANNED_OCR_ACCEPTANCE_EVIDENCE_ID", "synthetic-reviewed-setup")
    monkeypatch.setattr(validation_module, "SCANNED_OCR_ACCEPTANCE_EVIDENCE_IDS", frozenset({"synthetic-reviewed-setup"}))
    target = _experimental_scanned_bundle(tmp_path)
    run = json.loads((target / "run.json").read_text(encoding="utf-8"))
    promotion = json.loads(
        (target / "diagnostics" / "promotion.json").read_text(encoding="utf-8")
    )

    assert run["status"] == "validated"
    assert promotion["acceptance_evidence_id"] == (
        "synthetic-reviewed-setup"
    )
    assert promotion["eligible_for_validated"] is True


def test_consequential_digital_structure_warning_forces_experimental_status(
    tmp_path: Path,
) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=1)
    target = tmp_path / "run"
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)
    profile = FakeProfile(
        DocumentProfile.DIGITAL_OUTLINE,
        warnings=["outline anchor ambiguous on page 1"],
    )

    outcome = ingest_manual(
        source,
        target,
        run_id="run-1",
        provider=FakeProvider(),
        detector=lambda path: detected,
        profile_builder=_builder(profile, detected),
    )

    assert outcome.manifest.status.value == "experimental"
    assert any(
        "consequential parser" in warning for warning in outcome.manifest.warnings
    )


def test_manual_review_caption_is_excluded_and_forces_experimental_status(
    tmp_path: Path,
) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=1)
    target = tmp_path / "run"
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)
    profile = FakeProfile(DocumentProfile.DIGITAL_OUTLINE)
    electrical = VALID_CAPTION.replace(
        "Tipo tecnico: componente/assemblaggio",
        "Tipo tecnico: schema elettrico",
    )

    outcome = ingest_manual(
        source,
        target,
        run_id="run-1",
        provider=FakeProvider(caption_text=electrical),
        detector=lambda path: detected,
        profile_builder=_builder(profile, detected),
    )

    assert outcome.manifest.status.value == "experimental"
    assert any("require manual review" in warning for warning in outcome.manifest.warnings)
    manual = json.loads((target / "manual.json").read_text(encoding="utf-8"))
    trace = manual["content"][0]["content"][0]["trace"]
    assert trace["include_in_rag"] is False
    enrichment = json.loads(
        (target / "diagnostics" / "enrichment.json").read_text(encoding="utf-8")
    )
    assert enrichment["review_required"] == 1


def test_provider_preflight_failure_never_publishes_partial_output(
    tmp_path: Path,
) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=1)
    target = tmp_path / "run"
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)
    profile = FakeProfile(DocumentProfile.DIGITAL_OUTLINE)

    with pytest.raises(IngestionError, match="preflight failed"):
        ingest_manual(
            source,
            target,
            run_id="run-1",
            provider=FakeProvider(fail_preflight=True),
            detector=lambda path: detected,
            profile_builder=_builder(profile, detected),
        )

    assert not target.exists()
    assert not list(tmp_path.glob(".run.staging-*"))


def test_keyboard_interrupt_discards_bundle_and_staging(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=1)
    target = tmp_path / "run"
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)
    progress: list[ProgressEvent] = []

    class InterruptingProfile(FakeProfile):
        def run(self, *args, **kwargs):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        ingest_manual(
            source,
            target,
            run_id="run-1",
            provider=FakeProvider(),
            progress=progress.append,
            detector=lambda path: detected,
            profile_builder=_builder(
                InterruptingProfile(DocumentProfile.DIGITAL_OUTLINE),
                detected,
            ),
        )

    assert not target.exists()
    assert not list(tmp_path.glob(".run.staging-*"))
    assert progress[-1].stage is ProgressStage.EXTRACTION
    assert progress[-1].state is ProgressState.FAILED


def test_source_mutation_discards_staging(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=1)
    target = tmp_path / "run"
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)
    profile = FakeProfile(DocumentProfile.DIGITAL_OUTLINE, mutate_source=True)
    progress: list[ProgressEvent] = []

    with pytest.raises(IngestionError, match="changed during ingestion"):
        ingest_manual(
            source,
            target,
            run_id="run-1",
            provider=FakeProvider(),
            progress=progress.append,
            detector=lambda path: detected,
            profile_builder=_builder(profile, detected),
        )

    assert not target.exists()
    assert progress[-1].stage is ProgressStage.EXTRACTION
    assert progress[-1].state is ProgressState.FAILED


def test_source_change_before_atomic_publish_is_a_publication_failure(
    tmp_path: Path,
) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=1)
    target = tmp_path / "run"
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)
    progress: list[ProgressEvent] = []
    calls = 0

    def mutate_after_final_validation(run_dir: str | Path) -> ValidationReport:
        nonlocal calls
        calls += 1
        report = validate_run_bundle(run_dir)
        if calls == 2:
            source.write_bytes(source.read_bytes() + b"changed")
        return report

    with pytest.raises(IngestionError, match="changed during ingestion"):
        ingest_manual(
            source,
            target,
            run_id="run-1",
            provider=FakeProvider(),
            progress=progress.append,
            detector=lambda path: detected,
            profile_builder=_builder(
                FakeProfile(DocumentProfile.DIGITAL_OUTLINE),
                detected,
            ),
            validator=mutate_after_final_validation,
        )

    assert not target.exists()
    assert progress[-1].stage is ProgressStage.PUBLICATION
    assert progress[-1].state is ProgressState.FAILED


def test_same_size_mutation_with_restored_mtime_is_detected(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=1)
    target = tmp_path / "run"
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)
    profile = FakeProfile(
        DocumentProfile.DIGITAL_OUTLINE,
        mutate_source=True,
        stealth_mutation=True,
    )

    with pytest.raises(IngestionError, match="metadata changed during ingestion"):
        ingest_manual(
            source,
            target,
            run_id="run-1",
            provider=FakeProvider(),
            detector=lambda path: detected,
            profile_builder=_builder(profile, detected),
        )

    assert not target.exists()


def test_failed_bundle_validation_discards_staging(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=1)
    target = tmp_path / "run"
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)
    profile = FakeProfile(DocumentProfile.DIGITAL_OUTLINE)

    def failing_validator(run_dir: str | Path) -> ValidationReport:
        return ValidationReport(
            run_id="run-1",
            status="failed",
            checks=[ValidationCheck(id="forced", passed=False, message="forced failure")],
        )

    with pytest.raises(IngestionError, match="validation failed"):
        ingest_manual(
            source,
            target,
            run_id="run-1",
            provider=FakeProvider(),
            detector=lambda path: detected,
            profile_builder=_builder(profile, detected),
            validator=failing_validator,
        )

    assert not target.exists()


def test_failure_during_final_validation_pass_discards_staging(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=1)
    target = tmp_path / "run"
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)
    profile = FakeProfile(DocumentProfile.DIGITAL_OUTLINE)
    calls = 0

    def fail_final(run_dir: str | Path) -> ValidationReport:
        nonlocal calls
        calls += 1
        if calls == 1:
            return validate_run_bundle(run_dir)
        return ValidationReport(
            run_id="run-1",
            status="failed",
            checks=[ValidationCheck(id="final", passed=False, message="final failure")],
        )

    with pytest.raises(IngestionError, match="final pass 1 validation failed"):
        ingest_manual(
            source,
            target,
            run_id="run-1",
            provider=FakeProvider(),
            detector=lambda path: detected,
            profile_builder=_builder(profile, detected),
            validator=fail_final,
        )

    assert not target.exists()


def test_existing_target_is_rejected_before_preflight_or_profile(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=1)
    target = tmp_path / "run"
    target.mkdir()
    provider = FakeProvider()
    profile = FakeProfile(DocumentProfile.DIGITAL_OUTLINE)
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)

    with pytest.raises(RunBundleExistsError):
        ingest_manual(
            source,
            target,
            run_id="run-1",
            provider=provider,
            detector=lambda path: detected,
            profile_builder=_builder(profile, detected),
        )

    assert provider.events == []
    assert profile.events == []


def test_unapproved_provider_config_is_never_serialized(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=1)
    target = tmp_path / "run"
    provider = FakeProvider()
    provider.config = {"api_key": "do-not-persist"}  # type: ignore[attr-defined]
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)
    profile = FakeProfile(DocumentProfile.DIGITAL_OUTLINE)

    ingest_manual(
        source,
        target,
        run_id="run-1",
        provider=provider,
        detector=lambda path: detected,
        profile_builder=_builder(profile, detected),
    )

    serialized = (target / "run.json").read_text(encoding="utf-8")
    assert "do-not-persist" not in serialized
    run = json.loads(serialized)
    assert run["pipeline"]["config"]["enrichment"]["provider_config"]["model"] == (
        ACCEPTED_OLLAMA_MODEL
    )


def test_invalid_caption_returns_failed_report_instead_of_raising(tmp_path: Path) -> None:
    target = _validated_digital_bundle(tmp_path)
    manual_path = target / "manual.json"
    manual = json.loads(manual_path.read_text(encoding="utf-8"))
    manual["content"][0]["content"][0]["caption_generated"] = VALID_CAPTION.replace(
        "Tipo tecnico: componente/assemblaggio",
        "Tipo tecnico: marketing",
    )
    manual_path.write_text(json.dumps(manual), encoding="utf-8")

    for report in (build_validation_report(target), validate_run_bundle(target)):
        assert report.status == "failed"
        assert not _report_check(report, "captions.provenance.consistent").passed


def test_validated_status_cannot_be_forged_from_no_provider_run(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "manual.pdf", pages=1)
    target = tmp_path / "run"
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)
    ingest_manual(
        source,
        target,
        run_id="run-1",
        provider=None,
        detector=lambda path: detected,
        profile_builder=_builder(FakeProfile(DocumentProfile.DIGITAL_OUTLINE), detected),
    )
    run_path = target / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["status"] = "validated"
    run_path.write_text(json.dumps(run), encoding="utf-8")

    canonical = build_validation_report(target)
    assert canonical.status == "failed"
    assert not _report_check(canonical, "status.promotion.coherent").passed
    (target / "validation.json").write_text(
        canonical.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )

    checked = validate_run_bundle(target)
    assert checked.status == "failed"
    assert not _report_check(checked, "validation.artifact").passed
    assert not _report_check(checked, "status.promotion.coherent").passed


def test_coordinated_enrichment_and_trace_tampering_is_rejected(tmp_path: Path) -> None:
    target = _validated_digital_bundle(tmp_path)
    manual_path = target / "manual.json"
    manual = json.loads(manual_path.read_text(encoding="utf-8"))
    trace = manual["content"][0]["content"][0]["trace"]
    trace["include_in_rag"] = False
    trace["exclusion_reason"] = "arbitrary coordinated exclusion"
    manual_path.write_text(json.dumps(manual), encoding="utf-8")
    enrichment_path = target / "diagnostics" / "enrichment.json"
    enrichment = json.loads(enrichment_path.read_text(encoding="utf-8"))
    enrichment["enriched"] = 0
    enrichment["skipped"] = 1
    enrichment["items"][0]["status"] = "skipped"
    enrichment["items"][0]["reason"] = "arbitrary coordinated exclusion"
    enrichment_path.write_text(json.dumps(enrichment), encoding="utf-8")

    report = build_validation_report(target)

    assert report.status == "failed"
    check = _report_check(report, "promotion.evidence.coherent")
    assert not check.passed
    assert "impossible crop-to-enrichment transition" in check.message


@pytest.mark.parametrize("diagnostic", ["detection.json", "runtime.json"])
def test_required_diagnostic_cannot_be_removed(
    tmp_path: Path,
    diagnostic: str,
) -> None:
    target = _validated_digital_bundle(tmp_path)
    (target / "diagnostics" / diagnostic).unlink()

    report = build_validation_report(target)

    assert report.status == "failed"
    assert not _report_check(report, "promotion.evidence.coherent").passed


def test_provider_config_cannot_disagree_with_selected_model(tmp_path: Path) -> None:
    target = _validated_digital_bundle(tmp_path)
    run_path = target / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["pipeline"]["config"]["enrichment"]["provider_config"] = {
        "model": "other-model"
    }
    run_path.write_text(json.dumps(run), encoding="utf-8")

    report = build_validation_report(target)

    assert report.status == "failed"
    assert "provider_config model differs" in _report_check(
        report,
        "promotion.evidence.coherent",
    ).message


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prompt_version", "technical-caption-v999"),
        ("runtime_version", "other-runtime"),
    ],
)
def test_caption_prompt_and_runtime_must_match_pipeline(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    target = _validated_digital_bundle(tmp_path)
    manual_path = target / "manual.json"
    manual = json.loads(manual_path.read_text(encoding="utf-8"))
    manual["content"][0]["content"][0]["caption_provenance"][field] = value
    manual_path.write_text(json.dumps(manual), encoding="utf-8")

    report = build_validation_report(target)

    assert report.status == "failed"
    assert not _report_check(report, "captions.provenance.identity").passed


def test_content_mutation_changes_fingerprint_and_stales_declared_report(
    tmp_path: Path,
) -> None:
    target = _validated_digital_bundle(tmp_path)
    declared = json.loads((target / "validation.json").read_text(encoding="utf-8"))
    old_fingerprint = declared["metrics"]["bundle_fingerprint_sha256"]
    manual_path = target / "manual.json"
    manual = json.loads(manual_path.read_text(encoding="utf-8"))
    manual["content"][0]["content"][0]["caption_generated"] = VALID_CAPTION.replace(
        "Oggetto: gruppo meccanico",
        "Oggetto: assieme meccanico",
    )
    manual_path.write_text(json.dumps(manual), encoding="utf-8")

    canonical = build_validation_report(target)
    checked = validate_run_bundle(target)

    assert canonical.status == "passed"
    assert canonical.metrics["bundle_fingerprint_sha256"] != old_fingerprint
    assert checked.status == "failed"
    assert not _report_check(checked, "validation.artifact").passed


def test_caption_input_digest_binds_prompt_and_asset_bytes(tmp_path: Path) -> None:
    target = _validated_digital_bundle(tmp_path)
    (target / "assets" / "images" / "figure.png").write_bytes(b"mutated-image")

    asset_mutation = build_validation_report(target)

    assert asset_mutation.status == "failed"
    assert not _report_check(asset_mutation, "captions.input.coherent").passed

    target = _validated_digital_bundle(tmp_path / "digest")
    manual_path = target / "manual.json"
    manual = json.loads(manual_path.read_text(encoding="utf-8"))
    manual["content"][0]["content"][0]["caption_provenance"][
        "input_sha256"
    ] = "a" * 64
    manual_path.write_text(json.dumps(manual), encoding="utf-8")

    digest_mutation = build_validation_report(target)

    assert digest_mutation.status == "failed"
    assert not _report_check(digest_mutation, "captions.input.coherent").passed


def test_validation_message_wording_is_not_a_durable_policy_fact(tmp_path: Path) -> None:
    target = _validated_digital_bundle(tmp_path)
    validation_path = target / "validation.json"
    declared = json.loads(validation_path.read_text(encoding="utf-8"))
    for check in declared["checks"]:
        check["message"] = f"Updated explanation for {check['id']}"
    validation_path.write_text(json.dumps(declared), encoding="utf-8")

    report = validate_run_bundle(target)

    assert report.status == "passed"


def test_fixed_profile_setup_rejects_coordinated_parser_substitution(
    tmp_path: Path,
) -> None:
    target = _validated_digital_bundle(tmp_path)
    run_path = target / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["pipeline"]["parser"] = "arbitrary_parser"
    run["pipeline"]["config"]["parser"]["name"] = "arbitrary_parser"
    run_path.write_text(json.dumps(run), encoding="utf-8")
    manual_path = target / "manual.json"
    manual = json.loads(manual_path.read_text(encoding="utf-8"))
    manual["metadata"]["parser"] = "arbitrary_parser"
    manual_path.write_text(json.dumps(manual), encoding="utf-8")

    report = build_validation_report(target)

    assert report.status == "failed"
    assert not _report_check(report, "promotion.evidence.coherent").passed


def test_toc_must_match_manual_chapter_preorder(tmp_path: Path) -> None:
    target = _validated_digital_bundle(tmp_path)
    toc_path = target / "toc.json"
    toc = json.loads(toc_path.read_text(encoding="utf-8"))
    toc[0]["title"] = "Completely unrelated"
    toc_path.write_text(json.dumps(toc), encoding="utf-8")

    report = build_validation_report(target)

    assert report.status == "failed"
    assert not _report_check(report, "toc.hierarchy.coherent").passed


def test_scanned_acceptance_registry_is_historical_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        orchestrator_module,
        "SCANNED_OCR_ACCEPTANCE_EVIDENCE_ID",
        None,
    )
    target = _experimental_scanned_bundle(tmp_path)
    monkeypatch.setattr(
        validation_module,
        "SCANNED_OCR_ACCEPTANCE_EVIDENCE_IDS",
        frozenset({"acceptance-A"}),
    )
    assert validate_run_bundle(target).status == "passed"

    promotion_path = target / "diagnostics" / "promotion.json"
    promotion = json.loads(promotion_path.read_text(encoding="utf-8"))
    promotion["acceptance_evidence_id"] = "acceptance-A"
    profile_gate = next(
        gate for gate in promotion["gates"] if gate["id"] == "profile.accepted"
    )
    profile_gate["passed"] = True
    profile_gate["message"] = "Accepted by historical evidence A."
    promotion["eligible_for_validated"] = all(
        gate["passed"] for gate in promotion["gates"]
    )
    promotion_path.write_text(json.dumps(promotion), encoding="utf-8")
    run_path = target / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["status"] = "validated"
    run["warnings"] = promotion["profile_warnings"]
    run_path.write_text(json.dumps(run), encoding="utf-8")
    monkeypatch.setattr(
        validation_module,
        "SCANNED_OCR_ACCEPTANCE_EVIDENCE_IDS",
        frozenset({"acceptance-A", "acceptance-B"}),
    )
    canonical = build_validation_report(target)
    assert canonical.status == "passed"
    (target / "validation.json").write_text(
        canonical.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    assert validate_run_bundle(target).status == "passed"

    promotion["acceptance_evidence_id"] = "unknown-evidence"
    promotion_path.write_text(json.dumps(promotion), encoding="utf-8")
    unknown = build_validation_report(target)
    assert unknown.status == "failed"
    assert "not present in the accepted registry" in _report_check(
        unknown,
        "promotion.evidence.coherent",
    ).message


def _builder(profile: FakeProfile, expected: DetectedCapabilities):
    def build(selection: DetectedCapabilities, *, paddle_runtime, progress=None):
        assert selection is expected
        if selection.profile is DocumentProfile.SCANNED_OCR:
            assert paddle_runtime is not None
        return profile

    return build


def _validated_digital_bundle(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = _pdf(tmp_path / "manual.pdf", pages=1)
    target = tmp_path / "run"
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)
    ingest_manual(
        source,
        target,
        run_id="run-1",
        provider=FakeProvider(),
        detector=lambda path: detected,
        profile_builder=_builder(FakeProfile(DocumentProfile.DIGITAL_OUTLINE), detected),
    )
    return target


def _experimental_scanned_bundle(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = _pdf(tmp_path / "scan.pdf", pages=2)
    target = tmp_path / "scan-run"
    detected = _detected(DocumentProfile.SCANNED_OCR)
    ingest_manual(
        source,
        target,
        run_id="scan-run",
        provider=FakeProvider(),
        detector=lambda path: detected,
        profile_builder=_builder(FakeProfile(DocumentProfile.SCANNED_OCR), detected),
    )
    return target


def _report_check(report: ValidationReport, check_id: str) -> ValidationCheck:
    return next(check for check in report.checks if check.id == check_id)


def _detected(profile: DocumentProfile) -> DetectedCapabilities:
    embedded = profile is DocumentProfile.DIGITAL_OUTLINE
    text_layer = profile is not DocumentProfile.SCANNED_OCR
    return DetectedCapabilities(
        profile=profile,
        embedded_outline=embedded,
        outline_entries=1 if embedded else 0,
        text_layer=text_layer,
        sampled_pages=[1],
        pages_with_text=1 if text_layer else 0,
        median_text_characters=100 if text_layer else 0,
        profile_confidence=0.99,
        reasons=["test fixture"],
    )


def _pdf(path: Path, *, pages: int) -> Path:
    document = pymupdf.open()
    for number in range(1, pages + 1):
        page = document.new_page(width=300, height=400)
        page.insert_text((30, 50), f"Page {number} with enough fixture text")
    document.save(path)
    document.close()
    return path


def test_long_caption_is_included_with_warning_and_bundle_stays_validated(tmp_path: Path):
    source = _pdf(tmp_path / "manual.pdf", pages=2)
    target = tmp_path / "run-long"
    detected = _detected(DocumentProfile.DIGITAL_OUTLINE)
    caption = VALID_CAPTION.replace("Oggetto:", "Oggetto: " + "pannello " * 160)
    outcome = ingest_manual(
        source, target, run_id="run-long",
        provider=FakeProvider(caption_text=caption),
        detector=lambda path: detected,
        profile_builder=_builder(FakeProfile(DocumentProfile.DIGITAL_OUTLINE), detected),
    )
    assert outcome.manifest.status.value == "validated"
    assert outcome.validation.status == "passed"
    assert validate_run_bundle(target).status == "passed"
    assert any("Caption length advisory" in warning for warning in outcome.manifest.warnings)
    assert caption in (target / "manual.json").read_text().replace("\\n", "\n")
