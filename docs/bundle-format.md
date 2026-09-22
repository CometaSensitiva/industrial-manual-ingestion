# Bundle format · schema 1.1

`run.json` is the entry point. Its `artifacts` fields locate the remaining files with relative paths. Consumers must resolve these paths inside the bundle and reject path traversal.

| File | Contents |
| --- | --- |
| `run.json` | Source fingerprint, selected profile, runtime configuration, status and warnings |
| `manual.json` | Metadata and nested chapters containing title, text, image and table elements |
| `toc.json` | Compact hierarchy for navigation |
| `validation.json` | Software checks and technical metrics |
| `assets/` | Image/table crops and optional page rasters |
| `diagnostics/` | Extraction, structure, enrichment and publication evidence |

Print the authoritative schemas with `manual-ingestion schema manual` and `manual-ingestion schema run`.

## Records and provenance

An element carries its source page and, when available, bounding box and page size. Images reference a crop, original caption, generated caption and generation provenance. Tables retain Markdown and header-value serialized rows. `trace.include_in_rag` records retrieval eligibility; it is distinct from presence in the JSON bundle.

The viewer reads source `text` and `caption_original` independently of `caption_generated`. It never treats the existence of an image file as textual baseline information. Other text on the page remains separate records. In Inspect, the manual keeps its collapsible chapter hierarchy and every available bounding box is outlined by element type; the type label appears on hover and for the selected record. Selecting a record increases its emphasis without changing the underlying bundle. The Checks view reads `validation.json` as published (checks, status and metrics) and groups checks by the prefix of their `id`; it does not rerun the Python validator.

Page previews use `assets/pages/page_0001.png` (one-based, four digits). They are optional display artifacts produced with `manual-ingestion ingest ... --page-previews`; they do not change extraction, enrichment or validation results. Only processed pages are rendered.

## Status

- `completed`: processing finished; intermediate publication status.
- `validated`: software promotion gates passed.
- `experimental`: a bundle was produced, with explicit limitations or unmet promotion gates.
- `failed`: processing failed.

A software-valid description can still be factually wrong. Neither `validated` nor `include_in_rag` certifies semantic accuracy. Long captions are retained with a warning. Image descriptions requiring review remain inspectable; tables are serialized without VLM enrichment.

## Scanned documents

Scanned input requires a separate Python environment for PaddleOCR-VL. The backend preflight verifies exact runtime versions and reports missing dependencies before extraction. See `PaddleRuntimeConfig` and the accepted version constants in `src/manual_ingestion/adapters/paddle_adapter.py` for the enforced setup. Use `manual-ingestion ingest --help` for the runtime-path options.

Example isolated setup (Python 3.12):

```sh
python3.12 -m venv .venv_paddle
.venv_paddle/bin/pip install 'paddleocr[doc-parser]==3.7.0' 'paddlepaddle==3.2.1' 'paddlex==3.7.2'
manual-ingestion ingest scanned.pdf --out runs/scanned --paddle-python .venv_paddle/bin/python
```

Model downloads and platform compatibility must be checked on the target machine. The public CI does not run full OCR or model inference.

The public acceptance registry is empty. Scanned bundles can pass structural validation while remaining experimental. This does not prevent publication of their content. No private example is used as a general quality guarantee.

## Compatibility

The Python package distribution is `industrial-manual-ingestion`, its import remains `manual_ingestion`, and its command is `manual-ingestion`. Bundle schema 1.1 and extraction/enrichment behavior are preserved. The CLI now defaults to human output: existing scripts should add `--json`. Caption provenance records the exact Ollama version; any release is accepted, while the model digest, prompt version and output parameters are fixed. English is the default document language; choose `--language it` for Italian.

## Reproduce the public example

With the digital parser installed and Ollama running with `qwen3.5:4b`:

```sh
python examples/build_example.py
```

The script creates the synthetic source, calls the public ingestion API with page previews enabled and validates the published bundle. It refuses to overwrite an existing bundle: move the existing example aside before deliberately regenerating it. Generated diagnostic timestamps and timing fields naturally differ between runs.
