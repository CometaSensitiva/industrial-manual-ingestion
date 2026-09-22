import type { LoadedRunBundle } from "./contracts";
import { count, useT, type Key } from "./i18n";

type Check = { id: string; passed: boolean; message: string };

/** Check id prefixes grouped into four families a reader can reason about. */
const FAMILIES: { key: Key; prefixes: string[] }[] = [
  { key: "family.files", prefixes: ["run", "manifest", "artifacts", "diagnostics", "validation"] },
  { key: "family.structure", prefixes: ["manual", "toc", "pages", "ids", "trace", "source", "run_id"] },
  { key: "family.content", prefixes: ["assets", "captions", "tables"] },
  { key: "family.status", prefixes: ["promotion", "status"] },
];

function byFamily(checks: Check[]) {
  const groups = new Map<Key, Check[]>();
  for (const check of checks) {
    const prefix = check.id.split(".")[0] ?? check.id;
    const key = FAMILIES.find(family => family.prefixes.includes(prefix))?.key ?? "family.other";
    groups.set(key, [...(groups.get(key) ?? []), check]);
  }
  return [...FAMILIES.map(family => family.key), "family.other" as Key]
    .filter(key => groups.has(key))
    .map(key => {
      const items = groups.get(key)!;
      return { key, items, failed: items.filter(item => !item.passed).length };
    })
    .sort((a, b) => Number(b.failed > 0) - Number(a.failed > 0));
}

function Cells({ checks }: { checks: Check[] }) {
  return (
    <span className="cells" aria-hidden="true">
      {checks.map(check => <i key={check.id} className={check.passed ? "pass" : "fail"} title={check.id} />)}
    </span>
  );
}

const num = (value: unknown) => (typeof value === "number" ? value : null);

export function Validation({ bundle, onInspect }: { bundle: LoadedRunBundle; onInspect?: () => void }) {
  const t = useT();
  const { manifest, validation, source } = bundle;
  const checks = validation?.checks ?? [];
  const passed = checks.filter(check => check.passed).length;
  const status = validation?.status ?? "unavailable";
  const token = status === "passed" ? "[ok]" : status === "failed" ? "[!!]" : "[--]";
  const metrics = validation?.metrics ?? {};
  const detected = manifest.source.detected;
  const config = manifest.pipeline.config as Record<string, unknown>;
  const enrichment = config.enrichment as { enabled?: boolean; prompt_version?: string; preflight?: { version?: string; model_digest?: string } } | undefined;
  const files: { path: string | null | undefined; key: string }[] = [
    { path: "run.json", key: "run" },
    { path: manifest.artifacts.manual, key: "manual" },
    { path: manifest.artifacts.toc, key: "toc" },
    { path: manifest.artifacts.validation, key: "validation" },
  ];
  const stats: { key: Key; value: string }[] = [
    { key: "metric.pages", value: `${num(metrics.processed_page_count) ?? manual(bundle)}/${num(metrics.source_pages_total) ?? manifest.source.pages_total}` },
    { key: "metric.chapters", value: String(num(metrics.chapter_count) ?? "–") },
    { key: "metric.elements", value: String(num(metrics.element_count) ?? "–") },
    { key: "metric.captions", value: String(num(metrics.caption_count) ?? "–") },
    { key: "metric.tables", value: String(num(metrics.table_count) ?? "–") },
    { key: "metric.rows", value: String(num(metrics.table_serialized_row_count) ?? "–") },
    { key: "metric.assets", value: String(num(metrics.asset_reference_count) ?? "–") },
    { key: "metric.duplicates", value: String(num(metrics.duplicate_id_count) ?? "–") },
  ];
  const runLines: [string, string][] = [
    ["status", manifest.status],
    ["profile", manifest.pipeline.profile.replaceAll("_", " ")],
    ["parser", manifest.pipeline.parser],
    ["structure", manifest.pipeline.structure_strategy.replaceAll("_", " ")],
    ...(enrichment?.enabled === false ? [["describe", "skipped"] as [string, string]] : [
      ["model", `${manifest.pipeline.enrichment_model ?? "–"}${enrichment?.preflight?.model_digest ? ` · ${enrichment.preflight.model_digest.slice(0, 12)}` : ""}`] as [string, string],
      ["prompt", enrichment?.prompt_version ?? "–"] as [string, string],
      ["ollama", enrichment?.preflight?.version ?? "–"] as [string, string],
    ]),
  ];
  const signals: { key: Key; value: string }[] = [
    { key: "signal.outline", value: t(detected.embedded_outline ? "yes" : "no") },
    { key: "signal.outlineEntries", value: String(detected.outline_entries ?? "–") },
    { key: "signal.textLayer", value: t(detected.text_layer ? "yes" : "no") },
    { key: "signal.pagesWithText", value: `${detected.pages_with_text ?? "–"}/${detected.sampled_pages.length}` },
    { key: "signal.sampled", value: detected.sampled_pages.join(", ") },
  ];

  return (
    <div className="checks">
      <header className={`check-status ${status}`}>
        <div className="check-status-main">
          <span className="status-token" aria-hidden="true">{token}</span>
          <div>
            <h2>{t(status === "passed" ? "checksPassed" : status === "failed" ? "checksFailed" : "noReport")}</h2>
            <p>
              {validation ? t("checksSummary", { p: passed, t: checks.length }) : t("noReportText")}{" "}
              {manifest.warnings.length ? count(t, manifest.warnings.length, "warning1", "warningsCount") : t("noWarnings")}
            </p>
          </div>
          {validation && <strong className="status-count">{passed}<span>/{checks.length}</span></strong>}
        </div>
        {validation ? <Cells checks={checks} /> : <p className="footnote">{t("reportMissingHint")}</p>}
      </header>

      {manifest.errors.length > 0 && (
        <section className="callout negative" aria-label={t("runErrors")}>
          <span className="label">{t("runErrors")}</span>
          <ul className="plain-list">{manifest.errors.map((error, index) => <li key={index}>{error}</li>)}</ul>
        </section>
      )}
      {manifest.warnings.length > 0 && (
        <section className="callout warning" aria-label={t("runWarnings")}>
          <span className="label">{t("runWarnings")}</span>
          <ul className="plain-list">{manifest.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>
        </section>
      )}

      <div className="check-columns">
        <section aria-labelledby="areas-title">
          <h3 id="areas-title" className="label">{t("checksByArea")}</h3>
          <p className="section-hint">{t("checksAreaHint")}</p>
          {validation ? (
            <div className="areas">
              {byFamily(checks).map(({ key, items, failed }) => (
                <details key={key} className={failed ? "area has-failures" : "area"} open={failed > 0}>
                  <summary>
                    <span className="area-name">{t(key)}</span>
                    <Cells checks={items} />
                    <span className="summary-count">{items.length - failed}/{items.length}</span>
                  </summary>
                  {[...items].sort((a, b) => Number(a.passed) - Number(b.passed)).map(check => (
                    <div className={`check ${check.passed ? "passed" : "failed"}`} key={check.id}>
                      <span aria-label={t(check.passed ? "passed" : "failedWord")}>{check.passed ? "[ok]" : "[!!]"}</span>
                      <div><strong>{check.id}</strong><p>{check.message}</p></div>
                    </div>
                  ))}
                </details>
              ))}
            </div>
          ) : <p className="empty-note">{t("noCheckResults")}</p>}

          <section className="caveat" aria-labelledby="caveat-title">
            <h3 id="caveat-title">{t("caveatTitle")}</h3>
            <p>{t("caveatText")}</p>
            {onInspect && <button className="text-link" onClick={onInspect}>{t("compareInInspect")} →</button>}
            <p className="footnote">{t("readOnly")}</p>
          </section>
        </section>

        <aside className="check-side">
          <div className="terminal">
            <div className="terminal-bar" aria-hidden="true"><i /><i /><i /><span>manual-ingestion</span></div>
            <pre>
              <span className="prompt">$ </span>manual-ingestion validate {manifest.run_id}
              {"\n"}<i>├─</i> Result  <b className={status === "passed" ? "ok" : status === "failed" ? "fail" : "warn"}>{token} {status}</b>
              {"\n"}<i>├─</i> Checks  <b>{passed}/{checks.length} passed</b>
              {"\n"}<i>└─</i> Bundle  <b>{manifest.run_id}</b>
              {"\n"}
              {runLines.map(([name, value]) => <span key={name} className="run-line">{"\n"}  {name.padEnd(11)}<b>{value}</b></span>)}
            </pre>
          </div>

          <section aria-labelledby="measured-title">
            <h3 id="measured-title" className="label">{t("measured")}</h3>
            <dl className="stats">
              {stats.map(stat => <div key={stat.key}><dt>{t(stat.key)}</dt><dd>{stat.value}</dd></div>)}
            </dl>
          </section>

          <section aria-labelledby="signals-title">
            <h3 id="signals-title" className="label">{t("routeSignals")} · {t(`profile.${manifest.pipeline.profile}` as Key)}</h3>
            <dl className="kv">
              {signals.map(signal => <div key={signal.key}><dt>{t(signal.key)}</dt><dd>{signal.value}</dd></div>)}
            </dl>
            <details>
              <summary>{t("whyRoute")} <span className="summary-count">{detected.reasons.length}</span></summary>
              <ul className="plain-list">{detected.reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>
            </details>
          </section>

          <section aria-labelledby="files-title">
            <h3 id="files-title" className="label">{t("bundleFiles")}</h3>
            <ul className="files">
              {files.map(file => {
                const url = file.path ? source?.url(file.path) : null;
                const content = <><span><strong>{t(`file.${file.key}` as Key)}</strong><small>{t(`file.${file.key}.text` as Key)}</small></span><code>{file.path ?? t("notIncluded")}</code></>;
                return <li key={file.key}>{url ? <a href={url} target="_blank" rel="noreferrer">{content}<i aria-hidden="true">↗</i></a> : <div>{content}</div>}</li>;
              })}
            </ul>
            <details><summary>{t("runManifest")} <span className="summary-count">JSON</span></summary><pre>{JSON.stringify(manifest, null, 2)}</pre></details>
            {typeof metrics.bundle_fingerprint_sha256 === "string" && (
              <p className="fingerprint"><span>{t("fingerprint")}</span><code>{metrics.bundle_fingerprint_sha256}</code></p>
            )}
          </section>
        </aside>
      </div>
    </div>
  );
}

function manual(bundle: LoadedRunBundle) {
  return bundle.manual.metadata.pages_processed.length;
}
