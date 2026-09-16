import {
  assertSafeArtifactPath,
  resolveArtifactUrl,
  resolveRunLocation,
} from "./artifacts";

export interface BundleSource {
  label: string;
  kind: "remote" | "local";
  readJson(path: string, signal?: AbortSignal): Promise<unknown>;
  url(path: string): string | null;
  dispose(): void;
}

export class RemoteBundleSource implements BundleSource {
  readonly kind = "remote";
  readonly location;
  readonly label;
  constructor(input: string, pageUrl: string | URL) {
    this.location = resolveRunLocation(input, pageUrl);
    if (!["http:", "https:"].includes(this.location.baseUrl.protocol))
      throw new Error("Use an HTTP(S) URL or open a local folder.");
    this.label = this.location.baseUrl.href;
  }
  url(path: string) {
    return resolveArtifactUrl(this.location.baseUrl, path).href;
  }
  async readJson(path: string, signal?: AbortSignal) {
    let response: Response;
    try {
      response = await fetch(this.url(path), {
        signal,
        headers: { Accept: "application/json" },
      });
    } catch {
      throw new Error(
        `Cannot read ${path}. Check the URL and the server's CORS settings.`,
      );
    }
    if (!response.ok) throw new Error(`${path}: HTTP ${response.status}.`);
    try {
      return await response.json();
    } catch {
      throw new Error(`${path} is not valid JSON.`);
    }
  }
  dispose() {}
}

export class LocalBundleSource implements BundleSource {
  readonly kind = "local";
  readonly label;
  private files = new Map<string, File>();
  private urls = new Map<string, string>();
  constructor(files: File[]) {
    const manifest = files.filter((f) => f.name === "run.json");
    if (manifest.length !== 1)
      throw new Error(
        "Choose one bundle folder containing exactly one run.json.",
      );
    const manifestPath = manifest[0]!.webkitRelativePath || manifest[0]!.name;
    const root = manifestPath.slice(0, -"run.json".length);
    this.label = root.replace(/\/$/, "") || "Local bundle";
    for (const file of files) {
      const path = file.webkitRelativePath || file.name;
      if (path.startsWith(root)) this.files.set(path.slice(root.length), file);
    }
  }
  private key(path: string) {
    assertSafeArtifactPath(path);
    return path.replaceAll("\\", "/").replace(/^\.\//, "");
  }
  url(path: string) {
    const key = this.key(path),
      file = this.files.get(key);
    if (!file) return null;
    if (!this.urls.has(key)) this.urls.set(key, URL.createObjectURL(file));
    return this.urls.get(key)!;
  }
  async readJson(path: string) {
    const file = this.files.get(this.key(path));
    if (!file)
      throw new Error(
        `Missing file: ${path}. Choose the entire bundle folder.`,
      );
    try {
      return JSON.parse(await file.text()) as unknown;
    } catch {
      throw new Error(`${path} is not valid JSON.`);
    }
  }
  dispose() {
    for (const url of this.urls.values()) URL.revokeObjectURL(url);
    this.urls.clear();
  }
}
