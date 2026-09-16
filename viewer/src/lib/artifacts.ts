export interface RunLocation {
  baseUrl: URL;
  manifestUrl: URL;
}

function decodePath(path: string): string {
  try {
    return decodeURIComponent(path);
  } catch {
    throw new Error(`Percorso artefatto non codificato correttamente: ${path}`);
  }
}

export function assertSafeArtifactPath(path: string): void {
  const decoded = decodePath(path).replaceAll("\\", "/");
  const parts = decoded.split("/");
  const hasScheme = /^[a-zA-Z][a-zA-Z\d+.-]*:/.test(decoded);

  if (
    decoded.trim() === "" ||
    decoded.startsWith("/") ||
    decoded.startsWith("//") ||
    hasScheme ||
    parts.some((part) => part === "..")
  ) {
    throw new Error(`Percorso artefatto non sicuro: ${path}`);
  }
}

export function resolveRunLocation(
  input: string,
  pageUrl: string | URL,
): RunLocation {
  const value = input.trim();
  if (!value) {
    throw new Error("Indicare la cartella base di un run bundle.");
  }

  const candidate = new URL(value, pageUrl);
  candidate.hash = "";

  if (
    candidate.pathname.endsWith("/run.json") ||
    candidate.pathname === "run.json"
  ) {
    const manifestUrl = new URL(candidate);
    const baseUrl = new URL("./", manifestUrl);
    return { baseUrl, manifestUrl };
  }

  candidate.search = "";
  if (!candidate.pathname.endsWith("/")) {
    candidate.pathname = `${candidate.pathname}/`;
  }

  return {
    baseUrl: candidate,
    manifestUrl: new URL("run.json", candidate),
  };
}

export function resolveArtifactUrl(baseUrl: URL, artifactPath: string): URL {
  assertSafeArtifactPath(artifactPath);
  const normalized = artifactPath.replaceAll("\\", "/");
  return new URL(normalized, baseUrl);
}

export function resolveDirectoryUrl(baseUrl: URL, artifactPath: string): URL {
  assertSafeArtifactPath(artifactPath);
  const normalized = artifactPath.replaceAll("\\", "/").replace(/\/*$/, "/");
  return new URL(normalized, baseUrl);
}

export function pageRasterUrl(
  baseUrl: URL,
  assetsPath: string,
  page: number,
): URL {
  if (!Number.isInteger(page) || page < 1) {
    throw new Error(`Numero pagina non valido: ${page}`);
  }
  const filename = `page_${page.toString().padStart(4, "0")}.png`;
  return resolveArtifactUrl(
    baseUrl,
    `${assetsPath.replace(/\/*$/, "")}/pages/${filename}`,
  );
}
