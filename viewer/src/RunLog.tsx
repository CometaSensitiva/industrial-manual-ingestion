import { useEffect, useRef, useState } from "react";
import type { LoadedRunBundle } from "./contracts";
import { useT } from "./i18n";

/** Collapse sorted pages into the CLI's `1-3,5` syntax. */
export function pageRanges(pages: number[]): string {
  const ranges: string[] = [];
  for (let i = 0; i < pages.length; i++) {
    let end = i;
    while (end + 1 < pages.length && pages[end + 1] === pages[end]! + 1) end++;
    ranges.push(end > i ? `${pages[i]}-${pages[end]}` : `${pages[i]}`);
    i = end;
  }
  return ranges.join(",");
}

const quote = (value: string) => (/^[\w./-]+$/.test(value) ? value : `'${value.replaceAll("'", "'\\''")}'`);

/** Rebuild the ingest command that produced this bundle from its manifest. */
export function reproduceCommand(bundle: LoadedRunBundle, previews: boolean): string {
  const { manifest } = bundle;
  const config = manifest.pipeline.config as Record<string, unknown>;
  const pages = config.pages as { selection?: string; processed?: number[] } | undefined;
  const enrichment = config.enrichment as { enabled?: boolean } | undefined;
  const parts = ["manual-ingestion", "ingest", quote(manifest.source.file), "--out", quote(manifest.run_id)];
  if (pages?.selection && pages.selection !== "full_document" && pages.processed?.length) parts.push("--pages", pageRanges(pages.processed));
  if (previews) parts.push("--page-previews");
  if (enrichment?.enabled === false) parts.push("--no-enrich");
  return parts.join(" ");
}

/** Types the command once, when the card scrolls into view. */
function useTyped(text: string) {
  const ref = useRef<HTMLPreElement>(null);
  // Server rendering (tests, previews) shows the whole command at once.
  const [count, setCount] = useState(() => (typeof window === "undefined" ? text.length : 0));
  useEffect(() => {
    const node = ref.current;
    const still = typeof window === "undefined" || !("IntersectionObserver" in window) || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (!node || still) { setCount(text.length); return; }
    setCount(0);
    let timer = 0;
    const type = (at: number) => {
      setCount(at);
      if (at >= text.length) return;
      const glyph = text[at] ?? "";
      // Uneven keystrokes, a longer pause between words and after flags.
      const delay = 18 + Math.random() * 42 + (glyph === " " ? 60 + Math.random() * 90 : 0) + (Math.random() < 0.04 ? 160 : 0);
      timer = window.setTimeout(() => type(at + 1), delay);
    };
    const observer = new IntersectionObserver(([entry]) => {
      if (entry?.isIntersecting) { observer.disconnect(); timer = window.setTimeout(() => type(1), 200); }
    }, { threshold: 0.35 });
    observer.observe(node);
    return () => { observer.disconnect(); window.clearTimeout(timer); };
  }, [text]);
  return { ref, typed: text.slice(0, count), done: count >= text.length };
}

export type RunStep = { name: string; value: string; tone?: "ok" | "warn" };

/** The run as a CLI session: the command is typed, then its result tree prints. */
export function RunLog({ bundle, pageUrl, steps }: { bundle: LoadedRunBundle; pageUrl: string | null; steps: RunStep[] }) {
  const [previews, setPreviews] = useState(false);
  useEffect(() => {
    if (!pageUrl) return;
    const probe = new Image();
    probe.onload = () => setPreviews(true);
    probe.src = pageUrl;
  }, [pageUrl]);
  const t = useT();
  const command = reproduceCommand(bundle, previews);
  const { ref, typed, done } = useTyped(command);

  return (
    <div className="terminal">
      <div className="terminal-bar" aria-hidden="true"><i /><i /><i /><span>manual-ingestion</span></div>
      <pre ref={ref} aria-label={t("commandLabel", { c: command })}>
        <span className="prompt" aria-hidden="true">$ </span>
        <span className="typed">
          {/* Each argument stays whole; lines only break between arguments. */}
          {typed.split(/( )/).map((part, i) => (part === " " ? " " : <span key={i} className="arg">{part}</span>))}
        </span>
        {!done && <span className="caret" aria-hidden="true" />}
        {done && (
          <span className="run-steps">
            {steps.map((step, index) => (
              <span key={step.name} className="run-step" style={{ animationDelay: `${index * 120}ms` }}>
                {"\n"}<i>{index === steps.length - 1 ? "└─" : "├─"}</i> {step.name.padEnd(10)}
                <b className={step.tone}>{step.value}</b>
              </span>
            ))}
          </span>
        )}
      </pre>
    </div>
  );
}
