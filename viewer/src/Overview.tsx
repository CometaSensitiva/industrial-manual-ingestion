import { useState } from "react";
import type { LoadedRunBundle, ManualElement, ManualNode } from "./contracts";
import { count, useT, type Key } from "./i18n";
import { RunLog, type RunStep } from "./RunLog";

function collect(nodes: ManualNode[]): ManualNode[] {
  return nodes.flatMap(node => node.type === "chapter" ? [node, ...collect(node.content)] : [node]);
}

function EvidenceImage({ url, alt, fallback }: { url: string | null; alt: string; fallback: string }) {
  const [failed, setFailed] = useState(false);
  return url && !failed
    ? <img src={url} alt={alt} onError={() => setFailed(true)} />
    : <p className="story-unavailable">{fallback}</p>;
}

type TreeLine = { prefix: string; text: string; page: number; chapter: boolean };

/** The first chapters and a few of their records, drawn like `tree` output. */
function treeLines(nodes: ManualNode[], limit = 7): TreeLine[] {
  const lines: TreeLine[] = [];
  const top = nodes.slice(0, 3);
  top.forEach((node, index) => {
    const last = index === top.length - 1;
    const text = node.type === "chapter" ? node.title : node.text || node.caption_original || node.type;
    lines.push({ prefix: last ? "└─ " : "├─ ", text, page: node.page, chapter: node.type === "chapter" });
    if (node.type !== "chapter") return;
    const children = node.content.filter((child): child is ManualElement => child.type !== "chapter").slice(0, 2);
    children.forEach((child, childIndex) => {
      lines.push({
        prefix: (last ? "   " : "│  ") + (childIndex === children.length - 1 ? "└─ " : "├─ "),
        text: child.caption_original || child.text || `${child.type}`,
        page: child.page,
        chapter: false,
      });
    });
  });
  return lines.slice(0, limit);
}

export function Overview({ bundle, onInspect, onValidate }: {
  bundle: LoadedRunBundle;
  onInspect: (id?: string) => void;
  onValidate: () => void;
}) {
  const t = useT();
  const { manual, manifest, validation, source } = bundle;
  const nodes = collect(manual.content);
  const images = nodes.filter((node): node is ManualElement => node.type === "image");
  const described = images.filter(image => image.caption_generated?.trim());
  const tables = nodes.filter(node => node.type === "table");
  const chapters = nodes.filter(node => node.type === "chapter");
  const firstImage = described[0] ?? images[0];
  const firstPage = manual.metadata.pages_processed[0]!;
  const pageUrl = source?.url(`${manifest.artifacts.assets}/pages/page_${String(firstPage).padStart(4, "0")}.png`) ?? null;
  const imageUrl = firstImage?.image_path ? source?.url(firstImage.image_path) ?? null : null;
  const config = manifest.pipeline.config.enrichment;
  const disabled = typeof config === "object" && config !== null && "enabled" in config && config.enabled === false;
  const descriptionTitle = t(disabled ? "visualsSkipped" : images.length === 0 ? "visualsNone" : described.length === 0 ? "visualsMissing" : "visualsDescribed");
  const profile = manifest.pipeline.profile;
  const passed = validation?.checks.filter(check => check.passed).length ?? 0;
  const checksText = validation ? t("checksPassedLink", { p: passed, t: validation.checks.length }) : t("noChecksLink");
  const steps: RunStep[] = [
    { name: "read", value: manifest.pipeline.parser },
    { name: "organize", value: manifest.pipeline.structure_strategy.replaceAll("_", " ") },
    { name: "describe", value: disabled ? "skipped" : `${described.length}/${images.length} images${manifest.pipeline.enrichment_model ? ` · ${manifest.pipeline.enrichment_model}` : ""}`, tone: disabled ? "warn" : undefined },
    { name: "bundle", value: `${manifest.status === "validated" ? "[ok]" : "[!]"} ${manifest.status}${validation ? ` · ${passed}/${validation.checks.length} checks` : ""}`, tone: manifest.status === "validated" ? "ok" : "warn" },
  ];
  const stageHead = (index: string, name: Key) => (
    <div className="stage-head">
      <span className="label"><b>{index}</b>{t(name)}</span>
      <span className="stage-rule" aria-hidden="true" />
    </div>
  );

  return (
    <div className="overview">
      <header className="lede">
        <p>{t("lede")}</p>
        <button className="primary" onClick={() => onInspect()}>{t("inspectCta")} <span aria-hidden="true">→</span></button>
      </header>

      <ol className="stages" aria-label={t("journey")}>
        <li className="stage">
          {stageHead("01", "stageSource")}
          <div className="stage-media page-evidence">
            <EvidenceImage url={pageUrl} alt={t("pdfPageAlt", { n: firstPage })} fallback={t("pagePreviewMissing")} />
          </div>
          <h3>{t("sourceTitle")}</h3>
          <p className="stage-fact">{t("sourceFact", { p: manual.metadata.pages_processed.length, t: manual.metadata.pages_total })}</p>
        </li>
        <li className="stage">
          {stageHead("02", "stageStructure")}
          <div className="stage-media structure-evidence">
            <ul>
              {treeLines(manual.content).map((line, index) => (
                <li key={index} className={line.chapter ? "is-chapter" : undefined}>
                  <i>{line.prefix}</i><span>{line.text}</span><small>p.{line.page}</small>
                </li>
              ))}
            </ul>
            <p className="content-types">{count(t, images.length, "image1", "images")} · {count(t, tables.length, "table1", "tables")} · {t("textWord")}</p>
          </div>
          <h3>{t("structureTitle")}</h3>
          <p className="stage-fact">{count(t, chapters.length, "chapter1", "chapters")} · {count(t, nodes.length - chapters.length, "record1", "records")}</p>
        </li>
        <li className={`stage ${described.length ? "has-descriptions" : "without-descriptions"}`}>
          {stageHead("03", "stageVisuals")}
          <div className="stage-media visual-evidence">
            <EvidenceImage url={imageUrl} alt={firstImage ? t("imageAlt", { n: firstImage.page }) : ""} fallback={t(images.length ? "imagePreviewMissing" : "noImageRecords")} />
          </div>
          <h3>{descriptionTitle}</h3>
          <p className="stage-fact">
            {t("describedFact", { d: described.length, n: images.length })}
            {firstImage && <> · <button className="text-link" onClick={() => onInspect(firstImage.id)}>{t("compare")}</button></>}
          </p>
          {disabled && <p className="stage-note">{t("skippedNote")}</p>}
        </li>
      </ol>

      <section className="run" aria-labelledby="route-title">
        <div className="run-copy">
          <span className="label">{t("howItRan")}</span>
          <h2 id="route-title">{t(`route.${profile}.title` as Key)}</h2>
          <p>{t(`route.${profile}.text` as Key)} {t("reproduce")}</p>
          <button className="text-link" onClick={onValidate}>{checksText} →</button>
        </div>
        <RunLog bundle={bundle} pageUrl={pageUrl} steps={steps} />
      </section>

      <p className="footnote">{t("overviewFootnote")}</p>
    </div>
  );
}
