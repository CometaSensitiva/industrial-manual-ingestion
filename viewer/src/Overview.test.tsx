import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Overview } from "./Overview";
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
