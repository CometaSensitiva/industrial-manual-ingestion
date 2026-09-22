import type { LoadedRunBundle } from "./contracts";

export function Validation({ bundle }: { bundle: LoadedRunBundle }) {
  const { manifest, validation, source } = bundle;
  const passed = validation?.checks.filter(check => check.passed).length ?? 0;
  const status = validation?.status ?? "unavailable";
  const files = [
    { path: "run.json", name: "Run manifest", description: "Source, route and execution details" },
    { path: manifest.artifacts.manual, name: "Manual content", description: "Chapters, records and source references" },
    { path: manifest.artifacts.toc, name: "Table of contents", description: "The document hierarchy" },
    { path: manifest.artifacts.validation, name: "Validation report", description: "Individual software check results" },
  ];

  return (
    <div className="validation">
      <header className="story-heading">
        <div>
          <span className="eyebrow">BUNDLE REPORT</span>
          <h2>Check the structure. Inspect the meaning.</h2>
          <p>Software checks confirm the bundle contract. Compare generated descriptions with the original to assess their accuracy.</p>
        </div>
      </header>

      <section className={`validation-status ${status}`} aria-label="Published validation report">
        <pre className="status-ascii" aria-hidden="true">{`┌──────┐\n│  ${status === "passed" ? "ok" : status === "failed" ? "!!" : "--"}  │\n└──────┘`}</pre>
        <div className="status-copy">
          <span className="eyebrow">REPORTED RESULT</span>
          <h3>{status === "passed" ? "Software checks passed" : status === "failed" ? "Some checks failed" : "No validation report"}</h3>
          <p>{validation ? `${passed} of ${validation.checks.length} checks passed in the included report.` : "A software check report is not included in this bundle."}</p>
        </div>
        <div className="status-stat"><strong>{manifest.warnings.length}</strong><span>run warnings</span></div>
      </section>
      <p className="report-note">Read-only report · The viewer does not rerun the Python validator. Included in the bundle does not mean semantically verified.</p>

      <div className="validation-columns">
        <section className="report-section" aria-labelledby="checks-title">
          <div className="section-heading"><span className="eyebrow">DIAGNOSTICS</span><h3 id="checks-title">What was checked</h3></div>
          <details open={status !== "passed"}>
            <summary>Software checks <span className="summary-count">{validation ? `${passed}/${validation.checks.length}` : "Not available"}</span></summary>
            {validation ? validation.checks.map(check => (
              <div className={`check ${check.passed ? "passed" : "failed"}`} key={check.id}>
                <span aria-label={check.passed ? "Passed" : "Failed"}>{check.passed ? "[ok]" : "[!!]"}</span>
                <div><strong>{check.id.replaceAll("_", " ")}</strong><p>{check.message}</p></div>
              </div>
            )) : <p>No check results available.</p>}
          </details>
          <details>
            <summary>Run warnings <span className="summary-count">{manifest.warnings.length}</span></summary>
            {manifest.warnings.length ? <ul className="warning-list">{manifest.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul> : <p>No warnings reported.</p>}
          </details>
          {manifest.errors.length > 0 && <details open><summary>Run errors <span className="summary-count">{manifest.errors.length}</span></summary><ul className="warning-list">{manifest.errors.map((error, index) => <li key={index}>{error}</li>)}</ul></details>}
          <details><summary>Run manifest <span className="summary-count">JSON</span></summary><pre>{JSON.stringify(manifest, null, 2)}</pre></details>
        </section>
        <section className="report-section" aria-labelledby="files-title">
          <div className="section-heading"><span className="eyebrow">OUTPUT</span><h3 id="files-title">Inside the bundle</h3></div>
          <div className="artifact-links">
            {files.map(file => {
              const url = file.path ? source?.url(file.path) : null;
              const content = <><span className="file-symbol" aria-hidden="true">{"{ }"}</span><span><strong>{file.name}</strong><small>{file.description}</small><code>{file.path ?? "Not included"}</code></span><span className="file-arrow" aria-hidden="true">{url ? "↗" : "—"}</span></>;
              return url ? <a key={file.name} href={url} target="_blank" rel="noreferrer">{content}</a> : <div className="artifact-unavailable" key={file.name}>{content}</div>;
            })}
          </div>
        </section>
      </div>
    </div>
  );
}
