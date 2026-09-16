"""Typed diagnostics persisted beside canonical V2 run artifacts.

The contracts live outside the orchestrator so both ingestion and read-only
validation can use them without introducing an import cycle.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .models import (
    SCHEMA_VERSION,
    DetectedCapabilities,
    ElementType,
    StrictModel,
)

AUTOMATIC_ROUTING_STRATEGY = "pdf_capability_detection_v1"
PROMOTION_POLICY_VERSION = "1.0"


class DetectionDiagnostic(StrictModel):
    schema_version: Literal["1.1"] = SCHEMA_VERSION
    kind: Literal["detection"] = "detection"
    strategy: Literal["pdf_capability_detection_v1"] = AUTOMATIC_ROUTING_STRATEGY
    capabilities: DetectedCapabilities


class CropDecisionDiagnostic(StrictModel):
    element_id: str
    element_type: ElementType
    status: Literal["excluded", "kept", "preserved", "unassessed"]
    area_fraction: float | None = Field(default=None, ge=0, le=1)
    reason: str


class CropFilterDiagnostic(StrictModel):
    schema_version: Literal["1.1"] = SCHEMA_VERSION
    kind: Literal["crop_filter"] = "crop_filter"
    strategy: str
    threshold: float = Field(gt=0, lt=1)
    excluded: int = Field(ge=0)
    kept: int = Field(ge=0)
    preserved: int = Field(ge=0)
    unassessed: int = Field(ge=0)
    decisions: list[CropDecisionDiagnostic]

    @model_validator(mode="after")
    def validate_counts(self) -> CropFilterDiagnostic:
        counts = {
            status: sum(item.status == status for item in self.decisions)
            for status in ("excluded", "kept", "preserved", "unassessed")
        }
        declared = {
            "excluded": self.excluded,
            "kept": self.kept,
            "preserved": self.preserved,
            "unassessed": self.unassessed,
        }
        if counts != declared:
            raise ValueError("crop-filter counters must match decision statuses")
        element_ids = [item.element_id for item in self.decisions]
        if len(element_ids) != len(set(element_ids)):
            raise ValueError("crop-filter decision element IDs must be unique")
        return self


class EnrichmentItemDiagnostic(StrictModel):
    element_id: str
    status: Literal["enriched", "skipped", "failed", "review_required"]
    reason: str


class EnrichmentDiagnostic(StrictModel):
    schema_version: Literal["1.1"] = SCHEMA_VERSION
    kind: Literal["enrichment"] = "enrichment"
    provider_configured: bool
    provider_preflight: dict[str, Any] = Field(default_factory=dict)
    enriched: int = Field(ge=0)
    skipped: int = Field(ge=0)
    failed: int = Field(ge=0)
    review_required: int = Field(ge=0)
    items: list[EnrichmentItemDiagnostic]

    @model_validator(mode="after")
    def validate_counts(self) -> EnrichmentDiagnostic:
        counts = {
            status: sum(item.status == status for item in self.items)
            for status in ("enriched", "skipped", "failed", "review_required")
        }
        declared = {
            "enriched": self.enriched,
            "skipped": self.skipped,
            "failed": self.failed,
            "review_required": self.review_required,
        }
        if counts != declared:
            raise ValueError(
                "enrichment counters must match diagnostic item statuses"
            )
        element_ids = [item.element_id for item in self.items]
        if len(element_ids) != len(set(element_ids)):
            raise ValueError("enrichment diagnostic element IDs must be unique")
        if not self.provider_configured and self.provider_preflight:
            raise ValueError(
                "provider_preflight requires provider_configured=true"
            )
        return self


class StructureDiagnostic(StrictModel):
    schema_version: Literal["1.1"] = SCHEMA_VERSION
    kind: Literal["structure"] = "structure"
    strategy: str
    toc_available: bool
    details_available: bool
    details: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_details_availability(self) -> StructureDiagnostic:
        if self.details_available != (self.details is not None):
            raise ValueError(
                "details_available must match whether structure details are present"
            )
        if self.details is not None:
            detail_strategy = self.details.get("strategy")
            if detail_strategy is not None and detail_strategy != self.strategy:
                raise ValueError(
                    "structure detail strategy must match the diagnostic strategy"
                )
        return self


class RuntimeDiagnostic(StrictModel):
    schema_version: Literal["1.1"] = SCHEMA_VERSION
    kind: Literal["runtime"] = "runtime"
    profile_runtime: dict[str, Any] | None = None
    provider_runtime: dict[str, Any] | None = None
    paddle_runtime_config: dict[str, Any] | None = None


PROMOTION_GATE_IDS = (
    "document.full",
    "enrichment.configured",
    "captions.identity",
    "enrichment.review_free",
    "warnings.nonconsequential",
    "profile.accepted",
    "structure.accepted",
)


class PromotionGateDiagnostic(StrictModel):
    id: Literal[
        "document.full",
        "enrichment.configured",
        "captions.identity",
        "enrichment.review_free",
        "warnings.nonconsequential",
        "profile.accepted",
        "structure.accepted",
    ]
    passed: bool
    message: str


class PromotionDiagnostic(StrictModel):
    schema_version: Literal["1.1"] = SCHEMA_VERSION
    kind: Literal["promotion"] = "promotion"
    policy_version: Literal["1.0"] = PROMOTION_POLICY_VERSION
    eligible_for_validated: bool
    acceptance_evidence_id: str | None = None
    profile_warnings: list[str] = Field(default_factory=list)
    gates: list[PromotionGateDiagnostic]

    @model_validator(mode="after")
    def validate_gate_set(self) -> PromotionDiagnostic:
        gate_ids = tuple(gate.id for gate in self.gates)
        if gate_ids != PROMOTION_GATE_IDS:
            raise ValueError(
                "promotion gates must contain the canonical ordered gate set"
            )
        expected = all(gate.passed for gate in self.gates)
        if self.eligible_for_validated != expected:
            raise ValueError(
                "eligible_for_validated must match the promotion gate results"
            )
        if len(self.profile_warnings) != len(set(self.profile_warnings)):
            raise ValueError("profile_warnings must be unique")
        if self.acceptance_evidence_id is not None and (
            not self.acceptance_evidence_id.strip()
            or self.acceptance_evidence_id != self.acceptance_evidence_id.strip()
        ):
            raise ValueError("acceptance_evidence_id must be a canonical non-empty string")
        return self


class FailureReport(StrictModel):
    """Compact, standalone record for an unpublished ingestion failure.

    A failure report is deliberately not a bundle diagnostic or artifact.  It
    contains enough context for an operator to identify the failed invocation,
    without retaining a traceback, parser output, assets, environment details,
    or subprocess logs.
    """

    schema_version: Literal["1.1"] = SCHEMA_VERSION
    kind: Literal["ingestion_failure"] = "ingestion_failure"
    run_id: str
    source_file: str
    destination: str
    started_at: datetime
    failed_at: datetime
    stage: str | None = None
    error_type: str
    error_message: str = Field(max_length=4000)
    current: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, ge=0)
    unit: str | None = None
    bundle_published: Literal[False] = False

    @field_validator("run_id", "destination", "error_type")
    @classmethod
    def validate_required_strings(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("failure-report identity strings must be non-empty")
        return value

    @field_validator("source_file")
    @classmethod
    def validate_source_basename(cls, value: str) -> str:
        if (
            not value
            or not value.strip()
            or value in {".", ".."}
            or PurePosixPath(value).name != value
            or PureWindowsPath(value).name != value
        ):
            raise ValueError("source_file must be a non-empty basename")
        return value

    @field_validator("stage", "unit")
    @classmethod
    def validate_optional_strings(cls, value: str | None) -> str | None:
        if value is not None and (not value or not value.strip()):
            raise ValueError("optional failure-report strings must be non-empty")
        return value

    @field_validator("started_at", "failed_at")
    @classmethod
    def validate_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("failure-report timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_progress_and_time(self) -> FailureReport:
        if self.failed_at < self.started_at:
            raise ValueError("failed_at cannot precede started_at")
        if (
            self.current is not None
            and self.total is not None
            and self.current > self.total
        ):
            raise ValueError("failure-report current cannot exceed total")
        return self
