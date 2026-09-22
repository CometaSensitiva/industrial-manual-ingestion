import { useEffect, useRef, useState } from "react";
import type { LoadedRunBundle, ManualElement, ManualNode } from "./contracts";
import { loadBundleSource } from "./lib/loadRun";
import {
  LocalBundleSource,
  RemoteBundleSource,
  type BundleSource,
} from "./lib/bundleSource";
import { bboxToPercentage } from "./lib/geometry";

import { Validation } from "./Validation";
import { Overview } from "./Overview";
import { portraitRuns } from "./brand";

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

function nodeText(node: ManualNode): string {
  return node.type === "chapter"
    ? node.title
    : [node.text, node.caption_original, node.id, node.type]
        .filter(Boolean)
        .join(" ");
}

function treeMatches(node: ManualNode, query: string): boolean {
  if (!query) return true;
  if (nodeText(node).toLowerCase().includes(query)) return true;
  return node.type === "chapter"
    ? node.content.some((child) => treeMatches(child, query))
    : false;
}

function TreeNode({
  node,
  selected,
  query,
  collapsed,
  onSelect,
  onToggle,
}: {
  node: ManualNode;
  selected: string | undefined;
  query: string;
  collapsed: Set<string>;
  onSelect: (node: ManualNode) => void;
  onToggle: (id: string) => void;
}) {
  const isChapter = node.type === "chapter";
  const hasChildren = isChapter && node.content.length > 0;
  const directMatch = nodeText(node).toLowerCase().includes(query);
  const childQuery = directMatch ? "" : query;
  const children = isChapter
    ? node.content.filter((child) => treeMatches(child, childQuery))
    : [];
  const isCollapsed = !query && collapsed.has(node.id);

  return (
    <li className={`tree-branch ${isChapter ? "is-chapter" : "is-element"}`}>
      <div className="tree-row">
        {hasChildren ? (
          <button
            className="branch-toggle"
            aria-label={`${isCollapsed ? "Expand" : "Collapse"} ${nodeText(node)}`}
            aria-expanded={!isCollapsed}
            onClick={() => onToggle(node.id)}
          >
            <span aria-hidden="true">{isCollapsed ? "›" : "⌄"}</span>
          </button>
        ) : (
          <span className="branch-spacer" aria-hidden="true" />
        )}
        <button
          data-type={node.type}
          className={`tree-node ${isChapter ? "chapter-node" : "element-node"} ${selected === node.id ? "selected" : ""}`}
          onClick={() => onSelect(node)}
          title={isChapter ? node.title : node.id}
          aria-pressed={selected === node.id}
        >
          <span className="node-type" aria-hidden="true">
            {isChapter
              ? "§"
              : node.type === "image"
                ? "▧"
                : node.type === "table"
                  ? "▦"
                  : "·"}
          </span>
          <span className="node-label">
            {isChapter
              ? node.title
              : node.caption_original ||
                node.text?.slice(0, 64) ||
                `${label(node.type)} · page ${node.page}`}
          </span>
          <small>{node.page}</small>
        </button>
      </div>
      {hasChildren && !isCollapsed && (
        <ul>
          {children.map((child) => (
            <TreeNode
              key={child.id}
              node={child}
              selected={selected}
              query={childQuery}
              collapsed={collapsed}
              onSelect={onSelect}
              onToggle={onToggle}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

function DocumentTree({
  nodes,
  selected,
  query,
  collapsed,
  onSelect,
  onToggle,
}: {
  nodes: ManualNode[];
  selected: string | undefined;
  query: string;
  collapsed: Set<string>;
  onSelect: (node: ManualNode) => void;
  onToggle: (id: string) => void;
}) {
  const normalizedQuery = query.trim().toLowerCase();
  return (
    <ul className="tree-items" aria-label="Document structure">
      {nodes
        .filter((node) => treeMatches(node, normalizedQuery))
        .map((node) => (
          <TreeNode
            key={node.id}
            node={node}
            selected={selected}
            query={normalizedQuery}
            collapsed={collapsed}
            onSelect={onSelect}
            onToggle={onToggle}
          />
        ))}
    </ul>
  );
}
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
      <div className="record-heading" data-type={element.type}>
        <span className="eyebrow">
          {element.type} / page {element.page}
        </span>
        <span className="badge">
          {element.trace.include_in_rag
            ? "Available to retrieval"
            : "Excluded from retrieval"}
        </span>
      </div>
      <section className="text-block source-evidence">
        <div className="eyebrow">SOURCE</div>
        <h3>Source evidence</h3>
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
            The original page or crop is shown alongside. Text above is attached
            to this element; other page text is available in the document tree.
          </small>
        )}
      </section>
      {element.type === "image" && (
        <section className="text-block generated">
          <div className="eyebrow">GENERATED / LOCAL MODEL</div>
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
const portraitRows = portraitRuns();

function Workbench({ bundle }: { bundle: LoadedRunBundle }) {
  const [view, setView] = useState<View>("Inspect");
  const nodes = flatten(bundle.manual.content);
  const [selected, setSelected] = useState(
    nodes.find(({ node }) => node.type === "image")?.node.id ??
      nodes[0]?.node.id,
  );
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [sourceView, setSourceView] = useState<"Page" | "Crop">("Page");
  const [zoom, setZoom] = useState(1);
  const canvasRef = useRef<HTMLDivElement>(null);
  const node = nodes.find(({ node }) => node.id === selected)?.node;
  const element = node && node.type !== "chapter" ? node : null;
  const { manifest, manual, source } = bundle;
  const asset = (path: string) => source?.url(path) ?? null;
  const page = node?.page ?? manual.metadata.pages_processed[0] ?? 1;
  const pagePath = `${manifest.artifacts.assets}/pages/page_${String(page).padStart(4, "0")}.png`;
  const cropPath = element?.image_path || element?.table_image_path;
  const showingCrop = sourceView === "Crop" && !!cropPath;
  const geometry = element ? bboxToPercentage(element.source) : null;
  const imageUrl =
    sourceView === "Crop" && cropPath ? asset(cropPath) : asset(pagePath);
  useEffect(() => {
    canvasRef.current?.scrollTo({ top: 0, left: 0 });
  }, [imageUrl]);
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
    <div className={`product-view ${view === "Inspect" ? "inspect-view" : "summary-view"}`}>
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
              ? "[ok] reported checks passed"
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
        <Overview
          bundle={bundle}
          onInspect={(id) => {
            if (id) { setSelected(id); setSourceView("Page"); setZoom(1); }
            setView("Inspect");
          }}
          onValidate={() => setView("Validation")}
        />
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
            <DocumentTree
              nodes={bundle.manual.content}
              selected={selected}
              query={query}
              collapsed={collapsed}
              onSelect={(selectedNode) => {
                setSelected(selectedNode.id);
                setZoom(1);
              }}
              onToggle={(id) =>
                setCollapsed((current) => {
                  const next = new Set(current);
                  if (next.has(id)) next.delete(id);
                  else next.add(id);
                  return next;
                })
              }
            />
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
                  disabled={zoom <= 0.5}
                  onClick={() => setZoom((value) => Math.max(0.5, value - 0.25))}
                >
                  −
                </button>
                <button aria-label="Reset zoom" onClick={() => setZoom(1)}>
                  {Math.round(zoom * 100)}%
                </button>
                <button
                  aria-label="Zoom in"
                  disabled={zoom >= 2.5}
                  onClick={() => setZoom((value) => Math.min(2.5, value + 0.25))}
                >
                  +
                </button>
              </div>
            </div>
            <div ref={canvasRef} className="canvas" tabIndex={0} aria-label="Document preview">
              <div className="zoom-surface" style={{ width: `${zoom * 100}%` }}>
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
                            data-type={el.type}
                            className={`bbox ${selected === el.id ? "active" : ""}`}
                            aria-label={`Select ${el.type} ${el.id}`}
                            style={{
                              left: `${box.left}%`,
                              top: `${box.top}%`,
                              width: `${box.width}%`,
                              height: `${box.height}%`,
                            }}
                            onClick={() => setSelected(el.id)}
                          >
                            <span className="bbox-label" aria-hidden="true">
                              {label(el.type)}
                            </span>
                          </button>
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
          <aside className="inspector" tabIndex={0} aria-label="Element inspector">
            <div className="panel-title">Inspector <span>Source → text</span></div>
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
      {view === "Validation" && <Validation bundle={bundle} />}
    </div>
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
          <span className="brand-mark" aria-hidden="true">
            <span className="brand-ascii">
              {portraitRows.map((row, y) => (
                <span key={y}>
                  {row.map((run, x) => <span key={x} className={`tone-${run.tone === "." ? "line" : run.tone}`}>{run.text}</span>)}
                  {y < portraitRows.length - 1 && "\n"}
                </span>
              ))}
            </span>
          </span>
          <span>
            Industrial Manual Ingestion<small>BUNDLE VIEWER</small>
          </span>
        </a>
        <div className="header-actions">
          <span className="local-note">Local files stay in your browser</span>
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
        <span className="pipeline">PDF <i>→</i> Structure <i>→</i> Bundle</span>
        <a href="https://github.com/CometaSensitiva/industrial-manual-ingestion">
          Source & CLI ↗
        </a>
      </footer>
    </div>
  );
}
