"""Canonical V2 contracts shared by every ingestion profile."""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.1"


def _normalize_sha256(value: str) -> str:
    lowered = value.lower()
    if len(lowered) != 64 or any(char not in "0123456789abcdef" for char in lowered):
        raise ValueError("sha256 must be a 64-character hexadecimal digest")
    return lowered


def _normalize_run_id(value: str) -> str:
    """Keep run identifiers safe to use as one filesystem path component."""

    if not value or value != value.strip():
        raise ValueError("run_id must be non-empty and cannot have surrounding whitespace")
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("run_id must be a safe single path component")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("run_id cannot contain control characters")
    return value


def _normalize_artifact_path(value: str) -> str:
    """Return a canonical portable relative artifact path."""

    if (
        not value
        or not value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("artifact paths must be non-empty relative paths")
    windows_path = PureWindowsPath(value)
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if windows_path.is_absolute() or windows_path.drive or path.is_absolute() or ".." in path.parts:
        raise ValueError("artifact paths must be relative and cannot traverse parents")
    if not path.parts:
        raise ValueError("artifact paths must identify a file or directory")
    return path.as_posix()


def _paths_overlap(first: str, second: str) -> bool:
    first_parts = PurePosixPath(first).parts
    second_parts = PurePosixPath(second).parts
    common = min(len(first_parts), len(second_parts))
    return first_parts[:common] == second_parts[:common]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentProfile(StrEnum):
    DIGITAL_OUTLINE = "digital_outline"
    DIGITAL_RECONSTRUCTED = "digital_reconstructed"
    SCANNED_OCR = "scanned_ocr"


class RunStatus(StrEnum):
    INCOMPLETE = "incomplete"
    COMPLETED = "completed"
    VALIDATED = "validated"
    EXPERIMENTAL = "experimental"
    FAILED = "failed"


FINISHED_RUN_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.VALIDATED, RunStatus.EXPERIMENTAL}
)


class ElementType(StrEnum):
    TITLE = "title"
    TEXT = "text"
    IMAGE = "image"
    TABLE = "table"


class TableSerializationStatus(StrEnum):
    STRUCTURED = "structured"
    FALLBACK = "fallback"
    UNAVAILABLE = "unavailable"


class PageSize(StrictModel):
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class BoundingBox(StrictModel):
    x0: float
    y0: float
    x1: float
    y1: float

    @model_validator(mode="after")
    def validate_order(self) -> BoundingBox:
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError("bbox must have positive width and height")
        return self


class SourceReference(StrictModel):
    page: int = Field(ge=1)
    page_size: PageSize | None = None
    bbox: BoundingBox | None = None
    rotation: float = 0.0

    @model_validator(mode="after")
    def validate_bbox_bounds(self) -> SourceReference:
        if self.bbox is None or self.page_size is None:
            return self
        if (
            self.bbox.x0 < 0
            or self.bbox.y0 < 0
            or self.bbox.x1 > self.page_size.width
            or self.bbox.y1 > self.page_size.height
        ):
            raise ValueError("bbox must fit inside page_size")
        return self


class TraceMetadata(StrictModel):
    reading_order: int | None = Field(default=None, ge=0)
    parent_chapter_id: str | None = None
    role: str | None = None
    heading_level: int | None = Field(default=None, ge=1)
    include_in_rag: bool = True
    exclusion_reason: str | None = None
    continued: bool = False
    continuation_group: str | None = None

    @model_validator(mode="after")
    def validate_exclusion_reason(self) -> TraceMetadata:
        if self.include_in_rag and self.exclusion_reason:
            raise ValueError("exclusion_reason requires include_in_rag=false")
        if not self.include_in_rag and not self.exclusion_reason:
            raise ValueError("include_in_rag=false requires an exclusion_reason")
        if self.continued and not self.continuation_group:
            raise ValueError("continued=true requires continuation_group")
        return self


class CaptionProvenance(StrictModel):
    provider: str
    model: str
    prompt_version: str
    params: dict[str, Any] = Field(default_factory=dict)
    input_sha256: str
    runtime_version: str | None = None

    @field_validator("provider", "model", "prompt_version")
    @classmethod
    def validate_identity_field(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError(
                "caption provenance identity fields must be canonical non-empty strings"
            )
        return value

    @field_validator("input_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _normalize_sha256(value)


class TableSerializedRow(StrictModel):
    id: str
    row_index: int = Field(ge=1)
    serialized_text: str

    @field_validator("id", "serialized_text")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("serialized table row fields must be canonical non-empty strings")
        return value


class TableSerialization(StrictModel):
    strategy: Literal["header_value_rows_v1"] = "header_value_rows_v1"
    status: TableSerializationStatus
    rows: list[TableSerializedRow] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status_and_rows(self) -> TableSerialization:
        expected_indexes = list(range(1, len(self.rows) + 1))
        if [row.row_index for row in self.rows] != expected_indexes:
            raise ValueError("serialized table row indexes must be consecutive and one-based")
        if len({row.id for row in self.rows}) != len(self.rows):
            raise ValueError("serialized table row ids must be unique")
        if self.status is TableSerializationStatus.STRUCTURED and not self.rows:
            raise ValueError("structured table serialization requires rows")
        if self.status is TableSerializationStatus.FALLBACK and len(self.rows) != 1:
            raise ValueError("fallback table serialization requires exactly one row")
        if self.status is TableSerializationStatus.UNAVAILABLE and self.rows:
            raise ValueError("unavailable table serialization cannot contain rows")
        return self


class ManualElement(StrictModel):
    id: str
    type: ElementType
    page: int = Field(ge=1)
    source: SourceReference
    text: str | None = None
    image_path: str | None = None
    table_image_path: str | None = None
    table_markdown: str | None = None
    caption_original: str | None = None
    caption_generated: str | None = None
    caption_provenance: CaptionProvenance | None = None
    table_serialization: TableSerialization | None = None
    trace: TraceMetadata = Field(default_factory=TraceMetadata)

    @model_validator(mode="after")
    def validate_element(self) -> ManualElement:
        if self.page != self.source.page:
            raise ValueError("element page must match source.page")
        if self.caption_provenance and not self.caption_generated:
            raise ValueError("caption_provenance requires caption_generated")
        if self.caption_generated and self.type not in {ElementType.IMAGE, ElementType.TABLE}:
            raise ValueError("caption_generated is only valid for image and table elements")
        if self.table_serialization is not None and self.type is not ElementType.TABLE:
            raise ValueError("table_serialization is only valid for table elements")
        if self.type in {ElementType.TITLE, ElementType.TEXT} and not self.text:
            raise ValueError("title and text elements require text")
        if self.type is ElementType.IMAGE:
            if not self.image_path:
                raise ValueError("image elements require image_path")
            if self.table_image_path or self.table_markdown or self.table_serialization:
                raise ValueError("image elements cannot contain table artifacts")
        if self.type is ElementType.TABLE:
            if not self.table_image_path and not self.table_markdown:
                raise ValueError("table elements require table_image_path or table_markdown")
            if self.image_path:
                raise ValueError("table elements cannot contain image_path")
        return self


class Chapter(StrictModel):
    id: str
    type: Literal["chapter"] = "chapter"
    title: str
    level: int = Field(ge=1)
    page: int = Field(ge=1)
    content: list[Chapter | ManualElement] = Field(default_factory=list)


Chapter.model_rebuild()


class ManualMetadata(StrictModel):
    pages_total: int = Field(ge=1)
    pages_processed: list[int] = Field(min_length=1)
    profile: DocumentProfile
    parser: str
    structure_strategy: str
    toc_available: bool

    @field_validator("pages_processed")
    @classmethod
    def validate_pages(cls, pages: list[int]) -> list[int]:
        if any(page < 1 for page in pages):
            raise ValueError("processed pages must be positive")
        if len(pages) != len(set(pages)):
            raise ValueError("processed pages must be unique")
        return sorted(pages)

    @model_validator(mode="after")
    def validate_page_bounds(self) -> ManualMetadata:
        if self.pages_processed[-1] > self.pages_total:
            raise ValueError("processed pages cannot exceed pages_total")
        return self


class ManualDocument(StrictModel):
    schema_version: Literal["1.1"] = SCHEMA_VERSION
    type: Literal["manual"] = "manual"
    id: str
    title: str
    source_file: str
    language: str = "en"
    metadata: ManualMetadata
    content: list[Chapter | ManualElement] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _normalize_run_id(value)


class TocEntry(StrictModel):
    level: int = Field(ge=1)
    title: str
    page: int = Field(ge=1)
    confidence: float | None = Field(default=None, ge=0, le=1)


class DetectedCapabilities(StrictModel):
    profile: DocumentProfile
    embedded_outline: bool
    outline_entries: int = Field(ge=0)
    text_layer: bool
    sampled_pages: list[int] = Field(min_length=1)
    pages_with_text: int = Field(ge=0)
    median_text_characters: float = Field(ge=0)
    profile_confidence: float = Field(ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)

    @field_validator("sampled_pages")
    @classmethod
    def validate_sampled_pages(cls, pages: list[int]) -> list[int]:
        if any(page < 1 for page in pages):
            raise ValueError("sampled pages must be positive")
        if len(pages) != len(set(pages)):
            raise ValueError("sampled pages must be unique")
        return sorted(pages)

    @model_validator(mode="after")
    def validate_detection_evidence(self) -> DetectedCapabilities:
        if self.pages_with_text > len(self.sampled_pages):
            raise ValueError("pages_with_text cannot exceed sampled page count")
        if self.embedded_outline != (self.outline_entries > 0):
            raise ValueError("embedded_outline must match outline_entries")
        if self.profile is DocumentProfile.DIGITAL_OUTLINE and not (
            self.embedded_outline and self.text_layer
        ):
            raise ValueError("digital_outline requires an outline and usable text")
        if self.profile is DocumentProfile.DIGITAL_RECONSTRUCTED and (
            self.embedded_outline or not self.text_layer
        ):
            raise ValueError("digital_reconstructed requires text and no embedded outline")
        if self.profile is DocumentProfile.SCANNED_OCR and self.text_layer:
            raise ValueError("scanned_ocr requires no usable text layer")
        return self


class SourceDocument(StrictModel):
    file: str
    sha256: str
    size_bytes: int = Field(ge=0)
    pages_total: int = Field(ge=1)
    detected: DetectedCapabilities

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _normalize_sha256(value)

    @model_validator(mode="after")
    def validate_detection_pages(self) -> SourceDocument:
        if self.detected.sampled_pages[-1] > self.pages_total:
            raise ValueError("detected sampled pages cannot exceed source pages_total")
        return self


class PipelineSelection(StrictModel):
    profile: DocumentProfile
    parser: str
    structure_strategy: str
    enrichment_provider: str | None = None
    enrichment_model: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("enrichment_provider", "enrichment_model")
    @classmethod
    def validate_enrichment_identity_field(cls, value: str | None) -> str | None:
        if value is not None and (not value or value != value.strip()):
            raise ValueError(
                "pipeline enrichment identity fields must be canonical non-empty strings"
            )
        return value

    @model_validator(mode="after")
    def validate_enrichment_identity_pair(self) -> PipelineSelection:
        if (self.enrichment_provider is None) != (self.enrichment_model is None):
            raise ValueError(
                "pipeline enrichment provider and model must be declared together"
            )
        return self


class ArtifactPaths(StrictModel):
    manual: str = "manual.json"
    toc: str = "toc.json"
    validation: str | None = None
    assets: str = "assets"
    diagnostics: str = "diagnostics"

    @field_validator("manual", "toc", "validation", "assets", "diagnostics")
    @classmethod
    def validate_relative_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_artifact_path(value)

    @model_validator(mode="after")
    def validate_path_collisions(self) -> ArtifactPaths:
        configured = {
            "manual": self.manual,
            "toc": self.toc,
            "validation": self.validation,
            "assets": self.assets,
            "diagnostics": self.diagnostics,
            "run manifest": "run.json",
        }
        entries = [(name, path) for name, path in configured.items() if path is not None]
        for index, (first_name, first_path) in enumerate(entries):
            for second_name, second_path in entries[index + 1 :]:
                if _paths_overlap(first_path, second_path):
                    raise ValueError(
                        f"artifact paths collide: {first_name}={first_path!r} and "
                        f"{second_name}={second_path!r}"
                    )
        return self


class RunManifest(StrictModel):
    schema_version: Literal["1.1"] = SCHEMA_VERSION
    run_id: str
    status: RunStatus
    source: SourceDocument
    pipeline: PipelineSelection
    artifacts: ArtifactPaths = Field(default_factory=ArtifactPaths)
    started_at: str
    completed_at: str | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        return _normalize_run_id(value)

    @model_validator(mode="after")
    def validate_status(self) -> RunManifest:
        if self.status in {RunStatus.COMPLETED, RunStatus.VALIDATED, RunStatus.EXPERIMENTAL} and not self.completed_at:
            raise ValueError("a finished run requires completed_at")
        if self.status is RunStatus.FAILED and not self.errors:
            raise ValueError("a failed run requires at least one error")
        if self.status in FINISHED_RUN_STATUSES and self.errors:
            raise ValueError("a successful finished run cannot declare errors")
        if (
            self.status in {RunStatus.VALIDATED, RunStatus.EXPERIMENTAL}
            and self.artifacts.validation is None
        ):
            raise ValueError(
                f"a {self.status.value} run requires artifacts.validation"
            )
        if self.status is RunStatus.EXPERIMENTAL and not self.warnings:
            raise ValueError("an experimental run requires at least one warning")
        if self.pipeline.profile != self.source.detected.profile:
            raise ValueError("pipeline profile must match detected profile")
        return self


class ValidationCheck(StrictModel):
    id: str
    passed: bool
    message: str


class ValidationReport(StrictModel):
    schema_version: Literal["1.1"] = SCHEMA_VERSION
    run_id: str
    status: Literal["passed", "failed"]
    checks: list[ValidationCheck] = Field(min_length=1)
    metrics: dict[str, float | int | str | None] = Field(default_factory=dict)

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        return _normalize_run_id(value)

    @model_validator(mode="after")
    def validate_report_status(self) -> ValidationReport:
        expected = "passed" if all(check.passed for check in self.checks) else "failed"
        if self.status != expected:
            raise ValueError("validation status must match check results")
        return self
