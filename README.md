# Industrial Manual Ingestion

Turn technical PDFs into structured, traceable content with local image descriptions.

**[Open the viewer](https://CometaSensitiva.github.io/industrial-manual-ingestion/)** · [Bundle format](docs/bundle-format.md)

![Bundle viewer](docs/viewer.png)

One Python CLI processes the document. A read-only browser viewer connects each extracted record to its original page and image. Tables use structured serialization; images receive descriptions from a local Qwen model. No chat service or retrieval system is included.

## Quick start

Requires **Python 3.12**. From a terminal, move into the folder where you cloned or downloaded the project. If you followed the suggested folder name, the command is:

```sh
cd /path/to/industrial-manual-ingestion
```

Replace `/path/to` with the actual parent folder. On macOS, you can type `cd ` and drag the `industrial-manual-ingestion` folder into the terminal to insert its exact path.

For the first setup, create the environment and install the CLI with the digital PDF parser:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
pip install '.[docling]'
```

On later sessions, enter the project directory, activate the existing environment and open the guided help:

```sh
cd /path/to/industrial-manual-ingestion
source .venv/bin/activate
manual-ingestion --help
```

The help includes complete commands that can be copied directly. To try the included public example:

```sh
manual-ingestion detect examples/synthetic-manual.pdf
manual-ingestion ingest examples/synthetic-manual.pdf --out my-first-bundle --no-enrich --page-previews
manual-ingestion validate my-first-bundle
manual-ingestion validate examples/synthetic-bundle
```

The first bundle is intentionally created without Qwen so you can test the complete flow immediately. Its status is therefore `experimental`. The output folder must not already exist.

For image enrichment, run **Ollama 0.34.0** with **qwen3.5:4b**. The backend verifies the runtime and model digest; these are deliberately fixed for reproducible output.

```sh
ollama pull qwen3.5:4b
manual-ingestion ingest examples/synthetic-manual.pdf --out runs/my-manual --page-previews
```

First-time parsing downloads Docling model weights. Enrichment runs locally. Installation and model downloads require network access; documents are sent only to the configured Ollama endpoint (localhost by default).

## CLI

```sh
manual-ingestion detect manual.pdf
manual-ingestion ingest manual.pdf --out runs/manual --language en --page-previews
manual-ingestion validate runs/manual
manual-ingestion detect manual.pdf --json
manual-ingestion validate runs/manual --verbose
manual-ingestion schema manual
```

The interface and help are in English. `--language` records the document language; the preserved technical-caption prompt currently requests Italian fields, so Qwen descriptions may be in Italian.

Normal output is a concise human summary with branded, task-oriented help. `--json` gives undecorated machine-readable stdout; progress and library logs go to stderr. `--verbose` enables additional details. Run `manual-ingestion --help` for the guided workflow, or use `manual-ingestion ingest --help`, `detect --help`, `validate --help` and `schema --help` for focused instructions.

`--page-previews` adds full-page PNGs for the viewer. `--pages 1-3,5` processes a diagnostic subset. `--no-enrich` skips image descriptions. The latter two produce an experimental bundle. The destination must not already exist. `--write-failure-report` saves a report beside the destination on failure.

Scanned PDFs use an isolated PaddleOCR-VL runtime, configured with `--paddle-python` and `--paddle-cache`. See [runtime details](docs/bundle-format.md#scanned-documents). Scanned output remains experimental by default: this release does not claim general acceptance from a private document study.

## Viewer

The public viewer opens a synthetic example automatically. Use **Open bundle** to choose a local folder or enter an HTTP(S) URL. Local folders are read in the browser and are not uploaded. A remote server must allow cross-origin reads.

- **Overview:** document route, content counts and warnings.
- **Inspect:** document tree, source page, always-visible bounding boxes and extracted content. Images, tables, text and titles use the restrained accent palette inherited from the original research viewer.
- **Validation:** published software checks, provenance and JSON.

The source-text panel shows fields attached to the selected element, not a reconstruction of an external retrieval system. Add `--page-previews` during ingestion to inspect complete source pages and bounding boxes. Without it, available crops and extracted content remain inspectable.

Run locally with Node 22 or later:

```sh
cd viewer
npm ci
npm run dev
```

## How it works

```mermaid
flowchart LR
    PDF --> Detection
    Detection --> Outline[Digital with outline]
    Detection --> Reconstruction[Digital without outline]
    Detection --> OCR[Scanned PDF]
    Outline --> Structure[Structured content]
    Reconstruction --> Structure
    OCR --> Structure
    Structure --> Tables[Tables: serialization]
    Structure --> Images[Images: local Qwen]
    Tables --> Bundle[Checked bundle]
    Images --> Bundle
    Bundle --> Viewer
```

## Example and limits

`examples/synthetic-manual.pdf` is an invented two-page equipment manual. The example bundle is generated by the real pipeline, including Qwen inference; it is an illustration, not a benchmark or operating instruction. Its reproduction script is `examples/build_example.py`.

Software validation checks format, provenance and consistency. It does **not** certify semantic correctness. Generated descriptions can miss details or introduce errors. A long caption produces an advisory warning rather than losing the bundle. Routing confidence is a heuristic and is not presented as a measured probability.

The viewer supports bundle schema 1.1. It checks the loaded contract but does not repeat the complete Python validation. The backend supports fixed parser/model setups; it is not a universal model-selection framework.

## Development

```sh
pip install '.[dev,docling]'
pytest
cd viewer
npm ci
npm test
npm run build
```

CI runs the Python suite, builds a wheel, and tests/builds the viewer. GitHub Pages serves only the viewer and synthetic example. Runtime documents, caches and model files are not committed.

## License

Original project code and synthetic example: [MIT](LICENSE). Dependencies retain their own licenses; consult them when redistributing or embedding the complete stack, including PyMuPDF.
