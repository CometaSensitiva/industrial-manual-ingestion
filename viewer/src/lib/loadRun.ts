import { type ZodType } from "zod";
import {
  type LoadedRunBundle,
  type RunManifest,
  type ManualDocument,
  type ValidationReport,
  manualDocumentSchema,
  runManifestSchema,
  tocDocumentSchema,
  validationReportSchema,
} from "../contracts";
import { type BundleSource, RemoteBundleSource } from "./bundleSource";

export class RunBundleLoadError extends Error {
  constructor(
    message: string,
    readonly resource: string,
  ) {
    super(message);
  }
}
function verifyBundleCoherence(
  manifest: RunManifest,
  manual: ManualDocument,
  validation: ValidationReport | null,
): void {
  const contradictions: string[] = [];
  if (manifest.run_id !== manual.id)
    contradictions.push("run_id differs from manual.id");
  if (manifest.source.file !== manual.source_file) {
    contradictions.push("source.file differs from manual.source_file");
  }
  if (manifest.pipeline.profile !== manifest.source.detected.profile) {
    contradictions.push("profilo pipeline differs froml profilo rilevato");
  }
  if (manifest.pipeline.profile !== manual.metadata.profile) {
    contradictions.push("profilo pipeline differs froml profilo del manuale");
  }
  if (validation && validation.run_id !== manifest.run_id) {
    contradictions.push("validation.run_id differs froml run manifest");
  }
  if (manifest.status === "validated" && validation?.status !== "passed") {
    contradictions.push("validated run without passed software checks");
  }

  if (contradictions.length > 0) {
    throw new RunBundleLoadError(
      `Inconsistent bundle: ${contradictions.join("; ")}.`,
      "bundle",
    );
  }
}

export async function loadBundleSource(
  source: BundleSource,
  signal?: AbortSignal,
): Promise<LoadedRunBundle> {
  async function read<T>(path: string, schema: ZodType<T>): Promise<T> {
    const parsed = schema.safeParse(await source.readJson(path, signal));
    if (!parsed.success) {
      const fields = parsed.error.issues
        .slice(0, 3)
        .map((i) => i.path.join(".") || "root")
        .join(", ");
      throw new RunBundleLoadError(
        `${path} does not match bundle schema 1.1. Check: ${fields}.`,
        path,
      );
    }
    return parsed.data;
  }
  const manifest = await read("run.json", runManifestSchema);
  const [manual, toc, validation] = await Promise.all([
    read(manifest.artifacts.manual, manualDocumentSchema),
    read(manifest.artifacts.toc, tocDocumentSchema),
    manifest.artifacts.validation
      ? read(manifest.artifacts.validation, validationReportSchema)
      : Promise.resolve(null),
  ]);
  verifyBundleCoherence(manifest, manual, validation);
  const location =
    source instanceof RemoteBundleSource
      ? source.location
      : {
          baseUrl: new URL("https://local.invalid/"),
          manifestUrl: new URL("https://local.invalid/run.json"),
        };
  return { ...location, source, manifest, manual, toc, validation };
}
export async function loadRunBundle(
  input: string,
  pageUrl: string | URL,
  signal?: AbortSignal,
) {
  return loadBundleSource(new RemoteBundleSource(input, pageUrl), signal);
}
