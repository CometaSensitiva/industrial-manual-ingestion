import { useEffect, useRef, useState } from "react";
import type { LoadedRunBundle } from "./contracts";

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
      if (entry?.isIntersecting) { observer.disconnect(); timer = window.setTimeout(() => type(1), 350); }
    }, { threshold: 0.6 });
    observer.observe(node);
    return () => { observer.disconnect(); window.clearTimeout(timer); };
  }, [text]);
  return { ref, typed: text.slice(0, count), done: count >= text.length };
}

export function CommandCard({ bundle, pageUrl }: { bundle: LoadedRunBundle; pageUrl: string | null }) {
  const [previews, setPreviews] = useState(false);
  useEffect(() => {
    if (!pageUrl) return;
    const probe = new Image();
    probe.onload = () => setPreviews(true);
    probe.src = pageUrl;
  }, [pageUrl]);
  const command = reproduceCommand(bundle, previews);
  const { ref, typed, done } = useTyped(command);
  const status = bundle.manifest.status;

  return (
    <section className="cli-story" aria-labelledby="cli-title">
      <div className="route-copy">
        <span className="eyebrow">MADE WITH THE CLI</span>
        <h3 id="cli-title">One command made this bundle.</h3>
        <p>Run it on your machine to reproduce this run, or swap in your own PDF. Everything stays local.</p>
      </div>
      <div className="terminal-card">
        <pre ref={ref} aria-label={`Command: ${command}`}>
          <span className="prompt" aria-hidden="true">$ </span>
          <span className="typed">
            {/* Each argument stays whole; lines only break between arguments. */}
            {typed.split(/( )/).map((part, i) => (part === " " ? " " : <span key={i} className="arg">{part}</span>))}
          </span>
          {!done && <span className="caret" aria-hidden="true" />}
          {done && <span className="terminal-result" aria-hidden="true">{"\n"}<i>└─</i> bundle <b>{status === "validated" ? "[ok]" : "[!]"}</b> {status}</span>}
        </pre>
      </div>
    </section>
  );
}
