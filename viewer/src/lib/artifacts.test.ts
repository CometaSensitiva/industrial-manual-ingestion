import { describe, expect, it } from "vitest";

import {
  pageRasterUrl,
  resolveArtifactUrl,
  resolveRunLocation,
} from "./artifacts";

describe("artifact resolution", () => {
  it("resolves a bundle directory and its declared artifacts", () => {
    const location = resolveRunLocation(
      "./runs/case-1",
      "http://127.0.0.1:5173/demo/index.html",
    );

    expect(location.manifestUrl.href).toBe(
      "http://127.0.0.1:5173/demo/runs/case-1/run.json",
    );
    expect(
      resolveArtifactUrl(location.baseUrl, "records/manual-v2.json").href,
    ).toBe("http://127.0.0.1:5173/demo/runs/case-1/records/manual-v2.json");
  });

  it("accepts a direct run.json URL without duplicating the filename", () => {
    const location = resolveRunLocation(
      "https://example.test/runs/case-2/run.json",
      "https://example.test/inspector/",
    );

    expect(location.baseUrl.href).toBe("https://example.test/runs/case-2/");
    expect(location.manifestUrl.href).toBe(
      "https://example.test/runs/case-2/run.json",
    );
  });

  it("rejects traversal and absolute artifact paths", () => {
    const base = new URL("https://example.test/runs/case-3/");
    expect(() => resolveArtifactUrl(base, "../manual.json")).toThrow(
      /non sicuro/,
    );
    expect(() => resolveArtifactUrl(base, "%2e%2e/manual.json")).toThrow(
      /non sicuro/,
    );
    expect(() => resolveArtifactUrl(base, "%2e%2e%5cmanual.json")).toThrow(
      /non sicuro/,
    );
    expect(() =>
      resolveArtifactUrl(base, "https://evil.test/manual.json"),
    ).toThrow(/non sicuro/);
    expect(() => resolveArtifactUrl(base, "/manual.json")).toThrow(
      /non sicuro/,
    );
  });

  it("uses the canonical four-digit raster filename", () => {
    expect(
      pageRasterUrl(new URL("https://example.test/run/"), "assets", 7).href,
    ).toBe("https://example.test/run/assets/pages/page_0007.png");
  });
});
