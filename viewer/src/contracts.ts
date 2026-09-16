import type { BundleSource } from "./lib/bundleSource";
import { z } from "zod";

export const SCHEMA_VERSION = "1.1" as const;

export const documentProfileSchema = z.enum([
  "digital_outline",
  "digital_reconstructed",
  "scanned_ocr",
]);

export const runStatusSchema = z.enum([
  "incomplete",
  "completed",
  "validated",
  "experimental",
  "failed",
]);

export const elementTypeSchema = z.enum(["title", "text", "image", "table"]);

export type DocumentProfile = z.infer<typeof documentProfileSchema>;
export type RunStatus = z.infer<typeof runStatusSchema>;
export type ElementType = z.infer<typeof elementTypeSchema>;

export const pageSizeSchema = z
  .object({
    width: z.number().positive(),
    height: z.number().positive(),
  })
  .strict();

export const boundingBoxSchema = z
  .object({
    x0: z.number(),
    y0: z.number(),
    x1: z.number(),
    y1: z.number(),
  })
  .strict()
  .refine((bbox) => bbox.x1 > bbox.x0 && bbox.y1 > bbox.y0, {
    message: "Il bounding box deve avere larghezza e altezza positive",
  });

export const sourceReferenceSchema = z
  .object({
    page: z.number().int().positive(),
    page_size: pageSizeSchema.nullable(),
    bbox: boundingBoxSchema.nullable(),
    rotation: z.number(),
  })
  .strict()
  .superRefine((source, context) => {
    if (!source.bbox || !source.page_size) return;
    const { bbox, page_size: pageSize } = source;
    if (
      bbox.x0 < 0 ||
      bbox.y0 < 0 ||
      bbox.x1 > pageSize.width ||
      bbox.y1 > pageSize.height
    ) {
      context.addIssue({
        code: "custom",
        message: "Il bounding box deve essere contenuto nella pagina",
      });
    }
  });

export const traceMetadataSchema = z
  .object({
    reading_order: z.number().int().nonnegative().nullable(),
    parent_chapter_id: z.string().nullable(),
    role: z.string().nullable(),
    heading_level: z.number().int().positive().nullable(),
    include_in_rag: z.boolean(),
    exclusion_reason: z.string().nullable(),
    continued: z.boolean(),
    continuation_group: z.string().nullable(),
  })
  .strict()
  .superRefine((trace, context) => {
    if (trace.include_in_rag && trace.exclusion_reason !== null) {
      context.addIssue({
        code: "custom",
        message: "exclusion_reason richiede include_in_rag=false",
      });
    }
    if (!trace.include_in_rag && !trace.exclusion_reason) {
      context.addIssue({
        code: "custom",
        message: "include_in_rag=false richiede exclusion_reason",
      });
    }
    if (trace.continued && !trace.continuation_group) {
      context.addIssue({
        code: "custom",
        message: "continued=true richiede continuation_group",
      });
    }
  });

const sha256Schema = z
  .string()
  .regex(/^[0-9a-fA-F]{64}$/, "Digest SHA-256 non valido")
  .transform((value) => value.toLowerCase());

const safeRunIdSchema = z.string().refine(
  (value) =>
    value.length > 0 &&
    value === value.trim() &&
    value !== "." &&
    value !== ".." &&
    !value.includes("/") &&
    !value.includes("\\") &&
    ![...value].some((character) => {
      const code = character.charCodeAt(0);
      return code < 32 || code === 127;
    }),
  "run_id non è un componente di percorso sicuro",
);

function isPortableRelativePath(value: string): boolean {
  let decoded: string;
  try {
    decoded = decodeURIComponent(value).replaceAll("\\", "/");
  } catch {
    return false;
  }
  return (
    decoded.trim().length > 0 &&
    !decoded.startsWith("/") &&
    !/^[a-zA-Z]:/.test(decoded) &&
    !/^[a-zA-Z][a-zA-Z\d+.-]*:/.test(decoded) &&
    !decoded.split("/").includes("..") &&
    ![...decoded].some((character) => {
      const code = character.charCodeAt(0);
      return code < 32 || code === 127;
    })
  );
}

const artifactPathSchema = z
  .string()
  .refine(isPortableRelativePath, "Percorso artefatto relativo non valido");

export const captionProvenanceSchema = z
  .object({
    provider: z.string(),
    model: z.string(),
    prompt_version: z.string(),
    params: z.record(z.string(), z.unknown()),
    input_sha256: sha256Schema,
    runtime_version: z.string().nullable(),
  })
  .strict();

const canonicalNonEmptyStringSchema = z
  .string()
  .refine(
    (value) => value.length > 0 && value === value.trim(),
    "Il valore deve essere una stringa canonica non vuota",
  );

export const tableSerializedRowSchema = z
  .object({
    id: canonicalNonEmptyStringSchema,
    row_index: z.number().int().positive(),
    serialized_text: canonicalNonEmptyStringSchema,
  })
  .strict();

export const tableSerializationSchema = z
  .object({
    strategy: z.literal("header_value_rows_v1"),
    status: z.enum(["structured", "fallback", "unavailable"]),
    rows: z.array(tableSerializedRowSchema),
  })
  .strict()
  .superRefine((serialization, context) => {
    const expectedIndexes = serialization.rows.map((_, index) => index + 1);
    if (
      serialization.rows.some(
        (row, index) => row.row_index !== expectedIndexes[index],
      )
    ) {
      context.addIssue({
        code: "custom",
        message:
          "Gli indici delle righe devono essere consecutivi e partire da uno",
      });
    }
    const rowIds = serialization.rows.map((row) => row.id);
    if (new Set(rowIds).size !== rowIds.length) {
      context.addIssue({
        code: "custom",
        message: "Gli ID delle righe serializzate devono essere univoci",
      });
    }
    if (
      serialization.status === "structured" &&
      serialization.rows.length === 0
    ) {
      context.addIssue({
        code: "custom",
        message: "Una serializzazione structured richiede almeno una riga",
      });
    }
    if (
      serialization.status === "fallback" &&
      serialization.rows.length !== 1
    ) {
      context.addIssue({
        code: "custom",
        message: "Una serializzazione fallback richiede esattamente una riga",
      });
    }
    if (
      serialization.status === "unavailable" &&
      serialization.rows.length !== 0
    ) {
      context.addIssue({
        code: "custom",
        message: "Una serializzazione unavailable non può contenere righe",
      });
    }
  });

export type TableSerializedRow = z.infer<typeof tableSerializedRowSchema>;
export type TableSerialization = z.infer<typeof tableSerializationSchema>;

export const manualElementSchema = z
  .object({
    id: z.string(),
    type: elementTypeSchema,
    page: z.number().int().positive(),
    source: sourceReferenceSchema,
    text: z.string().nullable(),
    image_path: z.string().nullable(),
    table_image_path: z.string().nullable(),
    table_markdown: z.string().nullable(),
    caption_original: z.string().nullable(),
    caption_generated: z.string().nullable(),
    caption_provenance: captionProvenanceSchema.nullable(),
    table_serialization: tableSerializationSchema.nullable(),
    trace: traceMetadataSchema,
  })
  .strict()
  .superRefine((element, context) => {
    if (element.page !== element.source.page) {
      context.addIssue({
        code: "custom",
        message: "page e source.page non coincidono",
      });
    }
    if (element.caption_provenance && !element.caption_generated) {
      context.addIssue({
        code: "custom",
        message: "caption_provenance richiede caption_generated",
      });
    }
    if (
      element.caption_generated &&
      !["image", "table"].includes(element.type)
    ) {
      context.addIssue({
        code: "custom",
        message: "caption_generated è ammessa solo per immagini e tabelle",
      });
    }
    if (element.table_serialization !== null && element.type !== "table") {
      context.addIssue({
        code: "custom",
        message: "table_serialization è ammessa solo per le tabelle",
      });
    }
    if (["title", "text"].includes(element.type) && !element.text) {
      context.addIssue({
        code: "custom",
        message: `${element.type} richiede text`,
      });
    }
    if (element.type === "image" && !element.image_path) {
      context.addIssue({
        code: "custom",
        message: "image richiede image_path",
      });
    }
    if (
      element.type === "image" &&
      (element.table_image_path !== null ||
        element.table_markdown !== null ||
        element.table_serialization !== null)
    ) {
      context.addIssue({
        code: "custom",
        message: "image non può contenere artefatti tabella",
      });
    }
    if (
      element.type === "table" &&
      !element.table_image_path &&
      !element.table_markdown
    ) {
      context.addIssue({
        code: "custom",
        message: "table richiede table_image_path o table_markdown",
      });
    }
    if (element.type === "table" && element.image_path !== null) {
      context.addIssue({
        code: "custom",
        message: "table non può contenere image_path",
      });
    }
  });

export type ManualElement = z.infer<typeof manualElementSchema>;

export type Chapter = {
  id: string;
  type: "chapter";
  title: string;
  level: number;
  page: number;
  content: Array<Chapter | ManualElement>;
};

export const chapterSchema: z.ZodType<Chapter> = z.lazy(() =>
  z
    .object({
      id: z.string(),
      type: z.literal("chapter"),
      title: z.string(),
      level: z.number().int().positive(),
      page: z.number().int().positive(),
      content: z.array(z.union([chapterSchema, manualElementSchema])),
    })
    .strict(),
);

export type ManualNode = Chapter | ManualElement;

export const manualMetadataSchema = z
  .object({
    pages_total: z.number().int().positive(),
    pages_processed: z.array(z.number().int().positive()).min(1),
    profile: documentProfileSchema,
    parser: z.string(),
    structure_strategy: z.string(),
    toc_available: z.boolean(),
  })
  .strict()
  .superRefine((metadata, context) => {
    const pages = metadata.pages_processed;
    if (new Set(pages).size !== pages.length) {
      context.addIssue({
        code: "custom",
        message: "pages_processed contiene duplicati",
      });
    }
    if (
      pages.some(
        (page, index) => index > 0 && page < (pages[index - 1] ?? page),
      )
    ) {
      context.addIssue({
        code: "custom",
        message: "pages_processed deve essere ordinato",
      });
    }
    if ((pages.at(-1) ?? 0) > metadata.pages_total) {
      context.addIssue({
        code: "custom",
        message: "pages_processed supera pages_total",
      });
    }
  });

export const manualDocumentSchema = z
  .object({
    schema_version: z.literal(SCHEMA_VERSION),
    type: z.literal("manual"),
    id: safeRunIdSchema,
    title: z.string(),
    source_file: z.string(),
    language: z.string(),
    metadata: manualMetadataSchema,
    content: z.array(z.union([chapterSchema, manualElementSchema])),
  })
  .strict();

export type ManualDocument = z.infer<typeof manualDocumentSchema>;

export const tocEntrySchema = z
  .object({
    level: z.number().int().positive(),
    title: z.string(),
    page: z.number().int().positive(),
    confidence: z.number().min(0).max(1).nullable(),
  })
  .strict();

export const tocDocumentSchema = z.array(tocEntrySchema);
export type TocEntry = z.infer<typeof tocEntrySchema>;

export const detectedCapabilitiesSchema = z
  .object({
    profile: documentProfileSchema,
    embedded_outline: z.boolean(),
    outline_entries: z.number().int().nonnegative(),
    text_layer: z.boolean(),
    sampled_pages: z.array(z.number().int().positive()).min(1),
    pages_with_text: z.number().int().nonnegative(),
    median_text_characters: z.number().nonnegative(),
    profile_confidence: z.number().min(0).max(1),
    reasons: z.array(z.string()),
  })
  .strict()
  .superRefine((detected, context) => {
    if (
      new Set(detected.sampled_pages).size !== detected.sampled_pages.length
    ) {
      context.addIssue({
        code: "custom",
        message: "sampled_pages contiene duplicati",
      });
    }
    if (detected.pages_with_text > detected.sampled_pages.length) {
      context.addIssue({
        code: "custom",
        message: "pages_with_text supera le pagine campionate",
      });
    }
    if (detected.embedded_outline !== detected.outline_entries > 0) {
      context.addIssue({
        code: "custom",
        message: "embedded_outline non coincide con outline_entries",
      });
    }
    if (
      detected.profile === "digital_outline" &&
      !(detected.embedded_outline && detected.text_layer)
    ) {
      context.addIssue({
        code: "custom",
        message: "digital_outline richiede outline e text layer",
      });
    }
    if (
      detected.profile === "digital_reconstructed" &&
      (detected.embedded_outline || !detected.text_layer)
    ) {
      context.addIssue({
        code: "custom",
        message: "digital_reconstructed richiede text layer senza outline",
      });
    }
    if (detected.profile === "scanned_ocr" && detected.text_layer) {
      context.addIssue({
        code: "custom",
        message: "scanned_ocr richiede assenza di text layer",
      });
    }
  });

export const sourceDocumentSchema = z
  .object({
    file: z.string(),
    sha256: sha256Schema,
    size_bytes: z.number().int().nonnegative(),
    pages_total: z.number().int().positive(),
    detected: detectedCapabilitiesSchema,
  })
  .strict()
  .superRefine((source, context) => {
    if ((source.detected.sampled_pages.at(-1) ?? 0) > source.pages_total) {
      context.addIssue({
        code: "custom",
        message: "sampled_pages supera il numero di pagine sorgente",
      });
    }
  });

export const pipelineSelectionSchema = z
  .object({
    profile: documentProfileSchema,
    parser: z.string(),
    structure_strategy: z.string(),
    enrichment_provider: z.string().nullable(),
    enrichment_model: z.string().nullable(),
    config: z.record(z.string(), z.unknown()),
  })
  .strict();

export const artifactPathsSchema = z
  .object({
    manual: artifactPathSchema,
    toc: artifactPathSchema,
    validation: artifactPathSchema.nullable(),
    assets: artifactPathSchema,
    diagnostics: artifactPathSchema,
  })
  .strict()
  .superRefine((artifacts, context) => {
    const entries = [
      ["manual", artifacts.manual],
      ["toc", artifacts.toc],
      ["validation", artifacts.validation],
      ["assets", artifacts.assets],
      ["diagnostics", artifacts.diagnostics],
      ["manifest", "run.json"],
    ].filter((entry): entry is [string, string] => entry[1] !== null);
    const parts = (path: string) =>
      path.replaceAll("\\", "/").split("/").filter(Boolean);
    for (let first = 0; first < entries.length; first += 1) {
      for (let second = first + 1; second < entries.length; second += 1) {
        const firstEntry = entries[first];
        const secondEntry = entries[second];
        if (!firstEntry || !secondEntry) continue;
        const firstParts = parts(firstEntry[1]);
        const secondParts = parts(secondEntry[1]);
        const common = Math.min(firstParts.length, secondParts.length);
        if (
          firstParts.slice(0, common).join("/") ===
          secondParts.slice(0, common).join("/")
        ) {
          context.addIssue({
            code: "custom",
            message: `Percorsi artefatto sovrapposti: ${firstEntry[0]} e ${secondEntry[0]}`,
          });
        }
      }
    }
  });

export const runManifestSchema = z
  .object({
    schema_version: z.literal(SCHEMA_VERSION),
    run_id: safeRunIdSchema,
    status: runStatusSchema,
    source: sourceDocumentSchema,
    pipeline: pipelineSelectionSchema,
    artifacts: artifactPathsSchema,
    started_at: z.string(),
    completed_at: z.string().nullable(),
    warnings: z.array(z.string()),
    errors: z.array(z.string()),
  })
  .strict()
  .superRefine((manifest, context) => {
    const finished = ["completed", "validated", "experimental"].includes(
      manifest.status,
    );
    if (finished && !manifest.completed_at) {
      context.addIssue({
        code: "custom",
        message: "Un run concluso richiede completed_at",
      });
    }
    if (manifest.status === "failed" && manifest.errors.length === 0) {
      context.addIssue({
        code: "custom",
        message: "Un run failed richiede almeno un errore",
      });
    }
    if (
      ["validated", "experimental"].includes(manifest.status) &&
      manifest.artifacts.validation === null
    ) {
      context.addIssue({
        code: "custom",
        message: "Un run validated o experimental richiede validation",
      });
    }
    if (manifest.status === "experimental" && manifest.warnings.length === 0) {
      context.addIssue({
        code: "custom",
        message: "Un run experimental richiede almeno un warning",
      });
    }
    if (manifest.pipeline.profile !== manifest.source.detected.profile) {
      context.addIssue({
        code: "custom",
        message: "Profilo pipeline diverso dal profilo rilevato",
      });
    }
  });

export type RunManifest = z.infer<typeof runManifestSchema>;

export const validationCheckSchema = z
  .object({
    id: z.string(),
    passed: z.boolean(),
    message: z.string(),
  })
  .strict();

export const validationReportSchema = z
  .object({
    schema_version: z.literal(SCHEMA_VERSION),
    run_id: safeRunIdSchema,
    status: z.enum(["passed", "failed"]),
    checks: z.array(validationCheckSchema).min(1),
    metrics: z.record(z.string(), z.union([z.number(), z.string(), z.null()])),
  })
  .strict()
  .superRefine((report, context) => {
    const expected = report.checks.every((check) => check.passed)
      ? "passed"
      : "failed";
    if (report.status !== expected) {
      context.addIssue({
        code: "custom",
        message: "validation.status non coincide con gli esiti dei check",
      });
    }
  });

export type ValidationReport = z.infer<typeof validationReportSchema>;

export interface LoadedRunBundle {
  source?: BundleSource;
  baseUrl: URL;
  manifestUrl: URL;
  manifest: RunManifest;
  manual: ManualDocument;
  toc: TocEntry[];
  validation: ValidationReport | null;
}
