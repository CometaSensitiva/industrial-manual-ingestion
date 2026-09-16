"""Read-only validation for canonical V2 run bundles."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable

from pydantic import ValidationError

from .diagnostics import (
    AUTOMATIC_ROUTING_STRATEGY,
    CropFilterDiagnostic,
    DetectionDiagnostic,
    EnrichmentDiagnostic,
    PromotionDiagnostic,
    RuntimeDiagnostic,
    StructureDiagnostic,
)
from .adapters.paddle_adapter import ACCEPTED_PADDLE_PACKAGE_VERSIONS
from .enrichment import (
    CAPTION_UNAVAILABLE_REASON,
    CURRENT_PROMPT_VERSION,
    EnrichmentConfig,
    CAPTION_ALREADY_PRESENT_REASON,
    CAPTION_VALIDATED_REASON,
    MAX_CAPTION_CHARACTERS,
    TABLE_STRUCTURED_SERIALIZATION_REASON,
    caption_input_sha256,
    caption_review_reason,
    validate_caption_text,
)
from .models import (
    Chapter,
    DocumentProfile,
    ElementType,
    ManualDocument,
    ManualElement,
    RunManifest,
    RunStatus,
    TableSerializationStatus,
    TocEntry,
    ValidationCheck,
    ValidationReport,
)
from .postprocess import (
    SMALL_VISUAL_EXCLUSION_REASON,
    VISUAL_CROP_FILTER_STRATEGY,
    VISUAL_CROP_KEPT_REASON,
    VISUAL_CROP_UNASSESSED_REASON,
)
from .profiles.scanned_ocr import PRINTED_TOC_EXCLUSION_REASON
from .promotion import (
    DIAGNOSTIC_VISUAL_EXCLUSION_REASON,
    SCANNED_OCR_ACCEPTANCE_EVIDENCE_IDS,
    build_promotion_assessment,
    caption_identity_errors,
    failed_promotion_gate_ids,
)
from .structure.ocr_reconstruct import (
    OCR_FALLBACK_STRATEGY,
    OCR_STRUCTURE_STRATEGY,
    OcrReconstructionConfig,
    OcrTocReconstructionError,
    reconstruct_toc_from_ocr_elements,
)
from .structure.models import STRUCTURE_STRATEGY, ReconstructionConfig
from .structure.reconstruct import normalize_title
from .table_serialization import (
    TABLE_SERIALIZATION_STRATEGY,
    serialize_manual_tables,
)

FINISHED_STATUSES = {RunStatus.COMPLETED, RunStatus.VALIDATED, RunStatus.EXPERIMENTAL}
MIN_CONTENT_PAGE_COVERAGE = 0.75
VALIDATION_POLICY_VERSION = "1.1"
ACCEPTED_CORE_RUNTIME_VERSIONS = {
    "manual_ingestion": "1.0.0",
    "pydantic": "2.13.4",
    "pymupdf": "1.28.0",
    "pillow": "11.3.0",
}
ACCEPTED_DOCLING_VERSION = "2.112.0"


def validate_run_bundle(run_dir: str | Path) -> ValidationReport:
    """Validate a bundle and verify its declared report is current.

    The canonical expected report is constructed without reading
    ``validation.json``. This wrapper then compares the declared artifact with
    that report exactly, avoiding a self-referential fixed-point calculation.
    """

    root = Path(run_dir).expanduser().resolve()
    expected = build_validation_report(root)
    error = _declared_validation_error(root, expected)
    if error is None:
        return expected
    return _replace_validation_artifact_check(expected, error)


def build_validation_report(run_dir: str | Path) -> ValidationReport:
    """Build the one canonical validation report without trusting its copy.

    The report always uses the same ordered check identifiers. Malformed JSON,
    invalid contracts, unsafe paths, and missing files become failed checks
    instead of escaping as validation exceptions.
    """

    root = Path(run_dir).expanduser().resolve()
    checks: dict[str, ValidationCheck] = {}
    metrics: dict[str, float | int | str | None] = {
        "source_pages_total": None,
        "processed_page_count": 0,
        "chapter_count": 0,
        "element_count": 0,
        "element_page_count": 0,
        "toc_entry_count": 0,
        "asset_reference_count": 0,
        "caption_count": 0,
        "table_count": 0,
        "table_serialization_structured_count": 0,
        "table_serialization_fallback_count": 0,
        "table_serialization_unavailable_count": 0,
        "table_serialized_row_count": 0,
        "duplicate_id_count": 0,
        "validation_policy_version": VALIDATION_POLICY_VERSION,
    }

    is_directory = root.is_dir()
    _record(
        checks,
        "run.directory",
        is_directory,
        "Run directory exists" if is_directory else "Run directory not found",
    )

    manifest_path = root / "run.json"
    manifest_exists = is_directory and manifest_path.is_file()
    _record(
        checks,
        "manifest.exists",
        manifest_exists,
        "run.json exists" if manifest_exists else "run.json is missing",
    )
    manifest_raw, manifest_json_error = _read_json(manifest_path) if manifest_exists else (None, "run.json is missing")
    manifest_json_valid = isinstance(manifest_raw, dict)
    manifest_json_message = "run.json contains a JSON object"
    if not manifest_json_valid:
        manifest_json_message = manifest_json_error or "run.json must contain a JSON object"
    _record(checks, "manifest.json", manifest_json_valid, manifest_json_message)

    manifest: RunManifest | None = None
    manifest_contract_error: str | None = None
    if manifest_json_valid:
        try:
            manifest = RunManifest.model_validate(manifest_raw)
        except (ValidationError, TypeError, ValueError) as error:
            manifest_contract_error = _validation_message(error)
    _record(
        checks,
        "manifest.contract",
        manifest is not None,
        "run.json matches RunManifest" if manifest else manifest_contract_error or "Manifest contract was not evaluated",
    )

    fallback_run_id = _safe_fallback_run_id(
        _raw_string(manifest_raw, "run_id"),
        root.name,
    )
    run_id = manifest.run_id if manifest else fallback_run_id
    status_finished = manifest is not None and manifest.status in FINISHED_STATUSES
    _record(
        checks,
        "manifest.status.finished",
        status_finished,
        f"Finished status: {manifest.status.value}" if status_finished and manifest else "Run status must be completed, validated, or experimental",
    )

    artifact_values = _raw_artifact_paths(manifest_raw)
    resolved_artifacts: dict[str, Path] = {}
    artifact_errors: list[str] = []
    for name, relative in artifact_values.items():
        if relative is None:
            continue
        if not isinstance(relative, str):
            artifact_errors.append(f"{name}: artifact path must be a string or null")
            continue
        candidate, error = _resolve_inside(root, relative)
        if error:
            artifact_errors.append(f"{name}: {error}")
        elif candidate is not None:
            resolved_artifacts[name] = candidate
    artifact_errors.extend(_artifact_collision_errors(artifact_values))
    _record(
        checks,
        "artifacts.paths.safe",
        not artifact_errors,
        "All manifest artifact paths remain inside the run" if not artifact_errors else "; ".join(artifact_errors),
    )
    diagnostics_dir = resolved_artifacts.get("diagnostics")
    diagnostics_exists = diagnostics_dir is not None and diagnostics_dir.is_dir()
    _record(
        checks,
        "diagnostics.directory",
        diagnostics_exists,
        "Diagnostics directory exists"
        if diagnostics_exists
        else "Declared diagnostics directory is missing or unsafe",
    )

    manual, manual_checks = _load_manual(resolved_artifacts.get("manual"), "manual.json")
    for check in manual_checks:
        checks[check.id] = check
    toc, toc_checks = _load_toc(resolved_artifacts.get("toc"), "toc.json")
    for check in toc_checks:
        checks[check.id] = check

    if manual:
        chapters, elements = _flatten_manual(manual)
        metrics["processed_page_count"] = len(manual.metadata.pages_processed)
        metrics["chapter_count"] = len(chapters)
        metrics["element_count"] = len(elements)
        metrics["element_page_count"] = len({element.page for element, _ in elements})
        metrics["caption_count"] = sum(bool(element.caption_generated and element.caption_generated.strip()) for element, _ in elements)
        table_elements = [
            element
            for element, _ in elements
            if element.type is ElementType.TABLE
        ]
        metrics["table_count"] = len(table_elements)
        metrics["table_serialization_structured_count"] = sum(
            element.table_serialization is not None
            and element.table_serialization.status
            is TableSerializationStatus.STRUCTURED
            for element in table_elements
        )
        metrics["table_serialization_fallback_count"] = sum(
            element.table_serialization is not None
            and element.table_serialization.status
            is TableSerializationStatus.FALLBACK
            for element in table_elements
        )
        metrics["table_serialization_unavailable_count"] = sum(
            element.table_serialization is not None
            and element.table_serialization.status
            is TableSerializationStatus.UNAVAILABLE
            for element in table_elements
        )
        metrics["table_serialized_row_count"] = sum(
            len(element.table_serialization.rows)
            for element in table_elements
            if element.table_serialization is not None
        )
    else:
        chapters, elements = [], []
    if manifest:
        metrics["source_pages_total"] = manifest.source.pages_total
    metrics["toc_entry_count"] = len(toc) if toc is not None else 0

    run_id_errors: list[str] = []
    if manifest is None or manual is None:
        run_id_errors.append("manifest and manual contracts are required")
    elif manifest.run_id != manual.id:
        run_id_errors.append(f"manifest run_id {manifest.run_id!r} != manual id {manual.id!r}")

    _record(
        checks,
        "run_id.coherent",
        not run_id_errors,
        "Manifest and manual run IDs are coherent" if not run_id_errors else "; ".join(run_id_errors),
    )
    validation_declared = bool(
        manifest is not None and manifest.artifacts.validation is not None
    )
    _record(
        checks,
        "validation.artifact",
        manifest is not None,
        (
            "Declared validation report matches the current bundle"
            if validation_declared
            else "Validation artifact is not required"
            if manifest is not None
            else "Manifest contract is required to determine the validation artifact"
        ),
    )

    source_errors = _source_coherence_errors(manifest, manual, toc)
    _record(
        checks,
        "source.coherent",
        not source_errors,
        "Manifest pipeline/source metadata matches manual metadata" if not source_errors else "; ".join(source_errors),
    )

    page_errors = _page_errors(manifest, manual, chapters, elements)
    _record(
        checks,
        "pages.bounds",
        not page_errors,
        "Processed, chapter, and element pages are within source bounds" if not page_errors else "; ".join(page_errors),
    )

    content_coverage_errors = _content_page_coverage_errors(manual, elements)
    _record(
        checks,
        "pages.content.coverage",
        not content_coverage_errors,
        (
            "Canonical elements cover enough processed pages"
            if not content_coverage_errors
            else "; ".join(content_coverage_errors)
        ),
    )

    duplicate_ids = _duplicate_ids(chapters, elements)
    metrics["duplicate_id_count"] = len(duplicate_ids)
    ids_available = manual is not None
    _record(
        checks,
        "ids.unique",
        ids_available and not duplicate_ids,
        (
            "All chapter and element IDs are unique"
            if ids_available and not duplicate_ids
            else f"Duplicate IDs: {', '.join(duplicate_ids)}"
            if duplicate_ids
            else "Manual contract is required"
        ),
    )

    parent_errors = _parent_errors(chapters, elements)
    _record(
        checks,
        "trace.parents.coherent",
        manual is not None and not parent_errors,
        (
            "Chapter nesting and element parent traces are coherent"
            if manual is not None and not parent_errors
            else "; ".join(parent_errors) or "Manual contract is required"
        ),
    )

    asset_references = _asset_references(elements)
    metrics["asset_reference_count"] = len(asset_references)
    unsafe_assets: list[str] = []
    missing_assets: list[str] = []
    for element_id, field_name, relative in asset_references:
        candidate, error = _resolve_inside(root, relative)
        label = f"{element_id}.{field_name}"
        if error:
            unsafe_assets.append(f"{label}: {error}")
        elif candidate is None or not candidate.is_file():
            missing_assets.append(f"{label}: {relative}")
    _record(
        checks,
        "assets.paths.safe",
        manual is not None and not unsafe_assets,
        (
            "All referenced assets remain inside the run"
            if manual is not None and not unsafe_assets
            else "; ".join(unsafe_assets) or "Manual contract is required"
        ),
    )
    _record(
        checks,
        "assets.exist",
        manual is not None and not missing_assets,
        (
            "All referenced assets exist"
            if manual is not None and not missing_assets
            else f"Missing assets: {'; '.join(missing_assets)}"
            if missing_assets
            else "Manual contract is required"
        ),
    )

    caption_errors = _caption_errors(elements)
    _record(
        checks,
        "captions.provenance.consistent",
        manual is not None and not caption_errors,
        (
            "Generated captions and provenance are consistent"
            if manual is not None and not caption_errors
            else "; ".join(caption_errors) or "Manual contract is required"
        ),
    )

    identity_errors = caption_identity_errors(
        manifest,
        (element for element, _ in elements),
    )

    caption_input_errors = _caption_input_errors(manual, elements, root)
    _record(
        checks,
        "captions.input.coherent",
        manual is not None and not caption_input_errors,
        (
            "Caption provenance input digests match deterministic prompt and assets"
            if manual is not None and not caption_input_errors
            else "; ".join(caption_input_errors) or "Manual contract is required"
        ),
    )
    _record(
        checks,
        "captions.provenance.identity",
        manual is not None and not identity_errors,
        (
            "Generated-caption identity, prompt, and runtime match the pipeline"
            if manual is not None and not identity_errors
            else "; ".join(identity_errors) or "Manual contract is required"
        ),
    )

    promotion, promotion_errors = _load_and_verify_promotion(
        manifest=manifest,
        manual=manual,
        toc=toc,
        elements=elements,
        diagnostics_dir=diagnostics_dir,
    )
    _record(
        checks,
        "promotion.evidence.coherent",
        not promotion_errors,
        (
            "Persisted promotion evidence matches canonical bundle artifacts"
            if not promotion_errors
            else "; ".join(promotion_errors)
        ),
    )
    status_promotion_errors = _status_promotion_errors(manifest, promotion)
    _record(
        checks,
        "status.promotion.coherent",
        not status_promotion_errors,
        (
            "Validated status is supported by all promotion gates"
            if manifest is not None and manifest.status is RunStatus.VALIDATED
            and not status_promotion_errors
            else "Run status does not overclaim validated promotion"
            if not status_promotion_errors
            else "; ".join(status_promotion_errors)
        ),
    )

    toc_hierarchy_errors = _toc_hierarchy_errors(manual, toc, chapters)
    _record(
        checks,
        "toc.hierarchy.coherent",
        not toc_hierarchy_errors,
        (
            "TOC entries match the canonical chapter hierarchy"
            if not toc_hierarchy_errors
            else "; ".join(toc_hierarchy_errors)
        ),
    )
    toc_errors = _toc_page_errors(manifest, manual, toc)
    _record(
        checks,
        "toc.pages.bounds",
        not toc_errors,
        "All TOC pages are within source bounds" if not toc_errors else "; ".join(toc_errors),
    )

    table_serialization_errors = _table_serialization_errors(manifest, manual)
    _record(
        checks,
        "tables.serialization.coherent",
        manual is not None and not table_serialization_errors,
        (
            "Canonical table serializations match their source content and context"
            if manual is not None and not table_serialization_errors
            else "; ".join(table_serialization_errors)
            or "Manual contract is required"
        ),
    )

    ordered_ids = (
        "run.directory",
        "manifest.exists",
        "manifest.json",
        "manifest.contract",
        "manifest.status.finished",
        "artifacts.paths.safe",
        "diagnostics.directory",
        "manual.exists",
        "manual.json",
        "manual.contract",
        "toc.exists",
        "toc.json",
        "toc.contract",
        "run_id.coherent",
        "validation.artifact",
        "source.coherent",
        "pages.bounds",
        "pages.content.coverage",
        "ids.unique",
        "trace.parents.coherent",
        "assets.paths.safe",
        "assets.exist",
        "captions.provenance.consistent",
        "captions.provenance.identity",
        "captions.input.coherent",
        "promotion.evidence.coherent",
        "status.promotion.coherent",
        "toc.hierarchy.coherent",
        "toc.pages.bounds",
        "tables.serialization.coherent",
    )
    final_checks = [checks.get(check_id, ValidationCheck(id=check_id, passed=False, message="Check was not evaluated")) for check_id in ordered_ids]
    status = "passed" if all(check.passed for check in final_checks) else "failed"
    metrics["bundle_fingerprint_sha256"] = _bundle_fingerprint(
        root,
        validation_relative=artifact_values.get("validation"),
    )
    metrics["failed_check_count"] = sum(not check.passed for check in final_checks)
    return ValidationReport(run_id=run_id, status=status, checks=final_checks, metrics=metrics)


def _record(checks: dict[str, ValidationCheck], check_id: str, passed: bool, message: str) -> None:
    checks[check_id] = ValidationCheck(id=check_id, passed=passed, message=message)


def _bundle_fingerprint(
    root: Path,
    *,
    validation_relative: Any,
) -> str | None:
    """Hash every persisted bundle file except the report that stores the hash."""

    if not root.is_dir():
        return None
    excluded: set[Path] = set()
    if isinstance(validation_relative, str):
        candidate, error = _resolve_inside(root, validation_relative)
        if error is None and candidate is not None:
            excluded.add(candidate.resolve(strict=False))

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        if not path.is_file() or path.resolve(strict=False) in excluded:
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        try:
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            digest.update(f"unreadable:{type(exc).__name__}".encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _read_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return None, f"Cannot read valid JSON from {path.name}: {error}"


def _validation_message(error: Exception) -> str:
    if isinstance(error, ValidationError):
        parts = []
        for item in error.errors(include_url=False):
            location = ".".join(str(value) for value in item["loc"]) or "root"
            parts.append(f"{location}: {item['msg']}")
        return "; ".join(parts)
    return str(error)


def _raw_string(value: Any, key: str) -> str | None:
    if not isinstance(value, dict):
        return None
    raw = value.get(key)
    return raw if isinstance(raw, str) and raw else None


def _raw_artifact_paths(manifest_raw: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "manual": "manual.json",
        "toc": "toc.json",
        "validation": None,
        "assets": "assets",
        "diagnostics": "diagnostics",
    }
    if not isinstance(manifest_raw, dict) or not isinstance(manifest_raw.get("artifacts"), dict):
        return defaults
    artifacts = manifest_raw["artifacts"]
    for key in defaults:
        if key in artifacts:
            defaults[key] = artifacts[key]
    return defaults


def _resolve_inside(root: Path, relative: str) -> tuple[Path | None, str | None]:
    if (
        not relative
        or not relative.strip()
        or "\x00" in relative
        or any(ord(character) < 32 or ord(character) == 127 for character in relative)
    ):
        return None, "path must be a non-empty relative path"
    normalized = relative.replace("\\", "/")
    pure = PurePosixPath(normalized)
    windows = PureWindowsPath(relative)
    if (
        windows.is_absolute()
        or bool(windows.drive)
        or pure.is_absolute()
        or ".." in pure.parts
        or not pure.parts
    ):
        return None, f"unsafe path {relative!r}"
    try:
        candidate = (root / Path(*pure.parts)).resolve()
        candidate.relative_to(root)
    except (OSError, ValueError):
        return None, f"path escapes run root: {relative!r}"
    return candidate, None


def _artifact_collision_errors(artifact_values: dict[str, Any]) -> list[str]:
    entries: list[tuple[str, tuple[str, ...]]] = [("run manifest", ("run.json",))]
    for name, value in artifact_values.items():
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = value.replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            continue
        entries.append((name, path.parts))

    errors: list[str] = []
    for index, (first_name, first_parts) in enumerate(entries):
        for second_name, second_parts in entries[index + 1 :]:
            common = min(len(first_parts), len(second_parts))
            if first_parts[:common] == second_parts[:common]:
                errors.append(f"{first_name} path collides with {second_name} path")
    return errors


def _safe_fallback_run_id(*candidates: str | None) -> str:
    for candidate in candidates:
        if (
            candidate
            and candidate == candidate.strip()
            and candidate not in {".", ".."}
            and "/" not in candidate
            and "\\" not in candidate
            and not any(ord(character) < 32 or ord(character) == 127 for character in candidate)
        ):
            return candidate
    return "unknown"


def _load_manual(path: Path | None, label: str) -> tuple[ManualDocument | None, list[ValidationCheck]]:
    exists = path is not None and path.is_file()
    checks = [ValidationCheck(id="manual.exists", passed=exists, message=f"{label} exists" if exists else f"{label} is missing or unsafe")]
    raw, error = _read_json(path) if exists and path is not None else (None, f"{label} is missing or unsafe")
    json_valid = isinstance(raw, dict)
    checks.append(ValidationCheck(id="manual.json", passed=json_valid, message=f"{label} contains a JSON object" if json_valid else error or f"{label} must contain a JSON object"))
    manual: ManualDocument | None = None
    contract_error: str | None = None
    if json_valid:
        try:
            manual = ManualDocument.model_validate(raw)
        except (ValidationError, TypeError, ValueError) as exception:
            contract_error = _validation_message(exception)
    checks.append(ValidationCheck(id="manual.contract", passed=manual is not None, message="manual.json matches ManualDocument" if manual else contract_error or "Manual contract was not evaluated"))
    return manual, checks


def _load_toc(path: Path | None, label: str) -> tuple[list[TocEntry] | None, list[ValidationCheck]]:
    exists = path is not None and path.is_file()
    checks = [ValidationCheck(id="toc.exists", passed=exists, message=f"{label} exists" if exists else f"{label} is missing or unsafe")]
    raw, error = _read_json(path) if exists and path is not None else (None, f"{label} is missing or unsafe")
    json_valid = isinstance(raw, list)
    checks.append(ValidationCheck(id="toc.json", passed=json_valid, message=f"{label} contains a JSON array" if json_valid else error or f"{label} must contain a JSON array"))
    toc: list[TocEntry] | None = None
    contract_error: str | None = None
    if json_valid:
        try:
            toc = [TocEntry.model_validate(item) for item in raw]
        except (ValidationError, TypeError, ValueError) as exception:
            contract_error = _validation_message(exception)
    checks.append(ValidationCheck(id="toc.contract", passed=toc is not None, message="toc.json entries match TocEntry" if toc is not None else contract_error or "TOC contract was not evaluated"))
    return toc, checks


def _validation_reports_semantically_equal(
    declared: ValidationReport,
    expected: ValidationReport,
) -> bool:
    """Bind validation facts and metrics without freezing explanatory prose."""

    declared_checks = {check.id: check.passed for check in declared.checks}
    expected_checks = {check.id: check.passed for check in expected.checks}
    return (
        len(declared_checks) == len(declared.checks)
        and len(expected_checks) == len(expected.checks)
        and declared.schema_version == expected.schema_version
        and declared.run_id == expected.run_id
        and declared.status == expected.status
        and declared_checks == expected_checks
        and declared.metrics == expected.metrics
    )


def _declared_validation_error(
    root: Path,
    expected: ValidationReport,
) -> str | None:
    manifest_path = root / "run.json"
    raw, error = _read_json(manifest_path)
    if error or not isinstance(raw, dict):
        return "Manifest contract is required to verify the validation artifact"
    try:
        manifest = RunManifest.model_validate(raw)
    except (ValidationError, TypeError, ValueError):
        return "Manifest contract is required to verify the validation artifact"
    relative = manifest.artifacts.validation
    if relative is None:
        return None
    path, path_error = _resolve_inside(root, relative)
    if path_error or path is None or not path.is_file():
        return "Declared validation artifact is missing or unsafe"
    declared_raw, read_error = _read_json(path)
    if read_error:
        return read_error
    try:
        declared = ValidationReport.model_validate(declared_raw)
    except (ValidationError, TypeError, ValueError) as exception:
        return _validation_message(exception)
    if (
        manifest.status in {RunStatus.VALIDATED, RunStatus.EXPERIMENTAL}
        and declared.status != "passed"
    ):
        return (
            f"{manifest.status.value} status requires a passed declared "
            "validation report"
        )
    if not _validation_reports_semantically_equal(declared, expected):
        return "Declared validation report is stale or does not match the current bundle"
    return None


def _replace_validation_artifact_check(
    report: ValidationReport,
    message: str,
) -> ValidationReport:
    checks = [
        ValidationCheck(
            id=check.id,
            passed=False,
            message=message,
        )
        if check.id == "validation.artifact"
        else check
        for check in report.checks
    ]
    metrics = dict(report.metrics)
    metrics["failed_check_count"] = sum(not check.passed for check in checks)
    return ValidationReport(
        run_id=report.run_id,
        status="failed",
        checks=checks,
        metrics=metrics,
    )


def _load_and_verify_promotion(
    *,
    manifest: RunManifest | None,
    manual: ManualDocument | None,
    toc: list[TocEntry] | None,
    elements: list[tuple[ManualElement, Chapter | None]],
    diagnostics_dir: Path | None,
) -> tuple[PromotionDiagnostic | None, list[str]]:
    if manifest is None or manual is None:
        return None, ["manifest and manual contracts are required for promotion evidence"]

    required = manifest.status in {RunStatus.VALIDATED, RunStatus.EXPERIMENTAL}
    promotion_path = diagnostics_dir / "promotion.json" if diagnostics_dir else None
    promotion_exists = promotion_path is not None and promotion_path.is_file()
    if not required and not promotion_exists:
        return None, []
    if diagnostics_dir is None or not diagnostics_dir.is_dir():
        return None, ["canonical promotion diagnostics directory is missing or unsafe"]

    detection, detection_error = _load_diagnostic_contract(
        diagnostics_dir / "detection.json",
        DetectionDiagnostic,
        "detection.json",
    )
    crop_filter, crop_error = _load_diagnostic_contract(
        diagnostics_dir / "crop_filter.json",
        CropFilterDiagnostic,
        "crop_filter.json",
    )
    enrichment, enrichment_error = _load_diagnostic_contract(
        diagnostics_dir / "enrichment.json",
        EnrichmentDiagnostic,
        "enrichment.json",
    )
    structure, structure_error = _load_diagnostic_contract(
        diagnostics_dir / "structure.json",
        StructureDiagnostic,
        "structure.json",
    )
    promotion, promotion_error = _load_diagnostic_contract(
        diagnostics_dir / "promotion.json",
        PromotionDiagnostic,
        "promotion.json",
    )
    runtime, runtime_error = _load_diagnostic_contract(
        diagnostics_dir / "runtime.json",
        RuntimeDiagnostic,
        "runtime.json",
    )
    errors = [
        error
        for error in (
            detection_error,
            crop_error,
            enrichment_error,
            structure_error,
            promotion_error,
            runtime_error,
        )
        if error is not None
    ]
    if errors:
        return None, errors
    assert isinstance(detection, DetectionDiagnostic)
    assert isinstance(crop_filter, CropFilterDiagnostic)
    assert isinstance(enrichment, EnrichmentDiagnostic)
    assert isinstance(structure, StructureDiagnostic)
    assert isinstance(promotion, PromotionDiagnostic)
    assert isinstance(runtime, RuntimeDiagnostic)

    raw_routing = manifest.pipeline.config.get("routing")
    expected_routing = {
        "mode": "automatic",
        "strategy": AUTOMATIC_ROUTING_STRATEGY,
        "detected_profile": manifest.source.detected.profile.value,
        "profile_confidence": manifest.source.detected.profile_confidence,
    }
    if raw_routing != expected_routing:
        errors.append("pipeline routing config differs from canonical detection evidence")
    if detection.capabilities != manifest.source.detected:
        errors.append("detection diagnostic differs from manifest capabilities")
    expected_pages_config = {
        "selection": (
            "full_document"
            if manual.metadata.pages_processed
            == list(range(1, manifest.source.pages_total + 1))
            else "explicit_partial"
        ),
        "processed": manual.metadata.pages_processed,
    }
    if manifest.pipeline.config.get("pages") != expected_pages_config:
        errors.append("pipeline page-selection config differs from manual metadata")

    raw_postprocess = manifest.pipeline.config.get("postprocess")
    raw_crop_config = (
        raw_postprocess.get("visual_crop_filter")
        if isinstance(raw_postprocess, dict)
        else None
    )
    expected_crop_config = {
        "strategy": crop_filter.strategy,
        "threshold": crop_filter.threshold,
    }
    if raw_crop_config != expected_crop_config:
        errors.append("crop-filter diagnostic differs from pipeline config")

    raw_enrichment = manifest.pipeline.config.get("enrichment")
    enabled = (
        isinstance(raw_enrichment, dict)
        and raw_enrichment.get("enabled") is True
    )
    if enrichment.provider_configured != enabled:
        errors.append(
            "enrichment diagnostic provider_configured differs from pipeline config"
        )
    configured_preflight = (
        raw_enrichment.get("preflight")
        if isinstance(raw_enrichment, dict)
        else None
    )
    if configured_preflight != enrichment.provider_preflight:
        errors.append(
            "enrichment diagnostic preflight differs from pipeline config"
        )
    provider_config = (
        raw_enrichment.get("provider_config")
        if isinstance(raw_enrichment, dict)
        else None
    )
    if not enabled and provider_config is not None:
        errors.append("disabled enrichment cannot declare provider_config")
    if provider_config is not None:
        if not isinstance(provider_config, dict):
            errors.append("enrichment provider_config must be an object or null")
        elif provider_config.get("model") != manifest.pipeline.enrichment_model:
            errors.append("enrichment provider_config model differs from pipeline model")
    if structure.strategy != manifest.pipeline.structure_strategy:
        errors.append("structure diagnostic strategy differs from pipeline strategy")
    if structure.toc_available != manual.metadata.toc_available:
        errors.append("structure diagnostic TOC availability differs from manual metadata")
    raw_structure = manifest.pipeline.config.get("structure")
    expected_structure_config = {
        "strategy": structure.strategy,
        "config": (
            structure.details.get("config")
            if isinstance(structure.details, dict)
            and isinstance(structure.details.get("config"), dict)
            else None
        ),
    }
    if raw_structure != expected_structure_config:
        errors.append("structure diagnostic differs from pipeline structure config")
    errors.extend(
        _profile_structure_shape_errors(
            manifest=manifest,
            manual=manual,
            toc=toc,
            structure=structure,
        )
    )

    flat_elements = [element for element, _ in elements]
    visual_ids = {
        element.id
        for element in flat_elements
        if element.type in {ElementType.IMAGE, ElementType.TABLE}
    }
    diagnostic_ids = {item.element_id for item in enrichment.items}
    if diagnostic_ids != visual_ids:
        errors.append(
            "enrichment diagnostic item IDs differ from canonical visual element IDs"
        )
    errors.extend(_crop_filter_errors(crop_filter, flat_elements))
    errors.extend(_enrichment_item_errors(enrichment, flat_elements))
    errors.extend(
        _pipeline_transition_errors(
            crop_filter=crop_filter,
            enrichment=enrichment,
            elements=flat_elements,
        )
    )
    errors.extend(
        _trace_exclusion_errors(
            manifest=manifest,
            crop_filter=crop_filter,
            enrichment=enrichment,
            structure=structure,
            elements=flat_elements,
        )
    )
    errors.extend(
        _provider_parameter_errors(
            provider_config=provider_config,
            elements=flat_elements,
        )
    )

    review_ids: set[str] = set()
    for element in flat_elements:
        if not element.caption_generated:
            continue
        try:
            if caption_review_reason(
                element.caption_generated,
                prompt_version=(element.caption_provenance.prompt_version
                                if element.caption_provenance else CURRENT_PROMPT_VERSION),
            ) is not None:
                review_ids.add(element.id)
        except ValueError:
            continue
    diagnostic_review_ids = {
        item.element_id
        for item in enrichment.items
        if item.status == "review_required"
    }
    if diagnostic_review_ids != review_ids:
        errors.append(
            "enrichment diagnostic review items differ from canonical captions"
        )

    preflight_model = enrichment.provider_preflight.get("model")
    if (
        isinstance(preflight_model, str)
        and manifest.pipeline.enrichment_model != preflight_model
    ):
        errors.append("enrichment preflight model differs from pipeline model")
    errors.extend(
        _runtime_diagnostic_errors(
            manifest=manifest,
            enrichment=enrichment,
            runtime=runtime,
        )
    )
    errors.extend(
        _structure_diagnostic_errors(
            manifest=manifest,
            manual=manual,
            toc=toc,
            structure=structure,
            elements=flat_elements,
        )
    )
    persisted_evidence_id = promotion.acceptance_evidence_id
    if (
        persisted_evidence_id is not None
        and persisted_evidence_id not in SCANNED_OCR_ACCEPTANCE_EVIDENCE_IDS
    ):
        errors.append(
            "promotion acceptance evidence ID is not present in the accepted registry"
        )
    expected = build_promotion_assessment(
        manifest=manifest,
        manual=manual,
        enrichment=enrichment,
        structure=structure,
        profile_warnings=promotion.profile_warnings,
        # Persisted evidence is part of the historical run contract. A bundle
        # produced before profile acceptance therefore remains valid as an
        # experimental bundle after a later registry update.
        scanned_acceptance_evidence_id=persisted_evidence_id,
    )
    if not _promotion_diagnostics_semantically_equal(promotion, expected):
        errors.append("promotion.json differs from the recomputed promotion assessment")
    if manifest.status in {RunStatus.VALIDATED, RunStatus.EXPERIMENTAL}:
        failed_gate_count = len(failed_promotion_gate_ids(promotion))
        prefix_matches = (
            manifest.warnings[: len(promotion.profile_warnings)]
            == promotion.profile_warnings
        )
        warning_count_matches = len(manifest.warnings) == (
            len(promotion.profile_warnings) + failed_gate_count
        )
        warnings_are_canonical = all(
            warning and warning.strip() == warning
            for warning in manifest.warnings
        ) and len(manifest.warnings) == len(set(manifest.warnings))
        if not (prefix_matches and warning_count_matches and warnings_are_canonical):
            errors.append(
                "manifest warnings do not preserve profile warnings and one reason "
                "per failed promotion gate"
            )
    return promotion, errors


def _promotion_diagnostics_semantically_equal(
    declared: PromotionDiagnostic,
    expected: PromotionDiagnostic,
) -> bool:
    """Compare policy facts while treating messages as presentation text."""

    return (
        declared.schema_version == expected.schema_version
        and declared.kind == expected.kind
        and declared.policy_version == expected.policy_version
        and declared.eligible_for_validated == expected.eligible_for_validated
        and declared.acceptance_evidence_id == expected.acceptance_evidence_id
        and declared.profile_warnings == expected.profile_warnings
        and [(gate.id, gate.passed) for gate in declared.gates]
        == [(gate.id, gate.passed) for gate in expected.gates]
    )


def _enrichment_item_errors(
    enrichment: EnrichmentDiagnostic,
    elements: list[ManualElement],
) -> list[str]:
    """Bind every enrichment item status to its canonical visual state."""

    by_id = {element.id: element for element in elements}
    errors: list[str] = []
    for item in enrichment.items:
        element = by_id.get(item.element_id)
        if element is None:
            continue
        caption = element.caption_generated
        provenance = element.caption_provenance
        review_reason: str | None = None
        if caption:
            try:
                review_reason = caption_review_reason(
                    caption,
                    prompt_version=(provenance.prompt_version
                                    if provenance else CURRENT_PROMPT_VERSION),
                )
            except ValueError:
                errors.append(
                    f"enrichment item {item.element_id!r} references an invalid caption"
                )
                continue

        if item.status == "enriched":
            coherent = bool(
                enrichment.provider_configured
                and caption
                and provenance is not None
                and review_reason is None
                and element.trace.include_in_rag
                and item.reason == CAPTION_VALIDATED_REASON
            )
        elif item.status == "review_required":
            coherent = bool(
                enrichment.provider_configured
                and caption
                and provenance is not None
                and review_reason is not None
                and not element.trace.include_in_rag
                and element.trace.exclusion_reason == review_reason
                and item.reason == review_reason
            )
        elif item.status == "skipped":
            excluded_coherently = bool(
                not element.trace.include_in_rag
                and element.trace.exclusion_reason
                and item.reason == element.trace.exclusion_reason
                and review_reason is None
            )
            preexisting_caption = bool(
                caption
                and provenance is not None
                and element.trace.include_in_rag
                and review_reason is None
                and item.reason == CAPTION_ALREADY_PRESENT_REASON
            )
            structured_table = bool(
                element.type is ElementType.TABLE
                and element.trace.include_in_rag
                and not caption
                and provenance is None
                and item.reason == TABLE_STRUCTURED_SERIALIZATION_REASON
            )
            coherent = excluded_coherently or preexisting_caption or structured_table
        else:  # failed
            coherent = bool(
                enrichment.provider_configured
                and element.type is ElementType.IMAGE
                and not caption
                and provenance is None
                and not element.trace.include_in_rag
                and element.trace.exclusion_reason == CAPTION_UNAVAILABLE_REASON
                and item.reason.strip()
            )

        if not coherent:
            errors.append(
                f"enrichment item {item.element_id!r} status {item.status!r} "
                "does not match the canonical visual state"
            )
    return errors


def _crop_filter_errors(
    crop_filter: CropFilterDiagnostic,
    elements: list[ManualElement],
) -> list[str]:
    visual_by_id = {
        element.id: element
        for element in elements
        if element.type in {ElementType.IMAGE, ElementType.TABLE}
    }
    decision_ids = {decision.element_id for decision in crop_filter.decisions}
    errors: list[str] = []
    if decision_ids != set(visual_by_id):
        errors.append(
            "crop-filter decision IDs differ from canonical visual element IDs"
        )
    if crop_filter.strategy != VISUAL_CROP_FILTER_STRATEGY:
        errors.append("crop-filter strategy is not the accepted strategy")

    for decision in crop_filter.decisions:
        element = visual_by_id.get(decision.element_id)
        if element is None:
            continue
        if decision.element_type is not element.type:
            errors.append(
                f"crop decision {decision.element_id!r} type differs from manual"
            )
        bbox = element.source.bbox
        page_size = element.source.page_size
        expected_area = (
            None
            if bbox is None or page_size is None
            else ((bbox.x1 - bbox.x0) * (bbox.y1 - bbox.y0))
            / (page_size.width * page_size.height)
        )
        if expected_area is None:
            area_matches = decision.area_fraction is None
        else:
            area_matches = (
                decision.area_fraction is not None
                and math.isclose(
                    decision.area_fraction,
                    expected_area,
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
            )
        if not area_matches:
            errors.append(
                f"crop decision {decision.element_id!r} area differs from source geometry"
            )

        if decision.status == "excluded":
            coherent = bool(
                expected_area is not None
                and expected_area < crop_filter.threshold
                and not element.trace.include_in_rag
                and element.trace.exclusion_reason == SMALL_VISUAL_EXCLUSION_REASON
                and decision.reason == SMALL_VISUAL_EXCLUSION_REASON
            )
        elif decision.status == "kept":
            coherent = bool(
                expected_area is not None
                and expected_area >= crop_filter.threshold
                and decision.reason == VISUAL_CROP_KEPT_REASON
            )
        elif decision.status == "unassessed":
            coherent = bool(
                expected_area is None
                and decision.reason == VISUAL_CROP_UNASSESSED_REASON
            )
        else:  # preserved
            coherent = bool(
                not element.trace.include_in_rag
                and element.trace.exclusion_reason
                and decision.reason == element.trace.exclusion_reason
                and not (
                    decision.reason == SMALL_VISUAL_EXCLUSION_REASON
                    and expected_area is not None
                    and expected_area < crop_filter.threshold
                )
            )
        if not coherent:
            errors.append(
                f"crop decision {decision.element_id!r} status {decision.status!r} "
                "does not match canonical geometry and trace"
            )
    return errors


def _pipeline_transition_errors(
    *,
    crop_filter: CropFilterDiagnostic,
    enrichment: EnrichmentDiagnostic,
    elements: list[ManualElement],
) -> list[str]:
    """Verify the allowed crop→enrichment→manual state transitions."""

    by_id = {element.id: element for element in elements}
    enrichment_by_id = {item.element_id: item for item in enrichment.items}
    errors: list[str] = []
    for crop in crop_filter.decisions:
        element = by_id.get(crop.element_id)
        item = enrichment_by_id.get(crop.element_id)
        if element is None or item is None:
            continue
        if crop.status in {"excluded", "preserved"}:
            coherent = (
                item.status == "skipped"
                and not element.trace.include_in_rag
                and item.reason == crop.reason == element.trace.exclusion_reason
            )
        elif item.status == "enriched":
            coherent = element.trace.include_in_rag
        elif item.status == "review_required":
            coherent = not element.trace.include_in_rag
        elif item.status == "failed":
            coherent = (
                element.type is ElementType.IMAGE
                and not element.trace.include_in_rag
                and element.trace.exclusion_reason == CAPTION_UNAVAILABLE_REASON
            )
        elif (
            element.type is ElementType.TABLE
            and item.status == "skipped"
            and item.reason == TABLE_STRUCTURED_SERIALIZATION_REASON
            and element.trace.include_in_rag
        ):
            coherent = True
        elif (
            item.status == "skipped"
            and item.reason == CAPTION_ALREADY_PRESENT_REASON
            and element.trace.include_in_rag
        ):
            coherent = True
        elif enrichment.provider_configured:
            coherent = False
        else:
            coherent = (
                item.status == "skipped"
                and item.reason == DIAGNOSTIC_VISUAL_EXCLUSION_REASON
                and not element.trace.include_in_rag
                and element.trace.exclusion_reason
                == DIAGNOSTIC_VISUAL_EXCLUSION_REASON
            )
        if not coherent:
            errors.append(
                f"visual {crop.element_id!r} has an impossible crop-to-enrichment transition"
            )
    return errors


def _trace_exclusion_errors(
    *,
    manifest: RunManifest,
    crop_filter: CropFilterDiagnostic,
    enrichment: EnrichmentDiagnostic,
    structure: StructureDiagnostic,
    elements: list[ManualElement],
) -> list[str]:
    crop_by_id = {decision.element_id: decision for decision in crop_filter.decisions}
    enrichment_by_id = {item.element_id: item for item in enrichment.items}
    detected_toc_pages = (
        set(structure.details.get("detected_toc_pages", []))
        if isinstance(structure.details, dict)
        and isinstance(structure.details.get("detected_toc_pages"), list)
        else set()
    )
    errors: list[str] = []
    for element in elements:
        if element.trace.include_in_rag:
            continue
        reason = element.trace.exclusion_reason
        crop = crop_by_id.get(element.id)
        item = enrichment_by_id.get(element.id)
        small_crop = bool(
            reason == SMALL_VISUAL_EXCLUSION_REASON
            and element.type in {ElementType.IMAGE, ElementType.TABLE}
            and crop is not None
            and crop.status == "excluded"
        )
        diagnostic_no_provider = bool(
            reason == DIAGNOSTIC_VISUAL_EXCLUSION_REASON
            and element.type is ElementType.IMAGE
            and not enrichment.provider_configured
            and item is not None
            and item.status == "skipped"
            and item.reason == reason
        )
        try:
            canonical_review_reason = (
                caption_review_reason(
                    element.caption_generated,
                    prompt_version=(element.caption_provenance.prompt_version
                                    if element.caption_provenance else CURRENT_PROMPT_VERSION),
                )
                if element.caption_generated
                else None
            )
        except ValueError:
            canonical_review_reason = None
        review_required = bool(
            canonical_review_reason == reason
            and canonical_review_reason is not None
            and item is not None
            and item.status == "review_required"
        )
        caption_unavailable = bool(
            reason == CAPTION_UNAVAILABLE_REASON
            and element.type is ElementType.IMAGE
            and enrichment.provider_configured
            and item is not None
            and item.status == "failed"
        )
        printed_toc = bool(
            reason == PRINTED_TOC_EXCLUSION_REASON
            and manifest.pipeline.profile is DocumentProfile.SCANNED_OCR
            and element.page in detected_toc_pages
        )
        if not (
            small_crop
            or diagnostic_no_provider
            or review_required
            or caption_unavailable
            or printed_toc
        ):
            errors.append(
                f"element {element.id!r} has an unsupported retrieval exclusion"
            )
    return errors


def _provider_parameter_errors(
    *,
    provider_config: object,
    elements: list[ManualElement],
) -> list[str]:
    if not isinstance(provider_config, dict):
        return []
    output_keys = ("temperature", "num_predict", "repeat_penalty", "num_ctx")
    errors: list[str] = []
    for element in elements:
        provenance = element.caption_provenance
        if provenance is None:
            continue
        for key in output_keys:
            if key in provider_config and provenance.params.get(key) != provider_config[key]:
                errors.append(
                    f"caption provenance parameter {key!r} for {element.id!r} "
                    "differs from provider_config"
                )
    return errors


def _runtime_diagnostic_errors(
    *,
    manifest: RunManifest,
    enrichment: EnrichmentDiagnostic,
    runtime: RuntimeDiagnostic,
) -> list[str]:
    errors: list[str] = []
    raw_parser = manifest.pipeline.config.get("parser")
    if not isinstance(raw_parser, dict):
        errors.append("pipeline parser config must be an object")
        raw_parser = {}
    if raw_parser.get("name") != manifest.pipeline.parser:
        errors.append("pipeline parser config name differs from selected parser")
    if runtime.profile_runtime != raw_parser.get("runtime"):
        errors.append("runtime diagnostic profile runtime differs from pipeline config")
    expected_provider_runtime = enrichment.provider_preflight or None
    if runtime.provider_runtime != expected_provider_runtime:
        errors.append("runtime diagnostic provider runtime differs from enrichment preflight")
    if runtime.paddle_runtime_config != manifest.pipeline.config.get("paddle_runtime"):
        errors.append("runtime diagnostic Paddle config differs from pipeline config")

    profile_runtime = runtime.profile_runtime
    if not isinstance(profile_runtime, dict):
        return [*errors, "profile runtime metadata must be an object"]
    required = {"python", *ACCEPTED_CORE_RUNTIME_VERSIONS}
    if manifest.pipeline.parser == "docling":
        required.add("docling")
    missing = sorted(
        key
        for key in required
        if not isinstance(profile_runtime.get(key), str)
        or not str(profile_runtime[key]).strip()
    )
    if missing:
        errors.append("profile runtime is missing version(s): " + ", ".join(missing))
    python_version = profile_runtime.get("python")
    if not isinstance(python_version, str) or not python_version.startswith("3.12"):
        errors.append("profile runtime must record Python 3.12")
    for package, accepted in ACCEPTED_CORE_RUNTIME_VERSIONS.items():
        if profile_runtime.get(package) != accepted:
            errors.append(
                f"profile runtime {package} differs from accepted {accepted}"
            )
    if (
        manifest.pipeline.parser == "docling"
        and profile_runtime.get("docling") != ACCEPTED_DOCLING_VERSION
    ):
        errors.append(
            f"Docling runtime differs from accepted {ACCEPTED_DOCLING_VERSION}"
        )

    if manifest.pipeline.profile is DocumentProfile.SCANNED_OCR:
        for package, accepted in ACCEPTED_PADDLE_PACKAGE_VERSIONS.items():
            if profile_runtime.get(package) != accepted:
                errors.append(
                    f"scanned_ocr runtime {package} differs from accepted {accepted}"
                )
    return errors


def _profile_structure_shape_errors(
    *,
    manifest: RunManifest,
    manual: ManualDocument,
    toc: list[TocEntry] | None,
    structure: StructureDiagnostic,
) -> list[str]:
    profile = manifest.pipeline.profile
    strategy = manifest.pipeline.structure_strategy
    details = structure.details
    if not manual.metadata.toc_available or not toc:
        return ["selected profile requires a non-empty canonical TOC"]
    if profile is DocumentProfile.DIGITAL_OUTLINE:
        return (
            []
            if strategy == "embedded_outline" and details is None
            else ["digital_outline requires embedded-outline structure without details"]
        )
    if profile is DocumentProfile.DIGITAL_RECONSTRUCTED:
        if strategy != STRUCTURE_STRATEGY or details is None:
            return ["digital_reconstructed requires persisted reconstruction details"]
        return _digital_reconstruction_detail_errors(
            details=details,
            toc=toc,
            pages_total=manual.metadata.pages_total,
        )
    if strategy == "embedded_outline_with_paddle_ocr":
        return (
            []
            if manifest.source.detected.embedded_outline and details is None
            else ["scanned embedded-outline setup must not declare OCR reconstruction"]
        )
    if strategy in {OCR_STRUCTURE_STRATEGY, OCR_FALLBACK_STRATEGY} and details is not None:
        return []
    return ["scanned_ocr structure shape differs from the accepted setup"]


def _digital_reconstruction_detail_errors(
    *,
    details: dict[str, Any],
    toc: list[TocEntry],
    pages_total: int,
) -> list[str]:
    errors: list[str] = []
    if details.get("strategy") != STRUCTURE_STRATEGY:
        errors.append("digital reconstruction diagnostic strategy is invalid")
    raw_config = details.get("config")
    if not isinstance(raw_config, dict):
        return [*errors, "digital reconstruction config must be an object"]
    try:
        config = ReconstructionConfig(**raw_config)
    except (TypeError, ValueError) as exc:
        return [*errors, f"digital reconstruction config is invalid: {exc}"]
    if config.to_dict() != raw_config:
        errors.append("digital reconstruction config is not canonical")
    rows = details.get("printed_rows")
    matches = details.get("matches")
    if not isinstance(rows, list) or not rows:
        return [*errors, "digital reconstruction printed_rows must be non-empty"]
    if not isinstance(matches, list) or len(matches) != len(rows):
        return [*errors, "digital reconstruction matches must cover every printed row"]
    rows_by_index: dict[int, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"digital printed row {index} must be an object")
            continue
        row_index = row.get("index")
        level = row.get("level")
        title = row.get("title")
        source_page = row.get("source_page")
        if (
            not isinstance(row_index, int)
            or isinstance(row_index, bool)
            or row_index in rows_by_index
            or not isinstance(level, int)
            or isinstance(level, bool)
            or level < 1
            or not isinstance(title, str)
            or not title.strip()
            or not isinstance(source_page, int)
            or not 1 <= source_page <= pages_total
        ):
            errors.append(f"digital printed row {index} has invalid evidence fields")
            continue
        rows_by_index[row_index] = row
    reconstructed: list[TocEntry] = []
    seen: set[tuple[int, str, int]] = set()
    matched_rows: set[int] = set()
    for index, match in enumerate(matches):
        if not isinstance(match, dict):
            errors.append(f"digital fusion match {index} must be an object")
            continue
        row_index = match.get("row_index")
        page = match.get("page")
        if (
            not isinstance(row_index, int)
            or isinstance(row_index, bool)
            or row_index in matched_rows
            or row_index not in rows_by_index
        ):
            errors.append(f"digital fusion match {index} references an invalid row")
            continue
        matched_rows.add(row_index)
        if page is None:
            if match.get("decision") != "unresolved":
                errors.append(f"digital fusion match {index} has incoherent resolution")
            continue
        if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= pages_total:
            errors.append(f"digital fusion match {index} has an invalid page")
            continue
        row = rows_by_index[row_index]
        key = (int(row["level"]), normalize_title(str(row["title"])), page)
        if key in seen:
            continue
        seen.add(key)
        reconstructed.append(
            TocEntry(
                level=int(row["level"]),
                title=str(row["title"]),
                page=page,
            )
        )
    if matched_rows != set(rows_by_index):
        errors.append("digital fusion matches do not cover every valid row")
    if reconstructed != toc:
        errors.append("digital fusion diagnostics differ from toc.json")
    if details.get("entry_count") != len(reconstructed):
        errors.append("digital reconstruction entry_count differs from matches")
    detected_pages = details.get("detected_toc_pages")
    if (
        not isinstance(detected_pages, list)
        or not detected_pages
        or detected_pages != sorted(set(detected_pages))
        or any(
            not isinstance(page, int) or not 1 <= page <= pages_total
            for page in detected_pages
        )
    ):
        errors.append("digital reconstruction detected_toc_pages are invalid")
    for required_key, expected_type in (
        ("heading_candidates", list),
        ("ignored_repeated_lines", list),
        ("page_number_map", dict),
    ):
        if not isinstance(details.get(required_key), expected_type):
            errors.append(f"digital reconstruction {required_key} has invalid type")
    return errors


def _structure_diagnostic_errors(
    *,
    manifest: RunManifest,
    manual: ManualDocument,
    toc: list[TocEntry] | None,
    structure: StructureDiagnostic,
    elements: list[ManualElement],
) -> list[str]:
    if (
        manifest.pipeline.profile is not DocumentProfile.SCANNED_OCR
        or manifest.pipeline.structure_strategy
        not in {OCR_STRUCTURE_STRATEGY, OCR_FALLBACK_STRATEGY}
    ):
        return []
    if toc is None or structure.details is None:
        return ["scanned printed-index structure requires TOC and diagnostic details"]
    raw_config = structure.details.get("config")
    if not isinstance(raw_config, dict):
        return ["scanned structure diagnostic config must be an object"]
    try:
        config = OcrReconstructionConfig(**raw_config)
        recomputed = reconstruct_toc_from_ocr_elements(
            elements,
            pages_total=manual.metadata.pages_total,
            config=config,
        )
    except (TypeError, ValueError, OcrTocReconstructionError) as exc:
        return [f"scanned structure cannot be recomputed from manual evidence: {exc}"]
    errors: list[str] = []
    if list(recomputed.entries) != toc:
        errors.append("recomputed scanned structure differs from toc.json")
    expected_details = recomputed.diagnostics.to_dict()
    # Warning prose is explanatory; all structural facts remain bound exactly.
    declared_facts = {
        key: value
        for key, value in structure.details.items()
        if key != "warnings"
    }
    expected_facts = {
        key: value
        for key, value in expected_details.items()
        if key != "warnings"
    }
    if declared_facts != expected_facts:
        errors.append(
            "scanned structure diagnostic differs from deterministic reconstruction"
        )
    return errors


def _load_diagnostic_contract(
    path: Path,
    model: type[DetectionDiagnostic]
    | type[CropFilterDiagnostic]
    | type[EnrichmentDiagnostic]
    | type[StructureDiagnostic]
    | type[PromotionDiagnostic]
    | type[RuntimeDiagnostic],
    label: str,
) -> tuple[
    DetectionDiagnostic
    | CropFilterDiagnostic
    | EnrichmentDiagnostic
    | StructureDiagnostic
    | PromotionDiagnostic
    | RuntimeDiagnostic
    | None,
    str | None,
]:
    if not path.is_file():
        return None, f"{label} is missing"
    raw, error = _read_json(path)
    if error:
        return None, error
    try:
        return model.model_validate(raw), None
    except (ValidationError, TypeError, ValueError) as exception:
        return None, f"{label}: {_validation_message(exception)}"


def _status_promotion_errors(
    manifest: RunManifest | None,
    promotion: PromotionDiagnostic | None,
) -> list[str]:
    if manifest is None:
        return ["manifest contract is required for status promotion"]
    if manifest.status is not RunStatus.VALIDATED:
        return []
    if promotion is None:
        return ["Validated status is unsupported: promotion evidence is missing"]
    failed_ids = failed_promotion_gate_ids(promotion)
    if failed_ids:
        return [
            "Validated status is unsupported by promotion gates: "
            + ", ".join(failed_ids)
        ]
    return []


def _flatten_manual(
    manual: ManualDocument,
) -> tuple[list[tuple[Chapter, Chapter | None]], list[tuple[ManualElement, Chapter | None]]]:
    chapters: list[tuple[Chapter, Chapter | None]] = []
    elements: list[tuple[ManualElement, Chapter | None]] = []

    def walk(items: Iterable[Chapter | ManualElement], parent: Chapter | None) -> None:
        for item in items:
            if isinstance(item, Chapter):
                chapters.append((item, parent))
                walk(item.content, item)
            else:
                elements.append((item, parent))

    walk(manual.content, None)
    return chapters, elements


def _source_coherence_errors(
    manifest: RunManifest | None,
    manual: ManualDocument | None,
    toc: list[TocEntry] | None,
) -> list[str]:
    if manifest is None or manual is None:
        return ["manifest and manual contracts are required"]
    errors: list[str] = []
    metadata = manual.metadata
    if metadata.pages_total != manifest.source.pages_total:
        errors.append("manual pages_total differs from source pages_total")
    if manual.source_file != manifest.source.file:
        errors.append("manual source_file differs from manifest source file")
    if metadata.profile != manifest.pipeline.profile:
        errors.append("manual profile differs from pipeline profile")
    if metadata.parser != manifest.pipeline.parser:
        errors.append("manual parser differs from pipeline parser")
    if metadata.structure_strategy != manifest.pipeline.structure_strategy:
        errors.append("manual structure_strategy differs from pipeline strategy")
    if toc is not None and metadata.toc_available != bool(toc):
        errors.append("manual toc_available differs from toc.json content")
    return errors


def _table_serialization_errors(
    manifest: RunManifest | None,
    manual: ManualDocument | None,
) -> list[str]:
    """Bind persisted table rows to their canonical source and context.

    Serialization is deterministic and intentionally independent from retrieval.
    Recomputing it here detects missing or altered rows without claiming that the
    parser's source cells are semantically correct.
    """

    if manual is None:
        return ["manual contract is required"]

    _, elements = _flatten_manual(manual)
    table_elements = [
        element for element, _ in elements if element.type is ElementType.TABLE
    ]
    expected_config = {"strategy": TABLE_SERIALIZATION_STRATEGY}
    configured = (
        manifest.pipeline.config.get("table_serialization")
        if manifest is not None
        else None
    )
    errors: list[str] = []
    if table_elements and configured != expected_config:
        errors.append(
            "pipeline table_serialization config must declare strategy "
            f"{TABLE_SERIALIZATION_STRATEGY!r}"
        )
    elif configured is not None and configured != expected_config:
        errors.append(
            "pipeline table_serialization config differs from the canonical strategy"
        )

    expected_manual = serialize_manual_tables(manual)
    _, expected_elements = _flatten_manual(expected_manual)
    expected_by_id = {
        element.id: element
        for element, _ in expected_elements
        if element.type is ElementType.TABLE
    }
    for element in table_elements:
        expected = expected_by_id[element.id].table_serialization
        if element.table_serialization is None:
            errors.append(f"table {element.id!r} is missing table_serialization")
        elif element.table_serialization != expected:
            errors.append(
                f"table {element.id!r} serialization differs from canonical source "
                "content or context"
            )
    return errors


def _page_errors(
    manifest: RunManifest | None,
    manual: ManualDocument | None,
    chapters: list[tuple[Chapter, Chapter | None]],
    elements: list[tuple[ManualElement, Chapter | None]],
) -> list[str]:
    if manifest is None or manual is None:
        return ["manifest and manual contracts are required"]
    total = manifest.source.pages_total
    processed = set(manual.metadata.pages_processed)
    errors: list[str] = []
    if not processed:
        errors.append("pages_processed cannot be empty for a finished run")
    invalid_processed = sorted(page for page in processed if page > total)
    if invalid_processed:
        errors.append(f"processed pages outside 1..{total}: {invalid_processed}")
    invalid_sampled = sorted(page for page in manifest.source.detected.sampled_pages if page < 1 or page > total)
    if invalid_sampled:
        errors.append(f"sampled pages outside 1..{total}: {invalid_sampled}")
    if manifest.source.detected.pages_with_text > len(manifest.source.detected.sampled_pages):
        errors.append("pages_with_text exceeds sampled page count")
    for chapter, _ in chapters:
        if chapter.page > total:
            errors.append(f"chapter {chapter.id!r} page {chapter.page} exceeds {total}")
    for element, _ in elements:
        if element.page > total:
            errors.append(f"element {element.id!r} page {element.page} exceeds {total}")
        if element.page not in processed:
            errors.append(f"element {element.id!r} page {element.page} is not in pages_processed")
    return errors


def _content_page_coverage_errors(
    manual: ManualDocument | None,
    elements: list[tuple[ManualElement, Chapter | None]],
) -> list[str]:
    if manual is None:
        return ["manual contract is required"]
    processed = set(manual.metadata.pages_processed)
    if not processed:
        return ["pages_processed cannot be empty for content coverage"]
    element_pages = {element.page for element, _ in elements if element.page in processed}
    required_pages = math.ceil(len(processed) * MIN_CONTENT_PAGE_COVERAGE)
    if len(element_pages) < required_pages:
        missing = sorted(processed - element_pages)
        return [
            "canonical elements cover only "
            f"{len(element_pages)}/{len(processed)} processed pages; at least "
            f"{required_pages}/{len(processed)} ({MIN_CONTENT_PAGE_COVERAGE:.0%}) are "
            f"required; pages without elements: {missing}"
        ]
    return []


def _duplicate_ids(
    chapters: list[tuple[Chapter, Chapter | None]],
    elements: list[tuple[ManualElement, Chapter | None]],
) -> list[str]:
    counts: dict[str, int] = {}
    for node, _ in [*chapters, *elements]:
        counts[node.id] = counts.get(node.id, 0) + 1
    return sorted(node_id for node_id, count in counts.items() if count > 1)


def _parent_errors(
    chapters: list[tuple[Chapter, Chapter | None]],
    elements: list[tuple[ManualElement, Chapter | None]],
) -> list[str]:
    errors: list[str] = []
    for chapter, parent in chapters:
        if parent and chapter.level <= parent.level:
            errors.append(f"chapter {chapter.id!r} level must be deeper than parent {parent.id!r}")
    for element, parent in elements:
        declared = element.trace.parent_chapter_id
        expected = parent.id if parent else None
        if declared != expected:
            errors.append(f"element {element.id!r} parent trace {declared!r} != structural parent {expected!r}")
    return errors


def _asset_references(
    elements: list[tuple[ManualElement, Chapter | None]],
) -> list[tuple[str, str, str]]:
    references: list[tuple[str, str, str]] = []
    for element, _ in elements:
        if element.image_path:
            references.append((element.id, "image_path", element.image_path))
        if element.table_image_path:
            references.append((element.id, "table_image_path", element.table_image_path))
    return references


def _caption_errors(elements: list[tuple[ManualElement, Chapter | None]]) -> list[str]:
    errors: list[str] = []
    for element, _ in elements:
        caption = element.caption_generated
        provenance = element.caption_provenance
        is_visual = element.type in {ElementType.IMAGE, ElementType.TABLE}
        if caption is not None and not caption.strip():
            errors.append(f"element {element.id!r} has an empty generated caption")
        if caption and not is_visual:
            errors.append(f"non-visual element {element.id!r} cannot have a generated caption")
        if caption and caption.strip() and provenance is None:
            errors.append(f"element {element.id!r} has a generated caption without provenance")
        if provenance is not None and (caption is None or not caption.strip()):
            errors.append(f"element {element.id!r} has provenance without a generated caption")
        if element.type is ElementType.IMAGE and element.trace.include_in_rag and not caption:
            errors.append(f"retrievable image element {element.id!r} has no generated caption")
        if caption:
            try:
                canonical_caption = validate_caption_text(
                    caption,
                    max_characters=MAX_CAPTION_CHARACTERS,
                    prompt_version=(provenance.prompt_version
                                    if provenance else CURRENT_PROMPT_VERSION),
                )
                review_reason = caption_review_reason(
                    canonical_caption,
                    prompt_version=(provenance.prompt_version
                                    if provenance else CURRENT_PROMPT_VERSION),
                )
            except ValueError as exc:
                errors.append(f"element {element.id!r} has an invalid generated caption: {exc}")
                continue
            if caption.strip() != canonical_caption:
                errors.append(
                    f"element {element.id!r} generated caption is not in canonical form"
                )
            if review_reason is not None and (
                element.trace.include_in_rag
                or element.trace.exclusion_reason != review_reason
            ):
                errors.append(
                    f"element {element.id!r} requires visual review and must be excluded "
                    "from RAG with the canonical reason"
                )
    return errors


def _caption_input_errors(
    manual: ManualDocument | None,
    elements: list[tuple[ManualElement, Chapter | None]],
    root: Path,
) -> list[str]:
    if manual is None:
        return ["manual contract is required"]
    errors: list[str] = []
    for element, _ in elements:
        provenance = element.caption_provenance
        if provenance is None:
            continue
        try:
            expected = caption_input_sha256(
                manual,
                element_id=element.id,
                asset_root=root,
                config=EnrichmentConfig(prompt_version=provenance.prompt_version),
            )
        except (OSError, ValueError) as exc:
            errors.append(
                f"cannot recompute caption input for {element.id!r}: {exc}"
            )
            continue
        if provenance.input_sha256 != expected:
            errors.append(
                f"caption input digest for {element.id!r} differs from prompt and asset"
            )
    return errors


def _toc_page_errors(
    manifest: RunManifest | None,
    manual: ManualDocument | None,
    toc: list[TocEntry] | None,
) -> list[str]:
    if manifest is None or manual is None or toc is None:
        return ["manifest, manual, and toc contracts are required"]
    total = manifest.source.pages_total
    return [f"TOC entry {entry.title!r} page {entry.page} exceeds {total}" for entry in toc if entry.page > total]


def _toc_hierarchy_errors(
    manual: ManualDocument | None,
    toc: list[TocEntry] | None,
    chapters: list[tuple[Chapter, Chapter | None]],
) -> list[str]:
    if manual is None or toc is None:
        return ["manual and toc contracts are required"]
    if not manual.metadata.toc_available or not toc:
        return ["public ingestion profiles require a non-empty canonical TOC"]
    canonical = [
        (chapter.level, chapter.title, chapter.page)
        for chapter, _ in chapters
    ]
    declared = [(entry.level, entry.title, entry.page) for entry in toc]
    if canonical != declared:
        return ["toc.json entries differ from chapter preorder in manual.json"]
    return []
