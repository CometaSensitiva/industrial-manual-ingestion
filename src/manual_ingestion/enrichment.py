"""Shared, provider-independent enrichment for image elements."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .models import CaptionProvenance, Chapter, ElementType, ManualDocument, ManualElement
from .progress import (
    ProgressCallback,
    ProgressEvent,
    ProgressStage,
    ProgressState,
    emit_progress,
)

REQUIRED_LABELS = (
    "Tipo tecnico",
    "Oggetto",
    "Elementi visibili",
    "Relazioni/funzione",
    "Valori/avvertenze",
    "Rilevanza RAG",
    "Incertezze",
)
TECHNICAL_TYPES = (
    "schema elettrico",
    "schema/diagramma",
    "componente/assemblaggio",
    "UI/HMI",
    "tabella tecnica",
    "sicurezza",
    "altro tecnico",
)
MAX_CAPTION_CHARACTERS = 1200
MAX_LOCAL_CONTEXT_ITEMS = 6
CURRENT_PROMPT_VERSION = "technical-caption-v4"
SUPPORTED_PROMPT_VERSIONS = frozenset({"technical-caption-v2", "technical-caption-v3", CURRENT_PROMPT_VERSION})
ELECTRICAL_SCHEMATIC_REVIEW_REASON = (
    "generated caption for an electrical schematic requires manual visual review"
)
UNCERTAINTY_REVIEW_REASON = (
    "generated caption declares no uncertainty but contains an uncertain reading"
)
CAPTION_ALREADY_PRESENT_REASON = "caption already present"
CAPTION_VALIDATED_REASON = "caption validated"
TABLE_STRUCTURED_SERIALIZATION_REASON = "table uses structured serialization"
CAPTION_UNAVAILABLE_REASON = "caption unavailable after enrichment failure"


@dataclass(frozen=True)
class EnrichmentConfig:
    prompt_version: str = CURRENT_PROMPT_VERSION
    max_characters: int = MAX_CAPTION_CHARACTERS
    overwrite: bool = False


@dataclass(frozen=True)
class CaptionRequest:
    element_id: str
    element_type: ElementType
    prompt: str
    asset_path: Path | None


@dataclass(frozen=True)
class CaptionResponse:
    text: str
    provider: str
    model: str
    params: dict[str, object] = field(default_factory=dict)
    runtime_version: str | None = None
    done_reason: str = "stop"


class CaptionProvider(Protocol):
    def caption(self, request: CaptionRequest) -> CaptionResponse: ...


@dataclass(frozen=True)
class EnrichmentItemResult:
    element_id: str
    status: str
    reason: str


@dataclass(frozen=True)
class EnrichmentReport:
    items: tuple[EnrichmentItemResult, ...]

    @property
    def enriched(self) -> int:
        return sum(item.status == "enriched" for item in self.items)

    @property
    def skipped(self) -> int:
        return sum(item.status == "skipped" for item in self.items)

    @property
    def failed(self) -> int:
        return sum(item.status == "failed" for item in self.items)

    @property
    def review_required(self) -> int:
        return sum(item.status == "review_required" for item in self.items)


def enrich_manual(
    manual: ManualDocument,
    *,
    asset_root: Path,
    provider: CaptionProvider,
    config: EnrichmentConfig = EnrichmentConfig(),
    progress: ProgressCallback | None = None,
) -> tuple[ManualDocument, EnrichmentReport]:
    """Return an enriched copy and an explicit result for every visual element."""

    enriched = manual.model_copy(deep=True)
    asset_root = asset_root.expanduser().resolve()
    flat = list(_walk_elements(enriched))
    results: list[EnrichmentItemResult | None] = []
    jobs: list[tuple[int, ManualElement, CaptionRequest]] = []

    for element, chapter_path in flat:
        if element.type not in {ElementType.IMAGE, ElementType.TABLE}:
            continue
        if not element.trace.include_in_rag:
            results.append(EnrichmentItemResult(element.id, "skipped", element.trace.exclusion_reason or "excluded from retrieval"))
            continue
        if element.type is ElementType.TABLE:
            results.append(
                EnrichmentItemResult(
                    element.id,
                    "skipped",
                    TABLE_STRUCTURED_SERIALIZATION_REASON,
                )
            )
            continue
        if element.caption_generated and not config.overwrite:
            results.append(
                EnrichmentItemResult(
                    element.id,
                    "skipped",
                    CAPTION_ALREADY_PRESENT_REASON,
                )
            )
            continue

        try:
            asset_path = _resolve_asset(element, asset_root)
            if config.prompt_version == "technical-caption-v2":
                prompt_chapter_path = chapter_path
                section_context = _local_page_context(
                    flat,
                    target=element,
                    chapter_path=chapter_path,
                )
            else:
                prompt_chapter_path, section_context = _caption_context(
                    flat,
                    target=element,
                    chapter_path=chapter_path,
                )
            prompt = build_caption_prompt(
                manual=manual,
                element=element,
                chapter_path=prompt_chapter_path,
                section_context=section_context,
                max_characters=config.max_characters,
                prompt_version=config.prompt_version,
            )
            request = CaptionRequest(element.id, element.type, prompt, asset_path)
            result_index = len(results)
            results.append(None)
            jobs.append((result_index, element, request))
        except Exception as error:
            element.trace.include_in_rag = False
            element.trace.exclusion_reason = CAPTION_UNAVAILABLE_REASON
            results.append(EnrichmentItemResult(element.id, "failed", str(error)))

    total_calls = len(jobs)
    for current, (result_index, element, request) in enumerate(jobs, start=1):
        try:
            response = provider.caption(request)
            caption = validate_caption(response, max_characters=config.max_characters, prompt_version=config.prompt_version)
            element.caption_generated = caption
            element.caption_provenance = CaptionProvenance(
                provider=response.provider,
                model=response.model,
                prompt_version=config.prompt_version,
                params=response.params,
                input_sha256=_request_digest(request),
                runtime_version=response.runtime_version,
            )
            review_reason = caption_review_reason(caption, prompt_version=config.prompt_version)
            if review_reason is not None:
                element.trace.include_in_rag = False
                element.trace.exclusion_reason = review_reason
                results[result_index] = (
                    EnrichmentItemResult(element.id, "review_required", review_reason)
                )
            else:
                results[result_index] = (
                    EnrichmentItemResult(
                        element.id,
                        "enriched",
                        CAPTION_VALIDATED_REASON,
                    )
                )
        except Exception as error:
            element.trace.include_in_rag = False
            element.trace.exclusion_reason = CAPTION_UNAVAILABLE_REASON
            results[result_index] = EnrichmentItemResult(element.id, "failed", str(error))
        emit_progress(
            progress,
            ProgressEvent(
                stage=ProgressStage.ENRICHMENT,
                state=ProgressState.UPDATED,
                current=current,
                total=total_calls,
                unit="elements",
                detail=element.id,
            ),
        )

    if any(result is None for result in results):  # defensive invariant
        raise RuntimeError("enrichment did not produce a result for every visual element")
    return enriched, EnrichmentReport(
        tuple(result for result in results if result is not None)
    )


def validate_caption(response: CaptionResponse, *, max_characters: int,
                     prompt_version: str = CURRENT_PROMPT_VERSION) -> str:
    if response.done_reason not in {"stop", "completed"}:
        raise ValueError(f"provider did not complete normally: {response.done_reason}")
    return validate_caption_text(response.text, max_characters=max_characters, prompt_version=prompt_version)


def validate_caption_text(value: str, *, max_characters: int,
                          prompt_version: str = CURRENT_PROMPT_VERSION) -> str:
    """Validate the persisted seven-field caption independently of its provider."""

    if prompt_version not in SUPPORTED_PROMPT_VERSIONS:
        raise ValueError(f"unsupported caption prompt version: {prompt_version!r}")
    text = value.strip()
    if not text:
        raise ValueError("provider returned an empty caption")
    if prompt_version != CURRENT_PROMPT_VERSION and len(text) > max_characters:
        raise ValueError(f"caption exceeds {max_characters} characters")

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if any(":" not in line for line in lines):
        raise ValueError("every caption line must use the 'Label: value' format")
    labels = [line.split(":", 1)[0].strip() for line in lines]
    if labels != list(REQUIRED_LABELS):
        raise ValueError("caption must contain the seven required fields exactly once and in order")
    values = [line.split(":", 1)[1].strip() for line in lines]
    if any(not value for value in values):
        raise ValueError("caption fields cannot be empty")
    values[0] = _normalize_technical_type(values[0])
    return "\n".join(
        f"{label}: {value}" for label, value in zip(REQUIRED_LABELS, values, strict=True)
    )


def caption_review_reason(
    caption: str, *, prompt_version: str = CURRENT_PROMPT_VERSION,
) -> str | None:
    """Apply conservative evidence gates after the formal caption contract."""

    if prompt_version not in SUPPORTED_PROMPT_VERSIONS:
        raise ValueError(f"unsupported caption prompt version: {prompt_version!r}")
    first_line = caption.splitlines()[0] if caption.splitlines() else ""
    _, _, technical_type = first_line.partition(":")
    technical_type = _normalize_technical_type(technical_type)
    if technical_type == "schema elettrico":
        return ELECTRICAL_SCHEMATIC_REVIEW_REASON
    if prompt_version == "technical-caption-v2":
        return None

    fields = dict(line.split(":", 1) for line in caption.splitlines() if ":" in line)
    # A generic diagram label must not bypass the existing electrical review.
    # Use descriptive fields, not the generated retrieval question or nearby warning.
    observed = " ".join(fields.get(name, "") for name in (
        "Oggetto", "Elementi visibili", "Relazioni/funzione",
    )).casefold()
    electrical_pattern = (
        r"\b(?:elettric\w*|cablaggio|wiring|interruttore|messa a terra)\b"
        if prompt_version == "technical-caption-v3"
        else r"\b(?:circuito elettrico|schema elettrico|cablaggio|wiring|messa a terra)\b"
    )
    # A motor alone does not make a mechanical drawing an electrical schematic.
    if technical_type == "schema/diagramma" and re.search(electrical_pattern, observed):
        return ELECTRICAL_SCHEMATIC_REVIEW_REASON

    uncertainty = fields.get("Incertezze", "").strip().casefold()
    other_fields = " ".join(value for name, value in fields.items() if name != "Incertezze").casefold()
    if re.match(r"nessun[aoe]?\b", uncertainty) and re.search(
        r"\b(?:non\s+(?:leggibil[ei]|determinabil[ei]|chiar[oaie])|illeggibil[ei])\b",
        other_fields,
    ):
        return UNCERTAINTY_REVIEW_REASON
    return None


def build_caption_prompt(
    *,
    manual: ManualDocument,
    element: ManualElement,
    chapter_path: tuple[str, ...],
    section_context: str,
    max_characters: int,
    prompt_version: str = CURRENT_PROMPT_VERSION,
) -> str:
    """Render an explicit version; historical inputs must remain reproducible."""

    if prompt_version not in SUPPORTED_PROMPT_VERSIONS:
        raise ValueError(f"unsupported caption prompt version: {prompt_version!r}")
    renderer = (
        _build_caption_prompt_v2
        if prompt_version == "technical-caption-v2"
        else _build_caption_prompt_v3
    )
    prompt = renderer(
        manual=manual,
        element=element,
        chapter_path=chapter_path,
        section_context=section_context,
        max_characters=max_characters,
    )
    if prompt_version == CURRENT_PROMPT_VERSION:
        prompt = prompt.replace(
            f"Formato obbligatorio, massimo {max_characters} caratteri:",
            f"Formato obbligatorio; lunghezza consigliata entro {max_characters} caratteri:",
        )
    return prompt


def _build_caption_prompt_v3(
    *,
    manual: ManualDocument,
    element: ManualElement,
    chapter_path: tuple[str, ...],
    section_context: str,
    max_characters: int,
) -> str:
    # Development trials rejected longer rewrites and removing the eight-item
    # limit: both worsened fidelity/format on the inspected cases. V3 deliberately
    # keeps the wording; its runtime and review policy differ from historical V2.
    return _build_caption_prompt_v2(
        manual=manual,
        element=element,
        chapter_path=chapter_path,
        section_context=section_context,
        max_characters=max_characters,
    )


def _build_caption_prompt_v2(
    *,
    manual: ManualDocument,
    element: ManualElement,
    chapter_path: tuple[str, ...],
    section_context: str,
    max_characters: int,
) -> str:
    has_visual_asset = bool(element.image_path or element.table_image_path)
    visual_instruction = (
        "Analizza anche l'immagine allegata."
        if has_visual_asset
        else "Non hai un'immagine allegata: usa solo il testo strutturato disponibile."
    )
    table_block = (
        f"\nMarkdown tabella originale:\n{_truncate(element.table_markdown or '', 2200)}"
        if element.table_markdown
        else ""
    )
    chapter = " > ".join(chapter_path) or "Non disponibile"
    return f"""Ruolo: technical writer e ingegnere di sistema per manuali industriali.
Task: genera caption_generated per una pipeline RAG unimodale. La caption deve trasformare contenuto visuale o tabellare in testo tecnico ricercabile, non in descrizione narrativa.

Fonte primaria: {visual_instruction}
Descrivi SOLO cio che e effettivamente visibile. NON nominare dispositivi, impianti o componenti non raffigurati: se non li vedi, non vanno nella caption. Usa il testo vicino SOLO per nomi o sigle di cio che vedi, non per aggiungere elementi nuovi. Puoi includere una sola avvertenza direttamente collegata, compressa in poche parole. Non inventare dettagli non visibili. Se testo, valori o componenti non sono leggibili, scrivi "non leggibile" o "non determinabile". Non correggere table_markdown e non inventare righe o colonne. Non ripetere lo stesso elemento piu di una volta.

Manuale: {manual.title}
Tipo: {element.type.value}
Pagina: {element.page}
Capitolo: {chapter}
Caption originale: {element.caption_original or 'Non disponibile'}
Contesto locale della pagina (fonti immutabili): {_truncate(section_context, 2400) or 'Non disponibile'}{table_block}

Formato obbligatorio, massimo {max_characters} caratteri:
Tipo tecnico: scegli una sola categoria tra schema elettrico, schema/diagramma, componente/assemblaggio, UI/HMI, tabella tecnica, sicurezza, altro tecnico
Oggetto: oggetto o procedura rappresentata
Elementi visibili: massimo 8 elementi unici realmente visibili
Relazioni/funzione: collegamenti, disposizione spaziale visibile, sequenza o significato operativo; indica la posizione solo se chiaramente visibile, mai dedotta
Valori/avvertenze: codici, unita, parametri o avvisi visibili; altrimenti non applicabile
Rilevanza RAG: una domanda operativa breve
Incertezze: nessuna evidente oppure dettagli non leggibili

Rispondi solo con i sette campi, nello stesso ordine."""


def caption_input_sha256(
    manual: ManualDocument,
    *,
    element_id: str,
    asset_root: Path,
    config: EnrichmentConfig = EnrichmentConfig(),
) -> str:
    """Recompute the exact immutable prompt+asset input for one caption."""

    rows = _walk_elements(manual)
    matches = [row for row in rows if row[0].id == element_id]
    if len(matches) != 1:
        raise ValueError(
            f"caption input requires exactly one element ID {element_id!r}"
        )
    element, chapter_path = matches[0]
    if element.type not in {ElementType.IMAGE, ElementType.TABLE}:
        raise ValueError("caption input is defined only for image and table elements")
    asset_root = asset_root.expanduser().resolve()
    if config.prompt_version == "technical-caption-v2":
        prompt_chapter_path = chapter_path
        section_context = _local_page_context(
            rows,
            target=element,
            chapter_path=chapter_path,
        )
    else:
        prompt_chapter_path, section_context = _caption_context(
            rows,
            target=element,
            chapter_path=chapter_path,
        )
    request = CaptionRequest(
        element_id=element.id,
        element_type=element.type,
        prompt=build_caption_prompt(
            manual=manual,
            element=element,
            chapter_path=prompt_chapter_path,
            section_context=section_context,
            max_characters=config.max_characters,
            prompt_version=config.prompt_version,
        ),
        asset_path=_resolve_asset(element, asset_root),
    )
    return _request_digest(request)


def _walk_elements(manual: ManualDocument) -> list[tuple[ManualElement, tuple[str, ...]]]:
    rows: list[tuple[ManualElement, tuple[str, ...]]] = []

    def walk(items: list[Chapter | ManualElement], path: tuple[str, ...]) -> None:
        for item in items:
            if isinstance(item, Chapter):
                walk(item.content, (*path, item.title))
            else:
                rows.append((item, path))

    walk(manual.content, ())
    return rows


def _local_page_context(
    rows: list[tuple[ManualElement, tuple[str, ...]]],
    *,
    target: ManualElement,
    chapter_path: tuple[str, ...],
) -> str:
    """Return bounded nearby source text without generated-caption feedback."""

    target_index = next(
        index for index, (element, _) in enumerate(rows) if element is target
    )
    candidates: list[tuple[int, int, str, str]] = []
    for index, (element, path) in enumerate(rows):
        if element is target or element.page != target.page or path != chapter_path:
            continue
        immutable_text = element.text or element.caption_original
        if not immutable_text:
            continue
        candidates.append(
            (abs(index - target_index), index, element.id, immutable_text)
        )

    nearest = sorted(candidates)[:MAX_LOCAL_CONTEXT_ITEMS]
    nearest.sort(key=lambda item: item[1])
    return "\n".join(
        f"[{element_id}] {_truncate(value, 600)}"
        for _, _, element_id, value in nearest
    )


def _caption_context(
    rows: list[tuple[ManualElement, tuple[str, ...]]],
    *,
    target: ManualElement,
    chapter_path: tuple[str, ...],
) -> tuple[tuple[str, ...], str]:
    """Prefer text explicitly tied to the target figure over structural proximity."""

    figure_number = _nearby_figure_number(rows, target=target)
    if figure_number is not None:
        matched = _figure_sections(rows, page=target.page, figure_number=figure_number)
        if matched:
            titles = tuple(
                element.text.strip()
                for element in matched
                if element.type is ElementType.TITLE and element.text
            )
            context = "\n".join(
                f"[{element.id}] {_truncate(element.text or '', 600)}"
                for element in matched[:MAX_LOCAL_CONTEXT_ITEMS]
                if element.text
            )
            return titles or (f"Fig. {figure_number}",), context

    context = _local_page_context(rows, target=target, chapter_path=chapter_path)
    if context:
        return chapter_path, context
    # A structural chapter without supporting page text can be stale after OCR
    # reconstruction. Omitting it is safer than feeding a wrong topic to the VLM.
    return (), ""


def _nearby_figure_number(
    rows: list[tuple[ManualElement, tuple[str, ...]]],
    *,
    target: ManualElement,
) -> str | None:
    target_index = next(
        index for index, (element, _) in enumerate(rows) if element is target
    )
    for index in range(target_index + 1, min(len(rows), target_index + 3)):
        element, _ = rows[index]
        if element.page != target.page:
            break
        match = re.fullmatch(r"\s*Fig\.?\s*(\d+)\s*", element.text or "", re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _figure_sections(
    rows: list[tuple[ManualElement, tuple[str, ...]]],
    *,
    page: int,
    figure_number: str,
) -> list[ManualElement]:
    page_elements = [element for element, _ in rows if element.page == page]
    marker = re.compile(rf"\bFig\.?\s*{re.escape(figure_number)}\b", re.IGNORECASE)
    matched: list[ManualElement] = []
    for index, element in enumerate(page_elements):
        if element.type is not ElementType.TITLE:
            continue
        section = [element]
        for candidate in page_elements[index + 1:]:
            if candidate.type in {ElementType.TITLE, ElementType.IMAGE, ElementType.TABLE}:
                break
            if candidate.type is ElementType.TEXT:
                section.append(candidate)
        if any(marker.search(candidate.text or "") for candidate in section):
            matched.extend(section)
    return matched


def _normalize_technical_type(value: str) -> str:
    clean = " ".join(value.split())
    folded = clean.casefold()
    if any(marker in folded for marker in ("elettric", "wiring")):
        return "schema elettrico"
    allowed = {technical_type.casefold(): technical_type for technical_type in TECHNICAL_TYPES}
    try:
        return allowed[folded]
    except KeyError as exc:
        choices = ", ".join(TECHNICAL_TYPES)
        raise ValueError(f"Tipo tecnico must be one of: {choices}") from exc


def _resolve_asset(element: ManualElement, asset_root: Path) -> Path | None:
    relative = element.image_path or element.table_image_path
    if not relative:
        if element.type is ElementType.TABLE and element.table_markdown:
            return None
        raise ValueError("visual element has no asset path")
    candidate = (asset_root / relative).resolve()
    try:
        candidate.relative_to(asset_root)
    except ValueError as error:
        raise ValueError("asset path escapes the run root") from error
    if not candidate.is_file():
        raise FileNotFoundError(f"asset not found: {relative}")
    return candidate


def _request_digest(request: CaptionRequest) -> str:
    digest = hashlib.sha256()
    digest.update(request.element_id.encode())
    digest.update(request.element_type.value.encode())
    digest.update(request.prompt.encode())
    if request.asset_path:
        digest.update(request.asset_path.read_bytes())
    return digest.hexdigest()


def _truncate(value: str, maximum: int) -> str:
    clean = " ".join(value.split())
    return clean if len(clean) <= maximum else clean[: maximum - 1] + "…"
