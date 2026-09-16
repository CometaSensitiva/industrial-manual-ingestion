import { describe, expect, it, vi } from "vitest";
import { LocalBundleSource, RemoteBundleSource } from "./bundleSource";
import { loadBundleSource } from "./loadRun";
import { readFileSync, readdirSync } from "node:fs";
import { resolve, relative } from "node:path";

function file(path: string, value: string) {
  const f = new File([value], path.split("/").at(-1)!);
  Object.defineProperty(f, "webkitRelativePath", { value: path });
  return f;
}
describe("local bundle source", () => {
  it("reads files without network requests and releases object URLs", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const revoke = vi.spyOn(URL, "revokeObjectURL");
    const source = new LocalBundleSource([
      file("bundle/run.json", '{"ok":true}'),
      file("bundle/assets/a.png", "image"),
    ]);
    expect(await source.readJson("run.json")).toEqual({ ok: true });
    expect(source.url("assets/a.png")).toMatch(/^blob:/);
    expect(source.url("missing.png")).toBeNull();
    expect(() => source.url("../secret")).toThrow();
    expect(fetchSpy).not.toHaveBeenCalled();
    source.dispose();
    expect(revoke).toHaveBeenCalledTimes(1);
    vi.restoreAllMocks();
  });
  it("rejects ambiguous folders and non-web remote schemes", () => {
    expect(() => new LocalBundleSource([])).toThrow();
    expect(
      () =>
        new LocalBundleSource([
          file("a/run.json", "{}"),
          file("b/run.json", "{}"),
        ]),
    ).toThrow();
    expect(
      () =>
        new RemoteBundleSource(
          "file:///private/run.json",
          "https://example.org",
        ),
    ).toThrow();
  });
  it("opens the real synthetic bundle from a folder", async () => {
    const root = resolve("../examples/synthetic-bundle");
    const files = readdirSync(root, { recursive: true, withFileTypes: true })
      .filter((f) => f.isFile())
      .map((f) => {
        const path = resolve(f.parentPath, f.name);
        return file(
          `bundle/${relative(root, path)}`,
          readFileSync(path, "utf8"),
        );
      });
    const bundle = await loadBundleSource(new LocalBundleSource(files));
    expect(bundle.manifest.run_id).toBe("synthetic-example");
    expect(bundle.validation?.status).toBe("passed");
  });
});
