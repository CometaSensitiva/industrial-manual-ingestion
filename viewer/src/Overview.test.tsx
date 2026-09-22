import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Overview } from "./Overview";
import { Validation } from "./Validation";
import { manualDocumentSchema, runManifestSchema, validationReportSchema, type LoadedRunBundle, type ManualNode } from "./contracts";

function fixture(): LoadedRunBundle {
  const read = (name: string) => JSON.parse(readFileSync(new URL(`../../examples/synthetic-bundle/${name}`, import.meta.url), "utf8"));
  return { manual: manualDocumentSchema.parse(read("manual.json")), manifest: runManifestSchema.parse(read("run.json")), validation: validationReportSchema.parse(read("validation.json")), toc: [], baseUrl: new URL("https://example.org/"), manifestUrl: new URL("https://example.org/run.json") };
}
const render = (bundle: LoadedRunBundle) => renderToStaticMarkup(<Overview bundle={bundle} onInspect={() => {}} onValidate={() => {}} />);
function withoutDescriptions(nodes: ManualNode[]): void {
  for (const node of nodes) {
    if (node.type === "chapter") withoutDescriptions(node.content);
    else { node.caption_generated = null; node.caption_provenance = null; }
  }
}

describe("Overview run narrative", () => {
  it("describes the output and the selected route, with missing-preview fallbacks", () => {
    const html = render(fixture());
    expect(html).toContain("Visuals become text");
    expect(html).toContain("1/1");
    expect(html).toContain("The PDF already had a usable outline.");
    expect(html).toContain("Page preview not included");
  });
  it("does not claim image descriptions when enrichment was explicitly disabled", () => {
    const bundle = fixture();
    withoutDescriptions(bundle.manual.content);
    bundle.manifest.pipeline.config.enrichment = { enabled: false };
    const html = render(bundle);
    expect(html).toContain("Image descriptions skipped");
    expect(html).toContain("0/1");
    expect(html).not.toContain("Visuals become text");
  });
  it("does not infer a skipped step just from missing output", () => {
    const bundle = fixture();
    withoutDescriptions(bundle.manual.content);
    const html = render(bundle);
    expect(html).toContain("No descriptions included");
    expect(html).not.toContain("Image descriptions skipped");
  });
  it("reports failed or absent checks without claiming overall success", () => {
    const bundle = fixture();
    bundle.validation = { schema_version: "1.1", run_id: bundle.manifest.run_id, status: "failed", checks: [{ id: "a", passed: false, message: "missing asset" }], metrics: {} };
    expect(render(bundle)).toContain("0/1 software checks passed");
    bundle.validation = null;
    expect(render(bundle)).toContain("No software check report included");
    bundle.manifest.pipeline.profile = "scanned_ocr";
    expect(render(bundle)).toContain("This route is experimental.");
  });
});


describe("Validation report states", () => {
  it("keeps a passed report distinct from semantic verification", () => {
    const html = renderToStaticMarkup(<Validation bundle={fixture()} />);
    expect(html).toContain("Software checks passed");
    expect(html).toContain("does not mean semantically verified");
  });
  it("makes failed checks and run errors visible", () => {
    const bundle = fixture();
    bundle.validation = { schema_version: "1.1", run_id: bundle.manifest.run_id, status: "failed", checks: [{ id: "asset_exists", passed: false, message: "Missing image" }], metrics: {} };
    bundle.manifest.errors = ["Publication interrupted"];
    const html = renderToStaticMarkup(<Validation bundle={bundle} />);
    expect(html).toContain("Some checks failed");
    expect(html).toContain("Missing image");
    expect(html).toContain("Publication interrupted");
    expect(html).not.toContain("Software checks passed");
  });
  it("does not imply success or invent links when a report or source is absent", () => {
    const bundle = fixture();
    bundle.validation = null;
    const html = renderToStaticMarkup(<Validation bundle={bundle} />);
    expect(html).toContain("No validation report");
    expect(html).not.toContain("Software checks passed");
    expect(html).not.toContain("href=");
  });
});

describe("Reproduce-this-run command", () => {
  it("rebuilds the ingest command from the manifest", async () => {
    const { reproduceCommand, pageRanges } = await import("./RunLog");
    const bundle = fixture();
    expect(reproduceCommand(bundle, true)).toBe("manual-ingestion ingest synthetic-manual.pdf --out synthetic-example --page-previews");
    bundle.manifest.pipeline.config.enrichment = { enabled: false };
    bundle.manifest.pipeline.config.pages = { selection: "explicit", processed: [1, 2, 3, 5] };
    expect(reproduceCommand(bundle, false)).toBe("manual-ingestion ingest synthetic-manual.pdf --out synthetic-example --pages 1-3,5 --no-enrich");
    expect(pageRanges([2])).toBe("2");
  });
});

describe("Italian interface", () => {
  it("renders the overview and checks in Italian, leaving bundle data untouched", async () => {
    const { LangContext } = await import("./i18n");
    const bundle = fixture();
    const overview = renderToStaticMarkup(<LangContext.Provider value="it"><Overview bundle={bundle} onInspect={() => {}} onValidate={() => {}} /></LangContext.Provider>);
    expect(overview).toContain("Il PDF aveva già un indice utilizzabile.");
    expect(overview).toContain("1/1 immagini descritte");
    expect(overview).toContain("manual-ingestion ingest synthetic-manual.pdf");
    const checks = renderToStaticMarkup(<LangContext.Provider value="it"><Validation bundle={bundle} /></LangContext.Provider>);
    expect(checks).toContain("Verifiche software superate");
    expect(checks).not.toContain("Software checks passed");
  });
  it("keeps every placeholder of the English copy in the Italian copy", async () => {
    const { translator } = await import("./i18n");
    const en = translator("en"), it = translator("it");
    const vars = { n: 1, p: 1, t: 1, d: 1, c: "x", name: "x", type: "x", id: "x" };
    for (const key of ["pages", "checksMeta", "sourceFact", "describedFact", "checksSummary", "selectBox", "expand", "commandLabel"] as const) {
      expect(it(key, vars)).not.toMatch(/\{\w+\}/);
      expect(en(key, vars)).not.toMatch(/\{\w+\}/);
    }
  });
});
