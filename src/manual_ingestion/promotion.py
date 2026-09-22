"""Shared, artifact-based policy for promoting a run to ``validated``.

This module deliberately has no dependency on the orchestrator or validator.
Both callers construct the same assessment from canonical contracts and typed
diagnostics, preventing status policy from drifting between write and read
paths.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from .diagnostics import (
    EnrichmentDiagnostic,
    PromotionDiagnostic,
    PromotionGateDiagnostic,
    StructureDiagnostic,
)
from .enrichment import (
    CURRENT_PROMPT_VERSION,
    SUPPORTED_PROMPT_VERSIONS,
    EnrichmentConfig,
    caption_review_reason,
)
from .models import (
    Chapter,
    DocumentProfile,
    ElementType,
    ManualDocument,
    ManualElement,
    RunManifest,
)
from .profiles.scanned_ocr import SCANNED_EMBEDDED_OUTLINE_STRATEGY
from .providers.ollama import (
    ACCEPTED_OLLAMA_MODEL,
    ACCEPTED_OLLAMA_MODEL_DIGEST,
    ACCEPTED_OLLAMA_OUTPUT_PARAMS,
    OLLAMA_PROVIDER_NAME,
    OLLAMA_RUNTIME_BY_PROMPT_VERSION,
    ollama_runtime_accepted,
)
from .structure.ocr_reconstruct import (
    OCR_FALLBACK_STRATEGY,
    OCR_STRUCTURE_STRATEGY,
)


DIAGNOSTIC_NO_PROVIDER_REASON = (
    "Experimental diagnostic run: no caption provider was configured."
)
DIAGNOSTIC_VISUAL_EXCLUSION_REASON = (
    "caption provider unavailable in diagnostic run"
)

# No private document study is a general public acceptance guarantee.
CURRENT_SCANNED_OCR_ACCEPTANCE_EVIDENCE_ID: str | None = None
SCANNED_OCR_ACCEPTANCE_EVIDENCE_IDS: frozenset[str] = frozenset()
SCANNED_OCR_ACCEPTANCE_EVIDENCE_ID = CURRENT_SCANNED_OCR_ACCEPTANCE_EVIDENCE_ID

_CONSEQUENTIAL_WARNING_MARKERS = (
    "ambiguous",
    "failed",
    "fallback",
    "incomplete",
    "missing",
    "out of order",
    "partial",
    "restructure",
    "timed out",
    "unmatched",
    "unavailable",
    "unresolved",
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


def build_promotion_assessment(
    *,
    manifest: RunManifest,
    manual: ManualDocument,
    enrichment: EnrichmentDiagnostic | None,
    structure: StructureDiagnostic | None,
    profile_warnings: Sequence[str],
    scanned_acceptance_evidence_id: str | None = SCANNED_OCR_ACCEPTANCE_EVIDENCE_ID,
) -> PromotionDiagnostic:
    """Return the canonical ordered promotion assessment for one run.

    ``manifest.status`` is intentionally ignored. The result describes the
    evidence; callers separately decide whether a claimed status overstates it.
    """

    warnings = list(dict.fromkeys(profile_warnings))
    elements = list(_walk_elements(manual.content))
    gates = [
        _full_document_gate(manifest, manual),
        _enrichment_gate(manifest, elements, enrichment),
        _caption_identity_gate(manifest, elements),
        _review_gate(elements, enrichment),
        _warnings_gate(warnings),
        _profile_acceptance_gate(
            manifest,
            scanned_acceptance_evidence_id=scanned_acceptance_evidence_id,
        ),
        _structure_gate(manifest, structure),
    ]
    evidence_id = (
        scanned_acceptance_evidence_id
        if manifest.pipeline.profile is DocumentProfile.SCANNED_OCR
        else None
    )
    return PromotionDiagnostic(
        eligible_for_validated=all(gate.passed for gate in gates),
        acceptance_evidence_id=evidence_id,
        profile_warnings=warnings,
        gates=gates,
    )


def promotion_warnings(assessment: PromotionDiagnostic) -> list[str]:
    """Return stable user-facing reasons for an experimental status."""

    return [gate.message for gate in assessment.gates if not gate.passed]


def failed_promotion_gate_ids(assessment: PromotionDiagnostic) -> list[str]:
    return [gate.id for gate in assessment.gates if not gate.passed]


def warning_requires_experimental(warning: str) -> bool:
    normalized = warning.casefold()
    return any(marker in normalized for marker in _CONSEQUENTIAL_WARNING_MARKERS)


def caption_identity_errors(
    manifest: RunManifest | None,
    elements: Iterable[ManualElement],
) -> list[str]:
    """Describe provider/model disagreement across manual and pipeline."""

    if manifest is None:
        return ["manifest contract is required"]
    element_list = list(elements)
    pipeline_identity = (
        manifest.pipeline.enrichment_provider,
        manifest.pipeline.enrichment_model,
    )
    identities = sorted(
        {
            (element.caption_provenance.provider, element.caption_provenance.model)
            for element in element_list
            if element.caption_provenance is not None
        }
    )
    errors: list[str] = []
    if len(identities) > 1:
        labels = ", ".join(f"{provider}/{model}" for provider, model in identities)
        errors.append(
            f"caption provenance contains multiple provider/model identities: {labels}"
        )
    elif identities and identities[0] != pipeline_identity:
        actual = f"{identities[0][0]}/{identities[0][1]}"
        expected = (
            f"{pipeline_identity[0]}/{pipeline_identity[1]}"
            if all(pipeline_identity)
            else "none"
        )
        errors.append(
            f"caption provenance identity {actual!r} differs from pipeline identity "
            f"{expected!r}"
        )

    expected_max_characters = EnrichmentConfig().max_characters
    prompt_versions = sorted(
        {
            element.caption_provenance.prompt_version
            for element in element_list
            if element.caption_provenance is not None
        }
    )
    raw_enrichment = manifest.pipeline.config.get("enrichment")
    configured_prompt = (
        raw_enrichment.get("prompt_version")
        if isinstance(raw_enrichment, dict)
        else None
    )
    enrichment_enabled = bool(
        isinstance(raw_enrichment, dict)
        and raw_enrichment.get("enabled") is True
    )
    if (enrichment_enabled or prompt_versions) and (
        not isinstance(configured_prompt, str)
        or configured_prompt not in SUPPORTED_PROMPT_VERSIONS
    ):
        errors.append(
            "pipeline enrichment prompt_version must identify a supported frozen setup; "
            f"found {configured_prompt!r}"
        )
    unexpected_prompts = [
        version for version in prompt_versions if version != configured_prompt
    ]
    if unexpected_prompts:
        errors.append(
            "caption provenance prompt_version must match pipeline enrichment "
            f"{configured_prompt!r}; found {', '.join(repr(value) for value in unexpected_prompts)}"
        )
    configured_max_characters = (
        raw_enrichment.get("max_caption_characters")
        if isinstance(raw_enrichment, dict)
        else None
    )
    if (
        (enrichment_enabled or prompt_versions)
        and configured_max_characters != expected_max_characters
    ):
        errors.append(
            "pipeline enrichment max_caption_characters must be "
            f"{expected_max_characters}; found {configured_max_characters!r}"
        )
    preflight = (
        raw_enrichment.get("preflight")
        if isinstance(raw_enrichment, dict)
        else None
    )
    identity_required = enrichment_enabled or bool(identities)
    if identity_required and not isinstance(preflight, dict):
        errors.append("pipeline enrichment preflight must be an object")
        preflight = {}
    preflight_model = preflight.get("model") if isinstance(preflight, dict) else None
    if identity_required and preflight_model != manifest.pipeline.enrichment_model:
        errors.append(
            "pipeline enrichment preflight model must match enrichment_model; "
            f"found {preflight_model!r}"
        )
    expected_runtime = preflight.get("version") if isinstance(preflight, dict) else None
    if identity_required and not (
        isinstance(expected_runtime, str)
        and expected_runtime
        and expected_runtime.strip() == expected_runtime
    ):
        errors.append(
            "pipeline enrichment preflight version must be a canonical non-empty string"
        )
    if (
        identity_required
        and manifest.pipeline.enrichment_provider == OLLAMA_PROVIDER_NAME
        and (
            not isinstance((digest := preflight.get("model_digest")), str)
            or _SHA256.fullmatch(digest) is None
        )
    ):
        errors.append(
            "Ollama enrichment preflight model_digest must be a lowercase full SHA-256"
        )
    if identity_required and manifest.pipeline.enrichment_provider == OLLAMA_PROVIDER_NAME:
        if preflight_model != ACCEPTED_OLLAMA_MODEL:
            errors.append("Ollama enrichment model differs from the accepted setup")
        accepted_runtime = (
            OLLAMA_RUNTIME_BY_PROMPT_VERSION.get(configured_prompt)
            if isinstance(configured_prompt, str)
            else None
        )
        if accepted_runtime is None:
            errors.append("Caption prompt version differs from the accepted setup")
        elif not ollama_runtime_accepted(expected_runtime):
            errors.append(f"Ollama runtime version {expected_runtime!r} is not a release version")
        if (
            isinstance(preflight, dict)
            and preflight.get("model_digest") != ACCEPTED_OLLAMA_MODEL_DIGEST
        ):
            errors.append("Ollama model digest differs from the accepted setup")
        for element in element_list:
            provenance = element.caption_provenance
            if provenance is not None and provenance.params != ACCEPTED_OLLAMA_OUTPUT_PARAMS:
                errors.append(
                    f"caption provenance parameters for {element.id!r} differ "
                    "from the accepted Ollama setup"
                )
    if isinstance(expected_runtime, str) and expected_runtime:
        mismatched_runtime_ids = [
            element.id
            for element in element_list
            if element.caption_provenance is not None
            and element.caption_provenance.runtime_version != expected_runtime
        ]
        if mismatched_runtime_ids:
            errors.append(
                "caption provenance runtime_version must match enrichment preflight "
                f"version {expected_runtime!r} for "
                + ", ".join(mismatched_runtime_ids)
            )
    return errors


def _full_document_gate(
    manifest: RunManifest,
    manual: ManualDocument,
) -> PromotionGateDiagnostic:
    total = manifest.source.pages_total
    processed = manual.metadata.pages_processed
    full = processed == list(range(1, total + 1))
    message = (
        f"Processed all {total}/{total} source pages."
        if full
        else "Experimental partial run: "
        f"processed {len(processed)}/{total} source pages."
    )
    return PromotionGateDiagnostic(id="document.full", passed=full, message=message)


def _enrichment_gate(
    manifest: RunManifest,
    elements: list[ManualElement],
    enrichment: EnrichmentDiagnostic | None,
) -> PromotionGateDiagnostic:
    raw_enrichment = manifest.pipeline.config.get("enrichment")
    enabled = (
        isinstance(raw_enrichment, dict)
        and raw_enrichment.get("enabled") is True
    )
    identity_declared = bool(
        manifest.pipeline.enrichment_provider
        and manifest.pipeline.enrichment_model
    )
    diagnostic_configured = bool(
        enrichment is not None and enrichment.provider_configured
    )
    no_failed_items = bool(enrichment is not None and enrichment.failed == 0)
    diagnostic_exclusions = [
        element.id
        for element in elements
        if element.type is ElementType.IMAGE
        and element.trace.exclusion_reason == DIAGNOSTIC_VISUAL_EXCLUSION_REASON
    ]
    provider_config = (
        raw_enrichment.get("provider_config")
        if isinstance(raw_enrichment, dict)
        else None
    )
    accepted_config = bool(
        isinstance(provider_config, dict)
        and all(
            provider_config.get(key) == value
            for key, value in {
                "model": ACCEPTED_OLLAMA_MODEL,
                **ACCEPTED_OLLAMA_OUTPUT_PARAMS,
            }.items()
        )
    )
    accepted_preflight = bool(
        enrichment is not None
        and isinstance(raw_enrichment, dict)
        and isinstance(raw_enrichment.get("prompt_version"), str)
        and raw_enrichment["prompt_version"] in OLLAMA_RUNTIME_BY_PROMPT_VERSION
        and set(enrichment.provider_preflight) == {"version", "model", "model_digest"}
        and ollama_runtime_accepted(enrichment.provider_preflight["version"])
        and enrichment.provider_preflight["model"] == ACCEPTED_OLLAMA_MODEL
        and enrichment.provider_preflight["model_digest"] == ACCEPTED_OLLAMA_MODEL_DIGEST
    )
    accepted_identity = (
        manifest.pipeline.enrichment_provider == OLLAMA_PROVIDER_NAME
        and manifest.pipeline.enrichment_model == ACCEPTED_OLLAMA_MODEL
    )
    configured = (
        enabled
        and identity_declared
        and diagnostic_configured
        and no_failed_items
        and not diagnostic_exclusions
        and accepted_identity
        and accepted_config
        and accepted_preflight
    )
    if configured:
        message = (
            "Caption enrichment is configured as "
            f"{manifest.pipeline.enrichment_provider}/"
            f"{manifest.pipeline.enrichment_model}."
        )
    elif not (enabled and identity_declared and diagnostic_configured):
        message = DIAGNOSTIC_NO_PROVIDER_REASON
    elif not (accepted_identity and accepted_config and accepted_preflight):
        message = (
            "Experimental enrichment: provider, model, runtime, digest, or parameters "
            "differ from the accepted Ollama setup."
        )
    elif enrichment is not None and enrichment.failed:
        message = (
            "Experimental enrichment: "
            f"{enrichment.failed} visual element(s) failed captioning."
        )
    elif diagnostic_exclusions:
        message = (
            "Experimental diagnostic run: caption provider exclusions remain on "
            f"{len(diagnostic_exclusions)} visual element(s)."
        )
    else:
        message = "Experimental enrichment: caption setup is not promotable."
    return PromotionGateDiagnostic(
        id="enrichment.configured",
        passed=configured,
        message=message,
    )


def _caption_identity_gate(
    manifest: RunManifest,
    elements: list[ManualElement],
) -> PromotionGateDiagnostic:
    errors = caption_identity_errors(manifest, elements)
    return PromotionGateDiagnostic(
        id="captions.identity",
        passed=not errors,
        message=(
            "Generated-caption identity, prompt, and runtime match the pipeline."
            if not errors
            else "Experimental caption provenance: " + "; ".join(errors) + "."
        ),
    )


def _review_gate(
    elements: list[ManualElement],
    enrichment: EnrichmentDiagnostic | None,
) -> PromotionGateDiagnostic:
    review_ids: list[str] = []
    invalid_ids: list[str] = []
    for element in elements:
        if not element.caption_generated:
            continue
        try:
            if caption_review_reason(
                element.caption_generated,
                prompt_version=(element.caption_provenance.prompt_version
                                if element.caption_provenance else CURRENT_PROMPT_VERSION),
            ) is not None:
                review_ids.append(element.id)
        except ValueError:
            invalid_ids.append(element.id)
    diagnostic_count = enrichment.review_required if enrichment is not None else None
    count_matches = diagnostic_count == len(review_ids)
    passed = not review_ids and not invalid_ids and count_matches
    if invalid_ids:
        message = (
            "Experimental enrichment: invalid generated caption(s) cannot pass "
            "review for " + ", ".join(invalid_ids) + "."
        )
    elif review_ids:
        message = (
            "Experimental enrichment: "
            f"{len(review_ids)} visual element(s) require manual review "
            "and were excluded from RAG."
        )
    elif not count_matches:
        message = (
            "Experimental enrichment: diagnostic review count does not match "
            "canonical captions."
        )
    else:
        message = "No generated visual caption requires manual review."
    return PromotionGateDiagnostic(
        id="enrichment.review_free",
        passed=passed,
        message=message,
    )


def _warnings_gate(profile_warnings: list[str]) -> PromotionGateDiagnostic:
    consequential = [
        warning for warning in profile_warnings if warning_requires_experimental(warning)
    ]
    return PromotionGateDiagnostic(
        id="warnings.nonconsequential",
        passed=not consequential,
        message=(
            "No consequential parser or structure warnings were reported."
            if not consequential
            else "Experimental run: consequential parser or structure warnings "
            "were reported."
        ),
    )


def _profile_acceptance_gate(
    manifest: RunManifest,
    *,
    scanned_acceptance_evidence_id: str | None,
) -> PromotionGateDiagnostic:
    accepted_setup: dict[DocumentProfile, tuple[str, set[str]]] = {
        DocumentProfile.DIGITAL_OUTLINE: ("docling", {"embedded_outline"}),
        DocumentProfile.DIGITAL_RECONSTRUCTED: (
            "docling",
            {"printed_toc_body_heading_fusion_v1"},
        ),
        DocumentProfile.SCANNED_OCR: (
            "paddleocr_vl",
            {
                SCANNED_EMBEDDED_OUTLINE_STRATEGY,
                OCR_STRUCTURE_STRATEGY,
                OCR_FALLBACK_STRATEGY,
            },
        ),
    }
    expected_parser, expected_strategies = accepted_setup[manifest.pipeline.profile]
    setup_matches = (
        manifest.pipeline.parser == expected_parser
        and manifest.pipeline.structure_strategy in expected_strategies
    )
    if manifest.pipeline.profile is DocumentProfile.SCANNED_OCR:
        uses_embedded = (
            manifest.pipeline.structure_strategy
            == SCANNED_EMBEDDED_OUTLINE_STRATEGY
        )
        setup_matches = setup_matches and (
            manifest.source.detected.embedded_outline == uses_embedded
        )
    if not setup_matches:
        return PromotionGateDiagnostic(
            id="profile.accepted",
            passed=False,
            message=(
                "Experimental run: parser or structure strategy differs from the "
                f"accepted {manifest.pipeline.profile.value} setup."
            ),
        )
    if manifest.pipeline.profile is not DocumentProfile.SCANNED_OCR:
        return PromotionGateDiagnostic(
            id="profile.accepted",
            passed=True,
            message=(
                f"Profile {manifest.pipeline.profile.value} uses the accepted "
                "parser and structure setup."
            ),
        )
    accepted = bool(
        scanned_acceptance_evidence_id
        and scanned_acceptance_evidence_id.strip()
    )
    return PromotionGateDiagnostic(
        id="profile.accepted",
        passed=accepted,
        message=(
            "scanned_ocr is accepted by evidence "
            f"{scanned_acceptance_evidence_id}."
            if accepted
            else "Experimental scanned_ocr run: the bounded module acceptance "
            "gate has not been marked as passed."
        ),
    )


def _structure_gate(
    manifest: RunManifest,
    structure: StructureDiagnostic | None,
) -> PromotionGateDiagnostic:
    if manifest.pipeline.profile is not DocumentProfile.SCANNED_OCR:
        return PromotionGateDiagnostic(
            id="structure.accepted",
            passed=True,
            message="Profile structure satisfies promotion policy.",
        )

    strategy = manifest.pipeline.structure_strategy
    if strategy == SCANNED_EMBEDDED_OUTLINE_STRATEGY:
        return PromotionGateDiagnostic(
            id="structure.accepted",
            passed=True,
            message="Scanned structure uses the embedded outline.",
        )
    if strategy == OCR_FALLBACK_STRATEGY:
        return PromotionGateDiagnostic(
            id="structure.accepted",
            passed=False,
            message="Experimental scanned_ocr run: structure used the title fallback.",
        )
    if structure is None or structure.strategy != strategy or structure.details is None:
        return PromotionGateDiagnostic(
            id="structure.accepted",
            passed=False,
            message="Experimental scanned_ocr run: trusted structure evidence is missing.",
        )
    mode = structure.details.get("mode")
    ratio = structure.details.get("resolved_ratio")
    if strategy != OCR_STRUCTURE_STRATEGY or mode != "printed_index":
        return PromotionGateDiagnostic(
            id="structure.accepted",
            passed=False,
            message="Experimental scanned_ocr run: structure mode is not accepted.",
        )
    ratio_is_complete = (
        isinstance(ratio, (int, float))
        and not isinstance(ratio, bool)
        and float(ratio) == 1.0
    )
    return PromotionGateDiagnostic(
        id="structure.accepted",
        passed=ratio_is_complete,
        message=(
            "Scanned printed TOC resolution is complete."
            if ratio_is_complete
            else "Experimental scanned_ocr run: printed TOC resolution was "
            f"{float(ratio):.1%}, below full resolution."
            if isinstance(ratio, (int, float)) and not isinstance(ratio, bool)
            else "Experimental scanned_ocr run: printed TOC resolution is missing."
        ),
    )


def _walk_elements(
    items: Iterable[Chapter | ManualElement],
) -> Iterable[ManualElement]:
    for item in items:
        if isinstance(item, Chapter):
            yield from _walk_elements(item.content)
        else:
            yield item
