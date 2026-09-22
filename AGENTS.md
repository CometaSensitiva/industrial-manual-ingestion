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

The CLI and viewer share one identity: the ASCII portrait in `cli_display.LOGO`
(mirrored in `viewer/src/brand.ts`, kept equal by a test) and the lavender/cobalt
palette. Viewer copy lives in `viewer/src/i18n.ts` in English and Italian; bundle
content is never translated. Regenerate `viewer/public/` icons with
`python viewer/scripts/brand_assets.py` after changing the portrait.

Run `pytest`, `npm --prefix viewer test` and `npm --prefix viewer run build` for
relevant changes. Document public interfaces in README and docs/bundle-format.md;
keep README.md (English) and README.it.md (Italian) in step.
