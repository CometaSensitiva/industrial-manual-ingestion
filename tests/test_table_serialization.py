from __future__ import annotations

from manual_ingestion.models import (
    Chapter,
    DocumentProfile,
    ManualDocument,
    ManualElement,
    ManualMetadata,
    SourceReference,
    TableSerializationStatus,
    TraceMetadata,
)
from manual_ingestion.table_serialization import (
    serialize_manual_tables,
    serialize_table_element,
)


def _table(
    table_markdown: str | None,
    *,
    caption_original: str | None = "Caption originale",
    caption_generated: str | None = None,
    trace: TraceMetadata | None = None,
) -> ManualElement:
    return ManualElement(
        id="table-0007",
        type="table",
        page=12,
        source=SourceReference(page=12),
        table_image_path="assets/tables/table-0007.png",
        table_markdown=table_markdown,
        caption_original=caption_original,
        caption_generated=caption_generated,
        trace=trace or TraceMetadata(),
    )


def _serialize(element: ManualElement):
    return serialize_table_element(
        element,
        manual_title="Manuale operatore",
        chapter_path=("Diagnostica", "Allarmi"),
    )


def _manual(element: ManualElement) -> ManualDocument:
    return ManualDocument(
        id="run-1",
        title="Manuale operatore",
        source_file="manual.pdf",
        metadata=ManualMetadata(
            pages_total=12,
            pages_processed=[12],
            profile=DocumentProfile.DIGITAL_OUTLINE,
            parser="docling",
            structure_strategy="embedded_outline",
            toc_available=True,
        ),
        content=[
            Chapter(
                id="chapter-1",
                title="Diagnostica",
                level=1,
                page=12,
                content=[
                    Chapter(
                        id="chapter-2",
                        title="Allarmi",
                        level=2,
                        page=12,
                        content=[element],
                    )
                ],
            )
        ],
    )


def test_serializes_docling_gfm_rows_as_self_contained_records() -> None:
    result = _serialize(
        _table(
            "| Codice | Causa | Intervento |\n"
            "|---|---|---|\n"
            "| EX1100 | Pressione bassa | Controllare valvola |\n"
            "| EX1104 | Sensore assente | Verificare cavo |",
            caption_generated="Tabella degli allarmi EX1100–EX1104.",
        )
    )

    assert result.status is TableSerializationStatus.STRUCTURED
    assert [row.id for row in result.rows] == [
        "table-0007-row-0001",
        "table-0007-row-0002",
    ]
    assert result.rows[0].serialized_text == (
        "Manuale: Manuale operatore\n"
        "Percorso capitolo: Diagnostica > Allarmi\n"
        "Pagina PDF: 12\n"
        "Tabella: table-0007\n"
        "Descrizione tabella: Tabella degli allarmi EX1100–EX1104.\n"
        "Riga: 1\n"
        "Codice: EX1100\n"
        "Causa: Pressione bassa\n"
        "Intervento: Controllare valvola"
    )


def test_markdown_escaped_pipe_is_not_treated_as_a_column_boundary() -> None:
    result = _serialize(
        _table("| Codice | Valore |\n|---|---|\n| A\\|B | attivo |").model_copy(
            update={"caption_generated": None}
        )
    )

    assert "Codice: A|B" in result.rows[0].serialized_text
    assert "Descrizione tabella: Caption originale" in result.rows[0].serialized_text


def test_header_only_markdown_callout_becomes_generic_data() -> None:
    result = _serialize(_table("| ATTENZIONE | Indossare i guanti |\n|---|---|"))

    assert result.status is TableSerializationStatus.STRUCTURED
    assert "Colonna 1: ATTENZIONE" in result.rows[0].serialized_text
    assert "Colonna 2: Indossare i guanti" in result.rows[0].serialized_text


def test_single_markdown_callout_without_separator_becomes_generic_data() -> None:
    result = _serialize(_table("| NOTA | Riavviare il controllo |"))

    assert result.status is TableSerializationStatus.STRUCTURED
    assert "Colonna 1: NOTA" in result.rows[0].serialized_text
    assert "Colonna 2: Riavviare il controllo" in result.rows[0].serialized_text


def test_serializes_paddle_html_entities_br_and_headers() -> None:
    result = _serialize(
        _table(
            "<table><tr><th>Messaggio</th><th>Azione</th></tr>"
            "<tr><td>Allarme &#x27;A&#x27;<br>attivo</td><td>Reset</td></tr></table>"
        )
    )

    assert result.status is TableSerializationStatus.STRUCTURED
    assert "Messaggio: Allarme 'A' attivo" in result.rows[0].serialized_text
    assert "Azione: Reset" in result.rows[0].serialized_text


def test_html_without_th_uses_first_row_as_header() -> None:
    result = _serialize(
        _table("<table><tr><td>Codice</td><td>Stato</td></tr><tr><td>E1</td><td>ON</td></tr></table>")
    )

    assert "Codice: E1" in result.rows[0].serialized_text
    assert "Stato: ON" in result.rows[0].serialized_text


def test_html_rowspan_and_colspan_expand_deterministically() -> None:
    result = _serialize(
        _table(
            "<table>"
            '<tr><th rowspan="2">Codice</th><th colspan="2">Dettagli</th></tr>'
            "<tr><th>Causa</th><th>Azione</th></tr>"
            "<tr><td>E1</td><td>Pressione bassa</td><td>Reset</td></tr>"
            "</table>"
        )
    )

    text = result.rows[0].serialized_text
    assert "Codice: E1" in text
    assert "Dettagli / Causa: Pressione bassa" in text
    assert "Dettagli / Azione: Reset" in text


def test_empty_duplicate_headers_and_irregular_rows_are_canonicalized() -> None:
    result = _serialize(
        _table("| | Valore | Valore |\n|---|---|---|\n| A | B | C | D |\n| | | | |")
    )

    assert len(result.rows) == 1
    text = result.rows[0].serialized_text
    assert "Colonna 1: A" in text
    assert "Valore: B" in text
    assert "Valore [colonna 3]: C" in text
    assert "Colonna 4: D" in text


def test_plain_content_uses_one_fallback_record() -> None:
    result = _serialize(_table("Codice EX1100    Pressione bassa"))

    assert result.status is TableSerializationStatus.FALLBACK
    assert len(result.rows) == 1
    assert result.rows[0].serialized_text.endswith(
        "Contenuto: Codice EX1100 Pressione bassa"
    )


def test_missing_table_text_is_unavailable() -> None:
    result = _serialize(_table(None))

    assert result.status is TableSerializationStatus.UNAVAILABLE
    assert result.rows == []


def test_excluded_table_is_still_serialized() -> None:
    result = _serialize(
        _table(
            "| A | B |\n|---|---|\n| 1 | 2 |",
            trace=TraceMetadata(
                include_in_rag=False,
                exclusion_reason="visual crop below minimum area ratio",
            ),
        )
    )

    assert result.status is TableSerializationStatus.STRUCTURED


def test_manual_serialization_is_pure_deterministic_and_idempotent() -> None:
    original = _manual(_table("| A | B |\n|---|---|\n| 1 | 2 |"))

    first = serialize_manual_tables(original)
    second = serialize_manual_tables(first)

    original_table = original.content[0].content[0].content[0]
    first_table = first.content[0].content[0].content[0]
    assert isinstance(original_table, ManualElement)
    assert isinstance(first_table, ManualElement)
    assert original_table.table_serialization is None
    assert first.model_dump() == second.model_dump()
    assert "Percorso capitolo: Diagnostica > Allarmi" in (
        first_table.table_serialization.rows[0].serialized_text
    )
