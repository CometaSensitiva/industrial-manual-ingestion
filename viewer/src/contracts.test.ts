import { describe, expect, it } from "vitest";

import {
  manualDocumentSchema,
  manualElementSchema,
  tableSerializationSchema,
} from "./contracts";

const trace = {
  reading_order: 0,
  parent_chapter_id: null,
  role: null,
  heading_level: null,
  include_in_rag: true,
  exclusion_reason: null,
  continued: false,
  continuation_group: null,
};

const structuredSerialization = {
  strategy: "header_value_rows_v1" as const,
  status: "structured" as const,
  rows: [
    {
      id: "table-1-row-0001",
      row_index: 1,
      serialized_text: "Riga: 1\nCodice: EX1100",
    },
  ],
};

function tableElement(tableSerialization: unknown = structuredSerialization) {
  return {
    id: "table-1",
    type: "table",
    page: 2,
    source: { page: 2, page_size: null, bbox: null, rotation: 0 },
    text: null,
    image_path: null,
    table_image_path: "assets/tables/table-1.png",
    table_markdown: "| Codice |\n|---|\n| EX1100 |",
    caption_original: null,
    caption_generated: "Tabella dei codici di allarme.",
    caption_provenance: null,
    table_serialization: tableSerialization,
    trace,
  };
}

describe("table serialization contract", () => {
  it("accepts only canonical bundle schema 1.1", () => {
    const manual = {
      schema_version: "1.1",
      type: "manual",
      id: "run-1",
      title: "Manuale",
      source_file: "manuale.pdf",
      language: "it",
      metadata: {
        pages_total: 2,
        pages_processed: [2],
        profile: "digital_outline",
        parser: "docling",
        structure_strategy: "embedded_outline",
        toc_available: true,
      },
      content: [tableElement()],
    };

    expect(manualDocumentSchema.parse(manual).schema_version).toBe("1.1");
    expect(() =>
      manualDocumentSchema.parse({ ...manual, schema_version: "1.0" }),
    ).toThrow();
  });

  it("accepts schema 1.1 structured rows and nullable serialization", () => {
    expect(
      manualElementSchema.parse(tableElement()).table_serialization,
    ).toEqual(structuredSerialization);

    expect(
      manualElementSchema.parse({
        ...tableElement(null),
        id: "text-1",
        type: "text",
        text: "Testo sorgente",
        table_image_path: null,
        table_markdown: null,
        caption_generated: null,
      }).table_serialization,
    ).toBeNull();
  });

  it("keeps strategy, status and row objects strict", () => {
    expect(() =>
      tableSerializationSchema.parse({
        ...structuredSerialization,
        strategy: "another_strategy",
      }),
    ).toThrow();
    expect(() =>
      tableSerializationSchema.parse({
        ...structuredSerialization,
        extra: true,
      }),
    ).toThrow();
    expect(() =>
      tableSerializationSchema.parse({
        ...structuredSerialization,
        rows: [{ ...structuredSerialization.rows[0], extra: true }],
      }),
    ).toThrow();
  });

  it("enforces status, indexes and unique row IDs", () => {
    expect(() =>
      tableSerializationSchema.parse({
        ...structuredSerialization,
        rows: [],
      }),
    ).toThrow(/structured/);
    expect(() =>
      tableSerializationSchema.parse({
        ...structuredSerialization,
        status: "fallback",
        rows: [
          structuredSerialization.rows[0],
          {
            ...structuredSerialization.rows[0],
            row_index: 2,
          },
        ],
      }),
    ).toThrow(/fallback|univoci/);
    expect(() =>
      tableSerializationSchema.parse({
        ...structuredSerialization,
        rows: [{ ...structuredSerialization.rows[0], row_index: 2 }],
      }),
    ).toThrow(/consecutivi/);
    expect(() =>
      tableSerializationSchema.parse({
        strategy: "header_value_rows_v1",
        status: "unavailable",
        rows: structuredSerialization.rows,
      }),
    ).toThrow(/unavailable/);
  });

  it("rejects serialization on non-table elements", () => {
    expect(() =>
      manualElementSchema.parse({
        ...tableElement(),
        id: "text-1",
        type: "text",
        text: "Testo sorgente",
        table_image_path: null,
        table_markdown: null,
        caption_generated: null,
      }),
    ).toThrow(/solo per le tabelle/);
  });
});
