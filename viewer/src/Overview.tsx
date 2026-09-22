import { useState } from "react";
import type { LoadedRunBundle, ManualElement, ManualNode } from "./contracts";
import { CommandCard } from "./CommandCard";

function collect(nodes: ManualNode[]): ManualNode[] {
  return nodes.flatMap(node => node.type === "chapter" ? [node, ...collect(node.content)] : [node]);
}

function EvidenceImage({ url, alt, fallback }: { url: string | null; alt: string; fallback: string }) {
  const [failed, setFailed] = useState(false);
  return url && !failed
    ? <img src={url} alt={alt} onError={() => setFailed(true)} />
    : <p className="story-unavailable">{fallback}</p>;
}

export function Overview({ bundle, onInspect, onValidate }: {
  bundle: LoadedRunBundle;
  onInspect: (id?: string) => void;
  onValidate: () => void;
}) {
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
  const descriptionTitle = disabled ? "Image descriptions skipped" : images.length === 0 ? "No images to describe" : described.length === 0 ? "No descriptions included" : "Visuals become text";
  const routes = {
    digital_outline: { title: "The PDF already had a usable outline.", text: "The pipeline used it to organize the extracted content into chapters." },
    digital_reconstructed: { title: "The chapter structure needed rebuilding.", text: "The pipeline reconstructed the hierarchy from the extracted content." },
    scanned_ocr: { title: "The pages needed optical character recognition.", text: "The pipeline used OCR to recover content from scanned pages. This route is experimental." },
  };
  const route = routes[manifest.pipeline.profile];
  const checksText = validation ? `${validation.checks.filter(check => check.passed).length}/${validation.checks.length} software checks passed` : "No software check report included";

  return (
    <div className="overview overview-story">
      <header className="story-heading">
        <div>
          <span className="eyebrow">DOCUMENT JOURNEY</span>
          <h2>From pages to usable content.</h2>
          <p>Follow this run from its source PDF to the content included in the bundle.</p>
        </div>
        <button className="primary" onClick={() => onInspect()}>Inspect the document <span aria-hidden="true">→</span></button>
      </header>

      <ol className="story-stages" aria-label="Document processing journey">
        <li className="story-stage source-stage">
          <div className="stage-heading"><span className="stage-number">01</span><span>/ SOURCE</span></div>
          <div className="stage-media page-evidence">
            <EvidenceImage url={pageUrl} alt={`Original PDF page ${firstPage}`} fallback="Page preview not included in this bundle." />
            <span className="evidence-tag">Original · page {firstPage}</span>
          </div>
          <h3>It starts with a PDF.</h3>
          <p>The original document is the reference for every extracted record.</p>
          <div className="stage-fact"><strong>{manual.metadata.pages_processed.length}</strong> of {manual.metadata.pages_total} pages processed</div>
        </li>
        <li className="story-stage structure-stage">
          <div className="stage-heading"><span className="stage-number">02</span><span>/ STRUCTURE</span></div>
          <div className="stage-media structure-evidence">
            <span className="evidence-caption">root / document</span>
            <ul>
              {(chapters.length ? chapters : nodes).slice(0, 3).map(node => (
                <li key={node.id}><span>{node.type === "chapter" ? node.title : node.text || node.caption_original || node.type}</span><small>p. {node.page}</small></li>
              ))}
            </ul>
            <div className="content-types"><span>Text</span><span>{images.length} {images.length === 1 ? "image" : "images"}</span><span>{tables.length} {tables.length === 1 ? "table" : "tables"}</span></div>
          </div>
          <h3>Content finds its place.</h3>
          <p>Records stay connected to their source pages. Tables use structured serialization.</p>
          <div className="stage-fact"><strong>{chapters.length}</strong> {chapters.length === 1 ? "chapter" : "chapters"} · <strong>{nodes.length - chapters.length}</strong> content records</div>
        </li>
        <li className={`story-stage visual-stage ${described.length ? "has-descriptions" : "without-descriptions"}`}>
          <div className="stage-heading"><span className="stage-number">03</span><span>/ VISUALS</span></div>
          <div className="stage-media visual-evidence">
            <EvidenceImage url={imageUrl} alt={firstImage ? `Extracted image from page ${firstImage.page}` : "Extracted image"} fallback={images.length ? "Image preview unavailable." : "This bundle contains no image records."} />
            <span className="evidence-tag">{firstImage ? `Image · page ${firstImage.page}` : "No images"}</span>
          </div>
          <h3>{descriptionTitle}</h3>
          <p>{disabled ? "This run kept the extracted images and did not request generated descriptions." : described.length ? "The local vision model adds descriptions alongside the original images for comparison." : "No generated image descriptions are available in this bundle."}</p>
          <div className="stage-fact"><strong>{described.length}/{images.length}</strong> images with descriptions
            {firstImage && <button className="story-text-link" onClick={() => onInspect(firstImage.id)}>Compare image & text <span aria-hidden="true">↗</span></button>}
          </div>
        </li>
      </ol>

      <section className="route-story" aria-labelledby="route-title">
        <div className="route-copy"><span className="eyebrow">ROUTE / {manifest.pipeline.profile.replaceAll("_", " ").toUpperCase()}</span><h3 id="route-title">{route.title}</h3><p>{route.text}</p></div>
        <dl className="route-tree" aria-label="Pipeline route">
          <div><dt>PDF</dt></div>
          <div><dt><i>├─</i> read</dt><dd>{manifest.pipeline.parser}</dd></div>
          <div><dt><i>├─</i> organize</dt><dd>{manifest.pipeline.structure_strategy.replaceAll("_", " ")}</dd></div>
          <div><dt><i>└─</i> bundle</dt><dd>{manifest.status}</dd></div>
        </dl>
      </section>

      <CommandCard bundle={bundle} pageUrl={pageUrl} />

      <section className="bundle-story" aria-labelledby="bundle-title">
        <div><span className="eyebrow">RESULT</span><h3 id="bundle-title">One bundle. Traceable content.</h3><p>Content, source references and the run report, kept together.</p></div>
        <div className="bundle-files" aria-label="Bundle contents"><code>manual.json</code><code>toc.json</code><code>run.json</code><span>assets/</span></div>
        <button className="story-text-link" onClick={onValidate}>{checksText} <span aria-hidden="true">↗</span></button>
      </section>
      <p className="story-limit">Software checks verify the bundle structure. Generated descriptions still need comparison with the original.</p>
      <div className="story-details">
        <details><summary>Why this profile was selected</summary><ul>{manifest.source.detected.reasons.map((reason, i) => <li key={i}>{reason}</li>)}</ul></details>
        <details><summary>Run warnings ({manifest.warnings.length})</summary>{manifest.warnings.length ? manifest.warnings.map((warning, i) => <p key={i}>{warning}</p>) : <p>No warnings reported.</p>}</details>
      </div>
    </div>
  );
}
