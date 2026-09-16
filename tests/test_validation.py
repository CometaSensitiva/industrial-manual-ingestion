from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
import pytest

from manual_ingestion.enrichment import (
    EnrichmentConfig,
    ELECTRICAL_SCHEMATIC_REVIEW_REASON,
    caption_input_sha256,
)
from manual_ingestion.models import (
    CaptionProvenance,
    Chapter,
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
    TocEntry,
    TraceMetadata,
    ValidationCheck,
    ValidationReport,
)
from manual_ingestion.run_bundle import write_run_bundle
from manual_ingestion.table_serialization import (
    TABLE_SERIALIZATION_STRATEGY,
    serialize_manual_tables,
)
from manual_ingestion.validation import build_validation_report, validate_run_bundle

VALID_CAPTION = """Tipo tecnico: UI/HMI
Oggetto: pannello operatore
Elementi visibili: display, tasto avvio
Relazioni/funzione: il tasto e sotto il display
Valori/avvertenze: non applicabile
Rilevanza RAG: dove si trova il tasto avvio?
Incertezze: nessuna evidente"""


def _check(report, check_id: str):
    return next(check for check in report.checks if check.id == check_id)


@pytest.mark.parametrize(
    "name",
    ["case1-schema11-pages3-1004-20260712", "case2-schema11-pages3-1004-20260712"],
)
def test_available_historical_v2_enriched_bundle_still_validates(name) -> None:
    # Local acceptance evidence is intentionally ignored by Git. The byte-level
    # synthetic prompt regression in test_enrichment runs on every checkout.
    root = Path(__file__).resolve().parents[1] / "runs" / name
    if not root.is_dir():
        pytest.skip("historical acceptance bundle is not available in this checkout")
    manifest = json.loads((root / "run.json").read_text(encoding="utf-8"))
    assert manifest["pipeline"]["config"]["enrichment"]["prompt_version"] == "technical-caption-v2"
    report = validate_run_bundle(root)
    assert report.status == "passed", [
        (check.id, check.message) for check in report.checks if not check.passed
    ]
    assert _check(report, "captions.input.coherent").passed
    assert _check(report, "captions.provenance.identity").passed


def _make_bundle(
    tmp_path: Path,
    *,
    element_page: int = 1,
    pages_processed: list[int] | None = None,
    parent_chapter_id: str | None = "chapter-1",
    image_path: str = "assets/images/image-1.png",
    create_asset: bool = True,
    duplicate_id: bool = False,
    caption_without_provenance: bool = False,
    invalid_caption: bool = False,
    toc_page: int = 1,
    status: RunStatus = RunStatus.COMPLETED,
) -> Path:
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
        status=status,
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
            enrichment_provider="fake",
            enrichment_model="fake-vlm",
            config={
                "enrichment": {
                    "enabled": True,
                    "prompt_version": "technical-caption-v2",
                    "max_caption_characters": 1200,
                    "preflight": {"model": "fake-vlm", "version": "1.0"},
                }
            },
        ),
        started_at=now,
        completed_at=now if status in {RunStatus.COMPLETED, RunStatus.VALIDATED, RunStatus.EXPERIMENTAL} else None,
    )
    provenance = None
    caption = None
    if caption_without_provenance:
        caption = VALID_CAPTION
    else:
        caption = "Unstructured caption" if invalid_caption else VALID_CAPTION
        provenance = CaptionProvenance(
            provider="fake",
            model="fake-vlm",
            prompt_version="technical-caption-v2",
            input_sha256="c" * 64,
            runtime_version="1.0",
        )
    elements = [
        ManualElement(
            id="image-1",
            type="image",
            page=element_page,
            source=SourceReference(page=element_page),
            image_path=image_path,
            caption_generated=caption,
            caption_provenance=provenance,
            trace=TraceMetadata(parent_chapter_id=parent_chapter_id),
        )
    ]
    if duplicate_id:
        elements.append(
            ManualElement(
                id="image-1",
                type="text",
                page=element_page,
                source=SourceReference(page=element_page),
                text="Duplicate identifier",
                trace=TraceMetadata(parent_chapter_id="chapter-1"),
            )
        )
    processed = pages_processed if pages_processed is not None else [1]
    manual = ManualDocument(
        id="run-1",
        title="Manual",
        source_file="manual.pdf",
        metadata=ManualMetadata(
            pages_total=max([1, element_page, toc_page, *processed]),
            pages_processed=processed,
            profile=DocumentProfile.DIGITAL_OUTLINE,
            parser="docling",
            structure_strategy="embedded_outline",
            toc_available=True,
        ),
        content=[Chapter(id="chapter-1", title="One", level=1, page=1, content=elements)],
    )
    output = write_run_bundle(
        tmp_path / "run-1",
        manifest=manifest,
        manual=manual,
        toc=[TocEntry(level=1, title="One", page=toc_page)],
    )
    if create_asset and ".." not in image_path:
        asset = output / image_path
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(b"image")
        if provenance is not None and not duplicate_id:
            persisted = ManualDocument.model_validate_json(
                (output / "manual.json").read_text(encoding="utf-8")
            )
            digest = caption_input_sha256(
                persisted,
                element_id="image-1",
                asset_root=output,
                config=EnrichmentConfig(prompt_version="technical-caption-v2"),
            )
            raw = persisted.model_dump(mode="json")
            raw["content"][0]["content"][0]["caption_provenance"][
                "input_sha256"
            ] = digest
            (output / "manual.json").write_text(
                json.dumps(raw),
                encoding="utf-8",
            )
    return output


def _add_serialized_table(
    output: Path,
    *,
    table_markdown: str,
    table_image_path: str | None = None,
) -> None:
    manual_path = output / "manual.json"
    manual = ManualDocument.model_validate_json(
        manual_path.read_text(encoding="utf-8")
    )
    chapter = manual.content[0]
    assert isinstance(chapter, Chapter)
    chapter.content.append(
        ManualElement(
            id="table-1",
            type="table",
            page=1,
            source=SourceReference(page=1),
            table_image_path=table_image_path,
            table_markdown=table_markdown,
            trace=TraceMetadata(
                parent_chapter_id=chapter.id,
                include_in_rag=False,
                exclusion_reason="test fixture excludes table from retrieval",
            ),
        )
    )
    serialized = serialize_manual_tables(manual)
    manual_path.write_text(serialized.model_dump_json(), encoding="utf-8")

    manifest_path = output / "run.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pipeline"]["config"]["table_serialization"] = {
        "strategy": TABLE_SERIALIZATION_STRATEGY
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    if table_image_path is not None:
        asset = output / table_image_path
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(b"table image")


def test_valid_bundle_passes_with_stable_metrics(tmp_path) -> None:
    output = _make_bundle(tmp_path)
    before = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}

    report = validate_run_bundle(output)
    after = {path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()}

    assert report.status == "passed"
    assert all(check.passed for check in report.checks)
    assert len(report.checks) == 30
    assert report.checks[-1].id == "tables.serialization.coherent"
    assert after == before
    assert report.metrics["chapter_count"] == 1
    assert report.metrics["element_count"] == 1
    assert report.metrics["element_page_count"] == 1
    assert report.metrics["asset_reference_count"] == 1
    assert report.metrics["caption_count"] == 1
    assert report.metrics["validation_policy_version"] == "1.1"
    assert report.metrics["table_count"] == 0
    assert report.metrics["table_serialized_row_count"] == 0


def test_table_serialization_is_recomputed_with_stable_metrics(tmp_path) -> None:
    output = _make_bundle(tmp_path)
    _add_serialized_table(
        output,
        table_markdown=(
            "| Codice | Causa |\n"
            "| --- | --- |\n"
            "| EX1100 | Pressione bassa |\n"
            "| EX1101 | Sensore assente |"
        ),
    )

    report = validate_run_bundle(output)

    assert report.status == "passed"
    assert _check(report, "tables.serialization.coherent").passed
    assert report.metrics["table_count"] == 1
    assert report.metrics["table_serialization_structured_count"] == 1
    assert report.metrics["table_serialization_fallback_count"] == 0
    assert report.metrics["table_serialization_unavailable_count"] == 0
    assert report.metrics["table_serialized_row_count"] == 2


def test_altered_table_serialization_fails_the_thirtieth_check(tmp_path) -> None:
    output = _make_bundle(tmp_path)
    _add_serialized_table(
        output,
        table_markdown="| Codice | Causa |\n| --- | --- |\n| EX1100 | Pressione bassa |",
    )
    manual_path = output / "manual.json"
    manual = json.loads(manual_path.read_text(encoding="utf-8"))
    table = manual["content"][0]["content"][1]
    table["table_serialization"]["rows"][0]["serialized_text"] += " alterato"
    manual_path.write_text(json.dumps(manual), encoding="utf-8")

    report = validate_run_bundle(output)

    check = _check(report, "tables.serialization.coherent")
    assert not check.passed
    assert "differs from canonical source content or context" in check.message


def test_missing_table_serialization_fails_the_thirtieth_check(tmp_path) -> None:
    output = _make_bundle(tmp_path)
    _add_serialized_table(
        output,
        table_markdown="| Codice |\n| --- |\n| EX1100 |",
    )
    manual_path = output / "manual.json"
    manual = json.loads(manual_path.read_text(encoding="utf-8"))
    manual["content"][0]["content"][1]["table_serialization"] = None
    manual_path.write_text(json.dumps(manual), encoding="utf-8")

    report = validate_run_bundle(output)

    check = _check(report, "tables.serialization.coherent")
    assert not check.passed
    assert "missing table_serialization" in check.message


def test_coherent_fallback_and_unavailable_table_serializations_are_valid(
    tmp_path,
) -> None:
    fallback_output = _make_bundle(tmp_path / "fallback")
    _add_serialized_table(
        fallback_output,
        table_markdown="tabella non strutturata",
    )
    fallback_report = validate_run_bundle(fallback_output)

    unavailable_output = _make_bundle(tmp_path / "unavailable")
    _add_serialized_table(
        unavailable_output,
        table_markdown="   ",
        table_image_path="assets/tables/table-1.png",
    )
    unavailable_report = validate_run_bundle(unavailable_output)

    assert _check(fallback_report, "tables.serialization.coherent").passed
    assert fallback_report.metrics["table_serialization_fallback_count"] == 1
    assert fallback_report.metrics["table_serialized_row_count"] == 1
    assert _check(unavailable_report, "tables.serialization.coherent").passed
    assert unavailable_report.metrics["table_serialization_unavailable_count"] == 1
    assert unavailable_report.metrics["table_serialized_row_count"] == 0


def test_table_bundle_requires_canonical_strategy_in_manifest(tmp_path) -> None:
    output = _make_bundle(tmp_path)
    _add_serialized_table(
        output,
        table_markdown="| Codice |\n| --- |\n| EX1100 |",
    )
    manifest_path = output / "run.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["pipeline"]["config"]["table_serialization"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = validate_run_bundle(output)

    check = _check(report, "tables.serialization.coherent")
    assert not check.passed
    assert TABLE_SERIALIZATION_STRATEGY in check.message


def test_missing_manual_is_reported_without_exception(tmp_path) -> None:
    output = _make_bundle(tmp_path)
    (output / "manual.json").unlink()

    report = validate_run_bundle(output)

    assert report.status == "failed"
    assert not _check(report, "manual.exists").passed
    assert not _check(report, "manual.contract").passed


def test_missing_referenced_asset_is_reported(tmp_path) -> None:
    output = _make_bundle(tmp_path, create_asset=False)

    report = validate_run_bundle(output)

    assert not _check(report, "assets.exist").passed
    assert _check(report, "assets.paths.safe").passed


def test_manifest_artifact_traversal_is_never_followed(tmp_path) -> None:
    output = _make_bundle(tmp_path)
    raw = json.loads((output / "run.json").read_text(encoding="utf-8"))
    raw["artifacts"]["manual"] = "../outside-manual.json"
    (tmp_path / "outside-manual.json").write_text((output / "manual.json").read_text(encoding="utf-8"), encoding="utf-8")
    (output / "run.json").write_text(json.dumps(raw), encoding="utf-8")

    report = validate_run_bundle(output)

    assert not _check(report, "manifest.contract").passed
    assert not _check(report, "artifacts.paths.safe").passed
    assert not _check(report, "manual.exists").passed


def test_asset_traversal_is_reported(tmp_path) -> None:
    output = _make_bundle(tmp_path, image_path="../outside.png", create_asset=False)
    (tmp_path / "outside.png").write_bytes(b"outside")

    report = validate_run_bundle(output)

    assert not _check(report, "assets.paths.safe").passed


def test_duplicate_node_ids_are_reported(tmp_path) -> None:
    output = _make_bundle(tmp_path, duplicate_id=True)

    report = validate_run_bundle(output)

    assert not _check(report, "ids.unique").passed
    assert report.metrics["duplicate_id_count"] == 1


def test_out_of_bounds_processed_element_and_toc_pages_are_reported(tmp_path) -> None:
    output = _make_bundle(tmp_path, element_page=2, pages_processed=[2], toc_page=2)

    report = validate_run_bundle(output)

    assert not _check(report, "pages.bounds").passed
    assert not _check(report, "toc.pages.bounds").passed


def test_processed_pages_require_canonical_content_coverage(tmp_path) -> None:
    output = _make_bundle(tmp_path)
    run = json.loads((output / "run.json").read_text(encoding="utf-8"))
    run["source"]["pages_total"] = 4
    (output / "run.json").write_text(json.dumps(run), encoding="utf-8")
    manual = json.loads((output / "manual.json").read_text(encoding="utf-8"))
    manual["metadata"]["pages_total"] = 4
    manual["metadata"]["pages_processed"] = [1, 2, 3, 4]
    (output / "manual.json").write_text(json.dumps(manual), encoding="utf-8")

    insufficient = validate_run_bundle(output)
    insufficient_check = _check(insufficient, "pages.content.coverage")
    assert not insufficient_check.passed
    assert "cover only 1/4 processed pages" in insufficient_check.message
    assert insufficient.metrics["element_page_count"] == 1

    for page in (2, 3):
        manual["content"][0]["content"].append(
            {
                "id": f"text-{page}",
                "type": "text",
                "page": page,
                "source": {
                    "page": page,
                    "page_size": None,
                    "bbox": None,
                },
                "text": f"Content on page {page}",
                "image_path": None,
                "table_image_path": None,
                "table_markdown": None,
                "caption_original": None,
                "caption_generated": None,
                "caption_provenance": None,
                "trace": {
                    "reading_order": None,
                    "parent_chapter_id": "chapter-1",
                    "role": None,
                    "heading_level": None,
                    "include_in_rag": True,
                    "exclusion_reason": None,
                    "continued": False,
                    "continuation_group": None,
                },
            }
        )
    (output / "manual.json").write_text(json.dumps(manual), encoding="utf-8")

    threshold_met = validate_run_bundle(output)
    assert _check(threshold_met, "pages.content.coverage").passed
    assert threshold_met.metrics["element_page_count"] == 3


def test_incoherent_parent_trace_is_reported(tmp_path) -> None:
    output = _make_bundle(tmp_path, parent_chapter_id="chapter-other")

    report = validate_run_bundle(output)

    assert not _check(report, "trace.parents.coherent").passed


def test_caption_without_provenance_is_reported(tmp_path) -> None:
    output = _make_bundle(tmp_path, caption_without_provenance=True)

    report = validate_run_bundle(output)

    assert not _check(report, "captions.provenance.consistent").passed


def test_caption_without_required_fields_is_reported(tmp_path) -> None:
    output = _make_bundle(tmp_path, invalid_caption=True)

    report = validate_run_bundle(output)

    check = _check(report, "captions.provenance.consistent")
    assert not check.passed
    assert "invalid generated caption" in check.message


def test_unknown_caption_type_is_rejected_by_bundle_validation(tmp_path) -> None:
    output = _make_bundle(tmp_path)
    raw = json.loads((output / "manual.json").read_text(encoding="utf-8"))
    element = raw["content"][0]["content"][0]
    element["caption_generated"] = element["caption_generated"].replace(
        "Tipo tecnico: UI/HMI",
        "Tipo tecnico: marketing",
    )
    (output / "manual.json").write_text(json.dumps(raw), encoding="utf-8")

    report = validate_run_bundle(output)

    check = _check(report, "captions.provenance.consistent")
    assert not check.passed
    assert "Tipo tecnico must be one of" in check.message


def test_electrical_type_variant_cannot_bypass_canonical_review_gate(tmp_path) -> None:
    output = _make_bundle(tmp_path)
    raw = json.loads((output / "manual.json").read_text(encoding="utf-8"))
    element = raw["content"][0]["content"][0]
    element["caption_generated"] = element["caption_generated"].replace(
        "Tipo tecnico: UI/HMI",
        "Tipo tecnico: schema elettrico industriale",
    )
    (output / "manual.json").write_text(json.dumps(raw), encoding="utf-8")

    report = validate_run_bundle(output)

    check = _check(report, "captions.provenance.consistent")
    assert not check.passed
    assert "not in canonical form" in check.message
    assert "requires visual review" in check.message


def test_canonical_electrical_caption_requires_exact_rag_exclusion(tmp_path) -> None:
    output = _make_bundle(tmp_path)
    raw = json.loads((output / "manual.json").read_text(encoding="utf-8"))
    element = raw["content"][0]["content"][0]
    element["caption_generated"] = element["caption_generated"].replace(
        "Tipo tecnico: UI/HMI",
        "Tipo tecnico: schema elettrico",
    )
    (output / "manual.json").write_text(json.dumps(raw), encoding="utf-8")

    failed = validate_run_bundle(output)
    failed_check = _check(failed, "captions.provenance.consistent")
    assert not failed_check.passed
    assert "requires visual review" in failed_check.message

    element["trace"]["include_in_rag"] = False
    element["trace"]["exclusion_reason"] = ELECTRICAL_SCHEMATIC_REVIEW_REASON
    (output / "manual.json").write_text(json.dumps(raw), encoding="utf-8")

    passed = validate_run_bundle(output)
    assert _check(passed, "captions.provenance.consistent").passed


def test_run_id_mismatch_is_reported(tmp_path) -> None:
    output = _make_bundle(tmp_path)
    raw = json.loads((output / "manual.json").read_text(encoding="utf-8"))
    raw["id"] = "run-other"
    (output / "manual.json").write_text(json.dumps(raw), encoding="utf-8")

    report = validate_run_bundle(output)

    assert not _check(report, "run_id.coherent").passed


def test_unfinished_manifest_status_fails_expectation(tmp_path) -> None:
    output = _make_bundle(tmp_path, status=RunStatus.INCOMPLETE)

    report = validate_run_bundle(output)

    assert not _check(report, "manifest.status.finished").passed


def test_invalid_toc_json_becomes_failed_checks(tmp_path) -> None:
    output = _make_bundle(tmp_path)
    (output / "toc.json").write_text("{not-json", encoding="utf-8")

    report = validate_run_bundle(output)

    assert not _check(report, "toc.json").passed
    assert not _check(report, "toc.contract").passed


def test_declared_failed_validation_invalidates_experimental_bundle(tmp_path) -> None:
    output = _make_bundle(tmp_path)
    raw = json.loads((output / "run.json").read_text(encoding="utf-8"))
    raw["status"] = RunStatus.EXPERIMENTAL.value
    raw["warnings"] = ["bounded evidence only"]
    raw["artifacts"]["validation"] = "validation.json"
    (output / "run.json").write_text(json.dumps(raw), encoding="utf-8")
    failed_report = ValidationReport(
        run_id="run-1",
        status="failed",
        checks=[ValidationCheck(id="acceptance", passed=False, message="failed gate")],
    )
    (output / "validation.json").write_text(
        failed_report.model_dump_json(),
        encoding="utf-8",
    )

    report = validate_run_bundle(output)

    check = _check(report, "validation.artifact")
    assert report.status == "failed"
    assert not check.passed
    assert "experimental status requires a passed" in check.message


def test_raw_artifact_collision_is_reported_as_unsafe(tmp_path) -> None:
    output = _make_bundle(tmp_path)
    raw = json.loads((output / "run.json").read_text(encoding="utf-8"))
    raw["artifacts"]["diagnostics"] = raw["artifacts"]["assets"]
    (output / "run.json").write_text(json.dumps(raw), encoding="utf-8")

    report = validate_run_bundle(output)

    assert not _check(report, "manifest.contract").passed
    collision_check = _check(report, "artifacts.paths.safe")
    assert not collision_check.passed
    assert "collides" in collision_check.message


def test_missing_diagnostics_directory_is_reported(tmp_path) -> None:
    output = _make_bundle(tmp_path)
    (output / "diagnostics").rmdir()

    report = validate_run_bundle(output)

    assert report.status == "failed"
    assert not _check(report, "diagnostics.directory").passed


def test_invalid_raw_run_id_never_escapes_as_report_contract_error(tmp_path) -> None:
    output = _make_bundle(tmp_path)
    raw = json.loads((output / "run.json").read_text(encoding="utf-8"))
    raw["run_id"] = "../unsafe"
    (output / "run.json").write_text(json.dumps(raw), encoding="utf-8")

    report = validate_run_bundle(output)

    assert report.run_id == "run-1"
    assert report.status == "failed"
    assert not _check(report, "manifest.contract").passed


def test_fingerprint_excludes_only_the_declared_validation_artifact(tmp_path) -> None:
    output = _make_bundle(tmp_path)
    run_path = output / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["artifacts"]["validation"] = "reports/report.json"
    run_path.write_text(json.dumps(run), encoding="utf-8")
    report_path = output / "reports" / "report.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("declared report bytes", encoding="utf-8")
    before = build_validation_report(output).metrics["bundle_fingerprint_sha256"]

    (output / "validation.json").write_text(
        "orphan report must remain fingerprinted",
        encoding="utf-8",
    )
    after = build_validation_report(output).metrics["bundle_fingerprint_sha256"]

    assert after != before
