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
import { portraitCells } from "./brand";
import { LANGS, LangContext, count, detectLang, rememberLang, translator, useT, type Key, type Lang } from "./i18n";

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

function useTypeLabel() {
  const t = useT();
  return (type: string) => t(`type.${type}` as Key);
}

function recordLabel(node: ManualNode, typeLabel: (type: string) => string, pageLabel: (n: number) => string): string {
  if (node.type === "chapter") return node.title;
  return node.caption_original || node.text?.slice(0, 64) || `${typeLabel(node.type)} · ${pageLabel(node.page)}`;
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
  const t = useT();
  const typeLabel = useTypeLabel();
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
            aria-label={t(isCollapsed ? "expand" : "collapse", { name: nodeText(node) })}
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
            {isChapter ? "§" : node.type === "image" ? "▧" : node.type === "table" ? "▦" : "·"}
          </span>
          <span className="node-label">
            {recordLabel(node, typeLabel, (n) => t("pageLower", { n }))}
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
  const t = useT();
  const normalizedQuery = query.trim().toLowerCase();
  return (
    <ul className="tree-items" aria-label={t("documentStructure")}>
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
  const t = useT();
  const [missing, setMissing] = useState(false);
  useEffect(() => setMissing(false), [url]);
  if (!url || missing)
    return (
      <div className="empty-media">
        <span>{t("previewUnavailable")}</span>
        <small>{t("previewUnavailableHint")}</small>
      </div>
    );
  return (
    <div className="image-frame">
      <img src={url} alt={alt} onError={() => setMissing(true)} />
      {children}
    </div>
  );
}

function Raw({ value, title }: { value: unknown; title?: string }) {
  const t = useT();
  return (
    <details>
      <summary>{title ?? t("viewJson")}</summary>
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
  const t = useT();
  const typeLabel = useTypeLabel();
  const isTable = element.type === "table";
  const sourceText = element.text || element.caption_original;
  return (
    <article className="record" data-type={element.type}>
      <header className="record-head">
        <span className="type-chip">{typeLabel(element.type)}</span>
        <span className="record-meta">{t("pageLower", { n: element.page })}</span>
        <span className="record-meta">
          {t(element.trace.include_in_rag ? "inRetrieval" : "excludedRetrieval")}
        </span>
      </header>
      <section className="record-section">
        <h3 className="label">{t("source")}</h3>
        {isTable && element.table_markdown ? (
          <TableContent markdown={element.table_markdown} />
        ) : sourceText ? (
          <p className="preserve">{sourceText}</p>
        ) : (
          <p className="empty-note">{t("noSource")}</p>
        )}
        {!isTable &&
          element.text &&
          element.caption_original &&
          element.text !== element.caption_original && <p>{element.caption_original}</p>}
        {isTable && element.table_serialization && (
          <details>
            <summary>
              {t("serializedRows")} <span className="summary-count">{element.table_serialization.rows.length}</span>
            </summary>
            {element.table_serialization.rows.map((row) => (
              <p className="table-row" key={row.id}>
                {row.serialized_text}
              </p>
            ))}
          </details>
        )}
      </section>
      {element.type === "image" && (
        <section className="record-section generated">
          <h3 className="label">{t("generated")}</h3>
          {element.caption_generated ? (
            <Description text={element.caption_generated} />
          ) : (
            <p className="empty-note">{t("noGenerated")}</p>
          )}
          <p className="note">{t("generatedWarning")}</p>
        </section>
      )}
      {isTable && <p className="note">{t("tableNote")}</p>}
      {element.trace.exclusion_reason && <p className="note">{element.trace.exclusion_reason}</p>}
      <div className="record-raw">
        {element.caption_provenance && (
          <Raw title={t("provenance")} value={element.caption_provenance} />
        )}
        <Raw value={element} />
      </div>
    </article>
  );
}

const portrait = portraitCells();
const TONE_FILL: Record<string, string> = { h: "#d4c6fb", j: "#95a9ff", ".": "#8a8aa3" };

/** The CLI portrait as a halftone: one cell per glyph, legible at icon size. */
function BrandMark() {
  return (
    <span className="brand-mark" aria-hidden="true">
      <svg viewBox="-2 -1 48 46" width="100%" height="100%">
        {portrait.map((cell) => (
          <rect key={`${cell.x}-${cell.y}`} x={cell.x} y={cell.y * 2} width="1" height="2" fill={TONE_FILL[cell.tone] ?? TONE_FILL["."]} opacity={cell.opacity} />
        ))}
      </svg>
    </span>
  );
}

const VIEWS: { id: View; label: Key; hash: string }[] = [
  { id: "Overview", label: "overview", hash: "#overview" },
  { id: "Inspect", label: "inspect", hash: "#inspect" },
  { id: "Validation", label: "checks", hash: "#checks" },
];

function initialView(): View {
  const hash = typeof window === "undefined" ? "" : window.location.hash;
  return VIEWS.find((view) => view.hash === hash)?.id ?? "Overview";
}

function isTyping(target: EventTarget | null) {
  return target instanceof HTMLElement && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
}

function Workbench({ bundle }: { bundle: LoadedRunBundle }) {
  const t = useT();
  const typeLabel = useTypeLabel();
  const [view, setViewState] = useState<View>(initialView);
  const nodes = flatten(bundle.manual.content);
  const records = nodes.filter(({ node }) => node.type !== "chapter");
  const [selected, setSelected] = useState(
    nodes.find(({ node }) => node.type === "image")?.node.id ?? nodes[0]?.node.id,
  );
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [outlineOpen, setOutlineOpen] = useState(false);
  const [sourceView, setSourceView] = useState<"Page" | "Crop">("Page");
  const [zoom, setZoom] = useState(1);
  const canvasRef = useRef<HTMLDivElement>(null);
  const node = nodes.find(({ node }) => node.id === selected)?.node;
  const element = node && node.type !== "chapter" ? node : null;
  const { manifest, manual, validation, source } = bundle;
  const asset = (path: string) => source?.url(path) ?? null;
  const page = node?.page ?? manual.metadata.pages_processed[0] ?? 1;
  const pagePath = `${manifest.artifacts.assets}/pages/page_${String(page).padStart(4, "0")}.png`;
  const cropPath = element?.image_path || element?.table_image_path;
  const showingCrop = sourceView === "Crop" && !!cropPath;
  const geometry = element ? bboxToPercentage(element.source) : null;
  const imageUrl = sourceView === "Crop" && cropPath ? asset(cropPath) : asset(pagePath);
  const processed = manual.metadata.pages_processed;
  const recordIndex = records.findIndex(({ node }) => node.id === selected);
  const passed = validation?.checks.filter((check) => check.passed).length ?? 0;
  const pageLabel = (n: number) => t("pageLower", { n });

  function setView(next: View) {
    setViewState(next);
    const hash = VIEWS.find((item) => item.id === next)?.hash;
    if (hash && window.location.hash !== hash) history.replaceState(null, "", window.location.search + hash);
  }
  function select(id: string) {
    setSelected(id);
    setZoom(1);
    setOutlineOpen(false);
  }
  function moveRecord(delta: number) {
    const next = records[Math.max(0, Math.min(records.length - 1, recordIndex + delta))];
    if (next) select(next.node.id);
  }
  function movePage(delta: number) {
    const index = Math.max(0, processed.indexOf(page));
    const next = processed[Math.max(0, Math.min(processed.length - 1, index + delta))];
    const found = records.find(({ node }) => node.page === next);
    if (found) select(found.node.id);
  }
  useEffect(() => {
    canvasRef.current?.scrollTo({ top: 0, left: 0 });
  }, [imageUrl]);
  useEffect(() => {
    document.title = `${manual.title} · manual-ingestion`;
  }, [manual.title]);
  useEffect(() => {
    if (!outlineOpen) return;
    const close = (event: KeyboardEvent) => event.key === "Escape" && setOutlineOpen(false);
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [outlineOpen]);
  // j / k walk the records, like a pager in the terminal.
  useEffect(() => {
    if (view !== "Inspect") return;
    const walk = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey || isTyping(event.target)) return;
      if (event.key === "j") moveRecord(1);
      if (event.key === "k") moveRecord(-1);
    };
    window.addEventListener("keydown", walk);
    return () => window.removeEventListener("keydown", walk);
  });

  return (
    <div className={`workbench view-${view.toLowerCase()}`}>
      <div className="docbar">
        <div className="doc-title">
          <h1>{manual.title}</h1>
          <p className="doc-meta">
            <span>{processed.length === 1 ? t("page1") : t("pages", { n: processed.length })}</span>
            <span>{t(`profile.${manifest.pipeline.profile}` as Key)}</span>
            <span className={manifest.status === "validated" ? "ok" : "warn"}>
              {manifest.status === "validated" ? "[ok]" : "[!]"} {t(`status.${manifest.status}` as Key)}
            </span>
            {validation && <span>{t("checksMeta", { p: passed, t: validation.checks.length })}</span>}
          </p>
        </div>
        <nav className="tabs" aria-label={t("workspace")}>
          {VIEWS.map((item) => (
            <button
              key={item.id}
              aria-current={view === item.id ? "page" : undefined}
              onClick={() => setView(item.id)}
            >
              {t(item.label)}
            </button>
          ))}
        </nav>
      </div>

      <div className="view" key={view}>
        {view === "Overview" && (
          <Overview
            bundle={bundle}
            onInspect={(id) => {
              if (id) { select(id); setSourceView("Page"); }
              setView("Inspect");
            }}
            onValidate={() => setView("Validation")}
          />
        )}

        {view === "Inspect" && (
          <div className="workspace">
            <div className="record-bar">
              <button className="outline-toggle" onClick={() => setOutlineOpen(true)} aria-expanded={outlineOpen}>
                <span aria-hidden="true">☰</span> {t("outline")}
              </button>
              <button aria-label={t("prevRecord")} onClick={() => moveRecord(-1)} disabled={recordIndex <= 0}>‹</button>
              <span className="record-bar-label" data-type={node?.type}>
                {node ? recordLabel(node, typeLabel, pageLabel) : t("noRecords")}
              </span>
              <button aria-label={t("nextRecord")} onClick={() => moveRecord(1)} disabled={recordIndex >= records.length - 1}>›</button>
            </div>

            <aside className={`tree ${outlineOpen ? "open" : ""}`} aria-label={t("outline")}>
              <div className="panel-title">
                <span>{t("outline")}</span>
                <small>{count(t, records.length, "record1", "records")}</small>
                <button className="sheet-close" aria-label={t("closeOutline")} onClick={() => setOutlineOpen(false)}>×</button>
              </div>
              <input
                aria-label={t("filterDocument")}
                placeholder={t("findContent")}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
              <DocumentTree
                nodes={bundle.manual.content}
                selected={selected}
                query={query}
                collapsed={collapsed}
                onSelect={(selectedNode) => select(selectedNode.id)}
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
            {outlineOpen && <div className="sheet-backdrop" onClick={() => setOutlineOpen(false)} aria-hidden="true" />}

            <section className="source-panel" aria-label={t("originalDocument")}>
              <div className="source-toolbar">
                <div>
                  <button aria-label={t("prevPage")} onClick={() => movePage(-1)} disabled={page === processed[0]}>←</button>
                  <span>{t("pageN", { n: page })}</span>
                  <button aria-label={t("nextPage")} onClick={() => movePage(1)} disabled={page === processed.at(-1)}>→</button>
                </div>
                <div>
                  {records.length > 0 && (
                    <span className="kbd-hint" title={t("keyboardHint")}>
                      <kbd>j</kbd><kbd>k</kbd> {recordIndex + 1}/{records.length}
                    </span>
                  )}
                  {cropPath && (
                    <button onClick={() => setSourceView(sourceView === "Page" ? "Crop" : "Page")}>
                      {t(sourceView === "Page" ? "showCrop" : "showPage")}
                    </button>
                  )}
                  <span className="zoom">
                    <button aria-label={t("zoomOut")} disabled={zoom <= 0.5} onClick={() => setZoom((value) => Math.max(0.5, value - 0.25))}>−</button>
                    <button aria-label={t("resetZoom")} onClick={() => setZoom(1)}>{Math.round(zoom * 100)}%</button>
                    <button aria-label={t("zoomIn")} disabled={zoom >= 2.5} onClick={() => setZoom((value) => Math.min(2.5, value + 0.25))}>+</button>
                  </span>
                </div>
              </div>
              <div ref={canvasRef} className="canvas" tabIndex={0} aria-label={t("documentPreview")}>
                <div className="zoom-surface" style={{ width: `${zoom * 100}%` }}>
                  <Preview url={imageUrl} alt={showingCrop ? t("originalCrop") : t("originalPage", { n: page })}>
                    {!showingCrop &&
                      records
                        .filter(({ node }) => node.page === page)
                        .map(({ node }) => {
                          const el = node as ManualElement;
                          const box = bboxToPercentage(el.source);
                          return box ? (
                            <button
                              key={el.id}
                              data-type={el.type}
                              className={`bbox ${selected === el.id ? "active" : ""}`}
                              aria-label={t("selectBox", { type: typeLabel(el.type), id: el.id })}
                              style={{ left: `${box.left}%`, top: `${box.top}%`, width: `${box.width}%`, height: `${box.height}%` }}
                              onClick={() => setSelected(el.id)}
                            >
                              <span className="bbox-label" aria-hidden="true">{typeLabel(el.type)}</span>
                            </button>
                          ) : null;
                        })}
                  </Preview>
                </div>
              </div>
              <div className="source-foot">
                <span>{t(!showingCrop && geometry && imageUrl ? "selectRegion" : "originalEvidence")}</span>
                {imageUrl && <a href={imageUrl} target="_blank" rel="noreferrer">{t("openOriginal")} ↗</a>}
              </div>
            </section>

            <aside className="inspector" tabIndex={0} aria-label={t("inspector")}>
              {element ? (
                <Record key={element.id} element={element} />
              ) : node ? (
                <div className="record">
                  <h2 className="chapter-title">{node.type === "chapter" ? node.title : t("documentWord")}</h2>
                  <p className="empty-note">{t("selectRecord")}</p>
                  <Raw value={node} />
                </div>
              ) : (
                <p className="empty-note">{t("noRecords")}</p>
              )}
            </aside>
          </div>
        )}

        {view === "Validation" && <Validation bundle={bundle} onInspect={() => setView("Inspect")} />}
      </div>
    </div>
  );
}

type Theme = "system" | "light" | "dark";
const THEMES: { id: Theme; glyph: string; label: Key }[] = [
  { id: "system", glyph: "◐", label: "theme.system" },
  { id: "light", glyph: "○", label: "theme.light" },
  { id: "dark", glyph: "●", label: "theme.dark" },
];
const THEME_STORAGE = "manual-ingestion-theme";

function savedTheme(): Theme {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE);
    if (value === "light" || value === "dark") return value;
  } catch {
    // Storage can be unavailable; follow the system.
  }
  return "system";
}

/** System by default; an explicit choice is stamped on <html> and remembered. */
function useTheme(): [Theme, (theme: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(savedTheme);
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") delete root.dataset.theme;
    else root.dataset.theme = theme;
    // The browser bar follows an explicit choice; "system" keeps the media-based defaults.
    for (const meta of document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')) {
      meta.dataset.default ??= meta.content;
      meta.content = theme === "system" ? meta.dataset.default : theme === "dark" ? "#0f1019" : "#fcfcfd";
    }
    try {
      if (theme === "system") window.localStorage.removeItem(THEME_STORAGE);
      else window.localStorage.setItem(THEME_STORAGE, theme);
    } catch {
      // Not persisting is acceptable.
    }
  }, [theme]);
  return [theme, setTheme];
}

const SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

function Loading() {
  const t = useT();
  const [frame, setFrame] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setFrame((value) => (value + 1) % SPINNER.length), 80);
    return () => window.clearInterval(timer);
  }, []);
  return (
    <p className="loading" role="status">
      <span aria-hidden="true">{SPINNER[frame]}</span> {t("reading")}…
    </p>
  );
}

export function App() {
  const [lang, setLangState] = useState<Lang>(detectLang);
  const [theme, setTheme] = useTheme();
  const t = translator(lang);
  const [bundle, setBundle] = useState<LoadedRunBundle | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const active = useRef<BundleSource | null>(null);
  const sequence = useRef(0);
  const folder = useRef<HTMLInputElement>(null);
  function setLang(next: Lang) {
    setLangState(next);
    rememberLang(next);
  }
  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);
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
      if (id === sequence.current) setError(e instanceof Error ? e.message : String(e));
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
    remote(new URLSearchParams(window.location.search).get("run") || "./examples/synthetic-bundle/");
    return () => {
      sequence.current++;
      active.current?.dispose();
    };
  }, []);
  return (
    <LangContext.Provider value={lang}>
      <div className="app">
        <a className="skip" href="#main">{t("skip")}</a>
        <header className="topbar">
          <a className="brand" href="https://github.com/CometaSensitiva/industrial-manual-ingestion">
            <BrandMark />
            <span className="wordmark">manual-ingestion <span>viewer</span></span>
          </a>
          <div className="topbar-actions">
            <div className="switch theme-switch" role="group" aria-label={t("theme")}>
              {THEMES.map((option) => (
                <button key={option.id} aria-pressed={theme === option.id} onClick={() => setTheme(option.id)} aria-label={t(option.label)} title={t(option.label)}>
                  <span aria-hidden="true">{option.glyph}</span>
                </button>
              ))}
            </div>
            <div className="switch lang-switch" role="group" aria-label={t("language")}>
              {LANGS.map((code) => (
                <button key={code} aria-pressed={lang === code} onClick={() => setLang(code)} lang={code}>
                  {code}
                </button>
              ))}
            </div>
            <button className="ghost" onClick={() => setOpen(!open)} aria-expanded={open}>
              {t("openBundle")}
            </button>
          </div>
        </header>
        {open && (
          <div className="open-panel">
            <div className="open-intro">
              <span className="label">{t("openTitle")}</span>
              <p>{t("openIntro")}</p>
            </div>
            <div className="open-actions">
              <button className="primary" onClick={() => folder.current?.click()}>{t("chooseFolder")}</button>
              <button onClick={() => remote("./examples/synthetic-bundle/")}>{t("openExample")}</button>
            </div>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                remote(input);
              }}
            >
              <label htmlFor="url">{t("urlLabel")} <small>{t("urlHint")}</small></label>
              <div>
                <input id="url" placeholder="https://example.org/bundle/" value={input} onChange={(e) => setInput(e.target.value)} />
                <button disabled={busy}>{t("openUrl")}</button>
              </div>
            </form>
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
          </div>
        )}
        {error && (
          <div className="error" role="alert">
            <strong>[!!] {t("loadError")}</strong>
            <p>{error}</p>
            <button onClick={() => setOpen(true)}>{t("chooseAnother")}</button>
          </div>
        )}
        <main id="main" tabIndex={-1}>
          {busy && <Loading />}
          {bundle && <Workbench key={bundle.manifest.run_id + bundle.source?.label} bundle={bundle} />}
        </main>
        <footer>
          <span className="pipeline">{t("pipeline")}</span>
          <a href="https://github.com/CometaSensitiva/industrial-manual-ingestion">{t("sourceCli")} ↗</a>
        </footer>
      </div>
    </LangContext.Provider>
  );
}
