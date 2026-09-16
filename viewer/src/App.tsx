import { useEffect, useRef, useState } from "react";
import type { LoadedRunBundle, ManualElement, ManualNode } from "./contracts";
import { loadBundleSource } from "./lib/loadRun";
import {
  LocalBundleSource,
  RemoteBundleSource,
  type BundleSource,
} from "./lib/bundleSource";
import { bboxToPercentage } from "./lib/geometry";

type View = "Overview" | "Inspect" | "Validation";
function flatten(
  nodes: ManualNode[],
  depth = 0,
): { node: ManualNode; depth: number }[] {
  return nodes.flatMap((node) => [
    { node, depth },
    ...(node.type === "chapter" ? flatten(node.content, depth + 1) : []),
  ]);
}
const label = (text: string) => text.replaceAll("_", " ");
function Preview({
  url,
  alt,
  children,
}: {
  url: string | null;
  alt: string;
  children?: React.ReactNode;
}) {
  const [missing, setMissing] = useState(false);
  useEffect(() => setMissing(false), [url]);
  if (!url || missing)
    return (
      <div className="empty-media">
        <span>Source preview unavailable</span>
        <small>
          The bundle does not include this image. Extracted content remains
          available.
        </small>
      </div>
    );
  return (
    <div className="image-frame">
      <img src={url} alt={alt} onError={() => setMissing(true)} />
      {children}
    </div>
  );
}
function Raw({
  value,
  title = "View JSON",
}: {
  value: unknown;
  title?: string;
}) {
  return (
    <details>
      <summary>{title}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}
function Description({ text }: { text: string }) {
  const lines = text.split("\n").filter(Boolean);
  if (!lines.every((line) => line.includes(":")))
    return <p className="preserve">{text}</p>;
  return (
    <dl className="description-fields">
      {lines.map((line, index) => {
        const split = line.indexOf(":");
        return (
          <div key={index}>
            <dt>{line.slice(0, split)}</dt>
            <dd>{line.slice(split + 1).trim()}</dd>
          </div>
        );
      })}
    </dl>
  );
}
function TableContent({ markdown }: { markdown: string }) {
  const rows = markdown
    .trim()
    .split("\n")
    .map((line) =>
      line
        .trim()
        .replace(/^\|/, "")
        .replace(/\|$/, "")
        .split(/(?<!\\)\|/)
        .map((cell) => cell.trim().replaceAll("\\|", "|")),
    );
  if (
    rows.length < 2 ||
    !rows[1]!.every((cell) => /^:?-+:?$/.test(cell)) ||
    !rows.every((row) => row.length === rows[0]!.length)
  )
    return <p className="preserve">{markdown}</p>;
  return (
    <div className="structured-table">
      <table>
        <thead>
          <tr>
            {rows[0]!.map((cell, i) => (
              <th key={i}>{cell}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(2).map((row, i) => (
            <tr key={i}>
              {row.map((cell, j) => (
                <td key={j}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
function Record({ element }: { element: ManualElement }) {
  const isTable = element.type === "table";
  return (
    <>
      <div className="record-heading">
        <span className="eyebrow">
          {element.type} · page {element.page}
        </span>
        <span className="badge">
          {element.trace.include_in_rag
            ? "Available to retrieval"
            : "Excluded from retrieval"}
        </span>
      </div>
      <section className="text-block">
        <h3>{isTable ? "Structured content" : "Source caption / text"}</h3>
        <>
          {isTable && element.table_markdown ? (
            <TableContent markdown={element.table_markdown} />
          ) : (
            <p className="preserve">
              {element.text ||
                element.caption_original ||
                "No source text attached to this element."}
            </p>
          )}
        </>
        {isTable && element.table_serialization && (
          <details>
            <summary>
              Serialized rows ({element.table_serialization.rows.length})
            </summary>
            {element.table_serialization.rows.map((row) => (
              <p className="table-row" key={row.id}>
                {row.serialized_text}
              </p>
            ))}
          </details>
        )}
        {!isTable &&
          element.text &&
          element.caption_original &&
          element.text !== element.caption_original && (
            <p>{element.caption_original}</p>
          )}
        {element.type === "image" && (
          <small>
            This is the text attached to the element. Other page text is
            available in the document tree.
          </small>
        )}
      </section>
      {element.type === "image" && (
        <section className="text-block generated">
          <div className="eyebrow">LOCAL VISION MODEL</div>
          <h3>Qwen description</h3>
          {element.caption_generated ? (
            <Description text={element.caption_generated} />
          ) : (
            <p>No generated description included.</p>
          )}
          <small>
            Generated text may contain errors. Compare it with the original.
          </small>
        </section>
      )}
      {isTable && (
        <p className="note">
          Tables use structured serialization. They are not sent to the vision
          model.
        </p>
      )}
      {element.trace.exclusion_reason && (
        <p className="note">{element.trace.exclusion_reason}</p>
      )}
      {element.caption_provenance && (
        <Raw
          title="Description provenance"
          value={element.caption_provenance}
        />
      )}
      <Raw value={element} />
    </>
  );
}
function Workbench({ bundle }: { bundle: LoadedRunBundle }) {
  const [view, setView] = useState<View>("Inspect");
  const nodes = flatten(bundle.manual.content);
  const [selected, setSelected] = useState(
    nodes.find(({ node }) => node.type === "image")?.node.id ??
      nodes[0]?.node.id,
  );
  const [query, setQuery] = useState("");
  const [sourceView, setSourceView] = useState<"Page" | "Crop">("Page");
  const [zoom, setZoom] = useState(1);
  const node = nodes.find(({ node }) => node.id === selected)?.node;
  const element = node && node.type !== "chapter" ? node : null;
  const { manifest, manual, validation, source } = bundle;
  const asset = (path: string) => source?.url(path) ?? null;
  const page = node?.page ?? manual.metadata.pages_processed[0] ?? 1;
  const pagePath = `${manifest.artifacts.assets}/pages/page_${String(page).padStart(4, "0")}.png`;
  const cropPath = element?.image_path || element?.table_image_path;
  const showingCrop = sourceView === "Crop" && !!cropPath;
  const geometry = element ? bboxToPercentage(element.source) : null;
  const imageUrl =
    sourceView === "Crop" && cropPath ? asset(cropPath) : asset(pagePath);
  const images = nodes.filter(({ node }) => node.type === "image");
  const tables = nodes.filter(({ node }) => node.type === "table");
  const processed = manual.metadata.pages_processed;
  function movePage(delta: number) {
    const index = Math.max(0, processed.indexOf(page));
    const next =
      processed[Math.max(0, Math.min(processed.length - 1, index + delta))];
    const found = nodes.find(
      ({ node }) => node.page === next && node.type !== "chapter",
    );
    if (found) {
      setSelected(found.node.id);
      setZoom(1);
    }
  }
  return (
    <>
      <div className="document-bar">
        <div>
          <span className="eyebrow">OPEN DOCUMENT</span>
          <h1>{manual.title}</h1>
        </div>
        <div className="document-meta">
          <span>{processed.length} pages</span>
          <span className="badge">{label(manifest.pipeline.profile)}</span>
          <span className="badge">
            {manifest.status === "validated"
              ? "Reported checks passed"
              : label(manifest.status)}
          </span>
        </div>
      </div>
      <nav className="tabs" aria-label="Workspace">
        {(["Overview", "Inspect", "Validation"] as View[]).map((name) => (
          <button
            key={name}
            aria-current={view === name ? "page" : undefined}
            onClick={() => setView(name)}
          >
            {name}
          </button>
        ))}
      </nav>
      {view === "Overview" && (
        <div className="overview">
          <span className="eyebrow">FROM PDF TO TRACEABLE CONTENT</span>
          <h2>A manual you can inspect.</h2>
          <p>
            Read the source, see what was extracted, and compare it with the
            generated description.
          </p>
          <div className="flow">
            <span>PDF</span>
            <b>→</b>
            <span>Structure & content</span>
            <b>→</b>
            <span>Image descriptions</span>
            <b>→</b>
            <span>Checked bundle</span>
          </div>
          <div className="facts">
            <div>
              <strong>{processed.length}</strong>
              <span>processed pages</span>
            </div>
            <div>
              <strong>{images.length}</strong>
              <span>images</span>
            </div>
            <div>
              <strong>{tables.length}</strong>
              <span>structured tables</span>
            </div>
          </div>
          <h3>How this document was processed</h3>
          <p>
            {label(manifest.pipeline.profile)} · {manifest.pipeline.parser} ·{" "}
            {label(manifest.pipeline.structure_strategy)}
          </p>
          <ul>
            {manifest.source.detected.reasons.map((reason, i) => (
              <li key={i}>{reason}</li>
            ))}
          </ul>
          <button className="primary" onClick={() => setView("Inspect")}>
            Explore the document →
          </button>
          <p className="note">
            Validation checks the software contract. It does not certify the
            meaning or accuracy of AI-generated text.
          </p>
          <details>
            <summary>Run warnings ({manifest.warnings.length})</summary>
            {manifest.warnings.map((warning, i) => (
              <p key={i}>{warning}</p>
            ))}
          </details>
        </div>
      )}
      {view === "Inspect" && (
        <div className="workspace">
          <aside className="tree">
            <div className="panel-title">
              Document <span>{nodes.length} records</span>
            </div>
            <input
              aria-label="Filter document"
              placeholder="Find content…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            <div className="tree-items">
              {nodes
                .filter(
                  ({ node }) =>
                    !query ||
                    JSON.stringify(
                      node.type === "chapter"
                        ? node.title
                        : [node.text, node.caption_original, node.id],
                    )
                      .toLowerCase()
                      .includes(query.toLowerCase()),
                )
                .map(({ node, depth }) => (
                  <button
                    key={node.id}
                    className={selected === node.id ? "selected" : ""}
                    style={{ paddingLeft: `${12 + Math.min(depth, 3) * 10}px` }}
                    onClick={() => {
                      setSelected(node.id);
                      setZoom(1);
                    }}
                    aria-pressed={selected === node.id}
                  >
                    <span className="node-type">
                      {node.type === "chapter"
                        ? "§"
                        : node.type === "image"
                          ? "▧"
                          : node.type === "table"
                            ? "▦"
                            : "·"}
                    </span>
                    <span>
                      {node.type === "chapter"
                        ? node.title
                        : node.caption_original ||
                          node.text?.slice(0, 64) ||
                          `${label(node.type)} · ${node.id}`}
                    </span>
                    <small>{node.page}</small>
                  </button>
                ))}
            </div>
          </aside>
          <section className="source-panel" aria-label="Original document">
            <div className="source-toolbar">
              <div>
                <button
                  aria-label="Previous page"
                  onClick={() => movePage(-1)}
                  disabled={page === processed[0]}
                >
                  ←
                </button>
                <span>Page {page}</span>
                <button
                  aria-label="Next page"
                  onClick={() => movePage(1)}
                  disabled={page === processed.at(-1)}
                >
                  →
                </button>
              </div>
              <div>
                {cropPath && (
                  <button
                    onClick={() =>
                      setSourceView(sourceView === "Page" ? "Crop" : "Page")
                    }
                  >
                    {sourceView === "Page" ? "Show crop" : "Show page"}
                  </button>
                )}
                <button
                  aria-label="Zoom out"
                  onClick={() => setZoom(Math.max(1, zoom - 0.25))}
                >
                  −
                </button>
                <button aria-label="Reset zoom" onClick={() => setZoom(1)}>
                  {Math.round(zoom * 100)}%
                </button>
                <button
                  aria-label="Zoom in"
                  onClick={() => setZoom(Math.min(2.5, zoom + 0.25))}
                >
                  +
                </button>
              </div>
            </div>
            <div className="canvas">
              <div style={{ width: `${zoom * 100}%`, minWidth: "100%" }}>
                <Preview
                  url={imageUrl}
                  alt={
                    showingCrop
                      ? "Original selected crop"
                      : `Original page ${page}`
                  }
                >
                  {!showingCrop &&
                    nodes
                      .filter(
                        ({ node }) =>
                          node.type !== "chapter" && node.page === page,
                      )
                      .map(({ node }) => {
                        const el = node as ManualElement;
                        const box = bboxToPercentage(el.source);
                        return box ? (
                          <button
                            key={el.id}
                            className={`bbox ${selected === el.id ? "active" : ""}`}
                            aria-label={`Select ${el.type} ${el.id}`}
                            style={{
                              left: `${box.left}%`,
                              top: `${box.top}%`,
                              width: `${box.width}%`,
                              height: `${box.height}%`,
                            }}
                            onClick={() => setSelected(el.id)}
                          />
                        ) : null;
                      })}
                </Preview>
              </div>
            </div>
            <div className="source-foot">
              {!showingCrop && geometry && imageUrl
                ? "Select an outlined region to inspect its content."
                : "Original visual evidence from the bundle."}
              {imageUrl && (
                <a href={imageUrl} target="_blank" rel="noreferrer">
                  Open original ↗
                </a>
              )}
            </div>
          </section>
          <aside className="inspector">
            <div className="panel-title">Extracted content</div>
            {element ? (
              <Record element={element} />
            ) : node ? (
              <>
                <h2>{node.type === "chapter" ? node.title : "Document"}</h2>
                <p>
                  Select an element to compare source content and enrichment.
                </p>
                <Raw value={node} />
              </>
            ) : (
              <p>No records in this bundle.</p>
            )}
          </aside>
        </div>
      )}
      {view === "Validation" && (
        <div className="validation">
          <h2>Software checks</h2>
          <p>
            {validation?.status === "passed"
              ? "The published report passed its software checks."
              : "Check the report below before using this bundle."}{" "}
            This viewer checks structure and consistency; it does not rerun the
            Python validator.
          </p>
          <p className="note">
            Included in the bundle does not mean semantically verified.
          </p>
          <details open={validation?.status !== "passed"}>
            <summary>
              {validation?.checks.length ?? 0} reported checks ·{" "}
              {validation?.status ?? "not available"}
            </summary>
            {validation?.checks.map((check) => (
              <div className="check" key={check.id}>
                <span>{check.passed ? "✓" : "!"}</span>
                <div>
                  <strong>{check.id}</strong>
                  <p>{check.message}</p>
                </div>
              </div>
            ))}
          </details>
          <details>
            <summary>Warnings ({manifest.warnings.length})</summary>
            {manifest.warnings.map((w, i) => (
              <p key={i}>{w}</p>
            ))}
          </details>
          <h3>Bundle files</h3>
          <div className="artifact-links">
            {[
              "run.json",
              manifest.artifacts.manual,
              manifest.artifacts.toc,
              manifest.artifacts.validation,
            ]
              .filter((v): v is string => !!v)
              .map((path) => {
                const url = asset(path);
                return url ? (
                  <a key={path} href={url} target="_blank" rel="noreferrer">
                    {path} ↗
                  </a>
                ) : (
                  <span key={path}>{path} · missing</span>
                );
              })}
          </div>
          <Raw title="Run manifest" value={manifest} />
        </div>
      )}
    </>
  );
}
export function App() {
  const [bundle, setBundle] = useState<LoadedRunBundle | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const active = useRef<BundleSource | null>(null);
  const sequence = useRef(0);
  const folder = useRef<HTMLInputElement>(null);
  async function load(source: BundleSource) {
    const id = ++sequence.current;
    setBusy(true);
    setError("");
    try {
      const loaded = await loadBundleSource(source);
      if (id !== sequence.current) {
        source.dispose();
        return;
      }
      active.current?.dispose();
      active.current = source;
      setBundle(loaded);
      setOpen(false);
    } catch (e) {
      source.dispose();
      if (id === sequence.current)
        setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (id === sequence.current) setBusy(false);
    }
  }
  function remote(value: string) {
    try {
      void load(new RemoteBundleSource(value, window.location.href));
    } catch (e) {
      setError(String(e));
    }
  }
  useEffect(() => {
    remote(
      new URLSearchParams(window.location.search).get("run") ||
        "./examples/synthetic-bundle/",
    );
    return () => {
      sequence.current++;
      active.current?.dispose();
    };
  }, []);
  return (
    <div className="app">
      <a className="skip" href="#main">
        Skip to content
      </a>
      <header className="app-header">
        <a
          className="brand"
          href="https://github.com/CometaSensitiva/industrial-manual-ingestion"
        >
          <span className="brand-mark">M</span>
          <span>
            Industrial Manual Ingestion<small>BUNDLE VIEWER</small>
          </span>
        </a>
        <div className="header-actions">
          <span className="local-note">Files stay in your browser</span>
          <button onClick={() => setOpen(!open)} aria-expanded={open}>
            Open bundle
          </button>
        </div>
      </header>
      {open && (
        <div className="open-panel">
          <form
            onSubmit={(e) => {
              e.preventDefault();
              remote(input);
            }}
          >
            <label htmlFor="url">Bundle URL</label>
            <div>
              <input
                id="url"
                placeholder="https://example.org/bundle/"
                value={input}
                onChange={(e) => setInput(e.target.value)}
              />
              <button disabled={busy}>Open URL</button>
            </div>
          </form>
          <button onClick={() => folder.current?.click()}>
            Choose local folder
          </button>
          <button onClick={() => remote("./examples/synthetic-bundle/")}>
            Open example
          </button>
          <input
            hidden
            ref={folder}
            type="file"
            multiple
            {...{ webkitdirectory: "" }}
            onChange={(e) => {
              if (e.target.files?.length) {
                try {
                  void load(new LocalBundleSource(Array.from(e.target.files)));
                } catch (err) {
                  setError(String(err));
                }
              }
              e.target.value = "";
            }}
          />
          <small>
            Choose the folder containing run.json. Remote servers must allow
            cross-origin reads.
          </small>
        </div>
      )}
      {error && (
        <div className="error" role="alert">
          <strong>Could not open bundle</strong>
          <p>{error}</p>
          <button onClick={() => setOpen(true)}>Choose another bundle</button>
        </div>
      )}
      <main id="main" tabIndex={-1}>
        {busy && (
          <p className="loading" role="status">
            Reading bundle…
          </p>
        )}
        {bundle && (
          <Workbench
            key={bundle.manifest.run_id + bundle.source?.label}
            bundle={bundle}
          />
        )}
      </main>
      <footer>
        <span>PDF → structured content → image descriptions</span>
        <a href="https://github.com/CometaSensitiva/industrial-manual-ingestion">
          Source & CLI ↗
        </a>
      </footer>
    </div>
  );
}
