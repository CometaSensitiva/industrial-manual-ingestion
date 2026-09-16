"""One automatic, atomic orchestration path for V2 manual ingestion."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import pymupdf

from .adapters.paddle_adapter import PaddleRuntimeConfig
from .detection import detect_pdf_capabilities
from .diagnostics import (
    AUTOMATIC_ROUTING_STRATEGY,
    CropDecisionDiagnostic,
    CropFilterDiagnostic,
    DetectionDiagnostic,
    EnrichmentDiagnostic,
    EnrichmentItemDiagnostic,
    RuntimeDiagnostic,
    StructureDiagnostic,
)
from .enrichment import (
    CAPTION_ALREADY_PRESENT_REASON,
    TABLE_STRUCTURED_SERIALIZATION_REASON,
    CaptionProvider,
    EnrichmentConfig,
    EnrichmentItemResult,
    EnrichmentReport,
    enrich_manual,
)
from .models import (
    ArtifactPaths,
    Chapter,
    DetectedCapabilities,
    DocumentProfile,
    ElementType,
    ManualDocument,
    ManualElement,
    PipelineSelection,
    RunManifest,
    RunStatus,
    SourceDocument,
    StrictModel,
    TocEntry,
    ValidationReport,
)
from .postprocess import (
    VisualCropFilterReport,
    filter_small_visual_crops,
)
from .progress import (
    ProgressCallback,
    ProgressEvent,
    ProgressStage,
    ProgressState,
    emit_progress,
)
from .profiles import IngestionProfile, ProfileResult, build_profile
from .promotion import (
    DIAGNOSTIC_NO_PROVIDER_REASON,
    DIAGNOSTIC_VISUAL_EXCLUSION_REASON,
    SCANNED_OCR_ACCEPTANCE_EVIDENCE_ID,
    build_promotion_assessment,
    promotion_warnings,
    warning_requires_experimental,
)
from .run_bundle import RunBundleExistsError, RunBundleWorkspace
from .table_serialization import (
    TABLE_SERIALIZATION_STRATEGY,
    serialize_manual_tables,
)
from .validation import build_validation_report, validate_run_bundle


# Backward-compatible module attribute. The durable evidence ID in
# ``promotion.py`` is the actual shared policy input.
SCANNED_OCR_ACCEPTANCE_PASSED = SCANNED_OCR_ACCEPTANCE_EVIDENCE_ID is not None


class IngestionError(RuntimeError):
    """Raised when an ingestion run cannot be published safely."""


class ProfileBuilder(Protocol):
    def __call__(
        self,
        selection: DetectedCapabilities,
        *,
        paddle_runtime: PaddleRuntimeConfig | None,
        progress: ProgressCallback | None,
    ) -> IngestionProfile: ...


class BundleValidator(Protocol):
    def __call__(self, run_dir: str | Path) -> ValidationReport: ...


class IngestionOutcome(StrictModel):
    """Typed hand-off returned only after an atomic publication succeeds."""

    output_dir: Path
    manifest: RunManifest
    validation: ValidationReport


@dataclass(frozen=True)
class _SourceFingerprint:
    size_bytes: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int


@dataclass(slots=True)
class _ProgressReporter:
    """Own the stage lifecycle while adapters emit only truthful updates."""

    callback: ProgressCallback | None
    current_stage: ProgressStage | None = None

    def start(
        self,
        stage: ProgressStage,
        *,
        current: int | None = None,
        total: int | None = None,
        unit: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.current_stage = stage
        emit_progress(
            self.callback,
            ProgressEvent(stage, ProgressState.STARTED, current, total, unit, detail),
        )

    def complete(
        self,
        *,
        current: int | None = None,
        total: int | None = None,
        unit: str | None = None,
        detail: str | None = None,
    ) -> None:
        if self.current_stage is None:
            return
        emit_progress(
            self.callback,
            ProgressEvent(
                self.current_stage,
                ProgressState.COMPLETED,
                current,
                total,
                unit,
                detail,
            ),
        )

    def fail(self, error: BaseException) -> None:
        if self.current_stage is None:
            return
        detail = str(error).strip() or type(error).__name__
        emit_progress(
            self.callback,
            ProgressEvent(
                self.current_stage,
                ProgressState.FAILED,
                detail=detail,
            ),
        )


def ingest_manual(
    source_pdf: str | Path,
    target_dir: str | Path,
    *,
    run_id: str,
    provider: CaptionProvider | None,
    pages: list[int] | None = None,
    title: str | None = None,
    language: str = "en",
    paddle_runtime: PaddleRuntimeConfig | None = None,
    progress: ProgressCallback | None = None,
) -> IngestionOutcome:
    """Public fixed-routing entry point used by the CLI and integrations."""

    return _ingest_manual(
        source_pdf,
        target_dir,
        run_id=run_id,
        provider=provider,
        pages=pages,
        title=title,
        language=language,
        paddle_runtime=paddle_runtime,
        progress=progress,
        detector=detect_pdf_capabilities,
        profile_builder=build_profile,
        validator=validate_run_bundle,
    )


def _ingest_manual(
    source_pdf: str | Path,
    target_dir: str | Path,
    *,
    run_id: str,
    provider: CaptionProvider | None,
    pages: list[int] | None = None,
    title: str | None = None,
    language: str = "en",
    paddle_runtime: PaddleRuntimeConfig | None = None,
    progress: ProgressCallback | None = None,
    detector: Callable[[str | Path], DetectedCapabilities] = detect_pdf_capabilities,
    profile_builder: ProfileBuilder = build_profile,
    validator: BundleValidator = validate_run_bundle,
) -> IngestionOutcome:
    reporter = _ProgressReporter(progress)
    reporter.start(ProgressStage.PREPARATION)
    try:
        return _run_ingestion(
            source_pdf,
            target_dir,
            run_id=run_id,
            provider=provider,
            pages=pages,
            title=title,
            language=language,
            paddle_runtime=paddle_runtime,
            progress=progress,
            reporter=reporter,
            detector=detector,
            profile_builder=profile_builder,
            validator=validator,
        )
    except (Exception, KeyboardInterrupt) as error:
        reporter.fail(error)
        raise


def _run_ingestion(
    source_pdf: str | Path,
    target_dir: str | Path,
    *,
    run_id: str,
    provider: CaptionProvider | None,
    pages: list[int] | None,
    title: str | None,
    language: str,
    paddle_runtime: PaddleRuntimeConfig | None,
    progress: ProgressCallback | None,
    reporter: _ProgressReporter,
    detector: Callable[[str | Path], DetectedCapabilities],
    profile_builder: ProfileBuilder,
    validator: BundleValidator,
) -> IngestionOutcome:
    """Run the single V2 ingestion path and publish one coherent run bundle.

    The selected profile is always the result of capability detection.  There
    are no parser, structure, or technology overrides.  Supplying ``None`` as
    the caption provider is supported for diagnostics only and necessarily
    produces an experimental bundle.
    """

    started_at = _utc_now()
    source, target = _validate_paths_and_request(
        source_pdf=source_pdf,
        target_dir=target_dir,
        run_id=run_id,
        title=title,
        language=language,
    )
    initial_fingerprint = _fingerprint(source)
    source_sha256 = _hash_unchanged_source(source, initial_fingerprint)
    pages_total = _inspect_pdf_page_count(source)
    selected_pages = _validate_pages(pages, pages_total)
    reporter.complete()

    reporter.start(ProgressStage.DETECTION)
    detected = detector(source)
    if not isinstance(detected, DetectedCapabilities):
        raise TypeError("detector must return DetectedCapabilities")
    if detected.sampled_pages[-1] > pages_total:
        raise IngestionError(
            "detector returned sampled pages outside the inspected PDF page count"
        )

    provider_preflight = _preflight_provider(provider)
    effective_paddle_runtime = paddle_runtime
    if detected.profile is DocumentProfile.SCANNED_OCR:
        effective_paddle_runtime = paddle_runtime or PaddleRuntimeConfig()
    profile = profile_builder(
        detected,
        paddle_runtime=effective_paddle_runtime,
        progress=progress,
    )
    if not isinstance(getattr(profile, "profile", None), DocumentProfile):
        raise TypeError("profile builder returned an invalid ingestion profile")
    if profile.profile is not detected.profile:
        raise IngestionError(
            "profile builder violated automatic routing: "
            f"detected={detected.profile.value}, built={profile.profile.value}"
        )
    reporter.complete(detail=detected.profile.value)

    reporter.start(ProgressStage.EXTRACTION)
    with RunBundleWorkspace(target) as workspace:
        _write_diagnostic(
            workspace.diagnostics_dir / "detection.json",
            DetectionDiagnostic(
                strategy=AUTOMATIC_ROUTING_STRATEGY,
                capabilities=detected,
            ),
        )

        result = profile.run(
            source,
            run_id=run_id,
            assets_dir=workspace.assets_dir,
            pages=selected_pages if pages is not None else None,
            title=title,
            language=language,
        )
        _validate_profile_result(
            result,
            run_id=run_id,
            source=source,
            detected=detected,
            pages_total=pages_total,
            expected_pages=selected_pages,
            language=language,
            requested_title=title,
        )
        _ensure_source_unchanged(source, initial_fingerprint)
        reporter.complete()

        reporter.start(ProgressStage.CROP_FILTER)
        filtered_manual, crop_report = filter_small_visual_crops(result.manual)
        reporter.complete()

        reporter.start(ProgressStage.ENRICHMENT)
        if provider is None:
            final_manual, enrichment_report = _prepare_diagnostic_manual(
                filtered_manual
            )
            enrichment_progress = {
                "current": 0,
                "total": 0,
                "unit": "elements",
                "detail": "skipped: no caption provider configured",
            }
        else:
            final_manual, enrichment_report = enrich_manual(
                filtered_manual,
                asset_root=workspace.root,
                provider=provider,
                progress=progress,
            )
            provider_calls = (
                enrichment_report.enriched
                + enrichment_report.review_required
                + enrichment_report.failed
            )
            enrichment_progress = {
                "current": provider_calls,
                "total": provider_calls,
                "unit": "elements",
                "detail": None,
            }

        _sanitize_staging_paths_in_paddle_json(
            workspace.diagnostics_dir / "paddle",
            staging_root=workspace.root,
        )
        structure_details = _structure_details(result)
        profile_runtime = _profile_runtime(result, parser=final_manual.metadata.parser)
        enrichment_diagnostic, structure_diagnostic = _write_orchestration_diagnostics(
            workspace=workspace,
            crop_report=crop_report,
            enrichment_report=enrichment_report,
            provider=provider,
            provider_preflight=provider_preflight,
            structure_strategy=final_manual.metadata.structure_strategy,
            toc_available=final_manual.metadata.toc_available,
            structure_details=structure_details,
            profile_runtime=profile_runtime,
            paddle_runtime=effective_paddle_runtime,
            profile=detected.profile,
        )
        reporter.complete(**enrichment_progress)

        reporter.start(ProgressStage.TABLE_SERIALIZATION)
        final_manual = serialize_manual_tables(final_manual)
        table_warnings = _table_serialization_warnings(final_manual)
        reporter.complete()

        reporter.start(ProgressStage.VALIDATION)
        completed_at = _utc_now()
        source_document = SourceDocument(
            file=source.name,
            sha256=source_sha256,
            size_bytes=initial_fingerprint.size_bytes,
            pages_total=pages_total,
            detected=detected,
        )
        pipeline = _pipeline_selection(
            manual=final_manual,
            detected=detected,
            provider=provider,
            provider_preflight=provider_preflight,
            crop_report=crop_report,
            pages_total=pages_total,
            paddle_runtime=effective_paddle_runtime,
            profile_instance=profile,
            profile_runtime=profile_runtime,
            structure_details=structure_details,
        )

        caption_warnings = [
            f"Caption length advisory for {element.id!r}: "
            f"{len(element.caption_generated)} characters "
            f"(recommended maximum {EnrichmentConfig().max_characters}); retained in bundle."
            for element in _walk_elements(final_manual.content)
            if element.caption_generated
            and len(element.caption_generated) > EnrichmentConfig().max_characters
        ]
        pipeline_warnings = _deduplicate([
            *result.warnings, *table_warnings, *caption_warnings,
        ])

        draft_manifest = RunManifest(
            run_id=run_id,
            status=RunStatus.COMPLETED,
            source=source_document,
            pipeline=pipeline,
            artifacts=ArtifactPaths(),
            started_at=started_at,
            completed_at=completed_at,
            warnings=pipeline_warnings,
        )
        promotion = build_promotion_assessment(
            manifest=draft_manifest,
            manual=final_manual,
            enrichment=enrichment_diagnostic,
            structure=structure_diagnostic,
            profile_warnings=pipeline_warnings,
            scanned_acceptance_evidence_id=SCANNED_OCR_ACCEPTANCE_EVIDENCE_ID,
        )
        _write_diagnostic(
            workspace.diagnostics_dir / "promotion.json",
            promotion,
        )
        experimental_reasons = promotion_warnings(promotion)
        warnings = _deduplicate([*pipeline_warnings, *experimental_reasons])

        workspace.stage_bundle(
            manifest=draft_manifest,
            manual=final_manual,
            toc=list(result.toc),
        )
        draft_report = _validated_report(
            validator(workspace.root),
            run_id=run_id,
            phase="completed draft",
        )

        final_status = (
            RunStatus.EXPERIMENTAL if experimental_reasons else RunStatus.VALIDATED
        )
        final_manifest_raw = draft_manifest.model_dump(mode="json")
        final_manifest_raw.update(
            {
                "status": final_status.value,
                "artifacts": ArtifactPaths(validation="validation.json").model_dump(
                    mode="json"
                ),
                "warnings": warnings,
            }
        )
        final_manifest = RunManifest.model_validate(final_manifest_raw)
        final_report = _stage_canonical_validation(
            workspace=workspace,
            manifest=final_manifest,
            manual=final_manual,
            toc=list(result.toc),
            initial_report=draft_report,
            validator=validator,
        )
        reporter.complete()

        reporter.start(ProgressStage.PUBLICATION)
        _ensure_source_unchanged(
            source,
            initial_fingerprint,
            expected_sha256=source_sha256,
        )
        try:
            output_dir = workspace.publish()
            reporter.complete()
        except KeyboardInterrupt:
            # Once the atomic rename has succeeded there is no partial run to
            # discard.  Treat an interrupt in the final notification window as
            # success instead of reporting a false unpublished failure.
            if not target.exists():
                raise
            output_dir = target

    return IngestionOutcome(
        output_dir=output_dir,
        manifest=final_manifest,
        validation=final_report,
    )


def _validate_paths_and_request(
    *,
    source_pdf: str | Path,
    target_dir: str | Path,
    run_id: str,
    title: str | None,
    language: str,
) -> tuple[Path, Path]:
    _validate_run_id(run_id)
    if title is not None and not title.strip():
        raise ValueError("title cannot be empty when supplied")
    if not language.strip():
        raise ValueError("language cannot be empty")

    source = Path(source_pdf).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"PDF not found: {source}")
    if not source.is_file():
        raise ValueError(f"PDF source is not a file: {source}")

    target = Path(target_dir).expanduser().resolve()
    if target.exists():
        raise RunBundleExistsError(f"run bundle already exists: {target}")
    if target == source:
        raise ValueError("target directory cannot be the source PDF")
    existing_parent = _nearest_existing_parent(target.parent)
    if not existing_parent.is_dir():
        raise ValueError(f"target parent is not a directory: {existing_parent}")
    if not os.access(existing_parent, os.W_OK | os.X_OK):
        raise PermissionError(f"target parent is not writable: {existing_parent}")
    return source, target


def _validate_run_id(value: str) -> None:
    if not value or value != value.strip():
        raise ValueError("run_id must be non-empty without surrounding whitespace")
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("run_id must be a safe single path component")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("run_id cannot contain control characters")


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _fingerprint(source: Path) -> _SourceFingerprint:
    stat = source.stat()
    return _SourceFingerprint(
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        ctime_ns=stat.st_ctime_ns,
        device=stat.st_dev,
        inode=stat.st_ino,
    )


def _hash_unchanged_source(
    source: Path,
    fingerprint: _SourceFingerprint,
) -> str:
    digest = _source_sha256(source)
    _ensure_source_unchanged(source, fingerprint)
    return digest


def _ensure_source_unchanged(
    source: Path,
    expected: _SourceFingerprint,
    *,
    expected_sha256: str | None = None,
) -> None:
    try:
        current = _fingerprint(source)
    except OSError as exc:
        raise IngestionError(f"source PDF became unavailable during ingestion: {exc}") from exc
    if current != expected:
        raise IngestionError(
            "source PDF identity or filesystem metadata changed during ingestion; "
            "the run was discarded"
        )
    if expected_sha256 is not None and _source_sha256(source) != expected_sha256:
        raise IngestionError(
            "source PDF content hash changed during ingestion; the run was discarded"
        )


def _source_sha256(source: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inspect_pdf_page_count(source: Path) -> int:
    try:
        with pymupdf.open(source) as document:
            if not document.is_pdf:
                raise ValueError("source is not a PDF document")
            if document.needs_pass:
                raise ValueError("source PDF is password-protected")
            if document.page_count < 1:
                raise ValueError("source PDF contains no pages")
            return document.page_count
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"cannot inspect source PDF: {exc}") from exc


def _validate_pages(pages: list[int] | None, pages_total: int) -> list[int]:
    if pages is None:
        return list(range(1, pages_total + 1))
    if not pages:
        raise ValueError("pages cannot be empty")
    if any(isinstance(page, bool) or not isinstance(page, int) for page in pages):
        raise TypeError("pages must contain one-based integer page numbers")
    if len(pages) != len(set(pages)):
        raise ValueError("pages cannot contain duplicates")
    selected = sorted(pages)
    if selected[0] < 1 or selected[-1] > pages_total:
        raise ValueError(f"pages must be between 1 and {pages_total}")
    return selected


def _preflight_provider(provider: CaptionProvider | None) -> dict[str, Any]:
    if provider is None:
        return {}
    preflight = getattr(provider, "preflight", None)
    if preflight is None:
        return {}
    if not callable(preflight):
        raise TypeError("provider preflight attribute must be callable")
    try:
        result = preflight()
    except Exception as exc:
        raise IngestionError(f"caption provider preflight failed: {exc}") from exc
    if result is None:
        return {}
    if not isinstance(result, Mapping) or not all(
        isinstance(key, str) for key in result
    ):
        raise TypeError("provider preflight must return a string-keyed mapping")
    value = dict(result)
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise TypeError("provider preflight metadata must be JSON-serializable") from exc
    return value


def _validate_profile_result(
    result: ProfileResult,
    *,
    run_id: str,
    source: Path,
    detected: DetectedCapabilities,
    pages_total: int,
    expected_pages: list[int],
    language: str,
    requested_title: str | None,
) -> None:
    if not isinstance(result, ProfileResult):
        raise TypeError("profile must return ProfileResult")
    manual = result.manual
    if manual.id != run_id:
        raise IngestionError("profile result manual id differs from run_id")
    if manual.source_file != source.name:
        raise IngestionError("profile result source_file differs from the source PDF name")
    if manual.language != language:
        raise IngestionError("profile result language differs from the requested language")
    if requested_title is not None and manual.title != requested_title.strip():
        raise IngestionError("profile result title differs from the requested title")
    if manual.metadata.pages_total != pages_total:
        raise IngestionError("profile page count differs from the inspected PDF")
    if manual.metadata.pages_processed != expected_pages:
        raise IngestionError(
            "profile did not process the exact requested page selection: "
            f"expected={expected_pages}, actual={manual.metadata.pages_processed}"
        )
    if manual.metadata.profile is not detected.profile:
        raise IngestionError("profile result metadata differs from automatic detection")
    if manual.metadata.toc_available != bool(result.toc):
        raise IngestionError("profile TOC availability metadata is incoherent")
    if not manual.metadata.parser.strip() or not manual.metadata.structure_strategy.strip():
        raise IngestionError("profile parser and structure strategy must be explicit")
    for entry in result.toc:
        if not isinstance(entry, TocEntry):
            raise TypeError("profile TOC must contain TocEntry values")
    if not all(isinstance(warning, str) and warning.strip() for warning in result.warnings):
        raise TypeError("profile warnings must be non-empty strings")


def _prepare_diagnostic_manual(
    manual: ManualDocument,
) -> tuple[ManualDocument, EnrichmentReport]:
    diagnostic = manual.model_copy(deep=True)
    items: list[EnrichmentItemResult] = []
    for element in _walk_elements(diagnostic.content):
        if element.type not in {ElementType.IMAGE, ElementType.TABLE}:
            continue
        if not element.trace.include_in_rag:
            items.append(
                EnrichmentItemResult(
                    element.id,
                    "skipped",
                    element.trace.exclusion_reason or "excluded from retrieval",
                )
            )
            continue
        if element.type is ElementType.TABLE:
            items.append(
                EnrichmentItemResult(
                    element.id,
                    "skipped",
                    TABLE_STRUCTURED_SERIALIZATION_REASON,
                )
            )
            continue
        if element.caption_generated:
            items.append(
                EnrichmentItemResult(
                    element.id,
                    "skipped",
                    CAPTION_ALREADY_PRESENT_REASON,
                )
            )
            continue
        element.trace.include_in_rag = False
        element.trace.exclusion_reason = DIAGNOSTIC_VISUAL_EXCLUSION_REASON
        items.append(
            EnrichmentItemResult(
                element.id,
                "skipped",
                DIAGNOSTIC_VISUAL_EXCLUSION_REASON,
            )
        )
    return diagnostic, EnrichmentReport(tuple(items))


def _walk_elements(items: list[Chapter | ManualElement]) -> list[ManualElement]:
    elements: list[ManualElement] = []
    for item in items:
        if isinstance(item, Chapter):
            elements.extend(_walk_elements(item.content))
        else:
            elements.append(item)
    return elements


def _table_serialization_warnings(manual: ManualDocument) -> list[str]:
    """Promote only non-structured tables that remain eligible for retrieval."""

    fallback: list[str] = []
    unavailable: list[str] = []
    for element in _walk_elements(manual.content):
        if element.type is not ElementType.TABLE or not element.trace.include_in_rag:
            continue
        serialization = element.table_serialization
        if serialization is None:
            unavailable.append(element.id)
        elif serialization.status.value == "fallback":
            fallback.append(element.id)
        elif serialization.status.value == "unavailable":
            unavailable.append(element.id)

    warnings: list[str] = []
    if fallback:
        warnings.append(
            "Table serialization fallback for retrievable table(s): "
            + ", ".join(fallback)
        )
    if unavailable:
        warnings.append(
            "Table serialization missing or unavailable for retrievable table(s): "
            + ", ".join(unavailable)
        )
    return warnings


def _write_orchestration_diagnostics(
    *,
    workspace: RunBundleWorkspace,
    crop_report: VisualCropFilterReport,
    enrichment_report: EnrichmentReport,
    provider: CaptionProvider | None,
    provider_preflight: dict[str, Any],
    structure_strategy: str,
    toc_available: bool,
    structure_details: dict[str, Any] | None,
    profile_runtime: dict[str, Any] | None,
    paddle_runtime: PaddleRuntimeConfig | None,
    profile: DocumentProfile,
) -> tuple[EnrichmentDiagnostic, StructureDiagnostic]:
    crop = CropFilterDiagnostic(
        strategy=crop_report.strategy,
        threshold=crop_report.threshold,
        excluded=crop_report.excluded,
        kept=crop_report.kept,
        preserved=crop_report.preserved,
        unassessed=crop_report.unassessed,
        decisions=[
            CropDecisionDiagnostic(
                element_id=decision.element_id,
                element_type=decision.element_type,
                status=decision.status.value,
                area_fraction=decision.area_fraction,
                reason=decision.reason,
            )
            for decision in crop_report.decisions
        ],
    )
    enrichment = EnrichmentDiagnostic(
        provider_configured=provider is not None,
        provider_preflight=provider_preflight,
        enriched=enrichment_report.enriched,
        skipped=enrichment_report.skipped,
        failed=enrichment_report.failed,
        review_required=enrichment_report.review_required,
        items=[
            EnrichmentItemDiagnostic(
                element_id=item.element_id,
                status=item.status,
                reason=item.reason,
            )
            for item in enrichment_report.items
        ],
    )
    structure = StructureDiagnostic(
        strategy=structure_strategy,
        toc_available=toc_available,
        details_available=structure_details is not None,
        details=structure_details,
    )
    runtime = RuntimeDiagnostic(
        profile_runtime=profile_runtime,
        provider_runtime=provider_preflight or None,
        paddle_runtime_config=(
            _paddle_runtime_dict(paddle_runtime)
            if profile is DocumentProfile.SCANNED_OCR and paddle_runtime is not None
            else None
        ),
    )
    _write_diagnostic(workspace.diagnostics_dir / "crop_filter.json", crop)
    _write_diagnostic(workspace.diagnostics_dir / "enrichment.json", enrichment)
    _write_diagnostic(workspace.diagnostics_dir / "structure.json", structure)
    _write_diagnostic(workspace.diagnostics_dir / "runtime.json", runtime)
    return enrichment, structure


def _write_diagnostic(path: Path, value: StrictModel) -> None:
    serialized = json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(serialized)


def _structure_details(result: ProfileResult) -> dict[str, Any] | None:
    diagnostics = getattr(result, "diagnostics", None)
    if diagnostics is None:
        return None
    to_dict = getattr(diagnostics, "to_dict", None)
    if callable(to_dict):
        raw = to_dict()
    elif is_dataclass(diagnostics):
        raw = asdict(diagnostics)
    else:
        raise TypeError("profile structure diagnostics must support to_dict or be a dataclass")
    if not isinstance(raw, dict):
        raise TypeError("profile structure diagnostics must serialize to an object")
    # The current OCR diagnostic serializer predates this field even though it
    # is part of the typed dataclass. Preserve it without editing that module.
    if hasattr(diagnostics, "resolved_ratio"):
        raw.setdefault("resolved_ratio", getattr(diagnostics, "resolved_ratio"))
    json.dumps(raw)
    return raw


def _profile_runtime(
    result: ProfileResult,
    *,
    parser: str,
) -> dict[str, Any]:
    runtime = getattr(result, "runtime", None)
    if runtime is None:
        value: dict[str, Any] = {}
    else:
        if not isinstance(runtime, Mapping) or not all(
            isinstance(key, str) for key in runtime
        ):
            raise TypeError("profile runtime diagnostics must be a string-keyed mapping")
        value = dict(runtime)
    value.setdefault("manual_ingestion", _package_version("industrial-manual-ingestion"))
    value.setdefault("python", platform.python_version())
    value.setdefault("pydantic", _package_version("pydantic"))
    value.setdefault("pymupdf", _package_version("PyMuPDF"))
    value.setdefault("pillow", _package_version("Pillow"))
    if parser == "docling":
        value.setdefault("docling", _package_version("docling"))
    json.dumps(value)
    return value


def _package_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"


def _sanitize_staging_paths_in_paddle_json(
    paddle_dir: Path,
    *,
    staging_root: Path,
) -> None:
    if not paddle_dir.exists():
        return
    root = staging_root.resolve()
    for path in sorted(paddle_dir.rglob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise IngestionError(f"cannot sanitize Paddle diagnostic {path.name}: {exc}") from exc
        sanitized = _sanitize_json_value(raw, root)
        if sanitized == raw:
            continue
        temporary = path.with_name(f".{path.name}.sanitizing")
        temporary.write_text(
            json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)


def _sanitize_json_value(value: Any, staging_root: Path) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_json_value(item, staging_root)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_json_value(item, staging_root) for item in value]
    if isinstance(value, str):
        candidate = Path(value)
        if candidate.is_absolute():
            try:
                return candidate.resolve(strict=False).relative_to(staging_root).as_posix()
            except (OSError, ValueError):
                return value
    return value


def _pipeline_selection(
    *,
    manual: ManualDocument,
    detected: DetectedCapabilities,
    provider: CaptionProvider | None,
    provider_preflight: dict[str, Any],
    crop_report: VisualCropFilterReport,
    pages_total: int,
    paddle_runtime: PaddleRuntimeConfig | None,
    profile_instance: IngestionProfile,
    profile_runtime: dict[str, Any],
    structure_details: dict[str, Any] | None,
) -> PipelineSelection:
    provider_name, provider_model = _enrichment_identity(
        manual,
        provider=provider,
        provider_preflight=provider_preflight,
    )
    config: dict[str, Any] = {
        "routing": {
            "mode": "automatic",
            "strategy": AUTOMATIC_ROUTING_STRATEGY,
            "detected_profile": detected.profile.value,
            "profile_confidence": detected.profile_confidence,
        },
        "pages": {
            "selection": (
                "full_document"
                if manual.metadata.pages_processed == list(range(1, pages_total + 1))
                else "explicit_partial"
            ),
            "processed": manual.metadata.pages_processed,
        },
        "parser": {
            "name": manual.metadata.parser,
            "runtime": profile_runtime,
            "config": _profile_config_dict(profile_instance),
        },
        "structure": {
            "strategy": manual.metadata.structure_strategy,
            "config": (
                structure_details.get("config")
                if isinstance(structure_details, dict)
                and isinstance(structure_details.get("config"), dict)
                else None
            ),
        },
        "postprocess": {
            "visual_crop_filter": {
                "strategy": crop_report.strategy,
                "threshold": crop_report.threshold,
            }
        },
        "enrichment": {
            "enabled": provider is not None,
            "prompt_version": EnrichmentConfig().prompt_version,
            "max_caption_characters": EnrichmentConfig().max_characters,
            "provider_config": _provider_config_dict(provider),
            "preflight": provider_preflight,
        },
    }
    if detected.profile is DocumentProfile.SCANNED_OCR and paddle_runtime is not None:
        config["paddle_runtime"] = _paddle_runtime_dict(paddle_runtime)
    config["table_serialization"] = {
        "strategy": TABLE_SERIALIZATION_STRATEGY,
    }
    return PipelineSelection(
        profile=detected.profile,
        parser=manual.metadata.parser,
        structure_strategy=manual.metadata.structure_strategy,
        enrichment_provider=provider_name,
        enrichment_model=provider_model,
        config=config,
    )


def _profile_config_dict(profile: IngestionProfile) -> dict[str, Any] | None:
    adapter = getattr(profile, "content_adapter", None)
    public_config = getattr(adapter, "public_config", None)
    if public_config is None:
        return None
    if not callable(public_config):
        raise TypeError("content adapter public_config attribute must be callable")
    raw = public_config()
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise TypeError("content adapter public_config must return a string-keyed mapping")
    value = dict(raw)
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise TypeError("content adapter public_config must be JSON-serializable") from exc
    return value


def _enrichment_identity(
    manual: ManualDocument,
    *,
    provider: CaptionProvider | None,
    provider_preflight: dict[str, Any],
) -> tuple[str | None, str | None]:
    identities = {
        (element.caption_provenance.provider, element.caption_provenance.model)
        for element in _walk_elements(manual.content)
        if element.caption_provenance is not None
    }
    if len(identities) > 1:
        raise IngestionError("one run cannot contain captions from multiple providers or models")
    if identities:
        return next(iter(identities))
    if provider is None:
        return None, None

    provider_name = _provider_name(provider)
    model = provider_preflight.get("model")
    if not isinstance(model, str) or not model.strip():
        config = getattr(provider, "config", None)
        configured_model = getattr(config, "model", None)
        model = configured_model if isinstance(configured_model, str) else None
    return provider_name, model.strip() if isinstance(model, str) and model.strip() else None


def _provider_name(provider: CaptionProvider) -> str:
    for attribute in ("provider_name", "name"):
        value = getattr(provider, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    class_name = type(provider).__name__
    class_name = re.sub(r"(?:Caption)?Provider$", "", class_name) or class_name
    return re.sub(r"(?<!^)(?=[A-Z])", "_", class_name).casefold()


def _paddle_runtime_dict(config: PaddleRuntimeConfig) -> dict[str, Any]:
    executable = Path(config.python_executable).expanduser()
    return {
        "python_executable_name": executable.name or config.python_executable,
        "cache_configured": config.cache_dir is not None,
        "render_dpi": config.render_dpi,
        "max_render_edge": config.max_render_edge,
        "timeout_seconds_per_page": config.timeout_seconds_per_page,
        "max_total_timeout_seconds": config.max_total_timeout_seconds,
        "pages_per_worker": config.pages_per_worker,
    }


def _provider_config_dict(provider: CaptionProvider | None) -> dict[str, Any] | None:
    if provider is None:
        return None
    public_config = getattr(provider, "public_config", None)
    if public_config is None:
        return None
    if not callable(public_config):
        raise TypeError("provider public_config attribute must be callable")
    raw = public_config()
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise TypeError("provider public_config must return a string-keyed mapping")
    value = dict(raw)
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise TypeError("provider public_config must be JSON-serializable") from exc
    return value


def _warning_requires_experimental(warning: str) -> bool:
    """Backward-compatible wrapper around the shared promotion policy."""

    return warning_requires_experimental(warning)


def _validated_report(
    report: ValidationReport,
    *,
    run_id: str,
    phase: str,
) -> ValidationReport:
    if not isinstance(report, ValidationReport):
        raise TypeError("bundle validator must return ValidationReport")
    if report.run_id != run_id:
        raise IngestionError(
            f"{phase} validation returned an incoherent run_id: {report.run_id!r}"
        )
    if report.status != "passed":
        failed = ", ".join(check.id for check in report.checks if not check.passed)
        raise IngestionError(f"{phase} validation failed: {failed}")
    return report


def _stage_canonical_validation(
    *,
    workspace: RunBundleWorkspace,
    manifest: RunManifest,
    manual: ManualDocument,
    toc: list[TocEntry],
    initial_report: ValidationReport,
    validator: BundleValidator,
) -> ValidationReport:
    # The workspace requires a declared validation payload with the final
    # manifest. It is a temporary placeholder only: the canonical report below
    # is built without reading validation.json, then written exactly once.
    workspace.stage_bundle(
        manifest=manifest,
        manual=manual,
        toc=toc,
        validation=initial_report,
        replace=True,
    )
    canonical = _validated_report(
        build_validation_report(workspace.root),
        run_id=manifest.run_id,
        phase="canonical final",
    )
    workspace.stage_bundle(
        manifest=manifest,
        manual=manual,
        toc=toc,
        validation=canonical,
        replace=True,
    )
    checked = _validated_report(
        validator(workspace.root),
        run_id=manifest.run_id,
        phase="final pass 1",
    )
    if checked != canonical:
        raise IngestionError(
            "final validation returned a report different from the canonical report"
        )
    return checked


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
