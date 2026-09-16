# Industrial Manual Ingestion

This is the public software product. Keep original manuals, private evaluations,
academic materials, model weights and generated local runs out of Git.

- `src/manual_ingestion`: canonical Python backend and CLI.
- `viewer`: read-only bundle viewer. No ingestion service or upload backend.
- `examples`: synthetic source and real pipeline output for public demonstration.
- `tests`: backend acceptance and regression tests.

Preserve schema 1.1 and local inference unless a change explicitly requires otherwise.
TABLE uses structured serialization. IMAGE uses Qwen enrichment.
Software validation does not certify semantic accuracy. Do not present heuristic
confidence as a measured probability. Scanned OCR remains experimental by default.

Run `pytest`, `npm --prefix viewer test` and `npm --prefix viewer run build` for
relevant changes. Document public interfaces in README and docs/bundle-format.md.
