from __future__ import annotations

from dataclasses import dataclass
import hashlib

import pytest

from manual_ingestion.enrichment import (
    CAPTION_UNAVAILABLE_REASON,
    ELECTRICAL_SCHEMATIC_REVIEW_REASON,
    UNCERTAINTY_REVIEW_REASON,
    TABLE_STRUCTURED_SERIALIZATION_REASON,
    CaptionRequest,
    CaptionResponse,
    EnrichmentConfig,
    build_caption_prompt,
    caption_input_sha256,
    caption_review_reason,
    enrich_manual,
)
from manual_ingestion.progress import ProgressEvent, ProgressStage, ProgressState
from manual_ingestion.models import (
    Chapter,
    DocumentProfile,
    ManualDocument,
    ManualElement,
    ManualMetadata,
    SourceReference,
    TraceMetadata,
)

VALID_CAPTION = """Tipo tecnico: UI/HMI
Oggetto: pannello operatore
Elementi visibili: display, tasto avvio
Relazioni/funzione: il tasto e sotto il display
Valori/avvertenze: non applicabile
Rilevanza RAG: dove si trova il tasto avvio?
Incertezze: nessuna evidente"""


@dataclass
class FakeProvider:
    text: str = VALID_CAPTION
    done_reason: str = "stop"

    def caption(self, request: CaptionRequest) -> CaptionResponse:
        return CaptionResponse(
            text=self.text,
            provider="fake",
            model="fake-vlm",
            params={"temperature": 0},
            runtime_version="1",
            done_reason=self.done_reason,
        )


def manual_with_image(*, included: bool = True) -> ManualDocument:
    return ManualDocument(
        id="run-1",
        title="Manuale",
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
                title="Uso",
                level=1,
                page=1,
                content=[
                    ManualElement(id="text-1", type="text", page=1, source=SourceReference(page=1), text="Pannello operatore."),
                    ManualElement(
                        id="image-1",
                        type="image",
                        page=1,
                        source=SourceReference(page=1),
                        image_path="assets/images/image-1.png",
                        trace=TraceMetadata(include_in_rag=included, exclusion_reason=None if included else "small crop"),
                    ),
                ],
            )
        ],
    )


def image_element(manual: ManualDocument) -> ManualElement:
    chapter = manual.content[0]
    assert isinstance(chapter, Chapter)
    element = chapter.content[1]
    assert isinstance(element, ManualElement)
    return element


def test_enrichment_writes_validated_caption_and_provenance(tmp_path) -> None:
    asset = tmp_path / "assets" / "images" / "image-1.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"image")

    enriched, report = enrich_manual(manual_with_image(), asset_root=tmp_path, provider=FakeProvider())

    element = image_element(enriched)
    assert report.enriched == 1
    assert report.failed == 0
    assert report.review_required == 0
    assert element.caption_generated == VALID_CAPTION
    assert element.caption_provenance is not None
    assert element.caption_provenance.input_sha256 != "0" * 64


def test_enrichment_progress_counts_only_actual_provider_calls(tmp_path) -> None:
    manual = manual_with_image()
    chapter = manual.content[0]
    assert isinstance(chapter, Chapter)
    chapter.content.append(
        ManualElement(
            id="image-excluded",
            type="image",
            page=1,
            source=SourceReference(page=1),
            image_path="assets/images/not-needed.png",
            trace=TraceMetadata(
                include_in_rag=False,
                exclusion_reason="small crop",
            ),
        )
    )
    asset = tmp_path / "assets" / "images" / "image-1.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"image")
    progress: list[ProgressEvent] = []

    _, report = enrich_manual(
        manual,
        asset_root=tmp_path,
        provider=FakeProvider(),
        progress=progress.append,
    )

    assert report.enriched == 1
    assert report.skipped == 1
    assert len(progress) == 1
    assert progress[0] == ProgressEvent(
        stage=ProgressStage.ENRICHMENT,
        state=ProgressState.UPDATED,
        current=1,
        total=1,
        unit="elements",
        detail="image-1",
    )


def test_electrical_schematic_caption_is_preserved_but_excluded_for_review(
    tmp_path,
) -> None:
    asset = tmp_path / "assets" / "images" / "image-1.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"image")
    electrical_caption = VALID_CAPTION.replace(
        "Tipo tecnico: UI/HMI",
        "Tipo tecnico: schema elettrico",
    )

    enriched, report = enrich_manual(
        manual_with_image(),
        asset_root=tmp_path,
        provider=FakeProvider(text=electrical_caption),
    )

    element = image_element(enriched)
    assert report.enriched == 0
    assert report.review_required == 1
    assert element.caption_generated == electrical_caption
    assert element.caption_provenance is not None
    assert element.trace.include_in_rag is False
    assert element.trace.exclusion_reason == ELECTRICAL_SCHEMATIC_REVIEW_REASON


@pytest.mark.parametrize(
    "technical_type",
    ["schema elettrico industriale", "WIRING DIAGRAM"],
)
def test_electrical_type_variants_are_canonicalized_and_cannot_bypass_review(
    tmp_path,
    technical_type: str,
) -> None:
    asset = tmp_path / "assets" / "images" / "image-1.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"image")
    variant = VALID_CAPTION.replace(
        "Tipo tecnico: UI/HMI",
        f"Tipo tecnico: {technical_type}",
    )

    enriched, report = enrich_manual(
        manual_with_image(),
        asset_root=tmp_path,
        provider=FakeProvider(text=variant),
    )

    element = image_element(enriched)
    assert report.review_required == 1
    assert element.caption_generated is not None
    assert element.caption_generated.startswith("Tipo tecnico: schema elettrico\n")
    assert element.trace.include_in_rag is False
    assert element.trace.exclusion_reason == ELECTRICAL_SCHEMATIC_REVIEW_REASON


def test_unknown_technical_type_fails_closed(tmp_path) -> None:
    asset = tmp_path / "assets" / "images" / "image-1.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"image")
    unknown = VALID_CAPTION.replace(
        "Tipo tecnico: UI/HMI",
        "Tipo tecnico: marketing",
    )

    enriched, report = enrich_manual(
        manual_with_image(),
        asset_root=tmp_path,
        provider=FakeProvider(text=unknown),
    )

    assert report.failed == 1
    assert "Tipo tecnico must be one of" in report.items[0].reason
    assert image_element(enriched).caption_generated is None


def test_prompt_preserves_the_validated_anti_hallucination_constraints() -> None:
    manual = manual_with_image()
    prompt = build_caption_prompt(
        manual=manual,
        element=image_element(manual),
        chapter_path=("Uso",),
        section_context="Pannello operatore.",
        max_characters=1200,
    )

    assert "SOLO cio che e effettivamente visibile" in prompt
    assert "non per aggiungere elementi nuovi" in prompt
    assert "mai dedotta" in prompt


def test_historical_v2_prompt_bytes_remain_reproducible() -> None:
    manual = manual_with_image()
    prompt = build_caption_prompt(
        manual=manual,
        element=image_element(manual),
        chapter_path=("Uso",),
        section_context="Pannello operatore.",
        max_characters=1200,
        prompt_version="technical-caption-v2",
    )
    assert hashlib.sha256(prompt.encode()).hexdigest() == (
        "b74e4eba9ea43218556dc49d8b11ba6850d8396b04f6153fc19c8c2c8c0fae65"
    )


@pytest.mark.parametrize("prompt_version", ["technical-caption-v2", "technical-caption-v3", "technical-caption-v4"])
def test_caption_input_digest_replays_the_requested_prompt_version(tmp_path, prompt_version) -> None:
    asset = tmp_path / "assets" / "images" / "image-1.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"image")
    config = EnrichmentConfig(prompt_version=prompt_version)
    enriched, _ = enrich_manual(
        manual_with_image(), asset_root=tmp_path, provider=FakeProvider(), config=config,
    )
    provenance = image_element(enriched).caption_provenance
    assert provenance is not None
    assert provenance.prompt_version == prompt_version
    assert provenance.input_sha256 == caption_input_sha256(
        enriched, element_id="image-1", asset_root=tmp_path, config=config,
    )


def test_unknown_prompt_version_is_not_silently_rendered_as_current() -> None:
    manual = manual_with_image()
    with pytest.raises(ValueError, match="unsupported caption prompt version"):
        build_caption_prompt(
            manual=manual, element=image_element(manual), chapter_path=(),
            section_context="", max_characters=1200, prompt_version="technical-caption-v999",
        )


def test_enrichment_context_is_bounded_to_immutable_text_on_the_target_page(
    tmp_path,
) -> None:
    manual = ManualDocument(
        id="run-local-context",
        title="Manuale",
        source_file="manual.pdf",
        metadata=ManualMetadata(
            pages_total=2,
            pages_processed=[1, 2],
            profile=DocumentProfile.DIGITAL_OUTLINE,
            parser="docling",
            structure_strategy="embedded_outline",
            toc_available=True,
        ),
        content=[
            Chapter(
                id="chapter-1",
                title="Uso",
                level=1,
                page=1,
                content=[
                    ManualElement(
                        id="unrelated-page-one",
                        type="text",
                        page=1,
                        source=SourceReference(page=1),
                        text="SEGRETO NON CORRELATO DELLA PAGINA UNO",
                    ),
                    ManualElement(
                        id="nearby-page-two",
                        type="text",
                        page=2,
                        source=SourceReference(page=2),
                        text="Etichetta locale del pannello.",
                    ),
                    ManualElement(
                        id="target-image",
                        type="image",
                        page=2,
                        source=SourceReference(page=2),
                        image_path="assets/images/target.png",
                    ),
                ],
            )
        ],
    )
    asset = tmp_path / "assets" / "images" / "target.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"image")

    @dataclass
    class CapturingProvider(FakeProvider):
        prompt: str | None = None

        def caption(self, request: CaptionRequest) -> CaptionResponse:
            self.prompt = request.prompt
            return super().caption(request)

    provider = CapturingProvider()
    enriched, report = enrich_manual(manual, asset_root=tmp_path, provider=provider)

    assert report.enriched == 1
    assert provider.prompt is not None
    assert "[nearby-page-two] Etichetta locale del pannello." in provider.prompt
    assert "SEGRETO NON CORRELATO" not in provider.prompt
    chapter = enriched.content[0]
    assert isinstance(chapter, Chapter)
    target = chapter.content[2]
    assert isinstance(target, ManualElement)
    assert target.caption_generated == VALID_CAPTION


def test_enrichment_skips_excluded_crop_without_calling_provider(tmp_path) -> None:
    class ExplodingProvider:
        def caption(self, request: CaptionRequest) -> CaptionResponse:
            raise AssertionError("provider must not be called")

    enriched, report = enrich_manual(manual_with_image(included=False), asset_root=tmp_path, provider=ExplodingProvider())

    assert report.skipped == 1
    assert image_element(enriched).caption_generated is None


def test_invalid_or_truncated_caption_is_explicit_failure(tmp_path) -> None:
    asset = tmp_path / "assets" / "images" / "image-1.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"image")

    enriched, report = enrich_manual(
        manual_with_image(),
        asset_root=tmp_path,
        provider=FakeProvider(text="Tipo tecnico: UI", done_reason="length"),
        config=EnrichmentConfig(max_characters=700),
    )

    assert report.failed == 1
    assert "did not complete normally" in report.items[0].reason
    element = image_element(enriched)
    assert element.caption_generated is None
    assert element.trace.include_in_rag is False
    assert element.trace.exclusion_reason == CAPTION_UNAVAILABLE_REASON


def test_table_uses_structured_serialization_without_calling_provider(tmp_path) -> None:
    manual = manual_with_image(included=False)
    chapter = manual.content[0]
    assert isinstance(chapter, Chapter)
    chapter.content.append(
        ManualElement(
            id="table-1",
            type="table",
            page=1,
            source=SourceReference(page=1),
            table_markdown="| Codice | Valore |\n|---|---|\n| A | 1 |",
        )
    )

    class ExplodingProvider:
        def caption(self, request: CaptionRequest) -> CaptionResponse:
            raise AssertionError("provider must not be called for tables")

    enriched, report = enrich_manual(
        manual,
        asset_root=tmp_path,
        provider=ExplodingProvider(),
    )

    assert report.skipped == 2
    assert report.items[-1].reason == TABLE_STRUCTURED_SERIALIZATION_REASON
    enriched_chapter = enriched.content[0]
    assert isinstance(enriched_chapter, Chapter)
    table = enriched_chapter.content[-1]
    assert isinstance(table, ManualElement)
    assert table.caption_generated is None
    assert table.trace.include_in_rag is True


def test_scanned_figure_context_uses_only_text_tied_to_same_figure(tmp_path) -> None:
    manual = ManualDocument(
        id="run-figure-context",
        title="Manuale",
        source_file="manual.pdf",
        metadata=ManualMetadata(
            pages_total=1,
            pages_processed=[1],
            profile=DocumentProfile.SCANNED_OCR,
            parser="paddleocr",
            structure_strategy="ocr_reconstructed_toc",
            toc_available=True,
        ),
        content=[
            Chapter(
                id="wrong-chapter",
                title="8. Cross Slide Lock",
                level=1,
                page=1,
                content=[
                    ManualElement(id="title-a", type="title", page=1, source=SourceReference(page=1), text="1. Emergency Button (A, Fig. 11)"),
                    ManualElement(id="text-a", type="text", page=1, source=SourceReference(page=1), text="A arresta la macchina."),
                    ManualElement(id="title-d", type="title", page=1, source=SourceReference(page=1), text="4. Feed Selector (D, Fig. 12)"),
                    ManualElement(id="text-d", type="text", page=1, source=SourceReference(page=1), text="D seleziona l'avanzamento."),
                    ManualElement(id="target", type="image", page=1, source=SourceReference(page=1), image_path="assets/images/target.png"),
                    ManualElement(id="figure-label", type="text", page=1, source=SourceReference(page=1), text="Fig. 11"),
                    ManualElement(id="wrong-title", type="title", page=1, source=SourceReference(page=1), text="8. Cross Slide Lock"),
                    ManualElement(id="wrong-text", type="text", page=1, source=SourceReference(page=1), text="H blocca la slitta (Fig. 13)."),
                ],
            )
        ],
    )
    asset = tmp_path / "assets/images/target.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"image")

    @dataclass
    class CapturingProvider(FakeProvider):
        prompt: str | None = None

        def caption(self, request: CaptionRequest) -> CaptionResponse:
            self.prompt = request.prompt
            return super().caption(request)

    provider = CapturingProvider()
    enrich_manual(manual, asset_root=tmp_path, provider=provider)

    assert provider.prompt is not None
    assert "Emergency Button (A, Fig. 11)" in provider.prompt
    assert "A arresta la macchina" in provider.prompt
    assert "Feed Selector" not in provider.prompt
    assert "H blocca la slitta" not in provider.prompt


def test_caption_with_unstructured_extra_line_is_explicit_failure(tmp_path) -> None:
    asset = tmp_path / "assets" / "images" / "image-1.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"image")

    _, report = enrich_manual(
        manual_with_image(),
        asset_root=tmp_path,
        provider=FakeProvider(text=f"{VALID_CAPTION}\nnota libera"),
    )

    assert report.failed == 1
    assert "Label: value" in report.items[0].reason


def test_asset_path_cannot_escape_run_root(tmp_path) -> None:
    manual = manual_with_image()
    image_element(manual).image_path = "../secret.png"

    _, report = enrich_manual(manual, asset_root=tmp_path, provider=FakeProvider())

    assert report.failed == 1
    assert "escapes" in report.items[0].reason


@pytest.mark.parametrize(('caption', 'reason'), [
    (VALID_CAPTION.replace('UI/HMI', 'schema/diagramma').replace('display, tasto avvio', 'cavo elettrico, interruttore QF1, messa a terra'), ELECTRICAL_SCHEMATIC_REVIEW_REASON),
    (VALID_CAPTION.replace('non applicabile', 'valori M50 non leggibili'), UNCERTAINTY_REVIEW_REASON),
])
def test_v3_review_preserves_caption_and_v2_historical_policy(tmp_path, caption, reason):
    asset = tmp_path / 'assets/images/image-1.png'
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b'image')
    assert caption_review_reason(caption, prompt_version='technical-caption-v2') is None
    enriched, report = enrich_manual(manual_with_image(), asset_root=tmp_path, provider=FakeProvider(text=caption))
    element = image_element(enriched)
    assert element.caption_generated == caption
    assert element.trace.include_in_rag is False
    assert element.trace.exclusion_reason == reason
    assert report.review_required == 1
    assert report.failed == 0


def test_v3_does_not_flag_consistent_uncertainty_or_electrical_word_in_question():
    caption = VALID_CAPTION.replace('non applicabile', 'valori non leggibili').replace('nessuna evidente', 'valori non leggibili')
    assert caption_review_reason(caption) is None
    caption = VALID_CAPTION.replace('UI/HMI', 'schema/diagramma').replace('dove si trova il tasto avvio?', 'dove si trova un interruttore elettrico?')
    assert caption_review_reason(caption) is None


def test_current_caption_length_is_soft_but_historical_policy_is_preserved(tmp_path):
    asset = tmp_path / "assets/images/image-1.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"image")
    from manual_ingestion.enrichment import validate_caption_text
    caption = VALID_CAPTION.replace("pannello operatore", ("pannello operatore " * 100).strip())
    assert len(caption) > 1200
    assert validate_caption_text(caption, max_characters=1200) == caption
    with pytest.raises(ValueError, match="exceeds"):
        validate_caption_text(caption, max_characters=1200, prompt_version="technical-caption-v3")
    manual, report = enrich_manual(manual_with_image(), asset_root=tmp_path, provider=FakeProvider(text=caption))
    assert report.enriched == 1
    assert image_element(manual).caption_generated == caption
    assert image_element(manual).trace.include_in_rag


def test_motor_does_not_make_a_mechanical_diagram_an_electrical_schematic():
    caption = VALID_CAPTION.replace("UI/HMI", "schema/diagramma").replace(
        "display, tasto avvio", "motore elettrico, albero, ingranaggi")
    assert caption_review_reason(caption) is None
    assert caption_review_reason(caption, prompt_version="technical-caption-v3") == ELECTRICAL_SCHEMATIC_REVIEW_REASON
    assert caption_review_reason(caption.replace("motore elettrico", "cablaggio")) == ELECTRICAL_SCHEMATIC_REVIEW_REASON
